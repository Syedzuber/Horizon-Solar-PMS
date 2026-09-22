"""Row-level template filters for "Duplicate for locations" and "Rename task".

A FILTER AND NOT A CONTEXT FLAG, because _task_row.html is rendered by four responders
(project_overview and three HTMX helpers). A flag computed in one of them is a flag the
next responder forgets, and the icon silently vanishes on the first row swap. The filter
travels with the partial.

NO RULE HERE (R-13). It calls permissions.can_duplicate_task_for_locations() and nothing
else. The only thing it adds is the per-request cache for the project half of that
predicate, kept on `request.user` — AuthenticationMiddleware builds a fresh user object
for every request, so the cache cannot outlive the page it was built for.

    {% load duplicate_tags %}
    {% if task|can_duplicate_for_locations:request.user %}
    {% if task|can_rename:request.user %}
    {% if task|can_reorder:request.user %}

can_reorder asks permissions.can_reorder_phase() about the row's phase, with its own
cache keyed on the project (every term of that rule is on the project).

can_rename is the same arrangement around permissions.can_rename_task(), with a cache of
its own: the two predicates' project halves are different rules (rename admits
Residential), so one dict cannot serve both.

ONE PROJECT INSTANCE PER REQUEST, SHARED BY ALL THREE FILTERS. project_overview loads phases
with filter(project=...), so no phase has its project cached, and each filter's first
cache miss loads the project through whichever row reached it — two loads, plus the
assigned PM twice, whenever those rows sit in different phases. _seat_project() hands
the first loaded instance to every later row's phase. Queries only; no answer changes.
"""
from django import template

from ..permissions import can_duplicate_task_for_locations, can_rename_task, can_reorder_phase

register = template.Library()

_CACHE_ATTR = '_location_duplication_project_cache'
_RENAME_CACHE_ATTR = '_task_rename_project_cache'
_REORDER_CACHE_ATTR = '_phase_reorder_project_cache'
_PROJECTS_ATTR = '_row_filter_projects'


def _request_dict(user, attr):
    value = getattr(user, attr, None)
    if value is None:
        value = {}
        setattr(user, attr, value)
    return value


def _seat_project(task, user):
    """Give this row's phase the request's Project instance, if one is loaded yet. Reads
    no relation that is not already cached, so it never adds a query of its own."""
    if task is None or not type(task).phase.is_cached(task):
        return
    phase = task.phase
    projects = _request_dict(user, _PROJECTS_ATTR)
    if type(phase).project.is_cached(phase):
        projects.setdefault(phase.project_id, phase.project)
    elif phase.project_id in projects:
        phase.project = projects[phase.project_id]


def _ask(predicate, cache_attr, task, user):
    _seat_project(task, user)
    answer = predicate(user, task, project_cache=_request_dict(user, cache_attr))
    _seat_project(task, user)   # record the instance if this call was the one that loaded it
    return answer


@register.filter
def can_duplicate_for_locations(task, user):
    return _ask(can_duplicate_task_for_locations, _CACHE_ATTR, task, user)


@register.filter
def can_rename(task, user):
    return _ask(can_rename_task, _RENAME_CACHE_ATTR, task, user)


@register.filter
def can_reorder(task, user):
    """Asked per row, answered per project: the drag handle sits in each row's # cell,
    but can_reorder_phase() is a phase rule whose every term is on the project."""
    if task is None:
        return False
    _seat_project(task, user)
    answer = can_reorder_phase(user, task.phase,
                               project_cache=_request_dict(user, _REORDER_CACHE_ATTR))
    _seat_project(task, user)
    return answer
