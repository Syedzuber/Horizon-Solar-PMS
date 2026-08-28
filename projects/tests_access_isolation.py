"""
Access isolation lockdown — prompt 0.2. THE NEGATIVE HALF.

WHY THIS FILE EXISTS
--------------------
`tests_residential_baseline.py` is the positive net: it asserts what every role can
CORRECTLY still do, so the lockdown fails loudly if it over-blocks. This file is its
mirror. Every test below takes an actor the product ADMITTED BEFORE 0.2 and asserts they
are now refused.

Read the two together. On their own, either one is trivially satisfiable — a gate that
refuses everybody passes this file, and a gate that refuses nobody passes that one.

THE ONE RULE, INVERTED
----------------------
No test here asserts a capability. Each one names an audit finding, states the actor who
used to get through, and pins the refusal. If a test here starts failing, somebody has
reopened a hole — not broken a feature.

WHAT "REFUSED" MEANS, AND WHY IT DIFFERS BY ENDPOINT
----------------------------------------------------
Two shapes, and the difference is deliberate rather than sloppy:

  404  — the project-scope gate (`user_can_view_project`). A project you have no
         relationship to does not exist as far as you are concerned. Used by the eighteen
         A.5 detail endpoints, confirm_grn, payment_request_detail and the PM arm of
         set_milestone_amounts.
  403  — the role gate (`role_required`). The object may well exist; your ROLE is wrong.
         0.2 standardised this on HttpResponseForbidden so it matches the
         permissions.py-gated views, which already answered 403.

A caller cannot use the pair as an oracle: they only ever see the FIRST gate a view
applies, and no endpoint answers 403 for one project and 404 for another.

FIXTURE
-------
Two activated Residential projects with disjoint casts, mirroring
ResidentialBaselineBase's shape for the same reason: an isolation test against a
one-project fixture passes by accident. Project A's people are the actors; project B is
what they must not reach — and vice versa.

Activation goes through the real view, so if the lockdown breaks activation this file
errors in setUp exactly as the baseline does.

Run with:
    python manage.py test projects.tests_access_isolation --settings=solarpms.test_settings
"""
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.urls import reverse

from .models import (
    DCLineItem, DeliveryChallan, Issue, PaymentRequest, Project, Task, UserProfile,
    Vendor,
)
from .utils import RESIDENTIAL_FINANCE_ASSIGNEE_EMAIL, assign_tasks_to


def _profile(username, role, email='', **flags):
    """Create a User and set its auto-created UserProfile's role.

    A post_save signal on User creates the UserProfile, so we fetch and mutate rather than
    creating a second one — same helper shape as tests_residential_baseline.py.
    """
    user = User.objects.create_user(username=username, password='pw12345', email=email)
    profile = user.profile
    profile.role = role
    profile.is_active = True
    for field, value in flags.items():
        setattr(profile, field, value)
    profile.save()
    return profile


def _client_for(profile):
    client = Client()
    client.force_login(profile.user)
    return client


def _photo(name='site.jpg'):
    return SimpleUploadedFile(name, b'\xff\xd8\xff\xe0fake-jpeg-bytes',
                              content_type='image/jpeg')


class AccessIsolationBase(TestCase):
    """Two activated Residential projects with disjoint per-site casts.

    Project A: pm_a / coord_a / se_a / design_a
    Project B: pm_b / coord_b / se_b / design_b

    Both projects get a full cast, because several tests below need an actor who holds a
    REAL role relationship somewhere — so that what the gate refuses is provably the
    project relationship and not the role.
    """

    def setUp(self):
        # Activation refuses to run without this account (utils.attach_residential_template).
        self.finance = _profile('iso_fin', 'Finance',
                                email=RESIDENTIAL_FINANCE_ASSIGNEE_EMAIL)

        self.pm_a     = _profile('iso_pm_a',     'PM')
        self.coord_a  = _profile('iso_coord_a',  'Project Coordinator')
        self.se_a     = _profile('iso_se_a',     'Site Engineer')
        self.design_a = _profile('iso_design_a', 'Design')

        self.pm_b     = _profile('iso_pm_b',     'PM')
        self.coord_b  = _profile('iso_coord_b',  'Project Coordinator')
        self.se_b     = _profile('iso_se_b',     'Site Engineer')
        self.design_b = _profile('iso_design_b', 'Design')

        self.scm      = _profile('iso_scm',   'SCM')
        self.ceo      = _profile('iso_ceo',   'CEO')
        self.admin    = _profile('iso_admin', 'Admin')
        self.sysadmin = _profile('iso_sa',    'System Admin')

        self.project_a = self._make_and_activate('Iso Alpha', self.pm_a,
                                                 self.design_a, self.se_a)
        self.project_a.coordinators.add(self.coord_a)

        self.project_b = self._make_and_activate('Iso Bravo', self.pm_b,
                                                 self.design_b, self.se_b)
        self.project_b.coordinators.add(self.coord_b)

    def _make_and_activate(self, customer_name, pm, designer, site_engineer):
        project = Project.objects.create(
            customer_name=customer_name, customer_phone='9876543210',
            site_address='1 Sun Road', city='Lucknow', project_type='Residential',
            capacity_kw=Decimal('5.00'), contract_value=Decimal('300000.00'),
            status='Draft', assigned_pm=pm,
            target_commissioning_date=date.today() + timedelta(days=90),
        )
        response = _client_for(pm).post(
            reverse('project_activate', args=[project.project_id]),
            {'assigned_design_id': designer.pk},
        )
        self.assertEqual(response.status_code, 302, 'activation did not redirect')
        project.refresh_from_db()
        self.assertEqual(project.status, 'Active',
                         f'{customer_name} did not activate — is the Finance assignee present?')
        assign_tasks_to(
            Task.objects.filter(phase__project=project,
                                assigned_role=Task.SITE_ENGINEER),
            site_engineer,
        )
        return project

    # -- shared helpers ------------------------------------------------------

    def _a_task(self, project, assigned_role=Task.SITE_ENGINEER):
        return Task.objects.filter(phase__project=project,
                                   assigned_role=assigned_role).first()

    def _an_issue(self, project, raiser):
        """Raise a real issue on `project` through the real endpoint, as somebody who may."""
        _client_for(raiser).post(
            reverse('create_project_issue', args=[project.project_id]),
            {'title': f'Issue on {project.project_id}', 'severity': Issue.MEDIUM,
             'assigned_to': str(raiser.pk)},
        )
        return Issue.objects.get(title=f'Issue on {project.project_id}')

    def _a_challan(self, project):
        challan = DeliveryChallan.objects.create(
            project=project, dc_number=f'DC-{project.pk}', dc_date=date.today(),
            status=DeliveryChallan.EXPECTED, created_by=self.scm,
        )
        DCLineItem.objects.create(
            challan=challan, boq_category='Solar Modules',
            item_description='Solar Module 540Wp',
            ordered_quantity=Decimal('10.00'), unit='Nos',
        )
        return challan


# ---------------------------------------------------------------------------
# Findings 6 and 7 — the eighteen unscoped detail endpoints
# ---------------------------------------------------------------------------

class UnscopedDetailEndpointTests(AccessIsolationBase):
    """The A.5 endpoints carried `role == 'PM' and not user_can_manage_project(...)`,
    which is a scope check for PMs and a no-op for the other nine roles. Project
    Coordinator, Site Engineer and Design therefore reached every project in the
    portfolio. That is audit finding 6 (reads) and finding 7 (uploads)."""

    def test_an_unrelated_coordinator_cannot_open_project_overview(self):
        response = _client_for(self.coord_a).get(
            reverse('project_overview', args=[self.project_b.project_id]))
        self.assertEqual(response.status_code, 404)

    def test_an_unrelated_coordinator_cannot_open_task_detail(self):
        task = self._a_task(self.project_b)
        response = _client_for(self.coord_a).get(
            reverse('task_detail', args=[self.project_b.project_id, task.pk]))
        self.assertEqual(response.status_code, 404)

    def test_an_unrelated_coordinator_cannot_open_project_timeline(self):
        response = _client_for(self.coord_a).get(
            reverse('project_timeline', args=[self.project_b.project_id]))
        self.assertEqual(response.status_code, 404)

    def test_the_projects_own_coordinator_still_reaches_all_three(self):
        """The other half of the pair. Without this, a 404-everywhere regression would
        look like three passing isolation tests."""
        client = _client_for(self.coord_a)
        task = self._a_task(self.project_a)
        for url in (
            reverse('project_overview', args=[self.project_a.project_id]),
            reverse('task_detail', args=[self.project_a.project_id, task.pk]),
            reverse('project_timeline', args=[self.project_a.project_id]),
        ):
            with self.subTest(url=url):
                self.assertEqual(client.get(url).status_code, 200)

    def test_an_unrelated_design_user_cannot_upload_a_project_document(self):
        """Finding 7. A write, and one that puts a file on somebody else's project."""
        response = _client_for(self.design_a).post(
            reverse('upload_project_document', args=[self.project_b.project_id]),
            {'document_type': 'Site Photo', 'file': _photo()},
        )
        self.assertEqual(response.status_code, 404)

    def test_an_unrelated_site_engineer_cannot_upload_a_task_attachment(self):
        task = self._a_task(self.project_b)
        response = _client_for(self.se_a).post(
            reverse('upload_task_attachment',
                    args=[self.project_b.project_id, task.pk]),
            {'file': _photo()},
        )
        self.assertEqual(response.status_code, 404)

    def test_an_unrelated_site_engineer_cannot_change_a_task_status(self):
        """Finding 3. The role-matcher is portfolio-blind on its own: se_a holds the Site
        Engineer role, and every project has Site Engineer tasks, so before 0.2 the role
        comparison alone let them drive any of them."""
        task = self._a_task(self.project_b)
        before = task.status
        response = _client_for(self.se_a).post(
            reverse('task_status_update', args=[self.project_b.project_id, task.pk]),
            {'status': Task.IN_PROGRESS},
        )
        self.assertEqual(response.status_code, 404)
        task.refresh_from_db()
        self.assertEqual(task.status, before)

    def test_an_unrelated_site_engineer_cannot_set_a_task_due_date(self):
        task = self._a_task(self.project_b)
        response = _client_for(self.se_a).post(
            reverse('task_set_due_date', args=[self.project_b.project_id, task.pk]),
            {'due_date': date.today().isoformat()},
        )
        self.assertEqual(response.status_code, 404)
        task.refresh_from_db()
        self.assertIsNone(task.due_date)

    def test_an_unrelated_coordinator_cannot_comment_on_a_task(self):
        task = self._a_task(self.project_b)
        response = _client_for(self.coord_a).post(
            reverse('create_task_comment', args=[self.project_b.project_id, task.pk]),
            {'content': 'I should not be able to write this.'},
        )
        self.assertEqual(response.status_code, 404)


# ---------------------------------------------------------------------------
# Finding 2 — the eight Issue endpoints
# ---------------------------------------------------------------------------

class IssueEndpointIsolationTests(AccessIsolationBase):
    """The Issue endpoints resolve their project through `issue.project` rather than from
    the URL, so they needed the same fix in a different shape. Before 0.2 any
    authenticated non-PM reached every one of them on every issue in the portfolio."""

    def test_an_unrelated_site_engineer_cannot_open_issue_detail(self):
        issue = self._an_issue(self.project_b, self.pm_b)
        response = _client_for(self.se_a).get(
            reverse('issue_detail', args=[issue.pk]))
        self.assertEqual(response.status_code, 404)

    def test_an_unrelated_site_engineer_cannot_resolve_an_issue(self):
        """resolve_issue is the one endpoint where a wrong gate LEAVES THE SYSTEM — it
        sends WhatsApp and email to the project managers, the assignee and the raiser. The
        scope check sits immediately after `project = issue.project` and before the status
        transition, so a refused caller triggers no notification at all."""
        issue = self._an_issue(self.project_b, self.pm_b)
        _client_for(self.pm_b).post(reverse('update_issue_status', args=[issue.pk]))
        issue.refresh_from_db()
        self.assertEqual(issue.status, Issue.IN_PROGRESS)

        response = _client_for(self.se_a).post(
            reverse('resolve_issue', args=[issue.pk]),
            {'resolution_note': 'not mine to resolve'},
        )
        self.assertEqual(response.status_code, 404)
        issue.refresh_from_db()
        self.assertEqual(issue.status, Issue.IN_PROGRESS)
        self.assertIsNone(issue.resolved_at)

    def test_an_unrelated_design_user_cannot_advance_an_issue_status(self):
        issue = self._an_issue(self.project_b, self.pm_b)
        response = _client_for(self.design_a).post(
            reverse('update_issue_status', args=[issue.pk]))
        self.assertEqual(response.status_code, 404)
        issue.refresh_from_db()
        self.assertEqual(issue.status, Issue.OPEN)

    def test_an_unrelated_coordinator_cannot_assign_an_issue(self):
        issue = self._an_issue(self.project_b, self.pm_b)
        response = _client_for(self.coord_a).post(
            reverse('assign_issue', args=[issue.pk]),
            {'assigned_to': str(self.se_b.pk)},
        )
        self.assertEqual(response.status_code, 404)

    def test_an_unrelated_coordinator_cannot_comment_on_an_issue(self):
        issue = self._an_issue(self.project_b, self.pm_b)
        response = _client_for(self.coord_a).post(
            reverse('create_issue_comment', args=[issue.pk]),
            {'content': 'not my project'},
        )
        self.assertEqual(response.status_code, 404)

    def test_an_unrelated_site_engineer_cannot_raise_an_issue_on_a_project(self):
        response = _client_for(self.se_a).post(
            reverse('create_project_issue', args=[self.project_b.project_id]),
            {'title': 'Should never exist', 'severity': Issue.LOW,
             'assigned_to': str(self.se_b.pk)},
        )
        self.assertEqual(response.status_code, 404)
        self.assertFalse(Issue.objects.filter(title='Should never exist').exists())


class IssueAssigneeNarrowingTests(AccessIsolationBase):
    """`issue_detail` rendered EVERY active UserProfile in the company into the assignee
    dropdown, and `assign_issue` accepted any pk that came back. Both now resolve through
    _issue_assignable_profiles(project)."""

    def test_assign_issue_refuses_a_profile_with_no_relationship_to_the_project(self):
        """se_b is a real, active Site Engineer — they are refused for the project
        relationship they lack, not for being a bad pk."""
        issue = self._an_issue(self.project_a, self.pm_a)
        self.assertTrue(UserProfile.objects.filter(pk=self.se_b.pk, is_active=True).exists())

        response = _client_for(self.pm_a).post(
            reverse('assign_issue', args=[issue.pk]),
            {'assigned_to': str(self.se_b.pk)},
        )
        self.assertEqual(response.status_code, 302)   # redirect back with an error message
        issue.refresh_from_db()
        self.assertNotEqual(issue.assigned_to_id, self.se_b.pk)

    def test_assign_issue_still_accepts_a_profile_related_to_the_project(self):
        """The positive half — se_a holds tasks on project A, so they remain assignable."""
        issue = self._an_issue(self.project_a, self.pm_a)
        response = _client_for(self.pm_a).post(
            reverse('assign_issue', args=[issue.pk]),
            {'assigned_to': str(self.se_a.pk)},
        )
        self.assertEqual(response.status_code, 302)
        issue.refresh_from_db()
        self.assertEqual(issue.assigned_to_id, self.se_a.pk)

    def test_the_assignee_dropdown_no_longer_lists_the_whole_company(self):
        issue = self._an_issue(self.project_a, self.pm_a)
        response = _client_for(self.pm_a).get(reverse('issue_detail', args=[issue.pk]))
        self.assertEqual(response.status_code, 200)
        listed = {p.pk for p in response.context['all_profiles']}

        self.assertIn(self.pm_a.pk, listed)        # assigned PM
        self.assertIn(self.coord_a.pk, listed)     # coordinator
        self.assertIn(self.se_a.pk, listed)        # holds tasks on this project
        self.assertIn(self.design_a.pk, listed)    # assigned_design on this project

        for stranger in (self.pm_b, self.coord_b, self.se_b, self.design_b, self.ceo):
            with self.subTest(stranger=stranger.user.username):
                self.assertNotIn(stranger.pk, listed)


# ---------------------------------------------------------------------------
# Findings 1 and 5 — role gates that lacked a scope gate
# ---------------------------------------------------------------------------

class RoleGateScopingTests(AccessIsolationBase):

    def test_an_unrelated_site_engineer_cannot_confirm_a_grn(self):
        """AUDIT FINDING 1 — the reason a site engineer could not be given a login.
        `@role_required(['Site Engineer'])` had no project term, so any SE could sign off
        receipt of materials at any site in the portfolio."""
        challan = self._a_challan(self.project_b)
        line = challan.line_items.first()

        response = _client_for(self.se_a).post(
            reverse('confirm_grn', args=[self.project_b.project_id, challan.pk]),
            {f'received_qty_{line.pk}': '10', f'damaged_qty_{line.pk}': '0'},
        )
        self.assertEqual(response.status_code, 404)
        challan.refresh_from_db()
        self.assertEqual(challan.status, DeliveryChallan.EXPECTED)
        line.refresh_from_db()
        self.assertIsNone(line.received_quantity)

    def test_the_projects_own_site_engineer_can_still_confirm_a_grn(self):
        """The relationship the lockdown had to preserve — pinned in the baseline too, and
        repeated here so this file cannot pass by refusing every site engineer."""
        challan = self._a_challan(self.project_b)
        line = challan.line_items.first()

        response = _client_for(self.se_b).post(
            reverse('confirm_grn', args=[self.project_b.project_id, challan.pk]),
            {f'received_qty_{line.pk}': '10', f'damaged_qty_{line.pk}': '0'},
        )
        self.assertEqual(response.status_code, 302)
        line.refresh_from_db()
        self.assertEqual(line.received_quantity, Decimal('10.00'))

    def test_a_pm_cannot_set_milestone_amounts_on_another_pms_project(self):
        """AUDIT FINDING 5. `@role_required(['BD','PM'])` with no ownership term — a write
        straight onto the commercial terms of somebody else's contract."""
        response = _client_for(self.pm_a).post(
            reverse('set_milestone_amounts', args=[self.project_b.project_id]),
            data='{"m1_amount": "1", "m2_amount": "1", "m3_amount": "1"}',
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 404)

    def test_a_pm_can_still_set_milestone_amounts_on_their_own_project(self):
        response = _client_for(self.pm_a).post(
            reverse('set_milestone_amounts', args=[self.project_a.project_id]),
            data='{"m1_amount": "100000", "m2_amount": "100000", "m3_amount": "100000"}',
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['success'])

    def test_a_pm_cannot_read_another_pms_payment_request(self):
        """The vendor invoice number, the amount and the document URL were all readable by
        any PM in the company."""
        vendor = Vendor.objects.create(name='Acme Supplies')
        pr = PaymentRequest.objects.create(
            project=self.project_b, vendor=vendor, amount=Decimal('50000.00'),
            requested_by=self.pm_b.user,
        )
        response = _client_for(self.pm_a).get(
            reverse('payment_request_detail', args=[self.project_b.project_id, pr.pk]))
        self.assertEqual(response.status_code, 404)

    def test_the_owning_pm_can_still_read_their_own_payment_request(self):
        vendor = Vendor.objects.create(name='Acme Supplies')
        pr = PaymentRequest.objects.create(
            project=self.project_b, vendor=vendor, amount=Decimal('50000.00'),
            requested_by=self.pm_b.user,
        )
        response = _client_for(self.pm_b).get(
            reverse('payment_request_detail', args=[self.project_b.project_id, pr.pk]))
        self.assertEqual(response.status_code, 200)


# ---------------------------------------------------------------------------
# Findings 8 and 9 — the two missing dashboard gates
# ---------------------------------------------------------------------------

class DashboardGateTests(AccessIsolationBase):

    def test_a_site_engineer_is_refused_the_ceo_dashboard(self):
        """AUDIT FINDING 8. The docstring said "Access: CEO role only"; the view carried
        @login_required alone, so every authenticated user read the whole portfolio's
        financial position."""
        response = _client_for(self.se_a).get(reverse('dashboard_ceo'))
        self.assertEqual(response.status_code, 403)

    def test_the_ceo_and_the_admin_roles_still_reach_the_ceo_dashboard(self):
        """Admin and System Admin are unrestricted per execution-model §2 D-4."""
        for profile in (self.ceo, self.admin, self.sysadmin):
            with self.subTest(role=profile.role):
                response = _client_for(profile).get(reverse('dashboard_ceo'))
                self.assertEqual(response.status_code, 200)

    def test_the_task_drill_down_denies_a_role_with_no_branch(self):
        """AUDIT FINDING 9. Six roles reached every active project by falling off the end
        of the if/elif chain. Every role now has an explicit branch and the final `else`
        denies, so a blank role — and any role added to ROLE_CHOICES later — sees nothing
        until somebody chooses what it should see."""
        blank = _profile('iso_blank', '')
        for project in (self.project_a, self.project_b):
            Task.objects.filter(phase__project=project).update(due_date=date.today())

        response = _client_for(blank).get(reverse('tasks_due_today'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['total_count'], 0)
        self.assertEqual(response.context['groups'], [])

    def test_the_task_drill_down_is_still_scoped_for_a_coordinator(self):
        for project in (self.project_a, self.project_b):
            Task.objects.filter(phase__project=project).update(due_date=date.today())

        response = _client_for(self.coord_a).get(reverse('tasks_due_today'))
        self.assertEqual(response.status_code, 200)
        ids = {g['project'].project_id for g in response.context['groups']}
        self.assertEqual(ids, {self.project_a.project_id})

    def test_the_task_drill_down_is_still_portfolio_wide_for_scm(self):
        """Finding 9 is only PARTIALLY closed, and this pins which part. SCM, Finance, BD,
        CEO and the admin roles keep portfolio-wide task visibility by the 25 Aug decision
        — what changed is that they now have it by an explicit branch instead of by
        falling through. If SCM is ever narrowed, this test is the one to revisit."""
        for project in (self.project_a, self.project_b):
            Task.objects.filter(phase__project=project).update(due_date=date.today())

        response = _client_for(self.scm).get(reverse('tasks_due_today'))
        self.assertEqual(response.status_code, 200)
        ids = {g['project'].project_id for g in response.context['groups']}
        self.assertEqual(ids, {self.project_a.project_id, self.project_b.project_id})


# ---------------------------------------------------------------------------
# Finding 15 — the role_required fail-open
# ---------------------------------------------------------------------------

class ProfilelessUserTests(AccessIsolationBase):
    """`role_required` treated a user with no UserProfile as 'Admin', so a superuser
    created by `createsuperuser` passed all 33 @role_required(['Admin']) screens — while
    every permissions.py helper refused the same user."""

    def _profileless_superuser(self):
        user = User.objects.create_superuser(
            username='iso_root', email='root@example.com', password='pw12345')
        UserProfile.objects.filter(user=user).delete()
        user.refresh_from_db()
        return user

    def test_a_profileless_user_is_refused_an_admin_screen(self):
        user = self._profileless_superuser()
        client = Client()
        client.force_login(user)
        response = client.get(reverse('admin_master_switches'))
        self.assertEqual(response.status_code, 403)

    def test_a_profileless_user_is_refused_every_admin_panel_screen(self):
        user = self._profileless_superuser()
        client = Client()
        client.force_login(user)
        for name in ('admin_master_switches', 'admin_user_management',
                     'admin_notification_prefs', 'admin_departments'):
            with self.subTest(screen=name):
                self.assertEqual(client.get(reverse(name)).status_code, 403)

    def test_a_real_admin_still_reaches_the_admin_panel(self):
        """The other half — the fail-open closure must not cost a genuine Admin anything."""
        response = _client_for(self.admin).get(reverse('admin_master_switches'))
        self.assertEqual(response.status_code, 200)

    def test_the_login_page_does_not_loop_for_a_profileless_user(self):
        """get_user_dashboard() now answers NO_PROFILE_URL ('/login/') for a profile-less
        user instead of claiming the admin dashboard is their home. login_view carries a
        same-target guard so that cannot become a redirect loop."""
        user = self._profileless_superuser()
        client = Client()
        client.force_login(user)
        response = client.get('/login/')
        self.assertEqual(response.status_code, 200)


class DenialResponseShapeTests(AccessIsolationBase):
    """0.2 standardised authorisation denial on HttpResponseForbidden.

    Before this, `role_required` redirected with a flash message while every
    permissions.py-gated view returned 403 — two denial semantics, which let a probe tell
    "wrong role" from "not yours" and use the difference as an oracle.
    """

    def test_a_wrong_role_gets_403_rather_than_a_redirect(self):
        response = _client_for(self.se_a).get(reverse('admin_master_switches'))
        self.assertEqual(response.status_code, 403)

    def test_an_unauthenticated_user_is_still_redirected_to_login(self):
        """Authentication and authorisation stay distinguishable, deliberately: a caller
        who has not logged in is sent to log in, not given a 403 they cannot act on."""
        response = Client().get(reverse('admin_master_switches'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login/', response['Location'])


# ---------------------------------------------------------------------------
# Task 1 — System Admin
# ---------------------------------------------------------------------------

class SystemAdminUnrestrictedTests(AccessIsolationBase):
    """The one part of 0.2 that WIDENS rather than narrows, and it had to.

    user_can_view_project() had no System Admin branch, so it returned False for them on
    every project they did not personally manage. They reached the product only because
    the PM-only guard did not name them — remove that guard without adding this branch and
    System Admin loses everything. execution-model §2 D-4: Admin and System Admin are
    unrestricted.
    """

    def test_system_admin_reaches_a_project_they_have_no_relationship_to(self):
        response = _client_for(self.sysadmin).get(
            reverse('project_overview', args=[self.project_a.project_id]))
        self.assertEqual(response.status_code, 200)

    def test_system_admin_reaches_an_unrelated_projects_task_and_timeline(self):
        client = _client_for(self.sysadmin)
        task = self._a_task(self.project_b)
        for url in (
            reverse('task_detail', args=[self.project_b.project_id, task.pk]),
            reverse('project_timeline', args=[self.project_b.project_id]),
        ):
            with self.subTest(url=url):
                self.assertEqual(client.get(url).status_code, 200)

    def test_the_system_admin_widening_did_not_reach_the_boq(self):
        """THE STOP CONDITION FOR TASK 1, pinned as a test. user_can_view_project_boq()
        keeps its own BOQ_PORTFOLIO_READ_ROLES frozenset, which does not list System Admin,
        and does not defer to user_can_view_project(). Widening project visibility must
        never widen BOQ access as a side effect."""
        response = _client_for(self.sysadmin).get(
            reverse('boq_detail', args=[self.project_a.project_id]))
        self.assertEqual(response.status_code, 403)
