"""FIN-M-R9 — the payment_milestone_reached alert reads "Payment received" and reaches
the right people.

Rulings pinned here:
* R9: renamed "Payment received", without "initiate collection"; recipients BD, the
  project's managers (assigned PM and active Coordinators) and CEO.
* Interim Finance rule (Zuber, 12 Nov 2026): Finance users are recipients only when the
  person who recorded the payment is NOT Finance. FIN-M-R10 drops Finance entirely.
* recorded_by_name names who recorded it, on both firing points: the task row (task
  Done) and Finance's receipt (FIN-M-B9a, through the chokepoint).
* Unchanged: the event code, one alert per firing, and the WhatsApp leg's template
  (payment_notification) and its 1+2 params; only its send-log text changes.
* Named recipients stay off for this event.

The email text tested here is the CODE DEFAULT (the test database holds no template
version). Production holds an Admin-managed active version, which Zuber replaces in the
Admin panel after deploy; that step is outside the code.

Tests that change behaviour fail on the code before FIN-M-R9: the wording tests, the
second-Finance-user test and the named-person test.

Run with:
    python manage.py test projects.tests_payment_received_alert --settings=solarpms.test_settings
"""
from datetime import date
from decimal import Decimal

from django.urls import reverse

from .models import NotificationLog, PaymentMilestone, Task
from .notify_registry import get_event
from .tests_notify_migration_a import GoldenBase
from .tests_residential_baseline import _client_for, _profile

EVENT = 'payment_milestone_reached'
OLD_PHRASE = 'initiate collection'
TASK_NAME = 'Advance Payment Confirmation'


class PaymentReceivedBase(GoldenBase):
    """GoldenBase's project A, with a SECOND active Finance user who records nothing, so
    "Finance left out" is proven for every Finance user, not just the actor (whom the
    actor rule already dropped before R9)."""

    def setUp(self):
        super().setUp()
        self.finance_2 = _profile('finance_two', 'Finance', email='finance_two@example.com',
                                  phone_number='9000099999')
        self.by_phone[self.finance_2.phone_number] = self.finance_2
        self.task = self._task(self.project_a, TASK_NAME)
        self.assertTrue(self.task.is_payment_milestone)
        Task.objects.filter(pk=self.task.pk).update(due_date=date.today())

    # -- the two firing points ---------------------------------------------------

    def complete_task_as(self, profile):
        """`profile` marks the confirmation task Done from the task row."""
        return self.act(lambda: _client_for(profile).post(
            reverse('task_status_update', args=[self.project_a.project_id, self.task.pk]),
            {'status': Task.DONE}))

    def record_receipt_as_finance(self):
        """Finance records M1's receipt; FIN-M-B9a moves the task through the chokepoint."""
        milestone = self.project_a.milestones.get(milestone_name='M1')
        PaymentMilestone.objects.filter(pk=milestone.pk).update(
            status='Invoiced', amount=Decimal('100000.00'))
        return self.act(lambda: _client_for(self.finance).post(
            reverse('milestone_receive', args=[self.project_a.project_id, milestone.pk]),
            {'amount_received': '100000'}))

    # -- expectations ------------------------------------------------------------

    def expected_texts(self, actor):
        pid = self.project_a.project_id
        who = self.name(actor)
        in_app = (f'Payment received for project {pid} (Alpha Residence): '
                  f'"{TASK_NAME}", recorded by {who}.')
        paragraphs = [f'Payment received for project {pid} (Alpha Residence).',
                      f'"{TASK_NAME}" was recorded by {who}.']
        return in_app, paragraphs

    def assert_alert(self, actor, expected):
        """Who heard, on every channel, and what they were told — each exactly once."""
        in_app, paragraphs = self.expected_texts(actor)
        link = f'/projects/{self.project_a.project_id}/'

        notes = self.in_app()            # fails if anyone is told twice
        self.assertEqual(set(notes), expected)
        mails = self.emails()            # fails if anyone is emailed twice
        self.assertEqual(set(mails), {self.address(p) for p in expected})
        for person in expected:
            self.assert_in_app(notes[person], [in_app], link)
            self.assertNotIn(OLD_PHRASE, notes[person].message)
            payload = mails[self.address(person)]
            self.assert_email(payload, 'Payment received — Alpha Residence', paragraphs, link)
            self.assertNotIn(OLD_PHRASE, payload['textbody'])
            self.assertNotIn(OLD_PHRASE, payload['htmlbody'])

        # WhatsApp: the same people, template and params UNCHANGED (header customer,
        # body task + customer — the locked 1+2 shape).
        params = ['Alpha Residence', TASK_NAME, 'Alpha Residence']
        self.assertEqual(self.whatsapps(),
                         {person: ('payment_notification', params) for person in expected})
        # The WhatsApp send-log text is the new wording too.
        wa_rows = [row for row in self.logs if row.channel == 'whatsapp']
        self.assertEqual({row.recipient for row in wa_rows}, expected)
        for row in wa_rows:
            self.assertEqual(row.message, in_app)
        self.assert_logs(self.project_a, expected, 'payment_notification')


class RegistryWordingTests(PaymentReceivedBase):

    def test_label_and_texts_say_payment_received_not_initiate_collection(self):
        event = get_event(EVENT)
        self.assertEqual(event.label, 'Payment received')
        self.assertEqual(event.default_subject, 'Payment received — {{ customer_name }}')
        for text in (event.in_app_text, event.default_subject, event.default_body,
                     event.recipients_rule):
            self.assertNotIn(OLD_PHRASE, text.lower())
        self.assertIn('recorded_by_name', event.variable_names)

    def test_recipients_rule_documents_the_interim_finance_rule(self):
        rule = get_event(EVENT).recipients_rule
        self.assertIn('Finance users are included only when the payment was recorded by '
                      'someone who is not Finance', rule)
        self.assertIn('FIN-M-R10', rule)

    def test_code_channel_and_named_recipients_unchanged(self):
        event = get_event(EVENT)
        self.assertEqual(event.code, EVENT)
        self.assertEqual(event.channels, ('email',))
        self.assertTrue(event.whatsapp_sent_separately)
        self.assertFalse(event.named_recipients)


class FinanceRecordsTests(PaymentReceivedBase):
    """Finance recorded the payment: Finance already knows, so no Finance user is told."""

    def test_task_path_reaches_managers_bd_ceo_and_no_finance_user(self):
        self.complete_task_as(self.finance)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, Task.DONE)
        # finance_2 was told before R9; the actor `finance` was already dropped.
        self.assert_alert(self.finance, {self.pm_a, self.coord_a, self.bd, self.ceo})

    def test_receipt_path_reaches_the_same_people_once(self):
        self.record_receipt_as_finance()
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, Task.DONE)
        self.assert_alert(self.finance, {self.pm_a, self.coord_a, self.bd, self.ceo})

    def test_the_alert_fires_once_per_firing_point(self):
        """One in-app row per recipient and one alert's worth of email rows: the receipt
        path does not fire a second alert from the task side (FIN-M-B9a, unchanged)."""
        self.record_receipt_as_finance()
        email_rows = [row for row in self.logs
                      if row.channel == 'email' and row.event_code == EVENT]
        self.assertEqual(len(email_rows), 4)
        self.assertEqual(len(self.notes), 4)


class SomeoneElseRecordsTests(PaymentReceivedBase):
    """A Coordinator (or PM) recorded the payment by completing the task: Finance did not
    record it, so every active Finance user is told (interim rule until FIN-M-R10)."""

    def test_coordinator_completing_the_task_tells_every_finance_user(self):
        self.complete_task_as(self.coord_a)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, Task.DONE)
        # The Coordinator is the actor and dropped; the alert names them.
        self.assert_alert(self.coord_a, {self.pm_a, self.bd, self.ceo,
                                         self.finance, self.finance_2})

    def test_alert_names_the_coordinator_as_recorder(self):
        self.complete_task_as(self.coord_a)
        note = next(iter(self.in_app().values()))
        self.assertIn(f'recorded by {self.name(self.coord_a)}.', note.message)
        self.assertFalse(NotificationLog.objects.filter(
            event_code=EVENT, recipient=self.coord_a).exists())
