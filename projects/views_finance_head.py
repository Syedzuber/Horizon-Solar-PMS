"""
FIN-H-B3 — the Finance Head page (/finance/head/): money in and money out at a glance,
for the Finance Head and the CEO. It draws finance_head_summary() (FIN-H-B2) and nothing
else: every figure on the page comes from that one call (six queries).

READ ONLY (H2). No form, no button that changes data. Every tile and panel row that has
work behind it links to the screen where that work is done, and that screen keeps its
own gate:

    due / invoiced milestones   -> Finance work, its milestone or invoiced section
    awaiting / on hold / to pay -> the payments queue, filtered to that status; the
                                   panel's per-type figures open that type's tab
    approved bills not sent     -> the approvals list (contractor bills, approved), drawn
                                   as a link only for a viewer that list admits

WHO (H12): permissions.user_can_view_finance_head_page — the Finance Head, CEO, Admin and
System Admin. Everyone else gets the same 403 as Finance work. The Finance Head lands
here (decorators.get_user_dashboard, H13).

WHAT IT SAYS, BY RULING (Zuber, 16 Nov 2026)
    H4   A sum that includes rows with no amount says so beside it ("amount not set on
         N"), so a total built from blanks never reads as a plain ₹0.
    H9   No net tile. Net is shown per project type, below the chart.
    H17  Money in is "recorded" in the period (the day the receipt was saved), never
         "received on".
    H19  Approved contractor bills not yet sent to Finance are shown apart, never in
         "to pay".
    H21  With no payment request in PMS at all, every money-out figure reads "No payment
         requests in PMS yet." instead of ₹0.
    H37  The Residential net is shown only when money out exists.
OPEX is the stored word; the page says RESCO, as the payments queue does.

AMOUNTS: tiles use a short Indian form (₹1.22 Cr, ₹19.84 L, ₹45,000 under a lakh) with
the full figure in the tile's title; everything else uses Finance work's rupees() (full
Indian grouping, whole rupees).

STATE LIVES IN THE URL: ?period=month|30d|fy. Alpine holds only which chart week is
picked. No fetch, no XHR.

FIN-H-B4 adds the "Every item" list at the bottom: finance_head_items.finance_head_items()
(seven more queries, fixed). Its filters (dir, status, type, q, sort, page) are GET
parameters checked by finance_head_items.clean_item_params; every filter and paging link
keeps ?period= and lands on #items. Alpine holds only which rows are expanded.
"""
from decimal import ROUND_HALF_UP, Decimal
from urllib.parse import urlencode

from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_safe

from .decorators import _forbidden, login_required
from .finance_head import BUCKETS, PERIODS, TYPES, finance_head_summary
from .finance_head_items import clean_item_params, finance_head_items
from .models import APPROVAL_APPROVED, APPROVAL_KIND_CONTRACTOR_BILL, PaymentRequest
from .permissions import user_can_view_approval_list, user_can_view_finance_head_page
# Finance work's rupees() is the page family's one full-figure format (views._format_inr
# underneath). views_finance_work imports views at module level, never this module, so
# this import is not circular.
from .views_finance_work import INVOICE_OLD_DAYS, TYPE_LABELS, rupees

#: The period switch: value -> label. The value is finance_head.PERIODS; anything else
#: in the URL falls back to the first.
PERIOD_LABELS = {'month': 'This month', '30d': 'Last 30 days', 'fy': 'FY to date'}
DEFAULT_PERIOD = 'month'

#: H21: the words for every money-out figure while PMS holds no payment request.
NO_PAYMENTS = 'No payment requests in PMS yet.'
#: H37: in place of the Residential net while money out is not recorded.
NO_MONEY_OUT = 'Money out is not recorded in PMS yet.'
#: H8: money in, to collect and net for RESCO / CAPEX (None in the data).
NOT_IN_PMS = 'Not in PMS yet'
NOT_COMPARABLE = 'Not comparable'

#: Ageing buckets beyond INVOICE_OLD_DAYS (B3 answer 2: the tile's "over 30 days").
OVER_30_BUCKETS = ('31-60', '61-90', '90+')
BUCKET_LABELS = {'0-30': '0–30 days', '31-60': '31–60', '61-90': '61–90', '90+': '90+',
                 'unknown': 'Unknown'}

LAKH = Decimal('100000')
CRORE = Decimal('10000000')
TWO_PLACES = Decimal('0.01')


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def _short_inr(value):
    """A tile's short Indian figure (B3 answer 6): '₹1.22 Cr', '₹19.84 L', and below one
    lakh the full figure ('₹45,000'). Two decimals, half up. None stays None.

    The unit is chosen on the ROUNDED figure, so ₹99,99,999.99 reads '₹1.00 Cr' rather
    than '₹100.00 L', and ₹99,999.60 reads '₹1.00 L' rather than '₹1,00,000'."""
    if value is None:
        return None
    value = Decimal(value)
    sign = '−' if value < 0 else ''
    whole = abs(value).quantize(Decimal('1'), rounding=ROUND_HALF_UP)
    if whole < LAKH:
        return rupees(value)
    lakhs = (abs(value) / LAKH).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
    if lakhs < 100:
        return f'{sign}₹{lakhs} L'
    crores = (abs(value) / CRORE).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
    return f'{sign}₹{crores} Cr'


def _day_month(value):
    """'9 Nov' — portable (strftime's %-d is not on Windows)."""
    return f'{value.day} {value:%b}'


def _range_label(start, end):
    """'1–16 Nov', '1 Apr–16 Nov', or with years when the two differ."""
    if start.year != end.year:
        return f'{_day_month(start)} {start.year}–{_day_month(end)} {end.year}'
    if start.month == end.month:
        return f'{start.day}–{_day_month(end)}'
    return f'{_day_month(start)}–{_day_month(end)}'


def _unset(n):
    """H4: the note beside a sum that includes n rows with no amount; '' when n is 0."""
    return f'amount not set on {n}' if n else ''


def _plural(n, word):
    return f'{n} {word}{"" if n == 1 else "s"}'


# ---------------------------------------------------------------------------
# Links
# ---------------------------------------------------------------------------

def _finance_work(focus):
    return f'{reverse("finance_work")}?{urlencode({"focus": focus})}'


def _queue(status, ptype=None):
    """The payments queue filtered to `status`; on `ptype`'s tab when given, else on the
    queue's default tab (Residential, B3 answer 5)."""
    params = {'tab': ptype} if ptype else {}
    params['status'] = status
    return f'{reverse("payment_queue")}?{urlencode(params)}'


def _by_type_links(slot, status):
    """The per-type figures of one outgoing row, each linking to its own queue tab.
    Residential and RESCO always; CAPEX only when it has a payment in this row — a CAPEX
    payment is what makes the queue draw its CAPEX tab, so the link never lands on a tab
    the queue does not show (B3 answer 5). No query."""
    parts = []
    for t in TYPES:
        figure = slot['by_type'][t]
        if t == 'CAPEX' and not figure['count']:
            continue
        parts.append({'label': TYPE_LABELS[t], 'amount': rupees(figure['sum']),
                      'count': figure['count'], 'href': _queue(status, t)})
    return parts


# ---------------------------------------------------------------------------
# Page parts
# ---------------------------------------------------------------------------

def _tile(key, label, value, detail, href=None, tone='', message=None):
    """One tile. `value` is a Decimal drawn short with the full figure as the title, or
    a ready string (coverage). `message` replaces the value (H21)."""
    if isinstance(value, Decimal):
        shown, title = _short_inr(value), rupees(value)
    else:
        shown, title = value, ''
    return {'key': key, 'label': label, 'value': shown, 'title': title,
            'detail': ' · '.join(d for d in detail if d), 'href': href, 'tone': tone,
            'message': message}


def _tiles(s, recorded_label):
    has_payments = s['has_payment_data']
    received, paid = s['received_period'], s['paid_period']
    invoiced, due = s['invoiced'], s['due']
    over_30 = sum(s['receivables_ageing']['buckets'][b]['count'] for b in OVER_30_BUCKETS)
    coverage = s['coverage']
    out_types = [f'{TYPE_LABELS[t]} {rupees(paid["by_type"][t]["sum"])}' for t in TYPES
                 if t != 'CAPEX' or paid['by_type'][t]['count']]
    return [
        _tile('in', 'Money in', received['sum'],
              [f'Recorded {recorded_label}', 'Residential', _unset(received['null_amount_count'])]),
        _tile('out', 'Money out', paid['sum'],
              [f'Paid {recorded_label}', ' · '.join(out_types)],
              message=None if has_payments else NO_PAYMENTS),
        _tile('collect', 'To collect · invoiced', invoiced['sum'],
              [_plural(invoiced['count'], 'invoice'),
               f'{over_30} over {INVOICE_OLD_DAYS} days',
               _unset(invoiced['null_amount_count'])],
              href=_finance_work('invoiced'), tone='crit' if over_30 else ''),
        _tile('due', 'To invoice / confirm', due['sum'],
              [f'{due["count"]} due', f'M1 {due["m1"]["count"]} · M2/M3 {due["m2_m3"]["count"]}',
               _unset(due['null_amount_count'])],
              href=_finance_work('milestones'), tone='warn' if due['count'] else ''),
        _tile('to_pay', 'To pay', s['to_pay']['sum'],
              [f'{s["to_pay"]["count"]} approved', 'all types'],
              href=_queue(PaymentRequest.APPROVED),
              tone='warn' if s['to_pay']['count'] else '',
              message=None if has_payments else NO_PAYMENTS),
        _tile('awaiting', 'Awaiting approval', s['awaiting']['sum'],
              [f'{s["awaiting"]["count"]} requests', f'{s["on_hold"]["count"]} on hold'],
              href=_queue(PaymentRequest.PENDING_APPROVAL),
              tone='warn' if s['awaiting']['count'] else '',
              message=None if has_payments else NO_PAYMENTS),
        _tile('coverage', 'Milestones with an amount',
              f'{coverage["with_amount"]} of {coverage["total"]}',
              ['Residential', f'{coverage["without_amount"]} without'
               if coverage['without_amount'] else '']),
    ]


def _ageing(buckets):
    """An ageing bar: one segment per bucket, width by amount, or by count when every
    amount is blank (so a bar of null-amount rows still shows its shape). Each segment
    keeps its label, amount and count as text (never colour alone)."""
    total_sum = sum((buckets[b]['sum'] for b in BUCKETS), Decimal('0'))
    total_n = sum(buckets[b]['count'] for b in BUCKETS)
    by_amount = total_sum > 0
    parts = []
    for i, b in enumerate(BUCKETS):
        slot = buckets[b]
        weight = slot['sum'] if by_amount else Decimal(slot['count'])
        whole = total_sum if by_amount else Decimal(total_n)
        parts.append({
            'key': b, 'label': BUCKET_LABELS[b], 'step': 'unk' if b == 'unknown' else i + 1,
            'amount': rupees(slot['sum']), 'count': slot['count'],
            'pct': f'{(weight * 100 / whole):.2f}' if whole else '0',
        })
    return {'parts': parts, 'count': total_n, 'empty': total_n == 0}


def _incoming(s, recorded_label):
    booked, not_due, due = s['booked'], s['not_due'], s['due']
    invoiced, received, all_time = s['invoiced'], s['received_period'], s['received_all_time']
    due_notes = [f'M1 {due["m1"]["count"]} · M2/M3 {due["m2_m3"]["count"]}']
    if due['oldest_due_since']:
        due_notes.append(f'oldest due since {_day_month(due["oldest_due_since"])}')
    if due['undated_count']:
        due_notes.append(f'{due["undated_count"]} with no due date')
    return [
        {'key': 'booked', 'label': 'Booked contract value', 'amount': rupees(booked['sum']),
         'count': _plural(booked['count'], 'activated site'),
         'notes': [f'+ {booked["draft_count"]} Draft' if booked['draft_count'] else '',
                   _unset(booked['null_value_count'])], 'href': None},
        {'key': 'not_due', 'label': 'Not yet due', 'amount': rupees(not_due['sum']),
         'count': _plural(not_due['count'], 'milestone'),
         'notes': [_unset(not_due['null_amount_count'])], 'href': None},
        {'key': 'due', 'label': 'Due, not invoiced / confirmed', 'amount': rupees(due['sum']),
         'count': _plural(due['count'], 'milestone'),
         'notes': due_notes + [_unset(due['null_amount_count'])],
         'href': _finance_work('milestones')},
        {'key': 'invoiced', 'label': 'Invoiced, awaiting payment',
         'amount': rupees(invoiced['sum']), 'count': _plural(invoiced['count'], 'invoice'),
         'notes': [_unset(invoiced['null_amount_count'])], 'href': _finance_work('invoiced')},
        {'key': 'received', 'label': f'Recorded {recorded_label}',
         'amount': rupees(received['sum']), 'count': _plural(received['count'], 'receipt'),
         'notes': [f'All time {rupees(all_time["sum"])} ({all_time["count"]})',
                   _unset(received['null_amount_count'])], 'href': None},
    ]


def _outgoing(s, recorded_label):
    awaiting, on_hold, to_pay, paid = s['awaiting'], s['on_hold'], s['to_pay'], s['paid_period']
    backing = to_pay['by_backing']
    return [
        {'key': 'awaiting', 'label': 'Awaiting approval', 'amount': rupees(awaiting['sum']),
         'count': _plural(awaiting['count'], 'request'), 'notes': [],
         'href': _queue(PaymentRequest.PENDING_APPROVAL),
         'types': _by_type_links(awaiting, PaymentRequest.PENDING_APPROVAL)},
        {'key': 'on_hold', 'label': 'On hold', 'amount': rupees(on_hold['sum']),
         'count': _plural(on_hold['count'], 'request'), 'notes': [],
         'href': _queue(PaymentRequest.ON_HOLD),
         'types': _by_type_links(on_hold, PaymentRequest.ON_HOLD)},
        {'key': 'to_pay', 'label': 'Approved, to pay', 'amount': rupees(to_pay['sum']),
         'count': _plural(to_pay['count'], 'request'),
         'notes': [f'Orders {rupees(backing["order"]["sum"])} ({backing["order"]["count"]})',
                   f'Contractor bills {rupees(backing["bill"]["sum"])} ({backing["bill"]["count"]})'],
         'href': _queue(PaymentRequest.APPROVED),
         'types': _by_type_links(to_pay, PaymentRequest.APPROVED)},
        {'key': 'paid', 'label': f'Paid {recorded_label}', 'amount': rupees(paid['sum']),
         'count': _plural(paid['count'], 'payment'), 'notes': [],
         'href': _queue(PaymentRequest.CONFIRMED),
         'types': _by_type_links(paid, PaymentRequest.CONFIRMED)},
    ]


def _weekly(s):
    """The chart: 8 weeks, side-by-side in/out bars as percentages of the largest figure
    (both series on one scale). Money out is left undrawn under H21."""
    has_payments = s['has_payment_data']
    weeks = s['weekly']
    peak = max([w['in_sum'] for w in weeks]
               + ([w['out_sum'] for w in weeks] if has_payments else []) + [Decimal('0')])

    def pct(v):
        return f'{(v * 100 / peak):.2f}' if peak else '0'

    rows = []
    for i, w in enumerate(weeks):
        label = _day_month(w['week_start'])
        money_in, money_out = rupees(w['in_sum']), rupees(w['out_sum'])
        rows.append({
            'i': i, 'label': label, 'in': money_in, 'in_pct': pct(w['in_sum']),
            'out': money_out if has_payments else None,
            'out_pct': pct(w['out_sum']) if has_payments else '0',
            'tip': (f'Week of {label} · In {money_in} · '
                    + (f'Out {money_out}' if has_payments else 'Out: none recorded')),
        })
    return rows


def _net_cards(s):
    """H9: one card per project type. None in the data (RESCO / CAPEX money in, to
    collect, net) reads "Not in PMS yet" / "Not comparable", never 0 (H8). H21: the
    money-out lines read NO_PAYMENTS while PMS has no payment request (B3 answer 7).
    H37: the Residential net only when money out exists."""
    has_payments = s['has_payment_data']
    collect_unset = s['due']['null_amount_count'] + s['invoiced']['null_amount_count']
    cards = []
    for t in TYPES:
        figures = s['by_type'][t]
        residential = figures['in_period'] is not None
        if not residential:
            net = NOT_COMPARABLE
        elif has_payments:
            net = rupees(figures['net'])
        else:
            net = NO_MONEY_OUT
        cards.append({
            'key': t, 'label': TYPE_LABELS[t],
            'in': rupees(figures['in_period']) if residential else NOT_IN_PMS,
            'out': rupees(figures['out_period']) if has_payments else NO_PAYMENTS,
            'net': net,
            # data-net is drawn only for a real figure, so a test can tell it from words.
            'net_is_figure': residential and has_payments,
            'net_negative': residential and has_payments and figures['net'] < 0,
            'collect': rupees(figures['to_collect']) if residential else NOT_IN_PMS,
            'collect_note': _unset(collect_unset) if residential else '',
            'pay': (f'{rupees(figures["to_pay"])} · {rupees(figures["awaiting_plus_hold"])}'
                    if has_payments else NO_PAYMENTS),
        })
    return cards


# ---------------------------------------------------------------------------
# The request
# ---------------------------------------------------------------------------

def _period(request):
    """?period=, checked against finance_head.PERIODS; missing or unknown -> month."""
    value = request.GET.get('period', '')
    return value if value in PERIODS else DEFAULT_PERIOD


@login_required
@never_cache
@require_safe
def finance_head(request):
    """
    The Finance Head page: money in and out for the chosen period, receivables and
    payables with ageing, an 8-week chart and net by project type (FIN-H-B3), then the
    "Every item" list with each item's history (FIN-H-B4).
    Access: the Finance Head, CEO, Admin and System Admin
    (permissions.user_can_view_finance_head_page, H12); everyone else gets the 403 page.
    GET only (require_safe): it reads, never writes (H2). never_cache, because Back from
    a queue must not show figures from before Finance acted.
    """
    # H12: the Head's page, and the CEO / Admin / System Admin who read every money
    # screen. Plain Finance (no flag) works from Finance work instead, and every other
    # role is refused with the same 403 as every wrong-role page, so a probe learns
    # nothing.
    if not user_can_view_finance_head_page(request.user):
        return _forbidden(request)

    period = _period(request)
    s = finance_head_summary(period)
    recorded_label = _range_label(s['period_start'], s['period_end'])

    # The approvals list admits SCM, PM, CEO, Admin, System Admin and Design Head
    # authority — not Finance — so the bills row is a link only for a viewer it admits
    # (B3 answer 4); for the Finance Head it is plain text. May cost one query (the
    # Design Head deputy check) for a role outside the list's roles; fixed, not per row.
    # FIN-H-B4: asked ONCE and reused by the item list (R-B4-7), so the list's bill links
    # follow the same rule as the bills row and cost no second check.
    can_open_approvals = user_can_view_approval_list(request.user)
    bills_href = None
    if can_open_approvals:
        bills_href = (f'{reverse("approval_list")}?'
                      f'{urlencode({"kind": APPROVAL_KIND_CONTRACTOR_BILL, "status": APPROVAL_APPROVED})}')

    # FIN-H-B4: the "Every item" list. Its GET parameters are cleaned (unknown values fall
    # back to defaults) and the period rides along so every list link keeps it.
    items = finance_head_items({**clean_item_params(request.GET), 'period': period},
                               can_open_approvals)

    bills = s['bills_not_forwarded']
    return render(request, 'projects/finance_head.html', {
        'period': period,
        'periods': [{'value': v, 'label': PERIOD_LABELS[v], 'active': v == period,
                     'href': f'?{urlencode({"period": v})}'} for v in PERIODS],
        'period_label': PERIOD_LABELS[period],
        'recorded_label': recorded_label,
        'has_payment_data': s['has_payment_data'],
        'no_payments': NO_PAYMENTS,
        'tiles': _tiles(s, recorded_label),
        'incoming': _incoming(s, recorded_label),
        'receivables': _ageing(s['receivables_ageing']['buckets']),
        'receivables_unset': _unset(s['invoiced']['null_amount_count']),
        'outgoing': _outgoing(s, recorded_label),
        'payables': _ageing(s['payables_ageing']['all']),
        # H19 row. B3 follow-up answer 2: drawn whenever there is an unforwarded bill, even
        # with no payment request in PMS — it comes from bill approvals, not payments, so
        # H21's empty message must not hide it. With no bill and no payment it stays inside
        # the empty message.
        'bills': {'amount': rupees(bills['sum']), 'count': _plural(bills['count'], 'bill'),
                  'n': bills['count'], 'href': bills_href},
        'weeks': _weekly(s),
        'cards': _net_cards(s),
        'items': items,
    })
