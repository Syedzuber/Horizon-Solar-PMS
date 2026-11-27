"""
load_formula_rules — make the BOQ formula rules match the rules file (B-2c, 24 Nov 2026).

    python manage.py load_formula_rules                       # dry run (the default)
    python manage.py load_formula_rules --apply
    python manage.py load_formula_rules --deactivate R15      # dry run of a switch-off
    python manage.py load_formula_rules --deactivate R15 --apply

THE FILE IS THE SOURCE OF TRUTH (Zuber, B-2c Q6). projects/data/dw6_formula_rules_27nov.json
(DW-6: the DW-5 file plus INC2P, the design team's INC 2P rule for OPX-203; DW-5 was the
B-2c file of 24 Nov with R12 reading the "all modules" group) holds Praveen's Master BOQ
Excel formulas, one entry per rule with a stable key (R1 ... R25, a letter for a rule with
several targets: R7a/R7b; since DW-6 also a plain name such as INC2P, which cannot be read
as R12, the earthing rule). --apply makes the active rules match it:

    create      no rule on the target: a new rule
    same        the active rule already says what the file says: nothing written
    replace     the active rule differs: it is retired and the file's rule takes its place
    reactivate  no active rule, but a retired one says exactly what the file says (for
                example one switched off with --deactivate): it is switched back on, so
                its recalculation history stays on one rule row
    switch off  the file says "active": false and the target has an active rule
    off         the file says "active": false and nothing is active: nothing written

A second --apply writes nothing. Rules the file does not name are listed and left alone.

AN "OPEN" GROUP (DW-5, "open": true) lists its members as a MINIMUM: the loader adds any it
lacks and never removes one. "all modules" is open because the Head's catalogue form adds
each new module item to it (sync_module_group_membership); a closed group would lose that
item on the next --apply. Every other group is closed: the file's list is the whole list.
REACTIVATE lines are printed in their own section, above the rest (Q6), because they undo
a switch-off someone made on purpose.

DW-6 RULE KEYS, all optional, each stored in the parsed rules only when it is not the
default, so a file without them parses (and checksums) exactly as before:
    "site_factor": "module_wp"            multiply by the site's module wattage
    "clamp_at_zero": true                 below 0 is written as 0 with a warning
    "warning_check": "inc_2p_structures"  the named check (needs cross-check lines)
    driver "role": "check"                a cross-check line: read by the check, never summed;
                                          its weight is the modules one structure holds

THE KEY IS NOT STORED IN THE DATABASE. A rule row has a target, not a key; each target
belongs to exactly one key in the file, so the key is found through the file (this command,
--deactivate and formula_trial_report all do it that way). No migration was needed for it.

REFUSED BEFORE ANYTHING IS WRITTEN (C2): a code that does not exist, a non-RESCO code, a
retired or mandatory target, the configured-ACCB item (OPX-273) as a target, a unit that is
not the catalogue's, a repeated key or target, a rule loop, and B-2b's DEMO rules still on
the database. The dry run lists the refusals too and exits with an error.

--deactivate KEY retires that key's active rule and does nothing else. Its BOQ lines keep
their quantities and become ordinary lines, because a line is calculated only while an
active rule targets its item. AN OVERRIDE ON SUCH A LINE STAYS, UNUSED (Zuber, B-2c Q5):
while the rule is off nothing reads it; when the rule is switched back on it is used again,
with its old note, at whatever figure the line holds by then. The dry run counts them.
Because the file is the source of truth, the next --apply switches the rule back on unless
the file says "active": false.

SAVED ROW BY ROW through save(), never bulk_create: every save of a rule, driver or group
member re-checks the whole stored rule graph (B-2a), and bulk_create would skip that. The
whole apply is one transaction, so a refusal at any row leaves nothing written.

THE CHECKSUM IS OF THE PARSED RULES, not the raw bytes: the same rules give the same
checksum whether the file has Windows (CRLF) or Unix (LF) line endings, so the number on a
Windows dry run matches the one printed on production. It is printed and logged.
"""
import hashlib
import json
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from projects.accb import ACCB_CONFIGURED_CODE
from projects.boq_formulas import (
    DRIVER_ROLE_CHECK, DRIVER_ROLE_SUM, ROUND_NONE, ROUND_UP, SITE_FACTOR_CHOICES,
    SITE_FACTOR_MODULE_WP, WARNING_CHECK_CHOICES, DriverSpec, FormulaError, RuleSpec,
    build_rule_set,
)
from projects.models import (
    BOQFormulaDriver, BOQFormulaRule, BOQItem, BOQItemGroup, BOQItemGroupMember,
    BOQItemMaster, BOQLineOverride, log_activity,
)

from ._demo_support import print_db_banner
from .seed_formula_demo import DEMO_GROUP_PREFIX, DEMO_SUMMARY_PREFIX

# DW-6 (I7): the DW-6 file. The B-2c and DW-5 files stay for the record; loading the DW-5
# one now would switch nothing off (the loader never touches a rule the file does not name)
# but would no longer be the source of truth.
DEFAULT_FILE = Path(settings.BASE_DIR) / 'projects' / 'data' / 'dw6_formula_rules_27nov.json'

ACTOR = 'load_formula_rules (system)'
LOADED_ACTION_CODE = 'boq_formula_rules_loaded'
SWITCHED_OFF_ACTION_CODE = 'boq_formula_rule_switched_off'

#: R, a number and at most one letter (R7a), or since DW-6 (answer 4) a plain name of capital
#: letters and digits starting with a letter (INC2P), so the INC 2P rule cannot be confused
#: with R12 (OPX-177, earthing).
KEY_RE = re.compile(r'R\d+[a-z]?|[A-Z][A-Z0-9]{1,15}')

_SITE_FACTORS = {value for value, _ in SITE_FACTOR_CHOICES if value}
_WARNING_CHECKS = {value for value, _ in WARNING_CHECK_CHOICES if value}

#: The model's decimal places, so a value the column would round is refused, not changed.
_PLACES = {'multiplier': 6, 'divisor': 6, 'constant': 4, 'weight': 6}

# Rule steps (Step.kind).
CREATE, SAME, REPLACE, REACTIVATE, SWITCH_OFF, OFF = (
    'create', 'same', 'replace', 'reactivate', 'switch off', 'off')
WRITES = (CREATE, REPLACE, REACTIVATE, SWITCH_OFF)
# Group steps (GroupStep.kind).
G_CREATE, G_SAME, G_UPDATE = 'create', 'same', 'update'


class RulesFileError(Exception):
    """The file itself is malformed (before the database is looked at)."""


# ---------------------------------------------------------------------------
# Reading the file
# ---------------------------------------------------------------------------

def _decimal(value, name, where, places):
    """A Decimal from the file's string, refused if it has more places than the column."""
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise RulesFileError(f'{where}: {name} {value!r} is not a number.')
    if not number.is_finite():
        raise RulesFileError(f'{where}: {name} {value!r} is not a number.')
    if number.as_tuple().exponent < -places:
        raise RulesFileError(f'{where}: {name} {value} has more than {places} decimal places.')
    return _plain(number)


def _plain(number):
    """Trailing zeros dropped, never in exponent form: 150.000000 -> 150, not 1.5E+2."""
    number = number.normalize()
    return number.quantize(Decimal(1)) if number.as_tuple().exponent > 0 else number


def _text(value, name, where):
    if not isinstance(value, str) or not value.strip():
        raise RulesFileError(f'{where}: {name} is missing or empty.')
    return value.strip()


def parse_rules(raw):
    """(groups, rules) from the file's bytes, every value normalised. Pure: no database.

    Raises RulesFileError naming the first problem. utf-8-sig drops a BOM an editor may add.
    """
    try:
        data = json.loads(raw.decode('utf-8-sig'))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RulesFileError(f'The file is not valid JSON: {exc}')
    if not isinstance(data, dict) or not isinstance(data.get('rules'), list) \
            or not isinstance(data.get('groups'), list):
        raise RulesFileError('The file needs a "groups" list and a "rules" list.')

    groups, group_names = [], set()
    for n, entry in enumerate(data['groups'], start=1):
        where = f'group {n}'
        name = _text(entry.get('name'), 'name', where)
        where = f'group {name!r}'
        if name in group_names:
            raise RulesFileError(f'{where} is listed twice.')
        group_names.add(name)
        members = entry.get('members')
        if not isinstance(members, list) or not members:
            raise RulesFileError(f'{where}: members must be a non-empty list of codes.')
        members = [_text(code, 'member', where).upper() for code in members]
        if len(set(members)) != len(members):
            raise RulesFileError(f'{where}: a member is listed twice.')
        group = {'name': name, 'label': _text(entry.get('label'), 'label', where),
                 'members': sorted(members)}
        # DW-5: "open" is stored only when true, so a file without it parses (and checksums)
        # exactly as before.
        if 'open' in entry:
            if not isinstance(entry['open'], bool):
                raise RulesFileError(f'{where}: open must be true or false.')
            if entry['open']:
                group['open'] = True
        groups.append(group)

    rules, keys, targets = [], set(), {}
    for n, entry in enumerate(data['rules'], start=1):
        key = _text(entry.get('key'), 'key', f'rule {n}')
        where = f'rule {key}'
        if not KEY_RE.fullmatch(key):
            raise RulesFileError(f'{where}: a key is R, a number and at most one letter '
                                 f'(R7a), or capital letters and digits (INC2P).')
        if key in keys:
            raise RulesFileError(f'{where} is listed twice.')
        keys.add(key)
        target = _text(entry.get('target'), 'target', where).upper()
        if target in targets:
            raise RulesFileError(f'{where}: {target} is also the target of '
                                 f'{targets[target]}; an item has at most one rule.')
        targets[target] = key
        rounding = entry.get('rounding')
        if rounding not in (ROUND_UP, ROUND_NONE):
            raise RulesFileError(f'{where}: rounding is "{ROUND_UP}" or "{ROUND_NONE}".')
        for flag in ('floor_one_when_driven', 'active'):
            if not isinstance(entry.get(flag), bool):
                raise RulesFileError(f'{where}: {flag} must be true or false.')
        divisor = _decimal(entry.get('divisor'), 'divisor', where, _PLACES['divisor'])
        if divisor <= 0:
            raise RulesFileError(f'{where}: the divisor must be above 0.')

        drivers = []
        for d in entry.get('drivers') or []:
            weight = _decimal(d.get('weight', '1'), 'weight', where, _PLACES['weight'])
            label = (d.get('label') or '').strip()
            if ('item' in d) == ('group' in d):
                raise RulesFileError(f'{where}: a driver names exactly one of "item" and '
                                     f'"group".')
            if 'item' in d:
                driver = {'item': _text(d['item'], 'item', where).upper(),
                          'unit': _text(d.get('unit'), 'driver unit', where),
                          'weight': weight, 'label': label}
            else:
                name = _text(d['group'], 'group', where)
                if name not in group_names:
                    raise RulesFileError(f'{where}: group {name!r} is not in "groups".')
                driver = {'group': name, 'weight': weight, 'label': label}
            # DW-6: "role" stored only for a cross-check line, so a file without it parses
            # (and checksums) exactly as before.
            role = d.get('role', DRIVER_ROLE_SUM)
            if role not in (DRIVER_ROLE_SUM, DRIVER_ROLE_CHECK):
                raise RulesFileError(f'{where}: a driver role is "{DRIVER_ROLE_SUM}" or '
                                     f'"{DRIVER_ROLE_CHECK}".')
            if role == DRIVER_ROLE_CHECK:
                driver['role'] = DRIVER_ROLE_CHECK
            drivers.append(driver)
        extras = _rule_extras(entry, where, drivers)
        excel = entry.get('excel') or {}
        rules.append({
            'key': key, 'target': target,
            'unit': _text(entry.get('unit'), 'unit', where),
            'summary': _text(entry.get('summary'), 'summary', where),
            'drivers': drivers,
            'multiplier': _decimal(entry.get('multiplier'), 'multiplier', where,
                                   _PLACES['multiplier']),
            'divisor': divisor,
            'constant': _decimal(entry.get('constant'), 'constant', where,
                                 _PLACES['constant']),
            'rounding': rounding,
            'floor_one_when_driven': entry['floor_one_when_driven'],
            'active': entry['active'],
            'excel': {'row': excel.get('row'), 'formula': excel.get('formula', '')},
            **extras,
        })
    return groups, rules


def _rule_extras(entry, where, drivers):
    """DW-6's optional rule keys, each returned only when it is not the default (so the
    parsed form, and the checksum, of a file without them is unchanged)."""
    extras = {}
    factor = entry.get('site_factor', '')
    if factor not in _SITE_FACTORS | {''}:
        raise RulesFileError(f'{where}: site_factor is "" or "{SITE_FACTOR_MODULE_WP}".')
    if factor:
        extras['site_factor'] = factor
    clamp = entry.get('clamp_at_zero', False)
    if not isinstance(clamp, bool):
        raise RulesFileError(f'{where}: clamp_at_zero must be true or false.')
    if clamp:
        extras['clamp_at_zero'] = True
    check = entry.get('warning_check', '')
    if check not in _WARNING_CHECKS | {''}:
        raise RulesFileError(f'{where}: warning_check is "" or one of '
                             f'{", ".join(sorted(_WARNING_CHECKS))}.')
    has_check_lines = any(d.get('role') == DRIVER_ROLE_CHECK for d in drivers)
    if check and not has_check_lines:
        raise RulesFileError(f'{where}: warning_check needs at least one driver with '
                             f'"role": "check" (the lines it compares against).')
    if has_check_lines and not check:
        raise RulesFileError(f'{where}: a "role": "check" driver is read only by a '
                             f'warning_check; this rule has none.')
    if check:
        extras['warning_check'] = check
    return extras

def checksum(groups, rules):
    """sha256 of the parsed rules in a fixed form: line endings and spacing in the file do
    not change it, a changed value does."""
    def plain(value):
        if isinstance(value, Decimal):
            return format(value, 'f')
        if isinstance(value, dict):
            return {k: plain(v) for k, v in value.items()}
        if isinstance(value, list):
            return [plain(v) for v in value]
        return value
    text = json.dumps({'groups': plain(groups), 'rules': plain(rules)}, sort_keys=True,
                      separators=(',', ':'), ensure_ascii=False)
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def read_file(path):
    """(groups, rules, checksum) of the rules file. Raises CommandError if it is malformed."""
    try:
        groups, rules = parse_rules(Path(path).read_bytes())
    except RulesFileError as exc:
        raise CommandError(f'{path}: {exc}')
    return groups, rules, checksum(groups, rules)


def keys_by_target(path=DEFAULT_FILE):
    """{target code: key} from the rules file. For the trial report."""
    _groups, rules, _sha = read_file(path)
    return {r['target']: r['key'] for r in rules}


# ---------------------------------------------------------------------------
# Comparing the file with the database
# ---------------------------------------------------------------------------

def _file_definition(entry):
    """What a rule says, in a form a stored rule can be compared with. DW-6's fields come
    last, with the defaults a rule without them has, so a pre-DW-6 rule compares "same"."""
    drivers = frozenset(
        ('item', d['item'], d['weight'], d['label'], d.get('role', DRIVER_ROLE_SUM))
        if 'item' in d
        else ('group', d['group'], d['weight'], d['label'], d.get('role', DRIVER_ROLE_SUM))
        for d in entry['drivers'])
    return (entry['summary'], entry['multiplier'], entry['divisor'], entry['constant'],
            entry['rounding'], entry['floor_one_when_driven'], None, drivers,
            entry.get('site_factor', ''), entry.get('clamp_at_zero', False),
            entry.get('warning_check', ''))


def _db_definition(rule):
    """The same tuple for a stored rule. Its drivers must already be prefetched (with their
    item and group), so this costs no query. Decimals compare by value: 5 == 5.000000."""
    drivers = frozenset(
        ('item', d.item.code, _plain(d.weight), d.label, d.role) if d.item_id
        else ('group', d.group.name, _plain(d.weight), d.label, d.role)
        for d in rule.drivers.all())
    return (rule.summary, _plain(rule.multiplier), _plain(rule.divisor),
            _plain(rule.constant), rule.rounding, rule.floor_one_when_driven,
            rule.requires_group.name if rule.requires_group_id else None, drivers,
            rule.site_factor, rule.clamp_at_zero, rule.warning_check)


_FIELD_NAMES = ('summary', 'multiplier', 'divisor', 'constant', 'rounding', 'at least 1',
                'required group', 'drivers', 'site factor', 'clamp at 0', 'warning check')


def _differences(old, new):
    out = []
    for name, a, b in zip(_FIELD_NAMES, old, new):
        if a == b:
            continue
        if name == 'drivers':
            out.append('drivers')
        else:
            out.append(f'{name} {a} -> {b}')
    return out


def formula_text(entry):
    """The rule in one ASCII line, e.g. '= 5 x (lightning arresters), round up'. ASCII
    because a piped Windows console is cp1252 (see fix_tin_shade_ballast).

    DW-6: a negative weight is written with a minus ('modules - Tin Shade'), the wattage
    factor as 'x module Wp', and a clamp and a check are named after the rounding. With
    none of them the line is exactly what it was."""
    joined = ''
    summed = [d for d in entry['drivers'] if d.get('role') != DRIVER_ROLE_CHECK]
    for n, d in enumerate(summed):
        name = d['label'] or d.get('group') or d.get('item')
        weight = d['weight']
        size = -weight if weight < 0 else weight
        term = name if size == 1 else f"{size} x {name}"
        if n == 0:
            joined = ('- ' + term) if weight < 0 else term
        else:
            joined += (' - ' if weight < 0 else ' + ') + term
    if not summed:
        text = f"= {entry['constant']} (fixed)"
    else:
        text = '= ' + (joined if len(summed) == 1 else '(' + joined + ')')
        if entry['multiplier'] != 1:
            text += f" x {entry['multiplier']}"
        if entry.get('site_factor') == SITE_FACTOR_MODULE_WP:
            text += " x module Wp"
        if entry['divisor'] != 1:
            text += f" / {entry['divisor']}"
        if entry['constant']:
            text += f" + {entry['constant']}"
        text += ', round up' if entry['rounding'] == ROUND_UP else ', exact to 0.01'
        if entry.get('clamp_at_zero'):
            text += ', below 0 -> 0 with a warning'
        if entry.get('warning_check'):
            checks = len(entry['drivers']) - len(summed)
            text += f", check {entry['warning_check']} ({checks} lines)"
    return text


@dataclass
class Step:
    kind: str
    entry: dict
    active_rule: object = None      # the target's active rule now, if any
    reuse: object = None            # a retired rule identical to the file's, to switch on
    differences: list = field(default_factory=list)


@dataclass
class GroupStep:
    kind: str
    entry: dict
    group: object = None
    add: list = field(default_factory=list)
    remove: list = field(default_factory=list)
    new_label: bool = False


@dataclass
class Plan:
    groups: list = field(default_factory=list)
    steps: list = field(default_factory=list)
    others: list = field(default_factory=list)      # active rules on targets not in the file
    problems: list = field(default_factory=list)
    items: dict = field(default_factory=dict)

    def to_write(self):
        return ([g for g in self.groups if g.kind != G_SAME]
                + [s for s in self.steps if s.kind in WRITES])


def _rules_with_drivers(queryset):
    """Rules with their target, gate and drivers (with item and group) joined in: three
    queries however many rules, so the comparison below costs nothing per rule."""
    return list(queryset.select_related('target', 'requires_group')
                .prefetch_related('drivers__item', 'drivers__group').order_by('-pk'))


def plan_load(groups, rules):
    """What --apply would write, and every refusal. Reads only."""
    plan = Plan()
    codes = {r['target'] for r in rules}
    for r in rules:
        codes.update(d['item'] for d in r['drivers'] if 'item' in d)
    for g in groups:
        codes.update(g['members'])
    # Every catalogue row the file names, in one query.
    items = {m.code: m for m in BOQItemMaster.objects.filter(code__in=codes)}
    plan.items = items

    for code in sorted(codes - set(items)):
        plan.problems.append(f'{code} is not in the catalogue.')
    for code in sorted(c for c in codes & set(items) if items[c].project_type != 'OPEX'):
        plan.problems.append(f'{code} is not a RESCO item; every rule here is RESCO.')
    for r in rules:
        item = items.get(r['target'])
        if item is None:
            continue
        if r['target'] == ACCB_CONFIGURED_CODE:
            plan.problems.append(f"{r['key']}: {ACCB_CONFIGURED_CODE} is written only by the "
                                 f"ACCB section and cannot be calculated.")
        if not item.is_active:
            plan.problems.append(f"{r['key']}: the target {item.code} is retired.")
        if item.is_mandatory:
            plan.problems.append(f"{r['key']}: the target {item.code} is mandatory; a "
                                 f"calculated item cannot also be filled in by hand on "
                                 f"every BOQ.")
        if item.unit != r['unit']:
            plan.problems.append(f"{r['key']}: {item.code} is in {item.unit} in the "
                                 f"catalogue, not {r['unit']}.")
        for d in r['drivers']:
            driver = items.get(d.get('item'))
            if driver is not None and driver.unit != d['unit']:
                plan.problems.append(f"{r['key']}: the driver {driver.code} is in "
                                     f"{driver.unit} in the catalogue, not {d['unit']}.")

    # B-2b's demo rules are test data on the same items (OPX-184, OPX-185): they must go
    # first, through their own command, which also removes their records.
    if (BOQFormulaRule.objects.filter(summary__startswith=DEMO_SUMMARY_PREFIX).exists()
            or BOQItemGroup.objects.filter(name__startswith=DEMO_GROUP_PREFIX).exists()):
        plan.problems.append('The B-2b DEMO rules are on this database. Remove them first: '
                             'python manage.py seed_formula_demo --remove')

    if not plan.problems:
        _check_loop(plan, groups, rules, items)

    # Groups: the stored ones by name, with members (two queries).
    stored = {g.name: g for g in BOQItemGroup.objects.filter(
        name__in=[g['name'] for g in groups]).prefetch_related('members__item')}
    for entry in groups:
        group = stored.get(entry['name'])
        if group is None:
            plan.groups.append(GroupStep(G_CREATE, entry, add=list(entry['members'])))
            continue
        have = {m.item.code for m in group.members.all()}
        want = set(entry['members'])
        # DW-5: an open group's extra members (added by the Head's catalogue form) stay.
        remove = [] if entry.get('open') else sorted(have - want)
        step = GroupStep(G_SAME, entry, group, add=sorted(want - have),
                         remove=remove, new_label=group.label != entry['label'])
        if step.add or step.remove or step.new_label:
            step.kind = G_UPDATE
        plan.groups.append(step)

    # Rules: every stored rule on the file's targets, active and retired, newest first.
    on_targets = {}
    for rule in _rules_with_drivers(BOQFormulaRule.objects.filter(target__code__in=codes)):
        on_targets.setdefault(rule.target.code, []).append(rule)
    for entry in rules:
        stored_rules = on_targets.get(entry['target'], [])
        active = next((r for r in stored_rules if r.is_active), None)
        want = _file_definition(entry)
        if not entry['active']:
            plan.steps.append(Step(SWITCH_OFF if active else OFF, entry, active))
            continue
        if active is not None:
            have = _db_definition(active)
            if have == want:
                plan.steps.append(Step(SAME, entry, active))
                continue
            step = Step(REPLACE, entry, active, differences=_differences(have, want))
        else:
            step = Step(CREATE, entry)
        step.reuse = next((r for r in stored_rules
                           if not r.is_active and _db_definition(r) == want), None)
        if step.kind == CREATE and step.reuse is not None:
            step.kind = REACTIVATE
        plan.steps.append(step)

    # Active rules the file does not name: listed, never touched.
    targets = {r['target'] for r in rules}
    plan.others = list(BOQFormulaRule.objects.filter(is_active=True)
                       .exclude(target__code__in=targets).select_related('target')
                       .order_by('target__code'))
    return plan


def _check_loop(plan, groups, rules, items):
    """Refuse a loop among the file's active rules before anything is written. Every save
    re-checks the stored whole as well (B-2a), which also covers rules not in the file."""
    members = {g['name']: tuple(items[c].pk for c in g['members']) for g in groups}
    labels = {g['name']: g['label'] for g in groups}
    group_ids = {g['name']: n for n, g in enumerate(groups, start=1)}   # stand-in ids
    specs = []
    for n, r in enumerate(x for x in rules if x['active']):
        def spec(d):
            if 'item' in d:
                return DriverSpec(weight=d['weight'], label=d['label'] or d['item'],
                                  item_id=items[d['item']].pk)
            return DriverSpec(weight=d['weight'], label=d['label'] or labels[d['group']],
                              group_id=group_ids[d['group']], member_ids=members[d['group']])
        specs.append(RuleSpec(
            rule_id=n, target_id=items[r['target']].pk, summary=r['summary'],
            drivers=tuple(spec(d) for d in r['drivers'] if d.get('role') != DRIVER_ROLE_CHECK),
            # DW-6: cross-check lines are read too, so a loop through one is refused.
            check_drivers=tuple(spec(d) for d in r['drivers']
                                if d.get('role') == DRIVER_ROLE_CHECK),
            clamp_at_zero=r.get('clamp_at_zero', False),
            warning_check=r.get('warning_check', '')))
    try:
        build_rule_set(specs, {m.pk: m.code for m in items.values()}, {})
    except FormulaError as exc:
        plan.problems.append(str(exc))
        return
    # DW-6: a rule that raises warnings reads only hand-entered lines (the save-time check
    # in validate_formula_rules() says the same); refused here first, with nothing written.
    targets = {s.target_id for s in specs}
    codes = {m.pk: m.code for m in items.values()}
    for s in specs:
        if s.raises_warnings and s.dependency_ids() & targets:
            plan.problems.append(
                f'{codes[s.target_id]}: a rule that raises warnings may read only lines '
                f'entered by hand, not ' + ', '.join(sorted(codes[i] for i in
                                                          s.dependency_ids() & targets)) + '.')


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def _create_rule(entry, items, groups_by_name):
    rule = BOQFormulaRule(
        target=items[entry['target']], summary=entry['summary'],
        multiplier=entry['multiplier'], divisor=entry['divisor'],
        constant=entry['constant'], rounding=entry['rounding'],
        floor_one_when_driven=entry['floor_one_when_driven'],
        site_factor=entry.get('site_factor', ''),
        clamp_at_zero=entry.get('clamp_at_zero', False),
        warning_check=entry.get('warning_check', ''))
    rule.save()
    for d in entry['drivers']:
        role = d.get('role', DRIVER_ROLE_SUM)
        if 'item' in d:
            BOQFormulaDriver(rule=rule, item=items[d['item']], weight=d['weight'],
                             label=d['label'], role=role).save()
        else:
            BOQFormulaDriver(rule=rule, group=groups_by_name[d['group']], weight=d['weight'],
                             label=d['label'], role=role).save()
    return rule


def _set_active(rule, active):
    rule.is_active = active
    rule.save(update_fields=['is_active', 'updated_at'])


def write_plan(plan):
    """Carry out the plan. Runs inside handle()'s transaction; any refusal from a save()
    (a loop through a rule the file does not name) rolls the whole load back. Returns the
    count per kind written."""
    written = {}

    def count(kind):
        written[kind] = written.get(kind, 0) + 1

    groups_by_name = {}
    for step in plan.groups:
        group = step.group
        if step.kind == G_CREATE:
            group = BOQItemGroup(name=step.entry['name'], label=step.entry['label'])
            group.save()
        elif step.new_label:
            group.label = step.entry['label']
            group.save()
        groups_by_name[step.entry['name']] = group
        if step.kind == G_SAME:
            continue
        # Removed before added: a member leaving a group can never close a loop, a new
        # one can, and the save() of each new member re-checks the graph.
        if step.remove:
            BOQItemGroupMember.objects.filter(group=group,
                                              item__code__in=step.remove).delete()
        for code in step.add:
            BOQItemGroupMember(group=group, item=plan.items[code]).save()
        count(f'group {step.kind}')

    for step in plan.steps:
        if step.kind not in WRITES:
            continue
        # The active rule is retired first: one active rule per target is a database
        # UNIQUE, so the replacement cannot be saved while the old one is active.
        if step.active_rule is not None and step.kind in (REPLACE, SWITCH_OFF):
            _set_active(step.active_rule, False)
        if step.kind in (CREATE, REPLACE, REACTIVATE):
            if step.reuse is not None:
                _set_active(step.reuse, True)
            else:
                _create_rule(step.entry, plan.items, groups_by_name)
        count(step.kind)
    return written


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------

class Command(BaseCommand):
    help = ('Make the BOQ formula rules match the rules file (B-2c). Dry run unless '
            '--apply; --deactivate KEY switches one rule off.')

    def add_arguments(self, parser):
        parser.add_argument('--file', default=str(DEFAULT_FILE),
                            help='The rules file (default: the DW-6 file in projects/data).')
        parser.add_argument('--apply', action='store_true',
                            help='Write. Without it nothing is written.')
        parser.add_argument('--deactivate', action='append', metavar='KEY', default=[],
                            help='Switch off this rule (repeatable), and do nothing else.')

    def _say(self, text=''):
        """Write one line the console can print: a character its encoding lacks becomes
        '?' rather than stopping the run (cp1252 on a piped Windows console)."""
        encoding = getattr(self.stdout._out, 'encoding', None) or 'utf-8'
        self.stdout.write(text.encode(encoding, 'replace').decode(encoding))

    def handle(self, *args, **options):
        print_db_banner(self)
        path = Path(options['file'])
        if not path.is_file():
            raise CommandError(f'No such file: {path}')
        groups, rules, sha = read_file(path)
        apply = options['apply']
        mode = 'APPLY' if apply else 'DRY RUN (nothing written; use --apply to write)'
        self._say(f'load_formula_rules - {mode}')
        self._say(f'file: {path}')
        on = sum(1 for r in rules if r['active'])
        self._say(f'checksum (parsed rules): {sha}   groups: {len(groups)}   rules: '
                  f'{len(rules)} ({on} on, {len(rules) - on} off)')
        if options['deactivate']:
            return self._deactivate(options['deactivate'], rules, apply, path)

        plan = plan_load(groups, rules)
        written = None
        if apply and not plan.problems:
            # Planned again and written in one transaction: the plan above is what was
            # checked and printed; this one is what is written. RACE: two loads at once
            # would both plan a create; the second save fails on the one-active-per-target
            # UNIQUE and its whole transaction rolls back, so nothing is written twice.
            with transaction.atomic():
                plan = plan_load(groups, rules)
                if not plan.problems:
                    try:
                        written = write_plan(plan)
                    except ValidationError as exc:
                        raise CommandError('Refused while saving, nothing written: '
                                           + ' '.join(exc.messages))
                    if written:
                        log_activity(None, None,
                                     f'{ACTOR} applied {path.name} (checksum {sha[:12]}): '
                                     + ', '.join(f'{n} {k}' for k, n in written.items()),
                                     entity_type='BOQFormulaRule',
                                     action_code=LOADED_ACTION_CODE)
        self._print(plan, applied=written is not None)
        if plan.problems:
            raise CommandError(f'Refused: {len(plan.problems)} problem(s) above. Nothing was '
                               f'written.')
        if written is not None:
            self._say('Written: ' + (', '.join(f'{n} {k}' for k, n in written.items())
                                     or 'nothing (the database already matches the file)'))

    # -- printing ----------------------------------------------------------------

    def _print(self, plan, applied):
        verb = '' if applied else ' (would be)'
        if plan.problems:
            self._say('REFUSED:')
            for problem in plan.problems:
                self._say(f'  {problem}')

        reactivated = [s for s in plan.steps if s.reuse is not None]
        if reactivated:
            self._say(f'REACTIVATE{verb} - switched off earlier, switched back on because the '
                      f'file says "active": true:')
            for s in reactivated:
                self._say(f"  REACTIVATE  {s.entry['key']:5} {s.entry['target']}  "
                          f"{s.entry['summary']}  (rule #{s.reuse.pk})")

        self._say(f'Groups{verb}:')
        for g in plan.groups:
            detail = f"({len(g.entry['members'])} members)"
            if g.kind == G_UPDATE:
                parts = []
                if g.add:
                    parts.append('add ' + ', '.join(g.add))
                if g.remove:
                    parts.append('remove ' + ', '.join(g.remove))
                if g.new_label:
                    parts.append(f"label -> {g.entry['label']!r}")
                detail = '; '.join(parts)
            self._say(f"  {g.kind:7} {g.entry['name']}  {detail}")

        self._say(f'Rules{verb}:')
        for s in plan.steps:
            e = s.entry
            line = f"  {s.kind:10} {e['key']:5} {e['target']}  {e['summary']} {formula_text(e)}"
            if s.kind == REPLACE:
                line += f"  [rule #{s.active_rule.pk} retired: {'; '.join(s.differences)}]"
            elif s.kind in (SWITCH_OFF, OFF):
                line += '  [the file says "active": false]'
            self._say(line)

        if plan.others:
            self._say('Active rules not in the file (left as they are):')
            for rule in plan.others:
                self._say(f'  {rule.target.code}  {rule.summary}  (rule #{rule.pk})')

        tally = {}
        for s in plan.steps:
            tally[s.kind] = tally.get(s.kind, 0) + 1
        gtally = {}
        for g in plan.groups:
            gtally[g.kind] = gtally.get(g.kind, 0) + 1
        self._say('Summary: rules ' + ', '.join(f'{k} {n}' for k, n in tally.items())
                  + '; groups ' + ', '.join(f'{k} {n}' for k, n in gtally.items()))

    # -- --deactivate ------------------------------------------------------------

    def _deactivate(self, keys, rules, apply, path):
        by_key = {r['key']: r for r in rules}
        unknown = [k for k in keys if k not in by_key]
        if unknown:
            hints = []
            for k in unknown:
                lettered = sorted(x for x in by_key if re.fullmatch(re.escape(k) + '[a-z]', x))
                if lettered:
                    hints.append(f'{k} is {", ".join(lettered)}')
            raise CommandError('Not a key in the file: ' + ', '.join(unknown)
                               + (f' ({"; ".join(hints)})' if hints else '') + '.')

        to_switch = []
        for key in keys:
            entry = by_key[key]
            rule = (BOQFormulaRule.objects.filter(target__code=entry['target'], is_active=True)
                    .select_related('target').first())
            if rule is None:
                self._say(f"  {key:5} {entry['target']}  already off: nothing to do")
                continue
            # Lines on the item, and the overrides on them: they stay as they are.
            lines = BOQItem.objects.filter(item_master=rule.target)
            overrides = BOQLineOverride.objects.filter(line__item_master=rule.target).count()
            self._say(f"  switch off {key:5} {rule.target.code}  {rule.summary}  (rule #{rule.pk})")
            self._say(f'      {lines.count()} BOQ line(s) on {rule.target.code} keep their '
                      f'quantities and become ordinary lines.')
            self._say(f'      {overrides} override(s) on those lines stay, unused; if the rule '
                      f'is switched back on they are used again.')
            to_switch.append((key, rule))

        self._say(f'NOTE: {path.name} is the source of truth. The next load_formula_rules '
                  f'--apply switches these back on unless the file says "active": false.')
        if apply and to_switch:
            with transaction.atomic():
                for key, rule in to_switch:
                    _set_active(rule, False)
                    log_activity(None, None,
                                 f'{ACTOR} switched off {key} ({rule.target.code}, rule '
                                 f'#{rule.pk}): its lines are ordinary lines until it is '
                                 f'loaded again', entity_type='BOQFormulaRule',
                                 entity_id=rule.pk, action_code=SWITCHED_OFF_ACTION_CODE)
            self._say(f'Switched off: {len(to_switch)}')
        elif to_switch:
            self._say(f'Would switch off: {len(to_switch)}')
