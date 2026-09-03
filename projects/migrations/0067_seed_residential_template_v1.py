# Prompt 0.4 — migrate the Residential template out of Python source and into data,
# as RESIDENTIAL v1, then record provenance on every task that already exists.
#
# This migration imports from projects.utils by instruction: build_residential_phases()
# IS the thing being migrated, and after this runs it stops being executed at runtime and
# stays only as the seed this migration read. The usual "never import app code into a
# migration" caution is accepted here deliberately — the seeding helper takes its model
# classes as arguments, so it operates on the HISTORICAL models handed over below and not
# on the concrete ones.
#
# THAT REASONING WAS INCOMPLETE, AND IT COST A DEPLOY. Handing over the historical class
# stops the helper reading the WRONG model; it does nothing to stop the helper writing a
# field that model does not have YET. Prompt 1.3a added `is_mirror=` to
# seed_task_template_version() for migration 0075's benefit. `is_mirror` arrives in 0074 —
# seven migrations after this one — so on any database where 0067 had not already applied,
# this line raised `TypeError: TaskTemplateTask() got unexpected keyword arguments:
# 'is_mirror'`, migrate exited non-zero, and gunicorn never started. Production was rolled
# back on 03 Sep 2026.
#
# The fix is in the helper, not here: optional fields now go through
# utils.kwargs_for_model_state(), which drops any the handed-over model state does not
# carry. NOTHING IN THIS FILE CHANGED except this comment — the incompatibility was never
# in the migration. See rule R-22 in docs/execution-model.md, and the guard in
# projects/tests_migration_chain.py that would have caught it.
#
# Nothing else here may be edited. 0067 has NOT applied on production as of 03 Sep 2026
# (production sits at 0066), which is the only reason editing a committed migration was on
# the table at all. That licence expires the moment this chain applies to production.
from django.db import migrations

from projects.utils import (
    RESIDENTIAL_TEMPLATE_CODE,
    RESIDENTIAL_TEMPLATE_LABEL,
    _get_duration,
    build_residential_phases,
    seed_task_template_version,
)


def seed_v1(apps, schema_editor):
    TaskTemplate         = apps.get_model('projects', 'TaskTemplate')
    TaskTemplatePhase    = apps.get_model('projects', 'TaskTemplatePhase')
    TaskTemplateTask     = apps.get_model('projects', 'TaskTemplateTask')
    TaskDurationTemplate = apps.get_model('projects', 'TaskDurationTemplate')
    Task                 = apps.get_model('projects', 'Task')

    # Idempotent: a database that already carries a RESIDENTIAL template — one
    # bootstrapped by attach_residential_template() on a schema built without
    # migrations — is left exactly as it is.
    if TaskTemplate.objects.filter(code=RESIDENTIAL_TEMPLATE_CODE).exists():
        print(f'  [0067] {RESIDENTIAL_TEMPLATE_CODE} template already present — skipping seed.')
        return

    # Durations resolve EXACTLY as _get_duration() does at runtime today: a
    # TaskDurationTemplate row if one exists, else RESIDENTIAL_DURATION_DEFAULTS, else 1.
    # Read from the live table rather than from migration 0034's seed list, so an admin's
    # edits carry across unchanged and a project activated today gets what one activated
    # yesterday got.
    overrides = {
        row.task_name: row.duration_days
        for row in TaskDurationTemplate.objects.filter(project_type='residential')
    }

    template = seed_task_template_version(
        template_model=TaskTemplate,
        phase_model=TaskTemplatePhase,
        task_model=TaskTemplateTask,
        code=RESIDENTIAL_TEMPLATE_CODE,
        label=RESIDENTIAL_TEMPLATE_LABEL,
        project_type='Residential',   # Project.project_type vocabulary (capitalised)
        version_no=1,
        phases=build_residential_phases(),
        duration_resolver=lambda name: _get_duration(name, overrides),
        created_by=None,
    )

    n_phases = TaskTemplatePhase.objects.filter(template=template).count()
    n_tasks  = TaskTemplateTask.objects.filter(phase__template=template).count()
    print(f'  [0067] Seeded {RESIDENTIAL_TEMPLATE_CODE} v1 (active): '
          f'{n_phases} phases, {n_tasks} tasks.')

    # --- Backfill Task.template_task -------------------------------------------------
    # Best-effort provenance on tasks that already exist. Matched on (phase label, task
    # label) because all 52 template task names are distinct and Task.task_name is a plain
    # copy of the template label.
    #
    # Restricted to Residential projects: a hand-created task on an OPEX site could
    # otherwise collide by name with a Residential template row and be given provenance it
    # does not have. Tasks that do not match are LEFT NULL on purpose — they were added by
    # hand and belong to no template row.
    lookup = {
        (tt.phase.label, tt.label): tt.pk
        for tt in TaskTemplateTask.objects.filter(
            phase__template=template).select_related('phase')
    }

    matched = unmatched = 0
    unmatched_projects = set()
    pending = []
    # .iterator() so a large task table is streamed rather than held in memory at once.
    rows = (Task.objects
            .filter(phase__project__project_type='Residential')
            .select_related('phase', 'phase__project')
            .iterator(chunk_size=2000))
    for task in rows:
        tt_pk = lookup.get((task.phase.phase_name, task.task_name))
        if tt_pk is None:
            unmatched += 1
            unmatched_projects.add(task.phase.project_id)
            continue
        task.template_task_id = tt_pk
        pending.append(task)
        matched += 1
        if len(pending) >= 2000:
            Task.objects.bulk_update(pending, ['template_task'], batch_size=1000)
            pending = []
    if pending:
        Task.objects.bulk_update(pending, ['template_task'], batch_size=1000)

    print(f'  [0067] Backfill: {matched} matched, {unmatched} unmatched, '
          f'{len(unmatched_projects)} project(s) with >=1 unmatched task.')


def unseed_v1(apps, schema_editor):
    """Reverse: drop RESIDENTIAL v1 and the provenance it wrote. Nothing else."""
    TaskTemplate = apps.get_model('projects', 'TaskTemplate')
    Task         = apps.get_model('projects', 'Task')

    template = TaskTemplate.objects.filter(
        code=RESIDENTIAL_TEMPLATE_CODE, version_no=1).first()
    if template is None:
        return
    # SET_NULL would do this on delete anyway; explicit so the reverse reads plainly and
    # does not depend on cascade behaviour to be correct.
    Task.objects.filter(
        template_task__phase__template=template).update(template_task=None)
    template.delete()   # cascades to its phases and tasks


class Migration(migrations.Migration):

    dependencies = [
        ('projects', '0066_task_template'),
    ]

    operations = [
        migrations.RunPython(seed_v1, unseed_v1),
    ]
