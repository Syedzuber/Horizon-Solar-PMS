"""
S8 — CEO Tenders view cleanup: role-only "Waiting on", the approvals empty state and
footer scope, the execution scope label, one kWp formatter, and IST dates.

Real fixtures throughout; the builders are the S6 and S7 suites' own, subclassed rather
than copied so the fixtures cannot drift apart.

    python manage.py test projects.tests_ceo_tenders_cleanup --settings=solarpms.test_settings
"""
import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from . import tests_ceo_approvals_waiting as s6
from . import tests_stuck_sites as s7
from .approvals import withdraw_approval_request
from .models import (
    DESIGN_IN_QC, DesignAssignment, DesignAttempt, Project, ProjectPhase, Task,
)
from .tender_stages import STUCK_GROUP_MIN
from .views import (
    CONTEXT_TENDERS, _get_ceo_dashboard_context, _kwp_coverage_text, tender_sites_qs,
)


def _text(response):
    """The page's visible text, tags stripped and whitespace collapsed."""
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', response.content.decode()))


def _page(profile, context):
    client = Client(SERVER_NAME='localhost')
    client.force_login(profile.user)
    return client.get(reverse('dashboard_ceo'), {'context': context})


# The department tile's title span, exactly as the template writes it, so "Design" in
# "Design throughput" or "Design Head" elsewhere on the page cannot match.
def _tile(label):
    return f'<span class="fw-semibold" style="font-size:.85rem;">{label}</span>'


# ---------------------------------------------------------------------------
# a) T1 — the role alone when the record names nobody
# ---------------------------------------------------------------------------

class RoleOnlyWaitingOnTests(s7.Builders):

    def _in_qc_without_history(self, pid, due_days_ago):
        """An in_qc package with NO ledger row, so nothing names the reviewer."""
        project = s7._site(self.tender, pid)
        a = DesignAssignment.objects.create(project=project, status=DESIGN_IN_QC,
                                            assigned_to=self.designer,
                                            assigned_at=s7._ago(40), current_attempt_number=1)
        DesignAttempt.objects.create(assignment=a, attempt_number=1)
        s7._commit(a, s7.TODAY - timedelta(days=due_days_ago), self.designer)
        return project

    def test_in_qc_with_no_history_shows_the_role_only(self):
        self._in_qc_without_history('QC-NONE', 3)
        self.assertEqual(self.row_for('QC-NONE')['waiting_on'], 'Design QC')

    def test_one_known_person_keeps_name_and_role(self):
        self.s3('QC-KNOWN', 3, qc_actor=self.qc)
        self.assertEqual(self.row_for('QC-KNOWN')['waiting_on'], 'Quinn · Design QC')

    def test_the_heads_backlog_never_lists_the_holders(self):
        # Two active Design Heads in the fixture: before S8 both names were listed.
        self.s1('BACKLOG', 10)
        waiting = self.row_for('BACKLOG')['waiting_on']
        self.assertEqual(waiting, 'Design Head')
        self.assertNotIn('Hana', waiting)

    def test_role_only_rows_still_fold_into_one_group(self):
        for i in range(STUCK_GROUP_MIN):
            self._in_qc_without_history(f'QC-G{i}', 3)
        rows = [r for r in self.result()['rows'] if r['sites'] > 1]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['waiting_on'], 'Design QC')


# ---------------------------------------------------------------------------
# b) T2 — the empty state; c) T3 — the footer's scope
# ---------------------------------------------------------------------------

class ApprovalsEmptyStateTests(s6.WaitingFixture):

    def test_no_request_ever_raised(self):
        text = _text(_page(self.ceo, 'tenders'))
        self.assertIn('No approvals raised yet.', text)
        self.assertNotIn('Nothing waiting on anyone in the tenders.', text)
        self.assertFalse(self.card()['any_raised'])

    def test_requests_exist_but_none_waiting(self):
        approval = self.raise_request('Raised then withdrawn', projects=[self.live1])
        withdraw_approval_request(approval, self.scm, 'Raised by mistake.')
        text = _text(_page(self.ceo, 'tenders'))
        self.assertIn('Nothing waiting on anyone in the tenders.', text)
        self.assertNotIn('No approvals raised yet.', text)


class ApprovalsFooterScopeTests(s6.WaitingFixture):

    def test_a_deleted_site_link_counts_and_a_test_site_link_does_not(self):
        # A deleted site cannot be named at raise time, so it is deleted afterwards —
        # the way a request comes to point at one.
        deleted = s6._site('S8-DEL', self.tender)
        self.raise_request('Deleted site', pm=self.pm_x, projects=[deleted])
        Project.objects.filter(pk=deleted.pk).update(is_deleted=True)
        self.raise_request('Test site', pm=self.pm_x, projects=[self.test_site])
        self.assertEqual(self.card()['unlinked'], 1)
        text = _text(_page(self.ceo, 'tenders'))
        self.assertIn('+1 open request not linked to any live tender →', text)

    def test_cancelled_and_capex_links_count_as_outside(self):
        capex = s6._site('S8-CAPEX', None)
        Project.objects.filter(pk=capex.pk).update(project_type='CAPEX')
        self.raise_request('Cancelled', pm=self.pm_x, projects=[self.cancelled])
        self.raise_request('CAPEX', pm=self.pm_x, projects=[capex])
        self.raise_request('No links', pm=self.pm_x)
        self.assertEqual(self.card()['unlinked'], 3)

    def test_the_card_still_costs_two_queries_when_nothing_waits(self):
        self.raise_request('No links', pm=self.pm_x)
        with CaptureQueriesContext(connection) as ctx:
            card = self.card()
        self.assertEqual(card['unlinked'], 1)
        self.assertEqual(len(ctx.captured_queries), 2)


# ---------------------------------------------------------------------------
# d) T4 — execution scope label, tiles and the Usage note
# ---------------------------------------------------------------------------

class ExecutionScopeTests(s7.Builders):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        s7._site(cls.tender, 'EX-ACTIVE', status='Active')
        s7._site(cls.tender, 'EX-DRAFT')                        # live, not Active
        s7._site(None, 'EX-RES', status='Active', project_type='Residential')

    def test_tenders_hides_three_tiles_and_labels_the_scope(self):
        page = _page(self.ceo, 'tenders')
        html, text = page.content.decode(), _text(page)
        self.assertEqual(page.context['proj_total'], 1)
        self.assertIn('Execution — sites Active or In Progress (1)', text)
        for label in ('PM', 'SCM', 'Execution'):
            self.assertIn(_tile(label), html)
        for label in ('Design', 'BD', 'Finance'):
            self.assertNotIn(_tile(label), html)
        self.assertIn('All users, all projects', text)

    def test_residential_shows_all_six_tiles_and_no_label(self):
        page = _page(self.ceo, 'residential')
        html, text = page.content.decode(), _text(page)
        for label in ('PM', 'SCM', 'Design', 'BD', 'Execution', 'Finance'):
            self.assertIn(_tile(label), html)
        self.assertNotIn('Execution — sites Active or In Progress', text)
        self.assertNotIn('All users, all projects', text)


# ---------------------------------------------------------------------------
# e) T5 — one kWp formatter
# ---------------------------------------------------------------------------

class KwpFormatterTests(s7.Builders):

    def test_the_three_shapes_and_an_empty_stage(self):
        self.assertEqual(_kwp_coverage_text(Decimal('4745.00'), 86, 86), '4,745 kWp')
        self.assertEqual(_kwp_coverage_text(None, 0, 86), '— kWp (0 of 86 sites)')
        self.assertEqual(_kwp_coverage_text(Decimal('4155.00'), 79, 86),
                         '4,155 kWp (79 of 86 sites)')
        self.assertEqual(_kwp_coverage_text(Decimal('0'), 0, 0), '—')

    def test_header_card_and_pipeline_use_it(self):
        self.s1('KW-KNOWN', 10)                                  # 10 kWp
        self.s1('KW-NONE', 10)
        Project.objects.filter(project_id='KW-NONE').update(dc_capacity_kw=None)
        expected = '10 kWp (1 of 2 sites)'
        with patch('projects.views._kwp_coverage_text', wraps=_kwp_coverage_text) as spy:
            ctx = _get_ceo_dashboard_context(CONTEXT_TENDERS)
        self.assertEqual(ctx['tender_header']['kwp_text'], expected)
        self.assertEqual(ctx['tender_cards'][0]['kwp_text'], expected)
        stage_s1 = next(r for r in ctx['site_pipeline']['stages'] if r['code'] == 'S1')
        self.assertEqual(stage_s1['kwp'], expected)
        self.assertTrue(spy.called)

        text = _text(_page(self.ceo, 'tenders'))
        self.assertIn(f'1 tender · 2 sites · {expected} · 0 activated', text)
        self.assertIn('Capacity', text)
        self.assertIn('Released', text)
        self.assertNotIn('kWp released', text)
        self.assertNotIn('· kWp ', text)                    # the old unit-first card line


# ---------------------------------------------------------------------------
# f) T6 — IST dates at 01:00 IST
# ---------------------------------------------------------------------------

# 01:00 IST on 29 Sep is 19:30 UTC on 28 Sep: the window where a UTC process's
# date.today() still reads the 28th.
AT_0100_IST = datetime(2026, 9, 29, 1, 0, tzinfo=ZoneInfo('Asia/Kolkata'))


class _UtcServerDate(date):
    """date.today() as a UTC process would answer it at AT_0100_IST."""
    @classmethod
    def today(cls):
        return date(2026, 9, 28)


class IstDatesTests(s7.Builders):

    def _context_at_0100_ist(self):
        with patch('django.utils.timezone.now', return_value=AT_0100_IST), \
             patch('projects.views.date', _UtcServerDate):
            return _get_ceo_dashboard_context(CONTEXT_TENDERS)

    def _site_with_task(self, pid, due):
        project = s7._site(self.tender, pid, status='Active', assigned_pm=self.pm)
        phase = ProjectPhase.objects.create(project=project, phase_name='Install',
                                            phase_order=1)
        Task.objects.create(phase=phase, task_order=0, task_name=f'{pid} task',
                            assigned_role=Task.PM, due_date=due)

    def test_due_today_ist_is_due_today_not_overdue(self):
        self._site_with_task('IST-TODAY', date(2026, 9, 29))
        ctx = self._context_at_0100_ist()
        self.assertEqual(ctx['due_today_count'], 1)
        self.assertEqual(ctx['dept_pm_due_today'], 1)
        self.assertEqual(ctx['task_overdue'], 0)

    def test_due_yesterday_ist_is_overdue_and_the_stuck_list_agrees(self):
        self._site_with_task('IST-YDAY', date(2026, 9, 28))
        ctx = self._context_at_0100_ist()
        self.assertEqual(ctx['due_today_count'], 0)
        self.assertEqual(ctx['task_overdue'], 1)
        self.assertEqual(ctx['stuck_sites']['task_counts']['overdue'], ctx['task_overdue'])


# ---------------------------------------------------------------------------
# g) the Tenders context's query count does not grow
# ---------------------------------------------------------------------------

class TendersQueryCountTests(s6.WaitingFixture):
    """A waiting step in scope (the approvals card's 6) and a site with a PM, so
    stuck_sites' person read runs. This fixture measured 34 at d0c5929 (S7, before S8)
    and 34 after it: T2's empty-state test rides T3's aggregate instead of adding a
    query. (The local database's Tenders context measured 35 before and after; the one
    query between the two is the data's shape, not S8.)"""

    def test_tenders_context_does_not_grow(self):
        self.raise_request('In scope', projects=[self.live1])
        self.raise_request('Out of scope', pm=self.pm_x)
        Project.objects.filter(pk=self.live1.pk).update(assigned_pm=self.pm)
        with CaptureQueriesContext(connection) as ctx:
            result = _get_ceo_dashboard_context(CONTEXT_TENDERS)
        self.assertTrue(result['tender_approvals']['rows'])
        self.assertEqual(len(ctx.captured_queries), 34)
