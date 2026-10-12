"""
Punch point rectify / re-verify loop and visibility — closeout step 7b
(docs/CLOSEOUT_SPEC.md v2.3: CL-1 amended, CL-2, PP-1 to PP-6; go-ahead Q1–Q9).

WHAT IS PINNED HERE
-------------------
    PP-1  approval closes rejection-raised points only        ApprovalClosureTests      (1)
    PP-2  approval / self-certify refused while a QA/QC
          point is Open or Rectified                          ApprovalGuardTests        (2)
    PP-3  who may rectify; remark + photo                     RectifyTests              (3)
    PP-4  who may re-verify; accept / reject; Q4, Q5          ReverifyTests             (4)
    PP-4  every round kept, in order, append-only             HistoryTests              (5)
    PP-5  rejection-raised points stay out of the loop        RejectionRaisedTests      (6)
          waiver on Open and Rectified (Q1)                   WaiverTests               (7)
    CL-2  COD refused while a point is Rectified              CodTests                  (8)
    PP-6  SE card, PM count + list, row badge, flat queries   DashboardTests            (9)
    PP-6  notices (Q7, Q8)                                    NoticeTests              (10)
    PP-6  visit page badge links to the point                 VisitPageLinkTests       (11)
          Residential untouched                               ResidentialTests         (12)

Every visit-raised point here is raised through the real visit screens
(tests_qaqc_visit.VisitFixture): start, answer "No", submit. Storage is mocked there.

Run with:
    python manage.py test projects.tests_punch_rectify --settings=solarpms.test_settings
"""
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from . import punch_points
from .cod_views import CodRefused, record_refusal
from .models import (
    AppendOnlyViolation, NotificationLog, PunchPoint, PunchPointEvent, Task,
)
from .permissions import user_may_self_certify
from .punch_points import (
    close_punch_points_on_approval, open_punch_points_for_project, raise_punch_point,
)
from .qaqc_views import end_site_qaqc
from .tests_qaqc_assignment import _client_for, _messages, _profile
from .tests_qaqc_visit import VisitFixture, _photo
from .utils import assign_tasks_to
from .views import _attach_open_punch_points


class RectifyFixture(VisitFixture):
    """VisitFixture's two activated OPEX sites. On `site`: pm + coord, QA/QC engineer
    `qaqc`, `worker` holding every Site Engineer task. Added here: `other_se`, holding
    the Module task on `site` (a viewer who is not Civil's assignee), and `scm`."""

    def setUp(self):
        super().setUp()
        self.other_se = _profile('pr_other_se', 'Site Engineer')
        self.scm = _profile('pr_scm', 'SCM')

    # -- raising points through the real visit ----------------------------------

    def _raise(self, *pairs):
        """One visit on `site`: answer "No" on each (task, item index) and submit.
        Returns the raised points, in answer order."""
        self._start()
        visit = self._open_visit()
        for task, index in pairs:
            items = self.civil_items if task.pk == self.civil.pk else self.module_items
            self._answer(visit, task, items[index], answer='no',
                         remark=f'Defect {task.task_name} #{index}')
        self._submit(visit)
        return list(PunchPoint.objects.filter(qaqc_answer__visit=visit).order_by('pk'))

    def _one_point(self):
        return self._raise((self.civil, 0))[0]

    # -- the task's approval ------------------------------------------------------

    def _submitted(self, task=None, by=None):
        task = task or self.civil
        Task.objects.filter(pk=task.pk).update(status=Task.IN_PROGRESS)
        _client_for(by or self.worker).post(
            reverse('task_submit_for_approval', args=[self.site.project_id, task.pk]),
            {'submission_remarks': 'Work done.'})
        task.refresh_from_db()
        return task

    def _approve(self, task=None, by=None):
        task = task or self.civil
        with self.captureOnCommitCallbacks(execute=True):
            response = _client_for(by or self.pm).post(
                reverse('task_approve', args=[self.site.project_id, task.pk]),
                {'approval_remarks': 'Checked.'})
        task.refresh_from_db()
        return response

    # -- the loop -------------------------------------------------------------------

    def _rectify(self, point, by=None, remark='Clamps re-torqued.', photo=True):
        data = {'remark': remark}
        if photo:
            data['photo'] = _photo('fix.jpg')
        with self.captureOnCommitCallbacks(execute=True):
            response = _client_for(by or self.worker).post(
                reverse('punch_point_rectify', args=[self.site.project_id, point.pk]), data)
        point.refresh_from_db()
        return response

    def _reverify(self, point, decision='accept', by=None, remark='', photo=None):
        if photo is None:
            photo = decision == 'accept'
        data = {'decision': decision, 'remark': remark}
        if photo:
            data['photo'] = _photo('check.jpg')
        with self.captureOnCommitCallbacks(execute=True):
            response = _client_for(by or self.qaqc).post(
                reverse('punch_point_reverify', args=[self.site.project_id, point.pk]), data)
        point.refresh_from_db()
        return response

    def _waive(self, point, by=None, reason='Client accepted it.'):
        response = _client_for(by or self.pm).post(
            reverse('punch_point_waive', args=[self.site.project_id, point.pk]),
            {'waiver_reason': reason})
        point.refresh_from_db()
        return response

    def _reject_task(self, task=None, reason='Earthing not tested.'):
        """A rejection-raised point: worker submits, the PM rejects."""
        task = self._submitted(task)
        _client_for(self.pm).post(
            reverse('task_reject', args=[self.site.project_id, task.pk]),
            {'approval_remarks': reason})
        return PunchPoint.objects.get(task=task, qaqc_answer__isnull=True, reason=reason)

    def _task_page(self, task=None, by=None):
        return _client_for(by or self.pm).get(
            reverse('task_detail', args=[self.site.project_id, (task or self.civil).pk]))


# ---------------------------------------------------------------------------
# 1 — PP-1: approval closes a rejection-raised point, never a visit-raised one
# ---------------------------------------------------------------------------

class ApprovalClosureTests(RectifyFixture):

    def test_approval_closes_the_rejection_point_and_leaves_the_visit_point(self):
        visit_point = self._one_point()
        rejection_point = self._reject_task(reason='Conduit saddles missing.')
        # The visit point is waived so PP-2 lets the approval through; what is pinned
        # is that the approval then touches only the rejection-raised point.
        self._waive(visit_point)
        self._submitted()
        self._approve()
        self.assertIsNotNone(self.civil.approved_at)
        rejection_point.refresh_from_db()
        visit_point.refresh_from_db()
        self.assertEqual(rejection_point.status, PunchPoint.CLOSED)
        self.assertEqual(rejection_point.closure_method, PunchPoint.CLOSED_ON_APPROVAL)
        self.assertEqual(visit_point.status, PunchPoint.WAIVED)
        self.assertIsNone(visit_point.closed_at)

    def test_the_closure_function_skips_an_open_visit_point(self):
        visit_point = self._one_point()
        self.civil.approved_at = timezone.now()
        self.assertEqual(close_punch_points_on_approval(
            self.civil, self.pm, PunchPoint.CLOSED_ON_APPROVAL, self.site), 0)
        visit_point.refresh_from_db()
        self.assertEqual(visit_point.status, PunchPoint.OPEN)


# ---------------------------------------------------------------------------
# 2 — PP-2: approval refused while a QA/QC point is Open or Rectified
# ---------------------------------------------------------------------------

class ApprovalGuardTests(RectifyFixture):

    def test_refused_while_open_naming_the_point(self):
        point = self._one_point()
        self._submitted()
        response = self._approve()
        self.assertIsNone(self.civil.approved_at)
        self.assertNotEqual(self.civil.status, Task.DONE)
        message = ' '.join(_messages(response))
        self.assertIn(point.reason, message)
        self.assertIn('Rectify and re-verify it, or the PM waives it.', message)

    def test_refused_while_rectified(self):
        point = self._one_point()
        self._rectify(point)
        self.assertEqual(point.status, PunchPoint.RECTIFIED)
        self._submitted()
        response = self._approve()
        self.assertIsNone(self.civil.approved_at)
        self.assertIn('awaiting re-verification', ' '.join(_messages(response)))

    def test_allowed_after_reverify_accept(self):
        point = self._one_point()
        self._rectify(point)
        self._reverify(point, 'accept')
        self.assertEqual(point.status, PunchPoint.CLOSED)
        self._submitted()
        self._approve()
        self.assertIsNotNone(self.civil.approved_at)
        self.assertEqual(self.civil.status, Task.DONE)

    def test_allowed_after_waiver(self):
        point = self._one_point()
        self._waive(point)
        self._submitted()
        self._approve()
        self.assertEqual(self.civil.status, Task.DONE)

    def test_self_certification_is_refused_and_writes_nothing(self):
        point = self._one_point()
        # Leave the PM as the project's only approver, so their submission self-certifies.
        end_site_qaqc(self.site, self.pm)
        self.site.coordinators.remove(self.coord)
        self.assertTrue(user_may_self_certify(self.pm.user, self.site))
        Task.objects.filter(pk=self.civil.pk).update(status=Task.IN_PROGRESS)
        response = _client_for(self.pm).post(
            reverse('task_submit_for_approval', args=[self.site.project_id, self.civil.pk]),
            {'submission_remarks': 'Done myself.'})
        self.civil.refresh_from_db()
        self.assertIsNone(self.civil.approved_at)
        self.assertIsNone(self.civil.submitted_at)
        self.assertIn(point.reason, ' '.join(_messages(response)))

    def test_the_panel_lists_the_blockers_and_offers_no_approve(self):
        point = self._one_point()
        self._submitted()
        page = self._task_page()
        self.assertEqual([p.pk for p in page.context['approval_blockers']], [point.pk])
        self.assertContains(page, 'approval-blockers')
        self.assertContains(page, f'#punch-point-{point.pk}')
        self.assertNotContains(page, 'Approve &amp; complete')


# ---------------------------------------------------------------------------
# 3 — PP-3: who may rectify, and what it needs
# ---------------------------------------------------------------------------

class RectifyTests(RectifyFixture):

    def test_assignee_pm_and_coordinator_may_rectify(self):
        points = self._raise((self.civil, 0), (self.civil, 1), (self.civil, 2))
        for point, who in zip(points, (self.worker, self.pm, self.coord)):
            with self.subTest(who=who.user.username):
                self._rectify(point, by=who)
                self.assertEqual(point.status, PunchPoint.RECTIFIED)

    def test_qaqc_engineer_other_se_and_scm_are_refused(self):
        assign_tasks_to(Task.objects.filter(pk=self.module.pk), self.other_se)
        point = self._one_point()
        for who in (self.qaqc, self.other_se, self.scm):
            with self.subTest(who=who.user.username):
                response = self._rectify(point, by=who)
                self.assertEqual(response.status_code, 403)
                self.assertEqual(point.status, PunchPoint.OPEN)

    def test_qaqc_engineer_refused_even_with_a_coordinator_seat(self):
        point = self._one_point()
        self.site.coordinators.add(self.qaqc)
        self.assertEqual(self._rectify(point, by=self.qaqc).status_code, 403)
        self.assertEqual(point.status, PunchPoint.OPEN)

    def test_remark_and_photo_are_required(self):
        point = self._one_point()
        uploads = self._uploads()
        response = self._rectify(point, remark='')
        self.assertIn(punch_points.RECTIFY_REMARK_REQUIRED, _messages(response))
        response = self._rectify(point, photo=False)
        self.assertIn(punch_points.RECTIFY_PHOTO_REQUIRED, _messages(response))
        self.assertEqual(point.status, PunchPoint.OPEN)
        self.assertEqual(self._uploads(), uploads, 'a refused rectify uploaded a photo')
        self.assertFalse(PunchPointEvent.objects.exists())

    def test_task_page_offers_the_form_to_the_assignee_only(self):
        point = self._one_point()
        url = reverse('punch_point_rectify', args=[self.site.project_id, point.pk])
        self.assertContains(self._task_page(by=self.worker), url)
        self.assertNotContains(self._task_page(by=self.qaqc), url)


# ---------------------------------------------------------------------------
# 4 — PP-4: who may re-verify; accept and reject; go-ahead Q4 and Q5
# ---------------------------------------------------------------------------

class ReverifyTests(RectifyFixture):

    def test_engineer_accepts_with_photo_and_the_point_closes_reverified(self):
        point = self._one_point()
        self._rectify(point)
        self._reverify(point, 'accept')
        self.assertEqual(point.status, PunchPoint.CLOSED)
        self.assertEqual(point.closure_method, PunchPoint.CLOSED_ON_REVERIFICATION)
        self.assertEqual(point.closed_by, self.qaqc)
        self.assertIsNotNone(point.closed_at)

    def test_accept_without_photo_is_refused(self):
        point = self._one_point()
        self._rectify(point)
        response = self._reverify(point, 'accept', photo=False)
        self.assertIn(punch_points.ACCEPT_PHOTO_REQUIRED, _messages(response))
        self.assertEqual(point.status, PunchPoint.RECTIFIED)

    def test_reject_needs_a_reason_and_reopens(self):
        point = self._one_point()
        self._rectify(point)
        response = self._reverify(point, 'reject', remark='')
        self.assertIn(punch_points.REJECT_REASON_REQUIRED, _messages(response))
        self.assertEqual(point.status, PunchPoint.RECTIFIED)
        self._reverify(point, 'reject', remark='Still loose at the north end.')
        self.assertEqual(point.status, PunchPoint.OPEN)

    def test_rectify_again_after_a_reject(self):
        point = self._one_point()
        self._rectify(point)
        self._reverify(point, 'reject', remark='Not fixed.')
        self._rectify(point, remark='Fixed properly this time.')
        self.assertEqual(point.status, PunchPoint.RECTIFIED)
        self._reverify(point, 'accept')
        self.assertEqual(point.status, PunchPoint.CLOSED)

    def test_pm_and_coordinator_refused_while_an_engineer_can_act(self):
        point = self._one_point()
        self._rectify(point)
        for who in (self.pm, self.coord):
            with self.subTest(who=who.user.username):
                self.assertEqual(self._reverify(point, by=who).status_code, 403)
                self.assertEqual(point.status, PunchPoint.RECTIFIED)

    def test_pm_may_reverify_when_no_engineer_is_assigned(self):
        point = self._one_point()
        self._rectify(point)
        end_site_qaqc(self.site, self.pm)
        self._reverify(point, by=self.pm)
        self.assertEqual(point.status, PunchPoint.CLOSED)
        self.assertEqual(point.closed_by, self.pm)

    def test_pm_may_reverify_when_the_engineer_is_stale(self):
        """Q4: no EFFECTIVE engineer — the row is active but the engineer is no longer a
        Site Engineer — falls back to the PM, and the stale engineer is refused."""
        point = self._one_point()
        self._rectify(point)
        self.qaqc.role = 'SCM'
        self.qaqc.save()
        self.assertEqual(self._reverify(point, by=self.qaqc).status_code, 403)
        self._reverify(point, by=self.coord)
        self.assertEqual(point.status, PunchPoint.CLOSED)

    def test_whoever_rectified_may_not_reverify_that_round(self):
        """Q5: with no engineer, the PM who rectified is refused; the coordinator may."""
        point = self._one_point()
        end_site_qaqc(self.site, self.pm)
        self._rectify(point, by=self.pm)
        response = self._reverify(point, by=self.pm)
        self.assertIn(punch_points.REVERIFY_OWN_ROUND, _messages(response))
        self.assertEqual(point.status, PunchPoint.RECTIFIED)
        self.assertContains(self._task_page(by=self.pm), 'someone else must re-verify')
        self._reverify(point, by=self.coord)
        self.assertEqual(point.status, PunchPoint.CLOSED)

    def test_legacy_is_qaqc_holder_cannot_reverify(self):
        """Only the site assignment names the re-verifier (CL-D). A Site Engineer with
        the legacy flag who can see the site (they hold its Module task) is refused,
        with the engineer assigned and after it is ended."""
        legacy = _profile('pr_legacy_qaqc', 'Site Engineer', is_qaqc=True)
        assign_tasks_to(Task.objects.filter(pk=self.module.pk), legacy)
        point = self._one_point()
        self._rectify(point)
        self.assertEqual(self._reverify(point, by=legacy).status_code, 403)
        end_site_qaqc(self.site, self.pm)
        self.assertEqual(self._reverify(point, by=legacy).status_code, 403)
        self.assertEqual(point.status, PunchPoint.RECTIFIED)

    def test_reverify_on_an_open_point_is_refused(self):
        point = self._one_point()
        response = self._reverify(point, 'accept')
        self.assertIn(punch_points.REVERIFY_NOT_RECTIFIED, _messages(response))
        self.assertEqual(point.status, PunchPoint.OPEN)


# ---------------------------------------------------------------------------
# 5 — PP-4: every round kept, in order
# ---------------------------------------------------------------------------

class HistoryTests(RectifyFixture):

    def test_every_round_is_recorded_in_order(self):
        point = self._one_point()
        self._rectify(point, remark='First fix.')
        self._reverify(point, 'reject', remark='Not good enough.')
        self._rectify(point, by=self.pm, remark='Second fix.')
        self._reverify(point, 'accept', remark='Good now.')
        rounds = list(point.events.order_by('acted_at', 'pk'))
        self.assertEqual(
            [(r.action, r.actor_id, r.remark) for r in rounds],
            [(PunchPointEvent.RECTIFIED, self.worker.pk, 'First fix.'),
             (PunchPointEvent.REVERIFY_REJECTED, self.qaqc.pk, 'Not good enough.'),
             (PunchPointEvent.RECTIFIED, self.pm.pk, 'Second fix.'),
             (PunchPointEvent.REVERIFY_ACCEPTED, self.qaqc.pk, 'Good now.')])
        for r in (rounds[0], rounds[2], rounds[3]):
            self.assertIn('/punch-point/', r.photo_path)
        self.assertEqual(rounds[1].photo_path, '')
        page = self._task_page()
        for text in ('First fix.', 'Not good enough.', 'Second fix.', 'Good now.',
                     'Closed on re-verification'):
            self.assertContains(page, text)

    def test_rounds_are_append_only(self):
        point = self._one_point()
        self._rectify(point)
        event = point.events.get()
        event.remark = 'edited'
        with self.assertRaises(AppendOnlyViolation):
            event.save()
        with self.assertRaises(AppendOnlyViolation):
            event.delete()


# ---------------------------------------------------------------------------
# 6 — PP-5: rejection-raised points do not use the loop
# ---------------------------------------------------------------------------

class RejectionRaisedTests(RectifyFixture):

    def test_rectify_is_not_offered_and_the_endpoint_refuses(self):
        point = self._reject_task()
        url = reverse('punch_point_rectify', args=[self.site.project_id, point.pk])
        self.assertNotContains(self._task_page(by=self.worker), url)
        response = self._rectify(point)
        self.assertIn(punch_points.NOT_VISIT_RAISED, _messages(response))
        self.assertEqual(point.status, PunchPoint.OPEN)
        self.assertFalse(PunchPointEvent.objects.exists())

    def test_the_card_names_both_sources(self):
        self._one_point()
        self._reject_task()
        page = self._task_page()
        self.assertContains(page, 'Raised by QA/QC visit')
        self.assertContains(page, 'Raised by rejection')


# ---------------------------------------------------------------------------
# 7 — Waiver on Open and Rectified visit-raised points (go-ahead Q1)
# ---------------------------------------------------------------------------

class WaiverTests(RectifyFixture):

    def test_waive_an_open_visit_point(self):
        point = self._one_point()
        self._waive(point)
        self.assertEqual(point.status, PunchPoint.WAIVED)

    def test_waive_a_rectified_visit_point(self):
        point = self._one_point()
        self._rectify(point)
        self._waive(point, reason='Accepted as is.')
        self.assertEqual(point.status, PunchPoint.WAIVED)
        self.assertEqual(point.waived_by, self.pm)
        self.assertEqual(point.waiver_reason, 'Accepted as is.')

    def test_a_reverified_point_cannot_be_waived(self):
        point = self._one_point()
        self._rectify(point)
        self._reverify(point, 'accept')
        self._waive(point)
        self.assertEqual(point.status, PunchPoint.CLOSED)


# ---------------------------------------------------------------------------
# 8 — CL-2: COD refused while a point is Rectified
# ---------------------------------------------------------------------------

class CodTests(RectifyFixture):

    def test_rectified_still_counts_and_blocks_cod(self):
        point = self._one_point()
        self._rectify(point)
        self.assertEqual([p.pk for p in open_punch_points_for_project(self.site)], [point.pk])
        with self.assertRaises(CodRefused) as refused:
            record_refusal(self.site)
        self.assertTrue(refused.exception.open_points)

    def test_reverified_no_longer_counts(self):
        point = self._one_point()
        self._rectify(point)
        self._reverify(point, 'accept')
        self.assertFalse(open_punch_points_for_project(self.site).exists())


# ---------------------------------------------------------------------------
# 9 — PP-6: the dashboards and the row badge, at a fixed number of queries
# ---------------------------------------------------------------------------

class DashboardTests(RectifyFixture):

    def _count(self, url, by):
        client = _client_for(by)
        with CaptureQueriesContext(connection) as ctx:
            response = client.get(url)
        self.assertEqual(response.status_code, 200)
        return len(ctx.captured_queries), response

    def test_se_card_lists_only_their_own_tasks_points(self):
        assign_tasks_to(Task.objects.filter(pk=self.module.pk), self.other_se)
        civil_point, module_point = self._raise((self.civil, 0), (self.module, 0))
        mine = _client_for(self.worker).get(reverse('dashboard_site_engineer'))
        self.assertEqual([p.pk for p in mine.context['my_open_punch_points']],
                         [civil_point.pk])
        self.assertContains(mine, 'Open punch points on my tasks')
        self.assertContains(mine, f'#punch-point-{civil_point.pk}')
        theirs = _client_for(self.other_se).get(reverse('dashboard_site_engineer'))
        self.assertEqual([p.pk for p in theirs.context['my_open_punch_points']],
                         [module_point.pk])

    def test_se_card_omits_rejection_points_and_shows_rectified_without_a_button(self):
        point = self._one_point()
        self._reject_task(task=self.module)
        self._rectify(point)
        page = _client_for(self.worker).get(reverse('dashboard_site_engineer'))
        self.assertEqual([p.pk for p in page.context['my_open_punch_points']], [point.pk])
        self.assertContains(page, 'Rectified — awaiting re-verification')

    def test_pm_card_counts_per_site_and_lists_awaiting_reverification(self):
        points = self._raise((self.civil, 0), (self.civil, 1), (self.module, 0))
        raise_punch_point(self.module, 'A rejection-style point.', self.pm)
        self._rectify(points[0])
        page = _client_for(self.pm).get(reverse('dashboard_pm'))
        row = next(r for r in page.context['projects_with_progress']
                   if r['project'].pk == self.site.pk)
        self.assertEqual(row['open_punch_points'], 4)
        self.assertContains(page, '4 open punch points')
        awaiting = page.context['awaiting_reverification']
        self.assertEqual([p.pk for p in awaiting], [points[0].pk])
        self.assertFalse(awaiting[0].pm_may_reverify)   # the site has an engineer
        self.assertContains(page, 'Awaiting re-verification')

    def test_task_row_badge(self):
        self._raise((self.civil, 0), (self.civil, 1))
        page = _client_for(self.pm).get(reverse('project_overview', args=[self.site.project_id]))
        self.assertContains(page, 'open-punch-badge')
        self.assertContains(page, '2 punch points')

    def test_no_per_row_query_growth(self):
        points = self._raise((self.civil, 0), (self.civil, 1), (self.module, 0))
        for point in points:
            self._rectify(point)
        se_three, _ = self._count(reverse('dashboard_site_engineer'), self.worker)
        pm_three, _ = self._count(reverse('dashboard_pm'), self.pm)
        overview_url = reverse('project_overview', args=[self.site.project_id])
        row_three, _ = self._count(overview_url, self.pm)
        # Down to one outstanding point on one task: every list and badge shrinks.
        self._reverify(points[1], 'accept')
        self._reverify(points[2], 'accept')
        se_one, _ = self._count(reverse('dashboard_site_engineer'), self.worker)
        pm_one, page = self._count(reverse('dashboard_pm'), self.pm)
        row_one, _ = self._count(overview_url, self.pm)
        self.assertEqual(len(page.context['awaiting_reverification']), 1)
        self.assertEqual(se_three, se_one)
        self.assertEqual(pm_three, pm_one)
        self.assertEqual(row_three, row_one)


# ---------------------------------------------------------------------------
# 10 — PP-6 notices (go-ahead Q7, Q8)
# ---------------------------------------------------------------------------

class NoticeTests(RectifyFixture):

    def _in_app(self, template):
        return NotificationLog.objects.filter(template_name=template, channel='in_app')

    def test_assignee_hears_once_per_task_when_a_visit_raises_points(self):
        self._raise((self.civil, 0), (self.civil, 1), (self.module, 0))
        notices = self._in_app(punch_points.T_RAISED_BY_VISIT)
        self.assertEqual(notices.count(), 2)
        self.assertEqual(set(notices.values_list('recipient_id', flat=True)), {self.worker.pk})
        self.assertTrue(notices.filter(message__contains='2 punch points').exists())
        self.assertTrue(NotificationLog.objects.filter(
            template_name=punch_points.T_RAISED_BY_VISIT, channel='email').exists())

    def test_engineer_hears_on_rectify(self):
        self._rectify(self._one_point())
        self.assertEqual(
            list(self._in_app(punch_points.T_RECTIFIED).values_list('recipient_id', flat=True)),
            [self.qaqc.pk])

    def test_pm_and_coordinators_hear_on_rectify_when_no_engineer_minus_the_actor(self):
        point = self._one_point()
        end_site_qaqc(self.site, self.pm)
        self._rectify(point, by=self.coord)
        self.assertEqual(
            set(self._in_app(punch_points.T_RECTIFIED).values_list('recipient_id', flat=True)),
            {self.pm.pk})

    def test_assignee_hears_when_reverification_rejects(self):
        point = self._one_point()
        self._rectify(point)
        self._reverify(point, 'reject', remark='Still loose.')
        notices = self._in_app(punch_points.T_REVERIFY_REJECTED)
        self.assertEqual(list(notices.values_list('recipient_id', flat=True)), [self.worker.pk])
        self.assertIn('Still loose.', notices.get().message)


# ---------------------------------------------------------------------------
# 11 — PP-6: the visit page badge links to the point
# ---------------------------------------------------------------------------

class VisitPageLinkTests(RectifyFixture):

    def test_badge_links_to_the_point_on_its_task_page(self):
        point = self._one_point()
        visit = point.qaqc_answer.visit
        page = _client_for(self.pm).get(
            reverse('qaqc_visit_detail', args=[self.site.project_id, visit.pk]))
        self.assertContains(
            page, reverse('task_detail', args=[self.site.project_id, self.civil.pk])
            + f'#punch-point-{point.pk}')
        self.assertContains(page, 'qaqc-punch-link')


# ---------------------------------------------------------------------------
# 12 — Residential untouched
# ---------------------------------------------------------------------------

class ResidentialTests(RectifyFixture):

    def test_loop_urls_404_on_a_residential_project(self):
        house = self._site('PR House', self.pm, 'Residential')
        client = _client_for(self.pm)
        for name in ('punch_point_rectify', 'punch_point_reverify'):
            with self.subTest(name=name):
                self.assertEqual(
                    client.post(reverse(name, args=[house.project_id, 1])).status_code, 404)
        self.assertEqual(client.get(reverse('punch_point_event_photo',
                                            args=[house.project_id, 1])).status_code, 404)

    def test_the_row_badge_costs_a_residential_page_nothing(self):
        house = self._site('PR House 2', self.pm, 'Residential')
        tasks = [Task(pk=1), Task(pk=2)]
        with self.assertNumQueries(0):
            _attach_open_punch_points(tasks, house)
        self.assertEqual([t.open_punch_points for t in tasks], [0, 0])
