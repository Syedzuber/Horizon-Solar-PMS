"""O4b — an approver may approve part of a payment request.

What this file pins, and why each matters:

  * THE REQUEST IS NEVER REWRITTEN. `amount` stays what SCM asked for; approved_amount
    records what was approved, and every money rule reads effective_amount.
  * THE DIFFERENCE IS FREED. Approving 80k of 100k makes 20k available to request again,
    and paying the request pays 80k — paid, committed and invoice-awaited all move.
  * LESS THAN ASKED NEEDS A REASON; MORE THAN ASKED, ZERO OR NEGATIVE IS REFUSED. A
    refusal changes nothing — no status, no stamp, no ledger row.
  * THE DATABASE SAYS IT TOO: approved_amount within (0, amount], and an approved or paid
    request always carries one.
  * SCM'S ANSWER TO A HOLD CLEARS THE APPROVED AMOUNT with the rest of the approval, so the
    re-approval sets it afresh.
  * BOTH FIGURES SHOW WHEN THEY DIFFER, one when they agree. The requester hears about a
    partial approval, in-app; a full one tells nobody, as before.

Run with:
    python manage.py test projects.tests_payment_partial_approval --settings=solarpms.test_settings
The concurrency test runs only under the real (Postgres) settings:
    python manage.py test projects.tests_payment_partial_approval.ConcurrentPartialApprovalTests
"""
import threading
from decimal import Decimal
from unittest import skipUnless

from django.contrib.messages import get_messages
from django.db import IntegrityError, connection, transaction
from django.test import TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from .models import (
    Notification, PaymentRequest, StatusTransition, SUBJECT_PAYMENT_REQUEST,
    VendorOrderDocument, VENDOR_ORDER_DOC_INVOICE,
)
from .order_views import _order_money
from .tests_payment_approval import ApprovalFixture, _approver, _order, _payment
from .tests_vendor_order_raise import _client, _profile


def _messages(response):
    return [str(m) for m in get_messages(response.wsgi_request)]


class PartialFixture(ApprovalFixture):
    """An order of 2,00,000 with one pending request of 1,00,000 raised by SCM."""

    def setUp(self):
        self.order   = _order(self, total=Decimal('200000'), po='PO-O4B')
        self.payment = _payment(self, self.order, amount='100000')

    def approve_amount(self, amount, remark='', profile=None, payment=None):
        return _client(profile or self.approver).post(
            self.approve_url(payment), {'approved_amount': amount, 'remark': remark})

    def mark_paid(self, profile=None, extra=None):
        data = {'payment_date': timezone.localdate().isoformat(),
                'payment_reference': 'UTR-O4B'}
        data.update(extra or {})
        return _client(profile or self.finance).post(
            reverse('payment_mark_paid', args=[self.payment.pk]), data)

    def assert_unchanged(self):
        self.reload()
        self.assertEqual(self.payment.status, PaymentRequest.PENDING_APPROVAL)
        self.assertIsNone(self.payment.approved_amount)
        self.assertIsNone(self.payment.approved_by)
        self.assertEqual(self.payment.amount, Decimal('100000.00'))
        self.assertFalse(self.transitions().exclude(to_status=PaymentRequest.PENDING_APPROVAL)
                         .exists())

    def queue_html(self, profile=None):
        return _client(profile or self.finance).get(
            reverse('payment_queue'), {'tab': 'Residential'}).content.decode()


# ---------------------------------------------------------------------------
# Approving part of a request
# ---------------------------------------------------------------------------

class PartialApproveTests(PartialFixture):

    def test_approving_80k_of_100k_frees_20k_and_pays_80k(self):
        self.assertEqual(self.order.available_to_request, Decimal('100000.00'))

        response = self.approve_amount('80000', remark='Only 8 of 10 modules delivered')
        self.assertEqual(response.status_code, 302)

        self.reload()
        self.assertEqual(self.payment.status, PaymentRequest.APPROVED)
        self.assertEqual(self.payment.approved_amount, Decimal('80000.00'))
        self.assertEqual(self.payment.amount, Decimal('100000.00'))     # never rewritten
        self.assertEqual(self.payment.effective_amount, Decimal('80000.00'))
        self.assertEqual(self.payment.approved_by, self.approver)

        # Available rises by the 20k not approved.
        self.assertEqual(self.order.committed_amount, Decimal('80000.00'))
        self.assertEqual(self.order.available_to_request, Decimal('120000.00'))

        row = self.transitions().get(to_status=PaymentRequest.APPROVED)
        self.assertEqual(row.remark,
                         'approved ₹80000.00 of ₹100000.00: Only 8 of 10 modules delivered')

        self.assertEqual(self.mark_paid().status_code, 302)
        self.reload()
        self.assertEqual(self.payment.status, PaymentRequest.CONFIRMED)
        self.assertEqual(self.order.paid, Decimal('80000.00'))
        self.assertEqual(self.order.balance, Decimal('120000.00'))
        money = _order_money(self.order, list(self.order.documents.all()),
                             list(self.order.payments.all()))
        self.assertEqual(money['paid'], Decimal('80000.00'))
        self.assertEqual(money['committed'], Decimal('80000.00'))
        self.assertEqual(money['available'], Decimal('120000.00'))

    def test_the_freed_amount_can_be_requested_again(self):
        self.approve_amount('80000', remark='Short delivery')
        response = _client(self.scm).post(
            reverse('vendor_order_add_payment', args=[self.order.pk]),
            {'client_uuid': '6c1f0a54-0000-4000-8000-000000000001',
             'payment_amount': '120000'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.order.payments.count(), 2)
        self.assertEqual(self.order.available_to_request, Decimal('0.00'))

    def test_approving_less_without_a_reason_is_refused_and_changes_nothing(self):
        response = self.approve_amount('80000', remark='   ')
        self.assertEqual(response.status_code, 302)
        self.assertIn('needs a reason', ' '.join(_messages(response)))
        self.assert_unchanged()

    def test_approving_more_than_requested_is_refused(self):
        response = self.approve_amount('100000.01', remark='Rounding')
        self.assertIn('at most the ₹100000.00 requested', ' '.join(_messages(response)))
        self.assert_unchanged()

    def test_zero_negative_and_non_numbers_are_refused(self):
        for bad in ('0', '-5000', 'eighty', '', '80000.001'):
            with self.subTest(amount=bad):
                self.approve_amount(bad, remark='a reason')
                self.assert_unchanged()

    def test_full_approval_needs_no_reason_and_records_the_whole_amount(self):
        response = self.approve_amount('100000.00')
        self.assertEqual(response.status_code, 302)
        self.reload()
        self.assertEqual(self.payment.status, PaymentRequest.APPROVED)
        self.assertEqual(self.payment.approved_amount, Decimal('100000.00'))
        self.assertEqual(self.transitions().get(to_status=PaymentRequest.APPROVED).remark, '')

    def test_a_post_without_the_field_approves_in_full(self):
        """What the prefilled form submits unchanged, and what O4's own posts send."""
        self.approve()
        self.reload()
        self.assertEqual(self.payment.approved_amount, Decimal('100000.00'))


# ---------------------------------------------------------------------------
# The database
# ---------------------------------------------------------------------------

class ConstraintTests(PartialFixture):

    def assertRefused(self, **fields):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                PaymentRequest.objects.filter(pk=self.payment.pk).update(**fields)

    def test_the_database_refuses_an_approved_amount_above_the_request(self):
        self.assertRefused(status=PaymentRequest.APPROVED,
                           approved_amount=Decimal('100000.01'))

    def test_the_database_refuses_a_zero_or_negative_approved_amount(self):
        self.assertRefused(status=PaymentRequest.APPROVED, approved_amount=Decimal('0'))
        self.assertRefused(status=PaymentRequest.APPROVED, approved_amount=Decimal('-1'))

    def test_the_database_refuses_approved_or_paid_without_an_amount(self):
        self.assertRefused(status=PaymentRequest.APPROVED)
        self.assertRefused(status=PaymentRequest.CONFIRMED)

    def test_pending_and_held_rows_need_none(self):
        PaymentRequest.objects.filter(pk=self.payment.pk).update(status=PaymentRequest.ON_HOLD)
        self.reload()
        self.assertIsNone(self.payment.approved_amount)


# ---------------------------------------------------------------------------
# Hold after approval, and SCM's answer
# ---------------------------------------------------------------------------

class HoldAfterPartialApprovalTests(PartialFixture):

    def test_scm_answering_the_hold_clears_the_amount_and_reapproval_sets_it_afresh(self):
        self.approve_amount('80000', remark='Short delivery')
        self.hold(reason='Check the rate first')
        self.reload()
        self.assertEqual(self.payment.status, PaymentRequest.ON_HOLD)
        self.assertEqual(self.payment.approved_amount, Decimal('80000.00'))

        self.respond(response='Rate is per the PO')
        self.reload()
        self.assertEqual(self.payment.status, PaymentRequest.PENDING_APPROVAL)
        self.assertIsNone(self.payment.approved_amount)
        self.assertIsNone(self.payment.approved_by)
        self.assertIsNone(self.payment.approved_at)
        # Back to counting in full.
        self.assertEqual(self.order.committed_amount, Decimal('100000.00'))

        self.approve_amount('90000', remark='Nine delivered now', profile=self.approver_b)
        self.reload()
        self.assertEqual(self.payment.approved_amount, Decimal('90000.00'))
        self.assertEqual(self.payment.approved_by, self.approver_b)


# ---------------------------------------------------------------------------
# Invoice awaited
# ---------------------------------------------------------------------------

class InvoiceAwaitedTests(PartialFixture):

    def test_invoice_awaited_compares_the_approved_figure(self):
        VendorOrderDocument.objects.create(
            order=self.order, doc_type=VENDOR_ORDER_DOC_INVOICE, invoice_number='INV-80',
            invoice_amount=Decimal('80000'), file_name='inv.pdf', bucket='b',
            path='p/inv.pdf', uploaded_by=self.scm)
        self.approve_amount('80000', remark='Short delivery')
        self.mark_paid()

        # Paid 80k against 80k invoiced: nothing awaited. At the requested 100k it would be.
        money = _order_money(self.order, list(self.order.documents.all()),
                             list(self.order.payments.all()))
        self.assertFalse(money['invoice_awaited'])
        response = _client(self.finance).get(reverse('payment_queue'), {'tab': 'Residential'})
        self.assertEqual(response.context['tiles']['invoice_awaited_orders'], 0)

    def test_paying_more_than_invoiced_is_still_awaited(self):
        VendorOrderDocument.objects.create(
            order=self.order, doc_type=VENDOR_ORDER_DOC_INVOICE, invoice_number='INV-79',
            invoice_amount=Decimal('79999'), file_name='inv.pdf', bucket='b',
            path='p/inv.pdf', uploaded_by=self.scm)
        self.approve_amount('80000', remark='Short delivery')
        self.mark_paid()
        response = _client(self.finance).get(reverse('payment_queue'), {'tab': 'Residential'})
        self.assertEqual(response.context['tiles']['invoice_awaited_orders'], 1)


# ---------------------------------------------------------------------------
# Screens
# ---------------------------------------------------------------------------

class ScreenTests(PartialFixture):

    def test_the_queue_shows_both_figures_when_they_differ(self):
        self.approve_amount('80000', remark='Short delivery')
        html = self.queue_html()
        self.assertIn('₹100000.00 requested', html)
        self.assertIn('₹80000.00 approved', html)
        response = _client(self.finance).get(reverse('payment_queue'), {'tab': 'Residential'})
        self.assertEqual(response.context['tiles']['to_pay'], (1, Decimal('80000.00')))

    def test_the_queue_shows_one_figure_when_they_agree(self):
        self.approve_amount('100000')
        html = self.queue_html()
        self.assertNotIn('requested ·', html)
        self.assertNotIn(' approved</span>', html)
        self.assertIn('100000.00', html)

    def test_the_queue_approve_button_opens_the_form_prefilled(self):
        html = self.queue_html(self.approver)
        self.assertIn(f'data-bs-target="#q-approve-{self.payment.pk}"', html)
        self.assertIn(f'id="q-approve-{self.payment.pk}"', html)
        self.assertIn('name="approved_amount" value="100000.00"', html)

    def test_the_record_page_shows_both_figures_and_the_form(self):
        html = self.detail(self.approver).content.decode()
        self.assertIn('name="approved_amount" value="100000.00"', html)
        self.approve_amount('80000', remark='Short delivery')
        html = self.detail(self.scm).content.decode()
        self.assertIn('₹100000.00 requested', html)
        self.assertIn('₹80000.00 approved', html)
        for stray in ('{%', '%}', '{{', '}}', '{#'):
            self.assertNotIn(stray, html)

    def test_mark_paid_shows_the_amount_read_only_and_ignores_a_posted_one(self):
        self.approve_amount('80000', remark='Short delivery')
        html = self.queue_html()
        self.assertIn('Amount to pay', html)
        self.assertIn('₹80000.00', html)
        start = html.index(f'id="q-paid-{self.payment.pk}"')
        form = html[start:html.index('</form>', start)]
        self.assertIn('₹80000.00', form)
        self.assertNotIn('name="amount"', form)
        self.assertNotIn('name="approved_amount"', form)

        self.mark_paid(extra={'amount': '100000', 'approved_amount': '100000'})
        self.reload()
        self.assertEqual(self.payment.status, PaymentRequest.CONFIRMED)
        self.assertEqual(self.payment.approved_amount, Decimal('80000.00'))
        self.assertEqual(self.order.paid, Decimal('80000.00'))


# ---------------------------------------------------------------------------
# The requester hears about a partial approval
# ---------------------------------------------------------------------------

class PartialApprovalNoticeTests(PartialFixture):

    def test_a_partial_approval_tells_the_requester(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.approve_amount('80000', remark='Short delivery')
        notices = list(Notification.objects.all())
        self.assertEqual([n.recipient_id for n in notices], [self.scm.pk])
        self.assertEqual(notices[0].message,
                         'Payment request to Sunrise Modules approved ₹80000.00 of '
                         '₹100000.00: Short delivery')
        self.assertEqual(notices[0].link, reverse('vendor_order_detail', args=[self.order.pk]))

    def test_a_full_approval_tells_nobody(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.approve_amount('100000', remark='All good')
        self.assertFalse(Notification.objects.exists())

    def test_paid_notice_names_the_approved_figure(self):
        self.approve_amount('80000', remark='Short delivery')
        with self.captureOnCommitCallbacks(execute=True):
            self.mark_paid()
        self.assertIn('Payment of ₹80000.00 to Sunrise Modules was paid on',
                      Notification.objects.get().message)


# ---------------------------------------------------------------------------
# Two approvers, one instant (Postgres only — SQLite ignores select_for_update)
# ---------------------------------------------------------------------------

@skipUnless(connection.vendor == 'postgresql',
            'select_for_update is a no-op on SQLite; run under the real settings.')
class ConcurrentPartialApprovalTests(TransactionTestCase):
    """Two approvers approve different amounts at the same instant. One wins; the other
    is refused; the stored figure and the one ledger row are the winner's."""
    reset_sequences = True

    def setUp(self):
        from .tests_vendor_order_raise import _project
        from .models import Vendor
        self.scm     = _profile('o4bc_scm', 'SCM')
        self.pm      = _profile('o4bc_pm', 'PM')
        self.app_a   = _approver('o4bc_app_a')
        self.app_b   = _approver('o4bc_app_b', role='CEO')
        self.vendor  = Vendor.objects.create(name='Conc Vendor', contact_person='R',
                                             phone='9000000002')
        self.project = _project('Conc Site', self.pm)
        self.order   = _order(self, total=Decimal('200000'), po='PO-CONC-B')
        self.payment = _payment(self, self.order, amount='100000')

    def test_two_partial_approvals_at_once_leave_one_amount(self):
        results = {}
        barrier = threading.Barrier(2)
        acts = [(self.app_a, '70000', 'a'), (self.app_b, '90000', 'b')]

        def act(index, profile, amount, remark):
            barrier.wait()
            try:
                results[index] = _client(profile).post(
                    reverse('payment_approve', args=[self.payment.pk]),
                    {'approved_amount': amount, 'remark': remark}).status_code
            finally:
                connection.close()

        threads = [threading.Thread(target=act, args=(i, *spec)) for i, spec in enumerate(acts)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(set(results.values()), {302})

        self.payment.refresh_from_db()
        rows = list(StatusTransition.objects.filter(
            subject_type=SUBJECT_PAYMENT_REQUEST, subject_id=self.payment.pk))
        self.assertEqual(len(rows), 1)
        winner = {self.app_a.pk: Decimal('70000.00'), self.app_b.pk: Decimal('90000.00')}
        self.assertEqual(self.payment.approved_amount, winner[self.payment.approved_by_id])
        self.assertTrue(rows[0].remark.startswith(
            f'approved ₹{self.payment.approved_amount} of ₹100000.00'))
