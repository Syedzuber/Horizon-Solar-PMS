"""
WhatsApp template values made safe to send (NTF-DCG Session 3a, ruling W4).

Every value a registry event sends to Interakt passes through sanitise_params() once,
just before the send is handed off. Meta rejects a template message whose variable holds
a newline, a tab or more than four spaces in a row, and rejects (or Interakt truncates)
one whose header or body is too long. A rejection costs the person the whole message,
so the values are cleaned and cut here rather than refused.

THE RULES (W4, limits from Zuber's Q5 answer):
  * newlines, tabs and runs of 4+ spaces become one space; the value is trimmed;
  * an empty value becomes "-" (Meta refuses an empty variable);
  * the header value is cut so the RENDERED header (its fixed text plus the value) is at
    most HEADER_LIMIT characters;
  * each body value is cut to BODY_VALUE_LIMIT characters;
  * if the RENDERED body (its fixed text plus every value) is still over BODY_LIMIT, the
    longest values are cut first, each to the same length, until it fits.
A cut value ends with ELLIPSIS, which counts towards its length.

LENGTH IS COUNTED IN PYTHON CHARACTERS (code points). A cut never separates a base letter
from the combining marks after it, so a Devanagari syllable such as "कि" is kept whole or
dropped whole rather than leaving a stray vowel sign.

PURE: no Django, no app imports, so notify_registry can import the limits without a cycle
and the rules are unit-tested without a database (tests_notify_whatsapp).
"""

import re
import unicodedata

HEADER_LIMIT = 60          # rendered header, fixed text included
BODY_LIMIT = 1024          # rendered body, fixed text included
BODY_VALUE_LIMIT = 250     # one body value
ELLIPSIS = '…'        # "…", one character
EMPTY_VALUE = '-'

# A line break or tab with any whitespace around it is one space: "a \n  b" -> "a b".
# \v, \f and the Unicode line/paragraph separators count as line breaks too.
_BREAK_RE = re.compile(r'[ ]*[\r\n\t\v\f\u0085  ][\s]*')
_LONG_SPACE_RE = re.compile(r' {4,}')


def clean_value(value):
    """`value` as one trimmed line: breaks and tabs to a space, 4+ spaces to one,
    empty to "-". None counts as empty."""
    text = '' if value is None else str(value)
    text = _BREAK_RE.sub(' ', text)
    text = _LONG_SPACE_RE.sub(' ', text).strip()
    return text or EMPTY_VALUE


def cut(text, limit):
    """`text` at most `limit` characters; a cut one ends with ELLIPSIS.

    The cut point steps back over combining marks so a letter keeps its marks. A limit
    below 1 still returns ELLIPSIS: a variable may never be empty.
    """
    if len(text) <= limit:
        return text
    end = max(limit - 1, 0)
    # text[end] is the first character dropped. While it is a mark (category M*), the
    # character before it is the letter it belongs to: drop that letter as well.
    while end > 0 and unicodedata.category(text[end]).startswith('M'):
        end -= 1
    return text[:end].rstrip() + ELLIPSIS


def _level(lengths, budget):
    """The largest L such that sum(min(n, L) for n in lengths) <= budget, at least 1.

    Cutting every value longer than L down to L removes characters from the longest
    values first and never shortens a value that was already short enough.
    """
    low, high = 1, max(lengths)
    while low < high:
        mid = (low + high + 1) // 2
        if sum(min(n, mid) for n in lengths) <= budget:
            low = mid
        else:
            high = mid - 1
    return low


def sanitise_params(params, *, header_fixed_chars=0, body_fixed_chars=0):
    """[header, *body] cleaned and cut by the W4 rules, as a new list of str.

    params             : the builder's output, header first (ruling W1).
    header_fixed_chars : characters of the approved header outside its variable.
    body_fixed_chars   : characters of the approved body outside its variables.
    """
    values = [clean_value(v) for v in params]
    if not values:
        return values
    header = cut(values[0], HEADER_LIMIT - header_fixed_chars)
    body = [cut(v, BODY_VALUE_LIMIT) for v in values[1:]]
    budget = BODY_LIMIT - body_fixed_chars
    if body and sum(len(v) for v in body) > budget:
        level = _level([len(v) for v in body], budget)
        body = [cut(v, level) for v in body]
    return [header] + body


def describe_params(params):
    """One line naming each value's slot, for NotificationLog.message (ruling Q7) and
    test_whatsapp's output: 'header: X | body: a | b'."""
    params = list(params)
    if not params:
        return ''
    text = f'header: {params[0]}'
    if len(params) > 1:
        text += ' | body: ' + ' | '.join(params[1:])
    return text
