"""
SCHED-2e — the baseline (Task.baseline_start / baseline_due) and the site's slip.

WHAT THIS PINS (spec SCHED_Auto_Scheduling_Spec.md "Baseline" and "Baseline amendment",
9 Nov 2026):

    1  Pure plan   the baseline is the pure plan: the Civil due date and the chain
                   arithmetic, no forecast / late-finish / first-dating shift; for an
                   on-time site the pure plan and the real plan are identical
    2  Snapshot    the first scheduling run sets it; before Civil starts every run
                   re-snapshots it (Civil date edit, capacity edit, working-day switch);
                   a forecast-only run changes nothing (three frozen days: baseline held,
                   1, 2, 3 days behind)
    3  Freeze      once Civil starts (or ever started: Blocked after In Progress) the
                   forecast, a late Civil Done, a Civil date edit, a capacity edit and the
                   working-day switch move due dates but never the baseline; a task with
                   no baseline yet still gets its own
    4  Re-plan     rebaseline_site: note required, dry run by default, apply writes and
                   logs one baseline_reset row with no actor
    5  Summary     site_plan_summary(): 5 days behind for a crafted case, 0 on plan; the
                   overview line and the "plan:" row note render only for scheduled sites
    6  Backfill    migration 0141 writes the pure plan (not today's moved dates) on sites
                   with system dates only, and its reverse clears every baseline

Fixtures and frozen "today" (9 Nov 2026) come from tests_sched2_scheduler.SchedFixture.
The Civil due date is Tue 10 Nov; in the <=50 band and calendar days the plan ends with
Testing and Net meter on 16 Nov.

Run with:
    python manage.py test projects.tests_sched2e_baseline --settings=solarpms.test_settings
"""
from decimal import Decimal
from importlib import import_module
from io import StringIO
from types import SimpleNamespace

from django.core.management import CommandError, call_command
from django.urls import reverse

from .admin import TaskAdmin
from .models import (ActivityLog, Project, ScheduleBlock, ScheduleBlockTask, SystemSettings,
                     Task, TaskTemplate, TaskTemplatePhase, TaskTemplateTask)
from .scheduling import (BASELINE_RESET_ACTION_CODE, _plan, civil_date_has_passed,
                         civil_has_started, site_plan_summary)
from .tests_sched2_scheduler import (CIVIL, MODULE, NET_METER_INSTALLATION, TESTING, D,
                                     SchedFixture, freeze_today)
from .tests_two_step_completion import _client_for, _profile
from .utils import assign_task_to

SYSTEM = Task.DUE_DATE_SYSTEM


class BaselineFixture(SchedFixture):

    def _in_block(self, site):
        """Every in-block task of the site, fresh from the database, in plan order."""
        pure = _plan(Project.objects.get(pk=site.pk), False, D(9), pure=True)
        return pure.in_block

    def _baselines(self, site):
        return {t.pk: (t.baseline_start, t.baseline_due) for t in self._in_block(site)}

    def _due_dates(self, site):
        return {t.pk: t.due_date for t in self._in_block(site)}

    def _start_civil(self, site):
        """Civil In Progress through the real view, so the StatusTransition row is real."""
        civil = self._task(site, CIVIL)
        assign_task_to(civil, self.se, notify=False)    # a status change needs an assignee
        response = _client_for(self.pm).post(
            reverse('task_status_update', args=[site.project_id, civil.pk]),
            {'status': Task.IN_PROGRESS})
        self.assertIn(response.status_code, (200, 302))
        self.assertEqual(Task.objects.get(pk=civil.pk).status, Task.IN_PROGRESS)
        return civil

    def _apply_on(self, day, site, **kwargs):
        with freeze_today(day):
            return self._apply(site, **kwargs)

    def _summary(self, site):
        return site_plan_summary(Project.objects.get(pk=site.pk))


# ---------------------------------------------------------------------------
# 1. The pure plan
# ---------------------------------------------------------------------------

class PurePlanTests(BaselineFixture):

    def test_the_pure_plan_and_the_real_plan_are_identical_for_an_on_time_site(self):
        for dc, skip in (('40.00', False), ('40.00', True), ('80.00', False),
                         ('150.00', True)):
            with self.subTest(dc=dc, skip_sundays=skip):
                site = Project.objects.get(pk=self._site(dc, civil_due=D(10)).pk)
                real = _plan(site, skip, D(9))
                pure = _plan(site, skip, D(9), pure=True)
                self.assertIsNone(real.forecast)
                self.assertEqual(self._dates(pure), self._dates(real))
                self.assertEqual(
                    [(tp.task.pk, tp.start, tp.due) for bp in pure.blocks for tp in bp.tasks],
                    [(tp.task.pk, tp.start, tp.due) for bp in real.blocks for tp in bp.tasks])

    def test_the_pure_plan_ignores_the_forecast_late_finish_and_first_dating(self):
        site = self._site('40.00', civil_due=D(10))
        planned = self._dates(self._plan(site))
        self._done(self._task(site, CIVIL), self._ist(13))          # late finish
        with freeze_today(D(20)):                                    # and everything past plan
            reloaded = Project.objects.get(pk=site.pk)
            real = _plan(reloaded, False, D(20))
            pure = _plan(reloaded, False, D(20), pure=True)
        self.assertNotEqual(self._dates(real), planned)
        self.assertEqual(self._dates(pure), planned)

    def test_a_manual_date_does_not_change_the_baseline(self):
        """Spec amendment: Modules typed in by hand for 20 Nov keeps its planned 11-12 Nov
        baseline, so it shows "plan: 12 Nov" and the site its slip."""
        site = self._site('40.00', civil_due=D(10))
        self._set_due(self._task(site, MODULE), D(20))
        self._apply(site)
        module = self._task(site, MODULE)
        self.assertEqual(module.due_date_source, Task.DUE_DATE_MANUAL)
        self.assertEqual((module.due_date, module.baseline_start, module.baseline_due),
                         (D(20), D(11), D(12)))
        self.assertEqual(self._summary(site).days_behind, 4)

    def test_a_not_applicable_task_has_no_baseline(self):
        site = self._site('40.00', civil_due=D(10))
        Task.objects.filter(pk=self._task(site, MODULE).pk).update(is_not_applicable=True)
        self._apply(site)
        module = self._task(site, MODULE)
        self.assertEqual((module.baseline_start, module.baseline_due), (None, None))


# ---------------------------------------------------------------------------
# 2. Snapshot before Civil starts
# ---------------------------------------------------------------------------

class SnapshotTests(BaselineFixture):

    def test_first_scheduling_sets_the_baseline_equal_to_the_dates(self):
        site = self._site('40.00', civil_due=D(10))
        result = self._apply(site)
        self.assertFalse(result.baseline_frozen)
        self.assertEqual(result.baselined, 11)
        for task in self._in_block(site):
            self.assertEqual((task.baseline_start, task.baseline_due),
                             (task.schedule_start, task.due_date), task.task_name)
        civil = self._task(site, CIVIL)
        self.assertEqual((civil.baseline_start, civil.baseline_due), (D(7), D(10)))
        self.assertEqual(self._task(site, NET_METER_INSTALLATION).baseline_due, D(16))

    def test_before_civil_starts_a_civil_date_change_re_snapshots_through_the_real_view(self):
        site = self._site('40.00', civil_due=D(10))
        self._apply(site)
        civil = self._task(site, CIVIL)
        with self.captureOnCommitCallbacks(execute=True):
            _client_for(self.pm).post(
                reverse('task_set_due_date', args=[site.project_id, civil.pk]),
                {'due_date': D(12).isoformat()})
        civil.refresh_from_db()
        self.assertEqual(civil.baseline_due, D(12))
        self.assertEqual(self._task(site, NET_METER_INSTALLATION).baseline_due, D(18))
        for task in self._in_block(site):
            self.assertEqual(task.baseline_due, task.due_date, task.task_name)

    def test_a_second_run_with_no_change_writes_no_baseline(self):
        site = self._site('40.00', civil_due=D(10))
        self._apply(site)
        self.assertEqual(self._apply(site).baselined, 0)

    def test_a_forecast_only_run_never_moves_the_baseline_civil_not_started(self):
        """Civil due 10 Nov and still Not Started: each daily run moves the forecast a day,
        the baseline holds, and the site falls 1, 2, 3 days behind."""
        site = self._site('40.00', civil_due=D(10))
        self._apply(site)
        planned = self._baselines(site)
        for day, behind in ((11, 1), (12, 2), (13, 3)):
            with self.subTest(today=D(day)):
                result = self._apply_on(D(day), site)
                # SCHED-2e amendment: the Civil date (10 Nov) has passed, so the baseline
                # is now frozen as well. It would hold either way: the pure plan ignores
                # the forecast.
                self.assertTrue(result.baseline_frozen)
                self.assertGreater(result.forecast_moved, 0)
                self.assertEqual(result.baselined, 0)
                self.assertEqual(self._baselines(site), planned)
                summary = self._summary(site)
                self.assertEqual(summary.baseline_finish, D(16))
                self.assertEqual(summary.days_behind, behind)

    def test_clearing_the_site_before_civil_starts_clears_the_baseline(self):
        site = self._site('40.00', civil_due=D(10))
        self._apply(site)
        Project.objects.filter(pk=site.pk).update(dc_capacity_kw=None)
        self._apply(site)
        self.assertEqual(set(self._baselines(site).values()), {(None, None)})
        self.assertIsNone(self._summary(site))


# ---------------------------------------------------------------------------
# 3. Frozen once Civil starts
# ---------------------------------------------------------------------------

class FreezeTests(BaselineFixture):

    def setUp(self):
        self.site = self._site('40.00', civil_due=D(10))
        self._apply(self.site)
        self.planned = self._baselines(self.site)

    def test_civil_started_is_status_completed_at_or_a_transition_row(self):
        civil = self._task(self.site, CIVIL)
        self.assertFalse(civil_has_started(civil))
        self._start_civil(self.site)
        # Blocked after In Progress: only the ledger row still says it started.
        Task.objects.filter(pk=civil.pk).update(status=Task.BLOCKED)
        civil.refresh_from_db()
        self.assertTrue(civil_has_started(civil))
        self.assertIsNone(civil.completed_at)

    def test_after_civil_starts_a_forecast_shift_moves_due_dates_not_the_baseline(self):
        self._start_civil(self.site)
        before = self._due_dates(self.site)
        result = self._apply_on(D(12), self.site)
        self.assertTrue(result.baseline_frozen)
        self.assertGreater(result.forecast_moved, 0)
        self.assertNotEqual(self._due_dates(self.site), before)
        self.assertEqual(self._baselines(self.site), self.planned)

    def test_a_late_civil_done_leaves_the_baseline_unchanged(self):
        self._start_civil(self.site)
        self._done(self._task(self.site, CIVIL), self._ist(13))
        result = self._apply_on(D(13), self.site)
        self.assertEqual(result.blocks[0].re_anchored_after, D(13))
        self.assertEqual(self._task(self.site, TESTING).due_date, D(19))
        self.assertEqual(self._baselines(self.site), self.planned)

    def test_a_civil_date_edit_after_civil_starts_leaves_the_baseline_unchanged(self):
        civil = self._start_civil(self.site)
        civil.refresh_from_db()
        with self.captureOnCommitCallbacks(execute=True):
            _client_for(self.pm).post(
                reverse('task_set_due_date', args=[self.site.project_id, civil.pk]),
                {'due_date': D(12).isoformat()})
        self.assertEqual(self._task(self.site, NET_METER_INSTALLATION).due_date, D(18))
        self.assertEqual(self._baselines(self.site), self.planned)

    def test_blocked_after_started_stays_frozen(self):
        civil = self._start_civil(self.site)
        Task.objects.filter(pk=civil.pk).update(status=Task.BLOCKED)
        civil.refresh_from_db()
        self._set_due(civil, D(12))
        result = self._apply(self.site)
        self.assertTrue(result.baseline_frozen)
        self.assertEqual(self._baselines(self.site), self.planned)

    def test_a_capacity_edit_after_civil_starts_leaves_the_baseline_unchanged(self):
        self._start_civil(self.site)
        url = reverse('project_field_edit', args=[self.site.project_id])
        before = self._due_dates(self.site)
        with self.captureOnCommitCallbacks(execute=True):
            _client_for(self.pm).post(url, {'dc_capacity_kw': '150.00', 'reason': 'as built'})
        self.assertEqual(Project.objects.get(pk=self.site.pk).dc_capacity_kw, Decimal('150.00'))
        self.assertNotEqual(self._due_dates(self.site), before)
        self.assertEqual(self._baselines(self.site), self.planned)

    def test_the_working_day_switch_after_civil_starts_leaves_the_baseline_unchanged(self):
        self._start_civil(self.site)
        admin_profile = _profile('s2e_admin', 'Admin')
        _client_for(admin_profile).post(reverse('admin_master_switches'),
                                        {'schedule_action': 'confirm',
                                         'schedule_skip_sundays': '1'})
        self.assertTrue(SystemSettings.get().schedule_skip_sundays)
        self.assertEqual(self._task(self.site, TESTING).due_date, D(17))
        self.assertEqual(self._baselines(self.site), self.planned)

    def test_before_civil_starts_the_switch_and_capacity_do_re_snapshot(self):
        """The other side of the two tests above: the same edits before Civil starts."""
        admin_profile = _profile('s2e_admin2', 'Admin')
        _client_for(admin_profile).post(reverse('admin_master_switches'),
                                        {'schedule_action': 'confirm',
                                         'schedule_skip_sundays': '1'})
        self.assertEqual(self._task(self.site, CIVIL).baseline_start, D(6))
        self.assertEqual(self._task(self.site, TESTING).baseline_due, D(17))
        with self.captureOnCommitCallbacks(execute=True):
            _client_for(self.pm).post(reverse('project_field_edit', args=[self.site.project_id]),
                                      {'dc_capacity_kw': '150.00', 'reason': 'as built'})
        for task in self._in_block(self.site):
            self.assertEqual((task.baseline_start, task.baseline_due),
                             (task.schedule_start, task.due_date), task.task_name)

    def test_a_task_first_dated_after_the_freeze_gets_its_own_baseline(self):
        """Modules N/A at first (no plan, no baseline), Civil starts, Modules made
        applicable again: Modules takes its plan; nobody else's baseline moves."""
        module = self._task(self.site, MODULE)
        Task.objects.filter(pk=module.pk).update(is_not_applicable=True)
        self._apply(self.site)                         # before the freeze: Modules cleared
        self.assertEqual(self._baselines(self.site)[module.pk], (None, None))
        others = {pk: v for pk, v in self._baselines(self.site).items() if pk != module.pk}
        self._start_civil(self.site)
        Task.objects.filter(pk=module.pk).update(is_not_applicable=False)
        result = self._apply(self.site)
        self.assertTrue(result.baseline_frozen)
        self.assertEqual(result.baselined, 1)
        self.assertEqual(self._baselines(self.site)[module.pk], (D(11), D(12)))
        self.assertEqual({pk: v for pk, v in self._baselines(self.site).items()
                          if pk != module.pk}, others)


class DateFreezeTests(BaselineFixture):
    """SCHED-2e amendment (9 Nov 2026): the baseline also freezes once the Civil due date
    it rests on is before today, whichever of that and Civil starting comes first. Today
    is 9 Nov (the class freeze); days are calendar days (the switch's default)."""

    def _pm_moves_civil(self, site, value):
        """The PM's inline edit through the real view, with the after-commit reschedule
        run, exactly as a live request does."""
        civil = self._task(site, CIVIL)
        with self.captureOnCommitCallbacks(execute=True):
            response = _client_for(self.pm).post(
                reverse('task_set_due_date', args=[site.project_id, civil.pk]),
                {'due_date': value})
        self.assertIn(response.status_code, (200, 302))
        self.assertEqual(Task.objects.get(pk=civil.pk).status, Task.NOT_STARTED)

    def test_a_late_civil_moved_forward_keeps_the_plan_and_shows_the_slip(self):
        """Civil Not Started, due 6 Nov, today 9 Nov; the PM moves it to 17 Nov."""
        site = self._site('40.00', civil_due=D(6))
        self._apply_on(D(5), site)                  # planned while 6 Nov was still ahead
        planned = self._baselines(site)
        self.assertEqual(planned[self._task(site, CIVIL).pk][1], D(6))
        self._pm_moves_civil(site, '2026-11-17')
        self.assertEqual(self._baselines(site), planned, 'the plan must not move')
        summary = self._summary(site)
        self.assertEqual(summary.days_behind, 11)   # the Civil date moved 11 days
        page = _client_for(self.pm).get(
            reverse('project_overview', args=[site.project_id])).content.decode()
        self.assertIn('11 days behind plan', page)

    def test_a_civil_date_still_ahead_moved_forward_re_plans(self):
        """Civil Not Started, due 20 Nov, today 9 Nov; the PM moves it to 25 Nov."""
        site = self._site('40.00', civil_due=D(20))
        self._apply(site)
        before = self._baselines(site)
        self._pm_moves_civil(site, '2026-11-25')
        after = self._baselines(site)
        self.assertNotEqual(after, before)
        self.assertEqual(after[self._task(site, CIVIL).pk][1], D(25))
        summary = self._summary(site)
        self.assertEqual(summary.days_behind, 0)
        self.assertEqual(summary.baseline_finish, summary.forecast_finish)
        page = _client_for(self.pm).get(
            reverse('project_overview', args=[site.project_id])).content.decode()
        self.assertIn('On plan', page)

    def test_the_freeze_comes_when_the_civil_date_passes(self):
        site = self._site('40.00', civil_due=D(10))
        self.assertFalse(self._apply_on(D(10), site).baseline_frozen)   # due today: not yet
        self.assertTrue(self._apply_on(D(11), site).baseline_frozen)

    def test_both_civil_dates_are_asked(self):
        civil = SimpleNamespace(baseline_due=None, due_date=None)
        self.assertFalse(civil_date_has_passed(civil, D(9)))
        civil.baseline_due, civil.due_date = D(6), D(17)    # plan passed, date moved on
        self.assertTrue(civil_date_has_passed(civil, D(9)))
        civil.baseline_due, civil.due_date = D(20), D(5)    # date typed into the past
        self.assertTrue(civil_date_has_passed(civil, D(9)))
        civil.baseline_due, civil.due_date = D(20), D(25)
        self.assertFalse(civil_date_has_passed(civil, D(9)))
        self.assertFalse(civil_date_has_passed(civil, None))    # migration 0141: no today
        self.assertFalse(civil_date_has_passed(None, D(9)))

    def test_clearing_a_passed_civil_date_keeps_the_frozen_baseline(self):
        """Frozen means frozen: a site that drops out of the schedule after its Civil
        date passed keeps its plan, as one whose Civil has started does."""
        site = self._site('40.00', civil_due=D(6))
        self._apply_on(D(5), site)
        planned = self._baselines(site)
        self._pm_moves_civil(site, '')
        self.assertEqual(self._baselines(site), planned)
        self._pm_moves_civil(site, '2026-11-06')
        self.assertEqual(self._baselines(site), planned)

    def test_rebaseline_site_still_re_plans_a_date_frozen_site(self):
        site = self._site('40.00', civil_due=D(6))
        self._apply_on(D(5), site)
        self._pm_moves_civil(site, '2026-11-17')
        call_command('rebaseline_site', '--site', site.project_id, '--note', 'new plan',
                     '--apply', stdout=StringIO())
        self.assertEqual(self._baselines(site)[self._task(site, CIVIL).pk][1], D(17))
        self.assertEqual(self._summary(site).days_behind, 0)


# ---------------------------------------------------------------------------
# 4. rebaseline_site
# ---------------------------------------------------------------------------

class RebaselineCommandTests(BaselineFixture):

    def setUp(self):
        self.site = self._site('40.00', civil_due=D(10))
        self._apply(self.site)
        civil = self._start_civil(self.site)
        civil.refresh_from_db()
        self._set_due(civil, D(12))     # after the freeze: the baseline still says 10 Nov
        self._apply(self.site)
        self.planned = self._baselines(self.site)

    def _call(self, *args):
        out = StringIO()
        call_command('rebaseline_site', '--site', self.site.project_id, *args, stdout=out)
        return out.getvalue()

    def _resets(self):
        return ActivityLog.objects.filter(project=self.site,
                                          action_code=BASELINE_RESET_ACTION_CODE)

    def test_a_note_is_required(self):
        with self.assertRaises(CommandError):
            self._call()
        with self.assertRaisesMessage(CommandError, 'A note is required'):
            self._call('--note', '   ', '--apply')
        self.assertEqual(self._baselines(self.site), self.planned)
        self.assertFalse(self._resets().exists())

    def test_the_default_is_a_dry_run(self):
        out = self._call('--note', 'Civil re-planned')
        self.assertIn('DRY RUN', out)
        self.assertIn('Baselines to write: 11', out)
        self.assertEqual(self._baselines(self.site), self.planned)
        self.assertFalse(self._resets().exists())

    def test_apply_re_snapshots_from_the_current_plan_and_logs_once(self):
        out = self._call('--note', 'Civil re-planned after rain', '--apply')
        self.assertIn('Baselines written: 11', out)
        for task in self._in_block(self.site):
            self.assertEqual((task.baseline_start, task.baseline_due),
                             (task.schedule_start, task.due_date), task.task_name)
        self.assertEqual(self._task(self.site, CIVIL).baseline_due, D(12))
        rows = list(self._resets())
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0].actor)
        self.assertIn('note: Civil re-planned after rain', rows[0].action)
        self.assertIn('11 task(s) re-planned from Civil due 2026-11-12', rows[0].action)

    def test_an_unscheduled_site_is_refused(self):
        Project.objects.filter(pk=self.site.pk).update(dc_capacity_kw=None)
        with self.assertRaisesMessage(CommandError, 'is not scheduled (needs_capacity)'):
            self._call('--note', 'x', '--apply')
        self.assertEqual(self._baselines(self.site), self.planned)


# ---------------------------------------------------------------------------
# 5. The summary and the overview
# ---------------------------------------------------------------------------

class SummaryTests(BaselineFixture):

    def _overview(self, site):
        response = _client_for(self.pm).get(reverse('project_overview', args=[site.project_id]))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_five_days_behind(self):
        """Civil due 10 Nov and open on 15 Nov: blocks 2+ restart on 16 Nov, so Net meter
        is forecast for 21 Nov against a plan of 16 Nov."""
        site = self._site('40.00', civil_due=D(10))
        self._apply(site)
        self._start_civil(site)
        self._apply_on(D(15), site)
        summary = self._summary(site)
        self.assertEqual((summary.baseline_finish, summary.forecast_finish, summary.days_behind),
                         (D(16), D(21), 5))
        with freeze_today(D(15)):
            html = self._overview(site)
        self.assertIn('Planned finish 16 Nov', html)
        self.assertIn('Forecast 21 Nov', html)
        self.assertIn('5 days behind plan', html)
        self.assertIn('plan: 16 Nov', html)

    def test_on_plan(self):
        site = self._site('40.00', civil_due=D(10))
        self._apply(site)
        summary = self._summary(site)
        self.assertEqual((summary.baseline_finish, summary.forecast_finish, summary.days_behind),
                         (D(16), D(16), 0))
        html = self._overview(site)
        self.assertIn('Planned finish 16 Nov', html)
        self.assertIn('On plan', html)
        self.assertNotIn('behind plan', html)
        self.assertNotIn('plan: ', html)

    def test_ahead_of_plan_shows_zero(self):
        site = self._site('40.00', civil_due=D(10))
        self._apply(site)
        Task.objects.filter(pk=self._task(site, NET_METER_INSTALLATION).pk).update(
            baseline_due=D(30))
        self.assertEqual(self._summary(site).days_behind, 0)

    def test_the_line_renders_only_for_scheduled_sites(self):
        unscheduled = self._site(None)                       # no capacity, no Civil date
        self._apply(unscheduled)
        self.assertIsNone(self._summary(unscheduled))
        # SCHED-2d: the #site-plan-summary element is always drawn, empty when there is
        # no plan, so it can listen for a reschedule; what must be absent is the line.
        self.assertNotIn('Planned finish', self._overview(unscheduled))

        excluded = self._site('40.00', civil_due=D(10))
        self._apply(excluded)
        self._start_civil(excluded)                          # frozen baseline survives
        Project.objects.filter(pk=excluded.pk).update(exclude_from_auto_schedule=True)
        self._apply(excluded)
        self.assertIsNone(self._summary(excluded))
        self.assertNotIn('Planned finish', self._overview(excluded))

        scheduled = self._site('40.00', civil_due=D(10))
        self._apply(scheduled)
        self.assertIn('Planned finish', self._overview(scheduled))

    def test_the_baseline_is_read_only_in_the_admin(self):
        self.assertIn('baseline_start', TaskAdmin.readonly_fields)
        self.assertIn('baseline_due', TaskAdmin.readonly_fields)


# ---------------------------------------------------------------------------
# 6. Migration 0141
# ---------------------------------------------------------------------------

class _Apps:
    _MODELS = {m.__name__: m for m in (Task, Project, ScheduleBlock, ScheduleBlockTask,
                                       SystemSettings, TaskTemplate, TaskTemplatePhase,
                                       TaskTemplateTask)}

    def get_model(self, app_label, model_name):
        assert app_label == 'projects'
        return self._MODELS[model_name]


class BackfillMigrationTests(BaselineFixture):

    def setUp(self):
        self.migration = import_module('projects.migrations.0141_sched2e_backfill_baseline')

    def test_the_backfill_writes_the_pure_plan_not_todays_moved_dates(self):
        moved = self._site('40.00', civil_due=D(10))
        self._apply(moved)
        self._start_civil(moved)
        self._apply_on(D(12), moved)                         # forecast: Net meter 18 Nov
        never = self._site('40.00')                          # no system date at all
        Task.objects.update(baseline_start=None, baseline_due=None)   # as before 0141

        self.migration.backfill_baseline(_Apps(), None)

        self.assertEqual(self._task(moved, NET_METER_INSTALLATION).due_date, D(18))
        self.assertEqual(self._task(moved, NET_METER_INSTALLATION).baseline_due, D(16))
        civil = self._task(moved, CIVIL)
        self.assertEqual((civil.baseline_start, civil.baseline_due), (D(7), D(10)))
        self.assertEqual(sum(1 for v in self._baselines(moved).values() if v[1]), 11)
        self.assertEqual(self._summary(moved).days_behind, 2)
        self.assertEqual(set(self._baselines(never).values()), {(None, None)})

    def test_the_backfill_writes_nothing_but_the_baseline(self):
        site = self._site('40.00', civil_due=D(10))
        self._apply(site)
        Task.objects.update(baseline_start=None, baseline_due=None)
        before = list(Task.objects.order_by('pk').values_list(
            'pk', 'due_date', 'due_date_source', 'schedule_start', 'status'))
        logs = ActivityLog.objects.count()
        self.migration.backfill_baseline(_Apps(), None)
        self.assertEqual(list(Task.objects.order_by('pk').values_list(
            'pk', 'due_date', 'due_date_source', 'schedule_start', 'status')), before)
        self.assertEqual(ActivityLog.objects.count(), logs)

    def test_the_reverse_clears_every_baseline(self):
        site = self._site('40.00', civil_due=D(10))
        self._apply(site)
        self.migration.clear_baseline(_Apps(), None)
        self.assertFalse(Task.objects.filter(baseline_due__isnull=False).exists())
        self.assertFalse(Task.objects.filter(baseline_start__isnull=False).exists())
