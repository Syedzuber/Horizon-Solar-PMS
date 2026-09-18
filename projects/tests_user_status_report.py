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

    def test_not_active_count_and_sort(self):
        _, idle = self._worker('usr_a_idle')
        _, busy = self._worker('usr_b_busy')
        ActivityLog.objects.create(actor=busy, action='busy')
        report = build_user_status_rows(self.today)
        names = [r['name'] for r in report['rows']]
        # Same overdue (0) for all: inactive first, then by name.
        inactive = [r['name'] for r in report['rows'] if not r['active_today']]
        self.assertEqual(names[:len(inactive)], sorted(inactive, key=str.lower))
        self.assertLess(names.index('usr_a_idle'), names.index('usr_b_busy'))
        self.assertEqual(report['totals']['not_active_count'], len(inactive))
        self.assertNotIn('not_logged_in_count', report['totals'])
        self.assertNotIn('logged_in', report['rows'][0])


class DoneTodayTests(_ReportFixture):

    def test_completed_then_reopened_today_is_not_done_today(self):
        _, profile = _make_user('usr_reopen')
        # What the human path leaves behind on Done -> Blocked: completed_at stands.
        self._task(profile, status=Task.BLOCKED, completed_at=timezone.now())
        self.assertEqual(self._row(profile)['done_today'], 0)

    def test_completed_today_and_still_done_counts(self):
        _, profile = _make_user('usr_done')
        self._task(profile, status=Task.DONE, completed_at=timezone.now())
        self.assertEqual(self._row(profile)['done_today'], 1)

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
        self.assertEqual(small, 8)


class RenderingTests(_ReportFixture):

    def test_page_shows_active_today_and_footnote(self):
        ceo_user, _ = _make_user('usr_ceo', 'CEO')
        self._worker('usr_page')
        c = Client(SERVER_NAME='localhost')
        c.force_login(ceo_user)
        html = ' '.join(c.get(reverse('ceo_daily_report')).content.decode().split())
        self.assertIn('Active<br>Today', html)
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
