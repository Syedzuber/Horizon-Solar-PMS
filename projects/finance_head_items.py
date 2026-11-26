"""
Finance Head "Every item" list (FIN-H-B4): every money item the Finance Head page counts,
one row each, filterable, each with its history. Data only: no page, URL or template
here (views_finance_head draws it).

PUBLIC
    clean_item_params(data)   the list's GET parameters, every value checked; anything
                              unknown falls back to its default, so no parameter raises
    finance_head_items(params, viewer_can_open_approvals, today=None, checks=None)
                              {count, page, pages, items, filters, merged}

?check=<key> (FIN-H-B5): `checks` is finance_head_checks.check_filters(), worked out by
the view from its own queries; the list adds none. A known key keeps exactly that check's
items (each with its note), lifts the 30-day window and ignores direction and status;
search and type still apply (B5 answer 8). An unknown key is ignored. This module never
imports finance_head_checks (that module imports this one).

THE THREE KINDS (R-B4-1). Scopes are IMPORTED from finance_head, never copied, so the
list holds exactly the rows the page's figures count:
    milestone  finance_head.live_residential_milestones()      direction In
    payment    finance_head.scoped_payments(), typed by
               finance_head.payment_type_expr()                direction Out
    bill       finance_head.unforwarded_bills() (remainder > 0) direction Out
A Pending milestone on a Draft or Cancelled site is left out, as the figures leave it
out of both "due" and "not yet due" (H26).

THE SEVEN QUERIES (R-B4-8), whatever the data size:
    1. milestone rows   all live Residential milestones, light, with the R8 trigger state,
                        the trigger task's pk and the latest transition / log time
    2. payment rows     all scoped payments, light, with the open hold and latest
                        transition / hold / answer times
    3. bill rows        all unforwarded bills, light
    4. transitions      StatusTransition for the SHOWN milestones, payments and trigger
                        tasks (Done only)
    5. log lines        ActivityLog for the shown milestones, by entity_id, plus the
                        lines that carry no entity_id, matched by site + Mx
    6. holds            PaymentRequestHold for the shown payments
    7. payment people   who raised, approved and paid each shown payment
Queries 4-7 ALWAYS run, even when the page shows no item of their kind (A6): an empty id
list is padded with 0, a pk no row has, so Django cannot skip the query and the page's
cost never depends on what the filters leave. Rows 1-3 are filtered, sorted and sliced
in Python ("light rows"), because one order runs across three tables.

MOVEMENT. Each light row's latest movement is the newest of its own dated events, worked
out in Python (a date-only field counts from 00:00 IST that day; SQLite's GREATEST
returns NULL if any argument is NULL, so no SQL GREATEST is used):
    milestone  latest transition, latest log line with its entity_id, received date,
               invoice date, the site's activation (the milestones are created then),
               and the trigger task's Done ONLY while the milestone is Pending (A1)
    payment    raised, approved, paid date, latest transition, latest hold, latest answer
    bill       approval, latest payment request raised on it (forwarding)
Log lines with no entity_id (older auto-mark lines, BD amount lines, the creation line)
are matched by text and so are not read for movement; they all sit on a day another
event already covers (receipt, activation) or on a Pending milestone, which is open and
listed anyway. They do appear in the history.

DEFAULT VIEW (R-B4-2): open items plus anything that moved in the last 30 days (today and
the 29 days before, finance_head.period_start('30d')), newest movement first. "Not yet
due" milestones are hidden unless that status is chosen. Choosing a status, or searching,
lifts the 30-day window, so an older closed item can still be found (see the report's
question on this).

HISTORY (R-B4-6), newest first, loaded for the SHOWN page only. A log line that records
a status move and a StatusTransition are one event when they are on the same milestone,
reach the same status, and are at most SAME_EVENT_SECONDS apart (A4): one line, the log's
text and name, the transition's role. The same rule merges a payment's hold rows with its
on-hold / answered transitions.

FIN-M-B5-8 (ruling Q4, BOTH switch states): a milestone_legacy_marked log line (written
once per old Received milestone by mark_legacy_received_milestones) is NOT movement — the
marking moves no money, and counting it would put every old receipt in the 30-day view
on the day the command runs. The line stays in the item's history. Every other line
counts as before, recompute's "(from confirmed receipts)" lines included.

FIN-M-B5-8 — WITH CLIENT_RECEIPTS_LIVE ON (receipts_on=True), ONE more query (eight):
    8. receipts     PaymentReceipt rows of the SHOWN milestones (with milestone and site,
                    for the receipt-page permission), for the money label, the receipt
                    links and the history's receipt lines.
  * Query 4 also reads the shown milestones' receipt transitions (one more condition,
    not a query): recorded, queried, answered, confirmed, confirmation undone, rejected
    (and a legacy row's amount completed), each with its person and role at the time.
    Receipt lines are never merged by the A4 duplicate rule (they name no milestone
    status); recompute's own milestone lines appear as they already do.
  * A milestone row's latest receipt move counts as movement (a correlated subquery in
    query 1): a receipt waiting for Finance is exactly what the Head needs to see.
  * Pending / Invoiced rows show what is still owed: agreed (or invoice) amount less the
    confirmed money, never below 0 (client_money.owed, K9). The status pill and the
    ?status= values are unchanged; the money label (receipts.milestone_receipt_label —
    "Amount needed", "Part received ₹x of ₹y", "Received before receipts" …) shows beneath
    it when it says more than the stored status (ruling Q6).
  * Links to a receipt's page are drawn only for a viewer user_can_view_receipt admits
    (F5: not CEO, Admin, System Admin); the history lines show to everyone who may open
    the Finance Head page (ruling Q5).

Not a views module: it reads models and finance_head and formats money with Finance
work's rupees() (views_finance_work imports views at module level, never this module, so
the import is not circular). No writes, no cache.
"""
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time
from urllib.parse import urlencode

from django.db.models import OuterRef, Q, Subquery
from django.urls import reverse
from django.utils import timezone

from .client_money import owed
from .finance_head import (NOT_ACTIONABLE_STATUSES, TYPES, due_state,
                           live_residential_milestones, payment_type_expr, period_start,
                           scoped_payments, unforwarded_bills)
from .milestone_queries import annotate_trigger_done
from .models import (ACTOR_ROLE_SYSTEM, SUBJECT_PAYMENT_MILESTONE, SUBJECT_PAYMENT_RECEIPT,
                     SUBJECT_PAYMENT_REQUEST, SUBJECT_TASK, ActivityLog, PaymentMilestone,
                     PaymentReceipt, PaymentRequest, PaymentRequestHold, StatusTransition,
                     Task)
from .permissions import user_can_view_receipt
from .receipts import UNCONFIRMED_STATUSES, ReceiptTotals, milestone_receipt_label
from .views_finance_work import TYPE_LABELS, rupees

PAGE_SIZE = 25
SEARCH_MAX = 100
#: A ?check= value is a short lower-case key; anything else is dropped before lookup.
CHECK_RE = re.compile(r'[a-z_]{1,40}')
#: A4: a log line and a transition at most this far apart are the same event.
SAME_EVENT_SECONDS = 60

DIRECTIONS = {'': 'All', 'in': 'In', 'out': 'Out'}
DIRECTION_MARKS = {'in': '↓ In', 'out': '↑ Out'}

#: Status keys (the ?status= values) -> label, grouped by kind in the order shown.
STATUS_GROUPS = (
    ('Milestones (in)', (('not_due', 'Not yet due'), ('due', 'Due'),
                         ('invoiced', 'Invoiced'), ('received', 'Received'))),
    ('Payment requests (out)', (('awaiting', 'Awaiting approval'), ('on_hold', 'On hold'),
                                ('to_pay', 'To pay'), ('paid', 'Paid'),
                                ('rejected', 'Rejected'))),
    ('Contractor bills (out)', (('not_forwarded', 'Not forwarded'),)),
)
STATUS_LABELS = {key: label for _, group in STATUS_GROUPS for key, label in group}
#: The status pill's colour; the label always says the status too (never colour alone).
STATUS_TONES = {'not_due': 'mut', 'due': 'warn', 'invoiced': 'info', 'received': 'ok',
                'awaiting': 'warn', 'on_hold': 'crit', 'to_pay': 'info', 'paid': 'ok',
                'rejected': 'mut', 'not_forwarded': 'warn'}
OPEN_STATUSES = frozenset({'not_due', 'due', 'invoiced', 'awaiting', 'on_hold', 'to_pay',
                           'not_forwarded'})

SORTS = {'latest': 'Latest movement', 'oldest': 'Oldest open', 'largest': 'Largest amount'}
DEFAULT_SORT = 'latest'

PAYMENT_STATUS_KEYS = {
    PaymentRequest.PENDING_APPROVAL: 'awaiting',
    PaymentRequest.ON_HOLD: 'on_hold',
    PaymentRequest.APPROVED: 'to_pay',
    PaymentRequest.CONFIRMED: 'paid',
    PaymentRequest.REJECTED: 'rejected',
}
PAYMENT_STATUS_WORDS = dict(PaymentRequest.STATUS_CHOICES)

MILESTONE_ENTITY = 'Milestone'
MILESTONE_NAMES = (PaymentMilestone.M1, PaymentMilestone.M2, PaymentMilestone.M3)
MILESTONE_STATUS_WORDS = {PaymentMilestone.PENDING, PaymentMilestone.INVOICED,
                          PaymentMilestone.RECEIVED}

#: The pre-28 Aug task-sync line (written with .update(), no entity_id, no transition).
LEGACY_RECEIVED_RE = re.compile(
    r"^Milestone (M[123]) auto-marked Received via completion of Finance task "
    r"'([^']+)' by (.+) \(([^()]+)\)$")
#: BD's amount line. Older rows read "M1=₹37918"; since FIN-M-L "M1 ₹2,000 → ₹3,000" or
#: "M1 blank → ₹3,000", so the name is taken before either '=' or a space.
AMOUNTS_PREFIX = 'Set milestone amounts:'
AMOUNTS_NAME_RE = re.compile(r'\b(M[123])(?:=|\s)')
#: views._log_milestones_created: one line for all three, no entity_id.
CREATED_PREFIX = 'Payment milestones M1'
#: A FIN-M-L plain line's status part, e.g. "status Pending → Received".
STATUS_MOVE_RE = re.compile(r'\bstatus (\w+) → (\w+)')
OLDER_RECORD = 'older record'
#: FIN-M-B5-8 ruling Q4: mark_legacy_received_milestones' code (its CODE; a test pins the
#: two equal). Not movement; still history. Spelt here because the command imports views
#: at module level, which this module must not.
LEGACY_MARKED_CODE = 'milestone_legacy_marked'
#: The money label's plain answers: the stored status itself, already said by the pill.
PLAIN_LABELS = frozenset({PaymentMilestone.PENDING, PaymentMilestone.INVOICED,
                          PaymentMilestone.RECEIVED})


# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------

def clean_item_params(data):
    """The list's parameters from `data` (a QueryDict or dict). dir, status, type and sort
    are checked against their fixed values; q is trimmed and cut to SEARCH_MAX; page is a
    whole number of at least 1 (an out-of-range page is clamped to the last page later,
    when the count is known). Anything else falls back to its default."""
    def pick(key, allowed, default=''):
        value = data.get(key, '')
        return value if value in allowed else default

    try:
        page = int(data.get('page', '') or 1)
    except (TypeError, ValueError):
        page = 1
    check = (data.get('check', '') or '').strip()
    return {
        'dir': pick('dir', DIRECTIONS),
        'status': pick('status', STATUS_LABELS),
        'type': pick('type', TYPES),
        'q': (data.get('q', '') or '').strip()[:SEARCH_MAX],
        'sort': pick('sort', SORTS, DEFAULT_SORT),
        'page': max(page, 1),
        # Only the shape is checked here; finance_head_items drops a key the page's
        # checks do not know.
        'check': check if CHECK_RE.fullmatch(check) else '',
    }


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _at(day):
    """A date as the aware datetime at 00:00 IST that day (TIME_ZONE is Asia/Kolkata)."""
    return timezone.make_aware(datetime.combine(day, time.min)) if day else None


def _local(dt):
    return timezone.localdate(dt) if dt is not None else None


def _latest(*values):
    present = [v for v in values if v is not None]
    return max(present) if present else None


def _name(first, last, username):
    full = f'{first or ""} {last or ""}'.strip()
    return full or username or ''


def _role(code):
    if not code:
        return ''
    return 'System' if code == ACTOR_ROLE_SYSTEM else code


def _ids(values):
    """Distinct ids, or [0] when there are none. 0 is a pk no row has: the query still
    runs (A6: the page always costs the same) and matches nothing."""
    return sorted({v for v in values if v is not None}) or [0]


def _site_code(code, project_id):
    # Finance work's rule (views_finance_work._site_code): the site code, else the id.
    return code or project_id or ''


# ---------------------------------------------------------------------------
# The item
# ---------------------------------------------------------------------------

@dataclass
class Item:
    kind: str                 # 'milestone', 'payment' or 'bill'
    pk: int
    direction: str            # 'in' or 'out'
    ptype: str                # 'Residential', 'OPEX' or 'CAPEX'
    status: str               # a STATUS_LABELS key
    reference: str
    detail: str
    party: str
    amount: object            # Decimal, or None = amount not set
    amount_label: str
    since: object             # the date an open item waits from; None when unknown
    movement: object          # aware datetime of the latest movement; None when none
    haystack: str             # lower-case text the search looks in
    row: dict                 # the light row, for links and history
    age: object = None
    links: list = field(default_factory=list)
    history: list = field(default_factory=list)
    last_date: object = None
    last_who: str = ''
    check_note: str = ''      # FIN-H-B5: why this item is in the chosen ?check=
    money_label: str = ''     # FIN-M-B5-8: the receipt label, when it says more than the pill

    @property
    def key(self):
        return f'{self.kind}-{self.pk}'

    @property
    def is_open(self):
        return self.status in OPEN_STATUSES

    @property
    def status_label(self):
        return STATUS_LABELS[self.status]

    @property
    def tone(self):
        return STATUS_TONES[self.status]

    @property
    def mark(self):
        return DIRECTION_MARKS[self.direction]

    @property
    def type_label(self):
        return TYPE_LABELS.get(self.ptype, self.ptype)

    @property
    def amount_text(self):
        return rupees(self.amount)


# ---------------------------------------------------------------------------
# 1. Milestone rows
# ---------------------------------------------------------------------------

def _milestone_rows(receipts_on=False):
    """Query 1. Every live Residential milestone (finance_head scope), light, with the R8
    trigger state and the trigger task's pk (milestone_queries.annotate_trigger_done:
    trigger_task_id comes from the same subquery as the state, so the history reads the
    Done of the very task the due rule read) and two more correlated subqueries, not
    queries: the latest transition and latest entity-linked log line, for movement.
    order_by() clears Meta.ordering: the list is sorted in Python.

    The log line read for movement skips LEGACY_MARKED_CODE (ruling Q4). With the switch
    on (FIN-M-B5-8) a third correlated subquery adds the time of the latest transition of
    any of the milestone's receipts, also movement."""
    last_transition = Subquery(
        StatusTransition.objects.filter(subject_type=SUBJECT_PAYMENT_MILESTONE,
                                        subject_id=OuterRef('pk'))
        .order_by('-occurred_at').values('occurred_at')[:1])
    last_log = Subquery(
        ActivityLog.objects.filter(entity_type=MILESTONE_ENTITY, entity_id=OuterRef('pk'))
        .exclude(action_code=LEGACY_MARKED_CODE)
        .order_by('-timestamp').values('timestamp')[:1])
    extra = {}
    fields = []
    if receipts_on:
        # What it adds: the newest receipt event on this milestone (recorded, queried,
        # answered, confirmed, undone, rejected) — the receipts are matched inside the
        # same statement (nested OuterRef), never one query per milestone.
        extra['last_receipt_move'] = Subquery(
            StatusTransition.objects.filter(
                subject_type=SUBJECT_PAYMENT_RECEIPT,
                subject_id__in=PaymentReceipt.objects.filter(
                    milestone=OuterRef(OuterRef('pk'))).values('pk'))
            .order_by('-occurred_at').values('occurred_at')[:1])
        fields = ['last_receipt_move', 'received_before_receipts']
    return (annotate_trigger_done(live_residential_milestones())
            .annotate(last_transition=last_transition, last_log=last_log, **extra)
            .order_by()
            .values('pk', 'milestone_name', 'status', 'amount', 'amount_received',
                    'invoice_amount', 'invoice_date', 'invoice_number', 'received_date',
                    'project', 'project__project_id', 'project__site_code',
                    'project__customer_name', 'project__site_name', 'project__status',
                    'project__activated_at', 'trigger_name', 'trigger_status', 'trigger_na',
                    'trigger_done_at', 'trigger_task_id', 'last_transition', 'last_log',
                    *fields))


def _still_owed(base, r, receipts_on):
    """K9: with the switch on, `base` less the confirmed money (the cached
    amount_received, exact for Pending / Invoiced), never below 0, through
    client_money.owed — the figures' own rule (finance_head._still_owed). Off: `base`."""
    if not receipts_on:
        return base
    left, _ = owed(base, r['amount_received'] or 0)
    return left


def _milestone_items(rows, receipts_on=False):
    items = []
    for r in rows:
        status = r['status']
        trigger_done = r['trigger_status'] == Task.DONE
        if status == PaymentMilestone.PENDING:
            if r['project__status'] in NOT_ACTIONABLE_STATUSES:
                continue                    # H26: counted in neither due nor not due
            # The B2 figures' own rule (finance_head.due_state), so the list's due /
            # not-due always matches the tiles.
            key, since = due_state(r)
            amount = _still_owed(r['amount'], r, receipts_on)
        elif status == PaymentMilestone.INVOICED:
            key = 'invoiced'
            # As the figures: the invoice figure when recorded, else the agreed amount.
            amount = _still_owed(
                r['invoice_amount'] if r['invoice_amount'] is not None else r['amount'],
                r, receipts_on)
            # As receivables ageing: the invoice date, else the trigger's Done date.
            since = r['invoice_date'] or (_local(r['trigger_done_at']) if trigger_done
                                          else None)
        elif status == PaymentMilestone.RECEIVED:
            key, since, amount = 'received', None, r['amount_received']
        else:
            continue
        code = _site_code(r['project__site_code'], r['project__project_id'])
        movement = _latest(
            r['last_transition'], r['last_log'], _at(r['received_date']),
            _at(r['invoice_date']), r['project__activated_at'],
            # A1: the trigger's Done is movement only while the milestone is Pending.
            r['trigger_done_at'] if status == PaymentMilestone.PENDING and trigger_done
            else None,
            r.get('last_receipt_move'))      # FIN-M-B5-8: absent (None) with the switch off
        items.append(Item(
            kind='milestone', pk=r['pk'], direction='in', ptype='Residential', status=key,
            reference=f'{code} · {r["milestone_name"]}',
            detail=r['project__site_name'] or '',
            party=r['project__customer_name'] or '',
            amount=amount, amount_label='received' if key == 'received' else 'to collect',
            since=since, movement=movement,
            haystack=' '.join(v for v in (
                code, r['project__project_id'], r['project__customer_name'],
                r['project__site_name'], r['invoice_number'], r['milestone_name']) if v
            ).lower(),
            row=r))
    return items


# ---------------------------------------------------------------------------
# 2. Payment rows
# ---------------------------------------------------------------------------

def _payment_rows():
    """Query 2. Every scoped payment request (finance_head scope), light. The open hold's
    held_at (at most one open hold per request, a partial unique index) and the latest
    transition, hold and answer times are correlated subqueries, not queries."""
    holds = PaymentRequestHold.objects.filter(payment_request=OuterRef('pk'))
    return (scoped_payments()
            .annotate(
                ptype=payment_type_expr(),
                open_hold_at=Subquery(holds.filter(responded_at__isnull=True)
                                      .order_by().values('held_at')[:1]),
                last_hold_at=Subquery(holds.order_by('-held_at').values('held_at')[:1]),
                last_response_at=Subquery(holds.filter(responded_at__isnull=False)
                                          .order_by('-responded_at')
                                          .values('responded_at')[:1]),
                last_transition=Subquery(
                    StatusTransition.objects.filter(subject_type=SUBJECT_PAYMENT_REQUEST,
                                                    subject_id=OuterRef('pk'))
                    .order_by('-occurred_at').values('occurred_at')[:1]))
            .order_by()
            .values('pk', 'status', 'amount', 'approved_amount', 'requested_date',
                    'approved_at', 'payment_date', 'payment_reference', 'decision_reason',
                    'vendor__name', 'vendor_order', 'vendor_order__po_number',
                    'vendor_order__pi_number', 'contractor_bill',
                    'contractor_bill__bill_number', 'contractor_bill__request',
                    'contractor_bill__project__project_id',
                    'contractor_bill__project__site_code',
                    'contractor_bill__project__customer_name',
                    'project__project_id', 'project__site_code', 'project__customer_name',
                    'ptype', 'open_hold_at', 'last_hold_at', 'last_response_at',
                    'last_transition'))


def _payment_items(rows):
    items = []
    for r in rows:
        key = PAYMENT_STATUS_KEYS.get(r['status'])
        if key is None:
            continue
        effective = (r['approved_amount'] if r['approved_amount'] is not None
                     else r['amount'])
        if key == 'awaiting':
            amount, label, since = r['amount'], 'requested', _local(r['requested_date'])
        elif key in ('on_hold', 'to_pay'):
            amount = effective
            label = 'approved' if r['approved_amount'] is not None else 'requested'
            since = _local(r['open_hold_at'] if key == 'on_hold' else r['approved_at'])
        elif key == 'paid':
            amount, label, since = effective, 'paid', None
        else:
            amount, label, since = r['amount'], 'requested', None
        if r['contractor_bill'] is not None:
            code = _site_code(r['contractor_bill__project__site_code'],
                              r['contractor_bill__project__project_id'])
            customer = r['contractor_bill__project__customer_name']
            detail = f'Bill {r["contractor_bill__bill_number"]}'
        else:
            code = _site_code(r['project__site_code'], r['project__project_id'])
            customer = r['project__customer_name']
            numbers = [f'PO {r["vendor_order__po_number"]}' if r['vendor_order__po_number']
                       else '',
                       f'PI {r["vendor_order__pi_number"]}' if r['vendor_order__pi_number']
                       else '']
            detail = ' · '.join(n for n in numbers if n) or f'Order #{r["vendor_order"]}'
        reference = f'PR-{r["pk"]}'
        items.append(Item(
            kind='payment', pk=r['pk'], direction='out', ptype=r['ptype'], status=key,
            reference=reference, detail=' · '.join(v for v in (detail, code) if v),
            party=r['vendor__name'] or '', amount=amount, amount_label=label, since=since,
            movement=_latest(r['requested_date'], r['approved_at'], _at(r['payment_date']),
                             r['last_transition'], r['last_hold_at'],
                             r['last_response_at']),
            haystack=' '.join(v for v in (
                reference, r['vendor__name'], r['vendor_order__po_number'],
                r['vendor_order__pi_number'], r['contractor_bill__bill_number'], code,
                customer) if v).lower(),
            row=r))
    return items


# ---------------------------------------------------------------------------
# 3. Bill rows
# ---------------------------------------------------------------------------

def _bill_rows():
    """Query 3. Every approved contractor bill not yet fully forwarded (finance_head
    scope, remainder annotated), light, with the bill's approval, contractor and raiser
    joined and the latest forwarding (payment request on the bill) as a correlated
    subquery."""
    return (unforwarded_bills()
            .annotate(last_forwarded=Subquery(
                PaymentRequest.objects.filter(contractor_bill=OuterRef('pk'))
                .order_by('-requested_date').values('requested_date')[:1]))
            .order_by()
            .values('pk', 'amount', 'remainder', 'bill_number', 'bill_date', 'request',
                    'request__closed_at', 'request__raised_at', 'request__vendor__name',
                    'request__raised_by__role', 'request__raised_by__user__first_name',
                    'request__raised_by__user__last_name',
                    'request__raised_by__user__username', 'project__project_id',
                    'project__site_code', 'project__customer_name', 'project__project_type',
                    'last_forwarded'))


def _bill_items(rows):
    items = []
    for r in rows:
        code = _site_code(r['project__site_code'], r['project__project_id'])
        reference = f'Bill {r["bill_number"]}'
        items.append(Item(
            kind='bill', pk=r['pk'], direction='out', ptype=r['project__project_type'],
            status='not_forwarded', reference=reference, detail=code,
            party=r['request__vendor__name'] or '', amount=r['remainder'],
            # B4 answer 4: the bills row's own words on the page above.
            amount_label='not yet sent to Finance', since=_local(r['request__closed_at']),
            movement=_latest(r['request__closed_at'], r['last_forwarded']),
            haystack=' '.join(v for v in (
                reference, r['bill_number'], r['request__vendor__name'], code,
                r['project__customer_name']) if v).lower(),
            row=r))
    return items


# ---------------------------------------------------------------------------
# Filter, sort, page
# ---------------------------------------------------------------------------

def _keep(item, p, window_start, check=None):
    """R-B4-2 / R-B4-3 for one item; under a ?check= (FIN-H-B5) only the check's own
    items, then type and search (B5 answer 8: direction, status and the window are
    ignored, so the count on the check equals the items it opens)."""
    if check is not None:
        if item.key not in check['items']:
            return False
        if p['type'] and item.ptype != p['type']:
            return False
        return not p['q'] or p['q'].lower() in item.haystack
    if p['dir'] and item.direction != p['dir']:
        return False
    if p['type'] and item.ptype != p['type']:
        return False
    if p['status']:
        if item.status != p['status']:
            return False
    elif item.status == 'not_due':
        return False                       # hidden unless that status is chosen
    if p['q'] and p['q'].lower() not in item.haystack:
        return False
    if not p['status'] and not p['q'] and not item.is_open:
        # The default window: a closed item shows while it moved in the last 30 days.
        return item.movement is not None and _local(item.movement) >= window_start
    return True


def _latest_key(item):
    # Newest movement first; an item with none last; then a stable tie-break.
    return (item.movement is None,
            -item.movement.timestamp() if item.movement is not None else 0,
            item.kind, item.pk)


def _sort(items, sort):
    if sort == 'oldest':
        # Open items waiting longest first; open with no date next; closed ones last,
        # newest movement first among them.
        return sorted(items, key=lambda i: (not i.is_open, i.since is None,
                                            i.since or date.max, _latest_key(i)))
    if sort == 'largest':
        return sorted(items, key=lambda i: (i.amount is None, -(i.amount or 0),
                                            _latest_key(i)))
    return sorted(items, key=_latest_key)


# ---------------------------------------------------------------------------
# History (shown page only)
# ---------------------------------------------------------------------------

def _event(at, text, who='', role='', note='', to=None, moves=True, date_only=False,
           source='record'):
    return {'at': at, 'text': text, 'who': who, 'role': role, 'note': note, 'to': to,
            'moves': moves, 'date_only': date_only, 'source': source}


def _merge(records, transitions):
    """A4: each record that names a status (`to`) takes the first unused transition to
    the same status at most SAME_EVENT_SECONDS away: one line, the record's text and
    name, the transition's role. Returns (lines, merged_count)."""
    used = set()
    for rec in records:
        if rec['to'] is None or rec['at'] is None:
            continue
        for i, t in enumerate(transitions):
            if i in used or t['to'] != rec['to'] or t['at'] is None:
                continue
            if abs((t['at'] - rec['at']).total_seconds()) <= SAME_EVENT_SECONDS:
                used.add(i)
                rec['role'] = t['role'] or rec['role']
                rec['who'] = rec['who'] or t['who']
                break
    return records + [t for i, t in enumerate(transitions) if i not in used], len(used)


def _newest_first(lines):
    """Newest first; a line with no time last."""
    dated = sorted((l for l in lines if l['at'] is not None), key=lambda l: l['at'],
                   reverse=True)
    return dated + [l for l in lines if l['at'] is None]


def _who(name, role):
    return f'{name} ({role})' if name and role else name or role or ''


def _transition_event(t, text):
    return _event(t['occurred_at'], text,
                  who=_name(t['actor__user__first_name'], t['actor__user__last_name'],
                            t['actor__user__username']),
                  role=_role(t['actor_role_code']), to=t['to_status'], source='transition')


def _log_who(line):
    return _name(line['actor__user__first_name'], line['actor__user__last_name'],
                 line['actor__user__username'])


def _received_on(day, today):
    """'received 12 Nov' — the date the money arrived (FR13), with the year only when it
    is not this year's."""
    if day is None:
        return 'date received not yet recorded'
    return f'received {day.day} {day:%b}' + (f' {day.year}' if day.year != today.year else '')


def _receipt_history(receipts, transitions, today, viewer=None):
    """FIN-M-B5-8 (K10): one line per receipt event of the shown milestone's receipts —
    amount, date received, the person (and role at the time) — newest first with the
    rest. `to` is None, so the A4 merge never touches them.

    Every line of a receipt (confirmed and rejected included) links to that receipt's
    page, but only for a viewer the receipt page admits (F5, ruling Q5 and the wording
    answer 3); the line itself shows to everyone who may open this page."""
    lines = []
    for receipt in receipts:
        amount = rupees(receipt.amount) if receipt.amount is not None else 'Old receipt'
        received = _received_on(receipt.date_received, today)
        # Asked once per receipt; no query (site comes with the receipt, see _load_receipts).
        href = (reverse('receipt_detail', args=[receipt.milestone.project.project_id,
                                                receipt.pk])
                if user_can_view_receipt(viewer, receipt) else '')
        for t in transitions.get(receipt.pk, []):
            frm, to = t['from_status'], t['to_status']
            name = _name(t['actor__user__first_name'], t['actor__user__last_name'],
                         t['actor__user__username'])
            remark = f': {t["remark"]}' if t['remark'] else ''
            if to == PaymentReceipt.CONFIRMED:
                # FR13: the line's time is the confirmation; the money's day is beside it.
                text = f'{amount} confirmed by {name or "migration"} — {received}'
            elif to == PaymentReceipt.QUERIED:
                text = f'{amount} queried — {received}{remark}'
            elif to == PaymentReceipt.REJECTED:
                text = f'{amount} rejected — {received}{remark}'
            elif to == PaymentReceipt.RECORDED and frm == PaymentReceipt.QUERIED:
                text = f'Query on {amount} answered — {received}{remark}'
            elif to == PaymentReceipt.RECORDED and frm == PaymentReceipt.CONFIRMED:
                text = f'{amount} confirmation undone — {received}{remark}'
            elif to == PaymentReceipt.RECORDED and frm == PaymentReceipt.AMOUNT_NEEDED:
                text = f'{amount} amount completed from the bank — {received}'
            elif to == PaymentReceipt.RECORDED:
                text = f'{amount} recorded — {received}'
            else:
                text = 'Old receipt waiting for its amount'
            line = _event(t['occurred_at'], text, who=name, role=_role(t['actor_role_code']),
                          source='receipt')
            line['href'] = href
            lines.append(line)
    return lines


def _milestone_history(item, transitions, task_done, entity_logs, site_logs):
    r = item.row
    records = []
    # The trigger task's Done (M2/M3). Task has no completed_by, so the person is read
    # from the task's own Done transition: the one closest to completed_at.
    if r['trigger_status'] == Task.DONE and r['trigger_done_at'] is not None:
        done = task_done.get(r['trigger_task_id'], [])
        closest = min(done, key=lambda t: abs((t['occurred_at'] - r['trigger_done_at'])
                                              .total_seconds()), default=None)
        records.append(_event(
            r['trigger_done_at'], f'Trigger task “{r["trigger_name"]}” marked Done',
            who=_name(closest['actor__user__first_name'], closest['actor__user__last_name'],
                      closest['actor__user__username']) if closest else '',
            role=_role(closest['actor_role_code']) if closest else '',
            # A1: always in the history; movement only while the milestone is Pending.
            moves=r['status'] == PaymentMilestone.PENDING))
    for line in entity_logs.get(item.pk, []):
        move = STATUS_MOVE_RE.search(line['action'])
        to = move.group(2) if move and move.group(2) in MILESTONE_STATUS_WORDS else None
        records.append(_event(line['timestamp'], line['action'], who=_log_who(line), to=to))
    for line in site_logs.get((r['project'], r['milestone_name']), []):
        records.append(dict(line))     # a copy: one creation line serves three milestones
    trans = [_transition_event(t, f'Status {t["from_status"] or "new"} → {t["to_status"]}'
                                  + (' (task sync)' if t['reason_code'] == 'milestone_sync'
                                     else ''))
             for t in transitions.get(item.pk, [])]
    return _merge(records, trans)


def _payment_text(t, r, last_approval_pk):
    to, frm = t['to_status'], t['from_status']
    if to == PaymentRequest.PENDING_APPROVAL and not frm:
        return f'Raised for {rupees(r["amount"])}'
    if to == PaymentRequest.PENDING_APPROVAL and frm == PaymentRequest.ON_HOLD:
        return f'Hold answered{": " + t["remark"] if t["remark"] else ""}'
    if to == PaymentRequest.APPROVED:
        text = 'Approved'
        # The part amount stands on the request only for its latest approval (O4c).
        if (t['pk'] == last_approval_pk and r['approved_amount'] is not None
                and r['approved_amount'] < r['amount']):
            text += (f' {rupees(r["approved_amount"])} of {rupees(r["amount"])} requested'
                     ' (part approval)')
        return text + (f': {t["remark"]}' if t['remark'] else '')
    if to == PaymentRequest.ON_HOLD:
        return f'Put on hold{": " + t["remark"] if t["remark"] else ""}'
    if to == PaymentRequest.REJECTED:
        return f'Rejected: {t["remark"] or r["decision_reason"]}'
    if to == PaymentRequest.CONFIRMED:
        return _paid_text(r)
    return f'Status {PAYMENT_STATUS_WORDS.get(frm, frm or "new")} → ' \
           f'{PAYMENT_STATUS_WORDS.get(to, to)}'


def _paid_text(r):
    day = r['payment_date']
    paid = r['approved_amount'] if r['approved_amount'] is not None else r['amount']
    text = f'Paid {rupees(paid)}'
    if day:
        text += f' on {day.day} {day:%b %Y}'
    if r['payment_reference']:
        text += f', ref {r["payment_reference"]}'
    return text


def _payment_history(item, transitions, holds, people):
    r = item.row
    who = people.get(item.pk, {})
    trans_rows = transitions.get(item.pk, [])
    last_approval = max((t for t in trans_rows if t['to_status'] == PaymentRequest.APPROVED),
                        key=lambda t: t['occurred_at'], default=None)
    trans = [_transition_event(t, _payment_text(t, r, last_approval['pk'] if last_approval
                                                else None))
             for t in trans_rows]
    have = {t['to_status'] for t in trans_rows}
    raise_recorded = any(not t['from_status']
                         and t['to_status'] == PaymentRequest.PENDING_APPROVAL
                         for t in trans_rows)
    records = []
    for h in holds.get(item.pk, []):
        records.append(_event(h['held_at'], f'Put on hold: {h["reason"]}',
                              who=_name(h['held_by__user__first_name'],
                                        h['held_by__user__last_name'],
                                        h['held_by__user__username']),
                              to=PaymentRequest.ON_HOLD))
        if h['responded_at'] is not None:
            records.append(_event(h['responded_at'], f'Hold answered: {h["response"]}',
                                  who=_name(h['responded_by__user__first_name'],
                                            h['responded_by__user__last_name'],
                                            h['responded_by__user__username']),
                                  to=PaymentRequest.PENDING_APPROVAL))
    # Rows from before the ledger (or written without it) still say what their own
    # fields record: raised, approved, paid, rejected — each only when no transition
    # already tells it.
    if not raise_recorded:
        records.append(_event(r['requested_date'], f'Raised for {rupees(r["amount"])}',
                              who=who.get('raised_by', '')))
    if r['approved_at'] is not None and PaymentRequest.APPROVED not in have:
        text = 'Approved'
        if r['approved_amount'] is not None and r['approved_amount'] < r['amount']:
            text += (f' {rupees(r["approved_amount"])} of {rupees(r["amount"])} requested'
                     ' (part approval)')
        records.append(_event(r['approved_at'], text, who=who.get('approved_by', '')))
    if r['status'] == PaymentRequest.CONFIRMED and PaymentRequest.CONFIRMED not in have:
        records.append(_event(_at(r['payment_date']), _paid_text(r),
                              who=who.get('confirmed_by', ''), date_only=True))
    if r['status'] == PaymentRequest.REJECTED and PaymentRequest.REJECTED not in have:
        records.append(_event(None, f'Rejected: {r["decision_reason"]}'))
    return _merge(records, trans)


def _bill_history(item):
    r = item.row
    raised_by = _name(r['request__raised_by__user__first_name'],
                      r['request__raised_by__user__last_name'],
                      r['request__raised_by__user__username'])
    bill_day = r['bill_date']
    lines = [
        _event(r['request__raised_at'],
               f'Bill {r["bill_number"]} dated {bill_day.day} {bill_day:%b %Y} for '
               f'{rupees(r["amount"])} raised for approval', who=raised_by),
        _event(r['request__closed_at'], 'Bill approved'),
        _event(r['last_forwarded'],
               f'Already forwarded to Finance {rupees(r["amount"] - r["remainder"])}; '
               f'not yet forwarded {rupees(r["remainder"])}',
               note='' if r['last_forwarded'] else 'nothing forwarded yet'),
    ]
    return lines, 0


def _site_log_lines(logs):
    """Log lines with no entity_id -> {(project pk, Mx): [event, ...]}: the older
    auto-mark line (one Mx, noted "older record", name and role from its own text), BD's
    amount line (every Mx it names) and the creation line (all three, A3)."""
    by_site = defaultdict(list)
    for line in logs:
        text = line['action']
        legacy = LEGACY_RECEIVED_RE.match(text)
        if legacy:
            by_site[(line['project'], legacy.group(1))].append(_event(
                line['timestamp'], text, who=legacy.group(3), role=legacy.group(4),
                note=OLDER_RECORD, to=PaymentMilestone.RECEIVED))
        elif text.startswith(AMOUNTS_PREFIX):
            for name in dict.fromkeys(AMOUNTS_NAME_RE.findall(text[len(AMOUNTS_PREFIX):])):
                by_site[(line['project'], name)].append(
                    _event(line['timestamp'], text, who=_log_who(line)))
        elif text.startswith(CREATED_PREFIX):
            for name in MILESTONE_NAMES:
                by_site[(line['project'], name)].append(
                    _event(line['timestamp'], text, who=_log_who(line)))
    return by_site


ACTOR_FIELDS = ('actor__user__first_name', 'actor__user__last_name',
                'actor__user__username')


def _load_receipts(ms, receipts_on, viewer, today):
    """Query 8 (switch on only, FIN-M-B5-8): the shown milestones' receipts, any status,
    as instances with milestone and site (user_can_view_receipt reads the site through
    the receipt). Sets each milestone item's money label and its receipt links; returns
    {milestone pk: [receipts]} for the history. Off: {} and no query.

    The permission is asked per receipt but costs no query for this page's viewers: the
    Finance Head is Finance and CEO / Admin / System Admin are refused, both by role
    (permissions.user_can_view_receipt); the site comes with the receipt."""
    if not receipts_on:
        return {}
    by_ms = defaultdict(list)
    for receipt in (PaymentReceipt.objects.filter(milestone_id__in=_ids(i.pk for i in ms))
                    .select_related('milestone__project').order_by('date_received', 'pk')):
        by_ms[receipt.milestone_id].append(receipt)
    for item in ms:
        r, mine = item.row, by_ms.get(item.pk, [])
        totals = ReceiptTotals(
            confirmed=sum((x.amount for x in mine if x.status == PaymentReceipt.CONFIRMED),
                          ReceiptTotals().confirmed),
            unconfirmed=sum((x.amount for x in mine if x.status in UNCONFIRMED_STATUSES),
                            ReceiptTotals().unconfirmed),
            amount_needed=sum(x.status == PaymentReceipt.AMOUNT_NEEDED for x in mine))
        # The label helper reads status, amount and the legacy flag from a milestone; an
        # unsaved instance built from the light row carries exactly those (no query).
        label = milestone_receipt_label(
            PaymentMilestone(pk=item.pk, status=r['status'], amount=r['amount'],
                             received_before_receipts=r['received_before_receipts']),
            totals)
        item.money_label = '' if label in PLAIN_LABELS else label
        # K8a / ruling Q5: a link to each receipt still waiting for Finance, only for a
        # viewer the receipt page admits (F5).
        for x in mine:
            if x.status in UNCONFIRMED_STATUSES and user_can_view_receipt(viewer, x):
                item.links.append({
                    'label': f'Receipt {rupees(x.amount)} ({x.get_status_display().lower()})',
                    'kind': 'receipt',
                    'href': reverse('receipt_detail', args=[r['project__project_id'], x.pk])})
    return by_ms


def _load_history(shown, receipts_on=False, viewer=None, today=None):
    """Queries 4-7 for the shown items (and 8 with the switch on), then each item's
    history. Returns the number of duplicate lines merged away (A4)."""
    ms = [i for i in shown if i.kind == 'milestone']
    pays = [i for i in shown if i.kind == 'payment']
    ms_ids = _ids(i.pk for i in ms)
    pay_ids = _ids(i.pk for i in pays)
    trigger_ids = _ids(i.row['trigger_task_id'] for i in ms)
    project_ids = _ids(i.row['project'] for i in ms)
    receipts = _load_receipts(ms, receipts_on, viewer, today)

    # 4. Every transition the shown page needs, in one read: the milestones', the
    # payments', and the trigger tasks' Done moves (for who completed the trigger).
    # FIN-M-B5-8, switch on: and the shown milestones' receipts' moves — a subquery on
    # the receipt table inside the same statement, not a query.
    wanted = (Q(subject_type=SUBJECT_PAYMENT_MILESTONE, subject_id__in=ms_ids)
              | Q(subject_type=SUBJECT_PAYMENT_REQUEST, subject_id__in=pay_ids)
              | Q(subject_type=SUBJECT_TASK, subject_id__in=trigger_ids,
                  to_status=Task.DONE))
    if receipts_on:
        wanted |= Q(subject_type=SUBJECT_PAYMENT_RECEIPT,
                    subject_id__in=PaymentReceipt.objects.filter(milestone_id__in=ms_ids)
                    .values('pk'))
    transitions = list(
        StatusTransition.objects
        .filter(wanted)
        .order_by('occurred_at')
        .values('pk', 'subject_type', 'subject_id', 'from_status', 'to_status',
                'actor_role_code', 'reason_code', 'remark', 'occurred_at', *ACTOR_FIELDS))
    # 5. The milestones' log lines: by entity_id, plus the site's lines that carry none
    # (older auto-mark, BD amounts, creation), narrowed by their fixed opening words so
    # no unrelated site line is read.
    logs = list(
        ActivityLog.objects
        .filter(Q(entity_type=MILESTONE_ENTITY, entity_id__in=ms_ids)
                | (Q(entity_id__isnull=True, project_id__in=project_ids)
                   & (Q(action__startswith='Milestone M')
                      | Q(action__startswith=AMOUNTS_PREFIX)
                      | Q(action__startswith=CREATED_PREFIX))))
        .order_by('timestamp')
        .values('entity_id', 'project', 'action', 'timestamp', *ACTOR_FIELDS))
    # 6. Holds, each with who held and who answered.
    holds = defaultdict(list)
    for h in (PaymentRequestHold.objects.filter(payment_request_id__in=pay_ids)
              .order_by('held_at')
              .values('payment_request', 'reason', 'held_at', 'response', 'responded_at',
                      'held_by__user__first_name', 'held_by__user__last_name',
                      'held_by__user__username', 'responded_by__user__first_name',
                      'responded_by__user__last_name', 'responded_by__user__username')):
        holds[h['payment_request']].append(h)
    # 7. Who raised, approved and paid each payment (requested_by and confirmed_by are
    # auth users; approved_by is a profile).
    people = {}
    for p in (PaymentRequest.objects.filter(pk__in=pay_ids).order_by()
              .values('pk', 'requested_by__first_name', 'requested_by__last_name',
                      'requested_by__username', 'approved_by__user__first_name',
                      'approved_by__user__last_name', 'approved_by__user__username',
                      'confirmed_by__first_name', 'confirmed_by__last_name',
                      'confirmed_by__username')):
        people[p['pk']] = {
            'raised_by': _name(p['requested_by__first_name'], p['requested_by__last_name'],
                               p['requested_by__username']),
            'approved_by': _name(p['approved_by__user__first_name'],
                                 p['approved_by__user__last_name'],
                                 p['approved_by__user__username']),
            'confirmed_by': _name(p['confirmed_by__first_name'], p['confirmed_by__last_name'],
                                  p['confirmed_by__username']),
        }

    ms_trans, pay_trans, task_done = defaultdict(list), defaultdict(list), defaultdict(list)
    receipt_trans = defaultdict(list)
    for t in transitions:
        target = {SUBJECT_PAYMENT_MILESTONE: ms_trans, SUBJECT_PAYMENT_REQUEST: pay_trans,
                  SUBJECT_TASK: task_done,
                  SUBJECT_PAYMENT_RECEIPT: receipt_trans}[t['subject_type']]
        target[t['subject_id']].append(t)
    entity_logs = defaultdict(list)
    for line in logs:
        if line['entity_id'] is not None:
            entity_logs[line['entity_id']].append(line)
    site_logs = _site_log_lines(l for l in logs if l['entity_id'] is None)

    merged = 0
    for item in shown:
        if item.kind == 'milestone':
            lines, n = _milestone_history(item, ms_trans, task_done, entity_logs, site_logs)
            # FIN-M-B5-8: receipt events, after the merge so A4 never pairs them.
            lines = lines + _receipt_history(receipts.get(item.pk, []), receipt_trans,
                                             today, viewer)
        elif item.kind == 'payment':
            lines, n = _payment_history(item, pay_trans, holds, people)
        else:
            lines, n = _bill_history(item)
        merged += n
        item.history = _newest_first(lines)
        for line in item.history:
            line['when'] = _when(line)
            line['who_text'] = _who(line['who'], line['role'])
        # Last movement: its day from the light row (what the list is sorted by), its
        # person from the newest line that counts as movement on that same day.
        item.last_date = _local(item.movement)
        same_day = next((l for l in item.history if l['moves'] and l['at'] is not None
                         and _local(l['at']) == item.last_date), None)
        item.last_who = same_day['who_text'] if same_day else ''
    return merged


def _when(line):
    """'12 Oct 2026, 10:15' in IST; a date-only event without the time; None as ''."""
    at = line['at']
    if at is None:
        return ''
    local = timezone.localtime(at)
    if line['date_only']:
        return f'{local.day} {local:%b %Y}'
    return f'{local.day} {local:%b %Y}, {local:%H:%M}'


# ---------------------------------------------------------------------------
# Links (R-B4-7)
# ---------------------------------------------------------------------------

def _links(item, viewer_can_open_approvals):
    r = item.row
    if item.kind == 'milestone':
        links = []
        if item.status in ('due', 'invoiced'):
            focus = 'milestones' if item.status == 'due' else 'invoiced'
            code = _site_code(r['project__site_code'], r['project__project_id'])
            links.append({'label': 'Finance work', 'kind': 'finance_work',
                          'href': f'{reverse("finance_work")}?'
                                  f'{urlencode({"focus": focus, "q": code})}'})
        links.append({'label': 'Site page', 'kind': 'site',
                      'href': reverse('project_detail', args=[r['project__project_id']])})
        return links
    if item.kind == 'payment':
        if r['contractor_bill'] is not None:
            # Finance reads a bill it is asked to pay (user_can_view_forwarded_bill), so
            # a bill-backed payment always links to its approval.
            return [{'label': 'Bill approval', 'kind': 'approval',
                     'href': reverse('approval_detail', args=[r['contractor_bill__request']])}]
        return [{'label': 'Order', 'kind': 'order',
                 'href': f'{reverse("vendor_order_detail", args=[r["vendor_order"]])}'
                         f'#payment-{item.pk}'}]
    # An unforwarded bill is not yet Finance's: the approval is a link only for a viewer
    # the approvals list admits (worked out once by the view); plain text otherwise.
    if viewer_can_open_approvals:
        return [{'label': 'Bill approval', 'kind': 'approval',
                 'href': reverse('approval_detail', args=[r['request']])}]
    return []


# ---------------------------------------------------------------------------
# Filters for the template
# ---------------------------------------------------------------------------

def _href(period, p, **changes):
    """The page's URL query with the list's parameters (defaults left out), the period
    kept, landing on #items."""
    merged = {**p, **changes}
    query = {'period': period} if period else {}
    for key in ('dir', 'status', 'type', 'q', 'sort', 'check'):
        value = merged.get(key)
        if value and not (key == 'sort' and value == DEFAULT_SORT):
            query[key] = value
    if merged.get('page', 1) > 1:
        query['page'] = merged['page']
    return f'?{urlencode(query)}#items' if query else '?#items'


def _filters(p, period, page, pages, check=None):
    return {
        'values': p,
        'period': period,
        'directions': [{'value': v, 'label': l, 'selected': v == p['dir']}
                       for v, l in DIRECTIONS.items()],
        'status_groups': [{'label': g, 'options': [{'value': v, 'label': l,
                                                    'selected': v == p['status']}
                                                   for v, l in opts]}
                          for g, opts in STATUS_GROUPS],
        'types': [{'value': '', 'label': 'All', 'selected': not p['type']}]
                 + [{'value': t, 'label': TYPE_LABELS[t], 'selected': t == p['type']}
                    for t in TYPES],
        'sorts': [{'value': v, 'label': l, 'selected': v == p['sort']}
                  for v, l in SORTS.items()],
        'filtered': bool(p['dir'] or p['status'] or p['type'] or p['q']
                         or p['sort'] != DEFAULT_SORT or p['check']),
        'windowed': not p['status'] and not p['q'] and not p['check'],
        # FIN-H-B5: the chosen check, with a link that drops only the check.
        'check': ({'key': p['check'], 'label': check['label'],
                   'clear_href': _href(period, p, check='', page=1)}
                  if check is not None else None),
        'prev_href': _href(period, p, page=page - 1) if page > 1 else None,
        'next_href': _href(period, p, page=page + 1) if page < pages else None,
        'clear_href': _href(period, {}),
    }


# ---------------------------------------------------------------------------
# Public
# ---------------------------------------------------------------------------

def finance_head_items(params, viewer_can_open_approvals, today=None, checks=None,
                       receipts_on=False, viewer=None):
    """The "Every item" list for cleaned `params` (clean_item_params; 'period' may ride
    along and is kept in every link). SEVEN queries whatever the data size (module
    docstring). `viewer_can_open_approvals` decides whether an unforwarded bill links to
    its approval (R-B4-7). `today` defaults to the IST date (H22).

    Returns {count, page, pages, items, filters, merged}: `items` are the shown page's
    Item objects with links and history; `merged` is how many duplicate lines A4 took
    out of the shown page's history.

    `checks` (FIN-H-B5) is {key: check} from finance_head_checks.check_filters(); the
    ?check= value picks one (module docstring). No query: the check's items are ids.

    `receipts_on` (FIN-M-B5-8): CLIENT_RECEIPTS_LIVE as the view read it — EIGHT queries
    (module docstring). `viewer` is the request's user, asked only whether a receipt's
    page may be linked (permissions.user_can_view_receipt)."""
    p = {**clean_item_params({}), **params}
    check = (checks or {}).get(p['check'])
    if check is None:
        p['check'] = ''                     # unknown key: ignored
    today = today or timezone.localdate()
    window_start = period_start('30d', today)

    items = (_milestone_items(_milestone_rows(receipts_on), receipts_on)
             + _payment_items(_payment_rows()) + _bill_items(_bill_rows()))
    kept = _sort([i for i in items if _keep(i, p, window_start, check)], p['sort'])

    count = len(kept)
    pages = max(1, -(-count // PAGE_SIZE))
    page = min(p['page'], pages)            # R-B4-4: out of range -> the last page
    shown = kept[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]
    for item in shown:
        item.age = (today - item.since).days if item.is_open and item.since else None
        item.links = _links(item, viewer_can_open_approvals)
        if check is not None:
            item.check_note = check['items'].get(item.key, '')
    merged = _load_history(shown, receipts_on, viewer, today)

    return {'count': count, 'page': page, 'pages': pages, 'items': shown,
            'filters': _filters(p, params.get('period', ''), page, pages, check),
            'merged': merged}
