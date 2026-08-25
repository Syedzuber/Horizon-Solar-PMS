"""
Safety options on `send_eod_digest`: --out, --to and --i-am-sending-to-real-people.

These cover the three guarantees that let the command be run from a local machine
against the production database:

  1. --out renders the CEO aggregate to a file and issues ZERO write queries — it must
     survive a READ-ONLY database connection, so it may not touch SystemSettings.get()
     (a get_or_create) or write a NotificationLog row.
  2. --to replaces the aggregate recipient set outright: no role='CEO' lookup, no
     Admin/HR merge, and no individual digests.
  3. A plain invocation refuses and exits 1 instead of sending.

The metric queries themselves are covered elsewhere; nothing here asserts a number.
"""

import os
import tempfile
from datetime import date
from io import StringIO
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.management import call_command
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from projects.models import SystemSettings, NotificationLog

#: A statement that changes data. SAVEPOINT/RELEASE (the TestCase transaction wrapper)
#: and BEGIN/SET are not writes to application tables and are deliberately not listed.
WRITE_KEYWORDS = ('INSERT', 'UPDATE', 'DELETE', 'CREATE', 'ALTER', 'DROP', 'TRUNCATE')

REPORT_DATE = date(2026, 8, 24)


def _writes(captured):
    return [q['sql'] for q in captured
            if q['sql'].lstrip().upper().startswith(WRITE_KEYWORDS)]


def _profile(username, role, email=''):
    """A post_save signal creates the UserProfile, so fetch and mutate it."""
    user = User.objects.create_user(username=username, password='pw12345', email=email)
    profile = user.profile
    profile.role = role
    profile.is_active = True
    profile.save()
    return profile


@override_settings(ADMIN_DIGEST_EMAIL='admin@example.com',
                   HR_DIGEST_EMAIL='hr@example.com')
class EODDigestSafetyOptionsTests(TestCase):

    def setUp(self):
        self.ceo = _profile('ceo', 'CEO', 'ceo@example.com')
        self.pm = _profile('pm', 'PM', 'pm@example.com')
        self.tmpdir = tempfile.mkdtemp()

    def _call(self, *args):
        out, err = StringIO(), StringIO()
        code = None
        try:
            call_command('send_eod_digest', '--date', REPORT_DATE.isoformat(),
                         *args, stdout=out, stderr=err)
        except SystemExit as exc:
            code = exc.code
        return code, out.getvalue(), err.getvalue()

    # --- 1. --out ------------------------------------------------------------------

    def test_out_writes_the_file_and_issues_no_write_queries(self):
        path = os.path.join(self.tmpdir, 'ceo.html')
        # No SystemSettings row exists: SystemSettings.get() would CREATE one here, which
        # is exactly the write this path must avoid.
        self.assertEqual(SystemSettings.objects.count(), 0)

        with CaptureQueriesContext(connection) as captured:
            code, out, err = self._call('--out', path)

        self.assertIsNone(code)
        self.assertEqual(_writes(captured), [], 'the --out path must issue zero writes')
        self.assertEqual(SystemSettings.objects.count(), 0,
                         'a missing SystemSettings row must be read, never created')
        self.assertEqual(NotificationLog.objects.count(), 0)

        self.assertTrue(os.path.exists(path))
        with open(path, encoding='utf-8') as fh:
            html = fh.read()
        self.assertIn('EOD Summary', html)
        # The CEO variant, not the plain Admin/HR body (Admin/HR never set
        # show_ceo_sections, so these sections are absent from their email).
        self.assertIn('Executive snapshot', html)
        self.assertIn('Per-user status', html)
        self.assertIn('[out] email master switch: OFF', out)
        self.assertIn('(no SystemSettings row', out)
        self.assertIn('nothing sent, nothing logged, no rows written', out)

    def test_out_reports_the_master_switch_without_get_or_create(self):
        SystemSettings.objects.create(pk=1, email_enabled=True)
        path = os.path.join(self.tmpdir, 'ceo2.html')
        with CaptureQueriesContext(connection) as captured:
            code, out, err = self._call('--out', path)
        self.assertIsNone(code)
        self.assertEqual(_writes(captured), [])
        self.assertIn('[out] email master switch: ON', out)

    # --- 2. --to -------------------------------------------------------------------

    def test_to_overrides_recipients_and_skips_individual_digests(self):
        with patch('projects.notifications.send_aggregate_email') as agg, \
             patch('projects.notifications.send_notification') as individual:
            code, out, err = self._call('--to', 'me@example.com', '--to', 'me2@example.com')

        self.assertIsNone(code)
        individual.assert_not_called()
        sent_to = sorted(call.kwargs['to_email'] for call in agg.call_args_list)
        self.assertEqual(sent_to, ['me2@example.com', 'me@example.com'],
                         'only the --to addresses may be used')
        self.assertIn('me@example.com', out)
        self.assertIn('me2@example.com', out)
        # Neither the fixed Admin/HR addresses nor the role='CEO' address may appear.
        for address in ('admin@example.com', 'hr@example.com', 'ceo@example.com'):
            self.assertNotIn(address, out)

    def test_to_with_dry_run_sends_nothing(self):
        with patch('projects.notifications.send_aggregate_email') as agg:
            code, out, err = self._call('--to', 'me@example.com', '--dry-run')
        self.assertIsNone(code)
        agg.assert_not_called()
        self.assertIn('would send to --to: me@example.com', out)

    # --- 3. the interlock ------------------------------------------------------------

    def test_plain_invocation_refuses_and_exits_1(self):
        with patch('projects.notifications.send_aggregate_email') as agg, \
             patch('projects.notifications.send_notification') as individual:
            code, out, err = self._call()

        self.assertEqual(code, 1)
        agg.assert_not_called()
        individual.assert_not_called()
        self.assertEqual(NotificationLog.objects.count(), 0,
                         'the refusal must happen before the individual digests run')
        self.assertIn('REFUSING TO SEND', err)
        # The refusal names the recipients it resolved.
        self.assertIn('admin@example.com', err)
        self.assertIn('hr@example.com', err)
        self.assertIn('ceo@example.com', err)

    def test_interlock_flag_allows_the_send(self):
        with patch('projects.notifications.send_aggregate_email') as agg:
            code, out, err = self._call('--i-am-sending-to-real-people')
        self.assertIsNone(code)
        self.assertTrue(agg.called, 'the aggregate must send once the interlock is given')

    def test_dry_run_still_needs_no_interlock(self):
        with patch('projects.notifications.send_aggregate_email') as agg, \
             patch('projects.notifications.send_notification') as individual:
            code, out, err = self._call('--dry-run')
        self.assertIsNone(code)
        agg.assert_not_called()
        individual.assert_not_called()

    # --- the database banner ---------------------------------------------------------

    def test_every_run_prints_the_database_host_and_never_the_password(self):
        code, out, err = self._call('--dry-run')
        self.assertIn('[db] host=', out)
        self.assertNotIn('PASSWORD', out.upper())
        password = connection.settings_dict.get('PASSWORD') or ''
        if password:
            self.assertNotIn(password, out)
