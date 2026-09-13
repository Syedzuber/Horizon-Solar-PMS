"""A zero figure renders as zero; a missing figure still renders as missing (§D5, §D6).

WHY THIS FILE EXISTS
--------------------
Prompt 3.1-ACC (07bdaa2) made 0.0 the ordinary Rework reading for a clean designer, since
the first attempt is no longer rework. The tender dashboard guarded its Rework and Input
cells with `{% if w.rework %}`. Python's 0.0 is falsy, so every clean designer rendered as
"—" above a tooltip claiming there were no released sites, on a tender that had five.

The fix is display only. It is tested IN BOTH DIRECTIONS because the obvious regression is
the inverse bug: a designer with no finished site, whose figure is genuinely None, starting
to read as 0.0. Each screen is rendered, and the rendered HTML is asserted, not the context.

  - `zd_clean`: five released sites and one parked in the PM gate, first attempts only.
    Rework 0.0 and Input 0.0, over 6 finished sites.
  - `zd_fresh`: one site still in design. Nothing is finished, so both figures are None.

On quality analytics the "missing" rendering is _qa_figure's `Insufficient data (n=0)`,
not a dash. That partial is the only place a rate becomes text, and below 5 it refuses a
number by design. So the inverse assertion there is "no number", in that page's own terms.

The parked site is what makes the change-request-rate column's switch from `r.released` to
`r.finished` observable: without it the two counts are equal. It is parked through the one
admitted fixture writer, tests_design_pm_gate_fixtured.PmGateBase._park_in_pm_gate.
"""
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import (
    DesignAnalyticsPreference, DesignAssignment, DesignAttempt, Program, Project,
    ATTEMPT_REASON_INITIAL, DESIGN_IN_DESIGN, DESIGN_RELEASED,
)
# The ONE fixture writer of the PM-approval status, reused rather than duplicated.
from .tests_design_pm_gate_fixtured import PmGateBase


def _profile(username, role, is_design_head=False):
    """A post_save signal auto-creates the UserProfile; fetch and set, never create."""
    user = User.objects.create_user(username=username, password='x')
    profile = user.profile
    profile.role = role
    profile.is_active = True
    profile.is_design_head = is_design_head
    profile.save()
    return profile


def _flat(html):
    """Whitespace-collapsed text, so a caption wrapped across template lines still matches."""
    return ' '.join(html.split())


class ZeroDisplayBase(TestCase):

    def setUp(self):
        self.head  = _profile('zd_head',  'Design', is_design_head=True)
        self.clean = _profile('zd_clean', 'Design')
        self.fresh = _profile('zd_fresh', 'Design')
        self.program = Program.objects.create(
            name='Test-ZeroDisplay', program_type='OPEX', client_name='ZD',
            status='Active', short_tender_code='ZD')
        for i in range(1, 6):
            self._site(f'ZD-C{i}', self.clean, released=True)
        PmGateBase._park_in_pm_gate(self, self._site('ZD-C6', self.clean, released=False))
        self._site('ZD-F1', self.fresh, released=False)
        self.client.force_login(self.head.user)

    def _site(self, code, designer, released):
        site = Project(
            project_id=code, customer_name='ZD', customer_phone='9876543210',
            site_address='1 Sun Rd', city='Delhi', project_type='OPEX',
            program=self.program, site_code=code,
            dc_capacity_kw=Decimal('100.00'), status='Draft')
        site.save()
        a = DesignAssignment.objects.create(
            project=site, status=DESIGN_RELEASED if released else DESIGN_IN_DESIGN,
            assigned_to=designer, assigned_by=self.head, assigned_at=timezone.now(),
            released_at=timezone.now() if released else None,
            current_attempt_number=1,
            survey_file_bucket='b', survey_file_path=f'{code}/survey/x.pdf')
        DesignAttempt.objects.create(assignment=a, attempt_number=1,
                                     opened_reason=ATTEMPT_REASON_INITIAL)
        return a


# ===========================================================================
# The tender dashboard — the Rework and Input cells, and the captions
# ===========================================================================

class TenderDashboardZeroTests(ZeroDisplayBase):

    def setUp(self):
        super().setUp()
        response = self.client.get(reverse('design_tender_dashboard',
                                           kwargs={'pk': self.program.pk}))
        self.assertEqual(response.status_code, 200)
        self.html = response.content.decode()

    def _row(self, profile):
        """The workload table row for one designer, from <tr> to </tr>."""
        anchor = self.html.index(f'?designer={profile.pk}"')
        start = self.html.rindex('<tr', 0, anchor)
        return self.html[start:self.html.index('</tr>', anchor)]

    def test_a_clean_designer_reads_zero_not_the_dash(self):
        row = self._row(self.clean)
        self.assertIn('<strong>0.0&times;</strong>', row)                      # Rework
        self.assertIn('<strong class="text-warning">0.0&times;</strong>', row)  # Input
        self.assertNotIn('&mdash;', row)
        self.assertNotIn('No finished sites yet', row)
        # The sort keys carry the zero too, so a clean designer sorts as 0, not as "no data".
        self.assertEqual(row.count('data-v="0.0"'), 2)
        self.assertNotIn('data-v="-1"', row)

    def test_a_designer_with_no_finished_site_still_reads_the_dash(self):
        row = self._row(self.fresh)
        self.assertEqual(row.count('&mdash;'), 2)
        self.assertEqual(row.count('title="No finished sites yet — nothing to divide by"'), 2)
        self.assertNotIn('&times;', row)
        self.assertEqual(row.count('data-v="-1"'), 2)

    def test_the_false_tooltips_are_gone(self):
        self.assertNotIn('No released sites yet', self.html)
        self.assertNotIn('no input-caused rework', self.html)

    def test_the_captions_describe_what_the_figures_now_measure(self):
        text = _flat(self.html)
        self.assertIn('Sites and kW are current load: they exclude finished work '
                      '(released, or awaiting PM approval).', text)
        self.assertIn('designer-caused attempts (attempts a Group A failure opened, plus '
                      'uncategorised pre-Part-9 QC failures) &divide; finished sites '
                      '(released, or awaiting PM approval)', text)
        self.assertIn('a clean record reads 0.0&times;', text)
        self.assertIn('A dash means the designer has no finished site yet.', text)
        self.assertNotIn('&divide; released sites', text)
        # ZD-F1 is the one unfinished site with no approved date. The parked site owes none.
        self.assertIn('1 unfinished site (not yet released or awaiting PM approval) has no '
                      'approved due date', text)


# ===========================================================================
# Quality analytics — the rework panel, the change-request-rate panel, the legend
# ===========================================================================

class QualityAnalyticsZeroTests(ZeroDisplayBase):

    def setUp(self):
        super().setUp()
        # The rework panel is optional. Switch it on the way the selector would.
        DesignAnalyticsPreference.objects.create(profile=self.head,
                                                 metrics=['rework_multiplier'])
        response = self.client.get(reverse('design_quality_analytics_tender',
                                           kwargs={'pk': self.program.pk}))
        self.assertEqual(response.status_code, 200)
        self.html = response.content.decode()

    def _panel(self, header):
        """The table whose COLUMN HEADER is `header`. Anchored on the <th> markup because the
        same words also appear in the metric selector's catalogue description, higher up
        the page, and a bare text search lands in the wrong panel."""
        start = self.html.index(f'<th class="small text-muted">{header}</th>')
        return self.html[start:self.html.index('</table>', start)]

    @staticmethod
    def _row(panel, label):
        start = panel.index(f'<td>{label}</td>')
        return panel[start:panel.index('</tr>', start)]

    def test_a_clean_designer_reads_zero_in_the_rework_panel(self):
        row = self._row(self._panel('Designer-caused attempts per finished site'), 'zd_clean')
        self.assertIn('<span class="qa-figure">0.0</span>', row)
        self.assertIn('n=6', row)
        self.assertNotIn('Insufficient data', row)

    def test_a_designer_with_no_finished_site_shows_no_number(self):
        row = self._row(self._panel('Designer-caused attempts per finished site'), 'zd_fresh')
        self.assertIn('Insufficient data (n=0)', row)
        self.assertNotIn('qa-figure', row)

    def test_the_change_request_rate_columns_read_finished_sites(self):
        panel = self._panel('Accepted per finished site')
        self.assertIn('>Finished sites</th>', panel)
        self.assertNotIn('Released sites', panel)
        self.assertNotIn('per released site', panel)
        # No site has an assigned PM, so all seven land on "Unassigned". Six are finished
        # (five released, plus the one in the PM gate); five are released. The column
        # shows 6, the figure's own divisor, so it agrees with the n= beside it.
        row = self._row(panel, 'Unassigned')
        self.assertIn('n=6', row)
        self.assertTrue(row.rstrip().endswith('<td class="text-end small">6</td>'), row)

    def test_the_sample_size_legend_names_no_single_denominator(self):
        text = _flat(self.html)
        self.assertIn('Under 5 sites or reviews in the denominator, no figure is shown', text)
        self.assertNotIn('Under 5 released sites', text)
