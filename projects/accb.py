"""
B-3 — the ACCB configurator's rules: what a configured ACCB says, how two are judged
identical, and what a valid one is.

PURE, AND THAT IS THE POINT. Nothing here touches the database, a request or a model.
The functions below are the single definition of the approved description wording, of the
SCM grouping identity, of the one-line summary the activity log carries, and of the
server-side validation. The ACCB view, the B-4 copy, the SCM aggregate and the tests all
call these; the designer's live preview in opex_boq_entry.html is a JavaScript COPY of
accb_description() for display only, and the server always regenerates the text from the
stored configuration.

B-3c (Zuber, 23 Nov) — SWITCHGEAR FROM A FIXED LIST, SPD PER O/G FEEDER. Every feeder picks
one of the 11 options in ACCB_SWITCHGEAR (no typed rating, no separate breaker choice) and
each O/G feeder carries its own SPD tick. This supersedes ruling 26 and the B-3 decisions
on ratings and on one SPD per ACCB.

THIS MODULE IMPORTS NO MODELS. models.py imports the constants from here (the catalogue
code, the type list), so an import in the other direction would be circular.

A CONFIGURATION is a plain dict, the shape validate_accb_config() returns and
ACCBConfig.as_config() rebuilds:

    {'type': 'Type-2',
     'in':  [{'breaker': 'MCCB', 'rating': 100}, {'breaker': 'MCB', 'rating': 63}],
     'out': [{'breaker': 'MCCB', 'rating': 200, 'spd': True}]}

Feeders are kept in the designer's order. Order matters for display (the card shows them as
entered) and does not matter for identity (accb_config_key sorts them).
"""
from collections import Counter

from django.core.exceptions import ValidationError

#: The catalogue row every configured ACCB line links to (Zuber, B-3 Q2). OUTSIDE the
#: OPX-218..OPX-272 block the B-1 mapping workbook reserves. Left out of the picker, upload
#: and correction catalogue by get_opex_boq_catalogue(), and locked on both catalogue
#: screens, because a bare line on this code would have no configuration behind it.
ACCB_CONFIGURED_CODE = 'OPX-273'

#: Ruling 28 / F7 — a label the designer picks. Exactly these two.
ACCB_TYPES = ('Type-1', 'Type-2')

#: Ruling 30 — no business maximum; the UI (and therefore the server) caps each side at 20.
ACCB_MIN_FEEDERS = 1
ACCB_MAX_FEEDERS = 20

#: Ruling 31 — every ACCB is 4-pole, 415 V. Fixed text, never stored.
ACCB_POLES_VOLTAGE = '4P 415V'

#: The words every MCCB option shares after its rating (B-3c ruling 1). Praveen's sheet
#: spaces the commas unevenly ("100A,4P, 36kA,MCCB"); normalised to ", " (Zuber, 23 Nov).
_MCCB_TEXT = ('4P, 36kA, MCCB with S/C & O/L TM Based Extended Rotary Handle + '
              'Spreader Terminals')

#: B-3c ruling 1 — THE switchgear list: (breaker, rating, text), in the ruling's order.
#: The same 11 on the I/C and the O/G side. The dropdown, the validator, the description,
#: the grouping key and the tests all read this one constant; nothing else may list them.
#: Every rating belongs to exactly one breaker, which is what lets the description and the
#: key sort by rating alone.
ACCB_SWITCHGEAR = (
    ('MCB', 25, '25A, 4P, MCB'),
    ('MCB', 30, '30A, 4P, MCB'),
    ('MCB', 40, '40A, 4P, MCB'),
    ('MCB', 63, '63A, 4P, MCB'),
    ('MCCB', 100, f'100A, {_MCCB_TEXT}'),
    ('MCCB', 125, f'125A, {_MCCB_TEXT}'),
    ('MCCB', 200, f'200A, {_MCCB_TEXT}'),
    ('MCCB', 250, f'250A, {_MCCB_TEXT}'),
    ('MCCB', 320, f'320A, {_MCCB_TEXT}'),
    ('MCCB', 400, f'400A, {_MCCB_TEXT}'),
    ('MCCB', 630, f'630A, {_MCCB_TEXT}'),
)

#: (breaker, rating) -> the option's text.
_TEXT_BY_PAIR = {(breaker, rating): text for breaker, rating, text in ACCB_SWITCHGEAR}

#: Zuber, B-3 Q7 — the upload refuses a file that carries the ACCB code, in these words.
ACCB_UPLOAD_REFUSED_MESSAGE = ('ACCB lines are managed in the ACCB section, not by upload. '
                               'Remove the ACCB rows from the sheet and upload again.')

#: What the correction screen says to a quantity posted on an ACCB line. B-3 P7 refused
#: reviewers ACCBs altogether; DW-2b (DW-R3) gives them the configurator instead, so the
#: quantity box stays refused and points at it (Zuber, DW-2b Q8).
ACCB_REVIEWER_MESSAGE = 'Change an ACCB with its Edit button; its quantity is always 1.'

#: DW-2b — the reviewer's ACCB refusals, worded once for the view and its screen.
#: Answer 3: never under a change request, for the Head either (no BC-1 exception).
ACCB_CHANGE_REQUEST_MESSAGE = ("A change request is open on this design, so ACCBs can't be "
                               "changed until it is decided.")
#: Answer 6: a line committed to SCM is not changed here. The short form is the GET's note.
ACCB_ON_ORDER_NOTE = 'On an SCM order'
ACCB_ON_ORDER_MESSAGE = ("ACCB {number} is on an SCM order, so it can't be changed or "
                         "removed here. Nothing was saved.")
#: A6: removing the BOQ's only DCDB, ACDB or ACCB (DW-1's test).
ACCB_LAST_BOARD_MESSAGE = ("This is the BOQ's only DCDB, ACDB or ACCB. Add the replacement "
                           "first, then remove this one.")
#: A4: every reviewer ACCB action states why. Shown beside the form, in the pop-up.
ACCB_REASON_REQUIRED_MESSAGE = ('Give a reason for this ACCB change. Nothing was saved.')

#: B-3c answer 7 — what Duplicate says about an ACCB saved before the fixed list.
ACCB_REENTER_MESSAGE = 're-enter the switchgear first'

#: B-3 Q2 — why OPX-273 cannot be edited, deactivated or marked mandatory on either
#: catalogue screen. Mandatory matters most: get_opex_mandatory_items() would return it and
#: the picker save would try to put a bare ACCB line on every sheet.
ACCB_CATALOGUE_LOCKED_MESSAGE = (
    f'{ACCB_CONFIGURED_CODE} "ACCB (configured)" is locked: every configured ACCB line on a '
    f'site BOQ links to it. It cannot be edited, deactivated or marked mandatory.')


def accb_option_code(breaker, rating):
    """'MCCB-100' — the value a feeder's dropdown posts, and the token the key is made of.

    Names a (breaker, rating) pair rather than a list position, so a stored feeder, a POST
    and a grouping key all read the same and the codes survive a reordering of the list.
    """
    return f'{breaker}-{rating}'


#: option code -> (breaker, rating). The validator's whitelist.
_PAIR_BY_CODE = {accb_option_code(breaker, rating): (breaker, rating)
                 for breaker, rating, _text in ACCB_SWITCHGEAR}


def accb_switchgear_choices():
    """[{'code', 'rating', 'text'}] in the list's order — what the dropdown offers, and what
    the card's live preview sorts (by rating) and writes (the text) from."""
    return [{'code': accb_option_code(breaker, rating), 'rating': rating, 'text': text}
            for breaker, rating, text in ACCB_SWITCHGEAR]


def _count_text(count):
    """'1 No.' for a single feeder, 'N Nos.' otherwise (ruling 3)."""
    return '1 No.' if count == 1 else f'{count} Nos.'


def _incomer_groups(feeders):
    """'2 x 40A, 4P, MCB; 1 x 25A, 4P, MCB' — identical options grouped, rating high to low."""
    counts = Counter((f['breaker'], f['rating']) for f in feeders)
    return '; '.join(f'{counts[pair]} x {_TEXT_BY_PAIR[pair]}'
                     for pair in sorted(counts, key=lambda pair: -pair[1]))


def _outgoing_groups(feeders):
    """'1 x <text>, with SPD; 1 x <text>, without SPD' — grouped by option AND SPD, rating
    high to low, then "with SPD" before "without SPD" for the same option."""
    counts = Counter((f['breaker'], f['rating'], f['spd']) for f in feeders)
    ordered = sorted(counts, key=lambda group: (-group[1], not group[2]))
    return '; '.join(
        f'{counts[group]} x {_TEXT_BY_PAIR[group[:2]]}, '
        f'{"with SPD" if group[2] else "without SPD"}'
        for group in ordered)


def accb_description(config):
    """The approved BOQ-line text for one configured ACCB (B-3c ruling 3, tested word for
    word):

        ACCB Type-N, 4P 415V. I/C Feeder <count>: <groups>. O/G Feeder <count>: <groups>.

    THE SOURCE OF TRUTH for the wording. It is stored on BOQItem.description when the
    designer saves, so the download, the snapshots, the review panel, the SE drawings card
    and the vendor order line all show it without knowing it was generated. The longest
    text the rules can produce is 2,446 characters (B-3c pre-flight P5); every column it is
    copied into automatically is unbounded text.
    """
    return (f'ACCB {config["type"]}, {ACCB_POLES_VOLTAGE}. '
            f'I/C Feeder {_count_text(len(config["in"]))}: {_incomer_groups(config["in"])}. '
            f'O/G Feeder {_count_text(len(config["out"]))}: '
            f'{_outgoing_groups(config["out"])}.')


def accb_config_key(config):
    """The SCM grouping identity — 'Type-2|IN:MCCB-100,MCB-63|OUT:MCCB-200+SPD'.

    Two ACCBs are the same thing to buy when their type, the MULTISET of options on the I/C
    side and the multiset of (option, SPD) on the O/G side all match (B-3c ruling 4).
    Sorting each side makes the key independent of feeder order — including which of two
    identical O/G feeders carries an SPD tick — which is the one property the SCM aggregate
    depends on. Readable on purpose, so a group can be checked by eye in the database.

    At most 453 characters; the column (and VendorOrderLine.accb_config_key) holds 512. A
    B-3 key always had SPD or NOSPD as its second part, so an old key can never equal a
    new one.
    """
    incomers = sorted(config['in'], key=lambda f: -f['rating'])
    outgoings = sorted(config['out'], key=lambda f: (-f['rating'], not f['spd']))
    return '|'.join([
        config['type'],
        'IN:' + ','.join(accb_option_code(f['breaker'], f['rating']) for f in incomers),
        'OUT:' + ','.join(accb_option_code(f['breaker'], f['rating']) +
                          ('+SPD' if f['spd'] else '') for f in outgoings),
    ])


def accb_has_spd(config):
    """True when any O/G feeder has SPD — what ACCBConfig.has_spd stores (B-3c answer 2)."""
    return any(f['spd'] for f in config['out'])


def accb_summary(config):
    """'Type-2 · I/C 2 · O/G 3 · SPD 2' — the short form the activity log carries (B-3 Q6;
    B-3c answer 4). The SPD figure is the number of O/G feeders with SPD.

    BOUNDED BY CONSTRUCTION (at most 33 characters), so a log line built from it always
    fits ActivityLog.action's 255. The full text lives on the BOQ line and in the BOQ
    snapshots, never in the log.
    """
    # Read defensively: the save and remove log lines summarise the STORED configuration,
    # and one saved before B-3c has no per-feeder SPD. Such a feeder counts as no SPD.
    spd_count = sum(1 for f in config['out'] if isinstance(f, dict) and f.get('spd') is True)
    return (f'{config["type"]} · I/C {len(config["in"])} · '
            f'O/G {len(config["out"])} · SPD {spd_count}')


def _parse_spd(raw):
    """One O/G feeder's posted SPD value: '1' ticked, '0' not ticked, anything else refused.

    The card posts a hidden '1'/'0' per O/G feeder rather than a checkbox, because an
    unticked checkbox posts nothing and the SPD list would fall out of step with the
    feeder list. So a missing value is a fault, never "not ticked".
    """
    value = str(raw).strip() if raw is not None else ''
    if value == '1':
        return True
    if value == '0':
        return False
    return None


def _parse_side(label, options, errors):
    """One side's (breaker, rating) pairs from its posted option codes. Appends to
    `errors`; returns the pairs (possibly partial — only meaningful when no error was
    added)."""
    options = list(options or [])
    if not ACCB_MIN_FEEDERS <= len(options) <= ACCB_MAX_FEEDERS:
        errors.append(f'{label}: enter between {ACCB_MIN_FEEDERS} and {ACCB_MAX_FEEDERS} '
                      f'feeders (this ACCB has {len(options)}).')
        return []
    pairs = []
    for position, raw in enumerate(options, start=1):
        pair = _PAIR_BY_CODE.get(str(raw if raw is not None else '').strip())
        if pair is None:
            # A rating not in the list, a swapped breaker/rating pair and a blank all land
            # here: none of them is one of the 11 options.
            errors.append(f'{label} feeder {position}: choose the switchgear from the list.')
            continue
        pairs.append(pair)
    return pairs


def validate_accb_config(accb_type, in_options, out_options, out_spds, in_spds=()):
    """Validate a posted configuration and return it normalised, or raise ValidationError
    naming EVERY fault at once.

    THE ONLY WAY A CONFIGURATION REACHES THE DATABASE. The ACCB view calls this before it
    writes anything, and the stored JSON is exactly what this returns — so the JSON column,
    which the database cannot check, only ever holds values this function accepted. The
    B-4 copy and Duplicate re-check a stored configuration through the same function
    (stored_accb_config).

    Rules (B-3c T1): type in Type-1 / Type-2; 1 to 20 feeders on each side; each feeder one
    of the 11 options in ACCB_SWITCHGEAR; each O/G feeder a '1'/'0' SPD value; no SPD value
    on any I/C feeder. No check that the output rating covers the inputs (prototype answer
    3 — the designer's judgement).
    """
    errors = []
    accb_type = str(accb_type or '').strip()
    if accb_type not in ACCB_TYPES:
        errors.append('Choose the ACCB type: Type-1 or Type-2.')
    if list(in_spds or []):
        errors.append('I/C feeders have no SPD; SPD is ticked on O/G feeders only.')
    incomers = _parse_side('I/C', in_options, errors)
    outgoings = _parse_side('O/G', out_options, errors)
    out_spds = list(out_spds or [])
    spds = []
    if outgoings or out_options:
        if len(out_spds) != len(list(out_options or [])):
            errors.append('O/G: every feeder needs its SPD ticked or not ticked.')
        else:
            for position, raw in enumerate(out_spds, start=1):
                spd = _parse_spd(raw)
                if spd is None:
                    errors.append(f'O/G feeder {position}: SPD must be ticked or not ticked.')
                spds.append(spd)
    if errors:
        raise ValidationError(errors)
    return {'type': accb_type,
            'in': [{'breaker': b, 'rating': r} for b, r in incomers],
            'out': [{'breaker': b, 'rating': r, 'spd': spd}
                    for (b, r), spd in zip(outgoings, spds)]}


def stored_feeder_option(feeder):
    """A stored feeder's option code, or '' when it is not one of the 11 (a configuration
    saved before B-3c, or a malformed entry). Never raises."""
    if not isinstance(feeder, dict):
        return ''
    code = accb_option_code(feeder.get('breaker'), feeder.get('rating'))
    return code if code in _PAIR_BY_CODE else ''


def _stored_spd(feeder):
    """A stored O/G feeder's SPD as the '1'/'0' the validator takes, or None if it has none."""
    spd = feeder.get('spd') if isinstance(feeder, dict) else None
    return {True: '1', False: '0'}.get(spd) if isinstance(spd, bool) else None


def stored_accb_config(accb_type, feeders):
    """Re-check a STORED configuration against today's rules; return it normalised or raise
    ValidationError.

    Used where a saved ACCB is copied without the designer re-entering it — Duplicate
    (B-3c answer 7) and the B-4 copy from another site — so a configuration saved before
    the fixed list (a typed rating, one SPD per ACCB) can never be copied into a new one.
    Goes through validate_accb_config(), so there is one definition of "valid".
    """
    feeders = feeders if isinstance(feeders, dict) else {}
    incomers = feeders.get('in') or []
    outgoings = feeders.get('out') or []
    return validate_accb_config(
        accb_type,
        [stored_feeder_option(f) for f in incomers],
        [stored_feeder_option(f) for f in outgoings],
        [_stored_spd(f) for f in outgoings],
        # An I/C feeder carrying an SPD value is refused, as it would be from a POST.
        [f.get('spd') for f in incomers if isinstance(f, dict) and 'spd' in f])
