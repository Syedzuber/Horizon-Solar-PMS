"""
Admin Panel All Projects screen: the ?type= project-type filter.

WHAT IS PINNED HERE:
  * each stored project_type returns only its own rows;
  * the 'RESCO' pill is stored 'OPEX' — ?type=OPEX returns OPEX rows, and the display
    label ?type=RESCO is NOT a filter value (it falls back to All like any unknown);
  * an unknown value falls back to All, never a 500;
  * the "N projects total" line counts the filtered rows;
  * soft-deleted projects stay out under every filter.

Run with:
    python manage.py test projects.tests_admin_project_type_filter --settings=solarpms.test_settings
"""
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse

from .models import Project


def _profile(username, role):
    user = User.objects.create_user(username=username, password='x',
                                    first_name=username.title(), last_name='Test')
    profile = user.profile          # auto-created by the post_save signal
    profile.role = role
    profile.save(update_fields=['role'])
    return profile


def _project(name, project_type, **extra):
    return Project.objects.create(
        customer_name=name,
        customer_phone='9876543210',
        site_address='1 Sun Road',
        city='Lucknow',
        project_type=project_type,
        dc_capacity_kw=Decimal('5.00'),
        status='Draft',
        target_commissioning_date=date.today() + timedelta(days=90),
        **extra,
    )


class AdminProjectTypeFilterTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.admin = _profile('ptf_admin', 'Admin')
        cls.resi = [_project('Resi One', 'Residential'), _project('Resi Two', 'Residential')]
        cls.opex = [_project('Opex One', 'OPEX'), _project('Opex Two', 'OPEX'),
                    _project('Opex Three', 'OPEX')]
        cls.capex = [_project('Capex One', 'CAPEX')]
        cls.deleted = _project('Opex Deleted', 'OPEX', is_deleted=True)

    def setUp(self):
        self.client = Client(SERVER_NAME='localhost')
        self.client.force_login(self.admin.user)
        self.url = reverse('admin_project_list')

    def _get(self, **params):
        response = self.client.get(self.url, params)
        self.assertEqual(response.status_code, 200)
        return response

    def _pks(self, response):
        return {p.pk for p in response.context['projects']}

    def test_all_is_the_default(self):
        response = self._get()
        self.assertEqual(response.context['selected_type'], '')
        self.assertEqual(self._pks(response),
                         {p.pk for p in self.resi + self.opex + self.capex})

    def test_each_type_returns_only_its_rows(self):
        for stored, expected in (('Residential', self.resi),
                                 ('OPEX', self.opex),
                                 ('CAPEX', self.capex)):
            with self.subTest(type=stored):
                response = self._get(type=stored)
                self.assertEqual(response.context['selected_type'], stored)
                self.assertEqual(self._pks(response), {p.pk for p in expected})

    def test_resco_pill_links_to_stored_opex(self):
        response = self._get()
        self.assertContains(response, 'href="?type=OPEX"')
        self.assertContains(response, '>RESCO</a>')
        self.assertNotContains(response, '?type=RESCO')

    def test_resco_filter_returns_stored_opex_rows(self):
        response = self._get(type='OPEX')
        self.assertEqual(self._pks(response), {p.pk for p in self.opex})
        self.assertNotIn(self.deleted.pk, self._pks(response))

    def test_unknown_value_falls_back_to_all(self):
        everything = {p.pk for p in self.resi + self.opex + self.capex}
        for junk in ('RESCO', 'residential', 'bogus', "'; DROP TABLE", ''):
            with self.subTest(type=junk):
                response = self._get(type=junk)
                self.assertEqual(response.context['selected_type'], '')
                self.assertEqual(self._pks(response), everything)

    def test_count_line_matches_filtered_rows(self):
        for params, n in (({}, 6), ({'type': 'Residential'}, 2),
                          ({'type': 'OPEX'}, 3), ({'type': 'CAPEX'}, 1),
                          ({'type': 'bogus'}, 6)):
            with self.subTest(params=params):
                response = self._get(**params)
                label = 'project' if n == 1 else 'projects'
                self.assertContains(response, f'{n} {label} total')
