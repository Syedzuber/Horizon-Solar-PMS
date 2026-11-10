"""
DASH-B1 — the data behind the common Site Dashboard cards. No view, no template.

    cards = build_site_cards(request.user)          # every visible site, one SiteCard each
    sort_cards(cards, ORDER_URGENT)                  # or ORDER_LATEST (decision D7)
    capacity_totals(cards), programs_on_cards(cards)

WHO SEES WHAT is decided in permissions.py and nowhere here:
sites_visible_on_dashboard() is the site scope (D1, D9), user_sees_payment_totals() the
per-site payment figure (D8), and user_sees_money_activity_on_dashboard() whether the
activity feed shows money rows on every site or only on managed ones: together the
queryset form of user_can_see_project_money() (ruling 3). This module adds no role test
of its own.

A FIXED NUMBER OF QUERIES, whatever the number of sites (verification: tests assert the
same count for 3 and 30 sites). Every figure comes from one grouped aggregate, one
Window query or one Subquery over ALL visible sites at once; Python only merges the
already-aggregated rows (one per phase, per site, or per site x 5 feed rows). In
particular the two scheduler questions are answered WITHOUT calling the scheduler per
site:
    plan slip      one grouped Max, equal to scheduling.site_plan_summary()   (D6)
    schedule reason column terms in _plan()'s order, equal to
                   scheduling.schedule_site(apply=False).reason             (D6)
dashboard_data_check compares both, per site, against those functions on real data.

THE FIGURES, defined ONCE below by name (ruling 2):
    percentages      card_progress_q()   Internal, N/A out, mirrors IN, rounded down
    overdue          card_overdue_q()    Internal, human-owned, N/A out (PM card rule)
    blocked          card_blocked_q()    human-owned, N/A out, any task type
    lane state/dates lane_work_q()       every applicable task, any type, mirrors IN
"""
from dataclasses import dataclass, field, replace
from datetime import datetime

from django.db.models import (BooleanField, Case, Count, F, Max, Min, OuterRef, Q,
                              Subquery, Sum, Value, When, Window)
from django.db.models.functions import RowNumber
from django.utils import timezone

from .activity_codes import MONEY_ACTIVITY_CODES, NON_ACTIVITY_CODES
from .models import (ActivityLog, ContractorAttendance, DeliveryChallan, Issue,
                     PaymentRequest, Project, ProjectPhase, ScheduleBlockTask,
                     StatusTransition, Task, effective_amount_sum)
from .permissions import (manageable_projects_q, sites_visible_on_dashboard,
                          user_sees_money_activity_on_dashboard, user_sees_payment_totals)
from .scheduling import (ANCHOR_TASK_CODE, ELIGIBLE_PROJECT_TYPE, ELIGIBLE_STATUSES,
                         REASON_EXCLUDED, REASON_NEEDS_ANCHOR, REASON_NEEDS_CAPACITY,
                         REASON_NOT_ELIGIBLE, capacity_band)
from .task_health import OPEN_STATUSES, overdue_q
from .utils import applicable_tasks_q, human_owned_tasks_q, site_progress_tasks_q

#: The two orders the dashboard offers (D7).
ORDER_LATEST = 'latest'
ORDER_URGENT = 'urgent'
ORDERS = (ORDER_LATEST, ORDER_URGENT)

#: schedule_site() returns reason None for a site it schedules; the card says so in words.
REASON_SCHEDULED = 'scheduled'

#: A site above this DC capacity is flagged `large_site` on its card.
LARGE_SITE_KWP = 300

#: How many rows the card's activity feed and its "next" list hold.
FEED_LEN = 5
NEXT_LEN = 3

#: The lane holding the Execution tasks of an OPEX site (D5; every non-draft OPEX site
#: since DASH-B2 ruling 2, 10 Nov, scheduled or not).
EXECUTION_LANE_NAME = 'Execution'

#: Lane states, from the lane's applicable tasks of any type (ruling 2, option B).
LANE_EMPTY = 'empty'              # no applicable task at all
LANE_NOT_STARTED = 'not_started'
LANE_IN_PROGRESS = 'in_progress'
LANE_DONE = 'done'


# ---------------------------------------------------------------------------
# The figures — named once (ruling 2)
#
# TODO: views.dashboard_pm (internal_percent, its urgency aggregate) and
# views.project_overview (the per-phase `pct`) each spell the progress and overdue rules
# inline. They should call these four when the old dashboards are retired (DASH-B2).
# Deferred: DASH-B1 may not edit existing dashboard code, and both are pinned by their
# own tests (tests_progress_vs_workload) until then. dashboard_data_check holds the
# card and dashboard_pm equal on real data in the meantime.
# ---------------------------------------------------------------------------

def card_progress_q(prefix=''):
    """Q() for the tasks a progress percentage counts: Internal, not N/A, mirrors IN.

    Exactly dashboard_pm's `internal_total` and project_overview's per-phase `internal`:
    site COMPLETENESS (utils.site_progress_tasks_q, which keeps mirrors, because an
    undelivered consignment is unfinished work) narrowed to Internal work, with N/A out of
    numerator and denominator alike. `prefix` as in utils.human_owned_tasks_q()."""
    return (Q(**{f'{prefix}task_type': Task.INTERNAL})
            & site_progress_tasks_q(prefix) & applicable_tasks_q(prefix))


def card_overdue_q(today, prefix=''):
    """Q() for the tasks a card counts as overdue: the PM card's rule (D4).

    task_health.overdue_q() on Internal work, human-owned only (a mirror is nobody's
    late work) and not N/A. dashboard_pm's `internal_overdue` inside its human-owned,
    applicable task base, written as one Q so it can sit inside a grouped Count."""
    return (Q(**{f'{prefix}task_type': Task.INTERNAL}) & overdue_q(today, prefix)
            & human_owned_tasks_q(prefix) & applicable_tasks_q(prefix))


def card_blocked_q(prefix=''):
    """Q() for the tasks a card counts as blocked: Blocked, human-owned, not N/A, any task
    type — dashboard_pm's `blocked` count, the same rule on every lane (ruling 2)."""
    return (Q(**{f'{prefix}status': Task.BLOCKED})
            & human_owned_tasks_q(prefix) & applicable_tasks_q(prefix))


def lane_work_q(prefix=''):
    """Q() for the tasks a lane's STATE and actual dates read: every applicable task, any
    type, mirrors included (ruling 2, option B). Mirrors carry the real design and
    delivery dates — OPEX's Design and Procurement & Delivery phases are almost all
    mirrors — so leaving them out would show those lanes empty on nearly every site."""
    return applicable_tasks_q(prefix)


def percent(done, total):
    """dashboard_pm's arithmetic, kept to the letter: `int(done / total * 100)`, 0 when
    there is nothing to count. The float form matters: it is what the PM card shows, and
    `done * 100 // total` would differ from it on some values (29 of 100 -> 28 here)."""
    return int(done / total * 100) if total else 0


# ---------------------------------------------------------------------------
# Latest activity (D3)
# ---------------------------------------------------------------------------

def is_human_activity(action_code):
    """True when an ActivityLog row with this code counts as something a person did on the
    site (D3). Blank counts (older call sites write no code); `*_opened` (a pure read) and
    every code in activity_codes.NON_ACTIVITY_CODES do not."""
    code = action_code or ''
    return not (code.endswith('_opened') or code in NON_ACTIVITY_CODES)


def human_activity_q(prefix=''):
    """The queryset form of is_human_activity(): the two can never disagree because the
    tests assert both against the same rows. Positive terms on a NOT NULL column, so the
    negations cannot drop blank rows."""
    return ~(Q(**{f'{prefix}action_code__endswith': '_opened'})
             | Q(**{f'{prefix}action_code__in': NON_ACTIVITY_CODES}))


# ---------------------------------------------------------------------------
# The card
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Lane:
    """One phase of a site, or its Execution lane (D5)."""
    name: str
    total: int                 # Internal applicable tasks, mirrors in: the % denominator
    done: int                  # ... of them Done
    percent: int               # percent(done, total)
    work_total: int            # every applicable task, any type, mirrors in: the state
    work_done: int
    state: str                 # LANE_EMPTY / LANE_NOT_STARTED / LANE_IN_PROGRESS / LANE_DONE
    actual_start: object       # first move to In Progress / Done, else first completion
    actual_end: object         # last completion, only once every work task is Done
    overdue: int               # card_overdue_q
    blocked: int               # card_blocked_q


@dataclass(frozen=True)
class ActivityRow:
    at: datetime
    who: str                   # the actor's name, 'System' when there is none
    what: str                  # ActivityLog.action, as written


@dataclass(frozen=True)
class NextTask:
    name: str
    due: object
    external: bool = False     # Task.EXTERNAL: the card tags it "External" (DASH-B2 E7)


@dataclass(frozen=True)
class Schedule:
    """The site's place in the auto-schedule (D6). Every field but `reason` is None when
    the site has nothing to show."""
    reason: str                # REASON_SCHEDULED or one of scheduling's REASON_* values
    band: object               # scheduling.BAND_* key, only once scheduled
    anchor: object             # the Civil due date, only once scheduled
    baseline_finish: object    # = site_plan_summary().baseline_finish
    forecast_finish: object    # = site_plan_summary().forecast_finish
    days_behind: object        # = site_plan_summary().days_behind, never negative
    manual_dates: int          # in-block tasks (not the Civil anchor) with a PM-typed date, for B3


@dataclass(frozen=True)
class SiteCard:
    id: int
    project_id: str
    code: str                  # site_code, else project_id
    site_name: str             # site_name; for Residential (none) the customer name
    city: str
    project_type: str
    status: str
    is_draft: bool
    program_id: object
    program_code: object       # short_tender_code, else the program's name
    program_name: object
    pm_name: object
    dc_kwp: object             # Decimal or None — never 0 for blank (D10)
    ac_kw: object              # Decimal or None
    large_site: bool           # DC > LARGE_SITE_KWP
    activated_at: object
    created_at: object
    target_date: object
    current_phase: object      # phase name per utils.current_phase(); None for a Draft
    lanes: tuple               # Lane per phase, in phase order
    execution: object          # Lane for the Execution tasks of a non-draft OPEX site, else None
    progress: int              # percent() over the whole site, = dashboard_pm internal_percent
    overdue: int
    blocked: int
    open_issues: int
    dc_counts: dict            # {'expected', 'partial', 'received'}
    attendance_today: object   # headcount logged today, None when nothing logged
    payment_open: object       # Decimal; None when the viewer may not see it (D8)
    latest: object             # ActivityRow or None
    feed: tuple                # up to FEED_LEN ActivityRow, newest first
    next_tasks: tuple          # up to NEXT_LEN NextTask, soonest due first
    schedule: Schedule
    quiet_days: int            # days since the latest activity, else since activation/creation
    urgency: tuple = field(default=())   # (days behind or 0, overdue+blocked+issues, latest at)
    # The pipeline's Execution SEGMENT (DASH-B2 ruling 2): `execution` with its state and
    # actual dates also read from the location copies of block tasks; None when
    # `execution` is None. Display only: the copies stay in their phase lane and in every
    # count, and the scheduler's scope is unchanged.
    execution_segment: object = None


@dataclass(frozen=True)
class CapacityTotals:
    """D10: sums of the non-null capacities, and how many sites have none."""
    dc_kwp: object
    dc_missing: int
    ac_kw: object
    ac_missing: int


def _person_name(profile):
    if profile is None:
        return 'System'
    user = profile.user
    return (user.get_full_name() or user.username).strip()


def _in_block_q():
    """Q() for an Execution task (D5): the scheduler's own scope, scheduling._in_scope()
    (built from a template row, not a mirror, not a location copy), in an ACTIVE block —
    whatever phase holds it. The same in-block test site_plan_summary() applies."""
    block_rows = (ScheduleBlockTask.objects.filter(block__is_active=True)
                  .values('template_task_id'))
    return Q(template_task_id__in=block_rows, is_mirror=False, location_label='')


def _block_copy_q():
    """Q() for a LOCATION COPY of an Execution task (DASH-B2 ruling 2): a block task's
    template row, not a mirror, but a location_label. The scheduler leaves copies out
    (scheduling._in_scope), so they are never in _in_block_q(); the card reads them only
    for the Execution SEGMENT's state and actual dates. UKRU001's 25 copies all carry a
    block template row (checked on the 9 Nov restore before this was written)."""
    block_rows = (ScheduleBlockTask.objects.filter(block__is_active=True)
                  .values('template_task_id'))
    return Q(template_task_id__in=block_rows, is_mirror=False) & ~Q(location_label='')


# Public names for the two Execution tests, so another module (tenders_gantt, DASH-B3)
# reads the SAME rule without importing a private name. Aliases only: no behaviour of
# their own, and the private names stay for the code and tests already using them.
in_block_q = _in_block_q
block_copy_q = _block_copy_q


def _site_is_eligible_q():
    """scheduling._is_site_eligible() as column terms. Visible sites are already
    not deleted and not test (D9), so only the remaining terms are needed, but all are
    kept so the Q stays a faithful copy wherever it is used."""
    return Q(project_type=ELIGIBLE_PROJECT_TYPE, is_deleted=False, is_test=False,
             activated_at__isnull=False, status__in=ELIGIBLE_STATUSES)


def build_site_cards(user, today=None):
    """Every site `user` may see on the common dashboard, as a list of SiteCard in no
    particular order — pass it to sort_cards() for D7's two orders.

    `today` defaults to timezone.localdate() (IST), read once, so every figure on the page
    answers for the same day even across midnight.
    """
    today = today or timezone.localdate()
    visible = sites_visible_on_dashboard(user)
    # Every query below scopes itself with this SUBQUERY of the visible pks rather than a
    # Python list, so a portfolio of thousands of sites never becomes a parameter list.
    visible_pks = visible.values('pk')

    # 1. The sites, with the two relations every card prints.
    sites = list(visible.select_related('program', 'assigned_pm__user'))
    if not sites:
        return []

    # 2. Every phase of every site, in phase order: the lanes, and current_phase()'s walk.
    #    `pk` breaks phase_order ties so the order is stable (the model orders by
    #    phase_order alone).
    phases_by_site = {}
    for row in (ProjectPhase.objects.filter(project_id__in=visible_pks)
                .order_by('project_id', 'phase_order', 'pk')
                .values('id', 'project_id', 'phase_name')):
        phases_by_site.setdefault(row['project_id'], []).append(row)

    # 3. ONE grouped aggregate over every task of every visible site, one row per
    #    (phase, in Execution or not, location copy of an Execution task or not; the two
    #    flags never both hold). Everything per lane and per site is summed from
    #    these rows in Python: progress, overdue, blocked, state, dates, the current phase
    #    and the manual-date flag. The actual start of a task is its first move to In
    #    Progress or Done in the StatusTransition ledger, taken as a correlated subquery
    #    inside Min() so the ledger is not loaded row by row.
    first_start = (StatusTransition.objects
                   .filter(subject_type='task', subject_id=OuterRef('pk'),
                           to_status__in=[Task.IN_PROGRESS, Task.DONE])
                   .order_by('occurred_at').values('occurred_at')[:1])
    work = lane_work_q()
    done = Q(status=Task.DONE)
    human_open = human_owned_tasks_q() & applicable_tasks_q() & Q(status__in=OPEN_STATUSES)
    task_rows = (
        Task.objects.filter(phase__project_id__in=visible_pks)
        .annotate(in_block=Case(When(_in_block_q(), then=Value(True)),
                                default=Value(False), output_field=BooleanField()),
                  block_copy=Case(When(_block_copy_q(), then=Value(True)),
                                  default=Value(False), output_field=BooleanField()))
        .values('phase_id', 'in_block', 'block_copy')
        .annotate(
            total=Count('pk', filter=card_progress_q()),
            done=Count('pk', filter=card_progress_q() & done),
            work_total=Count('pk', filter=work),
            work_done=Count('pk', filter=work & done),
            work_started=Count('pk', filter=work & Q(status__in=[Task.IN_PROGRESS, Task.DONE])),
            overdue=Count('pk', filter=card_overdue_q(today)),
            blocked=Count('pk', filter=card_blocked_q()),
            first_move=Min(Subquery(first_start), filter=work),
            first_done=Min('completed_at', filter=work & done),
            last_done=Max('completed_at', filter=work & done),
            # utils.current_phase(): human-owned, applicable tasks; open ones make a phase
            # current, any of them makes it the fallback.
            human=Count('pk', filter=human_owned_tasks_q() & applicable_tasks_q()),
            human_open=Count('pk', filter=human_open),
            # Edge case 7: a PM-typed date inside a block is scheduled around, never
            # overwritten. Counted for B3's Gantt. The Civil anchor is left out: its date
            # is the PM's by rule, so it is manual on every scheduled site.
            manual=Count('pk', filter=Q(due_date_source=Task.DUE_DATE_MANUAL)
                         & applicable_tasks_q() & ~Q(template_task__code=ANCHOR_TASK_CODE)),
        )
        .order_by()
    )
    by_phase = {}
    for row in task_rows:
        by_phase.setdefault(row['phase_id'], []).append(row)

    # 4-7. Per-site counts, each one grouped query.
    open_issues = dict(
        Issue.objects.filter(project_id__in=visible_pks,
                             status__in=[Issue.OPEN, Issue.IN_PROGRESS])
        .values('project_id').annotate(n=Count('pk')).order_by()
        .values_list('project_id', 'n'))

    dc_key = {DeliveryChallan.EXPECTED: 'expected',
              DeliveryChallan.PARTIALLY_RECEIVED: 'partial',
              DeliveryChallan.RECEIVED: 'received'}
    dc_counts = {}
    for pid, status, n in (DeliveryChallan.objects
                           .filter(project_id__in=visible_pks, status__in=list(dc_key))
                           .values('project_id', 'status').annotate(n=Count('pk')).order_by()
                           .values_list('project_id', 'status', 'n')):
        dc_counts.setdefault(pid, {'expected': 0, 'partial': 0, 'received': 0})[dc_key[status]] = n

    # Contractor attendance writes no ActivityLog (D3); the card shows only whether
    # anything was logged today, and the headcount. Voided entries are not attendance.
    attendance = dict(
        ContractorAttendance.objects.filter(project_id__in=visible_pks, date=today,
                                            is_voided=False)
        .values('project_id').annotate(total=Sum('headcount')).order_by()
        .values_list('project_id', 'total'))

    # D8: open payment requests — pending approval, approved but unpaid (APPROVED; paid is
    # CONFIRMED), on hold — summed as payments.py sums them (effective_amount, O4b), and
    # only for a viewer who may see the figure. PaymentRequest.project is the lowest site
    # on the order, so the figure is indicative per site, not an exact attribution.
    sees_payments = user_sees_payment_totals(user)
    payments = {}
    if sees_payments:
        payments = dict(
            PaymentRequest.objects.filter(
                project_id__in=visible_pks,
                status__in=[PaymentRequest.PENDING_APPROVAL, PaymentRequest.APPROVED,
                            PaymentRequest.ON_HOLD])
            .values('project_id').annotate(total=effective_amount_sum()).order_by()
            .values_list('project_id', 'total'))

    # 8. The activity feed: the newest FEED_LEN human rows per site in one Window query
    #    (row 1 is "latest activity"). Filtering before numbering means older human rows
    #    are still found when the newest ones are all scheduler rows (edge case 3).
    feed_rows = (ActivityLog.objects.filter(project_id__in=visible_pks)
                 .filter(human_activity_q() & _money_rows_q(user))
                 .annotate(rn=Window(RowNumber(), partition_by=[F('project_id')],
                                     order_by=[F('timestamp').desc(), F('id').desc()]))
                 .filter(rn__lte=FEED_LEN)
                 .select_related('actor__user')
                 .order_by('project_id', 'rn'))
    feeds = {}
    for row in feed_rows:
        feeds.setdefault(row.project_id, []).append(
            ActivityRow(at=row.timestamp, who=_person_name(row.actor), what=row.action))

    # 9. The next NEXT_LEN dated, open, human-owned, applicable tasks per site, soonest
    #    due first, in one Window query. Same tasks the export's "next" list takes.
    next_rows = (Task.objects.filter(phase__project_id__in=visible_pks, due_date__isnull=False,
                                     status__in=OPEN_STATUSES)
                 .filter(human_owned_tasks_q() & applicable_tasks_q())
                 .annotate(rn=Window(RowNumber(), partition_by=[F('phase__project_id')],
                                     order_by=[F('due_date').asc(), F('phase__phase_order').asc(),
                                               F('task_order').asc(), F('pk').asc()]))
                 .filter(rn__lte=NEXT_LEN)
                 .order_by('phase__project_id', 'rn')
                 .values('phase__project_id', 'task_name', 'due_date', 'task_type'))
    next_tasks = {}
    for row in next_rows:
        next_tasks.setdefault(row['phase__project_id'], []).append(
            NextTask(name=row['task_name'], due=row['due_date'],
                     external=row['task_type'] == Task.EXTERNAL))

    # 10-13. The schedule (D6), from columns: no scheduler call per site.
    plans = _plan_summaries(visible_pks)
    reasons = _schedule_reasons(sites, visible_pks)

    cards = []
    for site in sites:
        cards.append(_card(site, today, phases_by_site.get(site.pk, []), by_phase,
                           open_issues, dc_counts, attendance, payments, sees_payments,
                           feeds.get(site.pk, []), next_tasks.get(site.pk, []),
                           plans.get(site.pk), reasons[site.pk]))
    return cards


def _money_rows_q(user):
    """Which feed rows carrying money the viewer may see (ruling 3): the queryset form of
    permissions.user_can_see_project_money(), the rule the project overview's Recent
    activity applies. Whoever permissions.user_sees_money_activity_on_dashboard() admits
    sees every money row; anyone else sees money rows only on sites they manage (assigned
    PM or coordinator, through manageable_projects_q, the queryset form of
    user_can_manage_project). The Design Head sees every site but manages none, so sees no
    money row. Inlined as a subquery, so it adds no query of its own."""
    # Portfolio money roles see every money line; the role test lives in permissions.py,
    # the same term user_can_see_project_money() answers True on before asking about the
    # project.
    if user_sees_money_activity_on_dashboard(user):
        return Q()
    # Everyone else: a money row only on a site they manage; non-money rows always.
    profile = getattr(user, 'profile', None)
    managed = (manageable_projects_q(profile) if profile is not None
               else Q(pk__in=[]))
    return (~Q(action_code__in=MONEY_ACTIVITY_CODES)
            | Q(project_id__in=Project.objects.filter(managed).values('pk')))


def _plan_summaries(visible_pks):
    """{site pk: (baseline_finish, forecast_finish, days_behind)} for every visible site
    scheduling.site_plan_summary() would answer for, in ONE grouped query (D6).

    The same terms as site_plan_summary(): the site must pass _is_site_eligible() and not
    be excluded (else None, so a leftover frozen baseline on an excluded site is never
    shown as a slip); the tasks are in an active block, not mirrors, not location copies,
    not N/A; and the answer is None when either Max is None."""
    eligible =(Project.objects.filter(pk__in=visible_pks)
                .filter(_site_is_eligible_q(), exclude_from_auto_schedule=False)
                .values('pk'))
    block_rows = (ScheduleBlockTask.objects.filter(block__is_active=True)
                  .values('template_task_id'))
    out = {}
    for row in (Task.objects.filter(phase__project_id__in=eligible,
                                    template_task_id__in=block_rows, is_mirror=False,
                                    location_label='', is_not_applicable=False)
                .values('phase__project_id')
                .annotate(baseline=Max('baseline_due'), forecast=Max('due_date'))
                .order_by()):
        if row['baseline'] is None or row['forecast'] is None:
            continue
        out[row['phase__project_id']] = (
            row['baseline'], row['forecast'],
            max((row['forecast'] - row['baseline']).days, 0))
    return out


def _schedule_reasons(sites, visible_pks):
    """{site pk: (reason, anchor task row or None)} — schedule_site(apply=False).reason
    for every visible site, from columns, in three queries (D6).

    The tests in scheduling._plan(), IN ITS ORDER, because the first that fails decides:
      1. not_eligible   the site row fails _is_site_eligible()
      2. not_eligible   the site's tasks come from no template or from more than one
                        (_site_template_id), or that template has no ACTIVE block holding
                        the Civil (anchor) task
      3. excluded       exclude_from_auto_schedule
      4. needs_capacity capacity_band(dc_capacity_kw) is None (blank or <= 0)
      5. needs_anchor   no anchor task, or it is N/A, or it has no due date
      6. scheduled
    The anchor task is _is_anchor_task(): in scope, code CIVIL_WORK_AND_MMS_INSTALLATION,
    in an active block; the first by (phase_order, task_order, pk), as _plan() sorts.
    """
    # The one template each site's tasks were built from (_site_template_id): None when
    # there are none or more than one. Every task with a template row counts, mirrors and
    # location copies included, exactly as that function reads them.
    template_of = {}
    for row in (Task.objects.filter(phase__project_id__in=visible_pks,
                                    template_task__isnull=False)
                .values('phase__project_id')
                .annotate(n=Count('template_task__phase__template_id', distinct=True),
                          t=Max('template_task__phase__template_id'))
                .order_by()):
        template_of[row['phase__project_id']] = row['t'] if row['n'] == 1 else None

    # Templates with an active block holding the anchor code: _plan()'s `anchor_block`.
    anchor_templates = set(ScheduleBlockTask.objects
                           .filter(block__is_active=True, template_task__code=ANCHOR_TASK_CODE)
                           .values_list('block__template_id', flat=True))

    # The anchor task per site: row 1 of a Window over the in-scope anchor-code tasks in
    # an active block, in _plan()'s sort order.
    anchors = {}
    for row in (Task.objects.filter(phase__project_id__in=visible_pks)
                .filter(_in_block_q(), template_task__code=ANCHOR_TASK_CODE)
                .annotate(rn=Window(RowNumber(), partition_by=[F('phase__project_id')],
                                    order_by=[F('phase__phase_order').asc(),
                                              F('task_order').asc(), F('pk').asc()]))
                .filter(rn=1)
                .values('phase__project_id', 'due_date', 'is_not_applicable')):
        anchors[row['phase__project_id']] = row

    out = {}
    for site in sites:
        eligible = (site.project_type == ELIGIBLE_PROJECT_TYPE and not site.is_deleted
                    and not site.is_test and site.activated_at is not None
                    and site.status in ELIGIBLE_STATUSES)
        anchor = anchors.get(site.pk)
        if not eligible or template_of.get(site.pk) not in anchor_templates:
            reason = REASON_NOT_ELIGIBLE
        elif site.exclude_from_auto_schedule:
            reason = REASON_EXCLUDED
        elif capacity_band(site.dc_capacity_kw) is None:
            reason = REASON_NEEDS_CAPACITY
        elif anchor is None or anchor['is_not_applicable'] or anchor['due_date'] is None:
            reason = REASON_NEEDS_ANCHOR
        else:
            reason = REASON_SCHEDULED
        out[site.pk] = (reason, anchor)
    return out


def _lane(name, rows):
    """Sum one or more task-aggregate rows into a Lane."""
    def total(key):
        return sum(r[key] for r in rows)

    def first(key, pick):
        values = [r[key] for r in rows if r[key] is not None]
        return pick(values) if values else None

    work_total, work_done = total('work_total'), total('work_done')
    first_move, first_done = first('first_move', min), first('first_done', min)
    starts = [v for v in (first_move, first_done) if v is not None]
    if not work_total:
        state = LANE_EMPTY
    elif work_done == work_total:
        state = LANE_DONE
    elif total('work_started'):
        state = LANE_IN_PROGRESS
    else:
        state = LANE_NOT_STARTED
    lane_total, lane_done = total('total'), total('done')
    return Lane(
        name=name, total=lane_total, done=lane_done, percent=percent(lane_done, lane_total),
        work_total=work_total, work_done=work_done, state=state,
        actual_start=min(starts) if starts else None,
        actual_end=first('last_done', max) if state == LANE_DONE else None,
        overdue=total('overdue'), blocked=total('blocked'),
    )


def _card(site, today, phases, by_phase, open_issues, dc_counts, attendance, payments,
          sees_payments, feed, next_tasks, plan, reason_and_anchor):
    reason, anchor = reason_and_anchor
    scheduled = reason == REASON_SCHEDULED
    is_draft = site.status == 'Draft'

    # D5, widened by DASH-B2 ruling 2 (10 Nov): on EVERY non-draft OPEX site, scheduled or
    # not, the Execution tasks leave their phases and are summed once as the Execution
    # lane, so every RESCO card draws the same six-segment pipeline. The test is the same
    # by-task rule either way (_in_block_q), so a site's Execution does not change shape
    # the day its Civil date arrives. Residential, CAPEX and drafts report every phase as
    # it is (a draft OPEX site has no tasks yet).
    split = site.project_type == ELIGIBLE_PROJECT_TYPE and not is_draft
    lanes, execution_rows, copy_rows, all_rows = [], [], [], []
    current = last_human = None
    for phase in phases:
        groups = by_phase.get(phase['id'], [])
        all_rows.extend(groups)
        if split:
            lane_rows = [r for r in groups if not r['in_block']]
            execution_rows.extend(r for r in groups if r['in_block'])
            # Location copies stay in their phase lane (above) and in every count; they
            # are only read again for the Execution segment's state and dates.
            copy_rows.extend(r for r in groups if r['block_copy'])
        else:
            lane_rows = list(groups)
        lanes.append(_lane(phase['phase_name'], lane_rows))
        # utils.current_phase(): the first phase with an open human-owned applicable task,
        # else the last phase holding any; asked of the phase's whole task set, Execution
        # tasks included, because that function knows nothing of Execution.
        human = sum(r['human'] for r in groups)
        if human:
            last_human = phase['phase_name']
            if current is None and sum(r['human_open'] for r in groups):
                current = phase['phase_name']
    execution = _lane(EXECUTION_LANE_NAME, execution_rows) if split else None
    segment = _execution_segment(execution, execution_rows, copy_rows) if split else None

    total_pct = sum(r['total'] for r in all_rows)
    done_pct = sum(r['done'] for r in all_rows)
    overdue = sum(r['overdue'] for r in all_rows)
    blocked = sum(r['blocked'] for r in all_rows)
    issues = open_issues.get(site.pk, 0)

    baseline, forecast, behind = plan if plan else (None, None, None)
    schedule = Schedule(
        reason=reason,
        band=capacity_band(site.dc_capacity_kw) if scheduled else None,
        anchor=anchor['due_date'] if scheduled else None,
        baseline_finish=baseline, forecast_finish=forecast, days_behind=behind,
        manual_dates=sum(r['manual'] for r in all_rows if r['in_block']),
    )

    latest = feed[0] if feed else None
    if latest is not None:
        quiet_from = timezone.localdate(latest.at)
    else:
        quiet_from = timezone.localdate(site.activated_at or site.created_at)
    program = site.program
    pm = site.assigned_pm
    return SiteCard(
        id=site.pk, project_id=site.project_id, code=site.site_code or site.project_id,
        site_name=site.site_name or site.customer_name or '', city=site.city or '',
        project_type=site.project_type, status=site.status, is_draft=is_draft,
        program_id=site.program_id,
        program_code=(program.short_tender_code or program.name) if program else None,
        program_name=program.name if program else None,
        pm_name=_person_name(pm) if pm else None,
        dc_kwp=site.dc_capacity_kw, ac_kw=site.ac_capacity_kw,
        large_site=site.dc_capacity_kw is not None and site.dc_capacity_kw > LARGE_SITE_KWP,
        activated_at=site.activated_at, created_at=site.created_at,
        target_date=site.target_commissioning_date,
        current_phase=None if is_draft else (current or last_human),
        lanes=tuple(lanes), execution=execution,
        progress=percent(done_pct, total_pct), overdue=overdue, blocked=blocked,
        open_issues=issues,
        dc_counts=dc_counts.get(site.pk, {'expected': 0, 'partial': 0, 'received': 0}),
        attendance_today=attendance.get(site.pk),
        payment_open=(payments.get(site.pk) if sees_payments else None),
        latest=latest, feed=tuple(feed), next_tasks=tuple(next_tasks), schedule=schedule,
        quiet_days=(today - quiet_from).days,
        urgency=(behind or 0, overdue + blocked + issues, latest.at if latest else None),
        execution_segment=segment,
    )


def _execution_segment(execution, execution_rows, copy_rows):
    """The pipeline's Execution segment (DASH-B2 ruling 2): the Execution lane, with its
    STATE and ACTUAL DATES read from the block tasks AND their location copies, so a
    multi-location site (UKRU001) shows Execution under way once any location has started.
    Every count (total, done, percent, work_*, overdue, blocked) stays the lane's own:
    the copies are already counted in their phase lane and in the site totals, and the
    scheduler's scope does not include them. The lane itself when there are no copies."""
    if not copy_rows:
        return execution
    both = _lane(EXECUTION_LANE_NAME, execution_rows + copy_rows)
    return replace(execution, state=both.state, actual_start=both.actual_start,
                   actual_end=both.actual_end)


# ---------------------------------------------------------------------------
# Sorting (D7), totals (D10), programs (edge case 10)
# ---------------------------------------------------------------------------

def _newest_first(moment):
    """A sort key putting the newest moment first and None after every moment."""
    return (1, 0) if moment is None else (0, -moment.timestamp())


def sort_cards(cards, order):
    """Return `cards` in D7's `order` (ORDER_LATEST or ORDER_URGENT). DRAFTS ALWAYS LAST,
    newest created first among themselves, whichever order.

        ORDER_LATEST  latest activity, newest first; a site with no activity after every
                      site with some
        ORDER_URGENT  days behind (none = 0), highest first; then overdue + blocked +
                      open issues, highest first; then latest activity as above

    Every remaining tie falls to project_id, ascending, so two sites with identical
    figures and timestamps never swap places between two page loads (edge case 8).
    """
    if order not in ORDERS:
        raise ValueError(f'unknown order {order!r}; expected one of {ORDERS}')

    def key(card):
        if card.is_draft:
            return (1, _newest_first(card.created_at), card.project_id)
        if order == ORDER_LATEST:
            return (0, _newest_first(card.urgency[2]), card.project_id)
        return (0, -card.urgency[0], -card.urgency[1], _newest_first(card.urgency[2]),
                card.project_id)

    return sorted(cards, key=key)


def capacity_totals(cards):
    """D10: total DC kWp and AC kW over the sites that have one, and how many have none.
    A blank capacity is skipped, never counted as 0."""
    dc = [c.dc_kwp for c in cards if c.dc_kwp is not None]
    ac = [c.ac_kw for c in cards if c.ac_kw is not None]
    return CapacityTotals(dc_kwp=sum(dc) if dc else None, dc_missing=len(cards) - len(dc),
                          ac_kw=sum(ac) if ac else None, ac_missing=len(cards) - len(ac))


def programs_on_cards(cards):
    """The programs that have at least one card, in first-seen order, as
    (program_id, code, name). Built from the cards and nothing else, so a program with no
    site the viewer may see never appears (edge case 10)."""
    seen = {}
    for card in cards:
        if card.program_id is not None and card.program_id not in seen:
            seen[card.program_id] = (card.program_id, card.program_code, card.program_name)
    return list(seen.values())
