"""
DASH-B2 — the Site Dashboard page, built on the DASH-B1 data layer (site_dashboard.py).

Reached from the nav's Home link and after login (DASH-B4, below); the separate "Site
Dashboard" nav link was removed as a duplicate of Home (R5).

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

DASH-B4: THE LANDING PAGE. PM, coordinator, CEO, Finance, SCM, Admin, System Admin and the
Design Head land here after login, and the PM / SCM / Finance / CEO dashboards redirect
here (decorators.retired_for_site_dashboard). What those pages offered that people act on
is carried, each piece scoped to the roles that had it (Zuber's rulings, 10 Nov):

    every viewer        "More" row (_more_links), Past target pill, QA/QC last visit and
                        open punch points on cards, the approvals-waiting card (None, and
                        so nothing drawn, for someone who approves nothing)
    PM, coordinator     awaiting re-verification group, "Request design change" links on
                        RESCO cards and draft rows whose design has started (the form is a
                        404 before that); draft rows link to the site for every viewer
    SCM                 three tiles, Stalled pill + stage line, grouped procurement and
                        open delivery issues groups
    Finance             Milestones awaiting tile

Every addition is a fixed number of queries whatever the number of sites
(tests_dash_b4 pins 3 against 30 sites per role), except grouped procurement, which is a
few queries per OPEX tender (ruling 11); none is per site.
"""
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from urllib.parse import urlencode

from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache

from .approval_queries import pending_approvals_card
from .attendance_views import attendance_unmapped_group_count, vendor_attendance_today_counts
from .dashboard_strips import (milestones_awaiting_count, pm_awaiting_reverification,
                               scm_open_delivery_issues, scm_pipeline, scm_tile_counts)
from .decorators import _forbidden, login_required
from .design_views import scm_opex_tender_rows
from .models import DesignAssignment, PaymentRequest, SystemSettings
from .payments import payment_counts
from .permissions import (DASHBOARD_MANAGER_ROLES, user_can_map_attendance,
                          user_can_raise_group_order, user_can_view_ceo_dashboard_lists,
                          user_can_view_payment_queue, user_can_view_site_dashboard,
                          user_can_view_vendor_attendance, user_sees_payment_totals)
from .punch_points import open_punch_point_counts
from .qaqc_views import last_visit_lines, qaqc_engineer_dashboard_context
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
    # DASH-B4 additions; the defaults are what a card shows when the view passes no extras.
    past_target: object = None     # (pill text, tooltip) when today or the forecast is past the target date
    qaqc_line: object = None       # "Last QA/QC visit: …" on a RESCO site with an engineer or a visit
    punch_points: int = 0          # outstanding punch points (Open or Rectified)
    scm: object = None             # SCM viewers only: {'pill': (text, level) or None, 'stages': text}
    design_change: bool = False    # PM/coordinator viewer and a RESCO site: offer the change-request link
    clear: bool = True             # nothing in the Attention column, so "✓ Clear" shows


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


def _past_target(card, today):
    """DASH-B4 ruling 9: the old PM / Finance "Delayed Nd" badge, kept as a "Past target"
    pill on any project type. (text, tooltip) when the later of today and the forecast
    finish is past the site's target date, else None.

    No query: target_date and the forecast are already on the card (one grouped build,
    site_dashboard.build_site_cards). Drafts and Commissioned sites carry none: a draft has
    not started, and a commissioned site's lateness is history, not something to act on,
    which is also why the old dashboards (Active / In Progress only) never showed it there.
    """
    target = card.target_date
    if target is None or card.is_draft or card.status == 'Commissioned':
        return None
    forecast = (card.schedule.forecast_finish
                if card.schedule.reason == REASON_SCHEDULED else None)
    latest = max(today, forecast) if forecast is not None else today
    if latest <= target:
        return None
    days = (latest - target).days
    if today > target:
        why = f'Target {_short_date(target)} has passed'
    else:
        why = f'Forecast finish {_short_date(forecast)} is after target {_short_date(target)}'
    return (f'Past target +{days}d', why)


def _qaqc_line(days):
    """The PM dashboard's QA/QC visit wording (Closeout step 7, V-10), from the value
    qaqc_views.last_visit_lines gives: None means an engineer but no submitted visit."""
    if days is None:
        return 'No QA/QC visit yet'
    if days == 0:
        return 'Last QA/QC visit: today'
    return f'Last QA/QC visit: {days} day{"s" if days != 1 else ""} ago'


#: The SCM stage words, as the old SCM dashboard's badges spelled them.
_STALL_STAGE_WORDS = {'boq': 'BOQ', 'order': 'order', 'delivery': 'delivery',
                      'issues': 'issues'}


def _scm_view(row):
    """DASH-B4 ruling 12: one "Stalled" pill and one stage line from a
    dashboard_strips.scm_pipeline() row. The pill names the worst stalled stage and its
    days (red beats amber, as the old badge chose); the line spells all four stages."""
    stall = row['stall']
    pill = None
    if stall is not None:
        stage, days, level = stall
        pill = (f'Stalled · {_STALL_STAGE_WORDS[stage]} {days}d',
                'crit' if level == 'red' else 'warn')

    def aged(word, status, age):
        if status is None:
            return f'{word}: none'
        return f'{word}: {status}' + (f' {age}d' if age is not None else '')

    issues = (f'Issues: {row["issue_count"]} open'
              + (f', oldest {row["issue_age"]}d' if row['issue_age'] is not None else '')
              if row['issue_count'] else 'Issues: clear')
    stages = ' · '.join((aged('BOQ', row['boq_status'], row['boq_age']),
                         aged('Order', row['order_status'], row['order_age']),
                         aged('Delivery', row['delivery_status'], row['delivery_age']),
                         issues))
    return {'pill': pill, 'stages': stages}


@dataclass(frozen=True)
class CardExtras:
    """Per-viewer inputs to the DASH-B4 card additions, built once per request by
    _card_extras() and handed to every _view() call. All maps are keyed by project pk."""
    today: object
    qaqc_lines: dict
    punch_points: dict
    scm: dict               # empty unless the viewer is SCM
    design_started: frozenset  # RESCO pks with a DesignAssignment; empty unless PM / coordinator viewer


def _card_extras(cards, profile, today):
    """The DASH-B4 card additions for `cards` (the non-draft ones), in a fixed number of
    queries whatever their number:

        qaqc_lines      qaqc_views.last_visit_lines: 2 queries, none without a RESCO card
        punch_points    punch_points.open_punch_point_counts: 1 grouped query
        scm             dashboard_strips.scm_pipeline: 4 queries, SCM viewers only
        design_started  1 query, PM / coordinator viewers with a RESCO card only

    last_visit_lines reads .pk and .project_type from what it is given, so each card is
    passed as a two-attribute stand-in rather than refetched as a Project.
    """
    live = [c for c in cards if not c.is_draft]
    stand_ins = [SimpleNamespace(pk=c.id, project_type=c.project_type) for c in live]
    # The stall pill and stage line are SCM's procurement signals (ruling 12): only an SCM
    # viewer pays the four queries or sees them. Every other role reads the DC counts
    # already on the card.
    scm = ({pk: _scm_view(row)
            for pk, row in scm_pipeline([(c.id, c.project_id) for c in live], today).items()}
           if profile.role == 'SCM' else {})
    # Ruling 6: PM and coordinator raise design change requests, so only they are offered
    # the link; the form (design_change_request_form) re-checks authority and explains a
    # closed window itself. It answers 404 for a site whose design has not started (no
    # DesignAssignment), so the link is drawn only where one exists: one grouped query over
    # the RESCO cards drawn (drafts included), never one per site. Everyone else pays none.
    design_started = frozenset()
    resco = [c.id for c in cards if c.project_type == ELIGIBLE_PROJECT_TYPE]
    if profile.role in DASHBOARD_MANAGER_ROLES and resco:
        design_started = frozenset(DesignAssignment.objects.filter(project_id__in=resco)
                                   .values_list('project_id', flat=True))
    return CardExtras(
        today=today,
        qaqc_lines=last_visit_lines(stand_ins, today),
        punch_points=open_punch_point_counts([c.id for c in live]),
        scm=scm,
        design_started=design_started,
    )


def _view(card, now, sees_payments, extras=None):
    past_target = qaqc_line = scm = None
    punch = 0
    design_change = False
    if extras is not None:
        past_target = _past_target(card, extras.today)
        if card.id in extras.qaqc_lines:
            qaqc_line = _qaqc_line(extras.qaqc_lines[card.id])
        punch = extras.punch_points.get(card.id, 0)
        scm = extras.scm.get(card.id)
        # RESCO sites with design under way, for a PM / coordinator viewer (_card_extras).
        design_change = card.id in extras.design_started
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
        past_target=past_target,
        qaqc_line=qaqc_line,
        punch_points=punch,
        scm=scm,
        design_change=design_change,
        # "✓ Clear" only when the Attention column has nothing else in it, so a card never
        # reads Clear beside a Past target, punch point or Stalled pill.
        clear=not (card.overdue or card.blocked or card.open_issues or past_target or punch
                   or (scm and scm['pill'])),
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


def _link(label, href, count=None):
    return {'label': label, 'href': href, 'count': count}


def _more_links(user, profile):
    """DASH-B4 rulings 16 and 17: the "More" row under the KPI tiles, as
    [{'label': group, 'links': [...]}] with empty groups left out. It is how the pages the
    retired dashboards linked to stay one click away. Every link is gated by the check its
    own page makes, so no link here leads to a 403:

        Tasks    every viewer: tasks_drill_down has its own branch for each Site Dashboard
                 role (PM / coordinator, Design for the Head, the portfolio roles), pinned by
                 tests_dash_b4. New project: PM only, as project_create's
                 @role_required(['PM']). Design dashboard: the Design Head, whose old home it
                 was; dashboard_design admits the Design role.
        SCM      user_can_map_attendance / user_can_view_vendor_attendance (SCM and Admin),
                 with the counts the SCM dashboard showed (one query each, only when shown)
        Finance  user_can_view_payment_queue, with the approved-to-pay count from
                 payments.payment_counts(), the function the queue's own tabs count with
        Admin    Admin: the three Admin-only screens the old Admin home linked
                 (@role_required(['Admin'])); System Admin: the sub-admin portal
                 (system_admin_required)
        CEO      user_can_view_ceo_dashboard_lists, the same CEO / Admin / System Admin set
                 as dashboard_ceo's decorator: its kept Residential and Tenders views
                 (ruling 5) and the overdue-task list
    """
    role = profile.role
    groups = []

    tasks = [_link('Due today', reverse('tasks_due_today')),
             _link('Due in 7 days', reverse('tasks_due_soon')),
             _link('Overdue', reverse('tasks_overdue'))]
    # Only a PM may create a project (project_create's decorator); a coordinator would get
    # a 403, which is why the old dashboard hid the button from them too.
    if role == 'PM':
        tasks.append(_link('New project', reverse('project_create')))
    # The Design Head's own work queue, QC links and catalogue live on the Design dashboard,
    # their home before DASH-B4 (ruling 16). Only the Head lands here; a plain designer
    # never sees this page. Deputies are not Site Dashboard users.
    if getattr(profile, 'is_design_head', False) and role == 'Design':
        tasks.append(_link('Design dashboard', reverse('dashboard_design')))
    groups.append({'label': 'Tasks', 'links': tasks})

    scm = []
    # Contractor mapping and vendor attendance: SCM and Admin only (ATT-B3 / ATT-B6); every
    # other role is refused by those pages, so their links and counts are not built.
    if user_can_map_attendance(user):
        scm.append(_link('Contractors to map', reverse('attendance_to_map'),
                         attendance_unmapped_group_count()))
    if user_can_view_vendor_attendance(user):
        today_counts = vendor_attendance_today_counts()
        scm.append(_link(
            f'Vendor attendance · {today_counts["sites"]} site'
            f'{"s" if today_counts["sites"] != 1 else ""} · {today_counts["people"]} '
            f'{"person" if today_counts["people"] == 1 else "people"}',
            reverse('vendor_attendance')))
    if scm:
        groups.append({'label': 'SCM', 'links': scm})

    # The payments queue: Finance, CEO, Admin, System Admin and payment approvers, as the
    # queue itself admits (and the nav's Payments link). The count is approved-to-pay, the
    # old Finance dashboard's "Payment Requests Pending" figure.
    if user_can_view_payment_queue(user):
        to_pay = payment_counts()[PaymentRequest.APPROVED].count
        groups.append({'label': 'Finance', 'links': [
            _link('Payments queue · to pay', reverse('payment_queue'), to_pay)]})

    admin = []
    # Admin-only screens (@role_required(['Admin'])): user management, the audit log and the
    # Admin Panel. Nobody else may open them, so nobody else is shown them.
    if role == 'Admin':
        admin += [_link('Manage users', reverse('user_list')),
                  _link('Activity log', reverse('admin_audit_log')),
                  _link('Admin panel', reverse('admin_master_switches'))]
    # The sub-admin portal is System Admin's alone (system_admin_required), and was their
    # home before DASH-B4.
    if role == 'System Admin':
        admin.append(_link('Sub-admin portal', reverse('subadmin_projects')))
    if admin:
        groups.append({'label': 'Admin', 'links': admin})

    # The CEO page's kept views and its overdue list: CEO, Admin and System Admin, the
    # roles dashboard_ceo and dashboard_ceo_overdue_tasks admit (CEO_DASHBOARD_ROLES).
    if user_can_view_ceo_dashboard_lists(user):
        ceo = reverse('dashboard_ceo')
        groups.append({'label': 'CEO', 'links': [
            _link('Portfolio analytics (Residential)', f'{ceo}?context=residential'),
            _link('Tenders overview', f'{ceo}?context=tenders'),
            _link('Overdue tasks list', reverse('dashboard_ceo_overdue_tasks'))]})
    return groups


def _role_strips(request, profile, today):
    """DASH-B4: the strips carried from the retired dashboards, scoped by role. Returns
    the template context they need; a strip's key is absent (or empty) for anyone outside
    its roles, and the template draws nothing for it.

        approvals_waiting           every viewer; pending_approvals_card() returns None for
                                    someone who approves nothing (ruling 18). 1-2 queries
        pm_reverify                 PM and coordinator (ruling 18). 4 queries
        scm_tiles, opex_tender_rows,
        scm_delivery_issues         SCM only (rulings 10, 11, 13). 3 + per-tender + 1
        milestones_awaiting         Finance only (ruling 14). 1 query
    """
    role = profile.role
    strips = {'approvals_waiting': pending_approvals_card(request.user)}
    # PM and coordinator only: the list is the Rectified points on the sites they manage,
    # and the Re-verify button is theirs where no QA/QC engineer can act (go-ahead Q4).
    # Portfolio roles manage no site; the Design Head cannot hold the PM's re-verify.
    if role in DASHBOARD_MANAGER_ROLES:
        strips['pm_reverify'] = pm_awaiting_reverification(profile)
    # SCM only (ruling 10, 11, 13): these are SCM's procurement queues, which only the SCM
    # dashboard carried. Admin reads the same pages through their own links.
    if role == 'SCM':
        strips['scm_tiles'] = scm_tile_counts(today)
        # Per-tender queries, accepted (ruling 11); built from SiteGroup and
        # DesignAssignment, so OPEX tenders appear although their sites stay Draft.
        strips['opex_tender_rows'] = scm_opex_tender_rows()
        # The partial's "Raise payment request" button; the target view re-checks it.
        strips['can_raise_group_order'] = user_can_raise_group_order(request.user)
        strips['scm_delivery_issues'] = scm_open_delivery_issues()
    # Finance only (ruling 14): client milestones are Finance's to invoice and receive.
    if role == 'Finance':
        strips['milestones_awaiting'] = milestones_awaiting_count()
    return strips


def _role_tiles(strips):
    """The SCM and Finance tiles (rulings 10, 14), appended after the E5 tiles. Plain
    counts with no filter: they are portfolio queues, not a property of the cards listed,
    so they are not cut by the search or the KPI filters (drawn as non-link tiles)."""
    tiles = []
    scm = strips.get('scm_tiles')
    if scm is not None:
        tiles += [
            {'label': 'BOQ awaiting ack.', 'value': scm['boq_awaiting'],
             'detail': 'submitted, not acknowledged'},
            {'label': 'Deliveries today', 'value': scm['deliveries_today'],
             'detail': 'DCs expected today'},
            {'label': 'Overdue DCs', 'value': scm['overdue_dcs'],
             'detail': 'past expected date'},
        ]
    if 'milestones_awaiting' in strips:
        tiles.append({'label': 'Milestones awaiting', 'value': strips['milestones_awaiting'],
                      'detail': 'Residential, pending'})
    return tiles


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

    # DASH-B4: the card additions are built over every card the page draws (the listed
    # ones and the drafts), once, whatever filter is on.
    extras = _card_extras(listed + drafts, profile, today)
    strips = _role_strips(request, profile, today)

    return render(request, 'projects/site_dashboard.html', {
        'cards': [_view(c, now, sees_payments, extras)
                  for c in sort_cards(listed, params['sort'])],
        'drafts': [_view(c, now, sees_payments, extras)
                   for c in sort_cards(drafts, params['sort'])],
        'kpis': kpis + _role_tiles(strips),
        'more_groups': _more_links(request.user, profile),
        **strips,
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
