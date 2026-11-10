"""
DASH-B3 — the Tenders Gantt: data and geometry. No view, no template.

    data = build_gantt(request.user, today)     # every visible OPEX-with-Program site
    timeline = data.timeline                     # month-aligned range (R1)
    draw(lane, timeline, show_plan)              # a lane's pieces as left/width percentages
    tender_cards(sites)                          # G9's card per Program

WHERE THE FIGURES COME FROM
    The site scope, every per-site and per-lane figure (state, actual dates, blocked,
    schedule reason, plan slip, latest activity, sort keys) is the Site Dashboard's own
    site_dashboard.build_site_cards(): this module adds no role test and no second copy
    of a rule. It adds ONE grouped query (lane_date_rows) for what the cards do not carry:
    planned, baseline and actual dates per phase lane and per schedule block. One query
    over every site at once, so the page costs a fixed number of queries whatever the
    site count.

MONEY NEVER LEAVES THIS MODULE. build_site_cards() computes the per-site payment figure
for the portfolio money roles, because the Site Dashboard needs it. GanttSite keeps the
card for the view to read named fields from; the view copies only those fields into its
own row objects, so no payment value can reach the template (R4, tested).

THE LANES (G3-G7)
    Collapsed   the six RESCO segments: Design, Pre-appr., Procure, Execution, Post-appr.,
                Closeout. Execution is the block tasks by template task (the scheduler's
                scope); location copies feed its state and actual dates (and, on a site
                the scheduler does not plan, its PM-typed dates: UKRU001).
    Expanded    Execution opens into its 8 blocks.

GEOMETRY (R1, R2)
    Every position is a percentage of the timeline, so it is the same for both zooms; the
    zoom only sets how many pixels a day takes (DAY_PX), i.e. how wide the track is
    drawn. A bar covers whole days: it starts at the left edge of its first day and ends
    at the right edge of its last. Bars and marks outside the range are clipped to its
    edge and flagged, never dropped.
"""
import calendar
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from django.db.models import (BooleanField, Case, CharField, Count, F, IntegerField, Max,
                              Min, OuterRef, Q, Subquery, Value, When)
from django.utils import timezone

from .models import StatusTransition, Task
from .permissions import sites_visible_on_dashboard
from .scheduling import ANCHOR_TASK_CODE, ELIGIBLE_PROJECT_TYPE, REASON_NEEDS_ANCHOR
from .site_dashboard import (EXECUTION_LANE_NAME, LANE_DONE, LANE_EMPTY, REASON_SCHEDULED,
                             block_copy_q, build_site_cards, capacity_totals, in_block_q,
                             lane_work_q)
from .views_site_dashboard import (QUIET_DAYS, REASON_LINES, RESCO_SEGMENTS,
                                   segments_for_card)

#: The three zooms (G10, DASH-B3b Z1) in button order, and the width of one day in each,
#: in pixels (R2). Zoom only widens the track: every bar is a % of the range.
ZOOM_MONTH = 'month'
ZOOM_WEEK = 'week'
ZOOM_DAY = 'day'
ZOOMS = (ZOOM_MONTH, ZOOM_WEEK, ZOOM_DAY)
DAY_PX = {ZOOM_MONTH: 5, ZOOM_WEEK: 18, ZOOM_DAY: 28}

#: The timeline always shows at least this much before and after today (R1) ...
RANGE_BEFORE = timedelta(days=30)
RANGE_AFTER = timedelta(days=45)
#: ... and never stretches further than this to fit a date, so one mistyped year (the
#: restore holds a due date in the year 0020) cannot squash every bar to a sliver.
RANGE_FLOOR_MONTHS = 6
RANGE_CEIL_MONTHS = 12

#: Piece kinds, which are also the CSS classes the template draws them with.
ACTUAL = 'a'       # work done or under way: first start to last done, or to today
FAINT = 'f'        # an open lane's remaining planned time, after today
PLANNED = 'p'      # planned only, nothing started: dashed outline
LATE = 'r'         # red: past the planned end, open (G5, G6) or the slip tail (G4)
FORECAST = 'e'     # an Execution block's (or the Execution lane's) forecast dates
PLAN_LINE = 'pl'   # thin dark line: the baseline (frozen pure plan, SCHED-2e)
DUE_MARK = 'dm'    # a single PM-typed date: open diamond at that day (R3)
MANUAL_MARK = 'mm' # "M": a block holding a PM-typed date (G5)
TARGET = 'tg'      # the site's target commissioning date (G11)
MARK_KINDS = (DUE_MARK, MANUAL_MARK, TARGET)

#: Where the Execution lane sits among the six, and the five phase lanes around it.
PHASE_LANES = tuple((name, label) for name, label in RESCO_SEGMENTS)


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Piece:
    """One bar or mark on a lane, in dates. `end` None makes it a mark at `start`."""
    kind: str
    start: date
    end: object = None


@dataclass(frozen=True)
class Drawn:
    """A Piece placed on the timeline: percentages as fixed-format strings, so neither
    locale nor Decimal ever reaches the style attribute."""
    cls: str           # piece kind, plus 'cs' / 'ce' when clipped at the start / end
    left: str
    width: object      # None for a mark


@dataclass
class Lane:
    key: str           # 'seg-0'..'seg-5', 'block-1'..'block-8'
    label: str
    text: str          # the dates in words, for the left column (G5: nothing on hover)
    pieces: list = field(default_factory=list)
    hatch: bool = False        # a human task in the lane is Blocked (G6)
    note: object = None        # a label drawn at today (G7), e.g. "Needs Civil date"
    block: bool = False        # an Execution block line, indented under Execution


@dataclass
class GanttSite:
    """One site: its Site Dashboard card (read by the view, never sent to the template),
    the six collapsed lanes and the expanded list (phases, Execution, its blocks)."""
    card: object
    lanes: list                # the six collapsed lanes, in RESCO_SEGMENTS order
    detail: list               # expanded: the six, with Execution's blocks after it
    target: object = None      # Piece(TARGET) or None
    highlight: object = None   # the highlighted segment's label (furthest started), or None


@dataclass(frozen=True)
class Timeline:
    start: date                # first day drawn (the 1st of a month)
    end: date                  # last day drawn (the last day of a month), inclusive

    @property
    def days(self):
        return (self.end - self.start).days + 1


@dataclass(frozen=True)
class GanttData:
    sites: list                # non-draft GanttSite, unsorted
    drafts: list               # draft GanttSite (no lanes), unsorted
    timeline: Timeline


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------

def _add_months(day, months):
    """`day` moved by whole months, clamped to the target month's last day."""
    index = day.year * 12 + day.month - 1 + months
    year, month = divmod(index, 12)
    month += 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def timeline_for(dates, today):
    """The range the chart draws (R1): month-aligned; at least RANGE_BEFORE before today
    and RANGE_AFTER after; stretched to the earliest and latest date drawn, but never
    beyond RANGE_FLOOR_MONTHS before or RANGE_CEIL_MONTHS after today."""
    low, high = today - RANGE_BEFORE, today + RANGE_AFTER
    floor = _add_months(today, -RANGE_FLOOR_MONTHS)
    ceiling = _add_months(today, RANGE_CEIL_MONTHS)
    for day in dates:
        if day is None:
            continue
        low = min(low, max(day, floor))
        high = max(high, min(day, ceiling))
    last = calendar.monthrange(high.year, high.month)[1]
    return Timeline(start=low.replace(day=1), end=high.replace(day=last))


def offset_days(timeline, day):
    """Whole days from the timeline's first day to `day` (negative before it)."""
    return (day - timeline.start).days


def offset_px(timeline, day, zoom):
    """The pixel offset of `day`'s left edge at `zoom` (R2): days times DAY_PX."""
    return offset_days(timeline, day) * DAY_PX[zoom]


def track_px(timeline, zoom):
    """The track's width in pixels at `zoom`."""
    return timeline.days * DAY_PX[zoom]


def _pct(value):
    return f'{value:.3f}'


def span(timeline, start, end):
    """(left %, width %, clipped at start, clipped at end) for a bar covering the days
    `start`..`end` inclusive. Clipped to the range, never dropped: a bar wholly outside
    becomes one day at the nearer edge, flagged. Width is never negative: an end before
    its start is drawn as one day at the start."""
    if end < start:
        end = start
    clip_start = start < timeline.start
    clip_end = end > timeline.end
    first = min(max(start, timeline.start), timeline.end)
    last = max(min(end, timeline.end), first)
    days = timeline.days
    left = offset_days(timeline, first) / days * 100
    width = ((last - first).days + 1) / days * 100
    return left, width, clip_start, clip_end


def point(timeline, day):
    """(left %, clipped) for a mark at the middle of `day`, clipped to the range."""
    clipped = day < timeline.start or day > timeline.end
    inside = min(max(day, timeline.start), timeline.end)
    return (offset_days(timeline, inside) + 0.5) / timeline.days * 100, clipped


def place(piece, timeline):
    """A Piece as a Drawn: percentages on `timeline`, clip flags as CSS classes."""
    if piece.end is None:
        left, clipped = point(timeline, piece.start)
        return Drawn(cls=piece.kind + (' cl' if clipped else ''), left=_pct(left), width=None)
    left, width, clip_start, clip_end = span(timeline, piece.start, piece.end)
    cls = piece.kind + (' cs' if clip_start else '') + (' ce' if clip_end else '')
    return Drawn(cls=cls, left=_pct(left), width=_pct(width))


def draw(pieces, timeline, show_plan=True):
    """Every piece placed; plan lines left out when "Show plan" is off (G10)."""
    return tuple(place(p, timeline) for p in pieces if show_plan or p.kind != PLAN_LINE)


def month_labels(timeline):
    """[(label, left %, width %)] for every month in the range; the year is shown on
    January and on the first month."""
    out, day = [], timeline.start
    while day <= timeline.end:
        last = day.replace(day=calendar.monthrange(day.year, day.month)[1])
        left, width, _, _ = span(timeline, day, last)
        label = f'{day:%b}' + (f' {day.year}' if day == timeline.start or day.month == 1 else '')
        out.append((label, _pct(left), _pct(width)))
        day = last + timedelta(days=1)
    return out


def week_labels(timeline):
    """[(label, left %)] for every Monday in the range: the ISO week number (G10 Week)."""
    out = []
    day = timeline.start + timedelta(days=(7 - timeline.start.weekday()) % 7)
    while day <= timeline.end:
        left, _, _, _ = span(timeline, day, day)
        out.append((f'W{day.isocalendar()[1]}', _pct(left)))
        day += timedelta(days=7)
    return out


@dataclass(frozen=True)
class DayCell:
    """One date in the Day-zoom header (DASH-B3b Z3), and its Sunday shade when it is one."""
    day: date
    initial: str               # weekday initial drawn above the date number: M T W T F S S
    left: str                  # left edge as % of the track, for the header cell
    frac: str                  # the same edge as a fraction (0..1), for the shade's calc()
    is_sunday: bool            # red cell and a faint full-height column (Z6: display only)
    is_today: bool             # gold-boxed cell


WEEKDAY_INITIALS = 'MTWTFSS'   # indexed by date.weekday(), Monday = 0


def day_cells(timeline, today):
    """[DayCell] for every date in the range, built from the range alone (no query).

    Sunday is marked whatever the Admin "skip Sundays" scheduling switch says (Z6): the
    shading tells the reader which day of the week it is, not whether work is planned."""
    out = []
    for offset in range(timeline.days):
        day = timeline.start + timedelta(days=offset)
        frac = offset / timeline.days
        out.append(DayCell(day=day, initial=WEEKDAY_INITIALS[day.weekday()],
                           left=_pct(frac * 100), frac=f'{frac:.5f}',
                           is_sunday=day.weekday() == 6, is_today=day == today))
    return out


def today_fraction(timeline, today):
    """Today's middle as a fraction of the track (0..1), for the today line's calc()."""
    left, _ = point(timeline, today)
    return f'{left / 100:.5f}'


# ---------------------------------------------------------------------------
# The one grouped query
# ---------------------------------------------------------------------------

def lane_date_rows(site_pks):
    """Planned, baseline and actual dates of every applicable task of `site_pks` (a
    subquery of Project pks), grouped by (site, phase name, schedule block or none,
    location copy or not). One query whatever the site count; Python merges the rows
    into lanes and blocks.

    Applicable tasks of any type, mirrors in: site_dashboard.lane_work_q(), the rule the
    cards' lane state and actual dates already read, so a bar and its card agree. The
    block of a task comes from its template row's ScheduleBlockTask, and only for a task
    the Execution rule admits (in_block_q: in an active block, not a mirror, not a
    location copy) or a location copy of one (block_copy_q). Grouping by the block's
    SEQUENCE is safe because only the OPEX template has blocks and a site is built from
    one template.

    The actual start of a task is its first move to In Progress or Done in the
    StatusTransition ledger, as a correlated subquery inside Min(), the same expression
    build_site_cards() uses for the lanes (that one is local to the function).
    """
    first_start = (StatusTransition.objects
                   .filter(subject_type='task', subject_id=OuterRef('pk'),
                           to_status__in=[Task.IN_PROGRESS, Task.DONE])
                   .order_by('occurred_at').values('occurred_at')[:1])
    in_execution = in_block_q() | block_copy_q()
    link = 'template_task__schedule_block_task__block'
    done = Q(status=Task.DONE)
    return list(
        Task.objects.filter(phase__project_id__in=site_pks)
        .filter(lane_work_q())
        .annotate(
            gantt_block=Case(When(in_execution, then=F(f'{link}__sequence')),
                             default=Value(None), output_field=IntegerField()),
            gantt_block_name=Case(When(in_execution, then=F(f'{link}__name')),
                                  default=Value(''), output_field=CharField()),
            gantt_copy=Case(When(block_copy_q(), then=Value(True)),
                            default=Value(False), output_field=BooleanField()))
        .values('phase__project_id', 'phase__phase_name', 'gantt_block', 'gantt_block_name',
                'gantt_copy')
        .annotate(
            n=Count('pk'),
            open_n=Count('pk', filter=~done),
            plan_start=Min('schedule_start'),
            due_min=Min('due_date'),
            due_max=Max('due_date'),
            base_start=Min('baseline_start'),
            base_due=Max('baseline_due'),
            # G5's M marker: a PM-typed date in the block. The Civil anchor is left out,
            # as Schedule.manual_dates leaves it out: its date is the PM's on every
            # scheduled site by rule, so marking it would say nothing.
            manual=Count('pk', filter=Q(due_date_source=Task.DUE_DATE_MANUAL)
                         & ~Q(template_task__code=ANCHOR_TASK_CODE)),
            first_move=Min(Subquery(first_start)),
            first_done=Min('completed_at', filter=done),
            last_done=Max('completed_at', filter=done),
        )
        .order_by())


@dataclass(frozen=True)
class Dates:
    """Grouped rows merged: one lane's or one block's dates."""
    n: int = 0
    open_n: int = 0
    plan_start: object = None    # earliest scheduler start, else earliest due date (R3)
    plan_end: object = None      # latest due date
    single: bool = False         # only PM-typed dates, all on one day: a due mark (R3)
    base_start: object = None
    base_due: object = None
    manual: int = 0
    actual_start: object = None  # local date
    actual_end: object = None    # local date, only once every task is Done


def _merge(rows):
    def low(key):
        values = [r[key] for r in rows if r[key] is not None]
        return min(values) if values else None

    def high(key):
        values = [r[key] for r in rows if r[key] is not None]
        return max(values) if values else None

    n = sum(r['n'] for r in rows)
    open_n = sum(r['open_n'] for r in rows)
    starts = [v for v in (low('first_move'), low('first_done')) if v is not None]
    last_done = high('last_done')
    scheduled_start = low('plan_start')
    due_min, due_max = low('due_min'), high('due_max')
    return Dates(
        n=n, open_n=open_n,
        plan_start=scheduled_start or due_min, plan_end=due_max,
        single=scheduled_start is None and due_max is not None and due_min == due_max,
        base_start=low('base_start'), base_due=high('base_due'),
        manual=sum(r['manual'] for r in rows),
        actual_start=timezone.localdate(min(starts)) if starts else None,
        actual_end=(timezone.localdate(last_done)
                    if last_done is not None and n and not open_n else None),
    )


# ---------------------------------------------------------------------------
# Lanes
# ---------------------------------------------------------------------------

def short_date(value):
    """'12 Nov' — day without a leading zero, portable (strftime's %-d is not on Windows)."""
    return f'{value.day} {value:%b}'


def _local(moment):
    return timezone.localdate(moment) if moment is not None else None


def _plain_lane(key, label, lane, dates, today):
    """G6: a phase lane (and the Execution lane of a site the scheduler does not plan).

    Actual bar from first start to last done; while open, solid to today and faint to the
    planned end; red from the planned end to today once past it; dashed outline when only
    planned; a due mark when the plan is one PM-typed day (R3); hatched when a human task
    in it is Blocked. `lane` is the card's Lane (state, actual dates, blocked)."""
    out = Lane(key=key, label=label, text='')
    if lane is None or lane.state == LANE_EMPTY:
        out.text = 'Nothing to do'
        return out
    out.hatch = bool(lane.blocked)
    done = lane.state == LANE_DONE
    start, end = _local(lane.actual_start), _local(lane.actual_end)
    planned_start, planned_end = dates.plan_start, dates.plan_end
    words = []
    if start is not None:
        if done:
            finish = max(end or start, start)
            out.pieces.append(Piece(ACTUAL, start, finish))
            words.append(f'{short_date(start)} → {short_date(finish)} · done')
        else:
            out.pieces.append(Piece(ACTUAL, start, max(today, start)))
            if planned_end is not None and planned_end > today:
                out.pieces.append(Piece(FAINT, today + timedelta(days=1), planned_end))
            words.append(f'{short_date(start)} → now')
            # A late lane says so below ("Late · <due> → now"); "due" would repeat it.
            if planned_end is not None and planned_end >= today:
                words.append(f'due {short_date(planned_end)}')
    elif planned_end is not None:
        if dates.single:
            out.pieces.append(Piece(DUE_MARK, planned_end))
            words.append(f'due {short_date(planned_end)}')
        else:
            out.pieces.append(Piece(PLANNED, planned_start, planned_end))
            words.append(f'planned {short_date(planned_start)} → {short_date(planned_end)}')
    if not done and planned_end is not None and planned_end < today:
        out.pieces.append(Piece(LATE, planned_end + timedelta(days=1), today))
        words.append(f'Late · {short_date(planned_end)} → now')
    if out.hatch:
        words.append('Blocked')
    out.text = ' · '.join(words) or 'No dates yet'
    return out


def _execution_lane(card, dates, today):
    """G4: the Execution lane of a SCHEDULED site. Forecast bar from the first block's
    actual start (else its planned start) to the forecast finish, solid up to today once
    started; the plan line from the first block's baseline start to the baseline finish;
    the red slip tail from the baseline finish to the forecast finish when behind. The
    slip tail is drawn on this lane only (G4)."""
    segment = card.execution_segment
    schedule = card.schedule
    out = Lane(key='seg-3', label='Execution', text='')
    started = _local(segment.actual_start)
    start = started or dates.plan_start
    finish = schedule.forecast_finish or dates.plan_end
    words = []
    if segment.state == LANE_DONE and started is not None:
        end = max(_local(segment.actual_end) or started, started)
        out.pieces.append(Piece(ACTUAL, started, end))
        words.append(f'{short_date(started)} → {short_date(end)} · done')
    elif start is not None and finish is not None:
        if started is not None:
            out.pieces.append(Piece(ACTUAL, started, max(min(today, finish), started)))
            if finish > today:
                out.pieces.append(Piece(FORECAST, today + timedelta(days=1), finish))
        else:
            out.pieces.append(Piece(FORECAST, start, finish))
        words.append(f'{short_date(start)} → {short_date(finish)}')
    baseline = schedule.baseline_finish
    if dates.base_start is not None and baseline is not None:
        out.pieces.append(Piece(PLAN_LINE, dates.base_start, baseline))
        words.append(f'plan → {short_date(baseline)}')
    behind = schedule.days_behind or 0
    if behind > 0 and baseline is not None and finish is not None:
        out.pieces.append(Piece(LATE, baseline + timedelta(days=1), finish))
        words.append(f'{behind} day{"s" if behind != 1 else ""} behind plan')
    out.text = ' · '.join(words) or 'No dates yet'
    return out


def _block_lane(sequence, name, dates, today):
    """G5: one Execution block. Its forecast bar (scheduler start to due date), or its
    actual bar once every task is Done; its plan line (baseline start to baseline due);
    red from its due date to today only while it is open past that date. Later blocks are
    merely shifted against their plan line, never red. "M" when it holds a PM-typed date
    (not the Civil anchor)."""
    out = Lane(key=f'block-{sequence}', label=f'{sequence}. {name}', text='', block=True)
    words = []
    end = dates.plan_end
    if dates.n and not dates.open_n and dates.actual_end is not None:
        start = dates.actual_start or dates.actual_end
        out.pieces.append(Piece(ACTUAL, start, max(dates.actual_end, start)))
        words.append(f'Done {short_date(dates.actual_end)}' if start == dates.actual_end
                     else f'{short_date(start)} → {short_date(dates.actual_end)} · done')
    elif end is not None:
        if dates.single:
            out.pieces.append(Piece(DUE_MARK, end))
            words.append(f'due {short_date(end)}')
        else:
            out.pieces.append(Piece(FORECAST, dates.plan_start, end))
            words.append(f'{short_date(dates.plan_start)} → {short_date(end)}')
        if dates.open_n and end < today:
            out.pieces.append(Piece(LATE, end + timedelta(days=1), today))
            words.insert(0, f'Late · {short_date(end)} → now')
    elif not dates.n:
        words.append('Not applicable')
    else:
        words.append('Not dated')
    if dates.base_start is not None and dates.base_due is not None:
        out.pieces.append(Piece(PLAN_LINE, dates.base_start, dates.base_due))
        words.append(f'plan {short_date(dates.base_start)} → {short_date(dates.base_due)}'
                     if dates.base_start != dates.base_due
                     else f'plan {short_date(dates.base_due)}')
    if dates.manual and end is not None:
        out.pieces.append(Piece(MANUAL_MARK, end))
        words.append('PM date')
    out.text = ' · '.join(words)
    return out


def _site(card, rows, today):
    """One non-draft site's lanes from its card and its grouped rows."""
    scheduled = card.schedule.reason == REASON_SCHEDULED
    by_name = {}
    for lane in card.lanes:
        by_name.setdefault(lane.name, lane)
    # The scheduler plans the in-block tasks only. On a site it does not plan (needs a
    # Civil date, excluded, ...) the location copies are the only Execution dates some
    # sites have (UKRU001, G7), so they count there; on a scheduled site they would draw
    # a second, unplanned set of dates over the plan.
    execution_rows = [r for r in rows if r['gantt_block'] is not None
                      and (not r['gantt_copy'] or not scheduled)]
    lanes = []
    for index, (name, label) in enumerate(PHASE_LANES):
        if name == EXECUTION_LANE_NAME:
            dates = _merge(execution_rows)
            if scheduled:
                lane = _execution_lane(card, dates, today)
            else:
                lane = _plain_lane('seg-3', label, card.execution_segment, dates, today)
                reason = REASON_LINES.get(card.schedule.reason)
                lane.note = reason[0] if reason else None
                if lane.note:
                    lane.text = f'{lane.note} · {lane.text}'
        else:
            # A phase lane holds its non-Execution tasks, location copies included,
            # exactly as the card's lane does.
            dates = _merge([r for r in rows if r['phase__phase_name'] == name
                            and (r['gantt_block'] is None or r['gantt_copy'])])
            lane = _plain_lane(f'seg-{index}', label, by_name.get(name), dates, today)
        lanes.append(lane)

    by_block = defaultdict(list)
    for row in execution_rows:
        by_block[(row['gantt_block'], row['gantt_block_name'])].append(row)
    blocks = [_block_lane(seq, name, _merge(group), today)
              for (seq, name), group in sorted(by_block.items())]
    detail = lanes[:4] + blocks + lanes[4:]

    highlight = next((s.label for s in segments_for_card(card) if s.current), None)
    target = Piece(TARGET, card.target_date) if card.target_date else None
    return GanttSite(card=card, lanes=lanes, detail=detail, target=target,
                     highlight=highlight)


def _is_gantt_site(card):
    """G1: OPEX sites with a Program; CAPEX, Residential and standalone sites never."""
    return card.project_type == ELIGIBLE_PROJECT_TYPE and card.program_id is not None


def build_gantt(user, today=None):
    """Every site `user` may see on the Tenders Gantt (G1), with its lanes, and the
    timeline that holds them all. The scope is sites_visible_on_dashboard() (through
    build_site_cards) narrowed to OPEX sites with a Program; drafts included, without
    lanes. A fixed number of queries: build_site_cards()'s, plus lane_date_rows()."""
    today = today or timezone.localdate()
    cards = [c for c in build_site_cards(user, today=today) if _is_gantt_site(c)]
    live = [c for c in cards if not c.is_draft]
    # The same visible-pk SUBQUERY build_site_cards starts from, narrowed in SQL to the
    # non-draft OPEX sites with a Program, so the lane query never becomes a parameter
    # list of pks and never reads a site this page does not draw.
    site_pks = (sites_visible_on_dashboard(user)
                .filter(project_type=ELIGIBLE_PROJECT_TYPE, program__isnull=False)
                .exclude(status='Draft').values('pk'))
    rows_by_site = defaultdict(list)
    if live:
        for row in lane_date_rows(site_pks):
            rows_by_site[row['phase__project_id']].append(row)
    sites = [_site(c, rows_by_site.get(c.id, []), today) for c in live]
    drafts = [GanttSite(card=c, lanes=[], detail=[]) for c in cards if c.is_draft]

    dates = []
    for site in sites:
        for lane in site.detail:
            for piece in lane.pieces:
                dates.append(piece.start)
                dates.append(piece.end)
        if site.target is not None:
            dates.append(site.target.start)
    return GanttData(sites=sites, drafts=drafts, timeline=timeline_for(dates, today))


# ---------------------------------------------------------------------------
# Tender cards (G9)
# ---------------------------------------------------------------------------

#: The stacked bar's buckets: the highlighted segment of each active site (the furthest
#: started, as on the Site Dashboard), sites with none started, then drafts.
NOT_STARTED = 'Not started'
DRAFT = 'Draft'
BUCKETS = tuple(label for _, label in RESCO_SEGMENTS) + (NOT_STARTED, DRAFT)


@dataclass(frozen=True)
class TenderCard:
    program_id: int
    code: str
    name: str
    dc_kwp: object        # whole kWp with separators, or None when no site has one
    ac_kw: object
    active: int
    draft: int
    behind: int
    civil: int
    quiet: int
    buckets: tuple        # (label, slug, sites, width %) for every non-empty bucket


def _whole(value):
    return None if value is None else f'{round(value):,}'


def tender_cards(sites):
    """One TenderCard per Program among `sites` (GanttSite, drafts included), by code.
    Counted from exactly the sites given, so a card's counts are the rows the chart shows
    for that tender under the same search."""
    groups = defaultdict(list)
    for site in sites:
        groups[site.card.program_id].append(site)
    out = []
    for program_id, members in groups.items():
        first = members[0].card
        active = [s for s in members if not s.card.is_draft]
        counts = dict.fromkeys(BUCKETS, 0)
        for site in active:
            counts[site.highlight or NOT_STARTED] += 1
        counts[DRAFT] = len(members) - len(active)
        totals = capacity_totals([s.card for s in members])
        out.append(TenderCard(
            program_id=program_id, code=first.program_code, name=first.program_name,
            dc_kwp=_whole(totals.dc_kwp), ac_kw=_whole(totals.ac_kw),
            active=len(active), draft=counts[DRAFT],
            behind=sum(1 for s in active if (s.card.schedule.days_behind or 0) > 0),
            civil=sum(1 for s in active if s.card.schedule.reason == REASON_NEEDS_ANCHOR),
            quiet=sum(1 for s in active if s.card.quiet_days >= QUIET_DAYS),
            buckets=tuple((label, f'k{i}', n, _pct(n / len(members) * 100))
                          for i, (label, n) in enumerate(counts.items()) if n),
        ))
    return sorted(out, key=lambda c: (c.code or '', c.program_id))
