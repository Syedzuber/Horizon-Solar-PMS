"""
When nobody else can approve, the submission IS the approval. Self-certification.

WHAT WENT WRONG, AND WHY IT IS THIS FILE AND NOT AN EDIT TO tests_two_step_completion
-------------------------------------------------------------------------------------
2.1 made an OPEX task take two signatures, and a later guard in `task_approve`
refused the second one when it would have come from the same person as the first.
Both halves are right. Together, on the sites the company actually runs, they were
a deadlock: production on 20 Sep 2026 had NOUGHT `is_qaqc` holders company-wide and
NOUGHT OPEX projects with a coordinator, so `user_can_approve_task()` admitted
exactly one person per project — the assigned PM — and the guard then removed them
the moment they were also the submitter. Two live tasks sat in "Awaiting approval"
with an empty approver pool and no action available to anyone in the product.

`tests_two_step_completion.py` cannot hold these tests, and that is the point. Its
fixture deliberately CONTAINS a QA/QC holder with sight of the site, because the
feature it pins needs a second signatory to exist. This file's subject is the pool
ITSELF — who is in it, and what the product does when it empties — so the fixture
here has no `is_qaqc` holder and no coordinator unless a test puts one there. The
membership of the approver pool is the variable under test, so it cannot be part
of a shared fixture.

THE TRIGGER IS THE EMPTY POOL, NOT A ROLE
-----------------------------------------
The rule implemented is `permissions.user_may_self_certify()`, and it has TWO
terms: the submitter passes `user_can_approve_task()` for the project, AND no one
else does. It is not "skip the step when the task is assigned to the PM", and it is
not "the pool is empty" either.

  * Not role-shaped. `SelfCertifiesWhenPoolIsEmptyTests` pins both live shapes
    against that — a PM submitting a task assigned to THEMSELVES (task 2071) and a
    PM submitting one assigned to SOMEBODY ELSE (task 2094). A rule reading the
    assignee gets the first right and the second wrong.
  * Not the empty pool alone. `DeactivatedPmTests` pins THAT — a site engineer on a
    project whose PM has been deactivated faces a genuinely empty pool and still may
    not sign, because they never held the signature they would be collapsing. An
    empty pool is not a licence; it only removes the second signature from somebody
    who already had the first.

Those two classes fail in opposite directions, which is the point: one catches a
rule narrowed to a role, the other catches a rule widened to anyone standing there.

THE OTHER DIRECTION IS PINNED JUST AS HARD. `NormalPathIsUntouchedTests` and
`SelfApprovalGuardStillRefusesTests` exist because the risk of this change is not
that self-certification fails to fire — it is that it fires when a second
signatory WAS available, quietly turning a two-signature rule into a one-signature
one on every site that has a coordinator. Every test in those classes asserts the
task is still sitting unapproved at the end.

EVERY FIXTURE IS A REALLY ACTIVATED SITE, and the task under test is the row
`attach_opex_template()` produced — the same standard `tests_two_step_completion`
sets, and for the same reason: a hand-made Task proves the `if` works and nothing
about whether production reaches it. The helpers are imported from that module
rather than copied so the two files cannot drift about what an OPEX site is.

Run with:
    python manage.py test projects.tests_approval_pool --settings=solarpms.test_settings
"""
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.contrib.messages import get_messages
from django.test import Client, TestCase
from django.urls import reverse

from .models import (
    ActivityLog, Project, StatusTransition, Task, UserProfile, SUBJECT_TASK,
)
from .permissions import (
    task_has_independent_approver, user_can_approve_task, user_may_self_certify,
)
from .utils import assign_task_to, resolve_residential_template

# Imported, not copied: `_seed_opex` carries the real 0075 seed through a stand-in
# apps registry, and a second copy of that machinery here would be a second thing to
# fix when the template moves.
from .tests_two_step_completion import (
    OPEX_TASK_NAME, _client_for, _profile, _seed_opex,
)


class PoolFixture(TestCase):
    """One really activated OPEX site whose approver pool is EXACTLY its PM.

    THE FIXTURE IS THE PRODUCTION SHAPE, deliberately and with nothing extra: no
    `is_qaqc` holder anywhere in the database, and no coordinator on the project.
    That is what production looked like on 20 Sep 2026 and it is the only shape in
    which the deadlock occurs. Tests that need a second signatory add one
    explicitly, in the test, where the reader can see it.

    `self.se` is a Site Engineer who holds a task on the site. They are NOT in the
    approver pool — a Site Engineer without `is_qaqc` fails both arms of
    `user_can_approve_task()` — and the fixture asserts that below rather than
    assuming it, because every test in this file is about who is and is not in that
    set.
    """

    @classmethod
    def setUpTestData(cls):
        resolve_residential_template()   # bootstraps RESIDENTIAL v1 on a virgin DB
        _seed_opex()

        cls.pm = _profile('pool_pm', 'PM')
        cls.se = _profile('pool_se', 'Site Engineer')
        # Created but NOT attached to the project. Tests that want an independent
        # approver call `self.site.coordinators.add(self.coordinator)` themselves.
        cls.coordinator = _profile('pool_coord', 'Project Coordinator')

    def setUp(self):
        self.site = Project.objects.create(
            customer_name='Pool Test Site',
            customer_phone='9876543210',
            site_address='1 Deadlock Road',
            city='Lucknow',
            project_type='OPEX',
            dc_capacity_kw=Decimal('100.00'),
            status='Draft',
            assigned_pm=self.pm,
        )
        response = _client_for(self.pm).post(
            reverse('opex_site_activate', args=[self.site.project_id]))
        self.assertEqual(response.status_code, 302, 'OPEX activation did not redirect')
        self.site.refresh_from_db()
        self.assertEqual(self.site.status, 'Active')

        self.task = self._opex_task()

        # A second non-mirror task, held by the site engineer. It gives the SE
        # visibility of the project (a Site Engineer sees a project by holding a task
        # on it) so that the SE-submits tests exercise the real path rather than
        # failing on a 404 that has nothing to do with the approver pool.
        self.se_task = Task.objects.filter(
            phase__project=self.site, is_mirror=False,
        ).exclude(pk=self.task.pk).first()
        self.assertIsNotNone(self.se_task, 'the OPEX attach produced only one task')
        assign_task_to(self.se_task, self.se, notify=False)

        # THE FIXTURE'S CENTRAL CLAIM, asserted and not assumed.
        self.assertEqual(
            UserProfile.objects.filter(is_qaqc=True).count(), 0,
            'this file models production: no QA/QC holders anywhere',
        )
        self.assertEqual(
            self.site.coordinators.count(), 0,
            'the fixture site must start with no coordinator',
        )
        self.assertFalse(
            user_can_approve_task(self.se.user, self.site),
            'a Site Engineer without is_qaqc must not be in the approver pool',
        )

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

    def _in_progress(self, task=None, actor=None):
        """Move a task to In Progress through the real screen.

        A due_date rides along because `_apply_task_status_change()` refuses In
        Progress without one and the OPEX attach leaves due dates null.

        `actor` defaults to the site PM, who satisfies the overview screen's gate for
        every task. It is a parameter because the deactivated-PM tests cannot use
        them — a deactivated user's session is refused at the door, and the fixture
        would fail for a reason that has nothing to do with the approver pool.
        """
        task = task or self.task
        response = _client_for(actor or self.pm).post(
            reverse('task_status_update', args=[self.site.project_id, task.pk]),
            {'status': Task.IN_PROGRESS,
             'due_date': (date.today() + timedelta(days=7)).isoformat()},
        )
        self.assertIn(response.status_code, (200, 302))
        task.refresh_from_db()
        self.assertEqual(task.status, Task.IN_PROGRESS,
                         'fixture could not put the task In Progress')
        return task

    def _submit(self, profile, task=None, remarks='Work finished on site.'):
        task = task or self.task
        data = {'submission_remarks': remarks} if remarks is not None else {}
        return _client_for(profile).post(
            reverse('task_submit_for_approval',
                    args=[self.site.project_id, task.pk]),
            data,
        )

    def _messages(self, response):
        return [str(m) for m in get_messages(response.wsgi_request)]

    def _transitions(self, task):
        return list(StatusTransition.objects.filter(
            subject_type=SUBJECT_TASK, subject_id=task.pk,
        ).order_by('occurred_at', 'pk'))

    def _codes(self, task):
        return list(ActivityLog.objects.filter(
            entity_type='Task', entity_id=task.pk,
        ).values_list('action_code', flat=True))

    def _assert_completed_self_certified(self, task, by):
        """The full shape of a self-certified completion, asserted in one place so
        every test that produces one checks the same seven things."""
        task.refresh_from_db()
        self.assertEqual(task.status, Task.DONE, 'self-certification must complete the task')
        self.assertTrue(task.approval_self_certified, 'the flag must record why')
        self.assertEqual(task.submitted_by, by)
        self.assertEqual(task.approved_by, by, 'both signatures are the submitter')
        self.assertIsNotNone(task.submitted_at)
        self.assertIsNotNone(task.approved_at)
        self.assertIsNotNone(task.completed_at, 'Done must stamp completed_at')

        # THE LEDGER ROW IS THE POINT OF REUSING _apply_task_status_change(). A Done
        # task with no StatusTransition is a hole in R-2 that no reader can
        # reconstruct afterwards, and writing `status` directly would have left one.
        to_done = [t for t in self._transitions(task) if t.to_status == Task.DONE]
        self.assertEqual(len(to_done), 1, 'exactly one transition to Done')
        self.assertEqual(to_done[0].from_status, Task.IN_PROGRESS)
        self.assertEqual(to_done[0].actor, by)


# ---------------------------------------------------------------------------
# a + b. The pool is empty -> the submission completes the task
# ---------------------------------------------------------------------------
class SelfCertifiesWhenPoolIsEmptyTests(PoolFixture):

    def test_pm_submits_own_task_self_certifies(self):
        """(a) The live shape of task 2071: PM-assigned, PM-submitted, no coordinator.

        Before this change the task landed in "Awaiting approval" and stayed there,
        because the only member of the approver pool was the person the guard in
        `task_approve` refuses.
        """
        self._in_progress()
        self.assertFalse(
            task_has_independent_approver(self.site, self.pm),
            'fixture precondition: the PM is the whole pool',
        )

        response = self._submit(self.pm)
        self.assertIn(response.status_code, (200, 302))

        self._assert_completed_self_certified(self.task, by=self.pm)
        self.assertIn('task_self_certified', self._codes(self.task))
        self.assertNotIn(
            'task_submitted_for_approval', self._codes(self.task),
            'a self-certified completion is not also an ordinary submission',
        )

    def test_pm_submits_another_users_task_self_certifies(self):
        """(b) The live shape of task 2094: assigned to somebody else, submitted by
        the PM, no coordinator.

        THIS IS THE TEST THAT FAILS IF ANYONE REWRITES THE RULE AS "skip the step
        when the task is assigned to the PM". The task here is assigned to the site
        engineer, so an assignee-shaped rule would queue it for approval — and the
        only person who could then give that approval is the PM who just submitted
        it. Same deadlock, reached by a different door.
        """
        self._in_progress(self.se_task)
        self.assertEqual(self.se_task.assigned_to, self.se,
                         'fixture precondition: the task is NOT the PM\'s')

        response = self._submit(self.pm, task=self.se_task)
        self.assertIn(response.status_code, (200, 302))

        # Signed by the PM, who submitted it — not by the assignee, who did not.
        self._assert_completed_self_certified(self.se_task, by=self.pm)

    def test_flag_distinguishes_self_certified_from_an_ordinary_completion(self):
        """`approval_self_certified` is what makes the two tellable apart afterwards.

        Pinned because the column looks redundant beside `submitted_by ==
        approved_by` and is the kind of thing a later reader deletes as derivable.
        """
        self._in_progress()
        self._submit(self.pm)
        self.task.refresh_from_db()
        self.assertTrue(self.task.approval_self_certified)

        # An ordinary two-person completion on the same site leaves it False.
        self.site.coordinators.add(self.coordinator)
        other = self._in_progress(self.se_task)
        self._submit(self.se, task=other)
        _client_for(self.coordinator).post(
            reverse('task_approve', args=[self.site.project_id, other.pk]),
            {'approval_remarks': 'Checked on site.'},
        )
        other.refresh_from_db()
        self.assertEqual(other.status, Task.DONE)
        self.assertFalse(
            other.approval_self_certified,
            'a task signed by two people is not self-certified',
        )


# ---------------------------------------------------------------------------
# c + d. A second signatory exists -> nothing changes
# ---------------------------------------------------------------------------
class NormalPathIsUntouchedTests(PoolFixture):

    def test_pm_submit_with_coordinator_present_awaits_approval(self):
        """(c) Same as (a) but the project HAS a coordinator.

        The coordinator is admitted by `user_can_manage_project()`, so the pool is
        not empty once the submitter is removed from it and the two-signature rule
        stands exactly as before.
        """
        self.site.coordinators.add(self.coordinator)
        self._in_progress()
        self.assertTrue(task_has_independent_approver(self.site, self.pm))

        response = self._submit(self.pm)
        self.assertIn(response.status_code, (200, 302))

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, Task.IN_PROGRESS,
                         'submission must not move the status (2.1 §3)')
        self.assertIsNotNone(self.task.submitted_at)
        self.assertIsNone(self.task.approved_at, 'nobody has approved this yet')
        self.assertFalse(self.task.approval_self_certified)
        self.assertIn('task_submitted_for_approval', self._codes(self.task))
        self.assertEqual(
            [t for t in self._transitions(self.task) if t.to_status == Task.DONE], [],
            'an awaiting task has not completed',
        )

    def test_site_engineer_submit_with_pm_present_awaits_approval(self):
        """(d) Regression: the ordinary case the feature was built for.

        The SE is not in the pool, the PM is, and the PM is not the submitter — so
        there is a second signature available and the task waits for it.
        """
        self._in_progress(self.se_task)
        self.assertTrue(task_has_independent_approver(self.site, self.se))

        response = self._submit(self.se, task=self.se_task)
        self.assertIn(response.status_code, (200, 302))

        self.se_task.refresh_from_db()
        self.assertEqual(self.se_task.status, Task.IN_PROGRESS)
        self.assertIsNotNone(self.se_task.submitted_at)
        self.assertIsNone(self.se_task.approved_at)
        self.assertFalse(self.se_task.approval_self_certified)

        # And the PM can still finish it, which is the half of "unchanged" that
        # matters: the pool is not merely non-empty, it works.
        _client_for(self.pm).post(
            reverse('task_approve', args=[self.site.project_id, self.se_task.pk]),
            {'approval_remarks': 'Inspected.'},
        )
        self.se_task.refresh_from_db()
        self.assertEqual(self.se_task.status, Task.DONE)
        self.assertEqual(self.se_task.approved_by, self.pm)
        self.assertFalse(self.se_task.approval_self_certified)

    def test_qaqc_holder_counts_as_an_independent_approver(self):
        """The second arm of `user_can_approve_task()` reaches this too.

        Not in the spec's list, but it is the arm most likely to be forgotten by
        anyone rebuilding the candidate set in `task_has_independent_approver()` —
        and the helper's contract is that its candidates are a SUPERSET of the
        predicate's members. An `is_qaqc` holder with sight of the site is such a
        member, so a candidate set built from PM and coordinators alone would
        self-certify past a person who was standing right there.
        """
        UserProfile.objects.filter(pk=self.se.pk).update(is_qaqc=True)
        self.se.refresh_from_db()
        self.assertTrue(user_can_approve_task(self.se.user, self.site))

        self._in_progress()
        self.assertTrue(
            task_has_independent_approver(self.site, self.pm),
            'the QA/QC holder is a second signature and must be counted',
        )
        self._submit(self.pm)
        self.task.refresh_from_db()
        self.assertIsNone(self.task.approved_at)
        self.assertFalse(self.task.approval_self_certified)


# ---------------------------------------------------------------------------
# e. Remarks are still required on the path that completes the task
# ---------------------------------------------------------------------------
class BlankRemarksRefusedTests(PoolFixture):

    def test_blank_remarks_on_self_certify_path_writes_nothing(self):
        """(e) Refused before any write — and this path needs the remark MORE than
        the ordinary one, because it is the only account of the work that will ever
        exist on the task."""
        self._in_progress()

        response = self._submit(self.pm, remarks='   ')
        self.assertIn(response.status_code, (200, 302))

        self.task.refresh_from_db()
        self.assertIsNone(self.task.submitted_at, 'nothing may be written')
        self.assertIsNone(self.task.approved_at)
        self.assertIsNone(self.task.submitted_by)
        self.assertIsNone(self.task.approved_by)
        self.assertFalse(self.task.approval_self_certified)
        self.assertEqual(self.task.status, Task.IN_PROGRESS)
        self.assertEqual(
            [t for t in self._transitions(self.task) if t.to_status == Task.DONE], [],
            'a refused submission leaves no ledger row',
        )
        self.assertNotIn('task_self_certified', self._codes(self.task))
        self.assertTrue(
            any('describe the work' in m.lower() for m in self._messages(response)),
            f'expected the blank-remark refusal, got {self._messages(response)}',
        )

    def test_missing_remarks_field_entirely_is_also_refused(self):
        """A POST with no `submission_remarks` key at all, not merely a blank one —
        the shape a hand-rolled request takes."""
        self._in_progress()
        self._submit(self.pm, remarks=None)
        self.task.refresh_from_db()
        self.assertIsNone(self.task.submitted_at)
        self.assertFalse(self.task.approval_self_certified)


# ---------------------------------------------------------------------------
# f. The self-approval guard is unchanged
# ---------------------------------------------------------------------------
class SelfApprovalGuardStillRefusesTests(PoolFixture):

    def test_pm_cannot_approve_own_submission_when_an_approver_exists(self):
        """(f) The guard in `task_approve` stays exactly as it is.

        This is the rule self-certification must not have quietly repealed. With a
        coordinator on the project the submission takes the ordinary path, and the
        PM who submitted it is still refused their own second signature — the work
        goes to the coordinator or it does not go.
        """
        self.site.coordinators.add(self.coordinator)
        self._in_progress()
        self._submit(self.pm)
        self.task.refresh_from_db()
        self.assertIsNotNone(self.task.submitted_at, 'precondition: it is awaiting')

        response = _client_for(self.pm).post(
            reverse('task_approve', args=[self.site.project_id, self.task.pk]),
            {'approval_remarks': 'Looks fine to me.'},
        )
        self.assertIn(response.status_code, (200, 302))

        self.task.refresh_from_db()
        self.assertIsNone(self.task.approved_at, 'the guard must still refuse')
        self.assertIsNone(self.task.approved_by)
        self.assertEqual(self.task.status, Task.IN_PROGRESS)
        self.assertFalse(self.task.approval_self_certified)
        self.assertTrue(
            any('submitted' in m.lower() and 'yourself' in m.lower()
                for m in self._messages(response)),
            f'expected the self-approval refusal, got {self._messages(response)}',
        )

    def test_the_independent_approver_can_then_sign_it(self):
        """The refusal above is about the PERSON, not about the task being stuck."""
        self.site.coordinators.add(self.coordinator)
        self._in_progress()
        self._submit(self.pm)

        _client_for(self.coordinator).post(
            reverse('task_approve', args=[self.site.project_id, self.task.pk]),
            {'approval_remarks': 'Verified the meter reading.'},
        )
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, Task.DONE)
        self.assertEqual(self.task.approved_by, self.coordinator)
        self.assertFalse(self.task.approval_self_certified)


# ---------------------------------------------------------------------------
# g. A deactivated PM. THE SITE ENGINEER DOES NOT INHERIT THE SIGNATURE.
# ---------------------------------------------------------------------------
class DeactivatedPmTests(PoolFixture):
    """(g) The assigned PM's `User.is_active` is False and the site engineer submits.

    AN EMPTY POOL IS NOT A LICENCE. The pool genuinely is empty here — a user who
    cannot log in is not a second signature, and `task_has_independent_approver()`
    correctly says so. What does NOT follow is that the person standing in front of
    it may sign instead. Self-certification collapses two signatures into one and
    can only do that for somebody who held the second one; a site engineer never
    did. `user_may_self_certify()` requires `user_can_approve_task()` as well, so
    the SE takes the ordinary path and the task waits.

    WAITING IS THE CORRECT OUTCOME AND IS STILL NOT A FINISHED STORY. The task is
    not lost and not silently completed, but nothing on any screen yet says WHY it
    is waiting or that the site has no active approver at all — it reads exactly
    like a task waiting on a PM who will be along shortly. That visible signal is
    recorded as "departed-PM reassignment" in the deferred log. The remedy in the
    product today is to reassign `assigned_pm`, and the last test here asserts that
    doing so works with no code change.
    """

    def _deactivate_pm(self):
        User.objects.filter(pk=self.pm.user.pk).update(is_active=False)
        self.pm.user.refresh_from_db()

    def test_inactive_pm_is_not_counted_as_an_approver(self):
        self._deactivate_pm()
        self.assertFalse(
            task_has_independent_approver(self.site, self.se),
            'a user who cannot log in is not a second signature',
        )

    def test_site_engineer_may_not_self_certify_when_the_pm_is_deactivated(self):
        """THE POINT OF THIS CLASS. The pool is empty, and the SE still may not sign.

        This is the test that fails if anyone reduces `user_may_self_certify()` back
        to "is the pool empty" — which is what it was, and which let a site engineer
        complete their own work unreviewed on any site whose PM had left.
        """
        self._deactivate_pm()
        self.assertFalse(
            user_may_self_certify(self.se.user, self.site),
            'a site engineer never held the approval signature to begin with',
        )

    def test_site_engineer_submission_waits_for_approval(self):
        """The behaviour that replaces it: the ordinary path, unchanged."""
        self._in_progress(self.se_task)
        self._deactivate_pm()

        response = self._submit(self.se, task=self.se_task)
        self.assertIn(response.status_code, (200, 302))

        self.se_task.refresh_from_db()
        self.assertEqual(self.se_task.status, Task.IN_PROGRESS,
                         'submission must not move the status (2.1 §3)')
        self.assertIsNotNone(self.se_task.submitted_at, 'the submission is recorded')
        self.assertEqual(self.se_task.submitted_by, self.se)
        self.assertIsNone(self.se_task.approved_at, 'nobody has approved it')
        self.assertIsNone(self.se_task.approved_by)
        self.assertFalse(self.se_task.approval_self_certified)
        self.assertIn('task_submitted_for_approval', self._codes(self.se_task))
        self.assertNotIn('task_self_certified', self._codes(self.se_task))
        self.assertEqual(
            [t for t in self._transitions(self.se_task) if t.to_status == Task.DONE], [],
            'a waiting task has not completed',
        )

    def test_the_row_offers_ordinary_submission_not_completion(self):
        """The label agrees with the endpoint on this path too — the SE must not be
        shown an offer to complete something they may not complete."""
        self._in_progress(self.se_task)
        self._deactivate_pm()
        body = _client_for(self.se).get(
            reverse('project_overview', args=[self.site.project_id])).content.decode()
        self.assertIn('Submit for approval', body)
        self.assertNotIn('Complete (self-certified)', body)

    def test_profile_level_deactivation_has_the_same_effect(self):
        """`UserProfile.is_active` is the portal's own soft switch and is checked
        alongside Django's, so turning either one off removes the person."""
        UserProfile.objects.filter(pk=self.pm.pk).update(is_active=False)
        self.pm.refresh_from_db()
        self.assertFalse(task_has_independent_approver(self.site, self.se))
        self.assertFalse(user_may_self_certify(self.se.user, self.site))

    def test_reassigning_the_project_lets_the_new_pm_approve(self):
        """The remedy the deferred entry names, asserted end to end: give the site an
        active manager and the waiting task can be signed off normally."""
        self._in_progress(self.se_task)
        self._deactivate_pm()
        self._submit(self.se, task=self.se_task)
        self.se_task.refresh_from_db()
        self.assertIsNone(self.se_task.approved_at, 'precondition: it is waiting')

        replacement = _profile('pool_pm2', 'PM')
        Project.objects.filter(pk=self.site.pk).update(assigned_pm=replacement)
        self.site.refresh_from_db()
        self.assertTrue(task_has_independent_approver(self.site, self.se))

        _client_for(replacement).post(
            reverse('task_approve', args=[self.site.project_id, self.se_task.pk]),
            {'approval_remarks': 'Picked up from the previous PM.'},
        )
        self.se_task.refresh_from_db()
        self.assertEqual(self.se_task.status, Task.DONE)
        self.assertEqual(self.se_task.approved_by, replacement)
        self.assertFalse(
            self.se_task.approval_self_certified,
            'it was signed by a second person, so it is not self-certified',
        )



# ---------------------------------------------------------------------------
# The label the person actually reads before pressing
# ---------------------------------------------------------------------------
class ButtonLabelTests(PoolFixture):
    """Both surfaces must say what the button will DO before it is pressed.

    THIS IS THE ONE THING THE TEMPLATES MUST NOT GET WRONG. The endpoint is correct
    either way — it reads the pool itself — so a stale label cannot complete the
    wrong task. What it can do is take a completion under somebody's name while the
    button they pressed said it was asking for review, which is the same
    one-signature completion 2.1 was built to stop, reached by lying about it.

    Asserted on the RENDERED page rather than on the context flag, because the flag
    being right and the markup ignoring it is precisely the failure mode.
    """

    SELF = 'Complete (self-certified)'
    NORMAL = 'Submit for approval'
    NOTE = 'No other approver on this project'

    def _detail(self, profile, task=None):
        return _client_for(profile).get(reverse(
            'task_detail', args=[self.site.project_id, (task or self.task).pk]))

    def _overview(self, profile):
        return _client_for(profile).get(
            reverse('project_overview', args=[self.site.project_id]))

    def test_task_detail_offers_self_certification_when_the_pool_is_empty(self):
        self._in_progress()
        body = self._detail(self.pm).content.decode()
        self.assertIn(self.SELF, body)
        self.assertIn(self.NOTE, body)

    def test_task_detail_offers_ordinary_submission_when_an_approver_exists(self):
        self.site.coordinators.add(self.coordinator)
        self._in_progress()
        body = self._detail(self.pm).content.decode()
        self.assertIn(self.NORMAL, body)
        self.assertNotIn(self.SELF, body)
        self.assertNotIn(self.NOTE, body)

    def test_phase_list_row_offers_self_certification_when_the_pool_is_empty(self):
        self._in_progress()
        body = self._overview(self.pm).content.decode()
        self.assertIn(self.SELF, body)
        self.assertIn(self.NOTE, body)

    def test_phase_list_row_offers_ordinary_submission_to_a_site_engineer(self):
        """The SE has a PM above them, so their submission is an ordinary one and
        the row must not offer to complete anything."""
        self._in_progress(self.se_task)
        body = self._overview(self.se).content.decode()
        self.assertIn(self.NORMAL, body)
        self.assertNotIn(self.SELF, body)

    def test_the_confirm_modal_matches_the_row_button(self):
        """THE DIALOG IS WHERE CONSENT IS ACTUALLY GIVEN. The row button opens
        `#submitApprovalModal`, which lives in project_overview.html; for one
        release it kept saying "Submit for approval" while the row beside it said
        "Complete (self-certified)", so the box a PM pressed OK in described an act
        it was not performing. Title, confirm button and body are asserted together
        because they are three separate strings in that file and any one of them can
        be missed."""
        self._in_progress()
        body = self._overview(self.pm).content.decode()

        # Title and footer button — two occurrences beyond the row button itself.
        self.assertGreaterEqual(
            body.count(self.SELF), 3,
            'expected the row button, the modal title and the modal confirm button',
        )
        self.assertNotIn('Submit for approval', body)
        # The body line the old wording contradicted outright.
        self.assertNotIn('stays In Progress until the project manager', body)
        self.assertGreaterEqual(body.count(self.NOTE), 2, 'row note and modal body')

    def test_the_confirm_modal_keeps_the_ordinary_wording_otherwise(self):
        self.site.coordinators.add(self.coordinator)
        self._in_progress()
        body = self._overview(self.pm).content.decode()
        self.assertIn('Submit for approval', body)
        self.assertIn('stays In Progress until the project manager', body)
        self.assertNotIn(self.SELF, body)
        self.assertNotIn(self.NOTE, body)


# ---------------------------------------------------------------------------
# The helper's own contract
# ---------------------------------------------------------------------------
class IndependentApproverHelperTests(PoolFixture):
    """`task_has_independent_approver()` answers THROUGH `user_can_approve_task()`
    and must not acquire an opinion of its own."""

    def test_excludes_only_the_submitter(self):
        self.site.coordinators.add(self.coordinator)
        # The coordinator is a second signature for the PM...
        self.assertTrue(task_has_independent_approver(self.site, self.pm))
        # ...and the PM is one for the coordinator.
        self.assertTrue(task_has_independent_approver(self.site, self.coordinator))

    def test_empty_when_the_only_approver_is_the_submitter(self):
        self.assertFalse(task_has_independent_approver(self.site, self.pm))

    def test_true_for_a_submitter_who_is_not_in_the_pool_at_all(self):
        self.assertTrue(task_has_independent_approver(self.site, self.se))

    def test_a_none_submitter_asks_only_whether_anyone_can_approve(self):
        """`submitted_by` is nullable, so the helper must tolerate None rather than
        raising on a row whose submitter's account has been removed."""
        self.assertTrue(task_has_independent_approver(self.site, None))

    def test_project_with_no_pm_and_nobody_else_has_no_approver(self):
        Project.objects.filter(pk=self.site.pk).update(assigned_pm=None)
        self.site.refresh_from_db()
        self.assertFalse(task_has_independent_approver(self.site, self.se))

    def test_every_candidate_it_returns_true_for_really_passes_the_predicate(self):
        """The contract in one assertion: the helper never claims an approver the
        predicate would refuse."""
        self.site.coordinators.add(self.coordinator)
        self.assertTrue(task_has_independent_approver(self.site, self.pm))
        passers = [p for p in UserProfile.objects.select_related('user')
                   if p != self.pm and user_can_approve_task(p.user, self.site)]
        self.assertTrue(passers, 'the helper said yes, so somebody must pass')


class MaySelfCertifyHelperTests(PoolFixture):
    """`user_may_self_certify()` is the AND of the two terms, and each one is load
    bearing on its own. The truth table, asserted rather than described."""

    def test_holds_the_signature_and_nobody_else_does(self):
        """True: the only case there is."""
        self.assertTrue(user_may_self_certify(self.pm.user, self.site))

    def test_holds_the_signature_but_somebody_else_does_too(self):
        """False on the SECOND term — an ordinary two-person approval is available."""
        self.site.coordinators.add(self.coordinator)
        self.assertFalse(user_may_self_certify(self.pm.user, self.site))

    def test_does_not_hold_the_signature_though_nobody_else_does_either(self):
        """False on the FIRST term. This is the deactivated-PM shape, stated as a
        pure predicate: the pool is empty and the site engineer still may not sign."""
        User.objects.filter(pk=self.pm.user.pk).update(is_active=False)
        self.assertFalse(task_has_independent_approver(self.site, self.se),
                         'precondition: the pool really is empty')
        self.assertFalse(user_may_self_certify(self.se.user, self.site))

    def test_does_not_hold_the_signature_and_somebody_else_does(self):
        """False on both terms — the ordinary healthy project."""
        self.assertFalse(user_may_self_certify(self.se.user, self.site))

    def test_a_user_with_no_profile_is_refused_rather_than_raising(self):
        """Matches the guard style of every other predicate in the module: a
        superuser made by `createsuperuser` has no UserProfile."""
        bare = User.objects.create_superuser(
            username='pool_bare', email='bare@example.com', password='x')
        UserProfile.objects.filter(user=bare).delete()
        bare.refresh_from_db()
        self.assertFalse(user_may_self_certify(bare, self.site))
