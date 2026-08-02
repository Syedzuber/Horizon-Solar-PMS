"""
Part 4.6 verification — the Design Head triages PM change requests.

WHY THIS FILE EXISTS
--------------------
Part 4 let a PM change request open a new attempt automatically. Part 4.6 puts the Design
Head in front of that, and nearly every way the amendment can go wrong is invisible from a
single screen:

  * a raise that still opens an attempt would leave the triage as decoration;
  * a rejection recorded without a reason would make the triage a rubber stamp;
  * a suspension keyed off `resulting_attempt` rather than `verdict` would keep a review
    suspended forever after a REJECTION, because that column stays null;
  * an acceptance that marked the closed attempt 'failed' would charge the designer with a
    rework loop the PM caused — the exact corruption `opened_reason` exists to prevent;
  * and a second pending request on one attempt would give the Head two verdicts to record
    against one suspension.

Every one of those is pinned below. Every refusal is exercised BY DIRECT POST, never by
asserting a button is absent — a hidden button is not a permission — and the two rules the
brief requires the DATABASE to enforce are exercised against the ORM with the view out of
the way.

Numbered VERIFICATION comments map to the session brief's verification list.
"""
from decimal import Decimal

from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .design_metrics import designer_workload, tender_metrics
from .models import (
    Program, Project, UserProfile, DesignAssignment, DesignAttempt, ArkaSubmission,
    DesignFile, DesignChangeRequest, SiteGroup, SiteGroupMembership, NotificationLog,
    DESIGN_IN_DESIGN, DESIGN_ARKA_SUBMITTED, DESIGN_IN_QC, DESIGN_RELEASED,
    DESIGN_AWAITING_HEAD_QC,
    ARKA_APPROVED, QC_PENDING, QC_PASSED,
    ATTEMPT_REASON_INITIAL, ATTEMPT_REASON_PM_CHANGE_REQUEST,
    CHANGE_REQUEST_PENDING, CHANGE_REQUEST_ACCEPTED, CHANGE_REQUEST_REJECTED,
    DESIGN_FILE_CAD_ZIP, SITE_GROUP_DRAFT, SITE_GROUP_LOCKED,
    ERR_LAYOUT,
)


def _profile(username, role, is_design_head=False, is_design_qc=False):
    """A post_save signal auto-creates the UserProfile; fetch and set, never create."""
    user = User.objects.create_user(username=username, password='x')
    profile = user.profile
    profile.role = role
    profile.is_active = True
    profile.is_design_head = is_design_head
    profile.is_design_qc = is_design_qc
    profile.save()
    return profile


class Part46Base(TestCase):
    """A site with a complete package sitting in Design QC, and one of every role."""

    def setUp(self):
        self.head     = _profile('head46', 'Design', is_design_head=True)
        self.deputy   = _profile('dep46',  'Design')
        self.qc       = _profile('qc46',   'Design', is_design_qc=True)
        self.designer = _profile('des46',  'Design')
        self.pm       = _profile('pm46',   'PM')
        self.scm      = _profile('scm46',  'SCM')
        self.se       = _profile('se46',   'Site Engineer')

        # The deputy is named on the HEAD's profile — the Part 4 helper, reused untouched.
        self.head.design_head_deputy = self.deputy
        self.head.save(update_fields=['design_head_deputy'])

        self.program = Program.objects.create(
            name='Test-Part46', program_type='OPEX', client_name='P46Client',
            status='Active', short_tender_code='P46')

        self.site, self.a = self._site('P46-A')
        self.attempt = self._package(self.a)

    # ── fixtures ────────────────────────────────────────────────────────────

    def _site(self, code):
        site = Project(
            project_id=code, customer_name='P46Client', customer_phone='9876543210',
            site_address='1 Sun Rd', city='Delhi', project_type='OPEX',
            program=self.program, site_code=code,
            capacity_kw=Decimal('100.00'), status='Draft',
            assigned_design=self.designer, assigned_pm=self.pm)
        site.save()
        assignment = DesignAssignment.objects.create(
            project=site, status=DESIGN_IN_DESIGN, assigned_to=self.designer,
            survey_file_bucket='b', survey_file_path=f'{code}/survey/x.pdf')
        return site, assignment

    def _package(self, assignment, number=1, reason=ATTEMPT_REASON_INITIAL):
        """A reviewable package on a fresh attempt: Arka approved at both gates, a current
        cad_zip, BOQ marked complete. Written directly — the gates are Part 9's tests."""
        attempt = DesignAttempt.objects.create(
            assignment=assignment, attempt_number=number, opened_reason=reason,
            boq_submitted_at=timezone.now(), boq_submitted_by=assignment.assigned_to)
        assignment.current_attempt_number = number
        assignment.status = DESIGN_ARKA_SUBMITTED
        assignment.save()
        arka = ArkaSubmission.objects.create(
            attempt=attempt, version=1, capacity_kw=Decimal('120.00'),
            arka_link='https://example.com/arka', submitted_by=assignment.assigned_to,
            verdict=ARKA_APPROVED, head_verdict=ARKA_APPROVED, is_current=True)
        DesignFile.objects.create(
            attempt=attempt, kind=DESIGN_FILE_CAD_ZIP, version=1,
            bucket='b', path=f'{assignment.project.project_id}/cad/x.zip',
            original_filename='x.zip', size_bytes=10, content_type='application/zip',
            derived_from_arka=arka,
            uploaded_by=assignment.assigned_to, is_current=True)
        return attempt

    def _into_qc(self, attempt=None):
        """QC has started: the window a change request may be raised in."""
        attempt = attempt or self.attempt
        attempt.qc_started_at = timezone.now()
        attempt.save(update_fields=['qc_started_at'])
        self.a.status = DESIGN_IN_QC
        self.a.save(update_fields=['status'])
        return attempt

    def _awaiting_head(self, attempt=None):
        """Gate 1 passed, gate 2 outstanding."""
        attempt = attempt or self.attempt
        attempt.qc_started_at   = attempt.qc_started_at or timezone.now()
        attempt.qc_verdict      = QC_PASSED
        attempt.qc_reviewed_by  = self.qc
        attempt.qc_reviewed_at  = timezone.now()
        attempt.head_started_at = timezone.now()
        attempt.save()
        self.a.status = DESIGN_AWAITING_HEAD_QC
        self.a.save(update_fields=['status'])
        return attempt

    # ── helpers ─────────────────────────────────────────────────────────────

    def _login(self, profile):
        self.assertTrue(self.client.login(username=profile.user.username, password='x'))

    def _raise(self, reason='Client moved the array to the north shed.'):
        return self.client.post(
            reverse('design_change_request',
                    kwargs={'project_id': self.site.project_id}),
            {'reason': reason})

    def _accept(self, change, **data):
        return self.client.post(
            reverse('design_change_request_accept', kwargs={'pk': change.pk}), data)

    def _reject(self, change, **data):
        return self.client.post(
            reverse('design_change_request_reject', kwargs={'pk': change.pk}), data)

    def _the_request(self):
        return DesignChangeRequest.objects.get(attempt__assignment=self.a)


# ===========================================================================
# 1-2. Raising
# ===========================================================================

class RaisingTests(Part46Base):

    def test_01_raising_creates_a_pending_row_and_opens_nothing(self):
        """VERIFICATION 1 — pending verdict, NO new attempt, status unchanged."""
        self._into_qc()
        before = self.a.attempts.count()

        self._login(self.pm)
        response = self._raise()
        self.assertEqual(response.status_code, 302)

        after = self.a.attempts.count()
        self.a.refresh_from_db()
        change = self._the_request()

        self.assertEqual(change.verdict, CHANGE_REQUEST_PENDING)
        self.assertIsNone(change.resulting_attempt_id)
        self.assertIsNone(change.decided_by_id)
        self.assertIsNone(change.decided_at)
        self.assertEqual(before, 1)
        self.assertEqual(after, 1, 'raising a change request opened an attempt')
        self.assertEqual(self.a.status, DESIGN_IN_QC, 'the site moved on a raise')
        self.assertEqual(self.a.current_attempt_number, 1)

    def test_02_a_second_pending_request_is_refused_by_the_database(self):
        """VERIFICATION 2 — the CONSTRAINT refuses it, not just the view.

        The view is bypassed entirely: the row is created through the ORM, which is the
        only way to prove the rule holds against admin edits and imports too.
        """
        self._into_qc()
        self._login(self.pm)
        self._raise()

        with self.assertRaises(IntegrityError) as caught:
            with transaction.atomic():
                DesignChangeRequest.objects.create(
                    attempt=self.attempt, requested_by=self.pm,
                    reason='and another thing', verdict=CHANGE_REQUEST_PENDING)
        self.assertIn('uniq_pending_change_request_per_attempt', str(caught.exception))

        # And the view refuses it with a message rather than a 500.
        self._raise(reason='and another thing')
        self.assertEqual(
            DesignChangeRequest.objects.filter(attempt=self.attempt).count(), 1)

    def test_02b_a_decided_request_does_not_block_a_new_one(self):
        """The partial constraint is partial: reject, then raise again."""
        self._into_qc()
        self._login(self.pm)
        self._raise()
        change = self._the_request()

        self.client.logout()
        self._login(self.head)
        self._reject(change, rejection_reason='The north shed is out of scope.')

        self.client.logout()
        self._login(self.pm)
        self._raise(reason='second thoughts, and a different one')
        self.assertEqual(
            DesignChangeRequest.objects.filter(attempt=self.attempt).count(), 2)


# ===========================================================================
# 3-4. Suspension at both gates
# ===========================================================================

class SuspensionTests(Part46Base):

    def test_03_design_qc_cannot_record_a_verdict_while_a_request_is_pending(self):
        """VERIFICATION 3 — gate 1, by direct POST."""
        self._into_qc()
        self._login(self.pm)
        self._raise()
        self.client.logout()

        self._login(self.qc)
        self.client.post(reverse('design_qc_pass',
                                 kwargs={'project_id': self.site.project_id}))
        self.attempt.refresh_from_db()
        self.a.refresh_from_db()
        self.assertEqual(self.attempt.qc_verdict, QC_PENDING)
        self.assertEqual(self.a.status, DESIGN_IN_QC)

        # And the failing verdict is refused identically.
        self.client.post(
            reverse('design_qc_fail', kwargs={'project_id': self.site.project_id}),
            {'qc_remarks': 'wrong', 'error_category': ERR_LAYOUT,
             'redo_scope_submitted': '1', 'redo': ['arka', 'cad']})
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.qc_verdict, QC_PENDING)
        self.assertEqual(self.a.attempts.count(), 1)

    def test_04_the_head_cannot_record_a_verdict_while_a_request_is_pending(self):
        """VERIFICATION 4 — gate 2, by direct POST."""
        self._awaiting_head()
        self._login(self.pm)
        self._raise()
        self.client.logout()

        self._login(self.head)
        self.client.post(reverse('design_head_qc_pass',
                                 kwargs={'project_id': self.site.project_id}))
        self.attempt.refresh_from_db()
        self.a.refresh_from_db()
        self.assertEqual(self.attempt.head_verdict, QC_PENDING)
        self.assertEqual(self.a.status, DESIGN_AWAITING_HEAD_QC,
                         'the site was released over a pending change request')

        self.client.post(
            reverse('design_head_qc_fail', kwargs={'project_id': self.site.project_id}),
            {'head_remarks': 'wrong', 'error_category': ERR_LAYOUT,
             'redo_scope_submitted': '1', 'redo': ['arka', 'cad']})
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.head_verdict, QC_PENDING)
        self.assertEqual(self.a.attempts.count(), 1)


# ===========================================================================
# 5-6. Rejection
# ===========================================================================

class RejectionTests(Part46Base):

    def test_05a_a_rejection_with_no_reason_is_refused_by_the_database(self):
        """VERIFICATION 5, first half — the CHECK constraint, view bypassed."""
        self._into_qc()
        self._login(self.pm)
        self._raise()
        change = self._the_request()

        with self.assertRaises(IntegrityError) as caught:
            with transaction.atomic():
                change.verdict = CHANGE_REQUEST_REJECTED
                change.save(update_fields=['verdict'])
        self.assertIn('cr_rejection_reason_required_when_rejected',
                      str(caught.exception))

        # And the view refuses it too, before the database is asked.
        change.refresh_from_db()
        self.client.logout()
        self._login(self.head)
        self._reject(change, rejection_reason='   ')
        change.refresh_from_db()
        self.assertEqual(change.verdict, CHANGE_REQUEST_PENDING)

    def test_05b_a_rejection_with_a_reason_opens_nothing(self):
        """VERIFICATION 5, second half."""
        self._into_qc()
        self._login(self.pm)
        self._raise()
        change = self._the_request()
        self.client.logout()

        self._login(self.head)
        self._reject(change, rejection_reason='The north shed is outside the tender.')

        change.refresh_from_db()
        self.a.refresh_from_db()
        self.assertEqual(change.verdict, CHANGE_REQUEST_REJECTED)
        self.assertEqual(change.rejection_reason,
                         'The north shed is outside the tender.')
        self.assertEqual(change.decided_by_id, self.head.pk)
        self.assertIsNotNone(change.decided_at)
        self.assertIsNone(change.resulting_attempt_id)
        self.assertEqual(self.a.attempts.count(), 1, 'a rejection opened an attempt')
        self.assertEqual(self.a.status, DESIGN_IN_QC, 'a rejection moved the site')

    def test_06_the_suspended_review_resumes_after_a_rejection(self):
        """VERIFICATION 6 — the whole point of keying the guard off `verdict`."""
        self._into_qc()
        self._login(self.pm)
        self._raise()
        change = self._the_request()
        self.client.logout()

        self._login(self.head)
        self._reject(change, rejection_reason='The current layout stands.')
        self.client.logout()

        self._login(self.qc)
        self.client.post(reverse('design_qc_pass',
                                 kwargs={'project_id': self.site.project_id}))
        self.attempt.refresh_from_db()
        self.a.refresh_from_db()
        self.assertEqual(self.attempt.qc_verdict, QC_PASSED,
                         'the review is still suspended after a rejection')
        self.assertEqual(self.a.status, DESIGN_AWAITING_HEAD_QC)


# ===========================================================================
# 7-8. Acceptance, and what it must not corrupt
# ===========================================================================

class AcceptanceTests(Part46Base):

    def test_07_acceptance_opens_n_plus_1_and_judges_nothing(self):
        """VERIFICATION 7 — the closed attempt keeps BOTH verdicts at pending."""
        self._into_qc()
        self._login(self.pm)
        self._raise()
        change = self._the_request()
        self.client.logout()

        self._login(self.head)
        self._accept(change)

        change.refresh_from_db()
        self.a.refresh_from_db()
        old = self.a.attempts.get(attempt_number=1)
        new = self.a.attempts.get(attempt_number=2)

        self.assertEqual(change.verdict, CHANGE_REQUEST_ACCEPTED)
        self.assertEqual(change.decided_by_id, self.head.pk)
        self.assertIsNotNone(change.decided_at)
        self.assertEqual(change.resulting_attempt_id, new.pk)

        self.assertEqual(new.opened_reason, ATTEMPT_REASON_PM_CHANGE_REQUEST)
        self.assertEqual(self.a.current_attempt_number, 2)
        self.assertEqual(self.a.status, DESIGN_IN_DESIGN)

        # THE ASSERTION THIS TEST EXISTS FOR.
        self.assertEqual(old.qc_verdict, QC_PENDING,
                         'the interrupted attempt was marked judged at gate 1')
        self.assertEqual(old.head_verdict, QC_PENDING,
                         'the interrupted attempt was marked judged at gate 2')
        self.assertIsNotNone(old.closed_at)

        # The brief moved, so nothing is carried forward (Part 9.1's `redo=None` path).
        self.assertEqual(new.arka_submissions.count(), 0)
        self.assertEqual(new.design_files.count(), 0)
        self.assertIsNone(new.boq_submitted_at)

    def test_07b_a_request_cannot_be_triaged_twice(self):
        self._into_qc()
        self._login(self.pm)
        self._raise()
        change = self._the_request()
        self.client.logout()

        self._login(self.head)
        self._accept(change)
        self._accept(change)
        self.assertEqual(self.a.attempts.count(), 2,
                         'a double submit opened a second attempt')

    def test_08_an_accepted_request_counts_as_pm_change_not_designer_rework(self):
        """VERIFICATION 8 — the Part 5 / Part 9 rework split is untouched."""
        self._into_qc()
        self._login(self.pm)
        self._raise()
        change = self._the_request()
        self.client.logout()
        self._login(self.head)
        self._accept(change)

        # Release the site so the multipliers have a denominator.
        self.a.refresh_from_db()
        self.a.status = DESIGN_RELEASED
        self.a.released_at = timezone.now()
        self.a.save()

        metrics = tender_metrics(self.program)
        row = next(w for w in metrics['workload'] if w['designer'].pk == self.designer.pk)
        self.assertEqual(row['pm_change_request'], 1)
        self.assertEqual(row['qc_failed'], 0)
        self.assertEqual(row['designer_error_attempts'], 0,
                         'a PM change request was charged to the designer')
        self.assertEqual(row['uncategorised_attempts'], 0)


# ===========================================================================
# 9. Who may triage
# ===========================================================================

class TriagePermissionTests(Part46Base):

    def setUp(self):
        super().setUp()
        self._into_qc()
        self._login(self.pm)
        self._raise()
        self.change = self._the_request()
        self.client.logout()

    def test_09_every_other_role_is_refused_both_actions_by_direct_post(self):
        """VERIFICATION 9 — PM, designer, Design QC, SCM and Site Engineer."""
        for profile in (self.pm, self.designer, self.qc, self.scm, self.se):
            with self.subTest(role=profile.user.username):
                self._login(profile)
                accept = self._accept(self.change)
                reject = self._reject(self.change, rejection_reason='no')
                self.assertEqual(accept.status_code, 403)
                self.assertEqual(reject.status_code, 403)
                self.client.logout()

        self.change.refresh_from_db()
        self.assertEqual(self.change.verdict, CHANGE_REQUEST_PENDING)
        self.assertEqual(self.a.attempts.count(), 1)

    def test_09b_the_named_deputy_succeeds(self):
        """VERIFICATION 9 — the Part 4 head-authority helper, deputy included."""
        self._login(self.deputy)
        self._accept(self.change)

        self.change.refresh_from_db()
        self.assertEqual(self.change.verdict, CHANGE_REQUEST_ACCEPTED)
        self.assertEqual(self.change.decided_by_id, self.deputy.pk)
        self.assertEqual(self.a.attempts.count(), 2)


# ===========================================================================
# 10-11. What each audience sees
# ===========================================================================

class VisibilityTests(Part46Base):

    def test_10_the_pending_queue_is_on_the_heads_dashboard_and_clears_on_triage(self):
        """VERIFICATION 10 — with the right age, and gone once decided."""
        self._into_qc()
        self._login(self.pm)
        self._raise()
        change = self._the_request()
        # Age the request two days so the column is proved, not just present.
        DesignChangeRequest.objects.filter(pk=change.pk).update(
            requested_at=timezone.now() - timezone.timedelta(days=2))
        self.client.logout()

        self._login(self.head)
        url = reverse('design_tender_dashboard', kwargs={'pk': self.program.pk})
        response = self.client.get(url)
        queue = response.context['m']['change_requests']
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]['project'].pk, self.site.pk)
        self.assertEqual(queue[0]['requested_by'].pk, self.pm.pk)
        self.assertEqual(queue[0]['attempt_number'], 1)
        self.assertEqual(queue[0]['age_days'], 2)

        # And it is on the attention list, which is the only other thing that shows it.
        reasons = ' '.join(r['reason'] for r in response.context['m']['attention'])
        self.assertIn('change request awaiting your decision', reasons)

        self._reject(change, rejection_reason='The current layout stands.')
        response = self.client.get(url)
        self.assertEqual(response.context['m']['change_requests'], [])
        reasons = ' '.join(r['reason'] for r in response.context['m']['attention'])
        self.assertNotIn('change request awaiting your decision', reasons)

    def test_10b_design_qc_does_not_see_the_triage_queue(self):
        """Triage is the Head's. The key is ABSENT from QC's context, not hidden by an if."""
        self._into_qc()
        self._login(self.pm)
        self._raise()
        self.client.logout()

        self._login(self.qc)
        response = self.client.get(
            reverse('design_qc_dashboard', kwargs={'pk': self.program.pk}))
        self.assertNotIn('change_requests', response.context['m'])
        reasons = ' '.join(r['reason'] for r in response.context['m']['attention'])
        self.assertNotIn('change request awaiting your decision', reasons)

    def test_11_the_pm_sees_the_rejection_reason_on_their_own_request(self):
        """VERIFICATION 11."""
        self._into_qc()
        self._login(self.pm)
        self._raise()
        change = self._the_request()
        self.client.logout()

        self._login(self.head)
        self._reject(change, rejection_reason='The north shed is outside the tender.')
        self.client.logout()

        self._login(self.pm)
        response = self.client.get(
            reverse('design_change_request_form',
                    kwargs={'project_id': self.site.project_id}))
        body = response.content.decode()
        self.assertIn('The north shed is outside the tender.', body)
        self.assertIn('Rejected', body)

    def test_11b_the_designer_sees_all_three_states_on_their_own_workspace(self):
        """Part 4.6 §6 — pending is a suspension notice, rejected is not a task."""
        self._into_qc()
        self._login(self.pm)
        self._raise()
        change = self._the_request()
        self.client.logout()

        self._login(self.designer)
        url = reverse('design_site_workspace',
                      kwargs={'project_id': self.site.project_id})
        self.assertIn('Awaiting the Design Head', self.client.get(url).content.decode())
        self.client.logout()

        self._login(self.head)
        self._reject(change, rejection_reason='The current layout stands.')
        self.client.logout()

        self._login(self.designer)
        body = self.client.get(url).content.decode()
        self.assertIn('No action needed', body)
        self.assertIn('The current layout stands.', body)


# ===========================================================================
# 12. Part 6 — the group lock
# ===========================================================================

class GroupLockTests(Part46Base):
    """A released, grouped site: the one shape in which a change request and a draft group
    coexist. Raising pulls the site out of the group (Part 6 §4, unchanged by 4.6), so the
    lock guard is exercised on a SECOND site that stays in."""

    def setUp(self):
        super().setUp()
        self.site2, self.a2 = self._site('P46-B')
        self.attempt2 = self._package(self.a2)
        for assignment in (self.a, self.a2):
            assignment.status = DESIGN_RELEASED
            assignment.released_at = timezone.now()
            assignment.save()
        self.attempt.qc_started_at = timezone.now()
        self.attempt.save(update_fields=['qc_started_at'])
        self.attempt2.qc_started_at = timezone.now()
        self.attempt2.save(update_fields=['qc_started_at'])

        self.group = SiteGroup.objects.create(
            program=self.program, name='Batch 1', status=SITE_GROUP_DRAFT,
            created_by=self.scm)
        SiteGroupMembership.objects.create(
            group=self.group, project=self.site2, added_by=self.scm)

    def test_12_a_lock_is_refused_while_pending_and_permitted_once_rejected(self):
        """VERIFICATION 12."""
        change = DesignChangeRequest.objects.create(
            attempt=self.attempt2, requested_by=self.pm,
            reason='the client wants a different inverter',
            verdict=CHANGE_REQUEST_PENDING)

        self._login(self.scm)
        self.client.post(reverse('site_group_lock', kwargs={'pk': self.group.pk}))
        self.group.refresh_from_db()
        self.assertEqual(self.group.status, SITE_GROUP_DRAFT,
                         'a group locked over a pending change request')
        self.client.logout()

        self._login(self.head)
        self._reject(change, rejection_reason='The specified inverter stands.')
        self.client.logout()

        self._login(self.scm)
        self.client.post(reverse('site_group_lock', kwargs={'pk': self.group.pk}))
        self.group.refresh_from_db()
        self.assertEqual(self.group.status, SITE_GROUP_LOCKED,
                         'a rejected change request still blocks the lock')


# ===========================================================================
# 14. Blast radius
# ===========================================================================

class BlastRadiusTests(Part46Base):

    def test_14_triage_touches_no_residential_row_no_task_and_no_notification(self):
        """VERIFICATION 14."""
        from django.forms.models import model_to_dict
        from .models import Task, BOQ, BOQItem

        residential = Project(
            project_id='RES-46', customer_name='House', customer_phone='9876543211',
            site_address='2 Sun Rd', city='Delhi', project_type='Residential',
            capacity_kw=Decimal('10.00'), status='Draft', assigned_pm=self.pm)
        residential.save()

        # Project carries no modified stamp, so the whole row is snapshotted rather than
        # one column — which is the stronger assertion anyway.
        before = {
            'residential_row': model_to_dict(Project.objects.get(pk=residential.pk)),
            'tasks':        Task.objects.count(),
            'boqs':         BOQ.objects.count(),
            'boq_items':    BOQItem.objects.count(),
            'notifications': NotificationLog.objects.count(),
        }

        self._into_qc()
        self._login(self.pm)
        self._raise()
        change = self._the_request()
        self.client.logout()
        self._login(self.head)
        self._accept(change)

        self.assertEqual(model_to_dict(Project.objects.get(pk=residential.pk)),
                         before['residential_row'])
        self.assertEqual(Task.objects.count(), before['tasks'])
        self.assertEqual(BOQ.objects.count(), before['boqs'])
        self.assertEqual(BOQItem.objects.count(), before['boq_items'])
        self.assertEqual(NotificationLog.objects.count(), before['notifications'])
