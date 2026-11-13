"""
FIN-M-B8b — Finance lands on the Finance work page (dashboard thread, 11-12 Nov; rulings
of 13 Nov: More-row option (b), retired redirect option A, Finance branch by role).

WHAT THIS PINS
    Landing      Finance's login POST, an authenticated /login/, `/`, Home and the 403
                 page's return link all go to /finance/work/
    Old URLs     every retired dashboard URL (and /landing/) 302s Finance to /finance/work/
                 (option A: the decorator sends each user to their own landing)
    One click    Finance still opens /dashboard/sites/; the Finance work page links to it;
                 the Site Dashboard's More row links back for everyone the Finance work page
                 admits (Finance, CEO, Admin, System Admin) and for nobody else
    Unchanged    CEO, Admin, System Admin, PM, coordinator, SCM and the Design Head still
                 land on the Site Dashboard; Site Engineer, designer and BD where they did
    Switch off   RETIRED_DASHBOARDS_REDIRECT=False changes nothing about Finance's login
                 (no production env change needed) and serves the old Finance page again

The Finance work URL is written out here, not imported from decorators, so this module
loads against the code before B8b and its tests fail there rather than erroring.

Today is frozen at 9 Nov 2026 by SchedFixture (through DashFixture).

Run with:
    python manage.py test projects.tests_finance_landing --settings=solarpms.test_settings
"""
from django.test import Client, RequestFactory, override_settings
from django.urls import reverse

from .context_processors import home_url
from .decorators import SITE_DASHBOARD_URL, get_post_login_url, get_user_dashboard
from .tests_dash_b4 import OLD_URLS, B4Fixture, _client
from .tests_site_dashboard import _fresh

FINANCE_WORK = '/finance/work/'


class FinanceLandingFixture(B4Fixture):

    def site_landers(self):
        """The Site Dashboard roles whose landing B8b leaves alone: every R1 role but Finance."""
        return tuple(p for p in self.r1() if p is not self.finance)

    def _login(self, profile):
        return Client().post('/login/', {'username': profile.user.username, 'password': 'x'})


# ---------------------------------------------------------------------------
# 1. Finance lands on Finance work
# ---------------------------------------------------------------------------

class FinanceLandsOnFinanceWorkTests(FinanceLandingFixture):

    def test_the_constant_is_the_finance_work_url(self):
        from . import decorators
        self.assertEqual(reverse('finance_work'), FINANCE_WORK)
        self.assertEqual(getattr(decorators, 'FINANCE_WORK_URL', None), FINANCE_WORK)
        self.assertEqual(decorators.ROLE_DASHBOARD['Finance'], FINANCE_WORK)

    def test_login_post_goes_to_finance_work(self):
        response = self._login(self.finance)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], FINANCE_WORK)
        self.assertEqual(get_post_login_url(_fresh(self.finance.user)), FINANCE_WORK)

    def test_authenticated_login_page_and_root_go_to_finance_work(self):
        client = _client(self.finance)
        self.assertEqual(client.get('/login/')['Location'], FINANCE_WORK)
        response = client.get('/', follow=True)
        self.assertEqual(response.redirect_chain[-1][0], FINANCE_WORK)
        self.assertEqual(response.status_code, 200)

    def test_home_link_is_finance_work(self):
        request = RequestFactory().get('/')
        request.user = _fresh(self.finance.user)
        self.assertEqual(home_url(request), {'home_url': FINANCE_WORK})
        # A context argument (LANDING_ROLES) does not send Finance anywhere else.
        self.assertEqual(get_user_dashboard(_fresh(self.finance.user), context='residential'),
                         FINANCE_WORK)
        # And the rendered nav's Home control points there, on any page Finance opens.
        self.assertContains(self._get(self.finance),
                            f'<a href="{FINANCE_WORK}" class="nav-link text-white small" title="Home">')

    def test_the_403_page_returns_finance_to_finance_work(self):
        response = _client(self.finance).get('/dashboard/bd/')
        self.assertEqual(response.status_code, 403)
        self.assertContains(response, f'href="{FINANCE_WORK}"', status_code=403)
        self.assertNotContains(response, f'href="{SITE_DASHBOARD_URL}"', status_code=403)

    def test_every_old_url_sends_finance_to_finance_work(self):
        client = _client(self.finance)
        for url in OLD_URLS:
            with self.subTest(url=url):
                response = client.get(url)
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response['Location'], FINANCE_WORK)

    def test_the_old_finance_url_with_a_context_also_goes_to_finance_work(self):
        response = _client(self.finance).get('/dashboard/finance/?context=residential')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], FINANCE_WORK)


# ---------------------------------------------------------------------------
# 2. The Site Dashboard stays one click away, both ways
# ---------------------------------------------------------------------------

class OneClickAwayTests(FinanceLandingFixture):

    def test_finance_still_opens_the_site_dashboard(self):
        self.assertEqual(self._get(self.finance).status_code, 200)

    def test_finance_work_links_to_the_site_list(self):
        page = _client(self.finance).get(FINANCE_WORK)
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, f'href="{SITE_DASHBOARD_URL}" data-site-list')

    def _finance_links(self, profile):
        groups = {g['label']: g['links'] for g in self._get(profile).context['more_groups']}
        return {link['label']: link['href'] for link in groups.get('Finance', [])}

    def test_more_row_links_to_finance_work_for_its_readers(self):
        for profile in (self.finance, self.ceo, self.admin, self.sysadmin):
            with self.subTest(role=profile.role):
                links = self._finance_links(profile)
                self.assertEqual(links.get('Finance work'), FINANCE_WORK)
                # The link opens for its viewer: no 403 behind it.
                self.assertEqual(_client(profile).get(FINANCE_WORK).status_code, 200)

    def test_more_row_has_no_finance_work_link_for_anyone_else(self):
        for profile in (self.pm, self.coord, self.scm, self.head):
            with self.subTest(role=profile.role, head=profile.is_design_head):
                self.assertNotIn('Finance work', self._finance_links(profile))
                self.assertEqual(_client(profile).get(FINANCE_WORK).status_code, 403)


# ---------------------------------------------------------------------------
# 3. No other role's landing moves
# ---------------------------------------------------------------------------

class OtherRolesUnchangedTests(FinanceLandingFixture):

    def test_other_site_dashboard_roles_still_land_on_the_site_dashboard(self):
        factory = RequestFactory()
        for profile in self.site_landers():
            with self.subTest(role=profile.role, head=profile.is_design_head):
                self.assertEqual(self._login(profile)['Location'], SITE_DASHBOARD_URL)
                request = factory.get('/')
                request.user = _fresh(profile.user)
                self.assertEqual(home_url(request), {'home_url': SITE_DASHBOARD_URL})

    def test_ceo_admin_and_system_admin_read_finance_work_but_do_not_land_there(self):
        for profile in (self.ceo, self.admin, self.sysadmin):
            with self.subTest(role=profile.role):
                self.assertEqual(get_user_dashboard(_fresh(profile.user)), SITE_DASHBOARD_URL)

    def test_other_site_dashboard_roles_old_urls_still_go_to_the_site_dashboard(self):
        for profile in self.site_landers():
            client = _client(profile)
            for url in OLD_URLS:
                with self.subTest(role=profile.role, head=profile.is_design_head, url=url):
                    response = client.get(url)
                    self.assertEqual(response.status_code, 302)
                    self.assertEqual(response['Location'], SITE_DASHBOARD_URL)

    def test_site_engineer_designer_and_bd_land_where_they_did(self):
        for profile, home in self.others():
            with self.subTest(role=profile.role):
                self.assertEqual(self._login(profile)['Location'], home)
                self.assertEqual(get_user_dashboard(_fresh(profile.user)), home)


# ---------------------------------------------------------------------------
# 4. The switch needs no production change
# ---------------------------------------------------------------------------

@override_settings(RETIRED_DASHBOARDS_REDIRECT=False)
class SwitchOffTests(FinanceLandingFixture):

    def test_switch_off_finance_still_logs_in_to_finance_work(self):
        self.assertEqual(self._login(self.finance)['Location'], FINANCE_WORK)

    def test_switch_off_serves_the_old_finance_page(self):
        self.assertEqual(self._get(self.finance, '/dashboard/finance/').status_code, 200)
