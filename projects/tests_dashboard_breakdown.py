"""
TB-2 — the activated-site strip on the Site Dashboard (views_site_dashboard._breakdown_strip,
the shared include projects/_site_breakdown_strip.html).

WHAT THIS PINS (each test fails if its rule is wrong)
    Same sites as the tile   the strip's Sites in execution equals the old tile for CEO, a
                             PM, a coordinator and under ?tender=; a PM sees only their own
    Same numbers as TB-1     under ?tender=<pk> all four figures equal site_breakdown() for
                             that tender and the tender page's own strip
    The tile                 "Sites in execution" is no longer drawn, every other tile is;
                             kpi_tiles() output is unchanged (one full output pinned)
    RESCO rule (P3)          all RESCO: no note; mixed: Mobilized / Active over RESCO only
                             and a note with the base; no RESCO: dashes and a note;
                             Sites in execution and Active today stay over every site
    Tender page              its strip HTML is byte-identical to the markup TB-1 shipped
    Query budget             exactly +1 query for the strip; none for an empty set

Run with:
    python manage.py test projects.tests_dashboard_breakdown --settings=solarpms.test_settings
"""
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.db import connection
from django.template import Context, Template
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from .models import DESIGN_IN_DESIGN, Program, Project, Task
from .site_breakdown import SiteBreakdown, site_breakdown
from .site_dashboard import build_site_cards
from .tests_sched2_scheduler import CIVIL
from .tests_site_breakdown import BreakdownFixture, _fresh
from .views_site_dashboard import _params, kpi_tiles

URL = reverse('site_dashboard')

#: The strip as TB-1 shipped it inside program_detail.html (248f52d), verbatim. The shared
#: include must render this byte for byte on the tender page.
TB1_STRIP = (
    '<section class="tb mb-4" aria-label="Activated sites">\n'
    '  <div class="chain">\n'
    '    <div class="fig"><div class="v">{{ breakdown.in_execution }}</div><div class="l">Sites in execution</div>{% if program.planned_site_count %}<div class="d">of {{ program.planned_site_count }} planned</div>{% endif %}</div>\n'
    '    <span class="arrow" aria-hidden="true">›</span>\n'
    '    <div class="fig"><div class="v">{{ breakdown.mobilized }}</div><div class="l">Mobilized</div><div class="d">DC + design started</div></div>\n'
    '    <span class="arrow" aria-hidden="true">›</span>\n'
    '    <div class="fig"><div class="v">{{ breakdown.active }}</div><div class="l">Active</div><div class="d">Civil In Progress/Done</div></div>\n'
    '  </div>\n'
    '  <div class="apart">\n'
    '    <div class="fig"><div class="v">{{ breakdown.active_today }}</div><div class="l">Active today</div><div class="d">any PMS update, last 24 h</div></div>\n'
    '  </div>\n'
    '</section>'
)

NOTE_NONE = 'Mobilized and Active apply to RESCO sites only'


def _section(html):
    start = html.index('<section class="tb')
    return html[start:html.index('</section>', start) + len('</section>')]


def _figures(b):
    return (b.in_execution, b.mobilized, b.active, b.active_today)


class DashboardStripFixture(BreakdownFixture):

    def _get(self, profile, query=''):
        client = Client()
        client.force_login(_fresh(profile.user))
        response = client.get(URL + query)
        self.assertEqual(response.status_code, 200)
        return response

    @staticmethod
    def _tile(response):
        return next(t for t in response.context['kpis'] if t['label'] == 'Sites in execution')

    def _activity(self, site):
        self._log(site, 'task_status_done', timezone.now() - timedelta(hours=1), actor=self.pm)

    def _active(self, site):
        """`site` (activated, so it has the Civil anchor) Mobilized and Active."""
        self._mobilized(site)
        self._civil(site, status=Task.IN_PROGRESS)
        return site


# ---------------------------------------------------------------------------
# 1. The strip counts exactly the tile's sites
# ---------------------------------------------------------------------------

class SameSitesAsTheTileTests(DashboardStripFixture):

    def setUp(self):
        self.mine = self._active(self._activated())
        self._activity(self.mine)
        self.mine_too = self._mobilized(self._resco())
        self.theirs = self._mobilized(self._resco(pm=self.other_pm))
        self.theirs.coordinators.add(self.coord)
        self.elsewhere = self._resco(program=None, pm=self.other_pm)
        self._resco(status='Draft')                       # never counted anywhere
        self.second = Program.objects.create(name='TB Two', program_type='OPEX',
                                             client_name='C', status='Active')
        self._resco(program=self.second)

    def test_equals_the_old_tile_for_ceo_pm_and_coordinator(self):
        for profile, expected in ((self.ceo, 5), (self.pm, 3), (self.coord, 1)):
            with self.subTest(role=profile.role):
                response = self._get(profile)
                self.assertEqual(response.context['breakdown'].in_execution,
                                 self._tile(response)['value'])
                self.assertEqual(response.context['breakdown'].in_execution, expected)

    def test_equals_the_old_tile_with_the_tender_filter_and_search(self):
        for query in (f'?tender={self.program.pk}', f'?tender={self.second.pk}',
                      f'?q={self.mine.project_id}', '?type=OPEX'):
            for profile in (self.ceo, self.pm):
                with self.subTest(query=query, role=profile.role):
                    response = self._get(profile, query)
                    self.assertEqual(response.context['breakdown'].in_execution,
                                     self._tile(response)['value'])

    def test_a_kpi_filter_leaves_the_strip_as_it_leaves_the_tile(self):
        plain = self._get(self.ceo).context['breakdown']
        filtered = self._get(self.ceo, '?filter=blocked')
        self.assertEqual(filtered.context['breakdown'], plain)
        self.assertEqual(plain.in_execution, self._tile(filtered)['value'])

    def test_a_pm_sees_only_their_own_sites(self):
        b = self._get(self.pm).context['breakdown']
        # mine (Active, touched), mine_too (Mobilized) and the other tender's site: never
        # the other PM's two.
        self.assertEqual(_figures(b), (3, 2, 1, 1))
        self.assertEqual(_figures(self._get(self.other_pm).context['breakdown']), (2, 1, 0, 0))

    def test_tender_filter_equals_site_breakdown_and_the_tender_page(self):
        expected = site_breakdown(self.program.sites.all(), timezone.now())
        response = self._get(self.ceo, f'?tender={self.program.pk}')
        b = response.context['breakdown']
        self.assertEqual(_figures(b), _figures(expected))
        self.assertEqual(_figures(b), (3, 3, 1, 1))
        self.assertIsNone(b.note)
        page = Client()
        page.force_login(_fresh(self.ceo.user))
        tender = page.get(reverse('program_detail', args=[self.program.pk]))
        self.assertEqual(_figures(tender.context['breakdown']), _figures(b))
        # The strip itself reads the same on both pages (no planned count on this tender).
        self.assertEqual(_section(response.content.decode()),
                         _section(tender.content.decode()))


# ---------------------------------------------------------------------------
# 2. The tiles
# ---------------------------------------------------------------------------

class TileTests(DashboardStripFixture):

    def setUp(self):
        self.site = self._mobilized(self._resco(dc_capacity_kw=Decimal('40.00')))
        self._resco(pm=self.other_pm, dc_capacity_kw=Decimal('62.50'))

    def test_the_separate_tile_is_gone_and_every_other_tile_is_drawn(self):
        for profile in (self.ceo, self.pm, self.finance, self.scm):
            with self.subTest(role=profile.role):
                response = self._get(profile)
                html = response.content.decode()
                self.assertNotIn('<span class="l">Sites in execution</span>', html)
                labels = [t['label'] for t in response.context['kpis']]
                self.assertIn('Sites in execution', labels)       # still in the data
                for label in labels:
                    if label != 'Sites in execution':
                        self.assertIn(f'<span class="l">{label}</span>', html)
                # The strip's own label, once, inside the tile card.
                kpis = html[html.index('id="kpis">'):]
                self.assertTrue(kpis.index('<section class="tb') < kpis.index('class="kpi'))
                self.assertEqual(html.count('<div class="l">Sites in execution</div>'), 1)
                # Neither template's {% comment %} text reaches the page.
                self.assertNotIn('skipped below because the strip shows it', html)
                self.assertNotIn('byte-for-byte', html)

    def test_kpi_tiles_output_is_unchanged(self):
        """One full kpi_tiles() output, pinned as it was before TB-2 (the snapshot reads it)."""
        cards = [c for c in build_site_cards(_fresh(self.ceo.user)) if not c.is_draft]
        params = {'sort': 'latest', 'q': '', 'type': '', 'tender': None, 'filter': ''}
        self.assertEqual(kpi_tiles(cards, params, True), [
            {'label': 'Sites in execution', 'value': 2, 'detail': None},
            {'label': 'DC kWp in execution', 'value': '102', 'detail': 'AC 0 kW'},
            {'key': 'behind', 'label': 'Behind plan', 'value': 0,
             'detail': '0 scheduled sites on plan', 'tone': '', 'sites': 0, 'active': False,
             'href': '?filter=behind'},
            {'key': 'civil', 'label': 'Needs Civil date', 'value': 0,
             'detail': 'no execution dates yet', 'tone': '', 'sites': 0, 'active': False,
             'href': '?filter=civil'},
            {'key': 'overdue', 'label': 'Overdue tasks', 'value': 0, 'detail': 'across 0 sites',
             'tone': '', 'sites': 0, 'active': False, 'href': '?filter=overdue'},
            {'key': 'blocked', 'label': 'Blocked tasks', 'value': 0,
             'detail': '0 sites affected', 'tone': '', 'sites': 0, 'active': False,
             'href': '?filter=blocked'},
            {'key': 'issues', 'label': 'Open issues', 'value': 0, 'detail': 'on 0 sites',
             'tone': '', 'sites': 0, 'active': False, 'href': '?filter=issues'},
            {'key': 'quiet', 'label': 'Gone quiet', 'value': 0, 'detail': 'no update in 7+ days',
             'tone': '', 'sites': 0, 'active': False, 'href': '?filter=quiet'},
            {'label': 'Payments pending', 'value': '₹0.0 L',
             'detail': 'open requests, indicative per site'},
        ])

    def test_the_page_passes_kpi_tiles_output_untouched(self):
        response = self._get(self.ceo)
        cards = [c for c in build_site_cards(_fresh(self.ceo.user)) if not c.is_draft]
        params = _params(response.wsgi_request, set())
        self.assertEqual(response.context['kpis'][:9], kpi_tiles(cards, params, True))


# ---------------------------------------------------------------------------
# 3. Residential and CAPEX (ruling P3(c) and its two refinements)
# ---------------------------------------------------------------------------

class RescoRuleTests(DashboardStripFixture):

    def setUp(self):
        self.resco = self._active(self._activated())

    def _strip(self, profile=None, query=''):
        response = self._get(profile or self.pm, query)
        return response, response.context['breakdown']

    def test_all_resco_shows_the_figures_and_no_note(self):
        response, b = self._strip()
        # Activation itself logs a human update, so the site is Active today too.
        self.assertEqual(_figures(b), (1, 1, 1, 1))
        self.assertIsNone(b.note)
        self.assertNotIn('data-breakdown-note', response.content.decode())

    def test_mixed_counts_mobilized_and_active_over_resco_only_with_the_base(self):
        res = self._bare()                                   # Residential, self.pm's
        self._bare(project_type='CAPEX')
        # Give the Residential site what TB-1's rules ask for, so only the RESCO cut keeps
        # it out of Mobilized: site_breakdown() alone would count it.
        self._dc(res)
        self._design(res, DESIGN_IN_DESIGN)
        self._activity(res)                                  # Active today: every site
        self.assertIn(res.pk, site_breakdown([res.pk], timezone.now()).mobilized_ids)
        response, b = self._strip()
        self.assertEqual(_figures(b), (3, 1, 1, 2))     # Active today: RESCO + Residential
        self.assertEqual(b.note, 'Mobilized and Active count RESCO sites only (1 of 3)')
        self.assertIn('<p class="note" data-breakdown-note>Mobilized and Active count RESCO '
                      'sites only (1 of 3)</p>', response.content.decode())

    def test_no_resco_site_draws_dashes_and_says_why(self):
        self._bare(pm=self.other_pm)
        self._activity(self._bare(project_type='CAPEX', pm=self.other_pm))
        response, b = self._strip(self.other_pm)
        self.assertEqual(_figures(b), (2, None, None, 1))
        self.assertEqual(b.note, NOTE_NONE)
        section = _section(response.content.decode())
        self.assertEqual(section.count('<div class="v">—</div>'), 2)
        self.assertIn('<div class="v">—</div><div class="l">Mobilized</div>', section)
        self.assertIn('<div class="v">—</div><div class="l">Active</div>', section)
        self.assertIn(f'data-breakdown-note>{NOTE_NONE}</p>', section)

    def test_the_type_filter_follows_the_same_rule(self):
        self._bare()
        _, b = self._strip(query='?type=Residential')
        self.assertEqual((b.mobilized, b.active, b.note), (None, None, NOTE_NONE))
        _, b = self._strip(query='?type=OPEX')
        self.assertEqual((_figures(b), b.note), ((1, 1, 1, 1), None))

    def test_tender_filter_on_a_mixed_portfolio_shows_no_note(self):
        self._bare()
        response, b = self._strip(self.ceo, f'?tender={self.program.pk}')
        self.assertIsNone(b.note)
        self.assertEqual(_figures(b),
                         _figures(site_breakdown(self.program.sites.all(), timezone.now())))

    def test_no_site_at_all_shows_zeros_and_no_note(self):
        _, b = self._strip(self.other_pm)
        self.assertEqual((_figures(b), b.note), ((0, 0, 0, 0), None))


# ---------------------------------------------------------------------------
# 4. The tender page is unchanged
# ---------------------------------------------------------------------------

class TenderPageUnchangedTests(DashboardStripFixture):

    def setUp(self):
        self._active(self._activated())
        self._activity(self._mobilized(self._resco()))

    def _tender_page(self):
        client = Client()
        client.force_login(_fresh(self.ceo.user))
        return client.get(reverse('program_detail', args=[self.program.pk]))

    def test_the_strip_html_is_byte_identical_to_tb1(self):
        for planned in (None, 0, 94):
            with self.subTest(planned=planned):
                Program.objects.filter(pk=self.program.pk).update(planned_site_count=planned)
                response = self._tender_page()
                expected = Template(TB1_STRIP).render(Context({
                    'breakdown': response.context['breakdown'],
                    'program': Program.objects.get(pk=self.program.pk)}))
                self.assertEqual(_section(response.content.decode()), expected)

    def test_the_strip_is_styled_once_and_carries_no_note(self):
        html = self._tender_page().content.decode()
        self.assertEqual(html.count('.tb .chain {'), 1)
        self.assertNotIn('data-breakdown-note', html)
        self.assertNotIn('byte-for-byte', html)


# ---------------------------------------------------------------------------
# 5. Query budget
# ---------------------------------------------------------------------------

class QueryBudgetTests(DashboardStripFixture):

    def setUp(self):
        for _ in range(3):
            self._activity(self._mobilized(self._resco()))
        self._bare()

    def _count(self, client, stub):
        with CaptureQueriesContext(connection) as captured:
            if stub:
                # site_breakdown() replaced by an answer that runs no query.
                with mock.patch('projects.views_site_dashboard.site_breakdown',
                                return_value=SiteBreakdown((), (), (), ())):
                    response = client.get(URL)
            else:
                response = client.get(URL)
        self.assertEqual(response.status_code, 200)
        return len(captured.captured_queries)

    def test_the_strip_costs_exactly_one_query(self):
        for profile in (self.ceo, self.pm, self.coord):
            with self.subTest(role=profile.role):
                client = Client()
                client.force_login(_fresh(profile.user))
                client.get(URL)                                  # warm the session
                with_strip = self._count(client, stub=False)
                without = self._count(client, stub=True)
                # The coordinator coordinates nothing here: an empty set costs nothing.
                self.assertEqual(with_strip - without, 0 if profile is self.coord else 1)
