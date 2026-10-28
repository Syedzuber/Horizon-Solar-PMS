"""
A site's coordinator who is also its QA/QC engineer — closeout spec v2.6 (CL-L, 28 Oct
2026; go-ahead rulings F1, F2, F4).

WHAT IS PINNED HERE
-------------------
    name    a coordinator is named QA/QC engineer, and an engineer is
            added as coordinator; the site's PM is still refused both
            ways                                                        NamingTests
    F4      nobody names themselves; another coordinator may; ending
            one's own assignment stays allowed                          SelfAssignmentTests
    hold    the coordinator-engineer takes a PM task and works it, is
            still refused Site Engineer tasks                           HoldingTests
    F1      no status change, checklist answer or submission on a Site
            Engineer task; due date allowed                             SiteEngineerWorkTests
    F1      visits leave out items whose answer they recorded           OwnAnswerVisitTests
    waive   cannot waive or rectify; the PM waives                      PunchPointTests
    approve cannot approve a task they hold; can approve an SE's task;
            out of the pool on the keyword path too (F2)                ApprovalTests
    B3      the double-role line on site_qaqc and the approval panel    DoubleRoleLineTests
    G103    project_overview rows, and the HTMX redraws (due date, phase
            reorder), draw no status select or Submit on a Site
            Engineer task exactly where the predicate refuses           RowControlTests

Built on tests_qaqc_engineer_tasks.EngineerTaskFixture: `site` (pm + coord, `worker`
holding every Site Engineer task), `other_site` (pm_b + coord_b). Here `coord`, the
site's own coordinator, replaces `qaqc` as its QA/QC engineer.

Run with:
    python manage.py test projects.tests_qaqc_coordinator_engineer --settings=solarpms.test_settings
"""
from datetime import date, timedelta

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from .models import ChecklistItemCompletion, PunchPoint, QaqcVisitAnswer, Task
from .permissions import (
    qaqc_engineer_on_site_engineer_task, task_has_independent_approver,
    user_can_waive_punch_point, user_works_on_site,
)
from .qaqc_views import eligible_qaqc_engineers, end_site_qaqc, site_checklist_points
from .site_pm import SitePmChangeRefused, change_site_pm
from .tests_qaqc_assignment import _client_for, _messages, _profile
from .tests_qaqc_engineer_tasks import CEIG, NET_METERING, EngineerTaskFixture

OWN_ANSWER_REFUSAL = 'You recorded the answer to this point'
DOUBLE_ROLE_PAGE = 'Also a coordinator of this site.'
DOUBLE_ROLE_PANEL = 'is also one of its coordinators.'


class CoordinatorEngineerFixture(EngineerTaskFixture):
    """`coord` is both a coordinator and the QA/QC engineer of `site`."""

    def setUp(self):
        super().setUp()
        self._assign_screen(self.coord)
        self.assertEqual(self._engineer_id(), self.coord.pk, 'fixture: not named')

    def _raise_as_coord(self, item_index=0):
        """One visit by the coordinator-engineer: "No" on a Civil point, submitted."""
        self._start(by=self.coord)
        visit = self._open_visit()
        self._answer(visit, self.civil, self.civil_items[item_index], answer='no',
                     remark='Clamp loose.', by=self.coord)
        self._submit(visit, by=self.coord)
        return PunchPoint.objects.get(qaqc_answer__visit=visit)


# ---------------------------------------------------------------------------
# Naming, in either order; the PM half stays
# ---------------------------------------------------------------------------

class NamingTests(EngineerTaskFixture):

    def test_a_coordinator_is_named_and_keeps_the_seat(self):
        self._assign_screen(self.coord)
        self.assertEqual(self._engineer_id(), self.coord.pk)
        self.assertTrue(self.site.coordinators.filter(pk=self.coord.pk).exists())
        self.assertTrue(user_works_on_site(self.coord.user, self.site))

    def test_the_engineer_is_added_as_coordinator(self):
        self._assign_screen(self.coord_b)
        response = _client_for(self.pm).post(
            reverse('assign_coordinators', args=[self.site.project_id]),
            {'coordinator_ids': [self.coord.pk, self.coord_b.pk]})
        self.assertNotIn('End the QA/QC assignment first', ' '.join(_messages(response)))
        self.assertEqual(set(self.site.coordinators.all()), {self.coord, self.coord_b})
        self.assertEqual(self._engineer_id(), self.coord_b.pk)

    def test_the_sites_pm_is_still_refused_as_engineer(self):
        response = self._assign_screen(self.pm, by=self.coord)
        self.assertIn('manages this site', ' '.join(_messages(response)))
        self.assertEqual(self._engineer_id(), self.qaqc.pk)
        self.assertNotIn(self.pm, set(eligible_qaqc_engineers(self.site)))

    def test_the_engineer_is_still_refused_as_pm(self):
        self._assign_screen(self.pm_b)
        admin = _profile('qce_admin', 'Admin')
        with self.assertRaisesMessage(SitePmChangeRefused, 'End the QA/QC assignment first'):
            change_site_pm(self.site, self.pm_b, admin, expected_outgoing_pm=self.pm,
                           reason='Rebalancing.')
        self.site.refresh_from_db()
        self.assertEqual(self.site.assigned_pm, self.pm)


# ---------------------------------------------------------------------------
# F4 — nobody names themselves
# ---------------------------------------------------------------------------

class SelfAssignmentTests(EngineerTaskFixture):

    def test_a_coordinator_cannot_name_themselves(self):
        screen = _client_for(self.coord).get(reverse('site_qaqc', args=[self.site.project_id]))
        self.assertNotIn(self.coord, set(screen.context['eligible']))
        response = self._assign_screen(self.coord, by=self.coord)
        self.assertIn('You cannot name yourself', ' '.join(_messages(response)))
        self.assertEqual(self._engineer_id(), self.qaqc.pk)

    def test_another_coordinator_names_them(self):
        coord_c = _profile('qce_coord_c', 'Project Coordinator')
        self.site.coordinators.add(coord_c)
        screen = _client_for(coord_c).get(reverse('site_qaqc', args=[self.site.project_id]))
        self.assertIn(self.coord, set(screen.context['eligible']))
        self._assign_screen(self.coord, by=coord_c)
        self.assertEqual(self._engineer_id(), self.coord.pk)

    def test_they_may_end_their_own_assignment(self):
        self._assign_screen(self.coord)
        _client_for(self.coord).post(reverse('site_qaqc', args=[self.site.project_id]),
                                     {'action': 'end'})
        self.assertFalse(self.site.qaqc_assignments.filter(ended_at__isnull=True).exists())


# ---------------------------------------------------------------------------
# Holding: a PM task yes, a Site Engineer task never
# ---------------------------------------------------------------------------

class HoldingTests(CoordinatorEngineerFixture):

    def test_they_take_a_pm_task_and_work_it(self):
        net = self._give(self._task(NET_METERING), self.coord)
        self.assertEqual(self._due(net, self.coord).status_code, 302)
        self._status(net, Task.IN_PROGRESS, self.coord)
        net.refresh_from_db()
        self.assertEqual(net.status, Task.IN_PROGRESS)
        self._submit_task(net, self.coord)
        self.assertTrue(net.is_awaiting_approval)
        self.assertFalse(net.approval_self_certified)

    def test_they_are_still_refused_a_site_engineer_task(self):
        _client_for(self.pm).post(
            reverse('task_assign', args=[self.site.project_id, self.civil.pk]),
            {'assigned_to': self.coord.pk})
        self.civil.refresh_from_db()
        self.assertEqual(self.civil.assigned_to, self.worker)


# ---------------------------------------------------------------------------
# F1 — no Site Engineer work from the coordinator seat
# ---------------------------------------------------------------------------

class SiteEngineerWorkTests(CoordinatorEngineerFixture):

    def test_status_change_is_refused(self):
        self.assertEqual(self._status(self.civil, Task.IN_PROGRESS, self.coord).status_code,
                         403)
        self.civil.refresh_from_db()
        self.assertEqual(self.civil.status, Task.NOT_STARTED)

    def test_checklist_answer_is_refused(self):
        response = _client_for(self.coord).post(
            reverse('checklist_item_complete',
                    args=[self.site.project_id, self.civil.pk, self.civil_items[0].pk]),
            {'answer': 'yes'})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(ChecklistItemCompletion.objects.filter(task=self.civil).exists())
        page = _client_for(self.coord).get(
            reverse('task_detail', args=[self.site.project_id, self.civil.pk]))
        self.assertFalse(page.context['can_complete_items'])

    def test_submission_is_refused(self):
        Task.objects.filter(pk=self.civil.pk).update(status=Task.IN_PROGRESS)
        self.assertEqual(self._submit_task(self.civil, self.coord).status_code, 403)
        self.assertFalse(self.civil.is_awaiting_approval)
        page = _client_for(self.coord).get(
            reverse('task_detail', args=[self.site.project_id, self.civil.pk]))
        self.assertFalse(page.context['can_submit_task'])

    def test_due_date_stays_allowed(self):
        self.assertEqual(self._due(self.civil, self.coord).status_code, 302)
        self.civil.refresh_from_db()
        self.assertEqual(self.civil.due_date, date.today() + timedelta(days=10))

    def test_the_pm_and_a_plain_coordinator_are_unaffected(self):
        # The engineer is not a coordinator here; coord_b is, and moves the SE task.
        self._assign_screen(self.pm_b)
        self.site.coordinators.add(self.coord_b)
        self.assertEqual(self._due(self.civil, self.coord_b).status_code, 302)
        self._status(self.civil, Task.IN_PROGRESS, self.coord_b)
        self.civil.refresh_from_db()
        self.assertEqual(self.civil.status, Task.IN_PROGRESS)


# ---------------------------------------------------------------------------
# F1 — a visit never checks the visiting engineer's own answers
# ---------------------------------------------------------------------------

class OwnAnswerVisitTests(CoordinatorEngineerFixture):

    def setUp(self):
        super().setUp()
        # An execution answer the coordinator recorded before they were named (or before
        # v2.6 refused it): the row as the completion view writes it.
        ChecklistItemCompletion.objects.create(
            task=self.civil, item=self.civil_items[0], is_checked=True, answer='yes',
            checked_by=self.coord.user, checked_at=timezone.now(),
            item_text_snapshot=self.civil_items[0].label)

    def _civil_items(self, points):
        return [i.pk for e in points if e['task'].pk == self.civil.pk for i in e['items']]

    def test_the_points_leave_out_their_answer_only(self):
        mine = self._civil_items(site_checklist_points(self.site, holder_id=self.coord.pk))
        self.assertNotIn(self.civil_items[0].pk, mine)
        self.assertIn(self.civil_items[1].pk, mine)
        everyone = self._civil_items(site_checklist_points(self.site))
        self.assertIn(self.civil_items[0].pk, everyone)

    def test_one_query_more_with_a_holder(self):
        with CaptureQueriesContext(connection) as plain:
            site_checklist_points(self.site)
        with CaptureQueriesContext(connection) as held:
            site_checklist_points(self.site, holder_id=self.coord.pk)
        self.assertEqual(len(held), len(plain) + 1)
        self.assertLessEqual(len(held), 6)

    def test_the_answer_endpoint_refuses_it(self):
        self._start(by=self.coord)
        visit = self._open_visit()
        response = self._answer(visit, self.civil, self.civil_items[0], by=self.coord)
        self.assertContains(response, OWN_ANSWER_REFUSAL)
        self.assertFalse(QaqcVisitAnswer.objects.filter(
            visit=visit, item=self.civil_items[0]).exists())
        self._answer(visit, self.civil, self.civil_items[1], by=self.coord)
        self.assertTrue(QaqcVisitAnswer.objects.filter(
            visit=visit, item=self.civil_items[1]).exists())


# ---------------------------------------------------------------------------
# Punch points: no waiver, no rectify; the PM waives
# ---------------------------------------------------------------------------

class PunchPointTests(CoordinatorEngineerFixture):

    def test_cannot_waive_and_the_pm_can(self):
        point = self._raise_as_coord()
        self.assertFalse(user_can_waive_punch_point(self.coord.user, self.site))
        self.assertEqual(self._waive(point, by=self.coord).status_code, 403)
        self.assertEqual(point.status, PunchPoint.OPEN)
        page = self._task_page(by=self.coord)
        self.assertFalse(any(p.offer_waive for p in page.context['task_punch_points']))
        self._waive(point, by=self.pm)
        self.assertEqual(point.status, PunchPoint.WAIVED)

    def test_cannot_rectify(self):
        point = self._raise_as_coord()
        self.assertEqual(self._rectify(point, by=self.coord).status_code, 403)
        self.assertEqual(point.status, PunchPoint.OPEN)

    def test_once_replaced_they_waive_again(self):
        # The refusal follows the assignment, not the person: replaced by pm_b, `coord`
        # is a plain coordinator and waives.
        point = self._raise_as_coord()
        self._assign_screen(self.pm_b)
        self.assertTrue(user_can_waive_punch_point(self.coord.user, self.site))
        self._waive(point, by=self.coord)
        self.assertEqual(point.status, PunchPoint.WAIVED)


# ---------------------------------------------------------------------------
# Approval
# ---------------------------------------------------------------------------

class ApprovalTests(CoordinatorEngineerFixture):

    def test_cannot_approve_a_task_they_hold(self):
        net = self._give(self._task(NET_METERING), self.coord)
        Task.objects.filter(pk=net.pk).update(status=Task.IN_PROGRESS)
        self._submit_task(net, self.coord)
        self.assertTrue(net.is_awaiting_approval, 'the engineer self-certified')
        for view in ('task_approve', 'task_reject'):
            with self.subTest(view=view):
                self._verdict(net, view, self.coord)
                self.assertTrue(net.is_awaiting_approval, f'{view} acted')
        self._verdict(net, 'task_approve', self.pm)
        self.assertEqual(net.status, Task.DONE)
        self.assertEqual(net.approved_by, self.pm)

    def test_can_approve_a_site_engineers_task(self):
        task = self._submitted(self.module)
        self.assertTrue(task.is_awaiting_approval)
        self._approve(task, by=self.coord)
        self.assertEqual(task.status, Task.DONE)
        self.assertEqual(task.approved_by, self.coord)

    def test_out_of_the_pool_on_the_keyword_path(self):
        """F2: the coordinators read must not put them back for a task they hold."""
        self.assertTrue(task_has_independent_approver(self.site, self.pm))
        self.assertFalse(task_has_independent_approver(self.site, self.pm,
                                                       engineer_holds_task=True))


# ---------------------------------------------------------------------------
# B3 — the double-role line
# ---------------------------------------------------------------------------

class DoubleRoleLineTests(CoordinatorEngineerFixture):

    def _panel(self, by):
        return _client_for(by).get(
            reverse('task_detail', args=[self.site.project_id, self.civil.pk]))

    def test_site_qaqc_page_says_so(self):
        page = _client_for(self.pm).get(reverse('site_qaqc', args=[self.site.project_id]))
        self.assertContains(page, DOUBLE_ROLE_PAGE)
        self.assertContains(page, 'cannot work or hold its')

    def test_site_qaqc_page_is_silent_for_a_plain_engineer(self):
        end_site_qaqc(self.site, self.pm)
        self._assign_screen(self.pm_b)
        page = _client_for(self.pm).get(reverse('site_qaqc', args=[self.site.project_id]))
        self.assertNotContains(page, DOUBLE_ROLE_PAGE)

    def test_the_approval_panel_says_so_to_managers_and_the_engineer(self):
        for who in (self.pm, self.coord):
            with self.subTest(who=who.user.username):
                self.assertContains(self._panel(who), DOUBLE_ROLE_PANEL)

    def test_the_approval_panel_is_silent_for_others(self):
        self.assertNotContains(self._panel(self.worker), DOUBLE_ROLE_PANEL)
        end_site_qaqc(self.site, self.pm)
        self._assign_screen(self.pm_b)
        self.assertNotContains(self._panel(self.pm), DOUBLE_ROLE_PANEL)


# ---------------------------------------------------------------------------
# G103 — the overview rows follow qaqc_engineer_on_site_engineer_task
# ---------------------------------------------------------------------------

STATUS_CONTROL = 'name="status"'
SUBMIT_BUTTON = 'openSubmitApprovalModal(this)'


class RowControlTests(CoordinatorEngineerFixture):
    """The page against the predicate, viewer by viewer: the row's status select and
    Submit button on a Site Engineer task are absent exactly where
    qaqc_engineer_on_site_engineer_task() is True. Every viewer here who is not refused
    is offered both (a manager, or the task's assignee of its role), so absence can only
    come from the rule."""

    def setUp(self):
        super().setUp()
        # In Progress, not submitted: the state the row draws Submit for.
        Task.objects.filter(pk=self.civil.pk).update(status=Task.IN_PROGRESS)
        self.civil.refresh_from_db()

    def _check(self, viewer, label):
        refused = qaqc_engineer_on_site_engineer_task(viewer.user, self.civil, self.site)
        page = _client_for(viewer).get(reverse('project_overview',
                                               args=[self.site.project_id]))
        row = self._row(page.content.decode(), self.civil)
        with self.subTest(viewer=label):
            self.assertEqual(page.context['qaqc_refused_task_role'] == self.civil.assigned_role,
                             refused)
            self.assertEqual(STATUS_CONTROL in row, not refused, 'status select')
            self.assertEqual(SUBMIT_BUTTON in row, not refused, 'Submit button')
        return refused

    def test_page_matches_the_predicate_for_every_viewer(self):
        self.site.coordinators.add(self.coord_b)
        cases = [(self.coord, 'engineer with a coordinator seat', True),
                 (self.coord_b, 'ordinary coordinator', False),
                 (self.pm, 'PM', False),
                 (self.worker, 'Site Engineer holding the task', False)]
        # The engineer without a seat: pm_b replaces coord, who becomes an ordinary
        # coordinator again. pm_b manages nothing here and holds nothing, so they are
        # offered neither control anyway; the rule and the page still agree.
        for viewer, label, expected in cases:
            self.assertEqual(self._check(viewer, label), expected, label)
        self._assign_screen(self.pm_b)
        for viewer, label, expected in [(self.pm_b, 'engineer without a seat', True),
                                        (self.coord, 'former engineer, coordinator', False)]:
            self.assertEqual(self._check(viewer, label), expected, label)

    def test_other_roles_rows_keep_their_controls(self):
        # CEIG Approval is a PM task the site's PM holds: the coordinator-engineer still
        # manages it, so its row keeps the select.
        ceig = self._task(CEIG)
        self.assertFalse(qaqc_engineer_on_site_engineer_task(self.coord.user, ceig, self.site))
        page = _client_for(self.coord).get(reverse('project_overview',
                                                   args=[self.site.project_id]))
        self.assertIn(STATUS_CONTROL, self._row(page.content.decode(), ceig))

    # -- the HTMX redraws (_task_row_context) ----------------------------------------

    def _due_redraw(self, by):
        """Set a due date on the Site Engineer task through the HTMX endpoint; the
        redrawn row's markup."""
        response = _client_for(by).post(
            reverse('task_set_due_date', args=[self.site.project_id, self.civil.pk]),
            {'due_date': (date.today() + timedelta(days=12)).isoformat()},
            HTTP_HX_REQUEST='true')
        self.assertEqual(response.status_code, 200)
        self.civil.refresh_from_db()
        self.assertEqual(self.civil.due_date, date.today() + timedelta(days=12),
                         'fixture: the due date was not set')
        return self._row(response.content.decode(), self.civil)

    def _reorder_redraw(self, by):
        """Reverse the Site Engineer task's phase through the HTMX endpoint; the civil
        row's markup in the redrawn phase."""
        phase = self.civil.phase
        ids = list(Task.objects.filter(phase=phase).order_by('task_order', 'pk')
                   .values_list('pk', flat=True))
        response = _client_for(by).post(
            reverse('phase_tasks_reorder', args=[self.site.project_id, phase.pk]),
            {'order': ','.join(str(pk) for pk in reversed(ids))}, HTTP_HX_REQUEST='true')
        self.assertEqual(response.status_code, 200)
        return self._row(response.content.decode(), self.civil)

    def test_a_due_date_redraw_follows_the_rule(self):
        self.assertTrue(qaqc_engineer_on_site_engineer_task(self.coord.user, self.civil,
                                                            self.site))
        row = self._due_redraw(self.coord)
        self.assertNotIn(STATUS_CONTROL, row)
        self.assertNotIn(SUBMIT_BUTTON, row)
        row = self._due_redraw(self.pm)
        self.assertIn(STATUS_CONTROL, row)
        self.assertIn(SUBMIT_BUTTON, row)

    def test_a_phase_reorder_redraw_follows_the_rule(self):
        row = self._reorder_redraw(self.coord)
        self.assertNotIn(STATUS_CONTROL, row)
        self.assertNotIn(SUBMIT_BUTTON, row)
        row = self._reorder_redraw(self.pm)
        self.assertIn(STATUS_CONTROL, row)
        self.assertIn(SUBMIT_BUTTON, row)
