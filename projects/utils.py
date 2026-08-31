import logging
import re

from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Assignment chokepoint
#
# Task.assigned_to is written in exactly two places: assign_task_to() for a
# single task, assign_tasks_to() for a set. Every other module calls one of
# these. Same pattern as send_notification(), log_activity() and
# user_can_manage_project() — no signals, no save() overrides.
# ---------------------------------------------------------------------------

# Both assignment templates count towards the same per-recipient/per-project
# budget: a person who has had one of each must not receive a third.
ASSIGN_NOTIFY_TEMPLATES = ('assign_task', 'assign_tasks_bulk')

# 1st assignment in the window sends assign_task, 2nd sends assign_tasks_bulk,
# 3rd and beyond send nothing until the window expires.
ASSIGN_COOLDOWN_WINDOW       = timedelta(hours=1)
ASSIGN_COOLDOWN_MAX_MESSAGES = 2

# Runaway backstop, well above the cooldown's own limit of 2 sends (= 6 rows at
# three channels each). Only a fault in the cooldown lookup can reach it.
ASSIGN_CIRCUIT_BREAKER_ROWS = 20

# Fallback origin for absolute links when no request is available (management
# commands, shell). Mirrors the hardcoded host already used in the email bodies.
SITE_BASE_URL = 'https://horizon-solar-pms-production.up.railway.app'


def _abs_url(path, request):
    """Absolute URL for `path`, preferring the live request's own origin."""
    if request is not None:
        return request.build_absolute_uri(path)
    return f'{SITE_BASE_URL}{path}'


def _assign_notification_state(user, project):
    """
    Return (sends_in_window, breaker_tripped) for one recipient on one project.

    Counted on the in_app channel only. send_notification() writes one
    NotificationLog row PER CHANNEL, so counting raw rows would read a single
    three-channel send as three. in_app is the reliable one-row-per-send marker:
    _send_in_app() always runs and always logs, with no master switch or
    per-user preference in front of it, so this counts notification *events* and
    does not drift when somebody turns WhatsApp or email off. That is also what
    keeps the cooldown from bypassing the preference layer — the decision to
    throttle is made on events, and delivery stays send_notification()'s call.

    The backstop deliberately counts every row on a different query shape, so a
    bug in the channel filter above cannot disable both at once.
    """
    from .models import NotificationLog

    window = NotificationLog.objects.filter(
        recipient=user,
        related_project=project,
        template_name__in=ASSIGN_NOTIFY_TEMPLATES,
        created_at__gte=timezone.now() - ASSIGN_COOLDOWN_WINDOW,
    )
    return window.filter(channel='in_app').count(), window.count() >= ASSIGN_CIRCUIT_BREAKER_ROWS


def _notify_assignment(task, user, actor=None, request=None):
    """
    Apply the per-recipient, per-project, 1-hour cooldown and send at most one
    message. Scope is per project on purpose: somebody assigned work on two
    projects inside the hour must hear about both.
    """
    from .notifications import send_notification

    project        = task.phase.project
    recipient_name = user.user.get_full_name() or user.user.username

    sends, breaker_tripped = _assign_notification_state(user, project)

    if breaker_tripped:
        logger.error(
            'assign_task_to: circuit breaker tripped — %s assignment notification rows '
            'for %s on %s inside the window; sending nothing.',
            ASSIGN_CIRCUIT_BREAKER_ROWS, recipient_name, project.project_id,
        )
        return

    if sends >= ASSIGN_COOLDOWN_MAX_MESSAGES:
        return

    if sends == 0:
        # First of the window — the existing message, unchanged.
        task_url     = f'/projects/{project.project_id}/tasks/{task.pk}/'
        task_url_abs = _abs_url(task_url, request)
        message = (
            f'Hi {recipient_name},\n\n'
            f'The task "{task.task_name}" on project {project.customer_name} has been assigned to you.\n\n'
            f'Please login to review details and update progress.'
        )
        send_notification(
            recipient=user,
            message=f'{message}\n\nView in Horizon Solar PMS:\n{SITE_BASE_URL}{task_url}',
            channels=['in_app', 'whatsapp', 'email'],
            link=task_url,
            subject=f'Task Assigned: {task.task_name} — {project.customer_name}',
            template='assign_task',
            template_params=[project.customer_name, recipient_name, task.task_name,
                             project.customer_name, task_url_abs],
            related_project=project,
            actor=actor,
        )
        return

    # Second of the window — one summary instead of a second per-task message.
    # It links to the project's task list rather than any single task: the count
    # goes stale the moment a third assignment lands, the list does not.
    task_count   = sends + 1
    list_url     = f'/projects/{project.project_id}/overview/'
    list_url_abs = _abs_url(list_url, request)
    message = (
        f'Hi {recipient_name},\n\n'
        f'{task_count} tasks on project {project.customer_name} have been assigned to you.\n\n'
        f'Please login to review details and update progress.'
    )
    send_notification(
        recipient=user,
        message=f'{message}\n\nView in Horizon Solar PMS:\n{SITE_BASE_URL}{list_url}',
        channels=['in_app', 'whatsapp', 'email'],
        link=list_url,
        subject=f'{task_count} Tasks Assigned — {project.customer_name}',
        template='assign_tasks_bulk',
        template_params=[project.customer_name, recipient_name, task_count,
                         project.customer_name, list_url_abs],
        related_project=project,
        actor=actor,
    )


def assign_task_to(task, user, notify=False, actor=None, request=None):
    """
    Write Task.assigned_to for ONE task, and make the notification decision.

    `notify` defaults to False deliberately. Seven of the nine write sites are
    silent today — activation, the assign_design bulk action, all three unassign
    paths and the Django admin — and must stay silent. Only the two interactive
    assignment views pass notify=True. A helper that notified by default would
    turn this refactor into an unrequested behaviour change discovered in
    production rather than in a test.

    Unassignment (user=None) never notifies: telling somebody work was taken off
    them is a separate decision nobody has asked for.

    Returns True if the field changed, False if this was a no-op. Assigning
    somebody to a task they already hold writes nothing and sends nothing, which
    is what makes a double-submitted form harmless.

    ActivityLog is deliberately not written here — callers own it. The
    interactive views log one line per task; the bulk paths log a single summary
    line. Logging inside this helper would give activation 20 extra ActivityLog
    rows and skew the EOD digest, which filters on action_code.
    """
    from .models import Task

    if task.assigned_to_id == (user.pk if user is not None else None):
        return False

    Task.objects.filter(pk=task.pk).update(assigned_to=user)
    task.assigned_to = user  # keep the caller's in-memory copy honest

    if notify and user is not None:
        _notify_assignment(task, user, actor=actor, request=request)

    return True


def assign_tasks_to(queryset, user):
    """
    Write Task.assigned_to for a SET of tasks. Returns the number of rows updated.

    Always silent, and there is no notify parameter by design. The three bulk
    write sites — activation's PM and Finance pre-assignment, and the
    assign_design action — send nothing today, and making that structural means
    no later edit can turn them noisy by flipping a default.

    Kept set-based on purpose: looping assign_task_to() over these would turn
    activation's 2 UPDATEs into 21 and assign_design's 1 into 7, inside blocks
    that are already atomic, for no behavioural gain.
    """
    return queryset.update(assigned_to=user)


# ---------------------------------------------------------------------------
# Metric chokepoint — prompt 1.3b (rule R-20)
#
# WHAT COUNTS AS SOMEBODY'S WORK. Every task METRIC in this codebase — overdue,
# pending, workload, progress, digest gating — routes through the two callables
# below, and they are the only place the rule is written down.
#
# A mirror task's status is DERIVED from another object (a DesignAssignment, a
# delivery quantity, a COD record) and no human may write it. Counting one as a
# person's work attributes another team's queue to the wrong person: an OPEX
# site's two PM mirrors are the PM's "pending approvals" without the PM ever
# being able to act on either, and an EOD digest gated on open work would email
# somebody about tasks they cannot touch.
#
# WHAT THIS IS NOT: it is not a visibility rule. A site dashboard must still
# SHOW its mirrors — displaying the derived state is the entire reason mirrors
# exist. This removes them from metrics, never from lists. The one exception is
# a list that exists to itemise a metric (`tasks_drill_down`, and the PM card's
# five-row blocked/overdue evidence lists), which must reconcile with the number
# it hangs off or the page contradicts itself.
#
# WHY A `Q` AND NOT A FILTERED QUERYSET: three consumers have no queryset to
# filter — two conditional aggregates (`Count(..., filter=...)` on the CEO
# dashboard and the SE dashboard, where a base filter would change what the
# other annotations on the same join count) and one prefetched Python list
# (`project_overview`'s per-phase progress). A `Q` serves all three plus the
# plain `.filter()` sites; `is_human_owned()` is the row-at-a-time form.
#
# WHY NOT A CUSTOM MANAGER: there are no custom managers anywhere in this
# codebase, deliberately — soft delete relies on `Task.objects` meaning
# "everything", so that every queryset has to state its own predicate and no
# reader has to guess whether one is already applied. See the module docstring
# of reports.py. Adding a manager for the second predicate while the first
# still has none would make both harder to reason about.
#
# The `prefix` convention is `reports._active_project_filter()`'s.
# ---------------------------------------------------------------------------

def human_owned_tasks_q(prefix=''):
    """Q() matching only the tasks a human is accountable for.

    `prefix` is the relation path to Task — '' when querying Task itself,
    'phases__tasks__' when querying Project, 'tasks__' when querying
    ProjectPhase.

    Positive, never a negation, and that is load-bearing on the CEO dashboard:
    a negated Q across the multi-valued `phases__tasks` relation takes Django's
    exclude() subquery path instead of a plain SQL FILTER, which would change
    the join fan-out the two card counts deliberately share.
    """
    from django.db.models import Q

    return Q(**{f'{prefix}is_mirror': False})


def is_human_owned(task):
    """Row-at-a-time form of human_owned_tasks_q(), for prefetched lists.

    Same rule, applied in Python where no queryset exists to filter.
    """
    return not task.is_mirror


# ---------------------------------------------------------------------------
# CURRENT PHASE — one implementation, and it excludes mirrors (R-21)
#
# Prompt B21, 31 Aug 2026. This replaces FOUR independent copies of "first
# phase holding a not-Done task": `Project.get_current_phase()`, `dashboard_pm`,
# `dashboard_site_engineer`, and an inline Python loop in `dashboard_bd`. All
# four now call this and nothing else computes it.
#
# WHY MIRRORS ARE OUT, and it is not an arbitrary extension of R-20's metric
# rule to a non-metric: a mirror's status is derived from another object and no
# human may write it (R-18, R-20). A phase is "current" as an answer to "what
# is this site waiting on" — so a phase is not current because a mirror in it
# is open, since nobody can act on a mirror to close it. The OPEX template made
# this urgent rather than tidy: its Phase 1 is `Design`, whose ONLY task is a
# mirror, and COD / HOTO / As-Built have no source object in existence to ever
# complete them. Without the exclusion every OPEX site reports "Design" as its
# current phase permanently, on all four screens, with all nine installation
# tasks done.
#
# WHAT THE FOUR COPIES DISAGREED ABOUT, and how it was settled (recorded here
# because a reader will otherwise assume they were identical):
#   • return type — PM returned a ProjectPhase, the other three a name string.
#     This returns the OBJECT; `Project.get_current_phase()` takes `.phase_name`
#     off it, because two templates print that method's result directly.
#   • the empty case — models.py and dashboard_bd returned None, PM and SE
#     returned the LAST phase. A fully-completed project therefore read
#     "Finance Closure" on the PM dashboard and "—" on the Admin project list,
#     a live defect before mirrors were involved. SETTLED BY DECISION on
#     31 Aug: the last phase that HOLDS a human-owned task. That equals the
#     old PM/SE answer for both templates shipping today, so the two most-used
#     screens are unchanged; it differs only for a future template ending in an
#     all-mirror phase — the shape OPEX Phase 1 has at the front.
# Ordering and the not-Done predicate were already identical across all four.
#
# A PYTHON LOOP, NOT A QUERYSET, and that is the whole performance argument.
# `phases.filter(...)` ignores the prefetch cache, so the queryset form cost
# 1–2 queries PER PROJECT at every call site — including the two that already
# prefetch (`admin_project_list`, 50 rows a page, and `dashboard_bd`). This
# form costs ZERO extra queries wherever phases and tasks are prefetched, which
# is now all four call sites: prompt B21 added the same `Prefetch` clause to
# `dashboard_pm` and `dashboard_site_engineer` that the other two already had.
#
# Pinned by projects/tests_current_phase.py.
# ---------------------------------------------------------------------------

def current_phase(project):
    """The ProjectPhase a site is currently working, or None.

    "Current" = the first phase, in `phase_order`, that still holds a
    HUMAN-OWNED task which is not Done. Mirrors are excluded via
    `is_human_owned()` — see R-20 and the block comment above: a mirror is
    nobody's work, so an open one does not make its phase current.

    When every human-owned task is Done, returns the LAST phase holding a
    human-owned task (decided 31 Aug 2026). Returns None only when the project
    has no phases, or no phase holds a human-owned task at all.

    Reads `project.phases.all()` and `phase.tasks.all()`, so it is free on a
    caller that prefetched both and expensive on one that did not. Every call
    site prefetches; a new one must too.
    """
    from .models import Task

    last_human_phase = None
    for phase in project.phases.all():
        for task in phase.tasks.all():
            if not is_human_owned(task):
                continue
            last_human_phase = phase
            if task.status != Task.DONE:
                return phase
    return last_human_phase


# ---------------------------------------------------------------------------
# State-ledger chokepoint — prompt 0.3 (rules R-2, R-3, R-4, R-9, R-14)
#
# StatusTransition rows are written HERE AND NOWHERE ELSE. Same pattern as
# assign_task_to() above and send_notification(): one function owns one table's
# writes, no signals, no save() overrides.
# ---------------------------------------------------------------------------

# Model class -> subject_type. subject_type is DERIVED FROM THE MODEL through
# this one registry and is never a string the caller passes: a caller-supplied
# string is how these vocabularies drift, and a typo would be indistinguishable
# from a subject type that genuinely has no rows.
#
# Built lazily and cached — utils cannot import models at module level.
_SUBJECT_TYPE_REGISTRY = None

# How to reach the owning Project from each subject. Task is the odd one: it has
# no project FK and must go through its phase (see feedback_task_project_path).
_SUBJECT_PROJECT_RESOLVERS = {
    'Project':          lambda s: s,
    'Task':             lambda s: s.phase.project,
    'BOQ':              lambda s: s.project,
    'DeliveryChallan':  lambda s: s.project,
    'Issue':            lambda s: s.project,
    'PaymentMilestone': lambda s: s.project,
}


def _subject_type_registry():
    """Model class -> subject_type constant. Cached after the first call."""
    global _SUBJECT_TYPE_REGISTRY
    if _SUBJECT_TYPE_REGISTRY is None:
        from .models import (
            Project, Task, BOQ, DeliveryChallan, Issue, PaymentMilestone,
            SUBJECT_PROJECT, SUBJECT_TASK, SUBJECT_BOQ,
            SUBJECT_DELIVERY_CHALLAN, SUBJECT_ISSUE, SUBJECT_PAYMENT_MILESTONE,
        )
        _SUBJECT_TYPE_REGISTRY = {
            Project:          SUBJECT_PROJECT,
            Task:             SUBJECT_TASK,
            BOQ:              SUBJECT_BOQ,
            DeliveryChallan:  SUBJECT_DELIVERY_CHALLAN,
            Issue:            SUBJECT_ISSUE,
            PaymentMilestone: SUBJECT_PAYMENT_MILESTONE,
        }
    return _SUBJECT_TYPE_REGISTRY


def record_transition(subject, to_status, from_status='', actor=None,
                      reason_code='', remark='', project=None,
                      client_uuid=None, occurred_at=None):
    """
    Write one StatusTransition row. MUST be called inside the same
    transaction.atomic() block as the status change it records.

    A transition row without its status change, or a status change without its
    row, is worse than neither — the first is a history of something that never
    happened, the second is a gap indistinguishable from "we never instrumented
    this path". Callers own the atomic block; this function does not open one,
    because opening its own would defeat the point.

    THIS FUNCTION RAISES. IT NEVER SWALLOWS. That is the whole difference from
    log_activity(), which catches bare `Exception` and logs it, so a failure
    there loses one line of a feed and nothing more. Here a swallowed failure
    would leave a status change with no record of who made it, silently, and the
    caller's atomic block would commit the change anyway. If someone later
    "harmonises" the two helpers by wrapping this body in try/except, they will
    have reintroduced exactly that defect. R-3: the feed and the ledger are
    different things and are allowed to fail differently.

    Args:
        subject:     the model INSTANCE whose status changed. Its class decides
                     subject_type via _subject_type_registry() — never a string.
        to_status:   required, the new status.
        from_status: the previous status; '' for a creation transition.
        actor:       UserProfile, or None where no human acted (the Zoho
                     webhook). actor.role is COPIED into actor_role_code, never
                     joined, so a later role change cannot rewrite history.
        reason_code: one of the REASON_* constants in models.
        remark:      free text. Mandatory for subject types listed in
                     REMARK_REQUIRED_SUBJECT_TYPES (R-9) — empty today.
        project:     override for the derived project. Pass it only where the
                     resolver cannot work (e.g. a subject not yet saved).
        client_uuid: R-14 idempotency key. A repeat is IGNORED — same row
                     returned, nothing duplicated, no exception.
        occurred_at: override for "now", for a replayed offline submission.

    Returns the StatusTransition row (existing one on an idempotent repeat).
    """
    from django.db import IntegrityError
    from .models import (
        StatusTransition, ACTOR_ROLE_SYSTEM, REMARK_REQUIRED_SUBJECT_TYPES,
    )

    subject_type = _subject_type_registry().get(type(subject))
    if subject_type is None:
        raise ValueError(
            f"record_transition(): {type(subject).__name__} is not an instrumented "
            f"subject type. Add it to the registry and to SUBJECT_TYPE_CHOICES, and "
            f"record it in docs/execution-model.md §13 — a subject type that writes "
            f"rows without being documented makes the ledger's gaps unreadable."
        )

    if not to_status:
        raise ValueError('record_transition(): to_status is required.')

    # R-9, enforced here rather than by the database — see the note on
    # StatusTransition.remark for why it cannot be a NOT NULL column yet.
    if subject_type in REMARK_REQUIRED_SUBJECT_TYPES and not (remark or '').strip():
        raise ValueError(
            f"record_transition(): a remark is mandatory for '{subject_type}' "
            f"transitions (R-9)."
        )

    if client_uuid:
        # Fast path: the replay we have already seen. The savepoint below is what
        # actually makes this safe under concurrency; this just avoids the
        # round-trip in the common case.
        existing = StatusTransition.objects.filter(client_uuid=client_uuid).first()
        if existing is not None:
            return existing

    if project is None:
        resolver = _SUBJECT_PROJECT_RESOLVERS.get(type(subject).__name__)
        if resolver is not None:
            project = resolver(subject)

    # Copied, not joined. A departed or re-roled user must not be able to change
    # what this row says they were at the time.
    if actor is not None:
        actor_role_code = actor.role or ACTOR_ROLE_SYSTEM
    else:
        actor_role_code = ACTOR_ROLE_SYSTEM

    row = StatusTransition(
        subject_type=subject_type,
        subject_id=subject.pk,
        project=project,
        from_status=from_status or '',
        to_status=to_status,
        actor=actor,
        actor_role_code=actor_role_code,
        reason_code=reason_code or '',
        remark=remark or '',
        client_uuid=client_uuid or None,
        occurred_at=occurred_at or timezone.now(),
    )

    if not client_uuid:
        row.save()
        return row

    # Idempotent replay, race included. The nested atomic() is a SAVEPOINT: without
    # it an IntegrityError here would poison the CALLER's transaction, so recovering
    # from a duplicate key would abort the status change we were called to record.
    try:
        with transaction.atomic():
            row.save()
        return row
    except IntegrityError:
        existing = StatusTransition.objects.filter(client_uuid=client_uuid).first()
        if existing is not None:
            return existing
        raise  # some other integrity problem — never swallow it


def generate_project_id(project_type):
    """
    Generate a unique project ID of the form HRP-{PREFIX}-{YEAR}-{NNN}.
    Must be called inside transaction.atomic() — uses select_for_update()
    to prevent duplicate IDs under concurrent saves.
    """
    # import inside function to avoid circular import at module level
    from .models import Project

    PREFIX_MAP = {
        'Residential': 'RES',
        'OPEX':        'OPX',
        'CAPEX':       'CAP',
    }
    prefix = PREFIX_MAP[project_type]
    year = timezone.now().year
    id_prefix = f"HRP-{prefix}-{year}-"

    # Derive the next number from the highest suffix ever issued, NOT from a row
    # count — a count reuses numbers once rows are deleted and collides with the
    # surviving project's ID. Uses the unfiltered default manager on purpose so
    # soft-deleted projects (is_deleted=True) still reserve their number.
    locked_ids = (
        Project.objects
        .select_for_update()
        .filter(project_id__startswith=id_prefix)
        .values_list('project_id', flat=True)
    )

    highest = 0
    for existing_id in locked_ids:
        suffix = existing_id[len(id_prefix):]
        # Ignore anything that isn't a plain number (hand-edited or legacy IDs)
        if suffix.isdigit():
            highest = max(highest, int(suffix))

    return f"HRP-{prefix}-{year}-{highest + 1:03d}"


def add_calendar_days(start_date, days):
    """
    Advance start_date by N calendar days. days=0 returns start_date unchanged.
    Weekends are NOT skipped — every calendar day counts.

    RENAMED from `add_workdays` in Part 8. The old name claimed to do working-day
    arithmetic and did not, which was harmless while nothing else in the system had
    a real definition of a working day. Part 8 adds one (`is_working_day` below), so
    two functions would have sat in this module both sounding like working-day maths
    while meaning different things. The rename is identifier-only: no call site's
    behaviour changes and no stored date moves. The Residential task cascade and the
    Gantt engine still get plain calendar arithmetic, which is what they always got.
    """
    return start_date + timedelta(days=days)


# ---------------------------------------------------------------------------
# WORKING DAYS — the single definition for the whole system (Part 8)
# ---------------------------------------------------------------------------
# Horizon Renewable Power's working week:
#   * every Sunday is off
#   * the 2nd and 4th Saturday of each month are off
#   * the 1st, 3rd and 5th Saturdays are WORKING days
#
# "2nd Saturday" means the second Saturday to fall in that calendar month, which is
# what the company means by it — not "14 days after the first". Because the Nth-ness
# is counted within the month, it is derived from the day-of-month alone: days 1-7
# hold the 1st Saturday, 8-14 the 2nd, and so on. That is why the nth calculation
# below is `(day - 1) // 7 + 1` and needs no calendar lookup.
#
# NO PUBLIC HOLIDAY CALENDAR. Deliberately out of scope for Part 8 and recorded as a
# known gap in DESIGN_MODULE_DEFERRED.md — a due date that lands on Diwali will not
# roll. Adding one later means changing `is_working_day` only; every caller rolls
# forward through whatever this function reports, so no caller needs to change.

SATURDAY = 5
SUNDAY   = 6

#: Saturdays that are NOT working days, counted within the calendar month.
NON_WORKING_SATURDAYS = (2, 4)


def is_working_day(d):
    """
    True if `d` is a working day under the company calendar described above.

    Public holidays are NOT considered — see DESIGN_MODULE_DEFERRED.md.
    """
    weekday = d.weekday()
    if weekday == SUNDAY:
        return False
    if weekday == SATURDAY:
        nth_saturday_of_month = (d.day - 1) // 7 + 1
        return nth_saturday_of_month not in NON_WORKING_SATURDAYS
    return True


def next_working_day(d):
    """
    Return `d` if it is a working day, otherwise the first working day after it.

    Rolls FORWARD only. A due date is a commitment to the site, so a date that lands
    on a day nobody works becomes the next day somebody does — never an earlier one,
    which would silently shorten the designer's window.

    The loop terminates because at most two consecutive non-working days can occur
    (a 2nd/4th Saturday followed by its Sunday); the bound is not relied upon.
    """
    while not is_working_day(d):
        d += timedelta(days=1)
    return d


#: Calendar days a designer gets between allocation and the due date, before the
#: non-working-day roll-forward is applied.
DESIGN_DUE_DATE_OFFSET_DAYS = 2


def design_due_date(allocation_date, offset_days=DESIGN_DUE_DATE_OFFSET_DAYS):
    """
    The due date for an OPEX design site allocated on `allocation_date`.

    Allocation date + 2 CALENDAR days, then rolled forward to the next working day.

    The offset is calendar days, not working days, on purpose: the commitment the
    business makes is "two days from allocation", and only the landing day is
    adjusted so it isn't a day nobody is in. Counting the offset in working days
    instead would push a Thursday allocation to the following Tuesday, which is not
    what was asked for.

    `allocation_date` must be a `date` — callers holding a datetime should pass
    `timezone.localdate(dt)` so the day is the one the office was actually in.
    """
    return next_working_day(allocation_date + timedelta(days=offset_days))


def recalculate_from_task(project, anchor_task, new_date, user=None):
    """
    Set anchor_task.due_date = new_date, then cascade-recalculate every task
    that comes after it (same phase order / task order sequence).
    Internal tasks chain sequentially; external tasks mirror the current
    internal chain position (run in parallel, not blocking the chain).
    Logs every changed due_date in DueDateChangeLog.
    Returns a (count, changed_tasks) tuple: the number of tasks whose due_date
    changed, and the list of those Task instances (with their new due_date already
    applied in memory). The task list is consumed by the HTMX due-date endpoint to
    render out-of-band row swaps for the cascade ripple — no cascade math changes.
    """
    # import inside function to avoid circular import at module level
    from .models import Task, DueDateChangeLog

    # Load all tasks in project order so we can walk forward from anchor
    tasks = list(
        Task.objects
        .filter(phase__project=project)
        .order_by('phase__phase_order', 'task_order')
    )

    anchor_idx = next((i for i, t in enumerate(tasks) if t.pk == anchor_task.pk), None)
    if anchor_idx is None:
        return 0

    changes = []

    # Apply new date to anchor
    old_anchor = tasks[anchor_idx].due_date
    tasks[anchor_idx].due_date = new_date
    if old_anchor != new_date:
        changes.append((tasks[anchor_idx], old_anchor, new_date))

    # Determine the internal chain position after the anchor.
    # If anchor is Internal, it becomes the new chain head.
    # If anchor is External, find the most recent Internal task before it.
    if tasks[anchor_idx].task_type == Task.INTERNAL:
        prev_internal_due = new_date
    else:
        # Find the most recent internal task at or before anchor
        prev_internal_due = new_date  # fallback if no prior internal exists
        for t in reversed(tasks[:anchor_idx]):
            if t.task_type == Task.INTERNAL and t.due_date:
                prev_internal_due = t.due_date
                break

    # Walk every task after the anchor and update its due date
    for task in tasks[anchor_idx + 1:]:
        old_d = task.due_date
        if task.task_type == Task.EXTERNAL:
            # External tasks run in parallel with the current internal position
            new_d = prev_internal_due
        else:
            new_d = add_calendar_days(prev_internal_due, task.duration_days)
            prev_internal_due = new_d  # advance the internal chain
        task.due_date = new_d
        if old_d != new_d:
            changes.append((task, old_d, new_d))

    # Single bulk_update for all changed tasks — avoids N individual saves
    Task.objects.bulk_update(tasks[anchor_idx:], ['due_date'])

    if user and changes:
        user_profile = user.profile
        # bulk_create the audit trail in one query
        DueDateChangeLog.objects.bulk_create([
            DueDateChangeLog(task=t, old_date=old_d, new_date=new_d, changed_by=user_profile)
            for t, old_d, new_d in changes
        ])

    return len(changes), [t for t, _old, _new in changes]


def calculate_due_dates(project, user=None):
    """
    Assign due_date to every task on project starting from project.activated_at.
    Internal tasks chain sequentially off the previous internal task's due date.
    External tasks get the same due date as the current internal chain position
    (they run in parallel, not blocking the chain).
    Called on full recalculation (project_recalculate_dates view).

    Writes ONE summary ActivityLog line for the whole recalc — never per-task, and
    no DueDateChangeLog rows (matching the cascade approach). `user` is the acting
    user; pass None for a system-triggered recalc with no clear actor.
    """
    # import inside function to avoid circular import at module level
    from .models import Task, log_activity

    tasks = (
        Task.objects
        .filter(phase__project=project)
        .order_by('phase__phase_order', 'task_order')
    )

    previous_internal_due = project.activated_at.date()

    count = 0
    for task in tasks:
        if task.task_type == Task.EXTERNAL:
            # External tasks shadow the current internal chain date
            task.due_date = previous_internal_due
        else:
            task.due_date = add_calendar_days(previous_internal_due, task.duration_days)
            previous_internal_due = task.due_date
        task.save()
        count += 1

    log_activity(
        project, user.profile if user else None,
        f"Recalculated due dates for {count} tasks",
        entity_type='Project', entity_id=project.pk,
        action_code='due_dates_recalculated',
    )


def compute_gantt_schedule(project, buffer_days=0, external_min_days=0):
    """
    Compute (start, end) per task IN-MEMORY for the Gantt, WITHOUT writing anything.

    HYBRID date source (stored-or-computed):
      - A task's END is its Task.due_date when that is set, otherwise the computed
        chain end (previous end + duration_days). So a PM's due-date edits (and the
        cascade in recalculate_from_task) move the bars, while the ~all-null live
        projects still render via the computed chain and are never blank.
      - The chain cursor advances to each internal task's actual (unbuffered) end, so
        downstream tasks chain off a stored due date when one is present.

    Bar rules:
      - Internal task: start = chain cursor, end = due_date or (cursor + duration);
        advances the cursor. A guard clamps end >= start if a manual due date inverts.
      - Internal duration-0 task: milestone (zero-width) at its due_date if set, else the
        cursor; does not advance the cursor.
      - External task: parallel/non-blocking — start = cursor, cursor NOT advanced.
        Display width = max(duration_days, external_min_days) so it never renders as a
        thin sliver on the client chart (its stored due date, which the cascade pins to
        the current internal position, is not used for width).
      - buffer_days: applied as a per-phase display OFFSET — a task in phase p is shifted
        by (p - 1) * buffer_days. Applied on top of BOTH stored and computed dates, so the
        Client view stays padded regardless of source. buffer_days=0 == the raw schedule.

    Returns a list of row dicts in (phase_order, task_order) order, or [] when
    project.activated_at is None (caller renders a "not activated" message).
    """
    from .models import Task

    if project.activated_at is None:
        return []

    tasks = (
        Task.objects
        .filter(phase__project=project)
        .select_related('phase')
        .order_by('phase__phase_order', 'task_order')
    )

    rows = []
    cursor = project.activated_at.date()   # raw (unbuffered) chain position
    buffer_accum = 0                        # accumulated per-phase buffer, applied as a display offset
    prev_phase_order = None

    for task in tasks:
        phase_order = task.phase.phase_order
        # Cross into a new phase → the client buffer for downstream tasks grows by one step.
        if prev_phase_order is not None and phase_order != prev_phase_order:
            buffer_accum += buffer_days
        prev_phase_order = phase_order

        dur = task.duration_days or 0
        if task.task_type == Task.EXTERNAL:
            raw_start = cursor
            raw_end = add_calendar_days(cursor, max(dur, external_min_days))
            is_marker, is_external = False, True
        elif dur == 0:
            raw_start = raw_end = task.due_date if task.due_date is not None else cursor
            is_marker, is_external = True, False
        else:
            raw_start = cursor
            raw_end = task.due_date if task.due_date is not None else add_calendar_days(cursor, dur)
            if raw_end < raw_start:            # guard a manually-inverted due date
                raw_end = raw_start
            cursor = raw_end                   # chain off the actual end (stored or computed)
            is_marker, is_external = (raw_start == raw_end), False

        offset = timedelta(days=buffer_accum)
        rows.append({
            'task_name':   task.task_name,
            'phase_name':  task.phase.phase_name,
            'phase_order': phase_order,
            'task_order':  task.task_order,
            'task_type':   task.task_type,
            'is_external': is_external,
            'is_marker':   is_marker,
            'status':      task.status,
            'start':       raw_start + offset,
            'end':         raw_end + offset,
        })

    return rows


def build_gantt_view(rows, phase_label_map=None, task_label_map=None):
    """
    Turn compute_gantt_schedule() rows into a render-ready weekly grid.

    Weekly columns run from the Monday on/before the earliest start to the week of
    the latest end (partial first/last weeks included). Each row carries a per-week
    `cells` list so the template needs no arithmetic:
      cell = {filled, marker, first, last}
    A bar occupies contiguous `filled` cells (rounded at first/last); a milestone
    shows a `marker` diamond in a single cell; tasks with no dates get all-empty
    cells and are flagged has_dates=False (rendered as a "date TBD" row).

    phase_label_map / task_label_map (Client view) remap the band + task labels;
    a missing key falls back to the internal name — never blank. Pass None (Internal
    view) to use internal names verbatim.
    """
    from .gantt_constants import GANTT_PHASE_COLORS

    phase_label_map = phase_label_map or {}
    task_label_map  = task_label_map or {}

    dated = [r for r in rows if r['start'] and r['end']]
    weeks = []
    grid_start = None
    if dated:
        min_start = min(r['start'] for r in dated)
        max_end   = max(r['end'] for r in dated)
        grid_start = min_start - timedelta(days=min_start.weekday())  # Monday anchor
        n_weeks = (max_end - grid_start).days // 7 + 1
        for i in range(n_weeks):
            wk_start = grid_start + timedelta(days=7 * i)
            weeks.append({'label': wk_start.strftime('%d %b'), 'start': wk_start})

    out_rows = []
    for r in rows:
        has_dates = bool(r['start'] and r['end'])
        cells = []
        if has_dates and grid_start is not None:
            offset = (r['start'] - grid_start).days // 7
            if r['is_marker']:
                span = 1
            else:
                end_idx = (r['end'] - grid_start).days // 7
                span = max(1, end_idx - offset + 1)
            for i in range(len(weeks)):
                in_bar = offset <= i < offset + span
                cells.append({
                    'filled': in_bar and not r['is_marker'],
                    'marker': r['is_marker'] and i == offset,
                    'first':  in_bar and i == offset,
                    'last':   in_bar and i == offset + span - 1,
                })
        else:
            cells = [{'filled': False, 'marker': False, 'first': False, 'last': False} for _ in weeks]

        out_rows.append({
            'label':       task_label_map.get(r['task_name'], r['task_name']),
            'phase_label': phase_label_map.get(r['phase_name'], r['phase_name']),
            'phase_order': r['phase_order'],
            'color':       GANTT_PHASE_COLORS.get(r['phase_order'], '#888888'),
            'is_external': r['is_external'],
            'is_marker':   r['is_marker'],
            'status':      r['status'],
            'has_dates':   has_dates,
            'start':       r['start'],
            'end':         r['end'],
            'cells':       cells,
        })

    return {'weeks': weeks, 'rows': out_rows}


# Hardcoded fallback durations used when TaskDurationTemplate DB table is empty.
# Must stay in sync with the seed data in migration 0034_task_duration_template.
RESIDENTIAL_DURATION_DEFAULTS = {
    'OCR, Documentation & Verification':    2,
    'Advance Payment Confirmation':          1,
    'DEV Schedule':                          1,
    'DEV Conduct':                           2,
    'DEV Data to Design':                    1,
    'DEV Inputs Validation':                 1,
    'Design':                                2,
    'Array Layout':                          2,
    'SLD':                                   2,
    'Installation Drawings':                 1,
    'BOQ Preparation':                       1,
    'Design Approval by Internal Team':      1,
    'Design Approval by Customer':           1,
    'Pre Installation Approvals':            2,
    'LC / PC / NC Required':                 2,
    'Vendor Registration':                   2,
    'Document Preparation':                  2,
    'Signing Document by Customer':          2,
    'Net Metering Application Submission':   2,
    'TFR Received':                          2,
    'Procurement Schedule':                  1,
    'PO Placed MMS':                         1,
    'PO Placed Module':                      1,
    'PO Placed Inverter':                    1,
    'PO for B & C Class Items':              1,
    'Finance Confirmation':                  1,
    'Pre Dispatch Payment Confirmation':     1,
    'Delivery Schedule':                     1,
    'Delivery of MMS':                       1,
    'Delivery of B & C Class Items':         1,
    'Delivery of Module':                    1,
    'Delivery of Inverter':                  1,
    'MMS Installation':                      1,
    'Earthing Work':                         1,
    'Module Installation':                   1,
    'Inverter Installation':                 1,
    'DC Wire Work':                          1,
    'AC Cable Work':                         1,
    'Connections and Voc Testing':           1,
    'Pre Commissioning Check List':          0,
    'Pre Commissioning Visit by DISCOM':     2,
    'Meter Testing':                         1,
    'SCO Release':                           2,
    'Meter Installation by DISCOM':          2,
    'RMS Configuration':                     1,
    'Plant Commissioning':                   1,
    'Commissioning Report Prepared':         1,
    'Commissioning Report Approved':         0,
    'Customer Handover':                     0,
    '100% Payment Confirmation':             2,
}


def _get_duration(task_name, overrides):
    """Return duration_days from DB overrides, falling back to hardcoded defaults."""
    if task_name in overrides:
        return overrides[task_name]
    return RESIDENTIAL_DURATION_DEFAULTS.get(task_name, 1)


# --- Residential Finance-owned tasks (part of the Residential template) -----
# The same production Finance user back-assigns two groups of tasks at activation:
#   1. Three fixed "send invoice" tasks inserted into the template
#      (Phase 1 / pos 2, Phase 5 / pos 6, Phase 8 / pos 10) — plain manual
#      tasks, no PDF/email automation.
#   2. Three finance-confirmation tasks already in the template
#      (RESIDENTIAL_FINANCE_CONFIRMATION_TASK_NAMES below).
#
# The assignee is resolved by email in ONE place. The account exists in
# production; it is required data — if absent, activation fails loudly and rolls
# back (see attach_residential_template) rather than creating tasks unassigned.
RESIDENTIAL_FINANCE_ASSIGNEE_EMAIL = 'santosh@horizonrenewablepower.com'

# Identified by fixed name (no dedicated model field) for future filtering.
INVOICE_TASK_ADVANCE  = 'Send Invoice - Advance Payment'
INVOICE_TASK_MATERIAL = 'Send Invoice - Material Supply'
INVOICE_TASK_FINAL    = 'Send Invoice - Final Payment'
INVOICE_TASK_NAMES = (INVOICE_TASK_ADVANCE, INVOICE_TASK_MATERIAL, INVOICE_TASK_FINAL)

# Existing finance-confirmation tasks back-assigned to the same Finance user.
# ("Finance Confirmation" is intentionally NOT here — it is deleted from the
# template; its M2 milestone role passes to Pre Dispatch Payment Confirmation.)
RESIDENTIAL_FINANCE_CONFIRMATION_TASK_NAMES = (
    'Advance Payment Confirmation',       # Phase 1
    'Pre Dispatch Payment Confirmation',  # Phase 5 (M2 payment milestone)
    '100% Payment Confirmation',          # Phase 9
)


def build_residential_phases():
    """
    Return the Residential EPC template as a list of phase dicts (9 phases / 52 tasks).

    NO LONGER EXECUTED AT RUNTIME. Prompt 0.4 moved the template into the database as
    TaskTemplate 'RESIDENTIAL' v1; attach_residential_template() now reads that instead
    of calling this. This function stays because it IS the seed migration 0067 read, and
    because the runtime bootstrap re-seeds a virgin database from it — deleting it would
    make both unreproducible. Change the template by shipping version+1, never by
    editing these literals.

    Task is imported inside to avoid a module-level circular import.
    """
    from .models import Task
    PHASES = [
            {
                'phase_name':  'Sales & Documentation',
                'phase_order': 1,
                'tasks': [
                    {'task_order': 1, 'task_name': 'OCR, Documentation & Verification', 'assigned_role': Task.BD,      'task_type': Task.INTERNAL},
                    {'task_order': 2, 'task_name': INVOICE_TASK_ADVANCE,                'assigned_role': Task.FINANCE, 'task_type': Task.INTERNAL},  # Send invoice — advance
                    {'task_order': 3, 'task_name': 'Advance Payment Confirmation',       'assigned_role': Task.FINANCE, 'task_type': Task.INTERNAL, 'is_payment_milestone': True},  # M1: Advance Payment
                ],
            },
            {
                'phase_name':  'Detail Engineering Visit',
                'phase_order': 2,
                'tasks': [
                    {'task_order': 1, 'task_name': 'DEV Schedule',          'assigned_role': Task.PM,           'task_type': Task.INTERNAL},
                    {'task_order': 2, 'task_name': 'DEV Conduct',           'assigned_role': Task.SITE_ENGINEER, 'task_type': Task.INTERNAL},
                    {'task_order': 3, 'task_name': 'DEV Data to Design',    'assigned_role': Task.SITE_ENGINEER, 'task_type': Task.INTERNAL},
                    {'task_order': 4, 'task_name': 'DEV Inputs Validation', 'assigned_role': Task.DESIGN,        'task_type': Task.INTERNAL},
                ],
            },
            {
                'phase_name':  'Design',
                'phase_order': 3,
                'tasks': [
                    {'task_order': 1, 'task_name': 'Design',                            'assigned_role': Task.DESIGN, 'task_type': Task.INTERNAL},
                    {'task_order': 2, 'task_name': 'Array Layout',                      'assigned_role': Task.DESIGN, 'task_type': Task.INTERNAL},
                    {'task_order': 3, 'task_name': 'SLD',                               'assigned_role': Task.DESIGN, 'task_type': Task.INTERNAL},
                    {'task_order': 4, 'task_name': 'Installation Drawings',             'assigned_role': Task.DESIGN, 'task_type': Task.INTERNAL},
                    {'task_order': 5, 'task_name': 'BOQ Preparation',                  'assigned_role': Task.DESIGN, 'task_type': Task.INTERNAL},
                    {'task_order': 6, 'task_name': 'Design Approval by Internal Team', 'assigned_role': Task.PM,     'task_type': Task.INTERNAL},
                    {'task_order': 7, 'task_name': 'Design Approval by Customer',      'assigned_role': Task.PM,     'task_type': Task.EXTERNAL},
                ],
            },
            {
                'phase_name':  'Pre-Installation Approvals',
                'phase_order': 4,
                'tasks': [
                    {'task_order': 1, 'task_name': 'Pre Installation Approvals',          'assigned_role': Task.PM,  'task_type': Task.INTERNAL},
                    {'task_order': 2, 'task_name': 'LC / PC / NC Required',               'assigned_role': Task.PM,  'task_type': Task.EXTERNAL},
                    {'task_order': 3, 'task_name': 'Vendor Registration',                 'assigned_role': Task.SCM, 'task_type': Task.EXTERNAL},
                    {'task_order': 4, 'task_name': 'Document Preparation',                'assigned_role': Task.PM,  'task_type': Task.INTERNAL},
                    {'task_order': 5, 'task_name': 'Signing Document by Customer',        'assigned_role': Task.PM,  'task_type': Task.EXTERNAL},
                    {'task_order': 6, 'task_name': 'Net Metering Application Submission', 'assigned_role': Task.PM,  'task_type': Task.INTERNAL},
                    {'task_order': 7, 'task_name': 'TFR Received',                        'assigned_role': Task.PM,  'task_type': Task.EXTERNAL},
                ],
            },
            {
                'phase_name':  'Procurement',
                'phase_order': 5,
                'tasks': [
                    {'task_order': 1, 'task_name': 'Procurement Schedule',              'assigned_role': Task.SCM,     'task_type': Task.INTERNAL},
                    {'task_order': 2, 'task_name': 'PO Placed MMS',                     'assigned_role': Task.SCM,     'task_type': Task.INTERNAL},
                    {'task_order': 3, 'task_name': 'PO Placed Module',                  'assigned_role': Task.SCM,     'task_type': Task.INTERNAL},
                    {'task_order': 4, 'task_name': 'PO Placed Inverter',                'assigned_role': Task.SCM,     'task_type': Task.INTERNAL},
                    {'task_order': 5, 'task_name': 'PO for B & C Class Items',          'assigned_role': Task.SCM,     'task_type': Task.INTERNAL},
                    {'task_order': 6, 'task_name': INVOICE_TASK_MATERIAL,              'assigned_role': Task.FINANCE, 'task_type': Task.INTERNAL},  # Send invoice — material supply
                    {'task_order': 7, 'task_name': 'Pre Dispatch Payment Confirmation', 'assigned_role': Task.FINANCE, 'task_type': Task.INTERNAL, 'is_payment_milestone': True},  # M2: Pre Dispatch (replaces deleted Finance Confirmation)
                ],
            },
            {
                'phase_name':  'Delivery',
                'phase_order': 6,
                'tasks': [
                    {'task_order': 1, 'task_name': 'Delivery Schedule',             'assigned_role': Task.SCM, 'task_type': Task.INTERNAL},
                    {'task_order': 2, 'task_name': 'Delivery of MMS',               'assigned_role': Task.SCM, 'task_type': Task.INTERNAL},
                    {'task_order': 3, 'task_name': 'Delivery of B & C Class Items', 'assigned_role': Task.SCM, 'task_type': Task.INTERNAL},
                    {'task_order': 4, 'task_name': 'Delivery of Module',            'assigned_role': Task.SCM, 'task_type': Task.INTERNAL},
                    {'task_order': 5, 'task_name': 'Delivery of Inverter',          'assigned_role': Task.SCM, 'task_type': Task.INTERNAL},
                ],
            },
            {
                'phase_name':  'Installation',
                'phase_order': 7,
                'tasks': [
                    {'task_order': 1, 'task_name': 'MMS Installation',             'assigned_role': Task.SITE_ENGINEER, 'task_type': Task.INTERNAL},
                    {'task_order': 2, 'task_name': 'Earthing Work',                'assigned_role': Task.SITE_ENGINEER, 'task_type': Task.INTERNAL},
                    {'task_order': 3, 'task_name': 'Module Installation',          'assigned_role': Task.SITE_ENGINEER, 'task_type': Task.INTERNAL},
                    {'task_order': 4, 'task_name': 'Inverter Installation',        'assigned_role': Task.SITE_ENGINEER, 'task_type': Task.INTERNAL},
                    {'task_order': 5, 'task_name': 'DC Wire Work',                 'assigned_role': Task.SITE_ENGINEER, 'task_type': Task.INTERNAL},
                    {'task_order': 6, 'task_name': 'AC Cable Work',                'assigned_role': Task.SITE_ENGINEER, 'task_type': Task.INTERNAL},
                    {'task_order': 7, 'task_name': 'Connections and Voc Testing',  'assigned_role': Task.SITE_ENGINEER, 'task_type': Task.INTERNAL},
                    {'task_order': 8, 'task_name': 'Pre Commissioning Check List', 'assigned_role': Task.SITE_ENGINEER, 'task_type': Task.INTERNAL},
                ],
            },
            {
                'phase_name':  'Commissioning',
                'phase_order': 8,
                'tasks': [
                    {'task_order': 1, 'task_name': 'Pre Commissioning Visit by DISCOM', 'assigned_role': Task.PM,            'task_type': Task.EXTERNAL},
                    {'task_order': 2, 'task_name': 'Meter Testing',                     'assigned_role': Task.SITE_ENGINEER,  'task_type': Task.INTERNAL},
                    {'task_order': 3, 'task_name': 'SCO Release',                       'assigned_role': Task.PM,            'task_type': Task.EXTERNAL},
                    {'task_order': 4, 'task_name': 'Meter Installation by DISCOM',      'assigned_role': Task.PM,            'task_type': Task.EXTERNAL},
                    {'task_order': 5, 'task_name': 'RMS Configuration',                 'assigned_role': Task.SITE_ENGINEER,  'task_type': Task.INTERNAL},
                    {'task_order': 6, 'task_name': 'Plant Commissioning',               'assigned_role': Task.SITE_ENGINEER,  'task_type': Task.INTERNAL},
                    {'task_order': 7, 'task_name': 'Commissioning Report Prepared',     'assigned_role': Task.SITE_ENGINEER,  'task_type': Task.INTERNAL},
                    {'task_order': 8, 'task_name': 'Commissioning Report Approved',     'assigned_role': Task.PM,            'task_type': Task.INTERNAL},
                    {'task_order': 9, 'task_name': 'Customer Handover',                 'assigned_role': Task.PM,            'task_type': Task.INTERNAL},
                    {'task_order': 10, 'task_name': INVOICE_TASK_FINAL,                 'assigned_role': Task.FINANCE,       'task_type': Task.INTERNAL},  # Send invoice — final payment
                ],
            },
            {
                'phase_name':  'Finance Closure',
                'phase_order': 9,
                'tasks': [
                    {'task_order': 1, 'task_name': '100% Payment Confirmation', 'assigned_role': Task.FINANCE, 'task_type': Task.INTERNAL, 'is_payment_milestone': True},  # M3: 100% Payment
                ],
            },
    ]
    return PHASES


def get_residential_template_task_names():
    """Return the ordered, de-duplicated list of (phase_name, task_name) pairs for the
    Residential template — the known task names the checklist admin can assign to.

    Reads the ACTIVE template so the picker cannot drift from what activation actually
    creates. Falls back to build_residential_phases() only when no template row exists
    at all, which is a virgin database; the two are identical by construction until a
    version 2 is authored, and once one is, an active template exists and the fallback
    can no longer fire.
    """
    template = resolve_active_task_template('Residential')
    if template is not None:
        source = [
            (phase.label, [t.label for t in phase.tasks.all()])
            for phase in template.phases.all()
        ]
    else:
        source = [
            (phase['phase_name'], [t['task_name'] for t in phase['tasks']])
            for phase in build_residential_phases()
        ]

    pairs = []
    seen = set()
    for phase_name, task_names in source:
        for name in task_names:
            if name not in seen:
                seen.add(name)
                pairs.append((phase_name, name))
    return pairs


# ---------------------------------------------------------------------------
# Versioned task templates (R-7)
#
# The template a project is built from lives in TaskTemplate / TaskTemplatePhase /
# TaskTemplateTask, not in Python source. Everything here is the read and seed side of
# that; the immutability rules live on the models.
# ---------------------------------------------------------------------------

RESIDENTIAL_TEMPLATE_CODE  = 'RESIDENTIAL'
RESIDENTIAL_TEMPLATE_LABEL = 'Residential EPC'


def template_code_from_label(label, max_length=100):
    """Stable uppercase identifier derived from a label.

    `code` is the cross-version identity of a phase or task: when version 2 rewords a
    label, `code` is what says it is still the same row. Derived rather than authored so
    version 1 needed no hand-written mapping. Verified collision-free across all 9 phase
    names and all 52 task names of the Residential template.
    """
    return re.sub(r'[^A-Za-z0-9]+', '_', label).strip('_').upper()[:max_length]


def resolve_active_task_template(project_type):
    """Return the active TaskTemplate for a project type, or None if there is none.

    prefetch_related because every caller immediately walks phases and their tasks —
    without it activation is 1 + 9 queries instead of 3.

    Import inside the function to avoid a module-level circular import.
    """
    from .models import TaskTemplate

    return (
        TaskTemplate.objects
        .filter(project_type=project_type, status=TaskTemplate.ACTIVE)
        .prefetch_related('phases__tasks')
        .first()
    )


def seed_task_template_version(*, template_model, phase_model, task_model,
                               code, label, project_type, version_no, phases,
                               duration_resolver, created_by=None):
    """Write one TaskTemplate version and its phases and tasks, then activate it.

    `phases` is a list of phase dicts in build_residential_phases() shape.
    `duration_resolver` is called as duration_resolver(task_name) -> int.

    Shared by migration 0067 and the virgin-database bootstrap below, so a template
    seeded from model state is byte-identical to the migrated one. The model CLASSES are
    passed in rather than imported because the migration hands over historical classes
    from apps.get_model(), which are different objects from the concrete ones.

    Created as a draft and flipped to active at the end, never created active: that is
    the order the real authoring flow uses, and the only order the R-7 save() guards on
    the concrete models permit.
    """
    template = template_model.objects.create(
        code=code,
        label=label,
        project_type=project_type,
        version_no=version_no,
        status='draft',
        effective_from=timezone.now().date(),
        created_by=created_by,
    )

    for phase_data in phases:
        phase = phase_model.objects.create(
            template=template,
            code=template_code_from_label(phase_data['phase_name'], 50),
            label=phase_data['phase_name'],
            sort_order=phase_data['phase_order'],
        )
        task_model.objects.bulk_create([
            task_model(
                phase=phase,
                code=template_code_from_label(t['task_name'], 100),
                label=t['task_name'],
                sort_order=t['task_order'],
                assigned_role=t['assigned_role'],
                task_type=t['task_type'],
                duration_days=duration_resolver(t['task_name']),
                is_payment_milestone=t.get('is_payment_milestone', False),
                # Optional, exactly like is_payment_milestone above: the Residential
                # phase dicts carry no 'is_mirror' key and so seed False, unchanged.
                # Set HERE, while the version is still a draft, rather than by a
                # QuerySet.update() after activate() — the R-7 guard is meant to make
                # post-activation content writes impossible, and leaning on the
                # documented update() bypass to author content would set the wrong
                # precedent in the very migration that introduces the field.
                is_mirror=t.get('is_mirror', False),
            )
            for t in phase_data['tasks']
        ])

    # Activate by hand rather than via TaskTemplate.activate(): a historical model class
    # carries no methods, and this function must work for both.
    # filter().update() so the archive and the activation cannot interleave with a
    # concurrent activation of a third version.
    template_model.objects.filter(
        code=code, status='active',
    ).exclude(pk=template.pk).update(status='archived')
    template.status = 'active'
    template.save(update_fields=['status'])
    return template


def _residential_duration_resolver():
    """Return a duration_resolver bound to the live TaskDurationTemplate overrides.

    Resolves exactly as _get_duration() always has — a TaskDurationTemplate row where
    one exists, else RESIDENTIAL_DURATION_DEFAULTS, else 1 — so a template seeded now
    gives the durations a project activated yesterday received.

    Import inside the function to avoid a module-level circular import.
    """
    from .models import TaskDurationTemplate

    # Note the case difference: this table stores 'residential' lowercase while
    # Project.project_type stores 'Residential'. Two vocabularies, never compared.
    overrides = {
        obj.task_name: obj.duration_days
        for obj in TaskDurationTemplate.objects.filter(project_type='residential')
    }
    return lambda task_name: _get_duration(task_name, overrides)


def resolve_residential_template():
    """Resolve the active RESIDENTIAL template, bootstrapping version 1 if the database
    has never held one.

    Three outcomes, and none of them is a silent fallback to build_residential_phases():

      - an active version exists      -> use it. The only path production ever takes,
                                         because migration 0067 seeded it at deploy.
      - NO row with this code at all   -> seed version 1 from build_residential_phases().
                                         A virgin database: the test suite (test_settings
                                         disables migrations, so 0067 never runs) or a
                                         schema built with --run-syncdb.
      - rows exist but none is active  -> RAISE. Someone archived every version. That is
                                         an operator error, and inventing a version to
                                         paper over it would hide it.

    The bootstrap is what stops this from needing a silent fallback, which would be the
    same trap as an admin screen that saves a value nothing reads.
    """
    from .models import TaskTemplate, TaskTemplatePhase, TaskTemplateTask

    template = resolve_active_task_template('Residential')
    if template is not None:
        return template

    if TaskTemplate.objects.filter(code=RESIDENTIAL_TEMPLATE_CODE).exists():
        raise TaskTemplate.DoesNotExist(
            f"Task template '{RESIDENTIAL_TEMPLATE_CODE}' exists but no version of it "
            f"is active. Activate one before activating a Residential project; a "
            f"version is not created automatically to cover this."
        )

    try:
        # Savepoint: a concurrent first activation losing the unique constraint must not
        # poison the outer activation transaction.
        with transaction.atomic():
            seed_task_template_version(
                template_model=TaskTemplate,
                phase_model=TaskTemplatePhase,
                task_model=TaskTemplateTask,
                code=RESIDENTIAL_TEMPLATE_CODE,
                label=RESIDENTIAL_TEMPLATE_LABEL,
                project_type='Residential',
                version_no=1,
                phases=build_residential_phases(),
                duration_resolver=_residential_duration_resolver(),
            )
    except IntegrityError:
        # Another activation seeded version 1 first. Re-read rather than fail: this can
        # only happen on a virgin database and both writers wanted the same rows.
        pass

    template = resolve_active_task_template('Residential')
    if template is None:
        raise TaskTemplate.DoesNotExist(
            f"Failed to resolve or bootstrap task template "
            f"'{RESIDENTIAL_TEMPLATE_CODE}'."
        )
    return template


def _attach_task_template(project, template):
    """Create `project`'s phases and tasks from `template`. THE ONE ATTACH.

    Shared by attach_residential_template() and attach_opex_template(), which differ
    only in which template they resolve and which of the created tasks they pre-assign
    afterwards. EXTRACTED by prompt 1.3c rather than copied: a second copy of this loop
    is a second place for the seventh snapshot below to be forgotten, and forgetting it
    once already made the whole of 1.3b inert (B19).

    Does NOT open its own transaction. The caller owns the atomic block, because the
    integrity assertions at the end must abort the caller's ACTIVATION and not merely
    this function — and callers must resolve `template` inside that same block, so a
    virgin database's bootstrap seed rolls back with the activation that triggered it
    rather than surviving a failed one.

    Returns the flat list of TaskTemplateTask rows attached, in template order.
    """
    # import inside function to avoid circular import at module level
    from .models import ProjectPhase, Task

    # Every task row of the template, in order, so the assertions below have
    # something to compare the created rows against.
    template_tasks = []

    for tpl_phase in template.phases.all():
        phase = ProjectPhase.objects.create(
            project=project,
            phase_name=tpl_phase.label,
            phase_order=tpl_phase.sort_order,
        )
        tpl_tasks = list(tpl_phase.tasks.all())
        template_tasks.extend(tpl_tasks)
        Task.objects.bulk_create([
            Task(
                phase=phase,
                task_name=t.label,
                task_order=t.sort_order,
                assigned_role=t.assigned_role,
                duration_days=t.duration_days,
                task_type=t.task_type,
                is_payment_milestone=t.is_payment_milestone,
                # THE SEVENTH SNAPSHOT — added by prompt 1.3c, closing B19. Absent
                # from this list until now, so no Task row could ever carry
                # is_mirror=True however its template row was flagged, and every
                # counter exclusion 1.3b shipped was correct and completely inert.
                # A SNAPSHOT like the six above, not a join through template_task:
                # retiring or re-versioning a template must never rewrite which rows
                # a live project treats as system-owned.
                is_mirror=t.is_mirror,
                template_task=t,   # provenance; nothing reads it back
            )
            for t in tpl_tasks
        ])

    # Integrity checks — roll back everything if the created rows do not match the
    # template. These assertions run inside the caller's atomic block so a mismatch
    # aborts the transaction, which is the whole point of them: a half-seeded project
    # must never ship. The expected numbers are DERIVED FROM THE TEMPLATE rather than
    # hardcoded, because the template is data and a second version — or a second
    # project type — may legitimately have different counts. They still catch a
    # partial write.
    expected_phases   = len(template.phases.all())
    expected_total    = len(template_tasks)
    expected_internal = sum(1 for t in template_tasks if t.task_type == Task.INTERNAL)
    expected_external = sum(1 for t in template_tasks if t.task_type == Task.EXTERNAL)

    # An empty template would make every count assertion below pass trivially and
    # ship a project with no work in it. Checked explicitly for that reason.
    assert expected_total > 0, f"Task template {template} has no tasks"

    phase_count = ProjectPhase.objects.filter(project=project).count()
    assert phase_count == expected_phases, \
        f"Expected {expected_phases} phases, got {phase_count}"

    task_count = Task.objects.filter(phase__project=project).count()
    assert task_count == expected_total, f"Expected {expected_total} tasks, got {task_count}"

    internal_count = Task.objects.filter(phase__project=project, task_type=Task.INTERNAL).count()
    assert internal_count == expected_internal, \
        f"Expected {expected_internal} internal tasks, got {internal_count}"

    external_count = Task.objects.filter(phase__project=project, task_type=Task.EXTERNAL).count()
    assert external_count == expected_external, \
        f"Expected {expected_external} external tasks, got {external_count}"

    return template_tasks


def attach_opex_template(project):
    """
    Create the phases and tasks for an OPEX site from the ACTIVE OPEX TaskTemplate.

    The non-Residential half of the attach, added by prompt 1.3c so that the 91 tender
    sites which cannot pass project_activate's designer gate can enter execution.
    Same core as Residential — _attach_task_template() above — and deliberately NONE of
    the three Residential-specific steps that follow it there:

      - no Finance-assignee raise, and no invoice / finance-confirmation name list:
        those tasks are Residential and do not exist in this template. Carrying the
        raise across would make OPEX activation depend on an account it has no use for.
      - PM pre-assignment EXCLUDES MIRRORS. The three real PM tasks (Net Metering,
        CEIG, Post-Installation Approvals) go to the site's PM so they are workable on
        day one; COD and HOTO — PM-role MIRRORS — are left with assigned_to NULL,
        because an unassigned mirror is an accurate statement that the row is nobody's
        task. Decided as OPEX_TEMPLATE_AUDIT.md §8 recommends. The owning ROLE is still
        set on all five mirrors, by the template.

    NO BOOTSTRAP, unlike resolve_residential_template(). Migration 0075 seeds OPEX v1 at
    deploy; if no active OPEX template exists this RAISES rather than inventing one from
    model state. There is no runtime OPEX builder to bootstrap from, and a silent
    fallback here would be the same trap as an admin screen that saves a value nothing
    reads. Under the test suite (test_settings disables migrations) callers seed by
    calling migration 0075's own seed_opex_v1().

    Resolves by `project.project_type` rather than a hardcoded 'OPEX', so a CAPEX
    template becomes a seed and an activation route with no further change here.
    Entire operation is atomic — any failure rolls back all phases and tasks.
    """
    from .models import Task, TaskTemplate

    with transaction.atomic():

        # Resolved inside the atomic block for the reason given on the core above.
        template = resolve_active_task_template(project.project_type)
        if template is None:
            raise TaskTemplate.DoesNotExist(
                f"No ACTIVE task template for project_type "
                f"'{project.project_type}'. Seed and activate one before activating a "
                f"{project.project_type} site; a version is not created automatically "
                f"to cover this."
            )

        _attach_task_template(project, template)

        # Pre-assign the PM's own NON-MIRROR tasks. `is_mirror=False` is the whole
        # difference from the Residential filter below, and it is load-bearing: without
        # it COD and HOTO land in every per-user counter as the PM's work, on 95 sites,
        # protected only by 1.3b's exclusion rather than by being true.
        assign_tasks_to(
            Task.objects.filter(
                phase__project=project,
                assigned_role=Task.PM,
                is_mirror=False,
            ),
            project.assigned_pm,
        )


def attach_residential_template(project):
    """
    Create the phases and tasks for a Residential project from the ACTIVE TaskTemplate.

    Reads TaskTemplate 'RESIDENTIAL' rather than build_residential_phases() since prompt
    0.4 — the template is data, and can be changed by shipping a new version instead of
    a deploy. Everything else about this function is unchanged.

    The phase/task creation and the integrity assertions moved into
    _attach_task_template() in prompt 1.3c so OPEX could share them; what stays here is
    exactly the Residential-specific part, and the behaviour is unchanged. The name is
    kept because three call sites use it, two of them in test modules that prompt was
    forbidden to touch.

    Pre-assigns PM-role tasks to assigned_pm. SE-role tasks start unassigned.
    The send-invoice and finance-confirmation tasks are auto-assigned to
    RESIDENTIAL_FINANCE_ASSIGNEE_EMAIL (required data — activation fails loudly
    and rolls back if that user is absent).
    Entire operation is atomic — any failure rolls back all phases and tasks.
    """
    # import inside function to avoid circular import at module level
    from .models import Task, UserProfile

    with transaction.atomic():

        # Resolved INSIDE the atomic block so a virgin database's bootstrap seed rolls
        # back with the activation it was triggered by, rather than surviving a failed
        # one. Raises rather than falling back if a template exists but none is active.
        template = resolve_residential_template()

        _attach_task_template(project, template)

        # Pre-assign PM tasks to the named PM on this project.
        # SE-role tasks start unassigned — same as Design/SCM/Finance.
        pm_profile = project.assigned_pm

        assign_tasks_to(
            Task.objects.filter(
                phase__project=project,
                assigned_role=Task.PM,
            ),
            pm_profile,
        )

        # Auto-assign the send-invoice AND finance-confirmation tasks to the same
        # Finance user (by email), resolved in one place. This is required data:
        # if the account is missing, fail loudly so the whole atomic activation
        # rolls back — never create these tasks unassigned. Uses an explicit raise
        # (not the assert pattern in _attach_task_template) because asserts are stripped under
        # `python -O`, which would silently re-enable create-unassigned behavior.
        finance_assignee = (
            UserProfile.objects
            .filter(user__email=RESIDENTIAL_FINANCE_ASSIGNEE_EMAIL)
            .first()
        )
        if finance_assignee is None:
            raise UserProfile.DoesNotExist(
                f"Cannot activate Residential project: finance assignee "
                f"'{RESIDENTIAL_FINANCE_ASSIGNEE_EMAIL}' not found. A Finance "
                f"UserProfile with this email must exist to own the send-invoice "
                f"and finance-confirmation tasks."
            )
        assign_tasks_to(
            Task.objects.filter(
                phase__project=project,
                task_name__in=INVOICE_TASK_NAMES + RESIDENTIAL_FINANCE_CONFIRMATION_TASK_NAMES,
            ),
            finance_assignee,
        )
