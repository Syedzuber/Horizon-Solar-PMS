"""
SCHED-2b: a block open past its due date pushes the rest forward (the daily forecast).

WHAT THIS PINS (spec SCHED_Auto_Scheduling_Spec.md, "Forecast shift"):

    1  Rule      an open block whose planned due date is before today re-anchors every
                 later block to the next working day after today, C = 0; the late block's
                 own dates and every manual date stay; a complete block keeps the
                 late-finish rule and the forecast does nothing; idempotent on one day
    2  Logging   one `due_dates_forecast_shift` row per site, never with an actor, alone
                 when every change was a forecast move, beside `due_dates_scheduled`
                 otherwise; nothing when nothing moved
    3  Command   the two new totals, the per-site forecast line, the -v 2 block lines
    4  Readers   the project overview feed hides the forecast row (the timeline shows
                 it); the audit log's 7-day "most active" ranks people only

All on the <=50 band (40 kWp), Civil due Tue 10 Nov 2026, the full 8-block chain, with
today frozen per test over the fixture's 9 Nov (freeze_today).

Run with:
    python manage.py test projects.tests_sched2b_forecast --settings=solarpms.test_settings
"""
from io import StringIO

from django.core.management import call_command
from django.urls import reverse

from .models import ActivityLog, Task
from .scheduling import FORECAST_ACTION_CODE, SKIP_MANUAL, schedule_site_safely
from .tests_sched2_scheduler import (
    CIVIL, D, DC_CABLE, INVERTER, LA, LATER_TASKS, MANUAL, MODULE, NET_METER_INSTALLATION,
    SCHEDULED, SYSTEM, TESTING, SchedFixture, freeze_today,
)
from .tests_two_step_completion import _client_for, _profile

AC_CABLE = 'AC_CABLE_LAYING'

# The plan with nothing late (today 9 or 10 Nov): blocks 2-8, (start, due).
# Day 1 = 11 Nov; see ArithmeticTests.test_le_50_band_in_calendar_days.
PLANNED = [(D(11), D(12)), (D(12), D(13)), (D(14), D(14)), (D(15), D(15)),
           (D(15), D(15)), (D(16), D(16)), (D(16), D(16))]

# Blocks 2-8 on the <=50 band, in half days: Modules 3, Earthing & DC 3, Inverter & DBs
# 2, AC cable 1, RMS & Gen meter 1, Testing 1, Net meter 1. With day 1 = X, a block that
# takes C from p to c starts on day floor(p / 2) + 1 and is due on day ceil(c / 2):
#     Modules        C 0-3    day 1 -> day 2
#     Earthing & DC  C 3-6    day 2 -> day 3
#     Inverter & DBs C 6-8    day 4 -> day 4
#     AC cable       C 8-9    day 5 -> day 5
#     RMS & Gen      C 9-10   day 5 -> day 5
#     Testing        C 10-11  day 6 -> day 6
#     Net meter      C 11-12  day 6 -> day 6


def _from_day_1(days):
    """Blocks 2-8 for a chain whose working days 1..6 are `days` (a 6-item list)."""
    d = dict(enumerate(days, start=1))
    return [(d[1], d[2]), (d[2], d[3]), (d[4], d[4]), (d[5], d[5]), (d[5], d[5]),
            (d[6], d[6]), (d[6], d[6])]


class ForecastRuleTests(SchedFixture):
    """Verification bar 1."""

    def setUp(self):
        self.site = self._site('40.00', civil_due=D(10))

    def test_today_is_the_civil_due_date_so_nothing_moves(self):
        with freeze_today(D(10)):
            result = self._plan(self.site)
        self.assertIsNone(result.forecast)
        self.assertEqual(self._dates(result)[1:], PLANNED)
        self.assertEqual(self._dates(result)[0], (D(7), D(10)))

    def test_civil_open_on_13_nov_starts_modules_on_14_nov(self):
        """Civil due Tue 10 Nov, still open on Fri 13 Nov: it is forecast to finish
        today, so day 1 of the rest is the next day after today, Sat 14 Nov (calendar
        days). Days 1-6 = 14, 15, 16, 17, 18, 19 Nov:
            Modules        14 -> 15
            Earthing & DC  15 -> 16
            Inverter & DBs 17 -> 17
            AC cable       18 -> 18
            RMS & Gen      18 -> 18
            Testing        19 -> 19
            Net meter      19 -> 19
        Civil keeps its own dates: planned start 7 Nov, due 10 Nov (red)."""
        with freeze_today(D(13)):
            result = self._plan(self.site)
            applied = self._apply(self.site)
        self.assertIs(result.forecast, result.blocks[0])
        self.assertEqual((result.forecast.forecast_after, result.forecast_start), (D(13), D(14)))
        self.assertEqual(self._dates(result)[0], (D(7), D(10)))
        self.assertEqual(self._dates(result)[1:],
                         _from_day_1([D(14), D(15), D(16), D(17), D(18), D(19)]))
        self.assertEqual(applied.forecast_moved, LATER_TASKS)
        civil = self._task(self.site, CIVIL)
        self.assertEqual((civil.due_date, civil.due_date_source), (D(10), MANUAL),
                         'the Civil date is never changed')
        module = self._task(self.site, MODULE)
        self.assertEqual((module.schedule_start, module.due_date, module.due_date_source),
                         (D(14), D(15), SYSTEM))
        self.assertEqual(self._task(self.site, NET_METER_INSTALLATION).due_date, D(19))

    def test_civil_open_on_14_nov_slides_one_more_day(self):
        with freeze_today(D(14)):
            result = self._plan(self.site)
        self.assertEqual(self._dates(result)[1:],
                         _from_day_1([D(15), D(16), D(17), D(18), D(19), D(20)]))
        self.assertEqual(result.blocks[1].start, D(15))

    def test_civil_open_on_13_nov_with_sundays_skipped(self):
        """Working days after Fri 13 Nov, Sun 15 skipped: days 1-6 = 14, 16, 17, 18,
        19, 20 Nov. Modules 14 -> 16, Earthing & DC 16 -> 17, Inverter & DBs 18,
        AC cable 19, RMS & Gen 19, Testing 20, Net meter 20. Civil counted back over
        Sun 8 Nov starts on 6 Nov, as before."""
        with freeze_today(D(13)):
            result = self._plan(self.site, skip_sundays=True)
        self.assertEqual(self._dates(result)[0], (D(6), D(10)))
        self.assertEqual(self._dates(result)[1:],
                         _from_day_1([D(14), D(16), D(17), D(18), D(19), D(20)]))
        later = [d for pair in self._dates(result)[1:] for d in pair]
        self.assertNotIn(6, {d.weekday() for d in later}, 'no planned date on a Sunday')

    def test_a_civil_done_late_keeps_the_late_finish_rule_and_no_forecast(self):
        """Civil due 10 Nov, Done 12 Nov, today 13 Nov: the late-finish rule restarts
        block 2 on 13 Nov; Modules is then due 14 Nov, not late, so no forecast."""
        self._done(self._task(self.site, CIVIL), self._ist(12))
        with freeze_today(D(13)):
            result = self._plan(self.site)
        self.assertIsNone(result.forecast)
        self.assertEqual(result.blocks[0].re_anchored_after, D(12))
        self.assertEqual(self._dates(result)[1:],
                         _from_day_1([D(13), D(14), D(15), D(16), D(17), D(18)]))

    def test_block_3_open_and_late_moves_only_blocks_4_to_8(self):
        """Civil Done 10 Nov and Modules Done 12 Nov, both on time; Earthing & DC
        (planned 12 -> 13 Nov) still open on Sun 15 Nov. Blocks 1-3 keep the plan; day 1
        of blocks 4-8 = Mon 16 Nov: Inverter & DBs 16 (C 0-2), AC cable 17 (C 2-3),
        RMS & Gen 17 (C 3-4), Testing 18 (C 4-5), Net meter 18 (C 5-6).
        Scheduled first on 9 Nov, so Earthing & DC already carries its system dates
        (a never-dated block would be first-dated instead; FirstDatingTests)."""
        self._done(self._task(self.site, CIVIL), self._ist(10))
        self._done(self._task(self.site, MODULE), self._ist(12))
        self._apply(self.site)                       # 9 Nov: the plan
        with freeze_today(D(15)):
            result = self._plan(self.site)
            self._apply(self.site)
        self.assertIs(result.forecast, result.blocks[2])
        self.assertEqual(self._dates(result)[:3], [(D(7), D(10))] + PLANNED[:2])
        self.assertEqual(self._dates(result)[3:], [
            (D(16), D(16)), (D(17), D(17)), (D(17), D(17)), (D(18), D(18)), (D(18), D(18))])
        self.assertEqual(result.to_forecast, 7, 'blocks 4-8 hold 7 tasks')
        for code in (LA, DC_CABLE):
            task = self._task(self.site, code)
            self.assertEqual((task.schedule_start, task.due_date), (D(12), D(13)),
                             'the late block keeps its own dates')
        self.assertEqual(self._task(self.site, INVERTER).due_date, D(16))

    def test_a_manual_date_in_block_5_never_moves(self):
        """AC cable typed by the PM for 12 Nov: on 13 Nov it is behind the forecast and
        still is not written; the blocks after it move as usual."""
        ac = self._task(self.site, AC_CABLE)
        self._set_due(ac, D(12))
        with freeze_today(D(13)):
            result = self._apply(self.site)
        actions = {tp.task.pk: tp.action for bp in result.blocks for tp in bp.tasks}
        self.assertEqual(actions[ac.pk], SKIP_MANUAL)
        ac.refresh_from_db()
        self.assertEqual((ac.due_date, ac.due_date_source, ac.schedule_start),
                         (D(12), MANUAL, None))
        self.assertEqual(self._task(self.site, TESTING).due_date, D(19))
        self.assertEqual(result.forecast_moved, LATER_TASKS - 1)

    def test_running_twice_on_one_day_writes_and_logs_nothing_the_second_time(self):
        self._apply(self.site)                       # 9 Nov: the plan
        with freeze_today(D(13)):
            first = self._apply(self.site)
            logs = ActivityLog.objects.filter(project=self.site).count()
            second = self._apply(self.site)
        self.assertEqual(first.forecast_moved, LATER_TASKS)
        self.assertEqual((second.written, second.started, second.cleared,
                          second.forecast_moved), (0, 0, 0, 0))
        self.assertIsNotNone(second.forecast, 'the site still has an open late block')
        self.assertEqual(ActivityLog.objects.filter(project=self.site).count(), logs)

    def test_the_next_day_moves_the_forecast_on_by_a_day(self):
        self._apply(self.site)
        with freeze_today(D(13)):
            self._apply(self.site)
        with freeze_today(D(14)):
            result = self._apply(self.site)
        self.assertEqual(result.forecast_moved, LATER_TASKS)
        self.assertEqual(self._task(self.site, MODULE).due_date, D(16))


class FirstDatingTests(SchedFixture):
    """The amendment: a system date is never written in the past for the first time.
    Civil Done on time on 10 Nov; today 20 Nov; Modules (planned 11 -> 12) still open."""

    def setUp(self):
        self.site = self._site('40.00', civil_due=D(10))
        self._done(self._task(self.site, CIVIL), self._ist(10))

    def _forecast_rows(self):
        return list(ActivityLog.objects.filter(project=self.site,
                                               action_code=FORECAST_ACTION_CODE)
                    .order_by('timestamp', 'pk').values_list('action', 'actor'))

    def test_a_never_dated_block_past_its_plan_starts_after_today(self):
        """(a) First scheduling on Fri 20 Nov: Modules has no system date, so it starts
        on day 1 = Sat 21 Nov and the rest chain from it. Days 1-6 = 21-26 Nov. Nothing
        is newly overdue, and the only row is the forecast row (no Civil start is
        written, the Civil task is Done)."""
        with freeze_today(D(20)):
            result = self._apply(self.site, actor=self.pm)
        self.assertIs(result.forecast, result.blocks[1])
        self.assertEqual(result.blocks[1].first_dated, D(21))
        self.assertEqual(self._dates(result)[1:],
                         _from_day_1([D(21), D(22), D(23), D(24), D(25), D(26)]))
        self.assertEqual(result.overdue_new, 0)
        self.assertEqual(result.forecast_moved, LATER_TASKS)
        self.assertEqual(self._logs(self.site), [], 'no due_dates_scheduled row')
        self.assertEqual(self._forecast_rows(), [
            ('Forecast: Modules not yet dated and already past plan; started 2026-11-21',
             None)])
        module = self._task(self.site, MODULE)
        self.assertEqual((module.schedule_start, module.due_date, module.due_date_source),
                         (D(21), D(22), SYSTEM))

    def test_a_block_already_system_dated_keeps_its_dates_and_the_rest_move(self):
        """(b) Modules already carries system dates 11 -> 12 Nov: it keeps them (red),
        and Earthing & DC onward starts on 21 Nov. Days 1-5 = 21-25 Nov:
            Earthing & DC  C 0-3  21 -> 22
            Inverter & DBs C 3-5  22 -> 23
            AC cable       C 5-6  23 -> 23
            RMS & Gen      C 6-7  24 -> 24
            Testing        C 7-8  24 -> 24
            Net meter      C 8-9  25 -> 25"""
        module = self._task(self.site, MODULE)
        Task.objects.filter(pk=module.pk).update(due_date=D(12), due_date_source=SYSTEM,
                                                 schedule_start=D(11))
        with freeze_today(D(20)):
            result = self._apply(self.site)
        self.assertIs(result.forecast, result.blocks[1])
        self.assertIsNone(result.blocks[1].first_dated)
        self.assertEqual(self._dates(result)[1:], [
            (D(11), D(12)), (D(21), D(22)), (D(22), D(23)), (D(23), D(23)),
            (D(24), D(24)), (D(24), D(24)), (D(25), D(25))])
        module.refresh_from_db()
        self.assertEqual((module.schedule_start, module.due_date), (D(11), D(12)))
        self.assertEqual(result.overdue_new, 0, 'Modules was already overdue')
        self.assertEqual(self._forecast_rows(), [
            ('Forecast: Modules open past due 2026-11-12; 9 later dates moved to start '
             '2026-11-21', None)])

    def test_running_twice_writes_nothing_the_second_time(self):
        """(c)"""
        with freeze_today(D(20)):
            self._apply(self.site)
            logs = ActivityLog.objects.filter(project=self.site).count()
            second = self._apply(self.site)
        self.assertEqual((second.written, second.started, second.cleared,
                          second.forecast_moved), (0, 0, 0, 0))
        self.assertEqual(ActivityLog.objects.filter(project=self.site).count(), logs)

    def test_the_next_days_keep_the_first_dates_until_they_pass(self):
        """The first dates are system dates from then on, so the block keeps them: on
        21 and 22 Nov nothing changes; on 23 Nov Modules (due 22) is late and stays red,
        and the forecast moves Earthing & DC onward to start on 24 Nov."""
        with freeze_today(D(20)):
            self._apply(self.site)
        for day in (D(21), D(22)):
            with freeze_today(day):
                again = self._apply(self.site)
            self.assertEqual((again.written, again.forecast_moved), (0, 0), day)
        with freeze_today(D(23)):
            result = self._apply(self.site)
        self.assertEqual(self._dates(result)[1], (D(21), D(22)))
        self.assertEqual(result.blocks[2].start, D(24))
        self.assertEqual(self._forecast_rows()[-1], (
            'Forecast: Modules open past due 2026-11-22; 9 later dates moved to start '
            '2026-11-24', None))

    def test_stored_dates_earlier_than_the_plan_do_not_pull_it_earlier(self):
        """Modules system-dated 5 -> 6 Nov, before its plan (11 -> 12): the plan wins,
        so it is written 11 -> 12 and the rest move as for (b)."""
        Task.objects.filter(pk=self._task(self.site, MODULE).pk).update(
            due_date=D(6), due_date_source=SYSTEM, schedule_start=D(5))
        with freeze_today(D(20)):
            result = self._apply(self.site)
        self.assertEqual(self._dates(result)[1], (D(11), D(12)))
        self.assertEqual(result.blocks[2].start, D(21))
        self.assertEqual(self._task(self.site, MODULE).due_date, D(12))

    def test_the_command_names_the_first_dated_block(self):
        out = StringIO()
        with freeze_today(D(20)):
            call_command('schedule_sites', site=self.site.project_id, verbosity=2, stdout=out)
        out = out.getvalue()
        self.assertIn('    forecast: Modules not yet dated and already past plan; started '
                      '2026-11-21; 10 dates to write from it', out)
        self.assertIn('2026-11-21 -> 2026-11-22 first-dated  [', out)
        self.assertIn('Sites with an open late block: 1', out)


class ForecastLoggingTests(SchedFixture):
    """Task 2 and rulings 1-2."""

    def setUp(self):
        self.site = self._site('40.00', civil_due=D(10))

    def _rows(self, code):
        return list(ActivityLog.objects.filter(project=self.site, action_code=code)
                    .order_by('timestamp', 'pk').values_list('action', 'actor'))

    def test_only_the_forecast_row_when_every_change_is_a_forecast_move(self):
        """A trigger run (the PM's action ran the scheduler) on 13 Nov: the only changes
        are forecast moves, so one forecast row, with no actor."""
        self._apply(self.site)                       # 9 Nov: one due_dates_scheduled row
        with freeze_today(D(13)):
            schedule_site_safely(self.site, self.pm)
        self.assertEqual(len(self._rows(SCHEDULED)), 1, 'no second scheduled row')
        self.assertEqual(self._rows(FORECAST_ACTION_CODE), [
            ('Forecast: Civil & MMS open past due 2026-11-10; 10 later dates moved to '
             'start 2026-11-14', None)])

    def test_both_rows_when_the_run_also_changes_something_else(self):
        """First schedule of a site whose Civil is already late: the Civil planned start
        is set too, so the usual row (with the PM as actor) is written beside the
        forecast row (with none)."""
        with freeze_today(D(13)):
            self._apply(self.site, actor=self.pm)
        self.assertEqual(self._rows(SCHEDULED), [
            ('Auto-scheduled due dates: anchor Civil due 2026-11-10, band <=50 kWp; '
             '10 written, 1 start set, 0 cleared', self.pm.pk)])
        self.assertEqual(self._rows(FORECAST_ACTION_CODE), [
            ('Forecast: Civil & MMS open past due 2026-11-10; 10 later dates moved to '
             'start 2026-11-14', None)])

    def test_a_block_after_civil_names_its_own_due_date(self):
        self._done(self._task(self.site, CIVIL), self._ist(10))
        self._done(self._task(self.site, MODULE), self._ist(12))
        self._apply(self.site)
        with freeze_today(D(15)):
            self._apply(self.site)
        self.assertEqual(self._rows(FORECAST_ACTION_CODE), [
            ('Forecast: Earthing & DC open past due 2026-11-13; 7 later dates moved to '
             'start 2026-11-16', None)])

    def test_no_forecast_row_when_nothing_is_late_or_on_a_dry_run(self):
        self._apply(self.site)
        with freeze_today(D(13)):
            self._plan(self.site)
        self.assertEqual(self._rows(FORECAST_ACTION_CODE), [])


class ForecastCommandTests(SchedFixture):
    """Task 4: the totals, the forecast line, and -v 2."""

    def setUp(self):
        self.site = self._site('40.00', civil_due=D(10))

    def _run(self, *args, **options):
        out = StringIO()
        call_command('schedule_sites', *args, site=self.site.project_id, stdout=out, **options)
        return out.getvalue()

    def test_a_dry_run_reports_the_forecast_and_writes_nothing(self):
        with freeze_today(D(13)):
            out = self._run(verbosity=1)
        self.assertIn('Sites with an open late block: 1', out)
        self.assertIn('Dates moved by forecast: 10 (would move)', out)
        self.assertIn('    forecast: Civil & MMS open past due 2026-11-10; 10 later dates '
                      'to move to start 2026-11-14', out)
        self.assertNotIn('Modules', out, 'block lines only with -v 2')
        self.assertIsNone(self._task(self.site, MODULE).due_date)

    def test_verbosity_2_lists_every_block_with_its_tasks(self):
        with freeze_today(D(13)):
            out = self._run(verbosity=2)
        lines = out.splitlines()
        civil = next(l for l in lines if l.startswith('      1 Civil & MMS'))
        self.assertIn('2026-11-07 -> 2026-11-10 forecast', civil)
        self.assertIn('=write:start', civil)
        modules = next(l for l in lines if l.startswith('      2 Modules'))
        self.assertIn('2026-11-14 -> 2026-11-15  [', modules)
        self.assertNotIn('forecast', modules)
        self.assertIn('=write]', modules)
        self.assertEqual(sum(1 for l in lines if l.startswith('      ')), 8)

    def test_apply_reports_the_dates_moved(self):
        call_command('schedule_sites', site=self.site.project_id, apply=True, stdout=StringIO())
        with freeze_today(D(13)):
            out = self._run(apply=True)
        self.assertIn('Dates moved by forecast: 10\n', out)
        self.assertIn('10 later dates moved to start 2026-11-14', out)


class ForecastReaderTests(SchedFixture):
    """Ruling 3: the two readers the forecast rows would distort."""

    def test_the_overview_feed_hides_the_forecast_row_and_the_timeline_shows_it(self):
        site = self._site('40.00', civil_due=D(10))
        with freeze_today(D(13)):
            self._apply(site, actor=self.pm)
        self.assertTrue(ActivityLog.objects.filter(
            project=site, action_code=FORECAST_ACTION_CODE).exists())
        client = _client_for(self.pm)
        feed = client.get(reverse('project_overview', args=[site.project_id])).context[
            'recent_activity']
        codes = {row.action_code for row in feed}
        self.assertNotIn(FORECAST_ACTION_CODE, codes)
        self.assertIn(SCHEDULED, codes, 'only the forecast row is hidden')
        page = client.get(reverse('project_timeline', args=[site.project_id])).context[
            'page_obj']
        self.assertIn(FORECAST_ACTION_CODE, {row.action_code for row in page})

    def test_the_audit_log_most_active_ranks_people_not_system_rows(self):
        admin = _profile('s2b_admin', 'Admin')
        ActivityLog.objects.bulk_create([
            ActivityLog(project=None, actor=None, action='Forecast: system row',
                        action_code=FORECAST_ACTION_CODE) for _ in range(50)])
        ActivityLog.objects.create(project=None, actor=admin, action='A person acted',
                                   action_code='')
        stats = _client_for(admin).get(reverse('admin_audit_log')).context['stats']
        self.assertIsNotNone(stats['most_active'])
        self.assertIsNotNone(stats['most_active']['actor__role'],
                             'the blank no-actor group must not rank')
        self.assertGreaterEqual(stats['today'], 51, 'the today count still has them')
