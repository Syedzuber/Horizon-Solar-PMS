"""FIN-M-B3b — every path that records milestone money reads it the same way, refuses bad
input with a message and saves nothing, and refuses deleted and non-Residential sites.

WHY THIS FILE EXISTS
--------------------
Before FIN-M-B3b the four paths each read the amount their own way:
* project_overview's Finance update_milestone: an unreadable amount received was dropped
  and Finance told "M2 updated."; a negative was saved as a receipt; "NaN" and an
  over-long number were server errors; a missing description field blanked the stored one.
* milestone_receive: Decimal() let negatives through and 500'd on NaN; it accepted deleted
  and OPEX sites and returned to the retired Finance dashboard.
* the confirmation task's payment-capture modal (the chokepoint): a bad amount was
  dropped and the task completed anyway, leaving a Received milestone with no amount.
* set_milestone_amounts: "abc" read as "All three milestone amounts are required.", a
  third decimal place was rounded, OPEX sites were accepted, and a database error's raw
  text was returned to the browser.

Rulings (Zuber, 13 Nov 2026):
* parse_decimal_input, places=2, min 0 (zero allowed), max from the column; a refusal
  refuses the whole save with "<Mx> not updated. <reason>" and writes no FIN-M-L line.
* Description: missing field = no change; present and blank = clear. Over 100 characters
  refused; a variance reason over 255 refused.
* Task path: a bad typed amount refuses the Done whatever the milestone's state; blank
  (Skip) still completes the task.
* Receipt straight from Pending on M2/M3 is allowed, with a warning on all three receipt
  paths; the send-invoice task is left open. The second sentence naming the task appears
  only when that task is really open (most local sites have none, or have it Done).
* Finance branch: Residential only; the amount is read only while not Received. On a
  Received milestone a typed amount that differs from the stored one warns "M2: amount
  not changed — already Received." beside the success line.
* milestone_receive: deleted site -> the user's dashboard; otherwise back to the project.
* set_milestone_amounts: Residential only; "M2 not updated. Amount: ..." in the JSON
  error; blank still refused; a database error is logged and answered generically.

Behaviour tests fail on the code before FIN-M-B3b.

Run with:
    python manage.py test projects.tests_milestone_input_checks --settings=solarpms.test_settings
"""
import json
from datetime import date
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.contrib.messages import get_messages
from django.test import Client, TestCase
from django.urls import reverse

from .models import ActivityLog, PaymentMilestone, Project, ProjectPhase, Task

BAD_AMOUNTS = {
    'abc':        'Amount received: "abc" is not a number.',
    '1,50,000':   'Amount received: "1,50,000" is not a number.',
    '-5000':      'Amount received: "-5000" cannot be negative.',
    'NaN':        'Amount received: "NaN" is not a number.',
    '1e15':       'Amount received: "1e15" is larger than the maximum of 9999999999.99.',
    '150000.505': 'Amount received: "150000.505" has more than 2 decimal places.',
}
MILESTONE_CODES = ('milestone_updated', 'milestone_amount_received', 'milestone_amounts_set')
NO_INVOICE = 'M2 marked Received, but no invoice was recorded for it.'
TASK_OPEN = " Its task 'Send Invoice - Material Supply' is still open."
TODAY = date.today()
TODAY_TEXT = f'{TODAY.day} {TODAY:%b %Y}'


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


class InputChecksBase(TestCase):
    """One Active Residential project with M1 (₹50,000), M2 (₹1,50,000) and M3 (no
    amount), the three confirmation tasks and the two M2/M3 "Send Invoice" tasks, all
    held by Finance. Every test patches notify, so no alert leaves the test."""

    def setUp(self):
        patcher = mock.patch('projects.views.notify')
        self.notify_mock = patcher.start()
        self.addCleanup(patcher.stop)
        self.pm = _profile('ic_pm', 'PM')
        self.finance = _profile('ic_fin', 'Finance')
        self.bd = _profile('ic_bd', 'BD')
        self.project = self._project('Input Checks Site')
        phase = ProjectPhase.objects.create(project=self.project, phase_name='Finance',
                                            phase_order=1)
        self.tasks = {}
        for order, name in enumerate([
                'Advance Payment Confirmation', 'Send Invoice - Material Supply',
                'Pre Dispatch Payment Confirmation', 'Send Invoice - Final Payment',
                '100% Payment Confirmation'], start=1):
            self.tasks[name] = Task.objects.create(
                phase=phase, task_name=name, task_order=order,
                assigned_role=Task.FINANCE, assigned_to=self.finance,
                is_payment_milestone=name.endswith('Confirmation'))
        self.m1, self.m2, self.m3 = [
            PaymentMilestone.objects.create(
                project=self.project, milestone_name=f'M{n}',
                milestone_description=f'Stage {n}', amount=amount, created_by=self.pm)
            for n, amount in enumerate(
                [Decimal('50000.00'), Decimal('150000.00'), None], start=1)]

    def _project(self, name, project_type='Residential'):
        return Project.objects.create(
            customer_name=name, status='Active', project_type=project_type,
            customer_phone='9876543210', site_address='1 Sun Road', city='Lucknow',
            state='Uttar Pradesh', dc_capacity_kw=Decimal('5.00'), assigned_pm=self.pm,
            cascade_scheduling=False)

    # -- the four paths ------------------------------------------------------

    def finance_edit(self, milestone, amount_received='', description=None, project=None):
        """The project page's Finance edit. `description` None leaves the field out."""
        data = {'action': 'update_milestone', 'milestone_pk': milestone.pk,
                'amount_received': amount_received}
        if description is not None:
            data['milestone_description'] = description
        return _client(self.finance).post(
            reverse('project_overview', args=[(project or self.project).project_id]), data)

    def receive(self, milestone, amount='150000', **extra):
        """The "Mark as Received" modal."""
        return _client(self.finance).post(
            reverse('milestone_receive', args=[milestone.project.project_id, milestone.pk]),
            {'amount_received': amount, **extra})

    def complete(self, task_name, **extra):
        """Finance marks a task Done from the task row (the payment-capture modal)."""
        task = self.tasks[task_name]
        return _client(self.finance).post(
            reverse('task_status_update', args=[self.project.project_id, task.pk]),
            {'status': Task.DONE, **extra})

    def set_amounts(self, body, project=None, who=None):
        return _client(who or self.bd).post(
            reverse('set_milestone_amounts', args=[(project or self.project).project_id]),
            data=json.dumps(body), content_type='application/json')

    # -- assertions ----------------------------------------------------------

    def lines(self):
        return list(ActivityLog.objects.filter(
            project=self.project, action_code__in=MILESTONE_CODES)
            .order_by('timestamp', 'pk').values_list('action_code', 'action'))

    def snapshot(self, milestone):
        milestone.refresh_from_db()
        return (milestone.status, milestone.amount, milestone.amount_received,
                milestone.received_date, milestone.variance_reason,
                milestone.milestone_description)

    def status_of(self, task_name):
        return Task.objects.get(pk=self.tasks[task_name].pk).status


# ---------------------------------------------------------------------------
# Bad amounts: refused on every path, nothing saved, no change-log line
# ---------------------------------------------------------------------------

class BadAmountTests(InputChecksBase):

    def test_finance_edit_refuses_every_bad_amount(self):
        before = self.snapshot(self.m2)
        for raw, reason in BAD_AMOUNTS.items():
            with self.subTest(raw=raw):
                response = self.finance_edit(self.m2, raw, description='Stage 2')
                self.assertRedirects(response, reverse('project_overview',
                                                       args=[self.project.project_id]),
                                     fetch_redirect_response=False)
                texts = _texts(response)
                self.assertTrue(texts[0].startswith(f'M2 not updated. {reason}'), texts)
                self.assertNotIn('M2 updated.', texts)
                self.assertEqual(self.snapshot(self.m2), before)
        self.assertEqual(self.lines(), [])
        self.assertEqual(self.status_of('Pre Dispatch Payment Confirmation'), Task.NOT_STARTED)

    def test_milestone_receive_refuses_every_bad_amount_and_returns_to_the_project(self):
        before = self.snapshot(self.m2)
        for raw, reason in {**BAD_AMOUNTS, '': 'Amount received is required.'}.items():
            with self.subTest(raw=raw):
                response = self.receive(self.m2, raw)
                self.assertRedirects(response, reverse('project_overview',
                                                       args=[self.project.project_id]),
                                     fetch_redirect_response=False)
                self.assertTrue(_texts(response)[0].startswith(f'M2 not updated. {reason}'),
                                _texts(response))
                self.assertEqual(self.snapshot(self.m2), before)
        self.assertEqual(self.lines(), [])

    def test_task_done_is_refused_on_every_bad_amount(self):
        before = self.snapshot(self.m2)
        for raw, reason in BAD_AMOUNTS.items():
            with self.subTest(raw=raw):
                response = self.complete('Pre Dispatch Payment Confirmation',
                                         amount_received=raw)
                texts = _texts(response)
                self.assertTrue(texts[0].startswith(f'M2 not updated. {reason}'), texts)
                self.assertTrue(texts[0].endswith('The task was not marked Done.'), texts)
                self.assertEqual(self.status_of('Pre Dispatch Payment Confirmation'),
                                 Task.NOT_STARTED)
                self.assertEqual(self.snapshot(self.m2), before)
        self.assertEqual(self.lines(), [])
        self.notify_mock.assert_not_called()

    def test_task_done_is_refused_on_a_bad_amount_even_when_the_milestone_is_received(self):
        """Ruling: whatever the milestone's state — today the amount would be ignored."""
        PaymentMilestone.objects.filter(pk=self.m2.pk).update(
            status='Received', amount_received=Decimal('150000.00'))
        response = self.complete('Pre Dispatch Payment Confirmation', amount_received='abc')
        self.assertIn('M2 not updated. Amount received: "abc" is not a number. '
                      'The task was not marked Done.', _texts(response))
        self.assertEqual(self.status_of('Pre Dispatch Payment Confirmation'), Task.NOT_STARTED)

    def test_set_milestone_amounts_refuses_every_bad_amount(self):
        for raw, reason in BAD_AMOUNTS.items():
            with self.subTest(raw=raw):
                response = self.set_amounts({'m1_amount': None, 'm2_amount': raw,
                                             'm3_amount': None})
                payload = response.json()
                self.assertFalse(payload['success'])
                self.assertTrue(payload['error'].startswith(
                    'M2 not updated. ' + reason.replace('Amount received', 'Amount')),
                    payload['error'])
                self.m2.refresh_from_db()
                self.assertEqual(self.m2.amount, Decimal('150000.00'))
        self.assertEqual(self.lines(), [])

    def test_set_milestone_amounts_still_refuses_blank(self):
        payload = self.set_amounts({'m2_amount': ''}).json()
        self.assertEqual(payload, {'success': False,
                                   'error': 'M2 not updated. Amount is required.'})

    def test_one_bad_amount_refuses_the_whole_set_milestone_amounts_save(self):
        payload = self.set_amounts({'m1_amount': '60000', 'm2_amount': '-1'}).json()
        self.assertFalse(payload['success'])
        self.m1.refresh_from_db()
        self.assertEqual(self.m1.amount, Decimal('50000.00'))


# ---------------------------------------------------------------------------
# Valid input: saved exactly as before, FIN-M-L lines unchanged
# ---------------------------------------------------------------------------

class ValidInputTests(InputChecksBase):

    def test_finance_edit_records_a_receipt_with_the_same_lines(self):
        PaymentMilestone.objects.filter(pk=self.m2.pk).update(status='Invoiced')
        response = self.finance_edit(self.m2, '150000', description='Stage 2')
        # FIN-M-RT (ruling): a pencil save that records a receipt says so.
        self.assertIn('M2 marked as Received.', _texts(response))
        self.m2.refresh_from_db()
        self.assertEqual((self.m2.status, self.m2.amount_received, self.m2.received_date),
                         ('Received', Decimal('150000.00'), TODAY))
        self.assertEqual(self.lines(), [
            ('milestone_updated',
             f'M2: status Invoiced → Received; received date blank → {TODAY_TEXT}'),
            ('milestone_amount_received', 'M2: amount received blank → ₹1,50,000'),
        ])
        self.assertEqual(self.status_of('Pre Dispatch Payment Confirmation'), Task.DONE)

    def test_milestone_receive_records_a_receipt_with_the_same_lines(self):
        PaymentMilestone.objects.filter(pk=self.m2.pk).update(status='Invoiced')
        response = self.receive(self.m2, '148000.50', variance_reason='Short by TDS')
        self.assertIn('M2 marked as Received.', _texts(response))
        self.m2.refresh_from_db()
        self.assertEqual(self.m2.amount_received, Decimal('148000.50'))
        self.assertEqual(self.lines(), [
            ('milestone_updated',
             f'M2: status Invoiced → Received; received date blank → {TODAY_TEXT}'),
            ('milestone_amount_received',
             "M2: amount received blank → ₹1,48,000.50; variance reason blank → "
             "'Short by TDS'"),
        ])

    def test_zero_is_still_a_valid_receipt(self):
        self.receive(self.m1, '0')
        self.m1.refresh_from_db()
        self.assertEqual((self.m1.status, self.m1.amount_received),
                         ('Received', Decimal('0')))

    def test_task_done_with_an_amount_records_it(self):
        self.complete('Advance Payment Confirmation', amount_received='50000.00')
        self.m1.refresh_from_db()
        self.assertEqual((self.m1.status, self.m1.amount_received),
                         ('Received', Decimal('50000.00')))
        self.assertEqual(self.status_of('Advance Payment Confirmation'), Task.DONE)

    def test_task_done_with_a_blank_amount_still_completes_skip(self):
        self.complete('Advance Payment Confirmation', amount_received='')
        self.m1.refresh_from_db()
        self.assertEqual(self.m1.status, 'Received')
        self.assertIsNone(self.m1.amount_received)
        self.assertEqual(self.status_of('Advance Payment Confirmation'), Task.DONE)

    def test_set_milestone_amounts_saves_valid_amounts_and_skips_nulls(self):
        payload = self.set_amounts({'m1_amount': None, 'm2_amount': 140000,
                                    'm3_amount': '10000.25'}).json()
        self.assertEqual(payload, {'success': True})
        self.m1.refresh_from_db(); self.m2.refresh_from_db(); self.m3.refresh_from_db()
        self.assertEqual((self.m1.amount, self.m2.amount, self.m3.amount),
                         (Decimal('50000.00'), Decimal('140000.00'), Decimal('10000.25')))

    def test_finance_edit_on_a_received_milestone_ignores_the_amount_box(self):
        """Ruling: the amount is read only while not Received. The box comes back
        pre-filled on every description edit, so it must not be able to refuse one."""
        PaymentMilestone.objects.filter(pk=self.m2.pk).update(
            status='Received', amount_received=Decimal('150000.00'))
        response = self.finance_edit(self.m2, 'abc', description='Paid by cheque')
        self.assertIn('M2 updated.', _texts(response))
        self.m2.refresh_from_db()
        self.assertEqual(self.m2.milestone_description, 'Paid by cheque')
        self.assertEqual(self.m2.amount_received, Decimal('150000.00'))

    def test_a_different_amount_on_a_received_milestone_warns_it_was_not_changed(self):
        """Ruling: a non-blank amount that differs from the stored one is warned about
        alongside the success line; the pre-filled stored value is not."""
        PaymentMilestone.objects.filter(pk=self.m2.pk).update(
            status='Received', amount_received=Decimal('150000.00'))
        warning = 'M2: amount not changed — already Received.'
        texts = _texts(self.finance_edit(self.m2, '160000', description='Stage 2'))
        self.assertIn('M2 updated.', texts)
        self.assertIn(warning, texts)
        self.m2.refresh_from_db()
        self.assertEqual(self.m2.amount_received, Decimal('150000.00'))
        for same in ('150000.00', '150000', ''):
            with self.subTest(same=same):
                texts = _texts(self.finance_edit(self.m2, same, description='Stage 2'))
                self.assertNotIn(warning, texts)


# ---------------------------------------------------------------------------
# Deleted and non-Residential sites
# ---------------------------------------------------------------------------

class SiteRuleTests(InputChecksBase):

    def opex_m2(self):
        opex = self._project('Opex Site', project_type='OPEX')
        return opex, PaymentMilestone.objects.create(project=opex, milestone_name='M2',
                                                     created_by=self.pm)

    def test_milestone_receive_refuses_a_deleted_site_and_goes_to_the_dashboard(self):
        Project.objects.filter(pk=self.project.pk).update(is_deleted=True)
        before = self.snapshot(self.m2)
        response = self.receive(self.m2, '150000')
        self.assertEqual(response.status_code, 302)
        self.assertNotIn(self.project.project_id, response['Location'])
        self.assertIn('This site has been deleted. No receipt was recorded.',
                      _texts(response))
        self.assertEqual(self.snapshot(self.m2), before)

    def test_milestone_receive_refuses_an_opex_site(self):
        opex, m2 = self.opex_m2()
        response = self.receive(m2, '150000')
        self.assertIn('Payment receipts are recorded on Residential payment milestones '
                      'only. No change was made.', _texts(response))
        m2.refresh_from_db()
        self.assertEqual((m2.status, m2.amount_received), ('Pending', None))

    def test_finance_edit_refuses_an_opex_site(self):
        opex, m2 = self.opex_m2()
        response = self.finance_edit(m2, '150000', description='', project=opex)
        self.assertIn('Payment receipts are recorded on Residential payment milestones '
                      'only. No change was made.', _texts(response))
        m2.refresh_from_db()
        self.assertEqual((m2.status, m2.amount_received), ('Pending', None))

    def test_set_milestone_amounts_refuses_an_opex_site_and_creates_no_row(self):
        opex = self._project('Opex Site', project_type='OPEX')
        payload = self.set_amounts({'m2_amount': '150000'}, project=opex).json()
        self.assertEqual(payload, {'success': False,
                                   'error': 'Milestone amounts are set on Residential '
                                            'sites only. No change was made.'})
        self.assertFalse(PaymentMilestone.objects.filter(project=opex).exists())

    def test_set_milestone_amounts_still_404s_a_deleted_site(self):
        Project.objects.filter(pk=self.project.pk).update(is_deleted=True)
        response = self.set_amounts({'m2_amount': '150000'})
        self.assertEqual(response.status_code, 404)


# ---------------------------------------------------------------------------
# Description and variance reason
# ---------------------------------------------------------------------------

class TextFieldTests(InputChecksBase):

    def test_finance_edit_without_the_description_field_keeps_it(self):
        self.finance_edit(self.m2, '')
        self.m2.refresh_from_db()
        self.assertEqual(self.m2.milestone_description, 'Stage 2')
        self.assertEqual(self.lines(), [])

    def test_finance_edit_with_a_blank_description_clears_it(self):
        self.finance_edit(self.m2, '', description='  ')
        self.m2.refresh_from_db()
        self.assertEqual(self.m2.milestone_description, '')

    def test_finance_edit_refuses_a_description_over_100_characters(self):
        response = self.finance_edit(self.m2, '150000', description='x' * 101)
        self.assertIn('M2 not updated. Description must be at most 100 characters.',
                      _texts(response))
        self.m2.refresh_from_db()
        self.assertEqual((self.m2.status, self.m2.milestone_description),
                         ('Pending', 'Stage 2'))

    def test_pm_edit_refuses_a_description_over_100_characters(self):
        response = _client(self.pm).post(
            reverse('project_overview', args=[self.project.project_id]),
            {'action': 'update_milestone', 'milestone_pk': self.m2.pk,
             'milestone_description': 'x' * 101, 'amount': '150000', 'due_date': ''})
        self.assertIn('M2 not updated. Description must be at most 100 characters.',
                      _texts(response))
        self.m2.refresh_from_db()
        self.assertEqual(self.m2.milestone_description, 'Stage 2')

    def test_a_description_of_exactly_100_characters_saves(self):
        self.finance_edit(self.m2, '', description='x' * 100)
        self.m2.refresh_from_db()
        self.assertEqual(self.m2.milestone_description, 'x' * 100)

    def test_milestone_receive_refuses_a_variance_reason_over_255(self):
        response = self.receive(self.m2, '150000', variance_reason='y' * 256)
        self.assertIn('M2 not updated. Variance reason must be at most 255 characters.',
                      _texts(response))
        self.m2.refresh_from_db()
        self.assertEqual(self.m2.status, 'Pending')

    def test_task_done_refuses_a_note_over_255(self):
        response = self.complete('Pre Dispatch Payment Confirmation',
                                 amount_received='150000', variance_reason='y' * 256)
        self.assertIn('M2 not updated. Variance reason must be at most 255 characters. '
                      'The task was not marked Done.', _texts(response))
        self.assertEqual(self.status_of('Pre Dispatch Payment Confirmation'), Task.NOT_STARTED)
        self.m2.refresh_from_db()
        self.assertEqual(self.m2.status, 'Pending')


# ---------------------------------------------------------------------------
# Receipt straight from Pending on M2/M3: allowed, with a warning
# ---------------------------------------------------------------------------

class NoInvoiceWarningTests(InputChecksBase):

    def test_warning_on_milestone_receive(self):
        response = self.receive(self.m2, '150000')
        self.assertIn(NO_INVOICE + TASK_OPEN, _texts(response))
        self.m2.refresh_from_db()
        self.assertEqual(self.m2.status, 'Received')
        # Ruling: the send-invoice task is left open.
        self.assertEqual(self.status_of('Send Invoice - Material Supply'), Task.NOT_STARTED)

    def test_warning_on_the_finance_edit(self):
        response = self.finance_edit(self.m2, '150000', description='Stage 2')
        self.assertIn(NO_INVOICE + TASK_OPEN, _texts(response))
        self.assertEqual(self.status_of('Send Invoice - Material Supply'), Task.NOT_STARTED)

    def test_warning_on_the_task_path(self):
        response = self.complete('Pre Dispatch Payment Confirmation',
                                 amount_received='150000')
        self.assertIn(NO_INVOICE + TASK_OPEN, _texts(response))
        self.assertEqual(self.status_of('Send Invoice - Material Supply'), Task.NOT_STARTED)

    def test_warning_on_m3_names_its_own_task(self):
        response = self.receive(self.m3, '1000')
        self.assertIn('M3 marked Received, but no invoice was recorded for it. '
                      "Its task 'Send Invoice - Final Payment' is still open.",
                      _texts(response))

    def test_no_task_sentence_when_the_send_invoice_task_is_done(self):
        Task.objects.filter(pk=self.tasks['Send Invoice - Material Supply'].pk).update(
            status=Task.DONE)
        texts = _texts(self.receive(self.m2, '150000'))
        self.assertIn(NO_INVOICE, texts)
        self.assertFalse(any('still open' in t for t in texts), texts)

    def test_no_task_sentence_when_the_send_invoice_task_is_not_applicable(self):
        Task.objects.filter(pk=self.tasks['Send Invoice - Material Supply'].pk).update(
            is_not_applicable=True)
        texts = _texts(self.receive(self.m2, '150000'))
        self.assertIn(NO_INVOICE, texts)
        self.assertFalse(any('still open' in t for t in texts), texts)

    def test_no_warning_on_m1(self):
        texts = _texts(self.receive(self.m1, '50000'))
        self.assertFalse(any('no invoice' in t for t in texts), texts)

    def test_no_warning_when_the_milestone_was_invoiced(self):
        PaymentMilestone.objects.filter(pk=self.m2.pk).update(status='Invoiced')
        texts = _texts(self.receive(self.m2, '150000'))
        self.assertFalse(any('no invoice' in t for t in texts), texts)

    def test_no_warning_on_a_description_only_finance_edit(self):
        texts = _texts(self.finance_edit(self.m2, '', description='Stage 2'))
        self.assertFalse(any('no invoice' in t for t in texts), texts)


# ---------------------------------------------------------------------------
# set_milestone_amounts: no raw database text in the response
# ---------------------------------------------------------------------------

class SetAmountsErrorTests(InputChecksBase):

    def test_a_database_error_is_logged_and_answered_generically(self):
        with mock.patch.object(PaymentMilestone.objects, 'update_or_create',
                               side_effect=Exception('relation "secret_table" broke')), \
                self.assertLogs('projects.views', level='ERROR') as logs:
            response = self.set_amounts({'m2_amount': '140000'})
        self.assertEqual(response.json(), {
            'success': False, 'error': 'The amounts could not be saved — tell Admin.'})
        self.assertNotIn('secret_table', response.content.decode())
        self.assertTrue(any('set_milestone_amounts failed' in line for line in logs.output))
