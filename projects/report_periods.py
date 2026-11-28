"""
RPT-B1 — the reporting periods every report is asked for.

    period, error = parse_period(request.GET)
    period.start_dt <= row.timestamp < period.end_dt          # aware IST datetimes
    period.start_date <= row.date <= period.end_date          # IST calendar days
    period.previous                                            # the comparison period

THE SIX PERIODS (decision D2, 28 Nov 2026)
    today       the IST calendar day of timezone.localdate()
    yesterday   the day before it
    week        Monday of this week to today (the week starts Monday)
    month       the 1st of this month to today
    date        one chosen day, not after today
    range       From to To, both inclusive, at most MAX_RANGE_DAYS, not after today
    No chosen day (On date, or a range's From) may be before REPORTS_START (RPT-B2a).

THE PREVIOUS PERIOD (D3) is the same length immediately before:
    today / yesterday / date  -> the day before
    week Mon..Wed             -> the previous Mon..Wed
    month 1..28 Nov           -> 1..28 Oct, the end clamped to the shorter month's last day
                                 (1..31 Mar -> 1..28 Feb, or 29 in a leap year)
    range of N days           -> the N days just before From
It is the one place a report's "vs" tile reads, so two reports can never compare against
different windows.

IST, NEVER THE SERVER CLOCK. USE_TZ=True and TIME_ZONE='Asia/Kolkata': timezone.localdate()
and timezone.make_aware() both work in IST, the same calls site_breakdown() uses for its
day, so a report's "today" and the dashboards' "today" cut at the same midnight.
date.today() would roll over 5 h 30 min early on a UTC server.

PURE. No query, no request object: `params` is anything with .get() (a QueryDict, a dict,
the management command's options).
"""

import calendar
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Optional

from django.utils import timezone
from django.utils.dateparse import parse_date


PERIOD_TODAY = 'today'
PERIOD_YESTERDAY = 'yesterday'
PERIOD_WEEK = 'week'
PERIOD_MONTH = 'month'
PERIOD_DATE = 'date'
PERIOD_RANGE = 'range'
PERIOD_KEYS = (PERIOD_TODAY, PERIOD_YESTERDAY, PERIOD_WEEK, PERIOD_MONTH, PERIOD_DATE,
               PERIOD_RANGE)

#: Longest Date range a report may be asked for (ruling RPT-R6). The RPT-A timings put a
#: 92-day period under 20 ms per query at production size, so the cap is about keeping
#: a report readable, not about load.
MAX_RANGE_DAYS = 92

# The words a person sees when a period cannot be read. Plain sentences, shown as they are.
ERROR_PICK_DATE = 'Pick a date'
ERROR_FUTURE = "Dates after today can't be chosen"
ERROR_FROM_AFTER_TO = 'From date is after To date'
ERROR_TOO_LONG = f'Date range can be at most {MAX_RANGE_DAYS} days'

#: The first day a report may cover (RPT-B2a, Zuber 28 Nov 2026). Coded `user_login` rows
#: begin on 28 Jul 2026; before it, sign-ins were logged with a blank action_code and
#: human_activity_q() counts them as actions, so an earlier period would credit people
#: with actions they never took. The pages set it as the date inputs' min as well.
REPORTS_START = date(2026, 7, 28)
ERROR_TOO_EARLY = 'Reports start on 28 Jul 2026'

EN_DASH = '–'


@dataclass(frozen=True)
class Period:
    """One reporting window. Dates are IST calendar days, both inclusive; the datetimes
    are the aware IST midnights around them, end EXCLUSIVE, so a timestamp filter is
    `start_dt <= t < end_dt` and a 23:59:59.9 IST row is in its own day."""
    key: str
    start_date: date
    end_date: date                  # inclusive
    start_dt: datetime              # aware, 00:00 IST on start_date
    end_dt: datetime                # aware, 00:00 IST the day after end_date (exclusive)
    days: int
    label: str
    previous: Optional['Period'] = None   # D3; None on a previous period itself

    @property
    def includes_today(self):
        """True when the period runs to today, so its last day is still filling up."""
        return self.end_date >= timezone.localdate()


def _day_text(d):
    # Built by hand rather than with strftime('%-d'): that flag does not exist on Windows,
    # where this code is also developed and tested.
    return f'{d.day} {d:%b %Y}'


def span_text(start, end):
    """'28 Nov 2026', '23 Nov – 28 Nov 2026' or '28 Dec 2025 – 3 Jan 2026'."""
    if start == end:
        return _day_text(start)
    if start.year == end.year:
        return f'{start.day} {start:%b} {EN_DASH} {_day_text(end)}'
    return f'{_day_text(start)} {EN_DASH} {_day_text(end)}'


def _bounds(start, end):
    """The aware IST datetimes around [start, end]: 00:00 on start, 00:00 after end."""
    return (timezone.make_aware(datetime.combine(start, time.min)),
            timezone.make_aware(datetime.combine(end + timedelta(days=1), time.min)))


def _make(key, start, end, label, previous=None):
    start_dt, end_dt = _bounds(start, end)
    return Period(key=key, start_date=start, end_date=end, start_dt=start_dt, end_dt=end_dt,
                  days=(end - start).days + 1, label=label, previous=previous)


def _previous_dates(key, start, end):
    """D3: the window a period is compared against."""
    if key == PERIOD_WEEK:
        # The same weekdays one week earlier: on a Wednesday, Mon..Wed against Mon..Wed.
        return start - timedelta(days=7), end - timedelta(days=7)
    if key == PERIOD_MONTH:
        # The same days of the previous month, clamped: on 31 Mar the previous month has
        # no 31st, so 1..31 Mar compares with all of February.
        prev_last = start - timedelta(days=1)
        prev_first = prev_last.replace(day=1)
        last_day = calendar.monthrange(prev_first.year, prev_first.month)[1]
        return prev_first, prev_first.replace(day=min(end.day, last_day))
    # today / yesterday / date / range: the same number of days just before.
    length = (end - start).days + 1
    return start - timedelta(days=length), start - timedelta(days=1)


def _previous_label(key, start, end):
    if key == PERIOD_RANGE:
        days = (end - start).days + 1
        return f'vs previous {days} day{"s" if days != 1 else ""}, {span_text(start, end)}'
    return f'vs {span_text(start, end)}'


def _label(key, start, end, today):
    if key == PERIOD_TODAY:
        return f'Today, {_day_text(start)}'
    if key == PERIOD_YESTERDAY:
        return f'Yesterday, {_day_text(start)}'
    if key == PERIOD_WEEK:
        return f'This week, {span_text(start, end)}'
    if key == PERIOD_MONTH:
        return f'This month, {span_text(start, end)}'
    if key == PERIOD_DATE:
        return _day_text(start)
    days = (end - start).days + 1
    # Edge case 6: a range ending today includes today's partial day, and says so.
    tail = ', to today' if end == today else ''
    return f'{span_text(start, end)} ({days} day{"s" if days != 1 else ""}{tail})'


def build_period(key, start, end, today):
    """The Period for `key` over [start, end] with its previous period attached. The
    caller has already validated the dates; this only shapes them."""
    prev_start, prev_end = _previous_dates(key, start, end)
    previous = _make(key, prev_start, prev_end, _previous_label(key, prev_start, prev_end))
    return _make(key, start, end, _label(key, start, end, today), previous)


def _read_date(raw):
    """A date from a query-string value, or None. parse_date returns None for text that
    does not look like a date but RAISES ValueError for one that does with impossible
    values ('2026-13-45'); both are the same thing to the person who typed it."""
    raw = (raw or '').strip()
    if not raw:
        return None
    try:
        return parse_date(raw)
    except ValueError:
        return None


def parse_period(params, today=None):
    """Read `period`, `date`, `from` and `to` from `params` and return (period, error).

    Never raises on user input. On any error the period returned is TODAY (the default)
    and `error` is a plain sentence to show above it, so a report page always has
    something to draw — the same fallback the CEO Daily Report uses for a bad ?date=.
    No `period`, or one this module does not know, is today with no error: an old or
    hand-edited link is ordinary, not a mistake worth a warning.
    """
    today = today or timezone.localdate()
    default = build_period(PERIOD_TODAY, today, today, today)
    key = (params.get('period') or '').strip().lower()

    if key == PERIOD_YESTERDAY:
        day = today - timedelta(days=1)
        return build_period(key, day, day, today), None
    if key == PERIOD_WEEK:
        # weekday() is 0 on Monday, so on a Monday the week is that one day (D2).
        return build_period(key, today - timedelta(days=today.weekday()), today, today), None
    if key == PERIOD_MONTH:
        return build_period(key, today.replace(day=1), today, today), None
    if key == PERIOD_DATE:
        day = _read_date(params.get('date'))
        if day is None:
            return default, ERROR_PICK_DATE
        if day > today:
            return default, ERROR_FUTURE
        if day < REPORTS_START:
            return default, ERROR_TOO_EARLY
        return build_period(key, day, day, today), None
    if key == PERIOD_RANGE:
        start, end = _read_date(params.get('from')), _read_date(params.get('to'))
        if start is None or end is None:
            return default, ERROR_PICK_DATE
        # Future first: a range that ends next week is wrong for that reason, whatever
        # its length.
        if start > today or end > today:
            return default, ERROR_FUTURE
        # Checked on From alone. A To before REPORTS_START means either From is before it
        # too (refused here) or From is after To (refused just below).
        if start < REPORTS_START:
            return default, ERROR_TOO_EARLY
        if start > end:
            return default, ERROR_FROM_AFTER_TO
        if (end - start).days + 1 > MAX_RANGE_DAYS:
            return default, ERROR_TOO_LONG
        return build_period(key, start, end, today), None
    return default, None
