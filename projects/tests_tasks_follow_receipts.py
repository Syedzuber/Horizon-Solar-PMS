"""
FIN-M-B5-5b — confirmation tasks follow the milestone; old money paths and the old alert
off (inert until the switch is on).

Pins, with CLIENT_RECEIPTS_LIVE on unless a test says otherwise:
  switch off  a human Done on a confirmation task still syncs the milestone and sends the
              old "Payment received" (R9); milestone_receive and the card's Finance box
              still mark Received; recompute moves no task; query counts on those paths
              are the ones measured at 0fb3559, before this session (K7)
  follower    K1: confirm -> task Done (one activity line with the suffix, a
              REASON_RECEIPTS ledger row, no R9, only the FR36 alert); undo -> Not Started
              with completed_at cleared (Q3); each of the three amount writers turning a
              milestone Received completes the task; an amount-only change leaves it; K2
              legacy milestones never move it; Q5 Blocked moves anyway, N/A is left with a
              warning; the writer refuses any task that is not the milestone's own; the
              task's ledger row carries the milestone row's remark (report ruling Q2); a
              follower failure undoes the whole confirmation and Finance sees an error
              (report ruling Q1)
  no loop     the follower's Done never re-enters the money sync (one milestone write,
              one transition); R9 never fires with the switch on from any chokepoint
              caller
  K4          every hand path (row, HTMX row, detail page, N/A, the chokepoint itself)
              refuses a confirmation task with the one message, for Finance, PM,
              Coordinator and Admin, before the unassigned check; an ordinary task and an
              old "Finance Confirmation" task (Q6) still change normally
  K5          milestone_receive refused: nothing written, task unchanged
  K6          the Finance box: amount equal to, above or below the stored value changes no
              money, status or task; the description saves; the box and both old pop-ups
              are absent and the page's JS list is empty (Q7)
  lock        the follower inside confirm_receipt sees the milestone it just changed

Run with:
    python manage.py test projects.tests_tasks_follow_receipts --settings=solarpms.test_settings
"""
import datetime
from decimal import Decimal
from unittest import mock

from django.contrib.messages import get_messages
from django.contrib.messages.storage.fallback import FallbackStorage
from django.db import connection
from django.test import Client, RequestFactory, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from . import receipts as receipts_module
from .models import (
    REASON_RECEIPTS, SUBJECT_PAYMENT_MILESTONE, SUBJECT_TASK, ActivityLog, NotificationLog,
    PaymentMilestone, ProjectPhase, StatusTransition, Task,
)
from .receipts import RECOMPUTE_SUFFIX, apply_confirmation_task_status
from .tests_milestone_follows_receipts import FollowBase, day
from .views import (
    FOLLOWS_RECEIPTS_REFUSAL, _TASK_STATUS_APPLIED, _TASK_STATUS_REFUSED,
    _apply_task_status_change, _sync_finance_task_from_milestone,
)

LIVE = override_settings(CLIENT_RECEIPTS_LIVE=True)
OFF = override_settings(CLIENT_RECEIPTS_LIVE=False)
D = Decimal
OLD_ALERT = 'payment_milestone_reached'

# K7: queries on the switch-off paths, measured on 0fb3559 (before FIN-M-B5-5b) with
# these same fixtures and requests. The switch adds settings reads only.
QUERIES_OFF_TASK_DONE = 38
QUERIES_OFF_MILESTONE_RECEIVE = 36
QUERIES_OFF_FINANCE_BOX = 36
QUERIES_OFF_CONFIRM = 26


def _client(profile):
    c = Client(SERVER_NAME='localhost')
    c.force_login(profile.user)
    return c


def _texts(response):
    return [str(m) for m in get_messages(response.wsgi_request)]


class TasksBase(FollowBase):
    """FollowBase (M1-M3, the M1 confirmation task — unassigned, like task 1604 on the
    restore) plus the M2 and M3 confirmation tasks held by Finance A, an ordinary PM task,
    and an old "Finance Confirmation" task (Q6), all on one phase."""

    def setUp(self):
        super().setUp()
        phase = self.task.phase
        self.t1 = self.task
        self.t2 = Task.objects.create(phase=phase, task_name='Pre Dispatch Payment Confirmation',
                                      task_order=2, assigned_role=Task.FINANCE,
                                      assigned_to=self.fin_a, is_payment_milestone=True)
        self.t3 = Task.objects.create(phase=phase, task_name='100% Payment Confirmation',
                                      task_order=3, assigned_role=Task.FINANCE,
                                      assigned_to=self.fin_a, is_payment_milestone=True)
        self.ordinary = Task.objects.create(
            phase=phase, task_name='Site Survey', task_order=4, assigned_role=Task.PM,
            assigned_to=self.pm, due_date=day(-5))
        self.old_name = Task.objects.create(
            phase=phase, task_name='Finance Confirmation', task_order=5,
            assigned_role=Task.FINANCE, assigned_to=self.fin_a, is_payment_milestone=False)

    # -- readers ---------------------------------------------------------------

    def task_row(self, task):
        t = Task.objects.get(pk=task.pk)
        return (t.status, t.completed_at is not None)

    def task_moves(self, task):
        return list(StatusTransition.objects.filter(subject_type=SUBJECT_TASK, subject_id=task.pk)
                    .order_by('pk').values_list('from_status', 'to_status', 'reason_code',
                                                'actor_id'))

    def task_lines(self, task):
        return list(ActivityLog.objects.filter(entity_type='Task', entity_id=task.pk)
                    .order_by('pk').values_list('action_code', 'action', 'actor_id'))

    def milestone_moves(self, milestone):
        return list(StatusTransition.objects.filter(
            subject_type=SUBJECT_PAYMENT_MILESTONE, subject_id=milestone.pk)
            .order_by('pk').values_list('from_status', 'to_status', 'reason_code'))

    def sent(self):
        return list(NotificationLog.objects.order_by('pk').values_list('event_code', flat=True))

    def request_as(self, profile, data=None):
        """A POST request carrying messages, for calling the chokepoint directly."""
        request = RequestFactory().post('/', data or {})
        request.user = profile.user
        request.session = 'session'
        request._messages = FallbackStorage(request)
        return request

    # -- human paths -------------------------------------------------------------

    def row_post(self, who, task, status, hx=False, **extra):
        kw = {'HTTP_HX_REQUEST': 'true'} if hx else {}
        return _client(who).post(
            reverse('task_status_update', args=[self.project.project_id, task.pk]),
            {'status': status, **extra}, **kw)

    def detail_post(self, who, task, status):
        return _client(who).post(
            reverse('task_detail_status_update', args=[self.project.project_id, task.pk]),
            {'status': status})

    def na_post(self, who, task, marking=True):
        return _client(who).post(
            reverse('task_set_not_applicable', args=[self.project.project_id, task.pk]),
            {'not_applicable': '1' if marking else '0', 'not_applicable_reason': 'why'})

    def finance_box(self, milestone, amount, description='new note'):
        return _client(self.fin_a).post(
            reverse('project_overview', args=[self.project.project_id]),
            {'action': 'update_milestone', 'milestone_pk': milestone.pk,
             'milestone_description': description, 'amount_received': amount})

    def receive(self, milestone, amount='50000'):
        return _client(self.fin_a).post(
            reverse('milestone_receive', args=[self.project.project_id, milestone.pk]),
            {'amount_received': amount})


# ===========================================================================
# 1. Switch off: everything as before (K7)
# ===========================================================================

@OFF
class SwitchOffTests(TasksBase):

    def test_a_human_done_still_syncs_the_milestone_and_sends_r9(self):
        self.set_amount(self.m2, '50000')
        with self.captureOnCommitCallbacks(execute=True):
            r = self.row_post(self.fin_a, self.t2, Task.DONE, amount_received='50000')
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.task_row(self.t2), (Task.DONE, True))
        m2 = PaymentMilestone.objects.get(pk=self.m2.pk)
        self.assertEqual((m2.status, m2.amount_received), ('Received', D('50000')))
        self.assertIn(OLD_ALERT, self.sent())

    def test_milestone_receive_still_marks_received(self):
        r = self.receive(self.m2)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.row(self.m2)[0], 'Received')
        self.assertEqual(self.task_row(self.t2), (Task.DONE, True))

    def test_the_finance_box_still_marks_received(self):
        self.finance_box(self.m2, '40000')
        m2 = PaymentMilestone.objects.get(pk=self.m2.pk)
        self.assertEqual((m2.status, m2.amount_received, m2.milestone_description),
                         ('Received', D('40000'), 'new note'))

    def test_recompute_moves_no_task(self):
        self.set_amount(self.m1, '50000')
        before = self.task_row(self.t1)
        r = self.confirmed(self.m1, '50000')
        self.undo(r)
        self.assertEqual(self.task_row(self.t1), before)
        self.assertEqual(self.task_moves(self.t1), [])

    def test_the_page_keeps_the_old_inputs(self):
        page = _client(self.fin_a).get(
            reverse('project_overview', args=[self.project.project_id])).content.decode()
        self.assertIn('placeholder="₹ Amount Received"', page)
        self.assertIn('id="receiveModal"', page)
        self.assertIn('id="paymentCaptureModal"', page)
        self.assertIn("var _FINANCE_CONF_TASKS     = ['Advance Payment Confirmation'", page)

    # -- K7: query counts as at 0fb3559 -----------------------------------------

    def count(self, fn):
        with CaptureQueriesContext(connection) as ctx:
            fn()
        return len(ctx.captured_queries)

    def test_query_counts_are_unchanged(self):
        self.set_amount(self.m1, '50000')
        c = _client(self.fin_a)
        url = reverse('task_status_update', args=[self.project.project_id, self.t2.pk])
        self.assertEqual(self.count(lambda: c.post(url, {'status': Task.DONE,
                                                         'amount_received': '50000'})),
                         QUERIES_OFF_TASK_DONE)
        url = reverse('milestone_receive', args=[self.project.project_id, self.m3.pk])
        self.assertEqual(self.count(lambda: c.post(url, {'amount_received': '5000'})),
                         QUERIES_OFF_MILESTONE_RECEIVE)
        url = reverse('project_overview', args=[self.project.project_id])
        self.assertEqual(self.count(lambda: c.post(url, {
            'action': 'update_milestone', 'milestone_pk': self.m1.pk,
            'milestone_description': 'x', 'amount_received': '100'})),
            QUERIES_OFF_FINANCE_BOX)
        r = self.receipt(self.pm, self.m1, amount='100', date=day(1))
        from .receipts import confirm_receipt
        self.assertEqual(self.count(lambda: confirm_receipt(self.fin_b.user, self.fresh(r))),
                         QUERIES_OFF_CONFIRM)


# ===========================================================================
# 2. The follower (K1, K2, Q3, Q5)
# ===========================================================================

@LIVE
class FollowerTests(TasksBase):

    def test_confirm_completing_a_milestone_completes_its_task(self):
        self.set_amount(self.m1, '50000')
        r = self.receipt(self.pm, self.m1, amount='50000', date=day(1))
        before = len(self.sent())
        with self.captureOnCommitCallbacks(execute=True):
            receipts_module.confirm_receipt(self.fin_b.user, self.fresh(r))
        self.assertEqual(self.row(self.m1)[0], 'Received')
        self.assertEqual(self.task_row(self.t1), (Task.DONE, True))
        self.assertEqual(self.task_lines(self.t1), [
            ('task_status_done',
             f'Changed task status to Done: Advance Payment Confirmation{RECOMPUTE_SUFFIX}',
             self.fin_b.pk)])
        self.assertEqual(self.task_moves(self.t1),
                         [(Task.NOT_STARTED, Task.DONE, REASON_RECEIPTS, self.fin_b.pk)])
        new = self.sent()[before:]
        self.assertNotIn(OLD_ALERT, new)
        # Exactly what FIN-M-B5-4b's confirm already sends: the per-receipt alert.
        self.assertEqual(set(new), {'receipt_payment_received'})

    def test_undo_reopens_the_task_to_not_started(self):
        self.set_amount(self.m1, '50000')
        r = self.confirmed(self.m1, '50000')
        self.undo(r)
        self.assertEqual(self.row(self.m1)[0], 'Pending')
        t = Task.objects.get(pk=self.t1.pk)
        self.assertEqual((t.status, t.completed_at), (Task.NOT_STARTED, None))
        self.assertEqual([m[:3] for m in self.task_moves(self.t1)], [
            (Task.NOT_STARTED, Task.DONE, REASON_RECEIPTS),
            (Task.DONE, Task.NOT_STARTED, REASON_RECEIPTS)])
        self.assertEqual(self.task_lines(self.t1)[-1][:2], (
            'task_status_not_started',
            f'Changed task status to Not Started: Advance Payment Confirmation'
            f'{RECOMPUTE_SUFFIX}'))

    def test_the_task_row_carries_the_milestone_rows_remark(self):
        # Ruling Q2 (B5-5b report): both ledger rows of one event say why.
        self.set_amount(self.m1, '50000')
        r = self.confirmed(self.m1, '50000')
        self.undo(r)
        self.confirmed(self.m2, '50000')                      # no agreed amount: waits
        self.pm_set(self.m2, '50000')
        rows = lambda subject_type, pk: list(StatusTransition.objects.filter(
            subject_type=subject_type, subject_id=pk).order_by('pk')
            .values_list('to_status', 'remark'))
        self.assertEqual(rows(SUBJECT_TASK, self.t1.pk), [
            (Task.DONE, 'receipt confirmed'),
            (Task.NOT_STARTED, 'receipt confirmation undone')])
        self.assertEqual([r[1] for r in rows(SUBJECT_PAYMENT_MILESTONE, self.m1.pk)],
                         ['receipt confirmed', 'receipt confirmation undone'])
        self.assertEqual(rows(SUBJECT_TASK, self.t2.pk), [(Task.DONE, 'agreed amount set')])

    def test_a_follower_failure_undoes_the_whole_confirmation(self):
        # Ruling Q1 (B5-5b report): money and task never disagree. A forced failure while
        # moving the task leaves the receipt recorded, the milestone and the task as they
        # were, writes no line, and Finance sees an error, not a server error page.
        self.set_amount(self.m1, '50000')
        r = self.receipt(self.pm, self.m1, amount='50000', date=day(1))
        c = _client(self.fin_b)                               # login row written first
        before = (self.row(self.m1), self.task_row(self.t1), ActivityLog.objects.count(),
                  StatusTransition.objects.count(), NotificationLog.objects.count())
        with mock.patch.object(receipts_module, 'apply_confirmation_task_status',
                               side_effect=RuntimeError('forced')), \
                self.assertLogs('projects.receipts', 'ERROR'), \
                self.captureOnCommitCallbacks(execute=True):
            resp = c.post(reverse('receipt_confirm', args=[self.project.project_id, r.pk]))
        self.assertEqual(resp.status_code, 302)
        self.assertIn(receipts_module.FOLLOW_FAILED, _texts(resp))
        self.assertEqual(self.fresh(r).status, 'recorded')
        self.assertEqual((self.row(self.m1), self.task_row(self.t1),
                          ActivityLog.objects.count(), StatusTransition.objects.count(),
                          NotificationLog.objects.count()), before)

    def test_each_amount_writer_turning_a_milestone_received_completes_its_task(self):
        tasks = {self.m1.pk: self.t1, self.m2.pk: self.t2, self.m3.pk: self.t3}
        for milestone, writer in zip((self.m1, self.m2, self.m3), self.WRITERS):
            with self.subTest(writer=writer):
                self.confirmed(milestone, '50000')            # no agreed amount yet
                self.assertEqual(self.task_row(tasks[milestone.pk])[0], Task.NOT_STARTED)
                getattr(self, writer)(milestone, '50000')
                self.assertEqual(self.row(milestone)[0], 'Received')
                self.assertEqual(self.task_row(tasks[milestone.pk]), (Task.DONE, True))

    def test_an_amount_only_change_leaves_the_task(self):
        self.set_amount(self.m1, '50000')
        self.confirmed(self.m1, '20000')                      # part received
        self.pm_set(self.m1, '60000')                         # amount change, still Pending
        self.assertEqual(self.row(self.m1)[:2], ('Pending', D('20000')))
        self.assertEqual(self.task_moves(self.t1), [])
        self.assertEqual(self.task_lines(self.t1), [])

    def test_a_legacy_milestone_never_moves_its_task(self):
        self.set_amount(self.m3, '39198')
        self.make_received(self.m3, got='53500')
        Task.objects.filter(pk=self.t3.pk).update(status=Task.DONE)
        self.mark('--apply', '--actor', self.fin_a.user.username)
        self.assertTrue(PaymentMilestone.objects.get(pk=self.m3.pk).received_before_receipts)
        r = self.confirmed(self.m3, '10000')
        self.undo(r)
        self.assertEqual(self.row(self.m3)[0], 'Received')
        self.assertEqual(self.task_row(self.t3)[0], Task.DONE)
        self.assertEqual(self.task_moves(self.t3), [])

    def test_a_blocked_task_follows_anyway(self):
        Task.objects.filter(pk=self.t1.pk).update(
            status=Task.BLOCKED, blocked_since=timezone.now() - datetime.timedelta(days=3))
        self.set_amount(self.m1, '50000')
        self.confirmed(self.m1, '50000')
        t = Task.objects.get(pk=self.t1.pk)
        self.assertEqual((t.status, t.blocked_since), (Task.DONE, None))
        self.assertEqual(self.task_moves(self.t1)[0][:2], (Task.BLOCKED, Task.DONE))

    def test_a_not_applicable_task_is_left_with_a_warning(self):
        Task.objects.filter(pk=self.t1.pk).update(is_not_applicable=True)
        self.set_amount(self.m1, '50000')
        with self.assertLogs('projects.receipts', 'WARNING') as logs:
            self.confirmed(self.m1, '50000')
        self.assertEqual(self.row(self.m1)[0], 'Received')
        self.assertEqual(self.task_row(self.t1)[0], Task.NOT_STARTED)
        self.assertIn('Not Applicable', logs.output[0])

    def test_a_task_already_in_the_target_status_writes_nothing(self):
        Task.objects.filter(pk=self.t1.pk).update(status=Task.DONE)
        self.set_amount(self.m1, '50000')
        self.confirmed(self.m1, '50000')
        self.assertEqual((self.task_moves(self.t1), self.task_lines(self.t1)), ([], []))

    def test_the_writer_refuses_any_other_task(self):
        m1 = PaymentMilestone.objects.get(pk=self.m1.pk)
        for task in (self.ordinary, self.old_name, self.t2):
            with self.subTest(task=task.task_name):
                with self.assertRaises(ValueError):
                    apply_confirmation_task_status(task, m1, Task.DONE, self.fin_a)
        other = self.site(name='Other')
        phase = ProjectPhase.objects.create(project=other, phase_name='F', phase_order=1)
        stranger = Task.objects.create(phase=phase, task_name='Advance Payment Confirmation',
                                       task_order=1, assigned_role=Task.FINANCE)
        with self.assertRaises(ValueError):
            apply_confirmation_task_status(stranger, m1, Task.DONE, self.fin_a)
        self.assertEqual(Task.objects.get(pk=stranger.pk).status, Task.NOT_STARTED)

    def test_the_follower_sees_the_milestone_it_just_changed(self):
        seen = []
        real = receipts_module.apply_confirmation_task_status

        def spy(task, milestone, new_status, actor, remark=''):
            seen.append(PaymentMilestone.objects.get(pk=milestone.pk).status)
            return real(task, milestone, new_status, actor, remark=remark)

        self.set_amount(self.m1, '50000')
        with mock.patch.object(receipts_module, 'apply_confirmation_task_status', spy):
            self.confirmed(self.m1, '50000')
        self.assertEqual(seen, ['Received'])


# ===========================================================================
# 3. No loop, no double, no old alert (K3)
# ===========================================================================

@LIVE
class NoLoopTests(TasksBase):

    def test_the_follower_never_re_enters_the_money_sync(self):
        self.set_amount(self.m1, '50000')
        r = self.receipt(self.pm, self.m1, amount='50000', date=day(1))
        with CaptureQueriesContext(connection) as ctx:
            receipts_module.confirm_receipt(self.fin_b.user, self.fresh(r))
        writes = [q['sql'] for q in ctx.captured_queries
                  if q['sql'].lstrip().upper().startswith('UPDATE')
                  and 'projects_paymentmilestone' in q['sql']]
        self.assertEqual(len(writes), 1, writes)
        self.assertEqual(self.milestone_moves(self.m1),
                         [('Pending', 'Received', REASON_RECEIPTS)])

    def test_r9_never_fires_from_any_chokepoint_caller(self):
        # A flagged task outside the name map reaches the R9 block, which is the only
        # sender; the switch keeps it silent.
        Task.objects.filter(pk=self.old_name.pk).update(is_payment_milestone=True)
        flagged = Task.objects.get(pk=self.old_name.pk)
        with self.captureOnCommitCallbacks(execute=True):
            outcome = _apply_task_status_change(flagged, Task.DONE, self.fin_a,
                                                self.request_as(self.fin_a), self.project)
            self.row_post(self.fin_a, self.t2, Task.DONE)
            _sync_finance_task_from_milestone(self.request_as(self.fin_a), self.project,
                                              self.m2, self.fin_a)
        self.assertEqual(outcome, _TASK_STATUS_APPLIED)
        self.assertNotIn(OLD_ALERT, self.sent())
        # The B9a sync is refused at rung 0a: the confirmation task did not move.
        self.assertEqual(self.task_row(self.t2)[0], Task.NOT_STARTED)
        self.assertEqual(self.row(self.m2)[0], 'Pending')


# ===========================================================================
# 4. No hand edits (K4, Q4, Q6)
# ===========================================================================

@LIVE
class HandEditTests(TasksBase):

    def people(self):
        return (self.fin_a, self.pm, self.coord, self.admin_p)

    def assertUntouched(self, task, status=Task.NOT_STARTED):
        t = Task.objects.get(pk=task.pk)
        self.assertEqual((t.status, t.is_not_applicable), (status, False))
        self.assertEqual(self.task_moves(task), [])
        self.assertEqual(self.task_lines(task), [])

    def test_the_overview_row_refuses_everyone(self):
        for who in self.people():
            for task in (self.t1, self.t2):                  # t1 unassigned, t2 assigned
                for status in (Task.DONE, Task.IN_PROGRESS, Task.BLOCKED):
                    with self.subTest(who=who.role, task=task.pk, status=status):
                        r = self.row_post(who, task, status, block_issue_title='x',
                                          due_date='2026-12-01')
                        self.assertEqual(r.status_code, 302)
                        self.assertIn(FOLLOWS_RECEIPTS_REFUSAL, _texts(r))
                        r = self.row_post(who, task, status, hx=True)
                        self.assertEqual(r.status_code, 200)
                        self.assertIn(FOLLOWS_RECEIPTS_REFUSAL, r.content.decode())
                        self.assertUntouched(task)

    def test_the_detail_page_refuses_everyone(self):
        for who in self.people():
            for task in (self.t1, self.t2):
                with self.subTest(who=who.role, task=task.pk):
                    r = self.detail_post(who, task, Task.DONE)
                    self.assertEqual(r.status_code, 302)
                    self.assertIn(FOLLOWS_RECEIPTS_REFUSAL, _texts(r))
                    self.assertUntouched(task)

    def test_not_applicable_is_refused_both_ways(self):
        for who in self.people():
            with self.subTest(who=who.role):
                r = self.na_post(who, self.t2)
                self.assertIn(FOLLOWS_RECEIPTS_REFUSAL, _texts(r))
                self.assertUntouched(self.t2)
        Task.objects.filter(pk=self.t3.pk).update(is_not_applicable=True)
        r = self.na_post(self.pm, self.t3, marking=False)
        self.assertIn(FOLLOWS_RECEIPTS_REFUSAL, _texts(r))
        self.assertTrue(Task.objects.get(pk=self.t3.pk).is_not_applicable)

    def test_the_chokepoint_refuses_every_move(self):
        for who in self.people():
            for status in (Task.DONE, Task.IN_PROGRESS, Task.BLOCKED):
                with self.subTest(who=who.role, status=status):
                    request = self.request_as(who, {'amount_received': '50000'})
                    outcome = _apply_task_status_change(
                        Task.objects.get(pk=self.t2.pk), status, who, request, self.project)
                    self.assertEqual(outcome, _TASK_STATUS_REFUSED)
                    self.assertIn(FOLLOWS_RECEIPTS_REFUSAL,
                                  [str(m) for m in request._messages])
                    self.assertUntouched(self.t2)
        self.assertEqual(self.row(self.m2)[0], 'Pending')

    def test_an_ordinary_task_still_changes_normally(self):
        r = self.row_post(self.pm, self.ordinary, Task.DONE)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.task_row(self.ordinary), (Task.DONE, True))

    def test_an_old_finance_confirmation_task_still_changes_normally(self):
        r = self.row_post(self.fin_a, self.old_name, Task.DONE)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.task_row(self.old_name), (Task.DONE, True))
        self.assertNotIn(FOLLOWS_RECEIPTS_REFUSAL, _texts(r))


# ===========================================================================
# 5. milestone_receive off (K5)
# ===========================================================================

@LIVE
class MilestoneReceiveOffTests(TasksBase):

    def test_refused_nothing_written(self):
        c = _client(self.fin_a)            # logged in first: the login writes its own row
        before = (ActivityLog.objects.count(), StatusTransition.objects.count())
        r = c.post(reverse('milestone_receive', args=[self.project.project_id, self.m2.pk]),
                   {'amount_received': '50000'})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(any('Record receipt' in t for t in _texts(r)))
        m2 = PaymentMilestone.objects.get(pk=self.m2.pk)
        self.assertEqual((m2.status, m2.amount_received, m2.received_date),
                         ('Pending', None, None))
        self.assertEqual(self.task_row(self.t2), (Task.NOT_STARTED, False))
        self.assertEqual((ActivityLog.objects.count(), StatusTransition.objects.count()),
                         before)


# ===========================================================================
# 6. The Finance box off (K6, Q7)
# ===========================================================================

@LIVE
class FinanceBoxOffTests(TasksBase):

    def test_an_amount_changes_no_money_and_the_description_saves(self):
        self.set_amount(self.m1, '50000')
        self.confirmed(self.m1, '20000')                      # stored amount_received 20000
        for typed in ('20000', '60000', '10000'):
            with self.subTest(typed=typed):
                r = self.finance_box(self.m1, typed, description=f'note {typed}')
                self.assertEqual(r.status_code, 302)
                m1 = PaymentMilestone.objects.get(pk=self.m1.pk)
                self.assertEqual((m1.status, m1.amount_received, m1.received_date),
                                 ('Pending', D('20000'), None))
                self.assertEqual(m1.milestone_description, f'note {typed}')
                self.assertEqual(self.task_row(self.t1), (Task.NOT_STARTED, False))
        self.assertEqual(self.milestone_moves(self.m1), [])

    def test_the_old_inputs_are_gone_from_the_page(self):
        page = _client(self.fin_a).get(
            reverse('project_overview', args=[self.project.project_id])).content.decode()
        self.assertNotIn('placeholder="₹ Amount Received"', page)
        self.assertNotIn('id="receiveModal"', page)
        self.assertNotIn('id="paymentCaptureModal"', page)
        self.assertIn('var _FINANCE_CONF_TASKS     = [];', page)
        # The invoice pop-up is not money received and stays (FIN-M-B3).
        self.assertIn('id="invoiceTaskModal"', page)
        # The description box stays in the Finance edit form.
        self.assertIn(f'id="editMilestone{self.m1.pk}"', page)
