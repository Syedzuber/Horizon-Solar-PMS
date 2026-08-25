"""
Report screens — read-only management reporting.

A separate module from views.py (which is ~10,800 lines) for the reason design_views.py
records at its own head: this is a self-contained new surface, urls.py imports it
alongside `views`, and no existing view is modified. Every figure on these pages comes
from projects/reports.py, the same builder the EOD digest email calls — the page and the
email cannot drift apart because there is only one query path.

READ-ONLY. Nothing in this module writes.
"""

import logging

from django.contrib import messages
from django.http import HttpResponseForbidden
from django.shortcuts import render
from django.utils import timezone
from django.utils.dateparse import parse_date

from .decorators import login_required, role_required
from .permissions import can_view_user_status_report
from .reports import build_user_status_rows

logger = logging.getLogger(__name__)


def _resolve_report_date(request):
    """Read ?date=YYYY-MM-DD, falling back to today (IST) on anything unusable.

    Returns (date, warning_or_None). Never raises: a hand-edited or bookmarked URL is a
    normal thing to receive, and a report page answering with a 500 for a bad query
    string would be worse than answering for today. Three fallback cases:
      - absent          -> today, no warning (the ordinary case)
      - unparseable     -> today, warned
      - in the future   -> today, warned (no data can exist there)
    """
    # timezone.localdate() — NOT date.today(). USE_TZ=True and the server clock is UTC,
    # so date.today() would roll over 5h30m early for an IST report.
    today = timezone.localdate()
    raw = (request.GET.get('date') or '').strip()
    if not raw:
        return today, None

    # parse_date returns None when the string does not LOOK like a date, but RAISES
    # ValueError when it looks like one and the values are out of range ('2026-13-45'
    # matches the regex, then date(month=13) throws). Both are the same thing to a
    # reader of this page, so both fall back — catching only one of them ships a 500.
    try:
        parsed = parse_date(raw)
    except ValueError:
        parsed = None
    if parsed is None:
        return today, f'Could not read the date "{raw}" — showing today instead.'
    if parsed > today:
        return today, f'{parsed:%d %b %Y} is in the future — showing today instead.'
    return parsed, None


@login_required
@role_required(['CEO', 'Admin', 'System Admin'])
def ceo_daily_report(request):
    """Per-user task and login status for one day.
    Access: CEO, Admin, System Admin.
    """
    # Two gates on purpose, and they are NOT redundant — they catch different users.
    # @role_required is the decorator pattern every gated view in views.py uses and
    # produces the standard message-and-redirect bounce for a wrong-role user; but it
    # falls back to treating a user with NO UserProfile as 'Admin' (decorators.py:106-109),
    # so a bare `createsuperuser` account would walk straight through it. The predicate
    # below refuses that case, and it is also the one that OWNS the rule per R-13: if this
    # report's audience ever changes, permissions.py is the single place it changes.
    if not can_view_user_status_report(request.user):
        return HttpResponseForbidden('CEO, Admin or System Admin only.')

    report_date, warning = _resolve_report_date(request)
    if warning:
        messages.warning(request, warning)

    report = build_user_status_rows(report_date)

    return render(request, 'projects/reports/ceo_daily_report.html', {
        'report_date': report_date,
        'rows':        report['rows'],
        'totals':      report['totals'],
        # Bounds the date picker so the browser refuses a future date before the
        # server has to warn about one.
        'max_date':    timezone.localdate(),
    })
