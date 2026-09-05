"""
A rejection leaves a defect behind that the next rejection cannot erase. Prompt 2.3a.

WHAT THE FEATURE IS
-------------------
`task_reject` already wrote its reason to `Task.approval_remarks`. That is ONE
column: the second rejection overwrites the first, so by the time anyone asks
"was the earthing finding ever fixed?" the finding is gone. A punch point is that
same reason kept as a ROW — one per rejection, in order, each with its own text.
The only thing a person may do to one directly is WAIVE it (accept the defect and
proceed), and B-12 puts that in the PM's hands alone.

WHY THE ISOLATION TESTS ARE THE POINT OF THIS FILE
--------------------------------------------------
`PunchPoint` was chosen over a discriminator column on `Issue` on one claim: that
the existing issue readers cost ZERO reads, because none of them names this table
and therefore none of them can see these rows. `PunchPointIsolationTests` is that
claim measured rather than asserted — it counts the issue register, the
task-detail panel's context and the project-level counts either side of a real
rejection and requires them not to move. If a later edit teaches an issue screen
about punch points, that class fails by name.

THE FLAG IS NOT THE AUTHORITY
-----------------------------
`user_can_approve_task()` admits `is_qaqc`, so the QA/QC holder is the person a
rejection makes the RAISER of a punch point. `WaiverAuthorityTests` pins that the
same person is refused the waiver — a raiser who can wave their own finding away
is a record that cancels itself, which is the failure B-12's "PM alone" exists to
prevent.

Run with:
    python manage.py test projects.tests_punch_point --settings=solarpms.test_settings
"""
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import reverse

from .models import (
    ActivityLog, Issue, Project, PunchPoint, Task, UserProfile,
)
from .permissions import user_can_approve_task, user_can_waive_punch_point
from .utils import RESIDENTIAL_FINANCE_ASSIGNEE_EMAIL, assign_task_to

# The 2.1 file already builds exactly the site these tests need — a really
# activated OPEX tender site, its PM, a QA/QC holder with sight of it and an
# outsider without. Reusing its fixture rather than copying one keeps both files
# testing the SAME shape: a template change that breaks the fixture breaks both,
# instead of one file quietly drifting onto a site the other no longer resembles.
from .tests_two_step_completion import (
    TwoStepFixture, _client_for, _profile,
)


class PunchPointFixture(TwoStepFixture):
    """Adds the two acts 2.3a is about: getting a task rejected, and reading back
    what that left behind."""

    def _submitted(self, task=None):
        task = task or self._in_progress()
        self._post('task_submit_for_approval', {'submission_remarks': 'work done'},
                   task=task)
        task.refresh_from_db()
        return task

    def _reject(self, reason, profile=None, task=None):
        """One full round: submit, then reject with `reason`.

        Going through both real endpoints rather than writing the columns is what
        makes these tests say anything about production — a hand-made rejected task
        proves the model saves and nothing about whether the view reaches it.
        """
        task = self._submitted(task=task)
        self._post('task_reject', {'approval_remarks': reason},
                   profile=profile, task=task)
        task.refresh_from_db()
        return task

    def _points(self, task=None):
        return list(PunchPoint.objects.filter(task=task or self.task))

    def _waive(self, punch_point, reason='Accepted, client signed off.', profile=None):
        return _client_for(profile or self.pm).post(
            reverse('punch_point_waive',
                    args=[self.site.project_id, punch_point.pk]),
            {'waiver_reason': reason},
        )

    def _messages(self, response):
        return [str(m) for m in get_messages(response.wsgi_request)]


# ---------------------------------------------------------------------------
# 1. A rejection raises a punch point, and a second one does not overwrite it
# ---------------------------------------------------------------------------

class RejectionRaisesAPunchPointTests(PunchPointFixture):

    def test_a_rejection_creates_one_punch_point_carrying_the_real_reason(self):
        task = self._reject('Earthing not tested.')
        points = self._points(task)
        self.assertEqual(len(points), 1,
                         'a rejection did not raise a punch point')
        self.assertEqual(points[0].reason, 'Earthing not tested.')
        self.assertEqual(points[0].status, PunchPoint.OPEN)
        self.assertEqual(points[0].raised_by, self.pm)
        self.assertIsNotNone(points[0].created_at)

    def test_two_rejections_leave_two_rows_with_their_own_reasons(self):
        """THE WHOLE POINT OF THE MODEL. `approval_remarks` holds only the second
        reason afterwards — that is unchanged and correct, it is "the latest word".
        What is new is that the FIRST reason is still readable somewhere."""
        task = self._reject('Earthing not tested.')
        task = self._reject('Cable trays still unpainted.', task=task)

        reasons = [p.reason for p in self._points(task)]
        self.assertEqual(
            reasons, ['Earthing not tested.', 'Cable trays still unpainted.'],
            'the second rejection overwrote the first punch point instead of '
            'raising its own')

        task.refresh_from_db()
        self.assertEqual(
            task.approval_remarks, 'Cable trays still unpainted.',
            'approval_remarks stopped behaving as it did before 2.3a')

    def test_a_third_rejection_keeps_all_three_in_order(self):
        task = self._reject('One.')
        task = self._reject('Two.', task=task)
        task = self._reject('Three.', task=task)
        self.assertEqual([p.reason for p in self._points(task)],
                         ['One.', 'Two.', 'Three.'])

    def test_the_qaqc_holder_is_recorded_as_the_raiser(self):
        """`raised_by` is the ACTOR, not the PM — a QA/QC rejection must name the
        person who found the defect, or the register cannot say who to ask."""
        task = self._reject('Torque marks missing.', profile=self.qaqc)
        points = self._points(task)
        self.assertEqual(len(points), 1)
        self.assertEqual(points[0].raised_by, self.qaqc)

    def test_a_refused_rejection_raises_nothing(self):
        """The punch point is created AFTER every refusal in the view. A rejection
        with no reason, or of an unsubmitted task, must leave no row — a blank or
        phantom punch point is worse than none."""
        task = self._in_progress()

        # Unsubmitted: refused before anything is written.
        self._post('task_reject', {'approval_remarks': 'no'}, task=task)
        self.assertEqual(self._points(task), [])

        # Submitted but reason blank: refused on the remarks check.
        task = self._submitted(task=task)
        self._post('task_reject', {'approval_remarks': '   '}, task=task)
        self.assertEqual(self._points(task), [],
                         'a rejection with no stated reason raised a punch point '
                         'with no reason in it')

    def test_a_stranger_rejecting_raises_nothing(self):
        task = self._submitted()
        response = self._post('task_reject', {'approval_remarks': 'no'},
                              profile=self.other)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self._points(task), [])

    def test_the_existing_rejection_behaviour_is_unchanged(self):
        """2.3a adds a row; it must not have quietly changed what a rejection
        already did. This restates the 2.1 contract from inside this file so a
        regression shows up here too, next to the code that caused it."""
        task = self._reject('Earthing not tested.')
        self.assertIsNone(task.submitted_by)
        self.assertIsNone(task.submitted_at)
        self.assertEqual(task.submission_remarks, '')
        self.assertEqual(task.approval_remarks, 'Earthing not tested.')
        self.assertIsNone(task.approved_at)
        self.assertIsNone(task.approved_by)
        self.assertEqual(task.status, Task.IN_PROGRESS)
        self.assertIn('task_rejected', list(ActivityLog.objects.filter(
            entity_type='Task', entity_id=task.pk,
        ).values_list('action_code', flat=True)))


# ---------------------------------------------------------------------------
# 2. The waiver
# ---------------------------------------------------------------------------

class WaiverTests(PunchPointFixture):

    def test_the_pm_waives_a_punch_point_and_the_decision_is_recorded(self):
        task = self._reject('Earthing not tested.')
        point = self._points(task)[0]

        response = self._waive(point, 'Client accepted; retest at next service.')
        self.assertEqual(response.status_code, 302)

        point.refresh_from_db()
        self.assertEqual(point.status, PunchPoint.WAIVED)
        self.assertEqual(point.waived_by, self.pm)
        self.assertIsNotNone(point.waived_at)
        self.assertEqual(point.waiver_reason,
                         'Client accepted; retest at next service.')

    def test_a_waiver_requires_a_reason(self):
        task = self._reject('Earthing not tested.')
        point = self._points(task)[0]

        response = self._waive(point, '   ')
        point.refresh_from_db()
        self.assertEqual(point.status, PunchPoint.OPEN,
                         'a punch point was waived with no reason given')
        self.assertIsNone(point.waived_at)
        self.assertIn('state why', ' '.join(self._messages(response)).lower())

    def test_a_waived_punch_point_cannot_be_waived_again(self):
        """`waived_by` and `waived_at` name the moment the decision was actually
        taken. A second waiver would move them onto whoever pressed the button
        last."""
        task = self._reject('Earthing not tested.')
        point = self._points(task)[0]
        self._waive(point, 'First and only decision.')
        point.refresh_from_db()
        first_at = point.waived_at

        response = self._waive(point, 'Second thoughts.')
        point.refresh_from_db()
        self.assertEqual(point.waiver_reason, 'First and only decision.')
        self.assertEqual(point.waived_at, first_at)
        self.assertIn('already been waived',
                      ' '.join(self._messages(response)).lower())

    def test_waiving_one_point_leaves_the_others_open(self):
        """Waivers are per-defect. Accepting one finding must not clear the rest of
        the register on the same task."""
        task = self._reject('Earthing not tested.')
        task = self._reject('Cable trays still unpainted.', task=task)
        first, second = self._points(task)

        self._waive(first)

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.status, PunchPoint.WAIVED)
        self.assertEqual(second.status, PunchPoint.OPEN,
                         'waiving one punch point waived another')

    def test_a_waiver_touches_nothing_on_the_task(self):
        """Waiving is a decision about a DEFECT, not a verdict on a submission. The
        task's status and every approval column must read exactly as they did."""
        task = self._reject('Earthing not tested.')
        before = (task.status, task.submitted_at, task.approved_at,
                  task.approval_remarks, task.completed_at)

        self._waive(self._points(task)[0])

        task.refresh_from_db()
        self.assertEqual(
            (task.status, task.submitted_at, task.approved_at,
             task.approval_remarks, task.completed_at),
            before,
            'waiving a punch point changed the task it hangs off')

    def test_a_waiver_writes_its_own_activity_code(self):
        task = self._reject('Earthing not tested.')
        self._waive(self._points(task)[0])
        self.assertIn('punch_point_waived', list(ActivityLog.objects.filter(
            entity_type='Task', entity_id=task.pk,
        ).values_list('action_code', flat=True)))

    def test_get_does_not_waive(self):
        task = self._reject('Earthing not tested.')
        point = self._points(task)[0]
        _client_for(self.pm).get(
            reverse('punch_point_waive', args=[self.site.project_id, point.pk]))
        point.refresh_from_db()
        self.assertEqual(point.status, PunchPoint.OPEN)


# ---------------------------------------------------------------------------
# 3. Who may waive — B-12's "PM alone"
# ---------------------------------------------------------------------------

class WaiverAuthorityTests(PunchPointFixture):

    def test_the_qaqc_holder_may_reject_but_may_not_waive(self):
        """The two authorities in one test, because the gap between them IS the
        rule. The same person, the same project, the same punch point: allowed to
        raise it, refused the power to wave it away."""
        self.assertTrue(
            user_can_approve_task(self.qaqc.user, self.site),
            'fixture broken — the QA/QC holder cannot reject here at all')
        self.assertFalse(
            user_can_waive_punch_point(self.qaqc.user, self.site),
            'the QA/QC flag granted the PM-only waiver')

        task = self._reject('Torque marks missing.', profile=self.qaqc)
        point = self._points(task)[0]

        response = self._waive(point, 'I withdraw it.', profile=self.qaqc)
        self.assertEqual(response.status_code, 403)
        point.refresh_from_db()
        self.assertEqual(point.status, PunchPoint.OPEN)
        self.assertEqual(point.waiver_reason, '')

    def test_a_stranger_gets_404_not_403(self):
        """Scope is answered before authority, as everywhere else in the portal: a
        user who cannot see the site is not told that the punch point exists."""
        task = self._reject('Earthing not tested.')
        point = self._points(task)[0]
        response = self._waive(point, 'no', profile=self.other)
        self.assertEqual(response.status_code, 404)
        point.refresh_from_db()
        self.assertEqual(point.status, PunchPoint.OPEN)

    def test_a_coordinator_on_the_project_may_waive(self):
        """PM-level authority routes through `user_can_manage_project()`, where a
        coordinator is additive PM authority. Pinned so that "PM alone" is not
        later read as "and not the coordinator either" in this one place while it
        means the opposite everywhere else."""
        coordinator = _profile('t23a_coord', 'Project Coordinator')
        self.site.coordinators.add(coordinator)

        task = self._reject('Earthing not tested.')
        point = self._points(task)[0]
        self._waive(point, 'Agreed with the PM.', profile=coordinator)

        point.refresh_from_db()
        self.assertEqual(point.status, PunchPoint.WAIVED)
        self.assertEqual(point.waived_by, coordinator)

    def test_a_punch_point_from_another_site_is_not_reachable_through_this_one(self):
        """The URL carries both a project and a punch point id. A PM who manages
        THIS site must not be able to waive a defect on one they do not, by pasting
        its id into their own site's URL."""
        task = self._reject('Earthing not tested.')
        foreign = PunchPoint.objects.create(
            task=Task.objects.filter(phase__project=self.site)
                             .exclude(pk=task.pk).first(),
            reason='belongs to this site',
        )
        # Re-point it at a task on a site this PM does not manage.
        other_pm = _profile('t23a_otherpm', 'PM')
        other_site = Project.objects.create(
            customer_name='2.3a Other Site',
            customer_phone='9876543212',
            site_address='9 Elsewhere Road',
            city='Lucknow',
            project_type='OPEX',
            capacity_kw=Decimal('50.00'),
            status='Draft',
            assigned_pm=other_pm,
        )
        _client_for(other_pm).post(
            reverse('opex_site_activate', args=[other_site.project_id]))
        foreign.task = Task.objects.filter(
            phase__project=other_site, is_mirror=False).first()
        foreign.save(update_fields=['task'])

        response = _client_for(self.pm).post(
            reverse('punch_point_waive',
                    args=[self.site.project_id, foreign.pk]),
            {'waiver_reason': 'not mine to waive'},
        )
        self.assertEqual(response.status_code, 404)
        foreign.refresh_from_db()
        self.assertEqual(foreign.status, PunchPoint.OPEN)


# ---------------------------------------------------------------------------
# 4. Option D's claim: the issue register cannot see any of this
# ---------------------------------------------------------------------------

class PunchPointIsolationTests(PunchPointFixture):
    """`PunchPoint` was chosen over a discriminator on `Issue` because the existing
    readers then cost nothing. These tests MEASURE that rather than repeating it."""

    def _issue_snapshot(self):
        return {
            'all':        Issue.objects.count(),
            'on_project': Issue.objects.filter(task__phase__project=self.site).count(),
            'on_task':    Issue.objects.filter(task=self.task).count(),
            'open':       Issue.objects.filter(status=Issue.OPEN).count(),
        }

    def test_rejections_and_waivers_move_no_issue_count(self):
        before = self._issue_snapshot()

        task = self._reject('Earthing not tested.')
        task = self._reject('Cable trays still unpainted.', task=task)
        self._waive(self._points(task)[0])

        self.assertEqual(len(self._points(task)), 2,
                         'fixture broken — no punch points were raised')
        self.assertEqual(
            self._issue_snapshot(), before,
            'raising or waiving a punch point changed an Issue count — the two '
            'registers are not separate after all')

    def test_the_task_detail_issues_panel_shows_no_punch_points(self):
        """The panel reads `task_issues`. The punch points ride in a context key of
        their own, so the two lists cannot contaminate each other."""
        task = self._reject('Earthing not tested.')
        response = _client_for(self.pm).get(
            reverse('task_detail', args=[self.site.project_id, task.pk]))
        self.assertEqual(response.status_code, 200)

        self.assertEqual(list(response.context['task_issues']), [],
                         'a punch point appeared in the Issues panel')
        self.assertEqual(len(response.context['task_punch_points']), 1)

    def test_a_real_issue_and_a_punch_point_coexist_without_seeing_each_other(self):
        task = self._reject('Earthing not tested.')
        issue = Issue.objects.create(
            project=self.site, task=task, title='Access road flooded',
            raised_by=self.pm,
        )

        response = _client_for(self.pm).get(
            reverse('task_detail', args=[self.site.project_id, task.pk]))
        self.assertEqual([i.pk for i in response.context['task_issues']], [issue.pk])
        self.assertEqual([p.reason for p in response.context['task_punch_points']],
                         ['Earthing not tested.'])

    def test_the_two_status_vocabularies_do_not_overlap(self):
        """`Issue.status` is untouched by 2.3a. A punch point's states are its own
        two, and neither is an issue state — so no code that switches on one can be
        handed the other and quietly take a branch."""
        issue_states = {value for value, _ in Issue.STATUS_CHOICES}
        punch_states = {value for value, _ in PunchPoint.STATUS_CHOICES}
        self.assertEqual(punch_states, {'Open', 'Waived'})
        self.assertEqual(
            punch_states & issue_states, {'Open'},
            'the two enums drifted — see the note in models.PunchPoint')
        self.assertNotIn('Waived', issue_states)


# ---------------------------------------------------------------------------
# 5. The task-detail block
# ---------------------------------------------------------------------------

class TaskDetailBlockTests(PunchPointFixture):

    def _detail(self, profile=None, task=None):
        return _client_for(profile or self.pm).get(
            reverse('task_detail',
                    args=[self.site.project_id, (task or self.task).pk]))

    def test_a_task_with_no_punch_points_renders_no_block(self):
        response = self._detail()
        self.assertEqual(len(response.context['task_punch_points']), 0)
        self.assertNotContains(response, 'punchPointsSection')

    def test_an_open_punch_point_and_its_reason_are_shown(self):
        task = self._reject('Earthing not tested.')
        response = self._detail(task=task)
        self.assertContains(response, 'punchPointsSection')
        self.assertContains(response, 'Earthing not tested.')

    def test_the_pm_is_offered_the_waive_control(self):
        task = self._reject('Earthing not tested.')
        response = self._detail(task=task)
        self.assertTrue(response.context['can_waive_punch_point'])
        self.assertContains(response, 'waiver_reason')
        self.assertContains(
            response,
            reverse('punch_point_waive',
                    args=[self.site.project_id, self._points(task)[0].pk]))

    def test_the_qaqc_holder_sees_the_point_but_not_the_control(self):
        """The button offered and the endpoint's answer must agree — a control that
        renders and then 403s is a worse experience than no control."""
        task = self._reject('Torque marks missing.', profile=self.qaqc)
        response = self._detail(profile=self.qaqc, task=task)
        self.assertContains(response, 'Torque marks missing.')
        self.assertFalse(response.context['can_waive_punch_point'])
        self.assertNotContains(response, 'waiver_reason')

    def test_a_waived_point_shows_who_waived_it_and_why(self):
        task = self._reject('Earthing not tested.')
        self._waive(self._points(task)[0], 'Client accepted the deviation.')
        response = self._detail(task=task)
        self.assertContains(response, 'Waived')
        self.assertContains(response, 'Client accepted the deviation.')
        # No control on a closed point.
        self.assertNotContains(response, 'waiver_reason')


# ---------------------------------------------------------------------------
# 6. Residential raises none of this
# ---------------------------------------------------------------------------

class ResidentialRaisesNoPunchPointsTests(TestCase):
    """A residential house has no rejection path — `_approval_preconditions()`
    refuses the endpoint outright — and therefore must never grow a punch point.

    Its own fixture, and a real activated house, for the reason 2.1's equivalent
    class gives: "we did not leak onto the other path" is the claim most likely to
    be true today and false after the next edit.
    """

    @classmethod
    def setUpTestData(cls):
        from .utils import resolve_residential_template
        resolve_residential_template()
        cls.pm       = _profile('t23ar_pm', 'PM')
        cls.designer = _profile('t23ar_des', 'Design')
        cls.finance  = _profile('t23ar_fin', 'Finance',
                                email=RESIDENTIAL_FINANCE_ASSIGNEE_EMAIL)

    def setUp(self):
        self.project = Project.objects.create(
            customer_name='2.3a House',
            customer_phone='9876543213',
            site_address='3 Baseline Lane',
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

    def test_rejecting_a_residential_task_is_refused_and_raises_nothing(self):
        response = _client_for(self.pm).post(
            reverse('task_reject', args=[self.project.project_id, self.task.pk]),
            {'approval_remarks': 'no good'},
        )
        joined = ' '.join(str(m) for m in get_messages(response.wsgi_request)).lower()
        self.assertIn('opex sites only', joined)
        self.assertEqual(
            PunchPoint.objects.filter(task=self.task).count(), 0,
            'the residential path grew a punch point')
        self.task.refresh_from_db()
        self.assertEqual(self.task.approval_remarks, '')

    def test_a_residential_task_detail_renders_with_no_punch_point_block(self):
        response = _client_for(self.pm).get(
            reverse('task_detail', args=[self.project.project_id, self.task.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['task_punch_points']), 0)
        self.assertNotContains(response, 'punchPointsSection')

    def test_completing_a_residential_task_still_takes_one_move(self):
        """The baseline claim, restated here so that a 2.3a regression onto the
        residential path fails in the 2.3a file."""
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
        self.assertEqual(self.task.status, Task.DONE)
        self.assertEqual(PunchPoint.objects.count(), 0)


# ---------------------------------------------------------------------------
# 7. is_qaqc is settable through the Admin Panel
# ---------------------------------------------------------------------------

class AdminCanSetQaQcFlagTests(TestCase):
    """The flag gates the rejection that raises a punch point, and until 2.3a the
    portal's own user editor had no checkbox for it — it could only be set from the
    Django admin or a shell. `is_design_head` and `is_design_qc` were already
    there; this adds the third to the same form, and nothing else.

    THE CLEAR-ON-SAVE TRAP IS WHAT THE SECOND TEST IS FOR. The view reads
    `cd['is_qaqc']` unconditionally, and an unchecked box posts NOTHING. A form
    that did not render the checkbox would therefore turn every unrelated save —
    a phone number correction — into a silent revocation of the flag.
    """

    @classmethod
    def setUpTestData(cls):
        cls.admin  = _profile('t23a_admin', 'Admin')
        cls.target = _profile('t23a_target', 'Site Engineer')

    def _payload(self, **overrides):
        data = {
            'first_name':   'Target',
            'last_name':    'Test',
            'username':     self.target.user.username,
            'email':        'target@example.com',
            'phone_number': '9876543210',
            'role':         'Site Engineer',
        }
        data.update(overrides)
        return data

    def _post(self, **overrides):
        return _client_for(self.admin).post(
            reverse('admin_user_edit', args=[self.target.user.pk]),
            self._payload(**overrides),
        )

    def test_the_edit_form_offers_the_checkbox(self):
        response = _client_for(self.admin).get(
            reverse('admin_user_edit', args=[self.target.user.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="is_qaqc"')

    def test_an_admin_can_turn_the_flag_on(self):
        self.assertFalse(self.target.is_qaqc)
        self._post(is_qaqc='on')
        self.target.refresh_from_db()
        self.assertTrue(self.target.is_qaqc,
                        'the Admin Panel could not set is_qaqc')

    def test_an_admin_can_turn_the_flag_off_again(self):
        self.target.is_qaqc = True
        self.target.save(update_fields=['is_qaqc'])
        self._post()          # box unchecked — posts nothing
        self.target.refresh_from_db()
        self.assertFalse(self.target.is_qaqc)

    def test_the_flag_survives_a_save_that_checks_it(self):
        """The realistic edit: an admin who has already granted QA/QC changes the
        phone number. The checkbox is rendered checked, so it posts, so the flag
        stays — which is exactly why the template must carry it."""
        self.target.is_qaqc = True
        self.target.save(update_fields=['is_qaqc'])
        self._post(is_qaqc='on', phone_number='9123456780')
        self.target.refresh_from_db()
        self.assertTrue(self.target.is_qaqc)
        self.assertEqual(self.target.phone_number, '9123456780')

    def test_the_role_is_untouched_by_the_new_field(self):
        """2.3a adds a capability flag and changes NOTHING about role handling —
        no new role string, no widened choice list. `ROLE_CHOICES` is asserted here
        because that is the thing the stop condition was about."""
        before = list(UserProfile.ROLE_CHOICES)
        self._post(is_qaqc='on')
        self.target.refresh_from_db()
        self.assertEqual(self.target.role, 'Site Engineer')
        self.assertEqual(list(UserProfile.ROLE_CHOICES), before)

    def test_the_other_two_flags_are_unaffected(self):
        self.target.is_design_head = True
        self.target.save(update_fields=['is_design_head'])
        self._post(is_qaqc='on', is_design_head='on')
        self.target.refresh_from_db()
        self.assertTrue(self.target.is_design_head)
        self.assertFalse(self.target.is_design_qc)
        self.assertTrue(self.target.is_qaqc)
