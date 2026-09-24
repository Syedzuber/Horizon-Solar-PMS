"""O4 — the payment approval gate, the hold and its response, and rejection after hold.

What this file pins, and why each matters:

  * A RAISED PAYMENT IS NOT AN APPROVED ONE. Every raise path creates PENDING_APPROVAL,
    and Finance's own screens — which read `status=APPROVED` — do not see it. That is the
    gate: before O4 a payment reached Finance the moment SCM asked for it.
  * NOBODY APPROVES THEIR OWN REQUEST. The flag says what you may be; the requester is
    refused whatever they hold. This is design settled decision 2 applied to money
    (design_views._other_gate_actor_conflict), and it is a 403, not a hidden button.
  * REJECT IS REACHABLE ONLY FROM HOLD. A rejection is final and frees the committed
    money, so SCM must have had the chance to answer first. Reject from PENDING_APPROVAL
    is refused and changes nothing.
  * NO ACTION WITHOUT ITS REASON. Hold needs a reason, reject needs a reason, a response
    needs text. An empty one refuses and writes nothing — no status, no row, no ledger.
  * ONE HOLD, ONE RESPONSE, AND A SECOND HOLD IS A SECOND ROW. The conversation is
    readable in order and no earlier reason is overwritten; at most one hold is open.
  * THE LEDGER HAS EVERY MOVE. Five transitions, each with from_status, to_status, the
    actor and the reason — PaymentRequest stopped being "instrumented-pending" here.

Run with:
    python manage.py test projects.tests_payment_approval --settings=solarpms.test_settings
The concurrency test runs only under the real (Postgres) settings:
    python manage.py test projects.tests_payment_approval.ConcurrentDecisionTests
"""
import threading
import uuid
from decimal import Decimal
from unittest import skipUnless

from django.db import connection
from django.test import TestCase, TransactionTestCase
from django.urls import reverse

from .models import (
    ActivityLog, PaymentRequest, PaymentRequestHold, StatusTransition,
    SUBJECT_PAYMENT_REQUEST, VendorOrder, VendorOrderLine, VendorOrderSite,
)
from .tests_vendor_order_raise import RaiseFixture, _client, _profile


def _order(fixture, total=Decimal('60000'), po='PO-O4'):
    order = VendorOrder.objects.create(
        vendor=fixture.vendor, project_type='Residential', po_number=po,
        created_by=fixture.scm, client_uuid=uuid.uuid4())
    VendorOrderSite.objects.create(order=order, project=fixture.project)
    VendorOrderLine.objects.create(order=order, item_description='Modules',
                                   quantity=Decimal('10'), amount=total)
    return order


def _payment(fixture, order, amount='25000', status=PaymentRequest.PENDING_APPROVAL,
             requested_by=None):
    return PaymentRequest.objects.create(
        vendor_order=order, project=fixture.project, vendor=fixture.vendor,
        amount=Decimal(amount), requested_by=requested_by or fixture.scm.user,
        status=status)


def _approver(username, role='Finance'):
    profile = _profile(username, role)
    profile.is_payment_approver = True
    profile.save(update_fields=['is_payment_approver'])
    return profile


class ApprovalFixture(RaiseFixture):
    """One order, one pending payment, and the four kinds of person O4 distinguishes:
    an approver, a second approver, a Finance user WITHOUT the flag, and SCM."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.approver   = _approver('o4_approver')
        cls.approver_b = _approver('o4_approver_b', role='CEO')
        cls.plain_fin  = _profile('o4_plain_fin', 'Finance')   # no flag
        cls.admin_noflag = _profile('o4_admin_noflag', 'Admin')

    def setUp(self):
        self.order   = _order(self)
        self.payment = _payment(self, self.order)

    # -- urls ----------------------------------------------------------------
    def approve_url(self, payment=None):
        return reverse('payment_approve', args=[(payment or self.payment).pk])

    def hold_url(self, payment=None):
        return reverse('payment_hold', args=[(payment or self.payment).pk])

    def reject_url(self, payment=None):
        return reverse('payment_reject', args=[(payment or self.payment).pk])

    def respond_url(self, payment=None):
        return reverse('payment_hold_respond', args=[(payment or self.payment).pk])

    # -- acts ----------------------------------------------------------------
    def approve(self, profile=None, remark='', payment=None):
        return _client(profile or self.approver).post(
            self.approve_url(payment), {'remark': remark})

    def hold(self, profile=None, reason='GRN not received', payment=None):
        return _client(profile or self.approver).post(
            self.hold_url(payment), {'reason': reason})

    def reject(self, profile=None, reason='Duplicate of PR-9', payment=None):
        return _client(profile or self.approver).post(
            self.reject_url(payment), {'reason': reason})

    def respond(self, profile=None, response='GRN attached', payment=None):
        return _client(profile or self.scm).post(
            self.respond_url(payment), {'response': response})

    # -- reads ---------------------------------------------------------------
    def reload(self, payment=None):
        (payment or self.payment).refresh_from_db()
        return payment or self.payment

    def transitions(self, payment=None):
        return StatusTransition.objects.filter(
            subject_type=SUBJECT_PAYMENT_REQUEST,
            subject_id=(payment or self.payment).pk).order_by('pk')

    def detail(self, profile):
        return _client(profile).get(
            reverse('vendor_order_detail', args=[self.order.pk]))


# ---------------------------------------------------------------------------
# The gate exists
# ---------------------------------------------------------------------------

class GateExistsTests(ApprovalFixture):

    def test_a_raised_payment_awaits_approval_and_finance_does_not_see_it_as_approved(self):
        """The whole point of O4 in one assertion. Finance's screens filter
        status=APPROVED (dashboard_finance, the CEO finance summary, the confirm view);
        a request awaiting approval is simply not in that set."""
        response, _storage = self.post()   # the real raise page, not a fixture row
        self.assertEqual(response.status_code, 302)
        pr = PaymentRequest.objects.exclude(pk=self.payment.pk).get()
        self.assertEqual(pr.status, PaymentRequest.PENDING_APPROVAL)
        self.assertFalse(
            PaymentRequest.objects.filter(pk=pr.pk, status=PaymentRequest.APPROVED).exists())

    def test_the_raised_payment_is_not_yet_approved_by_anyone(self):
        self.assertIsNone(self.payment.approved_by)
        self.assertIsNone(self.payment.approved_at)


# ---------------------------------------------------------------------------
# Approve
# ---------------------------------------------------------------------------

class ApproveTests(ApprovalFixture):

    def test_an_approver_approves_and_the_stamp_and_one_transition_follow(self):
        self.assertEqual(self.approve().status_code, 302)
        payment = self.reload()
        self.assertEqual(payment.status, PaymentRequest.APPROVED)
        self.assertEqual(payment.approved_by, self.approver)
        self.assertIsNotNone(payment.approved_at)

        transition = self.transitions().get()
        self.assertEqual(transition.from_status, PaymentRequest.PENDING_APPROVAL)
        self.assertEqual(transition.to_status, PaymentRequest.APPROVED)
        self.assertEqual(transition.actor, self.approver)
        self.assertTrue(ActivityLog.objects.filter(
            action_code='payment_request_approved', entity_id=payment.pk).exists())

    def test_the_remark_is_optional_on_approval_alone(self):
        self.assertEqual(self.approve(remark='').status_code, 302)
        self.assertEqual(self.reload().status, PaymentRequest.APPROVED)

    def test_a_remark_reaches_the_ledger_when_given(self):
        self.approve(remark='Checked against the GRN')
        self.assertEqual(self.transitions().get().remark, 'Checked against the GRN')

    def test_approve_is_post_only(self):
        response = _client(self.approver).get(self.approve_url())
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.reload().status, PaymentRequest.PENDING_APPROVAL)

    def test_approving_an_approved_request_is_refused_not_repeated(self):
        self.approve()
        self.assertEqual(self.approve(profile=self.approver_b).status_code, 302)
        self.assertEqual(self.transitions().count(), 1)
        self.assertEqual(self.reload().approved_by, self.approver)


# ---------------------------------------------------------------------------
# The same-person rule
# ---------------------------------------------------------------------------

class SamePersonTests(ApprovalFixture):

    def setUp(self):
        super().setUp()
        # The requester ALSO holds the approver flag — the case the rule is for.
        self.scm.is_payment_approver = True
        self.scm.save(update_fields=['is_payment_approver'])

    def test_the_requester_holding_the_flag_cannot_approve_their_own_request(self):
        response = self.approve(profile=self.scm)
        self.assertEqual(response.status_code, 403)
        payment = self.reload()
        self.assertEqual(payment.status, PaymentRequest.PENDING_APPROVAL)
        self.assertIsNone(payment.approved_by)
        self.assertFalse(self.transitions().exists())

    def test_the_requester_cannot_hold_or_reject_their_own_request_either(self):
        self.assertEqual(self.hold(profile=self.scm).status_code, 403)
        self.hold()                                   # somebody else holds it
        self.assertEqual(self.reject(profile=self.scm).status_code, 403)
        self.assertEqual(self.reload().status, PaymentRequest.ON_HOLD)

    def test_the_same_person_approves_somebody_elses_request_normally(self):
        """PER REQUEST, NOT PER USER — the design precedent's exact shape."""
        other = _payment(self, self.order, amount='1000',
                         requested_by=self.approver.user)
        self.assertEqual(self.approve(profile=self.scm, payment=other).status_code, 302)
        other.refresh_from_db()
        self.assertEqual(other.status, PaymentRequest.APPROVED)


# ---------------------------------------------------------------------------
# Hold, reject, and the order between them
# ---------------------------------------------------------------------------

class HoldAndRejectTests(ApprovalFixture):

    def test_reject_from_pending_approval_is_refused_and_changes_nothing(self):
        response = self.reject()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.reload().status, PaymentRequest.PENDING_APPROVAL)
        self.assertEqual(self.reload().decision_reason, '')
        self.assertFalse(self.transitions().exists())

    def test_hold_then_reject_succeeds_and_is_final(self):
        self.hold(reason='Invoice does not match the PO')
        self.assertEqual(self.reload().status, PaymentRequest.ON_HOLD)
        self.reject(reason='Order cancelled with the vendor')
        payment = self.reload()
        self.assertEqual(payment.status, PaymentRequest.REJECTED)
        self.assertEqual(payment.decision_reason, 'Order cancelled with the vendor')
        # Final: nothing moves it again.
        self.assertEqual(self.approve().status_code, 302)
        self.assertEqual(self.reload().status, PaymentRequest.REJECTED)
        self.assertEqual(self.hold().status_code, 302)
        self.assertEqual(self.reload().status, PaymentRequest.REJECTED)

    def test_hold_from_approved_works(self):
        self.approve()
        self.hold(profile=self.approver_b, reason='Vendor disputes the invoice')
        payment = self.reload()
        self.assertEqual(payment.status, PaymentRequest.ON_HOLD)
        self.assertEqual(payment.open_hold.reason, 'Vendor disputes the invoice')
        self.assertEqual(payment.open_hold.held_by, self.approver_b)
        # The approval stamp survives a hold — only a RESPONSE clears it.
        self.assertEqual(payment.approved_by, self.approver)

    def test_approve_from_on_hold_works_and_leaves_the_hold_open(self):
        self.hold()
        self.assertEqual(self.approve(profile=self.approver_b).status_code, 302)
        payment = self.reload()
        self.assertEqual(payment.status, PaymentRequest.APPROVED)
        self.assertIsNotNone(payment.open_hold)
        self.assertEqual(payment.open_hold.response, '')

    def test_rejection_leaves_the_open_hold_open(self):
        """It was overruled, not answered. Marking it answered would put words in
        SCM's mouth."""
        self.hold()
        self.reject()
        self.assertIsNotNone(self.reload().open_hold)


class MandatoryReasonTests(ApprovalFixture):

    def test_a_hold_with_no_reason_is_refused_and_writes_nothing(self):
        response = self.hold(reason='   ')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.reload().status, PaymentRequest.PENDING_APPROVAL)
        self.assertFalse(PaymentRequestHold.objects.exists())
        self.assertFalse(self.transitions().exists())

    def test_a_rejection_with_no_reason_is_refused_and_writes_nothing(self):
        self.hold()
        response = self.reject(reason='')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.reload().status, PaymentRequest.ON_HOLD)
        self.assertEqual(self.reload().decision_reason, '')
        self.assertEqual(self.transitions().count(), 1)   # the hold's row only

    def test_a_response_with_no_text_is_refused_and_writes_nothing(self):
        self.hold()
        response = self.respond(response='  ')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.reload().status, PaymentRequest.ON_HOLD)
        self.assertEqual(PaymentRequestHold.objects.get().response, '')
        self.assertEqual(self.transitions().count(), 1)


# ---------------------------------------------------------------------------
# The response, and what a second hold does
# ---------------------------------------------------------------------------

class RespondTests(ApprovalFixture):

    def test_scm_responds_and_the_request_goes_back_for_approval(self):
        self.hold(reason='GRN missing')
        self.respond(response='GRN 447 attached to the order')
        payment = self.reload()
        self.assertEqual(payment.status, PaymentRequest.PENDING_APPROVAL)
        hold = PaymentRequestHold.objects.get()
        self.assertEqual(hold.reason, 'GRN missing')       # the hold keeps its reason
        self.assertEqual(hold.response, 'GRN 447 attached to the order')
        self.assertEqual(hold.responded_by, self.scm)
        self.assertIsNotNone(hold.responded_at)
        self.assertIsNone(payment.open_hold)               # answered: no longer open
        self.assertTrue(ActivityLog.objects.filter(
            action_code='payment_hold_answered', entity_id=payment.pk).exists())

    def test_a_response_clears_an_approval_stamp_taken_before_the_hold(self):
        self.approve()
        self.hold(profile=self.approver_b)
        self.respond()
        payment = self.reload()
        self.assertEqual(payment.status, PaymentRequest.PENDING_APPROVAL)
        self.assertIsNone(payment.approved_by)
        self.assertIsNone(payment.approved_at)

    def test_a_second_hold_after_a_response_is_a_second_row(self):
        self.hold(reason='first question')
        self.respond(response='first answer')
        self.hold(reason='second question')
        holds = list(PaymentRequestHold.objects.order_by('pk'))
        self.assertEqual(len(holds), 2)
        self.assertEqual(holds[0].reason, 'first question')
        self.assertEqual(holds[0].response, 'first answer')   # untouched by the second
        self.assertEqual(holds[1].reason, 'second question')
        self.assertEqual(holds[1].response, '')
        self.assertEqual(self.reload().open_hold, holds[1])

    def test_only_one_hold_can_be_open_at_a_time(self):
        """The predicate refuses the second hold (the status is already ON_HOLD), and
        the partial unique index refuses it underneath — belt and database."""
        self.hold(reason='first question')
        self.assertEqual(self.hold(profile=self.approver_b,
                                   reason='second question').status_code, 302)
        self.assertEqual(PaymentRequestHold.objects.count(), 1)

        from django.db import IntegrityError, transaction
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                PaymentRequestHold.objects.create(
                    payment_request=self.payment, reason='straight past the view',
                    held_by=self.approver_b)

    def test_scm_cannot_respond_when_there_is_no_open_hold(self):
        self.assertEqual(self.respond().status_code, 403)
        self.hold()
        self.respond()
        self.assertEqual(self.respond(response='again').status_code, 403)

    def test_an_approver_cannot_answer_the_hold_they_took(self):
        self.hold()
        self.assertEqual(self.respond(profile=self.approver).status_code, 403)


# ---------------------------------------------------------------------------
# Who may not
# ---------------------------------------------------------------------------

class UnentitledTests(ApprovalFixture):

    def _all_four(self, profile):
        return [
            _client(profile).post(self.approve_url(), {'remark': 'x'}),
            _client(profile).post(self.hold_url(),    {'reason': 'x'}),
            _client(profile).post(self.reject_url(),  {'reason': 'x'}),
            _client(profile).post(self.respond_url(), {'response': 'x'}),
        ]

    def assert_forbidden_and_unchanged(self, profile):
        for response in self._all_four(profile):
            self.assertEqual(response.status_code, 403)
        self.assertEqual(self.reload().status, PaymentRequest.PENDING_APPROVAL)
        self.assertFalse(PaymentRequestHold.objects.exists())
        self.assertFalse(self.transitions().exists())

    def test_a_finance_user_without_the_flag_is_refused_on_every_action(self):
        self.assert_forbidden_and_unchanged(self.plain_fin)

    def test_an_admin_without_the_flag_is_refused_on_every_action(self):
        """The flag is not implied by seniority. An Admin reads every order and approves
        no payment until somebody ticks the box."""
        self.assert_forbidden_and_unchanged(self.admin_noflag)

    def test_a_pm_is_refused_on_every_action(self):
        self.assert_forbidden_and_unchanged(self.pm)

    def test_scm_is_refused_on_the_three_approver_actions(self):
        for response in self._all_four(self.scm)[:3]:
            self.assertEqual(response.status_code, 403)

    def test_a_reader_who_cannot_see_the_order_gets_403_before_anything_else(self):
        outsider = _profile('o4_outsider', 'PM')     # no site on this order
        self.assertEqual(
            _client(outsider).post(self.approve_url(), {'remark': 'x'}).status_code, 403)


# ---------------------------------------------------------------------------
# The ledger
# ---------------------------------------------------------------------------

class LedgerTests(ApprovalFixture):

    def test_every_action_records_from_to_actor_and_reason(self):
        self.hold(reason='GRN missing')
        self.respond(response='GRN attached')
        self.approve(remark='satisfied')
        self.hold(profile=self.approver_b, reason='vendor dispute')
        self.reject(profile=self.approver_b, reason='order cancelled')

        rows = list(self.transitions())
        self.assertEqual(
            [(r.from_status, r.to_status) for r in rows],
            [(PaymentRequest.PENDING_APPROVAL, PaymentRequest.ON_HOLD),
             (PaymentRequest.ON_HOLD,          PaymentRequest.PENDING_APPROVAL),
             (PaymentRequest.PENDING_APPROVAL, PaymentRequest.APPROVED),
             (PaymentRequest.APPROVED,         PaymentRequest.ON_HOLD),
             (PaymentRequest.ON_HOLD,          PaymentRequest.REJECTED)])
        self.assertEqual([r.actor for r in rows],
                         [self.approver, self.scm, self.approver,
                          self.approver_b, self.approver_b])
        self.assertEqual([r.remark for r in rows],
                         ['GRN missing', 'GRN attached', 'satisfied',
                          'vendor dispute', 'order cancelled'])
        # actor_role_code is COPIED at write time, never joined.
        self.assertEqual(rows[0].actor_role_code, 'Finance')
        self.assertEqual(rows[1].actor_role_code, 'SCM')

    def test_each_action_writes_its_own_activity_code(self):
        self.hold()
        self.respond()
        self.approve()
        codes = set(ActivityLog.objects.filter(entity_type='PaymentRequest',
                                               entity_id=self.payment.pk)
                    .values_list('action_code', flat=True))
        self.assertEqual(codes, {'payment_request_held', 'payment_hold_answered',
                                 'payment_request_approved'})


# ---------------------------------------------------------------------------
# The order detail page
# ---------------------------------------------------------------------------

class DetailPageTests(ApprovalFixture):

    def test_an_approver_sees_approve_and_hold_on_a_pending_payment(self):
        html = self.detail(self.approver).content.decode()
        self.assertIn(self.approve_url(), html)
        self.assertIn(self.hold_url(), html)
        self.assertNotIn(self.reject_url(), html)
        self.assertNotIn(self.respond_url(), html)

    def test_the_requester_who_is_also_an_approver_sees_no_buttons_and_is_told_why(self):
        self.scm.is_payment_approver = True
        self.scm.save(update_fields=['is_payment_approver'])
        html = self.detail(self.scm).content.decode()
        self.assertNotIn(self.approve_url(), html)
        self.assertNotIn(self.hold_url(), html)
        self.assertIn('You raised this request', html)

    def test_a_held_payment_shows_its_reason_and_who_held_it(self):
        self.hold(reason='GRN 447 is missing')
        html = self.detail(self.approver).content.decode()
        self.assertIn('GRN 447 is missing', html)
        self.assertIn('o4_approver', html)
        self.assertIn(self.reject_url(), html)

    def test_scm_sees_respond_on_a_held_payment_and_the_response_afterwards(self):
        self.hold(reason='GRN missing')
        html = self.detail(self.scm).content.decode()
        self.assertIn(self.respond_url(), html)
        self.assertIn('GRN missing', html)

        self.respond(response='GRN 447 attached')
        html = self.detail(self.scm).content.decode()
        self.assertNotIn(self.respond_url(), html)
        self.assertIn('GRN 447 attached', html)

    def test_the_page_renders_with_no_stray_template_tags(self):
        self.hold(reason='a reason')
        for profile in (self.approver, self.scm, self.pm):
            html = self.detail(profile).content.decode()
            self.assertEqual(self.detail(profile).status_code, 200)
            for stray in ('{%', '%}', '{{', '}}', '{#'):
                self.assertNotIn(stray, html)


# ---------------------------------------------------------------------------
# Two approvers, one instant (Postgres only — SQLite ignores select_for_update)
# ---------------------------------------------------------------------------

@skipUnless(connection.vendor == 'postgresql',
            'select_for_update is a no-op on SQLite; run under the real settings.')
class ConcurrentDecisionTests(TransactionTestCase):
    """Two approvers click at the same instant on one request.

    WHAT THE LOCK ACTUALLY GUARANTEES, and it is not always a refusal. Approve and Hold
    are each legal from the other's outcome by design (approve from ON_HOLD, hold from
    APPROVED), so that pair does not race into a refusal — it SERIALISES into a valid
    two-step chain. What the lock buys there is that the second decision sees the first:
    without it both would read PENDING_APPROVAL and both ledger rows would claim to have
    moved it from there, which is a history of something that never happened.

    Where the second decision is genuinely not permitted — two approvals, the second
    arriving at APPROVED — one wins and the other IS refused. Both cases below.
    """
    reset_sequences = True

    def setUp(self):
        from .tests_vendor_order_raise import _project
        from .models import Vendor
        self.scm      = _profile('o4c_scm', 'SCM')
        self.pm       = _profile('o4c_pm', 'PM')
        self.app_a    = _approver('o4c_app_a')
        self.app_b    = _approver('o4c_app_b', role='CEO')
        self.vendor   = Vendor.objects.create(name='Conc Vendor', contact_person='R',
                                              phone='9000000002')
        self.project  = _project('Conc Site', self.pm)
        self.order    = _order(self, po='PO-CONC')
        self.payment  = _payment(self, self.order)

    def _race(self, *acts):
        """Fire each (profile, url_name, data) at the same instant and collect the
        status codes. Each thread closes its own connection, as TransactionTestCase
        requires."""
        results = {}
        barrier = threading.Barrier(len(acts))

        def act(index, profile, url_name, data):
            barrier.wait()
            try:
                results[index] = _client(profile).post(
                    reverse(url_name, args=[self.payment.pk]), data).status_code
            finally:
                connection.close()

        threads = [threading.Thread(target=act, args=(index, *spec))
                   for index, spec in enumerate(acts)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        return results

    def rows(self):
        return list(StatusTransition.objects.filter(
            subject_type=SUBJECT_PAYMENT_REQUEST,
            subject_id=self.payment.pk).order_by('pk'))

    def test_approve_and_hold_serialise_and_the_ledger_chains(self):
        results = self._race(
            (self.app_a, 'payment_approve', {'remark': ''}),
            (self.app_b, 'payment_hold',    {'reason': 'wait'}),
        )
        self.assertEqual(set(results.values()), {302})    # neither 500'd

        self.payment.refresh_from_db()
        # Exactly one status, and it is one of the two that were attempted.
        self.assertIn(self.payment.status,
                      (PaymentRequest.APPROVED, PaymentRequest.ON_HOLD))

        # THE POINT: the second row's from_status is the first row's to_status. Without
        # the lock both would read — and record — PENDING_APPROVAL.
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].from_status, PaymentRequest.PENDING_APPROVAL)
        self.assertEqual(rows[1].from_status, rows[0].to_status)
        self.assertEqual(rows[1].to_status, self.payment.status)

        if self.payment.status == PaymentRequest.ON_HOLD:
            self.assertEqual(PaymentRequestHold.objects.count(), 1)

    def test_two_approvals_at_once_leave_one_winner_and_one_refusal(self):
        """Here the second decision IS forbidden by its predicate — approve is not
        reachable from APPROVED — so the loser is refused rather than chained."""
        results = self._race(
            (self.app_a, 'payment_approve', {'remark': 'a'}),
            (self.app_b, 'payment_approve', {'remark': 'b'}),
        )
        self.assertEqual(set(results.values()), {302})    # a refusal, not a 500

        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, PaymentRequest.APPROVED)
        rows = self.rows()
        self.assertEqual(len(rows), 1)                    # only the winner wrote
        self.assertEqual(rows[0].from_status, PaymentRequest.PENDING_APPROVAL)
        self.assertIn(self.payment.approved_by, (self.app_a, self.app_b))
        self.assertEqual(rows[0].actor, self.payment.approved_by)
