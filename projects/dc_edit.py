"""
DC-B3 — correcting a delivery challan after it was created, by SCM or Admin, with a
reason and an append-only history (DeliveryChallanRevision). A receipt is never
rewritten here.

WHAT MAY CHANGE (rulings E2-E4, E8):
  * the header, in any status: vendor, DC number, DC date, expected delivery date,
    warehouse, PO reference, notes — checked as create_delivery_challan checks them;
  * a line WITHOUT a receipt (has_receipt() below): category, description, ordered
    quantity, unit; it may be removed;
  * a line WITH a receipt: description and unit only — never removed;
  * new lines, in any status; at least one line always remains;
  * new challan documents, with DC-B2's checks, at most DC_DOCUMENTS_MAX_FILES live.
The status is never edited: recalculate_dc_status() runs after every save, exactly as
after a GRN, and an edit cannot move it (only received lines decide it, and their
quantities are untouchable here).

THE ORDER, which is what makes it all-or-nothing (the GRN views' shape):

    read_edit()             parses and checks; writes nothing, uploads nothing
    upload_dc_document()    before the transaction; a partial run discards itself
    -- transaction.atomic() --
    lock_challan()          grn_damage's lock; then everything the form relied on is
                            re-checked under it (E7)
    header, lines, documents, recalculate_dc_status(), the revision row, the feed line

and on ANY exception from the block the uploads are discarded.

This module imports models, storage, forms, number_input and grn_damage's lock; it is
imported by views.py and never imports it, so there is no cycle.
"""
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Exists, OuterRef

from .delivery_storage import (
    DC_DOCUMENTS_MAX_FILES, DeliveryStorageError, delivery_bucket,
    discard_unrecorded_dc_document, upload_dc_document, validate_dc_document,
)
from .forms import check_typed_date
from .grn_damage import lock_challan
from .models import (
    DCLineDamagePhoto, DCLineItem, DeliveryChallanAttachment, DeliveryChallanRevision,
    REASON_DC_EDITED, StockLocation, Vendor, log_activity, recalculate_dc_status,
)
from .number_input import decimal_field_max, parse_decimal_input

#: A reason shorter than this says nothing a later reader of the history can use.
DC_EDIT_REASON_MIN = 10

#: challan_snapshot()'s shape; a reader of an old revision checks it.
DC_EDIT_SNAPSHOT_SCHEMA = 1

# Its own key, not the GRN draft's or the issue draft's: a refused edit must never
# overwrite another form's refused values, or the other way round.
_DRAFT_SESSION_KEY = 'dc_edit_draft'

CHANGED_ELSEWHERE = ('This challan was changed while you were editing it — a GRN was '
                     'recorded or someone else saved an edit. Nothing was saved. The page '
                     'now shows the challan as it is; make your changes again.')
NOTHING_CHANGED = 'Nothing changed — the challan is exactly as it was. Nothing was saved.'
# delivery_storage.DELIVERY_STORAGE_OFF ends "to create the challan without them", which
# is the create form's sentence; this page needs its own.
EDIT_STORAGE_OFF = ('Challan documents cannot be stored right now: the private delivery '
                    'bucket is not configured. Nothing was saved — remove the attached '
                    'files to save the other changes.')

#: The receipt columns. The edit form has no input for any of them, so a POST naming
#: one for a line is not this form's, and is refused rather than ignored (E4).
RECEIPT_FIELDS = ('received_quantity', 'damaged_quantity', 'condition', 'grn_date',
                  'grn_confirmed_by', 'grn_notes', 'grn_on_behalf', 'grn_on_behalf_reason')

#: The four columns this module writes on an existing line, and the only ones: saved
#: with update_fields, so a line instance read before a GRN can never write its stale
#: receipt columns back.
LINE_EDIT_FIELDS = ('boq_category', 'item_description', 'ordered_quantity', 'unit')

_CATEGORIES = frozenset(value for value, _label in DCLineItem.CATEGORY_CHOICES)
_NEW_LINE_KEY = re.compile(r'^new_line_(?:category|description|qty|unit)_(\d+)$')


class EditRefused(Exception):
    """Raised inside the transaction when the re-check under the lock fails, so the
    block rolls back. str(exc) is the message for the person editing."""


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def has_receipt(line):
    """True if a GRN has touched `line` (approved pre-flight 2).

    ANY receipt column, not received_quantity alone. confirm_grn writes all of them;
    override_grn before G2 left grn_confirmed_by NULL, and a deleted confirmer's profile
    nulls it too, so no single column is enough on old rows. A received quantity of 0 is
    a receipt (nothing arrived). Damage photos count, live or withdrawn: a line that has
    evidence against it is a received line. `line` must come from lines_for_edit(),
    which annotates `_has_photo`.
    """
    return (line.received_quantity is not None
            or line.grn_date is not None
            or line.grn_confirmed_by_id is not None
            or line.grn_on_behalf
            or bool(line.condition)
            or line.damaged_quantity > 0
            or bool(line.grn_notes)
            or line._has_photo)


def lines_for_edit(challan):
    """Every line of `challan` in pk order, each with `has_receipt` set. One query."""
    lines = list(
        challan.line_items
        .annotate(_has_photo=Exists(DCLineDamagePhoto.objects.filter(line=OuterRef('pk'))))
        .order_by('pk'))
    for line in lines:
        line.has_receipt = has_receipt(line)
    return lines


def live_document_count(challan):
    """Live challan documents on `challan`; withdrawn ones do not count (ruling Q-D)."""
    return DeliveryChallanAttachment.objects.filter(
        challan=challan, withdrawn_at__isnull=True).count()


def vendor_choices(challan):
    """The vendor dropdown: every active vendor, plus the challan's own if it has since
    been deactivated — kept, never cleared by opening the form (ruling Q-C)."""
    vendors = list(Vendor.objects.filter(is_active=True).order_by('name'))
    if challan.vendor is not None and not challan.vendor.is_active:
        vendors.append(challan.vendor)
    return vendors


def warehouse_choices(challan):
    """The warehouse dropdown, as vendor_choices(): active ones by code, plus the
    challan's own if it has since been deactivated."""
    warehouses = list(StockLocation.objects.filter(is_active=True).order_by('code'))
    current = challan.issued_from_warehouse
    if current is not None and not current.is_active:
        warehouses.append(current)
    return warehouses


# ---------------------------------------------------------------------------
# The snapshot — schema 1
# ---------------------------------------------------------------------------

def challan_snapshot(challan, lines):
    """What `challan` says now, JSON-safe, display names beside ids (the
    ApprovalRoundSnapshot precedent). Schema 1:

      schema, header {vendor {id, name} | null, dc_number, dc_date, expected_delivery_date
                      (ISO | null), issued_from_warehouse {id, code, name} | null,
                      po_number, notes},
      lines [{id, boq_category, item_description, ordered_quantity, unit, has_receipt}]

    Lines in pk order; ordered_quantity a string with two places ("12.00") — a JSON
    number would be a float. save_challan_edit() adds documents_added [ids] to `after`.
    """
    vendor, warehouse = challan.vendor, challan.issued_from_warehouse
    return {
        'schema': DC_EDIT_SNAPSHOT_SCHEMA,
        'header': {
            'vendor': {'id': vendor.pk, 'name': vendor.name} if vendor else None,
            'dc_number': challan.dc_number,
            'dc_date': challan.dc_date.isoformat(),
            'expected_delivery_date': (challan.expected_delivery_date.isoformat()
                                       if challan.expected_delivery_date else None),
            'issued_from_warehouse': ({'id': warehouse.pk, 'code': warehouse.code,
                                       'name': warehouse.name} if warehouse else None),
            'po_number': challan.po_number,
            'notes': challan.notes,
        },
        'lines': [{
            'id': line.pk,
            'boq_category': line.boq_category,
            'item_description': line.item_description,
            'ordered_quantity': f'{line.ordered_quantity:.2f}',
            'unit': line.unit,
            'has_receipt': line.has_receipt,
        } for line in sorted(lines, key=lambda line: line.pk)],
    }


_HEADER_LABELS = (
    ('vendor', 'Vendor'), ('dc_number', 'DC number'), ('dc_date', 'DC date'),
    ('expected_delivery_date', 'Expected delivery'), ('issued_from_warehouse', 'Warehouse'),
    ('po_number', 'PO reference'), ('notes', 'Notes'),
)
_LINE_LABELS = (('boq_category', 'category'), ('item_description', 'description'),
                ('ordered_quantity', 'ordered'), ('unit', 'unit'))


def _shown(key, value):
    if not value:
        return '—'
    if key == 'vendor':
        return value['name']
    if key == 'issued_from_warehouse':
        return value['code']
    if key in ('dc_date', 'expected_delivery_date'):
        return date.fromisoformat(value).strftime('%d %b %Y')
    return value


def _line_text(line):
    return (f"{line['boq_category']} — {line['item_description']} "
            f"({line['ordered_quantity']} {line['unit']})")


def challan_revisions(challan):
    """The challan's edits, newest first, each with `changes` (revision_changes()) for
    the history on the challan page. One query."""
    revisions = list(DeliveryChallanRevision.objects.filter(challan=challan)
                     .select_related('revised_by__user').order_by('-revision_no'))
    for revision in revisions:
        revision.changes = revision_changes(revision)
    return revisions


def revision_changes(revision):
    """What one revision changed, one sentence per change, read from its two snapshots
    (no query). The history on the challan page lists these (E9). Notes are named, not
    quoted, to keep the list short; document names are never shown (ruling Q-B)."""
    before, after, changes = revision.before, revision.after, []
    for key, label in _HEADER_LABELS:
        old, new = before['header'].get(key), after['header'].get(key)
        if old == new:
            continue
        if key == 'notes':
            changes.append('Notes changed')
        else:
            changes.append(f'{label}: {_shown(key, old)} → {_shown(key, new)}')

    old_lines = {line['id']: line for line in before['lines']}
    new_lines = {line['id']: line for line in after['lines']}
    for pk in sorted(old_lines.keys() | new_lines.keys()):
        old, new = old_lines.get(pk), new_lines.get(pk)
        if new is None:
            changes.append(f'Line removed: {_line_text(old)}')
        elif old is None:
            changes.append(f'Line added: {_line_text(new)}')
        else:
            parts = [f'{label} {old[key]} → {new[key]}'
                     for key, label in _LINE_LABELS if old[key] != new[key]]
            if parts:
                changes.append(f'Line "{old["item_description"]}": ' + '; '.join(parts))

    added = len(after.get('documents_added', []))
    if added:
        changes.append(f'{added} challan document{"s" if added != 1 else ""} added')
    return changes


# ---------------------------------------------------------------------------
# The draft — a refused edit keeps what was typed (E11)
# ---------------------------------------------------------------------------
#
# The form posts and redirects, as the GRN forms do, so a refresh never re-posts. A
# refusal parks the raw typed strings in the session, tagged with the challan, and the
# edit page takes them out ONCE. Files cannot be put back into a file input, which is
# why the page says documents must be attached again.

def stash_edit_draft(request, challan, keep_typed=True):
    """Park the refused POST for the next render of the edit page. `keep_typed` False
    keeps the reason alone: after CHANGED_ELSEWHERE the page must show the challan as it
    now is, not the values typed over what it was."""
    post = request.POST
    if keep_typed:
        values = {key: post.get(key, '') for key in post if key != 'csrfmiddlewaretoken'}
    else:
        values = {'reason': post.get('reason', '')}
    request.session[_DRAFT_SESSION_KEY] = {
        'where': f'dc_edit:{challan.pk}',
        'values': values,
        'documents_dropped': any(f and f.name for f in request.FILES.getlist('challan_documents')),
    }


def pop_edit_draft(request, challan):
    """The draft stashed for `challan`, or None. Removed from the session when returned,
    so only the next render shows it."""
    draft = request.session.get(_DRAFT_SESSION_KEY)
    if not draft or draft.get('where') != f'dc_edit:{challan.pk}':
        return None
    del request.session[_DRAFT_SESSION_KEY]
    return draft


def edit_form_values(challan, lines, draft):
    """What the edit form shows: the draft's typed strings where there is a draft, the
    challan's own values otherwise. The hidden revision count and line ids are never
    taken from the draft — they always describe the challan as it is now."""
    typed = draft['values'] if draft else {}

    def value(key, current):
        return typed.get(key, current) if draft else current

    header = {
        'vendor_id': value('vendor_id', str(challan.vendor_id or '')),
        'po_number': value('po_number', challan.po_number),
        'issued_from_warehouse': value('issued_from_warehouse',
                                       str(challan.issued_from_warehouse_id or '')),
        'dc_number': value('dc_number', challan.dc_number),
        'dc_date': value('dc_date', challan.dc_date.isoformat()),
        'expected_delivery_date': value(
            'expected_delivery_date',
            challan.expected_delivery_date.isoformat() if challan.expected_delivery_date else ''),
        'notes': value('notes', challan.notes),
    }
    rows = []
    for line in lines:
        prefix = f'line_{line.pk}_'
        rows.append({
            'line': line,
            'category': value(prefix + 'category', line.boq_category),
            'description': value(prefix + 'description', line.item_description),
            'qty': value(prefix + 'qty', str(line.ordered_quantity)),
            'unit': value(prefix + 'unit', line.unit),
            'remove': typed.get(prefix + 'remove') == '1',
        })
    new_rows = [{
        'category': typed.get(f'new_line_category_{i}', ''),
        'description': typed.get(f'new_line_description_{i}', ''),
        'qty': typed.get(f'new_line_qty_{i}', ''),
        'unit': typed.get(f'new_line_unit_{i}', 'Nos'),
    } for i in _new_line_indexes(typed)]
    return {
        'form': header,
        'line_rows': rows,
        'new_rows': new_rows,
        'reason': typed.get('reason', ''),
        'documents_dropped': bool(draft and draft.get('documents_dropped')),
        'line_ids': ','.join(str(line.pk) for line in lines),
        'editable_line_ids': ','.join(str(line.pk) for line in lines if not line.has_receipt),
    }


# ---------------------------------------------------------------------------
# The plan — every refusal, before any upload
# ---------------------------------------------------------------------------

@dataclass
class EditPlan:
    reason: str = ''
    # What the form was opened with (hidden fields), re-checked against the challan
    # before the lock and again under it (E7).
    opened_revision: int = None
    opened_line_ids: frozenset = frozenset()
    opened_editable_ids: frozenset = frozenset()
    header: dict = field(default_factory=dict)
    lines: dict = field(default_factory=dict)       # pk -> {LINE_EDIT_FIELDS: value}
    removed: set = field(default_factory=set)       # pks
    new_lines: list = field(default_factory=list)   # [{LINE_EDIT_FIELDS: value}]
    documents: list = field(default_factory=list)   # the chosen files, in the order posted


def _int_or_none(raw):
    try:
        return int((raw or '').strip())
    except ValueError:
        return None


def _id_set(raw):
    ids = set()
    for part in (raw or '').split(','):
        number = _int_or_none(part)
        if number is not None:
            ids.add(number)
    return frozenset(ids)


def _new_line_indexes(values):
    """The new-line row numbers present in `values`, sorted. Read from every key, not
    counted up from 0: removing a row in the browser leaves a gap in the numbering, and
    a counter stops at the gap (SECONDARY_FINDINGS: the create form does)."""
    found = set()
    for key in values:
        match = _NEW_LINE_KEY.match(key)
        if match:
            found.add(int(match.group(1)))
    return sorted(found)


def _resolve_choice(raw, current, model, label, errors):
    """The vendor or warehouse the form chose. Blank is a choice (none). The challan's
    current one is kept even if it has since been deactivated; any other id must be an
    active row, and an unknown or inactive one is REFUSED — never silently cleared, as
    the create form does (ruling Q-C)."""
    raw = (raw or '').strip()
    if not raw:
        return None
    if current is not None and raw == str(current.pk):
        return current
    try:
        return model.objects.get(pk=int(raw), is_active=True)
    except (ValueError, model.DoesNotExist):
        errors.append(f'{label}: the one chosen is not available any more. Choose another.')
        return current


def _check_text(value, label, max_length, errors, required=False):
    if required and not value:
        errors.append(f'{label} is required.')
    elif len(value) > max_length:
        errors.append(f'{label}: at most {max_length} characters ({len(value)} typed).')


def _read_header(post, challan, errors):
    dc_number = post.get('dc_number', '').strip()
    po_number = post.get('po_number', '').strip()
    _check_text(dc_number, 'DC Number', 100, errors, required=True)
    _check_text(po_number, 'PO Number', 100, errors)

    # check_typed_date, as create: the 2020..today+5y window, and a bad date refused,
    # never dropped.
    dc_date_raw = post.get('dc_date', '').strip()
    dc_date, dc_date_error = check_typed_date(dc_date_raw)
    if not dc_date_raw:
        errors.append('DC Date is required.')
    elif dc_date_error:
        errors.append(f'DC Date: {dc_date_error}')
    expected, expected_error = check_typed_date(post.get('expected_delivery_date', '').strip())
    if expected_error:
        errors.append(f'Expected Delivery Date: {expected_error}')

    return {
        'vendor': _resolve_choice(post.get('vendor_id'), challan.vendor, Vendor,
                                  'Vendor', errors),
        'issued_from_warehouse': _resolve_choice(
            post.get('issued_from_warehouse'), challan.issued_from_warehouse,
            StockLocation, 'Warehouse', errors),
        'dc_number': dc_number,
        'dc_date': dc_date,
        'expected_delivery_date': expected,
        'po_number': po_number,
        'notes': post.get('notes', '').strip(),
    }


def _read_qty(raw, label, errors):
    """An ordered quantity as create reads it: two places, at least 0.01 (a dispatched
    quantity of zero is meaningless), within the column. Required — an existing line has
    no "abandoned row" to drop, and a partly filled new row is refused."""
    try:
        qty = parse_decimal_input(
            raw, places=2, min_value=Decimal('0.01'),
            max_value=decimal_field_max(DCLineItem, 'ordered_quantity'),
            field_label=f'{label}: quantity')
    except ValidationError as exc:
        errors.extend(exc.messages)
        return None
    if qty is None:
        errors.append(f'{label}: quantity is required.')
    return qty


def _read_line_fields(post, key, label, errors, *, category_and_qty=True):
    """Description and unit, and category and quantity when asked, from one row.
    `key(name)` is the POST key of that row's `name` field."""
    description = post.get(key('description'), '').strip()
    _check_text(description, f'{label}: description', 255, errors, required=True)
    unit = post.get(key('unit'), '').strip() or 'Nos'
    _check_text(unit, f'{label}: unit', 20, errors)
    values = {'item_description': description, 'unit': unit}
    if category_and_qty:
        category = post.get(key('category'), '').strip()
        if category not in _CATEGORIES:
            errors.append(f'{label}: choose a category.')
        values.update(boq_category=category,
                      ordered_quantity=_read_qty(post.get(key('qty')), label, errors))
    return values


def _read_lines(post, lines, errors):
    """The existing lines. Returns ({pk: values}, removed pks)."""
    values, removed = {}, set()
    for line in lines:
        prefix = f'line_{line.pk}_'

        def key(name, prefix=prefix):
            return prefix + name

        label = f'Line "{line.item_description}"'
        if any(prefix + name in post for name in RECEIPT_FIELDS):
            errors.append(f'{label}: receipt details are recorded through the GRN, '
                          f'never on this page.')
        if post.get(prefix + 'remove') == '1':
            if line.has_receipt:
                errors.append(f'{label}: material has been received against it, so it '
                              f'cannot be removed.')
            else:
                removed.add(line.pk)
                continue
        if not line.has_receipt:
            values[line.pk] = _read_line_fields(post, key, label, errors)
            continue
        # E4: a received line keeps its category and ordered quantity. The form renders
        # neither, so they arrive only from a page opened before the GRN (caught as
        # CHANGED_ELSEWHERE first) or a hand-made POST; a value that differs is refused.
        posted_category = post.get(prefix + 'category')
        posted_qty = (post.get(prefix + 'qty') or '').strip()
        if posted_category is not None and posted_category.strip() != line.boq_category:
            errors.append(f'{label}: material has been received against it, so its '
                          f'category cannot change.')
        if posted_qty:
            try:
                same = parse_decimal_input(posted_qty, places=2, field_label=label) \
                    == line.ordered_quantity
            except ValidationError:
                same = False
            if not same:
                errors.append(f'{label}: material has been received against it, so its '
                              f'ordered quantity cannot change.')
        values[line.pk] = dict(
            _read_line_fields(post, key, label, errors, category_and_qty=False),
            boq_category=line.boq_category, ordered_quantity=line.ordered_quantity)
    return values, removed


def _read_new_lines(post, errors):
    """Lines added on this page. A row left wholly blank is dropped, as on create; a
    row partly filled is refused, never dropped."""
    new_lines = []
    for number, i in enumerate(_new_line_indexes(post), start=1):

        def key(name, i=i):
            return f'new_line_{name}_{i}'

        if not any(post.get(key(name), '').strip() for name in ('category', 'description', 'qty')):
            continue
        new_lines.append(_read_line_fields(post, key, f'New line {number}', errors))
    return new_lines


def _changed_elsewhere(plan, challan, lines):
    """True when the challan is not the one the form was opened on (E7): another edit
    was saved (the revision count moved, or the line set), or a line the form offered
    as editable now has a receipt (an engineer confirmed a GRN meanwhile)."""
    if plan.opened_revision != challan.revisions.count():
        return True
    if plan.opened_line_ids != frozenset(line.pk for line in lines):
        return True
    return any(line.has_receipt for line in lines if line.pk in plan.opened_editable_ids)


def _document_errors(documents, live):
    """DC-B2's checks for documents added on this page, the count included: live ones
    already on the challan plus these, at most DC_DOCUMENTS_MAX_FILES (ruling Q-D)."""
    errors = []
    if live + len(documents) > DC_DOCUMENTS_MAX_FILES:
        errors.append(f'At most {DC_DOCUMENTS_MAX_FILES} challan documents can be on a '
                      f'challan; it has {live} and {len(documents)} were chosen. Nothing '
                      f'was saved.')
    return errors


def read_edit(request, challan, lines):
    """Parse and check one posted edit against the challan as read before the lock.
    Returns (plan, errors). Writes nothing and uploads nothing, so a refusal here leaves
    no trace. A challan that changed since the form was opened answers CHANGED_ELSEWHERE
    alone: every other message would be about values the person has not seen."""
    post = request.POST
    plan = EditPlan(
        opened_revision=_int_or_none(post.get('revision_count')),
        opened_line_ids=_id_set(post.get('line_ids')),
        opened_editable_ids=_id_set(post.get('editable_line_ids')),
    )
    if _changed_elsewhere(plan, challan, lines):
        return None, [CHANGED_ELSEWHERE]

    errors = []
    plan.reason = post.get('reason', '').strip()
    if len(plan.reason) < DC_EDIT_REASON_MIN:
        errors.append(f'Give a reason for the edit of at least {DC_EDIT_REASON_MIN} '
                      f'characters. Nothing was saved.')
    plan.header = _read_header(post, challan, errors)
    plan.lines, plan.removed = _read_lines(post, lines, errors)
    plan.new_lines = _read_new_lines(post, errors)
    if len(plan.lines) + len(plan.new_lines) < 1:
        errors.append('A challan keeps at least one line. Nothing was saved.')

    # DC-B2's document checks, last, so a file is refused for itself and not for a
    # field it has nothing to do with. Every file is validated before any is uploaded.
    # An empty <input type=file> posts one nameless part on some browsers; it is no file.
    plan.documents = [f for f in request.FILES.getlist('challan_documents') if f and f.name]
    if plan.documents:
        errors += _document_errors(plan.documents, live_document_count(challan))
        # Site Engineers open these documents, so they must carry no price. The tick is
        # the uploader's word for that; without it nothing is stored.
        if request.POST.get('no_prices_confirmed') != '1':
            errors.append('Tick "These documents show no rates, values or amounts" to '
                          'attach them — Site Engineers will see them. Nothing was saved.')
        if not delivery_bucket():
            errors.append(EDIT_STORAGE_OFF)
        for document in plan.documents:
            try:
                validate_dc_document(document)
            except DeliveryStorageError as exc:
                errors.append(str(exc))
    if errors:
        return None, errors
    return plan, []


def _header_values(challan):
    return {'vendor': challan.vendor_id, 'dc_number': challan.dc_number,
            'dc_date': challan.dc_date, 'expected_delivery_date': challan.expected_delivery_date,
            'issued_from_warehouse': challan.issued_from_warehouse_id,
            'po_number': challan.po_number, 'notes': challan.notes}


def _planned_header_values(plan):
    header = dict(plan.header)
    header['vendor'] = header['vendor'].pk if header['vendor'] else None
    header['issued_from_warehouse'] = (header['issued_from_warehouse'].pk
                                       if header['issued_from_warehouse'] else None)
    return header


def _line_changed(line, values):
    # Decimal compares by value, so "12" typed against a stored 12.00 is no change.
    return any(getattr(line, name) != values[name] for name in LINE_EDIT_FIELDS)


def _has_changes(plan, challan, lines):
    if _planned_header_values(plan) != _header_values(challan):
        return True
    if plan.removed or plan.new_lines or plan.documents:
        return True
    return any(_line_changed(line, plan.lines[line.pk])
               for line in lines if line.pk in plan.lines)


# ---------------------------------------------------------------------------
# The save
# ---------------------------------------------------------------------------

def save_challan_edit(request, project, challan, profile):
    """Apply one posted edit to `challan`. Returns (revision, errors): the new
    DeliveryChallanRevision and [], or None and the refusal messages, in which case
    nothing was written and nothing uploaded is left in storage. Any failure that is not
    a refusal discards the uploads and is re-raised."""
    lines = lines_for_edit(challan)
    plan, errors = read_edit(request, challan, lines)
    if errors:
        return None, errors
    # E6: a save that changes nothing is refused before any upload. A save that only
    # adds documents is a change (ruling Q-B).
    if not _has_changes(plan, challan, lines):
        return None, [NOTHING_CHANGED]

    # Uploaded before the transaction, so a storage failure refuses the edit with nothing
    # written. A partial run discards what it already stored.
    stored = []
    try:
        for document in plan.documents:
            stored.append(upload_dc_document(document, project))
    except DeliveryStorageError as exc:
        for upload in stored:
            discard_unrecorded_dc_document(upload)
        return None, [str(exc)]

    try:
        with transaction.atomic():
            revision = _write_edit(plan, project, challan, profile, stored)
    except EditRefused as exc:
        # Rolled back, so no attachment row records these uploads.
        for upload in stored:
            discard_unrecorded_dc_document(upload)
        return None, [str(exc)]
    except Exception:
        # Rolled back, so no attachment row records these uploads. Re-raised: a failure,
        # not a refusal, and the error page and logs must still see it.
        for upload in stored:
            discard_unrecorded_dc_document(upload)
        raise
    return revision, []


def _write_edit(plan, project, challan, profile, stored):
    """Every write of one edit. Call inside the transaction; raises EditRefused when the
    re-check under the lock fails, rolling everything back."""
    # RACE (E7): an engineer confirming a GRN, or a second editor, at the same moment.
    # Both GRN views and this one take the same row lock before writing, so they run one
    # after the other; what the form relied on is then asked again of the challan as it
    # is now. NOTE the GRN views read their lines BEFORE taking the lock, so a GRN that
    # waited on this edit can still write stale copies afterwards (SECONDARY_FINDINGS).
    locked = lock_challan(challan)
    lines = lines_for_edit(locked)
    if _changed_elsewhere(plan, locked, lines):
        raise EditRefused(CHANGED_ELSEWHERE)
    if plan.documents:
        count_errors = _document_errors(plan.documents, live_document_count(locked))
        if count_errors:
            raise EditRefused(count_errors[0])
    if not _has_changes(plan, locked, lines):
        raise EditRefused(NOTHING_CHANGED)

    before = challan_snapshot(locked, lines)
    revision_no = plan.opened_revision + 1

    for name, value in plan.header.items():
        setattr(locked, name, value)
    locked.save(update_fields=list(plan.header))

    by_pk = {line.pk: line for line in lines}
    for pk in plan.removed:
        # Only lines without a receipt reach here, and those have no damage photo, so
        # the PROTECT on DCLineDamagePhoto.line is never met; if it were, the delete
        # raises and the whole edit rolls back.
        by_pk[pk].delete()
    for pk, values in plan.lines.items():
        line = by_pk[pk]
        if _line_changed(line, values):
            for name in LINE_EDIT_FIELDS:
                setattr(line, name, values[name])
            line.save(update_fields=list(LINE_EDIT_FIELDS))
    for values in plan.new_lines:
        DCLineItem.objects.create(challan=locked, **values)

    # DC-B2: one row per stored document, in this block so the edit never commits
    # without the rows for objects already in storage. no_prices_confirmed is True only
    # because read_edit() refused every edit with files and no tick.
    added = [DeliveryChallanAttachment.objects.create(
        challan=locked, uploaded_by=profile, no_prices_confirmed=True, **upload).pk
        for upload in stored]

    # E5: the status is recalculated, never edited — the GRN views' call, unchanged. An
    # edit cannot move it (only received lines decide it, and theirs are untouched), so
    # the ledger row it writes on a change appears only when an old challan's stored
    # status was already wrong; REASON_DC_EDITED names that correction. It also re-derives
    # the OPEX delivery buckets, which a category change, removal or new line can move.
    recalculate_dc_status(locked, actor=profile, reason_code=REASON_DC_EDITED)

    after = challan_snapshot(locked, lines_for_edit(locked))
    if added:
        after['documents_added'] = added
    revision = DeliveryChallanRevision.objects.create(
        challan=locked, revision_no=revision_no, revised_by=profile,
        reason=plan.reason, before=before, after=after)

    # The reason is not in the feed line (ruling Q-F): it is in the challan's history,
    # and the feed is read by everyone who can see the site.
    old_number = before['header']['dc_number']
    action = (f'Edited DC {old_number}' if old_number == locked.dc_number
              else f'Edited DC {old_number} — now {locked.dc_number}')
    log_activity(project, profile, action, entity_type='DeliveryChallan',
                 entity_id=locked.pk, action_code='dc_edited')
    return revision
