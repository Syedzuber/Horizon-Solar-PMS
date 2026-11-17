"""
Finance Head data layer (FIN-H-B2): every figure the Finance Head page shows, computed
read-only with a FIXED number of queries (six, whatever the number of sites, milestones
or payments). No page, URL or template here: those are FIN-H-B3.

PUBLIC: finance_head_summary(period='month', today=None). It returns plain dicts: Decimal
money, int counts, `date` dates. It takes no user, so it holds no role rule; whoever
draws the page decides who may see it.

PUBLIC SCOPE HELPERS (FIN-H-B4-0), so the Finance Head item list (FIN-H-B4) reads exactly
the rows these figures count, without copying a rule:
    live_residential_milestones()  milestones on live, real Residential sites
    scoped_payments()              payment requests in scope (H14, R3/H28, B2 answer 2)
    unforwarded_bills()            approved contractor bills with remainder > 0 (H19)
    payment_type_expr()            a payment's project type as an SQL expression
Each returns a queryset or expression and runs no query of its own. Change a scope rule
here and both the figures and the list follow it.

THE SIX QUERIES
    1. _booked             aggregate over live Residential projects
    2. _incoming           one row read of live Residential milestones, with the R8
                           trigger state (milestone_queries.annotate_trigger_done), summed
                           in Python so "due" is read in exactly Finance work's order
    3. _outgoing           PaymentRequest grouped by status x type x order/bill x age
                           bucket x paid-in-period; the open hold's held_at is a
                           correlated subquery inside it
    4. _bills_not_forwarded approved contractor bills with committed_total_subquery()
    5. _weekly (in)        TruncWeek over received milestones
    6. _weekly (out)       TruncWeek over confirmed payments, grouped by type
    by_type and net are worked out in Python from the above.

RULINGS IMPLEMENTED (Zuber, 16 Nov 2026)
    H7/H8  Split by project type: Residential, OPEX (shown as RESCO by the page), CAPEX.
           Money in exists for Residential only; for OPEX/CAPEX it is "not in PMS" and
           returned as None, never 0. Net only for Residential.
    H14    Test sites (Project.is_test) excluded everywhere; payments through
           payments.exclude_test_site_payments() plus the combined rule below.
    R3/H28 Soft-deleted projects excluded. A contractor bill's payment follows the bill's
           site. Combined with H14 (B2 answer 2): an ORDER's payment counts only while
           its order has at least one site that is both live and real. An order that
           names no site at all is kept (S2: no site link is not evidence of test data).
    H15/H27 Booked contract value = Residential projects in every activated status
           (Active, In Progress, Commissioned, On Hold). Draft is counted apart, with no
           value; Cancelled is not counted.
    H16    Money out = effective amount (Coalesce(approved_amount, amount)) of CONFIRMED
           payments, dated by payment_date.
    H17    Money in = amount_received of Received milestones, dated by received_date,
           which is the day the receipt was SAVED, so basis = 'recorded'.
    H19    Approved contractor bills not yet forwarded: remainder = bill amount minus
           committed total (every payment on it but REJECTED, at effective amount).
           Shown apart; never added to "to pay".
    H21    has_payment_data = any PaymentRequest in scope, any status. Figures are still
           computed when it is False; B3 decides what to show.
    H22    Every date boundary comes from timezone.localdate() (IST), never date.today().
    H24    Payment state by status: awaiting = PENDING_APPROVAL; on hold = ON_HOLD (a
           hold on an approved request included); to pay = APPROVED; paid = CONFIRMED;
           REJECTED is dead and counted nowhere. The open hold is read only for on-hold
           ageing (held_at). On-hold sums use the effective amount.
    H25    Due as on Finance work: an M2/M3 Pending whose trigger task is Done, MISSING
           or Not Applicable is due; M1 Pending is due (R4: M1 has no trigger).
    H26    Pending milestones on Draft or Cancelled sites are in neither not_due nor due.
    H29    "Oldest due" = the day it became due: the trigger's Done date for M2/M3, the
           site's activation date for M1. Due rows with no such date (missing or N/A
           trigger, M1 on a site with no activated_at) are counted in due.undated_count.
    H30    Receivables ageing has an 'unknown' bucket for Invoiced milestones with
           neither an invoice_date nor a trigger Done date.

Null amounts are COUNTED SEPARATELY and add 0 to every sum; they are never folded into
"with amount" coverage.

Not a views module: imports models, payments, milestone_queries and utils only — never
views or decorators — so any page or command can import it without a cycle. No writes,
no signals, no cache.
"""
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.db.models import (BooleanField, Case, CharField, Count, DateTimeField,
                              DecimalField, Exists, ExpressionWrapper, F, OuterRef, Q,
                              Subquery, Sum, Value, When)
from django.db.models.functions import Coalesce, TruncWeek
from django.utils import timezone

from .milestone_queries import annotate_trigger_done
from .models import (APPROVAL_APPROVED, APPROVAL_KIND_CONTRACTOR_BILL,
                     ContractorBillDetail, PaymentMilestone, PaymentRequest,
                     PaymentRequestHold, Project, Task, VendorOrderSite,
                     effective_amount_sum)
from .payments import (committed_total_subquery, exclude_test_site_payments,
                       payment_type_q)

RESIDENTIAL = 'Residential'
TYPES = ('Residential', 'OPEX', 'CAPEX')

# H15/H27: every status a site reaches once activated. Draft has not been activated;
# Cancelled is not booked work.
ACTIVATED_STATUSES = ('Active', 'In Progress', 'Commissioned', 'On Hold')
# H26: Pending milestones on these sites are nobody's to act on (as on Finance work).
NOT_ACTIONABLE_STATUSES = ('Draft', 'Cancelled')

PERIODS = ('month', '30d', 'fy')
BUCKETS = ('0-30', '31-60', '61-90', '90+', 'unknown')
WEEKS = 8

ZERO = Decimal('0')
MONEY = DecimalField(max_digits=14, decimal_places=2)


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------

def period_start(period, today):
    """First day of `period` ending `today` (inclusive). 'month' = the 1st of today's
    month; '30d' = today - 29 (thirty days counting today); 'fy' = 1 April of the
    current Indian financial year (April-March)."""
    if period == 'month':
        return today.replace(day=1)
    if period == '30d':
        return today - timedelta(days=29)
    if period == 'fy':
        return date(today.year if today.month >= 4 else today.year - 1, 4, 1)
    raise ValueError(f'Unknown period {period!r}; expected one of {PERIODS}')


def _bucket_for_age(days):
    """Age in whole days -> bucket name. A date in the future (negative age) is new, so
    it goes with 0-30 rather than into 'unknown'."""
    if days is None:
        return 'unknown'
    if days <= 30:
        return '0-30'
    if days <= 60:
        return '31-60'
    if days <= 90:
        return '61-90'
    return '90+'


def _ist_midnight(day):
    """The aware datetime at 00:00 IST on `day` (TIME_ZONE is Asia/Kolkata), so a
    timestamp compared against it is bucketed by its IST date, never its UTC date."""
    return timezone.make_aware(datetime.combine(day, time.min))


def _local(dt):
    return timezone.localdate(dt) if dt is not None else None


# ---------------------------------------------------------------------------
# Small accumulators
# ---------------------------------------------------------------------------

def _money_count():
    return {'count': 0, 'sum': ZERO}


def _by_type():
    return {t: _money_count() for t in TYPES}


def _buckets():
    return {b: _money_count() for b in BUCKETS}


def _add(slot, n, amount):
    slot['count'] += n
    slot['sum'] += amount or ZERO


# ---------------------------------------------------------------------------
# 1. Booked contract value
# ---------------------------------------------------------------------------

def _live_residential_projects():
    """Residential sites that are not soft-deleted (R3) and not test sites (H14)."""
    return Project.objects.filter(project_type=RESIDENTIAL, is_deleted=False, is_test=False)


def _booked():
    """H15/H27: contract value of every activated live Residential site; Draft counted
    apart. ONE query (aggregate)."""
    activated = Q(status__in=ACTIVATED_STATUSES)
    row = _live_residential_projects().aggregate(
        total=Coalesce(Sum('contract_value', filter=activated), Value(ZERO),
                       output_field=MONEY),
        count=Count('pk', filter=activated),
        null_value_count=Count('pk', filter=activated & Q(contract_value__isnull=True)),
        draft_count=Count('pk', filter=Q(status='Draft')),
    )
    return {'count': row['count'], 'sum': row['total'],
            'null_value_count': row['null_value_count'], 'draft_count': row['draft_count']}


# ---------------------------------------------------------------------------
# 2. Incoming (Residential milestones)
# ---------------------------------------------------------------------------

def live_residential_milestones():
    """Every PaymentMilestone, all statuses, on a Residential site that is not
    soft-deleted (R3) and not a test site (H14). The milestone scope of every money-in
    figure here and of the FIN-H-B4 item list. A queryset; no query until evaluated.
    No ordering is set beyond Meta's; callers that group or sum clear it."""
    return PaymentMilestone.objects.filter(project__project_type=RESIDENTIAL,
                                           project__is_deleted=False, project__is_test=False)


def _milestone_rows():
    """Every milestone on a live Residential site, all statuses, with the site's status
    and activation time and the R8 trigger state. ONE query: annotate_trigger_done adds
    three correlated subqueries, not queries. order_by() clears Meta.ordering, which
    this read does not need."""
    qs = annotate_trigger_done(live_residential_milestones())
    return qs.order_by().values(
        'milestone_name', 'status', 'amount', 'amount_received', 'invoice_amount',
        'invoice_date', 'received_date', 'project__status', 'project__activated_at',
        'trigger_status', 'trigger_na', 'trigger_done_at')


def due_state(row):
    """('due', since) or ('not_due', None) for one Pending milestone on an actionable
    site, read in Finance work's order (views_finance_work._milestone_rows, H25):
    M1 is due from activation; for M2/M3 a missing task is due, Done is due from its
    date (checked BEFORE Not Applicable, so a Done+N/A task keeps its date), N/A is due,
    anything else is not yet due. `since` None = due with no date to count from.

    `row` needs milestone_name, project__activated_at and the annotate_trigger_done
    values trigger_status, trigger_na and trigger_done_at. Public since FIN-H-B4-1: the
    B2 figures and the FIN-H-B4 item list both call it, so "due" is one rule, not two."""
    if row['milestone_name'] == PaymentMilestone.M1:
        return 'due', _local(row['project__activated_at'])
    if row['trigger_status'] is None:
        return 'due', None
    if row['trigger_status'] == Task.DONE:
        return 'due', _local(row['trigger_done_at'])
    if row['trigger_na']:
        return 'due', None
    return 'not_due', None


def _incoming(today, start):
    """coverage, not_due, due, invoiced, received_period, received_all_time and
    receivables_ageing from the one milestone read."""
    coverage = {'total': 0, 'with_amount': 0, 'without_amount': 0}
    not_due = {'count': 0, 'sum': ZERO, 'null_amount_count': 0}
    due = {'count': 0, 'sum': ZERO, 'null_amount_count': 0,
           'm1': _money_count(), 'm2_m3': _money_count(),
           'oldest_due_since': None, 'undated_count': 0}
    invoiced = {'count': 0, 'sum': ZERO, 'null_amount_count': 0}
    received_period = {'count': 0, 'sum': ZERO, 'null_amount_count': 0, 'basis': 'recorded'}
    received_all = {'count': 0, 'sum': ZERO, 'null_amount_count': 0, 'basis': 'recorded'}
    ageing = {'buckets': _buckets(),
              'basis': {'invoice_date': 0, 'trigger_date': 0, 'unknown': 0}}

    for row in _milestone_rows():
        amount = row['amount']
        coverage['total'] += 1
        coverage['with_amount' if amount is not None else 'without_amount'] += 1
        status = row['status']

        if status == PaymentMilestone.PENDING:
            if row['project__status'] in NOT_ACTIONABLE_STATUSES:
                continue                                             # H26
            state, since = due_state(row)
            target = due if state == 'due' else not_due
            target['count'] += 1
            target['sum'] += amount or ZERO
            target['null_amount_count'] += amount is None
            if state == 'due':
                _add(due['m1' if row['milestone_name'] == PaymentMilestone.M1 else 'm2_m3'],
                     1, amount)
                if since is None:
                    due['undated_count'] += 1
                elif due['oldest_due_since'] is None or since < due['oldest_due_since']:
                    due['oldest_due_since'] = since

        elif status == PaymentMilestone.INVOICED:
            # The invoice figure when Finance recorded one, else the agreed amount; a row
            # with neither adds 0 and is counted.
            value = row['invoice_amount'] if row['invoice_amount'] is not None else amount
            invoiced['count'] += 1
            invoiced['sum'] += value or ZERO
            invoiced['null_amount_count'] += value is None
            # Age from the invoice date; an invoice saved without one (before B3) falls
            # back to the trigger's Done date; neither -> 'unknown' (H30).
            if row['invoice_date'] is not None:
                base, basis = row['invoice_date'], 'invoice_date'
            elif row['trigger_status'] == Task.DONE and row['trigger_done_at'] is not None:
                base, basis = _local(row['trigger_done_at']), 'trigger_date'
            else:
                base, basis = None, 'unknown'
            ageing['basis'][basis] += 1
            _add(ageing['buckets'][_bucket_for_age((today - base).days if base else None)],
                 1, value)

        elif status == PaymentMilestone.RECEIVED:
            got = row['amount_received']
            received_all['count'] += 1
            received_all['sum'] += got or ZERO
            received_all['null_amount_count'] += got is None
            rd = row['received_date']
            if rd is not None and start <= rd <= today:
                received_period['count'] += 1
                received_period['sum'] += got or ZERO
                received_period['null_amount_count'] += got is None

    return {'coverage': coverage, 'not_due': not_due, 'due': due, 'invoiced': invoiced,
            'received_period': received_period, 'received_all_time': received_all,
            'receivables_ageing': ageing}


# ---------------------------------------------------------------------------
# 3. Outgoing (payment requests)
# ---------------------------------------------------------------------------

def scoped_payments():
    """PaymentRequest rows the Finance Head counts: Residential, OPEX or CAPEX by
    payments.payment_type_q() (the order's type, or the bill site's type), with test and
    soft-deleted sites taken out (H14, R3/H28).

    The shared test-site rule runs first (it also drops a bill's payment on a test
    site). Then the combined rule (B2 answer 2): an ORDER's payment is dropped unless
    the order has at least one site that is both live and real, which also covers an
    order whose sites are a deleted real site and a live test site. An order with no
    site at all is kept, as in S2. Exists, never a join through the sites: a join would
    repeat each request once per site and every sum would count it that many times.
    A bill's payment is dropped when the bill's one site is soft-deleted.
    """
    qs = exclude_test_site_payments(PaymentRequest.objects.filter(payment_type_q(TYPES)))
    order_sites = VendorOrderSite.objects.filter(order=OuterRef('vendor_order'))
    qs = qs.exclude(Exists(order_sites)
                    & ~Exists(order_sites.filter(project__is_deleted=False,
                                                 project__is_test=False)))
    return qs.exclude(contractor_bill__project__is_deleted=True)


def payment_type_expr():
    """The payment's project type as an SQL expression for annotate(), the same rule as
    payment_type_q(): the order's type, or the bill site's type. Exactly one side is set
    (payment_request_order_xor_bill), and both joins are many-to-one, so it never
    repeats a row. Public since FIN-H-B4-0 (the item list labels each payment's type)."""
    return Coalesce('vendor_order__project_type', 'contractor_bill__project__project_type',
                    output_field=CharField())


def _outgoing(today, start):
    """has_payment_data, awaiting, on_hold, to_pay, paid_period and payables_ageing from
    ONE grouped query.

    Each row is grouped by status, type, backing (order or bill), age bucket and whether
    a CONFIRMED payment falls in the period. The age is counted from requested_date
    (awaiting), the OPEN hold's held_at (on hold; at most one open hold per request, so
    the subquery yields one value), or approved_at (to pay; null before O4 -> 'unknown').
    Buckets compare against IST midnights, so a request raised at 01:00 IST is aged by
    its IST day."""
    open_hold_at = Subquery(
        PaymentRequestHold.objects.filter(payment_request=OuterRef('pk'),
                                          responded_at__isnull=True)
        .order_by().values('held_at')[:1],
        output_field=DateTimeField())
    b30, b60, b90 = (_ist_midnight(today - timedelta(days=d)) for d in (30, 60, 90))
    qs = (scoped_payments()
          .annotate(
              ptype=payment_type_expr(),
              backing=Case(When(contractor_bill__isnull=False, then=Value('bill')),
                           default=Value('order'), output_field=CharField()),
              age_from=Case(
                  When(status=PaymentRequest.PENDING_APPROVAL, then=F('requested_date')),
                  When(status=PaymentRequest.ON_HOLD, then=open_hold_at),
                  When(status=PaymentRequest.APPROVED, then=F('approved_at')),
                  default=Value(None), output_field=DateTimeField()),
              paid_in_period=Case(
                  When(status=PaymentRequest.CONFIRMED, payment_date__gte=start,
                       payment_date__lte=today, then=Value(True)),
                  default=Value(False), output_field=BooleanField()))
          .annotate(bucket=Case(
              # Paid and rejected payments are not aged.
              When(status__in=[PaymentRequest.CONFIRMED, PaymentRequest.REJECTED],
                   then=Value('')),
              When(age_from__isnull=True, then=Value('unknown')),
              When(age_from__gte=b30, then=Value('0-30')),
              When(age_from__gte=b60, then=Value('31-60')),
              When(age_from__gte=b90, then=Value('61-90')),
              default=Value('90+'), output_field=CharField()))
          # order_by() clears Meta.ordering (-requested_date) so it cannot join the GROUP
          # BY and split one group into a row per request.
          .order_by()
          .values('status', 'ptype', 'backing', 'bucket', 'paid_in_period')
          .annotate(n=Count('pk'), requested=Sum('amount'), effective=effective_amount_sum()))

    awaiting = {'count': 0, 'sum': ZERO, 'by_type': _by_type()}
    on_hold = {'count': 0, 'sum': ZERO, 'by_type': _by_type()}
    to_pay = {'count': 0, 'sum': ZERO, 'by_type': _by_type(),
              'by_backing': {'order': _money_count(), 'bill': _money_count()}}
    paid_period = {'count': 0, 'sum': ZERO, 'by_type': _by_type()}
    ageing = {'awaiting': _buckets(), 'on_hold': _buckets(), 'to_pay': _buckets(),
              'all': _buckets()}
    any_rows = False

    for row in qs:
        any_rows = True
        status, t, n = row['status'], row['ptype'], row['n']
        effective = row['effective'] or ZERO
        if status == PaymentRequest.PENDING_APPROVAL:
            # Awaiting is what was ASKED for: nothing has been approved yet.
            state, money = 'awaiting', row['requested'] or ZERO
            target = awaiting
        elif status == PaymentRequest.ON_HOLD:
            state, money, target = 'on_hold', effective, on_hold
        elif status == PaymentRequest.APPROVED:
            state, money, target = 'to_pay', effective, to_pay
            _add(to_pay['by_backing'][row['backing']], n, money)
        elif status == PaymentRequest.CONFIRMED:
            if row['paid_in_period']:
                _add(paid_period, n, effective)
                _add(paid_period['by_type'][t], n, effective)
            continue
        else:
            continue                                                  # REJECTED: dead
        _add(target, n, money)
        _add(target['by_type'][t], n, money)
        _add(ageing[state][row['bucket']], n, money)
        _add(ageing['all'][row['bucket']], n, money)

    return {'has_payment_data': any_rows, 'awaiting': awaiting, 'on_hold': on_hold,
            'to_pay': to_pay, 'paid_period': paid_period, 'payables_ageing': ageing}


# ---------------------------------------------------------------------------
# 4. Approved contractor bills not yet forwarded (H19)
# ---------------------------------------------------------------------------

def unforwarded_bills():
    """Approved contractor bills on live, real sites (R3, H14) of the three types whose
    amount is not yet fully covered by payment requests, each annotated `remainder` =
    amount - committed total (every payment on the bill but REJECTED, at effective
    amount; payments.committed_total_subquery, which mirrors models.committed_total).
    Only remainder > 0 (H19). One row per bill, not grouped; a queryset, no query until
    evaluated. The subquery is correlated per bill, not a query per bill."""
    return (ContractorBillDetail.objects
            .filter(request__kind=APPROVAL_KIND_CONTRACTOR_BILL,
                    request__status=APPROVAL_APPROVED,
                    project__project_type__in=TYPES,
                    project__is_deleted=False, project__is_test=False)
            .annotate(remainder=ExpressionWrapper(
                F('amount') - committed_total_subquery(), output_field=MONEY))
            .filter(remainder__gt=0))


def _bills_not_forwarded():
    """unforwarded_bills() grouped by the bill site's type: count and remainder sum.
    ONE query."""
    rows = (unforwarded_bills()
            .order_by()
            .values('project__project_type')
            .annotate(n=Count('pk'), s=Sum('remainder')))
    result = {'count': 0, 'sum': ZERO, 'by_type': _by_type()}
    for row in rows:
        _add(result, row['n'], row['s'])
        _add(result['by_type'][row['project__project_type']], row['n'], row['s'])
    return result


# ---------------------------------------------------------------------------
# 5-6. Weekly series
# ---------------------------------------------------------------------------

def _week_key(value):
    # TruncWeek over a DateField returns a date; guard against a datetime from a backend
    # that widens it.
    return value.date() if isinstance(value, datetime) else value


def _weekly(today):
    """The last WEEKS weeks, Monday-start, oldest first, the current week last (it runs
    to today). Both source fields are DateFields already in IST (received_date and
    payment_date are calendar days), so the weeks need no time-zone conversion. TWO
    queries: one per object type; empty weeks are zero-filled here."""
    this_monday = today - timedelta(days=today.weekday())
    starts = [this_monday - timedelta(weeks=i) for i in range(WEEKS - 1, -1, -1)]
    first = starts[0]

    money_in = {}
    for row in (live_residential_milestones()
                .filter(status=PaymentMilestone.RECEIVED,
                        received_date__gte=first, received_date__lte=today)
                .annotate(week=TruncWeek('received_date'))
                .order_by().values('week')
                .annotate(n=Count('pk'), s=Sum('amount_received'))):
        money_in[_week_key(row['week'])] = (row['n'], row['s'] or ZERO)

    money_out = {}
    for row in (scoped_payments()
                .filter(status=PaymentRequest.CONFIRMED,
                        payment_date__gte=first, payment_date__lte=today)
                .annotate(week=TruncWeek('payment_date'), ptype=payment_type_expr())
                .order_by().values('week', 'ptype')
                .annotate(n=Count('pk'), s=effective_amount_sum())):
        money_out[(_week_key(row['week']), row['ptype'])] = (row['n'], row['s'] or ZERO)

    weeks = []
    for start in starts:
        in_n, in_s = money_in.get(start, (0, ZERO))
        out_by_type = {}
        for t in TYPES:
            n, s = money_out.get((start, t), (0, ZERO))
            out_by_type[t] = {'count': n, 'sum': s}
        weeks.append({
            'week_start': start,
            'in_sum': in_s, 'in_count': in_n,
            'out_sum': sum((v['sum'] for v in out_by_type.values()), ZERO),
            'out_count': sum(v['count'] for v in out_by_type.values()),
            'out_by_type': out_by_type,
        })
    return weeks


# ---------------------------------------------------------------------------
# Public
# ---------------------------------------------------------------------------

def finance_head_summary(period='month', today=None):
    """Every Finance Head figure for `period` ('month', '30d' or 'fy') ending `today`
    (default timezone.localdate(), H22). SIX queries whatever the data size; see the
    module docstring for the rulings and the return shape."""
    today = today or timezone.localdate()
    start = period_start(period, today)

    result = {'period': period, 'period_start': start, 'period_end': today,
              'booked': _booked()}
    result.update(_incoming(today, start))
    result.update(_outgoing(today, start))
    result['bills_not_forwarded'] = _bills_not_forwarded()
    result['weekly'] = _weekly(today)

    # H7/H8: money in, "to collect" and net exist for Residential only; None elsewhere
    # means "not in PMS", which the page must not draw as 0.
    by_type = {}
    for t in TYPES:
        out_period = result['paid_period']['by_type'][t]['sum']
        residential = t == RESIDENTIAL
        in_period = result['received_period']['sum'] if residential else None
        by_type[t] = {
            'in_period': in_period,
            'out_period': out_period,
            'to_collect': (result['due']['sum'] + result['invoiced']['sum']
                           if residential else None),
            'to_pay': result['to_pay']['by_type'][t]['sum'],
            'awaiting_plus_hold': (result['awaiting']['by_type'][t]['sum']
                                   + result['on_hold']['by_type'][t]['sum']),
            'net': in_period - out_period if residential else None,
        }
    result['by_type'] = by_type
    return result
