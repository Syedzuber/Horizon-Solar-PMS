"""
QA/QC engineer eligibility widened to PMs and Project Coordinators — closeout spec v2.4
(docs/CLOSEOUT_SPEC.md: CL-9 and CL-A amended; CL-H, CL-I, CL-J, CL-K).

WHAT IS PINNED HERE
-------------------
    CL-9/K  the full cycle as a PM engineer, and as a coordinator, on a
            site they do not manage: assigned, dashboard section, visit,
            "No", punch point, rectified, re-verified, task approved      FullCycleTests
    CL-A    refused: the site's PM, a Site Engineer task holder, an
            inactive user, a Finance user; a holder of another role's task
            is eligible (v2.5), and so is a site coordinator (CL-L, v2.6);
            the eligible list agrees                                       EligibilityTests
    CL-H    change_site_pm and hand_over_sites refuse the site's current
            QA/QC engineer; assign_coordinators adds them (CL-L, v2.6)     ManagerSeatTests
    CL-I    a PM engineer is not shown the PM-only overview blocks         OverviewContentTests
    CL-D    a PM engineer counts as the site's independent approver        SelfCertificationTests
    CL-J    a move between the three roles keeps the row live              StaleRuleTests
    one rule: QAQC_ENGINEER_ROLES is the only role term for the engineer   OneRoleRuleTests

The CL-G tripwire (no work gate opens for any engineer role) lives with the other
refusals: tests_access_isolation.ViewOnlyGrantNeverOpensWorkTripwireTests.

Built on tests_punch_rectify.RectifyFixture (two activated OPEX sites, visits through the
real screens, storage mocked). `pm_b` is other_site's PM and `coord_b` (added here) its
coordinator: each manages a site, just not `site`, where they are named QA/QC engineer.

Run with:
    python manage.py test projects.tests_qaqc_engineer_roles --settings=solarpms.test_settings
"""
import inspect

from django.contrib.auth.models import User
from django.urls import reverse

from . import permissions, punch_points, qaqc_views
from .models import PunchPoint, SiteQaqcAssignment, Task, UserProfile
from .permissions import (
    task_has_independent_approver, user_can_approve_task, user_can_view_project,
    user_is_site_qaqc, user_may_self_certify, user_works_on_site,
)
from .qaqc_views import assign_site_qaqc, eligible_qaqc_engineers, end_site_qaqc
from .site_pm import SitePmChangeRefused, change_site_pm, hand_over_sites
from .tests_punch_rectify import RectifyFixture
from .tests_qaqc_assignment import _client_for, _messages, _profile
from .utils import assign_task_to


class RolesFixture(RectifyFixture):
    """RectifyFixture plus `coord_b`, other_site's coordinator, and `admin`."""

    def setUp(self):
        super().setUp()
        self.coord_b = _profile('qr_coord_b', 'Project Coordinator')
        self.other_site.coordinators.add(self.coord_b)
        self.admin = _profile('qr_admin', 'Admin')

    def _assign_screen(self, engineer, by=None):
        """Assign (replacing `qaqc`) through the real screen, running the notices."""
        with self.captureOnCommitCallbacks(execute=True):
            return _client_for(by or self.pm).post(
                reverse('site_qaqc', args=[self.site.project_id]),
                {'action': 'assign', 'engineer_id': engineer.pk})

    def _engineer_id(self):
        return SiteQaqcAssignment.objects.get(project=self.site, ended_at__isnull=True).engineer_id


# ---------------------------------------------------------------------------
# CL-9 / CL-K — the whole loop as a PM, and as a Project Coordinator
# ---------------------------------------------------------------------------

class FullCycleTests(RolesFixture):

    def _cycle(self, engineer):
        # Assigned, on a site they do not manage.
        self._assign_screen(engineer)
        self.assertEqual(self._engineer_id(), engineer.pk)
        self.assertTrue(user_can_view_project(engineer.user, self.site))
        self.assertFalse(user_works_on_site(engineer.user, self.site))

        # Their own dashboard shows the site (CL-K), through the shared partial.
        dashboard = _client_for(engineer).get(reverse('dashboard_pm'))
        self.assertEqual(dashboard.status_code, 200)
        self.assertEqual([s['project'] for s in dashboard.context['qaqc_sites']], [self.site])
        self.assertContains(dashboard, 'id="qaqc-sites"')
        self.assertContains(dashboard, reverse('qaqc_visit_start', args=[self.site.project_id]))
        self.assertNotContains(dashboard, 'CL-K')   # the partial's comment did not leak

        # Start, answer "No", submit: a punch point is raised on the task.
        self._start(by=engineer)
        visit = self._open_visit()
        self.assertEqual(visit.engineer, engineer)
        response = self._answer(visit, self.civil, self.civil_items[0], answer='no',
                                remark='Anchor bolts loose.', by=engineer)
        self.assertEqual(response.status_code, 200)
        self._submit(visit, by=engineer)
        point = PunchPoint.objects.get(qaqc_answer__visit=visit)
        self.assertEqual(point.task, self.civil)
        self.assertEqual(point.raised_by, engineer)

        # The assignee rectifies; it waits in the engineer's own dashboard list.
        self._rectify(point)
        self.assertEqual(point.status, PunchPoint.RECTIFIED)
        dashboard = _client_for(engineer).get(reverse('dashboard_pm'))
        self.assertEqual(list(dashboard.context['awaiting_my_reverification']), [point])
        self.assertContains(dashboard, 'id="awaiting-my-reverification"')

        # The engineer re-verifies; the point closes.
        self._reverify(point, by=engineer)
        self.assertEqual(point.status, PunchPoint.CLOSED)
        self.assertEqual(point.closed_by, engineer)

        # The worker submits the task and the engineer approves it.
        task = self._submitted()
        self.assertTrue(task.is_awaiting_approval, 'fixture: the task did not queue')
        self.assertTrue(user_can_approve_task(engineer.user, self.site))
        self._approve(by=engineer)
        self.assertEqual(task.status, Task.DONE)
        self.assertEqual(task.approved_by, engineer)

    def test_as_a_pm_of_another_site(self):
        self._cycle(self.pm_b)

    def test_as_a_coordinator_of_another_site(self):
        self._cycle(self.coord_b)

    def test_the_site_engineer_dashboard_still_draws_the_sections(self):
        page = _client_for(self.qaqc).get(reverse('dashboard_site_engineer'))
        self.assertEqual([s['project'] for s in page.context['qaqc_sites']], [self.site])
        self.assertContains(page, 'id="qaqc-sites"')

    def test_a_pm_with_no_assignment_sees_no_section(self):
        page = _client_for(self.pm).get(reverse('dashboard_pm'))
        self.assertEqual(page.context['qaqc_sites'], [])
        self.assertNotContains(page, 'id="qaqc-sites"')
        self.assertNotContains(page, 'id="awaiting-my-reverification"')


# ---------------------------------------------------------------------------
# CL-A (amended) — who may not be named
# ---------------------------------------------------------------------------

class EligibilityTests(RolesFixture):

    def _refused(self, engineer, expected):
        response = self._assign_screen(engineer)
        self.assertIn(expected, ' '.join(_messages(response)))
        self.assertEqual(self._engineer_id(), self.qaqc.pk, 'the assignment changed')

    def test_the_sites_pm_is_refused(self):
        self._refused(self.pm, 'manages this site, so cannot be its independent QA/QC engineer')

    def test_a_site_coordinator_is_accepted(self):
        """CL-L (v2.6): a coordinator of the site may be named its QA/QC engineer."""
        self._assign_screen(self.coord)
        self.assertEqual(self._engineer_id(), self.coord.pk)
        self.assertTrue(self.site.coordinators.filter(pk=self.coord.pk).exists())

    def test_a_task_holder_is_refused(self):
        self._refused(self.worker, 'holds')

    def test_a_pm_holding_a_site_engineer_task_is_refused_as_a_holder(self):
        assign_task_to(self.module, self.pm_b)
        self._refused(self.pm_b, 'holds 1 Site Engineer task')

    def test_a_pm_or_coordinator_holding_their_own_role_task_is_eligible(self):
        """CL-A v2.5: a non-Site-Engineer task does not disqualify. pm_b holds the site's
        Net Metering Approval and coord_b its Completion Certificates; both stay on the
        eligible list and each is accepted by the writer."""
        assign_task_to(Task.objects.get(phase__project=self.site,
                                        task_name='Net Metering Approval'), self.pm_b)
        assign_task_to(Task.objects.get(phase__project=self.site,
                                        task_name='Completion Certificates (Paperwork)'),
                       self.coord_b)
        eligible = set(eligible_qaqc_engineers(self.site))
        self.assertIn(self.pm_b, eligible)
        self.assertIn(self.coord_b, eligible)
        for engineer in (self.pm_b, self.coord_b):
            with self.subTest(engineer=engineer.user.username):
                self._assign_screen(engineer)
                self.assertEqual(self._engineer_id(), engineer.pk)

    def test_an_inactive_user_is_refused(self):
        User.objects.filter(pk=self.pm_b.user.pk).update(is_active=False)
        self._refused(UserProfile.objects.select_related('user').get(pk=self.pm_b.pk),
                      'is not an active site engineer, PM or project coordinator')

    def test_a_finance_user_is_refused(self):
        finance = _profile('qr_finance', 'Finance')
        self._refused(finance, 'is not an active site engineer, PM or project coordinator')

    def test_the_eligible_list_agrees(self):
        finance = _profile('qr_finance_list', 'Finance')
        eligible = set(eligible_qaqc_engineers(self.site))
        # CL-L (v2.6): the site's coordinator is eligible; its PM is not.
        for profile in (self.pm_b, self.coord_b, self.spare, self.coord):
            with self.subTest(eligible=profile.user.username):
                self.assertIn(profile, eligible)
        for profile in (self.pm, self.worker, self.qaqc, finance):
            with self.subTest(refused=profile.user.username):
                self.assertNotIn(profile, eligible)
        screen = _client_for(self.pm).get(reverse('site_qaqc', args=[self.site.project_id]))
        self.assertEqual(set(screen.context['eligible']), eligible)
        self.assertContains(screen, 'Choose a site engineer, PM or project coordinator')


# ---------------------------------------------------------------------------
# CL-H — the engineer cannot be seated as the site's manager
# ---------------------------------------------------------------------------

class ManagerSeatTests(RolesFixture):

    def setUp(self):
        super().setUp()
        self._assign_screen(self.pm_b)          # pm_b checks `site`
        self.coord_c = _profile('qr_coord_c', 'Project Coordinator')

    def test_change_site_pm_refuses_the_engineer(self):
        with self.assertRaisesMessage(SitePmChangeRefused, 'End the QA/QC assignment first'):
            change_site_pm(self.site, self.pm_b, self.admin, expected_outgoing_pm=self.pm,
                           reason='Rebalancing.')
        self.site.refresh_from_db()
        self.assertEqual(self.site.assigned_pm, self.pm)

    def test_hand_over_sites_refuses_the_engineer_and_moves_nothing(self):
        with self.assertRaisesMessage(SitePmChangeRefused, 'End the QA/QC assignment first'):
            hand_over_sites([self.site], self.pm, self.pm_b, self.admin, 'Rebalancing.')
        self.site.refresh_from_db()
        self.assertEqual(self.site.assigned_pm, self.pm)
        self.assertFalse(Task.objects.filter(phase__project=self.site,
                                             assigned_to=self.pm_b).exists())

    def test_change_site_pm_still_seats_anyone_else(self):
        other_pm = _profile('qr_pm_c', 'PM')
        change_site_pm(self.site, other_pm, self.admin, expected_outgoing_pm=self.pm,
                       reason='Rebalancing.')
        self.site.refresh_from_db()
        self.assertEqual(self.site.assigned_pm, other_pm)

    def test_assign_coordinators_adds_the_engineer(self):
        """CL-L (v2.6): the coordinator half of CL-H is lifted; the engineer is added and
        stays the site's engineer."""
        end_site_qaqc(self.site, self.pm)
        assign_site_qaqc(self.site, self.coord_c, self.pm)
        response = _client_for(self.pm).post(
            reverse('assign_coordinators', args=[self.site.project_id]),
            {'coordinator_ids': [self.coord.pk, self.coord_c.pk, self.coord_b.pk]})
        self.assertNotIn('End the QA/QC assignment first', ' '.join(_messages(response)))
        self.assertEqual(set(self.site.coordinators.all()),
                         {self.coord, self.coord_c, self.coord_b})
        self.assertEqual(self._engineer_id(), self.coord_c.pk)

    def test_assign_coordinators_still_adds_anyone_else(self):
        _client_for(self.pm).post(
            reverse('assign_coordinators', args=[self.site.project_id]),
            {'coordinator_ids': [self.coord.pk, self.coord_c.pk]})
        self.assertEqual(set(self.site.coordinators.all()), {self.coord, self.coord_c})

    def test_after_ending_the_assignment_the_seat_is_open(self):
        end_site_qaqc(self.site, self.pm)
        change_site_pm(self.site, self.pm_b, self.admin, expected_outgoing_pm=self.pm,
                       reason='Rebalancing.')
        self.site.refresh_from_db()
        self.assertEqual(self.site.assigned_pm, self.pm_b)


# ---------------------------------------------------------------------------
# CL-I — the assignment grants a PM no PM-only content
# ---------------------------------------------------------------------------

class OverviewContentTests(RolesFixture):

    def _overview(self, profile):
        return _client_for(profile).get(reverse('project_overview', args=[self.site.project_id]))

    def test_a_pm_engineer_is_not_shown_the_vendor_orders_or_the_boq_card(self):
        self._assign_screen(self.pm_b)
        page = self._overview(self.pm_b)
        self.assertEqual(page.status_code, 200)
        self.assertFalse(page.context['show_vendor_orders'])
        self.assertFalse(page.context['manages_as_pm'])
        self.assertNotContains(page, 'id="vendor-orders-card"')
        self.assertNotContains(page, 'id="boqCollapse"')

    def test_a_coordinator_engineer_is_not_shown_the_boq_card(self):
        self._assign_screen(self.coord_b)
        page = self._overview(self.coord_b)
        self.assertFalse(page.context['manages_as_pm'])
        self.assertNotContains(page, 'id="boqCollapse"')
        self.assertNotContains(page, 'id="vendor-orders-card"')

    def test_the_sites_own_pm_and_coordinator_still_are(self):
        self._assign_screen(self.pm_b)
        pm_page = self._overview(self.pm)
        self.assertTrue(pm_page.context['show_vendor_orders'])
        self.assertContains(pm_page, 'id="vendor-orders-card"')
        self.assertContains(pm_page, 'id="boqCollapse"')
        self.assertNotContains(pm_page, 'CL-I')   # the template comments did not leak
        coord_page = self._overview(self.coord)
        self.assertTrue(coord_page.context['manages_as_pm'])
        self.assertContains(coord_page, 'id="boqCollapse"')

    def test_a_pm_engineer_cannot_open_the_boq_entry(self):
        """Step 0 of the build: opex_boq_entry asks user_can_view_project_boq, which
        never reads the QA/QC grant, so the engineer gets 403 whatever their role."""
        for engineer in (self.pm_b, self.coord_b):
            with self.subTest(engineer=engineer.role):
                self._assign_screen(engineer)
                response = _client_for(engineer).get(
                    reverse('opex_boq_entry', args=[self.site.project_id]))
                self.assertEqual(response.status_code, 403)


# ---------------------------------------------------------------------------
# CL-D — a PM engineer is an independent approver
# ---------------------------------------------------------------------------

class SelfCertificationTests(RolesFixture):

    def setUp(self):
        super().setUp()
        # Pool without the engineer: the PM alone (the coordinator is removed).
        self.site.coordinators.remove(self.coord)
        end_site_qaqc(self.site, self.pm)

    def test_without_an_engineer_the_pm_self_certifies(self):
        self.assertFalse(task_has_independent_approver(self.site, self.pm))
        self.assertTrue(user_may_self_certify(self.pm.user, self.site))

    def test_a_pm_engineer_counts_as_independent(self):
        assign_site_qaqc(self.site, self.pm_b, self.pm)
        self.assertTrue(task_has_independent_approver(self.site, self.pm))
        self.assertFalse(user_may_self_certify(self.pm.user, self.site))

    def test_a_coordinator_engineer_counts_as_independent(self):
        assign_site_qaqc(self.site, self.coord_b, self.pm)
        self.assertTrue(task_has_independent_approver(self.site, self.pm))

    def test_a_pm_submission_waits_for_the_pm_engineer(self):
        assign_site_qaqc(self.site, self.pm_b, self.pm)
        task = Task.objects.get(phase__project=self.site, task_name='Net Metering Approval')
        task = self._submitted(task, by=self.pm)
        self.assertTrue(task.is_awaiting_approval, 'the PM self-certified past the engineer')


# ---------------------------------------------------------------------------
# CL-J — what "stale" means once the role is not fixed
# ---------------------------------------------------------------------------

class StaleRuleTests(RolesFixture):

    def _overview_stale(self):
        page = _client_for(self.pm).get(reverse('project_overview', args=[self.site.project_id]))
        return page.context['qaqc_stale']

    def test_a_move_between_the_three_roles_keeps_the_row_live(self):
        self._assign_screen(self.pm_b)
        for role in ('Project Coordinator', 'Site Engineer', 'PM'):
            with self.subTest(role=role):
                UserProfile.objects.filter(pk=self.pm_b.pk).update(role=role)
                user = User.objects.get(pk=self.pm_b.user.pk)
                self.assertTrue(user_is_site_qaqc(user, self.site))
                self.assertFalse(self._overview_stale())

    def test_a_move_out_of_the_three_is_stale(self):
        self._assign_screen(self.pm_b)
        UserProfile.objects.filter(pk=self.pm_b.pk).update(role='Finance')
        self.assertFalse(user_is_site_qaqc(User.objects.get(pk=self.pm_b.user.pk), self.site))
        self.assertTrue(self._overview_stale())

    def test_an_inactive_login_is_stale(self):
        self._assign_screen(self.coord_b)
        User.objects.filter(pk=self.coord_b.user.pk).update(is_active=False)
        self.assertTrue(self._overview_stale())


# ---------------------------------------------------------------------------
# One rule — QAQC_ENGINEER_ROLES is the only role term for the engineer
# ---------------------------------------------------------------------------

class OneRoleRuleTests(RolesFixture):

    READERS = [
        qaqc_views.engineer_is_stale, qaqc_views.eligible_qaqc_engineers,
        permissions.user_is_site_qaqc, permissions._project_visibility,
        permissions.site_has_effective_qaqc, punch_points.effective_qaqc_engineer,
        punch_points.awaiting_reverification,
    ]

    def test_each_reader_uses_the_constant_and_spells_no_role(self):
        for func in self.READERS:
            source = inspect.getsource(func)
            with self.subTest(reader=func.__name__):
                if func is not permissions._project_visibility:
                    self.assertIn('QAQC_ENGINEER_ROLES', source)
                self.assertNotIn("engineer__role='", source)
                self.assertNotIn("role != 'Site Engineer'", source)
                self.assertNotIn("role='Site Engineer'", source)

    def test_the_view_grant_sits_outside_the_site_engineer_branch(self):
        """The grant is asked for every role (CL-I): a PM engineer sees the site."""
        self._assign_screen(self.pm_b)
        self.assertTrue(user_can_view_project(self.pm_b.user, self.site))
        self._assign_screen(self.coord_b)
        self.assertTrue(user_can_view_project(self.coord_b.user, self.site))
        self.assertFalse(user_can_view_project(self.pm_b.user, self.site),
                         'replaced: the grant ended with the assignment')
