"""FIN-M-B4 — the "Invoice due" alert tells Finance an M2/M3 invoice has fallen due (R8).

Rulings pinned here (Zuber, 13 Nov 2026):
* R8: the alert fires when the task before the send-invoice task is Done — "PO for B & C
  Class Items" -> M2, "Customer Handover" -> M3 — while that milestone is Pending.
* One definition: the trigger table is utils.INVOICE_DUE_TRIGGERS, and the Finance work
  page's TRIGGER_TASKS is that same object.
* Scope: Residential only (R2), not soft-deleted (R3), not Draft or Cancelled (the
  page's own NOT_ACTIONABLE_STATUSES); never on the from_milestone path.
* Paused, option (A): the email starts paused and waits for Admin; in-app is never
  paused, so it goes out from the deploy.
* Send once, option (b): one alert per site and milestone, found by its log label
  (invoice_due_m2 / invoice_due_m3) in NotificationLog; a reopened trigger completed
  again sends nothing.
* Recipients: every active Finance user, minus the person who completed the task; no
  named recipients. No WhatsApp.
* Deferred email; the agreed amount in the page's rupee format, or "amount not set".
* An ordinary task's Done costs exactly what it did before (17 queries in this fixture).

Every test that pins new behaviour fails on the code before FIN-M-B4 (the event code is
unknown there, and nothing is sent). The "nothing sent" tests pass on both, by design.

Run with:
    python manage.py test projects.tests_invoice_due_alert --settings=solarpms.test_settings
"""
from datetime import date
from decimal import Decimal

from django.contrib.messages.storage.fallback import FallbackStorage
from django.db import connection
from django.test import RequestFactory
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from .models import (Notification, NotificationEventSetting, NotificationLog,
                     PaymentMilestone, Project, Task)
from .notify_registry import get_event
from .tests_notify_migration_a import BASE_URL, GoldenBase
from .tests_residential_baseline import _client_for, _profile

EVENT = 'invoice_due'
PO_BC = 'PO for B & C Class Items'
HANDOVER = 'Customer Handover'
ORDINARY = 'PO Placed MMS'
# What an ordinary task's Done costs through the task row in this fixture, measured on
# the code before FIN-M-B4 (also 17 for a trigger task then). Must not move.
ORDINARY_DONE_QUERIES = 17


class InvoiceDueBase(GoldenBase):
    """GoldenBase's project A (activated Residential, PM pm_a) with a SECOND active
    Finance user, so "every Finance user" is proven for more than one person, and the
    SCM trigger task given an assignee (the row refuses an unassigned task)."""

    def setUp(self):
        super().setUp()
        self.finance_2 = _profile('finance_two', 'Finance', email='finance_two@example.com',
                                  phone_number='9000099999')
        self.by_phone[self.finance_2.phone_number] = self.finance_2
        for name in (PO_BC, HANDOVER, ORDINARY):
            task = self._task(self.project_a, name)
            Task.objects.filter(pk=task.pk).update(
                due_date=date.today(), assigned_to=task.assigned_to or self.scm)
        self.m2 = self.project_a.milestones.get(milestone_name='M2')
        self.m3 = self.project_a.milestones.get(milestone_name='M3')
        PaymentMilestone.objects.filter(pk=self.m2.pk).update(amount=Decimal('135000.00'))
        # The project page's milestone card, the anchor the Finance work page links to.
        self.link = (reverse('project_overview', args=[self.project_a.project_id])
                     + '#payment-milestones')

    # -- acts --------------------------------------------------------------------

    def switch_email_on(self):
        """What Admin's Activate leaves behind: an active setting row."""
        NotificationEventSetting.objects.create(
            event_code=EVENT, channel='email', is_active=True, changed_by=self.admin,
            reason='Tested and switched on')

    def post_status(self, name, status=Task.DONE, actor=None, project=None, client=None):
        """`actor` (default the site's PM) moves the task from the project-overview row.
        Pass a logged-in `client` to keep the login's queries out of a count."""
        project = project or self.project_a
        task = self._task(project, name)
        client = client or _client_for(actor or self.pm_a)
        return self.act(lambda: client.post(
            reverse('task_status_update', args=[project.project_id, task.pk]),
            {'status': status}))

    def apply_directly(self, name, profile, *, project=None, from_milestone=False):
        """Call the chokepoint itself, for the cases the screens never reach (another
        project type, a deleted site, a Finance actor, from_milestone). Returns the
        outcome; the task must really reach Done for a "nothing sent" test to mean
        anything, so each caller asserts it."""
        from .views import _apply_task_status_change

        project = project or Project.objects.get(pk=self.project_a.pk)
        task = self._task(project, name)
        request = RequestFactory().post('/', {'status': Task.DONE})
        request.user = profile.user
        request.session = {}
        request._messages = FallbackStorage(request)
        outcome = self.act(lambda: _apply_task_status_change(
            task, Task.DONE, profile, request, project, from_milestone=from_milestone))
        task.refresh_from_db()
        self.assertEqual(task.status, Task.DONE)
        return outcome

    # -- what was sent -----------------------------------------------------------

    def alert_notes(self):
        return {note.recipient: note for note in self.notes
                if note.link == self.link}

    def alert_rows(self):
        return [row for row in self.logs if row.event_code == EVENT]

    def assert_nothing_sent(self):
        self.assertEqual(self.alert_rows(), [])
        self.assertEqual(self.alert_notes(), {})
        self.assertFalse(NotificationLog.objects.filter(event_code=EVENT).exists())

    def expected(self, milestone, trigger, actor, amount):
        pid = self.project_a.project_id
        who = self.name(actor)
        d = timezone.localdate()
        in_app = (f'{milestone} invoice is now due for project {pid} (Alpha Residence): '
                  f'"{trigger}" was completed by {who}.')
        paragraphs = [
            f'The {milestone} invoice for project {pid} (Alpha Residence) is now due.',
            f'"{trigger}" was marked Done by {who} on {d.day} {d:%b %Y}.',
            f'Agreed amount: {amount}',
            "Record the invoice on the project's payment milestone card.",
        ]
        return in_app, paragraphs

    def assert_alert(self, milestone, trigger, actor, amount, recipients, *, email):
        in_app, paragraphs = self.expected(milestone, trigger, actor, amount)
        notes = self.in_app()                       # fails if anyone is told twice
        alert = {p: n for p, n in notes.items() if n.link == self.link}
        self.assertEqual(set(alert), recipients)
        for person in recipients:
            self.assert_in_app(alert[person], [in_app], self.link)

        mails = self.emails()                       # fails if anyone is emailed twice
        if email:
            self.assertEqual(set(mails), {self.address(p) for p in recipients})
            for person in recipients:
                self.assert_email(mails[self.address(person)],
                                  f'Invoice due — Alpha Residence ({milestone})',
                                  paragraphs, self.link)
        else:
            self.assertEqual(mails, {})
        # Never WhatsApp: the event declares none and the call site sends none.
        self.assertEqual(self.whatsapps(), {})

        label = f'invoice_due_{milestone.lower()}'
        rows = self.alert_rows()
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(row.related_project_id, self.project_a.pk)
            self.assertEqual(row.template_name, label)
            self.assertIn(row.recipient, recipients)
        return rows


class RegistryTests(InvoiceDueBase):

    def test_event_is_registered_paused_email_only_deferred(self):
        event = get_event(EVENT)
        self.assertEqual(event.label, 'Invoice due')
        self.assertEqual(event.module, 'Payments')
        self.assertEqual(event.channels, ('email',))
        self.assertEqual(event.default_state, 'paused')
        self.assertEqual(event.default_state_for('email'), 'paused')
        self.assertTrue(event.deferred)
        self.assertIsNone(event.whatsapp)
        self.assertFalse(event.whatsapp_sent_separately)
        self.assertFalse(event.named_recipients)
        self.assertEqual(event.default_subject,
                         'Invoice due — {{ customer_name }} ({{ milestone }})')
        self.assertEqual(event.button_label, 'Open the milestone card')
        self.assertEqual(
            [v.name for v in event.variables],
            ['project_id', 'customer_name', 'milestone', 'trigger_task',
             'completed_by_name', 'completed_on', 'agreed_amount'])

    def test_page_and_alert_share_one_trigger_table(self):
        from . import utils, views, views_finance_work

        self.assertIs(views_finance_work.TRIGGER_TASKS, utils.INVOICE_DUE_TRIGGERS)
        self.assertEqual(utils.INVOICE_DUE_TRIGGERS, {'M2': PO_BC, 'M3': HANDOVER})
        self.assertEqual(views._INVOICE_DUE_TRIGGER_TO_MILESTONE,
                         {PO_BC: 'M2', HANDOVER: 'M3'})


class AlertSentTests(InvoiceDueBase):

    def test_po_bc_done_with_m2_pending_alerts_every_finance_user(self):
        self.switch_email_on()
        self.post_status(PO_BC)
        self.assert_alert('M2', PO_BC, self.pm_a, '₹1,35,000',
                          {self.finance, self.finance_2}, email=True)

    def test_customer_handover_done_with_m3_pending_alerts_every_finance_user(self):
        self.switch_email_on()
        self.post_status(HANDOVER)
        # M3 has no agreed amount: the page's wording, never ₹0.
        self.assert_alert('M3', HANDOVER, self.pm_a, 'amount not set',
                          {self.finance, self.finance_2}, email=True)

    def test_paused_event_sends_in_app_but_no_email(self):
        """Ruling (A): no Admin switch yet. Every Finance user gets the in-app alert;
        nobody is emailed, and each email leg is logged as skipped."""
        self.post_status(PO_BC)
        rows = self.assert_alert('M2', PO_BC, self.pm_a, '₹1,35,000',
                                 {self.finance, self.finance_2}, email=False)
        email_rows = [r for r in rows if r.channel == 'email']
        self.assertEqual({r.recipient for r in email_rows}, {self.finance, self.finance_2})
        for row in email_rows:
            self.assertEqual((row.status, row.error_detail), ('skipped', 'event paused'))

    def test_finance_actor_is_not_told_about_their_own_act(self):
        self.switch_email_on()
        self.apply_directly(PO_BC, self.finance)
        in_app, _ = self.expected('M2', PO_BC, self.finance, '₹1,35,000')
        notes = self.alert_notes()
        self.assertEqual(set(notes), {self.finance_2})
        self.assert_in_app(notes[self.finance_2], [in_app], self.link)
        self.assertEqual(set(self.emails()), {self.address(self.finance_2)})

    def test_inactive_finance_user_is_not_told(self):
        self.finance_2.is_active = False
        self.finance_2.save(update_fields=['is_active'])
        self.post_status(PO_BC)
        self.assertEqual(set(self.alert_notes()), {self.finance})


class SendOnceTests(InvoiceDueBase):

    def reopen(self, name):
        """Put a Done trigger back to In Progress. The screens reopen through Blocked,
        which needs an Issue; the send-once check reads only the log, so the route back
        does not matter to it."""
        Task.objects.filter(pk=self._task(self.project_a, name).pk).update(
            status=Task.IN_PROGRESS, completed_at=None)

    def test_completing_the_trigger_again_sends_nothing(self):
        self.post_status(PO_BC)
        self.assertEqual(set(self.alert_notes()), {self.finance, self.finance_2})
        self.reopen(PO_BC)
        self.post_status(PO_BC)
        self.assertEqual(self._task(self.project_a, PO_BC).status, Task.DONE)
        self.assertEqual(self.alert_rows(), [])
        self.assertEqual(self.alert_notes(), {})

    def test_m2_alert_does_not_block_m3_or_another_site(self):
        self.post_status(PO_BC)
        self.post_status(HANDOVER)
        self.assertEqual({r.template_name for r in self.alert_rows()}, {'invoice_due_m3'})
        # Project B: its own M2 alert still goes out.
        trig_b = self._task(self.project_b, PO_BC)
        Task.objects.filter(pk=trig_b.pk).update(due_date=date.today(), assigned_to=self.scm)
        self.post_status(PO_BC, actor=self.pm_b, project=self.project_b)
        rows = self.alert_rows()
        self.assertTrue(rows)
        self.assertEqual({(r.related_project_id, r.template_name) for r in rows},
                         {(self.project_b.pk, 'invoice_due_m2')})


class NothingSentTests(InvoiceDueBase):

    def test_milestone_invoiced(self):
        PaymentMilestone.objects.filter(pk=self.m2.pk).update(status='Invoiced')
        self.post_status(PO_BC)
        self.assert_nothing_sent()

    def test_milestone_received(self):
        PaymentMilestone.objects.filter(pk=self.m3.pk).update(status='Received')
        self.post_status(HANDOVER)
        self.assert_nothing_sent()

    def test_task_not_a_trigger(self):
        self.post_status(ORDINARY)
        self.assertEqual(self._task(self.project_a, ORDINARY).status, Task.DONE)
        self.assert_nothing_sent()

    def test_trigger_moved_to_in_progress(self):
        self.post_status(PO_BC, status=Task.IN_PROGRESS)
        self.assertEqual(self._task(self.project_a, PO_BC).status, Task.IN_PROGRESS)
        self.assert_nothing_sent()

    def _as_type(self, project_type):
        Project.objects.filter(pk=self.project_a.pk).update(project_type=project_type)
        # OPEX and CAPEX finish by submit -> approve; an approved task passes that rung.
        Task.objects.filter(pk=self._task(self.project_a, PO_BC).pk).update(
            approved_at=timezone.now())
        self.apply_directly(PO_BC, self.pm_a)
        self.assert_nothing_sent()

    def test_opex_project(self):
        self._as_type('OPEX')

    def test_capex_project(self):
        self._as_type('CAPEX')

    def test_deleted_project(self):
        Project.objects.filter(pk=self.project_a.pk).update(is_deleted=True,
                                                            deleted_at=timezone.now())
        self.apply_directly(PO_BC, self.pm_a)
        self.assert_nothing_sent()

    def test_draft_project(self):
        Project.objects.filter(pk=self.project_a.pk).update(status='Draft')
        self.apply_directly(PO_BC, self.pm_a)
        self.assert_nothing_sent()

    def test_cancelled_project(self):
        Project.objects.filter(pk=self.project_a.pk).update(status='Cancelled')
        self.apply_directly(HANDOVER, self.pm_a)
        self.assert_nothing_sent()

    def test_from_milestone_path(self):
        self.apply_directly(PO_BC, self.finance, from_milestone=True)
        self.assert_nothing_sent()


class QueryCostTests(InvoiceDueBase):

    def count(self, name):
        """Queries of the POST alone, its after-commit sends included: the client is
        logged in and the URL built before counting, as when the figure was measured."""
        client = _client_for(self.pm_a)
        url = reverse('task_status_update',
                      args=[self.project_a.project_id, self._task(self.project_a, name).pk])
        with CaptureQueriesContext(connection) as ctx:
            with self.captureOnCommitCallbacks(execute=True):
                client.post(url, {'status': Task.DONE})
        self.assertEqual(self._task(self.project_a, name).status, Task.DONE)
        return len(ctx.captured_queries)

    def test_ordinary_task_done_costs_what_it_did(self):
        self.assertEqual(self.count(ORDINARY), ORDINARY_DONE_QUERIES)

    def test_trigger_with_milestone_not_pending_costs_one_query(self):
        """The milestone read, nothing else: no log check, no recipient query."""
        PaymentMilestone.objects.filter(pk=self.m2.pk).update(status='Received')
        self.assertEqual(self.count(PO_BC), ORDINARY_DONE_QUERIES + 1)

    def test_trigger_that_alerts_costs_three_queries_plus_the_sends(self):
        """Before commit: the milestone, the sent-before check, the Finance users (3).
        After commit, notify()'s own cost with email paused: the event's setting once,
        then per Finance user the master-switch read, the in-app Notification, its log
        row and the skipped email row (1 + 4 x 2 recipients = 9). Pinned so a later
        change to the block shows up here."""
        self.assertEqual(self.count(PO_BC), ORDINARY_DONE_QUERIES + 3 + 9)
