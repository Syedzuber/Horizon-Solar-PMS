"""
FIN-M-B8a — the Finance work page (/finance/work/): what Finance has to act on today, on a
phone or a desktop. Prototype: "Horizon Site Dashboards", tab Finance work (v6, 12 Nov).

READ ONLY. The page writes nothing and posts nothing. Every action is a link to a screen
that already exists and keeps its own gate:

    milestone to invoice / advance to confirm / receipt to record
        -> the project page's milestone card (#payment-milestones), where Finance's B3
           invoice form and Finance pencil live (milestone_invoice, update_milestone).
           FIN-M-RT: the link carries ?return=finance_work&rq=<this page's filters>, so
           a save there comes back here with the same filters (see finance_work_return)
    payment requests
        -> the payments queue tab for that project type and status (payment_views)
    goods receipts
        -> the delivery challan page, at that receipt's own anchor (#grn-000123)

SECTIONS, IN THE RULED ORDER (dashboard thread, 11-12 Nov)
    1. Milestones to act on    Residential, project not deleted, status not Draft or
                               Cancelled (ruling 3). M1 Pending = "confirm advance" (R4: no
                               invoice). M2/M3 Pending and due per R8; not-yet-due ones are a
                               number only.
    2. Invoiced, awaiting      Residential, not deleted, ANY status (ruling 3: money is
       payment                 owed whatever the site's state). Oldest invoice first.
    3. Payment requests        One button per project type (Residential / RESCO / CAPEX;
                               CAPEX only when the queue would show it). Counts, plus the 5
                               oldest awaiting approval or approved, each linking into the
                               queue. Not a second copy of the queue.
    4. Goods received,         By receipt date, imported receipts left out, every project
       last 7 days             type with a type tag (ruling 7). Damaged first.

"DUE" (R8) for M2/M3: the task before the send-invoice task is Done — "PO for B & C Class
Items" for M2, "Customer Handover" for M3 (TRIGGER_TASKS). A site with no such task, or
with it marked Not Applicable, counts as due and says so (ruling 4). Read with the
milestones in the same query, as correlated subqueries, so it is never a query per site.

QUERIES. A fixed number whatever the number of sites, milestones, payment requests or
receipts (tests_finance_work pins it): one for the milestones, four for the payment counts
(three per-type payment_counts() and the queue's CAPEX test), one for the payment rows, two
for the receipts and their lines. The lines are fetched by the same window filter rather
than prefetched, because Django skips a prefetch when there are no receipts and the count
would then depend on the data.

STATE LIVES IN THE URL: q (search), focus (one section) and ptype (payment type). Alpine
holds only whether a row is open. No fetch, no XHR.

AMOUNTS are whole rupees in Indian grouping (views._format_inr) everywhere on the page; a
blank amount reads "amount not set", never ₹0 (76 of 78 milestones have no agreed amount).
"""
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from urllib.parse import urlencode

from django.db.models import (BooleanField, CharField, Exists, OuterRef, Q, Subquery,
                              Value, Case, When)
from django.http import QueryDict
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_safe

from .decorators import _forbidden, login_required
from .models import (DCLineDamagePhoto, DeliveryChallanAttachment, GoodsReceipt,
                     GoodsReceiptLine, PaymentMilestone, PaymentRequest, Task, VendorOrder,
                     GOODS_RECEIPT_CORRECTION, GOODS_RECEIPT_IMPORTED, GOODS_RECEIPT_RECEIPT)
from .payments import payment_counts, payment_type_q
from .permissions import (milestone_card_mode, user_can_invoice_milestone,
                          user_can_see_contract_value, user_can_view_finance_work,
                          user_has_dc_read_role)
from .utils import INVOICE_DUE_TRIGGERS
# views.py imports this module only inside a function (FIN-M-B4's lazy import, at call
# time, when both modules are loaded), so this top-level import is not circular
# (dashboard_strips imports from views the same way). _format_inr is the one Indian
# grouping rule (whole rupees), already used by the CEO strips.
from .views import _format_inr

#: R8: the task whose Done makes an M2/M3 invoice fall due (the task before the
#: send-invoice task in the Residential template). Matched by name, as the B3 and B9a
#: task maps are. The table lives in utils as INVOICE_DUE_TRIGGERS (FIN-M-B4) so the
#: "Invoice due" alert reads the same one; this name is kept for the page and its tests.
TRIGGER_TASKS = INVOICE_DUE_TRIGGERS

#: Ruling 3: a Pending milestone on a Draft or Cancelled site is not Finance's to act on.
#: (A Draft Residential site has no milestones yet; Cancelled is excluded by the ruling.)
#: The "Invoice due" alert (FIN-M-B4) reads this same list, so the two agree.
NOT_ACTIONABLE_STATUSES = ('Draft', 'Cancelled')

#: How many payment requests the section lists per type (ruling 8); the rest are in the queue.
PAYMENT_ROWS = 5

#: The payment types, as the queue's tabs: (stored project_type, label). RESCO is the
#: business's word for the stored OPEX.
PAYMENT_TYPES = (('Residential', 'Residential'), ('OPEX', 'RESCO'), ('CAPEX', 'CAPEX'))
TYPE_LABELS = {'Residential': 'Residential', 'OPEX': 'RESCO', 'CAPEX': 'CAPEX'}

#: The goods-receipt window in days, today included (ruling 7: by receipt date).
RECEIPT_DAYS = 7

#: An invoice older than this many days counts in the tile's "30+ days" figure.
INVOICE_OLD_DAYS = 30

#: The sections, in the ruled order: focus key -> heading.
SECTIONS = {
    'milestones': 'Milestones to act on',
    'invoiced': 'Invoiced, awaiting payment',
    'payments': 'Payment requests',
    'receipts': 'Goods received, last 7 days',
}

SEARCH_MAX = 100

#: FIN-M-RT: the one value of the `return` marker that sends a milestone save back to
#: this page. A fixed name, never a URL: the redirect target is always this page.
RETURN_FINANCE_WORK = 'finance_work'

#: FIN-M-RT: `rq` is cut to this many characters before it is parsed. Enough for the
#: longest real value (a 100-character search of 3-byte characters, percent-encoded, is
#: 900, plus focus and ptype), and short enough that it can never hold the 1000 fields
#: that would make QueryDict raise after the milestone has already been saved.
RQ_MAX = 1200


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def rupees(value):
    """'₹1,35,000' in Indian grouping, whole rupees; None stays None so the template says
    "amount not set" (never ₹0). A negative figure keeps its sign in front, because
    _format_inr groups a negative wrongly ('-,50,000')."""
    if value is None:
        return None
    value = Decimal(value)
    sign = '−' if value < 0 else ''
    return f'{sign}₹{_format_inr(abs(value))}'


def _short_date(value):
    """'12 Nov' — portable (strftime's %-d is not on Windows)."""
    return f'{value.day} {value:%b}' if value else None


def _days(today, value):
    return (today - value).days if value else None


def _tone(days, warn, crit):
    """The waiting pill's colour: crit at `crit` days or more, warn at `warn`, else muted."""
    if days is None:
        return 'mut'
    if days >= crit:
        return 'crit'
    if days >= warn:
        return 'warn'
    return 'mut'


# ---------------------------------------------------------------------------
# Milestones
# ---------------------------------------------------------------------------

@dataclass
class MilestoneRow:
    milestone: object
    project: object
    code: str
    kind: str                 # 'advance' (M1), 'invoice' (M2/M3 due) or 'invoiced'
    since: object = None      # the date the wait is counted from
    waiting: object = None    # days since `since`
    tone: str = 'mut'
    note: str = ''            # 'trigger N/A' / 'no trigger task' (ruling 4)
    trigger: str = ''         # the R8 trigger task's name, M2/M3 only
    agreed: object = None     # BD's amount, formatted; None = amount not set
    invoiced: object = None   # invoice amount, formatted; None = amount not set
    received: object = None   # amount received so far, formatted; None = nothing
    due: object = None        # invoiced - received, formatted; None = amount not set
    due_value: object = None  # the same as a Decimal, for the tile's sum
    contract: object = None   # contract value, formatted, R12 viewers only
    contract_shown: bool = False
    action: str = ''          # the link's words for this viewer
    href: str = ''


def _milestone_queryset():
    """Every Residential milestone Finance may still have to act on, with its site and the
    site's PM in the same query, and its R8 trigger task's state read as three correlated
    subqueries (status, Not Applicable flag, completion time). ONE query for any number
    of sites.

    Two halves, as ruling 3 puts them: Pending on a site that is not Draft or Cancelled
    (to act on), and Invoiced on any site (money owed whatever the site's state). Both
    leave soft-deleted sites out (R3) and anything not Residential (R2). Received
    milestones are done and are not fetched.

    trigger_name is annotated first so the subqueries can match the task by OuterRef to
    it; M1 gets '' and so matches no task (it has no trigger, R4). order_by('pk') makes
    [:1] deterministic should a site ever carry two tasks of that name.
    """
    trigger_name = Case(
        *[When(milestone_name=name, then=Value(task)) for name, task in TRIGGER_TASKS.items()],
        default=Value(''), output_field=CharField())
    trigger = (Task.objects
               .filter(phase__project=OuterRef('project'), task_name=OuterRef('trigger_name'))
               .order_by('pk'))
    return (PaymentMilestone.objects
            .filter(project__project_type='Residential', project__is_deleted=False)
            .filter((Q(status=PaymentMilestone.PENDING)
                     & ~Q(project__status__in=NOT_ACTIONABLE_STATUSES))
                    | Q(status=PaymentMilestone.INVOICED))
            .select_related('project', 'project__assigned_pm__user')
            .annotate(trigger_name=trigger_name)
            .annotate(trigger_status=Subquery(trigger.values('status')[:1]),
                      trigger_na=Subquery(trigger.values('is_not_applicable')[:1],
                                          output_field=BooleanField()),
                      trigger_done_at=Subquery(trigger.values('completed_at')[:1]))
            .order_by('project__project_id', 'milestone_name'))


def _site_code(project):
    return project.site_code or project.project_id


def _milestone_rows(user, today, params):
    """(to_act, invoiced, not_due) from the one milestone query.

    to_act    M1 Pending (kind 'advance', waiting since the site's activation) and M2/M3
              Pending that are due (kind 'invoice', waiting since the trigger was Done)
    invoiced  Invoiced milestones (kind 'invoiced'), waiting since the invoice date
    not_due   how many M2/M3 Pending are not yet due — a number only

    `params` are this page's filters; every row's link carries them in the FIN-M-RT
    return marker ("Open site" rows too, by ruling), so a save on the card returns here.
    """
    # The marker is the same for every row; built once, not once per site.
    back = '?' + return_query(return_marker(params))
    site_page = {}
    to_act, invoiced = [], []
    not_due = 0
    for m in _milestone_queryset():
        project = m.project
        if project.project_id not in site_page:
            site_page[project.project_id] = (
                reverse('project_overview', args=[project.project_id]) + back
                + '#payment-milestones')
        # R12: the contract value only for the roles that may see it; every role this page
        # admits is one of them today, but the rule is asked, not assumed. No query.
        contract_shown = user_can_see_contract_value(user, project)
        row = MilestoneRow(
            milestone=m, project=project, code=_site_code(project), kind='',
            agreed=rupees(m.amount),
            contract=rupees(project.contract_value) if contract_shown else None,
            contract_shown=contract_shown, href=site_page[project.project_id])

        if m.status == PaymentMilestone.INVOICED:
            row.kind = 'invoiced'
            row.since = m.invoice_date
            row.invoiced = rupees(m.invoice_amount)
            row.received = rupees(m.amount_received)
            # Ruling 5: no invoice amount (an invoice recorded before B3) reads "amount
            # not set"; the agreed amount is never used in its place.
            if m.invoice_amount is not None:
                row.due_value = m.invoice_amount - (m.amount_received or Decimal('0'))
                row.due = rupees(row.due_value)
            row.waiting = _days(today, row.since)
            # The prototype's ageing: red past INVOICE_OLD_DAYS (the tile's figure), amber
            # past half of it.
            row.tone = ('crit' if (row.waiting or 0) > INVOICE_OLD_DAYS
                        else 'warn' if (row.waiting or 0) > INVOICE_OLD_DAYS // 2 else 'mut')
            # Finance records the receipt with the card's Finance pencil; milestone_card_mode
            # is asked with can_manage=False because only its Finance answer matters here
            # (a reader gets 'read' or None and is offered the site page). No query.
            row.action = ('Record receipt on the site page'
                          if milestone_card_mode(user, project, can_manage=False) == 'finance'
                          else 'Open site')
            invoiced.append(row)
            continue

        if m.milestone_name == PaymentMilestone.M1:
            # R4: the token advance is confirmed, never invoiced. It is owed from the day
            # the site was activated (activation creates the milestones).
            row.kind = 'advance'
            row.since = timezone.localdate(project.activated_at) if project.activated_at else None
            action = 'Confirm advance on the site page'
        else:
            row.trigger = TRIGGER_TASKS[m.milestone_name]
            # R8 with ruling 4: due when the trigger task is Done; a site with no trigger
            # task, or with it Not Applicable, is due too and says why (no date to count
            # from). Anything else is not yet due and is only counted.
            if m.trigger_status is None:
                row.note = 'no trigger task'
            elif m.trigger_status == Task.DONE:
                row.since = (timezone.localdate(m.trigger_done_at)
                             if m.trigger_done_at else None)
            elif m.trigger_na:
                row.note = 'trigger N/A'
            else:
                not_due += 1
                continue
            row.kind = 'invoice'
            action = f'Invoice {m.milestone_name} on the site page'
        row.waiting = _days(today, row.since)
        row.tone = _tone(row.waiting, 7, 14)
        # Finance only (ruling (a)): the invoice form and the receipt pencil are Finance's;
        # a reader is offered the site page instead of a verb they cannot carry out.
        row.action = action if user_can_invoice_milestone(user, project) else 'Open site'
        to_act.append(row)

    # Rows with no date to count from (no trigger task, trigger N/A, a site never
    # activated) come FIRST with a "check" pill (Zuber, B8a review): nothing says how long
    # they have waited, so Finance looks at them before the dated ones. Then longest wait.
    to_act.sort(key=lambda r: (r.since is not None, r.since or today, r.code,
                               r.milestone.milestone_name))
    # Oldest invoice first (prototype); an invoice with no date last.
    invoiced.sort(key=lambda r: (r.since is None, r.since or today, r.code, r.milestone.milestone_name))
    return to_act, invoiced, not_due


# ---------------------------------------------------------------------------
# Payment requests
# ---------------------------------------------------------------------------

def _capex_shown():
    """Whether the payments queue draws its CAPEX tab: a CAPEX order exists, or a payment
    on a CAPEX site's contractor bill. ONE query. The same test as
    payment_views.payment_queue (A5, 5a ruling Q1), so this page offers CAPEX exactly when
    the queue it links into has the tab.
    TODO: copied, not shared — payment_queue builds it inline; move both to one helper in
    payments.py when that view is next touched (out of B8a's scope)."""
    return (VendorOrder.objects.filter(project_type='CAPEX').values('pk')
            .union(PaymentRequest.objects
                   .filter(contractor_bill__project__project_type='CAPEX')
                   .values('pk'))).exists()


@dataclass
class PaymentRow:
    payment: object
    reference: str
    vendor: str
    amount: str
    requested: object      # what was asked, formatted, when an approval was for less
    status_label: str
    tone: str
    raised: str
    raised_by: str
    waiting: int
    href: str


def _payment_section(params, today):
    """The payment section: counts per type (one payment_counts() each, the function the
    queue's own tabs count with, so the two cannot disagree), the type buttons, and the
    PAYMENT_ROWS oldest requests of the chosen type awaiting approval or approved to pay.

    Five queries whatever the number of requests: three counts (all three types always,
    so the count does not move with the data), the CAPEX test and the rows."""
    counts = {value: payment_counts([value]) for value, _ in PAYMENT_TYPES}
    shown = [(value, label) for value, label in PAYMENT_TYPES
             if value != 'CAPEX' or _capex_shown()]
    shown_values = [value for value, _ in shown]
    ptype = params['ptype'] if params['ptype'] in shown_values else shown_values[0]

    queue = reverse('payment_queue')
    awaiting, to_pay = PaymentRequest.PENDING_APPROVAL, PaymentRequest.APPROVED
    buttons = [{
        'value': value, 'label': label, 'active': value == ptype,
        'open': counts[value][awaiting].count + counts[value][to_pay].count,
        'href': _query(params, ptype=value),
    } for value, label in shown]

    # The oldest open requests of this type: awaiting approval and approved-to-pay, by
    # raise date. The type is the order's or the bill's site's (payment_type_q), never the
    # nullable PaymentRequest.project anchor. Joins carry everything the row prints.
    rows = []
    for p in (PaymentRequest.objects
              .filter(payment_type_q([ptype]), status__in=[awaiting, to_pay])
              .select_related('vendor', 'vendor_order', 'contractor_bill', 'requested_by')
              .order_by('requested_date', 'pk')[:PAYMENT_ROWS]):
        if p.contractor_bill_id is not None:
            reference = f'Bill {p.contractor_bill.bill_number}'
        else:
            order = p.vendor_order
            numbers = [n for n in (order.po_number, order.pi_number) if n]
            reference = ' · '.join(numbers) if numbers else f'Order #{order.pk}'
        effective = p.effective_amount
        requester = p.requested_by
        rows.append(PaymentRow(
            payment=p, reference=reference,
            vendor=p.vendor.name if p.vendor else '—',
            amount=rupees(effective),
            requested=rupees(p.amount) if effective != p.amount else None,
            status_label=p.get_status_display(),
            tone='warn' if p.status == awaiting else 'info',
            raised=_short_date(timezone.localdate(p.requested_date)),
            raised_by=(requester.get_full_name() or requester.username) if requester else '—',
            waiting=(today - timezone.localdate(p.requested_date)).days,
            href=f'{queue}?{urlencode({"tab": ptype, "status": p.status})}'))

    def total(status):
        return (sum(counts[v][status].count for v, _ in PAYMENT_TYPES),
                sum((counts[v][status].amount for v, _ in PAYMENT_TYPES), Decimal('0')))

    awaiting_n, awaiting_sum = total(awaiting)
    to_pay_n, to_pay_sum = total(to_pay)
    return {
        'ptype': ptype,
        'ptype_label': TYPE_LABELS[ptype],
        'buttons': buttons,
        'rows': rows,
        'awaiting': counts[ptype][awaiting].count,
        'to_pay': counts[ptype][to_pay].count,
        'more': max(0, counts[ptype][awaiting].count + counts[ptype][to_pay].count - len(rows)),
        'queue_href': f'{queue}?{urlencode({"tab": ptype})}',
        'totals': {'awaiting': awaiting_n, 'awaiting_sum': rupees(awaiting_sum),
                   'to_pay': to_pay_n, 'to_pay_sum': rupees(to_pay_sum)},
    }


# ---------------------------------------------------------------------------
# Goods receipts
# ---------------------------------------------------------------------------

@dataclass
class ReceiptRow:
    receipt: object
    challan: object
    project: object
    code: str
    type_label: str
    state: str              # 'damaged', 'partial' or 'complete' (ruling 6)
    lines: list = field(default_factory=list)
    complete_lines: int = 0
    recorder: str = '—'
    who: str = ''           # 'Site engineer' or 'SCM correction'
    with_photo: bool = False
    href: object = None     # the DC page at this receipt; None for a role that cannot read DCs


def _receipt_window(today, prefix=''):
    """The receipts the section lists, as a filter both of its queries share: receipt date
    within the last RECEIPT_DAYS days (today included), not imported (ruling 7: the backfill
    stamps history in bulk), on a site that is not soft-deleted (R3). Every project type.
    `prefix` is 'receipt__' when filtering the receipts' lines."""
    start = today - timedelta(days=RECEIPT_DAYS - 1)
    return (Q(**{f'{prefix}receipt_date__gte': start, f'{prefix}receipt_date__lte': today,
                 f'{prefix}challan__project__is_deleted': False})
            & ~Q(**{f'{prefix}kind': GOODS_RECEIPT_IMPORTED}))


def _receipt_rows(today, dc_readable):
    """The goods-received section's rows. TWO queries for any number of receipts.

    1. The receipts, with challan, site, vendor and recorder joined, and two EXISTS for
       R14's "confirmed with photo": a live receipt document that is an image, or a damage
       photo recorded with this receipt.
    2. Their lines, fetched by the same window (receipt__ prefixed) rather than prefetched:
       a prefetch is skipped when there are no receipts, and the page's query count must
       not depend on the data.

    Ruling 6: damaged (a line's damaged count went up in this receipt) is red and sorted
    first; a partial delivery (a line's received total still below ordered) is a neutral
    "partial", never "short"; everything else is complete and plain.
    """
    receipts = list(
        GoodsReceipt.objects.filter(_receipt_window(today))
        .select_related('challan__project', 'challan__vendor', 'recorded_by__user')
        .annotate(
            has_image=Exists(DeliveryChallanAttachment.objects.filter(
                receipt=OuterRef('pk'), withdrawn_at__isnull=True, mime__startswith='image/')),
            has_damage_photo=Exists(DCLineDamagePhoto.objects.filter(receipt=OuterRef('pk'))))
        .order_by('-receipt_date', '-pk'))
    lines = {}
    for line in (GoodsReceiptLine.objects.filter(_receipt_window(today, 'receipt__'))
                 .order_by('receipt_id', 'pk')):
        lines.setdefault(line.receipt_id, []).append(line)

    rows = []
    for receipt in receipts:
        challan = receipt.challan
        project = challan.project
        row = ReceiptRow(receipt=receipt, challan=challan, project=project,
                         code=_site_code(project),
                         type_label=TYPE_LABELS.get(project.project_type, project.project_type),
                         state='complete')
        damaged = partial = False
        for line in lines.get(receipt.pk, []):
            line_damaged = line.damaged_after > (line.damaged_before or 0)
            line_partial = line.received_after < line.ordered_quantity
            damaged = damaged or line_damaged
            partial = partial or line_partial
            if not line_partial:
                row.complete_lines += 1
            row.lines.append({
                'item': line.item_description,
                'received': _qty(line.received_after), 'ordered': _qty(line.ordered_quantity),
                'unit': line.unit, 'damaged': line.damaged_after if line_damaged else 0,
                'partial': line_partial,
            })
        row.state = 'damaged' if damaged else ('partial' if partial else 'complete')
        recorder = receipt.recorded_by
        if recorder is not None:
            row.recorder = recorder.user.get_full_name() or recorder.user.username
        row.who = ('Site engineer' if receipt.kind == GOODS_RECEIPT_RECEIPT
                   else 'SCM correction' if receipt.kind == GOODS_RECEIPT_CORRECTION
                   else receipt.get_kind_display())
        # R14: the match Finance relies on is the site engineer's own confirmation with
        # an image. An SCM correction is shown, but never as that match.
        row.with_photo = (receipt.kind == GOODS_RECEIPT_RECEIPT
                          and (receipt.has_image or receipt.has_damage_photo))
        # The DC page admits Finance (read only) and Admin, not CEO or System Admin
        # (DC_DETAIL_ROLES): the link is drawn only where it would open.
        if dc_readable:
            row.href = (reverse('delivery_challan_detail', args=[project.project_id, challan.pk])
                        + '#' + (receipt.number or '').lower())
        rows.append(row)
    # Damaged first (ruling 6), then newest receipt first within each group.
    rows.sort(key=lambda r: r.state != 'damaged')
    return rows


def _qty(value):
    """150.00 -> '150', 2.50 -> '2.5'."""
    return f'{value.normalize():f}' if value is not None else '—'


# ---------------------------------------------------------------------------
# The request
# ---------------------------------------------------------------------------

def _clean_params(data):
    """The page's parameters read from `data` (a QueryDict). Anything unknown is dropped,
    so no parameter can raise. ptype is checked against the types actually shown later
    (_payment_section). Shared by the page's own GET and the FIN-M-RT return marker, so
    the filters a save returns to are cleaned by exactly the rule the page applies."""
    focus = data.get('focus', '')
    ptype = data.get('ptype', '')
    return {
        'q': data.get('q', '').strip()[:SEARCH_MAX],
        'focus': focus if focus in SECTIONS else '',
        'ptype': ptype if ptype in TYPE_LABELS else '',
    }


def _params(request):
    """The page's GET parameters (see _clean_params)."""
    return _clean_params(request.GET)


def _query(params, **changes):
    """The page's query string with `changes` applied; empty values left out."""
    merged = {**params, **changes}
    return '?' + urlencode({k: v for k, v in merged.items() if v})


# ---------------------------------------------------------------------------
# FIN-M-RT: returning here after a save on the milestone card
# ---------------------------------------------------------------------------

def return_marker(params):
    """The return marker for cleaned `params`: {'return': 'finance_work', 'rq': '<the
    page's query string, without the ?>'}. rq is '' when no filter is set."""
    return {'return': RETURN_FINANCE_WORK, 'rq': _query(params)[1:]}


def return_query(marker):
    """The marker as a query string (no ?), with an empty rq left out."""
    return urlencode({k: v for k, v in marker.items() if v})


def finance_work_return(data, user):
    """The cleaned return marker carried in `data` (request.POST or request.GET), or None.

    None unless `return` is exactly RETURN_FINANCE_WORK — a fixed name, so no URL from
    the request is ever redirected to — and `user` may open this page
    (permissions.user_can_view_finance_work: Finance, CEO, Admin, System Admin; anyone
    else would be sent to a 403, so they keep today's behaviour). rq is re-parsed and
    passed through _clean_params, so only q, focus and ptype survive, each checked as
    the page checks it; any other key in rq is dropped. Costs no query.
    """
    if data.get('return') != RETURN_FINANCE_WORK:
        return None
    if not user_can_view_finance_work(user):
        return None
    return return_marker(_clean_params(QueryDict(data.get('rq', '')[:RQ_MAX])))


def finance_work_url(marker):
    """This page's URL with the filters a cleaned `marker` carries."""
    return reverse('finance_work') + (f'?{marker["rq"]}' if marker['rq'] else '')


def _matches(q, *values):
    return not q or q in ' '.join(str(v) for v in values if v).lower()


def _milestone_hit(row, q):
    m, p = row.milestone, row.project
    return _matches(q, row.code, p.project_id, p.customer_name, p.city, m.milestone_name,
                    m.invoice_number)


def _receipt_hit(row, q):
    c = row.challan
    return _matches(q, row.receipt.number, c.dc_number, c.po_number,
                    c.vendor.name if c.vendor else '', row.code, row.project.project_id,
                    row.project.customer_name)


@login_required
@never_cache
@require_safe
def finance_work(request):
    """
    The Finance work page: milestones to act on, invoices awaiting payment, payment
    requests and goods received in the last 7 days, with tiles (FIN-M-B8a).
    Access: Finance, CEO, Admin and System Admin (permissions.user_can_view_finance_work);
    everyone else gets the 403 page. GET only (require_safe): it reads, never writes —
    every action is a link to an existing screen. never_cache, because Back from a site
    page must not show the queue as it was before Finance acted.
    """
    # Finance works here; CEO, Admin and System Admin read it. Everyone else is refused
    # with the same 403 every wrong-role page gives, so a probe learns nothing.
    if not user_can_view_finance_work(request.user):
        return _forbidden(request)

    today = timezone.localdate()
    params = _params(request)
    q = params['q'].lower()

    to_act, invoiced, not_due = _milestone_rows(request.user, today, params)
    payments = _payment_section(params, today)
    # Who may follow a receipt to its challan page: the DC page's own role rule. No query.
    receipts = _receipt_rows(today, user_has_dc_read_role(request.user))

    # Search cuts the milestone, invoice and receipt lists; the tiles are counted after the
    # cut, so a tile always equals the section it names. Payment rows are the queue's, so
    # the search is handed to the queue instead (its own search).
    to_act = [r for r in to_act if _milestone_hit(r, q)]
    invoiced = [r for r in invoiced if _milestone_hit(r, q)]
    receipts = [r for r in receipts if _receipt_hit(r, q)]

    advances = sum(1 for r in to_act if r.kind == 'advance')
    to_invoice = sum(1 for r in to_act if r.kind == 'invoice')
    due_sum = sum((r.due_value for r in invoiced if r.due_value is not None), Decimal('0'))
    due_unset = sum(1 for r in invoiced if r.due_value is None)
    old_invoices = sum(1 for r in invoiced if (r.waiting or 0) > INVOICE_OLD_DAYS)
    damaged = sum(1 for r in receipts if r.state == 'damaged')
    partial = sum(1 for r in receipts if r.state == 'partial')

    def tile(key, label, value, detail, tone=''):
        active = params['focus'] == key
        return {'key': key, 'label': label, 'value': value, 'detail': detail,
                'tone': tone if value else '', 'active': active,
                # A second click on the active tile shows every section again.
                'href': _query(params, focus='' if active else key)}

    invoiced_detail = f'{rupees(due_sum)} due'
    if due_unset:
        invoiced_detail += f' · {due_unset} amount not set'
    invoiced_detail += f' · {old_invoices} over {INVOICE_OLD_DAYS} days'
    totals = payments['totals']
    tiles = [
        tile('milestones', 'M1 advances to confirm', advances, 'Residential, no invoice', 'warn'),
        tile('milestones', 'M2/M3 due to invoice', to_invoice,
             f'{not_due} more not yet due', 'warn'),
        tile('invoiced', 'Invoiced, awaiting payment', len(invoiced), invoiced_detail,
             'crit' if old_invoices else ''),
        # Both payment tiles count every project type (Zuber, B8a review), unlike the
        # section below, which shows one type; the second line says so first.
        tile('payments', 'Payments awaiting approval', totals['awaiting'],
             f'All types · {totals["awaiting_sum"]}', 'warn'),
        tile('payments', 'Approved, to pay', totals['to_pay'],
             f'All types · {totals["to_pay_sum"]}', 'warn'),
        tile('receipts', 'Receipts with damage', damaged,
             f'last {RECEIPT_DAYS} days · {partial} partial', 'crit'),
    ]

    focus = params['focus']
    return render(request, 'projects/finance_work.html', {
        'params': params,
        'tiles': tiles,
        'show': {key: not focus or focus == key for key in SECTIONS},
        'sections': SECTIONS,
        'to_act': to_act,
        'not_due': not_due,
        'invoiced': invoiced,
        'payments': payments,
        'payment_search_href': (f'{reverse("payment_queue")}?'
                                f'{urlencode({"tab": payments["ptype"], "q": params["q"]})}'
                                if params['q'] else None),
        'receipts': receipts,
        'receipt_days': RECEIPT_DAYS,
        'clear_href': _query(params, q='', focus=''),
        'filtered': bool(params['q'] or focus),
        'today': today,
    })
