"""
Punch points close when their task is approved; one site-level open-points query.
Closeout step 2 (docs/CLOSEOUT_SPEC.md: CL-1, CL-2, CL-B).

WHAT IS PINNED
--------------
- CL-1: an approval, ordinary or self-certified, closes every Open point on THAT
  task, crediting the approver at the approval instant, with the method recorded.
  Waived points, and points on other tasks, are untouched.
- CL-B readiness: the closure does not care what raised the point. A row created
  directly, as a step-7 spot check will create it, closes the same way.
- A refused completion closes nothing (the closure is inside the approval's
  transaction).
- CL-2: `open_punch_points_for_project()` counts Open only, across every task on
  the site, Not Applicable tasks included (ruling Q3), never another site's.
- Display: Closed reads as Closed, with method, closer and time, on the full page
  and in the out-of-band card after an HTMX approve or reject (ruling Q4).
- Migration 0111 closes only points that CL-1 would have closed.

Run with:
    python manage.py test projects.tests_punch_point_closure --settings=solarpms.test_settings
"""
import re
from datetime import date, timedelta
from decimal import Decimal
from importlib import import_module
from unittest import mock

from django.apps import apps as django_apps
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import ActivityLog, Project, PunchPoint, Task
from .punch_points import open_punch_points_for_project
from .tests_punch_point import PunchPointFixture
from .tests_two_step_completion import _client_for, _profile
from .utils import RESIDENTIAL_FINANCE_ASSIGNEE_EMAIL, assign_task_to


class ClosureFixture(PunchPointFixture):
    """PunchPointFixture's activated OPEX site. The PM holds and submits the task;
    the QA/QC holder rejects and approves it, so no approval here is a
    self-approval."""

    def _approve(self, task=None, profile=None, **headers):
        task = task or self.task
        return _client_for(profile or self.qaqc).post(
            reverse('task_approve', args=[self.site.project_id, task.pk]),
            {'approval_remarks': 'Re-checked on site, fixed.'}, **headers)

    def _reject_then_approve(self, reason='Earthing not tested.'):
        """Reject (one Open point), resubmit, approve. Returns the task."""
        task = self._reject(reason, profile=self.qaqc)
        task = self._submitted(task=task)
        with self.captureOnCommitCallbacks(execute=True):
            self._approve(task)
        task.refresh_from_db()
        return task

    def _other_task(self):
        return Task.objects.filter(
            phase__project=self.site, is_mirror=False,
        ).exclude(pk=self.task.pk).first()


# ---------------------------------------------------------------------------
# 1. CL-1: the approval closes the task's Open points
# ---------------------------------------------------------------------------

class ApprovalClosesPunchPointsTests(ClosureFixture):

    def test_reject_resubmit_approve_closes_the_point_crediting_the_approver(self):
        task = self._reject_then_approve()
        self.assertEqual(task.status, Task.DONE)
        point = self._points(task)[0]
        self.assertEqual(point.status, PunchPoint.CLOSED)
        self.assertEqual(point.closed_by, self.qaqc)
        self.assertEqual(point.closed_at, task.approved_at,
                         'the closure time is the approval instant')
        self.assertEqual(point.closure_method, PunchPoint.CLOSED_ON_APPROVAL)
        # Waiver columns stay empty: a Closed point was never waived.
        self.assertIsNone(point.waived_by)
        self.assertIsNone(point.waived_at)

    def test_the_closure_is_logged_once_after_commit(self):
        task = self._reject_then_approve()
        logs = ActivityLog.objects.filter(
            entity_type='Task', entity_id=task.pk, action_code='punch_points_closed')
        self.assertEqual(logs.count(), 1)
        self.assertIn('Closed on approval', logs.get().action)
        self.assertEqual(logs.get().actor, self.qaqc)

    def test_an_approval_with_no_open_points_logs_no_closure(self):
        task = self._submitted()
        with self.captureOnCommitCallbacks(execute=True):
            self._approve(task)
        task.refresh_from_db()
        self.assertEqual(task.status, Task.DONE)
        self.assertFalse(ActivityLog.objects.filter(
            action_code='punch_points_closed').exists())

    def test_every_open_point_on_the_task_closes(self):
        task = self._reject('First finding.', profile=self.qaqc)
        task = self._reject('Second finding.', profile=self.qaqc, task=task)
        task = self._submitted(task=task)
        self._approve(task)
        self.assertEqual({p.status for p in self._points(task)}, {PunchPoint.CLOSED})

    def test_a_waived_point_is_untouched_by_approval(self):
        task = self._reject('Accepted defect.', profile=self.qaqc)
        waived = self._points(task)[0]
        self._waive(waived, 'Client accepted it.')
        waived.refresh_from_db()
        waived_at = waived.waived_at

        task = self._submitted(task=task)
        self._approve(task)
        waived.refresh_from_db()
        self.assertEqual(waived.status, PunchPoint.WAIVED)
        self.assertEqual(waived.waived_by, self.pm)
        self.assertEqual(waived.waived_at, waived_at)
        self.assertIsNone(waived.closed_by)
        self.assertIsNone(waived.closed_at)
        self.assertEqual(waived.closure_method, '')

    def test_points_on_other_tasks_of_the_site_are_untouched(self):
        other = self._other_task()
        elsewhere = PunchPoint.objects.create(
            task=other, reason='Different task.', raised_by=self.qaqc)
        self._reject_then_approve()
        elsewhere.refresh_from_db()
        self.assertEqual(elsewhere.status, PunchPoint.OPEN)
        self.assertIsNone(elsewhere.closed_at)

    def test_a_point_not_raised_by_a_rejection_also_closes(self):
        """CL-B readiness: a step-7 spot check will create the row directly. The
        closure must not assume task_reject made it."""
        task = self._in_progress()
        direct = PunchPoint.objects.create(
            task=task, reason='Spot check: cable glands loose.', raised_by=self.qaqc)
        task = self._submitted(task=task)
        self._approve(task)
        direct.refresh_from_db()
        self.assertEqual(direct.status, PunchPoint.CLOSED)
        self.assertEqual(direct.closed_by, self.qaqc)
        self.assertEqual(direct.closure_method, PunchPoint.CLOSED_ON_APPROVAL)

    def test_a_refused_completion_closes_nothing(self):
        """The closure runs inside the approval's transaction, after Done is
        accepted. A Blocked task cannot go to Done, so the approval unwinds, and
        the point must stay Open with it."""
        task = self._reject('Earthing not tested.', profile=self.qaqc)
        task = self._submitted(task=task)
        _client_for(self.pm).post(
            reverse('task_status_update', args=[self.site.project_id, task.pk]),
            {'status': Task.BLOCKED, 'block_issue_title': 'Rain stopped work'})
        task.refresh_from_db()
        self.assertEqual(task.status, Task.BLOCKED)

        self._approve(task)
        task.refresh_from_db()
        self.assertIsNone(task.approved_at, 'the approval should have unwound')
        point = self._points(task)[0]
        self.assertEqual(point.status, PunchPoint.OPEN)
        self.assertIsNone(point.closed_at)

    def test_reject_again_after_approval_raises_a_new_open_point(self):
        """Nothing in the app clears an approval, so the second round is reached
        the way an admin edit would reach it: the approval columns are cleared
        directly. The earlier Closed point keeps its closer and time."""
        task = self._reject_then_approve('Round one.')
        first = self._points(task)[0]
        closed_at, closed_by = first.closed_at, first.closed_by

        Task.objects.filter(pk=task.pk).update(
            status=Task.IN_PROGRESS, completed_at=None,
            approved_by=None, approved_at=None, approval_remarks='',
            submitted_by=None, submitted_at=None, submission_remarks='',
        )
        task = self._reject('Round two.', profile=self.qaqc, task=task)

        first.refresh_from_db()
        self.assertEqual(first.status, PunchPoint.CLOSED)
        self.assertEqual(first.closed_at, closed_at)
        self.assertEqual(first.closed_by, closed_by)
        second = self._points(task)[1]
        self.assertEqual(second.reason, 'Round two.')
        self.assertEqual(second.status, PunchPoint.OPEN)


# ---------------------------------------------------------------------------
# 2. A self-certified approval closes them too, and says so
# ---------------------------------------------------------------------------

class SelfCertifiedClosureTests(ClosureFixture):

    def test_self_certified_approval_closes_open_points_recorded_as_such(self):
        task = self._reject('Earthing not tested.', profile=self.qaqc)
        # Take away the only other approval signature on the site, so the PM's
        # resubmission self-certifies (permissions.user_may_self_certify).
        self.qaqc.is_qaqc = False
        self.qaqc.save(update_fields=['is_qaqc'])

        with self.captureOnCommitCallbacks(execute=True):
            self._post('task_submit_for_approval',
                       {'submission_remarks': 'Earth pits re-done and tested.'},
                       task=task)
        task.refresh_from_db()
        self.assertTrue(task.approval_self_certified,
                        'fixture did not reach the self-certified branch')
        self.assertEqual(task.status, Task.DONE)

        point = self._points(task)[0]
        self.assertEqual(point.status, PunchPoint.CLOSED)
        self.assertEqual(point.closed_by, self.pm)
        self.assertEqual(point.closed_at, task.approved_at)
        self.assertEqual(point.closure_method, PunchPoint.CLOSED_ON_SELF_CERTIFIED)
        log = ActivityLog.objects.get(
            entity_type='Task', entity_id=task.pk, action_code='punch_points_closed')
        self.assertIn('Closed on self-certified approval', log.action)

    def test_recompleting_an_approved_task_does_not_close_points(self):
        """Done -> Blocked -> In Progress -> Done passes rung 1 on an approved task,
        but it is not an approval, so a point raised in between stays Open."""
        task = self._reject_then_approve()
        client = _client_for(self.pm)
        url = reverse('task_status_update', args=[self.site.project_id, task.pk])
        client.post(url, {'status': Task.BLOCKED, 'block_issue_title': 'Theft'})
        later = PunchPoint.objects.create(
            task=task, reason='Raised after approval.', raised_by=self.qaqc)
        client.post(url, {'status': Task.IN_PROGRESS})
        client.post(url, {'status': Task.DONE})
        task.refresh_from_db()
        self.assertEqual(task.status, Task.DONE)
        later.refresh_from_db()
        self.assertEqual(later.status, PunchPoint.OPEN)


# ---------------------------------------------------------------------------
# 3. CL-2: the one site-level query
# ---------------------------------------------------------------------------

class OpenPunchPointsForProjectTests(ClosureFixture):

    def _second_site(self):
        site = Project.objects.create(
            customer_name='Closeout Other Site', customer_phone='9876543219',
            site_address='9 Elsewhere Road', city='Lucknow', project_type='OPEX',
            dc_capacity_kw=Decimal('50.00'), status='Draft', assigned_pm=self.pm,
        )
        _client_for(self.pm).post(reverse('opex_site_activate', args=[site.project_id]))
        return site

    def test_counts_open_only_across_every_task_never_another_site(self):
        closed_task = self._reject_then_approve()            # one Closed point
        other = self._other_task()
        open_a = PunchPoint.objects.create(task=other, reason='A', raised_by=self.qaqc)
        waived = PunchPoint.objects.create(task=other, reason='W', raised_by=self.qaqc)
        self._waive(waived)

        third = Task.objects.filter(phase__project=self.site, is_mirror=False).exclude(
            pk__in=[closed_task.pk, other.pk]).first()
        open_b = PunchPoint.objects.create(task=third, reason='B', raised_by=self.qaqc)

        far = self._second_site()
        far_task = Task.objects.filter(phase__project=far, is_mirror=False).first()
        PunchPoint.objects.create(task=far_task, reason='Other site', raised_by=self.qaqc)

        found = list(open_punch_points_for_project(self.site))
        self.assertEqual([p.pk for p in found], [open_a.pk, open_b.pk])
        self.assertEqual(
            [p.reason for p in open_punch_points_for_project(far)], ['Other site'])

    def test_a_point_on_a_not_applicable_task_still_counts(self):
        """Ruling Q3: N/A does not close a defect. The PM waives it."""
        other = self._other_task()
        point = PunchPoint.objects.create(task=other, reason='N/A task', raised_by=self.qaqc)
        Task.objects.filter(pk=other.pk).update(is_not_applicable=True)
        self.assertEqual(
            [p.pk for p in open_punch_points_for_project(self.site)], [point.pk])

    def test_empty_when_the_site_has_none(self):
        self.assertFalse(open_punch_points_for_project(self.site).exists())


# ---------------------------------------------------------------------------
# 4. Display: Closed reads as Closed, on the page and in the HTMX card
# ---------------------------------------------------------------------------

class ClosedPointDisplayTests(ClosureFixture):

    HX = {'HTTP_HX_REQUEST': 'true', 'HTTP_HX_TARGET': 'taskApprovalBlock'}

    def _detail(self, task):
        return _client_for(self.pm).get(
            reverse('task_detail', args=[self.site.project_id, task.pk]))

    def test_task_detail_shows_closed_with_method_closer_and_time(self):
        task = self._reject_then_approve()
        point = self._points(task)[0]
        response = self._detail(task)
        self.assertContains(response, '<span class="badge bg-success">Closed</span>',
                            html=True)
        self.assertContains(response, 'Closed on approval')
        # The closer and the time, on the closure line itself (the raiser's name
        # also appears in Raised By, so the name alone would prove nothing).
        when = timezone.localtime(point.closed_at).strftime('%d %b %Y %H:%M')
        self.assertRegex(
            response.content.decode(),
            rf'by\s+{re.escape(self.qaqc.user.get_full_name())}\s+on {when}')
        self.assertNotContains(response, 'Waived by')
        self.assertNotContains(response, '<span class="badge bg-danger">Open</span>',
                               html=True)

    def test_a_closed_point_with_no_closer_shows_a_dash(self):
        task = self._reject_then_approve()
        PunchPoint.objects.filter(task=task).update(closed_by=None)
        response = self._detail(task)
        self.assertContains(response, 'Closed on approval')
        self.assertRegex(response.content.decode(), r'by\s+—\s+on ')
        self.assertNotContains(response, 'Waived by')

    def test_hx_approve_swaps_a_fresh_punch_point_card(self):
        task = self._reject('Earthing not tested.', profile=self.qaqc)
        task = self._submitted(task=task)
        response = self._approve(task, **self.HX)
        content = response.content.decode()
        self.assertIn('id="punchPointsBlock" hx-swap-oob="true"', content)
        self.assertIn('Closed on approval', content)
        self.assertNotIn('<span class="badge bg-danger">Open</span>', content)

    def test_hx_reject_swaps_in_the_new_punch_point(self):
        task = self._submitted()
        response = _client_for(self.qaqc).post(
            reverse('task_reject', args=[self.site.project_id, task.pk]),
            {'approval_remarks': 'Conduit saddles missing.'}, **self.HX)
        content = response.content.decode()
        self.assertIn('id="punchPointsBlock" hx-swap-oob="true"', content)
        self.assertIn('Conduit saddles missing.', content)
        self.assertIn('<span class="badge bg-danger">Open</span>', content)

    def test_the_overview_submit_response_carries_no_punch_point_card(self):
        """The overview's submit modal targets the row badge and selects
        `.card-header .badge`. The approval card must stay the only card there."""
        task = self._in_progress()
        response = _client_for(self.pm).post(
            reverse('task_submit_for_approval', args=[self.site.project_id, task.pk]),
            {'submission_remarks': 'done'},
            HTTP_HX_REQUEST='true', HTTP_HX_TARGET=f'task-approval-{task.pk}')
        self.assertNotIn('punchPointsBlock', response.content.decode())

    def test_waiving_a_closed_point_is_refused_and_it_stays_closed(self):
        task = self._reject_then_approve()
        point = self._points(task)[0]
        response = self._waive(point, 'Too late.')
        point.refresh_from_db()
        self.assertEqual(point.status, PunchPoint.CLOSED)
        self.assertIsNone(point.waived_at)
        self.assertEqual(point.waiver_reason, '')
        self.assertIn('This punch point is already closed or waived.',
                      self._messages(response))

    def test_a_waiver_racing_an_approval_does_not_overwrite_closed(self):
        """The view reads the point as Open, then an approval closes it before the
        waiver's UPDATE. The view is handed a stale Open instance to simulate
        that gap; the UPDATE's own `status=Open` guard must match nothing."""
        task = self._reject('Earthing not tested.', profile=self.qaqc)
        stale = self._points(task)[0]                     # in memory: Open
        task = self._submitted(task=task)
        self._approve(task)                               # in the DB: Closed
        from django.shortcuts import get_object_or_404 as real_get

        def stale_point(model, *args, **kwargs):
            # Only the view's PunchPoint lookup is stale; the project lookup is real.
            return stale if model is PunchPoint else real_get(model, *args, **kwargs)

        with mock.patch('projects.views.get_object_or_404', side_effect=stale_point):
            response = self._waive(stale, 'Raced.')
        stale.refresh_from_db()
        self.assertEqual(stale.status, PunchPoint.CLOSED)
        self.assertIsNone(stale.waived_at)
        self.assertIn('This punch point is already closed or waived.',
                      self._messages(response))


# ---------------------------------------------------------------------------
# 5. Residential: no points, no errors
# ---------------------------------------------------------------------------

class ResidentialUnaffectedTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        from .utils import resolve_residential_template
        resolve_residential_template()
        cls.pm       = _profile('cl2r_pm', 'PM')
        cls.designer = _profile('cl2r_des', 'Design')
        cls.finance  = _profile('cl2r_fin', 'Finance',
                                email=RESIDENTIAL_FINANCE_ASSIGNEE_EMAIL)

    def test_a_residential_task_completes_with_no_points_and_no_errors(self):
        project = Project.objects.create(
            customer_name='Closeout House', customer_phone='9876543218',
            site_address='8 Baseline Lane', city='Lucknow', project_type='Residential',
            dc_capacity_kw=Decimal('5.00'), status='Draft', assigned_pm=self.pm,
            target_commissioning_date=date.today() + timedelta(days=90),
        )
        _client_for(self.pm).post(reverse('project_activate', args=[project.project_id]),
                                  {'assigned_design_id': self.designer.pk})
        task = Task.objects.filter(
            phase__project=project, assigned_role=Task.PM, is_mirror=False).first()
        assign_task_to(task, self.pm, notify=False)

        client = _client_for(self.pm)
        url = reverse('task_status_update', args=[project.project_id, task.pk])
        client.post(url, {'status': Task.IN_PROGRESS,
                          'due_date': (date.today() + timedelta(days=3)).isoformat()})
        client.post(url, {'status': Task.DONE})
        task.refresh_from_db()
        self.assertEqual(task.status, Task.DONE)
        self.assertEqual(PunchPoint.objects.count(), 0)
        self.assertFalse(open_punch_points_for_project(project).exists())
        response = client.get(reverse('task_detail', args=[project.project_id, task.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'punchPointsSection')


# ---------------------------------------------------------------------------
# 6. Migration 0111: retroactive closure of stale Open points
# ---------------------------------------------------------------------------

class StalePointMigrationTests(ClosureFixture):
    """Runs 0111's forwards() against the current models (the field set it touches
    is unchanged since 0110). The real migrate forward/back/forward is run
    against Postgres by hand; see the session report."""

    def _forwards(self):
        module = import_module('projects.migrations.0111_close_stale_punch_points')
        module.forwards(django_apps, None)

    def _approved_directly(self, task, at, by):
        Task.objects.filter(pk=task.pk).update(
            status=Task.DONE, approved_at=at, approved_by=by,
            submitted_at=at, submitted_by=self.pm)

    def test_a_noop_with_no_such_rows(self):
        self._forwards()
        self.assertEqual(PunchPoint.objects.count(), 0)

    def test_stale_open_points_close_and_genuine_open_points_stay(self):
        now = timezone.now()
        approved = self.task
        stale = PunchPoint.objects.create(task=approved, reason='Before', raised_by=self.qaqc)
        after = PunchPoint.objects.create(task=approved, reason='After', raised_by=self.qaqc)
        PunchPoint.objects.filter(pk=stale.pk).update(created_at=now - timedelta(days=2))
        PunchPoint.objects.filter(pk=after.pk).update(created_at=now + timedelta(hours=1))
        self._approved_directly(approved, now - timedelta(days=1), self.qaqc)

        unapproved = self._other_task()
        genuine = PunchPoint.objects.create(task=unapproved, reason='Open', raised_by=self.qaqc)

        # Done with no approved_at (ruling Q2: only an approval closes).
        done_only = Task.objects.filter(phase__project=self.site, is_mirror=False).exclude(
            pk__in=[approved.pk, unapproved.pk]).first()
        Task.objects.filter(pk=done_only.pk).update(status=Task.DONE)
        done_point = PunchPoint.objects.create(task=done_only, reason='D', raised_by=self.qaqc)

        self._forwards()

        stale.refresh_from_db()
        self.assertEqual(stale.status, PunchPoint.CLOSED)
        self.assertEqual(stale.closure_method, PunchPoint.CLOSED_RETROACTIVELY)
        self.assertEqual(stale.closed_by, self.qaqc)
        self.assertEqual(stale.closed_at, now - timedelta(days=1))
        for point in (after, genuine, done_point):
            point.refresh_from_db()
            self.assertEqual(point.status, PunchPoint.OPEN, point.reason)
            self.assertIsNone(point.closed_at)

    def test_closer_is_null_when_no_approver_was_recorded(self):
        now = timezone.now()
        stale = PunchPoint.objects.create(task=self.task, reason='X', raised_by=self.qaqc)
        PunchPoint.objects.filter(pk=stale.pk).update(created_at=now - timedelta(days=2))
        self._approved_directly(self.task, now - timedelta(days=1), None)
        self._forwards()
        stale.refresh_from_db()
        self.assertEqual(stale.status, PunchPoint.CLOSED)
        self.assertIsNone(stale.closed_by)
        self.assertEqual(stale.closure_method, PunchPoint.CLOSED_RETROACTIVELY)

    def test_a_waived_point_on_an_approved_task_is_untouched(self):
        now = timezone.now()
        point = PunchPoint.objects.create(task=self.task, reason='W', raised_by=self.qaqc)
        PunchPoint.objects.filter(pk=point.pk).update(
            created_at=now - timedelta(days=2), status=PunchPoint.WAIVED,
            waived_by=self.pm, waived_at=now - timedelta(days=2), waiver_reason='ok')
        self._approved_directly(self.task, now - timedelta(days=1), self.qaqc)
        self._forwards()
        point.refresh_from_db()
        self.assertEqual(point.status, PunchPoint.WAIVED)
        self.assertIsNone(point.closed_at)
