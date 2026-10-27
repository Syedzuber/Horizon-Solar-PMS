# AF-1 (Q4): stamp the money code on the milestone-amount feed lines written before
# set_milestone_amounts() had one.
#
# Data only, no schema change. Before AF-1 that line was logged with a blank action_code,
# so a code-based exclusion could not hide its amounts from viewers outside the project's
# money audience. The view now writes 'milestone_amounts_set'; this gives the old rows the
# same code.
#
# THE MATCH IS NARROW ON PURPOSE: blank code AND the exact prefix the view has always
# written. Nothing else in the codebase writes that prefix, and a row that already has a
# code is never touched. ActivityLog is append-only in spirit; this changes only the
# machine-readable key, never the text a person reads.
#
# Reversible: the reverse blanks the code again on rows carrying the prefix. That also
# blanks rows written by the new view after the forward ran, which is the state the code
# would be in if the view change were reverted with it.

from django.db import migrations

CODE = 'milestone_amounts_set'
PREFIX = 'Set milestone amounts:'


def stamp(apps, schema_editor):
    ActivityLog = apps.get_model('projects', 'ActivityLog')
    ActivityLog.objects.filter(action_code='', action__startswith=PREFIX).update(
        action_code=CODE)


def unstamp(apps, schema_editor):
    ActivityLog = apps.get_model('projects', 'ActivityLog')
    ActivityLog.objects.filter(action_code=CODE, action__startswith=PREFIX).update(
        action_code='')


class Migration(migrations.Migration):

    dependencies = [
        ('projects', '0120_design_file_pdf'),
    ]

    operations = [
        migrations.RunPython(stamp, unstamp),
    ]
