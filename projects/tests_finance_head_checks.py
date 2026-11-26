"""
FIN-H-B5 — the Finance Head page's data-trust checks and "Who recorded what"
(finance_head_checks.py and its two sections on /finance/head/).

THE RULES PINNED HERE (rulings H18, H20, H60-H62; B5 answers 1-11):
  * Seven checks, each counting only items the item list draws (count == items opened),
    test and soft-deleted sites excluded, a count of 0 drawn without a link.
  * "Recorded outside Finance" is gone (withdrawn by H65 / FIN-H-B5-1, removed in
    FIN-M-B5-8 K7); an old ?check=by_coordinator link shows the normal list.
  * Amount changed after Received: amount lines more than 60 s after the receipt.
  * Late invoice / late payment at 6 vs 7 days; undated rows reported, not counted.
  * Payment with no vendor invoice: order-backed only; a bill payment never.
  * Who recorded what: every active Finance user, anyone else with an act in the period
    (receipt, invoice, approval, hold, payment) with their role at the time; alphabetical;
    older names matched to a user only when exactly one active user has that full name;
    "N without amount"; H21's empty-payments state.
  * The page costs 23 queries at 3/0, 30/0 and 30/10, and with a ?check= chosen.

Real fixtures throughout (the FIN-H-B2 / B3 / B4 builders).

    python manage.py test projects.tests_finance_head_checks --settings=solarpms.test_settings
"""
from datetime import timedelta
from decimal import Decimal

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from .finance_head_checks import CHECK_KEYS, finance_head_checks
from .models import (SUBJECT_PAYMENT_MILESTONE, SUBJECT_PAYMENT_REQUEST,
                     VENDOR_ORDER_DOC_INVOICE, ActivityLog, PaymentMilestone,
                     PaymentRequest, PaymentRequestHold, StatusTransition,
                     VendorOrderDocument)
from .tests_finance_head_items import ItemsFixture
from .tests_finance_head_page import URL
from .tests_finance_work import _profile
from .utils import INVOICE_DUE_TRIGGERS

PO_BC = INVOICE_DUE_TRIGGERS['M2']
D = Decimal
COORD = 'Project Coordinator'


class ChecksFixture(ItemsFixture):

    def checks(self):
        return {c['key']: c for c in finance_head_checks('month', today=self.today)['checks']}

    def ids(self, key):
        return set(self.checks()[key]['items'])

    def who(self, period='30d'):
        return finance_head_checks(period, today=self.today)['who']

    def who_row(self, name, period='30d'):
        rows = [r for r in self.who(period)['rows'] if r['name'] == name]
        self.assertEqual(len(rows), 1, f'{name} has {len(rows)} rows')
        return rows[0]

    def received_by(self, site, name, actor, role, at, got='100'):
        """A Received milestone whose receipt the ledger records (since mid-September)."""
        m = self.received(site, name, got, timezone.localtime(at).date())
        # Written here, not through ItemsFixture.transition, which swaps a None actor
        # (an automatic move) for the Finance user.
        StatusTransition.objects.create(
            subject_type=SUBJECT_PAYMENT_MILESTONE, subject_id=m.pk,
            from_status=PaymentMilestone.PENDING, to_status=PaymentMilestone.RECEIVED,
            actor=actor, actor_role_code=role, reason_code='milestone_sync', project=site,
            occurred_at=at)
        return m

    def older_receipt(self, site, name, who_name, role, at, got='100'):
        """A Received milestone known only from the older auto-mark line (no entity_id,
        no transition), as the pre-ledger task sync wrote it."""
        m = self.received(site, name, got, timezone.localtime(at).date())
        self.log(site, f"Milestone {name} auto-marked Received via completion of Finance "
                       f"task 'Advance Payment' by {who_name} ({role})", at)
        return m

    def amount_line(self, site, text, at, milestone=None):
        """An amount-change line: entity-linked (PM edit) when `milestone` is given, else
        BD's site line."""
        line = ActivityLog.objects.create(
            project=site, actor=self.bd, action=text, entity_type='Milestone',
            entity_id=milestone.pk if milestone else None,
            action_code='milestone_amounts_set')
        ActivityLog.objects.filter(pk=line.pk).update(timestamp=at)
        return line

    def invoice_doc(self, payment):
        VendorOrderDocument.objects.create(
            order=payment.vendor_order, doc_type=VENDOR_ORDER_DOC_INVOICE,
            invoice_number=f'INV-{payment.pk}', invoice_amount=D('100'), file_name='i.pdf',
            bucket='b', path=f'o/{payment.pk}.pdf', uploaded_by=self.scm)

    def approved(self, days_ago, **fields):
        fields.setdefault('sites', [self.site()])
        return self.pay(status=PaymentRequest.APPROVED, approved='100',
                        approved_at=self.ago(days_ago) if days_ago is not None else None,
                        **fields)


# ---------------------------------------------------------------------------
# The constant (B5 answer 3)
# ---------------------------------------------------------------------------

class ConstantTests(ChecksFixture):

    def test_seven_checks_in_the_page_order(self):
        # FIN-M-B5-8 K7: by_coordinator withdrawn; the order of the rest is unchanged.
        self.assertEqual(CHECK_KEYS, (
            'no_amount', 'received_no_amount', 'invoiced_no_number',
            'paid_no_vendor_invoice', 'late_invoice', 'late_payment',
            'changed_after_received'))


# ---------------------------------------------------------------------------
# Milestone checks
# ---------------------------------------------------------------------------

class AmountChecksTests(ChecksFixture):

    def test_no_amount_counts_live_rows_only(self):
        counted = self.milestone(self.site(), 'M1')
        self.milestone(self.site(), 'M2', amount=D('10'))
        self.milestone(self.site(is_test=True), 'M1')
        self.milestone(self.site(is_deleted=True), 'M1')
        # H26 / answer 4: the list never draws a Pending milestone on a Draft site, so
        # the check does not count it either; a Received one there is drawn and counted.
        self.milestone(self.site(status='Draft'), 'M1')
        received_draft = self.received(self.site(status='Draft'), 'M1', '5', self.today)
        self.assertEqual(self.ids('no_amount'),
                         {f'milestone-{counted.pk}', f'milestone-{received_draft.pk}'})

    def test_received_no_amount(self):
        site = self.site()
        blank = self.received(site, 'M1', None, self.today)
        self.received(site, 'M2', '500', self.today)
        self.received(self.site(is_test=True), 'M1', None, self.today)
        self.assertEqual(self.ids('received_no_amount'), {f'milestone-{blank.pk}'})

    def test_invoiced_no_number_is_m2_and_m3_only(self):
        site = self.site()
        m2 = self.milestone(site, 'M2', status=PaymentMilestone.INVOICED)
        self.milestone(site, 'M3', status=PaymentMilestone.INVOICED, invoice_number='INV-9')
        self.milestone(site, 'M1', status=PaymentMilestone.INVOICED)   # never has a number
        self.milestone(self.site(is_deleted=True), 'M2', status=PaymentMilestone.INVOICED)
        self.assertEqual(self.ids('invoiced_no_number'), {f'milestone-{m2.pk}'})


class WithdrawnCheckTests(ChecksFixture):
    """FIN-M-B5-8 K7 (H65 / FIN-H-B5-1): "Recorded outside Finance" flagged BD and the PM
    side for recording client money, which is their job (FR1, FR8). It is gone, and an
    old link to it falls back to the normal list."""

    def test_gone_and_an_old_link_falls_back_to_the_normal_list(self):
        self.received_by(self.site(), 'M1', self.coord, COORD, self.ago(3))
        self.older_receipt(self.site(), 'M1', 'Fw_Coord', COORD, self.ago(80))
        self.assertNotIn('by_coordinator', self.checks())
        client = self.client_for(self.head)
        old = client.get(URL + '?check=by_coordinator').content.decode()
        plain = client.get(URL).content.decode()
        self.assertNotIn('Recorded outside Finance', old)
        self.assertNotIn('data-items-check', old)
        section = lambda html: (html.split('<!-- fhi:start -->', 1)[1]    # noqa: E731
                                .split('<!-- fhi:end -->', 1)[0])
        self.assertEqual(section(old), section(plain))


class ChangedAfterReceivedTests(ChecksFixture):

    def test_entity_line_after_received_counted_within_60s_and_before_not(self):
        received_at = self.ago(10)
        later = self.received_by(self.site(), 'M1', self.finance, 'Finance', received_at)
        self.amount_line(later.project, 'M1: amount ₹2,000 → ₹3,000',
                         received_at + timedelta(hours=2), milestone=later)
        same_save = self.received_by(self.site(), 'M1', self.pm, 'PM', received_at)
        self.amount_line(same_save.project, 'M1: amount ₹1 → ₹2',
                         received_at + timedelta(seconds=30), milestone=same_save)
        before = self.received_by(self.site(), 'M1', self.finance, 'Finance', received_at)
        self.amount_line(before.project, 'M1: amount ₹1 → ₹2',
                         received_at - timedelta(days=1), milestone=before)
        check = self.checks()['changed_after_received']
        self.assertEqual(set(check['items']), {f'milestone-{later.pk}'})
        self.assertIn('M1: amount ₹2,000 → ₹3,000', check['items'][f'milestone-{later.pk}'])

    def test_older_bd_line_after_the_auto_mark_counted_on_its_own_mx(self):
        site = self.site()
        mark = self.ago(90)
        m1 = self.older_receipt(site, 'M1', 'Fw_Fin', 'Finance', mark)
        m2 = self.older_receipt(site, 'M2', 'Fw_Fin', 'Finance', mark)
        self.amount_line(site, 'Set milestone amounts: M1=₹37918', mark + timedelta(days=5))
        self.amount_line(site, 'Set milestone amounts: M2 ₹1 → ₹2', mark - timedelta(days=5))
        self.assertEqual(self.ids('changed_after_received'), {f'milestone-{m1.pk}'})
        self.assertNotIn(f'milestone-{m2.pk}', self.ids('changed_after_received'))

    def test_bd_line_on_a_deleted_site_is_out(self):
        site = self.site(is_deleted=True)
        mark = self.ago(90)
        self.older_receipt(site, 'M1', 'Fw_Fin', 'Finance', mark)
        self.amount_line(site, 'Set milestone amounts: M1=₹1', mark + timedelta(days=5))
        self.assertEqual(self.checks()['changed_after_received']['count'], 0)


class LateInvoiceTests(ChecksFixture):

    def test_six_days_not_counted_seven_counted(self):
        late_site, fresh_site = self.site(), self.site()
        late = self.milestone(late_site, 'M2')
        self.trigger(late_site, PO_BC, done_days_ago=7)
        self.milestone(fresh_site, 'M2')
        self.trigger(fresh_site, PO_BC, done_days_ago=6)
        check = self.checks()['late_invoice']
        self.assertEqual(set(check['items']), {f'milestone-{late.pk}'})
        self.assertIn('7 days ago', check['items'][f'milestone-{late.pk}'])

    def test_undated_due_reported_not_counted(self):
        self.milestone(self.site(), 'M2')            # no trigger task: due, no date
        check = self.checks()['late_invoice']
        self.assertEqual(check['count'], 0)
        self.assertEqual(check['extra'], '1 due with no trigger date')

    def test_m1_draft_and_test_sites_never_counted(self):
        old = self.site(activated_at=self.ago(60))
        self.milestone(old, 'M1')                    # due 60 days, but M1 has no invoice
        draft = self.site(status='Draft')
        self.milestone(draft, 'M2')
        self.trigger(draft, PO_BC, done_days_ago=30)
        test = self.site(is_test=True)
        self.milestone(test, 'M2')
        self.trigger(test, PO_BC, done_days_ago=30)
        check = self.checks()['late_invoice']
        self.assertEqual((check['count'], check['extra']), (0, ''))


# ---------------------------------------------------------------------------
# Payment checks
# ---------------------------------------------------------------------------

class PaymentChecksTests(ChecksFixture):

    def test_late_payment_six_vs_seven_days_and_undated(self):
        late = self.approved(7)
        self.approved(6)
        self.approved(None)
        self.approved(30, sites=[self.site(is_test=True)])
        check = self.checks()['late_payment']
        self.assertEqual(set(check['items']), {f'payment-{late.pk}'})
        self.assertEqual(check['extra'], '1 approved request with no approval date')

    def test_paid_no_vendor_invoice_order_payments_only(self):
        approved = self.approved(1)
        paid = self.pay(sites=[self.site()], status=PaymentRequest.CONFIRMED, approved='100',
                        payment_date=self.today)
        with_doc = self.approved(1)
        self.invoice_doc(with_doc)
        self.pay(sites=[self.site()])                                    # awaiting
        bill = self.bill(self.site(), '500')
        self.pay(bill=bill, status=PaymentRequest.APPROVED, approved='100')
        self.approved(1, sites=[self.site(is_deleted=True)])
        self.assertEqual(self.ids('paid_no_vendor_invoice'),
                         {f'payment-{approved.pk}', f'payment-{paid.pk}'})


class ZeroLinkTests(ChecksFixture):

    def test_a_count_of_zero_has_no_link_and_a_count_links_to_its_items(self):
        m = self.milestone(self.site(), 'M1')
        html = self.client_for(self.head).get(URL + '?period=fy').content.decode()
        trust = html.split('<!-- fht:start -->', 1)[1].split('<!-- fht:end -->', 1)[0]
        self.assertIn('href="?period=fy&amp;check=no_amount#items"', trust)
        self.assertIn('data-check-zero', trust)
        self.assertNotIn('check=late_payment', trust)
        self.assertEqual(self.checks()['no_amount']['items'], {f'milestone-{m.pk}': ''})


# ---------------------------------------------------------------------------
# Who recorded what
# ---------------------------------------------------------------------------

class WhoRecordedTests(ChecksFixture):

    def test_every_active_finance_user_listed_even_with_nothing(self):
        gone = _profile('fh_gone', 'Finance')
        gone.is_active = False
        gone.save()
        names = [r['name'] for r in self.who()['rows']]
        self.assertIn('Fw_Fin', names)
        self.assertIn('Fh_Head', names)
        self.assertNotIn('Fh_Gone', names)
        self.assertNotIn('Fw_Coord', names)
        head = self.who_row('Fh_Head')
        self.assertTrue(head['head'])
        self.assertEqual((head['received_n'], head['roles']), (0, ['Finance']))

    def test_a_coordinator_appears_only_in_a_period_with_a_receipt(self):
        self.received_by(self.site(), 'M1', self.coord, COORD, self.ago(40))
        self.assertNotIn('Fw_Coord', [r['name'] for r in self.who('30d')['rows']])
        self.received_by(self.site(), 'M1', self.coord, COORD, self.ago(2), got=None)
        row = self.who_row('Fw_Coord', '30d')
        self.assertEqual((row['received_n'], row['received_unset'], row['roles']),
                         (1, 1, [COORD]))

    def test_alphabetical_never_by_volume(self):
        busy = _profile('aaa_busy', 'Finance')
        for _ in range(3):
            self.received_by(self.site(), 'M1', self.finance, 'Finance', self.ago(1))
        self.received_by(self.site(), 'M1', busy, 'Finance', self.ago(1))
        names = [r['name'] for r in self.who()['rows']]
        self.assertEqual(names, sorted(names, key=str.casefold))
        self.assertEqual(names[0], 'Aaa_Busy')

    def test_older_name_matched_to_one_user_else_kept_as_text(self):
        self.older_receipt(self.site(), 'M1', 'Fw_Coord', COORD, self.ago(2), got='700')
        self.older_receipt(self.site(), 'M1', 'Someone Left', 'PM', self.ago(2))
        matched = self.who_row('Fw_Coord')
        self.assertEqual((matched['key'], matched['received_sum'], matched['older']),
                         (f'p{self.coord.pk}', D('700'), True))
        text = self.who_row('Someone Left')
        self.assertEqual((text['key'], text['roles']), ('t-Someone Left', ['PM']))

    def test_two_active_users_with_the_name_keep_the_text(self):
        twin = _profile('twin', 'PM')
        twin.user.first_name = 'Fw_Coord'
        twin.user.save()
        self.older_receipt(self.site(), 'M1', 'Fw_Coord', COORD, self.ago(2))
        self.assertEqual(self.who_row('Fw_Coord')['key'], 't-Fw_Coord')

    def test_invoices_approvals_holds_and_payments_by_person(self):
        site = self.site()
        m = self.milestone(site, 'M2', status=PaymentMilestone.INVOICED, invoice_number='I-1')
        self.transition(SUBJECT_PAYMENT_MILESTONE, m.pk, PaymentMilestone.INVOICED,
                        frm=PaymentMilestone.PENDING, actor=self.finance, at=self.ago(1))
        appr = self.approved(1)
        PaymentRequest.objects.filter(pk=appr.pk).update(approved_by=self.ceo)
        self.transition(SUBJECT_PAYMENT_REQUEST, appr.pk, PaymentRequest.APPROVED,
                        frm=PaymentRequest.PENDING_APPROVAL, actor=self.ceo, role='CEO',
                        at=self.ago(1))
        self.pay(sites=[site], status=PaymentRequest.ON_HOLD, hold_at=self.ago(1))
        paid = self.pay(sites=[site], status=PaymentRequest.CONFIRMED, approved='250',
                        amount='300', payment_date=self.today)
        PaymentRequest.objects.filter(pk=paid.pk).update(confirmed_by=self.finance.user)
        fin = self.who_row('Fw_Fin')
        self.assertEqual((fin['invoices_n'], fin['held_n'], fin['paid_n'], fin['paid_sum']),
                         (1, 1, 1, D('250')))
        # B5 answer 9: someone outside Finance who approved gets a row, role at the time.
        ceo = self.who_row('Fw_Ceo')
        self.assertEqual((ceo['approvals_n'], ceo['roles']), (1, ['CEO']))

    def test_a_hold_outside_the_period_is_not_counted(self):
        p = self.pay(sites=[self.site()], status=PaymentRequest.ON_HOLD)
        h = PaymentRequestHold.objects.create(payment_request=p, reason='x', held_by=self.ceo)
        PaymentRequestHold.objects.filter(pk=h.pk).update(held_at=self.ago(45))
        self.assertNotIn('Fw_Ceo', [r['name'] for r in self.who('30d')['rows']])


class WhoSectionTests(ChecksFixture):

    def section(self, query='?period=30d'):
        html = self.client_for(self.head).get(URL + query).content.decode()
        return html.split('<!-- fht:start -->', 1)[1].split('<!-- fht:end -->', 1)[0]

    def test_empty_payments_state_and_without_amount(self):
        self.received_by(self.site(), 'M1', self.coord, COORD, self.ago(1), got=None)
        self.received_by(self.site(), 'M2', self.coord, COORD, self.ago(1), got='1000')
        html = self.section()
        self.assertEqual(html.count('No payment requests in PMS yet.'), 1)
        row = html.split('data-who="p%d"' % self.coord.pk, 1)[1].split('</tr>', 1)[0]
        self.assertIn('2 · ₹1,000 · 1 without amount', row)
        self.assertIn(COORD, row)
        self.assertEqual(row.count('>—<'), 3)        # invoices, approvals, paid out

    def test_all_blank_receipts_never_read_as_rupees_zero(self):
        self.received_by(self.site(), 'M1', self.coord, COORD, self.ago(1), got=None)
        row = self.section().split('data-who="p%d"' % self.coord.pk, 1)[1].split('</tr>', 1)[0]
        self.assertIn('1 · 1 without amount', row)
        self.assertNotIn('₹0', row)

    def test_sections_sit_between_the_page_and_the_item_list_in_order(self):
        html = self.client_for(self.head).get(URL).content.decode()
        order = [html.index(marker) for marker in
                 ('<!-- fh:end -->', 'id="who"', 'id="trust"', '<!-- fhi:start -->')]
        self.assertEqual(order, sorted(order))
        self.assertIn('Older receipts (before mid-September) show the role', html)


# ---------------------------------------------------------------------------
# Query count
# ---------------------------------------------------------------------------

class QueryCountTests(ChecksFixture):
    """12 (B3) + 4 (B5 checks) + 7 (B4 list) = 23 at every size and with a check chosen."""

    def add_sites(self, n):
        for _ in range(n):
            s = self.site()
            self.received_by(s, 'M1', self.coord, COORD, self.ago(2), got=None)
            self.older_receipt(s, 'M2', 'Fw_Fin', 'Finance', self.ago(80))
            self.amount_line(s, 'Set milestone amounts: M2=₹5', self.ago(1))
            self.milestone(s, 'M3', status=PaymentMilestone.INVOICED)

    def count(self, query=''):
        client = self.client_for(self.head)          # the login is not the page's cost
        with CaptureQueriesContext(connection) as ctx:
            response = client.get(URL + query)
        self.assertEqual(response.status_code, 200)
        return len(ctx.captured_queries)

    def test_23_at_3_0_30_0_30_10_and_with_a_check(self):
        self.add_sites(3)
        small = self.count()
        self.add_sites(27)
        large = self.count()
        for i in range(10):
            p = self.approved(10 if i % 2 else 1, hold_at=self.ago(1) if i == 3 else None)
            if i % 3 == 0:
                self.invoice_doc(p)
        with_payments = self.count()
        checked = self.count('?check=late_payment&q=fw')
        print(f'\nFIN-H-B5 queries: 3/0={small}, 30/0={large}, 30/10={with_payments}, '
              f'check={checked}')
        self.assertEqual((small, large, with_payments, checked), (23, 23, 23, 23))
