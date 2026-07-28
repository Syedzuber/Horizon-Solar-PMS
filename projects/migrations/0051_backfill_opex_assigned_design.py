"""
Data-only migration (Part 4.5): bring Project.assigned_design into step with
DesignAssignment.assigned_to on OPEX sites where the two had diverged.

WHY THIS EXISTS
---------------
Two fields name the designer of an OPEX site:

    DesignAssignment.assigned_to   — who the Design Head allocated the site to
    Project.assigned_design        — what user_can_edit_project_boq() gates BOQ
                                     authorship on, and what the Design dashboard
                                     keys its project cards off

Until Part 4.5 nothing kept them in step: `_allocate_one()` set the first and never
touched the second. A site allocated to designer A while `assigned_design` still named
designer B left A unable to enter the BOQ (403) and unable to see the site on their own
dashboard at all. Measured on the local database before this migration: 3 of 5 allocated
sites had diverged.

`_allocate_one()` now stamps both, so no NEW divergence can appear. This migration
repairs the rows that diverged before that fix.

SCOPE — DELIBERATELY NARROW
---------------------------
  * OPEX sites only. Residential projects have no DesignAssignment row, so the queryset
    cannot reach one.
  * Only rows where a designer is actually allocated (`assigned_to` is not null) and the
    two fields disagree. A null `assigned_design` on an allocated site counts as a
    disagreement and is repaired — it is the harsher failure of the two (no BOQ access
    at all rather than the wrong person having it).
  * `assigned_to` WINS. It is the field the design workflow writes deliberately, through
    a permission-checked view, with an ActivityLog entry naming who did it.
    `assigned_design` on these sites is either stale seed data or was never set.

The comparison is done in PYTHON, not as an `.exclude(F(...))`. A SQL inequality against
a NULL column is itself NULL rather than true, so the null-`assigned_design` rows — the
ones that matter most — would have been silently skipped by the obvious queryset. The row
count here is tiny and correctness is worth more than one fewer query.

REVERSIBILITY
-------------
Deliberately irreversible in the data sense: the pre-migration values are not recorded,
because restoring a divergence that was always a bug is not a state anyone wants back.
`RunPython.noop` is supplied as the reverse so `migrate` can still walk backwards past
this migration without erroring.

SAFE WHERE THERE IS NOTHING TO DO. On a database with no OPEX design work — production at
the time of writing — this matches zero rows and is a no-op. It prints what it changed
either way, so the deploy log records the outcome rather than leaving it to be inferred.
"""
from django.db import migrations


def backfill_assigned_design(apps, schema_editor):
    DesignAssignment = apps.get_model('projects', 'DesignAssignment')

    changed = []
    for assignment in (DesignAssignment.objects
                       .filter(assigned_to__isnull=False, project__project_type='OPEX')
                       .select_related('project')):
        project = assignment.project
        if project.assigned_design_id == assignment.assigned_to_id:
            continue
        before = project.assigned_design_id
        project.assigned_design_id = assignment.assigned_to_id
        project.save(update_fields=['assigned_design'])
        changed.append((project.project_id, before, assignment.assigned_to_id))

    if changed:
        print(f'\n  0051: repaired assigned_design on {len(changed)} OPEX site(s):')
        for project_id, before, after in changed:
            print(f'    {project_id}: assigned_design {before} -> {after}')
    else:
        print('\n  0051: no diverged OPEX sites found — nothing to repair.')


class Migration(migrations.Migration):

    dependencies = [
        ('projects', '0050_alter_designassignment_status'),
    ]

    operations = [
        migrations.RunPython(backfill_assigned_design, migrations.RunPython.noop),
    ]
