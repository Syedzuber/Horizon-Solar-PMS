"""Prompt 1.3a — the OPEX task template as data, and the `is_mirror` column.

WHAT THIS SUITE DOES NOT TEST, because 1.3a does not build it: nothing attaches this
template to a project, and nothing reads `is_mirror`. The human-write refusal lives in
`_apply_task_status_change()` and is prompt 1.3c's; the exclusion of mirrors from
overdue and workload counters is prompt 1.3b's. A test here that asserted either would
be asserting a thing that does not exist.

HOW THE FIXTURE GETS ITS DATA. `solarpms.test_settings` disables migrations and builds
the schema straight from model state, so migration 0075 NEVER RUNS under test. The base
class below therefore calls 0075's own `seed_opex_v1()` directly — the same function the
migration calls, with the concrete model classes standing in for the historical ones.
That is stricter than reading migration state would be: it exercises the seed, the
draft-then-activate order and the idempotency guard on every run. Residential is
bootstrapped the way the rest of the suite gets it, through
`resolve_residential_template()`.

EXPECTED_PHASES below is a SECOND, INDEPENDENT TRANSCRIPTION of
docs/OPEX_task_template_spec.md v1.5 §3. Migration 0075 holds the first. Two
transcriptions is the whole point — a typo in one fails against the other, where a test
that imported the migration's own data would agree with any typo it contained.
"""
from importlib import import_module

from django.test import TestCase

from .models import (
    Project, ProjectPhase, Task, TaskTemplate, TaskTemplatePhase,
    TaskTemplateTask, UserProfile,
)
from .utils import resolve_residential_template

OPEX_CODE        = 'OPEX'
RESIDENTIAL_CODE = 'RESIDENTIAL'

# (phase_name, phase_order, [(task_order, task_name, assigned_role, is_mirror), ...])
EXPECTED_PHASES = [
    ('Design', 1, [
        (1, 'Design', 'Design', True),
    ]),
    ('Approvals (Pre-Installation)', 2, [
        (1, 'Net Metering Approval', 'PM', False),
        (2, 'CEIG Approval',         'PM', False),
    ]),
    # Spec v1.5 §3: the two inspections are GONE (a consignment inspection is not a
    # site task — phase 4.5 owns it) and Material Delivery split into four. This phase
    # now holds only mirrors, which is why it can never be a current phase under R-21.
    ('Procurement & Delivery', 3, [
        (1, 'Delivery — Solar Panels', 'SCM', True),
        (2, 'Delivery — Inverters',    'SCM', True),
        (3, 'Delivery — BOS Kit',      'SCM', True),
        (4, 'Delivery — MMS',          'SCM', True),
    ]),
    ('Installation', 4, [
        (1, 'Civil Work and MMS Installation',     'Site Engineer', False),
        (2, 'Module Installation',                 'Site Engineer', False),
        (3, 'LA and Earthing Installation',        'Site Engineer', False),
        (4, 'DC Cable Laying with Conduit',        'Site Engineer', False),
        (5, 'DCDB and ACDB Installation',          'Site Engineer', False),
        (6, 'Inverter Installation',               'Site Engineer', False),
        (7, 'AC Cable Laying',                     'Site Engineer', False),
        (8, 'RMS Installation',                    'Site Engineer', False),
        (9, 'Solar Generation Meter Installation', 'Site Engineer', False),
    ]),
    ('Testing & Commissioning', 5, [
        (1, 'Testing & Commissioning', 'Site Engineer', False),
        (2, 'Net Meter Installation',  'Site Engineer', False),
    ]),
    ('Approvals (Post-Installation)', 6, [
        (1, 'Post-Installation Approvals', 'PM', False),
    ]),
    ('Closeout', 7, [
        (1, 'COD',                                 'PM',                  True),
        (2, 'Completion Certificates (Paperwork)', 'Project Coordinator', False),
        (3, 'As-Built Drawings',                   'Design',              True),
        (4, 'HOTO',                                'PM',                  True),
    ]),
]

# The eight the spec names. Asserted BY NAME, not by count — a count of 8 passes
# perfectly well when the wrong eight rows are flagged.
EXPECTED_MIRROR_NAMES = {
    'Design',
    # Material Delivery split into these four in spec v1.4 (kept by v1.5), reversing
    # v1.1's collapse to one. All four read Not Started until B-18 and SCM's catalogue
    # mapping both land — neither alone is sufficient.
    'Delivery — Solar Panels',
    'Delivery — Inverters',
    'Delivery — BOS Kit',
    'Delivery — MMS',
    'COD',
    'As-Built Drawings',
    'HOTO',
}

VALID_OPEX_ROLES = {'PM', 'Site Engineer', 'SCM', 'Design', 'Project Coordinator'}


# --- migration access ------------------------------------------------------------

def _seed_functions():
    """0075's seed and reverse. Imported by module name rather than through the
    migration loader, which test_settings disables."""
    module = import_module('projects.migrations.0075_seed_opex_template_v1')
    return module.seed_opex_v1, module.unseed_opex_v1


class _ConcreteApps:
    """Stands in for the `apps` registry a RunPython function is handed.

    The seed and reverse only ever call apps.get_model(), and the concrete classes
    answer every query the historical ones do. Passing the concrete classes is also
    what makes the R-7 draft guard real here: the historical classes carry no save()
    override, so a migration-state test could not catch a seed that wrote to an
    already-active template, and this one can.
    """

    _MODELS = {
        'TaskTemplate':      TaskTemplate,
        'TaskTemplatePhase': TaskTemplatePhase,
        'TaskTemplateTask':  TaskTemplateTask,
        'Task':              Task,
    }

    def get_model(self, app_label, model_name):
        assert app_label == 'projects'
        return self._MODELS[model_name]


def _seed_opex():
    seed, _unseed = _seed_functions()
    seed(_ConcreteApps(), None)


def _unseed_opex():
    _seed, unseed = _seed_functions()
    unseed(_ConcreteApps(), None)


def _opex_v1():
    return TaskTemplate.objects.get(code=OPEX_CODE, version_no=1)


class OpexTemplateBase(TestCase):
    """Both templates present, seeded the way production got them."""

    @classmethod
    def setUpTestData(cls):
        resolve_residential_template()   # bootstraps RESIDENTIAL v1 on a virgin DB
        _seed_opex()


# --- Task 3.1 – 3.4 --------------------------------------------------------------

class OpexTemplateShapeTests(OpexTemplateBase):
    """The seeded template matches docs/OPEX_task_template_spec.md v1.5, row for row."""

    def test_opex_v1_exists_and_is_active(self):
        template = _opex_v1()
        self.assertEqual(template.status, TaskTemplate.ACTIVE)
        self.assertEqual(template.project_type, 'OPEX')
        self.assertEqual(template.label, 'OPEX Execution')
        self.assertIsNotNone(
            template.effective_from,
            'activate() stamps effective_from; a null one would mean the row was '
            'created active instead of being flipped from draft.')

    def test_exactly_seven_phases_and_twenty_three_tasks(self):
        template = _opex_v1()
        self.assertEqual(
            TaskTemplatePhase.objects.filter(template=template).count(), 7)
        self.assertEqual(
            TaskTemplateTask.objects.filter(phase__template=template).count(), 23)

    def test_every_phase_and_task_matches_the_spec(self):
        """Names, order, roles and mirror flags — the whole table, in order."""
        template = _opex_v1()
        phases = list(
            TaskTemplatePhase.objects.filter(template=template).order_by('sort_order'))

        self.assertEqual(
            [(p.label, p.sort_order) for p in phases],
            [(name, order) for name, order, _ in EXPECTED_PHASES])

        for phase, (name, _order, expected_tasks) in zip(phases, EXPECTED_PHASES):
            with self.subTest(phase=name):
                actual = [
                    (t.sort_order, t.label, t.assigned_role, t.is_mirror)
                    for t in TaskTemplateTask.objects.filter(
                        phase=phase).order_by('sort_order')
                ]
                self.assertEqual(actual, expected_tasks)

    def test_every_assigned_role_is_a_storable_choice(self):
        """Task 3.2, in its stronger form: not merely a legal ROLE_CHOICES value but one
        of the five this template is allowed to use. An unrepresentable role does not
        raise on the way in — assigned_role is non-null with default PM, so a bad value
        becomes a silent PM. That is what this catches."""
        legal = {value for value, _ in Task.ROLE_CHOICES}
        self.assertTrue(
            VALID_OPEX_ROLES.issubset(legal),
            f'not storable as Task.assigned_role: {VALID_OPEX_ROLES - legal}')

        for task in TaskTemplateTask.objects.filter(phase__template=_opex_v1()):
            with self.subTest(task=task.label):
                self.assertIn(task.assigned_role, VALID_OPEX_ROLES)

    def test_exactly_the_eight_named_tasks_are_mirrors(self):
        """Task 3.3. By name, and both directions — nothing missing, nothing extra."""
        tasks = TaskTemplateTask.objects.filter(phase__template=_opex_v1())

        self.assertEqual({t.label for t in tasks if t.is_mirror}, EXPECTED_MIRROR_NAMES)
        self.assertEqual(
            {t.label for t in tasks if not t.is_mirror},
            {t.label for t in tasks} - EXPECTED_MIRROR_NAMES)
        self.assertEqual(tasks.filter(is_mirror=True).count(), 8)

    def test_every_mirror_carries_an_owning_role(self):
        """Task 3.4, and the A-1.3 audit's trap behind it: both status views refuse an
        unassigned task BEFORE _apply_task_status_change() runs, so a mirror with no
        owner would make 1.3c's refusal test pass without proving the refusal exists."""
        mirrors = TaskTemplateTask.objects.filter(
            phase__template=_opex_v1(), is_mirror=True)
        self.assertEqual(mirrors.count(), 8, 'fixture sanity')

        for task in mirrors:
            with self.subTest(task=task.label):
                self.assertIn(task.assigned_role, VALID_OPEX_ROLES)
                self.assertTrue(task.assigned_role.strip())

    def test_all_twenty_two_tasks_are_internal(self):
        """Spec §5: 'Task type | All 22 are Internal.'"""
        self.assertEqual(
            TaskTemplateTask.objects.filter(phase__template=_opex_v1())
            .exclude(task_type=Task.INTERNAL).count(), 0)

    def test_durations_are_left_at_the_field_default(self):
        """Spec §5: 'Durations | Unset in v1. The team decides per task later.' Pinned
        so real durations get set by editing the template, not by accident.

        The consequence, recorded here because it is 1.3c's to solve: all 22 are
        Internal and calculate_due_dates() chains Internal tasks one day at a time, so
        a site that ran that function would read as a 22-day project."""
        durations = set(
            TaskTemplateTask.objects.filter(phase__template=_opex_v1())
            .values_list('duration_days', flat=True))
        self.assertEqual(durations, {1})

    def test_no_opex_task_is_a_payment_milestone(self):
        """Residential payment milestones do not apply to tenders (spec §2a)."""
        self.assertEqual(
            TaskTemplateTask.objects.filter(
                phase__template=_opex_v1(), is_payment_milestone=True).count(), 0)


# --- Task 3.5 --------------------------------------------------------------------

class ResidentialUnchangedTests(OpexTemplateBase):
    """The already-active Residential template survived the new column."""

    def test_residential_v1_still_active_with_52_tasks(self):
        template = TaskTemplate.objects.get(code=RESIDENTIAL_CODE, version_no=1)
        self.assertEqual(template.status, TaskTemplate.ACTIVE)
        self.assertEqual(
            TaskTemplatePhase.objects.filter(template=template).count(), 9)
        self.assertEqual(
            TaskTemplateTask.objects.filter(phase__template=template).count(), 52)

    def test_no_residential_task_is_flagged_a_mirror(self):
        """The field default reached all 52 rows and left every one of them False."""
        self.assertEqual(
            TaskTemplateTask.objects.filter(
                phase__template__code=RESIDENTIAL_CODE, is_mirror=True).count(), 0)

    def test_the_two_templates_are_both_active_and_independent(self):
        """The partial unique constraint is per CODE, so two codes may both be active."""
        active = set(
            TaskTemplate.objects.filter(
                status=TaskTemplate.ACTIVE).values_list('code', flat=True))
        self.assertEqual(active, {RESIDENTIAL_CODE, OPEX_CODE})


# --- Task 3.6 --------------------------------------------------------------------

class SeedIdempotencyTests(OpexTemplateBase):
    """Running the seed twice produces no duplicate rows."""

    def test_running_the_seed_again_creates_nothing(self):
        before = (
            TaskTemplate.objects.count(),
            TaskTemplatePhase.objects.count(),
            TaskTemplateTask.objects.count(),
        )

        _seed_opex()   # the guard sees OPEX already present and returns

        self.assertEqual(
            (TaskTemplate.objects.count(),
             TaskTemplatePhase.objects.count(),
             TaskTemplateTask.objects.count()),
            before)
        self.assertEqual(
            TaskTemplate.objects.filter(code=OPEX_CODE).count(), 1,
            'a second OPEX template version must not be created')

    def test_the_guard_matches_on_code_not_on_version(self):
        """0075 skips when ANY version of OPEX exists, so a future v2 is never
        overwritten by a re-run of this migration on an older database."""
        _opex_v1()
        TaskTemplate.objects.filter(code=OPEX_CODE).update(version_no=2)

        _seed_opex()

        self.assertEqual(TaskTemplate.objects.filter(code=OPEX_CODE).count(), 1)
        self.assertFalse(
            TaskTemplate.objects.filter(code=OPEX_CODE, version_no=1).exists())


# --- Task 3.7 --------------------------------------------------------------------

class ReverseTests(OpexTemplateBase):
    """The migration's reverse removes the OPEX template and leaves Residential intact."""

    def test_reverse_drops_opex_and_keeps_residential(self):
        _unseed_opex()

        self.assertFalse(TaskTemplate.objects.filter(code=OPEX_CODE).exists())
        self.assertEqual(
            TaskTemplatePhase.objects.filter(template__code=OPEX_CODE).count(), 0)
        self.assertEqual(
            TaskTemplateTask.objects.filter(
                phase__template__code=OPEX_CODE).count(), 0,
            'the delete must cascade to phases and tasks')

        residential = TaskTemplate.objects.get(code=RESIDENTIAL_CODE, version_no=1)
        self.assertEqual(residential.status, TaskTemplate.ACTIVE)
        self.assertEqual(
            TaskTemplateTask.objects.filter(phase__template=residential).count(), 52)

    def test_reverse_is_safe_to_run_twice(self):
        _unseed_opex()
        _unseed_opex()   # returns quietly when the template is already gone

        self.assertFalse(TaskTemplate.objects.filter(code=OPEX_CODE).exists())

    def test_forward_after_reverse_rebuilds_the_same_template(self):
        """The round trip the VERIFY step runs against Postgres, asserted in the suite."""
        def snapshot():
            return [
                (t.phase.label, t.sort_order, t.label, t.assigned_role,
                 t.task_type, t.duration_days, t.is_mirror)
                for t in TaskTemplateTask.objects
                .filter(phase__template__code=OPEX_CODE)
                .select_related('phase')
                .order_by('phase__sort_order', 'sort_order')
            ]

        before = snapshot()
        _unseed_opex()
        _seed_opex()

        self.assertEqual(snapshot(), before)


# --- Task 3.8 --------------------------------------------------------------------

class TaskIsMirrorDefaultTests(TestCase):
    """`is_mirror` on a Task created outside any template."""

    def test_a_hand_created_task_is_not_a_mirror(self):
        project = Project.objects.create(
            customer_name='Tender Site A',
            customer_phone='9876543210',
            site_address='1 Sun Road',
            city='Lucknow',
            project_type='OPEX',
            status='Draft',
            assigned_pm=_a_pm(),
        )
        phase = ProjectPhase.objects.create(
            project=project, phase_name='Ad hoc', phase_order=1)

        task = Task.objects.create(
            phase=phase, task_name='Typed in by hand', task_order=1,
            assigned_role=Task.PM, task_type=Task.INTERNAL)

        self.assertFalse(task.is_mirror)
        task.refresh_from_db()
        self.assertFalse(
            task.is_mirror, 'the column default must be False in the database too')
        self.assertIsNone(task.template_task)

    def test_project_coordinator_is_a_storable_task_role(self):
        """1.3a added it so the OPEX spec's jointly-owned paperwork task could be stored
        as one value. It was already a UserProfile.ROLE_CHOICES value, so the two
        vocabularies now agree on it and views.py's _PROFILE_TO_TASK_ROLE — which maps
        only BD — passes it through unchanged."""
        self.assertIn(
            Task.PROJECT_COORDINATOR, {value for value, _ in Task.ROLE_CHOICES})
        self.assertIn(
            'Project Coordinator', {value for value, _ in UserProfile.ROLE_CHOICES})
        self.assertLessEqual(
            len(Task.PROJECT_COORDINATOR),
            Task._meta.get_field('assigned_role').max_length)


def _a_pm():
    """A PM profile. A post_save signal on User already creates the UserProfile, so
    fetch and mutate rather than creating a second one — the helper shape used by
    tests_task_template.py and tests_residential_baseline.py."""
    from django.contrib.auth.models import User

    user = User.objects.create_user(username='opex_tpl_pm', password='pw12345')
    profile = user.profile
    profile.role = 'PM'
    profile.is_active = True
    profile.save()
    return profile
