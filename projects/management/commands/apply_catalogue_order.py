"""
apply_catalogue_order — set the RESCO (OPEX) catalogue's positions from a committed data
file (DW-4, 27 Nov 2026).

    python manage.py apply_catalogue_order            # dry run (the default)
    python manage.py apply_catalogue_order --apply

THE FILE DECIDES, THIS COMMAND ONLY CARRIES IT OUT. projects/data/dw4_catalogue_order_27nov.csv
holds every RESCO item with its new `sort_order`: the master BOQ's sequence (each category
one contiguous run, nested categories moved to just after their host), numbered 10, 20,
30…, with Zuber's four placements (OPX-274, OPX-273, OPX-149, OPX-207). It was generated
from projects/data/b1_master_catalogue_23nov.csv and the live catalogue and reviewed by
Zuber before any --apply. Nothing here reorders anything by itself.

    code            the item (must be a live RESCO item)
    sort_order      its new position
    master_row      the master BOQ row it came from (blank: not in the master); for review
    category        for review only
    was_sort_order  its position when the file was made; an item whose live position has
                    moved since is REPORTED, never refused (Zuber, DW-4 answer 6)
    description     for review only

REFUSED BEFORE ANYTHING IS WRITTEN (all problems are listed, nothing stops at the first):
a wrong header, a code that is blank, repeated or not a live RESCO item, a position that is
not a whole number above 0 or is given to two items, and a live RESCO item the file leaves
out (answer 6: regenerate the file rather than guess where a new item goes).

WHAT IT CHANGES: BOQItemMaster.sort_order of RESCO items, and nothing else. Not BOQ lines
(serial_no is never rewritten, answer 8), not snapshots, not Residential items, not
wording, units, categories or the active flag.

Each changed item is logged old -> new through the catalogue screens' own helper
(forms.log_catalogue_change: "sort order: 8 → 90"), with no actor and the command named in
the text, plus one summary entry per apply, as import_master_catalogue (B-1) does. Running
--apply again changes nothing and logs nothing.
"""
import csv
import hashlib
import io
from dataclasses import dataclass, field
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from projects.forms import catalogue_snapshot, log_catalogue_change
from projects.models import BOQItemMaster, log_activity

DEFAULT_FILE = Path(settings.BASE_DIR) / 'projects' / 'data' / 'dw4_catalogue_order_27nov.csv'
COLUMNS = ('code', 'sort_order', 'master_row', 'category', 'was_sort_order', 'description')
PROJECT_TYPE = 'OPEX'
# CONSOLE OUTPUT USES "->", NOT "→": a piped Windows console writes cp1252, which has no
# "→" (the B-0b trap). The activity log is UTF-8 and keeps the catalogue screens' "→".
ACTOR = 'apply_catalogue_order (system)'
ACTION_CODE = 'boq_catalogue_item_moved'
SUMMARY_ACTION_CODE = 'boq_catalogue_order_applied'


@dataclass
class Change:
    item: BOQItemMaster
    old: int
    new: int


@dataclass
class Plan:
    changes: list = field(default_factory=list)
    in_place: int = 0
    moved: list = field(default_factory=list)       # (code, was in the file, live now, new)
    problems: list = field(default_factory=list)


def read_file(path):
    """(columns, rows, sha256). Read as bytes first so the checksum is of exactly the file
    applied; utf-8-sig drops a BOM if Excel added one."""
    raw = Path(path).read_bytes()
    reader = csv.DictReader(io.StringIO(raw.decode('utf-8-sig'), newline=''))
    rows = []
    for number, row in enumerate(reader, start=2):
        row['_line'] = number
        rows.append(row)
    return tuple(reader.fieldnames or ()), rows, hashlib.sha256(raw).hexdigest()


def _whole(text):
    """A whole number above 0, or None."""
    text = (text or '').strip()
    return int(text) if text.isdigit() and int(text) > 0 else None


def plan_order(columns, rows, lock=False):
    """Compare the file with the live RESCO catalogue. Writes nothing.

    `lock` (apply only, inside its transaction) reads the catalogue with SELECT ... FOR
    UPDATE, so no catalogue screen can save one of these rows between this plan and the
    write; a Head edit made before the lock is simply what the plan sees.
    """
    plan = Plan()
    if columns != COLUMNS:
        plan.problems.append(f'the header must be {",".join(COLUMNS)}; it is '
                             f'{",".join(columns) or "(empty)"}')
        return plan
    if not rows:
        plan.problems.append('the file has no rows')
        return plan

    # The whole RESCO catalogue, active and retired: every item has a position, and a
    # retired item's lines are still listed on existing BOQs in that position.
    live = BOQItemMaster.objects.filter(project_type=PROJECT_TYPE)
    if lock:
        live = live.select_for_update()
    live = {item.code: item for item in live}
    other_types = set(BOQItemMaster.objects.exclude(project_type=PROJECT_TYPE)
                      .values_list('code', flat=True))

    seen_codes, positions = {}, {}
    for row in rows:
        line, code = row['_line'], (row['code'] or '').strip()
        new = _whole(row['sort_order'])
        if not code:
            plan.problems.append(f'line {line}: no code')
            continue
        if code in seen_codes:
            plan.problems.append(f'{code}: on lines {seen_codes[code]} and {line}')
            continue
        seen_codes[code] = line
        if new is None:
            plan.problems.append(f'{code} (line {line}): sort_order {row["sort_order"]!r} is '
                                 f'not a whole number above 0')
            continue
        if new in positions:
            plan.problems.append(f'position {new} is given to {positions[new]} and {code}')
            continue
        positions[new] = code
        item = live.get(code)
        if item is None:
            where = 'a Residential item' if code in other_types else 'not in the catalogue'
            plan.problems.append(f'{code}: not a RESCO catalogue item ({where})')
            continue
        was = _whole(row['was_sort_order'])
        if was is not None and item.sort_order not in (was, new):
            plan.moved.append((code, was, item.sort_order, new))
        if item.sort_order == new:
            plan.in_place += 1
        else:
            plan.changes.append(Change(item=item, old=item.sort_order, new=new))

    for code in sorted(set(live) - set(seen_codes)):
        plan.problems.append(f'{code}: in the catalogue but not in the file (regenerate the '
                             f'file to place it)')
    plan.changes.sort(key=lambda change: change.new)
    return plan


class Command(BaseCommand):
    help = ('Set the RESCO catalogue positions from the DW-4 data file. Dry run unless '
            '--apply.')

    def add_arguments(self, parser):
        parser.add_argument('--file', default=str(DEFAULT_FILE),
                            help='the order file (default: %(default)s)')
        parser.add_argument('--apply', action='store_true',
                            help='write the positions, in one transaction')

    def _say(self, text=''):
        self.stdout.write(text)

    def handle(self, *args, **options):
        path = Path(options['file'])
        if not path.is_file():
            raise CommandError(f'No such file: {path}')
        columns, rows, sha = read_file(path)
        apply = options['apply']

        mode = 'APPLY' if apply else 'DRY RUN (nothing written; use --apply to write)'
        self._say(f'apply_catalogue_order - {mode}')
        self._say(f'file: {path}')
        self._say(f'sha256: {sha}   rows: {len(rows)}')

        plan = plan_order(columns, rows)
        written = None
        if apply and not plan.problems:
            # Planned again under the row locks, then written, in one transaction. The
            # first plan is what was checked; this one is what is written, and it is
            # refused the same way if anything moved in between.
            with transaction.atomic():
                plan = plan_order(columns, rows, lock=True)
                if not plan.problems:
                    written = self._write(plan, path, sha)

        self._print(plan, apply, written)
        if apply and written is None:
            raise CommandError(f'Refused: {len(plan.problems)} problem(s) above. Nothing was '
                               f'written.')

    def _write(self, plan, path, sha):
        """Set each changed position and log it. Runs inside handle()'s transaction with
        every RESCO row locked by the plan (select_for_update), so a plain save() cannot
        race a catalogue screen: the screen's save waits for this to commit. Returns the
        number of items changed."""
        for change in plan.changes:
            item = change.item
            before = catalogue_snapshot(item)
            item.sort_order = change.new
            item.save(update_fields=['sort_order', 'updated_at'])
            log_catalogue_change(None, item,
                                 f"{ACTOR} moved RESCO BOQ catalogue item '{item.code}'",
                                 before, action_code=ACTION_CODE)
        if plan.changes:
            moved = f', {len(plan.moved)} moved since the file was made' if plan.moved else ''
            log_activity(None, None,
                         f'{ACTOR} applied {path.name} (sha256 {sha[:12]}): '
                         f'{len(plan.changes)} positions changed, {plan.in_place} already in '
                         f'place{moved}',
                         entity_type='BOQItemMaster', action_code=SUMMARY_ACTION_CODE)
        return len(plan.changes)

    def _print(self, plan, apply, written):
        if plan.problems:
            self._say('')
            self._say(f'PROBLEMS ({len(plan.problems)}) - '
                      + ('nothing was written:' if apply else '--apply would be refused:'))
            for problem in plan.problems:
                self._say(f'  {problem}')
            return
        if plan.moved:
            self._say('')
            self._say(f'MOVED SINCE THE FILE WAS MADE ({len(plan.moved)}) - reported, still '
                      f'set to the file\'s position:')
            for code, was, now, new in plan.moved:
                self._say(f'  {code}: was {was} in the file, is {now} now, will be {new}')
        self._say('')
        if not plan.changes:
            self._say(f'Nothing to change: all {plan.in_place} items are already in the '
                      f'file\'s positions.')
            return
        verb = 'CHANGED' if written is not None else 'TO CHANGE'
        self._say(f'{verb} ({len(plan.changes)}), {plan.in_place} already in place:')
        for change in plan.changes:
            item = change.item
            self._say(f'  {item.code}  {change.old} -> {change.new}  {item.category}  '
                      f'{item.description[:60]}')
        if written is not None:
            self._say('')
            self._say(f'Applied: {written} positions changed, each logged, plus one summary '
                      f'entry.')
