"""
Query helpers for payment milestones that more than one reader needs.

Not a views module: it imports models and utils only, never views, decorators or
templates, so a view or a data layer can import it without a cycle.
"""
from django.db.models import BooleanField, Case, CharField, OuterRef, Subquery, Value, When

from .models import Task
from .utils import INVOICE_DUE_TRIGGERS


def annotate_trigger_done(milestone_qs):
    """Annotate each milestone in `milestone_qs` with the state of its R8 trigger task:
    the task whose Done makes an M2/M3 invoice fall due (utils.INVOICE_DUE_TRIGGERS —
    "PO for B & C Class Items" for M2, "Customer Handover" for M3). Returns the queryset.

    Extracted in FIN-H-B2-0 from views_finance_work._milestone_queryset(); used by the
    Finance work page and, next, by the Finance Head data layer (FIN-H-B2).

    ANNOTATIONS
        trigger_name     the trigger task's name; '' for M1, which has no trigger (R4)
        trigger_status   the task's status, None when the site has no such task
        trigger_na       the task's Not Applicable flag, None when there is no task
        trigger_done_at  the task's completed_at, None when there is no task or it was
                         never completed
        trigger_task_id  the task's pk, None when there is no task (FIN-H-B4-1: the
                         Finance Head item list reads the Done transition of this very
                         task for its history; Finance work and the B2 figures ignore it)

    HOW FINANCE WORK READS THEM (views_finance_work._milestone_rows, R8 + ruling 4), in
    this order — a reader that wants the same "due" must keep it:
        1. trigger_status is None    -> due, "no trigger task" (a missing trigger is due)
        2. trigger_status == Done    -> due, counted from trigger_done_at. Done is read
                                        BEFORE Not Applicable, so a Done task that also
                                        carries the N/A flag still counts from its date.
        3. trigger_na is True        -> due, "trigger N/A"
        4. anything else             -> not yet due
    M1 matches no task, so all three trigger values are None; the page handles M1 before
    it reads them.

    trigger_name is annotated first so the subqueries can match the task through
    OuterRef('trigger_name'). Each subquery is ordered by pk and takes [:1], so when a
    site carries two tasks of the trigger name the OLDEST (lowest pk) is read — the
    four values always come from the same task.

    Adds annotations only: no filter on status or project status, no select_related, no
    ordering. Those are each caller's own. Four correlated subqueries, so it adds no
    query of its own, whatever the number of milestones. A caller that ends in
    .values(...) without naming trigger_task_id leaves it out of its SELECT.
    """
    trigger_name = Case(
        *[When(milestone_name=name, then=Value(task))
          for name, task in INVOICE_DUE_TRIGGERS.items()],
        default=Value(''), output_field=CharField())
    # The site's task with the trigger name: matched through the milestone's project and
    # the trigger_name annotation; order_by('pk') makes [:1] deterministic.
    trigger = (Task.objects
               .filter(phase__project=OuterRef('project'), task_name=OuterRef('trigger_name'))
               .order_by('pk'))
    return (milestone_qs
            .annotate(trigger_name=trigger_name)
            .annotate(trigger_status=Subquery(trigger.values('status')[:1]),
                      trigger_na=Subquery(trigger.values('is_not_applicable')[:1],
                                          output_field=BooleanField()),
                      trigger_done_at=Subquery(trigger.values('completed_at')[:1]),
                      trigger_task_id=Subquery(trigger.values('pk')[:1])))
