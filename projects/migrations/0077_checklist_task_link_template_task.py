# Prompt 2.4 - key ChecklistTaskLink by its template task instead of by a task's name.
#
# WHY THE STRING KEY HAD TO GO. A link matched (task_name, project_type) against
# Task.task_name. Both halves of that were broken:
#
#   - Rewording a template task's label detached its checklist silently. A label is
#     version CONTENT and moves when a new template version is authored (R-7); an
#     assignment is not content and must not move with it. Nothing warned; the checklist
#     simply stopped appearing on the task.
#   - The authoring picker offered Residential names only, so an OPEX link could not be
#     created through the product at all. That is why production holds no links and this
#     database holds exactly one.
#
# WHAT THIS MIGRATION DOES, AND WHAT IT REFUSES TO DO. It adds the FK, then resolves each
# existing link by looking its task_name up as a LABEL in the currently active template
# for that project type. A row whose name matches nothing is LEFT NULL and PRINTED. It is
# not guessed at, not fuzzy-matched and not deleted: an unresolvable name is a fact about
# the data - a checklist assigned to a task that the live template no longer contains -
# and the honest record of it is a null FK next to the string that could not be found.
#
# EXPECTED HERE: exactly one row, 'SLD' (Residential), which predates the current
# Residential template and has no label in it. Its strings stay intact, so it still
# resolves through the string fallback in `_checklist_for_task()` for as long as any task
# is named 'SLD'; it simply is not on the new path. Production is empty, so this
# migration is a no-op there.
#
# LABELS ARE UNIQUE WITHIN EACH ACTIVE TEMPLATE (verified: 52 Residential, 23 OPEX, no
# duplicates in either), so a label resolves to at most one row and the lookup below
# needs no tie-break. It does not silently assume that: building the label map raises if
# it ever finds two tasks sharing a label, because the alternative is a dict that keeps
# whichever row it saw last and a checklist quietly attached to the wrong task.
#
# REVERSE drops the column, which drops the backfill with it. Nothing else to undo: the
# string columns are untouched by this migration in both directions.
import django.db.models.deletion
from django.db import migrations, models


def _active_labels_to_task(apps, project_type):
    """{label: TaskTemplateTask} for the active template of one project type.

    Historical models, so this walks the relations by hand rather than through
    projects.utils.resolve_active_task_template(), which returns concrete ones.
    """
    TaskTemplate     = apps.get_model('projects', 'TaskTemplate')
    TaskTemplateTask = apps.get_model('projects', 'TaskTemplateTask')

    template = TaskTemplate.objects.filter(
        project_type=project_type, status='active',
    ).first()
    if template is None:
        return {}

    mapping = {}
    duplicates = set()
    for tt in TaskTemplateTask.objects.filter(phase__template=template):
        if tt.label in mapping:
            duplicates.add(tt.label)
        mapping[tt.label] = tt
    if duplicates:
        raise RuntimeError(
            f"[0077] {project_type} template {template.code} v{template.version_no} has "
            f"duplicate task labels {sorted(duplicates)}; a label cannot be resolved to "
            f"one task. Refusing to guess - fix the template, then re-run."
        )
    return mapping


def backfill_template_task(apps, schema_editor):
    ChecklistTaskLink = apps.get_model('projects', 'ChecklistTaskLink')

    links = list(ChecklistTaskLink.objects.all())
    if not links:
        print("  [0077] No checklist task links to backfill.")
        return

    by_type = {}
    matched, unmatched = 0, []
    for link in links:
        if link.project_type not in by_type:
            by_type[link.project_type] = _active_labels_to_task(apps, link.project_type)
        tt = by_type[link.project_type].get(link.task_name)
        if tt is None:
            unmatched.append(link)
            continue
        # update() rather than save(): the concrete model's save() derives the strings
        # from the FK, and a historical model has no such method anyway. There is nothing
        # to derive here - the strings are exactly what we matched ON.
        ChecklistTaskLink.objects.filter(pk=link.pk).update(template_task=tt)
        matched += 1

    print(f"  [0077] Checklist task links: {matched} matched to a template task, "
          f"{len(unmatched)} left null.")
    for link in unmatched:
        # Printed one per line, plainly, because this is the migration's only report of
        # a row it could not key and the next person needs the name to look it up.
        print(f"  [0077] UNMATCHED - no task labelled '{link.task_name}' in the active "
              f"{link.project_type} template; template_task left null "
              f"(link pk={link.pk}, checklist_id={link.checklist_id}).")


def noop_reverse(apps, schema_editor):
    """The column is dropped by the reverse of AddField below, taking every value with
    it. Nothing to undo by hand."""
    return None


class Migration(migrations.Migration):

    dependencies = [
        ('projects', '0076_two_step_task_completion'),
    ]

    operations = [
        migrations.AddField(
            model_name='checklisttasklink',
            name='template_task',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='checklist_links', to='projects.tasktemplatetask'),
        ),
        # Between the field and its constraint: the constraint is UNIQUE on the new
        # column, so the backfill has to run before it is enforced - and if the backfill
        # ever produced two links on one template task, this is where that surfaces.
        migrations.RunPython(backfill_template_task, noop_reverse),
        migrations.AddConstraint(
            model_name='checklisttasklink',
            constraint=models.UniqueConstraint(condition=models.Q(('template_task__isnull', False)), fields=('template_task',), name='uniq_checklist_task_link_template_task'),
        ),
    ]
