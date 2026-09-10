"""
The site engineer finishes an OPEX task from the list they are already looking at.

WHAT WAS WRONG
--------------
2.1 put the two-step completion rule in `_apply_task_status_change()` and the two-step
CONTROLS on task_detail. The phase list on project_overview -- the screen a site engineer
actually lives on -- got neither. Three consequences, and this file pins the fix for each:

  1. A submitted task was invisible. It stays In Progress by design (2.1 3), so the row
     read exactly like a task nobody had started. `AwaitingApprovalIsVisibleTests`.
  2. The row's status select still offered Done, which rung 1 refuses on every OPEX task
     with no `approved_at`. Every selection of it was a refusal. `DoneIsNotOfferedTests`.
  3. There was no way to submit from the row at all -- the engineer had to open task_detail
     for a two-field form. `SubmitFromTheRowTests`.

WHAT THIS FILE DOES NOT TEST, because it is not this prompt's work: the refusal itself,
the submit/approve/reject endpoints, and the ledger they write. Those are
`tests_two_step_completion.py`, and nothing here changes them. Every assertion below is
about what the LIST renders and about the request the list makes -- if all of it were
deleted, the rule would be enforced identically and the screen would merely be unusable
again.

WHY THE QUERY-COUNT TEST IS HERE AND NOT AN AFTERTHOUGHT. The obvious build of this was
`{% include 'projects/partials/_task_approval.html' %}` per row, which needs
`_task_approval_context()`, which calls `user_can_manage_project()` twice, which is a
`.exists()` -- two queries per row on a list of 23 and growing. `CostPerRowTests` asserts
the compact partial costs the same per task as a Residential row that renders no approval
markup at all, which is the claim "it added nothing" stated as a number.

Run with:
    python manage.py test projects.tests_phase_list_approval --settings=solarpms.test_settings
"""
import io
import os
import re
from datetime import date, timedelta
from decimal import Decimal

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from .models import Project, ProjectPhase, Task
from .tests_two_step_completion import (
    OPEX_MIRROR_NAME, _client_for, _profile, _seed_opex,
)
from .utils import (
    RESIDENTIAL_FINANCE_ASSIGNEE_EMAIL, assign_task_to, resolve_residential_template,
)

# A NON-MIRROR SITE ENGINEER TASK, and not the PM task tests_two_step_completion.py
# uses. The row's status control is ROLE-gated (`user_task_role == task.assigned_role`)
# while the submit endpoint's authority is USER-level (`task.assigned_to == profile`) --
# so an SE holding a PM-role task gets the read-only badge and would prove nothing about
# the control this prompt changed. Phase 4 of the OPEX template is nine Site Engineer
# tasks; this is one of them, named so that a template change fails here loudly.
OPEX_SE_TASK_NAME = 'Module Installation'

TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), 'templates', 'projects')


def _template_source(*parts):
    with io.open(os.path.join(TEMPLATE_DIR, *parts), encoding='utf-8') as fh:
        return fh.read()


class PhaseListFixture(TestCase):
    """One really activated OPEX site plus one really activated Residential project.

    BOTH, ALWAYS. Every claim in this file is a claim about a DIFFERENCE between the two
    project types, and a fixture holding only the OPEX side would let "Done is gone" pass
    for the worst possible reason -- that it is gone everywhere.

    The site engineer holds a task here, which is also what gives them sight of the
    project at all: `user_can_view_project()` for a Site Engineer is "holds a task on it",
    so an SE with no assignment gets a 404 and every test below would fail for a reason
    that has nothing to do with approvals.
    """

    @classmethod
    def setUpTestData(cls):
        resolve_residential_template()
        _seed_opex()
        cls.pm      = _profile('pla_pm', 'PM')
        cls.se      = _profile('pla_se', 'Site Engineer')
        # project_activate() refuses a Residential activation with no designer.
        cls.designer = _profile('pla_design', 'Design')
        cls.finance = _profile('pla_fin', 'Finance',
                               email=RESIDENTIAL_FINANCE_ASSIGNEE_EMAIL)

    def setUp(self):
        self.site = self._activated_opex()
        self.task = Task.objects.get(phase__project=self.site, task_name=OPEX_SE_TASK_NAME)
        self.assertFalse(self.task.is_mirror)
        self.assertEqual(self.task.assigned_role, Task.SITE_ENGINEER)
        assign_task_to(self.task, self.se, notify=False)
        self.task.refresh_from_db()

    # -- fixture helpers ------------------------------------------------------

    def _activated_opex(self):
        site = Project.objects.create(
            customer_name='Phase-list Tender Site', customer_phone='9876543210',
            site_address='2 Approval Road', city='Lucknow', project_type='OPEX',
            dc_capacity_kw=Decimal('100.00'), status='Draft', assigned_pm=self.pm,
        )
        response = _client_for(self.pm).post(
            reverse('opex_site_activate', args=[site.project_id]))
        self.assertEqual(response.status_code, 302, 'OPEX activation did not redirect')
        site.refresh_from_db()
        self.assertEqual(site.status, 'Active')
        return site

    def _activated_residential(self):
        project = Project.objects.create(
            customer_name='Phase-list House', customer_phone='9876543211',
            site_address='3 Approval Road', city='Lucknow', project_type='Residential',
            dc_capacity_kw=Decimal('5.00'), status='Draft', assigned_pm=self.pm,
        )
        response = _client_for(self.pm).post(
            reverse('project_activate', args=[project.project_id]),
            {'assigned_design_id': self.designer.pk})
        self.assertIn(response.status_code, (200, 302))
        project.refresh_from_db()
        self.assertNotEqual(project.status, 'Draft',
                            'fixture could not activate the Residential project')
        return project

    def _overview(self, project=None, profile=None):
        project = project or self.site
        response = _client_for(profile or self.se).get(
            reverse('project_overview', args=[project.project_id]))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def _row(self, html, task):
        """The markup of exactly one task's <tr>, so an assertion about a row cannot
        pass because some OTHER row on a 23-row page happens to contain the string."""
        marker = 'id="task-row-%d"' % task.pk
        start  = html.find(marker)
        self.assertNotEqual(start, -1, 'task %d is not on the page' % task.pk)
        start  = html.rfind('<tr', 0, start)
        end    = html.find('</tr>', start)
        self.assertNotEqual(end, -1)
        return html[start:end]

    def _in_progress(self, task=None, profile=None):
        task = task or self.task
        response = _client_for(profile or self.se).post(
            reverse('task_status_update', args=[task.phase.project.project_id, task.pk]),
            {'status': Task.IN_PROGRESS,
             'due_date': (date.today() + timedelta(days=7)).isoformat()},
        )
        self.assertIn(response.status_code, (200, 302))
        task.refresh_from_db()
        self.assertEqual(task.status, Task.IN_PROGRESS,
                         'fixture could not put the task In Progress')
        return task


# ---------------------------------------------------------------------------
# 1. Done is not offered on an OPEX row -- and IS on a Residential one
# ---------------------------------------------------------------------------

class DoneIsNotOfferedTests(PhaseListFixture):

    def _options(self, row):
        return re.findall(r'<option value="([^"]+)"', row)

    def test_opex_row_offers_no_done_option(self):
        """The control no longer invites the one choice rung 1 always refuses."""
        row = self._row(self._overview(), self.task)
        self.assertIn('<select name="status"', row,
                      'the SE holding this task should get an editable status control')
        self.assertNotIn('Done', self._options(row))
        # The other three are untouched: this is a subtraction, not a rewrite.
        for value in (Task.NOT_STARTED, Task.IN_PROGRESS, Task.BLOCKED):
            self.assertIn(value, self._options(row))

    def test_an_already_done_opex_row_still_shows_done(self):
        """A select with no option matching its row displays the WRONG status silently.

        An approved task legitimately reaches Done, so the option survives on exactly
        the rows that are already there -- otherwise the control would read Not Started
        over a completed task.
        """
        Task.objects.filter(pk=self.task.pk).update(status=Task.DONE)
        row = self._row(self._overview(profile=self.pm), self.task)
        self.assertIn('Done', self._options(row))
        self.assertIn('value="Done" selected', row)

    def test_residential_row_still_offers_done(self):
        """2.1 6: Residential completes in one move, and this prompt did not change it."""
        project = self._activated_residential()
        task    = Task.objects.filter(phase__project=project, is_mirror=False).first()
        assign_task_to(task, self.pm, notify=False)
        row = self._row(self._overview(project=project, profile=self.pm), task)
        self.assertIn('Done', self._options(row))

    def test_residential_row_renders_no_approval_markup_at_all(self):
        project = self._activated_residential()
        task    = Task.objects.filter(phase__project=project, is_mirror=False).first()
        assign_task_to(task, self.pm, notify=False)
        html = self._overview(project=project, profile=self.pm)
        self.assertNotIn('task-approval-%d' % task.pk, html)
        self.assertNotIn('openSubmitApprovalModal(this)', html)
        self.assertNotIn('Awaiting approval', html)

    def test_the_refusal_itself_is_untouched(self):
        """The markup opens no door and closes none. A POST that skips the select still
        meets rung 1, so removing the option removed an invitation and not a guard."""
        self._in_progress()
        response = _client_for(self.pm).post(
            reverse('task_status_update', args=[self.site.project_id, self.task.pk]),
            {'status': Task.DONE},
        )
        self.assertIn(response.status_code, (200, 302))
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, Task.IN_PROGRESS)


# ---------------------------------------------------------------------------
# 2. A submitted task says so, in the list
# ---------------------------------------------------------------------------

class AwaitingApprovalIsVisibleTests(PhaseListFixture):

    def _submit(self):
        self._in_progress()
        response = _client_for(self.se).post(
            reverse('task_submit_for_approval', args=[self.site.project_id, self.task.pk]),
            {'submission_remarks': 'Meter installed and sealed.'},
        )
        self.assertIn(response.status_code, (200, 302))
        self.task.refresh_from_db()
        self.assertIsNotNone(self.task.submitted_at)

    def test_before_submission_the_row_offers_the_button(self):
        self._in_progress()
        row = self._row(self._overview(), self.task)
        self.assertIn('openSubmitApprovalModal(this)', row)
        self.assertIn(
            reverse('task_submit_for_approval', args=[self.site.project_id, self.task.pk]),
            row)

    def test_a_not_started_row_says_why_there_is_no_button(self):
        """`task_submit_for_approval` refuses anything not In Progress, so the row states
        the precondition instead of drawing a control that would turn the person away."""
        row = self._row(self._overview(), self.task)
        self.assertNotIn('openSubmitApprovalModal(this)', row)
        self.assertIn('Submit once In Progress', row)

    def test_after_submission_the_row_says_awaiting_approval(self):
        """THE DEFECT. The status is still In Progress by design, so without this badge
        the row is indistinguishable from one nobody has touched."""
        self._submit()
        row = self._row(self._overview(), self.task)
        self.assertIn('Awaiting approval', row)
        self.assertNotIn('openSubmitApprovalModal(this)', row)

    def test_the_pm_sees_it_waiting_too(self):
        """The approver's own list is where they find out there is something to approve."""
        self._submit()
        row = self._row(self._overview(profile=self.pm), self.task)
        self.assertIn('Awaiting approval', row)

    def test_after_approval_the_row_says_approved(self):
        self._submit()
        response = _client_for(self.pm).post(
            reverse('task_approve', args=[self.site.project_id, self.task.pk]),
            {'approval_remarks': 'Checked on site.'},
        )
        self.assertIn(response.status_code, (200, 302))
        self.task.refresh_from_db()
        self.assertIsNotNone(self.task.approved_at)
        self.assertEqual(self.task.status, Task.DONE)
        row = self._row(self._overview(profile=self.pm), self.task)
        self.assertIn('>Approved</span>', row)

    def test_a_mirror_row_gets_no_approval_block(self):
        """Rung 0 refuses a mirror before rung 1 is reached and nobody may ever submit
        one, so a Submit button there would be an offer that can never be taken up."""
        mirror = Task.objects.get(phase__project=self.site, task_name=OPEX_MIRROR_NAME)
        self.assertTrue(mirror.is_mirror)
        row = self._row(self._overview(profile=self.pm), mirror)
        self.assertNotIn('task-approval-%d' % mirror.pk, row)
        self.assertNotIn('openSubmitApprovalModal(this)', row)

    def test_a_bystander_sees_the_state_but_gets_no_button(self):
        """Someone who may look but not submit still needs to know the task is waiting."""
        outsider = _profile('pla_other_se', 'Site Engineer')
        sight    = Task.objects.filter(
            phase__project=self.site, is_mirror=False).exclude(pk=self.task.pk).first()
        assign_task_to(sight, outsider, notify=False)
        self._submit()
        row = self._row(self._overview(profile=outsider), self.task)
        self.assertIn('Awaiting approval', row)
        self.assertNotIn('openSubmitApprovalModal(this)', row)


# ---------------------------------------------------------------------------
# 3. Submitting from the row reaches the real endpoint and the row tells the truth
# ---------------------------------------------------------------------------

class SubmitFromTheRowTests(PhaseListFixture):
    """The row's form is the modal in project_overview.html, whose action is assembled
    from the button's data attributes. These tests post what that form posts.

    NO NEW ENDPOINT WAS ADDED and none was changed: the URL asserted here is the same
    `task_submit_for_approval` task_detail has always used, which is why a submit made
    from the list is indistinguishable, in the database, from one made from the detail
    page -- asserted directly in `test_the_row_submit_is_the_same_submission`.
    """

    def test_submitting_records_the_submission(self):
        self._in_progress()
        response = _client_for(self.se).post(
            reverse('task_submit_for_approval', args=[self.site.project_id, self.task.pk]),
            {'submission_remarks': 'Panels mounted, torque checked.'},
            HTTP_HX_REQUEST='true',
        )
        self.assertEqual(response.status_code, 200)
        self.task.refresh_from_db()
        self.assertIsNotNone(self.task.submitted_at)
        self.assertEqual(self.task.submitted_by, self.se)
        self.assertEqual(self.task.status, Task.IN_PROGRESS,
                         'submitting must not move the status (2.1 3)')

    def test_the_hx_response_carries_the_badge_the_row_selects(self):
        """`hx-select=".card-header .badge"` is only honest if the response actually has
        one -- and if it carries the TRUE state on a refusal as well as a success."""
        self._in_progress()
        body = _client_for(self.se).post(
            reverse('task_submit_for_approval', args=[self.site.project_id, self.task.pk]),
            {'submission_remarks': 'Done and checked.'},
            HTTP_HX_REQUEST='true',
        ).content.decode()
        self.assertIn('card-header', body)
        self.assertIn('Awaiting approval', body)

    def test_a_refused_submission_returns_the_unchanged_state(self):
        """An empty remark is refused. The badge the row swaps in must therefore still
        read 'Not submitted' -- the reason the state comes from the server and is not
        inferred client-side from a 200."""
        self._in_progress()
        body = _client_for(self.se).post(
            reverse('task_submit_for_approval', args=[self.site.project_id, self.task.pk]),
            {'submission_remarks': '   '},
            HTTP_HX_REQUEST='true',
        ).content.decode()
        self.assertIn('Not submitted', body)
        self.assertNotIn('Awaiting approval', body)
        self.task.refresh_from_db()
        self.assertIsNone(self.task.submitted_at)

    def test_the_row_submit_is_the_same_submission(self):
        """One endpoint, one record, whichever screen it came from."""
        self._in_progress()
        _client_for(self.se).post(
            reverse('task_submit_for_approval', args=[self.site.project_id, self.task.pk]),
            {'submission_remarks': 'From the list.'}, HTTP_HX_REQUEST='true')
        self.task.refresh_from_db()
        self.assertEqual(self.task.submission_remarks, 'From the list.')
        self.assertEqual(self.task.submitted_by, self.se)

    def test_the_row_block_cannot_widen_the_table(self):
        """The phase table already scrolls sideways on a phone (b4, deliberately out of
        scope here). This prompt's job was not to fix that but not to make it worse, so
        the new markup is checked for the two things that would: an unbreakable line and
        a fixed width. Every string added wraps at a space, and the widest unbreakable
        token is a single word — narrower than the 12rem column it sits in."""
        source = _template_source('partials', '_task_row_approval.html')
        markup = re.sub(r'{% comment %}.*?{% endcomment %}', '', source, flags=re.S)
        self.assertNotIn('nowrap', markup)
        self.assertNotIn('width:', markup)
        for phrase in ('Awaiting approval', 'Submit for approval', 'Submit once In Progress'):
            self.assertIn(phrase, markup)
            longest = max(len(word) for word in phrase.split())
            self.assertLessEqual(
                longest, 12,
                '%r contains a %d-character word; at .7rem that is still well inside the '
                '12rem status column, but check before letting it grow' % (phrase, longest))

    def test_the_modal_and_its_target_agree(self):
        """The button names a target the page actually contains; a typo in either would
        leave the badge un-swapped and nothing else would notice."""
        self._in_progress()
        html = self._overview()
        self.assertIn('data-target-id="#task-approval-%d"' % self.task.pk, html)
        self.assertIn('id="task-approval-%d"' % self.task.pk, html)
        self.assertIn('id="submitApprovalForm"', html)
        self.assertIn('hx-select=".card-header .badge"', html)


# ---------------------------------------------------------------------------
# 4. The row content costs nothing per task
# ---------------------------------------------------------------------------

class CostPerRowTests(PhaseListFixture):
    """The approval block must not scale the page with the task count.

    STATED AS A COMPARISON, not as a ceiling. An absolute number would pin every
    unrelated query on this page and fail on the next dashboard change; what this
    prompt is actually responsible for is the DIFFERENCE, so the test measures the
    marginal cost of a task on an OPEX list (approval block rendered) against the same
    marginal cost on a Residential list (no approval markup at all) and requires them
    to be equal.
    """

    EXTRA = 10

    def _marginal_cost(self, project, profile):
        client = _client_for(profile)
        url    = reverse('project_overview', args=[project.project_id])
        client.get(url)                                    # warm any per-session lookups
        with CaptureQueriesContext(connection) as before:
            self.assertEqual(client.get(url).status_code, 200)

        phase = ProjectPhase.objects.filter(project=project).order_by('phase_order').first()
        order = (Task.objects.filter(phase__project=project)
                 .order_by('-task_order').first().task_order) + 1
        for i in range(self.EXTRA):
            task = Task.objects.create(
                phase=phase, task_name='Cost probe %d' % i, task_order=order + i,
                assigned_role=Task.SITE_ENGINEER, task_type='Internal',
                status=Task.IN_PROGRESS,
            )
            assign_task_to(task, profile, notify=False)

        with CaptureQueriesContext(connection) as after:
            self.assertEqual(client.get(url).status_code, 200)
        return len(before), len(after), (len(after) - len(before)) / float(self.EXTRA)

    def test_an_opex_task_costs_the_same_as_a_residential_one(self):
        opex_before, opex_after, opex_cost = self._marginal_cost(self.site, self.pm)
        res_project = self._activated_residential()
        _, _, res_cost = self._marginal_cost(res_project, self.pm)
        self.assertEqual(
            opex_cost, res_cost,
            'a task on the OPEX list costs %.2f queries against %.2f on the Residential '
            'list -- the approval block is querying per row (%d -> %d over %d tasks)'
            % (opex_cost, res_cost, opex_before, opex_after, self.EXTRA),
        )

    def test_the_page_does_not_query_per_row_for_approval_authority(self):
        """The specific shape being ruled out: `user_can_manage_project()` is a
        `.exists()`, and calling it once per row was the obvious build of this feature."""
        _, _, cost = self._marginal_cost(self.site, self.pm)
        self.assertLessEqual(
            cost, 3.0,
            'a task on the OPEX phase list costs %.2f queries; the pre-existing cost is '
            'the assigned_to FK, its user, and the attachment count' % cost)


# ---------------------------------------------------------------------------
# 5. The camera opens on the one input that only ever wants a photograph
# ---------------------------------------------------------------------------

class CaptureAttributeTests(TestCase):
    """Asserted against the template SOURCE rather than a render.

    The claim is about markup that must be present on an attribute of an <input>, and
    rendering the checklist needs a Checklist, its items, a link to a task name and a
    completion-capable viewer -- four fixtures whose failure modes have nothing to do
    with the attribute under test. Reading the file asks the question directly.

    BOTH HALVES ARE PINNED. The second test is the more valuable one: `capture` on a
    multi-type attachment picker would force the camera and make uploading a PDF
    impossible, so "we did not add it there" is the part a future sweep is most likely
    to undo.
    """

    def test_the_checklist_photo_input_opens_the_camera(self):
        source = _template_source('partials', '_checklist.html')
        match  = re.search(r'<input type="file" name="photo"[^>]*>', source)
        self.assertIsNotNone(match, 'the checklist photo input has moved or changed shape')
        self.assertIn('capture="environment"', match.group(0))
        self.assertIn('required', match.group(0),
                      'the photo is still mandatory -- capture must not have replaced it')

    def test_the_mixed_type_pickers_do_not_force_the_camera(self):
        for parts in (('task_detail.html',),
                      ('project_overview.html',),
                      ('project_detail.html',)):
            source = _template_source(*parts)
            for tag in re.findall(r'<input type="file"[^>]*>', source, re.S):
                if 'name="photo"' in tag:
                    continue
                self.assertNotIn(
                    'capture=', tag,
                    '%s has a capture attribute on a multi-type picker: %s'
                    % ('/'.join(parts), tag),
                )
