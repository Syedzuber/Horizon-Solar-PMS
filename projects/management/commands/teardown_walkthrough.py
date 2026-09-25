"""
Management command: remove walkthrough AREAS that `seed_walkthrough` recorded — only
where that can be done exactly, and otherwise refuse and say why.

    python manage.py teardown_walkthrough --dry-run              # verdict per area
    python manage.py teardown_walkthrough --only reference       # remove one area
    python manage.py teardown_walkthrough                        # every area, or nothing

THE SUPPORTED RESET IS DROP DATABASE AND RE-SEED
------------------------------------------------
Almost every area drives the product, and the product writes StatusTransition rows —
the append-only ledger whose `delete()` raises AppendOnlyViolation. Removing such an
area exactly would mean force-deleting ledger rows through `QuerySet.delete()`, and
this command will not do that (sign-off item 3): there is one sanctioned bypass in the
codebase, in `teardown_opex_test_data`, and it says there must not be a second. So an
area whose manifest holds a StatusTransition row, or whose deletion would reach one,
is REFUSED, with the instruction to drop the database instead. The two commands are at
the top of docs/WALKTHROUGH_DATA.md.

WHAT IS CHECKED BEFORE ANYTHING IS DELETED
------------------------------------------
Django's own deletion Collector is run over the area's rows first, so the command knows
exactly what a delete would touch — cascades, SET_NULL updates, PROTECT and RESTRICT
— before it touches anything. An area is refused if:

  1. its manifest holds a StatusTransition row, or the cascade would reach one;
  2. the cascade would delete, or a SET_NULL would modify, any row that is not in the
     manifest of the areas being removed (a row a walker made by hand, say);
  3. a PROTECT or RESTRICT stands in the way (another area still depends on it).

All requested areas are checked; if any is refused, NOTHING is deleted. Otherwise the
whole removal runs in one transaction.

Refuses to run anywhere `seed_walkthrough` would refuse (the same four checks).
"""
from django.apps import apps
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models.deletion import Collector, ProtectedError, RestrictedError

from projects.management.commands._walkthrough_support import (
    AreaManifest, database_fingerprint, default_manifest_path, print_db_banner,
    require_walkthrough_database,
)
from projects.management.commands.seed_walkthrough import AREAS

LEDGER = 'projects.StatusTransition'


def _label(obj):
    return f'{obj._meta.app_label}.{type(obj).__name__}'


def _collect(collector, instances):
    """Collector.collect() wants a homogeneous batch, so feed it one model at a time."""
    by_model = {}
    for obj in instances:
        by_model.setdefault(type(obj), []).append(obj)
    for batch in by_model.values():
        collector.collect(batch)


class Command(BaseCommand):
    help = ('Remove walkthrough areas exactly, by manifest — or refuse, naming why. '
            'Areas that carry status history cannot be removed this way; drop the '
            'database instead.')

    def add_arguments(self, parser):
        parser.add_argument('--only', choices=AREAS, action='append', default=[],
                            help='Remove only this area (repeatable). Default: all.')
        parser.add_argument('--dry-run', action='store_true',
                            help='Report the verdict per area and delete nothing.')
        parser.add_argument('--manifest', type=str, default='',
                            help='Manifest path. Default: '
                                 '~/.horizon-pms-walkthrough/<database name>.json')

    def handle(self, *args, **options):
        print_db_banner(self)
        require_walkthrough_database(self)

        path = options['manifest'].strip() or default_manifest_path()
        manifest = AreaManifest.load(path)
        if manifest is None:
            raise CommandError(f'No readable manifest at {path}. Nothing is deleted '
                               f'without one. Reset with DROP DATABASE and re-seed.')
        if manifest.fingerprint != database_fingerprint():
            raise CommandError(
                f'The manifest at {path} belongs to a different database '
                f'({manifest.fingerprint}, this is {database_fingerprint()}). Refusing.')

        requested = options['only'] or [a for a in AREAS if a in manifest.areas]
        absent = [a for a in requested if a not in manifest.areas]
        if absent:
            raise CommandError(f'Not in the manifest (never seeded here): {", ".join(absent)}')
        if not requested:
            self.stdout.write('The manifest records no areas. Nothing to do.')
            return

        # Every row of every requested area, resolved. Removing several areas at once is
        # checked as one set, so an area may cascade into another that is also going.
        targets, keys = {}, set()
        for area in requested:
            instances = []
            for row in manifest.entries(area):
                model = apps.get_model(row['model'])
                obj = model.objects.filter(pk=row['pk']).first()
                if obj is not None:
                    instances.append(obj)
                    keys.add((row['model'], row['pk']))
            targets[area] = instances

        verdicts = {area: self._verdict(area, targets[area], keys, manifest)
                    for area in requested}

        self.stdout.write('Verdict per area:')
        for area in requested:
            reasons = verdicts[area]
            count = len(targets[area])
            if reasons:
                self.stdout.write(self.style.ERROR(f'  {area:<12} REFUSED  ({count} rows)'))
                for reason in reasons:
                    self.stdout.write(f'      - {reason}')
            else:
                self.stdout.write(self.style.SUCCESS(f'  {area:<12} removable ({count} rows)'))

        refused = [a for a in requested if verdicts[a]]
        if options['dry_run']:
            self.stdout.write(self.style.WARNING('DRY RUN - nothing deleted.'))
            return
        if refused:
            raise CommandError(
                f'Refusing: {", ".join(refused)} cannot be removed exactly. Nothing was '
                f'deleted. Reset with DROP DATABASE and re-seed (docs/WALKTHROUGH_DATA.md).')

        with transaction.atomic():
            collector = Collector(using='default')
            everything = [obj for area in requested for obj in targets[area]]
            if everything:
                _collect(collector, everything)
                deleted, per_model = collector.delete()
            else:
                deleted, per_model = 0, {}
            for area in requested:
                manifest.areas.pop(area, None)
            if manifest.areas:
                manifest.save()
            else:
                manifest.path.unlink(missing_ok=True)

        self.stdout.write(f'Deleted {deleted} rows:')
        for label, count in sorted(per_model.items()):
            self.stdout.write(f'  {label:<34} {count}')
        self.stdout.write(self.style.SUCCESS(
            f'Removed: {", ".join(requested)}. '
            + ('Manifest updated.' if manifest.areas else 'Manifest consumed and removed.')))

    # ------------------------------------------------------------------ verdict
    def _verdict(self, area, instances, allowed_keys, manifest):
        reasons = []
        ledger_rows = sum(1 for row in manifest.entries(area) if row['model'] == LEDGER)
        if ledger_rows:
            reasons.append(f'{ledger_rows} StatusTransition (append-only history) row(s) '
                           f'belong to this area; removing them would need the forbidden '
                           f'bypass. Drop the database instead.')
        if not instances:
            return reasons

        collector = Collector(using='default')
        try:
            _collect(collector, instances)
        except (ProtectedError, RestrictedError) as exc:
            kind = 'PROTECT' if isinstance(exc, ProtectedError) else 'RESTRICT'
            blockers = (exc.protected_objects if isinstance(exc, ProtectedError)
                        else exc.restricted_objects)
            keys = {(_label(o), o.pk) for o in blockers}
            outside = sorted(k for k in keys if k not in allowed_keys)
            inside = len(keys) - len(outside)
            if outside:
                reasons.append(
                    f'{kind}: {len(outside)} row(s) outside the areas being removed still '
                    f'point at it, e.g. '
                    + ', '.join(f'{m} pk={pk}' for m, pk in outside[:3]))
            if inside:
                reasons.append(
                    f'{kind}: {inside} row(s) within the areas being removed point at '
                    f'its rows, and Django does not resolve a {kind} within one cascade')
            return reasons

        outside, ledger_reached = set(), 0
        for model, objs in collector.data.items():
            for obj in objs:
                key = (_label(obj), obj.pk)
                if key[0] == LEDGER:
                    ledger_reached += 1
                if key not in allowed_keys:
                    outside.add(key)
        for qs in collector.fast_deletes:
            for obj in qs:
                key = (_label(obj), obj.pk)
                if key[0] == LEDGER:
                    ledger_reached += 1
                if key not in allowed_keys:
                    outside.add(key)
        modified = set()
        for (_field, _value), batches in collector.field_updates.items():
            for batch in batches:
                for obj in batch:
                    key = (_label(obj), obj.pk)
                    if key[0] == LEDGER:
                        ledger_reached += 1
                    if key not in allowed_keys:
                        modified.add(key)

        if ledger_reached and not ledger_rows:
            reasons.append(f'the cascade would reach {ledger_reached} StatusTransition '
                           f'row(s). Drop the database instead.')
        if outside:
            sample = ', '.join(f'{m} pk={pk}' for m, pk in sorted(outside)[:3])
            reasons.append(f'the cascade would delete {len(outside)} row(s) the seed did '
                           f'not record for the areas being removed, e.g. {sample}')
        if modified:
            sample = ', '.join(f'{m} pk={pk}' for m, pk in sorted(modified)[:3])
            reasons.append(f'SET_NULL would modify {len(modified)} row(s) outside the '
                           f'areas being removed, e.g. {sample}')
        return reasons
