import logging

from datetime import timedelta

from django.db import transaction
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
    Single source of truth: attach_residential_template() builds projects from this, and
    the checklist admin's task-name picker sources its known task names from it. Task is
    imported inside to avoid a module-level circular import.
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
    Residential template — the known task names the checklist admin can assign to."""
    pairs = []
    seen = set()
    for phase in build_residential_phases():
        for t in phase['tasks']:
            name = t['task_name']
            if name not in seen:
                seen.add(name)
                pairs.append((phase['phase_name'], name))
    return pairs


def attach_residential_template(project):
    """
    Create all 9 phases and 52 tasks for a Residential project.
    Pre-assigns PM-role tasks to assigned_pm. SE-role tasks start unassigned.
    The send-invoice and finance-confirmation tasks are auto-assigned to
    RESIDENTIAL_FINANCE_ASSIGNEE_EMAIL (required data — activation fails loudly
    and rolls back if that user is absent).
    Entire operation is atomic — any failure rolls back all phases and tasks.
    Asserts at the end verify the expected task counts; a failed assert rolls back via the outer atomic().
    """
    # import inside function to avoid circular import at module level
    from .models import ProjectPhase, Task, TaskDurationTemplate, UserProfile

    # DB-first duration lookup — falls back to RESIDENTIAL_DURATION_DEFAULTS if table is empty
    duration_overrides = {
        obj.task_name: obj.duration_days
        for obj in TaskDurationTemplate.objects.filter(project_type='residential')
    }

    with transaction.atomic():

        PHASES = build_residential_phases()

        for phase_data in PHASES:
            phase = ProjectPhase.objects.create(
                project=project,
                phase_name=phase_data['phase_name'],
                phase_order=phase_data['phase_order'],
            )
            Task.objects.bulk_create([
                Task(
                    phase=phase,
                    task_name=t['task_name'],
                    task_order=t['task_order'],
                    assigned_role=t['assigned_role'],
                    duration_days=_get_duration(t['task_name'], duration_overrides),
                    task_type=t['task_type'],
                    is_payment_milestone=t.get('is_payment_milestone', False),
                )
                for t in phase_data['tasks']
            ])

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
        # (not the assert pattern below) because asserts are stripped under
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

        # Integrity checks — roll back everything if counts are wrong.
        # These assertions run inside the atomic block so a mismatch aborts the transaction.
        task_count = Task.objects.filter(phase__project=project).count()
        assert task_count == 52, f"Expected 52 tasks, got {task_count}"

        internal_count = Task.objects.filter(phase__project=project, task_type=Task.INTERNAL).count()
        assert internal_count == 44, f"Expected 44 internal tasks, got {internal_count}"

        external_count = Task.objects.filter(phase__project=project, task_type=Task.EXTERNAL).count()
        assert external_count == 8, f"Expected 8 external tasks, got {external_count}"
