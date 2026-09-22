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
"""
import logging
import uuid as _uuid
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.contrib import messages
from django.db import transaction
from django.db.models import Prefetch
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render

from .decorators import login_required
from .models import (
    BOQItem, PaymentRequest, Project, Vendor, VendorOrder, VendorOrderDocument,
    VendorOrderLine, VendorOrderSite, log_activity,
    VENDOR_ORDER_DOC_INVOICE, VENDOR_ORDER_DOC_PI, VENDOR_ORDER_DOC_PO,
    VENDOR_ORDER_DOC_TYPE_CHOICES,
)
from .permissions import user_can_raise_vendor_order, user_can_view_vendor_order
from .supabase_storage import vendor_order_document_url
from .utils import record_transition
from .views import _validate_and_upload, _validate_upload_file

logger = logging.getLogger(__name__)

#: File slots on the raise page. One submission carries at most this many documents;
#: appending more to an existing order is O2b.
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
    documents = []
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
    if not any(d['doc_type'] in (VENDOR_ORDER_DOC_PO, VENDOR_ORDER_DOC_PI)
               for d in documents):
        errors.append('Attach at least one PO or PI document.')

    # ── Optional first payment ───────────────────────────────────────────────
    payment = None
    if post.get('request_payment'):
        amount = _parse_decimal(post.get('payment_amount'), 10)
        if amount is None:
            errors.append('The payment amount must be more than 0.')
        elif lines and amount > total:
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

    doc_rows = []
    for index in range(DOC_SLOTS):
        default_type = {0: VENDOR_ORDER_DOC_PO, 1: VENDOR_ORDER_DOC_PI}.get(index, '')
        doc_rows.append({
            'index':          index,
            'doc_type':       post.get(f'doc_type_{index}', default_type),
            'invoice_number': post.get(f'doc_invoice_number_{index}', ''),
            'invoice_amount': post.get(f'doc_invoice_amount_{index}', ''),
        })

    return {
        'project':         project,
        'vendors':         Vendor.objects.filter(is_active=True).order_by('name'),
        'groups':          groups,
        'doc_rows':        doc_rows,
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
    bucket = settings.SUPABASE_BUCKET

    # ── 1. Upload. Every file validated above; nothing written yet. ────────────
    try:
        from .supabase_storage import get_supabase_client
        client = get_supabase_client()
    except Exception as exc:
        logger.error('Vendor order: storage unavailable — %s', exc)
        return _refuse(request, project, boq_items,
                       ['The upload service is unavailable. Nothing was saved; try again.'],
                       client_uuid)

    uploaded = []
    try:
        for doc in cleaned['documents']:
            doc['path'] = f"vendor-orders/{client_uuid}/{_uuid.uuid4()}_{doc['file'].name}"
            _validate_and_upload(doc['file'], client, bucket, doc['path'])
            uploaded.append(doc['path'])
    except Exception as exc:
        logger.error('Vendor order: upload failed — %s', exc)
        _remove_uploaded(client, bucket, uploaded)
        return _refuse(request, project, boq_items,
                       ['A document could not be uploaded. Nothing was saved; try again.'],
                       client_uuid)

    # ── 2. Write, all or nothing. ─────────────────────────────────────────────
    vendor, payment = cleaned['vendor'], cleaned['payment']
    try:
        with transaction.atomic():
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
                for doc in cleaned['documents']
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
    except Exception as exc:
        logger.error('Vendor order: save failed after upload — %s', exc)
        _remove_uploaded(client, bucket, uploaded)
        # Two submissions with one key can both pass the check above; the loser lands
        # here on the unique index. Send it to the winner rather than to an error.
        existing = VendorOrder.objects.filter(client_uuid=client_uuid).first()
        if existing is not None:
            return redirect('vendor_order_detail', order_pk=existing.pk)
        return _refuse(request, project, boq_items,
                       ['The order could not be saved. Nothing was recorded; try again.'],
                       client_uuid)

    # ── 3. The feed. log_activity never raises, so it sits after the commit. ──
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
    """Read-only view of one order. No action buttons: adding payments or documents is
    O2b, approval is O4.

    Totals are summed here from the prefetched rows rather than read from the
    VendorOrder properties, which issue one aggregate each. Same arithmetic: `paid` is
    CONFIRMED payments only, `invoiced` is invoice documents' amounts.
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

    total    = sum((line.amount for line in lines), Decimal('0'))
    paid     = sum((p.amount for p in payments if p.status == PaymentRequest.CONFIRMED),
                   Decimal('0'))
    invoiced = sum((d.invoice_amount or Decimal('0') for d in documents
                    if d.doc_type == VENDOR_ORDER_DOC_INVOICE), Decimal('0'))

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
        'total':           total,
        'paid':            paid,
        'balance':         total - paid,
        'invoiced':        invoiced,
        # Money has gone out against invoices that have not come in.
        'invoice_awaited': paid > invoiced,
    })
