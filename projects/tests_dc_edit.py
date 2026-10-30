"""
DC-B3: correcting a delivery challan after creation — SCM and Admin, with a reason, an
append-only DeliveryChallanRevision, and never a rewritten receipt.

WHAT IS PINNED HERE:
  * header: an edit writes ONE revision whose before/after carry the old and new values
    (display names beside ids), and one feed line without the reason;
  * refusals: a reason shorter than ten characters, a save that changes nothing, an
    unknown or inactive vendor (the challan's own deactivated vendor is kept);
  * lines without a receipt: category, quantity, removal; a category change moves the
    OPEX delivery buckets (through recalculate_dc_status, the permitted caller);
  * lines with a receipt: category, quantity and every receipt field refused even when
    posted by hand; description allowed and the receipt left exactly as it was; never
    removed; the last line never removed; new lines added;
  * E7: an engineer confirming a GRN between open and save, before the lock and under
    it, and a stale revision count — all refused with nothing written;
  * audience: SE, PM and CEO can neither open nor post; Admin can;
  * documents: added with the tick, refused without it, the live cap (withdrawn not
    counted), uploads discarded on a database failure;
  * a refused POST keeps what was typed, once;
  * the challan page: Edit DC button for SCM/Admin only, "Edited N times" and the
    history to its whole audience, the history costing one query;
  * the model: append-only save/delete, the reason CHECK, the unique revision number.

This module deliberately never names the delivery-mirror derivation: its caller list is
closed and asserted across the package (tests_design_mirror_derivation.test_01b).

Run with:
    python manage.py test projects.tests_dc_edit --settings=solarpms.test_settings
"""
import re
from datetime import date, timedelta
from decimal import Decimal
from importlib import import_module
from unittest import mock

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, connection, transaction
from django.test import Client, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from .dc_edit import CHANGED_ELSEWHERE, NOTHING_CHANGED
from .models import (
    ActivityLog, AppendOnlyViolation, DCLineItem, DeliveryChallan,
    DeliveryChallanAttachment, DeliveryChallanRevision, Project, ProjectPhase,
    StatusTransition, StockLocation, Task, TaskTemplate, TaskTemplatePhase,
    TaskTemplateTask, Vendor, recalculate_dc_status,
)
from .utils import resolve_residential_template

BUCKET = 'Horizon-PMS-Delivery-test'
PDF = b'%PDF-1.4 challan\n'
REASON = 'Vendor sent a corrected challan copy'


def _profile(username, role):
    user = User.objects.create_user(username=username, password='x',
                                    first_name=username.title(), last_name='Test')
    profile = user.profile          # auto-created by the post_save signal
    profile.role = role
    profile.save(update_fields=['role'])
    return profile


def _project(project_id, pm=None):
    return Project.objects.create(
        project_id=project_id, customer_name=project_id, customer_phone='9876543210',
        site_address='1 Sun Road', city='Delhi', project_type='Residential',
        dc_capacity_kw=Decimal('5.00'), status='Active', assigned_pm=pm,
        target_commissioning_date=date.today() + timedelta(days=90))


def _vendor(name, active=True):
    return Vendor.objects.create(name=name, contact_person='Desk', phone='9000000000',
                                 is_active=active)


@override_settings(SUPABASE_DELIVERY_BUCKET=BUCKET)
class DcEditBase(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.scm = _profile('dce_scm', 'SCM')
        cls.admin = _profile('dce_admin', 'Admin')
        cls.ceo = _profile('dce_ceo', 'CEO')
        cls.pm = _profile('dce_pm', 'PM')
        cls.se = _profile('dce_se', 'Site Engineer')
        cls.se_elsewhere = _profile('dce_se2', 'Site Engineer')
        cls.site = _project('DCE-SITE', pm=cls.pm)
        cls.other = _project('DCE-OTHER')
        phase = ProjectPhase.objects.create(project=cls.site, phase_name='Install',
                                            phase_order=1)
        Task.objects.create(phase=phase, task_name='Receive material', task_order=1,
                            assigned_role=Task.SITE_ENGINEER, assigned_to=cls.se)
        cls.vendor = _vendor('Sunrise Traders')
        cls.vendor2 = _vendor('Northwind Solar')
        cls.warehouse = StockLocation.objects.create(name='Hyderabad Central',
                                                     code='HYD-1')

    def setUp(self):
        self.storage = mock.MagicMock()
        self.bucket_api = self.storage.storage.from_.return_value
        for target in ('projects.delivery_storage._client', 'projects.design_storage._client'):
            patcher = mock.patch(target, return_value=self.storage)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = Client(SERVER_NAME='localhost')
        self.dc = DeliveryChallan.objects.create(
            project=self.site, vendor=self.vendor, dc_number='DCE-1',
            dc_date=date.today(), po_number='PO-7', notes='Two trucks')
        # One line received (by the SE, 8 of 10, so the challan is Partially Received and
        # confirm_grn still accepts a GRN on it) and one still expected.
        self.received = DCLineItem.objects.create(
            challan=self.dc, boq_category='Solar Modules', item_description='Mono 540 Wp',
            ordered_quantity=Decimal('10'), unit='Nos', received_quantity=Decimal('8'),
            damaged_quantity=0, condition='Good', grn_date=date.today(),
            grn_confirmed_by=self.se, grn_notes='All fine')
        self.expected = DCLineItem.objects.create(
            challan=self.dc, boq_category='Inverter', item_description='5 kW string inverter',
            ordered_quantity=Decimal('2'), unit='Nos')
        recalculate_dc_status(self.dc)
        self.dc.refresh_from_db()
        self.login(self.scm)

    # -- helpers -------------------------------------------------------------

    def login(self, profile):
        self.client.force_login(profile.user)

    def url(self, challan=None):
        challan = challan or self.dc
        return reverse('delivery_challan_edit', args=[challan.project.project_id, challan.pk])

    def detail_url(self, challan=None):
        challan = challan or self.dc
        return reverse('delivery_challan_detail', args=[challan.project.project_id, challan.pk])

    def form(self, challan=None, **overrides):
        """What the edit form posts unchanged, as the template renders it, plus a
        reason. Overrides win; a value of None removes the key."""
        challan = challan or self.dc
        challan.refresh_from_db()
        lines = list(challan.line_items.order_by('pk'))
        received = {line.pk for line in lines if line.received_quantity is not None}
        data = {
            'revision_count': str(challan.revisions.count()),
            'line_ids': ','.join(str(line.pk) for line in lines),
            'editable_line_ids': ','.join(str(line.pk) for line in lines
                                          if line.pk not in received),
            'vendor_id': str(challan.vendor_id or ''),
            'po_number': challan.po_number,
            'issued_from_warehouse': str(challan.issued_from_warehouse_id or ''),
            'dc_number': challan.dc_number,
            'dc_date': challan.dc_date.isoformat(),
            'expected_delivery_date': (challan.expected_delivery_date.isoformat()
                                       if challan.expected_delivery_date else ''),
            'notes': challan.notes,
            'reason': REASON,
        }
        for line in lines:
            prefix = f'line_{line.pk}_'
            data[prefix + 'description'] = line.item_description
            data[prefix + 'unit'] = line.unit
            if line.pk not in received:
                data[prefix + 'category'] = line.boq_category
                data[prefix + 'qty'] = str(line.ordered_quantity)
        data.update(overrides)
        return {k: v for k, v in data.items() if v is not None}

    def post(self, data, challan=None):
        return self.client.post(self.url(challan), data)

    def assert_refused(self, response, text, challan=None):
        self.assertRedirects(response, self.url(challan), fetch_redirect_response=False)
        page = self.client.get(self.url(challan))
        self.assertContains(page, text)
        self.assertFalse(DeliveryChallanRevision.objects.exists())

    def snapshot_db(self):
        """Every column of the challan and its lines, to prove a refusal wrote nothing."""
        return (list(DeliveryChallan.objects.filter(pk=self.dc.pk).values()),
                list(DCLineItem.objects.filter(challan=self.dc).order_by('pk').values()))

    def uploads(self):
        return self.bucket_api.upload.call_args_list

    def removes(self):
        return self.bucket_api.remove.call_args_list


class HeaderEditTests(DcEditBase):

    def test_header_edit_writes_one_revision_with_before_and_after(self):
        response = self.post(self.form(dc_number='DCE-1A', vendor_id=str(self.vendor2.pk),
                                       issued_from_warehouse=str(self.warehouse.pk),
                                       po_number='PO-8'))
        self.assertRedirects(response, self.detail_url(), fetch_redirect_response=False)
        self.dc.refresh_from_db()
        self.assertEqual((self.dc.dc_number, self.dc.vendor, self.dc.po_number,
                          self.dc.issued_from_warehouse),
                         ('DCE-1A', self.vendor2, 'PO-8', self.warehouse))
        revision = DeliveryChallanRevision.objects.get()
        self.assertEqual((revision.revision_no, revision.revised_by, revision.reason),
                         (1, self.scm, REASON))
        self.assertEqual(revision.before['schema'], 1)
        self.assertEqual(revision.before['header']['dc_number'], 'DCE-1')
        self.assertEqual(revision.after['header']['dc_number'], 'DCE-1A')
        self.assertEqual(revision.before['header']['vendor'],
                         {'id': self.vendor.pk, 'name': 'Sunrise Traders'})
        self.assertEqual(revision.after['header']['vendor'],
                         {'id': self.vendor2.pk, 'name': 'Northwind Solar'})
        self.assertIsNone(revision.before['header']['issued_from_warehouse'])
        self.assertEqual(revision.after['header']['issued_from_warehouse'],
                         {'id': self.warehouse.pk, 'code': 'HYD-1',
                          'name': 'Hyderabad Central'})
        self.assertEqual(revision.before['lines'], revision.after['lines'])
        self.assertEqual(revision.after['lines'][0]['ordered_quantity'], '10.00')
        self.assertNotIn('documents_added', revision.after)

    def test_feed_line_names_the_change_without_the_reason(self):
        self.post(self.form(dc_number='DCE-1A'))
        row = ActivityLog.objects.get(action_code='dc_edited')
        self.assertEqual(row.action, 'Edited DC DCE-1 — now DCE-1A')
        self.assertEqual((row.entity_type, row.entity_id), ('DeliveryChallan', self.dc.pk))
        self.assertNotIn(REASON, row.action)
        self.post(self.form(notes='Three trucks'))
        self.assertEqual(ActivityLog.objects.filter(action_code='dc_edited')
                         .order_by('-pk').first().action, 'Edited DC DCE-1A')

    def test_a_missing_or_short_reason_is_refused(self):
        before = self.snapshot_db()
        for reason in (None, '', 'too short'):
            with self.subTest(reason=reason):
                response = self.post(self.form(notes='Changed', reason=reason))
                self.assert_refused(response, 'Give a reason for the edit of at least 10')
                self.assertEqual(self.snapshot_db(), before)

    def test_a_save_that_changes_nothing_is_refused(self):
        before = self.snapshot_db()
        # "2" typed against a stored 2.00 is the same quantity.
        response = self.post(self.form(**{f'line_{self.expected.pk}_qty': '2'}))
        self.assert_refused(response, NOTHING_CHANGED)
        self.assertEqual(self.snapshot_db(), before)
        self.assertFalse(ActivityLog.objects.filter(action_code='dc_edited').exists())

    def test_the_status_is_recalculated_never_edited(self):
        transitions = StatusTransition.objects.count()
        self.post(self.form(notes='Changed', status='Received'))
        self.dc.refresh_from_db()
        # One line received short and one still expected: Partially Received, as before.
        self.assertEqual(self.dc.status, DeliveryChallan.PARTIALLY_RECEIVED)
        self.assertEqual(StatusTransition.objects.count(), transitions)

    def test_a_stale_stored_status_is_corrected_with_the_edit_reason_code(self):
        DeliveryChallan.objects.filter(pk=self.dc.pk).update(status=DeliveryChallan.EXPECTED)
        self.post(self.form(notes='Changed'))
        self.dc.refresh_from_db()
        self.assertEqual(self.dc.status, DeliveryChallan.PARTIALLY_RECEIVED)
        row = StatusTransition.objects.filter(
            to_status=DeliveryChallan.PARTIALLY_RECEIVED).latest('pk')
        self.assertEqual((row.from_status, row.reason_code), ('Expected', 'dc_edited'))

    def test_dates_are_checked_as_on_create(self):
        response = self.post(self.form(dc_date='2019-01-01'))
        self.assert_refused(response, 'DC Date:')
        response = self.post(self.form(dc_date=''))
        self.assert_refused(response, 'DC Date is required.')

    def test_the_challans_own_inactive_vendor_is_kept(self):
        Vendor.objects.filter(pk=self.vendor.pk).update(is_active=False)
        page = self.client.get(self.url())
        self.assertContains(page, 'Sunrise Traders (inactive)')
        self.post(self.form(notes='Changed'))
        self.dc.refresh_from_db()
        self.assertEqual(self.dc.vendor, self.vendor)
        self.assertEqual(DeliveryChallanRevision.objects.count(), 1)

    def test_an_unknown_or_inactive_vendor_is_refused_never_cleared(self):
        retired = _vendor('Retired Co', active=False)
        before = self.snapshot_db()
        for vendor_id in (str(retired.pk), '999999', 'abc'):
            with self.subTest(vendor_id=vendor_id):
                response = self.post(self.form(vendor_id=vendor_id))
                self.assert_refused(response, 'Vendor: the one chosen is not available')
                self.assertEqual(self.snapshot_db(), before)


class LineEditTests(DcEditBase):

    def test_an_unreceived_line_category_and_quantity_change(self):
        self.post(self.form(**{f'line_{self.expected.pk}_category': 'BOS',
                               f'line_{self.expected.pk}_qty': '3.5'}))
        self.expected.refresh_from_db()
        self.assertEqual((self.expected.boq_category, self.expected.ordered_quantity),
                         ('BOS', Decimal('3.50')))
        revision = DeliveryChallanRevision.objects.get()
        before = {line['id']: line for line in revision.before['lines']}
        after = {line['id']: line for line in revision.after['lines']}
        self.assertEqual((before[self.expected.pk]['boq_category'],
                          after[self.expected.pk]['boq_category']), ('Inverter', 'BOS'))
        self.assertEqual(after[self.expected.pk]['ordered_quantity'], '3.50')

    def test_received_line_category_quantity_and_receipt_fields_are_refused(self):
        pk = self.received.pk
        before = self.snapshot_db()
        for overrides, text in (
                ({f'line_{pk}_category': 'BOS'}, 'its category cannot change'),
                ({f'line_{pk}_qty': '12'}, 'its ordered quantity cannot change'),
                ({f'line_{pk}_received_quantity': '4'}, 'recorded through the GRN'),
                ({f'line_{pk}_grn_confirmed_by': str(self.scm.pk)}, 'recorded through the GRN'),
                ({f'line_{pk}_damaged_quantity': '1'}, 'recorded through the GRN'),
        ):
            with self.subTest(overrides=overrides):
                response = self.post(self.form(notes='Changed', **overrides))
                self.assert_refused(response, text)
                self.assertEqual(self.snapshot_db(), before)

    def test_a_received_lines_unchanged_category_posted_by_hand_is_no_refusal(self):
        self.post(self.form(**{f'line_{self.received.pk}_category': 'Solar Modules',
                               f'line_{self.received.pk}_qty': '10.00', 'notes': 'Changed'}))
        self.assertEqual(DeliveryChallanRevision.objects.count(), 1)

    def test_received_line_description_and_unit_edit_keeps_the_receipt(self):
        receipt = DCLineItem.objects.filter(pk=self.received.pk).values(
            'received_quantity', 'damaged_quantity', 'condition', 'grn_date',
            'grn_confirmed_by', 'grn_notes', 'grn_on_behalf', 'grn_on_behalf_reason',
            'boq_category', 'ordered_quantity').get()
        self.post(self.form(**{f'line_{self.received.pk}_description': 'Mono PERC 540 Wp',
                               f'line_{self.received.pk}_unit': 'Pcs'}))
        self.received.refresh_from_db()
        self.assertEqual((self.received.item_description, self.received.unit),
                         ('Mono PERC 540 Wp', 'Pcs'))
        self.assertEqual(DCLineItem.objects.filter(pk=self.received.pk).values(
            *receipt).get(), receipt)

    def test_remove_an_unreceived_line(self):
        self.post(self.form(**{f'line_{self.expected.pk}_remove': '1'}))
        self.assertFalse(DCLineItem.objects.filter(pk=self.expected.pk).exists())
        revision = DeliveryChallanRevision.objects.get()
        self.assertEqual(len(revision.before['lines']), 2)
        self.assertEqual([line['id'] for line in revision.after['lines']], [self.received.pk])

    def test_a_received_line_is_never_removed(self):
        before = self.snapshot_db()
        response = self.post(self.form(**{f'line_{self.received.pk}_remove': '1'}))
        self.assert_refused(response, 'so it cannot be removed')
        self.assertEqual(self.snapshot_db(), before)

    def test_the_last_line_is_never_removed(self):
        challan = DeliveryChallan.objects.create(project=self.site, dc_number='DCE-2',
                                                 dc_date=date.today())
        only = DCLineItem.objects.create(challan=challan, boq_category='BOS',
                                         item_description='Cable kit',
                                         ordered_quantity=Decimal('1'))
        response = self.post(self.form(challan, **{f'line_{only.pk}_remove': '1'}),
                             challan=challan)
        self.assert_refused(response, 'A challan keeps at least one line', challan=challan)
        self.assertTrue(DCLineItem.objects.filter(pk=only.pk).exists())

    def test_remove_the_last_line_and_add_one_is_fine(self):
        challan = DeliveryChallan.objects.create(project=self.site, dc_number='DCE-3',
                                                 dc_date=date.today())
        only = DCLineItem.objects.create(challan=challan, boq_category='BOS',
                                         item_description='Cable kit',
                                         ordered_quantity=Decimal('1'))
        self.post(self.form(challan, **{
            f'line_{only.pk}_remove': '1', 'new_line_category_0': 'Structure',
            'new_line_description_0': 'Rail set', 'new_line_qty_0': '4',
            'new_line_unit_0': 'Set'}), challan=challan)
        self.assertEqual(list(challan.line_items.values_list('item_description', flat=True)),
                         ['Rail set'])

    def test_add_lines_with_a_gap_in_the_numbering(self):
        self.post(self.form(**{
            'new_line_category_0': 'BOS', 'new_line_description_0': 'DC cable',
            'new_line_qty_0': '100', 'new_line_unit_0': 'm',
            # Row 1 was removed in the browser; row 2 must still be read.
            'new_line_category_2': 'Structure', 'new_line_description_2': 'Rail set',
            'new_line_qty_2': '4', 'new_line_unit_2': '',
            # A wholly blank row is dropped, as on create.
            'new_line_category_3': '', 'new_line_description_3': '', 'new_line_qty_3': '',
        }))
        added = DCLineItem.objects.filter(challan=self.dc).exclude(
            pk__in=[self.received.pk, self.expected.pk]).order_by('pk')
        self.assertEqual([(line.boq_category, line.item_description, line.unit,
                           line.ordered_quantity) for line in added],
                         [('BOS', 'DC cable', 'm', Decimal('100.00')),
                          ('Structure', 'Rail set', 'Nos', Decimal('4.00'))])
        self.assertTrue(all(line.received_quantity is None for line in added))
        revision = DeliveryChallanRevision.objects.get()
        self.assertEqual(len(revision.after['lines']), 4)

    def test_a_partly_filled_new_line_is_refused(self):
        response = self.post(self.form(new_line_category_0='BOS', new_line_description_0='',
                                       new_line_qty_0='5'))
        self.assert_refused(response, 'New line 1: description is required.')

    def test_a_bad_quantity_or_category_is_refused(self):
        pk = self.expected.pk
        for overrides, text in (({f'line_{pk}_qty': '0'}, 'quantity'),
                                ({f'line_{pk}_qty': '-2'}, 'quantity'),
                                ({f'line_{pk}_qty': '1.005'}, 'quantity'),
                                ({f'line_{pk}_category': 'Rocks'}, 'choose a category')):
            with self.subTest(overrides=overrides):
                self.assert_refused(self.post(self.form(**overrides)), text)


class ConcurrencyTests(DcEditBase):

    def grn(self):
        """The SE confirms the expected line through the real GRN endpoint."""
        self.login(self.se)
        response = self.client.post(
            reverse('confirm_grn', args=[self.site.project_id, self.dc.pk]),
            {f'received_qty_{self.expected.pk}': '2', f'damaged_qty_{self.expected.pk}': '0'})
        self.assertEqual(response.status_code, 302)
        self.expected.refresh_from_db()
        self.assertIsNotNone(self.expected.received_quantity)
        self.login(self.scm)

    def test_se_confirms_between_open_and_save(self):
        data = self.form(**{f'line_{self.expected.pk}_category': 'BOS',
                            f'line_{self.expected.pk}_qty': '5'})
        self.grn()
        before = self.snapshot_db()
        response = self.post(data)
        self.assert_refused(response, 'This challan was changed while you were editing it')
        self.assertEqual(self.snapshot_db(), before)

    def test_se_confirms_while_the_edit_waits_for_the_lock(self):
        # The receipt lands after the pre-lock checks passed, as if the GRN held the lock
        # first: the re-check under the lock must refuse, and roll back.
        from .grn_damage import lock_challan as real_lock

        def grn_then_lock(challan):
            DCLineItem.objects.filter(pk=self.expected.pk).update(
                received_quantity=Decimal('2'), grn_date=date.today(),
                grn_confirmed_by=self.se, condition='Good')
            return real_lock(challan)

        data = self.form(**{f'line_{self.expected.pk}_qty': '5', 'dc_number': 'DCE-9'})
        with mock.patch('projects.dc_edit.lock_challan', side_effect=grn_then_lock):
            response = self.post(data)
        self.assert_refused(response, 'This challan was changed while you were editing it')
        self.dc.refresh_from_db()
        self.expected.refresh_from_db()
        self.assertEqual((self.dc.dc_number, self.expected.ordered_quantity),
                         ('DCE-1', Decimal('2.00')))

    def test_a_stale_revision_count_is_refused(self):
        data = self.form(notes='Mine')
        self.post(self.form(notes='Theirs'))
        self.assertEqual(DeliveryChallanRevision.objects.count(), 1)
        response = self.post(data)
        self.assertRedirects(response, self.url(), fetch_redirect_response=False)
        self.assertContains(self.client.get(self.url()), 'This challan was changed')
        self.dc.refresh_from_db()
        self.assertEqual(self.dc.notes, 'Theirs')
        self.assertEqual(DeliveryChallanRevision.objects.count(), 1)

    def test_a_conflict_keeps_only_the_reason(self):
        data = self.form(notes='Mine', reason='My careful reason')
        self.post(self.form(notes='Theirs'))
        self.post(data)
        page = self.client.get(self.url())
        self.assertContains(page, 'My careful reason')
        self.assertContains(page, '>Theirs</textarea>')
        self.assertNotContains(page, '>Mine</textarea>')

    def test_the_second_edit_numbers_itself_two(self):
        self.post(self.form(notes='First'))
        self.post(self.form(notes='Second'))
        self.assertEqual(list(DeliveryChallanRevision.objects.values_list(
            'revision_no', flat=True)), [1, 2])


class AudienceTests(DcEditBase):

    def test_se_pm_and_ceo_can_neither_open_nor_post(self):
        before = self.snapshot_db()
        for profile in (self.se, self.pm, self.ceo):
            with self.subTest(role=profile.role):
                self.login(profile)
                self.assertEqual(self.client.get(self.url()).status_code, 403)
                self.assertEqual(self.post(self.form(notes='Changed')).status_code, 403)
        self.assertEqual(self.snapshot_db(), before)
        self.assertFalse(DeliveryChallanRevision.objects.exists())

    def test_someone_who_cannot_see_the_site_gets_a_404(self):
        self.login(self.se_elsewhere)
        self.assertEqual(self.client.get(self.url()).status_code, 404)

    def test_admin_may_edit(self):
        self.login(self.admin)
        self.assertEqual(self.client.get(self.url()).status_code, 200)
        self.post(self.form(notes='Admin fixed it'))
        self.assertEqual(DeliveryChallanRevision.objects.get().revised_by, self.admin)

    def test_another_projects_url_is_a_404(self):
        url = reverse('delivery_challan_edit', args=[self.other.project_id, self.dc.pk])
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_login_required(self):
        self.client.logout()
        self.assertEqual(self.client.get(self.url()).status_code, 302)


class DocumentTests(DcEditBase):

    def attach(self, name, withdrawn=False):
        row = DeliveryChallanAttachment.objects.create(
            challan=self.dc, uploaded_by=self.scm, original_name=name, bucket=BUCKET,
            path=f'DCE-SITE/challan/{name}', size=2048, mime='application/pdf',
            no_prices_confirmed=True)
        if withdrawn:
            DeliveryChallanAttachment.objects.filter(pk=row.pk).update(
                withdrawn_at=timezone.now(), withdrawn_by=self.scm,
                withdrawn_reason='Shows unit rates')
        return row

    def pdf(self, name='vendor-dc.pdf'):
        return SimpleUploadedFile(name, PDF, content_type='application/pdf')

    def test_a_document_only_save_with_the_tick(self):
        self.post(self.form(no_prices_confirmed='1', challan_documents=[self.pdf()]))
        doc = DeliveryChallanAttachment.objects.get()
        self.assertTrue(doc.no_prices_confirmed)
        self.assertEqual(len(self.uploads()), 1)
        revision = DeliveryChallanRevision.objects.get()
        self.assertEqual(revision.after['documents_added'], [doc.pk])
        page = self.client.get(self.detail_url())
        self.assertContains(page, '1 challan document added')
        # The file name is on the documents card; never in the history card.
        history = re.search(r'id="dc-history"(.*?)</li>\s*</ul>\s*</div>\s*</div>',
                            page.content.decode(), re.S).group(1)
        self.assertIn('1 challan document added', history)
        self.assertNotIn('vendor-dc.pdf', history)

    def test_documents_without_the_tick_are_refused_with_nothing_uploaded(self):
        response = self.post(self.form(challan_documents=[self.pdf()]))
        self.assert_refused(response, 'These documents show no rates')
        self.assertEqual(self.uploads(), [])
        self.assertFalse(DeliveryChallanAttachment.objects.exists())

    def test_the_live_cap_counts_documents_already_on_the_challan(self):
        for i in range(9):
            self.attach(f'live-{i}.pdf')
        response = self.post(self.form(no_prices_confirmed='1',
                                       challan_documents=[self.pdf('a.pdf'), self.pdf('b.pdf')]))
        self.assert_refused(response, 'it has 9 and 2 were chosen')
        self.assertEqual(self.uploads(), [])

    def test_withdrawn_documents_do_not_count(self):
        for i in range(9):
            self.attach(f'live-{i}.pdf')
        self.attach('withdrawn.pdf', withdrawn=True)
        self.post(self.form(no_prices_confirmed='1', challan_documents=[self.pdf()]))
        self.assertEqual(DeliveryChallanAttachment.objects.filter(
            withdrawn_at__isnull=True).count(), 10)

    def test_storage_off_refuses_a_chosen_file(self):
        with override_settings(SUPABASE_DELIVERY_BUCKET=''):
            response = self.post(self.form(no_prices_confirmed='1',
                                           challan_documents=[self.pdf()]))
        self.assert_refused(response, 'remove the attached files to save the other changes')

    def test_uploads_are_discarded_on_a_database_failure(self):
        with mock.patch('projects.dc_edit.recalculate_dc_status',
                        side_effect=RuntimeError('db down')):
            with self.assertRaises(RuntimeError):
                self.post(self.form(dc_number='DCE-X', no_prices_confirmed='1',
                                    challan_documents=[self.pdf('a.pdf'), self.pdf('b.pdf')]))
        self.assertFalse(DeliveryChallanAttachment.objects.exists())
        self.assertFalse(DeliveryChallanRevision.objects.exists())
        self.dc.refresh_from_db()
        self.assertEqual(self.dc.dc_number, 'DCE-1')
        uploaded = [c.kwargs['path'] for c in self.uploads()]
        self.assertEqual(len(uploaded), 2)
        self.assertEqual(self.removes(), [mock.call([p]) for p in uploaded])


class DraftTests(DcEditBase):

    def test_a_refused_post_keeps_what_was_typed_once(self):
        pk = self.expected.pk
        self.post(self.form(dc_number='TYPED-NO', reason='short', notes='Typed note',
                            challan_documents=[SimpleUploadedFile('x.pdf', PDF)],
                            **{f'line_{pk}_qty': '7', f'line_{pk}_category': 'BOS',
                               'new_line_category_0': 'Structure',
                               'new_line_description_0': 'Typed new line',
                               'new_line_qty_0': '3', 'new_line_unit_0': 'Set'}))
        page = self.client.get(self.url())
        for text in ('value="TYPED-NO"', '>Typed note</textarea>', '>short</textarea>',
                     'value="Typed new line"', 'The files you chose were not kept'):
            self.assertContains(page, text)
        content = page.content.decode()
        self.assertRegex(content, rf'name="line_{pk}_qty"[^>]*value="7"')
        self.assertRegex(content, rf'name="line_{pk}_category"[^<]*>\s*<option value="">'
                                  rf'[^<]*</option>(\s*<option[^>]*>[^<]*</option>)*?'
                                  rf'\s*<option value="BOS" selected>')
        self.assertNotContains(page, 'name="no_prices_confirmed" value="1" id="noPricesConfirmed" checked')
        again = self.client.get(self.url())
        self.assertContains(again, 'value="DCE-1"')
        self.assertNotContains(again, 'TYPED-NO')
        self.dc.refresh_from_db()
        self.assertEqual(self.dc.dc_number, 'DCE-1')


class FormRenderTests(DcEditBase):

    def test_the_form_offers_only_what_may_change(self):
        page = self.client.get(self.url()).content.decode()
        self.assertIn(f'name="line_{self.expected.pk}_category"', page)
        self.assertIn(f'name="line_{self.expected.pk}_qty"', page)
        self.assertIn(f'name="line_{self.expected.pk}_remove"', page)
        self.assertIn(f'name="line_{self.received.pk}_description"', page)
        self.assertNotIn(f'name="line_{self.received.pk}_category"', page)
        self.assertNotIn(f'name="line_{self.received.pk}_qty"', page)
        self.assertNotIn(f'name="line_{self.received.pk}_remove"', page)
        self.assertIn(f'name="editable_line_ids" value="{self.expected.pk}"', page)
        self.assertIn('name="revision_count" value="0"', page)
        self.assertIn('Saving…', page)
        # Template comments stay comments (a multi-line {# would render as text).
        self.assertNotIn('COPIED from', page)
        self.assertNotIn("the challan's edit history (E9)",
                         self.client.get(self.detail_url()).content.decode())


class DetailPageTests(DcEditBase):

    def test_edit_button_for_scm_and_admin_only(self):
        edit = self.url()
        for profile, shown in ((self.scm, True), (self.admin, True), (self.pm, False),
                               (self.se, False)):
            with self.subTest(role=profile.role):
                self.login(profile)
                page = self.client.get(self.detail_url())
                self.assertEqual(f'href="{edit}"' in page.content.decode(), shown)

    def test_history_is_shown_to_the_whole_audience(self):
        self.post(self.form(dc_number='DCE-1A', **{
            f'line_{self.expected.pk}_qty': '4'}))
        for profile in (self.scm, self.se, self.pm):
            with self.subTest(role=profile.role):
                self.login(profile)
                page = self.client.get(self.detail_url())
                self.assertContains(page, 'Edited 1 time')
                self.assertContains(page, REASON)
                self.assertContains(page, 'DC number: DCE-1 → DCE-1A')
                self.assertContains(page, 'Line &quot;5 kW string inverter&quot;: ordered 2.00 → 4.00')

    def test_no_history_card_before_the_first_edit(self):
        page = self.client.get(self.detail_url())
        self.assertNotContains(page, 'id="dc-history"')
        self.assertNotContains(page, 'Edited 0')

    def _count(self, url):
        with CaptureQueriesContext(connection) as ctx:
            self.assertEqual(self.client.get(url).status_code, 200)
        return len(ctx)

    def test_the_history_costs_one_query(self):
        self.post(self.form(notes='One'))
        self.post(self.form(notes='Two'))
        url = self.detail_url()
        for profile in (self.scm, self.admin, self.pm, self.se):
            with self.subTest(role=profile.role):
                self.login(profile)
                self.client.get(url)       # warm per-process caches
                with mock.patch('projects.views.challan_revisions', return_value=[]), \
                        mock.patch('projects.views.user_can_edit_dc', return_value=False):
                    before = self._count(url)
                self.assertEqual(self._count(url) - before, 1)

    def test_more_revisions_cost_nothing_more(self):
        self.post(self.form(notes='One'))
        url = self.detail_url()
        self.client.get(url)
        one = self._count(url)
        self.post(self.form(notes='Two'))
        self.assertEqual(self._count(url), one)


class ModelTests(DcEditBase):

    def row(self, **overrides):
        values = dict(challan=self.dc, revision_no=1, revised_by=self.scm, reason=REASON,
                      before={'schema': 1}, after={'schema': 1})
        values.update(overrides)
        return DeliveryChallanRevision(**values)

    def test_append_only(self):
        row = self.row()
        row.save()
        row.reason = 'Rewritten reason here'
        with self.assertRaises(AppendOnlyViolation):
            row.save()
        with self.assertRaises(AppendOnlyViolation):
            row.delete()
        self.assertEqual(DeliveryChallanRevision.objects.get().reason, REASON)

    def test_the_reason_is_required(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.row(reason='').save()

    def test_one_revision_number_per_challan(self):
        self.row().save()
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.row().save()

    def test_an_edited_challan_cannot_be_deleted(self):
        from django.db.models import ProtectedError
        self.row().save()
        with self.assertRaises(ProtectedError):
            self.dc.delete()


# ---------------------------------------------------------------------------
# The OPEX delivery buckets — a really activated OPEX site, as tests_delivery_detail
# ---------------------------------------------------------------------------

class _ConcreteApps:
    """Stands in for the `apps` registry a RunPython function is handed."""

    _MODELS = {'TaskTemplate': TaskTemplate, 'TaskTemplatePhase': TaskTemplatePhase,
               'TaskTemplateTask': TaskTemplateTask, 'Task': Task}

    def get_model(self, app_label, model_name):
        return self._MODELS[model_name]


@override_settings(SUPABASE_DELIVERY_BUCKET=BUCKET)
class DeliveryBucketTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        resolve_residential_template()
        import_module('projects.migrations.0075_seed_opex_template_v1').seed_opex_v1(
            _ConcreteApps(), None)
        cls.pm = _profile('dceb_pm', 'PM')
        cls.scm = _profile('dceb_scm', 'SCM')

    def setUp(self):
        self.site = Project.objects.create(
            customer_name='DCE OPEX Site', customer_phone='9876543210',
            site_address='1 Bucket Road', city='Lucknow', project_type='OPEX',
            dc_capacity_kw=Decimal('100.00'), status='Draft', assigned_pm=self.pm)
        client = Client(SERVER_NAME='localhost')
        client.force_login(self.pm.user)
        client.post(reverse('opex_site_activate', args=[self.site.project_id]))
        self.site.refresh_from_db()
        self.assertEqual(self.site.status, 'Active')
        self.client = Client(SERVER_NAME='localhost')
        self.client.force_login(self.scm.user)

    def bucket(self, code):
        return Task.objects.get(phase__project=self.site, is_mirror=True,
                                template_task__code=code).status

    def test_an_unreceived_line_category_change_moves_the_bucket(self):
        challan = DeliveryChallan.objects.create(project=self.site, dc_number='OPX-1',
                                                 dc_date=date.today())
        DCLineItem.objects.create(
            challan=challan, boq_category='Solar Modules', item_description='Modules',
            ordered_quantity=Decimal('10'), received_quantity=Decimal('10'),
            damaged_quantity=0, condition='Good', grn_date=date.today(),
            grn_confirmed_by=self.pm)
        inverter = DCLineItem.objects.create(
            challan=challan, boq_category='Inverter', item_description='Inverter',
            ordered_quantity=Decimal('1'))
        recalculate_dc_status(challan)
        self.assertEqual((self.bucket('DELIVERY_SOLAR_PANELS'),
                          self.bucket('DELIVERY_INVERTERS')), (Task.DONE, Task.IN_PROGRESS))

        lines = list(challan.line_items.order_by('pk'))
        response = self.client.post(
            reverse('delivery_challan_edit', args=[self.site.project_id, challan.pk]), {
                'revision_count': '0', 'line_ids': ','.join(str(l.pk) for l in lines),
                'editable_line_ids': str(inverter.pk), 'dc_number': 'OPX-1',
                'dc_date': date.today().isoformat(), 'reason': 'Wrong category typed',
                f'line_{lines[0].pk}_description': 'Modules',
                f'line_{lines[0].pk}_unit': 'Nos',
                f'line_{inverter.pk}_category': 'Solar Modules',
                f'line_{inverter.pk}_description': 'Inverter',
                f'line_{inverter.pk}_qty': '1', f'line_{inverter.pk}_unit': 'Nos',
            })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(DeliveryChallanRevision.objects.count(), 1)
        # The panels bucket now has an unconfirmed line; the inverters bucket has none.
        self.assertEqual((self.bucket('DELIVERY_SOLAR_PANELS'),
                          self.bucket('DELIVERY_INVERTERS')),
                         (Task.IN_PROGRESS, Task.NOT_STARTED))
