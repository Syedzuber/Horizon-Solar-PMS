"""
Residential regression baseline — prompt 0.2a.

WHY THIS FILE EXISTS
--------------------
Prompt 0.2 is a permissions lockdown: eighteen unscoped endpoints, eight Issue endpoints
and three role-gates that lack a scope gate. Lockdowns do not fail by under-blocking.
They fail by OVER-blocking — by taking away access somebody legitimately needed, which
nobody notices until a real user is stuck mid-workflow on a live residential project.

This file is the net that catches that. Every test here asserts what a role can
CORRECTLY do today, so anything 0.2 breaks fails loudly in seconds.

THE ONE RULE
------------
NO TEST HERE ASSERTS A VULNERABILITY. `ACCESS_ISOLATION_AUDIT.md` ranks fifteen
findings, all of them current behaviour. Pinning any of them would mean 0.2 could not
fix them without appearing to cause a regression — and someone would "fix" the test
instead of keeping the fix.

Every assertion below is anchored on a relationship that GENUINELY EXISTS:
  * this PM manages this project
  * this Site Engineer holds a task on this project
  * this Finance user is the assignee of this milestone task
  * this Design user is `assigned_design` on this project

Where a legitimate behaviour is currently delivered ONLY through a broken gate — a
project-relationship-free `@role_required` — the test asserts the RELATIONSHIP, not the
gate, and says so in a comment. `confirm_grn` and `set_milestone_amounts` are the two
that matter: 0.2 must keep the related actor working, and is free to shut the unrelated
one out.

Behaviours deliberately NOT tested, because they are audit findings and 0.2 must be free
to remove them: an unrelated SE confirming a GRN (F1); any authenticated user reaching an
Issue write (F2); a role-matcher changing status on an unrelated project (F3); any write
path reaching a soft-deleted project (F4); a PM or BD setting milestone amounts on
someone else's project (F5); a non-PM opening an unrelated `project_overview` /
`task_detail` / `project_timeline` / `issue_detail` (F6); an unrelated upload (F7); a
non-CEO reaching `dashboard_ceo` (F8); `tasks_drill_down` falling through to the whole
portfolio for SCM/Finance/BD/System Admin (F9); a profile-less superuser passing an Admin
screen (F15).

THE FIXTURE'S SHAPE IS THE POINT
--------------------------------
There are TWO activated Residential projects with DIFFERENT PMs, Site Engineers and
designers. Half of these tests are "the right person can", and their value comes entirely
from a neighbouring project the same person should not reach. A one-project fixture would
pass every isolation test by accident.

`RESIDENTIAL_FINANCE_ASSIGNEE_EMAIL` is required data, not decoration: activation raises
and rolls back without that account (`utils.attach_residential_template`), so the Finance
user is the first line of the fixture and every activation test depends on it.

Supabase is mocked at `projects.supabase_storage.get_supabase_client` only — the real
`_validate_and_upload()` still runs, so extension, size and MIME checks are exercised for
real and only the network call is faked.

Run with:
    python manage.py test projects --settings=solarpms.test_settings

See RESIDENTIAL_BASELINE.md for the lifecycle these tests encode, and for the six defects
found while writing it (B-1..B-6). The two tests that were @skip'd against B-2 and
B-7 are live again: prompt 0.2b fixed both defects.
"""
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.http import Http404
from django.test import Client, TestCase
from django.urls import reverse

from .design_views import _opex_site
from .models import (
    BOQ, BOQItem, BOQItemMaster, BOQRevision, Checklist, ChecklistItem,
    ChecklistItemCompletion, ChecklistTaskLink, DCLineItem, DeliveryChallan, Issue,
    PaymentMilestone, PaymentRequest, Project, ProjectDocument, SystemSettings, Task,
    UserProfile, Vendor,
)
from .utils import (
    INVOICE_TASK_NAMES, RESIDENTIAL_FINANCE_ASSIGNEE_EMAIL,
    RESIDENTIAL_FINANCE_CONFIRMATION_TASK_NAMES, add_calendar_days, assign_tasks_to,
)

#: The six task names activation back-assigns to the Finance assignee.
FINANCE_TASK_NAMES = INVOICE_TASK_NAMES + RESIDENTIAL_FINANCE_CONFIRMATION_TASK_NAMES


def _profile(username, role, email='', **flags):
    """Create a User and set its auto-created UserProfile's role.

    A post_save signal on User creates the UserProfile, so we fetch and mutate rather
    than creating a second one. Same helper shape as tests_permissions.py.
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
    """A logged-in Client for this profile. Separate Client per actor so a test that
    walks a multi-role workflow never depends on login/logout ordering."""
    client = Client()
    client.force_login(profile.user)
    return client


def _photo(name='site.jpg'):
    """A JPEG upload that passes the real _validate_and_upload() checks."""
    return SimpleUploadedFile(name, b'\xff\xd8\xff\xe0fake-jpeg-bytes',
                              content_type='image/jpeg')


def _pdf(name='handover.pdf'):
    return SimpleUploadedFile(name, b'%PDF-1.4 fake', content_type='application/pdf')


class ResidentialBaselineBase(TestCase):
    """Two activated Residential projects, disjoint casts, plus the shared roles.

    Project A: pm_a / coord_a / se_a / design_a
    Project B: pm_b /           se_b / design_b

    The Finance, SCM, BD, CEO, Admin and System Admin users are shared — they are
    portfolio roles today, and the point of the second project is to separate the
    per-site roles, not those.
    """

    def setUp(self):
        # -- The account without which no Residential project can be activated. --
        self.finance = _profile('fin_base', 'Finance',
                                email=RESIDENTIAL_FINANCE_ASSIGNEE_EMAIL)

        # -- Project A cast --
        self.pm_a     = _profile('pm_a',     'PM')
        self.coord_a  = _profile('coord_a',  'Project Coordinator')
        self.se_a     = _profile('se_a',     'Site Engineer')
        self.design_a = _profile('design_a', 'Design')

        # -- Project B cast — the neighbouring project nobody above should reach --
        self.pm_b     = _profile('pm_b',     'PM')
        self.se_b     = _profile('se_b',     'Site Engineer')
        self.design_b = _profile('design_b', 'Design')

        # -- Shared portfolio roles --
        self.scm      = _profile('scm_u',   'SCM')
        self.bd       = _profile('bd_u',    'BD')
        self.ceo      = _profile('ceo_u',   'CEO')
        self.admin    = _profile('admin_u', 'Admin')
        self.sysadmin = _profile('sa_u',    'System Admin')

        # -- Residential BOQ catalogue. Migrations are disabled under test_settings, so
        #    the data migration that seeds the 37 production rows never runs and
        #    get_standard_boq_items() would raise RuntimeError on an empty catalogue.
        #    Three rows are enough to exercise seed → author → submit for real. --
        for order, (code, desc, cat, unit) in enumerate([
            ('ITM-001', 'Solar Module 540Wp',      'Solar Modules', 'Nos'),
            ('ITM-002', 'Module Mounting Structure', 'Structure',   'Nos'),
            ('ITM-003', 'String Inverter 5kW',     'Inverter',      'Nos'),
        ], start=1):
            BOQItemMaster.objects.create(
                code=code, description=desc, category=cat, unit=unit,
                project_type='Residential', is_active=True, sort_order=order,
            )

        self.project_a = self._make_and_activate(
            'Alpha Residence', self.pm_a, self.design_a, self.se_a,
            contract_value=Decimal('300000.00'),
        )
        self.project_a.coordinators.add(self.coord_a)

        self.project_b = self._make_and_activate(
            'Bravo Residence', self.pm_b, self.design_b, self.se_b,
            contract_value=Decimal('450000.00'),
        )

    # -- fixture helpers -----------------------------------------------------

    def _make_and_activate(self, customer_name, pm, designer, site_engineer,
                           contract_value=None):
        """Create a Draft Residential project and activate it THROUGH THE REAL VIEW.

        Activation is driven over HTTP rather than by calling
        attach_residential_template() directly, so the fixture itself is a standing
        assertion that the PM-owns-project gate, the designer requirement and the
        atomic seed all still work. If 0.2 breaks activation, every test in this file
        errors in setUp — which is the loudest possible failure.
        """
        project = Project.objects.create(
            customer_name=customer_name,
            customer_phone='9876543210',
            site_address='1 Sun Road',
            city='Lucknow',
            project_type='Residential',
            capacity_kw=Decimal('5.00'),
            contract_value=contract_value,
            status='Draft',
            assigned_pm=pm,
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

        # Activation deliberately leaves every SE task unassigned (utils.py:875). Hand
        # this project's engineer their real tasks, which is what a PM does through
        # task_assign and what every SE-scoped queryset keys on.
        assign_tasks_to(
            Task.objects.filter(phase__project=project,
                                assigned_role=Task.SITE_ENGINEER),
            site_engineer,
        )
        return project

    def _task(self, project, task_name):
        return Task.objects.get(phase__project=project, task_name=task_name)

    def _seed_boq(self, project, quantity=Decimal('10.00')):
        """Create a BOQ with priced items directly. Used by tests whose subject is
        downstream of the BOQ (payment requests, delivery); the authoring path itself
        is exercised for real in BOQWorkflowTests."""
        boq = BOQ.objects.create(project=project)
        for index, master in enumerate(BOQItemMaster.objects.filter(
                project_type='Residential').order_by('sort_order'), start=1):
            BOQItem.objects.create(
                boq=boq, serial_no=index, category=master.category,
                description=master.description, uom=master.unit,
                item_master=master, boq_quantity=quantity,
            )
        return boq

    def _make_dc(self, project, dc_number='DC-001', ordered=Decimal('10.00')):
        challan = DeliveryChallan.objects.create(
            project=project, dc_number=dc_number, dc_date=date.today(),
            status=DeliveryChallan.EXPECTED, created_by=self.scm,
        )
        DCLineItem.objects.create(
            challan=challan, boq_category='Solar Modules',
            item_description='Solar Module 540Wp', ordered_quantity=ordered, unit='Nos',
        )
        return challan


# ---------------------------------------------------------------------------
# 2.1 — Activation invariants
# ---------------------------------------------------------------------------

class ActivationInvariantTests(ResidentialBaselineBase):
    """What attach_residential_template() actually seeds.

    The counts are asserted against the CODE'S OWN assertion of 52
    (utils.py:912), not against the success message at views.py:2631, which says
    "53 tasks created" and is wrong. See RESIDENTIAL_BASELINE.md finding B-1.
    """

    def test_nine_phases_and_fifty_two_tasks(self):
        self.assertEqual(self.project_a.phases.count(), 9)
        self.assertEqual(
            Task.objects.filter(phase__project=self.project_a).count(), 52,
            'utils.py asserts == 52 inside the atomic block; the success message '
            'saying 53 is the thing that is wrong.',
        )

    def test_forty_four_internal_eight_external(self):
        tasks = Task.objects.filter(phase__project=self.project_a)
        self.assertEqual(tasks.filter(task_type=Task.INTERNAL).count(), 44)
        self.assertEqual(tasks.filter(task_type=Task.EXTERNAL).count(), 8)

    def test_three_payment_milestones_created_pending(self):
        milestones = self.project_a.milestones.order_by('milestone_name')
        self.assertEqual([m.milestone_name for m in milestones], ['M1', 'M2', 'M3'])
        for milestone in milestones:
            self.assertEqual(milestone.status, PaymentMilestone.PENDING)
            self.assertIsNone(milestone.amount)

    def test_pm_role_tasks_are_assigned_to_the_projects_pm(self):
        pm_tasks = Task.objects.filter(phase__project=self.project_a,
                                       assigned_role=Task.PM)
        self.assertEqual(pm_tasks.count(), 14)
        self.assertEqual(pm_tasks.exclude(assigned_to=self.pm_a).count(), 0)
        # ...and to THIS project's PM, not the other one's.
        self.assertEqual(
            Task.objects.filter(phase__project=self.project_b,
                                assigned_role=Task.PM).exclude(assigned_to=self.pm_b).count(),
            0,
        )

    def test_the_six_named_finance_tasks_go_to_the_finance_assignee(self):
        finance_tasks = Task.objects.filter(phase__project=self.project_a,
                                            task_name__in=FINANCE_TASK_NAMES)
        self.assertEqual(finance_tasks.count(), 6)
        self.assertEqual(finance_tasks.exclude(assigned_to=self.finance).count(), 0)
        # Every Finance-ROLE task on the project is one of those six — there is no
        # seventh Finance task quietly left unassigned.
        self.assertEqual(
            Task.objects.filter(phase__project=self.project_a,
                                assigned_role=Task.FINANCE).count(), 6,
        )

    def test_bd_and_scm_tasks_are_left_unassigned(self):
        """The blocking fact behind audit section D.2: task-based scoping gives SCM
        nothing on a freshly activated Residential project, because nothing assigns
        their eleven tasks."""
        scm_tasks = Task.objects.filter(phase__project=self.project_a,
                                        assigned_role=Task.SCM)
        self.assertEqual(scm_tasks.count(), 11)
        self.assertEqual(scm_tasks.filter(assigned_to__isnull=True).count(), 11)

        bd_tasks = Task.objects.filter(phase__project=self.project_a,
                                       assigned_role=Task.BD)
        self.assertEqual(bd_tasks.count(), 1)
        self.assertIsNone(bd_tasks.first().assigned_to)

    def test_the_three_payment_milestone_flags_sit_on_the_finance_tasks(self):
        flagged = list(
            Task.objects.filter(phase__project=self.project_a, is_payment_milestone=True)
            .values_list('task_name', flat=True)
        )
        self.assertCountEqual(flagged, list(RESIDENTIAL_FINANCE_CONFIRMATION_TASK_NAMES))
        # Plant Commissioning is a Site Engineer task and carries NO flag, despite
        # older notes describing it as the M3 trigger.
        self.assertFalse(self._task(self.project_a, 'Plant Commissioning').is_payment_milestone)

    def test_activation_rolls_back_entirely_without_the_finance_assignee(self):
        """Activation is atomic. Remove the required Finance account and the project
        must stay Draft with zero phases, zero tasks and zero milestones — never a
        half-seeded project."""
        UserProfile.objects.filter(user__email=RESIDENTIAL_FINANCE_ASSIGNEE_EMAIL).delete()
        User.objects.filter(email=RESIDENTIAL_FINANCE_ASSIGNEE_EMAIL).delete()

        draft = Project.objects.create(
            customer_name='Charlie Residence', customer_phone='9876543210',
            site_address='3 Sun Road', city='Lucknow', project_type='Residential',
            capacity_kw=Decimal('4.00'), status='Draft', assigned_pm=self.pm_a,
        )
        with self.assertRaises(UserProfile.DoesNotExist):
            _client_for(self.pm_a).post(
                reverse('project_activate', args=[draft.project_id]),
                {'assigned_design_id': self.design_a.pk},
            )

        draft.refresh_from_db()
        self.assertEqual(draft.status, 'Draft')
        self.assertIsNone(draft.activated_at)
        self.assertEqual(draft.phases.count(), 0)
        self.assertEqual(Task.objects.filter(phase__project=draft).count(), 0)
        self.assertEqual(draft.milestones.count(), 0)

    def test_activation_requires_a_designer(self):
        """No designer selected → no activation. This is what makes the project visible
        on the Design dashboard once Design tasks are seeded."""
        draft = Project.objects.create(
            customer_name='Delta Residence', customer_phone='9876543210',
            site_address='4 Sun Road', city='Lucknow', project_type='Residential',
            capacity_kw=Decimal('4.00'), status='Draft', assigned_pm=self.pm_a,
        )
        _client_for(self.pm_a).post(
            reverse('project_activate', args=[draft.project_id]), {},
        )
        draft.refresh_from_db()
        self.assertEqual(draft.status, 'Draft')
        self.assertEqual(draft.phases.count(), 0)

    def test_due_dates_start_null(self):
        self.assertEqual(
            Task.objects.filter(phase__project=self.project_a,
                                due_date__isnull=False).count(), 0,
        )


# ---------------------------------------------------------------------------
# 2.2 — Dashboard reachability AND content
# ---------------------------------------------------------------------------

class DashboardReachabilityTests(ResidentialBaselineBase):
    """Each role reaches its dashboard AND gets a non-empty result set.

    Counts, never bare 200s. A dashboard that renders with zero rows is the exact
    failure mode 0.2 risks: the narrowing lands, the page still returns 200, and
    nobody notices the cards are gone until a user says so.

    `dashboard_scm` and `dashboard_finance` are the canaries — they are the two the
    Finance/SCM narrowing would empty (audit D.3).
    """

    def test_pm_dashboard_shows_the_pms_own_projects(self):
        response = _client_for(self.pm_a).get(reverse('dashboard_pm'))
        self.assertEqual(response.status_code, 200)
        rows = response.context['projects_with_progress']
        self.assertEqual(len(rows), 1)
        self.assertGreaterEqual(response.context['summary']['active_projects'], 1)
        # Scoped: pm_a's dashboard does not carry pm_b's project.
        ids = {r['project'].project_id for r in rows}
        self.assertIn(self.project_a.project_id, ids)
        self.assertNotIn(self.project_b.project_id, ids)

    def test_project_coordinator_reaches_the_pm_dashboard_scoped_to_coordinated_sites(self):
        response = _client_for(self.coord_a).get(reverse('dashboard_pm'))
        self.assertEqual(response.status_code, 200)
        ids = {r['project'].project_id for r in response.context['projects_with_progress']}
        self.assertEqual(ids, {self.project_a.project_id})

    def test_site_engineer_dashboard_shows_the_projects_they_hold_tasks_on(self):
        response = _client_for(self.se_a).get(reverse('dashboard_site_engineer'))
        self.assertEqual(response.status_code, 200)
        projects = response.context['projects']
        self.assertEqual(len(projects), 1,
                         'se_a holds 14 tasks on project A and none on project B')

    def test_design_dashboard_shows_the_assigned_designers_project(self):
        response = _client_for(self.design_a).get(reverse('dashboard_design'))
        self.assertEqual(response.status_code, 200)
        rows = response.context['project_rows']
        self.assertEqual(len(rows), 1)

    def test_finance_dashboard_is_not_empty(self):
        """CANARY. Finance holds six real task assignments on each project via
        RESIDENTIAL_FINANCE_ASSIGNEE_EMAIL. If 0.2 narrows Finance and this goes to
        zero, the narrowing emptied the dashboard."""
        response = _client_for(self.finance).get(reverse('dashboard_finance'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['project_rows']), 2)
        self.assertEqual(response.context['total_milestones_awaiting'], 6)
        self.assertEqual(response.context['total_client_contract_value'],
                         Decimal('750000.00'))

    def test_scm_dashboard_is_not_empty(self):
        """CANARY. SCM holds NO task on either project — activation leaves all eleven
        SCM tasks unassigned — so SCM's dashboard is portfolio-wide by remit and this
        is what task-assignment scoping would zero out (audit D.3)."""
        response = _client_for(self.scm).get(reverse('dashboard_scm'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['project_rows']), 2)

    def test_scm_dashboard_counts_a_submitted_boq_awaiting_acknowledgement(self):
        boq = self._seed_boq(self.project_a)
        boq.status = 'Submitted'
        boq.submitted_by = self.design_a
        boq.save(update_fields=['status', 'submitted_by'])

        response = _client_for(self.scm).get(reverse('dashboard_scm'))
        self.assertEqual(response.context['summary']['boq_awaiting'], 1)

    def test_ceo_dashboard_shows_the_whole_portfolio(self):
        response = _client_for(self.ceo).get(reverse('dashboard_ceo'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['proj_total'], 2)
        self.assertEqual(len(response.context['project_cards']), 2)

    def test_bd_dashboard_is_not_empty(self):
        response = _client_for(self.bd).get(reverse('dashboard_bd'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['project_rows']), 2)

    def test_admin_dashboard_and_project_list_are_reachable(self):
        client = _client_for(self.admin)
        self.assertEqual(client.get(reverse('dashboard_admin')).status_code, 200)
        response = client.get(reverse('admin_project_list'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['projects']), 2)

    def test_system_admin_lands_on_subadmin_projects_with_rows(self):
        response = _client_for(self.sysadmin).get(reverse('subadmin_projects'))
        self.assertEqual(response.status_code, 200)
        assigned = response.context['assigned']
        self.assertEqual(len(assigned), 2,
                         'both projects have an assigned PM, so both sit in `assigned`')

    def test_landing_is_reachable_for_the_three_landing_roles(self):
        for profile in (self.ceo, self.finance, self.scm):
            with self.subTest(role=profile.role):
                response = _client_for(profile).get(reverse('landing'))
                self.assertEqual(response.status_code, 200)


# ---------------------------------------------------------------------------
# 2.3 — The workflows that must survive
# ---------------------------------------------------------------------------

class BOQWorkflowTests(ResidentialBaselineBase):
    """Author → submit → acknowledge → request revision → resubmit.

    NOTE ON THE PROMPT. 0.2a asks for "SCM requests a revision". SCM cannot:
    `boq_request_revision` routes through `user_can_manage_project()` and 403s for SCM
    on every project (views.py:6042). The revision requester is the PM or a Project
    Coordinator. Both are asserted below, plus SCM's refusal.
    """

    def test_assigned_designer_seeds_and_reads_the_boq_on_first_load(self):
        client = _client_for(self.design_a)
        response = client.get(reverse('boq_detail', args=[self.project_a.project_id]))
        self.assertEqual(response.status_code, 200)
        boq = BOQ.objects.get(project=self.project_a)
        self.assertEqual(boq.status, 'Draft')
        self.assertEqual(boq.items.count(), 3)
        # serial_no comes from the catalogue's sort_order, not row position.
        self.assertEqual(list(boq.items.order_by('serial_no')
                              .values_list('serial_no', flat=True)), [1, 2, 3])

    def _author(self, client, project, action='save_design', qty='12'):
        """Drive the Design authoring POST on boq_detail — the path the UI uses.

        The button in boq_detail.html posts `action=submit_design` to this view; the
        standalone `boq_submit` endpoint was a second implementation of the same act; 0.2b
        made it call the same _boq_snapshot() helper (see the test below).
        """
        boq = BOQ.objects.get(project=project)
        payload = {'action': action, 'notes': 'first pass'}
        for item in boq.items.all():
            payload[f'boq_qty_{item.pk}'] = qty
        return client.post(reverse('boq_detail', args=[project.project_id]), payload)

    def test_assigned_designer_authors_and_submits(self):
        client = _client_for(self.design_a)
        client.get(reverse('boq_detail', args=[self.project_a.project_id]))
        boq = BOQ.objects.get(project=self.project_a)

        self._author(client, self.project_a, 'save_design')
        boq.refresh_from_db()
        self.assertEqual(boq.items.filter(boq_quantity=Decimal('12.00')).count(), 3)
        self.assertEqual(boq.status, 'Draft')

        response = self._author(client, self.project_a, 'submit_design')
        self.assertEqual(response.status_code, 302)
        boq.refresh_from_db()
        self.assertEqual(boq.status, 'Submitted')
        self.assertEqual(boq.submitted_by, self.design_a)
        self.assertIsNotNone(boq.submitted_at)
        self.assertEqual(boq.version, 1)
        # One snapshot row per transition — R-8's existing precedent.
        self.assertEqual(boq.revisions.count(), 1)
        self.assertEqual(boq.revisions.first().reason, 'Initial submission')

    def test_the_standalone_boq_submit_endpoint_also_submits(self):
        """B-7, fixed by 0.2b: boq_submit now snapshots through _boq_snapshot(), which
        coerces Decimal. It previously built the snapshot from a raw .values() and raised
        an unhandled TypeError on every submission."""
        client = _client_for(self.design_a)
        client.get(reverse('boq_detail', args=[self.project_a.project_id]))
        self._author(client, self.project_a, 'save_design')
        client.post(reverse('boq_submit', args=[self.project_a.project_id]))
        self.assertEqual(BOQ.objects.get(project=self.project_a).status, 'Submitted')

    def test_submit_is_refused_when_no_item_carries_a_quantity(self):
        client = _client_for(self.design_a)
        client.get(reverse('boq_detail', args=[self.project_a.project_id]))

        # Both submit paths refuse before touching the snapshot, so both are safe to
        # assert here.
        client.post(reverse('boq_detail', args=[self.project_a.project_id]),
                    {'action': 'submit_design'})
        self.assertEqual(BOQ.objects.get(project=self.project_a).status, 'Draft')

        client.post(reverse('boq_submit', args=[self.project_a.project_id]))
        self.assertEqual(BOQ.objects.get(project=self.project_a).status, 'Draft')

    def test_scm_acknowledges_a_submitted_boq(self):
        boq = self._seed_boq(self.project_a)
        boq.status = 'Submitted'
        boq.save(update_fields=['status'])

        response = _client_for(self.scm).post(
            reverse('boq_acknowledge', args=[self.project_a.project_id]))
        self.assertEqual(response.status_code, 302)
        boq.refresh_from_db()
        self.assertEqual(boq.status, 'Acknowledged')

    def test_scm_acknowledge_is_refused_on_a_boq_that_is_not_submitted(self):
        boq = self._seed_boq(self.project_a)          # status Draft
        _client_for(self.scm).post(
            reverse('boq_acknowledge', args=[self.project_a.project_id]))
        boq.refresh_from_db()
        self.assertEqual(boq.status, 'Draft')

    def test_project_pm_requests_a_revision_and_the_designer_resubmits(self):
        boq = self._seed_boq(self.project_a)
        boq.status = 'Submitted'
        boq.save(update_fields=['status'])

        response = _client_for(self.pm_a).post(
            reverse('boq_request_revision', args=[self.project_a.project_id]),
            {'reason': 'Module wattage does not match the survey.'},
        )
        self.assertEqual(response.status_code, 302)
        boq.refresh_from_db()
        self.assertEqual(boq.status, 'Revision Requested')
        self.assertEqual(boq.revisions.count(), 1)
        self.assertIn('Revision requested', boq.revisions.first().reason)

        # Resubmission increments the version.
        self._author(_client_for(self.design_a), self.project_a, 'submit_design')
        boq.refresh_from_db()
        self.assertEqual(boq.status, 'Submitted')
        self.assertEqual(boq.version, 2)
        self.assertEqual(
            BOQRevision.objects.filter(boq=boq, reason='Revision v2').count(), 1)

    def test_project_coordinator_may_also_request_a_revision(self):
        """user_can_manage_project() treats a coordinator as PM-equivalent, and
        boq_request_revision routes through it. The docstring records that the older
        'PM only' wording was itself wrong."""
        boq = self._seed_boq(self.project_a)
        boq.status = 'Submitted'
        boq.save(update_fields=['status'])

        _client_for(self.coord_a).post(
            reverse('boq_request_revision', args=[self.project_a.project_id]),
            {'reason': 'Coordinator review.'},
        )
        boq.refresh_from_db()
        self.assertEqual(boq.status, 'Revision Requested')

    def test_scm_cannot_request_a_revision(self):
        """Correct scoping today, asserted so 0.2 cannot loosen it by accident."""
        boq = self._seed_boq(self.project_a)
        boq.status = 'Submitted'
        boq.save(update_fields=['status'])

        response = _client_for(self.scm).post(
            reverse('boq_request_revision', args=[self.project_a.project_id]),
            {'reason': 'nope'},
        )
        self.assertEqual(response.status_code, 403)
        boq.refresh_from_db()
        self.assertEqual(boq.status, 'Submitted')

    def test_boq_history_is_readable_by_the_designer_and_the_pm(self):
        boq = self._seed_boq(self.project_a)
        BOQRevision.objects.create(boq=boq, revised_by=self.design_a, version=1,
                                   reason='Initial submission', snapshot=[])
        for profile in (self.design_a, self.pm_a, self.scm):
            with self.subTest(role=profile.role):
                response = _client_for(profile).get(
                    reverse('boq_history', args=[self.project_a.project_id]))
                self.assertEqual(response.status_code, 200)


class DeliveryGRNWorkflowTests(ResidentialBaselineBase):
    """SCM raises the challan; the Site Engineer holding a task on that project
    confirms receipt; the DC status rolls up.

    `confirm_grn` is `@role_required(['Site Engineer'])` with NO project term — audit
    finding 1. This test asserts the SE WHO HOLDS TASKS ON THIS PROJECT, which is the
    relationship 0.2 must preserve. It deliberately does not assert the unrelated-SE
    path, which 0.2 is free to close.
    """

    def test_scm_creates_a_delivery_challan_with_line_items(self):
        response = _client_for(self.scm).post(
            reverse('create_delivery_challan', args=[self.project_a.project_id]),
            {
                'dc_number': 'DC-2026-001',
                'dc_date': date.today().isoformat(),
                'expected_delivery_date': (date.today() + timedelta(days=5)).isoformat(),
                'line_item_category_0': 'Solar Modules',
                'line_item_description_0': 'Solar Module 540Wp',
                'line_item_qty_0': '10',
                'line_item_unit_0': 'Nos',
            },
        )
        self.assertEqual(response.status_code, 302)
        challan = DeliveryChallan.objects.get(project=self.project_a)
        self.assertEqual(challan.status, DeliveryChallan.EXPECTED,
                         'no line item has a received quantity yet')
        self.assertEqual(challan.created_by, self.scm)
        self.assertEqual(challan.line_items.count(), 1)

    def test_a_challan_with_no_parseable_line_item_is_refused(self):
        _client_for(self.scm).post(
            reverse('create_delivery_challan', args=[self.project_a.project_id]),
            {'dc_number': 'DC-EMPTY', 'dc_date': date.today().isoformat()},
        )
        self.assertEqual(DeliveryChallan.objects.filter(project=self.project_a).count(), 0)

    def test_the_site_engineer_on_this_project_confirms_a_full_grn(self):
        challan = self._make_dc(self.project_a, ordered=Decimal('10.00'))
        item = challan.line_items.first()

        response = _client_for(self.se_a).post(
            reverse('confirm_grn', args=[self.project_a.project_id, challan.pk]),
            {f'received_qty_{item.pk}': '10', f'damaged_qty_{item.pk}': '0',
             f'grn_notes_{item.pk}': 'all good'},
        )
        self.assertEqual(response.status_code, 302)

        item.refresh_from_db()
        challan.refresh_from_db()
        self.assertEqual(item.received_quantity, Decimal('10.00'))
        self.assertEqual(item.damaged_quantity, 0)
        self.assertEqual(item.condition, DCLineItem.GOOD)
        self.assertEqual(item.grn_confirmed_by, self.se_a)
        self.assertEqual(item.grn_date, date.today())
        self.assertEqual(challan.status, DeliveryChallan.RECEIVED)

    def test_a_shortfall_alone_rolls_up_to_partially_received(self):
        challan = self._make_dc(self.project_a, 'DC-SHORT', ordered=Decimal('10.00'))
        item = challan.line_items.first()
        _client_for(self.se_a).post(
            reverse('confirm_grn', args=[self.project_a.project_id, challan.pk]),
            {f'received_qty_{item.pk}': '7', f'damaged_qty_{item.pk}': '0'},
        )
        item.refresh_from_db(); challan.refresh_from_db()
        # `condition` is derived from DAMAGE ALONE, so a pure shortfall reads 'Good'
        # while the DC rolls up to 'Partially Received'. The two disagree by design —
        # damaged_quantity is the source of truth and `condition` is kept only for
        # backward compatibility (views.py:8991).
        self.assertEqual(item.condition, DCLineItem.GOOD)
        self.assertEqual(challan.status, DeliveryChallan.PARTIALLY_RECEIVED)

    def test_a_shortfall_with_damage_rolls_up_to_rejected(self):
        """'Rejected' here means SEVERE DELIVERY FAILURE — two stacked problems — not
        a refused consignment (models.py:1304)."""
        challan = self._make_dc(self.project_a, 'DC-BAD', ordered=Decimal('10.00'))
        item = challan.line_items.first()
        _client_for(self.se_a).post(
            reverse('confirm_grn', args=[self.project_a.project_id, challan.pk]),
            {f'received_qty_{item.pk}': '6', f'damaged_qty_{item.pk}': '2'},
        )
        item.refresh_from_db(); challan.refresh_from_db()
        self.assertEqual(item.condition, DCLineItem.PARTIAL)
        self.assertEqual(challan.status, DeliveryChallan.REJECTED)

    def test_a_confirmed_dc_from_another_project_is_unreachable_through_this_url(self):
        """Cross-project guard, correct today: the DC must belong to the URL project."""
        other_challan = self._make_dc(self.project_b, 'DC-B-001')
        response = _client_for(self.se_a).post(
            reverse('confirm_grn', args=[self.project_a.project_id, other_challan.pk]), {})
        self.assertEqual(response.status_code, 404)

    def test_scm_overrides_a_grn_without_overwriting_the_original_engineer(self):
        challan = self._make_dc(self.project_a, 'DC-OVR', ordered=Decimal('10.00'))
        item = challan.line_items.first()
        _client_for(self.se_a).post(
            reverse('confirm_grn', args=[self.project_a.project_id, challan.pk]),
            {f'received_qty_{item.pk}': '8', f'damaged_qty_{item.pk}': '0'},
        )
        _client_for(self.scm).post(
            reverse('override_grn', args=[self.project_a.project_id, challan.pk]),
            {f'received_qty_{item.pk}': '10', f'damaged_qty_{item.pk}': '0'},
        )
        item.refresh_from_db(); challan.refresh_from_db()
        self.assertEqual(item.received_quantity, Decimal('10.00'))
        self.assertEqual(item.grn_confirmed_by, self.se_a,
                         'the original SE submitter is preserved on override')
        self.assertEqual(challan.status, DeliveryChallan.RECEIVED)


class MilestoneAndPaymentWorkflowTests(ResidentialBaselineBase):
    """M1 advance, M2 pre-dispatch, M3 final — plus the SCM→Finance vendor path."""

    def _set_amounts(self, client, project, m1, m2, m3):
        return client.post(
            reverse('set_milestone_amounts', args=[project.project_id]),
            data=f'{{"m1_amount": {m1}, "m2_amount": {m2}, "m3_amount": {m3}}}',
            content_type='application/json',
        )

    def test_the_projects_own_pm_sets_milestone_amounts(self):
        """set_milestone_amounts carries no ownership check (audit finding 5). This
        asserts the PM ON THIS PROJECT succeeding — the relationship, not the gate."""
        response = self._set_amounts(_client_for(self.pm_a), self.project_a,
                                     100000, 100000, 100000)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['success'])
        amounts = {m.milestone_name: m.amount for m in self.project_a.milestones.all()}
        self.assertEqual(amounts,
                         {'M1': Decimal('100000.00'), 'M2': Decimal('100000.00'),
                          'M3': Decimal('100000.00')})

    def test_amounts_that_do_not_sum_to_the_contract_value_are_refused(self):
        """The contract-value check fires only when all three would be non-null. Note
        the response is 200 with success=False — every failure on this endpoint is."""
        response = self._set_amounts(_client_for(self.pm_a), self.project_a,
                                     100000, 100000, 50000)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body['success'])
        self.assertIn('contract value', body['error'])
        self.assertEqual(
            self.project_a.milestones.filter(amount__isnull=False).count(), 0)

    def test_finance_invoices_m1_and_records_receipt(self):
        self._set_amounts(_client_for(self.pm_a), self.project_a, 100000, 100000, 100000)
        m1 = self.project_a.milestones.get(milestone_name='M1')
        client = _client_for(self.finance)

        client.post(reverse('milestone_invoice',
                            args=[self.project_a.project_id, m1.pk]))
        m1.refresh_from_db()
        self.assertEqual(m1.status, 'Invoiced')
        self.assertEqual(m1.invoice_date, date.today())

        client.post(reverse('milestone_receive',
                            args=[self.project_a.project_id, m1.pk]),
                    {'amount_received': '100000'})
        m1.refresh_from_db()
        self.assertEqual(m1.status, 'Received')
        self.assertEqual(m1.amount_received, Decimal('100000.00'))
        self.assertEqual(m1.received_date, date.today())

    def test_m1_receipt_closes_the_advance_payment_confirmation_task(self):
        """The milestone → task half of the bidirectional sync. M1 and M3 map to task
        names that exist. M2's did not until 0.2b derived the reverse map from the
        forward one — see B-2 and the test below."""
        m1 = self.project_a.milestones.get(milestone_name='M1')
        m1.status = 'Invoiced'
        m1.save(update_fields=['status'])

        _client_for(self.finance).post(
            reverse('milestone_receive', args=[self.project_a.project_id, m1.pk]),
            {'amount_received': '100000'},
        )
        task = self._task(self.project_a, 'Advance Payment Confirmation')
        self.assertEqual(task.status, Task.DONE)
        self.assertIsNotNone(task.completed_at)

    def test_m2_receipt_closes_the_pre_dispatch_payment_confirmation_task(self):
        """B-2, fixed by 0.2b: the milestone → task direction now reads
        MILESTONE_TO_FINANCE_TASK, derived from the forward map, so M2 resolves to the
        task that exists instead of the deleted 'Finance Confirmation'."""
        m2 = self.project_a.milestones.get(milestone_name='M2')
        m2.status = 'Invoiced'
        m2.save(update_fields=['status'])
        _client_for(self.finance).post(
            reverse('milestone_receive', args=[self.project_a.project_id, m2.pk]),
            {'amount_received': '100000'},
        )
        self.assertEqual(
            self._task(self.project_a, 'Pre Dispatch Payment Confirmation').status,
            Task.DONE)

    def test_overpayment_auto_fills_a_variance_reason(self):
        self._set_amounts(_client_for(self.pm_a), self.project_a, 100000, 100000, 100000)
        m3 = self.project_a.milestones.get(milestone_name='M3')
        m3.status = 'Invoiced'
        m3.save(update_fields=['status'])

        _client_for(self.finance).post(
            reverse('milestone_receive', args=[self.project_a.project_id, m3.pk]),
            {'amount_received': '105000'},
        )
        m3.refresh_from_db()
        self.assertEqual(m3.variance_reason, 'Overpayment')

    def test_completing_a_finance_confirmation_task_flips_its_milestone_to_received(self):
        """The task → milestone half. The Finance assignee holds this task by name, so
        the role-match branch of task_status_update fires on a real relationship."""
        task = self._task(self.project_a, '100% Payment Confirmation')
        Task.objects.filter(pk=task.pk).update(due_date=date.today())

        _client_for(self.finance).post(
            reverse('task_status_update', args=[self.project_a.project_id, task.pk]),
            {'status': Task.DONE, 'amount_received': '100000'},
        )
        task.refresh_from_db()
        self.assertEqual(task.status, Task.DONE)
        m3 = self.project_a.milestones.get(milestone_name='M3')
        self.assertEqual(m3.status, 'Received')
        self.assertEqual(m3.amount_received, Decimal('100000.00'))

    def test_scm_raises_a_payment_request_and_finance_confirms_it(self):
        boq = self._seed_boq(self.project_a)
        boq_item = boq.items.first()
        vendor = Vendor.objects.create(name='Sunrise Traders',
                                       contact_person='R Kumar', phone='9000000001')

        with patch('projects.supabase_storage.get_supabase_client',
                   return_value=MagicMock()):
            response = _client_for(self.scm).post(
                reverse('raise_payment_request', args=[self.project_a.project_id]),
                {
                    'vendor_id': str(vendor.pk),
                    'boq_item_id': str(boq_item.pk),
                    'invoice_number': 'INV-77',
                    'amount': '55000',
                    'note': 'module supply',
                    'invoice_document': _pdf('inv-77.pdf'),
                },
            )
        self.assertEqual(response.status_code, 302)

        pr = PaymentRequest.objects.get(project=self.project_a)
        self.assertEqual(pr.status, PaymentRequest.PENDING)
        self.assertEqual(pr.amount, Decimal('55000.00'))
        self.assertEqual(pr.requested_by, self.scm.user)
        self.assertEqual(pr.invoice_document_name, 'inv-77.pdf')

        response = _client_for(self.finance).post(
            reverse('confirm_payment_request', args=[self.project_a.project_id, pr.pk]),
            {'payment_date': date.today().isoformat(), 'payment_reference': 'UTR-991'},
        )
        self.assertEqual(response.status_code, 302)
        pr.refresh_from_db()
        self.assertEqual(pr.status, PaymentRequest.CONFIRMED)
        self.assertEqual(pr.payment_date, date.today())
        self.assertEqual(pr.payment_reference, 'UTR-991')
        self.assertEqual(pr.confirmed_by, self.finance.user)

    def test_a_payment_request_without_an_invoice_document_is_refused(self):
        """invoice_document is a hard business rule, not a UI nicety."""
        boq = self._seed_boq(self.project_a)
        vendor = Vendor.objects.create(name='V2', contact_person='X', phone='9000000002')
        _client_for(self.scm).post(
            reverse('raise_payment_request', args=[self.project_a.project_id]),
            {'vendor_id': str(vendor.pk), 'boq_item_id': str(boq.items.first().pk),
             'invoice_number': 'INV-78', 'amount': '100'},
        )
        self.assertEqual(PaymentRequest.objects.count(), 0)


class TaskProgressionTests(ResidentialBaselineBase):
    """Status moves, the due-date cascade, and checklist completion."""

    def test_the_assigned_site_engineer_moves_a_task_through_to_done(self):
        task = self._task(self.project_a, 'MMS Installation')
        self.assertEqual(task.assigned_to, self.se_a)
        client = _client_for(self.se_a)
        url = reverse('task_status_update', args=[self.project_a.project_id, task.pk])

        # In Progress requires a due date — supplied inline, as the UI does.
        Task.objects.filter(pk=task.pk).update(due_date=date.today() + timedelta(days=3))
        client.post(url, {'status': Task.IN_PROGRESS})
        task.refresh_from_db()
        self.assertEqual(task.status, Task.IN_PROGRESS)

        client.post(url, {'status': Task.DONE})
        task.refresh_from_db()
        self.assertEqual(task.status, Task.DONE)
        self.assertIsNotNone(task.completed_at)

    def test_in_progress_without_a_due_date_is_refused(self):
        task = self._task(self.project_a, 'Earthing Work')
        _client_for(self.se_a).post(
            reverse('task_status_update', args=[self.project_a.project_id, task.pk]),
            {'status': Task.IN_PROGRESS},
        )
        task.refresh_from_db()
        self.assertEqual(task.status, Task.NOT_STARTED)

    def test_an_unassigned_task_cannot_have_its_status_changed(self):
        """Hard precondition on task_status_update, ahead of any permission branch:
        every SCM task on a fresh Residential project is unassigned."""
        task = self._task(self.project_a, 'Procurement Schedule')
        self.assertIsNone(task.assigned_to)
        response = _client_for(self.scm).post(
            reverse('task_status_update', args=[self.project_a.project_id, task.pk]),
            {'status': Task.IN_PROGRESS},
        )
        self.assertEqual(response.status_code, 400)
        task.refresh_from_db()
        self.assertEqual(task.status, Task.NOT_STARTED)

    def test_blocking_a_task_requires_and_creates_an_issue(self):
        task = self._task(self.project_a, 'Module Installation')
        Task.objects.filter(pk=task.pk).update(due_date=date.today())
        client = _client_for(self.se_a)
        url = reverse('task_status_update', args=[self.project_a.project_id, task.pk])

        client.post(url, {'status': Task.BLOCKED})          # no title
        task.refresh_from_db()
        self.assertEqual(task.status, Task.NOT_STARTED)
        self.assertEqual(Issue.objects.filter(task=task).count(), 0)

        client.post(url, {'status': Task.BLOCKED,
                          'block_issue_title': 'Roof access refused',
                          'block_issue_description': 'Customer away'})
        task.refresh_from_db()
        self.assertEqual(task.status, Task.BLOCKED)
        self.assertIsNotNone(task.blocked_since)
        issue = Issue.objects.get(task=task)
        self.assertEqual(issue.title, 'Roof access refused')
        self.assertEqual(issue.raised_by, self.se_a)
        self.assertEqual(issue.project, self.project_a)

    def test_done_may_only_move_to_blocked(self):
        """The transition table exists to prevent gaming completion."""
        task = self._task(self.project_a, 'DC Wire Work')
        Task.objects.filter(pk=task.pk).update(status=Task.DONE,
                                               due_date=date.today())
        _client_for(self.se_a).post(
            reverse('task_status_update', args=[self.project_a.project_id, task.pk]),
            {'status': Task.IN_PROGRESS},
        )
        task.refresh_from_db()
        self.assertEqual(task.status, Task.DONE)

    def test_the_pm_enables_cascade_and_a_due_date_change_ripples_downstream(self):
        settings_obj = SystemSettings.get()
        settings_obj.cascade_scheduling_enabled = True
        settings_obj.save(update_fields=['cascade_scheduling_enabled'])

        client = _client_for(self.pm_a)
        client.post(reverse('enable_cascade_scheduling',
                            args=[self.project_a.project_id]))
        self.project_a.refresh_from_db()
        self.assertTrue(self.project_a.cascade_scheduling)
        # Enabling triggers a full recalculation off activated_at.
        self.assertEqual(
            Task.objects.filter(phase__project=self.project_a,
                                due_date__isnull=True).count(), 0)

        ordered = list(Task.objects.filter(phase__project=self.project_a)
                       .order_by('phase__phase_order', 'task_order'))
        anchor, following = ordered[0], ordered[1]
        before = following.due_date
        new_date = date.today() + timedelta(days=40)

        client.post(
            reverse('task_set_due_date',
                    args=[self.project_a.project_id, anchor.pk]),
            {'due_date': new_date.isoformat()},
        )
        anchor.refresh_from_db(); following.refresh_from_db()
        self.assertEqual(anchor.due_date, new_date)
        self.assertNotEqual(following.due_date, before)
        # Both are Internal, so the chain advances by the follower's duration.
        self.assertEqual(anchor.task_type, Task.INTERNAL)
        self.assertEqual(following.task_type, Task.INTERNAL)
        self.assertEqual(following.due_date,
                         add_calendar_days(new_date, following.duration_days))
        self.assertTrue(anchor.due_date_changes.exists(),
                        'every cascade change writes a DueDateChangeLog row')

    def test_a_role_owner_may_set_a_due_date_while_cascade_is_off(self):
        task = self._task(self.project_a, 'AC Cable Work')
        target = date.today() + timedelta(days=12)
        _client_for(self.se_a).post(
            reverse('task_set_due_date', args=[self.project_a.project_id, task.pk]),
            {'due_date': target.isoformat()},
        )
        task.refresh_from_db()
        self.assertEqual(task.due_date, target)

    def test_a_role_owner_is_refused_a_due_date_while_cascade_is_on(self):
        Project.objects.filter(pk=self.project_a.pk).update(cascade_scheduling=True)
        task = self._task(self.project_a, 'AC Cable Work')
        _client_for(self.se_a).post(
            reverse('task_set_due_date', args=[self.project_a.project_id, task.pk]),
            {'due_date': (date.today() + timedelta(days=12)).isoformat()},
        )
        task.refresh_from_db()
        self.assertIsNone(task.due_date)

    def test_the_assigned_user_completes_a_checklist_item_with_a_photo(self):
        task = self._task(self.project_a, 'Pre Commissioning Check List')
        # Draft, then items, then activate() — R-7 refuses an item added to an active
        # version, so the fixture has to build one the way the product does.
        checklist = Checklist.objects.create(name='Pre-commissioning')
        item = ChecklistItem.objects.create(checklist=checklist,
                                            label='Earth resistance < 5 ohm', order=1)
        checklist.activate()
        ChecklistTaskLink.objects.create(checklist=checklist,
                                         task_name=task.task_name,
                                         project_type='Residential')

        with patch('projects.supabase_storage.get_supabase_client',
                   return_value=MagicMock()):
            response = _client_for(self.se_a).post(
                reverse('checklist_item_complete',
                        args=[self.project_a.project_id, task.pk, item.pk]),
                {'photo': _photo()},
            )
        self.assertEqual(response.status_code, 302)

        completion = ChecklistItemCompletion.objects.get(item=item, task=task)
        self.assertTrue(completion.is_checked)
        self.assertEqual(completion.checked_by, self.se_a.user)
        self.assertIsNotNone(completion.checked_at)
        # is_checked is written in the SAME save as all three photo fields — a checked
        # item can never lack a photo.
        self.assertEqual(completion.photo_file_name, 'site.jpg')
        self.assertTrue(completion.photo_url)
        self.assertTrue(completion.photo_supabase_path)

    def test_a_checklist_item_cannot_be_checked_without_a_photo(self):
        task = self._task(self.project_a, 'Pre Commissioning Check List')
        # Draft, then items, then activate() — R-7 refuses an item added to an active
        # version, so the fixture has to build one the way the product does.
        checklist = Checklist.objects.create(name='Pre-commissioning')
        item = ChecklistItem.objects.create(checklist=checklist, label='Torque check',
                                            order=1)
        checklist.activate()
        ChecklistTaskLink.objects.create(checklist=checklist, task_name=task.task_name,
                                         project_type='Residential')

        _client_for(self.se_a).post(
            reverse('checklist_item_complete',
                    args=[self.project_a.project_id, task.pk, item.pk]), {})
        self.assertEqual(ChecklistItemCompletion.objects.count(), 0)


class IssueLifecycleTests(ResidentialBaselineBase):
    """Raise → assign → in progress → resolve → reopen → resolve → close, driven by
    the project's own PM throughout."""

    def _raise(self, client, project, title='Inverter arrived scratched', assignee=None):
        payload = {'title': title, 'description': 'noted at GRN', 'severity': Issue.HIGH}
        if assignee is not None:
            payload['assigned_to'] = str(assignee.pk)
        client.post(reverse('create_project_issue', args=[project.project_id]), payload)
        return Issue.objects.get(project=project, title=title)

    def test_pm_raises_an_issue_on_their_own_project(self):
        issue = self._raise(_client_for(self.pm_a), self.project_a,
                            assignee=self.se_a)
        self.assertEqual(issue.status, Issue.OPEN)
        self.assertEqual(issue.raised_by, self.pm_a)
        self.assertEqual(issue.assigned_to, self.se_a)
        self.assertEqual(issue.severity, Issue.HIGH)
        self.assertIsNone(issue.task)

    def test_an_issue_without_a_title_is_not_created(self):
        _client_for(self.pm_a).post(
            reverse('create_project_issue', args=[self.project_a.project_id]),
            {'title': '', 'description': 'x'},
        )
        self.assertEqual(Issue.objects.filter(project=self.project_a).count(), 0)

    def test_the_full_lifecycle_including_reopen_and_close(self):
        client = _client_for(self.pm_a)
        issue = self._raise(client, self.project_a, assignee=self.se_a)

        client.post(reverse('update_issue_status', args=[issue.pk]))
        issue.refresh_from_db()
        self.assertEqual(issue.status, Issue.IN_PROGRESS)

        client.post(reverse('resolve_issue', args=[issue.pk]),
                    {'resolution_note': 'Replacement despatched.'})
        issue.refresh_from_db()
        self.assertEqual(issue.status, Issue.RESOLVED)
        self.assertEqual(issue.resolution_note, 'Replacement despatched.')
        self.assertIsNotNone(issue.resolved_at)

        # Reopen clears the resolution — a Resolved issue can go back to Open.
        client.post(reverse('reopen_issue', args=[issue.pk]))
        issue.refresh_from_db()
        self.assertEqual(issue.status, Issue.OPEN)
        self.assertIsNone(issue.resolved_at)
        self.assertEqual(issue.resolution_note, '')

        client.post(reverse('update_issue_status', args=[issue.pk]))
        client.post(reverse('resolve_issue', args=[issue.pk]),
                    {'resolution_note': 'Replacement fitted.'})
        client.post(reverse('close_issue', args=[issue.pk]))
        issue.refresh_from_db()
        self.assertEqual(issue.status, Issue.CLOSED)
        self.assertIsNotNone(issue.closed_at)

    def test_an_unassigned_issue_cannot_be_moved_to_in_progress(self):
        client = _client_for(self.pm_a)
        issue = self._raise(client, self.project_a)
        self.assertIsNone(issue.assigned_to)
        client.post(reverse('update_issue_status', args=[issue.pk]))
        issue.refresh_from_db()
        self.assertEqual(issue.status, Issue.OPEN)

    def test_resolving_without_a_note_is_refused(self):
        client = _client_for(self.pm_a)
        issue = self._raise(client, self.project_a, assignee=self.se_a)
        client.post(reverse('update_issue_status', args=[issue.pk]))
        client.post(reverse('resolve_issue', args=[issue.pk]), {'resolution_note': '  '})
        issue.refresh_from_db()
        self.assertEqual(issue.status, Issue.IN_PROGRESS)

    def test_a_closed_issue_cannot_be_reassigned(self):
        client = _client_for(self.pm_a)
        issue = self._raise(client, self.project_a, assignee=self.se_a)
        client.post(reverse('update_issue_status', args=[issue.pk]))
        client.post(reverse('resolve_issue', args=[issue.pk]),
                    {'resolution_note': 'done'})
        client.post(reverse('close_issue', args=[issue.pk]))

        response = client.post(reverse('assign_issue', args=[issue.pk]),
                               {'assigned_to': str(self.pm_a.pk)})
        self.assertEqual(response.status_code, 403)
        issue.refresh_from_db()
        self.assertEqual(issue.assigned_to, self.se_a)

    def test_a_task_issue_links_to_its_task(self):
        task = self._task(self.project_a, 'Inverter Installation')
        _client_for(self.pm_a).post(
            reverse('create_task_issue', args=[self.project_a.project_id, task.pk]),
            {'title': 'Inverter DOA', 'severity': Issue.HIGH},
        )
        issue = Issue.objects.get(title='Inverter DOA')
        self.assertEqual(issue.task, task)
        self.assertEqual(issue.project, self.project_a)

    def test_a_delivery_issue_links_to_its_challan_and_is_cross_project_guarded(self):
        challan = self._make_dc(self.project_a, 'DC-ISSUE')
        _client_for(self.pm_a).post(
            reverse('create_delivery_issue',
                    args=[self.project_a.project_id, challan.pk]),
            {'title': 'Short delivery', 'severity': Issue.MEDIUM},
        )
        issue = Issue.objects.get(title='Short delivery')
        self.assertEqual(issue.delivery_challan, challan)

        other = self._make_dc(self.project_b, 'DC-B-ISSUE')
        response = _client_for(self.pm_b).post(
            reverse('create_delivery_issue',
                    args=[self.project_a.project_id, other.pk]),
            {'title': 'cross project', 'severity': Issue.LOW},
        )
        self.assertEqual(response.status_code, 404)


class DocumentUploadTests(ResidentialBaselineBase):
    """Upload by someone with a relationship to the project, then delete."""

    def test_the_project_pm_uploads_a_document_and_deletes_it(self):
        client = _client_for(self.pm_a)
        with patch('projects.supabase_storage.get_supabase_client',
                   return_value=MagicMock()):
            response = client.post(
                reverse('upload_project_document', args=[self.project_a.project_id]),
                {'files': [_pdf('handover.pdf')]},
            )
        self.assertEqual(response.status_code, 302)

        doc = ProjectDocument.objects.get(project=self.project_a)
        self.assertEqual(doc.uploaded_by, self.pm_a)
        self.assertEqual(doc.file_name, 'handover.pdf')
        self.assertEqual(doc.file_type, 'Document')
        self.assertGreaterEqual(doc.file_size_kb, 1)
        self.assertFalse(doc.is_deleted)

        client.post(reverse('delete_project_document',
                            args=[self.project_a.project_id, doc.pk]))
        doc.refresh_from_db()
        self.assertTrue(doc.is_deleted)
        self.assertEqual(doc.deleted_by, self.pm_a)
        self.assertIsNotNone(doc.deleted_at)

    def test_a_photo_upload_is_typed_as_a_photo(self):
        with patch('projects.supabase_storage.get_supabase_client',
                   return_value=MagicMock()):
            _client_for(self.se_a).post(
                reverse('upload_project_document', args=[self.project_a.project_id]),
                {'files': [_photo('roof.jpg')]},
            )
        doc = ProjectDocument.objects.get(project=self.project_a)
        self.assertEqual(doc.file_type, 'Photo')
        self.assertEqual(doc.uploaded_by, self.se_a)

    def test_an_unsupported_extension_is_rejected_without_creating_a_row(self):
        """The real _validate_and_upload() runs — only the network call is mocked."""
        bad = SimpleUploadedFile('payload.exe', b'MZ', content_type='application/x-msdownload')
        with patch('projects.supabase_storage.get_supabase_client',
                   return_value=MagicMock()):
            _client_for(self.pm_a).post(
                reverse('upload_project_document', args=[self.project_a.project_id]),
                {'files': [bad]},
            )
        self.assertEqual(ProjectDocument.objects.count(), 0)

    def test_a_different_user_cannot_delete_someone_elses_document(self):
        """Deletion is uploader-or-Admin, correct today. Asserted so 0.2 does not
        loosen it while widening the upload gate."""
        with patch('projects.supabase_storage.get_supabase_client',
                   return_value=MagicMock()):
            _client_for(self.pm_a).post(
                reverse('upload_project_document', args=[self.project_a.project_id]),
                {'files': [_pdf('pm-doc.pdf')]},
            )
        doc = ProjectDocument.objects.get(project=self.project_a)

        response = _client_for(self.se_a).post(
            reverse('delete_project_document',
                    args=[self.project_a.project_id, doc.pk]))
        self.assertEqual(response.status_code, 403)
        doc.refresh_from_db()
        self.assertFalse(doc.is_deleted)

    def test_an_admin_may_delete_any_document(self):
        with patch('projects.supabase_storage.get_supabase_client',
                   return_value=MagicMock()):
            _client_for(self.pm_a).post(
                reverse('upload_project_document', args=[self.project_a.project_id]),
                {'files': [_pdf('pm-doc.pdf')]},
            )
        doc = ProjectDocument.objects.get(project=self.project_a)
        _client_for(self.admin).post(
            reverse('delete_project_document',
                    args=[self.project_a.project_id, doc.pk]))
        doc.refresh_from_db()
        self.assertTrue(doc.is_deleted)


# ---------------------------------------------------------------------------
# 2.4 — Isolation that must ALREADY pass
# ---------------------------------------------------------------------------

class ExistingIsolationTests(ResidentialBaselineBase):
    """Scoping that is CORRECT today, pinned so 0.2 cannot loosen it by accident.

    None of these is an audit finding — each is a gate that already works.
    """

    def test_a_pm_cannot_edit_another_pms_project(self):
        response = _client_for(self.pm_b).get(
            reverse('project_edit', args=[self.project_a.project_id]))
        self.assertEqual(response.status_code, 404)

    def test_a_pm_cannot_activate_another_pms_project(self):
        draft = Project.objects.create(
            customer_name='Echo Residence', customer_phone='9876543210',
            site_address='5 Sun Road', city='Lucknow', project_type='Residential',
            capacity_kw=Decimal('3.00'), status='Draft', assigned_pm=self.pm_a,
        )
        response = _client_for(self.pm_b).post(
            reverse('project_activate', args=[draft.project_id]),
            {'assigned_design_id': self.design_b.pk},
        )
        self.assertEqual(response.status_code, 404)
        draft.refresh_from_db()
        self.assertEqual(draft.status, 'Draft')
        self.assertEqual(draft.phases.count(), 0)

    def test_a_pm_cannot_read_another_pms_boq(self):
        self._seed_boq(self.project_a)
        response = _client_for(self.pm_b).get(
            reverse('boq_detail', args=[self.project_a.project_id]))
        self.assertEqual(response.status_code, 403)

    def test_the_projects_own_pm_can_read_its_boq(self):
        """The other half of the pair — without this, a 403-everywhere regression
        would look like a passing isolation test."""
        self._seed_boq(self.project_a)
        response = _client_for(self.pm_a).get(
            reverse('boq_detail', args=[self.project_a.project_id]))
        self.assertEqual(response.status_code, 200)

    def test_a_design_user_cannot_read_another_projects_boq(self):
        self._seed_boq(self.project_a)
        response = _client_for(self.design_b).get(
            reverse('boq_detail', args=[self.project_a.project_id]))
        self.assertEqual(response.status_code, 403)

    def test_a_design_user_cannot_submit_another_projects_boq(self):
        boq = self._seed_boq(self.project_a)
        response = _client_for(self.design_b).post(
            reverse('boq_submit', args=[self.project_a.project_id]))
        self.assertEqual(response.status_code, 403)
        boq.refresh_from_db()
        self.assertEqual(boq.status, 'Draft')

    def test_close_and_reopen_refuse_a_non_pm(self):
        """`_is_project_pm()` is the one genuine object-level gate on the ten Issue
        endpoints — role AND ownership."""
        client = _client_for(self.pm_a)
        client.post(reverse('create_project_issue', args=[self.project_a.project_id]),
                    {'title': 'Guarded', 'severity': Issue.MEDIUM,
                     'assigned_to': str(self.se_a.pk)})
        issue = Issue.objects.get(title='Guarded')
        client.post(reverse('update_issue_status', args=[issue.pk]))
        client.post(reverse('resolve_issue', args=[issue.pk]),
                    {'resolution_note': 'fixed'})
        issue.refresh_from_db()
        self.assertEqual(issue.status, Issue.RESOLVED)

        for profile in (self.se_a, self.scm, self.design_a):
            with self.subTest(role=profile.role):
                other = _client_for(profile)
                self.assertEqual(
                    other.post(reverse('close_issue', args=[issue.pk])).status_code, 403)
                self.assertEqual(
                    other.post(reverse('reopen_issue', args=[issue.pk])).status_code, 403)
        issue.refresh_from_db()
        self.assertEqual(issue.status, Issue.RESOLVED)

    def test_a_pm_from_another_project_cannot_close_this_ones_issue(self):
        client = _client_for(self.pm_a)
        client.post(reverse('create_project_issue', args=[self.project_a.project_id]),
                    {'title': 'Not yours', 'severity': Issue.LOW,
                     'assigned_to': str(self.se_a.pk)})
        issue = Issue.objects.get(title='Not yours')
        client.post(reverse('update_issue_status', args=[issue.pk]))
        client.post(reverse('resolve_issue', args=[issue.pk]),
                    {'resolution_note': 'fixed'})

        response = _client_for(self.pm_b).post(reverse('close_issue', args=[issue.pk]))
        self.assertEqual(response.status_code, 403)

    def test_opex_site_refuses_a_residential_project(self):
        """The design module is OPEX-only, by a hard guard at its single entry point."""
        with self.assertRaises(Http404):
            _opex_site(self.project_a.project_id)

    def test_opex_site_refuses_a_soft_deleted_opex_project(self):
        deleted = Project.objects.create(
            customer_name='Gone Site', customer_phone='9876543210',
            site_address='9 Sun Road', city='Lucknow', project_type='OPEX',
            capacity_kw=Decimal('50.00'), status='Active', is_deleted=True,
        )
        with self.assertRaises(Http404):
            _opex_site(deleted.project_id)

    def test_opex_site_accepts_a_live_opex_project(self):
        live = Project.objects.create(
            customer_name='Live Site', customer_phone='9876543210',
            site_address='10 Sun Road', city='Lucknow', project_type='OPEX',
            capacity_kw=Decimal('50.00'), status='Active',
        )
        self.assertEqual(_opex_site(live.project_id).pk, live.pk)

    def test_the_task_drill_down_is_scoped_for_a_pm(self):
        """One of the three surfaces 0.2 touches. PM and Site Engineer are correctly
        scoped today; the roles that fall through are audit finding 9 and are not
        asserted here."""
        for project in (self.project_a, self.project_b):
            Task.objects.filter(phase__project=project).update(due_date=date.today())

        response = _client_for(self.pm_a).get(reverse('tasks_due_today'))
        self.assertEqual(response.status_code, 200)
        ids = {g['project'].project_id for g in response.context['groups']}
        self.assertEqual(ids, {self.project_a.project_id})
        self.assertGreater(response.context['total_count'], 0)

    def test_the_task_drill_down_is_scoped_for_a_site_engineer(self):
        for project in (self.project_a, self.project_b):
            Task.objects.filter(phase__project=project).update(due_date=date.today())

        response = _client_for(self.se_a).get(reverse('tasks_due_today'))
        self.assertEqual(response.status_code, 200)
        ids = {g['project'].project_id for g in response.context['groups']}
        self.assertEqual(ids, {self.project_a.project_id})
        # Scoped to the tasks they hold, not to every task on the project.
        self.assertEqual(response.context['total_count'], 14)


# ---------------------------------------------------------------------------
# 2.5 — Notifications
# ---------------------------------------------------------------------------

class NotificationTests(ResidentialBaselineBase):
    """Who gets told, and on which channels.

    `send_notification()` is patched at its import site in `projects.views` — nothing
    is sent, and the recipient set is asserted directly. `resolve_issue` matters most:
    0.2 changes who can reach it, and the notification is the part that escapes the
    system.
    """

    @staticmethod
    def _recipients(mock):
        return {call.kwargs['recipient'] for call in mock.call_args_list}

    def test_resolving_an_issue_notifies_managers_assignee_and_raiser_but_not_the_resolver(self):
        client = _client_for(self.pm_a)
        client.post(reverse('create_project_issue', args=[self.project_a.project_id]),
                    {'title': 'Notify me', 'severity': Issue.HIGH,
                     'assigned_to': str(self.se_a.pk)})
        issue = Issue.objects.get(title='Notify me')
        client.post(reverse('update_issue_status', args=[issue.pk]))

        with patch('projects.views.send_notification') as sender:
            client.post(reverse('resolve_issue', args=[issue.pk]),
                        {'resolution_note': 'Sorted on site.'})

        issue.refresh_from_db()
        self.assertEqual(issue.status, Issue.RESOLVED)

        recipients = self._recipients(sender)
        # PM is the resolver and is excluded; the coordinator is a project manager.
        self.assertEqual(recipients, {self.coord_a, self.se_a})
        self.assertNotIn(self.pm_a, recipients)
        for call in sender.call_args_list:
            self.assertEqual(call.kwargs['channels'], ['in_app', 'whatsapp', 'email'])
            self.assertEqual(call.kwargs['template'], 'issue_resolved')
            self.assertEqual(call.kwargs['related_project'], self.project_a)
            self.assertEqual(call.kwargs['actor'], self.pm_a)

    def test_resolving_notifies_nobody_outside_this_project(self):
        """The neighbouring project's PM and engineer must not be told."""
        client = _client_for(self.pm_a)
        client.post(reverse('create_project_issue', args=[self.project_a.project_id]),
                    {'title': 'Scoped notify', 'severity': Issue.LOW,
                     'assigned_to': str(self.se_a.pk)})
        issue = Issue.objects.get(title='Scoped notify')
        client.post(reverse('update_issue_status', args=[issue.pk]))

        with patch('projects.views.send_notification') as sender:
            client.post(reverse('resolve_issue', args=[issue.pk]),
                        {'resolution_note': 'done'})

        recipients = self._recipients(sender)
        self.assertNotIn(self.pm_b, recipients)
        self.assertNotIn(self.se_b, recipients)

    def test_raising_an_assigned_issue_notifies_the_assignee_and_the_managers(self):
        with patch('projects.views.send_notification') as sender:
            _client_for(self.pm_a).post(
                reverse('create_project_issue', args=[self.project_a.project_id]),
                {'title': 'New issue', 'severity': Issue.HIGH,
                 'assigned_to': str(self.se_a.pk)},
            )
        recipients = self._recipients(sender)
        self.assertEqual(recipients, {self.se_a, self.coord_a})

        by_recipient = {c.kwargs['recipient']: c.kwargs for c in sender.call_args_list}
        self.assertEqual(by_recipient[self.se_a]['template'], 'issue_created')
        self.assertEqual(by_recipient[self.se_a]['channels'],
                         ['in_app', 'whatsapp', 'email'])
        # Managers get an in-app nudge only — never WhatsApp or email.
        self.assertEqual(by_recipient[self.coord_a]['channels'], ['in_app'])

    def test_completing_a_payment_milestone_task_notifies_finance_managers_bd_and_ceo(self):
        task = self._task(self.project_a, 'Advance Payment Confirmation')
        self.assertTrue(task.is_payment_milestone)
        self.assertEqual(task.assigned_to, self.finance)
        Task.objects.filter(pk=task.pk).update(due_date=date.today())

        with patch('projects.views.send_notification') as sender:
            _client_for(self.finance).post(
                reverse('task_status_update',
                        args=[self.project_a.project_id, task.pk]),
                {'status': Task.DONE},
            )
        task.refresh_from_db()
        self.assertEqual(task.status, Task.DONE)

        recipients = self._recipients(sender)
        self.assertEqual(recipients,
                         {self.finance, self.pm_a, self.coord_a, self.bd, self.ceo})
        for call in sender.call_args_list:
            self.assertEqual(call.kwargs['template'], 'payment_notification')
            self.assertEqual(call.kwargs['channels'], ['in_app', 'whatsapp', 'email'])

    def test_an_ordinary_task_completion_notifies_nobody(self):
        task = self._task(self.project_a, 'Earthing Work')
        Task.objects.filter(pk=task.pk).update(due_date=date.today())
        with patch('projects.views.send_notification') as sender:
            _client_for(self.se_a).post(
                reverse('task_status_update',
                        args=[self.project_a.project_id, task.pk]),
                {'status': Task.DONE},
            )
        sender.assert_not_called()

    def test_confirming_a_payment_request_notifies_scm_managers_and_ceo(self):
        boq = self._seed_boq(self.project_a)
        vendor = Vendor.objects.create(name='Sunrise', contact_person='R',
                                       phone='9000000003')
        pr = PaymentRequest.objects.create(
            project=self.project_a, vendor=vendor, boq_item=boq.items.first(),
            invoice_number='INV-9', invoice_document_name='i.pdf',
            invoice_document_url='http://x/i.pdf', invoice_document_path='p/i.pdf',
            amount=Decimal('25000.00'), requested_by=self.scm.user,
            status=PaymentRequest.PENDING,
        )

        with patch('projects.views.send_notification') as sender:
            _client_for(self.finance).post(
                reverse('confirm_payment_request',
                        args=[self.project_a.project_id, pr.pk]),
                {'payment_date': date.today().isoformat(),
                 'payment_reference': 'UTR-1'},
            )
        pr.refresh_from_db()
        self.assertEqual(pr.status, PaymentRequest.CONFIRMED)

        recipients = self._recipients(sender)
        self.assertEqual(recipients, {self.scm, self.pm_a, self.coord_a, self.ceo})
        for call in sender.call_args_list:
            self.assertEqual(call.kwargs['template'], 'invoice_paid')

    def test_grn_confirmation_notifies_nobody(self):
        """Recorded as current behaviour, not endorsed: a delivery arriving damaged
        tells no one. If 0.2's neighbourhood ever adds a send here, this fails and the
        addition gets a decision rather than a surprise."""
        challan = self._make_dc(self.project_a, 'DC-SILENT')
        item = challan.line_items.first()
        with patch('projects.views.send_notification') as sender:
            _client_for(self.se_a).post(
                reverse('confirm_grn',
                        args=[self.project_a.project_id, challan.pk]),
                {f'received_qty_{item.pk}': '4', f'damaged_qty_{item.pk}': '4'},
            )
        sender.assert_not_called()
        challan.refresh_from_db()
        # Shortfall AND damage — two stacked problems, so 'red' → Rejected.
        self.assertEqual(challan.status, DeliveryChallan.REJECTED)

    def test_activation_notifies_nobody(self):
        """assign_tasks_to() is silent by design (utils.py:181): twenty back-assigned
        tasks send nothing."""
        draft = Project.objects.create(
            customer_name='Foxtrot Residence', customer_phone='9876543210',
            site_address='6 Sun Road', city='Lucknow', project_type='Residential',
            capacity_kw=Decimal('3.00'), status='Draft', assigned_pm=self.pm_a,
        )
        with patch('projects.views.send_notification') as sender:
            _client_for(self.pm_a).post(
                reverse('project_activate', args=[draft.project_id]),
                {'assigned_design_id': self.design_a.pk},
            )
        draft.refresh_from_db()
        self.assertEqual(draft.status, 'Active')
        sender.assert_not_called()
