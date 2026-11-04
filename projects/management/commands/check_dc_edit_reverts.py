"""Find DC-B3 challan edits a GRN may have undone (DG-L damage check). Read-only.

    python manage.py check_dc_edit_reverts

THE BUG IT LOOKS FOR. Until DG-L, confirm_grn and override_grn read the challan and its
lines before taking the row lock and saved those copies whole after it. An edit that
committed in between was undone: an edited line or header went back to what the GRN's
page had read, and a line the edit removed was INSERTed again under its old id.

HOW. Every DeliveryChallanRevision holds `after`, the challan as the edit left it, taken
under the lock (dc_edit.challan_snapshot, schema 1). For each challan the command walks
its revisions in order and compares each `after` with the next revision's `before` (also
taken under the lock), and the last `after` with the challan as it is now. Nothing else
writes the compared columns, so any difference means a writer that was not an edit.
Compared: the header (vendor id, DC number, DC date, expected delivery, warehouse id, PO
reference, notes) and, by line id, category, description, ordered quantity and unit.
has_receipt is not compared: a GRN changes it legitimately.

  SUSPECT      a GRN was logged on the challan inside the interval — the race's shape.
  UNEXPLAINED  a difference with no GRN in the interval (Django admin, a shell, a script).
  RECREATED    a line id the edit removed is back.

GRN times come from the activity line both GRN views write in their transaction ("SE
confirmed GRN" / "SCM overrode GRN"), and from the status ledger's grn_confirmed /
grn_overridden rows, which exist only when the status moved.

IT CORRECTS NOTHING. A reverted edit is put right on screen by SCM, with a revision.
Limit: a revert to the value the edit had anyway, or a revert later re-edited to the same
value, leaves no difference to find.

READ-ONLY BY CONSTRUCTION, like list_implausible_dates: SELECTs only, no get_or_create,
no SystemSettings read, nothing logged. On Postgres the transaction is also declared
READ ONLY, so a write would fail rather than land; it is rolled back either way. It
prints the database host and name first, never the password.
"""
from collections import defaultdict

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connection, transaction
from django.db.models import Max, Q
from django.utils import timezone

from projects.dc_edit import challan_snapshot, lines_for_edit
from projects.models import (
    REASON_GRN_CONFIRMED, REASON_GRN_OVERRIDDEN, SUBJECT_DELIVERY_CHALLAN, ActivityLog,
    DeliveryChallan, DeliveryChallanRevision, StatusTransition,
)

HEADER_FIELDS = ('vendor', 'dc_number', 'dc_date', 'expected_delivery_date',
                 'issued_from_warehouse', 'po_number', 'notes')
LINE_FIELDS = ('boq_category', 'item_description', 'ordered_quantity', 'unit')

# The two GRN views' activity lines, as they begin (views.confirm_grn / override_grn).
GRN_ACTIONS = Q(action__startswith='SE confirmed GRN for DC ') | Q(
    action__startswith='SCM overrode GRN for DC ')


def _header_value(header, name):
    """A snapshot header value, compared by id where the snapshot carries a name too."""
    value = header.get(name)
    if name in ('vendor', 'issued_from_warehouse') and value is not None:
        return value['id']
    return value


def _differences(earlier, later, seen_line_ids):
    """[(field, earlier value, later value, kind)] between two schema-1 snapshots."""
    found = []
    for name in HEADER_FIELDS:
        a, b = _header_value(earlier['header'], name), _header_value(later['header'], name)
        if a != b:
            found.append((name, a, b, 'header'))
    lines_a = {line['id']: line for line in earlier['lines']}
    lines_b = {line['id']: line for line in later['lines']}
    for pk, line in lines_a.items():
        if pk not in lines_b:
            found.append((f'line {pk}', 'present', 'missing', 'missing'))
            continue
        for name in LINE_FIELDS:
            if line[name] != lines_b[pk][name]:
                found.append((f'line {pk} {name}', line[name], lines_b[pk][name], 'line'))
    for pk in lines_b.keys() - lines_a.keys():
        # No path but an edit adds a line to an existing challan, so a line that appears
        # between two snapshots is either one the edit removed, back, or unexplained.
        kind = 'recreated' if pk in seen_line_ids else 'added'
        found.append((f'line {pk}', 'absent', 'present', kind))
    return found


class Command(BaseCommand):
    help = 'List DC-B3 challan edits a later GRN may have reverted (read-only).'

    def handle(self, *args, **options):
        db = settings.DATABASES['default']
        self.stdout.write(f"DB host: {db.get('HOST') or '(local socket)'}  name: {db.get('NAME')}")
        with transaction.atomic():
            if connection.vendor == 'postgresql':
                with connection.cursor() as cursor:
                    cursor.execute('SET TRANSACTION READ ONLY')
            self._report()
            transaction.set_rollback(True)

    def _report(self):
        newest = [ActivityLog.objects.aggregate(m=Max('timestamp'))['m'],
                  DeliveryChallan.objects.aggregate(m=Max('created_at'))['m']]
        newest = max((t for t in newest if t is not None), default=None)
        self.stdout.write('Newest ActivityLog / DeliveryChallan row: '
                          + (timezone.localtime(newest).isoformat() if newest else 'none'))

        revisions = defaultdict(list)
        for rev in DeliveryChallanRevision.objects.order_by('challan_id', 'revision_no'):
            revisions[rev.challan_id].append(rev)
        challan_ids = list(revisions)

        # Every GRN moment per challan, from both sources; sorted below.
        grn_times = defaultdict(list)
        for entity_id, ts in (ActivityLog.objects
                              .filter(GRN_ACTIONS, entity_type='DeliveryChallan',
                                      entity_id__in=challan_ids)
                              .values_list('entity_id', 'timestamp')):
            grn_times[entity_id].append(ts)
        for subject_id, ts in (StatusTransition.objects
                               .filter(subject_type=SUBJECT_DELIVERY_CHALLAN,
                                       subject_id__in=challan_ids,
                                       reason_code__in=(REASON_GRN_CONFIRMED,
                                                        REASON_GRN_OVERRIDDEN))
                               .values_list('subject_id', 'occurred_at')):
            grn_times[subject_id].append(ts)

        # _base_manager: a challan on a soft-deleted project is still checked.
        challans = (DeliveryChallan._base_manager
                    .filter(pk__in=challan_ids)
                    .select_related('project', 'vendor', 'issued_from_warehouse'))
        now = timezone.now()
        rows, suspect, unexplained = [], set(), set()
        for challan in challans.order_by('project__project_id', 'pk'):
            revs = revisions[challan.pk]
            current = challan_snapshot(challan, lines_for_edit(challan))
            seen_line_ids = set()
            for i, rev in enumerate(revs):
                for snap in (rev.before, rev.after):
                    seen_line_ids.update(line['id'] for line in snap['lines'])
                if i + 1 < len(revs):
                    later, until, against = revs[i + 1].before, revs[i + 1].revised_at, f'rev {i + 2} before'
                else:
                    later, until, against = current, now, 'current'
                diffs = _differences(rev.after, later, seen_line_ids)
                if not diffs:
                    continue
                grns = sorted(t for t in grn_times[challan.pk] if rev.revised_at < t <= until)
                last_grn = timezone.localtime(grns[-1]).strftime('%Y-%m-%d %H:%M:%S') if grns else '-'
                verdict = 'SUSPECT' if grns else 'UNEXPLAINED'
                (suspect if grns else unexplained).add(challan.pk)
                for field_name, rev_value, later_value, kind in diffs:
                    rows.append((challan.project.project_id, challan.pk, challan.dc_number,
                                 field_name, rev_value, later_value, against, rev.revision_no,
                                 timezone.localtime(rev.revised_at).strftime('%Y-%m-%d %H:%M:%S'),
                                 last_grn, 'RECREATED' if kind == 'recreated' else verdict))

        self.stdout.write(f"{'project_id':<22} {'dc':>6} {'dc_number':<18} {'field':<28} "
                          f"{'revision value':<24} {'value now':<24} {'compared with':<14} "
                          f"{'rev':>3} {'revised at':<19} {'last GRN after':<19} verdict")
        for (project_id, dc_pk, dc_number, field_name, rev_value, later_value, against,
             rev_no, revised_at, last_grn, verdict) in rows:
            self.stdout.write(f'{project_id:<22} {dc_pk:>6} {dc_number[:18]:<18} '
                              f'{field_name[:28]:<28} {str(rev_value)[:24]:<24} '
                              f'{str(later_value)[:24]:<24} {against:<14} {rev_no:>3} '
                              f'{revised_at:<19} {last_grn:<19} {verdict}')

        total_revs = sum(len(r) for r in revisions.values())
        self.stdout.write(
            f'{total_revs} revision(s) on {len(challan_ids)} challan(s) checked. '
            f'{len(rows)} difference row(s): '
            f'{sum(1 for r in rows if r[-1] == "SUSPECT")} suspect, '
            f'{sum(1 for r in rows if r[-1] == "RECREATED")} recreated, '
            f'{sum(1 for r in rows if r[-1] == "UNEXPLAINED")} unexplained. '
            f'Challans: {len(suspect)} with a GRN after the edit, '
            f'{len(unexplained - suspect)} with only unexplained differences.')
