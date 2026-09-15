"""
Dump every design figure the Design Head can see, as deterministic JSON.

WHY THIS EXISTS
---------------
A session that changes only what a figure SAYS (a caption, a docstring) has one safety
property: no figure moves. That is provable only by computing every figure before and
after and diffing the two, and the tool to do it has been rebuilt from scratch more than
once. It lives here so the next figure session starts with it.

WHAT IS DUMPED
--------------
  * design_analytics.compute() with EVERY metric selected, once per OPEX tender and once
    over all OPEX tenders combined — the quality analytics page at both scopes.
  * design_metrics.tender_metrics() once per OPEX tender — the tender dashboard.

Figures only. A Metric is written as its key, never its description, so a caption edit
shows as no diff at all. Model rows are written as `<model>:<pk>`, Decimals as strings,
dates as ISO text. Keys are sorted, so two runs over the same rows are byte-identical.

`--today` pins the date tender_metrics() measures overdue and queue ages against. Two runs
on different days differ in those ages without anything being wrong, so pass the same
date to both runs being compared.

READ ONLY. Every read runs inside a transaction that is rolled back, so even a read with a
side effect it should not have cannot persist one.

    python manage.py design_figure_snapshot --today 2026-09-15 --out before.json
    ... change captions ...
    python manage.py design_figure_snapshot --today 2026-09-15 --out after.json
    fc before.json after.json        (or: git diff --no-index before.json after.json)
"""
import json
from datetime import date, datetime
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import models, transaction

from projects.design_analytics import METRIC_CATALOGUE, Metric, compute
from projects.design_metrics import tender_metrics
from projects.models import Program


def _plain(value):
    """One JSON-safe value, recursively. Anything unrecognised is an error, not a repr —
    a repr can carry an object address and would make two identical runs differ."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Metric):
        return value.key
    if isinstance(value, models.Model):
        return f'{value._meta.label_lower}:{value.pk}'
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        items = [_plain(v) for v in value]
        return sorted(items, key=repr) if isinstance(value, (set, frozenset)) else items
    raise CommandError(f'design_figure_snapshot: cannot serialise {type(value).__name__}')


class Command(BaseCommand):
    help = 'Dump every design analytics and tender dashboard figure as deterministic JSON.'

    def add_arguments(self, parser):
        parser.add_argument('--today', help='YYYY-MM-DD for tender_metrics(); default today')
        parser.add_argument('--out', help='file to write; default stdout')

    def handle(self, *args, **options):
        today = None
        if options['today']:
            try:
                today = date.fromisoformat(options['today'])
            except ValueError:
                raise CommandError('--today must be YYYY-MM-DD')

        every_metric = {m.key for m in METRIC_CATALOGUE}
        with transaction.atomic():
            programs = list(Program.objects
                            .filter(is_deleted=False, program_type='OPEX')
                            .order_by('pk'))
            snapshot = {
                'analytics_all_tenders': compute(programs, every_metric),
                'analytics_per_tender': {
                    p.pk: compute([p], every_metric) for p in programs},
                'tender_dashboard': {
                    p.pk: tender_metrics(p, today=today) for p in programs},
            }
            text = json.dumps(_plain(snapshot), indent=1, sort_keys=True,
                              ensure_ascii=False)
            transaction.set_rollback(True)

        if options['out']:
            with open(options['out'], 'w', encoding='utf-8', newline='\n') as fh:
                fh.write(text + '\n')
            self.stderr.write(f'{len(programs)} tender(s) -> {options["out"]}')
        else:
            self.stdout.write(text)
