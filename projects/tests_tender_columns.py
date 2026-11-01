"""
TC-B1 — the CEO Tenders "Activated sites" rows: # · Site · kWp · Activated · Target · Status.

THE RULES: the list is in project_id order, so # (the row's place in it) cannot renumber
between page loads; capacity 0 and null are both unknown (None, "Not set"), as the card's
kWp total reads them; Activated is the IST date of activated_at, never the UTC one; Target
is target_commissioning_date as stored. tender_cards() hands the template dicts, so a
misspelt key would render blank without an error — the key set is pinned here.

Real fixtures throughout: every assertion reads rows written to the test database.

    python manage.py test projects.tests_tender_columns --settings=solarpms.test_settings
"""
from datetime import date, datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse

from .models import Program, Project
from .tender_stages import site_stages, tender_cards
from .views import tender_sites_qs

ACTIVATED_KEYS = {'pk', 'project_id', 'name', 'badge', 'status',
                  'capacity_kw', 'activated_on', 'target_date'}

# 20:00 UTC on 8 Oct is 01:30 IST on 9 Oct: the UTC date would be a day early.
LATE_EVENING_UTC = datetime(2026, 10, 8, 20, 0, tzinfo=dt_timezone.utc)


def _site(program, project_id, kwp, activated_at, target):
    return Project.objects.create(
        project_id=project_id, customer_name=f'Customer {project_id}',
        customer_phone='9876543210', site_address='1 Sun Road', city='Lucknow',
        project_type='OPEX', program=program, status='Active',
        dc_capacity_kw=Decimal(kwp) if kwp is not None else None,
        activated_at=activated_at, target_commissioning_date=target)


class ActivatedColumnFixtures(TestCase):
    """
    Tango (TNG), three activated sites created out of project_id order:
        TNG-3 capacity 0 · activated 8 Oct 20:00 UTC · target set
        TNG-1 capacity null · activated 1 Oct 06:00 UTC · no target
        TNG-2 capacity 1234.50 · activated 2 Oct 06:00 UTC · target set
    """

    @classmethod
    def setUpTestData(cls):
        cls.tango = Program.objects.create(name='Tango', program_type='OPEX', client_name='C',
                                           status='Active', short_tender_code='TNG')
        cls.target = date.today() + timedelta(days=60)
        cls.t3 = _site(cls.tango, 'TNG-3', '0', LATE_EVENING_UTC, cls.target)
        cls.t1 = _site(cls.tango, 'TNG-1', None,
                       datetime(2026, 10, 1, 6, 0, tzinfo=dt_timezone.utc), None)
        cls.t2 = _site(cls.tango, 'TNG-2', '1234.50',
                       datetime(2026, 10, 2, 6, 0, tzinfo=dt_timezone.utc), cls.target)

    def _activated(self):
        (card,) = tender_cards(tender_sites_qs(), {})
        return {site['project_id']: site for site in card['activated']}, card['activated']


class ActivatedDictTests(ActivatedColumnFixtures):

    def test_a_ordered_by_project_id(self):
        _, rows = self._activated()
        self.assertEqual([s['project_id'] for s in rows], ['TNG-1', 'TNG-2', 'TNG-3'])

    def test_b_capacity_zero_and_null_are_none(self):
        sites, _ = self._activated()
        self.assertIsNone(sites['TNG-3']['capacity_kw'])           # 0
        self.assertIsNone(sites['TNG-1']['capacity_kw'])           # null
        self.assertEqual(sites['TNG-2']['capacity_kw'], Decimal('1234.50'))

    def test_c_activated_on_is_the_ist_date(self):
        sites, _ = self._activated()
        self.assertEqual(sites['TNG-3']['activated_on'], date(2026, 10, 9))
        self.assertEqual(sites['TNG-1']['activated_on'], date(2026, 10, 1))

    def test_d_target_date_passes_through(self):
        sites, _ = self._activated()
        self.assertEqual(sites['TNG-3']['target_date'], self.target)
        self.assertIsNone(sites['TNG-1']['target_date'])

    def test_e_keys_exactly(self):
        _, rows = self._activated()
        for site in rows:
            self.assertEqual(set(site), ACTIVATED_KEYS)

    def test_activation_cleared_after_the_stage_read(self):
        """site_stages() and the row read are two queries. A site placed in S7 by the
        first and de-activated before the second gets activated_on None, not a 500."""
        stages = site_stages(tender_sites_qs())
        Project.objects.filter(pk=self.t2.pk).update(activated_at=None)
        with mock.patch('projects.tender_stages.site_stages', return_value=stages):
            sites, _ = self._activated()
        self.assertIsNone(sites['TNG-2']['activated_on'])
        self.assertEqual(sites['TNG-1']['activated_on'], date(2026, 10, 1))


class ActivatedRenderTests(ActivatedColumnFixtures):
    """The dicts reach the page under the keys the template reads."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        user = User.objects.create_user(username='tcb1_ceo', password='x')
        profile = user.profile          # auto-created by the post_save signal
        profile.role = 'CEO'
        profile.save(update_fields=['role'])
        cls.ceo = user

    def setUp(self):
        self.client = Client(SERVER_NAME='localhost')
        self.client.force_login(self.ceo)

    def test_columns_render(self):
        page = self.client.get(reverse('dashboard_ceo'), {'context': 'tenders'})
        html = page.content.decode()
        # Rows 1-3 in project_id order, whatever order the sites were created in.
        self.assertLess(html.index('title="TNG-1 · Customer TNG-1"'),
                        html.index('title="TNG-2 · Customer TNG-2"'))
        self.assertLess(html.index('title="TNG-2 · Customer TNG-2"'),
                        html.index('title="TNG-3 · Customer TNG-3"'))
        for n in (1, 2, 3):
            self.assertContains(page, f'<span class="tsite-num">{n}</span>', count=1)
        # Capacity: grouped, decimals only when present; 0 and null both "Not set".
        self.assertContains(page, '<span class="tsite-lbl">kWp</span> 1,234.50')
        self.assertContains(
            page, '<span class="tsite-lbl">kWp</span> <span class="tsite-unset">Not set</span>',
            count=2)
        # The IST date of 8 Oct 20:00 UTC.
        self.assertContains(page, '<span class="tsite-lbl">Activated</span> 09 Oct 26')
        self.assertContains(
            page, f'<span class="tsite-lbl">Target</span> {self.target:%d %b %y}', count=2)
        self.assertContains(
            page, '<span class="tsite-lbl">Target</span> <span class="tsite-unset">Not set</span>',
            count=1)
