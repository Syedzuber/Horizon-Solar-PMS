"""
TB-4 — Design, Procurement and Attendance today on the Site Dashboard
(views_site_dashboard._breakdown_strip with show_extra, the per-site challan counts added to
site_breakdown.SiteBreakdown).

WHAT THIS PINS (each test fails if its rule is wrong)
    All RESCO        no note; every figure equals site_breakdown() over the same sites
    Mixed            Design / Procurement over RESCO sites only, a Residential site's open
                     and overdue challans left out (ruling Q1); Attendance over every site
                     type (E3); the note names all four with the base (E2)
    No RESCO         every Design / Procurement figure a dash, "DCs" still plural;
                     Attendance shown; the note says why (Q2)
    Per-site counts  dcs_*_by_site sum to the existing totals and list the same sites
    ?tender=         the dashboard's extra block equals the tender page's, byte for byte
    Tender page      the extra block renders exactly as TB-3 shipped it (TB3_EXTRA), with
                     TB-6's two row classes
    Both pages       carry the TB-6 row classes and their tint rules (one partial, S4)
    Query budget     still exactly +1 query for the strip

The snapshot is no longer pinned here: SNAP-1c (Zuber, 17 Nov, Q1) put the TB-3 figures
in its payload on purpose, and tests_dashboard_snapshot pins them equal to site_breakdown.

Run with:
    python manage.py test projects.tests_dashboard_breakdown_extra --settings=solarpms.test_settings
"""
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.db import connection
from django.template import Context, Template
from django.template.loader import render_to_string
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from .models import DESIGN_RELEASED, DESIGN_SURVEY_RETURNED, Project
from .site_breakdown import SiteBreakdown, site_breakdown
from .site_dashboard import build_site_cards
from .tests_dashboard_breakdown import URL, DashboardStripFixture
from .tests_site_breakdown import NOW, _fresh
from .tests_site_breakdown_extra import DAY, ExtraFixture
from .views_site_dashboard import _breakdown_strip

NOTE_NONE = 'Mobilized, Active, Design and Procurement apply to RESCO sites only'

#: The show_extra block as TB-3 shipped it (8963dfd), verbatim from the closing </section>
#: to the end of the include. The tender page must keep rendering exactly this.
#: TB-6 (18 Nov 2026) re-took it with the two row classes tbx-line--design and
#: tbx-line--procurement: with those two tokens and the show_extra <style> removed, the new
#: output of both pages equals 8963dfd's byte for byte, so no word or figure moved.
TB3_EXTRA = (
    '\n'
    '  <div class="apart" data-tb3-attendance>\n'
    '    <div class="fig"><div class="v">{{ breakdown.attendance_today }}</div><div class="l">Attendance today</div><div class="d">contractor entry, IST day</div></div>\n'
    '  </div>\n'
    '  <div class="tbx-lines">\n'
    '    <div class="tbx-line tbx-line--design" data-tb3-design><span class="k">Design</span> <b>{{ breakdown.design_not_started }}</b> not started · <b>{{ breakdown.design_in_progress }}</b> in progress · <b>{{ breakdown.design_released }}</b> released + BOQ</div>\n'
    '    <div class="tbx-line tbx-line--procurement" data-tb3-procurement><span class="k">Procurement</span> <b>{{ breakdown.received }}</b> received of {{ breakdown.mobilized }} mobilized · <b>{{ breakdown.pending }}</b> pending · <b>{{ breakdown.dcs_to_confirm }}</b> DC{{ breakdown.dcs_to_confirm|pluralize }} to confirm, <b>{{ breakdown.dcs_overdue }}</b> overdue</div>\n'
    '  </div>\n'
    '</div>\n'
)

#: Every StripFigures field that follows the RESCO rule (E2).
RESCO_ONLY = ('mobilized', 'active', 'design_not_started', 'design_in_progress',
              'design_released', 'received', 'pending', 'dcs_to_confirm', 'dcs_overdue')


def _extra(html):
    """The show_extra markup after the strip's </section>: the attendance box and the two rows."""
    start = html.index('<div class="apart" data-tb3-attendance>')
    end = html.index('</div>\n</div>', html.index('data-tb3-procurement', start))
    return html[start:end + len('</div>\n</div>')]


class Fixture(ExtraFixture, DashboardStripFixture):

    def setUp(self):
        # The view runs on the real clock; site_breakdown reads the IST day of that moment
        # (the fixture freezes only the no-argument localdate()), so attendance and the
        # overdue cut-off are dated from it, as in TB-3's tender page test.
        self.today = timezone.localdate(timezone.now())

    def _strip(self, profile, query=''):
        response = self._get(profile, query)
        return response, response.context['breakdown']


# ---------------------------------------------------------------------------
# 1. The three cases of the RESCO rule (E2, E3)
# ---------------------------------------------------------------------------

class RescoRuleTests(Fixture):

    def _resco_sites(self, pm):
        """Four RESCO sites: mobilized with material, two open challans (one overdue) and
        attendance; mobilized with nothing received; released, no challan; survey returned."""
        got = self._mobilized(self._resco(pm=pm))
        self._line(self._challan(got, expected=self.today - timedelta(days=1)), Decimal('1'))
        self._attendance(got, day=self.today)
        self._mobilized(self._resco(pm=pm))
        self._design(self._resco(pm=pm), DESIGN_RELEASED)
        self._design(self._resco(pm=pm), DESIGN_SURVEY_RETURNED)

    def _residential_with_everything(self, pm):
        """A Residential site meeting every TB-3 rule: site_breakdown alone counts it in
        design, mobilized, received and both challan counts; only the RESCO cut drops it."""
        site = self._mobilized(self._bare(pm=pm))
        self._line(self._challan(site, expected=self.today - timedelta(days=2)), Decimal('3'))
        self._attendance(site, day=self.today)
        return site

    def test_all_resco_shows_site_breakdown_figures_and_no_note(self):
        self._resco_sites(self.other_pm)
        response, f = self._strip(self.other_pm)
        # site_breakdown keeps only live, non-draft sites itself, so the PM's sites will do.
        b = site_breakdown(Project.objects.filter(assigned_pm=self.other_pm), timezone.now())
        self.assertIsNone(f.note)
        for field in RESCO_ONLY + ('attendance_today',):
            with self.subTest(field=field):
                self.assertEqual(getattr(f, field), getattr(b, field))
        self.assertEqual((f.design_not_started, f.design_in_progress, f.design_released,
                          f.received, f.pending, f.dcs_to_confirm, f.dcs_overdue,
                          f.attendance_today), (1, 2, 1, 1, 1, 3, 1, 1))
        self.assertNotIn('data-breakdown-note', response.content.decode())

    def test_mixed_counts_design_and_procurement_over_resco_only(self):
        self._resco_sites(self.pm)
        res = self._residential_with_everything(self.pm)
        cap = self._bare(project_type='CAPEX')
        self._attendance(cap, day=self.today)
        # Without the cut the Residential site is counted everywhere, so the cut is what
        # keeps it out below.
        whole = site_breakdown([res.pk], timezone.now())
        self.assertEqual((whole.design_in_progress, whole.received, whole.dcs_to_confirm,
                          whole.dcs_overdue), (1, 1, 2, 1))

        response, f = self._strip(self.pm)
        self.assertEqual((f.design_not_started, f.design_in_progress, f.design_released),
                         (1, 2, 1))
        self.assertEqual((f.mobilized, f.received, f.pending), (2, 1, 1))
        self.assertEqual((f.dcs_to_confirm, f.dcs_overdue), (3, 1))   # Residential's 2 + 1 left out
        self.assertEqual(f.attendance_today, 3)                       # RESCO + Residential + CAPEX
        self.assertEqual(f.note, 'Mobilized, Active, Design and Procurement count RESCO sites '
                                 'only (4 of 6)')
        html = response.content.decode()
        self.assertIn('<p class="note" data-breakdown-note>Mobilized, Active, Design and '
                      'Procurement count RESCO sites only (4 of 6)</p>', html)
        block = _extra(html)
        self.assertIn('<div class="v">3</div><div class="l">Attendance today</div>', block)
        self.assertIn('<span class="k">Design</span> <b>1</b> not started · <b>2</b> in '
                      'progress · <b>1</b> released + BOQ', block)
        self.assertIn('<span class="k">Procurement</span> <b>1</b> received of 2 mobilized · '
                      '<b>1</b> pending · <b>3</b> DCs to confirm, <b>1</b> overdue', block)

    def test_no_resco_site_draws_dashes_and_still_shows_attendance(self):
        self._residential_with_everything(self.other_pm)
        self._bare(project_type='CAPEX', pm=self.other_pm)
        response, f = self._strip(self.other_pm)
        for field in RESCO_ONLY:
            with self.subTest(field=field):
                self.assertIsNone(getattr(f, field))
        self.assertEqual((f.attendance_today, f.note), (1, NOTE_NONE))
        html = response.content.decode()
        block = _extra(html)
        self.assertIn('<div class="v">1</div><div class="l">Attendance today</div>', block)
        self.assertIn('<span class="k">Design</span> <b>—</b> not started · <b>—</b> in '
                      'progress · <b>—</b> released + BOQ', block)
        self.assertIn('<span class="k">Procurement</span> <b>—</b> received of — mobilized · '
                      '<b>—</b> pending · <b>—</b> DCs to confirm, <b>—</b> overdue', block)
        self.assertNotIn('None', block)
        self.assertIn(f'data-breakdown-note>{NOTE_NONE}</p>', html)

    def test_no_site_at_all_shows_zeros_and_no_note(self):
        _, f = self._strip(self.other_pm)
        self.assertEqual([getattr(f, k) for k in RESCO_ONLY + ('attendance_today',)], [0] * 10)
        self.assertIsNone(f.note)


# ---------------------------------------------------------------------------
# 2. Per-site challan counts (Q1)
# ---------------------------------------------------------------------------

class PerSiteCountTests(Fixture):

    def test_per_site_counts_sum_to_the_totals_and_list_the_same_sites(self):
        two = self._mobilized(self._resco())                          # 1 open, no date
        self._challan(two, expected=DAY - timedelta(days=1))          # + 1 open, overdue
        one = self._resco()
        self._challan(one, expected=DAY)                              # due today: not overdue
        none = self._mobilized(self._resco())
        self._dc(none, 'Received')                                    # closed, not counted
        res = self._bare()
        self._challan(res, expected=DAY - timedelta(days=3))
        b = site_breakdown([two.pk, one.pk, none.pk, res.pk], NOW)
        self.assertEqual(set(b.dcs_to_confirm_by_site),
                         {(two.pk, 2), (one.pk, 1), (none.pk, 1), (res.pk, 1)})
        self.assertEqual(set(b.dcs_overdue_by_site), {(two.pk, 1), (res.pk, 1)})
        self.assertEqual(sum(n for _, n in b.dcs_to_confirm_by_site), b.dcs_to_confirm)
        self.assertEqual(sum(n for _, n in b.dcs_overdue_by_site), b.dcs_overdue)
        self.assertEqual(tuple(pk for pk, _ in b.dcs_to_confirm_by_site), b.dcs_to_confirm_site_ids)
        self.assertEqual(tuple(pk for pk, _ in b.dcs_overdue_by_site), b.dcs_overdue_site_ids)

    def test_still_one_query(self):
        for _ in range(5):
            site = self._mobilized(self._resco())
            self._challan(site, expected=DAY - timedelta(days=1))
        with CaptureQueriesContext(connection) as captured:
            site_breakdown(self.program.sites.all(), NOW)
        self.assertEqual(len(captured.captured_queries), 1)


# ---------------------------------------------------------------------------
# 3. The tender filter and the tender page
# ---------------------------------------------------------------------------

class TenderPageTests(Fixture):

    def setUp(self):
        super().setUp()
        got = self._mobilized(self._resco())
        self._line(self._challan(got, expected=self.today - timedelta(days=1)), Decimal('1'))
        self._attendance(got, day=self.today)
        self._mobilized(self._resco(pm=self.other_pm))
        self._design(self._resco(), DESIGN_SURVEY_RETURNED)
        # Outside the tender: a Residential site with a late challan, so the CEO's whole
        # portfolio is mixed while the tender itself is all RESCO.
        outside = self._mobilized(self._bare())
        self._challan(outside, expected=self.today - timedelta(days=4))

    def _tender_page(self):
        client = Client()
        client.force_login(_fresh(self.ceo.user))
        response = client.get(reverse('program_detail', args=[self.program.pk]))
        self.assertEqual(response.status_code, 200)
        return response

    def test_tender_filter_shows_the_tender_pages_figures(self):
        dash = self._get(self.ceo, f'?tender={self.program.pk}').content.decode()
        tender = self._tender_page().content.decode()
        self.assertEqual(_extra(dash), _extra(tender))
        self.assertIn('<b>2</b> in progress', _extra(dash))

    def test_tender_page_extra_block_is_byte_identical_to_tb3(self):
        response = self._tender_page()
        html = response.content.decode()
        rendered = Template(TB3_EXTRA).render(Context({'breakdown': response.context['breakdown']}))
        start = html.index('</section>', html.index('<section class="tb mb-4"')) + len('</section>')
        self.assertEqual(html[start:start + len(rendered)], rendered)

    def test_both_pages_draw_design_and_procurement_as_tinted_strips(self):
        # TB-6 (S4): one partial styles both pages, so each page carries the two row
        # classes once and the show_extra rules that tint them.
        pages = {'tender': self._tender_page().content.decode(),
                 'dashboard': self._get(self.ceo).content.decode()}
        for name, html in pages.items():
            with self.subTest(page=name):
                self.assertEqual(html.count('<div class="tbx-line tbx-line--design" '
                                            'data-tb3-design>'), 1)
                self.assertEqual(html.count('<div class="tbx-line tbx-line--procurement" '
                                            'data-tb3-procurement>'), 1)
                self.assertIn('.tbx .tbx-line--design { background: var(--tbx-design-bg);', html)
                self.assertIn('.tbx .tbx-line--procurement { background: var(--tbx-proc-bg);',
                              html)

    def test_include_renders_tb3_bytes_for_any_numbers(self):
        # pluralize's singular and plural, and zeros, through the TB-4 dash handling.
        for to_confirm in (0, 1, 2):
            b = SiteBreakdown((1, 2, 3), (1, 2), (1,), (3,), design_not_started_ids=(3,),
                              design_in_progress_ids=(1,), design_released_ids=(2,),
                              received_ids=(1,), pending_ids=(2,), attendance_today_ids=(1, 3),
                              dcs_to_confirm=to_confirm, dcs_overdue=min(to_confirm, 1))
            with self.subTest(dcs_to_confirm=to_confirm):
                html = render_to_string('projects/_site_breakdown_strip.html',
                                        {'breakdown': b, 'show_extra': True})
                tail = html[html.index('</section>') + len('</section>'):]
                self.assertEqual(tail, Template(TB3_EXTRA).render(Context({'breakdown': b})))


# ---------------------------------------------------------------------------
# 4. Query budget
# ---------------------------------------------------------------------------

class BudgetTests(Fixture):

    def setUp(self):
        super().setUp()
        for _ in range(3):
            site = self._mobilized(self._resco())
            self._challan(site, expected=self.today - timedelta(days=1))
            self._attendance(site, day=self.today)
        res = self._mobilized(self._bare())
        self._attendance(res, day=self.today)

    def test_the_strip_still_costs_exactly_one_query(self):
        client = Client()
        client.force_login(_fresh(self.pm.user))
        client.get(URL)                                               # warm the session
        counts = []
        for stub in (False, True):
            with CaptureQueriesContext(connection) as captured:
                if stub:
                    with mock.patch('projects.views_site_dashboard.site_breakdown',
                                    return_value=SiteBreakdown((), (), (), ())):
                        response = client.get(URL)
                else:
                    response = client.get(URL)
            self.assertEqual(response.status_code, 200)
            counts.append(len(captured.captured_queries))
        self.assertEqual(counts[0] - counts[1], 1)

    def test_breakdown_strip_alone_is_one_query(self):
        live = [c for c in build_site_cards(self.pm.user, today=self.today) if not c.is_draft]
        with CaptureQueriesContext(connection) as captured:
            f = _breakdown_strip(live, timezone.now())
        self.assertEqual(len(captured.captured_queries), 1)
        self.assertEqual((f.dcs_to_confirm, f.dcs_overdue, f.attendance_today), (6, 3, 4))
