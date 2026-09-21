"""
The navbar's Masters menu (Vendors + Warehouses) and the vendor_list gate behind it.

WHAT IS PINNED HERE:
  * user_can_view_vendor_list admits exactly SCM and Admin, the audience of the inline
    check it replaced, and vendor_list itself follows it (200 vs 403);
  * each Masters entry shows exactly when its view admits the user, and the menu is
    absent when neither does;
  * the SCM dashboard header no longer carries its own Vendors button.

Run with:
    python manage.py test projects.tests_masters_nav --settings=solarpms.test_settings
"""
from django.contrib.auth.models import AnonymousUser, User
from django.test import Client, TestCase
from django.urls import reverse

from .permissions import user_can_view_vendor_list

ROLES = ('SCM', 'Admin', 'System Admin', 'PM', 'Finance', 'CEO', 'Design',
         'Site Engineer', 'Project Coordinator')

# role -> (sees Vendors, sees Warehouses)
EXPECTED = {
    'SCM':          (True,  True),
    'Admin':        (True,  True),
    'System Admin': (False, True),
}


def _profile(username, role):
    user = User.objects.create_user(username=username, password='x',
                                    first_name=username.title(), last_name='Test')
    profile = user.profile          # auto-created by the post_save signal
    profile.role = role
    profile.save(update_fields=['role'])
    return profile


class MastersNavTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.profiles = {role: _profile(f'mn_{i}', role) for i, role in enumerate(ROLES)}

    def _client(self, role):
        client = Client(SERVER_NAME='localhost')
        client.force_login(self.profiles[role].user)
        return client

    def test_helper_admits_exactly_scm_and_admin(self):
        for role in ROLES:
            with self.subTest(role=role):
                self.assertEqual(user_can_view_vendor_list(self.profiles[role].user),
                                 role in ('SCM', 'Admin'))
        self.assertFalse(user_can_view_vendor_list(AnonymousUser()))

    def test_vendor_list_follows_the_helper(self):
        for role in ROLES:
            with self.subTest(role=role):
                status = self._client(role).get(reverse('vendor_list')).status_code
                self.assertEqual(status, 200 if role in ('SCM', 'Admin') else 403)

    def test_masters_entries_per_role(self):
        # Rendered on the notifications page: a base.html page every role may open.
        vendors_href = f'href="{reverse("vendor_list")}"'
        warehouses_href = f'href="{reverse("stock_locations")}"'
        for role in ROLES:
            sees_vendors, sees_warehouses = EXPECTED.get(role, (False, False))
            with self.subTest(role=role):
                response = self._client(role).get(reverse('notifications'))
                self.assertEqual(response.status_code, 200)
                html = response.content.decode()
                self.assertEqual('id="masters-menu"' in html,
                                 sees_vendors or sees_warehouses)
                self.assertEqual(vendors_href in html, sees_vendors)
                self.assertEqual(warehouses_href in html, sees_warehouses)

    def test_scm_dashboard_header_has_no_vendors_button(self):
        response = self._client('SCM').get(reverse('dashboard_scm'))
        self.assertNotContains(
            response, 'class="btn btn-outline-secondary btn-sm">Vendors</a>', html=False)
        # The link reaches SCM once, from the Masters menu.
        self.assertContains(response, f'href="{reverse("vendor_list")}"', count=1)
