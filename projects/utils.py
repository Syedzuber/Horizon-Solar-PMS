from datetime import timedelta

from django.db import transaction
from django.utils import timezone


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

    # select_for_update() cannot be combined with .count() (Django raises
    # NotSupportedError). Fetch locked IDs instead and count in Python.
    locked_ids = list(
        Project.objects
        .select_for_update()
        .filter(project_type=project_type, created_at__year=year)
        .values_list('id', flat=True)
    )
    count = len(locked_ids)
    return f"HRP-{prefix}-{year}-{count+1:03d}"


def add_workdays(start_date, days):
    """
    Advance start_date by N calendar days. days=0 returns start_date unchanged.
    NOTE: uses calendar days, not business days — weekends are not skipped.
    """
    return start_date + timedelta(days=days)


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
            new_d = add_workdays(prev_internal_due, task.duration_days)
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


def calculate_due_dates(project):
    """
    Assign due_date to every task on project starting from project.activated_at.
    Internal tasks chain sequentially off the previous internal task's due date.
    External tasks get the same due date as the current internal chain position
    (they run in parallel, not blocking the chain).
    Called on full recalculation (project_recalculate_dates view).
    """
    # import inside function to avoid circular import at module level
    from .models import Task

    tasks = (
        Task.objects
        .filter(phase__project=project)
        .order_by('phase__phase_order', 'task_order')
    )

    previous_internal_due = project.activated_at.date()

    for task in tasks:
        if task.task_type == Task.EXTERNAL:
            # External tasks shadow the current internal chain date
            task.due_date = previous_internal_due
        else:
            task.due_date = add_workdays(previous_internal_due, task.duration_days)
            previous_internal_due = task.due_date
        task.save()


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


def attach_residential_template(project):
    """
    Create all 9 phases and 50 tasks for a Residential project.
    Pre-assigns PM-role tasks to assigned_pm. SE-role tasks start unassigned.
    Entire operation is atomic — any failure rolls back all phases and tasks.
    Asserts at the end verify the expected task counts; a failed assert rolls back via the outer atomic().
    """
    # import inside function to avoid circular import at module level
    from .models import ProjectPhase, Task, TaskDurationTemplate

    # DB-first duration lookup — falls back to RESIDENTIAL_DURATION_DEFAULTS if table is empty
    duration_overrides = {
        obj.task_name: obj.duration_days
        for obj in TaskDurationTemplate.objects.filter(project_type='residential')
    }

    with transaction.atomic():

        # Full Residential EPC template — 9 phases, 50 tasks (42 internal, 8 external)
        PHASES = [
            {
                'phase_name':  'Sales & Documentation',
                'phase_order': 1,
                'tasks': [
                    {'task_order': 1, 'task_name': 'OCR, Documentation & Verification', 'assigned_role': Task.BD,     'task_type': Task.INTERNAL},
                    {'task_order': 2, 'task_name': 'Advance Payment Confirmation',       'assigned_role': Task.FINANCE, 'task_type': Task.INTERNAL, 'is_payment_milestone': True},  # M1: Advance Payment
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
                    {'task_order': 6, 'task_name': 'Finance Confirmation',              'assigned_role': Task.FINANCE, 'task_type': Task.INTERNAL, 'is_payment_milestone': True},  # M2: Finance Confirmation
                    {'task_order': 7, 'task_name': 'Pre Dispatch Payment Confirmation', 'assigned_role': Task.FINANCE, 'task_type': Task.INTERNAL},
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

        Task.objects.filter(
            phase__project=project,
            assigned_role=Task.PM,
        ).update(assigned_to=pm_profile)

        # Integrity checks — roll back everything if counts are wrong.
        # These assertions run inside the atomic block so a mismatch aborts the transaction.
        task_count = Task.objects.filter(phase__project=project).count()
        assert task_count == 50, f"Expected 50 tasks, got {task_count}"

        internal_count = Task.objects.filter(phase__project=project, task_type=Task.INTERNAL).count()
        assert internal_count == 42, f"Expected 42 internal tasks, got {internal_count}"

        external_count = Task.objects.filter(phase__project=project, task_type=Task.EXTERNAL).count()
        assert external_count == 8, f"Expected 8 external tasks, got {external_count}"
