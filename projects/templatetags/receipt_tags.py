"""
FIN-M-B5-6 (J7, ruling Q11) — does this task follow Finance-confirmed receipts?

With CLIENT_RECEIPTS_LIVE on, a Residential site's three Finance confirmation tasks move
only when their milestone's confirmed receipts move them (receipts.follow_confirmation_task,
FIN-M-B5-5b), and every human status change on them is refused on the server (K4, FR41).
The task row on the project page and the status control on the task page therefore draw
no status dropdown for such a task — only its status as text, with "Follows Finance
confirmation" beside it — rather than a dropdown whose every choice is refused.

ONE RULE, NOT A SECOND COPY. The filter asks views._task_follows_receipts(), the predicate
the refusal sites themselves ask (switch, Residential, the confirmation-task name map), so
the page and the server can never disagree. It costs no query: one settings read, and the
project is already in the template's hands.

PRESENTATION ONLY. Deleting this filter would change no refusal.
"""
from django import template

# Not circular: views.py never imports a template-tag module.
from projects.views import _task_follows_receipts

register = template.Library()


@register.filter
def follows_receipts(task, project):
    """True when `task` (on `project`) follows confirmed receipts now. Usage:
    {% if task|follows_receipts:project %}. False for a missing task or project, so a
    template that lacks either draws what it drew before."""
    if task is None or not project:
        return False
    return _task_follows_receipts(task, project)
