"""CAPEX-1 — the CAPEX task template (migration 0128) and CAPEX activation.

The test settings disable migrations, so every fixture seeds the way production does:
by calling the migrations' own functions — 0075's seed_opex_v1() for OPEX, then 0128's
seed_capex_v1() for CAPEX — against the concrete models. A hand-built CAPEX template
here would prove only the test's copy, not the migration's.

Activation is driven through the real view, opex_site_activate, because that is where
the Activate button on project_overview and the PM dashboard posts for every
non-Residential project.
"""
from decimal import Decimal
from importlib import import_module
from unittest.mock import patch

from django.contrib.auth.models import User
from django.contrib.messages import get_messages
from django.test import Client, TestCase
from django.urls import reverse

from .models import (
    Checklist, ChecklistItem, ChecklistTaskLink, Project, StatusTransition, Task,
    TaskTemplate, TaskTemplatePhase, TaskTemplateTask,
)
from .permissions import (
    can_duplicate_task_for_locations, can_rename_task, can_reorder_phase,
    user_can_mark_task_not_applicable,
)
from .phase_rules import SiteRuleState, phase_rule_warnings, start_warnings
from .qaqc_views import resolve_checklists_for_tasks
from .views import _checklist_for_task

OPEX_MIGRATION  = 'projects.migrations.0075_seed_opex_template_v1'
CAPEX_MIGRATION = 'projects.migrations.0128_seed_capex_template_v1'

# The eight OPEX mirrors as 0075 seeds them, and the two CAPEX-1 turns into manual tasks.
OPEX_MIRROR_CODES = {
    'DESIGN', 'DELIVERY_SOLAR_PANELS', 'DELIVERY_INVERTERS', 'DELIVERY_BOS_KIT',
    'DELIVERY_MMS', 'COD', 'AS_BUILT_DRAWINGS', 'HOTO',
}
CAPEX_MIRROR_CODES = OPEX_MIRROR_CODES - {'DESIGN', 'COD'}

CHECKLIST_TASK_CODE = 'CIVIL_WORK_AND_MMS_INSTALLATION'


class _ConcreteApps:
    """Stands in for the `apps` registry a RunPython function is handed. Same shape as
    tests_checklist_task_link.py's, plus the two models 0128 also asks for."""

    _MODELS = {
        'TaskTemplate':      TaskTemplate,
        'TaskTemplatePhase': TaskTemplatePhase,
        'TaskTemplateTask':  TaskTemplateTask,
        'Task':              Task,
        'ChecklistTaskLink': ChecklistTaskLink,
    }

    def get_model(self, app_label, model_name):
        assert app_label == 'projects'
        return self._MODELS[model_name]


def _seed_opex():
    import_module(OPEX_MIGRATION).seed_opex_v1(_ConcreteApps(), None)


def _seed_capex():
    import_module(CAPEX_MIGRATION).seed_capex_v1(_ConcreteApps(), None)


def _unseed_capex():
    import_module(CAPEX_MIGRATION).unseed_capex_v1(_ConcreteApps(), None)


def _template_rows(code):
    """(phase code, task code, label, order, role, type, duration, milestone, mirror) for
    every task of the active template `code`, in template order."""
    return list(
        TaskTemplateTask.objects
        .filter(phase__template__code=code, phase__template__status=TaskTemplate.ACTIVE)
        .order_by('phase__sort_order', 'sort_order')
        .values_list('phase__code', 'code', 'label', 'sort_order', 'assigned_role',
                     'task_type', 'duration_days', 'is_payment_milestone', 'is_mirror')
    )


def _profile(username, role):
    """signals.py creates the profile on post_save, so this UPDATES the role."""
    user = User.objects.create_user(username=username, password='pw12345')
    profile = user.profile
    profile.role = role
    profile.is_active = True
    profile.save()
    return profile


def _project(project_type, pm, name):
    """A Draft, standalone project — no Program — owned by `pm`."""
    return Project.objects.create(
        customer_name=name, customer_phone='9876543210', site_address='1 Test Road',
        city='Lucknow', project_type=project_type, dc_capacity_kw=Decimal('50.00'),
        status='Draft', assigned_pm=pm,
    )


def _activate(pm, project):
    client = Client()
    client.force_login(pm.user)
    response = client.post(reverse('opex_site_activate', args=[project.project_id]))
    messages = [str(m) for m in get_messages(response.wsgi_request)]
    project.refresh_from_db()
    return response, messages


def _linked_checklist():
    """An active checklist linked to the OPEX Civil Work task — the shape
    seed_opex_installation_checklists leaves behind. Items are added while the checklist
    is a draft; ChecklistItem's R-7 guard refuses them on an active one."""
    checklist = Checklist.objects.create(code='CAPEX1-TEST', name='Civil Work checks')
    ChecklistItem.objects.create(checklist=checklist, label='Pit depth checked', order=1)
    checklist.activate()
    opex_task = TaskTemplateTask.objects.get(
        phase__template__code='OPEX', code=CHECKLIST_TASK_CODE)
    ChecklistTaskLink.objects.create(checklist=checklist, template_task=opex_task)
    return checklist


class CapexTemplateSeedTests(TestCase):
    """What 0128 writes, compared row for row against the OPEX template it copies."""

    @classmethod
    def setUpTestData(cls):
        _seed_opex()
        cls.checklist = _linked_checklist()
        cls.opex_before = _template_rows('OPEX')
        _seed_capex()

    def test_capex_template_is_active_and_typed_capex(self):
        template = TaskTemplate.objects.get(code='CAPEX')
        self.assertEqual(
            (template.label, template.project_type, template.version_no, template.status),
            ('CAPEX Execution', 'CAPEX', 1, TaskTemplate.ACTIVE))

    def test_same_phases_tasks_codes_order_roles_and_durations_as_opex(self):
        # Everything but the two flags 0128 deliberately changes.
        def shape(rows):
            return [row[:7] for row in rows]
        self.assertEqual(len(_template_rows('CAPEX')), 23)
        self.assertEqual(shape(_template_rows('CAPEX')), shape(_template_rows('OPEX')))
        self.assertEqual(
            list(TaskTemplatePhase.objects.filter(template__code='CAPEX')
                 .order_by('sort_order').values_list('code', 'label', 'sort_order')),
            list(TaskTemplatePhase.objects.filter(template__code='OPEX')
                 .order_by('sort_order').values_list('code', 'label', 'sort_order')))

    def test_design_and_cod_are_manual_other_mirrors_unchanged(self):
        mirrors = set(TaskTemplateTask.objects.filter(
            phase__template__code='CAPEX', is_mirror=True).values_list('code', flat=True))
        self.assertEqual(mirrors, CAPEX_MIRROR_CODES)
        manual = TaskTemplateTask.objects.filter(
            phase__template__code='CAPEX', code__in=['DESIGN', 'COD'])
        self.assertEqual({(t.code, t.label, t.is_mirror) for t in manual},
                         {('DESIGN', 'Design', False), ('COD', 'COD', False)})

    def test_no_capex_template_task_is_a_payment_milestone(self):
        self.assertFalse(TaskTemplateTask.objects.filter(
            phase__template__code='CAPEX', is_payment_milestone=True).exists())

    def test_opex_template_rows_are_untouched_by_the_seed(self):
        self.assertEqual(_template_rows('OPEX'), self.opex_before)
        self.assertEqual(set(TaskTemplateTask.objects.filter(
            phase__template__code='OPEX', is_mirror=True).values_list('code', flat=True)),
            OPEX_MIRROR_CODES)

    def test_checklist_link_duplicated_onto_the_capex_task_same_checklist(self):
        link = ChecklistTaskLink.objects.get(
            template_task__phase__template__code='CAPEX')
        self.assertEqual(
            (link.checklist_id, link.template_task.code, link.task_name, link.project_type),
            (self.checklist.pk, CHECKLIST_TASK_CODE, 'Civil Work and MMS Installation', 'CAPEX'))
        # The OPEX link is still there and still OPEX's; no checklist was copied.
        self.assertEqual(ChecklistTaskLink.objects.filter(checklist=self.checklist).count(), 2)
        self.assertEqual(Checklist.objects.count(), 1)

    def test_rerun_is_a_no_op(self):
        _seed_capex()
        self.assertEqual(TaskTemplate.objects.filter(code='CAPEX').count(), 1)
        self.assertEqual(TaskTemplateTask.objects.filter(phase__template__code='CAPEX').count(), 23)
        self.assertEqual(ChecklistTaskLink.objects.filter(project_type='CAPEX').count(), 1)


class CapexSeedEdgeTests(TestCase):
    """0128 on a database without an OPEX template, and its reverse."""

    def test_no_opex_template_seeds_nothing_and_does_not_fail(self):
        _seed_capex()
        self.assertFalse(TaskTemplate.objects.filter(project_type='CAPEX').exists())

    def test_reverse_removes_only_what_it_created(self):
        _seed_opex()
        checklist = _linked_checklist()
        opex_before = _template_rows('OPEX')
        _seed_capex()
        _unseed_capex()
        self.assertFalse(TaskTemplate.objects.filter(code='CAPEX').exists())
        self.assertFalse(ChecklistTaskLink.objects.filter(project_type='CAPEX').exists())
        self.assertEqual(_template_rows('OPEX'), opex_before)
        self.assertTrue(ChecklistTaskLink.objects.filter(
            checklist=checklist, project_type='OPEX').exists())

    def test_reverse_refuses_once_a_project_was_built_from_it(self):
        _seed_opex()
        _seed_capex()
        pm = _profile('capex_rev_pm', 'PM')
        _activate(pm, _project('CAPEX', pm, 'CAPEX Reverse Site'))
        with self.assertRaisesMessage(RuntimeError, 'Cannot reverse'):
            _unseed_capex()
        self.assertTrue(TaskTemplate.objects.filter(code='CAPEX').exists())


class CapexActivationTests(TestCase):
    """A Draft, standalone CAPEX project activated through opex_site_activate."""

    @classmethod
    def setUpTestData(cls):
        _seed_opex()
        cls.checklist = _linked_checklist()
        _seed_capex()
        cls.pm = _profile('capex_pm', 'PM')

    def setUp(self):
        self.project = _project('CAPEX', self.pm, 'CAPEX Standalone Site')
        self.response, self.messages = _activate(self.pm, self.project)
        self.tasks = {t.template_task.code: t for t in
                      Task.objects.filter(phase__project=self.project)
                      .select_related('template_task', 'phase')}

    def test_activates_with_the_full_capex_task_set_and_no_program(self):
        self.assertEqual(self.response.status_code, 302)
        self.assertIsNone(self.project.program_id)
        self.assertEqual(self.project.status, 'Active')
        self.assertEqual(len(self.tasks), 23)
        self.assertEqual(self.project.phases.count(), 7)
        self.assertEqual(
            {t.template_task.phase.template.code for t in self.tasks.values()}, {'CAPEX'})
        self.assertIn('Site activated. 23 tasks created.', ' '.join(self.messages))

    def test_task_mirror_and_milestone_flags_follow_the_capex_template(self):
        self.assertEqual({code for code, t in self.tasks.items() if t.is_mirror},
                         CAPEX_MIRROR_CODES)
        self.assertFalse(any(t.is_payment_milestone for t in self.tasks.values()))

    def test_cod_is_pre_assigned_to_the_pm_design_is_not(self):
        # COD is a PM-role manual task on CAPEX, so attach_opex_template's PM
        # pre-assignment (role PM, non-mirror) picks it up. Accepted for CAPEX-1.
        self.assertEqual(self.tasks['COD'].assigned_to, self.pm)
        self.assertIsNone(self.tasks['DESIGN'].assigned_to)

    def test_checklist_attaches_on_the_capex_task_that_has_one_on_opex(self):
        civil = self.tasks[CHECKLIST_TASK_CODE]
        self.assertEqual(_checklist_for_task(civil, self.project), self.checklist)
        batch = resolve_checklists_for_tasks(list(self.tasks.values()), self.project)
        self.assertEqual(batch[civil.pk], self.checklist)
        self.assertIsNone(_checklist_for_task(self.tasks['MODULE_INSTALLATION'], self.project))

    def test_phase_rules_return_nothing_for_capex(self):
        self.assertEqual(SiteRuleState(self.project).rules, ())
        for task in self.tasks.values():
            self.assertEqual(phase_rule_warnings(task, self.project), [])
            self.assertEqual(start_warnings(task, Task.IN_PROGRESS, self.project), [])

    def test_task_actions_are_permitted_on_a_capex_task(self):
        user = self.pm.user
        task = self.tasks['NET_METERING_APPROVAL']
        self.assertTrue(user_can_mark_task_not_applicable(user, task, self.project))
        self.assertTrue(can_duplicate_task_for_locations(user, task))
        self.assertTrue(can_rename_task(user, task))
        self.assertTrue(can_reorder_phase(user, task.phase))
        # The manual Design task is a normal task now: Not Applicable is open to it.
        self.assertTrue(user_can_mark_task_not_applicable(user, self.tasks['DESIGN'], self.project))

    def test_an_active_project_is_not_activated_again(self):
        response, messages = _activate(self.pm, self.project)
        self.assertEqual(response.status_code, 302)
        self.assertIn('Site is already active.', messages)
        self.assertEqual(Task.objects.filter(phase__project=self.project).count(), 23)


class OpexActivationUnchangedTests(TestCase):
    """OPEX activation with the CAPEX template present beside it."""

    @classmethod
    def setUpTestData(cls):
        _seed_opex()
        _seed_capex()
        cls.pm = _profile('opex_pm', 'PM')

    def test_opex_site_gets_the_opex_template_23_tasks_8_mirrors(self):
        site = _project('OPEX', self.pm, 'OPEX Site')
        response, _ = _activate(self.pm, site)
        self.assertEqual(response.status_code, 302)
        tasks = Task.objects.filter(phase__project=site).select_related('template_task__phase__template')
        self.assertEqual(tasks.count(), 23)
        self.assertEqual({t.template_task.code for t in tasks if t.is_mirror}, OPEX_MIRROR_CODES)
        self.assertEqual({t.template_task.phase.template.code for t in tasks}, {'OPEX'})


class AttachFailureRollbackTests(TestCase):
    """R-2 past the pre-check: an attach that raises INSIDE opex_site_activate's atomic
    block takes the status change and its ledger row back with it.

    The pre-check is patched to report a template while the attach — which resolves
    through utils, not through the view's name — still finds none. That is the window a
    template retired between the two reads would open; nothing else reaches it now."""

    @classmethod
    def setUpTestData(cls):
        cls.pm = _profile('rollback_pm', 'PM')

    def test_a_failed_attach_leaves_nothing_behind(self):
        project = _project('CAPEX', self.pm, 'CAPEX Rollback Site')
        with patch('projects.views.resolve_active_task_template', return_value=object()):
            with self.assertRaises(TaskTemplate.DoesNotExist):
                _activate(self.pm, project)
        project.refresh_from_db()
        self.assertEqual(project.status, 'Draft')
        self.assertIsNone(project.activated_at)
        self.assertFalse(project.phases.exists())
        self.assertFalse(Task.objects.filter(phase__project=project).exists())
        self.assertFalse(StatusTransition.objects.filter(
            subject_id=project.pk, to_status='Active').exists())


class NoTemplateRefusalTests(TestCase):
    """A Draft site whose type has no active template is refused with a message, never a
    500, and nothing is written."""

    @classmethod
    def setUpTestData(cls):
        cls.pm = _profile('notpl_pm', 'PM')

    def _assert_refused(self, project, label):
        response, messages = _activate(self.pm, project)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(project.status, 'Draft')
        self.assertFalse(Task.objects.filter(phase__project=project).exists())
        self.assertEqual(len(messages), 1)
        self.assertIn(f'{label} projects cannot be activated yet', messages[0])
        return messages[0]

    def test_capex_without_a_template_is_refused(self):
        self._assert_refused(_project('CAPEX', self.pm, 'CAPEX No Template'), 'CAPEX')

    def test_resco_refusal_uses_the_label_never_the_stored_value(self):
        message = self._assert_refused(_project('OPEX', self.pm, 'RESCO No Template'), 'RESCO')
        self.assertNotIn('OPEX', message)
