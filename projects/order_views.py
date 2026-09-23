"""
O2 — recording a Residential vendor order, and reading one back.

A separate module from views.py for the same reason design_views.py is one: a
self-contained subsystem with its own URL group. urls.py imports it beside `views`.

WHAT AN ORDER IS. A VendorOrder RECORDS a purchase placed outside PMS (see the section
note above VendorOrder in models.py). This page is where SCM writes that record: the
vendor, the PO / PI numbers, the lines bought for this site, the documents, and
optionally the first payment against it. It replaces raise_payment_request, which
created a payment with no order.

RESIDENTIAL ONLY, ONE SITE PER ORDER. OPEX orders span a procurement group and arrive
with group ordering in O3; user_can_raise_vendor_order() refuses them here.

VALIDATE EVERYTHING, THEN UPLOAD, THEN WRITE. Nothing reaches storage until the whole
submission is known good, and nothing reaches the database until every file is stored.
If the database write fails, the files just uploaded are removed again (best effort) so
a failed submission leaves no orphan in the bucket.

O2b ADDS THREE DOORS to an existing order: a further payment (vendor_order_add_payment,
capped at the uncommitted balance under a row lock), more documents
(vendor_order_add_documents, append-only, open at any time), and the site's order list
(vendor_order_list). The raise and the append share _validate_document_slots() and
_upload_and_record_documents(), so a file is validated, stored and cleaned up the same
way whichever page it comes in by.

O2d SAYS WHAT THE SITES MEAN. A VendorOrderSite is the REQUIREMENT the order was sized
against, never a destination and never an allocation: material may be held centrally and
issued later, and this module holds no stock. An order may therefore have ZERO sites and
name only its tenders (VendorOrderProgram); a Residential order still carries exactly
one, and vendor_order_create is unchanged in behaviour. PaymentRequest.project became a
nullable DISPLAY ANCHOR — _order_project() is the one rule that computes it, and
scope_label is what a screen shows when it wants to say what a payment was for.

O3 ADDS THE SECOND RAISE PAGE and a tender's order list. vendor_order_create_group()
records ONE order sized against sites drawn from several procurement groups and several
tenders at once, with its lines prefilled from aggregate_group_boq() and editable, and a
zero-site central purchase as a first-class case. It is entered from a tender and limited
by none: see the O3 section note at the foot of this module. The Residential raise above
is untouched — the two share the header, document, invoice and payment helpers, and
nothing else.
"""
import json
import logging
import uuid as _uuid
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.contrib import messages
from django.db import IntegrityError, transaction
from django.db.models import Count, Prefetch, Q
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from .decorators import login_required
from .design_views import aggregate_group_boq, post_qc_pool
from .models import (
    BOQItem, BOQItemMaster, PaymentRequest, Program, Project, SiteGroup,
    SiteGroupMembership, Vendor, VendorOrder, VendorOrderDocument, VendorOrderLine,
    VendorOrderProgram, VendorOrderSite, committed_total, log_activity,
    GROUP_TYPE_PROCUREMENT, SITE_GROUP_LOCKED,
    VENDOR_ORDER_DOC_INVOICE, VENDOR_ORDER_DOC_PI, VENDOR_ORDER_DOC_PO,
    VENDOR_ORDER_DOC_TYPE_CHOICES,
)
from .permissions import (
    user_can_append_order_documents, user_can_raise_group_order,
    user_can_raise_vendor_order, user_can_request_order_payment,
    user_can_view_program_vendor_orders, user_can_view_project,
    user_can_view_project_vendor_orders, user_can_view_vendor_order,
)
from .supabase_storage import vendor_order_document_url
from .utils import record_transition
from .views import _validate_and_upload, _validate_upload_file

logger = logging.getLogger(__name__)

#: File slots on the raise and append pages. One submission carries at most this many
#: documents; the append page may be used as often as needed.
DOC_SLOTS = 5

_DOC_TYPES = dict(VENDOR_ORDER_DOC_TYPE_CHOICES)

# Short labels for the document-type select; the model's labels are for the detail page.
_DOC_TYPE_SELECT = [
    (VENDOR_ORDER_DOC_PO,      'PO'),
    (VENDOR_ORDER_DOC_PI,      'PI'),
    (VENDOR_ORDER_DOC_INVOICE, 'Invoice'),
    ('other',                  'Other'),
]


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _parse_decimal(raw, max_whole_digits):
    """A positive amount with at most 2 decimal places and `max_whole_digits` before the
    point (the column's max_digits − 2). None for anything else — blank, text, NaN,
    zero, negative, too precise or too large — so the caller words one error for all."""
    try:
        value = Decimal((raw or '').strip().replace(',', ''))
    except InvalidOperation:
        return None
    if not value.is_finite() or value <= 0:
        return None
    if value.as_tuple().exponent < -2:
        return None
    if value >= Decimal(10) ** max_whole_digits:
        return None
    return value


def _project_boq_items(project):
    """This project's BOQ rows, in serial order. The same scoping the old raise path and
    the SCM dashboard used (boq__project), with item_master for the line snapshot."""
    return list(
        BOQItem.objects.filter(boq__project=project)
        .select_related('item_master')
        .order_by('serial_no')
    )


def _validate_document_slots(request):
    """Read and validate the DOC_SLOTS file slots. Returns (documents, errors).

    Shared by the raise page and the append page, so a document is held to one standard
    whichever door it comes in by. Touches no storage. Whether a PO / PI is REQUIRED is
    the caller's rule, not this function's: the raise requires one, the append does not.
    """
    post, errors, documents = request.POST, [], []
    file_count = sum(len(request.FILES.getlist(key)) for key in request.FILES)
    if file_count > DOC_SLOTS:
        errors.append(f'Attach at most {DOC_SLOTS} files per order.')
    for index in range(DOC_SLOTS):
        upload = request.FILES.get(f'doc_file_{index}')
        if upload is None:
            continue
        doc_type = post.get(f'doc_type_{index}', '')
        if doc_type not in _DOC_TYPES:
            errors.append(f'{upload.name}: choose what kind of document it is.')
            continue
        try:
            _validate_upload_file(upload)
        except ValueError as exc:
            errors.append(f'{upload.name}: {exc}.')
            continue
        invoice_number, invoice_amount = '', None
        if doc_type == VENDOR_ORDER_DOC_INVOICE:
            invoice_number = post.get(f'doc_invoice_number_{index}', '').strip()
            invoice_amount = _parse_decimal(post.get(f'doc_invoice_amount_{index}'), 12)
            if not invoice_number or len(invoice_number) > 100:
                errors.append(f'{upload.name}: an invoice needs its invoice number.')
            if invoice_amount is None:
                errors.append(f'{upload.name}: an invoice needs an amount more than 0.')
        documents.append({
            'file': upload, 'doc_type': doc_type,
            'invoice_number': invoice_number, 'invoice_amount': invoice_amount,
        })
    return documents, errors


def _duplicate_invoice_numbers(documents, order=None):
    """The invoice numbers in `documents` that repeat — within this submission, or
    against an invoice already recorded on `order`. Compared trimmed and
    case-insensitive, so "inv-1 " and "INV-1" are one invoice. Returned as entered, each
    once, in submission order. The unique index uniq_invoice_number_per_order is the
    backstop; this is the check the user sees, and it runs before any upload.
    """
    seen = set()
    if order is not None:
        seen = {number.strip().casefold() for number in order.documents.filter(
            doc_type=VENDOR_ORDER_DOC_INVOICE).values_list('invoice_number', flat=True)}
    duplicates = []
    for doc in documents:
        if doc['doc_type'] != VENDOR_ORDER_DOC_INVOICE or not doc['invoice_number']:
            continue
        key = doc['invoice_number'].strip().casefold()
        if key in seen and doc['invoice_number'] not in duplicates:
            duplicates.append(doc['invoice_number'])
        seen.add(key)
    return duplicates


def _duplicate_invoice_errors(duplicates):
    return [f'Invoice {number} is already recorded on this order.' for number in duplicates]


def _parse_header(post, errors):
    """The vendor and the two reference numbers, checked. Appends to `errors` and returns
    (vendor, po_number, pi_number) — vendor may be None when it was refused.

    ONE COPY BECAUSE THERE ARE NOW TWO RAISE PAGES. The Residential raise and O3's group
    raise ask for exactly the same header and must refuse it in exactly the same words; a
    second copy is how the two come to disagree about whether an inactive vendor counts.
    Extracted verbatim from _parse_submission — same checks, same order, same strings.
    """
    vendor = None
    vendor_id = post.get('vendor_id', '').strip()
    if vendor_id.isdigit():
        vendor = Vendor.objects.filter(pk=int(vendor_id), is_active=True).first()
    if vendor is None:
        errors.append('Choose an active vendor.')

    po_number = post.get('po_number', '').strip()
    pi_number = post.get('pi_number', '').strip()
    if not po_number and not pi_number:
        errors.append('Enter a PO number, a PI number, or both.')
    if len(po_number) > 100 or len(pi_number) > 100:
        errors.append('PO and PI numbers are at most 100 characters.')
    return vendor, po_number, pi_number


def _parse_optional_payment(post, lines, total, errors):
    """The optional first payment. Appends to `errors`; returns the payment dict or None.

    Shared by both raise pages, for the reason given on _parse_header: the cap on a first
    payment is a money rule and there is one of it. Extracted verbatim — the early
    returns replace the elif/else chain and reach the same three outcomes.
    """
    if not post.get('request_payment'):
        return None
    amount = _parse_decimal(post.get('payment_amount'), 10)
    if amount is None:
        errors.append('The payment amount must be more than 0.')
        return None
    # The same rule vendor_order_add_payment applies: total − committed. A new order
    # has no payments yet, so committed is 0 and this is the order total — but it is
    # read through committed_total() so the two paths cannot drift apart.
    if lines and amount > total - committed_total(()):
        errors.append(f'The payment (₹{amount}) is more than the order total (₹{total}).')
        return None
    return {'amount': amount, 'note': post.get('payment_note', '').strip()}


def _parse_submission(request, boq_items):
    """Read and validate the whole POST. Returns (cleaned, errors).

    Touches no storage and writes nothing: every check that can refuse the submission
    runs here, before the first upload.
    """
    post, errors = request.POST, []
    items_by_pk = {item.pk: item for item in boq_items}

    # ── Idempotency key ──────────────────────────────────────────────────────
    try:
        client_uuid = _uuid.UUID(post.get('client_uuid', ''))
    except ValueError:
        client_uuid = None
        errors.append('This form expired. Check your entries and submit again.')

    # ── Header ───────────────────────────────────────────────────────────────
    vendor, po_number, pi_number = _parse_header(post, errors)

    # ── Lines ────────────────────────────────────────────────────────────────
    lines = []
    chosen = post.getlist('line')
    if not chosen:
        errors.append('Choose at least one BOQ item.')
    if len(chosen) != len(set(chosen)):
        errors.append('A BOQ item appears twice on this order.')
    for raw_pk in dict.fromkeys(chosen):
        item = items_by_pk.get(int(raw_pk)) if raw_pk.isdigit() else None
        if item is None:
            # Never another project's row, whatever the form was made to post.
            errors.append("A chosen item is not on this project's BOQ.")
            continue
        quantity = _parse_decimal(post.get(f'qty_{item.pk}'), 10)
        amount   = _parse_decimal(post.get(f'amount_{item.pk}'), 12)
        if quantity is None:
            errors.append(f'{item.description}: quantity must be more than 0.')
        if amount is None:
            errors.append(f'{item.description}: amount must be more than 0.')
        if quantity is not None and amount is not None:
            lines.append({'item': item, 'quantity': quantity, 'amount': amount})
    total = sum((line['amount'] for line in lines), Decimal('0'))

    # ── Documents ────────────────────────────────────────────────────────────
    documents, doc_errors = _validate_document_slots(request)
    errors.extend(doc_errors)
    # A new order has no invoices yet, so only repeats within this submission.
    errors.extend(_duplicate_invoice_errors(_duplicate_invoice_numbers(documents)))
    if not any(d['doc_type'] in (VENDOR_ORDER_DOC_PO, VENDOR_ORDER_DOC_PI)
               for d in documents):
        errors.append('Attach at least one PO or PI document.')

    # ── Optional first payment ───────────────────────────────────────────────
    payment = _parse_optional_payment(post, lines, total, errors)

    cleaned = {
        'client_uuid': client_uuid, 'vendor': vendor,
        'po_number': po_number, 'pi_number': pi_number,
        'lines': lines, 'total': total, 'documents': documents, 'payment': payment,
    }
    return cleaned, errors


# ---------------------------------------------------------------------------
# Rendering the raise page
# ---------------------------------------------------------------------------

def _doc_rows(post, default_types):
    """The file slots as the page draws them, with any typed type / invoice number /
    amount put back. `default_types` maps slot index to a preselected type."""
    return [
        {
            'index':          index,
            'doc_type':       post.get(f'doc_type_{index}', default_types.get(index, '')),
            'invoice_number': post.get(f'doc_invoice_number_{index}', ''),
            'invoice_amount': post.get(f'doc_invoice_amount_{index}', ''),
        }
        for index in range(DOC_SLOTS)
    ]


def _form_context(project, boq_items, post=None, client_uuid=None):
    """Everything the raise page draws. With `post`, every value the user typed is put
    back — a refused submission must never make them retype a line table. Files cannot
    be put back (browsers refuse it); the page says so."""
    post = post or {}
    chosen = set(post.getlist('line')) if post else set()

    groups, by_category = [], {}
    order = [c for c, _ in BOQItem.CATEGORY_CHOICES]
    for item in boq_items:
        key = str(item.pk)
        row = {
            'item':    item,
            'checked': key in chosen,
            'qty':     post.get(f'qty_{key}', item.boq_quantity if item.boq_quantity is not None else ''),
            'amount':  post.get(f'amount_{key}', ''),
        }
        by_category.setdefault(item.category, []).append(row)
    for category in order + sorted(set(by_category) - set(order)):
        if category in by_category:
            groups.append({'category': category, 'rows': by_category[category]})

    return {
        'project':         project,
        'vendors':         Vendor.objects.filter(is_active=True).order_by('name'),
        'groups':          groups,
        'doc_rows':        _doc_rows(post, {0: VENDOR_ORDER_DOC_PO, 1: VENDOR_ORDER_DOC_PI}),
        'doc_type_choices': _DOC_TYPE_SELECT,
        'client_uuid':     client_uuid or _uuid.uuid4(),
        'form': {
            'vendor_id':       post.get('vendor_id', ''),
            'po_number':       post.get('po_number', ''),
            'pi_number':       post.get('pi_number', ''),
            'request_payment': bool(post.get('request_payment')),
            'payment_amount':  post.get('payment_amount', ''),
            'payment_note':    post.get('payment_note', ''),
        },
        'refused':   False,
    }


def _refuse(request, project, boq_items, errors, client_uuid):
    for error in errors:
        messages.error(request, error)
    context = _form_context(project, boq_items, post=request.POST, client_uuid=client_uuid)
    context['refused'] = True
    return render(request, 'projects/vendor_order_form.html', context, status=400)


def _remove_uploaded(client, bucket, paths):
    """Best-effort delete of files stored for a submission that did not save. Never
    raises: the user's error is the save that failed, not this cleanup. A path that
    cannot be removed is logged so it can be found and removed by hand."""
    for path in paths:
        try:
            client.storage.from_(bucket).remove([path])
        except Exception as exc:
            logger.error('Vendor order cleanup: could not remove %s/%s — %s', bucket, path, exc)


class _UploadRefused(Exception):
    """Storage was unavailable or a file failed to upload. Nothing was written, and any
    file already stored has been removed. str(exc) is the message for the user."""


class _SaveFailed(Exception):
    """The database write failed after every file was stored. Nothing was written, and
    the stored files have been removed (best effort)."""


def _upload_and_record_documents(documents, folder, profile, write):
    """Store `documents`, then — in ONE atomic block — run `write()` and record a
    VendorOrderDocument for each file against the order it returns.

    `write` is a no-argument callable returning (order, result); it writes whatever
    else the submission creates (nothing, for an append). This function returns `result`.

    The sequence and the cleanup are the ones vendor_order_create has always had: every
    file is validated before this is called; nothing reaches the database until every
    file is stored; and if storing or writing fails, the files stored so far are removed
    again so a failed submission leaves no orphan in the bucket. Raises _UploadRefused
    or _SaveFailed — the caller words the refusal.
    """
    bucket = settings.SUPABASE_BUCKET

    # ── 1. Upload. Every file validated already; nothing written yet. ──────────
    try:
        from .supabase_storage import get_supabase_client
        client = get_supabase_client()
    except Exception as exc:
        logger.error('Vendor order: storage unavailable — %s', exc)
        raise _UploadRefused('The upload service is unavailable. Nothing was saved; try again.')

    uploaded = []
    try:
        for doc in documents:
            doc['path'] = f"vendor-orders/{folder}/{_uuid.uuid4()}_{doc['file'].name}"
            _validate_and_upload(doc['file'], client, bucket, doc['path'])
            uploaded.append(doc['path'])
    except Exception as exc:
        logger.error('Vendor order: upload failed — %s', exc)
        _remove_uploaded(client, bucket, uploaded)
        raise _UploadRefused('A document could not be uploaded. Nothing was saved; try again.')

    # ── 2. Write, all or nothing. ─────────────────────────────────────────────
    try:
        with transaction.atomic():
            order, result = write()
            VendorOrderDocument.objects.bulk_create([
                VendorOrderDocument(
                    order=order, doc_type=doc['doc_type'],
                    invoice_number=doc['invoice_number'],
                    invoice_amount=doc['invoice_amount'],
                    file_name=doc['file'].name, bucket=bucket, path=doc['path'],
                    file_type=(doc['file'].content_type or '')[:100],
                    file_size_kb=max(1, doc['file'].size // 1024),
                    uploaded_by=profile,
                )
                for doc in documents
            ])
    except Exception as exc:
        logger.error('Vendor order: save failed after upload — %s', exc)
        _remove_uploaded(client, bucket, uploaded)
        raise _SaveFailed() from exc
    return result


def _order_money(lines, documents, payments):
    """Every money figure an order page shows, summed from rows the caller already
    holds (prefetched), so a page of many orders costs no query per order. The same
    arithmetic as the VendorOrder properties; committed uses committed_total(), the one
    rule. The detail page and the list both call this, so their "Invoice awaited" agree.
    """
    total     = sum((line.amount for line in lines), Decimal('0'))
    paid      = sum((p.amount for p in payments if p.status == PaymentRequest.CONFIRMED),
                    Decimal('0'))
    invoiced  = sum((d.invoice_amount or Decimal('0') for d in documents
                     if d.doc_type == VENDOR_ORDER_DOC_INVOICE), Decimal('0'))
    committed = committed_total(payments)
    return {
        'total':     total,
        'paid':      paid,
        'balance':   total - paid,
        'invoiced':  invoiced,
        'committed': committed,
        'available': total - committed,
        # Money has gone out against invoices that have not come in.
        'invoice_awaited': paid > invoiced,
    }


def _order_project(order):
    """THE ANCHOR RULE (O2d): the site a payment or feed line on `order` is filed under —
    the site with the LOWEST project_id — or None when the order names no site.

    `project_id` is the FK column on VendorOrderSite, i.e. the Project's own primary key,
    so the answer is stable however the site rows were written and does not depend on the
    order in which they were added. A Residential order names exactly one site, so this
    is that site and the rule is the same answer O2b's "first site" gave.

    A DISPLAY ANCHOR, NOT SCOPE. It decides which site a payment is listed under and
    which project the feed line lands on, and it decides nothing else: the money and what
    it was for are the order's, whole. A site-less order anchors to None, and
    PaymentRequest.project is nullable so that it can (see the note on that field).

    Reads prefetched sites.
    """
    sites = list(order.sites.all())
    return min(sites, key=lambda site: site.project_id).project if sites else None


def _no_sites_text(programs):
    """What the sites section says when an order names none. Names the tenders when it
    has them, because "no sites recorded" alone reads like an incomplete record rather
    than a central purchase."""
    if programs:
        return f"No sites recorded — sized against {', '.join(p.name for p in programs)}"
    return 'No sites recorded'


def _order_with_sites(order_pk):
    return get_object_or_404(
        VendorOrder.objects.select_related('vendor').prefetch_related(
            Prefetch('sites', queryset=VendorOrderSite.objects.select_related('project')),
            Prefetch('programs',
                     queryset=VendorOrderProgram.objects.select_related('program'))),
        pk=order_pk,
    )


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

@login_required
def vendor_order_create(request, project_pk):
    """SCM records a vendor order for one Residential site. GET renders; POST creates.

    A full page, not a modal: a line table and several files do not fit a modal on a
    phone.
    """
    project = get_object_or_404(Project, pk=project_pk)
    if not user_can_raise_vendor_order(request.user, project):
        return HttpResponseForbidden()
    profile = request.user.profile
    boq_items = _project_boq_items(project)

    if request.method != 'POST':
        return render(request, 'projects/vendor_order_form.html',
                      _form_context(project, boq_items))

    # R-14. A double-click, or a resubmit after a slow response, carries the same key:
    # send it to the order it already made and create nothing.
    try:
        posted_uuid = _uuid.UUID(request.POST.get('client_uuid', ''))
    except ValueError:
        posted_uuid = None
    if posted_uuid is not None:
        existing = VendorOrder.objects.filter(client_uuid=posted_uuid).first()
        if existing is not None:
            return redirect('vendor_order_detail', order_pk=existing.pk)

    cleaned, errors = _parse_submission(request, boq_items)
    if errors:
        return _refuse(request, project, boq_items, errors, posted_uuid)

    client_uuid = cleaned['client_uuid']
    vendor, payment = cleaned['vendor'], cleaned['payment']

    def write():
        """Runs inside _upload_and_record_documents' atomic block, after every file is
        stored; the documents are recorded against the order this returns."""
        order = VendorOrder.objects.create(
            vendor=vendor, project_type=project.project_type,
            po_number=cleaned['po_number'], pi_number=cleaned['pi_number'],
            created_by=profile, client_uuid=client_uuid,
        )
        VendorOrderSite.objects.create(order=order, project=project, via_site_group=None)
        # The tender this order was sized against, derived from the site (O2d). A
        # Residential project is never under a Program — _validate_program_link()
        # excludes Residential outright — so today this writes nothing and always takes
        # the skip. It is here so the derivation lives on the raise path from the start,
        # and O3's group raise adds sites to the same loop rather than inventing it.
        if project.program_id is not None:
            VendorOrderProgram.objects.create(order=order, program_id=project.program_id)
        VendorOrderLine.objects.bulk_create([
            VendorOrderLine(
                order=order, boq_item=line['item'],
                item_master=line['item'].item_master,
                # The displayed identity is a snapshot (see VendorOrderLine).
                item_code=line['item'].item_master.code if line['item'].item_master else '',
                item_description=line['item'].description,
                item_unit=line['item'].uom,
                item_category=line['item'].category,
                quantity=line['quantity'], amount=line['amount'],
            )
            for line in cleaned['lines']
        ])
        pr = None
        if payment:
            # The legacy invoice fields are NOT NULL until O6 drops them; an order's
            # invoices live on VendorOrderDocument now, so they are written blank.
            pr = PaymentRequest.objects.create(
                vendor_order=order, project=project, vendor=vendor,
                amount=payment['amount'], note=payment['note'],
                requested_by=request.user,
                # APPROVED is correct until O4 introduces approval: a raised request
                # goes straight to Finance, exactly as raise_payment_request's did.
                status=PaymentRequest.APPROVED,
                invoice_number='', invoice_document_name='',
                invoice_document_url='', invoice_document_path='',
            )
            # Inside the atomic block: record_transition's contract is that the row
            # and the status it records commit together or not at all.
            record_transition(pr, to_status=PaymentRequest.APPROVED, from_status='',
                              actor=profile, project=project)
        return order, (order, pr)

    try:
        order, pr = _upload_and_record_documents(
            cleaned['documents'], client_uuid, profile, write)
    except _UploadRefused as exc:
        return _refuse(request, project, boq_items, [str(exc)], client_uuid)
    except _SaveFailed as exc:
        # Two submissions with one key can both pass the check above; the loser lands
        # here on the unique index. Send it to the winner rather than to an error.
        existing = VendorOrder.objects.filter(client_uuid=client_uuid).first()
        if existing is not None:
            return redirect('vendor_order_detail', order_pk=existing.pk)
        # uniq_invoice_number_per_order: same message as the check, files already removed.
        if isinstance(exc.__cause__, IntegrityError):
            duplicates = _duplicate_invoice_numbers(cleaned['documents'])
            if duplicates:
                return _refuse(request, project, boq_items,
                               _duplicate_invoice_errors(duplicates), client_uuid)
        return _refuse(request, project, boq_items,
                       ['The order could not be saved. Nothing was recorded; try again.'],
                       client_uuid)

    # ── The feed. log_activity never raises, so it sits after the commit. ──
    ref = order.po_number or order.pi_number
    log_activity(
        project, profile,
        f"Recorded order {ref} with {vendor.name}: ₹{cleaned['total']} "
        f"({len(cleaned['lines'])} line(s))",
        entity_type='VendorOrder', entity_id=order.pk, action_code='vendor_order_raised',
    )
    if pr is not None:
        log_activity(
            project, profile,
            f"Raised payment request to {vendor.name}: ₹{pr.amount} (order {ref})",
            entity_type='PaymentRequest', entity_id=pr.pk,
            action_code='payment_request_raised',
        )
    messages.success(request, f'Order {ref} recorded with {vendor.name}.')
    return redirect('vendor_order_detail', order_pk=order.pk)


@login_required
def vendor_order_detail(request, order_pk):
    """One order. Its only actions are the two O2b doors — request a payment, add
    documents — each drawn only when its predicate passes. Approval is O4.

    Totals are summed by _order_money() from the prefetched rows rather than read from
    the VendorOrder properties, which issue one aggregate each. Same arithmetic.

    THE SITES SECTION SAYS WHAT IT MEANS (O2d). It is headed "Sites this order was sized
    against", not "Sites", and carries one line saying the material may be held centrally
    and issued later — because a list of sites under a purchase reads as a delivery list,
    and reading it that way is how someone comes to believe PMS allocates stock. The
    tenders are shown beside it, and an order with no sites says so and names its tenders
    instead of rendering an empty list.
    """
    order = get_object_or_404(
        VendorOrder.objects.select_related('vendor', 'created_by__user').prefetch_related(
            # project__program and via_site_group: the detail page names each site's
            # group beside its tender (O3), and both are one join away.
            Prefetch('sites', queryset=VendorOrderSite.objects.select_related(
                'project', 'project__assigned_pm', 'project__program', 'via_site_group')),
            Prefetch('programs', queryset=VendorOrderProgram.objects.select_related(
                'program')),
            'lines',
            Prefetch('documents', queryset=VendorOrderDocument.objects.select_related(
                'uploaded_by__user')),
            Prefetch('payments', queryset=PaymentRequest.objects.select_related(
                'requested_by', 'confirmed_by')),
        ),
        pk=order_pk,
    )
    if not user_can_view_vendor_order(request.user, order):
        return HttpResponseForbidden()

    lines     = list(order.lines.all())
    documents = list(order.documents.all())
    payments  = list(order.payments.all())

    money     = _order_money(lines, documents, payments)
    project   = _order_project(order)

    document_groups = []
    for doc_type, label in VENDOR_ORDER_DOC_TYPE_CHOICES:
        docs = [{'doc': d, 'url': vendor_order_document_url(d)}
                for d in documents if d.doc_type == doc_type]
        if docs:
            document_groups.append({'label': label, 'docs': docs})

    # Prefetched; the template draws both, and _no_sites_text() words the empty case.
    sites    = list(order.sites.all())
    programs = [link.program for link in order.programs.all()]

    return render(request, 'projects/vendor_order_detail.html', {
        'order':           order,
        'sites':           sites,
        'programs':        programs,
        'no_sites_text':   _no_sites_text(programs),
        'lines':           lines,
        'document_groups': document_groups,
        'payments':        payments,
        **money,
        'orders_project':  project,
        # Each button only where its predicate passes; payment also only while some of
        # the total is still uncommitted. The view re-checks both on POST.
        'can_request_payment': (money['available'] > 0
                                and user_can_request_order_payment(request.user, order)),
        'can_add_documents':   user_can_append_order_documents(request.user, order),
    })


# ---------------------------------------------------------------------------
# O2b — a further payment, more documents, and the list of a site's orders
# ---------------------------------------------------------------------------

def _payment_context(order, available, post=None, client_uuid=None, refused=False):
    # No 'project' key: the page names the order's SCOPE (order.scope_label), not its
    # anchor, and a site-less order has no anchor to name (O2d).
    post = post or {}
    return {
        'order':       order,
        'available':   available,
        'client_uuid': client_uuid or _uuid.uuid4(),
        'form': {
            'payment_amount': post.get('payment_amount', ''),
            'payment_note':   post.get('payment_note', ''),
        },
        'refused':     refused,
    }


@login_required
def vendor_order_add_payment(request, order_pk):
    """SCM requests a further payment against an existing order. GET renders; POST
    creates one PaymentRequest for at most the order's uncommitted balance
    (total − committed_amount).

    THE ANCHOR. `project` is _order_project(order) — the lowest project_id of the order's
    sites, or None when it has none (O2d). It is the site the payment is LISTED under and
    nothing else; the money and what it was for are the order's. The same value anchors
    the feed line and the ledger row, and ActivityLog.project and
    StatusTransition.project are both nullable, so a site-less order writes both.
    """
    order = _order_with_sites(order_pk)
    if not user_can_request_order_payment(request.user, order):
        return HttpResponseForbidden()
    profile = request.user.profile
    project = _order_project(order)

    if request.method != 'POST':
        return render(request, 'projects/vendor_order_payment_form.html',
                      _payment_context(order, order.available_to_request))

    def refuse(errors, available, client_uuid):
        for error in errors:
            messages.error(request, error)
        return render(request, 'projects/vendor_order_payment_form.html',
                      _payment_context(order, available, request.POST, client_uuid,
                                       refused=True),
                      status=400)

    # R-14, as on the raise page: a repeated key goes to the order, and creates nothing.
    try:
        client_uuid = _uuid.UUID(request.POST.get('client_uuid', ''))
    except ValueError:
        client_uuid = None
    if client_uuid is not None and PaymentRequest.objects.filter(client_uuid=client_uuid).exists():
        return redirect('vendor_order_detail', order_pk=order.pk)

    errors = []
    if client_uuid is None:
        errors.append('This form expired. Check your entries and submit again.')
    amount = _parse_decimal(request.POST.get('payment_amount'), 10)
    if amount is None:
        errors.append('The payment amount must be more than 0.')
    note = request.POST.get('payment_note', '').strip()
    if errors:
        return refuse(errors, order.available_to_request, client_uuid)

    pr, available = None, None
    try:
        with transaction.atomic():
            # THE RACE. Two SCM tabs (or a retry with a new key) can each read the same
            # uncommitted balance and each ask for all of it; checked separately, both
            # pass and together exceed the order. Locking the ORDER row serialises every
            # payment on this order: the second request waits here until the first
            # commits, then recomputes committed_amount and sees the first's payment.
            # The lock must come BEFORE the read — a balance read outside it is stale.
            locked = VendorOrder.objects.select_for_update().get(pk=order.pk)
            available = locked.total - locked.committed_amount
            if amount <= available:
                pr = PaymentRequest.objects.create(
                    vendor_order=locked, project=project, vendor=order.vendor,
                    amount=amount, note=note, requested_by=request.user,
                    # APPROVED until O4: O4 puts PENDING_APPROVAL in front of Finance and
                    # changes this line. committed_total() already counts both.
                    status=PaymentRequest.APPROVED,
                    invoice_number='', invoice_document_name='',
                    invoice_document_url='', invoice_document_path='',
                    client_uuid=client_uuid,
                )
                # Inside the atomic block, as record_transition's contract requires.
                record_transition(pr, to_status=PaymentRequest.APPROVED, from_status='',
                                  actor=profile, project=project)
    except IntegrityError:
        # The same key raced past the check above; the winner holds the unique index.
        if PaymentRequest.objects.filter(client_uuid=client_uuid).exists():
            return redirect('vendor_order_detail', order_pk=order.pk)
        raise

    if pr is None:
        return refuse(
            [f'The payment (₹{amount}) is more than the balance still available to '
             f'request (₹{available}).'],
            available, client_uuid)

    # log_activity never raises, so it sits after the commit.
    ref = order.po_number or order.pi_number
    log_activity(
        project, profile,
        f"Raised payment request to {order.vendor.name}: ₹{pr.amount} (order {ref})",
        entity_type='PaymentRequest', entity_id=pr.pk, action_code='payment_request_raised',
    )
    messages.success(request, f'Payment of ₹{pr.amount} requested against order {ref}.')
    return redirect('vendor_order_detail', order_pk=order.pk)


def _documents_context(order, post=None, refused=False):
    # No 'project' key — see _payment_context.
    return {
        'order':            order,
        'doc_rows':         _doc_rows(post or {}, {}),
        'doc_type_choices': _DOC_TYPE_SELECT,
        'refused':          refused,
    }


@login_required
def vendor_order_add_documents(request, order_pk):
    """SCM attaches further documents to an order — a late invoice, a revised PI. GET
    renders; POST appends.

    Open at ANY time, including after every payment is confirmed. It only ever creates
    VendorOrderDocument rows: nothing on the order, and no existing document, is changed.
    Same slots, validation and cleanup as the raise page, through the same helpers.
    """
    order = _order_with_sites(order_pk)
    if not user_can_append_order_documents(request.user, order):
        return HttpResponseForbidden()
    profile = request.user.profile
    project = _order_project(order)

    if request.method != 'POST':
        return render(request, 'projects/vendor_order_documents_form.html',
                      _documents_context(order))

    def refuse(errors):
        for error in errors:
            messages.error(request, error)
        return render(request, 'projects/vendor_order_documents_form.html',
                      _documents_context(order, request.POST, refused=True), status=400)

    documents, errors = _validate_document_slots(request)
    if not documents and not errors:
        errors.append('Attach at least one document.')
    errors.extend(_duplicate_invoice_errors(_duplicate_invoice_numbers(documents, order)))
    if errors:
        return refuse(errors)

    # Files go in the order's own folder, beside the ones it was raised with.
    folder = order.client_uuid or f'order-{order.pk}'
    try:
        _upload_and_record_documents(documents, folder, profile, lambda: (order, None))
    except _UploadRefused as exc:
        return refuse([str(exc)])
    except _SaveFailed as exc:
        # The files are already removed. If uniq_invoice_number_per_order refused the
        # write — a second tab recorded the same invoice between the check and here — say
        # which invoice, exactly as the check would have.
        if isinstance(exc.__cause__, IntegrityError):
            duplicates = _duplicate_invoice_numbers(documents, order)
            if duplicates:
                return refuse(_duplicate_invoice_errors(duplicates))
        return refuse(['The documents could not be saved. Nothing was recorded; try again.'])

    ref = order.po_number or order.pi_number
    log_activity(
        project, profile,
        f"Added {len(documents)} document(s) to order {ref} with {order.vendor.name}",
        entity_type='VendorOrder', entity_id=order.pk,
        action_code='vendor_order_documents_added',
    )
    messages.success(request, f'{len(documents)} document(s) added to order {ref}.')
    return redirect('vendor_order_detail', order_pk=order.pk)


@login_required
def vendor_order_list(request, project_pk):
    """Every order placed for one site, newest first, with its money at a glance.

    Three prefetches carry every figure, so the page costs the same queries for one
    order as for fifty.
    """
    project = get_object_or_404(Project, pk=project_pk, is_deleted=False)
    if not user_can_view_project_vendor_orders(request.user, project):
        return HttpResponseForbidden()

    orders = (
        VendorOrder.objects.filter(sites__project=project)
        .select_related('vendor')
        .prefetch_related(
            'lines',
            Prefetch('documents', queryset=VendorOrderDocument.objects.filter(
                doc_type=VENDOR_ORDER_DOC_INVOICE)),
            'payments',
        )
        .order_by('-created_at', '-pk')
    )
    rows = [
        {'order': order,
         **_order_money(order.lines.all(), order.documents.all(), order.payments.all())}
        for order in orders
    ]
    return render(request, 'projects/vendor_order_list.html', {
        'project':   project,
        'rows':      rows,
        'raise_url': (reverse('vendor_order_create', args=[project.pk])
                      if user_can_raise_vendor_order(request.user, project) else None),
    })


# ---------------------------------------------------------------------------
# O3 — the group raise: one order sized against sites from several groups and
#      several tenders, and a tender's own order list
#
# THE SITES ARE THE SIZING BASIS AND NOTHING ELSE (O2d, restated because this is the
# page where it is easiest to get wrong). Section A of the raise page asks "whose
# requirement did you add up to reach these quantities", not "where is this going".
# Material bought here may sit in a central store and be issued to sites later, in a
# split nobody has decided yet — so a site named here is owed nothing, and no reader may
# treat a VendorOrderSite row as an allocation. The page says so in words, twice.
#
# NEVER LIMITED TO THE TENDER IT WAS ENTERED FROM. SCM reaches this page from a tender
# row, and the entering tender only decides what is PRE-TICKED. Every procurement group
# in the system is offered, across every tender, because a vendor's minimum order
# quantity does not respect tender boundaries and forcing one order per tender is how
# SCM ends up raising four orders for one truckload.
#
# ONE PAGE, ONE FORM, NO ROUND TRIP. Picking groups fills the site list and the line
# table in the browser; nothing is fetched between sections. That is why the server
# renders EVERY candidate line once, each carrying its per-site quantities as data, and
# the browser only shows, hides and re-sums them. One renderer, in Django, so the table
# a refused submission comes back with is the table the user was looking at.
# ---------------------------------------------------------------------------

def _candidate_contributions(candidate_ids):
    """{item_master_id: {project_pk: quantity}} over `candidate_ids`.

    THE SAME ROWS aggregate_group_boq() SUMS, KEYED BY PROJECT PK. Its own
    `contributions` map is keyed by `project__project_id` — the human site code, which is
    what the group screen prints — and this page has to match a value against a checkbox
    whose value is the pk. Rather than change the shape of a function four screens read,
    the pk-keyed form is built here from the same three filter terms (`boq_quantity__gt=0`
    and `item_master__isnull=False` over `boq__project_id__in`), so the two can never
    disagree about which BOQ rows count.

    One query, whatever the number of sites.
    """
    contributions = {}
    for row in (BOQItem.objects
                .filter(boq__project_id__in=candidate_ids, boq_quantity__gt=0,
                        item_master__isnull=False)
                .values('item_master', 'boq__project_id', 'boq_quantity')):
        contributions.setdefault(row['item_master'], {})[
            row['boq__project_id']] = row['boq_quantity']
    return contributions


def _raise_sources(entering_program):
    """Everything section A can offer: every PROCUREMENT group in the system with its
    live members, and each shown tender's post-QC pool.

    Returns (programs, sources, site_rows) where `sources` is one block per tender — its
    groups and its pool — and `site_rows` is one row per CANDIDATE SITE, tagged with the
    source it is reached through. A site appears ONCE: a live procurement membership is
    exclusive (SiteGroupMembership's partial unique constraint), and the pool is defined
    as "released and in no procurement group", so the two sets cannot overlap.

    COSTS A FIXED NUMBER OF QUERIES IN THE NUMBER OF SITES, which is the property that
    matters here: two for the groups and their members, then post_qc_pool() per shown
    tender. It does grow with the number of TENDERS, and that is deliberate —
    post_qc_pool() is the reviewed definition of "released and ungrouped", including the
    LEFT-JOIN trap documented on it, and a second hand-written copy of that exclusion is
    a worse trade than a query per tender.
    """
    groups = list(
        SiteGroup.objects
        .filter(group_type=GROUP_TYPE_PROCUREMENT, program__is_deleted=False,
                program__program_type='OPEX')
        .select_related('program')
        .annotate(member_count=Count('memberships',
                                     filter=Q(memberships__removed_at__isnull=True)))
        .order_by('program__name', '-created_at')
    )

    programs = {}
    for group in groups:
        programs.setdefault(group.program_id, group.program)
    if entering_program is not None:
        programs.setdefault(entering_program.pk, entering_program)
    programs = sorted(programs.values(), key=lambda program: program.name)

    members = {}
    for membership in (SiteGroupMembership.objects
                       .filter(group__in=groups, removed_at__isnull=True)
                       .select_related('project')
                       .order_by('project__project_id')):
        members.setdefault(membership.group_id, []).append(membership.project)

    sources, site_rows = [], []
    for program in programs:
        block = {'program': program, 'groups': [], 'pool': None}
        for group in groups:
            if group.program_id != program.pk:
                continue
            key = f'g{group.pk}'
            block['groups'].append({'group': group, 'key': key,
                                    'count': group.member_count})
            for project in members.get(group.pk, []):
                site_rows.append({'project': project, 'key': key, 'group': group,
                                  'program': program})
        pool = [assignment.project for assignment in post_qc_pool(program)]
        block['pool'] = {'key': f'p{program.pk}', 'count': len(pool)}
        for project in pool:
            site_rows.append({'project': project, 'key': f'p{program.pk}',
                              'group': None, 'program': program})
        sources.append(block)
    return programs, sources, site_rows


def _unfrozen_site_ids(project_ids):
    """Of `project_ids`, those whose BOQ quantities are NOT frozen — the site is in no
    LOCKED procurement group.

    THE QUERYSET FORM OF permissions.project_boq_is_group_locked(), inverted. Same three
    terms and the same reasons (see that function): `removed_at__isnull=True` because a
    site that left a group is free again, `group_type` spelled on the MEMBERSHIP because
    that is the column the exclusivity constraint is written over, and
    `group__status='locked'`. Asking it per site would cost a query per site; this costs
    one.

    Its answer becomes VendorOrder.raised_with_unfrozen_quantities, which is a historical
    fact about the moment of raising and is never recomputed afterwards. A site picked
    from the post-QC pool is in no group at all and so is unfrozen by this rule, which is
    the right answer — its BOQ can still move.
    """
    project_ids = list(project_ids)
    if not project_ids:
        return set()
    frozen = set(SiteGroupMembership.objects
                 .filter(project_id__in=project_ids, removed_at__isnull=True,
                         group_type=GROUP_TYPE_PROCUREMENT,
                         group__status=SITE_GROUP_LOCKED)
                 .values_list('project_id', flat=True))
    return {pk for pk in project_ids if pk not in frozen}


def _parse_group_sites(request, errors):
    """Section A. Returns (sites, programs, project_type, unfrozen).

    `sites` is a list of {'project', 'via_group_id'} in submission order, ONE PER SITE.

    ZERO SITES IS VALID (O2d) and is not an error here — a central purchase names its
    tenders and no site at all. What IS refused is a mixture of project types, a site
    that no longer exists or has been soft-deleted, and a site the caller cannot see.

    SCOPE IS CHECKED HERE, PER SITE, and not by user_can_raise_group_order() — see that
    predicate. The posted pks are resolved against Project rather than against whatever
    candidate list the page happened to render, because the page is a starting point and
    SCM may legitimately name a site from anywhere; the guards are visibility and not
    being deleted, which is the pair every other order surface applies.
    """
    post = request.POST

    # dict.fromkeys: a site ticked through two sources posts twice and must still become
    # ONE VendorOrderSite — uniq_vendor_order_site would refuse the second anyway, and
    # refusing the whole submission over a duplicate the page itself produced is worse
    # than collapsing it.
    ids = [int(raw) for raw in dict.fromkeys(post.getlist('site')) if raw.isdigit()]

    by_pk = {project.pk: project
             for project in Project.objects.filter(pk__in=ids, is_deleted=False)}
    resolved = []
    for pk in ids:
        project = by_pk.get(pk)
        if project is None:
            errors.append('A chosen site no longer exists.')
            continue
        if not user_can_view_project(request.user, project):
            errors.append('A chosen site is not one you can see.')
            continue
        resolved.append(project)

    # The group each site was picked THROUGH. DISPLAY ONLY (see VendorOrderSite.
    # via_site_group): a claim that no longer holds — the site was removed from the group
    # between the page loading and the form being submitted — is dropped to NULL rather
    # than refused, because the order's scope is the site rows and nothing reads
    # membership through this FK. What the quantities were frozen against is a separate
    # question, answered by _unfrozen_site_ids() from the memberships as they are now.
    claimed = {}
    for project in resolved:
        raw = post.get(f'via_group_{project.pk}', '').strip()
        if raw.isdigit():
            claimed[project.pk] = int(raw)
    live_pairs = set()
    if claimed:
        live_pairs = set(SiteGroupMembership.objects
                         .filter(group_id__in=set(claimed.values()),
                                 project_id__in=list(claimed), removed_at__isnull=True,
                                 group_type=GROUP_TYPE_PROCUREMENT)
                         .values_list('project_id', 'group_id'))
    sites = [{'project': project,
              'via_group_id': (claimed.get(project.pk)
                               if (project.pk, claimed.get(project.pk)) in live_pairs
                               else None)}
             for project in resolved]

    types = {project.project_type for project in resolved}
    if len(types) > 1:
        errors.append('Every site on one order must be the same kind of project — these '
                      f'are {", ".join(sorted(types))}. Raise one order per kind.')
    project_type = types.pop() if len(types) == 1 else None

    # The tenders this order says it was sized against. The page pre-ticks every tender
    # contributing a site and lets SCM remove any of them, so what is written is exactly
    # what was ticked — VendorOrderProgram records an intention, not a derived fact.
    program_ids = {int(raw) for raw in post.getlist('program') if raw.isdigit()}
    programs = (list(Program.objects.filter(pk__in=program_ids, is_deleted=False)
                     .order_by('name'))
                if program_ids else [])

    unfrozen = bool(_unfrozen_site_ids([project.pk for project in resolved]))
    return sites, programs, project_type, unfrozen


def _parse_group_lines(post, site_project_type, errors):
    """Section B. Returns (lines, total, project_type).

    ONE TICK NAME FOR BOTH KINDS OF ROW. An aggregate row and a hand-added row are the
    same thing by the time they are posted — a catalogue item, a quantity and an amount —
    so both post `line` = the BOQItemMaster pk. Which half of the screen a row came from
    is a fact about the screen, not about the order, and VendorOrderLine records no such
    field.

    THE QUANTITY IS NOT CHECKED AGAINST THE AGGREGATE, DELIBERATELY. The prefill is a
    starting point: an order may legitimately be for more than the requirement (a vendor
    minimum, spares) or less (part of it is already in stock). What was entered is what
    is stored.

    `project_type` comes back because a ZERO-SITE order takes its type from its lines —
    there is nothing else to take it from.
    """
    chosen = post.getlist('line')
    if not chosen:
        errors.append('Choose at least one item to order.')
    if len(chosen) != len(set(chosen)):
        errors.append('An item appears twice on this order.')

    ids = [int(raw) for raw in dict.fromkeys(chosen) if raw.isdigit()]
    masters = {master.pk: master for master in BOQItemMaster.objects.filter(pk__in=ids)}

    lines, types = [], set()
    for pk in ids:
        master = masters.get(pk)
        if master is None:
            errors.append('A chosen item is not in the catalogue.')
            continue
        types.add(master.project_type)
        if site_project_type is not None and master.project_type != site_project_type:
            errors.append(f'{master.code} is a {master.project_type} catalogue item and '
                          f'this order is for {site_project_type} sites.')
            continue
        quantity = _parse_decimal(post.get(f'qty_{pk}'), 10)
        amount   = _parse_decimal(post.get(f'amount_{pk}'), 12)
        if quantity is None:
            errors.append(f'{master.description}: quantity must be more than 0.')
        if amount is None:
            errors.append(f'{master.description}: amount must be more than 0.')
        if quantity is not None and amount is not None:
            lines.append({'master': master, 'quantity': quantity, 'amount': amount})

    project_type = site_project_type
    if project_type is None:
        # No sites: the lines decide, and they have to agree with each other.
        if len(types) > 1:
            errors.append('Every item on one order must come from the same catalogue — '
                          f'these are {", ".join(sorted(types))}.')
        elif len(types) == 1:
            project_type = types.pop()

    total = sum((line['amount'] for line in lines), Decimal('0'))
    return lines, total, project_type


def _parse_group_submission(request):
    """Read and validate the whole group-raise POST. Returns (cleaned, errors).

    The same contract as _parse_submission: touches no storage, writes nothing, and every
    check that can refuse the submission runs here, before the first upload. The header,
    the document slots, the invoice-number check and the optional first payment go
    through the very helpers the Residential raise uses, so a vendor, a file and a first
    payment are held to one standard on both pages.
    """
    post, errors = request.POST, []

    try:
        client_uuid = _uuid.UUID(post.get('client_uuid', ''))
    except ValueError:
        client_uuid = None
        errors.append('This form expired. Check your entries and submit again.')

    vendor, po_number, pi_number = _parse_header(post, errors)
    sites, programs, site_type, unfrozen = _parse_group_sites(request, errors)
    lines, total, project_type = _parse_group_lines(post, site_type, errors)
    if project_type is None and not errors:
        # Every line was refused for a reason already worded, or there were none.
        errors.append('Choose at least one item to order.')

    documents, doc_errors = _validate_document_slots(request)
    errors.extend(doc_errors)
    # A new order has no invoices yet, so only repeats within this submission.
    errors.extend(_duplicate_invoice_errors(_duplicate_invoice_numbers(documents)))
    if not any(doc['doc_type'] in (VENDOR_ORDER_DOC_PO, VENDOR_ORDER_DOC_PI)
               for doc in documents):
        errors.append('Attach at least one PO or PI document.')

    payment = _parse_optional_payment(post, lines, total, errors)

    cleaned = {
        'client_uuid': client_uuid, 'vendor': vendor,
        'po_number': po_number, 'pi_number': pi_number,
        'sites': sites, 'programs': programs, 'project_type': project_type,
        'unfrozen': unfrozen, 'lines': lines, 'total': total,
        'documents': documents, 'payment': payment,
    }
    return cleaned, errors


def _line_row_shape(master, qty, amount):
    """One line row as the shared include draws it. `master=None` yields the PROTOTYPE
    row whose four tokens "Add a line" substitutes in the browser.

    Hand-added rows carry no contributions — no site's BOQ is behind them — and are
    always ticked and always visible; the aggregate columns render as em dashes.
    """
    return {
        'master_id':   master.pk if master else '__PK__',
        'code':        master.code if master else '__CODE__',
        'description': master.description if master else '__DESC__',
        'unit':        master.unit if master else '__UNIT__',
        'contrib':     '{}',
        'checked':     True,
        'qty':         qty,
        'amount':      amount,
        'hand':        True,
    }


def _group_form_context(request, entering_program, post=None, client_uuid=None):
    """Everything the group raise page draws.

    With `post`, every tick and every typed figure is put back — a refused submission
    must never make someone rebuild a fifty-line table. Files cannot be put back
    (browsers refuse it); the page says so, exactly as the Residential one does.

    THE LINE TABLE IS RENDERED ONCE, FOR EVERY CANDIDATE SITE AT ONCE, and each row
    carries its per-site quantities in a data attribute. The browser shows the rows whose
    sites are ticked and re-sums them; it builds no markup of its own. That is what lets
    section B follow section A with no round trip while leaving exactly one renderer, in
    Django, for both the first draw and the redraw after a refusal.
    """
    post = post or {}
    programs, sources, site_rows = _raise_sources(entering_program)

    candidate_ids = [row['project'].pk for row in site_rows]
    agg = aggregate_group_boq(candidate_ids)
    contributions = _candidate_contributions(candidate_ids)

    ticked_sites   = set(post.getlist('site')) if post else set()
    ticked_lines   = set(post.getlist('line')) if post else set()
    ticked_sources = set(post.getlist('source')) if post else set()
    ticked_programs = set(post.getlist('program')) if post else set()
    if not post and entering_program is not None:
        # Entered from a tender: that tender is offered pre-ticked, so a zero-site order
        # raised straight away still says what it was sized against.
        ticked_programs = {str(entering_program.pk)}

    for row in site_rows:
        row['checked'] = str(row['project'].pk) in ticked_sites
    for block in sources:
        for entry in block['groups']:
            entry['checked'] = entry['key'] in ticked_sources
        block['pool']['checked'] = block['pool']['key'] in ticked_sources

    line_rows, aggregated = [], set()
    for line in agg['lines']:
        key = str(line['item_master'])
        aggregated.add(line['item_master'])
        line_rows.append({
            'master_id':   line['item_master'],
            'code':        line['item_master__code'],
            'description': line['item_master__description'],
            'unit':        line['item_master__unit'],
            'contrib':     json.dumps(
                {str(pk): str(quantity) for pk, quantity
                 in contributions.get(line['item_master'], {}).items()}),
            'checked':     key in ticked_lines,
            'qty':         post.get(f'qty_{key}', ''),
            'amount':      post.get(f'amount_{key}', ''),
            'hand':        False,
        })

    # Rows added by hand come back the way they went out. Anything ticked that the
    # aggregate does not know about was added by hand, by definition.
    hand_ids = [int(raw) for raw in ticked_lines
                if raw.isdigit() and int(raw) not in aggregated]
    hand_rows = [
        _line_row_shape(master, post.get(f'qty_{master.pk}', ''),
                        post.get(f'amount_{master.pk}', ''))
        for master in BOQItemMaster.objects.filter(pk__in=hand_ids).order_by('code')
    ]

    return {
        'entering_program': entering_program,
        'programs':         programs,
        'ticked_programs':  ticked_programs,
        'sources':          sources,
        'site_rows':        site_rows,
        'line_rows':        line_rows,
        'hand_rows':        hand_rows,
        # THE ROW MARKUP EXISTS ONCE. "Add a line" clones this prototype and substitutes
        # the four tokens, so a hand-added row is drawn by the same template include as
        # every other row and the two cannot drift apart.
        'proto_row':        _line_row_shape(None, '', ''),
        # item_master IS NULL, so these cannot be summed and cannot be ordered. Shown
        # read-only rather than dropped: a consolidated quantity that is silently short
        # is worse than one that says what it left out (aggregate_group_boq).
        'unlinked':         agg['unlinked'],
        'catalogue':        list(BOQItemMaster.objects.filter(is_active=True)
                                 .order_by('project_type', 'sort_order', 'code')),
        'vendors':          Vendor.objects.filter(is_active=True).order_by('name'),
        'doc_rows':         _doc_rows(post, {0: VENDOR_ORDER_DOC_PO,
                                             1: VENDOR_ORDER_DOC_PI}),
        'doc_type_choices': _DOC_TYPE_SELECT,
        'client_uuid':      client_uuid or _uuid.uuid4(),
        'form': {
            'vendor_id':       post.get('vendor_id', ''),
            'po_number':       post.get('po_number', ''),
            'pi_number':       post.get('pi_number', ''),
            'request_payment': bool(post.get('request_payment')),
            'payment_amount':  post.get('payment_amount', ''),
            'payment_note':    post.get('payment_note', ''),
        },
        'refused': False,
    }


def _entering_program(request):
    """The tender the page was entered from, or None. It decides what is PRE-TICKED and
    nothing else — every group in the system is offered whatever this is."""
    raw = (request.POST.get('entered_from') or request.GET.get('program') or '').strip()
    if not raw.isdigit():
        return None
    return Program.objects.filter(pk=int(raw), is_deleted=False).first()


@login_required
def vendor_order_create_group(request):
    """SCM records one order sized against sites drawn from any number of procurement
    groups and any number of tenders. GET renders; POST creates.

    ONE PAGE, FOUR SECTIONS, ONE FORM — sizing basis, lines, documents, optional first
    payment. The same shape as the Residential raise, with section A replacing that
    page's single project.

    EVERYTHING AFTER VALIDATION IS O2'S SEQUENCE, UNCHANGED: validate the whole
    submission, upload every file, then write in one transaction, and remove the uploaded
    files again if the write fails. _upload_and_record_documents() owns that sequence and
    this view hands it a `write()` like every other caller.
    """
    if not user_can_raise_group_order(request.user):
        return HttpResponseForbidden()
    profile = request.user.profile
    entering = _entering_program(request)

    if request.method != 'POST':
        return render(request, 'projects/vendor_order_group_form.html',
                      _group_form_context(request, entering))

    def refuse(errors, client_uuid):
        for error in errors:
            messages.error(request, error)
        context = _group_form_context(request, entering, post=request.POST,
                                      client_uuid=client_uuid)
        context['refused'] = True
        return render(request, 'projects/vendor_order_group_form.html', context,
                      status=400)

    # R-14, as on the Residential raise: a repeated key goes to the order it already made
    # and creates nothing.
    try:
        posted_uuid = _uuid.UUID(request.POST.get('client_uuid', ''))
    except ValueError:
        posted_uuid = None
    if posted_uuid is not None:
        existing = VendorOrder.objects.filter(client_uuid=posted_uuid).first()
        if existing is not None:
            return redirect('vendor_order_detail', order_pk=existing.pk)

    cleaned, errors = _parse_group_submission(request)
    if errors:
        return refuse(errors, posted_uuid)

    client_uuid = cleaned['client_uuid']
    vendor, payment, sites = cleaned['vendor'], cleaned['payment'], cleaned['sites']
    projects = [site['project'] for site in sites]

    # THE ANCHOR (O2d), computed at write time: the site with the lowest project_id — the
    # Project pk, which is the column _order_project() reads back off VendorOrderSite —
    # or None when the order names no site. A display anchor and nothing more.
    anchor = min(projects, key=lambda project: project.pk, default=None)

    def write():
        """Runs inside _upload_and_record_documents' atomic block, after every file is
        stored; the documents are recorded against the order this returns."""
        order = VendorOrder.objects.create(
            vendor=vendor, project_type=cleaned['project_type'],
            po_number=cleaned['po_number'], pi_number=cleaned['pi_number'],
            raised_with_unfrozen_quantities=cleaned['unfrozen'],
            created_by=profile, client_uuid=client_uuid,
        )
        VendorOrderSite.objects.bulk_create([
            VendorOrderSite(order=order, project=site['project'],
                            via_site_group_id=site['via_group_id'])
            for site in sites
        ])
        VendorOrderProgram.objects.bulk_create([
            VendorOrderProgram(order=order, program=program)
            for program in cleaned['programs']
        ])
        VendorOrderLine.objects.bulk_create([
            VendorOrderLine(
                order=order, item_master=line['master'],
                # A GROUP LINE HAS NO SINGLE BOQ ROW. Its quantity is the sum of several
                # sites' rows — or, for a hand-added line, of none at all — so there is
                # no BOQItem it could name. The join that survives is item_master, which
                # is what BOQItemMaster exists for.
                boq_item=None,
                item_code=line['master'].code,
                item_description=line['master'].description,
                item_unit=line['master'].unit,
                item_category=line['master'].category,
                quantity=line['quantity'], amount=line['amount'],
            )
            for line in cleaned['lines']
        ])
        pr = None
        if payment:
            # The legacy invoice fields are NOT NULL until O6 drops them; an order's
            # invoices live on VendorOrderDocument now, so they are written blank.
            pr = PaymentRequest.objects.create(
                vendor_order=order, project=anchor, vendor=vendor,
                amount=payment['amount'], note=payment['note'],
                requested_by=request.user,
                # APPROVED until O4 introduces approval, exactly as both other raise
                # paths create it.
                status=PaymentRequest.APPROVED,
                invoice_number='', invoice_document_name='',
                invoice_document_url='', invoice_document_path='',
            )
            # Inside the atomic block: record_transition's contract is that the row and
            # the status it records commit together or not at all.
            record_transition(pr, to_status=PaymentRequest.APPROVED, from_status='',
                              actor=profile, project=anchor)
        return order, (order, pr)

    try:
        order, pr = _upload_and_record_documents(
            cleaned['documents'], client_uuid, profile, write)
    except _UploadRefused as exc:
        return refuse([str(exc)], client_uuid)
    except _SaveFailed as exc:
        # Two submissions with one key can both pass the check above; the loser lands
        # here on the unique index. Send it to the winner rather than to an error.
        existing = VendorOrder.objects.filter(client_uuid=client_uuid).first()
        if existing is not None:
            return redirect('vendor_order_detail', order_pk=existing.pk)
        # uniq_invoice_number_per_order: same message as the check, files already removed.
        if isinstance(exc.__cause__, IntegrityError):
            duplicates = _duplicate_invoice_numbers(cleaned['documents'])
            if duplicates:
                return refuse(_duplicate_invoice_errors(duplicates), client_uuid)
        return refuse(['The order could not be saved. Nothing was recorded; try again.'],
                      client_uuid)

    # ── The feed. log_activity never raises, so it sits after the commit. ──
    # ONE LINE PER SITE, on the site itself, exactly as site_group_lock() writes its
    # lock: a group is not a project, and the site is where a PM looks to find out what
    # was bought against their requirement.
    #
    # A SITE-LESS ORDER IS NOT LOGGED AT ALL, and there is no way to log it today:
    # ActivityLog has a nullable `project` and NO program column, so an event against a
    # tender has nowhere to land — it would be written with project=NULL, where no feed
    # reads it. Recorded in DEFERRED rather than faked.
    ref = order.po_number or order.pi_number
    for project in projects:
        log_activity(
            project, profile,
            f"Recorded order {ref} with {vendor.name}: ₹{cleaned['total']} "
            f"({len(cleaned['lines'])} line(s), sized against {len(projects)} site(s))",
            entity_type='VendorOrder', entity_id=order.pk,
            action_code='vendor_order_raised',
        )
    if pr is not None:
        log_activity(
            anchor, profile,
            f"Raised payment request to {vendor.name}: ₹{pr.amount} (order {ref})",
            entity_type='PaymentRequest', entity_id=pr.pk,
            action_code='payment_request_raised',
        )
    messages.success(request, f'Order {ref} recorded with {vendor.name}.')
    return redirect('vendor_order_detail', order_pk=order.pk)


def _order_span(order):
    """How many tenders `order` spans — its VendorOrderProgram rows UNION the tenders of
    its sites. 0 or 1 means the list draws no label.

    The union rather than the program rows alone: a site's tender is a tender the order
    was sized against whether or not anyone ticked it, and the list is answering "is this
    order bigger than the tender you are looking at".

    Reads prefetched rows; costs no query.
    """
    ids = {link.program_id for link in order.programs.all()}
    ids |= {site.project.program_id for site in order.sites.all()
            if site.project.program_id is not None}
    return len(ids)


@login_required
def program_vendor_order_list(request, program_pk):
    """Every order raised against one tender, newest first, with its money at a glance.

    IN THIS TENDER means either arm: the order NAMES the tender (a VendorOrderProgram
    row, which is how a site-less central purchase gets here) OR it names a site in it.
    An order spanning several tenders appears in each of their lists and says so.

    The same prefetches and the same _order_money() the site list uses, so the two pages'
    figures agree and this one costs the same queries for one order as for fifty.
    """
    program = get_object_or_404(Program, pk=program_pk, is_deleted=False)
    if not user_can_view_program_vendor_orders(request.user, program):
        return HttpResponseForbidden()

    orders = (
        VendorOrder.objects
        .filter(Q(programs__program=program) | Q(sites__project__program=program))
        # The OR spans two joins, so an order with a program row AND two sites in this
        # tender would otherwise come back three times.
        .distinct()
        .select_related('vendor')
        .prefetch_related(
            'lines',
            Prefetch('documents', queryset=VendorOrderDocument.objects.filter(
                doc_type=VENDOR_ORDER_DOC_INVOICE)),
            'payments',
            'programs',
            Prefetch('sites', queryset=VendorOrderSite.objects.select_related('project')),
        )
        .order_by('-created_at', '-pk')
    )
    rows = [
        {'order': order,
         'spans': _order_span(order),
         **_order_money(order.lines.all(), order.documents.all(), order.payments.all())}
        for order in orders
    ]
    raise_url = None
    if user_can_raise_group_order(request.user):
        raise_url = f"{reverse('vendor_order_create_group')}?program={program.pk}"
    return render(request, 'projects/vendor_order_list.html', {
        'program':   program,
        'rows':      rows,
        'raise_url': raise_url,
    })
