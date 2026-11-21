"""W-3 — dashboards, chips and metrics follow the single Arka gate W-1 built.

W-1 (d093fd8) removed the Design Head's separate Arka approval: Design QC's verdict
decides. These tests pin the READINGS that had to follow:

  * RULE 1 — "Arka approved" means Design QC approved it, through ONE predicate,
    design_metrics.arka_is_approved(). A QC-approved Arka shows as approved on every chip,
    stage, "pending at", "waiting on" and capacity reading.
  * RULE 1, THE GUARD — an Arka the Head rejected under the earlier two-step review is
    never counted as approved.
  * RULE 2 — no "waiting on the Head for the Arka" anywhere. A legacy site still parked at
    `awaiting_head_arka` reads as pending at Design QC.
  * No Head-Arka tile, count, stage or queue entry is rendered.
  * RULE 3 — old Head Arka verdicts stay readable, labelled as the earlier two-step
    review; the overturn rate says its Arka half no longer grows.
  * RULE 4 — capacity agrees between the tender dashboard and analytics.

Fixtures are W-1's own (SingleGateBase): every step goes through the real endpoints, and
the two states nothing writes any more (a legacy `awaiting_head_arka` site, and an Arka
the Head rejected) are placed the way the pre-W-1 code placed them.
"""
from datetime import timedelta
from decimal import Decimal

from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from .design_analytics import METRIC_CATALOGUE, analytics_dataset, m_queue_latency
from .design_metrics import (
    HEAD_ACTION_STAGES, QC_ACTION_STAGES, STAGE_LABELS, _classify, arka_is_approved,
    tender_metrics,
)
from .design_views import (
    _approved_arka, _current_attempt, apply_design_status, design_head_dashboard_counts,
    design_qc_dashboard_counts, designer_dashboard_context,
)
from .models import (
    ARKA_APPROVED, ARKA_PENDING, ARKA_REJECTED,
    DESIGN_ARKA_REJECTED, DESIGN_ARKA_SUBMITTED, DESIGN_AWAITING_HEAD_ARKA, ERR_LAYOUT,
    PENDING_AT_DESIGNER, PENDING_AT_QC, Project, design_pending_at,
)
from .tender_stages import tender_site_list
from .tests_design_arka_single_gate import SingleGateBase


class DisplayBase(SingleGateBase):

    def _qc_approved(self, code):
        """Arka approved by Design QC after W-1: head_verdict stays 'pending' for good."""
        site = self._arka_submitted(code)
        self._step(self.qc, 'design_arka_approve', site, DESIGN_ARKA_SUBMITTED)
        arka = self._arka(site)
        self.assertEqual((arka.verdict, arka.head_verdict), (ARKA_APPROVED, ARKA_PENDING))
        return site

    def _legacy(self, code):
        site = self._arka_submitted(code)
        self._make_legacy(site)
        return site

    def _head_rejected(self, code):
        """An Arka QC passed and the Head overturned BEFORE W-1. The pre-W-1 Head rejection
        wrote exactly this: QC's approval kept, the Head's rejection beside it, and the site
        sent to `arka_rejected`. Nothing writes it now, so it is placed directly."""
        site = self._arka_submitted(code)
        arka = self._arka(site)
        arka.verdict = ARKA_APPROVED
        arka.reviewed_by = self.qc
        arka.head_verdict = ARKA_REJECTED
        arka.head_rejection_reason = 'string layout leaves no walkway'
        arka.head_failure_category = ERR_LAYOUT
        arka.head_reviewed_by = self.head
        arka.head_reviewed_at = timezone.now()
        arka.head_overturned_qc = True
        arka.save()
        apply_design_status(self._a(site), DESIGN_ARKA_REJECTED, self.head,
                            'legacy: Arka rejected by the Design Head (pre-W-1)',
                            'design_arka_qc_rejected')
        return site

    def _metrics_site(self, site):
        rows = [s for s in tender_metrics(self.program)['sites']
                if s['project'].pk == site.pk]
        self.assertEqual(len(rows), 1)
        return rows[0]

    def _card(self, site):
        """The design-dashboard tender card's context and its rendered chip line, through
        the same function and partial dashboard/design.html uses. A fresh Project, as the
        dashboard loads one: the fixture's instance caches its assignment from creation."""
        site = Project.objects.get(pk=site.pk)
        ctx = designer_dashboard_context(self.designer, [site])[site.pk]
        html = render_to_string('projects/design/_dashboard_design_chips.html',
                                {'p': {'design': ctx}})
        return ctx, html

    def _ceo_row(self, site):
        rows = tender_site_list(Project.objects.filter(pk=site.pk), timezone.localdate(),
                                timezone.now() - timedelta(days=7))
        self.assertEqual(len(rows), 1)
        return rows[0]


# ===========================================================================
# Rule 1 — a QC-approved Arka reads as approved everywhere
# ===========================================================================

class QcApprovedArkaReadsApprovedTests(DisplayBase):

    def test_the_predicate_and_the_view_helper_agree(self):
        site = self._qc_approved('W3-OK-PRED')
        arka = self._arka(site)
        self.assertTrue(arka_is_approved(arka))
        self.assertEqual(_approved_arka(_current_attempt(self._a(site))), arka)

    def test_stage_pending_at_and_waiting_on_name_the_designer(self):
        site = self._qc_approved('W3-OK-STAGE')
        self.assertEqual(self._metrics_site(site)['stage'], 'arka_approved')
        self.assertEqual(design_pending_at(self._a(site), self._arka(site)),
                         PENDING_AT_DESIGNER)
        self.assertNotIn(self._ceo_row(site)['waiting_on'], ('Design QC', 'Design Head'))

    def test_the_design_dashboard_card_chip_says_approved(self):
        """THE W-3 BUG. The chip read head_verdict, which stays 'pending' on every Arka
        since W-1, so each one showed as still awaiting Design QC."""
        site = self._qc_approved('W3-OK-CARD')
        ctx, html = self._card(site)
        self.assertTrue(ctx['arka_approved'])
        self.assertIn('Arka approved — CAD and BOQ outstanding', html)
        self.assertNotIn('Arka awaiting Design QC', html)
        self.assertNotIn('Design Head', html)

    def test_the_workspace_chips_and_arka_history_show_no_head_step(self):
        site = self._qc_approved('W3-OK-WS')
        r = self._get(self.head, 'design_head_review', site)
        self.assertContains(r, 'Arka approved — CAD and BOQ outstanding')
        self.assertContains(r, 'Arka v1: Approved')
        self.assertNotContains(r, 'awaiting Design Head')
        # _design_artifacts.html: no "Awaiting Head" chip on a pending head_verdict.
        self.assertNotContains(r, 'Awaiting Head')
        self.assertNotContains(r, 'earlier two-step review')

    def test_the_tender_and_qc_dashboards_show_it_with_the_designer(self):
        site = self._qc_approved('W3-OK-DASH')
        m = tender_metrics(self.program)
        counts = {s['key']: s['count'] for s in m['stages']}
        self.assertEqual(counts['arka_approved'], 1)
        self.assertEqual(counts['arka_submitted'], 0)
        self.assertEqual(m['qc_queue']['arka_count'], 0)
        self.assertEqual(m['queue']['arka_count'], 0)

        self.client.force_login(self.head.user)
        for name in ('design_tender_dashboard', 'design_qc_dashboard'):
            r = self.client.get(reverse(name, kwargs={'pk': self.program.pk})
                                + '?stage=arka_approved')
            self.assertEqual([s['project'].pk for s in r.context['drill']], [site.pk], name)
            self.assertNotContains(r, 'Arka awaiting Design Head')


# ===========================================================================
# Rule 1, the guard — a Head-rejected legacy Arka is never approved
# ===========================================================================

class HeadRejectedLegacyArkaTests(DisplayBase):

    def test_it_is_not_approved_by_the_predicate_or_the_helper(self):
        site = self._head_rejected('W3-REJ-PRED')
        arka = self._arka(site)
        self.assertEqual(arka.verdict, ARKA_APPROVED)    # QC did pass it
        self.assertFalse(arka_is_approved(arka))
        self.assertIsNone(_approved_arka(_current_attempt(self._a(site))))

    def test_it_counts_in_neither_capacity_figure(self):
        site = self._head_rejected('W3-REJ-CAP')
        self.assertIsNone(self._metrics_site(site)['capacity_kw'])
        self.assertEqual(tender_metrics(self.program)['capacity']['contributing'], 0)
        rows = [s for s in analytics_dataset([self.program])['sites']
                if s['project'].pk == site.pk]
        self.assertIsNone(rows[0]['capacity_kw'])

    def test_the_stage_split_does_not_call_it_approved(self):
        """Even at `arka_submitted` (a hold lifted back there, say) the split must not file
        it as approved: the predicate, not QC's verdict alone, decides."""
        site = self._head_rejected('W3-REJ-STAGE')
        a = self._a(site)
        a.status = DESIGN_ARKA_SUBMITTED      # in memory only: _classify is pure
        self.assertEqual(_classify(a, self._arka(site)), 'arka_submitted')

    def test_the_card_chip_is_not_green(self):
        site = self._head_rejected('W3-REJ-CARD')
        ctx, html = self._card(site)
        self.assertFalse(ctx['arka_approved'])
        self.assertNotIn('Arka approved', html)

    def test_its_head_verdict_stays_readable_as_history(self):
        """Rule 3: the Head's old verdict is shown, labelled as the earlier review."""
        site = self._head_rejected('W3-REJ-HIST')
        r = self._get(self.head, 'design_head_review', site)
        self.assertContains(r, 'Head rejected (earlier two-step review)')
        self.assertContains(r, 'Rejected by Design Head')


# ===========================================================================
# Rule 2 — a legacy awaiting_head_arka site is pending at Design QC
# ===========================================================================

class LegacySitePendingAtQcTests(DisplayBase):

    def test_it_is_filed_as_an_arka_awaiting_design_qc(self):
        site = self._legacy('W3-LEG-STAGE')
        self.assertEqual(self._metrics_site(site)['stage'], 'arka_submitted')
        self.assertIn('arka_submitted', QC_ACTION_STAGES)

    def test_it_is_in_design_qcs_queue_and_not_the_heads(self):
        self._legacy('W3-LEG-Q')
        m = tender_metrics(self.program)
        self.assertEqual(m['qc_queue']['arka_count'], 1)
        self.assertEqual(m['queue']['arka_count'], 0)
        qc_rows = [r for r in m['qc_attention'] if r['project'].project_id == 'W3-LEG-Q']
        self.assertEqual(len(qc_rows), 1)
        head_rows = [r for r in m['attention'] if 'Waiting on you' in r['reason']
                     and r['project'].project_id == 'W3-LEG-Q']
        self.assertEqual(head_rows, [])

    def test_pending_at_and_waiting_on_name_design_qc(self):
        site = self._legacy('W3-LEG-WHO')
        self.assertEqual(design_pending_at(self._a(site), self._arka(site)), PENDING_AT_QC)
        row = self._ceo_row(site)
        self.assertEqual(row['waiting_on'], 'Design QC')
        self.assertEqual(row['design_status'], 'Arka awaiting Design QC')

    def test_both_count_strips_put_it_with_design_qc(self):
        self._legacy('W3-LEG-STRIP')
        head = design_head_dashboard_counts(self.head.user)
        self.assertEqual(head['awaiting_arka'], 1)
        qc = design_qc_dashboard_counts(self.qc.user)
        self.assertEqual(qc['awaiting_arka'], 1)
        self.assertEqual(qc['open_pool'], 1)

    def test_every_chip_and_card_says_design_qc(self):
        site = self._legacy('W3-LEG-CHIP')
        ctx, html = self._card(site)
        self.assertIn('Arka awaiting Design QC', html)
        self.assertNotIn('Design Head', html)
        # The designer card's "waiting" line (_DESIGNER_ACTIONS) names Design QC too.
        self.assertIn('Design QC to move it on', ctx['waiting'])
        self.assertNotIn('Design Head', ctx['waiting'])

        r = self._get(self.qc, 'design_head_review', site)
        self.assertContains(r, 'Design: Arka awaiting Design QC')
        self.assertNotContains(r, 'Arka — awaiting Design Head')

    def test_the_dashboard_drill_downs_say_design_qc(self):
        self._legacy('W3-LEG-DRILL')
        self.client.force_login(self.head.user)
        for name in ('design_tender_dashboard', 'design_qc_dashboard'):
            r = self.client.get(reverse(name, kwargs={'pk': self.program.pk})
                                + '?stage=arka_submitted')
            self.assertEqual(len(r.context['drill']), 1, name)
            self.assertContains(r, 'Arka awaiting Design QC')
            # The package tile "Package awaiting Design Head" is real; only Arka ones go.
            self.assertNotContains(r, 'Arka awaiting Design Head')
            self.assertNotContains(r, 'Arka — awaiting Design Head')


# ===========================================================================
# No Head-Arka tile, count, stage or queue entry
# ===========================================================================

class NoHeadArkaTileTests(DisplayBase):

    def test_no_stage_or_head_action_stage_for_it(self):
        self.assertNotIn('awaiting_head_arka', STAGE_LABELS)
        self.assertNotIn('awaiting_head_arka', HEAD_ACTION_STAGES)

    def test_the_head_counts_carry_no_head_arka_key(self):
        self._legacy('W3-TILE-KEY')
        self.assertNotIn('awaiting_head_arka', design_head_dashboard_counts(self.head.user))

    def test_no_head_arka_tile_is_rendered(self):
        self._legacy('W3-TILE-R')
        self._qc_approved('W3-TILE-OK')
        self.client.force_login(self.head.user)

        r = self.client.get(reverse('dashboard_design'))
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, 'Arka awaiting <strong>you</strong>', html=False)
        self.assertContains(r, 'Arka with Design QC')

        r = self.client.get(reverse('design_tender_dashboard', kwargs={'pk': self.program.pk}))
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, 'Arka awaiting your approval')
        self.assertNotContains(r, 'Arka awaiting Design Head')
        self.assertContains(r, 'Packages awaiting your review')


# ===========================================================================
# Rule 3 — the overturn rate and queue latency say they are history
# ===========================================================================

class HistoryLabelTests(DisplayBase):

    def test_the_overturn_rate_says_its_arka_half_stopped_growing(self):
        metric = next(m for m in METRIC_CATALOGUE if m.key == 'overturn_rate')
        self.assertIn('will not grow', metric.caveat)
        self.assertIn('does not mean no overturns', metric.caveat)

    def test_the_head_arka_latency_row_is_labelled_as_the_removed_step(self):
        labels = [r['label'] for r in m_queue_latency({'arkas': [], 'attempts': []})['rows']]
        self.assertIn('Arka — waiting for a Head verdict (earlier two-step review; '
                      'step removed)', labels)


# ===========================================================================
# Rule 4 — capacity agrees between the tender dashboard and analytics
# ===========================================================================

class CapacityAgreesTests(DisplayBase):

    def test_the_two_figures_count_the_same_sites(self):
        ok       = self._qc_approved('W3-CAP-OK')
        legacy   = self._legacy('W3-CAP-LEG')
        rejected = self._head_rejected('W3-CAP-REJ')
        pending  = self._arka_submitted('W3-CAP-PEND')

        capacity = tender_metrics(self.program)['capacity']
        analytics = {s['project'].pk: s['capacity_kw']
                     for s in analytics_dataset([self.program])['sites']}
        dashboard = {s['project'].pk: s['capacity_kw']
                     for s in tender_metrics(self.program)['sites']}

        self.assertEqual(analytics, dashboard)
        self.assertEqual(dashboard, {ok.pk: Decimal('120.00'), legacy.pk: Decimal('120.00'),
                                     rejected.pk: None, pending.pk: None})
        self.assertEqual(capacity['contributing'], 2)
        self.assertEqual(capacity['designed_kw'], Decimal('240.00'))
        self.assertEqual(sum(v for v in analytics.values() if v is not None),
                         capacity['designed_kw'])
