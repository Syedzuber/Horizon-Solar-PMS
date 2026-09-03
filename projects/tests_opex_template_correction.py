"""Prompt 1.5 — the OPEX template's corrected table, and the three things the screen got wrong.

WHAT THIS SESSION CHANGED, and why the shape of these tests is unusual.

The browser test passed every blocker and then found three things no assertion had
covered, because all three were about what the SCREEN says rather than what the database
holds. This module pins all three, plus the corrected template they sit on:

  1. The Payment Milestones card rendered on OPEX sites, offering a Create M1 / M2 / M3
     button for an action `opex_site_activate` deliberately refuses. Card and endpoint
     are both gated now, and both halves are asserted — a hidden control is not a
     disabled one.
  2. The two SCM inspections left the template (an inspection at a vendor's works covers
     a consignment, not a site) and Material Delivery split into four delivery mirrors.
     23 tasks, 8 mirrors.
  3. Eight rows never change status and the page gave no reason. There is a marker now,
     and it is PRESENTATION ONLY — the last class here posts to both status entry points
     and proves the refusal is still the thing doing the refusing.

THE TEMPLATE WAS CORRECTED IN PLACE, NOT SUPERSEDED BY AN OPEX v2, and these tests
assert that too. R-7 freezes a version that is live; this one never was. `origin/main` is
at migration 0064 and 0075 has never been deployed, so there is no site anywhere that was
attached to the old table and no history worth a version record. `OpexIsStillOneVersionTests`
pins that there is exactly one OPEX version — a v2 appearing later means somebody bumped
without reading spec §6, or the deployment condition changed and the answer flipped back.

WHY THE PROGRESS BAR COUNTS MIRRORS HERE AND NOWHERE ELSE. Phase 3 now holds only
mirrors, as Phase 1 already did, and both rendered "0/0 done" above an empty bar while
their own card headers said "4 tasks" and "1 task". The product decision of 1 Sep 2026 is
that one mirror not updated is one task pending, so the per-phase bar counts them —
a deliberate, narrow carve-out of R-20 that `tests_mirror_metrics.py` pins from the other
side. R-21 is untouched: a mirror still never makes its phase current.

HOW THE FIXTURE GETS ITS DATA. `test_settings` disables migrations, so migration 0075
never runs and no OPEX template exists in a test database. `OpexActivationBase` is
imported from `tests_opex_activation` rather than rebuilt, so there is ONE definition of
"seed the OPEX template and stand up a Draft site under test" — the same reason
`tests_demo_data` imports `_seed_opex` from there.

Run with:
    python manage.py test projects.tests_opex_template_correction --settings=solarpms.test_settings
"""
import json

from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import reverse

from .models import (
    PaymentMilestone, Project, ProjectPhase, StatusTransition, Task, TaskTemplate,
    TaskTemplatePhase, TaskTemplateTask, SUBJECT_TASK,
)
from .utils import assign_task_to, resolve_residential_template

# Imported, not re-declared: the base class carries the seed, the PM, the Draft OPEX
# site and the activate helper, and a second copy would drift from it.
from .tests_opex_activation import OpexActivationBase, _client_for, _seed_opex
# The reverse lives in tests_opex_template.py, which is where 0075's two RunPython
# functions are wrapped as a pair; tests_opex_activation only ever needed the forward.
from .tests_opex_template import _unseed_opex

OPEX_CODE        = 'OPEX'
RESIDENTIAL_CODE = 'RESIDENTIAL'

# A FOURTH independent transcription of docs/OPEX_task_template_spec.md v1.5 §3.
# Migration 0075 holds the first, tests_opex_template.py the second,
# tests_opex_activation.py the third. Four is not redundancy for its own sake: this one
# is the only list that also states the phase each mirror sits in, which is what the
# mirrors-only-phase assertions below need.
EXPECTED_MIRRORS_BY_PHASE = {
    'Design': {'Design'},
    'Procurement & Delivery': {
        'Delivery — Solar Panels',
        'Delivery — Inverters',
        'Delivery — BOS Kit',
        'Delivery — MMS',
    },
    'Closeout': {'COD', 'As-Built Drawings', 'HOTO'},
}
EXPECTED_MIRROR_NAMES = set().union(*EXPECTED_MIRRORS_BY_PHASE.values())

# The two phases that hold ONLY mirrors. Neither can ever be a current phase (R-21).
MIRROR_ONLY_PHASES = {'Design', 'Procurement & Delivery'}

# The badge's title attribute, which is unique to it in the rendered page. Matching on
# the title rather than the word "Derived" keeps the count honest — "Derived" could
# plausibly appear in a task name or another tooltip later.
MIRROR_BADGE = 'Derived from the workspace that owns this work'

# The card's own heading. Matching the heading rather than the Create button is the
# point of the change: hiding only the button would leave a card reading "No milestones
# yet", which implies there should be some.
MILESTONE_CARD_HEADING = 'Payment Milestones'


# ---------------------------------------------------------------------------
# 1 — the corrected template
# ---------------------------------------------------------------------------

class CorrectedOpexTemplateTests(TestCase):
    """The seeded rows, before any project touches them."""

    @classmethod
    def setUpTestData(cls):
        resolve_residential_template()
        _seed_opex()

    def _opex(self):
        return TaskTemplate.objects.get(code=OPEX_CODE, status=TaskTemplate.ACTIVE)

    def test_the_active_opex_template_is_seven_phases_and_twenty_three_tasks(self):
        template = self._opex()
        self.assertEqual(
            TaskTemplatePhase.objects.filter(template=template).count(), 7)
        self.assertEqual(
            TaskTemplateTask.objects.filter(phase__template=template).count(), 23)

    def test_exactly_the_eight_named_rows_are_mirrors(self):
        """BY NAME and both directions. A count of 8 passes when the wrong eight are
        flagged, which is the failure this is built to catch."""
        tasks = TaskTemplateTask.objects.filter(phase__template=self._opex())
        self.assertEqual({t.label for t in tasks if t.is_mirror}, EXPECTED_MIRROR_NAMES)
        self.assertEqual(tasks.filter(is_mirror=True).count(), 8)
        self.assertEqual(tasks.filter(is_mirror=False).count(), 15)

    def test_the_two_inspections_are_gone(self):
        """Asserted as an absence, deliberately. Every other test here would still pass
        if the inspections were re-added alongside the four delivery mirrors — the
        counts would simply be 25 and somebody would update them."""
        labels = set(
            TaskTemplateTask.objects.filter(phase__template=self._opex())
            .values_list('label', flat=True))
        for gone in ('Inspection — Factory / Vendor',
                     'Inspection — Post-Delivery / Unloading',
                     'Material Delivery'):
            self.assertNotIn(
                gone, labels,
                f'{gone!r} is back in the OPEX template. The inspections belong to '
                f'SCM and inventory at phase 4.5 (an inspection covers a consignment, '
                f'not a site); "Material Delivery" was replaced by four named rows.')

    def test_the_four_delivery_mirrors_sit_in_order_and_belong_to_scm(self):
        phase = TaskTemplatePhase.objects.get(
            template=self._opex(), label='Procurement & Delivery')
        rows = list(TaskTemplateTask.objects.filter(phase=phase).order_by('sort_order'))
        self.assertEqual(
            [(r.sort_order, r.label, r.assigned_role, r.is_mirror) for r in rows],
            [(1, 'Delivery — Solar Panels', 'SCM', True),
             (2, 'Delivery — Inverters',    'SCM', True),
             (3, 'Delivery — BOS Kit',      'SCM', True),
             (4, 'Delivery — MMS',          'SCM', True)])

    def test_scm_and_design_own_no_entered_task_at_all(self):
        """A CONSEQUENCE, pinned so it is a decision rather than a discovery.

        Removing the two inspections took away SCM's only entered work, so SCM now owns
        four mirrors and nothing else — the position Design was already in. No SCM or
        Design person has a single actionable task on an OPEX site, and none of their
        six mirrors can move until B-18 and SCM's catalogue mapping both land.
        Recorded in EXECUTION_MODULE_DEFERRED.md §B27.
        """
        entered = TaskTemplateTask.objects.filter(
            phase__template=self._opex(), is_mirror=False)
        self.assertEqual(
            set(entered.values_list('assigned_role', flat=True)),
            {'PM', 'Site Engineer', 'Project Coordinator'},
            'SCM or Design has gained an entered task — if that is intended, this test '
            'and the deferred-module entry both need updating.')

    def test_every_task_is_internal_with_the_placeholder_duration(self):
        """Spec §5. The duration matters because all 23 being Internal at 1 day is
        exactly what makes calculate_due_dates() a 23-day chain (B18)."""
        for row in TaskTemplateTask.objects.filter(phase__template=self._opex()):
            with self.subTest(task=row.label):
                self.assertEqual(row.task_type, 'Internal')
                self.assertEqual(row.duration_days, 1)


# ---------------------------------------------------------------------------
# 2 — corrected IN PLACE: there is no OPEX v2
# ---------------------------------------------------------------------------

class OpexIsStillOneVersionTests(TestCase):
    """R-7 freezes a LIVE version. This one never was — see migration 0075's header."""

    @classmethod
    def setUpTestData(cls):
        resolve_residential_template()
        _seed_opex()

    def test_there_is_exactly_one_opex_version_and_it_is_v1_active(self):
        versions = list(TaskTemplate.objects.filter(code=OPEX_CODE))
        self.assertEqual(
            [(v.version_no, v.status) for v in versions],
            [(1, TaskTemplate.ACTIVE)],
            'A second OPEX version exists. Prompt 1.5 corrected migration 0075 in '
            'place because 0074/0075 had never been deployed — origin/main was at '
            '0064. If that has changed, R-7 applies again and a v2 bump is right, but '
            'this test must be replaced deliberately rather than simply updated.')

    def test_the_partial_unique_still_permits_only_one_active_version(self):
        """The constraint that WOULD adjudicate a v2, proven still live."""
        from django.db import IntegrityError, transaction
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                TaskTemplate.objects.create(
                    code=OPEX_CODE, label='OPEX Execution', project_type='OPEX',
                    version_no=2, status=TaskTemplate.ACTIVE)

    def test_the_seed_is_idempotent(self):
        """Re-running 0075 over a database that already has an OPEX template leaves it
        exactly as it is — matched on code alone, so a future v2 is never clobbered."""
        _seed_opex()
        self.assertEqual(TaskTemplate.objects.filter(code=OPEX_CODE).count(), 1)
        self.assertEqual(
            TaskTemplateTask.objects.filter(phase__template__code=OPEX_CODE).count(), 23)

    def test_the_reverse_removes_opex_and_leaves_residential_alone(self):
        """The re-seed round trip is the documented local upgrade path: `migrate
        projects 0074` then `migrate`. If the reverse does not work, correcting the
        template in place stops being cheap and the v2 argument changes."""
        _unseed_opex()
        self.assertFalse(TaskTemplate.objects.filter(code=OPEX_CODE).exists())
        self.assertTrue(
            TaskTemplate.objects.filter(
                code=RESIDENTIAL_CODE, status=TaskTemplate.ACTIVE).exists(),
            'the OPEX reverse took RESIDENTIAL with it')

        _seed_opex()
        self.assertEqual(
            TaskTemplateTask.objects.filter(phase__template__code=OPEX_CODE).count(), 23)

    def test_residential_is_untouched(self):
        template = TaskTemplate.objects.get(
            code=RESIDENTIAL_CODE, status=TaskTemplate.ACTIVE)
        self.assertEqual(
            TaskTemplatePhase.objects.filter(template=template).count(), 9)
        self.assertEqual(
            TaskTemplateTask.objects.filter(phase__template=template).count(), 52)
        self.assertEqual(
            TaskTemplateTask.objects.filter(
                phase__template=template, is_mirror=True).count(), 0,
            'the Residential template has no mirrors and must not acquire one here')


# ---------------------------------------------------------------------------
# 3 — a freshly activated site gets the corrected table
# ---------------------------------------------------------------------------

class ActivatedSiteGetsTheCorrectedTableTests(OpexActivationBase):

    def test_activation_creates_twenty_three_tasks_and_eight_mirrors_by_name(self):
        self._activate()
        tasks = self._tasks(self.site)
        self.assertEqual(tasks.count(), 23)
        self.assertEqual(
            set(tasks.filter(is_mirror=True).values_list('task_name', flat=True)),
            EXPECTED_MIRROR_NAMES,
            'the seventh snapshot (is_mirror) is missing from the bulk_create, or the '
            'template changed shape without this list being updated')

    def test_the_mirrors_land_in_the_phases_the_spec_puts_them_in(self):
        self._activate()
        for phase_name, expected in EXPECTED_MIRRORS_BY_PHASE.items():
            with self.subTest(phase=phase_name):
                phase = ProjectPhase.objects.get(
                    project=self.site, phase_name=phase_name)
                self.assertEqual(
                    set(Task.objects.filter(phase=phase, is_mirror=True)
                        .values_list('task_name', flat=True)),
                    expected)

    def test_two_phases_hold_only_mirrors(self):
        self._activate()
        for phase_name in MIRROR_ONLY_PHASES:
            with self.subTest(phase=phase_name):
                phase = ProjectPhase.objects.get(
                    project=self.site, phase_name=phase_name)
                self.assertEqual(
                    Task.objects.filter(phase=phase, is_mirror=False).count(), 0,
                    f'{phase_name} has gained an entered task')


# ---------------------------------------------------------------------------
# 4 — the Payment Milestones card
# ---------------------------------------------------------------------------

class PaymentMilestoneCardTests(OpexActivationBase):
    """Both halves. The card is what the browser test saw; the endpoint is what a stale
    tab still reaches once the card is gone."""

    def _overview(self, project):
        return _client_for(self.pm).get(
            reverse('project_overview', args=[project.project_id]))

    def test_the_card_does_not_render_on_an_opex_site(self):
        self._activate()
        response = self._overview(self.site)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(
            response, MILESTONE_CARD_HEADING,
            msg_prefix='The Payment Milestones card is still rendering on an OPEX '
                       'site. The whole card goes, not just the Create button — a '
                       'card reading "No milestones yet" implies there should be some.')

    def test_the_create_button_is_gone_with_it(self):
        self._activate()
        response = self._overview(self.site)
        self.assertNotContains(response, 'Create M1 / M2 / M3')

    def test_the_card_still_renders_on_a_residential_project(self):
        """The counterweight. Hiding it everywhere would also pass the test above."""
        residential = self._make_residential()
        self._activate_residential(residential)
        response = self._overview(residential)
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response, MILESTONE_CARD_HEADING,
            msg_prefix='The card vanished from Residential too — the gate is on the '
                       'wrong condition.')

    def test_the_create_endpoint_refuses_an_opex_project(self):
        """A HIDDEN CONTROL IS NOT A DISABLED ONE. This endpoint is reachable by POST
        from a stale tab or curl regardless of what the template renders."""
        self._activate()
        response = _client_for(self.pm).post(
            reverse('milestone_create', args=[self.site.project_id]))
        self.assertEqual(
            PaymentMilestone.objects.filter(project=self.site).count(), 0,
            'milestone_create minted M1/M2/M3 on a tender site — the exact rows '
            'opex_site_activate refuses to create')
        self.assertIn(
            'Residential',
            ' '.join(str(m) for m in get_messages(response.wsgi_request)),
            'refused for the wrong reason, or silently')

    def test_the_create_endpoint_still_works_for_residential(self):
        """The guard must not have closed the door it exists to hold open."""
        residential = self._make_residential()
        self._activate_residential(residential)
        PaymentMilestone.objects.filter(project=residential).delete()

        _client_for(self.pm).post(
            reverse('milestone_create', args=[residential.project_id]))
        self.assertEqual(
            set(PaymentMilestone.objects.filter(project=residential)
                .values_list('milestone_name', flat=True)),
            {'M1', 'M2', 'M3'})


# ---------------------------------------------------------------------------
# 5 — the mirror marker
# ---------------------------------------------------------------------------

class MirrorMarkerTests(OpexActivationBase):

    def _overview(self, project):
        return _client_for(self.pm).get(
            reverse('project_overview', args=[project.project_id]))

    def test_the_marker_renders_on_exactly_the_eight_mirror_rows(self):
        self._activate()
        body = self._overview(self.site).content.decode()
        self.assertEqual(
            body.count(MIRROR_BADGE), 8,
            'the mirror marker is on the wrong number of rows — it must appear once '
            'per is_mirror task and nowhere else')

    def test_no_marker_on_a_residential_project(self):
        """Residential has no mirrors, so the badge must never appear there."""
        residential = self._make_residential()
        self._activate_residential(residential)
        body = self._overview(residential).content.decode()
        self.assertEqual(body.count(MIRROR_BADGE), 0)

    def test_the_status_control_is_not_rendered_for_a_mirror(self):
        """Unavailable rather than merely refusing on submit. The PM here is the
        assigned PM, so every one of the 15 entered rows gets a control and the 8
        mirrors get none — 15 is the number that proves the filter is on is_mirror and
        not on something coincidental."""
        self._activate()
        body = self._overview(self.site).content.decode()
        self.assertEqual(
            body.count('<select name="status"'), 15,
            'a status control is rendered for a mirror row (or missing from an '
            'entered one)')


# ---------------------------------------------------------------------------
# 6 — the marker is NOT the guard
# ---------------------------------------------------------------------------

class TheRefusalIsUnchangedTests(OpexActivationBase):
    """The premortem's second risk, re-checked after touching the template.

    The risk was that the read-only rule ends up implemented in the UI — hidden
    dropdowns — rather than in `_apply_task_status_change()`, looking identical in
    testing and failing the first time somebody posts directly. Prompt 1.5 hid the
    dropdown. This class is the proof that hiding it changed nothing underneath: every
    mirror is ASSIGNED first, so the refusal cannot come from the unassigned gate, and
    then posted to through both entry points.
    """

    def setUp(self):
        super().setUp()
        self._activate()
        self.mirrors = list(self._tasks(self.site).filter(is_mirror=True))
        self.assertEqual(len(self.mirrors), 8, 'fixture sanity')
        for mirror in self.mirrors:
            assign_task_to(mirror, self.pm, notify=False)

    def test_every_mirror_is_refused_through_both_entry_points(self):
        for entry in ('task_status_update', 'task_detail_status_update'):
            for mirror in self.mirrors:
                with self.subTest(entry=entry, task=mirror.task_name):
                    response = _client_for(self.pm).post(
                        reverse(entry, args=[self.site.project_id, mirror.pk]),
                        {'status': Task.DONE},
                    )
                    mirror.refresh_from_db()
                    self.assertEqual(
                        mirror.status, Task.NOT_STARTED,
                        f'{mirror.task_name} moved through {entry} — the server-side '
                        f'refusal has been weakened or removed')
                    self.assertIn(
                        'mirror task',
                        ' '.join(str(m) for m in get_messages(response.wsgi_request)),
                        f'{mirror.task_name}: refused for the wrong reason')

    def test_a_refused_move_writes_nothing(self):
        """Rung 0 sits above the inline due_date write, so a refusal leaves no trace."""
        mirror = self.mirrors[0]
        _client_for(self.pm).post(
            reverse('task_status_update', args=[self.site.project_id, mirror.pk]),
            {'status': Task.DONE},
        )
        self.assertEqual(
            StatusTransition.objects.filter(
                subject_type=SUBJECT_TASK, subject_id=mirror.pk).count(), 0,
            'a refused mirror move recorded a transition — a refusal is not an event')

    def test_an_entered_task_still_moves(self):
        """The other side: the guard must refuse mirrors, not everything.

        Without this, "mirrors are refused" and "this OPEX site is frozen" look
        identical from the outside — and hiding the status control for mirrors is
        exactly the kind of change that could freeze the entered rows by accident.

        A due_date rides along because `_apply_task_status_change()` refuses In
        Progress without one (views.py, "In Progress requires a due date"). That guard
        sits BELOW the mirror check, so it is not what refuses a mirror — the mirror
        test above asserts the message to prove which rung answered.
        """
        entered = self._tasks(self.site).filter(is_mirror=False).first()
        assign_task_to(entered, self.pm, notify=False)
        _client_for(self.pm).post(
            reverse('task_status_update', args=[self.site.project_id, entered.pk]),
            {'status': Task.IN_PROGRESS, 'due_date': '2026-12-31'},
        )
        entered.refresh_from_db()
        self.assertEqual(
            entered.status, Task.IN_PROGRESS,
            'the mirror refusal has leaked onto entered tasks — the OPEX site is '
            'frozen, not merely read-only where it should be')


# ---------------------------------------------------------------------------
# 7 — the phase progress bar counts mirrors
# ---------------------------------------------------------------------------

class MirrorsCountInThePhaseBarTests(OpexActivationBase):
    """The R-20 carve-out, from the OPEX side.

    `tests_mirror_metrics.py` pins the rule on a minimal hand-built fixture. This pins
    what it produces on the real template: Procurement & Delivery reads 0/4 rather than
    0/0, which is the whole reason the decision was taken.
    """

    def _phase_rows(self):
        response = _client_for(self.pm).get(
            reverse('project_overview', args=[self.site.project_id]))
        data = json.loads(response.context['phase_data_json'])
        by_pk = {p.pk: p.phase_name
                 for p in ProjectPhase.objects.filter(project=self.site)}
        return {by_pk[row['pk']]: row for row in data}

    def test_the_delivery_phase_reads_out_of_four_not_zero(self):
        self._activate()
        row = self._phase_rows()['Procurement & Delivery']
        self.assertEqual(
            (row['internal_done'], row['internal_total']), (0, 4),
            'the mirrors-only delivery phase is back to "0/0 done" — one mirror not '
            'updated is one task pending (prompt 1.5)')

    def test_the_design_phase_reads_out_of_one(self):
        self._activate()
        row = self._phase_rows()['Design']
        self.assertEqual((row['internal_done'], row['internal_total']), (0, 1))

    def test_a_mixed_phase_counts_both_kinds(self):
        """Closeout is 3 mirrors + 1 entered, so it must read out of 4."""
        self._activate()
        row = self._phase_rows()['Closeout']
        self.assertEqual(row['internal_total'], 4)

    def test_the_bar_agrees_with_the_card_header(self):
        """The defect this fixes, stated as an invariant: the card header prints
        `phase.tasks.count` and the bar prints `internal_total`. They disagreed on every
        phase holding a mirror, which is what made "0/0 done" above "4 tasks" possible.
        """
        self._activate()
        rows = self._phase_rows()
        for phase in ProjectPhase.objects.filter(project=self.site):
            with self.subTest(phase=phase.phase_name):
                self.assertEqual(
                    rows[phase.phase_name]['internal_total'],
                    phase.tasks.count(),
                    f'{phase.phase_name}: the progress bar and the card header are '
                    f'counting different things again')
