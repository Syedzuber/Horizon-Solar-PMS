"""
SCHED-2d — the task list refreshes when a site's dates are rescheduled.

WHAT THIS PINS:

    1  Trigger      an HTMX action whose reschedule moved due dates answers with
                    HX-Trigger {"datesRescheduled": {"site": pk, "count": n}}, n being the
                    due dates that really changed (written + cleared); a no-op edit, a
                    task in no block, and a non-HTMX request get no trigger; the capacity
                    edit keeps its fieldEditDone beside it
    2  Collector    only a request inside @reports_reschedules collects; nothing leaks
                    out of it, and a reschedule outside one records nothing
    3  Partials     the rows endpoint draws the rows the full page draws, the summary
                    endpoint the line the full page draws; both refuse an outsider and a POST
    4  Wiring       the overview's list container and summary listen for the event

WHY TransactionTestCase FOR THE TRIGGERS. The reschedule runs in transaction.on_commit.
In production (no ATOMIC_REQUESTS) that callback runs before the view returns, which is
the whole reason the view can send the header. A TestCase wraps every request in its own
transaction and defers on_commit until after the response, so it would show no header for
a reason that never happens live. TransactionTestCase runs the requests in real
autocommit.

Run with:
    python manage.py test projects.tests_sched2d_refresh --settings=solarpms.test_settings
"""
import json
import re
from unittest.mock import patch

from django.test import TransactionTestCase
from django.urls import reverse

from .models import Project, Task
from .scheduling import (
    _dates_rescheduled, _record_dates_rescheduled, reports_reschedules, schedule_site,
)
from .tests_sched2_scheduler import (
    CIVIL, D, FROZEN_TODAY, LATER_TASKS, MODULE, NET_METERING, RMS, SchedFixture,
    _seed_template_and_blocks, freeze_today,
)
from .tests_two_step_completion import _client_for, _profile
from .utils import assign_task_to

HX = {'HTTP_HX_REQUEST': 'true'}
EVENT = 'datesRescheduled'


def _trigger(response):
    """The response's HX-Trigger as a dict, or None when it sends none."""
    header = response.get('HX-Trigger')
    return None if header is None else json.loads(header)


def _due_dates(site):
    return dict(Task.objects.filter(phase__project=site).values_list('pk', 'due_date'))


def _changed(before, after):
    return sum(1 for pk, due in after.items() if before.get(pk) != due)


class RefreshFixture(TransactionTestCase):
    """A RESCO site with no Civil date; Civil and Modules held by the site engineer.
    Today is FROZEN_TODAY (9 Nov 2026), as for every scheduler test."""

    _site = SchedFixture._site
    _task = SchedFixture._task
    _set_due = SchedFixture._set_due

    def setUp(self):
        patcher = freeze_today(FROZEN_TODAY)
        patcher.start()
        self.addCleanup(patcher.stop)
        _seed_template_and_blocks()
        self.pm = _profile('s2d_pm', 'PM')
        self.se = _profile('s2d_se', 'Site Engineer')
        self.site = self._site('40.00')
        self.civil = self._task(self.site, CIVIL)
        self.module = self._task(self.site, MODULE)
        for task in (self.civil, self.module):
            assign_task_to(task, self.se, notify=False)

    def _edit_date(self, task, value, hx=True, profile=None):
        return _client_for(profile or self.pm).post(
            reverse('task_set_due_date', args=[self.site.project_id, task.pk]),
            {'due_date': value}, **(HX if hx else {}))


# ---------------------------------------------------------------------------
# 1. The trigger
# ---------------------------------------------------------------------------

class TriggerTests(RefreshFixture):

    def test_a_civil_date_edit_sends_the_trigger_with_the_count(self):
        before = _due_dates(self.site)
        response = self._edit_date(self.civil, '2026-11-10')
        after = _due_dates(self.site)
        self.assertEqual(response.status_code, 200)
        # The Civil date itself is the person's edit, not a reschedule: not counted.
        self.assertEqual(_changed(before, after) - 1, LATER_TASKS)
        self.assertEqual(_trigger(response), {EVENT: {'site': self.site.pk,
                                                      'count': LATER_TASKS}})

    def test_the_role_owner_path_sends_it_too(self):
        response = self._edit_date(self.civil, '2026-11-10', profile=self.se)
        self.assertEqual(_trigger(response)[EVENT]['count'], LATER_TASKS)

    def test_clearing_the_civil_date_counts_the_cleared_dates(self):
        self._edit_date(self.civil, '2026-11-10')
        response = self._edit_date(self.civil, '')
        self.assertEqual(_trigger(response), {EVENT: {'site': self.site.pk,
                                                      'count': LATER_TASKS}})
        self.assertFalse(Task.objects.filter(phase__project=self.site,
                                             due_date_source=Task.DUE_DATE_SYSTEM).exists())

    def test_saving_the_same_date_again_sends_no_trigger(self):
        self._edit_date(self.civil, '2026-11-10')
        response = self._edit_date(self.civil, '2026-11-10')
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(_trigger(response))

    def test_a_manual_date_on_a_task_in_no_block_sends_no_trigger(self):
        self._edit_date(self.civil, '2026-11-10')
        net_metering = self._task(self.site, NET_METERING)
        response = self._edit_date(net_metering, '2026-12-01')
        net_metering.refresh_from_db()
        self.assertEqual(net_metering.due_date_source, Task.DUE_DATE_MANUAL)
        self.assertIsNone(_trigger(response))

    def test_a_reschedule_that_moves_no_due_date_sends_no_trigger(self):
        """Moving the Civil date on a site with no capacity: the scheduler runs and
        clears nothing (there is nothing to clear), so the count is 0."""
        Project.objects.filter(pk=self.site.pk).update(dc_capacity_kw=None)
        response = self._edit_date(self.civil, '2026-11-10')
        self.assertIsNone(_trigger(response))

    def test_a_non_htmx_edit_redirects_without_the_header(self):
        response = self._edit_date(self.civil, '2026-11-10', hx=False)
        self.assertEqual(response.status_code, 302)
        self.assertIsNone(_trigger(response))
        self.assertEqual(self._task(self.site, MODULE).due_date, D(12), 'still rescheduled')

    def test_the_in_progress_date_on_civil_sends_the_trigger(self):
        response = _client_for(self.pm).post(
            reverse('task_status_update', args=[self.site.project_id, self.civil.pk]),
            {'status': Task.IN_PROGRESS, 'due_date': '2026-11-10'}, **HX)
        self.assertEqual(_trigger(response)[EVENT]['count'], LATER_TASKS)

    def test_a_late_done_on_block_2_sends_the_trigger(self):
        """Modules (block 2) due 12 Nov, approved on 20 Nov: the late finish re-anchors
        every later block, through task_approve's HTMX response."""
        qaqc = _profile('s2d_qaqc', 'Site Engineer', is_qaqc=True)
        assign_task_to(self._task(self.site, RMS), qaqc, notify=False)
        self._edit_date(self.civil, '2026-11-10')
        pid = self.site.project_id
        _client_for(self.pm).post(reverse('task_status_update', args=[pid, self.module.pk]),
                                  {'status': Task.IN_PROGRESS}, **HX)
        _client_for(self.pm).post(reverse('task_submit_for_approval', args=[pid, self.module.pk]),
                                  {'submission_remarks': 'Modules mounted'}, **HX)
        self.module.refresh_from_db()
        self.assertEqual(self.module.due_date, D(12))
        before = _due_dates(self.site)
        with freeze_today(D(20)):
            response = _client_for(qaqc).post(
                reverse('task_approve', args=[pid, self.module.pk]),
                {'approval_remarks': 'Inspected'}, **HX)
        self.module.refresh_from_db()
        self.assertEqual(self.module.status, Task.DONE)
        moved = _changed(before, _due_dates(self.site))
        self.assertGreater(moved, 0)
        self.assertEqual(_trigger(response), {EVENT: {'site': self.site.pk, 'count': moved}})

    def test_a_capacity_edit_keeps_field_edit_done_beside_the_trigger(self):
        self._edit_date(self.civil, '2026-11-10')
        before = _due_dates(self.site)
        response = _client_for(self.pm).post(
            reverse('project_field_edit', args=[self.site.project_id]),
            {'dc_capacity_kw': '120.00', 'reason': 'as built'}, **HX)
        moved = _changed(before, _due_dates(self.site))
        self.assertGreater(moved, 0)
        self.assertEqual(_trigger(response), {
            'fieldEditDone': None,
            EVENT: {'site': self.site.pk, 'count': moved}})

    def test_a_capacity_edit_that_moves_nothing_keeps_its_plain_header(self):
        response = _client_for(self.pm).post(
            reverse('project_field_edit', args=[self.site.project_id]),
            {'dc_capacity_kw': '40.00', 'target_commissioning_date': '2027-01-15'}, **HX)
        self.assertEqual(response['HX-Trigger'], 'fieldEditDone')


# ---------------------------------------------------------------------------
# 2. The collector
# ---------------------------------------------------------------------------

class CollectorTests(RefreshFixture):

    def test_a_reschedule_outside_a_request_records_nothing(self):
        self._set_due(self.civil, D(10))
        result = schedule_site(Project.objects.get(pk=self.site.pk), apply=True)
        self.assertEqual(result.written, LATER_TASKS)
        self.assertIsNone(_dates_rescheduled.get())

    def test_the_collector_is_reset_after_the_request_even_on_an_error(self):
        @reports_reschedules
        def failing_view(request):
            raise RuntimeError('boom')

        request = type('R', (), {'headers': {'HX-Request': 'true'}})()
        with self.assertRaises(RuntimeError):
            failing_view(request)
        self.assertIsNone(_dates_rescheduled.get())

    def test_a_failure_to_record_never_reaches_the_action(self):
        with self.assertLogs('projects.scheduling', level='ERROR'):
            with patch('projects.scheduling._dates_rescheduled') as var:
                var.get.side_effect = RuntimeError('broken')
                _record_dates_rescheduled(self.site, 3)   # logs, does not raise


# ---------------------------------------------------------------------------
# 3. The two partial endpoints
# ---------------------------------------------------------------------------

_CSRF = re.compile(r'name="csrfmiddlewaretoken" value="[^"]*"')


def _norm(html):
    """Whitespace collapsed and CSRF tokens blanked: each render masks the token anew."""
    return ' '.join(_CSRF.sub('name="csrfmiddlewaretoken" value=""', html).split())


def _element(html, element_id, tag='div'):
    """The outer HTML of the first <tag id="element_id"...>...</tag> (no nesting of the
    same tag inside it, which holds for the summary line)."""
    match = re.search(rf'<{tag} id="{element_id}"[\s\S]*?</{tag}>', html)
    return None if match is None else match.group(0)


def _tbodies(html):
    """{phase-tasks-<pk>: inner HTML} for every phase tbody, attributes dropped."""
    return {m.group(1): _norm(m.group(2)) for m in re.finditer(
        r'<tbody id="(phase-tasks-\d+)"[^>]*>([\s\S]*?)</tbody>', html)}


class PartialTests(RefreshFixture):

    def _overview(self, profile=None):
        return _client_for(profile or self.pm).get(
            reverse('project_overview', args=[self.site.project_id])).content.decode()

    def _summary(self, profile=None):
        response = _client_for(profile or self.pm).get(
            reverse('project_plan_summary', args=[self.site.project_id]))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def assertSummaryMatchesPage(self, expected_text):
        page = _element(self._overview(), 'site-plan-summary')
        partial = _element(self._summary(), 'site-plan-summary')
        self.assertIsNotNone(page)
        self.assertEqual(_norm(page), _norm(partial))
        if expected_text:
            self.assertIn(expected_text, _norm(partial))

    def test_the_summary_partial_matches_the_full_page_with_no_plan(self):
        self.assertSummaryMatchesPage(None)
        self.assertNotIn('Planned finish', self._summary())

    def test_the_summary_partial_matches_the_full_page_on_plan(self):
        self._edit_date(self.civil, '2026-11-10')
        self.assertSummaryMatchesPage('On plan')

    def test_the_summary_partial_matches_the_full_page_behind_plan(self):
        self._edit_date(self.civil, '2026-11-10')
        # Civil started freezes the baseline; Civil still open on 20 Nov forecasts
        # every later block past it.
        _client_for(self.pm).post(
            reverse('task_status_update', args=[self.site.project_id, self.civil.pk]),
            {'status': Task.IN_PROGRESS}, **HX)
        with freeze_today(D(20)):
            schedule_site(Project.objects.get(pk=self.site.pk), apply=True)
            self.assertSummaryMatchesPage('behind plan')

    def test_the_rows_endpoint_draws_the_rows_the_page_draws(self):
        self._edit_date(self.civil, '2026-11-10')
        for profile in (self.pm, self.se):
            page = _tbodies(self._overview(profile))
            response = _client_for(profile).get(
                reverse('project_task_rows', args=[self.site.project_id]))
            self.assertEqual(response.status_code, 200)
            rows = _tbodies(response.content.decode())
            self.assertTrue(page)
            self.assertEqual(page, rows, f'rows differ for {profile.role}')

    def test_the_rows_carry_the_new_dates_and_plan_notes(self):
        self._edit_date(self.civil, '2026-11-10')
        Task.objects.filter(pk=self.module.pk).update(baseline_due=D(11))
        body = _client_for(self.pm).get(
            reverse('project_task_rows', args=[self.site.project_id])).content.decode()
        self.assertIn('value="2026-11-12"', body)
        self.assertIn('plan: 11 Nov', body)
        self.assertIn('hx-swap-oob="true"', body)
        self.assertNotIn('hxMessages', body, 'must not wipe the action\'s own message')

    def test_an_outsider_gets_404_from_both(self):
        outsider = _profile('s2d_out', 'Site Engineer')
        for name in ('project_task_rows', 'project_plan_summary'):
            response = _client_for(outsider).get(reverse(name, args=[self.site.project_id]))
            self.assertEqual(response.status_code, 404, name)

    def test_both_refuse_a_post(self):
        for name in ('project_task_rows', 'project_plan_summary'):
            response = _client_for(self.pm).post(reverse(name, args=[self.site.project_id]))
            self.assertEqual(response.status_code, 405, name)

    def test_the_overview_listens_on_the_list_and_the_summary(self):
        page = self._overview()
        rows_url = reverse('project_task_rows', args=[self.site.project_id])
        summary_url = reverse('project_plan_summary', args=[self.site.project_id])
        self.assertRegex(page, rf'<div id="task-list"\s+hx-get="{re.escape(rows_url)}"\s+'
                               r'hx-trigger="datesRescheduled from:body" hx-swap="none" '
                               r'hx-disinherit="\*">')
        self.assertRegex(page, rf'<div id="site-plan-summary"\s+hx-get="{re.escape(summary_url)}"\s+'
                               r'hx-trigger="datesRescheduled from:body" hx-swap="outerHTML"')
        self.assertIn('id="datesRescheduledNotice"', page)
        self.assertIn("addEventListener('datesRescheduled'", page)
        # The summary sits outside the list container, so no row swap can carry it.
        self.assertLess(page.index('id="site-plan-summary"'), page.index('id="task-list"'))
