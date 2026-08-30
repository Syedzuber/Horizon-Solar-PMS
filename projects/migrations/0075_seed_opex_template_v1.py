# Prompt 1.3a — seed the OPEX execution task template as OPEX v1.
#
# The template exists as versioned data and NOTHING ATTACHES IT. `project_activate`
# still refuses OPEX sites, still mints Residential milestones, and still resolves only
# the RESIDENTIAL template; prompt 1.3c changes all three. Until then this is 7 phases
# and 22 rows nothing reads. `is_mirror` is likewise inert — the human-write refusal is
# 1.3c's (in `_apply_task_status_change()`) and the counter exclusion is 1.3b's.
#
# Transcribed verbatim from docs/OPEX_task_template_spec.md v1.2 §3 — same names, same
# order, same owning roles, is_mirror per the M/E column. projects/tests_opex_template.py
# holds a SECOND, INDEPENDENT transcription of the same table and asserts the seeded rows
# against it, so a typo here fails a test rather than shipping.
#
# This migration imports from projects.utils for the same reason 0067 does, and with the
# same safety: seed_task_template_version() takes its model classes as arguments, so it
# operates on the HISTORICAL models handed over below and never on the concrete ones.
#
# The phase/task data lives HERE rather than beside build_residential_phases() in utils.
# That is not a departure from 0067 but the point of it: 0067's header says
# build_residential_phases() "IS the thing being migrated, and after this runs it stops
# being executed at runtime and stays only as the seed this migration read." There is no
# pre-existing OPEX builder to migrate out of runtime source, and nothing at runtime
# needs one — so the seed belongs in the migration that reads it, once.
from django.db import migrations

from projects.utils import seed_task_template_version

OPEX_TEMPLATE_CODE  = 'OPEX'
OPEX_TEMPLATE_LABEL = 'OPEX Execution'

# Durations are UNSET IN V1 by decision (spec §5, "Durations | Unset in v1. The team
# decides per task later."), so every task takes the duration_days field default of 1.
#
# WHAT THAT PRODUCES, stated because it is a real hazard and 1.3c owns it:
# calculate_due_dates() chains INTERNAL tasks sequentially off activated_at, and all 22
# of these are Internal — so if 1.3c calls it, the last task (HOTO) falls due
# activated_at + 22 calendar days and every OPEX site reads as a 22-day project, going
# overdue en masse within a month. Nothing calls it for OPEX today. See
# EXECUTION_MODULE_DEFERRED.md §B.
OPEX_DEFAULT_DURATION_DAYS = 1


def build_opex_phases():
    """The OPEX template, in build_residential_phases() shape.

    Roles use Task.ROLE_CHOICES values as literal strings — a historical model class
    carries no constants, and this function is called from a migration.
    """
    return [
        {
            'phase_name': 'Design',
            'phase_order': 1,
            'tasks': [
                # DesignAssignment.status. Not Started until allocated · In Progress from
                # allocation · Done at DESIGN_RELEASED · returns to In Progress on reopen.
                # The 11 drawing types stay in the design workspace and are not tasks.
                {'task_order': 1, 'task_name': 'Design', 'assigned_role': 'Design', 'task_type': 'Internal', 'is_mirror': True},
            ],
        },
        {
            'phase_name': 'Approvals (Pre-Installation)',
            'phase_order': 2,
            'tasks': [
                # Both entered: no statutory approval record exists until phase 5.1,
                # when these convert to mirrors in a later template version.
                {'task_order': 1, 'task_name': 'Net Metering Approval', 'assigned_role': 'PM', 'task_type': 'Internal'},
                {'task_order': 2, 'task_name': 'CEIG Approval',         'assigned_role': 'PM', 'task_type': 'Internal'},
            ],
        },
        {
            'phase_name': 'Procurement & Delivery',
            'phase_order': 3,
            'tasks': [
                # Both inspections entered; no inspection record until phase 4.5.
                {'task_order': 1, 'task_name': 'Inspection — Factory / Vendor',           'assigned_role': 'SCM', 'task_type': 'Internal'},
                {'task_order': 2, 'task_name': 'Inspection — Post-Delivery / Unloading',  'assigned_role': 'SCM', 'task_type': 'Internal'},
                # ONE task covering all materials. DCLineItem accepted quantity against
                # the site BOQ via the BOQItem FK (B-18): Not Started = none accepted ·
                # In Progress = some, below BOQ · Done = accepted >= BOQ. Damaged
                # excluded. Reads Not Started until B-18 lands, deliberately. Expands to
                # six once SCM maps all 207 OPEX catalogue items to buckets — 120 map to
                # none today and no category is named RMS.
                {'task_order': 3, 'task_name': 'Material Delivery',                        'assigned_role': 'SCM', 'task_type': 'Internal', 'is_mirror': True},
            ],
        },
        {
            'phase_name': 'Installation',
            'phase_order': 4,
            # All entered, all Site Engineer. The spec's "(PM / Coordinator as
            # applicable)" is prose about who does the work in practice; the stored
            # role is Site Engineer.
            'tasks': [
                {'task_order': 1, 'task_name': 'Civil Work and MMS Installation',     'assigned_role': 'Site Engineer', 'task_type': 'Internal'},
                {'task_order': 2, 'task_name': 'Module Installation',                 'assigned_role': 'Site Engineer', 'task_type': 'Internal'},
                {'task_order': 3, 'task_name': 'LA and Earthing Installation',        'assigned_role': 'Site Engineer', 'task_type': 'Internal'},
                {'task_order': 4, 'task_name': 'DC Cable Laying with Conduit',        'assigned_role': 'Site Engineer', 'task_type': 'Internal'},
                {'task_order': 5, 'task_name': 'DCDB and ACDB Installation',          'assigned_role': 'Site Engineer', 'task_type': 'Internal'},
                {'task_order': 6, 'task_name': 'Inverter Installation',               'assigned_role': 'Site Engineer', 'task_type': 'Internal'},
                {'task_order': 7, 'task_name': 'AC Cable Laying',                     'assigned_role': 'Site Engineer', 'task_type': 'Internal'},
                {'task_order': 8, 'task_name': 'RMS Installation',                    'assigned_role': 'Site Engineer', 'task_type': 'Internal'},
                {'task_order': 9, 'task_name': 'Solar Generation Meter Installation', 'assigned_role': 'Site Engineer', 'task_type': 'Internal'},
            ],
        },
        {
            'phase_name': 'Testing & Commissioning',
            'phase_order': 5,
            # Punch Points was a mirror in spec v1.0 and is DROPPED: `Issue` exists per
            # site, but "punch point" and "blocking" do not, and the Blocked branch of
            # _apply_task_status_change() auto-creates Issue rows — so the mirror would
            # conflate task blockers with the commissioning punch list. Returns at
            # phase 2.3. Do not re-add it here.
            'tasks': [
                {'task_order': 1, 'task_name': 'Testing & Commissioning', 'assigned_role': 'Site Engineer', 'task_type': 'Internal'},
                # The physical install. Distinct from the Phase 2 approval — locked.
                {'task_order': 2, 'task_name': 'Net Meter Installation',  'assigned_role': 'Site Engineer', 'task_type': 'Internal'},
            ],
        },
        {
            'phase_name': 'Approvals (Post-Installation)',
            'phase_order': 6,
            'tasks': [
                # One task covering a dynamic set of underlying approvals. Converts to a
                # mirror at phase 5.1, in the same version as Phase 2's two.
                {'task_order': 1, 'task_name': 'Post-Installation Approvals', 'assigned_role': 'PM', 'task_type': 'Internal'},
            ],
        },
        {
            'phase_name': 'Closeout',
            'phase_order': 7,
            'tasks': [
                # Milestone / COD record (phase 5.3). No source object exists today.
                {'task_order': 1, 'task_name': 'COD', 'assigned_role': 'PM', 'task_type': 'Internal', 'is_mirror': True},
                # Jointly owned by PM and Coordinator per the spec. assigned_role holds
                # ONE value, so 1.3a added 'Project Coordinator' to Task.ROLE_CHOICES —
                # a value UserProfile already had — rather than storing an
                # unrepresentable composite or silently defaulting to PM.
                {'task_order': 2, 'task_name': 'Completion Certificates (Paperwork)', 'assigned_role': 'Project Coordinator', 'task_type': 'Internal'},
                # Design workspace. Post-commissioning, so Closeout rather than Phase 1
                # despite appearing under Design in the source list. Blocks HOTO.
                {'task_order': 3, 'task_name': 'As-Built Drawings', 'assigned_role': 'Design', 'task_type': 'Internal', 'is_mirror': True},
                # Phase 5.3. No source object exists today.
                {'task_order': 4, 'task_name': 'HOTO', 'assigned_role': 'PM', 'task_type': 'Internal', 'is_mirror': True},
            ],
        },
    ]


def seed_opex_v1(apps, schema_editor):
    TaskTemplate      = apps.get_model('projects', 'TaskTemplate')
    TaskTemplatePhase = apps.get_model('projects', 'TaskTemplatePhase')
    TaskTemplateTask  = apps.get_model('projects', 'TaskTemplateTask')

    # Idempotent, matching 0067: a database that already carries an OPEX template — by
    # any version — is left exactly as it is. Matched on code alone, not on
    # (code, version_no), so a later v2 is never overwritten by a re-run of this.
    if TaskTemplate.objects.filter(code=OPEX_TEMPLATE_CODE).exists():
        print(f'  [0075] {OPEX_TEMPLATE_CODE} template already present — skipping seed.')
        return

    # Created as a DRAFT and activated at the end, inside the helper and in that order.
    # 0.5's checklist fixtures failed by adding items to an already-active parent and
    # prompt 1.0 existed only to repair them; the R-7 save() guard permits no other
    # order on the concrete models.
    template = seed_task_template_version(
        template_model=TaskTemplate,
        phase_model=TaskTemplatePhase,
        task_model=TaskTemplateTask,
        code=OPEX_TEMPLATE_CODE,
        label=OPEX_TEMPLATE_LABEL,
        project_type='OPEX',            # Project.project_type vocabulary
        version_no=1,
        phases=build_opex_phases(),
        duration_resolver=lambda name: OPEX_DEFAULT_DURATION_DAYS,
        created_by=None,
    )

    n_phases  = TaskTemplatePhase.objects.filter(template=template).count()
    n_tasks   = TaskTemplateTask.objects.filter(phase__template=template).count()
    n_mirrors = TaskTemplateTask.objects.filter(phase__template=template, is_mirror=True).count()
    print(f'  [0075] Seeded {OPEX_TEMPLATE_CODE} v1 (active): '
          f'{n_phases} phases, {n_tasks} tasks, {n_mirrors} mirrors.')

    # NO provenance backfill, unlike 0067. Nothing has ever attached a template to an
    # OPEX site, so there are no pre-existing OPEX tasks to give provenance to, and
    # matching Residential tasks by name would hand them provenance they do not have.


def unseed_opex_v1(apps, schema_editor):
    """Reverse: drop OPEX v1 and nothing else. RESIDENTIAL is not touched."""
    TaskTemplate = apps.get_model('projects', 'TaskTemplate')
    Task         = apps.get_model('projects', 'Task')

    template = TaskTemplate.objects.filter(
        code=OPEX_TEMPLATE_CODE, version_no=1).first()
    if template is None:
        return
    # Nothing attaches this template in 1.3a, so this should match zero rows today. It
    # is here because SET_NULL would do it on delete anyway and stating it keeps the
    # reverse correct once 1.3c starts creating tasks from these rows.
    Task.objects.filter(
        template_task__phase__template=template).update(template_task=None)
    template.delete()   # cascades to its phases and tasks


class Migration(migrations.Migration):

    dependencies = [
        ('projects', '0074_is_mirror_and_coordinator_role'),
    ]

    operations = [
        migrations.RunPython(seed_opex_v1, unseed_opex_v1),
    ]
