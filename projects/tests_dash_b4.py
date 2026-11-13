"""
DASH-B4 — the Site Dashboard becomes the dashboard; the PM, SCM, Finance and CEO dashboards
and /landing/ redirect to it (Zuber's rulings, 10 Nov).

WHAT THIS PINS
    Landing      every R1 role (PM, coordinator, CEO, SCM, Admin, System Admin, Design
                 Head) lands on /dashboard/sites/ after login, from /login/ POST, an
                 authenticated GET of /login/ and `/`; Finance lands on /finance/work/
                 since FIN-M-B8b (tests_finance_landing pins it in full); Site Engineer,
                 designer and BD land where they did. Home (home_url) follows (R1, R5).
    Redirects    each old URL 302s an R1 role to its own landing (the Site Dashboard, or
                 Finance work for Finance since FIN-M-B8b); the CEO page's
                 ?context=residential / tenders still serve (ruling 5); Site Engineer,
                 designer and BD keep today's 403; the settings switch turns it off (R2, 3)
    Nav          no separate "Site Dashboard" entry; Home points at it (R5)
    More row     each role's groups, and every link opens for that role (no 403); the
                 Finance group leads with "Finance work" for its readers (FIN-M-B8b)
    Strips       approvals card (and approve from it end to end), PM re-verify group,
                 SCM tiles / stall pill / stage line / procurement / delivery issues,
                 Finance milestones tile — each for its roles and not for others
    Cards        Past target pill (today and forecast), QA/QC visit line, punch points,
                 draft rows link to the site, design-change link for PM / coordinator
    Queries      the same number for 3 and 30 sites, for every Site Dashboard role

Today is frozen at 9 Nov 2026 by SchedFixture (through DashFixture).

Run with:
    python manage.py test projects.tests_dash_b4 --settings=solarpms.test_settings
"""
import os
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from django.db import connection
from django.test import Client, RequestFactory, SimpleTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from .approvals import create_approval_request
from .context_processors import home_url
from .dashboard_strips import milestones_awaiting_count
from .decorators import SITE_DASHBOARD_URL, get_post_login_url, get_user_dashboard
from .models import (BOQ, ApprovalStep, DeliveryChallan, DesignAssignment, Issue,
                     PaymentMilestone, ProjectPhase, PunchPoint, QaqcVisit,
                     SiteQaqcAssignment, Task, APPROVAL_KIND_MATERIAL_PRE_ORDER,
                     APPROVAL_PARTY_PM, APPROVAL_STEP_APPROVED, APPROVAL_STEP_SUPERSEDED)
from .tests_approvals import MODULE_LINE
from .tests_sched2_scheduler import FROZEN_TODAY
from .tests_site_dashboard import DashFixture, _fresh

URL = '/dashboard/sites/'
OLD_URLS = ('/dashboard/pm/', '/dashboard/scm/', '/dashboard/finance/', '/dashboard/ceo/',
            '/landing/')


def _client(profile):
    client = Client()
    client.force_login(profile.user)
    return client


class B4Fixture(DashFixture):

    def r1(self):
        return (self.pm, self.coord, self.ceo, self.finance, self.scm, self.admin,
                self.sysadmin, self.head)

    def landing(self, profile):
        """Where an R1 role lands: the Site Dashboard, except Finance, who land on the
        Finance work page since FIN-M-B8b (pinned in full by tests_finance_landing)."""
        return '/finance/work/' if profile is self.finance else SITE_DASHBOARD_URL

    def others(self):
        """The roles that keep their own dashboards, with where that is."""
        return ((self.se, '/dashboard/site-engineer/'), (self.designer, '/dashboard/design/'),
                (self.bd, '/dashboard/bd/'))

    def _get(self, profile, url=URL):
        return _client(profile).get(url)

    def _groups(self, response):
        return {g['label']: [link['label'] for link in g['links']]
                for g in response.context['more_groups']}


# ---------------------------------------------------------------------------
# 1. Landing after login, Home (R1, R5)
# ---------------------------------------------------------------------------

class LandingTests(B4Fixture):

    def test_every_r1_role_lands_on_the_site_dashboard_after_login(self):
        for profile in self.r1():
            with self.subTest(role=profile.role, head=profile.is_design_head):
                # FIN-M-B8b: Finance lands on the Finance work page (tests_finance_landing).
                home = self.landing(profile)
                response = Client().post('/login/', {'username': profile.user.username,
                                                     'password': 'x'})
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response['Location'], home)
                self.assertEqual(get_post_login_url(_fresh(profile.user)), home)

    def test_other_roles_land_where_they_did(self):
        for profile, home in self.others():
            with self.subTest(role=profile.role):
                response = Client().post('/login/', {'username': profile.user.username,
                                                     'password': 'x'})
                self.assertEqual(response['Location'], home)

    def test_an_authenticated_login_page_and_the_root_follow_the_same_rule(self):
        for profile in (self.ceo, self.finance, self.scm, self.pm):
            with self.subTest(role=profile.role):
                home = self.landing(profile)        # FIN-M-B8b: Finance work for Finance
                client = _client(profile)
                self.assertEqual(client.get('/login/')['Location'], home)
                response = client.get('/', follow=True)
                self.assertEqual(response.redirect_chain[-1][0], home)
                self.assertEqual(response.status_code, 200)
        response = _client(self.se).get('/', follow=True)
        self.assertEqual(response.redirect_chain[-1][0], '/dashboard/site-engineer/')

    def test_home_link_points_at_the_site_dashboard_for_r1_only(self):
        factory = RequestFactory()
        for profile in self.r1():
            with self.subTest(role=profile.role, head=profile.is_design_head):
                request = factory.get('/')
                request.user = _fresh(profile.user)
                # FIN-M-B8b: Finance's Home is the Finance work page.
                self.assertEqual(home_url(request), {'home_url': self.landing(profile)})
        for profile, home in self.others():
            with self.subTest(role=profile.role):
                self.assertEqual(get_user_dashboard(_fresh(profile.user)), home)

    def test_nav_has_home_and_no_separate_site_dashboard_entry(self):
        page = self._get(self.pm)
        self.assertContains(page, f'<a href="{URL}" class="nav-link text-white small" title="Home">')
        self.assertNotContains(page, 'title="Site Dashboard"')
        self.assertNotContains(_client(self.se).get('/dashboard/site-engineer/'), f'href="{URL}"')


# ---------------------------------------------------------------------------
# 2. The old URLs (R2, ruling 3, 4, 5)
# ---------------------------------------------------------------------------

class RedirectTests(B4Fixture):

    def test_every_old_url_redirects_every_r1_role(self):
        for profile in self.r1():
            client = _client(profile)
            for url in OLD_URLS:
                with self.subTest(role=profile.role, head=profile.is_design_head, url=url):
                    response = client.get(url)
                    self.assertEqual(response.status_code, 302)
                    # FIN-M-B8b option A: each user's own landing, so Finance work for Finance.
                    self.assertEqual(response['Location'], self.landing(profile))

    def test_the_ceo_pages_kept_by_ruling_5_still_serve(self):
        for profile in (self.ceo, self.admin, self.sysadmin):
            client = _client(profile)
            for context in ('residential', 'tenders'):
                with self.subTest(role=profile.role, context=context):
                    self.assertEqual(client.get(f'/dashboard/ceo/?context={context}').status_code, 200)
            # An unknown context is the bare page, so it is retired too.
            self.assertEqual(client.get('/dashboard/ceo/?context=junk').status_code, 302)

    def test_other_roles_keep_todays_403(self):
        for profile, _ in self.others():
            client = _client(profile)
            for url in OLD_URLS + ('/dashboard/ceo/?context=tenders',):
                with self.subTest(role=profile.role, url=url):
                    self.assertEqual(client.get(url).status_code, 403)

    def test_anonymous_is_still_sent_to_login(self):
        for url in OLD_URLS:
            with self.subTest(url=url):
                response = Client().get(url)
                self.assertEqual(response.status_code, 302)
                self.assertIn('/login/', response['Location'])

    @override_settings(RETIRED_DASHBOARDS_REDIRECT=False)
    def test_the_switch_serves_the_old_pages_to_their_own_audience(self):
        for profile, url in ((self.pm, '/dashboard/pm/'), (self.coord, '/dashboard/pm/'),
                             (self.scm, '/dashboard/scm/'), (self.finance, '/dashboard/finance/'),
                             (self.ceo, '/dashboard/ceo/'), (self.ceo, '/landing/')):
            with self.subTest(role=profile.role, url=url):
                self.assertEqual(self._get(profile, url).status_code, 200)

    @override_settings(RETIRED_DASHBOARDS_REDIRECT=False)
    def test_the_switch_does_not_widen_access(self):
        self.assertEqual(self._get(self.admin, '/dashboard/pm/').status_code, 403)
        self.assertEqual(self._get(self.se, '/dashboard/scm/').status_code, 403)

    @override_settings(RETIRED_DASHBOARDS_REDIRECT=False)
    def test_the_switch_off_still_lands_r1_roles_on_the_site_dashboard(self):
        """Turning the redirect off restores the old pages only; login and Home are
        get_user_dashboard(), which the switch does not touch."""
        response = Client().post('/login/', {'username': self.pm.user.username,
                                             'password': 'x'})
        self.assertEqual(response['Location'], SITE_DASHBOARD_URL)


class SwitchFromEnvironmentTests(SimpleTestCase):
    """RETIRED_DASHBOARDS_REDIRECT comes from the environment (default True), so it can be
    turned off on Railway with no code change. Read in a subprocess under the REAL
    settings (solarpms.settings), as tests_migration_chain does: these tests run under
    test_settings, and the point is what production's settings module reads."""

    REPO_ROOT = Path(__file__).resolve().parent.parent

    def _read(self, value):
        env = dict(os.environ)
        env['DJANGO_SETTINGS_MODULE'] = 'solarpms.settings'
        env.pop('RETIRED_DASHBOARDS_REDIRECT', None)
        if value is not None:
            # python-decouple reads os.environ before .env, so this wins over a local .env.
            env['RETIRED_DASHBOARDS_REDIRECT'] = value
        result = subprocess.run(
            [sys.executable, '-c',
             'import django; django.setup(); from django.conf import settings; '
             'print(repr(settings.RETIRED_DASHBOARDS_REDIRECT))'],
            cwd=str(self.REPO_ROOT), env=env,
            capture_output=True, text=True, encoding='utf-8', errors='replace')
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip().splitlines()[-1]

    def test_default_is_on(self):
        self.assertEqual(self._read(None), 'True')

    def test_the_environment_turns_it_off_and_on(self):
        for raw, expected in (('False', 'False'), ('false', 'False'), ('0', 'False'),
                              ('True', 'True'), ('1', 'True')):
            with self.subTest(raw=raw):
                self.assertEqual(self._read(raw), expected)


# ---------------------------------------------------------------------------
# 3. The "More" row (rulings 16, 17)
# ---------------------------------------------------------------------------

class MoreRowTests(B4Fixture):

    TASKS = ['Due today', 'Due in 7 days', 'Overdue']

    def test_groups_per_role(self):
        expected = {
            self.pm:       {'Tasks': self.TASKS + ['New project']},
            self.coord:    {'Tasks': self.TASKS},
            self.head:     {'Tasks': self.TASKS + ['Design dashboard']},
            self.scm:      {'Tasks': self.TASKS,
                            'SCM': ['Contractors to map',
                                    'Vendor attendance · 0 sites · 0 people']},
            # FIN-M-B8b ruling (b): "Finance work" heads the Finance group for everyone the
            # Finance work page admits (Finance, CEO, Admin, System Admin).
            self.finance:  {'Tasks': self.TASKS,
                            'Finance': ['Finance work', 'Payments queue · to pay']},
            self.ceo:      {'Tasks': self.TASKS,
                            'Finance': ['Finance work', 'Payments queue · to pay'],
                            'CEO': ['Portfolio analytics (Residential)', 'Tenders overview',
                                    'Overdue tasks list']},
            self.admin:    {'Tasks': self.TASKS,
                            'SCM': ['Contractors to map',
                                    'Vendor attendance · 0 sites · 0 people'],
                            'Finance': ['Finance work', 'Payments queue · to pay'],
                            'Admin': ['Manage users', 'Activity log', 'Admin panel'],
                            'CEO': ['Portfolio analytics (Residential)', 'Tenders overview',
                                    'Overdue tasks list']},
            self.sysadmin: {'Tasks': self.TASKS,
                            'Finance': ['Finance work', 'Payments queue · to pay'],
                            'Admin': ['Sub-admin portal'],
                            'CEO': ['Portfolio analytics (Residential)', 'Tenders overview',
                                    'Overdue tasks list']},
        }
        for profile, groups in expected.items():
            with self.subTest(role=profile.role, head=profile.is_design_head):
                self.assertEqual(self._groups(self._get(profile)), groups)

    def test_no_link_leads_to_a_403(self):
        """Every link is gated by its page's own check, so each one opens for its viewer."""
        for profile in self.r1():
            client = _client(profile)
            page = client.get(URL)
            for group in page.context['more_groups']:
                for link in group['links']:
                    with self.subTest(role=profile.role, head=profile.is_design_head,
                                      link=link['label']):
                        self.assertEqual(client.get(link['href']).status_code, 200,
                                         link['href'])

    def test_counts_ride_on_the_links(self):
        page = self._get(self.scm)
        scm = next(g for g in page.context['more_groups'] if g['label'] == 'SCM')
        self.assertEqual(scm['links'][0]['count'], 0)
        self.assertContains(page, 'id="more"')


# ---------------------------------------------------------------------------
# 4. Role strips (rulings 10-14, 18)
# ---------------------------------------------------------------------------

class ApprovalsCardTests(B4Fixture):

    def _raise(self):
        return create_approval_request(
            kind=APPROVAL_KIND_MATERIAL_PRE_ORDER, raised_by=self.scm, title='B4 module make',
            description='Propose a make.', pm_assignee=self.pm, material={},
            lines=[dict(MODULE_LINE)])

    def test_pm_sees_nothing_waiting_and_portfolio_roles_see_no_card(self):
        self.assertContains(self._get(self.pm), 'Nothing waiting')
        for profile in (self.ceo, self.finance, self.scm, self.admin, self.coord):
            with self.subTest(role=profile.role):
                self.assertIsNone(self._get(profile).context['approvals_waiting'])

    def test_approve_from_the_card_end_to_end(self):
        approval = self._raise()
        client = _client(self.pm)
        page = client.get(URL)
        card = page.context['approvals_waiting']
        self.assertEqual(card['count'], 1)
        detail = reverse('approval_detail', args=[approval.pk])
        self.assertContains(page, f'href="{detail}"')
        self.assertEqual(client.get(detail).status_code, 200)
        step = ApprovalStep.objects.exclude(verdict=APPROVAL_STEP_SUPERSEDED).get(
            request=approval, party=APPROVAL_PARTY_PM)
        response = client.post(reverse('approval_decide', args=[step.pk]),
                               {'verdict': APPROVAL_STEP_APPROVED})
        self.assertEqual(response.status_code, 302)
        step.refresh_from_db()
        self.assertEqual(step.verdict, APPROVAL_STEP_APPROVED)
        self.assertEqual(client.get(URL).context['approvals_waiting']['count'], 0)


class PmReverifyTests(B4Fixture):

    def setUp(self):
        self.site = self._bare(project_type='OPEX')
        phase = self._phase(self.site, 'Install', 1)
        self.task = self._plain_task(phase, 1, task_type=Task.INTERNAL)
        self.point = PunchPoint.objects.create(task=self.task, reason='Loose clamp',
                                               raised_by=self.pm, status=PunchPoint.RECTIFIED)

    def test_pm_gets_the_group_with_a_reverify_link_that_opens(self):
        client = _client(self.pm)
        page = client.get(URL)
        self.assertContains(page, 'id="pm-reverify-group"')
        [point] = page.context['pm_reverify']
        self.assertTrue(point.pm_may_reverify)          # no engineer on the site, no round
        href = (reverse('task_detail', args=[self.site.project_id, self.task.pk])
                + f'#punch-point-{self.point.pk}')
        self.assertContains(page, f'href="{href}"')
        self.assertContains(page, '>Re-verify</a>')
        task_page = client.get(href.split('#')[0])
        self.assertContains(task_page, f'id="punch-point-{self.point.pk}"')

    def test_an_engineer_on_the_site_turns_the_button_into_open(self):
        SiteQaqcAssignment.objects.create(project=self.site, engineer=self.other_pm,
                                          assigned_by=self.pm)
        [point] = self._get(self.pm).context['pm_reverify']
        self.assertFalse(point.pm_may_reverify)

    def test_no_group_for_other_roles(self):
        for profile in (self.ceo, self.scm, self.finance, self.head, self.other_pm):
            with self.subTest(role=profile.role):
                self.assertNotContains(self._get(profile), 'id="pm-reverify-group"')


class ScmStripTests(B4Fixture):

    def setUp(self):
        self.site = self._bare()
        today = FROZEN_TODAY
        BOQ.objects.create(project=self.site, status='Submitted',
                           submitted_at=timezone.make_aware(datetime(2026, 11, 3, 10)))
        self.dc = DeliveryChallan.objects.create(
            project=self.site, dc_number='B4-DC1', dc_date=today - timedelta(days=2),
            expected_delivery_date=today, status=DeliveryChallan.EXPECTED)
        DeliveryChallan.objects.create(
            project=self.site, dc_number='B4-DC2', dc_date=today - timedelta(days=4),
            expected_delivery_date=today - timedelta(days=1), status=DeliveryChallan.EXPECTED)
        self.issue = Issue.objects.create(project=self.site, delivery_challan=self.dc,
                                          title='Short supply', raised_by=self.pm)

    def _tile(self, response, label):
        return next(t for t in response.context['kpis'] if t['label'] == label)

    def test_scm_tiles(self):
        page = self._get(self.scm)
        self.assertEqual(self._tile(page, 'BOQ awaiting ack.')['value'], 1)
        self.assertEqual(self._tile(page, 'Deliveries today')['value'], 1)
        self.assertEqual(self._tile(page, 'Overdue DCs')['value'], 1)

    def test_stalled_pill_and_stage_line(self):
        page = self._get(self.scm)
        view = next(v for v in page.context['cards'] if v.card.id == self.site.pk)
        # BOQ submitted 6 days ago (> 5, red) is the worst stalled stage.
        self.assertEqual(view.scm['pill'], ('Stalled · BOQ 6d', 'crit'))
        self.assertIn('BOQ: Submitted 6d', view.scm['stages'])
        self.assertIn('Delivery: Overdue 1d', view.scm['stages'])
        self.assertIn('Issues: 1 open', view.scm['stages'])
        self.assertContains(page, 'data-stalled')
        self.assertContains(page, 'data-stage-line')
        self.assertFalse(view.clear)

    def test_procurement_and_delivery_issues_groups(self):
        page = self._get(self.scm)
        self.assertContains(page, 'id="scm-issues-group"')
        self.assertEqual([i.pk for i in page.context['scm_delivery_issues']], [self.issue.pk])
        self.assertContains(page, f'href="{reverse("issue_detail", args=[self.issue.pk])}"')
        self.assertIn('opex_tender_rows', page.context)
        self.assertIn('can_raise_group_order', page.context)

    def test_none_of_it_for_other_roles(self):
        for profile in (self.ceo, self.admin, self.pm, self.finance):
            with self.subTest(role=profile.role):
                page = self._get(profile)
                labels = [t['label'] for t in page.context['kpis']]
                self.assertNotIn('BOQ awaiting ack.', labels)
                self.assertNotContains(page, 'id="scm-issues-group"')
                self.assertNotContains(page, 'data-stalled')
                self.assertNotContains(page, 'data-stage-line')
                self.assertNotIn('scm_delivery_issues', page.context)


class FinanceTileTests(B4Fixture):

    def test_milestones_awaiting_for_finance_only(self):
        site = self._bare()
        PaymentMilestone.objects.create(project=site, milestone_name='M1',
                                        milestone_description='Advance', created_by=self.pm)
        page = self._get(self.finance)
        tile = next(t for t in page.context['kpis'] if t['label'] == 'Milestones awaiting')
        self.assertEqual(tile['value'], 1)
        for profile in (self.ceo, self.scm, self.pm, self.admin):
            with self.subTest(role=profile.role):
                labels = [t['label'] for t in self._get(profile).context['kpis']]
                self.assertNotIn('Milestones awaiting', labels)

    # FIN-M-B1 (rulings R2, R3): the tile counts Pending milestones on live, non-deleted
    # Residential projects only. Each test below seeds one case and calls the helper.

    def _milestone(self, site, status=PaymentMilestone.PENDING, name='M1'):
        return PaymentMilestone.objects.create(project=site, milestone_name=name,
                                               milestone_description='Advance',
                                               status=status, created_by=self.pm)

    def test_pending_on_a_live_residential_project_is_counted(self):
        self._milestone(self._bare())
        self.assertEqual(milestones_awaiting_count(), 1)

    def test_pending_on_a_soft_deleted_residential_project_is_not_counted(self):
        # project_delete sets is_deleted and leaves status Active, so status alone keeps it.
        self._milestone(self._bare(is_deleted=True, deleted_at=timezone.now()))
        self.assertEqual(milestones_awaiting_count(), 0)

    def test_pending_on_live_opex_and_capex_projects_is_not_counted(self):
        self._milestone(self._bare(project_type='OPEX'))
        self._milestone(self._bare(project_type='CAPEX'))
        self.assertEqual(milestones_awaiting_count(), 0)

    def test_invoiced_and_received_on_a_live_residential_project_are_not_counted(self):
        site = self._bare()
        self._milestone(site, status=PaymentMilestone.INVOICED, name='M1')
        self._milestone(site, status=PaymentMilestone.RECEIVED, name='M2')
        self.assertEqual(milestones_awaiting_count(), 0)

    def test_pending_on_a_draft_residential_project_is_not_counted(self):
        self._milestone(self._bare(status='Draft'))
        self.assertEqual(milestones_awaiting_count(), 0)

    def test_the_count_is_one_query(self):
        self._milestone(self._bare())
        self._milestone(self._bare(project_type='OPEX'))
        self._milestone(self._bare(is_deleted=True, deleted_at=timezone.now()))
        # Setup above is outside the block, so only the helper's own queries are counted;
        # the value is checked after the block so a wrong count cannot mask the query count.
        with self.assertNumQueries(1):
            count = milestones_awaiting_count()
        self.assertEqual(count, 1)

    def test_tile_second_line_says_residential_pending(self):
        page = self._get(self.finance)
        tile = next(t for t in page.context['kpis'] if t['label'] == 'Milestones awaiting')
        self.assertEqual(tile['detail'], 'Residential, pending')
        self.assertContains(page, '<span class="d">Residential, pending</span>', html=False)


# ---------------------------------------------------------------------------
# 5. Card additions (rulings 6, 8, 9, 18)
# ---------------------------------------------------------------------------

class CardAdditionTests(B4Fixture):

    def _view_of(self, profile, site, drafts=False):
        page = self._get(profile)
        key = 'drafts' if drafts else 'cards'
        return page, next(v for v in page.context[key] if v.card.id == site.pk)

    def test_past_target_on_any_type_when_today_is_past_it(self):
        late = self._bare(target_commissioning_date=FROZEN_TODAY - timedelta(days=4))
        ok = self._bare(target_commissioning_date=FROZEN_TODAY + timedelta(days=4))
        done = self._bare(status='Commissioned',
                          target_commissioning_date=FROZEN_TODAY - timedelta(days=4))
        page, view = self._view_of(self.pm, late)
        self.assertEqual(view.past_target[0], 'Past target +4d')
        self.assertContains(page, 'data-past-target')
        self.assertFalse(view.clear)
        self.assertIsNone(self._view_of(self.pm, ok)[1].past_target)
        self.assertIsNone(self._view_of(self.pm, done)[1].past_target)

    def test_past_target_when_the_forecast_finish_is_past_it(self):
        site = self._site(civil_due=date(2026, 11, 10))
        self._apply(site)
        card = self._card(self.pm, site)
        forecast = card.schedule.forecast_finish
        self.assertIsNotNone(forecast)
        target = forecast - timedelta(days=3)
        self.assertGreater(target, FROZEN_TODAY)        # only the forecast is late
        site.target_commissioning_date = target
        site.save(update_fields=['target_commissioning_date'])
        _, view = self._view_of(self.pm, site)
        self.assertEqual(view.past_target[0], 'Past target +3d')
        self.assertIn('Forecast finish', view.past_target[1])

    def test_qaqc_visit_line_and_punch_points(self):
        site = self._bare(project_type='OPEX')
        task = self._plain_task(self._phase(site, 'Install', 1), 1)
        PunchPoint.objects.create(task=task, reason='Gap', raised_by=self.pm)
        assignment = SiteQaqcAssignment.objects.create(project=site, engineer=self.other_pm,
                                                       assigned_by=self.pm)
        page, view = self._view_of(self.ceo, site)
        self.assertEqual(view.qaqc_line, 'No QA/QC visit yet')
        self.assertEqual(view.punch_points, 1)
        self.assertContains(page, '1 punch point<')
        QaqcVisit.objects.create(project=site, engineer=self.other_pm, assignment=assignment,
                                 visit_date=FROZEN_TODAY - timedelta(days=2),
                                 status=QaqcVisit.SUBMITTED, submitted_at=timezone.now())
        self.assertEqual(self._view_of(self.ceo, site)[1].qaqc_line,
                         'Last QA/QC visit: 2 days ago')

    def test_draft_rows_link_to_the_site_for_every_viewer(self):
        draft = self._bare(status='Draft')
        for profile in (self.pm, self.ceo):
            with self.subTest(role=profile.role):
                self.assertContains(self._get(profile),
                                    f'<a class="code" href="{reverse("project_overview", args=[draft.project_id])}">')

    def test_design_change_link_for_pm_and_coordinator_where_design_started(self):
        started = self._bare(project_type='OPEX', status='Draft')
        started.coordinators.add(self.coord)
        DesignAssignment.objects.create(project=started)
        not_started = self._bare(project_type='OPEX', status='Draft')
        live = self._bare(project_type='OPEX')
        DesignAssignment.objects.create(project=live)
        residential = self._bare()
        form = lambda site: reverse('design_change_request_form', kwargs={'project_id': site.project_id})
        pm_page = self._get(self.pm)
        self.assertContains(pm_page, f'href="{form(started)}"')
        self.assertContains(pm_page, f'href="{form(live)}"')
        self.assertNotContains(pm_page, f'href="{form(not_started)}"')
        self.assertNotContains(pm_page, f'href="{form(residential)}"')
        self.assertContains(self._get(self.coord), f'href="{form(started)}"')
        # The link opens the form (which explains a closed window itself).
        self.assertEqual(_client(self.pm).get(form(started)).status_code, 200)
        for profile in (self.ceo, self.scm, self.head):
            with self.subTest(role=profile.role):
                self.assertNotContains(self._get(profile), 'data-design-change')


# ---------------------------------------------------------------------------
# 6. Query budget: 3 sites against 30, every Site Dashboard role
# ---------------------------------------------------------------------------

class QueryBudgetTests(B4Fixture):

    def _populate(self, n):
        t0 = timezone.make_aware(datetime(2026, 11, 2, 9))
        for i in range(n):
            site = self._bare(target_commissioning_date=FROZEN_TODAY - timedelta(days=1))
            site.coordinators.add(self.coord)            # so the coordinator sees 30 too
            phase = self._phase(site, 'Install', 1)
            task = self._plain_task(phase, 1, task_type=Task.INTERNAL, due_date=FROZEN_TODAY)
            self._plain_task(phase, 2, task_type=Task.EXTERNAL, status=Task.BLOCKED)
            dc = DeliveryChallan.objects.create(
                project=site, dc_number=f'{site.project_id}-DC', dc_date=FROZEN_TODAY,
                expected_delivery_date=FROZEN_TODAY, status=DeliveryChallan.EXPECTED)
            Issue.objects.create(project=site, delivery_challan=dc, title='x', raised_by=self.pm)
            BOQ.objects.create(project=site, status='Submitted',
                               submitted_at=timezone.make_aware(datetime(2026, 11, 1, 9)))
            PunchPoint.objects.create(task=task, reason='r', raised_by=self.pm,
                                      status=PunchPoint.RECTIFIED)
            PaymentMilestone.objects.create(project=site, milestone_name='M1',
                                            milestone_description='m', created_by=self.pm)
            self._log(site, 'task_status_done', t0, actor=self.pm)

    def _count(self, client):
        with CaptureQueriesContext(connection) as captured:
            response = client.get(URL)
        self.assertEqual(response.status_code, 200)
        return len(captured.captured_queries), len(response.context['cards'])

    def test_the_same_number_of_queries_for_3_and_30_sites_per_role(self):
        opex = self._bare(project_type='OPEX', status='Draft')
        DesignAssignment.objects.create(project=opex)
        self._site(civil_due=date(2026, 11, 10))       # one scheduled RESCO site in both runs
        self._populate(2)
        roles = self.r1()
        clients = {p.pk: _client(p) for p in roles}
        before = {}
        for profile in roles:
            clients[profile.pk].get(URL)               # warm the session
            before[profile.pk] = self._count(clients[profile.pk])
        self._populate(27)
        for profile in roles:
            with self.subTest(role=profile.role, head=profile.is_design_head):
                n, cards = self._count(clients[profile.pk])
                # The coordinator coordinates only the populated sites, not the two RESCO
                # ones (assigned PM only), so 29 against the others' 30 + 1 scheduled.
                self.assertEqual(before[profile.pk][1] + 27, cards)
                self.assertEqual(n, before[profile.pk][0])
