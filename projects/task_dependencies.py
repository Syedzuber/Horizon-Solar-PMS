"""Task dependencies — the read-side predicate and the template-to-instance copy.

Introduced by prompt 1.4a (30 Aug 2026), answering B-08. The two models live in
`models.py` beside the template family they belong to; the *logic* lives here rather
than in `utils.py` because `incomplete_predecessors()` is the first piece of execution
*scheduling* in this codebase rather than another shape, and 1.4b plus whatever
eventually builds execution scheduling (B-05) will add beside it. Not in `views.py`.

READ THIS BEFORE ADDING ANYTHING HERE. B-08 was answered by the product owner on
30 Aug 2026: a dependent task may be started before its predecessor is Done, **by
anyone**, with a **mandatory reason** and a **warning**. No hard block. No role gate.
No approval step. Nothing in this module refuses a start, and nothing added to it may.
The mandatory reason is enforced by the status-change path (1.4b), which records the
early start as a `StatusTransition` with a remark — not here.
"""

from django.db import transaction


def incomplete_predecessors(task):
    """Predecessor tasks that are not yet Done, for a task about to be started.

    Read-only. Empty result means nothing blocks a normal start. A non-empty result
    does NOT forbid the start — B-08: anyone may proceed with a mandatory reason and
    a warning. This function reports; it never refuses.

    Returns the `Task` rows themselves, not a boolean and not a count: 1.4b has to name
    them in the warning, and a caller that only needs truthiness can test the result
    directly. A list rather than a queryset, so the cost is exactly one query at exactly
    the moment of the call, and a caller cannot accidentally re-run it by iterating
    twice.

    Ordered the way the tasks appear on screen — phase order, then task order — so the
    warning lists them in the sequence the reader is looking at.
    """
    from .models import Task

    return list(
        Task.objects
        .filter(dependents__successor=task)      # edges where `task` is the successor
        .exclude(status=Task.DONE)
        .select_related('phase')
        .order_by('phase__phase_order', 'task_order')
    )


def materialise_task_dependencies(project):
    """Copy this project's template dependency edges onto its own `Task` rows.

    Called once, at activation, immediately after the phases and tasks have been
    created and inside the same `transaction.atomic()` — see `attach_residential_template()`
    in `utils.py`. **Prompt 1.4a did not wire that call**; it builds the function so it
    can be called and tested in isolation, and 1.4b or a later session puts it in place.

    THE TEMPLATE VERSION IS DERIVED FROM THE PROJECT'S OWN TASKS, never re-resolved from
    whichever version happens to be active now. `Task.template_task` says which template
    row each task was built from; those rows are the truth about what this project was
    built from, and the active version may have moved on since. Re-resolving would
    reopen B-10 through the back door.

    Tasks whose `template_task` is null get no edges and cost nothing: a task added by
    hand has no template edge to copy, and `template_task` is `SET_NULL`, so provenance
    can be lost without the work being lost.

    IDEMPOTENT. Running it twice does not double the edges — existing pairs are read
    once and skipped. That matters because activation is retried in the field and
    because a later session may call this to repair a project.

    Writes one `save()` at a time and deliberately does NOT use `bulk_create()`.
    `bulk_create()` bypasses every guard `TaskDependency.clean()` holds — same-project,
    no-self-reference, no-cycles — and the materialiser is the one caller that must not
    be the thing that walks around them. A template is tens of tasks, this runs once per
    activation, and correctness is worth the round trips.

    Returns the number of edges created.
    """
    from .models import Task, TaskDependency, TaskTemplateTaskDependency

    tasks_by_template_task = {
        t.template_task_id: t
        for t in Task.objects.filter(
            phase__project=project, template_task__isnull=False,
        ).select_related('phase')
    }
    if not tasks_by_template_task:
        return 0

    template_task_ids = list(tasks_by_template_task)

    # Both endpoints must be present on this project. An edge with only one end
    # materialised would be a half-statement, and there is no honest thing to do with
    # it — so it is skipped rather than approximated.
    template_edges = TaskTemplateTaskDependency.objects.filter(
        predecessor_id__in=template_task_ids,
        successor_id__in=template_task_ids,
    ).values_list('predecessor_id', 'successor_id')

    existing = set(
        TaskDependency.objects
        .filter(predecessor__phase__project=project)
        .values_list('predecessor_id', 'successor_id')
    )

    created = 0
    with transaction.atomic():
        for tpl_pred_id, tpl_succ_id in template_edges:
            pred = tasks_by_template_task[tpl_pred_id]
            succ = tasks_by_template_task[tpl_succ_id]
            if (pred.pk, succ.pk) in existing:
                continue
            TaskDependency(predecessor=pred, successor=succ).save()
            existing.add((pred.pk, succ.pk))
            created += 1
    return created
