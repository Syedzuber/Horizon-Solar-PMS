"""
Coordinators as assignees on PM tasks (pre-flight of 26 Oct 2026, rulings R1-R5).

WHAT IS PINNED HERE
-------------------
views.task_assignee_candidates(task_role, project) is the one assignee rule. On a task
whose assigned_role is PM it offers every active PM (company-wide, unchanged) and THIS
project's active coordinators, labelled; never another project's coordinator, an
inactive one, or the site's QA/QC engineer. Every screen that picks an assignee asks it:
the overview's Assign modal, task_assign (list and POST), TaskAddForm and
duplicate-for-locations, so all four agree.

Removing a coordinator who holds open tasks on the project (assign_coordinators): their
PM-role tasks go back to the assigned PM, notified; with no assigned PM the edit is
refused whole; their Coordinator-role tasks stay, with a warning.

Run with:
    python manage.py test projects.tests_pm_task_coordinator_assignee --settings=solarpms.test_settings
"""
from unittest import mock

from django.contrib.messages import get_messages
from django.urls import reverse
from django.utils import timezone
from django.test import TestCase

from .forms import TaskAddForm
from .models import NotificationLog, Project, Task
from .reports import build_user_status_rows
from .tests_qaqc_assignment import _client_for, _profile, _seed_opex
from .utils import assign_task_to, assign_tasks_to, resolve_residential_template
from .views import _location_assignee_candidates, task_assignee_candidates


def _messages_with_level(response):
    return [(m.level_tag, str(m)) for m in get_messages(response.wsgi_request)]


class CoordinatorAssigneeFixture(TestCase):
    """Two activated OPEX sites.

    site_a: pm_a, coordinators coord_a and coord_idle (inactive).
    site_b: pm_b, coordinator coord_b.
    worker — Site Engineer holding every Site Engineer task on both sites.
    """

    @classmethod
    def setUpTestData(cls):
        resolve_residential_template()
        _seed_opex()
        cls.pm_a       = _profile('ca_pm_a', 'PM')
        cls.pm_b       = _profile('ca_pm_b', 'PM')
        cls.coord_a    = _profile('ca_coord_a', 'Project Coordinator')
        cls.coord_b    = _profile('ca_coord_b', 'Project Coordinator')
        cls.coord_idle = _profile('ca_coord_idle', 'Project Coordinator', is_active=False)
        cls.worker     = _profile('ca_worker', 'Site Engineer')
        cls.finance    = _profile('ca_fin', 'Finance')

    def setUp(self):
        # No HTTP leaves a test, whatever the notification switches say.
        post_patch = mock.patch('requests.post')
        post_patch.start()
        self.addCleanup(post_patch.stop)

        self.site_a = self._activated_site('CA Site Alpha', self.pm_a)
        self.site_a.coordinators.add(self.coord_a, self.coord_idle)
        self.site_b = self._activated_site('CA Site Bravo', self.pm_b)
        self.site_b.coordinators.add(self.coord_b)

    def _activated_site(self, name, pm):
        site = Project.objects.create(
            customer_name=name, customer_phone='9876543210', site_address='1 Assign Road',
            city='Lucknow', project_type='OPEX', dc_capacity_kw='100.00',
            status='Draft', assigned_pm=pm,
        )
        response = _client_for(pm).post(reverse('opex_site_activate', args=[site.project_id]))
        self.assertEqual(response.status_code, 302, 'OPEX activation did not redirect')
        site.refresh_from_db()
        assign_tasks_to(
            Task.objects.filter(phase__project=site, assigned_role=Task.SITE_ENGINEER,
                                is_mirror=False),
            self.worker,
        )
        return site

    def _pm_task(self, site):
        return Task.objects.get(phase__project=site, task_name='Net Metering Approval')

    def _se_task(self, site):
        return (Task.objects.filter(phase__project=site, assigned_to=self.worker)
                .order_by('pk').first())

    def _coord_role_task(self, site):
        return Task.objects.get(phase__project=site,
                                assigned_role=Task.PROJECT_COORDINATOR, is_mirror=False)

    def _pm_bucket(self, site, profile=None):
        response = _client_for(profile or site.assigned_pm).get(
            reverse('project_overview', args=[site.project_id]))
        self.assertEqual(response.status_code, 200)
        return response.context['candidates_by_role'][Task.PM]

    def _assign(self, site, task, profile, by=None):
        with self.captureOnCommitCallbacks(execute=True):
            return _client_for(by or site.assigned_pm).post(
                reverse('task_assign', args=[site.project_id, task.pk]),
                {'assigned_to': str(profile.pk)})


# ---------------------------------------------------------------------------
# The list: the overview modal and the helper
# ---------------------------------------------------------------------------

class CandidateListTests(CoordinatorAssigneeFixture):

    def test_pm_list_offers_this_projects_coordinator_after_the_pms_labelled(self):
        bucket = self._pm_bucket(self.site_a)
        pks = [c['pk'] for c in bucket]
        self.assertIn(self.coord_a.pk, pks)
        # The PM half is company-wide and unchanged: both PMs, first, unlabelled.
        self.assertEqual(pks[:2], [self.pm_a.pk, self.pm_b.pk])
        self.assertEqual(pks[-1], self.coord_a.pk)
        names = {c['pk']: c['name'] for c in bucket}
        self.assertEqual(names[self.coord_a.pk], 'Ca_Coord_A Test (Coordinator)')
        self.assertEqual(names[self.pm_a.pk], 'Ca_Pm_A Test')

    def test_pm_list_leaves_out_other_inactive_and_qaqc(self):
        # The site's QA/QC engineer may be a PM (QAQC_ENGINEER_ROLES); named through the
        # real screen, they leave the PM list here while staying on site_b's.
        engineer = _profile('ca_pm_engineer', 'PM')
        with self.captureOnCommitCallbacks(execute=True):
            _client_for(self.pm_a).post(reverse('site_qaqc', args=[self.site_a.project_id]),
                                        {'action': 'assign', 'engineer_id': engineer.pk})
        pks = [c['pk'] for c in self._pm_bucket(self.site_a)]
        self.assertNotIn(self.coord_b.pk, pks)
        self.assertNotIn(self.coord_idle.pk, pks)
        self.assertNotIn(engineer.pk, pks)
        self.assertIn(engineer.pk, [c['pk'] for c in self._pm_bucket(self.site_b)])

    def test_other_role_lists_offer_no_coordinator(self):
        se_pks = {p.pk for p in task_assignee_candidates(Task.SITE_ENGINEER, self.site_a)}
        self.assertEqual(se_pks, {self.worker.pk})
        # The Coordinator-role list stays company-wide, exactly as before.
        pc_pks = {p.pk for p in task_assignee_candidates(Task.PROJECT_COORDINATOR, self.site_a)}
        self.assertEqual(pc_pks, {self.coord_a.pk, self.coord_b.pk})

    def test_a_coordinator_offered_twice_appears_once(self):
        # A PM-role profile added to the M2M (limit_choices_to is form-level only)
        # is a role match, listed once and unlabelled.
        self.site_a.coordinators.add(self.pm_b)
        rows = list(task_assignee_candidates(Task.PM, self.site_a))
        self.assertEqual([p.pk for p in rows].count(self.pm_b.pk), 1)
        self.assertFalse(next(p for p in rows if p.pk == self.pm_b.pk).is_coordinator_candidate)

    def test_a_pm_role_mirror_offers_the_coordinator(self):
        mirror = Task.objects.filter(phase__project=self.site_a, is_mirror=True,
                                     assigned_role=Task.PM).first()
        self.assertIsNotNone(mirror, 'fixture: OPEX template has no PM-role mirror')
        self._assign(self.site_a, mirror, self.coord_a)
        mirror.refresh_from_db()
        self.assertEqual(mirror.assigned_to, self.coord_a)

    def test_standalone_page_labels_the_coordinator(self):
        response = _client_for(self.pm_a).get(
            reverse('task_assign', args=[self.site_a.project_id, self._pm_task(self.site_a).pk]))
        self.assertContains(response, 'Ca_Coord_A Test (Coordinator)')
        self.assertNotContains(response, 'Ca_Pm_A Test (Coordinator)')


# ---------------------------------------------------------------------------
# The POST
# ---------------------------------------------------------------------------

class TaskAssignPostTests(CoordinatorAssigneeFixture):

    def test_own_coordinator_accepted_on_a_pm_task_and_notified(self):
        task = self._pm_task(self.site_a)
        response = self._assign(self.site_a, task, self.coord_a)
        self.assertEqual(response.status_code, 302)
        task.refresh_from_db()
        self.assertEqual(task.assigned_to, self.coord_a)
        self.assertTrue(NotificationLog.objects.filter(
            recipient=self.coord_a, template_name='assign_task').exists())

    def test_a_coordinator_may_assign_to_themselves(self):
        task = self._pm_task(self.site_a)
        self._assign(self.site_a, task, self.coord_a, by=self.coord_a)
        task.refresh_from_db()
        self.assertEqual(task.assigned_to, self.coord_a)

    def test_another_projects_coordinator_is_404(self):
        task = self._pm_task(self.site_a)
        before = task.assigned_to_id
        response = self._assign(self.site_a, task, self.coord_b)
        self.assertEqual(response.status_code, 404)
        task.refresh_from_db()
        self.assertEqual(task.assigned_to_id, before)

    def test_inactive_coordinator_is_404(self):
        response = self._assign(self.site_a, self._pm_task(self.site_a), self.coord_idle)
        self.assertEqual(response.status_code, 404)

    def test_coordinator_on_a_site_engineer_task_is_404(self):
        task = self._se_task(self.site_a)
        response = self._assign(self.site_a, task, self.coord_a)
        self.assertEqual(response.status_code, 404)
        task.refresh_from_db()
        self.assertEqual(task.assigned_to, self.worker)


# ---------------------------------------------------------------------------
# TaskAddForm and duplicate-for-locations agree with the modal
# ---------------------------------------------------------------------------

class OtherPickersAgreeTests(CoordinatorAssigneeFixture):

    def _form(self, assignee):
        return TaskAddForm({
            'phase': self._pm_task(self.site_a).phase.pk, 'task_name': 'Extra liaison visit',
            'assigned_role': Task.PM, 'assigned_to': assignee.pk,
        }, project=self.site_a)

    def test_task_add_form_lists_and_accepts_what_the_modal_does(self):
        form = self._form(self.coord_a)
        self.assertTrue(form.is_valid(), form.errors)
        modal = [c['pk'] for c in self._pm_bucket(self.site_a)]
        self.assertEqual([p.pk for p in form.assignee_options], modal)

    def test_task_add_form_refuses_another_projects_coordinator(self):
        form = self._form(self.coord_b)
        self.assertFalse(form.is_valid())
        self.assertIn("one of this project's coordinators", ' '.join(form.errors['assigned_to']))

    def test_duplicate_candidates_equal_the_modal(self):
        modal = [c['pk'] for c in self._pm_bucket(self.site_a)]
        self.assertEqual(
            [p.pk for p in _location_assignee_candidates(self._pm_task(self.site_a))], modal)

    def test_duplicate_for_locations_accepts_own_and_refuses_other_coordinator(self):
        source = self._pm_task(self.site_a)
        url = reverse('task_duplicate_locations_create', args=[self.site_a.project_id, source.pk])
        with self.captureOnCommitCallbacks(execute=True):
            _client_for(self.pm_a).post(url, {'new_locations': 'Block B',
                                              'assigned_to': self.coord_b.pk})
        self.assertFalse(Task.objects.filter(phase__project=self.site_a,
                                             location_label='Block B').exists())
        with self.captureOnCommitCallbacks(execute=True):
            _client_for(self.pm_a).post(url, {'new_locations': 'Block A',
                                              'assigned_to': self.coord_a.pk})
        copy = Task.objects.get(phase__project=self.site_a, location_label='Block A')
        self.assertEqual(copy.assigned_to, self.coord_a)


# ---------------------------------------------------------------------------
# Removing a coordinator who holds tasks (R5)
# ---------------------------------------------------------------------------

class CoordinatorRemovalTests(CoordinatorAssigneeFixture):

    def _remove_coord_a(self, by=None, keep=()):
        with self.captureOnCommitCallbacks(execute=True):
            return _client_for(by or self.pm_a).post(
                reverse('assign_coordinators', args=[self.site_a.project_id]),
                {'coordinator_ids': [str(p.pk) for p in keep]})

    def test_pm_task_moves_to_the_assigned_pm_with_a_notification(self):
        # Removed by another coordinator, so the PM is told: assign_task_to() tells
        # nobody about a task they gave themselves (T3b rulings Q1/Q2).
        second = _profile('ca_coord_a2', 'Project Coordinator')
        self.site_a.coordinators.add(second)
        task = self._pm_task(self.site_a)
        self._assign(self.site_a, task, self.coord_a)
        NotificationLog.objects.all().delete()

        response = self._remove_coord_a(by=second, keep=(second,))

        task.refresh_from_db()
        self.assertEqual(task.assigned_to, self.pm_a)
        self.assertFalse(self.site_a.coordinators.filter(pk=self.coord_a.pk).exists())
        self.assertTrue(NotificationLog.objects.filter(
            recipient=self.pm_a, template_name='assign_task').exists())
        self.assertIn(('success', 'PM tasks moved to Ca_Pm_A Test: Net Metering Approval.'),
                      _messages_with_level(response))

    def test_pm_removing_the_coordinator_takes_the_task_without_a_self_notice(self):
        task = self._pm_task(self.site_a)
        self._assign(self.site_a, task, self.coord_a)
        NotificationLog.objects.all().delete()
        self._remove_coord_a()
        task.refresh_from_db()
        self.assertEqual(task.assigned_to, self.pm_a)
        self.assertFalse(NotificationLog.objects.filter(recipient=self.pm_a).exists())

    def test_done_task_is_left_alone(self):
        task = self._pm_task(self.site_a)
        assign_task_to(task, self.coord_a)
        Task.objects.filter(pk=task.pk).update(status=Task.DONE)
        self._remove_coord_a()
        task.refresh_from_db()
        self.assertEqual(task.assigned_to, self.coord_a)

    def test_refused_whole_when_there_is_no_assigned_pm(self):
        second = _profile('ca_coord_a2', 'Project Coordinator')
        self.site_a.coordinators.add(second)
        task = self._pm_task(self.site_a)
        assign_task_to(task, self.coord_a)
        Project.objects.filter(pk=self.site_a.pk).update(assigned_pm=None)

        response = self._remove_coord_a(by=second, keep=(second, self.coord_idle))

        task.refresh_from_db()
        self.assertEqual(task.assigned_to, self.coord_a)
        self.assertTrue(self.site_a.coordinators.filter(pk=self.coord_a.pk).exists())
        levels_and_text = _messages_with_level(response)
        self.assertEqual(len(levels_and_text), 1)
        level, text = levels_and_text[0]
        self.assertEqual(level, 'danger')   # MESSAGE_TAGS maps ERROR to Bootstrap's danger
        self.assertIn('Net Metering Approval (Ca_Coord_A Test)', text)
        self.assertIn('Nothing was changed.', text)

    def test_coordinator_role_task_stays_with_a_warning(self):
        task = self._coord_role_task(self.site_a)
        assign_task_to(task, self.coord_a)

        response = self._remove_coord_a()

        task.refresh_from_db()
        self.assertEqual(task.assigned_to, self.coord_a)
        self.assertFalse(self.site_a.coordinators.filter(pk=self.coord_a.pk).exists())
        warnings = [text for level, text in _messages_with_level(response) if level == 'warning']
        self.assertEqual(len(warnings), 1)
        self.assertIn(f'{task.task_name} (Ca_Coord_A Test)', warnings[0])


# ---------------------------------------------------------------------------
# The CEO report counts the task under the coordinator
# ---------------------------------------------------------------------------

class UserStatusReportTests(CoordinatorAssigneeFixture):

    def _tasks_assigned(self, profile):
        rows = build_user_status_rows(timezone.localdate())['rows']
        return next((r['tasks_assigned'] for r in rows if r['profile'] == profile), 0)

    def test_task_counts_under_the_coordinator(self):
        pm_before = self._tasks_assigned(self.pm_a)
        self._assign(self.site_a, self._pm_task(self.site_a), self.coord_a)
        self.assertEqual(self._tasks_assigned(self.coord_a), 1)
        self.assertEqual(self._tasks_assigned(self.pm_a), pm_before - 1)
