"""
DW-6 — the INC 2P rule (OPX-203) with its safeguards (ruling DW-R12; Zuber's answers 1-8,
27 Nov 2026).

    INC 2P modules = all modules - (3P + Other + Ballast + Tin Shade lines)
    OPX-203        = INC 2P modules x the site's module wattage / 1000, in kWp, to 0.01

    (a) below 0 -> 0 with a warning, never a refused save;
    (b) a cross-check warning when the count differs from the modules the 2P structure
        lines hold (2P*N holds 2N), with its own wording when they hold none (answer 1);
    (c) the explanation names the wattage it used, and says "mixed" when it is;
    (d) an override survives and suppresses neither warning.

Covered here: every other rule unchanged on 25 anonymised real sheets (answer 6); the
wattage (I1); the subtraction; (a)-(d); the warnings on the picker, the correction screen,
the QC/Head panel, the BOQ view (Design users only), the design group screen, Excel and
snapshots; never blocking a save or Mark complete; the loader's new keys; query pins.
"""
import hashlib
import json
from decimal import Decimal
from io import BytesIO
from pathlib import Path

from django.core.exceptions import ValidationError
from django.db import connection
from django.test import SimpleTestCase
from django.test.utils import CaptureQueriesContext
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from openpyxl import load_workbook

from .boq_formulas import (
    CHECK_INC_2P, DEFAULT_MODULE_WP_FALLBACK, DRIVER_ROLE_CHECK, ROUND_NONE,
    SITE_FACTOR_MODULE_WP, WARNING_BELOW_ZERO, WARNING_CROSS_CHECK, WARNING_NO_STRUCTURES,
    evaluate, load_rules, read_warning_rules, sheet_warnings, site_module_wp, warning_text,
)
from .management.commands.load_formula_rules import (
    DEFAULT_FILE, RulesFileError, parse_rules, read_file,
)
from .models import (
    BOQFormulaDriver, BOQFormulaRule, BOQItem, BOQItemMaster, BOQLineOverride,
    DesignBOQGroup, DesignBOQGroupMembership, DesignBOQGroupSnapshot, DesignSettings,
)
from .design_boq_group_views import hydrate, snapshot_fingerprint
from .tests_b2c_rules import B2cBase
from .tests_boq_formula_apply import D
from .tests_dw5_modules import WP, DW5Base, _client_login

BASELINE_FILE = Path(DEFAULT_FILE).parent / 'dw6_rule_baseline_sheets_27nov.json'
DW5_FILE = Path(DEFAULT_FILE).parent / 'dw5_formula_rules_27nov.json'

SUMMARY = 'INC work 2P structure: the modules not on 3P, Other, Ballast or Tin Shade, in kWp'
BELOW_ZERO = ('INC 2P works out below 0 (−10 modules); set to 0. Check the 3P, Other, '
              'Ballast and Tin Shade lines.')


# ═════════════════════════════════════════════════════════════════════════════
# Every other rule, exactly as before (answer 6)
# ═════════════════════════════════════════════════════════════════════════════

class UnchangedRulesTests(B2cBase):
    """The DW-6 file loaded; each of the 25 anonymised real sheets (item codes and
    quantities only) evaluated; every rule but OPX-203 gives exactly the quantity, figure,
    explanation and override flag it gave on 3c2b17f (stored in BASELINE_FILE)."""

    def test_every_rule_but_opx203_is_unchanged_on_25_real_sheets(self):
        sheets = json.loads(BASELINE_FILE.read_text(encoding='utf-8'))['sheets']
        self.assertEqual(len(sheets), 25)
        for sheet in sheets:
            for code in list(sheet['quantities']) + list(sheet['overrides']):
                BOQItemMaster.objects.get_or_create(code=code, defaults={
                    'description': f'{code} item', 'unit': 'Nos', 'project_type': 'OPEX',
                    'category': 'Other', 'sort_order': 9000})
        self.load('--apply')
        rules = load_rules('OPEX')
        pk = {m.code: m.pk for m in BOQItemMaster.objects.all()}
        compared = 0
        for n, sheet in enumerate(sheets):
            got = evaluate({pk[c]: D(q) for c, q in sheet['quantities'].items()}, rules,
                           {pk[c]: D(q) for c, q in sheet['overrides'].items()})
            after = {rules.codes[i]: [str(r.quantity), str(r.computed), r.explanation,
                                      r.overridden] for i, r in got.items()}
            self.assertIn('OPX-203', after)
            del after['OPX-203']
            with self.subTest(sheet=n):
                self.assertEqual(after, sheet['before'])
            compared += len(after)
        self.assertEqual(compared, 25 * 39)

    def test_no_rule_but_inc2p_raises_a_warning_on_any_sheet(self):
        self.load('--apply')
        rules = load_rules('OPEX')
        self.assertEqual([r.target_code for r in rules.rules if r.raises_warnings],
                         ['OPX-203'])


# ═════════════════════════════════════════════════════════════════════════════
# The fixture: DW-5's catalogue and R12, plus the INC 2P rule as the DW-6 file states it
# ═════════════════════════════════════════════════════════════════════════════

class DW6Base(DW5Base):
    """DW5Base (OPX-001 retired, 550/620/630/640 Wp, R12, default 640) plus:

        OPX-203  INC work 2P structure (KWp), the INC 2P rule on it
        OPX-035..038  3P / Other / Ballast / Tin Shade (Nos)
        OPX-027  2P*3*1000mm (holds 6 modules), OPX-030  2P*6*1000mm (holds 12)
        OPX-220  1P*3*1000mm (not in the cross-check)
    """

    def setUp(self):
        super().setUp()
        make = BOQItemMaster.objects.create
        for code, description, unit, category, position in (
                ('OPX-203', 'INC work 2P structure', 'KWp', 'BOS', 2670),
                ('OPX-035', '3P Structure', 'Nos', 'MMS', 550),
                ('OPX-036', 'Other Structure', 'Nos', 'MMS', 560),
                ('OPX-037', 'Ballast Type Structure', 'Nos', 'MMS', 570),
                ('OPX-038', 'Tin Shade', 'Nos', 'MMS', 580),
                ('OPX-027', '2P*3*1000mm height', 'Nos', 'MMS', 270),
                ('OPX-030', '2P*6*1000mm height', 'Nos', 'MMS', 300),
                ('OPX-220', '1P*3*1000mm height', 'Nos', 'MMS', 2200)):
            self.items[code] = make(code=code, description=description, unit=unit,
                                    category=category, project_type='OPEX',
                                    sort_order=position)
        self.inc2p = self._rule(
            'OPX-203', SUMMARY,
            [{'group': self.modules},
             {'item': self.items['OPX-035'], 'weight': -1, 'label': '3P structures'},
             {'item': self.items['OPX-036'], 'weight': -1, 'label': 'Other structures'},
             {'item': self.items['OPX-037'], 'weight': -1, 'label': 'Ballast'},
             {'item': self.items['OPX-038'], 'weight': -1, 'label': 'Tin Shade'},
             {'item': self.items['OPX-027'], 'weight': 6, 'role': DRIVER_ROLE_CHECK,
              'label': '2P*3*1000mm structures'},
             {'item': self.items['OPX-030'], 'weight': 12, 'role': DRIVER_ROLE_CHECK,
              'label': '2P*6*1000mm structures'}],
            multiplier=1, divisor=1000, rounding=ROUND_NONE,
            site_factor=SITE_FACTOR_MODULE_WP, clamp_at_zero=True, warning_check=CHECK_INC_2P)
        self.design_settings = DesignSettings.objects.get(pk=1)

    # ── helpers ─────────────────────────────────────────────────────────────

    def save(self, sheet=None, site=None):
        """A designer's picker save of the per-item sheet {code: qty}; module lines (made
        with _line) are left as they are, since the POST carries no Modules section."""
        _client_login(self, self.designer)
        return self._picker(sheet or {}, site=site)

    def opx203(self, site=None):
        return self._qty('OPX-203', site)

    def result(self, site=None):
        """The INC 2P Result for the site's stored sheet, as the screens work it out."""
        rules = load_rules('OPEX')
        quantities = {}
        for line in BOQItem.objects.filter(boq__project=site or self.site):
            if line.item_master_id and line.boq_quantity is not None:
                quantities[line.item_master_id] = (quantities.get(line.item_master_id, D(0))
                                                   + line.boq_quantity)
        return evaluate(quantities, rules)[self.items['OPX-203'].pk]

    def picker_row(self, site=None):
        """The picker payload row for OPX-203, or None."""
        page = self.client.get(reverse('opex_boq_entry', kwargs={
            'project_id': (site or self.site).project_id}))
        rows = {row['id']: row for row in json.loads(page.context['added_json'])}
        return page, rows.get(self.items['OPX-203'].pk)


# ═════════════════════════════════════════════════════════════════════════════
# T2 — the module wattage (I1) and the formula
# ═════════════════════════════════════════════════════════════════════════════

class WattageTests(DW6Base):

    def test_a_single_wattage_site_uses_its_own(self):
        self._line(WP[640], 52)
        self.save()
        self.assertEqual(self.opx203(), D('33.28'))          # 52 x 640 / 1000
        self.assertIn("640 Wp is the site's module wattage", self.result().explanation)

    def test_a_550_site_uses_550(self):
        self._line(WP[550], 100)
        self.save()
        self.assertEqual(self.opx203(), D('55.00'))

    def test_a_mixed_site_uses_the_default_and_says_mixed(self):
        self._line(WP[550], 30)
        self._line(WP[640], 20)
        self.save()
        self.assertEqual(self.opx203(), D('32.00'))          # 50 x 640 (the default) / 1000
        self.assertIn("640 Wp is the default wattage, because the site's modules are mixed "
                      "(550 Wp, 640 Wp)", self.result().explanation)

    def test_an_opx001_only_site_uses_the_default(self):
        self._line('OPX-001', 100)
        self.save()
        self.assertEqual(self.opx203(), D('64.00'))
        self.assertIn("640 Wp is the default wattage, because the site's module lines record "
                      "no wattage", self.result().explanation)

    def test_opx001_plus_the_default_wattage_is_that_wattage(self):
        self.design_settings.default_module_wp = 630
        self.design_settings.save()
        self._line('OPX-001', 50)
        self._line(WP[630], 50)
        self.save()
        self.assertEqual(self.opx203(), D('63.00'))          # 100 x 630 / 1000
        self.assertIn("630 Wp is the site's module wattage", self.result().explanation)

    def test_opx001_plus_another_wattage_is_mixed(self):
        self._line('OPX-001', 50)
        self._line(WP[550], 50)
        self.save()
        self.assertEqual(self.opx203(), D('64.00'))
        self.assertIn('mixed (550 Wp, wattage not recorded)', self.result().explanation)

    def test_a_default_change_applies_on_the_next_save_only(self):
        self._line('OPX-001', 100)
        self.save()
        self.assertEqual(self.opx203(), D('64.00'))
        self.design_settings.default_module_wp = 630
        self.design_settings.save()
        self.assertEqual(self.opx203(), D('64.00'))          # no sweep
        _page, row = self.picker_row()
        self.assertEqual(row['stale'], 'Recalculates to 63 on the next save.')
        self.save()
        self.assertEqual(self.opx203(), D('63.00'))

    def test_the_wattage_function(self):
        wp = {1: None, 2: 550, 3: 640}
        self.assertEqual(site_module_wp({}, wp, 640), (640, 'none', ()))
        self.assertEqual(site_module_wp({1: D(5)}, wp, 640), (640, 'not_recorded', (None,)))
        self.assertEqual(site_module_wp({2: D(5)}, wp, 640), (550, 'site', (550,)))
        self.assertEqual(site_module_wp({2: D(5), 3: D(0)}, wp, 640), (550, 'site', (550,)))
        self.assertEqual(site_module_wp({1: D(5), 3: D(5)}, wp, 640), (640, 'site', (640, None)))
        self.assertEqual(site_module_wp({1: D(5), 2: D(5)}, wp, 640),
                         (640, 'mixed', (550, None)))

    def test_with_no_settings_row_the_model_default_is_used(self):
        DesignSettings.objects.all().delete()
        self._line(WP[550], 10)
        self._line(WP[640], 10)
        self.save()
        self.assertEqual(self.opx203(), D('12.80'))          # 20 x 640 / 1000

    def test_the_fallback_is_the_model_default(self):
        self.assertEqual(DEFAULT_MODULE_WP_FALLBACK,
                         DesignSettings._meta.get_field('default_module_wp').default)


class SubtractionTests(DW6Base):

    def test_each_of_3p_other_ballast_and_tin_shade_reduces_the_count(self):
        for code in ('OPX-035', 'OPX-036', 'OPX-037', 'OPX-038'):
            with self.subTest(code=code):
                BOQItem.objects.filter(boq__project=self.site).delete()
                self._line(WP[640], 100)
                self.save({code: 25})
                self.assertEqual(self.opx203(), D('48.00'))   # (100 - 25) x 640 / 1000

    def test_all_four_together(self):
        self._line(WP[640], 100)
        self.save({'OPX-035': 1, 'OPX-036': 2, 'OPX-037': 3, 'OPX-038': 4})
        self.assertEqual(self.opx203(), D('57.60'))           # 90 x 0.64

    def test_the_explanation_writes_the_subtraction_with_minus_signs(self):
        self._line(WP[640], 52)
        self.save({'OPX-038': 12})
        self.assertEqual(
            self.result().explanation,
            f'{SUMMARY}: (52 modules (52 {WP[640]}) − 0 3P structures − 0 Other '
            f'structures − 0 Ballast − 12 Tin Shade) × 640 Wp ÷ 1000 = 25.6 KWp; '
            f"640 Wp is the site's module wattage")


# ═════════════════════════════════════════════════════════════════════════════
# (a) below 0: 0 with a warning, never a refusal
# ═════════════════════════════════════════════════════════════════════════════

class BelowZeroTests(DW6Base):

    def test_designer_path_writes_no_line_shows_the_warning_and_saves(self):
        self._line(WP[640], 10)
        response = self.save({'OPX-038': 20})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self._errors(response))
        self.assertEqual(self._qty('OPX-038'), D(20))         # the save went through
        self.assertEqual(self.opx203(), 'absent')              # D1: a calculated 0, no line
        page, _row = self.picker_row()
        absent = {r['code']: r for r in page.context['calculated_absent']}
        self.assertIn(BELOW_ZERO, absent['OPX-203']['warnings'])
        self.assertTrue(page.context['calculated_absent_warned'])
        self.assertContains(page, 'below 0, so set to 0 KWp')

    def test_an_existing_line_is_removed_on_the_designer_path(self):
        self._line(WP[640], 10)
        self.save()
        self.assertEqual(self.opx203(), D('6.40'))
        self.save({'OPX-038': 20})
        self.assertEqual(self.opx203(), 'absent')

    def test_reviewer_path_keeps_the_line_at_0_and_shows_the_warning(self):
        review = self._review_site('FX-QC6')
        self._line(WP[640], 10, site=review)
        tin = self._line('OPX-038', 1, site=review)
        self._line('OPX-203', D('5.76'), site=review)
        _client_login(self, self.qc)
        response = self._correct(review, 'set_quantity', item_id=str(tin.pk), quantity='20')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._qty('OPX-038', review), D(20))
        self.assertEqual(self._qty('OPX-203', review), D(0))   # kept at 0 (Q4)
        page = self.client.get(reverse('boq_correct', args=[review.project_id]))
        self.assertContains(page, BELOW_ZERO)

    def test_the_engine_still_refuses_below_0_for_a_rule_without_the_clamp(self):
        BOQFormulaRule.objects.filter(pk=self.inc2p.pk).update(clamp_at_zero=False)
        self._line(WP[640], 10)
        response = self.save({'OPX-038': 20})
        self.assertTrue(any('outside what a BOQ line can hold' in e
                            for e in self._errors(response)))
        self.assertEqual(self._qty('OPX-038'), 'absent')       # refused, rolled back


# ═════════════════════════════════════════════════════════════════════════════
# (b) the cross-check
# ═════════════════════════════════════════════════════════════════════════════

class CrossCheckTests(DW6Base):

    def warnings(self):
        return self.result().warning_texts

    def test_equal_raises_nothing(self):
        self._line(WP[640], 18)
        self.save({'OPX-027': 1, 'OPX-030': 1})               # 6 + 12
        self.assertEqual(self.warnings(), [])

    def test_different_names_both_numbers(self):
        self._line(WP[640], 20)
        self.save({'OPX-027': 1, 'OPX-030': 1})
        self.assertEqual(self.warnings(),
                         ['INC 2P counts 20 modules; the 2P structure lines hold 18.'])

    def test_no_2p_lines_has_its_own_wording(self):
        self._line(WP[640], 40)
        self.save()
        self.assertEqual(self.warnings(),
                         ['This BOQ has no 2P structure lines; INC 2P counts 40 modules.'])

    def test_both_0_raises_nothing(self):
        self._line(WP[640], 40)
        self.save({'OPX-038': 40})
        self.assertEqual(self.warnings(), [])

    def test_1p_lines_are_not_counted(self):
        self._line(WP[640], 21)
        self.save({'OPX-027': 1, 'OPX-030': 1, 'OPX-220': 1})  # 18 in 2P + 3 in 1P
        self.assertEqual(self.warnings(),
                         ['INC 2P counts 21 modules; the 2P structure lines hold 18.'])

    def test_below_0_raises_both(self):
        self._line(WP[640], 10)
        self.save({'OPX-038': 20})
        self.assertEqual(self.warnings(), [
            BELOW_ZERO,
            'This BOQ has no 2P structure lines; INC 2P counts −10 modules.'])

    def test_the_wordings(self):
        self.assertEqual(warning_text(WARNING_CROSS_CHECK, 'OPX-203', CHECK_INC_2P, D(72),
                                      D(86)),
                         'INC 2P counts 72 modules; the 2P structure lines hold 86.')
        self.assertEqual(warning_text(WARNING_NO_STRUCTURES, 'OPX-203', CHECK_INC_2P, D(40),
                                      D(0)),
                         'This BOQ has no 2P structure lines; INC 2P counts 40 modules.')
        self.assertEqual(warning_text(WARNING_BELOW_ZERO, 'OPX-203', CHECK_INC_2P, D(-10)),
                         BELOW_ZERO)


# ═════════════════════════════════════════════════════════════════════════════
# (d) overrides, and the warnings on every screen (I5)
# ═════════════════════════════════════════════════════════════════════════════

class OverrideTests(DW6Base):

    def test_an_override_survives_and_suppresses_neither_warning(self):
        self._line(WP[640], 52)
        self.save()
        line = self._lines('OPX-203')[0]
        self._override('set', line, quantity='30', note='Two roofs, surveyed')
        self.assertEqual(self.opx203(), D(30))
        self.save({'OPX-038': 60})                            # now below 0
        self.assertEqual(self.opx203(), D(30))                # the override holds
        self.assertTrue(BOQLineOverride.objects.filter(line=line).exists())
        _page, row = self.picker_row()
        self.assertTrue(row['ovr'])
        self.assertEqual(row['warn'], [
            BELOW_ZERO.replace('−10', '−8'),
            'This BOQ has no 2P structure lines; INC 2P counts −8 modules.'])

    def test_on_two_lines_the_warnings_still_show(self):
        self._line(WP[640], 40)
        self.save()
        self._line('OPX-203', 1)                               # a second line: not calculated
        page = self.client.get(self.picker_url)
        self.assertIn('This BOQ has no 2P structure lines; INC 2P counts 40 modules.',
                      page.context['calc_multi_messages'])


class ScreenTests(DW6Base):
    """The cross-check warning (40 modules, no 2P lines) on each screen in I5."""

    WORDS = 'This BOQ has no 2P structure lines; INC 2P counts 40 modules.'

    def setUp(self):
        super().setUp()
        self.review = self._review_site('FX-QC7')
        for code, qty in ((WP[640], 40), ('FX-INV1', 1), ('FX-DCDB', 1), ('OPX-203', '25.6')):
            self._line(code, qty, site=self.review)

    def test_the_picker_line(self):
        self._line(WP[640], 40)
        self.save()
        page, row = self.picker_row()
        self.assertEqual(row['warn'], [self.WORDS])
        self.assertContains(page, 'boq-rule-warning')

    def test_the_correction_screen(self):
        _client_login(self, self.qc)
        self.assertContains(self.client.get(reverse('boq_correct', args=[self.review.project_id])),
                            self.WORDS)

    def test_the_qc_and_head_panel(self):
        for who in (self.qc, self.head):
            _client_login(self, who)
            with self.subTest(who=who.user.username):
                self.assertContains(self.client.get(
                    reverse('design_qc_review', args=[self.review.project_id])), self.WORDS)

    def test_the_qc_panel_lists_a_warning_with_no_line(self):
        BOQItem.objects.filter(boq__project=self.review, item_master__code='OPX-203').delete()
        _client_login(self, self.qc)
        self.assertContains(self.client.get(
            reverse('design_qc_review', args=[self.review.project_id])),
            f'OPX-203 (not on this BOQ): {self.WORDS}')

    def test_the_boq_view_for_design_users_only(self):
        url = reverse('boq_detail', args=[self.review.project_id])
        _client_login(self, self.qc)
        self.assertContains(self.client.get(url), f'OPX-203: {self.WORDS}')
        _client_login(self, self.scm)
        page = self.client.get(url)
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, 'INC 2P counts')

    def test_mark_complete_is_not_blocked(self):
        self._line(WP[640], 40)
        self._line('FX-INV1', 1)
        self._line('FX-DCDB', 1)
        self.save({'FX-INV1': 1, 'FX-DCDB': 1})
        self.assertEqual(self.result().warning_texts, [self.WORDS])
        self.client.post(reverse('design_boq_complete', args=[self.site.project_id]),
                         {'boq_remarks': ''}, follow=True)
        attempt = self.assignment.attempts.first()
        self.assertIsNotNone(attempt and attempt.boq_submitted_at)


# ═════════════════════════════════════════════════════════════════════════════
# Design groups: screen, Excel, snapshots
# ═════════════════════════════════════════════════════════════════════════════

class GroupTests(DW6Base):

    def setUp(self):
        super().setUp()
        self.low = self._review_site('FX-G6A')                 # 20 modules, 30 Tin Shade
        for code, qty in ((WP[640], 20), ('OPX-038', 30), ('FX-INV1', 1), ('FX-DCDB', 1)):
            self._line(code, qty, site=self.low)
        self.ok = self._review_site('FX-G6B')                  # 12 modules on 2P*6 x 1
        for code, qty in ((WP[640], 12), ('OPX-030', 1), ('FX-INV1', 1), ('FX-DCDB', 1)):
            self._line(code, qty, site=self.ok)
        self.group = DesignBOQGroup.objects.create(name='DW6 G', created_by=self.head)
        for site in (self.low, self.ok):
            DesignBOQGroupMembership.objects.create(group=self.group, project=site,
                                                    added_by=self.head)
        _client_login(self, self.head)
        self.lines = [
            'FX-G6A OPX-203: ' + BELOW_ZERO,
            'FX-G6A OPX-203: This BOQ has no 2P structure lines; INC 2P counts −10 '
            'modules.']

    def test_the_screen_lists_the_warnings_per_site(self):
        page = self.client.get(reverse('design_boq_group_detail', args=[self.group.pk]))
        self.assertEqual(page.status_code, 200)
        for line in self.lines:
            self.assertContains(page, line)
        self.assertNotContains(page, 'FX-G6B OPX-203')

    def test_the_excel_has_them(self):
        response = self.client.get(reverse('design_boq_group_excel', args=[self.group.pk]))
        wb = load_workbook(BytesIO(response.content))
        cells = [str(c) for ws in wb.worksheets for row in ws.iter_rows(values_only=True)
                 for c in row if c]
        for line in self.lines:
            self.assertIn(line, cells)

    def test_a_snapshot_stores_the_facts_and_shows_the_same_words(self):
        self.client.post(reverse('design_boq_group_snapshot_take', args=[self.group.pk]),
                         {'note': ''})
        snap = DesignBOQGroupSnapshot.objects.get()
        self.assertEqual(snap.data['version'], 3)
        self.assertEqual(snap.data['rule_warnings'], [
            {'site': 'FX-G6A', 'code': 'OPX-203', 'check': CHECK_INC_2P,
             'kind': WARNING_BELOW_ZERO, 'modules': '-10', 'structure_modules': None},
            {'site': 'FX-G6A', 'code': 'OPX-203', 'check': CHECK_INC_2P,
             'kind': WARNING_NO_STRUCTURES, 'modules': '-10', 'structure_modules': '0'}])
        page = self.client.get(reverse('design_boq_group_snapshot_detail',
                                       args=[self.group.pk, snap.pk]))
        for line in self.lines:
            self.assertContains(page, line)

    def test_an_older_snapshot_opens_unchanged_and_the_fingerprint_ignores_warnings(self):
        self.client.post(reverse('design_boq_group_snapshot_take', args=[self.group.pk]),
                         {'note': ''})
        snap = DesignBOQGroupSnapshot.objects.get()
        live = snap.data
        old = {k: v for k, v in live.items() if k != 'rule_warnings'}
        old['version'] = 2                                     # DW-1b's shape exactly
        self.assertEqual(hydrate(old)['boq']['rule_warnings'], [])
        self.assertEqual(snapshot_fingerprint(old), snapshot_fingerprint(live))
        self.assertEqual(snap.fingerprint, snapshot_fingerprint(old))
        # Snapshots are never changed, so the version-2 one is a row of its own, stored as
        # DW-1b stored it, and the newest: the group screen compares the live BOQ with it.
        counted = sum(1 for s in old['sites'] if s['counted'])
        older = DesignBOQGroupSnapshot.objects.create(
            group=self.group, taken_by=self.head, taken_by_name='Head', data=old,
            fingerprint=snapshot_fingerprint(old), site_ids=[s['id'] for s in old['sites']],
            sites_counted=counted, sites_not_counted=len(old['sites']) - counted)
        page = self.client.get(reverse('design_boq_group_snapshot_detail',
                                       args=[self.group.pk, older.pk]))
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, 'INC 2P')
        screen = self.client.get(reverse('design_boq_group_detail', args=[self.group.pk]))
        self.assertNotContains(screen, 'id="boqGroupDiffers"')

    def test_the_reader_is_one_query_and_says_what_the_picker_says(self):
        with self.assertNumQueries(1):
            specs = read_warning_rules('OPEX')
        quantities = {self.m(640).pk: D(20), self.items['OPX-038'].pk: D(30)}
        self.assertEqual(sheet_warnings(specs, quantities),
                         list(evaluate(quantities, load_rules('OPEX'))[
                             self.items['OPX-203'].pk].warnings))


# ═════════════════════════════════════════════════════════════════════════════
# The guard, the loader and the file
# ═════════════════════════════════════════════════════════════════════════════

class GuardTests(DW6Base):

    def test_a_warning_rule_may_not_read_a_calculated_line(self):
        driver = BOQFormulaDriver(rule=self.inc2p, item=self.items['OPX-177'], weight=-1)
        with self.assertRaisesMessage(ValidationError, 'raises warnings, so it may read only '
                                                       'lines entered by hand'):
            driver.save()


class LoaderFileTests(SimpleTestCase):

    def test_the_dw6_file_is_the_dw5_file_plus_inc2p(self):
        groups5, rules5 = parse_rules(DW5_FILE.read_bytes())
        groups6, rules6 = parse_rules(DEFAULT_FILE.read_bytes())
        self.assertEqual(groups6, groups5)
        self.assertEqual(rules6[:-1], rules5)
        inc = rules6[-1]
        self.assertEqual((inc['key'], inc['target'], inc['unit'], inc['divisor'],
                          inc['rounding'], inc['site_factor'], inc['clamp_at_zero'],
                          inc['warning_check']),
                         ('INC2P', 'OPX-203', 'KWp', D(1000), ROUND_NONE,
                          SITE_FACTOR_MODULE_WP, True, CHECK_INC_2P))
        summed = [(d.get('group') or d['item'], d['weight']) for d in inc['drivers']
                  if d.get('role') != DRIVER_ROLE_CHECK]
        self.assertEqual(summed, [('all modules', 1), ('OPX-035', -1), ('OPX-036', -1),
                                  ('OPX-037', -1), ('OPX-038', -1)])

    def test_the_cross_check_lines_are_the_25_2p_items_at_2n(self):
        _groups, rules = parse_rules(DEFAULT_FILE.read_bytes())
        checks = {d['item']: (d['weight'], d['label']) for d in rules[-1]['drivers']
                  if d.get('role') == DRIVER_ROLE_CHECK}
        self.assertEqual(len(checks), 25)
        for code, (weight, label) in checks.items():
            with self.subTest(code=code):
                self.assertTrue(label.startswith('2P*'))
                self.assertEqual(weight, 2 * int(label.split('*')[1]))
        self.assertFalse({'OPX-220', 'OPX-221', 'OPX-222', 'OPX-223'} & set(checks))

    def test_files_without_the_new_keys_checksum_as_before(self):
        self.assertEqual(read_file(DW5_FILE)[2],
                         '62a10a92e7ddc2d4a787a38aeb72e7d742d9eb7b604092d550f5c8738c0d2f3c')

    def _file(self, **rule):
        base = {'key': 'X1', 'target': 'OPX-203', 'unit': 'KWp', 'summary': 's',
                'drivers': [{'item': 'OPX-038', 'unit': 'Nos'}], 'multiplier': '1',
                'divisor': '1', 'constant': '0', 'rounding': 'none',
                'floor_one_when_driven': False, 'active': True}
        base.update(rule)
        return json.dumps({'groups': [], 'rules': [base]}).encode()

    def test_the_key_format_accepts_inc2p_and_still_refuses_nonsense(self):
        self.assertEqual(parse_rules(self._file(key='INC2P'))[1][0]['key'], 'INC2P')
        for bad in ('inc2p', 'R', '2P', 'R12-b'):
            with self.subTest(key=bad), self.assertRaises(RulesFileError):
                parse_rules(self._file(key=bad))

    def test_a_check_needs_check_lines_and_check_lines_need_a_check(self):
        with self.assertRaisesMessage(RulesFileError, 'needs at least one driver'):
            parse_rules(self._file(warning_check=CHECK_INC_2P))
        with self.assertRaisesMessage(RulesFileError, 'read only by a warning_check'):
            parse_rules(self._file(drivers=[{'item': 'OPX-027', 'unit': 'Nos',
                                             'role': 'check'}]))
        for name, value in (('site_factor', 'kw'), ('warning_check', 'other'),
                            ('clamp_at_zero', 'yes')):
            with self.subTest(name=name), self.assertRaises(RulesFileError):
                parse_rules(self._file(**{name: value}))


# ═════════════════════════════════════════════════════════════════════════════
# Query pins (P9)
# ═════════════════════════════════════════════════════════════════════════════

#: Measured on 3c2b17f (DW-5) in a scratch worktree with this fixture's screens and DW-5's
#: one rule, then here with the INC 2P rule added. Targets (P9, answer 5): every screen that
#: evaluates the rules is unchanged (the default wattage rides on load_rules()'s rule query
#: as a subquery; the warnings ride on its results); the BOQ view for a Design user, the
#: group screen, its live Excel and the snapshot take add ONE query (read_warning_rules).
BASELINE = {
    'picker GET (designer)':      24,
    'picker save (designer)':     22,
    'upload preview (designer)':  19,
    'BOQ view (designer)':        9,     # a redirect to the picker
    'correction GET (QC)':        27,
    'QC panel (QC)':              38,
    'BOQ view (QC)':              22,
    'QC panel (Head)':            35,
    'design group screen (Head)': 16,
    'design group Excel (Head)':  11,
    'snapshot take (Head)':       18,
    'snapshot view (Head)':       6,
    'snapshot Excel (Head)':      5,
    'BOQ view (SCM)':             17,
}
DW6_EXTRA = {'BOQ view (QC)': 1, 'design group screen (Head)': 1,
             'design group Excel (Head)': 1, 'snapshot take (Head)': 1}
PINS = {name: n + DW6_EXTRA.get(name, 0) for name, n in BASELINE.items()}


class QueryTests(DW6Base):

    def _count(self, call):
        call()
        with CaptureQueriesContext(connection) as ctx:
            call()
        return len(ctx.captured_queries)

    def _measure(self, review, group):
        pid, rid = self.site.project_id, review.project_id
        out = {}
        _client_login(self, self.designer)
        module_post = {'action': 'save_draft', 'modules': '1',
                       'module_item': [str(self.m(640).pk)], 'module_qty': ['100']}
        out['picker GET (designer)'] = self._count(lambda: self.client.get(self.picker_url))
        out['picker save (designer)'] = self._count(
            lambda: self.client.post(self.picker_url, module_post))
        content = self.client.get(reverse('opex_boq_download', args=[pid])).content
        out['upload preview (designer)'] = self._count(lambda: self.client.post(
            reverse('opex_boq_upload', args=[pid]),
            {'phase': 'preview', 'file': SimpleUploadedFile('boq.xlsx', content)}))
        out['BOQ view (designer)'] = self._count(
            lambda: self.client.get(reverse('boq_detail', args=[pid])))
        _client_login(self, self.qc)
        out['correction GET (QC)'] = self._count(
            lambda: self.client.get(reverse('boq_correct', args=[rid])))
        out['QC panel (QC)'] = self._count(
            lambda: self.client.get(reverse('design_qc_review', args=[rid])))
        out['BOQ view (QC)'] = self._count(
            lambda: self.client.get(reverse('boq_detail', args=[rid])))
        _client_login(self, self.head)
        out['QC panel (Head)'] = self._count(
            lambda: self.client.get(reverse('design_qc_review', args=[rid])))
        out['design group screen (Head)'] = self._count(
            lambda: self.client.get(reverse('design_boq_group_detail', args=[group.pk])))
        out['design group Excel (Head)'] = self._count(
            lambda: self.client.get(reverse('design_boq_group_excel', args=[group.pk])))
        # The group's first snapshot, as the baseline measured it (snapshots are never
        # deleted, so each fixture takes exactly one).
        with CaptureQueriesContext(connection) as ctx:
            self.client.post(reverse('design_boq_group_snapshot_take', args=[group.pk]),
                             {'note': 'pin'})
        out['snapshot take (Head)'] = len(ctx.captured_queries)
        snap = DesignBOQGroupSnapshot.objects.filter(group=group).latest('pk')
        out['snapshot view (Head)'] = self._count(lambda: self.client.get(
            reverse('design_boq_group_snapshot_detail', args=[group.pk, snap.pk])))
        out['snapshot Excel (Head)'] = self._count(lambda: self.client.get(
            reverse('design_boq_group_snapshot_excel', args=[group.pk, snap.pk])))
        _client_login(self, self.scm)
        out['BOQ view (SCM)'] = self._count(
            lambda: self.client.get(reverse('boq_detail', args=[rid])))
        return out

    def _fixture(self, extra_sites=0):
        """The baseline's fixture, with the INC 2P rule raising a warning on every site."""
        self._line(WP[640], 100)
        self._line('FX-INV1', 1)
        review = self._review_site('FX-Q')
        self._line(WP[640], 50, site=review)
        self._line('FX-INV1', 1, site=review)
        group = DesignBOQGroup.objects.create(name='DW6 Q', created_by=self.head)
        DesignBOQGroupMembership.objects.create(group=group, project=review, added_by=self.head)
        for n in range(extra_sites):
            site = self._review_site(f'FX-Q{n}')
            for code, qty in ((WP[550], 30), (WP[640], 20), ('OPX-038', 70), ('OPX-030', 2)):
                self._line(code, qty, site=site)
            DesignBOQGroupMembership.objects.create(group=group, project=site,
                                                    added_by=self.head)
        return review, group

    def test_query_counts_are_pinned(self):
        measured = self._measure(*self._fixture())
        print('\nDW-6 measured query counts:', measured)
        self.assertEqual(measured, PINS)
        self.assertTrue(all(measured[k] - BASELINE[k] <= 1 for k in BASELINE))

    def test_no_count_grows_with_sites_or_lines(self):
        review, group = self._fixture(extra_sites=6)
        for code, qty in (('OPX-035', 1), ('OPX-036', 1), ('OPX-037', 1), ('OPX-038', 1),
                          ('OPX-027', 3), ('OPX-030', 3), ('OPX-220', 2)):
            self._line(code, qty, site=review)
        self.assertEqual(self._measure(review, group), PINS)
