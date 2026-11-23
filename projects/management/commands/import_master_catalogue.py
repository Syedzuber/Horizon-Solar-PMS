"""
import_master_catalogue — apply Praveen's confirmed master BOQ to the RESCO (OPEX)
catalogue (B-1, 23 Nov 2026).

    python manage.py import_master_catalogue                        # dry run (the default)
    python manage.py import_master_catalogue --report-csv inuse.csv # dry run + in-use CSV
    python manage.py import_master_catalogue --apply
    python manage.py import_master_catalogue --apply \
        --allow-codes OPX-012,OPX-040 --reason "edited on the screen on 24 Nov, Praveen agreed"

THE FILE DECIDES, THIS COMMAND ONLY CARRIES IT OUT. projects/data/b1_master_catalogue_23nov.csv
was generated from Praveen's confirmed mapping workbook with every decision applied: one
row, one action. Nothing here reinterprets a row. A row that does not fit the live
catalogue is listed as a mismatch for a person to decide, never corrected.

    CHECK       stays as it is (live wording and unit must match the file)
    REWORD      description becomes the file's `description`; unit unchanged
    CREATE      new item with the file's code, description and unit
    REACTIVATE  retired today, made active
    RETIRE      hidden from new BOQs (is_active off); never deleted
    KEEP        catalogue item not in the master: no change, must exist
    SKIP        not imported (duplicates, the NA row, ACCB Type-1)

WHAT IT NEVER CHANGES:
- BOQ lines (BOQItem). A line keeps the text and unit it was created with; rewording an
  item changes only what NEW lines copy. The in-use report lists the old lines so the team
  can decide them by hand, site by site.
- The unit of an existing item (ruling 12). A row whose unit differs from the live unit
  is a mismatch, not a change.
- OPX-273 (the ACCB configurator's item), Residential items, formula rules, ACCB
  configurations, orders, approvals.

CODES COME FROM THE FILE, never "highest + 1": OPX-273 already exists above the new codes.
A new item's sort_order is its code number (Zuber, B-1 Q4), so OPX-218 sorts at 218 — the
same rule migration 0057 used for OPX-001..207.

Every change is logged old -> new through the catalogue screens' own helper
(forms.log_catalogue_change), with no actor and the command named in the text, plus one
summary entry per apply. Running --apply twice changes nothing the second time.
"""
import csv
import hashlib
import io
import re
from dataclasses import dataclass, field
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from projects.accb import ACCB_CONFIGURED_CODE
from projects.forms import catalogue_snapshot, log_catalogue_change
from projects.models import (
    DESIGN_RELEASED, BOQFormulaDriver, BOQFormulaRule, BOQItem, BOQItemGroupMember,
    BOQItemMaster, VendorOrderLine, log_activity,
)
from projects.units import UNIT_CODES, canonical_unit

DEFAULT_FILE = Path(settings.BASE_DIR) / 'projects' / 'data' / 'b1_master_catalogue_23nov.csv'

#: Rows in the confirmed file. --expected-rows exists only so the tests can feed a small
#: fixture through the same validation; production runs never pass it.
EXPECTED_ROWS = 275

COLUMNS = ('action', 'code', 'master_sr', 'master_header', 'description', 'unit',
           'expected_description', 'expected_unit', 'auto_qty', 'reason')

CHECK, REWORD, CREATE, REACTIVATE, RETIRE, KEEP, SKIP = (
    'CHECK', 'REWORD', 'CREATE', 'REACTIVATE', 'RETIRE', 'KEEP', 'SKIP')
ACTIONS = (CHECK, REWORD, CREATE, REACTIVATE, RETIRE, KEEP, SKIP)
#: Rows that must carry the item's description and unit in the file.
ACTIONS_WITH_TEXT = (CHECK, REWORD, CREATE, REACTIVATE)

#: The master's section header -> the catalogue category a NEW item gets (Zuber, B-1 P7
#: answers). Each is an existing RESCO category; the plan refuses one that is not, so no
#: category is ever invented here.
HEADER_CATEGORIES = {
    'ACDB':             'ACDB',
    'Al Cable 4C':      'AC Cable',
    'Al Lug':           'Ring Type Lug',
    'Conduit':          'Conduit',
    'Inverter':         'Inverter',
    'Inverter Stand':   'BOS',
    'MMS':              'MMS',
    'Roof access':      'BOS',
    'Solar Meter + CT': 'Solar Meter + CT',
}

# CONSOLE OUTPUT USES "->", NOT "→": a piped Windows console writes cp1252, which has no
# "→" (the B-0b trap). The activity log is UTF-8 and keeps B-0's "→".
ACTOR = 'import_master_catalogue (system)'
ACTION_CODES = {
    CREATE:     'boq_catalogue_item_imported',
    REWORD:     'boq_catalogue_item_reworded',
    REACTIVATE: 'boq_catalogue_item_reactivated',
    RETIRE:     'boq_catalogue_item_retired',
}
SUMMARY_ACTION_CODE = 'boq_catalogue_import_applied'
VERBS = {CREATE: 'created', REWORD: 'reworded', REACTIVATE: 'reactivated', RETIRE: 'retired'}

_CODE_RE = re.compile(r'\AOPX-(\d{3})\Z')

# A line copies description, category and unit from the catalogue, so a value must fit
# BOTH columns. BOQItem.description is a TextField, so only the catalogue limits it.
DESCRIPTION_MAX = BOQItemMaster._meta.get_field('description').max_length
CATEGORY_MAX = min(BOQItemMaster._meta.get_field('category').max_length,
                   BOQItem._meta.get_field('category').max_length)
UNIT_MAX = min(BOQItemMaster._meta.get_field('unit').max_length,
               BOQItem._meta.get_field('uom').max_length)

REPORT_COLUMNS = (
    'item_code', 'action', 'catalogue_wording_before', 'catalogue_wording_after',
    'line_id', 'site_code', 'site_deleted', 'design_status', 'released', 'line_quantity',
    'line_unit', 'ordered_quantity', 'order_ids', 'ordered', 'line_description',
)

TO_DO, DONE, NO_CHANGE, SKIPPED = 'to do', 'done', 'no change', 'skipped'


@dataclass
class Problem:
    """One mismatch. `code` is '' for a problem with the file itself.

    ALLOWABLE problems are live-data drift a person can look at and accept with
    --allow-codes and --reason (a wording edited on the screen, an item added since 17 Nov).
    The rest can never be allowed, because carrying on would break a ruling or leave the
    action undefined: a malformed file, a unit change (ruling 12), a mandatory or formula
    item retired, OPX-273 named, a CREATE whose code is already taken by something else.
    """
    code: str
    text: str
    allowable: bool = False


@dataclass
class Step:
    """What one file row does to the live catalogue."""
    row: dict
    action: str
    code: str
    state: str                      # TO_DO, DONE, NO_CHANGE or SKIPPED
    item: BOQItemMaster = None      # the live item (None for a CREATE still to do)
    category: str = ''              # CREATE only

    @property
    def sort_order(self):
        return int(_CODE_RE.fullmatch(self.code).group(1))


@dataclass
class Plan:
    steps: list = field(default_factory=list)
    problems: list = field(default_factory=list)

    def to_do(self):
        return [s for s in self.steps if s.state == TO_DO]

    def blocking(self, allowed):
        """Problems that stop an apply: every one not both allowable and named."""
        return [p for p in self.problems if not (p.allowable and p.code in allowed)]


def read_file(path):
    """(columns, rows, sha256) of the CSV. Rows carry their spreadsheet line in '_line'.

    Read as bytes first so the checksum is of exactly the file applied. utf-8-sig drops a
    BOM if Excel added one; every other byte is kept, including surrounding spaces, because
    the descriptions are compared with the live catalogue character for character.
    """
    raw = Path(path).read_bytes()
    reader = csv.DictReader(io.StringIO(raw.decode('utf-8-sig'), newline=''))
    rows = []
    for number, row in enumerate(reader, start=2):
        row['_line'] = number
        rows.append(row)
    return tuple(reader.fieldnames or ()), rows, hashlib.sha256(raw).hexdigest()


def _formula_item_ids():
    """pks of catalogue items a formula reads or writes: active rules' targets, drivers of
    active rules, and members of any group (a group is read by whichever rule names it,
    and membership ignores is_active, B-2a). Three queries, whatever the catalogue size."""
    ids = set(BOQFormulaRule.objects.filter(is_active=True).values_list('target_id', flat=True))
    ids |= set(BOQFormulaDriver.objects.filter(rule__is_active=True, item__isnull=False)
               .values_list('item_id', flat=True))
    ids |= set(BOQItemGroupMember.objects.values_list('item_id', flat=True))
    return ids


def plan_import(columns, rows, expected_rows=EXPECTED_ROWS, lock=False):
    """THE ONE PLANNING FUNCTION: the dry run and the apply both call it, so the dry run
    cannot promise something the apply would do differently. Reads, never writes.

    Every problem is collected; nothing stops at the first. Order: the file's shape, the
    file's values, then the file against the live catalogue.

    `lock` (apply only, inside its transaction) reads the catalogue with SELECT ... FOR
    UPDATE, so a catalogue screen cannot change a row between this plan and the writes.
    """
    plan = Plan()

    def problem(code, text, allowable=False):
        plan.problems.append(Problem(code, text, allowable))

    # ── 1. the file's shape ───────────────────────────────────────────────────
    if columns != COLUMNS:
        problem('', f'the file columns are {", ".join(columns) or "(none)"}; expected '
                    f'{", ".join(COLUMNS)}')
        return plan          # the rows cannot be read against the wrong columns
    if len(rows) != expected_rows:
        problem('', f'the file has {len(rows)} rows; expected {expected_rows}')

    usable = []
    for row in rows:
        where = f"line {row['_line']}"
        # csv puts surplus cells under the key None and fills missing cells with None.
        if None in row or any(value is None for value in row.values()):
            problem('', f'{where}: wrong number of cells')
            continue
        if row['action'] not in ACTIONS:
            problem(row['code'], f"{where}: unknown action {row['action']!r}")
            continue
        usable.append(row)

    # SKIP rows may repeat a code (the two duplicates point at OPX-086) or carry none
    # (the NA row); every other code appears once.
    seen = {}
    for row in usable:
        if row['action'] == SKIP:
            continue
        if row['code'] in seen:
            problem(row['code'], f"line {row['_line']}: {row['code']} already on line "
                                 f"{seen[row['code']]}")
        else:
            seen[row['code']] = row['_line']

    # ── 2. the file's values ──────────────────────────────────────────────────
    checked = []
    for row in usable:
        action, code, where = row['action'], row['code'], f"line {row['_line']}"
        if action == SKIP:
            plan.steps.append(Step(row, action, code, SKIPPED))
            continue
        bad = len(plan.problems)
        if not _CODE_RE.fullmatch(code):
            problem(code, f'{where}: code {code!r} is not of the form OPX-000')
        if code == ACCB_CONFIGURED_CODE:
            problem(code, f'{where}: {ACCB_CONFIGURED_CODE} belongs to the ACCB '
                          f'configurator and is never imported')
        if action in ACTIONS_WITH_TEXT and not (row['description'] and row['unit']):
            problem(code, f'{where}: {action} needs a description and a unit')
        if row['unit'] and row['unit'] not in UNIT_CODES:
            problem(code, f"{where}: unit {row['unit']!r} is not on the unit list "
                          f"({', '.join(UNIT_CODES)})")
        elif len(row['unit']) > UNIT_MAX:
            problem(code, f"{where}: unit {row['unit']!r} is over {UNIT_MAX} characters")
        if len(row['description']) > DESCRIPTION_MAX:
            problem(code, f"{where}: description is {len(row['description'])} characters; "
                          f"the most a catalogue item holds is {DESCRIPTION_MAX}")
        # Zuber ruled every ACCB comes from the configurator: no row may bring one in.
        if 'ACCB' in row['description'].upper():
            problem(code, f'{where}: an ACCB row must be SKIP (every ACCB comes from the '
                          f'configurator)')
        if action in (CHECK, REACTIVATE) and row['description'] != row['expected_description']:
            problem(code, f'{where}: {action} says the item stays as it is, but description '
                          f'and expected_description differ')
        if action == REWORD and row['description'] == row['expected_description']:
            problem(code, f'{where}: REWORD with the same wording as today')
        category = ''
        if action == CREATE:
            category = HEADER_CATEGORIES.get(row['master_header'], '')
            if not category:
                problem(code, f"{where}: no category agreed for the master header "
                              f"{row['master_header']!r}")
            elif len(category) > CATEGORY_MAX:
                problem(code, f'{where}: category {category!r} is over {CATEGORY_MAX} '
                              f'characters')
        if len(plan.problems) == bad:
            checked.append((row, category))

    # ── 3. the file against the live catalogue ────────────────────────────────
    # Every type, not only OPEX: a CREATE code must be free across the whole table (code
    # is unique), and a Residential code in the file must be refused, not looked past.
    live_qs = BOQItemMaster.objects.order_by('code')
    if lock:
        live_qs = live_qs.select_for_update()
    live = {item.code: item for item in live_qs}
    opex_categories = {item.category for item in live.values() if item.project_type == 'OPEX'}
    formula_ids = _formula_item_ids()

    for row, category in checked:
        action, code = row['action'], row['code']
        item = live.get(code)

        if action == CREATE:
            if category not in opex_categories:
                problem(code, f'CREATE: category {category!r} is not a live RESCO category')
                continue
            if item is None:
                plan.steps.append(Step(row, action, code, TO_DO, category=category))
            elif (item.project_type == 'OPEX' and item.is_active and not item.is_mandatory
                    and item.description == row['description'] and item.unit == row['unit']
                    and item.category == category):
                # A CREATE already applied, by an earlier run: idempotent.
                plan.steps.append(Step(row, action, code, DONE, item, category))
            else:
                problem(code, f"CREATE: {code} already exists with other values "
                              f"({_describe(item)}); the file wants "
                              f"'{row['description']}' | {row['unit']} | {category}")
            continue

        if item is None or item.project_type != 'OPEX':
            problem(code, f'{action}: {code} is not in the RESCO catalogue')
            continue

        # Wording. REWORD counts as done once the live text is the file's new text.
        state = NO_CHANGE
        if action == REWORD:
            if item.description == row['description']:
                state = DONE
            else:
                state = TO_DO
                if item.description != row['expected_description']:
                    problem(code, f"REWORD: live wording '{item.description}' is not the "
                                  f"expected '{row['expected_description']}'", allowable=True)
        elif item.description != row['expected_description']:
            problem(code, f"{action}: live wording '{item.description}' is not the "
                          f"expected '{row['expected_description']}'", allowable=True)

        # Unit (ruling 12, Zuber B-1 Q1). A row that states a unit passes when the live
        # unit IS that unit, spelling variants included ('No' is Nos), whatever the 17 Nov
        # export said. Anything else would need the unit changed, which this command never
        # does, so it cannot be allowed. A row with no unit (KEEP, RETIRE) is checked
        # against the 17 Nov unit; drift there changes nothing here, so it is allowable.
        if row['unit']:
            if canonical_unit(item.unit) != canonical_unit(row['unit']):
                problem(code, f"{action}: live unit {item.unit!r}, file unit "
                              f"{row['unit']!r}; this import never changes a unit")
        elif canonical_unit(item.unit) != canonical_unit(row['expected_unit']):
            problem(code, f"{action}: live unit {item.unit!r} is not the expected "
                          f"{row['expected_unit']!r}", allowable=True)

        # Active state. REACTIVATE and RETIRE are done once the item is in their state.
        if action == REACTIVATE:
            state = DONE if item.is_active else TO_DO
        elif action == RETIRE:
            state = DONE if not item.is_active else TO_DO
            # Same rule as the catalogue screens' toggle: a mandatory item cannot be
            # retired (the picker would never offer an item every BOQ must carry).
            if item.is_mandatory:
                problem(code, 'RETIRE: the item is mandatory; clear the flag on the '
                              'catalogue screen first')
            # A retired target stops being calculated, and a retired driver or member
            # keeps counting while no longer offered: both are formula decisions.
            if item.pk in formula_ids:
                problem(code, 'RETIRE: the item is used by a formula rule (target, driver '
                              'or group member)')
        elif not item.is_active:
            problem(code, f'{action}: the item is retired today', allowable=True)

        plan.steps.append(Step(row, action, code, state, item))

    # Every live RESCO item must be in the file once, except the configurator's item.
    named = {row['code'] for row in usable if row['action'] != SKIP}
    for code, item in live.items():
        if item.project_type != 'OPEX' or code == ACCB_CONFIGURED_CODE:
            continue
        if code not in named:
            problem(code, f"live RESCO item {code} ({_describe(item)}) is not in the file",
                    allowable=True)
        if 'ACCB' in item.description.upper():
            problem(code, f'live item {code} mentions ACCB; every ACCB comes from the '
                          f'configurator ({ACCB_CONFIGURED_CODE})', allowable=True)
    return plan


def _describe(item):
    state = 'active' if item.is_active else 'retired'
    return (f"{item.project_type} '{item.description}' | {item.unit} | {item.category} | "
            f"{state}")


def _assignment(project):
    """The site's DesignAssignment, or None. A missing reverse one-to-one raises rather
    than returning None, even through select_related."""
    try:
        return project.design_assignment
    except ObjectDoesNotExist:
        return None


def in_use_rows(plan):
    """T2 — every BOQ line on a reworded or retired item, for the team to decide by hand.

    DELETED SITES INCLUDED, as B-0 counts "in use": a restored site brings its lines back.
    Three queries in all: the lines (with site and design assignment), order lines that
    name a line, and order lines on the item for an order that names the line's site (a
    tender group order links the item and the sites, not the BOQ line).

    Old and new wording come from the FILE (expected_description and description), so the
    report reads the same before and after the apply.
    """
    steps = {s.code: s for s in plan.steps if s.action in (REWORD, RETIRE)}
    if not steps:
        return []
    lines = list(BOQItem.objects
                 .filter(item_master__code__in=steps)
                 .select_related('item_master', 'boq__project__design_assignment')
                 .order_by('item_master__code', 'boq__project__project_id', 'pk'))
    by_line, by_site_item = {}, {}
    for link in VendorOrderLine.objects.filter(boq_item__in=lines).values('boq_item', 'order'):
        by_line.setdefault(link['boq_item'], set()).add(link['order'])
    for link in (VendorOrderLine.objects
                 .filter(item_master__code__in=steps, order__sites__project__isnull=False)
                 .values('item_master', 'order__sites__project', 'order')):
        key = (link['item_master'], link['order__sites__project'])
        by_site_item.setdefault(key, set()).add(link['order'])

    report = []
    for line in lines:
        step = steps[line.item_master.code]
        project = line.boq.project
        assignment = _assignment(project)
        orders = sorted(by_line.get(line.pk, set())
                        | by_site_item.get((line.item_master_id, project.pk), set()))
        ordered = bool(orders) or line.ordered_quantity is not None
        report.append({
            'item_code':                line.item_master.code,
            'action':                   step.action,
            'catalogue_wording_before': step.row['expected_description'],
            'catalogue_wording_after':  (step.row['description'] if step.action == REWORD
                                         else '(retired)'),
            'line_id':                  line.pk,
            'site_code':                project.project_id,
            'site_deleted':             'yes' if project.is_deleted else 'no',
            'design_status':            assignment.status if assignment else '',
            'released':                 ('yes' if assignment and
                                         assignment.status == DESIGN_RELEASED else 'no'),
            'line_quantity':            '' if line.boq_quantity is None else line.boq_quantity,
            'line_unit':                line.uom,
            'ordered_quantity':         ('' if line.ordered_quantity is None
                                         else line.ordered_quantity),
            'order_ids':                ' '.join(f'#{pk}' for pk in orders),
            'ordered':                  'yes' if ordered else 'no',
            'line_description':         line.description,
        })
    return report


class Command(BaseCommand):
    help = ('Apply the confirmed master BOQ file to the RESCO catalogue. Dry run unless '
            '--apply. Never touches BOQ lines or units.')

    def add_arguments(self, parser):
        parser.add_argument('--file', default=str(DEFAULT_FILE),
                            help='The master catalogue CSV (default: the agreed file).')
        parser.add_argument('--apply', action='store_true',
                            help='Write the changes. Without it, only print them.')
        parser.add_argument('--report-csv', metavar='PATH',
                            help='Also write the in-use report to this CSV (dry run or apply).')
        parser.add_argument('--allow-codes', default='',
                            help='Comma-separated codes whose allowable mismatches may pass. '
                                 'Needs --reason.')
        parser.add_argument('--reason', default='',
                            help='Why the --allow-codes mismatches are accepted; logged.')
        parser.add_argument('--expected-rows', type=int, default=EXPECTED_ROWS,
                            help=f'Rows the file must have (default {EXPECTED_ROWS}).')

    # ── output ───────────────────────────────────────────────────────────────

    def _say(self, text=''):
        """Write one line the console can print: a character its encoding lacks becomes
        '?' rather than stopping the run (cp1252 on a piped Windows console)."""
        encoding = getattr(self.stdout._out, 'encoding', None) or 'utf-8'
        self.stdout.write(text.encode(encoding, 'replace').decode(encoding))

    def handle(self, *args, **options):
        allowed = {c.strip().upper() for c in options['allow_codes'].split(',') if c.strip()}
        reason = options['reason'].strip()
        if allowed and not reason:
            raise CommandError('--allow-codes needs --reason: say why the mismatches are '
                               'accepted; the reason is logged.')
        if reason and not allowed:
            raise CommandError('--reason is only used with --allow-codes.')

        path = Path(options['file'])
        if not path.is_file():
            raise CommandError(f'No such file: {path}')
        columns, rows, sha = read_file(path)
        apply = options['apply']

        mode = 'APPLY' if apply else 'DRY RUN (nothing written; use --apply to write)'
        self._say(f'import_master_catalogue - {mode}')
        self._say(f'file: {path}')
        self._say(f'sha256: {sha}   rows: {len(rows)}')

        plan = plan_import(columns, rows, options['expected_rows'])
        self._check_allowed(plan, allowed)

        written = None
        if apply and not plan.blocking(allowed):
            # Planned again under the row locks, then written, in one transaction. The
            # first plan above is what was checked; this one is what is written, and it is
            # refused the same way if anything moved in between.
            with transaction.atomic():
                plan = plan_import(columns, rows, options['expected_rows'], lock=True)
                if not plan.blocking(allowed):
                    written = self._write(plan, path, sha, allowed, reason)

        self._print(plan, allowed, applied=written is not None, written=written)
        self._report(plan, options['report_csv'])
        if apply and written is None:
            raise CommandError(f'Refused: {len(plan.blocking(allowed))} mismatch(es) above. '
                               f'Nothing was written.')

    def _check_allowed(self, plan, allowed):
        """An allowed code must name an allowable mismatch. A code with none (a typo, or a
        mismatch already fixed) refuses the run, so an allowance never outlives its
        reason silently."""
        allowable = {p.code for p in plan.problems if p.allowable}
        stray = sorted(allowed - allowable)
        if stray:
            raise CommandError('--allow-codes names code(s) with no allowable mismatch: '
                               + ', '.join(stray))

    # ── writing ──────────────────────────────────────────────────────────────

    def _write(self, plan, path, sha, allowed, reason):
        """Carry out every TO_DO step and log it. Runs inside handle()'s transaction, with
        every catalogue row locked by the plan (select_for_update), so plain save() is safe:
        no catalogue screen can save one of these rows until this commits. A CREATE racing
        a screen that adds the same code fails on the unique code and rolls everything back.

        Each item's log entry comes from forms.log_catalogue_change, the catalogue screens'
        own helper, so the entry reads like theirs: "field: old → new". A CREATE has no
        "before", so it lists every field as "→ value". Returns the count per action.
        """
        written = {action: 0 for action in VERBS}
        for step in plan.to_do():
            action = step.action
            if action == CREATE:
                item = BOQItemMaster(
                    code=step.code, description=step.row['description'],
                    unit=step.row['unit'], category=step.category, project_type='OPEX',
                    is_active=True, is_mandatory=False, sort_order=step.sort_order)
                item.save()
                before = None
            else:
                item = step.item
                before = catalogue_snapshot(item)
                if action == REWORD:
                    item.description = step.row['description']
                    item.save(update_fields=['description', 'updated_at'])
                else:
                    item.is_active = action == REACTIVATE
                    item.save(update_fields=['is_active', 'updated_at'])
            step.item = item
            log_catalogue_change(None, item,
                                 f"{ACTOR} {VERBS[action]} RESCO BOQ catalogue item "
                                 f"'{step.code}'",
                                 before, action_code=ACTION_CODES[action])
            written[action] += 1

        if any(written.values()):
            counts = ', '.join(f'{n} {VERBS[a]}' for a, n in written.items())
            log_activity(None, None,
                         f'{ACTOR} applied {path.name} (sha256 {sha[:12]}): {counts}',
                         entity_type='BOQItemMaster', action_code=SUMMARY_ACTION_CODE)
            if allowed:
                log_activity(None, None,
                             f"{ACTOR} allowed mismatches on {', '.join(sorted(allowed))}; "
                             f"reason: {reason}",
                             entity_type='BOQItemMaster', action_code=SUMMARY_ACTION_CODE)
        return written

    # ── printing ─────────────────────────────────────────────────────────────

    def _print(self, plan, allowed, applied, written=None):
        say = self._say
        say()
        say('SUMMARY')
        for action in ACTIONS:
            steps = [s for s in plan.steps if s.action == action]
            to_do = sum(1 for s in steps if s.state == TO_DO)
            done = sum(1 for s in steps if s.state == DONE)
            if action in VERBS:
                say(f'  {action:<11}{len(steps):>4} rows   to do {to_do:>3}   already done {done:>3}')
            else:
                say(f'  {action:<11}{len(steps):>4} rows   (no change)')

        say()
        to_do = plan.to_do()
        if applied:
            if not any(written.values()):
                say('Nothing to do: every row is already applied. Nothing was written.')
            else:
                say('WRITTEN: ' + ', '.join(f'{n} {VERBS[a]}' for a, n in written.items()))
        elif not plan.blocking(allowed) and not to_do:
            say('Nothing to do: every row is already applied.')

        if to_do:
            say('CHANGES' + ('' if applied else ' (would be made)'))
            for step in to_do:
                say(self._change_line(step))

        say()
        say('MISMATCHES')
        if not plan.problems:
            say('  none')
        for p in plan.problems:
            if p.allowable and p.code in allowed:
                tag = '[allowed]'
            elif p.allowable:
                tag = '[can be allowed]'
            else:
                tag = '[cannot be allowed]'
            say(f"  {p.code or '(file)':<9} {tag:<19} {p.text}")

    @staticmethod
    def _change_line(step):
        action, code = step.action, step.code
        if action == CREATE:
            return (f"  {code}  CREATE      -> '{step.row['description']}' | "
                    f"{step.row['unit']} | {step.category} | sort {step.sort_order}")
        if action == REWORD:
            return (f"  {code}  REWORD      '{step.row['expected_description']}' "
                    f"-> '{step.row['description']}'")
        if action == REACTIVATE:
            return f'  {code}  REACTIVATE  active: no -> yes'
        return f'  {code}  RETIRE      active: yes -> no'

    def _report(self, plan, csv_path):
        """T2: the in-use report on the console, and in a CSV when asked. Read-only."""
        say = self._say
        report = in_use_rows(plan)
        say()
        say('IN USE: reworded and retired items on BOQ lines (no line is changed)')
        by_code = {}
        for entry in report:
            by_code.setdefault(entry['item_code'], []).append(entry)
        unused = [s.code for s in plan.steps
                  if s.action in (REWORD, RETIRE) and s.code not in by_code]
        if not report:
            say('  none')
        for code, entries in by_code.items():
            say(f"  {code} {entries[0]['action']}: {len(entries)} line(s)")
            for e in entries:
                say(f"    line {e['line_id']} | {e['site_code']}"
                    f"{' (deleted site)' if e['site_deleted'] == 'yes' else ''}"
                    f" | design {e['design_status'] or '-'} | released {e['released']}"
                    f" | qty {e['line_quantity'] or '-'} {e['line_unit']}"
                    f" | ordered {e['ordered']}"
                    f"{(' ' + e['order_ids']) if e['order_ids'] else ''}"
                    f"{(' (ordered qty ' + str(e['ordered_quantity']) + ')') if e['ordered_quantity'] != '' else ''}")
        if unused:
            say('  not on any line: ' + ', '.join(unused))
        if csv_path:
            # utf-8-sig so Excel opens the descriptions as UTF-8 when the team sorts it.
            with open(csv_path, 'w', encoding='utf-8-sig', newline='') as handle:
                writer = csv.DictWriter(handle, fieldnames=REPORT_COLUMNS)
                writer.writeheader()
                writer.writerows(report)
            say(f'  {len(report)} row(s) written to {csv_path}')
