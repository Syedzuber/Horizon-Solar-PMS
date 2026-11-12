"""FIN-M-B9a — the milestone <-> Finance task sync goes through proper code paths and
never swallows a failure.

WHY THIS FILE EXISTS
--------------------
R1: PaymentMilestone is the model of record; the three Finance confirmation tasks are the
trigger and the alert. The two are kept in step in both directions:

* Task Done -> milestone Received, inside _apply_task_status_change().
* Milestone Received -> task Done, from milestone_receive and from the Finance branch of
  project_overview's update_milestone.

Before FIN-M-B9a the second direction was a bulk Task update that skipped the task-status
chokepoint, so no task activity line, no payment alert and no reschedule check; and both
directions ended in `except Exception: pass`.

Rulings (Zuber, 12 Nov 2026):
* The record -> task direction calls the chokepoint with from_milestone=True, which skips
  the task -> record sync (the milestone is the source) and stamps the ledger row with
  REASON_MILESTONE_SYNC.
* Failure handling (a): the save always commits; a sync failure is logged at ERROR with
  the tag "FIN-M-B9a sync failed" and a traceback, and the user sees a warning. A failure
  writing the FIN-M-L change-log line inside the sync is logged too.
* A Blocked task is not forced Done: the chokepoint refuses Blocked -> Done, the task stays
  Blocked, Finance sees the refusal plus the warning, and the money is still saved.
* The record path sends the existing payment_milestone_reached alert (R9 renames it later).
* Matching stays by task name (code matching is FIN-M-B9b's question).

Every test that changes behaviour fails on the code before FIN-M-B9a. The chokepoint
import sits inside the one test body that needs the new keyword, so the module still
imports against the old code.

Run with:
    python manage.py test projects.tests_milestone_sync --settings=solarpms.test_settings
"""
from datetime import date
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.contrib.messages import get_messages
from django.test import Client, RequestFactory, TestCase
from django.urls import reverse

from .models import (
    SUBJECT_TASK, ActivityLog, PaymentMilestone, Project, ProjectPhase, StatusTransition, Task,
)

TAG = 'FIN-M-B9a sync failed'
RECORD_SIDE_WARNING = 'Milestone saved; the Finance task could not be updated — tell Admin.'
TASK_SIDE_WARNING = 'Task marked Done; the payment milestone could not be updated — tell Admin.'
MILESTONE_CODES = ('milestone_updated', 'milestone_amount_received', 'milestone_amounts_set')


def _profile(username, role):
    """A post_save signal creates the UserProfile; fetch and set, never create."""
    user = User.objects.create_user(username=username, password='x',
                                    first_name=username.title(), last_name='Test')
    profile = user.profile
    profile.role = role
    profile.is_active = True
    profile.save()
    return profile


def _client(profile):
    client = Client(SERVER_NAME='localhost')
    client.force_login(profile.user)
    return client


def _texts(response):
    return [str(m) for m in get_messages(response.wsgi_request)]


class MilestoneSyncBase(TestCase):
    """One Active Residential project with M1-M3 and the three Finance confirmation
    tasks, each flagged as a payment milestone and held by the Finance user."""

    def setUp(self):
        self.pm = _profile('ms_pm', 'PM')
        self.finance = _profile('ms_fin', 'Finance')
        self.project = Project.objects.create(
            customer_name='Milestone Sync', status='Active', project_type='Residential',
            customer_phone='9876543210', site_address='1 Sun Road', city='Lucknow',
            state='Uttar Pradesh', dc_capacity_kw=Decimal('5.00'), assigned_pm=self.pm,
            cascade_scheduling=False)
        phase = ProjectPhase.objects.create(project=self.project, phase_name='Finance',
                                            phase_order=1)
        self.tasks = {}
        for order, (name, label) in enumerate([
                ('Advance Payment Confirmation', 'M1'),
                ('Pre Dispatch Payment Confirmation', 'M2'),
                ('100% Payment Confirmation', 'M3')], start=1):
            self.tasks[label] = Task.objects.create(
                phase=phase, task_name=name, task_order=order,
                assigned_role=Task.FINANCE, assigned_to=self.finance,
                is_payment_milestone=True)
            PaymentMilestone.objects.create(
                project=self.project, milestone_name=label,
                milestone_description=label, amount=Decimal('100000.00'),
                created_by=self.pm)
        self.m2 = self.project.milestones.get(milestone_name='M2')
        self.m1 = self.project.milestones.get(milestone_name='M1')

    # -- helpers -------------------------------------------------------------

    def receive(self, milestone, amount='100000'):
        """Finance records a receipt from the Finance dashboard."""
        PaymentMilestone.objects.filter(pk=milestone.pk).update(status='Invoiced')
        return _client(self.finance).post(
            reverse('milestone_receive', args=[self.project.project_id, milestone.pk]),
            {'amount_received': amount})

    def receive_on_project_page(self, milestone, amount='100000'):
        """Finance records a receipt from the project page's milestone card."""
        return _client(self.finance).post(
            reverse('project_overview', args=[self.project.project_id]),
            {'action': 'update_milestone', 'milestone_pk': milestone.pk,
             'milestone_description': milestone.milestone_description,
             'amount_received': amount})

    def complete_task(self, label, **extra):
        """Finance marks a confirmation task Done from the task row (non-HTMX)."""
        task = self.tasks[label]
        return _client(self.finance).post(
            reverse('task_status_update', args=[self.project.project_id, task.pk]),
            {'status': Task.DONE, **extra})

    def task_lines(self, task):
        return list(ActivityLog.objects.filter(
            project=self.project, entity_type='Task', entity_id=task.pk)
            .values_list('action_code', 'action', 'actor_id'))

    def milestone_lines(self):
        return list(ActivityLog.objects.filter(
            project=self.project, action_code__in=MILESTONE_CODES)
            .order_by('timestamp', 'pk').values_list('action_code', 'action'))

    @staticmethod
    def alerts(notify_mock):
        return [c.args[0] for c in notify_mock.call_args_list]


# ---------------------------------------------------------------------------
# Record -> task: through the chokepoint
# ---------------------------------------------------------------------------

class RecordToTaskTests(MilestoneSyncBase):

    @mock.patch('projects.views.notify')
    def test_receipt_completes_the_task_through_the_chokepoint(self, notify_mock):
        task = self.tasks['M2']
        with mock.patch('projects.views.status_change_reschedules',
                        return_value=False) as reschedule_mock:
            self.receive(self.m2)

        task.refresh_from_db()
        self.assertEqual(task.status, Task.DONE)
        self.assertIsNotNone(task.completed_at)
        # The task's own activity line, under the Finance user's name.
        self.assertEqual(self.task_lines(task), [
            ('task_status_done',
             'Changed task status to Done: Pre Dispatch Payment Confirmation',
             self.finance.pk)])
        # The existing payment alert, once.
        self.assertEqual(self.alerts(notify_mock), ['payment_milestone_reached'])
        # The site reschedule hook was asked about this move.
        reschedule_mock.assert_called_once()
        asked_task, old_status, new_status = reschedule_mock.call_args.args
        self.assertEqual((asked_task.pk, old_status, new_status),
                         (task.pk, Task.NOT_STARTED, Task.DONE))

    @mock.patch('projects.views.notify')
    def test_project_page_receipt_also_goes_through_the_chokepoint(self, notify_mock):
        task = self.tasks['M2']
        self.receive_on_project_page(self.m2)

        self.m2.refresh_from_db()
        task.refresh_from_db()
        self.assertEqual(self.m2.status, 'Received')
        self.assertEqual(task.status, Task.DONE)
        self.assertEqual([c for c, _a, _u in self.task_lines(task)], ['task_status_done'])
        self.assertEqual(self.alerts(notify_mock), ['payment_milestone_reached'])

    @mock.patch('projects.views.notify')
    def test_ledger_row_keeps_the_milestone_sync_reason(self, notify_mock):
        from .models import REASON_MILESTONE_SYNC
        task = self.tasks['M2']
        self.receive(self.m2)
        rows = list(StatusTransition.objects.filter(
            subject_type=SUBJECT_TASK, subject_id=task.pk))
        self.assertEqual([(r.from_status, r.to_status, r.reason_code) for r in rows],
                         [(Task.NOT_STARTED, Task.DONE, REASON_MILESTONE_SYNC)])

    @mock.patch('projects.views.notify')
    def test_receipt_does_not_re_enter_the_task_to_milestone_sync(self, notify_mock):
        """One alert, and only milestone_receive's own FIN-M-L lines: no second
        "via completion of Finance task" line from the task side."""
        self.receive(self.m2, amount='90000')
        self.assertEqual(self.alerts(notify_mock), ['payment_milestone_reached'])
        lines = self.milestone_lines()
        self.assertEqual(len(lines), 2)
        self.assertFalse(any('via completion of Finance task' in text
                             for _c, text in lines))

    @mock.patch('projects.views.notify')
    def test_from_milestone_skips_the_task_to_milestone_sync(self, notify_mock):
        """The guard itself: called with from_milestone=True, the chokepoint completes
        the task and leaves the milestone alone, even one still Pending."""
        from django.contrib.messages.storage.fallback import FallbackStorage
        from .views import _TASK_STATUS_APPLIED, _apply_task_status_change

        request = RequestFactory().post('/', {'amount_received': '5'})
        request.user = self.finance.user
        request.session = {}
        request._messages = FallbackStorage(request)
        task = self.tasks['M1']

        outcome = _apply_task_status_change(task, Task.DONE, self.finance, request,
                                            self.project, from_milestone=True)

        self.assertEqual(outcome, _TASK_STATUS_APPLIED)
        task.refresh_from_db()
        self.m1.refresh_from_db()
        self.assertEqual(task.status, Task.DONE)
        self.assertEqual(self.m1.status, 'Pending')
        self.assertIsNone(self.m1.amount_received)

    @mock.patch('projects.views.notify')
    def test_blocked_task_stays_blocked_and_the_money_is_saved(self, notify_mock):
        """Ruling 4 (i): the chokepoint refuses Blocked -> Done. The receipt saves; the
        task does not move; Finance sees the refusal and the warning."""
        task = self.tasks['M2']
        Task.objects.filter(pk=task.pk).update(status=Task.BLOCKED)

        response = self.receive(self.m2, amount='75000')

        self.m2.refresh_from_db()
        task.refresh_from_db()
        self.assertEqual(self.m2.status, 'Received')
        self.assertEqual(self.m2.amount_received, Decimal('75000.00'))
        self.assertEqual(task.status, Task.BLOCKED)
        self.assertEqual(self.task_lines(task), [])
        self.assertEqual(self.alerts(notify_mock), [])
        texts = _texts(response)
        self.assertIn("Cannot move task from 'Blocked' to 'Done'.", texts)
        self.assertIn(RECORD_SIDE_WARNING, texts)
        self.assertIn('M2 marked as Received.', texts)

    @mock.patch('projects.views.notify')
    def test_forced_failure_is_logged_and_the_receipt_still_saves(self, notify_mock):
        task = self.tasks['M2']
        with mock.patch('projects.views._apply_task_status_change',
                        side_effect=RuntimeError('boom')), \
                self.assertLogs('projects.views', level='ERROR') as logs:
            response = self.receive(self.m2)

        self.m2.refresh_from_db()
        task.refresh_from_db()
        self.assertEqual(self.m2.status, 'Received')
        self.assertEqual(task.status, Task.NOT_STARTED)
        self.assertEqual(len(logs.records), 1)
        record = logs.records[0]
        self.assertIn(TAG, record.getMessage())
        self.assertIn(self.project.project_id, record.getMessage())
        self.assertIn('M2', record.getMessage())
        self.assertIn(str(task.pk), record.getMessage())
        self.assertIsNotNone(record.exc_info)       # the traceback
        self.assertIn(RECORD_SIDE_WARNING, _texts(response))

    @mock.patch('projects.views.notify')
    def test_already_done_task_is_left_alone(self, notify_mock):
        task = self.tasks['M2']
        Task.objects.filter(pk=task.pk).update(status=Task.DONE)
        with self.assertNoLogs('projects.views', level='ERROR'):
            response = self.receive(self.m2)
        task.refresh_from_db()
        self.assertEqual(task.status, Task.DONE)
        self.assertEqual(self.task_lines(task), [])
        self.assertEqual(self.alerts(notify_mock), [])
        self.assertNotIn(RECORD_SIDE_WARNING, _texts(response))

    @mock.patch('projects.views.notify')
    def test_already_received_milestone_changes_nothing(self, notify_mock):
        PaymentMilestone.objects.filter(pk=self.m2.pk).update(status='Received')
        before = self.milestone_lines()
        with self.assertNoLogs('projects.views', level='ERROR'):
            _client(self.finance).post(
                reverse('milestone_receive', args=[self.project.project_id, self.m2.pk]),
                {'amount_received': '100000'})
        self.assertEqual(self.tasks['M2'].__class__.objects.get(
            pk=self.tasks['M2'].pk).status, Task.NOT_STARTED)
        self.assertEqual(self.milestone_lines(), before)
        self.assertEqual(self.alerts(notify_mock), [])


# ---------------------------------------------------------------------------
# Task -> record: unchanged behaviour, failures logged
# ---------------------------------------------------------------------------

class TaskToRecordTests(MilestoneSyncBase):

    @mock.patch('projects.views.notify')
    def test_task_done_marks_the_milestone_received_with_fin_m_l_lines(self, notify_mock):
        self.complete_task('M1', amount_received='100000')

        self.m1.refresh_from_db()
        self.assertEqual(self.m1.status, 'Received')
        self.assertEqual(self.m1.received_date, date.today())
        today = f'{date.today().day} {date.today():%b %Y}'
        self.assertEqual(self.milestone_lines(), [
            ('milestone_updated',
             f'M1: status Pending → Received; received date blank → {today} via '
             f"completion of Finance task 'Advance Payment Confirmation' by "
             f'Ms_Fin Test (Finance)'),
            ('milestone_amount_received', 'M1: amount received blank → ₹1,00,000'),
        ])
        self.assertEqual(self.alerts(notify_mock), ['payment_milestone_reached'])

    @mock.patch('projects.views.notify')
    def test_forced_sync_failure_is_logged_and_the_task_stays_done(self, notify_mock):
        with mock.patch.object(PaymentMilestone.objects, 'filter',
                               side_effect=RuntimeError('boom')), \
                self.assertLogs('projects.views', level='ERROR') as logs:
            response = self.complete_task('M1')

        task = Task.objects.get(pk=self.tasks['M1'].pk)
        self.m1.refresh_from_db()
        self.assertEqual(task.status, Task.DONE)
        self.assertEqual(self.m1.status, 'Pending')
        self.assertEqual(len(logs.records), 1)
        message = logs.records[0].getMessage()
        self.assertIn(TAG, message)
        self.assertIn(self.project.project_id, message)
        self.assertIn('M1', message)
        self.assertIn(str(task.pk), message)
        self.assertIsNotNone(logs.records[0].exc_info)
        self.assertIn(TASK_SIDE_WARNING, _texts(response))

    @mock.patch('projects.views.notify')
    def test_change_log_failure_is_logged_not_swallowed(self, notify_mock):
        """Ruling 7: the milestone and the task both commit; the failed FIN-M-L line is
        logged at ERROR with the tag."""
        with mock.patch('projects.views._log_milestone_changes',
                        side_effect=RuntimeError('log broke')), \
                self.assertLogs('projects.views', level='ERROR') as logs:
            self.complete_task('M1', amount_received='100000')

        self.m1.refresh_from_db()
        self.assertEqual(self.m1.status, 'Received')
        self.assertEqual(Task.objects.get(pk=self.tasks['M1'].pk).status, Task.DONE)
        self.assertEqual(len(logs.records), 1)
        self.assertIn(TAG, logs.records[0].getMessage())
        self.assertIn('change-log', logs.records[0].getMessage())
        self.assertIsNotNone(logs.records[0].exc_info)

    @mock.patch('projects.views.notify')
    def test_already_received_milestone_is_left_alone(self, notify_mock):
        PaymentMilestone.objects.filter(pk=self.m1.pk).update(
            status='Received', amount_received=Decimal('100000.00'))
        with self.assertNoLogs('projects.views', level='ERROR'):
            response = self.complete_task('M1', amount_received='1')
        self.m1.refresh_from_db()
        self.assertEqual(self.m1.amount_received, Decimal('100000.00'))
        self.assertEqual(self.milestone_lines(), [])
        self.assertNotIn(TASK_SIDE_WARNING, _texts(response))
