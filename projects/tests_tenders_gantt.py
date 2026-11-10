"""
DASH-B3 — the Tenders Gantt page (views_tenders_gantt.tenders_gantt) and its data and
geometry module (tenders_gantt.py).

WHAT THIS PINS
    Access        200 for CEO, PM (own sites only) and the Design Head; 403 for a Site
                  Engineer and a profile-less account; login redirect for anonymous (G1)
    Scope         OPEX sites with a Program only: CAPEX, Residential, standalone, test and
                  Cancelled sites never appear; drafts sit in their own group (G1, G8)
    Geometry      a date's offset at both zooms; no negative width; bars outside the range
                  clipped to its edge, not dropped; the range's floor and ceiling (R1, R2)
    Execution     the slip tail only when behind and only on the Execution summary (G4);
                  only the open block past its due date is red, later blocks merely
                  shifted; "M" on a block holding a PM-typed date (G5)
    Unscheduled   the reason label at today; an excluded site's Execution drawn from its
                  location copies (G7)
    Order         both sorts; the "needs a Civil date" group and its toggle (G8)
    Tender cards  counts equal the rows shown for that tender; the card filter (G9)
    Money         no payment field in the context and no figure in the page, every role
                  (G12, R4)
    Escaping      a site name holding <script> is printed as text
    Queries       the same number for 3 sites and 30 sites; never_cache

Today is frozen at 9 Nov 2026 by SchedFixture (through DashFixture).

Run with:
    python manage.py test projects.tests_tenders_gantt --settings=solarpms.test_settings
"""
import dataclasses
from datetime import date, datetime, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.db import connection
from django.test import Client, SimpleTestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from .models import (PaymentRequest, Program, Project, Task, UserProfile, Vendor, VendorOrder,
                     VendorOrderSite)
from .tenders_gantt import (ACTUAL, DAY_PX, DUE_MARK, FORECAST, LATE, MANUAL_MARK, PLAN_LINE,
                            ZOOM_MONTH, ZOOM_WEEK, Piece, Timeline, build_gantt,
                            offset_days, offset_px, place, point, span, timeline_for,
                            track_px, _block_lane, Dates)
from .tests_sched2_scheduler import CIVIL, MODULE, NET_METERING, freeze_today
from .views_site_dashboard import RESCO_SEGMENTS
from .site_dashboard import SiteCard
from .tests_site_dashboard import DashFixture, _fresh

URL = '/dashboard/tenders/gantt/'


def _client(profile):
    client = Client()
    client.force_login(profile.user)
    return client


class GanttFixture(DashFixture):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.tender = Program.objects.create(name='Gantt Tender', program_type='OPEX',
                                            client_name='C', status='Active',
                                            short_tender_code='GNT')
        cls.tender2 = Program.objects.create(name='Second Tender', program_type='OPEX',
                                             client_name='C', status='Active',
                                             short_tender_code='SEC')

    def _get(self, profile, query=''):
        return _client(profile).get(URL + query)

    def _tsite(self, program=None, **kwargs):
        """A RESCO site activated through the real view, in a tender."""
        return self._site(program=program or self.tender, **kwargs)

    def _behind(self, days=3, **kwargs):
        """Scheduled, its last block `days` past its baseline (as tests_site_dashboard_view)."""
        site = self._tsite(civil_due=date(2026, 11, 10), **kwargs)
        self._apply(site)
        last = (Task.objects.filter(phase__project=site, baseline_due__isnull=False)
                .order_by('-baseline_due').first())
        Task.objects.filter(pk=last.pk).update(due_date=last.baseline_due + timedelta(days=days))
        return site

    @staticmethod
    def _rows(response):
        return response.context['rows'] + response.context['civil_rows']

    def _row(self, response, site):
        return next(r for r in self._rows(response) if r.id == site.pk)

    @staticmethod
    def _codes(rows):
        return [r.project_id for r in rows]

    @staticmethod
    def _lane(items, label):
        return next((lane, bars) for lane, bars in items if lane.label == label)

    @staticmethod
    def _kinds(bars):
        return [b.cls.split()[0] for b in bars]


# ---------------------------------------------------------------------------
# 1. Access and scope (G1, G8)
# ---------------------------------------------------------------------------

class AccessTests(GanttFixture):

    def test_url_name(self):
        self.assertEqual(reverse('tenders_gantt'), URL)

    def test_ceo_pm_and_design_head_open_the_page(self):
        for profile in (self.ceo, self.pm, self.head, self.coord, self.finance, self.admin):
            with self.subTest(role=profile.role, head=profile.is_design_head):
                self.assertEqual(self._get(profile).status_code, 200)

    def test_site_engineer_and_no_profile_get_403(self):
        self.assertEqual(self._get(self.se).status_code, 403)
        self.assertEqual(self._get(self.designer).status_code, 403)
        user = User.objects.create_user(username='gantt_noprofile', password='x')
        UserProfile.objects.filter(user=user).delete()
        client = Client()
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

    def test_pm_sees_only_their_sites(self):
        mine = self._tsite()
        theirs = self._tsite()
        Project.objects.filter(pk=theirs.pk).update(assigned_pm=self.other_pm)
        self.assertEqual(self._codes(self._rows(self._get(self.pm))), [mine.project_id])
        self.assertEqual(set(self._codes(self._rows(self._get(self.ceo)))),
                         {mine.project_id, theirs.project_id})

    def test_only_opex_sites_with_a_program_appear_and_drafts_are_grouped(self):
        shown = self._tsite()
        # A CAPEX site may only belong to a CAPEX Program (Project.clean); a Residential
        # site has none.
        capex_program = Program.objects.create(name='Capex Tender', program_type='CAPEX',
                                               client_name='C', status='Active',
                                               short_tender_code='CPX')
        hidden = [
            self._site(),                                                    # no Program
            self._bare(project_type='CAPEX', program=capex_program),
            self._bare(project_type='Residential'),
            self._bare(project_type='OPEX', program=self.tender, is_test=True),
            self._bare(project_type='OPEX', program=self.tender, status='Cancelled'),
        ]
        draft = self._bare(project_type='OPEX', program=self.tender, status='Draft')
        for profile in (self.ceo, self.pm, self.head):
            with self.subTest(role=profile.role, head=profile.is_design_head):
                response = self._get(profile)
                listed = self._codes(self._rows(response))
                drafts = [d.code for d in response.context['drafts']]
                self.assertEqual(listed, [shown.project_id])
                self.assertEqual(drafts, [draft.project_id])
                for site in hidden:
                    self.assertNotIn(site.project_id, listed + drafts)
        self.assertContains(self._get(self.ceo), 'id="drafts"')


# ---------------------------------------------------------------------------
# 2. Geometry (R1, R2)
# ---------------------------------------------------------------------------

class GeometryTests(SimpleTestCase):
    TL = Timeline(start=date(2026, 9, 1), end=date(2027, 1, 31))   # 153 days

    def test_a_date_maps_to_its_offset_at_both_zooms(self):
        day = date(2026, 11, 10)
        self.assertEqual(offset_days(self.TL, day), 70)
        self.assertEqual(offset_px(self.TL, day, ZOOM_MONTH), 70 * 5)
        self.assertEqual(offset_px(self.TL, day, ZOOM_WEEK), 70 * 18)
        self.assertEqual(track_px(self.TL, ZOOM_MONTH), 153 * DAY_PX[ZOOM_MONTH])
        self.assertEqual(track_px(self.TL, ZOOM_WEEK), 153 * DAY_PX[ZOOM_WEEK])
        # The percentage is the same at both zooms: the zoom only widens the track.
        left, width, _, _ = span(self.TL, day, day + timedelta(days=4))
        self.assertAlmostEqual(left / 100 * track_px(self.TL, ZOOM_WEEK),
                               offset_px(self.TL, day, ZOOM_WEEK))
        self.assertAlmostEqual(width / 100 * track_px(self.TL, ZOOM_MONTH), 5 * 5)
        self.assertEqual(place(Piece(ACTUAL, day, day), self.TL).left, '45.752')

    def test_width_is_never_negative(self):
        for start, end in ((date(2026, 11, 10), date(2026, 11, 1)),
                           (date(2026, 11, 10), date(2026, 11, 10))):
            _, width, _, _ = span(self.TL, start, end)
            self.assertGreater(width, 0)
            self.assertAlmostEqual(width, 100 / 153)

    def test_bars_outside_the_range_are_clipped_not_dropped(self):
        left, width, clip_start, clip_end = span(self.TL, date(2026, 8, 1), date(2026, 9, 10))
        self.assertEqual((left, clip_start, clip_end), (0, True, False))
        self.assertAlmostEqual(width, 10 / 153 * 100)
        left, width, clip_start, clip_end = span(self.TL, date(2027, 1, 20), date(2027, 3, 1))
        self.assertAlmostEqual(left + width, 100)
        self.assertEqual((clip_start, clip_end), (False, True))
        # Wholly before the range: one day at the left edge, flagged.
        drawn = place(Piece(ACTUAL, date(2020, 1, 1), date(2020, 2, 1)), self.TL)
        self.assertEqual((drawn.left, drawn.cls), ('0.000', 'a cs'))
        self.assertGreater(float(drawn.width), 0)
        drawn = place(Piece(DUE_MARK, date(2030, 1, 1)), self.TL)
        self.assertEqual(drawn.cls, 'dm cl')
        self.assertLess(float(drawn.left), 100)
        self.assertEqual(point(self.TL, date(2026, 9, 1))[1], False)

    def test_timeline_is_month_aligned_with_a_floor_and_a_ceiling(self):
        today = date(2026, 11, 10)
        tl = timeline_for([], today)
        self.assertEqual((tl.start, tl.end), (date(2026, 10, 1), date(2026, 12, 31)))
        self.assertLessEqual(tl.start, today - timedelta(days=30))
        self.assertGreaterEqual(tl.end, today + timedelta(days=45))
        tl = timeline_for([date(2026, 9, 16), date(2027, 1, 31), None], today)
        self.assertEqual((tl.start, tl.end), (date(2026, 9, 1), date(2027, 1, 31)))
        # A mistyped year never stretches it past 6 months back or 12 months on (R1).
        tl = timeline_for([date(20, 9, 17), date(2099, 1, 1)], today)
        self.assertEqual((tl.start, tl.end), (date(2026, 5, 1), date(2027, 11, 30)))

    def test_a_single_date_block_is_a_due_mark_not_a_bar(self):
        lane = _block_lane(3, 'Earthing & DC', Dates(n=1, open_n=1, plan_start=date(2026, 11, 20),
                                                       plan_end=date(2026, 11, 20), single=True,
                                                       manual=1), date(2026, 11, 9))
        self.assertEqual([p.kind for p in lane.pieces], [DUE_MARK, MANUAL_MARK])
        self.assertIsNone(lane.pieces[0].end)


# ---------------------------------------------------------------------------
# 3. The Execution lane and its blocks (G4, G5)
# ---------------------------------------------------------------------------

class ExecutionTests(GanttFixture):

    def _late_civil_site(self):
        """Scheduled on 9 Nov with Civil due 10 Nov and a PM-typed Module date; then seen
        on 13 Nov with Civil still open, so block 1 is late and the forecast moves the
        later blocks past today (SCHED-2b) against a baseline frozen since 10 Nov."""
        site = self._tsite(civil_due=date(2026, 11, 10))
        self._set_due(self._task(site, MODULE), date(2026, 11, 20))
        self._apply(site)
        return site

    def test_slip_tail_only_when_behind_and_only_on_the_execution_summary(self):
        on_plan = self._tsite(civil_due=date(2026, 11, 10))
        self._apply(on_plan)
        behind = self._behind(days=3)
        response = self._get(self.ceo)

        lane, bars = self._lane(self._row(response, on_plan).lanes, 'Execution')
        self.assertNotIn(LATE, self._kinds(bars))
        self.assertIn(PLAN_LINE, self._kinds(bars))

        row = self._row(response, behind)
        lane, bars = self._lane(row.lanes, 'Execution')
        self.assertEqual(self._kinds(bars).count(LATE), 1)
        self.assertIn('3 days behind plan', lane.text)
        tail = next(p for p in lane.pieces if p.kind == LATE)
        card = next(s.card for s in build_gantt(_fresh(self.ceo.user)).sites if s.card.id == behind.pk)
        self.assertEqual((tail.start, tail.end),
                         (card.schedule.baseline_finish + timedelta(days=1),
                          card.schedule.forecast_finish))
        # Nowhere else: no other collapsed lane, no block (no block is open past its date).
        for other, other_bars in row.lanes:
            if other.label != 'Execution':
                self.assertNotIn(LATE, self._kinds(other_bars), other.label)
        for other, other_bars in row.detail:
            if other.block:
                self.assertNotIn(LATE, self._kinds(other_bars), other.label)

    def test_only_the_open_block_past_its_due_date_is_red(self):
        site = self._late_civil_site()
        with freeze_today(date(2026, 11, 13)):
            self._apply(site)
            row = self._row(self._get(self.ceo), site)
        blocks = [(lane, bars) for lane, bars in row.detail if lane.block]
        self.assertEqual(len(blocks), 8)
        red = [lane.label for lane, bars in blocks if LATE in self._kinds(bars)]
        self.assertEqual(red, ['1. Civil & MMS'])
        civil, _ = blocks[0]
        late = next(p for p in civil.pieces if p.kind == LATE)
        self.assertEqual((late.start, late.end), (date(2026, 11, 11), date(2026, 11, 13)))
        self.assertTrue(civil.text.startswith('Late · 10 Nov → now'), civil.text)
        # Later blocks are shifted after their plan line, not red.
        for lane, bars in blocks[1:]:
            forecast = next((p for p in lane.pieces if p.kind == FORECAST), None)
            plan = next((p for p in lane.pieces if p.kind == PLAN_LINE), None)
            if forecast is not None and plan is not None:
                self.assertGreaterEqual(forecast.end, plan.end, lane.label)
                self.assertIn('plan', lane.text)
        # The left column says it all in words (G5).
        self.assertContains(self._get_at(site, date(2026, 11, 13)), 'Late · 10 Nov → now')

    def _get_at(self, site, day):
        with freeze_today(day):
            return self._get(self.ceo)

    def test_a_block_with_a_pm_typed_date_gets_the_m_marker(self):
        site = self._late_civil_site()
        row = self._row(self._get(self.ceo), site)
        marked = [lane.label for lane, bars in row.detail
                  if lane.block and MANUAL_MARK in self._kinds(bars)]
        self.assertEqual(marked, ['2. Modules'])
        modules = next(lane for lane, _ in row.detail if lane.label == '2. Modules')
        self.assertIn('PM date', modules.text)

    def test_show_plan_off_drops_the_plan_lines(self):
        site = self._behind()
        row = self._row(self._get(self.ceo, '?plan=off'), site)
        for lane, bars in row.detail:
            self.assertNotIn(PLAN_LINE, self._kinds(bars), lane.label)
        # The legend's swatch is the only plan line left on the page.
        self.assertContains(self._get(self.ceo, '?plan=off'), '<i class="pl"', count=1)
        self.assertGreater(self._get(self.ceo).content.decode().count('<i class="pl"'), 1)

    def test_week_zoom_widens_the_track_and_labels_weeks(self):
        self._tsite()
        month = self._get(self.ceo)
        week = self._get(self.ceo, '?zoom=week')
        tl = week.context['timeline']
        self.assertEqual(month.context['track_px'], tl.days * 5)
        self.assertEqual(week.context['track_px'], tl.days * 18)
        self.assertEqual(month.context['weeks'], [])
        self.assertTrue(week.context['weeks'])
        self.assertContains(week, 'class="wk"')
        self.assertEqual(self._get(self.ceo, '?zoom=nonsense').context['params']['zoom'], 'month')


# ---------------------------------------------------------------------------
# 4. Sites the scheduler does not plan (G7)
# ---------------------------------------------------------------------------

class UnscheduledTests(GanttFixture):

    def test_reason_labels_at_today(self):
        civil = self._tsite()
        capacity = self._tsite(dc=None, civil_due=date(2026, 11, 10))
        excluded = self._tsite(civil_due=date(2026, 11, 10), exclude_from_auto_schedule=True)
        response = self._get(self.ceo, '?group=off')
        for site, text in ((civil, 'Needs Civil date'), (capacity, 'Needs DC capacity'),
                           (excluded, 'Excluded from auto-schedule')):
            with self.subTest(text=text):
                lane, _ = self._lane(self._row(response, site).lanes, 'Execution')
                self.assertEqual(lane.note, text)
        self.assertContains(response, '<span class="note" style="left:', count=3)

    def test_excluded_site_draws_execution_from_its_location_copies(self):
        """UKRU001: the in-block tasks carry no dates, the location copies do."""
        site = self._tsite(exclude_from_auto_schedule=True)
        for code, label, due in ((CIVIL, 'Block A', date(2026, 10, 30)),
                                 (MODULE, 'Block A', date(2026, 11, 20))):
            copy = self._task(site, code)
            copy.pk = None
            copy.location_label = label
            copy.due_date = due
            copy.due_date_source = Task.DUE_DATE_MANUAL
            copy.save()
        started = Task.objects.get(phase__project=site, location_label='Block A',
                                   template_task__code=CIVIL)
        Task.objects.filter(pk=started.pk).update(status=Task.IN_PROGRESS)
        from .models import StatusTransition
        StatusTransition.objects.create(subject_type='task', subject_id=started.pk,
                                        from_status=Task.NOT_STARTED, to_status=Task.IN_PROGRESS,
                                        occurred_at=self._ist(2))
        row = self._row(self._get(self.ceo), site)
        lane, bars = self._lane(row.lanes, 'Execution')
        self.assertEqual(lane.note, 'Excluded from auto-schedule')
        actual = next(p for p in lane.pieces if p.kind == ACTUAL)
        self.assertEqual(actual.start, date(2026, 11, 2))
        self.assertIn('2 Nov → now', lane.text)
        blocks = {l.label: l for l, _ in row.detail if l.block}
        self.assertEqual(blocks['1. Civil & MMS'].pieces[0].kind, DUE_MARK)
        self.assertIn('Late · 30 Oct → now', blocks['1. Civil & MMS'].text)
        self.assertIn('due 20 Nov', blocks['2. Modules'].text)


class PhaseLaneTests(GanttFixture):

    def test_blocked_lane_is_hatched_and_a_single_pm_date_is_a_due_mark(self):
        """G6 and R3. The restore holds no Blocked task, so only this test shows it."""
        site = self._tsite()
        task = self._task(site, NET_METERING)
        self._set_due(task, date(2026, 11, 20))
        Task.objects.filter(pk=task.pk).update(status=Task.BLOCKED)
        label = dict(RESCO_SEGMENTS)[task.phase.phase_name]
        response = self._get(self.ceo, '?group=off')
        lane, bars = self._lane(self._row(response, site).lanes, label)
        self.assertTrue(lane.hatch)
        self.assertEqual(self._kinds(bars), [DUE_MARK])
        self.assertIn('due 20 Nov', lane.text)
        self.assertIn('Blocked', lane.text)
        self.assertContains(response, 'class="ln hx"')


# ---------------------------------------------------------------------------
# 5. Order, groups and tender cards (G8, G9)
# ---------------------------------------------------------------------------

class OrderTests(GanttFixture):

    def setUp(self):
        # Activation and the PM-typed Civil date log rows at the real clock, so the
        # staged activity sits after it: hour `day` after now, the larger the newer.
        now = timezone.now()
        t = lambda day: now + timedelta(hours=day)
        self.behind = self._behind(days=2)
        self._log(self.behind, '', t(1), actor=self.pm)
        self.fresh = self._tsite(civil_due=date(2026, 11, 10))
        self._apply(self.fresh)
        self._log(self.fresh, '', t(8), actor=self.pm)
        self.older = self._tsite(civil_due=date(2026, 11, 10))
        self._apply(self.older)
        self._log(self.older, '', t(5), actor=self.pm)
        self.civil = self._tsite()
        self._log(self.civil, '', t(7), actor=self.pm)
        self.draft_old = self._bare(project_type='OPEX', program=self.tender, status='Draft')
        self.draft_new = self._bare(project_type='OPEX', program=self.tender2, status='Draft')
        Project.objects.filter(pk=self.draft_old.pk).update(created_at=now - timedelta(days=2))

    def test_both_sorts_and_the_civil_group(self):
        latest = self._get(self.ceo)
        self.assertEqual(self._codes(latest.context['rows']),
                         [self.fresh.project_id, self.older.project_id, self.behind.project_id])
        self.assertEqual(self._codes(latest.context['civil_rows']), [self.civil.project_id])
        self.assertContains(latest, '<b class="num">1</b> site needs a Civil date')
        urgent = self._get(self.ceo, '?sort=urgent')
        self.assertEqual(self._codes(urgent.context['rows']),
                         [self.behind.project_id, self.fresh.project_id, self.older.project_id])
        # Drafts: newest first, whichever sort.
        for response in (latest, urgent):
            self.assertEqual([d.code for d in response.context['drafts']],
                             [self.draft_new.project_id, self.draft_old.project_id])
        # Running numbers continue into the group.
        self.assertEqual([r.number for r in self._rows(latest)], [1, 2, 3, 4])

    def test_group_toggle_off_puts_civil_sites_back_in_the_sort(self):
        response = self._get(self.ceo, '?group=off')
        self.assertEqual(response.context['civil_rows'], [])
        self.assertEqual(self._codes(response.context['rows']),
                         [self.fresh.project_id, self.civil.project_id,
                          self.older.project_id, self.behind.project_id])
        self.assertNotContains(response, 'id="civil-group"')
        self.assertFalse(response.context['group_toggle']['on'])
        self.assertNotIn('group', response.context['group_toggle']['href'])

    def test_tender_card_counts_equal_the_rows_shown_and_the_card_filters(self):
        response = self._get(self.ceo)
        cards = {t['card'].code: t for t in response.context['tender_cards']}
        self.assertEqual(set(cards), {'GNT', 'SEC'})
        for code, program in (('GNT', self.tender), ('SEC', self.tender2)):
            with self.subTest(tender=code):
                card = cards[code]['card']
                filtered = self._get(self.ceo, cards[code]['href'])
                self.assertEqual(filtered.context['params']['tender'], program.pk)
                self.assertEqual(card.active, len(self._rows(filtered)))
                self.assertEqual(card.draft, len(filtered.context['drafts']))
                self.assertEqual(card.civil, len(filtered.context['civil_rows']))
                self.assertEqual(card.behind, sum(1 for r in self._rows(filtered)
                                                  if r.schedule and r.schedule[1] == 'behind'))
                # A second click on the selected card clears the filter.
                again = next(t for t in filtered.context['tender_cards'] if t['card'].code == code)
                self.assertTrue(again['active'])
                self.assertNotIn('tender=', again['href'])
        gnt = cards['GNT']['card']
        self.assertEqual((gnt.active, gnt.draft, gnt.behind, gnt.civil), (4, 1, 1, 1))
        self.assertEqual(sum(b[2] for b in gnt.buckets), gnt.active + gnt.draft)

    def test_unknown_parameters_fall_back_to_defaults(self):
        for query in ('?sort=bogus', '?tender=abc', '?tender=999999', '?zoom=year',
                      '?plan=maybe', '?group=1', '?q=' + 'x' * 500):
            with self.subTest(query=query[:20]):
                response = self._get(self.ceo, query)
                self.assertEqual(response.status_code, 200)
                params = response.context['params']
                self.assertIn(params['sort'], ('latest', 'urgent'))
                self.assertIn(params['zoom'], ('month', 'week'))
                self.assertLessEqual(len(params['q']), 100)

    def test_search(self):
        Project.objects.filter(pk=self.older.pk).update(site_name='Rampur Girls College')
        response = self._get(self.ceo, '?q=rampur')
        self.assertEqual(self._codes(self._rows(response)), [self.older.project_id])
        self.assertEqual(response.context['drafts'], [])
        card = response.context['tender_cards'][0]['card']
        self.assertEqual((card.code, card.active, card.draft), ('GNT', 1, 0))


class GroupDefaultOpenTests(GanttFixture):
    """A collapsed group under an empty list looked like "no sites": the first group that
    holds sites renders open when everything above it is empty. group=off still wins."""

    CIVIL_OPEN = 'x-data="{ open: true }" id="civil-group"'
    CIVIL_SHUT = 'x-data="{ open: false }" id="civil-group"'
    DRAFTS_OPEN = 'x-data="{ open: true }" id="drafts"'
    DRAFTS_SHUT = 'x-data="{ open: false }" id="drafts"'

    def setUp(self):
        # The PM's only live site needs a Civil date; the scheduled site belongs to the
        # other PM, so the CEO and Admin have a main list and the PM does not. The PM's
        # one draft sits alone in the second tender.
        scheduled = self._tsite(civil_due=date(2026, 11, 10))
        self._apply(scheduled)
        Project.objects.filter(pk=scheduled.pk).update(assigned_pm=self.other_pm)
        self.civil = self._tsite()
        self.draft = self._bare(project_type='OPEX', program=self.tender2, status='Draft')

    def test_viewer_whose_only_sites_need_a_civil_date_sees_the_group_open(self):
        response = self._get(self.pm)
        self.assertEqual(response.context['rows'], [])
        self.assertEqual(self._codes(response.context['civil_rows']), [self.civil.project_id])
        self.assertTrue(response.context['civil_open'])
        self.assertFalse(response.context['drafts_open'])
        self.assertContains(response, self.CIVIL_OPEN)
        # Shown before Alpine starts: no x-cloak, and the button says it is expanded.
        self.assertContains(response, 'id="civil-rows" x-show="open">')
        self.assertContains(response, 'aria-expanded="true" aria-controls="civil-rows"')
        self.assertContains(response, self.DRAFTS_SHUT)

    def test_admin_and_ceo_defaults_are_unchanged(self):
        for profile in (self.admin, self.ceo):
            with self.subTest(role=profile.role):
                response = self._get(profile)
                self.assertNotEqual(response.context['rows'], [])
                self.assertFalse(response.context['civil_open'])
                self.assertFalse(response.context['drafts_open'])
                self.assertContains(response, self.CIVIL_SHUT)
                self.assertContains(response, 'id="civil-rows" x-show="open" x-cloak>')
                self.assertContains(response, self.DRAFTS_SHUT)
                self.assertContains(response, 'id="draft-rows" x-show="open" x-cloak>')

    def test_drafts_open_only_when_the_list_and_the_civil_group_are_empty(self):
        response = self._get(self.pm, f'?tender={self.tender2.pk}')
        self.assertEqual(response.context['rows'] + response.context['civil_rows'], [])
        self.assertTrue(response.context['drafts_open'])
        self.assertContains(response, self.DRAFTS_OPEN)
        self.assertContains(response, 'id="draft-rows" x-show="open">')

    def test_group_off_still_wins(self):
        # Ungrouped, the Civil-date site is back in the main list, so nothing opens.
        response = self._get(self.pm, '?group=off')
        self.assertEqual(self._codes(response.context['rows']), [self.civil.project_id])
        self.assertFalse(response.context['civil_open'])
        self.assertFalse(response.context['drafts_open'])
        self.assertNotContains(response, 'id="civil-group"')
        self.assertContains(response, self.DRAFTS_SHUT)


# ---------------------------------------------------------------------------
# 6. Money, escaping, queries (G12, R4)
# ---------------------------------------------------------------------------

class MoneyAndEscapingTests(GanttFixture):

    def test_no_payment_field_in_the_context_and_no_figure_in_the_page(self):
        site = self._tsite()
        vendor = Vendor.objects.create(name='Gantt Vendor')
        order = VendorOrder.objects.create(vendor=vendor, project_type='OPEX', created_by=self.scm,
                                           total_amount=Decimal('987654'))
        VendorOrderSite.objects.create(order=order, project=site)
        PaymentRequest.objects.create(project=site, vendor=vendor, vendor_order=order,
                                      amount=Decimal('987654'), requested_by=self.scm.user,
                                      status=PaymentRequest.PENDING_APPROVAL)
        # The card behind the page does carry it for CEO: the page must drop it.
        card = next(s.card for s in build_gantt(_fresh(self.ceo.user)).sites if s.card.id == site.pk)
        self.assertEqual(card.payment_open, Decimal('987654'))

        def names(value, seen):
            # A SiteCard anywhere in the context would put payment_open within the
            # template's reach, so its presence alone fails the test.
            self.assertNotIsInstance(value, SiteCard)
            if dataclasses.is_dataclass(value) and not isinstance(value, type):
                for f in dataclasses.fields(value):
                    seen.add(f.name)
                    names(getattr(value, f.name), seen)
            elif isinstance(value, dict):
                for key, item in value.items():
                    seen.add(str(key))
                    names(item, seen)
            elif isinstance(value, (list, tuple)):
                for item in value:
                    names(item, seen)
            return seen

        for profile in (self.ceo, self.finance, self.pm, self.head):
            with self.subTest(role=profile.role, head=profile.is_design_head):
                response = self._get(profile)
                keys = {'rows', 'civil_rows', 'drafts', 'tender_cards', 'params'}
                seen = set()
                for key in keys:
                    names(response.context[key], seen)
                self.assertFalse([n for n in seen if 'payment' in n.lower()], sorted(seen))
                page = response.content.decode()
                body = page[page.index('<div class="tg"'):]
                self.assertNotIn('987', body)
                self.assertNotIn('₹', body)
                self.assertNotIn('payment', body.lower())

    def test_a_site_name_holding_script_is_escaped(self):
        self._tsite(site_name='<script>alert(1)</script>')
        response = self._get(self.ceo)
        self.assertContains(response, '&lt;script&gt;alert(1)&lt;/script&gt;')
        self.assertNotContains(response, '<script>alert(1)</script>')


class QueryBudgetTests(GanttFixture):

    def _populate(self, n):
        t0 = timezone.make_aware(datetime(2026, 11, 2, 9))
        for _ in range(n):
            site = self._bare(project_type='OPEX', program=self.tender)
            phase = self._phase(site, 'Design', 1)
            self._plain_task(phase, 1, task_type=Task.INTERNAL, due_date=FROZEN_DUE)
            self._plain_task(phase, 2, task_type=Task.INTERNAL, status=Task.DONE,
                             completed_at=t0)
            self._log(site, 'task_status_done', t0, actor=self.pm)
        self._bare(project_type='OPEX', program=self.tender2, status='Draft')

    def _count(self, client):
        with CaptureQueriesContext(connection) as captured:
            response = client.get(URL)
        self.assertEqual(response.status_code, 200)
        return len(captured.captured_queries), len(self._rows(response))

    def test_the_same_number_of_queries_for_3_sites_and_30_sites(self):
        self._behind()                                    # one scheduled site in both runs
        self._populate(2)
        clients = {p.pk: _client(p) for p in (self.ceo, self.pm, self.head)}
        counts = {}
        for profile in (self.ceo, self.pm, self.head):
            clients[profile.pk].get(URL)                  # warm the session
            counts[profile.pk], sites = self._count(clients[profile.pk])
            self.assertEqual(sites, 3)
        self._populate(27)
        for profile in (self.ceo, self.pm, self.head):
            with self.subTest(role=profile.role, head=profile.is_design_head):
                n, sites = self._count(clients[profile.pk])
                self.assertEqual(sites, 30)
                self.assertEqual(n, counts[profile.pk])


FROZEN_DUE = date(2026, 11, 20)
