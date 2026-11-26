"""
FIN-M-B5-6 — THE ONE READ MODULE FOR CLIENT MONEY (ruling J2): what a page shows of the
money a client has paid on a payment milestone, once CLIENT_RECEIPTS_LIVE is on.

WHY A MODULE, NOT THE MILESTONE'S OWN COLUMNS. PaymentMilestone.status, amount_received
and received_date are a CACHE kept in step by receipts.recompute_milestone_from_receipts()
(FIN-M-B5-5a, H1). The cache cannot answer every question a page asks:
  * a legacy milestone (received_before_receipts, FR37) with no confirmed receipt carries
    its OLD amount in amount_received, so "confirmed money" read from the cache would be
    the old figure and the label would say "Received" instead of "Received before
    receipts";
  * money waiting for Finance (recorded + queried, FR2) is not in the cache at all;
  * the cache keeps one date (the latest date received, and only once Received), where
    the card shows every confirmed receipt with its own date received (FR13).
So the readers changed in FIN-M-B5-6 read the receipts here. No reader sums receipts
itself, and no reader in this module writes anything.

ONLY WHILE THE SWITCH IS ON. Every caller asks receipts_live() first and calls nothing
here while it is off (ruling J1: every page exactly as before, query counts included).

QUERIES. Set-based, a fixed number per call whatever the number of milestones or
receipts (J10): milestone_money() and receipts_confirmed_on() are one query each,
client_payment_owed() is one aggregate, money_in() two, unreceived_money() and owed()
none. Never a query per row.

SCOPE (J9) belongs to the caller: each function takes the milestones, the queryset or the
project filter the calling page already uses; nothing here narrows or widens it.

money_in() (FIN-M-B5-8, K2) is the Finance Head page's period and weekly money in: the
only reader that dates money, so the only one that reads every confirmed receipt's own
date received (FR13).

KNOWN DIFFERENCE (FIN-M-B5-8 ruling Q1): a "straggler" — Received through an old path,
not marked received_before_receipts, no receipt row (FR40(1)) — counts at its cached
amount_received in money_in() and client_payment_owed(), but milestone_money() gives it
₹0 confirmed, so the milestone card shows it with no money line.
TODO: make the card show a straggler's amount_received (follow-up, ruled out of FIN-M-B5-8;
expected 0 stragglers after the marking command runs).
"""
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

from django.db.models import (Case, Count, DateField, DecimalField, Exists, F, OuterRef, Q,
                              Sum, Value, When)
from django.db.models.functions import Coalesce

from .models import PaymentMilestone, PaymentReceipt
from .money_format import format_rupees
from .receipts import (LIVE_STATUSES, UNCONFIRMED_STATUSES, ReceiptTotals,
                       milestone_receipt_label)

ZERO = Decimal('0.00')

#: Ruling Q3: the card lists at most this many confirmed receipts per milestone, newest
#: first; the rest are "+ n more".
RECEIPT_LINES = 5


@dataclass
class MilestoneMoney:
    """One milestone's client money, as the pages show it (milestone_money)."""
    label: str                        # receipts.milestone_receipt_label (FR40(9))
    confirmed: Decimal = ZERO         # Finance-confirmed money (FR2)
    awaiting: Decimal = ZERO          # recorded + queried: shown apart, never counted (FR2)
    # The newest RECEIPT_LINES confirmed receipts: {'amount': '₹60,000', 'date': date}.
    lines: list = field(default_factory=list)
    more: int = 0                     # confirmed receipts beyond those lines (ruling Q3)
    old_record: bool = False          # FR37: legacy and no confirmed receipt yet
    old_amount: str = ''              # its old amount, formatted; '' when none was recorded
    over_agreed: str = ''             # confirmed money beyond the agreed amount (FR20, Q4)
    over_agreed_old: str = ''         # an old record's old amount beyond the agreed amount (ruling 25 Nov)

    @property
    def confirmed_text(self):
        return format_rupees(self.confirmed)

    @property
    def awaiting_text(self):
        return format_rupees(self.awaiting) if self.awaiting else ''


def milestone_money(milestones):
    """{milestone pk: MilestoneMoney} for every milestone in `milestones` — LOADED
    instances (the label reads status, amount and the legacy fields from them), already
    scoped by the caller. ONE query for any number of milestones and receipts, none when
    the list is empty.

    The query reads every receipt that is not rejected (rejected money counts nowhere,
    FR19), newest date received first; the sums, the receipt lines and the legacy state
    are worked out here in Python from those rows, so the card's lines and its totals
    can never disagree."""
    milestones = list(milestones)
    rows = []
    if milestones:
        # What it fetches: each live receipt's milestone, status, amount and date
        # received — values(), not instances, because no page here opens a receipt.
        # Ordered newest date received first (ruling Q3), pk as the tie-break so two
        # receipts on one day keep a stable order.
        rows = (PaymentReceipt.objects
                .filter(milestone_id__in=[m.pk for m in milestones])
                .exclude(status=PaymentReceipt.REJECTED)
                .values('milestone_id', 'status', 'amount', 'date_received')
                .order_by('-date_received', '-pk'))
    by_milestone = {m.pk: [] for m in milestones}
    for row in rows:
        by_milestone[row['milestone_id']].append(row)

    money = {}
    for m in milestones:
        receipts = by_milestone[m.pk]
        confirmed_rows = [r for r in receipts if r['status'] == PaymentReceipt.CONFIRMED]
        confirmed = sum((r['amount'] for r in confirmed_rows), ZERO)
        awaiting = sum((r['amount'] for r in receipts if r['status'] in UNCONFIRMED_STATUSES),
                       ZERO)
        totals = ReceiptTotals(
            confirmed=confirmed, unconfirmed=awaiting,
            amount_needed=sum(1 for r in receipts
                              if r['status'] == PaymentReceipt.AMOUNT_NEEDED))
        item = MilestoneMoney(
            label=milestone_receipt_label(m, totals), confirmed=confirmed, awaiting=awaiting,
            lines=[{'amount': format_rupees(r['amount']), 'date': r['date_received']}
                   for r in confirmed_rows[:RECEIPT_LINES]],
            more=max(len(confirmed_rows) - RECEIPT_LINES, 0))
        if m.received_before_receipts and not confirmed_rows:
            # FR37: the old figures stand until any confirmed receipt exists. Ruling Q5
            # words the case where no old amount was ever typed.
            item.old_record = True
            if m.legacy_amount_received is not None:
                item.old_amount = format_rupees(m.legacy_amount_received)
                # Ruling 25 Nov (after Q4): an old amount above the agreed amount shows
                # its excess too, marked as the old record's, until a confirmed receipt
                # replaces it.
                if m.amount is not None and m.legacy_amount_received > m.amount:
                    item.over_agreed_old = format_rupees(m.legacy_amount_received - m.amount)
        elif m.amount is not None and confirmed > m.amount:
            # Ruling Q4 (FR20): an overpayment keeps its line.
            item.over_agreed = format_rupees(confirmed - m.amount)
        money[m.pk] = item
    return money


def unreceived_money(milestone):
    """The confirmed money and label of a NOT-Received milestone, from its cached figures
    alone — NO query (ruling Q9: Finance work's rows add none). A MilestoneMoney with
    `label` and `confirmed` only (no lines, no awaiting).

    WHY THE CACHE IS EXACT HERE. With the switch on, recompute keeps a Pending or
    Invoiced milestone's amount_received equal to its confirmed receipts (blank when
    none, H1). A legacy milestone — the one case where the cache holds something else,
    its old amount — is always Received (FR37), so it never reaches this function. Ask
    milestone_money() for a Received milestone, or for anything the card shows."""
    confirmed = milestone.amount_received or ZERO
    return MilestoneMoney(label=milestone_receipt_label(milestone, ReceiptTotals(confirmed=confirmed)),
                          confirmed=confirmed)


def owed(base, confirmed):
    """(what is still owed, how much was paid beyond it) for an amount due `base` (an
    invoice amount, say) and the confirmed money `confirmed`. Owed is never below ₹0
    (ruling J2); the excess is None unless confirmed money passed `base` (ruling Q9's
    "over invoice"). `base` None is "amount not set": (None, None)."""
    if base is None:
        return None, None
    over = confirmed - base
    return max(base - confirmed, ZERO), (over if over > 0 else None)


def client_payment_owed(milestones):
    """The CEO's "Payment Pending from Client" with the switch on (ruling Q2): (the owed
    sum, how many older milestones were left out of it). `milestones` is the caller's
    scoped queryset (the CEO view's: Invoiced or Received, agreed amount set, live site,
    not deleted, not a test site). ONE aggregate query: both figures come from it.

    Owed, as before the switch: agreed amount less amount received, on every milestone
    whose received amount is blank or short of the agreed amount. With the switch on the
    cached amount_received IS the confirmed money (or the old amount on a legacy
    milestone, FR37), so the sum needs no receipt read — except one case, ruling Q2: a
    legacy milestone with no old amount and no confirmed receipt has a blank received
    amount, which the old rule counted as wholly owed. It is left out of the sum and
    counted instead, so the page can say "plus n older milestones with no amount
    recorded"."""
    zero = Value(ZERO, output_field=DecimalField(max_digits=12, decimal_places=2))
    # What it fetches: one row of two figures. The confirmed-receipt test is a correlated
    # EXISTS inside the same statement, never a query per milestone.
    has_confirmed = Exists(PaymentReceipt.objects.filter(
        milestone=OuterRef('pk'), status=PaymentReceipt.CONFIRMED))
    older = Q(received_before_receipts=True, legacy_amount_received__isnull=True,
              has_confirmed=False)
    figures = (milestones
               .filter(Q(amount_received__isnull=True) | Q(amount_received__lt=F('amount')))
               .annotate(has_confirmed=has_confirmed)
               .aggregate(s=Sum(F('amount') - Coalesce(F('amount_received'), zero),
                                filter=~older),
                          n=Count('pk', filter=older)))
    return figures['s'] or 0, figures['n']


def receipts_confirmed_on(day, project_filter):
    """Every receipt CONFIRMED on `day` (an IST date: confirmed_at's date read in the
    current time zone, Asia/Kolkata), oldest confirmation first, with its milestone and
    site joined — for the EOD digest's "Payments received today" (ruling J5/Q1). An undone
    confirmation has its confirmed_at cleared (receipts.unconfirm_receipt), so it drops out
    by itself. `project_filter` is the caller's Project lookups (e.g. project_type,
    is_deleted, is_test). ONE query."""
    scope = {f'milestone__project__{key}': value for key, value in project_filter.items()}
    return list(PaymentReceipt.objects
                .filter(status=PaymentReceipt.CONFIRMED, confirmed_at__date=day, **scope)
                .select_related('milestone__project')
                .order_by('confirmed_at', 'pk'))


@dataclass
class MoneyIn:
    """Client money in, grouped by day, for the Finance Head page (money_in). Each dict is
    {day: [count, sum]} or {day: [count, sum, count without amount]}; a day of None means
    no date was ever recorded (an old record), counted in all_time() only."""
    confirmed: dict                   # Finance-confirmed receipts by date received (FR13)
    old: dict                         # old records: legacy (FR37) + stragglers (Q1)
    awaiting: Decimal = ZERO          # recorded + queried: current state, never money in (FR2)
    awaiting_receipts: int = 0
    queried_receipts: int = 0

    def _days(self):
        for day, (n, s) in self.confirmed.items():
            yield day, n, s, 0, False
        for day, (n, s, blank) in self.old.items():
            yield day, n, s, blank, True

    def between(self, start, end):
        """{count, sum, null_amount_count, old_count, old_sum} for days start..end
        (inclusive). `count` is confirmed receipts plus old-record milestones."""
        out = {'count': 0, 'sum': ZERO, 'null_amount_count': 0, 'old_count': 0,
               'old_sum': ZERO}
        for day, n, s, blank, is_old in self._days():
            if day is None or not start <= day <= end:
                continue
            out['count'] += n
            out['sum'] += s
            out['null_amount_count'] += blank
            if is_old:
                out['old_count'] += n
                out['old_sum'] += s
        return out

    def all_time(self):
        """As between(), over every day, undated old records included (as the cache-based
        figure counted every Received milestone whatever its date)."""
        out = {'count': 0, 'sum': ZERO, 'null_amount_count': 0, 'old_count': 0,
               'old_sum': ZERO}
        for _, n, s, blank, is_old in self._days():
            out['count'] += n
            out['sum'] += s
            out['null_amount_count'] += blank
            if is_old:
                out['old_count'] += n
                out['old_sum'] += s
        return out

    def by_week(self, first_monday, end):
        """{Monday: (count, sum)} for days first_monday..end, each day in its Monday-start
        week (the page's weekly chart)."""
        weeks = {}
        for day, n, s, _, _ in self._days():
            if day is None or not first_monday <= day <= end:
                continue
            monday = day - timedelta(days=day.weekday())
            count, total = weeks.get(monday, (0, ZERO))
            weeks[monday] = (count + n, total + s)
        return weeks


def money_in(milestones):
    """Money in for the caller's scoped `milestones` (a PaymentMilestone queryset; the
    Finance Head page passes finance_head.live_residential_milestones()), as a MoneyIn.
    TWO queries whatever the data size (K2); periods, weeks and the all-time figure are
    then worked out in Python from the grouped rows, so they can never disagree.

    WHAT COUNTS (FR2, FR13, FR37, K4):
      * every CONFIRMED receipt, on its own date received — two receipts on one milestone
        can land in different periods and weeks; an overpayment counts in full (it is
        cash that came in); an undone confirmation is no longer confirmed, so it drops out;
      * a legacy milestone (received_before_receipts) with NO confirmed receipt, at its
        old amount on its old date (legacy_amount_received / legacy_received_date) —
        back again if every confirmation on it is undone;
      * a straggler — Received, not marked, no receipt row of any status (FR40(1), the
        rows recompute leaves alone) — at amount_received on received_date (ruling Q1).
    Recorded and queried receipts are returned apart as `awaiting` (FR2); rejected and
    amount-needed rows count nowhere.

    Project type: the Finance Head page passes Residential milestones only; receipts
    exist on Residential sites only (FR28), so no other type is ever summed here."""
    # A subquery, not an id list: the scope's size never changes the query's shape.
    scope = milestones.order_by().values('pk')
    money = MoneyIn(confirmed={}, old={})

    # Query 1. What it fetches: every live receipt (recorded, queried, confirmed) on the
    # scope, grouped by status and date received — confirmed rows give money in, the
    # other two the awaiting line, from the same read.
    for row in (PaymentReceipt.objects
                .filter(milestone__in=scope, status__in=LIVE_STATUSES)
                .values('status', 'date_received')
                .annotate(n=Count('pk'), s=Sum('amount'))
                .order_by()):
        if row['status'] == PaymentReceipt.CONFIRMED:
            money.confirmed[row['date_received']] = [row['n'], row['s'] or ZERO]
        else:
            money.awaiting += row['s'] or ZERO
            money.awaiting_receipts += row['n']
            if row['status'] == PaymentReceipt.QUERIED:
                money.queried_receipts += row['n']

    # Query 2. What it fetches: the old records, grouped by their day — legacy milestones
    # with no confirmed receipt (FR37) and stragglers (K4). The two Exists are correlated
    # inside the one statement, never a query per milestone.
    confirmed = Exists(PaymentReceipt.objects.filter(milestone=OuterRef('pk'),
                                                     status=PaymentReceipt.CONFIRMED))
    any_receipt = Exists(PaymentReceipt.objects.filter(milestone=OuterRef('pk')))
    legacy = Q(received_before_receipts=True)
    old_rows = (milestones
                .filter((legacy & ~confirmed)
                        | (Q(status=PaymentMilestone.RECEIVED, received_before_receipts=False)
                           & ~any_receipt))
                .annotate(day=Case(When(legacy, then=F('legacy_received_date')),
                                   default=F('received_date'), output_field=DateField()),
                          money=Case(When(legacy, then=F('legacy_amount_received')),
                                     default=F('amount_received'),
                                     output_field=DecimalField(max_digits=12,
                                                               decimal_places=2)))
                .order_by()
                .values('day')
                .annotate(n=Count('pk'), s=Sum('money'),
                          blank=Count('pk', filter=(legacy
                                                    & Q(legacy_amount_received__isnull=True))
                                      | (~legacy & Q(amount_received__isnull=True)))))
    for row in old_rows:
        money.old[row['day']] = [row['n'], row['s'] or ZERO, row['blank']]
    return money
