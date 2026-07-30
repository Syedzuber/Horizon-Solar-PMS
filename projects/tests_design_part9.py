"""
Part 9 verification — the Design QC role, the dual serial approval gates, error categories.

WHY THIS FILE EXISTS
--------------------
Part 9 turns one review into two, and almost every way it can go wrong is invisible from a
single screen:

  * a Head could countersign an Arka Design QC has not yet passed, which is a second gate
    in name only;
  * one person holding both flags could clear a site with two clicks, which is also a
    second gate in name only;
  * the CAD/BOQ unlock moved from `verdict` to `head_verdict`, so a stale read anywhere
    would either block a designer who is entitled to upload or let one past both gates;
  * and the rework multiplier now excludes Group B and C failures, so a category stored on
    the wrong attempt would quietly charge a designer for a bad survey.

Every one of those is pinned below, and every refusal is exercised BY DIRECT POST rather
than by asserting a button is missing — a hidden button is not a permission.

Numbered VERIFICATION comments map to the session brief's verification list.
"""
from decimal import Decimal

from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .design_metrics import (
    CAUSE_PM_CHANGE, CAUSE_UNCATEGORISED, classify_attempt_causes, designer_workload,
    tender_metrics,
)
from .models import (
    Program, Project, UserProfile, DesignAssignment, DesignAttempt, ArkaSubmission,
    NotificationLog,
    DESIGN_AWAITING_ALLOCATION, DESIGN_IN_DESIGN, DESIGN_ARKA_SUBMITTED,
    DESIGN_ARKA_REJECTED, DESIGN_ARTIFACTS_UPLOADED, DESIGN_IN_QC, DESIGN_RELEASED,
    DESIGN_AWAITING_HEAD_ARKA, DESIGN_AWAITING_HEAD_QC,
    ARKA_PENDING, ARKA_APPROVED, ARKA_REJECTED,
    QC_PENDING, QC_PASSED, QC_FAILED,
    ATTEMPT_REASON_INITIAL, ATTEMPT_REASON_QC_FAILED, ATTEMPT_REASON_PM_CHANGE_REQUEST,
    ERROR_GROUP_A, ERROR_GROUP_B, ERROR_GROUP_C,
    ERR_LAYOUT, ERR_SURVEY_INADEQUATE, ERR_REQUIREMENT_CHANGED, ERR_BOQ_QUANTITY,
    DESIGN_ERROR_CATEGORIES,
    error_category_group, category_counts_as_designer_rework,
)


def _profile(username, role, is_design_head=False, is_design_qc=False):
    """A post_save signal auto-creates the UserProfile; fetch and set, never create.

    Neither reviewer is a ROLE. Both are flags on a role='Design' user — see the note on
    UserProfile.is_design_qc for why Part 9 did not add a ROLE_CHOICES value.
    """
    user = User.objects.create_user(username=username, password='x')
    profile = user.profile
    profile.role = role
    profile.is_active = True
    profile.is_design_head = is_design_head
    profile.is_design_qc = is_design_qc
    profile.save()
    return profile


class Part9Base(TestCase):
    """Two distinct reviewers, one designer, and one bystander per excluded role."""

    def setUp(self):
        # VERIFICATION setup: a user with is_design_qc and a SEPARATE user with
        # is_design_head. `both` holds both flags and is used for decision 2.
        self.qc       = _profile('qc9',   'Design', is_design_qc=True)
        self.head     = _profile('head9', 'Design', is_design_head=True)
        self.both     = _profile('both9', 'Design', is_design_head=True, is_design_qc=True)
        self.designer = _profile('des9',  'Design')
        # Bystanders — every one of these must be refused every gate action.
        self.pm       = _profile('pm9',   'PM')
        self.scm      = _profile('scm9',  'SCM')
        self.se       = _profile('se9',   'Site Engineer')

        self.program = Program.objects.create(
            name='Test-Part9', program_type='OPEX', client_name='P9Client',
            status='Active', short_tender_code='P9')

    # ── fixtures ────────────────────────────────────────────────────────────

    def _site(self, code, designer=None):
        site = Project(
            project_id=code, customer_name='P9Client', customer_phone='9876543210',
            site_address='1 Sun Rd', city='Delhi', project_type='OPEX',
            program=self.program, site_code=code,
            capacity_kw=Decimal('100.00'), status='Draft')
        site.save()
        a = DesignAssignment.objects.create(
            project=site, status=DESIGN_IN_DESIGN,
            assigned_to=designer or self.designer,
            survey_file_bucket='b', survey_file_path=f'{code}/survey/x.pdf')
        return site, a

    def _submit_arka(self, assignment, version=1, capacity='120.00'):
        """What design_arka_submit writes, without going through the view."""
        attempt = assignment.attempts.filter(
            attempt_number=assignment.current_attempt_number).first()
        if attempt is None:
            attempt = DesignAttempt.objects.create(
                assignment=assignment, attempt_number=1,
                opened_reason=ATTEMPT_REASON_INITIAL)
            assignment.current_attempt_number = 1
        attempt.arka_submissions.filter(is_current=True).update(is_current=False)
        arka = ArkaSubmission.objects.create(
            attempt=attempt, version=version, capacity_kw=Decimal(capacity),
            arka_link='https://example.com/arka', submitted_by=assignment.assigned_to,
            verdict=ARKA_PENDING, is_current=True)
        assignment.status = DESIGN_ARKA_SUBMITTED
        assignment.save()
        return attempt, arka

    def _login(self, profile):
        self.assertTrue(self.client.login(username=profile.user.username, password='x'))

    def _post(self, name, project, **data):
        return self.client.post(
            reverse(name, kwargs={'project_id': project.project_id}), data)


# ===========================================================================
# 1-8. The Arka gates
# ===========================================================================

class ArkaGateTests(Part9Base):

    def setUp(self):
        super().setUp()
        self.site, self.a = self._site('P9-ARKA')
        self.attempt, self.arka = self._submit_arka(self.a)

    def test_01_submission_leaves_both_verdicts_pending(self):
        """VERIFICATION 1."""
        self.arka.refresh_from_db()
        self.assertEqual(self.arka.verdict, ARKA_PENDING)
        self.assertEqual(self.arka.head_verdict, ARKA_PENDING)
        self.assertFalse(self.arka.head_overturned_qc)
        self.assertEqual(self.a.status, DESIGN_ARKA_SUBMITTED)

    def test_02_head_cannot_approve_before_design_qc(self):
        """VERIFICATION 2 — the serial gate, by direct POST."""
        self._login(self.head)
        self._post('design_arka_head_approve', self.site)

        self.arka.refresh_from_db()
        self.a.refresh_from_db()
        self.assertEqual(self.arka.head_verdict, ARKA_PENDING, 'Head verdict was recorded')
        self.assertIsNone(self.arka.head_reviewed_by_id)
        self.assertEqual(self.a.status, DESIGN_ARKA_SUBMITTED)

    def test_03a_qc_rejection_without_category_is_refused(self):
        """VERIFICATION 3, first half."""
        self._login(self.qc)
        self._post('design_arka_reject', self.site, rejection_reason='layout is wrong')

        self.arka.refresh_from_db()
        self.a.refresh_from_db()
        self.assertEqual(self.arka.verdict, ARKA_PENDING, 'rejected with no category')
        self.assertEqual(self.a.status, DESIGN_ARKA_SUBMITTED)

    def test_03b_qc_rejection_with_an_unknown_category_is_refused(self):
        self._login(self.qc)
        self._post('design_arka_reject', self.site,
                   rejection_reason='layout is wrong', error_category='not_a_real_category')
        self.arka.refresh_from_db()
        self.assertEqual(self.arka.verdict, ARKA_PENDING)

    def test_03c_qc_rejection_with_reason_and_category_is_recorded(self):
        """VERIFICATION 3, second half."""
        self._login(self.qc)
        self._post('design_arka_reject', self.site,
                   rejection_reason='row spacing is wrong', error_category=ERR_LAYOUT)

        self.arka.refresh_from_db()
        self.a.refresh_from_db()
        self.assertEqual(self.arka.verdict, ARKA_REJECTED)
        self.assertEqual(self.arka.qc_failure_category, ERR_LAYOUT)
        self.assertEqual(self.arka.rejection_reason, 'row spacing is wrong')
        self.assertEqual(self.arka.reviewed_by_id, self.qc.pk)
        self.assertEqual(self.a.status, DESIGN_ARKA_REJECTED)
        # The Head never saw it, so his gate stays 'not judged'.
        self.assertEqual(self.arka.head_verdict, ARKA_PENDING)
        self.assertFalse(self.arka.head_overturned_qc)

    def test_04_qc_approval_moves_to_awaiting_head(self):
        """VERIFICATION 4."""
        self._login(self.qc)
        self._post('design_arka_approve', self.site)

        self.arka.refresh_from_db()
        self.a.refresh_from_db()
        self.assertEqual(self.arka.verdict, ARKA_APPROVED)
        self.assertEqual(self.arka.reviewed_by_id, self.qc.pk)
        self.assertEqual(self.arka.head_verdict, ARKA_PENDING)
        self.assertEqual(self.a.status, DESIGN_AWAITING_HEAD_ARKA)

    def test_05_cad_upload_refused_while_head_verdict_pending(self):
        """VERIFICATION 5 — by direct POST, the whole point of the Part 3 gate move."""
        self._login(self.qc)
        self._post('design_arka_approve', self.site)

        self._login(self.designer)
        response = self.client.post(
            reverse('design_artifact_upload', kwargs={'project_id': self.site.project_id}),
            {'kind': 'cad_zip'})
        # Refused before storage is touched; no DesignFile row exists either way.
        self.assertEqual(self.attempt.design_files.count(), 0)
        self.assertIn(response.status_code, (302, 403))

    def test_06_head_approval_unlocks_and_returns_to_arka_submitted(self):
        """VERIFICATION 6."""
        self._login(self.qc)
        self._post('design_arka_approve', self.site)
        self._login(self.head)
        self._post('design_arka_head_approve', self.site)

        self.arka.refresh_from_db()
        self.a.refresh_from_db()
        self.assertEqual(self.arka.head_verdict, ARKA_APPROVED)
        self.assertEqual(self.arka.head_reviewed_by_id, self.head.pk)
        self.assertFalse(self.arka.head_overturned_qc, 'gates agreed — not an overturn')
        # No new status is invented for "approved, artifacts outstanding" — see the Part 9
        # note in design_views.
        self.assertEqual(self.a.status, DESIGN_ARKA_SUBMITTED)

        # And the gate helper now admits the upload.
        from .design_views import _require_approved_arka, _current_attempt
        self.assertIsNotNone(_require_approved_arka(_current_attempt(self.a)))

    def test_07a_dual_flag_holder_cannot_record_both_verdicts(self):
        """VERIFICATION 7, first half — settled decision 2, by direct POST."""
        self._login(self.both)
        self._post('design_arka_approve', self.site)          # gate 1, allowed
        self.arka.refresh_from_db()
        self.assertEqual(self.arka.verdict, ARKA_APPROVED)
        self.assertEqual(self.arka.reviewed_by_id, self.both.pk)

        self._post('design_arka_head_approve', self.site)     # gate 2, REFUSED
        self.arka.refresh_from_db()
        self.assertEqual(self.arka.head_verdict, ARKA_PENDING,
                         'one person recorded both verdicts on the same Arka')
        self.assertIsNone(self.arka.head_reviewed_by_id)

    def test_07b_the_same_person_may_record_the_head_verdict_on_another_site(self):
        """VERIFICATION 7, second half — the refusal is per ARTIFACT, not per user."""
        other_site, other_a = self._site('P9-ARKA2')
        _attempt, other_arka = self._submit_arka(other_a)

        # Somebody ELSE passes the second site through Design QC.
        self._login(self.qc)
        self._post('design_arka_approve', other_site)

        # `both` blocked itself on the first site...
        self._login(self.both)
        self._post('design_arka_approve', self.site)
        self._post('design_arka_head_approve', self.site)
        self.arka.refresh_from_db()
        self.assertEqual(self.arka.head_verdict, ARKA_PENDING)

        # ...and is still free to act as Head on the second.
        self._post('design_arka_head_approve', other_site)
        other_arka.refresh_from_db()
        self.assertEqual(other_arka.head_verdict, ARKA_APPROVED)
        self.assertEqual(other_arka.head_reviewed_by_id, self.both.pk)

    def test_08_assigned_designer_holding_both_flags_is_refused_both_gates(self):
        """VERIFICATION 8 — settled decision 3 beats any flag."""
        self.designer.is_design_head = True
        self.designer.is_design_qc = True
        self.designer.save()

        self._login(self.designer)
        r1 = self._post('design_arka_approve', self.site)
        self.assertEqual(r1.status_code, 403)
        self.arka.refresh_from_db()
        self.assertEqual(self.arka.verdict, ARKA_PENDING)

        # And at gate 2, having been passed through gate 1 by somebody else.
        self._login(self.qc)
        self._post('design_arka_approve', self.site)
        self._login(self.designer)
        r2 = self._post('design_arka_head_approve', self.site)
        self.assertEqual(r2.status_code, 403)
        self.arka.refresh_from_db()
        self.assertEqual(self.arka.head_verdict, ARKA_PENDING)

    def test_head_rejection_sets_the_overturn_signal_on_the_arka(self):
        self._login(self.qc)
        self._post('design_arka_approve', self.site)
        self._login(self.head)
        self._post('design_arka_head_reject', self.site,
                   rejection_reason='capacity does not fit the roof',
                   error_category=ERR_BOQ_QUANTITY)

        self.arka.refresh_from_db()
        self.a.refresh_from_db()
        self.assertEqual(self.arka.head_verdict, ARKA_REJECTED)
        self.assertEqual(self.arka.head_failure_category, ERR_BOQ_QUANTITY)
        self.assertTrue(self.arka.head_overturned_qc)
        # Decision 5: the designer's experience is identical at either gate.
        self.assertEqual(self.a.status, DESIGN_ARKA_REJECTED)


# ===========================================================================
# 9-11. The package gates
# ===========================================================================

class PackageGateTests(Part9Base):

    def setUp(self):
        super().setUp()
        self.site, self.a = self._site('P9-PKG')
        self.attempt, self.arka = self._submit_arka(self.a)
        # Drive the Arka through both gates and mark the package complete.
        self.arka.verdict = ARKA_APPROVED
        self.arka.reviewed_by = self.qc
        self.arka.reviewed_at = timezone.now()
        self.arka.head_verdict = ARKA_APPROVED
        self.arka.head_reviewed_by = self.head
        self.arka.head_reviewed_at = timezone.now()
        self.arka.save()
        self.attempt.boq_submitted_at = timezone.now()
        self.attempt.boq_submitted_by = self.designer
        self.attempt.save()
        self.a.status = DESIGN_IN_QC
        self.a.save()
        self.attempt.qc_started_at = timezone.now()
        self.attempt.save()

    def test_09_qc_failure_with_group_a_closes_the_attempt_and_opens_n_plus_1(self):
        """VERIFICATION 9."""
        self._login(self.qc)
        self._post('design_qc_fail', self.site,
                   qc_remarks='string sizing is wrong', error_category=ERR_LAYOUT)

        self.attempt.refresh_from_db()
        self.a.refresh_from_db()
        rows = list(self.a.attempts.order_by('attempt_number'))
        self.assertEqual(len(rows), 2, 'attempt N+1 was not opened')

        n, n1 = rows
        self.assertEqual(n.qc_verdict, QC_FAILED)
        self.assertEqual(n.qc_failure_category, ERR_LAYOUT)
        self.assertEqual(n.qc_remarks, 'string sizing is wrong')
        self.assertEqual(n.qc_reviewed_by_id, self.qc.pk)
        self.assertIsNotNone(n.closed_at)
        # The Head never saw this attempt.
        self.assertEqual(n.head_verdict, QC_PENDING)
        self.assertFalse(n.head_overturned_qc)

        self.assertEqual(n1.attempt_number, 2)
        self.assertEqual(n1.opened_reason, ATTEMPT_REASON_QC_FAILED)
        self.assertEqual(n1.qc_verdict, QC_PENDING)
        self.assertEqual(self.a.status, DESIGN_IN_DESIGN)
        self.assertEqual(self.a.current_attempt_number, 2)

    def test_09b_qc_failure_without_a_category_is_refused(self):
        self._login(self.qc)
        self._post('design_qc_fail', self.site, qc_remarks='something is wrong')
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.qc_verdict, QC_PENDING)
        self.assertEqual(self.a.attempts.count(), 1, 'an attempt was opened anyway')

    def test_qc_pass_does_not_release_the_site(self):
        """The single biggest behaviour change from Part 4."""
        self._login(self.qc)
        self._post('design_qc_pass', self.site)

        self.attempt.refresh_from_db()
        self.a.refresh_from_db()
        self.assertEqual(self.attempt.qc_verdict, QC_PASSED)
        self.assertIsNotNone(self.attempt.head_started_at)
        self.assertIsNone(self.attempt.closed_at, 'attempt closed before the Head ruled')
        self.assertEqual(self.a.status, DESIGN_AWAITING_HEAD_QC)
        self.assertNotEqual(self.a.status, DESIGN_RELEASED)
        self.assertIsNone(self.a.released_at)

    def test_head_pass_releases(self):
        self._login(self.qc)
        self._post('design_qc_pass', self.site)
        self._login(self.head)
        self._post('design_head_qc_pass', self.site)

        self.attempt.refresh_from_db()
        self.a.refresh_from_db()
        self.assertEqual(self.attempt.head_verdict, QC_PASSED)
        self.assertEqual(self.attempt.head_reviewed_by_id, self.head.pk)
        self.assertIsNotNone(self.attempt.closed_at)
        self.assertFalse(self.attempt.head_overturned_qc)
        self.assertEqual(self.a.status, DESIGN_RELEASED)
        self.assertEqual(self.a.released_by_id, self.head.pk)

    def test_10_and_11_head_failure_opens_n_plus_1_and_sets_the_overturn_signal(self):
        """VERIFICATION 10 and 11."""
        self._login(self.qc)
        self._post('design_qc_pass', self.site)
        self.a.refresh_from_db()
        self.assertEqual(self.a.status, DESIGN_AWAITING_HEAD_QC)

        self._login(self.head)
        self._post('design_head_qc_fail', self.site,
                   head_remarks='earthing layout is not to IS',
                   error_category=ERR_LAYOUT)

        self.a.refresh_from_db()
        rows = list(self.a.attempts.order_by('attempt_number'))
        self.assertEqual(len(rows), 2)
        n, n1 = rows

        self.assertEqual(n.qc_verdict, QC_PASSED)
        self.assertEqual(n.head_verdict, QC_FAILED)
        self.assertEqual(n.head_failure_category, ERR_LAYOUT)
        self.assertEqual(n.head_remarks, 'earthing layout is not to IS')
        self.assertEqual(n.head_reviewed_by_id, self.head.pk)
        # VERIFICATION 11 — the overturn signal, stored and queryable.
        self.assertTrue(n.head_overturned_qc)
        self.assertEqual(n1.opened_reason, ATTEMPT_REASON_QC_FAILED)
        self.assertEqual(self.a.status, DESIGN_IN_DESIGN)

    def test_11b_the_overturn_signal_is_countable_per_reviewer_and_per_tender(self):
        """VERIFICATION 11 — 'countable' means countable in SQL, not in Python."""
        self._login(self.qc)
        self._post('design_qc_pass', self.site)
        self._login(self.head)
        self._post('design_head_qc_fail', self.site,
                   head_remarks='not to standard', error_category=ERR_LAYOUT)

        per_reviewer = (DesignAttempt.objects
                        .filter(head_overturned_qc=True, qc_reviewed_by=self.qc)
                        .count())
        per_tender = (DesignAttempt.objects
                      .filter(head_overturned_qc=True,
                              assignment__project__program=self.program)
                      .count())
        self.assertEqual(per_reviewer, 1)
        self.assertEqual(per_tender, 1)

    def test_head_cannot_rule_while_qc_verdict_is_pending(self):
        """The serial gate on the package side, by direct POST."""
        self._login(self.head)
        self._post('design_head_qc_pass', self.site)
        self.attempt.refresh_from_db()
        self.a.refresh_from_db()
        self.assertEqual(self.attempt.head_verdict, QC_PENDING)
        self.assertNotEqual(self.a.status, DESIGN_RELEASED)

    def test_dual_flag_holder_cannot_pass_a_package_it_qcd_itself(self):
        self._login(self.both)
        self._post('design_qc_pass', self.site)
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.qc_verdict, QC_PASSED)

        self._post('design_head_qc_pass', self.site)
        self.attempt.refresh_from_db()
        self.a.refresh_from_db()
        self.assertEqual(self.attempt.head_verdict, QC_PENDING)
        self.assertNotEqual(self.a.status, DESIGN_RELEASED)


# ===========================================================================
# 12. Error category groups and the three rework figures
# ===========================================================================

class ErrorCategoryTests(TestCase):

    def test_every_category_has_exactly_one_group(self):
        for cat in DESIGN_ERROR_CATEGORIES:
            self.assertIn(error_category_group(cat), (ERROR_GROUP_A, ERROR_GROUP_B,
                                                      ERROR_GROUP_C), cat)

    def test_group_membership_matches_the_brief(self):
        self.assertEqual(error_category_group(ERR_LAYOUT), ERROR_GROUP_A)
        self.assertEqual(error_category_group(ERR_BOQ_QUANTITY), ERROR_GROUP_A)
        self.assertEqual(error_category_group(ERR_SURVEY_INADEQUATE), ERROR_GROUP_B)
        self.assertEqual(error_category_group(ERR_REQUIREMENT_CHANGED), ERROR_GROUP_C)

    def test_blank_and_unknown_categories_are_group_none_and_not_designer_rework(self):
        for value in ('', None, 'made_up'):
            self.assertIsNone(error_category_group(value))
            self.assertFalse(category_counts_as_designer_rework(value))

    def test_only_group_a_counts_as_designer_rework(self):
        self.assertTrue(category_counts_as_designer_rework(ERR_LAYOUT))
        self.assertFalse(category_counts_as_designer_rework(ERR_SURVEY_INADEQUATE))
        self.assertFalse(category_counts_as_designer_rework(ERR_REQUIREMENT_CHANGED))


class ReworkMultiplierTests(Part9Base):
    """VERIFICATION 12 — three figures, reported distinctly, never merged."""

    def _released_site_with_causes(self, code, failures):
        """Build a released site whose attempts were opened by `failures`.

        `failures` is a list of (category, opened_reason) describing how each attempt
        AFTER the first came about. The category is written on the attempt that FAILED —
        one row earlier than the attempt it opened — which is exactly the relationship
        classify_attempt_causes() has to reconstruct.
        """
        site, a = self._site(code)
        attempts = [DesignAttempt.objects.create(
            assignment=a, attempt_number=1, opened_reason=ATTEMPT_REASON_INITIAL)]

        for i, (category, reason) in enumerate(failures, start=1):
            previous = attempts[-1]
            if reason == ATTEMPT_REASON_QC_FAILED:
                previous.qc_verdict = QC_FAILED
                previous.qc_remarks = 'failed'
                previous.qc_failure_category = category
                previous.save()
            previous.closed_at = timezone.now()
            previous.save()
            attempts.append(DesignAttempt.objects.create(
                assignment=a, attempt_number=i + 1, opened_reason=reason))

        a.current_attempt_number = len(attempts)
        a.status = DESIGN_RELEASED
        a.released_at = timezone.now()
        a.released_by = self.head
        a.save()
        return site, a, attempts

    def test_group_b_failure_does_not_increment_the_designer_multiplier(self):
        """VERIFICATION 12 — all three figures printed and asserted."""
        # Site 1: two attempts, the second caused by a GROUP A failure  -> designer rework
        self._released_site_with_causes(
            'P9-RW-A', [(ERR_LAYOUT, ATTEMPT_REASON_QC_FAILED)])
        # Site 2: two attempts, the second caused by a GROUP B failure  -> input quality
        self._released_site_with_causes(
            'P9-RW-B', [(ERR_SURVEY_INADEQUATE, ATTEMPT_REASON_QC_FAILED)])
        # Site 3: two attempts, the second caused by a PM change request -> its own figure
        self._released_site_with_causes(
            'P9-RW-PM', [('', ATTEMPT_REASON_PM_CHANGE_REQUEST)])

        m = tender_metrics(self.program)
        rows = [w for w in m['workload'] if w['designer'].pk == self.designer.pk]
        self.assertEqual(len(rows), 1)
        row = rows[0]

        print('\n--- VERIFICATION 12: the three rework figures ---')
        print(f"  released sites             : {row['released']}")
        print(f"  total attempts             : {row['attempts']}")
        print(f"  designer-error attempts (A): {row['designer_error_attempts']}")
        print(f"  input-problem attempts (B/C): {row['input_problem_attempts']}")
        print(f"  pm change request attempts : {row['pm_change_request']}")
        print(f"  rework multiplier          : {row['rework']}")
        print(f"  input-quality multiplier   : {row['input_quality']}")
        print(f"  pm-change multiplier       : {row['pm_change_multiplier']}")

        self.assertEqual(row['released'], 3)
        self.assertEqual(row['attempts'], 6)
        self.assertEqual(row['designer_error_attempts'], 1)
        self.assertEqual(row['input_problem_attempts'], 1)
        self.assertEqual(row['pm_change_request'], 1)

        # The Group B attempt is EXCLUDED from the numerator: 6 - 1 = 5, over 3 released.
        self.assertEqual(row['rework'], round(5 / 3, 1))
        # ...and counted here instead.
        self.assertEqual(row['input_quality'], round(1 / 3, 1))
        self.assertEqual(row['pm_change_multiplier'], round(1 / 3, 1))
        # The three are distinct numbers, never summed into one.
        self.assertNotEqual(row['rework'], row['input_quality'])

    def test_group_c_is_also_excluded_from_designer_rework(self):
        self._released_site_with_causes(
            'P9-RW-C', [(ERR_REQUIREMENT_CHANGED, ATTEMPT_REASON_QC_FAILED)])
        m = tender_metrics(self.program)
        row = [w for w in m['workload'] if w['designer'].pk == self.designer.pk][0]
        self.assertEqual(row['input_problem_attempts'], 1)
        self.assertEqual(row['designer_error_attempts'], 0)
        # 2 attempts - 1 input-caused = 1, over 1 released site.
        self.assertEqual(row['rework'], 1.0)

    def test_a_pre_part_9_failure_with_no_category_is_counted_and_surfaced(self):
        """Historical rows must not silently shrink the multiplier."""
        self._released_site_with_causes(
            'P9-RW-LEGACY', [('', ATTEMPT_REASON_QC_FAILED)])
        m = tender_metrics(self.program)
        row = [w for w in m['workload'] if w['designer'].pk == self.designer.pk][0]
        self.assertEqual(row['uncategorised_attempts'], 1)
        self.assertEqual(row['input_problem_attempts'], 0)
        # Still in the designer numerator — 2 attempts, nothing excluded.
        self.assertEqual(row['rework'], 2.0)

    def test_classify_attempt_causes_reads_the_previous_attempts_category(self):
        _site, _a, attempts = self._released_site_with_causes(
            'P9-RW-CLS', [(ERR_LAYOUT, ATTEMPT_REASON_QC_FAILED),
                          (ERR_SURVEY_INADEQUATE, ATTEMPT_REASON_QC_FAILED),
                          ('', ATTEMPT_REASON_PM_CHANGE_REQUEST)])
        causes = classify_attempt_causes(attempts)
        self.assertIsNone(causes[1], 'the initial attempt is not rework')
        self.assertEqual(causes[2], ERROR_GROUP_A)
        self.assertEqual(causes[3], ERROR_GROUP_B)
        self.assertEqual(causes[4], CAUSE_PM_CHANGE)

    def test_head_failure_category_is_preferred_over_the_qc_one(self):
        """A Head failure carries head_failure_category and an empty qc one."""
        site, a = self._site('P9-RW-HEAD')
        first = DesignAttempt.objects.create(
            assignment=a, attempt_number=1, opened_reason=ATTEMPT_REASON_INITIAL,
            qc_verdict=QC_PASSED, head_verdict=QC_FAILED, head_remarks='no',
            head_failure_category=ERR_SURVEY_INADEQUATE, head_overturned_qc=True)
        second = DesignAttempt.objects.create(
            assignment=a, attempt_number=2, opened_reason=ATTEMPT_REASON_QC_FAILED)
        causes = classify_attempt_causes([first, second])
        self.assertEqual(causes[2], ERROR_GROUP_B)


# ===========================================================================
# 13-14. Dashboards and role exclusion
# ===========================================================================

class DesignQcDashboardTests(Part9Base):
    """VERIFICATION 13 — the subset, by direct URL."""

    def setUp(self):
        super().setUp()
        self.site, self.a = self._site('P9-DASH')
        self._submit_arka(self.a)
        self.url = reverse('design_qc_dashboard', kwargs={'pk': self.program.pk})

    def test_design_qc_sees_stage_counts_and_their_own_queue(self):
        self._login(self.qc)
        r = self.client.get(self.url)
        self.assertEqual(r.status_code, 200)
        self.assertIn('stages', r.context['m'])
        self.assertIn('queue', r.context['m'])
        self.assertIn('attention', r.context['m'])
        self.assertContains(r, 'Your review queue')
        self.assertContains(r, 'Where sites are')

    def test_design_qc_dashboard_omits_workload_rework_and_capacity(self):
        """The exclusion is in the CONTEXT, not a template {% if %}."""
        self._login(self.qc)
        r = self.client.get(self.url)
        m = r.context['m']
        self.assertNotIn('workload', m)
        self.assertNotIn('capacity', m)
        self.assertNotContains(r, 'Designer workload')
        self.assertNotContains(r, 'Capacity realisation')
        self.assertNotContains(r, 'Rework')

    def test_the_head_may_also_open_it(self):
        self._login(self.head)
        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_designer_pm_scm_and_site_engineer_are_refused(self):
        for profile in (self.designer, self.pm, self.scm, self.se):
            self._login(profile)
            self.assertEqual(self.client.get(self.url).status_code, 403,
                             f'{profile.user.username} reached the QC dashboard')

    def test_design_qc_is_refused_the_full_head_dashboard(self):
        """The workload table and capacity panel stay Head-only."""
        head_url = reverse('design_tender_dashboard', kwargs={'pk': self.program.pk})
        self._login(self.qc)
        self.assertEqual(self.client.get(head_url).status_code, 403)


class ReviewQueueReachabilityTests(Part9Base):
    """The queue must carry ARKAS, not just packages.

    Regression test for a real defect found by using the product: a Design QC reviewer's
    dashboard correctly reported "2 Arka awaiting your verdict" while the review queue
    showed nothing, because the queue listed only the three package statuses. The only
    route to design_head_review was the Head's per-tender site list, which is gated on Head
    authority — so the number was true and unclickable.
    """

    def setUp(self):
        super().setUp()
        self.site, self.a = self._site('P9-REACH')
        self.attempt, self.arka = self._submit_arka(self.a)
        self.url = reverse('design_qc_queue')

    def test_a_submitted_arka_appears_in_the_design_qc_queue(self):
        self._login(self.qc)
        r = self.client.get(self.url)
        self.assertEqual(r.status_code, 200)
        codes = [row['site'].project_id for row in r.context['arka_rows']]
        self.assertIn('P9-REACH', codes)
        # And it is actionable by this reviewer, with a link to the Arka screen.
        row = [x for x in r.context['arka_rows'] if x['site'].project_id == 'P9-REACH'][0]
        self.assertTrue(row['can_qc'])
        self.assertFalse(row['can_head'])
        self.assertContains(
            r, reverse('design_head_review', kwargs={'project_id': 'P9-REACH'}))

    def test_an_arka_awaiting_the_head_appears_and_is_his_to_act_on(self):
        self._login(self.qc)
        self._post('design_arka_approve', self.site)

        self._login(self.head)
        r = self.client.get(self.url)
        row = [x for x in r.context['arka_rows'] if x['site'].project_id == 'P9-REACH'][0]
        self.assertTrue(row['awaiting_head'])
        self.assertTrue(row['can_head'])
        self.assertFalse(row['can_qc'])

        # Design QC still SEES it — knowing what is stacked at the other gate is the point
        # — but can no longer act on it.
        self._login(self.qc)
        r = self.client.get(self.url)
        row = [x for x in r.context['arka_rows'] if x['site'].project_id == 'P9-REACH'][0]
        self.assertFalse(row['can_qc'])
        self.assertFalse(row['can_head'])

    def test_a_fully_approved_arka_leaves_the_queue(self):
        """It is with the DESIGNER, owing CAD and BOQ — not in anybody's review queue."""
        self._login(self.qc)
        self._post('design_arka_approve', self.site)
        self._login(self.head)
        self._post('design_arka_head_approve', self.site)

        self.a.refresh_from_db()
        self.assertEqual(self.a.status, DESIGN_ARKA_SUBMITTED)   # the "artifacts outstanding" state
        r = self.client.get(self.url)
        codes = [row['site'].project_id for row in r.context['arka_rows']]
        self.assertNotIn('P9-REACH', codes,
                         'a both-gates-approved Arka is still sitting in a review queue')

    def test_a_both_gates_approved_site_does_not_claim_to_be_awaiting_anybody(self):
        """`arka_submitted` means two different things and must not render as one.

        The stored status is the same before Design QC has looked at the Arka and after the
        Head has approved it. A chip reading "Arka submitted" in the second case sent a
        Design Head hunting a site he had already cleared.
        """
        self._login(self.qc)
        self._post('design_arka_approve', self.site)
        self._login(self.head)
        self._post('design_arka_head_approve', self.site)

        r = self.client.get(reverse('design_head_review',
                                    kwargs={'project_id': 'P9-REACH'}))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'CAD and BOQ outstanding')
        self.assertNotContains(r, 'awaiting Design Head')
        self.assertNotContains(r, 'Arka awaiting Design QC')

    def test_a_site_still_at_gate_1_says_so(self):
        """The other half of the same chip — it must not read as approved either."""
        r = None
        self._login(self.qc)
        r = self.client.get(reverse('design_head_review',
                                    kwargs={'project_id': 'P9-REACH'}))
        self.assertContains(r, 'Arka awaiting Design QC')
        self.assertNotContains(r, 'CAD and BOQ outstanding')

    def test_a_dual_flag_holder_sees_its_own_qc_verdict_blocking_the_head_row(self):
        self._login(self.both)
        self._post('design_arka_approve', self.site)
        r = self.client.get(self.url)
        row = [x for x in r.context['arka_rows'] if x['site'].project_id == 'P9-REACH'][0]
        self.assertFalse(row['can_head'])
        self.assertTrue(row['blocked_own'])
        # Phrase chosen to sit on ONE source line in the template — the surrounding
        # sentence is wrapped, and a substring spanning the wrap would never match.
        self.assertContains(r, 'through Design QC yourself')

    def test_the_attention_list_links_arka_stages_to_the_arka_screen(self):
        """A package link on an Arka-stage row leads to 'nothing to review yet'."""
        m = tender_metrics(self.program)
        rows = [r for r in m['qc_attention']
                if r['project'].project_id == 'P9-REACH']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['stage_key'], 'arka_submitted')

        self._login(self.qc)
        r = self.client.get(reverse('design_qc_dashboard',
                                    kwargs={'pk': self.program.pk}))
        self.assertContains(
            r, reverse('design_head_review', kwargs={'project_id': 'P9-REACH'}))


class TemplateCommentSyntaxTests(TestCase):
    """No multi-line `{# ... #}` comment anywhere in the template tree.

    REGRESSION TEST FOR A BUG THE COMPILE CHECK CANNOT SEE. Django's lexer matches
    comments with `{#.*?#}` and NO re.DOTALL, so a `{#` whose closing `#}` is on a later
    line is never recognised as a comment — the entire block renders as literal page text,
    in the middle of the UI, and the template still "compiles" perfectly.

    Seven of these shipped in the first Part 9 commit and one was visible on the Arka
    review screen. `get_template()` passing proves nothing here, which is why this walks
    the source instead. Multi-line comments must use {% comment %}.
    """

    def test_no_multiline_hash_comments_in_any_template(self):
        import glob
        import os
        import re

        root = os.path.join(os.path.dirname(__file__), 'templates')
        offenders = []
        for path in glob.glob(os.path.join(root, '**', '*.html'), recursive=True):
            with open(path, encoding='utf-8') as fh:
                for lineno, line in enumerate(fh, start=1):
                    for match in re.finditer(r'\{#', line):
                        if '#}' not in line[match.end():]:
                            rel = os.path.relpath(path, root).replace(os.sep, '/')
                            offenders.append(f'{rel}:{lineno}  {line.strip()[:70]}')

        self.assertEqual(
            offenders, [],
            'Multi-line {# #} comments render as visible page text — use '
            '{% comment %} instead:\n  ' + '\n  '.join(offenders))


class RoleExclusionTests(Part9Base):
    """VERIFICATION 14 — every excluded role, every gate endpoint, by direct POST."""

    def setUp(self):
        super().setUp()
        self.site, self.a = self._site('P9-EXCL')
        self.attempt, self.arka = self._submit_arka(self.a)

    def test_every_gate_action_refuses_designer_pm_scm_and_site_engineer(self):
        endpoints = [
            ('design_arka_approve',      {}),
            ('design_arka_reject',       {'rejection_reason': 'x', 'error_category': ERR_LAYOUT}),
            ('design_arka_head_approve', {}),
            ('design_arka_head_reject',  {'rejection_reason': 'x', 'error_category': ERR_LAYOUT}),
            ('design_qc_start',          {}),
            ('design_qc_pass',           {}),
            ('design_qc_fail',           {'qc_remarks': 'x', 'error_category': ERR_LAYOUT}),
            ('design_head_qc_pass',      {}),
            ('design_head_qc_fail',      {'head_remarks': 'x', 'error_category': ERR_LAYOUT}),
        ]
        for profile in (self.designer, self.pm, self.scm, self.se):
            self._login(profile)
            for name, data in endpoints:
                r = self._post(name, self.site, **data)
                self.assertEqual(
                    r.status_code, 403,
                    f'{profile.user.username} was not refused {name} '
                    f'(got {r.status_code})')

        # Nothing moved.
        self.arka.refresh_from_db()
        self.a.refresh_from_db()
        self.assertEqual(self.arka.verdict, ARKA_PENDING)
        self.assertEqual(self.arka.head_verdict, ARKA_PENDING)
        self.assertEqual(self.a.status, DESIGN_ARKA_SUBMITTED)


# ===========================================================================
# 15-16. Constraints and blast radius
# ===========================================================================

class HeadConstraintTests(Part9Base):
    """The two new CHECK constraints, mirroring the migration-0049 pair."""

    def test_head_rejected_arka_requires_a_reason(self):
        _site, a = self._site('P9-CON1')
        attempt, arka = self._submit_arka(a)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                arka.head_verdict = ARKA_REJECTED
                arka.head_rejection_reason = ''
                arka.save()

    def test_head_failed_attempt_requires_remarks(self):
        _site, a = self._site('P9-CON2')
        attempt, _arka = self._submit_arka(a)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                attempt.head_verdict = QC_FAILED
                attempt.head_remarks = ''
                attempt.save()

    def test_the_part_1_constraints_still_guard_the_qc_side(self):
        """Decision 4 — migration 0049 continues to protect exactly the right field."""
        _site, a = self._site('P9-CON3')
        attempt, arka = self._submit_arka(a)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                arka.verdict = ARKA_REJECTED
                arka.rejection_reason = ''
                arka.save()


class BlastRadiusTests(Part9Base):
    """VERIFICATION 16 — nothing outside the OPEX design module was touched."""

    def test_no_residential_project_task_or_boq_row_is_created_or_changed(self):
        from .models import Task, BOQ
        residential_before = Project.objects.filter(project_type='Residential').count()
        tasks_before = Task.objects.count()
        boq_before = BOQ.objects.count()
        notifications_before = NotificationLog.objects.count()

        site, a = self._site('P9-BLAST')
        attempt, arka = self._submit_arka(a)
        self._login(self.qc)
        self._post('design_arka_approve', site)
        self._login(self.head)
        self._post('design_arka_head_approve', site)

        self.assertEqual(Project.objects.filter(project_type='Residential').count(),
                         residential_before)
        self.assertEqual(Task.objects.count(), tasks_before)
        self.assertEqual(BOQ.objects.count(), boq_before)
        # No notifications — Part 9 explicitly adds none (that is Part 7).
        self.assertEqual(NotificationLog.objects.count(), notifications_before)

    def test_neither_gate_writes_a_notification(self):
        site, a = self._site('P9-NONOTIF')
        attempt, arka = self._submit_arka(a)
        before = NotificationLog.objects.count()

        self._login(self.qc)
        self._post('design_arka_reject', site,
                   rejection_reason='layout', error_category=ERR_LAYOUT)
        self.assertEqual(NotificationLog.objects.count(), before)
