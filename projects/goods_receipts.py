"""
NTF-DCG 4a — goods receipts: the numbered, append-only journal of GRN submissions.

Every successful confirm_grn (kind 'receipt') and override_grn (kind 'correction')
writes one GoodsReceipt holding exactly what that submission recorded, so Finance can
cite "GRN-000123" when reconciling vendor payments. The DC line's receipt columns stay
the source of truth; nothing here changes what any screen, recalculate_dc_status or the
delivery mirrors read (see the section note above GoodsReceipt in models.py).

THE ORDER in both GRN views, around grn_damage's (see that module's docstring):

    _read_grn_lines()         every posted line
    changed_grn_lines()       keep only lines this submission changes (ruling P3, WA)
    ... reason / damage refusals ...
    NOTHING_TO_RECORD         refused when no line is left (G4), before any upload
    -- transaction.atomic() --
    lock_challan(), reload_grn_lines(), recheck_damage_plan()
    changed_grn_lines()       asked again of the fresh rows; none left = changed meanwhile
    (the view's line saves, the changed lines only)
    write_goods_receipt()     from the fresh, saved rows (G2)
    record_damage(receipt=)   photo rows carry the receipt (ruling P5)
    link_receipt_documents()  4b: the documents' rows carry the receipt (ruling A5)

NTF-DCG 4b — DOCUMENTS AT RECEIPT. A signed DC copy, LR/bilty or delivery photo may be
attached to either GRN form, each with its type (A2), at most five (A3). They are
DeliveryChallanAttachment rows with `receipt` set — DC-B2's model, bucket, validator,
uploader, discard, open link and withdraw, reused (A1). In the views:

    ... damage refusals, then G4 (a documents-only submission is refused there, Q2) ...
    plan_receipt_documents()  type, count, tick, storage, each file — before any upload
    upload_planned_photos()   unchanged, then
    upload_receipt_documents() the same DC-B2 uploader, folder 'grn-document' (Q7)
    -- transaction.atomic() -- ... as above, ending with link_receipt_documents()
    any refusal or rollback   discard_stored() AND discard_receipt_documents()

This module imports models, delivery_storage and grn_damage; it is imported by views.py
and by the backfill command and imports neither, so there is no cycle.
"""
import re

from django.db.models import Prefetch

from .delivery_storage import (
    RECEIPT_DOCUMENT_PATH_KIND, DeliveryStorageError, delivery_bucket,
    discard_unrecorded_dc_document, upload_dc_document, validate_dc_document,
)
from .grn_damage import photo_field
from .models import (
    RECEIPT_DOCUMENT_TYPE_CHOICES, DeliveryChallanAttachment, GoodsReceipt, GoodsReceiptLine,
)

#: G4: a submission in which no line is recorded or changed.
NOTHING_TO_RECORD = 'Nothing to record — enter a received quantity on at least one line.'

# ---------------------------------------------------------------------------
# NTF-DCG 4b — documents attached at receipt
# ---------------------------------------------------------------------------

#: A3: at most this many documents in one GRN submission; more refuses the whole GRN.
#: The forms render this many slots (tests_receipt_attachments pins the two together).
RECEIPT_DOCUMENTS_MAX_FILES = 5

#: One file input and one type select per slot, numbered from 1. Slots, not one
#: `multiple` input: a type is chosen per file (A2), and a `multiple` input cannot carry
#: one per file.
RECEIPT_DOCUMENT_FIELD = 'receipt_doc_{}'
RECEIPT_DOCUMENT_TYPE_FIELD = 'receipt_doc_type_{}'
_RECEIPT_DOCUMENT_FIELD_RE = re.compile(r'^receipt_doc_(\d+)$')

#: A4: the uploader's tick that no document shows a price, one per submission as DC-B2.
RECEIPT_DOCUMENTS_NO_PRICES_FIELD = 'receipt_docs_no_prices'

#: Ruling Q2 (option b): a submission carrying only documents is refused by G4, in these
#: words. A separate "add documents to an existing receipt" action is NTF-DCG 4b-2.
DOCUMENTS_NEED_A_QUANTITY = ('Documents can only be attached with a quantity change for '
                             'now — enter the received quantity, or ask SCM.')
RECEIPT_DOCUMENTS_NO_PRICES = ('Tick "These documents show no rates, values or amounts" to '
                               'attach them — Site Engineers and Finance will see them.')
RECEIPT_DOCUMENT_STORAGE_OFF = ('Delivery documents cannot be stored right now: the private '
                                'delivery bucket is not configured. Nothing was saved — '
                                'remove the documents to record the receipt without them.')
_RECEIPT_DOCUMENT_TYPES = frozenset(value for value, _ in RECEIPT_DOCUMENT_TYPE_CHOICES)


def _chosen_documents(request):
    """[(slot, type posted, file)] for every file in a receipt-document slot, in slot
    order. Every `receipt_doc_N` key is read, not only 1..5, so a sixth file from a
    crafted form is counted and refused, never dropped silently."""
    chosen = []
    for key in request.FILES:
        match = _RECEIPT_DOCUMENT_FIELD_RE.match(key)
        if not match:
            continue
        slot = int(match.group(1))
        doc_type = (request.POST.get(RECEIPT_DOCUMENT_TYPE_FIELD.format(slot)) or '').strip()
        chosen.extend((slot, doc_type, upload)
                      for upload in request.FILES.getlist(key) if upload and upload.name)
    chosen.sort(key=lambda entry: entry[0])
    return chosen


def receipt_documents_chosen(request):
    """True when the submission carries at least one receipt document (for G4's message)."""
    return bool(_chosen_documents(request))


def plan_receipt_documents(request):
    """Decide the documents this GRN attaches. Returns ([(type, file)], errors).

    Writes nothing and uploads nothing, so the views call it before any upload (A5) and a
    refusal leaves no trace. Refused: more than RECEIPT_DOCUMENTS_MAX_FILES; a file with
    no type or an unknown one (A2); no no-prices tick (A4); storage off; then each file
    through DC-B2's validate_dc_document (PDF/JPG/PNG, 20 MB, MIME, leading bytes). A type
    chosen in a slot with no file is ignored: it records nothing.
    """
    chosen = _chosen_documents(request)
    if not chosen:
        return [], []
    errors = []
    if len(chosen) > RECEIPT_DOCUMENTS_MAX_FILES:
        errors.append(f'At most {RECEIPT_DOCUMENTS_MAX_FILES} documents can be attached to '
                      f'one receipt; {len(chosen)} were chosen.')
    for _slot, doc_type, upload in chosen:
        if doc_type not in _RECEIPT_DOCUMENT_TYPES:
            errors.append(f'{upload.name}: choose what this document is — Signed DC copy, '
                          f'LR or bilty, Delivery photo or Other.')
    if request.POST.get(RECEIPT_DOCUMENTS_NO_PRICES_FIELD) != '1':
        errors.append(RECEIPT_DOCUMENTS_NO_PRICES)
    if errors:
        return [], errors
    if not delivery_bucket():
        return [], [RECEIPT_DOCUMENT_STORAGE_OFF]
    for _slot, _type, upload in chosen:
        try:
            validate_dc_document(upload)
        except DeliveryStorageError as exc:
            errors.append(str(exc))
    if errors:
        return [], errors
    return [(doc_type, upload) for _slot, doc_type, upload in chosen], []


def upload_receipt_documents(documents, project):
    """Upload every planned document with DC-B2's uploader, in the 'grn-document' folder.
    Returns [(type, stored dict)]. Raises DeliveryStorageError after discarding whatever
    this run already stored, as grn_damage.upload_planned_photos() does."""
    stored = []
    try:
        for doc_type, upload in documents:
            stored.append((doc_type, upload_dc_document(
                upload, project, path_kind=RECEIPT_DOCUMENT_PATH_KIND)))
    except DeliveryStorageError:
        discard_receipt_documents(stored)
        raise
    return stored


def discard_receipt_documents(stored):
    """Remove uploads no attachment row records — after a refused or rolled-back GRN.
    DC-B2's discard, which never removes a recorded document. Never raises."""
    for _type, upload in stored:
        discard_unrecorded_dc_document(upload)


def link_receipt_documents(receipt, stored, uploaded_by):
    """Write one attachment row per stored document, on `receipt` and its challan. Call
    inside the GRN view's transaction, after write_goods_receipt() (A5), so a rollback
    takes the rows back and the views' discard then finds the objects unrecorded.
    no_prices_confirmed is True because plan_receipt_documents() refused the submission
    without the tick. One INSERT per document (at most five); create(), not bulk_create(),
    so the model's append-only save() is the one that runs."""
    return [
        DeliveryChallanAttachment.objects.create(
            challan=receipt.challan, receipt=receipt, document_type=doc_type,
            uploaded_by=uploaded_by, original_name=upload['original_name'],
            bucket=upload['bucket'], path=upload['path'], size=upload['size'],
            mime=upload['mime'], no_prices_confirmed=True,
        )
        for doc_type, upload in stored
    ]


def line_changed(item, received_qty, damaged_qty, grn_notes, files):
    """True when posting these values records something on `item` (ruling P3).

    Compared with the row as it stands: the received quantity (None to a value
    included), the damaged quantity, the note — or photos attached. Condition is NOT
    compared: _read_grn_lines derives it from received and damaged, so it only moves
    with them, and a condition-only difference on an old row is not a recording.
    Decimal comparison, so 150 and 150.00 are the same value.
    """
    return bool(files
                or item.received_quantity != received_qty
                or item.damaged_quantity != damaged_qty
                or (item.grn_notes or '') != grn_notes)


def changed_grn_lines(request, grn_lines):
    """`grn_lines` (_read_grn_lines' tuples) narrowed to the lines this submission
    changes. Both GRN forms pre-fill every recorded line, so an untouched line is posted
    back with its own values; before 4a it was saved again anyway, re-stamping its GRN
    date and confirmer with today and the current user (ruling WA ends that)."""
    return [line for line in grn_lines
            if line_changed(line[0], line[1], line[2], line[4],
                            request.FILES.getlist(photo_field(line[0])))]


def write_goods_receipt(challan, *, kind, recorded_by, receipt_date, entries,
                        on_behalf_reason=''):
    """Write one receipt and its lines. Call inside the GRN view's transaction, after
    the line saves, with the challan row locked (G2). Two queries for the receipt
    (INSERT, then its number) and one for the lines.

    `entries` is [(saved DC line, received before, damaged before)]: the line as this
    submission left it, and its two quantities as they stood under the lock before it.
    A line with no received quantity before has no "before" at all, so damaged_before
    is NULL with it rather than the column's default 0.
    """
    receipt = GoodsReceipt.objects.create(
        challan=challan, kind=kind, recorded_by=recorded_by, receipt_date=receipt_date,
        on_behalf_reason=on_behalf_reason,
    )
    GoodsReceiptLine.objects.bulk_create([
        GoodsReceiptLine(
            receipt=receipt, dc_line=item,
            received_before=received_before,
            received_after=item.received_quantity,
            damaged_before=None if received_before is None else damaged_before,
            damaged_after=item.damaged_quantity,
            condition=item.condition or '', grn_notes=item.grn_notes or '',
            item_description=item.item_description, unit=item.unit,
            ordered_quantity=item.ordered_quantity,
        )
        for item, received_before, damaged_before in entries
    ])
    return receipt


def documents_message(documents, receipt):
    """The sentence a GRN's success message ends with when documents were attached, or
    '' — the same shape as grn_damage.damage_message()."""
    if not documents:
        return ''
    count = len(documents)
    return f' {count} document{"s" if count != 1 else ""} attached to {receipt.number}.'


def receipts_for_challan(challan, *, include_withdrawn=False):
    """The challan's receipts, newest first, each with `lines` prefetched in order and
    the recorder's login joined — for the DC page's Receipts section (G6). Three queries
    whatever the number of receipts, one when there are none.

    NTF-DCG 4b (A7): each receipt's `documents` are prefetched too, oldest first. A
    WITHDRAWN document is fetched only when `include_withdrawn` — the caller passes
    user_can_manage_dc_documents(), SCM and Admin (ruling Q3, DC-B2's rule) — and is left
    out in the query for everyone else, so the template cannot show one, or that one
    exists, by mistake.
    """
    documents = (DeliveryChallanAttachment.objects
                 .select_related('uploaded_by__user', 'withdrawn_by__user')
                 .order_by('uploaded_at', 'pk'))
    if not include_withdrawn:
        documents = documents.filter(withdrawn_at__isnull=True)
    return list(
        GoodsReceipt.objects.filter(challan=challan)
        .select_related('recorded_by__user')
        .prefetch_related(Prefetch('lines', queryset=GoodsReceiptLine.objects.order_by('pk')),
                          Prefetch('documents', queryset=documents))
        .order_by('-pk')
    )
