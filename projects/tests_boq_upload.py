"""
Session E2 verification — reading a filled BOQ spreadsheet back onto a site.

WHY THIS FILE EXISTS
--------------------
The upload inverts the one assumption the picker is built on. `opex_boq_entry`'s POST is
a full reconciliation: absence from the post means the designer removed the row, and that
is sound because the browser sends every row the sheet renders. A spreadsheet carries no
such guarantee — a designer who filters, sorts, trims a block or sends a partial file has
asked for nothing to be deleted. So the upload has NO delete path at all, and the test
that matters most in this file is the one that deletes rows from the file and asserts the
BOQ still has them (decision 4).

Three other things here can fail silently and are asserted from both directions:

  * a rejected file must write NOTHING. Every rejection test checks the BOQ afterwards,
    not just the response — a message on screen is not evidence that the database was
    left alone.

  * blank and zero must stay distinguishable. Blank leaves the existing quantity, `0`
    sets zero. Getting this backwards would let a half-filled sheet wipe a BOQ, and it
    would look like a successful import.

  * the file's specification columns must be ignored. Sheet protection is an affordance,
    not a boundary, so the test edits a description to something wrong and asserts the
    saved row carries the catalogue's text.

RUNNER: this module seeds the OPEX catalogue in setUp, so like tests_design_part11 it can
only run with migrations disabled —
`python manage.py test projects --settings=solarpms.test_settings`. Migrations 0047/0057
seed the same codes into a unique column, so a migrated database collides on the first row.
"""
import json
from decimal import Decimal
from io import BytesIO

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import (
    Program, Project, UserProfile, BOQ, BOQItem, BOQItemMaster,
    DesignAssignment, DesignAttempt, SiteGroup, SiteGroupMembership,
    SITE_GROUP_DRAFT, SITE_GROUP_LOCKED,
    DESIGN_IN_DESIGN, ATTEMPT_REASON_INITIAL,
)
from .views import BOQ_DOWNLOAD_COLUMNS, BOQ_DOWNLOAD_SHEET, BOQ_DOWNLOAD_OFF_SHEET

#: A small stand-in catalogue. The real one is 207 rows; nothing here depends on the
#: count, only on codes being resolvable and on OPX-001 carrying the mandatory flag.
#:
#: OPX-004's unit is deliberately `KWp` — a value that is NOT in BOQItem.UOM_CHOICES,
#: which 52 of the 185 live OPEX rows also carry. If the upload ever routes a write
#: through a ModelForm or calls full_clean(), this row is what catches it.
CATALOGUE = [
    # (code, description, unit, category, sort_order, is_mandatory, is_active)
    ('OPX-001', 'Solar PV Module',        'Nos',   'Module',   1,  True,  True),
    ('OPX-002', 'DCDB 5 In 5 Out',        'Nos',   'DCDB',     2,  False, True),
    ('OPX-003', 'AC Cable 4 sqmm',        'Meter', 'Cable',    3,  False, True),
    ('OPX-004', 'Ballast Type Structure', 'KWp',   'MMS',      4,  False, True),
    ('OPX-005', 'Earthing Electrode',     'Nos',   'Earthing', 5,  False, True),
    ('OPX-099', 'Withdrawn Item',         'Nos',   'Module',   99, False, False),
]


def _profile(username, role, is_design_qc=False):
    """A post_save signal auto-creates the UserProfile; fetch and set, never create."""
    user = User.objects.create_user(username=username, password='x')
    profile = user.profile
    profile.role = role
    profile.is_active = True
    profile.is_design_qc = is_design_qc
    profile.save()
    return profile


class UploadBase(TestCase):

    def setUp(self):
        BOQItemMaster.objects.bulk_create([
            BOQItemMaster(code=code, description=description, unit=unit, category=category,
                          project_type='OPEX', sort_order=sort_order,
                          is_mandatory=is_mandatory, is_active=is_active)
            for code, description, unit, category, sort_order, is_mandatory, is_active
            in CATALOGUE
        ])

        self.designer = _profile('desE2',  'Design')
        self.other    = _profile('desE2b', 'Design')
        self.qc       = _profile('qcE2',   'Design', is_design_qc=True)
        self.pm       = _profile('pmE2',   'PM')

        self.program = Program.objects.create(
            name='Test-E2', program_type='OPEX', client_name='E2Client',
            status='Active', short_tender_code='E2')

        self.site, self.assignment = self._opex_site('E2-A')

    # ── fixtures ────────────────────────────────────────────────────────────

    def _opex_site(self, code, designer=None):
        site = Project(
            project_id=code, customer_name='E2Client', customer_phone='9876543210',
            site_address='1 Sun Rd', city='Delhi', project_type='OPEX',
            program=self.program, site_code=code,
            capacity_kw=Decimal('100.00'), status='Draft',
            assigned_design=designer or self.designer, assigned_pm=self.pm)
        site.save()
        assignment = DesignAssignment.objects.create(
            project=site, status=DESIGN_IN_DESIGN,
            assigned_to=designer or self.designer,
            survey_file_bucket='b', survey_file_path=f'{code}/survey/x.pdf')
        return site, assignment

    def _master(self, code):
        return BOQItemMaster.objects.get(code=code)

    def _seed_boq(self, codes_and_quantities, site=None):
        """Put rows on the BOQ directly — the picker's own field mapping, so a seeded
        row is indistinguishable from one the picker wrote."""
        project = site or self.site
        boq = BOQ.objects.create(project=project)
        for code, quantity in codes_and_quantities:
            master = self._master(code)
            BOQItem.objects.create(
                boq=boq, item_master=master, serial_no=master.sort_order,
                category=master.category, description=master.description,
                uom=master.unit,
                boq_quantity=None if quantity is None else Decimal(str(quantity)),
                is_standard_item=True)
        return boq

    def _file(self, rows, sheet_name=BOQ_DOWNLOAD_SHEET, headers=None, off_rows=None):
        """Build an .xlsx in memory the way the download writes one."""
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.title = sheet_name
        ws.append(headers if headers is not None
                  else [head for head, _key in BOQ_DOWNLOAD_COLUMNS])
        for row in rows:
            ws.append(row)
        if off_rows is not None:
            off = wb.create_sheet(BOQ_DOWNLOAD_OFF_SHEET)
            off.append(['These rows are on this BOQ but are NOT in the OPEX catalogue.'])
            off.append([])
            off.append(['Description', 'Unit', 'Category', 'Quantity'])
            for row in off_rows:
                off.append(row)
        buf = BytesIO()
        wb.save(buf)
        buf.seek(0)
        return SimpleUploadedFile(
            'boq.xlsx', buf.read(),
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')

    def _row(self, code, quantity, description=None, unit=None, category=None):
        """One data-sheet row in BOQ_DOWNLOAD_COLUMNS order."""
        master = BOQItemMaster.objects.filter(code=code).first()
        return [code,
                description if description is not None else (master.description if master else ''),
                unit if unit is not None else (master.unit if master else ''),
                category if category is not None else (master.category if master else ''),
                quantity]

    def _login(self, profile):
        self.assertTrue(self.client.login(username=profile.user.username, password='x'))

    def _url(self, site=None):
        return reverse('opex_boq_upload',
                       kwargs={'project_id': (site or self.site).project_id})

    def _preview(self, rows, site=None, **kwargs):
        return self.client.post(self._url(site),
                                {'phase': 'preview', 'file': self._file(rows, **kwargs)})

    def _commit(self, response, site=None):
        """Confirm exactly what the preview handed back."""
        return self.client.post(self._url(site),
                                {'phase': 'commit',
                                 'rows_json': response.context['rows_json']})

    def _upload(self, rows, site=None, **kwargs):
        """Preview then confirm — the whole round trip."""
        preview = self._preview(rows, site, **kwargs)
        self.assertEqual(preview.context['stage'], 'preview',
                         f"preview was rejected: {preview.context.get('errors')} "
                         f"{preview.context.get('file_error')}")
        return self._commit(preview, site)

    def _quantities(self, site=None):
        return {row.item_master.code: row.boq_quantity
                for row in BOQItem.objects.filter(boq__project=site or self.site)
                if row.item_master_id}

    def _design_lock(self):
        """Stamp the current attempt's boq_submitted_at — the whole design-lock condition."""
        attempt = DesignAttempt.objects.create(
            assignment=self.assignment, attempt_number=1,
            opened_reason=ATTEMPT_REASON_INITIAL, boq_submitted_at=timezone.now())
        self.assignment.current_attempt_number = 1
        self.assignment.save()
        return attempt

    def _group_lock(self):
        group = SiteGroup.objects.create(program=self.program, name='G1',
                                         status=SITE_GROUP_LOCKED, created_by=self.pm)
        SiteGroupMembership.objects.create(group=group, project=self.site,
                                           added_by=self.pm)
        return group


# ===========================================================================
# Decision 4 — THE UPLOAD NEVER DELETES. The most important tests in this file.
# ===========================================================================

class NeverDeletesTests(UploadBase):

    def test_rows_absent_from_the_file_are_left_alone(self):
        """DECISION 4. The picker reads absence as removal; this must not."""
        self._seed_boq([('OPX-001', 5), ('OPX-002', 10), ('OPX-003', 15)])
        self._login(self.designer)

        # A file naming ONE of the three rows.
        self._upload([self._row('OPX-002', 99)])

        self.assertEqual(self._quantities(),
                         {'OPX-001': Decimal('5.00'),
                          'OPX-002': Decimal('99.00'),
                          'OPX-003': Decimal('15.00')},
                         'a row missing from the file was changed or deleted')

    def test_an_empty_looking_file_removes_nothing(self):
        """The pathological case: the designer deleted every row but one."""
        self._seed_boq([('OPX-001', 5), ('OPX-002', 10), ('OPX-003', 15),
                        ('OPX-004', 20), ('OPX-005', 25)])
        self._login(self.designer)

        self._upload([self._row('OPX-001', 5)])

        self.assertEqual(BOQItem.objects.filter(boq__project=self.site).count(), 5)

    def test_the_preview_always_reports_zero_removed(self):
        """Someone who deleted rows in Excel has to be told why nothing happened."""
        self._seed_boq([('OPX-001', 5), ('OPX-002', 10)])
        self._login(self.designer)

        response = self._preview([self._row('OPX-001', 5)])

        self.assertEqual(response.context['stage'], 'preview')
        self.assertContains(response, 'Rows removed')
        self.assertContains(response, 'never removed by an upload')

    def test_no_delete_survives_the_commit_either(self):
        """Belt and braces: assert on the rows, not on the preview's promise."""
        self._seed_boq([('OPX-001', 5), ('OPX-002', 10), ('OPX-003', 15)])
        before = set(BOQItem.objects.filter(boq__project=self.site)
                     .values_list('pk', flat=True))
        self._login(self.designer)

        self._upload([self._row('OPX-004', 1)])

        after = set(BOQItem.objects.filter(boq__project=self.site)
                    .values_list('pk', flat=True))
        self.assertTrue(before.issubset(after), 'an existing BOQItem row was deleted')


# ===========================================================================
# Decisions 2 and 3 — matching on code, and whole-file rejection
# ===========================================================================

class CodeMatchingTests(UploadBase):

    def test_an_unknown_code_rejects_the_whole_file(self):
        """DECISION 3. Nothing is written, and the BOQ is checked — not just the page."""
        self._seed_boq([('OPX-001', 5)])
        self._login(self.designer)

        response = self._preview([self._row('OPX-001', 77),
                                  ['NOPE-1', 'Made up', 'Nos', 'Module', 3]])

        self.assertEqual(response.context['stage'], 'errors')
        self.assertEqual(self._quantities(), {'OPX-001': Decimal('5.00')},
                         'a rejected file still changed the BOQ')

    def test_every_offending_row_is_named_not_just_the_first(self):
        """DECISION 3 — three bad codes, three errors, each with its own row number."""
        self._login(self.designer)

        response = self._preview([
            self._row('OPX-001', 1),
            ['BAD-A', 'x', 'Nos', 'Module', 1],
            ['BAD-B', 'x', 'Nos', 'Module', 2],
            ['BAD-C', 'x', 'Nos', 'Module', 3],
        ])

        errors = response.context['errors']
        self.assertEqual(len(errors), 3, errors)
        for code, row_number in (('BAD-A', 3), ('BAD-B', 4), ('BAD-C', 5)):
            self.assertTrue(any(code in e and f'Row {row_number}' in e for e in errors),
                            f'{code} on row {row_number} was not reported: {errors}')

    def test_an_inactive_code_is_rejected_like_an_unknown_one(self):
        """DECISION 2 — one membership test covers real, OPEX, and still active."""
        self._login(self.designer)

        response = self._preview([self._row('OPX-099', 4)])

        self.assertEqual(response.context['stage'], 'errors')
        self.assertTrue(any('OPX-099' in e for e in response.context['errors']))

    def test_a_duplicate_code_in_the_file_is_rejected(self):
        self._login(self.designer)

        response = self._preview([self._row('OPX-001', 1), self._row('OPX-001', 2)])

        self.assertEqual(response.context['stage'], 'errors')
        self.assertTrue(any('already appears on row 2' in e
                            for e in response.context['errors']),
                        response.context['errors'])

    def test_a_valid_code_not_yet_on_the_boq_is_added(self):
        self._seed_boq([('OPX-001', 5)])
        self._login(self.designer)

        self._upload([self._row('OPX-003', 12)])

        self.assertEqual(self._quantities()['OPX-003'], Decimal('12.00'))


# ===========================================================================
# Decision 5 — blank leaves alone, zero sets zero
# ===========================================================================

class BlankVersusZeroTests(UploadBase):

    def test_a_blank_quantity_leaves_the_existing_value(self):
        """DECISION 5, and the inversion of _quantity()'s empty-means-cleared rule."""
        self._seed_boq([('OPX-001', 42)])
        self._login(self.designer)

        self._upload([self._row('OPX-001', None)])

        self.assertEqual(self._quantities()['OPX-001'], Decimal('42.00'))

    def test_an_explicit_zero_sets_zero(self):
        self._seed_boq([('OPX-001', 42)])
        self._login(self.designer)

        self._upload([self._row('OPX-001', 0)])

        self.assertEqual(self._quantities()['OPX-001'], Decimal('0.00'))

    def test_a_new_row_with_a_blank_quantity_is_created_null(self):
        self._login(self.designer)

        self._upload([self._row('OPX-003', None)])

        self.assertIsNone(self._quantities()['OPX-003'])

    def test_a_negative_quantity_is_rejected(self):
        self._seed_boq([('OPX-001', 5)])
        self._login(self.designer)

        response = self._preview([self._row('OPX-001', -3)])

        self.assertEqual(response.context['stage'], 'errors')
        self.assertEqual(self._quantities()['OPX-001'], Decimal('5.00'))

    def test_unparseable_text_is_rejected_rather_than_silently_blanked(self):
        """The picker forgives this and reads None. In a file that is data loss."""
        self._seed_boq([('OPX-001', 5)])
        self._login(self.designer)

        response = self._preview([self._row('OPX-001', '1,200')])

        self.assertEqual(response.context['stage'], 'errors')
        self.assertTrue(any('1,200' in e for e in response.context['errors']))
        self.assertEqual(self._quantities()['OPX-001'], Decimal('5.00'))

    def test_scientific_notation_is_accepted(self):
        """Decimal parses it correctly, so it is not an error."""
        self._login(self.designer)

        self._upload([self._row('OPX-001', '1.2E+3')])

        self.assertEqual(self._quantities()['OPX-001'], Decimal('1200.00'))

    def test_an_oversized_quantity_never_reaches_the_database(self):
        """Audit §U1. numeric(10,2) tops out at 99999999.99; without this check the
        INSERT raises DataError and takes the whole transaction down."""
        self._login(self.designer)

        response = self._preview([self._row('OPX-001', '1E+21')])

        self.assertEqual(response.context['stage'], 'errors')
        self.assertFalse(BOQItem.objects.filter(boq__project=self.site).exists())


# ===========================================================================
# Decision 6 — mandatory items, and Decision 7 — the spec columns are ignored
# ===========================================================================

class MandatoryAndSpecTests(UploadBase):

    def test_a_missing_mandatory_item_is_put_back_and_named(self):
        """DECISION 6 — the same get_opex_mandatory_items() set the picker unions."""
        self._login(self.designer)

        response = self._upload([self._row('OPX-003', 7)])

        self.assertIn('OPX-001', self._quantities())
        self.assertIsNone(self._quantities()['OPX-001'])
        self.assertContains(response, 'OPX-001')

    def test_a_mandatory_row_already_on_the_boq_is_not_reported_as_put_back(self):
        """Absence means nothing here, so a row already present was never restored."""
        self._seed_boq([('OPX-001', 5)])
        self._login(self.designer)

        response = self._preview([self._row('OPX-003', 7)])

        self.assertEqual(list(response.context['plan_readded']), [])

    def test_the_file_cannot_change_a_description(self):
        """DECISION 7. Sheet protection is an affordance; the server re-derives."""
        self._login(self.designer)

        self._upload([self._row('OPX-003', 4, description='TOTALLY WRONG',
                                unit='XX', category='Nonsense')])

        row = BOQItem.objects.get(boq__project=self.site, item_master__code='OPX-003')
        self.assertEqual(row.description, 'AC Cable 4 sqmm')
        self.assertEqual(row.uom, 'Meter')
        self.assertEqual(row.category, 'Cable')

    def test_a_unit_outside_uom_choices_survives(self):
        """OPX-004 is KWp, which is not in BOQItem.UOM_CHOICES — as 52 live rows are
        not. A ModelForm or a full_clean() on this path would drop them."""
        self._login(self.designer)

        self._upload([self._row('OPX-004', 3)])

        row = BOQItem.objects.get(boq__project=self.site, item_master__code='OPX-004')
        self.assertEqual(row.uom, 'KWp')


# ===========================================================================
# Decision 8 — both locks refuse, with distinct messages
# ===========================================================================

class LockTests(UploadBase):

    def test_the_design_lock_refuses_and_points_at_the_route_out(self):
        self._seed_boq([('OPX-001', 5)])
        self._design_lock()
        self._login(self.designer)

        response = self.client.get(self._url())

        self.assertEqual(response.context['stage'], 'locked')
        self.assertIn('sends it back', response.context['lock_reason'])

    def test_the_group_lock_refuses_and_says_it_is_final(self):
        self._seed_boq([('OPX-001', 5)])
        self._group_lock()
        self._login(self.designer)

        response = self.client.get(self._url())

        self.assertEqual(response.context['stage'], 'locked')
        self.assertIn('no unlock', response.context['lock_reason'])

    def test_the_two_lock_messages_differ(self):
        self._login(self.designer)
        self._design_lock()
        design_reason = self.client.get(self._url()).context['lock_reason']
        self._group_lock()
        group_reason = self.client.get(self._url()).context['lock_reason']

        self.assertNotEqual(design_reason, group_reason)

    def test_a_crafted_commit_post_cannot_get_past_a_lock(self):
        """The locks are checked BEFORE the phase dispatch, so this reaches nothing."""
        self._seed_boq([('OPX-001', 5)])
        self._design_lock()
        self._login(self.designer)

        payload = json.dumps([{'code': 'OPX-001', 'quantity': '999'}])
        response = self.client.post(self._url(),
                                    {'phase': 'commit', 'rows_json': payload})

        self.assertEqual(response.context['stage'], 'locked')
        self.assertEqual(self._quantities()['OPX-001'], Decimal('5.00'))

    def test_a_crafted_preview_post_cannot_get_past_a_lock(self):
        self._seed_boq([('OPX-001', 5)])
        self._group_lock()
        self._login(self.designer)

        response = self.client.post(
            self._url(), {'phase': 'preview', 'file': self._file([self._row('OPX-001', 9)])})

        self.assertEqual(response.context['stage'], 'locked')
        self.assertEqual(self._quantities()['OPX-001'], Decimal('5.00'))


# ===========================================================================
# Decision 9 — the write gate, and the OPEX-only gate
# ===========================================================================

class GateTests(UploadBase):

    def test_only_the_assigned_designer_may_upload(self):
        self._login(self.other)
        self.assertEqual(self.client.get(self._url()).status_code, 403)

    def test_a_qc_reviewer_who_may_download_may_not_upload(self):
        """The read gate admits Design QC portfolio-wide; the write gate does not."""
        self._login(self.qc)

        self.assertEqual(self.client.get(self._url()).status_code, 403)
        self.assertEqual(
            self.client.get(reverse('opex_boq_download',
                                    kwargs={'project_id': self.site.project_id})).status_code,
            200)

    def test_the_pm_may_not_upload(self):
        self._login(self.pm)
        self.assertEqual(self.client.get(self._url()).status_code, 403)

    def test_a_residential_project_404s(self):
        site = Project(project_id='E2-RES', customer_name='House',
                       customer_phone='9876543211', site_address='2 Sun Rd', city='Delhi',
                       project_type='Residential', capacity_kw=Decimal('10.00'),
                       status='Draft', assigned_design=self.designer, assigned_pm=self.pm)
        site.save()
        self._login(self.designer)

        response = self.client.get(
            reverse('opex_boq_upload', kwargs={'project_id': 'E2-RES'}))

        self.assertEqual(response.status_code, 404)


# ===========================================================================
# The file itself — sheets, shape, limits, and the confirm-stage payload
# ===========================================================================

class FileHandlingTests(UploadBase):

    def test_the_off_catalogue_sheet_is_ignored_entirely(self):
        """This is why that sheet exists. Its rows would otherwise be unknown codes and
        would reject the whole file — the download would produce a file its own upload
        refuses. Mirrors TESTTENDER26-MB010, whose 37 rows are all off-catalogue."""
        self._login(self.designer)

        response = self._preview(
            [self._row('OPX-001', 5)],
            off_rows=[['595Wp Solar modules DCR', 'Nos', 'Solar Modules', 2],
                      ['Module Transport', 'Nos', 'Solar Modules', 1]])

        self.assertEqual(response.context['stage'], 'preview',
                         response.context.get('errors'))

    def test_a_missing_data_sheet_is_rejected_by_name(self):
        self._login(self.designer)

        response = self._preview([self._row('OPX-001', 5)], sheet_name='Something Else')

        self.assertEqual(response.context['stage'], 'upload')
        self.assertIn(BOQ_DOWNLOAD_SHEET, response.context['file_error'])

    def test_an_unrecognised_extra_column_is_tolerated(self):
        """A stale download carrying the old Mandatory column must still import."""
        self._login(self.designer)

        headers = ['Code', 'Description', 'Unit', 'Category', 'Mandatory', 'Quantity']
        response = self._preview(
            [['OPX-001', 'Solar PV Module', 'Nos', 'Module', 'Yes', 6]], headers=headers)

        self.assertEqual(response.context['stage'], 'preview',
                         response.context.get('errors'))

    def test_a_file_without_a_quantity_column_is_rejected(self):
        self._login(self.designer)

        response = self._preview([['OPX-001', 'Solar PV Module', 'Nos', 'Module']],
                                 headers=['Code', 'Description', 'Unit', 'Category'])

        self.assertEqual(response.context['stage'], 'upload')
        self.assertIn('Quantity', response.context['file_error'])

    def test_a_file_with_no_data_rows_is_rejected_not_treated_as_a_no_op(self):
        self._login(self.designer)

        response = self._preview([])

        self.assertEqual(response.context['stage'], 'upload')
        self.assertIn('no rows', response.context['file_error'])

    def test_a_non_xlsx_file_is_refused_before_it_is_opened(self):
        self._login(self.designer)

        response = self.client.post(self._url(), {
            'phase': 'preview',
            'file': SimpleUploadedFile('boq.pdf', b'%PDF-1.4 not a workbook',
                                       content_type='application/pdf')})

        self.assertEqual(response.context['stage'], 'upload')
        self.assertIn('.xlsx', response.context['file_error'])

    def test_too_many_rows_is_refused(self):
        self._login(self.designer)

        rows = [['OPX-001', 'x', 'Nos', 'Module', 1]] * (len(CATALOGUE) + 200)
        response = self._preview(rows)

        self.assertEqual(response.context['stage'], 'upload')
        self.assertIn('limit', response.context['file_error'])

    def test_trailing_blank_rows_are_dropped_and_row_numbers_still_point_true(self):
        """Excel adds phantom rows. They must not become records, and dropping them
        must not shift the row number a later error reports."""
        self._login(self.designer)

        response = self._preview([
            self._row('OPX-001', 1),
            [None, None, None, None, None],
            ['BAD-X', 'x', 'Nos', 'Module', 2],
        ])

        errors = response.context['errors']
        self.assertEqual(len(errors), 1, errors)
        self.assertIn('Row 4', errors[0])

    def test_a_forged_payload_at_the_confirm_stage_is_revalidated(self):
        """DECISION 12. The payload went out to the browser and came back."""
        self._seed_boq([('OPX-001', 5)])
        self._login(self.designer)

        forged = json.dumps([{'code': 'NOPE-9', 'quantity': '3'}])
        response = self.client.post(self._url(),
                                    {'phase': 'commit', 'rows_json': forged})

        self.assertEqual(response.context['stage'], 'errors')
        self.assertEqual(self._quantities(), {'OPX-001': Decimal('5.00')})

    def test_a_forged_negative_at_the_confirm_stage_is_revalidated(self):
        self._seed_boq([('OPX-001', 5)])
        self._login(self.designer)

        forged = json.dumps([{'code': 'OPX-001', 'quantity': '-7'}])
        response = self.client.post(self._url(),
                                    {'phase': 'commit', 'rows_json': forged})

        self.assertEqual(response.context['stage'], 'errors')
        self.assertEqual(self._quantities()['OPX-001'], Decimal('5.00'))


# ===========================================================================
# The BOQ row itself — created on commit only, never on GET and never at preview
# ===========================================================================

class LazyBoqCreateTests(UploadBase):

    def test_the_upload_screen_get_creates_nothing(self):
        self._login(self.designer)

        self.client.get(self._url())

        self.assertFalse(BOQ.objects.filter(project=self.site).exists())

    def test_the_preview_creates_nothing(self):
        """A designer who previews and walks away leaves the site as they found it."""
        self._login(self.designer)

        response = self._preview([self._row('OPX-001', 5)])

        self.assertEqual(response.context['stage'], 'preview')
        self.assertFalse(BOQ.objects.filter(project=self.site).exists())

    def test_a_rejected_file_creates_nothing(self):
        self._login(self.designer)

        self._preview([['BAD-Z', 'x', 'Nos', 'Module', 1]])

        self.assertFalse(BOQ.objects.filter(project=self.site).exists())

    def test_the_commit_creates_the_boq_when_there_is_none(self):
        """93 of the 97 live OPEX sites are in this state."""
        self._login(self.designer)

        self._upload([self._row('OPX-003', 8)])

        self.assertTrue(BOQ.objects.filter(project=self.site).exists())
        self.assertEqual(self._quantities()['OPX-003'], Decimal('8.00'))

    def test_the_serial_no_comes_from_the_catalogue_sort_order(self):
        """The picker's rule, so an uploaded row is indistinguishable from a picked one."""
        self._login(self.designer)

        self._upload([self._row('OPX-004', 2)])

        row = BOQItem.objects.get(boq__project=self.site, item_master__code='OPX-004')
        self.assertEqual(row.serial_no, self._master('OPX-004').sort_order)
        self.assertTrue(row.is_standard_item)


# ===========================================================================
# Counts reported to the designer
# ===========================================================================

class ReportingTests(UploadBase):

    def test_the_preview_counts_added_changed_and_unchanged(self):
        self._seed_boq([('OPX-001', 5), ('OPX-002', 10)])
        self._login(self.designer)

        response = self._preview([
            self._row('OPX-001', 5),     # unchanged — same value
            self._row('OPX-002', 20),    # changed
            self._row('OPX-003', 1),     # added
        ])

        self.assertEqual(len(response.context['plan_add']), 1)
        self.assertEqual(len(response.context['plan_change']), 1)
        self.assertEqual(response.context['plan_unchanged'], 1)

    def test_a_blank_counts_as_unchanged_not_as_a_change(self):
        self._seed_boq([('OPX-001', 5)])
        self._login(self.designer)

        response = self._preview([self._row('OPX-001', None)])

        self.assertEqual(len(response.context['plan_change']), 0)
        self.assertEqual(response.context['plan_unchanged'], 1)

    def test_the_result_counts_match_what_the_preview_promised(self):
        """A put-back mandatory row is not counted as something the file added."""
        self._seed_boq([('OPX-002', 10)])
        self._login(self.designer)

        preview = self._preview([self._row('OPX-002', 11), self._row('OPX-003', 1)])
        planned_add = len(preview.context['plan_add'])
        planned_change = len(preview.context['plan_change'])
        result = self._commit(preview)

        self.assertEqual(result.context['added'], planned_add)
        self.assertEqual(result.context['changed'], planned_change)
        self.assertTrue(result.context['readded'], 'OPX-001 should have been put back')
