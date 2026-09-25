"""The Design allocation screen's two display-only columns: QC, and Pending at.

WHAT THIS FILE PINS, AND WHY EACH PIN IS HERE
---------------------------------------------
`design_pending_at()` derives WHO OWES THE NEXT MOVE on a design assignment. It is
display-only: nothing stores it, no writer maintains it, and no status decision reads it.
That makes it cheap to get wrong quietly — a derivation with no consumer but a screen
fails by printing the wrong word, not by raising — so the pins below are about the two
ways it can print the wrong word:

  1. A STATUS WITH NO MAPPING. Test (a) iterates DESIGN_ASSIGNMENT_STATUS_CHOICES itself
     and refuses to hand-copy it. A status added to models.py without an entry in
     DESIGN_PENDING_AT fails here, which is the whole reason the dict lives beside the
     choices list rather than in the view.

  2. A MAPPING THAT READS THE STATUS ALONE WHERE THE STATUS IS NOT ENOUGH. Three of the
     answers are not status lookups — `arka_submitted` splits on the current Arka's
     head_verdict, the QC stages split on whether a reviewer was named, and a pending
     change request overrides every status there is. Each has its own class below.

AND THE COST. The column is derived per row on a list screen, so the only thing that
makes it affordable is that the helper never touches the database. (d) asserts that
directly with assertNumQueries(0) over rows the view has already fetched, and (e) asserts
the view's own budget grows by a constant rather than by the row count.

WHAT THIS FILE DELIBERATELY DOES NOT PIN. The three-per-row due-date N+1 that
design_head_sites() already had before these columns existed. It is recorded in
docs/DESIGN_MODULE_DEFERRED.md with its measured numbers; (e) is written as a DELTA for
exactly that reason — it holds whether or not that N+1 is ever fixed, and fixing it must
not have to touch this file.
"""
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import (
    ArkaSubmission, DesignAssignment, DesignAttempt, DesignChangeRequest, Program, Project,
    ARKA_APPROVED, ARKA_PENDING, ARKA_REJECTED,
    ATTEMPT_REASON_INITIAL,
    CHANGE_REQUEST_ACCEPTED, CHANGE_REQUEST_PENDING, CHANGE_REQUEST_REJECTED,
    DESIGN_ARKA_SUBMITTED, DESIGN_ARTIFACTS_UPLOADED, DESIGN_ASSIGNMENT_STATUS_CHOICES,
    DESIGN_AWAITING_SURVEY, DESIGN_IN_QC, DESIGN_PENDING_AT, DESIGN_QC_STAGE_STATUSES,
    DESIGN_RELEASED, DESIGN_SURVEY_RETURNED,
    PENDING_AT_CHANGE_REQUEST, PENDING_AT_DESIGNER, PENDING_AT_DESIGN_HEAD, PENDING_AT_ON_HOLD,
    PENDING_AT_PM, PENDING_AT_QC, PENDING_AT_QC_POOL, PENDING_AT_SCM, PENDING_AT_SURVEY,
    design_pending_at,
)


def _profile(username, role='Design', **flags):
    """A post_save signal auto-creates the UserProfile; fetch and set, never create."""
    user = User.objects.create_user(username=username, password='x')
    profile = user.profile
    profile.role = role
    profile.is_active = True
    for field, value in flags.items():
        setattr(profile, field, value)
    profile.save()
    return profile


class PendingAtBase(TestCase):
    """One tender, one Head, one designer, one QC reviewer.

    `_site()` parks a status with a queryset update where it needs to — every status in
    the choices list has to be reachable here, including `qc_failed`, which no product
    writer can produce (Session C dropped the write). These are tests of what a status
    DISPLAYS AS, not of how it is reached, so a fixture writer is the honest instrument;
    the product writers of each status are pinned by tests_design_pm_gate_live.
    """

    def setUp(self):
        self.head     = _profile('pa_head', is_design_head=True)
        self.designer = _profile('pa_des')
        self.reviewer = _profile('pa_qc')
        self.program = Program.objects.create(
            name='Test-PendingAt', program_type='OPEX', client_name='PAClient',
            status='Active', short_tender_code='PA')

    def _site(self, code, status=DESIGN_IN_QC, qc=None, with_attempt=False):
        site = Project(
            project_id=code, customer_name='PAClient', customer_phone='9876543210',
            site_address='1 Sun Rd', city='Delhi', project_type='OPEX',
            program=self.program, site_code=code,
            dc_capacity_kw=Decimal('100.00'), status='Draft')
        site.save()
        assignment = DesignAssignment.objects.create(
            project=site, status=status, assigned_to=self.designer,
            assigned_by=self.head, assigned_at=timezone.now(),
            qc_assigned_to=qc,
            survey_file_bucket='b', survey_file_path=code + '/survey/x.pdf')
        if with_attempt:
            self._attempt(assignment)
        return site, assignment

    def _attempt(self, assignment, number=1):
        attempt = DesignAttempt.objects.create(
            assignment=assignment, attempt_number=number,
            opened_reason=ATTEMPT_REASON_INITIAL)
        assignment.current_attempt_number = number
        assignment.save(update_fields=['current_attempt_number'])
        return attempt

    def _arka(self, attempt, head_verdict=ARKA_PENDING):
        """The current Arka on `attempt`, at the Head verdict asked for.

        `head_rejection_reason` is filled on the rejected branch because the Part 1 CHECK
        constraint head_rejection_reason_required_when_head_rejected demands it — a Head
        who refuses a layout without saying why is exactly what that constraint exists to
        stop, and a fixture is not exempt from it.
        """
        return ArkaSubmission.objects.create(
            attempt=attempt, version=1, capacity_kw=Decimal('100.00'),
            arka_link='https://example.com/arka', submitted_by=self.designer,
            verdict=ARKA_PENDING, head_verdict=head_verdict,
            head_rejection_reason=('Setback not met.'
                                   if head_verdict == ARKA_REJECTED else ''),
            is_current=True)

    def _site_rows(self, program=None):
        """The view's `rows`, fetched exactly as design_head_sites() fetches them, so the
        query assertions below are made against the real prefetch and not a convenient
        stand-in."""
        self.client.login(username=self.head.user.username, password='x')
        response = self.client.get(
            reverse('design_head_sites', kwargs={'pk': (program or self.program).pk}))
        self.assertEqual(response.status_code, 200)
        return response, response.context['rows']


# ===========================================================================
# (a) EVERY STATUS IS MAPPED — iterated from the real list, never transcribed
# ===========================================================================

class PendingAtCoverageTests(TestCase):

    def test_01_every_status_constant_has_a_mapping(self):
        """THE POINT OF THIS TEST IS THAT IT READS THE SOURCE LIST.

        A hand-copied roster would agree with any status somebody adds to models.py and
        forgets to map — it would pass while the screen printed a fallback for a real
        row. Iterating DESIGN_ASSIGNMENT_STATUS_CHOICES means the two lists cannot drift:
        the new status arrives in this loop the moment it is declared.
        """
        unmapped = [status for status, _label in DESIGN_ASSIGNMENT_STATUS_CHOICES
                    if status not in DESIGN_PENDING_AT]
        self.assertEqual(
            unmapped, [],
            'These DesignAssignment statuses have no DESIGN_PENDING_AT entry, so the '
            'Design Head site list cannot say who is holding them up: %s. Add each to '
            'the dict beside DESIGN_ASSIGNMENT_STATUS_CHOICES in models.py.' % unmapped)

    def test_02_the_mapping_invents_no_status(self):
        """The converse. An entry keyed on a status that no longer exists is dead weight
        that reads as coverage, so the dict is pinned in both directions."""
        known = {status for status, _label in DESIGN_ASSIGNMENT_STATUS_CHOICES}
        self.assertEqual(set(DESIGN_PENDING_AT) - known, set())

    def test_03_every_label_is_a_non_empty_string(self):
        """A mapping to None or '' renders an empty badge, which is the failure the
        column exists to prevent — it reads as "nobody is waiting on this"."""
        for status, label in DESIGN_PENDING_AT.items():
            self.assertIsInstance(label, str, status)
            self.assertTrue(label.strip(), status)

    def test_04_the_qc_stage_set_is_a_subset_of_the_choices(self):
        known = {status for status, _label in DESIGN_ASSIGNMENT_STATUS_CHOICES}
        self.assertTrue(DESIGN_QC_STAGE_STATUSES <= known)


# ===========================================================================
# (b) THE FIXED ANSWERS — the statuses that are a plain dict lookup
# ===========================================================================

class PendingAtFixedAnswerTests(PendingAtBase):

    def test_01_design_hold_is_on_hold(self):
        _, assignment = self._site('PA-HOLD', status=DESIGN_SURVEY_RETURNED)
        self.assertEqual(design_pending_at(assignment), PENDING_AT_ON_HOLD)

    def test_02_released_is_with_scm(self):
        _, assignment = self._site('PA-REL', status=DESIGN_RELEASED)
        self.assertEqual(design_pending_at(assignment), PENDING_AT_SCM)

    def test_03_awaiting_survey_is_survey_not_the_head(self):
        """"Survey", NOT "Design Head". A site can sit here because the client has not
        sent one, which is not a queue the Head can clear by acting — naming him would
        put work on his list that is not his to do."""
        _, assignment = self._site('PA-SURV', status=DESIGN_AWAITING_SURVEY)
        self.assertEqual(design_pending_at(assignment), PENDING_AT_SURVEY)
        self.assertNotEqual(design_pending_at(assignment), PENDING_AT_DESIGN_HEAD)

    def test_04_a_site_with_no_assignment_is_survey(self):
        """The template's hard-coded "Awaiting survey" badge covers the row with no
        DesignAssignment at all; this is the Pending-at cell on that same row, and the
        two must agree. 82 of the 93 assignments on the local database are pre-allocation
        rows, so this is the common case rather than an edge one."""
        self.assertEqual(design_pending_at(None), PENDING_AT_SURVEY)

    def test_05_the_pm_gate_names_the_pm(self):
        from .models import DESIGN_AWAITING_PM_APPROVAL
        _, assignment = self._site('PA-PM', status=DESIGN_AWAITING_PM_APPROVAL)
        self.assertEqual(design_pending_at(assignment), PENDING_AT_PM)

    def test_06_a_pm_rejection_returns_to_the_head(self):
        """Not the PM, and not the designer: the Head is the one who decides which of the
        two it goes back to (prompt 3.1b-2b)."""
        from .models import DESIGN_PM_REJECTED
        _, assignment = self._site('PA-PMREJ', status=DESIGN_PM_REJECTED)
        self.assertEqual(design_pending_at(assignment), PENDING_AT_DESIGN_HEAD)

    def test_07_qc_failed_maps_although_no_row_can_carry_it(self):
        """Session C dropped the `qc_failed` write — design_qc_fail() opens attempt N+1 in
        the same atomic block, so nothing could ever observe the status. It is still a
        legal choices member, so it is still mapped, and this test says so out loud rather
        than leaving a future reader to wonder whether the entry is live."""
        from .models import DESIGN_QC_FAILED
        self.assertIn(DESIGN_QC_FAILED, DESIGN_PENDING_AT)
        _, assignment = self._site('PA-QCF', status=DESIGN_QC_FAILED)
        self.assertEqual(design_pending_at(assignment), PENDING_AT_DESIGNER)


# ===========================================================================
# (c) THE QC STAGES — assigned reviewer vs the open pool
# ===========================================================================

class PendingAtQcStageTests(PendingAtBase):
    """NULL qc_assigned_to IS THE OPEN POOL, NOT AN UNASSIGNED SITE.

    The field's own comment on DesignAssignment is emphatic about it, and the distinction
    is operational rather than cosmetic: with the field null, any is_design_qc holder who
    is not this site's designer may record the verdict; with it set, exactly one person
    may. A single "QC" label for both would tell the Head a site is somebody's when it is
    nobody's in particular.
    """

    def test_01_qc_stage_with_a_named_reviewer_is_qc(self):
        for status in sorted(DESIGN_QC_STAGE_STATUSES):
            with self.subTest(status=status):
                _, assignment = self._site(
                    'PA-QCS-' + status[:6], status=status, qc=self.reviewer)
                self.assertEqual(design_pending_at(assignment), PENDING_AT_QC)

    def test_02_qc_stage_with_no_named_reviewer_is_the_pool(self):
        for status in sorted(DESIGN_QC_STAGE_STATUSES):
            with self.subTest(status=status):
                _, assignment = self._site('PA-QCP-' + status[:6], status=status, qc=None)
                self.assertEqual(design_pending_at(assignment), PENDING_AT_QC_POOL)

    def test_03_the_two_qc_answers_are_different_strings(self):
        """Guards against a refactor that collapses the pool label back into "QC" — the
        tests above would both still pass if the two constants were equal."""
        self.assertNotEqual(PENDING_AT_QC, PENDING_AT_QC_POOL)

    def test_04_a_named_reviewer_changes_nothing_outside_the_qc_stages(self):
        """The QC name is read ONLY where Design QC owes the verdict. A reviewer named on
        a site that is still in design does not make the site theirs."""
        from .models import DESIGN_IN_DESIGN
        _, assignment = self._site('PA-QCX', status=DESIGN_IN_DESIGN, qc=self.reviewer)
        self.assertEqual(design_pending_at(assignment), PENDING_AT_DESIGNER)


# ===========================================================================
# (d) `arka_submitted` — ONE STATUS, TWO OPPOSITE BOTTLENECKS
# ===========================================================================

class PendingAtArkaSubmittedTests(PendingAtBase):
    """The status cannot answer this on its own, and design_pending_at() does not try:
    it delegates to design_metrics._classify(), the classifier the tender dashboard
    already uses, so the two screens cannot disagree about the same site.

    head_verdict='approved' at this status means BOTH Arka gates are done and the site is
    back with the designer for CAD and BOQ. Anything else means Design QC still owes the
    Arka verdict. (An Arka that QC has passed but the Head has not sits at
    `awaiting_head_arka` and never reaches this branch.)
    """

    def _arka_site(self, code, head_verdict, qc=None):
        _, assignment = self._site(code, status=DESIGN_ARKA_SUBMITTED, qc=qc)
        attempt = self._attempt(assignment)
        return assignment, self._arka(attempt, head_verdict=head_verdict)

    def test_01_head_approved_arka_is_back_with_the_designer(self):
        assignment, arka = self._arka_site('PA-ARK-A', ARKA_APPROVED)
        self.assertEqual(design_pending_at(assignment, arka), PENDING_AT_DESIGNER)

    def test_02_an_unapproved_arka_is_a_qc_stage(self):
        assignment, arka = self._arka_site('PA-ARK-P', ARKA_PENDING)
        self.assertEqual(design_pending_at(assignment, arka), PENDING_AT_QC_POOL)

    def test_03_an_unapproved_arka_names_the_assigned_reviewer(self):
        """The second branch is a full QC stage, pool rule and all — not a bare "QC"."""
        assignment, arka = self._arka_site('PA-ARK-Q', ARKA_PENDING, qc=self.reviewer)
        self.assertEqual(design_pending_at(assignment, arka), PENDING_AT_QC)

    def test_04_a_rejected_arka_is_also_the_qc_branch(self):
        """`anything else` means anything else. A head_verdict of 'rejected' is not
        'approved', so the site is not the designer's by this rule — it reaches the
        designer through the `arka_rejected` STATUS instead, which is a different row."""
        assignment, arka = self._arka_site('PA-ARK-R', ARKA_REJECTED)
        self.assertEqual(design_pending_at(assignment, arka), PENDING_AT_QC_POOL)

    def test_05_no_arka_row_falls_back_to_the_qc_branch(self):
        """A site at `arka_submitted` with no current Arka is a broken row, not an
        approved one. The fallback must be the cautious reading — somebody still owes a
        verdict — never "the designer is working on it"."""
        _, assignment = self._site('PA-ARK-N', status=DESIGN_ARKA_SUBMITTED)
        self.assertEqual(design_pending_at(assignment, None), PENDING_AT_QC_POOL)

    def test_06_the_dict_entry_is_the_unapproved_reading(self):
        """The static entry backs the branch that is reached without an Arka row."""
        self.assertEqual(DESIGN_PENDING_AT[DESIGN_ARKA_SUBMITTED], PENDING_AT_QC)


# ===========================================================================
# (e) A PENDING CHANGE REQUEST OVERRIDES EVERY STATUS
# ===========================================================================

class PendingAtChangeRequestTests(PendingAtBase):
    """Part 4.6 made a raised change request INERT: it writes no status and opens no
    attempt. The assignment therefore goes on reading whatever it read before while both
    review gates are suspended underneath it and the Design Head owes a triage verdict.

    A status-first answer would name a reviewer who cannot act, which is why this test
    sweeps the WHOLE choices list rather than one or two interesting statuses.
    """

    def test_01_a_pending_request_overrides_every_status_in_the_list(self):
        for index, (status, _label) in enumerate(DESIGN_ASSIGNMENT_STATUS_CHOICES):
            with self.subTest(status=status):
                _, assignment = self._site('PA-CR-%02d' % index, status=status)
                self.assertEqual(
                    design_pending_at(assignment, change_request_pending=True),
                    PENDING_AT_CHANGE_REQUEST)

    def test_02_it_overrides_released_in_particular(self):
        """Named on its own because `released` is the one status a reader is most likely
        to assume is terminal. A change request can be raised against a released site, and
        the Head still owes the verdict."""
        _, assignment = self._site('PA-CR-REL', status=DESIGN_RELEASED)
        self.assertEqual(design_pending_at(assignment), PENDING_AT_SCM)
        self.assertEqual(
            design_pending_at(assignment, change_request_pending=True),
            PENDING_AT_CHANGE_REQUEST)

    def test_03_it_outranks_the_arka_split_as_well(self):
        """The override is tested BEFORE the Arka branch, so a head-approved Arka does not
        get to answer "Designer" over a suspended gate."""
        _, assignment = self._site('PA-CR-ARK', status=DESIGN_ARKA_SUBMITTED)
        attempt = self._attempt(assignment)
        arka = self._arka(attempt, head_verdict=ARKA_APPROVED)
        self.assertEqual(design_pending_at(assignment, arka), PENDING_AT_DESIGNER)
        self.assertEqual(
            design_pending_at(assignment, arka, change_request_pending=True),
            PENDING_AT_CHANGE_REQUEST)

    def test_04_a_decided_request_does_not_override(self):
        """`resulting_attempt` is no longer a proxy for "resolved" (Part 4.6), so the flag
        the view computes reads verdict='pending' and nothing else. A rejected or accepted
        request is history and must not hold the column hostage."""
        _, assignment = self._site('PA-CR-DONE', status=DESIGN_IN_QC, qc=self.reviewer)
        attempt = self._attempt(assignment)
        for verdict, reason in ((CHANGE_REQUEST_ACCEPTED, ''),
                                (CHANGE_REQUEST_REJECTED, 'The current version stands.')):
            DesignChangeRequest.objects.create(
                attempt=attempt, requested_by=self.head, reason='Panel count changed',
                verdict=verdict, rejection_reason=reason, origin='pm')
        _, rows = self._site_rows()
        row = next(r for r in rows if r['site'].project_id == 'PA-CR-DONE')
        self.assertEqual(row['pending_at'], PENDING_AT_QC)

    def test_05_the_view_detects_a_pending_request_on_the_current_attempt(self):
        """End to end through design_head_sites(), because the flag is the view's to
        compute — the helper only believes what it is told."""
        _, assignment = self._site('PA-CR-LIVE', status=DESIGN_IN_QC, qc=self.reviewer)
        attempt = self._attempt(assignment)
        _, rows = self._site_rows()
        row = next(r for r in rows if r['site'].project_id == 'PA-CR-LIVE')
        self.assertEqual(row['pending_at'], PENDING_AT_QC)

        DesignChangeRequest.objects.create(
            attempt=attempt, requested_by=self.head, reason='Inverter rating changed',
            verdict=CHANGE_REQUEST_PENDING, origin='pm')
        _, rows = self._site_rows()
        row = next(r for r in rows if r['site'].project_id == 'PA-CR-LIVE')
        self.assertEqual(row['pending_at'], PENDING_AT_CHANGE_REQUEST)


# ===========================================================================
# (f) THE COST — the helper is free, and the view's budget grows by a constant
# ===========================================================================

class PendingAtQueryCostTests(PendingAtBase):

    def test_01_the_helper_issues_no_query_over_the_view_s_own_rows(self):
        """assertNumQueries(0) ACROSS EVERY ROW THE VIEW BUILT, not over one hand-made
        object, and over a spread of statuses so that every branch of the helper is walked
        — including the `arka_submitted` one, which reaches into design_metrics._classify()
        through a function-level import.

        WHAT THIS DOES AND DOES NOT PROVE. It proves the helper is pure: no branch of it
        touches the database, whatever it is handed. It does NOT prove the VIEW prefetched
        anything — the helper is called here with the assignment alone, and `qc_assigned_to`
        is read by its FK id, which is on the row already. The view's own prefetching is
        what test_02 measures, by slope; these two tests are the two halves of the claim
        and neither is sufficient alone.
        """
        self._site('PA-COST-1', status=DESIGN_IN_QC, qc=self.reviewer)
        self._site('PA-COST-2', status=DESIGN_SURVEY_RETURNED)
        _, assignment = self._site('PA-COST-3', status=DESIGN_ARKA_SUBMITTED)
        self._arka(self._attempt(assignment), head_verdict=ARKA_APPROVED)
        self._site('PA-COST-4', status=DESIGN_RELEASED)
        self._site('PA-COST-5', status=DESIGN_ARTIFACTS_UPLOADED, with_attempt=True)

        _, rows = self._site_rows()
        self.assertEqual(len(rows), 5)

        # Re-derive from the already-fetched rows. Every argument is read off the row the
        # view built, which is exactly what the template does.
        with self.assertNumQueries(0):
            for row in rows:
                design_pending_at(row['assignment'])

    def test_02_the_per_row_slope_is_the_pre_existing_three_and_no_more(self):
        """THE DELTA, NOT THE ABSOLUTE — and deliberately so.

        design_head_sites() has a PRE-EXISTING three-queries-per-row read of
        DueDateCommitment: _effective_commitment(), _pending_extension() and the revisions
        count, one each per site. Measured on the local database before this change, 26
        queries for SCMPILOT's 6 sites and 266 for MPUVNL's 86. It is recorded in
        docs/DESIGN_MODULE_DEFERRED.md and is not this prompt's to fix.

        So a flat "same count at 3 and at 15" would fail on work that predates these
        columns and say nothing at all about them. What IS this prompt's to hold is that
        the two new columns add a CONSTANT: the QC reviewer rides the existing
        select_related, and the nested Prefetch batches both the current Arka and the
        pending change requests across the whole table — two queries, whatever the row
        count. So the SLOPE must still be exactly the three the due-date reads produce.

        If somebody later adds a per-row read to this screen, this is what catches it; if
        somebody fixes the due-date N+1, this is the one number to change.
        """
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        self.client.login(username=self.head.user.username, password='x')
        url = reverse('design_head_sites', kwargs={'pk': self.program.pk})

        def measure(n_sites, prefix):
            for index in range(n_sites):
                self._site('%s-%02d' % (prefix, index), status=DESIGN_IN_QC,
                           with_attempt=True)
            self.client.get(url)      # warm
            with CaptureQueriesContext(connection) as ctx:
                response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            return len(ctx.captured_queries)

        at_three = measure(3, 'PA-SLOPE-A')
        at_fifteen = measure(12, 'PA-SLOPE-B')   # 3 + 12 = 15 sites on the screen

        slope = (at_fifteen - at_three) / 12.0
        self.assertEqual(
            slope, 3.0,
            'design_head_sites() costs %.2f queries per row (3 at %d sites, %d at 15). '
            'THREE is the pre-existing DueDateCommitment N+1 recorded in '
            'docs/DESIGN_MODULE_DEFERRED.md. Anything above it is a new per-row read on '
            'this screen.' % (slope, at_three, at_fifteen))


# ===========================================================================
# (g) THE TWO COLUMNS ON THE PAGE
# ===========================================================================

class PendingAtRenderTests(PendingAtBase):

    def test_01_both_headers_are_present_in_order(self):
        """Position is part of the brief: Pending at directly after Status, QC directly
        after Designer."""
        self._site('PA-HDR', status=DESIGN_IN_QC)
        response, _rows = self._site_rows()
        html = response.content.decode()
        for earlier, later in (('<th>Status</th>', '<th>Pending at</th>'),
                               ('<th>Pending at</th>', '<th>Designer</th>'),
                               ('<th>Designer</th>', '<th>QC</th>'),
                               ('<th>QC</th>', '<th>Due date</th>')):
            self.assertIn(earlier, html)
            self.assertIn(later, html)
            self.assertLess(html.index(earlier), html.index(later),
                            '%s must come before %s' % (earlier, later))

    def test_02_the_qc_column_shows_the_reviewer_s_display_name(self):
        self.reviewer.user.first_name = 'Asha'
        self.reviewer.user.last_name = 'Rao'
        self.reviewer.user.save()
        self._site('PA-QCNAME', status=DESIGN_IN_QC, qc=self.reviewer)
        response, _rows = self._site_rows()
        self.assertContains(response, 'Asha Rao')

    def test_03_an_unassigned_qc_reads_open_pool_not_a_dash(self):
        """An em dash would say nobody may review it, which is the opposite of what null
        means on this field."""
        self._site('PA-QCPOOL', status=DESIGN_IN_QC, qc=None)
        response, _rows = self._site_rows()
        self.assertContains(response, 'Open pool')

    def test_04_every_row_renders_a_pending_at_value(self):
        """An empty cell reads as "nobody is waiting on this", which is never true."""
        self._site('PA-R1', status=DESIGN_IN_QC, qc=self.reviewer)
        self._site('PA-R2', status=DESIGN_SURVEY_RETURNED)
        self._site('PA-R3', status=DESIGN_RELEASED)
        _response, rows = self._site_rows()
        for row in rows:
            self.assertTrue(row['pending_at'].strip(), row['site'].project_id)

    def test_05_a_site_with_no_design_assignment_renders_survey(self):
        """The row whose Status cell falls back to the hard-coded "Awaiting survey" badge.
        Both cells have to agree, and neither may be blank."""
        site = Project(
            project_id='PA-NOASSIGN', customer_name='PAClient',
            customer_phone='9876543210', site_address='1 Sun Rd', city='Delhi',
            project_type='OPEX', program=self.program, site_code='PA-NOASSIGN',
            dc_capacity_kw=Decimal('100.00'), status='Draft')
        site.save()
        self.assertFalse(DesignAssignment.objects.filter(project=site).exists())

        response, rows = self._site_rows()
        row = next(r for r in rows if r['site'].project_id == 'PA-NOASSIGN')
        self.assertIsNone(row['assignment'])
        self.assertEqual(row['pending_at'], PENDING_AT_SURVEY)
        self.assertContains(response, 'Awaiting survey')

    def test_06_the_status_badge_is_unchanged(self):
        """The brief says not to touch it. The Design Hold row still renders the warning
        badge and its display label, beside a neutral Pending-at badge and not instead
        of one."""
        self._site('PA-BADGE', status=DESIGN_SURVEY_RETURNED)
        response, _rows = self._site_rows()
        html = response.content.decode()
        self.assertIn('badge bg-warning text-dark', html)
        self.assertIn('Design Hold — survey inadequate', html)
        self.assertIn(PENDING_AT_ON_HOLD, html)

    def test_07_the_collapse_panels_span_the_widened_table(self):
        """Two more columns means every inline action panel's colspan moves with them, or
        the panels stop short of the Actions column and the table visibly steps in."""
        self._site('PA-SPAN', status=DESIGN_IN_QC)
        response, _rows = self._site_rows()
        html = response.content.decode()
        self.assertNotIn('colspan="7"', html)
        self.assertIn('colspan="9"', html)
