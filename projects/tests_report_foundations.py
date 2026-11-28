"""
RPT-B1 — the Reports foundations: periods, the bounded activity builder, active users
and sites with bulk actions collapsed, the site label, and the two report gates.

WHAT THIS PINS (each test fails if its rule is wrong)
    Periods        IST day edges; the week starts Monday; previous period = the same length
                   before (week Mon..Wed, month clamped); range 92 days ok, 93 refused;
                   From > To, a future day, a missing day refused in plain words;
                   an unknown period is today
    Parity         human_activity_between() selects exactly what site_breakdown()'s old
                   inline filter selected, at every edge
    Users          a login is not an action; attendance is (its site is not made active);
                   scheduler and *_opened rows are not; a ledger row twinned with a log row
                   counts once; deactivated users are out; site-less actions are counted
    Bulk           >=10 sites within 2 s each = one action, its sites not active; fast single
                   submissions (the 27 Nov pattern) are not bulk; batches are counted apart
                   and on the day they started; two codes over the same sites in the same
                   second are one bulk action
    Sites          Draft vs in execution; test and deleted out everywhere; Cancelled
                   credits the user but is never an active site; site_code else project_id
    Open tasks     open, human-owned, applicable tasks of people with no action
    Gates          CEO / Admin / System Admin only, active user and profile
    Query budget   the same count for 1 day and 92, for 3 sites and 30

Run with:
    python manage.py test projects.tests_report_foundations --settings=solarpms.test_settings
"""
from datetime import date, datetime, time, timedelta

from django.contrib.auth.models import AnonymousUser, User
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from .models import (ActivityLog, ContractorAttendance, Project, ProjectPhase,
                     StatusTransition, Task, UserProfile)
from .permissions import (PAYMENT_REPORT_ROLES, REPORTS_ROLES, user_can_view_payment_reports,
                          user_can_view_reports)
from .report_activity import (STAGE_CANCELLED, STAGE_DESIGN, STAGE_EXECUTION, activity_report,
                              active_sites,
                              active_users, bulk_actions, open_tasks_no_activity,
                              signed_in_no_action, site_label, site_label_expr)
from .report_periods import (ERROR_FROM_AFTER_TO, ERROR_FUTURE, ERROR_PICK_DATE,
                             ERROR_TOO_LONG, MAX_RANGE_DAYS, PERIOD_DATE, PERIOD_RANGE,
                             PERIOD_TODAY, build_period, parse_period)
from .site_dashboard import human_activity_between, human_activity_q
from .tests_site_breakdown import NOW, BreakdownFixture, _ist as _breakdown_ist
from .tests_two_step_completion import _profile

SAT = date(2026, 11, 28)     # a Saturday; 23 Nov 2026 is the Monday of its week
DAY = date(2026, 11, 25)     # the fixture's report day (a Wednesday)


def ist(d, hour, minute=0, second=0, micro=0):
    """`hour:minute` IST on date `d`, as an aware datetime."""
    return timezone.make_aware(datetime.combine(d, time(hour, minute, second, micro)))


def day_period(d):
    return build_period(PERIOD_DATE, d, d, SAT)


# ---------------------------------------------------------------------------
# 1. Periods
# ---------------------------------------------------------------------------

class PeriodTests(TestCase):

    def _parse(self, **params):
        return parse_period(params, today=SAT)

    def test_today_is_the_default_with_its_ist_midnights(self):
        p, error = self._parse()
        self.assertIsNone(error)
        self.assertEqual((p.key, p.start_date, p.end_date, p.days), (PERIOD_TODAY, SAT, SAT, 1))
        self.assertEqual(p.start_dt, ist(SAT, 0))
        self.assertEqual(p.end_dt, ist(SAT + timedelta(days=1), 0))
        self.assertEqual(p.label, 'Today, 28 Nov 2026')
        self.assertEqual((p.previous.start_date, p.previous.end_date),
                         (date(2026, 11, 27), date(2026, 11, 27)))

    def test_an_unknown_period_is_today_without_an_error(self):
        p, error = self._parse(period='fortnight')
        self.assertIsNone(error)
        self.assertEqual(p.key, PERIOD_TODAY)

    def test_rows_at_2359_and_0001_ist_land_on_their_own_day(self):
        p = day_period(DAY)
        self.assertTrue(p.start_dt <= ist(DAY, 23, 59, 59, 999999) < p.end_dt)
        self.assertFalse(p.start_dt <= ist(DAY + timedelta(days=1), 0, 1) < p.end_dt)
        self.assertFalse(p.start_dt <= ist(DAY - timedelta(days=1), 23, 59) < p.end_dt)
        self.assertTrue(p.start_dt <= ist(DAY, 0, 1) < p.end_dt)

    def test_yesterday_compares_with_the_day_before(self):
        p, _ = self._parse(period='yesterday')
        self.assertEqual((p.start_date, p.previous.start_date),
                         (date(2026, 11, 27), date(2026, 11, 26)))
        self.assertEqual(p.label, 'Yesterday, 27 Nov 2026')

    def test_this_week_starts_monday(self):
        p, _ = self._parse(period='week')
        self.assertEqual((p.start_date, p.end_date, p.days), (date(2026, 11, 23), SAT, 6))
        self.assertEqual(p.label, 'This week, 23 Nov – 28 Nov 2026')

    def test_on_a_monday_this_week_is_one_day(self):
        p, _ = parse_period({'period': 'week'}, today=date(2026, 11, 23))
        self.assertEqual((p.start_date, p.end_date, p.days),
                         (date(2026, 11, 23), date(2026, 11, 23), 1))
        self.assertEqual((p.previous.start_date, p.previous.end_date),
                         (date(2026, 11, 16), date(2026, 11, 16)))

    def test_this_week_on_a_wednesday_compares_with_the_previous_mon_to_wed(self):
        p, _ = parse_period({'period': 'week'}, today=DAY)
        self.assertEqual((p.start_date, p.end_date), (date(2026, 11, 23), DAY))
        self.assertEqual((p.previous.start_date, p.previous.end_date),
                         (date(2026, 11, 16), date(2026, 11, 18)))

    def test_this_month_compares_with_the_same_days_last_month(self):
        p, _ = self._parse(period='month')
        self.assertEqual((p.start_date, p.end_date), (date(2026, 11, 1), SAT))
        self.assertEqual((p.previous.start_date, p.previous.end_date),
                         (date(2026, 10, 1), date(2026, 10, 28)))
        self.assertEqual(p.label, 'This month, 1 Nov – 28 Nov 2026')

    def test_the_month_comparison_clamps_to_the_shorter_month(self):
        p, _ = parse_period({'period': 'month'}, today=date(2027, 3, 31))
        self.assertEqual((p.previous.start_date, p.previous.end_date),
                         (date(2027, 2, 1), date(2027, 2, 28)))
        leap, _ = parse_period({'period': 'month'}, today=date(2028, 3, 31))
        self.assertEqual(leap.previous.end_date, date(2028, 2, 29))

    def test_a_chosen_date(self):
        p, error = self._parse(period='date', date='2026-11-25')
        self.assertIsNone(error)
        self.assertEqual((p.start_date, p.label, p.previous.label),
                         (DAY, '25 Nov 2026', 'vs 24 Nov 2026'))

    def test_a_missing_or_unreadable_date_asks_for_one(self):
        for value in ('', 'next week', '2026-13-45'):
            with self.subTest(value=value):
                p, error = self._parse(period='date', date=value)
                self.assertEqual((error, p.key), (ERROR_PICK_DATE, PERIOD_TODAY))
        _, error = self._parse(period='range', **{'from': '2026-11-01'})
        self.assertEqual(error, ERROR_PICK_DATE)

    def test_a_future_date_is_refused(self):
        _, error = self._parse(period='date', date='2026-11-29')
        self.assertEqual(error, ERROR_FUTURE)
        _, error = self._parse(period='range', **{'from': '2026-11-20', 'to': '2026-11-29'})
        self.assertEqual(error, ERROR_FUTURE)

    def test_from_after_to_is_refused(self):
        p, error = self._parse(period='range', **{'from': '2026-11-25', 'to': '2026-11-16'})
        self.assertEqual((error, p.key), (ERROR_FROM_AFTER_TO, PERIOD_TODAY))

    def test_a_range_of_92_days_is_accepted_and_93_refused(self):
        start = SAT - timedelta(days=MAX_RANGE_DAYS - 1)
        p, error = self._parse(period='range', **{'from': start.isoformat(), 'to': SAT.isoformat()})
        self.assertIsNone(error)
        self.assertEqual((p.key, p.days), (PERIOD_RANGE, 92))
        _, error = self._parse(period='range', **{'from': (start - timedelta(days=1)).isoformat(),
                                                  'to': SAT.isoformat()})
        self.assertEqual(error, ERROR_TOO_LONG)
        self.assertEqual(ERROR_TOO_LONG, 'Date range can be at most 92 days')

    def test_a_range_compares_with_the_same_number_of_days_before(self):
        p, _ = self._parse(period='range', **{'from': '2026-11-16', 'to': '2026-11-25'})
        self.assertEqual(p.label, '16 Nov – 25 Nov 2026 (10 days)')
        self.assertEqual((p.previous.start_date, p.previous.end_date),
                         (date(2026, 11, 6), date(2026, 11, 15)))
        self.assertEqual(p.previous.label, 'vs previous 10 days, 6 Nov – 15 Nov 2026')

    def test_a_range_ending_today_says_so(self):
        # Edge case 6: today's partial day is included, and the label says "to today".
        p, _ = self._parse(period='range', **{'from': '2026-11-16', 'to': SAT.isoformat()})
        self.assertEqual(p.label, '16 Nov – 28 Nov 2026 (13 days, to today)')
        self.assertEqual(p.end_dt, ist(SAT + timedelta(days=1), 0))


# ---------------------------------------------------------------------------
# 2. Builder parity with site_breakdown()'s old inline filter
# ---------------------------------------------------------------------------

class BuilderParityTests(BreakdownFixture):

    def test_the_builder_selects_what_the_old_inline_filter_selected(self):
        site = self._resco()
        day_start = _breakdown_ist(9, 0, 0)
        rows = [
            self._log(site, 'task_status_changed', day_start),                       # midnight
            self._log(site, '', _breakdown_ist(8, 23, 59, 59)),                      # yesterday
            self._log(site, '', NOW),                                                # == now
            self._log(site, '', NOW + timedelta(microseconds=1)),                    # after now
            self._log(site, 'due_dates_scheduled', NOW - timedelta(hours=1), actor=self.pm),
            self._log(site, 'project_overview_opened', NOW - timedelta(hours=1)),
            self._log(site, 'payment_request_raised', NOW - timedelta(hours=2)),     # money
            self._log(site, '', NOW - timedelta(hours=3)),                           # blank code
        ]
        # The filter site_breakdown() held before RPT-B1, written out by hand.
        old = set(ActivityLog.objects.filter(project_id=site.pk, timestamp__gte=day_start,
                                             timestamp__lte=NOW)
                  .filter(human_activity_q()).values_list('pk', flat=True))
        new = set(ActivityLog.objects.filter(project_id=site.pk)
                  .filter(human_activity_between(day_start, NOW, end_inclusive=True))
                  .values_list('pk', flat=True))
        self.assertEqual(new, old)
        self.assertEqual(new, {rows[0].pk, rows[2].pk, rows[6].pk, rows[7].pk})

    def test_the_exclusive_form_leaves_out_a_row_at_the_end(self):
        site = self._resco()
        at_end = self._log(site, '', NOW)
        inside = self._log(site, '', NOW - timedelta(seconds=1))
        got = set(ActivityLog.objects.filter(project_id=site.pk)
                  .filter(human_activity_between(NOW - timedelta(hours=1), NOW))
                  .values_list('pk', flat=True))
        self.assertEqual(got, {inside.pk})
        self.assertNotIn(at_end.pk, got)

    def test_site_breakdown_active_today_reads_through_the_builder(self):
        site = self._resco()
        self._log(site, '', NOW)
        self.assertEqual(self._b(site).active_today_ids, (site.pk,))


# ---------------------------------------------------------------------------
# Fixture for the activity tests
# ---------------------------------------------------------------------------

class ActivityFixture(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.pm = _profile('rpt_pm', 'PM')
        cls.designer = _profile('rpt_designer', 'Design')
        cls.se = _profile('rpt_se', 'Site Engineer')
        cls.ceo = _profile('rpt_ceo', 'CEO')

    _seq = 0

    def _site(self, status='Active', project_type='OPEX', code='auto', **fields):
        ActivityFixture._seq += 1
        n = ActivityFixture._seq
        if code == 'auto':
            code = f'S{n:04d}'
        return Project.objects.create(
            project_id=f'RPT-{n:04d}', site_code=code, customer_name=f'Customer {n}',
            customer_phone='9876543210', site_address='1 Report Road', city='Bhopal',
            project_type=project_type, status=status,
            activated_at=None if status == 'Draft' else timezone.now(),
            assigned_pm=self.pm, **fields)

    def _log(self, site, actor, when, code='task_status_changed'):
        row = ActivityLog.objects.create(project=site, actor=actor, action=f'did {code}',
                                         action_code=code)
        ActivityLog.objects.filter(pk=row.pk).update(timestamp=when)
        return row

    def _st(self, site, actor, when, subject='task', to_status='Done'):
        return StatusTransition.objects.create(
            subject_type=subject, subject_id=1, project=site, from_status='In Progress',
            to_status=to_status, actor=actor, actor_role_code='PM', occurred_at=when)

    def _attend(self, site, recorder, day, recorded_at, trade='CIVIL', voided=False):
        row = ContractorAttendance.objects.create(
            project=site, date=day, trade=trade, headcount=5, time_in='09:00',
            recorded_by=recorder, unlisted_contractor_name='Crew')
        update = {'recorded_at': recorded_at}
        if voided:
            update.update(is_voided=True, void_note='wrong', voided_by=recorder,
                          voided_at=recorded_at)
        ContractorAttendance.objects.filter(pk=row.pk).update(**update)
        return row

    def _task(self, site, person, **fields):
        phase, _ = ProjectPhase.objects.get_or_create(project=site, phase_order=1,
                                                      defaults={'phase_name': 'Installation'})
        fields.setdefault('task_name', 'Work')
        fields.setdefault('task_order', Task.objects.filter(phase=phase).count() + 1)
        return Task.objects.create(phase=phase, assigned_to=person, **fields)

    def _batch(self, actor, start, n_sites, code='design_allocated', gap_s=0.5, sites=None):
        sites = sites or [self._site(status='Draft') for _ in range(n_sites)]
        for i, site in enumerate(sites):
            self._log(site, actor, start + timedelta(seconds=gap_s * i), code=code)
        return sites

    def _report(self, d=DAY):
        return activity_report(day_period(d)).current

    @staticmethod
    def _user(report, profile):
        return next((r for r in report.active_users.rows if r.profile_id == profile.pk), None)

    @staticmethod
    def _site_ids(report):
        return {r.site.id for r in report.active_sites.rows}


# ---------------------------------------------------------------------------
# 3. Active users
# ---------------------------------------------------------------------------

class ActiveUserTests(ActivityFixture):

    def test_a_login_alone_is_not_activity_but_is_signed_in_no_action(self):
        login = ActivityLog.objects.create(project=None, actor=self.pm, action='User logged in',
                                           action_code='user_login')
        ActivityLog.objects.filter(pk=login.pk).update(timestamp=ist(DAY, 9))
        report = self._report()
        self.assertIsNone(self._user(report, self.pm))
        self.assertEqual([r.profile_id for r in report.signed_in_no_action], [self.pm.pk])
        self.assertEqual(report.signed_in_no_action[0].last_signed_in_at, ist(DAY, 9))

    def test_a_signed_in_user_who_also_acted_is_not_signed_in_no_action(self):
        site = self._site()
        login = ActivityLog.objects.create(project=None, actor=self.pm, action='in',
                                           action_code='user_login')
        ActivityLog.objects.filter(pk=login.pk).update(timestamp=ist(DAY, 9))
        self._log(site, self.pm, ist(DAY, 10))
        report = self._report()
        self.assertEqual(report.signed_in_no_action, ())
        self.assertEqual(self._user(report, self.pm).actions, 1)

    def test_attendance_alone_makes_the_user_active_but_not_the_site(self):
        site = self._site()
        self._attend(site, self.se, DAY, ist(DAY, 10))
        report = self._report()
        row = self._user(report, self.se)
        self.assertEqual((row.actions, row.site_ids), (1, (site.pk,)))
        self.assertEqual(self._site_ids(report), set())
        self.assertEqual(report.active_sites.total, 0)

    def test_voided_attendance_is_not_an_action(self):
        self._attend(self._site(), self.se, DAY, ist(DAY, 10), voided=True)
        self.assertIsNone(self._user(self._report(), self.se))

    def test_a_late_attendance_entry_counts_on_its_date(self):
        # Edge case 4: typed on the 28th for the 25th.
        site = self._site()
        self._attend(site, self.se, DAY, ist(SAT, 11))
        self.assertEqual(self._user(self._report(DAY), self.se).actions, 1)
        self.assertEqual(self._user(self._report(DAY), self.se).last_action_at, ist(SAT, 11))
        self.assertIsNone(self._user(self._report(SAT), self.se))

    def test_scheduler_and_opened_rows_are_not_activity(self):
        site = self._site()
        self._log(site, self.pm, ist(DAY, 9), code='due_dates_scheduled')
        self._log(site, self.pm, ist(DAY, 9), code='project_overview_opened')
        self._log(site, self.pm, ist(DAY, 9), code='user_logout')
        report = self._report()
        self.assertIsNone(self._user(report, self.pm))
        self.assertEqual(report.active_sites.total, 0)

    def test_a_ledger_row_twinned_with_a_log_row_counts_once(self):
        site = self._site()
        self._log(site, self.pm, ist(DAY, 10))
        self._st(site, self.pm, ist(DAY, 10) + timedelta(seconds=3))
        self.assertEqual(self._user(self._report(), self.pm).actions, 1)

    def test_an_unpaired_ledger_row_adds_an_action_but_not_an_active_site(self):
        site = self._site()
        self._log(site, self.pm, ist(DAY, 10))
        self._st(site, self.pm, ist(DAY, 10) + timedelta(seconds=30))   # beyond 5 s
        other = self._site()
        self._st(other, self.pm, ist(DAY, 11))
        report = self._report()
        row = self._user(report, self.pm)
        self.assertEqual(row.actions, 3)
        self.assertEqual(set(row.site_ids), {site.pk, other.pk})
        self.assertEqual(self._site_ids(report), {site.pk})

    def test_a_site_less_ledger_row_pairs_with_a_site_less_log_row(self):
        self._log(None, self.designer, ist(DAY, 10), code='design_boq_group_created')
        self._st(None, self.designer, ist(DAY, 10) + timedelta(seconds=1))
        row = self._user(self._report(), self.designer)
        self.assertEqual((row.actions, row.actions_without_site, row.site_ids), (1, 1, ()))

    def test_an_action_with_no_site_makes_an_active_user_with_no_site(self):
        # Edge case 2: the Design BOQ group rows carry project=NULL.
        self._log(None, self.designer, ist(DAY, 10), code='design_boq_group_created')
        report = self._report()
        row = self._user(report, self.designer)
        self.assertEqual((row.actions, row.actions_without_site, row.site_ids), (1, 1, ()))
        self.assertEqual(report.active_users.actions_without_site, 1)
        self.assertEqual(report.active_sites.total, 0)

    def test_a_deactivated_user_is_left_out_but_their_site_is_active(self):
        gone = _profile('rpt_gone', 'PM')
        User.objects.filter(pk=gone.user_id).update(is_active=False)
        hidden = _profile('rpt_hidden', 'PM')
        UserProfile.objects.filter(pk=hidden.pk).update(is_active=False)
        site = self._site()
        self._log(site, gone, ist(DAY, 10))
        self._log(site, hidden, ist(DAY, 11))
        report = self._report()
        self.assertEqual(report.active_users.users, 0)
        self.assertEqual(self._site_ids(report), {site.pk})
        self.assertEqual(report.active_sites.rows[0].user_names, ())

    def test_a_user_is_shown_with_their_current_role(self):
        # Edge case 7.
        self._log(self._site(), self.se, ist(DAY, 10))
        UserProfile.objects.filter(pk=self.se.pk).update(role='Project Coordinator')
        self.assertEqual(self._user(self._report(), self.se).role, 'Project Coordinator')

    def test_totals_by_role(self):
        site = self._site()
        self._log(site, self.pm, ist(DAY, 10))
        self._log(site, self.pm, ist(DAY, 11))
        self._log(site, self.designer, ist(DAY, 12))
        users = self._report().active_users
        self.assertEqual([(r.role, r.users, r.actions) for r in users.by_role],
                         [('Design', 1, 1), ('PM', 1, 2)])
        self.assertEqual((users.users, users.actions), (2, 3))
        self.assertEqual([r.profile_id for r in users.rows], [self.designer.pk, self.pm.pk])

    def test_rows_at_2359_and_0001_ist_count_on_their_own_day(self):
        late, early = self._site(), self._site()
        self._log(late, self.pm, ist(DAY, 23, 59, 59))
        self._log(early, self.pm, ist(DAY + timedelta(days=1), 0, 1))
        self.assertEqual(self._site_ids(self._report(DAY)), {late.pk})
        self.assertEqual(self._site_ids(self._report(DAY + timedelta(days=1))), {early.pk})

    def test_a_day_with_no_activity_is_zeros_and_still_has_a_previous_period(self):
        # Edge case 1.
        self._log(self._site(), self.pm, ist(DAY - timedelta(days=1), 10))
        report = activity_report(day_period(DAY))
        self.assertEqual((report.current.active_users.users, report.current.active_sites.total,
                          len(report.current.bulk_actions)), (0, 0, 0))
        self.assertEqual(report.previous.active_users.users, 1)
        self.assertEqual(report.previous.active_sites.total, 1)


# ---------------------------------------------------------------------------
# 4. Bulk actions
# ---------------------------------------------------------------------------

class BulkActionTests(ActivityFixture):

    def test_a_15_site_batch_is_one_action_and_leaves_its_sites_inactive(self):
        sites = self._batch(self.designer, ist(DAY, 12, 52, 41), 15)
        report = self._report()
        row = self._user(report, self.designer)
        self.assertEqual((row.actions, row.site_ids, row.bulk_only_site_count), (1, (), 15))
        self.assertEqual(report.active_sites.total, 0)
        self.assertEqual(set(report.active_sites.bulk_only_site_ids), {s.pk for s in sites})
        [batch] = report.bulk_actions
        self.assertEqual((batch.actor_id, batch.action_code, batch.site_count),
                         (self.designer.pk, 'design_allocated', 15))
        self.assertEqual(batch.started_at, ist(DAY, 12, 52, 41))

    def test_a_batch_site_with_a_normal_row_is_active(self):
        sites = self._batch(self.designer, ist(DAY, 12), 12)
        self._log(sites[3], self.pm, ist(DAY, 15))
        report = self._report()
        self.assertEqual(self._site_ids(report), {sites[3].pk})
        self.assertEqual(report.active_sites.bulk_only, 11)

    def test_a_3_site_quick_sequence_is_not_bulk(self):
        sites = self._batch(self.designer, ist(DAY, 12), 3)
        report = self._report()
        self.assertEqual(report.bulk_actions, ())
        self.assertEqual(self._user(report, self.designer).actions, 3)
        self.assertEqual(self._site_ids(report), {s.pk for s in sites})

    def test_fast_single_submissions_4_s_apart_are_not_bulk(self):
        # The 27 Nov 2026 pattern: 144 separate submissions 4-8 s apart are real work.
        sites = self._batch(self.designer, ist(DAY, 14), 12, code='design_survey_link_added',
                            gap_s=4)
        report = self._report()
        self.assertEqual(report.bulk_actions, ())
        self.assertEqual(self._user(report, self.designer).actions, 12)
        self.assertEqual(self._site_ids(report), {s.pk for s in sites})

    def test_two_batches_ten_minutes_apart_are_two_actions(self):
        self._batch(self.designer, ist(DAY, 10), 10)
        self._batch(self.designer, ist(DAY, 10, 10), 10)
        report = self._report()
        self.assertEqual(len(report.bulk_actions), 2)
        self.assertEqual(self._user(report, self.designer).actions, 2)

    def test_two_codes_over_the_same_sites_in_the_same_second_are_one_bulk_action(self):
        # B1 answer 1: design_bulk_allocate writes both codes per site in one POST.
        sites = self._batch(self.designer, ist(DAY, 12, 52, 41), 10, code='design_allocated',
                            gap_s=0.1)
        self._batch(self.designer, ist(DAY, 12, 52, 41, 50000), 10, code='design_qc_assigned',
                    gap_s=0.1, sites=sites)
        report = self._report()
        [batch] = report.bulk_actions
        self.assertEqual(batch.action_code, 'design_allocated + design_qc_assigned')
        self.assertEqual((batch.site_count, batch.source), (10, 'log'))
        self.assertEqual(self._user(report, self.designer).actions, 1)
        self.assertEqual(report.active_sites.bulk_only, 10)

    def test_the_same_sites_a_minute_apart_are_two_bulk_actions(self):
        sites = self._batch(self.designer, ist(DAY, 10), 10, code='design_allocated')
        self._batch(self.designer, ist(DAY, 10, 1), 10, code='design_qc_assigned', sites=sites)
        report = self._report()
        self.assertEqual(len(report.bulk_actions), 2)
        self.assertEqual(self._user(report, self.designer).actions, 2)

    def test_different_sites_in_the_same_second_are_two_bulk_actions(self):
        self._batch(self.designer, ist(DAY, 10), 10, code='design_allocated')
        self._batch(self.designer, ist(DAY, 10), 10, code='design_qc_assigned')
        report = self._report()
        self.assertEqual(sorted(b.action_code for b in report.bulk_actions),
                         ['design_allocated', 'design_qc_assigned'])
        self.assertEqual(self._user(report, self.designer).actions, 2)

    def test_a_batch_over_midnight_counts_on_the_day_it_started(self):
        # Edge case 3: 10 rows from 23:59:57 IST, 1 s apart, over midnight.
        sites = self._batch(self.designer, ist(DAY, 23, 59, 57), 10, gap_s=1)
        today, tomorrow = self._report(DAY), self._report(DAY + timedelta(days=1))
        self.assertEqual(len(today.bulk_actions), 1)
        self.assertEqual(today.bulk_actions[0].site_count, 10)
        self.assertEqual(self._user(today, self.designer).actions, 1)
        self.assertEqual(tomorrow.bulk_actions, ())
        self.assertIsNone(self._user(tomorrow, self.designer))
        self.assertEqual(self._site_ids(tomorrow), set())
        self.assertEqual(tomorrow.active_sites.bulk_only, 0)
        self.assertEqual(len(sites), 10)

    def test_unpaired_ledger_rows_are_batched_by_the_same_rule(self):
        for i, site in enumerate(self._site() for _ in range(10)):
            self._st(site, self.pm, ist(DAY, 9) + timedelta(seconds=i * 0.2))
        report = self._report()
        [batch] = report.bulk_actions
        self.assertEqual((batch.source, batch.action_code, batch.site_count),
                         ('ledger', 'task:Done', 10))
        self.assertEqual(self._user(report, self.pm).actions, 1)

    def test_the_wrappers_return_the_same_figures_as_the_report(self):
        self._batch(self.designer, ist(DAY, 12), 10)
        period = day_period(DAY)
        self.assertEqual(len(bulk_actions(period).current), 1)
        self.assertEqual(active_users(period).current.users, 1)
        self.assertEqual(active_sites(period).current.bulk_only, 10)
        self.assertEqual(signed_in_no_action(period).previous, ())
        self.assertEqual(open_tasks_no_activity(period).current, ())


# ---------------------------------------------------------------------------
# 5. Active sites and the site label
# ---------------------------------------------------------------------------

class ActiveSiteTests(ActivityFixture):

    def test_draft_sites_are_in_design_and_the_rest_in_execution(self):
        live, on_hold, draft = self._site(), self._site(status='On Hold'), self._site(status='Draft')
        for site in (live, on_hold, draft):
            self._log(site, self.pm, ist(DAY, 10))
        sites = self._report().active_sites
        self.assertEqual((sites.in_execution, sites.in_design, sites.total), (2, 1, 3))
        stages = {r.site.id: r.site.stage for r in sites.rows}
        self.assertEqual(stages, {live.pk: STAGE_EXECUTION, on_hold.pk: STAGE_EXECUTION,
                                  draft.pk: STAGE_DESIGN})

    def test_test_and_deleted_sites_never_count(self):
        hidden = [self._site(is_test=True), self._site(is_deleted=True)]
        for site in hidden:
            self._log(site, self.pm, ist(DAY, 10))
            self._attend(site, self.se, DAY, ist(DAY, 10))
        self._batch(self.designer, ist(DAY, 12), 10,
                    sites=[self._site(is_test=True) for _ in range(10)])
        report = self._report()
        self.assertEqual(report.active_sites.total, 0)
        self.assertEqual(report.active_sites.bulk_only, 0)
        self.assertEqual(report.active_users.users, 0)
        self.assertEqual(report.bulk_actions, ())

    def test_a_cancelled_site_credits_the_user_but_is_never_an_active_site(self):
        # B1 answer 2.
        cancelled = self._site(status='Cancelled')
        self._log(cancelled, self.pm, ist(DAY, 10))
        self._attend(cancelled, self.se, DAY, ist(DAY, 11))
        report = self._report()
        pm, se = self._user(report, self.pm), self._user(report, self.se)
        self.assertEqual((pm.actions, pm.site_ids), (1, (cancelled.pk,)))
        self.assertEqual((se.actions, se.site_ids), (1, (cancelled.pk,)))
        self.assertEqual(report.sites[cancelled.pk].stage, STAGE_CANCELLED)
        self.assertEqual((report.active_sites.total, report.active_sites.in_execution,
                          report.active_sites.in_design), (0, 0, 0))

    def test_a_bulk_batch_over_cancelled_sites_is_one_action_and_no_bulk_only_site(self):
        self._batch(self.designer, ist(DAY, 12), 10,
                    sites=[self._site(status='Cancelled') for _ in range(10)])
        report = self._report()
        self.assertEqual(len(report.bulk_actions), 1)
        self.assertEqual(self._user(report, self.designer).actions, 1)
        self.assertEqual(report.active_sites.bulk_only, 0)

    def test_the_site_row_names_its_users_and_last_activity(self):
        site = self._site()
        self._log(site, self.pm, ist(DAY, 10))
        self._log(site, self.designer, ist(DAY, 14))
        self._log(site, None, ist(DAY, 15), code='')
        [row] = self._report().active_sites.rows
        self.assertEqual(row.actions, 3)
        self.assertEqual(row.user_names, ('Rpt_Designer Test', 'Rpt_Pm Test'))
        self.assertEqual(row.last_activity_at, ist(DAY, 15))

    def test_a_site_is_classified_by_its_status_now(self):
        # Edge case 5: a Draft activated after the period reads "in execution" for it too.
        site = self._site(status='Draft')
        self._log(site, self.pm, ist(DAY, 10))
        Project.objects.filter(pk=site.pk).update(status='Active')
        self.assertEqual(self._report().active_sites.rows[0].site.stage, STAGE_EXECUTION)

    def test_the_label_is_site_code_else_project_id(self):
        coded = self._site(code='MB0303', site_name='Police Bhawan')
        bare = self._site(project_type='Residential', code=None)
        blank = self._site(code='')
        self.assertEqual(site_label(coded), 'MB0303')
        self.assertEqual(site_label(bare), bare.project_id)
        self.assertEqual(site_label(blank), blank.project_id)
        annotated = dict(Project.objects.filter(pk__in=[coded.pk, bare.pk, blank.pk])
                         .annotate(label=site_label_expr()).values_list('pk', 'label'))
        self.assertEqual(annotated, {coded.pk: 'MB0303', bare.pk: bare.project_id,
                                     blank.pk: blank.project_id})
        self._log(coded, self.pm, ist(DAY, 10))
        [row] = self._report().active_sites.rows
        self.assertEqual((row.site.label, row.site.name), ('MB0303', 'Police Bhawan'))

    def test_the_name_falls_back_to_the_customer(self):
        site = self._site(code=None)
        self._log(site, self.pm, ist(DAY, 10))
        [row] = self._report().active_sites.rows
        self.assertEqual((row.site.label, row.site.name), (site.project_id, site.customer_name))


# ---------------------------------------------------------------------------
# 6. Open tasks, no activity
# ---------------------------------------------------------------------------

class OpenTasksNoActivityTests(ActivityFixture):

    def test_an_idle_holder_of_open_tasks_is_listed_with_counts_as_of_now(self):
        site = self._site()
        today = timezone.localdate()
        self._task(site, self.se, due_date=today - timedelta(days=3))           # overdue
        self._task(site, self.se, status=Task.IN_PROGRESS)                       # open
        self._task(site, self.se, task_type=Task.EXTERNAL,
                   due_date=today - timedelta(days=3))                           # open, not card-overdue
        [row] = self._report().open_tasks_no_activity
        self.assertEqual((row.profile_id, row.open_tasks, row.overdue_tasks), (self.se.pk, 3, 1))
        self.assertIsNone(row.last_action_at)

    def test_a_holder_who_acted_is_not_listed(self):
        site = self._site()
        self._task(site, self.se)
        self._log(site, self.se, ist(DAY, 10))
        self.assertEqual(self._report().open_tasks_no_activity, ())

    def test_done_na_and_mirror_tasks_are_not_open(self):
        site = self._site()
        self._task(site, self.se, status=Task.DONE)
        self._task(site, self.se, is_not_applicable=True)
        self._task(site, self.se, is_mirror=True)
        self.assertEqual(self._report().open_tasks_no_activity, ())

    def test_tasks_on_a_test_site_are_not_counted(self):
        self._task(self._site(is_test=True), self.se)
        self.assertEqual(self._report().open_tasks_no_activity, ())

    def test_last_action_is_the_latest_at_any_time(self):
        site = self._site()
        self._task(site, self.se)
        self._log(site, self.se, ist(DAY - timedelta(days=10), 9))
        self._attend(site, self.se, DAY - timedelta(days=4), ist(DAY - timedelta(days=4), 9))
        [row] = self._report().open_tasks_no_activity
        self.assertEqual(row.last_action_at, ist(DAY - timedelta(days=4), 9))


# ---------------------------------------------------------------------------
# 7. Gates
# ---------------------------------------------------------------------------

class ReportGateTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.roles = {role: _profile(f'gate_{i}', role) for i, (role, _) in
                     enumerate(UserProfile.ROLE_CHOICES)}

    def _user(self, role):
        return User.objects.get(pk=self.roles[role].user_id)

    def test_ceo_admin_and_system_admin_only(self):
        self.assertEqual(REPORTS_ROLES, {'CEO', 'Admin', 'System Admin'})
        self.assertEqual(PAYMENT_REPORT_ROLES, {'CEO', 'Admin', 'System Admin'})
        for role in self.roles:
            with self.subTest(role=role):
                allowed = role in {'CEO', 'Admin', 'System Admin'}
                self.assertEqual(user_can_view_reports(self._user(role)), allowed)
                self.assertEqual(user_can_view_payment_reports(self._user(role)), allowed)

    def test_an_inactive_user_is_refused(self):
        User.objects.filter(pk=self.roles['CEO'].user_id).update(is_active=False)
        user = self._user('CEO')
        self.assertFalse(user_can_view_reports(user))
        self.assertFalse(user_can_view_payment_reports(user))

    def test_an_inactive_profile_is_refused(self):
        UserProfile.objects.filter(pk=self.roles['Admin'].pk).update(is_active=False)
        user = self._user('Admin')
        self.assertFalse(user_can_view_reports(user))
        self.assertFalse(user_can_view_payment_reports(user))

    def test_anonymous_and_profile_less_users_are_refused(self):
        self.assertFalse(user_can_view_reports(AnonymousUser()))
        self.assertFalse(user_can_view_payment_reports(AnonymousUser()))
        bare = User.objects.create_user(username='gate_bare', password='x')
        UserProfile.objects.filter(user=bare).delete()
        bare = User.objects.get(pk=bare.pk)
        self.assertFalse(user_can_view_reports(bare))
        self.assertFalse(user_can_view_payment_reports(bare))


# ---------------------------------------------------------------------------
# 8. Query budget
# ---------------------------------------------------------------------------

class QueryBudgetTests(ActivityFixture):

    def _count(self, period):
        with CaptureQueriesContext(connection) as ctx:
            activity_report(period)
        return len(ctx.captured_queries)

    def _spread(self, n_sites, days):
        sites = [self._site() for _ in range(n_sites)]
        for i, site in enumerate(sites):
            moment = ist(DAY - timedelta(days=i % days), 10)
            self._log(site, self.pm, moment)
            self._st(site, self.pm, moment + timedelta(minutes=1))
            self._attend(site, self.se, moment.date(), moment)
            self._task(site, self.se)
        login = ActivityLog.objects.create(project=None, actor=self.ceo, action='in',
                                           action_code='user_login')
        ActivityLog.objects.filter(pk=login.pk).update(timestamp=ist(DAY, 8))

    def test_the_same_number_of_queries_for_1_day_and_92_days(self):
        self._spread(30, 92)
        one = self._count(day_period(DAY))
        many = self._count(build_period(PERIOD_RANGE, DAY - timedelta(days=91), DAY, SAT))
        self.assertEqual(one, many)
        self.assertEqual(one, 10)

    def test_the_same_number_of_queries_for_3_sites_and_30_sites(self):
        self._spread(3, 1)
        small = self._count(day_period(DAY))
        self._spread(27, 1)
        self.assertEqual(self._count(day_period(DAY)), small)
