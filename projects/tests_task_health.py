"""
task_health: one overdue rule, every reader routed through it, and the task table's
Delayed / Due Today label, and the CEO overdue list.

THE RULES, each by a test class named for it
    PredicateAgreementTests   overdue_q() and is_overdue() give the same answer on every
                              status x Not Applicable x approved x date x project type,
                              and that answer is the rule as written. project_type and
                              approval make no difference.
    TaskTableTests            the Due Date cell: red date and a solid red "Delayed · Nd"
                              on an overdue row (a Derived row included), "· awaiting
                              approval" while submitted and unapproved, a solid orange
                              "Due Today", nothing on a Done row, a Not Applicable row or
                              an undated row; "Delayed · check date" in place of a day
                              count when the due date is before 2020; the HTMX responders
                              draw the same cell.
    CountAgreementTests       on one project the task table, the CEO Tasks card, the S7
                              stuck count and the daily report agree, and where they can
                              differ the difference is each one's SCOPE (mirrors, External,
                              unassigned), never the rule.
    BlockedIsOverdueTests     a Blocked task past its date is overdue on the CEO cards and
                              in stuck_sites(); a task both blocked past the limit and past
                              its date is in both counts and is one row candidate.
    NotApplicableTests        the two readers that did not exclude N/A before (the CEO At
                              Risk badge, the Site Engineer's next task) do now.
    CallerDateTests           the routed dashboards read timezone.localdate(), not
                              date.today().
    CeoOverdueListTests       the CEO Tasks card's "Overdue" number links to a list of
                              exactly the tasks it counts, in every context; who may open
                              it; what a row says; a fixed query count.
    UrgencyCountTests         the PM and Site Engineer urgency figures count a task that
                              is both Blocked and overdue once.
    QueryCountTests           the badge adds no query to the task table page.

Run with:
    python manage.py test projects.tests_task_health --settings=solarpms.test_settings
"""
import inspect
import io
import itertools
import os
import re
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.db import connection
from django.db.models import Count
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from . import task_health
from . import tests_stuck_sites as s7
from .models import Project, ProjectPhase, Task
from .permissions import CEO_DASHBOARD_ROLES, user_can_view_ceo_dashboard_lists
from .reports import build_user_status_rows
from .task_health import OPEN_STATUSES, days_overdue, is_overdue, overdue_q
from .tests_row_render_flags import MIRROR_NAME, RowFlagsFixture, _rows
from .tests_two_step_completion import _client_for, _profile
from .views import (
    CONTEXT_TENDERS, _attach_due_health, _get_ceo_dashboard_context,
    _render_phase_tasks_hx, _render_task_row_hx,
)

# The day the two Net Metering tasks were reported on: due 24 Sep reads 5 days overdue
# and due 26 Sep reads 3.
TODAY = date(2026, 9, 29)
YEAR_20 = date(20, 9, 24)            # a mistyped year, as on the production CEIG task

# The task table's two labels. The CEO card and the counts still say "Overdue".
# What the CEO overdue list costs: its one query, plus the session, the user, the
# profile and the navbar's notification count.
LIST_PAGE_QUERIES = 5

OVERDUE = 'Delayed ·'
DUE_TODAY = 'Due Today'

_real_localdate = timezone.localdate


def _today_is(day):
    """Freeze the argument-less timezone.localdate() at `day` for every caller. A call
    that converts a datetime (localdate(value)) still gets the real answer."""
    def fake(value=None, timezone=None):
        return day if value is None else _real_localdate(value, timezone)
    return patch('django.utils.timezone.localdate', side_effect=fake)


# ---------------------------------------------------------------------------
# 1 — the two forms agree, and agree with the rule
# ---------------------------------------------------------------------------

class PredicateAgreementTests(TestCase):
    """Every combination, on all three project types."""

    DATES = (None, YEAR_20, TODAY - timedelta(days=400), TODAY - timedelta(days=1),
             TODAY, TODAY + timedelta(days=1))

    @classmethod
    def setUpTestData(cls):
        now = timezone.now()
        statuses = [status for status, _ in Task.STATUS_CHOICES]
        cls.projects = {}
        tasks = []
        for project_type in ('Residential', 'OPEX', 'CAPEX'):
            project = Project.objects.create(
                customer_name=f'Health {project_type}', customer_phone='9876543210',
                site_address='1 Rule Road', city='Lucknow', project_type=project_type,
                dc_capacity_kw=Decimal('10.00'), status='Active')
            phase = ProjectPhase.objects.create(project=project, phase_name='Only',
                                                phase_order=1)
            cls.projects[project_type] = project
            combos = itertools.product(statuses, (False, True), (False, True), cls.DATES)
            for order, (status, na, approved, due) in enumerate(combos):
                tasks.append(Task(
                    phase=phase, task_name=f'{status}/{na}/{approved}/{due}',
                    task_order=order, status=status, is_not_applicable=na, due_date=due,
                    approved_at=now if approved else None))
        Task.objects.bulk_create(tasks)

    def _expected(self, task):
        """The rule, written out without either form."""
        return (task.due_date is not None and task.due_date < TODAY
                and task.status != Task.DONE and not task.is_not_applicable)

    def test_both_forms_are_the_rule_on_every_combination(self):
        tasks = list(Task.objects.select_related('phase__project'))
        self.assertEqual(len(tasks), 3 * 4 * 2 * 2 * len(self.DATES))
        in_sql = set(Task.objects.filter(overdue_q(TODAY)).values_list('pk', flat=True))
        for task in tasks:
            with self.subTest(task=task.task_name, type=task.phase.project.project_type):
                expected = self._expected(task)
                self.assertEqual(is_overdue(task, TODAY), expected)
                self.assertEqual(task.pk in in_sql, expected)
        self.assertTrue(in_sql)                     # the matrix is not all-False

    def test_project_type_and_approval_make_no_difference(self):
        per_type = {
            project_type: sorted(
                Task.objects.filter(phase__project=project).filter(overdue_q(TODAY))
                .values_list('task_name', flat=True))
            for project_type, project in self.projects.items()}
        self.assertEqual(per_type['OPEX'], per_type['Residential'])
        self.assertEqual(per_type['CAPEX'], per_type['Residential'])
        # An OPEX Done task with no approval is closed like any other Done task.
        unapproved_done = Task.objects.filter(
            phase__project=self.projects['OPEX'], status=Task.DONE, approved_at=None,
            is_not_applicable=False, due_date=TODAY - timedelta(days=1)).get()
        self.assertFalse(is_overdue(unapproved_done, TODAY))

    def test_the_prefixed_form_counts_the_same_tasks(self):
        counted = dict(
            Project.objects.annotate(n=Count(
                'phases__tasks', filter=overdue_q(TODAY, 'phases__tasks__')))
            .values_list('pk', 'n'))
        for project in self.projects.values():
            in_python = sum(is_overdue(t, TODAY)
                            for t in Task.objects.filter(phase__project=project))
            self.assertEqual(counted[project.pk], in_python)
            self.assertGreater(in_python, 0)

    def test_days_overdue(self):
        phase = ProjectPhase.objects.filter(project=self.projects['OPEX']).get()
        def task(**fields):
            return Task(phase=phase, task_name='t', task_order=0, **fields)
        self.assertEqual(days_overdue(task(due_date=date(2026, 9, 24)), TODAY), 5)
        self.assertEqual(days_overdue(task(due_date=date(2026, 9, 26)), TODAY), 3)
        self.assertEqual(days_overdue(task(due_date=TODAY - timedelta(days=1),
                                           status=Task.BLOCKED), TODAY), 1)
        for not_overdue in (task(due_date=TODAY), task(due_date=TODAY + timedelta(days=1)),
                            task(due_date=None),
                            task(due_date=date(2026, 9, 24), status=Task.DONE),
                            task(due_date=YEAR_20, is_not_applicable=True)):
            self.assertEqual(days_overdue(not_overdue, TODAY), 0)

    def test_open_statuses_are_every_status_but_done(self):
        self.assertEqual(set(OPEN_STATUSES),
                         {status for status, _ in Task.STATUS_CHOICES} - {Task.DONE})

    def test_the_module_reads_no_clock(self):
        """`today` is the caller's: no default, and nothing to read a date from."""
        for function in (overdue_q, is_overdue, days_overdue):
            self.assertIs(inspect.signature(function).parameters['today'].default,
                          inspect.Parameter.empty)
        for name in ('timezone', 'date', 'datetime'):
            self.assertFalse(hasattr(task_health, name), name)

    def test_no_reader_spells_the_rule_itself(self):
        """The only `due_date__lt` left beside task_health's own is the CEO page's ISSUE
        count, which is not a task."""
        base = os.path.dirname(__file__)
        spelling = re.compile(r'due_date__lt\s*=\s*(today|report_date|date\.today\(\))')
        for name in ('reports.py', 'tender_stages.py',
                     os.path.join('management', 'commands', 'send_eod_digest.py')):
            with io.open(os.path.join(base, name), encoding='utf-8') as fh:
                self.assertIsNone(spelling.search(fh.read()), name)
        with io.open(os.path.join(base, 'views.py'), encoding='utf-8') as fh:
            source = fh.read()
        found = list(spelling.finditer(source))
        self.assertEqual(len(found), 1)
        self.assertIn('issue_overdue', source[found[0].start() - 200:found[0].start()])


# ---------------------------------------------------------------------------
# 2 — the task table
# ---------------------------------------------------------------------------

class TaskTableFixture(RowFlagsFixture):
    """The reported site, rebuilt: two Net Metering tasks In Progress and past due, a
    CEIG task marked Not Applicable with a year-20 date, and one row for every other
    state the cell has to get right. Every other due date on the site is cleared, so
    the only dated rows are the ones named here."""

    def setUp(self):
        super().setUp()
        Task.objects.filter(phase__project=self.site).update(due_date=None)
        self._order = (Task.objects.filter(phase=self.phase)
                       .order_by('-task_order').first().task_order)
        self.ess2 = self._task('Net Metering Approval ESS-2', date(2026, 9, 24),
                               status=Task.IN_PROGRESS)
        self.ess1 = self._task('Net Metering approval ESS-1', date(2026, 9, 26),
                               status=Task.IN_PROGRESS)
        self.ceig = self._task('CEIG Approval', YEAR_20, is_not_applicable=True)
        self.blocked = self._task('Blocked and late', date(2026, 9, 28),
                                  status=Task.BLOCKED)
        self.done_late = self._task('Done late', date(2026, 9, 1), status=Task.DONE)
        self.due_today = self._task('Falls today', TODAY)
        self.done_today = self._task('Done, due today', TODAY, status=Task.DONE)
        self.na_today = self._task('Not applicable, due today', TODAY,
                                   is_not_applicable=True)
        self.undated = self._task('No date', None)
        self.future = self._task('Next week', date(2026, 10, 6))
        self.mirror = Task.objects.get(phase__project=self.site, task_name=MIRROR_NAME)
        self.assertTrue(self.mirror.is_mirror)

    def _task(self, name, due, **fields):
        self._order += 1
        fields.setdefault('assigned_role', Task.PM)
        fields.setdefault('assigned_to', self.pm)
        return Task.objects.create(phase=self.phase, task_name=name,
                                   task_order=self._order, due_date=due, **fields)

    def _page(self, profile=None):
        with _today_is(TODAY):
            return self._overview_rows(self.site, profile)

    def assertUnmarked(self, row):
        self.assertNotIn(OVERDUE, row)
        self.assertNotIn(DUE_TODAY, row)
        self.assertNotIn('badge-due-today', row)


class TaskTableTests(TaskTableFixture):

    def test_the_two_net_metering_tasks(self):
        rows = self._page()
        # Solid red, the status labels' own class, and no inline style on the label.
        self.assertIn('<span class="badge bg-danger text-wrap text-start">Delayed · 5d</span>', rows[self.ess2.pk])
        self.assertIn('<span class="badge bg-danger text-wrap text-start">Delayed · 3d</span>', rows[self.ess1.pk])

    def test_a_submitted_task_says_it_is_awaiting_approval(self):
        """Submitted and not approved is still open, so still delayed; the label adds
        whose move it is. A rejection clears submitted_at and the suffix goes."""
        Task.objects.filter(pk=self.ess2.pk).update(
            submitted_at=timezone.now(), submitted_by=self.pm)
        Task.objects.filter(pk=self.due_today.pk).update(
            submitted_at=timezone.now(), submitted_by=self.pm)
        rows = self._page()
        self.assertIn('<span class="badge bg-danger text-wrap text-start">Delayed · 5d · awaiting approval</span>',
                      rows[self.ess2.pk])
        self.assertNotIn('awaiting approval</span></div>', rows[self.ess1.pk])
        self.assertIn('<span class="badge badge-due-today">Due Today</span>',
                      rows[self.due_today.pk])
        Task.objects.filter(pk=self.ess2.pk).update(submitted_at=None, submitted_by=None)
        self.assertIn('<span class="badge bg-danger text-wrap text-start">Delayed · 5d</span>',
                      self._page()[self.ess2.pk])

    def test_a_not_applicable_task_with_a_year_20_date_shows_nothing(self):
        row = self._page()[self.ceig.pk]
        self.assertIn('Not Applicable', row)
        self.assertUnmarked(row)

    def test_done_not_applicable_undated_and_future_rows_show_nothing(self):
        rows = self._page()
        for task in (self.done_late, self.done_today, self.na_today, self.undated,
                     self.future):
            with self.subTest(task=task.task_name):
                self.assertUnmarked(rows[task.pk])

    def test_a_due_date_before_2020_says_check_date_not_a_day_count(self):
        """An open, applicable task with a mistyped year is still overdue, but 732,000
        days is not a delay anyone should read. The label says what to do; the date is
        still red."""
        Task.objects.filter(pk=self.ess2.pk).update(due_date=YEAR_20)
        row = self._page()[self.ess2.pk]
        self.assertIn('<span class="badge bg-danger text-wrap text-start">'
                      'Delayed · check date</span>', row)
        self.assertNotRegex(row, r'Delayed · \d')
        self.assertRegex(row, r'<input type="date"[^>]*class="[^"]*text-danger')

    def test_check_date_starts_the_day_before_the_floor(self):
        Task.objects.filter(pk=self.ess2.pk).update(due_date=date(2019, 12, 31))
        Task.objects.filter(pk=self.ess1.pk).update(due_date=date(2020, 1, 1))
        rows = self._page()
        self.assertIn('Delayed · check date</span>', rows[self.ess2.pk])
        self.assertIn(f'Delayed · {(TODAY - date(2020, 1, 1)).days}d</span>',
                      rows[self.ess1.pk])

    def test_check_date_keeps_the_awaiting_approval_suffix(self):
        Task.objects.filter(pk=self.ess2.pk).update(
            due_date=YEAR_20, submitted_at=timezone.now(), submitted_by=self.pm)
        self.assertIn('Delayed · check date · awaiting approval</span>',
                      self._page()[self.ess2.pk])

    def test_check_date_changes_no_count_and_not_the_ceo_list(self):
        """Display only, on the task table only. The task is still open and still late:
        the card, the stuck count and the report count it, and the CEO list shows it
        with its day count, first."""
        before = self._all_counts()
        Task.objects.filter(pk=self.ess2.pk).update(due_date=YEAR_20)
        self.assertEqual(self._all_counts(), before)
        self.assertEqual(before['ceo_tenders'], 3)
        ceo = _profile('th_ceo_check', 'CEO')
        with _today_is(TODAY):
            rows = _client_for(ceo).get(
                reverse('dashboard_ceo_overdue_tasks') + '?context=tenders').context['rows']
        self.assertEqual((rows[0]['task'], rows[0]['days']),
                         (self.ess2.task_name, (TODAY - YEAR_20).days))

    def _all_counts(self):
        with _today_is(TODAY):
            tenders = _get_ceo_dashboard_context(CONTEXT_TENDERS)
        return {'ceo_tenders': tenders['task_overdue'],
                'stuck': tenders['stuck_sites']['task_counts']['overdue'],
                'report': build_user_status_rows(TODAY)['totals']['overdue'],
                'table': sum(OVERDUE in row for row in self._page().values())}

    def test_due_today_is_solid_orange_and_not_delayed(self):
        row = self._page()[self.due_today.pk]
        self.assertIn('<span class="badge badge-due-today">Due Today</span>', row)
        self.assertNotIn(OVERDUE, row)

    def test_the_long_label_can_wrap(self):
        """A badge does not break by default, and "Delayed · Nd · awaiting approval" is
        wider than the 10rem Due Date column. text-wrap lets it break at a space instead
        of widening the table (the rule tests_phase_list_approval holds the Status cell
        to)."""
        Task.objects.filter(pk=self.ess2.pk).update(
            submitted_at=timezone.now(), submitted_by=self.pm)
        label = re.search(r'<span class="([^"]*)">Delayed · 5d · awaiting approval</span>',
                          self._page()[self.ess2.pk]).group(1).split()
        self.assertIn('text-wrap', label)
        self.assertNotIn('text-nowrap', label)

    def test_the_orange_is_one_css_class_on_the_page(self):
        """The colour lives in one class beside the other row styles, not in the row."""
        with _today_is(TODAY):
            html = _client_for(self.pm).get(
                reverse('project_overview', args=[self.site.project_id])).content.decode()
        self.assertRegex(
            html, r'\.badge-due-today\s*\{\s*background-color:\s*var\(--bs-orange\);\s*color:\s*#fff;\s*\}')
        due_cell = self._page()[self.due_today.pk].split('Due Today')[0].rsplit('<div', 1)[1]
        self.assertNotIn('style=', due_cell)

    def test_a_blocked_task_past_its_date_is_overdue(self):
        self.assertIn('Delayed · 1d', self._page()[self.blocked.pk])

    def test_a_derived_task_gets_the_same_treatment(self):
        Task.objects.filter(pk=self.mirror.pk).update(due_date=date(2026, 9, 19))
        row = self._page()[self.mirror.pk]
        self.assertIn('Derived', row)
        self.assertIn('Delayed · 10d', row)

    def test_the_date_is_red_in_the_editable_and_the_read_only_cell(self):
        """The PM gets a date input on every row; the Site Engineer gets plain text on a
        PM-role row. Both turn red, and neither colours the row."""
        as_pm = self._page()[self.ess2.pk]
        self.assertRegex(as_pm, r'<input type="date"[^>]*class="[^"]*text-danger')
        as_se = self._page(self.se)[self.ess2.pk]
        self.assertNotIn('<input type="date"', as_se)
        self.assertRegex(as_se, r'<span class="small text-danger">')
        self.assertIn('Delayed · 5d', as_se)
        for row in (as_pm, as_se):
            self.assertNotRegex(row, r'<tr[^>]*(table-danger|bg-danger)')
        # A row that is not overdue keeps an uncoloured date.
        self.assertNotRegex(self._page()[self.future.pk],
                            r'<input type="date"[^>]*class="[^"]*text-danger')

    def test_every_responder_draws_the_cell_the_page_draws(self):
        page = self._page()
        with _today_is(TODAY):
            phase = _rows(self._direct(_render_phase_tasks_hx, self.site, self.phase))
            for task in (self.ess2, self.due_today, self.ceig):
                task = Task.objects.select_related('template_task').get(pk=task.pk)
                single = _rows(self._direct(_render_task_row_hx, self.site, task))
                self.assertEqual(single[task.pk], page[task.pk])
                self.assertEqual(phase[task.pk], page[task.pk])

    def test_changing_the_due_date_redraws_the_badge(self):
        """The due-date POST answers with the row alone; the badge follows the new date."""
        args = [self.site.project_id, self.ess2.pk]
        with _today_is(TODAY):
            later = _rows(self._hx_post('task_set_due_date', args,
                                        {'due_date': '2026-10-15'}))[self.ess2.pk]
            earlier = _rows(self._hx_post('task_set_due_date', args,
                                          {'due_date': '2026-09-20'}))[self.ess2.pk]
            today = _rows(self._hx_post('task_set_due_date', args,
                                        {'due_date': TODAY.isoformat()}))[self.ess2.pk]
        self.assertUnmarked(later)
        self.assertIn('Delayed · 9d', earlier)
        self.assertIn(DUE_TODAY, today)


# ---------------------------------------------------------------------------
# 3 — one project, one count
# ---------------------------------------------------------------------------

class CountAgreementTests(TaskTableFixture):
    """Three tasks on the site are overdue: the two Net Metering tasks and the blocked
    one. The CEIG task is Not Applicable and is counted nowhere."""

    def _counts(self):
        rows = self._page()
        with _today_is(TODAY):
            tenders = _get_ceo_dashboard_context(CONTEXT_TENDERS)
            everything = _get_ceo_dashboard_context(None)
        report = build_user_status_rows(TODAY)
        return {
            'table': sum(OVERDUE in row for row in rows.values()),
            'ceo_tenders': tenders['task_overdue'],
            'ceo_no_context': everything['task_overdue'],
            'ceo_external': tenders['ext_overdue'],
            'stuck': tenders['stuck_sites']['task_counts']['overdue'],
            'report': report['totals']['overdue'],
        }

    def test_the_table_the_ceo_card_the_stuck_count_and_the_report_agree(self):
        self.assertEqual(self._counts(), {
            'table': 3, 'ceo_tenders': 3, 'ceo_no_context': 3, 'ceo_external': 0,
            'stuck': 3, 'report': 3})

    def test_the_report_names_each_holder(self):
        rows = {r['profile'].pk: r['overdue'] for r in build_user_status_rows(TODAY)['rows']}
        self.assertEqual(rows[self.pm.pk], 3)

    def test_where_they_differ_it_is_scope(self):
        """Same rule everywhere; each reader keeps its own scope. A Derived task is on
        the table only. An External task is on the table, the report and the External
        Dependencies card, not the Tasks card. An unassigned task is nobody's row on the
        report."""
        Task.objects.filter(pk=self.mirror.pk).update(due_date=date(2026, 9, 19))
        self.assertEqual(self._counts(), {
            'table': 4, 'ceo_tenders': 3, 'ceo_no_context': 3, 'ceo_external': 0,
            'stuck': 3, 'report': 3})

        self._task('DISCOM inspection', date(2026, 9, 25), task_type=Task.EXTERNAL)
        self.assertEqual(self._counts(), {
            'table': 5, 'ceo_tenders': 3, 'ceo_no_context': 3, 'ceo_external': 1,
            'stuck': 3, 'report': 4})

        self._task('Nobody holds this', date(2026, 9, 25), assigned_to=None)
        self.assertEqual(self._counts(), {
            'table': 6, 'ceo_tenders': 4, 'ceo_no_context': 4, 'ceo_external': 1,
            'stuck': 4, 'report': 4})


# ---------------------------------------------------------------------------
# 4 — Blocked and past due is overdue
# ---------------------------------------------------------------------------

class BlockedIsOverdueTests(s7.Builders):

    def test_the_ceo_cards_count_it_and_agree_with_the_stuck_count(self):
        now, today = timezone.now(), timezone.localdate()
        self.s7('BL-YOUNG', [{'status': Task.BLOCKED, 'blocked_since': now - timedelta(days=2),
                              'due_date': today - timedelta(days=5)}])
        self.s7('BL-BOTH', [{'status': Task.BLOCKED, 'blocked_since': now - timedelta(days=10),
                             'due_date': today - timedelta(days=5)}])
        self.s7('BL-UNDATED', [{'status': Task.BLOCKED,
                                'blocked_since': now - timedelta(days=10)}])
        ctx = _get_ceo_dashboard_context(CONTEXT_TENDERS)
        self.assertEqual(ctx['task_overdue'], 2)
        self.assertEqual(ctx['dept_pm_overdue'], 2)
        self.assertEqual(ctx['blocked_aged_7d'], 2)
        self.assertEqual(ctx['stuck_sites']['task_counts'],
                         {'blocked_aged': ctx['blocked_aged_7d'],
                          'overdue': ctx['task_overdue']})

    def test_blocked_under_the_limit_but_past_due_is_stuck_as_overdue(self):
        self.s7('YOUNG', [{'status': Task.BLOCKED,
                           'blocked_since': s7.CUTOFF + timedelta(days=2),
                           'due_date': s7.TODAY - timedelta(days=10),
                           'task_name': 'Waiting'}])
        row = self.row_for('YOUNG')
        self.assertEqual((row['rule'], row['days'], row['overshoot']),
                         ('overdue_task', 10, 10))
        self.assertEqual(row['waiting_on'], 'Pam · PM — overdue: Waiting')

    def test_blocked_with_no_blocked_since_is_judged_on_its_due_date_alone(self):
        """No blocked_since means no blocked clock to read; the row is late, not aged."""
        self.s7('NOCLOCK', [{'status': Task.BLOCKED, 'blocked_since': None,
                             'due_date': s7.TODAY - timedelta(days=6),
                             'task_name': 'Undated block'}])
        result = self.result()
        row = self.row_for('NOCLOCK', result)
        self.assertEqual((row['rule'], row['overshoot']), ('overdue_task', 6))
        self.assertEqual(result['task_counts'], {'blocked_aged': 0, 'overdue': 1})

    def test_a_task_under_both_rules_is_one_candidate(self):
        """Blocked 10 days (3 over the limit) and 3 days past due: blocked wins the tie,
        and the one task is not also counted as "+1 more"."""
        self.s7('BOTH', [{'status': Task.BLOCKED,
                          'blocked_since': s7.CUTOFF - timedelta(days=3),
                          'due_date': s7.TODAY - timedelta(days=3),
                          'task_name': 'Held'}])
        result = self.result()
        row = self.row_for('BOTH', result)
        self.assertEqual((row['rule'], row['overshoot']), ('blocked', 3))
        self.assertEqual(row['waiting_on'], 'Pam · PM — blocked: Held')
        self.assertEqual(result['task_counts'], {'blocked_aged': 1, 'overdue': 1})

    def test_the_further_overshoot_of_the_two_names_the_rule(self):
        self.s7('LATE', [{'status': Task.BLOCKED,
                          'blocked_since': s7.CUTOFF - timedelta(days=3),
                          'due_date': s7.TODAY - timedelta(days=20),
                          'task_name': 'Held'}])
        row = self.row_for('LATE')
        self.assertEqual((row['rule'], row['overshoot']), ('overdue_task', 20))


# ---------------------------------------------------------------------------
# 5 — the two readers that did not exclude Not Applicable
# ---------------------------------------------------------------------------

class NotApplicableTests(RowFlagsFixture):

    def _late(self, **fields):
        Task.objects.filter(phase__project=self.site).update(due_date=None)
        Task.objects.filter(pk=self.task.pk).update(
            due_date=timezone.localdate() - timedelta(days=4), **fields)

    def test_the_ceo_at_risk_badge(self):
        Project.objects.filter(pk=self.site.pk).update(
            target_commissioning_date=timezone.localdate() + timedelta(days=90))
        self._late()
        self.assertEqual(_get_ceo_dashboard_context(CONTEXT_TENDERS)['proj_at_risk'], 1)
        self._late(is_not_applicable=True)
        ctx = _get_ceo_dashboard_context(CONTEXT_TENDERS)
        self.assertEqual((ctx['proj_at_risk'], ctx['proj_on_time']), (0, 1))

    def _next_overdue(self):
        response = _client_for(self.se).get(reverse('dashboard_site_engineer'))
        self.assertEqual(response.status_code, 200)
        card = next(p for p in response.context['projects']
                    if p['project_id'] == self.site.project_id)
        return card['next_task'], card['next_overdue']

    def test_the_site_engineers_next_task(self):
        self._late()
        self.assertEqual(self._next_overdue(), (self.task.task_name, True))
        self._late(is_not_applicable=True)
        # Still chosen as the next task (not this session's to change); no longer late.
        self.assertEqual(self._next_overdue(), (self.task.task_name, False))


# ---------------------------------------------------------------------------
# 6 — callers pass timezone.localdate()
# ---------------------------------------------------------------------------

class _WrongDate(date):
    """date.today() on a machine whose clock is decades out. A view that still asked
    it would find nothing overdue."""
    @classmethod
    def today(cls):
        return date(2000, 1, 1)


class CallerDateTests(RowFlagsFixture):
    """One task, due yesterday by timezone.localdate(). Each routed page counts it while
    views.date.today() answers the year 2000."""

    def setUp(self):
        super().setUp()
        Task.objects.filter(phase__project=self.site).update(due_date=None)
        Task.objects.filter(pk=self.task.pk).update(
            due_date=timezone.localdate() - timedelta(days=1))

    def _context(self, profile, url_name):
        with patch('projects.views.date', _WrongDate):
            response = _client_for(profile).get(reverse(url_name))
        self.assertEqual(response.status_code, 200)
        return response.context

    def test_pm_dashboard(self):
        ctx = self._context(self.pm, 'dashboard_pm')
        self.assertEqual(ctx['summary']['tasks_overdue'], 1)
        self.assertEqual([row['overdue_count'] for row in ctx['projects_with_progress']
                          if row['project'].pk == self.site.pk], [1])

    def test_site_engineer_dashboard(self):
        ctx = self._context(self.se, 'dashboard_site_engineer')
        self.assertEqual((ctx['tasks_overdue'], ctx['total_overdue']), (1, 1))

    def test_design_dashboard(self):
        task = self._hand_design_task(self.site, status=Task.NOT_STARTED)
        Task.objects.filter(pk=task.pk).update(assigned_to=self.designer)
        self.assertEqual(self._context(self.designer, 'dashboard_design')['tasks_overdue'], 1)

    def test_scm_dashboard(self):
        scm = _profile('th_scm', 'SCM')
        self.assertEqual(self._context(scm, 'dashboard_scm')['summary']['tasks_overdue'], 1)

    def test_overdue_drill_down(self):
        ceo = _profile('th_ceo', 'CEO')
        self.assertEqual(self._context(ceo, 'tasks_overdue')['total_count'], 1)


# ---------------------------------------------------------------------------
# 7 — no query added to the task table page
# ---------------------------------------------------------------------------

class QueryCountTests(TaskTableFixture):

    def _page_queries(self):
        client = _client_for(self.pm)
        url = reverse('project_overview', args=[self.site.project_id])
        client.get(url)                                     # warm any per-process cache
        with CaptureQueriesContext(connection) as captured:
            self.assertEqual(client.get(url).status_code, 200)
        return len(captured)

    def test_the_page_costs_the_same_with_and_without_the_badge(self):
        with_badge = self._page_queries()
        with patch('projects.views._attach_due_health'):
            without = self._page_queries()
        self.assertEqual(with_badge, without)

    def test_the_helper_runs_no_query(self):
        tasks = list(Task.objects.filter(phase=self.phase))
        with self.assertNumQueries(0):
            _attach_due_health(tasks, TODAY)
        marked = {t.pk: (t.overdue_days, t.overdue_label, t.due_today) for t in tasks}
        self.assertEqual(marked[self.ess2.pk], (5, '5d', False))
        self.assertEqual(marked[self.due_today.pk], (0, '', True))
        self.assertEqual(marked[self.ceig.pk], (0, '', False))


# ---------------------------------------------------------------------------
# 8 — the CEO overdue list is the Tasks card's number
# ---------------------------------------------------------------------------

def _late(days, **fields):
    """Task fields for one task `days` past due by the real timezone.localdate(), which
    is what the dashboard and the list both read."""
    fields.setdefault('due_date', timezone.localdate() - timedelta(days=days))
    return fields


class CeoOverdueListFixture(s7.Builders):
    """One project of each type with every kind of task the card must and must not
    count, plus the projects the card leaves out whole."""

    # Task names the list must hold, on every project it shows.
    COUNTED = ('Late', 'Blocked late', 'Unassigned late', 'Submitted late')

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.admin = s7._profile('ol_admin', 'Admin', 'Ada')
        cls.sysadmin = s7._profile('ol_sys', 'System Admin', 'Sy')
        cls.finance = s7._profile('ol_fin', 'Finance', 'Fin')
        cls.se = s7._profile('ol_se', 'Site Engineer', 'Sita')

    def _tasks(self):
        now = timezone.now()
        return [
            _late(9, task_name='Late', status=Task.IN_PROGRESS, assigned_to=self.pm),
            _late(6, task_name='Blocked late', status=Task.BLOCKED, blocked_since=now,
                  assigned_to=self.se, assigned_role=Task.SITE_ENGINEER),
            _late(3, task_name='Unassigned late', assigned_role=Task.SCM),
            _late(1, task_name='Submitted late', status=Task.IN_PROGRESS,
                  assigned_to=self.pm, submitted_at=now, submitted_by=self.pm),
            # Everything below is left out by the card, so by the list.
            _late(9, task_name='External late', task_type=Task.EXTERNAL),
            _late(9, task_name='Mirror late', is_mirror=True),
            _late(9, task_name='Not applicable late', is_not_applicable=True),
            _late(9, task_name='Done late', status=Task.DONE),
            _late(0, task_name='Due today'),
            _late(-5, task_name='Not due yet'),
            {'task_name': 'Undated', 'status': Task.IN_PROGRESS},
        ]

    def setUp(self):
        self.s7('OL-OPEX', self._tasks())
        self.s7('OL-RES', self._tasks())
        self.s7('OL-CAPEX', self._tasks())
        # Whole projects the card leaves out: test data, deleted, On Hold, Draft.
        self.s7('OL-TEST', [_late(9, task_name='Late')])
        self.s7('OL-DELETED', [_late(9, task_name='Late')])
        self.s7('OL-HELD', [_late(9, task_name='Late')], status='On Hold')
        self.s7('OL-DRAFT', [_late(9, task_name='Late')], status='Draft')
        Project.objects.filter(project_id='OL-RES').update(project_type='Residential',
                                                           program=None)
        Project.objects.filter(project_id='OL-CAPEX').update(project_type='CAPEX')
        Project.objects.filter(project_id='OL-TEST').update(is_test=True)
        Project.objects.filter(project_id='OL-DELETED').update(is_deleted=True)

    def _url(self, context=None):
        url = reverse('dashboard_ceo_overdue_tasks')
        return f'{url}?context={context}' if context else url

    def _list(self, context=None, profile=None):
        response = _client_for(profile or self.ceo).get(self._url(context))
        self.assertEqual(response.status_code, 200)
        return response


class CeoOverdueListTests(CeoOverdueListFixture):

    # Which projects each context shows: Tenders is RESCO only (D2 hides CAPEX there).
    SITES = {None: ('OL-CAPEX', 'OL-OPEX', 'OL-RES'),
             'residential': ('OL-RES',),
             'tenders': ('OL-OPEX',)}

    def test_the_list_is_the_cards_number_in_every_context(self):
        for context, sites in self.SITES.items():
            with self.subTest(context=context):
                card = _get_ceo_dashboard_context(context)['task_overdue']
                response = self._list(context)
                rows = response.context['rows']
                self.assertEqual(len(rows), card)
                self.assertEqual(response.context['total'], card)
                self.assertEqual(card, len(sites) * len(self.COUNTED))
                self.assertEqual(sorted((r['site'], r['task']) for r in rows),
                                 sorted((site, name) for site in sites
                                        for name in self.COUNTED))

    def test_the_card_links_to_the_list_in_its_own_context(self):
        for context in self.SITES:
            with self.subTest(context=context):
                query = f'?context={context}' if context else ''
                html = _client_for(self.ceo).get(
                    reverse('dashboard_ceo') + query).content.decode()
                count = _get_ceo_dashboard_context(context)['task_overdue']
                self.assertRegex(
                    html, r'<a href="%s" class="stat-num red[^>]*>%d</a>'
                    % (re.escape(self._url(context)), count))

    def test_a_zero_is_not_a_link(self):
        Task.objects.all().update(due_date=None)
        html = _client_for(self.ceo).get(reverse('dashboard_ceo')).content.decode()
        self.assertNotIn(reverse('dashboard_ceo_overdue_tasks'), html)
        self.assertEqual(self._list().context['rows'], [])
        self.assertIn('No overdue task.', self._list().content.decode())

    def test_longest_overdue_first_then_site(self):
        rows = self._list().context['rows']
        self.assertEqual([(r['days'], r['site']) for r in rows], [
            (9, 'OL-CAPEX'), (9, 'OL-OPEX'), (9, 'OL-RES'),
            (6, 'OL-CAPEX'), (6, 'OL-OPEX'), (6, 'OL-RES'),
            (3, 'OL-CAPEX'), (3, 'OL-OPEX'), (3, 'OL-RES'),
            (1, 'OL-CAPEX'), (1, 'OL-OPEX'), (1, 'OL-RES')])

    def test_what_a_row_says(self):
        response = self._list('tenders')
        rows = {r['task']: r for r in response.context['rows']}
        late = rows['Late']
        self.assertEqual((late['site'], late['programme'], late['assignee'], late['days'],
                          late['status'], late['awaiting_approval']),
                         ('OL-OPEX', 'Alpha', 'Pam · PM', 9, Task.IN_PROGRESS, False))
        self.assertEqual(late['due_date'], timezone.localdate() - timedelta(days=9))
        self.assertEqual(late['site_url'], reverse('project_overview', args=['OL-OPEX']))
        self.assertEqual((rows['Blocked late']['assignee'], rows['Blocked late']['status']),
                         ('Sita · Site Engineer', Task.BLOCKED))
        # Nobody holds it: the owning role alone, no person.
        self.assertEqual(rows['Unassigned late']['assignee'], 'SCM')
        self.assertTrue(rows['Submitted late']['awaiting_approval'])
        text = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', response.content.decode()))
        self.assertIn('Tenders · Overdue tasks · 4 tasks', text)
        self.assertIn('In Progress · awaiting approval', text)
        self.assertEqual(text.count('awaiting approval'), 1)
        # A site with no programme shows a dash, not "None".
        self.assertEqual({r['programme'] for r in self._list('residential').context['rows']},
                         {'—'})

    def test_an_invalid_context_is_a_404(self):
        for bad in ('capex', 'Tenders', '', 'tenders ', 'all'):
            with self.subTest(context=bad):
                response = _client_for(self.ceo).get(
                    reverse('dashboard_ceo_overdue_tasks'), {'context': bad})
                self.assertEqual(response.status_code, 404)

    def test_who_may_open_it(self):
        for profile in (self.ceo, self.admin, self.sysadmin):
            self.assertEqual(_client_for(profile).get(self._url()).status_code, 200)
        for profile in (self.finance, self.pm, self.scm, self.se, self.designer):
            with self.subTest(role=profile.role):
                response = _client_for(profile).get(self._url('tenders'))
                self.assertEqual(response.status_code, 403)
                # The app's 403 page: not an empty body, and not a row of the list.
                body = response.content.decode()
                self.assertIn('<html', body.lower())
                self.assertNotIn('OL-OPEX', body)
        anonymous = self.client.get(self._url())
        self.assertEqual(anonymous.status_code, 302)
        self.assertIn('/login/', anonymous['Location'])

    def test_the_roles_come_from_permissions(self):
        self.assertEqual(CEO_DASHBOARD_ROLES, frozenset({'CEO', 'Admin', 'System Admin'}))
        self.assertTrue(user_can_view_ceo_dashboard_lists(self.ceo.user))
        self.assertFalse(user_can_view_ceo_dashboard_lists(self.finance.user))

    def _queries(self):
        client = _client_for(self.ceo)
        client.get(self._url())                             # warm any per-process cache
        with CaptureQueriesContext(connection) as captured:
            response = client.get(self._url())
        return len(captured), response.context['total']

    def test_the_page_costs_the_same_for_12_rows_and_132(self):
        few, total = self._queries()
        self.assertEqual(total, 12)
        people = [s7._profile(f'ol_p{i}', 'PM', f'P{i}') for i in range(6)]
        for i in range(30):
            program = s7._program(f'Tender {i}', f'T{i:02d}') if i % 3 == 0 else self.tender
            self.s7(f'OL-MANY-{i:02d}',
                    [_late(d, task_name=f'Late {d}', assigned_to=people[(i + d) % 6])
                     for d in (2, 4, 5, 7)], program=program)
        many, total = self._queries()
        self.assertEqual(total, 12 + 120)
        self.assertEqual(many, few)
        # Stated, so a change to the page's cost is a decision.
        self.assertEqual(few, LIST_PAGE_QUERIES)


# ---------------------------------------------------------------------------
# 9 — urgency counts a task once
# ---------------------------------------------------------------------------

class UrgencyCountTests(RowFlagsFixture):
    """The Site Engineer holds three tasks on the site: one Blocked and late, one
    Blocked with no date, one late and not blocked. Two are blocked, two are overdue,
    and three tasks are urgent."""

    def setUp(self):
        super().setUp()
        Task.objects.filter(phase__project=self.site).update(due_date=None)
        yesterday = timezone.localdate() - timedelta(days=1)
        others = list(Task.objects.filter(phase=self.phase, is_mirror=False,
                                          assigned_role=Task.SITE_ENGINEER)
                      .exclude(pk=self.task.pk).exclude(assigned_to=self.qaqc)[:2])
        Task.objects.filter(pk=self.task.pk).update(
            status=Task.BLOCKED, due_date=yesterday, assigned_to=self.se)
        Task.objects.filter(pk=others[0].pk).update(
            status=Task.BLOCKED, assigned_to=self.se)
        Task.objects.filter(pk=others[1].pk).update(
            status=Task.IN_PROGRESS, due_date=yesterday, assigned_to=self.se)

    def test_pm_dashboard(self):
        ctx = _client_for(self.pm).get(reverse('dashboard_pm')).context
        row = next(r for r in ctx['projects_with_progress']
                   if r['project'].pk == self.site.pk)
        self.assertEqual((row['blocked_count'], row['overdue_count'], row['urgency_count']),
                         (2, 2, 3))

    def test_site_engineer_dashboard(self):
        ctx = _client_for(self.se).get(reverse('dashboard_site_engineer')).context
        card = next(p for p in ctx['projects'] if p['project_id'] == self.site.project_id)
        self.assertEqual((card['blocked_count'], card['overdue_count']), (2, 2))
        extra = card['pending_grn_count'] + card['issue_count']
        self.assertEqual(card['urgency_count'], 3 + extra)
        self.assertEqual(ctx['total_urgent'], 3 + extra)
