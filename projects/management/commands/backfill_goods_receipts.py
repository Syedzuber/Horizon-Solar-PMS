"""Import receipt data recorded before goods receipts existed (NTF-DCG 4a, G7).

    python manage.py backfill_goods_receipts            # dry run: counts, writes nothing
    python manage.py backfill_goods_receipts --apply    # writes

WHAT IT WRITES. For each challan whose lines carry receipt data from before 4a and that
has no Imported receipt yet: one kind='imported' GoodsReceipt per group of lines sharing
(grn_date, grn_confirmed_by) — ruling P7, so two GRNs that really were separate are not
merged into one record. Each receipt line holds the line's receipt values with no
"before" (what came before is not known); receipt_date is the group's grn_date,
recorded_by its confirmer (NULL if none was stored), and the on-behalf reason is carried
when every line in the group was recorded on behalf with the same reason.

APPROXIMATE BY NATURE. Before 4a every GRN submission saved every pre-filled line again,
re-stamping its date and confirmer, so a challan received over several deliveries
usually shows one date. The Imported label on the DC page says so.

GAP CHALLANS. A live GRN between the deploy and this run journals the lines it changed,
and its receipt lines hold their BEFORE values — the state this command would have
imported. Such a line is imported from those before values (ruling P7), in its own group
with no confirmer, dated the earliest live receipt that changed it. Gap challans are
listed. A line whose first live receipt shows nothing before it was never received
before 4a and is not imported.

SKIPPED AND LISTED: challans that already have an Imported receipt (that is what makes a
second run write nothing), challans fully covered by live receipts, and lines with
receipt data but no received quantity or no GRN date (a receipt line needs both).

DRY RUN is read-only: one transaction, declared READ ONLY on Postgres, rolled back.
--apply writes each challan in its own transaction with the challan row locked
(grn_damage.lock_challan), so a GRN running at the same moment waits, and the plan is
made again under the lock. It prints the database host and name first, never the
password. No notification is sent; nothing else is changed.
"""
from collections import defaultdict

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connection, transaction
from django.db.models import Q

from projects.grn_damage import lock_challan
from projects.models import (
    GOODS_RECEIPT_IMPORTED, DCLineItem, DeliveryChallan, GoodsReceipt, GoodsReceiptLine,
)

#: Any receipt column set — the same columns as dc_edit.has_receipt(), less the photo
#: term (a photo is only ever stored with a received quantity).
HAS_RECEIPT_DATA = (Q(received_quantity__isnull=False) | Q(grn_date__isnull=False)
                    | Q(grn_confirmed_by__isnull=False) | Q(grn_on_behalf=True)
                    | Q(damaged_quantity__gt=0) | (Q(condition__isnull=False) & ~Q(condition='')))


def _has_receipt_data(line):
    return (line.received_quantity is not None or line.grn_date is not None
            or line.grn_confirmed_by_id is not None or line.grn_on_behalf
            or line.damaged_quantity > 0 or bool(line.condition))


def _condition(received, damaged):
    """_read_grn_lines' derivation, repeated: a gap line's before-values carry no
    condition of their own. Repeated, not imported — it is a local inside that view."""
    if not damaged:
        return DCLineItem.GOOD
    return DCLineItem.DAMAGED if damaged >= received else DCLineItem.PARTIAL


def plan_challan(challan):
    """(verdict, groups, gap, skipped_lines) for one challan. Reads only.

    verdict: 'already' | 'covered' | 'import' | 'nothing'. groups: {key: {date,
    recorded_by_id, reason, lines: [(line, received, damaged, condition, notes)]}}.
    """
    if challan.goods_receipts.filter(kind=GOODS_RECEIPT_IMPORTED).exists():
        return 'already', {}, False, []
    lines = list(challan.line_items.order_by('pk'))
    # The earliest live receipt line per DC line: its before-values are the state before
    # the journal began. Receipt pk order is commit order under the challan lock.
    first_live = {}
    for receipt_line in (GoodsReceiptLine.objects.filter(dc_line__challan=challan)
                         .select_related('receipt').order_by('receipt_id', 'pk')):
        first_live.setdefault(receipt_line.dc_line_id, receipt_line)

    groups, skipped, gap, covered = {}, [], False, False
    for line in lines:
        first = first_live.get(line.pk)
        if first is not None:
            covered = True
            if first.received_before is None:
                continue        # nothing was recorded on it before the journal began
            gap = True
            key = ('gap',)
            group = groups.setdefault(key, {'date': first.receipt.receipt_date,
                                            'recorded_by_id': None, 'reasons': set(),
                                            'all_on_behalf': False, 'lines': []})
            group['date'] = min(group['date'], first.receipt.receipt_date)
            damaged = first.damaged_before or 0
            group['lines'].append((line, first.received_before, damaged,
                                   _condition(first.received_before, damaged), ''))
            continue
        if not _has_receipt_data(line):
            continue
        if line.received_quantity is None or line.grn_date is None:
            skipped.append(line)
            continue
        key = (line.grn_date, line.grn_confirmed_by_id)
        group = groups.setdefault(key, {'date': line.grn_date,
                                        'recorded_by_id': line.grn_confirmed_by_id,
                                        'reasons': set(), 'all_on_behalf': True,
                                        'lines': []})
        group['all_on_behalf'] = group['all_on_behalf'] and line.grn_on_behalf
        group['reasons'].add(line.grn_on_behalf_reason)
        group['lines'].append((line, line.received_quantity, line.damaged_quantity,
                               line.condition or '', line.grn_notes or ''))
    if groups:
        return 'import', groups, gap, skipped
    return ('covered' if covered else 'nothing'), {}, gap, skipped


def _shared_reason(group):
    """The on-behalf reason when every line in the group carries the same one (P7)."""
    if group['all_on_behalf'] and len(group['reasons']) == 1:
        return next(iter(group['reasons']))
    return ''


def write_challan(challan, groups):
    """Write the planned Imported receipts. Inside the caller's transaction, challan
    locked. Returns (receipts, lines) written."""
    receipts = lines = 0
    for key in sorted(groups, key=lambda k: (groups[k]['date'], str(k))):
        group = groups[key]
        receipt = GoodsReceipt.objects.create(
            challan=challan, kind=GOODS_RECEIPT_IMPORTED,
            recorded_by_id=group['recorded_by_id'], receipt_date=group['date'],
            on_behalf_reason=_shared_reason(group),
        )
        GoodsReceiptLine.objects.bulk_create([
            GoodsReceiptLine(
                receipt=receipt, dc_line=line, received_before=None,
                received_after=received, damaged_before=None, damaged_after=damaged,
                condition=condition, grn_notes=notes,
                item_description=line.item_description, unit=line.unit,
                ordered_quantity=line.ordered_quantity,
            )
            for line, received, damaged, condition, notes in group['lines']
        ])
        receipts += 1
        lines += len(group['lines'])
    return receipts, lines


class Command(BaseCommand):
    help = ('Import receipt data recorded before goods receipts existed, as Imported '
            'receipts. Dry run by default; --apply writes. Idempotent.')

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Write the receipts. Without it nothing is written.')

    def handle(self, *args, **options):
        db = settings.DATABASES['default']
        self.stdout.write(f"DB host: {db.get('HOST') or '(local)'}  name: {db.get('NAME')}")
        apply = options['apply']
        self.stdout.write('Mode: APPLY' if apply else 'Mode: DRY RUN — nothing is written')

        # The challans with any receipt data on any line. One query.
        challan_ids = list(DCLineItem.objects.filter(HAS_RECEIPT_DATA)
                           .values_list('challan_id', flat=True).distinct().order_by('challan_id'))
        if apply:
            outcome = self._run(challan_ids, apply=True)
        else:
            with transaction.atomic():
                if connection.vendor == 'postgresql':
                    with connection.cursor() as cursor:
                        cursor.execute('SET TRANSACTION READ ONLY')
                outcome = self._run(challan_ids, apply=False)
                transaction.set_rollback(True)
        self._report(len(challan_ids), outcome, apply)

    def _run(self, challan_ids, apply):
        out = defaultdict(list)
        totals = {'receipts': 0, 'lines': 0}
        for pk in challan_ids:
            challan = DeliveryChallan.objects.select_related('project').get(pk=pk)
            if apply:
                with transaction.atomic():
                    # RACE: a GRN on this challan at the same moment would journal lines
                    # this plan reads; the lock makes it wait, and the plan is made
                    # again on what the lock shows.
                    challan = lock_challan(challan)
                    verdict, groups, gap, skipped = plan_challan(challan)
                    if verdict == 'import':
                        receipts, lines = write_challan(challan, groups)
                    else:
                        receipts = lines = 0
            else:
                verdict, groups, gap, skipped = plan_challan(challan)
                receipts = len(groups)
                lines = sum(len(g['lines']) for g in groups.values())
            label = f'DC pk {challan.pk} "{challan.dc_number}" ({challan.project.project_id})'
            out[verdict].append(label)
            if gap:
                out['gap'].append(label)
            for line in skipped:
                out['skipped_lines'].append(f'{label} line {line.pk}')
            if verdict == 'import':
                out['import_detail'].append(f'{label}: {receipts} receipt(s), {lines} line(s)')
            totals['receipts'] += receipts
            totals['lines'] += lines
        out['totals'] = totals
        return out

    def _report(self, considered, out, apply):
        w = self.stdout.write
        w(f'Challans with receipt data: {considered}')
        for key, title in (('already', 'Skipped — already has an Imported receipt'),
                           ('covered', 'Skipped — fully covered by live receipts'),
                           ('nothing', 'Skipped — no line importable'),
                           ('import', 'To import' if not apply else 'Imported')):
            w(f'  {title}: {len(out[key])}')
            rows = out['import_detail'] if key == 'import' else out[key]
            for row in rows:
                w(f'    {row}')
        w(f"Gap challans (before-value fallback used): {len(out['gap'])}")
        for row in out['gap']:
            w(f'    {row}')
        w(f"Lines skipped (receipt data but no received quantity or GRN date): "
          f"{len(out['skipped_lines'])}")
        for row in out['skipped_lines']:
            w(f'    {row}')
        totals = out['totals']
        verb = 'Written' if apply else 'Would write'
        w(f"{verb}: {totals['receipts']} receipt(s), {totals['lines']} line(s)")
