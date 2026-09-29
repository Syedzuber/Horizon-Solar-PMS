"""
S4 — tender_stages.tender_cards: one CEO card per tender (programme), built from the S3
stage helper, and the Tenders "Tenders" section that replaces the per-site cards.

THE RULES: five buckets fold S0-S7 (Survey S0+S1, Design S2-S4, Released S5, Procurement
S6, Execution S7); "Design released" is design_status == released, not the S5 count; the
tender pill is the worst health badge among activated sites that have one. Two queries
whatever the number of tenders or sites.

Real fixtures throughout: every assertion reads rows written to the test database.

    python manage.py test projects.tests_tender_cards --settings=solarpms.test_settings
"""
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.db import connection
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from .models import (
    DESIGN_AWAITING_ALLOCATION, DESIGN_IN_DESIGN, DESIGN_IN_QC, DESIGN_RELEASED,
    SITE_GROUP_LOCKED,
    DesignAssignment, Program, Project, ProjectPhase, SiteGroup, SiteGroupMembership, Task,
)
from .tender_stages import tender_cards
from .views import (
    CONTEXT_TENDERS, _get_ceo_dashboard_context, _tender_card_rows, tender_sites_qs,
)

ACTIVE_PROJECTS_LABEL = '<div class="section-label">Active projects</div>'
TENDERS_LABEL = '<div class="section-label">Tenders</div>'


def _profile(username, role):
    user = User.objects.create_user(username=username, password='x')
    profile = user.profile          # auto-created by the post_save signal
    profile.role = role
    profile.save(update_fields=['role'])
    return profile


def _program(name, code, program_type='OPEX'):
    return Program.objects.create(name=name, program_type=program_type, client_name='C',
                                  status='Active', short_tender_code=code)


def _site(program, project_id, kwp='10.00', status='Draft', design=None,
          project_type='OPEX', **extra):
    if status in ('Active', 'In Progress', 'Commissioned', 'On Hold'):
        extra.setdefault('activated_at', timezone.now())
    site = Project.objects.create(
        project_id=project_id, customer_name=f'Customer {project_id}',
        customer_phone='9876543210', site_address='1 Sun Road', city='Lucknow',
        project_type=project_type, program=program,
        dc_capacity_kw=Decimal(kwp) if kwp is not None else None, status=status,
        target_commissioning_date=date.today() + timedelta(days=90), **extra)
    if design:
        DesignAssignment.objects.create(project=site, status=design)
    return site


def _block(site):
    """One Blocked task on the site: the dashboard's Blocked badge."""
    phase = ProjectPhase.objects.create(project=site, phase_name='Installation', phase_order=1)
    Task.objects.create(phase=phase, task_name='Mount modules', task_order=1,
                        status=Task.BLOCKED)


class TenderCardFixtures(TestCase):
    """
    Alpha (ALP), 7 live sites, one per bucket and then some:
        A0 S0 10 kWp · A1 S1 20 · A2 S2 unknown · A3 S5 30 · A4 S6 zero (unknown)
        A5 S7 released 40 (Active, on time) · A6 S7 design open 50 (Active, blocked)
    Bravo (BRV), 3 sites, all S0, no capacity known.
    Charlie (no code), 2 sites, all capacity known: C1 S0 5.5 · C2 S7 Commissioned 4.5.
    Plus, never counted: a test site and a Cancelled site in Alpha, Delta (test sites
    only), a CAPEX programme with a live CAPEX site.
    """

    @classmethod
    def setUpTestData(cls):
        cls.alpha = _program('Alpha', 'ALP')
        cls.bravo = _program('Bravo', 'BRV')
        cls.charlie = _program('Charlie', '')

        a = cls.alpha
        cls.a0 = _site(a, 'ALP-0', kwp='10.00')
        cls.a1 = _site(a, 'ALP-1', kwp='20.00', design=DESIGN_AWAITING_ALLOCATION)
        cls.a2 = _site(a, 'ALP-2', kwp=None, design=DESIGN_IN_DESIGN)
        cls.a3 = _site(a, 'ALP-3', kwp='30.00', design=DESIGN_RELEASED)
        cls.a4 = _site(a, 'ALP-4', kwp='0', design=DESIGN_RELEASED)
        group = SiteGroup.objects.create(program=a, name='Locked batch',
                                         status=SITE_GROUP_LOCKED, locked_at=timezone.now())
        SiteGroupMembership.objects.create(group=group, project=cls.a4)
        cls.a5 = _site(a, 'ALP-5', kwp='40.00', status='Active', design=DESIGN_RELEASED,
                       site_name='Govt School Rampur')
        cls.a6 = _site(a, 'ALP-6', kwp='50.00', status='Active', design=DESIGN_IN_DESIGN)
        _block(cls.a6)
        _site(a, 'ALP-TEST', is_test=True, design=DESIGN_RELEASED)
        _site(a, 'ALP-CANC', status='Cancelled')

        for i in range(3):
            _site(cls.bravo, f'BRV-{i}', kwp=None)

        cls.c1 = _site(cls.charlie, 'CHR-1', kwp='5.50')
        cls.c2 = _site(cls.charlie, 'CHR-2', kwp='4.50', status='Commissioned',
                       design=DESIGN_RELEASED)

        delta = _program('Delta', 'DLT')
        _site(delta, 'DLT-1', is_test=True)
        _site(delta, 'DLT-2', is_test=True, status='Active')
        capex = _program('Capex Co', '', program_type='CAPEX')
        _site(capex, 'CPX-1', project_type='CAPEX', status='Active')

    def _cards(self, health=None):
        return {c['name']: c for c in tender_cards(tender_sites_qs(), health or {})}


class BucketTests(TenderCardFixtures):
    """a) bucket counts and percentages; buckets sum to the tender's sites."""

    def test_buckets_and_percentages(self):
        cards = self._cards()
        alpha = cards['Alpha']
        self.assertEqual(alpha['sites'], 7)
        self.assertEqual([(b['key'], b['sites'], b['pct']) for b in alpha['buckets']], [
            ('survey', 2, 28.6), ('design', 1, 14.3), ('released', 1, 14.3),
            ('procurement', 1, 14.3), ('execution', 2, 28.6)])
        self.assertEqual([(b['key'], b['sites'], b['pct']) for b in cards['Bravo']['buckets']], [
            ('survey', 3, 100.0), ('design', 0, 0.0), ('released', 0, 0.0),
            ('procurement', 0, 0.0), ('execution', 0, 0.0)])
        self.assertEqual([b['sites'] for b in cards['Charlie']['buckets']], [1, 0, 0, 0, 1])
        for card in cards.values():
            self.assertEqual(sum(b['sites'] for b in card['buckets']), card['sites'])
            self.assertEqual(sum(s['sites'] for s in card['stages']), card['sites'])

    def test_eight_stage_rows(self):
        alpha = self._cards()['Alpha']
        self.assertEqual([s['code'] for s in alpha['stages']],
                         ['S0', 'S1', 'S2', 'S3', 'S4', 'S5', 'S6', 'S7'])
        self.assertEqual([s['sites'] for s in alpha['stages']], [1, 1, 1, 0, 0, 1, 1, 2])
        self.assertEqual(alpha['stages'][7]['kwp'], Decimal('90.00'))
        self.assertEqual(alpha['stages'][6]['kwp_sites'], 0)       # A4's zero is unknown

    def test_header_fields_and_order(self):
        cards = tender_cards(tender_sites_qs(), {})
        self.assertEqual([c['name'] for c in cards], ['Alpha', 'Bravo', 'Charlie'])
        alpha = cards[0]
        self.assertEqual(alpha['code'], 'ALP')
        self.assertEqual(alpha['url'], reverse('program_detail', args=[self.alpha.pk]))
        self.assertEqual(cards[2]['code'], '')

    def test_equal_site_counts_order_by_name(self):
        # A second two-site tender, named before Charlie, sorts ahead of it.
        early = _program('Beta', 'BET')
        _site(early, 'BET-1')
        _site(early, 'BET-2')
        names = [c['name'] for c in tender_cards(tender_sites_qs(), {})]
        self.assertEqual(names, ['Alpha', 'Bravo', 'Beta', 'Charlie'])


class DesignReleasedTests(TenderCardFixtures):
    """b) released = design_status released, whatever the stage."""

    def test_released_counts_design_status(self):
        alpha = self._cards()['Alpha']
        # A3 (S5), A4 (S6) and A5 (S7, activated) — three, though S5 holds only one.
        self.assertEqual(alpha['released'], 3)
        self.assertEqual(alpha['buckets'][2]['sites'], 1)
        # A6 is activated with design still open: in Execution, not released.
        self.assertEqual(alpha['buckets'][4]['sites'], 2)
        # kWp of the released sites with known capacity: A3 30 + A5 40; A4 is zero.
        self.assertEqual(alpha['released_kwp'], Decimal('70.00'))
        self.assertEqual(alpha['released_kwp_sites'], 2)
        self.assertEqual(self._cards()['Bravo']['released'], 0)


class KwpCoverageTests(TenderCardFixtures):
    """c) kWp coverage per tender: some, none, all."""

    def test_coverage(self):
        rows = {c['name']: c for c in _tender_card_rows(tender_sites_qs(), [])}
        self.assertEqual(rows['Alpha']['kwp_text'], '150 kWp (5 of 7 sites)')
        self.assertEqual(rows['Bravo']['kwp_text'], '— kWp (0 of 3 sites)')
        self.assertEqual(rows['Charlie']['kwp_text'], '10 kWp')
        self.assertEqual(rows['Alpha']['released_kwp_text'], '70 kWp (2 of 3 sites)')
        self.assertEqual(rows['Bravo']['released_kwp_text'], '—')
        self.assertEqual(rows['Charlie']['released_kwp_text'], '4.5 kWp')
        self.assertEqual([b['width'] for b in rows['Alpha']['buckets']],
                         ['28.6', '14.3', '14.3', '14.3', '28.6'])


class HealthTests(TenderCardFixtures):
    """d) tender pill = worst activated-site health; none without an activated site."""

    def test_worst_badge_wins(self):
        cards = self._cards({self.a5.pk: 'on_time', self.a6.pk: 'at_risk'})
        self.assertEqual(cards['Alpha']['health'], 'at_risk')
        cards = self._cards({self.a5.pk: 'delayed', self.a6.pk: 'at_risk'})
        self.assertEqual(cards['Alpha']['health'], 'delayed')

    def test_from_the_dashboard(self):
        """The badges the dashboard itself computes: A6 has a Blocked task."""
        ctx = _get_ceo_dashboard_context(CONTEXT_TENDERS)
        cards = {c['name']: c for c in ctx['tender_cards']}
        alpha = cards['Alpha']
        self.assertEqual(alpha['health'], 'blocked')
        self.assertEqual(alpha['pill']['label'], 'Blocked')
        self.assertEqual(
            [(s['project_id'], s['name'], s['badge']) for s in alpha['activated']],
            [('ALP-5', 'Govt School Rampur', 'on_time'),
             ('ALP-6', 'Customer ALP-6', 'blocked')])      # no site_name: customer_name
        # No activated site: no pill.
        self.assertIsNone(cards['Bravo']['health'])
        self.assertIsNone(cards['Bravo']['pill'])
        self.assertEqual(cards['Bravo']['activated'], [])
        # Commissioned is outside the dashboard's cards: no badge, its status shown, and
        # the tender takes no health from it.
        self.assertEqual([(s['badge'], s['status']) for s in cards['Charlie']['activated']],
                         [(None, 'Commissioned')])
        self.assertIsNone(cards['Charlie']['health'])


class ScopeTests(TenderCardFixtures):
    """e) test and CAPEX sites never appear; h) cards add up to the header."""

    def test_test_and_capex_excluded(self):
        cards = tender_cards(tender_sites_qs(), {})
        self.assertEqual({c['name'] for c in cards}, {'Alpha', 'Bravo', 'Charlie'})
        project_ids = {c['name']: [s['project_id'] for s in c['activated']] for c in cards}
        self.assertNotIn('ALP-TEST', project_ids['Alpha'])
        self.assertEqual(cards[0]['sites'], 7)             # not the test or Cancelled site

    def test_cards_sum_to_the_header(self):
        ctx = _get_ceo_dashboard_context(CONTEXT_TENDERS)
        self.assertEqual(sum(c['sites'] for c in ctx['tender_cards']),
                         ctx['tender_header']['sites'])
        self.assertEqual(ctx['tender_header']['sites'], 12)
        self.assertEqual(len(ctx['tender_cards']), ctx['tender_header']['tenders'])


class QueryBudgetTests(TestCase):
    """f) 2 programmes x 3 sites cost what 4 programmes x 30 sites cost."""

    def _populate(self, prefix, programmes, sites):
        designs = [None, DESIGN_AWAITING_ALLOCATION, DESIGN_IN_DESIGN, DESIGN_IN_QC,
                   DESIGN_RELEASED]
        for p in range(programmes):
            program = _program(f'{prefix} tender {p}', f'{prefix}{p}')
            group = SiteGroup.objects.create(program=program, name=f'{prefix}{p} batch',
                                             status=SITE_GROUP_LOCKED, locked_at=timezone.now())
            for i in range(sites):
                site = _site(program, f'{prefix}{p}-{i:02d}',
                             status='Active' if i % 7 == 6 else 'Draft',
                             design=designs[i % len(designs)])
                if i % 5 == 3:
                    SiteGroupMembership.objects.create(group=group, project=site)

    def _count(self, prefix):
        qs = Project.objects.filter(project_id__startswith=prefix)
        with CaptureQueriesContext(connection) as direct:
            cards = tender_cards(qs, {})
        with CaptureQueriesContext(connection) as rows:
            _tender_card_rows(qs, [])
        return len(cards), len(direct), len(rows)

    def test_query_count_is_fixed(self):
        self._populate('QA', 2, 3)
        self._populate('QB', 4, 30)
        small, large = self._count('QA'), self._count('QB')
        self.assertEqual(small, (2, 2, 2))
        self.assertEqual(large, (4, 2, 2))


class RenderTests(TenderCardFixtures):
    """g) the section renders under Tenders only, and replaces the per-site cards there."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.ceo = _profile('s4_ceo', 'CEO')
        _site(None, 'RES-1', project_type='Residential', status='Active')

    def setUp(self):
        self.client = Client(SERVER_NAME='localhost')
        self.client.force_login(self.ceo.user)

    def test_tenders_renders_cards_not_site_cards(self):
        page = self.client.get(reverse('dashboard_ceo'), {'context': 'tenders'})
        self.assertContains(page, TENDERS_LABEL)
        self.assertNotContains(page, ACTIVE_PROJECTS_LABEL)
        # The label's initial text, one per card (x-text holds the string a second time).
        self.assertContains(page, "'Show stage breakdown'\">Show stage breakdown</span>", count=3)
        self.assertContains(page, '<span class="text-muted">ALP</span> · Alpha')
        self.assertContains(page, '7 sites · 150 kWp (5 of 7 sites)')
        self.assertContains(page, '3 sites · — kWp (0 of 3 sites)')
        self.assertContains(page, 'aria-label="Survey 2, Design 1, Released 1, Procurement 1, Execution 2"')
        self.assertContains(page, 'style="width:28.6%;"')
        self.assertContains(page, '<span class="fact-val">3 / 7</span>')
        self.assertContains(page, 'ALP-5 · Govt School Rampur')
        self.assertContains(page, reverse('project_overview', args=['ALP-6']))
        self.assertContains(page, '<span class="status-pill not-started">Commissioned</span>')
        self.assertContains(page, f'href="{reverse("program_detail", args=[self.alpha.pk])}"')
        self.assertContains(page, 'Open tender →', count=3)
        self.assertContains(page, 'No site activated yet.', count=1)
        # Collapsed by default: the expanded areas are cloaked until Alpine opens them.
        self.assertContains(page, 'x-show="open" x-cloak', count=3)
        # A multi-line template comment must not leak onto the page.
        self.assertNotContains(page, 'views._tender_card_rows; Alpine holds')

    def test_residential_and_no_context_keep_site_cards(self):
        for params, project_id in (({'context': 'residential'}, 'RES-1'), ({}, 'ALP-5')):
            page = self.client.get(reverse('dashboard_ceo'), params)
            self.assertContains(page, ACTIVE_PROJECTS_LABEL)
            self.assertNotContains(page, TENDERS_LABEL)
            self.assertNotContains(page, 'Show stage breakdown')
            self.assertIsNone(page.context['tender_cards'])
            self.assertIn(project_id,
                          [card['project'].project_id for card in page.context['project_cards']])
