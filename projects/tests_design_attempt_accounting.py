"""B-06 — attempt accounting stops charging designers for PM-caused rework.

WHAT THIS FILE PINS
-------------------
The design attempt figures now say what their docstrings claim:

  (a) a designer-caused attempt raises rework; a PM-change attempt does not — asserted
      separately, never as one combined case;
  (b) a site reopened by a change request keeps first-pass, and a QC failure of any
      category still costs it (first-pass counts loops, not fault);
  (c) rework, input and PM change are three independent quotients — a fixture whose three
      numerators are 1, 2 and 4 fails if any code path adds any of them together;
  (d) a site awaiting PM approval is not current load, is counted as released nowhere, and
      stays in every finished-site denominator so no ratio inflates;
  (e) the named-site case, on a RECONSTRUCTION of the SCMPILOT pilot's shape.

(d) IS EXERCISED BY A FIXTURE. This file does not write the status itself: it borrows the
one reviewed fixture writer, `tests_design_pm_gate_fixtured.PmGateBase._park_in_pm_gate`.
The product route to `awaiting_pm_approval` is tests_design_pm_gate_live's.

(e) IS A RECONSTRUCTION, NOT THE PILOT. The suite runs on an empty SQLite database, so the
five released SCMPILOT sites and the pilot's one in-flight site are rebuilt here in the
shape the local dump showed on 10 Sep 2026 (A8): one designer, five released sites with a
single initial attempt each, one unreleased site with a single initial attempt. The real
check against the dump is VERIFY 3 in the session report, not this test.

Authority: B-06 (product owner), DESIGN_APPROVAL_AUDIT.md finding 3, and
EXECUTION_MODULE_DEFERRED.md §D1.
"""
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from .design_analytics import CORE_METRICS, OPTIONAL_METRICS, compute
from .design_metrics import attempt_cause_split, rework_contribution, tender_metrics
from .models import (
    DesignAssignment, DesignAttempt, Program, Project,
    ATTEMPT_REASON_INITIAL, ATTEMPT_REASON_QC_FAILED, ATTEMPT_REASON_PM_CHANGE_REQUEST,
    DESIGN_IN_DESIGN, DESIGN_RELEASED,
    ERR_LAYOUT, ERR_REQUIREMENT_CHANGED, ERR_SURVEY_INADEQUATE,
    QC_FAILED,
)
# The ONE fixture writer of the PM-approval status, reused rather than duplicated.
from .tests_design_pm_gate_fixtured import PmGateBase


def _profile(username, role, first_name='', last_name=''):
    """A post_save signal auto-creates the UserProfile; fetch and set, never create."""
    user = User.objects.create_user(username=username, password='x',
                                    first_name=first_name, last_name=last_name)
    profile = user.profile
    profile.role = role
    profile.is_active = True
    profile.save()
    return profile


class AccountingBase(TestCase):

    designer_username = 'acc_des'
    designer_names = ('', '')
    program_name = 'Test-B06'

    def setUp(self):
        self.head = _profile('acc_head', 'Design')
        self.head.is_design_head = True
        self.head.save()
        self.designer = _profile(self.designer_username, 'Design', *self.designer_names)
        self.pm = _profile('acc_pm', 'PM')
        self.program = Program.objects.create(
            name=self.program_name, program_type='OPEX', client_name='B06',
            status='Active', short_tender_code='B06')

    # ── fixtures ────────────────────────────────────────────────────────────

    def _site(self, code, later=(), released=True):
        """A site whose attempts after the first were opened by `later`.

        `later` is a list of (category, opened_reason). As in the Part 9 fixture, a QC
        failure's category is written on the attempt that FAILED, one row before the attempt
        it opened — which is what classify_attempt_causes() reconstructs.
        """
        site = Project(
            project_id=code, customer_name='B06', customer_phone='9876543210',
            site_address='1 Sun Rd', city='Delhi', project_type='OPEX',
            program=self.program, site_code=code, assigned_pm=self.pm,
            dc_capacity_kw=Decimal('100.00'), status='Draft')
        site.save()
        a = DesignAssignment.objects.create(
            project=site, status=DESIGN_IN_DESIGN, assigned_to=self.designer,
            assigned_by=self.head, assigned_at=timezone.now(),
            survey_file_bucket='b', survey_file_path=f'{code}/survey/x.pdf')
        attempts = [DesignAttempt.objects.create(
            assignment=a, attempt_number=1, opened_reason=ATTEMPT_REASON_INITIAL)]
        for category, reason in later:
            previous = attempts[-1]
            if reason == ATTEMPT_REASON_QC_FAILED:
                previous.qc_verdict = QC_FAILED
                previous.qc_remarks = 'failed'
                previous.qc_failure_category = category
            previous.closed_at = timezone.now()
            previous.save()
            attempts.append(DesignAttempt.objects.create(
                assignment=a, attempt_number=previous.attempt_number + 1,
                opened_reason=reason))
        a.current_attempt_number = len(attempts)
        if released:
            a.status = DESIGN_RELEASED
            a.released_at = timezone.now()
            a.released_by = self.head
        a.save()
        return a

    def _clean(self, prefix, n):
        return [self._site(f'{prefix}{i}') for i in range(1, n + 1)]

    def _park(self, assignment):
        """Move a fixture site into the PM gate through the ONE admitted writer."""
        return PmGateBase._park_in_pm_gate(self, assignment)

    # ── readers ─────────────────────────────────────────────────────────────

    def _metrics(self):
        return tender_metrics(self.program)

    def _workload(self, metrics=None):
        metrics = metrics or self._metrics()
        return next(w for w in metrics['workload'] if w['designer'].pk == self.designer.pk)

    def _analytics(self):
        return compute([self.program], set(CORE_METRICS) | set(OPTIONAL_METRICS))

    @staticmethod
    def _panel(result, key):
        return next(p['data'] for p in result['panels'] if p['metric'].key == key)

    def _row(self, panel_data, label):
        return next(r for r in panel_data['rows'] if r['label'] == label)

    @property
    def _label(self):
        return self.designer.user.get_full_name() or self.designer.user.username


# ===========================================================================
# (a) Designer-caused raises rework; PM-caused does not. Separately.
# ===========================================================================

class ReworkCauseTests(AccountingBase):

    def test_a1_a_designer_caused_attempt_raises_rework(self):
        self._clean('ACC-A', 4)
        self._site('ACC-A5', [(ERR_LAYOUT, ATTEMPT_REASON_QC_FAILED)])
        row = self._workload()
        self.assertEqual(row['rework_attempts'], 1)
        self.assertEqual(row['finished'], 5)
        self.assertEqual(row['rework'], 0.2)
        # The analytics panel reads the same implementation (B-06 Option A).
        figure = self._row(self._panel(self._analytics(), 'rework_multiplier'),
                           self._label)['figure']
        self.assertEqual((figure['numerator'], figure['n'], figure['value']), (1, 5, 0.2))

    def test_a2_a_pm_change_attempt_does_not_raise_rework(self):
        self._clean('ACC-P', 4)
        self._site('ACC-P5', [('', ATTEMPT_REASON_PM_CHANGE_REQUEST)])
        row = self._workload()
        self.assertEqual(row['rework_attempts'], 0)
        self.assertEqual(row['rework'], 0.0)
        # It is counted — in its own figure and on the chip.
        self.assertEqual(row['pm_change_multiplier'], 0.2)
        self.assertEqual(row['pm_change_request'], 1)
        figure = self._row(self._panel(self._analytics(), 'rework_multiplier'),
                           self._label)['figure']
        self.assertEqual((figure['numerator'], figure['value']), (0, 0.0))

    def test_a3_an_uncategorised_qc_failure_still_counts_as_designer_rework(self):
        """B-06 Stop 1 Q2: a loop that happened is not forgotten for want of a category."""
        self._clean('ACC-U', 4)
        self._site('ACC-U5', [('', ATTEMPT_REASON_QC_FAILED)])
        row = self._workload()
        self.assertEqual(row['uncategorised_attempts'], 1)
        self.assertEqual(row['designer_error_attempts'], 0)
        self.assertEqual(row['rework'], 0.2)

    def test_a4_the_initial_attempt_is_not_rework(self):
        self._clean('ACC-I', 5)
        row = self._workload()
        self.assertEqual(row['attempts'], 5)
        self.assertEqual(row['rework'], 0.0)


# ===========================================================================
# (b) First-pass counts loops, not fault
# ===========================================================================

class FirstPassTests(AccountingBase):

    def test_b1_a_site_reopened_by_a_change_request_keeps_first_pass(self):
        self._clean('ACC-F', 4)
        reopened = self._site('ACC-F5', [('', ATTEMPT_REASON_PM_CHANGE_REQUEST),
                                         ('', ATTEMPT_REASON_PM_CHANGE_REQUEST)])
        self.assertEqual(reopened.current_attempt_number, 3)
        figure = self._row(self._panel(self._analytics(), 'first_pass_rate'),
                           self._label)['figure']
        self.assertEqual((figure['numerator'], figure['n'], figure['value']), (5, 5, 100.0))

    def test_b2_a_qc_failure_costs_first_pass_whatever_its_category(self):
        self._clean('ACC-G', 3)
        self._site('ACC-G4', [(ERR_LAYOUT, ATTEMPT_REASON_QC_FAILED)])            # Group A
        self._site('ACC-G5', [(ERR_SURVEY_INADEQUATE, ATTEMPT_REASON_QC_FAILED)])  # Group B
        figure = self._row(self._panel(self._analytics(), 'first_pass_rate'),
                           self._label)['figure']
        self.assertEqual((figure['numerator'], figure['n']), (3, 5))


# ===========================================================================
# (c) Three figures, computed independently, never summed
# ===========================================================================

class NeverSummedTests(AccountingBase):
    """ONE finished site whose three numerators are 1, 2 and 4. Powers of two, because then
    every sum of two or three of them (3, 5, 6, 7) differs from every one of them. A code
    path that adds any figure into another cannot produce all three exact values."""

    def setUp(self):
        super().setUp()
        self.a = self._site('ACC-SUM', [
            (ERR_LAYOUT,              ATTEMPT_REASON_QC_FAILED),   # attempt 2: Group A
            (ERR_SURVEY_INADEQUATE,   ATTEMPT_REASON_QC_FAILED),   # attempt 3: Group B
            (ERR_REQUIREMENT_CHANGED, ATTEMPT_REASON_QC_FAILED),   # attempt 4: Group C
            ('', ATTEMPT_REASON_PM_CHANGE_REQUEST),                # attempts 5-8: PM change
            ('', ATTEMPT_REASON_PM_CHANGE_REQUEST),
            ('', ATTEMPT_REASON_PM_CHANGE_REQUEST),
            ('', ATTEMPT_REASON_PM_CHANGE_REQUEST),
        ])

    def test_c1_each_figure_is_its_own_bucket(self):
        row = self._workload()
        self.assertEqual(row['finished'], 1)
        self.assertEqual(row['rework'], 1.0)
        self.assertEqual(row['input_quality'], 2.0)
        self.assertEqual(row['pm_change_multiplier'], 4.0)

    def test_c2_the_shared_implementation_keeps_them_apart_too(self):
        attempts = list(self.a.attempts.all())
        split = attempt_cause_split(attempts)
        self.assertEqual((split['initial'], split['designer'], split['input'],
                          split['pm_change']), (1, 1, 2, 4))
        contribution = rework_contribution({'attempts': attempts, 'finished': True})
        self.assertEqual((contribution['designer'], contribution['input'],
                          contribution['pm_change'], contribution['dropped']), (1, 2, 4, 1))
        # The partition is exhaustive: nothing counted twice, nothing lost.
        self.assertEqual(contribution['designer'] + contribution['input']
                         + contribution['pm_change'] + contribution['dropped'], len(attempts))

    def test_c3_the_analytics_numerator_is_the_designer_bucket_alone(self):
        row = self._row(self._panel(self._analytics(), 'rework_multiplier'), self._label)
        self.assertEqual(row['figure']['numerator'], 1)
        # `excluded` is a count of what the numerator left out (2 + 4), shown in its own
        # column. It is never divided and never compared with rework.
        self.assertEqual(row['excluded'], 6)


# ===========================================================================
# (d) A site awaiting PM approval — PARKED BY FIXTURE
# ===========================================================================

class AwaitingPmApprovalTests(AccountingBase):
    """Four released sites, one site parked in the PM gate (its attempt 2 opened by a
    Group A failure), and one site still in design.

    What each figure must do with the parked site:
      current load (sites / kW)        absent
      "N released", released_count,
        capacity throughput            absent — it is released nowhere
      rework / input / PM-change       in the denominator, and its attempts in the numerator
      first-pass, rework multiplier,
        change-request rate            in the finished-site denominator
      no_due_date                      absent — it owes no date
      stage                            its own tile, never 'released'
    """

    def setUp(self):
        super().setUp()
        self._clean('ACC-R', 4)
        self.parked = self._park(
            self._site('ACC-PM', [(ERR_LAYOUT, ATTEMPT_REASON_QC_FAILED)], released=False))
        self._site('ACC-LIVE', released=False)

    def test_d1_it_is_not_current_load_and_not_released(self):
        metrics = self._metrics()
        row = self._workload(metrics)
        self.assertEqual(row['sites'], 1, 'only ACC-LIVE is load')
        self.assertEqual(row['released'], 4)
        self.assertEqual(row['finished'], 5)
        stages = {s['key']: s['count'] for s in metrics['stages']}
        self.assertEqual(stages['awaiting_pm_approval'], 1)
        self.assertEqual(stages['released'], 4)
        # ACC-LIVE has no approved date and counts; the parked site has none and does not.
        self.assertEqual(metrics['no_due_date'], 1)

    def test_d2_it_stays_in_the_attempt_figures_denominator(self):
        row = self._workload()
        # 1 designer-caused attempt (the parked site's) over 5 finished sites. Had the site
        # dropped out of the bottom while its attempt stayed on top, this would be 1 / 4.
        self.assertEqual((row['rework_attempts'], row['finished']), (1, 5))
        result = self._analytics()
        figure = self._row(self._panel(result, 'rework_multiplier'), self._label)['figure']
        self.assertEqual((figure['numerator'], figure['n'], figure['value']), (1, 5, 0.2))

    def test_d3_first_pass_and_change_request_rate_count_it_as_finished(self):
        result = self._analytics()
        first = self._row(self._panel(result, 'first_pass_rate'), self._label)['figure']
        # In the denominator; not first-pass, because a QC failure reopened it.
        self.assertEqual((first['numerator'], first['n']), (4, 5))
        crr = self._panel(result, 'change_request_rate')
        pm_row = self._row(crr, self.pm.user.username)
        self.assertEqual((pm_row['finished'], pm_row['released']), (5, 4))
        self.assertEqual(pm_row['figure']['n'], 5)
        self.assertEqual(crr['team']['n'], 5)

    def test_d4_it_is_counted_as_released_nowhere(self):
        result = self._analytics()
        self.assertEqual(result['released_count'], 4)
        throughput = self._row(self._panel(result, 'capacity_throughput'), self._label)
        self.assertEqual(throughput['sites'], 4)


# ===========================================================================
# (e) The named-site case — a RECONSTRUCTION of the SCMPILOT pilot (see module docstring)
# ===========================================================================

class ScmPilotReconstructionTests(AccountingBase):
    """BEFORE (A8, local dump, current code at the time): demo.design ("Pilot Design") held
    SCMPILOT01-05 released, one initial attempt each, plus one in-flight site with one
    initial attempt. Rework 1.2 = 6 attempts / 5 released: five initial attempts plus the
    in-flight site's, none of them rework. Input 0.0, PM-change 0.0, first-pass 5/5.

    AFTER: rework 0.0 — the initial attempt is not rework (B1b), and the in-flight site's
    attempt is outside the figure (B1c). Every other figure is unchanged. The chip is
    unchanged too: 0 designer / 0 input / 0 PM change / 0 uncategorised."""

    designer_username = 'demo.design'
    designer_names = ('Pilot', 'Design')
    program_name = 'SCMPILOT SCM Pilot Walkthrough'

    def setUp(self):
        super().setUp()
        for i in range(1, 6):
            self._site(f'SCMPILOT0{i}')
        self._site('SCMPILOT06', released=False)

    def test_e1_the_tender_dashboard_figures(self):
        row = self._workload()
        self.assertEqual(row['released'], 5)
        self.assertEqual(row['finished'], 5)
        self.assertEqual(row['sites'], 1)
        self.assertEqual(row['rework'], 0.0)             # was 1.2
        self.assertEqual(row['input_quality'], 0.0)      # unchanged
        self.assertEqual(row['pm_change_multiplier'], 0.0)
        # The Split chip, unchanged, and the numerator is its designer bucket restricted
        # to finished sites. Both 0 here, and both are asserted.
        self.assertEqual((row['designer_error_attempts'], row['input_problem_attempts'],
                          row['pm_change_request'], row['uncategorised_attempts']),
                         (0, 0, 0, 0))
        self.assertEqual(row['rework_attempts'], 0)
        self.assertEqual(row['attempts'], 6)

    def test_e2_the_analytics_figures_agree_with_the_dashboard(self):
        result = self._analytics()
        first = self._row(self._panel(result, 'first_pass_rate'), 'Pilot Design')['figure']
        self.assertEqual((first['numerator'], first['n'], first['value']), (5, 5, 100.0))
        rework = self._row(self._panel(result, 'rework_multiplier'), 'Pilot Design')
        self.assertEqual((rework['figure']['numerator'], rework['figure']['n']), (0, 5))
        # Option A landed: one figure, one answer on both screens.
        self.assertEqual(rework['figure']['value'], self._workload()['rework'])
        self.assertEqual(result['released_count'], 5)
