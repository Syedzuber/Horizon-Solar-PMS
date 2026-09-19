"""
The CEO daily report — `reports.build_user_status_rows()` — Active Today and Done Today.

ACTIVE TODAY replaced LOGGED IN. Logged In read `User.last_login` alone, which holds only
the most recent login: someone who logged in yesterday and worked all day today on the
same session read as absent. Active Today is true if ANY of three sources shows the user
on the report date — a login that day, an ActivityLog row as actor, or a StatusTransition
row as actor. Each source gets a test where it is the ONLY one present, so dropping any
of the three from the builder fails exactly one test here.

DONE TODAY now requires status=Done as well as completed_at on the date. The human status
path never clears completed_at when a task leaves Done, so without the status condition a
task completed and reopened the same day still counted.

Also closes deferred G5 in part: the row-sum invariant and the constant query count are
pinned here, next to the columns most likely to disturb them.
"""
from datetime import datetime, time, timedelta

from django.contrib.auth.models import User
from django.db import connection
from django.template.loader import render_to_string
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from .models import ActivityLog, Project, ProjectPhase, Task
from .reports import build_user_status_rows
from .utils import record_transition

# The footnote is user-visible text the prompt fixed verbatim. Pinned here so a reword
# is a deliberate diff in two places, not a silent drift between page and email.
FOOTNOTE = (
    'Active Today means the user logged in, or performed at least one recorded action '
    '(creating, updating, approving, uploading) on the portal that day. Viewing pages is '
    'not recorded, so a user who only read the portal shows as not active.'
)


def _make_user(username, role='Site Engineer'):
    # A post_save signal creates the UserProfile; set the role on it rather than
    # creating a second one (OneToOne).
    user = User.objects.create_user(username=username, password='pw12345',
                                    first_name=username)
    profile = user.profile
    profile.role = role
    profile.save(update_fields=['role'])
    return user, profile


def _local_dt(day, hour=12, minute=0):
    """An aware datetime at `hour:minute` IST on `day`."""
    return timezone.make_aware(datetime.combine(day, time(hour, minute)))


class _ReportFixture(TestCase):
    """One active project; each user gets one open task on it so they have a row."""

    def setUp(self):
        self.today = timezone.localdate()
        self.yesterday = self.today - timedelta(days=1)
        _, self.pm = _make_user('usr_pm', 'PM')
        self.project = Project.objects.create(
            customer_name='Status Report Co', customer_phone='9876500099',
            site_address='1 Report Way', city='Lucknow', project_type='Residential',
            status='Active', assigned_pm=self.pm, activated_at=timezone.now(),
        )
        self.phase = ProjectPhase.objects.create(
            project=self.project, phase_name='Execution', phase_order=1)

    def _task(self, owner, status=Task.NOT_STARTED, **kwargs):
        return Task.objects.create(
            phase=self.phase, task_name=f'Task for {owner.pk}', task_order=1,
            assigned_role=Task.SITE_ENGINEER, task_type=Task.INTERNAL,
            status=status, assigned_to=owner, **kwargs)

    def _worker(self, username):
        user, profile = _make_user(username)
        self._task(profile)
        return user, profile

    def _row(self, profile, report_date=None):
        report = build_user_status_rows(report_date or self.today)
        rows = [r for r in report['rows'] if r['profile'].pk == profile.pk]
        self.assertEqual(len(rows), 1)
        return rows[0]


class ActiveTodayTests(_ReportFixture):

    def test_activity_log_row_today_without_fresh_login_is_active(self):
        user, profile = self._worker('usr_al')
        user.last_login = _local_dt(self.yesterday)
        user.save(update_fields=['last_login'])
        ActivityLog.objects.create(project=self.project, actor=profile,
                                   action='Uploaded a file', entity_type='File')
        self.assertTrue(self._row(profile)['active_today'])

    def test_status_transition_row_only_is_active(self):
        _, profile = self._worker('usr_st')
        task = self._task(profile)
        record_transition(task, to_status=Task.IN_PROGRESS,
                          from_status=Task.NOT_STARTED, actor=profile)
        self.assertFalse(ActivityLog.objects.filter(actor=profile).exists())
        self.assertTrue(self._row(profile)['active_today'])

    def test_fresh_login_only_is_active(self):
        user, profile = self._worker('usr_login')
        user.last_login = _local_dt(self.today, 9)
        user.save(update_fields=['last_login'])
        self.assertTrue(self._row(profile)['active_today'])

    def test_rows_yesterday_only_is_not_active(self):
        user, profile = self._worker('usr_old')
        user.last_login = _local_dt(self.yesterday)
        user.save(update_fields=['last_login'])
        log = ActivityLog.objects.create(project=self.project, actor=profile,
                                         action='Old action')
        # timestamp is auto_now_add; a queryset update is the only way to backdate it.
        ActivityLog.objects.filter(pk=log.pk).update(timestamp=_local_dt(self.yesterday))
        record_transition(self._task(profile), to_status=Task.IN_PROGRESS,
                          from_status=Task.NOT_STARTED, actor=profile,
                          occurred_at=_local_dt(self.yesterday))
        row = self._row(profile)
        self.assertFalse(row['active_today'])

    def test_day_boundary_is_local_midnight(self):
        """23:59 IST yesterday is yesterday and 00:00 IST today is today, although
        both fall on the same UTC date (18:29 and 18:30 UTC the day before)."""
        _, late = self._worker('usr_late')
        _, early = self._worker('usr_early')
        a = ActivityLog.objects.create(actor=late, action='late')
        b = ActivityLog.objects.create(actor=early, action='early')
        ActivityLog.objects.filter(pk=a.pk).update(timestamp=_local_dt(self.yesterday, 23, 59))
        ActivityLog.objects.filter(pk=b.pk).update(timestamp=_local_dt(self.today, 0, 0))
        self.assertFalse(self._row(late)['active_today'])
        self.assertTrue(self._row(early)['active_today'])

    def test_another_users_action_does_not_make_this_user_active(self):
        _, idle = self._worker('usr_idle')
        _, busy = self._worker('usr_busy')
        ActivityLog.objects.create(actor=busy, action='busy')
        self.assertFalse(self._row(idle)['active_today'])

    def test_not_active_count(self):
        _, idle = self._worker('usr_a_idle')
        _, busy = self._worker('usr_b_busy')
        ActivityLog.objects.create(actor=busy, action='busy')
        report = build_user_status_rows(self.today)
        inactive = [r['name'] for r in report['rows'] if not r['active_today']]
        self.assertIn('usr_a_idle', inactive)
        self.assertNotIn('usr_b_busy', inactive)
        self.assertEqual(report['totals']['not_active_count'], len(inactive))
        self.assertNotIn('not_logged_in_count', report['totals'])
        self.assertNotIn('logged_in', report['rows'][0])


class LastActiveTests(_ReportFixture):
    """`last_active` is the latest of the three Active Today sources, capped at the end
    of the report date."""

    def _al(self, profile, when):
        log = ActivityLog.objects.create(actor=profile, action='x')
        ActivityLog.objects.filter(pk=log.pk).update(timestamp=when)

    def test_latest_of_the_three_sources_wins(self):
        user, profile = self._worker('usr_la_all')
        user.last_login = _local_dt(self.today, 9)
        user.save(update_fields=['last_login'])
        self._al(profile, _local_dt(self.today, 11))
        record_transition(self._task(profile), to_status=Task.IN_PROGRESS,
                          from_status=Task.NOT_STARTED, actor=profile,
                          occurred_at=_local_dt(self.today, 15, 30))
        self.assertEqual(self._row(profile)['last_active'], _local_dt(self.today, 15, 30))

    def test_each_source_alone_sets_it(self):
        u1, login_only = self._worker('usr_la_login')
        u1.last_login = _local_dt(self.today, 8, 5)
        u1.save(update_fields=['last_login'])
        _, al_only = self._worker('usr_la_al')
        self._al(al_only, _local_dt(self.today, 10, 15))
        _, st_only = self._worker('usr_la_st')
        record_transition(self._task(st_only), to_status=Task.IN_PROGRESS,
                          from_status=Task.NOT_STARTED, actor=st_only,
                          occurred_at=_local_dt(self.today, 13, 45))
        self.assertEqual(self._row(login_only)['last_active'], _local_dt(self.today, 8, 5))
        self.assertEqual(self._row(al_only)['last_active'], _local_dt(self.today, 10, 15))
        self.assertEqual(self._row(st_only)['last_active'], _local_dt(self.today, 13, 45))

    def test_activity_after_a_past_report_date_is_ignored(self):
        user, profile = self._worker('usr_la_past')
        two_days_ago = self.today - timedelta(days=2)
        self._al(profile, _local_dt(two_days_ago, 17))
        self._al(profile, _local_dt(self.today, 10))
        user.last_login = _local_dt(self.today, 9)
        user.save(update_fields=['last_login'])
        row = self._row(profile, report_date=self.yesterday)
        self.assertEqual(row['last_active'], _local_dt(two_days_ago, 17))
        self.assertFalse(row['active_today'])

    def test_sorted_by_latest_activity_most_recent_first(self):
        _, early = self._worker('usr_sort_early')
        _, late = self._worker('usr_sort_late')
        _, old = self._worker('usr_sort_old')
        _, never = self._worker('usr_sort_never')
        self._al(early, _local_dt(self.today, 9))
        self._al(late, _local_dt(self.today, 17))
        self._al(old, _local_dt(self.today - timedelta(days=5), 20))
        order = [r['profile'].pk for r in build_user_status_rows(self.today)['rows']]
        mine = [pk for pk in order if pk in (early.pk, late.pk, old.pk, never.pk)]
        self.assertEqual(mine, [late.pk, early.pk, old.pk, never.pk])
        # No recorded activity goes after everyone who has any, the PM included.
        self.assertEqual(set(order[-2:]), {never.pk, self.pm.pk})

    def test_same_timestamp_is_broken_by_name(self):
        _, b = self._worker('usr_tie_b')
        _, a = self._worker('usr_tie_a')
        for p in (a, b):
            self._al(p, _local_dt(self.today, 11, 30))
        names = [r['name'] for r in build_user_status_rows(self.today)['rows']]
        self.assertEqual(names[:2], ['usr_tie_a', 'usr_tie_b'])

    def test_same_displayed_minute_is_broken_by_name(self):
        """b acted 40 seconds after a, but both show as 11:30, so name decides."""
        _, b = self._worker('usr_min_b')
        _, a = self._worker('usr_min_a')
        self._al(a, _local_dt(self.today, 11, 30))
        self._al(b, _local_dt(self.today, 11, 30) + timedelta(seconds=40))
        names = [r['name'] for r in build_user_status_rows(self.today)['rows']]
        self.assertEqual(names[:2], ['usr_min_a', 'usr_min_b'])

    def test_no_recorded_activity_sorts_by_name(self):
        self._worker('usr_none_b')
        self._worker('usr_none_a')
        names = [r['name'] for r in build_user_status_rows(self.today)['rows']]
        self.assertEqual(names, sorted(names, key=str.lower))

    def test_no_activity_at_all_is_none(self):
        _, profile = self._worker('usr_la_none')
        row = self._row(profile)
        self.assertIsNone(row['last_active'])
        self.assertFalse(row['active_today'])

    def test_page_shows_last_active_below_the_role(self):
        ceo_user, _ = _make_user('usr_la_ceo', 'CEO')
        _, today_p = self._worker('usr_la_today')
        self._al(today_p, _local_dt(self.today, 14, 7))
        _, old_p = self._worker('usr_la_old')
        self._al(old_p, _local_dt(self.today - timedelta(days=3), 16, 20))
        self._worker('usr_la_never')
        c = Client(SERVER_NAME='localhost')
        c.force_login(ceo_user)
        html = ' '.join(c.get(reverse('ceo_daily_report')).content.decode().split())
        # Name, then role, then the date and time with no label in front of it.
        today = self.today.strftime('%d %b %Y')
        old = (self.today - timedelta(days=3)).strftime('%d %b %Y')
        self.assertIn(f'usr_la_today</div> <div class="text-muted small">Site Engineer</div> '
                      f'<div class="text-muted small"> {today}, 14:07 </div>', html)
        self.assertIn(f'usr_la_old</div> <div class="text-muted small">Site Engineer</div> '
                      f'<div class="text-muted small"> {old}, 16:20 </div>', html)
        self.assertIn('usr_la_never</div> <div class="text-muted small">Site Engineer</div> '
                      '<div class="text-muted small"> No recorded activity </div>', html)
        self.assertNotIn('Last active', html)


class DoneTodayTests(_ReportFixture):

    def test_completed_then_reopened_today_is_not_done_today(self):
        _, profile = _make_user('usr_reopen')
        # What the human path leaves behind on Done -> Blocked: completed_at stands.
        self._task(profile, status=Task.BLOCKED, completed_at=timezone.now())
        self.assertEqual(self._row(profile)['done_today'], 0)

    def test_completed_today_and_still_done_counts(self):
        _, profile = _make_user('usr_done')
        task = self._task(profile, status=Task.DONE, completed_at=timezone.now())
        record_transition(task, to_status=Task.DONE, from_status=Task.IN_PROGRESS,
                          actor=profile)
        self.assertEqual(self._row(profile)['done_today'], 1)


class DoneBySelfOrOthersTests(_ReportFixture):
    """Done Today is split by who did the work; the other side of "by others" is
    credited to the closer as Closed for Others."""

    def setUp(self):
        super().setUp()
        _, self.se = _make_user('usr_split_se')
        # A coordinator with no task and no project link: before this split, nothing
        # on the report showed anything for them.
        _, self.coord = _make_user('usr_split_coord', 'Project Coordinator')

    def _done(self, owner, actor, submitted_by=None, ledger=True):
        task = self._task(owner, status=Task.DONE, completed_at=timezone.now(),
                          submitted_by=submitted_by)
        if ledger:
            record_transition(task, to_status=Task.DONE, from_status=Task.IN_PROGRESS,
                              actor=actor)
        return task

    def _rows(self):
        return {r['profile'].pk: r for r in build_user_status_rows(self.today)['rows']}

    def test_assignee_marks_own_task_done(self):
        self._done(self.se, self.se)
        rows = self._rows()
        self.assertEqual((rows[self.se.pk]['done_today'], rows[self.se.pk]['done_by_others']), (1, 0))
        self.assertNotIn(self.coord.pk, rows)

    def test_coordinator_closes_an_assignees_task(self):
        self._done(self.se, self.coord)
        rows = self._rows()
        self.assertEqual((rows[self.se.pk]['done_today'], rows[self.se.pk]['done_by_others']), (0, 1))
        self.assertEqual(rows[self.coord.pk]['closed_for_others'], 1)
        self.assertEqual(rows[self.coord.pk]['tasks_assigned'], 0)

    def test_opex_submitted_by_assignee_and_approved_by_pm_is_the_assignees_own(self):
        self._done(self.se, self.pm, submitted_by=self.se)
        rows = self._rows()
        self.assertEqual((rows[self.se.pk]['done_today'], rows[self.se.pk]['done_by_others']), (1, 0))
        self.assertEqual(rows[self.pm.pk]['closed_for_others'], 0)

    def test_opex_submitted_by_coordinator_credits_the_coordinator(self):
        self._done(self.se, self.pm, submitted_by=self.coord)
        rows = self._rows()
        self.assertEqual(rows[self.se.pk]['done_by_others'], 1)
        self.assertEqual(rows[self.coord.pk]['closed_for_others'], 1)
        self.assertEqual(rows[self.pm.pk]['closed_for_others'], 0)

    def test_unassigned_task_closed_by_coordinator_counts_for_them(self):
        task = Task.objects.create(
            phase=self.phase, task_name='Unassigned', task_order=1,
            assigned_role=Task.SITE_ENGINEER, task_type=Task.INTERNAL,
            status=Task.DONE, completed_at=timezone.now())
        record_transition(task, to_status=Task.DONE, from_status=Task.IN_PROGRESS,
                          actor=self.coord)
        self.assertEqual(self._rows()[self.coord.pk]['closed_for_others'], 1)

    def test_no_ledger_row_stays_with_the_assignee_and_credits_nobody(self):
        """Completions before 7 Sep 2026 have no ledger row. Calling them "by others"
        would say someone else did every task completed before September."""
        self._done(self.se, None, ledger=False)
        rows = self._rows()
        self.assertEqual((rows[self.se.pk]['done_today'], rows[self.se.pk]['done_by_others']), (1, 0))
        self.assertEqual(sum(r['closed_for_others'] for r in rows.values()), 0)

    def test_latest_done_transition_decides(self):
        """Done by the coordinator, reopened, then Done again by the assignee."""
        task = self._done(self.se, self.coord)
        record_transition(task, to_status=Task.BLOCKED, from_status=Task.DONE, actor=self.se)
        record_transition(task, to_status=Task.DONE, from_status=Task.BLOCKED, actor=self.se)
        rows = self._rows()
        self.assertEqual(rows[self.se.pk]['done_today'], 1)
        self.assertNotIn(self.coord.pk, rows)

    def test_a_task_completed_yesterday_counts_for_nobody_today(self):
        self._task(self.se, status=Task.DONE,
                   completed_at=_local_dt(self.yesterday))
        rows = self._rows()
        self.assertEqual((rows[self.se.pk]['done_today'], rows[self.se.pk]['done_by_others']), (0, 0))

    def test_by_others_and_closed_for_others_balance(self):
        """Every assigned task someone else closed appears once on each side."""
        _, se2 = _make_user('usr_split_se2')
        self._done(self.se, self.coord)
        self._done(se2, self.coord)
        self._done(se2, self.pm)
        self._done(self.se, self.se)
        report = build_user_status_rows(self.today)
        self.assertEqual(report['totals']['done_by_others'], 3)
        self.assertEqual(report['totals']['closed_for_others'], 3)
        self.assertEqual(report['totals']['done_today'], 1)
        for row in report['rows'] + [report['totals']]:
            self.assertEqual(
                row['not_started'] + row['in_progress'] + row['completed'] + row['blocked'],
                row['tasks_assigned'])

    def _page(self):
        ceo_user, _ = _make_user('usr_split_ceo', 'CEO')
        c = Client(SERVER_NAME='localhost')
        c.force_login(ceo_user)
        return ' '.join(c.get(reverse('ceo_daily_report')).content.decode().split())

    def test_page_shows_the_split_under_done_today(self):
        _, se2 = _make_user('usr_split_se2')
        self._done(self.se, self.coord)
        self._done(se2, self.coord)
        html = self._page()
        self.assertIn('+1 by others', html)
        self.assertIn('Closed 2 tasks for others', html)
        # A phrase under Done Today, not a column of its own.
        self.assertNotIn('Closed for<br>Others', html)

    def test_page_shows_no_closed_phrase_when_nobody_closed_for_others(self):
        self._done(self.se, self.se)
        html = self._page()
        # The footnote explains the phrase, so look for the rendered line itself.
        self.assertNotIn('for others</div>', html)
        self.assertNotIn('Closed 0', html)

    def test_singular_phrase(self):
        self._done(self.se, self.coord)
        self.assertIn('Closed 1 task for others', self._page())

    def test_row_sum_invariant_with_a_reopened_task(self):
        _, profile = _make_user('usr_sum')
        self._task(profile, status=Task.BLOCKED, completed_at=timezone.now())
        self._task(profile, status=Task.DONE, completed_at=timezone.now())
        self._task(profile, status=Task.IN_PROGRESS)
        self._task(profile, status=Task.NOT_STARTED)
        report = build_user_status_rows(self.today)
        for row in report['rows'] + [report['totals']]:
            self.assertEqual(
                row['not_started'] + row['in_progress'] + row['completed'] + row['blocked'],
                row['tasks_assigned'])


class QueryCountTests(_ReportFixture):

    def _count(self):
        with CaptureQueriesContext(connection) as ctx:
            build_user_status_rows(self.today)
        return len(ctx.captured_queries)

    def test_query_count_is_constant_in_user_count(self):
        for i in range(3):
            _, p = self._worker(f'usr_q{i}')
            ActivityLog.objects.create(actor=p, action='x')
        small = self._count()
        for i in range(3, 15):
            _, p = self._worker(f'usr_q{i}')
            ActivityLog.objects.create(actor=p, action='x')
            record_transition(self._task(p), to_status=Task.IN_PROGRESS,
                              from_status=Task.NOT_STARTED, actor=p)
        self.assertEqual(self._count(), small)
        self.assertEqual(small, 9)


class RenderingTests(_ReportFixture):

    def test_page_shows_active_today_and_footnote(self):
        ceo_user, _ = _make_user('usr_ceo', 'CEO')
        self._worker('usr_page')
        c = Client(SERVER_NAME='localhost')
        c.force_login(ceo_user)
        html = ' '.join(c.get(reverse('ceo_daily_report')).content.decode().split())
        self.assertIn('Active<br>Today', html)
        self.assertIn('<th>User<br><span class="fw-normal text-muted small">(latest activity)</span></th>', html)
        # The fixed header needs the table to scroll inside its own box.
        self.assertIn('class="table-responsive-xl report-table-wrap"', html)
        self.assertIn('position: sticky', html)
        self.assertIn('2 not active', html)
        self.assertIn(FOOTNOTE, html)
        self.assertNotIn('Logged', html)
        self.assertNotIn('logged in', html.replace(FOOTNOTE, '').lower())

    def test_email_bodies_show_active_today_and_footnote(self):
        self._worker('usr_mail')
        ctx = {
            'show_ceo_sections': True,
            'ceo': {'invoiced_today': 0, 'paid_today': 0, 'deliveries_today': 0,
                    'most_active': None},
            'user_status': build_user_status_rows(self.today),
            'date_str': 'today', 'metrics': {},
        }
        html = ' '.join(render_to_string('projects/email/eod_digest.html', ctx).split())
        txt = render_to_string('projects/email/eod_digest.txt', ctx)
        for body in (html, ' '.join(txt.split())):
            self.assertIn(FOOTNOTE, body)
            self.assertNotIn('logged in', body.replace(FOOTNOTE, '').lower())
        self.assertIn('Active<br>Today', html)
        self.assertIn('active today:', txt)
        self.assertIn('not active', txt)
