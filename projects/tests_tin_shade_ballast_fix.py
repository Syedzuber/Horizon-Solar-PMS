"""
B-0b — fix_tin_shade_ballast: Tin Shade (OPX-038) and Ballast (OPX-037) counted in Nos.

What these tests pin:
- --catalogue: dry run writes nothing; --apply changes only OPX-037 and OPX-038 to Nos,
  logs each before → after (OPX-037 with Praveen's one-off approval), never touches a
  BOQ line, and a second run changes nothing;
- the B-0 form still refuses a unit change on an item in use (the exception lives in the
  command only);
- --export lists exactly the lines on the two items, deleted sites included, with the
  agreed columns and flags, and writes nothing;
- --apply-counts writes a valid count and Nos with one log entry, skips blanks, refuses
  bad values, lines on other items and sites locked or ordered SINCE the export (checked
  live), skips lines changed since export, writes no correction record, and a second run
  writes nothing.
"""
import csv
import os
import tempfile
import uuid
from datetime import datetime
from decimal import Decimal
from io import StringIO
from zoneinfo import ZoneInfo

from django.contrib.auth.models import User
from django.core.management import CommandError, call_command
from django.test import TestCase
from django.utils import timezone

from .forms import UNIT_LOCKED_MESSAGE, BOQItemMasterForm
from .management.commands.fix_tin_shade_ballast import (
    CATALOGUE_ACTION_CODE, EXPORT_COLUMNS, LINE_ACTION_CODE,
)
from .models import (
    APPROVAL_KIND_MATERIAL_PRE_ORDER, ATTEMPT_REASON_INITIAL, BOQ, DESIGN_FILE_BOQ_EXCEL,
    DESIGN_IN_QC, DESIGN_RELEASED, SITE_GROUP_LOCKED, ActivityLog, ApprovalRequest,
    ArkaSubmission, BOQCorrection, BOQItem, BOQItemMaster, DesignAssignment, DesignAttempt,
    DesignFile, MaterialApprovalDetail, MaterialApprovalLine, Program, Project, SiteGroup,
    SiteGroupMembership, Vendor, VendorOrder, VendorOrderLine, VendorOrderProgram,
    VendorOrderSite,
    BOQ_CORRECTION_QUANTITY_CHANGED,
)
from .tests_catalogue_hygiene import _post_data

IST = ZoneInfo('Asia/Kolkata')


def _profile(username, role):
    """A post_save signal auto-creates the UserProfile; fetch and set, never create."""
    user = User.objects.create_user(username=username, password='x',
                                    first_name=username.title())
    profile = user.profile
    profile.role = role
    profile.is_active = True
    profile.save()
    return profile


class TsbBase(TestCase):
    """Three sites on one tender:
      A (TSB-A): Tin Shade 186.88 KWp (with a correction), and an INC work Tin shade line
                 (OPX-204) that must never be touched;
      B (TSB-B): Ballast 30.00 KWp, design released;
      C (TSB-C): deleted, Tin Shade 12 'No'.
    """

    def setUp(self):
        self.pm = _profile('tsb_pm', 'PM')
        self.designer = _profile('tsb_designer', 'Design')
        self.scm = _profile('tsb_scm', 'SCM')
        self.program = Program.objects.create(
            name='TSB Tender', program_type='OPEX', client_name='TSBClient',
            status='Active', short_tender_code='TSB')

        self.ballast = self._master('OPX-037', 'KWp', 'Ballast Type Structure', 'MMS')
        self.tin = self._master('OPX-038', 'No', 'Tin Shade', 'MMS')
        self.inc = self._master('OPX-204', 'KWp', 'INC work Tin shade', 'BOS')
        self.mms = self._master('OPX-027', 'Nos', '2P*3*1000mm height', 'MMS')

        self.site_a = self._site('TSB-A')
        self.site_b = self._site('TSB-B')
        self.site_c = self._site('TSB-C', is_deleted=True)

        # Made before 14 Nov, when the catalogue still said KWp.
        self.line_a = self._line(self.site_a, self.tin, Decimal('186.88'), uom='KWp')
        self.line_inc = self._line(self.site_a, self.inc, Decimal('186.88'))
        self.line_b = self._line(self.site_b, self.ballast, Decimal('30.00'))
        self.line_c = self._line(self.site_c, self.tin, Decimal('12'), uom='No')

        self.assignment_a = DesignAssignment.objects.create(
            project=self.site_a, status=DESIGN_IN_QC, assigned_to=self.designer,
            survey_file_bucket='b', survey_file_path='TSB-A/survey/x.pdf',
            current_attempt_number=1)
        self.attempt_a = DesignAttempt.objects.create(
            assignment=self.assignment_a, attempt_number=1,
            opened_reason=ATTEMPT_REASON_INITIAL,
            boq_submitted_at=datetime(2026, 11, 4, 11, 15, tzinfo=IST),
            boq_submitted_by=self.designer)
        DesignAssignment.objects.create(
            project=self.site_b, status=DESIGN_RELEASED, assigned_to=self.designer,
            survey_file_bucket='b', survey_file_path='TSB-B/survey/x.pdf')
        BOQCorrection.objects.create(
            boq=self.line_a.boq, item=self.line_a, item_code='OPX-038',
            item_description='Tin Shade', action=BOQ_CORRECTION_QUANTITY_CHANGED,
            quantity_before=Decimal('162.00'), quantity_after=Decimal('186.88'),
            corrected_by=self.pm)

        self.vendor = Vendor.objects.create(name='TSB Steel', contact_person='R',
                                            phone='9000000009')
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for name in os.listdir(self.tmp):
            os.remove(os.path.join(self.tmp, name))
        os.rmdir(self.tmp)

    # ── fixtures ────────────────────────────────────────────────────────────

    def _master(self, code, unit, description, category):
        return BOQItemMaster.objects.create(code=code, project_type='OPEX', unit=unit,
                                            description=description, category=category,
                                            sort_order=1)

    def _site(self, code, is_deleted=False):
        site = Project(
            project_id=code, customer_name='TSBClient', customer_phone='9876543210',
            site_address='1 Sun Rd', city='Delhi', project_type='OPEX',
            program=self.program, site_code=code, site_name=f'Site {code}',
            dc_capacity_kw=Decimal('100.00'), status='Draft', assigned_pm=self.pm,
            is_deleted=is_deleted)
        site.save()
        BOQ.objects.create(project=site)
        return site

    def _line(self, site, master, quantity, uom=None):
        return BOQItem.objects.create(
            boq=site.boq, item_master=master, serial_no=master.sort_order,
            category=master.category, description=master.description,
            uom=uom or master.unit, boq_quantity=quantity)

    def _lock(self, site):
        group = SiteGroup.objects.create(
            program=self.program, name='Batch 1', status=SITE_GROUP_LOCKED,
            created_by=self.pm, locked_by=self.pm, locked_at=timezone.now())
        SiteGroupMembership.objects.create(group=group, project=site, added_by=self.pm)

    def _order(self, site, master):
        order = VendorOrder.objects.create(
            vendor=self.vendor, project_type='OPEX', total_amount=Decimal('5000'),
            created_by=self.scm, client_uuid=uuid.uuid4())
        VendorOrderSite.objects.create(order=order, project=site)
        VendorOrderLine.objects.create(order=order, item_master=master,
                                       item_code=master.code,
                                       item_description=master.description,
                                       item_unit=master.unit, quantity=Decimal('4'))
        return order

    def _approval(self, site, description='Tin Shade structure'):
        request = ApprovalRequest.objects.create(
            kind=APPROVAL_KIND_MATERIAL_PRE_ORDER, title='Structure', description='x',
            raised_by=self.scm)
        request.projects.add(site)
        detail = MaterialApprovalDetail.objects.create(request=request)
        MaterialApprovalLine.objects.create(detail=detail, position=1,
                                            description=description,
                                            quantity=Decimal('4'), unit='Nos')
        return request

    # ── running the command ─────────────────────────────────────────────────

    def run_cmd(self, *args):
        out = StringIO()
        call_command('fix_tin_shade_ballast', *args, stdout=out)
        return out.getvalue()

    def export(self):
        path = os.path.join(self.tmp, f'export-{uuid.uuid4().hex}.csv')
        output = self.run_cmd('--export', path)
        with open(path, newline='', encoding='utf-8-sig') as handle:
            reader = csv.DictReader(handle)
            return reader.fieldnames, list(reader), output

    def write_csv(self, rows):
        path = os.path.join(self.tmp, f'filled-{uuid.uuid4().hex}.csv')
        with open(path, 'w', newline='', encoding='utf-8-sig') as handle:
            writer = csv.DictWriter(handle, fieldnames=EXPORT_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
        return path

    def filled(self, counts, notes=None):
        """The export with new_count (and note) filled in for the given line ids."""
        _fields, rows, _out = self.export()
        for row in rows:
            row['new_count'] = counts.get(int(row['line_id']), '')
            row['note'] = (notes or {}).get(int(row['line_id']), '')
        return self.write_csv(rows)

    def line_logs(self):
        return list(ActivityLog.objects.filter(action_code=LINE_ACTION_CODE)
                    .order_by('pk'))

    def snapshot_lines(self):
        return list(BOQItem.objects.order_by('pk')
                    .values_list('pk', 'uom', 'boq_quantity', 'item_master_id'))


# ===========================================================================
# T1 — --catalogue
# ===========================================================================

class CatalogueTests(TsbBase):

    def units(self):
        return dict(BOQItemMaster.objects.values_list('code', 'unit'))

    def test_dry_run_writes_nothing(self):
        before = self.units()
        out = self.run_cmd('--catalogue')
        self.assertIn('DRY RUN', out)
        self.assertIn('OPX-037: KWp -> Nos', out)
        self.assertIn('OPX-038: No -> Nos', out)
        # Console text is cp1252-safe ("->"), so a piped Windows run cannot crash on it.
        out.encode('cp1252')
        self.assertEqual(self.units(), before)
        self.assertFalse(ActivityLog.objects.exists())

    def test_apply_changes_only_the_two_items_and_logs_both(self):
        self.run_cmd('--catalogue', '--apply')
        self.assertEqual(self.units(), {'OPX-037': 'Nos', 'OPX-038': 'Nos',
                                        'OPX-204': 'KWp', 'OPX-027': 'Nos'})
        logs = {log.entity_id: log for log in
                ActivityLog.objects.filter(entity_type='BOQItemMaster')}
        self.assertEqual(set(logs), {self.ballast.pk, self.tin.pk})
        ballast_log, tin_log = logs[self.ballast.pk], logs[self.tin.pk]
        self.assertEqual(
            ballast_log.action,
            "fix_tin_shade_ballast (system) updated RESCO BOQ catalogue item 'OPX-037' "
            "(one-off unit change approved by Praveen (F5, 22 Nov)): unit: KWp → Nos")
        self.assertEqual(
            tin_log.action,
            "fix_tin_shade_ballast (system) updated RESCO BOQ catalogue item 'OPX-038'"
            ": unit: No → Nos")
        for log in (ballast_log, tin_log):
            self.assertEqual(log.action_code, CATALOGUE_ACTION_CODE)
            self.assertIsNone(log.actor)

    def test_a_second_apply_changes_nothing(self):
        self.run_cmd('--catalogue', '--apply')
        logged = ActivityLog.objects.count()
        out = self.run_cmd('--catalogue', '--apply')
        self.assertIn('0 changed, 2 already Nos, 0 not found.', out)
        self.assertEqual(ActivityLog.objects.count(), logged)

    def test_catalogue_never_touches_a_boq_line(self):
        before = self.snapshot_lines()
        self.run_cmd('--catalogue', '--apply')
        self.assertEqual(self.snapshot_lines(), before)

    def test_the_b0_form_still_refuses_a_unit_change_on_an_item_in_use(self):
        """The exception lives in the command. OPX-204 (in use, KWp) still cannot become
        Nos on the form, and OPX-037 cannot go back to KWp after the command."""
        self.run_cmd('--catalogue', '--apply')
        for item, unit in ((self.inc, 'Nos'),
                           (BOQItemMaster.objects.get(code='OPX-037'), 'KWp')):
            with self.subTest(code=item.code):
                form = BOQItemMasterForm(_post_data(item, unit=unit), instance=item)
                self.assertFalse(form.is_valid())
                self.assertEqual(form.errors['unit'], [UNIT_LOCKED_MESSAGE])


# ===========================================================================
# T2 — --export
# ===========================================================================

class ExportTests(TsbBase):

    def test_lists_exactly_the_two_items_lines_deleted_sites_included(self):
        fields, rows, out = self.export()
        self.assertEqual(tuple(fields), EXPORT_COLUMNS)
        self.assertEqual([int(r['line_id']) for r in rows],
                         [self.line_a.pk, self.line_b.pk, self.line_c.pk])
        self.assertEqual([r['site_code'] for r in rows], ['TSB-A', 'TSB-B', 'TSB-C'])
        self.assertIn('3 lines, 3 sites, 1 released sites, 1 deleted sites; 0 lines '
                      'locked or ordered, 0 with BOQ files uploaded; 0 tender-wide '
                      'orders/approvals on these items (flag only).', out)

    def test_row_values(self):
        _fields, rows, _out = self.export()
        a, b, c = rows
        self.assertEqual(a['site_name'], 'Site TSB-A')
        self.assertEqual(a['site_status'], 'Draft')
        self.assertEqual(a['design_status'], DESIGN_IN_QC)
        self.assertEqual(a['released'], 'no')
        self.assertEqual(a['site_deleted'], 'no')
        self.assertEqual(a['designer'], 'Tsb_Designer')
        self.assertEqual((a['item_code'], a['item_description']), ('OPX-038', 'Tin Shade'))
        self.assertEqual((a['current_uom'], a['current_quantity']), ('KWp', '186.88'))
        self.assertEqual(a['boq_marked_complete'], '2026-11-04 11:15')
        self.assertTrue(a['last_correction'].endswith(' 162.00 → 186.88'), a['last_correction'])
        self.assertEqual((a['locked_or_ordered'], a['lock_reason']), ('no', ''))
        self.assertEqual(a['boq_files_uploaded'], 'no')
        self.assertEqual((a['new_count'], a['note']), ('', ''))
        self.assertEqual((b['item_code'], b['released'], b['design_status']),
                         ('OPX-037', 'yes', DESIGN_RELEASED))
        self.assertEqual((b['boq_marked_complete'], b['last_correction']), ('', ''))
        self.assertEqual((c['site_deleted'], c['current_uom'], c['designer']),
                         ('yes', 'No', ''))

    def test_flags_locked_ordered_approved_and_files(self):
        self._lock(self.site_a)
        order = self._order(self.site_b, self.ballast)
        request = self._approval(self.site_c)
        arka = ArkaSubmission.objects.create(
            attempt=self.attempt_a, version=1, capacity_kw=Decimal('100'),
            arka_link='https://example.com/arka', submitted_by=self.designer)
        DesignFile.objects.create(
            attempt=self.attempt_a, kind=DESIGN_FILE_BOQ_EXCEL, version=1, bucket='b',
            path='TSB-A/boq/x.xlsx', derived_from_arka=arka, uploaded_by=self.designer)
        _fields, rows, out = self.export()
        a, b, c = rows
        self.assertEqual((a['locked_or_ordered'], a['lock_reason']),
                         ('yes', 'locked procurement group'))
        self.assertEqual((b['locked_or_ordered'], b['lock_reason']),
                         ('yes', f'VendorOrderLine on order #{order.pk}'))
        self.assertEqual((c['locked_or_ordered'], c['lock_reason']),
                         ('yes', f'MaterialApprovalLine on approval #{request.pk} (open)'))
        self.assertEqual([r['boq_files_uploaded'] for r in rows], ['yes', 'no', 'no'])
        self.assertIn('3 lines locked or ordered, 1 with BOQ files uploaded;', out)

    def test_only_this_site_and_the_two_items_flag(self):
        """An order on another item, or an approval that names neither item, does not
        flag. Either of the two items on the site's order does: C's line is Tin Shade
        and its order is for Ballast, and Q4 asks about OPX-037/038 for that site."""
        self._order(self.site_a, self.mms)          # A: this site, another item
        self._order(self.site_c, self.ballast)      # C: the other of the two items
        self._approval(self.site_a, description='Module mounting structure')
        _fields, rows, _out = self.export()
        self.assertEqual([r['locked_or_ordered'] for r in rows], ['no', 'no', 'yes'])

    def test_tender_wide_orders_and_approvals_are_listed_but_flag_nothing(self):
        """An order or approval naming a tender and no site is listed, one line each,
        in the summary. It never sets locked_or_ordered and adds no column."""
        order = VendorOrder.objects.create(
            vendor=self.vendor, project_type='OPEX', total_amount=Decimal('9000'),
            created_by=self.scm, client_uuid=uuid.uuid4(), po_number='PO-77')
        VendorOrderProgram.objects.create(order=order, program=self.program)
        VendorOrderLine.objects.create(order=order, item_master=self.tin, item_code='OPX-038',
                                       item_description='Tin Shade', quantity=Decimal('9'))
        request = ApprovalRequest.objects.create(
            kind=APPROVAL_KIND_MATERIAL_PRE_ORDER, title='Ballast blocks', description='x',
            raised_by=self.scm)
        request.programs.add(self.program)
        detail = MaterialApprovalDetail.objects.create(request=request)
        MaterialApprovalLine.objects.create(detail=detail, position=1,
                                            description='Ballast concrete block',
                                            quantity=Decimal('40'), unit='Nos')
        # Not tender-wide (it names a site), and an order on neither item: not listed.
        self._order(self.site_a, self.tin)
        unrelated = VendorOrder.objects.create(
            vendor=self.vendor, project_type='OPEX', total_amount=Decimal('100'),
            created_by=self.scm, client_uuid=uuid.uuid4())
        VendorOrderLine.objects.create(order=unrelated, item_master=self.mms,
                                       item_code='OPX-027', item_description='2P*3',
                                       quantity=Decimal('1'))

        fields, rows, out = self.export()
        self.assertIn('2 tender-wide orders/approvals on these items (flag only).', out)
        self.assertIn(f'tender-wide: order #{order.pk} | tender TSB | PO PO-77 | '
                      f'OPX-038 Tin Shade', out)
        self.assertIn(f'tender-wide: approval #{request.pk} (open) | tender TSB | '
                      f'Ballast blocks | Ballast concrete block', out)
        self.assertNotIn(f'order #{unrelated.pk} ', out)
        self.assertEqual(tuple(fields), EXPORT_COLUMNS)
        # Only A's own site order flags; B and C are untouched by the tender-wide two.
        self.assertEqual([r['locked_or_ordered'] for r in rows], ['yes', 'no', 'no'])

    def test_export_writes_nothing(self):
        lines, logs = self.snapshot_lines(), ActivityLog.objects.count()
        self.export()
        self.assertEqual(self.snapshot_lines(), lines)
        self.assertEqual(ActivityLog.objects.count(), logs)


# ===========================================================================
# T3 — --apply-counts
# ===========================================================================

class ApplyCountsTests(TsbBase):

    def test_a_valid_row_updates_quantity_and_uom_and_logs_both(self):
        path = self.filled({self.line_a.pk: '12'}, notes={self.line_a.pk: 'from layout'})
        out = self.run_cmd('--apply-counts', path, '--apply')
        self.line_a.refresh_from_db()
        self.assertEqual((self.line_a.boq_quantity, self.line_a.uom), (Decimal('12'), 'Nos'))
        logs = self.line_logs()
        self.assertEqual(len(logs), 1)
        self.assertEqual(
            logs[0].action,
            f'fix_tin_shade_ballast (system) updated BOQ line #{self.line_a.pk} OPX-038 on '
            f'TSB-A: quantity: 186.88 → 12; uom: KWp → Nos; note: from layout')
        self.assertEqual((logs[0].entity_type, logs[0].entity_id, logs[0].project_id),
                         ('BOQItem', self.line_a.pk, self.site_a.pk))
        self.assertIsNone(logs[0].actor)
        self.assertIn('Summary: applied 1, skipped (empty new_count) 2,', out)

    def test_dry_run_writes_nothing(self):
        path = self.filled({self.line_a.pk: '12'})
        lines, logs = self.snapshot_lines(), ActivityLog.objects.count()
        out = self.run_cmd('--apply-counts', path)
        self.assertIn('would apply 1', out)
        self.assertEqual(self.snapshot_lines(), lines)
        self.assertEqual(ActivityLog.objects.count(), logs)

    def test_a_blank_row_is_skipped(self):
        path = self.filled({})
        lines = self.snapshot_lines()
        out = self.run_cmd('--apply-counts', path, '--apply')
        self.assertIn('skipped (empty new_count) 3', out)
        self.assertEqual(self.snapshot_lines(), lines)

    def test_zero_negative_decimal_and_text_are_refused(self):
        for value in ('0', '-3', '2.5', 'twelve'):
            with self.subTest(value=value):
                path = self.filled({self.line_a.pk: value})
                out = self.run_cmd('--apply-counts', path, '--apply')
                self.assertIn('refused (bad value) 1', out)
                self.line_a.refresh_from_db()
                self.assertEqual(self.line_a.boq_quantity, Decimal('186.88'))
                self.assertEqual(self.line_logs(), [])

    def test_a_line_changed_since_export_is_skipped(self):
        path = self.filled({self.line_a.pk: '12'})
        BOQItem.objects.filter(pk=self.line_a.pk).update(boq_quantity=Decimal('190.00'))
        out = self.run_cmd('--apply-counts', path, '--apply')
        self.assertIn('skipped (changed since export) 1', out)
        self.assertIn('changed since export (now 190.00, CSV says 186.88)', out)
        self.line_a.refresh_from_db()
        self.assertEqual((self.line_a.boq_quantity, self.line_a.uom),
                         (Decimal('190.00'), 'KWp'))
        self.assertEqual(self.line_logs(), [])

    def test_a_line_on_another_item_is_refused_even_with_its_id_in_the_csv(self):
        _fields, rows, _out = self.export()
        rows.append(dict(rows[0], line_id=str(self.line_inc.pk), new_count='5'))
        path = self.write_csv(rows)
        out = self.run_cmd('--apply-counts', path, '--apply')
        self.assertIn('refused (not on OPX-037/038) 1', out)
        self.line_inc.refresh_from_db()
        self.assertEqual((self.line_inc.boq_quantity, self.line_inc.uom),
                         (Decimal('186.88'), 'KWp'))
        self.assertEqual(self.line_logs(), [])

    def test_a_second_run_writes_nothing(self):
        path = self.filled({self.line_a.pk: '12', self.line_b.pk: '4'})
        self.run_cmd('--apply-counts', path, '--apply')
        lines, logs = self.snapshot_lines(), ActivityLog.objects.count()
        out = self.run_cmd('--apply-counts', path, '--apply')
        self.assertIn('applied 0', out)
        self.assertIn('skipped (changed since export) 2', out)
        self.assertEqual(self.snapshot_lines(), lines)
        self.assertEqual(ActivityLog.objects.count(), logs)

    def test_no_correction_record_is_created(self):
        before = BOQCorrection.objects.count()
        path = self.filled({self.line_a.pk: '12', self.line_b.pk: '4'})
        self.run_cmd('--apply-counts', path, '--apply')
        self.assertEqual(BOQCorrection.objects.count(), before)

    # ── the live lock re-check (B-0b Q4) ────────────────────────────────────

    def _assert_refused_after_export(self, make_blocked, reason):
        path = self.filled({self.line_a.pk: '12'})
        make_blocked()
        out = self.run_cmd('--apply-counts', path, '--apply')
        self.assertIn('refused (locked or ordered) 1', out)
        self.assertIn(reason, out)
        self.line_a.refresh_from_db()
        self.assertEqual((self.line_a.boq_quantity, self.line_a.uom),
                         (Decimal('186.88'), 'KWp'))
        self.assertEqual(self.line_logs(), [])

    def test_a_site_locked_after_export_is_refused(self):
        self._assert_refused_after_export(lambda: self._lock(self.site_a),
                                          'locked procurement group')

    def test_a_site_ordered_after_export_is_refused(self):
        self._assert_refused_after_export(lambda: self._order(self.site_a, self.tin),
                                          'VendorOrderLine on order #')

    def test_a_site_with_an_approval_after_export_is_refused(self):
        self._assert_refused_after_export(lambda: self._approval(self.site_a),
                                          'MaterialApprovalLine on approval #')

    def test_a_row_is_refused_when_its_line_is_listed_twice(self):
        _fields, rows, _out = self.export()
        rows[0]['new_count'] = '12'
        rows.append(dict(rows[0], new_count='13'))
        out = self.run_cmd('--apply-counts', self.write_csv(rows), '--apply')
        self.assertIn('line listed twice', out)
        self.line_a.refresh_from_db()
        self.assertEqual(self.line_a.boq_quantity, Decimal('12'))


# ===========================================================================
# --allow-lines / --reason — the per-line override of the lock refusal
# ===========================================================================

class AllowLinesTests(TsbBase):

    REASON = 'SCM confirmed the order is for MMS 2P only'

    def setUp(self):
        super().setUp()
        self.path = self.filled({self.line_a.pk: '12'})
        self._lock(self.site_a)                        # locked after the export

    def test_a_locked_row_is_refused_without_the_flag(self):
        out = self.run_cmd('--apply-counts', self.path, '--apply')
        self.assertIn('refused (locked or ordered) 1', out)
        self.line_a.refresh_from_db()
        self.assertEqual(self.line_a.boq_quantity, Decimal('186.88'))
        self.assertEqual(self.line_logs(), [])

    def test_applied_with_the_flag_and_reason_and_the_override_is_logged(self):
        out = self.run_cmd('--apply-counts', self.path, '--apply',
                           '--allow-lines', str(self.line_a.pk), '--reason', self.REASON)
        self.assertIn('applied 1', out)
        self.assertIn('[lock override: locked procurement group]', out)
        self.line_a.refresh_from_db()
        self.assertEqual((self.line_a.boq_quantity, self.line_a.uom), (Decimal('12'), 'Nos'))
        logs = self.line_logs()
        self.assertEqual(len(logs), 1)
        self.assertEqual(
            logs[0].action,
            f'fix_tin_shade_ballast (system) updated BOQ line #{self.line_a.pk} OPX-038 on '
            f'TSB-A: quantity: 186.88 → 12; uom: KWp → Nos; lock override: locked '
            f'procurement group; reason: {self.REASON}')

    def test_the_dry_run_with_the_flag_writes_nothing(self):
        lines = self.snapshot_lines()
        out = self.run_cmd('--apply-counts', self.path,
                           '--allow-lines', str(self.line_a.pk), '--reason', self.REASON)
        self.assertIn('would apply 1', out)
        self.assertEqual(self.snapshot_lines(), lines)
        self.assertEqual(self.line_logs(), [])

    def test_a_listed_line_that_is_stale_is_still_skipped(self):
        BOQItem.objects.filter(pk=self.line_a.pk).update(boq_quantity=Decimal('190.00'))
        out = self.run_cmd('--apply-counts', self.path, '--apply',
                           '--allow-lines', str(self.line_a.pk), '--reason', self.REASON)
        self.assertIn('skipped (changed since export) 1', out)
        self.line_a.refresh_from_db()
        self.assertEqual(self.line_a.boq_quantity, Decimal('190.00'))
        self.assertEqual(self.line_logs(), [])

    def test_a_listed_line_with_a_bad_count_is_still_refused(self):
        path = self.filled({self.line_a.pk: '2.5'})
        out = self.run_cmd('--apply-counts', path, '--apply',
                           '--allow-lines', str(self.line_a.pk), '--reason', self.REASON)
        self.assertIn('refused (bad value) 1', out)
        self.assertEqual(self.line_logs(), [])

    def test_allow_lines_without_reason_is_rejected(self):
        for extra in ((), ('--reason', '   ')):
            with self.subTest(extra=extra):
                with self.assertRaisesMessage(CommandError, '--allow-lines needs --reason'):
                    self.run_cmd('--apply-counts', self.path, '--apply',
                                 '--allow-lines', str(self.line_a.pk), *extra)
        self.assertEqual(self.line_logs(), [])

    def test_an_id_not_in_the_csv_is_rejected(self):
        with self.assertRaisesMessage(CommandError, 'not in'):
            self.run_cmd('--apply-counts', self.path, '--apply',
                         '--allow-lines', f'{self.line_a.pk},{self.line_inc.pk}',
                         '--reason', self.REASON)
        self.line_a.refresh_from_db()
        self.assertEqual(self.line_a.boq_quantity, Decimal('186.88'))

    def test_reason_without_allow_lines_and_flags_outside_apply_counts_are_rejected(self):
        with self.assertRaisesMessage(CommandError, '--reason goes with --allow-lines'):
            self.run_cmd('--apply-counts', self.path, '--reason', self.REASON)
        with self.assertRaisesMessage(CommandError, 'only with --apply-counts'):
            self.run_cmd('--catalogue', '--allow-lines', '1', '--reason', self.REASON)
