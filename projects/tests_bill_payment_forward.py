"""Payments 5b — a contractor bill is forwarded to Finance; Finance acts on its payments;
SCM answers holds from the bill page.

What this file pins, and why each matters:

  * THE FORWARD WRITES WHAT 5a RULED (Q5): contractor_bill, vendor = the bill's
    contractor, project = the bill's site, PENDING_APPROVAL, one ledger row, one feed line.
  * THE BILL IS A CEILING (D-A57). Several payments may be forwarded, together never more
    than the bill; a rejected payment frees its money. The writer checks it under a lock
    on the bill row; the approve view checks it again under the same lock.
  * ONLY AN APPROVED BILL, ONLY SCM. Everyone else is a 403; an open, rejected or
    withdrawn bill is a message and writes nothing. A deleted site does NOT stop it
    (ruling Q3) — the page says so.
  * DUPLICATES: the key in the URL answers a repeat with the bill; the same bill and
    amount within 30 minutes warns, with an override.
  * FINANCE ACTS ON A BILL'S PAYMENT as on a PO/PI one — approve (full or partial),
    hold, reject, mark paid — and every action returns to the bill's payments section.
  * FINANCE READS A BILL IT IS ASKED TO PAY (D-A58), in full; never an unforwarded bill,
    a material approval or the approvals list.
  * THE BILL PAGE'S PAYMENTS SECTION: every full reader sees it, the PM read-only, SCM
    replies to a hold there; THE SITE ENGINEER SEES NO PAYMENT AND NO FIGURE.
  * NOTICES on a bill's payment reach the people a PO/PI one reaches, linking to the bill.
  * PO/PI PAYMENT ACTIONS BEHAVE EXACTLY AS BEFORE 5b. Every figure in
    PoPiActionPinTests — query count, status code, where the person lands, the message —
    was measured on HEAD 2c82561, before 5b touched order_views.py.

Run with:
    python manage.py test projects.tests_bill_payment_forward --settings=solarpms.test_settings
The lock test runs only under the real (Postgres) settings:
    python manage.py test projects.tests_bill_payment_forward.ConcurrentBillPaymentTests
"""
import threading
import time
import uuid
from datetime import timedelta
from decimal import Decimal
from unittest import mock, skipUnless

from django.db import connection, transaction
from django.test import TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from .approvals import apply_approval_decision, withdraw_approval_request
from .models import (
    ActivityLog, ApprovalRequest, ContractorBillDetail, Notification, PaymentRequest,
    PaymentRequestHold, Project, StatusTransition, SUBJECT_PAYMENT_REQUEST, Vendor,
    APPROVAL_APPROVED, APPROVAL_KIND_CONTRACTOR_BILL, APPROVAL_PARTY_PM,
    APPROVAL_PARTY_SITE_ENGINEER, APPROVAL_STEP_APPROVED, APPROVAL_STEP_REJECTED,
    VENDOR_KIND_CONTRACTOR,
)
from .permissions import approval_request_visibility_q, user_can_view_forwarded_bill
from .submission_guard import ALREADY_SUBMITTED
from .tests_approvals import SE_PHOTO
from .tests_contractor_bill_screens import SIGNED, ScreenFixture
from .tests_contractor_bills import BUCKET, _profile
from .tests_payment_queue import QueueFixture, _make_payment, _messages
from .tests_vendor_order_raise import _client


class BillPayFixture(ScreenFixture):
    """ScreenFixture's bill people (SCM, the bill's PM and Site Engineer, another PM and
    Site Engineer) and contractor, plus the payment people: an approver (Finance, holds
    the flag), a CEO approver, a Finance user without the flag, a CEO and an Admin.

    setUp raises a ₹12,500 bill (CB-7) on the fixture's site through the chokepoint and
    approves it — the Site Engineer confirms with a photo, the PM approves — so every test
    starts from an APPROVED bill with no payment."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.approver   = _profile('bp_approver', 'Finance', is_payment_approver=True)
        cls.approver_b = _profile('bp_approver_b', 'CEO', is_payment_approver=True)
        cls.fin_b      = _profile('bp_fin_b', 'Finance')
        cls.ceo        = _profile('bp_ceo', 'CEO')
        cls.admin      = _profile('bp_admin', 'Admin')

    def setUp(self):
        super().setUp()
        self.approval = self.approved_bill()
        self.detail = ContractorBillDetail.objects.get(request=self.approval)
        self.page_url = reverse('approval_detail', args=[self.approval.pk])
        self.bill_home = f'{self.page_url}#payments'
        self.form_url = reverse('approval_bill_payment', args=[self.approval.pk])

    def approved_bill(self, **overrides):
        approval = self.raise_bill(**overrides)
        apply_approval_decision(self.step(approval, APPROVAL_PARTY_SITE_ENGINEER),
                                APPROVAL_STEP_APPROVED, self.se, files=[dict(SE_PHOTO)])
        with self.settings(SUPABASE_BILLS_BUCKET=BUCKET):
            apply_approval_decision(self.step(approval, APPROVAL_PARTY_PM),
                                    APPROVAL_STEP_APPROVED, self.pm)
        approval.refresh_from_db()
        assert approval.status == APPROVAL_APPROVED
        return approval

    # -- acts ----------------------------------------------------------------
    def forward(self, amount='5000', profile=None, key=None, url=None, **extra):
        data = {'client_uuid': str(key or uuid.uuid4()), 'payment_amount': amount,
                'payment_note': 'Stage 1'}
        data.update(extra)
        return self.client_for(profile or self.scm).post(url or self.form_url, data)

    def forwarded(self, amount='5000'):
        """A payment on the bill, forwarded through the view; returns the row."""
        response = self.forward(amount)
        self.assertEqual(response.status_code, 302, _messages(response))
        return PaymentRequest.objects.filter(contractor_bill=self.detail).latest('pk')

    def post(self, profile, name, payment, **data):
        return self.client_for(profile).post(reverse(name, args=[payment.pk]), data)

    def hold(self, payment, reason='GRN missing'):
        response = self.post(self.approver, 'payment_hold', payment, reason=reason)
        self.assertEqual(response.status_code, 302)
        payment.refresh_from_db()
        return response

    def payments(self):
        return PaymentRequest.objects.filter(contractor_bill=self.detail)

    def ledger(self, payment):
        return list(StatusTransition.objects.filter(
            subject_type=SUBJECT_PAYMENT_REQUEST, subject_id=payment.pk).order_by('pk'))


# ===========================================================================
# A. The forward
# ===========================================================================

class ForwardTests(BillPayFixture):

    def test_the_form_is_keyed_and_defaults_to_what_is_left(self):
        client = self.client_for(self.scm)
        first = client.get(self.form_url)
        self.assertEqual(first.status_code, 302)
        self.assertTrue(first['Location'].startswith(f'{self.form_url}?key='))
        page = client.get(first['Location'])
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'name="payment_amount"')
        self.assertContains(page, 'value="12500.00"')
        self.assertContains(page, first['Location'].split('key=')[1])   # the hidden key
        self.forwarded('5000')
        page = client.get(client.get(self.form_url)['Location'])
        self.assertContains(page, 'value="7500.00"')

    def test_forwarding_writes_one_payment_as_ruled(self):
        response = self.forward('5000')
        self.assertEqual(response['Location'], self.bill_home)
        self.assertEqual(_messages(response),
                         ['Payment of ₹5000 requested against bill CB-7. It is with the '
                          'approvers.'])
        pay = self.payments().get()
        self.assertEqual(
            (pay.vendor_order_id, pay.vendor_id, pay.project_id, pay.status, pay.amount,
             pay.note, pay.requested_by_id, pay.approved_amount),
            (None, self.contractor.pk, self.site.pk, PaymentRequest.PENDING_APPROVAL,
             Decimal('5000'), 'Stage 1', self.scm.user.pk, None))
        [row] = self.ledger(pay)
        self.assertEqual((row.from_status, row.to_status, row.actor_id, row.project_id),
                         ('', PaymentRequest.PENDING_APPROVAL, self.scm.pk, self.site.pk))
        log = ActivityLog.objects.get(entity_type='PaymentRequest', entity_id=pay.pk)
        self.assertEqual((log.action_code, log.project_id),
                         ('payment_request_raised', self.site.pk))
        self.assertEqual(log.action, 'Raised payment request to Civil Co: ₹5000 (bill CB-7)')

    def test_a_bill_may_be_paid_in_several_never_above_its_amount(self):
        self.forwarded('5000')
        self.forwarded('7500')
        response = self.forward('0.01')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(_messages(response),
                         ["The payment (₹0.01) is more than the bill's balance still "
                          "available to request (₹0.00 of the ₹12500.00 billed)."])
        self.assertEqual(self.payments().count(), 2)

    def test_above_what_is_left_is_refused_and_writes_nothing(self):
        response = self.forward('12500.01')
        self.assertEqual(response.status_code, 400)
        self.assertIn("(₹12500.00 of the ₹12500.00 billed)", _messages(response)[0])
        self.assertFalse(self.payments().exists())
        self.assertContains(response, 'value="12500.01"', status_code=400)   # entry kept

    def test_a_rejected_payment_frees_its_money(self):
        pay = self.forwarded('12500')
        PaymentRequest.objects.filter(pk=pay.pk).update(status=PaymentRequest.REJECTED,
                                                        decision_reason='Wrong bill')
        self.assertEqual(self.forward('12500').status_code, 302)
        self.assertEqual(self.payments().count(), 2)

    def test_a_bad_amount_is_refused(self):
        for raw in ('', '0', '-5', 'abc', '1.234'):
            with self.subTest(amount=raw):
                response = self.forward(raw)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(_messages(response),
                                 ['The payment amount must be more than 0.'])
        self.assertFalse(self.payments().exists())

    def test_only_an_approved_bill_is_forwarded(self):
        still_open = self.raise_bill(bill=self.bill(bill_number='CB-8'))
        withdrawn = self.raise_bill(bill=self.bill(bill_number='CB-10'))
        with self.settings(SUPABASE_BILLS_BUCKET=BUCKET):
            withdraw_approval_request(withdrawn, self.scm, 'Duplicate.')
        rejected = self.raise_bill(bill=self.bill(bill_number='CB-11'))
        apply_approval_decision(self.step(rejected, APPROVAL_PARTY_SITE_ENGINEER),
                                APPROVAL_STEP_APPROVED, self.se, files=[dict(SE_PHOTO)])
        with self.settings(SUPABASE_BILLS_BUCKET=BUCKET):
            apply_approval_decision(self.step(rejected, APPROVAL_PARTY_PM),
                                    APPROVAL_STEP_REJECTED, self.pm, note='No.')
        for approval, word in ((still_open, 'open'), (withdrawn, 'withdrawn'),
                               (rejected, 'rejected')):
            url = reverse('approval_bill_payment', args=[approval.pk])
            for method in ('get', 'post'):
                with self.subTest(status=word, method=method):
                    response = (self.forward(url=url) if method == 'post'
                                else self.client_for(self.scm).get(url))
                    self.assertEqual(response.status_code, 302)
                    self.assertEqual(response['Location'],
                                     reverse('approval_detail', args=[approval.pk]))
                    self.assertEqual(_messages(response),
                                     [f'Only an approved bill can be sent to Finance for '
                                      f'payment. This bill is {word}. Nothing was changed.'])
        self.assertFalse(PaymentRequest.objects.exists())

    def test_nobody_but_scm_forwards(self):
        for profile in (self.approver, self.fin_b, self.approver_b, self.ceo, self.admin,
                        self.pm, self.se, self.pm_b):
            with self.subTest(role=profile.role, user=profile.user.username):
                self.assertEqual(self.client_for(profile).get(self.form_url).status_code, 403)
                self.assertEqual(self.forward(profile=profile).status_code, 403)
        self.assertFalse(PaymentRequest.objects.exists())

    def test_a_material_request_has_no_bill_to_pay(self):
        material = self.raise_material()
        url = reverse('approval_bill_payment', args=[material.pk])
        self.assertEqual(self.client_for(self.scm).get(url).status_code, 404)

    def test_a_deleted_site_is_still_paid_and_says_so(self):
        Project.objects.filter(pk=self.site.pk).update(is_deleted=True)
        client = self.client_for(self.scm)
        page = client.get(client.get(self.form_url)['Location'])
        self.assertContains(page, 'This site has been deleted.')
        self.assertEqual(self.forward('5000').status_code, 302)
        self.assertEqual(self.payments().count(), 1)
        self.assertContains(client.get(self.page_url), 'This site has been deleted.')

    def test_nothing_left_draws_no_form(self):
        self.forwarded('12500')
        client = self.client_for(self.scm)
        page = client.get(client.get(self.form_url)['Location'])
        self.assertContains(page, 'Nothing is left to request on this bill')
        self.assertNotContains(page, 'name="payment_amount"')
        self.assertNotContains(client.get(self.page_url), 'Request payment from Finance')


# ===========================================================================
# Duplicate protection
# ===========================================================================

class DuplicateTests(BillPayFixture):

    def test_a_repeated_key_answers_with_the_bill_and_writes_nothing(self):
        key = uuid.uuid4()
        self.assertEqual(self.forward('5000', key=key).status_code, 302)
        again = self.forward('5000', key=key)
        self.assertEqual(again.status_code, 302)
        self.assertEqual(again['Location'], self.bill_home)
        self.assertIn(ALREADY_SUBMITTED, ' '.join(_messages(again)))
        page = self.client_for(self.scm).get(self.form_url, {'key': str(key)})
        self.assertEqual(page['Location'], self.bill_home)
        self.assertEqual(self.payments().count(), 1)

    def test_the_same_bill_and_amount_within_30_minutes_warns(self):
        self.forwarded('5000')
        response = self.forward('5000')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'This looks like a payment request already raised.')
        self.assertContains(response, '₹5000.00 against bill CB-7, requested by')
        self.assertContains(response, 'name="confirm_duplicate"')
        self.assertEqual(self.payments().count(), 1)
        self.assertEqual(self.forward('5000', confirm_duplicate='1').status_code, 302)
        self.assertEqual(self.payments().count(), 2)

    def test_another_amount_an_old_one_or_a_rejected_one_does_not_warn(self):
        first = self.forwarded('3000')
        self.assertEqual(self.forward('4000').status_code, 302)          # another amount
        PaymentRequest.objects.filter(pk=first.pk).update(
            requested_date=timezone.now() - timedelta(minutes=31))
        self.assertEqual(self.forward('3000').status_code, 302)          # outside the window
        latest = self.payments().latest('pk')
        PaymentRequest.objects.filter(pk=latest.pk).update(
            status=PaymentRequest.REJECTED, decision_reason='x')
        self.assertEqual(self.forward('3000').status_code, 302)          # the match is rejected
        self.assertEqual(self.payments().count(), 4)


# ===========================================================================
# B. Finance acts on a bill's payment
# ===========================================================================

class FinanceActionTests(BillPayFixture):

    def test_approve_in_full_then_mark_paid(self):
        pay = self.forwarded('5000')
        response = self.post(self.approver, 'payment_approve', pay, approved_amount='5000')
        self.assertEqual(response['Location'], self.bill_home)
        pay.refresh_from_db()
        self.assertEqual((pay.status, pay.approved_amount, pay.approved_by_id),
                         (PaymentRequest.APPROVED, Decimal('5000'), self.approver.pk))
        response = self.post(self.fin_b, 'payment_mark_paid', pay,
                             payment_date=timezone.localdate().isoformat(),
                             payment_reference='UTR-55')
        self.assertEqual(response['Location'], f"{reverse('payment_queue')}?tab=Residential")
        pay.refresh_from_db()
        self.assertEqual((pay.status, pay.payment_reference),
                         (PaymentRequest.CONFIRMED, 'UTR-55'))
        self.assertEqual([r.to_status for r in self.ledger(pay)],
                         ['pending_approval', 'approved', 'confirmed'])
        log = ActivityLog.objects.get(action_code='payment_request_approved')
        self.assertEqual(log.action, 'Approved payment request of ₹5000.00 to Civil Co '
                                     '(bill CB-7)')

    def test_a_partial_approval_needs_a_reason_and_frees_the_rest(self):
        pay = self.forwarded('12500')
        refused = self.post(self.approver, 'payment_approve', pay, approved_amount='10000')
        self.assertIn('needs a reason', _messages(refused)[0])
        self.post(self.approver, 'payment_approve', pay, approved_amount='10000',
                  remark='Retention held')
        pay.refresh_from_db()
        self.assertEqual((pay.status, pay.approved_amount),
                         (PaymentRequest.APPROVED, Decimal('10000')))
        # The ₹2,500 not approved is free to request again.
        self.assertEqual(self.forward('2500').status_code, 302)

    def test_the_approve_ceiling_is_the_bill_amount(self):
        pay = self.forwarded('5000')
        # A payment raised past the ceiling by a shell or fixture: the approve view is
        # where it is caught.
        PaymentRequest.objects.create(
            contractor_bill=self.detail, project=self.site, vendor=self.contractor,
            amount=Decimal('12000'), requested_by=self.scm.user)
        response = self.post(self.approver, 'payment_approve', pay, approved_amount='5000')
        self.assertEqual(response['Location'], self.bill_home)
        self.assertEqual(_messages(response),
                         ['Approving ₹5000.00 would commit ₹17000.00 against the bill amount '
                          'of ₹12500.00; at most ₹500.00 can be approved. Nothing was '
                          'changed.'])
        pay.refresh_from_db()
        self.assertEqual(pay.status, PaymentRequest.PENDING_APPROVAL)

    def test_hold_answer_hold_again_reject(self):
        pay = self.forwarded('5000')
        self.assertEqual(self.hold(pay)['Location'], self.bill_home)
        self.assertEqual(pay.status, PaymentRequest.ON_HOLD)
        answered = self.post(self.scm, 'payment_hold_respond', pay, response='GRN sent')
        self.assertEqual(answered['Location'], self.bill_home)
        pay.refresh_from_db()
        self.assertEqual(pay.status, PaymentRequest.PENDING_APPROVAL)
        self.assertEqual(pay.holds.get().response, 'GRN sent')
        self.hold(pay, reason='Still short')
        rejected = self.post(self.approver, 'payment_reject', pay, reason='Duplicate')
        self.assertEqual(rejected['Location'], self.bill_home)
        pay.refresh_from_db()
        self.assertEqual((pay.status, pay.decision_reason),
                         (PaymentRequest.REJECTED, 'Duplicate'))
        self.assertEqual([r.to_status for r in self.ledger(pay)],
                         ['pending_approval', 'on_hold', 'pending_approval', 'on_hold',
                          'rejected'])

    def test_who_may_act(self):
        pay = self.forwarded('5000')
        for profile in (self.fin_b, self.pm, self.scm, self.se, self.admin):
            with self.subTest(action='approve', user=profile.user.username):
                self.assertEqual(self.post(profile, 'payment_approve', pay,
                                           approved_amount='5000').status_code, 403)
        self.hold(pay)
        for profile in (self.approver, self.fin_b, self.pm, self.se):
            with self.subTest(action='respond', user=profile.user.username):
                self.assertEqual(self.post(profile, 'payment_hold_respond', pay,
                                           response='x').status_code, 403)
        # PM_B is named nowhere on the bill: not even a reader.
        self.assertEqual(self.post(self.pm_b, 'payment_hold_respond', pay,
                                   response='x').status_code, 403)
        pay.refresh_from_db()
        self.assertEqual(pay.status, PaymentRequest.ON_HOLD)

    def test_the_queue_draws_the_actions_and_returns_to_itself(self):
        pay = self.forwarded('5000')
        queue = self.client_for(self.approver).get(reverse('payment_queue'),
                                                   {'tab': 'Residential'})
        row = next(r for r in queue.context['rows'] if r['payment'].pk == pay.pk)
        self.assertEqual((row['can_approve'], row['can_hold'], row['can_reject']),
                         (True, True, False))
        here = f"{reverse('payment_queue')}?tab=Residential"
        response = self.client_for(self.approver).post(
            reverse('payment_queue_action', args=[pay.pk, 'approve']),
            {'approved_amount': '5000', 'next': here})
        self.assertEqual(response['Location'], here)
        pay.refresh_from_db()
        self.assertEqual(pay.status, PaymentRequest.APPROVED)


# ===========================================================================
# C/E. The bill page's payments section
# ===========================================================================

class BillPageTests(BillPayFixture):

    def page(self, profile):
        return self.client_for(profile).get(self.page_url)

    def test_scm_sees_the_section_the_forward_button_and_the_reply_form(self):
        page = self.page(self.scm)
        self.assertContains(page, 'id="payments"')
        self.assertContains(page, 'No payment has been requested against this bill yet.')
        self.assertContains(page, f'href="{self.form_url}"')
        pay = self.forwarded('5000')
        self.hold(pay, reason='GRN missing')
        page = self.page(self.scm)
        self.assertContains(page, 'Bill ₹12500.00 · committed ₹5000.00 · paid ₹0.00')
        self.assertContains(page, 'still to request ₹7500.00')
        self.assertContains(page, 'GRN missing')
        self.assertContains(page, reverse('payment_hold_respond', args=[pay.pk]))
        self.assertNotContains(page, reverse('payment_approve', args=[pay.pk]))

    def test_an_unapproved_bill_has_no_payments_section(self):
        still_open = self.raise_bill(bill=self.bill(bill_number='CB-8'))
        page = self.client_for(self.scm).get(reverse('approval_detail', args=[still_open.pk]))
        self.assertNotContains(page, 'id="payments"')
        self.assertNotContains(page, 'Request payment from Finance')

    def test_a_finance_approver_reads_the_bill_in_full_with_the_approver_forms(self):
        pay = self.forwarded('5000')
        page = self.page(self.approver)
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, '₹12,500.00')                    # the amount
        self.assertContains(page, SIGNED)                          # the PDF
        self.assertContains(page, 'work.jpg')                      # the SE's photo
        self.assertContains(page, reverse('payment_approve', args=[pay.pk]))
        self.assertContains(page, reverse('payment_hold', args=[pay.pk]))
        self.assertNotContains(page, reverse('payment_hold_respond', args=[pay.pk]))
        self.assertNotContains(page, 'Request payment from Finance')

    def test_finance_without_the_flag_reads_it_without_buttons(self):
        pay = self.forwarded('5000')
        page = self.page(self.fin_b)
        self.assertContains(page, 'id="payments"')
        self.assertContains(page, '₹12,500.00')
        for name in ('payment_approve', 'payment_hold', 'payment_reject',
                     'payment_hold_respond'):
            self.assertNotContains(page, reverse(name, args=[pay.pk]))

    def test_the_pm_on_the_bill_reads_the_payments_without_actions(self):
        pay = self.forwarded('5000')
        self.hold(pay)
        page = self.page(self.pm)
        self.assertContains(page, 'id="payments"')
        self.assertContains(page, 'GRN missing')
        for name in ('payment_approve', 'payment_hold', 'payment_reject',
                     'payment_hold_respond'):
            self.assertNotContains(page, reverse(name, args=[pay.pk]))
        self.assertNotContains(page, 'Request payment from Finance')

    def test_the_site_engineer_sees_no_payment_and_no_figure(self):
        pay = self.forwarded('5000')
        self.hold(pay, reason='GRN missing')
        page = self.page(self.se)
        self.assertEqual(page.status_code, 200)
        # Money is matched with its rupee sign: the base template's own JavaScript
        # carries a bare 5000 (a dismiss timeout).
        for text in ('id="payments"', 'Payments', 'GRN missing', '₹5000', '₹5,000',
                     '12,500', '12500', 'Request payment', 'committed',
                     reverse('payment_hold_respond', args=[pay.pk])):
            with self.subTest(text=text):
                self.assertNotContains(page, text)
        self.assertEqual(self.client_for(self.se).get(self.form_url).status_code, 403)

    def test_the_section_costs_the_same_for_one_payment_or_five(self):
        def count():
            client = self.client_for(self.scm)
            client.get(self.page_url)
            with CaptureQueriesContext(connection) as ctx:
                self.assertEqual(client.get(self.page_url).status_code, 200)
            return len(ctx.captured_queries)
        self.forwarded('1000')
        self.hold(self.payments().get())
        one = count()
        for amount in ('1001', '1002', '1003', '1004'):
            self.hold(self.forwarded(amount))
        self.assertEqual(count(), one)


# ===========================================================================
# D. Finance reads a bill it is asked to pay (D-A58)
# ===========================================================================

class FinanceAccessTests(BillPayFixture):

    def test_finance_opens_a_bill_only_once_it_is_forwarded(self):
        for profile in (self.approver, self.fin_b):
            with self.subTest(user=profile.user.username, forwarded=False):
                self.assertEqual(self.client_for(profile).get(self.page_url).status_code, 403)
        self.forwarded('5000')
        for profile in (self.approver, self.fin_b):
            with self.subTest(user=profile.user.username, forwarded=True):
                self.assertEqual(self.client_for(profile).get(self.page_url).status_code, 200)

    def test_never_a_material_approval_nor_the_lists(self):
        self.forwarded('5000')
        material = self.raise_material()
        client = self.client_for(self.approver)
        self.assertEqual(client.get(reverse('approval_detail', args=[material.pk])).status_code,
                         403)
        self.assertEqual(client.get(reverse('approval_list')).status_code, 403)
        self.assertEqual(client.get(reverse('approval_aging')).status_code, 403)

    def test_reading_is_not_acting(self):
        self.forwarded('5000')
        client = self.client_for(self.approver)
        self.assertEqual(client.post(reverse('approval_withdraw', args=[self.approval.pk]),
                                     {'note': 'x'}).status_code, 403)
        self.assertEqual(client.get(reverse('approval_resubmit',
                                            args=[self.approval.pk])).status_code, 403)

    def test_the_list_rule_keeps_in_step(self):
        unforwarded = self.approved_bill(bill=self.bill(bill_number='CB-12'))
        self.forwarded('5000')
        rows = set(ApprovalRequest.objects.filter(
            approval_request_visibility_q(self.approver.user)).distinct()
            .values_list('pk', flat=True))
        self.assertEqual(rows, {self.approval.pk})
        self.assertNotIn(unforwarded.pk, rows)

    def test_the_admission_costs_one_query_and_only_for_finance_on_a_bill(self):
        self.forwarded('5000')
        material = self.raise_material()
        for profile, request, queries, answer in (
                (self.approver, self.approval, 1, True),
                (self.approver, material, 0, False),
                (self.scm, self.approval, 0, False),
                (self.pm, self.approval, 0, False),
                (self.ceo, self.approval, 0, False)):
            with self.subTest(user=profile.user.username, kind=request.kind):
                with self.assertNumQueries(queries):
                    self.assertEqual(user_can_view_forwarded_bill(profile.user, request),
                                     answer)


# ===========================================================================
# F. Notices
# ===========================================================================

class NoticeTests(BillPayFixture):

    def notices(self, act):
        """The (recipient pk, link) of every notice `act` sends, after commit."""
        Notification.objects.all().delete()
        with self.captureOnCommitCallbacks(execute=True):
            act()
        return set(Notification.objects.values_list('recipient_id', 'link'))

    def test_each_move_reaches_the_po_pi_recipients_and_links_to_the_bill(self):
        approvers = {self.approver.pk, self.approver_b.pk}
        link = self.page_url
        pay = None

        def forward():
            nonlocal pay
            pay = self.forwarded('12500')
        self.assertEqual(self.notices(forward), {(pk, link) for pk in approvers})
        self.assertEqual(self.notices(lambda: self.hold(pay)), {(self.scm.pk, link)})
        self.assertEqual(
            self.notices(lambda: self.post(self.scm, 'payment_hold_respond', pay,
                                           response='Sent')),
            {(pk, link) for pk in approvers})
        self.assertEqual(
            self.notices(lambda: self.post(self.approver, 'payment_approve', pay,
                                           approved_amount='10000', remark='Retention')),
            {(self.scm.pk, link)})
        self.assertEqual(
            self.notices(lambda: self.post(self.fin_b, 'payment_mark_paid', pay,
                                           payment_date=timezone.localdate().isoformat(),
                                           payment_reference='UTR-1')),
            {(self.scm.pk, link)})
        # The Site Engineer is told nothing about the bill's money.
        self.assertFalse(Notification.objects.filter(recipient=self.se,
                                                     link=link).exists())


# ===========================================================================
# The bill-row lock (Postgres only)
# ===========================================================================

@skipUnless(connection.vendor == 'postgresql',
            'select_for_update is a no-op on SQLite; this race only exists on Postgres')
class ConcurrentBillPaymentTests(TransactionTestCase):
    """Two SCM forwards that together exceed the bill, at the same moment. The bill-row
    lock must make the second wait for the first and then see its payment.

    The balance read is slowed (a sleep inside committed_total) so that, WITHOUT the
    lock, both would read nothing committed before either wrote — two payments, ₹22,500
    against a ₹12,500 bill. With the lock exactly one is created. The two amounts differ,
    so the duplicate warning never decides it.
    """

    def test_two_simultaneous_forwards_never_exceed_the_bill(self):
        scm = _profile('bp_race_scm', 'SCM')
        contractor = Vendor.objects.create(name='Race Civil', contact_person='R',
                                           phone='9000000031', kind=VENDOR_KIND_CONTRACTOR)
        from .tests_contractor_bills import _site
        site = _site('Race Bill Site')
        approval = ApprovalRequest.objects.create(
            kind=APPROVAL_KIND_CONTRACTOR_BILL, status=APPROVAL_APPROVED,
            closed_at=timezone.now(), title='Race bill', description='x',
            vendor=contractor, raised_by=scm)
        bill = ContractorBillDetail.objects.create(
            request=approval, project=site, amount=Decimal('12500'), bill_number='CB-R',
            bill_date=timezone.localdate(), pdf_file_name='r.pdf', pdf_bucket=BUCKET,
            pdf_path='r/r.pdf', pdf_size_kb=1)
        url = reverse('approval_bill_payment', args=[approval.pk])

        from . import payments as p
        real = p.committed_total

        def slow(rows):
            result = real(rows)
            time.sleep(0.5)
            return result

        barrier, statuses = threading.Barrier(2), []

        def forward(amount):
            try:
                client = _client(scm)
                barrier.wait()
                statuses.append(client.post(url, {
                    'client_uuid': str(uuid.uuid4()), 'payment_amount': amount,
                }).status_code)
            finally:
                connection.close()

        with mock.patch('projects.payments.committed_total', side_effect=slow):
            threads = [threading.Thread(target=forward, args=(amount,))
                       for amount in ('10000', '12500')]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        self.assertEqual(sorted(statuses), [302, 400])
        self.assertEqual(PaymentRequest.objects.filter(contractor_bill=bill).count(), 1)


# ===========================================================================
# PO/PI payment actions are what they were
# ===========================================================================

class _Rollback(Exception):
    pass


class PoPiActionPinTests(QueueFixture):
    """QueueFixture's PO/PI payments only. Each action is posted by a client warmed with
    one GET (its session and cache reads excluded), counted, and rolled back, so every
    row starts from the fixture."""

    def act(self, profile, url, data=None, method='post', prep=None):
        client = _client(profile)
        client.get(reverse('payment_queue'), {'tab': 'CAPEX'})       # warm the session
        result = None
        try:
            with transaction.atomic():
                if prep is not None:
                    prep()
                with CaptureQueriesContext(connection) as ctx:
                    response = getattr(client, method)(url, data or {})
                result = (len(ctx.captured_queries), response.status_code,
                          response.get('Location', ''), _messages(response))
                raise _Rollback
        except _Rollback:
            pass
        return result

    def held(self):
        PaymentRequest.objects.filter(pk=self.res_pay.pk).update(status=PaymentRequest.ON_HOLD)
        PaymentRequestHold.objects.create(payment_request=self.res_pay, reason='Why',
                                          held_by=self.approver)

    def over_committed(self):
        _make_payment(self, self.res_order, amount='80000')

    def scenarios(self):
        res, url = self.res_pay, lambda name, pk: reverse(name, args=[pk])
        add_url = url('vendor_order_add_payment', self.res_order.pk)
        here = f"{reverse('payment_queue')}?tab=Residential&status=on_hold"
        return {
            'approve_full':     (self.approver, url('payment_approve', res.pk),
                                 {'approved_amount': '20000'}, 'post', None),
            'approve_partial':  (self.approver, url('payment_approve', res.pk),
                                 {'approved_amount': '15000', 'remark': 'part'}, 'post', None),
            'approve_no_reason': (self.approver, url('payment_approve', res.pk),
                                  {'approved_amount': '15000'}, 'post', None),
            'approve_over_total': (self.approver, url('payment_approve', res.pk),
                                   {'approved_amount': '20000'}, 'post', self.over_committed),
            'approve_get':      (self.approver, url('payment_approve', res.pk), None, 'get',
                                 None),
            'approve_no_flag':  (self.fin_b, url('payment_approve', res.pk),
                                 {'approved_amount': '20000'}, 'post', None),
            'hold':             (self.approver, url('payment_hold', res.pk),
                                 {'reason': 'GRN'}, 'post', None),
            'hold_no_reason':   (self.approver, url('payment_hold', res.pk), {}, 'post', None),
            'reject_pending':   (self.approver, url('payment_reject', res.pk),
                                 {'reason': 'No'}, 'post', None),
            'reject_held':      (self.approver, url('payment_reject', res.pk),
                                 {'reason': 'No'}, 'post', self.held),
            'respond':          (self.scm, url('payment_hold_respond', res.pk),
                                 {'response': 'GRN attached'}, 'post', self.held),
            'respond_approver': (self.approver, url('payment_hold_respond', res.pk),
                                 {'response': 'x'}, 'post', self.held),
            'queue_hold':       (self.approver,
                                 reverse('payment_queue_action', args=[res.pk, 'hold']),
                                 {'reason': 'GRN', 'next': here}, 'post', None),
            'add_payment':      (self.scm, add_url,
                                 {'client_uuid': str(uuid.uuid4()),
                                  'payment_amount': '1000'}, 'post', None),
            'add_payment_over': (self.scm, add_url,
                                 {'client_uuid': str(uuid.uuid4()),
                                  'payment_amount': '80000'}, 'post', None),
            'add_payment_get':  (self.scm, add_url, None, 'get', None),
            'mark_paid':        (self.fin_b, url('payment_mark_paid', self.opex_pay.pk),
                                 {'payment_date': timezone.localdate().isoformat(),
                                  'payment_reference': 'UTR-9'}, 'post', None),
            'order_page':       (self.approver, url('vendor_order_detail', self.res_order.pk),
                                 None, 'get', None),
        }

    #: (queries, status, Location, messages), measured on HEAD 2c82561 before any 5b
    #: edit. {order} is the Residential order's page; a keyed GET's key is random, so
    #: only its prefix is compared.
    MEASURED_ON_HEAD = {
        'approve_full':      (17, 302, '{order}', ['Payment request of ₹20000.00 approved.']),
        'approve_partial':   (17, 302, '{order}',
                              ['Payment request of ₹20000.00 approved for ₹15000.00.']),
        'approve_no_reason': (7, 302, '{order}',
                              ['Approving ₹15000.00 of the ₹20000.00 requested needs a '
                               'reason. Nothing was changed.']),
        'approve_over_total': (12, 302, '{order}',
                               ['Approving ₹20000.00 would commit ₹100000.00 against the '
                                'order total of ₹90000.00; at most ₹10000.00 can be '
                                'approved. Nothing was changed.']),
        'approve_get':       (7, 302, '{order}', []),
        'approve_no_flag':   (7, 403, '', []),
        'hold':              (16, 302, '{order}',
                              ['Payment request held. SCM has been asked to respond.']),
        'hold_no_reason':    (7, 302, '{order}',
                              ['A hold must say why — the reason is what SCM answers.']),
        'reject_pending':    (10, 302, '{order}',
                              ['This payment request is now "Awaiting approval", and that '
                               'action is no longer available on it.']),
        'reject_held':       (15, 302, '{order}', ['Payment request rejected.']),
        'respond':           (18, 302, '{order}',
                              ['Response recorded. The request is back with the approver.']),
        'respond_approver':  (7, 403, '', []),
        'queue_hold':        (16, 302, '/payments/?tab=Residential&status=on_hold',
                              ['Payment request held. SCM has been asked to respond.']),
        'add_payment':       (15, 302, '{order}',
                              ['Payment of ₹1000 requested against order PO-RES-1.']),
        'add_payment_over':  (13, 400, '',
                              ['The payment (₹80000) is more than the balance still '
                               'available to request (₹70000.00).']),
        'add_payment_get':   (6, 302, '{order}payments/new/?key=', []),
        'mark_paid':         (11, 302, '/payments/?tab=OPEX',
                              ['Payment of ₹20000.00 to Kiran Cables marked paid.']),
        'order_page':        (13, 200, '', []),
    }

    def test_every_po_pi_action_is_as_measured_on_head(self):
        order = reverse('vendor_order_detail', args=[self.res_order.pk])
        scenarios = self.scenarios()
        self.assertEqual(set(scenarios), set(self.MEASURED_ON_HEAD))
        for name, (profile, url, data, method, prep) in scenarios.items():
            count, status, location, messages = self.MEASURED_ON_HEAD[name]
            with self.subTest(action=name):
                got = self.act(profile, url, data, method, prep)
                location = location.format(order=order)
                self.assertEqual(got[:2], (count, status))
                if name == 'add_payment_get':
                    self.assertTrue(got[2].startswith(location), got[2])
                else:
                    self.assertEqual(got[2], location)
                self.assertEqual(got[3], messages)
