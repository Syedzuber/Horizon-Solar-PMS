"""Prompt 3.1b-1 — the PM's design approval surface, REACHABLE BY NOTHING.

WHY THIS FILE EXISTS
--------------------
The queue, the approve view and the reject view act on `awaiting_pm_approval`, which no
product code path writes: design_head_qc_pass() still releases directly until prompt
3.1b-2 flips it. So in production the queue is always empty and both views refuse every
site. The tests below reach the status the only way anything can — through
PmGateBase._park_in_pm_gate(), imported rather than re-written, so the fixture stays the
single writer of the status in projects/.

  (a) inertness — the status is still written by the fixture alone; the two stamp fields
      by design_pm_approve() alone; and no pre-existing caller of apply_design_status()
      passes the two new keyword arguments
  (b) the queue renders 200 and empty, in words
  (c) the populated queue — own PM and a Coordinator see the site; nobody else does
  (d) approve
  (e) reject
  (f) a blank remark on reject is refused and writes nothing
  (g) both views refuse other statuses and other users — 403 / redirect, never a 500
  plus the workspace read (B3) and the nav entry (B6).

Nothing here writes the status or the two stamps directly; see the 3.1a module for the
walk that would catch it.
"""
import ast
import inspect
import os

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta

from . import design_views
from .models import (
    DesignAssignment, DesignAttempt, Project, StatusTransition,
    DESIGN_AWAITING_HEAD_QC, DESIGN_AWAITING_PM_APPROVAL, DESIGN_IN_QC, DESIGN_RELEASED,
    REASON_DESIGN_PM_APPROVED, REASON_DESIGN_PM_REJECTED, SUBJECT_DESIGN_ASSIGNMENT,
)
from .tests_design_pm_gate_inert import (
    PmGateBase, _PROJECTS_DIR, _profile, find_pm_gate_writes,
)

EMPTY_STATE = 'No designs are waiting for your approval.'


class PmApprovalBase(PmGateBase):

    def setUp(self):
        super().setUp()
        self.pm       = _profile('pa_pm',    'PM')
        self.other_pm = _profile('pa_pm2',   'PM')
        self.coord    = _profile('pa_coord', 'Project Coordinator')
        self.scm      = _profile('pa_scm',   'SCM')
        self.finance  = _profile('pa_fin',   'Finance')

    def _pm_site(self, code, status=DESIGN_IN_QC, pm=None):
        site, assignment = self._site(code, status)
        Project.objects.filter(pk=site.pk).update(assigned_pm=pm or self.pm)
        site.refresh_from_db()
        return site, assignment

    def _parked(self, code, pm=None):
        site, assignment = self._pm_site(code, pm=pm)
        return site, self._park_in_pm_gate(assignment)

    def _transitions(self, assignment):
        return StatusTransition.objects.filter(
            subject_type=SUBJECT_DESIGN_ASSIGNMENT, subject_id=assignment.pk)

    def _url(self, name, site):
        return reverse(name, kwargs={'project_id': site.project_id})

    def _queue(self, profile):
        self._login(profile)
        response = self.client.get(reverse('design_pm_approval_queue'))
        self.assertEqual(response.status_code, 200)
        return response

    def _queue_ids(self, profile):
        return [row['site'].project_id for row in self._queue(profile).context['rows']]

    def _assert_untouched(self, assignment, status):
        assignment.refresh_from_db()
        self.assertEqual(assignment.status, status)
        self.assertIsNone(assignment.pm_approved_at)
        self.assertIsNone(assignment.pm_approved_by_id)
        self.assertFalse(self._transitions(assignment).exists())


# ===========================================================================
# (a) INERTNESS
# ===========================================================================

def _apply_design_status_callers_passing(names):
    """{enclosing function: count} for every non-test call of apply_design_status() that
    passes any keyword in `names`. Parsed, not grepped, so formatting cannot hide one."""
    found = {}
    for root, dirs, files in os.walk(_PROJECTS_DIR):
        dirs[:] = [d for d in dirs if d not in ('migrations', '__pycache__')]
        for name in files:
            if not name.endswith('.py') or name.startswith('tests'):
                continue
            with open(os.path.join(root, name), encoding='utf-8-sig') as fh:
                tree = ast.parse(fh.read())
            for fn in ast.walk(tree):
                if not isinstance(fn, ast.FunctionDef):
                    continue
                for node in ast.walk(fn):
                    if not isinstance(node, ast.Call):
                        continue
                    callee = getattr(node.func, 'id', None) or getattr(node.func, 'attr', None)
                    if callee != 'apply_design_status':
                        continue
                    if {k.arg for k in node.keywords} & set(names):
                        found[fn.name] = found.get(fn.name, 0) + 1
    return found


class InertnessTests(TestCase):

    def test_01_the_status_is_still_written_by_the_fixture_alone(self):
        status_hits = [h for h in find_pm_gate_writes() if h[1].startswith('W')]
        self.assertEqual([(rel, pid) for rel, pid, _ in status_hits],
                         [('tests_design_pm_gate_inert.py', 'W1')], status_hits)

    def test_02_the_stamps_are_written_by_the_approve_view_alone(self):
        stamp_hits = [h for h in find_pm_gate_writes() if h[1].startswith('F')]
        self.assertEqual(len(stamp_hits), 1, stamp_hits)
        rel, pid, line = stamp_hits[0]
        self.assertEqual((rel, pid), ('design_views.py', 'F2'))
        self.assertIn(line, inspect.getsource(design_views.design_pm_approve))
        self.assertNotIn(line, inspect.getsource(design_views.design_pm_reject))

    def test_03_only_the_two_new_views_pass_the_new_arguments(self):
        """Every pre-existing caller of the chokepoint is unchanged: none passes
        reason_code or remark, so each still writes '' and '' exactly as before."""
        self.assertEqual(_apply_design_status_callers_passing(('reason_code', 'remark')),
                         {'design_pm_approve': 1, 'design_pm_reject': 1})

    def test_04_the_new_arguments_default_to_what_was_written_before(self):
        params = inspect.signature(design_views.apply_design_status).parameters
        self.assertEqual(params['reason_code'].default, '')
        self.assertEqual(params['remark'].default, '')

    def test_05_the_approve_view_requires_the_unreachable_status(self):
        """What makes the one new writer of `released` unreachable: its precondition."""
        source = inspect.getsource(design_views._pm_gate_guard)
        self.assertIn('!= DESIGN_AWAITING_PM_APPROVAL', source)
        self.assertIn('_pm_gate_guard(request, project)',
                      inspect.getsource(design_views.design_pm_approve))


# ===========================================================================
# (b) THE EMPTY QUEUE
# ===========================================================================

class EmptyQueueTests(PmApprovalBase):

    def test_01_renders_200_and_says_so_in_words(self):
        self._pm_site('PA-E1')                       # theirs, but not parked
        response = self._queue(self.pm)
        self.assertEqual(response.context['rows'], [])
        self.assertContains(response, EMPTY_STATE)

    def test_02_the_nav_entry_is_shown_to_pm_and_coordinator_only(self):
        link = reverse('design_pm_approval_queue')
        for profile in (self.pm, self.coord):
            self.assertContains(self._queue(profile), f'href="{link}"')
        for profile in (self.designer, self.head, self.scm, self.finance):
            self.assertNotContains(self._queue(profile), f'href="{link}"')


# ===========================================================================
# (c) THE POPULATED QUEUE
# ===========================================================================

class PopulatedQueueTests(PmApprovalBase):

    def setUp(self):
        super().setUp()
        self.site, self.assignment = self._parked('PA-Q1')
        self.site.coordinators.add(self.coord)
        self._pm_site('PA-Q1-NOT-PARKED')                     # same PM, still in QC
        self._parked('PA-Q2', pm=self.other_pm)               # another PM's parked site

    def test_01_its_own_pm_sees_it(self):
        response = self._queue(self.pm)
        self.assertEqual([r['site'].project_id for r in response.context['rows']], ['PA-Q1'])
        self.assertNotContains(response, EMPTY_STATE)
        self.assertContains(response, self._url('design_pm_approve', self.site))
        self.assertContains(response, self._url('design_pm_reject', self.site))
        self.assertContains(response, self._url('design_site_workspace', self.site))

    def test_02_a_coordinator_on_the_site_sees_it(self):
        self.assertEqual(self._queue_ids(self.coord), ['PA-Q1'])

    def test_03_nobody_else_sees_it(self):
        self.assertEqual(self._queue_ids(self.other_pm), ['PA-Q2'])
        for profile in (self.designer, self.head, self.scm, self.finance):
            self.assertNotIn('PA-Q1', self._queue_ids(profile))

    def test_04_one_row_per_site_however_many_coordinators(self):
        """Pins the queue's .distinct(): the coordinators M2M join would otherwise return
        this site once per coordinator."""
        self.site.coordinators.add(_profile('pa_coord2', 'Project Coordinator'))
        self.assertEqual(self._queue_ids(self.pm), ['PA-Q1'])
        self.assertEqual(self._queue_ids(self.coord), ['PA-Q1'])


# ===========================================================================
# (d) APPROVE
# ===========================================================================

class ApproveTests(PmApprovalBase):

    def test_01_releases_stamps_and_writes_one_ledger_row(self):
        site, assignment = self._parked('PA-A1')
        self._login(self.pm)
        response = self.client.post(self._url('design_pm_approve', site),
                                    {'remark': 'Matches the survey'})
        self.assertRedirects(response, reverse('design_pm_approval_queue'),
                             fetch_redirect_response=False)

        assignment.refresh_from_db()
        self.assertEqual(assignment.status, DESIGN_RELEASED)
        self.assertIsNotNone(assignment.pm_approved_at)
        self.assertEqual(assignment.pm_approved_by, self.pm)
        # B4 as amended: the release stamp moves to the PM's approval.
        self.assertEqual(assignment.released_at, assignment.pm_approved_at)
        self.assertEqual(assignment.released_by, self.pm)

        rows = list(self._transitions(assignment))
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.from_status, DESIGN_AWAITING_PM_APPROVAL)
        self.assertEqual(row.to_status, DESIGN_RELEASED)
        self.assertEqual(row.reason_code, REASON_DESIGN_PM_APPROVED)
        self.assertEqual(row.remark, 'Matches the survey')
        self.assertEqual(row.actor, self.pm)

    def test_02_the_remark_is_optional(self):
        site, assignment = self._parked('PA-A2')
        self._login(self.pm)
        self.client.post(self._url('design_pm_approve', site), {})
        assignment.refresh_from_db()
        self.assertEqual(assignment.status, DESIGN_RELEASED)
        self.assertEqual(self._transitions(assignment).get().remark, '')

    def test_03_a_coordinator_may_approve(self):
        site, assignment = self._parked('PA-A3')
        site.coordinators.add(self.coord)
        self._login(self.coord)
        self.client.post(self._url('design_pm_approve', site), {})
        assignment.refresh_from_db()
        self.assertEqual(assignment.status, DESIGN_RELEASED)
        self.assertEqual(assignment.pm_approved_by, self.coord)
        self.assertEqual(assignment.released_by, self.coord)


# ===========================================================================
# (e) REJECT
# ===========================================================================

class RejectTests(PmApprovalBase):

    def test_01_returns_to_the_head_and_writes_nothing_else(self):
        site, assignment = self._site('PA-R1')
        Project.objects.filter(pk=site.pk).update(assigned_pm=self.pm)
        design_views._open_first_attempt(assignment)
        assignment.refresh_from_db()
        self._park_in_pm_gate(assignment)
        attempts_before = DesignAttempt.objects.filter(assignment=assignment).count()
        attempt_number_before = assignment.current_attempt_number

        self._login(self.pm)
        response = self.client.post(self._url('design_pm_reject', site),
                                    {'remark': 'Inverter make is not the tendered one'})
        self.assertRedirects(response, reverse('design_pm_approval_queue'),
                             fetch_redirect_response=False)

        assignment.refresh_from_db()
        self.assertEqual(assignment.status, DESIGN_AWAITING_HEAD_QC)
        # NO attempt, NO approval stamp, NO release stamp.
        self.assertEqual(DesignAttempt.objects.filter(assignment=assignment).count(),
                         attempts_before)
        self.assertEqual(assignment.current_attempt_number, attempt_number_before)
        self.assertIsNone(assignment.pm_approved_at)
        self.assertIsNone(assignment.pm_approved_by_id)
        self.assertIsNone(assignment.released_at)
        self.assertIsNone(assignment.released_by_id)

        row = self._transitions(assignment).get()
        self.assertEqual(row.from_status, DESIGN_AWAITING_PM_APPROVAL)
        self.assertEqual(row.to_status, DESIGN_AWAITING_HEAD_QC)
        self.assertEqual(row.reason_code, REASON_DESIGN_PM_REJECTED)
        self.assertEqual(row.remark, 'Inverter make is not the tendered one')
        self.assertEqual(row.actor, self.pm)

    def test_02_an_old_release_stamp_survives_a_rejection(self):
        """A reopened site keeps its old released_at (audit P2), so a parked site can carry
        one. The rejection must leave it exactly as it found it."""
        site, assignment = self._parked('PA-R2')
        stamp = timezone.now() - timedelta(days=30)
        DesignAssignment.objects.filter(pk=assignment.pk).update(
            released_at=stamp, released_by=self.head)
        self._login(self.pm)
        self.client.post(self._url('design_pm_reject', site), {'remark': 'Wrong tilt'})
        assignment.refresh_from_db()
        self.assertEqual(assignment.status, DESIGN_AWAITING_HEAD_QC)
        self.assertEqual(assignment.released_at, stamp)
        self.assertEqual(assignment.released_by, self.head)


# ===========================================================================
# (f) A BLANK REMARK ON REJECT
# ===========================================================================

class BlankRemarkTests(PmApprovalBase):

    def test_01_blank_and_whitespace_are_refused_and_write_nothing(self):
        site, assignment = self._parked('PA-B1')
        self._login(self.pm)
        for remark in (None, '', '   \n\t '):
            data = {} if remark is None else {'remark': remark}
            response = self.client.post(self._url('design_pm_reject', site), data)
            self.assertRedirects(response, reverse('design_pm_approval_queue'),
                                 fetch_redirect_response=False)
            self.assertIn('a remark is required', self._messages(response))
            self._assert_untouched(assignment, DESIGN_AWAITING_PM_APPROVAL)


# ===========================================================================
# (g) REFUSALS
# ===========================================================================

class RefusalTests(PmApprovalBase):

    def test_01_both_views_refuse_any_other_status(self):
        for i, status in enumerate((DESIGN_IN_QC, DESIGN_AWAITING_HEAD_QC, DESIGN_RELEASED)):
            site, assignment = self._pm_site(f'PA-S{i}', status=status)
            self._login(self.pm)
            for name in ('design_pm_approve', 'design_pm_reject'):
                response = self.client.post(self._url(name, site), {'remark': 'x'})
                self.assertRedirects(response, reverse('design_pm_approval_queue'),
                                     fetch_redirect_response=False)
                self.assertIn('not awaiting your approval', self._messages(response))
                self._assert_untouched(assignment, status)

    def test_02_both_views_refuse_anyone_but_the_sites_pm_or_coordinator(self):
        site, assignment = self._parked('PA-U1')
        self._pm_site('PA-U2', pm=self.other_pm)
        for profile in (self.other_pm, self.coord, self.designer, self.head,
                        self.scm, self.finance):
            self._login(profile)
            for name in ('design_pm_approve', 'design_pm_reject'):
                response = self.client.post(self._url(name, site), {'remark': 'x'})
                self.assertEqual(response.status_code, 403, (profile.user.username, name))
                self.assertTrue(response.content.strip())
        self._assert_untouched(assignment, DESIGN_AWAITING_PM_APPROVAL)

    def test_03_a_get_writes_nothing(self):
        site, assignment = self._parked('PA-G1')
        self._login(self.pm)
        for name in ('design_pm_approve', 'design_pm_reject'):
            response = self.client.get(self._url(name, site))
            self.assertRedirects(response, reverse('design_pm_approval_queue'),
                                 fetch_redirect_response=False)
        self._assert_untouched(assignment, DESIGN_AWAITING_PM_APPROVAL)

    def test_04_an_unknown_site_is_404(self):
        self._login(self.pm)
        for name in ('design_pm_approve', 'design_pm_reject'):
            response = self.client.post(reverse(name, kwargs={'project_id': 'NO-SUCH'}))
            self.assertEqual(response.status_code, 404)


# ===========================================================================
# B3 — THE WORKSPACE READ
# ===========================================================================

class WorkspaceReadTests(PmApprovalBase):

    def _get(self, profile, site):
        self._login(profile)
        return self.client.get(self._url('design_site_workspace', site))

    def test_01_the_sites_pm_and_coordinator_read_it_while_parked(self):
        site, _ = self._parked('PA-W1')
        site.coordinators.add(self.coord)
        self.assertEqual(self._get(self.pm, site).status_code, 200)
        self.assertEqual(self._get(self.coord, site).status_code, 200)

    def test_02_the_pm_is_refused_at_every_other_status(self):
        for i, status in enumerate((DESIGN_IN_QC, DESIGN_AWAITING_HEAD_QC, DESIGN_RELEASED)):
            site, _ = self._pm_site(f'PA-W2-{i}', status=status)
            self.assertEqual(self._get(self.pm, site).status_code, 403)

    def test_03_another_pm_is_refused_even_while_parked(self):
        site, _ = self._parked('PA-W3')
        self._pm_site('PA-W3-OTHER', pm=self.other_pm)
        for profile in (self.other_pm, self.scm, self.finance):
            self.assertEqual(self._get(profile, site).status_code, 403)

    def test_04_the_designer_and_head_are_unchanged(self):
        site, _ = self._pm_site('PA-W4')
        self.assertEqual(self._get(self.designer, site).status_code, 200)
        self.assertEqual(self._get(self.head, site).status_code, 200)
