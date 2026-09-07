"""Session E verification — the OPEX Design mirror follows its DesignAssignment.

WHY THIS FILE EXISTS
--------------------
`Task.is_mirror` has meant "no human may write this" since prompt B22, and rung 0 in
`_apply_task_status_change()` enforces it. What it has NOT meant, until now, is that
anything writes it instead: every OPEX mirror sat at its seeded `Not Started` for ever,
and the OPEX spec's own comment said so — "until they are wired a mirror sits at its
seeded status, which is known and accepted."

This is the first derivation to be wired. It writes ONE of the eight mirrors, the Design
one, from ONE source, `DesignAssignment.status`.

THE TWO HALVES, AND WHY NEITHER IS SUFFICIENT ALONE
---------------------------------------------------
Audit A-2.3 §3.4 measured the thing that decides the shape of this work: **design runs
entirely before activation and nothing sequences them.** On the development database
(whose counts match the production figures the earlier audits quote):

    OPEX sites: 96 · with design_assignment: 87 · with any phase: 0

So a hook on `apply_design_status()` alone fires for **zero sites**, and the first site
anybody activates mints a Design mirror reading `Not Started` against a source that may
already be at `in_qc` — a mirror disagreeing with its source, which is the single
failure the whole mirror design exists to prevent.

    apply_design_status()   -> sync_design_mirror()   the HOOK      (a status moved)
    attach_opex_template()  -> sync_design_mirror()   the RECONCILE (the rows appeared)

`ReconcileAtActivationTests` is the half that covers every site that exists today.
`TheHookTests` is the half that covers every site after that. Both are pinned here
because either one alone is a feature that is correct for no site in the database.

THE TRAP THIS FILE IS WRITTEN AROUND
------------------------------------
A mirror test built on a hand-made `Task(is_mirror=True)` proves the `if` works and
nothing about whether production carries the flag, or whether the lookup finds the right
one of eight. **Every fixture below is a really activated OPEX site**, built by
`opex_site_activate` driving `attach_opex_template()`, and `_design_mirror()` asserts the
row it returns is a mirror and is not As-Built Drawings — the other `role=Design` mirror,
which does NOT follow this source and which a role-keyed lookup would return half the
time.

Run with:
    python manage.py test projects.tests_design_mirror_derivation --settings=solarpms.test_settings
"""
import inspect
import io
import os
import re
from decimal import Decimal
from importlib import import_module
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext
from django.db import connection
from django.urls import reverse
from django.utils import timezone

from . import design_views
from .design_views import (
    DESIGN_MIRROR_CODE, DESIGN_MIRROR_STATE_MAP, _design_mirror_task,
    apply_design_status, apply_mirror_status, derive_design_mirror_state,
    sync_design_mirror, sync_delivery_mirrors,
)
from .models import (
    ACTOR_ROLE_SYSTEM, DesignAssignment, Issue, Program, Project, StatusTransition,
    Task, TaskTemplate, TaskTemplatePhase, TaskTemplateTask, UserProfile,
    REASON_MIRROR_DERIVED, SUBJECT_TASK,
    DESIGN_ASSIGNMENT_STATUS_CHOICES,
    DESIGN_AWAITING_SURVEY, DESIGN_AWAITING_ALLOCATION, DESIGN_ALLOCATED,
    DESIGN_DUE_DATE_PROPOSED, DESIGN_IN_DESIGN, DESIGN_ARKA_SUBMITTED,
    DESIGN_AWAITING_HEAD_ARKA, DESIGN_ARKA_REJECTED, DESIGN_ARTIFACTS_UPLOADED,
    DESIGN_IN_QC, DESIGN_AWAITING_HEAD_QC, DESIGN_QC_FAILED, DESIGN_RELEASED,
    DESIGN_SURVEY_RETURNED,
)
from .utils import (
    RESIDENTIAL_FINANCE_ASSIGNEE_EMAIL, assign_task_to, attach_residential_template,
    resolve_residential_template,
)


# The OTHER Design-role mirror. Named as a constant because it is the specific wrong
# answer this feature is most likely to give: `assigned_role='Design'` matches this row
# AND the Design mirror, and As-Built is post-commissioning — nothing in the design
# workspace records it, so it must never move when a DesignAssignment does.
AS_BUILT_NAME = 'As-Built Drawings'


class _ConcreteApps:
    """Stands in for the `apps` registry a RunPython function is handed.

    Copied in shape from tests_mirror_readonly.py for the reason given there: the seed
    only ever calls apps.get_model(), and the concrete classes carry the save()
    overrides that make the R-7 draft guard real.
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


def _profile(username, role, email='', **flags):
    """A post_save signal auto-creates the UserProfile; fetch and set, never create."""
    user = User.objects.create_user(username=username, password='x', email=email)
    profile = user.profile
    profile.role = role
    profile.is_active = True
    for name, value in flags.items():
        setattr(profile, name, value)
    profile.save()
    return profile


class MirrorDerivationBase(TestCase):
    """A really activated OPEX site whose design status the test drives.

    `_site()` deliberately takes the design status as an argument and creates the
    DesignAssignment BEFORE activation, because that is the real-world order: design
    work runs first, on a Draft site with no tasks at all.
    """

    @classmethod
    def setUpTestData(cls):
        resolve_residential_template()   # bootstraps RESIDENTIAL v1 on a virgin DB
        _seed_opex()

        cls.pm       = _profile('se_pm',  'PM')
        cls.head     = _profile('se_head', 'Design', is_design_head=True)
        cls.designer = _profile('se_des',  'Design')
        cls.finance  = _profile('se_fin',  'Finance',
                                email=RESIDENTIAL_FINANCE_ASSIGNEE_EMAIL)
        cls.program = Program.objects.create(
            name='Test-SessionE', program_type='OPEX', client_name='SEClient',
            status='Active', short_tender_code='SE')

    # -- fixtures ------------------------------------------------------------

    def _draft_site(self, code, design_status=None, designer=None):
        """A Draft OPEX site, optionally already carrying a DesignAssignment.

        `design_status=None` means NO assignment row at all — the shape of the 9 sites
        that have one but not the other, and the control for the reconcile.
        """
        site = Project.objects.create(
            project_id=code, customer_name='SEClient', customer_phone='9876543210',
            site_address='1 Sun Rd', city='Delhi', project_type='OPEX',
            program=self.program, site_code=code, assigned_pm=self.pm,
            capacity_kw=Decimal('100.00'), status='Draft')
        assignment = None
        if design_status is not None:
            assignment = DesignAssignment.objects.create(
                project=site, status=design_status, assigned_to=designer,
                survey_file_bucket='b', survey_file_path=f'{code}/survey/x.pdf')
        return site, assignment

    def _activate(self, site):
        """Activation through the REAL view, so attach_opex_template() runs for real."""
        client = Client()
        client.force_login(self.pm.user)
        response = client.post(
            reverse('opex_site_activate', args=[site.project_id]))
        self.assertEqual(response.status_code, 302, 'OPEX activation did not redirect')
        site.refresh_from_db()
        self.assertEqual(site.status, 'Active')
        return site

    def _site(self, code, design_status=None, designer=None):
        """The common case: a Draft site with a design assignment, then activated."""
        site, assignment = self._draft_site(code, design_status, designer)
        self._activate(site)
        return site, assignment

    # -- accessors -----------------------------------------------------------

    def _design_mirror(self, site):
        """The Design mirror, with the fixture's own guards against the wrong row.

        Both asserts matter. The first is B22's trap (a row that is not a mirror proves
        nothing about a derivation); the second is A-2.3 §2.3's — `role='Design'`
        matches TWO mirrors and the wrong one is silently plausible.
        """
        task = _design_mirror_task(site)
        self.assertIsNotNone(task, 'the OPEX attach produced no Design mirror')
        self.assertTrue(task.is_mirror, 'the Design mirror is not flagged as one')
        self.assertNotEqual(
            task.task_name, AS_BUILT_NAME,
            'the lookup returned As-Built Drawings — the OTHER role=Design mirror, '
            'which does NOT follow DesignAssignment')
        return task

    def _as_built(self, site):
        task = Task.objects.filter(
            phase__project=site, task_name=AS_BUILT_NAME).first()
        self.assertIsNotNone(task, 'the OPEX attach produced no As-Built mirror')
        self.assertTrue(task.is_mirror)
        return task

    def _ledger(self, task):
        return list(StatusTransition.objects
                    .filter(subject_type=SUBJECT_TASK, subject_id=task.pk)
                    .order_by('pk'))


# ===========================================================================
# 1. The mapping — a pure function, tested as one
# ===========================================================================

class TheMappingTests(TestCase):
    """No database, no fixture. That is the point of a pure function.

    The mapping is a function of the STORED STATUS and not of the transition, and that
    is a hard requirement rather than a style choice: the reconcile at activation has no
    transition to read — it has one stored value and nothing else. A mapping keyed on
    (from, to) would leave the reconcile unable to call it.
    """

    def test_01_every_status_in_the_vocabulary_is_mapped(self):
        """The exhaustiveness check, read from the MODEL rather than transcribed.

        A fifteenth status added to DESIGN_STATUS_CHOICES without a mapping entry fails
        here, at the moment it is added, instead of raising in production on whichever
        site reaches it first.
        """
        vocabulary = {value for value, _label in DESIGN_ASSIGNMENT_STATUS_CHOICES}
        self.assertEqual(
            set(DESIGN_MIRROR_STATE_MAP), vocabulary,
            'DESIGN_MIRROR_STATE_MAP and DesignAssignment.status disagree about which '
            'statuses exist')

    def test_02_the_mapping_is_the_one_that_was_signed_off(self):
        """Transcribed from the prompt's own sign-off, NOT read from the map.

        Two transcriptions on purpose, the same argument tests_opex_template.py makes
        for itself: a test that imports the table it is checking agrees with any typo
        the table contains.
        """
        expected = {
            DESIGN_AWAITING_SURVEY:     Task.NOT_STARTED,
            DESIGN_AWAITING_ALLOCATION: Task.NOT_STARTED,
            DESIGN_ALLOCATED:           Task.IN_PROGRESS,
            DESIGN_DUE_DATE_PROPOSED:   Task.IN_PROGRESS,
            DESIGN_IN_DESIGN:           Task.IN_PROGRESS,
            DESIGN_ARKA_SUBMITTED:      Task.IN_PROGRESS,
            DESIGN_AWAITING_HEAD_ARKA:  Task.IN_PROGRESS,
            DESIGN_ARKA_REJECTED:       Task.IN_PROGRESS,
            DESIGN_ARTIFACTS_UPLOADED:  Task.IN_PROGRESS,
            DESIGN_IN_QC:               Task.IN_PROGRESS,
            DESIGN_AWAITING_HEAD_QC:    Task.IN_PROGRESS,
            DESIGN_QC_FAILED:           Task.IN_PROGRESS,
            DESIGN_SURVEY_RETURNED:     Task.BLOCKED,
            DESIGN_RELEASED:            Task.DONE,
        }
        for status, mirror in expected.items():
            self.assertEqual(derive_design_mirror_state(status), mirror,
                             f'{status!r} maps to the wrong mirror state')

    def test_03_awaiting_allocation_is_not_started(self):
        """The decision, pinned on its own because 82 of 87 sites sit on this row.

        Whatever this maps to is what the Design mirror reads on essentially the entire
        tender, so it is asserted separately rather than being one line of a loop.
        """
        self.assertEqual(derive_design_mirror_state(DESIGN_AWAITING_ALLOCATION),
                         Task.NOT_STARTED)

    def test_04_design_hold_is_blocked(self):
        """The other decision, pinned for the same reason: it is the exception."""
        self.assertEqual(derive_design_mirror_state(DESIGN_SURVEY_RETURNED),
                         Task.BLOCKED)

    def test_05_an_unknown_status_raises_rather_than_defaulting(self):
        """Not error handling — a refusal to guess.

        Not Started is a plausible-looking wrong answer, and defaulting to it would hide
        a missing mapping for months on whichever sites carried the new value.
        """
        with self.assertRaises(ValueError) as ctx:
            derive_design_mirror_state('some_new_status')
        self.assertIn('DESIGN_MIRROR_STATE_MAP', str(ctx.exception))

    def test_06_the_mapping_reaches_every_mirror_state_except_none(self):
        """Three of the four Task statuses are reachable, and that is deliberate.

        Stated as a test so that a later edit collapsing Blocked into In Progress — or
        adding a fifth mirror state, which is a migration plus every counter in the app —
        is a failure and not a silent narrowing.
        """
        self.assertEqual(
            set(DESIGN_MIRROR_STATE_MAP.values()),
            {Task.NOT_STARTED, Task.IN_PROGRESS, Task.BLOCKED, Task.DONE})


# ===========================================================================
# 2. The lookup — which of the eight mirrors, and the DESIGN code collision
# ===========================================================================

class TheLookupTests(MirrorDerivationBase):
    """`template_task.code`, scoped. The row this returns is the whole feature's aim."""

    def test_01_finds_the_design_mirror_and_not_as_built(self):
        site, _ = self._site('SE-LK1', DESIGN_AWAITING_ALLOCATION)
        task = self._design_mirror(site)
        self.assertEqual(task.task_name, 'Design')
        self.assertEqual(task.template_task.code, DESIGN_MIRROR_CODE)

    def test_02_role_design_matches_two_mirrors_which_is_why_code_is_used(self):
        """The trap, measured rather than asserted from the audit.

        If this ever returns 1, the lookup could have been role-keyed and this test is
        the thing that says so. It returning 2 is what makes `code` load-bearing.
        """
        site, _ = self._site('SE-LK2', DESIGN_AWAITING_ALLOCATION)
        by_role = Task.objects.filter(
            phase__project=site, is_mirror=True, assigned_role=Task.DESIGN)
        self.assertEqual(
            {t.task_name for t in by_role}, {'Design', AS_BUILT_NAME},
            'role=Design no longer matches two mirrors — re-read the lookup')

    def test_03_the_residential_template_has_a_task_whose_code_is_also_design(self):
        """The collision the scoping exists for, read out of the seeded templates.

        Both rows carry code='DESIGN'; `(phase, code)` is the uniqueness constraint, so
        this is legal and expected, and a portfolio-wide lookup on code alone would
        cross the two.
        """
        rows = {t.phase.template.project_type: t
                for t in TaskTemplateTask.objects
                .filter(code=DESIGN_MIRROR_CODE)
                .select_related('phase__template')}
        self.assertIn('Residential', rows, 'the Residential DESIGN task is gone')
        self.assertIn('OPEX', rows)
        self.assertFalse(rows['Residential'].is_mirror,
                         "the Residential Design task must never be a mirror")
        self.assertTrue(rows['OPEX'].is_mirror)

    def test_04_a_residential_project_has_no_design_mirror(self):
        """`is_mirror=True` IS the project-type guard, and this is what proves it.

        No `if project.project_type != 'OPEX'` exists in the lookup. It does not need
        one: the Residential template has no mirrors at all, so a Residential project
        returns None here without the function ever asking what type it is.
        """
        house = Project.objects.create(
            project_id='SE-RES1', customer_name='House', customer_phone='9876543210',
            site_address='2 Sun Rd', city='Delhi', project_type='Residential',
            capacity_kw=Decimal('5.00'), status='Draft', assigned_pm=self.pm)
        attach_residential_template(house)

        self.assertTrue(
            Task.objects.filter(phase__project=house,
                                template_task__code=DESIGN_MIRROR_CODE).exists(),
            'fixture is wrong: the Residential attach produced no DESIGN-coded task, '
            'so this test would pass for the wrong reason')
        self.assertIsNone(_design_mirror_task(house))

    def test_05_the_lookup_does_not_cross_projects(self):
        site_a, _ = self._site('SE-LK5A', DESIGN_AWAITING_ALLOCATION)
        site_b, _ = self._site('SE-LK5B', DESIGN_AWAITING_ALLOCATION)
        self.assertNotEqual(_design_mirror_task(site_a).pk,
                            _design_mirror_task(site_b).pk)

    def test_06_a_draft_site_with_no_tasks_returns_none(self):
        """The common case today: 87 sites hold a DesignAssignment, 0 hold a task."""
        site, _ = self._draft_site('SE-LK6', DESIGN_IN_DESIGN)
        self.assertIsNone(_design_mirror_task(site))


# ===========================================================================
# 3. The writer — the exact inverse of rung 0
# ===========================================================================

class TheWriterTests(MirrorDerivationBase):

    def setUp(self):
        self.site, self.assignment = self._site('SE-WR', DESIGN_AWAITING_ALLOCATION)
        self.mirror = self._design_mirror(self.site)

    def test_01_refuses_a_task_that_is_not_a_mirror(self):
        """The inverse of rung 0, and the reason both rules can stay total.

        Rung 0 refuses every mirror to every human; this refuses every non-mirror to
        every derivation. Between them every Task row is writable by exactly one path.
        """
        control = Task.objects.filter(
            phase__project=self.site, is_mirror=False).first()
        self.assertIsNotNone(control)
        with self.assertRaises(ValueError) as ctx:
            apply_mirror_status(control, Task.IN_PROGRESS, self.head,
                                REASON_MIRROR_DERIVED)
        self.assertIn('not a mirror', str(ctx.exception))

        control.refresh_from_db()
        self.assertEqual(control.status, Task.NOT_STARTED, 'the refusal wrote anyway')
        self.assertEqual(self._ledger(control), [], 'a refused write is not an event')

    def test_02_writes_the_status_and_one_ledger_row(self):
        wrote = apply_mirror_status(self.mirror, Task.IN_PROGRESS, self.head,
                                    REASON_MIRROR_DERIVED)
        self.assertTrue(wrote)

        self.mirror.refresh_from_db()
        self.assertEqual(self.mirror.status, Task.IN_PROGRESS)

        rows = self._ledger(self.mirror)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].from_status, Task.NOT_STARTED)
        self.assertEqual(rows[0].to_status, Task.IN_PROGRESS)
        self.assertEqual(rows[0].reason_code, REASON_MIRROR_DERIVED)

    def test_03_is_idempotent_and_writes_no_ledger_row(self):
        """LOAD-BEARING, not tidy.

        in_design -> arka_submitted -> awaiting_head_arka -> artifacts_uploaded -> in_qc
        is five design events that are all "In Progress". Writing unconditionally would
        leave four rows reading `In Progress -> In Progress`, and a row claiming x -> x
        is a history of something that did not happen (R-3).
        """
        apply_mirror_status(self.mirror, Task.IN_PROGRESS, self.head,
                            REASON_MIRROR_DERIVED)
        before = len(self._ledger(self.mirror))

        with CaptureQueriesContext(connection) as captured:
            wrote = apply_mirror_status(self.mirror, Task.IN_PROGRESS, self.head,
                                        REASON_MIRROR_DERIVED)

        self.assertFalse(wrote)
        self.assertEqual(len(self._ledger(self.mirror)), before,
                         'a no-op wrote a transition row')
        self.assertEqual(
            [q for q in captured.captured_queries
             if q['sql'].lstrip().upper().startswith(('UPDATE', 'INSERT'))], [],
            'a no-op issued a write')

    def test_04_names_only_the_columns_it_was_given(self):
        """filter().update(), not save() — the same rule apply_design_status() states.

        Every column outside the SET list is a column a concurrent writer still owns.
        """
        with CaptureQueriesContext(connection) as captured:
            apply_mirror_status(self.mirror, Task.IN_PROGRESS, self.head,
                                REASON_MIRROR_DERIVED)

        updates = [q['sql'] for q in captured.captured_queries
                   if re.search(r'UPDATE\s+"?projects_task"?', q['sql'], re.I)]
        self.assertEqual(len(updates), 1, 'the mirror was written more than once')
        columns = {c.strip().strip('"') for c in
                   re.findall(r'"?([A-Za-z_]+)"?\s*=',
                              re.search(r'SET\s+(.*?)\s+WHERE', updates[0],
                                        re.I | re.S).group(1))}
        self.assertEqual(columns, {'status'},
                         'the mirror write named a column it was not given')

    def test_05_done_stamps_completed_at(self):
        """Parity with the human path. A mirror at Done with a NULL completed_at is
        invisible to every completion metric in the app."""
        apply_mirror_status(self.mirror, Task.DONE, self.head, REASON_MIRROR_DERIVED)
        self.mirror.refresh_from_db()
        self.assertEqual(self.mirror.status, Task.DONE)
        self.assertIsNotNone(self.mirror.completed_at)

    def test_06_leaving_done_clears_completed_at(self):
        """THE ONE DELIBERATE DIVERGENCE FROM THE HUMAN PATH, pinned as one.

        `_apply_task_status_change()` does not clear this and never had to: a human may
        leave Done only for Blocked. A MIRROR reverses out of Done as ordinary business
        — the reopen route — and a completed_at standing on a row that is no longer done
        is a false date in a column people group by.
        """
        apply_mirror_status(self.mirror, Task.DONE, self.head, REASON_MIRROR_DERIVED)
        apply_mirror_status(self.mirror, Task.IN_PROGRESS, self.head,
                            REASON_MIRROR_DERIVED)
        self.mirror.refresh_from_db()
        self.assertEqual(self.mirror.status, Task.IN_PROGRESS)
        self.assertIsNone(self.mirror.completed_at,
                          'a reopened mirror still claims a completion date')

    def test_07_blocked_stamps_blocked_since_and_leaving_clears_it(self):
        apply_mirror_status(self.mirror, Task.BLOCKED, self.head, REASON_MIRROR_DERIVED)
        self.mirror.refresh_from_db()
        self.assertIsNotNone(self.mirror.blocked_since)

        apply_mirror_status(self.mirror, Task.IN_PROGRESS, self.head,
                            REASON_MIRROR_DERIVED)
        self.mirror.refresh_from_db()
        self.assertIsNone(self.mirror.blocked_since,
                          'a re-block would age from the FIRST block, not from zero')

    def test_08_a_derived_block_creates_no_issue(self):
        """The human Blocked branch auto-creates an Issue; a derivation must not.

        Conflating a Design Hold with the project issue log is exactly what the spec
        dropped the Punch Points mirror to avoid.
        """
        apply_mirror_status(self.mirror, Task.BLOCKED, self.head, REASON_MIRROR_DERIVED)
        self.assertEqual(Issue.objects.filter(task=self.mirror).count(), 0)
        self.assertEqual(Issue.objects.filter(project=self.site).count(), 0)

    def test_09_never_writes_a_due_date(self):
        """R-20 keeps mirrors out of every overdue count, and an undated mirror must
        still be able to follow its source — the human path's "In Progress requires a
        due date" guard is a rule about people, not about rows."""
        self.assertIsNone(self.mirror.due_date, 'fixture: mirrors seed undated')
        for status in (Task.IN_PROGRESS, Task.BLOCKED, Task.DONE):
            apply_mirror_status(self.mirror, status, self.head, REASON_MIRROR_DERIVED)
            self.mirror.refresh_from_db()
            self.assertIsNone(self.mirror.due_date,
                              f'moving to {status} gave the mirror a due date')

    def test_10_a_failing_ledger_takes_the_status_write_with_it(self):
        """No try/except, on purpose, and asserted rather than implied.

        `record_transition()` raises where `log_activity()` swallows, and this runs
        inside the caller's atomic block. A mirror that moved with no record of why is
        worse than an action that visibly failed and can be retried. A future session
        "harmonising" the two helpers fails here.
        """
        from django.db import transaction

        with patch('projects.design_views.record_transition',
                   side_effect=RuntimeError('ledger down')):
            with self.assertRaises(RuntimeError):
                with transaction.atomic():
                    apply_mirror_status(self.mirror, Task.IN_PROGRESS, self.head,
                                        REASON_MIRROR_DERIVED)

        self.mirror.refresh_from_db()
        self.assertEqual(self.mirror.status, Task.NOT_STARTED,
                         'the status survived a lost ledger row')

    def test_11_carries_the_source_events_actor(self):
        """Spec §2.8 — the ledger reads truthfully rather than attributing every mirror
        move to a system user."""
        apply_mirror_status(self.mirror, Task.IN_PROGRESS, self.head,
                            REASON_MIRROR_DERIVED)
        row = self._ledger(self.mirror)[-1]
        self.assertEqual(row.actor_id, self.head.pk)
        self.assertEqual(row.actor_role_code, self.head.role)


# ===========================================================================
# 4. The hook — a design status moves, the mirror follows
# ===========================================================================

class TheHookTests(MirrorDerivationBase):
    """Driven through `apply_design_status()`, which is the only door to the hook."""

    def setUp(self):
        self.site, self.assignment = self._site('SE-HK', DESIGN_AWAITING_ALLOCATION)
        self.mirror = self._design_mirror(self.site)
        self.assertEqual(self.mirror.status, Task.NOT_STARTED,
                         'fixture: awaiting_allocation reconciles to Not Started')

    def _move(self, status):
        from django.db import transaction
        with transaction.atomic():
            apply_design_status(self.assignment, status, self.head,
                                f'moved to {status}', 'design_test_move')
        self.mirror.refresh_from_db()
        return self.mirror.status

    def test_01_the_full_real_sequence(self):
        """Several real status changes on an already-activated site, in order.

        The linear workflow, then the hold, then release, then the reopen. Each step
        asserts the mirror AFTER the move rather than at the end, so a mapping that is
        right only at the last step fails here.
        """
        self.assertEqual(self._move(DESIGN_IN_DESIGN),          Task.IN_PROGRESS)
        self.assertEqual(self._move(DESIGN_ARKA_SUBMITTED),     Task.IN_PROGRESS)
        self.assertEqual(self._move(DESIGN_AWAITING_HEAD_ARKA), Task.IN_PROGRESS)
        self.assertEqual(self._move(DESIGN_ARTIFACTS_UPLOADED), Task.IN_PROGRESS)
        self.assertEqual(self._move(DESIGN_IN_QC),              Task.IN_PROGRESS)
        self.assertEqual(self._move(DESIGN_SURVEY_RETURNED),    Task.BLOCKED)
        self.assertEqual(self._move(DESIGN_IN_QC),              Task.IN_PROGRESS)
        self.assertEqual(self._move(DESIGN_AWAITING_HEAD_QC),   Task.IN_PROGRESS)
        self.assertEqual(self._move(DESIGN_RELEASED),           Task.DONE)
        # The reopen. released -> in_design is the accepted-change-request route, and
        # Done -> In Progress is a move VALID_TRANSITIONS forbids humans. Spec rule 3
        # requires it of a mirror: mirrors follow their source in BOTH directions.
        self.assertEqual(self._move(DESIGN_IN_DESIGN),          Task.IN_PROGRESS)

    def test_02_five_in_progress_design_moves_write_one_mirror_row(self):
        """The idempotency guard, seen from the hook's side.

        Five design transitions, one mirror transition. This is the shape §3.3 of the
        audit is about, and it is asserted end-to-end rather than only on the writer.
        """
        for status in (DESIGN_IN_DESIGN, DESIGN_ARKA_SUBMITTED,
                       DESIGN_AWAITING_HEAD_ARKA, DESIGN_ARTIFACTS_UPLOADED,
                       DESIGN_IN_QC):
            self._move(status)

        rows = self._ledger(self.mirror)
        self.assertEqual(len(rows), 1,
                         f'five design moves wrote {len(rows)} mirror transitions')
        self.assertEqual((rows[0].from_status, rows[0].to_status),
                         (Task.NOT_STARTED, Task.IN_PROGRESS))

    def test_03_a_no_transition_call_does_not_touch_the_mirror(self):
        """`new_status=None` is the companion-field write. Nothing moved, so the mirror
        must not move either — the hook sits under the same guard as the ledger."""
        from django.db import transaction
        self._move(DESIGN_IN_DESIGN)
        before = len(self._ledger(self.mirror))

        with transaction.atomic():
            apply_design_status(self.assignment, None, self.head,
                                'survey file replaced', 'design_survey_replaced',
                                extra_fields={'survey_file_path': 'x/y.pdf'})

        self.assertEqual(len(self._ledger(self.mirror)), before)

    def test_04_restating_the_current_status_does_not_touch_the_mirror(self):
        from django.db import transaction
        self._move(DESIGN_IN_DESIGN)
        before = len(self._ledger(self.mirror))

        with transaction.atomic():
            apply_design_status(self.assignment, DESIGN_IN_DESIGN, self.head,
                                'restated', 'design_test_move')

        self.assertEqual(len(self._ledger(self.mirror)), before)

    def test_05_the_hook_moves_only_the_design_mirror(self):
        """The other seven mirrors have their own sources and none of them is this one.

        As-Built Drawings is the one that matters: it carries role='Design' and is
        post-commissioning, so a role-keyed hook would move it here.
        """
        as_built = self._as_built(self.site)
        others = list(Task.objects.filter(phase__project=self.site, is_mirror=True)
                      .exclude(pk=self.mirror.pk))
        self.assertEqual(len(others), 7, 'the OPEX template no longer has 8 mirrors')

        self._move(DESIGN_RELEASED)

        for task in others:
            task.refresh_from_db()
            self.assertEqual(task.status, Task.NOT_STARTED,
                             f'{task.task_name!r} moved when the design status did')
        as_built.refresh_from_db()
        self.assertEqual(as_built.status, Task.NOT_STARTED)

    def test_06_a_design_move_on_an_unactivated_site_is_a_quiet_no_op(self):
        """Today this is EVERY site: 87 assignments, 0 tasks.

        It must not raise, and it must not warn — a not-yet-activated site is the
        expected case, and a warning per design move on 96 sites would bury the real
        defect the warning exists for.
        """
        from django.db import transaction
        site, assignment = self._draft_site('SE-HK6', DESIGN_AWAITING_ALLOCATION)

        with self.assertLogs('projects.design_views', level='DEBUG') as logs:
            with transaction.atomic():
                apply_design_status(assignment, DESIGN_IN_DESIGN, self.head,
                                    'allocated', 'design_allocated')

        assignment.refresh_from_db()
        self.assertEqual(assignment.status, DESIGN_IN_DESIGN,
                         'the design status itself must still move')
        self.assertTrue(any('not activated' in line for line in logs.output))
        self.assertFalse(any(line.startswith('WARNING') for line in logs.output),
                         'the routine case warned; the real defect will be buried')

    def test_07_a_detached_mirror_warns(self):
        """The OTHER not-found case, and the reason the two are told apart.

        The site HAS tasks, so the template was attached, and the Design mirror still
        cannot be found by its code — a re-versioned template with the code changed, or
        a deleted row. The mirror has silently stopped following its source and nothing
        else in the system would say so.
        """
        from django.db import transaction
        self.mirror.delete()

        with self.assertLogs('projects.design_views', level='WARNING') as logs:
            with transaction.atomic():
                apply_design_status(self.assignment, DESIGN_IN_DESIGN, self.head,
                                    'allocated', 'design_allocated')

        self.assertTrue(any('NOT FOUND' in line for line in logs.output))


# ===========================================================================
# 5. The reconcile — the half that covers every site that exists today
# ===========================================================================

class ReconcileAtActivationTests(MirrorDerivationBase):
    """THE REAL-WORLD CONDITION: a DesignAssignment exists, the site is not activated.

    That describes 87 of the 96 OPEX sites in the database and 0 sites are the other
    way round. Without this, activating any one of them mints a Design mirror reading
    Not Started against a source that has moved on.
    """

    def test_01_activation_reconciles_to_the_current_design_status(self):
        """The headline: correct IMMEDIATELY, not Not Started."""
        for code, design_status, expected in (
            ('SE-RC-A', DESIGN_IN_DESIGN,          Task.IN_PROGRESS),
            ('SE-RC-B', DESIGN_ARTIFACTS_UPLOADED, Task.IN_PROGRESS),
            ('SE-RC-C', DESIGN_IN_QC,              Task.IN_PROGRESS),
            ('SE-RC-D', DESIGN_RELEASED,           Task.DONE),
            ('SE-RC-E', DESIGN_SURVEY_RETURNED,    Task.BLOCKED),
        ):
            with self.subTest(design_status=design_status):
                site, _ = self._site(code, design_status)
                mirror = self._design_mirror(site)
                self.assertEqual(
                    mirror.status, expected,
                    f'a site activated at design status {design_status!r} got a '
                    f'mirror reading {mirror.status!r}')

    def test_02_the_five_statuses_that_actually_exist_in_production(self):
        """The measured distribution, not an invented one.

        awaiting_allocation: 82 · artifacts_uploaded: 2 · in_qc: 1 · arka_submitted: 1
        · in_design: 1. Every one of the 87 sites that exist is covered by this test.
        """
        for code, design_status, expected in (
            ('SE-PR-1', DESIGN_AWAITING_ALLOCATION, Task.NOT_STARTED),
            ('SE-PR-2', DESIGN_ARTIFACTS_UPLOADED,  Task.IN_PROGRESS),
            ('SE-PR-3', DESIGN_IN_QC,               Task.IN_PROGRESS),
            ('SE-PR-4', DESIGN_ARKA_SUBMITTED,      Task.IN_PROGRESS),
            ('SE-PR-5', DESIGN_IN_DESIGN,           Task.IN_PROGRESS),
        ):
            with self.subTest(design_status=design_status):
                site, _ = self._site(code, design_status)
                self.assertEqual(self._design_mirror(site).status, expected)

    def test_03_the_reconcile_writes_a_ledger_row_attributed_to_nobody(self):
        """ACTOR_ROLE_SYSTEM, and it is the honest answer rather than a shortcut.

        Nobody moved design at activation — the mirror is catching up to a status
        somebody else set, possibly months earlier. Stamping the activating PM would
        be a lie in the one column that exists to answer "who".
        """
        site, _ = self._site('SE-RC3', DESIGN_IN_QC)
        rows = self._ledger(self._design_mirror(site))
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0].from_status, rows[0].to_status),
                         (Task.NOT_STARTED, Task.IN_PROGRESS))
        self.assertIsNone(rows[0].actor_id)
        self.assertEqual(rows[0].actor_role_code, ACTOR_ROLE_SYSTEM)
        self.assertEqual(rows[0].reason_code, REASON_MIRROR_DERIVED)

    def test_04_no_design_assignment_leaves_the_template_default(self):
        """The 9 sites with no assignment row. Not Started is CORRECT for them, and it
        must arrive as the seeded value with no transition invented for it."""
        site, assignment = self._site('SE-RC4', design_status=None)
        self.assertIsNone(assignment, 'fixture: this site must have no assignment')
        mirror = self._design_mirror(site)
        self.assertEqual(mirror.status, Task.NOT_STARTED)
        self.assertEqual(self._ledger(mirror), [],
                         'a site with no design assignment invented a transition')

    def test_05_awaiting_allocation_writes_nothing_because_it_is_already_right(self):
        """82 of 87 sites. The reconcile runs, agrees with the seed, and writes nothing
        — the writer's idempotency guard doing its job at activation time."""
        site, _ = self._site('SE-RC5', DESIGN_AWAITING_ALLOCATION)
        mirror = self._design_mirror(site)
        self.assertEqual(mirror.status, Task.NOT_STARTED)
        self.assertEqual(self._ledger(mirror), [])

    def test_06_the_reconcile_touches_only_the_design_mirror(self):
        site, _ = self._site('SE-RC6', DESIGN_RELEASED)
        design = self._design_mirror(site)
        self.assertEqual(design.status, Task.DONE)
        for task in Task.objects.filter(phase__project=site, is_mirror=True
                                        ).exclude(pk=design.pk):
            self.assertEqual(task.status, Task.NOT_STARTED,
                             f'{task.task_name!r} was reconciled to the design status')

    def test_07_a_residential_activation_is_untouched(self):
        """The Residential path does not go through attach_opex_template() at all, and
        has no mirror to reconcile even if it did. Asserted rather than assumed."""
        house = Project.objects.create(
            project_id='SE-RC7', customer_name='House', customer_phone='9876543210',
            site_address='3 Sun Rd', city='Delhi', project_type='Residential',
            capacity_kw=Decimal('5.00'), status='Draft', assigned_pm=self.pm)
        attach_residential_template(house)

        self.assertFalse(Task.objects.filter(phase__project=house,
                                             is_mirror=True).exists())
        self.assertEqual(
            list(StatusTransition.objects.filter(
                subject_type=SUBJECT_TASK,
                subject_id__in=Task.objects.filter(phase__project=house)
                                           .values_list('pk', flat=True))),
            [], 'the Residential attach wrote a task transition')

    def test_08_activation_still_produces_the_whole_template(self):
        """The reconcile is a second write on ONE row and must not disturb the attach's
        own integrity assertions or its PM pre-assignment."""
        site, _ = self._site('SE-RC8', DESIGN_RELEASED)
        tasks = Task.objects.filter(phase__project=site)
        self.assertEqual(tasks.filter(is_mirror=True).count(), 8)
        self.assertEqual(
            tasks.filter(assigned_role=Task.PM, is_mirror=True,
                         assigned_to__isnull=False).count(), 0,
            'the reconcile assigned a mirror — mirrors stay nobody\'s task')
        self.assertTrue(
            tasks.filter(assigned_role=Task.PM, is_mirror=False,
                         assigned_to=self.pm).exists(),
            'the attach no longer pre-assigns the PM\'s own tasks')


# ===========================================================================
# 6. Rung 0 is unchanged — this session weakens the human refusal in no way
# ===========================================================================

class RungZeroStillRefusesTests(MirrorDerivationBase):
    """The Design mirror is now WRITTEN by a derivation and still REFUSED to humans.

    That pair is the whole design, and it is the pair a reader will doubt. Both entry
    points are driven, because R-18 puts the refusal in the shared helper and a rule
    that stops holding on one screen must fail a named test rather than become a
    support ticket.
    """

    ENTRIES = ('task_status_update', 'task_detail_status_update')

    def setUp(self):
        self.site, self.assignment = self._site('SE-R0', DESIGN_IN_QC)
        self.mirror = self._design_mirror(self.site)
        self.assertEqual(self.mirror.status, Task.IN_PROGRESS,
                         'fixture: the reconcile put the mirror at In Progress')
        # THE TRAP (B22): mirrors seed unassigned and BOTH screens refuse an unassigned
        # task before the mirror rule is ever reached. Without this the refusals below
        # would pass for the wrong reason.
        assign_task_to(self.mirror, self.pm, notify=False)
        self.mirror.refresh_from_db()
        self.assertEqual(self.mirror.assigned_to, self.pm)

    def test_01_both_screens_refuse_every_human_move(self):
        for entry in self.ENTRIES:
            for target in (Task.NOT_STARTED, Task.IN_PROGRESS, Task.BLOCKED, Task.DONE):
                with self.subTest(entry=entry, target=target):
                    client = Client()
                    client.force_login(self.pm.user)
                    client.post(
                        reverse(entry, args=[self.site.project_id, self.mirror.pk]),
                        {'status': target, 'block_issue_title': 'x'})
                    self.mirror.refresh_from_db()
                    self.assertEqual(
                        self.mirror.status, Task.IN_PROGRESS,
                        f'{entry} let a human write the Design mirror to {target}')

    def test_02_the_refusal_is_still_unconditional_in_source(self):
        """Read from the source, because tests 01 passes just as well if somebody adds
        a bypass parameter that this session's callers happen not to use.

        The predicate must remain `task.is_mirror` and nothing else.
        """
        from . import views
        source = io.open(
            os.path.abspath(views.__file__.replace('.pyc', '.py')),
            encoding='utf-8-sig').read()
        body = inspect.getsource(views._apply_task_status_change)
        self.assertIn('if task.is_mirror:', body)
        self.assertRegex(
            body, r'if task\.is_mirror:\s*\n\s*messages\.error\(',
            'rung 0 gained a condition beyond task.is_mirror')
        self.assertNotIn('sync_design_mirror', source,
                         'views.py now calls the derivation — the two paths must stay '
                         'apart, and views.py needs no edit for this feature')
        self.assertNotIn('apply_mirror_status', source)

    def test_03_the_derivation_still_moves_it_after_a_refused_human_attempt(self):
        """The pair, in one test: refused to a person, written by its source."""
        from django.db import transaction
        client = Client()
        client.force_login(self.pm.user)
        client.post(reverse('task_status_update',
                            args=[self.site.project_id, self.mirror.pk]),
                    {'status': Task.DONE})
        self.mirror.refresh_from_db()
        self.assertEqual(self.mirror.status, Task.IN_PROGRESS)

        with transaction.atomic():
            apply_design_status(self.assignment, DESIGN_RELEASED, self.head,
                                'released', 'design_head_qc_passed')
        self.mirror.refresh_from_db()
        self.assertEqual(self.mirror.status, Task.DONE)


# ===========================================================================
# 7. Callers — proved absent, not asserted absent
# ===========================================================================

class CallerDisciplineTests(TestCase):
    """Session C's discipline: a claim about who calls what is read out of the source.

    A narrow writer is only narrow while it stays narrow, and the next session to want
    a mirror written will reach for the writer directly rather than through the
    composition. This is what makes that visible in review.
    """

    #: Every .py under projects/, tests included. A test that calls the writer directly
    #: is a caller too, and one that grew a second production caller by copy-paste from
    #: a test is exactly the drift this sweeps for.
    def _sources(self):
        root = os.path.dirname(os.path.abspath(design_views.__file__))
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d != '__pycache__']
            for name in sorted(filenames):
                if name.endswith('.py'):
                    path = os.path.join(dirpath, name)
                    yield (os.path.relpath(path, root).replace('\\', '/'),
                           io.open(path, encoding='utf-8-sig').read())

    def _code_names(self, source):
        """[(lineno, NAME token)] for `source`, EXCLUDING comments and string literals.

        TOKENISED RATHER THAN GREPPED, and the difference is not pedantry. Both
        functions this class sweeps for are NAMED in prose all over this codebase — in
        the writer's own ValueError message ("apply_mirror_status(): task ... is not a
        mirror"), in rung 0's comment block, in the attachment point's, in this module's
        docstring. A regex over raw text calls every one of those a caller, and a sweep
        that cries wolf on documentation gets deleted by the first person it blocks.

        A tokeniser answers the question actually being asked — "is this identifier
        EXECUTED here" — and a file that will not tokenise is a syntax error somebody
        else's test will catch.
        """
        import tokenize
        names = []
        try:
            for token in tokenize.generate_tokens(io.StringIO(source).readline):
                if token.type == tokenize.NAME:
                    names.append((token.start[0], token.string))
        except (tokenize.TokenError, IndentationError, SyntaxError):
            return []
        return names

    def _imported_names(self, source):
        """{(lineno, name)} for every name BOUND BY AN IMPORT in `source`.

        PARSED, BECAUSE THE LINE-PREFIX TEST BELOW CANNOT SEE A PARENTHESISED IMPORT.
        `from .design_views import (\\n    sync_delivery_mirrors,\\n)` puts the name on
        a line of its own that starts with neither `import` nor `from`, so the prefix
        check waves it through and the sweep reports an IMPORT as a CALL. That is not
        hypothetical — views.py imports the delivery derivation exactly that way, and
        this method exists because it did.

        The consequence ran in the safe direction here (a false caller fails loudly)
        but it runs the other way just as easily: the whole point of these tests is to
        count the writer's callers exactly, and a counter that miscounts parenthesised
        imports is a counter that can be quietly satisfied by reformatting an import.

        A SyntaxError yields the empty set rather than raising: `_code_names()` already
        returns [] for a file that will not tokenise, and a file that will not parse is
        somebody else's test's problem, not this sweep's.
        """
        import ast
        try:
            tree = ast.parse(source)
        except (SyntaxError, ValueError):
            return set()
        bound = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    bound.add((alias.lineno, alias.asname or alias.name.split('.')[0]))
        return bound

    def _call_sites(self, function_name, *, exclude_files=()):
        """Files and lines where `function_name` appears as EXECUTED code.

        Its own `def` and every name bound by an `import` are excluded — a definition
        is not a call and neither is an import, and these functions are imported by
        this module precisely so they can be tested.
        """
        found = []
        for relpath, source in self._sources():
            if relpath in exclude_files:
                continue
            lines = source.splitlines()
            imported = self._imported_names(source)
            for lineno, name in self._code_names(source):
                if name != function_name:
                    continue
                # Single-line `def`/`import`/`from`, then the parenthesised-import case
                # the prefix test structurally cannot reach. Both kept: the prefix test
                # still covers a file that parses badly enough for ast to give up.
                stripped = lines[lineno - 1].strip()
                if stripped.startswith(('def ', 'import ', 'from ')):
                    continue
                if (lineno, name) in imported:
                    continue
                found.append(f'{relpath}:{lineno}: {stripped}')
        return found

    # The closed list of compositions permitted to write a mirror. ADDING A NAME HERE
    # IS THE WHOLE DECISION — it is meant to be a deliberate, reviewed act, which is
    # why the list is a literal and not computed from anything.
    MIRROR_WRITER_CALLERS = ('sync_design_mirror', 'sync_delivery_mirrors')

    def test_01_apply_mirror_status_is_called_only_by_named_compositions(self):
        """The two named derivations and nothing else.

        The writer is the door; the composition decides WHICH row and WHAT state. A
        caller that is not on this list means somebody has a Task in hand and a status
        in mind, which is the shape rung 0 exists to refuse.

        THE INVARIANT IS THE CLOSED LIST, NOT THE COUNT. Session E's version of this
        test asserted "exactly one caller" because at the time there was exactly one;
        Session F added the delivery derivation and the assertion became a list of two.
        What must NEVER happen is this relaxing into "any caller inside
        design_views.py" or a bare count — either would let a third composition appear
        with nobody deciding it should. The three assertions below are closed together:
        the count fixes how many call sites exist, the file check fixes where, and the
        per-function check fixes which functions they are inside. Two callers, both in
        design_views.py, one provably inside each named function, leaves no room for an
        unnamed third.
        """
        callers = self._call_sites(
            'apply_mirror_status',
            exclude_files=('tests_design_mirror_derivation.py',))
        self.assertEqual(
            len(callers), len(self.MIRROR_WRITER_CALLERS),
            'apply_mirror_status() has %d call sites, expected exactly %d (%s). A new '
            'one is a new composition and must be added to MIRROR_WRITER_CALLERS '
            'deliberately:\n%s' % (len(callers), len(self.MIRROR_WRITER_CALLERS),
                                   ', '.join(self.MIRROR_WRITER_CALLERS),
                                   '\n'.join(callers)))
        for caller in callers:
            self.assertTrue(
                caller.startswith('design_views.py:'),
                f'the writer is called from outside design_views.py: {caller}')

        # And each call site is inside one of the NAMED compositions, not merely in the
        # same file — the file-level check above would pass for a second view. With the
        # count pinned above, one call proven inside each name accounts for all of them.
        for name in self.MIRROR_WRITER_CALLERS:
            self.assertIn(
                'apply_mirror_status(',
                inspect.getsource(globals()[name]),
                f'{name}() is named as a mirror-writing composition but does not '
                f'call the writer')

    def test_01b_sync_delivery_mirrors_is_called_only_by_the_delivery_paths(self):
        """The DC create and the DC status recalculation. Both are named.

        The delivery analogue of test_02, and it exists for the same reason: a
        derivation nobody can reach from an unexpected place is what makes "read-derived"
        true. `recalculate_dc_status` (models.py) covers both GRN endpoints because they
        both funnel through it; `create_delivery_challan` (views.py) is separate only
        because that view deliberately does not call it.
        """
        callers = self._call_sites(
            'sync_delivery_mirrors',
            exclude_files=('tests_design_mirror_derivation.py',))
        files = sorted({c.split(':')[0] for c in callers})
        self.assertEqual(files, ['models.py', 'views.py'],
                         'sync_delivery_mirrors() gained a caller:\n'
                         + '\n'.join(callers))
        self.assertEqual(len(callers), 2,
                         'sync_delivery_mirrors() is called more than twice:\n'
                         + '\n'.join(callers))

    def test_02_sync_design_mirror_has_exactly_two_production_callers(self):
        """The hook and the reconcile. Both are named, so a third fails here."""
        callers = self._call_sites(
            'sync_design_mirror',
            exclude_files=('tests_design_mirror_derivation.py',))
        files = sorted({c.split(':')[0] for c in callers})
        self.assertEqual(files, ['design_views.py', 'utils.py'],
                         'sync_design_mirror() gained a caller:\n' + '\n'.join(callers))
        self.assertEqual(len(callers), 2,
                         'sync_design_mirror() is called more than twice:\n'
                         + '\n'.join(callers))

    def test_03_the_derivation_never_enters_the_human_status_path(self):
        """`_apply_task_status_change()` is never EXECUTED from the design module.

        Tokenised, not grepped, and the distinction is the point of the test rather
        than an implementation detail. design_views.py NAMES that function repeatedly
        and deliberately — the attachment point says the mirror "does NOT enter
        `_apply_task_status_change()`", the writer's docstring says it is that
        function's exact inverse. Those comments are the design being written down and
        must stay; what must never appear is the identifier in executable position.
        """
        source = io.open(
            os.path.abspath(design_views.__file__.replace('.pyc', '.py')),
            encoding='utf-8-sig').read()

        executed = {name for _lineno, name in self._code_names(source)}
        self.assertNotIn(
            '_apply_task_status_change', executed,
            'design_views.py now executes the HUMAN status path. The two doors are '
            'total only while they stay apart: rung 0 refuses every mirror to every '
            'person, apply_mirror_status() refuses every non-mirror to every '
            'derivation.')
        self.assertNotIn('from .views import', source,
                         'the design module imports from views.py')

        # The other direction, and the reason views.py needed no edit for this feature.
        from . import views
        views_source = io.open(
            os.path.abspath(views.__file__.replace('.pyc', '.py')),
            encoding='utf-8-sig').read()
        views_executed = {name for _lineno, name in self._code_names(views_source)}
        for name in ('sync_design_mirror', 'apply_mirror_status',
                     'derive_design_mirror_state'):
            self.assertNotIn(name, views_executed,
                             f'views.py now executes {name}() — the derivation is '
                             f'reached from the design module and nowhere else')

    def test_04_design_views_writes_no_task_status_outside_the_writer(self):
        """The Task-side analogue of Session C's own source sweep."""
        source = io.open(
            os.path.abspath(design_views.__file__.replace('.pyc', '.py')),
            encoding='utf-8-sig').read()
        offenders = [
            line.strip() for line in source.splitlines()
            if re.search(r'^\s*Task\.objects\.filter\(.*\)\.update\(', line)
            or re.search(r'^\s*task\.status\s*=(?!=)', line)
        ]
        # The ONE legitimate write lives in apply_mirror_status().
        writer = inspect.getsource(apply_mirror_status)
        for line in offenders:
            self.assertIn(line, writer,
                          f'design_views.py writes a Task status outside '
                          f'apply_mirror_status(): {line}')
