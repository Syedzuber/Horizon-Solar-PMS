"""
The daily morning task report: projects.task_report, the send_morning_task_report
command, and permissions.managed_project_ids_by_profile().

THE RULES, each by a test class named for it
    ManagedProjectIdsTests    the bulk helper equals manageable_projects_q() for every
                              active profile, drops inactive people, costs two queries.
    ScopeExclusionTests       Done, Not Applicable, unassigned, inactive-assignee,
                              test-project, deleted, Draft, On Hold and Commissioned work
                              is in no count; External work is.
    BoundaryDateTests         yesterday is delayed, today is due today, tomorrow is
                              neither; a mistyped year is delayed and says "check date".
    ScopePrecedenceTests      one scope per person, the first rule that matches; a PM
                              gets one report with their own tasks inside it; all-zero
                              people are not in the result.
    ManagerOwnTasksTests      "your projects and tasks" is a union: a manager's own task
                              on a project they do not manage is counted once, and their
                              own task on a project they do manage is not counted twice.
    ListTests                 delayed oldest first, both lists capped at 20 rows while
                              the counts are not; the link path follows the counts.
    SystemAdminExclusionTests a System Admin never receives a report, whatever they hold
                              or manage, but their tasks still count for their project's
                              manager and for management; CEO and Admin are unchanged.
    MirrorTests               an ASSIGNED mirror task is counted like any other: for its
                              assignee, for a manager of its project and for management;
                              a person whose only open tasks are mirrors gets a report.
    QueryCountTests           five queries, with exactly three users or thirty.
    CommandSendTests          the seven WhatsApp parameters in order, the email subject
                              and columns, a second run sends nothing, a failed WhatsApp
                              is retried without repeating the email.
    CommandSafetyTests        the dry run writes nothing and honours today's sent log;
                              the interlock; --to; the circuit breaker.

Run with:
    python manage.py test projects.tests_morning_task_report --settings=solarpms.test_settings
"""
from datetime import date, timedelta
from decimal import Decimal
from io import StringIO
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from .management.commands import send_morning_task_report as command_module
from .models import NotificationLog, Project, ProjectPhase, SystemSettings, Task, UserProfile
from .permissions import managed_project_ids_by_profile, manageable_projects_q
from .task_report import (
    CHECK_DATE, LIST_LIMIT, MORNING_REPORT_EXCLUDED_ROLES, SCOPE_ALL, SCOPE_PROJECTS,
    SCOPE_TASKS, build_task_reports,
)

#: A statement that changes data. SAVEPOINT / RELEASE (the TestCase wrapper) are not.
WRITE_KEYWORDS = ('INSERT', 'UPDATE', 'DELETE', 'CREATE', 'ALTER', 'DROP', 'TRUNCATE')

TEMPLATE = command_module.TEMPLATE


def _person(username, role, active=True, user_active=True):
    """A user and their profile. signals.py creates the profile on post_save, so this
    updates it rather than creating one."""
    user = User.objects.create_user(
        username=username, password='x', email=f'{username}@example.com',
        first_name=username.title(), last_name='Test', is_active=user_active)
    profile = user.profile
    profile.role = role
    profile.is_active = active
    profile.phone_number = '9876500000'
    profile.save()
    return profile


def _project(name, pm=None, status='Active', activated=True, is_test=False,
             is_deleted=False, coordinators=()):
    project = Project.objects.create(
        customer_name=name, customer_phone='9876543210', site_address='1 Report Road',
        city='Lucknow', project_type='Residential', dc_capacity_kw=Decimal('10.00'),
        status=status, assigned_pm=pm, is_test=is_test, is_deleted=is_deleted,
        activated_at=timezone.now() if activated else None)
    if coordinators:
        project.coordinators.set(coordinators)
    phase = ProjectPhase.objects.create(project=project, phase_name='Only', phase_order=1)
    return project, phase


_order = iter(range(1, 100000))


def _task(phase, assignee, due=None, status=Task.NOT_STARTED, name=None, **extra):
    order = next(_order)
    return Task.objects.create(
        phase=phase, task_name=name or f'Task {order}', task_order=order, status=status,
        assigned_to=assignee, due_date=due, **extra)


def _by_user(reports):
    return {r['profile'].user.username: r for r in reports}


def _counts(report):
    return (report['delayed'], report['due_today'], report['no_due_date'])


class _Dates:
    def setUp(self):
        super().setUp()
        self.today = timezone.localdate()
        self.yesterday = self.today - timedelta(days=1)
        self.tomorrow = self.today + timedelta(days=1)


# ---------------------------------------------------------------------------
# permissions.managed_project_ids_by_profile
# ---------------------------------------------------------------------------

class ManagedProjectIdsTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.pm = _person('pm1', 'PM')
        cls.pm2 = _person('pm2', 'PM')
        cls.coord = _person('coord1', 'Project Coordinator')
        cls.gone_coord = _person('coord2', 'Project Coordinator', active=False)
        cls.gone_user_coord = _person('coord3', 'Project Coordinator', user_active=False)
        cls.gone_pm = _person('pm3', 'PM', active=False)
        cls.se = _person('se1', 'Site Engineer')
        _project('A', pm=cls.pm, coordinators=[cls.coord, cls.gone_coord])
        _project('B', pm=cls.pm, coordinators=[cls.gone_user_coord])
        _project('C', pm=cls.pm2, coordinators=[cls.coord])
        _project('D', pm=cls.gone_pm)
        # Ownership only: a deleted, Draft or test project is still managed.
        _project('E', pm=cls.pm2, status='Draft', activated=False, is_deleted=True,
                 is_test=True)
        _project('F')  # nobody

    def test_equals_manageable_projects_q_for_every_active_profile(self):
        managed = managed_project_ids_by_profile()
        active = UserProfile.objects.filter(is_active=True, user__is_active=True)
        self.assertGreater(active.count(), 3)
        for profile in active:
            expected = set(Project.objects.filter(manageable_projects_q(profile))
                           .distinct().values_list('pk', flat=True))
            self.assertEqual(managed.get(profile.pk, set()), expected, profile.user.username)

    def test_inactive_people_are_absent(self):
        managed = managed_project_ids_by_profile()
        for profile in (self.gone_coord, self.gone_user_coord, self.gone_pm):
            self.assertNotIn(profile.pk, managed)
        self.assertNotIn(self.se.pk, managed)

    def test_two_queries_whatever_the_head_count(self):
        with self.assertNumQueries(2):
            managed_project_ids_by_profile()
        for n in range(10):
            _project(f'Extra {n}', pm=_person(f'xpm{n}', 'PM'),
                     coordinators=[_person(f'xco{n}', 'Project Coordinator')])
        with self.assertNumQueries(2):
            managed_project_ids_by_profile()


# ---------------------------------------------------------------------------
# What is counted
# ---------------------------------------------------------------------------

class ScopeExclusionTests(_Dates, TestCase):

    def setUp(self):
        super().setUp()
        self.se = _person('se', 'Site Engineer')
        _, self.phase = _project('Live')

    def _se_counts(self):
        report = _by_user(build_task_reports(self.today)).get('se')
        return _counts(report) if report else (0, 0, 0)

    def test_one_open_delayed_task_counts(self):
        _task(self.phase, self.se, due=self.yesterday)
        self.assertEqual(self._se_counts(), (1, 0, 0))

    def test_done_is_not_counted(self):
        _task(self.phase, self.se, due=self.yesterday, status=Task.DONE)
        _task(self.phase, self.se, due=self.today, status=Task.DONE)
        _task(self.phase, self.se, status=Task.DONE)
        self.assertEqual(self._se_counts(), (0, 0, 0))

    def test_not_applicable_is_not_counted(self):
        _task(self.phase, self.se, due=self.yesterday, is_not_applicable=True)
        _task(self.phase, self.se, due=self.today, is_not_applicable=True)
        _task(self.phase, self.se, is_not_applicable=True)
        self.assertEqual(self._se_counts(), (0, 0, 0))

    def test_unassigned_is_not_counted_even_for_the_ceo(self):
        _person('boss', 'CEO')
        _task(self.phase, None, due=self.yesterday)
        self.assertNotIn('boss', _by_user(build_task_reports(self.today)))

    def test_inactive_assignee_is_not_counted_anywhere(self):
        pm = _person('pm', 'PM')
        _, phase = _project('Managed', pm=pm)
        gone = _person('gone', 'Site Engineer', active=False)
        gone_user = _person('gone2', 'Site Engineer', user_active=False)
        _task(phase, gone, due=self.yesterday)
        _task(phase, gone_user, due=self.yesterday)
        _person('boss', 'CEO')
        reports = _by_user(build_task_reports(self.today))
        self.assertNotIn('gone', reports)
        self.assertNotIn('gone2', reports)
        self.assertNotIn('pm', reports)       # their project's only tasks are excluded
        self.assertNotIn('boss', reports)

    def test_projects_outside_the_live_set_are_not_counted(self):
        for kwargs in ({'is_test': True}, {'is_deleted': True},
                       {'status': 'Draft', 'activated': False},
                       {'status': 'Active', 'activated': False},
                       {'status': 'On Hold'}, {'status': 'Commissioned'},
                       {'status': 'Cancelled'}):
            _, phase = _project(f'Out {kwargs}', **kwargs)
            _task(phase, self.se, due=self.yesterday)
        self.assertEqual(self._se_counts(), (0, 0, 0))

    def test_in_progress_project_is_counted(self):
        _, phase = _project('Busy', status='In Progress')
        _task(phase, self.se, due=self.yesterday)
        self.assertEqual(self._se_counts(), (1, 0, 0))

    def test_external_and_blocked_tasks_are_counted(self):
        _task(self.phase, self.se, due=self.yesterday, task_type=Task.EXTERNAL)
        _task(self.phase, self.se, due=self.yesterday, status=Task.BLOCKED)
        _task(self.phase, self.se, due=self.today, status=Task.IN_PROGRESS)
        self.assertEqual(self._se_counts(), (2, 1, 0))


class BoundaryDateTests(_Dates, TestCase):

    def setUp(self):
        super().setUp()
        self.se = _person('se', 'Site Engineer')
        _, self.phase = _project('Live')

    def test_yesterday_today_tomorrow(self):
        _task(self.phase, self.se, due=self.yesterday, name='Late')
        _task(self.phase, self.se, due=self.today, name='Now')
        _task(self.phase, self.se, due=self.tomorrow, name='Later')
        _task(self.phase, self.se, name='Undated')
        report = _by_user(build_task_reports(self.today))['se']
        self.assertEqual(_counts(report), (1, 1, 1))
        self.assertEqual([r['task'] for r in report['delayed_tasks']], ['Late'])
        self.assertEqual([r['task'] for r in report['due_today_tasks']], ['Now'])
        self.assertEqual(report['delayed_tasks'][0]['days_late'], 1)

    def test_only_a_future_task_is_all_zero_and_skipped(self):
        _task(self.phase, self.se, due=self.tomorrow)
        self.assertEqual(build_task_reports(self.today), [])

    def test_a_mistyped_year_is_delayed_and_says_check_date(self):
        _task(self.phase, self.se, due=date(20, 9, 24))
        report = _by_user(build_task_reports(self.today))['se']
        self.assertEqual(report['delayed'], 1)
        self.assertEqual(report['delayed_tasks'][0]['days_late'], CHECK_DATE)


# ---------------------------------------------------------------------------
# Who gets which scope
# ---------------------------------------------------------------------------

class ScopePrecedenceTests(_Dates, TestCase):

    def setUp(self):
        super().setUp()
        self.ceo = _person('ceo', 'CEO')
        self.admin = _person('admin', 'Admin')
        self.sysadmin = _person('sysadmin', 'System Admin')
        self.pm = _person('pm', 'PM')
        self.coord = _person('coord', 'Project Coordinator')
        self.se = _person('se', 'Site Engineer')
        self.idle = _person('idle', 'Finance')
        _, self.mine = _project('PM project', pm=self.pm, coordinators=[self.coord])
        _, self.other = _project('Other project')
        _task(self.mine, self.pm, due=self.yesterday)        # the PM's own task
        _task(self.mine, self.se, due=self.today)            # the SE's, on the PM's project
        _task(self.other, self.se, due=self.yesterday)       # the SE's, elsewhere
        _task(self.other, self.pm, due=self.yesterday)       # the PM's, not on their project

    def test_each_person_gets_one_scope(self):
        reports = build_task_reports(self.today)
        names = [r['profile'].user.username for r in reports]
        self.assertEqual(len(names), len(set(names)))
        by = _by_user(reports)
        self.assertEqual(by['ceo']['scope'], SCOPE_ALL)
        self.assertEqual(by['admin']['scope'], SCOPE_ALL)
        self.assertNotIn('sysadmin', by)              # never a recipient (SystemAdminExclusionTests)
        self.assertEqual(by['pm']['scope'], SCOPE_PROJECTS)
        self.assertEqual(by['coord']['scope'], SCOPE_PROJECTS)
        self.assertEqual(by['se']['scope'], SCOPE_TASKS)

    def test_counts_per_scope(self):
        by = _by_user(build_task_reports(self.today))
        self.assertEqual(_counts(by['ceo']), (3, 1, 0))
        # Every in-scope task on the PM's project (their own included), plus their own
        # task on the project they do not manage. The SE's task there is not theirs.
        self.assertEqual(_counts(by['pm']), (2, 1, 0))
        self.assertEqual(_counts(by['coord']), (1, 1, 0))
        self.assertEqual(_counts(by['se']), (1, 1, 0))

    def test_all_zero_people_are_skipped(self):
        self.assertNotIn('idle', _by_user(build_task_reports(self.today)))

    def test_assigned_to_column_only_where_tasks_can_be_others(self):
        by = _by_user(build_task_reports(self.today))
        self.assertTrue(by['ceo']['show_assignee'])
        self.assertTrue(by['pm']['show_assignee'])
        self.assertFalse(by['se']['show_assignee'])

    def test_a_ceo_who_manages_a_project_stays_all_active_projects(self):
        _project('CEO project', pm=self.ceo)
        self.assertEqual(_by_user(build_task_reports(self.today))['ceo']['scope'], SCOPE_ALL)


class ListTests(_Dates, TestCase):

    def setUp(self):
        super().setUp()
        self.se = _person('se', 'Site Engineer')
        _, self.phase = _project('Live')

    def test_delayed_oldest_first_and_capped_while_counts_are_not(self):
        for n in range(LIST_LIMIT + 5):
            _task(self.phase, self.se, due=self.today - timedelta(days=n + 1),
                  name=f'late {n + 1}')
        for _ in range(LIST_LIMIT + 3):
            _task(self.phase, self.se, due=self.today)
        report = _by_user(build_task_reports(self.today))['se']
        self.assertEqual(report['delayed'], LIST_LIMIT + 5)
        self.assertEqual(report['due_today'], LIST_LIMIT + 3)
        self.assertEqual(len(report['delayed_tasks']), LIST_LIMIT)
        self.assertEqual(len(report['due_today_tasks']), LIST_LIMIT)
        days = [r['days_late'] for r in report['delayed_tasks']]
        self.assertEqual(days, sorted(days, reverse=True))
        self.assertEqual(days[0], LIST_LIMIT + 5)

    def test_link_path_follows_the_counts(self):
        _task(self.phase, self.se)
        self.assertEqual(_by_user(build_task_reports(self.today))['se']['link_path'], '/')
        _task(self.phase, self.se, due=self.today)
        self.assertEqual(_by_user(build_task_reports(self.today))['se']['link_path'],
                         '/tasks/due-today/')
        _task(self.phase, self.se, due=self.yesterday)
        self.assertEqual(_by_user(build_task_reports(self.today))['se']['link_path'],
                         '/tasks/overdue/')


class ManagerOwnTasksTests(_Dates, TestCase):
    """A manager's scope is the projects they manage UNION their own tasks elsewhere."""

    def setUp(self):
        super().setUp()
        self.pm = _person('pm', 'PM')
        self.coord = _person('coord', 'Project Coordinator')
        self.se = _person('se', 'Site Engineer')
        _, self.managed = _project('Managed', pm=self.pm, coordinators=[self.coord])
        _, self.elsewhere = _project('Elsewhere')

    def _report(self, username):
        return _by_user(build_task_reports(self.today))[username]

    def test_own_task_on_a_project_they_do_not_manage_is_counted_once(self):
        _task(self.elsewhere, self.pm, due=self.yesterday, name='PM elsewhere')
        _task(self.elsewhere, self.coord, due=self.today, name='Coord elsewhere')
        _task(self.elsewhere, self.se, due=self.yesterday, name='SE elsewhere')
        pm = self._report('pm')
        self.assertEqual(pm['scope'], SCOPE_PROJECTS)
        self.assertEqual(SCOPE_PROJECTS, 'your projects and tasks')
        self.assertEqual(_counts(pm), (1, 0, 0))
        self.assertEqual([r['task'] for r in pm['delayed_tasks']], ['PM elsewhere'])
        coord = self._report('coord')
        self.assertEqual(_counts(coord), (0, 1, 0))
        self.assertEqual([r['task'] for r in coord['due_today_tasks']], ['Coord elsewhere'])

    def test_own_task_on_a_managed_project_is_not_counted_twice(self):
        _task(self.managed, self.pm, due=self.yesterday, name='PM own, managed')
        _task(self.managed, self.pm, name='PM undated, managed')
        _task(self.managed, self.se, due=self.today, name='SE on managed')
        pm = self._report('pm')
        self.assertEqual(_counts(pm), (1, 1, 1))
        self.assertEqual([r['task'] for r in pm['delayed_tasks']], ['PM own, managed'])
        self.assertEqual([r['task'] for r in pm['due_today_tasks']], ['SE on managed'])

    def test_own_tasks_both_sides_add_up_once_each(self):
        _task(self.managed, self.pm, due=self.yesterday)
        _task(self.elsewhere, self.pm, due=self.yesterday)
        _task(self.elsewhere, self.pm)
        self.assertEqual(_counts(self._report('pm')), (2, 0, 1))


class SystemAdminExclusionTests(_Dates, TestCase):
    """MORNING_REPORT_EXCLUDED_ROLES decides who RECEIVES a report, not what is counted."""

    def setUp(self):
        super().setUp()
        self.ceo = _person('ceo', 'CEO')
        self.admin = _person('admin', 'Admin')
        self.pm = _person('pm', 'PM')
        self.sysadmin = _person('sysadmin', 'System Admin')
        _, self.phase = _project('PM project', pm=self.pm)
        _task(self.phase, self.sysadmin, due=self.yesterday, name='Sysadmin late')
        _task(self.phase, self.sysadmin, due=self.today, name='Sysadmin today')
        _task(self.phase, self.sysadmin, name='Sysadmin undated')

    def test_the_constant_is_the_stored_role_value(self):
        self.assertEqual(MORNING_REPORT_EXCLUDED_ROLES, frozenset({'System Admin'}))
        stored = {value for value, _ in UserProfile.ROLE_CHOICES}
        self.assertLessEqual(MORNING_REPORT_EXCLUDED_ROLES, stored)

    def test_a_system_admin_with_tasks_gets_no_report(self):
        self.assertNotIn('sysadmin', _by_user(build_task_reports(self.today)))

    def test_a_system_admin_who_manages_a_project_gets_no_report(self):
        _project('Sysadmin project', pm=self.sysadmin)
        self.assertNotIn('sysadmin', _by_user(build_task_reports(self.today)))

    def test_their_tasks_still_count_for_the_manager_and_management(self):
        by = _by_user(build_task_reports(self.today))
        for username, scope in (('pm', SCOPE_PROJECTS), ('ceo', SCOPE_ALL),
                                ('admin', SCOPE_ALL)):
            self.assertEqual(by[username]['scope'], scope, username)
            self.assertEqual(_counts(by[username]), (1, 1, 1), username)
            self.assertEqual([r['task'] for r in by[username]['delayed_tasks']],
                             ['Sysadmin late'], username)
            self.assertEqual([r['assigned_to'] for r in by[username]['due_today_tasks']],
                             ['Sysadmin Test'], username)

    def test_ceo_and_admin_still_receive_all_active_projects(self):
        by = _by_user(build_task_reports(self.today))
        self.assertEqual(by['ceo']['scope'], SCOPE_ALL)
        self.assertEqual(by['admin']['scope'], SCOPE_ALL)


class MirrorTests(_Dates, TestCase):
    """Production assigns delivery mirror tasks to people and the task table shows them
    as delayed under that person's name, so the report counts them too."""

    def setUp(self):
        super().setUp()
        self.ceo = _person('ceo', 'CEO')
        self.pm = _person('pm', 'PM')
        self.se = _person('se', 'Site Engineer')
        _, self.phase = _project('Delivery site', pm=self.pm)

    def test_an_assigned_late_mirror_is_delayed_for_assignee_manager_and_management(self):
        _task(self.phase, self.se, due=self.yesterday, is_mirror=True,
              name='Delivery of Module')
        by = _by_user(build_task_reports(self.today))
        self.assertEqual(by['se']['scope'], SCOPE_TASKS)
        self.assertEqual(by['pm']['scope'], SCOPE_PROJECTS)
        self.assertEqual(by['ceo']['scope'], SCOPE_ALL)
        for username in ('se', 'pm', 'ceo'):
            self.assertEqual(_counts(by[username]), (1, 0, 0), username)
            self.assertEqual([r['task'] for r in by[username]['delayed_tasks']],
                             ['Delivery of Module'], username)

    def test_a_person_whose_only_open_tasks_are_mirrors_gets_a_report(self):
        only_mirrors = _person('mirrorman', 'SCM')
        _task(self.phase, only_mirrors, due=self.yesterday, is_mirror=True)
        _task(self.phase, only_mirrors, due=self.today, is_mirror=True)
        _task(self.phase, only_mirrors, is_mirror=True)
        _task(self.phase, only_mirrors, due=self.yesterday, is_mirror=True,
              status=Task.DONE)                       # Done: still not counted
        report = _by_user(build_task_reports(self.today))['mirrorman']
        self.assertEqual(report['scope'], SCOPE_TASKS)
        self.assertEqual(_counts(report), (1, 1, 1))

    def test_an_unassigned_mirror_is_still_not_counted(self):
        _task(self.phase, None, due=self.yesterday, is_mirror=True)
        self.assertEqual(build_task_reports(self.today), [])


class QueryCountTests(_Dates, TestCase):
    """Exactly 3 users, then exactly 30, each group being a PM and a coordinator who both
    hold a task on a project they do not manage (the union's second half) and a Site
    Engineer on the managed project, whose late task there is an assigned mirror."""

    def _add_group(self, tag):
        pm = _person(f'{tag}pm', 'PM')
        coord = _person(f'{tag}co', 'Project Coordinator')
        se = _person(f'{tag}se', 'Site Engineer')
        _, managed = _project(f'{tag} managed', pm=pm, coordinators=[coord])
        _, elsewhere = _project(f'{tag} elsewhere')
        _task(managed, se, due=self.yesterday, is_mirror=True)
        _task(managed, se, due=self.today)
        _task(elsewhere, pm, due=self.yesterday)
        _task(elsewhere, coord)

    def _queries(self):
        with CaptureQueriesContext(connection) as captured:
            reports = build_task_reports(self.today)
        return len(captured), reports

    def test_same_query_count_at_three_users_and_thirty(self):
        self._add_group('g0')
        self.assertEqual(UserProfile.objects.count(), 3)
        small_queries, small = self._queries()
        for n in range(1, 10):
            self._add_group(f'g{n}')
        self.assertEqual(UserProfile.objects.count(), 30)
        large_queries, large = self._queries()
        self.assertEqual(small_queries, large_queries)
        self.assertEqual(large_queries, 5)
        self.assertEqual((len(small), len(large)), (3, 30))
        # The union and the mirror really ran at scale: each PM's 2 delayed are the SE's
        # mirror on their project plus their own from elsewhere; each coordinator's 1
        # undated is theirs; each SE's 1 delayed is the mirror.
        pms = [r for r in large if r['role'] == 'PM']
        coords = [r for r in large if r['role'] == 'Project Coordinator']
        ses = [r for r in large if r['role'] == 'Site Engineer']
        self.assertEqual((len(pms), len(coords), len(ses)), (10, 10, 10))
        self.assertTrue(all(_counts(r) == (2, 1, 0) for r in pms))
        self.assertTrue(all(_counts(r) == (1, 1, 1) for r in coords))
        self.assertTrue(all(_counts(r) == (1, 1, 0) for r in ses))

    def _add_sysadmin_group(self, tag):
        pm = _person(f'{tag}pm', 'PM')
        se = _person(f'{tag}se', 'Site Engineer')
        sysadmin = _person(f'{tag}sa', 'System Admin')
        _, managed = _project(f'{tag} managed', pm=pm)
        _project(f'{tag} sysadmin managed', pm=sysadmin)
        _task(managed, sysadmin, due=self.yesterday)
        _task(managed, se, due=self.today)

    def test_same_query_count_at_three_and_thirty_users_with_system_admins(self):
        self._add_sysadmin_group('s0')
        self.assertEqual(UserProfile.objects.count(), 3)
        small_queries, small = self._queries()
        for n in range(1, 10):
            self._add_sysadmin_group(f's{n}')
        self.assertEqual(UserProfile.objects.count(), 30)
        large_queries, large = self._queries()
        self.assertEqual((small_queries, large_queries), (5, 5))
        # 1 and 10 System Admins left out of the recipients; their tasks still counted.
        self.assertEqual((len(small), len(large)), (2, 20))
        self.assertFalse(any(r['role'] == 'System Admin' for r in large))
        self.assertTrue(all(_counts(r) == (1, 1, 0) for r in large if r['role'] == 'PM'))


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------

def _ok_response():
    response = MagicMock(status_code=200, text='{"id": "msg-1"}')
    response.json.return_value = {'id': 'msg-1'}
    return response


@override_settings(INTERAKT_API_KEY='test-interakt', ZEPTOMAIL_API_KEY='test-zepto',
                   ZEPTOMAIL_FROM_EMAIL='noreply@example.com',
                   APP_BASE_URL='https://pms.example.com')
class _CommandFixture(_Dates, TestCase):
    """A CEO, a PM with their own task, and a Site Engineer, all with non-zero counts.
    Both master switches are ON and requests.post is patched, so send_notification runs
    for real and writes real NotificationLog rows, and no HTTP leaves the test."""

    def setUp(self):
        super().setUp()
        self.ceo = _person('ceo', 'CEO')
        self.pm = _person('pm', 'PM')
        self.se = _person('se', 'Site Engineer')
        _, phase = _project('Live', pm=self.pm)
        _task(phase, self.pm, due=self.yesterday, name='PM own task')
        _task(phase, self.se, due=self.today, name='SE task')
        _task(phase, self.se)
        SystemSettings.objects.update_or_create(
            pk=1, defaults={'email_enabled': True, 'whatsapp_enabled': True})
        patcher = patch('projects.notifications.requests.post', return_value=_ok_response())
        self.post = patcher.start()
        self.addCleanup(patcher.stop)

    def _call(self, *args):
        out, err = StringIO(), StringIO()
        code = None
        try:
            call_command('send_morning_task_report', *args, stdout=out, stderr=err)
        except SystemExit as exc:
            code = exc.code
        return code, out.getvalue(), err.getvalue()

    def _posts(self, host):
        return [c for c in self.post.call_args_list if host in c.args[0]]

    def _sent(self, **filters):
        return NotificationLog.objects.filter(template_name=TEMPLATE, status='sent', **filters)


class CommandSendTests(_CommandFixture):

    def test_whatsapp_params_are_seven_in_order(self):
        with patch.object(command_module, 'send_notification') as send:
            self._call('--only-user', 'pm', '--i-am-sending-to-real-people')
        self.assertEqual(send.call_count, 1)
        kwargs = send.call_args.kwargs
        self.assertEqual(kwargs['template'], 'daily_task_report')
        self.assertEqual(kwargs['channels'], ['email', 'whatsapp'])
        params = kwargs['template_params']
        self.assertEqual(len(params), 7)
        self.assertEqual(params, [
            self.today.strftime('%d %b %Y'), 'Pm', 'your projects and tasks', '1', '1', '1',
            'https://pms.example.com/tasks/overdue/'])
        self.assertTrue(all(isinstance(p, str) for p in params))
        self.assertEqual(kwargs['subject'],
                         f"Task report {self.today.strftime('%d %b %Y')}: 1 delayed, 1 due today")

    def test_interakt_payload_splits_header_and_body(self):
        self._call('--only-user', 'se', '--i-am-sending-to-real-people')
        (interakt,) = self._posts('interakt')
        template = interakt.kwargs['json']['template']
        self.assertEqual(template['name'], 'daily_task_report')
        self.assertEqual(len(template['headerValues']), 1)
        self.assertEqual(len(template['bodyValues']), 6)

    def test_email_columns_and_footer(self):
        self._call('--i-am-sending-to-real-people', '--no-whatsapp')
        emails = {c.kwargs['json']['to'][0]['email_address']['address']: c.kwargs['json']
                  for c in self._posts('zeptomail')}
        pm_html = emails['pm@example.com']['htmlbody']
        se_html = emails['se@example.com']['htmlbody']
        self.assertIn('Assigned to', pm_html)
        self.assertNotIn('Assigned to', se_html)
        for body in (pm_html, emails['pm@example.com']['textbody']):
            self.assertIn('Counts include external steps such as DISCOM', body)
            self.assertIn('The linked page may show a slightly different set of tasks', body)
            self.assertIn('PM own task', body)
        for banned in ('display:flex', 'display: flex', 'display:grid', 'display: grid',
                       '<style', '<link', '<!--'):
            self.assertNotIn(banned, pm_html)

    def test_second_run_sends_nothing(self):
        self._call('--i-am-sending-to-real-people')
        first = self.post.call_count
        self.assertEqual(first, 6)                 # 3 people x 2 channels
        self.assertEqual(self._sent().count(), 6)
        code, out, _ = self._call('--i-am-sending-to-real-people')
        self.assertIsNone(code)
        self.assertEqual(self.post.call_count, first)
        self.assertEqual(self._sent().count(), 6)
        self.assertEqual(out.count('already sent today'), 3)

    def test_failed_whatsapp_is_retried_without_repeating_the_email(self):
        failing = MagicMock(status_code=500, text='boom')
        self.post.side_effect = lambda url, **kw: failing if 'interakt' in url else _ok_response()
        self._call('--only-user', 'se', '--i-am-sending-to-real-people')
        self.assertEqual(self._sent(recipient=self.se, channel='email').count(), 1)
        self.assertEqual(self._sent(recipient=self.se, channel='whatsapp').count(), 0)
        self.post.side_effect = None
        self.post.reset_mock()
        self._call('--only-user', 'se', '--i-am-sending-to-real-people')
        self.assertEqual(len(self._posts('zeptomail')), 0)
        self.assertEqual(len(self._posts('interakt')), 1)
        self.assertEqual(self._sent(recipient=self.se).count(), 2)

    def test_a_sent_log_from_yesterday_does_not_block_today(self):
        log = NotificationLog.objects.create(recipient=self.se, channel='email',
                                             status='sent', message='x',
                                             template_name=TEMPLATE)
        NotificationLog.objects.filter(pk=log.pk).update(
            created_at=timezone.now() - timedelta(days=1))
        self._call('--only-user', 'se', '--i-am-sending-to-real-people', '--no-whatsapp')
        self.assertEqual(len(self._posts('zeptomail')), 1)


class CommandSafetyTests(_CommandFixture):

    def test_dry_run_writes_nothing_and_sends_nothing(self):
        with CaptureQueriesContext(connection) as captured:
            code, out, _ = self._call('--dry-run')
        writes = [q['sql'] for q in captured
                  if q['sql'].lstrip().upper().startswith(WRITE_KEYWORDS)]
        self.assertEqual(writes, [])
        self.assertIsNone(code)
        self.assertEqual(self.post.call_count, 0)
        self.assertIn('recipients=3 emails=3 whatsapps=3', out)
        self.assertIn('[db] host=', out)

    def test_dry_run_skips_a_recipient_already_sent_today(self):
        for channel in ('email', 'whatsapp'):
            NotificationLog.objects.create(recipient=self.se, channel=channel, status='sent',
                                           message='x', template_name=TEMPLATE)
        for _ in range(2):
            _, out, _ = self._call('--dry-run')
            se_line = next(line for line in out.splitlines() if line.startswith('se '))
            self.assertIn('skip (already sent today)', se_line)
            self.assertIn('recipients=3 emails=2 whatsapps=2', out)

    def test_a_failed_or_skipped_log_does_not_count_as_sent(self):
        NotificationLog.objects.create(recipient=self.se, channel='email', status='failed',
                                       message='x', template_name=TEMPLATE)
        NotificationLog.objects.create(recipient=self.se, channel='whatsapp',
                                       status='skipped', message='x', template_name=TEMPLATE)
        _, out, _ = self._call('--dry-run')
        self.assertIn('recipients=3 emails=3 whatsapps=3', out)

    def test_real_send_without_the_flag_refuses(self):
        code, _, err = self._call()
        self.assertEqual(code, 1)
        self.assertIn('REFUSING TO SEND', err)
        self.assertIn('ceo (CEO)', err)
        self.assertEqual(self.post.call_count, 0)
        self.assertFalse(NotificationLog.objects.exists())

    def test_to_sends_only_the_email_to_the_typed_address(self):
        with patch.object(command_module, 'send_aggregate_email') as aggregate:
            code, _, _ = self._call('--only-user', 'pm', '--to', 'me@example.com')
        self.assertIsNone(code)
        self.assertEqual(aggregate.call_count, 1)
        kwargs = aggregate.call_args.kwargs
        self.assertEqual(kwargs['to_email'], 'me@example.com')
        self.assertIsNone(kwargs['log_recipient'])
        self.assertIn('PM own task', kwargs['html_body'])
        self.assertEqual(self.post.call_count, 0)

    def test_to_through_the_real_sender_leaves_no_row_under_the_user(self):
        self._call('--only-user', 'pm', '--to', 'me@example.com')
        (email,) = self._posts('zeptomail')
        self.assertEqual(email.kwargs['json']['to'][0]['email_address']['address'],
                         'me@example.com')
        self.assertFalse(NotificationLog.objects.exists())
        self.assertEqual(self._posts('interakt'), [])

    def test_to_needs_only_user(self):
        with self.assertRaises(CommandError):
            call_command('send_morning_task_report', '--to', 'me@example.com',
                         stdout=StringIO(), stderr=StringIO())

    def test_circuit_breaker_aborts_before_any_send(self):
        with patch.object(command_module, 'MAX_RECIPIENTS', 2):
            with self.assertRaises(CommandError):
                call_command('send_morning_task_report', '--i-am-sending-to-real-people',
                             stdout=StringIO(), stderr=StringIO())
        self.assertEqual(self.post.call_count, 0)
        self.assertFalse(NotificationLog.objects.exists())

    def test_unknown_only_user_is_an_error(self):
        with self.assertRaises(CommandError):
            call_command('send_morning_task_report', '--dry-run', '--only-user', 'nobody',
                         stdout=StringIO(), stderr=StringIO())
