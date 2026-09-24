"""O4c — the approved amount is a ceiling, and approval re-checks the order total.

What this file pins, and why each matters:

  * THE REPORTED SEQUENCE CANNOT OVER-COMMIT. Approve 80k of 100k, raise the freed 20k,
    hold the 80k, SCM answers: before O4c the answer cleared the approved amount and the
    request counted at 100k again — 120k committed on a 100k order. Now the answer keeps
    the 80k, a re-approval at 100k is refused, and committed never exceeds the total.
  * A RE-APPROVAL MAY LOWER THE FIGURE, NEVER RAISE IT, and less than requested still
    needs a reason.
  * THE AMOUNT IS REQUIRED. No approve-in-full fallback.
  * THE ORDER TOTAL IS A BACKSTOP UNDER A LOCK: an approval that would commit more than
    the total is refused, even on a request never approved before, and two approvals on
    one order serialise on the order row (Postgres).

Run with:
    python manage.py test projects.tests_payment_approval_ceiling --settings=solarpms.test_settings
The concurrency test runs only under the real (Postgres) settings:
    python manage.py test projects.tests_payment_approval_ceiling.ConcurrentOrderTotalTests
"""
import threading
import uuid
from decimal import Decimal
from unittest import skipUnless

from django.contrib.messages import get_messages
from django.db import connection, transaction
from django.test import TransactionTestCase
from django.urls import reverse

from .models import PaymentRequest, StatusTransition, SUBJECT_PAYMENT_REQUEST
from .tests_payment_approval import ApprovalFixture, _approver, _order, _payment
from .tests_vendor_order_raise import _client, _profile


def _messages(response):
    return ' '.join(str(m) for m in get_messages(response.wsgi_request))


class CeilingFixture(ApprovalFixture):
    """An order of 1,00,000 with one pending request for all of it."""

    def setUp(self):
        self.order   = _order(self, total=Decimal('100000'), po='PO-O4C')
        self.payment = _payment(self, self.order, amount='100000')

    def approve_amount(self, amount, remark='', profile=None, payment=None):
        return _client(profile or self.approver).post(
            self.approve_url(payment), {'approved_amount': amount, 'remark': remark})

    def raise_payment(self, amount):
        return _client(self.scm).post(
            reverse('vendor_order_add_payment', args=[self.order.pk]),
            {'client_uuid': str(uuid.uuid4()), 'payment_amount': amount})

    def assert_within_total(self):
        self.order.refresh_from_db()
        self.assertLessEqual(self.order.committed_amount, self.order.total_amount)


# ---------------------------------------------------------------------------
# The reported sequence
# ---------------------------------------------------------------------------

class ReportedSequenceTests(CeilingFixture):

    def test_hold_and_answer_cannot_bring_the_freed_amount_back(self):
        self.approve_amount('80000', remark='Only 8 of 10 delivered')
        self.assertEqual(self.order.committed_amount, Decimal('80000.00'))
        self.assert_within_total()

        self.assertEqual(self.raise_payment('20000').status_code, 302)
        self.assertEqual(self.order.payments.count(), 2)
        self.assertEqual(self.order.committed_amount, Decimal('100000.00'))
        self.assert_within_total()

        self.hold(reason='Check the rate')
        self.assert_within_total()
        self.respond(response='Rate is per the PO')
        self.reload()
        self.assertEqual(self.payment.status, PaymentRequest.PENDING_APPROVAL)
        self.assertEqual(self.payment.approved_amount, Decimal('80000.00'))
        self.assertEqual(self.order.committed_amount, Decimal('100000.00'))
        self.assert_within_total()

        response = self.approve_amount('100000', profile=self.approver_b)
        self.assertIn('previously approved for ₹80000.00', _messages(response))
        self.reload()
        self.assertEqual(self.payment.status, PaymentRequest.PENDING_APPROVAL)
        self.assertEqual(self.payment.approved_amount, Decimal('80000.00'))
        self.assert_within_total()

        response = self.approve_amount('80000', remark='Eight, as before',
                                       profile=self.approver_b)
        self.reload()
        self.assertEqual(self.payment.status, PaymentRequest.APPROVED)
        self.assertEqual(self.payment.approved_amount, Decimal('80000.00'))
        self.assertEqual(self.payment.approved_by, self.approver_b)
        self.assertEqual(self.order.committed_amount, Decimal('100000.00'))
        self.assert_within_total()

    def test_approving_straight_from_hold_is_capped_too(self):
        """Held after approval and released without SCM's answer: the same ceiling."""
        self.approve_amount('80000', remark='Short delivery')
        self.hold(reason='Check the rate')
        self.approve_amount('100000', profile=self.approver_b)
        self.reload()
        self.assertEqual(self.payment.status, PaymentRequest.ON_HOLD)
        self.assertEqual(self.payment.approved_amount, Decimal('80000.00'))


# ---------------------------------------------------------------------------
# Re-approval below the previous figure
# ---------------------------------------------------------------------------

class LowerReapprovalTests(CeilingFixture):

    def setUp(self):
        super().setUp()
        self.approve_amount('80000', remark='Short delivery')
        self.hold(reason='Check the rate')
        self.respond(response='Answered')

    def test_a_lower_reapproval_with_a_reason_works(self):
        self.approve_amount('60000', remark='Six only', profile=self.approver_b)
        self.reload()
        self.assertEqual(self.payment.status, PaymentRequest.APPROVED)
        self.assertEqual(self.payment.approved_amount, Decimal('60000.00'))
        self.assertEqual(self.transitions().filter(to_status=PaymentRequest.APPROVED)
                         .last().remark, 'approved ₹60000.00 of ₹100000.00: Six only')

    def test_a_lower_reapproval_without_a_reason_is_refused(self):
        response = self.approve_amount('60000', profile=self.approver_b)
        self.assertIn('needs a reason', _messages(response))
        self.reload()
        self.assertEqual(self.payment.status, PaymentRequest.PENDING_APPROVAL)
        self.assertEqual(self.payment.approved_amount, Decimal('80000.00'))

    def test_the_form_shows_and_prefills_the_previous_figure(self):
        html = self.detail(self.approver_b).content.decode()
        self.assertIn('Previously approved ₹80000.00', html)
        self.assertIn('name="approved_amount" value="80000.00"', html)
        self.assertIn('max="80000.00"', html)


# ---------------------------------------------------------------------------
# The amount is required
# ---------------------------------------------------------------------------

class AmountRequiredTests(CeilingFixture):

    def test_a_post_without_an_amount_is_refused(self):
        for data in ({'remark': 'ok'}, {'remark': 'ok', 'approved_amount': ''},
                     {'remark': 'ok', 'approved_amount': '   '}):
            with self.subTest(data=data):
                response = _client(self.approver).post(self.approve_url(), data)
                self.assertIn('must be more than 0', _messages(response))
                self.reload()
                self.assertEqual(self.payment.status, PaymentRequest.PENDING_APPROVAL)
                self.assertIsNone(self.payment.approved_amount)
                self.assertFalse(self.transitions().exists())


# ---------------------------------------------------------------------------
# The order-total backstop
# ---------------------------------------------------------------------------

class OrderTotalBackstopTests(CeilingFixture):

    def test_an_over_raised_request_never_approved_is_refused(self):
        # Written directly, past the raise path's own check: 100k + 30k on a 100k order.
        extra = _payment(self, self.order, amount='30000')
        response = self.approve_amount('30000', payment=extra)
        self.assertIn('at most ₹0.00 can be approved', _messages(response))
        extra.refresh_from_db()
        self.assertEqual(extra.status, PaymentRequest.PENDING_APPROVAL)
        self.assertIsNone(extra.approved_amount)

    def test_the_backstop_leaves_room_for_what_fits(self):
        extra = _payment(self, self.order, amount='30000')
        self.approve_amount('70000', remark='Seven only')        # the first: 70k
        response = self.approve_amount('30000', payment=extra)
        self.assertEqual(response.status_code, 302)
        extra.refresh_from_db()
        self.assertEqual(extra.status, PaymentRequest.APPROVED)
        self.assert_within_total()

    def test_a_rejected_request_does_not_count(self):
        extra = _payment(self, self.order, amount='30000')
        self.hold()
        self.reject()
        self.approve_amount('30000', payment=extra)
        extra.refresh_from_db()
        self.assertEqual(extra.status, PaymentRequest.APPROVED)


# ---------------------------------------------------------------------------
# Two approvals on one order, one instant (Postgres only)
# ---------------------------------------------------------------------------

@skipUnless(connection.vendor == 'postgresql',
            'select_for_update is a no-op on SQLite; run under the real settings.')
class ConcurrentOrderTotalTests(TransactionTestCase):
    """The order row is locked before the request, and approvals on one order serialise.

    WHY NOT "TWO APPROVALS THAT TOGETHER EXCEED THE TOTAL, ONE REFUSED". committed_total()
    counts a pending request at its requested amount, and an approval can only keep that
    figure or lower it (it is capped by the request, and by any earlier approval). So two
    requests that together exceed the total are each refused on their own, raced or not,
    and two that fit cannot be pushed over by approving them. What the lock must still
    guarantee is that the check reads settled figures and the outcome is one a serial run
    could give — the two tests below.
    """
    reset_sequences = True

    def setUp(self):
        from .tests_vendor_order_raise import _project
        from .models import Vendor
        self.scm     = _profile('o4cc_scm', 'SCM')
        self.pm      = _profile('o4cc_pm', 'PM')
        self.app_a   = _approver('o4cc_app_a')
        self.app_b   = _approver('o4cc_app_b', role='CEO')
        self.vendor  = Vendor.objects.create(name='Conc Vendor', contact_person='R',
                                             phone='9000000002')
        self.project = _project('Conc Site', self.pm)
        self.order   = _order(self, total=Decimal('100000'), po='PO-CONC-C')
        # Raised past the total directly: 1,00,000 + 30,000 on a 1,00,000 order.
        self.first   = _payment(self, self.order, amount='100000')
        self.second  = _payment(self, self.order, amount='30000')

    def _approve(self, profile, payment, amount, remark, results, key):
        try:
            results[key] = _client(profile).post(
                reverse('payment_approve', args=[payment.pk]),
                {'approved_amount': amount, 'remark': remark}).status_code
        finally:
            connection.close()

    def test_approval_waits_for_the_order_row_lock(self):
        """Another transaction holds the ORDER row: the approval blocks until it lets go,
        so the order lock is taken, and taken before anything is written."""
        from .models import VendorOrder
        results = {}
        worker = threading.Thread(target=self._approve, args=(
            self.app_a, self.first, '70000', 'Seven only', results, 'a'))
        with transaction.atomic():
            VendorOrder.objects.select_for_update().get(pk=self.order.pk)
            worker.start()
            worker.join(timeout=1.5)
            self.assertTrue(worker.is_alive(), 'approval did not wait for the order lock')
            self.assertEqual(PaymentRequest.objects.get(pk=self.first.pk).status,
                             PaymentRequest.PENDING_APPROVAL)
        worker.join(timeout=30)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results, {'a': 302})
        self.first.refresh_from_db()
        self.assertEqual(self.first.status, PaymentRequest.APPROVED)

    def test_two_approvals_on_one_order_resolve_as_some_serial_order_would(self):
        """First at 70k, second at 30k, at the same instant. Serially: first-then-second
        approves both (70 + 30 = 100); second-then-first refuses the second (30 + the
        first still pending at 100 = 130) and approves the first. Anything else — both
        refused, or more than the total committed — means the check read unsettled rows."""
        results = {}
        barrier = threading.Barrier(2)

        def act(profile, payment, amount, remark, key):
            barrier.wait()
            self._approve(profile, payment, amount, remark, results, key)

        threads = [threading.Thread(target=act, args=spec) for spec in (
            (self.app_a, self.first, '70000', 'Seven only', 'a'),
            (self.app_b, self.second, '30000', '', 'b'))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(set(results.values()), {302})         # refusal, not a 500

        self.first.refresh_from_db()
        self.second.refresh_from_db()
        outcome = (self.first.status, self.second.status)
        self.assertIn(outcome, [
            (PaymentRequest.APPROVED, PaymentRequest.APPROVED),
            (PaymentRequest.APPROVED, PaymentRequest.PENDING_APPROVAL),
        ])
        if outcome[1] == PaymentRequest.APPROVED:
            self.order.refresh_from_db()
            self.assertLessEqual(self.order.committed_amount, self.order.total_amount)

