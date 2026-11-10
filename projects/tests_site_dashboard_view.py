"""
DASH-B2 — the Site Dashboard page (views_site_dashboard.site_dashboard), its permission
helper (permissions.user_can_view_site_dashboard) and its nav link.

WHAT THIS PINS
    Access        200 for PM, coordinator, CEO, Finance, SCM, Admin, System Admin and the
                  Design Head; 403 for Site Engineer, designer, BD and a profile-less
                  account; login redirect for anonymous (E3, ruling 5)
    Freshness     Cache-Control: no-cache, no-store (never_cache, E3)
    Money         the payment pill, row and tile for CEO; none of them for a PM or the
                  Design Head (D8)
    Drafts        always after every active site, in both sorts (E4)
    KPI filters   each one narrows the list, its tile's site count equals the list
                  length, and the active tile's link clears it (E5)
    Search        search, type and tender narrow the list; unknown parameters never 500
    Pipeline      six fixed segments on a RESCO card, own phases otherwise (ruling 2); the
                  RESCO highlight is the furthest segment started, none if none has
                  (10 Nov); Residential keeps utils.current_phase()
    Attendance    a RESCO card's line shows while Execution has started and is not done
                  (10 Nov); Residential keeps the current_phase() rule
    QA/QC strip   PM and coordinator viewers only (ruling 3)
    Queries       the same number for 3 sites and 30 sites
    Escaping      a site name holding <script> is printed as text

Today is frozen at 9 Nov 2026 by SchedFixture (through DashFixture).

Run with:
    python manage.py test projects.tests_site_dashboard_view --settings=solarpms.test_settings
"""
from datetime import date, datetime, timedelta
from decimal import Decimal

from django.contrib.auth.models import AnonymousUser, User
from django.db import connection
from django.test import Client, RequestFactory
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from . import scheduling
from .context_processors import site_dashboard_nav
from .models import (ContractorAttendance, Issue, PaymentRequest, Program, Project,
                     SiteQaqcAssignment, Task, UserProfile, Vendor, VendorOrder,
                     VendorOrderSite)
from .permissions import user_can_view_site_dashboard
from .tests_sched2_scheduler import CIVIL, FROZEN_TODAY
from .tests_site_dashboard import DashFixture, _fresh
from .views_site_dashboard import FILTERS, RESCO_SEGMENTS, relative_time

URL = '/dashboard/sites/'


def _client(profile):
    client = Client()
    client.force_login(profile.user)
    return client


class ViewFixture(DashFixture):

    def _get(self, profile, query=''):
        return _client(profile).get(URL + query)

    def _listed(self, response):
        """The active cards the page lists, by code, in page order."""
        return [v.card.code for v in response.context['cards']]

    def _tile(self, response, key):
        return next(t for t in response.context['kpis'] if t.get('key') == key)


# ---------------------------------------------------------------------------
# 1. Access (E3) and the permission helper
# ---------------------------------------------------------------------------

class AccessTests(ViewFixture):

    def _allowed(self):
        return (self.pm, self.coord, self.ceo, self.finance, self.scm, self.admin,
                self.sysadmin, self.head)

    def _refused(self):
        return (self.se, self.designer, self.bd)

    def test_helper_per_role(self):
        for profile in self._allowed():
            with self.subTest(role=profile.role, head=profile.is_design_head):
                self.assertTrue(user_can_view_site_dashboard(_fresh(profile.user)))
        for profile in self._refused():
            with self.subTest(role=profile.role):
                self.assertFalse(user_can_view_site_dashboard(_fresh(profile.user)))
        self.assertFalse(user_can_view_site_dashboard(AnonymousUser()))

    def test_helper_refuses_a_user_with_no_profile(self):
        user = User.objects.create_user(username='dash_noprofile', password='x')
        UserProfile.objects.filter(user=user).delete()
        self.assertFalse(user_can_view_site_dashboard(User.objects.get(pk=user.pk)))

    def test_page_opens_for_the_allowed_roles(self):
        for profile in self._allowed():
            with self.subTest(role=profile.role, head=profile.is_design_head):
                self.assertEqual(self._get(profile).status_code, 200)

    def test_page_is_403_for_everyone_else(self):
        for profile in self._refused():
            with self.subTest(role=profile.role):
                self.assertEqual(self._get(profile).status_code, 403)
        user = User.objects.create_user(username='dash_noprofile2', password='x')
        UserProfile.objects.filter(user=user).delete()
        client = Client()
        # A fresh instance: the one create_user returned still caches the deleted profile,
        # and the login signal would log against it.
        client.force_login(User.objects.get(pk=user.pk))
        self.assertEqual(client.get(URL).status_code, 403)

    def test_anonymous_is_sent_to_login(self):
        response = Client().get(URL)
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login/', response['Location'])

    def test_never_cache(self):
        cache_control = self._get(self.ceo)['Cache-Control']
        for part in ('no-cache', 'no-store', 'must-revalidate', 'max-age=0'):
            self.assertIn(part, cache_control)

    def test_url_name(self):
        self.assertEqual(reverse('site_dashboard'), URL)

    def test_nav_link_follows_the_helper(self):
        factory = RequestFactory()
        for profile in self._allowed() + self._refused():
            with self.subTest(role=profile.role, head=profile.is_design_head):
                request = factory.get('/')
                request.user = _fresh(profile.user)
                self.assertEqual(site_dashboard_nav(request)['can_view_site_dashboard'],
                                 profile not in self._refused())
        request = factory.get('/')
        request.user = AnonymousUser()
        self.assertEqual(site_dashboard_nav(request), {'can_view_site_dashboard': False})
        self.assertContains(self._get(self.pm), f'href="{URL}"')
        se_page = _client(self.se).get(reverse('dashboard_site_engineer'))
        self.assertEqual(se_page.status_code, 200)
        self.assertNotContains(se_page, f'href="{URL}"')


# ---------------------------------------------------------------------------
# 2. Money (D8)
# ---------------------------------------------------------------------------

class MoneyTests(ViewFixture):

    def setUp(self):
        self.site = self._bare(site_name='Money Site')
        vendor = Vendor.objects.create(name='Dash View Vendor')
        order = VendorOrder.objects.create(vendor=vendor, project_type='Residential',
                                           created_by=self.scm, total_amount=Decimal('500000'))
        VendorOrderSite.objects.create(order=order, project=self.site)
        PaymentRequest.objects.create(
            project=self.site, vendor=vendor, vendor_order=order, amount=Decimal('250000'),
            requested_by=self.scm.user, status=PaymentRequest.PENDING_APPROVAL)

    def test_ceo_sees_the_pill_the_row_and_the_tile(self):
        response = self._get(self.ceo)
        self.assertContains(response, '₹2.5 L pay')
        self.assertContains(response, 'Payments pending')
        self.assertContains(response, 'Payment requests open')
        self.assertContains(response, 'data-money="payment"')

    def test_pm_and_design_head_see_no_money_anywhere(self):
        for profile in (self.pm, self.head):
            with self.subTest(role=profile.role, head=profile.is_design_head):
                response = self._get(profile)
                self.assertIn('MONEY SITE', response.content.decode().upper())
                for text in ('₹', 'Payments pending', 'Payment requests open',
                             'data-money', '&amp; money'):
                    self.assertNotContains(response, text)


# ---------------------------------------------------------------------------
# 3. Sort and drafts (E4)
# ---------------------------------------------------------------------------

class DraftTests(ViewFixture):

    def setUp(self):
        self.active = self._bare(site_name='Active One')
        self._log(self.active, '', timezone.make_aware(datetime(2026, 11, 1, 10)), actor=self.pm)
        self.draft_old = self._bare(status='Draft', site_name='Draft Old')
        self.draft_new = self._bare(status='Draft', site_name='Draft New')
        Project.objects.filter(pk=self.draft_old.pk).update(
            created_at=timezone.make_aware(datetime(2026, 10, 1, 10)))
        # The newest activity of all is on a draft: it must still sort after the active site.
        self._log(self.draft_old, '', timezone.make_aware(datetime(2026, 11, 8, 10)), actor=self.pm)

    def test_drafts_after_active_sites_in_both_sorts_newest_first(self):
        for sort in ('latest', 'urgent'):
            with self.subTest(sort=sort):
                response = self._get(self.ceo, f'?sort={sort}')
                html = response.content.decode()
                self.assertEqual(self._listed(response), [self.active.project_id])
                self.assertEqual([v.card.id for v in response.context['drafts']],
                                 [self.draft_new.pk, self.draft_old.pk])
                self.assertLess(html.index(f'data-site="{self.active.project_id}"'),
                                html.index('id="drafts"'))
                self.assertContains(response, 'draft sites awaiting activation')

    def test_no_drafts_group_under_a_kpi_filter(self):
        response = self._get(self.ceo, '?filter=quiet')
        self.assertEqual(response.context['drafts'], [])
        self.assertNotContains(response, 'id="drafts"')


# ---------------------------------------------------------------------------
# 4. KPI filters (E5), search, type, tender, bad parameters
# ---------------------------------------------------------------------------

class FilterTests(ViewFixture):

    def setUp(self):
        old = timezone.now() - timedelta(days=30)
        # behind: scheduled, last block three days late
        self.behind = self._site(civil_due=date(2026, 11, 10))
        self._apply(self.behind)
        last = (Task.objects.filter(phase__project=self.behind, baseline_due__isnull=False)
                .order_by('-baseline_due').first())
        Task.objects.filter(pk=last.pk).update(due_date=last.baseline_due + timedelta(days=3))
        # civil: RESCO, no Civil date
        self.civil = self._site()
        # overdue / blocked / issues / quiet: Residential sites, one problem each
        self.overdue = self._bare(site_name='Overdue Site')
        self._plain_task(self._phase(self.overdue, 'Install', 1), 1, task_type=Task.INTERNAL,
                         due_date=FROZEN_TODAY - timedelta(days=2))
        self.blocked = self._bare(site_name='Blocked Site')
        self._plain_task(self._phase(self.blocked, 'Install', 1), 1, status=Task.BLOCKED)
        self.issue = self._bare(site_name='Issue Site')
        Issue.objects.create(project=self.issue, title='Leak', raised_by=self.pm)
        self.quiet = self._bare(site_name='Quiet Site', activated_at=old)
        self.clear = self._bare(site_name='Clear Site')
        for site in (self.overdue, self.blocked, self.issue, self.clear):
            self._log(site, '', timezone.make_aware(datetime(2026, 11, 8, 10)), actor=self.pm)

    def test_each_filter_narrows_the_list_and_its_tile_counts_it(self):
        everything = self._listed(self._get(self.ceo))
        expected = {'behind': self.behind, 'civil': self.civil, 'overdue': self.overdue,
                    'blocked': self.blocked, 'issues': self.issue, 'quiet': self.quiet}
        self.assertEqual(set(FILTERS), set(expected))
        for key, site in expected.items():
            with self.subTest(filter=key):
                response = self._get(self.ceo, f'?filter={key}')
                listed = self._listed(response)
                tile = self._tile(response, key)
                self.assertIn(site.project_id, listed)
                self.assertLess(len(listed), len(everything))
                self.assertEqual(tile['sites'], len(listed))
                self.assertTrue(all(FILTERS[key](v.card) for v in response.context['cards']))
                self.assertTrue(tile['active'])
                # The page's own count of cards agrees with the context.
                self.assertEqual(response.content.decode().count('class="row cols"'), len(listed))
                self.assertContains(response, f'data-filter="{key}" data-sites="{len(listed)}"')

    def test_a_second_click_clears_the_filter(self):
        response = self._get(self.ceo, '?filter=behind&sort=urgent')
        tile = self._tile(response, 'behind')
        self.assertNotIn('filter=', tile['href'])
        self.assertIn('sort=urgent', tile['href'])
        other = self._tile(response, 'quiet')
        self.assertIn('filter=quiet', other['href'])

    def test_tiles_without_a_filter_are_not_links(self):
        response = self._get(self.ceo)
        keys = [t.get('key') for t in response.context['kpis']]
        self.assertEqual(keys[:2], [None, None])        # sites in execution, DC kWp
        self.assertEqual(response.context['kpis'][0]['value'], len(self._listed(response)))

    def test_behind_tile_names_the_worst_slip_and_most_urgent_puts_it_first(self):
        response = self._get(self.ceo, '?sort=urgent')
        self.assertEqual(self._listed(response)[0], self.behind.project_id)
        self.assertIn('worst 3 days', self._tile(response, 'behind')['detail'])
        self.assertContains(response, '3 days behind plan')

    def test_search(self):
        response = self._get(self.ceo, '?q=quiet+site')
        self.assertEqual(self._listed(response), [self.quiet.project_id])
        response = self._get(self.ceo, f'?q={self.civil.project_id}')
        self.assertEqual(self._listed(response), [self.civil.project_id])
        self.assertContains(response, 'Needs Civil date')

    def test_type_and_tender(self):
        program = Program.objects.create(name='Dash Tender', program_type='OPEX', client_name='C',
                                         status='Active', short_tender_code='DASHT')
        Project.objects.filter(pk=self.civil.pk).update(program=program)
        response = self._get(self.ceo, f'?tender={program.pk}')
        self.assertEqual(self._listed(response), [self.civil.project_id])
        response = self._get(self.ceo, '?type=OPEX')
        self.assertEqual(set(self._listed(response)),
                         {self.behind.project_id, self.civil.project_id})

    def test_unknown_parameters_are_ignored(self):
        everything = self._listed(self._get(self.ceo))
        for query in ('?sort=bogus', '?filter=nope', '?type=Bogus', '?tender=abc',
                      '?tender=999999', '?q=' + 'x' * 500,
                      '?sort=&filter=&type=&tender=&q=', '?filter=behind&filter=nope'):
            with self.subTest(query=query):
                response = self._get(self.ceo, query)
                self.assertEqual(response.status_code, 200)
                if query.startswith('?q=x'):
                    self.assertEqual(response.context['params']['q'], 'x' * 100)
                elif not query.startswith('?filter=behind'):
                    self.assertEqual(self._listed(response), everything)
        self.assertEqual(self._get(self.ceo, '?sort=bogus').context['params']['sort'], 'latest')


# ---------------------------------------------------------------------------
# 5. The card (E6, E7, ruling 2)
# ---------------------------------------------------------------------------

class CardTests(ViewFixture):

    def test_resco_card_has_the_six_segments_residential_its_own_phases(self):
        resco = self._site()
        residential = self._bare()
        self._phase(residential, 'Survey', 1)
        self._phase(residential, 'Commissioning', 2)
        response = self._get(self.ceo)
        views = {v.card.id: v for v in response.context['cards']}
        self.assertEqual([s.label for s in views[resco.pk].segments],
                         [label for _, label in RESCO_SEGMENTS])
        self.assertEqual([s.label for s in views[residential.pk].segments],
                         ['Survey', 'Commissioning'])

    def _resco_segments(self, site):
        view = next(v for v in self._get(self.ceo).context['cards'] if v.card.id == site.pk)
        return view, {s.label: s for s in view.segments}

    def test_resco_highlight_is_the_furthest_started_segment(self):
        """IPGCLG shape: pre-installation approvals still open, Execution started. The
        label marks Execution, although utils.current_phase() still answers Pre-appr.; the
        open approval keeps its own colour."""
        site = self._site(civil_due=date(2026, 11, 10))
        self._done(self._task(site, CIVIL), self._ist(8))
        approval = Task.objects.filter(phase__project=site,
                                       phase__phase_name='Approvals (Pre-Installation)').first()
        Task.objects.filter(pk=approval.pk).update(status=Task.IN_PROGRESS)
        view, segments = self._resco_segments(site)
        self.assertEqual(view.card.current_phase, 'Approvals (Pre-Installation)')
        self.assertEqual([label for label, s in segments.items() if s.current], ['Execution'])
        self.assertEqual(segments['Pre-appr.'].state, 'active')
        self.assertFalse(segments['Pre-appr.'].current)

    def test_resco_highlight_is_none_when_nothing_has_started(self):
        site = self._site()
        view, segments = self._resco_segments(site)
        self.assertTrue(all(s.state in ('na', 'ns') for s in segments.values()))
        self.assertFalse(any(s.current for s in segments.values()))

    def test_residential_highlight_is_still_current_phase(self):
        residential = self._bare()
        survey = self._phase(residential, 'Survey', 1)
        self._plain_task(survey, 1, status=Task.DONE, completed_at=self._ist(5))
        install = self._phase(residential, 'Install', 2)
        self._plain_task(install, 1)
        view = next(v for v in self._get(self.ceo).context['cards'] if v.card.id == residential.pk)
        self.assertEqual(view.card.current_phase, 'Install')
        self.assertEqual([s.label for s in view.segments if s.current], ['Install'])

    # The attendance line (10 Nov): on a RESCO card it follows the Execution segment, the
    # same data the highlight reads; Residential and CAPEX keep utils.current_phase().

    def _ipgclg_shape(self):
        """Execution started (Civil done) with a pre-installation approval still open, so
        utils.current_phase() answers Pre-appr."""
        site = self._site(civil_due=date(2026, 11, 10))
        self._done(self._task(site, CIVIL), self._ist(8))
        approval = Task.objects.filter(phase__project=site,
                                       phase__phase_name='Approvals (Pre-Installation)').first()
        Task.objects.filter(pk=approval.pk).update(status=Task.IN_PROGRESS)
        return site

    def _log_attendance(self, site, headcount):
        ContractorAttendance.objects.create(project=site, date=FROZEN_TODAY, trade='CIVIL',
                                            headcount=headcount, time_in='09:00',
                                            recorded_by=self.pm,
                                            unlisted_contractor_name='Crew 1')

    def test_resco_attendance_line_follows_execution_not_current_phase(self):
        site = self._ipgclg_shape()
        view, _ = self._resco_segments(site)
        self.assertEqual(view.card.current_phase, 'Approvals (Pre-Installation)')
        self.assertEqual(view.attendance, 'none')
        self.assertContains(self._get(self.ceo), '<span class="att no">No attendance today</span>')
        self._log_attendance(site, 7)
        view, _ = self._resco_segments(site)
        self.assertEqual(view.attendance, 'count')
        self.assertContains(self._get(self.ceo), 'Attendance today: 7 people')

    def test_resco_attendance_line_hidden_when_execution_done(self):
        site = self._ipgclg_shape()
        block_ids = set(scheduling.ScheduleBlockTask.objects.filter(block__is_active=True)
                        .values_list('template_task_id', flat=True))
        execution = [t.pk for t in Task.objects.filter(phase__project=site, location_label='')
                     if scheduling._in_scope(t) and t.template_task_id in block_ids]
        Task.objects.filter(pk__in=execution).update(status=Task.DONE,
                                                     completed_at=self._ist(9, 9),
                                                     approved_at=self._ist(9, 9))
        self._log_attendance(site, 4)
        view, segments = self._resco_segments(site)
        self.assertEqual(segments['Execution'].state, 'done')
        self.assertIsNone(view.attendance)
        response = self._get(self.ceo)
        self.assertNotContains(response, 'No attendance today')
        self.assertNotContains(response, '<span class="att">Attendance today')

    def test_resco_attendance_line_hidden_when_execution_not_started(self):
        site = self._site()
        self._log_attendance(site, 3)
        view, segments = self._resco_segments(site)
        self.assertIsNone(view.card.execution_segment.actual_start)
        self.assertIsNone(view.attendance)

    def test_residential_attendance_line_unchanged(self):
        """Residential keeps the current_phase() rule: 'none' only in Installation or
        Testing & Commissioning; a logged count shows in any phase."""
        residential = self._bare()
        survey = self._phase(residential, 'Survey', 1)
        self._plain_task(survey, 1)
        install = self._phase(residential, 'Installation', 2)
        self._plain_task(install, 1)

        def attendance():
            return next(v for v in self._get(self.ceo).context['cards']
                        if v.card.id == residential.pk).attendance

        self.assertIsNone(attendance())                      # Survey: no line
        Task.objects.filter(phase=survey).update(status=Task.DONE, completed_at=self._ist(5))
        self.assertEqual(attendance(), 'none')               # Installation, none logged
        self._log_attendance(residential, 5)
        self.assertEqual(attendance(), 'count')
        Task.objects.filter(phase=survey).update(status=Task.NOT_STARTED, completed_at=None)
        self.assertEqual(attendance(), 'count')              # a logged count shows in any phase

    def test_capacity_shows_not_set_when_blank(self):
        self._bare(dc_capacity_kw=Decimal('62.50'))
        self._bare()
        response = self._get(self.ceo)
        self.assertContains(response, '62.5 kWp')
        self.assertContains(response, '<span class="na">not set</span>')

    def test_next_due_tags_external_tasks(self):
        site = self._bare()
        phase = self._phase(site, 'Install', 1)
        self._plain_task(phase, 1, task_name='Utility inspection', task_type=Task.EXTERNAL,
                         due_date=FROZEN_TODAY + timedelta(days=3))
        self.assertContains(self._get(self.ceo), '<span class="tag ext">External</span>')

    def test_open_site_link(self):
        site = self._bare()
        self.assertContains(self._get(self.ceo),
                            reverse('project_overview', args=[site.project_id]))

    def test_a_script_in_a_site_name_is_escaped(self):
        self._bare(site_name='<script>alert(1)</script>')
        response = self._get(self.ceo)
        self.assertNotContains(response, '<script>alert(1)</script>')
        self.assertContains(response, '&lt;script&gt;alert(1)&lt;/script&gt;')

    def test_relative_time(self):
        now = timezone.make_aware(datetime(2026, 11, 9, 12))
        for delta, text in ((timedelta(seconds=20), 'just now'), (timedelta(minutes=25), '25m ago'),
                            (timedelta(hours=3), '3h ago'), (timedelta(days=1, hours=2), 'yesterday'),
                            (timedelta(days=4), '4d ago'), (-timedelta(minutes=5), 'just now')):
            with self.subTest(delta=delta):
                self.assertEqual(relative_time(now - delta, now), text)


# ---------------------------------------------------------------------------
# 6. QA/QC strip (ruling 3)
# ---------------------------------------------------------------------------

class QaqcStripTests(ViewFixture):

    def setUp(self):
        self.theirs = self._bare(project_type='OPEX', pm=self.other_pm)
        SiteQaqcAssignment.objects.create(project=self.theirs, engineer=self.pm,
                                          assigned_by=self.other_pm)

    def test_pm_who_checks_a_site_gets_the_collapsed_group(self):
        response = self._get(self.pm)
        self.assertContains(response, 'id="qaqc-group"')
        self.assertContains(response, 'x-show="open" x-cloak')
        self.assertEqual([s['project'].pk for s in response.context['qaqc_sites']],
                         [self.theirs.pk])
        # The site itself is not one of the PM's cards (ruling 4, 9 Nov).
        self.assertNotIn(self.theirs.project_id, self._listed(response))

    def test_no_strip_for_portfolio_roles_or_a_pm_who_checks_nothing(self):
        for profile in (self.ceo, self.head, self.other_pm):
            with self.subTest(role=profile.role, head=profile.is_design_head):
                self.assertNotContains(self._get(profile), 'id="qaqc-group"')


# ---------------------------------------------------------------------------
# 7. Query budget
# ---------------------------------------------------------------------------

class QueryBudgetTests(ViewFixture):

    def _populate(self, n):
        t0 = timezone.make_aware(datetime(2026, 11, 2, 9))
        for _ in range(n):
            site = self._bare()
            phase = self._phase(site, 'Install', 1)
            self._plain_task(phase, 1, task_type=Task.INTERNAL, due_date=FROZEN_TODAY)
            self._plain_task(phase, 2, task_type=Task.EXTERNAL, status=Task.BLOCKED)
            Issue.objects.create(project=site, title='x', raised_by=self.pm)
            self._log(site, 'task_status_done', t0, actor=self.pm)

    def _count(self, client):
        with CaptureQueriesContext(connection) as captured:
            response = client.get(URL)
        self.assertEqual(response.status_code, 200)
        return len(captured.captured_queries), len(response.context['cards'])

    def test_the_same_number_of_queries_for_3_sites_and_30_sites(self):
        theirs = self._bare(project_type='OPEX', pm=self.other_pm, status='Draft')
        SiteQaqcAssignment.objects.create(project=theirs, engineer=self.pm,
                                          assigned_by=self.other_pm)
        self._site(civil_due=date(2026, 11, 10))      # one scheduled RESCO site in both runs
        self._populate(2)
        clients = {p.pk: _client(p) for p in (self.ceo, self.pm, self.head)}
        counts = {}
        for profile in (self.ceo, self.pm, self.head):
            clients[profile.pk].get(URL)               # warm the session
            counts[profile.pk] = self._count(clients[profile.pk])
            self.assertEqual(counts[profile.pk][1], 3)
        self._populate(27)
        for profile in (self.ceo, self.pm, self.head):
            with self.subTest(role=profile.role, head=profile.is_design_head):
                n, cards = self._count(clients[profile.pk])
                self.assertEqual(cards, 30)
                self.assertEqual(n, counts[profile.pk][0])
