"""
W-6 — the "Arka/CAD revisions" column beside the reviewer BOQ corrections (ruling 10).

WHAT IS PINNED (Zuber's W-6 answers, 22 Nov 2026)
-------------------------------------------------
  * A revision is a reviewer row (reviewer_role set) that is not a carried copy, credited
    to designer_at_revision. After a reallocation it stays with that designer, who keeps a
    row with no site left (Q3); the new designer gets none.
  * Arka and CAD are split; the total is their sum. Two revisions on one site are two.
  * A carried copy is never counted again; the designer's own versions never count.
  * Scope = the corrections' scope: this tender, live sites, reached review. The gate
    never drops a revision the product can write (the window is inside review), but it
    applies all the same.
  * No designer stamped: no fallback to the current designer, its own footnote (Q2); the
    corrections footnote is unchanged.
  * Counts only — no revisions rate; the below-5 rule hides only the corrections rate (Q1).
  * Corrections, rate and the workload's rework figures are identical with and without
    revisions. The panel costs three queries however many designers there are.
  * The column, its tooltip and the footnote render on the tender dashboard, the only
    screen that shows the panel; the table shows for revisions alone (Q4).

FIXTURES are ReviewerCorrectionBase's (sites in `in_qc` with an approved Arka, a CAD and a
BOQ). Revisions and fails go through the product views (design_arka_revise,
design_cad_revise, design_head_qc_fail); storage and PDF extraction are mocked as in
W-5b's tests. Status moves and the few rows no product path can write are direct and say so.
"""
import io
from unittest import mock

from django.urls import reverse
from django.utils import timezone

from .design_metrics import (
    PACKAGE_REVIEW_REACHED_STATUSES, reviewer_correction_panel, tender_metrics,
)
from .design_views import _allocate_one
from .models import (
    ArkaSubmission, DesignAssignment, DesignAttempt, DesignFile, Program, Project,
    DESIGN_ARTIFACTS_UPLOADED, DESIGN_AWAITING_HEAD_QC, DESIGN_FILE_CAD_ZIP,
    DESIGN_IN_DESIGN, DESIGN_IN_QC, ERR_LAYOUT, QC_PASSED, REVIEWER_ROLE_QC,
)
from .permissions import BOQ_CORRECTION_WINDOW_STATUSES
from .tests_design_change_window import _profile
from .tests_design_reviewer_cad_revisions import BUCKET, DELETE, EXTRACT, STORE, _zip
from .tests_reviewer_corrections import ReviewerCorrectionBase, UNASSIGNED_NOTE

TOOLTIP = ('New Arka or CAD versions put in by QC or the Head during review. '
           'Not counted as rework.')
REVISION_NOTE = 'site(s) with reviewer revisions have no designer to credit.'


class RevisionColumnBase(ReviewerCorrectionBase):

    # ── product views ───────────────────────────────────────────────────────

    def _arka_revise(self, site, who=None, capacity='118.00'):
        """A reviewer Arka revision through design_arka_revise (the capacity changes)."""
        self._login(who or self.qc)
        self.client.post(
            reverse('design_arka_revise', kwargs={'project_id': site.project_id}),
            {'capacity_kw': capacity, 'arka_link': 'https://example.com/arka',
             'revision_reason': 'Two rows clash with the tank'})
        self.client.logout()

    def _cad_revise(self, site, who=None):
        """A reviewer CAD through design_cad_revise, storage and extraction mocked."""
        self._login(who or self.qc)
        upload = io.BytesIO(_zip())
        upload.name = 'reviewer.zip'
        with mock.patch(STORE, return_value=(BUCKET, f'{site.project_id}/cad_zip/r.zip')), \
             mock.patch(EXTRACT), \
             mock.patch(DELETE, return_value=[]):
            self.client.post(
                reverse('design_cad_revise', kwargs={'project_id': site.project_id}),
                {'cad_file': upload, 'revision_reason': 'Legend corrected'})
        self.client.logout()

    def _at_head(self, assignment):
        """FIXTURE WRITE — Design QC passed attempt 1; the package waits for the Head."""
        now = timezone.now()
        DesignAttempt.objects.filter(assignment=assignment, attempt_number=1).update(
            qc_verdict=QC_PASSED, qc_reviewed_by=self.qc, qc_reviewed_at=now,
            head_started_at=now)
        DesignAssignment.objects.filter(pk=assignment.pk).update(
            status=DESIGN_AWAITING_HEAD_QC)
        assignment.refresh_from_db()

    def _head_fail_boq_only(self, site, assignment):
        """The Head fails the package naming {BOQ} only, so Arka and CAD carry (W-7)."""
        self._login(self.head)
        self.client.post(
            reverse('design_head_qc_fail', kwargs={'project_id': site.project_id}),
            {'head_remarks': 'BOQ is short', 'error_category': ERR_LAYOUT,
             'redo_scope_submitted': '1', 'redo': ['boq']})
        self.client.logout()
        assignment.refresh_from_db()
        self.assertEqual(assignment.current_attempt_number, 2)    # setup: it failed

    def _reassign(self, assignment, designer):
        """As tests_reviewer_corrections: through _allocate_one, the product writer, with
        the status stepped back to in_design and restored (setup writes)."""
        DesignAssignment.objects.filter(pk=assignment.pk).update(status=DESIGN_IN_DESIGN)
        assignment.refresh_from_db()
        _allocate_one(assignment, designer, self.head)
        DesignAssignment.objects.filter(pk=assignment.pk).update(status=DESIGN_IN_QC)
        assignment.refresh_from_db()
        self.assertEqual(assignment.assigned_to, designer)       # setup: it moved

    def _site_for(self, code, designer):
        """FIXTURE WRITE — a site in review held by `designer` from the start."""
        site, assignment = self._pre_release(code)
        DesignAssignment.objects.filter(pk=assignment.pk).update(assigned_to=designer)
        assignment.refresh_from_db()
        return site, assignment

    # ── reads ───────────────────────────────────────────────────────────────

    def _figures(self, row):
        return (row['revisions'], row['arka_revisions'], row['cad_revisions'])

    def _raw_revisions(self):
        """Every reviewer row on the books, carried copies included."""
        return (ArkaSubmission.objects.exclude(reviewer_role='').count()
                + DesignFile.objects.exclude(reviewer_role='').count())


# ===========================================================================
# 1. Credit and split
# ===========================================================================

class CreditTests(RevisionColumnBase):

    def test_01_qc_and_head_revisions_credit_the_designer_at_revision(self):
        one, _ = self._pre_release('W6-C1')
        two, _ = self._pre_release('W6-C2')
        self._arka_revise(one, who=self.qc)
        self._cad_revise(two, who=self.head)
        self.assertEqual(self._raw_revisions(), 2)               # setup: both went through

        row = self._row()
        self.assertEqual(self._figures(row), (2, 1, 1))
        panel = self._panel()
        self.assertEqual((panel['total_revisions'], panel['unassigned_revision_sites']),
                         (2, 0))

    def test_02_arka_and_cad_split_and_the_total_is_their_sum(self):
        site, _ = self._pre_release('W6-S1')
        other, _ = self._pre_release('W6-S2')
        self._arka_revise(site, capacity='118.00')
        self._arka_revise(site, capacity='116.00')               # two on one site = two
        self._cad_revise(site)
        self._cad_revise(other)
        self._cad_revise(other, who=self.head)
        row = self._row()
        self.assertEqual(self._figures(row), (5, 2, 3))
        self.assertEqual(row['revisions'], row['arka_revisions'] + row['cad_revisions'])

    def test_03_after_reallocation_the_count_stays_with_the_old_designer(self):
        new = _profile('w6_new', 'Design')
        site, assignment = self._pre_release('W6-R')
        self._arka_revise(site)
        self._cad_revise(site)
        self._reassign(assignment, new)

        old_row = self._row(self.designer)
        new_row = self._row(new)
        # Q3: the old designer holds no site here now, and still gets a row.
        self.assertEqual((old_row['reviewed'], old_row['rate']), (0, None))
        self.assertEqual(self._figures(old_row), (2, 1, 1))
        self.assertEqual(new_row['reviewed'], 1)
        self.assertEqual(self._figures(new_row), (0, 0, 0))

        html = self._dashboard().content.decode()
        panel_html = html.split('id="reviewerCorrections"', 1)[1]
        self.assertIn('cw_des', panel_html)                       # built from the stamp
        self.assertIn('Arka 1 · CAD 1', panel_html)

    def test_04_the_designers_own_versions_are_not_counted(self):
        site, assignment = self._pre_release('W6-OWN')
        # FIXTURE WRITE — a second designer-made Arka and CAD on attempt 1.
        attempt = assignment.attempts.get()
        ArkaSubmission.objects.filter(attempt=attempt).update(is_current=False)
        arka = ArkaSubmission.objects.create(
            attempt=attempt, version=2, capacity_kw=110, arka_link='https://example.com/a2',
            submitted_by=self.designer, is_current=True)
        DesignFile.objects.filter(attempt=attempt).update(is_current=False)
        DesignFile.objects.create(
            attempt=attempt, kind=DESIGN_FILE_CAD_ZIP, version=2, bucket='b',
            path='W6-OWN/cad/v2.zip', original_filename='v2.zip', size_bytes=10,
            content_type='application/zip', derived_from_arka=arka,
            uploaded_by=self.designer, is_current=True)
        self.assertEqual(ArkaSubmission.objects.count() + DesignFile.objects.count(), 4)

        self.assertEqual(self._figures(self._row()), (0, 0, 0))
        self.assertEqual(self._panel()['total_revisions'], 0)


# ===========================================================================
# 2. A carried copy is counted once
# ===========================================================================

class CarryTests(RevisionColumnBase):

    def test_05_a_carried_copy_is_not_counted_again(self):
        site, assignment = self._pre_release('W6-CARRY')
        self._arka_revise(site)
        self._cad_revise(site)
        self._at_head(assignment)
        self._head_fail_boq_only(site, assignment)
        # setup: both revisions rode to attempt 2 as copies.
        self.assertEqual(self._raw_revisions(), 4)
        self.assertEqual(
            ArkaSubmission.objects.exclude(reviewer_role='')
            .filter(carried_forward_from__isnull=False).count()
            + DesignFile.objects.exclude(reviewer_role='')
            .filter(carried_forward_from__isnull=False).count(), 2)

        self.assertEqual(self._figures(self._row()), (2, 1, 1))
        self.assertEqual(self._panel()['total_revisions'], 2)


# ===========================================================================
# 3. Scope — the corrections' scope
# ===========================================================================

class ScopeTests(RevisionColumnBase):

    def test_06_another_tender_and_a_deleted_site_are_not_counted(self):
        site, _ = self._pre_release('W6-IN')
        self._arka_revise(site)
        gone, _ = self._pre_release('W6-DEL')
        self._arka_revise(gone)
        self._cad_revise(gone)
        Project.objects.filter(pk=gone.pk).update(is_deleted=True)   # setup: soft delete
        # A site on another tender: _site() builds on self.program, so swap it briefly.
        home = self.program
        self.program = Program.objects.create(
            name='Test-W6-other', program_type='OPEX', client_name='CWClient',
            status='Active', short_tender_code='W6O')
        elsewhere, _ = self._pre_release('W6-OUT')
        self._arka_revise(elsewhere)
        self.program = home
        self.assertEqual(ArkaSubmission.objects.exclude(reviewer_role='').count(), 3)

        self.assertEqual(self._figures(self._row()), (1, 1, 0))
        self.assertEqual(self._panel()['total_revisions'], 1)

    def test_07_the_reached_review_gate_never_drops_a_revision_the_product_writes(self):
        # The window statuses (BC-1's minus artifacts_uploaded, ruling 5) are all
        # reached-review statuses...
        window = set(BOQ_CORRECTION_WINDOW_STATUSES) - {DESIGN_ARTIFACTS_UPLOADED}
        self.assertTrue(window <= set(PACKAGE_REVIEW_REACHED_STATUSES))
        # ...so a revision at each is counted, and still counted after a fail moves the
        # site out of review (attempt 1's qc_started keeps it reached).
        at_qc, _ = self._pre_release('W6-G1')
        self._arka_revise(at_qc)
        at_head, head_assignment = self._pre_release('W6-G2')
        self._at_head(head_assignment)
        self._cad_revise(at_head)
        self._head_fail_boq_only(at_head, head_assignment)
        self.assertNotIn(head_assignment.status, PACKAGE_REVIEW_REACHED_STATUSES)

        originals = (ArkaSubmission.objects.exclude(reviewer_role='')
                     .filter(carried_forward_from__isnull=True).count()
                     + DesignFile.objects.exclude(reviewer_role='')
                     .filter(carried_forward_from__isnull=True).count())
        self.assertEqual(originals, 2)
        self.assertEqual(self._panel()['total_revisions'], originals)

    def test_08_the_gate_applies_to_a_revision_row_outside_review(self):
        """No product path writes this row: a reviewer Arka on a site that never reached
        review (in_design, QC never started, no ledger). Written directly to show the gate
        is the corrections' gate, not a pass-through."""
        site, assignment = self._pre_release('W6-NEVER')
        DesignAssignment.objects.filter(pk=assignment.pk).update(status=DESIGN_IN_DESIGN)
        DesignAttempt.objects.filter(assignment=assignment).update(qc_started_at=None)
        ArkaSubmission.objects.filter(attempt__assignment=assignment).update(
            reviewer_role=REVIEWER_ROLE_QC, revision_reason='forged',
            designer_at_revision=self.designer)

        self.assertEqual(self._panel()['total_revisions'], 0)
        self.assertIsNone(self._row())


# ===========================================================================
# 4. No designer stamped, and the rate rule
# ===========================================================================

class UnassignedAndRateTests(RevisionColumnBase):

    def test_09_no_designer_stamped_is_its_own_footnote_and_never_the_current_one(self):
        site, _ = self._pre_release('W6-NULL')
        self._arka_revise(site)
        # setup: as if the designer had been deleted (the FK is SET_NULL).
        ArkaSubmission.objects.exclude(reviewer_role='').update(designer_at_revision=None)

        panel = self._panel()
        self.assertEqual((panel['total_revisions'], panel['unassigned_revision_sites'],
                          panel['unassigned_sites']), (0, 1, 0))
        # The site's current designer keeps the site but is NOT given the revision.
        self.assertEqual(self._figures(self._row()), (0, 0, 0))
        html = self._dashboard().content.decode()
        self.assertIn(f'1 {REVISION_NOTE}', html)
        self.assertNotIn(UNASSIGNED_NOTE, html)
        self.assertNotIn('No reviewer corrections yet.', html)

    def test_10_counts_show_below_five_and_no_revisions_rate_exists(self):
        site, _ = self._pre_release('W6-LOW')
        self._arka_revise(site)
        row = self._row()
        self.assertEqual((row['reviewed'], row['rate_shown']), (1, False))
        self.assertEqual(self._figures(row), (1, 1, 0))
        self.assertFalse([k for k in row if 'revision' in k and 'rate' in k])
        html = self._dashboard().content.decode()
        self.assertIn('Arka 1 · CAD 0', html)

    def test_11_with_five_reviewed_the_corrections_rate_ignores_revisions(self):
        sites = [self._pre_release(f'W6-F{n}')[0] for n in range(5)]
        self._set_quantity(sites[0], 12)
        self._arka_revise(sites[1])
        self._cad_revise(sites[2])
        row = self._row()
        self.assertEqual((row['reviewed'], row['sites_corrected'], row['rate'],
                          row['rate_shown']), (5, 1, 20, True))
        self.assertEqual(self._figures(row), (2, 1, 1))


# ===========================================================================
# 5. Nothing else moves; the query count
# ===========================================================================

class UnchangedTests(RevisionColumnBase):

    CORRECTION_KEYS = ('reviewed', 'sites_corrected', 'rate', 'rate_shown',
                       'quantity_edits', 'lines_added')
    WORKLOAD_KEYS = ('attempts', 'qc_failed', 'pm_change_request', 'finished',
                     'rework_attempts', 'input_attempts', 'pm_change_attempts',
                     'rework', 'input_quality', 'pm_change_multiplier')

    def _snapshot(self):
        metrics = tender_metrics(self.program)
        panel = reviewer_correction_panel(metrics['sites'])
        return (
            {r['designer'].pk: tuple(r[k] for k in self.CORRECTION_KEYS)
             for r in panel['rows']},
            panel['total_edits'], panel['unassigned_sites'],
            {r['designer'].pk: tuple(r[k] for k in self.WORKLOAD_KEYS)
             for r in metrics['workload']},
        )

    def test_12_corrections_and_rework_are_identical_with_and_without_revisions(self):
        one, _ = self._pre_release('W6-U1')
        two, _ = self._pre_release('W6-U2')
        self._released('W6-U3')                       # a finished site, so rework is a number
        self._set_quantity(one, 12)
        self._add_line(two, 2)
        before = self._snapshot()
        self.assertEqual(before[3][self.designer.pk][3], 1)      # setup: one finished site

        self._arka_revise(one)
        self._cad_revise(two)
        self._cad_revise(two, who=self.head)
        self.assertEqual(self._raw_revisions(), 3)               # setup: all went through
        self.assertEqual(self._snapshot(), before)

    def test_13_the_query_count_is_constant_as_designers_are_added(self):
        site, _ = self._pre_release('W6-Q0')
        self._arka_revise(site)
        sites = tender_metrics(self.program)['sites']
        with self.assertNumQueries(3):
            one = reviewer_correction_panel(sites)

        for n in range(1, 4):
            designer = _profile(f'w6_q{n}', 'Design')
            mine, _ = self._site_for(f'W6-Q{n}', designer)
            self._arka_revise(mine)
            self._cad_revise(mine)
        sites = tender_metrics(self.program)['sites']
        with self.assertNumQueries(3):
            four = reviewer_correction_panel(sites)
        self.assertEqual((len(one['rows']), len(four['rows'])), (1, 4))
        self.assertEqual((one['total_revisions'], four['total_revisions']), (1, 7))


# ===========================================================================
# 6. Rendering on the tender dashboard
# ===========================================================================

class RenderTests(RevisionColumnBase):

    def test_14_the_column_and_tooltip_render_after_lines_added(self):
        site, _ = self._pre_release('W6-H1')
        self._set_quantity(site, 12)
        self._arka_revise(site)
        self._cad_revise(site)
        html = self._dashboard().content.decode()
        panel_html = html.split('id="reviewerCorrections"', 1)[1]
        header = f'<th class="text-end" title="{TOOLTIP}">Arka/CAD revisions</th>'
        self.assertIn(header, panel_html)
        self.assertLess(panel_html.index('Lines added'), panel_html.index(header))
        self.assertIn('Arka 1 · CAD 1', panel_html)

    def test_15_revisions_alone_show_the_table_not_the_empty_message(self):
        site, _ = self._pre_release('W6-H2')
        self._cad_revise(site)
        panel = self._panel()
        self.assertEqual((panel['total_edits'], panel['total_revisions']), (0, 1))
        html = self._dashboard().content.decode()
        self.assertNotIn('No reviewer corrections yet.', html)
        self.assertIn('Arka 0 · CAD 1', html)

    def test_16_fully_empty_keeps_the_message(self):
        self._pre_release('W6-H3')
        html = self._dashboard().content.decode()
        self.assertIn('No reviewer corrections yet.', html)
        self.assertNotIn('Arka/CAD revisions', html)
