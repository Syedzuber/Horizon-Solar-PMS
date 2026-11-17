"""
Finance Head data-trust checks and "Who recorded what" (FIN-H-B5). Data only: no page,
URL or template here (views_finance_head draws both sections).

PUBLIC
    finance_head_checks(period='month', today=None)
        {'checks': [...], 'who': {...}} from FOUR queries, whatever the data size
    trust_checks(today=None)            the 'checks' part alone (same four queries)
    who_recorded(period, today=None)    the 'who' part alone (same four queries)
    check_filters(checks)               {key: check} for finance_head_items' ?check=

Each check is {key, label, why, count, extra, items}. `items` maps an item-list key
('milestone-<pk>' / 'payment-<pk>') to a note shown on that row ('' for none), so the
item list can open exactly the counted items without a query of its own. COUNT ==
ITEMS OPENED (B5 answer 4): every check counts only items the item list draws (a
Pending milestone on a Draft or Cancelled site is in neither, H26).

SCOPE: the page's own, imported from finance_head and never copied — live, real
Residential milestones (live_residential_milestones) and payment requests in scope
(scoped_payments). Test and soft-deleted sites are out (H14, H28, H31).

THE EIGHT CHECKS (rulings H18, H20, H60-H62; B5 answers 1-7), all-time:
    no_amount              milestone amount null
    received_no_amount     Received, amount_received null
    by_coordinator         Received; the recorder's role AT THE TIME was not in
                           permissions.MONEY_RECORDER_ROLES (Coordinator, PM, automatic)
    invoiced_no_number     Invoiced M2/M3 with a blank invoice number (M1 never has one)
    paid_no_vendor_invoice order-backed APPROVED / CONFIRMED payment whose order has no
                           VendorOrderDocument of doc_type 'invoice' (a bill's PDF is
                           its invoice, so bill payments are never counted)
    late_invoice           due M2/M3 (finance_head.due_state) whose trigger was Done
                           7+ days ago; due rows with no trigger date reported apart
    late_payment           APPROVED with approved_at 7+ days ago; approved_at null
                           reported apart
    changed_after_received an amount line for the milestone written MORE than
                           SAME_EVENT_SECONDS after it became Received (a PM save that
                           sets the amount and Received together writes its amount line
                           a few ms after the move: one act, not a later change)

WHO RECORDED THE RECEIPT. The latest StatusTransition to Received (actor and
actor_role_code, copied at write time). Older receipts (before the ledger, mid-September)
have none: the pre-ledger task sync wrote only "Milestone Mx auto-marked Received via
completion of Finance task '…' by Name (Role)" with no entity_id, matched by site + Mx
(finance_head_items.LEGACY_RECEIVED_RE). Its Role is the profile's role when the line
was written. Its Name is matched to a user only when exactly one active user has that
full name; otherwise the text name stands.

THE FOUR QUERIES
    1. milestones   every in-scope milestone with the R8 trigger state and the time,
                    actor and role of its latest move to Received and to Invoiced
                    (correlated subqueries, not queries)
    2. log lines    in-scope sites' older auto-mark lines and every amount-change line
                    (code milestone_amounts_set: the PM edit's entity-linked
                    "Mx: amount a → b" and BD's "Set milestone amounts: …", both forms)
    3. payments     every scoped payment request with "its order has an invoice
                    document" and the role of its latest approval and payment
    4. people       every profile, with "is an active Finance user", and the holds it
                    placed in the period (count and role) as subqueries

Not a views module: imports models, permissions, finance_head, finance_head_items and
milestone_queries only (none of them imports this module, so no cycle). No writes.
"""
from collections import defaultdict
from datetime import datetime, time, timedelta

from django.db.models import Count, Exists, IntegerField, OuterRef, Q, Subquery, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from .finance_head import (NOT_ACTIONABLE_STATUSES, due_state, live_residential_milestones,
                           period_start, scoped_payments)
from .finance_head_items import (AMOUNTS_NAME_RE, AMOUNTS_PREFIX, LEGACY_RECEIVED_RE,
                                 MILESTONE_ENTITY, SAME_EVENT_SECONDS)
from .milestone_queries import annotate_trigger_done
from .models import (ACTOR_ROLE_SYSTEM, SUBJECT_PAYMENT_MILESTONE, SUBJECT_PAYMENT_REQUEST,
                     VENDOR_ORDER_DOC_INVOICE, ActivityLog, PaymentMilestone,
                     PaymentRequest, PaymentRequestHold, StatusTransition, UserProfile,
                     VendorOrderDocument)
from .permissions import MONEY_RECORDER_ROLES, active_finance_users

#: Ruling: "late" means this many whole days or more.
LATE_DAYS = 7
#: The one action code an amount change is logged under (views._log_milestone_changes
#: for the PM edit, views.set_milestone_amounts for BD; migration 0121 stamped BD's
#: older lines with it).
AMOUNT_CODE = 'milestone_amounts_set'
#: Shown on items whose recorder is known only from the older log line.
OLDER_ROLE_NOTE = 'role as recorded in the older log'

#: (key, label, why) in the page's order (B5 answer 1: eight checks).
CHECKS = (
    ('no_amount', 'Milestone amount not set', 'These count as ₹0 in every total.'),
    ('received_no_amount', 'Received with no amount', 'Money in is understated.'),
    ('by_coordinator', 'Recorded outside Finance',
     'Finance did not record this client money; check each receipt against the bank.'),
    ('invoiced_no_number', 'Invoiced with no invoice number',
     "The invoice cannot be matched to the customer's payment."),
    ('paid_no_vendor_invoice', 'Payment approved or paid with no vendor invoice',
     'Money goes out with no vendor bill on file.'),
    ('late_invoice', f'Trigger done, not invoiced {LATE_DAYS}+ days',
     'The customer cannot pay what has not been invoiced.'),
    ('late_payment', f'Approved, not paid {LATE_DAYS}+ days',
     'The vendor is waiting on money already approved.'),
    ('changed_after_received', 'Amount changed after Received',
     'The agreed figure moved after the money came in.'),
)
CHECK_KEYS = tuple(key for key, _, _ in CHECKS)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _ist_midnight(day):
    """00:00 IST on `day` (TIME_ZONE is Asia/Kolkata), so a timestamp is placed in a
    period by its IST date, never its UTC date (H22)."""
    return timezone.make_aware(datetime.combine(day, time.min))


def _local(dt):
    return timezone.localdate(dt) if dt is not None else None


def _day(value):
    return f'{value.day} {value:%b %Y}'


def _full_name(first, last):
    return f'{first or ""} {last or ""}'.strip()


def _role_label(code):
    return 'System' if code == ACTOR_ROLE_SYSTEM else (code or '')


def _who(name, role):
    return f'{name} ({role})' if name and role else name or role or 'not recorded'


def _plural(n, word):
    return f'{n} {word}{"" if n == 1 else "s"}'


def _listed(m):
    """True when the item list draws this milestone: everything but a Pending milestone
    on a Draft or Cancelled site (H26), so a check's count equals the items it opens."""
    return not (m['status'] == PaymentMilestone.PENDING
                and m['project__status'] in NOT_ACTIONABLE_STATUSES)


# ---------------------------------------------------------------------------
# The four queries
# ---------------------------------------------------------------------------

def _latest_milestone_move(to_status, field, output_field=None):
    """The `field` of the milestone's newest transition to `to_status` (correlated;
    -pk breaks a tie on the same instant)."""
    return Subquery(
        StatusTransition.objects.filter(subject_type=SUBJECT_PAYMENT_MILESTONE,
                                        subject_id=OuterRef('pk'), to_status=to_status)
        .order_by('-occurred_at', '-pk').values(field)[:1],
        output_field=output_field)


def _milestone_rows():
    """Query 1. Every in-scope milestone, all statuses, light: the R8 trigger state
    (annotate_trigger_done, for late_invoice through due_state) and the time, actor and
    role of the latest move to Received (who recorded the receipt) and to Invoiced (who
    raised the invoice). Ruling B5-10: one invoice per milestone, its latest move.
    Every annotation is a correlated subquery, so this stays ONE query."""
    received, invoiced = PaymentMilestone.RECEIVED, PaymentMilestone.INVOICED
    return list(
        annotate_trigger_done(live_residential_milestones())
        .annotate(recv_at=_latest_milestone_move(received, 'occurred_at'),
                  recv_actor=_latest_milestone_move(received, 'actor', IntegerField()),
                  recv_role=_latest_milestone_move(received, 'actor_role_code'),
                  inv_at=_latest_milestone_move(invoiced, 'occurred_at'),
                  inv_actor=_latest_milestone_move(invoiced, 'actor', IntegerField()),
                  inv_role=_latest_milestone_move(invoiced, 'actor_role_code'))
        .order_by()
        .values('pk', 'milestone_name', 'status', 'amount', 'amount_received',
                'invoice_number', 'project', 'project__status', 'project__activated_at',
                'trigger_status', 'trigger_na', 'trigger_done_at', 'recv_at',
                'recv_actor', 'recv_role', 'inv_at', 'inv_actor', 'inv_role'))


def _log_rows():
    """Query 2. The log lines the checks read, oldest first:
      * lines with no entity_id on in-scope sites that open with "Milestone M" (the older
        auto-mark line) or BD's "Set milestone amounts:" prefix;
      * entity-linked amount lines (AMOUNT_CODE) on in-scope milestones.
    The fixed opening words keep every unrelated site line out. Scope is a subquery, not
    an id list, so the query does not grow with the data."""
    scope = live_residential_milestones().order_by()
    return list(
        ActivityLog.objects
        .filter((Q(entity_id__isnull=True, project_id__in=scope.values('project'))
                 & (Q(action__startswith='Milestone M') | Q(action__startswith=AMOUNTS_PREFIX)))
                | Q(entity_type=MILESTONE_ENTITY, action_code=AMOUNT_CODE,
                    entity_id__in=scope.values('pk')))
        .order_by('timestamp', 'pk')
        .values('entity_id', 'project', 'action', 'timestamp'))


def _latest_payment_role(to_status):
    """The actor_role_code of the payment's newest transition to `to_status`: the role
    its approver or payer held AT THE TIME (B5 answer 9). None for a request written
    without the ledger; the caller falls back to the current role."""
    return Subquery(
        StatusTransition.objects.filter(subject_type=SUBJECT_PAYMENT_REQUEST,
                                        subject_id=OuterRef('pk'), to_status=to_status)
        .order_by('-occurred_at', '-pk').values('actor_role_code')[:1])


def _payment_rows():
    """Query 3. Every payment request in scope (finance_head.scoped_payments), light,
    with whether its order holds a vendor invoice document (Exists: an order with two
    invoices must not repeat the row) and the role at the time of its latest approval
    and payment."""
    return list(
        scoped_payments()
        .annotate(has_vendor_invoice=Exists(VendorOrderDocument.objects.filter(
                      order=OuterRef('vendor_order'), doc_type=VENDOR_ORDER_DOC_INVOICE)),
                  approve_role=_latest_payment_role(PaymentRequest.APPROVED),
                  paid_role=_latest_payment_role(PaymentRequest.CONFIRMED))
        .order_by()
        .values('pk', 'status', 'amount', 'approved_amount', 'approved_at', 'approved_by',
                'confirmed_by', 'payment_date', 'vendor_order', 'contractor_bill',
                'has_vendor_invoice', 'approve_role', 'paid_role'))


def _people(start, today):
    """Query 4. Every profile (active or not: a past actor still needs a name), with:
      * is_finance  - in permissions.active_finance_users() (B5 answer P4), as an
                      Exists so no role string is written here;
      * held_n      - holds this profile placed in the period on scoped payments;
      * hold_role   - the role at the time of its latest hold in the period (from the
                      ON_HOLD transition), None when the ledger has none.
    One row per profile: the hold count is a grouped correlated subquery, never a join.
    The row count follows the number of staff, not the data."""
    lo, hi = _ist_midnight(start), _ist_midnight(today + timedelta(days=1))
    in_scope = scoped_payments().order_by().values('pk')
    holds = (PaymentRequestHold.objects
             .filter(held_by=OuterRef('pk'), held_at__gte=lo, held_at__lt=hi,
                     payment_request__in=in_scope)
             .order_by().values('held_by').annotate(n=Count('pk')).values('n')[:1])
    hold_role = (StatusTransition.objects
                 .filter(subject_type=SUBJECT_PAYMENT_REQUEST,
                         to_status=PaymentRequest.ON_HOLD, actor=OuterRef('pk'),
                         occurred_at__gte=lo, occurred_at__lt=hi, subject_id__in=in_scope)
                 .order_by('-occurred_at', '-pk').values('actor_role_code')[:1])
    return list(
        UserProfile.objects
        .annotate(is_finance=Exists(active_finance_users().filter(pk=OuterRef('pk'))),
                  held_n=Coalesce(Subquery(holds, output_field=IntegerField()), Value(0)),
                  hold_role=Subquery(hold_role))
        .order_by()
        .values('pk', 'user', 'role', 'is_active', 'is_finance_head', 'user__is_active',
                'user__first_name', 'user__last_name', 'user__username', 'is_finance',
                'held_n', 'hold_role'))


# ---------------------------------------------------------------------------
# Receipts and amount lines
# ---------------------------------------------------------------------------

def _read_logs(logs, milestones):
    """-> (older, amount_lines). older: {(project pk, Mx): (at, name, role)} from the
    older auto-mark lines (the newest wins; there is one per site + Mx). amount_lines:
    {milestone pk: [(at, text), ...]} oldest first, BD's line filed under every Mx it
    names."""
    by_site = {(m['project'], m['milestone_name']): m['pk'] for m in milestones}
    older, amount_lines = {}, defaultdict(list)
    for line in logs:
        text, at = line['action'], line['timestamp']
        if line['entity_id'] is not None:
            amount_lines[line['entity_id']].append((at, text))
            continue
        legacy = LEGACY_RECEIVED_RE.match(text)
        if legacy:
            older[(line['project'], legacy.group(1))] = (at, legacy.group(3),
                                                         legacy.group(4))
        elif text.startswith(AMOUNTS_PREFIX):
            for name in dict.fromkeys(AMOUNTS_NAME_RE.findall(text[len(AMOUNTS_PREFIX):])):
                pk = by_site.get((line['project'], name))
                if pk is not None:
                    amount_lines[pk].append((at, text))
    return older, amount_lines


def _receipt(m, older, names, active_by_name):
    """Who recorded this Received milestone and when, or None when nothing records it.
    The transition wins; an older line is used only where no transition exists."""
    if m['recv_at'] is not None:
        actor = m['recv_actor']
        return {'at': m['recv_at'], 'actor': actor, 'name': names.get(actor, ''),
                'role': m['recv_role'], 'older': False}
    line = older.get((m['project'], m['milestone_name']))
    if line is None:
        return None
    at, name, role = line
    # Matched to a user only when exactly one ACTIVE user has that full name (ruling);
    # otherwise the row keeps the text name.
    matches = active_by_name.get(name, [])
    return {'at': at, 'actor': matches[0] if len(matches) == 1 else None, 'name': name,
            'role': role, 'older': True}


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def _checks(milestones, receipts, amount_lines, payments, today):
    found = {key: {} for key in CHECK_KEYS}
    extra = defaultdict(str)
    older_outside = 0
    undated_invoice = 0
    undated_payment = 0

    for m in milestones:
        if not _listed(m):
            continue
        key = f'milestone-{m["pk"]}'
        status = m['status']
        if m['amount'] is None:
            found['no_amount'][key] = ''
        if status == PaymentMilestone.RECEIVED:
            if m['amount_received'] is None:
                found['received_no_amount'][key] = ''
            rec = receipts.get(m['pk'])
            if rec is not None and rec['role'] not in MONEY_RECORDER_ROLES:
                note = f'Recorded by {_who(rec["name"], _role_label(rec["role"]))}'
                if rec['older']:
                    note += f' — {OLDER_ROLE_NOTE}'
                    older_outside += 1
                found['by_coordinator'][key] = note
            if rec is not None:
                # B5 answer 6: a line within SAME_EVENT_SECONDS after the receipt is the
                # same save (amount and status together), not a later change.
                later = [(at, text) for at, text in amount_lines.get(m['pk'], [])
                         if (at - rec['at']).total_seconds() > SAME_EVENT_SECONDS]
                if later:
                    at, text = later[-1]
                    found['changed_after_received'][key] = \
                        f'Changed {_day(timezone.localtime(at))}: {text}'
        elif status == PaymentMilestone.INVOICED:
            if (m['milestone_name'] in (PaymentMilestone.M2, PaymentMilestone.M3)
                    and not (m['invoice_number'] or '').strip()):
                found['invoiced_no_number'][key] = ''
        elif m['milestone_name'] != PaymentMilestone.M1:
            # Pending M2/M3 on an actionable site: due by the B2 figures' own rule.
            state, since = due_state(m)
            if state == 'due':
                if since is None:
                    undated_invoice += 1
                elif (today - since).days >= LATE_DAYS:
                    found['late_invoice'][key] = \
                        f'Trigger done {_day(since)}, {(today - since).days} days ago'

    for p in payments:
        key = f'payment-{p["pk"]}'
        status = p['status']
        if (status in (PaymentRequest.APPROVED, PaymentRequest.CONFIRMED)
                and p['contractor_bill'] is None and not p['has_vendor_invoice']):
            found['paid_no_vendor_invoice'][key] = ''
        if status == PaymentRequest.APPROVED:
            approved = _local(p['approved_at'])
            if approved is None:
                undated_payment += 1
            elif (today - approved).days >= LATE_DAYS:
                found['late_payment'][key] = \
                    f'Approved {_day(approved)}, {(today - approved).days} days ago'

    if older_outside:
        extra['by_coordinator'] = (f'{older_outside} of these from older records — '
                                   f'{OLDER_ROLE_NOTE}')
    if undated_invoice:
        extra['late_invoice'] = f'{undated_invoice} due with no trigger date'
    if undated_payment:
        extra['late_payment'] = f'{_plural(undated_payment, "approved request")} ' \
                                f'with no approval date'

    return [{'key': key, 'label': label, 'why': why, 'count': len(found[key]),
             'extra': extra[key], 'items': found[key]} for key, label, why in CHECKS]


# ---------------------------------------------------------------------------
# Who recorded what
# ---------------------------------------------------------------------------

def _who_recorded(milestones, receipts, payments, people, start, today):
    """One row per active Finance user (always) and per anyone else with a recorded act
    in the period: a receipt, an invoice, an approval, a hold or a payment (B5 answer 9).
    Each act is filed under the person and the role they held when they did it."""
    by_pk = {p['pk']: p for p in people}
    by_user = {p['user']: p['pk'] for p in people}
    rows = {}

    def in_period(day):
        return day is not None and start <= day <= today

    def row(pk=None, text_name=''):
        key = ('p', pk) if pk is not None else ('t', text_name)
        if key not in rows:
            person = by_pk.get(pk)
            name = (_full_name(person['user__first_name'], person['user__last_name'])
                    or person['user__username']) if person else (text_name or 'Not recorded')
            rows[key] = {
                'key': f'p{pk}' if pk is not None else f't-{text_name}',
                'name': name, 'current_role': person['role'] if person else '',
                'head': bool(person and person['is_finance'] and person['is_finance_head']),
                'finance': bool(person and person['is_finance']),
                'roles': set(), 'older': False,
                'received_n': 0, 'received_sum': 0, 'received_unset': 0,
                'invoices_n': 0, 'approvals_n': 0, 'held_n': 0, 'paid_n': 0, 'paid_sum': 0,
            }
        return rows[key]

    for p in people:
        if p['is_finance']:
            row(p['pk'])

    for m in milestones:
        rec = receipts.get(m['pk']) if m['status'] == PaymentMilestone.RECEIVED else None
        if rec is not None and in_period(_local(rec['at'])):
            r = row(rec['actor'], rec['name'])
            r['received_n'] += 1
            r['roles'].add(_role_label(rec['role']))
            r['older'] = r['older'] or rec['older']
            if m['amount_received'] is None:
                r['received_unset'] += 1
            else:
                r['received_sum'] += m['amount_received']
        if in_period(_local(m['inv_at'])):
            r = row(m['inv_actor'])
            r['invoices_n'] += 1
            r['roles'].add(_role_label(m['inv_role']))

    for p in payments:
        if p['approved_by'] is not None and in_period(_local(p['approved_at'])):
            r = row(p['approved_by'])
            r['approvals_n'] += 1
            r['roles'].add(_role_label(p['approve_role'] or r['current_role']))
        if (p['status'] == PaymentRequest.CONFIRMED and p['confirmed_by'] is not None
                and in_period(p['payment_date'])):
            # confirmed_by is the auth user; the row is the profile.
            r = row(by_user.get(p['confirmed_by']))
            r['paid_n'] += 1
            r['paid_sum'] += (p['approved_amount'] if p['approved_amount'] is not None
                              else p['amount'])
            r['roles'].add(_role_label(p['paid_role'] or r['current_role']))

    for p in people:
        if p['held_n']:
            r = row(p['pk'])
            r['held_n'] += p['held_n']
            r['roles'].add(_role_label(p['hold_role'] or p['role']))

    result = []
    for r in rows.values():
        r['roles'] = sorted(r['roles']) or ([r['current_role']] if r['current_role'] else [])
        result.append(r)
    # Workload, not a ranking (ruling): alphabetical by name, never by volume.
    result.sort(key=lambda r: (r['name'].casefold(), r['key']))
    return result


# ---------------------------------------------------------------------------
# Public
# ---------------------------------------------------------------------------

def finance_head_checks(period='month', today=None):
    """The eight trust checks (all-time) and the who-recorded-what rows for `period`
    ('month', '30d' or 'fy', finance_head.period_start) ending `today` (default the IST
    date, H22). FOUR queries whatever the data size (module docstring).

    Returns {'checks': [...], 'who': {'rows', 'has_payment_data', 'period_start',
    'period_end'}}. has_payment_data is any scoped payment request, any status: the
    same rule as finance_head_summary's (H21)."""
    today = today or timezone.localdate()
    start = period_start(period, today)

    milestones = _milestone_rows()
    logs = _log_rows()
    payments = _payment_rows()
    people = _people(start, today)

    names = {p['pk']: (_full_name(p['user__first_name'], p['user__last_name'])
                       or p['user__username']) for p in people}
    active_by_name = defaultdict(list)
    for p in people:
        if p['is_active'] and p['user__is_active']:
            full = _full_name(p['user__first_name'], p['user__last_name'])
            if full:
                active_by_name[full].append(p['pk'])

    older, amount_lines = _read_logs(logs, milestones)
    receipts = {}
    for m in milestones:
        if m['status'] == PaymentMilestone.RECEIVED:
            rec = _receipt(m, older, names, active_by_name)
            if rec is not None:
                receipts[m['pk']] = rec

    return {
        'checks': _checks(milestones, receipts, amount_lines, payments, today),
        'who': {'rows': _who_recorded(milestones, receipts, payments, people, start, today),
                'has_payment_data': bool(payments),
                'period_start': start, 'period_end': today},
    }


def trust_checks(today=None):
    """The eight checks alone. Four queries; the page calls finance_head_checks once
    instead, so it never pays for both halves twice."""
    return finance_head_checks('month', today)['checks']


def who_recorded(period, today=None):
    """The who-recorded-what part alone for `period`. Four queries (as above)."""
    return finance_head_checks(period, today)['who']


def check_filters(checks):
    """{key: check} for finance_head_items(checks=...): the ?check= value picks one, and
    its `items` are exactly the rows the count counted. No query."""
    return {c['key']: c for c in checks}
