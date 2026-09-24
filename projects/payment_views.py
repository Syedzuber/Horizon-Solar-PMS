"""
O5 — the Finance payments queue: every vendor payment in one place, by project type, with
the approver's three actions and Finance's mark-paid on each row.

A separate module for the reason order_views.py is one: its own URL group and its own
screen. It WRITES NOTHING OF ITS OWN. Approve, hold and reject are the O4 views in
order_views.py, reached through payment_queue_action() so they return here; mark-paid is
payments.mark_payment_paid(), the one writer of APPROVED -> CONFIRMED.

THE PROJECT TYPE IS THE ORDER'S, NEVER THE ANCHOR'S. Each tab filters on
`vendor_order__project_type`. `PaymentRequest.project` is a nullable display anchor (O2d):
reading scope through it drops every site-less payment, which is exactly the defect
EXECUTION_MODULE_DEFERRED.md §25 recorded against the Finance dashboard. Nothing here
filters on a project's status or joins through `project` to decide what is listed — a
payment on a Draft OPEX site, or on no site at all, is still money somebody has to pay.

FLAT IN THE NUMBER OF ROWS. One grouped query carries every tab's counts and the tiles'
sums, one more the invoice-awaited count, then the page and its six prefetches. Fifty
rows cost what one row costs.
"""
from decimal import Decimal
from urllib.parse import urlencode

from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import (
    Count, DecimalField, F, OuterRef, Prefetch, Q, Subquery, Sum, Value,
)
from django.db.models.functions import Coalesce
from django.http import Http404, HttpResponseForbidden, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme

from .decorators import login_required
from .forms import check_typed_date
from .models import (
    PaymentRequest, PaymentRequestHold, VendorOrder, VendorOrderDocument,
    VendorOrderProgram, VendorOrderSite, VENDOR_ORDER_DOC_INVOICE, effective_amount_sum,
)
from .order_views import _order_money, payment_approve, payment_hold, payment_reject
from .payments import PaymentRefused, mark_payment_paid
from .permissions import (
    PAYMENT_MARK_PAID_ROLES, user_can_approve_payment, user_can_hold_payment,
    user_can_mark_paid, user_can_reject_payment, user_can_view_payment_queue,
    user_can_view_vendor_order,
)
from .supabase_storage import vendor_order_document_url

#: The three tabs, in order: (stored project_type, label). The label is the product's
#: word — "RESCO" for the stored `OPEX` (EXECUTION_MODULE_DEFERRED.md §12).
QUEUE_TABS = [('Residential', 'Residential'), ('OPEX', 'RESCO'), ('CAPEX', 'CAPEX')]

#: The status chips: (value, label). '' is every status.
QUEUE_STATUS_CHIPS = [
    ('',                              'All'),
    (PaymentRequest.PENDING_APPROVAL, 'Awaiting approval'),
    (PaymentRequest.APPROVED,         'To pay'),
    (PaymentRequest.ON_HOLD,          'On hold'),
    (PaymentRequest.CONFIRMED,        'Paid'),
    (PaymentRequest.REJECTED,         'Rejected'),
]

QUEUE_PAGE_SIZE = 50

# Short document labels for a row's links; the model's are for the order page.
_DOC_LABELS = {'po': 'PO', 'pi': 'PI', 'invoice': 'Invoice', 'other': 'Other'}

_MONEY = DecimalField(max_digits=14, decimal_places=2)


def _safe_queue_next(request):
    """The queue URL a form came from, if it is one — else None.

    Two tests, both needed: the URL must be on this host (never an open redirect), and it
    must be the queue itself, so a posted `next` cannot send an approver anywhere the
    queue would not have.
    """
    target = (request.POST.get('next') or request.GET.get('next') or '').strip()
    if not target:
        return None
    if not url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()},
                                           require_https=request.is_secure()):
        return None
    if not target.startswith(reverse('payment_queue')):
        return None
    return target


def _grouped_counts():
    """{(project_type, status): (count, amount)} over every payment. ONE query, joined
    through `vendor_order` (NOT NULL) and never through `project`. The amount is the sum
    of effective_amount (O4b): an approved tile totals what was approved."""
    rows = (PaymentRequest.objects
            .values('vendor_order__project_type', 'status')
            .annotate(n=Count('pk'), amount=effective_amount_sum())
            .order_by())
    return {(r['vendor_order__project_type'], r['status']): (r['n'], r['amount'] or Decimal('0'))
            for r in rows}


def _invoice_awaited_orders(project_type):
    """How many of this tab's orders have had more paid against them than invoiced —
    the same rule as _order_money()'s `invoice_awaited` (paid > invoiced, an invoice with
    no amount counting 0), expressed as one query with two correlated sums so a join
    cannot multiply either of them. Paid is at effective_amount, as there."""
    paid = (PaymentRequest.objects
            .filter(vendor_order=OuterRef('pk'), status=PaymentRequest.CONFIRMED)
            .values('vendor_order').annotate(s=effective_amount_sum()).values('s'))
    invoiced = (VendorOrderDocument.objects
                .filter(order=OuterRef('pk'), doc_type=VENDOR_ORDER_DOC_INVOICE)
                .values('order').annotate(s=Sum('invoice_amount')).values('s'))
    zero = Value(Decimal('0'), output_field=_MONEY)
    return (VendorOrder.objects.filter(project_type=project_type)
            .annotate(paid_sum=Coalesce(Subquery(paid, output_field=_MONEY), zero),
                      invoiced_sum=Coalesce(Subquery(invoiced, output_field=_MONEY), zero))
            .filter(paid_sum__gt=F('invoiced_sum'))
            .count())


def _search(queryset, text):
    """Narrow to payments whose vendor, PO number, PI number, tender or site code
    contains `text`.

    The tender arm has two halves: a tender the order NAMES (VendorOrderProgram — how a
    site-less order is found) and the tender of a site it was sized against. Matching is
    done in a pk subquery, so the multi-valued joins cannot repeat a row and the outer
    query needs no DISTINCT.
    """
    match = (Q(vendor__name__icontains=text)
             | Q(vendor_order__po_number__icontains=text)
             | Q(vendor_order__pi_number__icontains=text)
             | Q(vendor_order__programs__program__name__icontains=text)
             | Q(vendor_order__sites__project__program__name__icontains=text)
             | Q(vendor_order__sites__project__project_id__icontains=text))
    return queryset.filter(pk__in=PaymentRequest.objects.filter(match).values('pk'))


def _row(user, payment, today):
    """One queue row: the payment, its order's money, its latest hold, its documents,
    and which actions THIS viewer may take. The flags are the predicates the views
    enforce, asked here only to decide what to draw — each is re-checked under a lock."""
    order = payment.vendor_order
    payments = list(order.payments.all())
    documents = list(order.documents.all())
    money = _order_money(order, documents, payments)
    holds = list(payment.holds.all())          # ordered '-held_at'; [0] is the latest
    # The O4 views answer 403 to anyone who cannot read the order BEFORE they ask the
    # approver predicate, so a flag holder outside the portfolio roles (a PM approver,
    # say) must not be shown a button that view will refuse. Free for the portfolio
    # roles; reads the prefetched sites for anyone else.
    readable = user_can_view_vendor_order(user, order)
    return {
        'payment':   payment,
        'order':     order,
        'money':     money,
        'counted':   payment.status != PaymentRequest.REJECTED,
        'age_days':  (today - timezone.localdate(payment.requested_date)).days,
        'hold':      holds[0] if holds and payment.status in (
                         PaymentRequest.ON_HOLD, PaymentRequest.PENDING_APPROVAL) else None,
        'documents': [{'label': _DOC_LABELS.get(d.doc_type, d.doc_type),
                       'number': d.invoice_number, 'name': d.file_name,
                       'url': vendor_order_document_url(d)} for d in documents],
        'can_approve':   readable and user_can_approve_payment(user, payment),
        'can_hold':      readable and user_can_hold_payment(user, payment),
        'can_reject':    readable and user_can_reject_payment(user, payment),
        'can_mark_paid': user_can_mark_paid(user, payment),
    }


def _query_string(**params):
    return urlencode({k: v for k, v in params.items() if v})


@login_required
def payment_queue(request):
    """The Finance payments queue (O5). Portfolio-wide, per D-4 — see
    user_can_view_payment_queue()."""
    if not user_can_view_payment_queue(request.user):
        return HttpResponseForbidden()

    tab_values = [value for value, _ in QUEUE_TABS]
    tab = request.GET.get('tab', '')
    if tab not in tab_values:
        tab = tab_values[0]
    status = request.GET.get('status', '')
    if status not in [value for value, _ in QUEUE_STATUS_CHIPS]:
        status = ''
    q = request.GET.get('q', '').strip()[:100]

    counts = _grouped_counts()

    def cell(project_type, st):
        return counts.get((project_type, st), (0, Decimal('0')))

    # Every tab's label carries its own backlog, so one tab's work is visible from the
    # others — there is deliberately no combined tab to hide it in.
    tabs = [{
        'value':      value, 'label': label, 'active': value == tab,
        'to_approve': cell(value, PaymentRequest.PENDING_APPROVAL)[0],
        'to_pay':     cell(value, PaymentRequest.APPROVED)[0],
        'url':        '?' + _query_string(tab=value),
    } for value, label in QUEUE_TABS]

    tiles = {
        'awaiting': cell(tab, PaymentRequest.PENDING_APPROVAL),
        'to_pay':   cell(tab, PaymentRequest.APPROVED),
        'on_hold':  cell(tab, PaymentRequest.ON_HOLD),
        'invoice_awaited_orders': _invoice_awaited_orders(tab),
    }

    chips = [{'value': value, 'label': label, 'active': value == status,
              'url': '?' + _query_string(tab=tab, status=value, q=q)}
             for value, label in QUEUE_STATUS_CHIPS]

    queryset = PaymentRequest.objects.filter(vendor_order__project_type=tab)
    if status:
        queryset = queryset.filter(status=status)
    if q:
        queryset = _search(queryset, q)
    queryset = (queryset
                .select_related('vendor', 'vendor_order', 'requested_by',
                                'approved_by__user', 'confirmed_by')
                .prefetch_related(
                    Prefetch('vendor_order__documents',
                             queryset=VendorOrderDocument.objects.order_by('doc_type', 'pk')),
                    'vendor_order__payments',
                    Prefetch('vendor_order__sites',
                             queryset=VendorOrderSite.objects.select_related('project')),
                    Prefetch('vendor_order__programs',
                             queryset=VendorOrderProgram.objects.select_related('program')),
                    Prefetch('holds', queryset=PaymentRequestHold.objects.select_related(
                        'held_by__user', 'responded_by__user')),
                )
                .order_by('-requested_date', '-pk'))

    page = Paginator(queryset, QUEUE_PAGE_SIZE).get_page(request.GET.get('page'))
    today = timezone.localdate()

    return render(request, 'projects/payment_queue.html', {
        'tabs':      tabs,
        'tab':       tab,
        'tab_label': dict(QUEUE_TABS)[tab],
        'tiles':     tiles,
        'chips':     chips,
        'status':    status,
        'q':         q,
        'page':      page,
        'rows':      [_row(request.user, payment, today) for payment in page],
        'today':     today,
        # Every action form posts this back, so the action returns to exactly this view
        # of the queue — same tab, same chip, same search, same page.
        'here':      request.get_full_path(),
        'page_query': _query_string(tab=tab, status=status, q=q),
    })


# The approver actions the queue draws, each the UNCHANGED O4 view.
_QUEUE_ACTIONS = {
    'approve': payment_approve,
    'hold':    payment_hold,
    'reject':  payment_reject,
}


@login_required
def payment_queue_action(request, payment_pk, action):
    """Approve, hold or reject from the queue, and come back to it.

    THE O4 VIEW DOES ALL OF IT. This calls the view itself, with the same request — its
    POST check, its 403s, its lock, its re-check, its ledger row and its message are the
    O4 code, untouched. The one thing added is where the person lands afterwards: the O4
    views always redirect to the order, and when this was reached from the queue (a safe
    `next` naming the queue) that redirect's destination is swapped for the queue. A 403
    or any other non-redirect answer passes through as the view gave it.
    """
    view = _QUEUE_ACTIONS.get(action)
    if view is None:
        raise Http404
    response = view(request, payment_pk=payment_pk)
    back = _safe_queue_next(request)
    if back and isinstance(response, HttpResponseRedirect):
        response['Location'] = back
    return response


@login_required
def payment_mark_paid(request, payment_pk):
    """Finance marks an approved payment paid, from the queue: a date and a reference,
    both mandatory. payments.mark_payment_paid() decides; this parses and words it.

    Not Finance (or no profile) is 403 — a fact about who is asking. Everything that can
    change while the page is open — the status, and whether this person approved it — is
    the service's refusal, a message and a return to the queue, never a 500.
    """
    payment = get_object_or_404(
        PaymentRequest.objects.select_related('vendor', 'vendor_order'), pk=payment_pk)
    back = _safe_queue_next(request) or (
        f"{reverse('payment_queue')}?{_query_string(tab=payment.vendor_order.project_type)}")
    if request.method != 'POST':
        return redirect(back)
    profile = getattr(request.user, 'profile', None)
    if profile is None or profile.role not in PAYMENT_MARK_PAID_ROLES:
        return HttpResponseForbidden()

    raw_date = request.POST.get('payment_date', '').strip()
    if not raw_date:
        messages.error(request, 'A payment date is required. Nothing was changed.')
        return redirect(back)
    payment_date, error = check_typed_date(raw_date)
    if error:
        messages.error(request, error)
        return redirect(back)

    try:
        mark_payment_paid(payment, request.user, payment_date,
                          request.POST.get('payment_reference', ''))
    except PaymentRefused as exc:
        messages.error(request, str(exc))
        return redirect(back)

    vendor = payment.vendor.name if payment.vendor_id else 'vendor'
    messages.success(request, f'Payment of ₹{payment.effective_amount} to {vendor} marked paid.')
    return redirect(back)
