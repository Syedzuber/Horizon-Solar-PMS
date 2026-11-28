"""
RPT-B1 — print the Reports foundations' figures for one period, to check them against
production before the Reports page (RPT-B2) is built on them.

    python manage.py report_data_check --period yesterday
    python manage.py report_data_check --date 2026-11-25
    python manage.py report_data_check --from 2026-11-16 --to 2026-11-25

READ-ONLY. It calls report_activity.activity_report() and prints; it writes nothing, sends
nothing and needs no user. Safe to run from the Railway console.

The period is read by report_periods.parse_period(), the same function the page will use,
so a period this command accepts is exactly one the page accepts, with the same errors.
"""

import time

from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from projects.report_activity import activity_report
from projects.report_periods import PERIOD_DATE, PERIOD_KEYS, PERIOD_RANGE, parse_period

TOP_USERS = 15
SITES_PER_USER = 12


def _when(moment):
    return timezone.localtime(moment).strftime('%d %b %H:%M:%S') if moment else 'never'


class Command(BaseCommand):
    help = 'Print the Reports activity figures for one period (read-only).'

    def add_arguments(self, parser):
        parser.add_argument('--period', choices=PERIOD_KEYS,
                            help='today, yesterday, week, month, date or range')
        parser.add_argument('--date', help='YYYY-MM-DD (implies --period date)')
        # `from` is a Python keyword, hence the dest.
        parser.add_argument('--from', dest='from_date', help='YYYY-MM-DD (implies range)')
        parser.add_argument('--to', dest='to_date', help='YYYY-MM-DD (implies range)')

    def handle(self, *args, **options):
        key = options['period']
        if key is None and options['date']:
            key = PERIOD_DATE
        if key is None and (options['from_date'] or options['to_date']):
            key = PERIOD_RANGE
        period, error = parse_period({'period': key, 'date': options['date'],
                                      'from': options['from_date'], 'to': options['to_date']})
        if error:
            raise CommandError(error)

        # CaptureQueriesContext forces a debug cursor for its duration only, so the count
        # is real on production (DEBUG=False) too; nothing else changes.
        with CaptureQueriesContext(connection) as queries:
            started = time.perf_counter()
            report = activity_report(period)
            elapsed_ms = (time.perf_counter() - started) * 1000

        out = self.stdout.write
        cur, prev = report.current, report.previous
        out(f'Period:   {period.label}   [{period.start_dt.isoformat()} .. '
            f'{period.end_dt.isoformat()})')
        out(f'Previous: {period.previous.label}' if period.previous else 'Previous: none')
        out('')

        users = cur.active_users
        out(f'ACTIVE USERS: {users.users} ({users.actions} actions, '
            f'{users.actions_without_site} with no site)   previous: '
            f'{prev.active_users.users} ({prev.active_users.actions} actions)')
        for role in users.by_role:
            out(f'  {role.role:22} users={role.users:3}  actions={role.actions:5}')
        out('')

        out(f'SIGNED IN, NO ACTION: {len(cur.signed_in_no_action)}   previous: '
            f'{len(prev.signed_in_no_action)}')
        for row in cur.signed_in_no_action:
            out(f'  {row.name} ({row.role}) - {row.sign_ins} sign-in(s), last '
                f'{_when(row.last_signed_in_at)}')
        out('')

        out(f'OPEN TASKS, NO ACTIVITY (tasks as of now): {len(cur.open_tasks_no_activity)}'
            f'   previous: {len(prev.open_tasks_no_activity)}')
        for row in cur.open_tasks_no_activity:
            out(f'  {row.name} ({row.role}) - open {row.open_tasks}, overdue '
                f'{row.overdue_tasks}, last action {_when(row.last_action_at)}')
        out('')

        sites = cur.active_sites
        out(f'ACTIVE SITES: {sites.total} = {sites.in_execution} in execution + '
            f'{sites.in_design} in design (Draft); touched only by bulk actions: '
            f'{sites.bulk_only} (not counted)')
        out(f'  previous: {prev.active_sites.total} = {prev.active_sites.in_execution} + '
            f'{prev.active_sites.in_design}; bulk-only {prev.active_sites.bulk_only}')
        out('')

        out(f'BULK ACTIONS: {len(cur.bulk_actions)}   previous: {len(prev.bulk_actions)}')
        for batch in cur.bulk_actions:
            out(f'  {batch.actor_name:20} {batch.action_code:32} {batch.source:6} '
                f'{_when(batch.started_at)} - {_when(batch.ended_at)}  sites={batch.site_count}')
        out('')

        out(f'TOP {TOP_USERS} USERS (most actions):')
        for row in sorted(users.rows, key=lambda r: (-r.actions, r.name.lower()))[:TOP_USERS]:
            labels = [cur.sites[pk].label for pk in row.site_ids if pk in cur.sites]
            more = len(labels) - SITES_PER_USER
            shown = ', '.join(labels[:SITES_PER_USER]) + (f' +{more} more' if more > 0 else '')
            bulk = f'; +{row.bulk_only_site_count} via bulk only' if row.bulk_only_site_count else ''
            out(f'  {row.name} ({row.role}) actions={row.actions} sites={len(labels)}'
                f' no-site={row.actions_without_site} last={_when(row.last_action_at)}{bulk}')
            if shown:
                out(f'      {shown}')
        out('')
        out(f'Queries: {len(queries.captured_queries)}   Wall time: {elapsed_ms:.1f} ms')
