"""FIN-M-B3 — Finance records the invoice on M2 and M3 (R7), and the "Send Invoice" task
and the milestone stay in step.

WHY THIS FILE EXISTS
--------------------
R7: M2 and M3 are invoiced before the money is received. Before FIN-M-B3 the only
invoice path, milestone_invoice, took no number and no amount, accepted M1, deleted and
OPEX projects, and was reached by no screen; the two "Send Invoice" tasks completed
without touching the milestone at all.

Rulings (Zuber, 12 Nov 2026):
* Separate fields: invoice_number (up to 50 characters) and invoice_amount; BD's
  `amount` is never overwritten.
* Duplicate numbers refused across live projects, trimmed and case-insensitive, in the
  view (no database index).
* Two entry points, one writer: the card's "Mark invoiced" form and completing the M2/M3
  "Send Invoice" task through the invoice modal.
* Who: Finance only (ruling (a)). A PM or Coordinator is refused on the task while the
  milestone is Pending; once it is Invoiced or Received, the task completes as before.
* The task detail page refuses Done on those tasks while Pending: "Record the invoice from
  the project page".
* Correction while Invoiced, logged old -> new; none after Received.
* A receipt straight from Pending is still allowed, with no warning (B3b).
* Number, date and amount required; date may be past, not future; amount at least ₹0.01;
  pre-filled from the milestone amount when set.
* New money code milestone_invoice_amount. Failure tag reused: "FIN-M-B9a sync failed".

Behaviour tests fail on the code before FIN-M-B3. The helpers that do not exist there are
imported inside the test bodies, so this module still imports against the old code.

Run with:
    python manage.py test projects.tests_milestone_invoice --settings=solarpms.test_settings
"""
from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.contrib.messages import get_messages
from django.db import connection
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from .models import (
    SUBJECT_PAYMENT_MILESTONE, SUBJECT_TASK, ActivityLog, PaymentMilestone, Project,
    ProjectPhase, StatusTransition, Task,
)

TAG = 'FIN-M-B9a sync failed'
RECORD_SIDE_WARNING = 'Milestone saved; the Finance task could not be updated — tell Admin.'
TASK_SIDE_WARNING = 'Task marked Done; the payment milestone could not be updated — tell Admin.'
PLAIN = 'milestone_updated'
INVOICE_MONEY = 'milestone_invoice_amount'
MILESTONE_CODES = (PLAIN, INVOICE_MONEY, 'milestone_amounts_set', 'milestone_amount_received')
TODAY = date.today()


def _day(value):
    """The log's date format, written out independently of the view's helper."""
    return f'{value.day} {value:%b %Y}'


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


def _form(number='INV/26-27/0412', day=None, amount='150000'):
    return {'invoice_number': number,
            'invoice_date': (day or TODAY).isoformat() if day != '' else '',
            'invoice_amount': amount}


class InvoiceBase(TestCase):
    """One Active Residential project with M1-M3 (M1 ₹50,000, M2 ₹1,50,000, M3 no amount),
    the three "Send Invoice" tasks and the M2 confirmation task, all held by Finance."""

    def setUp(self):
        self.pm = _profile('inv_pm', 'PM')
        self.coordinator = _profile('inv_pc', 'Project Coordinator')
        self.finance = _profile('inv_fin', 'Finance')
        self.bd = _profile('inv_bd', 'BD')
        self.ceo = _profile('inv_ceo', 'CEO')
        self.project = self._project('Invoice Site')
        self.project.coordinators.add(self.coordinator)
        phase = ProjectPhase.objects.create(project=self.project, phase_name='Finance',
                                            phase_order=1)
        self.tasks = {}
        for order, name in enumerate([
                'Send Invoice - Advance Payment', 'Send Invoice - Material Supply',
                'Pre Dispatch Payment Confirmation', 'Send Invoice - Final Payment'],
                start=1):
            self.tasks[name] = Task.objects.create(
                phase=phase, task_name=name, task_order=order,
                assigned_role=Task.FINANCE, assigned_to=self.finance,
                is_payment_milestone=(name == 'Pre Dispatch Payment Confirmation'))
        self.m1, self.m2, self.m3 = self._milestones(
            self.project, Decimal('50000.00'), Decimal('150000.00'), None)
        self.m2_task = self.tasks['Send Invoice - Material Supply']
        self.m3_task = self.tasks['Send Invoice - Final Payment']

    def _project(self, name, project_type='Residential'):
        return Project.objects.create(
            customer_name=name, status='Active', project_type=project_type,
            customer_phone='9876543210', site_address='1 Sun Road', city='Lucknow',
            state='Uttar Pradesh', dc_capacity_kw=Decimal('5.00'), assigned_pm=self.pm,
            cascade_scheduling=False)

    def _milestones(self, project, *amounts):
        return [PaymentMilestone.objects.create(
                    project=project, milestone_name=f'M{n}', milestone_description=f'M{n}',
                    amount=amount, created_by=self.pm)
                for n, amount in enumerate(amounts, start=1)]

    # -- helpers -------------------------------------------------------------

    def invoice(self, milestone, who=None, **form):
        """POST the card's invoice form."""
        return _client(who or self.finance).post(
            reverse('milestone_invoice',
                    args=[milestone.project.project_id, milestone.pk]),
            _form(**form))

    def complete(self, task, who=None, modal=True, **form):
        """Mark a task Done from the task row; `modal` adds the invoice modal's fields."""
        data = {'status': Task.DONE}
        if modal:
            data.update({'invoice_form': '1', **_form(**form)})
        return _client(who or self.finance).post(
            reverse('task_status_update', args=[self.project.project_id, task.pk]), data)

    def lines(self, project=None):
        return list(ActivityLog.objects.filter(
            project=project or self.project, action_code__in=MILESTONE_CODES)
            .order_by('timestamp', 'pk').values_list('action_code', 'action', 'actor_id'))

    def assert_untouched(self, milestone, status='Pending'):
        milestone.refresh_from_db()
        self.assertEqual(milestone.status, status)
        self.assertEqual(milestone.invoice_number, '')
        self.assertIsNone(milestone.invoice_amount)
        self.assertIsNone(milestone.invoice_date)

    def status_of(self, task):
        task.refresh_from_db()
        return task.status

    @staticmethod
    def alerts(notify_mock):
        return [c.args[0] for c in notify_mock.call_args_list]


# ---------------------------------------------------------------------------
# The two entry points
# ---------------------------------------------------------------------------

class CardInvoiceTests(InvoiceBase):

    @mock.patch('projects.views.notify')
    def test_finance_invoices_m2_from_the_card(self, notify_mock):
        response = self.invoice(self.m2, number='  INV/26-27/0412 ', amount='148500.50')
        self.assertRedirects(response, reverse('project_overview',
                                               args=[self.project.project_id]),
                             fetch_redirect_response=False)
        self.m2.refresh_from_db()
        self.assertEqual(self.m2.status, 'Invoiced')
        self.assertEqual(self.m2.invoice_number, 'INV/26-27/0412')
        self.assertEqual(self.m2.invoice_date, TODAY)
        self.assertEqual(self.m2.invoice_amount, Decimal('148500.50'))
        self.assertEqual(self.m2.amount, Decimal('150000.00'), "BD's amount untouched")
        # The send-invoice task is Done THROUGH THE CHOKEPOINT: ledger row with the sync
        # reason and the ordinary task activity line.
        self.assertEqual(self.status_of(self.m2_task), Task.DONE)
        row = StatusTransition.objects.get(subject_type=SUBJECT_TASK,
                                           subject_id=self.m2_task.pk)
        self.assertEqual(row.reason_code, 'milestone_sync')
        self.assertTrue(ActivityLog.objects.filter(
            entity_type='Task', entity_id=self.m2_task.pk,
            action_code='task_status_done').exists())
        ms_rows = StatusTransition.objects.filter(subject_type=SUBJECT_PAYMENT_MILESTONE,
                                                  subject_id=self.m2.pk)
        self.assertEqual([(r.from_status, r.to_status) for r in ms_rows],
                         [('Pending', 'Invoiced')])
        self.assertEqual(self.lines(), [
            (PLAIN, f"M2: status Pending → Invoiced; invoice date blank → {_day(TODAY)}; "
                    f"invoice number blank → 'INV/26-27/0412'", self.finance.pk),
            (INVOICE_MONEY, 'M2: invoice amount blank → ₹1,48,500.50', self.finance.pk),
        ])
        self.assertEqual(self.alerts(notify_mock), [], 'invoicing sends no alert')
        self.assertIn('M2 invoiced (INV/26-27/0412).', _texts(response))
        # The other tasks are untouched.
        self.assertEqual(self.status_of(self.m3_task), Task.NOT_STARTED)
        self.assertEqual(self.status_of(self.tasks['Pre Dispatch Payment Confirmation']),
                         Task.NOT_STARTED)

    def test_finance_invoices_m3_from_the_card(self):
        self.invoice(self.m3, number='INV-3', amount='99000')
        self.m3.refresh_from_db()
        self.assertEqual((self.m3.status, self.m3.invoice_amount),
                         ('Invoiced', Decimal('99000.00')))
        self.assertEqual(self.status_of(self.m3_task), Task.DONE)
        self.assertEqual(self.status_of(self.m2_task), Task.NOT_STARTED)

    def test_a_past_invoice_date_is_accepted(self):
        self.invoice(self.m2, day=TODAY - timedelta(days=40))
        self.m2.refresh_from_db()
        self.assertEqual(self.m2.invoice_date, TODAY - timedelta(days=40))

    def test_a_get_redirects_to_the_project_page_and_saves_nothing(self):
        response = _client(self.finance).get(
            reverse('milestone_invoice', args=[self.project.project_id, self.m2.pk]))
        self.assertRedirects(response, reverse('project_overview',
                                               args=[self.project.project_id]),
                             fetch_redirect_response=False)
        self.assert_untouched(self.m2)


class TaskInvoiceTests(InvoiceBase):

    @mock.patch('projects.views.notify')
    def test_finance_completes_the_m2_send_invoice_task_with_the_invoice(self, notify_mock):
        response = self.complete(self.m2_task, number='INV-77', amount='150000')
        self.assertEqual(self.status_of(self.m2_task), Task.DONE)
        self.m2.refresh_from_db()
        self.assertEqual(self.m2.status, 'Invoiced')
        self.assertEqual((self.m2.invoice_number, self.m2.invoice_date,
                          self.m2.invoice_amount),
                         ('INV-77', TODAY, Decimal('150000.00')))
        # The task row is a direct act (no sync reason); the milestone's row is the one
        # the invoice writer records.
        row = StatusTransition.objects.get(subject_type=SUBJECT_TASK,
                                           subject_id=self.m2_task.pk)
        self.assertEqual(row.reason_code, '')
        self.assertEqual(
            [(r.from_status, r.to_status) for r in StatusTransition.objects.filter(
                subject_type=SUBJECT_PAYMENT_MILESTONE, subject_id=self.m2.pk)],
            [('Pending', 'Invoiced')])
        self.assertEqual(self.lines(), [
            (PLAIN, f"M2: status Pending → Invoiced; invoice date blank → {_day(TODAY)}; "
                    f"invoice number blank → 'INV-77' via completion of task "
                    f"'Send Invoice - Material Supply'", self.finance.pk),
            (INVOICE_MONEY, 'M2: invoice amount blank → ₹1,50,000', self.finance.pk),
        ])
        self.assertEqual(self.alerts(notify_mock), [])
        self.assertIn('M2 invoiced (INV-77).', _texts(response))

    def test_a_bad_amount_refuses_the_task_and_the_invoice(self):
        response = self.complete(self.m2_task, amount='abc')
        self.assertEqual(self.status_of(self.m2_task), Task.NOT_STARTED)
        self.assert_untouched(self.m2)
        self.assertTrue(any(t.startswith('M2 not invoiced.') and
                            t.endswith('The task was not marked Done.')
                            for t in _texts(response)), _texts(response))
        self.assertEqual(self.lines(), [])

    def test_a_missing_number_refuses_the_task_and_the_invoice(self):
        response = self.complete(self.m2_task, number='   ')
        self.assertEqual(self.status_of(self.m2_task), Task.NOT_STARTED)
        self.assert_untouched(self.m2)
        self.assertIn('M2 not invoiced. Invoice number is required. The task was not '
                      'marked Done.', _texts(response))

    def test_without_the_modal_the_done_is_refused_with_a_pointer(self):
        response = self.complete(self.m2_task, modal=False)
        self.assertEqual(self.status_of(self.m2_task), Task.NOT_STARTED)
        self.assert_untouched(self.m2)
        self.assertIn("Completing 'Send Invoice - Material Supply' records the invoice "
                      "for M2. Record the invoice from the project page.", _texts(response))

    def test_the_task_detail_page_refuses_done_while_pending(self):
        response = _client(self.finance).post(
            reverse('task_detail_status_update',
                    args=[self.project.project_id, self.m2_task.pk]),
            {'status': Task.DONE})
        self.assertEqual(self.status_of(self.m2_task), Task.NOT_STARTED)
        self.assert_untouched(self.m2)
        self.assertTrue(any('Record the invoice from the project page' in t
                            for t in _texts(response)), _texts(response))

    def test_once_invoiced_the_task_completes_without_a_form(self):
        PaymentMilestone.objects.filter(pk=self.m2.pk).update(
            status='Invoiced', invoice_number='OLD-1')
        self.complete(self.m2_task, modal=False)
        self.assertEqual(self.status_of(self.m2_task), Task.DONE)
        self.m2.refresh_from_db()
        self.assertEqual((self.m2.status, self.m2.invoice_number), ('Invoiced', 'OLD-1'))

    def test_once_received_the_task_completes_without_a_form(self):
        # The 20 legacy M2s: money already in, the task is bookkeeping.
        PaymentMilestone.objects.filter(pk=self.m2.pk).update(status='Received')
        self.complete(self.m2_task, modal=False)
        self.assertEqual(self.status_of(self.m2_task), Task.DONE)

    def test_the_m1_send_invoice_task_behaves_as_before(self):
        # R4: M1 is not invoiced; its task completes without a form and M1 is untouched.
        task = self.tasks['Send Invoice - Advance Payment']
        self.complete(task, modal=False)
        self.assertEqual(self.status_of(task), Task.DONE)
        self.assert_untouched(self.m1)

    def test_a_writer_failure_leaves_the_done_standing_and_is_logged(self):
        with mock.patch('projects.views._record_milestone_invoice',
                        side_effect=RuntimeError('boom')), \
                self.assertLogs('projects.views', level='ERROR') as logs:
            response = self.complete(self.m2_task)
        self.assertEqual(self.status_of(self.m2_task), Task.DONE)
        self.assert_untouched(self.m2)
        self.assertIn(TASK_SIDE_WARNING, _texts(response))
        self.assertTrue(any(TAG in line for line in logs.output), logs.output)


# ---------------------------------------------------------------------------
# Refusals: nothing saved, a message every time
# ---------------------------------------------------------------------------

class CardRefusalTests(InvoiceBase):

    def assert_refused(self, response, message, milestone=None, status='Pending'):
        milestone = milestone or self.m2
        self.assert_untouched(milestone, status)
        self.assertIn(message, _texts(response))
        self.assertEqual(self.lines(milestone.project), [])
        self.assertEqual(self.status_of(self.m2_task), Task.NOT_STARTED)

    def test_missing_number(self):
        self.assert_refused(self.invoice(self.m2, number=''),
                            'M2 not invoiced. Invoice number is required.')

    def test_number_over_50_characters(self):
        self.assert_refused(self.invoice(self.m2, number='X' * 51),
                            'M2 not invoiced. Invoice number must be at most 50 characters.')

    def test_missing_date(self):
        self.assert_refused(self.invoice(self.m2, day=''),
                            'M2 not invoiced. Invoice date is required.')

    def test_future_date(self):
        self.assert_refused(self.invoice(self.m2, day=TODAY + timedelta(days=1)),
                            'M2 not invoiced. Invoice date cannot be in the future.')

    def test_bad_amounts(self):
        for amount in ('', 'abc', '0', '-5', '1,50,000', 'NaN', '10.001'):
            with self.subTest(amount=amount):
                response = self.invoice(self.m2, amount=amount)
                self.assert_untouched(self.m2)
                self.assertTrue(any(t.startswith('M2 not invoiced.')
                                    for t in _texts(response)), _texts(response))
        self.assertEqual(self.lines(), [])

    def test_m1_is_not_invoiced(self):
        self.assert_refused(self.invoice(self.m1),
                            'M1 is not invoiced — it is the token advance. No change was made.',
                            milestone=self.m1)

    def test_received_milestone(self):
        PaymentMilestone.objects.filter(pk=self.m2.pk).update(status='Received')
        self.assert_refused(self.invoice(self.m2),
                            'M2 is already Received; its invoice can no longer be changed.',
                            status='Received')

    def test_deleted_project(self):
        Project.objects.filter(pk=self.project.pk).update(is_deleted=True)
        self.assert_refused(self.invoice(self.m2),
                            'This site has been deleted. No invoice was recorded.')

    def test_opex_project(self):
        opex = self._project('Opex Site', project_type='OPEX')
        _m1, opex_m2, _m3 = self._milestones(opex, None, Decimal('100.00'), None)
        response = self.invoice(opex_m2)
        self.assert_untouched(opex_m2)
        self.assertIn('Invoices are recorded on Residential payment milestones only.',
                      _texts(response))
        self.assertEqual(self.lines(opex), [])

    def test_duplicate_number_on_another_project_ignoring_case_and_spaces(self):
        other = self._project('Other Site')
        _m1, other_m2, _m3 = self._milestones(other, None, None, None)
        PaymentMilestone.objects.filter(pk=other_m2.pk).update(
            status='Invoiced', invoice_number='INV/26-27/0412')
        self.assert_refused(self.invoice(self.m2, number=' inv/26-27/0412 '),
                            f'M2 not invoiced. Invoice number inv/26-27/0412 is already '
                            f'used on {other.project_id} M2.')

    def test_a_number_on_a_deleted_project_is_not_a_duplicate(self):
        other = self._project('Gone Site')
        _m1, other_m2, _m3 = self._milestones(other, None, None, None)
        PaymentMilestone.objects.filter(pk=other_m2.pk).update(invoice_number='INV-5')
        Project.objects.filter(pk=other.pk).update(is_deleted=True)
        self.invoice(self.m2, number='INV-5')
        self.m2.refresh_from_db()
        self.assertEqual(self.m2.status, 'Invoiced')


# ---------------------------------------------------------------------------
# Who: Finance only (ruling (a))
# ---------------------------------------------------------------------------

class WhoMayInvoiceTests(InvoiceBase):

    def test_pm_and_coordinator_are_refused_on_the_card(self):
        for who in (self.pm, self.coordinator, self.bd, self.ceo):
            with self.subTest(role=who.role):
                response = self.invoice(self.m2, who=who)
                self.assert_untouched(self.m2)
                self.assertIn('Only Finance can record an invoice. No change was made.',
                              _texts(response))

    def test_pm_and_coordinator_are_refused_on_the_task_while_pending(self):
        for who in (self.pm, self.coordinator):
            with self.subTest(role=who.role):
                response = self.complete(self.m2_task, who=who)
                self.assertEqual(self.status_of(self.m2_task), Task.NOT_STARTED)
                self.assert_untouched(self.m2)
                self.assertIn("Only Finance can complete 'Send Invoice - Material Supply' "
                              "while M2 is not invoiced — completing it records the "
                              "invoice. No change was made.", _texts(response))

    def test_coordinator_completes_the_task_once_invoiced(self):
        PaymentMilestone.objects.filter(pk=self.m2.pk).update(status='Invoiced')
        self.complete(self.m2_task, who=self.coordinator, modal=False)
        self.assertEqual(self.status_of(self.m2_task), Task.DONE)

    def test_the_permission_helper(self):
        from .permissions import user_can_invoice_milestone
        self.assertTrue(user_can_invoice_milestone(self.finance.user, self.project))
        for who in (self.pm, self.coordinator, self.bd, self.ceo):
            self.assertFalse(user_can_invoice_milestone(who.user, self.project), who.role)
        self.project.is_deleted = True
        self.assertFalse(user_can_invoice_milestone(self.finance.user, self.project))


# ---------------------------------------------------------------------------
# Blocked task, correction, receipt after invoice
# ---------------------------------------------------------------------------

class SyncAndLifecycleTests(InvoiceBase):

    def test_a_blocked_send_invoice_task_stays_blocked_and_finance_is_warned(self):
        Task.objects.filter(pk=self.m2_task.pk).update(status=Task.BLOCKED)
        response = self.invoice(self.m2)
        self.m2.refresh_from_db()
        self.assertEqual(self.m2.status, 'Invoiced', 'the invoice is saved')
        self.assertEqual(self.status_of(self.m2_task), Task.BLOCKED)
        texts = _texts(response)
        self.assertIn(RECORD_SIDE_WARNING, texts)
        self.assertIn("Cannot move task from 'Blocked' to 'Done'.", texts)

    def test_correction_while_invoiced_is_logged_old_to_new(self):
        self.invoice(self.m2, number='INV-1', amount='150000')
        ledger_before = StatusTransition.objects.count()
        lines_before = len(self.lines())
        response = self.invoice(self.m2, number='INV-2', amount='140000',
                                day=TODAY - timedelta(days=1))
        self.m2.refresh_from_db()
        self.assertEqual((self.m2.status, self.m2.invoice_number, self.m2.invoice_amount,
                          self.m2.invoice_date),
                         ('Invoiced', 'INV-2', Decimal('140000.00'),
                          TODAY - timedelta(days=1)))
        self.assertEqual(self.lines()[lines_before:], [
            (PLAIN, f"M2: invoice date {_day(TODAY)} → {_day(TODAY - timedelta(days=1))}; "
                    f"invoice number 'INV-1' → 'INV-2'", self.finance.pk),
            (INVOICE_MONEY, 'M2: invoice amount ₹1,50,000 → ₹1,40,000', self.finance.pk),
        ])
        self.assertEqual(StatusTransition.objects.count(), ledger_before,
                         'a correction is not a transition')
        self.assertIn('M2 invoice updated.', _texts(response))

    def test_resaving_the_same_invoice_writes_nothing(self):
        self.invoice(self.m2, number='INV-1', amount='150000')
        lines_before = len(self.lines())
        response = self.invoice(self.m2, number='INV-1', amount='150000.00')
        self.assertEqual(len(self.lines()), lines_before)
        self.assertIn('M2 invoice: no change.', _texts(response))

    def test_receipt_after_invoice_still_works_with_the_fin_m_l_lines(self):
        self.invoice(self.m2, number='INV-1', amount='150000')
        lines_before = len(self.lines())
        _client(self.finance).post(
            reverse('milestone_receive', args=[self.project.project_id, self.m2.pk]),
            {'amount_received': '150000'})
        self.m2.refresh_from_db()
        self.assertEqual(self.m2.status, 'Received')
        self.assertEqual(self.m2.invoice_number, 'INV-1', 'the invoice is kept')
        self.assertEqual(self.lines()[lines_before:], [
            (PLAIN, f'M2: status Invoiced → Received; received date blank → {_day(TODAY)}',
             self.finance.pk),
            ('milestone_amount_received', 'M2: amount received blank → ₹1,50,000',
             self.finance.pk),
        ])
        # B9a: the confirmation task is closed by the receipt.
        self.assertEqual(self.status_of(self.tasks['Pre Dispatch Payment Confirmation']),
                         Task.DONE)

    def test_receipt_straight_from_pending_is_still_allowed(self):
        response = _client(self.finance).post(
            reverse('milestone_receive', args=[self.project.project_id, self.m2.pk]),
            {'amount_received': '150000'})
        self.m2.refresh_from_db()
        self.assertEqual(self.m2.status, 'Received')
        self.assertFalse(any('invoice' in t.lower() for t in _texts(response)))


# ---------------------------------------------------------------------------
# The project page: card, modal pre-fill, query counts
# ---------------------------------------------------------------------------

class ProjectPageTests(InvoiceBase):

    def page(self, who):
        return _client(who).get(reverse('project_overview',
                                        args=[self.project.project_id]))

    def test_finance_gets_the_forms_and_the_prefill(self):
        response = self.page(self.finance)
        self.assertTrue(response.context['can_invoice_milestones'])
        self.assertEqual(response.context['invoice_task_prefill'], {
            'Send Invoice - Material Supply': {'milestone': 'M2', 'amount': '150000.00'},
            'Send Invoice - Final Payment':   {'milestone': 'M3', 'amount': ''},
        })
        by_name = {m.milestone_name: m for m in response.context['milestones']}
        self.assertFalse(by_name['M1'].invoiceable, 'M1 is not invoiced')
        self.assertTrue(by_name['M2'].invoiceable)
        self.assertEqual(by_name['M2'].invoice_amount_prefill, '150000.00')
        self.assertEqual(by_name['M3'].invoice_amount_prefill, '', 'blank when not set')
        content = response.content.decode()
        self.assertIn(reverse('milestone_invoice',
                              args=[self.project.project_id, self.m2.pk]), content)
        self.assertNotIn(reverse('milestone_invoice',
                                 args=[self.project.project_id, self.m1.pk]), content)
        self.assertIn('id="invoiceTaskModal"', content)

    def test_an_agreed_zero_is_not_prefilled(self):
        PaymentMilestone.objects.filter(pk=self.m2.pk).update(amount=Decimal('0'))
        response = self.page(self.finance)
        by_name = {m.milestone_name: m for m in response.context['milestones']}
        self.assertEqual(by_name['M2'].invoice_amount_prefill, '')

    def test_the_prefill_drops_a_milestone_once_invoiced(self):
        self.invoice(self.m2, number='INV-1', amount='149000')
        response = self.page(self.finance)
        self.assertNotIn('Send Invoice - Material Supply',
                         response.context['invoice_task_prefill'])
        by_name = {m.milestone_name: m for m in response.context['milestones']}
        self.assertEqual(by_name['M2'].invoice_amount_prefill, '149000.00',
                         'a correction starts from the invoiced amount')

    def test_other_roles_get_no_invoice_controls(self):
        for who in (self.pm, self.coordinator, self.bd, self.ceo):
            with self.subTest(role=who.role):
                response = self.page(who)
                self.assertFalse(response.context['can_invoice_milestones'])
                self.assertEqual(response.context['invoice_task_prefill'], {})
                content = response.content.decode()
                self.assertNotIn(reverse('milestone_invoice',
                                         args=[self.project.project_id, self.m2.pk]),
                                 content)
                self.assertNotIn('id="invoiceTaskModal"', content)

    def test_every_card_audience_sees_the_invoice(self):
        self.invoice(self.m2, number='INV/26-27/0412', amount='148500')
        for who in (self.finance, self.pm, self.coordinator, self.bd, self.ceo):
            with self.subTest(role=who.role):
                content = self.page(who).content.decode()
                self.assertIn('Invoice INV/26-27/0412', content)
                self.assertIn('Invoiced: ₹148500', content)

    def test_query_counts_per_role(self):
        """Pinned. The invoice controls are built from rows the page already loads, so
        these counts are the same on the code before FIN-M-B3."""
        expected = {'Finance': 42, 'PM': 45, 'Project Coordinator': 50, 'BD': 42, 'CEO': 41}
        counts = {}
        for who in (self.finance, self.pm, self.coordinator, self.bd, self.ceo):
            client = _client(who)
            url = reverse('project_overview', args=[self.project.project_id])
            client.get(url)   # warm per-session caches
            with CaptureQueriesContext(connection) as ctx:
                client.get(url)
            counts[who.role] = len(ctx.captured_queries)
        print('\nFIN-M-B3 project page queries:', counts)
        self.assertEqual(counts, expected)
