"""
S5 — tender_stages.design_throughput: the CEO Tenders "Design throughput" card.

THE RULE: every figure is the Design Head's own. Rework is rework_contribution(), the
median is m_cycle_time(), past due is is_overdue(); the tests below pin the behaviour and
then (h) holds each shared figure equal to design_metrics / design_analytics on one fixture.

Real fixtures throughout: every assertion reads rows written to the test database.

    python manage.py test projects.tests_design_throughput --settings=solarpms.test_settings
"""
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.contrib.auth.models import User
from django.db import connection
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from .design_analytics import (
    analytics_dataset, m_cycle_time, m_hold_rate, m_rework_multiplier,
)
from .design_metrics import tender_metrics
from .models import (
    ATTEMPT_REASON_INITIAL, ATTEMPT_REASON_PM_CHANGE_REQUEST, ATTEMPT_REASON_QC_FAILED,
    CHANGE_REQUEST_ACCEPTED, CHANGE_REQUEST_CORRECTED, CHANGE_REQUEST_ORIGIN_PM,
    CHANGE_REQUEST_ORIGIN_SCM, CHANGE_REQUEST_PENDING, CHANGE_REQUEST_PM_REJECTED,
    CHANGE_REQUEST_REJECTED, CHANGE_REQUEST_WITH_PM, CHANGE_REQUEST_WITHDRAWN,
    DESIGN_AWAITING_PM_APPROVAL, DESIGN_IN_DESIGN, DESIGN_IN_QC, DESIGN_PM_REJECTED,
    DESIGN_RELEASED, DESIGN_SURVEY_RETURNED, ERR_LAYOUT, ERR_SURVEY_INADEQUATE,
    QC_FAILED, QC_PASSED,
    DesignAssignment, DesignAttempt, DesignChangeRequest, DueDateCommitment, Program,
    Project,
)
from .tender_stages import design_throughput
from .views import tender_sites_qs

IST = ZoneInfo('Asia/Kolkata')
TODAY = date(2026, 9, 29)          # a Tuesday; its week began Monday 28 Sep


def _ist(y, m, d, hour=12, minute=0):
    return datetime(y, m, d, hour, minute, tzinfo=IST)


def _profile(username, role):
    user = User.objects.create_user(username=username, password='x')
    profile = user.profile          # auto-created by the post_save signal
    profile.role = role
    profile.save(update_fields=['role'])
    return profile


def _program(name, code, program_type='OPEX'):
    return Program.objects.create(name=name, program_type=program_type, client_name='C',
                                  status='Active', short_tender_code=code)


def _design(program, project_id, status, designer=None, site=None, **fields):
    """One site with a DesignAssignment at `status`. `site` carries Project overrides
    (is_test, project_type, status). Returns the assignment."""
    site = dict(site or {})
    site.setdefault('project_type', 'OPEX')
    site.setdefault('status', 'Draft')
    project = Project.objects.create(
        project_id=project_id, customer_name=f'Customer {project_id}',
        customer_phone='9876543210', site_address='1 Sun Road', city='Lucknow',
        program=program, dc_capacity_kw=Decimal('10.00'),
        target_commissioning_date=date.today() + timedelta(days=90), **site)
    return DesignAssignment.objects.create(project=project, status=status,
                                           assigned_to=designer, **fields)


def _attempt(assignment, number, reason=ATTEMPT_REASON_INITIAL, **fields):
    attempt = DesignAttempt.objects.create(assignment=assignment, attempt_number=number,
                                           opened_reason=reason, **fields)
    if number > assignment.current_attempt_number:
        assignment.current_attempt_number = number
        assignment.save(update_fields=['current_attempt_number'])
    return attempt


def _commit(assignment, on, by, approved=True, current=True):
    return DueDateCommitment.objects.create(
        assignment=assignment, proposed_date=on, proposed_by=by, is_current=current,
        approved_by=by if approved else None,
        approved_at=timezone.now() if approved else None)


def _cr(attempt, origin, verdict, by, **fields):
    return DesignChangeRequest.objects.create(attempt=attempt, requested_by=by,
                                              reason='Change it', origin=origin,
                                              verdict=verdict, **fields)


def _failed_qc(category):
    return {'qc_verdict': QC_FAILED, 'qc_remarks': 'Wrong', 'qc_failure_category': category}


# ---------------------------------------------------------------------------
# Builders — each lays down one metric's cases in a programme, so (h) can put all of
# them in ONE programme and compare every figure against the Head's.
# ---------------------------------------------------------------------------

#: Allocation Monday 3 Aug 2026 10:00 IST. (release date, working days, calendar days):
#: 8 Aug is the 2nd Saturday and 9 Aug a Sunday, so working and calendar days part.
CYCLE_ALLOCATED = _ist(2026, 8, 3, 10)
CYCLE_RELEASES = [
    (date(2026, 8, 4), 1, 1),
    (date(2026, 8, 5), 2, 2),
    (date(2026, 8, 10), 5, 7),
    (date(2026, 8, 11), 6, 8),
    (date(2026, 8, 12), 7, 9),
]


def build_cycle(program, prefix, designer, count, site=None):
    for i, (released_on, _, _) in enumerate(CYCLE_RELEASES[:count]):
        _design(program, f'{prefix}-CY{i}', DESIGN_RELEASED, designer, site,
                assigned_at=CYCLE_ALLOCATED,
                released_at=_ist(released_on.year, released_on.month, released_on.day))


def build_rework(program, prefix, designer, requester, site=None):
    """Five finished sites and one unfinished one. Designer loops: R1 (Group A) and R5
    (uncategorised) — 2 on 5 finished sites. R1 and R6 also carry an attempt opened by an
    accepted change request (PM and SCM), and R2 a Group B loop: none of those count."""
    r1 = _design(program, f'{prefix}-R1', DESIGN_RELEASED, designer, site)
    _attempt(r1, 1, **_failed_qc(ERR_LAYOUT))
    second = _attempt(r1, 2, ATTEMPT_REASON_QC_FAILED)
    third = _attempt(r1, 3, ATTEMPT_REASON_PM_CHANGE_REQUEST)
    _cr(second, CHANGE_REQUEST_ORIGIN_PM, CHANGE_REQUEST_ACCEPTED, requester,
        resulting_attempt=third)

    r2 = _design(program, f'{prefix}-R2', DESIGN_AWAITING_PM_APPROVAL, designer, site)
    _attempt(r2, 1, qc_verdict=QC_PASSED, head_verdict=QC_FAILED, head_remarks='Survey',
             head_failure_category=ERR_SURVEY_INADEQUATE)
    _attempt(r2, 2, ATTEMPT_REASON_QC_FAILED)

    r3 = _design(program, f'{prefix}-R3', DESIGN_RELEASED, designer, site)
    _attempt(r3, 1)

    r4 = _design(program, f'{prefix}-R4', DESIGN_IN_DESIGN, designer, site)
    _attempt(r4, 1, **_failed_qc(ERR_LAYOUT))
    _attempt(r4, 2, ATTEMPT_REASON_QC_FAILED)

    r5 = _design(program, f'{prefix}-R5', DESIGN_RELEASED, designer, site)
    _attempt(r5, 1, **_failed_qc(''))
    _attempt(r5, 2, ATTEMPT_REASON_QC_FAILED)

    r6 = _design(program, f'{prefix}-R6', DESIGN_RELEASED, designer, site)
    first = _attempt(r6, 1)
    reopened = _attempt(r6, 2, ATTEMPT_REASON_PM_CHANGE_REQUEST)
    _cr(first, CHANGE_REQUEST_ORIGIN_SCM, CHANGE_REQUEST_ACCEPTED, requester,
        resulting_attempt=reopened)


def build_crs(program, prefix, designer, pm, scm, site=None):
    """Three open requests — PM pending, SCM with the PM, SCM forwarded to the Head — and
    one of every closed verdict, which must not count."""
    now = timezone.now()
    c1 = _design(program, f'{prefix}-C1', DESIGN_IN_QC, designer, site)
    _cr(_attempt(c1, 1), CHANGE_REQUEST_ORIGIN_PM, CHANGE_REQUEST_PENDING, pm)

    c2 = _design(program, f'{prefix}-C2', DESIGN_RELEASED, designer, site)
    _cr(_attempt(c2, 1), CHANGE_REQUEST_ORIGIN_SCM, CHANGE_REQUEST_WITH_PM, scm)

    c3 = _design(program, f'{prefix}-C3', DESIGN_RELEASED, designer, site)
    _cr(_attempt(c3, 1), CHANGE_REQUEST_ORIGIN_SCM, CHANGE_REQUEST_PENDING, scm,
        pm_decided_by=pm, pm_decided_at=now, pm_note='Forwarded')

    c4 = _design(program, f'{prefix}-C4', DESIGN_RELEASED, designer, site)
    closed = _attempt(c4, 1)
    _cr(closed, CHANGE_REQUEST_ORIGIN_PM, CHANGE_REQUEST_ACCEPTED, pm)
    _cr(closed, CHANGE_REQUEST_ORIGIN_PM, CHANGE_REQUEST_REJECTED, pm,
        rejection_reason='Stands')
    _cr(closed, CHANGE_REQUEST_ORIGIN_SCM, CHANGE_REQUEST_PM_REJECTED, scm,
        pm_decided_by=pm, pm_decided_at=now, pm_note='No')
    _cr(closed, CHANGE_REQUEST_ORIGIN_SCM, CHANGE_REQUEST_WITHDRAWN, scm,
        withdrawn_by=scm, withdrawn_at=now, withdrawal_note='Mine')
    _cr(closed, CHANGE_REQUEST_ORIGIN_SCM, CHANGE_REQUEST_CORRECTED, scm,
        corrected_by=designer, corrected_at=now, correction_note='Fixed in BOQ')


def build_due(program, prefix, designer, today, site=None):
    """Hold 1 (H1); past due 4 (H1, P1, P6, P8). P2 is due today, P3 released, P4 with the
    PM, P5 PM-rejected, P7 only proposed — none of those is past due."""
    past, future = today - timedelta(days=5), today + timedelta(days=5)

    h1 = _design(program, f'{prefix}-H1', DESIGN_SURVEY_RETURNED, designer, site)
    _commit(h1, past, designer)
    _commit(_design(program, f'{prefix}-P1', DESIGN_IN_DESIGN, designer, site),
            today - timedelta(days=1), designer)
    _commit(_design(program, f'{prefix}-P2', DESIGN_IN_DESIGN, designer, site),
            today, designer)
    _commit(_design(program, f'{prefix}-P3', DESIGN_RELEASED, designer, site),
            past, designer)
    _commit(_design(program, f'{prefix}-P4', DESIGN_AWAITING_PM_APPROVAL, designer, site),
            past, designer)
    _commit(_design(program, f'{prefix}-P5', DESIGN_PM_REJECTED, designer, site),
            past, designer)
    # An extension requested after the date passed: current but unapproved, so the
    # approved date still governs.
    p6 = _design(program, f'{prefix}-P6', DESIGN_IN_DESIGN, designer, site)
    _commit(p6, past, designer, current=False)
    _commit(p6, future, designer, approved=False)
    _commit(_design(program, f'{prefix}-P7', DESIGN_IN_DESIGN, designer, site),
            past, designer, approved=False)
    _commit(_design(program, f'{prefix}-P8', DESIGN_IN_QC, designer, site),
            past, designer)


class People(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.designer = _profile('s5_designer', 'Design')
        cls.pm = _profile('s5_pm', 'PM')
        cls.scm = _profile('s5_scm', 'SCM')
        cls.tender = _program('S5 Tender', 'STP')

    def _dt(self, today=TODAY):
        return design_throughput(tender_sites_qs(), today)


class WindowTests(People):
    """a) the week (Mon-Sun) and month are IST calendar windows over strict releases."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.monday_0030 = _ist(2026, 9, 28, 0, 30)
        for project_id, released_at in (
                ('W-MON-0030', cls.monday_0030),
                ('W-SUN-2330', _ist(2026, 9, 27, 23, 30)),
                ('W-SEP-01', _ist(2026, 9, 1, 0, 30)),
                ('W-AUG-31', _ist(2026, 8, 31, 23, 30))):
            _design(cls.tender, project_id, DESIGN_RELEASED, cls.designer,
                    released_at=released_at)
        # With the PM, not released: never counted, whatever the stamps say.
        _design(cls.tender, 'W-PM', DESIGN_AWAITING_PM_APPROVAL, cls.designer)

    def test_monday_0030_ist_is_the_new_week(self):
        # 00:30 IST Monday is still Sunday in UTC; the IST date is what counts.
        self.assertEqual(self.monday_0030.astimezone(ZoneInfo('UTC')).date(),
                         date(2026, 9, 27))
        result = self._dt()
        self.assertEqual(result['week_start'], date(2026, 9, 28))
        self.assertEqual(result['released_week'], 1)
        self.assertEqual(result['released_month'], 3)

    def test_sunday_is_the_last_day_and_month_turns_on_the_1st(self):
        sunday = self._dt(date(2026, 10, 4))
        self.assertEqual(sunday['released_week'], 1)
        self.assertEqual(sunday['released_month'], 0)
        next_monday = self._dt(date(2026, 10, 5))
        self.assertEqual(next_monday['released_week'], 0)
        august = self._dt(date(2026, 8, 31))
        self.assertEqual(august['released_month'], 1)


class CycleTimeTests(People):
    """b) the median is m_cycle_time: working days, all time, MIN_DENOMINATOR rule."""

    def test_four_releases_are_too_few_to_judge(self):
        build_cycle(self.tender, 'CY', self.designer, 4)
        result = self._dt()
        self.assertTrue(result['cycle_insufficient'])
        self.assertEqual(result['cycle']['n'], 4)

    def test_five_releases_give_the_working_day_median(self):
        build_cycle(self.tender, 'CY', self.designer, 5)
        result = self._dt()
        self.assertFalse(result['cycle_insufficient'])
        self.assertEqual(result['cycle']['n'], 5)
        # Working days [1, 2, 5, 6, 7]; the calendar median would be 7.
        self.assertEqual(result['cycle']['median'], 5)

    def test_all_time_not_ninety_days(self):
        build_cycle(self.tender, 'CY', self.designer, 5)
        _design(self.tender, 'CY-OLD', DESIGN_RELEASED, self.designer,
                assigned_at=_ist(2026, 1, 5), released_at=_ist(2026, 1, 6))
        result = self._dt()
        self.assertEqual(result['cycle']['n'], 6)
        self.assertEqual(result['cycle']['median'], 3.5)   # [1, 1, 2, 5, 6, 7]

    def test_no_release_is_n_zero(self):
        _design(self.tender, 'CY-OPEN', DESIGN_IN_DESIGN, self.designer,
                assigned_at=CYCLE_ALLOCATED)
        result = self._dt()
        self.assertTrue(result['cycle_insufficient'])
        self.assertEqual(result['cycle']['n'], 0)
        self.assertIsNone(result['cycle']['median'])


class ReworkTests(People):
    """c) designer rework loops on finished sites; a change request never counts."""

    def test_designer_loops_on_finished_sites(self):
        build_rework(self.tender, 'RW', self.designer, self.pm)
        result = self._dt()
        self.assertEqual(result['rework_loops'], 2)
        self.assertEqual(result['finished_sites'], 5)
        # Both change-request attempts sit on finished sites and still add nothing.
        self.assertEqual(DesignAttempt.objects.filter(
            opened_reason=ATTEMPT_REASON_PM_CHANGE_REQUEST,
            assignment__status__in=[DESIGN_RELEASED]).count(), 2)

    def test_change_request_attempts_alone_are_no_rework(self):
        site = _design(self.tender, 'RW-CR', DESIGN_RELEASED, self.designer)
        first = _attempt(site, 1)
        for number in (2, 3):
            reopened = _attempt(site, number, ATTEMPT_REASON_PM_CHANGE_REQUEST)
            _cr(first, CHANGE_REQUEST_ORIGIN_PM, CHANGE_REQUEST_ACCEPTED, self.pm,
                resulting_attempt=reopened)
            first = reopened
        result = self._dt()
        self.assertEqual((result['rework_loops'], result['finished_sites']), (0, 1))

    def test_no_finished_site(self):
        site = _design(self.tender, 'RW-OPEN', DESIGN_IN_DESIGN, self.designer)
        _attempt(site, 1, **_failed_qc(ERR_LAYOUT))
        _attempt(site, 2, ATTEMPT_REASON_QC_FAILED)
        result = self._dt()
        self.assertEqual((result['rework_loops'], result['finished_sites']), (0, 0))


class ChangeRequestTests(People):
    """d) open = with the PM + pending, split by origin; closed verdicts excluded."""

    def test_open_split(self):
        build_crs(self.tender, 'CR', self.designer, self.pm, self.scm)
        self.assertEqual(self._dt()['change_requests'],
                         {'open': 3, 'pm': 1, 'scm': 2, 'with_pm': 1, 'with_head': 2})

    def test_only_closed_requests_is_zero(self):
        site = _design(self.tender, 'CR-X', DESIGN_RELEASED, self.designer)
        _cr(_attempt(site, 1), CHANGE_REQUEST_ORIGIN_PM, CHANGE_REQUEST_REJECTED, self.pm,
            rejection_reason='Stands')
        self.assertEqual(self._dt()['change_requests']['open'], 0)


class HoldAndPastDueTests(People):
    """e) hold is survey_returned; past due is is_overdue() with no stage filter."""

    def test_counts(self):
        build_due(self.tender, 'DU', self.designer, TODAY)
        result = self._dt()
        self.assertEqual(result['on_hold'], 1)
        self.assertEqual(result['past_due'], 4)

    def test_released_site_with_past_date_is_not_past_due(self):
        site = _design(self.tender, 'DU-REL', DESIGN_RELEASED, self.designer)
        _commit(site, TODAY - timedelta(days=30), self.designer)
        self.assertEqual(self._dt()['past_due'], 0)


class ScopeTests(People):
    """f) test sites, CAPEX sites and non-live sites never count."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        test_tender = _program('S5 Test tender', 'STT')
        capex = _program('S5 Capex', 'SCX', program_type='CAPEX')
        for program, prefix, site in (
                (test_tender, 'TST', {'is_test': True}),
                (cls.tender, 'TSI', {'is_test': True}),        # test site, real tender
                (capex, 'CPX', {'project_type': 'CAPEX'}),
                (cls.tender, 'CAN', {'status': 'Cancelled'})):
            build_cycle(program, prefix, cls.designer, 5, site)
            build_rework(program, prefix, cls.designer, cls.pm, site)
            build_crs(program, prefix, cls.designer, cls.pm, cls.scm, site)
            build_due(program, prefix, cls.designer, TODAY, site)
            _design(program, f'{prefix}-WK', DESIGN_RELEASED, cls.designer, site,
                    released_at=_ist(2026, 9, 28, 9))

    def test_nothing_counts(self):
        result = self._dt()
        self.assertEqual(
            (result['released_week'], result['released_month'], result['cycle']['n'],
             result['rework_loops'], result['finished_sites'],
             result['change_requests']['open'], result['on_hold'], result['past_due']),
            (0, 0, 0, 0, 0, 0, 0, 0))

    def test_live_sites_do_count(self):
        # Proof the builders are not inert: the same rows on live sites are counted.
        build_due(self.tender, 'LIVE', self.designer, TODAY)
        result = self._dt()
        self.assertEqual((result['on_hold'], result['past_due']), (1, 4))


class QueryBudgetTests(People):
    """g) 3 sites in 1 programme cost what 30 sites in 3 programmes cost: four queries."""

    def _populate(self, prefix, programmes, sites):
        for p in range(programmes):
            program = _program(f'{prefix} tender {p}', f'{prefix}{p}')
            for i in range(sites):
                status = DESIGN_RELEASED if i % 3 == 0 else DESIGN_IN_DESIGN
                site = _design(program, f'{prefix}{p}-{i:02d}', status, self.designer,
                               assigned_at=CYCLE_ALLOCATED,
                               released_at=_ist(2026, 8, 10) if status == DESIGN_RELEASED else None)
                attempt = _attempt(site, 1, **_failed_qc(ERR_LAYOUT))
                _attempt(site, 2, ATTEMPT_REASON_QC_FAILED)
                _commit(site, TODAY - timedelta(days=2), self.designer)
                if i % 2:
                    _cr(attempt, CHANGE_REQUEST_ORIGIN_SCM, CHANGE_REQUEST_WITH_PM, self.scm)

    def _count(self, prefix):
        qs = Project.objects.filter(project_id__startswith=prefix)
        with CaptureQueriesContext(connection) as queries:
            result = design_throughput(qs, TODAY)
        return len(queries), result

    def test_query_count_is_fixed(self):
        self._populate('QA', 1, 3)
        self._populate('QB', 3, 10)
        small, small_result = self._count('QA')
        large, large_result = self._count('QB')
        self.assertEqual((small, large), (4, 4))
        # And the reads covered the rows: every site is in a figure.
        self.assertEqual(small_result['past_due'], 2)
        self.assertEqual(large_result['past_due'], 18)
        self.assertEqual(large_result['cycle']['n'], 12)


class AgreementTests(People):
    """h) every shared figure equals the Design Head's on the same fixture: tender_metrics
    (his tender dashboard) and design_analytics (his quality analytics)."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        build_cycle(cls.tender, 'AG', cls.designer, 5)
        _design(cls.tender, 'AG-OLD', DESIGN_RELEASED, cls.designer,
                assigned_at=_ist(2026, 1, 5), released_at=_ist(2026, 1, 6))
        build_rework(cls.tender, 'AG', cls.designer, cls.pm)
        build_crs(cls.tender, 'AG', cls.designer, cls.pm, cls.scm)
        build_due(cls.tender, 'AG', cls.designer, TODAY)

    def setUp(self):
        self.result = self._dt()
        self.head = tender_metrics(self.tender, TODAY)
        self.analytics = analytics_dataset([self.tender])

    def test_median_equals_m_cycle_time(self):
        self.assertEqual(self.result['cycle'], m_cycle_time(self.analytics)['team'])
        self.assertEqual(self.result['cycle']['n'], 6)

    def test_rework_equals_the_head_rework_column_and_analytics(self):
        workload = self.head['workload']
        self.assertEqual(self.result['rework_loops'],
                         sum(row['rework_attempts'] for row in workload))
        self.assertEqual(self.result['finished_sites'],
                         sum(row['finished'] for row in workload))
        rows = m_rework_multiplier(self.analytics)['rows']
        self.assertEqual(self.result['rework_loops'],
                         sum(row['figure']['numerator'] for row in rows))
        self.assertEqual(self.result['finished_sites'],
                         sum(row['figure']['n'] for row in rows))
        self.assertEqual(self.result['rework_loops'], 2)

    def test_past_due_equals_the_head_overdue(self):
        self.assertEqual(self.result['past_due'],
                         sum(1 for s in self.head['sites'] if s['overdue']))
        self.assertEqual(self.result['past_due'], 4)

    def test_hold_equals_the_head_blocked(self):
        self.assertEqual(self.result['on_hold'],
                         sum(1 for s in self.head['sites'] if s['blocked']))
        self.assertEqual(self.result['on_hold'],
                         m_hold_rate(self.analytics)['currently_held'])
        self.assertEqual(self.result['on_hold'], 1)

    def test_with_head_equals_the_head_queue(self):
        crs = self.result['change_requests']
        self.assertEqual(crs['with_head'], len(self.head['change_requests']))
        self.assertEqual(crs['open'], crs['with_head'] + crs['with_pm'])
        self.assertEqual(crs['with_head'], 2)


CARD_LABEL = '<div class="section-label">Design throughput</div>'
FOOTER = ('Rework and change requests are counted separately. Change requests are not '
          'counted against the designer.')


class RenderTests(People):
    """T2) the card renders under Tenders only, with the confirmed text."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.ceo = _profile('s5_ceo', 'CEO')
        build_crs(cls.tender, 'RN', cls.designer, cls.pm, cls.scm)
        build_due(cls.tender, 'RN', cls.designer, timezone.localdate())

    def setUp(self):
        self.client = Client(SERVER_NAME='localhost')
        self.client.force_login(self.ceo.user)

    def test_tenders_card_text(self):
        page = self.client.get(reverse('dashboard_ceo'), {'context': 'tenders'})
        self.assertContains(page, CARD_LABEL, count=1)
        self.assertContains(page, 'Released this week')
        self.assertContains(page, 'Released this month')
        self.assertContains(page, 'Median allocation → release (working days)')
        self.assertContains(page, 'n = 0, too few to judge')
        # C2, C3, C4 released; P3 released; P4 with the PM: five finished, no loops.
        self.assertContains(page, '0 designer rework loops on 5 finished sites')
        self.assertContains(page, '3 open · PM 1 · SCM 2')
        self.assertContains(page, '1 with the PM · 2 with the Design Head')
        # Sliced to the card: other sections of the page use the same amber class.
        content = page.content.decode()
        card = content[content.index(CARD_LABEL):content.index(FOOTER)]
        self.assertIn('<span>On Design Hold</span>\n          '
                      '<span class="stat-num amber">1</span>', card)
        self.assertIn('<span>Past agreed design due date</span>\n          '
                      '<span class="stat-num amber">4</span>', card)
        self.assertContains(page, 'Past agreed design due date')
        self.assertContains(page, FOOTER)
        self.assertContains(page, '<div class="col-12 col-lg-6">\n    ' + CARD_LABEL)
        # The multi-line template comment must not leak onto the page.
        self.assertNotContains(page, 'nothing is derived here')

    def test_residential_and_no_context_have_no_card(self):
        for params in ({'context': 'residential'}, {}):
            page = self.client.get(reverse('dashboard_ceo'), params)
            self.assertNotContains(page, CARD_LABEL)
            self.assertIsNone(page.context['design_throughput'])


class EmptyRenderTests(People):
    """No finished site reads "—", and zero hold / past due stay the neutral colour."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.ceo = _profile('s5_ceo_empty', 'CEO')
        _design(cls.tender, 'EM-1', DESIGN_IN_DESIGN, cls.designer)

    def test_dashes_and_neutral_zeros(self):
        client = Client(SERVER_NAME='localhost')
        client.force_login(self.ceo.user)
        page = client.get(reverse('dashboard_ceo'), {'context': 'tenders'})
        content = page.content.decode()
        card = content[content.index(CARD_LABEL):content.index(FOOTER)]
        self.assertIn('<span class="fw-semibold text-end">—</span>', card)
        self.assertNotIn('stat-num amber', card)
        self.assertEqual(card.count('<span class="stat-num dark">0</span>'), 2)
        self.assertIn('0 open · PM 0 · SCM 0', card)
