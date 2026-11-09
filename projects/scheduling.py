"""Task due-date scheduling (SCHED, spec: SCHED_Auto_Scheduling_Spec.md).

SCHED-1: set_task_due_date(), the single writer of Task.due_date. Every production
write site routes through it, so every date carries a source (`manual` / `system`) and
every manual change leaves one ActivityLog row. The scheduler below rewrites only
`system` dates, which is why a write that bypassed this function would be a bug: a
person's date left marked `system` (or a stale `system` mark under a typed date) gets
overwritten.

The one known exception is management/commands/seed_opex_test_data.py (ruling D),
a demo-data command that writes due_date with a queryset update. Its rows carry a
date and a null source, which the scheduler treats as untouchable.

SCHED-2: schedule_site(), the RESCO auto-scheduler. The anchor is the due date of the
site's unlabelled Civil Work and MMS Installation task, whoever set it. Block 1 (Civil)
is never written; from the next working day after the Civil date, every task of each
later ScheduleBlock gets one planned start and one system due date from the site's DC
capacity band (spec Layer 4, as changed 9 Nov). It is called by the `schedule_sites`
command, by the Admin working-day switch, and after commit by the triggers (a Civil
date edit, a task reaching or leaving Done, Not Applicable, a capacity or exclusion
edit) through schedule_site_safely(), which never raises.

SCHED-2b: the forecast. A block still open after its planned due date is forecast to
finish today, so the later system dates re-anchor to the next working day after today;
only the late block shows red. "Today" is read once, in schedule_site(), so a daily
`schedule_sites --all --apply` (06:30 IST, before the morning report) moves the forecast
on by a day, and tests freeze it there.
"""
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from types import SimpleNamespace

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import (ActivityLog, Project, ScheduleBlock, ScheduleBlockTask, SystemSettings,
                     Task, log_activity)
from .task_health import is_overdue
from .utils import is_human_owned

logger = logging.getLogger(__name__)

MANUAL = Task.DUE_DATE_MANUAL
SYSTEM = Task.DUE_DATE_SYSTEM

# ActivityLog.action is max_length=255; a long task name plus the dates and a note
# would overflow it, and on Postgres an overflow is an error, not a truncation.
_ACTION_MAX = ActivityLog._meta.get_field('action').max_length


def _show(value):
    """A date as the log shows it. isoformat, not strftime: %Y prints a mistyped
    year 20 differently on Linux and Windows, and the reader has to recognise it."""
    return value.isoformat() if value else 'not set'


def set_task_due_date(task, new_date, source, actor, note=None, log=True):
    """Set `task.due_date` and `task.due_date_source` IN MEMORY. The caller saves.

    Why the caller saves: the 7 write sites save in different ways (`save()`, a
    `bulk_update()` over a cascade, `save(update_fields=...)` after a create, the
    admin's own save) and some run inside a transaction they own. Saving here would
    either double-write or step outside that transaction. So this function changes the
    instance and, for a manual change, writes the log row; persisting the instance is
    the caller's job, in the same request, straight after the call.

    `source` is Task.DUE_DATE_MANUAL ('manual', a person typed it) or
    Task.DUE_DATE_SYSTEM ('system', a calculation produced it). A None `new_date`
    clears both the date and the source.

    LOGGING (ruling A). When `log` is true and the source is manual, one ActivityLog
    row with action_code='due_date_changed' records the old and new dates:
        "Updated due date for task: X (was 2026-11-01, now 2026-11-08)"
        "Cleared due date for task: X (was 2026-11-01, now not set)"
    The opening words are the ones task_set_due_date always wrote, so anything that
    read the old lines by their text still finds them. This is the ONLY row for the
    change; callers do not log it themselves. No row is written when the date is
    unchanged (a source flip on the same date is not a date change). System callers
    pass log=False: the SCHED-2 scheduler logs once per site per run, and the
    Residential calculation keeps its own summary row.

    The log row needs `task.pk`, so a task being created must be saved (undated) before
    this is called with logging on. Raises ValueError otherwise, which is a programming
    error at the call site, not a runtime condition.

    Returns the previous due date.
    """
    if source not in (MANUAL, SYSTEM):
        raise ValueError(f'set_task_due_date: source must be {MANUAL!r} or {SYSTEM!r}, got {source!r}')

    old_date = task.due_date
    task.due_date = new_date
    task.due_date_source = source if new_date is not None else None

    if log and source == MANUAL and old_date != new_date:
        if task.pk is None:
            raise ValueError('set_task_due_date: save the task before logging a manual date change')
        _log_manual_change(task, old_date, new_date, actor, note)
    return old_date


def _log_manual_change(task, old_date, new_date, actor, note):
    """Write the one `due_date_changed` row, in its own savepoint.

    Not log_activity(): that swallows its own exception, but Model.save() has already
    flagged the caller's atomic block for rollback (mark_for_rollback_on_error), so the
    block's exit would silently roll back the caller's work, and on Postgres every query
    before that exit fails with "current transaction is aborted". Here the exception
    leaves this savepoint, which rolls back to it and clears the flag, and the failure
    is logged, so a failed log row can never abort the date change (ruling A). Proved
    against a database-refused INSERT in tests_sched1_due_date_source.
    """
    verb = 'Updated' if new_date is not None else 'Cleared'
    text = (f'{verb} due date for task: {task.task_name} '
            f'(was {_show(old_date)}, now {_show(new_date)})')
    if note:
        text += f'. {note}'
    if len(text) > _ACTION_MAX:
        text = text[:_ACTION_MAX - 1] + '…'
    try:
        with transaction.atomic():
            ActivityLog.objects.create(
                project=task.phase.project,
                actor=actor,
                action=text,
                entity_type='Task',
                entity_id=task.pk,
                action_code='due_date_changed',
            )
    except Exception:
        logger.exception('due_date_changed ActivityLog row failed for task %s', task.pk)


# ===========================================================================
# SCHED-2 — the RESCO auto-scheduler
# ===========================================================================

# The anchor task. Its DUE DATE (whatever its source) is the anchor of the whole site,
# and its block (block 1) is never written: the Civil date is the PM's. Matched by
# template task CODE, never by its label (spec: link by code).
ANCHOR_TASK_CODE = 'CIVIL_WORK_AND_MMS_INSTALLATION'

# Which sites are scheduled at all (pre-flight item 11, as ruled): RESCO, live, real,
# activated, and built from a template that has active blocks. On Hold, Commissioned,
# Cancelled and Draft are left alone, as the dashboards and the morning report leave them.
ELIGIBLE_PROJECT_TYPE = 'OPEX'
ELIGIBLE_STATUSES = ('Active', 'In Progress')

# Capacity bands: the key the result reports, the label people read, and the
# ScheduleBlock column holding the band's length. Labels are plain ASCII: they go to
# the command's console (a Windows cp1252 console cannot print a '≤') and the log.
BAND_LE_50, BAND_51_100, BAND_101_PLUS = 'le_50', '51_100', '101_plus'
BAND_LABELS = {BAND_LE_50: '<=50', BAND_51_100: '51-100', BAND_101_PLUS: '101+'}
BAND_FIELDS = {BAND_LE_50: 'days_le_50', BAND_51_100: 'days_51_100',
               BAND_101_PLUS: 'days_101_plus'}

# Why a site got no schedule. None on a ScheduleResult means it was scheduled.
REASON_NOT_ELIGIBLE = 'not_eligible'
REASON_EXCLUDED = 'excluded'
REASON_NEEDS_CAPACITY = 'needs_capacity'
REASON_NEEDS_ANCHOR = 'needs_anchor'

# Where the anchor came from. One source today: the Civil task's due date.
ANCHOR_CIVIL_DUE_DATE = 'civil_due_date'

# What happens to each task.
ACTION_WRITE = 'write'                # system due date + planned start (blocks 2+)
ACTION_WRITE_START = 'write:start'    # the Civil task: planned start only, never its due date
ACTION_UNCHANGED = 'unchanged'
ACTION_CLEAR = 'clear'                # system due date + planned start removed
ACTION_CLEAR_START = 'clear:start'    # the Civil task: planned start removed, date kept
SKIP_MANUAL = 'skip:manual'
SKIP_DONE = 'skip:done'
SKIP_NA = 'skip:na'
SKIP_ANCHOR_BLOCK = 'skip:anchor-block'   # any other task put in block 1: never written

SCHEDULED_ACTION_CODE = 'due_dates_scheduled'
# SCHED-2b: the row for dates moved by the forecast rule. Always written with no actor,
# trigger runs included (ruling 1, 9 Nov): the calendar moved the dates, not the person
# whose action happened to run the scheduler, so it must not count as their activity.
FORECAST_ACTION_CODE = 'due_dates_forecast_shift'


@dataclass
class TaskPlan:
    """One task's outcome in a run. `start`/`due` are the dates the scheduler wants;
    they are only written for ACTION_WRITE (both) and ACTION_WRITE_START (start)."""
    task: Task
    action: str
    start: object = None
    due: object = None


@dataclass
class BlockPlan:
    """One block on one site: its planned dates and what happens to each task. For
    block 1 `due` is the Civil task's own due date and `start` the start derived from
    it; neither is a system due date."""
    block: ScheduleBlock
    start: object            # planned start (None for a zero-day block)
    due: object              # planned due (None for a zero-day block)
    half_days: int           # this block's length on this site, in half days
    tasks: list = field(default_factory=list)   # TaskPlan rows
    re_anchored_after: object = None            # the block's last Done date, when it finished late
    forecast_after: object = None               # today, when the block is open past due and re-anchored the rest
    first_dated: object = None                  # its own new start, when it was past plan and never system-dated


@dataclass
class ScheduleResult:
    """What schedule_site() decided for one site, and (with apply=True) what it wrote."""
    site: Project
    reason: str = None               # None = scheduled; else a REASON_* constant
    band: str = None
    anchor: object = None            # the Civil task's due date
    anchor_source: str = None        # ANCHOR_CIVIL_DUE_DATE, or None
    blocks: list = field(default_factory=list)
    clears: list = field(default_factory=list)  # TaskPlan rows: ACTION_CLEAR / ACTION_CLEAR_START
    overdue_after: int = None        # site tasks overdue today once applied (shared rule)
    overdue_already: int = None      # ...of which were overdue before this run
    overdue_new: int = None          # ...of which become overdue because of this run
    applied: bool = False
    written: int = 0                 # due dates actually written (apply only)
    started: int = 0                 # Civil planned starts actually written (apply only)
    cleared: int = 0                 # rows actually cleared, either kind (apply only)
    forecast: object = None          # the BlockPlan open past due that re-anchored the rest, or the
                                     # first-dated block that re-anchored itself (SCHED-2b)
    forecast_start: object = None    # the new day 1: the next working day after today
    forecast_moved: int = 0          # of `written`, the dates after the forecast block (apply only)

    @property
    def writes(self):
        return [tp for bp in self.blocks for tp in bp.tasks if tp.action == ACTION_WRITE]

    @property
    def forecast_writes(self):
        """The due-date writes the forecast rules cause: those in the blocks after a late
        block, or, for a first-dated block, its own and those after it. Empty when no
        block is open past plan. Found by identity, not ==, because two BlockPlans can
        hold equal values."""
        if self.forecast is None:
            return []
        index = next(i for i, bp in enumerate(self.blocks) if bp is self.forecast)
        if self.forecast.first_dated is None:
            index += 1
        return [tp for bp in self.blocks[index:] for tp in bp.tasks
                if tp.action == ACTION_WRITE]

    @property
    def to_forecast(self):
        return len(self.forecast_writes)

    @property
    def start_writes(self):
        return [tp for bp in self.blocks for tp in bp.tasks if tp.action == ACTION_WRITE_START]

    @property
    def to_write(self):
        return len(self.writes)

    @property
    def to_start(self):
        return len(self.start_writes)

    @property
    def to_clear(self):
        return len(self.clears)

    @property
    def changes(self):
        """Every date this run would change: due dates, Civil starts and clears."""
        return self.to_write + self.to_start + self.to_clear

    def summary_line(self):
        """One line for the command: code, outcome, band, anchor, counts."""
        code = self.site.project_id
        if self.reason == REASON_NOT_ELIGIBLE:
            return f'{code:<12} {self.reason}'
        band = BAND_LABELS.get(self.band, '-')
        anchor = (f'{self.anchor.isoformat()} ({self.anchor_source})'
                  if self.anchor else '-')
        status = self.reason or 'scheduled'
        return (f'{code:<12} {status:<15} band={band:<7} anchor={anchor:<28} '
                f'write={self.to_write} start={self.to_start} clear={self.to_clear} '
                f'overdue_after={self.overdue_after} '
                f'(already {self.overdue_already}, new {self.overdue_new})')


def candidate_sites():
    """Every activated, not-deleted RESCO site: what `schedule_sites --all` walks.

    Wider than eligible_sites() on purpose, so the command prints why a test, excluded
    or On Hold site was left alone instead of hiding it; schedule_site() decides.
    """
    return Project.objects.filter(project_type=ELIGIBLE_PROJECT_TYPE, is_deleted=False,
                                  activated_at__isnull=False)


def eligible_sites():
    """The sites the scheduler would schedule (pre-flight item 11 plus ruling a).

    The template condition is "has a task built from a template that has an active
    block": data-driven, so a later template version with no blocks is not eligible
    until its blocks are seeded, and CAPEX joins by seeding rows (spec, Other rules).
    distinct() because the template join fans out over every task of the site.
    """
    return candidate_sites().filter(
        is_test=False,
        exclude_from_auto_schedule=False,
        status__in=ELIGIBLE_STATUSES,
        phases__tasks__template_task__phase__template__schedule_blocks__is_active=True,
    ).distinct()


def capacity_band(dc_kw):
    """The band key for a DC capacity, or None when it is unknown (null or <= 0).
    Decimal comparisons throughout: 50.0 is <=50, 50.1 is 51-100, 100.0 is 51-100."""
    if dc_kw is None or dc_kw <= 0:
        return None
    if dc_kw <= 50:
        return BAND_LE_50
    if dc_kw <= 100:
        return BAND_51_100
    return BAND_101_PLUS


def _half_days(days):
    """A block length (Decimal days, a multiple of 0.5) as an integer count of half
    days. Integers from here on, never floats (spec Layer 5). The model's CHECK makes a
    non-multiple impossible; refusing one here keeps a bad row from being rounded."""
    doubled = days * 2
    if doubled != doubled.to_integral_value():
        raise ValueError(f'block length {days} is not a multiple of 0.5 days')
    return int(doubled)


def working_day(anchor, n, skip_sundays):
    """The n-th day of a schedule whose day 1 is `anchor` (n >= 1).

    With skip_sundays, Sundays are not counted, and a Sunday `anchor` itself moves to
    Monday (spec Layer 5), so day 1 is never a Sunday. Without it, calendar days.
    """
    day = anchor
    if skip_sundays and day.weekday() == 6:
        day += timedelta(days=1)
    counted = 1
    while counted < n:
        day += timedelta(days=1)
        if skip_sundays and day.weekday() == 6:
            continue
        counted += 1
    return day


def working_day_back(last, n, skip_sundays):
    """The day on which an n-day piece of work starts if `last` is its n-th (last) day:
    `last` counted back n - 1 working days. Same day-counting as working_day(), run
    backwards; `last` itself is the PM's date and is taken as it is, Sunday or not."""
    day = last
    counted = 1
    while counted < n:
        day -= timedelta(days=1)
        if skip_sundays and day.weekday() == 6:
            continue
        counted += 1
    return day


def next_working_day(after, skip_sundays):
    """The first working day strictly after `after` (a Sunday rolls on to Monday)."""
    return working_day(after + timedelta(days=1), 1, skip_sundays)


def _site_template_id(tasks):
    """The one template the site's tasks were built from, or None when there is none
    or more than one (activation builds a site from exactly one template)."""
    ids = {t.template_task.phase.template_id for t in tasks if t.template_task_id}
    return ids.pop() if len(ids) == 1 else None


def _is_site_eligible(project):
    """The per-row half of eligible_sites(), asked before any query."""
    return (project.project_type == ELIGIBLE_PROJECT_TYPE
            and not project.is_deleted
            and not project.is_test
            and project.activated_at is not None
            and project.status in ELIGIBLE_STATUSES)


def _in_scope(task):
    """A task the scheduler may ever consider: built from a template row, not a mirror
    (its status is derived; spec Layer 4), not a location copy (those belong to their
    PM; UKRU001 only)."""
    return task.template_task_id is not None and not task.is_mirror and not task.location_label


def _is_anchor_task(task):
    """The site's unlabelled Civil Work and MMS Installation task (ruling: the anchor)."""
    return _in_scope(task) and task.template_task.code == ANCHOR_TASK_CODE


def _owned_by_a_person(task):
    """True when the date belongs to a person and the scheduler must not touch it:
    `manual`, or a date with no source at all (the demo seed, ruling D)."""
    return (task.due_date_source == MANUAL
            or (task.due_date_source is None and task.due_date is not None))


def _local_date(moment):
    return timezone.localtime(moment).date()


def _clear_plan(result, later_tasks, anchor_task):
    """For a site that is not (or no longer) scheduled: every system date on a task of
    blocks 2+ that is still open goes, and so does the Civil task's derived planned
    start, so a stale auto date never sits there looking planned. Manual dates, Done
    and N/A tasks, and the Civil task's own due date are not touched."""
    # TODO: a block made inactive, or a task taken out of a block, keeps the system dates
    # it already has: "never write a task that is not in a block" (SCHED-2 prompt) also
    # forbids clearing it. Deferred because the table has no such edit yet; if one is
    # made, clear those tasks' system dates in the same admin action (SECONDARY_FINDINGS).
    result.clears = [TaskPlan(task=t, action=ACTION_CLEAR) for t in later_tasks
                     if t.due_date_source == SYSTEM and t.status != Task.DONE
                     and not t.is_not_applicable]
    if (anchor_task is not None and anchor_task.schedule_start is not None
            and anchor_task.status != Task.DONE and not anchor_task.is_not_applicable):
        result.clears.append(TaskPlan(task=anchor_task, action=ACTION_CLEAR_START))


def _plan(project, skip_sundays, today):
    """Decide the whole schedule for one site. Reads only; writes nothing."""
    result = ScheduleResult(site=project)

    if not _is_site_eligible(project):
        result.reason = REASON_NOT_ELIGIBLE
        return result

    # Every task of the site with its template row and phase, in one query: the
    # scheduler reads all of them for the template, the anchor, the blocks and the
    # overdue preview, so per-block queries would only repeat this one.
    tasks = list(Task.objects.filter(phase__project=project)
                 .select_related('template_task__phase', 'phase'))
    template_id = _site_template_id(tasks)
    # The template's active blocks with their task links and codes, prefetched: the
    # code finds the anchor block, and every link maps a template task to its block.
    blocks = (list(ScheduleBlock.objects.filter(template_id=template_id, is_active=True)
                   .prefetch_related('block_tasks__template_task').order_by('sequence'))
              if template_id else [])
    anchor_block = next((b for b in blocks
                         if any(bt.template_task.code == ANCHOR_TASK_CODE
                                for bt in b.block_tasks.all())), None)
    if anchor_block is None:
        result.reason = REASON_NOT_ELIGIBLE
        return result

    block_of = {bt.template_task_id: block for block in blocks for bt in block.block_tasks.all()}
    by_block = {block.pk: [] for block in blocks}
    for t in tasks:
        if _in_scope(t) and t.template_task_id in block_of:
            by_block[block_of[t.template_task_id].pk].append(t)
    for members in by_block.values():
        members.sort(key=lambda t: (t.phase.phase_order, t.task_order, t.pk))
    # Blocks after the anchor block are the ones the scheduler dates. (Every seeded
    # block comes after it; a block placed before it would have nothing to count from.)
    later_blocks = [b for b in blocks if b.sequence > anchor_block.sequence]
    later_tasks = [t for b in later_blocks for t in by_block[b.pk]]
    # The unlabelled Civil task. Normally exactly one; if a site ever had two, the first
    # in task order is the anchor and the other is left alone.
    anchor_task = next((t for t in by_block[anchor_block.pk] if _is_anchor_task(t)), None)

    # Ruling a, as amended: an excluded site gets nothing written, and the system dates
    # it already has are cleared, so they stop looking planned.
    if project.exclude_from_auto_schedule:
        result.reason = REASON_EXCLUDED
        _clear_plan(result, later_tasks, anchor_task)
        return _with_overdue(result, tasks, today)

    result.band = capacity_band(project.dc_capacity_kw)
    if result.band is None:
        result.reason = REASON_NEEDS_CAPACITY
        _clear_plan(result, later_tasks, anchor_task)
        return _with_overdue(result, tasks, today)

    if (anchor_task is None or anchor_task.is_not_applicable
            or anchor_task.due_date is None):
        result.reason = REASON_NEEDS_ANCHOR
        _clear_plan(result, later_tasks, anchor_task)
        return _with_overdue(result, tasks, today)

    band_field = BAND_FIELDS[result.band]
    civil_due = anchor_task.due_date
    result.anchor, result.anchor_source = civil_due, ANCHOR_CIVIL_DUE_DATE

    # BLOCK 1 IS NEVER WRITTEN. Its table length only places the Civil task's planned
    # start: the due date is the block's last day, so the start is the due date counted
    # back (days - 1) working days. The due date itself is the PM's.
    anchor_half_days = _half_days(getattr(anchor_block, band_field))
    civil_start = working_day_back(civil_due, max((anchor_half_days + 1) // 2, 1), skip_sundays)
    plan = BlockPlan(block=anchor_block, start=civil_start, due=civil_due,
                     half_days=anchor_half_days)
    for t in by_block[anchor_block.pk]:
        plan.tasks.append(TaskPlan(task=t, action=_anchor_block_action(t, anchor_task, civil_start),
                                   start=civil_start if t is anchor_task else None))
    result.blocks.append(plan)

    # Block 2 starts on the next working day after the Civil due date, with the count C
    # reset to 0 there (ruling: "rule as written", 9 Nov). A late Civil finish moves that
    # day: the next working day after its Done date instead. An early finish moves
    # nothing; a plan is never pulled earlier.
    restart_after = civil_due
    if anchor_task.status == Task.DONE and anchor_task.completed_at:
        done_on = _local_date(anchor_task.completed_at)
        if done_on > civil_due:
            plan.re_anchored_after = done_on
            restart_after = done_on
    base = next_working_day(restart_after, skip_sundays)
    elapsed = 0   # half days used since `base`: the running total C of spec Layer 4

    # FORECAST, block 1 (SCHED-2b). The Civil task is "complete" here exactly when it is
    # Done, the same test the late-finish rule above uses, so the two rules never both
    # apply: an open Civil past its due date re-anchors block 2 to the next working day
    # after today. The Civil date itself is never changed.
    if anchor_task.status != Task.DONE and civil_due < today:
        forecast_base = _forecast_re_anchor(result, plan, base, today, skip_sundays)
        if forecast_base is not None:
            base = forecast_base

    for block in later_blocks:
        members = by_block[block.pk]
        applicable = [t for t in members if not t.is_not_applicable]
        # A block with nothing applicable on this site (all N/A, or none of its tasks
        # exist here) takes zero days: nothing to do, so no time passes for it.
        half_days = _half_days(getattr(block, band_field)) if applicable else 0
        before, elapsed = elapsed, elapsed + half_days
        start, due = _block_dates(base, before, elapsed, half_days, applicable, skip_sundays)

        # "Complete" is the late-finish rule's test (below); the first-dating rule and
        # the forecast both apply only to a block that is NOT complete, so no block is
        # ever moved by two rules.
        done = [t for t in members if t.status == Task.DONE]
        complete = bool(done) and all(t.status == Task.DONE or t.is_not_applicable
                                      for t in members)
        first_dated = None
        moved = False
        if not complete and due is not None and due < today:
            system_starts = [t.schedule_start for t in members
                             if t.due_date_source == SYSTEM and t.schedule_start]
            if not any(t.due_date_source == SYSTEM for t in members):
                # FIRST DATING (SCHED-2b amendment). A system date is never written in
                # the past for the first time: an open block that is already past its
                # plan and has never been given a system date starts on the next working
                # day after today, C = 0, and the blocks after it chain from it. The
                # check is "no system date on any of its tasks", so it fires once; from
                # the next run on the block keeps those dates (the branch below).
                first_dated = next_working_day(today, skip_sundays)
                base, before, elapsed, moved = first_dated, 0, half_days, True
            elif system_starts and max(system_starts) > start:
                # KEEP ITS SYSTEM DATES ("a block whose tasks already carry system dates
                # keeps them"). Without this, the run after a first dating would put the
                # block back on its old, past plan and turn it red the next morning. The
                # chain re-anchors at the block's stored start, which is only ever later
                # than the plan here (max, so a plan is never pulled earlier); when the
                # stored dates ARE the plan nothing changes. If those dates have passed
                # too, the forecast below treats the block as late, as before.
                base, before, elapsed, moved = max(system_starts), 0, half_days, True
            if moved:
                start, due = _block_dates(base, before, elapsed, half_days, applicable,
                                          skip_sundays)
        plan = BlockPlan(block=block, start=start, due=due, half_days=half_days)
        for t in members:
            plan.tasks.append(TaskPlan(task=t, action=_task_action(t, start, due),
                                       start=start, due=due))
        result.blocks.append(plan)
        if first_dated is not None:
            plan.first_dated = first_dated
            result.forecast, result.forecast_start = plan, first_dated

        # LATE FINISH (spec Layer 4). A finished block whose last Done is after its
        # planned due date pushes every later block: day 1 becomes the next working day
        # after that Done, and the count restarts. An early finish moves nothing.
        if complete and due is not None:
            done_dates = [_local_date(t.completed_at) for t in done if t.completed_at]
            last_done = max(done_dates) if done_dates else None
            if last_done is not None and last_done > due:
                plan.re_anchored_after = last_done
                base = next_working_day(last_done, skip_sundays)
                elapsed = 0
        # FORECAST (SCHED-2b). An OPEN block (not `complete`, the late-finish rule's own
        # test, so the two rules never both apply) whose planned due date has passed is
        # forecast to finish today: every later block re-anchors to the next working day
        # after today, C = 0. "Due" is the block's due date from this walk, as in the
        # late-finish rule: the plan, or the system dates it keeps (above). A manual date
        # in the block never changes whether it is late. The block's own dates stay (its
        # red is the delay signal), and later manual dates are still never written. At
        # most one block per site re-anchors: every due date after it is after today.
        elif due is not None and due < today:
            forecast_base = _forecast_re_anchor(
                result, plan, working_day(base, elapsed // 2 + 1, skip_sundays),
                today, skip_sundays)
            if forecast_base is not None:
                base, elapsed = forecast_base, 0

    return _with_overdue(result, tasks, today)


def _block_dates(base, before, elapsed, half_days, applicable, skip_sundays):
    """(start, due) of a block that takes the running total C from `before` to
    `elapsed` half days, counted from day 1 = `base`."""
    if not applicable:
        return None, None
    if half_days == 0:
        # A zero-length block that still has work (a 0 in the table) is planned on
        # the day the previous block was due, never on a day before day 1.
        day = working_day(base, max((before + 1) // 2, 1), skip_sundays)
        return day, day
    # start = day floor(C_prev / 2) + 1, due = day ceil(C / 2): a block that begins
    # half way through a day starts on that day, and two half-day blocks can share a
    # due date (spec Layer 4).
    return (working_day(base, before // 2 + 1, skip_sundays),
            working_day(base, (elapsed + 1) // 2, skip_sundays))


def _forecast_re_anchor(result, plan, planned_next_start, today, skip_sundays):
    """Mark `plan` as the open, late block whose later blocks are re-anchored, and
    return the new day 1 for them: the next working day after today.

    Returns None (and marks nothing) when that day would not be later than the next
    block's planned start, because a plan is never pulled earlier (spec Late finish).
    Today it cannot happen: the block was due before today, so the next working day
    after today is after any start the plan gives the next block. The check keeps that
    true if the day arithmetic ever changes.
    """
    forecast_start = next_working_day(today, skip_sundays)
    if forecast_start <= planned_next_start:
        return None
    plan.forecast_after = today
    result.forecast, result.forecast_start = plan, forecast_start
    return forecast_start


def _anchor_block_action(task, anchor_task, civil_start):
    """Block 1: only the Civil task's planned start is ever written, and only while it
    is open. schedule_start has no other writer (it is read-only in admin and written
    only here), so a non-empty start is always a system-set one and may be replaced."""
    if task.is_not_applicable:
        return SKIP_NA
    if task.status == Task.DONE:
        return SKIP_DONE
    if task is not anchor_task:
        return SKIP_ANCHOR_BLOCK
    if task.schedule_start == civil_start:
        return ACTION_UNCHANGED
    return ACTION_WRITE_START


def _task_action(task, start, due):
    """What the scheduler does to one task of blocks 2+. Order matters: N/A and Done are
    reported as such even when their date is also manual."""
    if task.is_not_applicable:
        return SKIP_NA
    if task.status == Task.DONE:
        return SKIP_DONE
    if _owned_by_a_person(task):
        return SKIP_MANUAL
    if (task.due_date == due and task.schedule_start == start
            and task.due_date_source == SYSTEM):
        return ACTION_UNCHANGED
    return ACTION_WRITE


def _with_overdue(result, tasks, today):
    """Fill the overdue counts: how many of the site's tasks would be overdue today once
    this run is applied, split into those already overdue before it and those it makes
    overdue.

    THE SHARED RULE, NOT A COPY (spec Layer 5): task_health.is_overdue() asked of each
    task as it stands and as it will stand, plus is_human_owned(), the same mirror
    exclusion the dashboards apply (human_owned_tasks_q). A planned start never makes a
    task overdue, so only due-date writes and clears change the answer.
    """
    new_due = {tp.task.pk: tp.due for tp in result.writes}
    new_due.update({tp.task.pk: None for tp in result.clears if tp.action == ACTION_CLEAR})
    after = already = 0
    for t in tasks:
        if not is_human_owned(t):
            continue
        later = SimpleNamespace(due_date=new_due.get(t.pk, t.due_date), status=t.status,
                                is_not_applicable=t.is_not_applicable)
        if is_overdue(later, today):
            after += 1
            if is_overdue(t, today):
                already += 1
    result.overdue_after, result.overdue_already, result.overdue_new = after, already, after - already
    return result


def schedule_site(project, actor=None, apply=False, skip_sundays=None):
    """Plan, and with apply=True write, one site's system due dates. Returns a
    ScheduleResult. See the module docstring and spec Layer 4 for the rules.

    `actor` is the UserProfile behind the trigger, or None (the command, a profile-less
    superuser). `skip_sundays` overrides the stored working-day switch, so the Admin
    screen can preview a flip before saving it.

    The dry run (apply=False) reads only. The apply run plans and writes inside ONE
    transaction holding the site row, so the plan is made from the state it writes over.
    """
    if skip_sundays is None:
        skip_sundays = SystemSettings.get().schedule_skip_sundays
    today = timezone.localdate()

    if not apply:
        return _plan(project, skip_sundays, today)

    with transaction.atomic():
        # select_for_update on the SITE: two triggers for one site (an approval and a
        # capacity edit committing together) run one after the other, the second
        # planning from what the first wrote, instead of both writing from one stale
        # read (spec Layer 5, concurrency).
        project = Project.objects.select_for_update().get(pk=project.pk)
        result = _plan(project, skip_sundays, today)
        result.applied = True
        forecast_pks = {tp.task.pk for tp in result.forecast_writes}
        for tp in result.writes:
            set_task_due_date(tp.task, tp.due, SYSTEM, actor, log=False)
            tp.task.schedule_start = tp.start
            saved = _save_dates(tp.task)
            result.written += saved
            if tp.task.pk in forecast_pks:
                result.forecast_moved += saved
        for tp in result.start_writes:
            tp.task.schedule_start = tp.start
            result.started += _save_start(tp.task)
        for tp in result.clears:
            tp.task.schedule_start = None
            if tp.action == ACTION_CLEAR:
                set_task_due_date(tp.task, None, SYSTEM, actor, log=False)
                result.cleared += _save_dates(tp.task)
            else:
                result.cleared += _save_start(tp.task)
        # One row per kind of change (SCHED-2b ruling 2). When every change in the run
        # was a forecast move (the daily run's normal case) only the forecast row is
        # written; when anything else changed too, the usual row (with all the counts)
        # is written as well. The forecast row has no actor, even in a trigger run
        # (ruling 1): see FORECAST_ACTION_CODE.
        changed = result.written + result.started + result.cleared
        if changed > result.forecast_moved:
            log_activity(project, actor, _log_text(result), entity_type='Project',
                         entity_id=project.pk, action_code=SCHEDULED_ACTION_CODE)
        if result.forecast_moved:
            log_activity(project, None, _forecast_log_text(result), entity_type='Project',
                         entity_id=project.pk, action_code=FORECAST_ACTION_CODE)
    return result


def _save_dates(task):
    """Persist the three schedule columns of one task of blocks 2+, and nothing else.
    Returns 1 when the row was written, 0 when it no longer qualified.

    filter().update() with the scheduler's own rules repeated as the WHERE clause, to
    close a race: the site row is locked, but a task status change or a PM's date edit
    does not take that lock, so between the plan's read and this write a task can become
    Done, N/A or manual. The WHERE makes this write a no-op instead of putting a system
    date over a finished task or a person's date. Only the three date columns are in the
    UPDATE, so a concurrent status write is never overwritten either.
    """
    return (Task.objects.filter(pk=task.pk, is_not_applicable=False)
            .exclude(status=Task.DONE)
            .filter(Q(due_date_source=SYSTEM)
                    | Q(due_date_source__isnull=True, due_date__isnull=True))
            .update(due_date=task.due_date, due_date_source=task.due_date_source,
                    schedule_start=task.schedule_start))


def _save_start(task):
    """Persist only the Civil task's planned start. Returns 1 when written, 0 when not.

    filter().update() for the race _save_dates() describes, with one more term: the due
    date must still be the one the start was counted back from. If the PM changed the
    Civil date after the plan was read, this write is a no-op, and that date change's
    own trigger reschedules the site from the new date.
    """
    return (Task.objects.filter(pk=task.pk, is_not_applicable=False, due_date=task.due_date)
            .exclude(status=Task.DONE)
            .update(schedule_start=task.schedule_start))


def _log_text(result):
    """The one ActivityLog line per site per run (spec Layer 2)."""
    counts = (f'{result.written} written, {result.started} start set, '
              f'{result.cleared} cleared')
    if result.reason is None:
        return (f'Auto-scheduled due dates: anchor Civil due {result.anchor.isoformat()}, '
                f'band {BAND_LABELS[result.band]} kWp; {counts}')
    return f'Auto-schedule cleared system due dates ({result.reason}); {counts}'


def _forecast_log_text(result):
    """The forecast row (SCHED-2b): which block is open past which due date, and how
    many later dates moved to start on which day; or, for a block given its first
    system dates after its plan had passed, the day it was started on. At most one of
    the two happens on a site, because every date after either is after today."""
    late = result.forecast
    if late.first_dated is not None:
        return (f'Forecast: {late.block.name} not yet dated and already past plan; '
                f'started {late.first_dated.isoformat()}')
    return (f'Forecast: {late.block.name} open past due {late.due.isoformat()}; '
            f'{result.forecast_moved} later dates moved to start '
            f'{result.forecast_start.isoformat()}')


def schedule_site_safely(project, actor=None):
    """schedule_site(apply=True) for the triggers: NEVER raises.

    The triggers run it after commit (transaction.on_commit), so the status change,
    approval, date or capacity edit is already saved; a scheduler failure must not turn
    that saved action into an error page for the person who did it (spec Layer 5). Any
    exception is logged with the site code and swallowed; the next trigger or a
    `schedule_sites` run reschedules the site.
    """
    try:
        return schedule_site(project, actor=actor, apply=True)
    except Exception:
        logger.exception('Auto-schedule failed for site %s',
                         getattr(project, 'project_id', project))
        return None


def task_is_scheduled(task):
    """True when the task belongs to an active schedule block, so a change to it can
    move the site's schedule. One indexed query; the triggers ask it before queueing a
    reschedule, so a change to an unscheduled task costs nothing more.

    NEVER RAISES. The triggers ask this synchronously, inside the caller's request and
    (in task_approve) inside its transaction, so an error here would undo the very
    change it was asked about. A failure answers False and is logged: the site misses
    one reschedule, which the next trigger or `schedule_sites` makes up.
    """
    try:
        if not _in_scope(task):
            return False
        return ScheduleBlockTask.objects.filter(template_task_id=task.template_task_id,
                                                block__is_active=True).exists()
    except Exception:
        logger.exception('Auto-schedule trigger check failed for task %s',
                         getattr(task, 'pk', task))
        return False


def task_is_schedule_anchor(task):
    """True for the site's unlabelled Civil task while its block is active: a change to
    its due date (set, changed or cleared) moves the whole site's schedule. Never
    raises, for the reason task_is_scheduled() gives."""
    try:
        if not _is_anchor_task(task):
            return False
    except Exception:
        logger.exception('Auto-schedule trigger check failed for task %s',
                         getattr(task, 'pk', task))
        return False
    return task_is_scheduled(task)


def status_change_reschedules(task, old_status, new_status):
    """True when this status move can change the site's schedule (spec Layer 4,
    Triggers; SCHED-2 ruling b): a block task reaching Done (a block can finish, late or
    not) or leaving Done (reopened: its block is no longer finished). Starting a task
    moves nothing: the anchor is the Civil due date, not its start.

    Never raises, for the reason task_is_scheduled() gives.
    """
    if old_status == new_status or Task.DONE not in (old_status, new_status):
        return False
    return task_is_scheduled(task)
