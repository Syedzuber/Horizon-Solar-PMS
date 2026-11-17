"""
FIN-H-B4-0 — the public scope helpers in projects/finance_head.py.

live_residential_milestones(), unforwarded_bills() and payment_type_expr() were pulled
out of B2's private code so the Finance Head item list (FIN-H-B4) reads exactly the rows
B2's figures count. Pinned here:
  * each helper's own scope (live, real, the right types; remainder > 0 only);
  * the helpers build querysets and run no query until evaluated;
  * agreement with finance_head_summary(): the same rows, the same totals.

    python manage.py test projects.tests_finance_head_scope --settings=solarpms.test_settings
"""
from decimal import Decimal

from django.db import connection
from django.test.utils import CaptureQueriesContext

from .finance_head import (finance_head_summary, live_residential_milestones,
                           payment_type_expr, scoped_payments, unforwarded_bills)
from .models import APPROVAL_OPEN, ApprovalRequest, PaymentMilestone, PaymentRequest
from .tests_finance_head_data import FinanceHeadFixture

D = Decimal


class LiveResidentialMilestonesTests(FinanceHeadFixture):

    def test_every_status_on_live_real_residential_sites_and_nothing_else(self):
        live = self.site()
        kept = [self.milestone(live, 'M1'),
                self.milestone(live, 'M2', status=PaymentMilestone.INVOICED),
                self.received(live, 'M3', '10', self.today)]
        # A Draft site is still in scope here; H26 is applied by the reader, not the scope.
        kept.append(self.milestone(self.site(status='Draft'), 'M1'))
        for fields in ({'is_deleted': True}, {'is_test': True}, {'project_type': 'OPEX'}):
            self.milestone(self.site(**fields), 'M1')
        self.assertEqual({m.pk for m in live_residential_milestones()}, {m.pk for m in kept})

    def test_it_is_the_set_b2_counts(self):
        live = self.site()
        self.milestone(live, 'M1', amount=D('5'))
        self.milestone(live, 'M2')
        self.milestone(self.site(is_test=True), 'M1')
        self.assertEqual(live_residential_milestones().count(),
                         self.summary()['coverage']['total'])


class UnforwardedBillsTests(FinanceHeadFixture):

    def test_one_row_per_bill_with_its_remainder_and_only_remainder_above_zero(self):
        site = self.site()
        part = self.bill(site, '100')
        self.pay(bill=part, amount='30')
        # A rejected payment commits nothing, so the whole bill is still unforwarded.
        rejected = self.bill(site, '50')
        self.pay(bill=rejected, amount='50', status=PaymentRequest.REJECTED)
        full = self.bill(site, '40')
        self.pay(bill=full, amount='40')
        rows = {b.pk: b.remainder for b in unforwarded_bills()}
        self.assertEqual(rows, {part.pk: D('70'), rejected.pk: D('50')})
        self.assertNotIn(full.pk, rows)

    def test_live_real_sites_of_the_three_types_and_approved_bills_only(self):
        kept = [self.bill(self.site(project_type=t), '10') for t in ('Residential', 'OPEX', 'CAPEX')]
        for fields in ({'is_deleted': True}, {'is_test': True}):
            self.bill(self.site(**fields), '10')
        still_open = self.bill(self.site(), '10')
        # approval_request_closed_at_iff_terminal: an open request has no closed_at.
        ApprovalRequest.objects.filter(pk=still_open.request_id).update(
            status=APPROVAL_OPEN, closed_at=None)
        self.assertEqual({b.pk for b in unforwarded_bills()}, {b.pk for b in kept})

    def test_count_and_sum_agree_with_the_b2_figure(self):
        a, b = self.bill(self.site(), '100'), self.bill(self.site(project_type='OPEX'), '60')
        self.pay(bill=a, amount='25')
        self.bill(self.site(is_test=True), '999')
        figure = self.summary()['bills_not_forwarded']
        rows = list(unforwarded_bills())
        self.assertEqual((len(rows), sum(r.remainder for r in rows)),
                         (figure['count'], figure['sum']))
        self.assertEqual(figure['sum'], D('135'))
        self.assertIn(b.pk, {r.pk for r in rows})


class PaymentTypeExprTests(FinanceHeadFixture):

    def test_order_type_for_an_order_payment_and_site_type_for_a_bill_payment(self):
        order_pay = self.pay(sites=[self.site(project_type='OPEX')], ptype='OPEX')
        bill_pay = self.pay(bill=self.bill(self.site(project_type='CAPEX'), '100'), amount='10')
        types = dict(scoped_payments().annotate(ptype=payment_type_expr())
                     .values_list('pk', 'ptype'))
        self.assertEqual(types, {order_pay.pk: 'OPEX', bill_pay.pk: 'CAPEX'})


class NoQueryUntilEvaluatedTests(FinanceHeadFixture):

    def test_building_the_scopes_runs_no_query(self):
        with CaptureQueriesContext(connection) as ctx:
            live_residential_milestones()
            unforwarded_bills()
            scoped_payments().annotate(ptype=payment_type_expr())
        self.assertEqual(len(ctx.captured_queries), 0)

    def test_the_summary_still_costs_six_queries(self):
        self.milestone(self.site(), 'M1')
        self.bill(self.site(), '10')
        with CaptureQueriesContext(connection) as ctx:
            finance_head_summary('month', today=self.today)
        self.assertEqual(len(ctx.captured_queries), 6)
