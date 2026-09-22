"""Row-level template filter for "Duplicate for locations".

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
"""
from django import template

from ..permissions import can_duplicate_task_for_locations

register = template.Library()

_CACHE_ATTR = '_location_duplication_project_cache'


@register.filter
def can_duplicate_for_locations(task, user):
    cache = getattr(user, _CACHE_ATTR, None)
    if cache is None:
        cache = {}
        setattr(user, _CACHE_ATTR, cache)
    return can_duplicate_task_for_locations(user, task, project_cache=cache)
