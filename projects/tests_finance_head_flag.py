"""FIN-H-B1 — the Finance Head flag, its two permission helpers and its Admin setting.

Rulings H10 (Finance role only, several holders, set by Admin only) and H12 (the page is
open to the Finance Head and to CEO, Admin, System Admin). The flag is separate from
is_payment_approver: the last class proves neither save touches the other.
"""

from django.contrib.auth.models import AnonymousUser, User
from django.test import Client, TestCase
from django.urls import reverse

from .models import UserProfile
from .permissions import user_can_view_finance_head_page, user_is_finance_head


def _profile(username, role, finance_head=False, payment_approver=False):
    """A user with the given role and flags. The profile is auto-created by the User
    post_save signal, so it is fetched and edited, never created."""
    user = User.objects.create_user(username=username, password='x',
                                    first_name=username.title(), last_name='Test')
    profile = user.profile
    profile.role = role
    profile.is_finance_head = finance_head
    profile.is_payment_approver = payment_approver
    profile.save(update_fields=['role', 'is_finance_head', 'is_payment_approver'])
    return profile


def _client_for(user):
    # ALLOWED_HOSTS has no 'testserver', so the default Client would 400.
    client = Client(SERVER_NAME='localhost')
    client.force_login(user)
    return client


def _fresh(profile):
    """Re-read the user so `user.profile` is loaded from the database, not cached."""
    return User.objects.get(pk=profile.user_id)


class UserIsFinanceHeadTests(TestCase):
    """Ruling 4 truth table for permissions.user_is_finance_head()."""

    def test_finance_with_flag_and_active(self):
        self.assertTrue(user_is_finance_head(_fresh(_profile('fh_yes', 'Finance', True))))

    def test_finance_without_flag(self):
        self.assertFalse(user_is_finance_head(_fresh(_profile('fh_plain', 'Finance'))))

    def test_non_finance_with_flag_set_directly_in_db(self):
        # The Admin form refuses this; a role change on the Departments screens or the
        # Django admin can still leave it, and the helper must ignore it.
        profile = _profile('fh_pm', 'PM')
        UserProfile.objects.filter(pk=profile.pk).update(is_finance_head=True)
        self.assertFalse(user_is_finance_head(_fresh(profile)))

    def test_inactive_profile(self):
        profile = _profile('fh_offp', 'Finance', True)
        UserProfile.objects.filter(pk=profile.pk).update(is_active=False)
        self.assertFalse(user_is_finance_head(_fresh(profile)))

    def test_inactive_login(self):
        profile = _profile('fh_offu', 'Finance', True)
        User.objects.filter(pk=profile.user_id).update(is_active=False)
        self.assertFalse(user_is_finance_head(_fresh(profile)))

    def test_no_profile(self):
        profile = _profile('fh_noprof', 'Finance', True)
        user_id = profile.user_id
        profile.delete()
        self.assertFalse(user_is_finance_head(User.objects.get(pk=user_id)))

    def test_anonymous(self):
        self.assertFalse(user_is_finance_head(AnonymousUser()))


class FinanceHeadPageHelperTests(TestCase):
    """Ruling 5 truth table for permissions.user_can_view_finance_head_page()."""

    def test_allowed(self):
        for name, role, flag in [('pg_fh', 'Finance', True), ('pg_ceo', 'CEO', False),
                                 ('pg_admin', 'Admin', False),
                                 ('pg_sa', 'System Admin', False)]:
            with self.subTest(role=role):
                self.assertTrue(user_can_view_finance_head_page(
                    _fresh(_profile(name, role, flag))))

    def test_refused(self):
        for name, role in [('pg_fin', 'Finance'), ('pg_pm', 'PM'), ('pg_scm', 'SCM'),
                           ('pg_coord', 'Project Coordinator'), ('pg_bd', 'BD'),
                           ('pg_design', 'Design')]:
            with self.subTest(role=role):
                self.assertFalse(user_can_view_finance_head_page(
                    _fresh(_profile(name, role))))

    def test_payment_approver_alone_does_not_admit(self):
        self.assertFalse(user_can_view_finance_head_page(
            _fresh(_profile('pg_appr', 'Finance', payment_approver=True))))

    def test_inactive_ceo_refused(self):
        profile = _profile('pg_ceo_off', 'CEO')
        UserProfile.objects.filter(pk=profile.pk).update(is_active=False)
        self.assertFalse(user_can_view_finance_head_page(_fresh(profile)))

    def test_inactive_ceo_login_refused(self):
        profile = _profile('pg_ceo_offu', 'CEO')
        User.objects.filter(pk=profile.user_id).update(is_active=False)
        self.assertFalse(user_can_view_finance_head_page(_fresh(profile)))

    def test_anonymous(self):
        self.assertFalse(user_can_view_finance_head_page(AnonymousUser()))


class AdminFormFinanceHeadTests(TestCase):
    """Ruling 3 and the Admin Panel save path (admin_user_edit)."""

    REFUSAL = 'Finance Head can only be set on a Finance user.'

    def setUp(self):
        self.admin = _profile('fhf_admin', 'Admin')
        self.target = _profile('fhftarget', 'Finance')
        self.url = reverse('admin_user_edit', args=[self.target.user.pk])

    def _post(self, role='Finance', first_name='Flag', **flags):
        data = {
            'first_name': first_name, 'last_name': 'Target', 'username': 'fhftarget',
            'email': 'fh@example.com', 'phone_number': '9876543210',
            'role': role, 'new_password': '', 'confirm_password': '',
        }
        data.update({k: 'on' for k, v in flags.items() if v})
        return _client_for(self.admin.user).post(self.url, data)

    def test_tick_saves_and_untick_clears(self):
        self.assertEqual(self._post(is_finance_head=True).status_code, 302)
        self.target.refresh_from_db()
        self.assertTrue(self.target.is_finance_head)

        self.assertEqual(self._post().status_code, 302)
        self.target.refresh_from_db()
        self.assertFalse(self.target.is_finance_head)

    def test_tick_on_pm_refused_and_nothing_saved(self):
        response = self._post(role='PM', first_name='Changed', is_finance_head=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.REFUSAL)
        self.target.refresh_from_db()
        self.target.user.refresh_from_db()
        self.assertFalse(self.target.is_finance_head)
        self.assertEqual(self.target.role, 'Finance')
        self.assertEqual(self.target.user.first_name, 'Fhftarget')

    def test_other_field_change_keeps_flag(self):
        # The "unrendered box" hazard: a normal save of a Finance Head, posting the box
        # as the rendered page would, must not clear the flag.
        UserProfile.objects.filter(pk=self.target.pk).update(is_finance_head=True)
        page = _client_for(self.admin.user).get(self.url).content.decode()
        self.assertRegex(page, r'name="is_finance_head"[^>]*\bchecked\b')
        self.assertEqual(self._post(first_name='Renamed', is_finance_head=True).status_code, 302)
        self.target.refresh_from_db()
        self.target.user.refresh_from_db()
        self.assertTrue(self.target.is_finance_head)
        self.assertEqual(self.target.user.first_name, 'Renamed')

    def test_edit_page_shows_checkbox_and_help_text(self):
        page = _client_for(self.admin.user).get(self.url)
        self.assertContains(page, 'name="is_finance_head"')
        self.assertContains(page, 'Finance Head — sees the Finance Head dashboard.')


class FinanceHeadSeparateFromApproverTests(TestCase):
    """Ruling 6: setting or clearing one flag never moves the other."""

    def setUp(self):
        self.admin = _profile('sep_admin', 'Admin')
        self.target = _profile('septarget', 'Finance')
        self.url = reverse('admin_user_edit', args=[self.target.user.pk])

    def _post(self, **flags):
        data = {
            'first_name': 'Sep', 'last_name': 'Target', 'username': 'septarget',
            'email': 'sep@example.com', 'phone_number': '9876543210',
            'role': 'Finance', 'new_password': '', 'confirm_password': '',
        }
        data.update({k: 'on' for k, v in flags.items() if v})
        self.assertEqual(_client_for(self.admin.user).post(self.url, data).status_code, 302)
        self.target.refresh_from_db()

    def test_head_on_and_off_leaves_approver(self):
        self._post(is_payment_approver=True)
        self._post(is_payment_approver=True, is_finance_head=True)
        self.assertTrue(self.target.is_payment_approver)
        self._post(is_payment_approver=True)
        self.assertFalse(self.target.is_finance_head)
        self.assertTrue(self.target.is_payment_approver)

    def test_approver_on_and_off_leaves_head(self):
        self._post(is_finance_head=True)
        self._post(is_finance_head=True, is_payment_approver=True)
        self.assertTrue(self.target.is_finance_head)
        self._post(is_finance_head=True)
        self.assertFalse(self.target.is_payment_approver)
        self.assertTrue(self.target.is_finance_head)


class DjangoAdminChangelistTests(TestCase):
    """The UserProfile changelist loads with the new filter applied."""

    def test_changelist_with_filter(self):
        superuser = User.objects.create_superuser('fh_su', 'su@example.com', 'x')
        # AdminAccessMiddleware redirects any non-'Admin' role away from /admin/.
        UserProfile.objects.filter(user=superuser).update(role='Admin')
        _profile('fh_listed', 'Finance', True)
        url = reverse('admin:projects_userprofile_changelist')
        response = _client_for(superuser).get(url, {'is_finance_head__exact': '1'})
        # 200, not the 302 to ?e=1 the admin returns for an unknown filter field.
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'fh_listed')
