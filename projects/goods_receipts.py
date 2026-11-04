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

This module imports models and grn_damage; it is imported by views.py and by the
backfill command and imports neither, so there is no cycle.
"""
from django.db.models import Prefetch

from .grn_damage import photo_field
from .models import GoodsReceipt, GoodsReceiptLine

#: G4: a submission in which no line is recorded or changed.
NOTHING_TO_RECORD = 'Nothing to record — enter a received quantity on at least one line.'


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


def receipts_for_challan(challan):
    """The challan's receipts, newest first, each with `lines` prefetched in order and
    the recorder's login joined — for the DC page's Receipts section (G6). Two queries
    whatever the number of receipts, one when there are none."""
    return list(
        GoodsReceipt.objects.filter(challan=challan)
        .select_related('recorded_by__user')
        .prefetch_related(Prefetch('lines', queryset=GoodsReceiptLine.objects.order_by('pk')))
        .order_by('-pk')
    )
