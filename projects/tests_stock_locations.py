"""
Warehouse maintenance screens (StockLocation) and the warehouse-keeper flag on the admin
user edit screen.

WHAT IS PINNED HERE:
  * create, edit and deactivate/reactivate, and that there is no delete;
  * the three roles in STOCK_LOCATION_ADMIN_ROLES are let in and PM, Finance and Site
    Engineer are refused on every one of the four URLs;
  * only a profile already holding `is_warehouse_keeper` can be named keeper, and naming
    someone does NOT set the flag;
  * an inactive warehouse is absent from the DC-create dropdown but still shown on a
    challan that already names it;
  * the admin user edit screen saves `is_warehouse_keeper` both ways.

WHAT IS DELIBERATELY NOT TESTED: anything a keeper may do. The keeper FK is recorded,
not read, and this session added no reader of it.

Run with:
    python manage.py test projects.tests_stock_locations --settings=solarpms.test_settings
"""
from datetime import date

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import NoReverseMatch, reverse

from .models import DeliveryChallan, Project, StockLocation, UserProfile


def _profile(username, role, keeper_flag=False):
    user = User.objects.create_user(username=username, password='x',
                                    first_name=username.title(), last_name='Test')
    profile = user.profile          # auto-created by the post_save signal
    profile.role = role
    profile.is_warehouse_keeper = keeper_flag
    profile.save(update_fields=['role', 'is_warehouse_keeper'])
    return profile


def _client_for(profile):
    client = Client(SERVER_NAME='localhost')
    client.force_login(profile.user)
    return client


class StockLocationBase(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.scm = _profile('sl_scm', 'SCM')
        cls.admin = _profile('sl_admin', 'Admin')
        cls.sysadmin = _profile('sl_sysadmin', 'System Admin')
        cls.pm = _profile('sl_pm', 'PM')
        cls.finance = _profile('sl_finance', 'Finance')
        cls.se = _profile('sl_se', 'Site Engineer')
        cls.keeper = _profile('sl_keeper', 'SCM', keeper_flag=True)
        cls.unflagged = _profile('sl_unflagged', 'SCM')

    def setUp(self):
        self.location = StockLocation.objects.create(code='HYD-1', name='Hyderabad Store')


class StockLocationAccessTests(StockLocationBase):

    def _urls(self):
        pk = self.location.pk
        return [
            ('get',  reverse('stock_locations')),
            ('get',  reverse('stock_location_create')),
            ('get',  reverse('stock_location_edit', args=[pk])),
            ('post', reverse('stock_location_toggle', args=[pk])),
        ]

    def test_admin_system_admin_and_scm_are_let_in(self):
        for profile in (self.admin, self.sysadmin, self.scm):
            client = _client_for(profile)
            for method, url in self._urls()[:3]:
                with self.subTest(role=profile.role, url=url):
                    self.assertEqual(getattr(client, method)(url).status_code, 200)

    def test_pm_finance_and_site_engineer_are_refused(self):
        for profile in (self.pm, self.finance, self.se):
            client = _client_for(profile)
            for method, url in self._urls():
                with self.subTest(role=profile.role, url=url):
                    self.assertEqual(getattr(client, method)(url).status_code, 403)
        self.location.refresh_from_db()
        self.assertTrue(self.location.is_active, 'a refused toggle must not write')

    def test_refused_create_writes_nothing(self):
        _client_for(self.pm).post(reverse('stock_location_create'),
                                  {'code': 'PM-1', 'name': 'Nope', 'keeper': ''})
        self.assertFalse(StockLocation.objects.filter(code='PM-1').exists())

    def test_nav_entry_follows_the_helper(self):
        """R-11: the screen ships with its way in. SCM reaches it from the main navbar."""
        url = reverse('stock_locations')
        scm_home = _client_for(self.scm).get(reverse('notifications'))
        self.assertContains(scm_home, url)
        pm_home = _client_for(self.pm).get(reverse('notifications'))
        self.assertNotContains(pm_home, url)

    def test_subadmin_shell_hides_system_admin_links_from_scm(self):
        page = _client_for(self.scm).get(reverse('stock_locations'))
        self.assertNotContains(page, reverse('subadmin_departments'))
        page = _client_for(self.sysadmin).get(reverse('stock_locations'))
        self.assertContains(page, reverse('subadmin_departments'))


class StockLocationCrudTests(StockLocationBase):

    def test_create(self):
        response = _client_for(self.scm).post(reverse('stock_location_create'), {
            'code': 'MUM-1', 'name': 'Mumbai Central', 'keeper': self.keeper.pk,
        })
        self.assertRedirects(response, reverse('stock_locations'),
                             fetch_redirect_response=False)
        location = StockLocation.objects.get(code='MUM-1')
        self.assertEqual(location.name, 'Mumbai Central')
        self.assertEqual(location.keeper, self.keeper)
        self.assertTrue(location.is_active)

    def test_create_without_keeper(self):
        _client_for(self.admin).post(reverse('stock_location_create'),
                                     {'code': 'DEL-1', 'name': 'Delhi', 'keeper': ''})
        self.assertIsNone(StockLocation.objects.get(code='DEL-1').keeper)

    def test_duplicate_code_refused(self):
        response = _client_for(self.scm).post(reverse('stock_location_create'),
                                              {'code': 'HYD-1', 'name': 'Dup', 'keeper': ''})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(StockLocation.objects.filter(code='HYD-1').count(), 1)

    def test_edit(self):
        response = _client_for(self.sysadmin).post(
            reverse('stock_location_edit', args=[self.location.pk]),
            {'code': 'HYD-2', 'name': 'Hyderabad North', 'keeper': self.keeper.pk},
        )
        self.assertEqual(response.status_code, 302)
        self.location.refresh_from_db()
        self.assertEqual((self.location.code, self.location.name, self.location.keeper),
                         ('HYD-2', 'Hyderabad North', self.keeper))

    def test_deactivate_and_reactivate(self):
        client = _client_for(self.scm)
        url = reverse('stock_location_toggle', args=[self.location.pk])
        client.post(url)
        self.location.refresh_from_db()
        self.assertFalse(self.location.is_active)
        self.assertTrue(StockLocation.objects.filter(pk=self.location.pk).exists())
        client.post(url)
        self.location.refresh_from_db()
        self.assertTrue(self.location.is_active)

    def test_toggle_is_post_only(self):
        _client_for(self.scm).get(reverse('stock_location_toggle', args=[self.location.pk]))
        self.location.refresh_from_db()
        self.assertTrue(self.location.is_active)

    def test_edit_form_does_not_touch_is_active(self):
        """Retirement has its own POST; saving the edit form must never close a warehouse."""
        self.location.is_active = False
        self.location.save(update_fields=['is_active'])
        _client_for(self.scm).post(reverse('stock_location_edit', args=[self.location.pk]),
                                   {'code': 'HYD-1', 'name': 'Renamed', 'keeper': ''})
        self.location.refresh_from_db()
        self.assertEqual(self.location.name, 'Renamed')
        self.assertFalse(self.location.is_active)

    def test_there_is_no_delete(self):
        for name in ('stock_location_delete',):
            with self.assertRaises(NoReverseMatch):
                reverse(name, args=[self.location.pk])


class KeeperRuleTests(StockLocationBase):

    def test_only_flagged_users_are_offered(self):
        page = _client_for(self.scm).get(reverse('stock_location_create'))
        self.assertContains(page, f'value="{self.keeper.pk}"')
        self.assertNotContains(page, f'value="{self.unflagged.pk}"')

    def test_unflagged_keeper_is_refused_on_create(self):
        response = _client_for(self.scm).post(reverse('stock_location_create'), {
            'code': 'BLR-1', 'name': 'Bengaluru', 'keeper': self.unflagged.pk,
        })
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['form'].errors.get('keeper'))
        self.assertFalse(StockLocation.objects.filter(code='BLR-1').exists())

    def test_unflagged_keeper_is_refused_on_edit(self):
        _client_for(self.scm).post(reverse('stock_location_edit', args=[self.location.pk]),
                                   {'code': 'HYD-1', 'name': 'x', 'keeper': self.unflagged.pk})
        self.location.refresh_from_db()
        self.assertIsNone(self.location.keeper)
        self.assertEqual(self.location.name, 'Hyderabad Store')

    def test_choosing_a_keeper_does_not_set_the_flag(self):
        """The flag is granted on the user edit screen and nowhere else. Choosing a
        flagged user must leave every profile's flag exactly as it was."""
        before = dict(UserProfile.objects.values_list('pk', 'is_warehouse_keeper'))
        _client_for(self.scm).post(reverse('stock_location_edit', args=[self.location.pk]),
                                   {'code': 'HYD-1', 'name': 'x', 'keeper': self.keeper.pk})
        _client_for(self.scm).post(reverse('stock_location_create'),
                                   {'code': 'X-1', 'name': 'x', 'keeper': self.unflagged.pk})
        after = dict(UserProfile.objects.values_list('pk', 'is_warehouse_keeper'))
        self.assertEqual(before, after)


class DeliveryChallanWarehouseTests(StockLocationBase):

    def setUp(self):
        super().setUp()
        self.project = Project.objects.create(
            customer_name='SL Customer', customer_phone='9876543210',
            site_address='1 Store Lane', city='Hyderabad',
            project_type='Residential', status='Active', assigned_pm=self.pm,
        )
        self.challan = DeliveryChallan.objects.create(
            project=self.project, dc_number='DC-SL-001', dc_date=date.today(),
            status=DeliveryChallan.EXPECTED, created_by=self.scm,
            issued_from_warehouse=self.location,
        )
        self.location.is_active = False
        self.location.save(update_fields=['is_active'])

    def test_inactive_location_absent_from_dc_create_dropdown(self):
        StockLocation.objects.create(code='LIVE-1', name='Still Open')
        page = _client_for(self.scm).get(
            reverse('create_delivery_challan', args=[self.project.project_id]))
        self.assertEqual(page.status_code, 200)
        codes = [w.code for w in page.context['warehouses']]
        self.assertEqual(codes, ['LIVE-1'])
        self.assertNotContains(page, 'Hyderabad Store')

    def test_inactive_location_still_shown_on_existing_challan(self):
        page = _client_for(self.scm).get(reverse(
            'delivery_challan_detail', args=[self.project.project_id, self.challan.pk]))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'HYD-1')
        self.assertContains(page, 'Hyderabad Store')
        self.assertContains(page, 'Inactive')
        self.challan.refresh_from_db()
        self.assertEqual(self.challan.issued_from_warehouse, self.location)


class WarehouseKeeperFlagTests(TestCase):
    """The checkbox on admin/user_edit.html, beside is_qaqc, same save path."""

    def setUp(self):
        self.admin = _profile('flag_admin', 'Admin')
        self.target = _profile('flagtarget', 'Site Engineer')
        self.url = reverse('admin_user_edit', args=[self.target.user.pk])

    def _post(self, **flags):
        data = {
            'first_name': 'Flag', 'last_name': 'Target', 'username': 'flagtarget',
            'email': 'flag@example.com', 'phone_number': '9876543210',
            'role': 'Site Engineer', 'new_password': '', 'confirm_password': '',
        }
        data.update({k: 'on' for k, v in flags.items() if v})
        return _client_for(self.admin).post(self.url, data)

    def test_checkbox_renders(self):
        page = _client_for(self.admin).get(self.url)
        self.assertContains(page, 'name="is_warehouse_keeper"')

    def test_checkbox_saves_on_and_off(self):
        response = self._post(is_warehouse_keeper=True)
        self.assertEqual(response.status_code, 302)
        self.target.refresh_from_db()
        self.assertTrue(self.target.is_warehouse_keeper)
        self.assertFalse(self.target.is_qaqc, 'the neighbouring flag is untouched')

        self._post()
        self.target.refresh_from_db()
        self.assertFalse(self.target.is_warehouse_keeper)

    def test_checkbox_initial_reflects_the_flag(self):
        self.target.is_warehouse_keeper = True
        self.target.save(update_fields=['is_warehouse_keeper'])
        page = _client_for(self.admin).get(self.url)
        self.assertTrue(page.context['form'].initial['is_warehouse_keeper'])
