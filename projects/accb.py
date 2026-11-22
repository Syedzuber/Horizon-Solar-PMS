"""
B-3 — the ACCB configurator's rules: what a configured ACCB says, how two are judged
identical, and what a valid one is.

PURE, AND THAT IS THE POINT. Nothing here touches the database, a request or a model.
The four functions below are the single definition of the approved description wording
(Praveen rulings 26-31, F7; prototype answers of 22 Nov), of the SCM grouping identity, of
the one-line summary the activity log carries, and of the server-side validation. The
ACCB view, the SCM aggregate and the tests all call these; the designer's live preview in
opex_boq_entry.html is a JavaScript COPY of accb_description() for display only, and the
server always regenerates the text from the stored configuration.

THIS MODULE IMPORTS NO MODELS. models.py imports the constants from here (the catalogue
code, the type list), so an import in the other direction would be circular.

A CONFIGURATION is a plain dict, the shape validate_accb_config() returns and
ACCBConfig.as_config() rebuilds:

    {'type': 'Type-2', 'spd': True,
     'in':  [{'breaker': 'MCCB', 'rating': 100}, {'breaker': 'MCCB', 'rating': 63}],
     'out': [{'breaker': 'MCCB', 'rating': 150}]}

Feeders are kept in the designer's order. Order matters for display (the card shows them as
entered) and does not matter for identity (accb_config_key sorts them).
"""
import re
from collections import Counter

from django.core.exceptions import ValidationError

#: The catalogue row every configured ACCB line links to (Zuber, B-3 Q2). OUTSIDE the
#: OPX-218..OPX-272 block the B-1 mapping workbook reserves. Left out of the picker, upload
#: and correction catalogue by get_opex_boq_catalogue(), and locked on both catalogue
#: screens, because a bare line on this code would have no configuration behind it.
ACCB_CONFIGURED_CODE = 'OPX-273'

#: Ruling 28 / F7 — a label the designer picks. Exactly these two.
ACCB_TYPES = ('Type-1', 'Type-2')

#: Ruling 27 — chosen per feeder. ORDER IS LOAD-BEARING: it is the order breaker groups
#: appear in the description ("MCCB first, then MCB") and in the grouping key.
ACCB_BREAKERS = ('MCCB', 'MCB')

#: Ruling 30 — no business maximum; the UI (and therefore the server) caps each side at 20.
ACCB_MIN_FEEDERS = 1
ACCB_MAX_FEEDERS = 20

#: Prototype answer 1 — any whole number of amps in this range is accepted.
ACCB_RATING_MIN = 1
ACCB_RATING_MAX = 1600

#: Prototype answer 1 — offered in the rating datalist. Suggestions only; never a whitelist.
ACCB_COMMON_RATINGS = (16, 20, 25, 32, 40, 50, 63, 80, 100, 125, 160, 200, 250, 315, 400, 630)

#: Ruling 31 — every ACCB is 4-pole, 415 V. Fixed text, never stored.
ACCB_POLES_VOLTAGE = '4P 415V'

#: The two sides, as (config key, the word the description uses).
ACCB_SIDES = (('in', 'I/C Feeder'), ('out', 'O/G Feeder'))

#: Zuber, B-3 Q7 — the upload refuses a file that carries the ACCB code, in these words.
ACCB_UPLOAD_REFUSED_MESSAGE = ('ACCB lines are managed in the ACCB section, not by upload. '
                               'Remove the ACCB rows from the sheet and upload again.')

#: B-3 P7 — a reviewer may not change an ACCB line; this is what the correction screen says.
ACCB_REVIEWER_MESSAGE = ('ACCB lines are edited by the designer; fail the BOQ to send it '
                         'back.')

#: B-3 Q2 — why OPX-273 cannot be edited, deactivated or marked mandatory on either
#: catalogue screen. Mandatory matters most: get_opex_mandatory_items() would return it and
#: the picker save would try to put a bare ACCB line on every sheet.
ACCB_CATALOGUE_LOCKED_MESSAGE = (
    f'{ACCB_CONFIGURED_CODE} "ACCB (configured)" is locked: every configured ACCB line on a '
    f'site BOQ links to it. It cannot be edited, deactivated or marked mandatory.')

_WHOLE_NUMBER = re.compile(r'^\d+$')


def _ratings_text(ratings):
    """'100A and 63A', '2×32A and 25A', '1600A, 1599A and 1598A'.

    Approved rules: high to low; a rating that repeats is written once as "N×R A"; the list
    is joined with ", " and the last pair with " and ".
    """
    counts = Counter(ratings)
    parts = [f'{counts[r]}×{r}A' if counts[r] > 1 else f'{r}A'
             for r in sorted(counts, reverse=True)]
    if len(parts) == 1:
        return parts[0]
    return ', '.join(parts[:-1]) + ' and ' + parts[-1]


def _side_text(feeders):
    """'2 Nos. with MCCB 4P 415V 100A and 63A' — one side of the description.

    "1 No." for a single feeder, "N Nos." otherwise. Breaker groups MCCB first, then MCB,
    joined with " + "; a breaker the side does not use is left out entirely.
    """
    count = len(feeders)
    head = '1 No.' if count == 1 else f'{count} Nos.'
    groups = []
    for breaker in ACCB_BREAKERS:
        ratings = [f['rating'] for f in feeders if f['breaker'] == breaker]
        if ratings:
            groups.append(f'{breaker} {ACCB_POLES_VOLTAGE} {_ratings_text(ratings)}')
    return f'{head} with ' + ' + '.join(groups)


def accb_description(config):
    """The approved BOQ-line text for one configured ACCB (prototype answer 5).

    THE SOURCE OF TRUTH for the wording. It is stored on BOQItem.description when the
    designer saves, so the download, the snapshots, the review panel, the SE drawings card
    and the vendor order snapshot all show it without knowing it was generated. The longest
    text the rules can produce is 416 characters (B-3 pre-flight P2); the line column is
    unbounded text.
    """
    sides = [f'{label} {_side_text(config[key])}' for key, label in ACCB_SIDES]
    return (f'ACCB {config["type"]}, ' + '; '.join(sides) +
            (', with SPD' if config['spd'] else ', without SPD'))


def _side_key(feeders):
    """One side's identity: breaker then rating high to low, so feeder order is irrelevant."""
    ordered = sorted(feeders, key=lambda f: (ACCB_BREAKERS.index(f['breaker']), -f['rating']))
    return ','.join(f'{f["breaker"]}-{f["rating"]}' for f in ordered)


def accb_config_key(config):
    """The SCM grouping identity — 'Type-2|SPD|IN:MCCB-100,MCCB-63|OUT:MCCB-150'.

    Two ACCBs are the same thing to buy when their type, their SPD tick and the MULTISET of
    (breaker, rating) on each side all match (B-3 "SCM grouping"). Sorting each side makes
    the key independent of the order the designer entered the feeders in, which is the one
    property the SCM aggregate depends on. Readable on purpose, so a group can be checked
    by eye in the database. At most about 430 characters; the column holds 512.
    """
    return '|'.join([
        config['type'],
        'SPD' if config['spd'] else 'NOSPD',
        'IN:' + _side_key(config['in']),
        'OUT:' + _side_key(config['out']),
    ])


def accb_summary(config):
    """'Type-2 · I/C 2 · O/G 1 · SPD' — the short form the activity log carries (B-3 Q6).

    BOUNDED BY CONSTRUCTION (at most 34 characters), so a log line built from it always
    fits ActivityLog.action's 255. The full text lives on the BOQ line and in the BOQ
    snapshots, never in the log.
    """
    return (f'{config["type"]} · I/C {len(config["in"])} · '
            f'O/G {len(config["out"])} · {"SPD" if config["spd"] else "no SPD"}')


def _parse_spd(raw):
    """A posted SPD tick. An unticked checkbox is absent, so absent or blank means False."""
    if raw is None:
        return False, None
    value = str(raw).strip().lower()
    if value in ('', '0', 'false', 'off'):
        return False, None
    if value in ('1', 'true', 'on'):
        return True, None
    return None, 'SPD must be ticked or not ticked.'


def _parse_side(label, breakers, ratings, errors):
    """One side's feeders from two parallel posted lists. Appends to `errors`; returns the
    feeders (possibly partial — only meaningful when no error was added)."""
    breakers = list(breakers or [])
    ratings = list(ratings or [])
    if len(breakers) != len(ratings):
        errors.append(f'{label}: every feeder needs both a breaker and a rating.')
        return []
    if not ACCB_MIN_FEEDERS <= len(breakers) <= ACCB_MAX_FEEDERS:
        errors.append(f'{label}: enter between {ACCB_MIN_FEEDERS} and {ACCB_MAX_FEEDERS} '
                      f'feeders (this ACCB has {len(breakers)}).')
        return []
    feeders = []
    for position, (breaker_raw, rating_raw) in enumerate(zip(breakers, ratings), start=1):
        breaker = str(breaker_raw or '').strip().upper()
        if breaker not in ACCB_BREAKERS:
            errors.append(f'{label} feeder {position}: choose MCB or MCCB.')
            continue
        rating_text = str(rating_raw if rating_raw is not None else '').strip()
        if not _WHOLE_NUMBER.fullmatch(rating_text):
            errors.append(f'{label} feeder {position}: the rating must be a whole number of '
                          f'amps from {ACCB_RATING_MIN} to {ACCB_RATING_MAX}.')
            continue
        rating = int(rating_text)
        if not ACCB_RATING_MIN <= rating <= ACCB_RATING_MAX:
            errors.append(f'{label} feeder {position}: {rating} A is outside '
                          f'{ACCB_RATING_MIN} to {ACCB_RATING_MAX} A.')
            continue
        feeders.append({'breaker': breaker, 'rating': rating})
    return feeders


def validate_accb_config(accb_type, spd, in_breakers, in_ratings, out_breakers, out_ratings):
    """Validate a posted configuration and return it normalised, or raise ValidationError
    naming EVERY fault at once.

    THE ONLY WAY A CONFIGURATION REACHES THE DATABASE. The ACCB view calls this before it
    writes anything, and the stored JSON is exactly what this returns — so the JSON column,
    which the database cannot check, only ever holds values this function accepted.

    Rules (B-3 T1): type in Type-1 / Type-2; 1 to 20 feeders on each side; breaker MCB or
    MCCB; rating a whole number 1 to 1600; SPD a boolean. No check that the output rating
    covers the inputs (prototype answer 3 — the designer's judgement).
    """
    errors = []
    accb_type = str(accb_type or '').strip()
    if accb_type not in ACCB_TYPES:
        errors.append('Choose the ACCB type: Type-1 or Type-2.')
    has_spd, spd_error = _parse_spd(spd)
    if spd_error:
        errors.append(spd_error)
    incomers = _parse_side('I/C', in_breakers, in_ratings, errors)
    outgoings = _parse_side('O/G', out_breakers, out_ratings, errors)
    if errors:
        raise ValidationError(errors)
    return {'type': accb_type, 'spd': has_spd, 'in': incomers, 'out': outgoings}
