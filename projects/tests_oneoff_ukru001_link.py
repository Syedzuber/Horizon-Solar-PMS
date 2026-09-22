"""The UKRU001 one-off link command: oneoff_ukru001_link_location_tasks.

UKRU001 and production ids 2099-2116 / 60 / 61 do not exist here, so each test builds
a small OPEX project with four hand-added tasks and points the command's constants
(PROJECT_ID, LINKS, EXPECTED_CODES) at them with patch.multiple. The target template
tasks are the real OPEX v1 rows from migration 0075, not stand-ins, so the active-template
and code checks run against the same content production has.

Run with:
    python manage.py test projects.tests_oneoff_ukru001_link --settings=solarpms.test_settings
"""
import datetime
from io import StringIO
from importlib import import_module
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from .models import (
    ActivityLog, Project, ProjectPhase, Task, TaskTemplate, TaskTemplatePhase,
    TaskTemplateTask, UserProfile,
)
from .utils import resolve_residential_template

CMD = 'oneoff_ukru001_link_location_tasks'
MODULE = 'projects.management.commands.oneoff_ukru001_link_location_tasks'
CIVIL = 'CIVIL_WORK_AND_MMS_INSTALLATION'
MODULE_CODE = 'MODULE_INSTALLATION'

# The fields the command must never touch.
UNTOUCHED = ('status', 'task_name', 'task_order', 'assigned_to_id', 'due_date',
             'submitted_at', 'approved_at')


class _ConcreteApps:
    """Stands in for the `apps` registry a RunPython function is handed."""

    _MODELS = {
        'TaskTemplate':      TaskTemplate,
        'TaskTemplatePhase': TaskTemplatePhase,
        'TaskTemplateTask':  TaskTemplateTask,
        'Task':              Task,
    }

    def get_model(self, app_label, model_name):
        return self._MODELS[model_name]


def _opex_template_task(code):
    return TaskTemplateTask.objects.get(
        code=code, phase__template__project_type='OPEX',
        phase__template__status=TaskTemplate.ACTIVE)


class LinkCommandTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        import_module('projects.migrations.0075_seed_opex_template_v1').seed_opex_v1(
            _ConcreteApps(), None)
        cls.civil = _opex_template_task(CIVIL)
        cls.module = _opex_template_task(MODULE_CODE)

        cls.user = User.objects.create_user(username='ukru_actor', password='pw12345')
        cls.actor = cls.user.profile
        cls.actor.role = 'PM'
        cls.actor.is_active = True
        cls.actor.save()

    def setUp(self):
        self.site = self._project('UKRU Test Site', 'OPEX')
        self.phase = ProjectPhase.objects.create(
            project=self.site, phase_name='Installation', phase_order=1)
        now = timezone.now()
        self.tasks = [
            self._task(f'{label} - Location {n}', order, now)
            for order, (label, n) in enumerate([
                ('Civil Work and MMS Installation', 1),
                ('Civil Work and MMS Installation', 2),
                ('Module Installation', 1),
                ('Module Installation', 2),
            ], start=1)
        ]
        self.links = {
            self.tasks[0].pk: self.civil.pk, self.tasks[1].pk: self.civil.pk,
            self.tasks[2].pk: self.module.pk, self.tasks[3].pk: self.module.pk,
        }
        self.codes = {self.civil.pk: CIVIL, self.module.pk: MODULE_CODE}

    # -- helpers ------------------------------------------------------------------

    def _project(self, name, project_type):
        return Project.objects.create(
            customer_name=name, customer_phone='9876543210', site_address='1 Test Road',
            city='Lucknow', project_type=project_type, status='Active')

    def _task(self, name, order, now, phase=None):
        return Task.objects.create(
            phase=phase or self.phase, task_name=name, task_order=order,
            assigned_to=self.actor, due_date=datetime.date(2026, 10, 1),
            submitted_at=now, approved_at=now)

    def _run(self, *args, links=None, codes=None, project_id=None):
        out, err = StringIO(), StringIO()
        self.err = err
        with patch.multiple(MODULE,
                            PROJECT_ID=project_id or self.site.project_id,
                            LINKS=self.links if links is None else links,
                            EXPECTED_CODES=self.codes if codes is None else codes):
            call_command(CMD, *args, stdout=out, stderr=err)
        return out.getvalue(), err.getvalue()

    def _apply(self, **kwargs):
        return self._run('--apply', '--actor', 'ukru_actor', **kwargs)

    def _snapshot(self):
        return {t['id']: t for t in Task.objects.filter(pk__in=self.links).values(
            'id', 'template_task_id', *UNTOUCHED)}

    def _logs(self):
        return ActivityLog.objects.filter(action_code='task_template_linked').count()

    def _assert_aborts(self, expect, **kwargs):
        """Abort, for the reason `expect` names, with nothing written."""
        before = self._snapshot()
        with self.assertRaisesMessage(CommandError, 'Nothing was written'):
            self._apply(**kwargs)
        self.assertIn(expect, self.err.getvalue())
        self.assertEqual(self._snapshot(), before, 'an aborted run wrote to a task')
        self.assertEqual(self._logs(), 0, 'an aborted run wrote an ActivityLog row')

    def _linked(self):
        return dict(Task.objects.filter(pk__in=self.links)
                    .values_list('id', 'template_task_id'))

    # -- 1. dry-run ---------------------------------------------------------------

    def test_dry_run_writes_nothing(self):
        before = self._snapshot()
        out, _ = self._run()
        self.assertEqual(self._snapshot(), before)
        self.assertEqual(self._logs(), 0)
        self.assertIn('DRY-RUN', out)
        self.assertEqual(out.count('WOULD LINK'), 4)

    # -- 2. apply -----------------------------------------------------------------

    def test_apply_links_every_row_logs_each_and_changes_nothing_else(self):
        before = self._snapshot()
        out, _ = self._apply()
        after = self._snapshot()

        self.assertEqual(self._linked(), self.links)
        for task_id, row in after.items():
            for field in UNTOUCHED:
                self.assertEqual(row[field], before[task_id][field],
                                 f'task {task_id}: {field} changed')

        logs = ActivityLog.objects.filter(action_code='task_template_linked')
        self.assertEqual(sorted(logs.values_list('entity_id', flat=True)), sorted(self.links))
        self.assertTrue(all(l.entity_type == 'Task' and l.actor_id == self.actor.pk
                            and l.project_id == self.site.pk for l in logs))
        self.assertIn(f'Linked to template task {self.civil.pk} ({CIVIL}); was none',
                      set(logs.values_list('action', flat=True)))
        self.assertIn('linked 4, already linked 0', out)
        self.assertIn('ActivityLog rows written: 4', out)
        self.assertNotIn('WARNING', out)

    def test_a_lost_log_row_warns_but_keeps_the_links(self):
        with patch(f'{MODULE}.log_activity'):
            out, _ = self._apply()
        self.assertEqual(self._linked(), self.links)
        self.assertIn('WARNING: 4 tasks linked but 0 ActivityLog rows', out)

    # -- 3-9. one bad row aborts everything ----------------------------------------

    def test_a_row_on_the_wrong_project_aborts(self):
        other = self._project('Some Other Site', 'OPEX')
        other_phase = ProjectPhase.objects.create(
            project=other, phase_name='Installation', phase_order=1)
        Task.objects.filter(pk=self.tasks[3].pk).update(phase=other_phase)
        self._assert_aborts(f'on project {other.project_id}, not')

    def test_a_row_on_a_non_opex_project_aborts(self):
        Project.objects.filter(pk=self.site.pk).update(project_type='Residential')
        self._assert_aborts("project type is 'Residential', not OPEX")

    def test_a_name_prefix_mismatch_aborts(self):
        Task.objects.filter(pk=self.tasks[2].pk).update(task_name='Inverter Installation - Loc 1')
        self._assert_aborts('does not start with')

    def test_a_row_already_linked_to_its_target_is_skipped_and_the_rest_link(self):
        Task.objects.filter(pk=self.tasks[0].pk).update(template_task=self.civil)
        out, _ = self._apply()
        self.assertEqual(self._linked(), self.links)
        self.assertIn('ALREADY LINKED', out)
        self.assertIn('linked 3, already linked 1', out)
        self.assertEqual(
            sorted(ActivityLog.objects.filter(action_code='task_template_linked')
                   .values_list('entity_id', flat=True)),
            sorted(t.pk for t in self.tasks[1:]))

    def test_a_row_linked_to_a_different_template_task_aborts(self):
        Task.objects.filter(pk=self.tasks[0].pk).update(template_task=self.module)
        self._assert_aborts(f'already linked to template task {self.module.pk}, not {self.civil.pk}')

    def test_a_mirror_task_aborts(self):
        Task.objects.filter(pk=self.tasks[1].pk).update(is_mirror=True)
        self._assert_aborts('is a mirror task')

    def test_a_payment_milestone_task_aborts(self):
        Task.objects.filter(pk=self.tasks[1].pk).update(is_payment_milestone=True)
        self._assert_aborts('is a payment milestone')

    def test_a_target_on_the_residential_template_aborts(self):
        # Residential has its own MODULE_INSTALLATION labelled "Module Installation", so
        # the code and name checks both pass and only the template check can stop it.
        resolve_residential_template()
        res = TaskTemplateTask.objects.get(code=MODULE_CODE,
                                           phase__template__project_type='Residential')
        links = {**self.links, self.tasks[2].pk: res.pk, self.tasks[3].pk: res.pk}
        self._assert_aborts('not the active OPEX template', links=links, codes={self.civil.pk: CIVIL, res.pk: MODULE_CODE})

    def test_a_target_on_an_archived_opex_template_aborts(self):
        # Activating v2 archives v1, which is where both targets live.
        v1 = self.civil.phase.template
        TaskTemplate.objects.create(
            code=v1.code, label=v1.label, project_type='OPEX',
            version_no=v1.version_no + 1, status=TaskTemplate.DRAFT).activate()
        self._assert_aborts('not the active OPEX template')

    def test_a_target_code_mismatch_aborts(self):
        self._assert_aborts("expected 'INVERTER_INSTALLATION'", codes={self.civil.pk: CIVIL, self.module.pk: 'INVERTER_INSTALLATION'})

    def test_a_missing_task_aborts(self):
        self._assert_aborts('task 999999: does not exist', links={**self.links, 999999: self.civil.pk})

    # -- 10. actor ----------------------------------------------------------------

    def test_apply_without_actor_is_refused(self):
        before = self._snapshot()
        with self.assertRaisesMessage(CommandError, '--apply requires --actor'):
            self._run('--apply')
        self.assertEqual(self._snapshot(), before)
        self.assertEqual(self._logs(), 0)

    def test_apply_with_an_unknown_actor_is_refused(self):
        before = self._snapshot()
        with self.assertRaisesMessage(CommandError, 'no such user'):
            self._run('--apply', '--actor', 'nobody_here')
        self.assertEqual(self._snapshot(), before)
        self.assertEqual(self._logs(), 0)

    def test_apply_with_an_inactive_actor_is_refused(self):
        User.objects.filter(pk=self.user.pk).update(is_active=False)
        with self.assertRaisesMessage(CommandError, 'inactive'):
            self._apply()
        self.assertEqual(self._logs(), 0)

    def test_apply_with_an_actor_without_a_profile_is_refused(self):
        UserProfile.objects.filter(pk=self.actor.pk).delete()
        with self.assertRaisesMessage(CommandError, 'no UserProfile'):
            self._apply()

    # -- 11. re-run ---------------------------------------------------------------

    def test_rerunning_apply_after_success_changes_nothing(self):
        self._apply()
        before, logs_before = self._snapshot(), self._logs()
        out, _ = self._apply()
        self.assertEqual(self._snapshot(), before)
        self.assertEqual(self._logs(), logs_before)
        self.assertEqual(out.count('ALREADY LINKED'), 4)
        self.assertIn('linked 0, already linked 4', out)
        self.assertNotIn('WARNING', out)


class ProductionConstantsTests(TestCase):
    """The hardcoded mapping itself, unpatched."""

    def test_the_mapping_is_the_eighteen_agreed_rows_and_never_2093(self):
        from projects.management.commands import oneoff_ukru001_link_location_tasks as cmd
        self.assertEqual(cmd.PROJECT_ID, 'UKRU001')
        self.assertEqual({k for k, v in cmd.LINKS.items() if v == 60}, set(range(2099, 2108)))
        self.assertEqual({k for k, v in cmd.LINKS.items() if v == 61}, set(range(2108, 2117)))
        self.assertEqual(len(cmd.LINKS), 18)
        self.assertNotIn(2093, cmd.LINKS)
        self.assertEqual(cmd.EXPECTED_CODES, {60: CIVIL, 61: MODULE_CODE})
