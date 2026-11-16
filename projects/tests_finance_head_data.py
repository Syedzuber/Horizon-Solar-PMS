"""
FIN-H-B2 — the Finance Head data layer (projects/finance_head.py).

THE RULES PINNED HERE (rulings H7, H8, H14-H17, H19, H21, H22, H24-H30; B2 answers 2-5):
  * Periods in IST: 'month' from the 1st, '30d' = today - 29 .. today, 'fy' from 1 April.
  * Scope: soft-deleted and test sites out everywhere; an order's payment counts only
    while its order has a site that is both live and real; Draft only in draft_count.
  * Due as on Finance work: M1 Pending due; M2/M3 due when the trigger is Done, missing
    or N/A; Pending on Draft/Cancelled sites in neither due nor not_due.
  * Null amounts counted apart, adding 0 to sums.
  * Outgoing by status, effective amounts, order vs bill; H19 bill remainders apart.
  * Money in / to collect / net are None for OPEX and CAPEX, never 0.
  * Six queries, whatever the data size.

Real fixtures throughout: every assertion reads rows written to the test database.

    python manage.py test projects.tests_finance_head_data --settings=solarpms.test_settings
"""
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from .finance_head import finance_head_summary, period_start
from .models import (APPROVAL_OPEN, PaymentMilestone, PaymentRequest, PaymentRequestHold,
                     Task, VendorOrder, VendorOrderSite)
from .tests_fin_h_helpers import PaymentHelperFixture
from .utils import INVOICE_DUE_TRIGGERS

PO_BC = INVOICE_DUE_TRIGGERS['M2']
HANDOVER = INVOICE_DUE_TRIGGERS['M3']
D = Decimal


class FinanceHeadFixture(PaymentHelperFixture):
    """Builders on top of the B2-0 fixture (sites, milestones, triggers, bills)."""

    def summary(self, period='month', today=None):
        return finance_head_summary(period, today=today or self.today)

    def received(self, site, name, got, on):
        return self.milestone(site, name, status=PaymentMilestone.RECEIVED,
                              amount_received=D(got) if got is not None else None,
                              received_date=on)

    def pay(self, *, sites=(), ptype='Residential', status=PaymentRequest.PENDING_APPROVAL,
            amount='100', approved=None, payment_date=None, approved_at=None, bill=None,
            requested_at=None, hold_at=None):
        """One payment request, on a new order sized against `sites` or on `bill`. An
        open hold is written when `hold_at` is given; dates are set after the insert
        because requested_date and held_at are auto_now_add."""
        common = dict(amount=D(amount), requested_by=self.scm.user, status=status,
                      approved_amount=D(approved) if approved is not None else None,
                      payment_date=payment_date, approved_at=approved_at,
                      decision_reason='Duplicate' if status == PaymentRequest.REJECTED else '')
        if bill is not None:
            p = PaymentRequest.objects.create(contractor_bill=bill, project=bill.project,
                                              vendor=self.contractor, **common)
        else:
            order = VendorOrder.objects.create(vendor=self.vendor, project_type=ptype,
                                               total_amount=D('10000000'), created_by=self.scm)
            for s in sites:
                VendorOrderSite.objects.create(order=order, project=s)
            p = PaymentRequest.objects.create(vendor_order=order, vendor=self.vendor,
                                              project=sites[0] if sites else None, **common)
        if requested_at is not None:
            PaymentRequest.objects.filter(pk=p.pk).update(requested_date=requested_at)
        if hold_at is not None:
            h = PaymentRequestHold.objects.create(payment_request=p, reason='Check',
                                                  held_by=self.finance)
            PaymentRequestHold.objects.filter(pk=h.pk).update(held_at=hold_at)
        return p

    def ago(self, days):
        return timezone.now() - timedelta(days=days)


# ---------------------------------------------------------------------------
# Periods (IST)
# ---------------------------------------------------------------------------

class PeriodTests(FinanceHeadFixture):

    def test_month_keeps_1_nov_and_drops_31_oct_on_16_nov(self):
        site = self.site()
        self.received(site, 'M1', '10', date(2026, 10, 31))
        self.received(site, 'M2', '20', date(2026, 11, 1))
        r = self.summary('month', today=date(2026, 11, 16))
        self.assertEqual(r['period_start'], date(2026, 11, 1))
        self.assertEqual((r['received_period']['count'], r['received_period']['sum']), (1, D('20')))
        self.assertEqual((r['received_all_time']['count'], r['received_all_time']['sum']), (2, D('30')))
        self.assertEqual(r['received_period']['basis'], 'recorded')

    def test_financial_year_starts_on_1_april(self):
        self.assertEqual(period_start('fy', date(2026, 11, 16)), date(2026, 4, 1))
        self.assertEqual(period_start('fy', date(2027, 3, 31)), date(2026, 4, 1))
        self.assertEqual(period_start('fy', date(2027, 4, 1)), date(2027, 4, 1))

    def test_30_days_includes_today_minus_29_and_excludes_today_minus_30(self):
        site = self.site()
        self.received(site, 'M1', '1', self.today - timedelta(days=29))
        self.received(site, 'M2', '2', self.today - timedelta(days=30))
        r = self.summary('30d')
        self.assertEqual(r['period_start'], self.today - timedelta(days=29))
        self.assertEqual((r['received_period']['count'], r['received_period']['sum']), (1, D('1')))

    def test_unknown_period_is_refused(self):
        with self.assertRaises(ValueError):
            finance_head_summary('week')

    def test_today_defaults_to_the_ist_date(self):
        self.assertEqual(finance_head_summary('month')['period_end'], timezone.localdate())

    def test_a_request_raised_after_midnight_ist_is_aged_by_its_ist_day(self):
        # 00:30 IST on today-30 is 19:00 UTC on today-31: by IST it is 30 days old.
        ist_day = self.today - timedelta(days=30)
        raised = timezone.make_aware(datetime.combine(ist_day, time(0, 30)))
        self.pay(sites=[self.site()], requested_at=raised)
        ageing = self.summary()['payables_ageing']['awaiting']
        self.assertEqual((ageing['0-30']['count'], ageing['31-60']['count']), (1, 0))


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------

class ScopeTests(FinanceHeadFixture):

    def test_deleted_and_test_sites_are_left_out_everywhere(self):
        for fields in ({'is_deleted': True}, {'is_test': True}):
            site = self.site(contract_value=D('500'), **fields)
            self.milestone(site, 'M1', amount=D('10'))
            self.milestone(site, 'M2', status=PaymentMilestone.INVOICED, amount=D('20'),
                           invoice_date=self.today)
            self.received(site, 'M3', '30', self.today)
            self.pay(sites=[site])
            self.pay(bill=self.bill(site, '100'), amount='10')
            opex = self.site(project_type='OPEX', **fields)
            self.bill(opex, '100')
        r = self.summary()
        self.assertEqual((r['booked']['count'], r['booked']['sum']), (0, D('0')))
        self.assertEqual(r['coverage']['total'], 0)
        self.assertEqual(r['received_all_time']['count'], 0)
        self.assertFalse(r['has_payment_data'])
        self.assertEqual(r['bills_not_forwarded']['count'], 0)
        self.assertEqual(sum(w['in_count'] for w in r['weekly']), 0)

    def order_kept(self, *site_fields):
        sites = [self.site(**f) for f in site_fields]
        self.pay(sites=sites, amount='100')
        return self.summary()['awaiting']['count'] == 1

    def test_an_order_with_one_deleted_and_one_live_site_is_kept(self):
        self.assertTrue(self.order_kept({'is_deleted': True}, {}))

    def test_an_order_whose_sites_are_all_deleted_is_dropped(self):
        self.assertFalse(self.order_kept({'is_deleted': True}, {'is_deleted': True}))

    def test_an_order_with_a_deleted_real_site_and_a_live_test_site_is_dropped(self):
        # B2 answer 2: no site that is both live and real.
        self.assertFalse(self.order_kept({'is_deleted': True}, {'is_test': True}))

    def test_an_order_with_no_site_is_kept(self):
        self.assertTrue(self.order_kept())

    def test_booked_counts_every_activated_status_and_draft_apart(self):
        for status in ('Active', 'In Progress', 'Commissioned', 'On Hold'):
            self.site(status=status, contract_value=D('100'))
        self.site(status='Active')                                    # no contract value
        self.site(status='Draft', contract_value=D('999'))
        self.site(status='Cancelled', contract_value=D('999'))
        self.site(project_type='OPEX', contract_value=D('999'))
        booked = self.summary()['booked']
        self.assertEqual(booked, {'count': 5, 'sum': D('400'), 'null_value_count': 1,
                                  'draft_count': 1})


# ---------------------------------------------------------------------------
# Due rules
# ---------------------------------------------------------------------------

class DueTests(FinanceHeadFixture):

    def test_due_and_not_due_follow_finance_work(self):
        due_ids = set()
        s1 = self.site(activated_at=self.ago(20))
        self.milestone(s1, 'M1', amount=D('1'))                                # due (M1)
        s2 = self.site()
        self.milestone(s2, 'M2', amount=D('2'))
        self.trigger(s2, PO_BC, done_days_ago=5)                               # due (Done)
        s3 = self.site()
        self.milestone(s3, 'M2', amount=D('4'))                                # due (missing)
        s4 = self.site()
        self.milestone(s4, 'M3', amount=D('8'))
        self.trigger(s4, HANDOVER, status=Task.NOT_STARTED, is_not_applicable=True)  # due (N/A)
        s5 = self.site()
        self.milestone(s5, 'M2', amount=D('16'))
        self.trigger(s5, PO_BC, status=Task.IN_PROGRESS)                       # not due
        for status in ('Draft', 'Cancelled'):                                  # neither
            s = self.site(status=status)
            self.milestone(s, 'M1', amount=D('100'))
            self.milestone(s, 'M2', amount=D('100'))
        r = self.summary()
        self.assertEqual((r['due']['count'], r['due']['sum']), (4, D('15')))
        self.assertEqual(r['due']['m1'], {'count': 1, 'sum': D('1')})
        self.assertEqual(r['due']['m2_m3'], {'count': 3, 'sum': D('14')})
        self.assertEqual((r['not_due']['count'], r['not_due']['sum']), (1, D('16')))
        # Missing and N/A triggers have no date to count from.
        self.assertEqual(r['due']['undated_count'], 2)
        # Coverage still counts the Draft and Cancelled sites' milestones.
        self.assertEqual(r['coverage']['total'], 9)

    def test_oldest_due_since_is_the_trigger_done_date_or_activation(self):
        s1 = self.site(activated_at=self.ago(40))
        self.milestone(s1, 'M1')
        s2 = self.site()
        self.milestone(s2, 'M2')
        task = self.trigger(s2, PO_BC, done_days_ago=60)
        due = self.summary()['due']
        self.assertEqual(due['oldest_due_since'], timezone.localdate(task.completed_at))
        self.assertEqual(due['undated_count'], 0)

    def test_m1_on_a_site_never_activated_is_due_and_undated(self):
        s = self.site(activated_at=None)
        self.milestone(s, 'M1')
        due = self.summary()['due']
        self.assertEqual((due['count'], due['undated_count'], due['oldest_due_since']),
                         (1, 1, None))


# ---------------------------------------------------------------------------
# Null amounts and receivables ageing
# ---------------------------------------------------------------------------

class NullAmountTests(FinanceHeadFixture):

    def test_null_amounts_are_counted_apart_and_add_nothing(self):
        s = self.site()
        self.milestone(s, 'M1')                                   # due, no amount
        self.milestone(s, 'M2', amount=D('50'))                   # due (missing trigger)
        self.milestone(self.site(), 'M3', status=PaymentMilestone.INVOICED,
                       invoice_date=self.today)                   # neither amount
        self.received(s, 'M3', None, self.today)
        r = self.summary()
        self.assertEqual(r['coverage'], {'total': 4, 'with_amount': 1, 'without_amount': 3})
        self.assertEqual((r['due']['sum'], r['due']['null_amount_count']), (D('50'), 1))
        self.assertEqual((r['invoiced']['sum'], r['invoiced']['null_amount_count']), (D('0'), 1))
        self.assertEqual((r['received_period']['sum'], r['received_period']['null_amount_count']),
                         (D('0'), 1))

    def test_invoiced_uses_invoice_amount_then_agreed_amount(self):
        s = self.site()
        self.milestone(s, 'M2', status=PaymentMilestone.INVOICED, amount=D('100'),
                       invoice_amount=D('90'), invoice_date=self.today)
        self.milestone(s, 'M3', status=PaymentMilestone.INVOICED, amount=D('40'),
                       invoice_date=self.today)
        inv = self.summary()['invoiced']
        self.assertEqual((inv['count'], inv['sum'], inv['null_amount_count']), (2, D('130'), 0))


class ReceivablesAgeingTests(FinanceHeadFixture):

    def test_bucket_edges_trigger_fallback_and_unknown(self):
        for days in (30, 31, 60, 61, 90, 91):
            self.milestone(self.site(), 'M2', status=PaymentMilestone.INVOICED, amount=D('1'),
                           invoice_date=self.today - timedelta(days=days))
        fallback = self.site()
        self.milestone(fallback, 'M3', status=PaymentMilestone.INVOICED, amount=D('5'))
        self.trigger(fallback, HANDOVER, done_days_ago=45)
        self.milestone(self.site(), 'M2', status=PaymentMilestone.INVOICED, amount=D('7'))
        ageing = self.summary()['receivables_ageing']
        counts = {b: v['count'] for b, v in ageing['buckets'].items()}
        self.assertEqual(counts, {'0-30': 1, '31-60': 3, '61-90': 2, '90+': 1, 'unknown': 1})
        self.assertEqual(ageing['buckets']['31-60']['sum'], D('7'))    # 1 + 1 + fallback 5
        self.assertEqual(ageing['buckets']['unknown']['sum'], D('7'))
        self.assertEqual(ageing['basis'], {'invoice_date': 6, 'trigger_date': 1, 'unknown': 1})


# ---------------------------------------------------------------------------
# Outgoing
# ---------------------------------------------------------------------------

class EmptyOutgoingTests(FinanceHeadFixture):

    def test_no_payments_is_flagged_and_tender_money_in_is_none(self):
        self.milestone(self.site(), 'M1', amount=D('10'))
        r = self.summary()
        self.assertFalse(r['has_payment_data'])
        for key in ('awaiting', 'on_hold', 'to_pay', 'paid_period'):
            self.assertEqual((r[key]['count'], r[key]['sum']), (0, D('0')), key)
        for t in ('OPEX', 'CAPEX'):
            row = r['by_type'][t]
            self.assertEqual((row['in_period'], row['to_collect'], row['net']), (None, None, None))
            self.assertEqual((row['out_period'], row['to_pay']), (D('0'), D('0')))
        self.assertEqual(r['by_type']['Residential']['to_collect'], D('10'))

    def test_a_rejected_request_alone_still_means_payment_data_exists(self):
        self.pay(sites=[self.site()], status=PaymentRequest.REJECTED)
        r = self.summary()
        self.assertTrue(r['has_payment_data'])
        self.assertEqual(r['awaiting']['count'] + r['to_pay']['count'], 0)


class OutgoingTests(FinanceHeadFixture):

    def setUp(self):
        super().setUp()
        R, O = self.site(), self.site(project_type='OPEX')
        month_start = self.today.replace(day=1)
        self.pay(sites=[R], amount='100')                                       # awaiting R
        self.pay(sites=[O], ptype='OPEX', amount='200', status=PaymentRequest.ON_HOLD,
                 hold_at=self.ago(10))                                          # hold O, from pending
        self.pay(sites=[R], amount='300', approved='250', status=PaymentRequest.ON_HOLD,
                 approved_at=self.ago(80), hold_at=self.ago(70))                # hold R, from approved
        self.pay(sites=[O], ptype='OPEX', amount='400', approved='350',
                 status=PaymentRequest.APPROVED, approved_at=self.ago(40))       # to pay O (order)
        self.pay(sites=[R], amount='500', approved='450', status=PaymentRequest.CONFIRMED,
                 payment_date=self.today)                                        # paid R, in period
        self.pay(sites=[R], amount='600', approved='600', status=PaymentRequest.CONFIRMED,
                 payment_date=month_start - timedelta(days=1))                   # paid last month
        self.pay(sites=[R], amount='700', status=PaymentRequest.REJECTED)        # dead
        self.bill_o = self.bill(O, '1000')
        self.pay(bill=self.bill_o, amount='100', approved='100',
                 status=PaymentRequest.APPROVED)                                 # to pay O (bill), pre-O4
        self.r = self.summary()

    def test_each_state_sums_its_own_rows(self):
        r = self.r
        self.assertTrue(r['has_payment_data'])
        self.assertEqual((r['awaiting']['count'], r['awaiting']['sum']), (1, D('100')))
        self.assertEqual((r['on_hold']['count'], r['on_hold']['sum']), (2, D('450')))
        self.assertEqual(r['on_hold']['by_type']['Residential']['sum'], D('250'))
        self.assertEqual(r['on_hold']['by_type']['OPEX']['sum'], D('200'))
        self.assertEqual((r['to_pay']['count'], r['to_pay']['sum']), (2, D('450')))
        self.assertEqual(r['to_pay']['by_backing'], {'order': {'count': 1, 'sum': D('350')},
                                                     'bill': {'count': 1, 'sum': D('100')}})
        self.assertEqual(r['to_pay']['by_type']['OPEX'], {'count': 2, 'sum': D('450')})
        self.assertEqual((r['paid_period']['count'], r['paid_period']['sum']), (1, D('450')))

    def test_nothing_is_counted_twice(self):
        r = self.r
        counted = sum(r[k]['count'] for k in ('awaiting', 'on_hold', 'to_pay', 'paid_period'))
        self.assertEqual(counted, 6)        # 8 requests - the rejected - last month's paid
        for key in ('awaiting', 'on_hold', 'to_pay', 'paid_period'):
            self.assertEqual(sum(v['sum'] for v in r[key]['by_type'].values()), r[key]['sum'], key)

    def test_a_bill_payment_is_typed_by_the_bill_site(self):
        self.assertEqual(self.r['to_pay']['by_type']['Residential']['count'], 0)

    def test_by_type(self):
        bt = self.r['by_type']
        self.assertEqual(bt['Residential']['awaiting_plus_hold'], D('350'))
        self.assertEqual(bt['Residential']['out_period'], D('450'))
        self.assertEqual(bt['OPEX']['awaiting_plus_hold'], D('200'))
        self.assertEqual(bt['OPEX']['to_pay'], D('450'))
        self.assertIsNone(bt['OPEX']['net'])

    def test_payables_ageing(self):
        ageing = self.r['payables_ageing']
        self.assertEqual(ageing['all']['0-30'], {'count': 2, 'sum': D('300')})
        self.assertEqual(ageing['on_hold']['61-90'], {'count': 1, 'sum': D('250')})
        self.assertEqual(ageing['to_pay']['31-60'], {'count': 1, 'sum': D('350')})
        self.assertEqual(ageing['to_pay']['unknown'], {'count': 1, 'sum': D('100')})

    def test_the_bill_remainder_is_shown_apart(self):
        bills = self.r['bills_not_forwarded']
        self.assertEqual((bills['count'], bills['sum']), (1, D('900')))
        self.assertEqual(bills['by_type']['OPEX']['sum'], D('900'))


class BillsNotForwardedTests(FinanceHeadFixture):

    def test_remainder_ignores_rejected_and_stays_out_of_to_pay(self):
        bill = self.bill(self.site(), '100')
        self.pay(bill=bill, amount='60')                                       # forwarded
        self.pay(bill=bill, amount='30', status=PaymentRequest.REJECTED)       # does not reduce
        full = self.bill(self.site(), '50')
        self.pay(bill=full, amount='50')                                       # fully forwarded
        open_bill = self.bill(self.site(), '70')
        # Not approved; an open request carries no closed_at (closed_at_iff_terminal).
        open_bill.request.status, open_bill.request.closed_at = APPROVAL_OPEN, None
        open_bill.request.save(update_fields=['status', 'closed_at'])
        r = self.summary()
        self.assertEqual(r['bills_not_forwarded']['count'], 1)
        self.assertEqual(r['bills_not_forwarded']['sum'], D('40'))
        self.assertEqual(r['to_pay']['sum'], D('0'))


# ---------------------------------------------------------------------------
# Net and weekly
# ---------------------------------------------------------------------------

class NetTests(FinanceHeadFixture):

    def test_net_is_residential_in_minus_residential_out(self):
        R, O = self.site(), self.site(project_type='OPEX')
        self.received(R, 'M1', '1000', self.today)
        self.pay(sites=[R], amount='450', approved='450', status=PaymentRequest.CONFIRMED,
                 payment_date=self.today)
        self.pay(sites=[O], ptype='OPEX', amount='300', approved='300',
                 status=PaymentRequest.CONFIRMED, payment_date=self.today)
        bt = self.summary()['by_type']
        self.assertEqual(bt['Residential']['net'], D('550'))
        self.assertEqual(bt['OPEX']['out_period'], D('300'))
        self.assertIsNone(bt['OPEX']['net'])
        self.assertIsNone(bt['CAPEX']['in_period'])


class WeeklyTests(FinanceHeadFixture):

    def test_eight_monday_weeks_oldest_first_and_a_sunday_lands_in_its_week(self):
        monday = self.today - timedelta(days=self.today.weekday())
        sunday = monday - timedelta(days=1)
        R, O = self.site(), self.site(project_type='OPEX')
        self.received(R, 'M1', '10', sunday)
        self.received(R, 'M2', '99', monday - timedelta(weeks=7, days=1))      # before the range
        self.pay(sites=[O], ptype='OPEX', amount='40', approved='30',
                 status=PaymentRequest.CONFIRMED, payment_date=sunday)
        weekly = self.summary()['weekly']
        self.assertEqual(len(weekly), 8)
        self.assertEqual([w['week_start'] for w in weekly],
                         [monday - timedelta(weeks=i) for i in range(7, -1, -1)])
        last_week = weekly[-2]
        self.assertEqual((last_week['in_count'], last_week['in_sum']), (1, D('10')))
        self.assertEqual((last_week['out_count'], last_week['out_sum']), (1, D('30')))
        self.assertEqual(last_week['out_by_type']['OPEX'], {'count': 1, 'sum': D('30')})
        self.assertEqual(sum(w['in_count'] for w in weekly), 1)
        self.assertEqual(weekly[-1]['in_sum'], D('0'))


# ---------------------------------------------------------------------------
# Query count
# ---------------------------------------------------------------------------

class QueryCountTests(FinanceHeadFixture):

    def build(self, sites, payments):
        built = []
        for i in range(sites):
            s = self.site(project_type='Residential' if i % 3 else 'OPEX',
                          contract_value=D('1000'))
            built.append(s)
            self.milestone(s, 'M1', amount=D('10'))
            self.milestone(s, 'M2', status=PaymentMilestone.INVOICED, invoice_date=self.today)
            self.received(s, 'M3', '5', self.today)
            self.trigger(s, PO_BC)
        statuses = [PaymentRequest.PENDING_APPROVAL, PaymentRequest.ON_HOLD,
                    PaymentRequest.APPROVED, PaymentRequest.CONFIRMED, PaymentRequest.REJECTED]
        for i in range(payments):
            status = statuses[i % 5]
            approved = '50' if status in (PaymentRequest.APPROVED, PaymentRequest.CONFIRMED) else None
            kw = dict(status=status, amount='100', approved=approved,
                      payment_date=self.today if status == PaymentRequest.CONFIRMED else None,
                      hold_at=self.ago(5) if status == PaymentRequest.ON_HOLD else None)
            if i % 2:
                self.pay(bill=self.bill(built[i % sites], '500'), **kw)
            else:
                self.pay(sites=built[:2], **kw)

    def assert_six(self, sites, payments):
        # Each size is built in its own test (own transaction), so the sizes never add up.
        self.build(sites, payments)
        with CaptureQueriesContext(connection) as ctx:
            self.summary()
        print(f'\nFIN-H-B2 query count {sites} sites / {payments} payments: '
              f'{len(ctx.captured_queries)}')
        self.assertEqual(len(ctx.captured_queries), 6)

    def test_3_sites_0_payments(self):
        self.assert_six(3, 0)

    def test_30_sites_0_payments(self):
        self.assert_six(30, 0)

    def test_30_sites_10_payments(self):
        self.assert_six(30, 10)
