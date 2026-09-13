"""The D13 prompt — a package under review cannot be put on Design Hold.

WHY THIS FILE EXISTS
--------------------
A Design Hold means "the survey is inadequate, so design work pauses". The Head clears it by
supplying a survey, and _status_after_unblock() returns the site to `in_design` — correct for
a site that was being designed. A hold taken at `artifacts_uploaded`, `in_qc` or
`awaiting_head_qc` came back at `in_design` too, on the SAME attempt: no new attempt, no
change request, the package out of the QC queue, its BOQ still design-locked
(EXECUTION_MODULE_DEFERRED.md §D13).

The fix is a refusal, not a redirection. Those three statuses joined
DESIGN_NOT_WITH_DESIGNER_STATUSES; _status_after_unblock() is unchanged. The date guards moved
to DESIGN_CLOCK_STOPPED_STATUSES, whose membership did not change, because the overdue clock
keeps running during review.

  (a) the hold is refused at each review status, through the real endpoint, with the message
  (b) my_sites does not draw the Hold control on any of them
  (c) the hold is still allowed wherever the designer does hold the site, and clears back
  (d) THE ALTERNATIVE ROUTE, end to end, from both statuses the message offers it at
  (e) the regression pin: from in_qc, hold -> clear can no longer produce in_design
  (f) is_overdue() and attention_list() answer for every status exactly as before
  (g) the split polices itself: every clock-stopped status is excluded from is_overdue()
"""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .design_metrics import (
    _classify, attempt_cause_split, attention_list, classify_attempt_causes, is_overdue,
)
from .models import (
    ActivityLog, ArkaSubmission, DesignAssignment, DesignAttempt, DesignFile,
    DueDateCommitment, Program, Project,
    ARKA_APPROVED, ATTEMPT_REASON_INITIAL, ATTEMPT_REASON_QC_FAILED,
    DESIGN_ARTIFACTS_UPLOADED, DESIGN_ASSIGNMENT_STATUS_CHOICES, DESIGN_AWAITING_HEAD_QC,
    DESIGN_CLOCK_STOPPED_STATUSES, DESIGN_ERROR_CATEGORY_LABELS, DESIGN_FILE_CAD_ZIP,
    DESIGN_IN_DESIGN, DESIGN_IN_QC, DESIGN_NOT_WITH_DESIGNER_STATUSES, DESIGN_SURVEY_RETURNED,
    ERR_SURVEY_INADEQUATE, ERROR_GROUP_B,
)

REVIEW_STATUSES = (DESIGN_ARTIFACTS_UPLOADED, DESIGN_IN_QC, DESIGN_AWAITING_HEAD_QC)

#: The refusal, verbatim, per review status. Written out by hand rather than assembled from
#: the view's table, so a change to the wording has to be made in two places on purpose.
_TAIL = (' so it cannot go on Design Hold — a hold pauses design work, and this package is '
         'no longer being designed. If the survey is inadequate, ask {who} to fail the '
         'package with the category "Survey data inadequate or incorrect". It comes back to '
         'you as a new attempt, recorded as a survey problem, not as your rework.')
EXPECTED_REFUSAL = {
    DESIGN_ARTIFACTS_UPLOADED: '{code} is waiting for Design QC to start review,'
                               + _TAIL.format(who='Design QC'),
    DESIGN_IN_QC:              '{code} is in review with Design QC,'
                               + _TAIL.format(who='Design QC'),
    DESIGN_AWAITING_HEAD_QC:   '{code} has passed Design QC and is with the Design Head,'
                               + _TAIL.format(who='the Design Head'),
}


def _profile(username, role='Design', is_design_head=False, is_design_qc=False):
    """A post_save signal auto-creates the UserProfile; fetch and set, never create."""
    user = User.objects.create_user(username=username, password='x')
    profile = user.profile
    profile.role = role
    profile.is_active = True
    profile.is_design_head = is_design_head
    profile.is_design_qc = is_design_qc
    profile.save()
    return profile


class HoldBase(TestCase):

    def setUp(self):
        self.head     = _profile('hr_head', is_design_head=True)
        self.qc       = _profile('hr_qc', is_design_qc=True)
        self.designer = _profile('hr_des')
        self.program = Program.objects.create(
            name='Test-HoldRefusal', program_type='OPEX', client_name='HRClient',
            status='Active', short_tender_code='HR')
        self.today = timezone.localdate()

    def _site(self, code, status=DESIGN_IN_DESIGN, due_in_days=5):
        """An allocated site with an APPROVED agreed date and a survey FILE (no folder link,
        so the Head's link form is not locked), at `status`."""
        site = Project(
            project_id=code, customer_name='HRClient', customer_phone='9876543210',
            site_address='1 Sun Rd', city='Delhi', project_type='OPEX',
            program=self.program, site_code=code,
            dc_capacity_kw=Decimal('100.00'), status='Draft')
        site.save()
        assignment = DesignAssignment.objects.create(
            project=site, status=status, assigned_to=self.designer,
            assigned_by=self.head, assigned_at=timezone.now(),
            survey_file_bucket='b', survey_file_path=f'{code}/survey/x.pdf')
        DueDateCommitment.objects.create(
            assignment=assignment, proposed_date=self.today + timedelta(days=due_in_days),
            proposed_by=self.head, approved_by=self.head, approved_at=timezone.now(),
            is_current=True)
        return site, assignment

    def _complete_package(self, assignment, qc_started):
        """Attempt 1 carrying everything _package_is_complete() checks: an Arka approved at
        both gates, a current cad_zip paired to it, and a BOQ marked complete."""
        attempt = DesignAttempt.objects.create(
            assignment=assignment, attempt_number=1, opened_reason=ATTEMPT_REASON_INITIAL)
        DesignAssignment.objects.filter(pk=assignment.pk).update(current_attempt_number=1)
        assignment.refresh_from_db()
        arka = ArkaSubmission.objects.create(
            attempt=attempt, version=1, capacity_kw=Decimal('100.00'),
            arka_link='https://example.com/arka', submitted_by=self.designer,
            verdict=ARKA_APPROVED, reviewed_by=self.head, reviewed_at=timezone.now(),
            head_verdict=ARKA_APPROVED, head_reviewed_by=self.head,
            head_reviewed_at=timezone.now(), is_current=True)
        DesignFile.objects.create(
            attempt=attempt, kind=DESIGN_FILE_CAD_ZIP, version=1,
            bucket='b', path=f'{assignment.project.project_id}/cad_zip/a.zip',
            original_filename='a.zip', size_bytes=1000,
            archive_listing=[{'name': 'a.pdf', 'size': 10}, {'name': 'a.dwg', 'size': 20}],
            derived_from_arka=arka, uploaded_by=self.designer, is_current=True)
        attempt.boq_submitted_at = timezone.now()
        attempt.boq_submitted_by = self.designer
        if qc_started:
            attempt.qc_started_at = timezone.now()
        attempt.save()
        return attempt

    def _login(self, profile):
        self.assertTrue(self.client.login(username=profile.user.username, password='x'))

    def _post(self, name, site, data=None, **kwargs):
        return self.client.post(reverse(name, kwargs={'project_id': site.project_id}),
                                data or {}, **kwargs)

    def _messages(self, response):
        return [str(m) for m in get_messages(response.wsgi_request)]

    def _hold(self, site, reason='the survey has no roof dimensions'):
        return self._post('design_mark_blocked', site, {'reason': reason})

    def _clear_by_link(self, site):
        """The Head clears a hold by recording a survey folder link — one of the two callers
        of _status_after_unblock(); the other needs file storage."""
        return self._post('design_survey_link_set', site,
                          {'survey_folder_url': 'https://drive.google.com/drive/folders/x'})


# ===========================================================================
# (a) Refused at each review status, through the real endpoint
# ===========================================================================

class RefusalTests(HoldBase):

    def _assert_refused(self, status):
        code = f'HR-A-{status[:6].upper()}'
        site, assignment = self._site(code, status=status)
        self._login(self.designer)
        response = self._hold(site)

        # Not a 500 and not a 403: _deny's shape, a redirect carrying the message.
        self.assertEqual(response.status_code, 302, status)
        self.assertEqual(self._messages(response), [EXPECTED_REFUSAL[status].format(code=code)])
        # Nothing moved and nothing was recorded — a refused act is not an event.
        assignment.refresh_from_db()
        self.assertEqual(assignment.status, status)
        self.assertIsNone(assignment.survey_returned_at)
        self.assertEqual(assignment.survey_return_reason, '')
        self.assertFalse(ActivityLog.objects.filter(
            project=site, action_code='design_blocked').exists())

        # And the page it lands on is not blank: the message renders there.
        followed = self._hold(site, reason='again')
        self.assertEqual(followed.status_code, 302)
        page = self.client.get(followed['Location'])
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'so it cannot go on Design Hold')

    def test_01_artifacts_uploaded(self):
        self._assert_refused(DESIGN_ARTIFACTS_UPLOADED)

    def test_02_in_qc(self):
        self._assert_refused(DESIGN_IN_QC)

    def test_03_awaiting_head_qc(self):
        self._assert_refused(DESIGN_AWAITING_HEAD_QC)

    def test_04_the_message_names_the_real_category_label(self):
        """The refusal quotes the category by its label; if the label is ever reworded, the
        designer would be told to ask for a category the reviewer cannot find."""
        self.assertEqual(DESIGN_ERROR_CATEGORY_LABELS[ERR_SURVEY_INADEQUATE],
                         'Survey data inadequate or incorrect')
        for text in EXPECTED_REFUSAL.values():
            self.assertIn(f'"{DESIGN_ERROR_CATEGORY_LABELS[ERR_SURVEY_INADEQUATE]}"', text)
            # Not a permission refusal (the mirror-refusal precedent): no permission exists
            # that would make this hold right, so the text must not invite a request for one.
            self.assertNotIn('permission', text.lower())


# ===========================================================================
# (b) my_sites does not draw the Hold control at any review status
# ===========================================================================

class ButtonTests(HoldBase):

    def test_01_no_hold_control_on_my_sites_under_review(self):
        control_site, _ = self._site('HR-B-CTL', status=DESIGN_IN_DESIGN)
        review_sites = {s: self._site(f'HR-B-{i}', status=s)[0]
                        for i, s in enumerate(REVIEW_STATUSES)}
        self._login(self.designer)
        html = self.client.get(reverse('design_my_sites')).content.decode()

        # Control: the same screen draws the control for a site being designed, so its
        # absence below comes from the status and not from the screen.
        control_url = reverse('design_mark_blocked', kwargs={'project_id': control_site.project_id})
        self.assertIn(f'action="{control_url}"', html)
        self.assertIn(f'data-bs-target="#blk{control_site.pk}"', html)

        for status, site in review_sites.items():
            url = reverse('design_mark_blocked', kwargs={'project_id': site.project_id})
            self.assertNotIn(f'action="{url}"', html, status)
            self.assertNotIn(f'data-bs-target="#blk{site.pk}"', html, status)
            self.assertNotIn(f'id="blk{site.pk}"', html, status)


# ===========================================================================
# (c) Still allowed wherever the designer holds the site — and it still clears back
# ===========================================================================

class StillAllowedTests(HoldBase):

    #: Where the designer DOES hold the site, written out by hand — every allocated working
    #: status outside the refused set. The three pre-allocation/held statuses are refused by
    #: can_mark_blocked's own tuple, not by the set, and are not a designer's workflow.
    DESIGNER_HOLDS = ('allocated', 'due_date_proposed', 'in_design', 'arka_submitted',
                      'arka_rejected', 'awaiting_head_arka', 'qc_failed')

    def test_01_the_hand_list_is_every_status_the_set_leaves_open(self):
        every = {v for v, _ in DESIGN_ASSIGNMENT_STATUS_CHOICES}
        left_open = every - DESIGN_NOT_WITH_DESIGNER_STATUSES - {
            DESIGN_SURVEY_RETURNED, 'awaiting_survey', 'awaiting_allocation'}
        self.assertEqual(left_open, set(self.DESIGNER_HOLDS))

    def test_02_the_hold_is_accepted_at_each(self):
        self._login(self.designer)
        for i, status in enumerate(self.DESIGNER_HOLDS):
            site, assignment = self._site(f'HR-C-{i}', status=status)
            self._hold(site)
            assignment.refresh_from_db()
            self.assertEqual(assignment.status, DESIGN_SURVEY_RETURNED, status)

    def test_03_a_hold_from_in_design_clears_back_to_in_design(self):
        """The working workflow, whole: hold, then the Head supplies a survey."""
        site, assignment = self._site('HR-C-CLR', status=DESIGN_IN_DESIGN)
        self._login(self.designer)
        self._hold(site)
        assignment.refresh_from_db()
        self.assertEqual(assignment.status, DESIGN_SURVEY_RETURNED)

        self._login(self.head)
        self._clear_by_link(site)
        assignment.refresh_from_db()
        self.assertEqual(assignment.status, DESIGN_IN_DESIGN)


# ===========================================================================
# (d) THE ALTERNATIVE ROUTE the refusal names — the test that makes it defensible
# ===========================================================================

class AlternativeRouteTests(HoldBase):
    """A package under review that needs a new survey is failed with survey_inadequate. That
    opens attempt N+1 as a QC failure whose cause is Group B — an input problem, not the
    designer's rework (B-06). Driven through the real endpoints, as the QC reviewer."""

    def _fail_for_survey(self, site):
        return self._post('design_qc_fail', site, {
            'qc_remarks': 'the survey has no roof dimensions — a new survey is needed',
            'error_category': ERR_SURVEY_INADEQUATE})

    def _assert_input_problem(self, assignment):
        assignment.refresh_from_db()
        self.assertEqual(assignment.current_attempt_number, 2)
        attempts = list(assignment.attempts.order_by('attempt_number'))
        self.assertEqual([a.attempt_number for a in attempts], [1, 2])
        self.assertEqual(attempts[0].qc_failure_category, ERR_SURVEY_INADEQUATE)
        self.assertIsNotNone(attempts[0].closed_at)
        self.assertEqual(attempts[1].opened_reason, ATTEMPT_REASON_QC_FAILED)

        self.assertEqual(classify_attempt_causes(attempts), {1: None, 2: ERROR_GROUP_B})
        split = attempt_cause_split(attempts)
        self.assertEqual(split['input'], 1, split)
        self.assertEqual(split['designer'], 0, split)
        self.assertEqual(split['group_a'], 0, split)
        # The survey-inadequate default redoes everything, so nothing carries forward and
        # the designer starts attempt 2 by submitting an Arka.
        self.assertEqual(assignment.status, DESIGN_IN_DESIGN)

    def test_01_from_in_qc(self):
        site, assignment = self._site('HR-D-QC', status=DESIGN_IN_QC)
        self._complete_package(assignment, qc_started=True)
        self._login(self.qc)
        response = self._fail_for_survey(site)
        self.assertEqual(response.status_code, 302)
        self._assert_input_problem(assignment)

    def test_02_from_artifacts_uploaded_qc_starts_then_fails(self):
        """The message tells a designer here to ask Design QC, and QC has not started. The
        door it names opens: QC start's only preconditions are the reviewer's authority and a
        complete package, which a site that reached `artifacts_uploaded` already has."""
        site, assignment = self._site('HR-D-AU', status=DESIGN_ARTIFACTS_UPLOADED)
        self._complete_package(assignment, qc_started=False)
        self._login(self.qc)

        self._post('design_qc_start', site)
        assignment.refresh_from_db()
        self.assertEqual(assignment.status, DESIGN_IN_QC)

        self._fail_for_survey(site)
        self._assert_input_problem(assignment)


# ===========================================================================
# (e) The regression pin on the defect itself
# ===========================================================================

class ReopenRoutePinTests(HoldBase):
    """FAILS IF THE THREE REVIEW STATUSES LEAVE DESIGN_NOT_WITH_DESIGNER_STATUSES. With them
    out, the hold is accepted, the Head's link clears it, and _status_after_unblock() puts a
    package under review back at `in_design` on the same attempt — which is §D13."""

    def test_01_hold_then_clear_from_in_qc_cannot_produce_in_design(self):
        site, assignment = self._site('HR-E-QC', status=DESIGN_IN_QC)
        attempt = self._complete_package(assignment, qc_started=True)

        self._login(self.designer)
        self._hold(site)
        self._login(self.head)
        self._clear_by_link(site)

        assignment.refresh_from_db()
        self.assertNotEqual(assignment.status, DESIGN_IN_DESIGN,
                            'a package under review was reopened to in_design by a Design '
                            'Hold and its clear — the §D13 route is open again')
        self.assertEqual(assignment.status, DESIGN_IN_QC)
        self.assertEqual(assignment.current_attempt_number, attempt.attempt_number)
        self.assertEqual(assignment.attempts.count(), 1)


# ===========================================================================
# (f) is_overdue and attention_list: the same answer for every status as before
# ===========================================================================

class OverdueRuleUnmovedTests(HoldBase):
    """3.1b-2a made is_overdue() and attention_list() name statuses directly instead of reading
    the guard set, so this change could not move them. These tables are the answers before
    this change, written out by hand — a test that imported the sets would agree with them."""

    #: The only statuses where a site past its approved date is NOT overdue, and where its
    #: revision count is NOT listed for attention. Every other status answers the opposite.
    NOT_ON_THE_CLOCK = frozenset({'awaiting_pm_approval', 'released', 'pm_rejected'})

    def _every_status_site(self, due_in_days):
        return {status: self._site(f'HR-F{due_in_days}-{i:02d}', status=status,
                                   due_in_days=due_in_days)[1]
                for i, (status, _) in enumerate(DESIGN_ASSIGNMENT_STATUS_CHOICES)}

    def test_01_is_overdue_for_every_status(self):
        for status, assignment in self._every_status_site(due_in_days=-10).items():
            commitment = assignment.due_date_commitments.get()
            self.assertEqual(is_overdue(assignment, commitment, self.today),
                             status not in self.NOT_ON_THE_CLOCK, status)

    def test_02_attention_lists_revisions_for_every_status_on_the_clock(self):
        for status, assignment in self._every_status_site(due_in_days=5).items():
            site = {'assignment': assignment, 'project': assignment.project,
                    'designer': self.designer, 'stage': _classify(assignment, None),
                    'overdue': False, 'days_over': 0, 'pending_crs': [], 'blocked': False,
                    'revisions': 5, 'released': status == 'released', 'arka': None}
            # own_stages=() isolates the revisions band from the "waiting on you" band,
            # which lists by stage and would answer a different question.
            rows = attention_list([site], self.today, own_stages=())
            self.assertEqual(len(rows), 0 if status in self.NOT_ON_THE_CLOCK else 1, status)

    def test_03_the_review_statuses_are_still_on_the_clock(self):
        """The point of the split, stated on the rows: refusing the hold at these three did
        not stop their clock."""
        for i, status in enumerate(REVIEW_STATUSES):
            _, assignment = self._site(f'HR-F3-{i}', status=status, due_in_days=-3)
            self.assertTrue(is_overdue(assignment, assignment.due_date_commitments.get(),
                                       self.today), status)


# ===========================================================================
# (g) The split polices itself
# ===========================================================================

class SplitSelfPolicingTests(HoldBase):
    """THE DATE CONTROLS AND THE OVERDUE CLOCK MUST CLOSE AT THE SAME STATUSES, or a site goes
    overdue against a date nobody can move. The date controls read
    DESIGN_CLOCK_STOPPED_STATUSES, so every member must be excluded from is_overdue().

    ONE-DIRECTIONAL, deliberately not equality: is_overdue() also returns False for a site
    with no approved date — an unallocated site, say — for an unrelated reason, and an
    equality test would demand that such a status join the set."""

    def test_01_every_clock_stopped_status_is_excluded_from_is_overdue(self):
        self.assertTrue(DESIGN_CLOCK_STOPPED_STATUSES)
        for i, status in enumerate(sorted(DESIGN_CLOCK_STOPPED_STATUSES)):
            _, assignment = self._site(f'HR-G-{i}', status=status, due_in_days=-30)
            commitment = assignment.due_date_commitments.get()
            self.assertIsNotNone(commitment.approved_at)
            self.assertFalse(is_overdue(assignment, commitment, self.today), status)
