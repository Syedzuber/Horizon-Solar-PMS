"""NTF-DCG 4a — goods receipts: the numbered, append-only journal of GRN submissions.

Pinned here:
  * each successful confirm_grn writes one kind='receipt' GoodsReceipt and each
    override_grn one kind='correction', numbered "GRN-{pk:06d}", with a line only for the
    DC lines that submission recorded or changed, holding before AND after values;
  * an empty submission is refused with the draft kept, before any upload (G4), and a
    submission left empty under the lock is refused as changed meanwhile;
  * ruling WA: a line posted back unchanged is not saved — its date and confirmer stay;
  * refused and rolled-back GRNs write no receipt (G2);
  * damage: the receipt line carries it, and the photo rows point at the receipt (P5);
  * append-only models; numbering; the backfill command (G7); the DC page section (G6);
  * the GRN form wording (WB).

SQLite: the challan row lock is a no-op here, so two concurrent writers cannot be run
in this suite. Numbering is pinned at the logic level (the number is the pk, which the
INSERT draws, and the unique index is the backstop); the two-worker case was run by
hand on Postgres (session report).

Run with:
    python manage.py test projects.tests_goods_receipts --settings=solarpms.test_settings
"""
from datetime import date, timedelta
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.db import IntegrityError, connection, transaction
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from . import grn_damage
from .goods_receipts import NOTHING_TO_RECORD, line_changed
from .models import (
    GOODS_RECEIPT_CORRECTION, GOODS_RECEIPT_IMPORTED, GOODS_RECEIPT_RECEIPT,
    AppendOnlyViolation, DCLineDamagePhoto, DCLineItem, DeliveryChallan, GoodsReceipt,
    GoodsReceiptLine, ProjectPhase, Task,
)
from .tests_grn_damage import GrnDamageBase, _profile, jpg

TOTAL_LABEL = 'Total received so far (including earlier deliveries)'


class ReceiptBase(GrnDamageBase):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        # A second site engineer on the same site, for ruling WA.
        cls.se_b = _profile('gr_se_b', 'Site Engineer')
        phase = ProjectPhase.objects.get(project=cls.site)
        Task.objects.create(phase=phase, task_name='Receive material too', task_order=2,
                            assigned_role=Task.SITE_ENGINEER, assigned_to=cls.se_b)

    def receipts(self):
        return list(GoodsReceipt.objects.filter(challan=self.dc).order_by('pk'))

    def lines_of(self, receipt):
        return {line.dc_line_id: line for line in receipt.lines.all()}

    def assert_refused_empty(self, response):
        self.assertIn(NOTHING_TO_RECORD, self.messages_of(response))
        self.assertIn('grn_draft', self.client.session)


# ---------------------------------------------------------------------------
# Receipts from the two views
# ---------------------------------------------------------------------------

class ConfirmReceiptTests(ReceiptBase):

    def test_first_confirm_writes_one_receipt_with_lines_only_for_recorded_lines(self):
        self.confirm({self.panels: ('6', '0', [])})
        [receipt] = self.receipts()
        self.assertEqual(receipt.kind, GOODS_RECEIPT_RECEIPT)
        self.assertEqual(receipt.number, f'GRN-{receipt.pk:06d}')
        self.assertEqual((receipt.recorded_by, receipt.receipt_date, receipt.on_behalf_reason,
                          receipt.notes), (self.se, date.today(), '', ''))
        lines = self.lines_of(receipt)
        self.assertEqual(set(lines), {self.panels.pk})     # the skipped inverter has none
        line = lines[self.panels.pk]
        self.assertEqual((line.received_before, line.received_after, line.damaged_before,
                          line.damaged_after, line.condition, line.grn_notes),
                         (None, Decimal('6'), None, 0, DCLineItem.GOOD, f'note {self.panels.pk}'))
        self.assertEqual((line.item_description, line.unit, line.ordered_quantity),
                         ('Panel 540Wp', 'Nos', Decimal('10')))
        self.assertEqual(line.received_change, '— → 6 (+6)')

    def test_second_partial_confirm_records_before_and_after(self):
        self.confirm({self.panels: ('6', '0', [])})
        self.confirm({self.panels: ('10', '0', []), self.inverter: ('2', '0', [])})
        first, second = self.receipts()
        lines = self.lines_of(second)
        self.assertEqual(set(lines), {self.panels.pk, self.inverter.pk})
        panels = lines[self.panels.pk]
        self.assertEqual((panels.received_before, panels.received_after,
                          panels.damaged_before, panels.damaged_after),
                         (Decimal('6'), Decimal('10'), 0, 0))
        self.assertEqual(panels.received_change, '6 → 10 (+4)')
        self.assertNotEqual(first.number, second.number)

    def test_identical_resubmit_is_refused_as_empty_and_keeps_the_draft(self):
        self.confirm({self.panels: ('6', '0', [])})
        DCLineItem.objects.filter(pk=self.panels.pk).update(grn_date=date.today() - timedelta(days=3))
        response = self.confirm({self.panels: ('6.00', '0', [])})
        self.assert_refused_empty(response)
        self.assertEqual(len(self.receipts()), 1)
        self.panels.refresh_from_db()
        self.assertEqual(self.panels.grn_date, date.today() - timedelta(days=3))

    def test_an_all_blank_submission_is_refused_as_empty(self):
        response = self.confirm({})
        self.assert_refused_empty(response)
        self.assertEqual(self.receipts(), [])
        self.dc.refresh_from_db()
        self.assertEqual(self.dc.status, DeliveryChallan.EXPECTED)

    def test_a_notes_only_change_is_recorded(self):
        self.confirm({self.panels: ('6', '0', [])})
        self.login(self.se)
        self.client.post(reverse('confirm_grn', args=[self.site.project_id, self.dc.pk]), {
            f'received_qty_{self.panels.pk}': '6', f'damaged_qty_{self.panels.pk}': '0',
            f'grn_notes_{self.panels.pk}': 'one crate opened for count'})
        second = self.receipts()[1]
        line = self.lines_of(second)[self.panels.pk]
        self.assertEqual((line.received_change, line.grn_notes),
                         ('6 → 6 (±0)', 'one crate opened for count'))


class UnchangedLineNotSavedTests(ReceiptBase):
    """Ruling WA."""

    def test_an_unchanged_line_keeps_its_confirmer_and_date_when_another_se_confirms(self):
        # SE A records both lines; the inverter one short keeps the DC open for SE B.
        self.confirm({self.panels: ('10', '0', []), self.inverter: ('1', '0', [])},
                     actor=self.se)
        earlier = date.today() - timedelta(days=2)
        DCLineItem.objects.filter(pk=self.panels.pk).update(grn_date=earlier)
        # The form pre-fills the panels line, so SE B posts it back unchanged.
        self.confirm({self.panels: ('10', '0', []), self.inverter: ('2', '0', [])},
                     actor=self.se_b)
        self.refresh(self.panels, self.inverter)
        self.assertEqual((self.panels.grn_confirmed_by, self.panels.grn_date),
                         (self.se, earlier))
        self.assertEqual((self.inverter.grn_confirmed_by, self.inverter.grn_date,
                          self.inverter.received_quantity),
                         (self.se_b, date.today(), Decimal('2')))
        second = self.receipts()[1]
        self.assertEqual(set(self.lines_of(second)), {self.inverter.pk})
        self.assertEqual(second.recorded_by, self.se_b)
        # Status and mirrors still run as before: both lines are now complete.
        self.dc.refresh_from_db()
        self.assertEqual(self.dc.status, DeliveryChallan.RECEIVED)

    def test_an_unchanged_unconfirmed_line_asks_scm_for_no_reason(self):
        # A pre-G2 override left a line with a quantity and no confirmer.
        DCLineItem.objects.filter(pk=self.panels.pk).update(
            received_quantity=Decimal('10'), grn_notes=f'note {self.panels.pk}',
            condition=DCLineItem.GOOD, grn_date=date.today())
        self.confirm({self.inverter: ('2', '0', [])})
        response = self.override({self.panels: ('10', '0', []), self.inverter: ('1', '0', [])})
        self.assertNotIn(NOTHING_TO_RECORD, self.messages_of(response))
        self.refresh(self.panels, self.inverter)
        self.assertIsNone(self.panels.grn_confirmed_by)
        self.assertFalse(self.panels.grn_on_behalf)
        self.assertEqual(self.inverter.received_quantity, Decimal('1'))

    def test_line_changed_compares_received_damaged_notes_and_photos_not_condition(self):
        item = DCLineItem(received_quantity=Decimal('5.00'), damaged_quantity=1,
                          grn_notes='n', condition=DCLineItem.DAMAGED)
        self.assertFalse(line_changed(item, Decimal('5'), 1, 'n', []))
        self.assertTrue(line_changed(item, Decimal('6'), 1, 'n', []))
        self.assertTrue(line_changed(item, Decimal('5'), 0, 'n', []))
        self.assertTrue(line_changed(item, Decimal('5'), 1, 'm', []))
        self.assertTrue(line_changed(item, Decimal('5'), 1, 'n', ['photo']))
        self.assertTrue(line_changed(DCLineItem(damaged_quantity=0), Decimal('0'), 0, '', []))


class OverrideReceiptTests(ReceiptBase):

    def test_first_time_override_is_a_correction_with_the_reason(self):
        self.override({self.panels: ('10', '0', [])}, reason='Site engineer unreachable today')
        [receipt] = self.receipts()
        self.assertEqual((receipt.kind, receipt.recorded_by, receipt.on_behalf_reason),
                         (GOODS_RECEIPT_CORRECTION, self.scm, 'Site engineer unreachable today'))
        self.assertEqual(receipt.get_kind_display(), 'Correction (by SCM)')
        self.assertEqual(self.lines_of(receipt)[self.panels.pk].received_before, None)

    def test_a_correction_is_a_correction_and_carries_no_unused_reason(self):
        self.confirm({self.panels: ('10', '0', [])})
        self.override({self.panels: ('9', '0', [])}, reason='typed but not needed')
        first, second = self.receipts()
        self.assertEqual((first.kind, second.kind), (GOODS_RECEIPT_RECEIPT, GOODS_RECEIPT_CORRECTION))
        self.assertEqual(second.on_behalf_reason, '')
        self.assertEqual(self.lines_of(second)[self.panels.pk].received_change, '10 → 9 (−1)')

    def test_an_empty_override_is_refused_and_keeps_the_reason(self):
        self.confirm({self.panels: ('10', '0', [])})
        response = self.override({self.panels: ('10', '0', [])}, reason='kept as typed')
        self.assert_refused_empty(response)
        self.assertEqual(self.client.session['grn_draft']['on_behalf_reason'], 'kept as typed')
        self.assertEqual(len(self.receipts()), 1)


class NoReceiptOnFailureTests(ReceiptBase):

    def test_a_grn_refused_as_changed_meanwhile_writes_no_receipt(self):
        with mock.patch('projects.views.reload_grn_lines',
                        side_effect=grn_damage.ChallanChangedMeanwhile(
                            grn_damage.GRN_CHANGED_MEANWHILE)):
            response = self.confirm({self.panels: ('10', '0', [])})
        self.assertIn(grn_damage.GRN_CHANGED_MEANWHILE, self.messages_of(response))
        self.assertEqual(self.receipts(), [])

    def test_the_same_values_committed_in_the_window_leave_nothing_and_refuse(self):
        real = grn_damage.upload_planned_photos

        def other_grn_then_upload(plan, project):
            DCLineItem.objects.filter(pk=self.panels.pk).update(
                received_quantity=Decimal('10'), grn_notes=f'note {self.panels.pk}',
                condition=DCLineItem.GOOD, grn_confirmed_by=self.se_b, grn_date=date.today())
            return real(plan, project)

        with mock.patch('projects.views.upload_planned_photos', side_effect=other_grn_then_upload):
            response = self.confirm({self.panels: ('10', '0', [])})
        self.assertIn(grn_damage.GRN_CHANGED_MEANWHILE, self.messages_of(response))
        self.assertEqual(self.receipts(), [])
        self.panels.refresh_from_db()
        self.assertEqual(self.panels.grn_confirmed_by, self.se_b)

    def test_a_rolled_back_grn_writes_no_receipt(self):
        with mock.patch('projects.views.recalculate_dc_status', side_effect=RuntimeError('db down')):
            with self.assertRaises(RuntimeError):
                self.confirm({self.panels: ('10', '0', [])})
        self.assertEqual(self.receipts(), [])
        self.panels.refresh_from_db()
        self.assertIsNone(self.panels.received_quantity)


class DamageReceiptTests(ReceiptBase):

    def test_a_damaged_line_carries_damage_condition_and_its_photos(self):
        self.confirm({self.panels: ('10', '2', [jpg()])}, description='Two panels cracked')
        [receipt] = self.receipts()
        line = self.lines_of(receipt)[self.panels.pk]
        self.assertEqual((line.damaged_after, line.condition), (2, DCLineItem.PARTIAL))
        self.assertTrue(line.has_damage)
        [photo] = DCLineDamagePhoto.objects.all()
        self.assertEqual(photo.receipt, receipt)

    def test_a_new_photo_on_an_unchanged_damaged_line_is_recorded(self):
        self.confirm({self.panels: ('10', '2', [jpg()])}, description='Two panels cracked')
        self.confirm({self.panels: ('10', '2', [jpg('second.jpg')])})
        second = self.receipts()[1]
        self.assertEqual(set(self.lines_of(second)), {self.panels.pk})
        self.assertEqual(DCLineDamagePhoto.objects.filter(receipt=second).count(), 1)


# ---------------------------------------------------------------------------
# The models
# ---------------------------------------------------------------------------

class ModelTests(ReceiptBase):

    def make(self, kind=GOODS_RECEIPT_RECEIPT, recorded_by='se'):
        return GoodsReceipt.objects.create(
            challan=self.dc, kind=kind, receipt_date=date.today(),
            recorded_by=self.se if recorded_by == 'se' else recorded_by)

    def test_append_only(self):
        receipt = self.make()
        line = GoodsReceiptLine.objects.create(
            receipt=receipt, dc_line=self.panels, received_after=Decimal('1'), damaged_after=0,
            item_description='x', unit='Nos', ordered_quantity=Decimal('1'))
        for row in (receipt, line):
            with self.subTest(model=type(row).__name__):
                with self.assertRaises(AppendOnlyViolation):
                    row.save()
                with self.assertRaises(AppendOnlyViolation):
                    row.delete()

    def test_numbers_come_from_the_pk_and_are_unique(self):
        first, second = self.make(), self.make()
        self.assertEqual([first.number, second.number],
                         [f'GRN-{first.pk:06d}', f'GRN-{second.pk:06d}'])
        self.assertEqual(GoodsReceipt.objects.get(pk=second.pk).number, second.number)
        # The unique index is the backstop if two rows were ever given one number.
        with self.assertRaises(IntegrityError), transaction.atomic():
            GoodsReceipt.objects.filter(pk=second.pk).update(number=first.number)

    def test_a_live_receipt_needs_a_recorder_an_imported_one_need_not(self):
        self.make(kind=GOODS_RECEIPT_IMPORTED, recorded_by=None)
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make(kind=GOODS_RECEIPT_RECEIPT, recorded_by=None)

    def test_the_challan_and_line_are_protected(self):
        from django.db.models import ProtectedError
        self.confirm({self.panels: ('10', '0', [])})
        with self.assertRaises(ProtectedError):
            DCLineItem.objects.filter(pk=self.panels.pk).delete()

    def test_no_price_field_anywhere_a_receipt_reads(self):
        for model in (GoodsReceipt, GoodsReceiptLine, DCLineItem):
            names = [f.name for f in model._meta.get_fields()]
            with self.subTest(model=model.__name__):
                self.assertFalse([n for n in names
                                  if any(w in n for w in ('price', 'rate', 'amount', 'cost'))])


# ---------------------------------------------------------------------------
# G6 — the DC page, and WB — the form wording
# ---------------------------------------------------------------------------

class PageTests(ReceiptBase):

    def detail(self, profile):
        self.login(profile)
        return self.client.get(reverse('delivery_challan_detail',
                                       args=[self.site.project_id, self.dc.pk]))

    def section(self, profile):
        html = self.detail(profile).content.decode()
        if 'id="dc-receipts"' not in html:
            return ''
        # Up to the next card or modal, so the forms' and modal's text cannot answer
        # for it. Which one comes next depends on the role and the challan's state.
        start = html.index('id="dc-receipts"')
        ends = [html.find(marker, start + 1) for marker in ('<div class="card ', '<div class="modal ')]
        return html[start:min([e for e in ends if e != -1] or [len(html)])]

    def test_no_section_before_the_first_receipt(self):
        self.assertEqual(self.section(self.scm), '')

    def test_se_scm_and_pm_see_the_receipts(self):
        self.confirm({self.panels: ('6', '0', [])})
        self.confirm({self.panels: ('10', '0', [])})
        self.override({self.panels: ('9', '0', [])})
        first, second, third = self.receipts()
        for profile in (self.se, self.scm, self.pm):
            with self.subTest(role=profile.role):
                section = self.section(profile)
                for receipt in (first, second, third):
                    self.assertIn(receipt.number, section)
                self.assertLess(section.index(third.number), section.index(first.number))
                self.assertIn('Correction (by SCM)', section)
                self.assertIn('6 → 10 (+4)', section)
                self.assertIn('10 → 9 (−1)', section)

    def test_an_imported_receipt_says_it_is_approximate(self):
        GoodsReceipt.objects.create(challan=self.dc, kind=GOODS_RECEIPT_IMPORTED,
                                    receipt_date=date.today())
        section = self.section(self.se)
        self.assertIn('Imported', section)
        self.assertIn('Approximate', section)
        self.assertIn('not recorded', section)

    def test_more_receipts_cost_nothing_more(self):
        self.confirm({self.panels: ('6', '0', [])})
        self.detail(self.scm)
        with CaptureQueriesContext(connection) as one:
            self.detail(self.scm)
        self.confirm({self.panels: ('8', '0', [])})
        self.confirm({self.inverter: ('2', '0', [])})
        self.detail(self.scm)
        with CaptureQueriesContext(connection) as three:
            self.detail(self.scm)
        self.assertEqual(len(three), len(one))

    def test_both_forms_say_the_box_is_the_running_total(self):
        se_page = self.detail(self.se).content.decode()
        scm_page = self.detail(self.scm).content.decode()
        self.assertIn(f'<th style="width:10rem;">{TOTAL_LABEL}</th>', se_page)
        self.assertIn(f'<th style="width:10rem;">{TOTAL_LABEL}</th>', scm_page)
        self.assertIn(f'data-label="{TOTAL_LABEL}"', se_page)
        self.assertIn(f'data-label="{TOTAL_LABEL}"', scm_page)
        self.assertNotIn('Received Qty', se_page + scm_page)


# ---------------------------------------------------------------------------
# G7 — backfill_goods_receipts
# ---------------------------------------------------------------------------

class BackfillTests(ReceiptBase):

    def run_cmd(self, *args):
        out = StringIO()
        call_command('backfill_goods_receipts', *args, stdout=out)
        return out.getvalue()

    def legacy(self, line, received, on=None, by=None, damaged=0, reason=''):
        """Receipt data written the way pre-4a GRNs left it — no receipt."""
        DCLineItem.objects.filter(pk=line.pk).update(
            received_quantity=Decimal(received), damaged_quantity=damaged,
            condition=DCLineItem.GOOD if not damaged else DCLineItem.PARTIAL,
            grn_date=on or date.today() - timedelta(days=5),
            grn_confirmed_by=by, grn_notes='legacy',
            grn_on_behalf=bool(reason), grn_on_behalf_reason=reason)

    def test_dry_run_writes_nothing(self):
        self.legacy(self.panels, '10', by=self.se)
        output = self.run_cmd()
        self.assertIn('Mode: DRY RUN', output)
        self.assertIn('Would write: 1 receipt(s), 1 line(s)', output)
        self.assertFalse(GoodsReceipt.objects.exists())

    def test_apply_imports_one_receipt_per_date_and_confirmer_and_is_idempotent(self):
        day1, day2 = date.today() - timedelta(days=9), date.today() - timedelta(days=4)
        self.legacy(self.panels, '10', on=day1, by=self.se)
        self.legacy(self.inverter, '2', on=day2, by=self.scm, reason='SE unreachable')
        output = self.run_cmd('--apply')
        self.assertIn('Written: 2 receipt(s), 2 line(s)', output)
        first, second = GoodsReceipt.objects.order_by('receipt_date')
        self.assertEqual((first.kind, first.receipt_date, first.recorded_by),
                         (GOODS_RECEIPT_IMPORTED, day1, self.se))
        self.assertEqual((second.receipt_date, second.recorded_by, second.on_behalf_reason),
                         (day2, self.scm, 'SE unreachable'))
        line = first.lines.get()
        self.assertEqual((line.received_before, line.received_after, line.damaged_before,
                          line.grn_notes, line.received_change),
                         (None, Decimal('10'), None, 'legacy', '10'))
        again = self.run_cmd('--apply')
        self.assertIn('Written: 0 receipt(s), 0 line(s)', again)
        self.assertIn('Skipped — already has an Imported receipt: 1', again)
        self.assertEqual(GoodsReceipt.objects.count(), 2)

    def test_a_gap_challan_imports_the_before_values_of_its_first_live_receipt(self):
        self.legacy(self.panels, '6', by=self.se)
        self.confirm({self.panels: ('10', '0', [])})       # a live GRN before the backfill
        output = self.run_cmd('--apply')
        self.assertIn('Gap challans (before-value fallback used): 1', output)
        imported = GoodsReceipt.objects.get(kind=GOODS_RECEIPT_IMPORTED)
        line = imported.lines.get()
        self.assertEqual((line.received_after, line.grn_notes, imported.recorded_by),
                         (Decimal('6'), '', None))
        self.assertIn('Written: 0', self.run_cmd('--apply'))

    def test_a_challan_fully_covered_by_live_receipts_is_skipped_and_listed(self):
        self.confirm({self.panels: ('10', '0', [])})
        output = self.run_cmd('--apply')
        self.assertIn('Skipped — fully covered by live receipts: 1', output)
        self.assertIn(f'DC pk {self.dc.pk}', output)
        self.assertEqual(GoodsReceipt.objects.filter(kind=GOODS_RECEIPT_IMPORTED).count(), 0)

    def test_a_line_with_no_received_quantity_is_skipped_and_listed(self):
        DCLineItem.objects.filter(pk=self.panels.pk).update(grn_confirmed_by=self.se)
        output = self.run_cmd('--apply')
        self.assertIn('Lines skipped (receipt data but no received quantity or GRN date): 1',
                      output)
        self.assertIn(f'line {self.panels.pk}', output)
        self.assertFalse(GoodsReceipt.objects.exists())
