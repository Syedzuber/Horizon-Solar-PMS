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
queries however many rules exist. NOTHING CALLS THIS YET: the BOQ write paths are hooked
in B-2b, and Praveen's real rules are loaded in B-2c. On the day this ships there are no
rule rows at all.

THIS MODULE IMPORTS NO MODELS AT MODULE LEVEL. models.py imports the rounding constants and
validate_formula_rules() from here, so the two functions that read the database import
models inside themselves.
"""
from dataclasses import dataclass
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

def _load_specs(include_retired_targets):
    """(specs, codes, units, types) from the rule tables in three queries, always three.

    include_retired_targets=False is load_rules(): a rule whose target item is retired is
    skipped (Q8). Retired items still count as drivers and group members, because a retired
    inverter on a BOQ still needs fasteners. Validation passes True, so a loop through a
    retired target is refused too: it would return the day the item is reactivated.
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


def load_rules():
    """The active rule set, ordered for evaluate(). Three queries whatever the rule count."""
    specs, codes, units, _types = _load_specs(include_retired_targets=False)
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
