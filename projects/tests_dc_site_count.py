"""
DC-B1: a site's delivery challans counted by status, on the DC detail page and on the
project overview's Delivery Challans card. Read-only; no model change.

WHAT IS PINNED HERE:
  * delivery_summary.site_challan_counts() and site_challan_counts_for_project() give
    the same dict, one key per status, on a site holding one challan of each status;
  * another project's challans are never counted;
  * the detail page shows the line and links it to the overview card's #dc-card anchor;
  * the overview card carries id="dc-card", shows the line and keeps its count badge;
    a site with no challans shows no line ("No delivery challans yet." unchanged);
  * "rejected" appears only when there is one; every other zero part still shows;
  * query cost: the overview pays nothing for the line, the detail page one query.
    Measured BEFORE vs AFTER inside each test by swapping the helper for a constant —
    the difference is the helper's own cost, whatever the rest of the page costs.

Run with:
    python manage.py test projects.tests_dc_site_count --settings=solarpms.test_settings
"""
from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.db import connection
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from .delivery_summary import site_challan_counts, site_challan_counts_for_project
from .models import DeliveryChallan, Project

# Every challan status, once. Spelled through the model's constants so a renamed value
# cannot leave this fixture testing a status the model no longer has.
ALL_STATUSES = (DeliveryChallan.RECEIVED, DeliveryChallan.PARTIALLY_RECEIVED,
                DeliveryChallan.EXPECTED, DeliveryChallan.REJECTED)

FULL_LINE = ('4 challans for this site — 1 received · 1 partially received · '
             '1 expected · 1 rejected')
ONE_EXPECTED_LINE = ('1 challan for this site — 0 received · 0 partially received · '
                     '1 expected')

# What a helper returns when the test swaps it out to measure the page without it.
_STUB = {'total': 0, 'received': 0, 'partially_received': 0, 'expected': 0, 'rejected': 0}


def _profile(username, role):
    user = User.objects.create_user(username=username, password='x',
                                    first_name=username.title(), last_name='Test')
    profile = user.profile          # auto-created by the post_save signal
    profile.role = role
    profile.save(update_fields=['role'])
    return profile


class DcSiteCountTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.scm = _profile('dcc_scm', 'SCM')
        cls.site = cls._project('DCC-SITE')
        cls.other = cls._project('DCC-OTHER')
        cls.empty = cls._project('DCC-EMPTY')
        cls.challans = [cls._challan(cls.site, f'DCC-{n}', status)
                        for n, status in enumerate(ALL_STATUSES, start=1)]
        # The other project holds challans of every status too, twice over, so any
        # leak would show in every key of the site's count.
        for n, status in enumerate(ALL_STATUSES * 2, start=1):
            cls._challan(cls.other, f'OTH-{n}', status)
        cls.lone = cls._project('DCC-LONE')
        cls.lone_dc = cls._challan(cls.lone, 'LONE-1', DeliveryChallan.EXPECTED)

    @staticmethod
    def _project(project_id):
        return Project.objects.create(
            project_id=project_id, customer_name=project_id, customer_phone='9876543210',
            site_address='1 Sun Road', city='Delhi', project_type='Residential',
            dc_capacity_kw=Decimal('5.00'), status='Active',
            target_commissioning_date=date.today() + timedelta(days=90))

    @staticmethod
    def _challan(project, number, status):
        return DeliveryChallan.objects.create(
            project=project, dc_number=number, dc_date=date.today(), status=status)

    def setUp(self):
        self.client = Client(SERVER_NAME='localhost')
        self.client.force_login(self.scm.user)

    def _detail_url(self, project, challan):
        return reverse('delivery_challan_detail', args=[project.project_id, challan.pk])

    def _overview_url(self, project):
        return reverse('project_overview', args=[project.project_id])

    def _queries(self, url):
        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        return len(ctx.captured_queries)

    # ── the helpers ─────────────────────────────────────────────────────────

    def test_one_challan_of_each_status_counts_one_in_each_key(self):
        expected = {'total': 4, 'received': 1, 'partially_received': 1,
                    'expected': 1, 'rejected': 1}
        self.assertEqual(site_challan_counts_for_project(self.site), expected)
        self.assertEqual(site_challan_counts(self.challans), expected)

    def test_another_projects_challans_are_never_counted(self):
        self.assertEqual(site_challan_counts_for_project(self.site)['total'], 4)
        self.assertEqual(site_challan_counts_for_project(self.other)['total'], 8)
        self.assertEqual(site_challan_counts_for_project(self.empty), _STUB)

    def test_the_pure_count_runs_no_query_and_the_project_count_runs_one(self):
        rows = list(DeliveryChallan.objects.filter(project=self.site))
        with self.assertNumQueries(0):
            site_challan_counts(rows)
        with self.assertNumQueries(1):
            site_challan_counts_for_project(self.site)

    # ── the detail page ─────────────────────────────────────────────────────

    def test_detail_page_shows_the_line_linked_to_the_overview_card(self):
        response = self.client.get(self._detail_url(self.site, self.challans[0]))
        self.assertContains(response, FULL_LINE)
        self.assertContains(
            response, f'href="{self._overview_url(self.site)}#dc-card"')

    def test_detail_page_counts_only_its_own_site(self):
        response = self.client.get(self._detail_url(self.lone, self.lone_dc))
        self.assertContains(response, ONE_EXPECTED_LINE)
        self.assertNotContains(response, ONE_EXPECTED_LINE + ' ·',
                               msg_prefix='rejected shows only when there is one')

    # ── the overview card ───────────────────────────────────────────────────

    def test_overview_card_shows_the_line_and_keeps_its_badge(self):
        response = self.client.get(self._overview_url(self.site))
        self.assertContains(response, 'id="dc-card"')
        self.assertContains(response, FULL_LINE)
        self.assertContains(response, '<span class="badge bg-secondary ms-1">4</span>')

    def test_overview_card_zero_parts_show_and_rejected_does_not(self):
        response = self.client.get(self._overview_url(self.lone))
        self.assertContains(response, ONE_EXPECTED_LINE)
        self.assertNotContains(response, ONE_EXPECTED_LINE + ' ·',
                               msg_prefix='rejected shows only when there is one')

    def test_overview_card_with_no_challans_is_unchanged(self):
        response = self.client.get(self._overview_url(self.empty))
        self.assertContains(response, 'No delivery challans yet.')
        self.assertNotContains(response, 'for this site —')

    # ── query cost, before vs after ─────────────────────────────────────────

    def test_overview_pays_nothing_for_the_line(self):
        url = self._overview_url(self.site)
        self._queries(url)      # warm per-process caches before either measurement
        with mock.patch('projects.views.site_challan_counts', return_value=_STUB):
            before = self._queries(url)
        after = self._queries(url)
        self.assertEqual(after, before)

    def test_detail_page_pays_at_most_one_query_for_the_line(self):
        url = self._detail_url(self.site, self.challans[0])
        self._queries(url)
        with mock.patch('projects.views.site_challan_counts_for_project',
                        return_value=_STUB):
            before = self._queries(url)
        after = self._queries(url)
        self.assertLessEqual(after - before, 1)
        self.assertGreaterEqual(after - before, 0)
