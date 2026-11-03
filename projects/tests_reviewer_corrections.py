"""
BC-2 — reviewer BOQ corrections per designer, on the Design Head's tender dashboard.

WHAT IS PINNED
--------------
  * The counts and the rate: sites corrected / sites that reached package review, with
    quantity edits and added lines counted separately. Several edits on one BOQ are one
    site.
  * The denominator: a site counts when the ledger, a started QC or its current status
    says its package went to review, never on a stamped BOQ alone.
  * Both B-06 exclusions: a correction a change request cites, and one made while a
    request was open on the site, cited or not. A correction made in review BEFORE a later
    request still counts.
  * Rework is untouched: the workload table's figures are identical with and without
    corrections.
  * Head and deputy only. A designer is refused the page; the Design QC dashboard does not
    carry the panel.
  * One query for the panel, flat in the number of sites; one more for the drill-down.

FIXTURES are RoutingBase's (tests_change_request_routing), so corrections, raises,
forwards and `corrected` verdicts go through the product views. The rows that the product
can no longer make at that point (an in-review correction on a released site, a row after
a request closed) are written directly and say so.
"""
from datetime import timedelta
from decimal import Decimal

from django.urls import reverse
from django.utils import timezone

from .design_metrics import (
    reviewer_correction_drill, reviewer_correction_panel, reviewer_corrections,
    tender_metrics,
)
from .models import (
    BOQCorrection, BOQItemMaster, DesignAttempt, DesignAssignment, StatusTransition,
    ATTEMPT_REASON_QC_FAILED, BOQ_CORRECTION_QUANTITY_CHANGED, CHANGE_REQUEST_CORRECTED,
    CHANGE_REQUEST_REJECTED, DESIGN_ARKA_SUBMITTED, DESIGN_ARTIFACTS_UPLOADED,
    DESIGN_IN_DESIGN, ERR_LAYOUT, QC_FAILED, SUBJECT_DESIGN_ASSIGNMENT,
)
from .tests_change_request_routing import RoutingBase
from .tests_design_change_window import _profile


class ReviewerCorrectionBase(RoutingBase):

    def setUp(self):
        super().setUp()
        # A catalogue item a reviewer can add as a new line (add_line refuses one that is
        # already on the BOQ, and every fixture BOQ already holds self.item).
        self.extra = BOQItemMaster.objects.create(
            code='CW-002', description='Inverter', unit='Nos', project_type='OPEX')

    def _set_quantity(self, site, quantity, profile=None):
        """One set_quantity POST through boq_correct, on the fixture's catalogue row."""
        item = site.boq.items.get(item_master=self.item)
        self._login(profile or self.qc)
        self.client.post(reverse('boq_correct', kwargs={'project_id': site.project_id}),
                         {'action': 'set_quantity', 'item_id': item.pk,
                          'quantity': str(quantity)})
        self.client.logout()

    def _add_line(self, site, quantity, profile=None):
        self._login(profile or self.qc)
        self.client.post(reverse('boq_correct', kwargs={'project_id': site.project_id}),
                         {'action': 'add_line', 'master_id': str(self.extra.pk),
                          'quantity': str(quantity)})
        self.client.logout()

    def _panel(self):
        return reviewer_correction_panel(tender_metrics(self.program)['sites'])

    def _row(self, profile=None):
        """The panel row for one designer (the fixture designer by default), or None."""
        profile = profile or self.designer
        return next((r for r in self._panel()['rows'] if r['designer'].pk == profile.pk),
                    None)

    def _dashboard(self, profile=None, query=''):
        self._login(profile or self.head)
        response = self.client.get(
            reverse('design_tender_dashboard', kwargs={'pk': self.program.pk}) + query)
        self.client.logout()
        return response


# ===========================================================================
# 1. Counts and rate
# ===========================================================================

class CountTests(ReviewerCorrectionBase):

    def test_01_two_of_three_reviewed_sites_corrected(self):
        one, _ = self._pre_release('BC2-1')
        two, _ = self._pre_release('BC2-2')
        self._pre_release('BC2-3')
        self._set_quantity(one, 12)
        self._set_quantity(two, 14)
        self.assertEqual(BOQCorrection.objects.count(), 2)   # setup: both went through

        row = self._row()
        self.assertEqual((row['reviewed'], row['sites_corrected'], row['rate']), (3, 2, 67))
        self.assertEqual((row['quantity_edits'], row['lines_added']), (2, 0))

        html = self._dashboard().content.decode()
        self.assertIn('Reviewer BOQ corrections', html)
        self.assertIn('67%', html)
        self.assertNotIn('No reviewer corrections yet.', html)

    def test_02_several_edits_on_one_boq_are_one_site(self):
        site, _ = self._pre_release('BC2-M')
        self._set_quantity(site, 12)
        self._set_quantity(site, 15)
        self._add_line(site, 2)
        self.assertEqual(BOQCorrection.objects.count(), 3)   # setup: all three went through

        row = self._row()
        self.assertEqual((row['reviewed'], row['sites_corrected'], row['rate']), (1, 1, 100))
        self.assertEqual((row['quantity_edits'], row['lines_added']), (2, 1))

    def test_03_zero_state(self):
        self._pre_release('BC2-Z')
        panel = self._panel()
        self.assertEqual(panel['total_edits'], 0)
        self.assertEqual(panel['rows'][0]['reviewed'], 1)
        self.assertIn('No reviewer corrections yet.', self._dashboard().content.decode())

    def test_04_figures_follow_the_designer(self):
        other = _profile('bc2_other', 'Design')
        mine, _ = self._pre_release('BC2-D1')
        theirs, assignment = self._pre_release('BC2-D2')
        DesignAssignment.objects.filter(pk=assignment.pk).update(assigned_to=other)
        self._set_quantity(theirs, 12)
        self.assertEqual(self._row()['sites_corrected'], 0)
        self.assertEqual(self._row(other)['sites_corrected'], 1)


# ===========================================================================
# 2. The denominator — sites that reached package review
# ===========================================================================

class DenominatorTests(ReviewerCorrectionBase):

    def test_05_a_stamped_boq_that_never_reached_review_is_not_counted(self):
        reviewed, _ = self._pre_release('BC2-R')
        _, stamped = self._site('BC2-S')
        attempt = self._package(stamped)
        # Still in the Arka review: BOQ stamped, QC never started, no ledger entry.
        DesignAttempt.objects.filter(pk=attempt.pk).update(qc_started_at=None)
        DesignAssignment.objects.filter(pk=stamped.pk).update(status=DESIGN_ARKA_SUBMITTED)
        self.assertIsNotNone(DesignAttempt.objects.get(pk=attempt.pk).boq_submitted_at)

        self.assertEqual(self._row()['reviewed'], 1)
        self.assertEqual(self._panel()['reviewed_projects'][self.designer.pk], [reviewed])

    def test_06_the_ledger_alone_is_enough(self):
        """A site back in design with no QC start still counts if the ledger says its
        package entered artifacts_uploaded once."""
        site, assignment = self._site('BC2-L')
        attempt = self._package(assignment)
        DesignAttempt.objects.filter(pk=attempt.pk).update(qc_started_at=None)
        self.assertIsNone(self._row())                       # not yet: nothing says review
        # Ledger row written directly (setup): the product writes it inside
        # apply_design_status, which this fixture does not drive.
        StatusTransition.objects.create(
            subject_type=SUBJECT_DESIGN_ASSIGNMENT, subject_id=assignment.pk, project=site,
            from_status=DESIGN_ARKA_SUBMITTED, to_status=DESIGN_ARTIFACTS_UPLOADED,
            actor=self.designer, actor_role_code='Design')
        self.assertEqual(assignment.status, DESIGN_IN_DESIGN)
        self.assertEqual(self._row()['reviewed'], 1)


# ===========================================================================
# 3. The B-06 exclusions — change requests are never charged to the designer
# ===========================================================================

class ExclusionTests(ReviewerCorrectionBase):

    def test_07_a_correction_cited_by_a_corrected_verdict_is_excluded(self):
        site, assignment = self._released('BC2-C')
        self._raise(self.scm, site)
        change = self._one(assignment)
        self._forward(change)
        cited = self._boq_correct(site, 12)
        self._correct(change)
        change.refresh_from_db()
        self.assertEqual(change.verdict, CHANGE_REQUEST_CORRECTED)
        self.assertEqual(list(change.boq_corrections.all()), [cited])   # setup

        self.assertNotIn(cited, reviewer_corrections())
        row = self._row()
        self.assertEqual((row['reviewed'], row['sites_corrected']), (1, 0))
        self.assertEqual(row['quantity_edits'], 0)

        # A cited row always falls inside its request's open window too, so the window
        # alone would exclude it. Moved before the raise (direct write), only the citation
        # still excludes it — this pins that exclusion on its own.
        BOQCorrection.objects.filter(pk=cited.pk).update(
            corrected_at=change.requested_at - timedelta(minutes=1))
        self.assertNotIn(cited, reviewer_corrections())
        self.assertEqual(self._row()['sites_corrected'], 0)

    def test_08_a_head_correction_while_a_request_was_pending_is_excluded_uncited(self):
        site, assignment = self._released('BC2-P')
        self._raise(self.pm, site)
        change = self._one(assignment)
        uncited = self._boq_correct(site, 12)                # the BC-1 Head exception
        self.assertIsNotNone(uncited)                        # setup: the screen was open
        self._reject(change)
        change.refresh_from_db()
        self.assertEqual(change.verdict, CHANGE_REQUEST_REJECTED)
        self.assertFalse(change.boq_corrections.exists())    # never cited

        self.assertNotIn(uncited, reviewer_corrections())
        self.assertEqual(self._row()['sites_corrected'], 0)

        # Once the request closed, the window closed with it: a row stamped after
        # decided_at counts again. Written directly — the product refuses a correction on
        # a released site with no pending request (BC-1).
        later = self._in_review_correction(site, 13, minutes_ago=-5)
        self.assertGreater(later.corrected_at, change.decided_at)
        self.assertIn(later, reviewer_corrections())
        self.assertEqual(self._row()['quantity_edits'], 1)

    def test_09_a_correction_in_review_before_a_later_request_still_counts(self):
        site, assignment = self._released('BC2-E')
        early = self._in_review_correction(site, 11)         # in review, before any raise
        self._raise(self.pm, site)
        change = self._one(assignment)
        self.assertLess(early.corrected_at, change.requested_at)
        cited = self._boq_correct(site, 12)
        self._correct(change)

        counted = list(reviewer_corrections())
        self.assertIn(early, counted)
        self.assertNotIn(cited, counted)
        row = self._row()
        self.assertEqual((row['sites_corrected'], row['quantity_edits']), (1, 1))


# ===========================================================================
# 4. Rework is untouched
# ===========================================================================

class ReworkUnchangedTests(ReviewerCorrectionBase):

    def test_10_the_rework_figures_are_identical_with_and_without_corrections(self):
        site, assignment = self._released('BC2-W')
        # One Group A loop on the finished site (fixture writer), so Rework reads 1.0x
        # rather than a zero that any change would also leave at zero.
        first = DesignAttempt.objects.get(assignment=assignment, attempt_number=1)
        DesignAttempt.objects.filter(pk=first.pk).update(
            qc_verdict=QC_FAILED, qc_remarks='layout clash', qc_failure_category=ERR_LAYOUT)
        DesignAttempt.objects.create(assignment=assignment, attempt_number=2,
                                     opened_reason=ATTEMPT_REASON_QC_FAILED)

        def figures():
            w = next(r for r in tender_metrics(self.program)['workload']
                     if r['designer'].pk == self.designer.pk)
            return (w['rework'], w['input_quality'], w['pm_change_multiplier'],
                    w['attempts'], w['designer_error_attempts'])

        before = figures()
        self.assertEqual(before, (1.0, 0.0, 0.0, 2, 1))
        self._in_review_correction(site, 12)
        self._in_review_correction(site, 14)
        self.assertEqual(self._row()['quantity_edits'], 2)   # the corrections do count here
        self.assertEqual(figures(), before)
        self.assertEqual(DesignAttempt.objects.filter(assignment=assignment).count(), 2)
        self.assertIn('<strong>1.0&times;</strong>', self._dashboard().content.decode())


# ===========================================================================
# 5. Who sees it
# ===========================================================================

class AccessTests(ReviewerCorrectionBase):

    def setUp(self):
        super().setUp()
        site, _ = self._pre_release('BC2-A')
        self._set_quantity(site, 12)

    def test_11_the_head_and_the_deputy_see_the_panel(self):
        deputy = _profile('bc2_deputy', 'Design')
        self.head.design_head_deputy = deputy
        self.head.save(update_fields=['design_head_deputy'])
        for profile in (self.head, deputy):
            response = self._dashboard(profile)
            self.assertEqual(response.status_code, 200)
            self.assertIn('Reviewer BOQ corrections', response.content.decode())

    def test_12_a_non_head_cannot_see_the_metric(self):
        for profile in (self.designer, self.qc, self.pm, self.scm):
            response = self._dashboard(profile)
            self.assertEqual(response.status_code, 403, profile.user.username)
            self.assertNotIn('Reviewer BOQ corrections', response.content.decode())
        # Design QC's own dashboard is a strict subset and does not carry the panel.
        self._login(self.qc)
        response = self.client.get(
            reverse('design_qc_dashboard', kwargs={'pk': self.program.pk}))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('Reviewer BOQ corrections', response.content.decode())
        self.assertNotIn('corrections', response.context)


# ===========================================================================
# 6. Drill-down and query count
# ===========================================================================

class DrillAndQueryTests(ReviewerCorrectionBase):

    def test_13_the_drill_down_lists_each_row_with_before_and_after(self):
        site, _ = self._pre_release('BC2-X')
        self._set_quantity(site, 12)
        self._add_line(site, 3)
        response = self._dashboard(query=f'?corrections={self.designer.pk}')
        drill = response.context['corrections_drill']
        self.assertEqual([g['project'] for g in drill], [site])
        rows = drill[0]['corrections']
        self.assertEqual([(c.quantity_before, c.quantity_after) for c in rows],
                         [(Decimal('10.00'), Decimal('12.00')), (None, Decimal('3.00'))])
        self.assertEqual({c.corrected_by for c in rows}, {self.qc})
        html = response.content.decode()
        self.assertIn('BC2-X', html)
        self.assertIn('Quantity changed', html)
        self.assertIn('Line added', html)

    def test_14_an_unknown_designer_opens_no_drill(self):
        self._pre_release('BC2-U')
        response = self._dashboard(query='?corrections=999999')
        self.assertIsNone(response.context['corrections_drill'])

    def test_15_the_panel_is_one_query_flat_in_sites(self):
        for n in range(1, 4):
            site, _ = self._pre_release(f'BC2-Q{n}')
            self._set_quantity(site, 12)
        sites = tender_metrics(self.program)['sites']
        with self.assertNumQueries(1):
            three = reviewer_correction_panel(sites)
        for n in range(4, 8):
            site, _ = self._pre_release(f'BC2-Q{n}')
            self._set_quantity(site, 12)
        sites = tender_metrics(self.program)['sites']
        with self.assertNumQueries(1):
            seven = reviewer_correction_panel(sites)
        self.assertEqual((three['total_edits'], seven['total_edits']), (3, 7))
        with self.assertNumQueries(1):
            reviewer_correction_drill(seven, self.designer.pk)
        with self.assertNumQueries(0):
            reviewer_correction_panel([])
