"""Payments 5a — a payment may belong to a contractor bill; every reader is bill-safe.

What this file pins, and why each matters:

  * THE DATABASE HOLDS "ORDER OR BILL, NEVER BOTH, NEVER NEITHER"
    (payment_request_order_xor_bill, migration 0109).
  * A BILL'S PAYMENT IS FILED UNDER ITS BILL SITE'S PROJECT TYPE — the same tab, tile and
    count a PO/PI payment of that type is in (payments.payment_type_q). The CAPEX tab is
    drawn for a CAPEX bill's payment even with no CAPEX order (ruling Q1).
  * EVERY FINANCE PAGE RENDERS WITH ONE PRESENT and shows the bill — site code, contractor,
    bill number — where an order row shows its order, linking to the bill's approval page.
    Finance following that link opens the bill since 5b (D-A58); 5a pinned the 403 so 5b
    changed it on purpose.
  * APPROVE, HOLD, REJECT AND ANSWER ARRIVED IN 5b: each acts on a bill's payment and
    returns to the bill's payments section (5a refused them). tests_bill_payment_forward
    pins the rest of 5b.
  * NOTICES on a bill's payment link to the bill; a link that cannot be built is logged
    and the notice still goes out without it — for PO/PI payments too.
  * TEST SITES: payment_counts() still counts them for bills as for orders (audit I);
    only the CEO strip drops a bill whose site is test data (ruling Q2).
  * PO/PI PAGES COST WHAT THEY DID: each count below was measured on HEAD ced1991, before
    5a, with the same fixture.

No writer of a bill's payment exists until 5b, so bills and their payments are written
here directly.

Run with:
    python manage.py test projects.tests_bill_payment_readers --settings=solarpms.test_settings
"""
import uuid
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.db import IntegrityError, connection, transaction
from django.db.models import ProtectedError
from django.test.utils import CaptureQueriesContext
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

from .models import (
    APPROVAL_APPROVED, APPROVAL_KIND_CONTRACTOR_BILL, VENDOR_KIND_CONTRACTOR,
    ApprovalRequest, ContractorBillDetail, Notification, PaymentRequest,
    PaymentRequestHold, Project, StatusTransition, SUBJECT_PAYMENT_REQUEST, Vendor,
)
from .payments import ceo_payment_strip, payment_counts, send_payment_notices
from .tests_payment_queue import QueueFixture, _messages
from .tests_vendor_order_raise import RaiseFixture, _client, _project
from .utils import record_transition


def _make_bill(fixture, site, number, amount='30000'):
    """An APPROVED contractor bill on `site`, written directly (no storage, no steps):
    the readers under test read the bill row, never how it was approved."""
    approval = ApprovalRequest.objects.create(
        kind=APPROVAL_KIND_CONTRACTOR_BILL, status=APPROVAL_APPROVED,
        closed_at=timezone.now(), title=f'Civil works {number}', description='Foundations.',
        vendor=fixture.contractor, raised_by=fixture.scm)
    return ContractorBillDetail.objects.create(
        request=approval, project=site, amount=Decimal(amount), bill_number=number,
        bill_date=timezone.localdate() - timedelta(days=5), pdf_file_name=f'{number}.pdf',
        pdf_bucket='bills-private', pdf_path=f'{site.pk}/{number}.pdf', pdf_size_kb=80)


def _make_bill_payment(fixture, bill, *, amount='10000',
                       status=PaymentRequest.PENDING_APPROVAL, approved_by=None):
    """A payment on `bill`, as 5b's writer is ruled to write it (Q5): vendor = the bill's
    contractor, project = the bill's site."""
    approved = status in (PaymentRequest.APPROVED, PaymentRequest.CONFIRMED)
    return PaymentRequest.objects.create(
        contractor_bill=bill, project=bill.project, vendor=fixture.contractor,
        amount=Decimal(amount), requested_by=fixture.scm.user, status=status,
        approved_by=approved_by, approved_at=timezone.now() if approved_by else None,
        approved_amount=Decimal(amount) if approved else None)


class BillPaymentFixture(QueueFixture):
    """QueueFixture's four PO/PI payments (one per tab and shape), plus contractor bills on
    a Residential, an OPEX and a CAPEX site, each with one payment."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.contractor = Vendor.objects.create(
            name='Civil Works Co', contact_person='C', phone='9000000077',
            kind=VENDOR_KIND_CONTRACTOR)
        cls.res_bill_site = _project('Bill Home', cls.pm)
        cls.opex_bill_site = _project('Bill Rooftop', cls.pm, project_type='OPEX',
                                      program=cls.tender)
        cls.capex_bill_site = _project('Bill Plant', cls.pm, project_type='CAPEX')

    def setUp(self):
        super().setUp()
        self.res_bill = _make_bill(self, self.res_bill_site, 'CB-7001')
        self.res_bill_pay = _make_bill_payment(self, self.res_bill)
        self.opex_bill = _make_bill(self, self.opex_bill_site, 'CB-7002')
        self.opex_bill_pay = _make_bill_payment(self, self.opex_bill,
                                                status=PaymentRequest.APPROVED,
                                                approved_by=self.approver)
        self.capex_bill = _make_bill(self, self.capex_bill_site, 'CB-7003')
        self.capex_bill_pay = _make_bill_payment(self, self.capex_bill)

    def bill_url(self, bill):
        return reverse('approval_detail', args=[bill.request_id])


# ---------------------------------------------------------------------------
# The model
# ---------------------------------------------------------------------------

class ModelTests(BillPaymentFixture):

    def refused(self, **links):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                PaymentRequest.objects.create(
                    vendor=self.contractor, amount=Decimal('5'),
                    requested_by=self.scm.user, **links)

    def test_a_payment_with_neither_an_order_nor_a_bill_is_refused(self):
        self.refused()

    def test_a_payment_with_both_an_order_and_a_bill_is_refused(self):
        self.refused(vendor_order=self.res_order, contractor_bill=self.res_bill)

    def test_a_bill_payment_names_its_site_and_its_bill(self):
        self.assertEqual(self.res_bill_pay.scope_label, self.res_bill_site.project_id)
        anchorless = PaymentRequest(pk=999, contractor_bill=self.res_bill,
                                    vendor=self.contractor, amount=Decimal('1'))
        self.assertIn(f'bill #{self.res_bill.pk}', str(anchorless))

    def test_a_bill_with_a_payment_cannot_be_deleted(self):
        with self.assertRaises(ProtectedError):
            with transaction.atomic():
                self.res_bill.delete()


# ---------------------------------------------------------------------------
# Tabs, tiles and counts
# ---------------------------------------------------------------------------

class TabTests(BillPaymentFixture):

    def test_each_bill_payment_is_on_its_bill_sites_tab_only(self):
        expected = {'Residential': {self.res_pay.pk, self.res_bill_pay.pk},
                    'OPEX': {self.opex_pay.pk, self.central_pay.pk, self.opex_bill_pay.pk},
                    'CAPEX': {self.capex_pay.pk, self.capex_bill_pay.pk}}
        for tab, pks in expected.items():
            with self.subTest(tab=tab):
                self.assertEqual(self.listed(self.queue(tab=tab)), pks)

    def test_the_tiles_and_counts_include_the_bill_payment(self):
        counts = payment_counts(['Residential'])
        self.assertEqual(counts[PaymentRequest.PENDING_APPROVAL].count, 2)
        self.assertEqual(counts[PaymentRequest.PENDING_APPROVAL].amount, Decimal('30000'))
        tabs = {t['value']: t for t in self.queue(tab='Residential').context['tabs']}
        self.assertEqual(tabs['Residential']['to_approve'], 2)
        self.assertEqual(tabs['OPEX']['to_pay'], 2)   # an order's and a bill's
        self.assertEqual(payment_counts()[PaymentRequest.PENDING_APPROVAL].count, 4)

    def test_the_finance_and_ceo_tiles_count_the_bill_payment(self):
        finance = _client(self.finance).get(reverse('dashboard_finance'),
                                            {'context': 'tenders'})
        self.assertEqual(finance.context['total_payment_requests'], 2)
        ceo = _client(self.ceo).get(reverse('dashboard_ceo'), {'context': 'residential'})
        self.assertEqual(ceo.context['fin_payment_requests_pending'], 0)
        ceo_all = _client(self.ceo).get(reverse('dashboard_ceo'))
        self.assertEqual(ceo_all.context['fin_payment_requests_pending'], 2)


class CapexTabTests(RaiseFixture):
    """No CAPEX order at all: the tab appears for a CAPEX bill's payment alone (Q1)."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.contractor = Vendor.objects.create(
            name='Civil Works Co', contact_person='C', phone='9000000077',
            kind=VENDOR_KIND_CONTRACTOR)
        cls.capex_site = _project('Bill Plant', cls.pm, project_type='CAPEX')

    def tabs(self):
        response = _client(self.finance).get(reverse('payment_queue'), {'tab': 'CAPEX'})
        return [t['value'] for t in response.context['tabs']], response

    def test_no_capex_order_and_no_capex_bill_payment_hides_the_tab(self):
        _make_bill(self, self.capex_site, 'CB-9')    # a bill with no payment
        self.assertEqual(self.tabs()[0], ['Residential', 'OPEX'])

    def test_a_capex_bill_payment_draws_the_tab_and_is_listed_on_it(self):
        pay = _make_bill_payment(self, _make_bill(self, self.capex_site, 'CB-9'))
        values, response = self.tabs()
        self.assertEqual(values, ['Residential', 'OPEX', 'CAPEX'])
        self.assertEqual({row['payment'].pk for row in response.context['rows']}, {pay.pk})


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

class SearchTests(BillPaymentFixture):

    def test_the_queue_finds_a_bill_payment_by_bill_site_tender_and_contractor(self):
        cases = [('Residential', 'CB-7001', {self.res_bill_pay.pk}),
                 ('Residential', self.res_bill_site.project_id, {self.res_bill_pay.pk}),
                 ('OPEX', 'Delhi Rooftop', {self.opex_pay.pk, self.opex_bill_pay.pk}),
                 ('CAPEX', 'Civil Works', {self.capex_bill_pay.pk}),
                 ('Residential', 'PO-RES-1', {self.res_pay.pk})]
        for tab, text, pks in cases:
            with self.subTest(q=text):
                self.assertEqual(self.listed(self.queue(tab=tab, q=text)), pks)


# ---------------------------------------------------------------------------
# Every Finance page renders with one present, and shows the bill
# ---------------------------------------------------------------------------

class PageTests(BillPaymentFixture):

    def test_every_queue_tab_and_chip_renders_and_shows_the_bill(self):
        for tab, bill in (('Residential', self.res_bill), ('OPEX', self.opex_bill),
                          ('CAPEX', self.capex_bill)):
            for status in ('', 'pending_approval', 'approved', 'on_hold', 'confirmed',
                           'rejected'):
                with self.subTest(tab=tab, status=status):
                    self.assertEqual(self.queue(tab=tab, status=status).status_code, 200)
            with self.subTest(tab=tab, shows='bill'):
                page = self.queue(tab=tab)
                self.assertContains(page, f'Bill {bill.bill_number} · {bill.project.project_id}')
                self.assertContains(page, 'Civil Works Co')
                self.assertContains(page, f'href="{self.bill_url(bill)}"')

    def test_a_bill_row_shows_its_money_line_and_the_approver_buttons(self):
        row = next(r for r in self.queue(self.approver, tab='Residential').context['rows']
                   if r['payment'].pk == self.res_bill_pay.pk)
        self.assertIsNone(row['order'])
        self.assertEqual(row['money'], {'total': Decimal('30000'), 'paid': Decimal('0'),
                                        'committed': Decimal('10000')})
        # 5b: a pending bill payment is approved or held from the queue like any other.
        self.assertEqual((row['can_approve'], row['can_hold'], row['can_reject']),
                         (True, True, False))

    def test_the_finance_dashboard_names_the_bill(self):
        for context in ('', 'residential', 'tenders'):
            with self.subTest(context=context):
                page = _client(self.finance).get(reverse('dashboard_finance'),
                                                  {'context': context} if context else {})
                self.assertEqual(page.status_code, 200)
        page = _client(self.finance).get(reverse('dashboard_finance'))
        self.assertContains(page, 'Bill CB-7002')     # the approved one, on its site's card

    def test_the_ceo_dashboard_renders_in_every_context(self):
        for context in ('', 'residential', 'tenders'):
            with self.subTest(context=context):
                page = _client(self.ceo).get(reverse('dashboard_ceo'),
                                             {'context': context} if context else {})
                self.assertEqual(page.status_code, 200)

    def test_my_documents_names_and_opens_the_bill(self):
        page = _client(self.scm).get(reverse('my_documents'))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'Bill CB-7001')
        self.assertContains(page, f'href="{self.bill_url(self.res_bill)}"')

    def test_the_old_payment_link_redirects_to_the_bill(self):
        response = _client(self.scm).get(reverse(
            'payment_request_detail', args=[self.res_bill_site.project_id,
                                            self.res_bill_pay.pk]))
        self.assertRedirects(response, self.bill_url(self.res_bill),
                             fetch_redirect_response=False)

    def test_finance_following_the_bill_link_opens_the_bill(self):
        # D-A58, 5b: a payment points at the bill, so Finance may open it (5a: 403).
        response = _client(self.finance).get(self.bill_url(self.res_bill))
        self.assertEqual(response.status_code, 200)

    def test_mark_paid_on_a_bill_payment_returns_to_its_sites_tab(self):
        response = self.mark_paid(self.fin_b, self.opex_bill_pay)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], f"{reverse('payment_queue')}?tab=OPEX")
        self.opex_bill_pay.refresh_from_db()
        self.assertEqual(self.opex_bill_pay.status, PaymentRequest.CONFIRMED)


# ---------------------------------------------------------------------------
# Approve / hold / reject / answer act on a bill's payment since 5b (5a refused them)
# ---------------------------------------------------------------------------

class BillPaymentActionTests(BillPaymentFixture):
    """5a's four refusal scenarios (ActionRefusalTests), turned round by 5b: each action
    now happens and returns to the bill's payments section, or to the queue view it came
    from."""

    def snapshot(self, payment):
        payment.refresh_from_db()
        return (payment.status, payment.approved_amount, payment.approved_by_id,
                PaymentRequestHold.objects.filter(payment_request=payment).count(),
                StatusTransition.objects.filter(subject_type=SUBJECT_PAYMENT_REQUEST,
                                                subject_id=payment.pk).count())

    def assertActed(self, profile, url, data, payment, location, status):
        response = _client(profile).post(url, data)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], location)
        self.assertNotIn('not available yet', ' '.join(_messages(response)))
        payment.refresh_from_db()
        self.assertEqual(payment.status, status)

    def test_approver_actions_act_and_go_back_to_the_bill(self):
        pay, bill = self.res_bill_pay, f'{self.bill_url(self.res_bill)}#payments'
        for name, data, status in (
                ('payment_hold', {'reason': 'Check'}, PaymentRequest.ON_HOLD),
                ('payment_reject', {'reason': 'No'}, PaymentRequest.REJECTED)):
            with self.subTest(view=name):
                self.assertActed(self.approver, reverse(name, args=[pay.pk]), data, pay,
                                 bill, status)
        pay = self.capex_bill_pay
        with self.subTest(view='payment_approve'):
            self.assertActed(self.approver, reverse('payment_approve', args=[pay.pk]),
                             {'approved_amount': '10000'}, pay,
                             f'{self.bill_url(self.capex_bill)}#payments',
                             PaymentRequest.APPROVED)

    def test_the_queue_action_comes_back_to_the_queue_view_it_came_from(self):
        here = f"{reverse('payment_queue')}?tab=OPEX&status=approved"
        self.assertActed(
            self.approver,
            reverse('payment_queue_action', args=[self.opex_bill_pay.pk, 'hold']),
            {'reason': 'Check', 'next': here}, self.opex_bill_pay, here,
            PaymentRequest.ON_HOLD)

    def test_scm_answering_a_hold_goes_to_the_bill(self):
        pay = self.res_bill_pay
        PaymentRequest.objects.filter(pk=pay.pk).update(status=PaymentRequest.ON_HOLD)
        PaymentRequestHold.objects.create(payment_request=pay, reason='Why',
                                          held_by=self.approver)
        self.assertActed(self.scm, reverse('payment_hold_respond', args=[pay.pk]),
                         {'response': 'Because'}, pay,
                         f'{self.bill_url(self.res_bill)}#payments',
                         PaymentRequest.PENDING_APPROVAL)

    def test_a_get_goes_to_the_bill_and_writes_nothing(self):
        before = self.snapshot(self.capex_bill_pay)
        response = _client(self.approver).get(
            reverse('payment_approve', args=[self.capex_bill_pay.pk]))
        self.assertEqual(response['Location'], f'{self.bill_url(self.capex_bill)}#payments')
        self.assertEqual(self.snapshot(self.capex_bill_pay), before)


# ---------------------------------------------------------------------------
# Notices
# ---------------------------------------------------------------------------

class NoticeTests(BillPaymentFixture):

    def move(self, payment, to_status, from_status, actor, remark=''):
        """A ledger row for `payment`, as a 5b action will write it; the StatusTransition
        receiver sends the notices after commit."""
        with self.captureOnCommitCallbacks(execute=True):
            with transaction.atomic():
                record_transition(payment, to_status=to_status, from_status=from_status,
                                  actor=actor, remark=remark, project=payment.project)

    def test_every_notifying_move_on_a_bill_payment_links_to_the_bill(self):
        pay, link = self.res_bill_pay, self.bill_url(self.res_bill)
        moves = [(PaymentRequest.PENDING_APPROVAL, '', self.scm),              # raise
                 (PaymentRequest.ON_HOLD, PaymentRequest.PENDING_APPROVAL, self.approver),
                 (PaymentRequest.PENDING_APPROVAL, PaymentRequest.ON_HOLD, self.scm)]
        for to_status, from_status, actor in moves:
            with self.subTest(to=to_status, frm=from_status):
                Notification.objects.all().delete()
                self.move(pay, to_status, from_status, actor, remark='r')
                links = set(Notification.objects.values_list('link', flat=True))
                self.assertTrue(links)
                self.assertEqual(links, {link})

    def test_paid_and_partial_notices_link_to_the_bill(self):
        pay = self.opex_bill_pay
        PaymentRequest.objects.filter(pk=pay.pk).update(approved_amount=Decimal('4000'))
        self.move(pay, PaymentRequest.APPROVED, PaymentRequest.PENDING_APPROVAL,
                  self.approver, remark='approved ₹4000 of ₹10000: part')
        PaymentRequest.objects.filter(pk=pay.pk).update(
            status=PaymentRequest.CONFIRMED, payment_date=timezone.localdate(),
            payment_reference='UTR-1')
        self.move(pay, PaymentRequest.CONFIRMED, PaymentRequest.APPROVED, self.fin_b,
                  remark='UTR-1')
        self.assertEqual(Notification.objects.count(), 2)
        self.assertEqual(set(Notification.objects.values_list('link', flat=True)),
                         {self.bill_url(self.opex_bill)})

    def test_a_po_pi_notice_still_carries_its_order_link(self):
        self.move(self.res_pay, PaymentRequest.ON_HOLD, PaymentRequest.PENDING_APPROVAL,
                  self.approver, remark='GRN missing')
        notice = Notification.objects.get()
        self.assertEqual(notice.link, reverse('vendor_order_detail', args=[self.res_order.pk]))

    def test_a_link_that_fails_is_logged_and_the_notice_still_goes(self):
        for payment in (self.res_pay, self.res_bill_pay):
            with self.subTest(payment=payment.pk):
                Notification.objects.all().delete()
                row = StatusTransition(subject_type=SUBJECT_PAYMENT_REQUEST,
                                       subject_id=payment.pk, to_status='on_hold',
                                       from_status='pending_approval', actor=self.approver,
                                       remark='why')
                with mock.patch('projects.payments.reverse',
                                side_effect=NoReverseMatch('boom')), \
                        self.assertLogs('projects.payments', 'ERROR') as logs:
                    send_payment_notices(row)
                self.assertIn('no link for payment', logs.output[0])
                notice = Notification.objects.get()
                self.assertEqual((notice.recipient_id, notice.link), (self.scm.pk, ''))


# ---------------------------------------------------------------------------
# Test sites
# ---------------------------------------------------------------------------

class TestSiteTests(BillPaymentFixture):

    def setUp(self):
        super().setUp()
        Project.objects.filter(pk=self.opex_bill_site.pk).update(is_test=True)

    def test_payment_counts_still_counts_a_bill_on_a_test_site(self):
        # Audit I: payment_counts() excludes no test site, for orders or bills.
        self.assertEqual(payment_counts(['OPEX'])[PaymentRequest.APPROVED].count, 2)

    def test_the_ceo_strip_drops_a_bill_on_a_test_site_and_keeps_a_real_one(self):
        today = timezone.localdate()
        # OPEX: the order's approved payment stays, the test-site bill's goes (Q2).
        self.assertEqual(ceo_payment_strip(['OPEX'], today)['approved'].count, 1)
        # CAPEX: the real-site bill's payment is kept beside the site-less order's.
        self.assertEqual(ceo_payment_strip(['CAPEX'], today)['awaiting'].count, 2)


# ---------------------------------------------------------------------------
# PO/PI pages cost what they did
# ---------------------------------------------------------------------------

class PoPiQueryCountTests(QueueFixture):
    """QueueFixture's PO/PI payments only. Every figure was measured on HEAD ced1991,
    before 5a, with this fixture and a warmed client (the first request's session and
    cache reads excluded)."""

    MEASURED_ON_HEAD = [
        ('payment_queue',     'finance', {'tab': 'Residential'}, 17),
        ('payment_queue',     'finance', {'tab': 'OPEX'}, 17),
        ('payment_queue',     'finance', {'tab': 'CAPEX'}, 17),
        ('payment_queue',     'finance', {'tab': 'OPEX', 'q': 'PO'}, 17),
        ('dashboard_finance', 'finance', {}, 11),
        ('dashboard_finance', 'finance', {'context': 'residential'}, 11),
        ('dashboard_ceo',     'ceo',     {}, 14),
        ('dashboard_ceo',     'ceo',     {'context': 'residential'}, 14),
        ('dashboard_ceo',     'ceo',     {'context': 'tenders'}, 35),
        ('my_documents',      'scm',     {}, 10),
    ]

    def count(self, client, url, params):
        client.get(url, params)
        with CaptureQueriesContext(connection) as ctx:
            client.get(url, params)
        return len(ctx.captured_queries)

    def test_each_page_costs_what_it_did_before_5a(self):
        for name, who, params, expected in self.MEASURED_ON_HEAD:
            with self.subTest(page=name, params=params):
                client = _client(getattr(self, who))
                self.assertEqual(self.count(client, reverse(name), params), expected)

    def test_the_old_payment_link_costs_what_it_did(self):
        url = reverse('payment_request_detail', args=[self.project.project_id,
                                                      self.res_pay.pk])
        self.assertEqual(self.count(_client(self.finance), url, {}), 3)

    def test_a_notice_costs_what_it_did(self):
        row = StatusTransition(subject_type=SUBJECT_PAYMENT_REQUEST,
                               subject_id=self.central_pay.pk, to_status='on_hold',
                               from_status='pending_approval', actor=self.approver,
                               remark='why')
        with CaptureQueriesContext(connection) as ctx:
            send_payment_notices(row)
        self.assertEqual(len(ctx.captured_queries), 7)
