"""
The QA/QC engineer holding the site's tasks — closeout spec v2.5 (docs/CLOSEOUT_SPEC.md,
CL-A revised 26 Oct 2026).

WHAT IS PINNED HERE
-------------------
    hold    a coordinator engineer holds, moves, dates and submits the
            site's Coordinator task; a PM engineer the same with a PM task;
            neither can touch any other task there                    CoordinatorEngineerTests
                                                                      PmEngineerTests
    refuse  a Site Engineer engineer is still refused the site's Site
            Engineer tasks, both directions                           SiteEngineerEngineerTests
    approve the engineer never approves, rejects or self-certifies a
            task they hold; they are out of its approver pool, so a
            PM-submitted one self-certifies on a site with no
            coordinator (ruling Q3); the labels agree                 OwnTaskApprovalTests
    visits  tasks the visiting engineer holds are not their points,
            on the page, the answer endpoint and the coverage figure  OwnTaskVisitTests

The other half — holding a task never opens the site's other tasks, for every engineer
role — is the CL-G tripwire, tests_access_isolation.ViewOnlyGrantNeverOpensWorkTripwireTests.
punch_points.next_action is unchanged (ruling Q8; EXECUTION_MODULE_DEFERRED.md G94).

Built on tests_qaqc_engineer_roles.RolesFixture: two activated OPEX sites, `site` (pm +
coord, QA/QC engineer `qaqc`) and `other_site` (pm_b + coord_b). pm_b and coord_b manage
other_site only, so on `site` each has nothing but the QA/QC grant once named there.

Run with:
    python manage.py test projects.tests_qaqc_engineer_tasks --settings=solarpms.test_settings
"""
import re
from datetime import date, timedelta

from django.urls import reverse

from .models import Checklist, ChecklistItem, ChecklistTaskLink, QaqcVisitAnswer, Task
from .permissions import (
    task_has_independent_approver, user_may_self_certify, user_works_on_site,
    user_works_on_task,
)
from .qaqc_views import qaqc_visit_card_context, site_checklist_points
from .tests_qaqc_assignment import _client_for, _messages, _profile
from .tests_qaqc_engineer_roles import RolesFixture
from .tests_qaqc_visit import _photo

PAPERWORK = 'Completion Certificates (Paperwork)'
NET_METERING = 'Net Metering Approval'
CEIG = 'CEIG Approval'
OWN_TASK_REFUSAL = "so you cannot approve or reject it"


class EngineerTaskFixture(RolesFixture):
    """RolesFixture plus helpers for an engineer holding one of `site`'s tasks."""

    def _task(self, name):
        return Task.objects.get(phase__project=self.site, task_name=name)

    def _give(self, task, profile):
        """Assign through the real task_assign view, as the site's PM."""
        response = _client_for(self.pm).post(
            reverse('task_assign', args=[self.site.project_id, task.pk]),
            {'assigned_to': profile.pk})
        self.assertEqual(response.status_code, 302)
        task.refresh_from_db()
        self.assertEqual(task.assigned_to, profile, 'fixture: the task was not assigned')
        return task

    def _status(self, task, status, by):
        return _client_for(by).post(
            reverse('task_status_update', args=[self.site.project_id, task.pk]),
            {'status': status})

    def _due(self, task, by, days=10):
        return _client_for(by).post(
            reverse('task_set_due_date', args=[self.site.project_id, task.pk]),
            {'due_date': (date.today() + timedelta(days=days)).isoformat()})

    def _submit_task(self, task, by):
        with self.captureOnCommitCallbacks(execute=True):
            response = _client_for(by).post(
                reverse('task_submit_for_approval', args=[self.site.project_id, task.pk]),
                {'submission_remarks': 'Paperwork filed.'})
        task.refresh_from_db()
        return response

    def _verdict(self, task, view, by):
        with self.captureOnCommitCallbacks(execute=True):
            response = _client_for(by).post(
                reverse(view, args=[self.site.project_id, task.pk]),
                {'approval_remarks': 'Looked at it.'})
        task.refresh_from_db()
        return response

    def _works_its_own_task(self, engineer, task):
        """The engineer moves, dates and submits `task`, which they hold."""
        self.assertFalse(user_works_on_site(engineer.user, self.site))
        self.assertTrue(user_works_on_task(engineer.user, task, self.site))

        # A due date first: In Progress refuses a task with none.
        self.assertEqual(self._due(task, engineer).status_code, 302)
        task.refresh_from_db()
        self.assertEqual(task.due_date, date.today() + timedelta(days=10))

        self.assertIn(self._status(task, Task.IN_PROGRESS, engineer).status_code, (200, 302))
        task.refresh_from_db()
        self.assertEqual(task.status, Task.IN_PROGRESS)

        self._submit_task(task, engineer)
        self.assertTrue(task.is_awaiting_approval, 'the submission did not queue')
        self.assertFalse(task.approval_self_certified)

    def _cannot_touch(self, engineer, task):
        """Status (both screens) and due date refused on a task they do not hold."""
        before = (task.status, task.due_date)
        self.assertEqual(self._status(task, Task.IN_PROGRESS, engineer).status_code, 403)
        self.assertEqual(self._due(task, engineer).status_code, 403)
        detail = _client_for(engineer).post(
            reverse('task_detail_status_update', args=[self.site.project_id, task.pk]),
            {'status': Task.IN_PROGRESS})
        self.assertEqual(detail.status_code, 403)
        task.refresh_from_db()
        self.assertEqual((task.status, task.due_date), before)

    def _row(self, body, task):
        """The task's row markup on project_overview."""
        match = re.search(rf'id="task-row-{task.pk}".*?</tr>', body, re.S)
        self.assertIsNotNone(match, f'no row for {task.task_name}')
        return match.group(0)


# ---------------------------------------------------------------------------
# Holding: a coordinator engineer, and a PM engineer
# ---------------------------------------------------------------------------

class CoordinatorEngineerTests(EngineerTaskFixture):

    def setUp(self):
        super().setUp()
        self._assign_screen(self.coord_b)
        self.paper = self._give(self._task(PAPERWORK), self.coord_b)
        # A second Coordinator task, held by the site's own coordinator: same role, not
        # theirs, so only the working relationship can refuse it.
        self.other_paper = Task.objects.create(
            phase=self.paper.phase, task_name='Warranty paperwork', task_order=999,
            assigned_role=Task.PROJECT_COORDINATOR, assigned_to=self.coord)

    def test_holds_updates_and_submits_their_task(self):
        self._works_its_own_task(self.coord_b, self.paper)

    def test_cannot_touch_any_other_task_on_the_site(self):
        for task in (self.other_paper, self._task(NET_METERING), self.civil):
            with self.subTest(task=task.task_name):
                self._cannot_touch(self.coord_b, task)

    def test_the_row_draws_controls_on_their_task_only(self):
        body = _client_for(self.coord_b).get(
            reverse('project_overview', args=[self.site.project_id])).content.decode()
        own = self._row(body, self.paper)
        self.assertIn(reverse('task_status_update', args=[self.site.project_id, self.paper.pk]), own)
        self.assertIn(reverse('task_set_due_date', args=[self.site.project_id, self.paper.pk]), own)
        other = self._row(body, self.other_paper)
        self.assertNotIn(reverse('task_status_update',
                                 args=[self.site.project_id, self.other_paper.pk]), other)
        self.assertNotIn(reverse('task_set_due_date',
                                 args=[self.site.project_id, self.other_paper.pk]), other)

    def test_can_answer_a_checklist_on_their_task_if_one_is_ever_linked(self):
        checklist = Checklist.objects.create(name='Paperwork checks', code='qe-paper',
                                             version_no=1)
        item = ChecklistItem.objects.create(checklist=checklist, label='Signed', order=1)
        checklist.activate()
        for name in (PAPERWORK, 'Warranty paperwork'):
            ChecklistTaskLink.objects.create(checklist=checklist, task_name=name,
                                             project_type='OPEX')
        Task.objects.filter(pk__in=[self.paper.pk, self.other_paper.pk]).update(
            status=Task.IN_PROGRESS)
        own = _client_for(self.coord_b).post(
            reverse('checklist_item_complete', args=[self.site.project_id, self.paper.pk, item.pk]),
            {'answer': 'yes', 'photo': _photo()})
        self.assertNotEqual(own.status_code, 403)
        other = _client_for(self.coord_b).post(
            reverse('checklist_item_complete',
                    args=[self.site.project_id, self.other_paper.pk, item.pk]),
            {'answer': 'yes', 'photo': _photo()})
        self.assertEqual(other.status_code, 403)


class PmEngineerTests(EngineerTaskFixture):

    def setUp(self):
        super().setUp()
        self._assign_screen(self.pm_b)
        self.net = self._give(self._task(NET_METERING), self.pm_b)

    def test_holds_updates_and_submits_their_task(self):
        self._works_its_own_task(self.pm_b, self.net)

    def test_cannot_touch_any_other_task_on_the_site(self):
        # CEIG Approval is a PM task the site's PM holds since activation.
        ceig = self._task(CEIG)
        self.assertEqual(ceig.assigned_to, self.pm)
        # Activation leaves the Coordinator task unassigned; give it to the site's
        # coordinator so the refusal is the working relationship's, not "unassigned".
        paper = self._give(self._task(PAPERWORK), self.coord)
        for task in (ceig, paper, self.civil):
            with self.subTest(task=task.task_name):
                self._cannot_touch(self.pm_b, task)


# ---------------------------------------------------------------------------
# A Site Engineer engineer: still refused, both directions
# ---------------------------------------------------------------------------

class SiteEngineerEngineerTests(EngineerTaskFixture):

    def test_cannot_be_given_a_site_engineer_task(self):
        response = _client_for(self.pm).post(
            reverse('task_assign', args=[self.site.project_id, self.civil.pk]),
            {'assigned_to': self.qaqc.pk})
        self.assertIn('cannot hold its Site Engineer tasks', ' '.join(_messages(response)))
        self.civil.refresh_from_db()
        self.assertEqual(self.civil.assigned_to, self.worker)

    def test_a_site_engineer_task_holder_cannot_be_named(self):
        response = self._assign_screen(self.worker)
        self.assertIn('Site Engineer task', ' '.join(_messages(response)))
        self.assertEqual(self._engineer_id(), self.qaqc.pk)


# ---------------------------------------------------------------------------
# Approval on a task the engineer holds (B5, rulings Q3-Q5)
# ---------------------------------------------------------------------------

class OwnTaskApprovalTests(EngineerTaskFixture):

    def setUp(self):
        super().setUp()
        self._assign_screen(self.coord_b)
        self.paper = self._give(self._task(PAPERWORK), self.coord_b)
        Task.objects.filter(pk=self.paper.pk).update(status=Task.IN_PROGRESS)
        self.paper.refresh_from_db()

    def test_the_engineer_never_self_certifies(self):
        # Even with the site's PM and coordinator gone, their own submission waits.
        self.site.coordinators.clear()
        self.pm.user.is_active = False
        self.pm.user.save(update_fields=['is_active'])
        self.assertFalse(user_may_self_certify(self.coord_b.user, self.site,
                                               engineer_holds_task=True))
        self._submit_task(self.paper, self.coord_b)
        self.assertTrue(self.paper.is_awaiting_approval)
        self.assertIsNone(self.paper.approved_at)
        self.assertFalse(self.paper.approval_self_certified)

    def test_cannot_approve_or_reject_a_task_they_hold(self):
        self._submit_task(self.paper, self.pm)          # the PM submits on their behalf
        self.assertTrue(self.paper.is_awaiting_approval, 'fixture: a coordinator exists')
        for view in ('task_approve', 'task_reject'):
            with self.subTest(view=view):
                response = self._verdict(self.paper, view, self.coord_b)
                self.assertIn(OWN_TASK_REFUSAL, ' '.join(_messages(response)))
                self.assertTrue(self.paper.is_awaiting_approval, f'{view} acted')
        # The rest of the pool may: the site's coordinator signs it off.
        self._verdict(self.paper, 'task_approve', self.coord)
        self.assertEqual(self.paper.status, Task.DONE)
        self.assertEqual(self.paper.approved_by, self.coord)

    def test_the_panel_offers_them_no_verdict_and_says_why(self):
        self._submit_task(self.paper, self.pm)
        page = _client_for(self.coord_b).get(
            reverse('task_detail', args=[self.site.project_id, self.paper.pk]))
        self.assertFalse(page.context['can_approve_task'])
        self.assertTrue(page.context['own_engineer_task'])
        self.assertContains(page, 'so you cannot approve or reject it')
        self.assertNotContains(page, 'Approve &amp; complete')

    def test_they_still_approve_tasks_they_do_not_hold(self):
        task = self._submitted()                        # the worker's Civil task
        self._approve(task, by=self.coord_b)
        self.assertEqual(task.status, Task.DONE)
        self.assertEqual(task.approved_by, self.coord_b)

    def test_they_are_out_of_the_pool_for_a_task_they_hold(self):
        self.site.coordinators.clear()
        self.assertTrue(task_has_independent_approver(self.site, self.pm))
        self.assertFalse(task_has_independent_approver(self.site, self.pm,
                                                       engineer_holds_task=True))

    def test_a_pm_submission_of_their_task_self_certifies_with_no_coordinator(self):
        """Ruling Q3: not left waiting for a signature that can never come."""
        self.site.coordinators.clear()
        self._submit_task(self.paper, self.pm)
        self.assertEqual(self.paper.status, Task.DONE)
        self.assertTrue(self.paper.approval_self_certified)
        self.assertEqual(self.paper.approved_by, self.pm)

    def test_a_pm_submission_of_their_task_waits_when_a_coordinator_exists(self):
        self._submit_task(self.paper, self.pm)
        self.assertTrue(self.paper.is_awaiting_approval)
        self.assertFalse(self.paper.approval_self_certified)

    def test_the_row_labels_agree_with_the_endpoint(self):
        """With no coordinator, the PM's row for the engineer's task offers completion;
        the row for a PM task they could submit (the engineer is in its pool) does not."""
        self.site.coordinators.clear()
        ceig = self._task(CEIG)
        Task.objects.filter(pk=ceig.pk).update(status=Task.IN_PROGRESS)
        body = _client_for(self.pm).get(
            reverse('project_overview', args=[self.site.project_id])).content.decode()
        self.assertIn('data-self-certify="1"', self._row(body, self.paper))
        self.assertIn('data-self-certify="0"', self._row(body, ceig))
        # The modal carries both wordings, so the script can match whichever row opened it.
        self.assertIn('js-self-certify-yes', body)
        self.assertIn('js-self-certify-no', body)


# ---------------------------------------------------------------------------
# Visits leave out tasks the visiting engineer holds (B6)
# ---------------------------------------------------------------------------

class OwnTaskVisitTests(EngineerTaskFixture):

    def setUp(self):
        super().setUp()
        self._assign_screen(self.coord_b)
        self.paper = self._give(self._task(PAPERWORK), self.coord_b)
        # A checklist on the Coordinator task: impossible today (checklists live on Site
        # Engineer tasks only), which is exactly the case the rule guards.
        checklist = Checklist.objects.create(name='Paperwork checks', code='qe-visit-paper',
                                             version_no=1)
        self.paper_item = ChecklistItem.objects.create(checklist=checklist, label='Signed',
                                                       order=1)
        checklist.activate()
        ChecklistTaskLink.objects.create(checklist=checklist, task_name=PAPERWORK,
                                         project_type='OPEX')

    def _point_tasks(self, points):
        return {entry['task'].pk for entry in points}

    def test_own_tasks_are_not_their_points(self):
        self.assertIn(self.paper.pk, self._point_tasks(site_checklist_points(self.site)))
        self.assertNotIn(self.paper.pk, self._point_tasks(
            site_checklist_points(self.site, holder_id=self.coord_b.pk)))

    def test_the_visit_page_and_the_answer_endpoint_leave_them_out(self):
        self._start(by=self.coord_b)
        visit = self._open_visit()
        page = _client_for(self.coord_b).get(
            reverse('qaqc_visit_detail', args=[self.site.project_id, visit.pk]))
        self.assertNotIn(self.paper.pk, {g['task'].pk for g in page.context['groups']})
        self.assertIn(self.civil.pk, {g['task'].pk for g in page.context['groups']})
        response = self._answer(visit, self.paper, self.paper_item, by=self.coord_b)
        self.assertContains(response, 'is assigned to you, so it is not checked')
        self.assertFalse(QaqcVisitAnswer.objects.filter(visit=visit, task=self.paper).exists())

    def test_the_coverage_figure_counts_without_them(self):
        all_points = sum(len(e['items']) for e in site_checklist_points(self.site))
        card = qaqc_visit_card_context(self.site, self.pm.user)
        self.assertEqual(card['qaqc_visit_coverage']['total'], all_points - 1)
        overview = _client_for(self.pm).get(
            reverse('project_overview', args=[self.site.project_id]))
        self.assertEqual(overview.context['qaqc_visit_coverage']['total'], all_points - 1)
