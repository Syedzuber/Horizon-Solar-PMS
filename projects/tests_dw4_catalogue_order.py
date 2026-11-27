"""
DW-4 verification — one catalogue order everywhere, matching the master BOQ (27 Nov 2026).

WHAT THIS FILE PROVES
---------------------
  * THE COMMAND (apply_catalogue_order). A dry run writes nothing. --apply sets every
    position in the file and logs one entry per item changed (before -> after) plus one
    summary. A second --apply changes and logs nothing. A code the catalogue does not have,
    a live item the file leaves out, a repeated code and a repeated position are each
    refused before anything is written. An item moved since the file was made is reported,
    not refused (Zuber, DW-4 answer 6).
  * THE FORM (BOQItemMasterForm, Head and Admin screens). A changed position that another
    ACTIVE item of the same type already uses is refused, naming that item; a retired
    item's position does not block; an unchanged position is never re-checked, so the
    pre-reorder duplicate 22 stays editable (answer 4); the change is logged; the Head's
    create form offers the last position + 10; the help text names the neighbours.
  * EVERY READER (P3), TABLE-DRIVEN. One BOQ whose serial_no order, code order and
    catalogue order all differ. Each reader must list its lines in catalogue position
    order; lines with no catalogue item last; the two lines on one item, and the two ACCB
    lines, together in serial_no order. A reader left on serial_no or on code fails.
  * RUNNING ROW NUMBERS (answer 5) on boq_detail and the Residential raise form.
  * SNAPSHOTS (O7). A design group snapshot taken before a reorder renders exactly as before
    and keeps its fingerprint; the live screen shows no "differs" banner.
  * QUERY COUNTS, pinned at the counts measured on the starting commit (42d3dfc): DW-4 adds
    none (P8).

Run with:
    python manage.py test projects.tests_dw4_catalogue_order --settings=solarpms.test_settings
"""
import csv
import io
import re
import tempfile
from decimal import Decimal
from pathlib import Path

from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from openpyxl import load_workbook

from .accb import ACCB_CONFIGURED_CODE
from .boq_formulas import formula_view
from .design_boq_group_views import (
    _live_members, _member_rows, consolidate, group_boq_data, hydrate, snapshot_changes,
    snapshot_fingerprint,
)
from .design_views import _boq_review_panel, aggregate_group_boq
from .models import (
    ActivityLog, BOQ, BOQItem, BOQItemMaster, DesignAssignment, DesignAttempt,
    DesignBOQGroup, DesignBOQGroupMembership, DesignBOQGroupSnapshot, Program, Project,
    ATTEMPT_REASON_INITIAL, DESIGN_IN_QC, DESIGN_RELEASED,
)
from .order_views import _form_context, _project_boq_items
from .site_drawings_views import _boq_rows
from .tests_accb_config import EX_TYPE1, EX_TYPE2
from .views import _accb_create, _boq_copy_plan, _calculated_absent_rows


def _profile(username, role, **flags):
    """A post_save signal auto-creates the UserProfile; fetch and set, never create."""
    user = User.objects.create_user(username=username, password='x')
    profile = user.profile
    profile.role = role
    for name, value in flags.items():
        setattr(profile, name, value)
    profile.save()
    return profile


def _client(profile):
    client = Client(SERVER_NAME='localhost')
    client.force_login(profile.user)
    return client


def _label(code, quantity=None):
    """How a line is named in the expectations: its code, and its quantity where two lines
    share a code (D, the ACCBs), so 'together in serial_no order' is visible."""
    code = code or 'adhoc'
    return code if quantity is None else f'{code}:{Decimal(quantity).normalize():f}'


class ReaderFixture(TestCase):
    """One OPEX site whose three orders all disagree:

        catalogue position  ACCB 5 · A 10 · MAND 15 · B 20 · R 25 (retired) · C 30 · D 40
        code text           D OPX-514 < C 515 < R 516 < B 517 < MAND 518 < A 519 (reversed)
        serial_no           adhoc 0 · D 1 · C 2 · B 3 · A 4 · R 5 · ACCB 5, 5 · D 8

    so a reader ordering by serial_no or by code puts some line in the wrong place.
    """

    @classmethod
    def setUpTestData(cls):
        cls.designer = _profile('dw4_des', 'Design')
        cls.qc = _profile('dw4_qc', 'Design', is_design_qc=True)
        cls.head = _profile('dw4_head', 'Design', is_design_head=True)
        cls.pm = _profile('dw4_pm', 'PM')
        cls.scm = _profile('dw4_scm', 'SCM')
        cls.admin = _profile('dw4_admin', 'Admin')
        cls.program = Program.objects.create(program_type='OPEX', name='DW4 Tender',
                                             client_name='C', status='Active',
                                             short_tender_code='DWF')

        def master(code, sort_order, category, description, unit='Nos', **extra):
            return BOQItemMaster.objects.create(
                code=code, sort_order=sort_order, category=category,
                description=description, unit=unit, project_type='OPEX', **extra)

        cls.A = master('OPX-519', 10, 'Inverter', '10 kW Grid -Tie Inverter @3P')
        cls.MAND = master('OPX-518', 15, 'Inverter', 'Inverter stand', is_mandatory=True)
        cls.B = master('OPX-517', 20, 'Inverter', '80 kW Grid -Tie Inverter @3P')
        cls.R = master('OPX-516', 25, 'Inverter', '150 kW (retired)', is_active=False)
        cls.C = master('OPX-515', 30, 'Conduit', '32/40 DWC HDPE Conduit Pipe', unit='Meter')
        cls.ACCB = master(ACCB_CONFIGURED_CODE, 5, 'ACCB', 'ACCB (configured)')
        cls.D = master('OPX-514', 40, 'Conduit', 'Elbow for 32/40 DWC HDPE conduit')

        cls.site = cls.opex_site('DW4S1')
        boq = cls.site.boq
        for item, serial, qty in ((cls.D, 1, '2'), (cls.C, 2, '30'), (cls.B, 3, '1'),
                                  (cls.A, 4, '1'), (cls.R, 5, '1'), (cls.D, 8, '3')):
            cls.line(boq, item, serial, qty)
        BOQItem.objects.create(boq=boq, serial_no=0, category='Other', item_master=None,
                               description='Ad-hoc fitting', uom='Nos',
                               boq_quantity=Decimal('4'), is_standard_item=False)
        # Two configured ACCB lines: serial_no = the ACCB item's position (5) on both, as
        # the ACCB section writes them, so they tie and fall back to line id.
        _accb_create(boq, cls.ACCB, EX_TYPE1, cls.designer)
        _accb_create(boq, cls.ACCB, EX_TYPE2, cls.designer)

        # The QC holds the review turn on an in_qc site with the BOQ handed in, so the
        # correction screen opens for them (BC-1 / DW-2a).
        assignment = cls.site.design_assignment
        DesignAttempt.objects.create(assignment=assignment, attempt_number=1,
                                     opened_reason=ATTEMPT_REASON_INITIAL,
                                     boq_submitted_at=timezone.now(),
                                     boq_submitted_by=cls.designer)
        assignment.current_attempt_number = 1
        assignment.save(update_fields=['current_attempt_number'])

    @classmethod
    def opex_site(cls, code, status=DESIGN_IN_QC):
        site = Project.objects.create(
            project_id=code, customer_name='C', customer_phone='9876543210',
            site_address='1 Sun Rd', city='Delhi', project_type='OPEX', program=cls.program,
            site_code=code, dc_capacity_kw=Decimal('100.00'), status='Draft',
            assigned_design=cls.designer, assigned_pm=cls.pm, site_name=f'Site {code}')
        DesignAssignment.objects.create(project=site, assigned_to=cls.designer,
                                        qc_assigned_to=cls.qc, status=status,
                                        survey_file_bucket='b',
                                        survey_file_path=f'{code}/survey/x.pdf')
        BOQ.objects.create(project=site)
        site.refresh_from_db()
        return site

    @staticmethod
    def line(boq, item, serial, qty):
        """A line as the picker writes one: text, unit and category copied from the item."""
        return BOQItem.objects.create(
            boq=boq, serial_no=serial, category=item.category, item_master=item,
            description=item.description, uom=item.unit,
            boq_quantity=None if qty is None else Decimal(qty), is_standard_item=True)

    def release(self):
        DesignAssignment.objects.filter(project=self.site).update(
            status=DESIGN_RELEASED, released_at=timezone.now())


# Every line on the site, in catalogue position order (O6).
ALL_LINES = [f'{ACCB_CONFIGURED_CODE}:1', f'{ACCB_CONFIGURED_CODE}:1',
             'OPX-519', 'OPX-517', 'OPX-516', 'OPX-515', 'OPX-514:2', 'OPX-514:3', 'adhoc']
# The picker's catalogue rows (active, not the ACCB item, not retired).
ON_LINES = ['OPX-519', 'OPX-517', 'OPX-515', 'OPX-514:2', 'OPX-514:3']
# Off-catalogue: the retired item's line, then the line with no item (last).
OFF_LINES = ['OPX-516', 'adhoc']


class ReaderOrderTests(ReaderFixture):
    """THE TABLE. Each entry is one reader from the DW-4 pre-flight P3 table, the list it
    produces turned into labels, and what it must be. A new reader belongs here."""

    @staticmethod
    def _lines(rows):
        out = []
        codes = {}
        for row in rows:
            code = row.item_master.code if row.item_master_id else ''
            codes[code] = codes.get(code, 0) + 1
            out.append((code, row.boq_quantity))
        multi = {c for c, n in codes.items() if n > 1}
        return [_label(c, q if c in multi else None) for c, q in out]

    @staticmethod
    def _flat(groups):
        return [row for _category, rows in groups for row in rows]

    # ── the readers ──────────────────────────────────────────────────────────

    def read_split_on(self):
        from .models import split_opex_boq_rows
        on, _off = split_opex_boq_rows(self.site.boq, {m.pk for m in (self.A, self.MAND, self.B,
                                                                      self.C, self.D)})
        return self._lines(on)

    def read_split_off(self):
        from .models import split_opex_boq_rows
        _on, off = split_opex_boq_rows(self.site.boq, {m.pk for m in (self.A, self.MAND, self.B,
                                                                      self.C, self.D)})
        return self._lines(off)

    def read_picker_off_block(self):
        response = _client(self.designer).get(reverse('opex_boq_entry',
                                                      args=[self.site.project_id]))
        self.assertEqual(response.status_code, 200)
        return self._lines(self._flat(response.context['added_off']))

    def read_picker_search_results(self):
        import json
        response = _client(self.designer).get(reverse('opex_boq_entry',
                                                      args=[self.site.project_id]))
        return [row['code'] for row in json.loads(response.context['catalogue_json'])]

    def read_download_data_sheet(self):
        response = _client(self.pm).get(reverse('opex_boq_download',
                                                args=[self.site.project_id]))
        self.assertEqual(response.status_code, 200)
        sheet = load_workbook(io.BytesIO(response.content))['BOQ']
        rows = list(sheet.iter_rows(min_row=2, values_only=True))
        header = [c.value for c in sheet[1]]
        # DW-7: one row per active catalogue item, in catalogue order (the two OPX-514 lines
        # are one row here and are listed on the Other lines sheet).
        return [r[header.index('Code')] for r in rows]

    def read_download_off_sheet(self):
        response = _client(self.pm).get(reverse('opex_boq_download',
                                                args=[self.site.project_id]))
        sheet = load_workbook(io.BytesIO(response.content))['Other lines (read only)']
        rows = [r for r in sheet.iter_rows(min_row=5, values_only=True)]
        codes = [r[0] or 'adhoc' for r in rows]
        return [_label(r[0], r[4]) if codes.count(r[0] or 'adhoc') > 1 else (r[0] or 'adhoc')
                for r in rows]

    def read_correction_screen(self):
        response = _client(self.qc).get(reverse('boq_correct', args=[self.site.project_id]))
        self.assertEqual(response.status_code, 200)
        return self._lines(self._flat(response.context['rows_by_category']))

    def read_review_panel(self):
        panel = _boq_review_panel(self.site.boq)
        return (self._lines(self._flat(panel['boq_by_category']))
                + self._lines(self._flat(panel['boq_off_catalogue'])))

    def read_boq_detail(self):
        response = _client(self.scm).get(reverse('boq_detail', args=[self.site.project_id]))
        self.assertEqual(response.status_code, 200)
        return self._lines(self._flat(response.context['items_by_category']))

    def read_site_card(self):
        rows = _boq_rows(self.site)
        codes = [r['code'] for r in rows]
        return [_label(r['code'], r['quantity']) if r['code'] and codes.count(r['code']) > 1
                else (r['code'] or 'adhoc') for r in rows]

    def read_aggregate(self):
        return [line['item_master__code'] for line in aggregate_group_boq([self.site.pk])['lines']]

    def read_copy_plan_add(self):
        plan = _boq_copy_plan(self.site.boq, None, formula_view(None, 'OPEX'), predict=False)
        return [entry['code'] for entry in plan['upload']['add']]

    def read_copy_plan_skipped(self):
        plan = _boq_copy_plan(self.site.boq, None, formula_view(None, 'OPEX'), predict=False)
        return [entry['code'] for entry in plan['skipped']]

    def read_calculated_absent(self):
        class Formulas:                      # formula_view's absent(), in RULE order
            @staticmethod
            def absent():
                return [(self.D.pk, None), (self.A.pk, None), (self.C.pk, None)]
        by_id = {m.pk: m for m in (self.A, self.B, self.C, self.D, self.MAND)}
        return [row['code'] for row in _calculated_absent_rows(Formulas, by_id)]

    def read_head_catalogue(self):
        response = _client(self.head).get(reverse('design_boq_catalogue'))
        return [item.code for item in response.context['items']]

    def read_admin_catalogue(self):
        response = _client(self.admin).get(reverse('admin_boq_items') + '?type=OPEX')
        return [item.code for item in response.context['items']]

    READERS = (
        ('split_opex_boq_rows: catalogue rows (picker, download, correction, panel)',
         'read_split_on', ON_LINES),
        ('split_opex_boq_rows: off-catalogue rows', 'read_split_off', OFF_LINES),
        ('picker: off-catalogue block', 'read_picker_off_block', OFF_LINES),
        ('picker: search results', 'read_picker_search_results',
         ['OPX-519', 'OPX-518', 'OPX-517', 'OPX-515', 'OPX-514']),
        ('download: data sheet (DW-7: every active catalogue item once)',
         'read_download_data_sheet',
         ['OPX-519', 'OPX-518', 'OPX-517', 'OPX-515', 'OPX-514']),
        ('download: Other lines sheet (DW-7: retired, 2+ lines, no item last)',
         'read_download_off_sheet',
         ['OPX-516', 'OPX-514:2', 'OPX-514:3', 'adhoc']),
        ('correction screen', 'read_correction_screen', ON_LINES),
        ('QC review panel', 'read_review_panel', ON_LINES + OFF_LINES),
        ('boq_detail', 'read_boq_detail', ALL_LINES),
        ('site Drawings & BOQ card', 'read_site_card', ALL_LINES),
        ('aggregate (SCM groups, group raise, design group screen and Excel)',
         'read_aggregate', ['OPX-519', 'OPX-517', 'OPX-516', 'OPX-515', 'OPX-514']),
        ('copy: lines copied', 'read_copy_plan_add', ['OPX-519', 'OPX-517', 'OPX-515']),
        ('copy: lines skipped', 'read_copy_plan_skipped', ['OPX-516', 'OPX-514', 'OPX-514']),
        ('calculated items not on this BOQ', 'read_calculated_absent',
         ['OPX-519', 'OPX-515', 'OPX-514']),
        ('Head catalogue screen', 'read_head_catalogue',
         [ACCB_CONFIGURED_CODE, 'OPX-519', 'OPX-518', 'OPX-517', 'OPX-516', 'OPX-515',
          'OPX-514']),
        ('Admin catalogue screen', 'read_admin_catalogue',
         [ACCB_CONFIGURED_CODE, 'OPX-519', 'OPX-518', 'OPX-517', 'OPX-516', 'OPX-515',
          'OPX-514']),
    )

    def test_every_reader_lists_lines_in_catalogue_order(self):
        for name, method, expected in self.READERS:
            with self.subTest(reader=name):
                self.assertEqual(getattr(self, method)(), expected)

    def test_site_card_after_release(self):
        """The card's own context, on a released site, carries the same order."""
        from .site_drawings_views import site_drawings_context
        self.release()
        rows = site_drawings_context(self.site)['boq_rows']
        self.assertEqual([r['code'] or 'adhoc' for r in rows],
                         [label.split(':')[0] for label in ALL_LINES])

    def test_design_group_warnings_follow_catalogue_order(self):
        """The design group's blank-line warnings, per site, in catalogue order."""
        other = self.opex_site('DW4S2')
        self.line(other.boq, self.C, 1, None)
        self.line(other.boq, self.A, 2, None)
        group = DesignBOQGroup.objects.create(name='DW4 G', created_by=self.head)
        for site in (self.site, other):
            DesignBOQGroupMembership.objects.create(group=group, project=site,
                                                    added_by=self.head)
        blanks = consolidate(_member_rows(_live_members(group)))['blanks']
        self.assertEqual([(b['site'], b['code']) for b in blanks],
                         [('DW4S2', 'OPX-519'), ('DW4S2', 'OPX-515')])

    def test_the_picker_sheet_sorts_by_catalogue_position_not_code(self):
        """renderSheet() runs in the browser, so this checks the shipped script: it sorts
        each category by the item's place in the server's catalogue array (which is in
        position order, see read_picker_search_results), never by code text."""
        response = _client(self.designer).get(reverse('opex_boq_entry',
                                                      args=[self.site.project_id]))
        html = response.content.decode()
        self.assertIn('return POS[a.id] - POS[b.id];', html)
        self.assertNotIn('a.code < b.code', html)

    def test_a_reorder_moves_every_reader(self):
        """Answer 8: a later Head reorder applies to existing BOQs. Move D to the top and
        A to the bottom; the line-level readers follow without any line being rewritten."""
        serials = list(BOQItem.objects.order_by('pk').values_list('serial_no', flat=True))
        BOQItemMaster.objects.filter(pk=self.D.pk).update(sort_order=3)
        BOQItemMaster.objects.filter(pk=self.A.pk).update(sort_order=50)
        self.assertEqual(self.read_site_card(),
                         ['OPX-514:2', 'OPX-514:3',
                          f'{ACCB_CONFIGURED_CODE}:1', f'{ACCB_CONFIGURED_CODE}:1',
                          'OPX-517', 'OPX-516', 'OPX-515', 'OPX-519', 'adhoc'])
        # The correction screen groups by category, in catalogue order too: D now leads, so
        # Conduit (D, D, C) comes before Inverter (B, A).
        self.assertEqual(self.read_correction_screen(),
                         ['OPX-514:2', 'OPX-514:3', 'OPX-515', 'OPX-517', 'OPX-519'])
        self.assertEqual(
            serials, list(BOQItem.objects.order_by('pk').values_list('serial_no', flat=True)))


class RowNumberTests(ReaderFixture):
    """Answer 5: screens that list lines in the new order number them 1, 2, 3… in the order
    shown, not by the stored serial_no (which is never rewritten)."""

    def test_boq_detail_numbers_rows_in_display_order(self):
        response = _client(self.scm).get(reverse('boq_detail', args=[self.site.project_id]))
        shown = [row for _c, rows in response.context['items_by_category'] for row in rows]
        self.assertEqual([row.row_number for row in shown], list(range(1, len(shown) + 1)))
        html = response.content.decode()
        # The first row's number cell reads 1, not the stored serial_no (4 for OPX-519).
        self.assertInHTML('<td class="text-muted small align-middle">1</td>', html, count=1)
        self.assertEqual(BOQItem.objects.get(boq=self.site.boq, item_master=self.A).serial_no, 4)

    def test_residential_raise_form_lists_and_numbers_in_catalogue_order(self):
        res_a = BOQItemMaster.objects.create(code='ITM-902', sort_order=1, category='Inverter',
                                             description='Res inverter', unit='Nos')
        res_b = BOQItemMaster.objects.create(code='ITM-901', sort_order=2, category='Inverter',
                                             description='Res inverter 2', unit='Nos')
        site = Project.objects.create(
            project_id='DW4-RES', customer_name='H', customer_phone='9876543211',
            site_address='2 Sun Rd', city='Delhi', project_type='Residential',
            dc_capacity_kw=Decimal('5.00'), status='Draft', assigned_pm=self.pm)
        boq = BOQ.objects.create(project=site)
        for item, serial in ((res_b, 1), (res_a, 7)):
            BOQItem.objects.create(boq=boq, serial_no=serial, category='Inverter',
                                   item_master=item, description=item.description, uom='Nos',
                                   boq_quantity=Decimal('1'))
        items = _project_boq_items(site)
        self.assertEqual([i.item_master.code for i in items], ['ITM-902', 'ITM-901'])
        groups = _form_context(site, items)['groups']
        rows = [row for group in groups for row in group['rows']]
        self.assertEqual([(row['number'], row['item'].item_master.code) for row in rows],
                         [(1, 'ITM-902'), (2, 'ITM-901')])
        response = _client(self.scm).get(reverse('vendor_order_create', args=[site.pk]),
                                         follow=True)
        html = response.content.decode()
        self.assertIn('1. Res inverter', html)
        self.assertNotIn('7. Res inverter', html)


class SnapshotUnchangedTests(ReaderFixture):
    """O7: a snapshot keeps the order it stored. A reorder changes the live screen only, and
    the fingerprint, which never read order, does not move."""

    def test_snapshot_renders_and_fingerprints_as_before_a_reorder(self):
        group = DesignBOQGroup.objects.create(name='DW4 Snap', created_by=self.head)
        DesignBOQGroupMembership.objects.create(group=group, project=self.site,
                                                added_by=self.head)

        def live():
            rows = _member_rows(_live_members(group))
            return group_boq_data(group.name, rows, consolidate(rows))

        data = live()
        snapshot = DesignBOQGroupSnapshot.objects.create(
            group=group, taken_by=self.head, taken_by_name='Head', data=data,
            fingerprint=snapshot_fingerprint(data), site_ids=[self.site.pk],
            sites_counted=1, sites_not_counted=0)
        page = reverse('design_boq_group_snapshot_detail', args=[group.pk, snapshot.pk])

        def render():
            # The page carries a fresh CSRF token on every request (64 characters, in the
            # htmx headers); everything else must be byte for byte the same.
            return re.sub(rb'\b[A-Za-z0-9]{64}\b', b'<csrf>',
                          _client(self.head).get(page).content)

        before_html = render()
        before_order = [l['item_master__code'] for l in hydrate(snapshot.data)['boq']['lines']]

        # The reorder: the first item goes last, the last first.
        BOQItemMaster.objects.filter(pk=self.A.pk).update(sort_order=500)
        BOQItemMaster.objects.filter(pk=self.D.pk).update(sort_order=1)

        snapshot.refresh_from_db()
        self.assertEqual(render(), before_html)
        self.assertEqual([l['item_master__code'] for l in hydrate(snapshot.data)['boq']['lines']],
                         before_order)
        self.assertEqual(snapshot_fingerprint(snapshot.data), snapshot.fingerprint)
        self.assertEqual(snapshot_fingerprint(live()), snapshot.fingerprint)
        self.assertFalse(any(snapshot_changes(snapshot.data, live()).values()))
        # The live order did move, so the comparison above is not vacuous.
        self.assertEqual([l['code'] for l in live()['lines']][0], 'OPX-514')
        detail = _client(self.head).get(reverse('design_boq_group_detail', args=[group.pk]))
        self.assertNotContains(detail, 'id="boqGroupDiffers"')


# ═════════════════════════════════════════════════════════════════════════════
# Query counts (P8): measured on the starting commit 42d3dfc with this fixture, as the
# designer / SCM / PM / QC / Head / Admin named. DW-4 switches the order inside queries that
# already join the catalogue item, so every count must be unchanged.
# ═════════════════════════════════════════════════════════════════════════════

QUERY_PINS = {
    'picker GET (designer)':        20,
    'boq_detail (SCM)':             17,
    # DW-7: 11. BOQ and program joined into the project query (-2), category order gone (-1),
    # the rules check (+1, none in this fixture) and DW-1's groups (+1); no override query
    # without a calculated item. Flat in the catalogue's size (tests_dw7_master_boq_sheet).
    'download (PM)':                11,
    # DW-2b (Q7): 23. The ACCB pop-up's one order check, for this fixture's two ACCBs
    # (flat in their number: tests_dw2b_reviewer_accb).
    'correction screen (QC)':       23,
    'QC review (QC)':               34,
    'site card rows':               1,
    'raise form, Residential (SCM)': 13,
    'design group screen (Head)':   17,   # DW-6: +1 the rules that raise warnings
    'design group Excel (Head)':    12,   # DW-6: +1 the rules that raise warnings
    'Head catalogue':               7,
    'Admin catalogue':              10,
}


class QueryCountTests(ReaderFixture):

    def _count(self, profile, url, follow=False):
        client = _client(profile)
        client.get(url, follow=follow)                  # warm the session
        with CaptureQueriesContext(connection) as ctx:
            response = client.get(url, follow=follow)
        self.assertEqual(response.status_code, 200, url)
        return len(ctx.captured_queries)

    def test_query_counts_are_pinned(self):
        group = DesignBOQGroup.objects.create(name='DW4 Q', created_by=self.head)
        DesignBOQGroupMembership.objects.create(group=group, project=self.site,
                                                added_by=self.head)
        res = Project.objects.create(
            project_id='DW4-RQ', customer_name='H', customer_phone='9876543211',
            site_address='2 Sun Rd', city='Delhi', project_type='Residential',
            dc_capacity_kw=Decimal('5.00'), status='Draft', assigned_pm=self.pm)
        res_item = BOQItemMaster.objects.create(code='ITM-950', sort_order=1,
                                                category='Inverter', description='Res inv',
                                                unit='Nos')
        BOQItem.objects.create(boq=BOQ.objects.create(project=res), serial_no=1,
                               category='Inverter', item_master=res_item,
                               description='Res inv', uom='Nos', boq_quantity=Decimal('1'))
        pid = self.site.project_id

        def card():
            with CaptureQueriesContext(connection) as ctx:
                _boq_rows(self.site)
            return len(ctx.captured_queries)

        measured = {
            'picker GET (designer)':  self._count(self.designer, reverse('opex_boq_entry', args=[pid])),
            'boq_detail (SCM)':       self._count(self.scm, reverse('boq_detail', args=[pid])),
            'download (PM)':          self._count(self.pm, reverse('opex_boq_download', args=[pid])),
            'correction screen (QC)': self._count(self.qc, reverse('boq_correct', args=[pid])),
            'QC review (QC)':         self._count(self.qc, reverse('design_qc_review', args=[pid])),
            'site card rows':         card(),
            'raise form, Residential (SCM)': self._count(
                self.scm, reverse('vendor_order_create', args=[res.pk]), follow=True),
            'design group screen (Head)': self._count(
                self.head, reverse('design_boq_group_detail', args=[group.pk])),
            'design group Excel (Head)': self._count(
                self.head, reverse('design_boq_group_excel', args=[group.pk])),
            'Head catalogue':  self._count(self.head, reverse('design_boq_catalogue')),
            'Admin catalogue': self._count(self.admin, reverse('admin_boq_items') + '?type=OPEX'),
        }
        print('\nDW-4 measured query counts:', measured)
        self.assertEqual(measured, QUERY_PINS)


# ═════════════════════════════════════════════════════════════════════════════
# The command
# ═════════════════════════════════════════════════════════════════════════════

class CommandTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        make = BOQItemMaster.objects.create
        cls.x = make(code='OPX-601', sort_order=22, category='Inverter', description='X',
                     unit='Nos', project_type='OPEX')
        cls.y = make(code='OPX-602', sort_order=22, category='Inverter', description='Y',
                     unit='Nos', project_type='OPEX')
        cls.z = make(code='OPX-603', sort_order=3, category='Conduit', description='Z',
                     unit='Nos', project_type='OPEX', is_active=False)
        cls.res = make(code='ITM-601', sort_order=1, category='Inverter', description='R',
                       unit='Nos', project_type='Residential')

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def write(self, rows, name='order.csv'):
        path = Path(self.tmp.name) / name
        with open(path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f, lineterminator='\n')
            writer.writerow(['code', 'sort_order', 'master_row', 'category', 'was_sort_order',
                             'description'])
            writer.writerows(rows)
        return path

    def good(self):
        return self.write([['OPX-602', '10', '1', 'Inverter', '22', 'Y'],
                           ['OPX-601', '20', '2', 'Inverter', '22', 'X'],
                           ['OPX-603', '30', '', 'Conduit', '3', 'Z']])

    def run_command(self, path, apply=False):
        out = io.StringIO()
        call_command('apply_catalogue_order', file=str(path), apply=apply, stdout=out)
        return out.getvalue()

    def positions(self):
        return dict(BOQItemMaster.objects.values_list('code', 'sort_order'))

    def logs(self):
        return list(ActivityLog.objects.filter(entity_type='BOQItemMaster')
                    .order_by('pk').values_list('action_code', 'action'))

    def test_dry_run_writes_nothing_and_prints_the_checksum(self):
        before = self.positions()
        out = self.run_command(self.good())
        self.assertEqual(self.positions(), before)
        self.assertEqual(self.logs(), [])
        self.assertIn('sha256: ', out)
        self.assertIn('DRY RUN', out)
        self.assertIn('OPX-602  22 -> 10', out)

    def test_apply_sets_every_position_and_logs_each_change_plus_a_summary(self):
        self.run_command(self.good(), apply=True)
        self.assertEqual(self.positions(),
                         {'OPX-602': 10, 'OPX-601': 20, 'OPX-603': 30, 'ITM-601': 1})
        logs = self.logs()
        self.assertEqual([code for code, _ in logs],
                         ['boq_catalogue_item_moved'] * 3 + ['boq_catalogue_order_applied'])
        self.assertIn("'OPX-602': sort order: 22 → 10", logs[0][1])
        self.assertIn('3 positions changed', logs[-1][1])

    def test_a_second_apply_changes_and_logs_nothing(self):
        path = self.good()
        self.run_command(path, apply=True)
        count = ActivityLog.objects.count()
        out = self.run_command(path, apply=True)
        self.assertEqual(ActivityLog.objects.count(), count)
        self.assertIn('Nothing to change', out)
        self.assertNotIn('moved since the file was made', out)

    def assertRefused(self, path):
        """--apply raises and writes nothing: no position moves, no entry is logged."""
        before, logs = self.positions(), self.logs()
        with self.assertRaises(CommandError):
            self.run_command(path, apply=True)
        self.assertEqual(self.positions(), before)
        self.assertEqual(self.logs(), logs)

    def test_a_code_the_catalogue_does_not_have_is_refused(self):
        path = self.write([['OPX-602', '10', '', 'Inverter', '22', 'Y'],
                           ['OPX-601', '20', '', 'Inverter', '22', 'X'],
                           ['OPX-603', '30', '', 'Conduit', '3', 'Z'],
                           ['OPX-699', '40', '', 'Conduit', '4', 'Ghost']])
        self.assertRefused(path)
        self.assertIn('OPX-699: not a RESCO catalogue item', self.run_command(path))

    def test_a_residential_code_is_refused(self):
        path = self.write([['OPX-602', '10', '', 'Inverter', '22', 'Y'],
                           ['OPX-601', '20', '', 'Inverter', '22', 'X'],
                           ['OPX-603', '30', '', 'Conduit', '3', 'Z'],
                           ['ITM-601', '40', '', 'Inverter', '1', 'R']])
        self.assertRefused(path)

    def test_a_live_item_missing_from_the_file_is_refused(self):
        path = self.write([['OPX-602', '10', '', 'Inverter', '22', 'Y'],
                           ['OPX-601', '20', '', 'Inverter', '22', 'X']])
        self.assertRefused(path)
        self.assertIn('OPX-603: in the catalogue but not in the file', self.run_command(path))

    def test_a_repeated_position_is_refused(self):
        path = self.write([['OPX-602', '10', '', 'Inverter', '22', 'Y'],
                           ['OPX-601', '10', '', 'Inverter', '22', 'X'],
                           ['OPX-603', '30', '', 'Conduit', '3', 'Z']])
        self.assertRefused(path)
        self.assertIn('position 10 is given to OPX-602 and OPX-601', self.run_command(path))

    def test_a_repeated_code_is_refused(self):
        path = self.write([['OPX-602', '10', '', 'Inverter', '22', 'Y'],
                           ['OPX-602', '20', '', 'Inverter', '22', 'Y'],
                           ['OPX-601', '25', '', 'Inverter', '22', 'X'],
                           ['OPX-603', '30', '', 'Conduit', '3', 'Z']])
        self.assertRefused(path)

    def test_a_bad_header_or_number_is_refused(self):
        bad = Path(self.tmp.name) / 'bad.csv'
        bad.write_text('code,position\nOPX-601,10\n', encoding='utf-8')
        self.assertRefused(bad)
        self.assertRefused(self.write([['OPX-602', 'ten', '', 'Inverter', '22', 'Y'],
                                       ['OPX-601', '20', '', 'Inverter', '22', 'X'],
                                       ['OPX-603', '30', '', 'Conduit', '3', 'Z']]))

    def test_an_item_moved_since_the_file_was_made_is_reported_not_refused(self):
        BOQItemMaster.objects.filter(pk=self.z.pk).update(sort_order=7)
        out = self.run_command(self.good(), apply=True)
        self.assertIn('OPX-603: was 3 in the file, is 7 now', out)
        self.assertEqual(self.positions()['OPX-603'], 30)


# ═════════════════════════════════════════════════════════════════════════════
# The Head's (and the Admin's) position field — O5, answer 4
# ═════════════════════════════════════════════════════════════════════════════

class PositionFieldTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.head = _profile('dw4f_head', 'Design', is_design_head=True)
        cls.admin = _profile('dw4f_admin', 'Admin')
        make = BOQItemMaster.objects.create
        cls.pipe = make(code='OPX-130', sort_order=1780, category='Conduit', unit='Meter',
                        description='32/40 DWC HDPE Conduit Pipe', project_type='OPEX')
        cls.elbow = make(code='OPX-256', sort_order=1790, category='Conduit', unit='Nos',
                         description='Elbow for 32/40 DWC HDPE conduit', project_type='OPEX')
        cls.tee = make(code='OPX-257', sort_order=1800, category='Conduit', unit='Nos',
                       description='Tee for 32/40 DWC HDPE conduit', project_type='OPEX')
        cls.old = make(code='OPX-147', sort_order=2060, category='Conduit', unit='Pkt',
                       description='SS Saddle for 32MM conduit (100PCS)', project_type='OPEX',
                       is_active=False)
        # A pre-existing duplicate, as OPX-022 and OPX-208 shared 22 before the reorder.
        cls.twin = make(code='OPX-208', sort_order=2500, category='BOS', unit='Nos',
                        description='150 kW inverter', project_type='OPEX')
        cls.twin2 = make(code='OPX-022', sort_order=2500, category='BOS', unit='Nos',
                         description='125 kW inverter', project_type='OPEX')
        cls.res = make(code='ITM-001', sort_order=1795, category='Solar Modules',
                       unit='Nos', description='Residential module')

    def post(self, profile, item, **changes):
        data = {'description': item.description, 'unit': item.unit, 'category': item.category,
                'sort_order': item.sort_order, 'is_active': 'on' if item.is_active else ''}
        if profile.role == 'Admin':
            data['project_type'] = item.project_type
            url = reverse('admin_boq_item_edit', args=[item.pk])
        else:
            url = reverse('design_boq_catalogue_edit', args=[item.pk])
        data.update(changes)
        data = {k: v for k, v in data.items() if v != ''}
        return _client(profile).post(url, data)

    def test_a_used_position_is_refused_naming_the_item(self):
        response = self.post(self.head, self.tee, sort_order=1790)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Position 1790 is already used by OPX-256 '
                                      '(Elbow for 32/40 DWC HDPE conduit).')
        self.tee.refresh_from_db()
        self.assertEqual(self.tee.sort_order, 1800)

    def test_the_admin_screen_refuses_it_too(self):
        response = self.post(self.admin, self.tee, sort_order=1780)
        self.assertContains(response, 'Position 1780 is already used by OPX-130')

    def test_a_retired_items_position_does_not_block(self):
        response = self.post(self.head, self.tee, sort_order=2060)
        self.assertEqual(response.status_code, 302)
        self.tee.refresh_from_db()
        self.assertEqual(self.tee.sort_order, 2060)

    def test_another_project_types_position_does_not_block(self):
        # ITM-001 holds 1795 on the Residential template; an OPEX item may still take it.
        response = self.post(self.head, self.pipe, sort_order=1795)
        self.assertEqual(response.status_code, 302)
        self.pipe.refresh_from_db()
        self.assertEqual(self.pipe.sort_order, 1795)

    def test_an_unchanged_position_is_not_rechecked(self):
        # OPX-208 and OPX-022 share 2500. Editing the wording of one must still save
        # (answer 4): the position was not changed, so it is not checked.
        response = self.post(self.head, self.twin, description='150 kW inverter, reworded')
        self.assertEqual(response.status_code, 302)

    def test_a_free_position_moves_the_item_and_is_logged(self):
        response = self.post(self.head, self.tee, sort_order=1785)
        self.assertEqual(response.status_code, 302)
        self.tee.refresh_from_db()
        self.assertEqual(self.tee.sort_order, 1785)
        entry = ActivityLog.objects.filter(entity_type='BOQItemMaster',
                                           entity_id=self.tee.pk).latest('pk')
        self.assertIn('sort order: 1800 → 1785', entry.action)

    def test_the_refusal_suggests_a_free_position_between_the_neighbours(self):
        response = self.post(self.head, self.tee, sort_order=1780)
        self.assertContains(response, '1785 would sit between OPX-130 and OPX-256')

    def test_the_help_text_names_the_items_either_side(self):
        response = _client(self.head).get(reverse('design_boq_catalogue_edit',
                                                  args=[self.elbow.pk]))
        self.assertContains(response, 'Now between OPX-130 (1780, 32/40 DWC HDPE Conduit '
                                      'Pipe) and OPX-257 (1800, Tee for 32/40 DWC HDPE conduit)')

    def test_the_admin_create_form_offers_last_plus_ten_in_its_own_catalogue(self):
        """Build answer 1: last + 10 within the item's own template, not across the table.
        RESCO's last is 2500 (OPX-022/208), Residential's is ITM-001's 1795."""
        client = _client(self.admin)
        opex = client.get(reverse('admin_boq_item_create') + '?type=OPEX')
        self.assertEqual(opex.context['form'].initial['sort_order'], 2510)
        self.assertEqual(opex.context['form'].initial['project_type'], 'OPEX')
        self.assertContains(opex, 'At the end, after OPX-208 (2500, 150 kW inverter)')
        residential = client.get(reverse('admin_boq_item_create'))
        self.assertEqual(residential.context['form'].initial['sort_order'], 1805)
        self.assertEqual(residential.context['form'].initial['project_type'], 'Residential')
        unknown = client.get(reverse('admin_boq_item_create') + '?type=Nope')
        self.assertEqual(unknown.context['form'].initial['sort_order'], 1805)

    def test_the_admin_list_add_link_carries_its_type_filter(self):
        client = _client(self.admin)
        self.assertContains(client.get(reverse('admin_boq_items') + '?type=OPEX'),
                            'href="' + reverse('admin_boq_item_create') + '?type=OPEX"')
        self.assertContains(client.get(reverse('admin_boq_items')),
                            'href="' + reverse('admin_boq_item_create') + '"')

    def test_the_admin_create_refuses_a_used_position_too(self):
        response = _client(self.admin).post(reverse('admin_boq_item_create'), {
            'code': 'OPX-999', 'project_type': 'OPEX', 'description': 'New elbow',
            'unit': 'Nos', 'category': 'Conduit', 'sort_order': 1790, 'is_active': 'on'})
        self.assertContains(response, 'Position 1790 is already used by OPX-256')
        self.assertFalse(BOQItemMaster.objects.filter(code='OPX-999').exists())

    def test_the_heads_create_form_offers_the_last_position_plus_ten(self):
        response = _client(self.head).get(reverse('design_boq_catalogue_create'))
        # The last position of ANY item, retired included (OPX-147 is not last here).
        self.assertEqual(response.context['form'].initial['sort_order'], 2510)
        self.assertContains(response, 'At the end, after OPX-208 (2500, 150 kW inverter)')
