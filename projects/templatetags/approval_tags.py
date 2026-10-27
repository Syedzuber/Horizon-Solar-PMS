"""Row-level template filter for the two-step completion label (2.1, G85).

    {% load approval_tags %}
    {{ self_certify_by_assignee|self_certify_for:task }}

NO RULE HERE (R-13). It reads the answer views._phase_list_two_step() computed for the
row's assignee -- permissions.user_may_self_certify(), asked once per distinct assignee
-- and nothing else. A filter only because a template cannot index a dict by a
variable key.
"""
from django import template

register = template.Library()


@register.filter
def self_certify_for(answers, task):
    """Would the viewer's submission of `task` complete it outright? `answers` is the
    builder's per-assignee mapping, which asks the predicate itself for an assignee it
    has not seen."""
    return answers[task.assigned_to_id]
