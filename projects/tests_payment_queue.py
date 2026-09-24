"""O5 — the Finance payments queue, mark-paid, and the in-app payment notices.

What this file pins, and why each matters:

  * THE TAB IS THE ORDER'S PROJECT TYPE. A RESCO payment never appears under Residential,
    and a payment is listed whether or not it has a site — `PaymentRequest.project` is a
    nullable display anchor, and reading scope through it is the defect §25 recorded.
    Draft-status OPEX sites are listed too: nothing here filters on a project's status.
  * THE APPROVER IS NOT THE PAYER. The person who approved a request, and the person who
    raised it, cannot mark it paid; another Finance user can.
  * MARK-PAID IS ONE WRITER. The queue's form goes through payments.mark_payment_paid()
    (the project page's confirm did too, until O6 deleted it): one set of refusals, one
    ledger row.
  * AN ACTION FROM THE QUEUE COMES BACK TO THE QUEUE, same tab and filter, although the
    O4 views it reaches are unchanged and still redirect to the order on their own.
  * FOUR MOVES NOTIFY, IN-APP ONLY, NEVER THE ACTOR.
  * THE PAGE IS FLAT in the number of rows.

Run with:
    python manage.py test projects.tests_payment_queue --settings=solarpms.test_settings
The concurrency test runs only under the real (Postgres) settings:
    python manage.py test projects.tests_payment_queue.ConcurrentMarkPaidTests
"""
import threading
import uuid
from datetime import timedelta
from decimal import Decimal
from unittest import skipUnless

from django.contrib.messages import get_messages
from django.db import connection
from django.test import TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from .models import (
    Notification, NotificationLog, PaymentRequest, PaymentRequestHold, Program, Project,
    StatusTransition, SUBJECT_PAYMENT_REQUEST, Vendor, VendorOrder, VendorOrderDocument,
    VendorOrderLine, VendorOrderProgram, VendorOrderSite,
)
from .tests_payment_approval import _approver
from .tests_vendor_order_raise import RaiseFixture, _client, _profile, _project


def _make_order(fixture, project_type, *, site=None, program=None, po='', pi='',
                total=Decimal('90000'), vendor=None):
    order = VendorOrder.objects.create(
        vendor=vendor or fixture.vendor, project_type=project_type, po_number=po,
        pi_number=pi, total_amount=total, created_by=fixture.scm,
        client_uuid=uuid.uuid4())
    if site is not None:
        VendorOrderSite.objects.create(order=order, project=site)
    if program is not None:
        VendorOrderProgram.objects.create(order=order, program=program)
    VendorOrderLine.objects.create(order=order, item_description='Modules',
                                   quantity=Decimal('10'), amount=total)
    return order


def _make_payment(fixture, order, *, amount='20000', status=PaymentRequest.PENDING_APPROVAL,
                  requested_by=None, approved_by=None, project='anchor'):
    if project == 'anchor':
        site = order.sites.first()
        project = site.project if site else None
    # O4b: an approved or paid row carries its approved amount — in full, here.
    approved = status in (PaymentRequest.APPROVED, PaymentRequest.CONFIRMED)
    return PaymentRequest.objects.create(
        vendor_order=order, project=project, vendor=order.vendor,
        amount=Decimal(amount), requested_by=requested_by or fixture.scm.user,
        status=status, approved_by=approved_by,
        approved_at=timezone.now() if approved_by else None,
        approved_amount=Decimal(amount) if approved else None)


def _messages(response):
    return [str(m) for m in get_messages(response.wsgi_request)]


class QueueFixture(RaiseFixture):
    """One payment per tab and shape: a Residential pending one, an approved one on a
    DRAFT OPEX site, a SITE-LESS OPEX one on hold, and a site-less CAPEX one pending."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.approver   = _approver('o5_approver')            # Finance, holds the flag
        cls.approver_b = _approver('o5_approver_b', role='CEO')
        cls.fin_b      = _profile('o5_fin_b', 'Finance')      # no flag
        cls.ceo        = _profile('o5_ceo', 'CEO')
        cls.design     = _profile('o5_design', 'Design')
        cls.tender     = Program.objects.create(
            program_type='OPEX', name='Delhi Rooftop Tender', client_name='IPGCL',
            short_tender_code='DRT26', status='Active')
        cls.central_tender = Program.objects.create(
            program_type='OPEX', name='Central Stock Tender', client_name='NDMC',
            short_tender_code='CST26', status='Active')
        cls.opex_site = _project('Opex Draft Site', cls.pm, project_type='OPEX',
                                 program=cls.tender)
        Project.objects.filter(pk=cls.opex_site.pk).update(status='Draft')
        cls.opex_site.refresh_from_db()
        cls.vendor_b = Vendor.objects.create(name='Kiran Cables', contact_person='K',
                                             phone='9000000009')

    def setUp(self):
        self.res_order = _make_order(self, 'Residential', site=self.project, po='PO-RES-1')
        self.res_pay = _make_payment(self, self.res_order)

        self.opex_order = _make_order(self, 'OPEX', site=self.opex_site, po='PO-OPX-7',
                                      vendor=self.vendor_b)
        self.opex_pay = _make_payment(self, self.opex_order, status=PaymentRequest.APPROVED,
                                      approved_by=self.approver)

        self.central_order = _make_order(self, 'OPEX', program=self.central_tender,
                                         pi='PI-CENTRAL')
        self.central_pay = _make_payment(self, self.central_order,
                                         status=PaymentRequest.ON_HOLD)
        PaymentRequestHold.objects.create(payment_request=self.central_pay,
                                          reason='Rate above tender', held_by=self.approver)

        self.capex_order = _make_order(self, 'CAPEX', po='PO-CAP-3')
        self.capex_pay = _make_payment(self, self.capex_order)

    # -- helpers ------------------------------------------------------------
    def queue(self, profile=None, **params):
        return _client(profile or self.finance).get(reverse('payment_queue'), params)

    def listed(self, response):
        return {row['payment'].pk for row in response.context['rows']}

    def mark_paid(self, profile, payment, date=None, reference='UTR-555', next_url=None):
        data = {'payment_date': (date or timezone.localdate()).isoformat(),
                'payment_reference': reference}
        if next_url:
            data['next'] = next_url
        return _client(profile).post(reverse('payment_mark_paid', args=[payment.pk]), data)


# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------

class TabTests(QueueFixture):

    def test_each_tab_shows_only_its_project_type(self):
        self.assertEqual(self.listed(self.queue(tab='Residential')), {self.res_pay.pk})
        self.assertEqual(self.listed(self.queue(tab='OPEX')),
                         {self.opex_pay.pk, self.central_pay.pk})
        self.assertEqual(self.listed(self.queue(tab='CAPEX')), {self.capex_pay.pk})

    def test_a_resco_payment_never_appears_under_residential(self):
        residential = self.listed(self.queue(tab='Residential'))
        self.assertNotIn(self.opex_pay.pk, residential)
        self.assertNotIn(self.central_pay.pk, residential)

    def test_the_default_tab_is_residential(self):
        self.assertEqual(self.queue().context['tab'], 'Residential')

    def test_a_siteless_payment_appears_in_its_tab(self):
        self.assertIsNone(self.central_pay.project)
        self.assertIn(self.central_pay.pk, self.listed(self.queue(tab='OPEX')))
        self.assertIn(self.capex_pay.pk, self.listed(self.queue(tab='CAPEX')))

    def test_a_payment_on_a_draft_opex_site_appears(self):
        self.assertEqual(self.opex_site.status, 'Draft')
        self.assertIn(self.opex_pay.pk, self.listed(self.queue(tab='OPEX')))

    def test_tab_labels_counts_match_the_rows(self):
        response = self.queue(tab='OPEX')
        for tab in response.context['tabs']:
            with self.subTest(tab=tab['value']):
                rows = self.queue(tab=tab['value']).context['rows']
                statuses = [row['payment'].status for row in rows]
                self.assertEqual(tab['to_approve'],
                                 statuses.count(PaymentRequest.PENDING_APPROVAL))
                self.assertEqual(tab['to_pay'], statuses.count(PaymentRequest.APPROVED))
        text = response.content.decode()
        self.assertIn('RESCO · 0 to approve · 1 to pay', text)
        self.assertIn('Residential · 1 to approve · 0 to pay', text)
        self.assertIn('CAPEX · 1 to approve · 0 to pay', text)

    def test_the_tiles_count_and_sum_this_tab(self):
        tiles = self.queue(tab='OPEX').context['tiles']
        self.assertEqual(tiles['to_pay'], (1, Decimal('20000')))
        self.assertEqual(tiles['on_hold'], (1, Decimal('20000')))
        self.assertEqual(tiles['awaiting'][0], 0)

    def test_invoice_awaited_counts_orders_paid_beyond_their_invoices(self):
        self.assertEqual(self.queue(tab='OPEX').context['tiles']['invoice_awaited_orders'], 0)
        _make_payment(self, self.opex_order, amount='5000', status=PaymentRequest.CONFIRMED)
        VendorOrderDocument.objects.create(
            order=self.opex_order, doc_type='invoice', invoice_number='INV-1',
            invoice_amount=Decimal('1000'), file_name='i.pdf', bucket='b', path='p/i.pdf',
            uploaded_by=self.scm)
        response = self.queue(tab='OPEX')
        self.assertEqual(response.context['tiles']['invoice_awaited_orders'], 1)
        self.assertContains(response, 'Invoice awaited')

    def test_status_chip_filters_rows(self):
        self.assertEqual(self.listed(self.queue(tab='OPEX', status='on_hold')),
                         {self.central_pay.pk})
        self.assertEqual(self.listed(self.queue(tab='OPEX', status='approved')),
                         {self.opex_pay.pk})

    def test_a_held_row_shows_its_reason_and_who_held_it(self):
        response = self.queue(tab='OPEX')
        self.assertContains(response, 'Rate above tender')
        self.assertContains(response, 'Awaiting a response from SCM.')


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

class SearchTests(QueueFixture):

    def test_search_by_po(self):
        self.assertEqual(self.listed(self.queue(tab='OPEX', q='OPX-7')), {self.opex_pay.pk})

    def test_search_by_pi(self):
        self.assertEqual(self.listed(self.queue(tab='OPEX', q='central')),
                         {self.central_pay.pk})

    def test_search_by_vendor(self):
        self.assertEqual(self.listed(self.queue(tab='OPEX', q='kiran')), {self.opex_pay.pk})

    def test_search_by_tender_named_on_a_siteless_order(self):
        self.assertEqual(self.listed(self.queue(tab='OPEX', q='Central Stock')),
                         {self.central_pay.pk})

    def test_search_by_tender_of_a_site(self):
        self.assertEqual(self.listed(self.queue(tab='OPEX', q='Delhi Rooftop')),
                         {self.opex_pay.pk})

    def test_search_by_site_code(self):
        self.assertEqual(self.listed(self.queue(tab='OPEX', q=self.opex_site.project_id)),
                         {self.opex_pay.pk})

    def test_search_does_not_repeat_a_row_with_several_matching_joins(self):
        VendorOrderProgram.objects.create(order=self.opex_order, program=self.central_tender)
        rows = self.queue(tab='OPEX', q='Tender').context['rows']
        pks = [row['payment'].pk for row in rows]
        self.assertEqual(len(pks), len(set(pks)))


# ---------------------------------------------------------------------------
# Mark paid — the approver is not the payer
# ---------------------------------------------------------------------------

class MarkPaidTests(QueueFixture):

    def test_the_approver_cannot_mark_it_paid(self):
        response = self.mark_paid(self.approver, self.opex_pay)
        self.assertEqual(response.status_code, 302)
        self.assertIn('You approved this request', ' '.join(_messages(response)))
        self.opex_pay.refresh_from_db()
        self.assertEqual(self.opex_pay.status, PaymentRequest.APPROVED)

    def test_the_requester_cannot_mark_it_paid(self):
        pay = _make_payment(self, self.opex_order, status=PaymentRequest.APPROVED,
                            requested_by=self.fin_b.user, approved_by=self.approver)
        response = self.mark_paid(self.fin_b, pay)
        self.assertIn('You raised this request', ' '.join(_messages(response)))
        pay.refresh_from_db()
        self.assertEqual(pay.status, PaymentRequest.APPROVED)

    def test_another_finance_user_can(self):
        response = self.mark_paid(self.finance, self.opex_pay)
        self.assertEqual(response.status_code, 302)
        self.opex_pay.refresh_from_db()
        self.assertEqual(self.opex_pay.status, PaymentRequest.CONFIRMED)
        self.assertEqual(self.opex_pay.payment_reference, 'UTR-555')
        self.assertEqual(self.opex_pay.payment_date, timezone.localdate())
        self.assertEqual(self.opex_pay.confirmed_by, self.finance.user)

    def test_the_buttons_follow_the_predicate(self):
        def can(profile):
            rows = self.queue(profile, tab='OPEX').context['rows']
            return {r['payment'].pk: r['can_mark_paid'] for r in rows}[self.opex_pay.pk]
        self.assertFalse(can(self.approver))
        self.assertTrue(can(self.finance))
        self.assertFalse(can(self.ceo))

    def test_without_a_reference_it_is_refused(self):
        response = self.mark_paid(self.finance, self.opex_pay, reference='   ')
        self.assertIn('reference', ' '.join(_messages(response)))
        self.opex_pay.refresh_from_db()
        self.assertEqual(self.opex_pay.status, PaymentRequest.APPROVED)

    def test_a_future_date_is_refused(self):
        response = self.mark_paid(self.finance, self.opex_pay,
                                  date=timezone.localdate() + timedelta(days=1))
        self.assertIn('cannot be in the future', ' '.join(_messages(response)))
        self.opex_pay.refresh_from_db()
        self.assertEqual(self.opex_pay.status, PaymentRequest.APPROVED)

    def test_a_date_before_the_raise_is_refused(self):
        response = self.mark_paid(self.finance, self.opex_pay,
                                  date=timezone.localdate() - timedelta(days=1))
        self.assertIn('before the request was raised', ' '.join(_messages(response)))
        self.opex_pay.refresh_from_db()
        self.assertEqual(self.opex_pay.status, PaymentRequest.APPROVED)

    def test_a_non_approved_request_is_refused_not_a_500(self):
        for payment in (self.res_pay, self.central_pay):
            with self.subTest(status=payment.status):
                response = self.mark_paid(self.finance, payment)
                self.assertEqual(response.status_code, 302)
                self.assertIn('cannot be marked paid', ' '.join(_messages(response)))
                before = payment.status
                payment.refresh_from_db()
                self.assertEqual(payment.status, before)

    def test_mark_paid_records_approved_to_confirmed(self):
        self.mark_paid(self.finance, self.opex_pay, reference='UTR-9')
        row = StatusTransition.objects.get(subject_type=SUBJECT_PAYMENT_REQUEST,
                                           subject_id=self.opex_pay.pk)
        self.assertEqual((row.from_status, row.to_status),
                         (PaymentRequest.APPROVED, PaymentRequest.CONFIRMED))
        self.assertEqual(row.actor, self.finance)
        self.assertEqual(row.remark, 'UTR-9')
        self.assertEqual(row.project, self.opex_site)

    def test_a_siteless_payment_can_be_marked_paid(self):
        pay = _make_payment(self, self.central_order, status=PaymentRequest.APPROVED,
                            approved_by=self.approver)
        self.mark_paid(self.finance, pay)
        pay.refresh_from_db()
        self.assertEqual(pay.status, PaymentRequest.CONFIRMED)
        row = StatusTransition.objects.get(subject_type=SUBJECT_PAYMENT_REQUEST,
                                           subject_id=pay.pk)
        self.assertIsNone(row.project)

    def test_non_finance_is_403(self):
        for profile in (self.scm, self.ceo, self.approver_b):
            with self.subTest(role=profile.role):
                self.assertEqual(self.mark_paid(profile, self.opex_pay).status_code, 403)
        self.opex_pay.refresh_from_db()
        self.assertEqual(self.opex_pay.status, PaymentRequest.APPROVED)


# ConfirmThroughServiceTests lived here until O6 deleted the project page's confirm door.
# Everything it pinned — the approver and the requester refused, a mandatory reference,
# the APPROVED -> CONFIRMED ledger row — is MarkPaidTests above, through the one door left.


# ---------------------------------------------------------------------------
# Actions from the queue come back to the queue
# ---------------------------------------------------------------------------

class ReturnToQueueTests(QueueFixture):

    def test_approve_from_the_queue_returns_to_the_same_tab_and_filter(self):
        here = reverse('payment_queue') + '?tab=Residential&status=pending_approval&q=RES'
        response = _client(self.approver).post(
            reverse('payment_queue_action', args=[self.res_pay.pk, 'approve']),
            {'remark': '', 'approved_amount': '20000', 'next': here})
        self.assertRedirects(response, here, fetch_redirect_response=False)
        self.res_pay.refresh_from_db()
        self.assertEqual(self.res_pay.status, PaymentRequest.APPROVED)
        self.assertEqual(StatusTransition.objects.filter(
            subject_type=SUBJECT_PAYMENT_REQUEST, subject_id=self.res_pay.pk).count(), 1)

    def test_hold_and_reject_return_to_the_queue(self):
        here = reverse('payment_queue') + '?tab=OPEX&page=1'
        response = _client(self.approver_b).post(
            reverse('payment_queue_action', args=[self.opex_pay.pk, 'hold']),
            {'reason': 'Check GRN', 'next': here})
        self.assertRedirects(response, here, fetch_redirect_response=False)
        response = _client(self.approver_b).post(
            reverse('payment_queue_action', args=[self.opex_pay.pk, 'reject']),
            {'reason': 'Duplicate', 'next': here})
        self.assertRedirects(response, here, fetch_redirect_response=False)
        self.opex_pay.refresh_from_db()
        self.assertEqual(self.opex_pay.status, PaymentRequest.REJECTED)

    def test_a_refusal_also_returns_to_the_queue(self):
        here = reverse('payment_queue') + '?tab=OPEX'
        response = _client(self.approver).post(
            reverse('payment_queue_action', args=[self.opex_pay.pk, 'hold']),
            {'reason': '', 'next': here})
        self.assertRedirects(response, here, fetch_redirect_response=False)

    def test_mark_paid_returns_to_the_queue(self):
        here = reverse('payment_queue') + '?tab=OPEX&status=approved'
        response = self.mark_paid(self.finance, self.opex_pay, next_url=here)
        self.assertRedirects(response, here, fetch_redirect_response=False)

    def test_an_offsite_next_is_ignored(self):
        response = _client(self.approver).post(
            reverse('payment_queue_action', args=[self.res_pay.pk, 'approve']),
            {'remark': '', 'approved_amount': '20000',
             'next': 'https://evil.example/payments/'})
        self.assertRedirects(response, reverse('vendor_order_detail', args=[self.res_order.pk]),
                             fetch_redirect_response=False)

    def test_a_next_that_is_not_the_queue_is_ignored(self):
        response = _client(self.approver).post(
            reverse('payment_queue_action', args=[self.res_pay.pk, 'approve']),
            {'remark': '', 'approved_amount': '20000', 'next': reverse('notifications')})
        self.assertRedirects(response, reverse('vendor_order_detail', args=[self.res_order.pk]),
                             fetch_redirect_response=False)

    def test_the_o4_403_passes_through(self):
        response = _client(self.fin_b).post(
            reverse('payment_queue_action', args=[self.res_pay.pk, 'approve']),
            {'next': reverse('payment_queue')})
        self.assertEqual(response.status_code, 403)

    def test_an_unknown_action_is_404(self):
        response = _client(self.approver).post(
            reverse('payment_queue_action', args=[self.res_pay.pk, 'respond']), {})
        self.assertEqual(response.status_code, 404)


# ---------------------------------------------------------------------------
# Notifications — in-app only, right recipients, never the actor
# ---------------------------------------------------------------------------

class NotificationTests(QueueFixture):

    def notices(self):
        return {(n.recipient_id, n.message) for n in Notification.objects.all()}

    def recipients(self):
        return set(Notification.objects.values_list('recipient_id', flat=True))

    def assert_in_app_only(self):
        self.assertTrue(NotificationLog.objects.exists())
        self.assertFalse(NotificationLog.objects.exclude(channel='in_app').exists())

    def test_hold_notifies_the_requester_only(self):
        with self.captureOnCommitCallbacks(execute=True):
            _client(self.approver).post(reverse('payment_hold', args=[self.res_pay.pk]),
                                        {'reason': 'GRN missing'})
        self.assertEqual(self.recipients(), {self.scm.pk})
        message = Notification.objects.get().message
        self.assertEqual(message, 'Payment of ₹20000.00 to Sunrise Modules is on hold: GRN missing')
        self.assertEqual(Notification.objects.get().link,
                         reverse('vendor_order_detail', args=[self.res_order.pk]))
        self.assert_in_app_only()

    def test_a_response_notifies_every_approver_but_the_requester(self):
        with self.captureOnCommitCallbacks(execute=True):
            _client(self.scm).post(reverse('payment_hold_respond', args=[self.central_pay.pk]),
                                   {'response': 'Rate matches addendum'})
        self.assertEqual(self.recipients(), {self.approver.pk, self.approver_b.pk})
        self.assertIn('responded to the hold on ₹20000.00 to Sunrise Modules',
                      Notification.objects.first().message)
        self.assert_in_app_only()

    def test_a_raise_notifies_every_approver_but_the_requester(self):
        # The requester holds the flag too: they must still hear nothing.
        self.scm.is_payment_approver = True
        self.scm.save(update_fields=['is_payment_approver'])
        with self.captureOnCommitCallbacks(execute=True):
            response = _client(self.scm).post(
                reverse('vendor_order_add_payment', args=[self.res_order.pk]),
                {'client_uuid': str(uuid.uuid4()), 'payment_amount': '1000'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.recipients(), {self.approver.pk, self.approver_b.pk})
        self.assert_in_app_only()

    def test_an_inactive_approver_is_not_notified(self):
        self.approver_b.is_active = False
        self.approver_b.save(update_fields=['is_active'])
        with self.captureOnCommitCallbacks(execute=True):
            _client(self.scm).post(
                reverse('vendor_order_add_payment', args=[self.res_order.pk]),
                {'client_uuid': str(uuid.uuid4()), 'payment_amount': '1000'})
        self.assertEqual(self.recipients(), {self.approver.pk})

    def test_mark_paid_notifies_the_requester_only(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.mark_paid(self.finance, self.opex_pay, reference='UTR-4')
        self.assertEqual(self.recipients(), {self.scm.pk})
        self.assertIn('Ref: UTR-4', Notification.objects.get().message)
        self.assert_in_app_only()

    def test_the_actor_is_never_notified(self):
        # SCM raises AND answers; SCM is the requester on the raise and the actor on the
        # answer — in neither case does SCM hear about its own act.
        with self.captureOnCommitCallbacks(execute=True):
            _client(self.scm).post(reverse('payment_hold_respond', args=[self.central_pay.pk]),
                                   {'response': 'ok'})
        self.assertNotIn(self.scm.pk, self.recipients())

    def test_approve_and_reject_notify_nobody(self):
        with self.captureOnCommitCallbacks(execute=True):
            _client(self.approver).post(reverse('payment_approve', args=[self.res_pay.pk]),
                                        {'remark': '', 'approved_amount': '20000'})
            _client(self.approver).post(reverse('payment_reject', args=[self.central_pay.pk]),
                                        {'reason': 'No'})
        self.assertFalse(Notification.objects.exists())

    def test_a_refused_move_notifies_nobody(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.mark_paid(self.approver, self.opex_pay)       # refused inside the lock
        self.assertFalse(Notification.objects.exists())


# ---------------------------------------------------------------------------
# Who may see the queue, and the nav
# ---------------------------------------------------------------------------

class AccessTests(QueueFixture):

    def test_scm_pm_and_design_are_403(self):
        for profile in (self.scm, self.pm, self.design):
            with self.subTest(role=profile.role):
                self.assertEqual(self.queue(profile).status_code, 403)

    def test_finance_ceo_admin_and_a_flag_holder_are_admitted(self):
        other = _approver('o5_pm_approver', role='PM')
        for profile in (self.finance, self.ceo, self.admin, other):
            with self.subTest(role=profile.role):
                self.assertEqual(self.queue(profile).status_code, 200)

    def test_a_flag_holder_who_cannot_read_the_order_gets_no_buttons(self):
        # The O4 views 403 before asking the approver predicate when the order is not
        # readable; the queue must not draw a button that door will refuse.
        outsider = _approver('o5_pm_outsider', role='PM')
        rows = {r['payment'].pk: r for r in self.queue(outsider, tab='OPEX').context['rows']}
        self.assertFalse(rows[self.central_pay.pk]['can_approve'])
        self.assertFalse(rows[self.central_pay.pk]['can_reject'])
        rows = {r['payment'].pk: r for r in self.queue(self.approver, tab='OPEX').context['rows']}
        self.assertTrue(rows[self.central_pay.pk]['can_approve'])
        self.assertTrue(rows[self.central_pay.pk]['can_reject'])

    def test_the_nav_link_follows_the_predicate(self):
        url = reverse('payment_queue')
        self.assertContains(self.queue(self.finance), f'href="{url}"')
        response = _client(self.scm).get(reverse('vendor_order_detail',
                                                 args=[self.res_order.pk]))
        self.assertNotContains(response, f'href="{url}"')

    def test_the_finance_dashboard_links_to_the_queue(self):
        response = _client(self.finance).get(reverse('dashboard_finance'))
        self.assertContains(response, reverse('payment_queue'))


# ---------------------------------------------------------------------------
# Flat in the number of rows
# ---------------------------------------------------------------------------

class QueryCountTests(QueueFixture):

    def _fill(self, n):
        for i in range(n):
            order = _make_order(self, 'CAPEX', po=f'PO-QC-{i}', program=self.central_tender)
            VendorOrderDocument.objects.create(
                order=order, doc_type='po', file_name='po.pdf', bucket='b', path=f'p/{i}.pdf',
                uploaded_by=self.scm)
            pay = _make_payment(self, order, status=PaymentRequest.ON_HOLD)
            PaymentRequestHold.objects.create(payment_request=pay, reason='why',
                                              held_by=self.approver)

    def _count(self):
        client = _client(self.approver)
        client.get(reverse('payment_queue'), {'tab': 'CAPEX'})   # warm the session
        with CaptureQueriesContext(connection) as ctx:
            response = client.get(reverse('payment_queue'), {'tab': 'CAPEX'})
        return len(ctx.captured_queries), len(response.context['rows'])

    def test_query_count_is_flat_across_1_and_50_rows(self):
        PaymentRequest.objects.filter(pk=self.capex_pay.pk).delete()
        self._fill(1)
        one, rows_one = self._count()
        self._fill(49)
        fifty, rows_fifty = self._count()
        print(f'\n[O5 query count] {rows_one} row: {one} queries; '
              f'{rows_fifty} rows: {fifty} queries')
        self.assertEqual((rows_one, rows_fifty), (1, 50))
        self.assertEqual(one, fifty)


# ---------------------------------------------------------------------------
# Two Finance users, one instant (Postgres only — SQLite ignores select_for_update)
# ---------------------------------------------------------------------------

@skipUnless(connection.vendor == 'postgresql',
            'select_for_update is a no-op on SQLite; run under the real settings.')
class ConcurrentMarkPaidTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.scm      = _profile('o5c_scm', 'SCM')
        self.pm       = _profile('o5c_pm', 'PM')
        self.approver = _approver('o5c_app')
        self.fin_a    = _profile('o5c_fin_a', 'Finance')
        self.fin_b    = _profile('o5c_fin_b', 'Finance')
        self.vendor   = Vendor.objects.create(name='Conc Vendor', contact_person='R',
                                              phone='9000000002')
        self.project  = _project('Conc Site', self.pm)
        self.order    = _make_order(self, 'Residential', site=self.project, po='PO-CONC')
        self.payment  = _make_payment(self, self.order, status=PaymentRequest.APPROVED,
                                      approved_by=self.approver)

    def test_two_finance_users_mark_paid_at_once_and_one_wins(self):
        results, barrier = {}, threading.Barrier(2)

        def act(index, profile):
            barrier.wait()
            try:
                results[index] = _client(profile).post(
                    reverse('payment_mark_paid', args=[self.payment.pk]),
                    {'payment_date': timezone.localdate().isoformat(),
                     'payment_reference': f'UTR-{index}'}).status_code
            finally:
                connection.close()

        threads = [threading.Thread(target=act, args=(i, p))
                   for i, p in enumerate((self.fin_a, self.fin_b))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(set(results.values()), {302})       # a refusal, not a 500
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, PaymentRequest.CONFIRMED)
        rows = StatusTransition.objects.filter(subject_type=SUBJECT_PAYMENT_REQUEST,
                                               subject_id=self.payment.pk)
        self.assertEqual(rows.count(), 1)                     # only the winner wrote
        self.assertEqual(rows.get().remark, self.payment.payment_reference)
