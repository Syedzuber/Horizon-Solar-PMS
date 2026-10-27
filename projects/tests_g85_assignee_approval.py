"""
Nobody gives a verdict on the work assigned to them — G85, rule R-c (EXECUTION_MODULE_DEFERRED.md
G85, closed by this build; the audit of 27 Oct 2026).

WHAT IS PINNED HERE
-------------------
    refuse   the PM may not approve or reject a PM-held task a coordinator
             submitted; a coordinator may not approve or reject a
             coordinator-held task the PM submitted; one message          AssigneeVerdictTests
    withdraw the assignee who submitted their own task may still reject it   AssigneeVerdictTests
    panel    the assignee is offered no verdict form, and is told why        AssigneeVerdictTests
    pool     the assignee is out of their own task's approver pool, so on a
             site with no QA/QC engineer the submission self-certifies
             instead of deadlocking, in both directions                     NoDeadlockTests
    mid-queue a QA/QC assignment ending while a task waits leaves a way out:
             the submitter withdraws and resubmits, and it self-certifies   NoDeadlockTests
    label    the self-certify label is per row's assignee, and the modal
             carries both wordings when the rows differ                    RowLabelTests
    batch    the phase list's one-read form agrees with the predicate      PerPageFormTests
    marker   a self-certified completion reads "Approved by <name>
             (self-certified)" on the task page and its overview row        SelfCertifiedMarkerTests

Unchanged and pinned elsewhere: a sole approver submitting their own task self-certifies
(tests_approval_pool.SelfCertifiesWhenPoolIsEmptyTests); the submitter refusal's message
(tests_two_step_completion, tests_approval_pool); the QA/QC engineer's own path
(tests_qaqc_engineer_tasks).

Built on tests_punch_rectify.RectifyFixture (tests_qaqc_visit.VisitFixture): an activated
OPEX site with its PM `pm`, coordinator `coord`, QA/QC engineer `qaqc` and `worker`
holding every Site Engineer task. The PM tasks are the PM's from activation.

Run with:
    python manage.py test projects.tests_g85_assignee_approval --settings=solarpms.test_settings
"""
import re

from django.urls import reverse

from .models import Task
from .permissions import (
    task_has_independent_approver, user_may_self_certify, user_may_self_certify_by_assignee,
)
from .qaqc_views import end_site_qaqc
from .tests_punch_rectify import RectifyFixture
from .tests_qaqc_assignment import _client_for, _messages
from .utils import assign_task_to

PM_TASK = 'CEIG Approval'
COORD_TASK = 'Completion Certificates (Paperwork)'
ASSIGNEE_REFUSAL = 'is assigned to you and someone else submitted it'
SELF = 'Complete (self-certified)'
NORMAL = 'Submit for approval'


def _name(profile):
    return profile.user.get_full_name() or profile.user.username


class AssigneeFixture(RectifyFixture):

    def setUp(self):
        super().setUp()
        self.pm_task = Task.objects.get(phase__project=self.site, task_name=PM_TASK)
        self.assertEqual(self.pm_task.assigned_to, self.pm,
                         'fixture: activation gives the PM task to the site PM')
        self.coord_task = Task.objects.get(phase__project=self.site, task_name=COORD_TASK)
        assign_task_to(self.coord_task, self.coord, notify=False)
        Task.objects.filter(pk__in=[self.pm_task.pk, self.coord_task.pk, self.civil.pk]).update(
            status=Task.IN_PROGRESS)
        for task in (self.pm_task, self.coord_task, self.civil):
            task.refresh_from_db()

    def _submit(self, task, by):
        with self.captureOnCommitCallbacks(execute=True):
            response = _client_for(by).post(
                reverse('task_submit_for_approval', args=[self.site.project_id, task.pk]),
                {'submission_remarks': 'Work done.'})
        task.refresh_from_db()
        return response

    def _verdict(self, task, view, by):
        with self.captureOnCommitCallbacks(execute=True):
            response = _client_for(by).post(
                reverse(view, args=[self.site.project_id, task.pk]),
                {'approval_remarks': 'Looked at it.'})
        task.refresh_from_db()
        return response

    def _no_engineer(self):
        with self.captureOnCommitCallbacks(execute=True):
            end_site_qaqc(self.site, self.pm)

    def _row(self, body, task):
        match = re.search(rf'id="task-row-{task.pk}".*?</tr>', body, re.S)
        self.assertIsNotNone(match, f'no row for {task.task_name}')
        return match.group(0)

    def _overview(self, by):
        response = _client_for(by).get(reverse('project_overview', args=[self.site.project_id]))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()


# ---------------------------------------------------------------------------
# The refusal, both directions, both verdicts
# ---------------------------------------------------------------------------

class AssigneeVerdictTests(AssigneeFixture):
    """The site has its QA/QC engineer, so each submission queues for them."""

    def _refused_both_verdicts(self, task, assignee):
        for view in ('task_approve', 'task_reject'):
            with self.subTest(view=view):
                response = self._verdict(task, view, assignee)
                self.assertIn(ASSIGNEE_REFUSAL, ' '.join(_messages(response)))
                self.assertTrue(task.is_awaiting_approval, f'{view} acted on the task')
                self.assertIsNone(task.approved_by)
                self.assertEqual(task.status, Task.IN_PROGRESS)

    def test_the_pm_gives_no_verdict_on_their_task_a_coordinator_submitted(self):
        self._submit(self.pm_task, self.coord)
        self.assertTrue(self.pm_task.is_awaiting_approval, 'fixture: the engineer is a signature')
        self._refused_both_verdicts(self.pm_task, self.pm)
        # The engineer, a real second person, signs it off.
        self._verdict(self.pm_task, 'task_approve', self.qaqc)
        self.assertEqual(self.pm_task.status, Task.DONE)
        self.assertEqual(self.pm_task.approved_by, self.qaqc)

    def test_a_coordinator_gives_no_verdict_on_their_task_the_pm_submitted(self):
        self._submit(self.coord_task, self.pm)
        self.assertTrue(self.coord_task.is_awaiting_approval)
        self._refused_both_verdicts(self.coord_task, self.coord)
        self._verdict(self.coord_task, 'task_approve', self.qaqc)
        self.assertEqual(self.coord_task.status, Task.DONE)
        self.assertEqual(self.coord_task.approved_by, self.qaqc)

    def test_one_message_for_both_verdicts(self):
        self._submit(self.pm_task, self.coord)
        approve = ' '.join(_messages(self._verdict(self.pm_task, 'task_approve', self.pm)))
        reject = ' '.join(_messages(self._verdict(self.pm_task, 'task_reject', self.pm)))
        self.assertEqual(approve, reject)

    def test_an_assignee_who_submitted_may_still_withdraw(self):
        """Behaviour unchanged when the assignee is also the submitter: rejecting is
        withdrawing, and approving is the submitter refusal's case, with its message."""
        self._submit(self.pm_task, self.pm)
        approve = self._verdict(self.pm_task, 'task_approve', self.pm)
        self.assertIn('yourself', ' '.join(_messages(approve)))
        self.assertNotIn(ASSIGNEE_REFUSAL, ' '.join(_messages(approve)))
        self._verdict(self.pm_task, 'task_reject', self.pm)
        self.assertIsNone(self.pm_task.submitted_at, 'the withdrawal was refused')

    def test_others_still_give_verdicts(self):
        """The coordinator, who submitted nothing and holds nothing here, approves."""
        task = self._submitted()                        # the worker's Civil task
        self._verdict(task, 'task_approve', self.coord)
        self.assertEqual(task.status, Task.DONE)
        self.assertEqual(task.approved_by, self.coord)

    def test_the_panel_offers_the_assignee_no_verdict_and_says_why(self):
        self._submit(self.pm_task, self.coord)
        page = _client_for(self.pm).get(
            reverse('task_detail', args=[self.site.project_id, self.pm_task.pk]))
        self.assertFalse(page.context['can_approve_task'])
        self.assertTrue(page.context['own_task_submitted_by_other'])
        self.assertContains(page, 'someone else submitted it, so you cannot')
        self.assertNotContains(page, 'Approve &amp; complete')
        self.assertNotContains(page, reverse('task_reject',
                                             args=[self.site.project_id, self.pm_task.pk]))


# ---------------------------------------------------------------------------
# No deadlock: the assignee is out of the pool
# ---------------------------------------------------------------------------

class NoDeadlockTests(AssigneeFixture):

    def test_the_assignee_is_out_of_the_pool(self):
        self._no_engineer()
        # Beside the coordinator, the PM counts — unless the task is the PM's own.
        self.assertTrue(task_has_independent_approver(self.site, self.coord))
        self.assertFalse(task_has_independent_approver(self.site, self.coord,
                                                       assignee_id=self.pm.pk))

    def test_a_pm_task_a_coordinator_submits_self_certifies_without_an_engineer(self):
        self._no_engineer()
        self.assertTrue(user_may_self_certify(self.coord.user, self.site,
                                              assignee_id=self.pm.pk))
        self._submit(self.pm_task, self.coord)
        self.assertEqual(self.pm_task.status, Task.DONE, 'the submission queued: deadlock')
        self.assertTrue(self.pm_task.approval_self_certified)
        self.assertEqual(self.pm_task.approved_by, self.coord)
        self.assertEqual(self.pm_task.submitted_by, self.coord)

    def test_a_coordinator_task_the_pm_submits_self_certifies_without_an_engineer(self):
        self._no_engineer()
        self._submit(self.coord_task, self.pm)
        self.assertEqual(self.coord_task.status, Task.DONE, 'the submission queued: deadlock')
        self.assertTrue(self.coord_task.approval_self_certified)
        self.assertEqual(self.coord_task.approved_by, self.pm)

    def test_the_same_submitter_queues_a_task_someone_else_can_sign(self):
        """The pool is per task: the coordinator's submission of the worker's task still
        has the PM as its second signature."""
        self._no_engineer()
        self._submit(self.civil, self.coord)
        self.assertTrue(self.civil.is_awaiting_approval)
        self.assertFalse(self.civil.approval_self_certified)

    def test_an_engineer_ending_mid_queue_leaves_a_way_out(self):
        """A PM task the coordinator submitted waits for the engineer; the assignment
        ends. The PM (assignee) and the coordinator (submitter) are both refused a
        verdict, but the coordinator may withdraw, and resubmitting then self-certifies
        because the pool without them both is empty."""
        self._submit(self.pm_task, self.coord)
        self.assertTrue(self.pm_task.is_awaiting_approval, 'fixture: it waits for the engineer')
        self._no_engineer()

        self._verdict(self.pm_task, 'task_approve', self.pm)
        self._verdict(self.pm_task, 'task_approve', self.coord)
        self.assertTrue(self.pm_task.is_awaiting_approval, 'someone approved past a refusal')

        self._verdict(self.pm_task, 'task_reject', self.coord)     # withdraw
        self.assertIsNone(self.pm_task.submitted_at, 'the submitter could not withdraw')
        self._submit(self.pm_task, self.coord)
        self.assertEqual(self.pm_task.status, Task.DONE)
        self.assertTrue(self.pm_task.approval_self_certified)
        self.assertEqual(self.pm_task.approved_by, self.coord)


# ---------------------------------------------------------------------------
# The label is per row
# ---------------------------------------------------------------------------

class RowLabelTests(AssigneeFixture):

    def test_rows_with_different_assignees_get_their_own_label(self):
        """No engineer: the PM submitting the coordinator's task completes it (the pool
        without both is empty); submitting the worker's task does not (the coordinator
        signs)."""
        self._no_engineer()
        body = self._overview(self.pm)
        self.assertIn('data-self-certify="1"', self._row(body, self.coord_task))
        self.assertIn('data-self-certify="0"', self._row(body, self.civil))
        # The PM's own task: the coordinator is its second signature.
        self.assertIn('data-self-certify="0"', self._row(body, self.pm_task))
        self.assertIn('js-self-certify-yes', body)
        self.assertIn('js-self-certify-no', body)

    def test_the_task_page_label_agrees_with_the_row(self):
        self._no_engineer()
        for task, label, absent in ((self.coord_task, SELF, NORMAL),
                                    (self.civil, NORMAL, SELF)):
            with self.subTest(task=task.task_name):
                page = _client_for(self.pm).get(
                    reverse('task_detail', args=[self.site.project_id, task.pk]))
                self.assertContains(page, label)
                self.assertNotContains(page, absent)

    def test_the_modal_has_one_wording_when_every_row_agrees(self):
        """With the engineer on the site every PM submission queues, so the page renders
        the ordinary wording only."""
        body = self._overview(self.pm)
        self.assertNotIn(SELF, body)
        # The class attribute, not the bare name: the page's script names both classes.
        self.assertNotIn('class="js-self-certify-yes', body)
        self.assertIn('class="js-self-certify-no', body)


class PerPageFormTests(AssigneeFixture):
    """The phase list answers every row from one pool read
    (user_may_self_certify_by_assignee); it must give exactly what the endpoint's own
    predicate gives for each assignee, for every viewer, with and without an engineer."""

    def test_the_per_page_form_agrees_with_the_predicate(self):
        assignees = [self.pm.pk, self.coord.pk, self.worker.pk, self.qaqc.pk, None]
        viewers = (self.pm, self.coord, self.worker, self.qaqc)
        for shape in ('engineer', 'no engineer'):
            if shape == 'no engineer':
                self._no_engineer()
            for viewer in viewers:
                batch = user_may_self_certify_by_assignee(viewer.user, self.site, assignees)
                for assignee_id in assignees:
                    with self.subTest(shape=shape, viewer=viewer.user.username,
                                      assignee=assignee_id):
                        self.assertEqual(batch[assignee_id], user_may_self_certify(
                            viewer.user, self.site, assignee_id=assignee_id))


# ---------------------------------------------------------------------------
# B5: the self-certified marker
# ---------------------------------------------------------------------------

class SelfCertifiedMarkerTests(AssigneeFixture):

    def test_the_task_page_and_the_row_say_self_certified(self):
        self._no_engineer()
        self._submit(self.pm_task, self.coord)
        self.assertTrue(self.pm_task.approval_self_certified)
        marker = f'{_name(self.coord)} (self-certified)'

        page = _client_for(self.pm).get(
            reverse('task_detail', args=[self.site.project_id, self.pm_task.pk]))
        self.assertContains(page, marker)

        row = self._row(self._overview(self.pm), self.pm_task)
        self.assertIn(f'Approved by {marker}', row)

    def test_an_ordinary_approval_has_no_marker(self):
        self._submit(self.pm_task, self.coord)
        self._verdict(self.pm_task, 'task_approve', self.qaqc)
        self.assertFalse(self.pm_task.approval_self_certified)
        page = _client_for(self.pm).get(
            reverse('task_detail', args=[self.site.project_id, self.pm_task.pk]))
        self.assertNotContains(page, '(self-certified)')
        self.assertNotIn('(self-certified)', self._row(self._overview(self.pm), self.pm_task))
