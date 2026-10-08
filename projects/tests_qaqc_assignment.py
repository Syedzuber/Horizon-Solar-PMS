"""
QA/QC engineer per site — closeout step 3 (docs/CLOSEOUT_SPEC.md CL-9, CL-A, CL-D).

WHAT IS PINNED HERE
-------------------
A PM or coordinator names one active Site Engineer as an OPEX site's QA/QC engineer
(SiteQaqcAssignment). The assignment, not the `is_qaqc` flag, grants that engineer:

    - view of the site            (user_can_view_project → user_is_site_qaqc)
    - approve / reject its tasks  (user_can_approve_task  → user_is_site_qaqc)
    - a seat in the approver pool (task_has_independent_approver)

and nothing else. The role-match gates (status, due date, checklist answers, GRN)
also ask user_works_on_site(), which is view minus that grant, so the engineer sees
the site's work without being able to do it.

CL-A both ends: an engineer holding a task on the site cannot be named, and the named
engineer cannot then be given a task there.

The NEGATIVE half — the engineer refused on a site they are not assigned to, refused
again the moment the assignment ends, and refused on every do-the-work endpoint — is in
tests_access_isolation.QaqcAssignmentIsolationTests, beside the other refusals. This
file holds what the engineer and the managers CAN do, and the rules around it.

EVERY SITE HERE IS REALLY ACTIVATED through opex_site_activate, and every assignment is
made through the real screen, so the tests exercise the writer, its lock and its notices.

Run with:
    python manage.py test projects.tests_qaqc_assignment --settings=solarpms.test_settings
"""
from datetime import date, timedelta
from decimal import Decimal
from importlib import import_module

from django.contrib.auth.models import User
from django.contrib.messages import get_messages
from django.db import IntegrityError, connection, transaction
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from .admin import TaskAdminForm
from .forms import TaskAddForm
from .models import (
    AppendOnlyViolation, Issue, NotificationLog, Project, PunchPoint, SiteQaqcAssignment, Task,
    TaskTemplate, TaskTemplatePhase, TaskTemplateTask, UserProfile,
)
from .permissions import (
    site_qaqc_engineer_id, task_has_independent_approver, user_can_approve_task,
    user_can_manage_project, user_can_view_project, user_is_site_qaqc,
    user_may_self_certify, user_works_on_site,
)
from .utils import (
    RESIDENTIAL_FINANCE_ASSIGNEE_EMAIL, assign_task_to, assign_tasks_to,
    resolve_residential_template,
)


class _ConcreteApps:
    """Stands in for the `apps` registry the 0075 seed's RunPython function is handed.
    Same shape as tests_two_step_completion.py."""

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


def _profile(username, role, email='', **flags):
    """A user whose signal-created profile carries `role` (and any flags)."""
    user = User.objects.create_user(
        username=username, password='x', email=email,
        first_name=username.title(), last_name='Test',
    )
    profile = user.profile
    profile.role = role
    profile.is_active = True
    for field, value in flags.items():
        setattr(profile, field, value)
    profile.save()
    return profile


def _client_for(profile):
    client = Client()
    client.force_login(profile.user)
    return client


def _messages(response):
    return [str(m) for m in get_messages(response.wsgi_request)]


class QaqcFixture(TestCase):
    """Two activated OPEX sites with disjoint managers, and the people around them.

    site_a: pm_a + coord_a.   site_b: pm_b.
    worker  — Site Engineer holding every Site Engineer task on BOTH sites
    qaqc    — Site Engineer holding no task anywhere: the engineer under test
    spare   — a second free Site Engineer, for replacement
    legacy  — Site Engineer with is_qaqc=True, holding no task (the Q-0 shape)

    No test methods here: tests_access_isolation imports this fixture too.
    """

    @classmethod
    def setUpTestData(cls):
        resolve_residential_template()   # bootstraps RESIDENTIAL v1 on a virgin DB
        _seed_opex()

        cls.pm_a    = _profile('qq_pm_a', 'PM')
        cls.coord_a = _profile('qq_coord_a', 'Project Coordinator')
        cls.pm_b    = _profile('qq_pm_b', 'PM')
        cls.worker  = _profile('qq_worker', 'Site Engineer')
        cls.qaqc    = _profile('qq_qaqc', 'Site Engineer')
        cls.spare   = _profile('qq_spare', 'Site Engineer')
        cls.legacy  = _profile('qq_legacy', 'Site Engineer', is_qaqc=True)
        cls.design  = _profile('qq_design', 'Design')
        cls.scm     = _profile('qq_scm', 'SCM')
        cls.finance = _profile('qq_fin', 'Finance', email=RESIDENTIAL_FINANCE_ASSIGNEE_EMAIL)

    def setUp(self):
        self.site_a = self._activated_site('QA Site Alpha', self.pm_a)
        self.site_a.coordinators.add(self.coord_a)
        self.site_b = self._activated_site('QA Site Bravo', self.pm_b)

    def _activated_site(self, name, pm):
        site = Project.objects.create(
            customer_name=name, customer_phone='9876543210', site_address='1 Check Road',
            city='Lucknow', project_type='OPEX', dc_capacity_kw=Decimal('100.00'),
            status='Draft', assigned_pm=pm,
        )
        response = _client_for(pm).post(reverse('opex_site_activate', args=[site.project_id]))
        self.assertEqual(response.status_code, 302, 'OPEX activation did not redirect')
        site.refresh_from_db()
        self.assertEqual(site.status, 'Active')
        assign_tasks_to(
            Task.objects.filter(phase__project=site, assigned_role=Task.SITE_ENGINEER,
                                is_mirror=False),
            self.worker,
        )
        return site

    # -- helpers ---------------------------------------------------------------

    def _assign(self, site, engineer, by=None):
        """Assign through the real screen, running the after-commit notices."""
        with self.captureOnCommitCallbacks(execute=True):
            return _client_for(by or site.assigned_pm).post(
                reverse('site_qaqc', args=[site.project_id]),
                {'action': 'assign', 'engineer_id': engineer.pk})

    def _end(self, site, by=None):
        with self.captureOnCommitCallbacks(execute=True):
            return _client_for(by or site.assigned_pm).post(
                reverse('site_qaqc', args=[site.project_id]), {'action': 'end'})

    def _se_task(self, site, index=0):
        return list(Task.objects.filter(phase__project=site, assigned_to=self.worker)
                    .order_by('phase__phase_order', 'task_order', 'pk'))[index]

    def _pm_task(self, site):
        return Task.objects.get(phase__project=site, task_name='Net Metering Approval')

    def _in_progress(self, site, task, actor):
        response = _client_for(actor).post(
            reverse('task_status_update', args=[site.project_id, task.pk]),
            {'status': Task.IN_PROGRESS,
             'due_date': (date.today() + timedelta(days=7)).isoformat()})
        self.assertIn(response.status_code, (200, 302))
        task.refresh_from_db()
        self.assertEqual(task.status, Task.IN_PROGRESS, 'fixture: not In Progress')

    def _submitted(self, site, index=0):
        """A Site Engineer task the worker has started and submitted for approval."""
        task = self._se_task(site, index)
        self._in_progress(site, task, self.worker)
        _client_for(self.worker).post(
            reverse('task_submit_for_approval', args=[site.project_id, task.pk]),
            {'submission_remarks': 'Work complete.'})
        task.refresh_from_db()
        self.assertTrue(task.is_awaiting_approval, 'fixture: task not awaiting approval')
        return task

    def _overview(self, site, profile):
        return _client_for(profile).get(reverse('project_overview', args=[site.project_id]))


# ---------------------------------------------------------------------------
# 1. The assigned engineer, on their site
# ---------------------------------------------------------------------------

class AssignedEngineerCanTests(QaqcFixture):
    """Test 1, positive half: view, approve, reject. The refusals are the tripwire in
    tests_access_isolation."""

    def setUp(self):
        super().setUp()
        self._assign(self.site_a, self.qaqc)

    def test_the_engineer_holds_no_task_on_the_site(self):
        self.assertFalse(Task.objects.filter(phase__project=self.site_a,
                                             assigned_to=self.qaqc).exists())

    def test_can_view_the_site_and_its_tasks(self):
        self.assertTrue(user_can_view_project(self.qaqc.user, self.site_a))
        self.assertEqual(self._overview(self.site_a, self.qaqc).status_code, 200)
        task = self._se_task(self.site_a)
        response = _client_for(self.qaqc).get(
            reverse('task_detail', args=[self.site_a.project_id, task.pk]))
        self.assertEqual(response.status_code, 200)

    def test_task_detail_offers_the_verdict_and_no_submit(self):
        task = self._submitted(self.site_a)
        response = _client_for(self.qaqc).get(
            reverse('task_detail', args=[self.site_a.project_id, task.pk]))
        self.assertTrue(response.context['can_approve_task'])
        self.assertFalse(response.context['can_submit_task'])
        self.assertFalse(response.context['can_waive_punch_point'])

    def test_can_approve_a_submitted_task(self):
        task = self._submitted(self.site_a)
        response = _client_for(self.qaqc).post(
            reverse('task_approve', args=[self.site_a.project_id, task.pk]),
            {'approval_remarks': 'Checked on site.'})
        self.assertEqual(response.status_code, 302)
        task.refresh_from_db()
        self.assertEqual(task.status, Task.DONE)
        self.assertEqual(task.approved_by, self.qaqc)

    def test_can_reject_a_submitted_task_and_raise_a_punch_point(self):
        task = self._submitted(self.site_a)
        response = _client_for(self.qaqc).post(
            reverse('task_reject', args=[self.site_a.project_id, task.pk]),
            {'approval_remarks': 'Earthing pit not to drawing.'})
        self.assertEqual(response.status_code, 302)
        task.refresh_from_db()
        self.assertFalse(task.is_awaiting_approval)
        point = PunchPoint.objects.get(task=task)
        self.assertEqual(point.raised_by, self.qaqc)

    def test_overview_rows_draw_no_role_matched_controls(self):
        """user_task_role is None for the engineer, so _task_row.html draws neither the
        status select nor the due-date editor on the Site Engineer rows."""
        response = self._overview(self.site_a, self.qaqc)
        self.assertIsNone(response.context['user_task_role'])
        task = self._se_task(self.site_a)
        self.assertNotContains(
            response, reverse('task_status_update', args=[self.site_a.project_id, task.pk]))
        self.assertNotContains(
            response, reverse('task_set_due_date', args=[self.site_a.project_id, task.pk]))

    def test_the_worker_still_gets_the_controls(self):
        response = self._overview(self.site_a, self.worker)
        self.assertEqual(response.context['user_task_role'], Task.SITE_ENGINEER)

    def test_issues_and_uploads_stay_open_to_the_engineer(self):
        """Q-B: an inspector must be able to raise and photograph defects."""
        response = _client_for(self.qaqc).post(
            reverse('create_project_issue', args=[self.site_a.project_id]),
            {'title': 'Loose MC4 connector', 'severity': Issue.MEDIUM,
             'assigned_to': str(self.pm_a.pk)})
        self.assertIn(response.status_code, (200, 302))
        self.assertTrue(self.site_a.issues.filter(title='Loose MC4 connector').exists())


# ---------------------------------------------------------------------------
# 3. Ending and replacing take effect at once (positive half)
# ---------------------------------------------------------------------------

class ReplaceAndEndTests(QaqcFixture):

    def test_replace_moves_access_and_keeps_history(self):
        self._assign(self.site_a, self.qaqc)
        self._assign(self.site_a, self.spare)
        self.assertFalse(user_can_view_project(self.qaqc.user, self.site_a))
        self.assertTrue(user_can_view_project(self.spare.user, self.site_a))
        rows = list(SiteQaqcAssignment.objects.filter(project=self.site_a)
                    .order_by('assigned_at', 'pk'))
        self.assertEqual([r.engineer for r in rows], [self.qaqc, self.spare])
        self.assertEqual(rows[0].end_reason, SiteQaqcAssignment.END_REPLACED)
        self.assertEqual(rows[0].ended_by, self.pm_a)
        self.assertIsNone(rows[1].ended_at)

    def test_end_stamps_who_when_why(self):
        self._assign(self.site_a, self.qaqc)
        self._end(self.site_a, by=self.coord_a)
        row = SiteQaqcAssignment.objects.get(project=self.site_a)
        self.assertEqual(row.end_reason, SiteQaqcAssignment.END_ENDED)
        self.assertEqual(row.ended_by, self.coord_a)
        self.assertIsNotNone(row.ended_at)
        self.assertIsNone(site_qaqc_engineer_id(self.site_a))

    def test_ending_with_nothing_to_end_is_refused_with_a_message(self):
        response = self._end(self.site_a)
        self.assertIn('no QA/QC engineer to end', ' '.join(_messages(response)))

    def test_assigning_the_same_engineer_twice_is_refused(self):
        self._assign(self.site_a, self.qaqc)
        response = self._assign(self.site_a, self.qaqc)
        self.assertIn('already', ' '.join(_messages(response)))
        self.assertEqual(SiteQaqcAssignment.objects.filter(project=self.site_a).count(), 1)

    def test_role_change_stops_access_and_flags_the_card(self):
        """Deactivation / role change: access stops, the row stays, the card says so."""
        self._assign(self.site_a, self.qaqc)
        UserProfile.objects.filter(pk=self.qaqc.pk).update(role='Design')
        self.assertFalse(user_is_site_qaqc(User.objects.get(pk=self.qaqc.user.pk), self.site_a))
        self.assertIsNone(SiteQaqcAssignment.objects.get(project=self.site_a).ended_at)
        response = self._overview(self.site_a, self.pm_a)
        self.assertTrue(response.context['qaqc_stale'])
        self.assertContains(response, 'No longer an active site engineer')

    def test_deactivated_profile_stops_access(self):
        self._assign(self.site_a, self.qaqc)
        UserProfile.objects.filter(pk=self.qaqc.pk).update(is_active=False)
        self.assertFalse(user_can_view_project(User.objects.get(pk=self.qaqc.user.pk),
                                               self.site_a))


# ---------------------------------------------------------------------------
# 4. CL-A at assignment: an engineer holding a task is refused
# ---------------------------------------------------------------------------

class ClaAtAssignmentTests(QaqcFixture):

    def test_an_engineer_holding_a_task_is_refused_and_the_tasks_are_named(self):
        response = self._assign(self.site_a, self.worker)
        joined = ' '.join(_messages(response))
        self.assertIn('holds', joined)
        self.assertIn(self._se_task(self.site_a).task_name, joined)
        self.assertFalse(SiteQaqcAssignment.objects.filter(project=self.site_a).exists())

    def test_a_done_task_still_disqualifies(self):
        """'Holds a task' includes Done (and mirror and N/A) rows: the person did the work."""
        task = self._pm_task(self.site_a)
        assign_task_to(task, self.spare)
        Task.objects.filter(pk=task.pk).update(status=Task.DONE)
        response = self._assign(self.site_a, self.spare)
        self.assertIn('holds 1 task', ' '.join(_messages(response)))
        self.assertFalse(SiteQaqcAssignment.objects.filter(project=self.site_a).exists())

    def test_the_eligible_list_leaves_task_holders_out(self):
        response = _client_for(self.pm_a).get(reverse('site_qaqc', args=[self.site_a.project_id]))
        eligible = set(response.context['eligible'])
        self.assertIn(self.qaqc, eligible)
        self.assertIn(self.spare, eligible)
        self.assertNotIn(self.worker, eligible)

    def test_an_inactive_engineer_is_refused(self):
        UserProfile.objects.filter(pk=self.spare.pk).update(is_active=False)
        response = self._assign(self.site_a, UserProfile.objects.get(pk=self.spare.pk))
        self.assertIn('not an active site engineer', ' '.join(_messages(response)))

    def test_a_non_site_engineer_is_refused(self):
        response = self._assign(self.site_a, self.design)
        self.assertIn('not an active site engineer', ' '.join(_messages(response)))


# ---------------------------------------------------------------------------
# 5. CL-A, the other end: the engineer cannot then be given a task on the site
# ---------------------------------------------------------------------------

class ClaOtherEndTests(QaqcFixture):

    MESSAGE = "is this site's QA/QC engineer. End or replace that assignment first."

    def setUp(self):
        super().setUp()
        self._assign(self.site_a, self.qaqc)
        self.task = self._se_task(self.site_a)

    def test_task_assign_refuses_with_the_message(self):
        response = _client_for(self.pm_a).post(
            reverse('task_assign', args=[self.site_a.project_id, self.task.pk]),
            {'assigned_to': self.qaqc.pk})
        self.assertIn(self.MESSAGE, ' '.join(_messages(response)))
        self.task.refresh_from_db()
        self.assertEqual(self.task.assigned_to, self.worker)

    def test_task_assign_htmx_modal_refuses_and_closes(self):
        response = _client_for(self.pm_a).post(
            reverse('task_assign', args=[self.site_a.project_id, self.task.pk]),
            {'assigned_to': self.qaqc.pk}, HTTP_HX_REQUEST='true')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['HX-Trigger'], 'taskAssigned')
        self.assertContains(response, 'End or replace that assignment first.')
        self.task.refresh_from_db()
        self.assertEqual(self.task.assigned_to, self.worker)

    def test_candidate_lists_leave_the_engineer_out(self):
        response = _client_for(self.pm_a).get(
            reverse('task_assign', args=[self.site_a.project_id, self.task.pk]))
        self.assertNotIn(self.qaqc, list(response.context['candidates']))
        self.assertIn(self.spare, list(response.context['candidates']))
        overview = self._overview(self.site_a, self.pm_a)
        se_pks = {c['pk'] for c in overview.context['candidates_by_role'][Task.SITE_ENGINEER]}
        self.assertNotIn(self.qaqc.pk, se_pks)
        self.assertIn(self.spare.pk, se_pks)

    def test_the_engineer_is_still_assignable_on_another_site(self):
        task_b = self._se_task(self.site_b)
        _client_for(self.pm_b).post(
            reverse('task_assign', args=[self.site_b.project_id, task_b.pk]),
            {'assigned_to': self.qaqc.pk})
        task_b.refresh_from_db()
        self.assertEqual(task_b.assigned_to, self.qaqc)

    def test_task_add_form_refuses(self):
        form = TaskAddForm({
            'phase': self.task.phase.pk, 'task_name': 'Extra inspection walk',
            'assigned_role': Task.SITE_ENGINEER, 'assigned_to': self.qaqc.pk,
        }, project=self.site_a)
        self.assertFalse(form.is_valid())
        self.assertIn(self.MESSAGE, ' '.join(form.errors['assigned_to']))
        self.assertNotIn(self.qaqc, list(form.assignee_options))

    def test_duplicate_for_locations_refuses(self):
        response = _client_for(self.pm_a).post(
            reverse('task_duplicate_locations_create',
                    args=[self.site_a.project_id, self.task.pk]),
            {'new_locations': 'Parking', 'assigned_to': self.qaqc.pk})
        self.assertIn(self.MESSAGE, ' '.join(_messages(response)))
        self.assertFalse(Task.objects.filter(phase__project=self.site_a,
                                             assigned_to=self.qaqc).exists())

    def test_task_admin_form_refuses_a_change_to_the_engineer(self):
        form = TaskAdminForm(instance=self.task)
        data = {name: form.initial.get(name) for name in form.fields}
        data = {k: ('' if v is None else v) for k, v in data.items()}
        data['assigned_to'] = self.qaqc.pk
        bound = TaskAdminForm(data, instance=self.task)
        bound.is_valid()
        self.assertIn(self.MESSAGE, ' '.join(bound.errors.get('assigned_to', [])))

    def test_after_ending_the_engineer_is_assignable_again(self):
        self._end(self.site_a)
        _client_for(self.pm_a).post(
            reverse('task_assign', args=[self.site_a.project_id, self.task.pk]),
            {'assigned_to': self.qaqc.pk})
        self.task.refresh_from_db()
        self.assertEqual(self.task.assigned_to, self.qaqc)


# ---------------------------------------------------------------------------
# 6. Who may assign
# ---------------------------------------------------------------------------

class WhoMayAssignTests(QaqcFixture):

    def _screen(self, profile):
        return _client_for(profile).get(reverse('site_qaqc', args=[self.site_a.project_id]))

    def test_the_pm_and_the_coordinator_may(self):
        screen = self._screen(self.pm_a)
        self.assertEqual(screen.status_code, 200)
        self.assertNotContains(screen, 'Closeout step 3')   # template comment did not leak
        self.assertEqual(self._screen(self.coord_a).status_code, 200)
        self._assign(self.site_a, self.qaqc, by=self.coord_a)
        self.assertEqual(site_qaqc_engineer_id(self.site_a), self.qaqc.pk)

    def test_others_are_refused(self):
        # PM of another site and Design have no sight of the site: 404. The worker
        # SE, SCM and Finance can see it but do not manage it: 403.
        expected = {self.pm_b: 404, self.design: 404, self.worker: 403,
                    self.scm: 403, self.finance: 403}
        for profile, status in expected.items():
            with self.subTest(role=profile.role, user=profile.user.username):
                self.assertEqual(self._screen(profile).status_code, status)
                response = _client_for(profile).post(
                    reverse('site_qaqc', args=[self.site_a.project_id]),
                    {'action': 'assign', 'engineer_id': self.qaqc.pk})
                self.assertEqual(response.status_code, status)
        self.assertFalse(SiteQaqcAssignment.objects.exists())

    def test_the_engineer_cannot_end_their_own_assignment(self):
        self._assign(self.site_a, self.qaqc)
        response = _client_for(self.qaqc).post(
            reverse('site_qaqc', args=[self.site_a.project_id]), {'action': 'end'})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(site_qaqc_engineer_id(self.site_a), self.qaqc.pk)

    def test_the_card_shows_everyone_and_the_link_only_to_managers(self):
        self._assign(self.site_a, self.qaqc)
        link = reverse('site_qaqc', args=[self.site_a.project_id])
        pm_page = self._overview(self.site_a, self.pm_a)
        self.assertContains(pm_page, 'id="qaqc-engineer-pill"')
        self.assertContains(pm_page, link)
        self.assertNotContains(pm_page, 'Closeout step 3')   # template comment did not leak
        worker_page = self._overview(self.site_a, self.worker)
        self.assertContains(worker_page, 'id="qaqc-engineer-pill"')
        self.assertNotContains(worker_page, link)


# ---------------------------------------------------------------------------
# 7. The database holds one active assignment per site
# ---------------------------------------------------------------------------

class DatabaseConstraintTests(QaqcFixture):

    def test_a_second_active_row_is_refused_by_the_database(self):
        SiteQaqcAssignment.objects.create(project=self.site_a, engineer=self.qaqc,
                                          assigned_by=self.pm_a)
        with self.assertRaises(IntegrityError), transaction.atomic():
            SiteQaqcAssignment.objects.create(project=self.site_a, engineer=self.spare,
                                              assigned_by=self.pm_a)

    def test_ended_rows_do_not_count(self):
        now = timezone.now()
        SiteQaqcAssignment.objects.create(
            project=self.site_a, engineer=self.qaqc, assigned_by=self.pm_a,
            assigned_at=now - timedelta(days=2), ended_at=now - timedelta(days=1),
            ended_by=self.pm_a, end_reason=SiteQaqcAssignment.END_ENDED)
        SiteQaqcAssignment.objects.create(project=self.site_a, engineer=self.spare,
                                          assigned_by=self.pm_a)
        self.assertEqual(site_qaqc_engineer_id(self.site_a), self.spare.pk)

    def test_half_ended_rows_are_refused(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            SiteQaqcAssignment.objects.create(
                project=self.site_a, engineer=self.qaqc, assigned_by=self.pm_a,
                ended_at=timezone.now())

    def test_rows_are_append_only(self):
        row = SiteQaqcAssignment.objects.create(project=self.site_a, engineer=self.qaqc,
                                                assigned_by=self.pm_a)
        with self.assertRaises(AppendOnlyViolation):
            row.save()
        with self.assertRaises(AppendOnlyViolation):
            row.delete()


# ---------------------------------------------------------------------------
# 8. The approver pool counts the engineer
# ---------------------------------------------------------------------------

class ApproverPoolTests(QaqcFixture):

    def test_without_an_engineer_the_pm_self_certifies(self):
        """The 0088 behaviour, unchanged: the PM is the only approver."""
        self.assertFalse(task_has_independent_approver(self.site_b, self.pm_b))
        self.assertTrue(user_may_self_certify(self.pm_b.user, self.site_b))

    def test_the_engineer_is_counted_so_the_pm_does_not_self_certify(self):
        self._assign(self.site_b, self.qaqc)
        self.assertTrue(task_has_independent_approver(self.site_b, self.pm_b))
        self.assertFalse(user_may_self_certify(self.pm_b.user, self.site_b))

    def test_a_pm_submission_waits_for_the_engineer(self):
        self._assign(self.site_b, self.qaqc)
        task = self._pm_task(self.site_b)
        self._in_progress(self.site_b, task, self.pm_b)
        _client_for(self.pm_b).post(
            reverse('task_submit_for_approval', args=[self.site_b.project_id, task.pk]),
            {'submission_remarks': 'Approval received from DISCOM.'})
        task.refresh_from_db()
        self.assertTrue(task.is_awaiting_approval, 'the PM self-certified past the engineer')
        _client_for(self.qaqc).post(
            reverse('task_approve', args=[self.site_b.project_id, task.pk]),
            {'approval_remarks': 'Letter seen.'})
        task.refresh_from_db()
        self.assertEqual(task.status, Task.DONE)
        self.assertEqual(task.approved_by, self.qaqc)

    def test_an_ended_engineer_no_longer_counts(self):
        self._assign(self.site_b, self.qaqc)
        self._end(self.site_b)
        self.assertFalse(task_has_independent_approver(self.site_b, self.pm_b))


# ---------------------------------------------------------------------------
# 9. Residential: no screen, no card, no query
# ---------------------------------------------------------------------------

class ResidentialUntouchedTests(QaqcFixture):

    def setUp(self):
        super().setUp()
        self.home = Project.objects.create(
            customer_name='Home Roof', customer_phone='9876543210', site_address='2 Roof Lane',
            city='Lucknow', project_type='Residential', dc_capacity_kw=Decimal('5.00'),
            contract_value=Decimal('300000.00'), status='Draft', assigned_pm=self.pm_a,
            target_commissioning_date=date.today() + timedelta(days=90),
        )

    def test_no_assign_screen(self):
        response = _client_for(self.pm_a).get(reverse('site_qaqc', args=[self.home.project_id]))
        self.assertEqual(response.status_code, 404)

    def test_no_card(self):
        response = self._overview(self.home, self.pm_a)
        self.assertFalse(response.context['show_qaqc_card'])
        self.assertNotContains(response, 'id="qaqc-engineer-pill"')

    def test_the_row_reader_spends_no_query(self):
        with self.assertNumQueries(0):
            self.assertIsNone(site_qaqc_engineer_id(self.home))
        with self.assertNumQueries(0):
            self.assertFalse(user_is_site_qaqc(self.qaqc.user, self.home))

    def test_the_writer_refuses_a_residential_site(self):
        from .qaqc_views import QaqcAssignmentRefused, assign_site_qaqc
        with self.assertRaises(QaqcAssignmentRefused):
            assign_site_qaqc(self.home, self.qaqc, self.pm_a)


# ---------------------------------------------------------------------------
# 10. is_qaqc is unchanged
# ---------------------------------------------------------------------------

class LegacyFlagUnchangedTests(QaqcFixture):
    """Q-0: the flag stays exactly as it was — ANDed with visibility, no writer added."""

    def test_flag_without_a_task_or_assignment_grants_nothing(self):
        self.assertFalse(user_can_view_project(self.legacy.user, self.site_a))
        self.assertFalse(user_can_approve_task(self.legacy.user, self.site_a))

    def test_flag_with_a_task_still_approves(self):
        assign_task_to(self._pm_task(self.site_a), self.legacy)
        self.assertTrue(user_can_approve_task(self.legacy.user, self.site_a))

    def test_assignment_does_not_need_the_flag(self):
        self.assertFalse(self.qaqc.is_qaqc)
        self._assign(self.site_a, self.qaqc)
        self.assertTrue(user_can_approve_task(self.qaqc.user, self.site_a))
        self.assertFalse(UserProfile.objects.get(pk=self.qaqc.pk).is_qaqc,
                         'assigning must not write the legacy flag')


# ---------------------------------------------------------------------------
# 11. Query cost does not grow per row
# ---------------------------------------------------------------------------

class QueryCostTests(QaqcFixture):

    def _count(self, profile):
        with CaptureQueriesContext(connection) as ctx:
            response = self._overview(self.site_a, profile)
        self.assertEqual(response.status_code, 200)
        return len(ctx.captured_queries)

    def test_the_row_reader_and_the_helper_cost_one_query(self):
        self._assign(self.site_a, self.qaqc)
        user = User.objects.get(pk=self.qaqc.user.pk)
        user.profile  # load the profile outside the count
        with self.assertNumQueries(1):
            self.assertTrue(user_is_site_qaqc(user, self.site_a))

    def test_view_for_a_task_holder_costs_no_extra_query(self):
        """The grant is asked second, so an engineer holding a task pays exactly what
        they paid before: the one task-holding query."""
        self._assign(self.site_a, self.qaqc)
        user = User.objects.get(pk=self.worker.user.pk)
        user.profile
        # coordinators exists + task exists; the assigned_pm compare reads the FK the
        # fixture's instance already holds. No third query for the QA/QC grant.
        with self.assertNumQueries(2):
            self.assertTrue(user_can_view_project(user, self.site_a))

    def test_overview_growth_per_row_is_the_same_for_the_engineer_and_the_worker(self):
        """Whatever the page costs per row, the engineer pays the same per row as the
        task-holding engineer: the assignment check is per page, never per row."""
        self._assign(self.site_a, self.qaqc)
        before = {p: self._count(p) for p in (self.qaqc, self.worker)}
        phase = self._se_task(self.site_a).phase
        top = Task.objects.filter(phase=phase).order_by('-task_order').first().task_order
        for n in range(1, 6):
            Task.objects.create(phase=phase, task_name=f'Extra row {n}',
                                assigned_role=Task.SITE_ENGINEER, task_order=top + n)
        after = {p: self._count(p) for p in (self.qaqc, self.worker)}
        self.assertEqual(after[self.qaqc] - before[self.qaqc],
                         after[self.worker] - before[self.worker])


# ---------------------------------------------------------------------------
# user_works_on_site: identical to the old view rule for everyone but the engineer
# ---------------------------------------------------------------------------

class WorksOnSiteEquivalenceTests(QaqcFixture):
    """The Q-A ruling: the new positive rule changes nothing for any existing user.
    Before closeout step 3, user_works_on_site and user_can_view_project were one
    function; with no assignment they must still agree for every person and site."""

    def _people(self):
        return [self.pm_a, self.coord_a, self.pm_b, self.worker, self.qaqc, self.spare,
                self.legacy, self.design, self.scm, self.finance]

    def test_without_an_assignment_works_on_site_equals_view(self):
        for profile in self._people():
            for site in (self.site_a, self.site_b):
                with self.subTest(user=profile.user.username, site=site.project_id):
                    self.assertEqual(user_works_on_site(profile.user, site),
                                     user_can_view_project(profile.user, site))

    def test_with_an_assignment_only_the_engineer_differs(self):
        self._assign(self.site_a, self.qaqc)
        for profile in self._people():
            for site in (self.site_a, self.site_b):
                with self.subTest(user=profile.user.username, site=site.project_id):
                    differs = (user_works_on_site(profile.user, site)
                               != user_can_view_project(profile.user, site))
                    self.assertEqual(differs, profile == self.qaqc and site == self.site_a)

    def test_for_a_site_engineer_it_is_manages_or_holds_a_task(self):
        self._assign(self.site_a, self.qaqc)
        for profile in (self.worker, self.qaqc, self.spare, self.legacy):
            for site in (self.site_a, self.site_b):
                holds = Task.objects.filter(phase__project=site, assigned_to=profile).exists()
                with self.subTest(user=profile.user.username, site=site.project_id):
                    self.assertEqual(user_works_on_site(profile.user, site),
                                     holds or user_can_manage_project(profile.user, site))


# ---------------------------------------------------------------------------
# Dashboard section and notifications
# ---------------------------------------------------------------------------

class DashboardSectionTests(QaqcFixture):

    def test_the_section_lists_the_site_with_its_awaiting_count(self):
        self._assign(self.site_a, self.qaqc)
        self._submitted(self.site_a, 0)
        self._submitted(self.site_a, 1)
        response = _client_for(self.qaqc).get(reverse('dashboard_site_engineer'))
        rows = response.context['qaqc_sites']
        self.assertEqual([r['project'] for r in rows], [self.site_a])
        self.assertEqual(rows[0]['awaiting_count'], 2)
        self.assertContains(response, 'id="qaqc-sites"')
        self.assertContains(response, reverse('project_overview', args=[self.site_a.project_id]))
        self.assertContains(response, '2 awaiting approval')
        self.assertNotContains(response, 'Closeout step 3')   # template comment did not leak

    def test_an_engineer_with_no_assignment_sees_no_section(self):
        response = _client_for(self.worker).get(reverse('dashboard_site_engineer'))
        self.assertEqual(response.context['qaqc_sites'], [])
        self.assertNotContains(response, 'id="qaqc-sites"')

    def test_an_ended_assignment_leaves_the_section(self):
        self._assign(self.site_a, self.qaqc)
        self._end(self.site_a)
        response = _client_for(self.qaqc).get(reverse('dashboard_site_engineer'))
        self.assertEqual(response.context['qaqc_sites'], [])


class NotificationTests(QaqcFixture):

    def _in_app(self, profile):
        return list(NotificationLog.objects.filter(recipient=profile, channel='in_app')
                    .order_by('created_at', 'pk').values_list('message', flat=True))

    def test_the_new_engineer_is_told_in_app_and_by_email(self):
        self._assign(self.site_a, self.qaqc)
        messages = self._in_app(self.qaqc)
        self.assertEqual(len(messages), 1)
        self.assertIn('You are now the QA/QC engineer for', messages[0])
        self.assertTrue(NotificationLog.objects.filter(
            recipient=self.qaqc, channel='email').exists())
        self.assertFalse(NotificationLog.objects.filter(
            recipient=self.qaqc, channel='whatsapp').exists())

    def test_the_outgoing_engineer_is_told_on_replace(self):
        self._assign(self.site_a, self.qaqc)
        self._assign(self.site_a, self.spare)
        self.assertIn(f'You are no longer the QA/QC engineer for {self.site_a.project_id}',
                      self._in_app(self.qaqc)[-1])
        self.assertTrue(NotificationLog.objects.filter(
            recipient=self.qaqc, channel='email',
            message__startswith='You are no longer').exists())

    def test_the_outgoing_engineer_is_told_on_end(self):
        self._assign(self.site_a, self.qaqc)
        self._end(self.site_a)
        self.assertIn('You are no longer the QA/QC engineer for', self._in_app(self.qaqc)[-1])

    def test_a_refused_assignment_tells_nobody(self):
        self._assign(self.site_a, self.worker)
        self.assertEqual(self._in_app(self.worker), [])
