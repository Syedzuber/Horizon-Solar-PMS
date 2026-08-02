"""
Part 10 verification — the Design Head's quality analytics.

WHY THIS FILE EXISTS
--------------------
Part 10 adds no workflow. It adds arithmetic over other parts' rows and one stored
preference, and almost everything that can go wrong with it is invisible from the screen:

  * a rate over a denominator of two renders as a plausible percentage and is then
    repeated in a review as fact — so every rate is pinned against the minimum-denominator
    rule, and one designer with fewer than five released sites is asserted to produce no
    percentage anywhere on the page;
  * a Group B or C failure folded into a designer figure is the exact unfairness the whole
    A/B/C split exists to prevent, and nothing on screen would say it had happened — so a
    Group B failure and a Group C failure are created and asserted ABSENT from the designer
    numbers and PRESENT in their own;
  * an accepted change request charged to the designer rather than the PM who raised it is
    the same failure through a different door, and is pinned separately;
  * on-time delivery read from `is_current` rather than the approved commitment would let a
    designer clear their own late delivery by requesting an extension afterwards — so the
    late case is built with a pending extension that WOULD have made it on time;
  * and "read only" is a claim, not a fact, until row counts are taken on every design
    model before and after a full page load.

Every refusal is exercised BY DIRECT GET rather than by asserting a link is missing — a
missing navigation link is not a permission.

Numbered VERIFICATION comments map to the session brief's verification list.
"""
from decimal import Decimal
from datetime import date, timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .design_analytics import (
    CORE_METRICS, MIN_DENOMINATOR, METRIC_CATALOGUE, OPTIONAL_METRICS,
    compute, rate, ratio, selected_metric_keys,
)
from .models import (
    ActivityLog, ArkaSubmission, BOQ, BOQItem, DesignAnalyticsPreference,
    DesignAssignment, DesignAttempt, DesignChangeRequest, DesignFile,
    DueDateCommitment, NotificationLog, Program, Project, Task, UserProfile,
    ARKA_APPROVED, ARKA_PENDING, ARKA_REJECTED,
    ATTEMPT_REASON_INITIAL, ATTEMPT_REASON_QC_FAILED, ATTEMPT_REASON_PM_CHANGE_REQUEST,
    CHANGE_REQUEST_ACCEPTED, CHANGE_REQUEST_REJECTED,
    DESIGN_IN_DESIGN, DESIGN_RELEASED, DESIGN_SURVEY_RETURNED,
    ERR_BOQ_QUANTITY, ERR_ELECTRICAL_DESIGN, ERR_LAYOUT,
    ERR_REQUIREMENT_CHANGED, ERR_SURVEY_INADEQUATE,
    QC_FAILED, QC_PASSED, QC_PENDING,
    error_category_group,
)


def _profile(username, role, is_design_head=False, is_design_qc=False):
    """A post_save signal auto-creates the UserProfile; fetch and set, never create."""
    user = User.objects.create_user(username=username, password='x')
    profile = user.profile
    profile.role = role
    profile.is_active = True
    profile.is_design_head = is_design_head
    profile.is_design_qc = is_design_qc
    profile.save()
    return profile


#: Every design model Part 10 reads. Row counts on all of them are taken before and after
#: a page load — see test_page_load_writes_no_workflow_row.
WORKFLOW_MODELS = (DesignAssignment, DesignAttempt, ArkaSubmission, DesignFile,
                   DueDateCommitment, DesignChangeRequest, ActivityLog)


class Part10Base(TestCase):
    """Two tenders, two designers with deliberately different sample sizes, two reviewers.

    THE SAMPLE SIZES ARE THE FIXTURE'S WHOLE POINT. `alpha` releases 6 sites, which clears
    the minimum denominator; `bravo` releases 2, which does not. Almost every assertion
    below depends on those two numbers straddling MIN_DENOMINATOR, so changing them will
    break tests that look unrelated.
    """

    def setUp(self):
        self.head     = _profile('head10',  'Design', is_design_head=True)
        self.qc       = _profile('qc10',    'Design', is_design_qc=True)
        self.qc2      = _profile('qc10b',   'Design', is_design_qc=True)
        self.alpha    = _profile('alpha10', 'Design')
        self.bravo    = _profile('bravo10', 'Design')
        self.pm       = _profile('pm10',    'PM')
        self.pm2      = _profile('pm10b',   'PM')
        self.scm      = _profile('scm10',   'SCM')
        self.se       = _profile('se10',    'Site Engineer')
        # The deputy is REFUSED this screen — see the note above design_quality_analytics.
        # Named on the Head's profile so the deputy predicate is genuinely true for them.
        self.deputy   = _profile('dep10',   'Design')
        self.head.design_head_deputy = self.deputy
        self.head.save()

        self.t1 = Program.objects.create(
            name='P10-Tender-One', program_type='OPEX', client_name='C1',
            status='Active', short_tender_code='P10A')
        self.t2 = Program.objects.create(
            name='P10-Tender-Two', program_type='OPEX', client_name='C2',
            status='Active', short_tender_code='P10B')

        self.now = timezone.now()
        self._build()

    # ── fixtures ────────────────────────────────────────────────────────────

    def _site(self, program, code, designer, pm, released=False,
              attempt_count=1, allocated_days_ago=40, released_days_ago=10):
        site = Project(
            project_id=code, customer_name='C', customer_phone='9876543210',
            site_address='1 Sun Rd', city='Delhi', project_type='OPEX',
            program=program, site_code=code, assigned_pm=pm,
            capacity_kw=Decimal('100.00'), status='Draft')
        site.save()
        assignment = DesignAssignment.objects.create(
            project=site,
            status=DESIGN_RELEASED if released else DESIGN_IN_DESIGN,
            assigned_to=designer,
            assigned_at=self.now - timedelta(days=allocated_days_ago),
            released_at=(self.now - timedelta(days=released_days_ago)) if released else None,
            current_attempt_number=attempt_count,
            survey_file_bucket='b', survey_file_path=f'{code}/survey/x.pdf')
        return site, assignment

    def _attempt(self, assignment, number, reason=ATTEMPT_REASON_INITIAL, **kw):
        return DesignAttempt.objects.create(
            assignment=assignment, attempt_number=number, opened_reason=reason, **kw)

    def _passed(self, assignment, number=1, reason=ATTEMPT_REASON_INITIAL,
                qc_by=None, overturned=False, head_verdict=QC_PASSED):
        """An attempt both gates ruled on. The shape every released site ends in."""
        return self._attempt(
            assignment, number, reason,
            qc_verdict=QC_PASSED, qc_remarks='ok',
            qc_reviewed_by=qc_by or self.qc,
            qc_started_at=self.now - timedelta(days=20),
            qc_reviewed_at=self.now - timedelta(days=18),
            head_verdict=head_verdict,
            head_remarks='ok' if head_verdict != QC_FAILED else 'head disagrees',
            head_reviewed_by=self.head,
            head_started_at=self.now - timedelta(days=17),
            head_reviewed_at=self.now - timedelta(days=15),
            head_overturned_qc=overturned)

    def _arka(self, attempt, version=1, verdict=ARKA_APPROVED,
              head_verdict=ARKA_APPROVED, submitted_by=None, is_current=True,
              qc_by=None, overturned=False, qc_category='', carried_from=None):
        return ArkaSubmission.objects.create(
            attempt=attempt, version=version, capacity_kw=Decimal('120.00'),
            arka_link='https://example.com/a', submitted_by=submitted_by or self.alpha,
            verdict=verdict,
            rejection_reason='no' if verdict == ARKA_REJECTED else '',
            qc_failure_category=qc_category,
            reviewed_by=qc_by or self.qc,
            reviewed_at=self.now - timedelta(days=25),
            head_verdict=head_verdict,
            head_rejection_reason='no' if head_verdict == ARKA_REJECTED else '',
            head_reviewed_by=self.head,
            head_reviewed_at=self.now - timedelta(days=24),
            head_overturned_qc=overturned,
            carried_forward_from=carried_from,
            is_current=is_current)

    def _due(self, assignment, proposed, approved=True, is_current=True, days_ago=30):
        return DueDateCommitment.objects.create(
            assignment=assignment, proposed_date=proposed,
            proposed_by=assignment.assigned_to,
            approved_by=self.head if approved else None,
            approved_at=(self.now - timedelta(days=days_ago)) if approved else None,
            change_reason='' if is_current else 'superseded',
            is_current=is_current)

    def _build(self):
        """The whole fixture, laid out so every expected figure is countable by hand.

        ALPHA — tender 1, 6 released sites:
            A1 A2 A3 A4   released on attempt 1        first-pass
            A5            released on attempt 2        not first-pass, Group A cause
            A6            released on attempt 2        not first-pass, GROUP B cause
          plus A7, unreleased, whose attempt 2 was opened by a GROUP C failure.
          -> first pass 4/6 · rework numerator excludes A6's and A7's second attempts

        BRAVO — tender 2, 2 released sites. Below MIN_DENOMINATOR on purpose.
        """
        self.alpha_sites, self.bravo_sites = [], []

        # ---- alpha, four clean first-pass releases ---------------------------
        for i in range(1, 5):
            site, a = self._site(self.t1, f'P10A-A{i}', self.alpha, self.pm,
                                 released=True, attempt_count=1)
            t = self._passed(a, 1, qc_by=self.qc)
            self._arka(t, 1)
            # Due 8 days ago, released 10 days ago — comfortably ON TIME, so the late case
            # built in test_on_time_delivery_uses_the_approved_date stands out.
            self._due(a, (self.now - timedelta(days=8)).date())
            self.alpha_sites.append((site, a, t))

        # ---- A5: two attempts, the first failed at QC with a GROUP A category --
        site5, a5 = self._site(self.t1, 'P10A-A5', self.alpha, self.pm,
                               released=True, attempt_count=2)
        t5a = self._attempt(a5, 1, qc_verdict=QC_FAILED, qc_remarks='layout wrong',
                            qc_failure_category=ERR_LAYOUT,
                            qc_reviewed_by=self.qc,
                            qc_started_at=self.now - timedelta(days=30),
                            qc_reviewed_at=self.now - timedelta(days=29))
        t5b = self._passed(a5, 2, reason=ATTEMPT_REASON_QC_FAILED, qc_by=self.qc)
        self._arka(t5a, 1, verdict=ARKA_APPROVED)
        self._arka(t5b, 1)
        self._due(a5, (self.now - timedelta(days=8)).date())
        self.alpha_sites.append((site5, a5, t5b))

        # ---- A6: two attempts, the first failed with a GROUP B category --------
        # THE KEY ROW FOR VERIFICATION 7. Attempt 2 exists because the SURVEY was
        # wrong, so it must not appear in alpha's rework numerator or in the error
        # distribution, and must appear in the Group B count.
        site6, a6 = self._site(self.t1, 'P10A-A6', self.alpha, self.pm,
                               released=True, attempt_count=2)
        t6a = self._attempt(a6, 1, qc_verdict=QC_FAILED, qc_remarks='survey no good',
                            qc_failure_category=ERR_SURVEY_INADEQUATE,
                            qc_reviewed_by=self.qc,
                            qc_started_at=self.now - timedelta(days=30),
                            qc_reviewed_at=self.now - timedelta(days=29))
        t6b = self._passed(a6, 2, reason=ATTEMPT_REASON_QC_FAILED, qc_by=self.qc)
        self._arka(t6a, 1)
        self._arka(t6b, 1)
        self._due(a6, (self.now - timedelta(days=8)).date())
        self.alpha_sites.append((site6, a6, t6b))
        self.site_group_b = site6

        # ---- A7: unreleased, attempt 2 opened by a GROUP C failure -------------
        # THE KEY ROW FOR VERIFICATION 8, together with the accepted change request
        # raised by pm2 below.
        site7, a7 = self._site(self.t1, 'P10A-A7', self.alpha, self.pm2,
                               released=False, attempt_count=2)
        t7a = self._attempt(a7, 1, qc_verdict=QC_FAILED, qc_remarks='brief moved',
                            qc_failure_category=ERR_REQUIREMENT_CHANGED,
                            qc_reviewed_by=self.qc,
                            qc_started_at=self.now - timedelta(days=26),
                            qc_reviewed_at=self.now - timedelta(days=25))
        t7b = self._attempt(a7, 2, reason=ATTEMPT_REASON_PM_CHANGE_REQUEST,
                            qc_started_at=self.now - timedelta(days=8))
        self._arka(t7a, 1)
        self.a7, self.t7a, self.t7b = a7, t7a, t7b

        # An ACCEPTED change request raised by pm2. Counted against pm2, never alpha.
        self.cr_accepted = DesignChangeRequest.objects.create(
            attempt=t7a, requested_by=self.pm2, reason='client changed the roof',
            verdict=CHANGE_REQUEST_ACCEPTED, decided_by=self.head,
            decided_at=self.now - timedelta(days=9), resulting_attempt=t7b)
        # And a rejected one, so the rejection rate has something to report.
        self.cr_rejected = DesignChangeRequest.objects.create(
            attempt=t7b, requested_by=self.pm2, reason='and again',
            verdict=CHANGE_REQUEST_REJECTED, decided_by=self.head,
            decided_at=self.now - timedelta(days=2),
            rejection_reason='out of scope for this tender')

        # ---- three MORE Group A failures, across two further categories --------
        # VERIFICATION 6 needs three DISTINCT Group A categories present. ERR_LAYOUT
        # already exists on t5a; these add ERR_BOQ_QUANTITY and ERR_ELECTRICAL_DESIGN.
        site8, a8 = self._site(self.t1, 'P10A-A8', self.alpha, self.pm,
                               released=False, attempt_count=2)
        self._attempt(a8, 1, qc_verdict=QC_FAILED, qc_remarks='boq qty',
                      qc_failure_category=ERR_BOQ_QUANTITY,
                      qc_reviewed_by=self.qc,
                      qc_started_at=self.now - timedelta(days=22),
                      qc_reviewed_at=self.now - timedelta(days=21))
        t8b = self._attempt(a8, 2, reason=ATTEMPT_REASON_QC_FAILED)
        # An Arka REJECTED at the QC gate carries its own Group A category. It is a
        # failure of the same kind through a different artifact, and the distribution
        # must count both — hence _failure_rows reads four fields, not two.
        self._arka(t8b, 1, verdict=ARKA_REJECTED, head_verdict=ARKA_PENDING,
                   qc_category=ERR_ELECTRICAL_DESIGN, is_current=True)
        self.a8 = a8

        # ---- alpha on Design Hold once, cleared ---------------------------------
        # survey_returned_at stays set after the hold is cleared — that is what makes
        # "ever held" answerable at all. Both log events exist so the duration metric
        # has one completed interval.
        site9, a9 = self._site(self.t1, 'P10A-A9', self.alpha, self.pm)
        a9.survey_returned_at = self.now - timedelta(days=35)
        a9.survey_returned_by = self.alpha
        a9.survey_return_reason = 'survey unreadable'
        a9.save()
        ActivityLog.objects.create(project=site9, actor=self.alpha,
                                   action='held', action_code='design_blocked')
        ActivityLog.objects.create(project=site9, actor=self.head,
                                   action='cleared',
                                   action_code='design_survey_unblocked')
        self.site_held = site9

        # ---- bravo, tender 2, TWO released sites — under the minimum -----------
        for i in range(1, 3):
            site, a = self._site(self.t2, f'P10B-B{i}', self.bravo, self.pm,
                                 released=True, attempt_count=1)
            t = self._passed(a, 1, qc_by=self.qc2)
            self._arka(t, 1, submitted_by=self.bravo, qc_by=self.qc2)
            self._due(a, (self.now - timedelta(days=8)).date())
            self.bravo_sites.append((site, a, t))

    # ── helpers ─────────────────────────────────────────────────────────────

    def _login(self, profile):
        self.assertTrue(self.client.login(username=profile.user.username, password='x'))

    def _panel(self, result, key):
        for p in result['panels']:
            if p['metric'].key == key:
                return p['data']
        return None

    def _all(self, selected=None):
        return compute([self.t1, self.t2],
                       selected or (set(CORE_METRICS) | set(OPTIONAL_METRICS)))

    def _t1(self, selected=None):
        return compute([self.t1], selected or (set(CORE_METRICS) | set(OPTIONAL_METRICS)))

    @staticmethod
    def _row(panel_data, label_fragment):
        for r in panel_data['rows']:
            if label_fragment in r['label']:
                return r
        return None


# ===========================================================================
# 1. The sample size guard
# ===========================================================================

class SampleSizeGuardTests(Part10Base):

    def test_rate_refuses_below_minimum(self):
        """VERIFICATION 4 (unit level): under 5, there is no number at all."""
        for n in range(0, MIN_DENOMINATOR):
            f = rate(1, n)
            self.assertIsNone(f['value'], f'n={n} produced a percentage')
            self.assertEqual(f['state'], 'insufficient')
            self.assertEqual(f['n'], n)

    def test_rate_marks_low_confidence_band(self):
        self.assertEqual(rate(1, 5)['state'], 'low')
        self.assertEqual(rate(1, 14)['state'], 'low')
        self.assertEqual(rate(1, 15)['state'], 'ok')
        self.assertEqual(rate(3, 6)['value'], 50.0)

    def test_ratio_obeys_the_same_threshold(self):
        self.assertIsNone(ratio(8, 4)['value'])
        self.assertEqual(ratio(8, 4)['n'], 4)
        self.assertEqual(ratio(8, 5)['value'], 1.6)

    def test_designer_under_the_minimum_shows_no_percentage_anywhere(self):
        """VERIFICATION 4 — with a REAL user, on the REAL page.

        bravo has 2 released sites. Every per-designer rate for bravo must render as
        `Insufficient data (n=…)` and never as a percentage.
        """
        result = self._all()
        for key in ('first_pass_rate', 'qc_failure_rate', 'head_failure_rate',
                    'on_time_delivery'):
            row = self._row(self._panel(result, key), 'bravo10')
            self.assertIsNotNone(row, f'bravo missing from {key}')
            self.assertEqual(row['figure']['state'], 'insufficient', key)
            self.assertIsNone(row['figure']['value'], key)
            self.assertLess(row['figure']['n'], MIN_DENOMINATOR, key)

        # And on the rendered page, as text.
        self._login(self.head)
        html = self.client.get(reverse('design_quality_analytics')).content.decode()
        self.assertIn('Insufficient data (n=2)', html)

    def test_alpha_clears_the_minimum_and_shows_a_figure(self):
        """The other side of the same rule — 6 released sites DOES produce a number."""
        row = self._row(self._panel(self._all(), 'first_pass_rate'), 'alpha10')
        self.assertEqual(row['figure']['n'], 6)
        self.assertEqual(row['figure']['state'], 'low')
        self.assertIsNotNone(row['figure']['value'])


# ===========================================================================
# 2. The metrics themselves
# ===========================================================================

class MetricArithmeticTests(Part10Base):

    def test_first_pass_rate_matches_a_manual_count(self):
        """VERIFICATION 5 — computed figure against a hand count of the rows."""
        manual_first = DesignAssignment.objects.filter(
            status=DESIGN_RELEASED, assigned_to=self.alpha,
            current_attempt_number=1).count()
        manual_released = DesignAssignment.objects.filter(
            status=DESIGN_RELEASED, assigned_to=self.alpha).count()
        self.assertEqual((manual_first, manual_released), (4, 6))

        row = self._row(self._panel(self._all(), 'first_pass_rate'), 'alpha10')
        self.assertEqual(row['figure']['numerator'], manual_first)
        self.assertEqual(row['figure']['n'], manual_released)
        self.assertEqual(row['figure']['value'], round(100 * 4 / 6, 1))

        # Team-wide: alpha 4/6 plus bravo 2/2 = 6 of 8.
        team = self._panel(self._all(), 'first_pass_rate')['team']
        self.assertEqual((team['numerator'], team['n']), (6, 8))

    def test_error_distribution_counts_three_group_a_categories_exactly(self):
        """VERIFICATION 6 — failures across three Group A categories, counted exactly."""
        data = self._panel(self._all(), 'error_distribution')
        counts = {c['category']: c['count'] for c in data['team']}
        self.assertEqual(counts, {
            ERR_LAYOUT: 1,
            ERR_BOQ_QUANTITY: 1,
            ERR_ELECTRICAL_DESIGN: 1,
        })
        self.assertEqual(data['total'], 3)
        # Two came from package failures, one from a rejected Arka. Both sources count.
        self.assertEqual(data['by_source'], {'package': 2, 'arka': 1})

        # Every category on the panel really is Group A, asked of the Part 9 helper
        # rather than of a tuple in this file.
        for c in data['team']:
            self.assertEqual(error_category_group(c['category']), 'A')

    def test_group_b_failure_is_in_input_quality_and_in_no_designer_figure(self):
        """VERIFICATION 7 — the Group B failure appears in B and nowhere else."""
        result = self._all()

        # PRESENT in input quality.
        b = self._panel(result, 'group_b_failures')
        self.assertEqual(b['total'], 1)
        self.assertEqual([c['category'] for c in b['counts']], [ERR_SURVEY_INADEQUATE])

        # ABSENT from the error distribution, which is Group A only.
        dist = self._panel(result, 'error_distribution')
        self.assertNotIn(ERR_SURVEY_INADEQUATE, [c['category'] for c in dist['team']])
        for row in dist['rows']:
            self.assertNotIn(ERR_SURVEY_INADEQUATE,
                             [c['category'] for c in row['counts']])

        # ABSENT from the rework numerator. A6's attempt 2 exists because the survey was
        # wrong; alpha must not be charged for it.
        rework = self._row(self._panel(result, 'rework_multiplier'), 'alpha10')
        self.assertGreaterEqual(rework['excluded'], 1)
        total_attempts = DesignAttempt.objects.filter(
            assignment__assigned_to=self.alpha).count()
        self.assertEqual(rework['figure']['numerator'] + rework['excluded'],
                         total_attempts)

    def test_group_c_failure_and_accepted_cr_land_on_the_pm_not_the_designer(self):
        """VERIFICATION 8 — brief changes are charged to the PM who moved the brief."""
        result = self._all()

        # The Group C failure is counted in C.
        c = self._panel(result, 'group_c_failures')
        self.assertEqual(c['total'], 1)
        self.assertEqual([x['category'] for x in c['counts']], [ERR_REQUIREMENT_CHANGED])

        # It is NOT in the Group A distribution.
        dist = self._panel(result, 'error_distribution')
        self.assertNotIn(ERR_REQUIREMENT_CHANGED,
                         [x['category'] for x in dist['team']])

        # The accepted change request is on pm2's row, and pm2 is a PM, not a designer.
        crr = self._panel(result, 'change_request_rate')
        pm_row = self._row(crr, 'pm10b')
        self.assertIsNotNone(pm_row)
        self.assertEqual(pm_row['accepted'], 1)
        self.assertEqual(self.pm2.role, 'PM')

        # No designer name appears in the change request table at all.
        labels = [r['label'] for r in crr['rows']]
        self.assertNotIn('alpha10', labels)
        self.assertNotIn('bravo10', labels)

        # And the attempt the accepted request opened is out of alpha's rework numerator.
        rework = self._row(self._panel(result, 'rework_multiplier'), 'alpha10')
        self.assertGreaterEqual(rework['excluded'], 2)  # one Group B, one PM change

    def test_overturn_rate_matches_a_manual_count(self):
        """VERIFICATION 9 — overturns counted by hand against the computed rate."""
        # Flip two of qc's package verdicts to overturned, and one Arka.
        attempts = list(DesignAttempt.objects.filter(
            qc_reviewed_by=self.qc).exclude(head_verdict=QC_PENDING)[:2])
        self.assertEqual(len(attempts), 2)
        for t in attempts:
            t.head_overturned_qc = True
            t.save()
        arka = ArkaSubmission.objects.filter(
            reviewed_by=self.qc).exclude(head_verdict=ARKA_PENDING).first()
        arka.head_overturned_qc = True
        arka.save()

        manual_num = (
            DesignAttempt.objects.filter(qc_reviewed_by=self.qc,
                                         head_overturned_qc=True)
            .exclude(head_verdict=QC_PENDING).count()
            + ArkaSubmission.objects.filter(reviewed_by=self.qc,
                                            head_overturned_qc=True)
            .exclude(head_verdict=ARKA_PENDING).count())
        manual_den = (
            DesignAttempt.objects.filter(qc_reviewed_by=self.qc)
            .exclude(head_verdict=QC_PENDING).count()
            + ArkaSubmission.objects.filter(reviewed_by=self.qc)
            .exclude(head_verdict=ARKA_PENDING).count())
        self.assertEqual(manual_num, 3)

        row = self._row(self._panel(self._all(), 'overturn_rate'), 'qc10')
        self.assertEqual(row['figure']['numerator'], manual_num)
        self.assertEqual(row['figure']['n'], manual_den)

    def test_on_time_delivery_uses_the_approved_date_not_a_pending_extension(self):
        """VERIFICATION 10 — a pending extension must not rescue a late release.

        The site is released 20 days ago. Its APPROVED date was 25 days ago, so it is
        late. A pending extension proposes a date 5 days ago, which WOULD make it on time
        if `is_current` were read instead of the approved row.
        """
        site, assignment = self._site(self.t1, 'P10A-LATE', self.bravo, self.pm,
                                      released=True, released_days_ago=20)
        self._passed(assignment, 1, qc_by=self.qc2)
        approved = self._due(assignment, (self.now - timedelta(days=25)).date(),
                             approved=True, is_current=False, days_ago=30)
        pending = self._due(assignment, (self.now - timedelta(days=5)).date(),
                            approved=False, is_current=True)
        self.assertTrue(pending.is_current)
        self.assertIsNone(pending.approved_at)
        self.assertLess(approved.proposed_date, pending.proposed_date)

        row = self._row(self._panel(self._all(), 'on_time_delivery'), 'bravo10')
        # bravo's two other released sites are on time; this one is not.
        self.assertEqual(row['figure']['n'], 3)
        self.assertEqual(row['figure']['numerator'], 2)

    def test_hold_rate_counts_sites_ever_held_not_only_currently_held(self):
        data = self._panel(self._all(), 'hold_rate')
        self.assertEqual(data['held'], 1)
        # The held site's status was never moved to survey_returned, so a status-based
        # count would report zero. This one does not.
        self.assertEqual(data['currently_held'], 0)
        self.assertGreater(data['allocated'], 0)

    def test_hold_duration_reads_paired_log_events(self):
        data = self._panel(self._all(), 'hold_duration')
        self.assertEqual(data['events'], 2)
        self.assertEqual(data['completed'], 1)
        self.assertEqual(data['still_open'], 0)

    def test_extension_rate_counts_assignments_with_more_than_one_commitment(self):
        self._due(self.a8, (self.now - timedelta(days=20)).date())
        self._due(self.a8, (self.now - timedelta(days=10)).date(),
                  is_current=False, days_ago=9)
        data = self._panel(self._all(), 'extension_rate')
        self.assertEqual(data['extended'], 1)
        self.assertEqual(
            data['with_any'],
            DesignAssignment.objects.filter(due_date_commitments__isnull=False)
            .distinct().count())

    def test_cr_rejection_rate_reports_pending_beside_the_figure(self):
        data = self._panel(self._all(), 'cr_rejection_rate')
        self.assertEqual((data['rejected'], data['accepted'], data['total']), (1, 1, 2))
        # Denominator of 2 — below the minimum, so no percentage.
        self.assertIsNone(data['figure']['value'])

    def test_arka_iterations_exclude_carried_forward_versions(self):
        """A copy carried onto the next attempt is not an iteration anybody performed."""
        base = ArkaSubmission.objects.filter(attempt__assignment__assigned_to=self.alpha).first()
        before = self._row(self._panel(self._all(), 'arka_iterations'), 'alpha10')
        self._arka(self.t7b, 1, carried_from=base, submitted_by=self.alpha)
        after = self._row(self._panel(self._all(), 'arka_iterations'), 'alpha10')
        self.assertEqual(before['figure']['numerator'], after['figure']['numerator'])
        self.assertEqual(before['figure']['n'], after['figure']['n'])

    def test_per_tender_and_all_tenders_differ_correctly(self):
        """VERIFICATION 11 — the two scopes compute, and the combined one is the sum."""
        t1 = self._t1()
        both = self._all()
        t2 = compute([self.t2], set(CORE_METRICS) | set(OPTIONAL_METRICS))

        self.assertEqual(t1['released_count'], 6)          # alpha's six
        self.assertEqual(t2['released_count'], 2)          # bravo's two
        self.assertEqual(both['released_count'], 8)

        # bravo appears only in tender 2 and in the combined view.
        self.assertIsNone(self._row(self._panel(t1, 'first_pass_rate'), 'bravo10'))
        self.assertIsNotNone(self._row(self._panel(t2, 'first_pass_rate'), 'bravo10'))
        self.assertIsNotNone(self._row(self._panel(both, 'first_pass_rate'), 'bravo10'))

        # And the team denominators add up rather than being recomputed differently.
        self.assertEqual(self._panel(both, 'first_pass_rate')['team']['n'],
                         self._panel(t1, 'first_pass_rate')['team']['n']
                         + self._panel(t2, 'first_pass_rate')['team']['n'])

    def test_no_ranking_anywhere(self):
        """Every per-person table is ordered by NAME. A ranked list is forbidden."""
        result = self._all()
        for key in ('first_pass_rate', 'qc_failure_rate', 'arka_iterations',
                    'on_time_delivery', 'overturn_rate', 'change_request_rate'):
            labels = [r['label'] for r in self._panel(result, key)['rows']]
            self.assertEqual(labels, sorted(labels, key=str.lower), key)


# ===========================================================================
# 3. Configuration
# ===========================================================================

class ConfigurationTests(Part10Base):

    def test_default_is_core_only(self):
        self.assertEqual(selected_metric_keys(None), set(CORE_METRICS))
        self.assertEqual(len(CORE_METRICS), 5)

    def test_every_core_metric_renders_with_core_only_selected(self):
        """VERIFICATION 2, first half."""
        self._login(self.head)
        response = self.client.get(reverse('design_quality_analytics'))
        self.assertEqual(response.status_code, 200)
        keys = [p['metric'].key for p in response.context['panels']]
        self.assertEqual(set(keys), set(CORE_METRICS))
        html = response.content.decode()
        for key in CORE_METRICS:
            label = next(m.label for m in METRIC_CATALOGUE if m.key == key)
            self.assertIn(label, html)

    def test_core_metrics_cannot_be_switched_off(self):
        """VERIFICATION 2, second half — by DIRECT POST, not by a disabled checkbox.

        The form renders core boxes disabled, so a browser never submits them. A disabled
        input is not a permission, so the endpoint is posted an explicit attempt to store
        an empty selection AND an attempt to store a core key, and neither may take.
        """
        self._login(self.head)
        self.client.post(reverse('design_analytics_configure'),
                         {'metrics': list(CORE_METRICS), 'scope': ''})
        preference = DesignAnalyticsPreference.objects.get(profile=self.head)
        self.assertEqual(preference.metrics, [])          # core keys discarded, not stored
        self.assertEqual(selected_metric_keys(preference), set(CORE_METRICS))

        response = self.client.get(reverse('design_quality_analytics'))
        self.assertEqual(set(p['metric'].key for p in response.context['panels']),
                         set(CORE_METRICS))

    def test_a_core_key_that_reaches_storage_is_still_ignored(self):
        """Belt and braces: even a row hand-edited to hold a core key changes nothing."""
        preference = DesignAnalyticsPreference.objects.create(
            profile=self.head, metrics=list(CORE_METRICS) + ['cycle_time'])
        self.assertEqual(selected_metric_keys(preference),
                         set(CORE_METRICS) | {'cycle_time'})

    def test_unknown_keys_are_dropped_rather_than_raising(self):
        preference = DesignAnalyticsPreference.objects.create(
            profile=self.head, metrics=['cycle_time', 'a_metric_from_2029'])
        self.assertEqual(selected_metric_keys(preference),
                         set(CORE_METRICS) | {'cycle_time'})

    def test_enable_three_persist_disable_and_reset(self):
        """VERIFICATION 3 — the whole configuration round trip."""
        self._login(self.head)
        three = ['cycle_time', 'queue_latency', 'group_b_failures']

        self.client.post(reverse('design_analytics_configure'),
                         {'metrics': three, 'scope': ''})
        response = self.client.get(reverse('design_quality_analytics'))
        keys = set(p['metric'].key for p in response.context['panels'])
        self.assertEqual(keys, set(CORE_METRICS) | set(three))

        # Persist across a fresh session — it is stored per user, not per page.
        self.client.logout()
        self._login(self.head)
        response = self.client.get(reverse('design_quality_analytics'))
        self.assertEqual(set(p['metric'].key for p in response.context['panels']),
                         set(CORE_METRICS) | set(three))

        # Disable one.
        self.client.post(reverse('design_analytics_configure'),
                         {'metrics': three[:2], 'scope': ''})
        response = self.client.get(reverse('design_quality_analytics'))
        keys = set(p['metric'].key for p in response.context['panels'])
        self.assertNotIn('group_b_failures', keys)
        self.assertIn('cycle_time', keys)

        # Reset.
        self.client.post(reverse('design_analytics_reset'), {'scope': ''})
        self.assertFalse(
            DesignAnalyticsPreference.objects.filter(profile=self.head).exists())
        response = self.client.get(reverse('design_quality_analytics'))
        self.assertEqual(set(p['metric'].key for p in response.context['panels']),
                         set(CORE_METRICS))

    def test_selection_is_per_user(self):
        """A second Head's selection does not move the first one's."""
        other = _profile('head10b', 'Design', is_design_head=True)
        self._login(self.head)
        self.client.post(reverse('design_analytics_configure'),
                         {'metrics': ['cycle_time'], 'scope': ''})
        self.client.logout()
        self._login(other)
        self.client.post(reverse('design_analytics_configure'),
                         {'metrics': ['queue_latency'], 'scope': ''})
        self.assertEqual(
            DesignAnalyticsPreference.objects.get(profile=self.head).metrics,
            ['cycle_time'])
        self.assertEqual(
            DesignAnalyticsPreference.objects.get(profile=other).metrics,
            ['queue_latency'])

    def test_scope_survives_a_configuration_post(self):
        self._login(self.head)
        response = self.client.post(reverse('design_analytics_configure'),
                                    {'metrics': ['cycle_time'], 'scope': str(self.t1.pk)})
        self.assertRedirects(
            response, reverse('design_quality_analytics_tender', kwargs={'pk': self.t1.pk}))


# ===========================================================================
# 4. Access
# ===========================================================================

class AccessTests(Part10Base):

    #: VERIFICATION 12 — every one of these must be refused by DIRECT URL.
    def _refused_profiles(self):
        return [
            ('designer alpha', self.alpha),
            ('designer bravo', self.bravo),
            ('design qc',      self.qc),
            ('pm',             self.pm),
            ('scm',            self.scm),
            ('site engineer',  self.se),
            # The deputy too. This screen is the one place the module does NOT admit them
            # — see the note above design_quality_analytics.
            ('head deputy',    self.deputy),
        ]

    def test_every_other_role_is_refused_by_direct_url(self):
        """VERIFICATION 12 — all four URLs, for all seven users."""
        targets = [
            ('get',  reverse('design_quality_analytics'), {}),
            ('get',  reverse('design_quality_analytics_tender',
                             kwargs={'pk': self.t1.pk}), {}),
            ('post', reverse('design_analytics_configure'), {'metrics': ['cycle_time']}),
            ('post', reverse('design_analytics_reset'), {}),
        ]
        for name, profile in self._refused_profiles():
            self._login(profile)
            for method, url, payload in targets:
                response = (self.client.get(url) if method == 'get'
                            else self.client.post(url, payload))
                self.assertEqual(response.status_code, 403,
                                 f'{name} was not refused {method.upper()} {url}')
            self.client.logout()

    def test_a_refused_post_writes_nothing(self):
        """A 403 on the configure endpoint must not leave a preference row behind."""
        self._login(self.alpha)
        self.client.post(reverse('design_analytics_configure'),
                         {'metrics': ['cycle_time']})
        self.assertEqual(DesignAnalyticsPreference.objects.count(), 0)

    def test_the_head_is_admitted(self):
        self._login(self.head)
        self.assertEqual(
            self.client.get(reverse('design_quality_analytics')).status_code, 200)
        self.assertEqual(
            self.client.get(reverse('design_quality_analytics_tender',
                                    kwargs={'pk': self.t1.pk})).status_code, 200)

    def test_anonymous_is_sent_to_login(self):
        response = self.client.get(reverse('design_quality_analytics'))
        self.assertIn(response.status_code, (302, 403))

    def test_a_residential_program_is_not_reachable(self):
        residential = Program.objects.create(
            name='P10-Res', program_type='CAPEX', client_name='C',
            status='Active', short_tender_code='P10R')
        self._login(self.head)
        response = self.client.get(reverse('design_quality_analytics_tender',
                                           kwargs={'pk': residential.pk}))
        self.assertEqual(response.status_code, 404)


# ===========================================================================
# 5. Read-only, and the query budget
# ===========================================================================

class ReadOnlyTests(Part10Base):

    def _counts(self):
        return {m.__name__: m.objects.count() for m in WORKFLOW_MODELS}

    def test_page_load_writes_no_workflow_row(self):
        """VERIFICATION 13 — row counts on every design model, before and after.

        Both scopes, and with EVERY metric selected, so the Design Hold duration read (the
        one query that only appears when an optional metric is on) is exercised too.
        """
        self._login(self.head)
        self.client.post(reverse('design_analytics_configure'),
                         {'metrics': list(OPTIONAL_METRICS), 'scope': ''})

        before = self._counts()
        self.client.get(reverse('design_quality_analytics'))
        self.client.get(reverse('design_quality_analytics_tender',
                                kwargs={'pk': self.t1.pk}))
        self.assertEqual(self._counts(), before)

    def test_page_load_touches_no_residential_row_and_no_notification(self):
        """VERIFICATION 14 — Residential, Task, BOQ and NotificationLog all unchanged."""
        self._login(self.head)
        before = {
            'projects': Project.objects.count(),
            'tasks':    Task.objects.count(),
            'boq':      BOQ.objects.count(),
            'boq_item': BOQItem.objects.count(),
            'notif':    NotificationLog.objects.count(),
        }
        self.client.get(reverse('design_quality_analytics'))
        self.assertEqual({
            'projects': Project.objects.count(),
            'tasks':    Task.objects.count(),
            'boq':      BOQ.objects.count(),
            'boq_item': BOQItem.objects.count(),
            'notif':    NotificationLog.objects.count(),
        }, before)

    def test_a_get_does_not_create_a_preference_row(self):
        """get_or_create on a GET would be a write on a read. It is a filter().first()."""
        self._login(self.head)
        self.client.get(reverse('design_quality_analytics'))
        self.assertEqual(DesignAnalyticsPreference.objects.count(), 0)

    def test_query_count_is_flat_in_the_number_of_sites(self):
        """VERIFICATION 1 — the N+1 check, stated as a property rather than a number.

        A fixed query count is asserted by comparing the SAME page against a scope with
        twelve more sites in it. If any metric queried per site, per attempt or per
        designer, the second number would be larger.
        """
        selected = set(CORE_METRICS) | set(OPTIONAL_METRICS)

        with self.assertNumQueries(6):
            compute([self.t1, self.t2], selected)

        for i in range(12):
            site, a = self._site(self.t2, f'P10B-EXTRA{i}', self.bravo, self.pm,
                                 released=True, attempt_count=1)
            t = self._passed(a, 1, qc_by=self.qc2)
            self._arka(t, 1, submitted_by=self.bravo, qc_by=self.qc2)
            self._due(a, (self.now - timedelta(days=8)).date())

        with self.assertNumQueries(6):
            compute([self.t1, self.t2], selected)

    def test_core_only_costs_one_query_fewer(self):
        """The hold-event read is paid for only when its metric is on.

        Five batched reads carry the entire core set — assignments, attempts, Arka
        versions, commitments, change requests. The sixth appears only for Design Hold
        duration, which is the one metric that needs the activity log.
        """
        with self.assertNumQueries(5):
            compute([self.t1], set(CORE_METRICS))
        with self.assertNumQueries(6):
            compute([self.t1], set(CORE_METRICS) | {'hold_duration'})


# ===========================================================================
# 6. The catalogue itself
# ===========================================================================

class CatalogueTests(Part10Base):

    def test_every_catalogue_key_has_a_computation(self):
        from .design_analytics import _COMPUTE
        for metric in METRIC_CATALOGUE:
            self.assertIn(metric.key, _COMPUTE, metric.key)

    def test_core_and_optional_partition_the_catalogue(self):
        self.assertEqual(set(CORE_METRICS) | set(OPTIONAL_METRICS),
                         set(m.key for m in METRIC_CATALOGUE))
        self.assertFalse(set(CORE_METRICS) & set(OPTIONAL_METRICS))

    def test_the_core_set_spans_all_four_groups(self):
        """The locked set must not be four designer metrics wearing a lock.

        Three of the five measure something OTHER than a designer — that is the whole
        argument for locking them, and it is worth a test rather than a comment.
        """
        core = [m for m in METRIC_CATALOGUE if m.core]
        self.assertEqual(sorted(set(m.group for m in core)), ['A', 'B', 'C', 'D'])
        self.assertEqual(sum(1 for m in core if m.group != 'A'), 3)

    def test_every_core_metric_states_why_it_is_locked(self):
        for metric in METRIC_CATALOGUE:
            if metric.core:
                self.assertTrue(metric.why.strip(), metric.key)

    def test_every_reduced_metric_carries_its_caveat(self):
        """The four metrics that could not be built as specified must say so on screen."""
        reduced = {'stage_dwell', 'cr_by_stage', 'hold_duration', 'capacity_throughput'}
        for metric in METRIC_CATALOGUE:
            if metric.key in reduced:
                self.assertTrue(metric.caveat.strip(), metric.key)

    def test_no_export_route_exists(self):
        """Deliberate gap: no CSV, no PDF, no scheduled report in this session."""
        from django.urls import NoReverseMatch
        for name in ('design_analytics_export', 'design_analytics_csv',
                     'design_analytics_pdf'):
            with self.assertRaises(NoReverseMatch):
                reverse(name)
