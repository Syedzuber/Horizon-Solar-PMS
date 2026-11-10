"""
DASH-B2 — the Site Dashboard page, built on the DASH-B1 data layer (site_dashboard.py).

Reached from its own nav link. It is NOT the landing page and replaces no dashboard yet:
retiring the old ones is DASH-B4, after Zuber confirms nothing is missing.

WHERE EVERY FIGURE COMES FROM (E1)
    build_site_cards()   every card, in a fixed number of queries whatever the site count
    sort_cards()         the two orders, drafts last (E4)
    capacity_totals()    the DC / AC tile
    programs_on_cards()  the tender filter's options
The view's only queries of its own are the QA/QC strip's (qaqc_views'
qaqc_engineer_dashboard_context: one query for someone checking no site, a fixed few
otherwise; PM and coordinator viewers only, ruling 3) and one for "Dates as of" (below). Search, type,
tender and the KPI filter are applied in Python to the cards already built, and the KPI
tiles are counted from the SAME filtered cards, so a tile and the list it filters always
agree.

STATE LIVES IN THE URL (E2). sort, q, type, tender and filter are GET parameters: the
search box is a GET form and every tile and sort button is a link. Alpine holds only
whether a card, the drafts group or the QA/QC group is open. No fetch, no XHR, no polling.
An unknown or malformed parameter is ignored (an unknown sort falls back to the default).

"DATES AS OF" (E8, SCHED-R). The line under the title reads SystemSettings.
schedule_last_run_at, which only a finished `schedule_sites --all --apply` writes (the
06:30 IST Railway cron). Amber when it is older than the most recent 06:30 at least 30
minutes past (so before 07:00 yesterday's run still counts), or was never written.
ActivityLog rows are not a stand-in (ruling 1, 10 Nov): a run that changes no date logs
nothing. One query of its own (_dates_as_of).
"""
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from urllib.parse import urlencode

from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.cache import never_cache

from .decorators import _forbidden, login_required
from .models import SystemSettings
from .permissions import (DASHBOARD_MANAGER_ROLES, user_can_view_site_dashboard,
                          user_sees_payment_totals)
from .qaqc_views import qaqc_engineer_dashboard_context
from .scheduling import (BAND_LABELS, ELIGIBLE_PROJECT_TYPE, REASON_EXCLUDED,
                         REASON_NEEDS_ANCHOR, REASON_NEEDS_CAPACITY, REASON_NOT_ELIGIBLE)
from .site_dashboard import (EXECUTION_LANE_NAME, LANE_DONE, LANE_EMPTY, LANE_IN_PROGRESS,
                             ORDER_LATEST, ORDER_URGENT, ORDERS, REASON_SCHEDULED,
                             build_site_cards, capacity_totals, programs_on_cards,
                             sort_cards)

#: A site with no human update for this many days counts as "Gone quiet" (E5).
QUIET_DAYS = 7

#: (hour, minute) IST the Railway `schedule-sites` cron runs (`0 1 * * *` UTC). A
#: "Dates as of" stamp older than the most recent slot at least DAILY_RUN_GRACE in the
#: past is drawn amber (SCHED-R, R3 as amended 10 Nov).
DAILY_RUN_TIME = (6, 30)

#: Railway may start a cron service some minutes late; the slot is not owed until then.
DAILY_RUN_GRACE = timedelta(minutes=30)

#: Longest search string kept; anything longer is cut, never refused.
SEARCH_MAX = 100

#: The type filter's options, as Project.project_type values and the label shown. OPEX
#: is shown as RESCO, the name the business uses for those tenders.
TYPE_LABELS = {'OPEX': 'RESCO', 'CAPEX': 'CAPEX', 'Residential': 'Residential'}

SORT_LABELS = {ORDER_LATEST: 'Latest activity', ORDER_URGENT: 'Most urgent'}

#: The six fixed segments of every RESCO card's pipeline (ruling 2, 10 Nov): the OPEX
#: template's phase labels, with the Execution lane between Procurement and the
#: post-installation approvals. Matched by phase NAME because the card's lanes carry the
#: name only; a phase missing on some site shows as an empty segment rather than shifting
#: the others. Installation and Testing & Commissioning are not segments: their block
#: tasks are the Execution segment.
RESCO_SEGMENTS = (
    ('Design', 'Design'),
    ('Approvals (Pre-Installation)', 'Pre-appr.'),
    ('Procurement & Delivery', 'Procure'),
    (EXECUTION_LANE_NAME, 'Execution'),
    ('Approvals (Post-Installation)', 'Post-appr.'),
    ('Closeout', 'Closeout'),
)

#: The phases a crew is on site for, by utils.current_phase(). A Residential or CAPEX card
#: in one of them, with no attendance logged today, says so (E6). A RESCO card uses its
#: Execution segment instead (_attendance_line).
EXECUTION_PHASES = ('Installation', 'Testing & Commissioning')

#: What a non-scheduled OPEX card's schedule line says, and its tone (E6).
REASON_LINES = {
    REASON_NEEDS_ANCHOR: ('Needs Civil date', 'none'),
    REASON_NEEDS_CAPACITY: ('Needs DC capacity', 'none'),
    REASON_EXCLUDED: ('Excluded from auto-schedule', 'none'),
    REASON_NOT_ELIGIBLE: ('Not scheduled', 'mut'),
}


def _is_behind(card):
    return (card.schedule.days_behind or 0) > 0


#: The KPI filters (E5): key -> the test a card must pass to stay listed. Each tile's
#: site count is the number of cards its test passes, which is exactly the list it leaves.
FILTERS = {
    'behind': _is_behind,
    'civil': lambda card: card.schedule.reason == REASON_NEEDS_ANCHOR,
    'overdue': lambda card: card.overdue > 0,
    'blocked': lambda card: card.blocked > 0,
    'issues': lambda card: card.open_issues > 0,
    'quiet': lambda card: card.quiet_days >= QUIET_DAYS,
}


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def relative_time(moment, now):
    """'just now', '25m ago', '3h ago', 'yesterday', '4d ago' — the prototype's wording.
    A moment in the future (clock skew) reads 'just now' rather than a negative age."""
    minutes = int((now - moment).total_seconds() // 60)
    if minutes < 1:
        return 'just now'
    if minutes < 60:
        return f'{minutes}m ago'
    hours = minutes // 60
    if hours < 24:
        return f'{hours}h ago'
    days = hours // 24
    return 'yesterday' if days == 1 else f'{days}d ago'


def _capacity(value):
    """A capacity as typed, without trailing zeros: Decimal('62.50') -> '62.5'. None stays
    None, so the template prints 'not set' (D10: blank is never 0)."""
    if value is None:
        return None
    return format(value.normalize(), ',f')


def _whole(value):
    """A total, rounded to a whole number with thousands separators."""
    return None if value is None else f'{round(value):,}'


def _lakh(value):
    """Rupees in lakh, one decimal: 250000 -> '₹2.5 L'."""
    return f'₹{Decimal(value) / Decimal(100000):.1f} L'


# ---------------------------------------------------------------------------
# One card's display
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Segment:
    label: str
    title: str        # full phase name and state, for the tooltip and screen readers
    state: str        # CSS class: na / ns / active / late / blocked / done
    current: bool     # the card's current phase


@dataclass(frozen=True)
class CardView:
    """A SiteCard plus what the template needs to draw it, computed here so the template
    holds no logic beyond if/for."""
    card: object
    type_label: str
    place: str
    tender: str
    dc: object
    ac: object
    segments: tuple
    health: str
    schedule_line: object      # (text, tone) or None
    finish_line: str
    latest_when: object
    feed: tuple                # (relative time, ActivityRow)
    band: object
    payment: object
    attendance: object         # 'count', 'none' or None (no line); see _attendance_line
    scheduled: bool            # the site is in the auto-schedule (its Schedule block shows the plan)
    quiet: bool                # gone quiet: no human update for QUIET_DAYS or more


def _segment_state(lane):
    """The pipeline's colour for one lane: empty, then done, then blocked beats late beats
    in progress. A lane with nothing applicable is 'na' (drawn empty)."""
    if lane is None or lane.state == LANE_EMPTY:
        return 'na'
    if lane.state == LANE_DONE:
        return 'done'
    if lane.blocked:
        return 'blocked'
    if lane.overdue:
        return 'late'
    if lane.state == LANE_IN_PROGRESS:
        return 'active'
    return 'ns'


_STATE_WORDS = {'na': 'nothing to do', 'ns': 'not started', 'active': 'in progress',
                'late': 'overdue work', 'blocked': 'blocked', 'done': 'done'}


def _segment(label, name, lane, current):
    state = _segment_state(lane)
    counts = f' ({lane.work_done}/{lane.work_total})' if lane is not None and lane.work_total else ''
    return Segment(label=label, title=f'{name}: {_STATE_WORDS[state]}{counts}', state=state,
                   current=current)


def _segments(card):
    """E6's pipeline. A RESCO card always shows the six RESCO_SEGMENTS, Execution being
    the card's Execution segment (block tasks, plus location copies for state and dates;
    ruling 2). Residential and CAPEX show their own phases in order, the current one per
    utils.current_phase(). A draft shows none.

    THE HIGHLIGHTED RESCO SEGMENT IS THE FURTHEST ONE STARTED (Zuber, 10 Nov), not
    utils.current_phase(). That rule picks the first phase with an open human-owned task,
    and the pre-installation approvals stay open long after Execution begins, so it lit
    Pre-appr. on sites with crews on the roof. Here the label marks the furthest segment,
    in pipeline order, with an actual start; none when nothing has started. Display only:
    current_phase() is unchanged. An earlier segment left open keeps its own colour
    (active / late / blocked), so open approvals stay visible.
    """
    if card.is_draft:
        return ()
    if card.execution_segment is not None:
        by_name = {}
        for lane in card.lanes:
            by_name.setdefault(lane.name, lane)
        lanes = [card.execution_segment if name == EXECUTION_LANE_NAME else by_name.get(name)
                 for name, _ in RESCO_SEGMENTS]
        started = [i for i, lane in enumerate(lanes)
                   if lane is not None and lane.actual_start is not None]
        furthest = started[-1] if started else None
        return tuple(_segment(label, name, lane, i == furthest)
                     for i, ((name, label), lane) in enumerate(zip(RESCO_SEGMENTS, lanes)))
    return tuple(_segment(lane.name, lane.name, lane, card.current_phase == lane.name)
                 for lane in card.lanes)


def _attendance_line(card):
    """Which attendance line the collapsed card shows (E6): 'count' for "Attendance
    today: N", 'none' for "No attendance today", None for no line. The figure itself is
    card.attendance_today, unchanged.

    A RESCO CARD READS ITS EXECUTION SEGMENT (Zuber, 10 Nov), the same data the pipeline
    highlight reads: a line only while Execution has started and is not done, whatever
    utils.current_phase() says. current_phase() answers Pre-appr. on sites whose crews are
    on the roof (open pre-installation approvals), so the old rule hid a missing roll call
    exactly where it matters. Residential and CAPEX keep the old rule: a logged count
    always shows, and "none" only in an EXECUTION_PHASES phase.
    """
    segment = card.execution_segment
    if segment is not None:
        if segment.actual_start is None or segment.state == LANE_DONE:
            return None
        return 'count' if card.attendance_today is not None else 'none'
    if card.attendance_today is not None:
        return 'count'
    if not card.is_draft and card.current_phase in EXECUTION_PHASES:
        return 'none'
    return None


def _health(card):
    """The card's left stripe (E6): red for a blocked task, amber when behind plan or
    overdue, grey when gone quiet. Drafts carry none."""
    if card.is_draft:
        return ''
    if card.blocked:
        return 'crit'
    if _is_behind(card) or card.overdue:
        return 'warn'
    if card.quiet_days >= QUIET_DAYS:
        return 'quiet'
    return ''


def _schedule_line(card):
    """E6's schedule line: behind / on plan for a scheduled site, the reason for any other
    non-draft RESCO site, nothing for Residential, CAPEX or a draft."""
    schedule = card.schedule
    if card.is_draft:
        return None
    if schedule.reason == REASON_SCHEDULED:
        if schedule.days_behind is None:
            return ('Scheduled', 'mut')
        if schedule.days_behind > 0:
            n = schedule.days_behind
            return (f'{n} day{"s" if n != 1 else ""} behind plan', 'behind')
        return ('On plan', 'onplan')
    if card.project_type == ELIGIBLE_PROJECT_TYPE:
        return REASON_LINES.get(schedule.reason)
    return None


def _finish_line(card):
    """'Finish <forecast>' on a scheduled site with a forecast, else 'Target <date>'."""
    schedule = card.schedule
    if schedule.reason == REASON_SCHEDULED and schedule.forecast_finish is not None:
        return 'Finish ' + _short_date(schedule.forecast_finish)
    if card.target_date is not None:
        return 'Target ' + _short_date(card.target_date)
    return 'Target not set'


def _short_date(value):
    """'12 Nov' — day without a leading zero, portable (strftime's %-d is not on Windows)."""
    return f'{value.day} {value:%b}'


def _view(card, now, sees_payments):
    return CardView(
        card=card,
        type_label=TYPE_LABELS.get(card.project_type, card.project_type),
        place=card.site_name or card.city,
        tender=card.program_code or 'Standalone',
        dc=_capacity(card.dc_kwp),
        ac=_capacity(card.ac_kw),
        segments=_segments(card),
        health=_health(card),
        schedule_line=_schedule_line(card),
        finish_line=_finish_line(card),
        latest_when=relative_time(card.latest.at, now) if card.latest else None,
        feed=tuple((relative_time(row.at, now), row) for row in card.feed),
        band=BAND_LABELS.get(card.schedule.band) if card.schedule.band else None,
        # D8: the figure only for a viewer user_sees_payment_totals() admits; the card
        # already carries None for anyone else, and this keeps the template from ever
        # printing a pill or a row for them.
        payment=(_lakh(card.payment_open) if sees_payments and card.payment_open else None),
        attendance=_attendance_line(card),
        scheduled=card.schedule.reason == REASON_SCHEDULED,
        quiet=FILTERS['quiet'](card),
    )


# ---------------------------------------------------------------------------
# The request
# ---------------------------------------------------------------------------

def _params(request, program_ids):
    """The page's GET parameters, validated. Anything unknown or malformed is dropped (an
    unknown sort falls back to Latest activity), so no parameter can raise."""
    sort = request.GET.get('sort', ORDER_LATEST)
    if sort not in ORDERS:
        sort = ORDER_LATEST
    project_type = request.GET.get('type', '')
    if project_type not in TYPE_LABELS:
        project_type = ''
    try:
        tender = int(request.GET.get('tender', ''))
    except ValueError:
        tender = None
    if tender not in program_ids:
        tender = None
    active = request.GET.get('filter', '')
    if active not in FILTERS:
        active = ''
    return {
        'sort': sort,
        'q': request.GET.get('q', '').strip()[:SEARCH_MAX],
        'type': project_type,
        'tender': tender,
        'filter': active,
    }


def _query(params, **changes):
    """The page's query string with `changes` applied; empty values are left out, so the
    URLs stay short and a cleared filter disappears from the address bar."""
    merged = {**params, **changes}
    if merged.get('sort') == ORDER_LATEST:
        merged['sort'] = ''
    return '?' + urlencode({k: v for k, v in merged.items() if v not in ('', None)})


def _matches(card, params):
    """The search, type and tender filters, on one card. Search is case-insensitive over
    what the card shows: code, project ID, name, city, PM and tender."""
    if params['type'] and card.project_type != params['type']:
        return False
    if params['tender'] is not None and card.program_id != params['tender']:
        return False
    q = params['q'].lower()
    if q:
        haystack = ' '.join(str(v) for v in (card.code, card.project_id, card.site_name,
                                             card.city, card.pm_name or '',
                                             card.program_code or '')).lower()
        return q in haystack
    return True


def _kpis(live, params, sees_payments):
    """E5's tiles, counted from `live` — the same cards the list is cut from — so a filter
    tile's site count always equals the length of the list it leaves."""
    def tile(key, label, value, detail, tone=''):
        sites = sum(1 for c in live if FILTERS[key](c))
        active = params['filter'] == key
        return {'key': key, 'label': label, 'value': value, 'detail': detail,
                'tone': tone if value else '', 'sites': sites, 'active': active,
                # A second click on the active tile clears it (E5).
                'href': _query(params, filter='' if active else key)}

    totals = capacity_totals(live)
    behind = [c for c in live if _is_behind(c)]
    scheduled = sum(1 for c in live if c.schedule.reason == REASON_SCHEDULED)
    worst = max((c.schedule.days_behind for c in behind), default=0)
    overdue_sites = sum(1 for c in live if c.overdue)
    blocked_sites = sum(1 for c in live if c.blocked)
    issue_sites = sum(1 for c in live if c.open_issues)

    dc_detail = f'AC {_whole(totals.ac_kw) or 0} kW'
    if totals.dc_missing:
        dc_detail += f' · {totals.dc_missing} without DC'
    tiles = [
        {'label': 'Sites in execution', 'value': len(live), 'detail': None},
        {'label': 'DC kWp in execution', 'value': _whole(totals.dc_kwp) or 0, 'detail': dc_detail},
        tile('behind', 'Behind plan', len(behind),
             (f'of {scheduled} scheduled · worst {worst} day{"s" if worst != 1 else ""}'
              if behind else f'{scheduled} scheduled sites on plan'), 'crit'),
        tile('civil', 'Needs Civil date', sum(1 for c in live if FILTERS['civil'](c)),
             'no execution dates yet', 'warn'),
        tile('overdue', 'Overdue tasks', sum(c.overdue for c in live),
             f'across {overdue_sites} site{"s" if overdue_sites != 1 else ""}', 'crit'),
        tile('blocked', 'Blocked tasks', sum(c.blocked for c in live),
             f'{blocked_sites} site{"s" if blocked_sites != 1 else ""} affected', 'crit'),
        tile('issues', 'Open issues', sum(c.open_issues for c in live),
             f'on {issue_sites} site{"s" if issue_sites != 1 else ""}', 'warn'),
        tile('quiet', 'Gone quiet', sum(1 for c in live if FILTERS['quiet'](c)),
             f'no update in {QUIET_DAYS}+ days', 'warn'),
    ]
    # D8: the open payment figure only for the portfolio money roles; for anyone else the
    # tile is not drawn at all (not a zero), because a zero would read as "nothing open".
    if sees_payments:
        open_total = sum((c.payment_open for c in live if c.payment_open), Decimal(0))
        tiles.append({'label': 'Payments pending', 'value': _lakh(open_total),
                      'detail': 'open requests, indicative per site'})
    return tiles


def _dates_as_of(now):
    """E8 / SCHED-R: the "Dates as of" line under the title, from the stamp the daily
    `schedule_sites --all --apply` run writes. Returns {'text', 'stale'}; stale draws amber.
    """
    # One query, and a read only: values().first() rather than SystemSettings.get(), whose
    # get_or_create would INSERT the row from a GET page if it were ever missing. No
    # context processor loads SystemSettings, so there is nothing to reuse.
    row = SystemSettings.objects.filter(pk=1).values('schedule_last_run_at').first()
    stamp = row['schedule_last_run_at'] if row else None
    if stamp is None:
        return {'text': 'Dates not yet refreshed by the daily run', 'stale': True}
    local = timezone.localtime(stamp)
    text = f'Dates as of {_short_date(local)}, {local:%H:%M}'
    # Compare against the most recent daily slot whose grace has expired, not simply
    # today's 06:30: before 07:00 IST today's run is not yet owed (or Railway is still
    # starting it), so a normal run yesterday stays grey overnight and through the run.
    current = timezone.localtime(now)
    due = current.replace(hour=DAILY_RUN_TIME[0], minute=DAILY_RUN_TIME[1],
                          second=0, microsecond=0)
    if current < due + DAILY_RUN_GRACE:
        due -= timedelta(days=1)
    if local < due:
        return {'text': f'{text} — the daily refresh has not run today', 'stale': True}
    return {'text': text, 'stale': False}


# Public names for the display rules the Tenders Gantt (DASH-B3, views_tenders_gantt)
# reuses, so that page shows the same highlight, schedule line and stamp as this one
# without importing private names. Aliases only; relative_time and QUIET_DAYS are public
# already.
segments_for_card = _segments
schedule_line = _schedule_line
dates_as_of = _dates_as_of


@login_required
@never_cache
def site_dashboard(request):
    """
    The Site Dashboard: one card per visible site, KPI tiles, search and filters (DASH-B2).
    Access: PM, Project Coordinator, CEO, Finance, SCM, Admin, System Admin and the Design
    Head (permissions.user_can_view_site_dashboard); everyone else gets the 403 page.
    GET only in effect: it reads, never writes. never_cache, because task dates move daily
    and Back from a site must not show yesterday's page (E3).
    """
    # Only the roles sites_visible_on_dashboard() gives a scope; a Site Engineer, a
    # designer, BD or a profile-less account is refused with the same 403 every wrong-role
    # dashboard gives (role_required's _forbidden), so a probe learns nothing.
    if not user_can_view_site_dashboard(request.user):
        return _forbidden(request)

    # One IST day and one clock for every figure and every "3h ago" on the page.
    today = timezone.localdate()
    now = timezone.now()
    sees_payments = user_sees_payment_totals(request.user)

    cards = build_site_cards(request.user, today=today)
    programs = programs_on_cards(cards)
    params = _params(request, {p[0] for p in programs})

    # Search, type and tender cut the whole set; the tiles are counted after that cut, so
    # they describe what the list can show.
    scoped = [c for c in cards if _matches(c, params)]
    live = [c for c in scoped if not c.is_draft]
    drafts = [c for c in scoped if c.is_draft]
    kpis = _kpis(live, params, sees_payments)

    listed = live
    if params['filter']:
        listed = [c for c in live if FILTERS[params['filter']](c)]
        # A KPI filter is a question about sites in execution; drafts have no tasks to be
        # late, blocked or quiet on, so their group is not drawn under a filter.
        drafts = []

    profile = request.user.profile
    # The QA/QC strip is for PM and coordinator viewers only (ruling 3): they are the
    # managers who may also be named a site's QA/QC engineer (CL-I) on sites this page
    # does not list for them. Portfolio roles see every site already; the Design Head
    # cannot be a QA/QC engineer (QAQC_ENGINEER_ROLES).
    qaqc = (qaqc_engineer_dashboard_context(profile)
            if profile.role in DASHBOARD_MANAGER_ROLES else {})

    return render(request, 'projects/site_dashboard.html', {
        'cards': [_view(c, now, sees_payments) for c in sort_cards(listed, params['sort'])],
        'drafts': [_view(c, now, sees_payments) for c in sort_cards(drafts, params['sort'])],
        'kpis': kpis,
        'params': params,
        'programs': programs,
        'type_labels': TYPE_LABELS,
        'sort_links': [{'label': SORT_LABELS[order], 'active': params['sort'] == order,
                        'href': _query(params, sort=order)} for order in ORDERS],
        'clear_href': _query(params, q='', type='', tender=None, filter=''),
        'filtered': bool(params['q'] or params['type'] or params['tender'] is not None
                         or params['filter']),
        'sees_payments': sees_payments,
        'today': today,
        'dates_as_of': _dates_as_of(now),
        'scope_label': ('Sites you manage' if profile.role in DASHBOARD_MANAGER_ROLES
                        else 'All live sites'),
        **qaqc,
    })
