"""
W-5a — Design QC and the Design Head may revise the Arka during package review.

WHY THIS FILE EXISTS
--------------------
Zuber (20 Nov 2026): QC and the Head can put in a new Arka when they think changes are
needed, as they can already correct the BOQ (BC-1). Rulings pinned here:

  * one window for BOQ correction and the revision — BC-1's boq_correction_closed_reason(),
    as it stands, minus its Head change-request exception (Q5, P2);
  * the reviewer may change the link, the capacity or both, with a mandatory reason
    (ruling 7); no status change; the current CAD stays current (ruling 8);
  * the new version keeps the QC approval of the version it replaced (option A), and is
    shown everywhere as "QC approval by Y kept; revised by X (role): reason" (Q1);
  * a Head revision is final (F2) — it does not trip W-1's own-Arka-QC refusal — while a
    Head who gave the ORIGINAL QC verdict is still refused;
  * the record: reviewer, role ('head' wins for someone holding both, Q6), reason and the
    designer at revision, frozen against reallocation; carried copies keep all three;
  * a revision is not designer rework: no attempt, no BOQCorrection, and the analytics
    leave it out of Arka iterations, stage dwell and queue latency (Q3);
  * the pairing line on the review screen, the reworded yellow CAD mark and BOQ badge (Q7),
    and the W-7 carried-BOQ wording fix.

Every step goes through the real views by POST except the fixtures that put a package in
front of a gate (status and verdict columns written directly, as tests_design_redo_scope
does). PDF extraction is mocked wherever a refusal is asserted to call nothing.
"""
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .design_analytics import (
    analytics_dataset, m_arka_iterations, m_queue_latency, m_stage_dwell,
)
from .design_metrics import arka_is_approved
from .design_views import user_may_revise_design
from .models import (
    ActivityLog, ArkaSubmission, BOQ, BOQCorrection, BOQItem, DesignAssignment,
    DesignAttempt, DesignChangeRequest, DesignFile, Program, Project, SiteGroup,
    SiteGroupMembership,
    ARKA_APPROVED, ATTEMPT_REASON_INITIAL, CHANGE_REQUEST_ORIGIN_PM, CHANGE_REQUEST_PENDING,
    DESIGN_ARKA_SUBMITTED, DESIGN_ARTIFACTS_UPLOADED, DESIGN_AWAITING_HEAD_QC,
    DESIGN_AWAITING_PM_APPROVAL, DESIGN_FILE_CAD_ZIP, DESIGN_IN_DESIGN, DESIGN_IN_QC,
    DESIGN_PM_REJECTED, DESIGN_RELEASED, DESIGN_SURVEY_RETURNED, ERR_LAYOUT, QC_PASSED,
    REVIEWER_ROLE_HEAD, REVIEWER_ROLE_QC, SITE_GROUP_LOCKED,
)
from .permissions import user_can_correct_boq

ACTION_CODE = 'design_arka_reviewer_revised'
EXTRACT = 'projects.design_views.extract_cad_pdfs'


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


class RevisionBase(TestCase):
    """Attempt 1 in `in_qc`: Arka v1 (120 kW) approved by QC, a current CAD zip drawn from
    it, and the BOQ stamped AFTER the Arka (so the BOQ provenance is v1). The QC reviewer
    and the Head are different people."""

    def setUp(self):
        self.qc       = _profile('qc5',   'Design', is_design_qc=True)
        self.head     = _profile('head5', 'Design', is_design_head=True)
        self.designer = _profile('des5',  'Design')
        self.other_designer = _profile('des5b', 'Design')
        self.pm       = _profile('pm5',   'PM')

        self.program = Program.objects.create(
            name='Test-W5', program_type='OPEX', client_name='W5Client',
            status='Active', short_tender_code='W5')
        self.site = Project(
            project_id='W5-REV', customer_name='W5Client', customer_phone='9876543210',
            site_address='1 Sun Rd', city='Delhi', project_type='OPEX', program=self.program,
            site_code='W5-REV', dc_capacity_kw=Decimal('100.00'), status='Draft',
            assigned_pm=self.pm)
        self.site.save()
        self.a = DesignAssignment.objects.create(
            project=self.site, status=DESIGN_IN_QC, assigned_to=self.designer,
            survey_file_bucket='b', survey_file_path='W5-REV/survey/x.pdf',
            current_attempt_number=1)
        now = timezone.now()
        self.attempt = DesignAttempt.objects.create(
            assignment=self.a, attempt_number=1, opened_reason=ATTEMPT_REASON_INITIAL,
            qc_started_at=now)
        self.arka = ArkaSubmission.objects.create(
            attempt=self.attempt, version=1, capacity_kw=Decimal('120.00'),
            arka_link='https://example.com/arka1', submitted_by=self.designer,
            verdict=ARKA_APPROVED, reviewed_by=self.qc, reviewed_at=now, is_current=True)
        self.cad = DesignFile.objects.create(
            attempt=self.attempt, kind=DESIGN_FILE_CAD_ZIP, version=1,
            bucket='b', path='W5-REV/cad_zip/v1.zip', original_filename='W5.zip',
            derived_from_arka=self.arka, uploaded_by=self.designer, is_current=True)
        # Stamped after the Arka exists, so _boq_provenance() names v1.
        self.attempt.boq_submitted_at = timezone.now()
        self.attempt.boq_submitted_by = self.designer
        self.attempt.save()
        boq = BOQ.objects.create(project=self.site)
        BOQItem.objects.create(boq=boq, serial_no=1, description='Module',
                               uom='Nos', boq_quantity=10)

    # ── fixtures ────────────────────────────────────────────────────────────

    def _set_status(self, status):
        extra = {}
        if status == DESIGN_RELEASED:
            # A real release stamps both (design_pm_approve); the review screen names them.
            extra = {'released_at': timezone.now(), 'released_by': self.pm}
        DesignAssignment.objects.filter(pk=self.a.pk).update(status=status, **extra)
        self.a.refresh_from_db()

    def _at_head(self):
        """Design QC passed attempt 1; the package waits for the Head."""
        now = timezone.now()
        self.attempt.qc_verdict = QC_PASSED
        self.attempt.qc_reviewed_by = self.qc
        self.attempt.qc_reviewed_at = now
        self.attempt.head_started_at = now
        self.attempt.save()
        self._set_status(DESIGN_AWAITING_HEAD_QC)

    def _pending_change_request(self):
        return DesignChangeRequest.objects.create(
            attempt=self.attempt, requested_by=self.pm, reason='One pit short.',
            verdict=CHANGE_REQUEST_PENDING, origin=CHANGE_REQUEST_ORIGIN_PM)

    # ── real views ──────────────────────────────────────────────────────────

    def _login(self, profile):
        self.client.logout()
        self.assertTrue(self.client.login(username=profile.user.username, password='x'))

    def _post(self, name, **data):
        return self.client.post(
            reverse(name, kwargs={'project_id': self.site.project_id}), data)

    def _revise(self, who, capacity='118.00', link=None, reason='Two rows clash with the tank'):
        self._login(who)
        return self._post('design_arka_revise', capacity_kw=capacity,
                          arka_link=link or self.arka.arka_link, revision_reason=reason)

    def _current_arka(self, attempt=None):
        return (attempt or self.attempt).arka_submissions.get(is_current=True)

    def _page(self, who, name='design_qc_review'):
        self._login(who)
        response = self.client.get(reverse(name, kwargs={'project_id': self.site.project_id}))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def _assert_nothing_written(self):
        self.assertEqual(ArkaSubmission.objects.filter(attempt=self.attempt).count(), 1)
        self.assertEqual(self._current_arka().pk, self.arka.pk)
        self.assertFalse(ActivityLog.objects.filter(action_code=ACTION_CODE).exists())


# ===========================================================================
# The window
# ===========================================================================

class WindowTests(RevisionBase):

    def test_qc_and_the_head_may_revise_in_each_window_status(self):
        # artifacts_uploaded left out: ruling 5 (W-5b) opens revisions at in_qc.
        for status in (DESIGN_IN_QC, DESIGN_AWAITING_HEAD_QC):
            for who in (self.qc, self.head):
                with self.subTest(status=status, who=who.user.username):
                    self._set_status(status)
                    before = self._current_arka()
                    self._revise(who, capacity=str(before.capacity_kw - 1))
                    after = self._current_arka()
                    self.assertEqual(after.version, before.version + 1)
                    self.a.refresh_from_db()
                    self.assertEqual(self.a.status, status)

    @mock.patch(EXTRACT)
    def test_every_other_status_is_refused_and_writes_nothing(self, extract):
        for status in (DESIGN_IN_DESIGN, DESIGN_ARKA_SUBMITTED, DESIGN_AWAITING_PM_APPROVAL,
                       DESIGN_RELEASED, DESIGN_PM_REJECTED, DESIGN_SURVEY_RETURNED):
            for who in (self.qc, self.head):
                with self.subTest(status=status, who=who.user.username):
                    self._set_status(status)
                    response = self._revise(who)
                    self.assertEqual(response.status_code, 302)
                    self._assert_nothing_written()
                    self.a.refresh_from_db()
                    self.assertEqual(self.a.status, status)
        extract.assert_not_called()

    @mock.patch(EXTRACT)
    def test_the_designer_the_pm_and_an_outsider_are_forbidden(self, extract):
        self.designer.is_design_qc = True   # the flag does not let a designer review their own site
        self.designer.save()
        for who in (self.designer, self.pm, self.other_designer):
            with self.subTest(who=who.user.username):
                self.assertEqual(self._revise(who).status_code, 403)
                self._assert_nothing_written()
        extract.assert_not_called()

    @mock.patch(EXTRACT)
    def test_a_locked_procurement_group_refuses(self, extract):
        group = SiteGroup.objects.create(
            program=self.program, name='Batch W5', status=SITE_GROUP_LOCKED,
            created_by=self.pm, locked_by=self.pm, locked_at=timezone.now())
        SiteGroupMembership.objects.create(group=group, project=self.site, added_by=self.pm)
        self._revise(self.head)
        self._assert_nothing_written()
        extract.assert_not_called()

    @mock.patch(EXTRACT)
    def test_a_closed_attempt_is_refused(self, extract):
        self.attempt.closed_at = timezone.now()
        self.attempt.save()
        self._revise(self.qc)
        self._assert_nothing_written()
        extract.assert_not_called()

    def test_the_heads_change_request_exception_is_not_extended(self):
        """BC-1 lets the Head correct the BOQ while a change request is pending (D-7). The
        revision refuses: the exception is the one branch the extra term removes."""
        self._pending_change_request()
        self.assertTrue(user_can_correct_boq(self.head.user, self.site))   # BC-1 unchanged
        self._revise(self.head)
        self._assert_nothing_written()

    def test_a_stale_tab_after_release_is_refused(self):
        """The window is checked on the POST, under the row lock, not only on the form."""
        self.assertTrue(user_may_revise_design(self.qc.user, self.site))
        self._set_status(DESIGN_RELEASED)
        self._revise(self.qc)
        self._assert_nothing_written()


# ===========================================================================
# The Arka revision
# ===========================================================================

class ArkaRevisionTests(RevisionBase):

    def test_link_only_capacity_only_and_both_are_accepted(self):
        cases = (('120.00', 'https://example.com/arka-b'),
                 ('117.50', 'https://example.com/arka-b'),
                 ('116.00', 'https://example.com/arka-c'))
        for capacity, link in cases:
            with self.subTest(capacity=capacity, link=link):
                before = self._current_arka()
                self._revise(self.qc, capacity=capacity, link=link)
                after = self._current_arka()
                self.assertEqual(after.version, before.version + 1)
                self.assertEqual((after.capacity_kw, after.arka_link),
                                 (Decimal(capacity), link))

    def test_no_change_at_all_is_refused(self):
        self._revise(self.qc, capacity='120.00', link=self.arka.arka_link)
        self._assert_nothing_written()
        # Trailing zeros are the same number, not a change.
        self._revise(self.qc, capacity='120.000', link=self.arka.arka_link)
        self._assert_nothing_written()

    def test_a_blank_reason_is_refused(self):
        self._revise(self.qc, reason='   ')
        self._assert_nothing_written()

    def test_an_overlong_reason_and_a_third_decimal_are_refused(self):
        self._revise(self.qc, reason='x' * 1001)
        self._assert_nothing_written()
        self._revise(self.qc, capacity='118.005')
        self._assert_nothing_written()

    def test_the_shared_field_check_refuses_nan_and_infinity_on_both_paths(self):
        """_read_arka_fields() is shared with the designer's submission. Before W-5a 'NaN'
        was a 500 there (Decimal NaN comparison raises) and 'Infinity' reached the
        database; both are now the ordinary "as a number" message."""
        for raw in ('NaN', 'Infinity'):
            with self.subTest(path='reviewer', raw=raw):
                self._revise(self.qc, capacity=raw)
                self._assert_nothing_written()
            with self.subTest(path='designer', raw=raw):
                self._set_status(DESIGN_IN_DESIGN)
                self._login(self.designer)
                response = self._post('design_arka_submit', capacity_kw=raw,
                                      arka_link='https://example.com/arka9')
                self.assertEqual(response.status_code, 302)
                self.assertEqual(ArkaSubmission.objects.filter(attempt=self.attempt).count(), 1)
                self._set_status(DESIGN_IN_QC)

    def test_the_new_version_is_current_approved_and_the_status_and_cad_stand(self):
        self._revise(self.head)
        new = self._current_arka()
        self.arka.refresh_from_db()
        self.assertFalse(self.arka.is_current)          # the designer's version is history
        self.assertTrue(arka_is_approved(new))
        self.a.refresh_from_db()
        self.assertEqual(self.a.status, DESIGN_IN_QC)
        self.cad.refresh_from_db()
        self.assertTrue(self.cad.is_current)            # ruling 8
        self.assertEqual(self.cad.derived_from_arka_id, self.arka.pk)

    def test_the_qc_approval_is_kept_with_its_author_not_the_reviser(self):
        self._revise(self.head)
        new = self._current_arka()
        self.assertEqual((new.verdict, new.reviewed_by_id, new.reviewed_at),
                         (ARKA_APPROVED, self.qc.pk, self.arka.reviewed_at))
        self.assertEqual(new.head_verdict, 'pending')    # never a Head verdict
        self.assertIsNone(new.head_reviewed_by_id)
        # A second revision still names the person who really approved the layout.
        self._revise(self.qc, capacity='110.00', reason='Tank moved again')
        self.assertEqual(self._current_arka().reviewed_by_id, self.qc.pk)

    def test_one_activity_entry_names_the_change_the_reason_and_the_kept_approval(self):
        self._revise(self.head, capacity='118.00', reason='Two rows clash with the tank')
        entries = ActivityLog.objects.filter(action_code=ACTION_CODE)
        self.assertEqual(entries.count(), 1)
        text = entries.get().action
        self.assertIn('head5 (Head) revised the Arka (v1 → v2)', text)
        self.assertIn('capacity: 120.00 → 118.00 kW', text)
        self.assertNotIn('link:', text)                  # unchanged fields are omitted
        self.assertIn('QC approval by qc5 kept; revised by head5 (Head): '
                      'Two rows clash with the tank', text)


# ===========================================================================
# W-1's two-people rule
# ===========================================================================

class TwoPeopleRuleTests(RevisionBase):

    def test_a_head_who_revised_the_arka_can_still_give_the_final_approval(self):
        self._at_head()
        self._revise(self.head)
        self._login(self.head)
        self._post('design_head_qc_pass')
        self.a.refresh_from_db()
        self.assertEqual(self.a.status, DESIGN_AWAITING_PM_APPROVAL)

    def test_a_head_who_gave_the_original_arka_qc_verdict_is_still_refused(self):
        self.head.is_design_qc = True
        self.head.save()
        self.arka.reviewed_by = self.head
        self.arka.save()
        self._at_head()
        self._revise(self.head)
        self.assertEqual(self._current_arka().version, 2)
        self._login(self.head)
        self._post('design_head_qc_pass')
        self.a.refresh_from_db()
        self.assertEqual(self.a.status, DESIGN_AWAITING_HEAD_QC)

    def test_a_qc_revision_then_the_qc_pass_then_the_heads_final_approval(self):
        self._revise(self.qc)
        self._login(self.qc)
        self._post('design_qc_pass')
        self.a.refresh_from_db()
        self.assertEqual(self.a.status, DESIGN_AWAITING_HEAD_QC)
        self._login(self.head)
        self._post('design_head_qc_pass')
        self.a.refresh_from_db()
        self.assertEqual(self.a.status, DESIGN_AWAITING_PM_APPROVAL)


# ===========================================================================
# The record
# ===========================================================================

class RecordTests(RevisionBase):

    def test_each_revision_stores_reviewer_role_reason_and_designer(self):
        for who, role in ((self.qc, REVIEWER_ROLE_QC), (self.head, REVIEWER_ROLE_HEAD)):
            with self.subTest(role=role):
                before = self._current_arka()
                self._revise(who, capacity=str(before.capacity_kw - 1), reason=f'by {role}')
                new = self._current_arka()
                self.assertEqual((new.submitted_by_id, new.reviewer_role, new.revision_reason,
                                  new.designer_at_revision_id),
                                 (who.pk, role, f'by {role}', self.designer.pk))

    def test_someone_with_both_authorities_is_recorded_as_the_head(self):
        self.head.is_design_qc = True
        self.head.save()
        self._revise(self.head)
        self.assertEqual(self._current_arka().reviewer_role, REVIEWER_ROLE_HEAD)

    def test_the_stored_designer_survives_a_reallocation(self):
        self._revise(self.qc)
        DesignAssignment.objects.filter(pk=self.a.pk).update(assigned_to=self.other_designer)
        self.assertEqual(self._current_arka().designer_at_revision_id, self.designer.pk)

    def test_the_database_refuses_a_revision_without_a_reason(self):
        from django.db import IntegrityError, transaction
        with self.assertRaises(IntegrityError), transaction.atomic():
            ArkaSubmission.objects.create(
                attempt=self.attempt, version=9, capacity_kw=Decimal('1'),
                arka_link='https://example.com/x', submitted_by=self.qc,
                reviewer_role=REVIEWER_ROLE_QC, revision_reason='', is_current=False)


# ===========================================================================
# Carry-forward
# ===========================================================================

class CarryTests(RevisionBase):

    def test_a_fail_carries_the_revised_arka_with_its_record(self):
        self._at_head()
        self._revise(self.head, reason='Tank clearance')
        revision = self._current_arka()
        self._login(self.head)
        self._post('design_head_qc_fail', head_remarks='BOQ is short',
                   error_category=ERR_LAYOUT, redo_scope_submitted='1', redo=['boq'])
        self.a.refresh_from_db()
        new_attempt = self.a.attempts.get(attempt_number=2)
        copy = self._current_arka(new_attempt)
        self.assertEqual(copy.carried_forward_from_id, revision.pk)
        self.assertEqual((copy.reviewer_role, copy.revision_reason,
                          copy.designer_at_revision_id, copy.reviewed_by_id),
                         (REVIEWER_ROLE_HEAD, 'Tank clearance', self.designer.pk, self.qc.pk))
        # A carried approved Arka opens the attempt at `arka_submitted` (W-1 rule).
        self.assertEqual(self.a.status, DESIGN_ARKA_SUBMITTED)
        # W-6's count sees one revision, not one per attempt it rides.
        self.assertEqual(ArkaSubmission.objects.exclude(reviewer_role='')
                         .filter(carried_forward_from__isnull=True).count(), 1)

    def test_a_carried_cad_keeps_its_reviewer_fields(self):
        """W-5b writes reviewer CADs; the copy call carries the fields from W-5a on."""
        DesignFile.objects.filter(pk=self.cad.pk).update(
            reviewer_role=REVIEWER_ROLE_QC, revision_reason='Fixed the legend',
            designer_at_revision=self.designer)
        self._at_head()
        self._login(self.head)
        self._post('design_head_qc_fail', head_remarks='BOQ is short',
                   error_category=ERR_LAYOUT, redo_scope_submitted='1', redo=['boq'])
        new_attempt = self.a.attempts.get(attempt_number=2)
        copy = new_attempt.design_files.get(kind=DESIGN_FILE_CAD_ZIP, is_current=True)
        self.assertEqual((copy.reviewer_role, copy.revision_reason,
                          copy.designer_at_revision_id),
                         (REVIEWER_ROLE_QC, 'Fixed the legend', self.designer.pk))


# ===========================================================================
# Not designer rework (and Q3 — the analytics)
# ===========================================================================

class NoReworkTests(RevisionBase):

    def _figures(self):
        data = analytics_dataset([self.program])
        iterations = {r['label']: r['figure'] for r in m_arka_iterations(data)['rows']}
        dwell = {r['label']: r['spread']['n'] for r in m_stage_dwell(data)['rows']}
        latency = {r['label']: r['spread']['n'] for r in m_queue_latency(data)['rows']}
        return iterations, dwell, latency

    def test_no_attempt_no_boq_correction(self):
        self._revise(self.head)
        self.assertEqual(self.a.attempts.count(), 1)
        self.assertEqual(BOQCorrection.objects.count(), 0)

    def test_arka_iterations_do_not_count_a_revision(self):
        before, _, _ = self._figures()
        self._revise(self.head)
        after, _, _ = self._figures()
        self.assertEqual(after, before)

    def test_stage_dwell_takes_no_sample_from_a_revision(self):
        label = 'Arka submitted → Design QC verdict'
        _, before, _ = self._figures()
        self._revise(self.head)
        _, after, _ = self._figures()
        self.assertEqual(after[label], before[label])

    def test_queue_latency_takes_no_sample_from_a_revision(self):
        label = 'Arka — waiting for a Design QC verdict'
        _, _, before = self._figures()
        self._revise(self.head)
        _, _, after = self._figures()
        self.assertEqual(after[label], before[label])

    def test_a_designer_version_still_counts(self):
        """Sanity: the exclusion is the reviewer's version only, not every new version."""
        before, _, _ = self._figures()
        ArkaSubmission.objects.filter(pk=self.arka.pk).update(is_current=False)
        ArkaSubmission.objects.create(
            attempt=self.attempt, version=2, capacity_kw=Decimal('118'),
            arka_link='https://example.com/a2', submitted_by=self.designer,
            verdict=ARKA_APPROVED, reviewed_by=self.qc, reviewed_at=timezone.now())
        after, _, _ = self._figures()
        self.assertNotEqual(after, before)


# ===========================================================================
# Displays
# ===========================================================================

class DisplayTests(RevisionBase):

    PAIRING = 'CAD drawn from Arka v1 · attempt 1;'

    def test_pairing_line_on_the_qc_and_head_screens_after_a_revision(self):
        self._revise(self.qc, reason='Tank clearance')
        html = self._page(self.qc)
        self.assertIn(self.PAIRING, html)
        self.assertIn('current Arka is v2', html)
        self.assertIn('(revised by qc5, QC: Tank clearance)', html)
        self._at_head()
        self.assertIn(self.PAIRING, self._page(self.head))

    def test_no_pairing_line_when_the_cad_arka_is_current(self):
        self.assertNotIn('CAD drawn from Arka', self._page(self.qc))

    def test_pairing_line_for_a_w7_carried_cad(self):
        self._at_head()
        self._login(self.head)
        self._post('design_head_qc_fail', head_remarks='layout is wrong',
                   error_category=ERR_LAYOUT, redo_scope_submitted='1', redo=['arka'])
        self._login(self.designer)
        self._post('design_arka_submit', capacity_kw='118.00',
                   arka_link='https://example.com/arka2')
        self._login(self.qc)
        self._post('design_arka_approve')
        self.a.refresh_from_db()
        self.assertEqual(self.a.status, DESIGN_ARTIFACTS_UPLOADED)
        html = self._page(self.qc)
        self.assertIn(self.PAIRING, html)
        self.assertIn('current Arka is v1', html)
        self.assertNotIn('(revised by', html)

    def test_the_yellow_cad_mark_reads_as_the_pairing_line_after_a_revision(self):
        self._revise(self.head, reason='Tank clearance')
        html = self._page(self.qc)
        self.assertIn('drawn from Arka v1; current Arka is v2', html)
        self.assertNotIn('not the current Arka', html)

    def test_the_revision_form_shows_only_in_the_window_to_the_reviewers(self):
        url = reverse('design_arka_revise', kwargs={'project_id': self.site.project_id})
        self.assertIn(url, self._page(self.qc))
        self._at_head()
        self.assertIn(url, self._page(self.head))
        for status in (DESIGN_RELEASED, DESIGN_IN_DESIGN):
            with self.subTest(status=status):
                self._set_status(status)
                self.assertNotIn(url, self._page(self.qc))
                self.assertNotIn(url, self._page(self.head))
        self._set_status(DESIGN_IN_QC)
        self.assertNotIn(url, self._page(self.designer, 'design_site_workspace'))

    def test_the_qc_approval_of_a_revised_version_is_never_shown_plain(self):
        """Ruling Q1: the chip, the review screens and the Arka screen all say the approval
        was kept and who revised it; none shows only "approved by qc5" for v2."""
        self._revise(self.head, reason='Tank clearance')
        sentence = 'QC approval by qc5 kept; revised by head5 (Head): Tank clearance'
        qc_html = self._page(self.qc)
        self.assertIn(sentence, qc_html)
        self.assertNotIn('approved by Design QC', qc_html)       # the package card
        # The versions panel: one plain "QC approved" chip (v1, the designer's) and one
        # plain "QC <date> by" line (v1); v2 has neither.
        self.assertEqual(qc_html.count('QC approved</span>'), 1)
        self.assertIn('revised by head5 (Head)', qc_html)
        arka_html = self._page(self.qc, 'design_head_review')
        self.assertIn(sentence, arka_html)
        self.assertNotIn('approved by Design QC', arka_html)

    def test_the_boq_badge_names_the_reviewer_revision(self):
        self._revise(self.head, reason='Capacity cut')
        html = self._page(self.qc)
        self.assertIn('BOQ completed under Arka v1; Arka revised to v2 by head5 (Head): '
                      'Capacity cut', html)
        self.assertNotIn('not the current version', html)

    def test_a_carried_boq_stamp_says_where_it_was_completed(self):
        """The W-7 TODO: a stamp carried by a fail used to read "completed before any Arka
        was submitted"."""
        self._at_head()
        self._login(self.head)
        self._post('design_head_qc_fail', head_remarks='layout is wrong',
                   error_category=ERR_LAYOUT, redo_scope_submitted='1', redo=['arka'])
        self._login(self.designer)
        self._post('design_arka_submit', capacity_kw='118.00',
                   arka_link='https://example.com/arka2')
        html = self._page(self.designer, 'design_site_workspace')
        self.assertIn('completed on attempt 1 and carried forward, before this attempt', html)
        self.assertNotIn('completed before any Arka was submitted', html)

    def test_a_boq_completed_before_any_arka_keeps_its_wording(self):
        self.attempt.boq_submitted_at = self.arka.submitted_at - timedelta(hours=1)
        self.attempt.save()
        html = self._page(self.qc)
        self.assertIn('completed before any Arka was submitted', html)
        self.assertNotIn('carried forward, before this attempt', html)
