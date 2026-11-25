"""
FIN-M-B5-4a — the ONE Indian-grouping rupee formatter, shared by views.py, the receipt
pages (views_receipts.py) and the receipt writer (receipts.py).

Moved here from views.py unchanged (ruling 24 Nov, B5-3 deferral): receipts.py cannot
import from views.py, because views.py imports receipts at module level, so the writer's
overpayment warning used to print raw '₹110000.00'. views.py imports both functions back
under their old names (_format_inr, _milestone_rupees), so every existing caller and the
text on every existing page are unchanged.

Constants and pure functions only — no project imports, so any module may import this
without a cycle.
"""
from decimal import ROUND_HALF_UP, Decimal


def format_inr(value):
    """Whole rupees in Indian grouping: Decimal('200000.00') -> '2,00,000',
    Decimal('12345678.50') -> '1,23,45,679'. Formatted here because Django's
    intcomma groups in thousands (200,000), which a CEO reading lakhs would misread."""
    digits = str(int((value or Decimal('0')).quantize(Decimal('1'), rounding=ROUND_HALF_UP)))
    head, tail = digits[:-3], digits[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return ','.join(groups + [tail])


def format_rupees(value):
    """An amount as people read it: '₹1,35,000', '₹1,00,000.40', 'blank'.

    Not format_inr() alone, which rounds to whole rupees: in an audit line a 40-paise
    correction would then read "₹1,00,000 → ₹1,00,000". Paise are shown only when they
    are not zero. The sign is handled here because format_inr() mis-groups a negative
    ('-,50,000'), and milestone_receive still accepts one (a secondary finding)."""
    if value is None:
        return 'blank'
    value = Decimal(value)
    if not value.is_finite():
        # NaN/Infinity cannot be grouped; say what was stored rather than raise inside
        # a log call.
        return str(value)
    sign = '-' if value < 0 else ''
    value = abs(value).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    whole = Decimal(int(value))
    text = format_inr(whole)
    paise = int((value - whole) * 100)
    if paise:
        text += f'.{paise:02d}'
    return f'{sign}₹{text}'
