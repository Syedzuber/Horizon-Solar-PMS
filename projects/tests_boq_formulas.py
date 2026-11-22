"""
B-2a verification — the BOQ formula engine: rules stored as data, a pure evaluator.

WHAT THIS FILE PROVES
---------------------
  * Every rule shape Praveen's table needs, on TEST rules and TEST items (no real
    catalogue code appears here): a weighted sum of drivers (R1), ÷ 1.5 ÷ 100 rounded up
    (R5), a chain two deep that reads the ROUNDED computed quantity (R7), × 40% ÷ 1.5 in
    exact Decimal (R11), a chain on a group (R13 -> R14), a sum of two unlike drivers
    (R16), "+ 4" over three groups (R20), a driverless constant (R15), and 'none'
    rounding for kWp (R24/R25).
  * G3: a driver of 0 gives 0, not 1; a tiny positive driver gives 1, on both rounding
    modes.
  * requires_group (Q3): a rule gives 0, constant included, when the site has none of
    the group.
  * Loops are refused when a rule, driver or group member is saved, and again, defensively,
    by evaluate().
  * The explanation text for each shape, word for word.
  * Purity: evaluate() runs no query; load_rules() runs the same 3 however many rules exist.
  * Save-time validation: divisor above 0, a driver is exactly one of item or group, a
    target is not its own driver, one active rule per item, one project type per rule,
    never the configured-ACCB row.
  * Retired targets are skipped by load_rules(); retired drivers and group members still
    count (Q8).

RUNNER: `manage.py test projects --settings=solarpms.test_settings` (migrations disabled,
the catalogue rows are created in setUp).
"""
from decimal import Decimal, localcontext

from django.contrib import admin
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import RequestFactory, SimpleTestCase, TestCase

from .accb import ACCB_CONFIGURED_CODE
from .boq_formulas import (
    QUANTITY_MAX, ROUND_NONE, ROUND_UP, DriverSpec, FormulaCycleError, FormulaError,
    RuleSet, RuleSpec, build_rule_set, evaluate, load_rules,
)
from .models import (
    BOQFormulaDriver, BOQFormulaRule, BOQItem, BOQItemGroup, BOQItemGroupMember,
    BOQItemMaster,
)
from .number_input import decimal_field_max


def D(value):
    return Decimal(str(value))


# ---------------------------------------------------------------------------
# Pure tests: hand-built specs, no database
# ---------------------------------------------------------------------------

#: Test items: id -> (code, unit). Codes are TST-*, never a real catalogue code.
ITEMS = {
    1: ('TST-LA1', 'Nos'), 2: ('TST-LA2', 'Nos'), 3: ('TST-EARTH', 'Meter'),
    4: ('TST-CON', 'Meter'), 5: ('TST-SAD40', 'Pkt'), 6: ('TST-CON63', 'Meter'),
    7: ('TST-SAD63', 'Pkt'), 8: ('TST-SCREW', 'Pkt'), 9: ('TST-GI', 'Meter'),
    10: ('TST-EPOXY', 'Nos'), 11: ('TST-LOGGER', 'Nos'), 12: ('TST-RS485', 'Meter'),
    13: ('TST-FLEX', 'Meter'), 14: ('TST-DCRED', 'Meter'), 15: ('TST-TIE', 'Pkt'),
    16: ('TST-INV1', 'Nos'), 17: ('TST-INV2', 'Nos'), 18: ('TST-DCDB', 'Nos'),
    19: ('TST-ACDB', 'Nos'), 20: ('TST-FAST', 'Nos'), 21: ('TST-WMS', 'Set'),
    22: ('TST-TIN', 'Nos'), 23: ('TST-INCTIN', 'KWp'), 24: ('TST-BALLAST', 'Nos'),
    25: ('TST-INCB', 'KWp'),
}
(LA1, LA2, EARTH, CON, SAD40, CON63, SAD63, SCREW, GI, EPOXY, LOGGER, RS485, FLEX, DCRED,
 TIE, INV1, INV2, DCDB, ACDB, FAST, WMS, TIN, INCTIN, BALLAST, INCB) = range(1, 26)
CODES = {i: c for i, (c, _) in ITEMS.items()}
UNITS = {i: u for i, (_, u) in ITEMS.items()}
INVERTERS = (INV1, INV2)


def item_driver(item_id, weight=1, label=None):
    return DriverSpec(weight=D(weight), label=label or CODES[item_id], item_id=item_id)


def group_driver(members, label, weight=1, group_id=900):
    return DriverSpec(weight=D(weight), label=label, group_id=group_id,
                      member_ids=tuple(members))


_next_rule_id = iter(range(1, 10_000))


def spec(target, drivers=(), m=1, d=1, c=0, rounding=ROUND_UP, floor=True,
         requires=None, requires_label='', summary='Rule'):
    return RuleSpec(
        rule_id=next(_next_rule_id), target_id=target, summary=summary,
        drivers=tuple(drivers), multiplier=D(m), divisor=D(d), constant=D(c),
        rounding=rounding, floor_one_when_driven=floor,
        requires_member_ids=None if requires is None else tuple(requires),
        requires_label=requires_label)


def ruleset(*specs):
    return build_rule_set(specs, CODES, UNITS)


def run(specs, quantities, overrides=None):
    return evaluate({k: D(v) for k, v in quantities.items()}, ruleset(*specs), overrides)


R1 = dict(target=EARTH, drivers=[group_driver((LA1, LA2), 'lightning arresters')], m=5,
          summary='5 m per lightning arrester')
R5_40 = dict(target=SAD40, drivers=[item_driver(CON)], d=150,
             summary='One 100-pack per 150 m of 32/40 conduit')
R5_63 = dict(target=SAD63, drivers=[item_driver(CON63)], d=150,
             summary='One 100-pack per 150 m of 50/63 conduit')
R7 = dict(target=SCREW, drivers=[item_driver(SAD40), item_driver(SAD63)], m=2,
          summary='2 packs per saddle pack')
R11 = dict(target=EPOXY, drivers=[item_driver(GI)], m='0.4', d='1.5',
           summary='One insulator per 1.5 m on 40% of the GI strip')
R13 = dict(target=LOGGER, drivers=[group_driver(INVERTERS, 'inverters')],
           summary='One data logger per inverter')
R14 = dict(target=RS485, drivers=[item_driver(LOGGER)], m=5,
           summary='5 m of RS485 cable per data logger')
R15 = dict(target=WMS, c=1, summary='WMS')
R16 = dict(target=TIE, drivers=[item_driver(FLEX), item_driver(DCRED)], d=150,
           summary='One packet of 250 mm ties per 150 of conduit and DC cable')
R20 = dict(target=FAST, drivers=[group_driver(INVERTERS, 'inverters', group_id=901),
                                 group_driver((DCDB,), 'DCDBs', group_id=902),
                                 group_driver((ACDB,), 'ACDBs', group_id=903)],
           m=4, c=4, summary='4 fasteners per inverter, DCDB and ACDB, plus 4')
R24 = dict(target=INCTIN, drivers=[item_driver(TIN)], m='0.64', rounding=ROUND_NONE,
           floor=False, summary='0.64 kWp of INC work per tin shade')
R25 = dict(target=INCB, drivers=[item_driver(BALLAST)], m='0.62', rounding=ROUND_NONE,
           floor=False, summary='0.62 kWp of INC work per ballast structure')


class SumOfDriversTests(SimpleTestCase):
    """R1 and R16: drivers are summed, each with its weight."""

    def test_group_driver_times_multiplier(self):
        result = run([spec(**R1)], {LA1: 2, LA2: 1})[EARTH]
        self.assertEqual(result.quantity, D('15.00'))
        self.assertEqual(result.driver_sum, D(3))
        self.assertEqual(result.explanation,
                         '5 m per lightning arrester: 3 lightning arresters '
                         '(2 TST-LA1 + 1 TST-LA2) × 5 = 15 Meter')

    def test_weighted_item_drivers(self):
        rule = spec(EARTH, [item_driver(LA1, 2, 'conventional'), item_driver(LA2, 3, 'ESE')],
                    m=5, summary='Weighted')
        result = run([rule], {LA1: 2, LA2: 1})[EARTH]
        # (2 × 2 + 3 × 1) × 5 = 35
        self.assertEqual(result.driver_sum, D(7))
        self.assertEqual(result.quantity, D(35))
        self.assertEqual(result.explanation,
                         'Weighted: (2 × 2 conventional + 3 × 1 ESE) × 5 = 35 Meter')

    def test_two_unlike_drivers_summed_as_given(self):
        # R16 (G4: built as given): 70 + 2958 = 3028 / 150 = 20.18… -> 21 packets.
        result = run([spec(**R16)], {FLEX: 70, DCRED: 2958})[TIE]
        self.assertEqual(result.quantity, D(21))
        self.assertEqual(result.explanation,
                         'One packet of 250 mm ties per 150 of conduit and DC cable: '
                         '(70 TST-FLEX + 2958 TST-DCRED) ÷ 150 = 20.1866…, rounded up to '
                         '21 Pkt')

    def test_missing_and_blank_drivers_count_as_zero(self):
        results = evaluate({LA1: None}, ruleset(spec(**R1)))
        self.assertEqual(results[EARTH].quantity, D(0))
        self.assertEqual(results[EARTH].explanation,
                         '5 m per lightning arrester: 0 lightning arresters × 5 = 0 Meter')


class RoundUpTests(SimpleTestCase):
    """R5: ÷ 1.5 ÷ 100 (stored as one divisor, 150), rounded up to whole packs."""

    def qty(self, metres):
        return run([spec(**R5_40)], {CON: metres})[SAD40].quantity

    def test_149_metres_is_one_pack(self):
        self.assertEqual(self.qty(149), D(1))

    def test_150_metres_is_exactly_one_pack(self):
        self.assertEqual(self.qty(150), D(1))

    def test_151_metres_is_two_packs(self):
        self.assertEqual(self.qty(151), D(2))

    def test_zero_metres_is_zero_packs(self):
        self.assertEqual(self.qty(0), D(0))

    def test_explanation_shows_the_rounding(self):
        result = run([spec(**R5_40)], {CON: 151})[SAD40]
        self.assertEqual(result.explanation,
                         'One 100-pack per 150 m of 32/40 conduit: 151 TST-CON ÷ 150 = '
                         '1.0066…, rounded up to 2 Pkt')

    def test_quantities_have_two_decimal_places(self):
        # The shape of BOQItem.boq_quantity, so B-2b can write the value as it comes.
        self.assertEqual(self.qty(151).as_tuple().exponent, -2)


class ExactDecimalTests(SimpleTestCase):
    """R11 and R25: exact Decimal arithmetic, never float."""

    def test_forty_percent_divided_by_one_and_a_half(self):
        result = run([spec(**R11)], {GI: 280})[EPOXY]
        with localcontext() as ctx:
            ctx.prec = 50
            exact = Decimal(280) * Decimal('0.4') / Decimal('1.5')
        self.assertEqual(result.raw, exact)
        self.assertEqual(result.quantity, D(75))
        self.assertEqual(result.explanation,
                         'One insulator per 1.5 m on 40% of the GI strip: 280 TST-GI × 0.4 '
                         '÷ 1.5 = 74.6666…, rounded up to 75 Nos')

    def test_terminating_result_is_not_rounded_up(self):
        # 150 × 0.4 ÷ 1.5 is exactly 40: multiply before dividing keeps it exact.
        result = run([spec(**R11)], {GI: 150})[EPOXY]
        self.assertEqual(result.raw, D(40))
        self.assertEqual(result.quantity, D(40))
        self.assertEqual(result.explanation,
                         'One insulator per 1.5 m on 40% of the GI strip: 150 TST-GI × 0.4 '
                         '÷ 1.5 = 40 Nos')

    def test_none_rounding_is_exact_half_up(self):
        # 0.62 × 0.25 is exactly 0.155, which rounds half-up to 0.16. In float it is
        # 0.15499999…, which rounds to 0.15. This is the test the red check breaks.
        result = run([spec(**R25)], {BALLAST: '0.25'})[INCB]
        self.assertEqual(result.raw, D('0.155'))
        self.assertEqual(result.quantity, D('0.16'))
        self.assertEqual(result.explanation,
                         '0.62 kWp of INC work per ballast structure: 0.25 TST-BALLAST × '
                         '0.62 = 0.155, rounded to 0.16 KWp')

    def test_none_rounding_keeps_an_exact_kwp_value(self):
        result = run([spec(**R24)], {TIN: 3})[INCTIN]
        self.assertEqual(result.quantity, D('1.92'))
        self.assertEqual(result.explanation,
                         '0.64 kWp of INC work per tin shade: 3 TST-TIN × 0.64 = 1.92 KWp')

    def test_result_beyond_the_column_is_refused(self):
        rule = spec(EARTH, [item_driver(LA1)], m=1000, summary='Huge')
        with self.assertRaises(FormulaError):
            run([rule], {LA1: 100000})


class ChainTests(SimpleTestCase):
    """R7, R14: computed items as drivers, in dependency order, reading the ROUNDED value."""

    def test_chain_two_deep_reads_rounded_quantities(self):
        # 151 m -> 2 saddle packs (rounded), 0 m -> 0; screws = 2 × (2 + 0) = 4. Reading
        # the unrounded 1.0066… would give 2 × 1.0066… = 2.01… -> 3.
        results = run([spec(**R7), spec(**R5_63), spec(**R5_40)], {CON: 151, CON63: 0})
        self.assertEqual(results[SAD40].quantity, D(2))
        self.assertEqual(results[SCREW].quantity, D(4))
        self.assertEqual(results[SCREW].chain, (SAD40, SAD63))
        self.assertEqual(results[SAD40].chain, ())
        self.assertEqual(results[SCREW].explanation,
                         '2 packs per saddle pack: (2 TST-SAD40 + 0 TST-SAD63) × 2 = 4 Pkt '
                         '(TST-SAD40, TST-SAD63 are computed lines; the rounded quantity '
                         'is used)')

    def test_order_follows_dependencies_not_input_order(self):
        rules = ruleset(spec(**R7), spec(**R5_63), spec(**R5_40))
        order = [rule.target_id for rule in rules.rules]
        self.assertLess(order.index(SAD40), order.index(SCREW))
        self.assertLess(order.index(SAD63), order.index(SCREW))
        # Ready rules are taken in code order, so the order never depends on input order.
        self.assertEqual(order, [rule.target_id for rule in
                                 ruleset(spec(**R5_40), spec(**R5_63), spec(**R7)).rules])

    def test_chain_on_a_group_driver(self):
        results = run([spec(**R14), spec(**R13)], {INV1: 2, INV2: 1})
        self.assertEqual(results[LOGGER].quantity, D(3))
        self.assertEqual(results[RS485].quantity, D(15))
        self.assertEqual(results[RS485].chain, (LOGGER,))
        self.assertEqual(results[LOGGER].explanation,
                         'One data logger per inverter: 3 inverters (2 TST-INV1 + 1 TST-INV2)'
                         ' = 3 Nos')
        self.assertEqual(results[RS485].explanation,
                         '5 m of RS485 cable per data logger: 3 TST-LOGGER × 5 = 15 Meter '
                         '(TST-LOGGER is a computed line; the rounded quantity is used)')

    def test_hand_entered_computed_item_is_reported_not_used(self):
        # The sheet says 99 saddle packs; the rule computes 2, and screws read the 2.
        results = run([spec(**R5_40), spec(**R5_63), spec(**R7)],
                      {CON: 151, SAD40: 99})
        self.assertEqual(results[SAD40].quantity, D(2))
        self.assertEqual(results[SAD40].entered, D(99))
        self.assertFalse(results[SAD40].overridden)
        self.assertEqual(results[SCREW].quantity, D(4))
        self.assertIsNone(results[SCREW].entered)

    def test_override_is_used_downstream_and_the_rule_value_kept(self):
        results = run([spec(**R5_40), spec(**R5_63), spec(**R7)], {CON: 151},
                      overrides={SAD40: D(5)})
        self.assertTrue(results[SAD40].overridden)
        self.assertEqual(results[SAD40].quantity, D(5))
        self.assertEqual(results[SAD40].computed, D(2))
        self.assertEqual(results[SCREW].quantity, D(10))
        self.assertEqual(results[SAD40].explanation,
                         'One 100-pack per 150 m of 32/40 conduit: set by hand to 5 Pkt; '
                         'the rule gives 2 Pkt')


class ConstantTests(SimpleTestCase):
    """R15 (a driverless constant) and R20 ("+ 4")."""

    def test_driverless_constant(self):
        result = run([spec(**R15)], {})[WMS]
        self.assertEqual(result.quantity, D(1))
        self.assertEqual(result.driver_sum, D(0))
        self.assertEqual(result.explanation, 'WMS: a fixed 1 Set')

    def test_plus_four_over_three_groups(self):
        # 4 × (2 + 1 inverters + 2 DCDBs + 3 ACDBs) + 4 = 36
        result = run([spec(**R20)], {INV1: 2, INV2: 1, DCDB: 2, ACDB: 3})[FAST]
        self.assertEqual(result.quantity, D(36))
        self.assertEqual(result.explanation,
                         '4 fasteners per inverter, DCDB and ACDB, plus 4: (3 inverters '
                         '(2 TST-INV1 + 1 TST-INV2) + 2 DCDBs (2 TST-DCDB) + 3 ACDBs '
                         '(3 TST-ACDB)) × 4 + 4 = 36 Nos')

    def test_plus_four_with_no_drivers_is_four_as_built(self):
        # Built as given. Whether this should be 0 is open with Praveen; if so, the rule
        # gets a requires_group and RequiresGroupTests shows the result.
        self.assertEqual(run([spec(**R20)], {})[FAST].quantity, D(4))


class G3FloorTests(SimpleTestCase):
    """At least 1, only when the driver sum is above 0."""

    def test_zero_driver_gives_zero_not_one_round_up(self):
        self.assertEqual(run([spec(**R13)], {})[LOGGER].quantity, D(0))

    def test_tiny_driver_gives_one_round_up(self):
        result = run([spec(target=LOGGER, drivers=[item_driver(INV1)], m='0.001',
                           summary='Tiny')], {INV1: '0.01'})[LOGGER]
        self.assertEqual(result.quantity, D(1))

    def test_floor_matters_on_a_none_row(self):
        # 0.01 × 0.1 = 0.001 -> 0.00 at two decimals; the floor raises it to 1.
        rule = spec(target=INCTIN, drivers=[item_driver(TIN)], m='0.1', rounding=ROUND_NONE,
                    floor=True, summary='Floored')
        result = run([rule], {TIN: '0.01'})[INCTIN]
        self.assertEqual(result.quantity, D(1))
        self.assertEqual(result.explanation,
                         'Floored: 0.01 TST-TIN × 0.1 = 0.001, raised to the minimum of 1 KWp '
                         'because the drivers are above 0')
        self.assertEqual(run([rule], {TIN: 0})[INCTIN].quantity, D(0))

    def test_floor_off_keeps_a_small_kwp_value(self):
        # R24/R25 have the floor off (Q2): 0.64 kWp stays 0.64, never 1.
        self.assertEqual(run([spec(**R24)], {TIN: 1})[INCTIN].quantity, D('0.64'))


class RequiresGroupTests(SimpleTestCase):
    """Q3: a rule gives 0, constant included, when the site has none of its group."""

    def test_constant_is_zero_without_the_group(self):
        rule = spec(**dict(R15, requires=INVERTERS, requires_label='inverters'))
        result = run([rule], {DCDB: 4})[WMS]
        self.assertEqual(result.quantity, D(0))
        self.assertEqual(result.explanation, 'WMS: 0 Set, because the site has no inverters')

    def test_constant_applies_with_the_group(self):
        rule = spec(**dict(R15, requires=INVERTERS, requires_label='inverters'))
        self.assertEqual(run([rule], {INV2: 1})[WMS].quantity, D(1))

    def test_plus_four_is_zero_without_the_group(self):
        rule = spec(**dict(R20, requires=INVERTERS, requires_label='inverters'))
        self.assertEqual(run([rule], {DCDB: 1})[FAST].quantity, D(0))
        self.assertEqual(run([rule], {INV1: 1})[FAST].quantity, D(8))

    def test_gate_on_a_computed_item_is_evaluated_after_it(self):
        # The gate reads TST-LOGGER, itself computed: it must be worked out first.
        rule = spec(**dict(R15, requires=(LOGGER,), requires_label='data loggers'))
        results = run([rule, spec(**R13)], {INV1: 1})
        self.assertEqual(results[WMS].quantity, D(1))


class CycleTests(SimpleTestCase):
    """Loops refused when the rule set is built, and again inside evaluate()."""

    def test_two_rule_loop_refused_at_build(self):
        a = spec(SAD40, [item_driver(SCREW)])
        b = spec(SCREW, [item_driver(SAD40)])
        with self.assertRaises(FormulaCycleError) as raised:
            ruleset(a, b)
        self.assertIn('TST-SAD40', str(raised.exception))
        self.assertIn('TST-SCREW', str(raised.exception))

    def test_self_loop_through_a_group_refused_at_build(self):
        with self.assertRaises(FormulaCycleError):
            ruleset(spec(LOGGER, [group_driver((INV1, LOGGER), 'inverters')]))

    def test_loop_through_the_gate_refused_at_build(self):
        with self.assertRaises(FormulaCycleError):
            ruleset(spec(WMS, c=1, requires=(WMS,), requires_label='wms'))

    def test_evaluate_refuses_a_loop_it_is_handed(self):
        a = spec(SAD40, [item_driver(SCREW)])
        b = spec(SCREW, [item_driver(SAD40)])
        with self.assertRaises(FormulaCycleError):
            evaluate({}, RuleSet(rules=(a, b), codes=CODES, units=UNITS))

    def test_evaluate_refuses_a_set_out_of_order(self):
        # No loop, but the reader is first: evaluate() must not read a stale value.
        with self.assertRaises(FormulaCycleError):
            evaluate({CON: D(151)},
                     RuleSet(rules=(spec(**R7), spec(**R5_40), spec(**R5_63)),
                             codes=CODES, units=UNITS))

    def test_two_rules_for_one_item_refused(self):
        with self.assertRaises(FormulaError):
            ruleset(spec(**R15), spec(**R15))


# ---------------------------------------------------------------------------
# Database tests: models, save-time validation, the loader, purity
# ---------------------------------------------------------------------------

class FormulaStorageTests(TestCase):

    def setUp(self):
        def master(code, unit='Nos', project_type='OPEX', active=True):
            return BOQItemMaster.objects.create(
                code=code, description=f'{code} test item', unit=unit,
                category='Test', project_type=project_type, is_active=active)

        self.inv1 = master('TST-INV1')
        self.inv2 = master('TST-INV2')
        self.inv_retired = master('TST-INV-OLD', active=False)
        self.dcdb = master('TST-DCDB')
        self.logger = master('TST-LOGGER')
        self.rs485 = master('TST-RS485', unit='Meter')
        self.fast = master('TST-FAST')
        self.wms = master('TST-WMS', unit='Set')
        self.res_item = master('TST-RES', project_type='Residential')
        self.accb = master(ACCB_CONFIGURED_CODE)

        self.inverters = BOQItemGroup.objects.create(name='all inverters', label='inverters')
        for item in (self.inv1, self.inv2, self.inv_retired):
            BOQItemGroupMember.objects.create(group=self.inverters, item=item)

    def rule(self, target, drivers=(), **fields):
        rule = BOQFormulaRule.objects.create(target=target,
                                             summary=fields.pop('summary', 'Rule'), **fields)
        for driver in drivers:
            BOQFormulaDriver.objects.create(rule=rule, **driver)
        return rule

    # -- the loader -----------------------------------------------------------

    def test_load_and_evaluate_round_trip(self):
        self.rule(self.logger, [{'group': self.inverters}], floor_one_when_driven=True,
                  summary='One data logger per inverter')
        self.rule(self.rs485, [{'item': self.logger}], multiplier=5,
                  summary='5 m of RS485 cable per data logger')
        results = evaluate({self.inv1.pk: D(2), self.inv_retired.pk: D(1)}, load_rules())
        # The retired inverter still on the sheet still counts (Q8).
        self.assertEqual(results[self.logger.pk].quantity, D(3))
        self.assertEqual(results[self.rs485.pk].quantity, D(15))
        # Members are listed in code order ('-' sorts before '1').
        self.assertEqual(results[self.logger.pk].explanation,
                         'One data logger per inverter: 3 inverters '
                         '(1 TST-INV-OLD + 2 TST-INV1) = 3 Nos')

    def test_retired_driver_item_still_counts(self):
        self.rule(self.logger, [{'item': self.inv_retired}])
        results = evaluate({self.inv_retired.pk: D(4)}, load_rules())
        self.assertEqual(results[self.logger.pk].quantity, D(4))

    def test_rule_with_retired_target_is_skipped(self):
        self.rule(self.logger, [{'group': self.inverters}])
        self.logger.is_active = False
        self.logger.save()
        self.assertEqual(load_rules().rules, ())

    def test_inactive_rule_is_skipped(self):
        self.rule(self.logger, [{'group': self.inverters}], is_active=False)
        self.assertEqual(load_rules().rules, ())

    def test_requires_group_round_trip(self):
        self.rule(self.wms, constant=1, requires_group=self.inverters, summary='WMS')
        rules = load_rules()
        self.assertEqual(evaluate({self.dcdb.pk: D(2)}, rules)[self.wms.pk].quantity, D(0))
        self.assertEqual(evaluate({self.inv2.pk: D(1)}, rules)[self.wms.pk].quantity, D(1))
        self.assertEqual(evaluate({}, rules)[self.wms.pk].explanation,
                         'WMS: 0 Set, because the site has no inverters')

    def test_driver_label_overrides_the_code(self):
        self.rule(self.rs485, [{'item': self.logger, 'label': 'data loggers'}], multiplier=5,
                  summary='Cable')
        result = evaluate({self.logger.pk: D(2)}, load_rules())[self.rs485.pk]
        self.assertEqual(result.explanation, 'Cable: 2 data loggers × 5 = 10 Meter')

    # -- purity and query counts ------------------------------------------------

    def test_load_rules_is_three_queries_however_many_rules(self):
        self.rule(self.logger, [{'group': self.inverters}])
        with self.assertNumQueries(3):
            load_rules()
        self.rule(self.rs485, [{'item': self.logger}], multiplier=5)
        self.rule(self.fast, [{'group': self.inverters}, {'item': self.dcdb, 'weight': 2}],
                  multiplier=4, constant=4)
        self.rule(self.wms, constant=1, requires_group=self.inverters)
        with self.assertNumQueries(3):
            rules = load_rules()
        self.assertEqual(len(rules.rules), 4)

    def test_load_rules_with_no_rules_is_still_three_queries(self):
        with self.assertNumQueries(3):
            self.assertEqual(load_rules().rules, ())

    def test_evaluate_runs_no_query(self):
        self.rule(self.logger, [{'group': self.inverters}])
        self.rule(self.rs485, [{'item': self.logger}], multiplier=5)
        self.rule(self.wms, constant=1, requires_group=self.inverters)
        rules = load_rules()
        with self.assertNumQueries(0):
            results = evaluate({self.inv1.pk: D(1)}, rules)
            [r.explanation for r in results.values()]
        self.assertEqual(results[self.rs485.pk].quantity, D(5))

    def test_quantity_max_matches_the_boq_column(self):
        self.assertEqual(QUANTITY_MAX, decimal_field_max(BOQItem, 'boq_quantity'))

    # -- save-time validation ---------------------------------------------------

    def assertRefused(self, model, make, fragment):
        """`make` raises ValidationError mentioning `fragment`, and no row is left."""
        before = model.objects.count()
        with self.assertRaises(ValidationError) as raised:
            make()
        self.assertIn(fragment, str(raised.exception))
        self.assertEqual(model.objects.count(), before)

    def test_zero_divisor_refused(self):
        self.assertRefused(BOQFormulaRule, lambda: self.rule(self.logger, divisor=0),
                           'The divisor must be above 0.')

    def test_driver_with_item_and_group_refused(self):
        rule = self.rule(self.logger)
        self.assertRefused(
            BOQFormulaDriver,
            lambda: BOQFormulaDriver.objects.create(rule=rule, item=self.inv1,
                                                    group=self.inverters),
            'exactly one of an item or a group')

    def test_driver_with_neither_refused(self):
        rule = self.rule(self.logger)
        self.assertRefused(BOQFormulaDriver,
                           lambda: BOQFormulaDriver.objects.create(rule=rule),
                           'exactly one of an item or a group')

    def test_target_as_its_own_driver_refused(self):
        rule = self.rule(self.logger)
        self.assertRefused(BOQFormulaDriver,
                           lambda: BOQFormulaDriver.objects.create(rule=rule, item=self.logger),
                           'cannot be its own driver')

    def test_loop_across_two_rules_refused_at_save(self):
        self.rule(self.rs485, [{'item': self.logger}])
        second = self.rule(self.logger)
        self.assertRefused(BOQFormulaDriver,
                           lambda: BOQFormulaDriver.objects.create(rule=second,
                                                                   item=self.rs485),
                           'form a loop')

    def test_loop_closed_by_a_group_member_refused(self):
        # The logger is computed from "all inverters"; adding the logger to that group
        # would make it its own driver.
        self.rule(self.logger, [{'group': self.inverters}])
        self.assertRefused(
            BOQItemGroupMember,
            lambda: BOQItemGroupMember.objects.create(group=self.inverters, item=self.logger),
            'form a loop')

    def test_loop_through_a_retired_target_refused(self):
        # Skipped by load_rules(), but the loop would come back the day it is reactivated.
        self.rule(self.rs485, [{'item': self.logger}])
        second = self.rule(self.logger)
        self.logger.is_active = False
        self.logger.save()
        self.assertRefused(BOQFormulaDriver,
                           lambda: BOQFormulaDriver.objects.create(rule=second,
                                                                   item=self.rs485),
                           'form a loop')

    def test_second_active_rule_for_an_item_refused(self):
        self.rule(self.wms, constant=1)
        self.assertRefused(BOQFormulaRule, lambda: self.rule(self.wms, constant=2),
                           'already has an active formula rule')

    def test_retired_rule_can_be_replaced(self):
        old = self.rule(self.wms, constant=1)
        old.is_active = False
        old.save()
        self.rule(self.wms, constant=2)
        self.assertEqual(evaluate({}, load_rules())[self.wms.pk].quantity, D(2))

    def test_mixed_project_types_refused(self):
        rule = self.rule(self.logger)
        self.assertRefused(BOQFormulaDriver,
                           lambda: BOQFormulaDriver.objects.create(rule=rule,
                                                                   item=self.res_item),
                           'mixes project types')

    def test_mixed_project_types_through_a_group_refused(self):
        self.rule(self.logger, [{'group': self.inverters}])
        self.assertRefused(
            BOQItemGroupMember,
            lambda: BOQItemGroupMember.objects.create(group=self.inverters,
                                                      item=self.res_item),
            'mixes project types')

    def test_configured_accb_cannot_be_a_target(self):
        self.assertRefused(BOQFormulaRule, lambda: self.rule(self.accb, constant=1),
                           'configured ACCB')

    def test_unknown_rounding_refused(self):
        self.assertRefused(BOQFormulaRule, lambda: self.rule(self.wms, rounding='down'),
                           'rounding')

    # -- admin --------------------------------------------------------------------

    def test_admin_is_view_only(self):
        request = RequestFactory().get('/')
        request.user = User.objects.create_superuser('formula_admin', 'a@example.com', 'x')
        for model in (BOQFormulaRule, BOQItemGroup):
            model_admin = admin.site._registry[model]
            self.assertFalse(model_admin.has_add_permission(request))
            self.assertFalse(model_admin.has_change_permission(request))
            self.assertFalse(model_admin.has_delete_permission(request))
            for inline in model_admin.get_inline_instances(request):
                self.assertFalse(inline.has_add_permission(request, None))
                self.assertFalse(inline.has_change_permission(request))
                self.assertFalse(inline.has_delete_permission(request))
