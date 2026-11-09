"""SCHED-2e — backfill Task.baseline_start / baseline_due on the sites already scheduled
(spec "Baseline amendment (9 Nov 2026)").

WHICH SITES: every site with at least one `system` due date, the 20 IPGCL sites the
9 Nov production run scheduled. On each, every in-block task gets its PURE PLAN: the
Civil task its own due date and counted-back start; every other task the chain arithmetic
from the Civil due date, capacity band and working-day switch, with no forecast,
late-finish or first-dating shift. Today's due dates are NOT copied: on a site whose
forecast already moved (1901, 1908) they are not the plan.

WRITES NOTHING ELSE: only the two baseline columns, by filter().update(), so no due
date, source, start or ActivityLog row changes. Race: none in practice; this runs in
`migrate` before gunicorn serves a request.

R-22, AND WHY THIS IS SAFE ANYWAY. This migration calls live code,
projects.scheduling.load_site_for_plan() and pure_baselines_for_site(), so the pure plan
is the scheduler's own walk and not a second copy of its arithmetic (Zuber, 9 Nov). Both
are handed this migration's HISTORICAL models and rows: load_site_for_plan() filters only
on fields that exist from 0137 on, pure_baselines_for_site() never queries and reads only
attributes that exist from 0137 on, and the writes below use the historical Task. On an
empty database (the migration-chain test) no site qualifies and no live code runs at all.
The remaining exposure: a future change to either function that reads a later field
would break this migration on a database restored from before 0141 that has scheduled
sites. That is recorded in SECONDARY_FINDINGS.md.

REVERSE: both columns back to null on every task.
"""
from django.db import migrations


def backfill_baseline(apps, schema_editor):
    # Lazy import: the module imports projects.models, which the migration loader must
    # not import at module load time (and a top-level import would run it on every
    # `migrate`, not only when this operation runs).
    from projects.scheduling import load_site_for_plan, pure_baselines_for_site

    Task = apps.get_model('projects', 'Task')
    Project = apps.get_model('projects', 'Project')
    ScheduleBlock = apps.get_model('projects', 'ScheduleBlock')
    SystemSettings = apps.get_model('projects', 'SystemSettings')

    # The sites the scheduler has already dated: at least one task carries a system date.
    site_ids = (Task.objects.filter(due_date_source='system')
                .values_list('phase__project_id', flat=True).distinct())
    sites = list(Project.objects.filter(pk__in=list(site_ids)).order_by('pk'))
    if not sites:
        return
    # The working-day switch as stored (row pk=1, SystemSettings.get()'s row); its model
    # default, False, when the row was never created.
    skip_sundays = (SystemSettings.objects.filter(pk=1)
                    .values_list('schedule_skip_sundays', flat=True).first()) or False

    for site in sites:
        tasks, blocks = load_site_for_plan(site, task_model=Task, block_model=ScheduleBlock)
        targets = pure_baselines_for_site(site, tasks, blocks, skip_sundays)
        for pk, (start, due) in targets.items():
            if start is None and due is None:
                continue
            Task.objects.filter(pk=pk).update(baseline_start=start, baseline_due=due)


def clear_baseline(apps, schema_editor):
    Task = apps.get_model('projects', 'Task')
    (Task.objects.filter(baseline_start__isnull=False)
     | Task.objects.filter(baseline_due__isnull=False)).update(baseline_start=None,
                                                               baseline_due=None)


class Migration(migrations.Migration):

    dependencies = [
        ('projects', '0140_sched2e_task_baseline'),
    ]

    operations = [
        migrations.RunPython(backfill_baseline, clear_baseline),
    ]
