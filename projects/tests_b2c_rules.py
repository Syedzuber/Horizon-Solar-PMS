"""
B-2c — Praveen's Excel formula rules as data: the rules file, load_formula_rules,
formula_trial_report, the fix_tin_shade_ballast hook, the catalogue form and the picker text.

What these tests pin:
- every rule in projects/data/b2c_formula_rules_24nov.json against a worked example, loaded
  through the loader from THE REAL FILE (so editing the file is what a red check breaks),
  including R11 74.66 -> 75, R20 with no inverter (4) and with an ACCB (not counted), R16 with
  a 40 mm flexible conduit (not counted), R24/R25 (no rounding, no floor) and the G3 floor;
- the chains (R7, R14, R17, R22, R23) follow their source, also when it is overridden;
- the loader: dry run writes nothing, --apply writes every rule, a second --apply writes
  nothing, refusals (mandatory, retired, unknown code, wrong unit, loop, DEMO rules), the
  checksum is the same for CRLF and LF copies, replace / reactivate / switch off;
- --deactivate: the lines keep their quantities, accept a typed figure, overrides stay, other
  rules still run;
- the trial report: columns, rows, writes nothing;
- fix_tin_shade_ballast --apply-counts recalculates INC work in the same transaction; its dry
  run shows every calculated change; locked or ordered sites get their own section;
- the catalogue form refuses a calculated item as mandatory;
- loading rules changes no existing BOQ line (no catch-up);
- query counts with all of the file's rules loaded.
"""
import csv
import json
import os
import shutil
import tempfile
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.core.exceptions import ValidationError
from django.core.management import CommandError, call_command
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from .boq_formulas import evaluate, load_rules
from .forms import BOQItemMasterForm
from .management.commands import fix_tin_shade_ballast as tsb_command
from .management.commands.formula_trial_report import COLUMNS, IST
from .management.commands.load_formula_rules import (
    DEFAULT_FILE, _db_definition, _file_definition, parse_rules, read_file,
)
from .models import (
    ActivityLog, BOQFormulaDriver, BOQFormulaRule, BOQItem, BOQItemGroup, BOQItemGroupMember,
    BOQItemMaster, BOQLineOverride, BOQRecalculation, VendorOrder, VendorOrderLine,
    VendorOrderSite, FORMULA_SOURCE_TIN_SHADE,
)
from .tests_boq_formula_apply import BASELINE as WRITE_BASELINE, FormulaBase
from .tests_boq_formula_display import (
    WITH_RULES_EXTRA, DisplayBase, screens_expected,
)
from .tests_catalogue_hygiene import _post_data
from .tests_tin_shade_ballast_fix import TsbBase

D = Decimal

#: Codes the examples use that the file may not name (the red check takes OPX-218 out of
#: "all inverters"; it must still exist, so the R13 example fails on its figure, not on a
#: missing item). OPX-144 is the 40 mm flexible conduit R16 must not count.
EXTRA_CODES = {'OPX-218': 'Nos', 'OPX-144': 'Meter', 'OPX-273': 'Nos'}


def rules_data():
    """The real rules file, parsed as JSON (a fresh copy each call, safe to edit)."""
    return json.loads(DEFAULT_FILE.read_text(encoding='utf-8'))


def all_on(data):
    """`data` with every rule active. The real file has every rule on since Q3 (R24/R25
    switched on); kept so these tests do not depend on the file's active flags."""
    for rule in data['rules']:
        rule['active'] = True
    return data


def catalogue_codes(data):
    """{code: unit} for every code the file names, plus EXTRA_CODES."""
    units = dict(EXTRA_CODES)
    for group in data['groups']:
        for code in group['members']:
            units.setdefault(code, 'Nos')
    for rule in data['rules']:
        units[rule['target']] = rule['unit']
        for driver in rule['drivers']:
            if 'item' in driver:
                units[driver['item']] = driver['unit']
    return units


def make_catalogue(data):
    """The RESCO catalogue rows the file needs, in the file's units. Codes already created
    by a base class (OPX-273, Tin Shade fixtures) are left as they are."""
    category = {}
    for group in data['groups']:
        for code in group['members']:
            category[code] = group['label']
    for code, unit in catalogue_codes(data).items():
        BOQItemMaster.objects.get_or_create(code=code, defaults={
            'description': f'{code} item', 'unit': unit, 'project_type': 'OPEX',
            'category': category.get(code, 'BOS'), 'sort_order': int(code[4:]),
            'is_active': True})
    return {m.code: m for m in BOQItemMaster.objects.filter(code__startswith='OPX-')}


class RulesFileMixin:
    """Writing variants of the rules file and running the two commands."""

    def make_tmp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def write_rules(self, data, name='rules.json', crlf=False):
        text = json.dumps(data, indent=2)
        if crlf:
            text = text.replace('\n', '\r\n')
        path = os.path.join(self.tmp, name)
        with open(path, 'w', encoding='utf-8', newline='') as handle:
            handle.write(text)
        return path

    def load(self, *args, path=None):
        out = StringIO()
        call_command('load_formula_rules', '--file', path or str(DEFAULT_FILE), *args,
                     stdout=out)
        return out.getvalue()

    def load_all_on(self, *args):
        """--apply (by default) the real file with every rule switched on."""
        return self.load(*(args or ('--apply',)),
                         path=self.write_rules(all_on(rules_data()), 'all_on.json'))


class B2cBase(RulesFileMixin, FormulaBase):

    def setUp(self):
        super().setUp()
        self.make_tmp()
        self.opx = make_catalogue(rules_data())
        self.keys = {r['target']: r['key'] for r in rules_data()['rules']}
        self.targets = {r['key']: r['target'] for r in rules_data()['rules']}

    def calc(self, sheet, overrides=None):
        """{key: quantity} from the LOADED rules for a sheet {code: qty}. Pure evaluation."""
        rules = load_rules('OPEX')
        pk = {code: m.pk for code, m in self.opx.items()}
        results = evaluate({pk[c]: D(str(q)) for c, q in sheet.items()}, rules,
                           {pk[c]: D(str(q)) for c, q in (overrides or {}).items()})
        return {self.keys[rules.codes[i]]: r.quantity for i, r in results.items()}

    def save(self, quantities, site=None):
        """The picker save with exactly these {OPX code: qty} rows (absence = removal; a
        calculated row may be left out, the picker never deletes it)."""
        data = {'action': 'save_draft', 'item': []}
        for code, quantity in quantities.items():
            pk = self.opx[code].pk
            data['item'].append(str(pk))
            data[f'qty_{pk}'] = '' if quantity is None else str(quantity)
        return self.client.post(reverse('opex_boq_entry', kwargs={
            'project_id': (site or self.site).project_id}), data, follow=True)

    def opx_qty(self, code, site=None):
        lines = list(BOQItem.objects.filter(boq__project=site or self.site,
                                            item_master__code=code))
        self.assertLessEqual(len(lines), 1)
        return lines[0].boq_quantity if lines else 'absent'


# ===========================================================================
# T5 — every rule against a worked example
# ===========================================================================

#: (key, sheet {code: qty}, expected). One example per key at least; the special cases the
#: prompt names have their own tests below as well.
EXAMPLES = [
    ('R1',   {'OPX-175': 2, 'OPX-176': 1}, '15'),
    ('R2a',  {'OPX-130': 100}, '10'), ('R2b', {'OPX-130': 35}, '4'), ('R2c', {'OPX-130': 100}, '10'),
    ('R3a',  {'OPX-259': 45}, '5'), ('R3b', {'OPX-259': 45}, '5'), ('R3c', {'OPX-259': 10}, '1'),
    ('R4a',  {'OPX-263': 35, 'OPX-259': 0}, '4'), ('R4b', {'OPX-263': 35}, '4'),
    ('R4c',  {'OPX-263': 35}, '4'),
    ('R5',   {'OPX-130': 100}, '1'), ('R6', {'OPX-259': 200}, '2'),
    ('R7a',  {'OPX-130': 100, 'OPX-259': 200}, '6'), ('R7b', {'OPX-130': 100, 'OPX-259': 200}, '6'),
    ('R8',   {'OPX-218': 1, 'OPX-012': 2}, '15'),
    ('R9',   {'OPX-168': 281}, '141'),
    ('R10a', {'OPX-170': 3, 'OPX-171': 2}, '5'), ('R10b', {'OPX-170': 3, 'OPX-171': 2}, '5'),
    ('R11',  {'OPX-168': 280}, '75'),
    ('R12',  {'OPX-001': 120}, '240'),
    ('R13',  {'OPX-218': 1, 'OPX-208': 1}, '2'),
    ('R14',  {'OPX-012': 2}, '10'),
    ('R15',  {}, '1'),
    ('R16',  {'OPX-143': 100, 'OPX-059': 200}, '2'), ('R17', {'OPX-143': 100, 'OPX-059': 200}, '2'),
    ('R18a', {}, '1'), ('R18b', {}, '16'), ('R18c', {}, '1'), ('R18d', {}, '1'),
    ('R18e', {}, '1'), ('R18f', {}, '1'),
    ('R19',  {'OPX-187': 42}, '9'),
    ('R20',  {'OPX-218': 1, 'OPX-002': 1, 'OPX-237': 1}, '16'),
    ('R21a', {'OPX-218': 3}, '3'), ('R21b', {'OPX-218': 3}, '3'),
    ('R22',  {'OPX-218': 3}, '6'), ('R23', {'OPX-218': 3}, '3'),
    ('R24',  {'OPX-038': 3}, '1.92'),
    ('R25',  {'OPX-037': 1}, '0.62'),
]


class RuleExampleTests(B2cBase):

    def setUp(self):
        super().setUp()
        self.load_all_on()

    def test_every_key_in_the_file_has_an_example(self):
        self.assertEqual({k for k, _s, _e in EXAMPLES}, set(self.targets))

    def test_every_rule_against_its_worked_example(self):
        for key, sheet, expected in EXAMPLES:
            with self.subTest(key=key, sheet=sheet):
                self.assertEqual(self.calc(sheet)[key], D(expected))

    def test_r11_74_66_rounds_up_to_75(self):
        self.assertEqual(self.calc({'OPX-168': 280})['R11'], D('75'))

    def test_r20_with_no_inverter_is_4(self):
        self.assertEqual(self.calc({})['R20'], D('4'))

    def test_r20_does_not_count_an_accb(self):
        self.assertEqual(self.calc({'OPX-273': 2})['R20'], D('4'))

    def test_r20_counts_dcdbs_and_every_acdb_including_the_10_kw_ones(self):
        # F4-15: the Excel's ACDB range stopped before OPX-237 and left DCDBs out.
        sheet = {'OPX-002': 1, 'OPX-007': 1, 'OPX-237': 1, 'OPX-240': 1, 'OPX-242': 1}
        self.assertEqual(self.calc(sheet)['R20'], D('24'))

    def test_the_10_kw_inverter_counts_in_every_per_inverter_rule(self):
        # F4-14. The red check takes OPX-218 out of "all inverters" and watches this fail.
        result = self.calc({'OPX-218': 1})
        self.assertEqual([result[k] for k in ('R8', 'R13', 'R20', 'R21a', 'R21b')],
                         [D('5'), D('1'), D('8'), D('1'), D('1')])

    def test_r16_does_not_count_a_40_mm_flexible_conduit(self):
        self.assertEqual(self.calc({'OPX-144': 300})['R16'], D('0'))

    def test_r4_comes_from_the_100_mm_pipe_not_the_50_63(self):
        # G2: the Excel's 100 mm fittings read the 50/63 pipe.
        result = self.calc({'OPX-259': 100})
        self.assertEqual((result['R3a'], result['R4a']), (D('10'), D('0')))

    def test_r24_three_tin_shades_is_1_92_and_r25_is_not_rounded(self):
        result = self.calc({'OPX-038': 3, 'OPX-037': D('0.25')})
        self.assertEqual((result['R24'], result['R25']), (D('1.92'), D('0.16')))

    def test_r24_and_r25_have_no_floor(self):
        result = self.calc({'OPX-038': 1, 'OPX-037': 1})
        self.assertEqual((result['R24'], result['R25']), (D('0.64'), D('0.62')))

    def test_g3_at_least_1_only_when_the_driver_is_above_0(self):
        self.assertEqual(self.calc({'OPX-130': 1})['R2a'], D('1'))
        self.assertEqual(self.calc({'OPX-130': 0})['R2a'], D('0'))
        self.assertEqual(self.calc({})['R13'], D('0'))

    # -- chains ------------------------------------------------------------------

    def test_chains_follow_their_source(self):
        result = self.calc({'OPX-130': 200, 'OPX-259': 100, 'OPX-218': 2,
                            'OPX-143': 160, 'OPX-059': 0})
        self.assertEqual(result['R7a'], (result['R5'] + result['R6']) * 2)      # (2 + 1) x 2
        self.assertEqual(result['R7a'], D('6'))
        self.assertEqual(result['R14'], result['R13'] * 5)
        self.assertEqual(result['R17'], result['R16'])
        self.assertEqual((result['R22'], result['R23']), (result['R21a'] * 2, result['R21a']))

    def test_chains_follow_an_overridden_source(self):
        result = self.calc({'OPX-130': 100, 'OPX-218': 1, 'OPX-143': 10},
                           overrides={'OPX-148': 5, 'OPX-184': 3, 'OPX-188': 4, 'OPX-197': 7})
        self.assertEqual((result['R7a'], result['R7b']), (D('10'), D('10')))
        self.assertEqual(result['R14'], D('15'))
        self.assertEqual(result['R17'], D('4'))
        self.assertEqual((result['R22'], result['R23']), (D('14'), D('7')))

    def test_a_designer_override_on_a_chain_source_is_followed_on_save(self):
        self._login(self.designer)
        self.save({'OPX-130': 100})
        self.assertEqual((self.opx_qty('OPX-148'), self.opx_qty('OPX-154')), (D('1'), D('2')))
        saddles = BOQItem.objects.get(boq__project=self.site, item_master__code='OPX-148')
        self._override('set', saddles, quantity='3', note='three runs on the roof')
        self.assertEqual((self.opx_qty('OPX-154'), self.opx_qty('OPX-155')), (D('6'), D('6')))


# ===========================================================================
# T1 — the loader
# ===========================================================================

def _rows():
    """A snapshot of every formula-table row: what a write would change."""
    return (list(BOQFormulaRule.objects.order_by('pk').values_list('pk', 'is_active',
                                                                    'updated_at')),
            list(BOQFormulaDriver.objects.order_by('pk').values_list('pk', 'rule_id')),
            list(BOQItemGroup.objects.order_by('pk').values_list('pk', 'label', 'updated_at')),
            list(BOQItemGroupMember.objects.order_by('pk').values_list('pk', 'item_id')))


class LoaderTests(B2cBase):

    def test_the_dry_run_writes_nothing(self):
        out = self.load()
        self.assertIn('DRY RUN', out)
        # DW-5: six groups — the DW-5 file (now the default) adds "all modules".
        self.assertIn('Summary: rules create 39; groups create 6', out)
        self.assertEqual(_rows(), ([], [], [], []))

    def test_every_rule_in_the_file_is_active_including_r24_and_r25(self):
        # Q3 (Zuber, 24 Nov): R24/R25 switched on; B-2c first shipped them off.
        rules = {r['key']: r['active'] for r in rules_data()['rules']}
        self.assertEqual(len(rules), 39)
        self.assertTrue(all(rules.values()))
        self.assertTrue(rules['R24'] and rules['R25'])

    def test_apply_writes_every_rule_in_the_file(self):
        out = self.load('--apply')
        data = rules_data()
        on = [r for r in data['rules'] if r['active']]
        self.assertEqual(BOQFormulaRule.objects.filter(is_active=True).count(), len(on))
        self.assertFalse(BOQFormulaRule.objects.filter(is_active=False).exists())
        self.assertEqual(BOQFormulaDriver.objects.count(), sum(len(r['drivers']) for r in on))
        self.assertEqual(BOQItemGroup.objects.count(), 6)   # DW-5: + "all modules"
        self.assertEqual(BOQItemGroupMember.objects.count(),
                         sum(len(g['members']) for g in data['groups']))
        _groups, parsed = parse_rules(DEFAULT_FILE.read_bytes())
        for entry in parsed:
            rule = (BOQFormulaRule.objects.filter(target__code=entry['target'], is_active=True)
                    .prefetch_related('drivers__item', 'drivers__group').first())
            with self.subTest(key=entry['key']):
                if entry['active']:
                    self.assertEqual(_db_definition(rule), _file_definition(entry))
                else:
                    self.assertIsNone(rule)
        self.assertIn('Written: 6 group create, 39 create', out)   # DW-5: + "all modules"
        self.assertEqual(ActivityLog.objects.filter(
            action_code='boq_formula_rules_loaded').count(), 1)

    def test_a_second_apply_writes_nothing(self):
        self.load('--apply')
        before, logs = _rows(), ActivityLog.objects.count()
        with CaptureQueriesContext(connection) as ctx:
            out = self.load('--apply')
        writes = [q['sql'] for q in ctx.captured_queries
                  if q['sql'].lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE'))]
        self.assertEqual(writes, [])
        self.assertEqual((_rows(), ActivityLog.objects.count()), (before, logs))
        self.assertIn('Summary: rules same 39; groups same 6', out)   # DW-5: + "all modules"
        self.assertIn('Written: nothing (the database already matches the file)', out)

    def _refused(self, data=None, contains=''):
        path = self.write_rules(data or rules_data(), 'refuse.json')
        with self.assertRaises(CommandError) as raised:
            self.load('--apply', path=path)
        self.assertIn('Nothing was written', str(raised.exception))
        self.assertEqual(_rows(), ([], [], [], []))
        return raised

    def test_it_refuses_a_mandatory_target(self):
        BOQItemMaster.objects.filter(code='OPX-089').update(is_mandatory=True)
        self._refused()

    def test_it_refuses_a_retired_target(self):
        BOQItemMaster.objects.filter(code='OPX-186').update(is_active=False)
        self._refused()

    def test_it_refuses_an_unknown_code(self):
        data = rules_data()
        data['rules'][0]['drivers'] = [{'item': 'OPX-999', 'unit': 'Nos'}]
        self._refused(data)

    def test_it_refuses_a_unit_that_is_not_the_catalogues(self):
        BOQItemMaster.objects.filter(code='OPX-167').update(unit='Nos')
        self._refused()

    def test_it_refuses_a_driver_unit_that_is_not_the_catalogues(self):
        BOQItemMaster.objects.filter(code='OPX-130').update(unit='Nos')
        self._refused()

    def test_it_refuses_a_loop(self):
        data = rules_data()
        r5 = next(r for r in data['rules'] if r['key'] == 'R5')
        r5['drivers'] = [{'item': 'OPX-154', 'unit': 'Pkt', 'label': 'GI screws'}]
        out = StringIO()
        path = self.write_rules(data, 'loop.json')
        with self.assertRaises(CommandError):
            call_command('load_formula_rules', '--file', path, '--apply', stdout=out)
        self.assertIn('loop', out.getvalue())
        self.assertEqual(_rows(), ([], [], [], []))

    def test_it_refuses_while_the_demo_rules_are_there(self):
        BOQFormulaRule(target=self.items['FX-LOG'], summary='DEMO — test', constant=1).save()
        before = BOQFormulaRule.objects.count()
        with self.assertRaises(CommandError):
            self.load('--apply')
        self.assertEqual(BOQFormulaRule.objects.count(), before)

    def test_the_checksum_is_the_same_for_crlf_and_lf_copies(self):
        data = rules_data()
        lf = self.write_rules(data, 'lf.json')
        crlf = self.write_rules(data, 'crlf.json', crlf=True)
        with open(crlf, 'rb') as handle:
            self.assertIn(b'\r\n', handle.read())
        self.assertEqual(read_file(lf)[2], read_file(crlf)[2])
        self.assertEqual(read_file(lf)[2], read_file(DEFAULT_FILE)[2])
        data['rules'][0]['multiplier'] = '6'
        self.assertNotEqual(read_file(self.write_rules(data, 'changed.json'))[2],
                            read_file(lf)[2])

    def test_a_changed_rule_replaces_the_active_one(self):
        self.load('--apply')
        old = BOQFormulaRule.objects.get(target__code='OPX-184', is_active=True)
        data = rules_data()
        next(r for r in data['rules'] if r['key'] == 'R13')['multiplier'] = '2'
        out = self.load('--apply', path=self.write_rules(data, 'r13x2.json'))
        self.assertIn(f'replace    R13', out)
        self.assertIn('multiplier 1 -> 2', out)
        old.refresh_from_db()
        new = BOQFormulaRule.objects.get(target__code='OPX-184', is_active=True)
        self.assertFalse(old.is_active)
        self.assertEqual(new.multiplier, D('2'))

    def test_active_false_in_the_file_switches_a_rule_off(self):
        self.load('--apply')
        self.assertTrue(BOQFormulaRule.objects.filter(target__code='OPX-204',
                                                      is_active=True).exists())
        data = rules_data()
        next(r for r in data['rules'] if r['key'] == 'R24')['active'] = False
        out = self.load('--apply', path=self.write_rules(data, 'r24_off.json'))
        self.assertIn('switch off R24', out)
        self.assertFalse(BOQFormulaRule.objects.filter(target__code='OPX-204',
                                                       is_active=True).exists())

    def test_a_switched_off_rule_is_reactivated_in_its_own_section_above_the_rest(self):
        self.load('--apply')
        wms = BOQFormulaRule.objects.get(target__code='OPX-186', is_active=True)
        self.load('--deactivate', 'R15', '--apply')
        out = self.load()
        self.assertIn('REACTIVATE  R15   OPX-186', out)
        self.assertLess(out.index('REACTIVATE'), out.index('Groups'))
        self.load('--apply')
        wms.refresh_from_db()
        self.assertTrue(wms.is_active)
        self.assertEqual(BOQFormulaRule.objects.filter(target__code='OPX-186').count(), 1)

    def test_a_malformed_file_is_refused_before_the_database_is_read(self):
        data = rules_data()
        data['rules'][1]['key'] = data['rules'][0]['key']
        with self.assertRaises(CommandError) as raised:
            self.load(path=self.write_rules(data, 'dup.json'))
        self.assertIn('listed twice', str(raised.exception))


# ===========================================================================
# C3 — switching a rule off
# ===========================================================================

class SwitchOffTests(B2cBase):

    def setUp(self):
        super().setUp()
        self.load('--apply')
        self._login(self.designer)
        self.save({'OPX-218': 1})
        self.wms = BOQItem.objects.get(boq__project=self.site, item_master__code='OPX-186')

    def test_the_dry_run_writes_nothing_and_counts_the_overrides(self):
        self._override('set', self.wms, quantity='2', note='two arrays')
        before = _rows()
        out = self.load('--deactivate', 'R15')
        self.assertEqual(_rows(), before)
        self.assertIn('1 BOQ line(s) on OPX-186 keep their quantities', out)
        self.assertIn('1 override(s) on those lines stay, unused', out)
        self.assertIn('Would switch off: 1', out)

    def test_lines_keep_their_quantity_and_accept_a_typed_figure(self):
        self.load('--deactivate', 'R15', '--apply')
        self.assertFalse(BOQFormulaRule.objects.filter(target__code='OPX-186',
                                                       is_active=True).exists())
        self.assertEqual(self.opx_qty('OPX-186'), D('1'))
        # An ordinary line now: posted with a typed figure, it is saved as typed, while the
        # other rules still run (a second inverter moves the fasteners to 12).
        self.save({'OPX-218': 2, 'OPX-186': 3})
        self.assertEqual(self.opx_qty('OPX-186'), D('3'))
        self.assertEqual(self.opx_qty('OPX-194'), D('12'))
        self.assertEqual(self.opx_qty('OPX-184'), D('2'))

    def test_an_override_on_a_switched_off_rule_stays_unused(self):
        self._override('set', self.wms, quantity='2', note='two arrays')
        self.load('--deactivate', 'R15', '--apply')
        self.assertTrue(BOQLineOverride.objects.filter(line=self.wms).exists())
        self.save({'OPX-218': 1, 'OPX-186': 4})
        self.assertEqual(self.opx_qty('OPX-186'), D('4'))

    def test_an_unknown_key_is_refused_with_the_lettered_keys(self):
        with self.assertRaises(CommandError) as raised:
            self.load('--deactivate', 'R7')
        self.assertIn('R7 is R7a, R7b', str(raised.exception))


# ===========================================================================
# T2 — the trial report
# ===========================================================================

class ReportTests(B2cBase):

    def setUp(self):
        super().setUp()
        self.load('--apply')
        self._login(self.designer)
        self.save({'OPX-218': 1})
        self.today = timezone.now().astimezone(IST).date().isoformat()

    def report(self, *args):
        out, err = StringIO(), StringIO()
        call_command('formula_trial_report', '--since', self.today, *args, stdout=out,
                     stderr=err)
        return list(csv.DictReader(StringIO(out.getvalue()))), out.getvalue(), err.getvalue()

    def test_it_lists_every_record_since_the_date_with_the_agreed_columns(self):
        rows, text, err = self.report()
        self.assertEqual(text.splitlines()[0].split(','), list(COLUMNS))
        self.assertEqual(len(rows), BOQRecalculation.objects.count())
        self.assertGreater(len(rows), 0)
        self.assertIn(f'{len(rows)} row(s)', err)
        wms = next(r for r in rows if r['item_code'] == 'OPX-186')
        self.assertEqual({k: wms[k] for k in ('site', 'site_deleted', 'tender', 'rule_key',
                                               'before', 'after', 'change', 'path', 'who')},
                         {'site': 'FX-A', 'site_deleted': 'no', 'tender': 'Test-FX',
                          'rule_key': 'R15', 'before': '', 'after': '1', 'change': 'added',
                          'path': 'BOQ picker save', 'who': 'Fxdes (fxdes)'})

    def test_a_later_since_and_another_site_list_nothing(self):
        out = StringIO()
        call_command('formula_trial_report', '--since', '2099-01-01', stdout=out,
                     stderr=StringIO())
        self.assertEqual(out.getvalue().splitlines(), [','.join(COLUMNS)])
        rows, _t, _e = self.report('--site', 'FX-QC')
        self.assertEqual(rows, [])

    def test_it_writes_nothing(self):
        with CaptureQueriesContext(connection) as ctx:
            self.report()
        self.assertTrue(ctx.captured_queries)
        for query in ctx.captured_queries:
            self.assertTrue(query['sql'].lstrip().upper().startswith('SELECT'), query['sql'])

    def test_out_writes_a_file(self):
        path = os.path.join(self.tmp, 'trial.csv')
        call_command('formula_trial_report', '--since', self.today, '--out', path,
                     stdout=StringIO(), stderr=StringIO())
        with open(path, encoding='utf-8-sig', newline='') as handle:
            self.assertEqual(len(list(csv.DictReader(handle))),
                             BOQRecalculation.objects.count())

    def test_until_before_since_is_refused(self):
        with self.assertRaises(CommandError):
            self.report('--until', '2020-01-01')


# ===========================================================================
# T3 — fix_tin_shade_ballast --apply-counts recalculates INC work
# ===========================================================================

class TinShadeHookTests(TsbBase):
    """TsbBase's site A: Tin Shade 186.88 KWp and an INC work Tin Shade line (OPX-204) at
    186.88. With an R24-shaped rule active, writing a count of 12 makes INC work 7.68."""

    def setUp(self):
        super().setUp()
        self.r24 = BOQFormulaRule(target=self.inc, summary='INC work Tin Shade: 0.64 kWp per '
                                  'Tin Shade structure', multiplier=D('0.64'),
                                  rounding='none')
        self.r24.save()
        BOQFormulaDriver(rule=self.r24, item=self.tin, label='Tin Shade structures').save()

    def test_apply_counts_recalculates_inc_work_in_the_same_transaction(self):
        path = self.filled({self.line_a.pk: '12'})
        out = self.run_cmd('--apply-counts', path, '--apply')
        self.line_a.refresh_from_db()
        self.line_inc.refresh_from_db()
        self.assertEqual((self.line_a.boq_quantity, self.line_inc.boq_quantity),
                         (D('12'), D('7.68')))
        record = BOQRecalculation.objects.get()
        self.assertEqual((record.source, record.actor, record.item_code, record.quantity_before,
                          record.quantity_after),
                         (FORMULA_SOURCE_TIN_SHADE, None, 'OPX-204', D('186.88'), D('7.68')))
        self.assertIn('OPX-204 186.88 -> 7.68 KWp', out)

    def test_a_rule_that_fails_rolls_the_count_back(self):
        path = self.filled({self.line_a.pk: '12'})
        with mock.patch.object(tsb_command, 'apply_formulas',
                               side_effect=ValidationError('too large for the column')):
            out = self.run_cmd('--apply-counts', path, '--apply')
        self.line_a.refresh_from_db()
        self.assertEqual(self.line_a.boq_quantity, D('186.88'))
        self.assertEqual(self.line_logs(), [])
        self.assertIn('refused (a formula rule cannot be worked out) 1', out)

    def test_the_dry_run_shows_every_calculated_change_and_writes_nothing(self):
        # A second rule on the site shows the dry run lists every rule's change, not only
        # INC work (Zuber Q2): a constant adds a line.
        wms = BOQItemMaster.objects.create(code='OPX-186', project_type='OPEX', unit='Set',
                                           description='WMS', category='BOS', sort_order=186)
        BOQFormulaRule(target=wms, summary='One WMS per site', constant=1).save()
        path = self.filled({self.line_a.pk: '12'})
        lines = self.snapshot_lines()
        out = self.run_cmd('--apply-counts', path)
        self.assertIn('calculated lines that would change', out)
        self.assertIn('OPX-204 186.88 -> 7.68 KWp', out)
        self.assertIn('OPX-186 added: 1 Set', out)
        self.assertEqual(self.snapshot_lines(), lines)
        self.assertFalse(BOQRecalculation.objects.exists())

    def test_a_locked_and_ordered_site_has_its_own_section(self):
        self._lock(self.site_a)
        order = VendorOrder.objects.create(
            vendor=self.vendor, project_type='OPEX', total_amount=D('5000'),
            created_by=self.scm, client_uuid=__import__('uuid').uuid4())
        VendorOrderSite.objects.create(order=order, project=self.site_a)
        VendorOrderLine.objects.create(order=order, item_master=self.inc, boq_item=self.line_inc,
                                       item_code='OPX-204', item_description='INC work',
                                       item_unit='KWp', quantity=D('4'))
        path = self.filled({self.line_a.pk: '12'})
        out = self.run_cmd('--apply-counts', path, '--allow-lines', str(self.line_a.pk),
                           '--reason', 'Praveen re-counted')
        self.assertIn('on LOCKED OR ORDERED sites (applied through --allow-lines):', out)
        self.assertIn('OPX-204 186.88 -> 7.68 KWp [this line is ordered]', out)
        self.assertNotIn('calculated lines that would change (every', out)
        self.run_cmd('--apply-counts', path, '--allow-lines', str(self.line_a.pk),
                     '--reason', 'Praveen re-counted', '--apply')
        self.line_inc.refresh_from_db()
        self.assertEqual(self.line_inc.boq_quantity, D('7.68'))

    def test_with_no_rules_nothing_is_recalculated(self):
        self.r24.is_active = False
        self.r24.save()
        path = self.filled({self.line_a.pk: '12'})
        self.run_cmd('--apply-counts', path, '--apply')
        self.line_inc.refresh_from_db()
        self.assertEqual(self.line_inc.boq_quantity, D('186.88'))
        self.assertFalse(BOQRecalculation.objects.exists())


# ===========================================================================
# C2 — the catalogue form
# ===========================================================================

class CatalogueFormTests(B2cBase):

    def setUp(self):
        super().setUp()
        self.load('--apply')

    def _form(self, code):
        item = self.opx[code]
        return BOQItemMasterForm(data=_post_data(item, is_mandatory='on', project_type='OPEX'),
                                 instance=item)

    def test_a_calculated_item_cannot_be_marked_mandatory(self):
        form = self._form('OPX-186')
        self.assertFalse(form.is_valid())
        self.assertIn('calculated by a formula rule', ' '.join(form.errors['is_mandatory']))

    def test_an_item_that_is_not_calculated_can(self):
        self.assertTrue(self._form('OPX-130').is_valid(), self._form('OPX-130').errors)

    def test_once_the_rule_is_off_it_can(self):
        self.load('--deactivate', 'R15', '--apply')
        self.assertTrue(self._form('OPX-186').is_valid())


# ===========================================================================
# No catch-up, and the picker's upload text (C6)
# ===========================================================================

class NoCatchUpTests(B2cBase):

    def test_loading_rules_changes_no_existing_boq_line(self):
        for code, quantity in (('OPX-218', 2), ('OPX-184', 5), ('OPX-194', 72), ('OPX-148', 7)):
            self._line(code, quantity)
        before = list(BOQItem.objects.order_by('pk').values_list('pk', 'boq_quantity'))
        self.load('--apply')
        self.assertEqual(list(BOQItem.objects.order_by('pk').values_list('pk', 'boq_quantity')),
                         before)
        self.assertFalse(BOQRecalculation.objects.exists())
        # The next save brings them up to date (calculated rows are not posted; the picker
        # keeps them).
        self._login(self.designer)
        self.save({'OPX-218': 2})
        self.assertEqual((self.opx_qty('OPX-184'), self.opx_qty('OPX-194')), (D('2'), D('12')))


class PickerTextTests(B2cBase):

    def test_the_upload_line_no_longer_says_it_never_removes_anything(self):
        self._login(self.designer)
        page = self.client.get(reverse('opex_boq_entry',
                                       kwargs={'project_id': self.site.project_id}))
        self.assertNotContains(page, 'it never removes anything')
        self.assertContains(page, 'one that now works out to 0')


# ===========================================================================
# P8 — query counts with every rule in the file loaded
# ===========================================================================

class AllRulesScreenQueryTests(RulesFileMixin, DisplayBase):

    def test_every_screen_costs_the_baseline_plus_five_with_the_files_rules(self):
        self.make_tmp()
        make_catalogue(rules_data())
        self.load('--apply')
        self._sheets()
        self.assertEqual(self._screens(),
                         screens_expected(WITH_RULES_EXTRA))


#: B-2c, measured with the file's 39 active rules on this fixture, a save in which no
#: calculated line changes (the measured call is the second, identical one). Each POST adds
#: the exists() gate, the BOQ lock, load_rules()'s three and apply_formulas()'s read of the
#: sheet; a landing page adds the screens' five. Fixed whatever the number of rules or
#: lines; a save that changes calculated lines adds one write per changed line and one
#: INSERT for their records. set_quantity has one more: with rules it re-reads the line under
#: the lock (views.boq_correct, item.refresh_from_db, B-2b), so a concurrent save is seen.
WRITE_WITH_RULES_EXTRA = {'picker': 6 + WITH_RULES_EXTRA, 'upload': 6,
                          'add_line': 6 + WITH_RULES_EXTRA,
                          'set_quantity': 7 + WITH_RULES_EXTRA}


class AllRulesWriteQueryTests(B2cBase):

    def setUp(self):
        super().setUp()
        self.load('--apply')
        self.review = self._review_site()
        self._line('FX-PANEL', 10, site=self.review)

    def _count(self, call):
        call()
        with CaptureQueriesContext(connection) as ctx:
            call()
        return len(ctx)

    def test_picker(self):
        self._login(self.designer)
        n = self._count(lambda: self._picker({'FX-LA1': 2, 'FX-EARTH': 7}))
        self.assertEqual(n, WRITE_BASELINE['picker'] + WRITE_WITH_RULES_EXTRA['picker'])

    def test_upload(self):
        self._login(self.designer)
        self._upload([('FX-LA1', 3), ('FX-EARTH', 11)])   # first save adds the constants
        url, rows_json = self._upload_rows_json([('FX-LA1', 3), ('FX-EARTH', 11)])
        # DW-7b: a confirm that changes nothing is refused before any write, so the warm-up
        # and the measured confirm each change FX-LA1, which drives none of the file's rules:
        # still a save in which no calculated line changes, plus its one UPDATE.
        payloads = iter([rows_json.replace('"3"', '"4"'), rows_json.replace('"3"', '"5"')])
        n = self._count(lambda: self.client.post(url, {'phase': 'commit',
                                                        'rows_json': next(payloads)}))
        self.assertEqual(n, WRITE_BASELINE['upload'] + WRITE_WITH_RULES_EXTRA['upload'] + 1)

    def test_add_line(self):
        self._login(self.qc)
        self._correct(self.review, 'set_quantity',
                      item_id=str(self._lines('FX-PANEL', self.review)[0].pk), quantity='11')
        codes = iter(['FX-EARTH', 'FX-LOG'])
        n = self._count(lambda: self._correct(
            self.review, 'add_line', master_id=str(self.items[next(codes)].pk), quantity='9'))
        self.assertEqual(n, WRITE_BASELINE['add_line'] + WRITE_WITH_RULES_EXTRA['add_line'])

    def test_set_quantity(self):
        self._login(self.qc)
        line = self._lines('FX-PANEL', self.review)[0]
        quantities = iter(['12', '13', '14'])
        self._correct(self.review, 'set_quantity', item_id=str(line.pk),
                      quantity=next(quantities))
        n = self._count(lambda: self._correct(self.review, 'set_quantity',
                                              item_id=str(line.pk), quantity=next(quantities)))
        self.assertEqual(n, WRITE_BASELINE['set_quantity']
                         + WRITE_WITH_RULES_EXTRA['set_quantity'])
