"""The one definition of "this task is overdue".

Before this module fourteen readers each spelt the rule themselves and they did not
agree: the CEO cards and stuck_sites() counted Not Started / In Progress only, the
four dashboard stat blocks and the daily report counted everything not Done, and two
(the CEO At Risk badge and the Site Engineer's next task) did not exclude Not
Applicable. Every one of them now takes the rule from here.

THE RULE: a task is overdue when it has a due date, that date is strictly before
`today`, its status is not Done, and it is not marked Not Applicable.

  - Blocked counts. A blocked task past its date is still late.
  - Not Applicable is a FLAG (Task.is_not_applicable), not a status. The stored status
    underneath it is still Not Started / In Progress / Blocked, so the flag is tested
    beside the status rather than inside it.
  - Done is closed whether or not it was approved. An OPEX task cannot reach Done
    without approved_at (rung 1 of views._apply_task_status_change), and a task that is
    submitted and waiting for approval keeps its open status, so it is still overdue.
  - project_type is not part of the rule. tests_task_health holds that true.

WHAT IS NOT HERE, because it is SCOPE, not the rule. Each caller composes its own:
mirrors (utils.human_owned_tasks_q), Internal / External (Task.task_type), whose task
it is, and which projects count. "The CEO card counts internal overdue tasks" is
`Q(task_type=Task.INTERNAL) & overdue_q(today)`.

`today` IS ALWAYS PASSED IN, from the caller's timezone.localdate(). Nothing here reads
a clock, so one page cannot straddle midnight between two of its own counts.

NOT design_metrics.is_overdue() / days_overdue(): those are about a DesignAssignment's
approved due-date commitment, not a Task.
"""
from django.db.models import Q

from .models import Task


#: Every status in which a task's work is still outstanding: all of Task.STATUS_CHOICES
#: except Done. Derived, so a status added later is open work until someone decides
#: otherwise, and both forms below read the same tuple and cannot disagree.
#:
#: A positive list rather than ~Q(status=Done) for the reason utils.human_owned_tasks_q
#: gives: a negated Q across the multi-valued phases__tasks relation takes Django's
#: exclude() subquery path instead of a plain SQL FILTER, which changes the join fan-out
#: that conditional Counts on a Project queryset share.
OPEN_STATUSES = tuple(status for status, _ in Task.STATUS_CHOICES if status != Task.DONE)


def overdue_q(today, prefix=''):
    """Q() matching overdue tasks: the ORM form of the rule in the module docstring.

    `today` is the caller's timezone.localdate(). `prefix` is the relation path to Task,
    '' when querying Task itself and 'phases__tasks__' when querying Project: the same
    convention as utils.human_owned_tasks_q().
    """
    return Q(**{
        # `__lt` already drops NULL; the isnull term is spelt so the rule reads whole.
        f'{prefix}due_date__isnull': False,
        f'{prefix}due_date__lt': today,
        f'{prefix}status__in': OPEN_STATUSES,
        f'{prefix}is_not_applicable': False,
    })


def is_overdue(task, today):
    """Row-at-a-time form of overdue_q(), for a task already in memory. Same rule."""
    return (task.due_date is not None
            and task.due_date < today
            and task.status in OPEN_STATUSES
            and not task.is_not_applicable)


def days_overdue(task, today):
    """Whole days past the due date: 1 on the day after it. 0 when not overdue."""
    if not is_overdue(task, today):
        return 0
    return (today - task.due_date).days
