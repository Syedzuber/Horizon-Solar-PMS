"""
SCHED-R — the record of each daily scheduler run, and the Site Dashboard's "Dates as of".

WHAT THIS PINS
    Write     only a finished `schedule_sites --all --apply` stamps
              SystemSettings.schedule_last_run_at / schedule_last_run_sites (R1, R2)
    No write  a dry run, a --site run (with or without --apply), a run that raised, the
              after-commit trigger (schedule_site_safely) and the Admin working-day switch
    Update    the stamp touches only its two columns, never the notification switches
    Display   "Dates as of 9 Nov, 06:31" in IST; amber "— the daily refresh has not run
              today" when older than the daily 06:30 IST slot; amber "Dates not yet refreshed by
              the daily run" when never written (R3); "today's 06:30" means the most
              recent 06:30 at least 30 minutes past (amended 10 Nov), so 06:45 with
              yesterday's stamp is grey and 07:05 is amber; every allowed role sees
              it (R4)
    Queries   the dashboard costs exactly one more query than without the line

Today is frozen at 9 Nov 2026 by SchedFixture (through DashFixture). The clock tests pass
`now` to _dates_as_of directly, or patch django.utils.timezone.now for a rendered page.

Run with:
    python manage.py test projects.tests_sched_r_run_record --settings=solarpms.test_settings
"""
from datetime import datetime
from datetime import timezone as dt_timezone
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from .models import SystemSettings
from .scheduling import candidate_sites, schedule_site_safely
from .tests_sched2_scheduler import SchedFixture, _client_for, _profile
from .tests_site_dashboard_view import URL, ViewFixture, _client
from .views_site_dashboard import _dates_as_of

NOT_TODAY = ' — the daily refresh has not run today'
NEVER = 'Dates not yet refreshed by the daily run'


def _ist(day, hour, minute=0):
    """An aware datetime on `day` Nov 2026 at hour:minute IST (TIME_ZONE)."""
    return timezone.make_aware(datetime(2026, 11, day, hour, minute))


def _stamp():
    row = SystemSettings.objects.filter(pk=1).values(
        'schedule_last_run_at', 'schedule_last_run_sites').first()
    return (row['schedule_last_run_at'], row['schedule_last_run_sites']) if row else (None, None)


# ---------------------------------------------------------------------------
# 1. Who writes the stamp (R2)
# ---------------------------------------------------------------------------

class RunRecordTests(SchedFixture):

    def setUp(self):
        # One eligible, scheduled site and one with no capacity (checked, not eligible):
        # both count in schedule_last_run_sites, which is len(sites), not "scheduled".
        self.site = self._site('40.00', civil_due=timezone.localdate().replace(day=10))
        self.blank = self._site(None)

    def _run(self, *args, **options):
        call_command('schedule_sites', *args, stdout=StringIO(), **options)

    def test_a_full_applied_run_writes_the_stamp_and_site_count(self):
        before = timezone.now()
        self._run(all=True, apply=True)
        at, sites = _stamp()
        self.assertIsNotNone(at)
        self.assertTrue(before <= at <= timezone.now())
        self.assertEqual(sites, candidate_sites().count())
        self.assertEqual(sites, 2)

    def test_a_later_full_run_moves_the_stamp_on(self):
        self._run(all=True, apply=True)
        first, _ = _stamp()
        self._run(all=True, apply=True)
        second, _ = _stamp()
        self.assertGreater(second, first)

    def test_a_dry_run_writes_nothing(self):
        self._run(all=True)
        self.assertEqual(_stamp(), (None, None))

    def test_a_single_site_run_writes_nothing_even_with_apply(self):
        self._run(site=self.site.project_id, apply=True)
        self._run(site=self.site.project_id)
        self.assertEqual(_stamp(), (None, None))

    def test_a_run_that_raises_writes_nothing(self):
        with patch('projects.management.commands.schedule_sites.schedule_site',
                   side_effect=RuntimeError('boom')):
            with self.assertRaises(RuntimeError):
                self._run(all=True, apply=True)
        self.assertEqual(_stamp(), (None, None))

    def test_a_raising_run_leaves_an_earlier_stamp_alone(self):
        self._run(all=True, apply=True)
        earlier = _stamp()
        with patch('projects.management.commands.schedule_sites.schedule_site',
                   side_effect=RuntimeError('boom')):
            with self.assertRaises(RuntimeError):
                self._run(all=True, apply=True)
        self.assertEqual(_stamp(), earlier)

    def test_the_after_commit_trigger_writes_nothing(self):
        schedule_site_safely(self.site, actor=self.pm)
        self.assertEqual(_stamp(), (None, None))

    def test_the_admin_working_day_switch_writes_nothing(self):
        admin = _profile('schedr_admin', 'Admin')
        response = _client_for(admin).post(
            reverse('admin_master_switches'),
            {'schedule_action': 'confirm', 'schedule_skip_sundays': '1'})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(SystemSettings.get().schedule_skip_sundays)   # it did recalculate
        self.assertEqual(_stamp(), (None, None))

    def test_the_stamp_touches_only_its_own_columns(self):
        # filter().update() of two columns: a switch saved by Admin before the run keeps
        # its value (no get()+save() of a stale row).
        SystemSettings.get()
        SystemSettings.objects.filter(pk=1).update(email_enabled=True, maintenance_mode=True)
        self._run(all=True, apply=True)
        row = SystemSettings.objects.get(pk=1)
        self.assertTrue(row.email_enabled)
        self.assertTrue(row.maintenance_mode)
        self.assertIsNotNone(row.schedule_last_run_at)

    def test_a_fresh_database_gets_its_row(self):
        SystemSettings.objects.all().delete()
        self._run(all=True, apply=True)
        self.assertEqual(SystemSettings.objects.count(), 1)
        self.assertIsNotNone(_stamp()[0])


# ---------------------------------------------------------------------------
# 2. The line itself (R3): text and amber, at a given clock
# ---------------------------------------------------------------------------

class DatesAsOfTests(SchedFixture):

    def _set(self, value):
        SystemSettings.get()
        SystemSettings.objects.filter(pk=1).update(schedule_last_run_at=value)

    def test_never_written_is_amber(self):
        self._set(None)
        self.assertEqual(_dates_as_of(_ist(9, 10)), {'text': NEVER, 'stale': True})

    def test_no_row_at_all_is_amber_and_creates_nothing(self):
        SystemSettings.objects.all().delete()
        self.assertEqual(_dates_as_of(_ist(9, 10)), {'text': NEVER, 'stale': True})
        self.assertFalse(SystemSettings.objects.exists())

    def test_todays_run_shows_in_ist_not_amber(self):
        self._set(_ist(9, 6, 31))
        self.assertEqual(_dates_as_of(_ist(9, 10)),
                         {'text': 'Dates as of 9 Nov, 06:31', 'stale': False})

    def test_the_ist_time_not_utc_is_shown(self):
        # 01:01 UTC is 06:31 IST: the cron's real moment.
        self._set(datetime(2026, 11, 9, 1, 1, tzinfo=dt_timezone.utc))
        self.assertEqual(_dates_as_of(_ist(9, 10))['text'], 'Dates as of 9 Nov, 06:31')

    def test_exactly_0630_counts_as_today(self):
        self._set(_ist(9, 6, 30))
        self.assertFalse(_dates_as_of(_ist(9, 10))['stale'])

    def test_yesterdays_run_is_amber_not_today(self):
        self._set(_ist(8, 6, 31))
        self.assertEqual(_dates_as_of(_ist(9, 10)),
                         {'text': 'Dates as of 8 Nov, 06:31' + NOT_TODAY, 'stale': True})

    def test_a_manual_run_before_0630_today_is_still_not_today(self):
        self._set(_ist(9, 5, 0))
        self.assertTrue(_dates_as_of(_ist(9, 10))['stale'])

    # R3 as amended 10 Nov: the cutoff is the most recent 06:30 IST at least 30 minutes
    # past, so before 07:00 the page still compares against yesterday's 06:30.

    def test_late_evening_after_a_normal_run_is_grey(self):
        self._set(_ist(9, 6, 31))
        self.assertEqual(_dates_as_of(_ist(9, 23)),
                         {'text': 'Dates as of 9 Nov, 06:31', 'stale': False})

    def test_after_midnight_yesterdays_run_is_grey(self):
        self._set(_ist(8, 6, 31))
        self.assertFalse(_dates_as_of(_ist(9, 3))['stale'])

    def test_0645_with_yesterdays_stamp_is_grey_within_the_grace(self):
        self._set(_ist(8, 6, 31))
        self.assertEqual(_dates_as_of(_ist(9, 6, 45)),
                         {'text': 'Dates as of 8 Nov, 06:31', 'stale': False})

    def test_0705_with_yesterdays_stamp_is_amber(self):
        self._set(_ist(8, 6, 31))
        self.assertEqual(_dates_as_of(_ist(9, 7, 5)),
                         {'text': 'Dates as of 8 Nov, 06:31' + NOT_TODAY, 'stale': True})

    def test_the_grace_ends_at_0700_exactly(self):
        self._set(_ist(8, 6, 31))
        self.assertFalse(_dates_as_of(_ist(9, 6, 59))['stale'])
        self.assertTrue(_dates_as_of(_ist(9, 7, 0))['stale'])

    def test_a_missed_day_is_amber_even_overnight(self):
        # Two days old: yesterday's 06:30 slot was missed, so amber before 07:00 too.
        self._set(_ist(7, 6, 31))
        self.assertTrue(_dates_as_of(_ist(9, 3))['stale'])

    def test_never_run_is_amber_at_any_hour(self):
        self._set(None)
        for hour, minute in ((3, 0), (6, 45), (7, 5), (23, 0)):
            with self.subTest(at=f'{hour:02d}:{minute:02d}'):
                self.assertEqual(_dates_as_of(_ist(9, hour, minute)),
                                 {'text': NEVER, 'stale': True})


# ---------------------------------------------------------------------------
# 3. On the page (R3, R4) and its query cost
# ---------------------------------------------------------------------------

class DashboardLineTests(ViewFixture):

    def _set(self, value):
        SystemSettings.get()
        SystemSettings.objects.filter(pk=1).update(schedule_last_run_at=value)

    def _allowed(self):
        return (self.pm, self.coord, self.ceo, self.finance, self.scm, self.admin,
                self.sysadmin, self.head)

    def test_every_allowed_role_sees_the_stamp(self):
        self._set(_ist(9, 6, 31))
        with patch('django.utils.timezone.now', return_value=_ist(9, 10)):
            for profile in self._allowed():
                with self.subTest(role=profile.role, head=profile.is_design_head):
                    response = self._get(profile)
                    self.assertContains(response, 'Dates as of 9 Nov, 06:31')
                    self.assertNotContains(response, NOT_TODAY)
                    self.assertFalse(response.context['dates_as_of']['stale'])

    def test_the_amber_not_today_state(self):
        self._set(_ist(8, 6, 31))
        with patch('django.utils.timezone.now', return_value=_ist(9, 10)):
            response = self._get(self.ceo)
        self.assertContains(response, 'Dates as of 8 Nov, 06:31' + NOT_TODAY)
        self.assertContains(response, 'color: var(--sd-warn)')

    def test_the_never_run_state(self):
        self._set(None)
        response = self._get(self.pm)
        self.assertContains(response, NEVER)
        self.assertContains(response, 'color: var(--sd-warn)')

    def test_the_template_comment_is_not_printed(self):
        response = self._get(self.pm)
        self.assertNotContains(response, 'SCHED-R:')

    def test_the_line_costs_exactly_one_query(self):
        self._set(_ist(9, 6, 31))
        client = _client(self.ceo)
        client.get(URL)   # warm the session

        def count():
            with CaptureQueriesContext(connection) as captured:
                self.assertEqual(client.get(URL).status_code, 200)
            return len(captured.captured_queries)

        with_line = count()
        with patch('projects.views_site_dashboard._dates_as_of',
                   return_value={'text': 'x', 'stale': False}):
            without_line = count()
        self.assertEqual(with_line - without_line, 1)
