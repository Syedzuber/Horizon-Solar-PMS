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
"""
import logging
import uuid as _uuid
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.contrib import messages
from django.db import IntegrityError, transaction
from django.db.models import Prefetch
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from .decorators import login_required
from .models import (
    BOQItem, PaymentRequest, Project, Vendor, VendorOrder, VendorOrderDocument,
    VendorOrderLine, VendorOrderSite, committed_total, log_activity,
    VENDOR_ORDER_DOC_INVOICE, VENDOR_ORDER_DOC_PI, VENDOR_ORDER_DOC_PO,
    VENDOR_ORDER_DOC_TYPE_CHOICES,
)
from .permissions import (
    user_can_append_order_documents, user_can_raise_vendor_order,
    user_can_request_order_payment, user_can_view_project_vendor_orders,
    user_can_view_vendor_order,
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
    payment = None
    if post.get('request_payment'):
        amount = _parse_decimal(post.get('payment_amount'), 10)
        if amount is None:
            errors.append('The payment amount must be more than 0.')
        # The same rule vendor_order_add_payment applies: total − committed. A new order
        # has no payments yet, so committed is 0 and this is the order total — but it is
        # read through committed_total() so the two paths cannot drift apart.
        elif lines and amount > total - committed_total(()):
            errors.append(f'The payment (₹{amount}) is more than the order total (₹{total}).')
        else:
            payment = {'amount': amount, 'note': post.get('payment_note', '').strip()}

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
    """The site a payment or feed line on `order` is filed under: its first site.
    A Residential order has exactly one. Group orders (O3) must decide their own answer
    before they use the add-payment path. Reads prefetched sites."""
    sites = sorted(order.sites.all(), key=lambda site: site.pk)
    return sites[0].project if sites else None


def _order_with_sites(order_pk):
    return get_object_or_404(
        VendorOrder.objects.select_related('vendor').prefetch_related(
            Prefetch('sites', queryset=VendorOrderSite.objects.select_related('project'))),
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
    """
    order = get_object_or_404(
        VendorOrder.objects.select_related('vendor', 'created_by__user').prefetch_related(
            Prefetch('sites', queryset=VendorOrderSite.objects.select_related(
                'project', 'project__assigned_pm', 'via_site_group')),
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

    return render(request, 'projects/vendor_order_detail.html', {
        'order':           order,
        'sites':           list(order.sites.all()),
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
    post = post or {}
    return {
        'order':       order,
        'project':     _order_project(order),
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
    (total − committed_amount)."""
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
    return {
        'order':            order,
        'project':          _order_project(order),
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
