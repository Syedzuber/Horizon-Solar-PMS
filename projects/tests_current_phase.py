"""
"Which phase is this site on" has ONE answer. Prompt B21.

WHY THIS FILE EXISTS
--------------------
"First phase holding a not-Done task" was written FOUR times — `Project.
get_current_phase()` in models.py, `dashboard_pm`, `dashboard_site_engineer`, and an
inline Python loop in `dashboard_bd`. Prompt 1.3b examined all four and deliberately
changed none: three-of-four fixed would have been worse than none, because the same
site would then report different phases on different screens.

Two things made it urgent rather than untidy.

**The OPEX template's Phase 1 is `Design`, its only task is a MIRROR, and no
derivation hook exists to ever write it Done.** COD, HOTO and As-Built have no source
object in existence either. So every OPEX site displayed its current phase as
"Design" — permanently, on all four screens, with all nine installation tasks
complete.

**And the four already disagreed, before mirrors were involved.** On a project with
every task Done, models.py and dashboard_bd returned `None` while the PM and SE
dashboards returned the LAST phase: a finished Residential project read
"Finance Closure" on the PM dashboard and "—" on the Admin project list, at the same
moment, from the same data.

WHAT IS PINNED HERE
-------------------
`AgreementTests` is the invariant this session exists to create: **all four call
sites, on the same project, in five different states, return the same phase name.**
It is parameterised over the call sites rather than written out four times, so a
fifth call site added later is one line to cover — and a copy of the rule
reintroduced anywhere is a named failure rather than a support ticket.

The other classes pin what that one answer IS: Residential unchanged
(`ResidentialUnchangedTests`), OPEX no longer stuck on Design
(`OpexIsNotStuckOnDesignTests`), phases advancing while mirrors stay open
(`AdvanceTests`), the settled empty case (`EmptyCaseTests`), and the prefetch that
keeps it free in a loop (`QueryCostTests`).

FIXTURES ARE REAL ATTACHED PROJECTS, never hand-made Phase/Task rows. The OPEX site
is activated over HTTP through `opex_site_activate`; the Residential project through
`project_activate`. A hand-made fixture would prove the helper works and nothing
about whether the templates production actually ships have the shape the helper
assumes — which is the entire subject of test 3 below.

Run with:
    python manage.py test projects --settings=solarpms.test_settings
"""
from datetime import date, timedelta
from decimal import Decimal
from importlib import import_module

from django.contrib.auth.models import User
from django.db.models import Prefetch
from django.test import Client, TestCase
from django.urls import reverse

from .models import (
    Project, ProjectPhase, Task, TaskTemplate, TaskTemplatePhase,
    TaskTemplateTask, UserProfile,
)
from .utils import (
    RESIDENTIAL_FINANCE_ASSIGNEE_EMAIL, assign_tasks_to, current_phase,
    resolve_residential_template,
)


# Phase names transcribed from the SPECS, not read out of migration 0075 or
# utils.build_residential_phases() — same argument as tests_opex_template.py: a test
# that imports the seed's own data agrees with any typo the seed contains.
OPEX_DESIGN            = 'Design'
OPEX_PRE_APPROVALS     = 'Approvals (Pre-Installation)'
OPEX_PROCUREMENT       = 'Procurement & Delivery'
OPEX_INSTALLATION      = 'Installation'
OPEX_COMMISSIONING     = 'Testing & Commissioning'
OPEX_POST_APPROVALS    = 'Approvals (Post-Installation)'
OPEX_CLOSEOUT          = 'Closeout'

RESI_SALES             = 'Sales & Documentation'
RESI_DEV               = 'Detail Engineering Visit'
RESI_FINANCE_CLOSURE   = 'Finance Closure'

# The one non-mirror task in the OPEX Closeout phase. Deleting it is how
# EmptyCaseTests tells option (c) — "last phase HOLDING a human-owned task" — apart
# from option (b), "last phase", which the PM and SE dashboards used to implement.
OPEX_CLOSEOUT_HUMAN_TASK = 'Completion Certificates (Paperwork)'


class _ConcreteApps:
    """Stands in for the `apps` registry a RunPython function is handed.

    Copied in shape from tests_mirror_readonly.py / tests_opex_activation.py, for the
    reason given there: the seed only ever calls apps.get_model(), and the concrete
    classes carry the save() overrides that make the R-7 draft guard real.
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

    signals.py creates the profile on post_save with the model's DEFAULT role, so this
    UPDATES rather than creates — a get_or_create here returns the signal's profile
    with the wrong role and every role gate then refuses.
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


# ---------------------------------------------------------------------------
# THE FOUR CALL SITES, as four readers that all return a phase NAME or None.
#
# Normalising to the name is deliberate and is not papering over a difference: the
# PM dashboard genuinely holds a ProjectPhase OBJECT in its context (its template
# does `{{ row.current_phase.phase_name }}`) while the other three hold a string.
# That split is a signature constraint, not a disagreement about which phase — and
# what AgreementTests exists to pin is the phase, so the readers normalise.
# ---------------------------------------------------------------------------

def _read_from_model(test, project):
    """① models.py — Project.get_current_phase(). What the Admin project list prints."""
    return project.get_current_phase()


def _read_from_pm_dashboard(test, project):
    """② views.py — dashboard_pm. Context row key 'current_phase', a ProjectPhase."""
    response = _client_for(test.pm).get(reverse('dashboard_pm'))
    test.assertEqual(response.status_code, 200)
    row = test._row_for(response.context['projects_with_progress'], project)
    phase = row['current_phase']
    return phase.phase_name if phase else None


def _read_from_se_dashboard(test, project):
    """③ views.py — dashboard_site_engineer. Context row key 'phase', a name."""
    response = _client_for(test.se).get(reverse('dashboard_site_engineer'))
    test.assertEqual(response.status_code, 200)
    return test._row_for(response.context['projects'], project)['phase']


def _read_from_bd_dashboard(test, project):
    """④ views.py — dashboard_bd. Context row key 'phase', a name.

    The inline Python loop that used to compute this — the fourth copy of the rule —
    was REMOVED by B21 rather than kept as a fast path.
    """
    response = _client_for(test.bd).get(reverse('dashboard_bd'))
    test.assertEqual(response.status_code, 200)
    return test._row_for(response.context['project_rows'], project)['phase']


# Every call site that answers "which phase is this site on". A new one goes here, and
# AgreementTests covers it without any other change.
CALL_SITES = [
    ('models.Project.get_current_phase', _read_from_model),
    ('views.dashboard_pm',               _read_from_pm_dashboard),
    ('views.dashboard_site_engineer',    _read_from_se_dashboard),
    ('views.dashboard_bd',               _read_from_bd_dashboard),
]


class CurrentPhaseFixture(TestCase):
    """One really-activated OPEX site and one really-activated Residential project.

    Both carry an SE-assigned task, because the Site Engineer dashboard selects
    projects by `phases__tasks__assigned_to` — without one, the third call site would
    silently have nothing to read and every agreement test would compare three
    readers while claiming to compare four.
    """

    @classmethod
    def setUpTestData(cls):
        resolve_residential_template()   # bootstraps RESIDENTIAL v1 on a virgin DB
        _seed_opex()

        cls.pm      = _profile('b21_pm', 'PM')
        cls.se      = _profile('b21_se', 'Site Engineer')
        cls.bd      = _profile('b21_bd', 'BD')
        cls.design  = _profile('b21_design', 'Design')
        # Required data for the Residential path — attach_residential_template()
        # raises and rolls activation back if this account is absent.
        cls.finance = _profile('b21_fin', 'Finance',
                               email=RESIDENTIAL_FINANCE_ASSIGNEE_EMAIL)

    def setUp(self):
        self.opex = self._activate_opex('B21 Tender Site')
        self.resi = self._activate_residential('B21 Residence')

    # -- fixture helpers -----------------------------------------------------

    def _activate_opex(self, customer_name):
        """A Draft OPEX site activated THROUGH THE REAL VIEW, then given the SE their
        Installation tasks — the attach leaves every SE-role task unassigned."""
        site = Project.objects.create(
            customer_name=customer_name,
            customer_phone='9876543210',
            site_address='1 Tender Road',
            city='Lucknow',
            project_type='OPEX',
            capacity_kw=Decimal('100.00'),
            status='Draft',
            assigned_pm=self.pm,
        )
        response = _client_for(self.pm).post(
            reverse('opex_site_activate', args=[site.project_id]))
        self.assertEqual(response.status_code, 302, 'OPEX activation did not redirect')
        site.refresh_from_db()
        self.assertEqual(site.status, 'Active')

        assign_tasks_to(
            Task.objects.filter(phase__project=site,
                                assigned_role=Task.SITE_ENGINEER),
            self.se,
        )
        return site

    def _activate_residential(self, customer_name):
        project = Project.objects.create(
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
        response = _client_for(self.pm).post(
            reverse('project_activate', args=[project.project_id]),
            {'assigned_design_id': self.design.pk},
        )
        self.assertEqual(response.status_code, 302, 'activation did not redirect')
        project.refresh_from_db()
        self.assertEqual(
            project.status, 'Active',
            'Residential did not activate — is the Finance assignee present?')

        assign_tasks_to(
            Task.objects.filter(phase__project=project,
                                assigned_role=Task.SITE_ENGINEER),
            self.se,
        )
        return project

    # -- reading helpers -----------------------------------------------------

    @staticmethod
    def _row_pk(row):
        """The three dashboards do not agree on how a row identifies its project.

        PM and BD rows carry the fetched `Project` under 'project' (the shape
        `_apply_project_sections` requires); the SE dashboard carries a bare 'pk' and
        no object. Not this session's to unify — recorded in EXECUTION_MODULE_DEFERRED.
        """
        return row['project'].pk if 'project' in row else row['pk']

    def _row_for(self, rows, project):
        """The dashboard context row for `project`, or a named failure.

        A missing row is a fixture problem — the dashboard's own selection dropped the
        project — and must not be reported as a phase mismatch.
        """
        for row in rows:
            if self._row_pk(row) == project.pk:
                return row
        self.fail(
            f'{project.project_id} is not on this dashboard at all — the fixture no '
            f'longer satisfies its project-selection rule, so nothing was compared.'
        )

    def _phase_names(self, project):
        return list(project.phases.order_by('phase_order')
                    .values_list('phase_name', flat=True))

    def _tasks_in(self, project, phase_name, human_only=True):
        qs = Task.objects.filter(phase__project=project,
                                 phase__phase_name=phase_name)
        return qs.filter(is_mirror=False) if human_only else qs

    def _complete(self, queryset):
        """Mark tasks Done with a direct UPDATE, deliberately not through the status path.

        `_apply_task_status_change()` REFUSES a mirror outright since B22 (R-18), and
        several tests here need a project state in which mirrors stay Not Started
        while human tasks are Done — which is precisely the state the read path under
        test has to handle. The subject of this module is the READ path; the write
        path has its own module (tests_mirror_readonly.py).
        """
        return queryset.update(status=Task.DONE)

    def _complete_all_human(self, project):
        return self._complete(
            Task.objects.filter(phase__project=project, is_mirror=False))


# ---------------------------------------------------------------------------
# 1 — the invariant this session exists to create
# ---------------------------------------------------------------------------

class AgreementTests(CurrentPhaseFixture):
    """All four call sites return the same phase, in every state that matters.

    Parameterised over CALL_SITES rather than written out four times: a fifth call
    site is one line, and a reintroduced copy of the rule fails here by name.
    """

    def _assert_all_agree(self, project, expected, note=''):
        answers = {}
        for label, reader in CALL_SITES:
            with self.subTest(call_site=label, state=note):
                answers[label] = reader(self, project)
                self.assertEqual(
                    answers[label], expected,
                    f'{label} says {answers[label]!r} for {project.project_id} '
                    f'({note}); every call site must say {expected!r}.'
                )
        self.assertEqual(
            len(set(answers.values())), 1,
            f'the four call sites disagree about {project.project_id} ({note}): '
            f'{answers!r}. This is exactly the defect B21 closed.'
        )

    def test_01_opex_fresh(self):
        self._assert_all_agree(self.opex, OPEX_PRE_APPROVALS, 'nothing done')

    def test_02_opex_part_way(self):
        self._complete(self._tasks_in(self.opex, OPEX_PRE_APPROVALS))
        self._assert_all_agree(self.opex, OPEX_PROCUREMENT, 'phase 2 done')

    def test_03_opex_everything_human_done(self):
        self._complete_all_human(self.opex)
        self._assert_all_agree(self.opex, OPEX_CLOSEOUT, 'all human work done')

    def test_04_residential_fresh(self):
        self._assert_all_agree(self.resi, RESI_SALES, 'nothing done')

    def test_05_residential_everything_done(self):
        self._complete(Task.objects.filter(phase__project=self.resi))
        self._assert_all_agree(self.resi, RESI_FINANCE_CLOSURE, 'all work done')

    def test_06_a_blocked_task_still_makes_its_phase_current(self):
        """Blocked is not Done. Pinned because the two old query copies spelled the
        predicate `status__in=[Not Started, In Progress, Blocked]` while the two loop
        copies spelled it `!= Done` — extensionally identical only because
        STATUS_CHOICES has exactly four values, which a fifth status would break."""
        phase_two = self._tasks_in(self.opex, OPEX_PRE_APPROVALS)
        self._complete(phase_two)
        stuck = phase_two.first()
        Task.objects.filter(pk=stuck.pk).update(status=Task.BLOCKED)
        self._assert_all_agree(self.opex, OPEX_PRE_APPROVALS, 'one task Blocked')


# ---------------------------------------------------------------------------
# 2 — Residential is unchanged
# ---------------------------------------------------------------------------

class ResidentialUnchangedTests(CurrentPhaseFixture):
    """The consolidation must not move a single Residential project.

    It cannot, and this class says why in an assertion rather than in a comment: the
    Residential template contains NO mirror at all, so the exclusion is a no-op on
    every one of the ~95 live projects. Only the empty case could have moved, and it
    was settled to keep the PM/SE answer.
    """

    def test_01_the_residential_template_contains_no_mirror(self):
        self.assertEqual(
            Task.objects.filter(phase__project=self.resi, is_mirror=True).count(), 0,
            'a mirror has appeared in the Residential template — the claim that R-21 '
            'cannot move a Residential project no longer holds, and every figure in '
            'this class needs re-deriving.'
        )

    def test_02_fresh_project_is_on_the_first_phase(self):
        self.assertEqual(current_phase(self.resi).phase_name, RESI_SALES)
        self.assertEqual(self.resi.get_current_phase(), RESI_SALES)

    def test_03_phase_advances_exactly_as_before(self):
        self._complete(self._tasks_in(self.resi, RESI_SALES))
        self.assertEqual(self.resi.get_current_phase(), RESI_DEV)

    def test_04_completed_project_reports_the_last_phase(self):
        """What the PM and SE dashboards showed before B21, kept. models.py and
        dashboard_bd used to show None here; that was the pre-existing disagreement,
        and it was settled in favour of the two most-used screens."""
        self._complete(Task.objects.filter(phase__project=self.resi))
        self.assertEqual(self.resi.get_current_phase(), RESI_FINANCE_CLOSURE)


# ---------------------------------------------------------------------------
# 3 — the defect B21 was urgent for
# ---------------------------------------------------------------------------

class OpexIsNotStuckOnDesignTests(CurrentPhaseFixture):
    """A fresh OPEX site is NOT on Design, and can never be.

    OPEX Phase 1 holds exactly one task, `Design`, and it is a mirror whose source
    object — a DesignAssignment reaching DESIGN_RELEASED — has no derivation hook
    writing it back yet. Before B21 that pinned every OPEX site's displayed phase at
    "Design" forever, on all four screens, with all nine installation tasks complete.
    """

    def test_01_phase_one_is_design_and_holds_only_a_mirror(self):
        """The fixture assertion the rest of the class depends on. If the OPEX
        template changes shape, this fails first and by name."""
        self.assertEqual(self._phase_names(self.opex)[0], OPEX_DESIGN)
        design_tasks = self._tasks_in(self.opex, OPEX_DESIGN, human_only=False)
        self.assertEqual(design_tasks.count(), 1)
        self.assertTrue(design_tasks.first().is_mirror)
        self.assertEqual(design_tasks.first().status, Task.NOT_STARTED)

    def test_02_fresh_site_is_on_approvals_not_design(self):
        phase = current_phase(self.opex)
        self.assertEqual(phase.phase_name, OPEX_PRE_APPROVALS)
        self.assertNotEqual(phase.phase_name, OPEX_DESIGN)

    def test_03_design_is_never_current_at_any_point_in_the_site_s_life(self):
        """Walks the whole site forward one phase at a time. Design holds no
        human-owned task at all, so it cannot become current at any state."""
        seen = [current_phase(self.opex).phase_name]
        for phase_name in self._phase_names(self.opex):
            self._complete(self._tasks_in(self.opex, phase_name))
            phase = current_phase(self.opex)
            seen.append(phase.phase_name if phase else None)
        self.assertNotIn(OPEX_DESIGN, seen,
                         f'Design became current at some point: {seen!r}')

    def test_04_all_nine_installation_tasks_done_does_not_read_design(self):
        """The sentence from the prompt, as an assertion."""
        installation = self._tasks_in(self.opex, OPEX_INSTALLATION)
        self.assertEqual(installation.count(), 9)
        self._complete(installation)
        self._complete(self._tasks_in(self.opex, OPEX_PRE_APPROVALS))
        self._complete(self._tasks_in(self.opex, OPEX_PROCUREMENT))
        self.assertEqual(current_phase(self.opex).phase_name, OPEX_COMMISSIONING)


# ---------------------------------------------------------------------------
# 4 — advancing past an open mirror
# ---------------------------------------------------------------------------

class AdvanceTests(CurrentPhaseFixture):
    """Completing a phase's human-owned tasks advances the phase even though a mirror
    in it is still Not Started. This is the half of R-21 that is not about Design."""

    def test_01_procurement_advances_with_its_mirror_still_open(self):
        self._complete(self._tasks_in(self.opex, OPEX_PRE_APPROVALS))
        self.assertEqual(current_phase(self.opex).phase_name, OPEX_PROCUREMENT)

        procurement = self._tasks_in(self.opex, OPEX_PROCUREMENT, human_only=False)
        self.assertEqual(procurement.filter(is_mirror=True).count(), 1,
                         'Procurement & Delivery should hold exactly one mirror '
                         '(Material Delivery)')
        self._complete(procurement.filter(is_mirror=False))

        self.assertEqual(current_phase(self.opex).phase_name, OPEX_INSTALLATION)
        self.assertEqual(
            procurement.get(is_mirror=True).status, Task.NOT_STARTED,
            'the mirror must still be Not Started — if the fixture completed it, this '
            'test proved nothing about the exclusion.'
        )

    def test_02_the_full_walk_visits_every_human_phase_in_order(self):
        """Six phases, in phase_order, Design absent. Pins the ordering as well as
        the exclusion: the four copies agreed on phase_order and this keeps them
        agreeing after the consolidation."""
        visited = []
        for _ in range(10):
            phase = current_phase(self.opex)
            if phase is None or phase.phase_name in visited:
                break
            visited.append(phase.phase_name)
            self._complete(self._tasks_in(self.opex, phase.phase_name))
        self.assertEqual(visited, [
            OPEX_PRE_APPROVALS, OPEX_PROCUREMENT, OPEX_INSTALLATION,
            OPEX_COMMISSIONING, OPEX_POST_APPROVALS, OPEX_CLOSEOUT,
        ])


# ---------------------------------------------------------------------------
# 5 — the empty case, decided 31 Aug 2026
# ---------------------------------------------------------------------------

class EmptyCaseTests(CurrentPhaseFixture):
    """When every human-owned task is Done: the LAST PHASE HOLDING A HUMAN-OWNED TASK.

    Option (c) of three that were put up. Option (a) was None — what models.py and
    dashboard_bd did. Option (b) was the last phase, full stop — what the PM and SE
    dashboards did. (b) and (c) give the same answer for both templates shipping
    today, which is why the two most-used screens do not change; test 03 below
    manufactures the case that separates them.
    """

    def test_01_opex_with_all_human_work_done_reads_closeout(self):
        self._complete_all_human(self.opex)
        self.assertEqual(current_phase(self.opex).phase_name, OPEX_CLOSEOUT)
        self.assertEqual(
            Task.objects.filter(phase__project=self.opex, is_mirror=True,
                                status=Task.NOT_STARTED).count(), 5,
            'all five mirrors must still be Not Started, or this is not the empty case'
        )

    def test_02_residential_with_all_work_done_reads_finance_closure(self):
        self._complete(Task.objects.filter(phase__project=self.resi))
        self.assertEqual(current_phase(self.resi).phase_name, RESI_FINANCE_CLOSURE)

    def test_03_it_is_the_last_HUMAN_phase_not_simply_the_last_phase(self):
        """The case that tells option (c) from option (b).

        Delete Closeout's one human-owned task and the phase becomes all-mirror — the
        shape OPEX Phase 1 already has at the front, and the shape a future template
        version could have at the back when COD and HOTO get their derivation hooks
        and Completion Certificates converts. Option (b) would name Closeout, a phase
        in which no human ever had anything to do; option (c) names the last phase
        somebody actually worked.
        """
        Task.objects.filter(phase__project=self.opex,
                            task_name=OPEX_CLOSEOUT_HUMAN_TASK).delete()
        self.assertEqual(
            self._tasks_in(self.opex, OPEX_CLOSEOUT).count(), 0,
            'Closeout should now hold no human-owned task')
        self._complete_all_human(self.opex)
        self.assertEqual(current_phase(self.opex).phase_name, OPEX_POST_APPROVALS)

    def test_04_a_project_whose_every_phase_is_mirrors_has_no_current_phase(self):
        """The one state that still returns None with phases present. Nobody has
        anything to do anywhere, so naming a phase would be an invention."""
        Task.objects.filter(phase__project=self.opex, is_mirror=False).delete()
        self.assertIsNone(current_phase(self.opex))
        self.assertIsNone(self.opex.get_current_phase())


# ---------------------------------------------------------------------------
# 6 — a project with no phases
# ---------------------------------------------------------------------------

class NoPhasesTests(CurrentPhaseFixture):
    """All four copies already agreed on this: None. Kept exactly.

    Only three call sites can be exercised. The Site Engineer dashboard selects
    projects by `phases__tasks__assigned_to`, so a project with no phases holds no
    task to assign and is structurally invisible there — asserted below rather than
    left as a silently-skipped reader.
    """

    def setUp(self):
        super().setUp()
        self.bare = Project.objects.create(
            customer_name='B21 Bare Project',
            customer_phone='9876543210',
            site_address='1 Empty Road',
            city='Lucknow',
            project_type='Residential',
            capacity_kw=Decimal('5.00'),
            status='Active',          # so the PM and BD dashboards list it
            assigned_pm=self.pm,
        )
        self.assertEqual(self.bare.phases.count(), 0)

    def test_01_helper_and_model_method_return_none(self):
        self.assertIsNone(current_phase(self.bare))
        self.assertIsNone(self.bare.get_current_phase())

    def test_02_pm_and_bd_dashboards_show_none(self):
        self.assertIsNone(_read_from_pm_dashboard(self, self.bare))
        self.assertIsNone(_read_from_bd_dashboard(self, self.bare))

    def test_03_the_se_dashboard_cannot_see_a_phaseless_project(self):
        response = _client_for(self.se).get(reverse('dashboard_site_engineer'))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(
            self.bare.pk,
            [self._row_pk(row) for row in response.context['projects']],
            'the SE dashboard selects on phases__tasks__assigned_to; a phaseless '
            'project appearing there means that rule changed and test_02 should now '
            'cover the fourth call site too.'
        )


# ---------------------------------------------------------------------------
# 7 — the cost
# ---------------------------------------------------------------------------

class QueryCostTests(CurrentPhaseFixture):
    """The helper costs ZERO queries per project on a caller that prefetched.

    This is the reason R-21 is a Python loop over `phases.all()` / `tasks.all()`
    rather than the `phases.filter(...)` queryset two of the four copies used: a
    related-manager `.filter()` ignores the prefetch cache, so the queryset form cost
    1–2 queries PER PROJECT at every call site, including the two that already
    prefetched (`admin_project_list`, 50 rows a page, and `dashboard_bd`).

    Asserted at one project and again at three, so a per-project cost cannot hide
    behind a constant.
    """

    DASHBOARD_PREFETCH = Prefetch(
        'phases',
        queryset=ProjectPhase.objects.prefetch_related('tasks').order_by('phase_order'),
    )

    def _prefetched(self, projects):
        """The exact prefetch clause all four call sites now carry."""
        return list(
            Project.objects
            .filter(pk__in=[p.pk for p in projects])
            .prefetch_related(self.DASHBOARD_PREFETCH)
        )

    def test_01_zero_queries_for_one_project(self):
        loaded = self._prefetched([self.opex])
        with self.assertNumQueries(0):
            for project in loaded:
                current_phase(project)

    def test_02_zero_queries_for_three_projects(self):
        extra = self._activate_opex('B21 Second Site')
        loaded = self._prefetched([self.opex, self.resi, extra])
        self.assertEqual(len(loaded), 3)
        with self.assertNumQueries(0):
            for project in loaded:
                current_phase(project)

    def test_03_the_model_method_is_free_on_a_prefetched_caller_too(self):
        """`admin_project_list` renders `project.get_current_phase` from a template
        over a 50-row page; the delegate must not reintroduce a query."""
        loaded = self._prefetched([self.opex, self.resi])
        with self.assertNumQueries(0):
            for project in loaded:
                project.get_current_phase()

    def test_04_the_pm_dashboard_loop_does_not_grow_by_phase_count(self):
        """End to end, on the real view. Two OPEX sites cost the same as one, plus
        that site's own per-project counters — what must NOT appear is a further
        1 + phase_count for the phase lookup."""
        from django.test.utils import CaptureQueriesContext
        from django.db import connection

        client = _client_for(self.pm)
        with CaptureQueriesContext(connection) as one_site:
            client.get(reverse('dashboard_pm'))

        self._activate_opex('B21 Third Site')
        with CaptureQueriesContext(connection) as two_sites:
            client.get(reverse('dashboard_pm'))

        growth = len(two_sites) - len(one_site)
        # The per-project counter queries in the loop are pre-existing and out of
        # scope; the phase lookup used to add 1–2 more per project on top of them and
        # must now add none. 24 is the measured per-project cost of everything else on
        # the card; the ceiling is deliberately not tight, because tightening it would
        # make this test fail for reasons that have nothing to do with R-21.
        self.assertLessEqual(
            growth, 24,
            f'adding one project grew the PM dashboard by {growth} queries; the '
            f'phase lookup is supposed to cost zero of them.'
        )
