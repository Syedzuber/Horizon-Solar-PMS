"""Punch points leave Open by approval, and the one site-level open-points query.
Closeout step 2 (docs/CLOSEOUT_SPEC.md: CL-1, CL-2, CL-B).

TWO FUNCTIONS, ONE PLACE EACH.

`close_punch_points_on_approval()` is the only runtime writer of `PunchPoint.CLOSED`
(migration 0111 wrote it once, for points that predate this rule). Two views call it: `task_approve`, and the self-certified branch of
`task_submit_for_approval`. Those are the two acts that put an approval on a task.
Re-completing an already-approved task through the status screens (Done -> Blocked ->
In Progress -> Done) is not an approval and does not call it. A waiver is not a
closure either; `punch_point_waive` keeps its own write.

`open_punch_points_for_project()` is the CL-2 query. The COD and HOTO gates (closeout
steps 4 and 8) will read it, and must not spell their own.

THE CLOSURE DOES NOT ASK WHERE A POINT CAME FROM (CL-B). Today every punch point is
raised by `task_reject`. From step 7 a QA/QC spot-check "No" raises them too. CL-1
closes every Open point on the approved task, whatever raised it, so nothing here
reads `raised_by` or the reason.
"""
from django.db import transaction

from .models import PunchPoint, log_activity


def close_punch_points_on_approval(task, actor, method, project):
    """Close every Open punch point on `task`, crediting `actor` at the approval time.

    Called INSIDE the approval's `transaction.atomic()` block, after the status
    function has accepted Done. A completion that is refused unwinds the closure with
    it, so a point is never Closed against an approval that did not happen.

    `task.approved_at` must already be set on the instance (both callers set it
    before asking for Done). It is the closure time, so the ledger shows the point
    closing at the moment of approval rather than a few milliseconds after it.

    `method` is `PunchPoint.CLOSED_ON_APPROVAL` or `CLOSED_ON_SELF_CERTIFIED`.
    Waived and already-Closed points are left exactly as they are.

    Returns the number of points closed.
    """
    if task.approved_at is None:
        raise ValueError('close_punch_points_on_approval needs an approved task')

    # Race: `status=OPEN` is in the UPDATE's own WHERE, not checked beforehand, so a
    # PM waiving the same point at the same moment cannot be overwritten. Whichever
    # write commits first wins the row, and the second matches nothing. The CHECK
    # punchpoint_closure_columns_match_status refuses any mixed result.
    closed = PunchPoint.objects.filter(task=task, status=PunchPoint.OPEN).update(
        status=PunchPoint.CLOSED,
        closed_by=actor,
        closed_at=task.approved_at,
        closure_method=method,
    )

    if closed:
        label = dict(PunchPoint.CLOSURE_METHOD_CHOICES)[method]
        text = (f"{label}: {closed} punch point{'s' if closed != 1 else ''} "
                f"on {task.task_name}")[:255]
        # After commit, not inline. log_activity swallows its own errors, but on
        # Postgres a failed INSERT inside this atomic block would still poison the
        # transaction and roll the approval back. A lambda rather than
        # functools.partial, as in approval_notices.py.
        transaction.on_commit(
            lambda: log_activity(
                project, actor, text,
                entity_type='Task', entity_id=task.pk,
                action_code='punch_points_closed',
            )
        )
    return closed


def open_punch_points_for_project(project):
    """Every Open punch point on `project`, across every task on the site (CL-2).

    THE ONE DEFINITION. The COD gate (closeout step 4) and the HOTO gate (step 8)
    will both refuse while this is non-empty. Neither may filter it further or spell
    its own version.

    - Closed and Waived never count. Only `status=Open` is outstanding.
    - Every task counts, Not Applicable ones included (ruling Q3, 8 Oct 2026). A
      defect on work later found not applicable is still a recorded defect until the
      PM waives it.
    - There is no severity and no "blocking" field (CL-2). Every Open point blocks.

    Returns a queryset, oldest first, so a gate can say how many points there are
    and list them.
    """
    return (
        PunchPoint.objects
        .filter(task__phase__project=project, status=PunchPoint.OPEN)
        .select_related('task')
        .order_by('created_at', 'pk')
    )
