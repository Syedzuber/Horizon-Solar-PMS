"""rebaseline_site — the deliberate re-plan of one site's baseline (SCHED-2e).

Re-snapshots every in-block task's baseline (Task.baseline_start / baseline_due) from the
site's PURE PLAN as it stands now: the Civil due date, capacity band and working-day
switch, without the forecast, late-finish or first-dating shifts. It works whether or not
Civil has started; that is its whole point, since the scheduler freezes the baseline once
Civil starts.

DRY RUN BY DEFAULT. Without --apply each task that would change is printed, old plan ->
new plan, and nothing is written. With --apply the same lines are printed, the baselines
are written under the site lock, and ONE ActivityLog row `baseline_reset` is logged with
no actor and the note in its text (even when nothing changed: the decision is the record).

THE NOTE IS MANDATORY: it is the only record of why the plan moved.

    python manage.py rebaseline_site --site 1901 --note "Civil re-planned after rain"
    python manage.py rebaseline_site --site 1901 --note "Civil re-planned after rain" --apply

A PM-facing button for this comes in SCHED-3; until then this command is the only way.
"""
from django.core.management.base import BaseCommand, CommandError

from projects.models import Project
from projects.scheduling import rebaseline_site


def _show(value):
    return value.isoformat() if value else '-'


class Command(BaseCommand):
    help = "Re-snapshot one site's baseline from its pure plan (dry run unless --apply)."

    def add_arguments(self, parser):
        parser.add_argument('--site', metavar='CODE', required=True,
                            help='The site, by its project ID / site code.')
        parser.add_argument('--note', required=True,
                            help='Why the plan is being reset. Logged with the reset.')
        parser.add_argument('--apply', action='store_true',
                            help='Write the baseline. Without it this is a dry run.')

    def handle(self, *args, **options):
        apply = options['apply']
        site = Project.objects.filter(project_id=options['site'], is_deleted=False).first()
        if site is None:
            raise CommandError(f"No site with code {options['site']!r}.")
        try:
            result = rebaseline_site(site, options['note'], apply=apply)
        except ValueError as exc:
            raise CommandError(str(exc))

        self.stdout.write(f"{'APPLY' if apply else 'DRY RUN (nothing written; add --apply to write)'}"
                          f" - site {site.project_id}, Civil due {_show(result.anchor)}")
        for task, (old_start, old_due), (new_start, new_due) in result.changes:
            self.stdout.write(f'  {task.task_name[:45]:<45} '
                              f'{_show(old_start)} -> {_show(old_due)}  =>  '
                              f'{_show(new_start)} -> {_show(new_due)}')
        if apply:
            self.stdout.write(f'Baselines written: {result.written}; '
                              f'logged baseline_reset.')
        else:
            self.stdout.write(f'Baselines to write: {len(result.changes)}')
