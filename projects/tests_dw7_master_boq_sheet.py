"""
DW-7 verification — one master BOQ sheet for download and upload (27 Nov 2026).

WHAT THIS FILE PROVES
---------------------
  * THE ROUND TRIP (the target). One site carrying every kind of line — typed, blank, zero,
    mandatory, calculated, overridden, a configured ACCB, a retired item, a line with no
    item, and an item on two lines — is downloaded and uploaded unchanged: the preview says
    "This file changes nothing" and offers no Confirm; a confirm posted anyway leaves every
    line, override and recalculation record exactly as it was.
  * THE SITE FILE (L1-L5). Instructions first; the BOQ sheet lists every active catalogue
    item once in catalogue order, under Code, Category, Description, Unit, Quantity,
    Calculated, Required; calculated rows grey and locked; ACCB (read only) and Other lines
    (read only) as ruled (answers 4-6); DW-1's minimum content marked (answer 11).
  * THE BLANK MASTER (L8). Same rows and columns, no quantities, no reference sheets; filled
    and uploaded to a site, the preview adds exactly the filled rows.
  * THE UPLOAD. An old-order file uploads as before; a figure typed into a calculated row is
    ignored and the preview says so (D3); a stale calculated line is listed only as brought
    up to date by its rule (answer 2); a figure for an item on two lines is not written
    (answer 4); the L7 refusals name what was found.
  * GATES (L9). The blank master and its buttons for a designer, QC and the Head; refused to
    SCM and the PM.
  * QUERY COUNTS (P9, answer 10), and that no download grows with the catalogue.

Run with:
    python manage.py test projects.tests_dw7_master_boq_sheet --settings=solarpms.test_settings
"""
import json
from decimal import Decimal
from io import BytesIO

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from openpyxl import Workbook, load_workbook

from .design_boq_group_views import SUMMARY_HEADERS, UNLINKED_HEADERS
from .models import (
    ActivityLog, BOQ, BOQItem, BOQItemMaster, BOQLineOverride, BOQRecalculation,
)
from .tests_accb_config import EX_TYPE1
from .tests_boq_formula_apply import FormulaBase, _profile
from .views import (
    BOQ_DOWNLOAD_ACCB_SHEET, BOQ_DOWNLOAD_INFO_SHEET, BOQ_DOWNLOAD_OFF_SHEET,
    BOQ_DOWNLOAD_SHEET, BOQ_UPLOAD_USE_THE_DOWNLOAD, _accb_create,
)

D = Decimal

#: The ruled column order (DW-R9) and the two markers after it (L1).
HEADERS = ['Code', 'Category', 'Description', 'Unit', 'Quantity', 'Calculated', 'Required']
#: The catalogue, in sort_order: FormulaBase's FX items (9001-9008) plus DW-7's own.
EXTRA_ITEMS = [
    # (code, description, unit, category, sort_order, extra)
    ('FX-MAND', 'Module mounting clamp', 'Nos', 'Modules', 8990, {'is_mandatory': True}),
    ('FX-DCDB', 'DCDB 2 In 2 Out',       'Nos', 'DCDB',    9010, {}),
    ('FX-ACDB', 'ACDB 1 In 1 Out',       'Nos', 'ACDB',    9011, {}),
    ('FX-DUP',  'Cable tie',             'Nos', 'BOS',     9012, {}),
    ('FX-OLD',  'Retired junction box',  'Nos', 'BOS',     9013, {'is_active': False}),
]
GREY = 'FFD9D9D9'

#: P9 / answer 10 — measured on this fixture (rules active, a BOQ with an override).
#: Site download: 8 for the session, user, project (+BOQ +program) and the read gate; then
#: catalogue, lines, ACCB lines, overrides, mandatory items, the rules (4) and DW-1's
#: groups. No query per catalogue row: adding 30 items changes none of these.
PINS = {
    'site download (designer)': 18,
    'blank master (designer)':  10,
    'upload preview, round trip (designer)': 19,
    # DW-7b: the round-trip confirm is refused before any write (was 25 when it ran).
    'upload confirm, round trip (designer)': 20,
}


def _client(profile):
    client = Client(SERVER_NAME='localhost')
    client.force_login(profile.user)
    return client


class MasterSheetBase(FormulaBase):
    """FormulaBase's catalogue and rules, plus one site carrying every kind of line.

        FX-PANEL 10 typed · FX-MAND 1 (mandatory) · FX-INV1 12 · FX-LA1 2 · FX-DCDB blank ·
        FX-ACDB 0 · FX-EARTH 10, FX-LOG overridden to 4, FX-RS485 20 (calculated) · ACCB 1 ·
        FX-OLD 3 (retired) · 'Ad-hoc fitting' 4 (no item) · FX-DUP 2 and 3 (two lines)
    """

    def setUp(self):
        super().setUp()
        for code, description, unit, category, sort_order, extra in EXTRA_ITEMS:
            self.items[code] = BOQItemMaster.objects.create(
                code=code, description=description, unit=unit, category=category,
                project_type='OPEX', sort_order=sort_order, **{'is_active': True, **extra})
        self.scm = _profile('dw7scm', 'SCM')
        self._rules()
        # DW-1's minimum-content groups, by the names DW-1 reads (models.BOQ_MINIMUM_CONTENT).
        self._group('all inverters', 'inverters', ['FX-INV1', 'FX-INV2'])
        self._group('all DCDBs', 'DCDBs', ['FX-DCDB'])
        self._group('all ACDBs', 'ACDBs', ['FX-ACDB'])

        self._login(self.designer)
        # The calculated lines are created by the engine through the picker's own save.
        self._picker({'FX-PANEL': 10, 'FX-MAND': 1, 'FX-INV1': 12, 'FX-LA1': 2,
                      'FX-DCDB': None, 'FX-ACDB': 0})
        self._override('set', self._lines('FX-LOG')[0], quantity='4', note='two roofs')
        boq = self.site.boq
        _accb_create(boq, self.accb_master, EX_TYPE1, self.designer)
        self._line('FX-OLD', 3)
        self._line('FX-DUP', 2)
        self._line('FX-DUP', 3)
        BOQItem.objects.create(boq=boq, serial_no=0, category='Other', item_master=None,
                               description='Ad-hoc fitting', uom='Nos', boq_quantity=D('4'),
                               is_standard_item=False)

    # ── helpers ─────────────────────────────────────────────────────────────

    def _catalogue_codes(self):
        """Every active OPEX item but OPX-273, in sort_order: the BOQ sheet's rows."""
        return list(BOQItemMaster.objects.filter(is_active=True, project_type='OPEX')
                    .exclude(code='OPX-273').order_by('sort_order', 'code')
                    .values_list('code', flat=True))

    def _download(self, profile=None, site=None):
        response = _client(profile or self.designer).get(
            reverse('opex_boq_download', args=[(site or self.site).project_id]))
        self.assertEqual(response.status_code, 200)
        return response

    def _master(self, profile=None):
        return _client(profile or self.designer).get(reverse('opex_boq_master_download'))

    @staticmethod
    def _book(content):
        return load_workbook(BytesIO(content))

    @staticmethod
    def _rows(ws, min_row=2):
        return list(ws.iter_rows(min_row=min_row, values_only=True))

    def _by_code(self, ws):
        return {row[0]: row for row in self._rows(ws)}

    @staticmethod
    def _bytes(wb):
        out = BytesIO()
        wb.save(out)
        return out.getvalue()

    def _preview(self, content, site=None, name='boq.xlsx'):
        url = reverse('opex_boq_upload', args=[(site or self.site).project_id])
        return self.client.post(url, {'phase': 'preview',
                                      'file': SimpleUploadedFile(name, content)})

    def _commit(self, preview, site=None):
        url = reverse('opex_boq_upload', args=[(site or self.site).project_id])
        return self.client.post(url, {'phase': 'commit',
                                      'rows_json': preview.context['rows_json']})

    def _snapshot(self, site=None):
        """Everything a round trip must leave alone: every line field the upload could
        touch, every override, and the recalculation trail."""
        site = site or self.site
        lines = sorted(BOQItem.objects.filter(boq__project=site).values_list(
            'pk', 'item_master_id', 'boq_quantity', 'description', 'uom', 'category',
            'serial_no', 'is_standard_item', 'ordered_quantity'))
        overrides = sorted(BOQLineOverride.objects.filter(line__boq__project=site)
                           .values_list('line_id', 'note', 'set_as'))
        return lines, overrides, BOQRecalculation.objects.filter(boq__project=site).count()

    def _count(self, call):
        call()                                  # warm the session and caches
        with CaptureQueriesContext(connection) as ctx:
            call()
        return len(ctx.captured_queries)


# ═════════════════════════════════════════════════════════════════════════════
# The round trip
# ═════════════════════════════════════════════════════════════════════════════

class RoundTripTests(MasterSheetBase):

    def test_the_fixture_carries_every_kind_of_line(self):
        self.assertEqual(self._qty('FX-EARTH'), D(10))
        self.assertEqual(self._qty('FX-LOG'), D(4))
        self.assertEqual(self._qty('FX-RS485'), D(20))
        self.assertEqual(self._qty('FX-DCDB'), None)
        self.assertEqual(self._qty('FX-ACDB'), D(0))
        self.assertEqual(len(self._lines('FX-DUP')), 2)
        self.assertEqual(len(self._lines(self.accb_master.code)), 1)
        self.assertTrue(BOQItem.objects.filter(boq__project=self.site,
                                               item_master__isnull=True).exists())

    def test_download_then_upload_unchanged_reports_no_changes(self):
        preview = self._preview(self._download().content)

        self.assertEqual(preview.context['stage'], 'preview')
        self.assertTrue(preview.context['no_changes'])
        self.assertEqual(preview.context['upload_summary'], {
            'added': [], 'changed': [], 'unchanged': 6, 'readded': [], 'replaced': [],
            'kept_override': [], 'removed': [], 'recalculated': [], 'on_lines': []})
        self.assertContains(preview, 'This file changes nothing.')
        self.assertNotContains(preview, 'Confirm and apply')

    def test_a_confirm_posted_directly_is_refused_and_writes_nothing(self):
        """DW-7b: the server re-plans the confirm; a file that changes nothing returns to
        the picker, every line, override and recalculation record as it was, no activity."""
        before = self._snapshot()
        logged = ActivityLog.objects.filter(action_code='boq_uploaded').count()
        preview = self._preview(self._download().content)

        result = self._commit(preview)

        self.assertRedirects(result, reverse('opex_boq_entry', args=[self.site.project_id]),
                             fetch_redirect_response=False)
        page = self.client.get(result['Location'])
        self.assertIn('This file changes nothing. Nothing was saved.',
                      [str(m) for m in page.context['messages']])
        self.assertEqual(self._snapshot(), before)
        self.assertEqual(ActivityLog.objects.filter(action_code='boq_uploaded').count(), logged)

    def test_a_direct_confirm_on_a_site_with_no_boq_creates_nothing(self):
        """DW-7b: no BOQ created and nothing put back. With no mandatory item, a blank file
        on a site with no BOQ changes nothing."""
        BOQItemMaster.objects.filter(code='FX-MAND').update(is_mandatory=False)
        site, _assignment = self._opex_site('FX-NONE')
        url = reverse('opex_boq_upload', args=[site.project_id])
        rows_json = json.dumps([{'code': code, 'quantity': None}
                                for code in self._catalogue_codes()])

        result = self.client.post(url, {'phase': 'commit', 'rows_json': rows_json})

        self.assertRedirects(result, reverse('opex_boq_entry', args=[site.project_id]),
                             fetch_redirect_response=False)
        self.assertFalse(BOQ.objects.filter(project=site).exists())
        self.assertFalse(ActivityLog.objects.filter(project=site,
                                                    action_code='boq_uploaded').exists())

    def test_a_confirm_that_changes_something_is_still_applied(self):
        """The refusal is only for "nothing at all": one changed figure goes through."""
        wb = self._book(self._download().content)
        for row in wb[BOQ_DOWNLOAD_SHEET].iter_rows(min_row=2):
            if row[0].value == 'FX-PANEL':
                row[4].value = 11

        result = self._commit(self._preview(self._bytes(wb)))

        self.assertEqual(result.context['stage'], 'result')
        self.assertEqual(self._qty('FX-PANEL'), D(11))

    def test_a_site_with_no_boq_round_trips_to_a_put_back_only(self):
        """The blank file of a site with no BOQ: the mandatory item is put back (it is a
        change, so the preview is not "changes nothing"), and nothing else is added."""
        site, _assignment = self._opex_site('FX-NEW')
        preview = self._preview(self._download(site=site).content, site=site)

        summary = preview.context['upload_summary']
        self.assertEqual((summary['added'], summary['readded']), ([], ['FX-MAND']))
        self.assertFalse(preview.context['no_changes'])
        self.assertFalse(BOQ.objects.filter(project=site).exists())


# ═════════════════════════════════════════════════════════════════════════════
# The site file
# ═════════════════════════════════════════════════════════════════════════════

class SiteFileTests(MasterSheetBase):

    def setUp(self):
        super().setUp()
        self.response = self._download()
        self.wb = self._book(self.response.content)
        self.ws = self.wb[BOQ_DOWNLOAD_SHEET]

    def test_sheets_instructions_first_then_boq_accb_and_other_lines(self):
        self.assertEqual(self.wb.sheetnames, [BOQ_DOWNLOAD_INFO_SHEET, BOQ_DOWNLOAD_SHEET,
                                              BOQ_DOWNLOAD_ACCB_SHEET, BOQ_DOWNLOAD_OFF_SHEET])
        self.assertEqual((BOQ_DOWNLOAD_ACCB_SHEET, BOQ_DOWNLOAD_OFF_SHEET),
                         ('ACCB (read only)', 'Other lines (read only)'))
        for ws in self.wb.worksheets:
            self.assertTrue(ws.protection.sheet, ws.title)

    def test_the_file_name_is_the_site_and_the_date(self):
        self.assertEqual(self.response['Content-Disposition'],
                         f'attachment; filename="FX-A_BOQ_{timezone.localdate().isoformat()}.xlsx"')

    def test_the_five_ruled_columns_come_first_then_the_markers(self):
        self.assertEqual([c.value for c in self.ws[1]], HEADERS)

    def test_every_active_item_once_in_catalogue_order(self):
        codes = [row[0] for row in self._rows(self.ws)]
        self.assertEqual(codes, self._catalogue_codes())
        self.assertNotIn('FX-OLD', codes)
        self.assertNotIn(self.accb_master.code, codes)

    def test_quantities_are_the_stored_figures_and_blank_stays_blank(self):
        rows = self._by_code(self.ws)
        quantity = {code: row[4] for code, row in rows.items()}
        self.assertEqual((quantity['FX-PANEL'], quantity['FX-ACDB'], quantity['FX-EARTH'],
                          quantity['FX-LOG'], quantity['FX-RS485']), (10, 0, 10, 4, 20))
        # A blank line, an item not on the site and an item on two lines are all blank.
        self.assertEqual((quantity['FX-DCDB'], quantity['FX-INV2'], quantity['FX-DUP']),
                         (None, None, None))

    def test_calculated_overridden_and_two_line_rows_are_marked(self):
        marks = {code: row[5] for code, row in self._by_code(self.ws).items()}
        self.assertEqual(marks['FX-EARTH'], 'Calculated')
        self.assertEqual(marks['FX-RS485'], 'Calculated')
        self.assertEqual(marks['FX-LOG'], 'Overridden: two roofs')
        self.assertEqual(marks['FX-DUP'], 'On 2 lines: see Other lines')
        self.assertIsNone(marks['FX-PANEL'])

    def test_required_marks_mandatory_items_and_dw1_minimum_content(self):
        required = {code: row[6] for code, row in self._by_code(self.ws).items()}
        self.assertEqual(required['FX-MAND'], 'Required')
        self.assertEqual(required['FX-INV1'], 'At least one inverter')
        self.assertEqual(required['FX-INV2'], 'At least one inverter')
        self.assertEqual(required['FX-DCDB'], 'At least one DCDB, ACDB or ACCB')
        self.assertEqual(required['FX-ACDB'], 'At least one DCDB, ACDB or ACCB')
        self.assertIsNone(required['FX-PANEL'])

    def test_calculated_cells_are_grey_and_locked_the_rest_unlocked(self):
        excel_row = {row[0].value: row[0].row for row in self.ws.iter_rows(min_row=2)}
        for code in ('FX-EARTH', 'FX-LOG', 'FX-RS485', 'FX-DUP'):
            cell = self.ws.cell(row=excel_row[code], column=5)
            self.assertTrue(cell.protection.locked, code)
            self.assertEqual(cell.fill.fgColor.rgb, GREY, code)
        for code in ('FX-PANEL', 'FX-DCDB', 'FX-INV2'):
            cell = self.ws.cell(row=excel_row[code], column=5)
            self.assertFalse(cell.protection.locked, code)
            self.assertNotEqual(cell.fill.fgColor.rgb, GREY, code)
        self.assertTrue(self.ws.cell(row=excel_row['FX-PANEL'], column=1).protection.locked)

    def test_the_accb_sheet_keeps_todays_columns(self):
        rows = self._rows(self.wb[BOQ_DOWNLOAD_ACCB_SHEET], min_row=4)
        self.assertEqual(rows[0], ('ACCB', 'Code', 'Description', 'Unit', 'Quantity'))
        self.assertEqual([(r[0], r[1], r[4]) for r in rows[1:]], [(1, 'OPX-273', 1)])

    def test_the_other_lines_sheet_holds_what_the_boq_sheet_cannot(self):
        rows = self._rows(self.wb[BOQ_DOWNLOAD_OFF_SHEET], min_row=4)
        self.assertEqual(list(rows[0]),
                         ['Code', 'Category', 'Description', 'Unit', 'Quantity', 'Why'])
        self.assertEqual([(r[0], r[4], r[5]) for r in rows[1:]], [
            ('FX-DUP', 2, 'Item on 2 lines'),
            ('FX-DUP', 3, 'Item on 2 lines'),
            ('FX-OLD', 3, 'Retired catalogue item'),
            (None, 4, 'No catalogue item'),
        ])

    def test_the_instructions_name_the_site_and_the_rules(self):
        text = ' '.join(str(c) for row in self._rows(self.wb[BOQ_DOWNLOAD_INFO_SHEET], 1)
                        for c in row if c)
        self.assertIn('BOQ — FX-A', text)
        self.assertIn('Grey rows are calculated', text)
        self.assertIn('A blank row adds nothing', text)
        self.assertIn('Do not rename the "BOQ" sheet', text)

    def test_a_site_with_no_boq_downloads_the_full_catalogue_and_creates_nothing(self):
        site, _assignment = self._opex_site('FX-EMPTY')
        wb = self._book(self._download(site=site).content)
        self.assertEqual([r[0] for r in self._rows(wb[BOQ_DOWNLOAD_SHEET])],
                         self._catalogue_codes())
        self.assertEqual(wb.sheetnames, [BOQ_DOWNLOAD_INFO_SHEET, BOQ_DOWNLOAD_SHEET])
        self.assertFalse(BOQ.objects.filter(project=site).exists())


# ═════════════════════════════════════════════════════════════════════════════
# The blank master
# ═════════════════════════════════════════════════════════════════════════════

class BlankMasterTests(MasterSheetBase):

    def test_same_rows_and_columns_as_the_site_file_with_no_quantities(self):
        response = self._master()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response['Content-Disposition'],
            f'attachment; filename="Master_BOQ_blank_{timezone.localdate().isoformat()}.xlsx"')
        wb = self._book(response.content)
        self.assertEqual(wb.sheetnames, [BOQ_DOWNLOAD_INFO_SHEET, BOQ_DOWNLOAD_SHEET])
        ws = wb[BOQ_DOWNLOAD_SHEET]
        site_ws = self._book(self._download().content)[BOQ_DOWNLOAD_SHEET]

        self.assertEqual([c.value for c in ws[1]], HEADERS)
        rows = self._rows(ws)
        self.assertEqual([r[:4] for r in rows], [r[:4] for r in self._rows(site_ws)])
        self.assertEqual({r[4] for r in rows}, {None})
        marks = {r[0]: r[5] for r in rows}
        # Calculated items are still marked; no override note, no two-line mark (no site).
        self.assertEqual((marks['FX-EARTH'], marks['FX-LOG'], marks['FX-DUP']),
                         ('Calculated', 'Calculated', None))
        self.assertEqual({r[0]: r[6] for r in rows}['FX-MAND'], 'Required')

    def test_filled_and_uploaded_to_a_site_the_preview_adds_exactly_the_filled_rows(self):
        site, _assignment = self._opex_site('FX-B')
        wb = self._book(self._master().content)
        ws = wb[BOQ_DOWNLOAD_SHEET]
        filled = {'FX-MAND': 1, 'FX-PANEL': 7, 'FX-DCDB': 2}
        for row in ws.iter_rows(min_row=2):
            if row[0].value in filled:
                row[4].value = filled[row[0].value]

        preview = self._preview(self._bytes(wb), site=site)

        summary = preview.context['upload_summary']
        self.assertEqual(sorted(summary['added']), sorted(filled))
        self.assertEqual((summary['changed'], summary['readded'], summary['recalculated']),
                         ([], [], []))
        self._commit(preview, site=site)
        self.assertEqual({code: self._qty(code, site=site) for code in filled},
                         {code: D(q) for code, q in filled.items()})
        self.assertEqual(BOQItem.objects.filter(boq__project=site).count(), 3)


# ═════════════════════════════════════════════════════════════════════════════
# The upload
# ═════════════════════════════════════════════════════════════════════════════

class UploadTests(MasterSheetBase):

    def test_an_old_order_file_uploads_as_before(self):
        wb = Workbook()
        ws = wb.active
        ws.title = 'BOQ'
        ws.append(['Code', 'Description', 'Unit', 'Category', 'Quantity'])
        ws.append(['FX-PANEL', 'x', 'x', 'x', 12])
        ws.append(['FX-INV2', 'x', 'x', 'x', 3])
        ws.append(['FX-DCDB', 'x', 'x', 'x', None])

        preview = self._preview(self._bytes(wb))
        summary = preview.context['upload_summary']
        self.assertEqual(summary['added'], ['FX-INV2'])
        self.assertEqual(summary['changed'], [('FX-PANEL', D(10), D(12))])
        self._commit(preview)
        self.assertEqual((self._qty('FX-PANEL'), self._qty('FX-INV2'), self._qty('FX-DCDB')),
                         (D(12), D(3), None))

    def test_a_figure_typed_into_a_calculated_row_is_ignored_and_said(self):
        wb = self._book(self._download().content)
        ws = wb[BOQ_DOWNLOAD_SHEET]
        ws.protection.sheet = False                       # "unprotect the sheet first"
        for row in ws.iter_rows(min_row=2):
            if row[0].value == 'FX-EARTH':
                row[4].value = 99

        preview = self._preview(self._bytes(wb))

        self.assertEqual(preview.context['upload_summary']['replaced'],
                         [{'code': 'FX-EARTH', 'file': D(99), 'calculated': D(10)}])
        self.assertTrue(preview.context['no_changes'])
        self.assertContains(preview, 'Calculated: will be replaced by 10')
        self._commit(preview)
        self.assertEqual(self._qty('FX-EARTH'), D(10))

    def test_a_stale_site_shows_only_the_catch_up_list(self):
        """Answer 2: a calculated figure equal to the stored one is "as downloaded"."""
        BOQItem.objects.filter(pk=self._lines('FX-EARTH')[0].pk).update(boq_quantity=D(7))

        preview = self._preview(self._download().content)

        summary = preview.context['upload_summary']
        self.assertEqual(summary['recalculated'], [('FX-EARTH', D(7), D(10))])
        self.assertEqual((summary['added'], summary['changed'], summary['readded'],
                          summary['replaced'], summary['kept_override'], summary['removed']),
                         ([], [], [], [], [], []))
        self.assertFalse(preview.context['no_changes'])
        self.assertContains(preview, '1 calculated line brought up to date by their rules')
        self.assertContains(preview, 'Confirm and apply')

    def test_a_figure_typed_over_a_stale_calculated_line_is_not_changes_nothing(self):
        """DW-7b: the summary lists the stale line only as replaced, but the engine still
        writes it, so the preview offers Confirm and the commit brings it up to date."""
        BOQItem.objects.filter(pk=self._lines('FX-EARTH')[0].pk).update(boq_quantity=D(7))
        wb = self._book(self._download().content)
        for row in wb[BOQ_DOWNLOAD_SHEET].iter_rows(min_row=2):
            if row[0].value == 'FX-EARTH':
                row[4].value = 99

        preview = self._preview(self._bytes(wb))

        summary = preview.context['upload_summary']
        self.assertEqual((summary['replaced'], summary['recalculated']),
                         ([{'code': 'FX-EARTH', 'file': D(99), 'calculated': D(10)}], []))
        self.assertFalse(preview.context['no_changes'])
        self.assertEqual(self._commit(preview).context['stage'], 'result')
        self.assertEqual(self._qty('FX-EARTH'), D(10))

    def test_changes_in_the_downloaded_file_are_exactly_what_the_preview_shows(self):
        """Zuber's check 3: fill three blank rows and change one typed quantity."""
        wb = self._book(self._download().content)
        new = {'FX-INV2': 5, 'FX-LA2': 1, 'FX-DCDB': 2, 'FX-PANEL': 11}
        for row in wb[BOQ_DOWNLOAD_SHEET].iter_rows(min_row=2):
            if row[0].value in new:
                row[4].value = new[row[0].value]

        summary = self._preview(self._bytes(wb)).context['upload_summary']

        self.assertEqual(summary['added'], ['FX-LA2', 'FX-INV2'])
        self.assertEqual(sorted(summary['changed']),
                         [('FX-DCDB', None, D(2)), ('FX-PANEL', D(10), D(11))])

    def test_a_figure_for_an_item_on_two_lines_is_not_written(self):
        wb = self._book(self._download().content)
        ws = wb[BOQ_DOWNLOAD_SHEET]
        for row in ws.iter_rows(min_row=2):
            if row[0].value == 'FX-DUP':
                row[4].value = 9

        preview = self._preview(self._bytes(wb))

        self.assertEqual(preview.context['upload_summary']['on_lines'], [('FX-DUP', D(9), 2)])
        self.assertContains(preview, 'this BOQ has it on 2 lines')
        self._commit(preview)
        self.assertEqual([line.boq_quantity for line in self._lines('FX-DUP')], [D(2), D(3)])

    def test_a_blank_mandatory_row_with_no_line_is_put_back(self):
        BOQItem.objects.filter(boq__project=self.site, item_master__code='FX-MAND').delete()

        summary = self._preview(self._download().content).context['upload_summary']

        self.assertEqual((summary['added'], summary['readded']), ([], ['FX-MAND']))

    def test_the_sheet_name_matches_ignoring_case_and_spaces(self):
        wb = Workbook()
        ws = wb.active
        ws.title = ' boq '
        ws.append(['Code', 'Quantity'])
        ws.append(['FX-PANEL', 12])
        preview = self._preview(self._bytes(wb))
        self.assertEqual(preview.context['upload_summary']['changed'],
                         [('FX-PANEL', D(10), D(12))])


class RefusalMessageTests(MasterSheetBase):
    """L7: every refusal reaches the page with its reason."""

    def test_the_team_template_names_the_sheets_found(self):
        wb = Workbook()
        wb.active.title = 'BOM  (2)'
        wb.active.append(['S.No', 'Description', 'Qty'])
        wb.create_sheet('Sheet1')

        preview = self._preview(self._bytes(wb))

        expected = ("This file has no sheet named BOQ (found: 'BOM  (2)', 'Sheet1'). "
                    "Download this site's BOQ from the picker, fill the Quantity column, and "
                    "upload that file.")
        self.assertEqual(preview.context['file_error'], expected)
        self.assertContains(preview, "found: &#x27;BOM  (2)&#x27;, &#x27;Sheet1&#x27;")

    def test_a_boq_sheet_without_a_code_column(self):
        wb = Workbook()
        wb.active.title = 'BOQ'
        wb.active.append(['S.No', 'Description', 'Unit', 'Quantity'])
        wb.active.append([1, 'Module', 'Nos', 20])

        preview = self._preview(self._bytes(wb))

        self.assertEqual(preview.context['file_error'],
                         "This isn't the system's BOQ sheet: it has no Code column. "
                         + BOQ_UPLOAD_USE_THE_DOWNLOAD)
        self.assertEqual(self._qty('FX-PANEL'), D(10))


# ═════════════════════════════════════════════════════════════════════════════
# Gates (L9)
# ═════════════════════════════════════════════════════════════════════════════

class GateTests(MasterSheetBase):

    def test_the_blank_master_is_for_design_users(self):
        for profile, status in ((self.designer, 200), (self.qc, 200), (self.head, 200),
                                (self.scm, 403), (self.pm, 403)):
            with self.subTest(role=profile.user.username):
                self.assertEqual(self._master(profile).status_code, status)

    def test_the_picker_button_is_shown_only_to_design_users(self):
        url = reverse('opex_boq_entry', args=[self.site.project_id])
        for profile, shown in ((self.designer, True), (self.qc, True), (self.head, True),
                               (self.scm, False), (self.pm, False)):
            with self.subTest(role=profile.user.username):
                response = _client(profile).get(url)
                self.assertEqual(response.status_code, 200)
                self.assertEqual('js-boq-master-download' in response.content.decode(), shown)

    def test_the_catalogue_button_is_shown_to_the_head(self):
        response = _client(self.head).get(reverse('design_boq_catalogue'))
        self.assertContains(response, 'js-boq-master-download')
        self.assertContains(response, reverse('opex_boq_master_download'))

    def test_the_site_download_keeps_the_read_gate(self):
        for profile in (self.designer, self.qc, self.head, self.scm, self.pm):
            with self.subTest(role=profile.user.username):
                self._download(profile)


# ═════════════════════════════════════════════════════════════════════════════
# Other downloads (P6: no change) and query counts (P9)
# ═════════════════════════════════════════════════════════════════════════════

class OtherDownloadTests(MasterSheetBase):

    def test_the_design_group_excel_columns_are_unchanged(self):
        """P6: the group Excels carry no Category column and already run Code, Description,
        Unit in the ruled order, so DW-7 changes none of them (snapshot data and fingerprint
        are untouched by construction; tests_design_boq_group_snapshots still pins them)."""
        self.assertEqual(SUMMARY_HEADERS, ['Code', 'Description', 'Unit', 'Sites', 'Total',
                                           'Note'])
        self.assertEqual(UNLINKED_HEADERS, ['Site', 'Description', 'Unit', 'Quantity'])


class QueryCountTests(MasterSheetBase):

    def _measure(self):
        designer = _client(self.designer)
        site_url = reverse('opex_boq_download', args=[self.site.project_id])
        content = designer.get(site_url).content
        upload_url = reverse('opex_boq_upload', args=[self.site.project_id])

        def preview():
            return designer.post(upload_url, {'phase': 'preview',
                                              'file': SimpleUploadedFile('b.xlsx', content)})
        rows_json = preview().context['rows_json']
        return {
            'site download (designer)': self._count(lambda: designer.get(site_url)),
            'blank master (designer)': self._count(
                lambda: designer.get(reverse('opex_boq_master_download'))),
            'upload preview, round trip (designer)': self._count(preview),
            'upload confirm, round trip (designer)': self._count(
                lambda: designer.post(upload_url, {'phase': 'commit', 'rows_json': rows_json})),
        }

    def test_query_counts_are_pinned(self):
        self.assertEqual(self._measure(), PINS)

    def test_no_count_grows_with_the_catalogue(self):
        BOQItemMaster.objects.bulk_create([
            BOQItemMaster(code=f'FX-Z{n:02d}', description=f'Extra {n}', unit='Nos',
                          category='BOS', project_type='OPEX', sort_order=9100 + n)
            for n in range(30)])
        self.assertEqual(self._measure(), PINS)
