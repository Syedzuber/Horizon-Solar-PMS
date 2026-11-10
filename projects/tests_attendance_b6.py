"""
Contractor attendance — ATT-B6: "Vendor attendance", every site's crews for one day (SCM).

WHAT IS PINNED HERE
-------------------
    who may open the page (ruling 1, edge case 10)          AccessTests
    the day: default, past, future, malformed (ruling 3)    DayTests
    the site blocks and their cards (rulings 6, 8)          ListTests
    "Not logged" and the voided footnote (ruling 7)         NotLoggedTests
    filters, together and alone (rulings 4, C, D)           FilterTests
    the summary strip agrees with the blocks (ruling 5)     SummaryTests
    the SCM dashboard link (ruling 10)                      DashboardLinkTests
    queries do not grow with the number of sites            QueryBoundTests

"Now" is Monday 2 Nov 2026, 10:30 AM IST (B4Fixture's clock). ContractorAttendance's
recorded_at default is bound at import, so it would keep the real time and make every
entry look late; _entry() sets it to the entry's own day instead.

Run with:
    python manage.py test projects.tests_attendance_b6 --settings=solarpms.test_settings
"""
from datetime import datetime, time, timedelta

from django.db import connection
from django.test import override_settings
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from django.utils.html import escape

from .attendance_views import (
    BAD_DAY, FUTURE_DAY, NOT_LOGGED_CONTRACTOR_FILTER, vendor_attendance_day,
    vendor_attendance_entries, vendor_attendance_today_counts,
)
from .models import ContractorAttendance, Program, Project, Task, Vendor
from .permissions import user_can_view_vendor_attendance
from .tests_attendance_b4 import MONDAY, MONDAY_1030, B4Fixture, _profile

SUNDAY = MONDAY - timedelta(days=1)


def _client(profile):
    client = Client()
    client.force_login(profile.user)
    return client


class B6Fixture(B4Fixture):
    """B4's template, people and site builder, plus a second tender, a second contractor,
    Finance and CEO users, and an entry builder that sets recorded_at to the entry's day."""

    def setUp(self):
        super().setUp()
        self.finance = _profile('b6_fin', 'Finance')
        self.ceo = _profile('b6_ceo', 'CEO')
        self.program2 = Program.objects.create(name='Test-B6-Other', program_type='OPEX',
                                               client_name='B6Client', status='Active',
                                               short_tender_code='B6')
        self.vendor2 = Vendor.objects.create(name='Verma Electricals', contact_person='V',
                                             phone='9876543211', kind='contractor')

    def _entry(self, site, day=MONDAY, contractor='default', unlisted='', headcount=4,
               trade='CIVIL', photo=True, voided=False, time_in=time(9, 0),
               recorded_at=None):
        contractor = self.vendor if contractor == 'default' else contractor
        recorded_at = recorded_at or timezone.make_aware(datetime.combine(day, time(9, 5)))
        photo_fields = ({'photo_file_name': 'crew.jpg', 'photo_bucket': 'b',
                         'photo_path': f'{site.project_id}/attendance/{trade}.jpg'}
                        if photo else {})
        void_fields = ({'is_voided': True, 'void_note': 'Wrong site', 'voided_by': self.pm,
                        'voided_at': MONDAY_1030} if voided else {})
        return ContractorAttendance.objects.create(
            project=site, date=day, contractor=contractor,
            unlisted_contractor_name='' if contractor else unlisted, trade=trade,
            headcount=headcount, time_in=time_in, recorded_by=self.se,
            recorded_at=recorded_at, updated_at=recorded_at, **photo_fields, **void_fields)

    def _get(self, profile=None, **params):
        return _client(profile or self.scm).get(reverse('vendor_attendance'), params)


# ---------------------------------------------------------------------------
# Access
# ---------------------------------------------------------------------------

class AccessTests(B6Fixture):

    def test_helper_is_scm_and_admin_only(self):
        for profile in (self.scm, self.admin):
            self.assertTrue(user_can_view_vendor_attendance(profile.user), profile.role)
        for profile in (self.pm, self.coord, self.se, self.finance, self.ceo):
            self.assertFalse(user_can_view_vendor_attendance(profile.user), profile.role)

    def test_scm_and_admin_open_the_page(self):
        for profile in (self.scm, self.admin):
            self.assertEqual(self._get(profile).status_code, 200, profile.role)

    def test_pm_se_finance_and_ceo_are_refused(self):
        """Edge case 10."""
        for profile in (self.pm, self.se, self.finance, self.ceo):
            self.assertEqual(self._get(profile).status_code, 403, profile.role)

    def test_anonymous_goes_to_login(self):
        response = Client().get(reverse('vendor_attendance'))
        self.assertEqual(response.status_code, 302)

    def test_post_is_not_allowed(self):
        response = _client(self.scm).post(reverse('vendor_attendance'))
        self.assertEqual(response.status_code, 405)


# ---------------------------------------------------------------------------
# The day
# ---------------------------------------------------------------------------

class DayTests(B6Fixture):

    def test_parser(self):
        self.assertEqual(vendor_attendance_day('', MONDAY), (MONDAY, None))
        self.assertEqual(vendor_attendance_day(None, MONDAY), (MONDAY, None))
        self.assertEqual(vendor_attendance_day('2026-10-30', MONDAY),
                         (MONDAY - timedelta(days=3), None))
        self.assertEqual(vendor_attendance_day('2026-11-03', MONDAY), (MONDAY, FUTURE_DAY))
        for raw in ('yesterday', '2026-13-01', '2026-02-30', '02-11-2026', '99999-01-01'):
            self.assertEqual(vendor_attendance_day(raw, MONDAY), (MONDAY, BAD_DAY), raw)

    def test_defaults_to_today_with_next_disabled(self):
        site = self._site('B6-A')
        self._entry(site)
        response = self._get()
        self.assertEqual(response.context['day'], MONDAY)
        self.assertContains(response, 'id="va-next-disabled"')
        self.assertNotContains(response, 'id="va-next"')
        self.assertContains(response, f'date={SUNDAY.isoformat()}')
        self.assertContains(response, f'max="{MONDAY.isoformat()}"')

    def test_past_date_shows_that_day_with_a_next_link(self):
        site = self._site('B6-A')
        self._entry(site, day=SUNDAY, headcount=7)
        self._entry(site, day=MONDAY, headcount=3, trade='ELECTRICAL')
        response = self._get(date=SUNDAY.isoformat())
        self.assertEqual(response.context['day'], SUNDAY)
        self.assertEqual(response.context['summary']['people'], 7)
        self.assertContains(response, 'id="va-next"')
        self.assertContains(response, f'date={MONDAY.isoformat()}')

    def test_future_date_falls_back_to_today_with_a_message(self):
        """Edge case 2."""
        response = self._get(date='2026-11-05')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['day'], MONDAY)
        self.assertContains(response, escape(FUTURE_DAY))

    def test_malformed_date_falls_back_to_today_with_a_message(self):
        """Edge case 3: no 500."""
        response = self._get(date='not-a-date')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['day'], MONDAY)
        self.assertContains(response, escape(BAD_DAY))

    def test_navigation_keeps_the_filters(self):
        self._site('B6-A')
        response = self._get(date=SUNDAY.isoformat(), q='B6', program=self.program.pk)
        self.assertIn('q=B6', response.context['prev_url'])
        self.assertIn(f'program={self.program.pk}', response.context['next_url'])
        self.assertIn(f'date={MONDAY.isoformat()}', response.context['next_url'])


# ---------------------------------------------------------------------------
# The site blocks
# ---------------------------------------------------------------------------

class ListTests(B6Fixture):

    def test_one_block_per_site_in_code_order_with_header(self):
        site_b = self._site('B6-B')
        site_a = self._site('B6-A')
        self._entry(site_b, headcount=5)
        self._entry(site_a, headcount=2)
        self._entry(site_a, contractor=self.vendor2, trade='ELECTRICAL', headcount=3)
        response = self._get()
        blocks = response.context['blocks']
        self.assertEqual([b['project'].project_id for b in blocks], ['B6-A', 'B6-B'])
        self.assertEqual((blocks[0]['crews'], blocks[0]['people']), (2, 5))
        self.assertEqual(blocks[0]['pm'], self.pm)
        self.assertContains(response, 'PM Ravi Kumar Singh')
        self.assertContains(response, 'RESCO')
        self.assertContains(response, reverse('attendance_today', args=['B6-A']))

    def test_site_without_pm_shows_no_pm(self):
        """Edge case 8."""
        self._entry(self._site('B6-A', pm=None))
        response = self._get()
        self.assertIsNone(response.context['blocks'][0]['pm'])
        self.assertContains(response, 'class="va-no-pm"')

    def test_deactivated_pm_shows_no_pm(self):
        self.pm.is_active = False
        self.pm.save()
        self._entry(self._site('B6-A'))
        self.assertContains(self._get(), 'class="va-no-pm"')

    def test_entry_without_photo_has_chip_and_no_view_photo_link(self):
        """Edge case 9."""
        site = self._site('B6-A')
        entry = self._entry(site, photo=False)
        response = self._get()
        self.assertContains(response, 'att-no-photo-chip')
        self.assertNotContains(response, 'va-photo-link')
        self.assertNotContains(response, reverse('attendance_photo', args=['B6-A', entry.pk]))

    def test_entry_with_photo_links_to_the_signed_photo_view(self):
        site = self._site('B6-A')
        entry = self._entry(site)
        response = self._get()
        self.assertContains(response, 'va-photo-link')
        self.assertContains(response, reverse('attendance_photo', args=['B6-A', entry.pk]))
        self.assertNotContains(response, 'att-no-photo-chip')

    def test_unmapped_crew_shows_not_listed_and_a_map_link_to_the_queue(self):
        """Ruling 8."""
        self._entry(self._site('B6-A'), contractor=None, unlisted='Raju Civil')
        response = self._get()
        self.assertContains(response, 'Raju Civil')
        self.assertContains(response, 'Not listed · waiting for SCM')
        self.assertContains(response, f"{reverse('attendance_to_map')}?site=B6-A")
        self.assertEqual(response.context['summary']['unmapped'], 1)

    def test_cards_carry_no_actions(self):
        """Ruling B: _entry_card.html with every action off — no edit, time out, correct
        or map forms, whoever opens the page."""
        site = self._site('B6-A')
        self._entry(site, contractor=None, unlisted='Raju Civil')
        self._entry(site, day=SUNDAY)
        for profile, day in ((self.scm, MONDAY), (self.admin, MONDAY), (self.admin, SUNDAY)):
            response = self._get(profile, date=day.isoformat())
            for marker in ('attendance_edit', 'Mark time out', 'att-correct-link',
                           'att-map-form', 'att-remap-form', 'att-locked', 'att-history'):
                self.assertNotContains(response, marker)

    def test_chips_come_from_the_site_page_card(self):
        """Open today, Left open on a past day, Late entry, Originally logged as, and the
        photo-time warning — all drawn by _entry_card.html from the same helpers."""
        site = self._site('B6-A')
        mapped = self._entry(site, trade='ELECTRICAL')
        ContractorAttendance.objects.filter(pk=mapped.pk).update(original_unlisted_name='raju')
        old_photo = self._entry(site)
        ContractorAttendance.objects.filter(pk=old_photo.pk).update(
            photo_taken_at=MONDAY_1030 - timedelta(days=3))
        today = self._get()
        self.assertContains(today, '>Open<')
        self.assertContains(today, 'Originally logged as ‘raju’')
        self.assertContains(today, 'att-photo-flag')
        self._entry(site, day=SUNDAY, recorded_at=MONDAY_1030)  # recorded the next day
        past = self._get(date=SUNDAY.isoformat())
        self.assertContains(past, 'att-left-open-chip')
        self.assertContains(past, 'att-late-chip')

    def test_soft_deleted_site_is_never_shown(self):
        """Edge case 7: neither in the list nor under Not logged."""
        gone = self._site('B6-GONE')
        self._entry(gone)
        self._site('B6-GONE2')
        Project.objects.filter(project_id__in=['B6-GONE', 'B6-GONE2']).update(is_deleted=True)
        response = self._get()
        self.assertEqual(response.context['blocks'], [])
        self.assertEqual(response.context['not_logged'], [])
        self.assertNotContains(response, 'B6-GONE')

    def test_blocks_open_up_to_ten_sites_and_collapse_beyond(self):
        for n in range(10):
            self._entry(self._site(f'B6-{n:02d}'))
        response = self._get()
        self.assertTrue(response.context['blocks_open'])
        self.assertContains(response, "x-data=\"{ open: true }\"", count=10)
        self._entry(self._site('B6-10'))
        response = self._get()
        self.assertFalse(response.context['blocks_open'])
        self.assertContains(response, "x-data=\"{ open: false }\"", count=11)

    def test_empty_day(self):
        """Edge case 1: zeros, a friendly empty state, and Not logged still shown."""
        self._site('B6-A')
        response = self._get()
        self.assertEqual(response.context['summary'],
                         {'sites': 0, 'crews': 0, 'people': 0, 'not_logged': 1,
                          'unmapped': 0})
        self.assertContains(response, 'No attendance logged on any site on 02 Nov 2026.')
        self.assertContains(response, 'id="va-not-logged-title"')
        self.assertContains(response, 'class="card shadow-sm mb-2 va-not-logged"', count=1)


# ---------------------------------------------------------------------------
# Not logged and the voided footnote
# ---------------------------------------------------------------------------

class NotLoggedTests(B6Fixture):

    def test_working_site_with_nothing_logged_is_listed(self):
        self._entry(self._site('B6-A'))
        self._site('B6-B')
        self._site('B6-C', status=Task.NOT_STARTED)  # no installation work in progress
        response = self._get()
        self.assertEqual([row['project'].project_id for row in response.context['not_logged']],
                         ['B6-B'])
        self.assertEqual(response.context['not_logged'][0]['pm'], self.pm)
        self.assertContains(response, reverse('attendance_today', args=['B6-B']))
        self.assertContains(response, '>Not logged<')

    def test_site_with_only_voided_entries_is_not_logged_and_counted(self):
        """Edge case 4."""
        site = self._site('B6-A')
        self._entry(site, voided=True)
        self._entry(site, trade='ELECTRICAL', voided=True)
        response = self._get()
        self.assertEqual(response.context['blocks'], [])
        self.assertEqual([row['project'].project_id for row in response.context['not_logged']],
                         ['B6-A'])
        self.assertContains(response, '2 voided entries hidden')

    def test_voided_entry_beside_a_live_one_is_left_out_of_the_block(self):
        site = self._site('B6-A')
        self._entry(site, headcount=4)
        self._entry(site, trade='ELECTRICAL', headcount=9, voided=True)
        response = self._get()
        self.assertEqual(response.context['blocks'][0]['people'], 4)
        self.assertContains(response, '1 voided entry hidden')
        self.assertNotContains(response, 'att-voided')

    def test_not_logged_without_pm(self):
        self._site('B6-A', pm=None)
        response = self._get()
        self.assertIsNone(response.context['not_logged'][0]['pm'])
        self.assertContains(response, 'No PM')

    def test_past_day_label_says_it_uses_todays_task_status(self):
        """Edge case 12."""
        self._site('B6-A')
        response = self._get(date=SUNDAY.isoformat())
        self.assertContains(response, "Not logged (based on today's task status)")
        self.assertEqual([row['project'].project_id for row in response.context['not_logged']],
                         ['B6-A'])
        self.assertNotContains(self._get(), 'based on today')


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------

class FilterTests(B6Fixture):

    def setUp(self):
        super().setUp()
        self.a = self._site('B6-A')                                 # tender 1, vendor
        self.b = self._site('B6-B')                                 # tender 1, vendor2
        self.c = self._site('B6-C')                                 # tender 2, both
        Project.objects.filter(pk=self.c.pk).update(program=self.program2)
        self.res = self._site('RES-1', project_type='Residential')  # no tender
        self._site('B6-D')                                          # tender 1, not logged
        self._entry(self.a, headcount=2)
        self._entry(self.b, contractor=self.vendor2, trade='ELECTRICAL', headcount=3)
        self._entry(self.c, headcount=4)
        self._entry(self.c, contractor=self.vendor2, trade='ELECTRICAL', headcount=5)
        self._entry(self.res, headcount=6)

    def _codes(self, response):
        return [b['project'].project_id for b in response.context['blocks']]

    def test_unfiltered_shows_every_site_and_type(self):
        response = self._get()
        self.assertEqual(self._codes(response), ['B6-A', 'B6-B', 'B6-C', 'RES-1'])
        self.assertContains(response, 'Residential')

    def test_contractor_filter(self):
        """Edge case 5: only sites the contractor logged on, only its crews, summary
        matching; Not logged replaced by the note (ruling C)."""
        response = self._get(contractor=self.vendor2.pk)
        self.assertEqual(self._codes(response), ['B6-B', 'B6-C'])
        self.assertEqual(response.context['summary'],
                         {'sites': 2, 'crews': 2, 'people': 8, 'not_logged': None,
                          'unmapped': 0})
        self.assertIsNone(response.context['not_logged'])
        self.assertContains(response, escape(NOT_LOGGED_CONTRACTOR_FILTER))
        self.assertNotContains(response, 'id="va-sum-not-logged"')
        self.assertNotContains(response, 'id="va-not-logged-title"')

    def test_tender_filter_narrows_list_and_not_logged(self):
        response = self._get(program=self.program.pk)
        self.assertEqual(self._codes(response), ['B6-A', 'B6-B'])
        self.assertEqual([row['project'].project_id for row in response.context['not_logged']],
                         ['B6-D'])
        self.assertEqual(response.context['summary']['not_logged'], 1)
        response = self._get(program=self.program2.pk)
        self.assertEqual(self._codes(response), ['B6-C'])
        self.assertEqual(response.context['not_logged'], [])

    def test_site_code_search_narrows_list_and_not_logged(self):
        response = self._get(q='b6-d')
        self.assertEqual(self._codes(response), [])
        self.assertEqual([row['project'].project_id for row in response.context['not_logged']],
                         ['B6-D'])
        response = self._get(q='res')
        self.assertEqual(self._codes(response), ['RES-1'])

    def test_filters_combine(self):
        response = self._get(program=self.program.pk, contractor=self.vendor.pk, q='A')
        self.assertEqual(self._codes(response), ['B6-A'])
        self.assertEqual(response.context['summary']['people'], 2)

    def test_filter_matching_nothing_names_the_filter(self):
        """Edge case 6."""
        response = self._get(program=self.program2.pk, contractor=self.vendor.pk, q='zzz')
        self.assertEqual(self._codes(response), [])
        self.assertContains(response, 'No attendance on 02 Nov 2026 for tender Test-B6-Other · '
                                      'contractor Sharma Civil Works · site code containing '
                                      '‘zzz’.')
        self.assertContains(response, 'id="va-clear"')

    def test_clear_filters_link_keeps_only_the_date(self):
        response = self._get(date=SUNDAY.isoformat(), q='B6')
        self.assertEqual(response.context['clear_url'],
                         f"{reverse('vendor_attendance')}?date={SUNDAY.isoformat()}")
        self.assertNotContains(self._get(), 'id="va-clear"')

    def test_unknown_filter_values_are_ignored(self):
        response = self._get(program='999999', contractor='abc')
        self.assertEqual(self._codes(response), ['B6-A', 'B6-B', 'B6-C', 'RES-1'])

    def test_deactivated_contractor_in_the_url_still_shows(self):
        """Ruling D."""
        Vendor.objects.filter(pk=self.vendor2.pk).update(is_active=False)
        response = self._get(contractor=self.vendor2.pk)
        self.assertIn(self.vendor2, response.context['vendors'])
        self.assertContains(response, 'Verma Electricals (inactive)')
        self.assertEqual(self._codes(response), ['B6-B', 'B6-C'])
        self.assertNotIn(self.vendor2, self._get().context['vendors'])


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

class SummaryTests(B6Fixture):

    def test_summary_matches_the_blocks(self):
        a, b = self._site('B6-A'), self._site('B6-B')
        self._site('B6-C')
        self._entry(a, headcount=3)
        self._entry(a, contractor=None, unlisted='Raju', trade='ELECTRICAL', headcount=6)
        self._entry(b, headcount=11)
        self._entry(b, trade='ELECTRICAL', headcount=50, voided=True)
        response = self._get()
        summary, blocks = response.context['summary'], response.context['blocks']
        self.assertEqual(summary, {'sites': 2, 'crews': 3, 'people': 20, 'not_logged': 1,
                                   'unmapped': 1})
        self.assertEqual(summary['crews'], sum(b['crews'] for b in blocks))
        self.assertEqual(summary['people'], sum(b['people'] for b in blocks))
        self.assertContains(response, '<div class="fw-semibold fs-5" id="va-sum-people">20</div>',
                            html=True)


# ---------------------------------------------------------------------------
# SCM dashboard link
# ---------------------------------------------------------------------------

# DASH-B4: this class exercises a retired dashboard page itself, so the redirect to the
# Site Dashboard is switched off for it. Remove it with the old views (deletion commit).
@override_settings(RETIRED_DASHBOARDS_REDIRECT=False)
class DashboardLinkTests(B6Fixture):

    def test_counts_are_todays_live_entries_on_live_sites(self):
        a, b, gone = self._site('B6-A'), self._site('B6-B'), self._site('B6-GONE')
        self._entry(a, headcount=3)
        self._entry(a, trade='ELECTRICAL', headcount=4)
        self._entry(b, headcount=5)
        self._entry(b, trade='ELECTRICAL', headcount=40, voided=True)
        self._entry(b, day=SUNDAY, trade='STRUCTURE_MOUNTING', headcount=30)
        self._entry(gone, headcount=20)
        Project.objects.filter(pk=gone.pk).update(is_deleted=True)
        with self.assertNumQueries(1):
            counts = vendor_attendance_today_counts()
        self.assertEqual(counts, {'sites': 2, 'people': 12})

    def test_scm_dashboard_shows_the_link_with_todays_figures(self):
        self._entry(self._site('B6-A'), headcount=12)
        response = _client(self.scm).get(reverse('dashboard_scm'))
        self.assertContains(response, reverse('vendor_attendance'))
        self.assertContains(response, 'Vendor attendance · 1 site · 12 people')


# ---------------------------------------------------------------------------
# Query bound
# ---------------------------------------------------------------------------

class QueryBoundTests(B6Fixture):

    def _count(self, profile=None, **params):
        with CaptureQueriesContext(connection) as ctx:
            response = self._get(profile, **params)
        self.assertEqual(response.status_code, 200)
        return len(ctx.captured_queries)

    def _sites(self, start, stop):
        for n in range(start, stop):
            site = self._site(f'B6-{n:03d}', pm=self.pm if n % 2 else self.pm2)
            self._entry(site, contractor=self.vendor if n % 3 else None,
                        unlisted='' if n % 3 else f'Crew {n}', photo=bool(n % 4))
            self._site(f'B6-N{n:03d}')  # a working site with nothing logged

    def test_page_queries_do_not_grow_with_the_number_of_sites(self):
        """Edge case 11: 2 sites and 60 sites cost the same, for SCM and Admin, with and
        without filters."""
        self._sites(0, 2)
        cases = [(self.scm, {}), (self.admin, {}),
                 (self.scm, {'program': self.program.pk, 'contractor': self.vendor.pk,
                             'q': 'B6'})]
        self._count()  # warm-up: the first request pays one-off lookups
        few = [self._count(profile, **params) for profile, params in cases]
        self._sites(2, 60)
        many = [self._count(profile, **params) for profile, params in cases]
        self.assertEqual(many, few)
        self.assertEqual(len(self._get().context['blocks']), 60)

    def test_entries_query_is_one_query(self):
        self._sites(0, 5)
        with self.assertNumQueries(1):
            entries = list(vendor_attendance_entries(MONDAY))
        with self.assertNumQueries(0):
            for entry in entries:
                str(entry.project.assigned_pm.user)
                entry.contractor_label
                str(entry.recorded_by.user)
