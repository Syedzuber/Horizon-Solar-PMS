"""FIN-M-RN2 — no new task may take a name other features look tasks up by, and a task
marked Not Applicable cannot change status through any path.

WHY THIS FILE EXISTS
--------------------
FIN-M-RN stopped renames to and from the protected names (reserved_task_names()) and
renames of Not Applicable tasks. Its pre-flight found two gaps, both proved on the local
copy with rolled-back attempts:

1. "Add task" accepted a protected name in any case, so a site could hold two "Customer
   Handover" tasks and every name lookup (invoice-due trigger, invoice step,
   payment-confirmation sync) would match both.
2. Nothing on the server refused a status change on a Not Applicable task. Only the
   templates hid the controls. On all eleven paths tried (task row, task detail,
   Finance's send-invoice and confirmation completions, the receipt and invoice syncs,
   two-step submit and approve, Blocked) an N/A task moved, and an N/A trigger task
   fired the "Invoice due" alert.

Rulings (Zuber, 14 Nov 2026):
* Add task refuses a protected name, ignoring case, on every project type, with RN's
  wording. Nothing is saved.
* A refusal rung straight after the mirror rung of _apply_task_status_change(): "'<task>'
  is marked Not Applicable. Un-mark it before changing its status. No change was made."
* _approval_preconditions() refuses the same way, so submit, approve and reject are
  refused too (submit changes no status but would leave the task awaiting an approval
  that could never pass).
* Milestone -> task sync (receipt or invoice) with an N/A target: the money or invoice is
  saved, the task is skipped, and Finance reads "M3 saved. Its task '…' is marked Not
  Applicable, so it was left as it is." No "tell Admin": nothing is broken.

Every test that checks a refusal fails on the code before FIN-M-RN2.

Run with:
    python manage.py test projects.tests_rn2_guards --settings=solarpms.test_settings
"""
from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.contrib.messages import get_messages
from django.test import Client, TestCase
from django.urls import reverse

from .models import (
    SUBJECT_TASK, ActivityLog, Issue, PaymentMilestone, Project, ProjectPhase, PunchPoint,
    StatusTransition, Task,
)
from .permissions import reserved_task_names
from .tests_two_step_completion import TwoStepFixture, _client_for

NA_REFUSAL = ("'{}' is marked Not Applicable. Un-mark it before changing its status. "
              "No change was made.")
SYNC_SKIPPED = "{} saved. Its task '{}' is marked Not Applicable, so it was left as it is."
RESERVED_REFUSAL = '"{}" is a name the finance workflow looks tasks up by. Choose a different name.'

TRIGGER = 'Customer Handover'
SEND_INVOICE = 'Send Invoice - Final Payment'
CONFIRMATION = '100% Payment Confirmation'


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


class ResidentialFixture(TestCase):
    """One Active Residential site with the three M3 tasks and a Pending M3:
    the trigger (Customer Handover, PM), the send-invoice task and the payment
    confirmation (both Finance). Every task is assigned and dated, so any refusal seen
    is the N/A rule and not the unassigned or no-due-date guard."""

    def setUp(self):
        self.pm = _profile('rn2_pm', 'PM')
        self.finance = _profile('rn2_fin', 'Finance')
        self.project = Project.objects.create(
            customer_name='RN2 Guards', status='Active', project_type='Residential',
            customer_phone='9876543210', site_address='1 Sun Road', city='Lucknow',
            state='Uttar Pradesh', dc_capacity_kw=Decimal('5.00'), assigned_pm=self.pm,
            cascade_scheduling=False)
        self.phase = ProjectPhase.objects.create(project=self.project,
                                                 phase_name='Commissioning', phase_order=1)
        due = date.today() + timedelta(days=7)
        self.trigger = Task.objects.create(
            phase=self.phase, task_name=TRIGGER, task_order=1, assigned_role=Task.PM,
            assigned_to=self.pm, due_date=due)
        self.send_invoice = Task.objects.create(
            phase=self.phase, task_name=SEND_INVOICE, task_order=2,
            assigned_role=Task.FINANCE, assigned_to=self.finance, due_date=due)
        self.confirmation = Task.objects.create(
            phase=self.phase, task_name=CONFIRMATION, task_order=3,
            assigned_role=Task.FINANCE, assigned_to=self.finance, due_date=due,
            is_payment_milestone=True)
        self.m3 = PaymentMilestone.objects.create(
            project=self.project, milestone_name='M3', milestone_description='Final',
            amount=Decimal('100000.00'), created_by=self.pm)

    # -- helpers -------------------------------------------------------------

    def set_na(self, task, marking=True):
        """Mark or un-mark through the real view, the only way production does it."""
        response = _client(self.pm).post(
            reverse('task_set_not_applicable', args=[self.project.project_id, task.pk]),
            {'not_applicable': '1' if marking else '0',
             'not_applicable_reason': 'RN2 test'})
        task.refresh_from_db()
        self.assertEqual(task.is_not_applicable, marking,
                         f'fixture could not set N/A={marking}: {response.status_code}')
        return task

    def move(self, task, profile, status, url='task_status_update', **extra):
        return _client(profile).post(
            reverse(url, args=[self.project.project_id, task.pk]),
            {'status': status, **extra})

    def ledger(self, task):
        return StatusTransition.objects.filter(subject_type=SUBJECT_TASK,
                                               subject_id=task.pk).count()

    def status_lines(self, task):
        return ActivityLog.objects.filter(entity_type='Task', entity_id=task.pk,
                                          action_code__startswith='task_status_').count()


# ---------------------------------------------------------------------------
# 1. Add task refuses a protected name
# ---------------------------------------------------------------------------

class AddTaskReservedNameTests(ResidentialFixture):

    def add(self, name, project=None, phase=None, hx=False):
        project = project or self.project
        extra = {'HTTP_HX_REQUEST': 'true'} if hx else {}
        return _client(self.pm).post(
            reverse('task_add', args=[project.project_id]),
            {'phase': (phase or self.phase).pk, 'task_name': name,
             'assigned_role': Task.PM, 'assigned_to': self.pm.pk}, **extra)

    def count(self, name, project=None):
        return Task.objects.filter(phase__project=project or self.project,
                                   task_name__iexact=name).count()

    def test_customer_handover_is_refused_and_nothing_is_created(self):
        before = Task.objects.count()
        response = self.add('Customer Handover')
        self.assertEqual(response.status_code, 200)   # the form re-rendered, no redirect
        self.assertEqual(Task.objects.count(), before)
        self.assertEqual(self.count(TRIGGER), 1)
        self.assertIn(RESERVED_REFUSAL.format('Customer Handover'),
                      response.context['form'].errors['task_name'])

    def test_send_invoice_in_lower_case_is_refused(self):
        response = self.add('send invoice - final payment')
        self.assertEqual(self.count(SEND_INVOICE), 1)
        self.assertIn(RESERVED_REFUSAL.format('send invoice - final payment'),
                      response.context['form'].errors['task_name'])

    def test_every_protected_name_is_refused_in_any_case(self):
        for name in sorted(reserved_task_names()):
            for spelled in (name, name.upper(), name.title()):
                with self.subTest(name=spelled):
                    before = Task.objects.count()
                    self.add(spelled)
                    self.assertEqual(Task.objects.count(), before)

    def test_the_htmx_modal_shows_the_refusal_inline(self):
        response = self.add('CUSTOMER HANDOVER', hx=True)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'projects/task_add_modal.html')
        self.assertContains(response, 'a name the finance workflow looks tasks up by')
        self.assertEqual(self.count(TRIGGER), 1)

    def test_refused_on_a_non_residential_site_too(self):
        site = Project.objects.create(
            customer_name='RN2 OPEX', status='Active', project_type='OPEX',
            customer_phone='9876543210', site_address='2 Sun Road', city='Lucknow',
            state='Uttar Pradesh', dc_capacity_kw=Decimal('50.00'), assigned_pm=self.pm)
        phase = ProjectPhase.objects.create(project=site, phase_name='Closeout',
                                            phase_order=1)
        Task.objects.create(phase=phase, task_name='Handover', task_order=1,
                            assigned_role=Task.PM, assigned_to=self.pm)
        self.add('Advance Payment Confirmation', project=site, phase=phase)
        self.assertEqual(self.count('Advance Payment Confirmation', project=site), 0)

    def test_an_ordinary_task_is_still_created(self):
        response = self.add('Customer Handover photos')
        self.assertEqual(response.status_code, 302)
        task = Task.objects.get(phase=self.phase, task_name='Customer Handover photos')
        self.assertEqual(task.assigned_to, self.pm)


# ---------------------------------------------------------------------------
# 2. The chokepoint refuses every status change on an N/A task
# ---------------------------------------------------------------------------

@mock.patch('projects.views.notify')
class NotApplicableStatusTests(ResidentialFixture):

    def assert_untouched(self, response, task, status=Task.NOT_STARTED, ledger=0, lines=0):
        """`ledger` / `lines`: the ledger rows and status log lines the task already
        had before the refused move (an earlier, legitimate move leaves one of each)."""
        task.refresh_from_db()
        self.assertEqual(task.status, status)
        self.assertTrue(task.is_not_applicable)
        self.assertIsNone(task.completed_at)
        self.assertEqual(self.ledger(task), ledger)
        self.assertEqual(self.status_lines(task), lines)
        self.assertIn(NA_REFUSAL.format(task.task_name), _texts(response))

    def test_row_done_on_an_na_trigger_is_refused_and_sends_no_alert(self, notify):
        self.set_na(self.trigger)
        response = self.move(self.trigger, self.pm, Task.DONE)
        self.assert_untouched(response, self.trigger)
        notify.assert_not_called()
        self.m3.refresh_from_db()
        self.assertEqual(self.m3.status, PaymentMilestone.PENDING)

    def test_row_in_progress_is_refused(self, notify):
        self.set_na(self.trigger)
        response = self.move(self.trigger, self.pm, Task.IN_PROGRESS)
        self.assert_untouched(response, self.trigger)

    def test_row_blocked_is_refused_and_raises_no_issue(self, notify):
        self.set_na(self.trigger)
        response = self.move(self.trigger, self.pm, Task.BLOCKED,
                             block_issue_title='RN2 block')
        self.assert_untouched(response, self.trigger)
        self.assertFalse(Issue.objects.filter(task=self.trigger).exists())

    def test_task_detail_done_is_refused(self, notify):
        self.set_na(self.trigger)
        response = self.move(self.trigger, self.pm, Task.DONE,
                             url='task_detail_status_update')
        self.assert_untouched(response, self.trigger)
        notify.assert_not_called()

    def test_an_in_progress_na_task_cannot_be_completed_either(self, notify):
        self.move(self.trigger, self.pm, Task.IN_PROGRESS)
        self.set_na(self.trigger)
        response = self.move(self.trigger, self.pm, Task.DONE)
        self.assert_untouched(response, self.trigger, status=Task.IN_PROGRESS,
                              ledger=1, lines=1)

    def test_finance_completing_an_na_send_invoice_task_records_no_invoice(self, notify):
        self.set_na(self.send_invoice)
        response = self.move(self.send_invoice, self.finance, Task.DONE,
                             invoice_form='1', invoice_number='RN2-INV-1',
                             invoice_date=date.today().isoformat(),
                             invoice_amount='1000')
        self.assert_untouched(response, self.send_invoice)
        self.m3.refresh_from_db()
        self.assertEqual(self.m3.status, PaymentMilestone.PENDING)
        self.assertEqual(self.m3.invoice_number, '')

    def test_finance_completing_an_na_confirmation_task_marks_no_money_received(self, notify):
        self.set_na(self.confirmation)
        response = self.move(self.confirmation, self.finance, Task.DONE,
                             amount_received='100000')
        self.assert_untouched(response, self.confirmation)
        self.m3.refresh_from_db()
        self.assertEqual(self.m3.status, PaymentMilestone.PENDING)
        self.assertIsNone(self.m3.amount_received)
        notify.assert_not_called()

    def test_unmarking_restores_status_changes(self, notify):
        self.set_na(self.trigger)
        self.move(self.trigger, self.pm, Task.DONE)
        self.set_na(self.trigger, marking=False)
        self.move(self.trigger, self.pm, Task.DONE)
        self.trigger.refresh_from_db()
        self.assertEqual(self.trigger.status, Task.DONE)
        self.assertEqual(self.ledger(self.trigger), 1)
        # Un-marked, the trigger is ordinary again and its Done raises the alert.
        self.assertIn('invoice_due', [c.args[0] for c in notify.call_args_list])


# ---------------------------------------------------------------------------
# 3. Milestone -> task sync: money and invoice saved, N/A task skipped
# ---------------------------------------------------------------------------

@mock.patch('projects.views.notify')
class MilestoneSyncSkipsNotApplicableTests(ResidentialFixture):

    def assert_skipped(self, response, task):
        task.refresh_from_db()
        self.assertEqual(task.status, Task.NOT_STARTED)
        self.assertEqual(self.ledger(task), 0)
        texts = _texts(response)
        self.assertIn(SYNC_SKIPPED.format('M3', task.task_name), texts)
        self.assertFalse([t for t in texts if 'tell Admin' in t], texts)
        self.assertNotIn(NA_REFUSAL.format(task.task_name), texts)

    def test_receipt_from_the_finance_modal_is_saved_and_the_task_left(self, notify):
        self.set_na(self.confirmation)
        PaymentMilestone.objects.filter(pk=self.m3.pk).update(status='Invoiced')
        response = _client(self.finance).post(
            reverse('milestone_receive', args=[self.project.project_id, self.m3.pk]),
            {'amount_received': '100000'})
        self.m3.refresh_from_db()
        self.assertEqual(self.m3.status, PaymentMilestone.RECEIVED)
        self.assertEqual(self.m3.amount_received, Decimal('100000.00'))
        self.assert_skipped(response, self.confirmation)

    def test_receipt_from_the_project_page_is_saved_and_the_task_left(self, notify):
        self.set_na(self.confirmation)
        response = _client(self.finance).post(
            reverse('project_overview', args=[self.project.project_id]),
            {'action': 'update_milestone', 'milestone_pk': self.m3.pk,
             'milestone_description': 'Final', 'amount_received': '100000'})
        self.m3.refresh_from_db()
        self.assertEqual(self.m3.status, PaymentMilestone.RECEIVED)
        self.assert_skipped(response, self.confirmation)

    def test_invoice_from_the_card_is_saved_and_the_task_left(self, notify):
        self.set_na(self.send_invoice)
        response = _client(self.finance).post(
            reverse('milestone_invoice', args=[self.project.project_id, self.m3.pk]),
            {'invoice_number': 'RN2-INV-2', 'invoice_date': date.today().isoformat(),
             'invoice_amount': '1000'})
        self.m3.refresh_from_db()
        self.assertEqual(self.m3.status, PaymentMilestone.INVOICED)
        self.assertEqual(self.m3.invoice_number, 'RN2-INV-2')
        self.assert_skipped(response, self.send_invoice)

    def test_an_applicable_task_is_still_completed_by_the_receipt(self, notify):
        PaymentMilestone.objects.filter(pk=self.m3.pk).update(status='Invoiced')
        _client(self.finance).post(
            reverse('milestone_receive', args=[self.project.project_id, self.m3.pk]),
            {'amount_received': '100000'})
        self.confirmation.refresh_from_db()
        self.assertEqual(self.confirmation.status, Task.DONE)


# ---------------------------------------------------------------------------
# 4. Two-step approval refuses an N/A task: submit, approve, reject
# ---------------------------------------------------------------------------

class TwoStepNotApplicableTests(TwoStepFixture):

    def set_na(self, marking=True):
        _client_for(self.pm).post(
            reverse('task_set_not_applicable', args=[self.site.project_id, self.task.pk]),
            {'not_applicable': '1' if marking else '0', 'not_applicable_reason': 'RN2'})
        self.task.refresh_from_db()
        self.assertEqual(self.task.is_not_applicable, marking)

    def submitted(self):
        self._in_progress()
        self._post('task_submit_for_approval', {'submission_remarks': 'work done'})
        self.task.refresh_from_db()
        self.assertIsNotNone(self.task.submitted_at)

    def test_submit_is_refused(self):
        self._in_progress()
        self.set_na()
        response = self._post('task_submit_for_approval', {'submission_remarks': 'done'})
        self.task.refresh_from_db()
        self.assertIsNone(self.task.submitted_at)
        self.assertNotIn('task_submitted_for_approval', self._codes(self.task))
        self.assertIn(NA_REFUSAL.format(self.task.task_name), self._messages(response))

    def test_approve_is_refused_and_writes_nothing(self):
        self.submitted()
        self.set_na()
        ledger_before = len(self._ledger(self.task))
        response = self._post('task_approve', {'approval_remarks': 'ok'}, profile=self.qaqc)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, Task.IN_PROGRESS)
        self.assertIsNone(self.task.approved_at)
        self.assertEqual(len(self._ledger(self.task)), ledger_before)
        self.assertIn(NA_REFUSAL.format(self.task.task_name), self._messages(response))

    def test_reject_is_refused_and_raises_no_punch_point(self):
        self.submitted()
        self.set_na()
        response = self._post('task_reject', {'approval_remarks': 'no'}, profile=self.qaqc)
        self.task.refresh_from_db()
        self.assertIsNotNone(self.task.submitted_at)
        self.assertFalse(PunchPoint.objects.filter(task=self.task).exists())
        self.assertIn(NA_REFUSAL.format(self.task.task_name), self._messages(response))

    def test_unmarked_the_task_is_approved_as_before(self):
        self.submitted()
        self.set_na()
        self.set_na(marking=False)
        self._post('task_approve', {'approval_remarks': 'ok'}, profile=self.qaqc)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, Task.DONE)
