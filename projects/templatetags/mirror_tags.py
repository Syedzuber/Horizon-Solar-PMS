"""
Mirror source text — what a Derived row says it follows (closeout 1b).

A mirror task (Task.is_mirror) never takes a status from a person: rung 0 of
`views._apply_task_status_change()` refuses every human write. The "Derived" chip on
the task row and on task detail says so, and its tooltip names WHERE the status comes
from. Before this module the tooltip was one generic sentence for all eight mirrors, so
a reader could not tell a Delivery row (it moves when a challan is received) from HOTO
(nothing writes it yet).

ONE MAPPING, DEFINED HERE ONCE. Both templates read it through the `mirror_source`
filter; neither names a template code. Keyed by `template_task.code`, never by
task_name — the label is content and can be reworded by a template version; the code
is what the mirror writers themselves look rows up by.

The keys come from the constants the writers use where one exists:
DESIGN_MIRROR_CODE (sync_design_mirror), DC_CATEGORY_TO_MIRROR_CODE's values
(sync_delivery_mirrors) and COD_MIRROR_CODE (sync_cod_mirror, closeout step 4).
AS_BUILT_DRAWINGS and HOTO have no writer and therefore no constant anywhere, so they
are spelled out here and only here.

PRESENTATION ONLY. Nothing here decides whether a status may be written.
"""
from django import template

from projects.design_views import COD_MIRROR_CODE, DESIGN_MIRROR_CODE
from projects.models import DC_CATEGORY_TO_MIRROR_CODE

register = template.Library()

# Appended to every source sentence, the generic one included, so each tooltip says
# both where the status comes from and that this screen cannot change it.
MIRROR_CANNOT_SET = 'Its status cannot be set here.'

# For a mirror whose code is not below (a future mirror, or a row with no
# template_task). The previous generic tooltip, minus its "and cannot be set here",
# which MIRROR_CANNOT_SET now says for every code.
MIRROR_SOURCE_DEFAULT = ('Derived from the workspace that owns this work — its status '
                         'updates itself.')

_DELIVERY_SOURCE = 'Updates from delivery challans and GRN.'

MIRROR_SOURCE_TEXT = {
    DESIGN_MIRROR_CODE: 'Updates from the design workspace.',
    **{code: _DELIVERY_SOURCE for code in DC_CATEGORY_TO_MIRROR_CODE.values()},
    # Closeout step 4: the COD record (cod_views) drives this mirror through
    # sync_cod_mirror.
    COD_MIRROR_CODE:     'Updates from the COD record.',
    # "not built yet" is part of the text on purpose: these two sit at their seeded
    # status until their source records exist, and the tooltip must not imply a
    # derivation that does not run.
    'AS_BUILT_DRAWINGS': 'Will update from the As-Built record (not built yet).',
    'HOTO':              'Will update from Final Acceptance (not built yet).',
}


def mirror_source_text(code):
    """The full tooltip for a mirror with this template code: its source sentence (or
    the generic one) followed by MIRROR_CANNOT_SET."""
    return f'{MIRROR_SOURCE_TEXT.get(code, MIRROR_SOURCE_DEFAULT)} {MIRROR_CANNOT_SET}'


@register.filter
def mirror_source(task):
    """`{{ task|mirror_source }}` — the Derived chip's tooltip for this task.

    Reads task.template_task, which costs one query unless the caller select_related it
    (project_overview's prefetch does; task_detail does not, accepted for one row). A
    task with no template_task gets the generic text rather than an error."""
    code = task.template_task.code if task.template_task_id else None
    return mirror_source_text(code)
