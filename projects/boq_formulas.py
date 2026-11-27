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

# ---------------------------------------------------------------------------
# DW-6 (ruling DW-R12, INC 2P) — the three things a rule could not say before, each a plain
# field on the rule, never an expression:
#
#   * a PER-SITE FACTOR (site_factor). 'module_wp' multiplies by the site's module wattage
#     (site_module_wp(), DW-R14), after the multiplier and before the divisor.
#   * CLAMP AT 0 (clamp_at_zero). A result below 0 is written as 0 with a "below 0"
#     warning, instead of the refusal every other rule keeps.
#   * A NAMED CHECK (warning_check). 'inc_2p_structures' compares the rule's driver sum with
#     the modules its CROSS-CHECK LINES hold (drivers whose role is 'check', each weighted
#     by the modules one structure holds) and warns when they differ.
#
# WARNINGS ARE COMPUTED, NEVER STORED, and never refuse anything: they ride on each Result
# and are worded when shown (warning_text()), so a design-group snapshot can store the
# facts and word them later.
# ---------------------------------------------------------------------------

#: BOQFormulaRule.site_factor: '' = none (every rule before DW-6).
SITE_FACTOR_MODULE_WP = 'module_wp'
SITE_FACTOR_CHOICES = [
    ('',                    'None'),
    (SITE_FACTOR_MODULE_WP, "The site's module wattage (Wp)"),
]

#: BOQFormulaRule.warning_check: '' = none.
CHECK_INC_2P = 'inc_2p_structures'
WARNING_CHECK_CHOICES = [
    ('',           'None'),
    (CHECK_INC_2P, 'INC 2P modules against the 2P structure lines'),
]

#: BOQFormulaDriver.role: 'sum' is summed into the rule (every driver before DW-6); 'check'
#: is read only by the rule's named check, never summed.
DRIVER_ROLE_SUM = 'sum'
DRIVER_ROLE_CHECK = 'check'
DRIVER_ROLE_CHOICES = [
    (DRIVER_ROLE_SUM,   'Summed into the rule'),
    (DRIVER_ROLE_CHECK, 'Cross-check line only'),
]

#: RuleWarning.kind
WARNING_BELOW_ZERO = 'below_zero'          # clamp_at_zero: the rule worked out below 0
WARNING_CROSS_CHECK = 'cross_check'        # the check lines hold a different number
WARNING_NO_STRUCTURES = 'no_structures'    # the check lines hold nothing (Zuber, DW-6 answer 1)

#: The model default of DesignSettings.default_module_wp, used when the row does not exist
#: (DesignSettings.current() returns an unsaved row holding it). Written out because this
#: module imports no models; tests_dw6_inc2p pins it equal to the model field's default.
DEFAULT_MODULE_WP_FALLBACK = 640

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
    # DW-6. The defaults are what every rule before DW-6 is, so its result is unchanged.
    site_factor: str = ''                      # '' or SITE_FACTOR_MODULE_WP
    clamp_at_zero: bool = False                # below 0 -> 0 with a warning, instead of refusing
    warning_check: str = ''                    # '' or CHECK_INC_2P
    check_drivers: tuple = ()                  # DriverSpecs with role 'check': read by the check, never summed
    target_code: str = ''                      # For the warnings, which are worded from facts alone

    @property
    def raises_warnings(self):
        """True if this rule can raise a warning (DW-6): only these need read_warning_rules()."""
        return self.clamp_at_zero or bool(self.warning_check)

    def dependency_ids(self):
        """Every item this rule reads: item drivers, group members, the gate group and (DW-6)
        the cross-check lines, so ordering and loop checks see everything it reads."""
        ids = set()
        for driver in self.drivers + self.check_drivers:
            if driver.item_id is not None:
                ids.add(driver.item_id)
            ids.update(driver.member_ids)
        if self.requires_member_ids:
            ids.update(self.requires_member_ids)
        return ids


@dataclass(frozen=True)
class RuleSet:
    """Rules in evaluation (dependency) order, plus each referenced item's code and unit.

    DW-6: `module_wp` maps every MODULE item the rules reference (in practice the "all
    modules" group, which the Head's catalogue form keeps complete) to its wattage, None for
    one that records none (OPX-001). `default_module_wp` is the Head's default (DW-R14).
    Both are read only by a rule whose site_factor is 'module_wp'."""
    rules: tuple
    codes: Mapping
    units: Mapping
    module_wp: Mapping = field(default_factory=dict)
    default_module_wp: int = DEFAULT_MODULE_WP_FALLBACK


@dataclass(frozen=True)
class RuleWarning:
    """One warning a rule raised on one sheet: facts only, worded by warning_text().

    `modules` is the rule's driver sum (for INC 2P: the formula's INC 2P module count);
    `structure_modules` is what the cross-check lines hold (None for a below-0 warning)."""
    kind: str                      # WARNING_BELOW_ZERO, WARNING_CROSS_CHECK or WARNING_NO_STRUCTURES
    code: str                      # The target item's code, e.g. OPX-203
    check: str                     # The rule's warning_check ('' for a plain clamp)
    modules: Decimal
    structure_modules: Decimal | None = None


def _signed(value):
    """'−8' (a true minus sign) for a negative number, as I3's wording writes it."""
    text = _num(value)
    return '−' + text[1:] if text.startswith('-') else text


def warning_text(kind, code, check, modules, structure_modules=None):
    """The sentence for one warning (rulings DW-R12 (a)/(b), I3, I4, DW-6 answer 1). Pure.

    Takes the facts rather than a RuleWarning so a design-group snapshot, which stores them
    as plain values, is worded by exactly this function."""
    if check == CHECK_INC_2P:
        if kind == WARNING_BELOW_ZERO:
            return (f'INC 2P works out below 0 ({_signed(modules)} modules); set to 0. Check '
                    f'the 3P, Other, Ballast and Tin Shade lines.')
        if kind == WARNING_NO_STRUCTURES:
            return (f'This BOQ has no 2P structure lines; INC 2P counts {_signed(modules)} '
                    f'modules.')
        if kind == WARNING_CROSS_CHECK:
            return (f'INC 2P counts {_signed(modules)} modules; the 2P structure lines hold '
                    f'{_signed(structure_modules)}.')
    if kind == WARNING_BELOW_ZERO:
        return f'{code} works out below 0 ({_signed(modules)}); set to 0.'
    # A kind this code does not know (a snapshot taken under a later rule): shown, not dropped.
    return f'{code}: {kind}'


def rule_warning_text(warning):
    """warning_text() for a RuleWarning."""
    return warning_text(warning.kind, warning.code, warning.check, warning.modules,
                        warning.structure_modules)


def _driver_sum(drivers, values):
    """Sum of weight x quantity over `drivers` against the working values. Pure."""
    total = ZERO
    for driver in drivers:
        if driver.item_id is not None:
            quantity = values.get(driver.item_id, ZERO)
        else:
            quantity = sum((values.get(m, ZERO) for m in driver.member_ids), ZERO)
        total += driver.weight * quantity
    return total


def rule_warnings(rule, values):
    """The warnings `rule` raises on a sheet, as RuleWarnings. Pure: no query.

    THE ONE PLACE A WARNING IS DECIDED. evaluate() calls it for each rule as it works the
    rule out, and sheet_warnings() calls it for the BOQ view and the design groups, which
    read the rules through read_warning_rules() instead of load_rules(). Both give the same
    answer because a rule that raises warnings may read only hand-entered lines (refused
    otherwise by validate_formula_rules()), so `values` are the sheet's own quantities on
    either path.

    Depends on the drivers only, never on an override of the target (DW-R12 (d): an
    override suppresses neither warning) and never on the wattage (a wattage is above 0, so
    the sign of the result is the sign of the driver sum).

      * below 0 (clamp_at_zero): the driver sum is below 0.
      * inc_2p_structures: the driver sum differs from what the check lines hold. Nothing
        when both are 0. When the check lines hold nothing, the "no 2P structure lines"
        wording (Zuber, DW-6 answer 1).
    """
    if not rule.raises_warnings:
        return ()
    with localcontext() as ctx:
        ctx.prec = _PRECISION
        modules = _driver_sum(rule.drivers, values)
        warnings = []
        if rule.clamp_at_zero and modules < 0:
            warnings.append(RuleWarning(WARNING_BELOW_ZERO, rule.target_code,
                                        rule.warning_check, modules))
        if rule.warning_check == CHECK_INC_2P:
            held = _driver_sum(rule.check_drivers, values)
            if modules != held:
                kind = WARNING_NO_STRUCTURES if held == 0 else WARNING_CROSS_CHECK
                warnings.append(RuleWarning(kind, rule.target_code, rule.warning_check,
                                            modules, held))
    return tuple(warnings)


def site_module_wp(values, module_wp, default):
    """(wattage, how) — the site's module wattage for a 'module_wp' rule (DW-R14, I1). Pure.

    The wattages of the site's module lines with a quantity above 0, an OPX-001 line (no
    wattage recorded) counting as the default. All the same: that wattage. Otherwise, or
    with no module line at all: the default. `how` is what the explanation says:

        'site'          one wattage            "640 Wp is the site's module wattage"
        'not_recorded'  only OPX-001 lines     "... the default wattage, because the site's
                                                module lines record no wattage"
        'mixed'         two or more            "... the default wattage, because the site's
                                                modules are mixed (550 Wp, 640 Wp)"
        'none'          no module line         "... the default wattage; the site has no
                                                module lines"
    Returns a third element: the wattages found (sorted, None for "not recorded"), for the
    'mixed' wording.
    """
    found = {module_wp[item_id] for item_id in module_wp
             if values.get(item_id, ZERO) > 0}
    if not found:
        return default, 'none', ()
    effective = {default if wp is None else wp for wp in found}
    listed = tuple(sorted(found, key=lambda wp: (wp is None, wp or 0)))
    if len(effective) > 1:
        return default, 'mixed', listed
    if found == {None}:
        return default, 'not_recorded', listed
    return effective.pop(), 'site', listed


def _wattage_words(wp, how, listed):
    """The explanation's closing words about the wattage it used (DW-R12 (c))."""
    if how == 'site':
        return f"{wp} Wp is the site's module wattage"
    if how == 'mixed':
        names = ', '.join('wattage not recorded' if w is None else f'{w} Wp' for w in listed)
        return (f"{wp} Wp is the default wattage, because the site's modules are mixed "
                f"({names})")
    if how == 'not_recorded':
        return (f"{wp} Wp is the default wattage, because the site's module lines record no "
                f"wattage")
    return f'{wp} Wp is the default wattage; the site has no module lines'


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
    warnings: tuple = ()      # DW-6: RuleWarnings (rule_warnings()); () for every rule that raises none

    @property
    def warning_texts(self):
        """The warnings as sentences (warning_text()), in the order raised."""
        return [rule_warning_text(w) for w in self.warnings]


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------

def build_rule_set(specs, codes, units, module_wp=None,
                   default_module_wp=DEFAULT_MODULE_WP_FALLBACK):
    """Order `specs` so every computed driver is evaluated before the rules that read it.

    DW-6: `module_wp` and `default_module_wp` are carried onto the RuleSet unchanged (see
    RuleSet); a caller with no module rule leaves them out.

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
    return RuleSet(rules=tuple(ordered), codes=dict(codes), units=dict(units),
                   module_wp=dict(module_wp or {}), default_module_wp=default_module_wp)


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
    """(quantity, text) for one driver, e.g. (3, '3 lightning arresters (2 X + 1 Y)').

    DW-6: a NEGATIVE weight's text carries no sign ('12 Tin Shade', or '2 × 12 Tin Shade'
    for -2); _join_terms() writes it with a minus between terms. No rule before DW-6 has a
    negative weight, so their text is unchanged."""
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
    if driver.weight < 0:
        if driver.weight != -ONE:
            text = f'{_num(-driver.weight)} × {text}'
    elif driver.weight != ONE:
        text = f'{_num(driver.weight)} × {text}'
    return quantity, text


def _join_terms(drivers, terms):
    """'a + b − c': each term after the first with a plus, or a minus for a negative weight
    (DW-6). With only positive weights this is exactly ' + '.join(terms), as before DW-6."""
    out = ''
    for n, (driver, text) in enumerate(zip(drivers, terms)):
        if n == 0:
            out = ('− ' + text) if driver.weight < 0 else text
        else:
            out += (' − ' if driver.weight < 0 else ' + ') + text
    return out


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
                                                          set(results), rules)
        # DW-6: decided from the drivers alone (rule_warnings()), so an override of this
        # item suppresses neither warning (DW-R12 (d)).
        warnings = rule_warnings(rule, values)

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
            overridden=overridden, warnings=warnings)
        values[rule.target_id] = quantity

    return results


def _compute(rule, values, codes, unit, computed_ids, rules=None):
    """(quantity, raw, driver_sum, explanation) for one rule against the working values.

    `computed_ids` are the items already worked out by earlier rules in this evaluation;
    used only to say so in the explanation. `rules` (the RuleSet) is read only for a
    'module_wp' rule's wattages (DW-6)."""
    summary = rule.summary
    wattage = None      # DW-6: (wp, how, listed) for a 'module_wp' rule
    clamped = False     # DW-6: a clamp_at_zero rule that worked out below 0

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
        if rule.site_factor == SITE_FACTOR_MODULE_WP:
            # DW-6 (DW-R12, I1): the site's module wattage, multiplied in before the
            # divisor like the multiplier, so 52 modules x 640 / 1000 stays exact.
            wattage = site_module_wp(values, rules.module_wp if rules else {},
                                     rules.default_module_wp if rules
                                     else DEFAULT_MODULE_WP_FALLBACK)
            raw = driver_sum * rule.multiplier * wattage[0] / rule.divisor + rule.constant
        elif rule.site_factor:
            raise FormulaError(f'Unknown site factor {rule.site_factor!r} on the rule for '
                               f'{codes.get(rule.target_id, rule.target_id)}.')
        else:
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

        # DW-6 (DW-R12 (a)): a clamped rule below 0 is written as 0, and rule_warnings()
        # raises the "below 0" warning; every other rule below 0 is still refused below.
        if rule.clamp_at_zero and raw < 0:
            clamped = True
            quantity = ZERO

    quantity = quantity.quantize(CENT)
    if quantity < 0 or quantity > QUANTITY_MAX:
        raise FormulaError(
            f'The rule for {codes.get(rule.target_id, rule.target_id)} gives '
            f'{_num(quantity)}, outside what a BOQ line can hold (0 to {QUANTITY_MAX}).')

    # The arithmetic, in the order it was done, then what rounding and the floor did.
    if not rule.drivers:
        expression = None
    else:
        joined = _join_terms(rule.drivers, terms)
        expression = joined if len(terms) == 1 else '(' + joined + ')'
        if rule.multiplier != ONE:
            expression += f' × {_num(rule.multiplier)}'
        if wattage is not None:
            expression += f' × {wattage[0]} Wp'
        if rule.divisor != ONE:
            expression += f' ÷ {_num(rule.divisor)}'
        if rule.constant:
            expression += f' + {_num(rule.constant)}'

    if expression is None:
        explanation = f'{summary}: a fixed {_num(quantity)} {unit}'
    elif clamped:
        explanation = (f'{summary}: {expression} = {_signed_raw(raw)}, below 0, so set to '
                       f'0 {unit}')
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
    if wattage is not None:
        # DW-R12 (c): which wattage was used, and why ("mixed" when it is).
        explanation = explanation.rstrip() + '; ' + _wattage_words(*wattage)
    return quantity, raw, driver_sum, explanation.rstrip()


def _signed_raw(raw):
    """_raw_text() with a true minus sign: '−8', '−0.3333…'."""
    text = _raw_text(raw)
    return '−' + text[1:] if text.startswith('-') else text


# ---------------------------------------------------------------------------
# Loading and save-time validation (the only functions that touch the database)
# ---------------------------------------------------------------------------

def _load_specs(include_retired_targets, project_type=None):
    """(specs, codes, units, types, module_wp, default_module_wp) from the rule tables in
    three queries, always three.

    DW-6: the Head's default module wattage rides on query 1 as a subquery, so a 'module_wp'
    rule costs no query of its own; each referenced module item's wattage comes with the
    item rows already joined. A driver whose role is 'check' goes into the rule's
    check_drivers, never its sum.

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
    from django.db.models import Q, Subquery
    from .models import BOQFormulaDriver, BOQFormulaRule, BOQItemGroupMember, DesignSettings

    rule_q = Q(is_active=True)
    driver_q = Q(rule__is_active=True)
    if not include_retired_targets:
        rule_q &= Q(target__is_active=True)
        driver_q &= Q(rule__target__is_active=True)
    if project_type is not None:
        rule_q &= Q(target__project_type=project_type)
        driver_q &= Q(rule__target__project_type=project_type)

    # 1. Active rules with their target (code, unit, type) and gate group label.
    #    DW-6: with the default module wattage (DesignSettings' one row) as a subquery.
    rules = list(BOQFormulaRule.objects.filter(rule_q)
                 .select_related('target', 'requires_group')
                 .annotate(default_module_wp=Subquery(
                     DesignSettings.objects.filter(pk=1).values('default_module_wp')[:1]))
                 .order_by('pk'))
    # 2. Those rules' drivers, filtered by the same terms through the join rather than by a
    #    list of rule ids, so the query count does not depend on how many rules exist.
    drivers = list(BOQFormulaDriver.objects.filter(driver_q)
                   .select_related('item', 'group').order_by('pk'))
    # 3. Every group member. The group table is a handful of named lists (five in Praveen's
    #    rules), so reading all of it is cheaper than a filtered join and keeps this at 3.
    members = list(BOQItemGroupMember.objects.select_related('item')
                   .order_by('item__code', 'pk'))

    codes, units, types, module_wp = {}, {}, {}, {}

    def note(item):
        codes[item.pk], units[item.pk], types[item.pk] = item.code, item.unit, item.project_type
        if item.is_module:
            module_wp[item.pk] = item.module_wp      # None for OPX-001 (no wattage recorded)

    members_of = {}
    for member in members:
        members_of.setdefault(member.group_id, []).append(member.item_id)
        note(member.item)

    drivers_of, checks_of = {}, {}
    for driver in drivers:
        if driver.item_id is not None:
            note(driver.item)
            spec = DriverSpec(weight=driver.weight, label=driver.label or driver.item.code,
                              item_id=driver.item_id)
        else:
            spec = DriverSpec(weight=driver.weight, label=driver.label or driver.group.label,
                              group_id=driver.group_id,
                              member_ids=tuple(members_of.get(driver.group_id, ())))
        bucket = checks_of if driver.role == DRIVER_ROLE_CHECK else drivers_of
        bucket.setdefault(driver.rule_id, []).append(spec)

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
            site_factor=rule.site_factor, clamp_at_zero=rule.clamp_at_zero,
            warning_check=rule.warning_check,
            check_drivers=tuple(checks_of.get(rule.pk, ())),
            target_code=rule.target.code,
        ))
    # Every rule row carries the same subquery value; None when the settings row does not
    # exist yet, which DesignSettings.current() reads as the model default.
    default = next((r.default_module_wp for r in rules if r.default_module_wp), None)
    return (specs, codes, units, types, module_wp,
            default or DEFAULT_MODULE_WP_FALLBACK)


def load_rules(project_type=None):
    """The active rule set, ordered for evaluate(). Three queries whatever the rule count.

    project_type='OPEX' keeps only RESCO rules (B-2b Q3). The write paths always pass the
    site's type: without it a Residential constant rule would put a Residential catalogue
    line on every RESCO BOQ it saved. None (the default) is every type, as before.
    """
    specs, codes, units, _types, module_wp, default = _load_specs(
        include_retired_targets=False, project_type=project_type)
    return build_rule_set(specs, codes, units, module_wp, default)


def validate_formula_rules():
    """Raise ValidationError if the stored rules loop or mix project types.

    Called by the save() of BOQFormulaRule, BOQFormulaDriver and BOQItemGroupMember AFTER
    the row is written and inside the same transaction, so a refused row is rolled back.
    Checking the stored whole rather than the one row is what catches a loop closed by a
    group member: adding an item to "all inverters" can close a loop through any rule that
    reads that group.
    """
    specs, codes, units, types, _module_wp, _default = _load_specs(
        include_retired_targets=True)
    try:
        build_rule_set(specs, codes, units)
    except FormulaError as exc:
        raise ValidationError(str(exc))

    # DW-6: a rule that raises warnings reads only hand-entered lines. The BOQ view and the
    # design groups decide its warnings from the stored quantities (sheet_warnings()), not
    # from a full evaluation; a computed driver would make them disagree with the picker.
    targets = {spec.target_id for spec in specs}
    for spec in specs:
        if spec.raises_warnings and spec.dependency_ids() & targets:
            computed = sorted(codes[i] for i in spec.dependency_ids() & targets)
            raise ValidationError(
                f'The rule for {codes[spec.target_id]} raises warnings, so it may read only '
                f'lines entered by hand; {", ".join(computed)} '
                f'{"is" if len(computed) == 1 else "are"} calculated.')

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
# reviewer's add_line and set_quantity; since B-2c also fix_tin_shade_ballast
# --apply-counts (source 'tin_shade_fix'). NOT the ACCB endpoint. Each path calls
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


def read_formula_rules(project_type):
    """The rules for a READ (a screen or the upload preview): no lock. One query with no
    rules, four with any number.

    The write paths use prepare_formula_save(), which also locks the BOQ row; a page load
    must not take a row lock, so the screens come here instead."""
    if not formula_rules_exist(project_type):
        return FormulaSave()
    return FormulaSave(rules=load_rules(project_type))


def _override_of(line):
    """The line's BOQLineOverride or None, with no query (select_related loaded it)."""
    from django.core.exceptions import ObjectDoesNotExist
    try:
        return line.formula_override
    except ObjectDoesNotExist:
        return None


# ---------------------------------------------------------------------------
# B-2b.1 — the sheet as the engine sees it, and what the engine would write.
#
# ONE INPUT BUILDER AND ONE WRITE DECISION, shared by the save (apply_formulas), the
# upload preview (which predicts the commit) and the screens (which explain the lines).
# Three copies of "which lines count, which are overridden, which are removed at 0" is how
# a preview would come to promise something the save does not do.
# ---------------------------------------------------------------------------

@dataclass
class SheetLine:
    """One BOQ line as the engine reads it. Not frozen: the upload preview edits copies to
    stand for the sheet as it will be after the commit."""
    pk: int | None                        # None for a line the upload preview would create
    item_master_id: int | None
    boq_quantity: Decimal | None
    overridden: bool = False
    override_note: str = ''
    override_label: str = ''              # B-2e: "Overridden by QC (name)", for the screens
    removed_label: str = ''               # DW-3: "Removed (0) by QC (name)" when overridden to 0
    ordered_quantity: Decimal | None = None
    line: object = None                   # The BOQItem, for a stored line


def override_label(override):
    """B-2e (Zuber Q6): what every override marker says about who set it.

        "Overridden by QC (Asha Rao)", "... by the Head (...)", "... by the designer (...)",
        or "Overridden (name not recorded)" when the profile has gone (set_by is SET_NULL).

    `set_as` is the capacity stamped at save time, never worked out from today's flags. The
    override's set_by and user must already be joined in, so this costs no query."""
    return _override_marker('Overridden', override)


def _override_marker(word, override):
    """The text "<word> by <who> (<name>)", the B-2e wording shared by override_label() and
    removed_label(). No query: set_by and its user are already joined in."""
    # Lazy import: models.py imports this module, so importing models at the top would be
    # circular.
    from .models import OVERRIDE_SET_AS_CHOICES, OVERRIDE_SET_AS_DESIGNER
    if override.set_by is None:
        return f'{word} (name not recorded)'
    user = override.set_by.user
    who = dict(OVERRIDE_SET_AS_CHOICES).get(override.set_as or OVERRIDE_SET_AS_DESIGNER,
                                            'the designer')
    return f'{word} by {who} ({user.get_full_name() or user.username})'


#: DW-3 (ruling DW-R4) — the word a removed calculated line is marked with, ahead of who
#: set it: "Removed (0) by QC (Asha Rao)". The site card shows this word alone (answer 10).
REMOVED_MARKER = 'Removed (0)'


def is_removed(overridden, quantity):
    """DW-3: True if a calculated line is REMOVED — overridden to exactly 0.

    REMOVE IS NOT A STATE OF ITS OWN (ruling DW-R4, R2): it is an override whose figure is
    0, written through the one override path, so this is the only definition and every
    screen asks it. A blank figure is not 0, and an override to any other figure keeps the
    "Overridden" marker. Pure."""
    return bool(overridden) and quantity is not None and quantity == 0


def removed_label(override):
    """DW-3 (R4): "Removed (0) by the designer (name)", "... by QC (...)", "... by the Head
    (...)", the same "who" as override_label(). No query."""
    return _override_marker(REMOVED_MARKER, override)


def sheet_line(line):
    """A stored BOQItem as a SheetLine. Its override, with who set it, must already be
    joined in (read_sheet()'s select_related), so this costs no query."""
    override = _override_of(line)
    removed = override is not None and is_removed(True, line.boq_quantity)
    return SheetLine(pk=line.pk, item_master_id=line.item_master_id,
                     boq_quantity=line.boq_quantity, overridden=override is not None,
                     override_note=override.note if override is not None else '',
                     override_label=override_label(override) if override is not None else '',
                     removed_label=removed_label(override) if removed else '',
                     ordered_quantity=line.ordered_quantity, line=line)


def read_sheet(boq):
    """Every line on `boq` as SheetLines, overrides joined in. One query; none for no BOQ.

    B-2e: who set each override (set_by and its user) is joined in the same query, so the
    three screens can name them (override_label) for no extra query."""
    if boq is None:
        return []
    return [sheet_line(line)
            for line in boq.items.select_related('formula_override__set_by__user')
                                 .order_by('pk')]


def sheet_inputs(sheet, targets):
    """(quantities, lines_of, overrides): evaluate()'s input from a sheet. Pure.

    Every line with a catalogue item counts, ACCB lines included, summed per item; a blank
    quantity counts as nothing; lines with no item are left out.

    OVERRIDES, two kinds, both "use the figure on the sheet, not the rule's":
      * a computed item on ONE line with a BOQLineOverride: that line's quantity.
      * A3 (Zuber, B-2b.1) — a computed item on TWO OR MORE lines: the sheet's total for it.
        Those lines are not written (Q9), so what is on the BOQ is what later rules are
        sized from, exactly as with an override. Without this, evaluate() would put the
        rule's own figure in front of the later rules, a number that is on no line.
    """
    quantities, lines_of = {}, {}
    for line in sheet:
        if line.item_master_id is None:
            continue
        lines_of.setdefault(line.item_master_id, []).append(line)
        if line.boq_quantity is not None:
            quantities[line.item_master_id] = (quantities.get(line.item_master_id, ZERO)
                                               + line.boq_quantity)
    overrides = {}
    for target in targets:
        own = lines_of.get(target, [])
        if len(own) > 1:
            overrides[target] = quantities.get(target, ZERO)
        elif len(own) == 1 and own[0].overridden:
            overrides[target] = own[0].boq_quantity if own[0].boq_quantity is not None else ZERO
    return quantities, lines_of, overrides


#: What the engine does to one computed item (FormulaAction.kind).
ACTION_ADD = 'add'                      # no line, result above 0: create it
ACTION_CHANGE = 'change'                # one line, a different figure: set it
ACTION_REMOVE = 'remove'                # one line, result 0: remove it (D1)
ACTION_ZERO = 'zero'                    # one line, result 0, kept: set it to 0
ACTION_SKIP_MULTIPLE = 'skip_multiple'  # 2+ lines: not written (Q9)
ACTION_SKIP_OVERRIDE = 'skip_override'  # overridden: left alone


@dataclass(frozen=True)
class FormulaAction:
    kind: str
    result: Result
    line: SheetLine | None = None


def formula_line_blocked(line):
    """True if a line that works out to 0 must be kept at 0 rather than removed (P5): it has
    an ordered quantity or an order line. The order-line query runs only for a stored line,
    and only when it is about to fall to 0."""
    if line.ordered_quantity is not None:
        return True
    if line.pk is None:
        return False
    # Lazy import: models.py imports this module, so importing models at the top would be
    # circular.
    from .models import VendorOrderLine
    return VendorOrderLine.objects.filter(boq_item_id=line.pk).exists()


def work_out_formulas(sheet, prepared, *, remove_zero=True):
    """(results, actions) for one sheet: evaluate() and then what a save would write.

    THE ONE PLACE THE WRITE RULES LIVE. apply_formulas() carries the actions out; the upload
    preview reports them for a sheet that is not saved yet. Raises FormulaError when a rule
    cannot produce its figure (too large for the column, or a loop).

      * 2+ lines on the target item: not written (Q9), and its sheet total feeds later
        rules (A3, in sheet_inputs()).
      * an overridden line: left alone.
      * no line: created if the result is above 0; nothing at 0 (D1).
      * one line: changed only if the figure differs.
      * one line at 0 (D1): removed, unless remove_zero is False (the reviewer path, Q4) or
        formula_line_blocked() says it must be kept, in which case it is set to 0.
    """
    quantities, lines_of, overrides = sheet_inputs(sheet, prepared.targets)
    results = evaluate(quantities, prepared.rules, overrides)
    actions = []
    for result in results.values():      # evaluation (dependency) order
        own = lines_of.get(result.item_id, [])
        if len(own) > 1:
            actions.append(FormulaAction(ACTION_SKIP_MULTIPLE, result))
            continue
        if result.overridden:
            actions.append(FormulaAction(ACTION_SKIP_OVERRIDE, result, own[0]))
            continue
        line = own[0] if own else None
        new = result.computed
        if line is None:
            if new > 0:
                actions.append(FormulaAction(ACTION_ADD, result))
            continue
        before = line.boq_quantity
        if new == 0:
            if remove_zero and not formula_line_blocked(line):
                actions.append(FormulaAction(ACTION_REMOVE, result, line))
            elif before is None or before != 0:
                actions.append(FormulaAction(ACTION_ZERO, result, line))
            continue
        if before is None or before != new:
            actions.append(FormulaAction(ACTION_CHANGE, result, line))
    return results, actions


def apply_formulas(boq, prepared, *, source, actor, driving_correction=None,
                   remove_zero=True):
    """Bring every computed line on `boq` up to date. Returns a FormulaSummary.

    Runs inside the caller's transaction, after the caller's own writes, with the rules and
    the lock from prepare_formula_save(). With no rules it returns at once: no query, no
    write.

    THE INPUT is sheet_inputs() and WHAT IS WRITTEN is work_out_formulas(): both shared with
    the upload preview, so the preview can only promise what this does. In short: an
    overridden line and an item on 2+ lines are left alone; a missing line is created above
    0; one line follows its rule; a line at 0 is removed (D1) unless remove_zero is False
    (the reviewer path, Q4: boq_correct never deletes a line, so it is set to 0 and the
    designer's next save removes it) or it has an ordered quantity or an order line.
    Every line that changes gets one BOQRecalculation, all written in one INSERT.

    Raises ValidationError with the engine's reason when a rule cannot produce its figure;
    every caller refuses the save with it and its transaction rolls back (A4).
    """
    summary = FormulaSummary()
    if not prepared.active:
        return summary

    # Lazy import: models.py imports this module, so importing models at the top would be
    # circular.
    from .models import BOQItem, BOQItemMaster, BOQRecalculation

    codes = prepared.rules.codes

    # One read of the whole sheet, each line with its override joined in, after the
    # caller's writes and under the caller's BOQ lock: this is the sheet the computed lines
    # must follow.
    sheet = read_sheet(boq)
    try:
        results, actions = work_out_formulas(sheet, prepared, remove_zero=remove_zero)
    except FormulaError as exc:
        # A result the BOQ column cannot hold (or a loop, which load_rules never returns).
        # As a ValidationError every hooked path refuses the save with the reason, the same
        # way the picker refuses a bad quantity box, and its transaction rolls back.
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

    for action in actions:               # evaluation (dependency) order
        result = action.result
        code = codes.get(result.item_id, '')
        if action.kind == ACTION_SKIP_MULTIPLE:
            summary.skipped_multiple.append(code)
            continue
        if action.kind == ACTION_SKIP_OVERRIDE:
            summary.skipped_overridden.append(code)
            continue
        if action.kind == ACTION_ADD:
            to_create.append(result)
            continue

        line = action.line.line           # the stored BOQItem
        before = line.boq_quantity
        if action.kind == ACTION_REMOVE:
            # Recorded first, with no line: the record survives the line it describes.
            summary.removed.append(record(result, None, before, None,
                                          line.description, line.uom))
            line.delete()
            continue
        new = result.computed
        line.boq_quantity = new
        line.save(update_fields=['boq_quantity'])
        bucket = summary.kept_at_zero if action.kind == ACTION_ZERO else summary.changed
        bucket.append(record(result, line, before, new, line.description, line.uom))

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
    warnings = [multiple_lines_message(code) for code in summary.skipped_multiple]
    return info, warnings


def multiple_lines_message(code):
    """Q9 (Zuber, B-2b.1): what a designer is told about a computed item on 2+ lines."""
    return (f'{code} is on this BOQ more than once, so it is not calculated: its lines are '
            f'ordinary lines. Remove the extra line; when one is left, it is calculated '
            f'again on the next save.')


# ---------------------------------------------------------------------------
# B-2b.1 — what the screens show about calculated lines (read only)
# ---------------------------------------------------------------------------

#: LineFormula.kind
LINE_CALCULATED = 'calculated'
LINE_OVERRIDDEN = 'overridden'
LINE_MULTIPLE = 'multiple'


@dataclass(frozen=True)
class LineFormula:
    """What a screen shows beside one line of a computed item."""
    kind: str                       # LINE_CALCULATED, LINE_OVERRIDDEN or LINE_MULTIPLE
    explanation: str = ''           # The rule in plain words; '' when it could not be worked out
    computed: Decimal | None = None  # The rule's figure; None when it could not be worked out
    stale: bool = False             # The stored figure is not what the next save writes (Q2)
    note: str = ''                  # The override's note
    override_by: str = ''           # B-2e: "Overridden by QC (name)" (override_label())
    removed_by: str = ''            # DW-3: "Removed (0) by QC (name)"; '' unless overridden to 0
    warnings: tuple = ()            # DW-6: the rule's warnings as sentences; shown on every kind of line

    @property
    def is_removed(self):
        """DW-3: an override to 0 (is_removed()). Still is_overridden: a removed line is an
        overridden one, so a screen asks this first and shows the Removed marker instead."""
        return bool(self.removed_by)

    @property
    def is_calculated(self):
        return self.kind == LINE_CALCULATED

    @property
    def is_overridden(self):
        return self.kind == LINE_OVERRIDDEN

    @property
    def is_multiple(self):
        return self.kind == LINE_MULTIPLE

    @property
    def stale_text(self):
        """Q2: the STORED figure is shown; this says what the next save will make of it."""
        if not self.stale or self.computed is None:
            return ''
        if self.computed == 0:
            return 'Works out to 0, so the next save removes this line.'
        return f'Recalculates to {_num(self.computed)} on the next save.'


@dataclass
class FormulaView:
    """The rules applied to one BOQ for display: nothing is written.

    `error` is the engine's reason when the rules cannot be worked out for this sheet (a
    figure too large for the column); the screens then mark the calculated lines without
    an explanation and show the reason once."""
    rules: RuleSet | None = None
    results: dict = field(default_factory=dict)
    lines_of: dict = field(default_factory=dict)     # item id -> [SheetLine]
    error: str = ''

    @property
    def active(self):
        return self.rules is not None

    @property
    def targets(self):
        if self.rules is None:
            return frozenset()
        return frozenset(rule.target_id for rule in self.rules.rules)

    def code(self, item_id):
        return self.rules.codes.get(item_id, '') if self.rules else ''

    def unit(self, item_id):
        return self.rules.units.get(item_id, '') if self.rules else ''

    def info_for(self, line):
        """LineFormula for a stored BOQItem, or None when its item is not computed."""
        if line.item_master_id not in self.targets:
            return None
        own = self.lines_of.get(line.item_master_id, [])
        result = self.results.get(line.item_master_id)
        # DW-6: the warnings come from the drivers, so they are shown on an overridden line
        # (DW-R12 (d)) and on an item on 2+ lines (Zuber, DW-6 answer 7) alike.
        warnings = tuple(result.warning_texts) if result else ()
        if len(own) > 1:
            return LineFormula(kind=LINE_MULTIPLE,
                               explanation=multiple_lines_message(self.code(line.item_master_id)),
                               warnings=warnings)
        stored = next((s for s in own if s.pk == line.pk), None)
        explanation = result.explanation if result else ''
        computed = result.computed if result else None
        if stored is not None and stored.overridden:
            return LineFormula(kind=LINE_OVERRIDDEN, explanation=explanation,
                               computed=computed, note=stored.override_note,
                               override_by=stored.override_label,
                               removed_by=stored.removed_label, warnings=warnings)
        return LineFormula(kind=LINE_CALCULATED, explanation=explanation, computed=computed,
                           stale=(computed is not None and line.boq_quantity != computed),
                           warnings=warnings)

    def absent(self):
        """[(item id, Result or None)] for computed items with no line, in evaluation order:
        the A1 block ("Calculated items not on this BOQ")."""
        if self.rules is None:
            return []
        return [(rule.target_id, self.results.get(rule.target_id))
                for rule in self.rules.rules if not self.lines_of.get(rule.target_id)]

    def absent_warnings(self):
        """DW-6: [(code, sentence)] for the warnings of calculated items with NO line (a
        clamped INC 2P on the designer's path, or a rule loaded since the last save), for a
        screen that has no "not on this BOQ" block of its own (the QC/Head panel). No
        query."""
        return [(self.code(item_id), text) for item_id, result in self.absent()
                if result is not None for text in result.warning_texts]


# ---------------------------------------------------------------------------
# DW-6 — the warnings alone, for the screens that do not evaluate the rules: the BOQ view
# (boq_detail, Design users) and the design groups (their screen, Excel and snapshots).
#
# ONE QUERY, whatever the number of rules or sites (Zuber, DW-6 answer 5): only the rules
# that can raise a warning, with their drivers and each group driver's members, in one
# joined read. The warning itself is rule_warnings(), the function evaluate() calls, on the
# same RuleSpec shape, so these screens can only say what the picker says.
# ---------------------------------------------------------------------------

def read_warning_rules(project_type):
    """[RuleSpec] for the active rules of `project_type` that can raise a warning, in target
    code order. ONE query; nothing is computed or written.

    Each spec carries exactly what rule_warnings() reads: the target's code, the clamp and
    the named check, and the drivers split into summed and cross-check ones. No labels and
    no rounding: nothing here is explained or worked out."""
    # Lazy import: models.py imports this module, so importing models at the top would be
    # circular.
    from django.db.models import Q
    from .models import BOQFormulaDriver
    # One row per driver, or per member for a group driver (a LEFT JOIN through the group's
    # members; an item driver's member column is NULL). Same terms as load_rules(), plus
    # "can raise a warning".
    rows = (BOQFormulaDriver.objects
            .filter(rule__is_active=True, rule__target__is_active=True,
                    rule__target__project_type=project_type)
            .filter(Q(rule__clamp_at_zero=True) | ~Q(rule__warning_check=''))
            .values_list('rule_id', 'rule__target_id', 'rule__target__code',
                         'rule__clamp_at_zero', 'rule__warning_check',
                         'pk', 'role', 'weight', 'item_id', 'group_id', 'group__members__item')
            .order_by('rule__target__code', 'rule_id', 'pk'))
    rules, drivers = {}, {}
    for (rule_id, target_id, code, clamp, check,
         driver_id, role, weight, item_id, group_id, member_id) in rows:
        rules.setdefault(rule_id, (target_id, code, clamp, check))
        driver = drivers.setdefault((rule_id, driver_id),
                                    {'role': role, 'weight': weight, 'item_id': item_id,
                                     'group_id': group_id, 'members': []})
        if member_id is not None:
            driver['members'].append(member_id)
    specs = []
    for rule_id, (target_id, code, clamp, check) in rules.items():
        summed, checks = [], []
        for (owner, _pk), d in drivers.items():
            if owner != rule_id:
                continue
            spec = DriverSpec(weight=d['weight'], label='', item_id=d['item_id'],
                              group_id=d['group_id'], member_ids=tuple(d['members']))
            (checks if d['role'] == DRIVER_ROLE_CHECK else summed).append(spec)
        specs.append(RuleSpec(rule_id=rule_id, target_id=target_id, summary='',
                              drivers=tuple(summed), clamp_at_zero=clamp,
                              warning_check=check, check_drivers=tuple(checks),
                              target_code=code))
    return specs


def sheet_warnings(specs, quantities):
    """[RuleWarning] for one sheet: `quantities` maps item id -> quantity (lines summed per
    item; a blank counts as nothing). Pure: no query. In the specs' (target code) order."""
    values = {item_id: Decimal(q) for item_id, q in quantities.items() if q is not None}
    return [w for spec in specs for w in rule_warnings(spec, values)]


def formula_view(boq, project_type):
    """FormulaView for one BOQ. One exists() query with no rules; with rules, that plus
    load_rules()'s three plus one read of the lines, however many rules exist.

    Built from the same sheet_inputs() the save uses, so an explanation on the screen is
    the one the next save would record."""
    prepared = read_formula_rules(project_type)
    if not prepared.active:
        return FormulaView()
    sheet = read_sheet(boq)
    quantities, lines_of, overrides = sheet_inputs(sheet, prepared.targets)
    view = FormulaView(rules=prepared.rules, lines_of=lines_of)
    try:
        view.results = evaluate(quantities, prepared.rules, overrides)
    except FormulaError as exc:
        view.error = str(exc)
    return view


# ---------------------------------------------------------------------------
# DW-3 — what a Remove would change, for the pop-up (read only)
#
# A Remove is an override to 0 (ruling DW-R4, R2), so what follows it is exactly what the
# override would make the engine do. Worked out ON PAGE LOAD (Zuber, DW-3 answer 1) from
# the FormulaView the screen already built: evaluate() and sheet_inputs() are pure, so the
# list for every calculated line together costs no query. work_out_formulas() is not
# called per line because its formula_line_blocked() queries for each stored line that
# falls to 0; here the line's ordered_quantity (already loaded) answers that, which is the
# whole answer on a RESCO BOQ, since a RESCO order line names no BOQ row.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RemovalPreview:
    """What Remove on one calculated line would change besides the line itself."""
    follow: tuple = ()          # Plain sentences, in evaluation order: "OPX-198 2 → 0, removed from the BOQ"
    also_up_to_date: int = 0    # Other stale calculated lines the same save brings up to date (answer 2)


def _qty_text(value):
    return 'blank' if value is None else _num(value)


def _next_save_changes(result, own, remove_zero):
    """True if the next save changes this calculated item whatever else happens (Q2's
    "stale"), judged as work_out_formulas() would write it. Pure."""
    if len(own) > 1 or (own and own[0].overridden):
        return False
    if not own:
        return result.computed > 0
    stored = own[0].boq_quantity
    if result.computed == 0:
        return (remove_zero and own[0].ordered_quantity is None) or stored != 0
    return stored != result.computed


def removal_previews(view, *, remove_zero):
    """{item id: RemovalPreview} for every calculated line a Remove may be offered on: an
    item on exactly one line that is not removed already. Pure: no query.

    `remove_zero` is the path's own (True for the designer, False for QC and the Head, as
    _boq_line_override() passes it): on the designer path a line that follows to 0 is
    removed (D1), on the reviewer path it is set to 0, and the sentence says which (answer 3).

    ONLY WHAT FOLLOWS FROM THE REMOVE IS LISTED (answer 2). The trial is compared with the
    page's own evaluation, not with the stored figures, so a stale line the next save would
    change anyway is not in `follow`; it is counted in `also_up_to_date` instead. The figure
    shown on the left is the STORED one, the one on the screen.

    A target whose trial cannot be worked out (a figure too large for the column) gets no
    preview; its save refuses with the engine's reason (A4).
    """
    if not view.active or view.error:
        return {}
    sheet = [line for lines in view.lines_of.values() for line in lines]
    quantities, lines_of, overrides = sheet_inputs(sheet, view.targets)
    codes = view.rules.codes
    base = view.results
    stale = {item_id for item_id, result in base.items()
             if _next_save_changes(result, lines_of.get(item_id, []), remove_zero)}
    previews = {}
    for target in view.targets:
        own = lines_of.get(target, [])
        if len(own) != 1 or is_removed(own[0].overridden, own[0].boq_quantity):
            continue
        try:
            trial = evaluate(quantities, view.rules, {**overrides, target: ZERO})
        except FormulaError:
            continue
        follow, follow_ids = [], set()
        for item_id, result in trial.items():         # evaluation (dependency) order
            if item_id == target or result.quantity == base[item_id].quantity:
                continue
            lines = lines_of.get(item_id, [])
            if len(lines) > 1 or (lines and lines[0].overridden):
                continue                               # not written (Q9), or left alone
            after = result.computed
            code = codes.get(item_id, '')
            if not lines:
                if after > 0:
                    follow.append(f'{code} added at {_num(after)}')
                    follow_ids.add(item_id)
                continue
            stored = lines[0].boq_quantity
            if after == 0 and remove_zero and lines[0].ordered_quantity is None:
                follow.append(f'{code} {_qty_text(stored)} → 0, removed from the BOQ')
            elif stored is not None and stored == after:
                continue                               # the line already carries it
            else:
                follow.append(f'{code} {_qty_text(stored)} → {_num(after)}')
            follow_ids.add(item_id)
        previews[target] = RemovalPreview(
            follow=tuple(follow), also_up_to_date=len(stale - follow_ids - {target}))
    return previews
