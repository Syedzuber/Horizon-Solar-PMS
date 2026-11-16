"""
FIN-H-B2-0 — the three helpers extracted for the Finance Head data layer, pinned here so
the extraction is behaviour-neutral and the helpers keep their rules once B2 uses them.

THE RULES PINNED HERE:
  * milestone_queries.annotate_trigger_done() annotates trigger_name, trigger_status,
    trigger_na and trigger_done_at, and adds no filter or ordering of its own. Finance
    work reads them as: no task = due ("no trigger task"), Done = due from its date
    (Done before Not Applicable), Not Applicable = due ("trigger N/A"), else not due.
    M1 has no trigger. Two tasks of the trigger name: the oldest (lowest pk) is read.
  * payments.exclude_test_site_payments() drops an order's payment only when every one
    of its order's sites is a test site, and a bill's payment when the bill's site is.
  * payments.committed_total_subquery() equals models.committed_total() for the same
    bill — every status but REJECTED, each at its effective amount, 0 with none.

Real fixtures throughout: every assertion reads rows written to the test database.

    python manage.py test projects.tests_fin_h_helpers --settings=solarpms.test_settings
"""
from datetime import timedelta
from decimal import Decimal

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from .milestone_queries import annotate_trigger_done
from .models import (
    APPROVAL_APPROVED, APPROVAL_KIND_CONTRACTOR_BILL, VENDOR_KIND_CONTRACTOR,
    ApprovalRequest, ContractorBillDetail, PaymentMilestone, PaymentRequest, Task, Vendor,
    VendorOrder, VendorOrderSite, committed_total,
)
from .payments import committed_total_subquery, exclude_test_site_payments
from .tests_finance_work import FinanceWorkFixture
from .utils import INVOICE_DUE_TRIGGERS

PO_BC = INVOICE_DUE_TRIGGERS['M2']
HANDOVER = INVOICE_DUE_TRIGGERS['M3']


# ---------------------------------------------------------------------------
# Helper 1: annotate_trigger_done
# ---------------------------------------------------------------------------

class TriggerDoneTests(FinanceWorkFixture):

    def annotated(self, milestone):
        return annotate_trigger_done(PaymentMilestone.objects.filter(pk=milestone.pk)).get()

    def test_m2_with_its_trigger_done_carries_the_completion_time(self):
        site = self.site()
        m2 = self.milestone(site, 'M2')
        task = self.trigger(site, PO_BC, done_days_ago=4)
        m = self.annotated(m2)
        self.assertEqual(m.trigger_name, PO_BC)
        self.assertEqual(m.trigger_status, Task.DONE)
        self.assertEqual(m.trigger_done_at, task.completed_at)
        self.assertIs(m.trigger_na, False)

    def test_trigger_in_progress_has_no_done_time(self):
        site = self.site()
        m2 = self.milestone(site, 'M2')
        self.trigger(site, PO_BC, status=Task.IN_PROGRESS)
        m = self.annotated(m2)
        self.assertEqual((m.trigger_status, m.trigger_done_at), (Task.IN_PROGRESS, None))
        # Finance work: not yet due, so it is only counted.
        self.assertNotIn(m2.pk, [r.milestone.pk for r in self.ctx()['to_act']])

    def test_missing_trigger_is_all_none_and_finance_work_counts_it_due(self):
        site = self.site()
        m2 = self.milestone(site, 'M2')
        m = self.annotated(m2)
        self.assertEqual((m.trigger_name, m.trigger_status, m.trigger_na, m.trigger_done_at),
                         (PO_BC, None, None, None))
        row = {r.milestone.pk: r for r in self.ctx()['to_act']}[m2.pk]
        self.assertEqual((row.kind, row.note, row.since), ('invoice', 'no trigger task', None))

    def test_not_applicable_trigger_is_flagged_and_finance_work_counts_it_due(self):
        site = self.site()
        m3 = self.milestone(site, 'M3')
        self.trigger(site, HANDOVER, status=Task.NOT_STARTED, is_not_applicable=True)
        m = self.annotated(m3)
        self.assertEqual((m.trigger_status, m.trigger_na, m.trigger_done_at),
                         (Task.NOT_STARTED, True, None))
        row = {r.milestone.pk: r for r in self.ctx()['to_act']}[m3.pk]
        self.assertEqual((row.kind, row.note, row.since), ('invoice', 'trigger N/A', None))

    def test_done_is_read_before_not_applicable(self):
        site = self.site()
        m3 = self.milestone(site, 'M3')
        task = self.trigger(site, HANDOVER, done_days_ago=2, is_not_applicable=True)
        m = self.annotated(m3)
        self.assertEqual((m.trigger_status, m.trigger_na), (Task.DONE, True))
        row = {r.milestone.pk: r for r in self.ctx()['to_act']}[m3.pk]
        self.assertEqual((row.kind, row.note, row.since),
                         ('invoice', '', timezone.localdate(task.completed_at)))

    def test_m1_has_no_trigger_value(self):
        site = self.site()
        m1 = self.milestone(site, 'M1')
        self.trigger(site, PO_BC)
        self.trigger(site, HANDOVER)
        m = self.annotated(m1)
        self.assertEqual((m.trigger_name, m.trigger_status, m.trigger_na, m.trigger_done_at),
                         ('', None, None, None))

    def test_two_tasks_of_the_trigger_name_read_the_oldest(self):
        site = self.site()
        m2 = self.milestone(site, 'M2')
        first = self.trigger(site, PO_BC, done_days_ago=9)
        self.trigger(site, PO_BC, status=Task.IN_PROGRESS)
        m = self.annotated(m2)
        self.assertEqual((m.trigger_status, m.trigger_done_at), (Task.DONE, first.completed_at))

    def test_it_adds_no_filter_and_costs_one_query(self):
        # A Received milestone on a Draft OPEX site is outside every Finance work filter;
        # the helper must still return it, and the whole read is one query.
        draft = self.site(project_type='OPEX', status='Draft')
        kept = [self.milestone(draft, 'M2', status=PaymentMilestone.RECEIVED),
                self.milestone(self.site(), 'M3')]
        with CaptureQueriesContext(connection) as ctx:
            rows = list(annotate_trigger_done(PaymentMilestone.objects.all()))
        self.assertEqual(len(ctx.captured_queries), 1)
        self.assertEqual({m.pk for m in rows}, {m.pk for m in kept})


# ---------------------------------------------------------------------------
# Payment fixtures (helpers 2 and 3)
# ---------------------------------------------------------------------------

class PaymentHelperFixture(FinanceWorkFixture):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.contractor = Vendor.objects.create(
            name='Civil Works Co', contact_person='C', phone='9000000077',
            kind=VENDOR_KIND_CONTRACTOR)

    def order_payment(self, sites, amount='1000'):
        """One order sized against `sites`, one pending payment on it."""
        order = VendorOrder.objects.create(
            vendor=self.vendor, project_type='Residential',
            total_amount=Decimal('1000000'), created_by=self.scm)
        for site in sites:
            VendorOrderSite.objects.create(order=order, project=site)
        return PaymentRequest.objects.create(
            vendor_order=order, project=sites[0] if sites else None, vendor=self.vendor,
            amount=Decimal(amount), requested_by=self.scm.user,
            status=PaymentRequest.PENDING_APPROVAL)

    def bill(self, site, amount='100'):
        """An APPROVED contractor bill on `site`, written directly: the helpers read the
        bill row, never how it was approved."""
        FinanceWorkFixture._seq += 1
        number = f'FH-{FinanceWorkFixture._seq}'
        approval = ApprovalRequest.objects.create(
            kind=APPROVAL_KIND_CONTRACTOR_BILL, status=APPROVAL_APPROVED,
            closed_at=timezone.now(), title=f'Civil works {number}', description='Walls.',
            vendor=self.contractor, raised_by=self.scm)
        return ContractorBillDetail.objects.create(
            request=approval, project=site, amount=Decimal(amount), bill_number=number,
            bill_date=self.today - timedelta(days=5), pdf_file_name=f'{number}.pdf',
            pdf_bucket='bills-private', pdf_path=f'{site.pk}/{number}.pdf', pdf_size_kb=80)

    def bill_payment(self, bill, amount, status=PaymentRequest.PENDING_APPROVAL,
                     approved=None):
        return PaymentRequest.objects.create(
            contractor_bill=bill, project=bill.project, vendor=self.contractor,
            amount=Decimal(amount), requested_by=self.scm.user, status=status,
            approved_amount=Decimal(approved) if approved is not None else None,
            # payment_request_refusal_needs_reason: a REJECTED row must say why.
            decision_reason='Duplicate' if status == PaymentRequest.REJECTED else '')


# ---------------------------------------------------------------------------
# Helper 2: exclude_test_site_payments
# ---------------------------------------------------------------------------

class TestSiteExclusionTests(PaymentHelperFixture):

    def kept(self, *payments):
        pks = [p.pk for p in payments]
        return set(exclude_test_site_payments(PaymentRequest.objects.filter(pk__in=pks))
                   .values_list('pk', flat=True))

    def test_an_order_whose_only_site_is_a_test_site_is_excluded(self):
        pay = self.order_payment([self.site(is_test=True)])
        self.assertEqual(self.kept(pay), set())

    def test_an_order_with_one_test_and_one_real_site_is_kept(self):
        pay = self.order_payment([self.site(is_test=True), self.site()])
        self.assertEqual(self.kept(pay), {pay.pk})

    def test_an_order_with_no_site_is_kept(self):
        pay = self.order_payment([])
        self.assertEqual(self.kept(pay), {pay.pk})

    def test_a_bill_on_a_test_site_is_excluded_one_on_a_real_site_kept(self):
        test_pay = self.bill_payment(self.bill(self.site(is_test=True)), '30')
        real_pay = self.bill_payment(self.bill(self.site()), '30')
        self.assertEqual(self.kept(test_pay, real_pay), {real_pay.pk})

    def test_an_order_on_two_real_sites_is_counted_once(self):
        # Exists, never a join: two sites must not repeat the request.
        pay = self.order_payment([self.site(), self.site()])
        rows = list(exclude_test_site_payments(PaymentRequest.objects.filter(pk=pay.pk)))
        self.assertEqual(len(rows), 1)


# ---------------------------------------------------------------------------
# Helper 3: committed_total_subquery
# ---------------------------------------------------------------------------

class CommittedTotalSubqueryTests(PaymentHelperFixture):

    def committed(self, bill):
        return (ContractorBillDetail.objects.filter(pk=bill.pk)
                .annotate(c=committed_total_subquery()).get().c)

    def python_rule(self, bill):
        return committed_total(PaymentRequest.objects.filter(contractor_bill=bill))

    def test_pending_approved_for_less_and_rejected(self):
        bill = self.bill(self.site(), '100')
        self.bill_payment(bill, '30')                                         # counts 30
        self.bill_payment(bill, '30', PaymentRequest.APPROVED, approved='20')  # counts 20
        self.bill_payment(bill, '50', PaymentRequest.REJECTED)                # counts 0
        self.assertEqual(self.committed(bill), Decimal('50'))
        self.assertEqual(self.committed(bill), self.python_rule(bill))

    def test_a_bill_with_no_payments_is_zero(self):
        bill = self.bill(self.site(), '100')
        self.assertEqual(self.committed(bill), Decimal('0'))
        self.assertEqual(self.committed(bill), self.python_rule(bill))

    def test_every_status_agrees_with_the_python_rule_and_bills_stay_apart(self):
        site = self.site()
        bill, other = self.bill(site, '1000'), self.bill(site, '1000')
        self.bill_payment(bill, '100')
        self.bill_payment(bill, '200', PaymentRequest.ON_HOLD, approved='150')
        self.bill_payment(bill, '300', PaymentRequest.APPROVED, approved='300')
        self.bill_payment(bill, '120', PaymentRequest.CONFIRMED, approved='90')
        self.bill_payment(bill, '999', PaymentRequest.REJECTED, approved='999')
        self.bill_payment(other, '7')
        self.assertEqual(self.committed(bill), Decimal('640'))
        self.assertEqual(self.committed(bill), self.python_rule(bill))
        self.assertEqual(self.committed(other), Decimal('7'))
        self.assertEqual(self.committed(other), self.python_rule(other))

    def test_many_bills_cost_one_query(self):
        site = self.site()
        bills = [self.bill(site, '100') for _ in range(3)]
        for b in bills:
            self.bill_payment(b, '10')
        with CaptureQueriesContext(connection) as ctx:
            values = list(ContractorBillDetail.objects.filter(pk__in=[b.pk for b in bills])
                          .annotate(c=committed_total_subquery()).values_list('c', flat=True))
        self.assertEqual(len(ctx.captured_queries), 1)
        self.assertEqual(values, [Decimal('10')] * 3)
