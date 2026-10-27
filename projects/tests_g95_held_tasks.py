"""
G95 — a PM or Project Coordinator's tasks on sites they do not manage
(docs/EXECUTION_MODULE_DEFERRED.md G95, closed by this build).

WHAT IS PINNED HERE
-------------------
    one rule  qaqc_engineer_projects_q() agrees with user_is_site_qaqc() case
              for case: active, ended, inactive profile, role outside
              QAQC_ENGINEER_ROLES, non-OPEX, two historical rows        QaqcEngineerProjectsQTests
    card      a coordinator QA/QC engineer sees their held task on
              "Your tasks on other sites", and its link opens; empty
              card hidden; a task on a site they cannot open is only
              counted                                                   HeldElsewhereCardTests
    counts    the three stat blocks and the drill-down include it,
              marked by site; a task held on a managed site counts
              once and is never on the card                             HeldElsewhereCountTests
    cost      the PM dashboard is the pre-G95 figure plus one query,
              however many tasks are held                               DashboardQueryCountTests

Built on tests_qaqc_assignment.QaqcFixture: two activated OPEX sites, site_a (pm_a +
coord_a) and site_b (pm_b). Added here: coord_b, site_b's coordinator, who is named site_a's
QA/QC engineer and given site_a's paperwork task (a Project Coordinator task, CL-A v2.5).

Run with:
    python manage.py test projects.tests_g95_held_tasks --settings=solarpms.test_settings
"""
from datetime import timedelta

from django.contrib.auth.models import User
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from .models import Project, SiteQaqcAssignment, Task, UserProfile
from .permissions import qaqc_engineer_projects_q, user_is_site_qaqc
from .tests_qaqc_assignment import QaqcFixture, _client_for, _profile

PAPERWORK = 'Completion Certificates (Paperwork)'
HIDDEN_LINE = 'assigned to you on sites you cannot open'

# The PM dashboard for coord_b on this fixture before G95 (measured on fd6b354 with the
# same setUp), and G95's one added query: the card's list. Neither the stat blocks nor the
# drill-down's OR adds a query; the subquery runs inside the existing COUNTs.
DASHBOARD_QUERIES_BEFORE_G95 = 44
DASHBOARD_QUERIES = DASHBOARD_QUERIES_BEFORE_G95 + 1


class HeldTaskFixture(QaqcFixture):
    """QaqcFixture plus coord_b (site_b's coordinator), named site_a's QA/QC engineer
    through the real screen. No task is given here; each test class decides."""

    def setUp(self):
        super().setUp()
        self.today = timezone.localdate()
        self.coord_b = _profile('g95_coord_b', 'Project Coordinator')
        self.site_b.coordinators.add(self.coord_b)
        self._assign(self.site_a, self.coord_b)
        self.assertTrue(user_is_site_qaqc(self.coord_b.user, self.site_a),
                        'fixture: coord_b is not site_a\'s engineer')

    def _give(self, site, task, profile, due):
        """Assign through the real task_assign view as the site's PM, then set the due
        date directly (fixture state; the date gates are not under test here)."""
        response = _client_for(site.assigned_pm).post(
            reverse('task_assign', args=[site.project_id, task.pk]),
            {'assigned_to': profile.pk})
        self.assertEqual(response.status_code, 302)
        Task.objects.filter(pk=task.pk).update(due_date=due)
        task.refresh_from_db()
        self.assertEqual(task.assigned_to, profile, 'fixture: the task was not assigned')
        return task

    def _paperwork(self, site):
        return Task.objects.get(phase__project=site, task_name=PAPERWORK)

    def _dashboard(self, profile):
        response = _client_for(profile).get(reverse('dashboard_pm'))
        self.assertEqual(response.status_code, 200)
        return response

    def _drill(self, profile, name):
        response = _client_for(profile).get(reverse(name))
        self.assertEqual(response.status_code, 200)
        return response


# ---------------------------------------------------------------------------
# One rule: the queryset form agrees with user_is_site_qaqc()
# ---------------------------------------------------------------------------

class QaqcEngineerProjectsQTests(QaqcFixture):
    """Each case asks all three readings: user_is_site_qaqc(), the Q on Project, and the
    Q on Task through 'phase__project__'."""

    def _agree(self, profile_pk, project, expected):
        user = User.objects.get(profile__pk=profile_pk)   # fresh: no cached profile
        profile = user.profile
        project.refresh_from_db()
        helper = user_is_site_qaqc(user, project)
        on_project = Project.objects.filter(
            qaqc_engineer_projects_q(profile), pk=project.pk).exists()
        on_task = Task.objects.filter(
            qaqc_engineer_projects_q(profile, 'phase__project__'),
            phase__project=project).exists()
        self.assertEqual((helper, on_project, on_task), (expected, expected, expected))

    def test_active_assignment(self):
        self._assign(self.site_a, self.qaqc)
        self._agree(self.qaqc.pk, self.site_a, True)
        self._agree(self.qaqc.pk, self.site_b, False)

    def test_ended_assignment(self):
        self._assign(self.site_a, self.qaqc)
        self._end(self.site_a)
        self.assertFalse(SiteQaqcAssignment.objects.filter(
            project=self.site_a, ended_at__isnull=True).exists())
        self._agree(self.qaqc.pk, self.site_a, False)

    def test_inactive_profile(self):
        self._assign(self.site_a, self.qaqc)
        UserProfile.objects.filter(pk=self.qaqc.pk).update(is_active=False)
        self._agree(self.qaqc.pk, self.site_a, False)

    def test_role_outside_qaqc_engineer_roles(self):
        self._assign(self.site_a, self.qaqc)
        UserProfile.objects.filter(pk=self.qaqc.pk).update(role='Finance')
        self._agree(self.qaqc.pk, self.site_a, False)

    def test_non_opex_project(self):
        # The writer refuses a non-OPEX site, so the row is made on an OPEX site and the
        # type changed underneath it: the shape both readings must refuse.
        self._assign(self.site_a, self.qaqc)
        Project.objects.filter(pk=self.site_a.pk).update(project_type='CAPEX')
        self._agree(self.qaqc.pk, self.site_a, False)

    def test_two_historical_rows_for_the_same_site(self):
        self._assign(self.site_a, self.qaqc)
        self._end(self.site_a)
        self._assign(self.site_a, self.qaqc)
        self.assertEqual(SiteQaqcAssignment.objects.filter(
            project=self.site_a, engineer=self.qaqc).count(), 2)
        self._agree(self.qaqc.pk, self.site_a, True)
        # Beside a managed-project leg, no task is counted twice: the assignment is a
        # subquery, not a join over both rows.
        site_tasks = Task.objects.filter(phase__project=self.site_a).count()
        either = Task.objects.filter(
            Q(phase__project_id__in=[self.site_a.pk])
            | qaqc_engineer_projects_q(self.qaqc, 'phase__project__'))
        self.assertEqual(either.count(), site_tasks)


# ---------------------------------------------------------------------------
# The card
# ---------------------------------------------------------------------------

class HeldElsewhereCardTests(HeldTaskFixture):

    def test_coordinator_engineer_sees_their_held_paperwork_task(self):
        paper = self._give(self.site_a, self._paperwork(self.site_a), self.coord_b, self.today)
        response = self._dashboard(self.coord_b)
        self.assertEqual(response.context['held_elsewhere_tasks'], [paper])
        self.assertEqual(response.context['held_elsewhere_hidden'], 0)
        body = response.content.decode()
        self.assertIn('id="held-elsewhere"', body)
        self.assertIn('Your tasks on other sites', body)
        self.assertIn('Due Today', body)
        self.assertNotIn(HIDDEN_LINE, body)
        link = reverse('task_detail', args=[self.site_a.project_id, paper.pk])
        self.assertIn(f'href="{link}"', body)
        # The link opens.
        self.assertEqual(_client_for(self.coord_b).get(link).status_code, 200)

    def test_overdue_task_is_flagged(self):
        self._give(self.site_a, self._paperwork(self.site_a), self.coord_b,
                   self.today - timedelta(days=3))
        body = self._dashboard(self.coord_b).content.decode()
        self.assertIn('Delayed · 3d', body)

    def test_empty_card_is_hidden(self):
        body = self._dashboard(self.coord_b).content.decode()
        self.assertNotIn('id="held-elsewhere"', body)
        self.assertNotIn('Your tasks on other sites', body)

    def test_task_on_a_site_they_cannot_open_is_counted_not_listed(self):
        # G86: any PM may be given any site's PM task. pm_b neither manages site_a nor is
        # its engineer, so cannot open it.
        task = self._give(self.site_a, self._pm_task(self.site_a), self.pm_b, self.today)
        response = self._dashboard(self.pm_b)
        self.assertEqual(response.context['held_elsewhere_tasks'], [])
        self.assertEqual(response.context['held_elsewhere_hidden'], 1)
        body = response.content.decode()
        self.assertIn('id="held-elsewhere"', body)
        self.assertIn(f'1 more task {HIDDEN_LINE}', body)
        self.assertNotIn(reverse('task_detail', args=[self.site_a.project_id, task.pk]), body)
        # Not in the counts or the drill-down either.
        self.assertEqual(response.context['summary']['tasks_due_today'],
                         self._managed_due_today(self.pm_b))
        drill = self._drill(self.pm_b, 'tasks_due_today')
        self.assertNotIn(task, [t for g in drill.context['groups'] for t in g['tasks']])

    def test_listed_and_hidden_together(self):
        paper = self._give(self.site_a, self._paperwork(self.site_a), self.coord_b, self.today)
        # coord_b given a Coordinator task on a third site they cannot open (ORM: the
        # screens offer only the site's own coordinators, so this is the admin path, G88).
        site_c = self._activated_site('QA Site Charlie', self.pm_b)
        other = self._paperwork(site_c)
        Task.objects.filter(pk=other.pk).update(assigned_to=self.coord_b)
        response = self._dashboard(self.coord_b)
        self.assertEqual(response.context['held_elsewhere_tasks'], [paper])
        self.assertEqual(response.context['held_elsewhere_hidden'], 1)

    def _managed_due_today(self, profile):
        managed = Project.objects.filter(Q(assigned_pm=profile) | Q(coordinators=profile))
        return Task.objects.filter(
            phase__project__in=managed, due_date=self.today, is_mirror=False,
            is_not_applicable=False,
            status__in=[Task.NOT_STARTED, Task.IN_PROGRESS, Task.BLOCKED]).count()


# ---------------------------------------------------------------------------
# The counts and the drill-down
# ---------------------------------------------------------------------------

class HeldElsewhereCountTests(HeldTaskFixture):

    def _summary(self, profile):
        return self._dashboard(profile).context['summary']

    def _counts_with_and_without(self, due):
        """The three stat blocks for coord_b with the paperwork task held, and without."""
        paper = self._give(self.site_a, self._paperwork(self.site_a), self.coord_b, due)
        held = self._summary(self.coord_b)
        Task.objects.filter(pk=paper.pk).update(assigned_to=None)
        unheld = self._summary(self.coord_b)
        Task.objects.filter(pk=paper.pk).update(assigned_to=self.coord_b)
        return paper, {k: held[k] - unheld[k]
                       for k in ('tasks_due_today', 'tasks_due_soon', 'tasks_overdue')}

    def _drilled(self, profile, name):
        groups = self._drill(profile, name).context['groups']
        return {t.pk: g['held_elsewhere'] for g in groups for t in g['tasks']}

    def test_due_today_counted_and_drilled_marked_by_site(self):
        paper, diff = self._counts_with_and_without(self.today)
        self.assertEqual(diff, {'tasks_due_today': 1, 'tasks_due_soon': 0, 'tasks_overdue': 0})
        self.assertIs(self._drilled(self.coord_b, 'tasks_due_today')[paper.pk], True)
        body = self._drill(self.coord_b, 'tasks_due_today').content.decode()
        self.assertIn('QA/QC site · your tasks only', body)

    def test_overdue_counted_and_drilled(self):
        paper, diff = self._counts_with_and_without(self.today - timedelta(days=2))
        self.assertEqual(diff, {'tasks_due_today': 0, 'tasks_due_soon': 0, 'tasks_overdue': 1})
        self.assertIn(paper.pk, self._drilled(self.coord_b, 'tasks_overdue'))

    def test_due_soon_counted_and_drilled(self):
        paper, diff = self._counts_with_and_without(self.today + timedelta(days=3))
        self.assertEqual(diff, {'tasks_due_today': 0, 'tasks_due_soon': 1, 'tasks_overdue': 0})
        self.assertIn(paper.pk, self._drilled(self.coord_b, 'tasks_due_soon'))

    def test_managed_sites_are_not_marked(self):
        Task.objects.filter(phase__project=self.site_b, is_mirror=False).update(
            due_date=self.today)
        drilled = self._drilled(self.coord_b, 'tasks_due_today')
        self.assertTrue(drilled)
        self.assertFalse(any(drilled.values()))

    def test_pm_managing_the_site_sees_no_duplicate(self):
        # pm_a manages site_a and holds its PM task: counted once, in the managed part,
        # never on the card.
        task = self._give(self.site_a, self._pm_task(self.site_a), self.pm_a, self.today)
        response = self._dashboard(self.pm_a)
        self.assertEqual(response.context['held_elsewhere_tasks'], [])
        self.assertEqual(response.context['held_elsewhere_hidden'], 0)
        self.assertNotIn('id="held-elsewhere"', response.content.decode())
        groups = self._drill(self.pm_a, 'tasks_due_today').context['groups']
        listed = [t.pk for g in groups for t in g['tasks']]
        self.assertEqual(listed.count(task.pk), 1)
        self.assertEqual(response.context['summary']['tasks_due_today'], len(listed))

    def test_engineer_who_also_manages_the_site_counts_once_in_the_managed_part(self):
        # The screens refuse a manager as engineer (CL-A, CL-H); the ORM shape is the one
        # left after a direct edit, and the OR must still count the task once.
        paper = self._give(self.site_a, self._paperwork(self.site_a), self.coord_b, self.today)
        self.site_a.coordinators.add(self.coord_b)
        response = self._dashboard(self.coord_b)
        self.assertEqual(response.context['held_elsewhere_tasks'], [])
        self.assertEqual(response.context['held_elsewhere_hidden'], 0)
        groups = self._drill(self.coord_b, 'tasks_due_today').context['groups']
        rows = [(t.pk, g['held_elsewhere']) for g in groups for t in g['tasks']]
        self.assertEqual([r for r in rows if r[0] == paper.pk], [(paper.pk, False)])
        self.assertEqual(response.context['summary']['tasks_due_today'], len(rows))


# ---------------------------------------------------------------------------
# Query cost
# ---------------------------------------------------------------------------

class DashboardQueryCountTests(HeldTaskFixture):

    def _count(self, profile):
        client = _client_for(profile)
        with self.assertNumQueries(DASHBOARD_QUERIES):
            response = client.get(reverse('dashboard_pm'))
        self.assertEqual(response.status_code, 200)
        return response

    def test_empty_card(self):
        self._count(self.coord_b)

    def test_one_held_task(self):
        self._give(self.site_a, self._paperwork(self.site_a), self.coord_b, self.today)
        self.assertEqual(len(self._count(self.coord_b).context['held_elsewhere_tasks']), 1)

    def test_more_held_tasks_cost_nothing_more(self):
        self._give(self.site_a, self._paperwork(self.site_a), self.coord_b, self.today)
        site_c = self._activated_site('QA Site Charlie', self.pm_b)
        Task.objects.filter(pk=self._paperwork(site_c).pk).update(assigned_to=self.coord_b)
        response = self._count(self.coord_b)
        self.assertEqual(len(response.context['held_elsewhere_tasks']), 1)
        self.assertEqual(response.context['held_elsewhere_hidden'], 1)
