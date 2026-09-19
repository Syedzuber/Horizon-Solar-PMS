"""
A hand-added task is created ASSIGNED, in a role its phase actually uses. Session T2.

THE DEFECT
----------
`TaskAddForm.assigned_role` offered all seven roles with no blank option and the
template selected none, so the browser submitted the first — PM. The form had no
assignee field, so every hand-added task was born unassigned, and an unassigned task
cannot change status on either screen. On an OPEX delivery phase the result was a PM
task that `task_assign` would only let a PM hold: the SCM user it was meant for could
not be given it at all.

WHAT IS PINNED HERE
-------------------
- `roles_for_phase()` and its three sources, in `Task.ROLE_CHOICES` order.
- The add form offers only the phase's roles: one role is preselected, several lead
  with a blank option. Never a silent first choice.
- `TaskAddForm.clean()` is THE guard. Both add-task templates are `novalidate`, and
  these tests POST directly, so no narrowing in the page can be what passes them.
- A created task is assigned and can move to In Progress at once.

EVERY OPEX FIXTURE IS A REALLY ACTIVATED SITE, as in tests_two_step_completion.py: the
phases under test are the rows `attach_opex_template()` produced.

Run with:
    python manage.py test projects.tests_task_add --settings=solarpms.test_settings
"""
import re
from datetime import date, timedelta
from decimal import Decimal
from importlib import import_module

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse

from .models import (
    Project, ProjectPhase, Task, TaskTemplate, TaskTemplatePhase, TaskTemplateTask,
    UserProfile,
)
from .utils import resolve_residential_template, roles_for_phase


DELIVERY_PHASE = 'Procurement & Delivery'   # OPEX v1: four SCM tasks, one role
CLOSEOUT_PHASE = 'Closeout'                 # OPEX v1: PM, Design, Project Coordinator


class _ConcreteApps:
    """Stands in for the `apps` registry a RunPython function is handed."""

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
    module = import_module('projects.migrations.0075_seed_opex_template_v1')
    module.seed_opex_v1(_ConcreteApps(), None)


def _profile(username, role):
    """signals.py creates the profile on User post_save; update its role."""
    user = User.objects.create_user(username=username, password='x',
                                    first_name=username.title(), last_name='Test')
    profile = user.profile
    profile.role = role
    profile.save(update_fields=['role'])
    return profile


def _client_for(profile):
    client = Client()
    client.force_login(profile.user)
    return client


class TaskAddFixture(TestCase):

    @classmethod
    def setUpTestData(cls):
        resolve_residential_template()
        _seed_opex()
        cls.pm       = _profile('t2_pm', 'PM')
        cls.scm      = _profile('t2_scm', 'SCM')
        cls.design   = _profile('t2_design', 'Design')
        cls.inactive = _profile('t2_scm_gone', 'SCM')
        cls.inactive.is_active = False
        cls.inactive.save(update_fields=['is_active'])

    def setUp(self):
        self.site = Project.objects.create(
            customer_name='T2 Tender Site', customer_phone='9876543210',
            site_address='1 Delivery Road', city='Lucknow', project_type='OPEX',
            dc_capacity_kw=Decimal('100.00'), status='Draft', assigned_pm=self.pm,
        )
        response = _client_for(self.pm).post(
            reverse('opex_site_activate', args=[self.site.project_id]))
        self.assertEqual(response.status_code, 302, 'OPEX activation did not redirect')
        self.site.refresh_from_db()
        self.assertEqual(self.site.status, 'Active')
        self.delivery = ProjectPhase.objects.get(project=self.site, phase_name=DELIVERY_PHASE)
        self.closeout = ProjectPhase.objects.get(project=self.site, phase_name=CLOSEOUT_PHASE)
        self.url = reverse('task_add', args=[self.site.project_id])

    def post(self, **fields):
        data = {'task_name': 'Delivery_Solar panel lot-2',
                'due_date': (date.today() + timedelta(days=7)).isoformat()}
        data.update(fields)
        return _client_for(self.pm).post(self.url, data, HTTP_HX_REQUEST='true')

    def added(self):
        return Task.objects.filter(phase__project=self.site,
                                   task_name='Delivery_Solar panel lot-2')

    def narrowed(self, **params):
        """The modal as the phase/role hx-get re-renders it."""
        return _client_for(self.pm).get(self.url, params, HTTP_HX_REQUEST='true')

    @staticmethod
    def select(response, name):
        html = response.content.decode()
        block = re.search(rf'<select name="{name}".*?</select>', html, re.S).group(0)
        return re.findall(r'<option value="([^"]*)"\s*(selected)?', block)


# ---------------------------------------------------------------------------
# roles_for_phase — the three sources
# ---------------------------------------------------------------------------

class RolesForPhaseTests(TaskAddFixture):

    def test_template_source_on_an_activated_opex_site(self):
        self.assertEqual(roles_for_phase(self.delivery), [Task.SCM])
        self.assertEqual(roles_for_phase(self.closeout),
                         [Task.PM, Task.DESIGN, Task.PROJECT_COORDINATOR])

    def test_a_phase_without_template_tasks_uses_its_own_tasks(self):
        legacy = ProjectPhase.objects.create(project=self.site, phase_name='Legacy',
                                             phase_order=99)
        Task.objects.create(phase=legacy, task_name='a', task_order=1,
                            assigned_role=Task.FINANCE)
        Task.objects.create(phase=legacy, task_name='b', task_order=2,
                            assigned_role=Task.PM)
        self.assertEqual(roles_for_phase(legacy), [Task.PM, Task.FINANCE])

    def test_an_empty_phase_offers_every_role(self):
        empty = ProjectPhase.objects.create(project=self.site, phase_name='Empty',
                                            phase_order=98)
        self.assertEqual(roles_for_phase(empty), [r for r, _ in Task.ROLE_CHOICES])

    def test_it_writes_nothing(self):
        before = Task.objects.count()
        with self.assertNumQueries(1):
            roles_for_phase(self.delivery)
        self.assertEqual(Task.objects.count(), before)


# ---------------------------------------------------------------------------
# What the form offers
# ---------------------------------------------------------------------------

class OfferedChoicesTests(TaskAddFixture):

    def test_the_delivery_phase_offers_scm_only_preselected(self):
        response = self.narrowed(phase=self.delivery.pk)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.select(response, 'assigned_role'), [('SCM', 'selected')])
        # Assignees follow the preselected role: active SCM users only.
        offered = [pk for pk, _ in self.select(response, 'assigned_to') if pk]
        self.assertEqual(offered, [str(self.scm.pk)])

    def test_a_multi_role_phase_offers_exactly_its_set_blank_first(self):
        response = self.narrowed(phase=self.closeout.pk)
        self.assertEqual(self.select(response, 'assigned_role'),
                         [('', ''), ('PM', ''), ('Design', ''),
                          ('Project Coordinator', '')])

    def test_no_phase_chosen_offers_no_role(self):
        response = self.narrowed()
        self.assertEqual(self.select(response, 'assigned_role'), [('', '')])

    def test_changing_the_role_narrows_the_assignees(self):
        response = self.narrowed(phase=self.closeout.pk, assigned_role='Design')
        offered = [pk for pk, _ in self.select(response, 'assigned_to') if pk]
        self.assertEqual(offered, [str(self.design.pk)])


# ---------------------------------------------------------------------------
# What the server accepts — clean() is the guard
# ---------------------------------------------------------------------------

class CreationTests(TaskAddFixture):

    def test_the_delivery_task_is_created_as_scm_and_assigned(self):
        self.post(phase=self.delivery.pk, assigned_role='SCM', assigned_to=self.scm.pk)
        task = self.added().get()
        self.assertEqual(task.assigned_role, Task.SCM)
        self.assertEqual(task.assigned_to, self.scm)
        self.assertFalse(task.is_mirror)
        self.assertIsNone(task.template_task)

    def test_pm_on_the_delivery_phase_is_refused_and_nothing_written(self):
        # A PM assignee, so the ONLY rule that can stop this is the phase-role one.
        response = self.post(phase=self.delivery.pk, assigned_role='PM',
                             assigned_to=self.pm.pk)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'is not a role used in')
        self.assertFalse(self.added().exists())

    def test_an_assignee_in_another_role_is_refused(self):
        response = self.post(phase=self.delivery.pk, assigned_role='SCM',
                             assigned_to=self.pm.pk)
        self.assertContains(response, "Choose someone in the task")
        self.assertFalse(self.added().exists())

    def test_an_inactive_assignee_is_refused(self):
        self.post(phase=self.delivery.pk, assigned_role='SCM',
                  assigned_to=self.inactive.pk)
        self.assertFalse(self.added().exists())

    def test_no_assignee_is_refused(self):
        self.post(phase=self.delivery.pk, assigned_role='SCM')
        self.assertFalse(self.added().exists())

    def test_a_multi_role_phase_requires_a_role_to_be_chosen(self):
        self.post(phase=self.closeout.pk, assigned_to=self.pm.pk)
        self.assertFalse(self.added().exists())

    def test_a_multi_role_phase_accepts_any_of_its_roles(self):
        self.post(phase=self.closeout.pk, assigned_role='Design',
                  assigned_to=self.design.pk)
        self.assertEqual(self.added().get().assigned_role, Task.DESIGN)

    def test_the_created_task_moves_to_in_progress_at_once(self):
        self.post(phase=self.delivery.pk, assigned_role='SCM', assigned_to=self.scm.pk)
        task = self.added().get()
        _client_for(self.scm).post(
            reverse('task_status_update', args=[self.site.project_id, task.pk]),
            {'status': Task.IN_PROGRESS}, HTTP_HX_REQUEST='true')
        task.refresh_from_db()
        self.assertEqual(task.status, Task.IN_PROGRESS)
