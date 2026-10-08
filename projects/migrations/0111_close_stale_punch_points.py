# Closeout step 2 (CL-1) - data half. No schema.
#
# Before CL-1 an approval left a task's punch points Open. Those points would have
# closed under the rule, so this closes them now, once, and says so on the row:
#
#   which    status='Open' AND task.approved_at IS NOT NULL
#            AND created_at <= task.approved_at
#            (a point raised AFTER the approval was not closed by it, so it stays Open)
#   closer   the task's approved_by, which may be NULL (an account since removed)
#   when     task.approved_at - the moment the rule would have closed it
#   method   'retroactive' ("Closed retroactively (CL-1)")
#
# A task that is Done without an approved_at is NOT touched (ruling Q2): only an
# approval closes a point.
#
# Literal values, not PunchPoint.CLOSED etc.: a migration runs against the
# historical model and must not depend on constants that may later be renamed.
#
# A no-op on a database with no such rows. Reverse is a no-op: once closed, a point
# cannot be told apart from one closed at runtime except by its method, and
# re-opening it would put back a state the rule says is wrong.
from django.db import migrations
from django.db.models import F


def forwards(apps, schema_editor):
    PunchPoint = apps.get_model('projects', 'PunchPoint')

    stale = (
        PunchPoint.objects
        .filter(status='Open', task__approved_at__isnull=False,
                created_at__lte=F('task__approved_at'))
        .select_related('task')
        .order_by('pk')
    )
    pending = []
    for point in stale:
        point.status = 'Closed'
        point.closed_by_id = point.task.approved_by_id
        point.closed_at = point.task.approved_at
        point.closure_method = 'retroactive'
        pending.append(point)
    if pending:
        PunchPoint.objects.bulk_update(
            pending, ['status', 'closed_by', 'closed_at', 'closure_method'])


class Migration(migrations.Migration):

    dependencies = [
        ('projects', '0110_punch_point_closure'),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
