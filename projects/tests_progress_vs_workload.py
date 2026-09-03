"""Prompt 1.6 — progress and workload are different questions, on a real site.

WHY THIS MODULE EXISTS. From 1.3b (30 Aug) to 1.5 (1 Sep) one rule governed every
task metric in the codebase: a metric excludes mirrors. 1.5 removed the exclusion
from `project_overview`'s per-phase bar so a delivery phase would read 0/4 rather
than 0/0 — right, because 0/4 tells a PM four deliveries are outstanding and 0/0
tells them nothing. But it changed ONE SCREEN. `dashboard_pm`'s percentage and the
CEO project cards went on computing the same project's completeness on a different
denominator, so one activated OPEX site reported itself out of 15 in two places and
out of 23 in a third.

THE RULE THAT REPLACED IT (R-20, both halves):

    "How much of this SITE is done"          -> mirrors IN.  A site is not
        finished because the humans finished; the deliveries and the design are
        part of the work. `utils.site_progress_tasks_q()`.

    "How much work does this PERSON or TEAM owe" -> mirrors OUT. A mirror is
        nobody's task; counting it against somebody attributes another team's
        queue to them. `utils.human_owned_tasks_q()` / `is_human_owned()`.

WHAT MAKES THIS MODULE DIFFERENT FROM `tests_mirror_metrics.py`. That module builds
its mirrors BY HAND — one non-mirror and six identical mirrors — because it is a
guard over counters and wants a fixture with no other moving parts. Every test here
runs against a site taken through the REAL `opex_site_activate` view: 23 tasks, 8 of
them mirrors, seeded by migration 0075's own function. The numbers below are the
template's, not a fixture's, so a template change moves them and a reader can check
them against docs/OPEX_task_template_spec.md v1.5 §3.

    23 tasks = 15 entered + 8 mirrors  (Design; the four Delivery rows; COD;
    As-Built Drawings; HOTO). All 23 are `task_type='Internal'`.

THE TEST THIS MODULE EXISTS TO CREATE is `PmAndOverviewAgreeTests` — the PM
dashboard's percentage and the project overview's phase bars, on one project, from
the same data at the same moment. Everything else is scaffolding around it.

WHAT IS DELIBERATELY NOT ASSERTED HERE. Nothing about which SCREEN draws which
number: no template changed in 1.6 and none needed to. These are context values.
"""
from datetime import date

from django.urls import reverse

from .models import Task
from .reports import build_user_status_rows
from .tests_opex_activation import OpexActivationBase, _client_for

# docs/OPEX_task_template_spec.md v1.5 §3, stated as three numbers rather than one
# so a failure says WHICH half moved. A fourth independent transcription; migration
# 0075 holds the first, tests_opex_template.py the second, tests_opex_activation.py
# the third.
OPEX_TOTAL_TASKS = 23
OPEX_MIRRORS = 8
OPEX_ENTERED_TASKS = OPEX_TOTAL_TASKS - OPEX_MIRRORS   # 15

# The three PM-role tasks that are NOT mirrors. `attach_opex_template()` pre-assigns
# exactly these to the site's PM and leaves COD and HOTO — PM-role MIRRORS — with
# assigned_to NULL, because an unassigned mirror is an accurate statement that the
# row is nobody's.
OPEX_PM_ENTERED_TASKS = 3


class ProgressVsWorkloadBase(OpexActivationBase):
    """One activated OPEX site, every task dated, so the due-date counters fire.

    `opex_site_activate` deliberately does NOT call `calculate_due_dates()` (B18 —
    every OPEX task carries `duration_days=1`, so the chain would put the whole
    tender portfolio overdue within a month). Every task therefore lands with
    `due_date` NULL, and a due-date counter over an untouched site reads 0 whether
    or not it excludes mirrors — a green test proving nothing. The fixture dates
    them by hand for exactly that reason, and dates them ALL IDENTICALLY so the only
    thing that can separate two counts is the mirror flag.
    """

    def setUp(self):
        super().setUp()
        self._activate()
        Task.objects.filter(phase__project=self.site).update(due_date=date.today())
        self.client = _client_for(self.pm)

    def _site_tasks(self):
        return Task.objects.filter(phase__project=self.site)

    def _complete(self, n):
        """Mark `n` ENTERED tasks Done, by direct update.

        Not through `_apply_task_status_change()`: that helper reads `request.POST`
        and writes `messages`, so it has no non-HTTP caller (deferred §B item 6),
        and every path that could reach it refuses a mirror anyway (R-18). What
        matters to these tests is the STATUS on the row, not how it got there.
        """
        pks = list(self._site_tasks().filter(is_mirror=False)
                   .order_by('phase__phase_order', 'task_order')
                   .values_list('pk', flat=True)[:n])
        Task.objects.filter(pk__in=pks).update(status=Task.DONE)
        return pks

    def _pm_card(self):
        resp = self.client.get(reverse('dashboard_pm'))
        self.assertEqual(resp.status_code, 200)
        rows = [r for r in resp.context['projects_with_progress']
                if r['project'].pk == self.site.pk]
        self.assertEqual(len(rows), 1, 'the OPEX site is missing from the PM dashboard')
        return rows[0]

    def _overview_phases(self):
        import json
        resp = self.client.get(
            reverse('project_overview', args=[self.site.project_id]))
        self.assertEqual(resp.status_code, 200)
        return json.loads(resp.context['phase_data_json'])

    def _ceo_card(self):
        ceo = _profile_ceo()
        resp = _client_for(ceo).get(reverse('dashboard_ceo'))
        self.assertEqual(resp.status_code, 200)
        cards = [c for c in resp.context['project_cards']
                 if c['project'].pk == self.site.pk]
        self.assertEqual(len(cards), 1, 'the OPEX site is missing from the CEO cards')
        return cards[0], resp.context


def _profile_ceo():
    """A CEO, created lazily per test rather than in setUpTestData.

    `dashboard_ceo` is the only view here needing a role the OPEX fixture does not
    already carry, and creating it in the shared class data would put a CEO profile
    into every test in the module including the ones counting user rows.
    """
    from .tests_opex_activation import _profile
    from .models import UserProfile
    existing = UserProfile.objects.filter(role='CEO').first()
    return existing or _profile('pvw_ceo', 'CEO')


# ---------------------------------------------------------------------------
# 1 — Every PROGRESS number counts all 23
# ---------------------------------------------------------------------------

class ProgressCountsEveryTaskTests(ProgressVsWorkloadBase):
    """A site is not finished because the humans finished."""

    def test_pm_card_total_and_internal_total_count_all_twenty_three(self):
        row = self._pm_card()
        self.assertEqual(
            (row['total_tasks'], row['internal_total']),
            (OPEX_TOTAL_TASKS, OPEX_TOTAL_TASKS),
            'The PM card is measuring this site out of its ENTERED tasks. It asks '
            '"how much of this site is done", so the eight mirrors belong in the '
            'denominator: four undelivered consignments are outstanding work on the '
            'site whoever records them. Route it through '
            'utils.site_progress_tasks_q(), not human_owned_tasks_q().'
        )

    def test_pm_card_done_moves_with_completed_work_and_stays_out_of_23(self):
        self._complete(5)
        row = self._pm_card()
        self.assertEqual(row['done_tasks'], 5)
        self.assertEqual(row['internal_done'], 5)
        self.assertEqual(row['internal_total'], OPEX_TOTAL_TASKS)
        self.assertEqual(row['internal_percent'], int(5 / OPEX_TOTAL_TASKS * 100))

    def test_overview_phase_bars_sum_to_all_twenty_three(self):
        self.assertEqual(
            sum(p['internal_total'] for p in self._overview_phases()),
            OPEX_TOTAL_TASKS,
            'The per-phase bars no longer account for every task on the site. This '
            'is the metric prompt 1.5 fixed: a phase holding only mirrors read '
            '"0/0 done" above an empty bar while its own card header said "4 tasks".'
        )

    def test_the_all_mirror_delivery_phase_reads_out_of_four(self):
        """The screen that drove the 1.5 decision. Procurement & Delivery holds
        four tasks and every one is a mirror."""
        phases = {p['pk']: p for p in self._overview_phases()}
        from .models import ProjectPhase
        delivery = ProjectPhase.objects.get(
            project=self.site, phase_name='Procurement & Delivery')
        row = phases[delivery.pk]
        self.assertEqual(
            (row['internal_done'], row['internal_total']), (0, 4),
            'Procurement & Delivery reads 0/0 again. Four deliveries outstanding is '
            'four tasks pending; 0/0 tells the PM nothing.'
        )

    def test_ceo_card_pending_plus_completed_covers_all_twenty_three(self):
        card, _ = self._ceo_card()
        self.assertEqual(card['pending'] + card['completed'], OPEX_TOTAL_TASKS)


# ---------------------------------------------------------------------------
# 2 — Every WORKLOAD number counts only the 15 entered
# ---------------------------------------------------------------------------

class WorkloadCountsOnlyEnteredTasksTests(ProgressVsWorkloadBase):
    """A mirror is nobody's task, so it is nobody's workload."""

    def test_ceo_department_aggregate_total_is_fifteen(self):
        """QUERY 2's base — the one the 18 dept_* counts ride on.

        A SEPARATE QUERYSET from QUERY 1's card pair above, which is the whole
        reason one could move to PROGRESS while this stayed WORKLOAD.
        """
        _, ctx = self._ceo_card()
        self.assertEqual(
            ctx['task_total'], OPEX_ENTERED_TASKS,
            'The CEO department aggregate is counting mirrors. These 18 dept_* '
            'counts match on assigned_role with NO assignment term, so they are '
            'the one place a mirror inflates a department whether or not anybody '
            'is assigned to it.'
        )

    def test_pm_pending_approvals_counts_three_not_five(self):
        """PM-role tasks on an OPEX site are three entered plus two mirrors (COD,
        HOTO). The card matches on assigned_role with no assignment term, so it is
        where the two mirrors would land whether or not anyone owned them."""
        resp = self.client.get(reverse('dashboard_pm'))
        self.assertEqual(
            resp.context['summary']['pending_approvals'], OPEX_PM_ENTERED_TASKS,
            'COD and HOTO are on the PM\'s pending-approvals card. No PM can act '
            'on either — their status is derived from another object.'
        )

    def test_pm_due_today_counts_fifteen(self):
        resp = self.client.get(reverse('dashboard_pm'))
        self.assertEqual(resp.context['summary']['due_today'], OPEX_ENTERED_TASKS)
        self.assertEqual(resp.context['summary']['tasks_due_today'], OPEX_ENTERED_TASKS)

    def test_the_drill_down_list_matches_the_card_it_hangs_off(self):
        """A list that itemises a metric must reconcile with it, or the page
        contradicts itself."""
        resp = self.client.get(reverse('tasks_due_today'))
        self.assertEqual(resp.status_code, 200)
        listed = [t for g in resp.context['groups'] for t in g['tasks']
                  if t.phase.project_id == self.site.pk]
        self.assertEqual(len(listed), OPEX_ENTERED_TASKS)
        self.assertEqual([t for t in listed if t.is_mirror], [])

    def test_the_pm_row_on_the_user_status_report_counts_three(self):
        """`attach_opex_template()` pre-assigns the three entered PM tasks and
        leaves COD and HOTO unassigned, so this passes for two reasons at once —
        assert the mirror half explicitly by assigning them."""
        self._site_tasks().filter(is_mirror=True).update(assigned_to=self.pm)
        rows = [r for r in build_user_status_rows(date.today())['rows']
                if r['profile'].pk == self.pm.pk]
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            rows[0]['tasks_assigned'], OPEX_PM_ENTERED_TASKS,
            'Eight mirrors assigned to this PM landed in their row on the per-user '
            'status report. Their status is written by another team\'s object.'
        )

    def test_the_eod_digest_company_total_excludes_mirrors(self):
        """Also a send gate: a mirror here can flip somebody from "no digest" to
        "digest" and email them about work they cannot do.

        This counter has an `assigned_to__isnull=False` term, and the OPEX attach
        assigns only the three entered PM tasks — so assign the eight mirrors to a
        real person first or the test passes because nothing owns them.
        """
        from projects.management.commands.send_eod_digest import Command
        before = Command()._company_totals(date.today())['assigned']
        self.assertEqual(before, OPEX_PM_ENTERED_TASKS)
        self._site_tasks().filter(is_mirror=True).update(assigned_to=self.pm)
        self.assertEqual(
            Command()._company_totals(date.today())['assigned'], before,
            'Eight mirrors handed to a real person moved the company open-task '
            'total. This number also gates whether the digest sends at all.')


# ---------------------------------------------------------------------------
# 3 — The CEO card invariant: the two terms move together
# ---------------------------------------------------------------------------

class CeoCardInvariantTests(ProgressVsWorkloadBase):
    """Pending + Completed == the project's task total, whichever category the
    pair landed in.

    It holds by CONSTRUCTION today — `pending` is `task_total_count -
    task_done_count`, not a queried count — and that is exactly why it needs a
    test: the construction is one line and a later session that "simplifies"
    pending into its own Count(filter=~Q(status=DONE)) breaks the partition and
    the join fan-out in the same edit.
    """

    def _assert_invariant(self):
        card, _ = self._ceo_card()
        self.assertEqual(
            card['pending'] + card['completed'],
            self._site_tasks().count(),
            'Pending + Completed no longer equals the project\'s task count. The '
            'two terms are one partition of one project and must move together — '
            'if one filter changed, the other must too.'
        )

    def test_invariant_holds_with_nothing_done(self):
        self._assert_invariant()

    def test_invariant_holds_part_way_through(self):
        self._complete(7)
        self._assert_invariant()

    def test_invariant_holds_with_every_entered_task_done(self):
        """The end state of a real OPEX site under today's code: all 15 entered
        tasks Done, all 8 mirrors still open because no derivation hook exists to
        close them. The card reads 8 pending / 15 completed — a TRUE statement
        about the site, and the accepted cost of the PROGRESS half of R-20."""
        self._complete(OPEX_ENTERED_TASKS)
        self._assert_invariant()
        card, _ = self._ceo_card()
        self.assertEqual((card['pending'], card['completed']),
                         (OPEX_MIRRORS, OPEX_ENTERED_TASKS))


# ---------------------------------------------------------------------------
# 4 — The CEO report's row-sum invariant is a WORKLOAD number and did not move
# ---------------------------------------------------------------------------

class UserStatusRowSumUnmovedTests(ProgressVsWorkloadBase):
    """not_started + in_progress + completed + blocked == tasks_assigned.

    The existing guard against a join fan-out on `build_user_status_rows()`. 1.6
    touched no queryset in reports.py, and this asserts that on a real site with
    eight mirrors assigned to a real person — the shape most likely to break it.
    """

    def test_row_sum_holds_on_an_opex_site_with_mirrors_assigned(self):
        self._site_tasks().filter(is_mirror=True).update(assigned_to=self.pm)
        self._complete(4)
        # Spread the remainder across the other three status columns — the
        # invariant is that the FOUR partition tasks_assigned, and a fixture
        # sitting in two of them cannot show that.
        spread = list(self._site_tasks().filter(
            is_mirror=False, status=Task.NOT_STARTED
        ).values_list('pk', flat=True)[:4])
        Task.objects.filter(pk__in=spread[:2]).update(status=Task.IN_PROGRESS)
        Task.objects.filter(pk__in=spread[2:]).update(status=Task.BLOCKED)
        report = build_user_status_rows(date.today())
        for row in report['rows'] + [report['totals']]:
            self.assertEqual(
                row['not_started'] + row['in_progress']
                + row['completed'] + row['blocked'],
                row['tasks_assigned'],
                'The four status columns no longer partition tasks_assigned. With '
                'mirrors present that means the exclusion reached some columns and '
                'not others, or a join fanned rows out.'
            )

    def test_the_magnitude_did_not_move_either(self):
        """The invariant would also hold if mirrors were counted everywhere. Pin
        the number, not only the arithmetic."""
        self._site_tasks().filter(is_mirror=True).update(assigned_to=self.pm)
        rows = [r for r in build_user_status_rows(date.today())['rows']
                if r['profile'].pk == self.pm.pk]
        self.assertEqual(rows[0]['tasks_assigned'], OPEX_PM_ENTERED_TASKS)


# ---------------------------------------------------------------------------
# 5 — THE TEST THIS SESSION EXISTS TO CREATE
# ---------------------------------------------------------------------------

class PmAndOverviewAgreeTests(ProgressVsWorkloadBase):
    """`dashboard_pm`'s percentage and `project_overview`'s phase bars describe
    the same project and must not disagree.

    Both apply the same `task_type == 'Internal'` predicate — the PM card over the
    whole project, the overview per phase — and every task belongs to exactly one
    phase, so the phase bars must sum to the card. That identity was FALSE between
    1.5 and 1.6, by exactly the eight mirrors, on every OPEX site.

    Asserted in three states, because agreeing at 0% and disagreeing later is the
    failure a single-state test would miss.
    """

    def _assert_agreement(self, label):
        row = self._pm_card()
        phases = self._overview_phases()
        self.assertEqual(
            (sum(p['internal_total'] for p in phases),
             sum(p['internal_done'] for p in phases)),
            (row['internal_total'], row['internal_done']),
            f'\n\n{label}: the PM dashboard and the project overview report this '
            'project\'s completeness from different denominators. Both ask "how '
            'much of this site is done" and both filter task_type=Internal, so the '
            'phase bars must sum to the card. If they differ by the mirror count, '
            'one of the two is using utils.human_owned_tasks_q() where it should '
            'use utils.site_progress_tasks_q().'
        )

    def test_they_agree_before_any_work(self):
        self._assert_agreement('nothing done')

    def test_they_agree_part_way_through(self):
        self._complete(6)
        self._assert_agreement('6 of 15 entered tasks done')

    def test_they_agree_when_every_entered_task_is_done(self):
        self._complete(OPEX_ENTERED_TASKS)
        self._assert_agreement('all 15 entered tasks done')
        row = self._pm_card()
        self.assertEqual(
            (row['internal_done'], row['internal_total']),
            (OPEX_ENTERED_TASKS, OPEX_TOTAL_TASKS),
            'A site with four undelivered consignments reads 100% complete.'
        )

    def test_the_disagreement_would_be_exactly_the_mirror_count(self):
        """Names the size of the regression so a failure is diagnosable rather
        than merely red."""
        row = self._pm_card()
        human_only = self._site_tasks().filter(
            is_mirror=False, task_type=Task.INTERNAL).count()
        self.assertEqual(row['internal_total'] - human_only, OPEX_MIRRORS)


# ---------------------------------------------------------------------------
# 6 — Residential has no mirrors, so both categories give the same answer
# ---------------------------------------------------------------------------

class ResidentialIsUnaffectedTests(OpexActivationBase):
    """The regression half. `attach_residential_template()` copies `is_mirror`
    from the template task, and no RESIDENTIAL template row carries it — so on a
    Residential project the two helpers are the same predicate over the same rows
    and 1.6 cannot have moved a number.

    Asserted rather than argued, because "it has no mirrors" is a property of
    seeded data that a later template edit can quietly change.
    """

    def setUp(self):
        super().setUp()
        self.resi = self._make_residential()
        self._activate_residential(self.resi)
        self.client = _client_for(self.pm)

    def _resi_tasks(self):
        return Task.objects.filter(phase__project=self.resi)

    def test_the_residential_template_still_carries_no_mirror(self):
        self.assertEqual(self._resi_tasks().filter(is_mirror=True).count(), 0)
        self.assertGreater(self._resi_tasks().count(), 0)

    def test_progress_and_workload_denominators_are_identical(self):
        resp = self.client.get(reverse('dashboard_pm'))
        row = [r for r in resp.context['projects_with_progress']
               if r['project'].pk == self.resi.pk][0]
        self.assertEqual(row['total_tasks'], self._resi_tasks().count())
        self.assertEqual(
            row['internal_total'],
            self._resi_tasks().filter(task_type=Task.INTERNAL).count())
        # ... and the workload form of the same two counts agrees, which is the
        # actual claim: on this project the split makes no difference.
        self.assertEqual(
            row['total_tasks'],
            self._resi_tasks().filter(is_mirror=False).count())

    def test_the_phase_bars_still_sum_to_the_pm_card(self):
        import json
        resp = self.client.get(
            reverse('project_overview', args=[self.resi.project_id]))
        phases = json.loads(resp.context['phase_data_json'])
        pm = self.client.get(reverse('dashboard_pm'))
        row = [r for r in pm.context['projects_with_progress']
               if r['project'].pk == self.resi.pk][0]
        self.assertEqual(sum(p['internal_total'] for p in phases),
                         row['internal_total'])

    def test_external_pending_still_excludes_nothing_it_should_not(self):
        """`ext_pending` in the overview loop went back to WORKLOAD in 1.6 (1.5
        had swept it in with the two bar numbers by changing one binding three
        outputs read). Residential is where it is actually exercised — it is the
        only template with External tasks — and the count must be unchanged."""
        import json
        resp = self.client.get(
            reverse('project_overview', args=[self.resi.project_id]))
        phases = json.loads(resp.context['phase_data_json'])
        self.assertEqual(
            sum(p['ext_pending'] for p in phases),
            self._resi_tasks().filter(task_type=Task.EXTERNAL)
            .exclude(status=Task.DONE).count())
