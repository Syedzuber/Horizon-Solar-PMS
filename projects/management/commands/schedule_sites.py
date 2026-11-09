"""schedule_sites — run the RESCO auto-scheduler (SCHED-2) over one site, a tender or all.

DRY RUN BY DEFAULT. Without --apply nothing is written: each site's plan is printed with
the due dates it would write, the Civil planned start it would set, the dates it would
clear, and how many of its tasks would then be overdue (already overdue / newly overdue
because of the schedule). With
--apply the same lines are printed and each site is written in its own transaction
(scheduling.schedule_site), logging one `due_dates_scheduled` ActivityLog row per site
that changed.

    python manage.py schedule_sites --site 1901
    python manage.py schedule_sites --tender IPGCLG
    python manage.py schedule_sites --all
    python manage.py schedule_sites --all --apply
    python manage.py schedule_sites --site 2112 -v 2     # every block, every task

SCHED-2b: a site with a block open past its due date prints a `forecast:` line, and the
totals count those sites and the dates the forecast moves. Production runs
`schedule_sites --all --apply` daily at 06:30 IST (01:00 UTC), before the morning report,
so the forecast moves on by a day each morning. With -v 2 each site also lists its blocks:
sequence, name, planned start -> due, each task's action, and `forecast` on the block
that re-anchored the rest (or `first-dated` on a block started after today because it
had passed its plan before it was ever dated); that is the view to check before any
--apply.

Also the way to apply a working-day switch flipped outside the Admin screen, and to
retry a site whose after-commit trigger failed (the failure is in the server log).
"""
from collections import Counter

from django.core.management.base import BaseCommand, CommandError

from projects.models import Project
from projects.scheduling import (
    BAND_LABELS, REASON_NOT_ELIGIBLE, candidate_sites, eligible_sites,
    schedule_site,
)

# The extra line Zuber asked for: eligible sites big enough that the 101+ band's fixed
# lengths may not fit them, listed so the table can be checked against real sites.
LARGE_SITE_KWP = 300


class Command(BaseCommand):
    help = 'Plan (default) or --apply RESCO auto-scheduled due dates for a site, a tender or all sites.'

    def add_arguments(self, parser):
        scope = parser.add_mutually_exclusive_group(required=True)
        scope.add_argument('--site', metavar='CODE', help='One site, by its project ID / site code.')
        scope.add_argument('--tender', metavar='CODE', help="One tender's sites, by its short tender code.")
        scope.add_argument('--all', action='store_true', help='Every activated RESCO site.')
        parser.add_argument('--apply', action='store_true',
                            help='Write the dates. Without it this is a dry run.')

    def handle(self, *args, **options):
        apply = options['apply']
        verbose = options['verbosity'] >= 2
        if options['site']:
            sites = list(Project.objects.filter(project_id=options['site'], is_deleted=False))
            if not sites:
                raise CommandError(f"No site with code {options['site']!r}.")
        elif options['tender']:
            sites = list(candidate_sites().filter(program__short_tender_code=options['tender'])
                         .order_by('project_id'))
            if not sites:
                raise CommandError(f"No activated RESCO sites in tender {options['tender']!r}.")
        else:
            sites = list(candidate_sites().order_by('project_id'))

        self.stdout.write(f"{'APPLY' if apply else 'DRY RUN (nothing written; add --apply to write)'}"
                          f" - {len(sites)} site(s)")
        outcomes = Counter()
        totals = Counter()
        scheduled = []
        for site in sites:
            result = schedule_site(site, apply=apply)
            self.stdout.write(result.summary_line())
            if result.forecast is not None:
                totals['forecast_sites'] += 1
                self.stdout.write(self._forecast_line(result, apply))
            if verbose:
                for line in self._block_lines(result):
                    self.stdout.write(line)
            outcomes[result.reason or 'scheduled'] += 1
            if result.reason is None:
                scheduled.append(result)
            totals['write'] += result.written if apply else result.to_write
            totals['start'] += result.started if apply else result.to_start
            totals['clear'] += result.cleared if apply else result.to_clear
            totals['forecast_moved'] += result.forecast_moved if apply else result.to_forecast
            if result.reason != REASON_NOT_ELIGIBLE:
                totals['overdue_after'] += result.overdue_after or 0
                totals['overdue_already'] += result.overdue_already or 0
                totals['overdue_new'] += result.overdue_new or 0

        verb = 'written' if apply else 'to write'
        self.stdout.write('')
        self.stdout.write('Totals: ' + ', '.join(f'{k} {v}' for k, v in sorted(outcomes.items())))
        self.stdout.write(f"Due dates {verb}: {totals['write']}; Civil starts {verb}: {totals['start']}; "
                          f"{'cleared' if apply else 'to clear'}: {totals['clear']}")
        self.stdout.write(f"Tasks overdue after: {totals['overdue_after']} "
                          f"(already overdue {totals['overdue_already']}, "
                          f"newly overdue from scheduling {totals['overdue_new']})")
        self.stdout.write(f"Sites with an open late block: {totals['forecast_sites']}")
        self.stdout.write(f"Dates moved by forecast: {totals['forecast_moved']}"
                          f"{'' if apply else ' (would move)'}")
        listed = '; '.join(f'{r.site.project_id} ({BAND_LABELS[r.band]}, write {r.to_write})'
                           for r in scheduled) or 'none'
        self.stdout.write(f'Scheduled sites: {len(scheduled)} - {listed}')

        # Over the same eligible set the scheduler uses, not just the sites listed above.
        large = list(eligible_sites().filter(dc_capacity_kw__gt=LARGE_SITE_KWP)
                     .order_by('-dc_capacity_kw').values_list('project_id', 'dc_capacity_kw'))
        listed = ', '.join(f'{code} ({kwp} kWp)' for code, kwp in large) or 'none'
        self.stdout.write(f'Eligible sites above {LARGE_SITE_KWP} kWp DC '
                          f'(band {BAND_LABELS["101_plus"]}): {len(large)} - {listed}')

    @staticmethod
    def _forecast_line(result, apply):
        """The forecast under a site's line: the open late block, its planned due date,
        and the dates moved (written, with --apply; to write, without)."""
        late = result.forecast
        moved = result.forecast_moved if apply else result.to_forecast
        if late.first_dated is not None:
            return (f'    forecast: {late.block.name} not yet dated and already past plan; '
                    f'started {late.first_dated.isoformat()}; {moved} dates '
                    f'{"written" if apply else "to write"} from it')
        return (f'    forecast: {late.block.name} open past due {late.due.isoformat()}; '
                f'{moved} later dates {"moved" if apply else "to move"} to start '
                f'{result.forecast_start.isoformat()}')

    @staticmethod
    def _block_lines(result):
        """-v 2: one line per block. A zero-day block (nothing applicable on the site)
        shows '-' for its dates."""
        lines = []
        for bp in result.blocks:
            start = bp.start.isoformat() if bp.start else '-'
            due = bp.due.isoformat() if bp.due else '-'
            marker = (' forecast' if bp.forecast_after
                      else ' first-dated' if bp.first_dated else '')
            actions = '; '.join(f'{tp.task.task_name}={tp.action}' for tp in bp.tasks) or 'no tasks'
            lines.append(f'      {bp.block.sequence} {bp.block.name:<16} {start} -> {due}'
                         f'{marker}  [{actions}]')
        return lines
