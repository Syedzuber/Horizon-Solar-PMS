"""Prompt 1.3c — the OPEX opening transition, and the proof that 1.3b is no longer inert.

WHAT THIS SUITE IS FOR. Three prompts built the OPEX execution start in pieces: 1.3a
seeded the template and added `Task.is_mirror`, 1.3b excluded mirrors from a dozen
counters, and neither could be exercised end to end because `attach_residential_template()`
never copied `is_mirror` onto a `Task` row (B19) and nothing attached the OPEX template
at all. Every test in `tests_mirror_metrics.py` therefore sets `is_mirror` BY HAND and
says so in its docstring — those tests prove the counters and cannot prove the pipeline
that feeds them.

`MirrorsSurviveTheAttachTests` and `CountersOnARealSiteTests` below are the ones that
close that gap: they take a site through the REAL activation view and assert on the rows
it actually created. If the seventh snapshot is ever dropped from the `bulk_create` again,
those two classes fail and the rest of the suite does not.

HOW THE FIXTURE GETS ITS DATA. `solarpms.test_settings` disables migrations and builds
the schema straight from model state, so migration 0075 NEVER RUNS under test and no
OPEX template exists in a test database. `attach_opex_template()` deliberately has NO
virgin-database bootstrap — unlike `resolve_residential_template()` — because there is
no runtime OPEX builder to bootstrap from and inventing a template to paper over a
missing one is exactly the failure mode 0.4 designed against. So this module seeds by
calling migration 0075's own `seed_opex_v1()`, which is the pattern
`tests_opex_template.py` established and documents at length.

WHAT THIS SUITE DOES NOT TEST, because 1.3c does not build it: the human-write refusal on
a mirror. That belongs in `_apply_task_status_change()` (R-18) and is still not
implemented — see `EXECUTION_MODULE_DEFERRED.md` §B. A test here asserting it would be
asserting a thing that does not exist, and would in any case pass for the wrong reason:
both status views refuse an UNASSIGNED task before that helper runs, and every mirror
this module creates is unassigned.
"""
from datetime import date, timedelta
from decimal import Decimal
from importlib import import_module

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse

from .models import (
    ActivityLog, PaymentMilestone, Project, ProjectPhase, StatusTransition, Task,
    TaskTemplate, TaskTemplatePhase, TaskTemplateTask, UserProfile,
    REASON_EXECUTION_STARTED,
)
from .reports import build_user_status_rows
from .utils import resolve_residential_template

RESIDENTIAL_FINANCE_EMAIL = 'santosh@horizonrenewablepower.com'

# The eight mirrors, BY NAME. A count of 8 passes perfectly well when the wrong eight
# rows are flagged, which is why every assertion below compares this set and not
# `.count()`. A third independent transcription of docs/OPEX_task_template_spec.md
# v1.5 §3 — migration 0075 holds the first, tests_opex_template.py the second.
EXPECTED_MIRROR_NAMES = {
    'Design',
    # Material Delivery split four ways in spec v1.4; the two SCM inspections that
    # stood beside it were removed in the same revision (phase 4.5 owns them). SCM
    # therefore owns FOUR MIRRORS AND NO ENTERED TASK on an OPEX site.
    'Delivery — Solar Panels',
    'Delivery — Inverters',
    'Delivery — BOS Kit',
    'Delivery — MMS',
    'COD',
    'As-Built Drawings',
    'HOTO',
}

# The three PM-role tasks that are NOT mirrors, and so are the only ones the OPEX attach
# hands to the site's PM.
EXPECTED_PM_ASSIGNED_NAMES = {
    'Net Metering Approval',
    'CEIG Approval',
    'Post-Installation Approvals',
}


# ---------------------------------------------------------------------------
# Fixture plumbing
# ---------------------------------------------------------------------------

class _ConcreteApps:
    """Stands in for the `apps` registry a RunPython function is handed.

    Copied in shape from tests_opex_template.py for the reason given there: the seed
    only ever calls apps.get_model(), the concrete classes answer every query the
    historical ones do, and passing the concrete classes is what makes the R-7 draft
    guard real (the historical classes carry no save() override).
    """

    _MODELS = {
        'TaskTemplate':      TaskTemplate,
        'TaskTemplatePhase': TaskTemplatePhase,
        'TaskTemplateTask':  TaskTemplateTask,
        'Task':              Task,
    }

    def get_model(self, app_label, model_name):
        assert app_label == 'projects'
        return self._MODELS[model_name]


def _seed_opex():
    module = import_module('projects.migrations.0075_seed_opex_template_v1')
    module.seed_opex_v1(_ConcreteApps(), None)


def _profile(username, role, email=''):
    """Create a user and give their profile `role`.

    The profile itself already exists by the time create_user() returns — signals.py
    creates one on post_save with the model's DEFAULT role. So this UPDATES rather than
    creates; a get_or_create(defaults={'role': role}) here silently returns the signal's
    profile with the wrong role, and every role_required gate then 403s.
    """
    user = User.objects.create_user(
        username=username, password='x', email=email,
        first_name=username.title(), last_name='Test',
    )
    profile, _ = UserProfile.objects.get_or_create(user=user)
    profile.role = role
    profile.save(update_fields=['role'])
    return profile


def _client_for(profile):
    client = Client()
    client.force_login(profile.user)
    return client


class OpexActivationBase(TestCase):
    """One PM, one OPEX Draft site, both templates seeded the way production got them."""

    @classmethod
    def setUpTestData(cls):
        resolve_residential_template()   # bootstraps RESIDENTIAL v1 on a virgin DB
        _seed_opex()

        cls.pm       = _profile('opexpm',    'PM')
        cls.other_pm = _profile('otherpm',   'PM')
        cls.designer = _profile('opexdes',   'Design')
        # Required data for the RESIDENTIAL path only — attach_residential_template()
        # raises and rolls the whole activation back without it.
        cls.finance  = _profile('opexfin',   'Finance', email=RESIDENTIAL_FINANCE_EMAIL)

    def setUp(self):
        self.site = self._make_opex_site()

    def _make_opex_site(self, customer_name='Tender Site A', pm=None):
        return Project.objects.create(
            customer_name=customer_name,
            customer_phone='9876543210',
            site_address='1 Tender Road',
            city='Lucknow',
            project_type='OPEX',
            capacity_kw=Decimal('100.00'),
            status='Draft',
            assigned_pm=pm or self.pm,
        )

    def _activate(self, project=None, actor=None):
        project = project or self.site
        response = _client_for(actor or self.pm).post(
            reverse('opex_site_activate', args=[project.project_id]))
        project.refresh_from_db()
        return response

    def _make_residential(self, customer_name='House A'):
        return Project.objects.create(
            customer_name=customer_name,
            customer_phone='9876543210',
            site_address='1 Sun Road',
            city='Lucknow',
            project_type='Residential',
            capacity_kw=Decimal('5.00'),
            status='Draft',
            assigned_pm=self.pm,
            target_commissioning_date=date.today() + timedelta(days=90),
        )

    def _activate_residential(self, project):
        response = _client_for(self.pm).post(
            reverse('project_activate', args=[project.project_id]),
            {'assigned_design_id': self.designer.pk},
        )
        project.refresh_from_db()
        return response

    def _tasks(self, project):
        return Task.objects.filter(phase__project=project)


# ---------------------------------------------------------------------------
# 4.1 — an OPEX site activates
# ---------------------------------------------------------------------------

class OpexActivationTests(OpexActivationBase):

    def test_activation_stamps_status_and_activated_at(self):
        self.assertIsNone(self.site.activated_at)
        response = self._activate()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.site.status, 'Active')
        self.assertIsNotNone(self.site.activated_at)

    def test_activation_creates_seven_phases_in_spec_order(self):
        self._activate()
        self.assertEqual(
            [(p.phase_name, p.phase_order)
             for p in ProjectPhase.objects.filter(project=self.site).order_by('phase_order')],
            [('Design', 1),
             ('Approvals (Pre-Installation)', 2),
             ('Procurement & Delivery', 3),
             ('Installation', 4),
             ('Testing & Commissioning', 5),
             ('Approvals (Post-Installation)', 6),
             ('Closeout', 7)])

    def test_activation_creates_twenty_three_tasks(self):
        self._activate()
        self.assertEqual(self._tasks(self.site).count(), 23)

    def test_no_terminal_status_becomes_reachable(self):
        """1.3c ships the OPENING transition only; closure is phase 5 (D-4)."""
        self._activate()
        self.assertEqual(self.site.status, 'Active')
        self.assertNotIn(
            self.site.status, ['Commissioned', 'On Hold', 'Cancelled'],
            'the opening transition must not reach a terminal status')


# ---------------------------------------------------------------------------
# 4.1 (the mirror half) — THE SEVENTH SNAPSHOT, on a real attached site
# ---------------------------------------------------------------------------

class MirrorsSurviveTheAttachTests(OpexActivationBase):
    """B19's regression net.

    `attach_residential_template()` copied SIX fields from `TaskTemplateTask` and not
    `is_mirror`, so no Task row could carry the flag however its template row was
    flagged, and 1.3b's twelve counter exclusions were correct and completely inert.
    These assertions are on rows the REAL VIEW created — no fixture here sets `is_mirror`
    by hand, which is the whole difference from tests_mirror_metrics.py.
    """

    def test_five_mirrors_are_created_asserted_by_name(self):
        self._activate()
        self.assertEqual(
            {t.task_name for t in self._tasks(self.site).filter(is_mirror=True)},
            EXPECTED_MIRROR_NAMES,
            'the seventh snapshot is missing from the bulk_create again — every '
            'counter exclusion 1.3b shipped is inert without it')

    def test_the_other_fifteen_are_not_mirrors(self):
        """A blanket is_mirror=True would satisfy the assertion above by count."""
        self._activate()
        non_mirrors = self._tasks(self.site).filter(is_mirror=False)
        self.assertEqual(non_mirrors.count(), 15)
        self.assertEqual(
            non_mirrors.filter(task_name__in=EXPECTED_MIRROR_NAMES).count(), 0)

    def test_every_mirror_carries_an_owning_role(self):
        """Never a blank role. A mirror with no role is counted by no department."""
        self._activate()
        for task in self._tasks(self.site).filter(is_mirror=True):
            self.assertTrue(
                task.assigned_role,
                f'mirror {task.task_name!r} has no owning role')

    def test_every_mirror_is_unassigned_and_the_pm_gets_only_real_work(self):
        """The Option A decision, pinned.

        An unassigned mirror is an accurate statement that the row is nobody's task.
        The PM pre-assignment therefore carries `is_mirror=False`, so COD and HOTO —
        both PM-role — stay NULL while the three real PM approvals go to the site's PM.
        """
        self._activate()
        self.assertEqual(
            self._tasks(self.site).filter(is_mirror=True, assigned_to__isnull=False).count(), 0,
            'a mirror was pre-assigned to somebody')
        self.assertEqual(
            {t.task_name for t in self._tasks(self.site).filter(assigned_to=self.pm)},
            EXPECTED_PM_ASSIGNED_NAMES)

    def test_mirrors_carry_template_provenance(self):
        """The flag is a SNAPSHOT, and the FK beside it is provenance only."""
        self._activate()
        for task in self._tasks(self.site).filter(is_mirror=True):
            self.assertIsNotNone(task.template_task)
            self.assertTrue(
                task.template_task.is_mirror,
                f'{task.task_name!r} is flagged but its template row is not')


# ---------------------------------------------------------------------------
# 4.2 — no Residential payment milestones
# ---------------------------------------------------------------------------

class NoResidentialMilestonesTests(OpexActivationBase):

    def test_opex_activation_creates_no_payment_milestones(self):
        """M1/M2/M3 describe a three-milestone residential contract, not a tender.

        project_activate mints them UNCONDITIONALLY for every project type, which is how
        12 of them ended up on non-Residential projects (counted in production 01 Sep
        2026; the 285 this docstring used to name was the A-1.3 audit's projection, not
        a count). This path must add none.
        """
        self._activate()
        self.assertEqual(
            PaymentMilestone.objects.filter(project=self.site).count(), 0)

    def test_no_task_is_flagged_as_a_payment_milestone(self):
        """The OPEX template carries no is_payment_milestone rows either, so the
        task->milestone sync has nothing to fire on."""
        self._activate()
        self.assertEqual(
            self._tasks(self.site).filter(is_payment_milestone=True).count(), 0)

    def test_due_dates_are_left_null(self):
        """B18. All 23 tasks are Internal with duration_days=1, so calculate_due_dates()
        would put HOTO at activated_at + 23 days and the whole tender portfolio overdue
        within a month. Activation deliberately does not call it: a null due date says
        'not scheduled', where 23 sequential days says something specific and false.
        """
        self._activate()
        self.assertEqual(
            self._tasks(self.site).filter(due_date__isnull=False).count(), 0)


# ---------------------------------------------------------------------------
# 4.3 — no designer required
# ---------------------------------------------------------------------------

class NoDesignerRequiredTests(OpexActivationBase):

    def test_activation_succeeds_with_assigned_design_null(self):
        """91 of 96 live tender sites have assigned_design=NULL. Design allocation for
        OPEX lives on DesignAssignment.assigned_to, which project_activate's gate does
        not read — that gate is why those 91 are structurally unactivatable."""
        self.assertIsNone(self.site.assigned_design)
        self._activate()
        self.assertEqual(self.site.status, 'Active')
        self.assertIsNone(
            self.site.assigned_design,
            'activation must not quietly populate a FK the design module does not read')

    def test_no_designer_is_posted_and_none_is_needed(self):
        response = _client_for(self.pm).post(
            reverse('opex_site_activate', args=[self.site.project_id]))
        self.site.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.site.status, 'Active')


# ---------------------------------------------------------------------------
# 4.4 — the ledger row
# ---------------------------------------------------------------------------

class LedgerTests(OpexActivationBase):

    def _transition(self):
        return StatusTransition.objects.get(
            subject_id=self.site.pk, to_status='Active')

    def test_a_transition_row_is_written_with_the_expected_subject_and_reason(self):
        self._activate()
        row = self._transition()
        self.assertEqual(row.subject_type, 'project')
        self.assertEqual(row.from_status, 'Draft')
        self.assertEqual(row.to_status, 'Active')
        self.assertEqual(row.reason_code, REASON_EXECUTION_STARTED)
        self.assertEqual(row.actor, self.pm)
        self.assertEqual(row.actor_role_code, 'PM')

    def test_an_activity_log_row_is_written(self):
        self._activate()
        self.assertTrue(
            ActivityLog.objects.filter(
                project=self.site, action__icontains='Activated project').exists())

    def test_the_ledger_row_rolls_back_with_a_failed_attach(self):
        """R-2: a transition row without its status change is a history of something
        that never happened. Both are in one atomic block, so retiring the template
        must leave neither."""
        TaskTemplate.objects.filter(project_type='OPEX').update(
            status=TaskTemplate.ARCHIVED)
        with self.assertRaises(TaskTemplate.DoesNotExist):
            self._activate()
        self.site.refresh_from_db()
        self.assertEqual(self.site.status, 'Draft')
        self.assertIsNone(self.site.activated_at)
        self.assertEqual(
            StatusTransition.objects.filter(
                subject_id=self.site.pk, to_status='Active').count(), 0)
        self.assertEqual(ProjectPhase.objects.filter(project=self.site).count(), 0)


# ---------------------------------------------------------------------------
# 4.5 — a second activation
# ---------------------------------------------------------------------------

class SecondActivationIsRefusedTests(OpexActivationBase):
    """REFUSED, not idempotent — the choice Task 2 made, pinned here.

    There is no uniqueness constraint on (project, phase_order), so an attach that ran
    twice would silently produce 14 phases and 44 tasks and `_phase_progress_subqueries()`
    would paper over it by taking the lowest-pk phase. The refusal is status-based and
    sits BEFORE the atomic block, so a repeat cannot leave partial state.
    """

    def test_a_second_activation_creates_nothing_further(self):
        self._activate()
        self.assertEqual(ProjectPhase.objects.filter(project=self.site).count(), 7)
        first_activated_at = self.site.activated_at

        response = self._activate()

        self.assertEqual(response.status_code, 302)
        self.assertEqual(ProjectPhase.objects.filter(project=self.site).count(), 7)
        self.assertEqual(self._tasks(self.site).count(), 23)
        self.assertEqual(
            self.site.activated_at, first_activated_at,
            'the second activation re-stamped activated_at')

    def test_a_second_activation_writes_no_second_ledger_row(self):
        self._activate()
        self._activate()
        self.assertEqual(
            StatusTransition.objects.filter(
                subject_id=self.site.pk, to_status='Active').count(), 1)


# ---------------------------------------------------------------------------
# 4.6 — authority
# ---------------------------------------------------------------------------

class AuthorityTests(OpexActivationBase):
    """Authority routes through user_can_manage_project() (R-13). No inline comparison."""

    def test_a_non_owning_pm_cannot_activate(self):
        response = self._activate(actor=self.other_pm)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.site.status, 'Draft')
        self.assertEqual(ProjectPhase.objects.filter(project=self.site).count(), 0)

    def test_a_project_coordinator_on_the_site_can_activate(self):
        """Coordinator authority is ADDITIVE and lives in user_can_manage_project()."""
        coordinator = _profile('opexcoord', 'Project Coordinator')
        self.site.coordinators.add(coordinator)
        self._activate(actor=coordinator)
        self.assertEqual(self.site.status, 'Active')

    def test_a_designer_is_refused_by_the_role_gate(self):
        response = self._activate(actor=self.designer)
        self.assertEqual(self.site.status, 'Draft')
        self.assertNotEqual(response.status_code, 302)

    def test_get_does_not_activate(self):
        response = _client_for(self.pm).get(
            reverse('opex_site_activate', args=[self.site.project_id]))
        self.site.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.site.status, 'Draft')

    def test_a_soft_deleted_site_cannot_be_activated(self):
        self.site.is_deleted = True
        self.site.save(update_fields=['is_deleted'])
        response = self._activate()
        self.assertEqual(response.status_code, 404)

    def test_a_residential_project_is_refused_by_this_view(self):
        """This view owns the non-Residential transition and nothing else. Activating a
        Residential project here would skip its milestones and its Finance assignee."""
        house = self._make_residential()
        response = _client_for(self.pm).post(
            reverse('opex_site_activate', args=[house.project_id]))
        house.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(house.status, 'Draft')
        self.assertEqual(ProjectPhase.objects.filter(project=house).count(), 0)


# ---------------------------------------------------------------------------
# 4.7 — Residential is unchanged
# ---------------------------------------------------------------------------

class ResidentialIsUnchangedTests(OpexActivationBase):
    """The attach was EXTRACTED, not rewritten. `attach_residential_template()` keeps its
    name, its signature and its behaviour; what moved into `_attach_task_template()` is
    the phase loop and the four integrity assertions, which both types now share.

    The 92 characterisation tests in tests_residential_baseline.py are the real net.
    This class is the same claim stated where a reader of 1.3c will find it.
    """

    def setUp(self):
        super().setUp()
        self.house = self._make_residential()
        self._activate_residential(self.house)

    def test_residential_still_activates_with_its_full_shape(self):
        self.assertEqual(self.house.status, 'Active')
        self.assertIsNotNone(self.house.activated_at)
        self.assertEqual(ProjectPhase.objects.filter(project=self.house).count(), 9)
        self.assertEqual(self._tasks(self.house).count(), 52)

    def test_residential_still_creates_its_three_milestones(self):
        self.assertEqual(
            sorted(PaymentMilestone.objects.filter(
                project=self.house).values_list('milestone_name', flat=True)),
            ['M1', 'M2', 'M3'])

    def test_residential_has_no_mirrors(self):
        """The seventh snapshot copies whatever the template says, and RESIDENTIAL v1
        flags nothing. Adding the line must not invent mirrors on 26 live projects."""
        self.assertEqual(self._tasks(self.house).filter(is_mirror=True).count(), 0)

    def test_residential_still_assigns_its_pm_and_finance_tasks(self):
        self.assertTrue(self._tasks(self.house).filter(assigned_to=self.pm).exists())
        self.assertTrue(self._tasks(self.house).filter(assigned_to=self.finance).exists())

    def test_residential_still_requires_a_designer(self):
        """The gate that strands 91 OPEX sites is correct for Residential and stays."""
        second = self._make_residential('House B')
        response = _client_for(self.pm).post(
            reverse('project_activate', args=[second.project_id]), {})
        second.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(second.status, 'Draft')
        self.assertEqual(ProjectPhase.objects.filter(project=second).count(), 0)

    def test_residential_ledger_row_still_carries_no_reason_code(self):
        """REASON_EXECUTION_STARTED was NOT retrofitted onto the Residential call. A new
        value in a column that path has never written is a behaviour change, however
        small, and this path is pinned."""
        row = StatusTransition.objects.get(
            subject_id=self.house.pk, to_status='Active')
        self.assertEqual(row.reason_code, '')

    def test_the_two_paths_share_one_attach_implementation(self):
        """Not two copies. Both project types go through _attach_task_template(), which
        is why the seventh snapshot cannot be present in one and missing from the other.
        """
        from . import utils
        self.assertTrue(hasattr(utils, '_attach_task_template'))
        self.assertIn('_attach_task_template', utils.attach_opex_template.__code__.co_names)
        self.assertIn('_attach_task_template',
                      utils.attach_residential_template.__code__.co_names)


# ---------------------------------------------------------------------------
# 4.8 — THE COUNTERS, ON A GENUINELY ATTACHED SITE
# ---------------------------------------------------------------------------

class CountersOnARealSiteTests(OpexActivationBase):
    """The test that proves 1.3b is no longer inert.

    Every assertion here is on a site activated through the real view, whose five mirror
    rows exist because the pipeline created them. Before the seventh snapshot was copied
    these tests would all have passed VACUOUSLY — there would have been no mirror to
    exclude. Now they fail if either half regresses: drop the snapshot and the mirror
    count assertion goes; drop an exclusion and the counter assertion goes.
    """

    def setUp(self):
        super().setUp()
        self._activate()
        # The guard that stops every assertion below from passing vacuously.
        self.assertEqual(
            {t.task_name for t in self._tasks(self.site).filter(is_mirror=True)},
            EXPECTED_MIRROR_NAMES,
            'no mirrors on the site — every counter assertion below would pass for '
            'the wrong reason')

    # -- counter 1: dashboard_pm.pending_approvals --------------------------------
    #
    # The strongest of the three. It matches on assigned_role with NO assignment term,
    # so the site's two PM-role mirrors (COD, HOTO) land on this card whether or not
    # anybody is assigned to them — which is exactly the case here, since Option A
    # leaves every mirror unassigned.

    def test_pending_approvals_excludes_the_two_pm_mirrors(self):
        response = _client_for(self.pm).get(reverse('dashboard_pm'))
        self.assertEqual(response.status_code, 200)

        pm_role_not_started = self._tasks(self.site).filter(
            assigned_role=Task.PM, status='Not Started')
        self.assertEqual(pm_role_not_started.count(), 5,
                         'expected 3 real PM tasks + COD + HOTO')

        self.assertEqual(
            response.context['summary']['pending_approvals'], 3,
            'COD and HOTO are being counted as the PM\'s pending approvals')

    # -- counter 2: the CEO aggregate's dept_pm_pending ---------------------------

    def test_ceo_dept_pm_pending_excludes_the_two_pm_mirrors(self):
        from .views import _get_ceo_dashboard_context
        context = _get_ceo_dashboard_context()
        pm_row = next(r for r in context['dept_rows'] if r['label'] == 'PM')
        self.assertEqual(
            pm_row['pending'], 3,
            'the CEO department rollup is counting COD and HOTO as PM work')

    def test_ceo_dept_design_and_scm_pending_exclude_their_mirrors(self):
        """Both read 0, and SCM's 0 IS A REAL FINDING, not a weaker test.

        SCM used to carry one mirror and two entered inspections, so this asserted 2
        and the exclusion was what made it 2 rather than 3. Spec v1.4 removed both
        inspections — a vendor inspection covers a consignment, not a site — and split
        Material Delivery into four mirrors. SCM therefore now owns FOUR MIRRORS AND
        NOTHING ELSE on an OPEX site, exactly as Design already did.

        SO THE ASSERTION IS WEAKER THAN IT WAS, and honestly so: 0 is what both the
        correct exclusion and a deleted exclusion would produce IF the mirrors were
        unassigned, and it is only the exclusion that produces it once they are
        assigned — which `_assign_every_mirror()` in this class does. Read it as
        pinning the consequence of the template change, not as proof of R-20; the
        project-card test above is the load-bearing proof.

        WHAT IT MEANS FOR A REAL USER: no SCM or Design person has a single actionable
        OPEX task, and none of their six mirrors can move until B-18 and SCM's
        catalogue mapping land. Recorded in EXECUTION_MODULE_DEFERRED.md §B27.
        """
        from .views import _get_ceo_dashboard_context
        context = _get_ceo_dashboard_context()
        rows = {r['label']: r for r in context['dept_rows']}
        self.assertEqual(rows['Design']['pending'], 0)
        self.assertEqual(
            rows['SCM']['pending'], 0,
            'SCM owns only the four delivery mirrors since spec v1.4 removed the two '
            'inspections; anything above 0 means a mirror is being counted as work.')

    # -- counter 3: build_user_status_rows ----------------------------------------
    #
    # STATED PLAINLY: this is the WEAKEST of the four assertions in this class. The
    # report filters `assigned_to__isnull=False` before the mirror exclusion is reached,
    # so with Option A's unassigned mirrors it would exclude them even if
    # human_owned_tasks_q() were deleted. It is asserted because the outcome matters and
    # a future decision to assign mirrors would make it load-bearing — not because it
    # proves the exclusion today. The card counts below are the third REAL proof.

    def test_build_user_status_rows_shows_the_pm_only_real_work(self):
        report = build_user_status_rows(date.today())
        row = next(r for r in report['rows'] if r['profile'] == self.pm)
        self.assertEqual(
            row['tasks_assigned'], 3,
            'the per-user report is attributing mirror rows to the PM')

    # -- counter 4: the project card counts, which are the OTHER half of R-20 -----

    def test_the_project_card_counts_every_task_including_mirrors(self):
        """R-20's PROGRESS half. This card asks "how much of this SITE is done", and a
        site is not finished because the humans finished — four undelivered
        consignments are outstanding work on the site whoever records them. So all 23
        count, mirrors included, through `site_progress_tasks_q()`.

        CHANGED BY PROMPT 1.6, 1 Sep 2026. From 1.3b until then this method was
        `test_the_project_card_counts_exclude_all_eight_mirrors` and asserted **15**,
        because R-20 was a single rule — "a task metric excludes mirrors" — and this
        card was its strongest available proof: the counts are project-scoped with no
        assignment term, so all eight mirrors would land in them and only the helper
        kept them out.

        THE CLASS DOES NOT LOSE THAT PROOF. Counters 1 and 2 above — the CEO
        `dept_rows` for Design and SCM — are WORKLOAD numbers, still excluded, and
        still demonstrate exactly what this method used to. What changed is that this
        particular card turned out to be asking the other question, and 1.6 split the
        rule rather than leaving `dashboard_pm` measuring a site out of 15 while
        `project_overview`'s phase bars measured the same site out of 23.
        """
        response = _client_for(self.pm).get(reverse('dashboard_pm'))
        card = next(c for c in response.context['projects_with_progress']
                    if c['project'].pk == self.site.pk)
        self.assertEqual(self._tasks(self.site).count(), 23)
        self.assertEqual(
            card['total_tasks'], 23,
            'the project card is measuring this site out of its ENTERED tasks. It is '
            'a completeness figure, not a workload one — route it through '
            'utils.site_progress_tasks_q().')

    # -- and the negative: the mirrors are still VISIBLE ---------------------------

    def test_the_exclusion_is_a_metric_rule_and_not_a_visibility_rule(self):
        """R-20's own caveat. A mirror is dropped from COUNTS; it must still appear on
        the site's own task list, or the PM cannot see that the work exists at all."""
        response = _client_for(self.pm).get(
            reverse('project_overview', args=[self.site.project_id]))
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        for name in EXPECTED_MIRROR_NAMES:
            self.assertIn(name, body, f'mirror {name!r} is hidden from the task list')
