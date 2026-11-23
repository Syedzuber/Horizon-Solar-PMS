"""
B-1 — import_master_catalogue: the confirmed master BOQ applied to the RESCO catalogue.

What these tests pin (a small fixture file, never the real one):
- a dry run writes nothing; --apply creates with the file's codes and units, rewords only
  the description, reactivates, retires without touching a line, keeps and skips, and logs
  every change old → new with no actor and an action code;
- existing BOQ lines keep their own text after a reword, and a line added afterwards takes
  the new wording (Z1); a picker save and an upload never rewrite an existing line's text
  or unit (Zuber B-1 Q2: today's behaviour, pinned);
- every refusal is listed and nothing is written; --allow-codes needs --reason and lets
  only an allowable, named mismatch through; a second --apply changes nothing;
- OPX-273, ACCB configurations, formula rules, BOQ lines and Residential items are
  untouched; the in-use report lists a released, ordered line and a deleted site's line;
- the picker GET costs the same number of queries after the import (no per-item query).
"""
import csv
import os
import tempfile
import uuid
from decimal import Decimal
from io import BytesIO, StringIO

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import CommandError, call_command
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from .accb import ACCB_CONFIGURED_CODE
from .management.commands.import_master_catalogue import (
    ACTION_CODES, COLUMNS, REPORT_COLUMNS, SUMMARY_ACTION_CODE,
)
from .models import (
    ACCBConfig, ActivityLog, BOQ, BOQFormulaDriver, BOQFormulaRule, BOQItem,
    BOQItemGroup, BOQItemGroupMember, BOQItemMaster, DesignAssignment, Program, Project,
    Vendor, VendorOrder, VendorOrderLine, VendorOrderSite,
    DESIGN_IN_DESIGN, DESIGN_RELEASED,
)
from .views import BOQ_DOWNLOAD_COLUMNS, BOQ_DOWNLOAD_SHEET

D = Decimal

FUSE = '5 In/ 5 Out DCBDB, 16Amp Fuse protection'
MCB = '5 In/ 5 Out DCBDB, 16Amp MCB protection'
BLLAST = 'INC work Bllast struture'
BALLAST = 'INC work Ballast structure'

#: (code, description, unit, category, sort_order, is_active, is_mandatory) — the live
#: RESCO catalogue the fixture file is written against.
LIVE = [
    ('OPX-001', 'Solar PV Module',                     'Nos',   'Module',   1,   True,  True),
    ('OPX-002', FUSE,                                  'Nos',   'DCDB',     2,   True,  False),
    ('OPX-035', '3P Structure',                        'Nos',   'MMS',      35,  False, False),
    # After B-0b: live Nos, the 17 Nov export said KWp, the file says Nos (Zuber Q1).
    ('OPX-037', 'Ballast Type Structure',              'Nos',   'MMS',      37,  True,  False),
    # An old spelling: 'No' is Nos (units.UNIT_SPELLINGS), so it passes and stays 'No'.
    ('OPX-038', 'Tin Shade',                           'No',    'MMS',      38,  True,  False),
    ('OPX-075', '1.1kV*4C*300Sqmm*Cu*XLPE*cable',      'Meter', 'AC Cable', 75,  True,  False),
    ('OPX-086', '1.1kV*1C*300Sqmm*Cu*XLPE*cable',      'Meter', 'AC Cable', 86,  True,  False),
    ('OPX-147', 'SS Saddle for 32MM conduit (100PCS)', 'Pkt',   'Conduit',  147, True,  False),
    ('OPX-205', BLLAST,                                'KWp',   'BOS',      205, True,  False),
]


def _row(action, code, description='', unit='', expected_description='',
         expected_unit='', header='', reason=''):
    return {'action': action, 'code': code, 'master_sr': '', 'master_header': header,
            'description': description, 'unit': unit,
            'expected_description': expected_description, 'expected_unit': expected_unit,
            'auto_qty': '', 'reason': reason}


def fixture_rows():
    """One row per action, the shapes the real file has: a CHECK whose unit B-0b already
    moved (OPX-037), a CHECK on an old spelling (OPX-038), a duplicate SKIP pointing at a
    CHECK code (OPX-086), the ACCB Type-1 SKIP, the NA row."""
    return [
        _row('CHECK', 'OPX-001', 'Solar PV Module', 'Nos', 'Solar PV Module', 'Nos'),
        _row('REWORD', 'OPX-002', MCB, 'Nos', FUSE, 'Nos'),
        _row('REACTIVATE', 'OPX-035', '3P Structure', 'Nos', '3P Structure', 'Nos'),
        _row('CHECK', 'OPX-037', 'Ballast Type Structure', 'Nos', 'Ballast Type Structure',
             'KWp'),
        _row('CHECK', 'OPX-038', 'Tin Shade', 'Nos', 'Tin Shade', 'No'),
        _row('KEEP', 'OPX-075', '', '', '1.1kV*4C*300Sqmm*Cu*XLPE*cable', 'Meter'),
        _row('CHECK', 'OPX-086', '1.1kV*1C*300Sqmm*Cu*XLPE*cable', 'Meter',
             '1.1kV*1C*300Sqmm*Cu*XLPE*cable', 'Meter'),
        _row('SKIP', 'OPX-086', '1.1kV*1C*300Sqmm*Cu*XLPE*cable', reason='duplicate'),
        _row('RETIRE', 'OPX-147', '', '', 'SS Saddle for 32MM conduit (100PCS)', 'Pkt'),
        _row('REWORD', 'OPX-205', BALLAST, 'KWp', BLLAST, 'KWp'),
        _row('CREATE', 'OPX-244', '1.1kV*4C*16Sqmm*AL*XLPE*cable', 'Meter',
             header='Al Cable 4C'),
        _row('CREATE', 'OPX-259', '50/63 DWC HDPE Conduit Pipe', 'Meter', header='Conduit'),
        _row('CREATE', 'OPX-267', 'SS Saddle for 63MM conduit (100PCS)', 'Pkt',
             header='Conduit'),
        _row('CREATE', 'OPX-271', 'Inverter Stand', 'Nos', header='Inverter Stand'),
        _row('SKIP', 'OPX-243', 'ACCB Type-1 (439) I/C Feeder 2 Nos.', 'Nos',
             reason='every ACCB from the configurator'),
        _row('SKIP', '', 'Module cleaning system is not considered currently',
             reason='skip'),
    ]


def _profile(username, role, **flags):
    """A post_save signal auto-creates the UserProfile; fetch and set, never create."""
    user = User.objects.create_user(username=username, password='x',
                                    first_name=username.capitalize())
    profile = user.profile
    profile.role = role
    profile.is_active = True
    for name, value in flags.items():
        setattr(profile, name, value)
    profile.save()
    return profile


class ImportBase(TestCase):
    """The LIVE catalogue, OPX-273 and one Residential item; four sites on one tender:
      A (MC-A): in design — OPX-002 'Fuse', OPX-086, OPX-205 'Bllast', and a configured ACCB;
      B (MC-B): in design, empty;
      R (MC-R): released — OPX-002 ordered (ordered_quantity and an order), OPX-147;
      X (MC-X): deleted — OPX-002.
    """

    def setUp(self):
        self.items = {}
        for code, description, unit, category, sort_order, active, mandatory in LIVE:
            self.items[code] = BOQItemMaster.objects.create(
                code=code, description=description, unit=unit, category=category,
                project_type='OPEX', sort_order=sort_order, is_active=active,
                is_mandatory=mandatory)
        self.accb_master, _ = BOQItemMaster.objects.get_or_create(
            code=ACCB_CONFIGURED_CODE,
            defaults={'description': 'ACCB (configured)', 'unit': 'Nos', 'category': 'ACCB',
                      'project_type': 'OPEX', 'sort_order': 273})
        self.residential = BOQItemMaster.objects.create(
            code='ITM-001', description='Solar Panel', unit='Nos', category='Solar Modules',
            project_type='Residential', sort_order=1)

        self.designer = _profile('mcdes', 'Design')
        self.pm = _profile('mcpm', 'PM')
        self.scm = _profile('mcscm', 'SCM')
        self.program = Program.objects.create(
            name='Test-MC', program_type='OPEX', client_name='MCClient', status='Active',
            short_tender_code='MC')

        self.site_a = self._site('MC-A')
        self.site_b = self._site('MC-B')
        self.site_r = self._site('MC-R', status=DESIGN_RELEASED)
        self.site_x = self._site('MC-X', is_deleted=True)

        self.line_a_fuse = self._line(self.site_a, 'OPX-002', 2)
        self.line_a_cable = self._line(self.site_a, 'OPX-086', 10)
        self.line_a_inc = self._line(self.site_a, 'OPX-205', 3)
        self.line_r_fuse = self._line(self.site_r, 'OPX-002', 4)
        self.line_r_fuse.ordered_quantity = D('4')
        self.line_r_fuse.save(update_fields=['ordered_quantity'])
        self.line_r_saddle = self._line(self.site_r, 'OPX-147', 1)
        self.line_x_fuse = self._line(self.site_x, 'OPX-002', 1)

        accb_line = BOQItem.objects.create(
            boq=self.site_a.boq, item_master=self.accb_master, serial_no=273,
            category='ACCB', description='ACCB 1: Type-1, IN 2 x MCB 40A, OUT 1 x MCCB 63A',
            uom='Nos', boq_quantity=D('1'), is_standard_item=True)
        ACCBConfig.objects.create(
            boq=self.site_a.boq, boq_item=accb_line, number=1, accb_type='Type-1',
            has_spd=False, config_key='k1',
            feeders={'in': [{'breaker': 'MCB', 'rating': 40}] * 2,
                     'out': [{'breaker': 'MCCB', 'rating': 63}]})

        vendor = Vendor.objects.create(name='MC Elec', contact_person='R', phone='9000000001')
        self.order = VendorOrder.objects.create(
            vendor=vendor, project_type='OPEX', total_amount=D('5000'), created_by=self.scm,
            client_uuid=uuid.uuid4())
        VendorOrderSite.objects.create(order=self.order, project=self.site_r)
        VendorOrderLine.objects.create(
            order=self.order, item_master=self.items['OPX-002'], item_code='OPX-002',
            item_description=FUSE, item_unit='Nos', quantity=D('4'))

        self.tmp = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)
        self.rows = fixture_rows()

    def _cleanup(self):
        for name in os.listdir(self.tmp):
            os.remove(os.path.join(self.tmp, name))
        os.rmdir(self.tmp)

    # ── fixtures ────────────────────────────────────────────────────────────

    def _site(self, code, status=DESIGN_IN_DESIGN, is_deleted=False):
        site = Project(
            project_id=code, customer_name='MCClient', customer_phone='9876543210',
            site_address='1 Sun Rd', city='Delhi', project_type='OPEX',
            program=self.program, site_code=code, dc_capacity_kw=D('100.00'),
            status='Draft', assigned_design=self.designer, assigned_pm=self.pm,
            is_deleted=is_deleted)
        site.save()
        DesignAssignment.objects.create(
            project=site, status=status, assigned_to=self.designer,
            survey_file_bucket='b', survey_file_path=f'{code}/survey/x.pdf')
        BOQ.objects.create(project=site)
        return site

    def _line(self, site, code, quantity):
        master = self.items[code]
        return BOQItem.objects.create(
            boq=site.boq, item_master=master, serial_no=master.sort_order,
            category=master.category, description=master.description, uom=master.unit,
            boq_quantity=D(str(quantity)), is_standard_item=True)

    def _master(self, code):
        return BOQItemMaster.objects.get(code=code)

    # ── running the command ─────────────────────────────────────────────────

    def _write(self, rows=None):
        path = os.path.join(self.tmp, 'master.csv')
        with open(path, 'w', encoding='utf-8', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=COLUMNS)
            writer.writeheader()
            writer.writerows(self.rows if rows is None else rows)
        return path

    def run_cmd(self, *args, rows=None):
        rows = self.rows if rows is None else rows
        out = StringIO()
        call_command('import_master_catalogue', '--file', self._write(rows),
                     '--expected-rows', str(len(rows)), *args, stdout=out)
        return out.getvalue()

    def refused(self, *args, rows=None):
        """Run --apply expecting the refusal; returns the printed output."""
        out = StringIO()
        rows = self.rows if rows is None else rows
        with self.assertRaises(CommandError) as caught:
            call_command('import_master_catalogue', '--file', self._write(rows),
                         '--expected-rows', str(len(rows)), '--apply', *args, stdout=out)
        return out.getvalue() + str(caught.exception)

    def state(self):
        """Everything the import could touch, with updated_at, so 'nothing written' is a
        comparison of the whole picture."""
        return {
            'masters': list(BOQItemMaster.objects.order_by('code').values()),
            'lines':   list(BOQItem.objects.order_by('pk').values()),
            'accb':    list(ACCBConfig.objects.order_by('pk').values()),
            'rules':   list(BOQFormulaRule.objects.order_by('pk').values()),
            'drivers': list(BOQFormulaDriver.objects.order_by('pk').values()),
            'groups':  list(BOQItemGroupMember.objects.order_by('pk').values()),
            'orders':  list(VendorOrderLine.objects.order_by('pk').values()),
            'logs':    ActivityLog.objects.count(),
        }

    # ── screens ─────────────────────────────────────────────────────────────

    def _login(self):
        self.assertTrue(self.client.login(username='mcdes', password='x'))

    def _picker(self, site, quantities):
        """Save the picker with exactly these {code: qty} rows (absence = removal)."""
        data = {'action': 'save_draft', 'item': []}
        for code, quantity in quantities.items():
            pk = self._master(code).pk
            data['item'].append(str(pk))
            data[f'qty_{pk}'] = str(quantity)
        return self.client.post(reverse('opex_boq_entry', kwargs={
            'project_id': site.project_id}), data, follow=True)

    def _upload(self, site, rows):
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.title = BOQ_DOWNLOAD_SHEET
        ws.append([header for header, _key in BOQ_DOWNLOAD_COLUMNS])
        for code, quantity in rows:
            ws.append([code, '', '', '', quantity])
        out = BytesIO()
        wb.save(out)
        url = reverse('opex_boq_upload', kwargs={'project_id': site.project_id})
        preview = self.client.post(url, {
            'phase': 'preview', 'file': SimpleUploadedFile('boq.xlsx', out.getvalue())})
        self.assertEqual(preview.context['stage'], 'preview')
        return self.client.post(url, {'phase': 'commit',
                                      'rows_json': preview.context['rows_json']})

    def _text(self, line):
        line.refresh_from_db()
        return (line.description, line.uom, line.category, line.serial_no)


# ===========================================================================
# Dry run and apply
# ===========================================================================

class DryRunTests(ImportBase):

    def test_dry_run_writes_nothing(self):
        before = self.state()
        out = self.run_cmd()
        self.assertEqual(self.state(), before)
        self.assertIn('DRY RUN', out)
        self.assertIn('CREATE        4 rows   to do   4', out)
        self.assertIn('REWORD        2 rows   to do   2', out)
        self.assertIn('(would be made)', out)
        self.assertIn('MISMATCHES\n  none', out)

    def test_dry_run_writes_the_report_csv_before_anything_changes(self):
        """Zuber Q2: the old wording is saved before apply."""
        path = os.path.join(self.tmp, 'inuse.csv')
        before = self.state()
        self.run_cmd('--report-csv', path)
        self.assertEqual(self.state(), before)
        with open(path, encoding='utf-8-sig', newline='') as handle:
            rows = list(csv.DictReader(handle))
        fuse = [r for r in rows if r['item_code'] == 'OPX-002']
        self.assertEqual({r['catalogue_wording_before'] for r in fuse}, {FUSE})
        self.assertEqual({r['catalogue_wording_after'] for r in fuse}, {MCB})


class ApplyTests(ImportBase):

    def setUp(self):
        super().setUp()
        self.before = self.state()
        self.out = self.run_cmd('--apply')

    def test_creates_with_the_files_codes_units_and_agreed_categories(self):
        expected = {
            'OPX-244': ('1.1kV*4C*16Sqmm*AL*XLPE*cable', 'Meter', 'AC Cable', 244),
            'OPX-259': ('50/63 DWC HDPE Conduit Pipe', 'Meter', 'Conduit', 259),
            'OPX-267': ('SS Saddle for 63MM conduit (100PCS)', 'Pkt', 'Conduit', 267),
            'OPX-271': ('Inverter Stand', 'Nos', 'BOS', 271),
        }
        for code, (description, unit, category, sort_order) in expected.items():
            item = self._master(code)
            self.assertEqual((item.description, item.unit, item.category, item.sort_order,
                              item.project_type, item.is_active, item.is_mandatory),
                             (description, unit, category, sort_order, 'OPEX', True, False))

    def test_skip_rows_create_nothing(self):
        self.assertFalse(BOQItemMaster.objects.filter(code='OPX-243').exists())
        self.assertEqual(BOQItemMaster.objects.count(), len(self.before['masters']) + 4)

    def test_reword_changes_only_the_description(self):
        old = {m['code']: m for m in self.before['masters']}
        for code, new in (('OPX-002', MCB), ('OPX-205', BALLAST)):
            item = self._master(code)
            self.assertEqual(item.description, new)
            for name in ('unit', 'category', 'sort_order', 'is_active', 'is_mandatory',
                         'project_type'):
                self.assertEqual(getattr(item, name), old[code][name], f'{code} {name}')

    def test_reactivates_and_retires(self):
        self.assertTrue(self._master('OPX-035').is_active)
        self.assertFalse(self._master('OPX-147').is_active)

    def test_retire_leaves_its_lines_alone(self):
        before = {line['id']: line for line in self.before['lines']}
        self.assertEqual(BOQItem.objects.filter(pk=self.line_r_saddle.pk).values()[0],
                         before[self.line_r_saddle.pk])

    def test_check_and_keep_items_are_untouched_including_old_spellings(self):
        old = {m['code']: m for m in self.before['masters']}
        for code in ('OPX-001', 'OPX-037', 'OPX-038', 'OPX-075', 'OPX-086'):
            self.assertEqual(BOQItemMaster.objects.filter(code=code).values()[0], old[code])
        self.assertEqual(self._master('OPX-038').unit, 'No')

    def test_each_change_is_logged_old_to_new_by_the_system(self):
        logs = ActivityLog.objects.exclude(action_code='').filter(
            action_code__in=list(ACTION_CODES.values()) + [SUMMARY_ACTION_CODE])
        counts = {}
        for log in logs:
            counts[log.action_code] = counts.get(log.action_code, 0) + 1
            self.assertIsNone(log.actor_id)
            self.assertIsNone(log.project_id)
            self.assertTrue(log.action.startswith('import_master_catalogue (system)'))
        self.assertEqual(counts, {
            'boq_catalogue_item_imported': 4, 'boq_catalogue_item_reworded': 2,
            'boq_catalogue_item_reactivated': 1, 'boq_catalogue_item_retired': 1,
            SUMMARY_ACTION_CODE: 1})
        reword = logs.get(action_code='boq_catalogue_item_reworded',
                          entity_id=self._master('OPX-002').pk)
        self.assertIn(f"description: '{FUSE}' → '{MCB}'", reword.action)
        self.assertEqual(reword.entity_type, 'BOQItemMaster')
        retire = logs.get(action_code='boq_catalogue_item_retired')
        self.assertIn('active: yes → no', retire.action)
        created = logs.get(action_code='boq_catalogue_item_imported',
                           entity_id=self._master('OPX-267').pk)
        self.assertIn('unit: → Pkt', created.action)
        summary = logs.get(action_code=SUMMARY_ACTION_CODE)
        self.assertIn('4 created, 2 reworded, 1 reactivated, 1 retired', summary.action)

    def test_nothing_else_changes(self):
        """OPX-273 and its ACCB configuration, formula rules, BOQ lines, orders and the
        Residential item: identical, updated_at included."""
        after = self.state()
        for key in ('lines', 'accb', 'rules', 'drivers', 'groups', 'orders'):
            self.assertEqual(after[key], self.before[key], key)
        old = {m['code']: m for m in self.before['masters']}
        new = {m['code']: m for m in after['masters']}
        self.assertEqual(new[ACCB_CONFIGURED_CODE], old[ACCB_CONFIGURED_CODE])
        self.assertEqual(new['ITM-001'], old['ITM-001'])
        self.assertEqual(ACCBConfig.objects.count(), 1)

    def test_a_second_apply_changes_nothing_and_says_so(self):
        before = self.state()
        out = self.run_cmd('--apply')
        self.assertEqual(self.state(), before)
        self.assertIn('Nothing to do: every row is already applied. Nothing was written.', out)
        self.assertIn('CREATE        4 rows   to do   0   already done   4', out)

    def test_output_lists_each_change(self):
        self.assertIn('WRITTEN: 4 created, 2 reworded, 1 reactivated, 1 retired', self.out)
        self.assertIn(f"OPX-002  REWORD      '{FUSE}' -> '{MCB}'", self.out)
        self.assertIn('OPX-035  REACTIVATE  active: no -> yes', self.out)
        self.assertIn('OPX-147  RETIRE      active: yes -> no', self.out)
        self.assertIn("OPX-271  CREATE      -> 'Inverter Stand' | Nos | BOS | sort 271",
                      self.out)


# ===========================================================================
# BOQ lines keep their own text (Z1, Zuber Q2)
# ===========================================================================

class LineTextTests(ImportBase):

    def test_existing_lines_keep_their_text_after_a_reword(self):
        lines = (self.line_a_fuse, self.line_r_fuse, self.line_x_fuse, self.line_a_inc)
        before = [self._text(line) for line in lines]
        self.run_cmd('--apply')
        self.assertEqual([self._text(line) for line in lines], before)
        self.assertEqual(self._text(self.line_a_fuse)[0], FUSE)
        self.assertEqual(self._text(self.line_a_inc)[0], BLLAST)

    def test_a_line_added_after_the_reword_takes_the_new_wording(self):
        self.run_cmd('--apply')
        self._login()
        self._picker(self.site_b, {'OPX-002': 3, 'OPX-259': 40})
        added = {l.item_master.code: l for l in self.site_b.boq.items.all()}
        self.assertEqual((added['OPX-002'].description, added['OPX-002'].uom), (MCB, 'Nos'))
        self.assertEqual((added['OPX-259'].description, added['OPX-259'].uom),
                         ('50/63 DWC HDPE Conduit Pipe', 'Meter'))

    def test_a_picker_save_never_rewrites_an_existing_lines_text(self):
        """Pinned today's behaviour: quantity changed (OPX-002), quantity unchanged
        (OPX-086, OPX-205) — every existing line keeps its own text and unit."""
        self.run_cmd('--apply')
        lines = (self.line_a_fuse, self.line_a_cable, self.line_a_inc)
        before = [self._text(line) for line in lines]
        self._login()
        self._picker(self.site_a, {'OPX-002': 5, 'OPX-086': '10', 'OPX-205': '3'})
        self.line_a_fuse.refresh_from_db()
        self.assertEqual(self.line_a_fuse.boq_quantity, D('5'))
        self.assertEqual([self._text(line) for line in lines], before)
        self.assertEqual(self._text(self.line_a_fuse)[0], FUSE)

    def test_an_upload_never_rewrites_an_existing_lines_text(self):
        """Pinned today's behaviour: one line's quantity changed (OPX-002), one in the file
        unchanged (OPX-086), one not in the file at all (OPX-205)."""
        self.run_cmd('--apply')
        lines = (self.line_a_fuse, self.line_a_cable, self.line_a_inc)
        before = [self._text(line) for line in lines]
        self._login()
        self._upload(self.site_a, [('OPX-002', 7), ('OPX-086', 10)])
        self.line_a_fuse.refresh_from_db()
        self.assertEqual(self.line_a_fuse.boq_quantity, D('7'))
        self.assertEqual([self._text(line) for line in lines], before)

    def test_the_picker_get_costs_the_same_queries_after_the_import(self):
        """No per-item query: four more catalogue items, the same count."""
        self._login()
        url = reverse('opex_boq_entry', kwargs={'project_id': self.site_a.project_id})

        def count():
            self.client.get(url)            # warm the session and content-type cache
            with CaptureQueriesContext(connection) as ctx:
                response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            return len(ctx)

        before = count()
        self.run_cmd('--apply')
        self.assertEqual(count(), before)


# ===========================================================================
# Refusals: every one listed, nothing written
# ===========================================================================

class RefusalTests(ImportBase):

    def assertRefused(self, expected_text, *args, rows=None):
        before = self.state()
        out = self.refused(*args, rows=rows)
        self.assertEqual(self.state(), before)
        self.assertIn(expected_text, out)
        self.assertIn('Nothing was written', out)
        return out

    def _with(self, code, row_action, **changes):
        rows = fixture_rows()
        for row in rows:
            if row['code'] == code and row['action'] == row_action:
                row.update(changes)
        return rows

    def test_a_create_whose_code_exists_with_other_values(self):
        BOQItemMaster.objects.create(code='OPX-259', description='Some other pipe',
                                     unit='Meter', category='Conduit', project_type='OPEX',
                                     sort_order=259)
        self.assertRefused('CREATE: OPX-259 already exists with other values')

    def test_a_create_whose_code_is_a_residential_item(self):
        BOQItemMaster.objects.create(code='OPX-267', description='x', unit='Pkt',
                                     category='Conduit', project_type='Residential')
        self.assertRefused('CREATE: OPX-267 already exists with other values')

    def test_a_live_description_that_is_not_the_expected_one(self):
        BOQItemMaster.objects.filter(code='OPX-001').update(description='Solar Module 540W')
        self.assertRefused("CHECK: live wording 'Solar Module 540W' is not the expected "
                           "'Solar PV Module'")

    def test_a_reword_whose_live_text_drifted(self):
        BOQItemMaster.objects.filter(code='OPX-002').update(description='DCDB edited')
        self.assertRefused("REWORD: live wording 'DCDB edited' is not the expected")

    def test_a_unit_not_on_the_list(self):
        self.assertRefused("unit 'Mtr' is not on the unit list",
                           rows=self._with('OPX-259', 'CREATE', unit='Mtr'))

    def test_a_description_over_the_length(self):
        self.assertRefused('description is 256 characters',
                           rows=self._with('OPX-259', 'CREATE', description='x' * 256))

    def test_retiring_a_mandatory_item(self):
        BOQItemMaster.objects.filter(code='OPX-147').update(is_mandatory=True)
        self.assertRefused('RETIRE: the item is mandatory')

    def test_retiring_a_formula_driver(self):
        rule = BOQFormulaRule(target=self.items['OPX-075'], summary='5 m per saddle pack')
        rule.save()
        BOQFormulaDriver(rule=rule, item=self.items['OPX-147']).save()
        self.assertRefused('RETIRE: the item is used by a formula rule')

    def test_retiring_a_group_member(self):
        group = BOQItemGroup(name='all saddles', label='saddles')
        group.save()
        BOQItemGroupMember(group=group, item=self.items['OPX-147']).save()
        self.assertRefused('RETIRE: the item is used by a formula rule')

    def test_a_live_item_missing_from_the_file(self):
        BOQItemMaster.objects.create(code='OPX-300', description='Added on 24 Nov',
                                     unit='Nos', category='BOS', project_type='OPEX',
                                     sort_order=300)
        self.assertRefused('live RESCO item OPX-300')

    def test_a_row_that_would_change_a_used_items_unit(self):
        self.assertRefused("CHECK: live unit 'Meter', file unit 'Nos'; this import never "
                           "changes a unit",
                           rows=self._with('OPX-086', 'CHECK', unit='Nos'))

    def test_a_unit_the_file_moved_but_live_did_not(self):
        """Zuber Q1: OPX-037 still KWp (B-0b not run) differs from the file's Nos."""
        BOQItemMaster.objects.filter(code='OPX-037').update(unit='KWp')
        self.assertRefused("CHECK: live unit 'KWp', file unit 'Nos'")

    def test_a_create_with_no_agreed_category(self):
        self.assertRefused("no category agreed for the master header 'Earthing kit'",
                           rows=self._with('OPX-259', 'CREATE', master_header='Earthing kit'))

    def test_the_wrong_row_count(self):
        out = StringIO()
        with self.assertRaises(CommandError):
            call_command('import_master_catalogue', '--file', self._write(),
                         '--apply', stdout=out)
        self.assertIn('the file has 16 rows; expected 275', out.getvalue())

    def test_every_problem_is_listed_not_only_the_first(self):
        BOQItemMaster.objects.filter(code='OPX-001').update(description='Solar Module 540W')
        BOQItemMaster.objects.create(code='OPX-300', description='Added on 24 Nov',
                                     unit='Nos', category='BOS', project_type='OPEX',
                                     sort_order=300)
        out = self.assertRefused('live RESCO item OPX-300',
                                 rows=self._with('OPX-259', 'CREATE', unit='Mtr'))
        self.assertIn("live wording 'Solar Module 540W'", out)
        self.assertIn("unit 'Mtr' is not on the unit list", out)

    def test_an_accb_row_that_is_not_skipped(self):
        self.assertRefused('an ACCB row must be SKIP',
                           rows=self._with('OPX-243', 'SKIP', action='CREATE',
                                           master_header='ACDB'))


class AllowCodesTests(ImportBase):

    def setUp(self):
        super().setUp()
        BOQItemMaster.objects.filter(code='OPX-001').update(description='Solar Module 540W')

    def test_a_named_allowable_mismatch_goes_through_and_its_reason_is_logged(self):
        out = self.run_cmd('--apply', '--allow-codes', 'OPX-001',
                           '--reason', 'renamed on the screen; Praveen agreed')
        self.assertIn('[allowed]', out)
        self.assertEqual(self._master('OPX-002').description, MCB)
        # A CHECK row is never written, allowed or not.
        self.assertEqual(self._master('OPX-001').description, 'Solar Module 540W')
        self.assertTrue(ActivityLog.objects.filter(
            action_code=SUMMARY_ACTION_CODE,
            action__contains='allowed mismatches on OPX-001; reason: renamed on the screen; '
                             'Praveen agreed').exists())

    def test_allow_codes_without_a_reason_is_refused(self):
        before = self.state()
        with self.assertRaisesMessage(CommandError, '--allow-codes needs --reason'):
            self.run_cmd('--apply', '--allow-codes', 'OPX-001')
        self.assertEqual(self.state(), before)

    def test_a_mismatch_that_cannot_be_allowed_still_refuses(self):
        BOQItemMaster.objects.filter(code='OPX-147').update(is_mandatory=True)
        before = self.state()
        out = self.refused('--allow-codes', 'OPX-001', '--reason', 'x')
        self.assertEqual(self.state(), before)
        self.assertIn('[cannot be allowed] RETIRE: the item is mandatory', out)
        # Naming it does not help: it has no allowable mismatch to allow.
        with self.assertRaisesMessage(CommandError, 'no allowable mismatch: OPX-147'):
            self.run_cmd('--apply', '--allow-codes', 'OPX-001,OPX-147', '--reason', 'x')
        self.assertEqual(self.state(), before)

    def test_an_allowed_code_with_no_mismatch_is_refused(self):
        before = self.state()
        with self.assertRaisesMessage(CommandError, 'no allowable mismatch: OPX-002'):
            self.run_cmd('--apply', '--allow-codes', 'OPX-001,OPX-002', '--reason', 'x')
        self.assertEqual(self.state(), before)


# ===========================================================================
# T2 — the in-use report
# ===========================================================================

class InUseReportTests(ImportBase):

    def _report(self, *args):
        path = os.path.join(self.tmp, 'inuse.csv')
        out = self.run_cmd('--report-csv', path, *args)
        with open(path, encoding='utf-8-sig', newline='') as handle:
            reader = csv.DictReader(handle)
            self.assertEqual(tuple(reader.fieldnames), REPORT_COLUMNS)
            return out, list(reader)

    def test_a_released_ordered_line_is_listed_correctly(self):
        _out, rows = self._report()
        row = next(r for r in rows if r['line_id'] == str(self.line_r_fuse.pk))
        self.assertEqual(
            {k: row[k] for k in ('item_code', 'action', 'site_code', 'released',
                                 'design_status', 'ordered', 'ordered_quantity', 'order_ids',
                                 'site_deleted', 'line_description')},
            {'item_code': 'OPX-002', 'action': 'REWORD', 'site_code': 'MC-R',
             'released': 'yes', 'design_status': DESIGN_RELEASED, 'ordered': 'yes',
             'ordered_quantity': '4.00', 'order_ids': f'#{self.order.pk}',
             'site_deleted': 'no', 'line_description': FUSE})

    def test_every_line_on_reworded_and_retired_items_deleted_sites_included(self):
        _out, rows = self._report()
        self.assertEqual(
            sorted((r['item_code'], r['site_code'], r['site_deleted'], r['ordered'])
                   for r in rows),
            [('OPX-002', 'MC-A', 'no', 'no'), ('OPX-002', 'MC-R', 'no', 'yes'),
             ('OPX-002', 'MC-X', 'yes', 'no'), ('OPX-147', 'MC-R', 'no', 'no'),
             ('OPX-205', 'MC-A', 'no', 'no')])
        saddle = next(r for r in rows if r['item_code'] == 'OPX-147')
        self.assertEqual((saddle['action'], saddle['catalogue_wording_after']),
                         ('RETIRE', '(retired)'))

    def test_the_report_reads_the_same_after_the_apply(self):
        _out, before = self._report()
        self.run_cmd('--apply')
        _out, after = self._report()
        self.assertEqual(after, before)

    def test_the_console_groups_lines_per_item_and_names_unused_items(self):
        self.line_a_inc.delete()
        out, _rows = self._report()
        self.assertIn('OPX-002 REWORD: 3 line(s)', out)
        self.assertIn('not on any line: OPX-205', out)
