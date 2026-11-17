"""
FIN-H-B4 — the Finance Head page's "Every item" list (finance_head_items.py) and its
section on /finance/head/.

THE RULES PINNED HERE (rulings R-B4-1 to R-B4-9, answers A1-A6):
  * Default view: open items plus anything that moved in the last 30 days; "Not yet due"
    hidden unless chosen; newest movement first. A1: a trigger's Done is movement only
    while the milestone is Pending.
  * Filters (direction, status, type, search, sort) alone and combined; unknown values
    fall back to defaults. Search: site code, customer, vendor, invoice number, PO/PI
    number, "PR-<pk>" and "<pk>".
  * Paging: 25 a page; out of range -> the last page.
  * Scope from finance_head: test and soft-deleted sites out; H31 mixed-site order out;
    a bill with nothing left to forward out.
  * History: A4 duplicate rule (60 s); older auto-mark line matched by site + Mx and
    marked "older record"; BD amount line on the right Mx; creation line on all three
    (A3); a payment's raise, hold, answer, part approval and payment in order; a
    rejection's reason.
  * "amount not set", never ₹0.
  * Links (A5): bill link for a viewer of the approvals list only; order payment to the
    order page's anchor.
  * The whole page costs exactly 23 queries at 3/0, 30/0 and 30/10 (A6; 19 before
    FIN-H-B5 added its four, answer 11).
  * FIN-H-B5 ?check=: exactly the check's items, count == items opened for every check;
    search and type apply, direction and status ignored; unknown key ignored.

Real fixtures throughout (the FIN-H-B2 / B3 builders).

    python manage.py test projects.tests_finance_head_items --settings=solarpms.test_settings
"""
from datetime import timedelta
from decimal import Decimal

from django.db import connection
from django.test.utils import CaptureQueriesContext

# Module aliases: finance_head_items is also the name of the function imported below.
from . import finance_head as finance_head_module
from . import finance_head_items as items_module
from .finance_head import finance_head_summary
from .finance_head_checks import check_filters, finance_head_checks
from .finance_head_items import (PAGE_SIZE, SAME_EVENT_SECONDS, clean_item_params,
                                 finance_head_items)
from .models import (SUBJECT_PAYMENT_MILESTONE, SUBJECT_PAYMENT_REQUEST, SUBJECT_TASK,
                     ActivityLog, PaymentMilestone, PaymentRequest, PaymentRequestHold,
                     StatusTransition, Task, VendorOrder, VendorOrderSite)
from .tests_finance_head_page import URL, PageFixture
from .utils import INVOICE_DUE_TRIGGERS

PO_BC = INVOICE_DUE_TRIGGERS['M2']
HANDOVER = INVOICE_DUE_TRIGGERS['M3']
D = Decimal


class ItemsFixture(PageFixture):

    def old_site(self, **fields):
        """A site activated long ago, so its activation (the milestones' creation) is not
        a movement inside the 30-day window."""
        fields.setdefault('activated_at', self.ago(200))
        return self.site(**fields)

    def result(self, viewer_can_open_approvals=False, **params):
        return finance_head_items(clean_item_params(params), viewer_can_open_approvals,
                                  today=self.today)

    def keys(self, **params):
        return [i.key for i in self.result(**params)['items']]

    def item(self, key, **params):
        params.setdefault('status', '')
        found = [i for i in self.result(**params)['items'] if i.key == key]
        self.assertEqual(len(found), 1, f'{key} not listed with {params}')
        return found[0]

    def transition(self, subject_type, subject_id, to, frm='', actor=None, role='Finance',
                   at=None, project=None, reason='', remark=''):
        t = StatusTransition(subject_type=subject_type, subject_id=subject_id,
                             from_status=frm, to_status=to, actor=actor or self.finance,
                             actor_role_code=role, reason_code=reason, remark=remark,
                             project=project, occurred_at=at or self.ago(1))
        # The payment post_save receiver sends notices unless told not to (O6).
        t._notify = False
        t.save()
        return t

    def log(self, project, action, at, entity_id=None, actor=None):
        line = ActivityLog.objects.create(project=project, actor=actor or self.pm,
                                          action=action, entity_type='Milestone',
                                          entity_id=entity_id)
        ActivityLog.objects.filter(pk=line.pk).update(timestamp=at)
        return line


# ---------------------------------------------------------------------------
# Default view (R-B4-2, A1)
# ---------------------------------------------------------------------------

class DefaultViewTests(ItemsFixture):

    def test_open_and_recent_shown_old_closed_and_not_due_hidden(self):
        due = self.milestone(self.old_site(), 'M1', amount=D('1000'))
        recent = self.received(self.old_site(), 'M2', '500', self.today - timedelta(days=5))
        old = self.received(self.old_site(), 'M3', '500', self.today - timedelta(days=60))
        s = self.old_site()
        not_due = self.milestone(s, 'M2', amount=D('10'))
        self.trigger(s, PO_BC, status=Task.IN_PROGRESS)
        keys = self.keys()
        self.assertIn(f'milestone-{due.pk}', keys)
        self.assertIn(f'milestone-{recent.pk}', keys)
        self.assertNotIn(f'milestone-{old.pk}', keys)
        self.assertNotIn(f'milestone-{not_due.pk}', keys)
        # Chosen, "Not yet due" shows; so does the old receipt under "Received".
        self.assertIn(f'milestone-{not_due.pk}', self.keys(status='not_due'))
        self.assertIn(f'milestone-{old.pk}', self.keys(status='received'))

    def test_newest_movement_first(self):
        a = self.received(self.old_site(), 'M1', '1', self.today - timedelta(days=20))
        b = self.received(self.old_site(), 'M1', '1', self.today - timedelta(days=2))
        c = self.received(self.old_site(), 'M1', '1', self.today - timedelta(days=9))
        self.assertEqual(self.keys(), [f'milestone-{b.pk}', f'milestone-{c.pk}',
                                       f'milestone-{a.pk}'])

    def test_a1_trigger_done_moves_a_received_milestone_never(self):
        s = self.old_site()
        m = self.received(s, 'M2', '900', self.today - timedelta(days=60))
        task = self.trigger(s, PO_BC, done_days_ago=2)
        self.transition(SUBJECT_TASK, task.pk, Task.DONE, frm=Task.IN_PROGRESS,
                        actor=self.coord, role='Project Coordinator', at=task.completed_at)
        self.assertNotIn(f'milestone-{m.pk}', self.keys())
        # Still in its history, with the person from the task's own transition.
        item = self.item(f'milestone-{m.pk}', status='received')
        line = next(l for l in item.history if l['text'].startswith('Trigger task'))
        self.assertEqual(line['who_text'], 'Fw_Coord (Project Coordinator)')
        self.assertFalse(line['moves'])

    def test_a1_trigger_done_moves_a_pending_milestone(self):
        s = self.old_site()
        m = self.milestone(s, 'M2', amount=D('5'))
        self.trigger(s, PO_BC, done_days_ago=1)
        other = self.milestone(self.old_site(), 'M1', amount=D('5'))   # moved 200 d ago
        self.assertEqual(self.keys()[:2], [f'milestone-{m.pk}', f'milestone-{other.pk}'])


# ---------------------------------------------------------------------------
# Filters (R-B4-3)
# ---------------------------------------------------------------------------

class FilterTests(ItemsFixture):

    def setUp(self):
        super().setUp()
        self.R = self.old_site(site_code='HZ-77')
        self.O = self.old_site(project_type='OPEX')
        self.m1 = self.milestone(self.R, 'M1', amount=D('100'))                      # due
        self.m3 = self.milestone(self.R, 'M3', status=PaymentMilestone.INVOICED,
                                 invoice_amount=D('300'), invoice_date=self.today,
                                 invoice_number='INV-5521')                          # invoiced
        self.awaiting = self.pay(sites=[self.O], ptype='OPEX', amount='700')
        self.to_pay = self.pay(sites=[self.R], amount='900', approved='900',
                               status=PaymentRequest.APPROVED, approved_at=self.ago(3))
        VendorOrder.objects.filter(pk=self.to_pay.vendor_order_id).update(
            po_number='PO-4411', pi_number='PI-8812')
        self.bill_o = self.bill(self.O, '2000')

    def test_direction(self):
        self.assertEqual(set(self.keys(dir='in')),
                         {f'milestone-{self.m1.pk}', f'milestone-{self.m3.pk}'})
        self.assertEqual(set(self.keys(dir='out')),
                         {f'payment-{self.awaiting.pk}', f'payment-{self.to_pay.pk}',
                          f'bill-{self.bill_o.pk}'})

    def test_each_status(self):
        self.assertEqual(self.keys(status='due'), [f'milestone-{self.m1.pk}'])
        self.assertEqual(self.keys(status='invoiced'), [f'milestone-{self.m3.pk}'])
        self.assertEqual(self.keys(status='awaiting'), [f'payment-{self.awaiting.pk}'])
        self.assertEqual(self.keys(status='to_pay'), [f'payment-{self.to_pay.pk}'])
        self.assertEqual(self.keys(status='not_forwarded'), [f'bill-{self.bill_o.pk}'])
        self.assertEqual(self.keys(status='paid'), [])

    def test_type(self):
        self.assertEqual(set(self.keys(type='OPEX')),
                         {f'payment-{self.awaiting.pk}', f'bill-{self.bill_o.pk}'})
        self.assertEqual(self.keys(type='CAPEX'), [])

    def test_combined(self):
        self.assertEqual(self.keys(dir='out', type='OPEX', status='awaiting'),
                         [f'payment-{self.awaiting.pk}'])
        self.assertEqual(self.keys(dir='in', type='OPEX'), [])

    def test_sort_largest_and_oldest(self):
        self.assertEqual(self.keys(sort='largest')[0], f'bill-{self.bill_o.pk}')
        # Oldest open: M1 waits from the site's activation, 200 days ago.
        self.assertEqual(self.keys(sort='oldest')[0], f'milestone-{self.m1.pk}')

    def test_bad_values_fall_back(self):
        default = self.keys()
        self.assertEqual(self.keys(dir='sideways', status='nope', type='Mars', sort='zz',
                                   page='abc'), default)
        p = clean_item_params({'dir': 'x', 'status': 'y', 'type': 'z', 'sort': 'w',
                               'page': '-4', 'q': '  hz-77  '})
        # FIN-H-B5 added 'check' (only its shape is checked here; an unknown key is
        # dropped by finance_head_items, CheckFilterTests).
        self.assertEqual(p, {'dir': '', 'status': '', 'type': '', 'q': 'hz-77',
                             'sort': 'latest', 'page': 1, 'check': ''})

    def test_search(self):
        self.assertEqual(set(self.keys(q='hz-77')),
                         {f'milestone-{self.m1.pk}', f'milestone-{self.m3.pk}',
                          f'payment-{self.to_pay.pk}'})
        self.assertEqual(self.keys(q='INV-5521'), [f'milestone-{self.m3.pk}'])
        self.assertEqual(self.keys(q='po-4411'), [f'payment-{self.to_pay.pk}'])
        self.assertEqual(self.keys(q='PI-8812'), [f'payment-{self.to_pay.pk}'])
        self.assertEqual(set(self.keys(q='surya')),
                         {f'payment-{self.awaiting.pk}', f'payment-{self.to_pay.pk}'})
        self.assertEqual(self.keys(q='civil works'), [f'bill-{self.bill_o.pk}'])
        self.assertEqual(self.keys(q=f'PR-{self.awaiting.pk}'), [f'payment-{self.awaiting.pk}'])
        self.assertIn(f'payment-{self.awaiting.pk}', self.keys(q=str(self.awaiting.pk)))


# ---------------------------------------------------------------------------
# Paging (R-B4-4)
# ---------------------------------------------------------------------------

class PagingTests(ItemsFixture):

    def test_26_items_make_two_pages_and_out_of_range_is_the_last(self):
        for _ in range(26):
            self.pay(amount='10')
        first = self.result()
        self.assertEqual((first['count'], first['pages'], first['page']), (26, 2, 1))
        self.assertEqual(len(first['items']), PAGE_SIZE)
        self.assertIsNone(first['filters']['prev_href'])
        self.assertIn('page=2', first['filters']['next_href'])
        second = self.result(page='2')
        self.assertEqual((second['page'], len(second['items'])), (2, 1))
        self.assertEqual(self.result(page='99')['page'], 2)
        self.assertEqual(self.result(page='0')['page'], 1)


# ---------------------------------------------------------------------------
# Scope (R-B4-1)
# ---------------------------------------------------------------------------

class ScopeTests(ItemsFixture):

    def test_test_and_deleted_sites_mixed_orders_and_spent_bills_are_out(self):
        live = self.milestone(self.old_site(), 'M1')
        test_ms = self.milestone(self.old_site(is_test=True), 'M1')
        gone_ms = self.milestone(self.old_site(is_deleted=True), 'M1')
        # H31: an order whose sites are a deleted real site and a live test site.
        mixed = self.pay(sites=[self.old_site(is_deleted=True), self.old_site(is_test=True)])
        kept = self.pay(sites=[self.old_site()])
        spent = self.bill(self.old_site(), '100')
        self.pay(bill=spent, amount='100', approved='100', status=PaymentRequest.APPROVED)
        open_bill = self.bill(self.old_site(), '100')
        self.pay(bill=open_bill, amount='40', approved='40', status=PaymentRequest.APPROVED)
        keys = self.keys()
        self.assertIn(f'milestone-{live.pk}', keys)
        self.assertIn(f'payment-{kept.pk}', keys)
        self.assertIn(f'bill-{open_bill.pk}', keys)
        for gone in (f'milestone-{test_ms.pk}', f'milestone-{gone_ms.pk}',
                     f'payment-{mixed.pk}', f'bill-{spent.pk}'):
            self.assertNotIn(gone, keys)
        bill_item = self.item(f'bill-{open_bill.pk}')
        self.assertEqual((bill_item.amount, bill_item.amount_label),
                         (D('60'), 'not yet sent to Finance'))

    def test_due_and_not_due_agree_with_the_figures(self):
        """FIN-H-B4-1: the list imports finance_head.due_state, the figures' own rule, so
        there is one due rule; the list's due / not-due still match the figures."""
        self.assertIs(items_module.due_state, finance_head_module.due_state)
        s1 = self.site()
        self.milestone(s1, 'M1')
        s2 = self.site()
        self.milestone(s2, 'M2')
        self.trigger(s2, PO_BC)
        self.milestone(self.site(), 'M2')                                 # no trigger: due
        s4 = self.site()
        self.milestone(s4, 'M3')
        self.trigger(s4, HANDOVER, status=Task.NOT_STARTED, is_not_applicable=True)
        s5 = self.site()
        self.milestone(s5, 'M2')
        self.trigger(s5, PO_BC, status=Task.IN_PROGRESS)
        for status in ('Draft', 'Cancelled'):
            self.milestone(self.site(status=status), 'M1')
        r = finance_head_summary(today=self.today)
        self.assertEqual(self.result(status='due')['count'], r['due']['count'])
        self.assertEqual(self.result(status='not_due')['count'], r['not_due']['count'])
        self.assertEqual((r['due']['count'], r['not_due']['count']), (4, 1))


# ---------------------------------------------------------------------------
# History (R-B4-6, A3, A4)
# ---------------------------------------------------------------------------

class MilestoneHistoryTests(ItemsFixture):

    def setUp(self):
        super().setUp()
        self.s = self.old_site()
        self.m1 = self.milestone(self.s, 'M1')
        self.m2 = self.received(self.s, 'M2', '500', self.today - timedelta(days=3))
        self.m3 = self.received(self.s, 'M3', '700', self.today - timedelta(days=4))
        self.t0 = self.ago(3)

    def history(self, m):
        return self.item(f'milestone-{m.pk}').history

    def test_a4_transition_and_log_line_within_60_seconds_show_once(self):
        self.transition(SUBJECT_PAYMENT_MILESTONE, self.m2.pk, 'Received', frm='Pending',
                        actor=self.finance, role='Finance', at=self.t0, project=self.s,
                        reason='milestone_sync')
        text = "M2: status Pending → Received via completion of Finance task 'X' by Fw_Pm (PM)"
        self.log(self.s, text, self.t0 + timedelta(milliseconds=14), entity_id=self.m2.pk,
                 actor=self.pm)
        lines = [l for l in self.history(self.m2) if 'Received' in l['text']]
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]['text'], text)
        self.assertEqual(lines[0]['who_text'], 'Fw_Pm (Finance)')   # log's name, transition's role
        self.assertEqual(self.result(status='received')['merged'], 1)

    def test_two_minutes_apart_show_twice(self):
        self.transition(SUBJECT_PAYMENT_MILESTONE, self.m2.pk, 'Received', frm='Pending',
                        at=self.t0, project=self.s)
        self.log(self.s, 'M2: status Pending → Received',
                 self.t0 + timedelta(seconds=SAME_EVENT_SECONDS * 2), entity_id=self.m2.pk)
        lines = [l for l in self.history(self.m2) if 'Received' in l['text']]
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]['text'], 'M2: status Pending → Received')   # newest first

    def test_older_auto_mark_line_matched_by_site_and_mx(self):
        text = ("Milestone M3 auto-marked Received via completion of Finance task "
                "'100% Payment Confirmation' by Santosh Thakur (Finance)")
        self.log(self.s, text, self.t0, entity_id=None, actor=self.finance)
        m3_lines = self.history(self.m3)
        self.assertEqual([l['text'] for l in m3_lines], [text])
        self.assertEqual(m3_lines[0]['note'], 'older record')
        self.assertEqual(m3_lines[0]['who_text'], 'Santosh Thakur (Finance)')
        self.assertEqual(self.history(self.m2), [])

    def test_bd_amount_lines_attach_to_the_right_mx(self):
        old = 'Set milestone amounts: M2=₹124000'
        new = 'Set milestone amounts: M1 ₹2,000 → ₹3,000'
        self.log(self.s, old, self.ago(5), actor=self.bd)
        self.log(self.s, new, self.ago(4), actor=self.bd)
        self.assertEqual([l['text'] for l in self.history(self.m2)], [old])
        self.assertEqual([l['text'] for l in self.history(self.m1)], [new])
        self.assertEqual(self.history(self.m3), [])

    def test_creation_line_on_all_three(self):
        self.log(self.s, 'Payment milestones M1–M3 created', self.ago(200), actor=self.pm)
        for m in (self.m1, self.m2, self.m3):
            self.assertEqual([l['text'] for l in self.history(m)],
                             ['Payment milestones M1–M3 created'])

    def test_another_sites_lines_stay_on_their_site(self):
        other = self.old_site()
        self.log(other, 'Set milestone amounts: M2=₹5', self.ago(5))
        self.assertEqual(self.history(self.m2), [])


class PaymentHistoryTests(ItemsFixture):

    def test_raise_hold_answer_part_approval_and_payment_in_order(self):
        p = self.pay(sites=[self.old_site()], amount='100', approved='80',
                     status=PaymentRequest.CONFIRMED, payment_date=self.today,
                     approved_at=self.ago(2))
        PaymentRequest.objects.filter(pk=p.pk).update(payment_reference='UTR-1')
        t = [self.ago(6), self.ago(5), self.ago(4), self.ago(2), self.ago(1)]
        S = SUBJECT_PAYMENT_REQUEST
        self.transition(S, p.pk, PaymentRequest.PENDING_APPROVAL, role='SCM', actor=self.scm,
                        at=t[0])
        self.transition(S, p.pk, PaymentRequest.ON_HOLD, frm=PaymentRequest.PENDING_APPROVAL,
                        role='CEO', actor=self.ceo, at=t[1], remark='Check rate')
        self.transition(S, p.pk, PaymentRequest.PENDING_APPROVAL, frm=PaymentRequest.ON_HOLD,
                        role='SCM', actor=self.scm, at=t[2], remark='Rate is right')
        self.transition(S, p.pk, PaymentRequest.APPROVED, frm=PaymentRequest.PENDING_APPROVAL,
                        role='CEO', actor=self.ceo, at=t[3])
        self.transition(S, p.pk, PaymentRequest.CONFIRMED, frm=PaymentRequest.APPROVED,
                        role='Finance', actor=self.finance, at=t[4], remark='UTR-1')
        h = PaymentRequestHold.objects.create(payment_request=p, reason='Check rate',
                                              held_by=self.ceo)
        PaymentRequestHold.objects.filter(pk=h.pk).update(
            held_at=t[1] + timedelta(milliseconds=5), response='Rate is right',
            responded_by=self.scm, responded_at=t[2] + timedelta(milliseconds=5))
        item = self.item(f'payment-{p.pk}', status='paid')
        today = f'{self.today.day} {self.today:%b %Y}'
        self.assertEqual([l['text'] for l in item.history], [
            f'Paid ₹80 on {today}, ref UTR-1',
            'Approved ₹80 of ₹100 requested (part approval)',
            'Hold answered: Rate is right',
            'Put on hold: Check rate',
            'Raised for ₹100',
        ])
        self.assertEqual(item.history[3]['who_text'], 'Fw_Ceo (CEO)')
        self.assertEqual((item.amount, item.amount_label), (D('80'), 'paid'))

    def test_rejected_shows_its_reason(self):
        p = self.pay(sites=[self.old_site()], status=PaymentRequest.REJECTED)
        item = self.item(f'payment-{p.pk}', status='rejected')
        self.assertIn('Rejected: Duplicate', [l['text'] for l in item.history])
        self.assertIn('Raised for ₹100', [l['text'] for l in item.history])


# ---------------------------------------------------------------------------
# The section on the page: amounts, links, empty message, query count
# ---------------------------------------------------------------------------

class PageSectionTests(ItemsFixture):

    def section(self, profile=None, query=''):
        html = self.get(profile or self.head, query).content.decode()
        return html.split('<!-- fhi:start -->', 1)[1].split('<!-- fhi:end -->', 1)[0]

    def row(self, html, key):
        return html.split(f'data-item="{key}"', 1)[1].split('data-item="', 1)[0]

    def test_amount_not_set_instead_of_zero(self):
        m = self.milestone(self.old_site(), 'M1')
        row = self.row(self.section(), f'milestone-{m.pk}')
        self.assertIn('amount not set', row)
        self.assertNotIn('₹0', row)

    def test_bill_link_absent_for_the_finance_head_present_for_the_ceo(self):
        b = self.bill(self.old_site(), '500')
        self.assertNotIn('data-link="approval"', self.row(self.section(self.head),
                                                          f'bill-{b.pk}'))
        self.assertIn(f'href="/approvals/{b.request_id}/" data-link="approval"',
                      self.row(self.section(self.ceo), f'bill-{b.pk}'))

    def test_order_payment_links_to_the_order_page_anchor(self):
        p = self.pay(sites=[self.old_site()])
        self.assertIn(f'href="/orders/{p.vendor_order_id}/#payment-{p.pk}" data-link="order"',
                      self.row(self.section(), f'payment-{p.pk}'))

    def test_milestone_links_to_finance_work_and_the_site(self):
        s = self.old_site(site_code='HZ-9')
        m = self.milestone(s, 'M1')
        row = self.row(self.section(), f'milestone-{m.pk}')
        self.assertIn('href="/finance/work/?focus=milestones&amp;q=HZ-9"', row)
        self.assertIn(f'href="/projects/{s.project_id}/" data-link="site"', row)

    def test_direction_marker_is_text(self):
        m = self.milestone(self.old_site(), 'M1')
        self.assertIn('data-dir="in">↓ In</span>', self.row(self.section(), f'milestone-{m.pk}'))

    def test_empty_message_and_period_kept(self):
        html = self.section(query='?period=fy&status=paid')
        self.assertIn('Nothing matches these filters.', html)
        self.assertIn('<input type="hidden" name="period" value="fy">', html)
        self.assertIn('href="?period=fy#items" class="btn btn-sm btn-outline-secondary"', html)

    def test_count_and_paging_links_keep_the_period(self):
        for _ in range(26):
            self.pay(amount='10')
        html = self.section(query='?period=30d&dir=out')
        self.assertIn('26 items', html)
        self.assertIn('href="?period=30d&amp;dir=out&amp;page=2#items"', html)
        self.assertIn('Page 2 of 2', self.section(query='?period=30d&dir=out&page=7'))


class QueryCountTests(ItemsFixture):
    """A6: 12 for the B3 page + 4 for the FIN-H-B5 checks + 7 for the list = 23, whatever
    the size (B5 answer 11 repinned 19 -> 23)."""

    def add_sites(self, n):
        for _ in range(n):
            s = self.site(contract_value=D('500000'))
            self.milestone(s, 'M1')
            self.milestone(s, 'M2')
            self.trigger(s, PO_BC)
            self.milestone(s, 'M3', status=PaymentMilestone.INVOICED, invoice_amount=D('1000'),
                           invoice_date=self.today)

    def count(self):
        client = self.client_for(self.head)
        with CaptureQueriesContext(connection) as ctx:
            response = client.get(URL)
        self.assertEqual(response.status_code, 200)
        return len(ctx.captured_queries)

    def test_exactly_23_at_3_0_30_0_and_30_10(self):
        self.add_sites(3)
        small = self.count()
        self.add_sites(27)
        large = self.count()
        sites = [self.site() for _ in range(2)]
        for i in range(10):
            status = (PaymentRequest.CONFIRMED if i % 3 == 0 else
                      PaymentRequest.APPROVED if i % 3 == 1 else
                      PaymentRequest.PENDING_APPROVAL)
            self.pay(sites=sites, ptype='OPEX' if i % 2 else 'Residential', status=status,
                     approved='100' if status != PaymentRequest.PENDING_APPROVAL else None,
                     payment_date=self.today if status == PaymentRequest.CONFIRMED else None,
                     hold_at=self.ago(1) if i == 2 else None)
        with_payments = self.count()
        print(f'\nFIN-H-B4 queries: 3/0={small}, 30/0={large}, 30/10={with_payments}')
        self.assertEqual((small, large, with_payments), (23, 23, 23))

    def test_23_when_the_filters_leave_nothing(self):
        self.add_sites(3)
        client = self.client_for(self.head)
        with CaptureQueriesContext(connection) as ctx:
            client.get(URL + '?status=paid&q=nothing-matches')
        self.assertEqual(len(ctx.captured_queries), 23)


# ---------------------------------------------------------------------------
# FIN-H-B5: ?check= (B5 answers 4 and 8)
# ---------------------------------------------------------------------------

class CheckFilterTests(ItemsFixture):

    def checked(self, **params):
        filters = check_filters(finance_head_checks('month', today=self.today)['checks'])
        return finance_head_items(clean_item_params(params), False, today=self.today,
                                  checks=filters), filters

    def build_one_of_each(self):
        old = self.old_site()
        self.received(old, 'M1', None, self.today - timedelta(days=60))   # 2 + 1
        not_due = self.old_site()
        self.milestone(not_due, 'M2')                                     # 1, not due
        self.trigger(not_due, PO_BC, status=Task.IN_PROGRESS)
        late = self.old_site()
        self.milestone(late, 'M3', amount=D('9'))
        self.trigger(late, HANDOVER, done_days_ago=10)                    # 6
        self.milestone(self.old_site(), 'M2', status=PaymentMilestone.INVOICED,
                       amount=D('5'))                                     # 4
        coord = self.received(self.old_site(), 'M1', '5', self.today - timedelta(days=50))
        self.transition(SUBJECT_PAYMENT_MILESTONE, coord.pk, PaymentMilestone.RECEIVED,
                        actor=self.coord, role='Project Coordinator', at=self.ago(50))   # 3
        changed = self.received(self.old_site(), 'M1', '5', self.today - timedelta(days=40))
        self.transition(SUBJECT_PAYMENT_MILESTONE, changed.pk, PaymentMilestone.RECEIVED,
                        at=self.ago(40))
        line = ActivityLog.objects.create(project=changed.project, actor=self.pm,
                                          action='M1: amount ₹1 → ₹2', entity_type='Milestone',
                                          entity_id=changed.pk,
                                          action_code='milestone_amounts_set')
        ActivityLog.objects.filter(pk=line.pk).update(timestamp=self.ago(5))            # 8
        self.pay(sites=[self.old_site()], status=PaymentRequest.APPROVED, approved='10',
                 approved_at=self.ago(9))                                               # 5 + 7
        self.milestone(self.old_site(status='Draft'), 'M1')    # H26: in no check, no list

    def test_every_check_opens_exactly_its_items_count_equals_opened(self):
        self.build_one_of_each()
        result, filters = self.checked()
        for key, check in filters.items():
            with self.subTest(check=key):
                self.assertGreater(check['count'], 0)
                opened, _ = self.checked(check=key)
                self.assertEqual({i.key for i in opened['items']}, set(check['items']))
                self.assertEqual(opened['count'], check['count'])

    def test_direction_status_and_window_ignored_type_and_search_apply(self):
        self.build_one_of_each()
        plain, filters = self.checked(check='no_amount')
        wanted = set(filters['no_amount']['items'])
        ignored, _ = self.checked(check='no_amount', dir='out', status='paid')
        self.assertEqual({i.key for i in ignored['items']}, wanted)
        self.assertEqual(self.checked(check='no_amount', type='OPEX')[0]['count'], 0)
        self.assertEqual(self.checked(check='no_amount', q='nothing-like-this')[0]['count'], 0)
        self.assertEqual(plain['filters']['windowed'], False)
        self.assertEqual(plain['filters']['check']['label'], 'Milestone amount not set')
        self.assertIn('check=no_amount', plain['filters']['next_href'] or
                      plain['filters']['clear_href'] + 'check=no_amount')

    def test_unknown_or_malformed_key_is_ignored(self):
        self.build_one_of_each()
        default, _ = self.checked()
        for value in ('nope', 'NO_AMOUNT', 'x' * 60, '<script>'):
            with self.subTest(value=value):
                result, _ = self.checked(check=value)
                self.assertEqual([i.key for i in result['items']],
                                 [i.key for i in default['items']])
                self.assertIsNone(result['filters']['check'])

    def test_each_item_carries_its_note(self):
        self.build_one_of_each()
        result, _ = self.checked(check='by_coordinator')
        self.assertEqual([i.check_note for i in result['items']],
                         ['Recorded by Fw_Coord (Project Coordinator)'])

    def test_the_section_says_which_check_and_keeps_it_in_links(self):
        self.build_one_of_each()
        html = self.client_for(self.head).get(URL + '?period=fy&check=late_payment').content.decode()
        section = html.split('<!-- fhi:start -->', 1)[1].split('<!-- fhi:end -->', 1)[0]
        self.assertIn('data-items-check', section)
        self.assertIn('Approved, not paid 7+ days', section)
        self.assertIn('<input type="hidden" name="check" value="late_payment">', section)
        self.assertIn('href="?period=fy#items"', section)                # clear the check
        self.assertIn('data-check-note', section)

    def test_direction_and_status_hidden_under_a_check_shown_without(self):
        # B5 follow-up answer 2: a check ignores both, so neither dropdown is drawn; the
        # banner and its "Show every item" link stay. Type, sort and search remain.
        self.build_one_of_each()
        client = self.client_for(self.head)

        def section(query):
            html = client.get(URL + query).content.decode()
            return html.split('<!-- fhi:start -->', 1)[1].split('<!-- fhi:end -->', 1)[0]

        checked = section('?check=no_amount')
        self.assertNotIn('name="dir"', checked)
        self.assertNotIn('name="status"', checked)
        for kept in ('name="type"', 'name="sort"', 'name="q"', 'data-items-check',
                     'data-check-clear'):
            self.assertIn(kept, checked)
        plain = section('')
        self.assertIn('name="dir"', plain)
        self.assertIn('name="status"', plain)
        self.assertNotIn('data-items-check', plain)
