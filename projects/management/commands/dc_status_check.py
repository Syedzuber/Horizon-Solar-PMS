"""Compare every delivery challan's stored status with what its lines give. Read-only.

    python manage.py dc_status_check

WHY IT EXISTS. NTF-DCG 4c (ruling (a), 4 Nov 2026) changed the status rule: a challan is
Received only when every line has a recorded received quantity (an explicit 0 counts).
The rule runs only when a GRN or a DC edit recalculates the challan, so a row saved under
an older rule keeps its old status until then. This command finds those rows.

WHAT IT PRINTS. One line per challan whose stored status differs from
models.derive_dc_status() — the same function recalculate_dc_status() uses, so this asks
exactly the GRN views' question, not a restatement of it. Each difference carries a
reason:

  UNRECORDED   N of M lines unrecorded — the recorded lines alone give the stored
               status; only the blank lines make the difference (the 4c rule).
  RULE DRIFT   the recorded lines alone already give a different status: a row from
               before the severity rollup, or written outside recalculate_dc_status().

IT CORRECTS NOTHING, and has no apply mode (ruling S6). A stored status moves the next
time the challan is recalculated: an SCM GRN override that changes a line, or a DC edit.
The SE's GRN form is hidden on a Received challan, so the SE cannot do it.

READ-ONLY BY CONSTRUCTION, like check_dc_edit_reverts: SELECTs only, nothing logged. On
Postgres the transaction is also declared READ ONLY, so a write would fail rather than
land; it is rolled back either way. It prints the database host and name first, never
the password.
"""
from django.core.management.base import BaseCommand
from django.db import connection, transaction

from projects.models import DeliveryChallan, derive_dc_status

REASON_UNRECORDED = 'UNRECORDED'
REASON_RULE_DRIFT = 'RULE DRIFT'


def challan_difference(challan, lines):
    """(derived status, reason) for `challan` whose lines are `lines`, or None when the
    stored status already matches. Pure; the command and its tests both call it."""
    derived = derive_dc_status(lines)
    if derived == challan.status:
        return None
    # The recorded lines on their own are what the rule saw before 4c (blank lines were
    # left out of the rollup). If they give the stored status, the blanks are the whole
    # difference; if not, the row disagrees with the rule even on its recorded lines.
    recorded = [line for line in lines if line.received_quantity is not None]
    if recorded and derive_dc_status(recorded) == challan.status:
        return derived, REASON_UNRECORDED
    return derived, REASON_RULE_DRIFT


class Command(BaseCommand):
    help = ('Report-only: every delivery challan whose stored status differs from what its '
            'lines give under the NTF-DCG 4c rule. Writes nothing.')

    def handle(self, *args, **options):
        db = connection.settings_dict
        self.stdout.write(f"DB host: {db.get('HOST') or '(local socket)'}  name: {db.get('NAME')}")
        with transaction.atomic():
            if connection.vendor == 'postgresql':
                with connection.cursor() as cursor:
                    cursor.execute('SET TRANSACTION READ ONLY')
            self._report()
            # Nothing was written; rolled back anyway so the command can never commit.
            transaction.set_rollback(True)

    def _report(self):
        # Every challan, deleted sites included: a stored status is wrong or right
        # whatever the site's state, and this is a data check, not a screen. Two queries:
        # the challans with their project, then every line (prefetch).
        challans = (DeliveryChallan.objects
                    .select_related('project')
                    .prefetch_related('line_items')
                    .order_by('project__project_id', 'dc_number', 'pk'))
        total = differences = 0
        for challan in challans:
            total += 1
            lines = list(challan.line_items.all())
            found = challan_difference(challan, lines)
            if found is None:
                continue
            differences += 1
            derived, reason = found
            recorded = sum(1 for line in lines if line.received_quantity is not None)
            deleted = ' [site deleted]' if challan.project.is_deleted else ''
            self.stdout.write(
                f'{challan.project.project_id}{deleted}  DC {challan.dc_number} (id {challan.pk}): '
                f'stored {challan.status} -> rule {derived}  '
                f'lines recorded {recorded} / {len(lines)}  {reason}')
        self.stdout.write(f'Challans checked: {total}. Differences: {differences}.')
