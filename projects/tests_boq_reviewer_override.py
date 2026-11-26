"""
B-2e — QC and the Head may override a calculated BOQ figure (Zuber, 24 Nov 2026).

What this file pins:

  * E1/E2: on the correction screen, QC and the Head set, replace and clear an override on
    a calculated line (`override_set` / `override_clear`, the line named by `line_id`),
    through the designer's override logic (views._boq_line_override). A blank note or a
    missing quantity is refused and nothing is saved.
  * Q3: only while the design is in artifacts_uploaded / in_qc / awaiting_head_qc. The Head
    under a pending change request on a released site may still correct the BOQ, but not
    override a calculated figure. Outside the correction window it is refused as before.
  * E3: NOT COUNTED. No BOQCorrection, the reviewer-corrections metric is unchanged, and a
    change request's `corrected` evidence (_uncited_corrections_since) sees nothing.
    Downstream lines follow, each with its own BOQRecalculation (override_set /
    override_cleared, actor = the reviewer, no driving correction).
  * Q2: the reviewer path never deletes a line: a line that works out to 0 is set to 0.
  * E5: an override on a calculated item with no line creates the line.
  * E4: a reviewer may replace a designer's override; after a fail-back the designer may
    replace or clear a reviewer's.
  * E6: the plain quantity box stays refused for a calculated line (D2's message).
  * Q1/Q6: the override records the capacity it was set in (set_as) and every marker —
    picker, correction screen, review panel — says who set it.
  * Q5: the activity entry reads "Name (QC) overrode ..." and names a replaced override.
  * Query counts: the three reviewer POSTs pinned, at or below set_quantity's 34 on the
    5-rule fixture; the screens cost nothing extra for showing who set an override.
"""
from datetime import timedelta
from decimal import Decimal

from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from .boq_formulas import FORMULA_REVIEWER_MESSAGE, override_label
from .design_metrics import reviewer_corrections
from .design_views import _open_next_attempt, _uncited_corrections_since
from .models import (
    ActivityLog, BOQCorrection, BOQLineOverride, BOQRecalculation, DesignChangeRequest,
    ATTEMPT_REASON_QC_FAILED, CHANGE_REQUEST_ORIGIN_PM, CHANGE_REQUEST_PENDING,
    DESIGN_ARTIFACTS_UPLOADED, DESIGN_AWAITING_HEAD_QC, DESIGN_AWAITING_PM_APPROVAL,
    DESIGN_RELEASED, FORMULA_SOURCE_OVERRIDE_CLEARED, FORMULA_SOURCE_OVERRIDE_SET,
    OVERRIDE_SET_AS_DESIGNER, OVERRIDE_SET_AS_HEAD, OVERRIDE_SET_AS_QC, Project,
)
from .permissions import user_can_correct_boq
from .tests_boq_formula_display import (WITH_RULES_EXTRA, DisplayBase,
                                        screens_expected)
from .views import (REVIEWER_OVERRIDE_CLOSED_MESSAGE, REVIEWER_OVERRIDE_NOTE_REQUIRED_MESSAGE,
                    boq_override_set_log_text)

D = Decimal

NOTHING_SAVED = 'Nothing was saved.'

#: set_quantity on the correction screen, measured on 84bbb98 (B-2e pre-flight) with
#: DisplayBase's 5-rule fixture, no redirect followed. The reviewer override must not cost
#: more than the reviewer's other write.
SET_QUANTITY_5_RULES = 34

#: The reviewer override POSTs, measured at build on the same fixture (no redirect
#: followed): a set on an existing calculated line as the sheet's first write, a clear of
#: it, and a create (E5) on a sheet whose rule gives 0. Pinned so a later change that adds
#: a query per line or per rule shows up here.
REVIEWER_POSTS = {'set': 34, 'clear': 28, 'create': 33}


class ReviewerOverrideBase(DisplayBase):
    """A site in Design QC with its BOQ stamped and three rules applied by hand:

        FX-EARTH = 5 m x arresters      2 FX-LA1           -> 10
        FX-LOG   = inverters / 10, up   12 FX-INV1          -> 2
        FX-RS485 = 5 m x FX-LOG                             -> 10
    """

    def setUp(self):
        super().setUp()
        self._rules()
        self.review = self._review_site()
        self.assignment = self.review.design_assignment
        for code, quantity in [('FX-LA1', 2), ('FX-EARTH', 10), ('FX-INV1', 12),
                               ('FX-LOG', 2), ('FX-RS485', 10), ('FX-PANEL', 10)]:
            self._line(code, quantity, site=self.review)
        self.logger = self._lines('FX-LOG', self.review)[0]

    # ── helpers ─────────────────────────────────────────────────────────────

    def _at(self, status):
        self.assignment.status = status
        self.assignment.save(update_fields=['status'])

    def _set(self, line, quantity, note, site=None):
        return self._correct(site or self.review, 'override_set', line_id=str(line.pk),
                             quantity=str(quantity), note=note)

    def _clear(self, line, site=None):
        return self._correct(site or self.review, 'override_clear', line_id=str(line.pk))

    def _create(self, code, quantity, note, site=None):
        return self._correct(site or self.review, 'override_set',
                             master_id=str(self.items[code].pk), quantity=str(quantity),
                             note=note)

    def _logs(self, code):
        return list(ActivityLog.objects.filter(action_code=code).order_by('pk')
                    .values_list('action', flat=True))

    def _no_inverter_site(self, code='FX-QZ'):
        """A review site with no inverters: FX-LOG (and so FX-RS485) works out to 0."""
        site = self._review_site(code)
        for item, quantity in [('FX-LA1', 2), ('FX-EARTH', 10)]:
            self._line(item, quantity, site=site)
        return site


# ===========================================================================
# T1 — set, replace and clear, by QC and by the Head
# ===========================================================================

class SetTests(ReviewerOverrideBase):

    def test_qc_sets_an_override_and_the_lines_that_follow_it_move(self):
        self._login(self.qc)
        response = self._set(self.logger, 4, 'two roofs, two loggers each')
        self.assertIn('FX-LOG set to 4 by hand. It will not recalculate until the override '
                      'is cleared.', self._messages(response))
        self.assertEqual((self._qty('FX-LOG', self.review), self._qty('FX-RS485', self.review)),
                         (D(4), D(20)))
        override = BOQLineOverride.objects.get()
        self.assertEqual((override.line, override.note, override.set_by, override.set_as),
                         (self.logger, 'two roofs, two loggers each', self.qc,
                          OVERRIDE_SET_AS_QC))
        # The downstream line's own record, and the overridden line's (B-2b.1 Q3).
        follower = BOQRecalculation.objects.get(item_code='FX-RS485')
        self.assertEqual((follower.quantity_before, follower.quantity_after, follower.source,
                          follower.actor, follower.driving_correction),
                         (D(10), D(20), FORMULA_SOURCE_OVERRIDE_SET, self.qc, None))
        own = BOQRecalculation.objects.get(item_code='FX-LOG')
        self.assertEqual((own.quantity_before, own.quantity_after, own.source, own.actor),
                         (D(2), D(4), FORMULA_SOURCE_OVERRIDE_SET, self.qc))

    def test_the_activity_entry_names_qc(self):
        self._login(self.qc)
        self._set(self.logger, 4, 'two roofs')
        log = ActivityLog.objects.get(action_code='boq_formula_override_set')
        self.assertEqual(log.action, 'Fxqc (QC) overrode FX-LOG on FX-QC: 2 → 4 (two roofs)')
        self.assertEqual((log.entity_type, log.entity_id), ('BOQItem', self.logger.pk))

    def test_the_head_sets_one_stamped_as_the_head(self):
        self._at(DESIGN_AWAITING_HEAD_QC)
        self._login(self.head)
        self._set(self.logger, 3, 'third logger for the carport')
        self.assertEqual(BOQLineOverride.objects.get().set_as, OVERRIDE_SET_AS_HEAD)
        self.assertEqual(self._logs('boq_formula_override_set'),
                         ['Fxhead (Head) overrode FX-LOG on FX-QC: 2 → 3 '
                          '(third logger for the carport)'])

    def test_it_is_open_on_a_handed_in_package_too(self):
        self._at(DESIGN_ARTIFACTS_UPLOADED)
        self._login(self.qc)
        self._set(self.logger, 4, 'two roofs')
        self.assertEqual(self._qty('FX-LOG', self.review), D(4))

    def test_a_blank_note_is_refused_and_nothing_is_saved(self):
        self._login(self.qc)
        response = self._set(self.logger, 4, '   ')
        # DW-2a (answer 8): answered on the page, beside the form it came from, with the
        # typed quantity kept — HTTP 200, no redirect and no banner at the top.
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.redirect_chain, [])
        self.assertEqual(self._messages(response), [])
        self.assertIn(NOTHING_SAVED, REVIEWER_OVERRIDE_NOTE_REQUIRED_MESSAGE)
        page = response.content.decode()
        form = page[page.index(f'name="line_id" value="{self.logger.pk}"'):]
        form = form[:form.index('</form>')]
        self.assertIn(REVIEWER_OVERRIDE_NOTE_REQUIRED_MESSAGE, form)
        self.assertIn('name="quantity" required', form)
        self.assertIn('value="4"', form)
        self.assertEqual(self._qty('FX-LOG', self.review), D(2))
        self.assertFalse(BOQLineOverride.objects.exists())
        self.assertFalse(BOQRecalculation.objects.exists())
        self.assertFalse(ActivityLog.objects.filter(
            action_code='boq_formula_override_set').exists())

    def test_a_missing_quantity_is_refused(self):
        self._login(self.qc)
        response = self._set(self.logger, '', 'two roofs')
        self.assertIn('Enter the quantity this line should carry.', self._messages(response))
        self.assertFalse(BOQLineOverride.objects.exists())

    def _set_replace_and_clear(self, profile, set_as, tag):
        self._login(profile)
        self._set(self.logger, 4, 'first')
        self._set(self.logger, 5, 'second')
        override = BOQLineOverride.objects.get()
        self.assertEqual((override.note, override.set_by, override.set_as),
                         ('second', profile, set_as))
        self.assertEqual(self._qty('FX-RS485', self.review), D(25))
        name = profile.user.first_name
        self.assertEqual(self._logs('boq_formula_override_set')[-1],
                         f"{name}{tag} overrode FX-LOG on FX-QC: 4 → 5 (second); "
                         f"replaced {name}{tag}'s 4 (first)")

        response = self._clear(self.logger)
        self.assertIn('Override on FX-LOG cleared; it is calculated again (2).',
                      self._messages(response))
        self.assertFalse(BOQLineOverride.objects.exists())
        self.assertEqual((self._qty('FX-LOG', self.review), self._qty('FX-RS485', self.review)),
                         (D(2), D(10)))
        cleared = BOQRecalculation.objects.get(item_code='FX-LOG',
                                               source=FORMULA_SOURCE_OVERRIDE_CLEARED)
        self.assertEqual((cleared.quantity_before, cleared.quantity_after, cleared.actor),
                         (D(5), D(2), profile))
        self.assertEqual(self._logs('boq_formula_override_cleared'),
                         [f'{name}{tag} cleared the override on FX-LOG on FX-QC '
                          f'(calculated: 2)'])

    def test_qc_can_set_replace_and_clear(self):
        self._set_replace_and_clear(self.qc, OVERRIDE_SET_AS_QC, ' (QC)')

    def test_the_head_can_set_replace_and_clear(self):
        self._at(DESIGN_AWAITING_HEAD_QC)
        self._set_replace_and_clear(self.head, OVERRIDE_SET_AS_HEAD, ' (Head)')

    def test_clearing_a_line_with_no_override_changes_nothing(self):
        self._login(self.qc)
        response = self._clear(self.logger)
        self.assertIn('FX-LOG has no override — nothing changed.', self._messages(response))
        self.assertFalse(BOQRecalculation.objects.exists())


# ===========================================================================
# T2 — not counted against the designer (E3)
# ===========================================================================

class NotCountedTests(ReviewerOverrideBase):

    def test_no_correction_is_recorded_and_no_counter_moves(self):
        since = timezone.now()
        counted_before = reviewer_corrections().count()
        self._login(self.qc)
        self._set(self.logger, 4, 'qc figure')
        self._at(DESIGN_AWAITING_HEAD_QC)
        self._login(self.head)
        self._set(self.logger, 5, 'head figure')
        self._clear(self.logger)
        self.assertEqual(BOQCorrection.objects.count(), 0)
        self.assertEqual(reviewer_corrections().count(), counted_before)
        # The Head's `corrected` verdict reads exactly this; an override is no evidence.
        self.assertEqual(_uncited_corrections_since(self.review, since), [])
        # Every record the overrides wrote is a recalculation with no driving correction.
        self.assertTrue(BOQRecalculation.objects.exists())
        self.assertFalse(BOQRecalculation.objects.filter(
            driving_correction__isnull=False).exists())

    def test_an_ordinary_correction_still_counts(self):
        """The control: the metric this file says does not move does move for a
        correction, so the assertion above is not vacuous."""
        counted_before = reviewer_corrections().count()
        self._login(self.qc)
        panel = self._lines('FX-PANEL', self.review)[0]
        self._correct(self.review, 'set_quantity', item_id=str(panel.pk), quantity='11')
        self.assertEqual(reviewer_corrections().count(), counted_before + 1)


# ===========================================================================
# Q2 and E5 — zero is kept, and a missing calculated line can be created
# ===========================================================================

class CreateAndZeroTests(ReviewerOverrideBase):

    def setUp(self):
        super().setUp()
        self.quiet = self._no_inverter_site()

    def test_a_reviewer_override_creates_a_missing_calculated_line(self):
        self._login(self.qc)
        response = self._create('FX-LOG', 1, 'logger for the remote meter', site=self.quiet)
        self.assertIn('FX-LOG added at 1 by hand. It will not recalculate until the override '
                      'is cleared.', self._messages(response))
        line = self._lines('FX-LOG', self.quiet)[0]
        self.assertEqual((line.boq_quantity, line.formula_override.set_as,
                          line.formula_override.set_by),
                         (D(1), OVERRIDE_SET_AS_QC, self.qc))
        # The line that follows it is created too, with its own record.
        self.assertEqual(self._qty('FX-RS485', self.quiet), D(5))
        own = BOQRecalculation.objects.get(item_code='FX-LOG')
        self.assertEqual((own.quantity_before, own.quantity_after, own.source),
                         (None, D(1), FORMULA_SOURCE_OVERRIDE_SET))
        self.assertEqual(self._logs('boq_formula_override_set'),
                         ['Fxqc (QC) overrode FX-LOG on FX-QZ: not on the BOQ → 1 '
                          '(logger for the remote meter)'])
        self.assertEqual(BOQCorrection.objects.count(), 0)

    def test_clearing_to_zero_keeps_the_lines_at_zero(self):
        self._login(self.qc)
        self._create('FX-LOG', 1, 'logger for the remote meter', site=self.quiet)
        log_line = self._lines('FX-LOG', self.quiet)[0]
        response = self._clear(log_line, site=self.quiet)
        self.assertIn("Override on FX-LOG cleared. It works out to 0, so the line is set to "
                      "0; the designer's next save removes it.", self._messages(response))
        # Q2: never deleted on this screen — the line and its follower stay, at 0.
        self.assertEqual((self._qty('FX-LOG', self.quiet), self._qty('FX-RS485', self.quiet)),
                         (D(0), D(0)))

    def test_the_screen_lists_calculated_items_with_no_line_with_an_override_form(self):
        self._login(self.qc)
        response = self.client.get(reverse('boq_correct', kwargs={
            'project_id': self.quiet.project_id}))
        self.assertContains(response, 'id="boqCalcAbsent"')
        self.assertContains(response, f'name="master_id" value="{self.items["FX-LOG"].pk}"')
        absent = {row['code'] for row in response.context['calculated_absent']}
        self.assertEqual(absent, {'FX-LOG', 'FX-RS485'})

    def test_an_item_that_is_not_calculated_cannot_be_created_this_way(self):
        self._login(self.qc)
        response = self._create('FX-PANEL', 5, 'x', site=self.quiet)
        self.assertIn('That item is not calculated, so there is nothing to override. Add it on '
                      'the BOQ.', self._messages(response))
        self.assertEqual(self._lines('FX-PANEL', self.quiet), [])


# ===========================================================================
# The gate: who, and when (Q3)
# ===========================================================================

class GateTests(ReviewerOverrideBase):

    def test_past_review_it_is_refused_like_any_correction(self):
        self._at(DESIGN_AWAITING_PM_APPROVAL)
        for profile in (self.qc, self.head):
            with self.subTest(user=profile.user.username):
                self._login(profile)
                response = self._set(self.logger, 4, 'too late')
                self.assertIn('This design has moved past review. Raise a change request '
                              'instead.', self._messages(response))
                self.assertFalse(BOQLineOverride.objects.exists())

    def test_the_head_under_a_pending_change_request_on_a_released_site_is_refused(self):
        self._at(DESIGN_RELEASED)
        attempt = self.assignment.attempts.get(attempt_number=1)
        DesignChangeRequest.objects.create(
            attempt=attempt, requested_by=self.pm, reason='Logger count looks wrong.',
            verdict=CHANGE_REQUEST_PENDING, origin=CHANGE_REQUEST_ORIGIN_PM)
        # The correction itself is open to him (BC-1 branch 2)...
        self.assertTrue(user_can_correct_boq(self.head.user,
                                             Project.objects.get(pk=self.review.pk)))
        self._login(self.head)
        page = self.client.get(reverse('boq_correct', kwargs={
            'project_id': self.review.project_id}))
        self.assertEqual(page.status_code, 200)
        # ...but no override form is offered, and a crafted POST is refused (Q3).
        self.assertNotContains(page, 'boq-override-form')
        response = self._set(self.logger, 4, 'under the change request')
        self.assertIn(REVIEWER_OVERRIDE_CLOSED_MESSAGE, self._messages(response))
        self.assertFalse(BOQLineOverride.objects.exists())
        self.assertEqual(self._qty('FX-LOG', self.review), D(2))
        # A clear is refused the same way.
        BOQLineOverride.objects.create(line=self.logger, note='designer', set_by=self.designer)
        response = self._clear(self.logger)
        self.assertIn(REVIEWER_OVERRIDE_CLOSED_MESSAGE, self._messages(response))
        self.assertTrue(BOQLineOverride.objects.exists())

    def test_the_sites_designer_and_others_are_forbidden(self):
        for profile in (self.designer, self.pm):
            with self.subTest(user=profile.user.username):
                self._login(profile)
                response = self.client.post(
                    reverse('boq_correct', kwargs={'project_id': self.review.project_id}),
                    {'action': 'override_set', 'line_id': str(self.logger.pk),
                     'quantity': '4', 'note': 'x'})
                self.assertEqual(response.status_code, 403)
        self.assertFalse(BOQLineOverride.objects.exists())

    def test_the_plain_quantity_box_stays_refused_for_a_calculated_line(self):
        self._login(self.qc)
        response = self._correct(self.review, 'set_quantity', item_id=str(self.logger.pk),
                                 quantity='4')
        self.assertIn(FORMULA_REVIEWER_MESSAGE, self._messages(response))
        BOQLineOverride.objects.create(line=self.logger, note='n', set_by=self.qc,
                                       set_as=OVERRIDE_SET_AS_QC)
        response = self._correct(self.review, 'set_quantity', item_id=str(self.logger.pk),
                                 quantity='4')
        self.assertIn(FORMULA_REVIEWER_MESSAGE, self._messages(response))
        self.assertEqual(self._qty('FX-LOG', self.review), D(2))

    def test_a_line_that_is_not_calculated_cannot_be_overridden(self):
        self._login(self.qc)
        panel = self._lines('FX-PANEL', self.review)[0]
        response = self._set(panel, 4, 'x')
        self.assertIn('FX-PANEL is not a calculated line, so there is nothing to override. '
                      'Change its quantity on the BOQ.', self._messages(response))
        self.assertFalse(BOQLineOverride.objects.exists())

    def test_a_line_from_another_sites_boq_is_a_404(self):
        other = self._line('FX-LOG', 2)          # on self.site, not the review site
        self._login(self.qc)
        response = self.client.post(
            reverse('boq_correct', kwargs={'project_id': self.review.project_id}),
            {'action': 'override_set', 'line_id': str(other.pk), 'quantity': '4',
             'note': 'x'})
        self.assertEqual(response.status_code, 404)

    def test_the_form_on_a_calculated_row_never_carries_set_quantitys_field(self):
        self._login(self.qc)
        page = self.client.get(reverse('boq_correct', kwargs={
            'project_id': self.review.project_id}))
        self.assertContains(page, f'name="line_id" value="{self.logger.pk}"')
        self.assertNotContains(page, f'name="item_id" value="{self.logger.pk}"')


# ===========================================================================
# E4 — the hand-back, both ways
# ===========================================================================

class HandBackTests(ReviewerOverrideBase):

    def test_a_reviewer_replaces_the_designers_override(self):
        self.logger.boq_quantity = D(3)
        self.logger.save(update_fields=['boq_quantity'])
        BOQLineOverride.objects.create(line=self.logger, note='three roofs',
                                       set_by=self.designer)
        old = self._age_override()
        self._login(self.qc)
        self._set(self.logger, 5, 'five roofs on the survey')
        override = BOQLineOverride.objects.get()
        self.assertEqual((override.set_by, override.set_as, override.note),
                         (self.qc, OVERRIDE_SET_AS_QC, 'five roofs on the survey'))
        # The in-place replace is a QuerySet.update(), which skips auto_now: set_at must
        # still move to the replacement's time.
        self.assertGreater(override.set_at, old + timedelta(days=2))
        self.assertEqual(self._logs('boq_formula_override_set'),
                         ["Fxqc (QC) overrode FX-LOG on FX-QC: 3 → 5 (five roofs on the "
                          "survey); replaced Fxdes (designer)'s 3 (three roofs)"])

    def _age_override(self):
        """Move the one override's set_at three days back (a QuerySet.update(), so auto_now
        does not undo it) and return that time, so a replace that left set_at alone would
        be caught."""
        old = timezone.now() - timedelta(days=3)
        BOQLineOverride.objects.update(set_at=old)
        self.assertEqual(BOQLineOverride.objects.get().set_at, old)
        return old

    def _fail_back(self):
        with transaction.atomic():
            _open_next_attempt(self.assignment, ATTEMPT_REASON_QC_FAILED, self.qc,
                               'Design QC failure on attempt 1')

    def test_after_a_fail_back_the_designer_sees_replaces_and_clears_a_reviewers_override(self):
        self._login(self.qc)
        self._set(self.logger, 4, 'two roofs')
        self._fail_back()

        self._login(self.designer)
        picker = self._picker_get(site=self.review)
        row = self._added(picker)[self.items['FX-LOG'].pk]
        self.assertEqual((row['ovr'], row['by'], row['note']),
                         (True, 'Overridden by QC (Fxqc)', 'two roofs'))

        old = self._age_override()
        self._override('set', self.logger, site=self.review, quantity='6',
                       note='designer: six')
        override = BOQLineOverride.objects.get()
        self.assertEqual((override.set_by, override.set_as),
                         (self.designer, OVERRIDE_SET_AS_DESIGNER))
        self.assertGreater(override.set_at, old + timedelta(days=2))
        self.assertEqual(self._logs('boq_formula_override_set')[-1],
                         "Fxdes overrode FX-LOG on FX-QC: 4 → 6 (designer: six); replaced "
                         "Fxqc (QC)'s 4 (two roofs)")

        self._override('clear', self.logger, site=self.review)
        self.assertFalse(BOQLineOverride.objects.exists())
        self.assertEqual(self._qty('FX-LOG', self.review), D(2))

    def test_after_a_fail_back_the_designer_can_clear_a_reviewers_override_directly(self):
        self._login(self.qc)
        self._set(self.logger, 4, 'two roofs')
        self._fail_back()
        self._login(self.designer)
        response = self._override('clear', self.logger, site=self.review)
        self.assertIn('Override on FX-LOG cleared; it is calculated again (2).',
                      self._messages(response))
        self.assertFalse(BOQLineOverride.objects.exists())


# ===========================================================================
# T3 — every marker names who set the override
# ===========================================================================

class MarkerTests(ReviewerOverrideBase):

    def test_the_correction_screen_names_qc(self):
        self._login(self.qc)
        self._set(self.logger, 4, 'two roofs')
        page = self.client.get(reverse('boq_correct', kwargs={
            'project_id': self.review.project_id}))
        self.assertContains(page, 'Overridden by QC (Fxqc): two roofs')
        self.assertContains(page, 'name="action" value="override_clear"')

    def test_the_review_panel_names_the_head(self):
        self._login(self.head)
        self._set(self.logger, 4, 'two roofs')
        page = self.client.get(reverse('design_qc_review', kwargs={
            'project_id': self.review.project_id}))
        self.assertContains(page, 'Overridden by the Head (Fxhead): two roofs')

    def test_the_picker_names_the_designer_on_their_own_override(self):
        line = self._line('FX-LOG', 3)
        BOQLineOverride.objects.create(line=line, note='three roofs', set_by=self.designer)
        self._login(self.designer)
        row = self._added(self._picker_get())[self.items['FX-LOG'].pk]
        self.assertEqual(row['by'], 'Overridden by the designer (Fxdes)')

    def test_a_profile_no_longer_recorded(self):
        override = BOQLineOverride(note='n', set_by=None, set_as=OVERRIDE_SET_AS_QC)
        self.assertEqual(override_label(override), 'Overridden (name not recorded)')


# ===========================================================================
# Q5 — the activity line always fits
# ===========================================================================

class LogTextTests(ReviewerOverrideBase):

    def test_without_a_replacement_the_designer_text_is_as_before(self):
        self.assertEqual(boq_override_set_log_text('Fxdes', OVERRIDE_SET_AS_DESIGNER, 'FX-LOG',
                                                   'FX-A', '2', '4', 'two roofs'),
                         'Fxdes overrode FX-LOG on FX-A: 2 → 4 (two roofs)')

    def test_the_longest_replacement_fits_activitylog_action(self):
        name = 'N' * 60                                   # _ACCB_LOG_NAME_MAX
        code = 'C' * 32                                   # BOQItemMaster.code
        site = 'S' * Project._meta.get_field('project_id').max_length
        text = boq_override_set_log_text(name, OVERRIDE_SET_AS_HEAD, code, site,
                                         '99999999.99', '99999999.99', 'x' * 500,
                                         (name, OVERRIDE_SET_AS_DESIGNER, 'y' * 500))
        self.assertLessEqual(len(text), 255)
        self.assertIn('; replaced ', text)


# ===========================================================================
# Query counts (P7)
# ===========================================================================

def _n(call):
    with CaptureQueriesContext(connection) as ctx:
        response = call()
    return len(ctx), response.status_code


class QueryTests(DisplayBase):

    def setUp(self):
        super().setUp()
        self._five_rules()

    def test_the_reviewer_posts_are_pinned_and_no_dearer_than_set_quantity(self):
        self._sheets()
        self._login(self.qc)
        url = reverse('boq_correct', kwargs={'project_id': self.review.project_id})
        self.client.get(url)       # warm the session and the content-type cache
        earth = self._lines('FX-EARTH', self.review)[0]
        counts = {}
        counts['set'] = _n(lambda: self.client.post(url, {
            'action': 'override_set', 'line_id': str(earth.pk), 'quantity': '12',
            'note': 'mast-mounted'}))
        counts['clear'] = _n(lambda: self.client.post(url, {
            'action': 'override_clear', 'line_id': str(earth.pk)}))

        # E5 on a sheet with no inverters, where FX-LOG works out to 0 and has no line.
        quiet = self._review_site('FX-QZ')
        for code, quantity in [('FX-PANEL', 10), ('FX-LA1', 2), ('FX-EARTH', 10)]:
            self._line(code, quantity, site=quiet)
        quiet_url = reverse('boq_correct', kwargs={'project_id': quiet.project_id})
        self.client.get(quiet_url)
        counts['create'] = _n(lambda: self.client.post(quiet_url, {
            'action': 'override_set', 'master_id': str(self.items['FX-LOG'].pk),
            'quantity': '1', 'note': 'remote meter'}))

        self.assertEqual({k: v[1] for k, v in counts.items()},
                         {'set': 302, 'clear': 302, 'create': 302})
        self.assertEqual({k: v[0] for k, v in counts.items()}, REVIEWER_POSTS)
        for key, (count, _status) in counts.items():
            self.assertLessEqual(count, SET_QUANTITY_5_RULES, key)
        self.assertTrue(BOQLineOverride.objects.filter(line__boq__project=quiet).exists())

    def _replace_count(self, code, extra_lines):
        """Queries for a reviewer replacing an override, on a sheet already up to date,
        with `extra_lines` ordinary lines beside the calculated ones."""
        site = self._review_site(code)
        for item, quantity in DisplayBase.SHEET:
            self._line(item, quantity, site=site)
        if extra_lines:
            from .models import BOQItemMaster
            misc = BOQItemMaster.objects.get_or_create(
                code='FX-MISC', defaults={'description': 'Sundries', 'unit': 'Nos',
                                          'category': 'Misc', 'project_type': 'OPEX',
                                          'sort_order': 9099, 'is_active': True})[0]
            self.items['FX-MISC'] = misc
            for _ in range(extra_lines):
                self._line('FX-MISC', 1, site=site)
        url = reverse('boq_correct', kwargs={'project_id': site.project_id})
        earth = self._lines('FX-EARTH', site)[0]
        self.client.get(url)
        # The first set brings the sheet up to date (adds the missing calculated lines).
        self.client.post(url, {'action': 'override_set', 'line_id': str(earth.pk),
                               'quantity': '12', 'note': 'first'})
        count, status = _n(lambda: self.client.post(url, {
            'action': 'override_set', 'line_id': str(earth.pk), 'quantity': '14',
            'note': 'second'}))
        self.assertEqual(status, 302)
        return count

    def test_a_reviewer_override_does_not_grow_with_the_lines_on_the_sheet(self):
        self._login(self.qc)
        self.assertEqual(self._replace_count('FX-Q1', 0), self._replace_count('FX-Q2', 25))

    def test_naming_who_set_an_override_costs_no_query_on_any_screen(self):
        self._sheets()
        for site, profile, set_as in [(self.site, self.designer, OVERRIDE_SET_AS_DESIGNER),
                                      (self.review, self.qc, OVERRIDE_SET_AS_QC)]:
            BOQLineOverride.objects.create(line=self._lines('FX-EARTH', site)[0],
                                           note='set by hand', set_by=profile, set_as=set_as)
        self.assertEqual(self._screens(),
                         screens_expected(WITH_RULES_EXTRA))
