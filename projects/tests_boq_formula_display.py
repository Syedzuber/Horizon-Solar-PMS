"""
B-2b.1 — calculated BOQ lines shown as calculated (still no live rules).

What this file pins (Zuber's B-2b.1 answers, 22 Nov 2026):

  * NO RULES: every screen renders as before, for exactly one extra query (the exists()
    gate). The baselines below were measured on dd80884, before B-2b.1, with this file's
    fixture (the same requests, second of two identical calls, solarpms.test_settings).
  * WITH RULES (5): the picker draws calculated rows read-only with the rule in plain words,
    marks an overridden row with its note and Clear, keeps calculated items out of the
    search, and lists calculated items with no line (A1 block). The count is the same for
    any number of rules.
  * A1: an override on a calculated item with no line creates it, with a record; Clear
    removes it again when the rule gives 0 (D1). Q8: no BOQ, refused. Q3: an override on an
    existing line writes a record too.
  * A3: an item on two lines is not written, and later rules read the sheet's total.
  * Q9: those two lines are ordinary lines in the picker; removing one recalculates the
    other on that save.
  * A4: a rule that cannot produce its figure is refused with the reason on the upload
    (preview and commit), the correction screen and the override endpoint, never a 500, and
    nothing is saved.
  * A2: the upload preview and the commit agree, field by field, for the same file.
  * Reviewer screens: the correction screen and the package review panel mark calculated
    lines, and the correction screen offers no quantity form or "add" for them.
"""
import json
from decimal import Decimal

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from .models import (
    ActivityLog, BOQ, BOQCorrection, BOQItemGroup, BOQItemMaster, BOQLineOverride,
    BOQRecalculation, FORMULA_SOURCE_OVERRIDE_CLEARED, FORMULA_SOURCE_OVERRIDE_SET,
    FORMULA_SOURCE_PICKER,
)
from .tests_boq_formula_apply import FormulaBase

D = Decimal

#: Measured on dd80884 (B-2b, before B-2b.1) with DisplayBase's sheet and _screens()'s
#: requests, under solarpms.test_settings: picker GET 18, upload preview 14, boq_correct GET
#: 21, the package review screen 38 for QC and 35 for the Head. Rules made no difference
#: then (the GETs did not read them).
#: DW-8: 'correct' +1 — the correction screen's "← My queue" asks Head authority (the deputy
#: lookup) for this fixture's open-pool QC reviewer; a reviewer named on the site pays none.
#: DW-5: 'picker' +1 — the Modules pop-up's default wattage (DesignSettings.current()), read
#: for the designer who can edit.
BASELINE = {'picker': 19, 'preview': 14, 'correct': 22, 'review_qc': 38, 'review_head': 35}

#: With rules, each screen adds the exists() gate, load_rules()'s three queries and one read
#: of the lines with their overrides: five, whatever the number of rules.
WITH_RULES_EXTRA = 5

#: DW-2a — the package review screen now asks whose turn it is ONCE for its three buttons
#: (permissions.design_reviewer_turns: correct the BOQ, revise the Arka, revise the CAD)
#: instead of user_can_correct_boq() plus user_may_revise_design(), which between them read
#: the current attempt, its change requests and the group lock twice over. Five queries
#: fewer, for QC and for the Head; measured at the DW-2a build. The other screens are as
#: they were.
DW2A_REVIEW_SAVING = {'review_qc': 5, 'review_head': 5}


def screens_expected(extra):
    """BASELINE plus `extra` on every screen, less DW-2a's saving on the review screen."""
    return {k: v + extra - DW2A_REVIEW_SAVING.get(k, 0) for k, v in BASELINE.items()}

OVERFLOW = 'outside what a BOQ line can hold'


class DisplayBase(FormulaBase):

    SHEET = [('FX-PANEL', 10), ('FX-LA1', 2), ('FX-EARTH', 10), ('FX-INV1', 3)]

    def setUp(self):
        super().setUp()
        for code, description, unit, category, sort_order in [
                ('FX-CLAMP', 'Module clamp', 'Nos', 'Modules', 9009),
                ('FX-WMS', 'Weather monitoring station', 'Nos', 'Monitoring', 9010)]:
            self.items[code] = BOQItemMaster.objects.create(
                code=code, description=description, unit=unit, category=category,
                project_type='OPEX', sort_order=sort_order, is_active=True)

    def _five_rules(self):
        """B-2b's three, plus an item-driven rule and a gated constant."""
        self._rules()
        inverters = BOQItemGroup.objects.get(name='fx inverters')
        self._rule('FX-CLAMP', '2 clamps per module',
                   [{'item': self.items['FX-PANEL'], 'label': 'modules'}], multiplier=2)
        self._rule('FX-WMS', '1 weather station per site with inverters', [], constant=1,
                   requires_group=inverters)

    def _sheets(self):
        self.review = self._review_site()
        for code, quantity in self.SHEET:
            self._line(code, quantity, site=self.review)
            self._line(code, quantity)

    def _count(self, call):
        call()      # warm the session and the content-type cache
        with CaptureQueriesContext(connection) as ctx:
            response = call()
        self.assertEqual(response.status_code, 200)
        return len(ctx)

    def _screens(self):
        """{screen: query count} for the four screens, as the baseline was measured."""
        counts = {}
        self._login(self.designer)
        picker = reverse('opex_boq_entry', kwargs={'project_id': self.site.project_id})
        counts['picker'] = self._count(lambda: self.client.get(picker))
        upload = reverse('opex_boq_upload', kwargs={'project_id': self.site.project_id})
        counts['preview'] = self._count(lambda: self.client.post(upload, {
            'phase': 'preview',
            'file': self._file([('FX-LA1', 3), ('FX-EARTH', 11), ('FX-LOG', 2)])}))
        self._login(self.qc)
        correct = reverse('boq_correct', kwargs={'project_id': self.review.project_id})
        counts['correct'] = self._count(lambda: self.client.get(correct))
        review = reverse('design_qc_review', kwargs={'project_id': self.review.project_id})
        counts['review_qc'] = self._count(lambda: self.client.get(review))
        self._login(self.head)
        counts['review_head'] = self._count(lambda: self.client.get(review))
        return counts

    def _picker_get(self, site=None):
        return self.client.get(reverse('opex_boq_entry', kwargs={
            'project_id': (site or self.site).project_id}))

    def _added(self, response):
        return {row['id']: row for row in json.loads(response.context['added_json'])}

    def _override_master(self, code, site=None, **data):
        return self.client.post(
            reverse('opex_boq_override', kwargs={'project_id': (site or self.site).project_id}),
            {'action': 'set', 'master_id': str(self.items[code].pk), **data}, follow=True)


# ===========================================================================
# No rules: every screen as before, for one exists()
# ===========================================================================

class NoRulesTests(DisplayBase):

    def setUp(self):
        super().setUp()
        self._sheets()

    def test_each_screen_costs_its_baseline_plus_one_exists(self):
        self.assertEqual(self._screens(), screens_expected(1))

    def test_the_picker_payloads_and_page_are_as_before(self):
        self._login(self.designer)
        response = self._picker_get()
        for row in json.loads(response.context['added_json']):
            self.assertEqual(set(row), {'id', 'qty'})
        for item in json.loads(response.context['catalogue_json']):
            self.assertNotIn('calc', item)
        for marker in ('boqOverrideModal', 'boqCalcAbsent', 'boqCalcMulti', 'boqOvrHint',
                       'boqFormulaError'):
            self.assertNotContains(response, f'id="{marker}"')

    def test_the_reviewer_screens_carry_no_marker(self):
        self._login(self.qc)
        correct = self.client.get(reverse('boq_correct', kwargs={
            'project_id': self.review.project_id}))
        earth = self._lines('FX-EARTH', self.review)[0]
        self.assertContains(correct, f'name="item_id" value="{earth.pk}"')
        self.assertNotContains(correct, 'boq-formula-marker')
        review = self.client.get(reverse('design_qc_review', kwargs={
            'project_id': self.review.project_id}))
        self.assertNotContains(review, 'boq-formula-marker')


class WithRulesQueryTests(DisplayBase):

    def test_five_rules_cost_five_more_queries_than_the_baseline_on_every_screen(self):
        self._five_rules()
        self._sheets()
        self.assertEqual(self._screens(),
                         screens_expected(WITH_RULES_EXTRA))


# ===========================================================================
# T1 — the picker
# ===========================================================================

class PickerDisplayTests(DisplayBase):

    def setUp(self):
        super().setUp()
        self._rules()
        self._login(self.designer)
        self._picker({'FX-LA1': 2, 'FX-LA2': 1, 'FX-INV1': 12})   # EARTH 15, LOG 2, RS485 10
        self.earth = self._lines('FX-EARTH')[0]
        self.logger = self._lines('FX-LOG')[0]
        self._override('set', self.logger, quantity='4', note='two roofs')  # RS485 20

    def test_a_calculated_row_is_read_only_with_its_rule_in_plain_words(self):
        earth = self._added(self._picker_get())[self.items['FX-EARTH'].pk]
        self.assertEqual(earth, {
            'id': self.items['FX-EARTH'].pk, 'qty': '15', 'calc': True, 'line': self.earth.pk,
            'expl': '5 m per lightning arrester: 3 lightning arresters (2 FX-LA1 + 1 FX-LA2) '
                    '× 5 = 15 Meter',
            'ovr': False, 'note': '', 'stale': ''})

    def test_the_sheet_script_draws_a_calculated_row_as_text_with_no_quantity_field(self):
        content = self._picker_get().content.decode()
        # calcRow(): the figure as text, the hidden `item` input, the override controls,
        # and no qty_ field (absent = unchanged on the server).
        self.assertIn("if (CALC[item.id]) {\n          html += calcRow(item, CALC[item.id]);",
                      content)
        self.assertIn('\'<input type="hidden" name="item" value="\' + item.id + \'">\' +\n'
                      '                  overrideControls(item, calc)', content)

    def test_an_overridden_row_carries_its_marker_note_and_clear(self):
        response = self._picker_get()
        logger = self._added(response)[self.items['FX-LOG'].pk]
        self.assertEqual((logger['ovr'], logger['note'], logger['line'], logger['qty']),
                         (True, 'two roofs', self.logger.pk, '4'))
        self.assertEqual(logger['expl'], '1 data logger per 10 inverters: set by hand to 4 '
                                         'Nos; the rule gives 2 Nos')
        self.assertContains(response, 'id="boqOverrideClear"')
        self.assertContains(response, 'id="boqOverrideModal"')
        self.assertContains(response, 'form="boqOverrideClear" name="item_id"')

    def test_calculated_items_are_flagged_and_left_out_of_the_search(self):
        response = self._picker_get()
        flagged = {item['code'] for item in json.loads(response.context['catalogue_json'])
                   if item.get('calc')}
        self.assertEqual(flagged, {'FX-EARTH', 'FX-LOG', 'FX-RS485'})
        self.assertContains(response, 'if (item.calc) { return false; }')

    def test_a_stale_line_shows_the_stored_figure_and_what_the_next_save_writes(self):
        la2 = self._lines('FX-LA2')[0]
        la2.boq_quantity = D(3)
        la2.save(update_fields=['boq_quantity'])     # rule now gives 25; 15 is stored
        earth = self._added(self._picker_get())[self.items['FX-EARTH'].pk]
        self.assertEqual((earth['qty'], earth['stale']),
                         ('15', 'Recalculates to 25 on the next save.'))

    def test_a_calculated_item_with_no_line_is_listed_with_an_override(self):
        self._picker({'FX-INV1': 12})                # no arresters: EARTH removed (D1)
        response = self._picker_get()
        self.assertNotIn(self.items['FX-EARTH'].pk, self._added(response))
        self.assertEqual(response.context['calculated_absent'], [{
            'master_id': self.items['FX-EARTH'].pk, 'code': 'FX-EARTH',
            'description': 'Earthing cable 16 sq mm', 'unit': 'Meter',
            'explanation': '5 m per lightning arrester: 0 lightning arresters × 5 = 0 Meter',
            'stale': ''}])
        self.assertContains(response, 'Calculated items not on this BOQ (1)')
        self.assertContains(response, f'data-master="{self.items["FX-EARTH"].pk}"')

    def test_override_controls_are_disabled_while_the_sheet_has_unsaved_changes(self):
        content = self._picker_get().content.decode()
        self.assertIn("button.disabled = dirty;", content)
        self.assertIn('Save your changes first', content)


# ===========================================================================
# T2 — the override endpoint: A1, Q3, Q8
# ===========================================================================

class OverrideCreateTests(DisplayBase):

    def setUp(self):
        super().setUp()
        self._rules()
        self._login(self.designer)
        self._picker({'FX-INV1': 12})                # LOG 2, RS485 10; EARTH has no line

    def test_an_override_on_an_item_with_no_line_creates_it_with_a_record(self):
        response = self._override_master('FX-EARTH', quantity='8', note='ESE on a mast')
        line = self._lines('FX-EARTH')[0]
        master = self.items['FX-EARTH']
        self.assertEqual((line.boq_quantity, line.description, line.uom, line.serial_no,
                          line.is_standard_item),
                         (D(8), master.description, master.unit, master.sort_order, True))
        self.assertEqual(BOQLineOverride.objects.get(line=line).note, 'ESE on a mast')
        record = BOQRecalculation.objects.get(item_code='FX-EARTH')
        self.assertEqual((record.line, record.quantity_before, record.quantity_after,
                          record.source, record.rule),
                         (line, None, D(8), FORMULA_SOURCE_OVERRIDE_SET, self.earth_rule))
        self.assertIn('set by hand to 8', record.explanation)
        self.assertIn('FX-EARTH added at 8 by hand. It will not recalculate until the '
                      'override is cleared.', self._messages(response))
        log = ActivityLog.objects.get(action_code='boq_formula_override_set')
        self.assertEqual(log.action, 'Fxdes overrode FX-EARTH on FX-A: not on the BOQ → 8 '
                                     '(ESE on a mast)')

    def test_a_blank_note_is_refused_and_nothing_is_created(self):
        response = self._override_master('FX-EARTH', quantity='8', note='  ')
        self.assertIn('Nothing was saved.', self._messages(response))
        self.assertEqual(self._lines('FX-EARTH'), [])
        self.assertFalse(BOQRecalculation.objects.filter(item_code='FX-EARTH').exists())

    def test_clearing_it_removes_the_line_again_when_the_rule_gives_0(self):
        self._override_master('FX-EARTH', quantity='8', note='ESE on a mast')
        line = self._lines('FX-EARTH')[0]
        response = self._override('clear', line)
        self.assertEqual(self._lines('FX-EARTH'), [])
        record = BOQRecalculation.objects.get(item_code='FX-EARTH',
                                              source=FORMULA_SOURCE_OVERRIDE_CLEARED)
        self.assertEqual((record.quantity_before, record.quantity_after), (D(8), None))
        self.assertIn('Override on FX-EARTH cleared. It works out to 0, so the line was '
                      'removed.', self._messages(response))

    def test_a_create_is_refused_once_the_line_exists(self):
        self._override_master('FX-EARTH', quantity='8', note='ESE on a mast')
        response = self._override_master('FX-EARTH', quantity='9', note='again')
        self.assertIn('FX-EARTH is already on this BOQ. Reload the page and override its '
                      'line.', self._messages(response))
        self.assertEqual([l.boq_quantity for l in self._lines('FX-EARTH')], [D(8)])

    def test_an_item_that_is_not_calculated_cannot_be_created_by_override(self):
        response = self._override_master('FX-PANEL', quantity='8', note='x')
        self.assertIn('That item is not calculated, so there is nothing to override. Add it '
                      'on the BOQ.', self._messages(response))
        self.assertEqual(self._lines('FX-PANEL'), [])

    def test_a_site_with_no_boq_is_told_to_save_it_first(self):
        site, _assignment = self._opex_site('FX-B')
        response = self._override_master('FX-EARTH', site=site, quantity='8', note='x')
        self.assertIn('Save the BOQ first. An override can be set once the BOQ has been '
                      'saved.', self._messages(response))
        self.assertFalse(BOQ.objects.filter(project=site).exists())

    def test_an_override_on_an_existing_line_writes_a_record_too(self):
        logger = self._lines('FX-LOG')[0]
        self._override('set', logger, quantity='4', note='two roofs')
        record = BOQRecalculation.objects.get(item_code='FX-LOG',
                                              source=FORMULA_SOURCE_OVERRIDE_SET)
        self.assertEqual((record.line, record.quantity_before, record.quantity_after,
                          record.source),
                         (logger, D(2), D(4), FORMULA_SOURCE_OVERRIDE_SET))
        self.assertEqual(record.explanation, '1 data logger per 10 inverters: set by hand to '
                                             '4 Nos; the rule gives 2 Nos')

    def test_an_override_that_does_not_change_the_figure_writes_no_record(self):
        logger = self._lines('FX-LOG')[0]
        self._override('set', logger, quantity='2', note='confirmed on site')
        self.assertTrue(BOQLineOverride.objects.filter(line=logger).exists())
        self.assertFalse(BOQRecalculation.objects.filter(
            item_code='FX-LOG', source=FORMULA_SOURCE_OVERRIDE_SET).exists())

    def test_an_item_on_two_lines_cannot_be_overridden(self):
        self._line('FX-LOG', 3)
        logger = self._lines('FX-LOG')[0]
        response = self._override('set', logger, quantity='4', note='x')
        self.assertIn('FX-LOG is on this BOQ more than once, so it is not calculated: its '
                      'lines are ordinary lines. Remove the extra line; when one is left, it '
                      'is calculated again on the next save.', self._messages(response))
        self.assertFalse(BOQLineOverride.objects.exists())


# ===========================================================================
# T3 — A3, and Q9 in the picker
# ===========================================================================

class TwoLinesTests(DisplayBase):

    def setUp(self):
        super().setUp()
        self._rules()
        self._login(self.designer)

    def test_a3_an_item_on_two_lines_feeds_later_rules_at_the_sheet_total(self):
        # The rule gives FX-LOG 2 (12 inverters / 10, up); the sheet carries 3 + 4 = 7.
        self._line('FX-LOG', 3)
        self._line('FX-LOG', 4)
        self._picker({'FX-INV1': 12})
        self.assertEqual([l.boq_quantity for l in self._lines('FX-LOG')], [D(3), D(4)])
        self.assertEqual(self._qty('FX-RS485'), D(35))     # 5 m x 7, not 5 m x 2
        self.assertFalse(BOQRecalculation.objects.filter(item_code='FX-LOG').exists())

    def _two_earth_lines(self):
        self._picker({'FX-LA1': 2})                        # EARTH 10
        first = self._lines('FX-EARTH')[0]
        second = self._line('FX-EARTH', 4)
        return first, second

    def test_q9_the_picker_draws_each_line_as_an_ordinary_line(self):
        first, second = self._two_earth_lines()
        response = self._picker_get()
        self.assertEqual([row.pk for row in response.context['calc_multi_rows']],
                         [first.pk, second.pk])
        self.assertNotIn(self.items['FX-EARTH'].pk, self._added(response))
        for line in (first, second):
            self.assertContains(response, f'name="qty_row_{line.pk}"')
            self.assertContains(response, f'boqDropCalcLine({line.pk})')
        self.assertContains(response, 'FX-EARTH is on this BOQ more than once, so it is not '
                                      'calculated: its lines are ordinary lines.')

    def test_q9_removing_one_line_recalculates_the_other_on_that_save(self):
        first, second = self._two_earth_lines()
        self._picker({'FX-LA1': 2}, extra={'drop_line': str(first.pk),
                                           f'qty_row_{second.pk}': '4'})
        self.assertEqual([l.pk for l in self._lines('FX-EARTH')], [second.pk])
        self.assertEqual(self._qty('FX-EARTH'), D(10))
        record = BOQRecalculation.objects.filter(item_code='FX-EARTH').order_by('-pk').first()
        self.assertEqual((record.quantity_before, record.quantity_after, record.source),
                         (D(4), D(10), FORMULA_SOURCE_PICKER))

    def test_q9_a_line_of_the_pair_is_editable_and_nothing_is_recalculated(self):
        first, second = self._two_earth_lines()
        self._picker({'FX-LA1': 2}, extra={f'qty_row_{first.pk}': '6',
                                           f'qty_row_{second.pk}': '4'})
        self.assertEqual([l.boq_quantity for l in self._lines('FX-EARTH')], [D(6), D(4)])

    def test_q9_a_page_that_names_neither_line_keeps_both(self):
        first, second = self._two_earth_lines()
        self._picker({'FX-LA1': 2})
        self.assertEqual([l.pk for l in self._lines('FX-EARTH')], [first.pk, second.pk])


# ===========================================================================
# T4 — A4: refused with the reason, nothing saved, never a 500
# ===========================================================================

class RefusalTests(DisplayBase):

    def setUp(self):
        super().setUp()
        self._rules()
        # 999999 x 1000 modules is far past numeric(10,2); 999999 x 1000 loggers likewise.
        self._rule('FX-CLAMP', 'overflow by modules',
                   [{'item': self.items['FX-PANEL'], 'label': 'modules'}], multiplier=999999)
        self._rule('FX-WMS', 'overflow by loggers',
                   [{'item': self.items['FX-LOG'], 'label': 'loggers'}], multiplier=999999)

    def test_the_upload_preview_refuses_with_the_reason(self):
        self._login(self.designer)
        url = reverse('opex_boq_upload', kwargs={'project_id': self.site.project_id})
        response = self.client.post(url, {'phase': 'preview',
                                          'file': self._file([('FX-PANEL', 1000)])})
        self.assertEqual(response.status_code, 200)
        self.assertIn(OVERFLOW, response.context['file_error'])
        self.assertIn('Nothing has been changed.', response.context['file_error'])
        self.assertNotContains(response, 'value="commit"')

    def test_the_upload_commit_refuses_with_the_reason_and_saves_nothing(self):
        self._login(self.designer)
        url = reverse('opex_boq_upload', kwargs={'project_id': self.site.project_id})
        response = self.client.post(url, {'phase': 'commit', 'rows_json': json.dumps(
            [{'code': 'FX-PANEL', 'quantity': '1000'}])})
        self.assertEqual(response.status_code, 200)
        self.assertIn(OVERFLOW, response.context['file_error'])
        self.assertIn('Nothing was saved.', response.context['file_error'])
        self.assertFalse(BOQ.objects.filter(project=self.site).exists())  # rolled back

    def _review_with_panel(self):
        review = self._review_site()
        self._line('FX-PANEL', 10, site=review)
        self._login(self.qc)
        return review

    def test_a_correction_refuses_with_the_reason_and_saves_nothing(self):
        review = self._review_with_panel()
        panel = self._lines('FX-PANEL', review)[0]
        response = self._correct(review, 'set_quantity', item_id=str(panel.pk),
                                 quantity='1000')
        self.assertEqual(response.status_code, 200)
        messages = self._messages(response)
        self.assertTrue(any(OVERFLOW in m for m in messages), messages)
        self.assertIn('Nothing was saved.', messages)
        self.assertEqual(self._qty('FX-PANEL', review), D(10))
        self.assertFalse(BOQCorrection.objects.exists())

    def test_an_added_line_is_refused_with_the_reason_and_saves_nothing(self):
        review = self._review_site()
        self._login(self.qc)
        response = self._correct(review, 'add_line', master_id=str(self.items['FX-PANEL'].pk),
                                 quantity='1000')
        self.assertTrue(any(OVERFLOW in m for m in self._messages(response)))
        self.assertEqual(self._lines('FX-PANEL', review), [])
        self.assertFalse(BOQCorrection.objects.exists())

    def test_an_override_refuses_with_the_reason_and_saves_nothing(self):
        self._login(self.designer)
        self._picker({'FX-INV1': 12})                # LOG 2, WMS 1999998
        logger = self._lines('FX-LOG')[0]
        response = self._override('set', logger, quantity='1000', note='big site')
        self.assertEqual(response.status_code, 200)
        messages = self._messages(response)
        self.assertTrue(any(OVERFLOW in m for m in messages), messages)
        self.assertIn('Nothing was saved.', messages)
        self.assertEqual(self._qty('FX-LOG'), D(2))
        self.assertFalse(BOQLineOverride.objects.exists())

    def test_clearing_an_override_refuses_with_the_reason_too(self):
        self._login(self.designer)
        self._picker({'FX-INV1': 12})
        logger = self._lines('FX-LOG')[0]
        self._override('set', logger, quantity='1', note='one roof')
        # Driven so far that, recalculated, the logger count overflows its dependant.
        self._picker({'FX-INV1': 50000})
        response = self._override('clear', logger)
        self.assertTrue(any(OVERFLOW in m for m in self._messages(response)))
        self.assertTrue(BOQLineOverride.objects.filter(line=logger).exists())


# ===========================================================================
# T5 — A2: the preview and the commit agree
# ===========================================================================

class UploadAgreementTests(DisplayBase):

    def setUp(self):
        super().setUp()
        self._rules()
        self._login(self.designer)
        self._picker({'FX-LA1': 2, 'FX-INV1': 12, 'FX-PANEL': 5})   # EARTH 10, LOG 2, RS485 10
        self._override('set', self._lines('FX-LOG')[0], quantity='4', note='two roofs')

    FILE = [('FX-LA1', 0),      # changed: arresters to 0, so FX-EARTH is removed (D1)
            ('FX-INV2', 3),     # added
            ('FX-LOG', 1),      # calculated and overridden: kept at 4
            ('FX-RS485', 7),    # calculated: replaced by 5 m x 4 = 20
            ('FX-PANEL', 5)]    # unchanged

    def test_the_preview_and_the_commit_agree_field_by_field(self):
        url = reverse('opex_boq_upload', kwargs={'project_id': self.site.project_id})
        preview = self.client.post(url, {'phase': 'preview', 'file': self._file(self.FILE)})
        planned = preview.context['upload_summary']
        commit = self.client.post(url, {'phase': 'commit',
                                        'rows_json': preview.context['rows_json']})
        done = commit.context['upload_summary']

        self.assertEqual(set(planned), set(done))
        for key in planned:
            self.assertEqual(planned[key], done[key], key)
        self.assertEqual(done, {
            'added': ['FX-INV2'],
            'changed': [('FX-LA1', D(2), D(0))],
            'unchanged': 1,
            'readded': [],
            'replaced': [{'code': 'FX-RS485', 'file': D(7), 'calculated': D(20)}],
            'kept_override': [{'code': 'FX-LOG', 'file': D(1), 'calculated': D(4)}],
            'removed': ['FX-EARTH'],
            # DW-7 answer 2: the engine's own catch-up, apart from the file's figures. RS485
            # is already listed as replaced, so it is not listed again.
            'recalculated': [],
            # DW-7 answer 4: no item here is on 2+ lines.
            'on_lines': [],
        })
        # What the commit did is what both said.
        self.assertEqual((self._qty('FX-EARTH'), self._qty('FX-RS485'), self._qty('FX-LOG')),
                         ('absent', D(20), D(4)))
        self.assertEqual((commit.context['added'], commit.context['changed'],
                          commit.context['removed']), (1, 1, 1))

    def test_the_preview_marks_calculated_and_overridden_rows_and_counts_the_removal(self):
        url = reverse('opex_boq_upload', kwargs={'project_id': self.site.project_id})
        preview = self.client.post(url, {'phase': 'preview', 'file': self._file(self.FILE)})
        self.assertContains(preview, 'Calculated: will be replaced by 20')
        self.assertContains(preview, 'Override kept: stays at 4')
        self.assertContains(preview, 'Rows removed: <strong>1</strong>')
        self.assertContains(preview, 'id="boqPreviewRemoved"')
        # A calculated row in the file is neither "added" nor "changed".
        self.assertEqual(([e['code'] for e in preview.context['plan_add']],
                          [c['entry']['code'] for c in preview.context['plan_change']]),
                         (['FX-INV2'], ['FX-LA1']))


# ===========================================================================
# T6 — the reviewer screens
# ===========================================================================

class ReviewerScreenTests(DisplayBase):

    def setUp(self):
        super().setUp()
        self._rules()
        self.review = self._review_site()
        for code, quantity in [('FX-LA1', 2), ('FX-LA2', 1), ('FX-EARTH', 15), ('FX-PANEL', 10)]:
            self._line(code, quantity, site=self.review)
        self.earth = self._lines('FX-EARTH', self.review)[0]
        BOQLineOverride.objects.create(line=self.earth, note='mast-mounted ESE',
                                       set_by=self.designer)
        self._login(self.qc)

    def test_the_correction_screen_marks_calculated_rows_and_offers_no_form(self):
        response = self.client.get(reverse('boq_correct', kwargs={
            'project_id': self.review.project_id}))
        panel = self._lines('FX-PANEL', self.review)[0]
        self.assertContains(response, 'boq-formula-marker')
        self.assertContains(response, '>Calculated</span>')
        self.assertContains(response, 'Overridden by the designer (Fxdes): mast-mounted ESE')
        self.assertNotContains(response, f'name="item_id" value="{self.earth.pk}"')
        self.assertContains(response, f'name="item_id" value="{panel.pk}"')

    def test_the_correction_screen_does_not_offer_calculated_items_to_add(self):
        response = self.client.get(reverse('boq_correct', kwargs={
            'project_id': self.review.project_id}))
        offered = {m.code for _category, masters in response.context['addable_groups']
                   for m in masters}
        self.assertTrue({'FX-LOG', 'FX-RS485'}.isdisjoint(offered))
        self.assertIn('FX-INV1', offered)

    def test_the_review_panel_marks_calculated_rows(self):
        response = self.client.get(reverse('design_qc_review', kwargs={
            'project_id': self.review.project_id}))
        self.assertContains(response, 'boq-formula-marker')
        self.assertContains(response, 'Overridden by the designer (Fxdes): mast-mounted ESE')
        self._login(self.head)
        response = self.client.get(reverse('design_qc_review', kwargs={
            'project_id': self.review.project_id}))
        self.assertContains(response, '>Calculated</span>')
