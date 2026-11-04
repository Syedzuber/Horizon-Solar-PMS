"""NTF-DCG Session 2 — deferred sends after commit, in the background, with a send log.

WHAT IS PINNED
  Commit     nothing is sent and no email row exists before commit; a rolled-back
             transaction leaves no row and sends nothing, through notify() and through
             notify_now() called inside an open transaction.
  The row    one email row per send, written 'pending' at hand-off and still pending
             when the transport is called, then finished in place: sent / failed /
             skipped, finished_at stamped, error text cut to PENDING_ERROR_LIMIT.
  Outcomes   provider exception or failure ⇒ failed, caller unaffected; master switch
             off ⇒ skipped; preference off ⇒ skipped; event paused ⇒ skipped with no
             pending row; no address ⇒ failed (ruling Q1: the same as a synchronous send).
  Hand-off   a pool that refuses work finishes the row as failed.
  Unchanged  no shipped event is deferred; a non-deferred event never reaches
             notify_deferred and its rows keep finished_at NULL; send_notification()
             with log_row_id finishes the row and creates none.
  Commands   list_stuck_notifications lists only pending rows older than --minutes;
             notify_deferred_smoke refuses anything but Postgres on localhost.

THREADS ARE NOT RUN HERE. solarpms.test_settings sets NOTIFY_DEFERRED_SYNC, so the job
runs inline at hand-off (in-memory SQLite is private to one connection; a pool thread
would see no tables). notify_deferred_smoke exercises the real pool on local Postgres.

INTERCEPTION, as tests_notify: `projects.notifications._zeptomail_post` is a recorder,
`requests.sessions.Session.request` raises, and the email switch is ON in the
throwaway database.

Run with:
    python manage.py test projects.tests_notify_deferred --settings=solarpms.test_settings
"""
import dataclasses
import io
from datetime import timedelta
from unittest import mock

import requests
from django.core.management import CommandError, call_command
from django.db import transaction
from django.test import TestCase, override_settings
from django.utils import timezone

from . import notify as N
from . import notify_admin as A
from . import notify_deferred, notify_registry
from .models import Notification, NotificationLog, SystemSettings
from .notifications import PENDING_ERROR_LIMIT, send_notification
from .notify_registry import NOTIFICATION_EVENTS, get_event
from .tests_approvals import _profile

EVENT = 'admin_test_message'
LABEL = 'deferred_label'


class Base(TestCase):

    def setUp(self):
        self.admin = _profile('nd_admin', 'Admin')
        self.alice = _profile('nd_alice', 'SCM')
        for profile in (self.admin, self.alice):
            profile.user.email = f'{profile.user.username}@example.com'
            profile.user.save(update_fields=['email'])
        switches = SystemSettings.get()
        switches.email_enabled = True
        switches.save()

        self.mail = []
        # The status of the email row at the moment the transport is called: proves the
        # row is written pending BEFORE the provider is reached.
        self.status_at_send = []

        def record(to_email, to_name, subject, text_body, html_body=None):
            self.status_at_send.append(list(
                NotificationLog.objects.filter(channel='email')
                .values_list('status', flat=True)))
            self.mail.append({'to': to_email, 'subject': subject, 'text': text_body,
                              'html': html_body})
            return True, ''

        self.transport = mock.patch('projects.notifications._zeptomail_post',
                                    side_effect=record)
        self.transport_mock = self.transport.start()
        self.addCleanup(self.transport.stop)
        http_patch = mock.patch.object(
            requests.sessions.Session, 'request',
            side_effect=AssertionError('an HTTP request was attempted'))
        self.http = http_patch.start()
        self.addCleanup(http_patch.stop)
        self.addCleanup(lambda: self.http.assert_not_called())

        # admin_test_message made deferred for this test only; the shipped entry is
        # restored by patch.dict on cleanup.
        registry = mock.patch.dict(NOTIFICATION_EVENTS, {
            EVENT: dataclasses.replace(get_event(EVENT), deferred=True)})
        registry.start()
        self.addCleanup(registry.stop)

    def send(self, recipients=None):
        """notify() as production runs it: the send happens after commit."""
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            N.notify(EVENT, recipients=recipients or [self.alice],
                     values={'sender_name': 'Asha'}, log_label=LABEL)
        return callbacks

    def email_rows(self):
        return list(NotificationLog.objects.filter(channel='email').order_by('pk'))


class CommitTests(Base):

    def test_nothing_is_sent_or_logged_before_commit(self):
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            N.notify(EVENT, recipients=[self.alice], values={'sender_name': 'Asha'},
                     log_label=LABEL)
        self.assertEqual(len(callbacks), 1)
        self.assertEqual((self.mail, self.email_rows()), ([], []))
        self.assertEqual(Notification.objects.count(), 0)
        # Running the commit callbacks now (and those they register) does the send.
        with self.captureOnCommitCallbacks(execute=True):
            for callback in callbacks:
                callback()
        self.assertEqual(len(self.mail), 1)
        self.assertEqual([r.status for r in self.email_rows()], ['sent'])

    def test_a_rolled_back_transaction_leaves_no_row_and_sends_nothing(self):
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            try:
                with transaction.atomic():
                    N.notify(EVENT, recipients=[self.alice], values={'sender_name': 'A'})
                    raise RuntimeError('the action failed')
            except RuntimeError:
                pass
        self.assertEqual(callbacks, [])
        self.assertEqual((self.mail, list(NotificationLog.objects.all())), ([], []))
        self.assertEqual(Notification.objects.count(), 0)

    def test_notify_now_inside_a_transaction_waits_for_its_commit(self):
        # The misuse notify_now()'s docstring warns about: the pending row is written in
        # the open transaction, and the hand-off waits for it, so a rollback removes both.
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            try:
                with transaction.atomic():
                    N.notify_now(EVENT, recipients=[self.alice],
                                 values={'sender_name': 'A'})
                    self.assertEqual([r.status for r in self.email_rows()], ['pending'])
                    raise RuntimeError('the action failed')
            except RuntimeError:
                pass
        self.assertEqual(callbacks, [])
        self.assertEqual((self.mail, self.email_rows()), ([], []))


class RowTests(Base):

    def test_the_row_is_pending_at_send_time_then_sent_in_place(self):
        before = timezone.now()
        self.send()
        self.assertEqual(self.status_at_send, [['pending']])
        [row] = self.email_rows()
        self.assertEqual((row.status, row.error_detail, row.event_code, row.template_name),
                         ('sent', '', EVENT, LABEL))
        self.assertEqual(row.recipient, self.alice)
        self.assertGreaterEqual(row.finished_at, row.created_at)
        self.assertGreaterEqual(row.created_at, before)
        self.assertEqual(row.message, self.mail[0]['text'])
        self.assertEqual(self.mail[0]['to'], 'nd_alice@example.com')

    def test_in_app_stays_synchronous_and_unchanged(self):
        self.send()
        in_app = NotificationLog.objects.get(channel='in_app')
        self.assertEqual((in_app.status, in_app.finished_at), ('sent', None))
        self.assertEqual(Notification.objects.filter(recipient=self.alice).count(), 1)

    def test_a_provider_exception_fails_the_row_and_never_reaches_the_caller(self):
        self.transport_mock.side_effect = RuntimeError('boom ' * 500)
        with self.assertLogs('projects.notify_deferred', 'ERROR'):
            self.send()   # does not raise
        [row] = self.email_rows()
        self.assertEqual(row.status, 'failed')
        self.assertTrue(row.error_detail.startswith('RuntimeError: boom boom'))
        self.assertEqual(len(row.error_detail), PENDING_ERROR_LIMIT)
        self.assertIsNotNone(row.finished_at)
        self.assertEqual(NotificationLog.objects.get(channel='in_app').status, 'sent')

    def test_a_provider_refusal_fails_the_row_with_its_detail(self):
        self.transport_mock.side_effect = lambda *a, **k: (False, 'HTTP 500: down')
        self.send()
        [row] = self.email_rows()
        self.assertEqual((row.status, row.error_detail), ('failed', 'HTTP 500: down'))

    def test_master_switch_off_is_skipped(self):
        switches = SystemSettings.get()
        switches.email_enabled = False
        switches.save()
        self.send()
        [row] = self.email_rows()
        self.assertEqual((row.status, row.error_detail), ('skipped', 'Master switch off'))
        self.assertIsNotNone(row.finished_at)
        self.assertEqual(self.mail, [])

    def test_preference_off_is_skipped(self):
        self.alice.email_notifications = False
        self.alice.save(update_fields=['email_notifications'])
        self.send()
        [row] = self.email_rows()
        self.assertEqual((row.status, row.error_detail), ('skipped', 'User preference off'))
        self.assertEqual(self.mail, [])

    def test_a_paused_event_is_skipped_with_no_pending_row(self):
        A.set_state(EVENT, 'email', False, self.admin, 'Pause it')
        with mock.patch.object(notify_deferred, 'defer') as defer:
            self.send()
        defer.assert_not_called()
        [row] = self.email_rows()
        self.assertEqual((row.status, row.error_detail, row.finished_at),
                         ('skipped', 'event paused', None))
        self.assertEqual(self.mail, [])

    def test_no_address_fails_as_a_synchronous_send_does(self):
        # Ruling Q1: 'failed' on both paths, with the reason in error_detail.
        self.alice.user.email = ''
        self.alice.user.save(update_fields=['email'])
        self.send()
        [row] = self.email_rows()
        self.assertEqual((row.status, row.error_detail),
                         ('failed', 'Recipient has no email address'))

    def test_one_row_per_recipient(self):
        bob = _profile('nd_bob', 'Finance')
        bob.user.email = 'nd_bob@example.com'
        bob.user.save(update_fields=['email'])
        self.send([self.alice, bob])
        self.assertEqual(sorted((r.recipient_id, r.status) for r in self.email_rows()),
                         sorted([(self.alice.pk, 'sent'), (bob.pk, 'sent')]))


class HandOffTests(Base):

    @override_settings(NOTIFY_DEFERRED_SYNC=False)
    def test_a_pool_that_refuses_work_fails_the_row(self):
        with mock.patch.object(notify_deferred._POOL, 'submit',
                               side_effect=RuntimeError('cannot schedule new futures')), \
                self.assertLogs('projects.notify_deferred', 'ERROR'):
            self.send()
        [row] = self.email_rows()
        self.assertEqual(row.status, 'failed')
        self.assertEqual(row.error_detail,
                         'not handed off: RuntimeError: cannot schedule new futures')
        self.assertEqual(self.mail, [])

    @override_settings(NOTIFY_DEFERRED_SYNC=False)
    def test_outside_test_mode_the_job_goes_to_the_pool_with_connection_close(self):
        with mock.patch.object(notify_deferred._POOL, 'submit') as submit:
            self.send()
        [row] = self.email_rows()
        self.assertEqual(row.status, 'pending')   # the (mocked) pool never ran it
        args = submit.call_args.args
        self.assertEqual((args[0], args[1], args[2], args[4]),
                         (notify_deferred._run, row.pk, self.alice.pk, True))
        self.assertEqual(args[3]['channels'], ['email'])


class ShippedRegistryTests(TestCase):
    """Outside Base, whose setUp makes admin_test_message deferred."""

    def test_no_shipped_event_is_deferred(self):
        # S1: the 38 events that existed before S2 all stay synchronous.
        self.assertEqual(len(NOTIFICATION_EVENTS), 38)
        self.assertEqual([c for c, e in NOTIFICATION_EVENTS.items() if e.deferred], [])

    def test_deferred_defaults_to_false(self):
        field = {f.name: f for f in dataclasses.fields(notify_registry.NotificationEvent)}
        self.assertIs(field['deferred'].default, False)


class UnchangedTests(Base):

    def test_a_non_deferred_event_never_reaches_notify_deferred(self):
        with mock.patch.dict(NOTIFICATION_EVENTS, {
                EVENT: dataclasses.replace(get_event(EVENT), deferred=False)}), \
                mock.patch.object(notify_deferred, 'defer') as defer:
            self.send()
        defer.assert_not_called()
        [row] = self.email_rows()
        self.assertEqual((row.status, row.finished_at), ('sent', None))
        self.assertEqual(self.status_at_send, [[]])   # no row existed until after the send

    def test_log_row_id_finishes_the_row_and_creates_none(self):
        row = NotificationLog.objects.create(recipient=self.alice, channel='email',
                                             status='pending', message='m')
        manager = NotificationLog.objects
        with mock.patch.object(manager, 'create', wraps=manager.create) as create:
            send_notification(self.alice, 'Hello', channels=['email'], subject='S',
                              log_row_id=row.pk)
        create.assert_not_called()
        row.refresh_from_db()
        self.assertEqual(row.status, 'sent')
        self.assertIsNotNone(row.finished_at)

    def test_a_finished_row_is_not_overwritten(self):
        row = NotificationLog.objects.create(recipient=self.alice, channel='email',
                                             status='sent', message='m')
        send_notification(self.alice, 'Hello', channels=['email'], log_row_id=row.pk)
        row.refresh_from_db()
        self.assertEqual((row.status, row.finished_at), ('sent', None))


class StuckCommandTests(Base):

    def _row(self, status, minutes_old):
        row = NotificationLog.objects.create(recipient=self.alice, channel='email',
                                             status=status, message='m', event_code=EVENT)
        # created_at is auto_now_add, so age it with an update.
        NotificationLog.objects.filter(pk=row.pk).update(
            created_at=timezone.now() - timedelta(minutes=minutes_old))
        return row

    def run_command(self, *args):
        out = io.StringIO()
        call_command('list_stuck_notifications', *args, stdout=out)
        return out.getvalue()

    def test_only_old_pending_rows_are_listed(self):
        old = self._row('pending', 30)
        recent = self._row('pending', 2)
        finished = self._row('sent', 30)
        out = self.run_command()
        self.assertIn('Pending for more than 10 min', out)
        self.assertIn(': 1\n', out)
        self.assertIn(f'#{old.pk} ', out)
        self.assertNotIn(f'#{recent.pk} ', out)
        self.assertNotIn(f'#{finished.pk} ', out)
        self.assertIn('nd_alice', out)
        self.assertIn(f'event={EVENT}', out)

    def test_minutes_moves_the_cutoff(self):
        recent = self._row('pending', 2)
        self.assertIn(f'#{recent.pk} ', self.run_command('--minutes', '1'))


class SmokeCommandTests(TestCase):

    def test_it_refuses_a_database_that_is_not_local_postgres(self):
        with self.assertRaisesRegex(CommandError, 'only against Postgres on localhost'):
            call_command('notify_deferred_smoke', '--user', 'nobody', stdout=io.StringIO())
