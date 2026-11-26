"""
FIN-M-B5-8 — the Finance Head page follows Finance-confirmed client receipts while
CLIENT_RECEIPTS_LIVE is on; with it off the page is unchanged (K1).

THE RULES PINNED HERE (rulings K1-K14, Q1-Q8 of 25-26 Nov 2026; FR2, FR13, FR37, FR42,
H60, H61, H66):
  * Switch off: receipts in the database change nothing on the page, at 23 queries.
  * money_in: confirmed receipts on their own date received (two receipts on one
    milestone land in different periods and weeks); recorded, queried and rejected never
    count, an undone confirmation drops out; a legacy milestone counts its old amount on
    its old date until a confirmed receipt replaces it, and again when every confirmation
    is undone; a straggler counts at amount_received (Q1); test, deleted and
    non-Residential sites are out (K3, K12); an overpayment counts in full.
  * The awaiting line: recorded + queried, never in money in.
  * Owed: agreed (or invoice) less confirmed, never below 0, one milestone's excess never
    reducing another's (K9).
  * Checks: each new or changed check's count equals what ?check= opens (H66); "No BD
    set" counts a Draft site and lists its sites in place, with no ?check= (Q2); "No
    agreed amount" keeps every item and says how many are not yet received (Q3);
    setting an agreed amount that turns a milestone Received never fires "Amount changed
    after Received" (K8e, through the real BD view).
  * History: each receipt event once, with amount, date received and person; the
    confirmed line names both dates (FR13); the CEO sees the lines but no receipt link,
    the Finance Head both (Q5). The marking line is history, never movement (Q4, both
    switch states).
  * Who recorded what: a BD who recorded appears with the role; an idle Finance user
    appears; alphabetical; recorded vs confirmed; old pre-receipt moves in "Receipts
    recorded" only, with the footnote's count (Q7); no totals; no POST form (H46).
  * Queries: 23 off and 27 on, at 3 sites / 0 receipts and at 30 sites / 40 receipts.

    python manage.py test projects.tests_finance_head_receipts --settings=solarpms.test_settings
"""
import json
import re
from datetime import date, timedelta
from decimal import Decimal

from django.db import connection
from django.test.utils import CaptureQueriesContext, override_settings
from django.urls import reverse
from django.utils import timezone

from .client_money import money_in
from .finance_head import finance_head_summary, live_residential_milestones
from .finance_head_checks import check_filters, finance_head_checks
from .finance_head_items import LEGACY_MARKED_CODE, clean_item_params, finance_head_items
from .management.commands.mark_legacy_received_milestones import CODE as MARK_CODE
from .models import (SUBJECT_PAYMENT_MILESTONE, ActivityLog, PaymentMilestone,
                     PaymentReceipt, Project, StatusTransition)
from .receipts import (answer_receipt_query, confirm_receipt, query_receipt, record_receipt,
                       reject_receipt, unconfirm_receipt)
from .tests_finance_head_checks import COORD, ChecksFixture
from .tests_finance_head_page import URL
from .tests_finance_work import _profile

D = Decimal
LIVE = override_settings(CLIENT_RECEIPTS_LIVE=True)
OFF = override_settings(CLIENT_RECEIPTS_LIVE=False)
TOKEN = re.compile(r'[A-Za-z0-9]{64}')
#: A fixed "today" for period and week edges (a Monday-start week of 26 Oct holds Sat 31
#: Oct; 2 Nov is the next Monday). Every receipt date here is in the past for real too,
#: so the writers' "not after today" rule never refuses them.
NOV16 = date(2026, 11, 16)


class ReceiptsFixture(ChecksFixture):
    """Receipts through the real writers: recorded by the plain Finance user, acted on by
    the Finance Head (also Finance; never the recorder, FR11)."""

    def rec(self, m, amount, day, by=None):
        m = PaymentMilestone.objects.get(pk=m.pk)
        return record_receipt((by or self.finance).user, m, D(amount), day, 'upi').receipt

    def confirmed(self, m, amount, day, by=None, note=''):
        r = self.rec(m, amount, day, by)
        confirm_receipt(self.head.user, r, overpayment_note=note)
        return PaymentReceipt.objects.get(pk=r.pk)

    def queried(self, m, amount, day):
        r = self.rec(m, amount, day)
        query_receipt(self.head.user, r, 'Check the bank line')
        return r

    def rejected(self, m, amount, day):
        r = self.rec(m, amount, day)
        reject_receipt(self.head.user, r, 'Duplicate')
        return r

    def undone(self, r):
        unconfirm_receipt(self.head.user, PaymentReceipt.objects.get(pk=r.pk), 'Wrong site')

    def direct(self, m, amount, day, status=PaymentReceipt.CONFIRMED):
        """A receipt written straight to the table, for sites the writers refuse (test,
        deleted, non-Residential): the page's scope, not the writer, must keep it out."""
        return PaymentReceipt.objects.create(
            milestone=m, status=status, amount=D(amount), date_received=day, mode='upi',
            recorded_by=self.finance,
            confirmed_by=self.head if status == PaymentReceipt.CONFIRMED else None,
            confirmed_at=timezone.now() if status == PaymentReceipt.CONFIRMED else None)

    def legacy(self, site, name, old, day, amount=None):
        """An old Received milestone as mark_legacy_received_milestones leaves it."""
        m = self.received(site, name, old, day)
        PaymentMilestone.objects.filter(pk=m.pk).update(
            received_before_receipts=True, amount=amount,
            legacy_amount_received=D(old) if old is not None else None,
            legacy_received_date=day, legacy_marked_at=timezone.now())
        return PaymentMilestone.objects.get(pk=m.pk)

    def money(self, period='month', today=NOV16):
        return finance_head_summary(period, today=today, receipts_on=True)

    def live_checks(self, period='month'):
        return {c['key']: c for c in
                finance_head_checks(period, today=self.today, receipts_on=True)['checks']}

    def items(self, viewer=None, **params):
        filters = check_filters(finance_head_checks('month', today=self.today,
                                                    receipts_on=True)['checks'])
        return finance_head_items(clean_item_params(params), False, today=self.today,
                                  checks=filters, receipts_on=True,
                                  viewer=(viewer or self.head).user)

    def item(self, m, **params):
        params.setdefault('q', m.project.project_id)
        found = [i for i in self.items(**params)['items'] if i.key == f'milestone-{m.pk}']
        self.assertEqual(len(found), 1, f'milestone {m.pk} not listed with {params}')
        return found[0]

    def received_words(self, day):
        """The history's 'received 2 Nov' (the year only when not this year's)."""
        return f'received {day.day} {day:%b}' + ('' if day.year == self.today.year
                                                  else f' {day.year}')


# ---------------------------------------------------------------------------
# Switch off (K1)
# ---------------------------------------------------------------------------

@OFF
class SwitchOffTests(ReceiptsFixture):

    def page(self):
        client = self.client_for(self.head)
        with CaptureQueriesContext(connection) as ctx:
            html = client.get(URL).content.decode()
        return TOKEN.sub('T', html), len(ctx.captured_queries)

    def test_receipts_in_the_database_change_nothing_at_23_queries(self):
        site = self.site()
        m1 = self.milestone(site, 'M1', amount=D('100000'))
        old = self.legacy(self.site(), 'M1', '53500', self.today - timedelta(days=3))
        before, n_before = self.page()
        # Written with the switch off: the writers work, recompute does nothing (FR38).
        self.confirmed(m1, '30000', self.today - timedelta(days=2))
        self.rec(m1, '5000', self.today)
        self.confirmed(old, '10000', self.today - timedelta(days=1))
        after, n_after = self.page()
        self.assertEqual(before, after)
        self.assertEqual((n_before, n_after), (23, 23))
        self.assertNotIn('Receipts confirmed', after)
        self.assertIn('Money in (recorded, Residential)', after)


# ---------------------------------------------------------------------------
# money_in (K2-K4, K6, K12)
# ---------------------------------------------------------------------------

@LIVE
class MoneyInTests(ReceiptsFixture):

    def test_two_receipts_either_side_of_a_month_edge_land_in_their_own_period_and_week(self):
        m = self.milestone(self.site(), 'M1', amount=D('100000'))
        self.confirmed(m, '30000', date(2026, 10, 31))        # Saturday: week of 26 Oct
        self.confirmed(m, '20000', date(2026, 11, 2))         # Monday: week of 2 Nov
        s = self.money()
        self.assertEqual((s['received_period']['sum'], s['received_period']['count']),
                         (D('20000'), 1))
        self.assertEqual(s['received_period']['basis'], 'date_received')
        self.assertEqual(self.money('30d')['received_period']['sum'], D('50000'))
        weeks = {w['week_start']: w['in_sum'] for w in s['weekly']}
        self.assertEqual(weeks[date(2026, 10, 26)], D('30000'))
        self.assertEqual(weeks[date(2026, 11, 2)], D('20000'))
        self.assertEqual(s['by_type']['Residential']['in_period'], D('20000'))

    def test_recorded_queried_rejected_never_count_undo_drops_out_awaiting_apart(self):
        m = self.milestone(self.site(), 'M1', amount=D('100000'))
        day = date(2026, 11, 2)
        self.rec(m, '1000', day)
        self.queried(m, '2000', day)
        self.rejected(m, '3000', day)
        self.undone(self.confirmed(m, '4000', day))
        self.confirmed(m, '5000', day)
        s = self.money()
        self.assertEqual((s['received_period']['sum'], s['received_period']['count']),
                         (D('5000'), 1))
        self.assertEqual(s['received_all_time']['sum'], D('5000'))
        # FR2: recorded 1,000 + queried 2,000 + the undone 4,000 (back to recorded).
        self.assertEqual(s['awaiting_receipts'],
                         {'sum': D('7000'), 'count': 3, 'queried': 1})

    def test_legacy_old_amount_until_a_confirmed_receipt_and_back_when_all_undone(self):
        m = self.legacy(self.site(), 'M3', '53500', date(2026, 11, 5), amount=D('39198'))
        s = self.money()
        self.assertEqual((s['received_period']['sum'], s['received_period']['old_sum']),
                         (D('53500'), D('53500')))
        r = self.confirmed(m, '10000', date(2026, 11, 3))
        s = self.money()
        self.assertEqual((s['received_period']['sum'], s['received_period']['old_sum']),
                         (D('10000'), D('0')))                               # FR37
        self.undone(r)
        self.assertEqual(self.money()['received_period']['sum'], D('53500'))

    def test_a_legacy_milestone_with_no_old_amount_is_counted_without_amount(self):
        self.legacy(self.site(), 'M1', None, date(2026, 11, 5))
        s = self.money()
        self.assertEqual((s['received_period']['count'], s['received_period']['sum'],
                          s['received_period']['null_amount_count']), (1, D('0'), 1))

    def test_a_straggler_counts_at_amount_received_on_its_received_date(self):
        # Q1: Received through an old path, not marked, no receipt row.
        self.received(self.site(), 'M1', '7000', date(2026, 11, 4))
        s = self.money()
        self.assertEqual((s['received_period']['sum'], s['received_period']['old_count']),
                         (D('7000'), 1))

    def test_an_overpayment_counts_in_full(self):
        m = self.milestone(self.site(), 'M1', amount=D('1000'))
        self.confirmed(m, '1500', date(2026, 11, 2), note='Client paid the M2 advance too')
        self.assertEqual(self.money()['received_period']['sum'], D('1500'))

    def test_test_deleted_and_non_residential_sites_are_out(self):
        # K3 / K12: a receipt the writers would refuse, written straight to the table.
        day = date(2026, 11, 4)
        for fields in ({'is_test': True}, {'is_deleted': True}, {'project_type': 'OPEX'}):
            with self.subTest(**fields):
                m = self.received(self.site(**fields), 'M1', '9000', day)
                self.direct(m, '9000', day)
                self.direct(self.milestone(self.site(**fields), 'M2'), '400', day,
                            status=PaymentReceipt.RECORDED)
        s = self.money()
        self.assertEqual((s['received_all_time']['count'], s['awaiting_receipts']['count']),
                         (0, 0))
        self.assertEqual(self.items(status='received')['count'], 0)
        self.assertEqual(self.live_checks()['awaiting_confirmation']['count'], 0)

    def test_money_in_is_two_queries_whatever_the_size(self):
        for n in range(6):
            m = self.milestone(self.site(), 'M1', amount=D('1000'))
            self.confirmed(m, '100', date(2026, 11, 2))
            self.legacy(self.site(), 'M2', '50', date(2026, 11, 3))
        with CaptureQueriesContext(connection) as ctx:
            result = money_in(live_residential_milestones())
            result.between(date(2026, 11, 1), NOV16)
        self.assertEqual(len(ctx.captured_queries), 2)


# ---------------------------------------------------------------------------
# Owed (K9)
# ---------------------------------------------------------------------------

@LIVE
class OwedTests(ReceiptsFixture):

    def test_part_paid_never_below_zero_and_no_cross_offset(self):
        due = self.milestone(self.site(), 'M1', amount=D('1000'))
        self.confirmed(due, '300', date(2026, 11, 2))
        over = self.milestone(self.site(), 'M2', status=PaymentMilestone.INVOICED,
                              amount=D('1000'), invoice_amount=D('400'),
                              invoice_date=self.today)
        self.confirmed(over, '500', date(2026, 11, 3))   # past the invoice, short of agreed
        self.milestone(self.site(), 'M2', status=PaymentMilestone.INVOICED,
                       amount=D('900'), invoice_amount=D('300'), invoice_date=self.today)
        s = finance_head_summary('month', today=self.today, receipts_on=True)
        self.assertEqual(s['due']['sum'], D('700'))
        # 0 for the over-paid invoice, never -100 taken off the other one's 300.
        self.assertEqual(s['invoiced']['sum'], D('300'))
        self.assertEqual(s['by_type']['Residential']['to_collect'], D('1000'))
        off = finance_head_summary('month', today=self.today)
        self.assertEqual((off['due']['sum'], off['invoiced']['sum']), (D('1000'), D('700')))

    def test_the_item_keeps_its_pill_shows_the_label_and_what_is_owed(self):
        m = self.milestone(self.site(), 'M1', amount=D('1000'))
        self.confirmed(m, '300', date(2026, 11, 2))
        item = self.item(m)
        self.assertEqual((item.status_label, item.amount), ('Due', D('700')))
        self.assertEqual(item.money_label, 'Part received ₹300 of ₹1,000')
        plain = self.item(self.milestone(self.site(), 'M1', amount=D('5')))
        self.assertEqual(plain.money_label, '')            # "Pending" says nothing more
        html = self.client_for(self.head).get(URL + f'?q={m.project.project_id}')
        self.assertContains(html, '<span class="pill warn">Due</span><span class="meta" '
                                  'data-money-label>Part received ₹300 of ₹1,000</span>')


# ---------------------------------------------------------------------------
# Checks (K7, K8, H66)
# ---------------------------------------------------------------------------

@LIVE
class CheckTests(ReceiptsFixture):

    def opened(self, key):
        return {i.key for i in self.items(check=key)['items']}

    def test_new_and_changed_checks_count_what_they_open(self):
        self.milestone(self.site(), 'M1')                                     # no amount
        self.legacy(self.site(), 'M1', '500', self.today - timedelta(days=40))
        waiting = self.milestone(self.site(), 'M1', amount=D('1000'))
        self.rec(waiting, '100', self.today)
        self.queried(waiting, '50', self.today)
        checks = self.live_checks()
        for key in ('no_amount', 'received_no_amount', 'awaiting_confirmation'):
            with self.subTest(check=key):
                self.assertGreater(checks[key]['count'], 0)
                self.assertEqual(self.opened(key), set(checks[key]['items']))
                self.assertEqual(self.items(check=key)['count'], checks[key]['count'])
        self.assertEqual(list(checks)[-2:], ['awaiting_confirmation', 'no_bd'])
        self.assertEqual(checks['awaiting_confirmation']['extra'],
                         '1 milestone · ₹150 (1 queried)')

    def test_received_before_receipts_not_yet_recorded(self):
        old = self.legacy(self.site(), 'M1', '500', date(2026, 11, 2))
        blank = self.legacy(self.site(), 'M1', None, date(2026, 11, 2))
        only_rejected = self.legacy(self.site(), 'M1', '400', date(2026, 11, 2))
        self.rejected(only_rejected, '400', date(2026, 11, 2))
        recorded = self.legacy(self.site(), 'M1', '300', date(2026, 11, 2))
        self.rec(recorded, '300', date(2026, 11, 2))
        straggler = self.received(self.site(), 'M1', '200', date(2026, 11, 2))
        paid = self.milestone(self.site(), 'M1', amount=D('100'))
        self.confirmed(paid, '100', date(2026, 11, 2))                   # Received now
        check = self.live_checks()['received_no_amount']
        self.assertEqual(check['label'], 'Received before receipts, not yet recorded')
        self.assertEqual(check['items'], {f'milestone-{old.pk}': 'old amount ₹500',
                                          f'milestone-{blank.pk}': 'no amount recorded',
                                          f'milestone-{only_rejected.pk}': 'old amount ₹400',
                                          f'milestone-{straggler.pk}': 'not yet marked'})
        self.assertEqual(check['extra'], '1 with no amount recorded · 1 not yet marked')

    def test_no_agreed_amount_keeps_every_item_and_says_how_many_are_not_received(self):
        pending = self.milestone(self.site(), 'M1')
        received = self.legacy(self.site(), 'M1', '500', date(2026, 11, 2))
        check = self.live_checks()['no_amount']
        self.assertEqual(check['label'], 'No agreed amount')
        self.assertEqual(set(check['items']),
                         {f'milestone-{pending.pk}', f'milestone-{received.pk}'})
        self.assertEqual(check['extra'], "1 not yet received — receipts can't complete these")
        with OFF:
            off = {c['key']: c for c in finance_head_checks('month', today=self.today)['checks']}
        self.assertEqual((off['no_amount']['label'], off['no_amount']['extra'],
                          off['no_amount']['count']), ('Milestone amount not set', '', 2))
        self.assertNotIn('awaiting_confirmation', off)

    def test_no_bd_set_counts_sites_a_draft_one_included_and_lists_them_in_place(self):
        draft = self.site(status='Draft')
        active = self.site()
        with_bd = self.site()
        Project.objects.filter(pk=with_bd.pk).update(bd=self.bd)
        self.site(status='Cancelled')
        self.site(is_test=True)
        self.site(project_type='OPEX')
        check = self.live_checks()['no_bd']
        self.assertEqual(check['count'], 2)
        self.assertEqual({s['pk'] for s in check['sites']}, {draft.pk, active.pk})
        self.assertNotIn('no_bd', check_filters(list(self.live_checks().values())))
        client = self.client_for(self.head)
        html = client.get(URL).content.decode()
        row = html.split('data-check="no_bd"', 1)[1].split('</li>\n', 1)[0]
        self.assertIn('<span class="fht-n" data-check-count>2</span>', row)
        self.assertNotIn('data-check-link', row)
        self.assertIn('<details class="meta" data-check-sites>', row)
        self.assertNotIn('<details class="meta" data-check-sites open', row)
        self.assertIn(reverse('project_overview', args=[draft.project_id]), row)
        self.assertEqual(row.count('<li data-site='), 2)                      # H66
        # An old or hand-made ?check=no_bd is an unknown key: the normal list.
        self.assertNotIn('data-items-check', client.get(URL + '?check=no_bd').content.decode())

    def test_setting_the_agreed_amount_that_turns_it_received_is_not_a_change(self):
        # K8e through the real BD view: confirmed money first, then the agreed amount.
        site = self.site()
        m = self.milestone(site, 'M1')
        self.confirmed(m, '25000', date(2026, 10, 20), by=self.finance)
        response = self.client_for(self.bd).post(
            reverse('set_milestone_amounts', args=[site.project_id]),
            data=json.dumps({'m1_amount': 25000, 'm2_amount': None, 'm3_amount': None}),
            content_type='application/json')
        self.assertEqual(response.json(), {'success': True})
        m.refresh_from_db()
        self.assertEqual(m.status, PaymentMilestone.RECEIVED)
        self.assertEqual(self.live_checks()['changed_after_received']['count'], 0)


# ---------------------------------------------------------------------------
# History and links (K10, Q4, Q5)
# ---------------------------------------------------------------------------

@LIVE
class HistoryTests(ReceiptsFixture):

    def test_each_receipt_event_once_with_amount_date_and_person(self):
        m = self.milestone(self.site(), 'M1', amount=D('50000'))
        day = date(2026, 11, 2)
        r = self.queried(m, '20000', day)
        answer_receipt_query(self.finance.user, PaymentReceipt.objects.get(pk=r.pk),
                             'Bank line attached')
        confirm_receipt(self.head.user, PaymentReceipt.objects.get(pk=r.pk))
        self.undone(r)
        reject_receipt(self.head.user, PaymentReceipt.objects.get(pk=r.pk), 'Duplicate')
        lines = [l['text'] for l in self.item(m).history]
        got = self.received_words(day)
        for text in (f'₹20,000 recorded — {got}',
                     f'₹20,000 queried — {got}: Check the bank line',
                     f'Query on ₹20,000 answered — {got}: Bank line attached',
                     f'₹20,000 confirmed by Fh_Head — {got}',
                     f'₹20,000 confirmation undone — {got}: Wrong site',
                     f'₹20,000 rejected — {got}: Duplicate'):
            with self.subTest(text=text):
                self.assertEqual(lines.count(text), 1)
        confirmed = next(l for l in self.item(m).history if 'confirmed by' in l['text'])
        # FR13: the line's own time is the confirmation (today), the money's day beside it.
        self.assertEqual(timezone.localtime(confirmed['at']).date(), self.today)
        self.assertEqual(confirmed['who_text'], 'Fh_Head (Finance)')

    def test_the_ceo_sees_the_lines_but_no_receipt_link_the_head_both(self):
        m = self.milestone(self.site(), 'M1', amount=D('50000'))
        r = self.rec(m, '15000', date(2026, 11, 2))
        link = reverse('receipt_detail', args=[m.project.project_id, r.pk])
        query = '?check=awaiting_confirmation'
        ceo = self.client_for(self.ceo).get(URL + query).content.decode()
        head = self.client_for(self.head).get(URL + query).content.decode()
        self.assertIn('₹15,000 recorded', ceo)
        self.assertNotIn(link, ceo)
        self.assertNotIn('data-link="receipt"', ceo)
        self.assertIn(f'href="{link}" data-link="receipt"', head)

    def test_every_receipt_history_line_links_for_the_head_none_for_the_ceo(self):
        # Wording answer 3: confirmed and rejected receipts link from the history too,
        # only for a viewer the receipt page admits (F5).
        m = self.milestone(self.site(), 'M1', amount=D('50000'))
        kept = self.confirmed(m, '20000', date(2026, 11, 2))
        gone = self.rejected(m, '1000', date(2026, 11, 3))
        waiting = self.rec(m, '5000', date(2026, 11, 4))
        links = {r.pk: reverse('receipt_detail', args=[m.project.project_id, r.pk])
                 for r in (kept, gone, waiting)}
        head = [l for l in self.item(m).history if l['source'] == 'receipt']
        ceo = [l for l in self.item(m, viewer=self.ceo).history if l['source'] == 'receipt']
        self.assertEqual({l['href'] for l in head}, set(links.values()))
        self.assertEqual(len(ceo), len(head))
        self.assertEqual({l['href'] for l in ceo}, {''})
        query = '?check=awaiting_confirmation'
        page = self.client_for(self.head).get(URL + query).content.decode()
        for link in links.values():
            self.assertIn(f'<a href="{link}" data-history-link="receipt">Open receipt</a>',
                          page)
        ceo_page = self.client_for(self.ceo).get(URL + query).content.decode()
        self.assertIn('₹20,000 confirmed by', ceo_page)
        self.assertNotIn('data-history-link', ceo_page)

    def test_the_marking_line_is_history_never_movement_in_both_states(self):
        self.assertEqual(LEGACY_MARKED_CODE, MARK_CODE)
        m = self.legacy(self.old_site(), 'M1', '500', self.today - timedelta(days=60))
        line = ActivityLog.objects.create(project=m.project, actor=self.finance,
                                          action='M1: marked as received before receipts',
                                          entity_type='Milestone', entity_id=m.pk,
                                          action_code=LEGACY_MARKED_CODE)
        self.assertIsNotNone(line.timestamp)
        for state in (OFF, LIVE):
            with self.subTest(live=state is LIVE), state:
                default = finance_head_items(clean_item_params({}), False, today=self.today,
                                             receipts_on=state is LIVE)
                self.assertNotIn(f'milestone-{m.pk}', [i.key for i in default['items']])
                chosen = finance_head_items(clean_item_params({'status': 'received'}), False,
                                            today=self.today, receipts_on=state is LIVE)
                item = next(i for i in chosen['items'] if i.key == f'milestone-{m.pk}')
                self.assertIn('M1: marked as received before receipts',
                              [l['text'] for l in item.history])

    def test_a_receipt_move_is_movement(self):
        m = self.received(self.old_site(), 'M1', '500', self.today - timedelta(days=60))
        self.rec(m, '100', self.today - timedelta(days=60))       # recorded today
        self.assertIn(f'milestone-{m.pk}', [i.key for i in self.items()['items']])


# ---------------------------------------------------------------------------
# Who recorded what (K11, Q7, H60, H46)
# ---------------------------------------------------------------------------

@LIVE
class WhoTests(ReceiptsFixture):

    def who_live(self, period='month'):
        return finance_head_checks(period, today=self.today, receipts_on=True)['who']

    def row(self, who, name):
        rows = [r for r in who['rows'] if r['name'] == name]
        self.assertEqual(len(rows), 1, f'{name}: {len(rows)} rows')
        return rows[0]

    def test_recorded_and_confirmed_per_person_with_roles_alphabetical(self):
        idle = _profile('aa_idle_fin', 'Finance')
        site = self.site()
        Project.objects.filter(pk=site.pk).update(bd=self.bd)
        self.confirmed(self.milestone(site, 'M1', amount=D('90000')), '25000',
                       self.today, by=self.bd)
        other = self.milestone(self.site(), 'M2', amount=D('90000'))
        self.confirmed(other, '10000', self.today)
        self.undone(self.confirmed(other, '4000', self.today))
        self.rejected(other, '1000', self.today)
        who = self.who_live()
        bd = self.row(who, 'Fw_Bd')
        self.assertEqual((bd['roles'], bd['received_n'], bd['received_sum'], bd['confirmed_n']),
                         (['BD'], 1, D('25000'), 0))
        fin = self.row(who, 'Fw_Fin')
        self.assertEqual((fin['received_n'], fin['received_sum']), (3, D('15000')))
        head = self.row(who, 'Fh_Head')
        # The undone confirmation is no longer confirmed; the recompute that turned
        # nothing Received here is never read as a recording.
        self.assertEqual((head['confirmed_n'], head['confirmed_sum'], head['received_n']),
                         (2, D('35000'), 0))
        self.assertEqual(self.row(who, 'Aa_Idle_Fin')['received_n'], 0)
        names = [r['name'] for r in who['rows']]
        self.assertEqual(names, sorted(names, key=str.casefold))
        self.assertEqual(idle.role, 'Finance')

    def test_the_section_has_no_totals_and_no_post_form(self):
        self.confirmed(self.milestone(self.site(), 'M1', amount=D('900')), '100', self.today)
        html = self.client_for(self.head).get(URL).content.decode()
        section = html.split('<!-- fht:start -->', 1)[1].split('<!-- fht:end -->', 1)[0]
        self.assertIn('<th scope="col">Receipts recorded</th><th scope="col">Receipts '
                      'confirmed</th>', section)
        self.assertNotIn('Total', section)
        self.assertNotIn('%', section.split('</style>', 1)[1])
        self.assertNotIn('<form', section)

    def test_old_moves_on_marked_milestones_count_as_recorded_only_with_the_footnote(self):
        m = self.received_by(self.site(), 'M1', self.coord, COORD, self.ago(2), got='800')
        PaymentMilestone.objects.filter(pk=m.pk).update(
            received_before_receipts=True, legacy_amount_received=D('800'),
            legacy_received_date=m.received_date)
        # A milestone recompute turns Received: its transition's actor is the confirmer.
        paid = self.milestone(self.site(), 'M1', amount=D('100'))
        self.confirmed(paid, '100', self.today)
        self.assertTrue(StatusTransition.objects.filter(
            subject_type=SUBJECT_PAYMENT_MILESTONE, subject_id=paid.pk,
            to_status=PaymentMilestone.RECEIVED, actor=self.head).exists())
        who = self.who_live('30d')
        coord = self.row(who, 'Fw_Coord')
        self.assertEqual((coord['received_n'], coord['received_sum'], coord['confirmed_n'],
                          coord['roles']), (1, D('800'), 0, [COORD]))
        self.assertEqual(self.row(who, 'Fh_Head')['received_n'], 0)
        self.assertEqual(who['old_receipts'], 1)
        html = self.client_for(self.head).get(URL + '?period=30d').content.decode()
        self.assertIn('Receipts recorded includes 1 recorded before receipt records began '
                      '(older receipts may have no amount).', html)


# ---------------------------------------------------------------------------
# The page's words (K5, K6)
# ---------------------------------------------------------------------------

@LIVE
class WordingTests(ReceiptsFixture):

    def test_money_in_words_old_records_line_and_awaiting_row(self):
        self.legacy(self.site(), 'M1', '53500', self.today)
        m = self.milestone(self.site(), 'M1', amount=D('90000'))
        self.confirmed(m, '20000', self.today)
        self.rec(m, '5000', self.today)
        self.queried(m, '1000', self.today)
        html = self.client_for(self.head).get(URL).content.decode()
        self.assertIn('— confirmed by Finance, by date received', html)
        self.assertIn('includes ₹53,500 from old records (before receipts)', html)
        self.assertIn('₹6,000 recorded, awaiting Finance confirmation (2 receipts)', html)
        self.assertIn('Money in (received, confirmed by Finance, Residential)', html)
        tile = html.split('data-tile="in"', 1)[1].split('</div>', 1)[0]
        self.assertIn('₹73,500', tile)                       # never the ₹6,000 awaiting

    def test_why_lines_switch_on_and_off(self):
        on = {k: c['why'] for k, c in self.live_checks().items()}
        self.assertEqual(on['no_bd'], "The BD can't record the advance (M1) on these "
                                      "sites; only the PM side or Finance can.")
        self.assertEqual(on['received_no_amount'],
                         'Old money not yet backed by receipt records. Where no amount '
                         'was recorded, money in is understated.')
        self.assertEqual(on['awaiting_confirmation'], 'Recorded money counts nowhere '
                                                      'until Finance checks it against the bank.')
        off = {c['key']: c['why'] for c in
               finance_head_checks('month', today=self.today)['checks']}
        self.assertEqual(off['received_no_amount'], 'Money in is understated.')
        self.assertNotIn('no_bd', off)


# ---------------------------------------------------------------------------
# Query counts (K13)
# ---------------------------------------------------------------------------

class QueryCountTests(ReceiptsFixture):
    """23 off, 27 on (+1 figures, +2 checks, +1 list), the same at both sizes and on every
    view of the page."""

    VIEWS = ('', '?check=awaiting_confirmation', '?status=received', '?period=fy&q=FW')

    def counts(self):
        client = self.client_for(self.head)          # the login is not the page's cost
        out = []
        for query in self.VIEWS:
            with CaptureQueriesContext(connection) as ctx:
                self.assertEqual(client.get(URL + query).status_code, 200)
            out.append(len(ctx.captured_queries))
        return out

    def add(self, sites):
        """`sites` sites, each with a Pending M1 (amount set) and a legacy M2."""
        for _ in range(sites):
            site = self.site()
            self.milestone(site, 'M1', amount=D('100000'))
            self.legacy(site, 'M2', '700', self.today - timedelta(days=3))

    def test_23_off_27_on_at_3_sites_0_receipts_and_30_sites_40_receipts(self):
        with LIVE:
            self.add(3)
        with OFF:
            small_off = self.counts()
        with LIVE:
            small_on = self.counts()
            self.add(27)
            m1s = list(live_residential_milestones().filter(milestone_name='M1'))
            self.assertEqual(len(m1s), 30)
            for i in range(40):
                m = m1s[i % 30]
                if i % 2:
                    self.rec(m, '500', self.today)
                else:
                    self.confirmed(m, '1000', self.today)
            self.assertEqual(PaymentReceipt.objects.count(), 40)
            large_on = self.counts()
        with OFF:
            large_off = self.counts()
        print(f'\nFIN-M-B5-8 queries (main, check, list, fy+search): off 3/0={small_off} '
              f'30/40={large_off}; on 3/0={small_on} 30/40={large_on}')
        self.assertEqual(small_off, [23] * 4)
        self.assertEqual(large_off, [23] * 4)
        self.assertEqual(small_on, [27] * 4)
        self.assertEqual(large_on, [27] * 4)
