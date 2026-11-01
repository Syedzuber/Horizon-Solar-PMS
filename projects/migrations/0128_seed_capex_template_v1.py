# CAPEX-1 — seed the CAPEX execution task template as CAPEX v1, a copy of the ACTIVE
# OPEX (RESCO) template.
#
# READ FROM THE DATABASE, NOT TRANSCRIBED. 0075 transcribes the OPEX spec into a literal;
# this migration does not repeat that literal. It copies whatever the active OPEX version
# holds at the moment it runs — phases, tasks, codes, order, roles, types and durations —
# so CAPEX starts as the RESCO template and a second transcription has no chance to drift
# from the first.
#
# THREE DEPARTURES FROM A STRAIGHT COPY, all decided for CAPEX-1 (Zuber, 01 Nov 2026):
#   - DESIGN and COD are ordinary manual tasks (is_mirror=False), names unchanged. Their
#     writers are OPEX-only — every design view 404s a non-OPEX site (design_views
#     `_opex_site`) and COD recording refuses one (cod_views `record_refusal`) — so as
#     mirrors they could never move on a CAPEX site, and rung 0 would refuse a person too.
#   - No payment milestones: is_payment_milestone=False on every row. TaskTemplateTask has
#     no other milestone field, so that is the whole of "no milestone linkage".
#   - Every other mirror (the four deliveries, As-Built, HOTO) stays a mirror. The
#     delivery hooks carry no type check, so those four derive on CAPEX exactly as on OPEX.
#
# A SEPARATE TEMPLATE AND NOT A SECOND project_type ON OPEX's ROW, because
# TaskTemplate.project_type is one column and resolve_active_task_template() selects by
# it. The code is 'CAPEX', not 'OPEX': uniq_active_task_template_per_code allows one
# active version per code. Consequences, accepted: phase_rules.RULES is keyed ('OPEX', 1)
# and so does not apply to CAPEX, which is also what its OPEX-only early returns say.
#
# CHECKLIST LINKS POINT AT THE SAME Checklist ROWS. A link names its task by FK to a
# template task and is matched by that task's code plus the template's project_type
# (views._checklist_task_link_for, qaqc_views.resolve_checklists_for_tasks), so an OPEX
# link can never serve a CAPEX task — each needs its own link. Nothing on
# ChecklistTaskLink makes `checklist` unique, so the CAPEX link reuses the checklist and
# no item is copied. The (task_name, project_type) unique_together differs by type.
#
# WHAT THIS DOES NOT COVER. The seven OPEX links come from the management command
# seed_opex_installation_checklists, not from a migration. This copies the links that
# exist when it runs: a database where that command runs AFTER this migration gets OPEX
# links only, and the CAPEX tasks are linked by an Admin on the portal checklist screen.
#
# No dependency edges are copied: the OPEX template has none, and nothing materialises
# template edges at activation (task_dependencies.materialise_task_dependencies() has no
# caller).
from django.db import migrations
from django.utils import timezone

CAPEX_TEMPLATE_CODE  = 'CAPEX'
CAPEX_TEMPLATE_LABEL = 'CAPEX Execution'
CAPEX_VERSION_NO     = 1

# Template task codes that are mirrors on OPEX and manual tasks on CAPEX (see above).
MANUAL_ON_CAPEX = frozenset({'DESIGN', 'COD'})


def seed_capex_v1(apps, schema_editor):
    TaskTemplate      = apps.get_model('projects', 'TaskTemplate')
    TaskTemplatePhase = apps.get_model('projects', 'TaskTemplatePhase')
    TaskTemplateTask  = apps.get_model('projects', 'TaskTemplateTask')
    ChecklistTaskLink = apps.get_model('projects', 'ChecklistTaskLink')

    # Idempotent on either key: a template already carrying the CAPEX code, or one
    # already serving CAPEX projects under some other code, means CAPEX has a template
    # and this seed has nothing to add. A later v2 is never overwritten by a re-run.
    if TaskTemplate.objects.filter(code=CAPEX_TEMPLATE_CODE).exists() \
            or TaskTemplate.objects.filter(project_type='CAPEX').exists():
        print('  [0128] A CAPEX template is already present — skipping seed.')
        return

    # Fetches the one active OPEX version with its phases and tasks; there is at most one
    # (uniq_active_task_template_per_code on code 'OPEX', and 0075 is the only writer).
    source = (TaskTemplate.objects
              .filter(project_type='OPEX', status='active')
              .order_by('code', '-version_no')
              .first())
    if source is None:
        # A fresh database whose 0075 found nothing to seed, or one where OPEX was
        # removed. Nothing to copy is not a failure.
        print('  [0128] No active OPEX template — nothing to copy, skipping seed.')
        return

    # Created as a DRAFT and flipped active at the end, never created active — the order
    # the R-7 save() guards on the concrete models permit, so tests can run this same
    # function against concrete classes (see tests_capex_template.py).
    template = TaskTemplate.objects.create(
        code=CAPEX_TEMPLATE_CODE,
        label=CAPEX_TEMPLATE_LABEL,
        project_type='CAPEX',          # Project.project_type vocabulary
        version_no=CAPEX_VERSION_NO,
        status='draft',
        effective_from=timezone.now().date(),   # the day CAPEX got it, not OPEX's date
        created_by=None,
    )

    # OPEX template task pk -> its CAPEX copy, for re-pointing the checklist links.
    copy_of = {}
    for src_phase in TaskTemplatePhase.objects.filter(template=source).order_by('sort_order', 'pk'):
        phase = TaskTemplatePhase.objects.create(
            template=template,
            code=src_phase.code,
            label=src_phase.label,
            sort_order=src_phase.sort_order,
        )
        for src_task in TaskTemplateTask.objects.filter(phase=src_phase).order_by('sort_order', 'pk'):
            copy_of[src_task.pk] = TaskTemplateTask.objects.create(
                phase=phase,
                code=src_task.code,
                label=src_task.label,
                sort_order=src_task.sort_order,
                assigned_role=src_task.assigned_role,
                task_type=src_task.task_type,
                duration_days=src_task.duration_days,
                is_payment_milestone=False,
                is_mirror=src_task.is_mirror and src_task.code not in MANUAL_ON_CAPEX,
            )

    # task_name and project_type are set HERE, not left to ChecklistTaskLink.save(): a
    # historical model carries no custom save(), so nothing would derive them.
    links = 0
    for link in ChecklistTaskLink.objects.filter(template_task_id__in=list(copy_of)):
        target = copy_of[link.template_task_id]
        ChecklistTaskLink.objects.create(
            checklist_id=link.checklist_id,
            template_task=target,
            task_name=target.label,
            project_type='CAPEX',
        )
        links += 1

    # filter().update() rather than save(): matches seed_task_template_version(). There
    # is no other CAPEX-coded version to archive (checked at the top), and the partial
    # unique constraint adjudicates if a concurrent run ever got this far too.
    TaskTemplate.objects.filter(pk=template.pk).update(status='active')

    n_mirrors = sum(1 for t in copy_of.values() if t.is_mirror)
    print(f'  [0128] Seeded {CAPEX_TEMPLATE_CODE} v{CAPEX_VERSION_NO} (active) from '
          f'{source.code} v{source.version_no}: {len(copy_of)} tasks, {n_mirrors} mirrors, '
          f'{links} checklist links.')


def unseed_capex_v1(apps, schema_editor):
    """Reverse: drop CAPEX v1 and the checklist links pointing at it, nothing else.

    REFUSES once any project task was built from it. Task.template_task is SET_NULL, so
    deleting would strip live CAPEX tasks of their provenance and, with it, their
    checklists — the link lookup joins through template_task.
    """
    TaskTemplate      = apps.get_model('projects', 'TaskTemplate')
    Task              = apps.get_model('projects', 'Task')
    ChecklistTaskLink = apps.get_model('projects', 'ChecklistTaskLink')

    template = TaskTemplate.objects.filter(
        code=CAPEX_TEMPLATE_CODE, version_no=CAPEX_VERSION_NO).first()
    if template is None:
        return

    in_use = Task.objects.filter(template_task__phase__template=template).count()
    if in_use:
        raise RuntimeError(
            f'[0128] Cannot reverse: {in_use} project task(s) were built from '
            f'{CAPEX_TEMPLATE_CODE} v{CAPEX_VERSION_NO}. Deleting it would orphan them.'
        )

    # Links first: their FK is SET_NULL, so the template's cascade would leave them
    # behind as unkeyed rows rather than remove them.
    ChecklistTaskLink.objects.filter(template_task__phase__template=template).delete()
    template.delete()   # cascades to its phases and tasks


class Migration(migrations.Migration):

    dependencies = [
        ('projects', '0127_attendance_mapping_confirmed'),
    ]

    operations = [
        migrations.RunPython(seed_capex_v1, unseed_capex_v1),
    ]
