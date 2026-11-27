"""
DW-2a — whoever's turn it is may revise: one reviewer-turn rule.

WHY THIS FILE EXISTS
--------------------
The design team's top complaint was that QC kept changing a site after passing it: QC could
still revise the Arka and the CAD, correct the BOQ and override calculated figures at
`awaiting_head_qc`, because the four actions read three separate windows. Zuber's rulings
(26 Nov 2026), pinned here:

  * DW-R2 — QC may act during its own turn (Arka review through QC review); once QC passes
    the package only the Head may. One rule for the four actions:
        Arka revise    QC: arka_submitted (verdict pending), in_qc   Head: + awaiting_head_qc
        CAD revise     QC: in_qc                                     Head: + awaiting_head_qc
        BOQ correction QC: artifacts_uploaded, in_qc                 Head: + awaiting_head_qc
        Override       QC: artifacts_uploaded, in_qc                 Head: + awaiting_head_qc
  * answer 1 — at arka_submitted QC's turn covers the Arka revision only, while the Arka
    verdict is pending; answer 7 — the same for the Head;
  * answer 3 — BC-1's Head exception under a pending change request stays for BOQ
    correction; answer 1 (pre-flight) — overrides get no change-request exception;
  * answer 0 — a person with both authorities is the Head (W-5 Q6), stamped so;
  * answer 2 — the wording of a version revised before the QC verdict;
  * answers 4/5/8/9/10 — the Arka form on the Arka review screen, the note box before the
    Override button with a plain message beside the form, the QC-pool outsider's message,
    and the one-line note where QC's buttons were.

THE TABLE CASES ARE GENERATED from SPEC below — an independent literal copy of the ruling,
not the module's own table — for every design status, both roles and all four actions, so a
cell missing from (or added to) permissions.DESIGN_REVIEWER_TURN_WINDOWS fails here.
"""
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.contrib.messages import get_messages
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from .models import (
    ActivityLog, ArkaSubmission, BOQCorrection, BOQItem, BOQLineOverride, DesignAssignment,
    DesignAttempt, DesignChangeRequest, Project,
    ARKA_APPROVED, ARKA_PENDING, ARKA_REJECTED,
    CHANGE_REQUEST_ORIGIN_PM, CHANGE_REQUEST_ORIGIN_SCM, CHANGE_REQUEST_PENDING,
    CHANGE_REQUEST_WITH_PM,
    DESIGN_ARKA_SUBMITTED, DESIGN_ARTIFACTS_UPLOADED, DESIGN_AWAITING_HEAD_QC,
    DESIGN_AWAITING_PM_APPROVAL, DESIGN_IN_QC, DESIGN_RELEASED,
    OVERRIDE_SET_AS_HEAD, REVIEWER_ROLE_HEAD, REVIEWER_ROLE_QC,
)
from .permissions import (
    DESIGN_REVIEWER_TURN_WINDOWS,
    REVIEWER_ACTION_ACCB_EDIT,
    REVIEWER_ACTION_ARKA_REVISE, REVIEWER_ACTION_BOQ_CORRECT, REVIEWER_ACTION_BOQ_OVERRIDE,
    REVIEWER_ACTION_CAD_REVISE, REVIEWER_TURN_ARKA_DECIDED, REVIEWER_TURN_ARKA_DECIDED_MESSAGE,
    REVIEWER_TURN_HEAD, REVIEWER_TURN_QC, REVIEWER_TURN_QC_PASSED,
    REVIEWER_TURN_QC_PASSED_MESSAGE, REVIEWER_TURN_QC_PASSED_NOTE,
    BOQ_CORRECTION_CLOSED_CHANGE_REQUEST, design_reviewer_turn,
)
from .tests_boq_reviewer_override import ReviewerOverrideBase
from .tests_design_reviewer_cad_revisions import CadRevisionBase
from .views import REVIEWER_OVERRIDE_CHANGE_REQUEST_MESSAGE, REVIEWER_OVERRIDE_NOTE_REQUIRED_MESSAGE

D = Decimal
ARKA = REVIEWER_ACTION_ARKA_REVISE
CAD = REVIEWER_ACTION_CAD_REVISE
CORRECT = REVIEWER_ACTION_BOQ_CORRECT
OVERRIDE = REVIEWER_ACTION_BOQ_OVERRIDE
ACCB = REVIEWER_ACTION_ACCB_EDIT

#: The ruling as Zuber wrote it (DW-R2, answers 1-4), in model constants. Independent of
#: permissions.DESIGN_REVIEWER_TURN_WINDOWS on purpose: the generated cases below compare the
#: helper's answers against THIS, so a drifted table cell fails a test.
SPEC = {
    ARKA:     {'qc':   {DESIGN_ARKA_SUBMITTED, DESIGN_IN_QC},
               'head': {DESIGN_ARKA_SUBMITTED, DESIGN_IN_QC, DESIGN_AWAITING_HEAD_QC}},
    CAD:      {'qc':   {DESIGN_IN_QC},
               'head': {DESIGN_IN_QC, DESIGN_AWAITING_HEAD_QC}},
    CORRECT:  {'qc':   {DESIGN_ARTIFACTS_UPLOADED, DESIGN_IN_QC},
               'head': {DESIGN_ARTIFACTS_UPLOADED, DESIGN_IN_QC, DESIGN_AWAITING_HEAD_QC}},
    OVERRIDE: {'qc':   {DESIGN_ARTIFACTS_UPLOADED, DESIGN_IN_QC},
               'head': {DESIGN_ARTIFACTS_UPLOADED, DESIGN_IN_QC, DESIGN_AWAITING_HEAD_QC}},
    # DW-2b (DW-R3): the ACCB edit, add and remove — BOQ correction's cells, and (answer 3)
    # no change-request exception.
    ACCB:     {'qc':   {DESIGN_ARTIFACTS_UPLOADED, DESIGN_IN_QC},
               'head': {DESIGN_ARTIFACTS_UPLOADED, DESIGN_IN_QC, DESIGN_AWAITING_HEAD_QC}},
}

#: Every design status, from the model's own choices, so a new status is a new case.
ALL_STATUSES = [value for value, _label in
                DesignAssignment._meta.get_field('status').choices]


def _profile(username, role, **flags):
    user = User.objects.create_user(username=username, password='x')
    profile = user.profile
    profile.role = role
    profile.is_active = True
    for name, value in flags.items():
        setattr(profile, name, value)
    profile.save()
    return profile


class TurnBase(CadRevisionBase):
    """RevisionBase's package (attempt 1, Arka v1 approved by QC, CAD v1, BOQ stamped) with
    one more lever: the Arka verdict, which decides QC's turn at `arka_submitted`."""

    def _arka_pending(self):
        ArkaSubmission.objects.filter(pk=self.arka.pk).update(
            verdict=ARKA_PENDING, reviewed_by=None, reviewed_at=None)

    def _turn(self, who, action):
        return design_reviewer_turn(who.user, Project.objects.get(pk=self.site.pk), action)

    def _messages(self, response):
        return ' '.join(str(m) for m in get_messages(response.wsgi_request))


# ===========================================================================
# T1 — the table, every cell
# ===========================================================================

class TableTests(TurnBase):

    def test_the_table_is_the_ruling(self):
        """The module's table, literal for literal, against the ruling in model constants:
        a renamed status or a changed cell fails here first."""
        self.assertEqual({action: {role: set(cells) for role, cells in roles.items()}
                          for action, roles in DESIGN_REVIEWER_TURN_WINDOWS.items()}, SPEC)
        self.assertEqual(REVIEWER_TURN_QC, REVIEWER_ROLE_QC)
        self.assertEqual(REVIEWER_TURN_HEAD, REVIEWER_ROLE_HEAD)
        self.assertEqual(REVIEWER_TURN_HEAD, OVERRIDE_SET_AS_HEAD)

    def test_every_cell_for_qc_and_the_head(self):
        """Generated: action x role x every design status. Expected comes from SPEC only. At
        `arka_submitted` the Arka verdict is pending, the one state in which that status
        is in anyone's turn (the given-verdict case is the next test)."""
        self.assertGreaterEqual(len(ALL_STATUSES), 15)
        for status in ALL_STATUSES:
            self._set_status(status)
            if status == DESIGN_ARKA_SUBMITTED:
                self._arka_pending()
            else:
                ArkaSubmission.objects.filter(pk=self.arka.pk).update(
                    verdict=ARKA_APPROVED, reviewed_by=self.qc, reviewed_at=self.arka.reviewed_at)
            for action, roles in SPEC.items():
                for role, who in (('qc', self.qc), ('head', self.head)):
                    with self.subTest(status=status, action=action, role=role):
                        turn = self._turn(who, action)
                        self.assertEqual(turn.allowed, status in roles[role], turn.reason)
                        if turn.allowed:
                            self.assertEqual(turn.role, role)

    def test_qc_is_told_why_at_awaiting_head_qc(self):
        self._at_head()
        for action in SPEC:
            with self.subTest(action=action):
                turn = self._turn(self.qc, action)
                self.assertFalse(turn.allowed)
                self.assertEqual(turn.reason, REVIEWER_TURN_QC_PASSED)
                self.assertTrue(self._turn(self.head, action).allowed)

    def test_arka_submitted_after_the_verdict_is_nobodys_turn(self):
        """Answers 1 and 7: once QC has approved the Arka, arka_submitted is the designer's
        turn (CAD and BOQ outstanding) — for the Head too."""
        self._set_status(DESIGN_ARKA_SUBMITTED)
        for verdict in (ARKA_APPROVED, ARKA_REJECTED):
            ArkaSubmission.objects.filter(pk=self.arka.pk).update(
                verdict=verdict,
                rejection_reason='Rows clash' if verdict == ARKA_REJECTED else '')
            for who in (self.qc, self.head):
                with self.subTest(verdict=verdict, who=who.user.username):
                    turn = self._turn(who, ARKA)
                    self.assertFalse(turn.allowed)
                    self.assertEqual(turn.reason, REVIEWER_TURN_ARKA_DECIDED)
        self._arka_pending()
        for who in (self.qc, self.head):
            self.assertTrue(self._turn(who, ARKA).allowed)
            # Only the Arka: the package is not in yet.
            for action in (CAD, CORRECT, OVERRIDE, ACCB):
                self.assertFalse(self._turn(who, action).allowed)

    def _change_request(self, verdict):
        scm = verdict == CHANGE_REQUEST_WITH_PM
        return DesignChangeRequest.objects.create(
            attempt=self.attempt, requested_by=self.pm, reason='One pit short.',
            verdict=verdict, origin=CHANGE_REQUEST_ORIGIN_SCM if scm else CHANGE_REQUEST_ORIGIN_PM)

    def test_a_pending_change_request_opens_correction_for_the_head_only(self):
        """Answer 3: BC-1's exception stays for BOQ correction, at any status. Pre-flight
        answer 1: never for an override (nor a revision)."""
        self._change_request(CHANGE_REQUEST_PENDING)
        for status in (DESIGN_IN_QC, DESIGN_AWAITING_HEAD_QC, DESIGN_RELEASED):
            self._set_status(status)
            with self.subTest(status=status):
                self.assertTrue(self._turn(self.head, CORRECT).allowed)
                for action in (OVERRIDE, ARKA, CAD, ACCB):
                    self.assertFalse(self._turn(self.head, action).allowed, action)
                for action in SPEC:
                    self.assertFalse(self._turn(self.qc, action).allowed, action)
        self._set_status(DESIGN_IN_QC)
        self.assertEqual(self._turn(self.head, OVERRIDE).reason,
                         BOQ_CORRECTION_CLOSED_CHANGE_REQUEST)

    def test_a_request_with_the_pm_closes_every_action_for_everyone(self):
        self._change_request(CHANGE_REQUEST_WITH_PM)
        for action in SPEC:
            for who in (self.qc, self.head):
                with self.subTest(action=action, who=who.user.username):
                    self.assertFalse(self._turn(who, action).allowed)

    def test_the_designer_and_others_have_no_turn(self):
        self.designer.is_design_qc = True
        self.designer.save()
        for who in (self.designer, self.pm, self.other_designer):
            for action in SPEC:
                with self.subTest(who=who.user.username, action=action):
                    self.assertFalse(self._turn(who, action).allowed)

    def test_someone_with_both_authorities_is_the_head(self):
        """Answer 0: QC-first ordering saves a query, never the role."""
        both = _profile('both5', 'Design', is_design_qc=True, is_design_head=True)
        self._at_head()
        turn = self._turn(both, CORRECT)
        self.assertTrue(turn.allowed)            # the Head's cell, not QC's
        self._set_status(DESIGN_IN_QC)
        turn = self._turn(both, CORRECT)
        self.assertTrue(turn.allowed)
        self.assertEqual(turn.role, REVIEWER_TURN_HEAD)


# ===========================================================================
# T5 — the four actions through their real views
# ===========================================================================

class ActionViewTests(TurnBase):

    def _correct(self, who, quantity='11'):
        self._login(who)
        row = BOQItem.objects.get(boq__project=self.site)
        return self.client.post(reverse('boq_correct', kwargs={'project_id': self.site.project_id}),
                                {'action': 'set_quantity', 'item_id': str(row.pk),
                                 'quantity': quantity})

    def test_qc_is_refused_every_action_at_awaiting_head_qc_and_nothing_is_written(self):
        self._at_head()
        # Arka
        response = self._revise(self.qc)
        self.assertIn(REVIEWER_TURN_QC_PASSED_MESSAGE, self._messages(response))
        self._assert_nothing_written()
        # CAD — refused before anything is stored.
        response, mocks = self._cad(self.qc)
        self.assertIn(REVIEWER_TURN_QC_PASSED_MESSAGE, self._message(response))
        mocks.store.assert_not_called()
        self._assert_no_cad_written(mocks)
        # BOQ correction
        response = self._correct(self.qc)
        self.assertRedirects(response, reverse('boq_detail', kwargs={
            'project_id': self.site.project_id}), fetch_redirect_response=False)
        self.assertIn(REVIEWER_TURN_QC_PASSED_MESSAGE, self._messages(response))
        self.assertEqual(BOQItem.objects.get(boq__project=self.site).boq_quantity, D('10'))
        self.assertFalse(BOQCorrection.objects.exists())

    def test_the_head_may_take_each_action_at_awaiting_head_qc(self):
        self._at_head()
        self._revise(self.head)
        self.assertEqual(self._current_arka().version, 2)
        _response, mocks = self._cad(self.head)
        mocks.store.assert_called_once()
        self.assertEqual(self._current_cad().version, 2)
        self._correct(self.head)
        self.assertEqual(BOQItem.objects.get(boq__project=self.site).boq_quantity, D('11'))
        self.assertEqual(BOQCorrection.objects.get().corrected_by, self.head)

    def test_qc_revises_a_pending_arka_and_it_stays_pending(self):
        self._set_status(DESIGN_ARKA_SUBMITTED)
        self._arka_pending()
        response = self._revise(self.qc, capacity='118.00')
        self.assertRedirects(response, reverse('design_head_review', kwargs={
            'project_id': self.site.project_id}), fetch_redirect_response=False)
        self.assertIn('still waiting for the Design QC verdict', self._messages(response))
        new = self._current_arka()
        self.assertEqual((new.version, new.verdict, new.reviewed_by_id, new.reviewed_at),
                         (2, ARKA_PENDING, None, None))
        self.assertEqual(new.reviewer_role, REVIEWER_ROLE_QC)
        self.a.refresh_from_db()
        self.assertEqual(self.a.status, DESIGN_ARKA_SUBMITTED)

    def test_once_the_verdict_is_given_qc_may_not_revise_at_arka_submitted(self):
        self._set_status(DESIGN_ARKA_SUBMITTED)          # Arka v1 is approved
        response = self._revise(self.qc)
        self.assertIn(REVIEWER_TURN_ARKA_DECIDED_MESSAGE, self._messages(response))
        self._assert_nothing_written()

    def test_qc_may_not_revise_the_cad_at_artifacts_uploaded(self):
        self._set_status(DESIGN_ARTIFACTS_UPLOADED)
        response, mocks = self._cad(self.qc)
        self.assertIn('Revisions open once QC has started reviewing the package.',
                      self._message(response))
        mocks.store.assert_not_called()
        self._assert_no_cad_written(mocks)

    def test_qc_revises_then_passes_its_own_arka_and_the_head_still_approves(self):
        """DW-R2: QC may pass an Arka it revised itself; W-1's two-people check at the Head's
        final approval still passes (the Head did not give the Arka its QC verdict)."""
        DesignAttempt.objects.filter(pk=self.attempt.pk).update(qc_started_at=None)
        self._set_status(DESIGN_ARKA_SUBMITTED)
        self._arka_pending()
        self._revise(self.qc, capacity='118.00')
        self._login(self.qc)
        self._post('design_arka_approve')
        revised = self._current_arka()
        self.assertEqual((revised.version, revised.verdict, revised.reviewed_by_id),
                         (2, ARKA_APPROVED, self.qc.pk))
        self.a.refresh_from_db()
        self.assertEqual(self.a.status, DESIGN_ARTIFACTS_UPLOADED)   # CAD and BOQ were in
        self._post('design_qc_start')
        self._post('design_qc_pass')
        self.a.refresh_from_db()
        self.assertEqual(self.a.status, DESIGN_AWAITING_HEAD_QC)
        self._login(self.head)
        self._post('design_head_qc_pass')
        self.a.refresh_from_db()
        self.assertEqual(self.a.status, DESIGN_AWAITING_PM_APPROVAL)

    def test_the_head_may_correct_under_a_pending_request_in_review(self):
        """Answer 3: BC-1's exception still works for the Head's BOQ correction."""
        DesignChangeRequest.objects.create(
            attempt=self.attempt, requested_by=self.pm, reason='One pit short.',
            verdict=CHANGE_REQUEST_PENDING, origin=CHANGE_REQUEST_ORIGIN_PM)
        self._correct(self.head)
        self.assertEqual(BOQCorrection.objects.get().corrected_by, self.head)


class PoolOutsiderTests(TurnBase):
    """Answer 9: a Design QC flag holder on a site named to someone else gets a plain
    message, not a 403; everyone else outside the reviewers keeps the 403."""

    def setUp(self):
        super().setUp()
        self.named = _profile('named5', 'Design', is_design_qc=True)
        self.named.user.first_name, self.named.user.last_name = 'Nadia', 'Reviewer'
        self.named.user.save()
        DesignAssignment.objects.filter(pk=self.a.pk).update(qc_assigned_to=self.named)

    def test_the_pool_qc_is_told_whose_review_it_is_on_all_three_endpoints(self):
        expected = 'QC for this site is assigned to Nadia Reviewer.'
        self._login(self.qc)
        response = self.client.get(reverse('boq_correct', kwargs={
            'project_id': self.site.project_id}))
        self.assertRedirects(response, reverse('boq_detail', kwargs={
            'project_id': self.site.project_id}), fetch_redirect_response=False)
        self.assertIn(expected, self._messages(response))
        response = self._revise(self.qc)
        self.assertEqual(response.status_code, 302)
        self.assertIn(expected, self._messages(response))
        self._assert_nothing_written()
        response, mocks = self._cad(self.qc)
        self.assertEqual(response.status_code, 302)
        self.assertIn(expected, self._message(response))
        mocks.store.assert_not_called()

    def test_everyone_else_still_gets_the_403(self):
        for who in (self.pm, self.other_designer, self.designer):
            with self.subTest(who=who.user.username):
                self._login(who)
                response = self.client.post(reverse('boq_correct', kwargs={
                    'project_id': self.site.project_id}), {'action': 'set_quantity'})
                self.assertIn(response.status_code, (403,))
                self.assertEqual(self._revise(who).status_code, 403)

    def test_the_named_reviewer_still_acts(self):
        self.assertTrue(self._turn(self.named, CORRECT).allowed)


# ===========================================================================
# The override (B-2e) — needs the calculated-line fixture
# ===========================================================================

class OverrideTurnTests(ReviewerOverrideBase):

    def _page(self):
        return self.client.get(reverse('boq_correct', kwargs={
            'project_id': self.review.project_id}))

    def test_qc_is_refused_an_override_at_awaiting_head_qc(self):
        self._at(DESIGN_AWAITING_HEAD_QC)
        self._login(self.qc)
        response = self._set(self.logger, 4, 'too late for QC')
        self.assertIn(REVIEWER_TURN_QC_PASSED_MESSAGE, self._messages(response))
        self.assertFalse(BOQLineOverride.objects.exists())
        self.assertEqual(self._qty('FX-LOG', self.review), D(2))

    def test_the_head_overrides_there(self):
        self._at(DESIGN_AWAITING_HEAD_QC)
        self._login(self.head)
        self._set(self.logger, 4, 'carport loggers')
        self.assertEqual(BOQLineOverride.objects.get().set_as, OVERRIDE_SET_AS_HEAD)

    def test_the_head_may_not_override_under_a_pending_request_in_review(self):
        """Pre-flight answer 1: the Head loses overrides while a change request is pending;
        his BOQ correction stays open (BC-1)."""
        attempt = self.assignment.attempts.get(attempt_number=1)
        DesignChangeRequest.objects.create(
            attempt=attempt, requested_by=self.pm, reason='Logger count looks wrong.',
            verdict=CHANGE_REQUEST_PENDING, origin=CHANGE_REQUEST_ORIGIN_PM)
        self._login(self.head)
        page = self._page()
        self.assertEqual(page.status_code, 200)              # correction still open
        self.assertNotContains(page, 'boq-override-form')
        response = self._set(self.logger, 4, 'under the request')
        self.assertIn(REVIEWER_OVERRIDE_CHANGE_REQUEST_MESSAGE, self._messages(response))
        self.assertFalse(BOQLineOverride.objects.exists())
        self.assertEqual(self._qty('FX-LOG', self.review), D(2))

    def test_someone_with_both_authorities_is_stamped_as_the_head(self):
        """Answer 0: a dual-authority reviewer at in_qc (QC's own cell) is still 'head'."""
        both = _profile('fxboth', 'Design', is_design_qc=True, is_design_head=True)
        self._login(both)
        self._set(self.logger, 4, 'both hats')
        self.assertEqual(BOQLineOverride.objects.get().set_as, OVERRIDE_SET_AS_HEAD)
        self.assertIn('(Head) overrode FX-LOG', ActivityLog.objects.get(
            action_code='boq_formula_override_set').action)

    def test_an_empty_note_on_a_missing_calculated_item_is_answered_beside_its_form(self):
        """Answer 8 on the E5 form (a calculated item with no line): HTTP 200, the message
        beside that form, the block open, the typed quantity kept, nothing written."""
        quiet = self._no_inverter_site()
        self._login(self.qc)
        response = self._create('FX-LOG', 3, '', site=quiet)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._messages(response), [])
        page = response.content.decode()
        self.assertIn('id="boqCalcAbsent" open', page)
        form = page[page.index(f'name="master_id" value="{self.items["FX-LOG"].pk}"'):]
        form = form[:form.index('</form>')]
        self.assertIn(REVIEWER_OVERRIDE_NOTE_REQUIRED_MESSAGE, form)
        self.assertIn('value="3"', form)
        self.assertEqual(self._lines('FX-LOG', quiet), [])
        self.assertFalse(BOQLineOverride.objects.exists())

    def test_the_note_box_comes_before_the_button_and_is_not_required(self):
        """Answer 5: the likely cause of the 1613 "inactive" override — a required note
        below the button — is gone."""
        self._login(self.qc)
        page = self._page().content.decode()
        form = page[page.index(f'name="line_id" value="{self.logger.pk}"'):]
        form = form[:form.index('</form>')]
        self.assertLess(form.index('Note (required)'), form.index('name="quantity"'))
        self.assertLess(form.index('name="note"'), form.index('type="submit"'))
        note_input = form[form.index('name="note"'):]
        note_input = note_input[:note_input.index('>')]
        self.assertNotIn('required', note_input)


# ===========================================================================
# T3 — the screens
# ===========================================================================

class ScreenTests(TurnBase):

    def _get(self, who, name):
        self._login(who)
        response = self.client.get(reverse(name, kwargs={'project_id': self.site.project_id}))
        self.assertEqual(response.status_code, 200)
        return response

    def _urls(self):
        pid = self.site.project_id
        return {name: reverse(name, kwargs={'project_id': pid})
                for name in ('design_arka_revise', 'design_cad_revise', 'boq_correct')}

    def test_after_the_qc_pass_qc_sees_no_buttons_and_the_note(self):
        self._at_head()
        html = self._get(self.qc, 'design_qc_review').content.decode()
        for url in self._urls().values():
            self.assertNotIn(url, html)
        self.assertEqual(html.count(REVIEWER_TURN_QC_PASSED_NOTE), 2)   # package + BOQ card
        detail = self._get(self.qc, 'boq_detail').content.decode()
        self.assertNotIn(self._urls()['boq_correct'], detail)
        self.assertIn(REVIEWER_TURN_QC_PASSED_NOTE, detail)

    def test_the_head_sees_them_and_no_note(self):
        self._at_head()
        html = self._get(self.head, 'design_qc_review').content.decode()
        for url in self._urls().values():
            self.assertIn(url, html)
        self.assertNotIn(REVIEWER_TURN_QC_PASSED_NOTE, html)
        detail = self._get(self.head, 'boq_detail').content.decode()
        self.assertIn(self._urls()['boq_correct'], detail)
        self.assertNotIn(REVIEWER_TURN_QC_PASSED_NOTE, detail)

    def test_the_arka_review_screen_offers_the_revision_while_the_verdict_is_pending(self):
        self._set_status(DESIGN_ARKA_SUBMITTED)
        self._arka_pending()
        for who in (self.qc, self.head):
            with self.subTest(who=who.user.username):
                response = self._get(who, 'design_head_review')
                self.assertTrue(response.context['can_revise_arka'])
                self.assertContains(response, self._urls()['design_arka_revise'])
                self.assertContains(response, 'It still needs the Design QC verdict')
        # The package screen shows only the Arka form: the CAD and BOQ are not in yet.
        html = self._get(self.qc, 'design_qc_review').content.decode()
        self.assertIn(self._urls()['design_arka_revise'], html)
        self.assertNotIn(self._urls()['design_cad_revise'], html)

    def test_after_the_verdict_the_arka_review_screen_says_whose_turn_it_is(self):
        self._set_status(DESIGN_ARKA_SUBMITTED)          # Arka v1 approved
        for who in (self.qc, self.head):
            with self.subTest(who=who.user.username):
                response = self._get(who, 'design_head_review')
                self.assertFalse(response.context['can_revise_arka'])
                self.assertNotContains(response, self._urls()['design_arka_revise'])
                self.assertContains(response, REVIEWER_TURN_ARKA_DECIDED_MESSAGE)


# ===========================================================================
# Answer 2 — the wording of a revised Arka
# ===========================================================================

class WordingTests(TurnBase):

    def _revised(self, **fields):
        return ArkaSubmission.objects.create(
            attempt=self.attempt, version=9, capacity_kw=D('118'), arka_link='https://x.example',
            submitted_by=self.qc, reviewer_role=REVIEWER_ROLE_QC, revision_reason='Tank clash',
            designer_at_revision=self.designer, is_current=False, **fields)

    def test_a_pending_pre_verdict_revision(self):
        arka = self._revised(verdict=ARKA_PENDING)
        self.assertEqual(arka.revision_approval_text,
                         'Revised by qc5 (QC) before the QC verdict: Tank clash')

    def test_a_pre_verdict_revision_approved_afterwards(self):
        arka = self._revised(verdict=ARKA_PENDING)
        ArkaSubmission.objects.filter(pk=arka.pk).update(
            verdict=ARKA_APPROVED, reviewed_by=self.qc,
            reviewed_at=arka.submitted_at + timezone.timedelta(minutes=5))
        arka.refresh_from_db()
        self.assertEqual(arka.revision_approval_text,
                         'Approved by Design QC (qc5) after the revision; revised by qc5 (QC): '
                         'Tank clash')

    def test_a_revision_during_review_keeps_the_copied_approval(self):
        arka = self._revised(verdict=ARKA_APPROVED, reviewed_by=self.qc,
                             reviewed_at=timezone.now() - timezone.timedelta(days=1))
        self.assertEqual(arka.revision_approval_text,
                         'QC approval by qc5 kept; revised by qc5 (QC): Tank clash')


# ===========================================================================
# P10 — query pins
# ===========================================================================

def _n(client, url):
    client.get(url)                     # warm the session and the content-type cache
    with CaptureQueriesContext(connection) as ctx:
        response = client.get(url)
    return len(ctx), response.status_code


#: Measured at the DW-2a build on TurnBase's fixture (second of two identical GETs).
#: Arka review screen at arka_submitted, before (d6953b3) -> after:
#:   verdict pending   QC 21 -> 23, Head 17 -> 19   (+2: open change requests, group lock —
#:                                                    the form's turn; answer 5 accepted)
#:   verdict approved  QC 21 -> 21, Head 19 -> 19   (+0: the turn closes before any read)
ARKA_REVIEW_PINS = {('pending', 'qc'): 23, ('pending', 'head'): 19,
                    ('approved', 'qc'): 21, ('approved', 'head'): 19}
#: The package review and correction screens at in_qc / awaiting_head_qc. One turn
#: evaluation replaced user_can_correct_boq() + user_may_revise_design(), so the review
#: screen is cheaper; the correction screen is unchanged (d6953b3: QC 22, Head 20).
PACKAGE_PINS = {('design_qc_review', 'in_qc', 'qc'): 36,
                ('design_qc_review', 'in_qc', 'head'): 33,
                ('design_qc_review', 'awaiting_head_qc', 'qc'): 36,
                ('design_qc_review', 'awaiting_head_qc', 'head'): 35,
                # DW-8: 23 — "← My queue" asks Head authority (the deputy lookup) for this
                # fixture's open-pool QC reviewer.
                ('boq_correct', 'in_qc', 'qc'): 23,
                ('boq_correct', 'in_qc', 'head'): 20,
                ('boq_correct', 'awaiting_head_qc', 'head'): 20}


class QueryPinTests(TurnBase):

    def test_the_arka_review_screen(self):
        url = reverse('design_head_review', kwargs={'project_id': self.site.project_id})
        counts = {}
        for verdict in ('pending', 'approved'):
            if verdict == 'pending':
                self._arka_pending()
            else:
                ArkaSubmission.objects.filter(pk=self.arka.pk).update(
                    verdict=ARKA_APPROVED, reviewed_by=self.qc, reviewed_at=self.arka.reviewed_at)
            self._set_status(DESIGN_ARKA_SUBMITTED)
            for role, who in (('qc', self.qc), ('head', self.head)):
                self._login(who)
                count, status = _n(self.client, url)
                self.assertEqual(status, 200)
                counts[(verdict, role)] = count
        self.assertEqual(counts, ARKA_REVIEW_PINS)

    def test_the_package_review_and_correction_screens(self):
        counts = {}
        for status in (DESIGN_IN_QC, DESIGN_AWAITING_HEAD_QC):
            if status == DESIGN_AWAITING_HEAD_QC:
                self._at_head()
            for name in ('design_qc_review', 'boq_correct'):
                for role, who in (('qc', self.qc), ('head', self.head)):
                    if (name, status, role) not in PACKAGE_PINS:
                        continue       # QC's correction screen there is a redirect now
                    self._login(who)
                    url = reverse(name, kwargs={'project_id': self.site.project_id})
                    count, code = _n(self.client, url)
                    self.assertEqual(code, 200)
                    counts[(name, status, role)] = count
        self.assertEqual(counts, PACKAGE_PINS)
