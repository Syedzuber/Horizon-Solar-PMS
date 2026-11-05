"""
NTF-DCG 5c — the goods-receipt email (event grn_recorded, rulings E1-E9, N1, N5-N7).

Every new GoodsReceipt of kind 'receipt' (an SE's confirm_grn) or 'correction' (SCM's
override_grn) tells SCM, the site's PM and Admin's named recipients (Finance, ruling R5)
what was recorded, so a vendor payment can be reconciled against it: the GRN number,
challan, vendor, PO reference, each line's before → after, damage, documents, and a link
to that receipt on the challan page.

ONE FIRE POINT (E3). goods_receipts.write_goods_receipt() calls fire_after_commit() for
the receipt it just wrote. Both GRN views write their receipt through it, inside their
locked transaction; the backfill command creates its 'imported' receipts directly and
never passes through it, and fire_after_commit() refuses that kind anyway.

WHY THE VALUES ARE READ AFTER COMMIT, NOT AT THE CALL. write_goods_receipt() runs before
the view records the damage photos and the Issue, links the documents and recalculates
the challan's status — all things this email reports. notify() freezes its values when
it is called, so it cannot be used here. Instead the callback registered on commit
re-reads the receipt, by then complete, and calls notify_now(), the documented way for
code that already runs after commit (see notify.notify_now). A refused or rolled-back
GRN drops the callback with its transaction, so it sends nothing.

NO MONEY. Nothing here reads a rate, price or amount; no DC line, receipt line, vendor
or challan field read below holds one (tests_grn_notify pins the event's variables).

This module imports models, notify, permissions and delivery_notify (none of which
import it); goods_receipts imports it. No cycle.
"""

import logging
from collections import Counter

from django.db import transaction

from .delivery_notify import challan_link, quantity_text, site_code, site_display_name
from .models import (
    GOODS_RECEIPT_CORRECTION, GOODS_RECEIPT_RECEIPT, RECEIPT_DOCUMENT_TYPE_CHOICES,
    DCLineDamagePhoto, DeliveryChallanAttachment, GoodsReceipt, Issue,
)
from .notify import notify_now
from .notify_registry import EmailTable
from .permissions import active_scm_users, site_assigned_pm

logger = logging.getLogger(__name__)

EVENT = 'grn_recorded'

#: The kinds that send (E3). 'imported' never does: those receipts describe the state
#: before numbering began and were never a delivery anyone needs telling about.
SENDING_KINDS = (GOODS_RECEIPT_RECEIPT, GOODS_RECEIPT_CORRECTION)

NO_VENDOR = 'Not recorded'
NO_PO = 'not linked'
NO_DOCUMENTS = 'No documents attached'
CORRECTION_PREFIX = 'Correction: '


def _date(value):
    return value.strftime('%d %b %Y') if value else 'Not recorded'


def _name(profile):
    if profile is None:
        return 'Not recorded'
    return profile.user.get_full_name() or profile.user.username


def receipt_anchor(receipt):
    """The id of the receipt's entry in the challan page's Receipts card, e.g.
    'grn-000123'. The template writes the same `receipt.number|lower`; the number never
    changes once set, so the anchor is stable."""
    return (receipt.number or '').lower()


def receipt_link(receipt):
    """The challan page, anchored to this receipt (E5). The page's script opens that
    receipt's entry (5c-Q4). Relative: notify() makes it absolute."""
    challan = receipt.challan
    return f'{challan_link(challan.project, challan)}#{receipt_anchor(receipt)}'


def grn_recipients(project):
    """The code-resolved people (E2): every active SCM user, and the site's assigned PM
    alone (site_assigned_pm, which leaves coordinators out and returns None for an
    inactive PM). notify_now() drops the actor and merges the named recipients."""
    people = list(active_scm_users().select_related('user'))
    pm = site_assigned_pm(project)
    if pm is not None:
        people.append(pm)
    return people


def receipt_kind_text(receipt):
    """'Receipt', or 'Correction (by SCM)' with the on-behalf reason when SCM recorded a
    line nobody had confirmed (4a, G5: carried only then)."""
    text = receipt.get_kind_display()
    if receipt.on_behalf_reason:
        text += f', recorded on behalf of the site team: {receipt.on_behalf_reason}'
    return text


def material_table(lines):
    """The receipt's lines as the three-column table (N7, 5c-Q8): Item (unit and DC qty
    beneath); Received before → after (change); Damage / notes (damaged before → after,
    condition, notes). The text rows are the plain-text form approved at pre-flight.

    The damaged change is shown only on a line with damage before or after this receipt
    (GoodsReceiptLine.has_damage); "Damaged 0 → 0 (±0)" on every clean line was noise
    (5c review). Condition and notes still show."""
    rows, text_rows = [], []
    for line in lines:
        dc_qty = quantity_text(line.ordered_quantity)
        notes = [f'Damaged {line.damaged_change}'] if line.has_damage else []
        text = (f'{line.item_description} — Unit: {line.unit} · DC qty: {dc_qty} · '
                f'Received: {line.received_change}')
        if line.has_damage:
            text += f' · Damaged: {line.damaged_change}'
        if line.condition:
            notes.append(line.condition)
            text += f' · Condition: {line.condition}'
        if line.grn_notes:
            notes.append(line.grn_notes)
            text += f' · Notes: {line.grn_notes}'
        rows.append((f'{line.item_description}\n{line.unit} · DC qty {dc_qty}',
                     line.received_change, '\n'.join(notes)))
        text_rows.append(text)
    return EmailTable(caption='Material', columns=('Item', 'Received', 'Damage / notes'),
                      rows=tuple(rows), text_rows=tuple(text_rows))


def damage_section(receipt, lines):
    """The damaged lines (damaged after this receipt > 0), the Issue(s) their photos are
    linked to — number and title only, no link (5c-Q5) — and the count of damage photos
    linked to THIS receipt. '' when no line is damaged, which drops the paragraph."""
    damaged = [line for line in lines if line.damaged_after]
    if not damaged:
        return ''
    # The Issue a damaged line's photos hang on: normally the challan's one damage Issue
    # (grn_damage, R4), which may predate this receipt when the damage was recorded
    # earlier. Read through the photos rather than "the open damage Issue" so a resolved
    # Issue that still holds those photos is named too.
    issues = list(Issue.objects.filter(
        damage_photos__line__in=[line.dc_line_id for line in damaged]).distinct()
        .order_by('pk').values_list('pk', 'title'))
    photos = DCLineDamagePhoto.objects.filter(receipt=receipt).count()
    footer = [f'Issue #{pk}: {title}' for pk, title in issues] or ['Issue: none linked']
    footer.append(f'Damage photos linked to this receipt: {photos}')
    rows, text_rows = [], []
    for line in damaged:
        notes = [part for part in (line.condition, line.grn_notes) if part]
        rows.append((f'{line.item_description}\n{line.unit} · DC qty '
                     f'{quantity_text(line.ordered_quantity)}',
                     line.damaged_change, '\n'.join(notes)))
        text = (f'{line.item_description} — Unit: {line.unit} · Damaged: '
                f'{line.damaged_change}')
        if line.condition:
            text += f' · Condition: {line.condition}'
        if line.grn_notes:
            text += f' · Notes: {line.grn_notes}'
        text_rows.append(text)
    return EmailTable(caption='Damage', columns=('Item', 'Damaged', 'Condition / notes'),
                      rows=tuple(rows), text_rows=tuple(text_rows), footer=tuple(footer))


def documents_summary(receipt):
    """'Signed DC copy: 1 · Delivery photo: 2', in the form's type order, or
    'No documents attached'. Withdrawn documents are not counted."""
    counts = Counter(DeliveryChallanAttachment.objects.filter(
        receipt=receipt, withdrawn_at__isnull=True).values_list('document_type', flat=True))
    parts = [f'{label}: {counts[value]}' for value, label in RECEIPT_DOCUMENT_TYPE_CHOICES
             if counts[value]]
    return ' · '.join(parts) or NO_DOCUMENTS


def grn_values(receipt):
    """{variable: value} for `receipt`, read now (after commit, see the module
    docstring). status_after is the challan's status as stored: the GRN view
    recalculated it in the receipt's transaction, so it is the status this receipt left.
    """
    challan = receipt.challan
    project = challan.project
    lines = list(receipt.lines.order_by('pk'))
    return {
        'subject_prefix': CORRECTION_PREFIX if receipt.kind == GOODS_RECEIPT_CORRECTION else '',
        'grn_number': receipt.number,
        'receipt_kind': receipt_kind_text(receipt),
        'recorded_by_name': _name(receipt.recorded_by),
        'receipt_date': _date(receipt.receipt_date),
        'site_code': site_code(project),
        'site_name': site_display_name(project),
        'dc_number': challan.dc_number,
        'dc_date': _date(challan.dc_date),
        'vendor_name': challan.vendor.name if challan.vendor_id else NO_VENDOR,
        'po_reference': (challan.po_number or '').strip() or NO_PO,
        'status_after': challan.status,
        'material_table': material_table(lines),
        'damage_section': damage_section(receipt, lines),
        'documents_summary': documents_summary(receipt),
    }


def send_grn_recorded(receipt_pk):
    """The after-commit body: read the receipt and send grn_recorded. Never raises — the
    GRN is already saved and must not look failed because a message could not go."""
    try:
        receipt = (GoodsReceipt.objects
                   .select_related('challan__project__assigned_pm__user', 'challan__vendor',
                                   'recorded_by__user')
                   .get(pk=receipt_pk))
        if receipt.kind not in SENDING_KINDS:
            return
        project = receipt.challan.project
        notify_now(EVENT, recipients=grn_recipients(project), values=grn_values(receipt),
                   actor=receipt.recorded_by, related_project=project,
                   link=receipt_link(receipt))
    except Exception:
        logger.exception('grn_notify: could not send %s for receipt %s', EVENT, receipt_pk)


def fire_after_commit(receipt):
    """Send grn_recorded for `receipt` once its transaction commits (E3). Called by
    write_goods_receipt() only. An imported receipt is refused here as well as never
    passing through that writer."""
    if receipt.kind not in SENDING_KINDS:
        return
    receipt_pk = receipt.pk
    # Robust: a failure in the callback is logged and never reaches the committed GRN. A
    # lambda, not functools.partial: robust on_commit names the callback in its log
    # line, which needs a __qualname__.
    transaction.on_commit(lambda: send_grn_recorded(receipt_pk), robust=True)
