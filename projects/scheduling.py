"""Task due-date scheduling (SCHED, spec: SCHED_Auto_Scheduling_Spec.md).

SCHED-1 holds one thing: set_task_due_date(), the single writer of Task.due_date.
Every production write site routes through it, so every date carries a source
(`manual` / `system`) and every manual change leaves one ActivityLog row. The
SCHED-2 scheduler will rewrite only `system` dates, which is why a write that
bypassed this function would be a bug: a person's date left marked `system` (or a
stale `system` mark under a typed date) gets overwritten.

The one known exception is management/commands/seed_opex_test_data.py (ruling D),
a demo-data command that writes due_date with a queryset update. Its rows carry a
date and a null source, which the scheduler treats as untouchable.
"""
import logging

from django.db import transaction

from .models import ActivityLog, Task

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
