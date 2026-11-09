"""
SCHED-2 — the RESCO auto-scheduler: duration table, arithmetic, rules, triggers, switch.

WHAT THIS PINS (spec SCHED_Auto_Scheduling_Spec.md Layers 4 and 5, as changed 9 Nov:
the anchor is the due date of the site's unlabelled Civil task; block 1 is never
written; block 2 starts on the next working day after the Civil date with C reset):

    1  Arithmetic   exact planned dates for a Civil due date of Tue 10 Nov 2026, in all
                    three bands, calendar days and Sundays skipped, the Civil planned
                    start counted back, a weekend Civil date, band edges, no anchor
    2  Rules        what the scheduler never writes (the Civil date, manual and
                    null-source dates, Done, N/A, location copies, mirrors), late Civil
                    and late block finishes re-anchor, early finishes do not, all-N/A
                    block = 0 days, clearing (capacity, Civil date, Civil N/A, the
                    exclusion flag), idempotence
    3  Triggers     every write site queues a reschedule AFTER COMMIT: the Civil date
                    (edit screen both paths, In Progress entry, Django admin), Done in
                    and out, N/A, capacity, exclusion; a scheduler failure never undoes
                    the change
    4  Overdue      the preview count equals the shared rule's count after applying,
                    split into already overdue and newly overdue
    5  Switch       the Admin working-day switch previews, then recalculates on confirm

Fixtures are real: the OPEX v1 template and the eight blocks come from the 0075, 0138
and 0139 seed functions, and every site is activated through opex_site_activate.

SCHED-2c (spec "Meter blocks", 9 Nov): block 6 is RMS & Gen meter (0.5 day in every band),
Testing moved to 7, and block 8 is Net meter (0.5 / 1 / 1).

Run with:
    python manage.py test projects.tests_sched2_scheduler --settings=solarpms.test_settings
"""
from datetime import date, datetime, timedelta
from datetime import timezone as dt_timezone
from decimal import Decimal
from importlib import import_module
from unittest.mock import patch

from django.contrib import admin as django_admin
from django.contrib.auth.models import User
from django.contrib.messages import get_messages
from django.db import DatabaseError
from django.test import Client, RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from .models import (
    ActivityLog, Project, ScheduleBlock, ScheduleBlockTask, SystemSettings, Task,
    TaskTemplate, TaskTemplatePhase, TaskTemplateTask,
)
from .scheduling import (
    ACTION_UNCHANGED, ACTION_WRITE_START, ANCHOR_CIVIL_DUE_DATE, BAND_101_PLUS, BAND_51_100,
    BAND_LE_50, REASON_EXCLUDED, REASON_NEEDS_ANCHOR, REASON_NEEDS_CAPACITY,
    REASON_NOT_ELIGIBLE, SKIP_DONE, SKIP_MANUAL, SKIP_NA, capacity_band, schedule_site,
    set_task_due_date, status_change_reschedules, task_is_schedule_anchor, task_is_scheduled,
)
from .task_health import overdue_q
from .tests_sched1_due_date_source import _admin_post_data
from .tests_two_step_completion import _client_for, _profile
from .utils import assign_task_to, human_owned_tasks_q

MANUAL = Task.DUE_DATE_MANUAL
SYSTEM = Task.DUE_DATE_SYSTEM

CIVIL = 'CIVIL_WORK_AND_MMS_INSTALLATION'
MODULE = 'MODULE_INSTALLATION'
LA = 'LA_AND_EARTHING_INSTALLATION'
DC_CABLE = 'DC_CABLE_LAYING_WITH_CONDUIT'
INVERTER = 'INVERTER_INSTALLATION'
TESTING = 'TESTING_COMMISSIONING'
NET_METERING = 'NET_METERING_APPROVAL'      # a PM task in no block
NET_METER_INSTALLATION = 'NET_METER_INSTALLATION'
RMS = 'RMS_INSTALLATION'
GEN_METER = 'SOLAR_GENERATION_METER_INSTALLATION'
COD = 'COD'                                 # a mirror

SCHEDULED = 'due_dates_scheduled'

# Blocks 2-8 hold 10 tasks: Modules 1, Earthing & DC 2, Inverter & DBs 2, AC 1,
# RMS & Gen meter 2, Testing 1, Net meter 1.
LATER_TASKS = 10


def D(day, month=11, year=2026):
    return date(year, month, day)


# SCHED-2b made the plan depend on "today": a block open past its due date re-anchors
# the blocks after it. These tests use a Civil due date of 10 Nov 2026 and were written
# for a today before it, so the whole suite runs on a fixed today, 9 Nov 2026, instead of
# changing its answers from 11 Nov on. Tests that need another day patch again over it.
FROZEN_TODAY = date(2026, 11, 9)
_REAL_LOCALDATE = timezone.localdate


def freeze_today(day):
    """A patch of django.utils.timezone.localdate that answers `day` when asked for
    today, and still converts a datetime it is given. Patched on the module, not on
    projects.scheduling, so the views (typed-date checks, the overdue rule) agree with
    the scheduler about which day it is."""
    def localdate(value=None, timezone=None):
        return day if value is None else _REAL_LOCALDATE(value, timezone)
    return patch('django.utils.timezone.localdate', side_effect=localdate)


class _Apps:
    """Stands in for the `apps` registry the two seed migrations are handed; both only
    call apps.get_model()."""

    _MODELS = {model.__name__: model for model in (
        TaskTemplate, TaskTemplatePhase, TaskTemplateTask, Task,
        ScheduleBlock, ScheduleBlockTask)}

    def get_model(self, app_label, model_name):
        assert app_label == 'projects'
        return self._MODELS[model_name]


def _seed_template_and_blocks():
    """The OPEX v1 template (0075), the six blocks (0138) and the meter blocks (0139),
    through the migrations' own functions, so the fixture is the production table and not
    a copy of it."""
    import_module('projects.migrations.0075_seed_opex_template_v1').seed_opex_v1(_Apps(), None)
    # On a migrated database (test_settings_postgres) 0138 and 0139 have already run; 0075
    # skips itself when its template exists, and the blocks follow the same rule here.
    opex_blocks = ScheduleBlock.objects.filter(template__code='OPEX', template__version_no=1)
    if not opex_blocks.exists():
        import_module('projects.migrations.0138_sched2_seed_schedule_blocks').seed_schedule_blocks(
            _Apps(), None)
    if not opex_blocks.filter(name='Net meter').exists():
        import_module('projects.migrations.0139_sched2c_meter_blocks').add_meter_blocks(
            _Apps(), None)


class SchedFixture(TestCase):
    """Seeded template and blocks, a PM, and a site engineer; sites made per test.
    Today is FROZEN_TODAY for the whole class (see freeze_today)."""

    @classmethod
    def setUpClass(cls):
        patcher = freeze_today(FROZEN_TODAY)
        patcher.start()
        cls.addClassCleanup(patcher.stop)
        super().setUpClass()

    @classmethod
    def setUpTestData(cls):
        _seed_template_and_blocks()
        cls.pm = _profile('s2_pm', 'PM')
        cls.se = _profile('s2_se', 'Site Engineer')

    def _site(self, dc='40.00', civil_due=None, **fields):
        """A RESCO site activated through the real view, then given `fields` and, when
        `civil_due` is given, a manual Civil due date (the anchor)."""
        site = Project.objects.create(
            customer_name='SCHED-2 Site', customer_phone='9876543210',
            site_address='1 Schedule Road', city='Lucknow', project_type='OPEX',
            dc_capacity_kw=Decimal('40.00'), status='Draft', assigned_pm=self.pm,
        )
        response = _client_for(self.pm).post(
            reverse('opex_site_activate', args=[site.project_id]))
        self.assertEqual(response.status_code, 302, 'OPEX activation did not redirect')
        self.assertEqual(Project.objects.get(pk=site.pk).status, 'Active')
        fields.setdefault('dc_capacity_kw', None if dc is None else Decimal(dc))
        Project.objects.filter(pk=site.pk).update(**fields)
        site.refresh_from_db()
        if civil_due is not None:
            self._set_due(self._task(site, CIVIL), civil_due)
        return site

    def _set_due(self, task, value, source=MANUAL):
        set_task_due_date(task, value, source, self.pm)
        task.save(update_fields=['due_date', 'due_date_source'])

    def _task(self, site, code):
        return Task.objects.get(phase__project=site, template_task__code=code, location_label='')

    def _plan(self, site, skip_sundays=False):
        return schedule_site(Project.objects.get(pk=site.pk), skip_sundays=skip_sundays)

    def _apply(self, site, skip_sundays=False, actor=None):
        return schedule_site(Project.objects.get(pk=site.pk), actor=actor, apply=True,
                             skip_sundays=skip_sundays)

    @staticmethod
    def _dates(result):
        return [(bp.start, bp.due) for bp in result.blocks]

    def _logs(self, site):
        # Oldest first: ActivityLog's default ordering is newest first.
        return list(ActivityLog.objects.filter(project=site, action_code=SCHEDULED)
                    .order_by('timestamp', 'pk').values_list('action', flat=True))

    def _done(self, task, when):
        """Put a task in Done as of `when` (an aware datetime). A fixture write, not a
        product path: the late-finish rule reads only status and completed_at."""
        Task.objects.filter(pk=task.pk).update(status=Task.DONE, completed_at=when,
                                               approved_at=when)

    def _ist(self, day, hour=12, month=11):
        return timezone.make_aware(datetime(2026, month, day, hour, 0))


# ---------------------------------------------------------------------------
# 1. Arithmetic — exact dates (verification bar 2 / item 5)
# ---------------------------------------------------------------------------

class ArithmeticTests(SchedFixture):
    """(start, due) per block, blocks 1-8; block 1 is (Civil planned start, Civil due date).

    Rule (scheduling._plan): day 1 is the next working day after the Civil due date
    (Wed 11 Nov); C is the running total of half days; a block that takes C from p to c
    starts on day floor(p / 2) + 1 and is due on day ceil(c / 2). The calendar-day rows
    are the spec's "Meter blocks" worked example."""

    def test_the_dates_are_the_weekdays_used_below(self):
        self.assertEqual(D(10).weekday(), 1, '10 Nov 2026 must be a Tuesday')
        self.assertEqual(D(15).weekday(), 6, '15 Nov 2026 must be a Sunday')
        self.assertEqual(D(22).weekday(), 6, '22 Nov 2026 must be a Sunday')
        self.assertEqual(D(8).weekday(), 6, '8 Nov 2026 must be a Sunday')

    def test_le_50_band_in_calendar_days(self):
        result = self._plan(self._site('40.00', civil_due=D(10)))
        self.assertIsNone(result.reason)
        self.assertEqual(result.band, BAND_LE_50)
        self.assertEqual((result.anchor, result.anchor_source), (D(10), ANCHOR_CIVIL_DUE_DATE))
        self.assertEqual(self._dates(result), [
            (D(7), D(10)), (D(11), D(12)), (D(12), D(13)),
            (D(14), D(14)), (D(15), D(15)),
            (D(15), D(15)),   # 6 RMS & Gen meter  C 9 -> 10
            (D(16), D(16)),   # 7 Testing          C 10 -> 11
            (D(16), D(16)),   # 8 Net meter        C 11 -> 12
        ])

    def test_le_50_band_with_sundays_skipped(self):
        """Working days from 11 Nov, Sun 15 skipped: day 1 = 11, 2 = 12, 3 = 13, 4 = 14,
        5 = 16, 6 = 17.
            Modules        C 0-3    start day 1 = 11, due day 2 = 12
            Earthing & DC  C 3-6    start day 2 = 12, due day 3 = 13
            Inverter & DBs C 6-8    start day 4 = 14, due day 4 = 14
            AC cable       C 8-9    start day 5 = 16, due day 5 = 16
            RMS & Gen      C 9-10   start day 5 = 16, due day 5 = 16
            Testing        C 10-11  start day 6 = 17, due day 6 = 17
            Net meter      C 11-12  start day 6 = 17, due day 6 = 17
        Civil: 3.5 days -> 4 working days ending 10 Nov, back over Sun 8 -> 6 Nov."""
        result = self._plan(self._site('40.00', civil_due=D(10)), skip_sundays=True)
        self.assertEqual(self._dates(result), [
            (D(6), D(10)), (D(11), D(12)), (D(12), D(13)),
            (D(14), D(14)), (D(16), D(16)),
            (D(16), D(16)), (D(17), D(17)), (D(17), D(17)),
        ])
        later = [d for pair in self._dates(result)[1:] for d in pair]
        self.assertNotIn(6, {d.weekday() for d in later}, 'no planned date on a Sunday')

    def test_51_100_band_in_calendar_days(self):
        result = self._plan(self._site('75.00', civil_due=D(10)))
        self.assertEqual(result.band, BAND_51_100)
        self.assertEqual(self._dates(result), [
            (D(4), D(10)), (D(11), D(14)), (D(15), D(17)),
            (D(17), D(18)), (D(18), D(18)),
            (D(19), D(19)),   # 6 RMS & Gen meter  C 16 -> 17
            (D(19), D(20)),   # 7 Testing          C 17 -> 19
            (D(20), D(21)),   # 8 Net meter        C 19 -> 21
        ])

    def test_101_plus_band_in_calendar_days(self):
        result = self._plan(self._site('250.00', civil_due=D(10)))
        self.assertEqual(result.band, BAND_101_PLUS)
        self.assertEqual(self._dates(result), [
            (D(31, 10), D(10)), (D(11), D(15)), (D(16), D(20)),
            (D(21), D(21)), (D(22), D(23)),
            (D(24), D(24)),   # 6 RMS & Gen meter  C 26 -> 27
            (D(24), D(25)),   # 7 Testing          C 27 -> 29
            (D(25), D(26)),   # 8 Net meter        C 29 -> 31
        ])

    def test_101_plus_band_with_sundays_skipped(self):
        """Working days from 11 Nov, Suns 15 and 22 skipped: day 1 = 11 ... 4 = 14,
        5 = 16 ... 10 = 21, 11 = 23, 12 = 24, 13 = 25, 14 = 26, 15 = 27, 16 = 28.
            Modules        C 0-10   start day 1 = 11,  due day 5 = 16
            Earthing & DC  C 10-20  start day 6 = 17,  due day 10 = 21
            Inverter & DBs C 20-22  start day 11 = 23, due day 11 = 23
            AC cable       C 22-26  start day 12 = 24, due day 13 = 25
            RMS & Gen      C 26-27  start day 14 = 26, due day 14 = 26
            Testing        C 27-29  start day 14 = 26, due day 15 = 27
            Net meter      C 29-31  start day 15 = 27, due day 16 = 28
        Civil: 11 days -> 11 working days ending 10 Nov, back over Suns 8 and 1 -> 29 Oct."""
        result = self._plan(self._site('250.00', civil_due=D(10)), skip_sundays=True)
        self.assertEqual(self._dates(result), [
            (D(29, 10), D(10)), (D(11), D(16)), (D(17), D(21)),
            (D(23), D(23)), (D(24), D(25)),
            (D(26), D(26)), (D(26), D(27)), (D(27), D(28)),
        ])
        later = [d for pair in self._dates(result)[1:] for d in pair]
        self.assertNotIn(6, {d.weekday() for d in later}, 'no planned date on a Sunday')

    def test_block_2_after_a_saturday_civil_date_skips_the_sunday(self):
        site = self._site('40.00', civil_due=D(14))
        self.assertEqual(self._plan(site, skip_sundays=True).blocks[1].start, D(16))
        self.assertEqual(self._plan(site, skip_sundays=False).blocks[1].start, D(15))

    def test_band_edges(self):
        self.assertEqual(capacity_band(Decimal('50.0')), BAND_LE_50)
        self.assertEqual(capacity_band(Decimal('50.1')), BAND_51_100)
        self.assertEqual(capacity_band(Decimal('100.0')), BAND_51_100)
        self.assertEqual(capacity_band(Decimal('100.1')), BAND_101_PLUS)
        self.assertIsNone(capacity_band(None))
        self.assertIsNone(capacity_band(Decimal('0')))

    def test_null_and_zero_capacity_need_capacity(self):
        for dc in (None, '0.00'):
            result = self._plan(self._site(dc, civil_due=D(10)))
            self.assertEqual(result.reason, REASON_NEEDS_CAPACITY, dc)
            self.assertEqual(result.blocks, [])

    def test_no_civil_date_or_a_not_applicable_civil_needs_an_anchor(self):
        self.assertEqual(self._plan(self._site('40.00')).reason, REASON_NEEDS_ANCHOR)
        site = self._site('40.00', civil_due=D(10))
        Task.objects.filter(pk=self._task(site, CIVIL).pk).update(is_not_applicable=True)
        self.assertEqual(self._plan(site).reason, REASON_NEEDS_ANCHOR)

    def test_apply_writes_blocks_2_to_8_and_only_the_civil_start(self):
        site = self._site('40.00', civil_due=D(10))
        result = self._apply(site, actor=self.pm)
        self.assertEqual((result.written, result.started), (LATER_TASKS, 1))
        civil = self._task(site, CIVIL)
        self.assertEqual((civil.due_date, civil.due_date_source, civil.schedule_start),
                         (D(10), MANUAL, D(7)), 'the Civil date is the PM\'s; only its start is set')
        for code in (LA, DC_CABLE):
            task = self._task(site, code)
            self.assertEqual((task.schedule_start, task.due_date, task.due_date_source),
                             (D(12), D(13), SYSTEM))
        for code in (RMS, GEN_METER):
            task = self._task(site, code)
            self.assertEqual((task.schedule_start, task.due_date, task.due_date_source),
                             (D(15), D(15), SYSTEM), code)
        testing = self._task(site, TESTING)
        self.assertEqual((testing.schedule_start, testing.due_date), (D(16), D(16)))
        net_meter = self._task(site, NET_METER_INSTALLATION)
        self.assertEqual((net_meter.schedule_start, net_meter.due_date, net_meter.due_date_source),
                         (D(16), D(16), SYSTEM))
        self.assertIsNone(self._task(site, NET_METERING).due_date,
                          'Net Metering Approval is in no block')
        self.assertEqual(self._logs(site), [
            'Auto-scheduled due dates: anchor Civil due 2026-11-10, band <=50 kWp; '
            '10 written, 1 start set, 0 cleared'])


# ---------------------------------------------------------------------------
# 2. Rules (verification bar 3)
# ---------------------------------------------------------------------------

class RuleTests(SchedFixture):

    def setUp(self):
        self.site = self._site('40.00', civil_due=D(10))

    def _actions(self, result):
        return {tp.task.pk: tp.action for bp in result.blocks for tp in bp.tasks}

    def test_the_civil_date_is_the_anchor_whatever_its_source_and_is_never_written(self):
        civil = self._task(self.site, CIVIL)
        Task.objects.filter(pk=civil.pk).update(due_date_source=SYSTEM)
        result = self._apply(self.site)
        civil.refresh_from_db()
        self.assertEqual((civil.due_date, civil.due_date_source), (D(10), SYSTEM))
        self.assertEqual(self._actions(result)[civil.pk], ACTION_WRITE_START)
        self.assertEqual(result.blocks[1].start, D(11))

    def test_a_done_civil_keeps_its_start_unwritten(self):
        civil = self._task(self.site, CIVIL)
        self._done(civil, self._ist(9))
        result = self._apply(self.site)
        civil.refresh_from_db()
        self.assertEqual(self._actions(result)[civil.pk], SKIP_DONE)
        self.assertIsNone(civil.schedule_start)

    def test_a_manual_date_is_never_overwritten(self):
        module = self._task(self.site, MODULE)
        self._set_due(module, D(30))
        result = self._apply(self.site)
        module.refresh_from_db()
        self.assertEqual((module.due_date, module.due_date_source, module.schedule_start),
                         (D(30), MANUAL, None))
        self.assertEqual(self._actions(result)[module.pk], SKIP_MANUAL)

    def test_a_null_source_date_is_never_overwritten(self):
        """Ruling D: the demo seed writes a date with no source; untouchable."""
        module = self._task(self.site, MODULE)
        Task.objects.filter(pk=module.pk).update(due_date=D(29), due_date_source=None)
        self._apply(self.site)
        module.refresh_from_db()
        self.assertEqual((module.due_date, module.due_date_source), (D(29), None))

    def test_done_and_not_applicable_tasks_are_never_touched(self):
        la, dc_cable = self._task(self.site, LA), self._task(self.site, DC_CABLE)
        self._done(la, self._ist(11))
        Task.objects.filter(pk=dc_cable.pk).update(is_not_applicable=True)
        result = self._apply(self.site)
        actions = self._actions(result)
        self.assertEqual((actions[la.pk], actions[dc_cable.pk]), (SKIP_DONE, SKIP_NA))
        for task in (la, dc_cable):
            task.refresh_from_db()
            self.assertEqual((task.due_date, task.due_date_source, task.schedule_start),
                             (None, None, None))

    def test_location_copies_and_mirrors_are_skipped(self):
        module = self._task(self.site, MODULE)
        copy = Task.objects.create(
            phase=module.phase, task_name=f'{module.task_name} — Block A',
            task_order=module.task_order, assigned_role=module.assigned_role,
            template_task=module.template_task, location_label='Block A')
        # A block task flagged as a mirror: mirrors are never written, block or not.
        testing = self._task(self.site, TESTING)
        Task.objects.filter(pk=testing.pk).update(is_mirror=True)
        self._apply(self.site)
        copy.refresh_from_db()
        testing.refresh_from_db()
        self.assertIsNone(copy.due_date)
        self.assertIsNone(testing.due_date)
        module.refresh_from_db()
        self.assertEqual(module.due_date_source, SYSTEM, 'the unlabelled task is scheduled')

    def test_a_late_civil_re_anchors_blocks_2_to_8(self):
        """Civil due 10 Nov, Done 13 Nov: block 2 restarts on 14 Nov, C = 0."""
        self._done(self._task(self.site, CIVIL), self._ist(13))
        result = self._plan(self.site)
        self.assertEqual(result.blocks[0].re_anchored_after, D(13))
        self.assertEqual(self._dates(result)[1:], [
            (D(14), D(15)), (D(15), D(16)), (D(17), D(17)), (D(18), D(18)), (D(18), D(18)),
            (D(19), D(19)), (D(19), D(19)),
        ])

    def test_the_done_date_is_the_local_ist_date(self):
        """20:00 UTC on 15 Nov is 01:30 IST on 16 Nov: block 2 restarts on the 17th."""
        utc_evening = datetime(2026, 11, 15, 20, 0, tzinfo=dt_timezone.utc)
        self._done(self._task(self.site, CIVIL), utc_evening)
        result = self._plan(self.site)
        self.assertEqual(result.blocks[0].re_anchored_after, D(16))
        self.assertEqual(result.blocks[1].start, D(17))

    def test_an_early_civil_finish_does_not_pull_dates_earlier(self):
        planned = self._dates(self._plan(self.site))
        self._done(self._task(self.site, CIVIL), self._ist(8))
        result = self._plan(self.site)
        self.assertIsNone(result.blocks[0].re_anchored_after)
        self.assertEqual(self._dates(result)[1:], planned[1:])

    def test_a_late_block_2_re_anchors_blocks_3_to_8(self):
        """Modules due 12 Nov, Done 14 Nov: block 3 restarts on 15 Nov, C = 0."""
        self._done(self._task(self.site, MODULE), self._ist(14))
        result = self._plan(self.site)
        self.assertEqual(result.blocks[1].re_anchored_after, D(14))
        self.assertEqual(self._dates(result)[2:], [
            (D(15), D(16)), (D(16), D(17)), (D(17), D(17)), (D(18), D(18)), (D(18), D(18)),
            (D(19), D(19)),
        ])

    def test_an_all_not_applicable_block_takes_zero_days(self):
        """Block 3 all N/A: C after block 2 is 3 half days, so block 4 starts on day 2
        (12 Nov) and is due day 3 (13 Nov); blocks 5 to 8 follow."""
        Task.objects.filter(pk__in=[self._task(self.site, LA).pk,
                                    self._task(self.site, DC_CABLE).pk]).update(
            is_not_applicable=True)
        result = self._plan(self.site)
        self.assertEqual(result.blocks[2].half_days, 0)
        self.assertEqual(self._dates(result)[1:], [
            (D(11), D(12)), (None, None), (D(12), D(13)), (D(13), D(13)), (D(14), D(14)),
            (D(14), D(14)), (D(15), D(15)),
        ])

    def test_removing_capacity_clears_only_system_dates_and_the_civil_start(self):
        self._apply(self.site)
        module, inverter = self._task(self.site, MODULE), self._task(self.site, INVERTER)
        self._set_due(module, D(30))
        self._done(self._task(self.site, LA), self._ist(12))
        Project.objects.filter(pk=self.site.pk).update(dc_capacity_kw=None)

        result = self._apply(self.site)
        self.assertEqual(result.reason, REASON_NEEDS_CAPACITY)
        self.assertEqual(result.cleared, 9, '10 system dates less the manual and the Done, '
                                            'plus the Civil start')
        inverter.refresh_from_db()
        self.assertEqual((inverter.due_date, inverter.due_date_source, inverter.schedule_start),
                         (None, None, None))
        module.refresh_from_db()
        self.assertEqual((module.due_date, module.due_date_source), (D(30), MANUAL))
        la = self._task(self.site, LA)
        self.assertEqual((la.due_date, la.due_date_source), (D(13), SYSTEM),
                         'a Done task keeps its date')
        civil = self._task(self.site, CIVIL)
        self.assertEqual((civil.due_date, civil.schedule_start), (D(10), None))
        self.assertEqual(self._logs(self.site)[-1],
                         'Auto-schedule cleared system due dates (needs_capacity); '
                         '0 written, 0 start set, 9 cleared')

    def test_clearing_the_civil_date_clears_the_sites_system_dates(self):
        self._apply(self.site)
        self._set_due(self._task(self.site, CIVIL), None)
        result = self._apply(self.site)
        self.assertEqual((result.reason, result.cleared), (REASON_NEEDS_ANCHOR, LATER_TASKS + 1))
        self.assertFalse(Task.objects.filter(phase__project=self.site,
                                             due_date_source=SYSTEM).exists())

    def test_a_not_applicable_civil_clears_system_dates_but_not_its_own_start(self):
        self._apply(self.site)
        civil = self._task(self.site, CIVIL)
        Task.objects.filter(pk=civil.pk).update(is_not_applicable=True)
        result = self._apply(self.site)
        self.assertEqual((result.reason, result.cleared), (REASON_NEEDS_ANCHOR, LATER_TASKS))
        civil.refresh_from_db()
        self.assertEqual(civil.schedule_start, D(7), 'an N/A task is never touched')

    def test_a_second_run_with_no_change_writes_and_logs_nothing(self):
        self._apply(self.site)
        with patch.object(Task, 'save') as save:
            result = self._apply(self.site)
        save.assert_not_called()
        self.assertEqual((result.changes, result.written, result.started, result.cleared),
                         (0, 0, 0, 0))
        self.assertTrue(all(tp.action == ACTION_UNCHANGED
                            for bp in result.blocks for tp in bp.tasks))
        self.assertEqual(len(self._logs(self.site)), 1, 'only the first run logs')

    def test_excluding_a_site_clears_its_system_dates_and_writes_nothing(self):
        """Ruling a, as amended: turning the flag on clears the site's system dates."""
        self._apply(self.site)
        Project.objects.filter(pk=self.site.pk).update(exclude_from_auto_schedule=True)
        result = self._apply(self.site)
        self.assertEqual(result.reason, REASON_EXCLUDED)
        self.assertEqual((result.written, result.started, result.cleared),
                         (0, 0, LATER_TASKS + 1))
        self.assertFalse(Task.objects.filter(phase__project=self.site,
                                             due_date_source=SYSTEM).exists())
        civil = self._task(self.site, CIVIL)
        self.assertEqual((civil.due_date, civil.schedule_start), (D(10), None))
        again = self._apply(self.site)
        self.assertEqual(again.changes, 0)
        self.assertEqual(self._logs(self.site)[-1],
                         'Auto-schedule cleared system due dates (excluded); '
                         '0 written, 0 start set, 11 cleared')
        self.assertEqual(len(self._logs(self.site)), 2, 'the excluded re-run logs nothing')

    def test_ineligible_sites_are_not_scheduled(self):
        for fields in ({'status': 'On Hold'}, {'is_test': True}):
            site = self._site('40.00', civil_due=D(10), **fields)
            result = self._apply(site)
            self.assertEqual(result.reason, REASON_NOT_ELIGIBLE, fields)
            self.assertFalse(Task.objects.filter(phase__project=site,
                                                 due_date_source=SYSTEM).exists())


# ---------------------------------------------------------------------------
# 3. Overdue preview (verification bar 5)
# ---------------------------------------------------------------------------

class OverduePreviewTests(SchedFixture):

    def test_the_preview_count_matches_the_shared_rule_after_applying(self):
        """Civil due and Done on 10 Oct, Modules Done on time on 12 Oct: Earthing & DC
        (planned 12 -> 13 Oct) is open past due, and its LA task already carries those
        system dates. Since SCHED-2b only that block goes red: its undated DC cable task
        gets the block's dates (newly overdue), and the blocks after it are forecast from
        10 Nov. (Before SCHED-2b an open Civil 30 days late made every later block newly
        overdue; a block never system-dated is now started after today instead, so the
        late block here has to be one that already carries system dates.)"""
        today = timezone.localdate()
        self.assertEqual(today, FROZEN_TODAY)
        site = self._site('40.00', civil_due=D(10, 10))
        self._done(self._task(site, CIVIL), self._ist(10, month=10))
        # Noise either side of the rule: a manual overdue task outside the blocks
        # (already overdue), a mirror with a past date (counted by neither), a Done
        # block task (never overdue), and an N/A one (never overdue).
        self._set_due(self._task(site, NET_METERING), today - timedelta(days=2))
        Task.objects.filter(pk=self._task(site, COD).pk).update(
            due_date=today - timedelta(days=5), due_date_source=MANUAL)
        self._done(self._task(site, MODULE), self._ist(12, month=10))
        Task.objects.filter(pk=self._task(site, LA).pk).update(
            due_date=D(13, 10), due_date_source=SYSTEM, schedule_start=D(12, 10))
        Task.objects.filter(pk=self._task(site, TESTING).pk).update(is_not_applicable=True)

        def shared_count():
            return (Task.objects.filter(phase__project=site)
                    .filter(human_owned_tasks_q()).filter(overdue_q(today)).count())

        before = shared_count()
        preview = self._plan(site)
        self._apply(site)
        after = shared_count()
        self.assertEqual(before, 2, 'Net Metering Approval and the system-dated LA task')
        self.assertEqual(preview.overdue_after, after)
        self.assertEqual(preview.overdue_already, before)
        self.assertEqual(preview.overdue_new, after - before)
        self.assertEqual(preview.overdue_new, 1, 'the DC cable task of the late block, only')


# ---------------------------------------------------------------------------
# 4. Triggers (verification bar 4)
# ---------------------------------------------------------------------------

class TriggerFixture(SchedFixture):
    """A site with no Civil date yet; Civil and Modules held by the site engineer."""

    def setUp(self):
        self.site = self._site('40.00')
        self.civil = self._task(self.site, CIVIL)
        assign_task_to(self.civil, self.se, notify=False)
        self.module = self._task(self.site, MODULE)
        assign_task_to(self.module, self.se, notify=False)

    def _start(self, task, profile=None, with_date=True):
        data = {'status': Task.IN_PROGRESS}
        if with_date:
            data['due_date'] = (timezone.localdate() + timedelta(days=7)).isoformat()
        return _client_for(profile or self.pm).post(
            reverse('task_status_update', args=[self.site.project_id, task.pk]), data)

    def _edit_date(self, task, value, profile=None):
        return _client_for(profile or self.pm).post(
            reverse('task_set_due_date', args=[self.site.project_id, task.pk]),
            {'due_date': value})

    def _queued(self, target, post):
        """Run `post()` with the scheduler call patched at `target`. Asserts nothing ran
        before commit, then runs the commit callbacks; returns the mock."""
        with patch(target) as mock:
            with self.captureOnCommitCallbacks(execute=False) as callbacks:
                post()
            mock.assert_not_called()   # never inside the request's transaction
            for callback in callbacks:
                callback()
        return mock

    def assertScheduledOnce(self, mock):
        self.assertEqual(mock.call_count, 1, 'expected exactly one reschedule after commit')
        self.assertEqual(mock.call_args.args[0].pk, self.site.pk)


class CivilDateTriggerTests(TriggerFixture):

    def test_the_pm_setting_the_civil_date_schedules_after_commit(self):
        mock = self._queued('projects.views.schedule_site_safely',
                            lambda: self._edit_date(self.civil, '2026-11-10'))
        self.civil.refresh_from_db()
        self.assertEqual((self.civil.due_date, self.civil.due_date_source), (D(10), MANUAL))
        self.assertScheduledOnce(mock)

    def test_the_role_owner_setting_the_civil_date_schedules_after_commit(self):
        mock = self._queued('projects.views.schedule_site_safely',
                            lambda: self._edit_date(self.civil, '2026-11-10', profile=self.se))
        self.civil.refresh_from_db()
        self.assertEqual(self.civil.due_date, D(10))
        self.assertScheduledOnce(mock)

    def test_moving_and_clearing_the_civil_date_both_schedule(self):
        self._set_due(self.civil, D(10))
        for value in ('2026-11-12', ''):
            mock = self._queued('projects.views.schedule_site_safely',
                                lambda: self._edit_date(self.civil, value))
            self.assertScheduledOnce(mock)
        self.civil.refresh_from_db()
        self.assertIsNone(self.civil.due_date)

    def test_saving_the_same_civil_date_again_does_not_schedule(self):
        self._set_due(self.civil, D(10))
        mock = self._queued('projects.views.schedule_site_safely',
                            lambda: self._edit_date(self.civil, '2026-11-10'))
        mock.assert_not_called()

    def test_a_date_on_another_task_does_not_schedule(self):
        mock = self._queued('projects.views.schedule_site_safely',
                            lambda: self._edit_date(self.module, '2026-11-20'))
        mock.assert_not_called()

    def test_the_civil_date_end_to_end_writes_the_schedule(self):
        with self.captureOnCommitCallbacks(execute=True):
            self._edit_date(self.civil, '2026-11-10')
        self.assertEqual(self._logs(self.site), [
            'Auto-scheduled due dates: anchor Civil due 2026-11-10, band <=50 kWp; '
            '10 written, 1 start set, 0 cleared'])
        self.assertEqual(self._task(self.site, MODULE).due_date, D(12))
        with self.captureOnCommitCallbacks(execute=True):
            self._edit_date(self.civil, '')
        self.assertFalse(Task.objects.filter(phase__project=self.site,
                                             due_date_source=SYSTEM).exists())

    def test_the_in_progress_date_on_civil_schedules_after_commit(self):
        mock = self._queued('projects.views.schedule_site_safely',
                            lambda: self._start(self.civil))
        self.civil.refresh_from_db()
        self.assertEqual(self.civil.status, Task.IN_PROGRESS)
        self.assertEqual(self.civil.due_date_source, MANUAL)
        self.assertScheduledOnce(mock)

    def test_starting_civil_that_already_has_a_date_does_not_schedule(self):
        """The anchor is the Civil date, not its start."""
        self._set_due(self.civil, D(10))
        mock = self._queued('projects.views.schedule_site_safely',
                            lambda: self._start(self.civil, with_date=False))
        self.civil.refresh_from_db()
        self.assertEqual(self.civil.status, Task.IN_PROGRESS)
        mock.assert_not_called()

    def test_a_scheduler_failure_keeps_the_date_and_is_logged(self):
        with patch('projects.scheduling.schedule_site', side_effect=RuntimeError('boom')):
            with self.assertLogs('projects.scheduling', level='ERROR') as logs:
                with self.captureOnCommitCallbacks(execute=True):
                    response = self._edit_date(self.civil, '2026-11-10')
        self.assertIn(response.status_code, (200, 302))
        self.civil.refresh_from_db()
        self.assertEqual(self.civil.due_date, D(10))
        self.assertTrue(any(f'Auto-schedule failed for site {self.site.project_id}' in line
                            for line in logs.output), logs.output)

    def test_a_scheduler_failure_keeps_the_status_change(self):
        with patch('projects.scheduling.schedule_site', side_effect=RuntimeError('boom')):
            with self.assertLogs('projects.scheduling', level='ERROR'):
                with self.captureOnCommitCallbacks(execute=True):
                    self._start(self.civil)
        self.civil.refresh_from_db()
        self.assertEqual(self.civil.status, Task.IN_PROGRESS)


class StatusTriggerTests(TriggerFixture):

    def test_a_block_task_starting_does_not_reschedule(self):
        self._set_due(self.module, D(20))
        mock = self._queued('projects.views.schedule_site_safely',
                            lambda: self._start(self.module, with_date=False))
        mock.assert_not_called()

    def test_self_certified_completion_schedules_after_commit(self):
        self._start(self.civil)
        mock = self._queued('projects.views.schedule_site_safely', lambda: _client_for(
            self.pm).post(reverse('task_submit_for_approval',
                                  args=[self.site.project_id, self.civil.pk]),
                          {'submission_remarks': 'Civil complete'}))
        self.civil.refresh_from_db()
        self.assertEqual(self.civil.status, Task.DONE)
        self.assertTrue(self.civil.approval_self_certified, 'the self-certified branch ran')
        self.assertScheduledOnce(mock)

    def test_reopening_a_done_task_schedules_after_commit(self):
        """Ruling b: Done -> Blocked from the task-detail screen, as its assignee."""
        self._done(self.civil, timezone.now())
        mock = self._queued('projects.views.schedule_site_safely', lambda: _client_for(
            self.se).post(reverse('task_detail_status_update',
                                  args=[self.site.project_id, self.civil.pk]),
                          {'status': Task.BLOCKED, 'block_issue_title': 'Footing cracked'}))
        self.civil.refresh_from_db()
        self.assertEqual(self.civil.status, Task.BLOCKED)
        self.assertScheduledOnce(mock)

    def test_marking_and_unmarking_not_applicable_both_schedule(self):
        url = reverse('task_set_not_applicable', args=[self.site.project_id, self.module.pk])
        for flag in ('1', '0'):
            mock = self._queued('projects.views.schedule_site_safely', lambda: _client_for(
                self.pm).post(url, {'not_applicable': flag, 'not_applicable_reason': 'scope'}))
            self.module.refresh_from_db()
            self.assertEqual(self.module.is_not_applicable, flag == '1')
            self.assertScheduledOnce(mock)

    def test_not_applicable_on_a_task_in_no_block_does_not_schedule(self):
        # Net Metering Approval: since SCHED-2c every Site Engineer task of the
        # template is in a block, so the task in no block is a PM one.
        net_metering = self._task(self.site, NET_METERING)
        url = reverse('task_set_not_applicable', args=[self.site.project_id, net_metering.pk])
        mock = self._queued('projects.views.schedule_site_safely', lambda: _client_for(
            self.pm).post(url, {'not_applicable': '1', 'not_applicable_reason': 'scope'}))
        mock.assert_not_called()

    def test_a_dc_capacity_edit_schedules_after_commit(self):
        url = reverse('project_field_edit', args=[self.site.project_id])
        mock = self._queued('projects.views.schedule_site_safely', lambda: _client_for(
            self.pm).post(url, {'dc_capacity_kw': '120.00', 'reason': 'as built'}))
        self.site.refresh_from_db()
        self.assertEqual(self.site.dc_capacity_kw, Decimal('120.00'))
        self.assertScheduledOnce(mock)

    def test_an_edit_that_leaves_capacity_alone_does_not_schedule(self):
        url = reverse('project_field_edit', args=[self.site.project_id])
        mock = self._queued('projects.views.schedule_site_safely', lambda: _client_for(
            self.pm).post(url, {'dc_capacity_kw': '40.00',
                                'target_commissioning_date': '2027-01-15'}))
        mock.assert_not_called()

    def test_the_trigger_checks_never_raise(self):
        with patch('projects.scheduling.ScheduleBlockTask.objects') as manager:
            manager.filter.side_effect = DatabaseError('down')
            with self.assertLogs('projects.scheduling', level='ERROR'):
                self.assertFalse(task_is_scheduled(self.module))
                self.assertFalse(task_is_schedule_anchor(self.civil))
                self.assertFalse(status_change_reschedules(self.civil, Task.IN_PROGRESS,
                                                           Task.DONE))


class ApprovalTriggerTests(TriggerFixture):
    """task_approve: a second signature exists, so the PM's submission queues."""

    def setUp(self):
        super().setUp()
        self.qaqc = _profile('s2_qaqc', 'Site Engineer', is_qaqc=True)
        # Visibility of the site for the approver: a Site Engineer sees a project by
        # holding a task on it (see tests_two_step_completion.TwoStepFixture).
        assign_task_to(self._task(self.site, RMS), self.qaqc, notify=False)

    def test_approval_completion_schedules_after_commit(self):
        self._start(self.civil)
        _client_for(self.pm).post(
            reverse('task_submit_for_approval', args=[self.site.project_id, self.civil.pk]),
            {'submission_remarks': 'Civil complete'})
        self.civil.refresh_from_db()
        self.assertNotEqual(self.civil.status, Task.DONE, 'the submission must queue')
        mock = self._queued('projects.views.schedule_site_safely', lambda: _client_for(
            self.qaqc).post(reverse('task_approve', args=[self.site.project_id, self.civil.pk]),
                            {'approval_remarks': 'Inspected'}))
        self.civil.refresh_from_db()
        self.assertEqual(self.civil.status, Task.DONE)
        self.assertScheduledOnce(mock)


class AdminTriggerTests(TriggerFixture):
    """TaskAdmin (Civil date, N/A) and ProjectAdmin (capacity, exclusion), through real
    admin POSTs."""

    def setUp(self):
        super().setUp()
        self.superuser = User.objects.create_superuser('s2_su', 'su@example.com', 'pw12345')
        # AdminAccessMiddleware lets only an Admin-role profile reach /admin/.
        self.superuser.profile.role = 'Admin'
        self.superuser.profile.save(update_fields=['role'])
        self.admin_client = Client()
        self.admin_client.force_login(self.superuser)

    def _request(self):
        request = RequestFactory().get('/')
        request.user = self.superuser
        return request

    def _task_post(self, task, **changes):
        task = Task.objects.get(pk=task.pk)
        model_admin = django_admin.site._registry[Task]
        form = model_admin.get_form(self._request(), task, change=True)(instance=task)
        data = _admin_post_data(form)
        data.update(changes)
        response = self.admin_client.post(
            reverse('admin:projects_task_change', args=[task.pk]), data)
        self.assertEqual(response.status_code, 302, getattr(response, 'context_data', {})
                         and response.context_data.get('errors'))
        return response

    def _project_post(self, **changes):
        """The change form's own values, every inline posted empty (no rows touched),
        and `changes` on top."""
        site = Project.objects.get(pk=self.site.pk)
        model_admin = django_admin.site._registry[Project]
        request = self._request()
        form = model_admin.get_form(request, site, change=True)(instance=site)
        data = _admin_post_data(form)
        for formset, _inline in model_admin.get_formsets_with_inlines(request, site):
            prefix = formset.get_default_prefix()
            data.update({f'{prefix}-TOTAL_FORMS': '0', f'{prefix}-INITIAL_FORMS': '0'})
        data.update(changes)
        response = self.admin_client.post(
            reverse('admin:projects_project_change', args=[site.pk]), data)
        self.assertEqual(response.status_code, 302, getattr(response, 'context_data', {})
                         and response.context_data.get('errors'))
        return response

    def test_task_admin_civil_date_change_schedules_after_commit(self):
        mock = self._queued('projects.admin.schedule_site_safely',
                            lambda: self._task_post(self.civil, due_date='2026-11-10'))
        self.civil.refresh_from_db()
        self.assertEqual((self.civil.due_date, self.civil.due_date_source), (D(10), MANUAL))
        self.assertScheduledOnce(mock)

    def test_task_admin_date_on_another_task_does_not_schedule(self):
        mock = self._queued('projects.admin.schedule_site_safely',
                            lambda: self._task_post(self.module, due_date='2026-11-20'))
        mock.assert_not_called()

    def test_task_admin_not_applicable_change_schedules_after_commit(self):
        mock = self._queued('projects.admin.schedule_site_safely',
                            lambda: self._task_post(self.module, is_not_applicable='on'))
        self.module.refresh_from_db()
        self.assertTrue(self.module.is_not_applicable)
        self.assertScheduledOnce(mock)

    def test_task_admin_edit_without_a_trigger_change_does_not_schedule(self):
        mock = self._queued('projects.admin.schedule_site_safely',
                            lambda: self._task_post(self.module, duration_days='3'))
        mock.assert_not_called()

    def test_project_admin_capacity_change_schedules(self):
        mock = self._queued('projects.admin.schedule_site_safely',
                            lambda: self._project_post(dc_capacity_kw='75.00'))
        self.assertScheduledOnce(mock)

    def test_project_admin_exclusion_is_logged_and_schedules(self):
        mock = self._queued('projects.admin.schedule_site_safely',
                            lambda: self._project_post(exclude_from_auto_schedule='on'))
        self.assertScheduledOnce(mock)
        self.assertEqual(list(ActivityLog.objects.filter(
            project=self.site, action_code='auto_schedule_exclusion_changed')
            .values_list('action', flat=True)),
            ['Excluded site from auto-scheduling (Django admin)'])

    def test_project_admin_unrelated_edit_does_not_schedule(self):
        mock = self._queued('projects.admin.schedule_site_safely',
                            lambda: self._project_post(city='Kanpur'))
        mock.assert_not_called()

    def test_the_project_has_no_planned_mobilization_fields(self):
        names = {f.name for f in Project._meta.get_fields()}
        self.assertFalse({n for n in names if n.startswith('planned_mobilization')})

    def test_the_working_day_switch_is_read_only_in_django_admin(self):
        model_admin = django_admin.site._registry[SystemSettings]
        self.assertIn('schedule_skip_sundays', model_admin.readonly_fields)


# ---------------------------------------------------------------------------
# 5. The Admin working-day switch (task 6)
# ---------------------------------------------------------------------------

class WorkingDaySwitchTests(SchedFixture):

    def setUp(self):
        self.admin = _profile('s2_admin', 'Admin')
        self.site = self._site('40.00', civil_due=D(10))
        self._apply(self.site)   # calendar days

    def _post(self, action, skip, profile=None):
        return _client_for(profile or self.admin).post(
            reverse('admin_master_switches'),
            {'schedule_action': action, 'schedule_skip_sundays': '1' if skip else '0'})

    def test_the_preview_counts_and_changes_nothing(self):
        """Skipping Sundays moves the Civil start (7 -> 6 Nov), AC cable and both tasks of
        RMS & Gen meter (Sun 15 -> Mon 16 Nov), and Testing and Net meter (16 -> 17 Nov):
        six dates."""
        response = self._post('preview', True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'This will recalculate 6 system-set dates on 1 site.')
        self.assertFalse(SystemSettings.get().schedule_skip_sundays)
        self.assertEqual(self._task(self.site, TESTING).due_date, D(16))

    def test_confirm_saves_the_setting_and_recalculates(self):
        response = self._post('confirm', True)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(SystemSettings.get().schedule_skip_sundays)
        self.assertEqual(self._task(self.site, TESTING).due_date, D(17))
        self.assertEqual(self._task(self.site, RMS).due_date, D(16))
        self.assertEqual(self._task(self.site, CIVIL).schedule_start, D(6))
        self.assertEqual(len(self._logs(self.site)), 2)
        messages = [str(m) for m in get_messages(response.wsgi_request)]
        self.assertIn('Auto-schedule now uses working days (Sundays skipped). '
                      'Recalculated 6 system-set dates on 1 site.', messages)
        # Back again: calendar days, the original dates.
        self._post('confirm', False)
        self.assertFalse(SystemSettings.get().schedule_skip_sundays)
        self.assertEqual(self._task(self.site, TESTING).due_date, D(16))

    def test_the_main_save_does_not_touch_the_switch(self):
        _client_for(self.admin).post(reverse('admin_master_switches'),
                                     {'schedule_skip_sundays': 'on'})
        self.assertFalse(SystemSettings.get().schedule_skip_sundays)

    def test_only_admin_may_flip_the_switch(self):
        response = self._post('confirm', True, profile=self.pm)
        self.assertNotEqual(response.status_code, 200)
        self.assertFalse(SystemSettings.get().schedule_skip_sundays)
        self.assertEqual(self._task(self.site, TESTING).due_date, D(16))


# ---------------------------------------------------------------------------
# 6. The table itself
# ---------------------------------------------------------------------------

class ScheduleBlockModelTests(SchedFixture):

    def test_the_seed_matches_the_pms_table(self):
        rows = [(b.sequence, b.name, str(b.days_le_50), str(b.days_51_100),
                 str(b.days_101_plus),
                 sorted(bt.template_task.code for bt in b.block_tasks.all()))
                for b in ScheduleBlock.objects.filter(template__code='OPEX')
                .prefetch_related('block_tasks__template_task').order_by('sequence')]
        self.assertEqual(rows, [
            (1, 'Civil & MMS', '3.5', '7.0', '11.0', [CIVIL]),
            (2, 'Modules', '1.5', '4.0', '5.0', [MODULE]),
            (3, 'Earthing & DC', '1.5', '2.5', '5.0', [DC_CABLE, LA]),
            (4, 'Inverter & DBs', '1.0', '1.0', '1.0',
             ['DCDB_AND_ACDB_INSTALLATION', INVERTER]),
            (5, 'AC cable', '0.5', '0.5', '2.0', ['AC_CABLE_LAYING']),
            (6, 'RMS & Gen meter', '0.5', '0.5', '0.5', [RMS, GEN_METER]),
            (7, 'Testing', '0.5', '1.0', '1.0', [TESTING]),
            (8, 'Net meter', '0.5', '1.0', '1.0', [NET_METER_INSTALLATION]),
        ])

    def test_the_meter_migration_refuses_a_second_run_and_reverses_cleanly(self):
        """0139 fails loudly when Testing is no longer at 6 (already applied), and its
        reverse restores the six-block table exactly."""
        migration = import_module('projects.migrations.0139_sched2c_meter_blocks')
        with self.assertRaisesMessage(RuntimeError, 'Expected the Testing block'):
            migration.add_meter_blocks(_Apps(), None)
        migration.remove_meter_blocks(_Apps(), None)
        self.assertEqual(
            list(ScheduleBlock.objects.filter(template__code='OPEX').order_by('sequence')
                 .values_list('sequence', 'name')),
            [(1, 'Civil & MMS'), (2, 'Modules'), (3, 'Earthing & DC'), (4, 'Inverter & DBs'),
             (5, 'AC cable'), (6, 'Testing')])
        self.assertFalse(ScheduleBlockTask.objects.filter(
            template_task__code__in=[RMS, GEN_METER, NET_METER_INSTALLATION]).exists())

    def test_a_length_that_is_not_a_half_day_multiple_is_refused(self):
        from django.core.exceptions import ValidationError
        from django.db import IntegrityError, transaction
        block = ScheduleBlock.objects.get(template__code='OPEX', sequence=1)
        block.days_le_50 = Decimal('0.3')
        with self.assertRaises(ValidationError):
            block.clean()
        with self.assertRaises(IntegrityError), transaction.atomic():
            ScheduleBlock.objects.filter(pk=block.pk).update(days_le_50=Decimal('0.3'))
        with self.assertRaises(IntegrityError), transaction.atomic():
            ScheduleBlock.objects.filter(pk=block.pk).update(days_51_100=Decimal('-0.5'))

    def test_a_task_from_another_template_is_refused_by_clean(self):
        from django.core.exceptions import ValidationError
        block = ScheduleBlock.objects.get(template__code='OPEX', sequence=6)
        other = TaskTemplate.objects.create(code='OTHER', label='Other', project_type='CAPEX')
        phase = TaskTemplatePhase.objects.create(template=other, code='P', label='P', sort_order=1)
        foreign = TaskTemplateTask.objects.create(phase=phase, code='X', label='X', sort_order=1)
        with self.assertRaises(ValidationError):
            ScheduleBlockTask(block=block, template_task=foreign).clean()

    def test_deleting_the_template_takes_its_blocks_with_it(self):
        """The 0075 reverse deletes the OPEX template; its blocks must not block it
        (CASCADE on the template, RESTRICT on the task link)."""
        TaskTemplate.objects.get(code='OPEX', version_no=1).delete()
        self.assertFalse(ScheduleBlock.objects.filter(template__code='OPEX').exists())
        self.assertFalse(ScheduleBlockTask.objects.exists())

    def test_a_scheduled_template_task_cannot_be_deleted_on_its_own(self):
        from django.db.models.deletion import RestrictedError
        civil = TaskTemplateTask.objects.filter(phase__template__code='OPEX', code=CIVIL)
        with self.assertRaises(RestrictedError):
            civil.delete()   # the queryset delete: TaskTemplateTask.delete() adds R-7 on top

    def test_a_task_is_in_at_most_one_block(self):
        from django.db import IntegrityError, transaction
        civil = TaskTemplateTask.objects.get(phase__template__code='OPEX', code=CIVIL)
        block_2 = ScheduleBlock.objects.get(template__code='OPEX', sequence=2)
        with self.assertRaises(IntegrityError), transaction.atomic():
            ScheduleBlockTask.objects.create(block=block_2, template_task=civil)
