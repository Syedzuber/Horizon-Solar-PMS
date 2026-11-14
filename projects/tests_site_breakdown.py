"""
TB-1 — the activated-site breakdown (site_breakdown.py) and its strip on the tender page.

WHAT THIS PINS (each test fails if its rule is wrong)
    In execution   = the Site Dashboard's "Sites in execution" tile for the same tender;
                   test, deleted, cancelled and draft sites never counted anywhere
    Mobilized      a live challan AND design started (tender_stages S2-S5); either alone,
                   a Rejected-only challan, or no DesignAssignment does not count
    Active         the Civil ANCHOR task In Progress or Done; Not Started with a date,
                   Blocked, N/A, or only a location copy started does not count
    Active today   a human activity row in the 24 h up to `now`; 25 h ago, attendance only,
                   scheduler / *_opened rows and rows after `now` do not count
    Extraction     sites_visible_on_dashboard() returns exactly what it did before the D9
                   rule moved into dashboard_live_sites()
    Query budget   1 query for 1 site and for 50
    Page           the strip renders on program_detail for the tender's PM and for CEO,
                   whole tender for both; "Active sites (live)" gone; status badge reads
                   "Activated"

Run with:
    python manage.py test projects.tests_site_breakdown --settings=solarpms.test_settings
"""
from datetime import date, datetime, timedelta

from django.contrib.auth.models import User
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from .activity_codes import MONEY_ACTIVITY_CODES
from .models import (DESIGN_ASSIGNMENT_STATUS_CHOICES, DESIGN_ALLOCATED,
                     DESIGN_AWAITING_ALLOCATION, DESIGN_AWAITING_SURVEY, DESIGN_IN_DESIGN,
                     DESIGN_RELEASED, DESIGN_SURVEY_RETURNED, ContractorAttendance,
                     DeliveryChallan, DesignAssignment, Program, Project, Task)
from .permissions import dashboard_live_sites, sites_visible_on_dashboard
from .site_breakdown import DESIGN_STARTED_STATUSES, site_breakdown
from .site_dashboard import build_site_cards
from .tests_sched2_scheduler import CIVIL
from .tests_site_dashboard import DashFixture

#: 8 PM IST on the fixture's frozen day (SchedFixture freezes localdate at 9 Nov 2026):
#: the moment the daily snapshot is sent.
NOW = timezone.make_aware(datetime(2026, 11, 9, 20, 0))


def _fresh(user):
    return User.objects.get(pk=user.pk)


class BreakdownFixture(DashFixture):
    """DashFixture's roles, seeded OPEX template and blocks, plus one tender."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.program = Program.objects.create(name='TB Tender', program_type='OPEX',
                                             client_name='C', status='Active',
                                             short_tender_code='TBONE')

    def _resco(self, **fields):
        """A bare RESCO site (no tasks) in the tender."""
        fields.setdefault('program', self.program)
        return self._bare(project_type='OPEX', **fields)

    def _activated(self, **fields):
        """A RESCO site activated through the real view (so it has the Civil anchor in an
        active block), then put in the tender."""
        site = self._site(**fields)
        Project.objects.filter(pk=site.pk).update(program=self.program)
        return site

    def _dc(self, site, status=DeliveryChallan.EXPECTED):
        return DeliveryChallan.objects.create(project=site, dc_number=f'DC-{site.pk}-{status}',
                                              dc_date=date(2026, 11, 1), status=status,
                                              created_by=self.scm)

    def _design(self, site, status):
        return DesignAssignment.objects.update_or_create(project=site,
                                                         defaults={'status': status})[0]

    def _civil(self, site, **fields):
        Task.objects.filter(pk=self._task(site, CIVIL).pk).update(**fields)

    def _mobilized(self, site):
        """Give `site` everything Mobilized needs."""
        self._dc(site)
        self._design(site, DESIGN_IN_DESIGN)
        return site

    def _b(self, *sites, now=NOW):
        return site_breakdown(Project.objects.filter(pk__in=[s.pk for s in sites]), now)


# ---------------------------------------------------------------------------
# 1. In execution (B1)
# ---------------------------------------------------------------------------

class InExecutionTests(BreakdownFixture):

    def setUp(self):
        self.live = self._mobilized(self._resco())
        self.on_hold = self._resco(status='On Hold')
        self.theirs = self._resco(pm=self.other_pm)
        # Every condition met on each of these, so only the exclusion keeps them out.
        self.hidden = [self._resco(is_test=True), self._resco(status='Cancelled'),
                       self._resco(status='Draft'), self._resco(is_deleted=True)]
        for site in self.hidden:
            self._mobilized(site)
            self._log(site, '', NOW - timedelta(hours=1))
        self.elsewhere = self._resco(program=None)

    def test_equals_the_site_dashboard_tile_for_the_same_tender(self):
        b = site_breakdown(self.program.sites.all(), timezone.now())
        client = Client()
        client.force_login(_fresh(self.ceo.user))
        response = client.get(reverse('site_dashboard') + f'?tender={self.program.pk}')
        tile = response.context['kpis'][0]
        self.assertEqual(tile['label'], 'Sites in execution')
        self.assertEqual(b.in_execution, tile['value'])
        self.assertEqual(set(b.in_execution_ids),
                         {v.card.id for v in response.context['cards']})
        self.assertEqual(set(b.in_execution_ids), {self.live.pk, self.on_hold.pk, self.theirs.pk})

    def test_equals_the_cards_build_site_cards_counts_as_live(self):
        cards = [c for c in build_site_cards(_fresh(self.ceo.user))
                 if c.program_id == self.program.pk and not c.is_draft]
        self.assertEqual(set(site_breakdown(self.program.sites.all(), NOW).in_execution_ids),
                         {c.id for c in cards})

    def test_test_cancelled_draft_and_deleted_sites_are_excluded_everywhere(self):
        b = site_breakdown(self.program.sites.all(), NOW)
        for site in self.hidden:
            for ids in (b.in_execution_ids, b.mobilized_ids, b.active_ids, b.active_today_ids):
                self.assertNotIn(site.pk, ids)
        self.assertEqual(b.mobilized_ids, (self.live.pk,))

    def test_only_the_given_sites_are_counted(self):
        self.assertNotIn(self.elsewhere.pk, site_breakdown(self.program.sites.all(), NOW).in_execution_ids)
        self.assertEqual(self._b(self.elsewhere).in_execution_ids, (self.elsewhere.pk,))
        self.assertEqual(site_breakdown([], NOW).in_execution, 0)


# ---------------------------------------------------------------------------
# 2. Extraction: sites_visible_on_dashboard unchanged (P2 ruling)
# ---------------------------------------------------------------------------

class DashboardScopeUnchangedTests(BreakdownFixture):

    def setUp(self):
        self.mine = self._bare()
        self.coordinated = self._bare(pm=self.other_pm)
        self.coordinated.coordinators.add(self.coord)
        self.theirs = self._bare(pm=self.other_pm)
        self.draft = self._bare(status='Draft')
        self.on_hold = self._bare(status='On Hold')
        self._bare(is_test=True)
        self._bare(is_deleted=True)
        self._bare(status='Cancelled')

    def _visible(self, profile):
        return set(sites_visible_on_dashboard(_fresh(profile.user)).values_list('pk', flat=True))

    def test_each_role_sees_exactly_the_sites_it_saw_before(self):
        every = {self.mine.pk, self.coordinated.pk, self.theirs.pk, self.draft.pk,
                 self.on_hold.pk}
        expected = {
            self.ceo: every,
            self.admin: every,
            self.pm: {self.mine.pk, self.draft.pk, self.on_hold.pk},
            self.coord: {self.coordinated.pk},
        }
        for profile, sites in expected.items():
            with self.subTest(role=profile.role):
                self.assertEqual(self._visible(profile), sites)

    def test_the_portfolio_scope_is_dashboard_live_sites(self):
        self.assertEqual(self._visible(self.ceo),
                         set(dashboard_live_sites().values_list('pk', flat=True)))


# ---------------------------------------------------------------------------
# 3. Mobilized (B2, P3)
# ---------------------------------------------------------------------------

class MobilizedTests(BreakdownFixture):

    def test_dc_and_design_started_counts(self):
        site = self._mobilized(self._resco())
        self.assertEqual(self._b(site).mobilized_ids, (site.pk,))

    def test_every_challan_state_but_rejected_counts(self):
        for status in (DeliveryChallan.EXPECTED, DeliveryChallan.PARTIALLY_RECEIVED,
                       DeliveryChallan.RECEIVED):
            with self.subTest(status=status):
                site = self._resco()
                self._dc(site, status)
                self._design(site, DESIGN_RELEASED)
                self.assertEqual(self._b(site).mobilized, 1)

    def test_dc_without_design_started_does_not_count(self):
        for status in (DESIGN_AWAITING_SURVEY, DESIGN_AWAITING_ALLOCATION, DESIGN_SURVEY_RETURNED):
            with self.subTest(status=status):
                site = self._resco()
                self._dc(site)
                self._design(site, status)
                self.assertEqual(self._b(site).mobilized, 0)

    def test_dc_with_no_design_assignment_does_not_count(self):
        site = self._resco()
        self._dc(site)
        self.assertEqual(self._b(site).mobilized, 0)

    def test_design_without_dc_does_not_count(self):
        site = self._resco()
        self._design(site, DESIGN_RELEASED)
        self.assertEqual(self._b(site).mobilized, 0)

    def test_a_rejected_only_dc_does_not_count(self):
        site = self._resco()
        self._dc(site, DeliveryChallan.REJECTED)
        self._design(site, DESIGN_IN_DESIGN)
        self.assertEqual(self._b(site).mobilized, 0)
        self._dc(site, DeliveryChallan.RECEIVED)
        self.assertEqual(self._b(site).mobilized, 1)

    def test_design_started_is_the_ceo_pipeline_s2_to_s5(self):
        # Ruling P3: allocated onwards counts; awaiting survey, awaiting allocation and
        # Design Hold do not. Every status is on one side or the other.
        not_started = {DESIGN_AWAITING_SURVEY, DESIGN_AWAITING_ALLOCATION, DESIGN_SURVEY_RETURNED}
        every = {value for value, _ in DESIGN_ASSIGNMENT_STATUS_CHOICES}
        self.assertEqual(DESIGN_STARTED_STATUSES, every - not_started)
        self.assertIn(DESIGN_ALLOCATED, DESIGN_STARTED_STATUSES)


# ---------------------------------------------------------------------------
# 4. Active (B3, P4)
# ---------------------------------------------------------------------------

class ActiveTests(BreakdownFixture):

    def test_civil_in_progress_and_done_count(self):
        for status in (Task.IN_PROGRESS, Task.DONE):
            with self.subTest(status=status):
                site = self._activated()
                self._civil(site, status=status)
                self.assertEqual(self._b(site).active_ids, (site.pk,))

    def test_not_started_with_a_due_date_does_not_count(self):
        site = self._activated(civil_due=date(2026, 11, 10))
        self.assertIsNotNone(self._task(site, CIVIL).due_date)
        self.assertEqual(self._b(site).active, 0)

    def test_blocked_does_not_count(self):
        site = self._activated()
        self._civil(site, status=Task.BLOCKED)
        self.assertEqual(self._b(site).active, 0)

    def test_a_not_applicable_civil_task_does_not_count(self):
        site = self._activated()
        self._civil(site, status=Task.IN_PROGRESS, is_not_applicable=True)
        self.assertEqual(self._b(site).active, 0)

    def test_only_the_anchor_counts_not_a_location_copy(self):
        site = self._activated()
        anchor = self._task(site, CIVIL)
        Task.objects.create(phase=anchor.phase, task_order=anchor.task_order + 50,
                            task_name=anchor.task_name, template_task=anchor.template_task,
                            location_label='Block B', status=Task.IN_PROGRESS)
        self.assertEqual(self._b(site).active, 0)
        self._civil(site, status=Task.IN_PROGRESS)
        self.assertEqual(self._b(site).active, 1)

    def test_other_execution_work_without_civil_does_not_count(self):
        site = self._activated()
        Task.objects.filter(phase__project=site).exclude(template_task__code=CIVIL).update(
            status=Task.IN_PROGRESS)
        self.assertEqual(self._b(site).active, 0)

    def test_a_site_with_no_civil_task_is_not_active(self):
        self.assertEqual(self._b(self._resco()).active, 0)


# ---------------------------------------------------------------------------
# 5. Active today (B4)
# ---------------------------------------------------------------------------

class ActiveTodayTests(BreakdownFixture):

    def setUp(self):
        self.site = self._resco()

    def _today(self, now=NOW):
        return self._b(self.site, now=now).active_today

    def test_an_update_23_hours_ago_counts(self):
        self._log(self.site, 'task_status_changed', NOW - timedelta(hours=23))
        self.assertEqual(self._today(), 1)

    def test_an_update_25_hours_ago_does_not(self):
        self._log(self.site, 'task_status_changed', NOW - timedelta(hours=25))
        self.assertEqual(self._today(), 0)

    def test_an_update_at_9_pm_the_evening_before_counts_at_8_pm(self):
        self._log(self.site, '', timezone.make_aware(datetime(2026, 11, 8, 21, 0)))
        self.assertEqual(self._today(), 1)

    def test_attendance_only_does_not_count(self):
        ContractorAttendance.objects.create(project=self.site, date=date(2026, 11, 9),
                                            trade='CIVIL', headcount=12, time_in='09:00',
                                            recorded_by=self.pm, unlisted_contractor_name='Crew')
        self.assertEqual(self._today(), 0)

    def test_scheduler_and_opened_rows_do_not_count(self):
        self._log(self.site, 'due_dates_scheduled', NOW - timedelta(hours=1), actor=self.pm)
        self._log(self.site, 'project_overview_opened', NOW - timedelta(hours=1), actor=self.pm)
        self.assertEqual(self._today(), 0)

    def test_money_rows_count(self):
        self._log(self.site, sorted(MONEY_ACTIVITY_CODES)[0], NOW - timedelta(hours=2))
        self.assertEqual(self._today(), 1)

    def test_a_row_after_now_is_not_counted_for_that_now(self):
        self._log(self.site, '', NOW + timedelta(hours=1))
        self.assertEqual(self._today(), 0)
        self.assertEqual(self._today(now=NOW + timedelta(hours=2)), 1)

    def test_active_today_is_not_a_subset_of_active(self):
        self._log(self.site, '', NOW - timedelta(hours=1))
        b = self._b(self.site)
        self.assertEqual((b.active, b.active_today), (0, 1))


# ---------------------------------------------------------------------------
# 6. Query budget (P7)
# ---------------------------------------------------------------------------

class QueryCountTests(BreakdownFixture):

    def _sites(self, n):
        sites = []
        for i in range(n):
            site = self._mobilized(self._resco())
            self._log(site, '', NOW - timedelta(hours=1))
            sites.append(site)
        return sites

    def _count(self, sites):
        projects = Project.objects.filter(pk__in=[s.pk for s in sites])
        with CaptureQueriesContext(connection) as ctx:
            b = site_breakdown(projects, NOW)
        return len(ctx.captured_queries), b

    def test_one_query_for_1_and_for_50_sites(self):
        civil = self._activated()
        self._civil(civil, status=Task.IN_PROGRESS)
        one, b1 = self._count([civil])
        fifty, b50 = self._count([civil] + self._sites(49))
        self.assertEqual((one, fifty), (1, 1))
        self.assertEqual((b1.active, b50.in_execution, b50.mobilized, b50.active_today),
                         (1, 50, 49, 49))


# ---------------------------------------------------------------------------
# 7. The tender page (T2)
# ---------------------------------------------------------------------------

class TenderPageTests(BreakdownFixture):

    def setUp(self):
        self.mine = self._mobilized(self._resco())
        self._log(self.mine, '', timezone.now() - timedelta(hours=1))
        # Another PM's site in the same tender: the strip counts the whole tender.
        self.theirs = self._resco(pm=self.other_pm)
        self._resco(status='Draft')

    def _page(self, profile):
        client = Client()
        client.force_login(_fresh(profile.user))
        response = client.get(reverse('program_detail', args=[self.program.pk]))
        self.assertEqual(response.status_code, 200)
        return response

    def test_the_strip_renders_for_the_tenders_pm_and_for_ceo(self):
        for profile in (self.pm, self.ceo):
            with self.subTest(role=profile.role):
                response = self._page(profile)
                b = response.context['breakdown']
                self.assertEqual((b.in_execution, b.mobilized, b.active, b.active_today),
                                 (2, 1, 0, 1))
                html = response.content.decode()
                for text in ('Sites in execution', '>Mobilized<', '>Active<', '>Active today<',
                             'DC + design started', 'Civil In Progress/Done',
                             'any PMS update, last 24 h'):
                    self.assertIn(text, html)

    def test_the_planned_count_shows_under_sites_in_execution_when_set(self):
        Program.objects.filter(pk=self.program.pk).update(planned_site_count=94)
        self.assertIn('Sites in execution</div><div class="d">of 94 planned</div>',
                      self._page(self.ceo).content.decode())

    def test_no_planned_line_when_the_tender_has_no_planned_count(self):
        for blank in (None, 0):
            with self.subTest(planned=blank):
                Program.objects.filter(pk=self.program.pk).update(planned_site_count=blank)
                self.assertNotIn(' planned</div>', self._page(self.ceo).content.decode())

    def test_the_old_card_is_gone_and_the_status_badge_reads_activated(self):
        html = self._page(self.ceo).content.decode()
        self.assertNotIn('Active sites (live)', html)
        self.assertIn('Activated: 2', html)
        self.assertNotIn('>Active: ', html)
        # The comments in the template never reach the page.
        self.assertNotIn('TB-1', html)
