"""
DW-5 — modules by wattage, the "all modules" group, and the Head's default wattage.

Rulings DW-R11, DW-R14, answers 9 and 11; Zuber's pre-flight answers of 27 Nov (Q1-Q14).

  * THE COMMAND (apply_dw5_modules): the dry run writes nothing; --apply creates the four
    items (is_module, module_wp, positions, logged), marks OPX-001 a module, retires it and
    clears its mandatory flag, creates the settings row, creates "all modules" and replaces
    R12 only; a second --apply writes nothing; a file that would change another rule is
    refused whole; an OPX-001-only site's earthing sets are the same before and after (Q9).
  * THE GROUP (M3, option b): OPX-001 and the four items are members; a module item the Head
    adds later joins it with no rules-file change, and the loader never drops it (open);
    OPX-177 = 2 x every module line on a mixed site.
  * THE POP-UP (M7): three wattages write three lines; a count of 0 removes one; a wattage
    chosen twice is refused (two rows, or a row and a move); moving the OPX-001 count to a
    wattage removes the OPX-001 line and keeps the total; the rules run once; a save without
    the Modules section leaves every module line alone; the page marks Edit modules data-ovr
    (off while unsaved) and its show handler refuses while dirty.
  * MINIMUM CONTENT (M6): Mark complete refuses with no module line and passes with any
    wattage line or an OPX-001 line with a quantity; the group screen and its Excel warn; a
    missing group skips the test and logs.
  * NOTHING IS PUT BACK BLANK (picker, copy, upload); the DW-7 download marks wattage rows
    "At least one module", lists OPX-001 lines on Other lines, and its round trip changes
    nothing; an older download carrying an OPX-001 row is refused with the P7 words.
  * THE UPLOAD AND A REVIEWER'S QUANTITY CORRECTION still work on wattage lines (Q5); a
    reviewer cannot add a module line (M9).
  * DESIGN SETTINGS (M8): Head only; before/after logged; the pop-up's preselection follows;
    a wattage with no item is refused; the default's item cannot be retired (Q13).
  * THE CATALOGUE FORM: module fields need the Module category and a wattage; one item per
    wattage (Q4); fixed once a BOQ line uses the item (Q3).
  * EXISTING OPX-001 LINES show on every screen and in group totals, as a retired item.
  * QUERY PINS (P11), flat in the number of module lines.

RUNNER: `manage.py test projects.tests_dw5_modules --settings=solarpms.test_settings`.
"""
import json
import os
import shutil
import tempfile
from decimal import Decimal
from io import BytesIO, StringIO
from pathlib import Path
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import CommandError, call_command
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from openpyxl import load_workbook

from . import models as models_module
from . import views as views_module
from .boq_formulas import ROUND_UP
from .boq_modules import (
    MODULE_RETIRED_COPY_REASON, MODULE_REVIEWER_ADD_MESSAGE, WATTAGE_NOT_RECORDED,
    plan_module_save,
)
from .management.commands.apply_dw5_modules import MODULE_ITEMS
from .management.commands.load_formula_rules import (
    DEFAULT_FILE, parse_rules, plan_load,
)
from .models import (
    ActivityLog, BOQ, BOQFormulaDriver, BOQFormulaRule, BOQItem, BOQItemGroup,
    BOQItemGroupMember, BOQItemMaster, BOQRecalculation, DesignBOQGroup,
    DesignBOQGroupMembership, DesignSettings, MODULE_GROUP_NAME, boq_minimum_content_rules,
)
from .tests_b2c_rules import make_catalogue, rules_data
from .tests_boq_formula_apply import D, FormulaBase, _profile
from .views import BOQ_DOWNLOAD_OFF_SHEET, BOQ_DOWNLOAD_SHEET

B2C_FILE = Path(DEFAULT_FILE).parent / 'b2c_formula_rules_24nov.json'
WP = {wp: code for code, wp, _position in MODULE_ITEMS}      # 550 -> 'OPX-275', ...
REQUIRED_COL = 6    # Code, Category, Description, Unit, Quantity, Calculated, Required


def _client_login(test, profile):
    test.assertTrue(test.client.login(username=profile.user.username, password='x'))


# ═════════════════════════════════════════════════════════════════════════════
# The DW-5 state, built directly (what apply_dw5_modules leaves on a database)
# ═════════════════════════════════════════════════════════════════════════════

class DW5Base(FormulaBase):
    """FormulaBase's site, designer, QC and Head, plus the DW-5 catalogue and rules:

        OPX-001 Solar PV Module      is_module, no wattage, RETIRED, not mandatory
        OPX-275..278                 550 / 620 / 630 / 640 Wp, positions 12-18
        OPX-177 earthing set         = 2 x all modules (R12), at least 1 when driven
        all inverters / DCDBs / ACDBs on FX-INV1/2, FX-DCDB, FX-ACDB (DW-1's groups)
        design settings              640 Wp
    """

    def setUp(self):
        super().setUp()
        make = BOQItemMaster.objects.create
        self.items['OPX-001'] = make(
            code='OPX-001', description='Solar PV Module', unit='Nos', category='Module',
            project_type='OPEX', sort_order=10, is_active=False, is_module=True)
        for code, wp, position in MODULE_ITEMS:
            self.items[code] = make(
                code=code, description=f'Solar PV Module {wp} Wp', unit='Nos',
                category='Module', project_type='OPEX', sort_order=position,
                is_module=True, module_wp=wp)
        self.items['OPX-177'] = make(
            code='OPX-177', description='Module earthing set', unit='Set',
            category='Earthing', project_type='OPEX', sort_order=1770)
        self.items['FX-DCDB'] = make(code='FX-DCDB', description='DCDB 2 in 2 out', unit='Nos',
                                     category='DCDB', project_type='OPEX', sort_order=9101)
        self.items['FX-ACDB'] = make(code='FX-ACDB', description='ACDB 63 A', unit='Nos',
                                     category='ACDB', project_type='OPEX', sort_order=9102)
        self.modules = self._group(MODULE_GROUP_NAME, 'modules',
                                   ['OPX-001'] + [code for code, _wp, _p in MODULE_ITEMS])
        self._group('all inverters', 'inverters', ['FX-INV1', 'FX-INV2'])
        self._group('all DCDBs', 'DCDBs', ['FX-DCDB'])
        self._group('all ACDBs', 'ACDBs', ['FX-ACDB'])
        self.r12 = self._rule('OPX-177', '2 module earthing sets per module',
                              [{'group': self.modules}], multiplier=2, rounding=ROUND_UP,
                              floor_one_when_driven=True)
        DesignSettings.objects.create(pk=1, default_module_wp=640)
        self.scm = _profile('dw5scm', 'SCM')
        self.picker_url = reverse('opex_boq_entry', kwargs={'project_id': self.site.project_id})
        models_module._MINIMUM_CONTENT_LOGGED.clear()

    def m(self, wp):
        return self.items[WP[wp]]

    def _modules(self, rows=(), others=(), sheet=None, action='save_draft', site=None):
        """Save the picker with the Modules section: rows [(wp, count)], others
        [(line, count, move-to wp or None)], and the per-item sheet {code: qty}."""
        data = {'action': action, 'item': [], 'modules': '1', 'module_item': [],
                'module_qty': [], 'module_line': []}
        for code, quantity in (sheet or {}).items():
            pk = self.items[code].pk
            data['item'].append(str(pk))
            data[f'qty_{pk}'] = str(quantity)
        for wp, count in rows:
            data['module_item'].append(str(self.m(wp).pk))
            data['module_qty'].append(str(count))
        for line, count, to in others:
            data['module_line'].append(str(line.pk))
            data[f'module_line_qty_{line.pk}'] = str(count)
            data[f'module_line_to_{line.pk}'] = '' if to is None else str(self.m(to).pk)
        url = reverse('opex_boq_entry', kwargs={'project_id': (site or self.site).project_id})
        return self.client.post(url, data, follow=True)

    def _module_lines(self, site=None):
        """{code: quantity} of the module lines on the site's BOQ."""
        return {line.item_master.code: line.boq_quantity for line in
                BOQItem.objects.filter(boq__project=site or self.site,
                                       item_master__is_module=True).select_related('item_master')}

    def _errors(self, response):
        return [str(m) for m in response.context['messages'] if m.level_tag == 'danger'
                or m.level == 40]

    def _mixed_site(self):
        """OPX-001 100 (retired), 550 Wp 30, 640 Wp 20, and an inverter and a DCDB."""
        self.legacy = self._line('OPX-001', 100)
        self._line(WP[550], 30)
        self._line(WP[640], 20)
        self._line('FX-INV1', 1)
        self._line('FX-DCDB', 1)


# ═════════════════════════════════════════════════════════════════════════════
# T2 — the command
# ═════════════════════════════════════════════════════════════════════════════

class CommandTests(FormulaBase):
    """The production command on the pre-DW-5 state: the B-2c catalogue and rules loaded,
    OPX-001 active and mandatory, one site with an OPX-001 line."""

    def setUp(self):
        super().setUp()
        data = rules_data()
        make_catalogue(json.loads(B2C_FILE.read_text(encoding='utf-8')))
        # Positions in steps of 10, as DW-4 left production (OPX-001 at 10, OPX-002 at 20).
        for item in BOQItemMaster.objects.filter(code__startswith='OPX-').exclude(code='OPX-273'):
            item.sort_order = int(item.code[4:]) * 10
            item.save(update_fields=['sort_order'])
        BOQItemMaster.objects.filter(code='OPX-001').update(
            category='Module', description='Solar PV Module', is_mandatory=True)
        out = StringIO()
        call_command('load_formula_rules', '--file', str(B2C_FILE), '--apply', stdout=out)
        self.assertIn('Written:', out.getvalue())
        self.assertNotIn('all modules', json.dumps(json.loads(B2C_FILE.read_text())['groups']))
        self.assertTrue(any(g['name'] == MODULE_GROUP_NAME for g in data['groups']))
        self.legacy = BOQItemMaster.objects.get(code='OPX-001')
        self.items['OPX-001'] = self.legacy
        self.items['OPX-177'] = BOQItemMaster.objects.get(code='OPX-177')
        self.old_line = self._line('OPX-001', 100)

    def run_command(self, *args):
        out = StringIO()
        call_command('apply_dw5_modules', *args, stdout=out)
        return out.getvalue()

    def state(self):
        return (BOQItemMaster.objects.count(), BOQItemGroup.objects.count(),
                BOQItemGroupMember.objects.count(), BOQFormulaRule.objects.count(),
                BOQFormulaRule.objects.filter(is_active=True).count(),
                ActivityLog.objects.count(), DesignSettings.objects.count(),
                tuple(BOQItemMaster.objects.filter(code='OPX-001').values_list(
                    'is_active', 'is_mandatory', 'is_module').get()),
                tuple(BOQItem.objects.values_list('pk', 'boq_quantity').order_by('pk')))

    def test_the_dry_run_writes_nothing(self):
        before = self.state()
        out = self.run_command()
        self.assertIn('DRY RUN', out)
        self.assertIn('create  OPX-278  Solar PV Module 640 Wp  (position 18)', out)
        self.assertIn('rule    replace R12 OPX-177', out)
        self.assertIn('Would write:', out)
        self.assertEqual(self.state(), before)

    def test_apply_creates_the_four_items_retires_opx001_and_logs(self):
        out = self.run_command('--apply')
        self.assertIn('Written:', out)
        for code, wp, position in MODULE_ITEMS:
            item = BOQItemMaster.objects.get(code=code)
            self.assertEqual(
                (item.description, item.category, item.unit, item.project_type,
                 item.sort_order, item.is_active, item.is_module, item.module_wp,
                 item.is_mandatory),
                (f'Solar PV Module {wp} Wp', 'Module', 'Nos', 'OPEX', position, True, True,
                 wp, False))
        legacy = BOQItemMaster.objects.get(code='OPX-001')
        self.assertEqual((legacy.is_active, legacy.is_mandatory, legacy.is_module,
                          legacy.module_wp), (False, False, True, None))
        group = BOQItemGroup.objects.get(name=MODULE_GROUP_NAME)
        self.assertEqual(sorted(group.members.values_list('item__code', flat=True)),
                         ['OPX-001', 'OPX-275', 'OPX-276', 'OPX-277', 'OPX-278'])
        rule = BOQFormulaRule.objects.get(target__code='OPX-177', is_active=True)
        self.assertEqual([(d.item_id, d.group.name) for d in rule.drivers.all()],
                         [(None, MODULE_GROUP_NAME)])
        # The old R12 row is retired and kept; no other rule changed.
        self.assertEqual(list(BOQFormulaRule.objects.filter(is_active=False)
                              .values_list('target__code', flat=True)), ['OPX-177'])
        self.assertEqual(BOQFormulaRule.objects.filter(is_active=True).count(), 39)
        self.assertEqual(DesignSettings.objects.get(pk=1).default_module_wp, 640)
        codes = list(ActivityLog.objects.values_list('action_code', flat=True))
        self.assertEqual(codes.count('design_boq_item_created'), 4)
        self.assertIn('design_boq_item_deactivated', codes)
        self.assertIn('boq_formula_rules_loaded', codes)
        self.assertIn('dw5_modules_applied', codes)
        created = ActivityLog.objects.filter(action_code='design_boq_item_created').first()
        self.assertIn('module: → yes', created.action)
        retired = ActivityLog.objects.get(action_code='design_boq_item_deactivated').action
        self.assertIn('active: yes → no', retired)
        self.assertIn('mandatory: yes → no', retired)
        # The OPX-001 line is untouched.
        self.old_line.refresh_from_db()
        self.assertEqual((self.old_line.item_master_id, self.old_line.boq_quantity),
                         (legacy.pk, D(100)))

    def test_a_second_apply_changes_nothing(self):
        self.run_command('--apply')
        before = self.state()
        out = self.run_command('--apply')
        self.assertIn('Nothing to do', out)
        self.assertEqual(self.state(), before)
        loader = StringIO()
        call_command('load_formula_rules', stdout=loader)
        self.assertIn('Summary: rules same 39; groups same 6', loader.getvalue())

    def test_a_file_that_would_change_another_rule_is_refused_whole(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        data = rules_data()
        next(r for r in data['rules'] if r['key'] == 'R1')['multiplier'] = '6'
        path = os.path.join(tmp, 'rules.json')
        Path(path).write_text(json.dumps(data), encoding='utf-8')
        before = self.state()
        with self.assertRaisesMessage(CommandError, 'rule R1 (replace)'):
            self.run_command('--apply', '--file', path)
        self.assertEqual(self.state(), before)

    def test_existing_earthing_sets_are_unchanged_on_an_opx001_only_site(self):
        """Q9 (Zuber): R12's replace changes no figure where OPX-001 is the only module."""
        _client_login(self, self.designer)
        url = reverse('opex_boq_entry', kwargs={'project_id': self.site.project_id})
        self.client.post(url, {'action': 'save_draft', 'item': [str(self.legacy.pk)],
                               f'qty_{self.legacy.pk}': '100'})
        self.assertEqual(self._qty('OPX-177'), D(200))
        self.run_command('--apply')
        # The next save, now through the Modules section (OPX-001 retired, its line kept).
        self.client.post(url, {'action': 'save_draft', 'modules': '1',
                               'module_line': [str(self.old_line.pk)],
                               f'module_line_qty_{self.old_line.pk}': '100',
                               f'module_line_to_{self.old_line.pk}': ''})
        self.assertEqual(self._qty('OPX-001'), D(100))
        self.assertEqual(self._qty('OPX-177'), D(200))


# ═════════════════════════════════════════════════════════════════════════════
# T3 — the group and R12
# ═════════════════════════════════════════════════════════════════════════════

class GroupTests(DW5Base):

    def test_the_group_holds_opx001_and_the_four_wattages(self):
        self.assertEqual(
            sorted(self.modules.members.values_list('item__code', flat=True)),
            ['OPX-001', 'OPX-275', 'OPX-276', 'OPX-277', 'OPX-278'])

    def test_a_module_item_the_head_adds_joins_the_group_without_a_rules_change(self):
        _client_login(self, self.head)
        response = self.client.post(reverse('design_boq_catalogue_create'), {
            'code': 'OPX-279', 'description': 'Solar PV Module 580 Wp', 'unit': 'Nos',
            'category': 'Module', 'is_active': 'on', 'sort_order': '13',
            'is_module': 'on', 'module_wp': '580'}, follow=True)
        self.assertEqual(response.status_code, 200)
        item = BOQItemMaster.objects.get(code='OPX-279')
        self.assertEqual((item.is_module, item.module_wp, item.project_type),
                         (True, 580, 'OPEX'))
        self.assertTrue(self.modules.members.filter(item=item).exists())
        self.assertTrue(ActivityLog.objects.filter(action_code='boq_group_member_added',
                                                   entity_id=item.pk).exists())
        # Offered in the pop-up straight away.
        _client_login(self, self.designer)
        data = self.client.get(self.picker_url).context['module_data']
        self.assertIn('580 Wp', [c['label'] for c in data['choices']])
        # And the loader keeps it: the file's group is open.
        groups, rules = parse_rules(json.dumps({'groups': [
            {'name': MODULE_GROUP_NAME, 'label': 'modules', 'open': True,
             'members': ['OPX-001']}], 'rules': []}).encode())
        step = plan_load(groups, rules).groups[0]
        self.assertEqual(step.remove, [])

    def test_a_closed_group_would_drop_it(self):
        """The control for the test above: without "open" the loader removes members."""
        groups, rules = parse_rules(json.dumps({'groups': [
            {'name': MODULE_GROUP_NAME, 'label': 'modules', 'members': ['OPX-001']}],
            'rules': []}).encode())
        self.assertEqual(plan_load(groups, rules).groups[0].remove,
                         ['OPX-275', 'OPX-276', 'OPX-277', 'OPX-278'])

    def test_opx177_is_twice_every_module_line_on_a_mixed_site(self):
        self._mixed_site()
        _client_login(self, self.designer)
        self._modules(rows=[(550, 30), (640, 20)], others=[(self.legacy, 100, None)],
                      sheet={'FX-INV1': 1, 'FX-DCDB': 1})
        self.assertEqual(self._module_lines(),
                         {'OPX-001': D(100), WP[550]: D(30), WP[640]: D(20)})
        self.assertEqual(self._qty('OPX-177'), D(300))


# ═════════════════════════════════════════════════════════════════════════════
# T5 — the Modules pop-up
# ═════════════════════════════════════════════════════════════════════════════

class PopupTests(DW5Base):

    def setUp(self):
        super().setUp()
        _client_login(self, self.designer)

    def test_three_wattages_write_three_lines(self):
        self._modules(rows=[(550, 10), (620, 20), (640, 30)])
        self.assertEqual(self._module_lines(),
                         {WP[550]: D(10), WP[620]: D(20), WP[640]: D(30)})
        line = self._lines(WP[640])[0]
        self.assertEqual((line.description, line.uom, line.category, line.serial_no,
                          line.is_standard_item), ('Solar PV Module 640 Wp', 'Nos', 'Module',
                                                   18, True))
        self.assertEqual(self._qty('OPX-177'), D(120))

    def test_a_count_of_0_removes_its_line(self):
        self._modules(rows=[(550, 10), (640, 30)])
        self._modules(rows=[(550, 0), (640, 30)])
        self.assertEqual(self._module_lines(), {WP[640]: D(30)})
        self._modules(rows=[])
        self.assertEqual(self._module_lines(), {})
        self.assertEqual(self._qty('OPX-177'), 'absent')

    def test_a_wattage_chosen_twice_is_refused_and_nothing_is_saved(self):
        self._modules(rows=[(640, 5)])
        response = self._modules(rows=[(640, 10), (640, 20)], sheet={'FX-INV1': 1})
        self.assertIn('640 Wp is chosen twice. Each wattage can appear once: put its whole '
                      'count on one row.', self._errors(response))
        self.assertIn('Nothing was saved.', self._errors(response))
        self.assertEqual(self._module_lines(), {WP[640]: D(5)})
        self.assertEqual(self._qty('FX-INV1'), 'absent')

    def test_a_move_onto_a_wattage_that_has_a_row_is_refused(self):
        legacy = self._line('OPX-001', 100)
        response = self._modules(rows=[(620, 10)], others=[(legacy, 100, 620)])
        self.assertIn('620 Wp is chosen twice. Each wattage can appear once: put its whole '
                      'count on one row.', self._errors(response))
        self.assertEqual(self._module_lines(), {'OPX-001': D(100)})

    def test_moving_the_opx001_count_to_a_wattage_removes_it_and_keeps_the_total(self):
        legacy = self._line('OPX-001', 100)
        self._line(WP[640], 20)
        self._modules(rows=[(640, 20)], others=[(legacy, 100, None)])
        self.assertEqual(self._qty('OPX-177'), D(240))
        self._modules(rows=[(640, 20)], others=[(legacy, 100, 620)])
        self.assertEqual(self._module_lines(), {WP[620]: D(100), WP[640]: D(20)})
        self.assertEqual(self._qty('OPX-177'), D(240))

    def test_the_opx001_count_can_be_edited_or_set_to_0(self):
        legacy = self._line('OPX-001', 100)
        self._modules(others=[(legacy, 80, None)])
        self.assertEqual(self._module_lines(), {'OPX-001': D(80)})
        self._modules(others=[(legacy, 0, None)])
        self.assertEqual(self._module_lines(), {})

    def test_the_rules_recalculate_once_per_save(self):
        with mock.patch.object(views_module, 'apply_formulas',
                               wraps=views_module.apply_formulas) as spy:
            self._modules(rows=[(550, 10), (620, 20), (640, 30)])
        self.assertEqual(spy.call_count, 1)
        self.assertEqual(BOQRecalculation.objects.filter(
            boq__project=self.site, item_code='OPX-177').count(), 1)

    def test_a_save_without_the_modules_section_leaves_module_lines_alone(self):
        """A page drawn before DW-5, or a hand-built POST: no `modules=1`."""
        legacy = self._line('OPX-001', 100)
        self._line(WP[640], 20)
        self.client.post(self.picker_url, {
            'action': 'save_draft', 'item': [str(self.items['FX-INV1'].pk),
                                             str(self.m(640).pk)],
            f'qty_{self.items["FX-INV1"].pk}': '2', f'qty_{self.m(640).pk}': '99'})
        self.assertEqual(self._module_lines(), {'OPX-001': D(100), WP[640]: D(20)})
        self.assertEqual(self._qty('FX-INV1'), D(2))
        legacy.refresh_from_db()
        self.assertEqual(legacy.boq_quantity, D(100))

    def test_a_blank_saved_line_posted_back_blank_is_unchanged(self):
        line = self._line(WP[640], None)
        response = self.client.post(self.picker_url, {
            'action': 'save_draft', 'modules': '1', 'module_item': [str(self.m(640).pk)],
            'module_qty': ['']}, follow=True)
        self.assertNotIn('Nothing was saved.', self._errors(response))
        line.refresh_from_db()
        self.assertIsNone(line.boq_quantity)

    def test_a_new_row_needs_a_count_and_a_wattage(self):
        response = self.client.post(self.picker_url, {
            'action': 'save_draft', 'modules': '1',
            'module_item': [str(self.m(640).pk), ''], 'module_qty': ['', '5']}, follow=True)
        errors = self._errors(response)
        self.assertIn('Enter a count for 640 Wp, or set it to 0 to remove it.', errors)
        self.assertIn('Choose a wattage on every module row.', errors)
        self.assertEqual(self._module_lines(), {})

    def test_the_page_draws_the_block_the_data_and_the_dirty_guard(self):
        self._line('OPX-001', 100)
        self._line(WP[640], 20)
        response = self.client.get(self.picker_url)
        self.assertEqual([(r['code'], r['label'], r['qty']) for r in response.context['module_block']],
                         [('OPX-001', WATTAGE_NOT_RECORDED, D(100)),
                          (WP[640], '640 Wp', D(20))])
        data = response.context['module_data']
        self.assertEqual([c['label'] for c in data['choices']],
                         ['550 Wp', '620 Wp', '630 Wp', '640 Wp'])
        self.assertEqual(data['default'], self.m(640).pk)
        self.assertEqual(data['rows'], [{'id': self.m(640).pk, 'qty': '20'}])
        self.assertEqual([o['label'] for o in data['others']], [WATTAGE_NOT_RECORDED])
        html = response.content.decode()
        self.assertIn('id="boqModulesEdit" data-ovr', html)
        self.assertIn("elModModal.addEventListener('show.bs.modal'", html)
        self.assertIn('if (dirty) { event.preventDefault(); return; }', html)
        self.assertIn('120 in all', html)
        # Module items are left out of the search and the per-item sheet; the OPX-001 line
        # is not also listed as an off-catalogue row.
        catalogue = json.loads(response.context['catalogue_json'])
        self.assertTrue(all(i.get('mod') for i in catalogue if i['code'] in WP.values()))
        self.assertFalse(any(i.get('mod') for i in catalogue if i['code'] not in WP.values()))
        self.assertEqual(json.loads(response.context['added_json']), [])
        self.assertEqual(response.context['added_off'], [])
        self.assertEqual(response.context['added_count'], 2)

    def test_a_reviewer_sees_the_block_but_no_pop_up(self):
        self._line('OPX-001', 100)
        _client_login(self, self.qc)
        response = self.client.get(self.picker_url)
        self.assertIsNone(response.context['module_data'])
        self.assertNotIn('boqModulesEdit', response.content.decode())
        self.assertContains(response, WATTAGE_NOT_RECORDED)

    def test_plan_module_save_is_pure(self):
        """No query: the pop-up's plan works from the lines and the POST alone."""
        legacy = self._line('OPX-001', 100)
        lines = list(BOQItem.objects.filter(pk=legacy.pk).select_related('item_master'))
        module_by_id = {m.pk: m for m in (self.m(550), self.m(640))}
        from django.http import QueryDict
        post = QueryDict(mutable=True)
        post.setlist('module_item', [str(self.m(550).pk)])
        post.setlist('module_qty', ['7'])
        post.setlist('module_line', [str(legacy.pk)])
        post[f'module_line_qty_{legacy.pk}'] = '100'
        post[f'module_line_to_{legacy.pk}'] = str(self.m(640).pk)
        with self.assertNumQueries(0):
            plan = plan_module_save(post, lines, module_by_id)
        self.assertEqual(([m.code for m, _q in plan.create], [l.pk for l in plan.delete]),
                         ([WP[550], WP[640]], [legacy.pk]))


# ═════════════════════════════════════════════════════════════════════════════
# T4 — minimum content and no put-backs
# ═════════════════════════════════════════════════════════════════════════════

class MinimumContentTests(DW5Base):

    def setUp(self):
        super().setUp()
        _client_login(self, self.designer)
        self._line('FX-INV1', 1)
        self._line('FX-DCDB', 1)
        self.complete_url = reverse('design_boq_complete',
                                    kwargs={'project_id': self.site.project_id})

    def complete(self):
        return self.client.post(self.complete_url, {'boq_remarks': ''}, follow=True)

    def stamped(self):
        attempt = self.assignment.attempts.first()
        return attempt is not None and attempt.boq_submitted_at is not None

    def test_refused_with_no_module_line(self):
        response = self.complete()
        self.assertFalse(self.stamped())
        self.assertIn("FX-A: can't mark this BOQ complete: it has no module line with a "
                      "quantity. Add it and save first.", self._errors(response))

    def test_a_blank_or_0_module_line_does_not_count(self):
        self._line(WP[640], None)
        self._line(WP[550], 0)
        self.complete()
        self.assertFalse(self.stamped())

    def test_passes_with_any_wattage_line(self):
        self._line(WP[620], 12)
        self.complete()
        self.assertTrue(self.stamped())

    def test_passes_with_an_opx001_line_with_a_quantity(self):
        self._line('OPX-001', 12)
        self.complete()
        self.assertTrue(self.stamped())

    def test_the_picker_route_names_the_module_and_passes_once_one_is_added(self):
        response = self._modules(sheet={'FX-INV1': 1, 'FX-DCDB': 1}, action='mark_complete')
        self.assertIn("Your sheet was saved, but FX-A can't be marked complete: it has no "
                      "module line with a quantity. Add it and save first.",
                      self._errors(response))
        self._modules(rows=[(640, 10)], sheet={'FX-INV1': 1, 'FX-DCDB': 1},
                      action='mark_complete')
        self.assertTrue(self.stamped())

    def test_three_gaps_read_as_one_sentence(self):
        BOQItem.objects.filter(boq__project=self.site).delete()
        self._line('FX-LA1', 1)
        response = self.complete()
        self.assertIn("FX-A: can't mark this BOQ complete: it has no module line with a "
                      "quantity, no inverter line with a quantity, and no DCDB, ACDB or ACCB "
                      "line with a quantity. Add them and save first. An ACCB saves from its "
                      "own card.", self._errors(response))

    def test_a_missing_group_skips_the_module_test_and_logs(self):
        # The group cannot be deleted while R12 reads it (PROTECT); an empty group is
        # treated exactly as a missing one (DW-1 Q7).
        BOQItemGroupMember.objects.filter(group=self.modules).delete()
        with self.assertLogs('projects.models', level='WARNING') as logs:
            rules = boq_minimum_content_rules()
        self.assertNotIn('no_module', [key for key, _m, _a in rules])
        self.assertIn('all modules', ' '.join(logs.output))
        self.complete()
        self.assertTrue(self.stamped())

    def test_nothing_is_put_back_blank_by_the_picker(self):
        # Even a module item still flagged mandatory (the state before the command).
        self.items['OPX-001'].is_active = True
        self.items['OPX-001'].is_mandatory = True
        self.items['OPX-001'].save()
        response = self._modules(sheet={'FX-INV1': 1})
        self.assertEqual(self._module_lines(), {})
        self.assertFalse(any('Put back' in str(m) for m in response.context['messages']))
        page = self.client.get(self.picker_url)
        self.assertEqual(json.loads(page.context['added_json']),
                         [{'id': self.items['FX-INV1'].pk, 'qty': '1'}])


class GroupScreenTests(DW5Base):
    """The design group screen and its Excel warn for a counted site with no module line,
    and show an OPX-001 line in the totals as a retired item."""

    def setUp(self):
        super().setUp()
        self.with_modules = self._review_site('FX-G1')
        for code, quantity in (('OPX-001', 100), (WP[640], 20), ('FX-INV1', 1),
                               ('FX-DCDB', 1)):
            self._line(code, quantity, site=self.with_modules)
        self.without = self._review_site('FX-G2')
        self._line('FX-INV1', 1, site=self.without)
        self._line('FX-DCDB', 1, site=self.without)
        self.group = DesignBOQGroup.objects.create(name='DW5 G', created_by=self.head)
        for site in (self.with_modules, self.without):
            DesignBOQGroupMembership.objects.create(group=self.group, project=site,
                                                    added_by=self.head)
        _client_login(self, self.head)

    def test_the_screen_warns_and_marks_opx001_retired(self):
        response = self.client.get(reverse('design_boq_group_detail', args=[self.group.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'no module line with a quantity')
        boq = response.context['boq']
        self.assertEqual([g['site'] for g in boq['content_gaps']], ['FX-G2'])
        lines = {line['item_master__code']: line for line in boq['lines']}
        self.assertTrue(lines['OPX-001']['retired'])
        self.assertEqual(lines['OPX-001']['total_quantity'], D(100))
        self.assertEqual(lines[WP[640]]['total_quantity'], D(20))
        self.assertFalse(lines[WP[640]]['retired'])
        self.assertContains(response, 'Retired')

    def test_the_excel_warns(self):
        response = self.client.get(reverse('design_boq_group_excel', args=[self.group.pk]))
        wb = load_workbook(BytesIO(response.content))
        text = ' '.join(str(cell) for ws in wb.worksheets
                        for row in ws.iter_rows(values_only=True) for cell in row if cell)
        self.assertIn('FX-G2: no module line with a quantity', text)


# ═════════════════════════════════════════════════════════════════════════════
# DW-7 download / upload, B-4 copy, reviewer correction
# ═════════════════════════════════════════════════════════════════════════════

class SheetTests(DW5Base):

    def setUp(self):
        super().setUp()
        _client_login(self, self.designer)
        self.legacy = self._line('OPX-001', 100)
        self._line(WP[640], 20)
        self._line('FX-INV1', 1)
        # Through the picker's own save, so the earthing set is calculated (240) and the
        # download carries the figure the engine would give.
        self._modules(rows=[(640, 20)], others=[(self.legacy, 100, None)],
                      sheet={'FX-INV1': 1})
        self.assertEqual(self._qty('OPX-177'), D(240))

    def _download(self):
        response = self.client.get(reverse('opex_boq_download', args=[self.site.project_id]))
        self.assertEqual(response.status_code, 200)
        return response.content

    def test_the_download_marks_module_rows_and_lists_opx001_on_other_lines(self):
        wb = load_workbook(BytesIO(self._download()))
        rows = {row[0]: row for row in wb[BOQ_DOWNLOAD_SHEET].iter_rows(min_row=2, values_only=True)}
        self.assertNotIn('OPX-001', rows)
        for code in WP.values():
            self.assertEqual(rows[code][REQUIRED_COL], 'At least one module')
        self.assertEqual(rows[WP[640]][4], 20)
        other = [row for row in wb[BOQ_DOWNLOAD_OFF_SHEET].iter_rows(min_row=2, values_only=True)
                 if row and row[0] == 'OPX-001']
        self.assertEqual(len(other), 1)
        self.assertEqual((other[0][4], other[0][5]), (100, 'Retired catalogue item'))

    def test_the_round_trip_changes_nothing(self):
        content = self._download()
        before = sorted(BOQItem.objects.filter(boq__project=self.site)
                        .values_list('pk', 'item_master_id', 'boq_quantity'))
        url = reverse('opex_boq_upload', args=[self.site.project_id])
        preview = self.client.post(url, {'phase': 'preview',
                                         'file': SimpleUploadedFile('boq.xlsx', content)})
        self.assertContains(preview, 'This file changes nothing.')
        after = sorted(BOQItem.objects.filter(boq__project=self.site)
                       .values_list('pk', 'item_master_id', 'boq_quantity'))
        self.assertEqual(after, before)

    def test_an_older_download_with_an_opx001_row_is_refused_with_the_p7_words(self):
        response = self.client.post(reverse('opex_boq_upload', args=[self.site.project_id]),
                                    {'phase': 'preview',
                                     'file': self._file([('OPX-001', ''), (WP[640], 25)])})
        self.assertContains(response, 'Row 2: item code &quot;OPX-001&quot; (Solar PV Module) '
                                      'is retired: modules are now one item per wattage. '
                                      'Download the BOQ again and enter modules on the '
                                      'wattage rows.')
        self.assertEqual(self._qty(WP[640]), D(20))

    def test_the_upload_still_sets_a_wattage_line(self):
        """Q5 (Zuber): the DW-7 upload works on wattage lines."""
        response = self._upload([(WP[640], 25), (WP[550], 5)])
        self.assertEqual(response.context['stage'], 'result')
        self.assertEqual(self._module_lines(),
                         {'OPX-001': D(100), WP[640]: D(25), WP[550]: D(5)})
        self.assertEqual(self._qty('OPX-177'), D(260))   # 2 x (100 + 25 + 5)

    def test_the_upload_puts_nothing_back_blank(self):
        BOQItem.objects.filter(boq__project=self.site).delete()
        self._upload([('FX-INV1', 2)])
        self.assertEqual(self._module_lines(), {})

    def test_the_copy_skips_opx001_with_the_reason_and_puts_nothing_back(self):
        target, _assignment = self._opex_site('FX-T')
        url = reverse('opex_boq_copy', args=[target.project_id])
        page = self.client.post(url, {'phase': 'preview', 'source_code': self.site.project_id})
        self.assertContains(page, MODULE_RETIRED_COPY_REASON)
        response = self.client.post(url, {'phase': 'commit',
                                          'source_code': self.site.project_id}, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._module_lines(target), {WP[640]: D(20)})
        self.assertFalse(BOQItem.objects.filter(boq__project=target,
                                                boq_quantity__isnull=True).exists())


class ReviewerTests(DW5Base):

    def setUp(self):
        super().setUp()
        self.review = self._review_site()
        self.line640 = self._line(WP[640], 20, site=self.review)
        self.legacy = self._line('OPX-001', 100, site=self.review)
        _client_login(self, self.qc)

    def test_a_reviewer_corrects_a_wattage_line(self):
        """Q5 (Zuber): the reviewer's quantity correction works on wattage lines."""
        self._correct(self.review, 'set_quantity', item_id=str(self.line640.pk), quantity='24')
        self.assertEqual(self._qty(WP[640], self.review), D(24))

    def test_a_reviewer_corrects_an_opx001_line(self):
        self._correct(self.review, 'set_quantity', item_id=str(self.legacy.pk), quantity='90')
        self.assertEqual(self._qty('OPX-001', self.review), D(90))

    def test_a_reviewer_cannot_add_a_module_line(self):
        response = self._correct(self.review, 'add_line', master_id=str(self.m(550).pk),
                                 quantity='5')
        self.assertIn(MODULE_REVIEWER_ADD_MESSAGE, self._messages(response))
        self.assertEqual(self._qty(WP[550], self.review), 'absent')
        page = self.client.get(reverse('boq_correct', args=[self.review.project_id]))
        addable = [m.code for _c, items in page.context['addable_groups'] for m in items]
        self.assertFalse(set(addable) & set(WP.values()))

    def test_existing_opx001_lines_show_on_the_review_screens(self):
        for url in (reverse('boq_correct', args=[self.review.project_id]),
                    reverse('design_qc_review', args=[self.review.project_id]),
                    reverse('opex_boq_entry', args=[self.review.project_id])):
            self.assertContains(self.client.get(url), 'Solar PV Module', msg_prefix=url)


# ═════════════════════════════════════════════════════════════════════════════
# T1 — design settings, and the catalogue form's module rules
# ═════════════════════════════════════════════════════════════════════════════

class DesignSettingsTests(DW5Base):

    def setUp(self):
        super().setUp()
        self.url = reverse('design_settings')

    def test_head_only(self):
        _client_login(self, self.head)
        self.assertEqual(self.client.get(self.url).status_code, 200)
        for profile in (self.qc, self.designer, self.scm, self.pm):
            _client_login(self, profile)
            self.assertEqual(self.client.get(self.url).status_code, 403, profile)
            self.assertEqual(self.client.post(self.url, {'default_module_wp': '630'}).status_code,
                             403, profile)
        self.assertEqual(DesignSettings.objects.get(pk=1).default_module_wp, 640)

    def test_a_change_is_logged_before_and_after_and_the_preselection_follows(self):
        _client_login(self, self.head)
        self.client.post(self.url, {'default_module_wp': '630'})
        row = DesignSettings.objects.get(pk=1)
        self.assertEqual((row.default_module_wp, row.updated_by), (630, self.head))
        log = ActivityLog.objects.get(action_code='design_settings_changed')
        self.assertEqual(log.action, 'Design settings: default module wattage: 640 Wp → 630 Wp')
        _client_login(self, self.designer)
        self.assertEqual(self.client.get(self.picker_url).context['module_data']['default'],
                         self.m(630).pk)

    def test_a_wattage_with_no_item_is_refused(self):
        _client_login(self, self.head)
        response = self.client.post(self.url, {'default_module_wp': '700'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Choose a wattage an active module item has.')
        self.assertEqual(DesignSettings.objects.get(pk=1).default_module_wp, 640)

    def test_the_defaults_item_cannot_be_retired(self):
        _client_login(self, self.head)
        response = self.client.post(reverse('design_boq_catalogue_toggle',
                                            args=[self.m(640).pk]), follow=True)
        self.assertContains(response, '640 Wp is the default module wattage in Design '
                                      'settings. Change Design settings first.')
        self.assertTrue(BOQItemMaster.objects.get(pk=self.m(640).pk).is_active)
        self.client.post(reverse('design_boq_catalogue_toggle', args=[self.m(550).pk]))
        self.assertFalse(BOQItemMaster.objects.get(pk=self.m(550).pk).is_active)

    def test_with_no_row_the_default_is_640_and_nothing_is_written(self):
        DesignSettings.objects.all().delete()
        _client_login(self, self.designer)
        self.assertEqual(self.client.get(self.picker_url).context['module_data']['default'],
                         self.m(640).pk)
        self.assertFalse(DesignSettings.objects.exists())

    def test_the_catalogue_links_to_it(self):
        _client_login(self, self.head)
        self.assertContains(self.client.get(reverse('design_boq_catalogue')),
                            reverse('design_settings'))


class CatalogueFormTests(DW5Base):

    def setUp(self):
        super().setUp()
        _client_login(self, self.head)

    def post_create(self, **fields):
        data = {'code': 'OPX-290', 'description': 'Solar PV Module 585 Wp', 'unit': 'Nos',
                'category': 'Module', 'is_active': 'on', 'sort_order': '19',
                'is_module': 'on', 'module_wp': '585'}
        data.update(fields)
        return self.client.post(reverse('design_boq_catalogue_create'),
                                {k: v for k, v in data.items() if v is not None})

    def test_a_module_needs_the_module_category_and_a_wattage(self):
        response = self.post_create(category='BOS', module_wp=None)
        self.assertContains(response, 'A module item belongs to the Module category.')
        self.assertContains(response, 'Enter the module&#x27;s wattage in Wp.')
        self.assertFalse(BOQItemMaster.objects.filter(code='OPX-290').exists())

    def test_one_item_per_wattage(self):
        response = self.post_create(module_wp='640')
        self.assertContains(response, '640 Wp is already OPX-278 (Solar PV Module 640 Wp). '
                                      'Each wattage is one item.')

    def test_fixed_once_a_line_uses_the_item(self):
        self._line(WP[630], 5)
        item = self.m(630)
        response = self.client.post(reverse('design_boq_catalogue_edit', args=[item.pk]), {
            'description': item.description, 'unit': 'Nos', 'category': 'Module',
            'is_active': 'on', 'sort_order': str(item.sort_order), 'is_module': 'on',
            'module_wp': '635'})
        self.assertContains(response, 'This item is on 1 site BOQ line, so whether it is a '
                                      'module and its wattage are fixed')
        self.assertEqual(BOQItemMaster.objects.get(pk=item.pk).module_wp, 630)

    def test_an_edit_is_logged_with_the_module_fields(self):
        self.post_create()
        item = BOQItemMaster.objects.get(code='OPX-290')
        self.client.post(reverse('design_boq_catalogue_edit', args=[item.pk]), {
            'description': item.description, 'unit': 'Nos', 'category': 'Module',
            'is_active': 'on', 'sort_order': '19', 'is_module': 'on', 'module_wp': '590'})
        log = ActivityLog.objects.filter(action_code='design_boq_item_updated').latest('pk')
        self.assertIn('wattage: 585 Wp → 590 Wp', log.action)

    def test_a_non_module_create_logs_as_before(self):
        self.client.post(reverse('design_boq_catalogue_create'), {
            'code': 'OPX-291', 'description': 'Cable gland', 'unit': 'Nos', 'category': 'BOS',
            'is_active': 'on', 'sort_order': '2910'})
        log = ActivityLog.objects.get(action_code='design_boq_item_created')
        self.assertNotIn('module', log.action)
        self.assertNotIn('wattage', log.action)
        self.assertFalse(BOQItemGroupMember.objects.filter(item__code='OPX-291').exists())

    def test_the_admin_form_does_not_offer_the_module_fields(self):
        from .forms import BOQItemMasterForm, HeadCatalogueItemForm
        self.assertNotIn('is_module', BOQItemMasterForm().fields)
        self.assertIn('is_module', HeadCatalogueItemForm().fields)


# ═════════════════════════════════════════════════════════════════════════════
# P11 — query pins, flat in the number of module lines
# ═════════════════════════════════════════════════════════════════════════════

#: Measured with QueryTests' fixture under solarpms.test_settings. The picker GET carries
#: DW-5's one query (the default wattage); every other screen costs what it did on 729b981
#: (the group's members are read in the query DW-1 already made). Flat in module lines
#: (test_no_count_grows_with_the_number_of_module_lines).
DW5_PINS = {
    'picker GET (designer)':             24,
    'picker save (designer)':            22,
    'download (designer)':               18,
    'upload preview (designer)':         19,
    'mark complete, refused (designer)': 11,
    'design group screen (Head)':        16,
    'design group Excel (Head)':         11,
    'Head catalogue':                    7,
    'design settings (Head)':            6,
}


class QueryTests(DW5Base):

    def _count(self, call):
        call()
        with CaptureQueriesContext(connection) as ctx:
            call()
        return len(ctx.captured_queries)

    def _measure(self):
        _client_login(self, self.designer)
        pid = self.site.project_id
        module_post = {'action': 'save_draft', 'modules': '1', 'module_item': [], 'module_qty': []}
        for line in BOQItem.objects.filter(boq__project=self.site, item_master__is_module=True,
                                           item_master__is_active=True):
            module_post['module_item'].append(str(line.item_master_id))
            module_post['module_qty'].append(str(line.boq_quantity))
        content = self.client.get(reverse('opex_boq_download', args=[pid])).content
        upload_url = reverse('opex_boq_upload', args=[pid])
        measured = {
            'picker GET (designer)': self._count(lambda: self.client.get(self.picker_url)),
            'picker save (designer)': self._count(
                lambda: self.client.post(self.picker_url, module_post)),
            'download (designer)': self._count(
                lambda: self.client.get(reverse('opex_boq_download', args=[pid]))),
            'upload preview (designer)': self._count(lambda: self.client.post(
                upload_url, {'phase': 'preview',
                             'file': SimpleUploadedFile('boq.xlsx', content)})),
            'mark complete, refused (designer)': self._count(lambda: self.client.post(
                reverse('design_boq_complete', args=[pid]), {'boq_remarks': ''})),
        }
        _client_login(self, self.head)
        measured['design group screen (Head)'] = self._count(
            lambda: self.client.get(reverse('design_boq_group_detail', args=[self.group.pk])))
        measured['design group Excel (Head)'] = self._count(
            lambda: self.client.get(reverse('design_boq_group_excel', args=[self.group.pk])))
        measured['Head catalogue'] = self._count(
            lambda: self.client.get(reverse('design_boq_catalogue')))
        measured['design settings (Head)'] = self._count(
            lambda: self.client.get(reverse('design_settings')))
        print('\nDW-5 measured query counts:', measured)
        return measured

    def setUp(self):
        super().setUp()
        self._line('OPX-001', 100)
        self._line('FX-INV1', 1)       # no DCDB: Mark complete is refused, so it can repeat
        self.counted = self._review_site('FX-Q')
        self._line('OPX-001', 50, site=self.counted)
        self.group = DesignBOQGroup.objects.create(name='DW5 Q', created_by=self.head)
        DesignBOQGroupMembership.objects.create(group=self.group, project=self.counted,
                                                added_by=self.head)

    def test_query_counts_are_pinned(self):
        self.assertEqual(self._measure(), DW5_PINS)

    def test_no_count_grows_with_the_number_of_module_lines(self):
        first = self._measure()
        for wp in (550, 620, 630, 640):
            self._line(WP[wp], 10)
            self._line(WP[wp], 10, site=self.counted)
        self.assertEqual(self._measure(), first)
