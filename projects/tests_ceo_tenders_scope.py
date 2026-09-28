"""
S1 — the CEO dashboard's Tenders scope and the Project.is_test flag.

THE RULE: on the CEO dashboard, Tenders = RESCO (OPEX) only, and test data (is_test) is
hidden in both contexts. The cards still read activated sites (Active / In Progress)
through projects_qs; the Tenders header reads tender_sites_qs(), which also counts
Draft, On Hold and Commissioned sites. Nothing outside the CEO dashboard changes —
tests_scm_context_filter and tests_payment_readers_o6 hold the shared definition.

Real fixtures throughout: every assertion reads rows written to the test database.

    python manage.py test projects.tests_ceo_tenders_scope --settings=solarpms.test_settings
"""
from datetime import date, timedelta
from decimal import Decimal
from io import StringIO

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Program, Project
from .views import (
    _context_filter, _format_kwp, _get_ceo_dashboard_context, tender_sites_qs,
)


def _profile(username, role):
    user = User.objects.create_user(username=username, password='x')
    profile = user.profile          # auto-created by the post_save signal
    profile.role = role
    profile.save(update_fields=['role'])
    return profile


def _project(project_id, project_type, status='Active', kwp='10.00', **extra):
    if status in ('Active', 'In Progress'):
        extra.setdefault('activated_at', timezone.now())
    return Project.objects.create(
        project_id=project_id, customer_name=project_id, customer_phone='9876543210',
        site_address='1 Sun Road', city='Lucknow', project_type=project_type,
        dc_capacity_kw=Decimal(kwp), status=status,
        target_commissioning_date=date.today() + timedelta(days=90),
        **extra)


def _card_ids(context):
    """project_ids on the CEO dashboard's cards — i.e. what projects_qs returned."""
    return {c['project'].project_id for c in _get_ceo_dashboard_context(context)['project_cards']}


class CeoTendersScopeTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.ceo = _profile('s1_ceo', 'CEO')
        tender = Program.objects.create(
            name='S1 Tender', program_type='OPEX', client_name='C',
            status='Active', short_tender_code='SIA')
        other_tender = Program.objects.create(
            name='S1 Tender Two', program_type='OPEX', client_name='C',
            status='Active', short_tender_code='SIB')
        capex_plant = Program.objects.create(
            name='S1 Plant', program_type='CAPEX', client_name='C', status='Active')

        cls.live   = _project('S1-LIVE', 'OPEX', program=tender, kwp='1000.50')
        cls.draft  = _project('S1-DRAFT', 'OPEX', status='Draft', program=tender, kwp='200.00')
        cls.hold   = _project('S1-HOLD', 'OPEX', status='On Hold', program=other_tender, kwp='50.00')
        cls.gone   = _project('S1-CANC', 'OPEX', status='Cancelled', program=tender)
        cls.dele   = _project('S1-DEL', 'OPEX', program=tender, is_deleted=True)
        cls.test   = _project('S1-TEST', 'OPEX', program=tender, is_test=True)
        cls.test_d = _project('S1-TESTD', 'OPEX', status='Draft', program=tender, is_test=True)
        cls.capex  = _project('S1-CPX', 'CAPEX', program=capex_plant)
        cls.resi   = _project('S1-RES', 'Residential')

    # a) is_test ---------------------------------------------------------------

    def test_test_project_excluded_from_tenders_cards(self):
        self.assertNotIn('S1-TEST', _card_ids('tenders'))
        self.assertIn('S1-LIVE', _card_ids('tenders'))

    def test_test_project_excluded_from_tender_sites(self):
        ids = set(tender_sites_qs().values_list('project_id', flat=True))
        self.assertNotIn('S1-TEST', ids)
        self.assertNotIn('S1-TESTD', ids)

    def test_test_residential_project_excluded_from_residential_cards(self):
        _project('S1-RES-T', 'Residential', is_test=True)
        self.assertNotIn('S1-RES-T', _card_ids('residential'))

    def test_no_context_hides_test_data(self):
        """?context absent hides test data too since S2 (T4) — the navbar Home link lands
        a CEO there. It still spans every project type."""
        self.assertNotIn('S1-TEST', _card_ids(None))

    # b) CAPEX -----------------------------------------------------------------

    def test_capex_excluded_from_tenders_cards_and_tender_sites(self):
        self.assertNotIn('S1-CPX', _card_ids('tenders'))
        self.assertNotIn('S1-CPX', set(tender_sites_qs().values_list('project_id', flat=True)))

    # c) Draft -----------------------------------------------------------------

    def test_draft_site_counted_in_tender_sites_but_not_on_cards(self):
        self.assertIn('S1-DRAFT', set(tender_sites_qs().values_list('project_id', flat=True)))
        self.assertNotIn('S1-DRAFT', _card_ids('tenders'))

    def test_tender_sites_is_exactly_the_live_statuses(self):
        """On Hold counts, Cancelled does not."""
        self.assertEqual(
            set(tender_sites_qs().values_list('project_id', flat=True)),
            {'S1-LIVE', 'S1-DRAFT', 'S1-HOLD'})

    # d) deleted ---------------------------------------------------------------

    def test_deleted_site_excluded_from_both(self):
        self.assertNotIn('S1-DEL', _card_ids('tenders'))
        self.assertNotIn('S1-DEL', set(tender_sites_qs().values_list('project_id', flat=True)))

    # e) Residential unchanged ---------------------------------------------------

    def test_residential_cards_match_the_shared_definition(self):
        """The shared _context_filter is the pre-S1 definition, left untouched; for a
        non-test project the CEO Residential cards must list the same projects."""
        before = set(Project.objects.filter(
            is_deleted=False, status__in=['Active', 'In Progress'],
            **_context_filter('residential'),
        ).values_list('project_id', flat=True))
        self.assertEqual(before, {'S1-RES'})
        self.assertEqual(_card_ids('residential'), before)

    # header -------------------------------------------------------------------

    def test_tenders_header_figures(self):
        header = _get_ceo_dashboard_context('tenders')['tender_header']
        self.assertEqual(header['tenders'], 2)          # S1 Tender + S1 Tender Two
        self.assertEqual(header['sites'], 3)            # LIVE, DRAFT, HOLD
        self.assertEqual(header['kwp'], Decimal('1250.50'))
        self.assertEqual(header['activated'], 1)        # only LIVE carries activated_at
        self.assertEqual(header['kwp_display'], '1,250.5')

    def test_header_rendered_under_tenders_only(self):
        client = Client(SERVER_NAME='localhost')
        client.force_login(self.ceo.user)
        tenders = client.get(reverse('dashboard_ceo'), {'context': 'tenders'})
        self.assertContains(tenders, '2 tenders · 3 sites · 1,250.5 kWp · 1 activated')
        self.assertNotContains(tenders, 'active project')
        resi = client.get(reverse('dashboard_ceo'), {'context': 'residential'})
        self.assertContains(resi, '1 active project')
        self.assertIsNone(resi.context['tender_header'])

    def test_kwp_format(self):
        self.assertEqual(_format_kwp(Decimal('7125.50')), '7,125.5')
        self.assertEqual(_format_kwp(Decimal('4745.00')), '4,745')
        self.assertEqual(_format_kwp(Decimal('0.25')), '0.25')
        self.assertEqual(_format_kwp(None), '0')


class FlagTestProjectsCommandTests(TestCase):
    """f) flag_test_projects: dry run writes nothing, --apply flags only prefix matches
    (case-sensitive), --unflag reverses."""

    @classmethod
    def setUpTestData(cls):
        for pid in ('PILOTA01', 'PILOTA02', 'DEMOB01', 'REAL01', 'pilota03'):
            _project(pid, 'OPEX', status='Draft')
        _project('PILOTA99', 'OPEX', status='Draft', is_deleted=True)

    def _run(self, *args):
        out = StringIO()
        call_command('flag_test_projects', *args, stdout=out)
        return out.getvalue()

    def _flagged(self):
        return set(Project.objects.filter(is_test=True).values_list('project_id', flat=True))

    def test_dry_run_changes_nothing(self):
        out = self._run('--prefix', 'PILOTA', '--prefix', 'DEMOB')
        self.assertEqual(self._flagged(), set())
        self.assertIn('DRY RUN: 4 project(s)', out)
        self.assertIn('| PILOTA01 | OPEX | Draft | -', out)
        self.assertNotIn('Updated', out)

    def test_apply_flags_only_prefix_matches(self):
        out = self._run('--prefix', 'PILOTA', '--prefix', 'DEMOB', '--apply')
        # Deleted PILOTA99 is flagged too; lower-case pilota03 and REAL01 are not.
        self.assertEqual(self._flagged(), {'PILOTA01', 'PILOTA02', 'PILOTA99', 'DEMOB01'})
        self.assertIn('Updated 4', out)

    def test_unflag_reverses(self):
        self._run('--prefix', 'PILOTA', '--prefix', 'DEMOB', '--apply')
        out = self._run('--prefix', 'PILOTA', '--apply', '--unflag')
        self.assertEqual(self._flagged(), {'DEMOB01'})
        self.assertIn('Updated 3', out)
