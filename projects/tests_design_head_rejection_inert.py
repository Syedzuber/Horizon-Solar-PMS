"""The Design Head's two answers to a PM rejection exist — and NOTHING CAN REACH THEM.

WHY THIS FILE EXISTS
--------------------
The fourth session of this shape, after 3.1a (the PM-approval status), 3.1b-1 (the PM's
approval surface) and the pm_rejected schema prompt. A PM who rejects a package sends it
back to the Design Head, who either OVERRULES the PM (return to the PM, unchanged) or
AGREES (send back to the designer, classified). Both are built here, with every screen
that shows them, while nothing can write `pm_rejected`. Prompt 3.1b-2c flips the PM's
reject onto that status and everything below becomes reachable in one commit.

  (a) THE INERTNESS CHAIN, both halves: nothing writes pm_rejected (the grep), and the only
      writer of awaiting_pm_approval refuses every other status (a POST per status)
  (b) return to PM: the status, one ledger row, and nothing on the attempt
  (c) send back: the Head's classification on attempt N, attempt N+1, the due date moved
      out by the review time — at BOTH opening statuses — and the head_* fields untouched;
      the null head_reviewed_at branch; an open extension request refuses it
  (d) blank category and blank remarks are refused in the view, before any write
  (e) both actions refuse every other status and every user without gate-2 authority
  (f) the PM's rejection remark on the Head's rendered sites row
  (g) query cost: the ledger read adds ZERO queries to design_head_sites, at 5 and 86 rows
  (h) the subset invariant: a stopped clock means the designer does not hold the site
  plus the screens: the QC queue excludes the status, the review screen's two branches,
  the PM's workspace banner, and the Head's count tile

THE FIXTURES REUSE THE TWO INERT MODULES' OWN WRITERS (RejectedBase._park_rejected and
PmGateBase._park_in_pm_gate), so this file adds no write shape to either walk. Field names
the walks search for are assembled from pieces for the same reason.
"""
import inspect
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.db import connection
from django.db.models import CharField, Value
from django.forms.models import model_to_dict
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from . import design_views
from .design_metrics import is_overdue
from .design_views import (
    design_head_dashboard_counts, design_head_return_to_pm, latest_design_transition,
)
from .models import (
    ArkaSubmission, DesignAssignment, DesignAttempt, DesignFile, DueDateCommitment, Project,
    StatusTransition,
    ARKA_APPROVED, ATTEMPT_REASON_PM_REJECTED, DESIGN_ARKA_SUBMITTED,
    DESIGN_ASSIGNMENT_STATUS_CHOICES, DESIGN_AWAITING_PM_APPROVAL,
    DESIGN_CLOCK_STOPPED_STATUSES, DESIGN_FILE_CAD_ZIP, DESIGN_IN_DESIGN, DESIGN_IN_QC,
    DESIGN_NOT_WITH_DESIGNER_STATUSES, DESIGN_PM_REJECTED, ERR_LAYOUT,
    ERR_SURVEY_INADEQUATE, QC_PASSED, REASON_DESIGN_HEAD_RETURNED_TO_PM,
    REASON_DESIGN_PM_REJECTED, SUBJECT_DESIGN_ASSIGNMENT,
)
from .tests_design_pm_gate_inert import PmGateBase, find_pm_gate_writes
from .tests_design_pm_rejected_inert import (
    RejectedBase, _profile, find_pm_rejected_writes,
)
from .utils import record_transition

#: The send-back form's remarks field. Assembled so no line here is a dict-key write of it.
_REMARKS = 'pm_rejection' + '_remarks'
_PM_REMARK = 'The inverter layout blocks the fire access path on the east side.'


class HeadRejectionBase(RejectedBase):
    """RejectedBase's head, designer and tender, plus the site's PM and a QC reviewer, and a
    package parked at pm_rejected exactly as 3.1b-2c will leave one."""

    def setUp(self):
        super().setUp()
        self.pm = _profile('hr_pm', 'PM')
        self.qc = _profile('hr_qc', 'Design')
        self.qc.is_design_qc = True
        self.qc.save()

    def _package(self, code, passed_days_ago=4, agreed_offset=5):
        """A site whose attempt 1 passed BOTH gates `passed_days_ago` days ago, with an Arka
        approved at both gates, a current CAD zip and a complete BOQ — what
        design_head_qc_pass() leaves behind. The agreed date is today + `agreed_offset`."""
        site, assignment = self._site(code, status=DESIGN_IN_QC)
        Project.objects.filter(pk=site.pk).update(assigned_pm=self.pm)
        DueDateCommitment.objects.filter(assignment=assignment).update(
            proposed_date=self.today + timedelta(days=agreed_offset))
        attempt = self._attempt(assignment, 1)
        passed = timezone.now() - timedelta(days=passed_days_ago)
        DesignAttempt.objects.filter(pk=attempt.pk).update(
            qc_verdict=QC_PASSED, qc_reviewed_by=self.qc, qc_reviewed_at=passed,
            head_verdict=QC_PASSED, head_reviewed_by=self.head, head_reviewed_at=passed,
            closed_at=passed, boq_submitted_at=passed, boq_submitted_by=self.designer)
        arka = ArkaSubmission.objects.create(
            attempt=attempt, version=1, capacity_kw=Decimal('100.00'),
            arka_link='https://arka.example/1', submitted_by=self.designer,
            verdict=ARKA_APPROVED, head_verdict=ARKA_APPROVED, is_current=True)
        DesignFile.objects.create(
            attempt=attempt, kind=DESIGN_FILE_CAD_ZIP, version=1, bucket='b',
            path=f'{code}/cad.zip', original_filename='cad.zip',
            derived_from_arka=arka, uploaded_by=self.designer, is_current=True)
        DesignAssignment.objects.filter(pk=assignment.pk).update(current_attempt_number=1)
        return site, assignment

    def _park_with_pm_remark(self, assignment, remark=_PM_REMARK):
        """Park at pm_rejected (RejectedBase's fixture writer) and write the PM's ledger row,
        as the PM's reject will from 3.1b-2c: its reason_code and its remark."""
        assignment = self._park_rejected(assignment)
        record_transition(assignment, assignment.status, actor=self.pm,
                          reason_code=REASON_DESIGN_PM_REJECTED, remark=remark)
        return assignment

    def _parked(self, code, **kw):
        site, assignment = self._package(code, **kw)
        return site, self._park_with_pm_remark(assignment)

    def _ledger(self, assignment):
        return StatusTransition.objects.filter(
            subject_type=SUBJECT_DESIGN_ASSIGNMENT, subject_id=assignment.pk)

    def _send_back(self, code, redo=('arka', 'cad', 'boq'), category=ERR_LAYOUT,
                   remarks='Move the string inverters clear of the fire path.'):
        return self.client.post(reverse('design_head_send_back', args=[code]), {
            'error_category': category, _REMARKS: remarks,
            'redo_scope_submitted': '1', 'redo': list(redo)})

    def _return(self, code, remark='The layout meets the fire code; see sheet 4.'):
        return self.client.post(reverse('design_head_return_to_pm', args=[code]),
                                {'remark': remark})

    def _effective_date(self, assignment):
        return (assignment.due_date_commitments.filter(approved_at__isnull=False)
                .order_by('-approved_at', '-pk').first())


# ===========================================================================
# (a) THE INERTNESS CHAIN — both halves, each asserted
# ===========================================================================

class InertnessChainTests(HeadRejectionBase):

    def test_a1_step_one_nothing_writes_pm_rejected(self):
        """STEP ONE, BY GREP: the status is written by the fixture in the pm_rejected module
        and nowhere else. This session added writers of the two fields and the reason, and
        NONE of the status."""
        status_hits = [(rel, pid, line) for rel, pid, line in find_pm_rejected_writes()
                       if pid.startswith('S')]
        self.assertEqual([(rel, pid) for rel, pid, _ in status_hits],
                         [('tests_design_pm_rejected_inert.py', 'S1')], status_hits)
        self.assertFalse(DesignAssignment.objects.filter(status=DESIGN_PM_REJECTED).exists())

    def test_a2_the_only_product_writer_of_awaiting_pm_approval_is_return_to_pm(self):
        """The grep's half of step two: one product write of awaiting_pm_approval, and it is
        in design_head_return_to_pm(), behind a pm_rejected-only guard."""
        product = [(rel, pid, line) for rel, pid, line in find_pm_gate_writes()
                   if not rel.startswith('tests_') and pid.startswith('W')]
        self.assertEqual([(rel, pid) for rel, pid, _ in product],
                         [('design_views.py', 'W3')], product)
        source = inspect.getsource(design_head_return_to_pm)
        self.assertIn('_qc_guard(request, project, (DESIGN_PM_REJECTED,)', source)
        self.assertIn('locked = _lock_pm_rejected(assignment)', source)

    def test_a3_step_two_return_to_pm_refuses_every_other_status(self):
        """STEP TWO, BY BEHAVIOUR — not left as reasoning in a comment. Every status in the
        choices except pm_rejected is posted against, as the Design Head with a valid
        remark. Each is refused with a message, and nothing is written: no status change,
        no ledger row, no attempt."""
        site, assignment = self._package('HR-A3')
        self._login(self.head)
        attempts = DesignAttempt.objects.count()
        others = [value for value, _ in DESIGN_ASSIGNMENT_STATUS_CHOICES
                  if value != DESIGN_PM_REJECTED]
        self.assertEqual(len(others), len(DESIGN_ASSIGNMENT_STATUS_CHOICES) - 1)
        for value in others:
            with self.subTest(status=value):
                DesignAssignment.objects.filter(pk=assignment.pk).update(status=value)
                rows = self._ledger(assignment).count()
                response = self._return('HR-A3')
                self.assertEqual(response.status_code, 302)
                self.assertIn('not available at this stage', self._messages(response))
                assignment.refresh_from_db()
                self.assertEqual(assignment.status, value)
                self.assertEqual(self._ledger(assignment).count(), rows)
                self.assertEqual(DesignAttempt.objects.count(), attempts)


# ===========================================================================
# (b) Return to PM
# ===========================================================================

class ReturnToPmTests(HeadRejectionBase):

    def setUp(self):
        super().setUp()
        self.site, self.assignment = self._parked('HR-B')
        self._login(self.head)

    def test_b1_returns_to_the_pm_with_one_ledger_row_and_nothing_on_the_attempt(self):
        attempt = DesignAttempt.objects.get(assignment=self.assignment, attempt_number=1)
        before = model_to_dict(attempt)
        rows = self._ledger(self.assignment).count()
        commitments = self.assignment.due_date_commitments.count()

        response = self._return('HR-B', remark='  The layout meets the fire code.  ')

        self.assertEqual(response.status_code, 302)
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.status, DESIGN_AWAITING_PM_APPROVAL)
        self.assertEqual(self._ledger(self.assignment).count(), rows + 1)
        row = self._ledger(self.assignment).order_by('-occurred_at', '-pk').first()
        self.assertEqual((row.from_status, row.to_status),
                         (DESIGN_PM_REJECTED, DESIGN_AWAITING_PM_APPROVAL))
        self.assertEqual(row.reason_code, REASON_DESIGN_HEAD_RETURNED_TO_PM)
        self.assertEqual(row.remark, 'The layout meets the fire code.')
        self.assertEqual(row.actor, self.head)
        # NOTHING on the attempt, and no attempt created.
        self.assertEqual(DesignAttempt.objects.filter(assignment=self.assignment).count(), 1)
        attempt.refresh_from_db()
        self.assertEqual(model_to_dict(attempt), before)
        self.assertEqual(self.assignment.current_attempt_number, 1)
        # Nor the date, the release or the PM's approval stamps.
        self.assertEqual(self.assignment.due_date_commitments.count(), commitments)
        self.assertIsNone(self.assignment.released_at)
        self.assertIsNone(self.assignment.pm_approved_at)

    def test_b2_a_blank_remark_is_refused_and_writes_nothing(self):
        rows = self._ledger(self.assignment).count()
        response = self._return('HR-B', remark='   ')
        self.assertEqual(response.status_code, 302)
        self.assertIn('a remark is required', self._messages(response))
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.status, DESIGN_PM_REJECTED)
        self.assertEqual(self._ledger(self.assignment).count(), rows)


# ===========================================================================
# (c) Send back to the designer
# ===========================================================================

class SendBackTests(HeadRejectionBase):

    def _assert_head_fields_untouched(self, attempt):
        self.assertEqual(attempt.head_verdict, QC_PASSED)
        self.assertEqual(attempt.head_failure_category, '')
        self.assertEqual(attempt.head_remarks, '')
        self.assertEqual(attempt.head_reviewed_by, self.head)

    def test_c1_redo_including_the_arka_opens_at_in_design(self):
        site, assignment = self._parked('HR-C1', passed_days_ago=4, agreed_offset=5)
        agreed = self._effective_date(assignment).proposed_date
        self._login(self.head)

        response = self._send_back('HR-C1', redo=('arka', 'cad', 'boq'))

        self.assertEqual(response.status_code, 302)
        assignment.refresh_from_db()
        self.assertEqual(assignment.status, DESIGN_IN_DESIGN)
        self.assertEqual(assignment.current_attempt_number, 2)
        first = DesignAttempt.objects.get(assignment=assignment, attempt_number=1)
        self.assertEqual(first.pm_rejection_category, ERR_LAYOUT)
        self.assertEqual(first.pm_rejection_remarks,
                         'Move the string inverters clear of the fire path.')
        self.assertEqual(first.redo_required, ['arka', 'boq', 'cad'])
        self._assert_head_fields_untouched(first)
        second = DesignAttempt.objects.get(assignment=assignment, attempt_number=2)
        self.assertEqual(second.opened_reason, ATTEMPT_REASON_PM_REJECTED)
        # D15 (c): moved out by the 4 whole days since the Head's pass, as one new
        # approved row — a revision.
        self.assertEqual(self._effective_date(assignment).proposed_date,
                         agreed + timedelta(days=4))
        self.assertEqual(assignment.due_date_commitments.count(), 2)
        self.assertEqual(assignment.due_date_commitments.filter(is_current=True).count(), 1)

    def test_c2_redo_without_the_arka_opens_at_arka_submitted_with_the_margin_restored(self):
        """The Arka is approved at both gates, so it carries forward and the site opens at
        arka_submitted. The designer had 2 days in hand when the Head passed the package 4
        days ago; they have 2 days in hand again, and are not overdue."""
        site, assignment = self._parked('HR-C2', passed_days_ago=4, agreed_offset=-2)
        self._login(self.head)

        self._send_back('HR-C2', redo=('boq',), category=ERR_SURVEY_INADEQUATE)

        assignment.refresh_from_db()
        self.assertEqual(assignment.status, DESIGN_ARKA_SUBMITTED)
        second = DesignAttempt.objects.get(assignment=assignment, attempt_number=2)
        self.assertEqual(second.opened_reason, ATTEMPT_REASON_PM_REJECTED)
        self.assertTrue(second.arka_submissions.filter(
            is_current=True, head_verdict=ARKA_APPROVED).exists())
        first = DesignAttempt.objects.get(assignment=assignment, attempt_number=1)
        self.assertEqual(first.redo_required, ['boq'])
        self._assert_head_fields_untouched(first)
        effective = self._effective_date(assignment)
        self.assertEqual(effective.proposed_date, self.today + timedelta(days=2))
        self.assertFalse(is_overdue(assignment, effective))

    def test_c3_a_null_head_reviewed_at_adds_nothing_and_the_site_is_overdue_on_arrival(self):
        """The named cost of decision (c): with no Head-pass time there is no span, so no
        row is written, and a past date stays past."""
        site, assignment = self._parked('HR-C3', agreed_offset=-2)
        DesignAttempt.objects.filter(assignment=assignment).update(head_reviewed_at=None)
        self._login(self.head)

        self._send_back('HR-C3')

        assignment.refresh_from_db()
        self.assertEqual(assignment.status, DESIGN_IN_DESIGN)
        self.assertEqual(assignment.due_date_commitments.count(), 1)
        self.assertTrue(is_overdue(assignment, self._effective_date(assignment)))

    def test_c4_an_open_extension_request_refuses_the_send_back_and_writes_nothing(self):
        """Product decision, 13 Sep 2026: refuse. The Head rules on the request first."""
        site, assignment = self._parked('HR-C4')
        assignment.due_date_commitments.filter(is_current=True).update(is_current=False)
        requested = DueDateCommitment.objects.create(
            assignment=assignment, proposed_date=self.today + timedelta(days=20),
            proposed_by=self.designer, change_reason='More strings than surveyed.',
            is_current=True)
        rows = self._ledger(assignment).count()
        self._login(self.head)

        response = self._send_back('HR-C4')

        self.assertEqual(response.status_code, 302)
        self.assertIn('extension request', self._messages(response))
        assignment.refresh_from_db()
        self.assertEqual(assignment.status, DESIGN_PM_REJECTED)
        self.assertEqual(DesignAttempt.objects.filter(assignment=assignment).count(), 1)
        first = DesignAttempt.objects.get(assignment=assignment, attempt_number=1)
        self.assertEqual((first.pm_rejection_category, first.redo_required), ('', []))
        self.assertEqual(assignment.due_date_commitments.count(), 2)
        requested.refresh_from_db()
        self.assertTrue(requested.is_current)
        self.assertIsNone(requested.approved_at)
        self.assertEqual(self._ledger(assignment).count(), rows)


# ===========================================================================
# (d) Blank category, blank remarks — refused in the view, before any write
# ===========================================================================

class SendBackValidationTests(HeadRejectionBase):

    def setUp(self):
        super().setUp()
        self.site, self.assignment = self._parked('HR-D')
        self._login(self.head)

    def _assert_nothing_written(self):
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.status, DESIGN_PM_REJECTED)
        self.assertEqual(DesignAttempt.objects.filter(assignment=self.assignment).count(), 1)
        first = DesignAttempt.objects.get(assignment=self.assignment, attempt_number=1)
        self.assertEqual(first.pm_rejection_category, '')
        self.assertEqual(first.pm_rejection_remarks, '')
        self.assertEqual(first.redo_required, [])
        self.assertEqual(self.assignment.due_date_commitments.count(), 1)

    def test_d1_a_blank_category_is_refused(self):
        response = self._send_back('HR-D', category='')
        self.assertEqual(response.status_code, 302)
        self.assertIn('an error category is required', self._messages(response))
        self._assert_nothing_written()

    def test_d2_blank_remarks_with_a_category_never_reach_the_database(self):
        """The case pm_rejection_remarks_required_with_category would refuse with an
        IntegrityError. The view refuses it first, as a message and a redirect."""
        response = self._send_back('HR-D', category=ERR_LAYOUT, remarks='   ')
        self.assertEqual(response.status_code, 302)
        self.assertIn('remarks are required', self._messages(response))
        self._assert_nothing_written()

    def test_d3_an_unrecognised_category_is_refused(self):
        response = self._send_back('HR-D', category='not_a_category')
        self.assertEqual(response.status_code, 302)
        self._assert_nothing_written()


# ===========================================================================
# (e) Wrong status, wrong person — never a 500, never a blank body
# ===========================================================================

class RefusalTests(HeadRejectionBase):

    def test_e1_send_back_refuses_every_other_status(self):
        site, assignment = self._package('HR-E1')
        self._login(self.head)
        attempts = DesignAttempt.objects.count()
        for value, _ in DESIGN_ASSIGNMENT_STATUS_CHOICES:
            if value == DESIGN_PM_REJECTED:
                continue
            with self.subTest(status=value):
                DesignAssignment.objects.filter(pk=assignment.pk).update(status=value)
                response = self._send_back('HR-E1')
                self.assertEqual(response.status_code, 302)
                self.assertIn('not available at this stage', self._messages(response))
                self.assertEqual(DesignAttempt.objects.count(), attempts)

    def test_e2_both_actions_refuse_a_user_without_gate_2_authority(self):
        site, assignment = self._parked('HR-E2')
        for who in (self.designer, self.qc, self.pm):
            self._login(who)
            for response in (self._return('HR-E2'), self._send_back('HR-E2')):
                with self.subTest(user=who.user.username, url=response.request['PATH_INFO']):
                    self.assertEqual(response.status_code, 403)
                    self.assertIn(b'Design Head', response.content)
        assignment.refresh_from_db()
        self.assertEqual(assignment.status, DESIGN_PM_REJECTED)
        self.assertEqual(DesignAttempt.objects.filter(assignment=assignment).count(), 1)

    def test_e3_the_head_who_is_the_sites_designer_is_refused(self):
        """Gate-2 authority, not Head authority: the self-review exclusion holds here too."""
        site, assignment = self._parked('HR-E3')
        DesignAssignment.objects.filter(pk=assignment.pk).update(assigned_to=self.head)
        self._login(self.head)
        self.assertEqual(self._return('HR-E3').status_code, 403)
        self.assertEqual(self._send_back('HR-E3').status_code, 403)

    def test_e4_a_get_redirects_to_the_review_screen(self):
        self._parked('HR-E4')
        self._login(self.head)
        for name in ('design_head_return_to_pm', 'design_head_send_back'):
            response = self.client.get(reverse(name, args=['HR-E4']))
            self.assertRedirects(response, reverse('design_qc_review', args=['HR-E4']),
                                 fetch_redirect_response=False)


# ===========================================================================
# (f) The PM's remark on the Head's sites row, and (g) what it costs
# ===========================================================================

class HeadSitesTests(HeadRejectionBase):

    def test_f1_the_pm_rejection_remark_is_on_the_rendered_row(self):
        self._parked('HR-F1')
        self._package('HR-F2')                  # a neighbour that is not rejected
        self._login(self.head)
        html = self.client.get(
            reverse('design_head_sites', args=[self.program.pk])).content.decode()
        self.assertIn(_PM_REMARK, html)
        self.assertIn('rejected by the PM', html)
        self.assertEqual(html.count('Review PM rejection'), 1)
        self.assertIn(reverse('design_qc_review', args=['HR-F1']), html)

    def test_f2_only_the_latest_pm_rejection_is_shown(self):
        site, assignment = self._parked('HR-F3')
        record_transition(assignment, assignment.status, actor=self.pm,
                          reason_code=REASON_DESIGN_PM_REJECTED, remark='Second look: still no.')
        self._login(self.head)
        html = self.client.get(
            reverse('design_head_sites', args=[self.program.pk])).content.decode()
        self.assertIn('Second look: still no.', html)
        self.assertNotIn(_PM_REMARK, html)

    def _queries(self, patched):
        self._login(self.head)
        url = reverse('design_head_sites', args=[self.program.pk])
        if patched:
            blank = mock.patch.object(
                design_views, 'latest_design_transition',
                side_effect=lambda *a, **k: Value(None, output_field=CharField()))
            with blank, CaptureQueriesContext(connection) as ctx:
                self.assertEqual(self.client.get(url).status_code, 200)
        else:
            with CaptureQueriesContext(connection) as ctx:
                self.assertEqual(self.client.get(url).status_code, 200)
        return len(ctx.captured_queries)

    def _measure(self, rows):
        self._parked('HR-G-00')
        for i in range(1, rows):
            self._package(f'HR-G-{i:02d}')
        with_read, without_read = self._queries(False), self._queries(True)
        self.assertEqual(with_read, without_read,
                         f'the ledger read added {with_read - without_read} queries '
                         f'at {rows} rows')
        return with_read

    def test_g1_the_ledger_read_adds_no_query_at_5_rows(self):
        self._measure(5)

    def test_g2_the_ledger_read_adds_no_query_at_86_rows(self):
        """The row count nirankar's MPUVNL screen renders. The screen's OWN growth with row
        count is pre-existing (§D21) and deliberately not pinned here."""
        self._measure(86)

    def test_g3_the_helper_is_one_query_for_every_row(self):
        for i in range(3):
            self._parked(f'HR-G3-{i}')
        with CaptureQueriesContext(connection) as ctx:
            rows = list(DesignAssignment.objects.annotate(
                remark=latest_design_transition('remark',
                                                reason_code=REASON_DESIGN_PM_REJECTED)))
        self.assertEqual(len(ctx.captured_queries), 1)
        self.assertEqual({r.remark for r in rows}, {_PM_REMARK})


# ===========================================================================
# The screens: the queue, the review screen, the workspace, the count tile
# ===========================================================================

class ScreenTests(HeadRejectionBase):

    def test_qc_queue_excludes_a_pm_rejected_package_for_the_head_and_for_qc(self):
        self._parked('HR-Q1')
        for who in (self.head, self.qc):
            self._login(who)
            html = self.client.get(reverse('design_qc_queue')).content.decode()
            self.assertNotIn('HR-Q1', html)

    def test_the_head_sees_both_actions_on_the_review_screen(self):
        self._parked('HR-R1')
        self._login(self.head)
        html = self.client.get(reverse('design_qc_review', args=['HR-R1'])).content.decode()
        self.assertIn(reverse('design_head_return_to_pm', args=['HR-R1']), html)
        self.assertIn(reverse('design_head_send_back', args=['HR-R1']), html)
        self.assertIn('PM rejection · Design Head', html)
        self.assertIn('redoDefaults', html)
        self.assertNotIn('Nothing to review yet', html)

    def test_d2_a_qc_reviewer_is_told_the_truth_and_offered_nothing(self):
        site, assignment = self._parked('HR-R2')
        self._login(self.qc)
        html = self.client.get(reverse('design_qc_review', args=['HR-R2'])).content.decode()
        self.assertIn('The site\'s PM rejected this package', html)
        self.assertNotIn('Nothing to review yet', html)
        self.assertNotIn(reverse('design_head_send_back', args=['HR-R2']), html)

        PmGateBase._park_in_pm_gate(self, assignment)
        html = self.client.get(reverse('design_qc_review', args=['HR-R2'])).content.decode()
        self.assertIn('It is with the site\'s PM for approval', html)
        self.assertNotIn('Nothing to review yet', html)

    def test_d10_the_workspace_tells_the_pm_the_truth_and_the_head_what_it_always_did(self):
        site, assignment = self._package('HR-W1')
        PmGateBase._park_in_pm_gate(self, assignment)
        url = reverse('design_site_workspace', args=['HR-W1'])

        self._login(self.pm)
        html = self.client.get(url).content.decode()
        self.assertIn('as the site\'s Project Manager', html)
        self.assertIn(reverse('design_pm_approval_queue'), html)
        self.assertNotIn('You are viewing this as Design Head', html)
        self.assertNotIn(reverse('design_my_sites'), html)

        self._login(self.head)
        html = self.client.get(url).content.decode()
        self.assertIn('You are viewing this as Design Head', html)
        self.assertIn(reverse('design_head_review', args=['HR-W1']), html)
        self.assertNotIn('as the site\'s Project Manager', html)

    def test_the_count_tile_counts_pm_rejected_apart_from_awaiting_head_qc(self):
        self._parked('HR-T1')
        counts = design_head_dashboard_counts(self.head.user)
        self.assertEqual(counts['pm_rejected'], 1)
        self.assertEqual(counts['awaiting_head_qc'], 0)
        self._login(self.head)
        html = self.client.get(reverse('dashboard_design')).content.decode()
        self.assertIn('Rejected by the <strong>PM</strong>', html)


# ===========================================================================
# (h) The subset invariant
# ===========================================================================

class SubsetInvariantTests(HeadRejectionBase):

    def test_h_a_stopped_clock_means_the_designer_does_not_hold_the_site(self):
        """Folded in from the D13 review. If a future session adds a status to the
        clock-stopped set alone, this fails: the designer's clock cannot stop on a site
        the designer still holds."""
        self.assertTrue(DESIGN_CLOCK_STOPPED_STATUSES <= DESIGN_NOT_WITH_DESIGNER_STATUSES)
