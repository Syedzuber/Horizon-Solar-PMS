"""
An OPEX task is finished by two people. Prompt 2.1.

WHAT THE FEATURE IS
-------------------
On an OPEX site, a task no longer goes to Done because the person holding it said
so. The holder SUBMITS it with an account of the work; the PM or a QA/QC holder
APPROVES it with remarks, and only that approval unlocks Done. A rejection clears
the submission and hands the task back.

Residential is untouched and stays a one-move completion (2.1 §6). That is not a
side effect of this file's fixtures — it is the first thing asserted, in
`ResidentialIsUntouchedTests`, because "we did not break the other path" is the
claim most likely to be true today and false after the next edit.

WHERE THE RULE ACTUALLY LIVES, AND WHY THAT SHAPES THIS FILE
-----------------------------------------------------------
The Done gate is ONE rung in `_apply_task_status_change()`, the single decision
path for task status (R-18). It is therefore reachable from BOTH status screens,
and `TheRungTests` runs the refusal through both as two subclasses of one mixin —
the same shape `tests_mirror_readonly.py` uses, for the same reason: a rule that
held on the overview row and not on the task-detail block would be a rule with a
hole in it, and the hole would be a screen nobody thought to test.

THE ORDERING CLAIM IS TESTED, NOT ASSUMED
-----------------------------------------
The new rung sits BELOW the mirror rung. That is not decoration: an assigned OPEX
mirror satisfies both refusals, and which message it gets decides whether the user
is told "go and get this approved" (untrue — nobody may ever approve a mirror,
because nobody may ever submit one) or "this is derived from somewhere else"
(true, and actionable). `RungOrderingTests` pins the mirror message on exactly
that overlap, so a later edit that reorders the ladder fails a named test rather
than degrading a message.

EVERY OPEX FIXTURE IS A REALLY ACTIVATED SITE. The tasks under test are the rows
`attach_opex_template()` produced, not hand-made `Task`s — a hand-made row proves
the `if` works and nothing about whether production reaches it.

Run with:
    python manage.py test projects.tests_two_step_completion --settings=solarpms.test_settings
"""
from datetime import date, timedelta
from decimal import Decimal
from importlib import import_module

from django.contrib.auth.models import User
from django.contrib.messages import get_messages
from django.test import Client, TestCase
from django.urls import reverse

from .models import (
    ActivityLog, Project, StatusTransition, Task, TaskTemplate,
    TaskTemplatePhase, TaskTemplateTask, UserProfile, SUBJECT_TASK,
)
from .permissions import user_can_approve_task, user_can_submit_task_for_approval
from .reports import build_user_status_rows
from .utils import (
    RESIDENTIAL_FINANCE_ASSIGNEE_EMAIL, assign_task_to,
    resolve_residential_template,
)


# A non-mirror PM task the OPEX attach pre-assigns to the site PM. Named rather
# than looked up by `is_mirror=False` so that a template change which removes it
# fails loudly here instead of silently selecting some other row.
OPEX_TASK_NAME = 'Net Metering Approval'

# The mirror used for the overlap tests. Any of the eight would do; COD is the one
# tests_mirror_readonly.py already uses, so a fixture surprise shows up in both.
OPEX_MIRROR_NAME = 'COD'


class _ConcreteApps:
    """Stands in for the `apps` registry a RunPython function is handed.
    Copied from tests_mirror_readonly.py — the seed only calls apps.get_model()."""

    _MODELS = {
        'TaskTemplate':      TaskTemplate,
        'TaskTemplatePhase': TaskTemplatePhase,
        'TaskTemplateTask':  TaskTemplateTask,
        'Task':              Task,
    }

    def get_model(self, app_label, model_name):
        assert app_label == 'projects'
        return self._MODELS[model_name]


def _seed_opex():
    module = import_module('projects.migrations.0075_seed_opex_template_v1')
    module.seed_opex_v1(_ConcreteApps(), None)


def _profile(username, role, email='', **flags):
    """Create a user and give their profile `role` (plus any capability flags).

    signals.py creates the profile on post_save with the model's DEFAULT role, so
    this UPDATES rather than creates — a get_or_create here returns the signal's
    profile with the wrong role and every role gate then refuses.
    """
    user = User.objects.create_user(
        username=username, password='x', email=email,
        first_name=username.title(), last_name='Test',
    )
    profile, _ = UserProfile.objects.get_or_create(user=user)
    profile.role = role
    for field, value in flags.items():
        setattr(profile, field, value)
    profile.save(update_fields=['role', *flags])
    return profile


def _client_for(profile):
    client = Client()
    client.force_login(profile.user)
    return client


class TwoStepFixture(TestCase):
    """One really activated OPEX site, its PM, a QA/QC holder who can see it, and
    an outsider who cannot.

    THE PM IS THE ACTOR FOR MOST TESTS, for the reason tests_mirror_readonly.py
    gives: the two status screens do not admit the same people (overview is
    role-or-PM, detail is assignee-only), so a contract that must hold on both
    needs someone who satisfies both. The site's PM, holding the task, is that
    person. It is also the realistic OPEX shape — a small site where the PM is the
    only manager present.

    THE QA/QC HOLDER IS A SITE ENGINEER WITH `is_qaqc=True` WHO HOLDS A TASK HERE,
    not a portfolio role. `user_can_approve_task()` ands the flag with
    `user_can_view_project()`, and a Site Engineer's visibility IS "holds a task on
    this project" — so the fixture has to give them one, and `test_qaqc_*` would
    pass for the wrong reason if it did not.
    """

    @classmethod
    def setUpTestData(cls):
        resolve_residential_template()   # bootstraps RESIDENTIAL v1 on a virgin DB
        _seed_opex()

        cls.pm      = _profile('t21_pm', 'PM')
        cls.qaqc    = _profile('t21_qaqc', 'Site Engineer', is_qaqc=True)
        # Same role and no flag: proves the refusals below are about the FLAG and
        # the ownership, not about the role string.
        cls.other   = _profile('t21_other', 'Site Engineer')
        cls.finance = _profile('t21_fin', 'Finance',
                               email=RESIDENTIAL_FINANCE_ASSIGNEE_EMAIL)

    def setUp(self):
        self.site = Project.objects.create(
            customer_name='2.1 Tender Site',
            customer_phone='9876543210',
            site_address='1 Approval Road',
            city='Lucknow',
            project_type='OPEX',
            capacity_kw=Decimal('100.00'),
            status='Draft',
            assigned_pm=self.pm,
        )
        response = _client_for(self.pm).post(
            reverse('opex_site_activate', args=[self.site.project_id]))
        self.assertEqual(response.status_code, 302, 'OPEX activation did not redirect')
        self.site.refresh_from_db()
        self.assertEqual(self.site.status, 'Active')

        self.task = self._opex_task()

        # The QA/QC holder's visibility of this site. See the class docstring: without
        # a task here, user_can_view_project() is False for a Site Engineer and every
        # approval test would fail for a reason that has nothing to do with 2.1.
        sight = Task.objects.filter(
            phase__project=self.site, is_mirror=False,
        ).exclude(pk=self.task.pk).first()
        assign_task_to(sight, self.qaqc, notify=False)

    # -- fixture helpers ------------------------------------------------------

    def _opex_task(self):
        task = Task.objects.filter(
            phase__project=self.site, task_name=OPEX_TASK_NAME).first()
        self.assertIsNotNone(task, 'the OPEX attach produced no task named '
                                   f'{OPEX_TASK_NAME!r}')
        self.assertFalse(task.is_mirror, 'the task under test must not be a mirror')
        self.assertEqual(task.assigned_to, self.pm,
                         'the attach pre-assigns non-mirror PM tasks to the site PM')
        return task

    def _in_progress(self, task=None):
        """Move the task to In Progress through the real screen.

        A due_date rides along because `_apply_task_status_change()` refuses In
        Progress without one, and the OPEX attach leaves due dates null. Going
        through the view rather than `.update()` keeps the starting state one the
        product can actually produce.
        """
        task = task or self.task
        response = _client_for(self.pm).post(
            reverse('task_status_update', args=[self.site.project_id, task.pk]),
            {'status': Task.IN_PROGRESS,
             'due_date': (date.today() + timedelta(days=7)).isoformat()},
        )
        self.assertIn(response.status_code, (200, 302))
        task.refresh_from_db()
        self.assertEqual(task.status, Task.IN_PROGRESS,
                         'fixture could not put the task In Progress')
        return task

    def _post(self, url_name, data=None, profile=None, task=None):
        task = task or self.task
        return _client_for(profile or self.pm).post(
            reverse(url_name, args=[self.site.project_id, task.pk]), data or {})

    def _messages(self, response):
        return [str(m) for m in get_messages(response.wsgi_request)]

    def _ledger(self, task):
        return list(StatusTransition.objects.filter(
            subject_type=SUBJECT_TASK, subject_id=task.pk))

    def _codes(self, task):
        return list(ActivityLog.objects.filter(
            entity_type='Task', entity_id=task.pk,
        ).values_list('action_code', flat=True))


# ---------------------------------------------------------------------------
# 1. The rung: Done is refused without an approval, on BOTH screens
# ---------------------------------------------------------------------------

class RungContract:
    """The Done refusal is a property of `_apply_task_status_change()`, so it must
    hold identically from either entry point. ENTRY is set by the subclasses."""

    ENTRY = None

    def _status_post(self, task, status, **extra):
        data = {'status': status}
        data.update(extra)
        return _client_for(self.pm).post(
            reverse(self.ENTRY, args=[self.site.project_id, task.pk]), data)

    def test_an_unapproved_opex_task_is_refused_done(self):
        task = self._in_progress()
        response = self._status_post(task, Task.DONE)
        task.refresh_from_db()
        self.assertEqual(
            task.status, Task.IN_PROGRESS,
            'an unapproved OPEX task reached Done — the 2.1 rung did not fire')
        self.assertIsNone(task.completed_at)

    def test_the_refusal_names_the_rule(self):
        """The message has to say what to DO, not merely that the answer is no —
        the same standard the mirror refusal above it is held to."""
        task = self._in_progress()
        response = self._status_post(task, Task.DONE)
        joined = ' '.join(self._messages(response)).lower()
        self.assertIn('submitted for approval', joined)
        self.assertIn('approved', joined)

    def test_a_refused_completion_writes_nothing(self):
        """Rung 1 sits above the inline due_date write and above every ledger call,
        so a refused move must leave no trace at all — no transition row, no
        activity line, no completed_at."""
        task = self._in_progress()
        before_ledger = len(self._ledger(task))
        before_logs   = len(self._codes(task))

        self._status_post(task, Task.DONE)

        self.assertEqual(len(self._ledger(task)), before_ledger,
                         'a refused completion wrote a StatusTransition row')
        self.assertEqual(len(self._codes(task)), before_logs,
                         'a refused completion wrote an ActivityLog row')

    def test_the_other_transitions_are_untouched(self):
        """The rung is about Done and only Done. Not Started -> In Progress still
        works on an OPEX task, or the rung has been written too widely."""
        task = self.task
        self.assertEqual(task.status, Task.NOT_STARTED)
        self._status_post(task, Task.IN_PROGRESS,
                          due_date=(date.today() + timedelta(days=7)).isoformat())
        task.refresh_from_db()
        self.assertEqual(task.status, Task.IN_PROGRESS)


class RungFromOverviewTests(RungContract, TwoStepFixture):
    ENTRY = 'task_status_update'


class RungFromTaskDetailTests(RungContract, TwoStepFixture):
    ENTRY = 'task_detail_status_update'


# ---------------------------------------------------------------------------
# 2. Rung ordering — a mirror is still refused AS A MIRROR
# ---------------------------------------------------------------------------

class RungOrderingTests(TwoStepFixture):
    """An OPEX mirror satisfies BOTH refusals. The mirror one must win.

    This is the test that makes "the new rung sits below rung 0" a fact rather
    than a claim in a comment. It has to assign the mirror first, or the
    unassigned gate in both views refuses it before either rung is reached and
    the test proves nothing — the trap tests_mirror_readonly.py documents.
    """

    def _assigned_mirror(self):
        task = Task.objects.filter(
            phase__project=self.site, task_name=OPEX_MIRROR_NAME).first()
        self.assertIsNotNone(task)
        self.assertTrue(task.is_mirror)
        assign_task_to(task, self.pm, notify=False)
        task.refresh_from_db()
        self.assertEqual(task.assigned_to, self.pm,
                         'THE TRAP: an unassigned mirror is refused before either rung')
        return task

    def test_an_opex_mirror_asked_for_done_gets_the_mirror_message(self):
        task = self._assigned_mirror()
        response = _client_for(self.pm).post(
            reverse('task_status_update', args=[self.site.project_id, task.pk]),
            {'status': Task.DONE})
        joined = ' '.join(self._messages(response)).lower()
        self.assertIn('mirror task', joined,
                      'the approval rung answered before the mirror rung — the '
                      'ladder has been reordered')
        self.assertNotIn('submitted for approval', joined)

    def test_a_mirror_cannot_be_submitted(self):
        task = self._assigned_mirror()
        response = self._post('task_submit_for_approval',
                              {'submission_remarks': 'done'}, task=task)
        task.refresh_from_db()
        self.assertIsNone(task.submitted_at,
                          'a mirror was submitted for approval')
        self.assertIn('mirror task', ' '.join(self._messages(response)).lower())

    def test_a_mirror_cannot_be_approved(self):
        task = self._assigned_mirror()
        response = self._post('task_approve', {'approval_remarks': 'ok'}, task=task)
        task.refresh_from_db()
        self.assertIsNone(task.approved_at, 'a mirror was approved')
        self.assertNotEqual(task.status, Task.DONE)


# ---------------------------------------------------------------------------
# 3. Submit
# ---------------------------------------------------------------------------

class SubmitTests(TwoStepFixture):

    def test_a_submission_records_who_when_and_what(self):
        task = self._in_progress()
        self._post('task_submit_for_approval',
                   {'submission_remarks': 'Meter installed and sealed.'})
        task.refresh_from_db()
        self.assertEqual(task.submitted_by, self.pm)
        self.assertIsNotNone(task.submitted_at)
        self.assertEqual(task.submission_remarks, 'Meter installed and sealed.')

    def test_a_submission_does_not_move_the_status(self):
        """'Submitted' is not a fifth status. The daily report's four columns
        partition on status and would stop summing if it were."""
        task = self._in_progress()
        self._post('task_submit_for_approval', {'submission_remarks': 'done'})
        task.refresh_from_db()
        self.assertEqual(task.status, Task.IN_PROGRESS)

    def test_a_submission_writes_no_status_transition(self):
        """The ledger records status CHANGES. An 'In Progress -> In Progress' row
        would be a transition that never happened."""
        task = self._in_progress()
        before = len(self._ledger(task))
        self._post('task_submit_for_approval', {'submission_remarks': 'done'})
        self.assertEqual(len(self._ledger(task)), before)
        self.assertIn('task_submitted_for_approval', self._codes(task))

    def test_remarks_are_required_and_nothing_is_written_without_them(self):
        task = self._in_progress()
        response = self._post('task_submit_for_approval', {'submission_remarks': '   '})
        task.refresh_from_db()
        self.assertIsNone(task.submitted_at,
                          'a submission with no remark was accepted')
        self.assertIn('describe the work',
                      ' '.join(self._messages(response)).lower())

    def test_a_task_that_has_not_started_cannot_be_submitted(self):
        self.assertEqual(self.task.status, Task.NOT_STARTED)
        response = self._post('task_submit_for_approval',
                              {'submission_remarks': 'done'})
        self.task.refresh_from_db()
        self.assertIsNone(self.task.submitted_at)
        self.assertIn('in progress', ' '.join(self._messages(response)).lower())

    def test_a_second_submission_is_refused_while_one_is_outstanding(self):
        task = self._in_progress()
        self._post('task_submit_for_approval', {'submission_remarks': 'first'})
        task.refresh_from_db()
        first_at = task.submitted_at

        self._post('task_submit_for_approval', {'submission_remarks': 'second'})
        task.refresh_from_db()
        self.assertEqual(task.submitted_at, first_at,
                         'a re-submit reset the clock on an outstanding submission')
        self.assertEqual(task.submission_remarks, 'first')

    def test_the_assignee_may_submit_and_a_stranger_may_not(self):
        task = self._in_progress()
        # The QA/QC holder can SEE this project but does not hold this task and is
        # not its PM — approval authority is not submission authority.
        response = self._post('task_submit_for_approval',
                              {'submission_remarks': 'done'}, profile=self.qaqc)
        self.assertEqual(response.status_code, 403)
        task.refresh_from_db()
        self.assertIsNone(task.submitted_at)

    def test_the_permission_helper_admits_assignee_and_pm_only(self):
        self.assertTrue(user_can_submit_task_for_approval(
            self.pm.user, self.task, self.site))
        self.assertFalse(user_can_submit_task_for_approval(
            self.qaqc.user, self.task, self.site))
        self.assertFalse(user_can_submit_task_for_approval(
            self.other.user, self.task, self.site))


# ---------------------------------------------------------------------------
# 4. Approve
# ---------------------------------------------------------------------------

class ApproveTests(TwoStepFixture):

    def _submitted(self):
        task = self._in_progress()
        self._post('task_submit_for_approval', {'submission_remarks': 'work done'})
        task.refresh_from_db()
        self.assertIsNotNone(task.submitted_at)
        return task

    def test_approval_completes_the_task_through_the_status_function(self):
        task = self._submitted()
        self._post('task_approve', {'approval_remarks': 'Inspected on site.'})
        task.refresh_from_db()
        self.assertEqual(task.status, Task.DONE)
        self.assertIsNotNone(task.completed_at)
        self.assertEqual(task.approved_by, self.pm)
        self.assertIsNotNone(task.approved_at)
        self.assertEqual(task.approval_remarks, 'Inspected on site.')

    def test_the_completion_is_recorded_in_the_ledger_exactly_once(self):
        """Approval routes through `_apply_task_status_change()` rather than saving
        the status itself, so the Done transition is written by the same code that
        writes every other one — once, with the actor and the from-status."""
        task = self._submitted()
        self._post('task_approve', {'approval_remarks': 'ok'})
        rows = [r for r in self._ledger(task) if r.to_status == Task.DONE]
        self.assertEqual(len(rows), 1, 'the Done transition was not written exactly once')
        self.assertEqual(rows[0].from_status, Task.IN_PROGRESS)
        self.assertIn('task_status_done', self._codes(task))
        self.assertIn('task_approved', self._codes(task))

    def test_an_unsubmitted_task_cannot_be_approved(self):
        task = self._in_progress()
        response = self._post('task_approve', {'approval_remarks': 'ok'})
        task.refresh_from_db()
        self.assertIsNone(task.approved_at)
        self.assertEqual(task.status, Task.IN_PROGRESS)
        self.assertIn('not been submitted', ' '.join(self._messages(response)).lower())

    def test_approval_remarks_are_required_and_nothing_is_written_without_them(self):
        task = self._submitted()
        response = self._post('task_approve', {'approval_remarks': ''})
        task.refresh_from_db()
        self.assertIsNone(task.approved_at)
        self.assertEqual(task.status, Task.IN_PROGRESS)
        self.assertIn('approval remarks', ' '.join(self._messages(response)).lower())

    def test_a_qaqc_holder_who_can_see_the_project_may_approve(self):
        task = self._submitted()
        self._post('task_approve', {'approval_remarks': 'QA pass'},
                   profile=self.qaqc)
        task.refresh_from_db()
        self.assertEqual(task.status, Task.DONE)
        self.assertEqual(task.approved_by, self.qaqc)

    def test_the_same_role_without_the_flag_may_not_approve(self):
        """`self.other` is a Site Engineer exactly like `self.qaqc`, minus the flag
        and minus any relationship to this site. Both refusals matter, so this is
        the test that says the flag is doing the work and the role is not.

        404 AND NOT 403, DELIBERATELY. A Site Engineer's visibility is "holds a task
        on this project", so someone with no relationship to the site fails
        `user_can_view_project()` and is refused by SCOPE before authority is ever
        asked — the 0.2 lockdown rule every other task endpoint follows. A 403 here
        would mean the scope check had been dropped and the endpoint was
        acknowledging the existence of a project this person may not see."""
        task = self._submitted()
        response = self._post('task_approve', {'approval_remarks': 'sneaking in'},
                              profile=self.other)
        self.assertEqual(response.status_code, 404)
        task.refresh_from_db()
        self.assertIsNone(task.approved_at)
        self.assertEqual(task.status, Task.IN_PROGRESS)

    def test_the_permission_helper_admits_pm_and_scoped_qaqc_only(self):
        self.assertTrue(user_can_approve_task(self.pm.user, self.site))
        self.assertTrue(user_can_approve_task(self.qaqc.user, self.site))
        self.assertFalse(user_can_approve_task(self.other.user, self.site))

    def test_an_approved_task_is_not_approved_twice(self):
        task = self._submitted()
        self._post('task_approve', {'approval_remarks': 'first'})
        task.refresh_from_db()
        first_at = task.approved_at

        self._post('task_approve', {'approval_remarks': 'second'}, profile=self.qaqc)
        task.refresh_from_db()
        self.assertEqual(task.approved_at, first_at)
        self.assertEqual(task.approved_by, self.pm)
        self.assertEqual(task.approval_remarks, 'first')

    def test_an_approved_task_may_then_go_to_done_from_the_ordinary_screen(self):
        """The gate is `approved_at`, not "came through task_approve". Once a task
        is approved the ordinary status control completes it like any other — which
        is what makes the approval a key rather than a private back door."""
        task = self._submitted()
        Task.objects.filter(pk=task.pk).update(
            approved_by=self.pm, approved_at=task.submitted_at,
            approval_remarks='out of band')
        response = _client_for(self.pm).post(
            reverse('task_status_update', args=[self.site.project_id, task.pk]),
            {'status': Task.DONE})
        task.refresh_from_db()
        self.assertEqual(task.status, Task.DONE)


# ---------------------------------------------------------------------------
# 5. Reject
# ---------------------------------------------------------------------------

class RejectTests(TwoStepFixture):

    def _submitted(self):
        task = self._in_progress()
        self._post('task_submit_for_approval', {'submission_remarks': 'work done'})
        task.refresh_from_db()
        return task

    def test_rejection_clears_the_submission_and_keeps_the_task_in_progress(self):
        task = self._submitted()
        self._post('task_reject', {'approval_remarks': 'Earthing not tested.'})
        task.refresh_from_db()
        self.assertIsNone(task.submitted_by)
        self.assertIsNone(task.submitted_at)
        self.assertEqual(task.submission_remarks, '')
        self.assertEqual(task.approval_remarks, 'Earthing not tested.')
        self.assertEqual(task.status, Task.IN_PROGRESS)

    def test_rejection_leaves_no_approver(self):
        """`approved_at` is the Done gate. A rejection that set it — or left it set
        from a previous round — would complete the task it just refused."""
        task = self._submitted()
        self._post('task_reject', {'approval_remarks': 'no'})
        task.refresh_from_db()
        self.assertIsNone(task.approved_at)
        self.assertIsNone(task.approved_by)

    def test_a_rejected_task_still_cannot_reach_done(self):
        task = self._submitted()
        self._post('task_reject', {'approval_remarks': 'no'})
        _client_for(self.pm).post(
            reverse('task_status_update', args=[self.site.project_id, task.pk]),
            {'status': Task.DONE})
        task.refresh_from_db()
        self.assertEqual(task.status, Task.IN_PROGRESS)

    def test_a_rejected_task_can_be_resubmitted_and_then_approved(self):
        """The full round trip. This is the test that says a rejection is a
        hand-back and not a dead end."""
        task = self._submitted()
        self._post('task_reject', {'approval_remarks': 'Earthing not tested.'})

        self._post('task_submit_for_approval',
                   {'submission_remarks': 'Earthing tested, results attached.'})
        task.refresh_from_db()
        self.assertIsNotNone(task.submitted_at)
        self.assertEqual(task.submission_remarks,
                         'Earthing tested, results attached.')
        # The rejection's text does not survive into the new round.
        self.assertEqual(task.approval_remarks, '')

        self._post('task_approve', {'approval_remarks': 'Verified.'})
        task.refresh_from_db()
        self.assertEqual(task.status, Task.DONE)

    def test_rejection_requires_remarks(self):
        task = self._submitted()
        response = self._post('task_reject', {'approval_remarks': ' '})
        task.refresh_from_db()
        self.assertIsNotNone(task.submitted_at, 'the submission was cleared anyway')
        self.assertIn('why the work', ' '.join(self._messages(response)).lower())

    def test_an_unsubmitted_task_cannot_be_rejected(self):
        task = self._in_progress()
        response = self._post('task_reject', {'approval_remarks': 'no'})
        task.refresh_from_db()
        self.assertEqual(task.approval_remarks, '')
        self.assertIn('nothing to reject', ' '.join(self._messages(response)).lower())

    def test_an_approved_task_cannot_be_rejected(self):
        task = self._submitted()
        self._post('task_approve', {'approval_remarks': 'ok'})
        response = self._post('task_reject', {'approval_remarks': 'changed my mind'})
        task.refresh_from_db()
        self.assertEqual(task.status, Task.DONE)
        self.assertEqual(task.approval_remarks, 'ok')
        self.assertIn('already been approved',
                      ' '.join(self._messages(response)).lower())

    def test_a_stranger_may_not_reject(self):
        """404 by scope, as in ApproveTests — see the note there."""
        task = self._submitted()
        response = self._post('task_reject', {'approval_remarks': 'no'},
                              profile=self.other)
        self.assertEqual(response.status_code, 404)
        task.refresh_from_db()
        self.assertIsNotNone(task.submitted_at)


# ---------------------------------------------------------------------------
# 6. Residential is untouched
# ---------------------------------------------------------------------------

class ResidentialIsUntouchedTests(TestCase):
    """A residential task completes in ONE move, by the person holding it, exactly
    as it did before 2.1 existed (§6).

    A separate fixture from the OPEX one on purpose: this is the claim the rest of
    the file could quietly falsify, so it is asserted against a real activated
    house rather than inferred from the absence of an OPEX flag.
    """

    @classmethod
    def setUpTestData(cls):
        resolve_residential_template()
        cls.pm       = _profile('t21r_pm', 'PM')
        cls.designer = _profile('t21r_des', 'Design')
        cls.finance  = _profile('t21r_fin', 'Finance',
                                email=RESIDENTIAL_FINANCE_ASSIGNEE_EMAIL)

    def setUp(self):
        # Activated THROUGH THE REAL VIEW, and therefore carrying the designer and
        # the target date the residential path requires — the shape
        # tests_residential_baseline.py's own fixture uses, so a house here is the
        # same house the 92 characterisation tests run against.
        self.project = Project.objects.create(
            customer_name='2.1 House',
            customer_phone='9876543211',
            site_address='2 Baseline Lane',
            city='Lucknow',
            project_type='Residential',
            capacity_kw=Decimal('5.00'),
            status='Draft',
            assigned_pm=self.pm,
            target_commissioning_date=date.today() + timedelta(days=90),
        )
        response = _client_for(self.pm).post(
            reverse('project_activate', args=[self.project.project_id]),
            {'assigned_design_id': self.designer.pk},
        )
        self.assertEqual(response.status_code, 302)
        self.project.refresh_from_db()
        self.assertEqual(self.project.status, 'Active')

        self.task = Task.objects.filter(
            phase__project=self.project, assigned_role=Task.PM, is_mirror=False,
        ).first()
        assign_task_to(self.task, self.pm, notify=False)
        self.task.refresh_from_db()

    def test_a_residential_task_goes_straight_to_done(self):
        client = _client_for(self.pm)
        client.post(
            reverse('task_status_update',
                    args=[self.project.project_id, self.task.pk]),
            {'status': Task.IN_PROGRESS,
             'due_date': (date.today() + timedelta(days=3)).isoformat()})
        client.post(
            reverse('task_status_update',
                    args=[self.project.project_id, self.task.pk]),
            {'status': Task.DONE})
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, Task.DONE,
                         'the 2.1 rung leaked onto the residential path')
        self.assertIsNotNone(self.task.completed_at)
        # No approval was involved, and the columns stay null for the life of the row.
        self.assertIsNone(self.task.submitted_at)
        self.assertIsNone(self.task.approved_at)

    def test_the_approval_endpoints_refuse_a_residential_task(self):
        response = _client_for(self.pm).post(
            reverse('task_submit_for_approval',
                    args=[self.project.project_id, self.task.pk]),
            {'submission_remarks': 'done'})
        self.task.refresh_from_db()
        self.assertIsNone(self.task.submitted_at,
                          'a residential task was put into the approval workflow')
        joined = ' '.join(str(m) for m in get_messages(response.wsgi_request)).lower()
        self.assertIn('opex sites only', joined)


# ---------------------------------------------------------------------------
# 7. The CEO daily report still adds up
# ---------------------------------------------------------------------------

class DailyReportInvariantTests(TwoStepFixture):
    """`not_started + in_progress + completed + blocked == tasks_assigned`.

    The four columns partition rows BY STATUS. Submission does not touch status —
    that is the whole reason it was built as columns beside the status rather than
    as a fifth STATUS_CHOICES value — so a task sitting submitted-and-unapproved
    must still land in exactly one of the four. This test is what would fail if
    someone later "tidied" submission into the status vocabulary.
    """

    def _row_for(self, profile):
        report = build_user_status_rows(date.today())
        for row in report['rows']:
            if row['profile'].pk == profile.pk:
                return row
        self.fail(f'{profile.user.username} is missing from the daily report')

    def test_the_four_columns_sum_for_a_user_with_a_submitted_task(self):
        task = self._in_progress()
        self._post('task_submit_for_approval', {'submission_remarks': 'done'})
        task.refresh_from_db()
        self.assertIsNotNone(task.submitted_at, 'fixture did not submit the task')
        self.assertIsNone(task.approved_at)

        row = self._row_for(self.pm)
        self.assertEqual(
            row['not_started'] + row['in_progress'] + row['completed'] + row['blocked'],
            row['tasks_assigned'],
            'the four status columns stopped partitioning Tasks Assigned',
        )
        self.assertGreaterEqual(row['in_progress'], 1,
                                'the submitted task left the In Progress column')

    def test_the_totals_row_sums_too(self):
        task = self._in_progress()
        self._post('task_submit_for_approval', {'submission_remarks': 'done'})
        totals = build_user_status_rows(date.today())['totals']
        self.assertEqual(
            totals['not_started'] + totals['in_progress']
            + totals['completed'] + totals['blocked'],
            totals['tasks_assigned'],
        )
