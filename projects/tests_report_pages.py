"""
RPT-B2a — the Reports pages: the list of reports, the report view shell and the Active
users report.

WHAT THIS PINS (each test fails if its rule is wrong)
    Access        CEO, Admin and System Admin get both pages; PM, SCM, Finance, Design,
                  Site Engineer and Coordinator get the 403 page; an inactive profile gets
                  it too; an inactive login and anonymous go to /login/; an unknown slug
                  is 404 for those let in and 403 for those not
    Periods       every period key renders; a bad range shows the red line ABOVE today's
                  figures (B1 open question 5); a range ending today says "to today"
    Tiles         an "as of now" tile never shows a comparison (D4); "was 0" when the
                  previous figure is 0
    Nav           the Reports link for the three roles, not for a PM, in base.html and in
                  the System Admin shell; the More row's CEO group carries it
    Queries       the page costs the same for 3 sites and 30, and for 1 day and 92 (D8)
    Parity        the page's tiles equal report_data_check's figures for the same period
    Edge cases    no activity; 140 sites; only site-less actions; System Admin = CEO

Run with:
    python manage.py test projects.tests_report_pages --settings=solarpms.test_settings
"""
import re
from datetime import timedelta
from io import StringIO

from django.contrib.auth.models import User
from django.core.management import call_command
from django.db import connection
from django.template.loader import render_to_string
from django.test import Client, RequestFactory, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from .models import ActivityLog, UserProfile
from .report_pages import compare
from .tests_report_foundations import DAY, ActivityFixture, ist
from .tests_two_step_completion import _profile
from .views_site_dashboard import _more_links

URL = '/reports/active-users/'
DAY_PARAMS = {'period': 'date', 'date': DAY.isoformat()}

REPORT_ROLES = ('CEO', 'Admin', 'System Admin')
REFUSED_ROLES = ('PM', 'SCM', 'Finance', 'Design', 'Site Engineer', 'Project Coordinator')


def tiles(html):
    """{label: (value, comparison text)} read from the rendered tiles, tags stripped."""
    found = {}
    for block in re.findall(r'<div class="tile[^"]*">(.*?)\n</div>', html, re.S):
        label = re.search(r'<div class="l">(.*?)</div>', block, re.S).group(1)
        value = re.search(r'<div class="v">(.*?)</div>', block, re.S).group(1)
        lines = re.findall(r'<div class="d">(.*?)</div>', block, re.S)
        text = ' '.join(re.sub(r'<[^>]+>', ' ', line) for line in lines)
        found[label] = (int(value), ' '.join(text.split()))
    return found


class PagesFixture(ActivityFixture):
    """ActivityFixture's people and row builders, plus one account per role."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.people = {role: _profile(f'rp_{role.lower().replace(" ", "_")}', role)
                      for role in REPORT_ROLES + REFUSED_ROLES}

    def client_for(self, profile):
        client = Client(SERVER_NAME='localhost')
        client.force_login(profile.user)
        return client


class AccessTests(PagesFixture):

    def test_the_three_roles_open_both_pages(self):
        for role in REPORT_ROLES:
            client = self.client_for(self.people[role])
            for url in ('/reports/', URL):
                self.assertEqual(client.get(url).status_code, 200, (role, url))

    def test_every_other_role_gets_the_403_page(self):
        for role in REFUSED_ROLES:
            client = self.client_for(self.people[role])
            for url in ('/reports/', URL, '/reports/no-such-report/'):
                response = client.get(url)
                self.assertEqual(response.status_code, 403, (role, url))
                # The project's own 403 page (decorators._forbidden renders 403.html).
                self.assertIn(b'</html>', response.content)

    def test_the_refusal_matches_the_daily_reports(self):
        client = self.client_for(self.people['PM'])
        ours, theirs = client.get(URL), client.get(reverse('ceo_daily_report'))
        self.assertEqual((ours.status_code, ours.content), (theirs.status_code, theirs.content))

    def test_an_inactive_profile_is_refused(self):
        ceo = self.people['CEO']
        UserProfile.objects.filter(pk=ceo.pk).update(is_active=False)
        self.assertEqual(self.client_for(ceo).get(URL).status_code, 403)

    def test_an_inactive_login_is_sent_to_sign_in(self):
        ceo = self.people['CEO']
        client = self.client_for(ceo)
        User.objects.filter(pk=ceo.user_id).update(is_active=False)
        for url in ('/reports/', URL):
            self.assertRedirects(client.get(url), '/login/', fetch_redirect_response=False)

    def test_anonymous_is_sent_to_sign_in(self):
        client = Client(SERVER_NAME='localhost')
        for url in ('/reports/', URL):
            self.assertRedirects(client.get(url), '/login/', fetch_redirect_response=False)

    def test_an_unknown_report_is_404(self):
        self.assertEqual(self.client_for(self.people['CEO']).get('/reports/nope/').status_code,
                         404)

    def test_the_daily_report_keeps_its_address(self):
        response = self.client_for(self.people['CEO']).get('/reports/user-status/')
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'projects/reports/ceo_daily_report.html')

    def test_post_is_not_allowed(self):
        client = self.client_for(self.people['CEO'])
        self.assertEqual(client.post(URL).status_code, 405)


class HomePageTests(PagesFixture):

    def test_the_cards(self):
        html = self.client_for(self.people['CEO']).get('/reports/').content.decode()
        self.assertIn('Active users', html)
        self.assertIn('Daily user status', html)
        self.assertIn(f'action="{URL}"', html)
        self.assertIn(f'action="{reverse("ceo_daily_report")}"', html)
        self.assertIn("How it's counted", html)
        for label in ('Today', 'Yesterday', 'This week', 'This month', 'On date', 'Date range'):
            self.assertIn(f'<span>{label}</span>', html)
        self.assertIn('Generate report', html)
        # The form is a plain GET: no fetch, no XHR on the page.
        self.assertIn('method="get"', html)
        self.assertNotIn('fetch(', html)
        # Template comments never leak onto the page.
        self.assertNotIn('RPT-B2a level 1', html)


class PeriodTests(PagesFixture):

    def test_every_period_key_renders(self):
        client = self.client_for(self.people['CEO'])
        for params, label in (
                ({'period': 'today'}, 'Today, '),
                ({'period': 'yesterday'}, 'Yesterday, '),
                ({'period': 'week'}, 'This week, '),
                ({'period': 'month'}, 'This month, '),
                (DAY_PARAMS, f'{DAY.day} {DAY:%b %Y}'),
                ({'period': 'range', 'from': (DAY - timedelta(days=9)).isoformat(),
                  'to': DAY.isoformat()}, '(10 days)')):
            response = client.get(URL, params)
            self.assertEqual(response.status_code, 200, params)
            self.assertIsNone(response.context['period_error'], params)
            self.assertIn(label, response.content.decode(), params)
        self.assertEqual(response.context['period'].start_date, DAY - timedelta(days=9))

    def test_a_bad_range_shows_the_error_above_todays_figures(self):
        today = timezone.localdate()
        site = self._site()
        self._log(site, self.pm, timezone.now() - timedelta(minutes=1))     # today
        self._log(site, self.designer, ist(DAY, 10))                       # not today
        response = self.client_for(self.people['CEO']).get(URL, {
            'period': 'range', 'from': DAY.isoformat(),
            'to': (DAY - timedelta(days=3)).isoformat()})
        html = response.content.decode()
        self.assertIn('class="err" role="alert">From date is after To date. Showing today', html)
        self.assertLess(html.index('class="err"'), html.index('class="tiles"'))
        self.assertIn(f'Today, {today.day} {today:%b %Y}', html)
        self.assertEqual(tiles(html)['Active users'][0], 1)
        # The form keeps what was sent, so the range can be corrected in place.
        self.assertEqual(response.context['form']['period'], 'range')
        self.assertEqual(response.context['form']['from'], DAY.isoformat())

    def test_a_range_ending_today_says_so(self):
        today = timezone.localdate()
        html = self.client_for(self.people['CEO']).get(URL, {
            'period': 'range', 'from': (today - timedelta(days=2)).isoformat(),
            'to': today.isoformat()}).content.decode()
        self.assertIn('(3 days, to today)', html)

    def test_the_header_names_the_period_and_who_generated_it(self):
        ceo = self.people['CEO']
        html = self.client_for(ceo).get(URL, DAY_PARAMS).content.decode()
        self.assertRegex(html, r'<b>25 Nov 2026</b> · Generated \d{1,2} [A-Z][a-z]{2} \d{4}, '
                         r'\d\d:\d\d by ' + re.escape(ceo.user.get_full_name()))
        self.assertIn('window.print()', html)
        self.assertIn('@media print', html)
        self.assertIn('size: A4', html)


class TileTests(PagesFixture):

    def test_an_as_of_now_tile_shows_no_comparison(self):
        site = self._site()
        self._task(site, self.se)
        self._log(site, self.pm, ist(DAY, 10))
        self._log(site, self.pm, ist(DAY - timedelta(days=1), 10))
        response = self.client_for(self.people['CEO']).get(URL, DAY_PARAMS)
        found = tiles(response.content.decode())
        value, text = found['Open tasks, no activity']
        self.assertEqual(value, 1)
        self.assertIn('as of now', text)
        self.assertNotIn('vs ', text)
        self.assertNotRegex(text, '[▲▼%]')
        # The others do compare, against B1's previous-period label.
        self.assertIn('vs 24 Nov 2026', found['Active users'][1])
        [as_of_now] = [t for t in response.context['tiles'] if t['as_of_now']]
        self.assertIsNone(as_of_now['compare'])
        # On paper the caption names the moment: "as of 28 Nov 2026, 13:44", shown only
        # by the print stylesheet.
        generated = timezone.localtime(response.context['generated_at'])
        self.assertIn('<span class="screen-only">as of now</span><span class="print-only">'
                      f'as of {generated.day} {generated:%b %Y, %H:%M}</span>',
                      response.content.decode())

    def test_the_date_inputs_start_on_28_jul_2026(self):
        client = self.client_for(self.people['CEO'])
        for url in ('/reports/', URL):
            html = client.get(url).content.decode()
            self.assertIn('name="date" value=', html)
            self.assertEqual(html.count('min="2026-07-28"'),
                             3 * html.count('class="pform no-print" x-data'), url)

    def test_a_day_before_28_jul_2026_is_refused_on_the_page(self):
        response = self.client_for(self.people['CEO']).get(
            URL, {'period': 'date', 'date': '2026-07-27'})
        self.assertIn('class="err" role="alert">Reports start on 28 Jul 2026. Showing today',
                      response.content.decode())
        self.assertEqual(response.context['period'].key, 'today')

    def test_comparison_wording(self):
        self.assertEqual(compare(5, 0, 'vs x')['text'], 'was 0')
        self.assertEqual(compare(0, 0, 'vs x')['text'], 'no change')
        self.assertEqual((compare(123, 100, 'vs x')['text'], compare(123, 100, 'vs x')['arrow']),
                         ('23%', '▲'))
        self.assertEqual(compare(50, 100, 'vs x')['arrow'], '▼')
        self.assertEqual(compare(3, 1, 'vs x', up_is_good=False)['tone'], 'bad')
        self.assertIsNone(compare(3, None, 'vs x'))


class ActiveUsersReportTests(PagesFixture):

    def _page(self, profile=None, params=DAY_PARAMS):
        response = self.client_for(profile or self.people['CEO']).get(URL, params)
        self.assertEqual(response.status_code, 200)
        return response, response.content.decode()

    def test_no_activity_is_zeros_and_no_crash(self):
        response, html = self._page()
        self.assertEqual({k: v[0] for k, v in tiles(html).items()},
                         {'Active users': 0, 'Signed in, no action': 0,
                          'Open tasks, no activity': 0, 'Actions': 0})
        self.assertIn('No activity in this period', html)
        self.assertNotIn('Bulk actions</h2>', html)

    def test_who_was_active_sorted_by_actions_with_what_they_did(self):
        site = self._site()
        for minute in range(3):
            self._log(site, self.designer, ist(DAY, 10, minute), code='design_head_qc_passed')
        self._log(site, self.designer, ist(DAY, 11), code='design_survey_link_added')
        self._log(site, self.pm, ist(DAY, 12))
        response, html = self._page()
        who = response.context['who']
        self.assertEqual([r['name'] for r in who],
                         [self.designer.user.get_full_name(), self.pm.user.get_full_name()])
        self.assertIn('Head QC passed 3 · Survey link added 1', html)

    def test_140_sites_stay_one_row_with_the_full_list_inside_details(self):
        sites = [self._site() for _ in range(140)]
        for i, site in enumerate(sites):
            # 5 s apart: single submissions, not one bulk action.
            self._log(site, self.pm, ist(DAY, 9) + timedelta(seconds=5 * i))
        response, html = self._page()
        [row] = response.context['who']
        self.assertEqual((row['sites']['count'], len(row['sites']['shown']), row['sites']['more']),
                         (140, 6, 134))
        cell = re.search(r'<td data-l="Site list">(.*?)</td>', html, re.S).group(1)
        summary = re.search(r'<summary>(.*?)</summary>', cell, re.S).group(1)
        self.assertEqual(summary.count('class="pill"'), 6)
        self.assertIn('+134 more', summary)
        self.assertEqual(cell.split('</summary>', 1)[1].count('class="pill"'), 140)

    def test_only_site_less_actions_show_a_dash_for_sites(self):
        self._log(None, self.designer, ist(DAY, 10), code='design_boq_submitted')
        self._log(None, self.designer, ist(DAY, 11), code='design_boq_submitted')
        response, html = self._page()
        row = re.search(r'<td data-l="Sites" class="num">(.*?)</td>\s*'
                        r'<td data-l="No-site actions" class="num">(.*?)</td>', html, re.S)
        self.assertEqual((row.group(1), row.group(2)), ('–', '2'))
        self.assertRegex(html, r'<td data-l="Site list">\s*–')

    def test_idle_holders_signed_in_and_bulk_tables(self):
        site = self._site()
        self._task(site, self.se)                          # idle, never acted
        login = ActivityLog.objects.create(project=None, actor=self.ceo, action='in',
                                           action_code='user_login')
        ActivityLog.objects.filter(pk=login.pk).update(timestamp=ist(DAY, 8, 30))
        self._batch(self.designer, ist(DAY, 15), 12, code='design_allocated', gap_s=0.2)
        response, html = self._page()
        self.assertRegex(html, r'<td data-l="Last action">never</td>')
        self.assertIn('25 Nov 2026, 08:30', html)
        self.assertIn('A bulk action is one submission across many sites. It counts as one '
                      'action.', html)
        self.assertIn('<td data-l="Action">Allocated</td>', html)
        self.assertIn('12 sites', html)

    def test_by_role_lists_idle_roles_first(self):
        self._log(self._site(), self.pm, ist(DAY, 10))
        response, _ = self._page()
        rows = response.context['by_role']
        pcts = [r['pct'] for r in rows]
        self.assertEqual(pcts, sorted(pcts))
        pm = next(r for r in rows if r['role'] == 'PM')
        accounts = UserProfile.objects.filter(role='PM', is_active=True,
                                              user__is_active=True).count()
        self.assertEqual((pm['users'], pm['accounts'], pm['pct']),
                         (1, accounts, round(100 / accounts)))
        self.assertTrue(any(r['users'] == 0 and r['pct'] == 0 for r in rows))

    def test_system_admin_sees_the_same_report_as_the_ceo(self):
        self._log(self._site(), self.pm, ist(DAY, 10))
        self._task(self._site(), self.se)
        ceo, _ = self._page(self.people['CEO'])
        sys_admin, _ = self._page(self.people['System Admin'])
        for key in ('tiles', 'by_role', 'who', 'idle', 'signed_in', 'bulk'):
            self.assertEqual(ceo.context[key], sys_admin.context[key], key)
        self.assertTemplateUsed(sys_admin, 'base.html')

    def test_the_page_matches_report_data_check(self):
        a, b = self._site(), self._site(status='Draft')
        self._log(a, self.pm, ist(DAY, 9))
        self._log(b, self.designer, ist(DAY, 10))
        self._st(a, self.pm, ist(DAY, 13))
        self._attend(a, self.se, DAY, ist(DAY, 11))
        self._task(a, self.se)
        self._task(b, self.ceo)
        login = ActivityLog.objects.create(project=None, actor=self.ceo, action='in',
                                           action_code='user_login')
        ActivityLog.objects.filter(pk=login.pk).update(timestamp=ist(DAY, 8))
        self._batch(self.designer, ist(DAY, 15), 11, gap_s=0.3)
        out = StringIO()
        call_command('report_data_check', date=DAY.isoformat(), stdout=out)
        text = out.getvalue()
        users, actions = map(int, re.search(r'ACTIVE USERS: (\d+) \((\d+) actions', text).groups())
        signed = int(re.search(r'SIGNED IN, NO ACTION: (\d+)', text).group(1))
        idle = int(re.search(r'OPEN TASKS, NO ACTIVITY \(tasks as of now\): (\d+)', text).group(1))
        bulk = int(re.search(r'BULK ACTIONS: (\d+)', text).group(1))
        _, html = self._page()
        found = {k: v[0] for k, v in tiles(html).items()}
        self.assertEqual(found, {'Active users': users, 'Signed in, no action': signed,
                                 'Open tasks, no activity': idle, 'Actions': actions})
        self.assertEqual((users, actions, signed, idle, bulk), (3, 5, 1, 1, 1))
        self.assertEqual(html.count('<td data-l="Action">'), bulk)


class QueryBudgetTests(PagesFixture):

    def _count(self, params):
        client = self.client_for(self.people['CEO'])
        client.get(URL, params)            # warm the session so both counts are alike
        with CaptureQueriesContext(connection) as ctx:
            response = client.get(URL, params)
        self.assertEqual(response.status_code, 200)
        return len(ctx.captured_queries)

    def _spread(self, n_sites, days):
        for i in range(n_sites):
            site = self._site()
            moment = ist(DAY - timedelta(days=i % days), 10)
            self._log(site, self.pm, moment)
            self._log(site, self.designer, moment + timedelta(minutes=2), code='design_qc_passed')
            self._st(site, self.pm, moment + timedelta(minutes=1))
            self._attend(site, self.se, moment.date(), moment)
            self._task(site, self.se)

    def test_the_same_for_3_sites_and_30_sites(self):
        self._spread(3, 1)
        small = self._count(DAY_PARAMS)
        self._spread(27, 1)
        self.assertEqual(self._count(DAY_PARAMS), small)

    def test_the_same_for_1_day_and_92_days(self):
        self._spread(30, 92)
        one = self._count(DAY_PARAMS)
        many = self._count({'period': 'range', 'to': DAY.isoformat(),
                            'from': (DAY - timedelta(days=91)).isoformat()})
        self.assertEqual(one, many)


class NavTests(PagesFixture):

    def _render(self, template, profile):
        request = RequestFactory(SERVER_NAME='localhost').get('/')
        request.user = profile.user
        return render_to_string(template, request=request)

    def test_the_reports_link_for_the_three_roles_and_not_a_pm(self):
        link = 'href="/reports/"'
        for role in REPORT_ROLES:
            self.assertIn(link, self._render('base.html', self.people[role]), role)
        self.assertNotIn(link, self._render('base.html', self.people['PM']))

    def test_the_system_admin_shell_carries_it(self):
        self.assertIn('href="/reports/"', self._render('projects/subadmin/subadmin_base.html',
                                                       self.people['System Admin']))

    def test_an_inactive_profile_is_not_shown_the_link(self):
        ceo = self.people['CEO']
        UserProfile.objects.filter(pk=ceo.pk).update(is_active=False)
        ceo.refresh_from_db()
        self.assertNotIn('href="/reports/"', self._render('base.html', ceo))

    def test_the_more_rows_ceo_group(self):
        def ceo_links(profile):
            groups = {g['label']: g['links'] for g in _more_links(profile.user, profile)}
            return [link['href'] for link in groups.get('CEO', [])]
        for role in REPORT_ROLES:
            self.assertIn('/reports/', ceo_links(self.people[role]), role)
        self.assertNotIn('/reports/', ceo_links(self.people['PM']))
