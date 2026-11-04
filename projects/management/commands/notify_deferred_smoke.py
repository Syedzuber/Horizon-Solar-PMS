"""Fire one deferred notification on local Postgres and watch its row (NTF-DCG S2, P5).

    python manage.py notify_deferred_smoke --user <username> [--delay N] [--rollback | --fail]

WHAT IT SHOWS. It sends admin_test_message to one local user, as a DEFERRED event,
inside transaction.atomic(), through the real notify() → notify_deferred → background
pool → send_notification() path with real pool threads. It prints timestamps for: the
transaction block returning (at once), the email row seen 'pending', and the row
finishing N seconds later.

  --delay N    the fake transport sleeps N seconds before answering.
  --rollback   the block raises after notify(): proves no log row and no in-app row
               exist and the transport was never called.
  --fail       the fake transport raises a long error: the row ends 'failed' with the
               text cut to PENDING_ERROR_LIMIT, and the command's transaction committed.

IT NEVER REACHES A PROVIDER, WHATEVER THE SWITCHES SAY. Everything below is patched IN
THIS PROCESS ONLY, and is checked, and the network probed, before anything fires:
  * projects.notifications._zeptomail_post is a fake that sleeps and answers;
  * requests' Session.request, socket.create_connection, socket.socket.connect and
    socket.getaddrinfo raise. They are the backstops; Postgres is unaffected, because
    psycopg2 connects through libpq, not Python sockets;
  * SystemSettings.get() returns an in-memory copy with email on, so the send reaches
    the fake rather than being skipped. The stored row is never saved, and its switches
    are printed again at the end;
  * the registry entry for admin_test_message is replaced, in memory, by a copy with
    deferred=True. No shipped event is deferred (S1), and no 39th event is added.

It refuses to run unless the database is Postgres on localhost, and refuses when
NOTIFY_DEFERRED_SYNC is set (no threads would be exercised).

WRITES (except --rollback): one in-app Notification and two NotificationLog rows (in_app,
email) for the user, labelled template_name='notify_deferred_smoke', so they never
count as an Admin test send ('notify_test'). They are left for viewing in Django admin.
"""
import copy
import dataclasses
import socket
import time
from contextlib import ExitStack
from unittest import mock

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.utils import timezone

from projects import notifications, notify_registry
from projects.models import Notification, NotificationLog, SystemSettings, UserProfile
from projects.notify import channel_is_active, notify
from projects.notify_registry import get_event

EVENT = 'admin_test_message'
LABEL = 'notify_deferred_smoke'
LOCAL_HOSTS = ('localhost', '127.0.0.1', '::1')
FAIL_TEXT_LENGTH = 3000   # longer than PENDING_ERROR_LIMIT, to show the cut
POLL_SECONDS = 0.5
GRACE_SECONDS = 15        # how long past --delay to wait for the row to finish


class NetworkBlocked(Exception):
    """Raised by every network backstop this command installs."""


class _Rollback(Exception):
    """Raised inside the block by --rollback."""


def _blocked(*args, **kwargs):
    raise NetworkBlocked('notify_deferred_smoke: network access is blocked in this command')


def _clock(moment):
    """`moment` as HH:MM:SS.mmm local time; 'None' for a NULL column."""
    if moment is None:
        return 'None'
    return timezone.localtime(moment).strftime('%H:%M:%S.%f')[:-3]


def _stamp():
    return _clock(timezone.now())


def _last_pk(model):
    return model.objects.order_by('-pk').values_list('pk', flat=True).first() or 0


class Command(BaseCommand):
    help = ('Send one DEFERRED test notification to a local user through real pool '
            'threads with a fake transport, and print its log row moving from pending.')

    def add_arguments(self, parser):
        parser.add_argument('--user', required=True, help='Username of the local recipient.')
        parser.add_argument('--delay', type=float, default=0.0,
                            help='Seconds the fake transport sleeps before answering.')
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument('--rollback', action='store_true',
                          help='Raise inside the transaction after notify().')
        mode.add_argument('--fail', action='store_true',
                          help='The fake transport raises a long error.')

    def handle(self, *args, **options):
        """Run the smoke. Local-only developer command; refuses any non-local database."""
        w = self.stdout.write
        db = settings.DATABASES['default']
        w(f"Database: {db.get('HOST') or '(local)'} / {db.get('NAME')}")
        # Local Postgres only: never production, and never SQLite, whose in-memory or
        # file database the pool threads could not share in the way the smoke needs.
        if 'postgresql' not in db.get('ENGINE', '') or db.get('HOST') not in LOCAL_HOSTS:
            raise CommandError('Refused: this command runs only against Postgres on localhost.')
        if getattr(settings, 'NOTIFY_DEFERRED_SYNC', False):
            raise CommandError('Refused: NOTIFY_DEFERRED_SYNC is set, so no pool thread '
                               'would run; this command exists to exercise them.')
        delay = max(options['delay'], 0.0)

        profile = (UserProfile.objects.select_related('user')
                   .filter(user__username=options['user']).first())
        if profile is None:
            raise CommandError(f"No UserProfile for username {options['user']!r}.")
        stored = SystemSettings.objects.filter(pk=1).values(
            'email_enabled', 'whatsapp_enabled').first()
        w(f'Real switches (stored): {stored}')
        w(f"Recipient: {profile.user.username} <{profile.user.email or 'NO EMAIL'}> "
          f'email_notifications={profile.email_notifications} '
          f'active={profile.is_active and profile.user.is_active}')
        w(f"Mode: {'rollback' if options['rollback'] else 'fail' if options['fail'] else 'send'}"
          f', fake transport delay {delay:g} s')

        calls = []

        def fake_send(to_email, to_name, subject, text_body, html_body=None):
            calls.append(_stamp())
            time.sleep(delay)
            if options['fail']:
                raise RuntimeError('smoke: fake transport failure ' + 'x' * FAIL_TEXT_LENGTH)
            return True, ''

        fake_switches = copy.copy(SystemSettings.get())
        fake_switches.email_enabled = True
        fake_switches.whatsapp_enabled = False
        deferred_event = dataclasses.replace(get_event(EVENT), deferred=True)

        with ExitStack() as patches:
            patches.enter_context(mock.patch.object(notifications, '_zeptomail_post', fake_send))
            patches.enter_context(mock.patch.object(requests.sessions.Session, 'request', _blocked))
            patches.enter_context(mock.patch.object(socket, 'create_connection', _blocked))
            patches.enter_context(mock.patch.object(socket.socket, 'connect', _blocked))
            patches.enter_context(mock.patch.object(socket, 'getaddrinfo', _blocked))
            patches.enter_context(mock.patch.object(SystemSettings, 'get',
                                                    return_value=fake_switches))
            patches.enter_context(mock.patch.dict(notify_registry.NOTIFICATION_EVENTS,
                                                  {EVENT: deferred_event}))
            self._check_fakes(fake_send, fake_switches)
            if not channel_is_active(get_event(EVENT), 'email'):
                w('NOTE: admin_test_message email is PAUSED here; expect a skipped row and '
                  'no pending row.')
            self._fire(profile, delay, options, calls)

        after = SystemSettings.objects.filter(pk=1).values(
            'email_enabled', 'whatsapp_enabled').first()
        w(f'Real switches after the run (stored): {after}')

    def _check_fakes(self, fake_send, fake_switches):
        """Refuse to fire unless every fake is in place and every backstop really blocks."""
        w = self.stdout.write
        checks = {
            'transport is the fake': notifications._zeptomail_post is fake_send,
            'requests Session.request blocked': requests.sessions.Session.request is _blocked,
            'socket.create_connection blocked': socket.create_connection is _blocked,
            'socket.socket.connect blocked': socket.socket.connect is _blocked,
            'socket.getaddrinfo blocked': socket.getaddrinfo is _blocked,
            'switches are the in-memory copy, email on':
                SystemSettings.get() is fake_switches and fake_switches.email_enabled,
            'event is deferred (in memory)': get_event(EVENT).deferred,
        }

        def probe_socket():
            with socket.socket() as sock:
                sock.connect(('127.0.0.1', 9))

        probes = {
            'probe socket.create_connection': lambda: socket.create_connection(
                ('api.zeptomail.in', 443), timeout=1),
            'probe socket.connect': probe_socket,
            'probe requests.post': lambda: requests.post(
                'https://api.zeptomail.in/v1.1/email', timeout=1),
        }
        for name, probe in probes.items():
            try:
                probe()
                checks[name] = False
            except NetworkBlocked:
                checks[name] = True
            except Exception:
                checks[name] = False
        for name, ok in checks.items():
            w(f"  [{'ok' if ok else 'FAIL'}] {name}")
        if not all(checks.values()):
            raise CommandError('Refused: a fake or backstop is not in place; nothing was sent.')

    def _fire(self, profile, delay, options, calls):
        w = self.stdout.write
        log_mark, notif_mark = _last_pk(NotificationLog), _last_pk(Notification)

        w(f'{_stamp()} entering transaction.atomic()')
        started = time.monotonic()
        txid = None
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    # Gives the transaction an id without writing anything, so its
                    # outcome can be asked of Postgres afterwards (txid_status).
                    cursor.execute('SELECT txid_current()')
                    txid = cursor.fetchone()[0]
                notify(EVENT, recipients=[profile], values={'sender_name': LABEL},
                       log_label=LABEL)
                if options['rollback']:
                    raise _Rollback()
        except _Rollback:
            w(f'{_stamp()} block raised _Rollback (as asked)')
        returned = time.monotonic()
        w(f'{_stamp()} transaction block returned after '
          f'{(returned - started) * 1000:.0f} ms (includes the commit callbacks)')
        with connection.cursor() as cursor:
            cursor.execute('SELECT txid_status(%s)', [txid])
            w(f'  transaction {txid}: {cursor.fetchone()[0]}')

        rows = NotificationLog.objects.filter(pk__gt=log_mark, template_name=LABEL)
        if options['rollback']:
            # Wait as long as a wrongly started job would need, then look again.
            time.sleep(delay + 1)
            w(f'{_stamp()} after {delay + 1:g} s:')
            w(f'  NotificationLog rows labelled {LABEL!r} since start: {rows.count()}')
            w(f'  in-app Notification rows for the user since start: '
              f'{Notification.objects.filter(pk__gt=notif_mark, recipient=profile).count()}')
            w(f'  fake transport calls: {len(calls)}')
            return

        for row in rows.order_by('pk'):
            w(f'  #{row.pk} {row.channel:7} {row.status:8} created {_clock(row.created_at)}')
        email = rows.filter(channel='email').order_by('pk').first()
        if email is None:
            w('  no email row was written')
            return
        last = email.status
        w(f'{_stamp()} email row #{email.pk} is {last!r}')
        deadline = returned + delay + GRACE_SECONDS
        while last == 'pending' and time.monotonic() < deadline:
            time.sleep(POLL_SECONDS)
            now_status = NotificationLog.objects.filter(pk=email.pk).values_list(
                'status', flat=True).first()
            if now_status != last:
                last = now_status
                w(f'{_stamp()} email row #{email.pk} is now {last!r} '
                  f'({time.monotonic() - returned:.1f} s after the block returned)')
        email.refresh_from_db()
        if email.status == 'pending':
            w(f'  still pending after {delay + GRACE_SECONDS:g} s')
        w(f'  final: status={email.status} created={_clock(email.created_at)} '
          f'finished={_clock(email.finished_at)}')
        w(f'  error_detail ({len(email.error_detail)} chars): {email.error_detail[:70]!r}'
          + ('...' if len(email.error_detail) > 70 else ''))
        w(f'  in-app Notification rows for the user since start: '
          f'{Notification.objects.filter(pk__gt=notif_mark, recipient=profile).count()}')
        w(f'  fake transport calls: {len(calls)} at {calls}')
