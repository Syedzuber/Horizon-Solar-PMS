"""
B-2a — the BOQ formula engine: rules stored as data, evaluated on the server.

A FORMULA RULE says how one catalogue item's quantity follows from other lines on the same
site's BOQ, in Praveen's words ("5 m of earthing cable per lightning arrester"). It is a
ROW, not an expression: BOQFormulaRule holds the target item, a multiplier, a divisor, an
added constant, a rounding mode and the "at least 1" floor; BOQFormulaDriver rows name what
is summed (one item, or a named group of items, each with a weight). There is no string
that is parsed, and nothing here ever calls eval().

    quantity = round((sum of weight x driver quantity) x multiplier / divisor + constant)

  * ROUNDING. 'up' rounds up to a whole number (packs, pieces, metres: F4-17). 'none' keeps
    the exact value and rounds half-up to 0.01, because the BOQ column holds two decimals
    (INC work in kWp, R24/R25: Zuber's B-2a Q2).
  * THE G3 FLOOR. "At least 1, only when the driver sum is above 0." On an 'up' rule it can
    never change anything (rounding up any positive number already gives at least 1); it
    is kept per rule because it records Praveen's intent and it does matter on 'none' rows.
  * REQUIRES GROUP (B-2a Q3). Optional. The rule gives 0 unless the site has at least one
    of that group, constant included. Empty means the rule always applies.
  * CHAINS. A driver may itself be a computed item (R7, R14, R17, R22, R23). Rules are
    evaluated in dependency order, and a chained driver uses the computed item's ROUNDED
    quantity, the number on the sheet (B-2a Q1). Loops are refused when a rule, driver or
    group member is saved, and again, defensively, at evaluation.

PURE WHERE IT MATTERS. evaluate() and build_rule_set() take plain data and touch no
database, request or model. load_rules() is the one reader and issues the same three
queries however many rules exist. B-2b hooks the BOQ write paths through
prepare_formula_save() and apply_formulas() at the end of this module; Praveen's real rules
are loaded in B-2c, so until then there are no rule rows and those hooks cost one query.

THIS MODULE IMPORTS NO MODELS AT MODULE LEVEL. models.py imports the rounding constants and
validate_formula_rules() from here, so the two functions that read the database import
models inside themselves.
"""
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_HALF_UP, Decimal, localcontext
from typing import Mapping

from django.core.exceptions import ValidationError

#: Round up to a whole number: packs, pieces and metres (F4-17).
ROUND_UP = 'up'
#: Keep the exact value, rounded half-up to 0.01 for the two-decimal column (R24/R25).
ROUND_NONE = 'none'

ROUNDING_CHOICES = [
    (ROUND_UP,   'Round up to a whole number'),
    (ROUND_NONE, 'Exact (to 0.01)'),
]

#: The largest value BOQItem.boq_quantity (numeric(10,2)) can hold. Written out rather than
#: read from the model because this module imports no models; tests_boq_formulas pins it
#: equal to number_input.decimal_field_max(BOQItem, 'boq_quantity').
QUANTITY_MAX = Decimal('99999999.99')

ZERO = Decimal('0')
ONE = Decimal('1')
CENT = Decimal('0.01')

#: Working precision. Quantities have 2 decimals and weights and multipliers 6, so every
#: product is exact far below 50 digits. A division that does not terminate (x / 1.5) is
#: cut at 50 digits, which is too fine to move a whole-number or 0.01 rounding.
_PRECISION = 50


class FormulaError(Exception):
    """A rule set or a result the engine cannot work with (bad data, out-of-range result)."""


class FormulaCycleError(FormulaError):
    """Rules that depend on each other in a loop, so no order of evaluation exists."""


@dataclass(frozen=True)
class DriverSpec:
    """One driver of a rule: an item (item_id) or a named group (member_ids), weighted."""
    weight: Decimal
    label: str                       # How the explanation names it: an item code, or the group's plural label
    item_id: int | None = None
    group_id: int | None = None
    member_ids: tuple = ()           # The group's items (retired ones included); empty for an item driver


@dataclass(frozen=True)
class RuleSpec:
    """One rule as plain data: what evaluate() needs and nothing that can query."""
    rule_id: int
    target_id: int
    summary: str
    drivers: tuple = ()
    multiplier: Decimal = ONE
    divisor: Decimal = ONE
    constant: Decimal = ZERO
    rounding: str = ROUND_UP
    floor_one_when_driven: bool = False
    requires_member_ids: tuple | None = None   # None = always applies; else the gate group's items
    requires_label: str = ''

    def dependency_ids(self):
        """Every item this rule reads: item drivers, group members and the gate group."""
        ids = set()
        for driver in self.drivers:
            if driver.item_id is not None:
                ids.add(driver.item_id)
            ids.update(driver.member_ids)
        if self.requires_member_ids:
            ids.update(self.requires_member_ids)
        return ids


@dataclass(frozen=True)
class RuleSet:
    """Rules in evaluation (dependency) order, plus each referenced item's code and unit."""
    rules: tuple
    codes: Mapping
    units: Mapping


@dataclass(frozen=True)
class Result:
    """One computed line.

    `quantity` is the value the sheet should carry and the value chained rules read;
    `computed` is what the rule gives. They differ only when the caller passed an override
    for this item. `entered` is the quantity the BOQ already carried for the item (None
    when it is not on the sheet or blank): reported, never used by the rule."""
    item_id: int
    rule_id: int
    quantity: Decimal
    computed: Decimal
    raw: Decimal              # Before rounding and the floor; Decimal at working precision
    driver_sum: Decimal       # Sum of weight x driver quantity; 0 for a driverless constant
    explanation: str
    chain: tuple              # Computed items this rests on, directly or through them, in evaluation order
    entered: Decimal | None
    overridden: bool


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------

def build_rule_set(specs, codes, units):
    """Order `specs` so every computed driver is evaluated before the rules that read it.

    Kahn's algorithm. Among rules that are ready at the same time the one whose target code
    sorts first goes first, so the order (and every explanation) never depends on the order
    the rows came back from the database. Raises FormulaCycleError naming the items left in
    a loop, and FormulaError if two rules compute the same item.
    """
    by_target = {}
    for spec in specs:
        if spec.target_id in by_target:
            raise FormulaError(
                f'Two rules compute {codes.get(spec.target_id, spec.target_id)}; '
                f'an item has at most one active rule.')
        by_target[spec.target_id] = spec

    def sort_key(target_id):
        return (str(codes.get(target_id, '')), target_id)

    # waiting_on[t]: the computed items rule t still needs; readers[d]: rules that read d.
    waiting_on = {t: spec.dependency_ids() & set(by_target) for t, spec in by_target.items()}
    readers = {t: set() for t in by_target}
    for t, deps in waiting_on.items():
        for d in deps:
            readers[d].add(t)

    ready = sorted((t for t, deps in waiting_on.items() if not deps), key=sort_key)
    ordered = []
    while ready:
        target = ready.pop(0)
        ordered.append(by_target[target])
        newly_ready = []
        for reader in readers[target]:
            waiting_on[reader].discard(target)
            if not waiting_on[reader]:
                newly_ready.append(reader)
        ready = sorted(ready + newly_ready, key=sort_key)

    if len(ordered) != len(by_target):
        stuck = sorted((t for t in by_target if waiting_on[t]), key=sort_key)
        raise FormulaCycleError(
            'Formula rules form a loop; none of these can be worked out first: '
            + ', '.join(str(codes.get(t, t)) for t in stuck) + '.')
    return RuleSet(rules=tuple(ordered), codes=dict(codes), units=dict(units))


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

def _num(value):
    """A quantity as a person writes it: 15.00 -> '15', 0.10 -> '0.1'."""
    return format(value.normalize(), 'f')


def _raw_text(raw):
    """The unrounded value: exact when it has at most 4 decimals, else cut with '…'."""
    if raw == raw.quantize(Decimal('0.0001'), rounding=ROUND_DOWN):
        return _num(raw)
    return format(raw.quantize(Decimal('0.0001'), rounding=ROUND_DOWN), 'f') + '…'


def _driver_term(driver, values, codes):
    """(quantity, text) for one driver, e.g. (3, '3 lightning arresters (2 X + 1 Y)')."""
    if driver.item_id is not None:
        quantity = values.get(driver.item_id, ZERO)
        text = f'{_num(quantity)} {driver.label}'
    else:
        parts = [(m, values.get(m, ZERO)) for m in driver.member_ids]
        quantity = sum((q for _, q in parts), ZERO)
        shown = [f'{_num(q)} {codes.get(m, m)}' for m, q in parts if q]
        text = f'{_num(quantity)} {driver.label}'
        if shown:
            text += ' (' + ' + '.join(shown) + ')'
    if driver.weight != ONE:
        text = f'{_num(driver.weight)} × {text}'
    return quantity, text


def evaluate(quantities, rules, overrides=None):
    """Work out every rule in `rules` against one BOQ's quantities. Pure: no queries.

    `quantities` maps catalogue item id -> the line's quantity (None or missing counts as 0).
    `overrides` (optional, for B-2b) maps item id -> a quantity to use for that computed
    item instead of the rule's value; the rule's value is still reported as `computed`.

    Returns {target item id: Result} for EVERY rule, zeros included; whether a 0 line is
    written is the caller's decision.

    A computed item's hand-entered quantity is NOT an input to anything: the rule computes
    it, and later rules read the computed (or overridden) value. The entered value is
    reported on the Result as `entered`.

    Raises FormulaCycleError if a rule needs a computed item that has not been worked out
    yet. load_rules() never returns such a set; this is the defensive check for a RuleSet
    built some other way.
    """
    overrides = overrides or {}
    targets = {rule.target_id for rule in rules.rules}
    if len(targets) != len(rules.rules):
        raise FormulaError('Two rules compute the same item; an item has at most one '
                           'active rule.')

    # Working values: the sheet's quantities, overwritten by each computed item as soon as
    # it is worked out, so a later rule reads the computed value, never the hand-entered one.
    values = {item_id: Decimal(q) for item_id, q in quantities.items() if q is not None}
    results = {}
    codes, units = rules.codes, rules.units
    position = {rule.target_id: i for i, rule in enumerate(rules.rules)}

    for rule in rules.rules:
        needed = rule.dependency_ids()
        pending = (needed & targets) - set(results)
        if pending:
            raise FormulaCycleError(
                f'{codes.get(rule.target_id, rule.target_id)} needs '
                + ', '.join(str(codes.get(p, p)) for p in sorted(pending))
                + ' worked out first; the rules loop or are out of order.')

        unit = units.get(rule.target_id, '')
        computed, raw, driver_sum, explanation = _compute(rule, values, codes, unit,
                                                          set(results))

        # The chain: computed items this rule reads, and everything they rest on in turn,
        # listed in evaluation order.
        chain = set()
        for result in results.values():
            if result.item_id in needed:
                chain.update(result.chain)
                chain.add(result.item_id)
        chain = sorted(chain, key=position.__getitem__)

        overridden = rule.target_id in overrides
        quantity = Decimal(overrides[rule.target_id]) if overridden else computed
        if overridden:
            explanation = (f'{rule.summary}: set by hand to {_num(quantity)} {unit}; '
                           f'the rule gives {_num(computed)} {unit}').rstrip()

        entered = quantities.get(rule.target_id)
        results[rule.target_id] = Result(
            item_id=rule.target_id, rule_id=rule.rule_id, quantity=quantity,
            computed=computed, raw=raw, driver_sum=driver_sum, explanation=explanation,
            chain=tuple(chain), entered=None if entered is None else Decimal(entered),
            overridden=overridden)
        values[rule.target_id] = quantity

    return results


def _compute(rule, values, codes, unit, computed_ids):
    """(quantity, raw, driver_sum, explanation) for one rule against the working values.

    `computed_ids` are the items already worked out by earlier rules in this evaluation;
    used only to say so in the explanation."""
    summary = rule.summary

    with localcontext() as ctx:
        ctx.prec = _PRECISION

        # REQUIRES GROUP: answered first, so a site without the group gets 0, constant
        # included (that is the point of the gate for a constant such as WMS).
        if rule.requires_member_ids is not None:
            present = sum((values.get(m, ZERO) for m in rule.requires_member_ids), ZERO)
            if present <= 0:
                return (ZERO.quantize(CENT), ZERO, ZERO,
                        f'{summary}: 0 {unit}, because the site has no '
                        f'{rule.requires_label}'.rstrip())

        driver_sum = ZERO
        terms = []
        for driver in rule.drivers:
            quantity, text = _driver_term(driver, values, codes)
            driver_sum += driver.weight * quantity
            terms.append(text)

        # Multiply before dividing, so a terminating result stays exact (150 x 0.4 / 1.5
        # is exactly 40). Decimal throughout: a float here gives 0.62 x 0.25 = 0.15 instead
        # of 0.155 -> 0.16 (pinned in tests_boq_formulas).
        raw = driver_sum * rule.multiplier / rule.divisor + rule.constant

        if rule.rounding == ROUND_UP:
            quantity = raw.to_integral_value(rounding=ROUND_CEILING)
        elif rule.rounding == ROUND_NONE:
            quantity = raw.quantize(CENT, rounding=ROUND_HALF_UP)
        else:
            raise FormulaError(f'Unknown rounding mode {rule.rounding!r} on the rule for '
                               f'{codes.get(rule.target_id, rule.target_id)}.')

        # G3: at least 1, only when the drivers are above 0. A driverless constant has no
        # driver sum, so the floor never applies to it.
        floored = (rule.floor_one_when_driven and rule.drivers
                   and driver_sum > 0 and quantity < ONE)
        if floored:
            quantity = ONE

    quantity = quantity.quantize(CENT)
    if quantity < 0 or quantity > QUANTITY_MAX:
        raise FormulaError(
            f'The rule for {codes.get(rule.target_id, rule.target_id)} gives '
            f'{_num(quantity)}, outside what a BOQ line can hold (0 to {QUANTITY_MAX}).')

    # The arithmetic, in the order it was done, then what rounding and the floor did.
    if not rule.drivers:
        expression = None
    else:
        expression = terms[0] if len(terms) == 1 else '(' + ' + '.join(terms) + ')'
        if rule.multiplier != ONE:
            expression += f' × {_num(rule.multiplier)}'
        if rule.divisor != ONE:
            expression += f' ÷ {_num(rule.divisor)}'
        if rule.constant:
            expression += f' + {_num(rule.constant)}'

    if expression is None:
        explanation = f'{summary}: a fixed {_num(quantity)} {unit}'
    elif floored:
        explanation = (f'{summary}: {expression} = {_raw_text(raw)}, raised to the minimum '
                       f'of 1 {unit} because the drivers are above 0')
    elif quantity != raw:
        how = 'rounded up to' if rule.rounding == ROUND_UP else 'rounded to'
        explanation = f'{summary}: {expression} = {_raw_text(raw)}, {how} {_num(quantity)} {unit}'
    else:
        explanation = f'{summary}: {expression} = {_num(quantity)} {unit}'

    # Say when a driver is itself a computed line, so "2 × (2 + 1)" can be traced back.
    chained = [d.label for d in rule.drivers
               if d.item_id is not None and d.item_id in computed_ids]
    if chained:
        explanation += (f' ({", ".join(chained)} '
                        f'{"is a computed line" if len(chained) == 1 else "are computed lines"}'
                        f'; the rounded quantity is used)')
    return quantity, raw, driver_sum, explanation.rstrip()


# ---------------------------------------------------------------------------
# Loading and save-time validation (the only functions that touch the database)
# ---------------------------------------------------------------------------

def _load_specs(include_retired_targets, project_type=None):
    """(specs, codes, units, types) from the rule tables in three queries, always three.

    include_retired_targets=False is load_rules(): a rule whose target item is retired is
    skipped (Q8). Retired items still count as drivers and group members, because a retired
    inverter on a BOQ still needs fasteners. Validation passes True, so a loop through a
    retired target is refused too: it would return the day the item is reactivated.

    project_type (B-2b Q3) keeps only the rules whose target is of that type. Q9 makes every
    rule one type throughout, so filtering on the target is filtering the whole rule. None
    is every type, which is what validation needs.
    """
    # Lazy import: models.py imports ROUNDING_CHOICES and validate_formula_rules from this
    # module, so importing models at module level here would be circular.
    from django.db.models import Q
    from .models import BOQFormulaDriver, BOQFormulaRule, BOQItemGroupMember

    rule_q = Q(is_active=True)
    driver_q = Q(rule__is_active=True)
    if not include_retired_targets:
        rule_q &= Q(target__is_active=True)
        driver_q &= Q(rule__target__is_active=True)
    if project_type is not None:
        rule_q &= Q(target__project_type=project_type)
        driver_q &= Q(rule__target__project_type=project_type)

    # 1. Active rules with their target (code, unit, type) and gate group label.
    rules = list(BOQFormulaRule.objects.filter(rule_q)
                 .select_related('target', 'requires_group').order_by('pk'))
    # 2. Those rules' drivers, filtered by the same terms through the join rather than by a
    #    list of rule ids, so the query count does not depend on how many rules exist.
    drivers = list(BOQFormulaDriver.objects.filter(driver_q)
                   .select_related('item', 'group').order_by('pk'))
    # 3. Every group member. The group table is a handful of named lists (five in Praveen's
    #    rules), so reading all of it is cheaper than a filtered join and keeps this at 3.
    members = list(BOQItemGroupMember.objects.select_related('item')
                   .order_by('item__code', 'pk'))

    codes, units, types = {}, {}, {}

    def note(item):
        codes[item.pk], units[item.pk], types[item.pk] = item.code, item.unit, item.project_type

    members_of = {}
    for member in members:
        members_of.setdefault(member.group_id, []).append(member.item_id)
        note(member.item)

    drivers_of = {}
    for driver in drivers:
        if driver.item_id is not None:
            note(driver.item)
            spec = DriverSpec(weight=driver.weight, label=driver.label or driver.item.code,
                              item_id=driver.item_id)
        else:
            spec = DriverSpec(weight=driver.weight, label=driver.label or driver.group.label,
                              group_id=driver.group_id,
                              member_ids=tuple(members_of.get(driver.group_id, ())))
        drivers_of.setdefault(driver.rule_id, []).append(spec)

    specs = []
    for rule in rules:
        note(rule.target)
        gate = rule.requires_group
        specs.append(RuleSpec(
            rule_id=rule.pk, target_id=rule.target_id, summary=rule.summary,
            # A driver whose rule was activated between queries 1 and 2 has no rule here
            # and is simply not used; the next load sees both.
            drivers=tuple(drivers_of.get(rule.pk, ())),
            multiplier=rule.multiplier, divisor=rule.divisor, constant=rule.constant,
            rounding=rule.rounding, floor_one_when_driven=rule.floor_one_when_driven,
            requires_member_ids=(None if gate is None
                                 else tuple(members_of.get(gate.pk, ()))),
            requires_label='' if gate is None else gate.label,
        ))
    return specs, codes, units, types


def load_rules(project_type=None):
    """The active rule set, ordered for evaluate(). Three queries whatever the rule count.

    project_type='OPEX' keeps only RESCO rules (B-2b Q3). The write paths always pass the
    site's type: without it a Residential constant rule would put a Residential catalogue
    line on every RESCO BOQ it saved. None (the default) is every type, as before.
    """
    specs, codes, units, _types = _load_specs(include_retired_targets=False,
                                              project_type=project_type)
    return build_rule_set(specs, codes, units)


def validate_formula_rules():
    """Raise ValidationError if the stored rules loop or mix project types.

    Called by the save() of BOQFormulaRule, BOQFormulaDriver and BOQItemGroupMember AFTER
    the row is written and inside the same transaction, so a refused row is rolled back.
    Checking the stored whole rather than the one row is what catches a loop closed by a
    group member: adding an item to "all inverters" can close a loop through any rule that
    reads that group.
    """
    specs, codes, units, types = _load_specs(include_retired_targets=True)
    try:
        build_rule_set(specs, codes, units)
    except FormulaError as exc:
        raise ValidationError(str(exc))

    # Q9: one project type per rule: the target, item drivers, group members and the gate.
    for spec in specs:
        kinds = {types[i] for i in spec.dependency_ids() | {spec.target_id}}
        if len(kinds) > 1:
            raise ValidationError(
                f'The rule for {codes[spec.target_id]} mixes project types '
                f'({", ".join(sorted(kinds))}). A rule, its drivers and its groups must all '
                f'be one project type.')


# ---------------------------------------------------------------------------
# B-2b — applying the rules to one BOQ, inside a write path's own transaction
#
# THE HOOKED PATHS (Zuber, 22 Nov): the picker save, the spreadsheet upload, and the
# reviewer's add_line and set_quantity. NOT the ACCB endpoint and NOT
# fix_tin_shade_ballast --apply-counts (that hook is B-2c's). Each path calls
# prepare_formula_save() at the START of its transaction, before it reads or writes a line,
# and apply_formulas() at the END, after its own writes.
#
# A LINE IS COMPUTED BECAUSE AN ACTIVE RULE TARGETS ITS CATALOGUE ITEM. That is worked out
# here, from the loaded rules, every time; nothing on BOQItem records it. A designer may
# override a computed line (BOQLineOverride, a one-to-one beside the line); an overridden
# line keeps its quantity until the override is cleared, and later rules read that value.
#
# WITH NO ACTIVE RULES FOR THE SITE'S TYPE, ALL OF THIS COSTS ONE exists() QUERY and
# writes nothing: prepare_formula_save() returns an inactive marker and apply_formulas()
# returns at once. That is the state B-2b ships in.
# ---------------------------------------------------------------------------

#: D2 — what a reviewer is told when they try to set or add a computed item.
FORMULA_REVIEWER_MESSAGE = ("This quantity is calculated. Correct the item it's calculated "
                            "from, or fail the BOQ to send it back.")


@dataclass(frozen=True)
class FormulaSave:
    """What a hooked write path needs from the start of its transaction to the end.

    `rules` is None when no active rule exists for the site's type: then nothing is
    computed, `targets` is empty and apply_formulas() does nothing."""
    rules: RuleSet | None = None

    @property
    def active(self):
        return self.rules is not None

    @property
    def targets(self):
        """Catalogue item ids computed on this save: every active rule's target."""
        if self.rules is None:
            return frozenset()
        return frozenset(rule.target_id for rule in self.rules.rules)


@dataclass(frozen=True)
class FormulaChange:
    """One computed line apply_formulas() wrote, for the caller's message."""
    code: str
    before: Decimal | None        # None = there was no line
    after: Decimal | None         # None = the line was removed (D1)
    unit: str


@dataclass
class FormulaSummary:
    """What apply_formulas() did. Falsy when nothing changed and nothing was skipped."""
    added: list = field(default_factory=list)               # FormulaChange
    changed: list = field(default_factory=list)             # FormulaChange
    removed: list = field(default_factory=list)             # FormulaChange
    kept_at_zero: list = field(default_factory=list)        # FormulaChange: fell to 0, line kept (reviewer path, or ordered)
    skipped_overridden: list = field(default_factory=list)  # Item codes left at their override
    skipped_multiple: list = field(default_factory=list)    # Item codes on 2+ lines, not written (Q9)
    results: dict = field(default_factory=dict)             # evaluate()'s output, by item id

    @property
    def changes(self):
        return self.added + self.changed + self.removed + self.kept_at_zero

    def __bool__(self):
        return bool(self.changes or self.skipped_multiple)


def formula_rules_exist(project_type):
    """True if an active rule with an active target of `project_type` exists. One query.

    The same terms as load_rules(project_type): a rule on a retired target is not applied,
    so it must not make a save pay for the lock and the load either."""
    # Lazy import: models.py imports this module, so importing models at the top would be
    # circular.
    from .models import BOQFormulaRule
    return BOQFormulaRule.objects.filter(
        is_active=True, target__is_active=True, target__project_type=project_type,
    ).exists()


def lock_boq(boq):
    """Lock the BOQ row for the rest of the caller's transaction (B-2b Q2). One query.

    TAKEN AT THE START OF THE HOOKED TRANSACTION, BEFORE ANY LINE IS READ OR WRITTEN. Two
    saves on one BOQ (QC and the Head both in awaiting_head_qc, two designer tabs, an upload
    beside a picker save) would otherwise each compute from the other's uncommitted driver,
    and the later commit would leave a stale computed line. Taking it here, not inside
    apply_formulas(), also avoids a deadlock: the picker updates every row on the sheet, so
    a lock taken only at the end could wait on a line the other save holds while that save
    waits on the BOQ. Django drops FOR UPDATE on SQLite, which has no row locks."""
    # Lazy import: models.py imports this module (see formula_rules_exist).
    from .models import BOQ
    list(BOQ.objects.select_for_update().filter(pk=boq.pk).values_list('pk', flat=True))


def prepare_formula_save(boq, project_type):
    """Start of a hooked write. One query with no rules; with rules, the lock plus three.

    The caller must be inside transaction.atomic() and must call this before it reads the
    lines it is about to change."""
    if not formula_rules_exist(project_type):
        return FormulaSave()
    lock_boq(boq)
    return FormulaSave(rules=load_rules(project_type))


def _override_of(line):
    """The line's BOQLineOverride or None, with no query (select_related loaded it)."""
    from django.core.exceptions import ObjectDoesNotExist
    try:
        return line.formula_override
    except ObjectDoesNotExist:
        return None


def apply_formulas(boq, prepared, *, source, actor, driving_correction=None,
                   remove_zero=True):
    """Bring every computed line on `boq` up to date. Returns a FormulaSummary.

    Runs inside the caller's transaction, after the caller's own writes, with the rules and
    the lock from prepare_formula_save(). With no rules it returns at once: no query, no
    write.

    THE INPUT (pre-flight P4). Every line with a catalogue item, ACCB lines included (Qty 1
    each), summed per item; lines with no item are left out; a blank quantity counts as
    nothing; retired items count. An OVERRIDDEN computed line goes to evaluate() as an
    override, so later rules read the quantity on the sheet, not the rule's.

    WHAT IS WRITTEN, per rule in dependency order:
      * an overridden line: left alone (reported).
      * 2+ lines on the target item: left alone and reported (Q9). Not summed, not picked.
      * no line: created from the catalogue's current wording (Z1) if the result is above
        0; nothing if it is 0 (D1).
      * one line: only boq_quantity changes, and only if it differs.
      * one line whose result is 0 (D1): removed, unless remove_zero is False (the reviewer
        path, Q4: boq_correct never deletes a line, so it is set to 0 and the designer's
        next save removes it), or the line has an ordered quantity or an order line (kept
        at 0; neither is reachable on a RESCO BOQ today, pre-flight P5).
    Every line that changes gets one BOQRecalculation, all written in one INSERT.
    """
    summary = FormulaSummary()
    if not prepared.active:
        return summary

    # Lazy import: models.py imports this module, so importing models at the top would be
    # circular.
    from .models import BOQItem, BOQItemMaster, BOQRecalculation, VendorOrderLine

    rules = prepared.rules
    codes = rules.codes

    # One read of the whole sheet, each line with its override joined in, after the
    # caller's writes and under the caller's BOQ lock: this is the sheet the computed lines
    # must follow.
    lines = list(boq.items.select_related('formula_override').order_by('pk'))
    quantities, lines_of = {}, {}
    for line in lines:
        if line.item_master_id is None:
            continue
        lines_of.setdefault(line.item_master_id, []).append(line)
        if line.boq_quantity is not None:
            quantities[line.item_master_id] = (quantities.get(line.item_master_id, ZERO)
                                               + line.boq_quantity)

    overrides = {}
    for target in prepared.targets:
        own = lines_of.get(target, [])
        if len(own) == 1 and _override_of(own[0]) is not None:
            overrides[target] = own[0].boq_quantity if own[0].boq_quantity is not None else ZERO

    try:
        results = evaluate(quantities, rules, overrides)
    except FormulaError as exc:
        # A result the BOQ column cannot hold (or a loop, which load_rules never returns).
        # As a ValidationError the picker refuses the save with the reason, the same way it
        # refuses a bad quantity box; on the other paths the transaction rolls back.
        raise ValidationError(str(exc))
    summary.results = results

    records, to_create = [], []

    def record(result, line, before, after, description, unit):
        records.append(BOQRecalculation(
            boq=boq, line=line, item_code=codes.get(result.item_id, ''),
            item_description=description, item_unit=unit, rule_id=result.rule_id,
            quantity_before=before, quantity_after=after, source=source,
            explanation=result.explanation, actor=actor,
            driving_correction=driving_correction))
        return FormulaChange(code=codes.get(result.item_id, ''), before=before, after=after,
                             unit=unit)

    for result in results.values():      # evaluation (dependency) order
        code = codes.get(result.item_id, '')
        own = lines_of.get(result.item_id, [])
        if len(own) > 1:
            summary.skipped_multiple.append(code)
            continue
        if result.overridden:
            summary.skipped_overridden.append(code)
            continue
        line = own[0] if own else None
        new = result.computed

        if line is None:
            if new > 0:
                to_create.append(result)
            continue

        before = line.boq_quantity
        if new == 0:
            # P5 blockers. The order-line query runs only for a line about to fall to 0.
            blocked = (line.ordered_quantity is not None
                       or VendorOrderLine.objects.filter(boq_item=line).exists())
            if remove_zero and not blocked:
                # Recorded first, with no line: the record survives the line it describes.
                summary.removed.append(record(result, None, before, None,
                                              line.description, line.uom))
                line.delete()
                continue
            if before is not None and before == 0:
                continue
            line.boq_quantity = new
            line.save(update_fields=['boq_quantity'])
            summary.kept_at_zero.append(record(result, line, before, new,
                                               line.description, line.uom))
            continue

        if before is not None and before == new:
            continue
        line.boq_quantity = new
        line.save(update_fields=['boq_quantity'])
        summary.changed.append(record(result, line, before, new, line.description, line.uom))

    if to_create:
        # The catalogue rows for the new lines: one query, only when a line is added.
        masters = BOQItemMaster.objects.in_bulk([r.item_id for r in to_create])
        for result in to_create:
            master = masters[result.item_id]
            # THE PICKER'S CREATE, FIELD FOR FIELD (Z1): the catalogue's current wording.
            line = BOQItem.objects.create(
                boq=boq, item_master=master, serial_no=master.sort_order,
                category=master.category, description=master.description,
                uom=master.unit, boq_quantity=result.computed, is_standard_item=True)
            summary.added.append(record(result, line, None, result.computed,
                                        master.description, master.unit))

    if records:
        BOQRecalculation.objects.bulk_create(records)
    return summary


def formula_summary_messages(summary):
    """(info lines, warning lines) describing a save's FormulaSummary. Pure."""
    def qty(value):
        return 'blank' if value is None else _num(value)

    info = []
    if summary.added:
        info.append('Calculated lines added: ' + ', '.join(
            f'{c.code} {qty(c.after)} {c.unit}'.rstrip() for c in summary.added) + '.')
    if summary.changed:
        info.append('Calculated lines updated: ' + ', '.join(
            f'{c.code} {qty(c.before)} → {qty(c.after)}' for c in summary.changed) + '.')
    if summary.removed:
        info.append('Calculated lines removed because they now work out to 0: '
                    + ', '.join(c.code for c in summary.removed) + '.')
    if summary.kept_at_zero:
        info.append('Calculated lines set to 0: '
                    + ', '.join(c.code for c in summary.kept_at_zero) + '.')
    warnings = [
        f'{code} is on this BOQ more than once, so its calculated quantity was not '
        f'written. Remove the extra line.'
        for code in summary.skipped_multiple]
    return info, warnings
