"""The UKRU001 one-off label command: oneoff_ukru001_set_location_labels.

UKRU001 and production ids 2099-2116 / 60 / 61 do not exist here, so each test builds
a small OPEX project with four linked location tasks and points the command's
constants (PROJECT_ID, LABELS, EXPECTED_CODES) at them with patch.multiple. The
template tasks are the real OPEX v1 rows from migration 0075, so the names are built
from the same labels production has.

Run with:
    python manage.py test projects.tests_oneoff_ukru001_labels --settings=solarpms.test_settings
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

CMD = 'oneoff_ukru001_set_location_labels'
MODULE = 'projects.management.commands.oneoff_ukru001_set_location_labels'
CIVIL = 'CIVIL_WORK_AND_MMS_INSTALLATION'
MODULE_CODE = 'MODULE_INSTALLATION'
ACTION_CODE = 'task_location_label_set'

# The fields the command must never touch.
UNTOUCHED = ('status', 'task_name', 'task_order', 'template_task_id', 'assigned_to_id',
             'due_date', 'submitted_at', 'approved_at')


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


class SetLabelsCommandTests(TestCase):

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
        rows = [(self.civil, 'Ticket Plaza 1'), (self.civil, 'Parking-3'),
                (self.module, 'Ticket Plaza 1'), (self.module, 'Parking-3')]
        self.tasks = [
            Task.objects.create(
                phase=self.phase, task_name=f'{tt.label} {loc}', task_order=order,
                template_task=tt, assigned_to=self.actor,
                due_date=datetime.date(2026, 10, 1), submitted_at=now, approved_at=now)
            for order, (tt, loc) in enumerate(rows, start=1)
        ]
        self.labels = {t.pk: (tt.pk, loc) for t, (tt, loc) in zip(self.tasks, rows)}
        self.codes = {self.civil.pk: CIVIL, self.module.pk: MODULE_CODE}

    # -- helpers ------------------------------------------------------------------

    def _project(self, name, project_type):
        return Project.objects.create(
            customer_name=name, customer_phone='9876543210', site_address='1 Test Road',
            city='Lucknow', project_type=project_type, status='Active')

    def _run(self, *args, labels=None, codes=None):
        out, err = StringIO(), StringIO()
        self.err = err
        with patch.multiple(MODULE,
                            PROJECT_ID=self.site.project_id,
                            LABELS=self.labels if labels is None else labels,
                            EXPECTED_CODES=self.codes if codes is None else codes):
            call_command(CMD, *args, stdout=out, stderr=err)
        return out.getvalue(), err.getvalue()

    def _apply(self, **kwargs):
        return self._run('--apply', '--actor', 'ukru_actor', **kwargs)

    def _snapshot(self):
        return {t['id']: t for t in Task.objects.filter(pk__in=self.labels).values(
            'id', 'location_label', *UNTOUCHED)}

    def _logs(self):
        return ActivityLog.objects.filter(action_code=ACTION_CODE).count()

    def _set(self):
        return dict(Task.objects.filter(pk__in=self.labels)
                    .values_list('id', 'location_label'))

    def _assert_aborts(self, expect, **kwargs):
        """Abort, for the reason `expect` names, with nothing written."""
        before = self._snapshot()
        with self.assertRaisesMessage(CommandError, 'Nothing was written'):
            self._apply(**kwargs)
        self.assertIn(expect, self.err.getvalue())
        self.assertEqual(self._snapshot(), before, 'an aborted run wrote to a task')
        self.assertEqual(self._logs(), 0, 'an aborted run wrote an ActivityLog row')

    # -- dry-run ------------------------------------------------------------------

    def test_dry_run_writes_nothing(self):
        before = self._snapshot()
        out, _ = self._run()
        self.assertEqual(self._snapshot(), before)
        self.assertEqual(self._logs(), 0)
        self.assertIn('DRY-RUN', out)
        self.assertEqual(out.count('WOULD SET'), 4)

    # -- apply --------------------------------------------------------------------

    def test_apply_sets_every_label_logs_each_and_changes_nothing_else(self):
        before = self._snapshot()
        out, _ = self._apply()
        after = self._snapshot()

        self.assertEqual(self._set(), {pk: loc for pk, (_, loc) in self.labels.items()})
        for task_id, row in after.items():
            for field in UNTOUCHED:
                self.assertEqual(row[field], before[task_id][field],
                                 f'task {task_id}: {field} changed')

        logs = ActivityLog.objects.filter(action_code=ACTION_CODE)
        self.assertEqual(sorted(logs.values_list('entity_id', flat=True)), sorted(self.labels))
        self.assertTrue(all(l.entity_type == 'Task' and l.actor_id == self.actor.pk
                            and l.project_id == self.site.pk for l in logs))
        self.assertIn("Location label set to 'Parking-3'; was ''",
                      set(logs.values_list('action', flat=True)))
        self.assertIn('set 4 of 4', out)
        self.assertIn('ActivityLog rows written: 4', out)
        self.assertNotIn('WARNING', out)

    def test_a_lost_log_row_warns_but_keeps_the_labels(self):
        with patch(f'{MODULE}.log_activity'):
            out, _ = self._apply()
        self.assertEqual(self._set(), {pk: loc for pk, (_, loc) in self.labels.items()})
        self.assertIn('WARNING: 4 labels set but 0 ActivityLog rows', out)

    # -- one bad row aborts everything --------------------------------------------

    def test_a_row_on_the_wrong_project_aborts(self):
        other = self._project('Some Other Site', 'OPEX')
        other_phase = ProjectPhase.objects.create(
            project=other, phase_name='Installation', phase_order=1)
        Task.objects.filter(pk=self.tasks[3].pk).update(phase=other_phase)
        self._assert_aborts(f'on project {other.project_id}, not')

    def test_a_row_linked_to_the_other_template_task_aborts(self):
        Task.objects.filter(pk=self.tasks[0].pk).update(template_task=self.module)
        self._assert_aborts(f'template task is {self.module.pk}, expected {self.civil.pk}')

    def test_an_unlinked_row_aborts(self):
        Task.objects.filter(pk=self.tasks[2].pk).update(template_task=None)
        self._assert_aborts(f'template task is None, expected {self.module.pk}')

    def test_a_name_that_does_not_match_its_label_aborts(self):
        # Right prefix, wrong location: the label must not be written from the map
        # onto a task whose name says somewhere else.
        Task.objects.filter(pk=self.tasks[1].pk).update(
            task_name=f'{self.civil.label} Food Court')
        self._assert_aborts('expected ')

    def test_a_name_with_a_separator_aborts(self):
        Task.objects.filter(pk=self.tasks[1].pk).update(
            task_name=f'{self.civil.label} - Parking-3')
        self._assert_aborts(f"expected '{self.civil.label} Parking-3'")

    def test_a_row_with_a_label_already_set_aborts(self):
        Task.objects.filter(pk=self.tasks[1].pk).update(location_label='Somewhere')
        self._assert_aborts("location_label is already 'Somewhere'")

    def test_a_target_code_mismatch_aborts(self):
        self._assert_aborts("expected 'INVERTER_INSTALLATION'",
                            codes={self.civil.pk: CIVIL, self.module.pk: 'INVERTER_INSTALLATION'})

    def test_a_missing_task_aborts(self):
        self._assert_aborts('task 999999: does not exist',
                            labels={**self.labels, 999999: (self.civil.pk, 'ESS-2')})

    # -- actor --------------------------------------------------------------------

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

    # -- re-run -------------------------------------------------------------------

    def test_rerunning_apply_after_success_aborts_and_changes_nothing(self):
        self._apply()
        before, logs_before = self._snapshot(), self._logs()
        with self.assertRaisesMessage(CommandError, 'Nothing was written'):
            self._apply()
        self.assertIn('location_label is already', self.err.getvalue())
        self.assertEqual(self._snapshot(), before)
        self.assertEqual(self._logs(), logs_before)


class ProductionConstantsTests(TestCase):
    """The hardcoded mapping itself, unpatched."""

    def test_the_mapping_is_the_eighteen_agreed_rows(self):
        from projects.management.commands import oneoff_ukru001_set_location_labels as cmd
        self.assertEqual(cmd.PROJECT_ID, 'UKRU001')
        self.assertEqual(cmd.EXPECTED_CODES, {60: CIVIL, 61: MODULE_CODE})
        expected = {
            'Ticket Plaza 1': (2099, 2108), 'Ticket Plaza 2': (2100, 2109),
            'Toilet Block 4': (2101, 2110), 'Parking-3': (2102, 2111),
            'Toilet Block 5': (2103, 2112), 'Toilet Block 6': (2104, 2113),
            'ESS-2': (2105, 2114), 'Food Court': (2106, 2115),
            'Library Block': (2107, 2116),
        }
        want = {}
        for loc, (civil_id, module_id) in expected.items():
            want[civil_id] = (60, loc)
            want[module_id] = (61, loc)
        self.assertEqual(cmd.LABELS, want)
