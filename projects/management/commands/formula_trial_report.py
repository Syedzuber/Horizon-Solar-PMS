"""
formula_trial_report — every calculated BOQ change since a date, as CSV (B-2c, C4).

    python manage.py formula_trial_report --since 2026-11-25
    python manage.py formula_trial_report --since 2026-11-25 --until 2026-11-26 --out trial.csv
    python manage.py formula_trial_report --since 2026-11-25 --site JKPCH064

READ-ONLY. It reads BOQRecalculation (one row per calculated quantity that changed, written
by boq_formulas.apply_formulas on every hooked path) and writes nothing to the database.
--since and --until are calendar days in India time: --since 2026-11-25 starts at 00:00 IST
that day; --until is inclusive (up to the end of that day).

THE RULE KEY (R1 ... R25) comes from the rules file through the rule's target item: a rule
row has no key, and each target belongs to exactly one key (see load_formula_rules). A rule
whose target is not in the file shows an empty key.

DELETED SITES ARE INCLUDED, with site_deleted = yes. This is an audit of what the engine
changed, and fix_tin_shade_ballast --apply-counts recalculates deleted sites too (it counts
them on purpose; see its _affected_lines). Leaving them out would hide changes that happened.

CSV to stdout unless --out; the row count goes to stderr so stdout stays a clean CSV.
"""
import csv
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from django.core.management.base import BaseCommand, CommandError

from projects.boq_formulas import _num

from .load_formula_rules import DEFAULT_FILE, keys_by_target

IST = ZoneInfo('Asia/Kolkata')

COLUMNS = ('when_ist', 'site', 'site_deleted', 'tender', 'rule_key', 'item_code',
           'item_description', 'unit', 'before', 'after', 'change', 'path', 'who',
           'driving_correction', 'explanation')

#: Who a record with no actor was written by. Only fix_tin_shade_ballast writes with none.
SYSTEM = 'system'


def _day(text, option):
    try:
        return datetime.strptime(text, '%Y-%m-%d').date()
    except ValueError:
        raise CommandError(f'{option} is a date like 2026-11-25, not {text!r}.')


def _qty(value):
    return '' if value is None else _num(value)


def change_kind(before, after):
    if after is None:
        return 'removed'
    if before is None:
        return 'added'
    if after == 0:
        return 'set to 0'
    return 'changed'


def report_rows(since, until=None, site=None, keys=None):
    """The report's rows (dicts keyed by COLUMNS), oldest first. One query."""
    # Lazy import: management commands import models at call time in this project.
    from projects.models import BOQRecalculation

    keys = keys or {}
    # Each record with its site, tender, author and rule target joined in, so the whole
    # report is one SELECT however many rows it has.
    records = (BOQRecalculation.objects
               .filter(created_at__gte=since)
               .select_related('boq__project__program', 'actor__user', 'rule__target')
               .order_by('created_at', 'pk'))
    if until is not None:
        records = records.filter(created_at__lt=until)
    if site:
        records = records.filter(boq__project__project_id=site)
    for r in records:
        project = r.boq.project
        if r.actor_id:
            user = r.actor.user
            who = f'{user.get_full_name() or user.username} ({user.username})'
        else:
            who = SYSTEM
        yield {
            'when_ist': r.created_at.astimezone(IST).strftime('%Y-%m-%d %H:%M:%S'),
            'site': project.project_id,
            'site_deleted': 'yes' if project.is_deleted else 'no',
            'tender': project.program.name if project.program_id else '',
            'rule_key': keys.get(r.rule.target.code, ''),
            'item_code': r.item_code,
            'item_description': r.item_description,
            'unit': r.item_unit,
            'before': _qty(r.quantity_before),
            'after': _qty(r.quantity_after),
            'change': change_kind(r.quantity_before, r.quantity_after),
            'path': r.get_source_display(),
            'who': who,
            'driving_correction': r.driving_correction_id or '',
            'explanation': r.explanation,
        }


class Command(BaseCommand):
    help = ('Read-only: every calculated BOQ quantity that changed since a date (B-2c trial '
            'report), as CSV.')

    def add_arguments(self, parser):
        parser.add_argument('--since', required=True, metavar='YYYY-MM-DD',
                            help='First day (India time) to include.')
        parser.add_argument('--until', metavar='YYYY-MM-DD',
                            help='Last day (India time) to include; default: up to now.')
        parser.add_argument('--site', help='Only this site code.')
        parser.add_argument('--out', metavar='PATH', help='Write the CSV here, not stdout.')
        parser.add_argument('--file', default=str(DEFAULT_FILE),
                            help='The rules file the keys are read from.')

    def handle(self, *args, **options):
        since = datetime.combine(_day(options['since'], '--since'), time.min, tzinfo=IST)
        until = None
        if options['until']:
            until = datetime.combine(_day(options['until'], '--until') + timedelta(days=1),
                                     time.min, tzinfo=IST)
            if until <= since:
                raise CommandError('--until is before --since.')
        keys = keys_by_target(Path(options['file']))
        rows = report_rows(since, until, options['site'], keys)

        if options['out']:
            with open(options['out'], 'w', encoding='utf-8-sig', newline='') as handle:
                count = self._write(handle, rows, '\r\n')
            self.stderr.write(f'{count} row(s) written to {options["out"]}')
        else:
            # '\n' on the console: Django's stdout wrapper is in text mode, so '\r\n' would
            # come out as '\r\r\n' on Windows.
            count = self._write(self.stdout, rows, '\n')
            self.stderr.write(f'{count} row(s)')

    @staticmethod
    def _write(handle, rows, lineterminator):
        writer = csv.DictWriter(handle, fieldnames=COLUMNS, lineterminator=lineterminator)
        writer.writeheader()
        count = 0
        for row in rows:
            writer.writerow(row)
            count += 1
        return count
