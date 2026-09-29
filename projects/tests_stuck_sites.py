"""
S7 — tender_stages.stuck_sites: the CEO Tenders "Stuck sites" section.

THE RULE: where the app already defines "late" for a stage, that definition is used —
design_metrics.is_overdue for S2/S3, the CEO page's own Blocked Tasks / Tasks terms for S7.
STUCK_LIMITS holds our own limits for the rest (S1, S4, S5, S6, and a PM-rejected package).
Every limit is >=, overshoot is days - limit, S0 is never stuck.

Real fixtures throughout: every assertion reads rows written to the test database. The
stage tests pass a fixed TODAY and cutoff so every boundary is exact; the page and
agreement tests use the real clock with days of margin either side.

    python manage.py test projects.tests_stuck_sites --settings=solarpms.test_settings
"""
import html
import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.contrib.auth.models import User
from django.db import connection
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from .design_metrics import tender_metrics
from .models import (
    ARKA_APPROVED, DESIGN_ALLOCATED, DESIGN_ARKA_SUBMITTED, DESIGN_ARTIFACTS_UPLOADED,
    DESIGN_AWAITING_ALLOCATION, DESIGN_AWAITING_PM_APPROVAL, DESIGN_AWAITING_SURVEY,
    DESIGN_IN_DESIGN, DESIGN_IN_QC, DESIGN_PM_REJECTED, DESIGN_RELEASED,
    DESIGN_SURVEY_RETURNED, SITE_GROUP_LOCKED, SUBJECT_DESIGN_ASSIGNMENT,
    ArkaSubmission, DesignAssignment, DesignAttempt, DueDateCommitment, Program, Project,
    ProjectPhase, SiteGroup, SiteGroupMembership, StatusTransition, Task, Vendor,
    VendorOrder, VendorOrderSite,
)
from .tender_stages import (
    STAGE_ACTIVATED, STAGE_AWAITING_PM, STAGE_IN_DESIGN, STAGE_IN_QC, STAGE_LOCKED_GROUP,
    STAGE_NO_SURVEY, STAGE_RELEASED, STAGE_SURVEY_ON_FILE, STAGES, STUCK_LIMITS,
    stuck_limit_lines, stuck_sites,
)
from .views import CONTEXT_TENDERS, _get_ceo_dashboard_context, tender_sites_qs

IST = ZoneInfo('Asia/Kolkata')
TODAY = date(2026, 9, 29)
# Noon IST seven days before TODAY: the card's cutoff, so block_days is exactly 7.
CUTOFF = datetime(2026, 9, 22, 12, 0, tzinfo=IST)


def _ago(days, hour=12):
    """Noon IST `days` calendar days before TODAY."""
    d = TODAY - timedelta(days=days)
    return datetime(d.year, d.month, d.day, hour, 0, tzinfo=IST)


def _profile(username, role, first='', **flags):
    user = User.objects.create_user(username=username, password='x', first_name=first)
    profile = user.profile          # auto-created by the post_save signal
    profile.role = role
    for name, value in flags.items():
        setattr(profile, name, value)
    profile.save()
    return profile


def _program(name, code):
    return Program.objects.create(name=name, program_type='OPEX', client_name='C',
                                  status='Active', short_tender_code=code)


def _site(program, pid, status='Draft', **extra):
    if status in ('Active', 'In Progress', 'On Hold', 'Commissioned'):
        extra.setdefault('activated_at', _ago(40))
    extra.setdefault('project_type', 'OPEX')
    return Project.objects.create(
        project_id=pid, customer_name=pid, customer_phone='9876543210',
        site_address='1 Sun Road', city='Lucknow', program=program,
        dc_capacity_kw=Decimal('10.00'), status=status,
        target_commissioning_date=date(2027, 3, 31), **extra)


def _ledger(assignment, to_status, at, actor=None):
    StatusTransition.objects.create(
        subject_type=SUBJECT_DESIGN_ASSIGNMENT, subject_id=assignment.pk,
        project=assignment.project, from_status='x', to_status=to_status,
        actor=actor, actor_role_code='Design', occurred_at=at)


def _commit(assignment, on, by, approved=True, current=True):
    return DueDateCommitment.objects.create(
        assignment=assignment, proposed_date=on, proposed_by=by, is_current=current,
        approved_by=by if approved else None,
        approved_at=_ago(60) if approved else None)


class Builders(TestCase):
    """People and one builder per stage. Each builder takes the day count that decides
    the boundary and returns the site."""

    @classmethod
    def setUpTestData(cls):
        cls.head = _profile('head1', 'Design', 'Hana', is_design_head=True)
        cls.head2 = _profile('head2', 'Design', 'Hari', is_design_head=True)
        _profile('head_gone', 'Design', 'Gone', is_design_head=True, is_active=False)
        cls.qc = _profile('qc1', 'Design', 'Quinn', is_design_qc=True)
        cls.designer = _profile('des1', 'Design', 'Dev')
        cls.pm = _profile('pm1', 'PM', 'Pam')
        cls.pm2 = _profile('pm2', 'PM', 'Pat')
        cls.scm = _profile('scm1', 'SCM', 'Sam')
        cls.ceo = _profile('ceo1', 'CEO', 'Cee')
        cls.tender = _program('Alpha', 'ALP')

    # -- one builder per stage ------------------------------------------------------
    def s1(self, pid, days, program=None, **site):
        project = _site(program or self.tender, pid, **site)
        DesignAssignment.objects.create(project=project, status=DESIGN_AWAITING_ALLOCATION,
                                        survey_link_added_at=_ago(days))
        return project

    def s2(self, pid, due_days_ago, status=DESIGN_IN_DESIGN, program=None, assigned=40):
        project = _site(program or self.tender, pid)
        a = DesignAssignment.objects.create(project=project, status=status,
                                            assigned_to=self.designer,
                                            assigned_at=_ago(assigned),
                                            current_attempt_number=1)
        DesignAttempt.objects.create(assignment=a, attempt_number=1)
        if due_days_ago is not None:
            _commit(a, TODAY - timedelta(days=due_days_ago), self.designer)
        return project

    def s3(self, pid, due_days_ago, qc_actor=None):
        project = _site(self.tender, pid)
        a = DesignAssignment.objects.create(project=project, status=DESIGN_IN_QC,
                                            assigned_to=self.designer,
                                            assigned_at=_ago(40), current_attempt_number=1)
        DesignAttempt.objects.create(assignment=a, attempt_number=1, qc_started_at=_ago(20))
        _commit(a, TODAY - timedelta(days=due_days_ago), self.designer)
        _ledger(a, DESIGN_IN_QC, _ago(20), actor=qc_actor)
        return project

    def pm_rejected(self, pid, days):
        project = _site(self.tender, pid)
        a = DesignAssignment.objects.create(project=project, status=DESIGN_PM_REJECTED,
                                            assigned_to=self.designer,
                                            assigned_at=_ago(40), current_attempt_number=1)
        # A missed date must not matter: is_overdue() never counts a PM rejection.
        _commit(a, TODAY - timedelta(days=30), self.designer)
        _ledger(a, DESIGN_PM_REJECTED, _ago(days))
        return project

    def s4(self, pid, days, pm=None, program=None):
        project = _site(program or self.tender, pid, assigned_pm=pm or self.pm)
        a = DesignAssignment.objects.create(project=project,
                                            status=DESIGN_AWAITING_PM_APPROVAL,
                                            assigned_at=_ago(40))
        _ledger(a, DESIGN_AWAITING_PM_APPROVAL, _ago(days))
        return project

    def s5(self, pid, days):
        project = _site(self.tender, pid)
        DesignAssignment.objects.create(project=project, status=DESIGN_RELEASED,
                                        released_at=_ago(days))
        return project

    def s6(self, pid, days, ordered=False):
        project = _site(self.tender, pid)
        DesignAssignment.objects.create(project=project, status=DESIGN_RELEASED,
                                        released_at=_ago(days + 5))
        group = SiteGroup.objects.create(program=self.tender, name=f'Batch {pid}',
                                         status=SITE_GROUP_LOCKED, locked_at=_ago(days))
        SiteGroupMembership.objects.create(group=group, project=project, added_by=self.scm)
        if ordered:
            vendor = Vendor.objects.create(name=f'Vendor {pid}')
            order = VendorOrder.objects.create(vendor=vendor, project_type='OPEX',
                                               created_by=self.scm,
                                               total_amount=Decimal('1000'))
            VendorOrderSite.objects.create(order=order, project=project)
        return project

    def s7(self, pid, tasks, status='Active', program=None):
        """`tasks`: dicts of Task fields on one phase."""
        project = _site(program or self.tender, pid, status=status, assigned_pm=self.pm)
        DesignAssignment.objects.create(project=project, status=DESIGN_RELEASED,
                                        released_at=_ago(50))
        phase = ProjectPhase.objects.create(project=project, phase_name='Install',
                                            phase_order=1)
        for i, fields in enumerate(tasks):
            fields.setdefault('task_name', f'Task {i}')
            Task.objects.create(phase=phase, task_order=i, **fields)
        return project

    # -- reading the result ---------------------------------------------------------
    def result(self, today=TODAY, cutoff=CUTOFF):
        return stuck_sites(tender_sites_qs(), today, cutoff)

    def row_for(self, pid, result=None):
        for row in (result or self.result())['rows']:
            if row['site'].split(' · ')[0] == pid:
                return row
        return None


# ---------------------------------------------------------------------------
# (a) each stage's rule: one site just inside, one just past
# ---------------------------------------------------------------------------

class StageBoundaryTests(Builders):

    def assertBoundary(self, inside, past, overshoot=0):
        result = self.result()
        self.assertIsNone(self.row_for(inside, result), inside)
        row = self.row_for(past, result)
        self.assertIsNotNone(row, past)
        self.assertEqual(row['overshoot'], overshoot)
        return row

    def test_s1_seven_days_is_stuck_six_is_not(self):
        self.s1('IN-S1', 6)
        self.s1('PAST-S1', 7)
        row = self.assertBoundary('IN-S1', 'PAST-S1')
        self.assertEqual((row['days_text'], row['limit_text']), ('7 days', '7 days'))

    def test_s2_due_yesterday_is_stuck_due_today_is_not(self):
        self.s2('IN-S2', 0)
        self.s2('PAST-S2', 1)
        row = self.assertBoundary('IN-S2', 'PAST-S2', overshoot=1)
        self.assertEqual(row['days_text'], '1 day past due')
        self.assertEqual(row['limit_text'], 'Due 28 Sep')

    def test_s3_uses_the_due_date(self):
        self.s3('IN-S3', 0)
        self.s3('PAST-S3', 1)
        self.assertBoundary('IN-S3', 'PAST-S3', overshoot=1)

    def test_s3_pm_rejected_uses_our_limit_from_the_ledger(self):
        self.pm_rejected('IN-PMR', 6)
        self.pm_rejected('PAST-PMR', 7)
        row = self.assertBoundary('IN-PMR', 'PAST-PMR')
        self.assertEqual(row['stage'], STAGE_IN_QC)
        self.assertEqual(row['days_text'], '7 days since PM rejection')

    def test_s4_three_days(self):
        self.s4('IN-S4', 2)
        self.s4('PAST-S4', 3)
        self.assertBoundary('IN-S4', 'PAST-S4')

    def test_s5_seven_days(self):
        self.s5('IN-S5', 6)
        self.s5('PAST-S5', 7)
        self.assertBoundary('IN-S5', 'PAST-S5')

    def test_s6_seven_days_and_only_without_a_po_pi(self):
        self.s6('IN-S6', 6)
        self.s6('PAST-S6', 7)
        self.s6('ORDERED-S6', 30, ordered=True)
        self.assertBoundary('IN-S6', 'PAST-S6')
        self.assertIsNone(self.row_for('ORDERED-S6'))

    def test_s7_blocked_at_the_cutoff_is_stuck_a_minute_later_is_not(self):
        self.s7('IN-S7', [{'status': Task.BLOCKED,
                           'blocked_since': CUTOFF + timedelta(minutes=1)}])
        self.s7('PAST-S7', [{'status': Task.BLOCKED, 'blocked_since': CUTOFF}])
        row = self.assertBoundary('IN-S7', 'PAST-S7')
        self.assertEqual((row['days_text'], row['limit_text']),
                         ('7 days blocked', 'Blocked 7 days'))

    def test_s7_internal_task_due_yesterday_is_stuck_due_today_is_not(self):
        self.s7('IN-S7D', [{'due_date': TODAY}])
        self.s7('PAST-S7D', [{'due_date': TODAY - timedelta(days=1),
                              'status': Task.IN_PROGRESS}])
        self.assertBoundary('IN-S7D', 'PAST-S7D', overshoot=1)

    def test_s7_ignores_mirrors_na_external_and_sites_not_in_execution(self):
        old = CUTOFF - timedelta(days=5)
        self.s7('MIRROR', [{'status': Task.BLOCKED, 'blocked_since': old, 'is_mirror': True}])
        self.s7('NA', [{'status': Task.BLOCKED, 'blocked_since': old,
                        'is_not_applicable': True}])
        self.s7('EXT', [{'due_date': TODAY - timedelta(days=5), 'task_type': Task.EXTERNAL}])
        self.s7('HELD', [{'status': Task.BLOCKED, 'blocked_since': old}], status='On Hold')
        self.s7('DONE', [{'status': Task.BLOCKED, 'blocked_since': old}],
                status='Commissioned')
        self.assertEqual(self.result()['rows'], [])

    def test_undated_s4_is_counted_not_guessed(self):
        project = _site(self.tender, 'NOLEDGER', assigned_pm=self.pm)
        DesignAssignment.objects.create(project=project, status=DESIGN_AWAITING_PM_APPROVAL)
        result = self.result()
        self.assertEqual(result['rows'], [])
        self.assertEqual(result['undated'], 1)


# ---------------------------------------------------------------------------
# (b) S2/S3 use is_overdue exactly
# ---------------------------------------------------------------------------

class OverdueRuleTests(Builders):

    def test_pending_extension_still_counts_as_the_head_does(self):
        project = self.s2('EXT-S2', None)
        a = project.design_assignment
        _commit(a, TODAY - timedelta(days=3), self.designer, current=False)
        _commit(a, TODAY + timedelta(days=10), self.designer, approved=False, current=True)
        row = self.row_for('EXT-S2')
        self.assertIsNotNone(row)
        self.assertEqual(row['overshoot'], 3)
        head = {s['project'].project_id: s for s in tender_metrics(self.tender, TODAY)['sites']}
        self.assertTrue(head['EXT-S2']['overdue'])
        self.assertEqual(head['EXT-S2']['days_over'], row['overshoot'])

    def test_no_approved_date_is_never_past_due(self):
        self.s2('NODATE', None, status=DESIGN_ALLOCATED)
        proposed = self.s2('PROPOSED', None)
        _commit(proposed.design_assignment, TODAY - timedelta(days=9), self.designer,
                approved=False)
        self.assertEqual(self.result()['rows'], [])

    def test_every_s2_s3_site_agrees_with_the_head(self):
        for i, due in enumerate((5, 1, 0, -3)):
            self.s2(f'AG-{i}', due)
        self.s3('AG-QC', 4)
        self.pm_rejected('AG-PMR', 2)
        ours = {r['site'] for r in self.result()['rows']}
        head = {s['project'].project_id for s in tender_metrics(self.tender, TODAY)['sites']
                if s['overdue']}
        self.assertEqual(ours, head)


# ---------------------------------------------------------------------------
# (c) S0, test and CAPEX sites never appear
# ---------------------------------------------------------------------------

class ScopeTests(Builders):

    def test_s0_test_and_capex_never_appear(self):
        _site(self.tender, 'S0-NONE')
        DesignAssignment.objects.create(project=_site(self.tender, 'S0-SURVEY'),
                                        status=DESIGN_AWAITING_SURVEY)
        DesignAssignment.objects.create(project=_site(self.tender, 'S0-HOLD'),
                                        status=DESIGN_SURVEY_RETURNED,
                                        survey_returned_at=_ago(90))
        self.s1('TEST-S1', 60, is_test=True)
        capex = _site(None, 'CAPEX-S1', project_type='CAPEX')
        DesignAssignment.objects.create(project=capex, status=DESIGN_AWAITING_ALLOCATION,
                                        survey_link_added_at=_ago(60))
        result = self.result()
        self.assertEqual(result['rows'], [])
        self.assertEqual(result['stuck'], 0)
        self.assertNotIn(STAGE_NO_SURVEY, STUCK_LIMITS)


# ---------------------------------------------------------------------------
# (d) grouping at 5+
# ---------------------------------------------------------------------------

class GroupingTests(Builders):

    def test_five_fold_four_stay_separate(self):
        beta = _program('Beta', 'BET')
        for i in range(5):
            self.s1(f'A-{i}', 20)
        for i in range(4):
            self.s1(f'B-{i}', 20, program=beta)
        result = self.result()
        labels = sorted(r['site'] for r in result['rows'])
        self.assertEqual(labels, ['5 sites', 'B-0', 'B-1', 'B-2', 'B-3'])
        group = next(r for r in result['rows'] if r['site'] == '5 sites')
        self.assertEqual(group['sites'], 5)
        self.assertEqual(group['url'], reverse('program_detail', args=[self.tender.pk]))
        self.assertEqual(result['stuck'], 9)

    def test_limit_stages_group_on_entry_date_only(self):
        for i in range(4):
            self.s1(f'SAME-{i}', 20)
        self.s1('OTHER-DAY', 21)
        self.assertEqual(len(self.result()['rows']), 5)

    def test_due_date_stages_group_on_the_due_date_not_entry(self):
        for i in range(5):
            self.s2(f'DUE-{i}', 4, assigned=30 + i)
        rows = self.result()['rows']
        self.assertEqual([r['site'] for r in rows], ['5 sites'])
        self.assertEqual(rows[0]['days_text'], '4 days past due')

    def test_shared_name_or_n_people(self):
        for i in range(5):
            self.s4(f'PMA-{i}', 10)
        for i in range(4):
            self.s4(f'PMB-{i}', 12)
        self.s4('PMB-X', 12, pm=self.pm2)
        rows = {r['days_text']: r for r in self.result()['rows']}
        self.assertEqual(rows['10 days']['waiting_on'], 'Pam · PM')
        self.assertEqual(rows['12 days']['waiting_on'], '2 people')


# ---------------------------------------------------------------------------
# (e) ordering
# ---------------------------------------------------------------------------

class OrderingTests(Builders):

    def test_longest_overshoot_then_tender_then_site(self):
        beta = _program('Beta', 'BET')
        self.s1('BET-1', 10, program=beta)       # over 3
        self.s1('ALP-3', 10)                     # over 3
        self.s5('ALP-2', 20)                     # over 13
        self.s2('ALP-1', 13)                     # over 13
        order = [r['site'] for r in self.result()['rows']]
        self.assertEqual(order, ['ALP-1', 'ALP-2', 'ALP-3', 'BET-1'])
        self.assertEqual(order, [r['site'] for r in self.result()['rows']])

    def test_summary_counts_sites_per_stage(self):
        for i in range(6):
            self.s1(f'G-{i}', 30)
        self.s2('D-1', 2)
        self.s5('R-1', 9)
        result = self.result()
        self.assertEqual(result['stuck'], 8)
        self.assertEqual([(b['label'], b['sites']) for b in result['by_stage']],
                         [('Design backlog', 6), ('In design', 1),
                          ('Released, not grouped', 1)])


# ---------------------------------------------------------------------------
# Waiting on
# ---------------------------------------------------------------------------

class WaitingOnTests(Builders):

    def test_each_stage_names_who_holds_it(self):
        self.s1('W-S1', 10)
        self.s2('W-DESIGN', 3)
        self.s2('W-UPLOADED', 3, status=DESIGN_ARTIFACTS_UPLOADED)
        self.s3('W-QC', 3, qc_actor=self.qc)
        self.pm_rejected('W-PMR', 9)
        self.s4('W-S4', 5)
        self.s5('W-S5', 9)
        self.s6('W-S6', 9)
        self.s7('W-S7', [{'status': Task.BLOCKED, 'blocked_since': CUTOFF,
                          'task_name': 'Mount rails'},
                         {'due_date': TODAY - timedelta(days=2)}])
        result = self.result()
        got = {r['site']: r['waiting_on'] for r in result['rows']}
        heads = 'Design Head'                        # the role alone: no record names one (S8)
        self.assertEqual(got, {
            'W-S1': heads,
            'W-DESIGN': 'Dev · Designer',
            'W-UPLOADED': 'Design QC',
            'W-QC': 'Quinn · Design QC',
            'W-PMR': heads,
            'W-S4': 'Pam · PM',
            'W-S5': 'SCM',
            'W-S6': 'Sam · SCM',
            'W-S7': 'Pam · PM — overdue: Task 1 (+1 more)',
        })

    def test_blocked_beats_overdue_at_equal_overshoot(self):
        self.s7('TIE', [{'due_date': TODAY - timedelta(days=2), 'task_name': 'Late'},
                        {'status': Task.BLOCKED, 'blocked_since': CUTOFF - timedelta(days=2),
                         'task_name': 'Stuck'}])
        row = self.row_for('TIE')
        self.assertEqual(row['rule'], 'blocked')
        self.assertEqual(row['waiting_on'], 'Pam · PM — blocked: Stuck (+1 more)')

    def test_arka_submitted_splits_by_the_heads_court(self):
        qc_queue = self.s2('ARKA-QC', 3, status=DESIGN_ARKA_SUBMITTED)
        mine = self.s2('ARKA-OK', 3, status=DESIGN_ARKA_SUBMITTED)
        for project, verdict in ((qc_queue, 'pending'), (mine, ARKA_APPROVED)):
            ArkaSubmission.objects.create(
                attempt=project.design_assignment.attempts.get(), version=1,
                capacity_kw=Decimal('10'), arka_link='https://arka.example/x',
                submitted_by=self.designer, verdict=verdict, head_verdict=verdict)
        got = {r['site']: r['waiting_on'] for r in self.result()['rows']}
        self.assertEqual(got['ARKA-QC'], 'Design QC')
        self.assertEqual(got['ARKA-OK'], 'Dev · Designer')


# ---------------------------------------------------------------------------
# Agreement with the owning screens (rulings 2 and 3)
# ---------------------------------------------------------------------------

class AgreementTests(Builders):

    def test_no_due_date_equals_the_heads_count(self):
        self.s1('ND-1', 3)
        self.s1('ND-2', 30)
        self.s2('ND-ALLOC', None, status=DESIGN_ALLOCATED)
        self.s2('ND-DATED', 2)
        self.s4('ND-PM', 1)                           # finished: not counted
        self.s5('ND-REL', 1)                          # finished: not counted
        DesignAssignment.objects.create(project=_site(self.tender, 'ND-SURVEY'),
                                        status=DESIGN_AWAITING_SURVEY)
        DesignAssignment.objects.create(project=_site(self.tender, 'ND-HOLD'),
                                        status=DESIGN_SURVEY_RETURNED)
        head = tender_metrics(self.tender, TODAY)['no_due_date']
        self.assertEqual(head, 5)
        self.assertEqual(self.result()['no_due_date'], [{'tender': 'Alpha', 'count': head}])

    def test_s7_task_counts_equal_the_cards(self):
        now = timezone.now()
        today = timezone.localdate()
        old, young = now - timedelta(days=10), now - timedelta(days=3)
        self.s7('T-1', [{'status': Task.BLOCKED, 'blocked_since': old},
                        {'status': Task.BLOCKED, 'blocked_since': young},
                        {'status': Task.BLOCKED, 'blocked_since': old, 'is_mirror': True},
                        {'status': Task.BLOCKED, 'blocked_since': old,
                         'is_not_applicable': True},
                        {'due_date': today - timedelta(days=5)},
                        {'due_date': today - timedelta(days=5), 'task_type': Task.EXTERNAL},
                        {'due_date': today + timedelta(days=5)}])
        self.s7('T-2', [{'status': Task.BLOCKED, 'blocked_since': old},
                        {'due_date': today - timedelta(days=4), 'status': Task.IN_PROGRESS}],
                status='In Progress')
        self.s7('T-HELD', [{'status': Task.BLOCKED, 'blocked_since': old}], status='On Hold')
        self.s7('T-TEST', [{'status': Task.BLOCKED, 'blocked_since': old}])
        # A tender site with no programme is still a tender site on both counts.
        Project.objects.filter(project_id='T-2').update(program=None)
        Project.objects.filter(project_id='T-TEST').update(is_test=True)
        ctx = _get_ceo_dashboard_context(CONTEXT_TENDERS)
        self.assertEqual(ctx['stuck_sites']['task_counts'],
                         {'blocked_aged': ctx['blocked_aged_7d'],
                          'overdue': ctx['task_overdue']})
        self.assertEqual(ctx['stuck_sites']['task_counts'], {'blocked_aged': 2, 'overdue': 2})


# ---------------------------------------------------------------------------
# (f) the page: section text and a footer that lists exactly STUCK_LIMITS
# ---------------------------------------------------------------------------

def _text(response):
    body = re.sub(r'<[^>]+>', ' ', response.content.decode())
    return re.sub(r'\s+', ' ', html.unescape(body))


class PageTests(Builders):

    def page(self, context='tenders'):
        client = Client(SERVER_NAME='localhost')
        client.force_login(self.ceo.user)
        return client.get(reverse('dashboard_ceo'), {'context': context})

    def footer(self, response):
        match = re.search(r'<p[^>]*data-stuck-limits>(.*?)</p>', response.content.decode(), re.S)
        return html.unescape(match.group(1)).split(' · ')

    def test_footer_lists_all_seven_rules_from_stuck_limits(self):
        lines = self.footer(self.page())
        self.assertEqual(lines, stuck_limit_lines(7))
        stages = [key for key, _ in STAGES if key != STAGE_NO_SURVEY]
        self.assertEqual(list(STUCK_LIMITS), stages)
        self.assertEqual(len(lines), len(STUCK_LIMITS))
        for line, key in zip(lines, stages):
            entry = STUCK_LIMITS[key]
            if entry['days'] is not None:
                self.assertIn(f"{entry['days']} day", line)
                self.assertIn('(our limit)', line)
            else:
                self.assertTrue("Design Head's rule" in line or 'CEO Blocked Tasks' in line,
                                line)
        self.assertIn(f"{STUCK_LIMITS[STAGE_IN_QC]['pm_rejected_days']} days (our limit)",
                      lines[2])

    def test_section_text_and_empty_state(self):
        text = _text(self.page())
        self.assertIn('Stuck sites', text)
        self.assertIn('No site is past its limit.', text)
        project = _site(self.tender, 'PG-1', site_name='Hilltop')
        DesignAssignment.objects.create(
            project=project, status=DESIGN_AWAITING_ALLOCATION,
            survey_link_added_at=timezone.now() - timedelta(days=10))
        text = _text(self.page())
        self.assertIn('1 site stuck — Design backlog 1', text)
        self.assertIn('PG-1 · Hilltop Alpha Survey on file, not allocated 10 days 7 days '
                      'Design Head', text)
        self.assertIn("Alpha: 1 unfinished site with no agreed due date (Design Head's count)",
                      text)
        self.assertNotIn('No site is past its limit.', text)

    def test_rows_past_ten_are_behind_the_toggle(self):
        for i in range(11):
            self.s1(f'TG-{i:02d}', 0)
            DesignAssignment.objects.filter(project__project_id=f'TG-{i:02d}').update(
                survey_link_added_at=timezone.now() - timedelta(days=8 + i))
        body = self.page().content.decode()
        self.assertIn('Show all 11 rows', body)
        self.assertEqual(body.count('x-show="all" x-cloak'), 1)

    def test_not_on_residential(self):
        self.s1('RS-1', 30)
        self.assertNotIn('Stuck sites', _text(self.page('residential')))
        self.assertIsNone(_get_ceo_dashboard_context(None)['stuck_sites'])


# ---------------------------------------------------------------------------
# (g) fixed query count
# ---------------------------------------------------------------------------

class QueryBudgetTests(Builders):

    def build(self, prefix, n):
        makers = [
            lambda i: self.s1(f'{prefix}-{i}', 10 + i % 3),
            lambda i: self.s2(f'{prefix}-{i}', 2 + i % 2),
            lambda i: self.s3(f'{prefix}-{i}', 3, qc_actor=self.qc),
            lambda i: self.pm_rejected(f'{prefix}-{i}', 9),
            lambda i: self.s4(f'{prefix}-{i}', 5),
            lambda i: self.s5(f'{prefix}-{i}', 9),
            lambda i: self.s6(f'{prefix}-{i}', 9, ordered=i % 2 == 0),
            lambda i: self.s7(f'{prefix}-{i}', [
                {'status': Task.BLOCKED, 'blocked_since': CUTOFF, 'assigned_to': self.pm},
                {'due_date': TODAY - timedelta(days=3), 'assigned_to': self.pm2}]),
        ]
        for i in range(n):
            makers[i % len(makers)](i)

    def count(self):
        with CaptureQueriesContext(connection) as queries:
            result = self.result()
        return len(queries), result

    def test_three_sites_and_thirty_cost_the_same(self):
        self.build('Q3', 3)
        small, small_result = self.count()
        self.build('Q30', 30)
        large, large_result = self.count()
        self.assertEqual(small, 7)
        self.assertEqual(large, small)
        self.assertGreater(len(large_result['rows']), len(small_result['rows']))
