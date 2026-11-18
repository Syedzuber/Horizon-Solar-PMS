"""
TB-3 — design, procurement and attendance figures in site_breakdown, and their rows on the
tender page.

WHAT THIS PINS (each test fails if its rule is wrong)
    Design         every status lands in the bucket of its CEO pipeline stage: S0-S1 not
                   started (Design Hold included), S2-S4 in progress, S5 released; the
                   buckets are read from STATUS_TO_STAGE (moving a status there moves it
                   here); no DesignAssignment = no bucket
    Received       among MOBILIZED sites only; a component line with received > 0 on a
                   challan of any status, Rejected included (Q1); received + pending =
                   mobilized; the copy agrees with first_component_received() on every
                   site (Q2 tie-test)
    DCs to confirm Expected and Partially Received count, Received and Rejected do not;
                   counts challans, not sites; overdue only when the expected date is before
                   the IST day of `now`; no expected date is never overdue
    Attendance     a non-voided entry dated the IST day of `now`; yesterday, voided and the
                   IST midnight boundary; the copy agrees with sites_missing_attendance()
                   for the same day (Q2 tie-test)
    Exclusions     test, draft, cancelled and deleted sites in no TB-3 figure
    Query budget   still 1 query for 1 and for 50 sites with every TB-3 condition present
    Pages          the strip without show_extra renders byte-for-byte as before TB-3; the
                   tender page shows the attendance figure and the Design / Procurement rows
                   (the Site Dashboard shows them too since TB-4: tests_dashboard_breakdown_extra)

Run with:
    python manage.py test projects.tests_site_breakdown_extra --settings=solarpms.test_settings
"""
import hashlib
from datetime import date, datetime, timedelta
from datetime import timezone as dt_timezone
from decimal import Decimal
from unittest import mock

from django.db import connection
from django.template.loader import render_to_string
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from .attendance_alerts import sites_missing_attendance
from .models import (DC_CATEGORY_TO_MIRROR_CODE, DESIGN_ALLOCATED,
                     DESIGN_ASSIGNMENT_STATUS_CHOICES, DESIGN_AWAITING_ALLOCATION,
                     DESIGN_AWAITING_SURVEY, DESIGN_IN_DESIGN, DESIGN_RELEASED,
                     DESIGN_SURVEY_RETURNED, ContractorAttendance, DCLineItem,
                     DeliveryChallan, Project, Task, first_component_received)
from .phase_rules import PHASE_INSTALLATION
from .site_breakdown import (BUCKET_IN_PROGRESS, BUCKET_NOT_STARTED, BUCKET_RELEASED,
                             DESIGN_STARTED_STATUSES, design_bucket, site_breakdown)
from .tender_stages import STAGE_SURVEY_ON_FILE, STATUS_TO_STAGE
from .tests_site_breakdown import NOW, BreakdownFixture, _fresh
from .views_site_dashboard import StripFigures

DAY = date(2026, 11, 9)          # the IST day of NOW (8 PM IST, 9 Nov 2026)

#: The ruling (G2), written out by hand so the derivation in site_breakdown is checked
#: against it, not against itself.
NOT_STARTED = {DESIGN_AWAITING_SURVEY, DESIGN_AWAITING_ALLOCATION, DESIGN_SURVEY_RETURNED}
RELEASED = {DESIGN_RELEASED}

#: sha256 and length of _site_breakdown_strip.html rendered without show_extra, as the Site
#: Dashboard rendered it before TB-4, taken at c31c2e5 before TB-3 touched the template. The
#: dashboard passes show_extra since TB-4; these still pin the include's plain output.
#: TB-5 (18 Nov 2026) re-took both with "any PMS update, today" for "..., last 24 h": the
#: new output with the old text put back still hashed to c31c2e5's pins (1899 / 6ab05170…,
#: 1988 / d8454a9d…), so nothing else in the plain output moved.
DASHBOARD_STRIP_BEFORE_TB3 = {
    'full': (1895, '7932086f4d12731d99d17606b5689b22402099658efe408189823a7899548415'),
    'dash': (1984, '86697329ec0d5a05043f9fa6c19e09736d4e0335a8e6a5a5595b2c8aeb2c8dc6'),
}


def _utc(day, hour, minute):
    """`hour:minute` IST on `day` Nov 2026, expressed as a UTC-aware datetime — the form
    timezone.now() hands the views — so a rule that took the UTC date would be caught."""
    return timezone.make_aware(datetime(2026, 11, day, hour, minute)).astimezone(dt_timezone.utc)


class ExtraFixture(BreakdownFixture):

    _dc_seq = 0

    def _challan(self, site, status=DeliveryChallan.EXPECTED, expected=None):
        ExtraFixture._dc_seq += 1
        return DeliveryChallan.objects.create(
            project=site, dc_number=f'TB3-DC-{ExtraFixture._dc_seq}', dc_date=date(2026, 11, 1),
            status=status, expected_delivery_date=expected, created_by=self.scm)

    def _line(self, challan, received, category=DCLineItem.SOLAR_MODULES):
        return DCLineItem.objects.create(challan=challan, boq_category=category,
                                         item_description='Item', ordered_quantity=Decimal('10'),
                                         received_quantity=received)

    _att_seq = 0

    def _attendance(self, site, day=DAY, voided=False):
        ExtraFixture._att_seq += 1
        fields = {}
        if voided:
            fields = {'is_voided': True, 'void_note': 'wrong site', 'voided_by': self.pm,
                      'voided_at': timezone.now()}
        return ContractorAttendance.objects.create(
            project=site, date=day, trade='CIVIL', headcount=6, time_in='09:00',
            recorded_by=self.pm, unlisted_contractor_name=f'Crew {ExtraFixture._att_seq}',
            **fields)


# ---------------------------------------------------------------------------
# 1. Design buckets (G2)
# ---------------------------------------------------------------------------

class DesignBucketTests(ExtraFixture):

    def test_every_status_lands_in_its_ruled_bucket(self):
        sites = {}
        for status, _ in DESIGN_ASSIGNMENT_STATUS_CHOICES:
            site = self._resco()
            self._design(site, status)
            sites[status] = site.pk
        b = site_breakdown(self.program.sites.all(), NOW)
        for status, pk in sites.items():
            with self.subTest(status=status):
                expected = ('not_started' if status in NOT_STARTED
                            else 'released' if status in RELEASED else 'in_progress')
                found = [name for name, ids in (('not_started', b.design_not_started_ids),
                                                ('in_progress', b.design_in_progress_ids),
                                                ('released', b.design_released_ids))
                         if pk in ids]
                self.assertEqual(found, [expected])
        self.assertEqual((b.design_not_started, b.design_in_progress, b.design_released),
                         (3, len(DESIGN_ASSIGNMENT_STATUS_CHOICES) - 4, 1))

    def test_design_hold_is_not_started(self):
        site = self._resco()
        self._design(site, DESIGN_SURVEY_RETURNED)
        b = self._b(site)
        self.assertEqual((b.design_not_started_ids, b.design_in_progress, b.design_released),
                         ((site.pk,), 0, 0))

    def test_buckets_are_read_from_status_to_stage(self):
        site = self._resco()
        self._design(site, DESIGN_ALLOCATED)
        self.assertEqual(self._b(site).design_in_progress_ids, (site.pk,))
        with mock.patch.dict(STATUS_TO_STAGE, {DESIGN_ALLOCATED: STAGE_SURVEY_ON_FILE}):
            b = self._b(site)
            self.assertEqual((b.design_not_started_ids, b.design_in_progress),
                             ((site.pk,), 0))
            self.assertEqual(design_bucket(DESIGN_ALLOCATED), BUCKET_NOT_STARTED)

    def test_in_progress_plus_released_is_tb1_design_started(self):
        started = {s for s, _ in DESIGN_ASSIGNMENT_STATUS_CHOICES
                   if design_bucket(s) in (BUCKET_IN_PROGRESS, BUCKET_RELEASED)}
        self.assertEqual(started, set(DESIGN_STARTED_STATUSES))

    def test_no_design_record_is_in_no_bucket_and_buckets_add_up(self):
        bare = self._resco()
        held = self._resco()
        self._design(held, DESIGN_SURVEY_RETURNED)
        live = self._resco()
        self._design(live, DESIGN_IN_DESIGN)
        b = self._b(bare, held, live)
        for ids in (b.design_not_started_ids, b.design_in_progress_ids, b.design_released_ids):
            self.assertNotIn(bare.pk, ids)
        self.assertEqual(b.design_not_started + b.design_in_progress + b.design_released + 1,
                         b.in_execution)
        self.assertIsNone(design_bucket(None))


# ---------------------------------------------------------------------------
# 2. Received / pending (G3, Q1, Q2)
# ---------------------------------------------------------------------------

class ReceivedTests(ExtraFixture):

    def _mob(self):
        """A mobilized site: an open challan (no lines) and design started."""
        return self._mobilized(self._resco())

    def test_received_quantity_above_zero_counts_on_any_open_or_closed_status(self):
        for status in (DeliveryChallan.EXPECTED, DeliveryChallan.PARTIALLY_RECEIVED,
                       DeliveryChallan.RECEIVED):
            with self.subTest(status=status):
                site = self._mob()
                self._line(self._challan(site, status), Decimal('4'))
                b = self._b(site)
                self.assertEqual((b.received_ids, b.pending), ((site.pk,), 0))

    def test_zero_or_blank_received_is_pending(self):
        for received in (Decimal('0'), None):
            with self.subTest(received=received):
                site = self._mob()
                self._line(self._challan(site, DeliveryChallan.PARTIALLY_RECEIVED), received)
                b = self._b(site)
                self.assertEqual((b.received, b.pending_ids), (0, (site.pk,)))

    def test_a_rejected_challan_with_material_counts_as_received(self):
        # Ruling Q1 (the 20 Oct rule): Rejected means shortfall AND damage, not "nothing came".
        site = self._mob()
        self._line(self._challan(site, DeliveryChallan.REJECTED), Decimal('3'))
        self.assertEqual(self._b(site).received_ids, (site.pk,))

    def test_a_rejected_only_site_is_not_mobilized_so_in_neither(self):
        site = self._resco()
        self._design(site, DESIGN_IN_DESIGN)
        self._line(self._challan(site, DeliveryChallan.REJECTED), Decimal('3'))
        b = self._b(site)
        self.assertEqual((b.mobilized, b.received, b.pending), (0, 0, 0))

    def test_material_on_a_site_that_is_not_mobilized_is_not_counted(self):
        site = self._resco()
        self._design(site, DESIGN_AWAITING_ALLOCATION)   # design not started
        self._line(self._challan(site, DeliveryChallan.RECEIVED), Decimal('10'))
        self.assertTrue(first_component_received(site))
        b = self._b(site)
        self.assertEqual((b.mobilized, b.received, b.pending), (0, 0, 0))

    def test_received_plus_pending_is_mobilized(self):
        got, waiting = self._mob(), self._mob()
        self._line(self._challan(got), Decimal('1'))
        self._line(self._challan(waiting), None)
        self._resco()                                    # not mobilized
        b = site_breakdown(self.program.sites.all(), NOW)
        self.assertEqual(set(b.received_ids) | set(b.pending_ids), set(b.mobilized_ids))
        self.assertFalse(set(b.received_ids) & set(b.pending_ids))
        self.assertEqual((b.received_ids, b.pending_ids), ((got.pk,), (waiting.pk,)))

    def test_tie_the_copy_agrees_with_first_component_received_on_every_site(self):
        # Every site is mobilized (open challan + design started), so received_ids is the
        # copied condition exactly; each varies only in what its lines say.
        cases = []
        for status in (DeliveryChallan.EXPECTED, DeliveryChallan.PARTIALLY_RECEIVED,
                       DeliveryChallan.RECEIVED, DeliveryChallan.REJECTED):
            for received in (Decimal('5'), Decimal('0'), None):
                site = self._mob()
                self._line(self._challan(site, status), received)
                cases.append(site)
        for category in DC_CATEGORY_TO_MIRROR_CODE:
            site = self._mob()
            self._line(self._challan(site), Decimal('0.5'), category=category)
            cases.append(site)
        cases.append(self._mob())                        # a challan with no lines at all
        b = self._b(*cases)
        self.assertEqual(set(b.mobilized_ids), {s.pk for s in cases})
        for site in cases:
            with self.subTest(site=site.project_id):
                self.assertEqual(site.pk in b.received_ids, first_component_received(site))
        # Both sides of the comparison are exercised, Rejected with material included.
        self.assertEqual(b.received, 4 + len(DC_CATEGORY_TO_MIRROR_CODE))
        self.assertGreater(b.pending, 0)


# ---------------------------------------------------------------------------
# 3. DCs to confirm (G4)
# ---------------------------------------------------------------------------

class DcsToConfirmTests(ExtraFixture):

    def test_expected_and_partially_received_count_received_and_rejected_do_not(self):
        site = self._resco()
        for status in (DeliveryChallan.EXPECTED, DeliveryChallan.PARTIALLY_RECEIVED,
                       DeliveryChallan.RECEIVED, DeliveryChallan.REJECTED):
            self._challan(site, status)
        b = self._b(site)
        self.assertEqual((b.dcs_to_confirm, b.dcs_to_confirm_site_ids), (2, (site.pk,)))

    def test_counts_challans_not_sites_and_any_site_in_execution(self):
        one, two = self._resco(), self._resco()          # neither is mobilized
        self._challan(one)
        self._challan(one, DeliveryChallan.PARTIALLY_RECEIVED)
        self._challan(two)
        b = self._b(one, two)
        self.assertEqual((b.mobilized, b.dcs_to_confirm, b.dcs_to_confirm_site_ids),
                         (0, 3, (one.pk, two.pk)))

    def test_overdue_only_when_the_expected_date_is_before_today(self):
        site = self._resco()
        self._challan(site, expected=DAY - timedelta(days=1))   # overdue
        self._challan(site, expected=DAY)                       # due today: not overdue
        self._challan(site, expected=DAY + timedelta(days=3))
        self._challan(site)                                     # no date: never overdue
        self._challan(site, DeliveryChallan.RECEIVED, expected=DAY - timedelta(days=9))
        b = self._b(site)
        self.assertEqual((b.dcs_to_confirm, b.dcs_overdue, b.dcs_overdue_site_ids),
                         (4, 1, (site.pk,)))

    def test_today_is_the_ist_day_of_now(self):
        # 00:30 IST on 10 Nov is still 9 Nov in UTC: a challan expected on 9 Nov is overdue
        # in IST and would not be if the UTC date were used.
        site = self._resco()
        self._challan(site, expected=DAY)
        self.assertEqual(self._b(site, now=_utc(10, 0, 30)).dcs_overdue, 1)
        self.assertEqual(self._b(site, now=NOW).dcs_overdue, 0)


# ---------------------------------------------------------------------------
# 4. Attendance today (G5, Q2)
# ---------------------------------------------------------------------------

class AttendanceTodayTests(ExtraFixture):

    def setUp(self):
        self.site = self._resco()

    def _att(self, now=NOW):
        return self._b(self.site, now=now).attendance_today_ids

    def test_an_entry_today_counts(self):
        self._attendance(self.site)
        self.assertEqual(self._att(), (self.site.pk,))

    def test_yesterday_does_not(self):
        self._attendance(self.site, day=DAY - timedelta(days=1))
        self.assertEqual(self._att(), ())

    def test_a_voided_entry_does_not(self):
        self._attendance(self.site, voided=True)
        self.assertEqual(self._att(), ())
        self._attendance(self.site)
        self.assertEqual(self._att(), (self.site.pk,))

    def test_ist_midnight_boundary(self):
        self._attendance(self.site)                      # dated 9 Nov
        # 00:01 IST on 9 Nov is 8 Nov in UTC: still the 9th's entry. 23:59 IST on 9 Nov is
        # 9 Nov in UTC too, but 00:01 IST on 10 Nov is 9 Nov in UTC: no longer the 9th.
        self.assertEqual(self._att(now=_utc(9, 0, 1)), (self.site.pk,))
        self.assertEqual(self._att(now=_utc(9, 23, 59)), (self.site.pk,))
        self.assertEqual(self._att(now=_utc(8, 23, 59)), ())
        self.assertEqual(self._att(now=_utc(10, 0, 1)), ())

    def test_tie_the_copy_agrees_with_sites_missing_attendance_for_the_same_day(self):
        # Activated sites with installation work In Progress: the alert considers exactly
        # these, so each must be in one list and absent from the other.
        sites = [self._activated() for _ in range(4)]
        for site in sites:
            work = (Task.objects.filter(phase__project=site, is_mirror=False,
                                        template_task__phase__code=PHASE_INSTALLATION)
                    .order_by('pk').first())
            self.assertIsNotNone(work)
            Task.objects.filter(pk=work.pk).update(status=Task.IN_PROGRESS)
        logged, yesterday, voided, none = sites
        self._attendance(logged)
        self._attendance(yesterday, day=DAY - timedelta(days=1))
        self._attendance(voided, voided=True)
        b = self._b(*sites)
        missing = {s.pk for s in sites_missing_attendance(DAY)}
        for site in sites:
            with self.subTest(site=site.project_id):
                self.assertNotEqual(site.pk in b.attendance_today_ids, site.pk in missing)
        self.assertEqual(b.attendance_today_ids, (logged.pk,))
        self.assertEqual(missing, {yesterday.pk, voided.pk, none.pk})


# ---------------------------------------------------------------------------
# 5. Exclusions (G1)
# ---------------------------------------------------------------------------

class ExclusionTests(ExtraFixture):

    def test_test_draft_cancelled_and_deleted_sites_are_in_no_tb3_figure(self):
        hidden = [self._resco(is_test=True), self._resco(status='Draft'),
                  self._resco(status='Cancelled'), self._resco(is_deleted=True)]
        for site in hidden:
            # Every TB-3 condition met, so only the exclusion keeps each out.
            self._mobilized(site)
            self._line(self._challan(site, expected=DAY - timedelta(days=2)), Decimal('2'))
            self._attendance(site)
        b = site_breakdown(self.program.sites.all(), NOW)
        for ids in (b.design_not_started_ids, b.design_in_progress_ids, b.design_released_ids,
                    b.received_ids, b.pending_ids, b.dcs_to_confirm_site_ids,
                    b.dcs_overdue_site_ids, b.attendance_today_ids):
            for site in hidden:
                self.assertNotIn(site.pk, ids)
        self.assertEqual((b.in_execution, b.dcs_to_confirm, b.dcs_overdue), (0, 0, 0))


# ---------------------------------------------------------------------------
# 6. Query budget (P6)
# ---------------------------------------------------------------------------

class QueryBudgetTests(ExtraFixture):

    def _full(self):
        site = self._mobilized(self._resco())
        self._line(self._challan(site, expected=DAY - timedelta(days=1)), Decimal('1'))
        self._attendance(site)
        return site

    def _count(self, sites):
        projects = Project.objects.filter(pk__in=[s.pk for s in sites])
        with CaptureQueriesContext(connection) as ctx:
            b = site_breakdown(projects, NOW)
        return len(ctx.captured_queries), b

    def test_one_query_for_1_and_for_50_sites_with_every_tb3_condition(self):
        first = self._full()
        one, b1 = self._count([first])
        fifty, b50 = self._count([first] + [self._full() for _ in range(49)])
        self.assertEqual((one, fifty), (1, 1))
        self.assertEqual((b1.received, b1.dcs_to_confirm, b1.dcs_overdue, b1.attendance_today),
                         (1, 2, 1, 1))
        self.assertEqual((b50.received, b50.dcs_to_confirm, b50.dcs_overdue,
                          b50.attendance_today, b50.design_in_progress),
                         (50, 100, 50, 50, 50))


# ---------------------------------------------------------------------------
# 7. Pages (T2)
# ---------------------------------------------------------------------------

class PageTests(ExtraFixture):

    def test_the_site_dashboard_strip_renders_as_before_tb3(self):
        cases = {
            'full': {'breakdown': StripFigures(7, 3, 2, 4, None), 'note': None},
            'dash': {'breakdown': StripFigures(5, None, None, 1, 'Mobilized and Active apply '
                                               'to RESCO sites only'),
                     'note': 'Mobilized and Active apply to RESCO sites only'},
        }
        for name, context in cases.items():
            with self.subTest(case=name):
                html = render_to_string('projects/_site_breakdown_strip.html', context)
                self.assertEqual((len(html), hashlib.sha256(html.encode()).hexdigest()),
                                 DASHBOARD_STRIP_BEFORE_TB3[name])

    def test_the_tender_page_shows_attendance_and_the_two_rows(self):
        # program_detail passes the real timezone.now(); the fixture freezes only the
        # no-argument localdate(), so the page's "today" is the IST day of the real clock.
        today = timezone.localdate(timezone.now())
        got = self._mobilized(self._resco())
        self._line(self._challan(got, expected=today - timedelta(days=1)), Decimal('1'))
        self._attendance(got, day=today)
        self._mobilized(self._resco())                   # mobilized, nothing received
        held = self._resco()
        self._design(held, DESIGN_SURVEY_RETURNED)
        client = Client()
        client.force_login(_fresh(self.ceo.user))
        response = client.get(reverse('program_detail', args=[self.program.pk]))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn('<div class="v">1</div><div class="l">Attendance today</div>', html)
        self.assertIn('<span class="k">Design</span> <b>1</b> not started · <b>2</b> in '
                      'progress · <b>0</b> released + BOQ', html)
        # Two open challans on `got` (the fixture's Expected one and the dated one), one late.
        self.assertIn('<span class="k">Procurement</span> <b>1</b> received of 2 mobilized · '
                      '<b>1</b> pending · <b>3</b> DCs to confirm, <b>1</b> overdue', html)
        # The extra block follows the TB-1 section rather than sitting inside it.
        section_end = html.index('</section>', html.index('<section class="tb mb-4"'))
        self.assertGreater(html.index('data-tb3-attendance'), section_end)
        self.assertEqual(html.count('<div class="tb tbx mb-4">'), 1)
