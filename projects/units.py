"""
Units of measure — one list, and what each unit allows a quantity to be (D-A29, D-A30).

A COUNT unit counts whole things (12 Nos, 3 Sets), so its quantity is a whole number. A
MEASURED unit measures an amount (12.5 Meter, 2.25 KWp), so its quantity may carry up to
two decimal places — the most the quantity columns hold. The unit decides; the person
typing never does.

Nothing here reads the database, so a model module, a migration or a template filter can
import it without a cycle.
"""
from decimal import Decimal

UNIT_COUNT = 'count'
UNIT_MEASURED = 'measured'

# THE ONE SHARED VOCABULARY. It is enforced in two places: approval material lines
# (MaterialApprovalLine: choices here, a CHECK in the database, and approvals.py's
# chokepoint), and the BOQ catalogue (BOQItemMaster.unit: BOQItemMasterForm offers only
# these codes, B-0 ruling 13). BOQItem.uom, VendorOrderLine and DCLineItem units stay free
# text: a BOQ line keeps the unit text it was created with, so a catalogue fix never
# rewrites a site's sheet.
#
# (code, label, kind). The code is what is stored; the label is what a person reads.
UNITS = (
    ('Nos',   'Nos',      UNIT_COUNT),
    ('Meter', 'Meter',    UNIT_MEASURED),
    ('Set',   'Set',      UNIT_COUNT),
    ('Pkt',   'Packet',   UNIT_COUNT),
    ('Pair',  'Pair',     UNIT_COUNT),
    ('Lot',   'Lot',      UNIT_COUNT),
    ('KWp',   'KWp',      UNIT_MEASURED),
    ('Kg',    'Kg',       UNIT_MEASURED),
    ('LS',    'Lump sum', UNIT_MEASURED),
)

UNIT_CHOICES = tuple((code, label) for code, label, _ in UNITS)
UNIT_CODES = tuple(code for code, _, _ in UNITS)
COUNT_UNITS = tuple(code for code, _, kind in UNITS if kind == UNIT_COUNT)
MEASURED_UNITS = tuple(code for code, _, kind in UNITS if kind == UNIT_MEASURED)
UNIT_LABELS = dict(UNIT_CHOICES)

# OLD SPELLINGS OF A LISTED UNIT (B-0, ruling 13), and only those. Each maps a stored
# catalogue value to the code it already meant, so rewriting it is a spelling fix, not a
# change of unit: 'No.' and 'Nos' count the same thing. A value that is neither a code nor
# a key here (anything a person would have to decide about) has no mapping, and nothing
# guesses one.
UNIT_SPELLINGS = {
    'No.': 'Nos',
    'No':  'Nos',
    'Mtr': 'Meter',
    'LOT': 'Lot',
}


def canonical_unit(value):
    """The list code `value` stands for: itself if it is a code, its mapped code if it is
    a known old spelling, else None. Surrounding spaces are ignored; case is not folded,
    because deciding that two spellings are one unit is a ruling, and the rulings made so
    far are exactly the keys of UNIT_SPELLINGS."""
    value = (value or '').strip()
    if value in UNIT_CODES:
        return value
    return UNIT_SPELLINGS.get(value)


def unit_places(unit):
    """The decimal places a quantity in `unit` may carry: 0 for a count unit, 2 for a
    measured one. None for a code that is not on the list."""
    if unit in COUNT_UNITS:
        return 0
    if unit in MEASURED_UNITS:
        return 2
    return None


def format_quantity(quantity, unit):
    """A quantity as people read it, without its unit: "120" for 120 Nos, "2.50" for
    2.5 KWp. `quantity` may be a Decimal or the string a round snapshot stores.

    A count unit's quantity is whole (the CHECK holds it), so its ".00" is noise and is
    dropped. A measured one always shows two places, so 2.5 and 2.50 read the same. A unit
    not on the list is shown exactly as given — never rounded."""
    value = Decimal(str(quantity))
    places = unit_places(unit)
    # A value that does not fit its unit's places (possible only for a row written around
    # the chokepoint and the CHECK) is shown as stored: formatting it would round it.
    if places is None or value != round(value, places):
        return str(quantity)
    return f'{value:.{places}f}'
