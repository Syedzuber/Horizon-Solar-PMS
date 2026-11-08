"""
SCHED-1 — Task.due_date_source and the single date-setting function.

WHY THIS EXISTS
---------------
The SCHED-2 scheduler will rewrite only due dates marked `system`. A write site that
bypasses scheduling.set_task_due_date() would leave a person's date unmarked or marked
`system`, and the scheduler would then overwrite it (spec Layer 5). So every one of the
7 production write paths is exercised here, end to end through its real entry point:

    1  edit screen               task_set_due_date (role owner, PM; set and clear)
    2  In Progress date entry    task_status_update with an inline due_date
    3  add a task                task_add
    4  duplicate-for-locations   task_duplicate_locations_create
    5  Django admin              TaskAdmin change and add forms
    6  cascade                   task_set_due_date with cascade ON (recalculate_from_task)
    7  full recalculation        project_recalculate_dates / enable_cascade_scheduling
                                 (calculate_due_dates)

Paths 1-5 are manual: source 'manual' and exactly one ActivityLog row with
action_code='due_date_changed' (ruling A). Path 6 is manual for the typed starting task
and system for the tasks it computes (ruling B). Path 7 is system with no per-task row
and the same dates as before SCHED-1 (ruling C).

Also pinned: create-then-date runs in one transaction (a failure setting the date leaves
no undated task), and a failed log write never aborts the date change (its savepoint).

Run with:
    python manage.py test projects.tests_sched1_due_date_source --settings=solarpms.test_settings
"""
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

from django.contrib import admin as django_admin
from django.contrib.auth.models import User
from django.db import DatabaseError
from django.forms import MultiWidget
from django.test import Client, TestCase
from django.urls import reverse

from . import tests_duplicate_for_locations as dup
from . import tests_residential_baseline as rb
from .models import ActivityLog, DueDateChangeLog, Project, SystemSettings, Task
from .scheduling import set_task_due_date
from .utils import add_calendar_days

MANUAL = Task.DUE_DATE_MANUAL
SYSTEM = Task.DUE_DATE_SYSTEM


def _changed_rows(task):
    """The due_date_changed rows for one task, oldest first."""
    return list(ActivityLog.objects.filter(entity_type='Task', entity_id=task.pk,
                                           action_code='due_date_changed')
                .order_by('timestamp', 'pk').values_list('action', flat=True))


def _expected_recalc_dates(project):
    """calculate_due_dates() as it stood BEFORE SCHED-1, re-stated independently: the
    chain starts at activated_at's date; an Internal task is due its duration after the
    previous Internal one, an External task shadows the current chain date."""
    previous = project.activated_at.date()
    expected = {}
    for task in (Task.objects.filter(phase__project=project)
                 .order_by('phase__phase_order', 'task_order')):
        if task.task_type == Task.EXTERNAL:
            expected[task.pk] = previous
        else:
            previous = add_calendar_days(previous, task.duration_days)
            expected[task.pk] = previous
    return expected


# ---------------------------------------------------------------------------
# The function itself
# ---------------------------------------------------------------------------

class SetTaskDueDateUnitTests(rb.ResidentialBaselineBase):

    def setUp(self):
        super().setUp()
        self.task = self._task(self.project_a, 'AC Cable Work')

    def test_a_manual_date_sets_both_fields_and_does_not_save(self):
        target = date(2026, 12, 1)
        old = set_task_due_date(self.task, target, MANUAL, self.pm_a)
        self.assertIsNone(old)
        self.assertEqual((self.task.due_date, self.task.due_date_source), (target, MANUAL))
        stored = Task.objects.get(pk=self.task.pk)
        self.assertEqual((stored.due_date, stored.due_date_source), (None, None),
                         'the function must leave saving to the caller')

    def test_none_clears_the_date_and_the_source(self):
        set_task_due_date(self.task, date(2026, 12, 1), SYSTEM, None, log=False)
        set_task_due_date(self.task, None, MANUAL, self.pm_a)
        self.assertEqual((self.task.due_date, self.task.due_date_source), (None, None))

    def test_an_unknown_source_is_a_programming_error(self):
        with self.assertRaises(ValueError):
            set_task_due_date(self.task, date(2026, 12, 1), 'auto', self.pm_a)

    def test_logging_an_unsaved_task_is_a_programming_error(self):
        unsaved = Task(phase=self.task.phase, task_name='Unsaved', task_order=99)
        with self.assertRaises(ValueError):
            set_task_due_date(unsaved, date(2026, 12, 1), MANUAL, self.pm_a)

    def test_the_log_row_keeps_the_opening_words_and_states_both_dates(self):
        set_task_due_date(self.task, date(2026, 12, 1), MANUAL, self.pm_a)
        set_task_due_date(self.task, date(2026, 12, 5), MANUAL, self.pm_a, note='Because')
        set_task_due_date(self.task, None, MANUAL, self.pm_a)
        self.assertEqual(_changed_rows(self.task), [
            'Updated due date for task: AC Cable Work (was not set, now 2026-12-01)',
            'Updated due date for task: AC Cable Work (was 2026-12-01, now 2026-12-05). Because',
            'Cleared due date for task: AC Cable Work (was 2026-12-05, now not set)',
        ])
        row = ActivityLog.objects.filter(action_code='due_date_changed').first()
        self.assertEqual((row.project, row.actor), (self.project_a, self.pm_a))

    def test_no_row_when_the_date_is_unchanged(self):
        set_task_due_date(self.task, date(2026, 12, 1), SYSTEM, None, log=False)
        set_task_due_date(self.task, date(2026, 12, 1), MANUAL, self.pm_a)
        self.assertEqual(self.task.due_date_source, MANUAL,
                         'a person re-entering a system date takes it over')
        self.assertEqual(_changed_rows(self.task), [])

    def test_a_system_write_is_never_logged_per_task(self):
        set_task_due_date(self.task, date(2026, 12, 1), SYSTEM, None)
        self.assertEqual(_changed_rows(self.task), [])

    def test_a_long_task_name_is_truncated_to_the_column(self):
        self.task.task_name = 'X' * 200
        set_task_due_date(self.task, date(2026, 12, 1), MANUAL, self.pm_a, note='N' * 100)
        (text,) = _changed_rows(self.task)
        self.assertEqual(len(text), ActivityLog._meta.get_field('action').max_length)

    def test_a_failed_log_write_never_aborts_the_date_change(self):
        """Ruling A: the row is written in its own savepoint. A failing INSERT is rolled
        back to that savepoint and logged; the caller's save still goes through."""
        failing = MagicMock()
        failing.objects.create.side_effect = DatabaseError('log table unavailable')
        failing._meta = ActivityLog._meta
        with patch('projects.scheduling.ActivityLog', failing), \
                self.assertLogs('projects.scheduling', level='ERROR'):
            set_task_due_date(self.task, date(2026, 12, 1), MANUAL, self.pm_a)
        self.task.save()
        self.task.refresh_from_db()
        self.assertEqual((self.task.due_date, self.task.due_date_source),
                         (date(2026, 12, 1), MANUAL))


# ---------------------------------------------------------------------------
# Paths 1, 2, 3, 6, 7 — Residential entry points
# ---------------------------------------------------------------------------

class EditScreenTests(rb.ResidentialBaselineBase):
    """Path 1: task_set_due_date, both branches, set and clear."""

    def _post(self, profile, task, value):
        return rb._client_for(profile).post(
            reverse('task_set_due_date', args=[self.project_a.project_id, task.pk]),
            {'due_date': value})

    def test_a_role_owner_sets_a_manual_date_with_one_row(self):
        task = self._task(self.project_a, 'AC Cable Work')
        self._post(self.se_a, task, '2026-12-01')
        task.refresh_from_db()
        self.assertEqual((task.due_date, task.due_date_source), (date(2026, 12, 1), MANUAL))
        self.assertEqual(_changed_rows(task), [
            'Updated due date for task: AC Cable Work (was not set, now 2026-12-01)'])
        self.assertEqual(ActivityLog.objects.filter(
            entity_id=task.pk, action__startswith='Updated due date').count(), 1,
            'the old blank-code line must be gone (ruling A: one row)')

    def test_a_role_owner_clears_a_date(self):
        task = self._task(self.project_a, 'AC Cable Work')
        self._post(self.se_a, task, '2026-12-01')
        self._post(self.se_a, task, '')
        task.refresh_from_db()
        self.assertEqual((task.due_date, task.due_date_source), (None, None))
        self.assertEqual(_changed_rows(task)[-1],
                         'Cleared due date for task: AC Cable Work (was 2026-12-01, now not set)')

    def test_the_pm_sets_and_clears_with_cascade_off(self):
        task = self._task(self.project_a, 'Earthing Work')
        self._post(self.pm_a, task, '2026-12-03')
        task.refresh_from_db()
        self.assertEqual((task.due_date, task.due_date_source), (date(2026, 12, 3), MANUAL))
        self._post(self.pm_a, task, '')
        task.refresh_from_db()
        self.assertEqual((task.due_date, task.due_date_source), (None, None))
        self.assertEqual(_changed_rows(task), [
            'Updated due date for task: Earthing Work (was not set, now 2026-12-03)',
            'Cleared due date for task: Earthing Work (was 2026-12-03, now not set)',
        ])

    def test_resubmitting_the_same_date_writes_no_row(self):
        task = self._task(self.project_a, 'AC Cable Work')
        self._post(self.se_a, task, '2026-12-01')
        self._post(self.se_a, task, '2026-12-01')
        self.assertEqual(len(_changed_rows(task)), 1)

    def test_a_person_editing_a_system_date_makes_it_manual(self):
        task = self._task(self.project_a, 'AC Cable Work')
        Task.objects.filter(pk=task.pk).update(due_date=date(2026, 12, 1),
                                               due_date_source=SYSTEM)
        self._post(self.se_a, task, '2026-12-09')
        task.refresh_from_db()
        self.assertEqual(task.due_date_source, MANUAL)
        self.assertEqual(_changed_rows(task), [
            'Updated due date for task: AC Cable Work (was 2026-12-01, now 2026-12-09)'])


class InProgressDateEntryTests(rb.ResidentialBaselineBase):
    """Path 2: the date typed when an undated task moves to In Progress."""

    def test_the_inline_date_is_manual_and_logged(self):
        task = self._task(self.project_a, 'Earthing Work')
        self.assertIsNone(task.due_date)
        rb._client_for(self.se_a).post(
            reverse('task_status_update', args=[self.project_a.project_id, task.pk]),
            {'status': Task.IN_PROGRESS, 'due_date': '2026-12-04'})
        task.refresh_from_db()
        self.assertEqual(task.status, Task.IN_PROGRESS)
        self.assertEqual((task.due_date, task.due_date_source), (date(2026, 12, 4), MANUAL))
        self.assertEqual(_changed_rows(task), [
            'Updated due date for task: Earthing Work (was not set, now 2026-12-04). '
            'Set when moving to In Progress'])
        self.assertTrue(ActivityLog.objects.filter(
            entity_id=task.pk, action_code='task_status_in_progress').exists(),
            'the status line is unchanged')


class TaskAddTests(rb.ResidentialBaselineBase):
    """Path 3: task_add."""

    def _add(self, name, due=''):
        # A phase that already holds a PM task: roles_for_phase() narrows the role
        # choices to the phase's template roles, so PM must be one of them.
        phase = (Task.objects.filter(phase__project=self.project_a, assigned_role=Task.PM)
                 .order_by('phase__phase_order').first().phase)
        response = rb._client_for(self.pm_a).post(
            reverse('task_add', args=[self.project_a.project_id]),
            {'phase': phase.pk, 'task_name': name, 'assigned_role': Task.PM,
             'assigned_to': self.pm_a.pk, 'due_date': due})
        # A refused form re-renders with 200; a saved task redirects.
        self.assertEqual(response.status_code, 302, 'task_add refused the form')
        return response

    def test_a_dated_add_is_manual_and_logged(self):
        self._add('Added dated', '2026-12-06')
        task = Task.objects.get(task_name='Added dated')
        self.assertEqual((task.due_date, task.due_date_source), (date(2026, 12, 6), MANUAL))
        self.assertEqual(_changed_rows(task), [
            'Updated due date for task: Added dated (was not set, now 2026-12-06)'])

    def test_an_undated_add_has_no_source_and_no_row(self):
        self._add('Added undated')
        task = Task.objects.get(task_name='Added undated')
        self.assertEqual((task.due_date, task.due_date_source), (None, None))
        self.assertEqual(_changed_rows(task), [])

    def test_a_failure_setting_the_date_leaves_no_undated_task(self):
        """The create and the date-set share task_add's atomic block (condition on the
        accepted build approach)."""
        with patch('projects.views.set_task_due_date', side_effect=RuntimeError('boom')):
            with self.assertRaises(RuntimeError):
                self._add('Added then failed', '2026-12-06')
        self.assertFalse(Task.objects.filter(task_name='Added then failed').exists())


class CascadeTests(rb.ResidentialBaselineBase):
    """Path 6: recalculate_from_task, reached by a PM date edit with cascade ON."""

    def test_the_typed_start_is_manual_and_the_computed_tasks_are_system(self):
        Project.objects.filter(pk=self.project_a.pk).update(cascade_scheduling=True)
        ordered = list(Task.objects.filter(phase__project=self.project_a)
                       .order_by('phase__phase_order', 'task_order'))
        anchor, following = ordered[0], ordered[1:]
        new_date = date(2026, 12, 10)
        rb._client_for(self.pm_a).post(
            reverse('task_set_due_date', args=[self.project_a.project_id, anchor.pk]),
            {'due_date': new_date.isoformat()})

        anchor.refresh_from_db()
        self.assertEqual((anchor.due_date, anchor.due_date_source), (new_date, MANUAL))
        self.assertEqual(_changed_rows(anchor), [
            f'Updated due date for task: {anchor.task_name} (was not set, now 2026-12-10)'])
        self.assertEqual(ActivityLog.objects.filter(
            entity_id=anchor.pk, action__startswith='Updated due date').count(), 1,
            'logged once: the view no longer adds its own line')

        for task in following:
            task.refresh_from_db()
            self.assertIsNotNone(task.due_date)
            self.assertEqual(task.due_date_source, SYSTEM, task.task_name)
            self.assertEqual(_changed_rows(task), [], task.task_name)
        # The cascade's own per-task trail is untouched.
        self.assertTrue(DueDateChangeLog.objects.filter(task=following[0]).exists())


class FullRecalculationTests(rb.ResidentialBaselineBase):
    """Path 7, ruling C: both entry points into calculate_due_dates() produce the same
    dates as before SCHED-1, every one marked system, with no per-task row."""

    def _assert_recalculated(self, project):
        expected = _expected_recalc_dates(project)
        tasks = Task.objects.filter(phase__project=project)
        self.assertEqual(len(expected), tasks.count())
        for task in tasks:
            self.assertEqual(task.due_date, expected[task.pk], task.task_name)
            self.assertEqual(task.due_date_source, SYSTEM, task.task_name)
        self.assertFalse(ActivityLog.objects.filter(
            project=project, action_code='due_date_changed').exists())
        self.assertEqual(ActivityLog.objects.filter(
            project=project, action_code='due_dates_recalculated').count(), 1)

    def test_the_recalculate_dates_button(self):
        # A manual date beforehand: the full recalculation has always overwritten every
        # date, and now says so by marking it system.
        typed = self._task(self.project_a, 'AC Cable Work')
        Task.objects.filter(pk=typed.pk).update(due_date=date(2030, 1, 1),
                                                due_date_source=MANUAL)
        rb._client_for(self.pm_a).post(
            reverse('project_recalculate_dates', args=[self.project_a.project_id]))
        self._assert_recalculated(self.project_a)

    def test_switching_cascade_scheduling_on(self):
        settings_obj = SystemSettings.get()
        settings_obj.cascade_scheduling_enabled = True
        settings_obj.save(update_fields=['cascade_scheduling_enabled'])
        rb._client_for(self.pm_a).post(
            reverse('enable_cascade_scheduling', args=[self.project_a.project_id]))
        self.project_a.refresh_from_db()
        self.assertTrue(self.project_a.cascade_scheduling)
        self._assert_recalculated(self.project_a)


# ---------------------------------------------------------------------------
# Path 5 — Django admin
# ---------------------------------------------------------------------------

def _admin_post_data(form):
    """The POST body a browser would send for this admin form, unchanged."""
    data = {}
    for name, field in form.fields.items():
        value = form[name].value()
        if isinstance(field.widget, MultiWidget):
            parts = field.widget.decompress(value)
            for i, part in enumerate(parts):
                data[f'{name}_{i}'] = '' if part is None else str(part)
        elif isinstance(value, bool):
            if value:
                data[name] = 'on'
        elif isinstance(value, (list, tuple)):
            data[name] = [str(v) for v in value]
        elif value is None:
            data[name] = ''
        else:
            data[name] = value.isoformat() if hasattr(value, 'isoformat') else str(value)
    return data


class AdminTests(rb.ResidentialBaselineBase):

    def setUp(self):
        super().setUp()
        self.superuser = User.objects.create_superuser('sched1_su', 'su@example.com', 'pw12345')
        # AdminAccessMiddleware lets only an Admin-role profile reach /admin/.
        self.superuser.profile.role = 'Admin'
        self.superuser.profile.save(update_fields=['role'])
        self.client = Client()
        self.client.force_login(self.superuser)
        self.model_admin = django_admin.site._registry[Task]

    def _form_for(self, task):
        request = MagicMock(user=self.superuser)
        form_class = self.model_admin.get_form(request, task, change=task is not None)
        return form_class(instance=task)

    def test_due_date_source_is_read_only_and_the_date_keeps_its_place(self):
        task = self._task(self.project_a, 'AC Cable Work')
        form = self._form_for(task)
        self.assertNotIn('due_date_source', form.fields)
        self.assertNotIn('schedule_start', form.fields)
        self.assertIn('due_date', form.fields)
        fields = self.model_admin.get_fields(MagicMock(user=self.superuser), task)
        at = fields.index('duration_days')
        self.assertEqual(fields[at + 1:at + 3], ['due_date', 'due_date_source'])
        response = self.client.get(reverse('admin:projects_task_change', args=[task.pk]))
        self.assertEqual(response.status_code, 200)

    def test_an_admin_date_edit_is_manual_and_logged(self):
        task = self._task(self.project_a, 'AC Cable Work')
        Task.objects.filter(pk=task.pk).update(due_date=date(2026, 12, 1),
                                               due_date_source=SYSTEM)
        task.refresh_from_db()
        data = _admin_post_data(self._form_for(task))
        data['due_date'] = '2026-12-20'
        response = self.client.post(
            reverse('admin:projects_task_change', args=[task.pk]), data)
        self.assertEqual(response.status_code, 302, getattr(response, 'context_data', {})
                         and response.context_data.get('errors'))
        task.refresh_from_db()
        self.assertEqual((task.due_date, task.due_date_source), (date(2026, 12, 20), MANUAL))
        self.assertEqual(_changed_rows(task), [
            'Updated due date for task: AC Cable Work (was 2026-12-01, now 2026-12-20). '
            'Set in Django admin'])

    def test_an_admin_edit_that_leaves_the_date_alone_keeps_the_source(self):
        task = self._task(self.project_a, 'AC Cable Work')
        Task.objects.filter(pk=task.pk).update(due_date=date(2026, 12, 1),
                                               due_date_source=SYSTEM)
        task.refresh_from_db()
        data = _admin_post_data(self._form_for(task))
        data['task_name'] = 'AC Cable Work renamed'
        response = self.client.post(
            reverse('admin:projects_task_change', args=[task.pk]), data)
        self.assertEqual(response.status_code, 302)
        task.refresh_from_db()
        self.assertEqual(task.task_name, 'AC Cable Work renamed')
        self.assertEqual((task.due_date, task.due_date_source), (date(2026, 12, 1), SYSTEM))
        self.assertEqual(_changed_rows(task), [])

    def test_an_admin_add_with_a_date_is_manual_and_logged(self):
        phase = self._task(self.project_a, 'AC Cable Work').phase
        data = _admin_post_data(self._form_for(None))
        data.update({'phase': str(phase.pk), 'task_name': 'Admin added',
                     'task_order': '99', 'due_date': '2026-12-21'})
        response = self.client.post(reverse('admin:projects_task_add'), data)
        self.assertEqual(response.status_code, 302)
        task = Task.objects.get(task_name='Admin added')
        self.assertEqual((task.due_date, task.due_date_source), (date(2026, 12, 21), MANUAL))
        self.assertEqual(_changed_rows(task), [
            'Updated due date for task: Admin added (was not set, now 2026-12-21). '
            'Set in Django admin'])


# ---------------------------------------------------------------------------
# Path 4 — duplicate-for-locations (OPEX)
# ---------------------------------------------------------------------------

class DuplicateForLocationsTests(dup.DuplicateFixture):

    def test_a_copy_of_a_system_dated_task_is_manual(self):
        """Spec Layer 5: the copy must be manual, never system."""
        Task.objects.filter(pk=self.source.pk).update(due_date=date(2026, 12, 15),
                                                      due_date_source=SYSTEM)
        self._create(new='Block A\nBlock B')
        copies = list(Task.objects.filter(template_task_id=self.source.template_task_id,
                                          phase=self.source.phase)
                      .exclude(location_label=''))
        self.assertEqual(len(copies), 2)
        for copy in copies:
            self.assertEqual((copy.due_date, copy.due_date_source),
                             (date(2026, 12, 15), MANUAL), copy.task_name)
            self.assertEqual(_changed_rows(copy), [
                f'Updated due date for task: {copy.task_name} (was not set, now 2026-12-15). '
                f'Copied from task #{self.source.pk}'])
        self.source.refresh_from_db()
        self.assertEqual(self.source.due_date_source, SYSTEM, 'the source is not touched')

    def test_a_copy_of_an_undated_task_has_no_source(self):
        self._create(new='Block C')
        copy = Task.objects.get(location_label='Block C')
        self.assertEqual((copy.due_date, copy.due_date_source), (None, None))
        self.assertEqual(_changed_rows(copy), [])

    def test_a_failure_setting_the_date_leaves_no_undated_copy(self):
        Task.objects.filter(pk=self.source.pk).update(due_date=date(2026, 12, 15),
                                                      due_date_source=MANUAL)
        before = Task.objects.count()
        with patch('projects.views.set_task_due_date', side_effect=RuntimeError('boom')):
            with self.assertRaises(RuntimeError):
                self._create(new='Block D')
        self.assertEqual(Task.objects.count(), before)
        self.assertFalse(Task.objects.filter(location_label='Block D').exists())
