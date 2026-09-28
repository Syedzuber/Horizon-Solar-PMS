"""One reader for a number a person typed into a form.

WHY THIS EXISTS. Before it, each view read its quantity boxes its own way, and the ways
disagreed about what a bad number means:

* `Decimal()` with `InvalidOperation -> None` (boq_detail) quietly CLEARED a quantity the
  user had typed, and let a negative through untouched.
* `_safe_decimal()` (the GRN views, the challan raise) deletes every character that is not
  a digit or a point before converting, so "-5" was stored as 5 and "abc5" as 5.
* `int(float(...))` (damaged quantity) truncated "2.7" to 2 without saying so.
* None of them refused a third decimal place. The columns hold two, so the database
  ROUNDED it — "38.405" was saved as 38.41 and the user was told "saved".

This module REFUSES instead of guessing. A value that is not exactly what the column can
hold comes back as a ValidationError whose message names the field, and the view shows it.
Nothing here rounds, clamps or strips characters.

WHERE THE LIMIT LIVES. The browser's `step` attribute is a typing aid, not a rule: the
quantity inputs use step="any" so the spinner never fights a fractional quantity, which
means the two-decimal limit is enforced HERE, on the server, and only here.
"""

from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError


def decimal_field_max(model, field_name):
    """The largest value a model's DecimalField column can hold, e.g. 99999999.99 for
    max_digits=10, decimal_places=2. Read from the field so a later change to the
    column's size moves every caller's limit with it."""
    field = model._meta.get_field(field_name)
    whole_digits = field.max_digits - field.decimal_places
    return Decimal(10) ** whole_digits - Decimal(1).scaleb(-field.decimal_places)


def parse_decimal_input(raw, *, places, min_value=None, max_value=None, field_label):
    """Read one typed number. Returns a Decimal, or None when the box was left empty.

    Raises ValidationError, with a message that starts with `field_label`, for anything
    that is not a finite number with at most `places` decimal places inside
    [min_value, max_value]. `places=0` means a whole number.

    TRAILING ZEROS DO NOT COUNT as decimal places: "38.400" is 38.40 exactly, so it is
    accepted at places=2 and returned as typed. "38.405" is not, and is refused — never
    rounded to 38.41.

    Pass `max_value` as the largest value the destination column can hold. Without it an
    over-long number reaches the database and fails there as a server error instead of
    as a message.
    """
    text = '' if raw is None else str(raw).strip()
    if not text:
        return None

    try:
        value = Decimal(text)
    except InvalidOperation:
        raise ValidationError(f'{field_label}: "{text}" is not a number.')

    # Decimal() accepts "NaN", "Infinity" and "-inf". No column can store them.
    if not value.is_finite():
        raise ValidationError(f'{field_label}: "{text}" is not a number.')

    # Counted from the digit tuple rather than with normalize() or quantize(): both of
    # those go through the decimal context's 28-digit precision and would ROUND a long
    # enough input before it was measured, which is the one thing this function exists
    # not to do. Trailing zeros after the point are dropped first, so 38.400 counts as 2.
    _, digits, exponent = value.as_tuple()
    while exponent < 0 and len(digits) > 1 and digits[-1] == 0:
        digits = digits[:-1]
        exponent += 1
    if exponent < -places:
        if places == 0:
            raise ValidationError(f'{field_label}: "{text}" must be a whole number.')
        raise ValidationError(
            f'{field_label}: "{text}" has more than {places} decimal places. '
            f'Enter it to {places} places — it is not rounded for you.')

    if min_value is not None and value < min_value:
        if min_value == 0:
            raise ValidationError(f'{field_label}: "{text}" cannot be negative.')
        raise ValidationError(f'{field_label}: "{text}" must be at least {min_value}.')
    if max_value is not None and value > max_value:
        raise ValidationError(f'{field_label}: "{text}" is larger than the maximum of {max_value}.')

    # "-0" and "-0.00" are valid Decimals equal to zero. Store them as plain zero, so a
    # negative sign never reaches a column that the rest of the app reads as non-negative.
    if value.is_zero():
        value = abs(value)
    return value
