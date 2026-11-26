"""
FIN-M-B5-6 — money readers follow confirmed receipts (inert until the switch is on).

Pins, with CLIENT_RECEIPTS_LIVE on unless a test says otherwise:
  switch off  every changed reader draws what it drew at ce373d4 (no new marker, the old
              wording) and runs the same number of queries (OFF_COUNTS, measured at
              ce373d4 on this file's own fixture) — J1
  module      client_money: confirmed receipts newest first with their dates; recorded,
              queried and rejected money never counted; awaiting = recorded + queried;
              a legacy milestone shows its old record until a receipt is confirmed, and
              again after an undo (FR37); over the agreed amount (FR20); "+ n more" after
              five lines (Q3); owed never below 0; the CEO sum leaves out a legacy
              milestone with no old amount and counts it (Q2); the digest's receipts
              confirmed today, scoped (Q1) — each one query
  readers     cases (a)-(g) of the pre-flight on the card, the BD dashboard, the CEO
              figure, Finance work and the digest
  card        every mode shows the label; no "short"; the awaiting line; the old-record
              lines; no variance reason (Q6); R12's contract footer unchanged
  digest      confirmed today with a date received last week is in, with its date;
              confirmed yesterday is not
  task row    no status dropdown on a confirmation task with the switch on, on the row
              and on the task page (J7, Q11); the dropdown on every other task; no query
  Finance Head  its page identical with the switch on and off (J8)
  queries     each changed page a fixed number with the switch on (J10), stated in
              ON_EXTRA, not growing with more milestones or receipts

Run with:
    python manage.py test projects.tests_readers_follow_receipts --settings=solarpms.test_settings
"""
import datetime
import re
from decimal import Decimal

from django.db import connection
from django.http import QueryDict
from django.test import Client, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from .models import PaymentMilestone, PaymentReceipt, Project, ProjectPhase, Task
from .receipts import confirm_receipt, query_receipt, reject_receipt, unconfirm_receipt
from .tests_receipts import ReceiptBase, _profile

LIVE = override_settings(CLIENT_RECEIPTS_LIVE=True)
OFF = override_settings(CLIENT_RECEIPTS_LIVE=False)
D = Decimal
TOKEN = re.compile(r'[A-Za-z0-9]{64}')

#: Switch-off query counts of every changed page on CaseBase's fixture, measured by
#: running OffTests.test_switch_off_query_counts against the ce373d4 tree (J1).
OFF_COUNTS = {
    'overview_finance': 39, 'overview_manager': 44, 'overview_bd': 39, 'overview_read': 39,
    'bd_dashboard': 10, 'ceo_residential': 14, 'finance_work': 13, 'task_detail': 21,
    'digest_metrics': 5,
}

#: The fixed number each page adds with the switch on, against OFF_COUNTS (J10): one
#: receipt read for the card and the BD dashboard; Finance work's rows read the cache
#: (no query, ruling Q9) and its 1 is FIN-M-B5-4a's client-receipt sections, already
#: there at ce373d4; the CEO sum and the digest swap one query for another; the task row
#: and task page read nothing.
ON_EXTRA = {
    'overview_finance': 1, 'overview_manager': 1, 'overview_bd': 1, 'overview_read': 1,
    'bd_dashboard': 1, 'ceo_residential': 0, 'finance_work': 1, 'task_detail': 0,
    'digest_metrics': 0,
}

#: Markers only the switch-on branches draw.
NEW_MARKERS = ('data-card-money', 'data-follows-receipts', 'data-label', 'data-confirmed',
               'data-over', 'data-older-no-amount', 'awaiting Finance confirmation',
               'old record', 'Part received', 'Amount needed', 'Received before receipts')


def day(n):
    """n days before today (IST), so a receipt date is never in the future."""
    return timezone.localdate() - datetime.timedelta(days=n)


def shown(d):
    """A date as the card prints it ('d M Y')."""
    return f'{d:%d %b %Y}'


def _client(profile):
    c = Client(SERVER_NAME='localhost')
    c.force_login(profile.user)
    return c


def _cm():
    """client_money, imported per call: OffTests also runs against the ce373d4 tree, which
    has no such module, to measure OFF_COUNTS."""
    from . import client_money
    return client_money


class CaseBase(ReceiptBase):
    """ReceiptBase plus the pre-flight's cases (a)-(g) on four live Residential sites,
    built through the real writers with the switch on, so the milestones' cached figures
    are exactly what recompute leaves:

      A.M1 (a)  Pending, no agreed amount, ₹25,000 confirmed
      A.M2 (b)  agreed ₹1,00,000: ₹40,000 confirmed + ₹30,000 recorded
      B.M1 (c)  agreed ₹1,00,000: ₹60,000 received 40 days ago + ₹40,000 received 3 days
                ago, both confirmed -> Received
      B.M2 (d)  Invoiced at ₹1,00,000, ₹40,000 confirmed
      C.M1 (e1) legacy, old amount ₹53,500 (agreed ₹39,198), no receipt
      C.M2 (e2) legacy, no old amount (agreed ₹50,000), no receipt
      C.M3 (f)  legacy, old ₹53,500 (agreed ₹39,198), then ₹20,000 confirmed
      D.M1 (g)  agreed ₹1,00,000, ₹1,20,000 confirmed
      D.M2 (g') no agreed amount, Invoiced at ₹1,00,000, ₹1,20,000 confirmed
    """

    def setUp(self):
        super().setUp()
        with LIVE:
            self.build()

    def build(self):
        now = timezone.now()
        self.sa = self.site(amounts=(None, '100000', None), name='Case A')
        self.sb = self.site(amounts=('100000', '100000', None), name='Case B')
        self.sc = self.site(amounts=('39198', '50000', '39198'), name='Case C')
        self.sd = self.site(amounts=('100000', None, None), name='Case D')
        Project.objects.filter(pk__in=[p.pk for p in (self.sa, self.sb, self.sc, self.sd)]
                               ).update(activated_at=now - datetime.timedelta(days=5))
        self.a, self.b, _ = self.milestones(self.sa)
        self.c, self.d, _ = self.milestones(self.sb)
        self.e1, self.e2, self.f = self.milestones(self.sc)
        self.g, self.g2, _ = self.milestones(self.sd)

        self.ra = self.confirmed(self.a, '25000', day(4))
        self.rb = self.confirmed(self.b, '40000', day(6))
        self.rb_wait = self.receipt(self.pm, self.b, amount='30000', date=day(2))
        self.rc_old = self.confirmed(self.c, '60000', day(40))
        self.rc_new = self.confirmed(self.c, '40000', day(3))
        self.invoice(self.d, '100000')
        self.rd = self.confirmed(self.d, '40000', day(5))
        self.legacy(self.e1, '53500', day(90))
        self.legacy(self.e2, None, day(80))
        self.legacy(self.f, '53500', day(70))
        self.rf = self.confirmed(self.f, '20000', day(2))
        self.rg = self.confirmed(self.g, '120000', day(1))
        self.invoice(self.g2, '100000')
        self.rg2 = self.confirmed(self.g2, '120000', day(1))
        for m in ('a', 'b', 'c', 'd', 'e1', 'e2', 'f', 'g', 'g2'):
            setattr(self, m, PaymentMilestone.objects.get(pk=getattr(self, m).pk))

    # -- builders --------------------------------------------------------------

    def confirmed(self, milestone, amount, date):
        """Recorded by the PM, confirmed by Finance B (with a note, used only when over)."""
        r = self.receipt(self.pm, milestone, amount=amount, date=date)
        confirm_receipt(self.fin_b.user, r, overpayment_note='client paid extra')
        return self.fresh(r)

    def invoice(self, milestone, amount):
        PaymentMilestone.objects.filter(pk=milestone.pk).update(
            status=PaymentMilestone.INVOICED, invoice_date=day(10),
            invoice_amount=D(amount), invoice_number=f'INV-{milestone.pk}')

    def legacy(self, milestone, old, old_date):
        """What mark_legacy_received_milestones leaves on a Received milestone."""
        old = D(old) if old is not None else None
        PaymentMilestone.objects.filter(pk=milestone.pk).update(
            status=PaymentMilestone.RECEIVED, amount_received=old, received_date=old_date,
            received_before_receipts=True, legacy_amount_received=old,
            legacy_received_date=old_date, legacy_marked_at=timezone.now())

    # -- readers ---------------------------------------------------------------

    def overview(self, project, who):
        return _client(who).get(reverse('project_overview', args=[project.project_id]))

    def card(self, project, who):
        """The milestone card's HTML alone, from its id to the next column."""
        page = self.overview(project, who).content.decode()
        start = page.index('id="payment-milestones"')
        return page[start:page.index('class="col-12 col-md-4"', start)]

    def count(self, fn):
        with CaptureQueriesContext(connection) as ctx:
            fn()
        return len(ctx.captured_queries)

    def page_counts(self):
        """{page: queries} for every changed page, each GET warmed once first."""
        from .management.commands.send_eod_digest import Command
        task = self.conf_task()
        pages = {
            'overview_finance': (self.fin_a, reverse('project_overview', args=[self.sb.project_id])),
            'overview_manager': (self.pm, reverse('project_overview', args=[self.sb.project_id])),
            'overview_bd': (self.bd, reverse('project_overview', args=[self.sb.project_id])),
            'overview_read': (self.ceo, reverse('project_overview', args=[self.sb.project_id])),
            'bd_dashboard': (self.bd, reverse('dashboard_bd')),
            'ceo_residential': (self.ceo, reverse('dashboard_ceo') + '?context=residential'),
            'finance_work': (self.fin_a, reverse('finance_work')),
            'task_detail': (self.fin_a, reverse('task_detail', args=[self.sb.project_id, task.pk])),
        }
        counts = {}
        for key, (who, url) in pages.items():
            c = _client(who)
            self.assertEqual(c.get(url).status_code, 200, key)
            counts[key] = self.count(lambda: c.get(url))
        counts['digest_metrics'] = self.count(
            lambda: Command()._ceo_extra_metrics(timezone.localdate()))
        return counts

    def conf_task(self):
        """Site B's M1 confirmation task, held by Finance A (created once)."""
        task = Task.objects.filter(phase__project=self.sb,
                                   task_name='Advance Payment Confirmation').first()
        if task is None:
            phase = ProjectPhase.objects.create(project=self.sb, phase_name='Finance',
                                                phase_order=1)
            task = Task.objects.create(phase=phase, task_name='Advance Payment Confirmation',
                                       task_order=1, assigned_role=Task.FINANCE,
                                       assigned_to=self.fin_a, is_payment_milestone=True)
            Task.objects.create(phase=phase, task_name='Site Survey', task_order=2,
                                assigned_role=Task.FINANCE, assigned_to=self.fin_a)
        return task


# ===========================================================================
# 1. Switch off: as at ce373d4 (J1)
# ===========================================================================

@OFF
class OffTests(CaseBase):

    def test_switch_off_query_counts(self):
        self.maxDiff = None
        self.assertEqual(self.page_counts(), OFF_COUNTS)

    def test_no_reader_draws_a_new_marker(self):
        self.conf_task()
        pages = [self.overview(p, who).content.decode()
                 for p in (self.sa, self.sb, self.sc, self.sd)
                 for who in (self.fin_a, self.pm, self.bd, self.ceo)]
        pages += [_client(self.bd).get(reverse('dashboard_bd')).content.decode(),
                  _client(self.ceo).get(reverse('dashboard_ceo') + '?context=residential'
                                        ).content.decode(),
                  _client(self.fin_a).get(reverse('finance_work')).content.decode(),
                  _client(self.fin_a).get(reverse('task_detail', args=[
                      self.sb.project_id, self.conf_task().pk])).content.decode()]
        for page in pages:
            for marker in NEW_MARKERS:
                self.assertNotIn(marker, page)

    def test_the_card_keeps_its_old_wording(self):
        card = self.card(self.sa, self.fin_a)
        self.assertIn('Received: ₹40000', card)
        self.assertIn('₹60000 short', card)
        self.assertIn('>Pending</span>', card)

    def test_the_digest_keeps_its_old_count(self):
        from .management.commands.send_eod_digest import Command
        metrics = Command()._ceo_extra_metrics(timezone.localdate())
        self.assertNotIn('paid_list', metrics)
        self.assertEqual(metrics['paid_today'], PaymentMilestone.objects.filter(
            received_date=timezone.localdate()).count())

    def test_the_ceo_figure_keeps_its_old_rule(self):
        from .views import _get_ceo_dashboard_context
        ctx = _get_ceo_dashboard_context('residential')
        # d: 60,000; e2: the whole 50,000 (blank received); f: 39,198 - 20,000.
        self.assertEqual(ctx['fin_client_payment_pending'], D('129198.00'))
        self.assertEqual(ctx.get('fin_client_payment_older', 0), 0)   # absent at ce373d4

    def test_the_status_dropdown_stays_on_a_confirmation_task(self):
        task = self.conf_task()
        page = self.overview(self.sb, self.fin_a).content.decode()
        self.assertIn('<select name="status"', _row(page, task))
        detail = _client(self.fin_a).get(reverse('task_detail', args=[
            self.sb.project_id, task.pk])).content.decode()
        self.assertIn('<select name="status"', detail)


def _row(page, task):
    """One task row's HTML from the project page."""
    start = page.index(f'id="task-row-{task.pk}"')
    return page[start:page.index('</tr>', start)]


# ===========================================================================
# 2. The read module (J2)
# ===========================================================================

@LIVE
class ModuleTests(CaseBase):

    def money(self, *milestones):
        return _cm().milestone_money(milestones)

    def test_confirmed_receipts_newest_first_with_their_dates(self):
        item = self.money(self.c)[self.c.pk]
        self.assertEqual(item.confirmed, D('100000.00'))
        self.assertEqual(item.lines, [{'amount': '₹40,000', 'date': day(3)},
                                      {'amount': '₹60,000', 'date': day(40)}])
        self.assertEqual((item.label, item.more, item.awaiting), ('Received', 0, D('0')))

    def test_recorded_queried_and_rejected_are_never_confirmed(self):
        queried = self.receipt(self.pm, self.b, amount='5000', date=day(1))
        query_receipt(self.fin_a.user, queried, 'which account?')
        rejected = self.receipt(self.pm, self.b, amount='7000', date=day(1))
        reject_receipt(self.fin_a.user, rejected, 'not ours')
        item = self.money(self.b)[self.b.pk]
        self.assertEqual(item.confirmed, D('40000.00'))
        # Awaiting = recorded (30,000) + queried (5,000); the rejected 7,000 counts nowhere.
        self.assertEqual(item.awaiting, D('35000.00'))
        self.assertEqual(item.awaiting_text, '₹35,000')
        self.assertEqual(item.label, 'Part received ₹40,000 of ₹1,00,000')

    def test_legacy_shows_its_old_record_until_a_receipt_is_confirmed(self):
        money = self.money(self.e1, self.e2, self.f)
        self.assertEqual((money[self.e1.pk].old_record, money[self.e1.pk].old_amount,
                          money[self.e1.pk].label),
                         (True, '₹53,500', 'Received before receipts'))
        self.assertEqual((money[self.e2.pk].old_record, money[self.e2.pk].old_amount),
                         (True, ''))
        f = money[self.f.pk]
        self.assertEqual((f.old_record, f.confirmed, f.label), (False, D('20000.00'), 'Received'))
        self.assertEqual(f.lines, [{'amount': '₹20,000', 'date': day(2)}])

    def test_an_undo_removes_the_money_and_brings_the_old_record_back(self):
        unconfirm_receipt(self.fin_a.user, self.rf, 'bounced')
        f = self.money(PaymentMilestone.objects.get(pk=self.f.pk))[self.f.pk]
        self.assertEqual((f.old_record, f.old_amount, f.confirmed, f.awaiting),
                         (True, '₹53,500', D('0'), D('20000.00')))
        unconfirm_receipt(self.fin_a.user, self.ra, 'bounced')
        a = self.money(PaymentMilestone.objects.get(pk=self.a.pk))[self.a.pk]
        self.assertEqual((a.confirmed, a.lines, a.label), (D('0'), [], 'Amount needed'))

    def test_overpayment(self):
        money = self.money(self.g, self.e1)
        self.assertEqual((money[self.g.pk].over_agreed, money[self.g.pk].over_agreed_old),
                         ('₹20,000', ''))
        self.assertEqual(money[self.e1.pk].over_agreed, '')

    def test_an_old_record_above_the_agreed_amount_shows_its_excess(self):
        # Ruling 25 Nov: 013 M3 before its receipt — old ₹53,500 against agreed ₹39,198.
        # e1 is exactly that; f is the same milestone once ₹20,000 was confirmed.
        money = self.money(self.e1, self.e2, self.f)
        self.assertEqual(money[self.e1.pk].over_agreed_old, '₹14,302')
        self.assertEqual(money[self.e2.pk].over_agreed_old, '')   # no old amount
        self.assertEqual(money[self.f.pk].over_agreed_old, '')    # replaced by a receipt
        unconfirm_receipt(self.fin_a.user, self.rf, 'bounced')
        f = self.money(PaymentMilestone.objects.get(pk=self.f.pk))[self.f.pk]
        self.assertEqual(f.over_agreed_old, '₹14,302')             # the old record is back

    def test_more_than_five_receipts_say_how_many_more(self):
        for n in range(6):
            self.confirmed(self.e2, str(1000 + n), day(10 + n))
        item = self.money(PaymentMilestone.objects.get(pk=self.e2.pk))[self.e2.pk]
        self.assertEqual((len(item.lines), item.more), (5, 1))
        self.assertEqual(item.lines[0], {'amount': '₹1,000', 'date': day(10)})

    def test_one_query_for_any_number_of_milestones(self):
        everything = list(PaymentMilestone.objects.all())
        self.assertEqual(self.count(lambda: _cm().milestone_money(everything)), 1)
        self.assertEqual(self.count(lambda: _cm().milestone_money([])), 0)

    def test_the_cache_read_agrees_with_the_receipts_where_finance_work_uses_it(self):
        # Ruling Q9: Finance work's rows (Pending / Invoiced) read the cache, no query.
        for m in (self.a, self.b, self.d, self.g2):
            with self.subTest(milestone=m.pk):
                fast, full = _cm().unreceived_money(m), self.money(m)[m.pk]
                self.assertEqual((fast.label, fast.confirmed), (full.label, full.confirmed))
        self.assertEqual(self.count(lambda: _cm().unreceived_money(self.a)), 0)

    def test_owed_is_never_negative(self):
        owed = _cm().owed
        self.assertEqual(owed(D('100000'), D('120000')), (D('0'), D('20000')))
        self.assertEqual(owed(D('100000'), D('40000')), (D('60000'), None))
        self.assertEqual(owed(D('100000'), D('100000')), (D('0'), None))
        self.assertEqual(owed(None, D('5')), (None, None))

    def test_client_payment_owed_leaves_out_the_legacy_with_no_amount(self):
        scope = PaymentMilestone.objects.filter(
            project__in=[self.sa, self.sb, self.sc, self.sd],
            status__in=['Invoiced', 'Received'], amount__isnull=False)
        self.assertEqual(_cm().client_payment_owed(scope), (D('79198.00'), 1))
        self.assertEqual(self.count(lambda: _cm().client_payment_owed(scope)), 1)
        # Once a receipt is confirmed on it, it is an ordinary owed milestone again.
        self.confirmed(self.e2, '10000', day(1))
        self.assertEqual(_cm().client_payment_owed(scope), (D('119198.00'), 0))

    def test_receipts_confirmed_on_follow_the_callers_scope(self):
        scope = {'project_type': 'Residential', 'is_deleted': False, 'is_test': False}
        today = timezone.localdate()
        everything = PaymentReceipt.objects.filter(status=PaymentReceipt.CONFIRMED)
        self.assertEqual(len(_cm().receipts_confirmed_on(today, scope)), everything.count())
        test_site = self.site(name='Test', is_test=True)
        r = self.confirmed(self.milestones(test_site)[0], '1000', day(1))
        deleted = self.site(name='Gone')
        r2 = self.confirmed(self.milestones(deleted)[0], '1000', day(1))
        Project.objects.filter(pk=deleted.pk).update(is_deleted=True)
        got = {x.pk for x in _cm().receipts_confirmed_on(today, scope)}
        self.assertNotIn(r.pk, got)
        self.assertNotIn(r2.pk, got)
        self.assertIn(r.pk, {x.pk for x in _cm().receipts_confirmed_on(
            today, {'project_type': 'Residential'})})
        self.assertEqual(self.count(lambda: _cm().receipts_confirmed_on(today, scope)), 1)


# ===========================================================================
# 3. The milestone card (J4, rulings Q3-Q7)
# ===========================================================================

@LIVE
class CardTests(CaseBase):

    MODES = ('fin_a', 'pm', 'bd', 'ceo')   # finance, manager, bd, read

    def cards(self, project):
        return {who: self.card(project, getattr(self, who)) for who in self.MODES}

    def test_every_mode_shows_the_label_and_never_short(self):
        expected = {
            self.sa: ['Amount needed — ₹25,000 confirmed', 'Part received ₹40,000 of ₹1,00,000'],
            self.sb: ['>Received</span>', 'Part received ₹40,000 of ₹1,00,000'],
            self.sc: ['Received before receipts'],
            self.sd: ['>Received</span>'],
        }
        for project, labels in expected.items():
            for who, card in self.cards(project).items():
                with self.subTest(site=project.customer_name, mode=who):
                    for label in labels:
                        self.assertIn(label, card)
                    self.assertNotIn('short</span>', card)
                    self.assertNotIn('excess</span>', card)
                    self.assertNotIn('Received: ₹', card)

    def test_the_header_chip_carries_the_label(self):
        card = self.card(self.sb, self.fin_a)
        self.assertIn(f'title="M2 — Part received ₹40,000 of ₹1,00,000 (INV-{self.d.pk})"', card)
        self.assertIn('title="M1 — Received"', card)

    def test_confirmed_lines_with_dates_and_the_awaiting_line(self):
        a_card = self.card(self.sa, self.pm)
        self.assertIn(f'₹25,000 received {shown(day(4))}', a_card)
        self.assertIn(f'₹40,000 received {shown(day(6))}', a_card)
        self.assertIn('₹30,000 awaiting Finance confirmation', a_card)
        b_card = self.card(self.sb, self.pm)
        self.assertLess(b_card.index(f'₹40,000 received {shown(day(3))}'),
                        b_card.index(f'₹60,000 received {shown(day(40))}'))

    def test_old_record_lines(self):
        for who, card in self.cards(self.sc).items():
            with self.subTest(mode=who):
                self.assertIn('₹53,500 received — old record', card)
                self.assertIn('₹14,302 over agreed amount (old record)', card)
                self.assertIn('Amount not recorded — old record', card)
                self.assertIn(f'₹20,000 received {shown(day(2))}', card)
                self.assertEqual(card.count('old record'), 3)   # e1 twice, e2 once; f replaced

    def test_overpayment_line(self):
        self.assertIn('₹20,000 over agreed amount', self.card(self.sd, self.bd))

    def test_no_variance_reason(self):
        PaymentMilestone.objects.filter(pk=self.e1.pk).update(variance_reason='paid in two parts')
        for who, card in self.cards(self.sc).items():
            self.assertNotIn('paid in two parts', card)

    def test_more_than_five_receipts_link_for_whoever_may_record(self):
        for n in range(6):
            self.confirmed(self.e2, str(1000 + n), day(10 + n))
        link = reverse('receipt_record', args=[self.sc.project_id, self.e2.pk])
        self.assertIn(f'<a href="{link}">+ 1 more</a>', self.card(self.sc, self.fin_a))
        read = self.card(self.sc, self.ceo)
        self.assertIn('+ 1 more', read)
        self.assertNotIn(link, read)

    def test_r12_contract_footer_unchanged(self):
        Project.objects.filter(pk=self.sb.pk).update(contract_value=D('200000'))
        for who in ('fin_a', 'ceo'):
            self.assertIn('contract-value-footer', self.card(self.sb, getattr(self, who)))
        for who in ('pm', 'bd'):
            self.assertNotIn('contract-value-footer', self.card(self.sb, getattr(self, who)))


# ===========================================================================
# 4. BD dashboard, CEO figure, Finance work (Q2, Q8, Q9)
# ===========================================================================

@LIVE
class BdDashboardTests(CaseBase):

    def test_badges_show_the_label_counts_stay_status_based(self):
        page = _client(self.bd).get(reverse('dashboard_bd')).content.decode()
        for label in ('Amount needed — ₹25,000 confirmed', 'Part received ₹40,000 of ₹1,00,000',
                      'Received before receipts'):
            self.assertIn(label, page)
        self.assertNotIn('short', page.split('Payment Milestones', 1)[1].split('View Project')[0])
        # Status-based (J3): site B has M1 Received of 3, and M2 Invoiced awaiting payment.
        self.assertIn('1/3 milestones', page)
        self.assertIn('1 milestone awaiting payment receipt', page)


@LIVE
class CeoFigureTests(CaseBase):

    def test_older_milestones_with_no_amount_leave_the_sum(self):
        from .views import _get_ceo_dashboard_context
        ctx = _get_ceo_dashboard_context('residential')
        # d 60,000 + f 19,198 (FR37, ruling Q2); e2 counted apart; part-paid b out (J3).
        self.assertEqual((ctx['fin_client_payment_pending'], ctx['fin_client_payment_older']),
                         (D('79198.00'), 1))
        page = _client(self.ceo).get(reverse('dashboard_ceo') + '?context=residential')
        self.assertContains(page, '₹79198')
        self.assertContains(page, 'plus 1 older milestone with no amount recorded')

    def test_test_and_deleted_sites_stay_out(self):
        from .views import _get_ceo_dashboard_context
        for flag in ('is_test', 'is_deleted'):
            site = self.site(amounts=('50000',), name=flag)
            m = self.milestones(site)[0]
            self.legacy(m, None, day(5))
            Project.objects.filter(pk=site.pk).update(**{flag: True})
        ctx = _get_ceo_dashboard_context('residential')
        self.assertEqual((ctx['fin_client_payment_pending'], ctx['fin_client_payment_older']),
                         (D('79198.00'), 1))


@LIVE
class FinanceWorkTests(CaseBase):

    def rows(self):
        from .views_finance_work import _clean_params, _milestone_rows
        to_act, invoiced, _ = _milestone_rows(self.fin_a.user, timezone.localdate(),
                                              _clean_params(QueryDict('')), receipts_on=True)
        return {r.milestone.pk: r for r in to_act + invoiced}

    def test_rows_follow_receipts(self):
        rows = self.rows()
        a, b, d, g2 = rows[self.a.pk], rows[self.b.pk], rows[self.d.pk], rows[self.g2.pk]
        self.assertEqual((a.label, a.confirmed_so_far),
                         ('Amount needed — ₹25,000 confirmed', ''))
        self.assertEqual((b.confirmed_so_far, b.label), ('₹40,000', ''))
        self.assertEqual((d.due, d.received, d.confirmed_so_far, d.over_invoice),
                         ('₹60,000', '₹40,000', '₹40,000', ''))
        self.assertEqual((g2.due, g2.due_value, g2.over_invoice), ('₹0', D('0'), '₹20,000'))
        for m in (self.c, self.e1, self.e2, self.f, self.g):    # Received: not listed
            self.assertNotIn(m.pk, rows)

    def test_figures_are_whole_rupees_as_the_rest_of_the_page(self):
        # Ruling 25 Nov: the page's own style (rupees), not format_rupees' paise.
        self.confirmed(self.a, '0.40', day(1))
        self.confirmed(self.b, '0.60', day(1))
        rows = self.rows()
        self.assertEqual(rows[self.a.pk].label, 'Amount needed — ₹25,000 confirmed')
        self.assertEqual(rows[self.b.pk].confirmed_so_far, '₹40,001')
        PaymentMilestone.objects.filter(pk=self.g2.pk).update(invoice_amount=D('99999.50'))
        self.assertEqual(self.rows()[self.g2.pk].over_invoice, '₹20,001')

    def test_the_page_draws_them(self):
        page = _client(self.fin_a).get(reverse('finance_work')).content.decode()
        self.assertIn('data-label>Amount needed — ₹25,000 confirmed</span>', page)
        self.assertIn('<span class="amt" data-confirmed>₹40,000</span> confirmed so far', page)
        self.assertIn('₹20,000 over invoice', page)
        self.assertNotIn('−₹20,000', page)
        self.assertIn('₹60,000 due', page)   # the tile: 60,000 + 0, never 60,000 - 20,000


# ===========================================================================
# 5. The EOD digest (J5, ruling Q1)
# ===========================================================================

@LIVE
class DigestTests(CaseBase):

    def metrics(self):
        from .management.commands.send_eod_digest import Command
        return Command()._ceo_extra_metrics(timezone.localdate())

    def test_confirmed_today_is_in_with_its_date_yesterday_is_not(self):
        PaymentReceipt.objects.filter(status=PaymentReceipt.CONFIRMED).exclude(
            pk=self.ra.pk).update(confirmed_at=timezone.now() - datetime.timedelta(days=1))
        week_ago = day(7)
        PaymentReceipt.objects.filter(pk=self.ra.pk).update(date_received=week_ago)
        m = self.metrics()
        self.assertEqual(m['paid_today'], 1)
        self.assertEqual(m['paid_list'], [{
            'site': self.sa.site_code or self.sa.project_id, 'milestone': 'M1',
            'amount': '₹25,000', 'received': f'{week_ago.day} {week_ago:%b}'}])

    def test_no_date_when_received_today_and_part_payments_count(self):
        PaymentReceipt.objects.filter(pk=self.rb.pk).update(date_received=timezone.localdate())
        site_a = self.sa.site_code or self.sa.project_id
        rows = {(r['site'], r['milestone']): r for r in self.metrics()['paid_list']}
        self.assertEqual(rows[(site_a, 'M2')], {'site': site_a, 'milestone': 'M2',
                                                'amount': '₹40,000', 'received': ''})  # (b): part payment
        self.assertEqual(self.metrics()['paid_today'],
                         PaymentReceipt.objects.filter(status=PaymentReceipt.CONFIRMED).count())

    def test_the_email_shows_the_list(self):
        from django.template.loader import render_to_string
        m = self.metrics()
        ctx = {'ceo': m, 'show_ceo_sections': True, 'metrics': {}, 'user_status': {}}
        text = render_to_string('projects/email/eod_digest.txt', ctx)
        self.assertIn(f'{self.sa.site_code or self.sa.project_id} · M1 · ₹25,000', text)
        html = render_to_string('projects/email/eod_digest.html', ctx)
        self.assertIn('M1 &middot; ₹25,000', html)


# ===========================================================================
# 6. Confirmation-task rows (J7, ruling Q11)
# ===========================================================================

@LIVE
class TaskRowTests(CaseBase):

    def test_no_dropdown_on_a_confirmation_task_dropdown_elsewhere(self):
        task = self.conf_task()
        other = Task.objects.get(phase__project=self.sb, task_name='Site Survey')
        for who in (self.fin_a, self.pm):
            with self.subTest(who=who.role):
                page = self.overview(self.sb, who).content.decode()
                row = _row(page, task)
                self.assertNotIn('<select name="status"', row)
                self.assertIn('Follows Finance confirmation', row)
                self.assertIn('>Not Started</span>', row)
                self.assertIn('<select name="status"', _row(page, other))

    def test_the_task_page_has_no_dropdown(self):
        task = self.conf_task()
        detail = _client(self.fin_a).get(reverse('task_detail', args=[
            self.sb.project_id, task.pk])).content.decode()
        self.assertNotIn('<select name="status"', detail)
        self.assertIn('Follows Finance confirmation', detail)

    def test_an_htmx_redraw_has_no_dropdown_either(self):
        task = self.conf_task()
        r = _client(self.fin_a).post(
            reverse('task_status_update', args=[self.sb.project_id, task.pk]),
            {'status': Task.DONE}, HTTP_HX_REQUEST='true')
        self.assertEqual(r.status_code, 200)
        self.assertNotIn('<select name="status"', _row(r.content.decode(), task))

    def test_not_on_a_tender_site(self):
        task = self.conf_task()
        Project.objects.filter(pk=self.sb.pk).update(project_type='OPEX')
        from .templatetags.receipt_tags import follows_receipts
        self.assertFalse(follows_receipts(task, Project.objects.get(pk=self.sb.pk)))
        self.assertFalse(follows_receipts(task, None))

    def test_the_row_and_the_task_page_add_no_query(self):
        task = self.conf_task()
        url = reverse('task_detail', args=[self.sb.project_id, task.pk])
        c = _client(self.fin_a)
        c.get(url)
        on = self.count(lambda: c.get(url))
        with OFF:
            off = self.count(lambda: c.get(url))
        self.assertEqual(on, off)


# ===========================================================================
# 7. Finance Head untouched (J8) and query counts (J10)
# ===========================================================================

class FinanceHeadTests(CaseBase):

    def test_with_the_switch_off_receipts_change_nothing_on_the_finance_head_page(self):
        # Narrowed in FIN-M-B5-8 (Zuber, 26 Nov): B5-6 pinned on == off because the page
        # was outside B5-6; B5-8 makes the switch-on page follow receipts (its tests are in
        # tests_finance_head_receipts). What still holds here is K1: with the switch OFF,
        # neither the page nor finance_head_summary() reads a receipt, so taking every
        # receipt away changes neither.
        from .finance_head import finance_head_summary
        from .models import PaymentReceiptQuery
        c = _client(self.ceo)
        url = reverse('finance_head')
        with OFF:
            before = TOKEN.sub('T', c.get(url).content.decode())
            s_before = finance_head_summary()
        self.assertTrue(PaymentReceipt.objects.exists())
        PaymentReceiptQuery.objects.all().delete()
        PaymentReceipt.objects.all().delete()
        with OFF:
            after = TOKEN.sub('T', c.get(url).content.decode())
            s_after = finance_head_summary()
        self.assertEqual(before, after)
        self.assertEqual(s_before, s_after)


class QueryCountTests(CaseBase):

    def test_switch_on_adds_a_fixed_number_per_page(self):
        with OFF:
            off = self.page_counts()
        with LIVE:
            on = self.page_counts()
        self.assertEqual({k: on[k] - off[k] for k in on}, ON_EXTRA)

    def test_switch_on_counts_do_not_grow(self):
        with LIVE:
            before = self.page_counts()
            more = self.site(amounts=('70000', '80000', None), name='Case E')
            Project.objects.filter(pk=more.pk).update(activated_at=timezone.now())
            m1, m2, _ = self.milestones(more)
            for n in range(3):
                self.confirmed(m1, str(1000 + n), day(n + 1))
            self.receipt(self.pm, m2, amount='500', date=day(1))
            for n in range(4):
                self.confirmed(self.b, str(100 + n), day(n + 1))
            after = self.page_counts()
        self.assertEqual(after, before)
