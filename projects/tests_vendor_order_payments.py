"""O2b — further payments, appended documents, and a site's order list.

What this file pins, and why each matters:

  * ONE COMMITMENT RULE. committed_total() — every payment not REJECTED — decides how
    much of an order is spoken for, on the raise page and the add-payment page alike.
    An ON_HOLD payment still holds its money; a REJECTED one frees it.
  * NEVER MORE THAN THE TOTAL. A payment above total − committed is refused and creates
    nothing; two simultaneous requests cannot together exceed the total (the Postgres
    test at the bottom — SQLite ignores select_for_update, so it is skipped there).
  * APPEND ONLY. Documents may be added at any time, even after everything is paid, and
    adding them never modifies an existing document row.
  * WHO. SCM alone adds payments and documents; the list admits whoever may read the
    site's orders, and costs the same queries for one order as for ten.

Run with:
    python manage.py test projects.tests_vendor_order_payments --settings=solarpms.test_settings
The concurrency test runs only under the real (Postgres) settings:
    python manage.py test projects.tests_vendor_order_payments.ConcurrentPaymentTests
"""
import threading
import time
import uuid
from decimal import Decimal
from unittest import skipUnless
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.db import connection
from django.test import Client, TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from .models import (
    ActivityLog, PaymentRequest, Project, StatusTransition, SUBJECT_PAYMENT_REQUEST, Vendor,
    VendorOrder, VendorOrderDocument, VendorOrderLine, VendorOrderSite,
    VENDOR_ORDER_DOC_INVOICE, VENDOR_ORDER_DOC_PO,
)
from .tests_vendor_order_raise import RaiseFixture, _client, _pdf, _profile, _project


def _make_order(fixture, total=Decimal('60000'), project=None, po='PO-B'):
    """An order recorded directly — one line of `total`, one PO document — so these
    tests do not depend on the raise page."""
    order = VendorOrder.objects.create(
        vendor=fixture.vendor, project_type='Residential', po_number=po,
        total_amount=total, created_by=fixture.scm, client_uuid=uuid.uuid4())
    VendorOrderSite.objects.create(order=order, project=project or fixture.project)
    VendorOrderLine.objects.create(order=order, item_description='Modules',
                                   quantity=Decimal('10'), amount=total)
    VendorOrderDocument.objects.create(order=order, doc_type=VENDOR_ORDER_DOC_PO,
                                       file_name='po.pdf', bucket='b', path=f'p/{po}.pdf',
                                       uploaded_by=fixture.scm)
    return order


def _pay(fixture, order, amount, status=PaymentRequest.APPROVED):
    reason = 'held' if status in (PaymentRequest.ON_HOLD, PaymentRequest.REJECTED) else ''
    # O4b: an approved or paid row carries its approved amount — in full, here.
    approved = status in (PaymentRequest.APPROVED, PaymentRequest.CONFIRMED)
    return PaymentRequest.objects.create(
        vendor_order=order, project=order.sites.get().project, vendor=fixture.vendor,
        amount=Decimal(amount), requested_by=fixture.scm.user, status=status,
        decision_reason=reason, approved_amount=Decimal(amount) if approved else None)


class PaymentFixture(RaiseFixture):

    def setUp(self):
        self.order = _make_order(self)
        _pay(self, self.order, '25000')   # 35,000 left to request

    def pay_url(self, order=None):
        return reverse('vendor_order_add_payment', args=[(order or self.order).pk])

    def post_payment(self, amount, profile=None, key=None, order=None):
        return _client(profile or self.scm).post(self.pay_url(order), {
            'client_uuid': key or str(uuid.uuid4()),
            'payment_amount': amount, 'payment_note': 'second tranche',
        })


class CommittedAmountTests(PaymentFixture):

    def test_every_status_but_rejected_is_committed(self):
        for status in (PaymentRequest.PENDING_APPROVAL, PaymentRequest.ON_HOLD,
                       PaymentRequest.REJECTED, PaymentRequest.CONFIRMED):
            _pay(self, self.order, '1000', status=status)
        # 25,000 approved + 1,000 each of pending, on hold, confirmed; rejected excluded.
        self.assertEqual(self.order.committed_amount, Decimal('28000'))
        self.assertEqual(self.order.available_to_request, Decimal('32000'))


class AddPaymentTests(PaymentFixture):

    def test_a_second_payment_within_the_balance_succeeds(self):
        response = self.post_payment('35000')
        self.assertRedirects(response, reverse('vendor_order_detail', args=[self.order.pk]),
                             fetch_redirect_response=False)
        pr = PaymentRequest.objects.latest('pk')
        self.assertEqual(pr.amount, Decimal('35000'))
        self.assertEqual(pr.vendor_order, self.order)
        self.assertEqual(pr.project, self.project)
        # O4: a further payment is raised PENDING_APPROVAL, as on both raise pages. Was
        # APPROVED until O4; the committed-balance arithmetic above is unaffected,
        # because committed_total() counts every status but REJECTED.
        self.assertEqual(pr.status, PaymentRequest.PENDING_APPROVAL)
        self.assertEqual(pr.requested_by, self.scm.user)
        transition = StatusTransition.objects.get(subject_type=SUBJECT_PAYMENT_REQUEST,
                                                  subject_id=pr.pk)
        self.assertEqual(transition.to_status, PaymentRequest.PENDING_APPROVAL)
        self.assertTrue(ActivityLog.objects.filter(
            action_code='payment_request_raised', entity_id=pr.pk).exists())

    def test_a_payment_above_the_balance_is_refused_and_creates_nothing(self):
        response = self.post_payment('35000.01')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(PaymentRequest.objects.count(), 1)
        self.assertEqual(StatusTransition.objects.filter(
            subject_type=SUBJECT_PAYMENT_REQUEST).count(), 0)
        self.assertFalse(ActivityLog.objects.filter(
            action_code='payment_request_raised').exists())

    def test_a_rejected_payment_frees_its_amount(self):
        PaymentRequest.objects.update(status=PaymentRequest.REJECTED, decision_reason='no')
        response = self.post_payment('60000')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(PaymentRequest.objects.count(), 2)

    def test_an_on_hold_payment_does_not_free_its_amount(self):
        PaymentRequest.objects.update(status=PaymentRequest.ON_HOLD, decision_reason='wait')
        response = self.post_payment('35000.01')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(PaymentRequest.objects.count(), 1)

    def test_the_same_client_uuid_twice_makes_one_payment(self):
        key = str(uuid.uuid4())
        first = self.post_payment('1000', key=key)
        second = self.post_payment('1000', key=key)
        self.assertEqual(first.status_code, 302)
        self.assertEqual(second['Location'], first['Location'])
        self.assertEqual(PaymentRequest.objects.filter(client_uuid=key).count(), 1)
        self.assertEqual(PaymentRequest.objects.count(), 2)

    def test_the_page_renders_the_available_balance(self):
        response = _client(self.scm).get(self.pay_url())
        self.assertContains(response, 'data-available="35000.00"')

    def test_zero_and_junk_amounts_are_refused(self):
        for amount in ('0', '-5', 'abc', ''):
            with self.subTest(amount=amount):
                self.assertEqual(self.post_payment(amount).status_code, 400)
        self.assertEqual(PaymentRequest.objects.count(), 1)

    def test_other_roles_are_refused(self):
        for profile in (self.finance, self.pm, self.admin):
            with self.subTest(role=profile.role):
                self.assertEqual(_client(profile).get(self.pay_url()).status_code, 403)
                self.assertEqual(self.post_payment('1000', profile=profile).status_code, 403)
        self.assertEqual(PaymentRequest.objects.count(), 1)

    def test_scm_is_refused_when_a_site_is_deleted(self):
        Project.objects.filter(pk=self.project.pk).update(is_deleted=True)
        self.assertEqual(self.post_payment('1000').status_code, 403)
        self.assertEqual(PaymentRequest.objects.count(), 1)


class RaiseUsesCommittedRuleTests(RaiseFixture):

    def test_the_first_payment_check_reads_committed_total(self):
        # A payment of 25,000 on a 60,000 order passes (RaiseTests proves it). Make the
        # one rule report 50,000 committed and the same submission must be refused.
        with patch('projects.order_views.committed_total', return_value=Decimal('50000')):
            response, storage = self.post()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(VendorOrder.objects.count(), 0)
        self.assertNothingUploaded(storage)


class AddDocumentsTests(PaymentFixture):

    def docs_url(self):
        return reverse('vendor_order_add_documents', args=[self.order.pk])

    def post_docs(self, profile=None, storage=None, **data):
        storage = storage or MagicMock()
        payload = {
            'doc_type_0': VENDOR_ORDER_DOC_INVOICE, 'doc_file_0': _pdf('final-invoice.pdf'),
            'doc_invoice_number_0': 'INV-9', 'doc_invoice_amount_0': '60000',
        }
        payload.update(data)
        payload = {k: v for k, v in payload.items() if v is not None}
        with patch('projects.supabase_storage.get_supabase_client', return_value=storage):
            response = _client(profile or self.scm).post(self.docs_url(), payload)
        return response, storage

    def _snapshot(self):
        return list(VendorOrderDocument.objects.order_by('pk').values())

    def test_append_after_every_payment_is_confirmed_adds_rows_and_changes_none(self):
        PaymentRequest.objects.update(status=PaymentRequest.CONFIRMED)
        _pay(self, self.order, '35000', status=PaymentRequest.CONFIRMED)
        self.assertEqual(self.order.balance, Decimal('0'))
        before = self._snapshot()

        response, storage = self.post_docs(**{
            'doc_type_1': 'other', 'doc_file_1': _pdf('delivery-note.pdf')})
        self.assertRedirects(response, reverse('vendor_order_detail', args=[self.order.pk]),
                             fetch_redirect_response=False)

        after = self._snapshot()
        self.assertEqual(after[:len(before)], before)          # nothing modified
        added = after[len(before):]
        self.assertEqual(len(added), 2)
        self.assertEqual({d['doc_type'] for d in added}, {VENDOR_ORDER_DOC_INVOICE, 'other'})
        for doc in added:
            self.assertTrue(doc['path'].startswith(f'vendor-orders/{self.order.client_uuid}/'))
        self.assertEqual(storage.storage.from_.return_value.upload.call_count, 2)
        self.assertEqual(self.order.invoiced, Decimal('60000'))
        self.assertTrue(ActivityLog.objects.filter(
            action_code='vendor_order_documents_added', entity_id=self.order.pk).exists())

    def test_an_invoice_without_its_number_is_refused_and_uploads_nothing(self):
        before = self._snapshot()
        response, storage = self.post_docs(doc_invoice_number_0='')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self._snapshot(), before)
        storage.storage.from_.return_value.upload.assert_not_called()

    def test_the_page_renders_five_slots_and_no_template_comment(self):
        response = _client(self.scm).get(self.docs_url())
        self.assertContains(response, 'name="doc_file_4"')
        self.assertNotContains(response, 'client-side twin')

    def test_no_file_is_refused(self):
        response, _ = self.post_docs(doc_file_0=None)
        self.assertEqual(response.status_code, 400)

    def test_a_failed_save_removes_the_uploaded_files(self):
        from django.db import DatabaseError
        before = self._snapshot()
        with patch('projects.order_views.VendorOrderDocument.objects.bulk_create',
                   side_effect=DatabaseError('boom')):
            response, storage = self.post_docs()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self._snapshot(), before)
        bucket = storage.storage.from_.return_value
        uploaded = [call.kwargs['path'] for call in bucket.upload.call_args_list]
        removed  = [call.args[0][0] for call in bucket.remove.call_args_list]
        self.assertEqual(len(uploaded), 1)
        self.assertEqual(removed, uploaded)

    def test_other_roles_are_refused(self):
        before = self._snapshot()
        for profile in (self.finance, self.pm, self.admin):
            with self.subTest(role=profile.role):
                response, storage = self.post_docs(profile=profile)
                self.assertEqual(response.status_code, 403)
                storage.storage.from_.return_value.upload.assert_not_called()
        self.assertEqual(self._snapshot(), before)


class DetailActionTests(PaymentFixture):

    def detail(self, profile):
        return _client(profile).get(reverse('vendor_order_detail', args=[self.order.pk]))

    def test_scm_sees_both_actions_and_the_way_back(self):
        response = self.detail(self.scm)
        self.assertContains(response, self.pay_url())
        self.assertContains(response,
                            reverse('vendor_order_add_documents', args=[self.order.pk]))
        self.assertContains(response, reverse('vendor_order_list', args=[self.project.pk]))

    def test_request_payment_is_hidden_when_nothing_is_left(self):
        _pay(self, self.order, '35000', status=PaymentRequest.ON_HOLD)
        response = self.detail(self.scm)
        self.assertNotContains(response, self.pay_url())
        self.assertContains(response,
                            reverse('vendor_order_add_documents', args=[self.order.pk]))

    def test_finance_sees_neither_action(self):
        response = self.detail(self.finance)
        self.assertNotContains(response, self.pay_url())
        self.assertNotContains(response,
                               reverse('vendor_order_add_documents', args=[self.order.pk]))


class ListTests(RaiseFixture):

    def list_url(self):
        return reverse('vendor_order_list', args=[self.project.pk])

    def test_scm_finance_and_the_sites_pm_can_read_it(self):
        _make_order(self)
        for profile in (self.scm, self.finance, self.pm):
            with self.subTest(role=profile.role):
                self.assertEqual(_client(profile).get(self.list_url()).status_code, 200)

    def test_another_pm_is_refused(self):
        self.assertEqual(_client(self.pm_b).get(self.list_url()).status_code, 403)

    def test_only_this_sites_orders_newest_first(self):
        older = _make_order(self, po='PO-OLD')
        newer = _make_order(self, po='PO-NEW')
        _make_order(self, project=self.other_project, po='PO-ELSEWHERE')
        content = _client(self.scm).get(self.list_url()).content.decode()
        self.assertNotIn('PO-ELSEWHERE', content)
        self.assertLess(content.index('PO-NEW'), content.index('PO-OLD'))
        self.assertIn(reverse('vendor_order_detail', args=[older.pk]), content)
        self.assertIn(reverse('vendor_order_detail', args=[newer.pk]), content)

    def _count(self):
        client = _client(self.pm)
        with CaptureQueriesContext(connection) as ctx:
            response = client.get(self.list_url())
        self.assertEqual(response.status_code, 200)
        return len(ctx.captured_queries)

    def test_query_count_is_flat_across_1_and_10_orders(self):
        order = _make_order(self, po='PO-0')
        _pay(self, order, '100', status=PaymentRequest.CONFIRMED)
        one = self._count()
        for i in range(1, 10):
            order = _make_order(self, po=f'PO-{i}')
            _pay(self, order, '100', status=PaymentRequest.CONFIRMED)
        ten = self._count()
        self.assertEqual(one, ten)

    def test_invoice_awaited_on_the_list_matches_the_detail_page(self):
        awaited = _make_order(self, po='PO-AWAIT')
        _pay(self, awaited, '5000', status=PaymentRequest.CONFIRMED)
        settled = _make_order(self, po='PO-SETTLED')
        _pay(self, settled, '5000', status=PaymentRequest.CONFIRMED)
        VendorOrderDocument.objects.create(
            order=settled, doc_type=VENDOR_ORDER_DOC_INVOICE, invoice_number='INV-1',
            invoice_amount=Decimal('5000'), file_name='i.pdf', bucket='b', path='p/i.pdf',
            uploaded_by=self.scm)

        client = _client(self.scm)
        rows = {row['order'].pk: row['invoice_awaited']
                for row in client.get(self.list_url()).context['rows']}
        for order in (awaited, settled):
            detail = client.get(reverse('vendor_order_detail', args=[order.pk]))
            with self.subTest(order=order.po_number):
                self.assertEqual(rows[order.pk], detail.context['invoice_awaited'])
        self.assertTrue(rows[awaited.pk])
        self.assertFalse(rows[settled.pk])


class DashboardOrdersLinkTests(RaiseFixture):

    def test_the_residential_card_links_to_the_list_with_its_count(self):
        _make_order(self)
        _make_order(self, po='PO-2')
        Project.objects.filter(pk__in=[self.project.pk, self.other_project.pk]).update(
            status='Active')
        response = _client(self.scm).get(reverse('dashboard_scm'))
        self.assertContains(response, reverse('vendor_order_list', args=[self.project.pk]))
        self.assertContains(response, 'Orders (2)')
        self.assertContains(response, 'Orders (0)')   # the other site, no orders


@skipUnless(connection.vendor == 'postgresql',
            'select_for_update is a no-op on SQLite; this race only exists on Postgres')
class ConcurrentPaymentTests(TransactionTestCase):
    """Two SCM requests for the whole remaining balance, at the same moment. The order
    row lock must make the second wait for the first and then see its payment.

    The balance read is slowed (a sleep inside committed_total) so that, WITHOUT the
    lock, both requests would read 0 committed before either wrote — the test then
    fails with two payments. With the lock, exactly one is created.
    """

    def test_two_simultaneous_requests_never_exceed_the_total(self):
        scm = _profile('o2b_race_scm', 'SCM')
        pm = _profile('o2b_race_pm', 'PM')
        vendor = Vendor.objects.create(name='Race Vendor', contact_person='R',
                                       phone='9000000009')
        project = _project('Race Site', pm)
        order = VendorOrder.objects.create(vendor=vendor, project_type='Residential',
                                           po_number='PO-RACE', created_by=scm,
                                           total_amount=Decimal('1000'))
        VendorOrderSite.objects.create(order=order, project=project)
        VendorOrderLine.objects.create(order=order, item_description='Modules',
                                       quantity=Decimal('1'), amount=Decimal('1000'))
        url = reverse('vendor_order_add_payment', args=[order.pk])

        from . import models as m
        real = m.committed_total

        def slow(payments):
            result = real(payments)
            time.sleep(0.5)
            return result

        barrier, statuses = threading.Barrier(2), []

        def request():
            try:
                client = _client(scm)
                barrier.wait()
                statuses.append(client.post(url, {
                    'client_uuid': str(uuid.uuid4()), 'payment_amount': '1000',
                }).status_code)
            finally:
                connection.close()

        with patch('projects.models.committed_total', side_effect=slow):
            threads = [threading.Thread(target=request) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        self.assertEqual(sorted(statuses), [302, 400])
        self.assertEqual(PaymentRequest.objects.filter(vendor_order=order).count(), 1)
