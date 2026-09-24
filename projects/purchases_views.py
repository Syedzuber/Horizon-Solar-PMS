"""
O8a — the purchases & payments workspace: one place SCM records a PO / PI and asks for
money against it, and one list of every PO / PI record for the roles that read them.

A separate module for the reason order_views.py and payment_views.py are: its own URL
group (purchases/). urls.py imports it beside them.

ADDITIVE. Nothing here replaces an existing door. vendor_order_create (Residential, one
site, priced lines), vendor_order_create_group (the tender row's Raise payment request),
the record page's Request payment and the site and tender order lists all work exactly as
before; O8b decides what, if anything, they become.

REUSE, NOT COPY. Every rule a submission here is held to is order_views' own function:

  * Add PO / PI parses with _parse_header, _parse_new_order_documents, _parse_order_total,
    _parse_payment_within, _parse_group_sites and _parse_requirement; writes through
    _write_order_record inside _save_order_submission's upload-then-write sequence; and
    draws sections 3 and 4 with _picker_context() and partials/vendor_order_picker*.html —
    the group raise's own picker, which gained Residential rows behind a flag and nothing
    else. Its optional payment is _create_order_payment(), the writer behind Request
    payment, called inside the same transaction as the record.
  * Raise payment request picks a record and hands it to _submit_order_payment(), the
    POST path of vendor_order_add_payment itself: same available-to-request rule under
    the same lock, same client_uuid idempotency, same PENDING_APPROVAL row and ledger row.

WHAT THIS MODULE ADDS: the list, the two pages that host those calls, and ONE rule of its
own — what a record is (Residential or RESCO), derived from what it is recorded against,
because it is the first page where both kinds can be recorded (_record_project_type).
"""
import uuid as _uuid

from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Prefetch, Q
from django.http import HttpResponseForbidden
from django.shortcuts import redirect, render
from django.urls import reverse

from .decorators import login_required
from .models import (
    PaymentRequest, Program, Project, Vendor, VendorOrder, VendorOrderDocument,
    VendorOrderProgram, VendorOrderSite, committed_total,
    VENDOR_ORDER_DOC_INVOICE, VENDOR_ORDER_DOC_PI, VENDOR_ORDER_DOC_PO,
)
from .order_views import (
    _DOC_TYPE_SELECT, _create_order_payment, _doc_rows, _log_order_recorded,
    _log_payment_raised, _order_money, _order_ref, _order_with_sites, _parse_client_uuid,
    _parse_group_sites, _parse_header, _parse_new_order_documents, _parse_order_total,
    _parse_payment_within, _parse_requirement, _picker_context, _picker_program,
    _posted_order_uuid, _save_order_submission, _submit_order_payment, _write_anchor,
    _write_order_record,
)
from .permissions import (
    user_can_raise_vendor_order, user_can_request_order_payment,
    user_can_use_purchases_workspace, user_can_view_purchases_workspace,
)

#: Records per page on the workspace list.
PAGE_SIZE = 50

#: The two kinds a record is recorded as here. VendorOrder.project_type stores the
#: Project vocabulary, in which RESCO is 'OPEX'. CAPEX has no purchase path yet.
RECORD_TYPES = [('Residential', 'Residential'), ('OPEX', 'RESCO')]
_RECORD_TYPE_LABELS = dict(RECORD_TYPES)

#: How many site codes a list row names before "+N more".
_SITES_SHOWN = 3


def _int_param(raw):
    raw = (raw or '').strip()
    return int(raw) if raw.isdigit() else None


# ---------------------------------------------------------------------------
# The workspace list
# ---------------------------------------------------------------------------

def _recorded_against(order):
    """What a list row says the record was recorded against — its tenders (the ticked
    ones UNION its sites' tenders, as _order_span counts them) and its site codes.
    Reads prefetched rows; costs no query."""
    sites = [link.project for link in order.sites.all()]
    tenders = {link.program.pk: link.program.name for link in order.programs.all()}
    for project in sites:
        if project.program_id is not None:
            tenders.setdefault(project.program_id, project.program.name)
    codes = sorted(project.project_id for project in sites)
    return {
        'tenders': sorted(tenders.values()),
        'sites':   codes[:_SITES_SHOWN],
        'more':    max(0, len(codes) - _SITES_SHOWN),
        'residential': order.project_type == 'Residential',
    }


def _workspace_row(user, order):
    documents = order.documents.all()
    payments  = list(order.payments.all())
    money     = _order_money(order, documents, payments)
    return {
        'order':    order,
        **money,
        'awaiting': sum(1 for p in payments if p.status == PaymentRequest.PENDING_APPROVAL),
        'on_hold':  sum(1 for p in payments if p.status == PaymentRequest.ON_HOLD),
        'against':  _recorded_against(order),
        # The record page's own rule for its Request payment button.
        'can_request_payment': (money['available'] > 0
                                and user_can_request_order_payment(user, order)),
    }


@login_required
def purchases_workspace(request):
    """Every PO / PI record, newest first, fifty to a page, with its money at a glance.

    Read by SCM, CEO, Admin, System Admin and Finance; only SCM sees the two actions and
    the per-row Request payment link.

    FILTERS. `q` searches vendor, PO, PI, tender name, site code and customer. `type`
    narrows to Residential or RESCO. `program` and `project` narrow to records recorded
    against that tender (either arm, as program_vendor_order_list reads it: the tender
    was ticked, OR a site in it was named) or that site — the links the cards will carry.

    FLAT IN THE NUMBER OF ROWS: one count, one page, four prefetches.
    """
    if not user_can_view_purchases_workspace(request.user):
        return HttpResponseForbidden()
    can_use = user_can_use_purchases_workspace(request.user)

    q = request.GET.get('q', '').strip()
    record_type = request.GET.get('type', '')
    if record_type not in _RECORD_TYPE_LABELS:
        record_type = ''
    program_pk = _int_param(request.GET.get('program'))
    project_pk = _int_param(request.GET.get('project'))

    orders = VendorOrder.objects.all()
    if record_type:
        orders = orders.filter(project_type=record_type)
    if program_pk is not None:
        orders = orders.filter(Q(programs__program_id=program_pk)
                               | Q(sites__project__program_id=program_pk))
    if project_pk is not None:
        orders = orders.filter(sites__project_id=project_pk)
    if q:
        orders = orders.filter(
            Q(vendor__name__icontains=q) | Q(po_number__icontains=q)
            | Q(pi_number__icontains=q)
            | Q(programs__program__name__icontains=q)
            | Q(sites__project__program__name__icontains=q)
            | Q(sites__project__project_id__icontains=q)
            | Q(sites__project__site_code__icontains=q)
            | Q(sites__project__customer_name__icontains=q))
    orders = (
        # The filters span to-many joins, so one order could come back once per match.
        orders.distinct()
        .select_related('vendor')
        .prefetch_related(
            Prefetch('documents', queryset=VendorOrderDocument.objects.filter(
                doc_type=VENDOR_ORDER_DOC_INVOICE)),
            'payments',
            Prefetch('programs', queryset=VendorOrderProgram.objects.select_related(
                'program')),
            Prefetch('sites', queryset=VendorOrderSite.objects.select_related(
                'project', 'project__program')),
        )
        .order_by('-created_at', '-pk')
    )
    page = Paginator(orders, PAGE_SIZE).get_page(request.GET.get('page'))
    rows = [_workspace_row(request.user, order) for order in page.object_list]

    # The narrowing tender / site, named in the heading. Asked only when filtered.
    program = (Program.objects.filter(pk=program_pk).only('pk', 'name').first()
               if program_pk is not None else None)
    project = (Project.objects.filter(pk=project_pk)
               .only('pk', 'project_id', 'customer_name').first()
               if project_pk is not None else None)

    # The prefill the two buttons carry, so "Add PO / PI" from a tender's view starts with
    # that tender ticked.
    carry = '&'.join(f'{key}={value}' for key, value in
                     (('program', program_pk), ('project', project_pk)) if value is not None)
    params = request.GET.copy()
    params.pop('page', None)

    return render(request, 'projects/purchases_workspace.html', {
        'rows':         rows,
        'page':         page,
        'page_query':   params.urlencode(),
        'q':            q,
        'record_type':  record_type,
        'record_types': RECORD_TYPES,
        'program':      program,
        'program_pk':   program_pk,
        'project':      project,
        'project_pk':   project_pk,
        'filtered':     bool(q or record_type or program_pk is not None
                             or project_pk is not None),
        'can_use':      can_use,
        'new_url':      reverse('purchases_new') + (f'?{carry}' if carry else ''),
        'pay_url':      reverse('purchases_pay'),
    })


# ---------------------------------------------------------------------------
# Add PO / PI
# ---------------------------------------------------------------------------

def _record_project_type(post, sites, programs, errors):
    """What the record IS — 'Residential' or 'OPEX' (RESCO) — or None with an error.

    DERIVED FROM WHAT IT IS RECORDED AGAINST: the kind of its sites and of its ticked
    tenders. Two kinds is refused — a PO / PI is Residential or RESCO, never both, for
    the reason the group raise refuses mixed sites (every money reader splits by it).
    Nothing recorded against at all is the one case the kind is ASKED: the posted
    `project_type` choice, required then and ignored otherwise.

    A mixture of site kinds has already been refused by _parse_group_sites(), in its own
    words; this adds nothing to that.
    """
    site_kinds = {site['project'].project_type for site in sites}
    if len(site_kinds) > 1:
        return None
    kinds = site_kinds | {program.program_type for program in programs}
    if len(kinds) > 1:
        errors.append('A PO / PI is recorded against Residential projects or against RESCO '
                      'tenders and sites — not both. Record one PO / PI per kind.')
        return None
    if kinds:
        kind = kinds.pop()
    else:
        kind = post.get('project_type', '')
        if kind not in _RECORD_TYPE_LABELS:
            errors.append('Nothing is recorded against, so choose whether this PO / PI is '
                          'Residential or RESCO.')
            return None
    if kind not in _RECORD_TYPE_LABELS:
        errors.append('Only Residential and RESCO purchases are recorded here.')
        return None
    return kind


def _parse_record_submission(request):
    """Read and validate the whole Add PO / PI POST. Returns (cleaned, errors).

    The contract every raise page keeps: touches no storage, writes nothing, and every
    check that can refuse runs here, before the first upload. `cleaned` has the shape
    _write_order_record() reads, plus documents, payment and the amounts warning.
    """
    post, errors = request.POST, []
    client_uuid = _parse_client_uuid(post, errors)

    # 1. PO / PI
    vendor, po_number, pi_number = _parse_header(post, errors)
    documents = _parse_new_order_documents(request, errors)

    # 2. Order total, and the optional payment it caps
    order_total = _parse_order_total(post, errors)
    payment = (_parse_payment_within(post, order_total, errors)
               if post.get('request_payment') else None)

    # 3. Recorded against — the group raise's parser, then this page's kind rule.
    sites, programs, _site_type, unfrozen = _parse_group_sites(request, errors)
    project_type = _record_project_type(post, sites, programs, errors)

    residential = [site['project'] for site in sites
                   if site['project'].project_type == 'Residential']
    for project in residential:
        # vendor_order_create's own gate, per site. SCM passes it for every live
        # Residential project; it is asked so the two doors cannot disagree.
        if not user_can_raise_vendor_order(request.user, project):
            errors.append(f'{project.project_id}: not a project a PO / PI can be recorded '
                          'against.')
    # A Residential site's tender is DERIVED, as vendor_order_create derives it — a
    # Residential project has no tender to tick. Project._validate_program_link() forbids
    # a Residential project a Program, so today this finds none; it is here so the
    # derivation matches the Residential raise from the start.
    derived = {project.program_id for project in residential
               if project.program_id is not None} - {program.pk for program in programs}
    if derived:
        programs = programs + list(Program.objects.filter(pk__in=derived))

    # 4. Requirement
    lines, priced_total = _parse_requirement(
        post, [site['project'].pk for site in sites], errors)

    cleaned = {
        'client_uuid': client_uuid, 'vendor': vendor,
        'po_number': po_number, 'pi_number': pi_number,
        'documents': documents, 'order_total': order_total, 'payment': payment,
        'sites': sites, 'programs': programs, 'project_type': project_type,
        # Residential has no procurement groups, so nothing is ever frozen; the model
        # stores False for it and reads that as "not applicable" (VendorOrder).
        'unfrozen': unfrozen if project_type != 'Residential' else False,
        'lines': lines,
        'amounts_differ': (priced_total is not None and order_total is not None
                           and priced_total != order_total),
        'priced_total': priced_total,
    }
    return cleaned, errors


def _new_record_prefill(request):
    """(default_programs, default_sites) from ?program= and ?project= — str pks.

    ?program ticks a live RESCO tender. ?project adds that site or Residential project;
    a RESCO site also ticks its tender, as adding it by ticking its group would."""
    programs, sites = [], []
    program = _picker_program(request.GET.get('program'))
    if program is not None and program.program_type == 'OPEX':
        programs.append(str(program.pk))
    project_pk = _int_param(request.GET.get('project'))
    if project_pk is not None:
        project = (Project.objects.filter(pk=project_pk, is_deleted=False)
                   .only('pk', 'project_type', 'program_id').first())
        if project is not None:
            sites.append(str(project.pk))
            if project.project_type == 'OPEX' and project.program_id is not None:
                programs.append(str(project.program_id))
    return programs, sites


def _new_record_context(request, post=None, client_uuid=None):
    """Everything Add PO / PI draws. With `post`, every entry comes back from it."""
    post = post or {}
    default_programs, default_sites = ([], []) if post else _new_record_prefill(request)
    return {
        **_picker_context(None, post, default_programs=default_programs,
                          default_sites=default_sites, include_residential=True),
        'vendors':          Vendor.objects.filter(is_active=True).order_by('name'),
        'doc_rows':         _doc_rows(post, {0: VENDOR_ORDER_DOC_PO, 1: VENDOR_ORDER_DOC_PI}),
        'doc_type_choices': _DOC_TYPE_SELECT,
        'client_uuid':      client_uuid or _uuid.uuid4(),
        'record_types':     RECORD_TYPES,
        'form': {
            'vendor_id':       post.get('vendor_id', ''),
            'po_number':       post.get('po_number', ''),
            'pi_number':       post.get('pi_number', ''),
            'order_total':     post.get('order_total', ''),
            'project_type':    post.get('project_type', ''),
            'request_payment': bool(post.get('request_payment')),
            'payment_amount':  post.get('payment_amount', ''),
            'payment_note':    post.get('payment_note', ''),
        },
        'refused': False,
    }


class _PaymentDidNotFit(Exception):
    """The payment asked for with a new record did not fit its total under the lock.
    Validation caps it at the total first, so this is unreachable short of a rule change;
    raised inside the write, it rolls the record back and the page refuses."""


@login_required
def purchases_new(request):
    """SCM records a PO / PI — vendor, numbers, documents, order total — recorded against
    RESCO tenders and sites, Residential projects, or nothing, and optionally raises its
    first payment request in the same submission. GET renders; POST creates.

    THE RECORD IS WRITTEN AS THE GROUP RAISE WRITES IT (_write_order_record): sites,
    tenders and requirement snapshot identical. What differs is only what the group raise
    requires and this page does not — a payment — and what this page offers that the
    group raise does not — Residential projects, and the kind choice when nothing is
    recorded against.
    """
    if not user_can_use_purchases_workspace(request.user):
        return HttpResponseForbidden()
    profile = request.user.profile
    template = 'projects/purchases_new.html'

    if request.method != 'POST':
        return render(request, template, _new_record_context(request))

    def refuse(errors, client_uuid):
        for error in errors:
            messages.error(request, error)
        context = _new_record_context(request, post=request.POST, client_uuid=client_uuid)
        context['refused'] = True
        return render(request, template, context, status=400)

    posted_uuid, existing = _posted_order_uuid(request)
    if existing is not None:
        return redirect('vendor_order_detail', order_pk=existing.pk)

    cleaned, errors = _parse_record_submission(request)
    if errors:
        return refuse(errors, posted_uuid)

    client_uuid, payment = cleaned['client_uuid'], cleaned['payment']
    projects = [site['project'] for site in cleaned['sites']]
    anchor = _write_anchor(projects)

    def write():
        """Inside _upload_and_record_documents' atomic block, after every file is stored.
        The payment is Request payment's own writer, so it is held to the same locked
        available-to-request rule — against a record that, being new, has committed
        nothing."""
        order = _write_order_record(cleaned, profile, client_uuid)
        pr = None
        if payment is not None:
            pr, _available = _create_order_payment(
                order, anchor, payment['amount'], payment['note'], request.user, profile,
                client_uuid=None)
            if pr is None:
                raise _PaymentDidNotFit()
        return order, (order, pr)

    saved, refusal = _save_order_submission(
        cleaned['documents'], client_uuid, profile, write, refuse,
        'The PO / PI could not be saved. Nothing was recorded; try again.')
    if refusal is not None:
        return refusal
    order, pr = saved

    # ── The feed, after the commit — the group raise's two lines. ──
    _log_order_recorded(order, projects, profile)
    if pr is not None:
        _log_payment_raised(anchor, profile, order, pr)

    ref = _order_ref(order)
    text = f'PO / PI {ref} recorded with {order.vendor.name}.'
    if pr is not None:
        text += f' Payment request of ₹{pr.amount} raised.'
    messages.success(request, text)
    if cleaned['amounts_differ']:
        messages.warning(request, f"The item amounts (₹{cleaned['priced_total']}) do not "
                                  f"add up to the order total (₹{order.total_amount}). "
                                  'Recorded as entered.')
    return redirect('vendor_order_detail', order_pk=order.pk)


# ---------------------------------------------------------------------------
# Raise payment request
# ---------------------------------------------------------------------------

def _payable_orders(user):
    """Every record `user` may request a payment against that still has money available
    to request, newest first, as (order, available) pairs.

    available is total − committed_total(payments) — the one rule, over prefetched rows,
    so the list costs the same queries for one record as for many. A record with nothing
    left is not offered: Request payment would refuse any amount on it.
    """
    orders = (VendorOrder.objects.select_related('vendor')
              .prefetch_related(
                  'payments',
                  Prefetch('sites', queryset=VendorOrderSite.objects.select_related(
                      'project')),
                  Prefetch('programs', queryset=VendorOrderProgram.objects.select_related(
                      'program')))
              .order_by('-created_at', '-pk'))
    pairs = []
    for order in orders:
        available = order.total_amount - committed_total(order.payments.all())
        if available > 0 and user_can_request_order_payment(user, order):
            pairs.append((order, available))
    return pairs


def _pay_context(request, selected, post=None, client_uuid=None, refused=False):
    post = post or {}
    options = [{'order': order, 'available': available,
                'selected': selected is not None and order.pk == selected}
               for order, available in _payable_orders(request.user)]
    chosen = next((option for option in options if option['selected']), None)
    return {
        'options':     options,
        'chosen':      chosen,
        # ?order= named a record that is not offered — nothing left, or not requestable.
        'not_offered': selected is not None and chosen is None,
        'client_uuid': client_uuid or _uuid.uuid4(),
        'form': {
            'payment_amount': post.get('payment_amount', ''),
            'payment_note':   post.get('payment_note', ''),
        },
        'refused':     refused,
    }


@login_required
def purchases_pay(request):
    """SCM raises a payment request against an existing PO / PI record. GET renders the
    picker (?order=<pk> preselects); POST hands the chosen record to
    _submit_order_payment() — vendor_order_add_payment's own POST path — unchanged.

    Which record is chosen is the ONLY thing this view decides. Whether this person may
    request against it is user_can_request_order_payment(), the record page's predicate,
    asked here as there; everything after is the shared path.
    """
    if not user_can_use_purchases_workspace(request.user):
        return HttpResponseForbidden()
    template = 'projects/purchases_pay.html'

    if request.method != 'POST':
        return render(request, template,
                      _pay_context(request, _int_param(request.GET.get('order'))))

    order_pk = _int_param(request.POST.get('order'))

    def refuse(errors, _available, client_uuid):
        for error in errors:
            messages.error(request, error)
        return render(request, template,
                      _pay_context(request, order_pk, request.POST, client_uuid,
                                   refused=True),
                      status=400)

    if order_pk is None:
        return refuse(['Choose the PO / PI record to request payment against.'],
                      None, request.POST.get('client_uuid'))
    order = _order_with_sites(order_pk)
    if not user_can_request_order_payment(request.user, order):
        return HttpResponseForbidden()
    return _submit_order_payment(request, order, refuse)
