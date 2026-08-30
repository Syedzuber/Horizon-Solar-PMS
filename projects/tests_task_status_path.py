"""
The one task status-change path. Prompt B8.

WHY THIS FILE EXISTS
--------------------
Before B8 there were TWO ~180-line copies of the task status decision — one in
`task_status_update` (the project-overview row control) and one in
`task_detail_status_update` (the task-detail status block) — reached from two
screens the same person uses interchangeably. As 1.4a put it: a rule added to one
is not enforced, merely avoidable.

B8 extracted the shared decision into `_apply_task_status_change()`. The existing
suite already covers "each view still works". THIS FILE COVERS SOMETHING ELSE:
that BOTH VIEWS BEHAVE IDENTICALLY where they are supposed to, and that where they
deliberately DO NOT, the difference is the one that was decided rather than one that
drifted in.

That distinction is why every contract test below runs twice — once per entry point —
through two concrete subclasses of one mixin. A rule that stops holding on one screen
fails a named test rather than becoming a support ticket eighteen months later.

Coverage before this file, for scale: `task_status_update` had 16 test methods across
three modules; `task_detail_status_update` had exactly ONE, asserting only that a
ledger row appeared. The detail screen's permission gate, due-date precondition,
blocked/issue path and milestone sync were characterised nowhere.

WHAT IS DELIBERATELY *NOT* ASSERTED TO BE IDENTICAL
---------------------------------------------------
Four differences between the two pre-B8 copies were found by B8's pre-flight and
PRESERVED rather than resolved, because resolving any of them would have been a
behaviour change and B8's remit was explicitly "no new rule, no behaviour change".
Each is pinned by a test in `DecidedDifferenceTests` so the resolution lives in code
and not only in a commit message. See EXECUTION_MODULE_DEFERRED.md B12-B15.

Run with:
    python manage.py test projects --settings=solarpms.test_settings
"""
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse

from .models import (
    ActivityLog, BOQItemMaster, Issue, Project, StatusTransition, Task,
    UserProfile, REASON_BLOCKED, REASON_UNBLOCKED, SUBJECT_TASK,
)
from .utils import RESIDENTIAL_FINANCE_ASSIGNEE_EMAIL, assign_tasks_to


def _profile(username, role, email=''):
    """Create a User and set its auto-created UserProfile's role.

    A post_save signal on User creates the UserProfile, so we fetch and mutate
    rather than creating a second one. Same helper shape as the ledger suite.
    """
    user = User.objects.create_user(username=username, password='pw12345', email=email)
    profile = user.profile
    profile.role = role
    profile.is_active = True
    profile.save()
    return profile


def _client_for(profile):
    client = Client()
    client.force_login(profile.user)
    return client


class TaskStatusPathFixture(TestCase):
    """One activated Residential project whose Site Engineer can drive BOTH screens.

    The actor matters more than usual here. The two entry points do not admit the
    same people — the overview row is role-or-PM, the detail block is assignee-only —
    so a test that runs through both needs someone who satisfies both at once. A Site
    Engineer who is ALSO the specific `assigned_to` on a Site Engineer task is exactly
    that person, and is the ordinary case in production rather than a contrivance.
    """

    def setUp(self):
        # Activation raises and rolls back without this account.
        self.finance = _profile('b8_fin', 'Finance',
                                email=RESIDENTIAL_FINANCE_ASSIGNEE_EMAIL)
        self.pm     = _profile('b8_pm', 'PM')
        self.se     = _profile('b8_se', 'Site Engineer')
        self.design = _profile('b8_design', 'Design')

        # Migrations are disabled under test_settings, so the catalogue data migration
        # never runs and activation's BOQ seeding would raise on an empty catalogue.
        for order, (code, desc, cat, unit) in enumerate([
            ('ITM-001', 'Solar Module 540Wp',        'Solar Modules', 'Nos'),
            ('ITM-002', 'Module Mounting Structure', 'Structure',     'Nos'),
            ('ITM-003', 'String Inverter 5kW',       'Inverter',      'Nos'),
        ], start=1):
            BOQItemMaster.objects.create(
                code=code, description=desc, category=cat, unit=unit,
                project_type='Residential', is_active=True, sort_order=order,
            )

        self.project = Project.objects.create(
            customer_name='B8 Residence',
            customer_phone='9876543210',
            site_address='1 Consolidation Way',
            city='Lucknow',
            project_type='Residential',
            capacity_kw=Decimal('5.00'),
            contract_value=Decimal('300000.00'),
            status='Draft',
            assigned_pm=self.pm,
            target_commissioning_date=date.today() + timedelta(days=90),
        )
        response = _client_for(self.pm).post(
            reverse('project_activate', args=[self.project.project_id]),
            {'assigned_design_id': self.design.pk},
        )
        self.assertEqual(response.status_code, 302, 'activation did not redirect')
        self.project.refresh_from_db()
        self.assertEqual(self.project.status, 'Active')

        # Activation deliberately leaves every SE task unassigned — hand this
        # project's engineer their real tasks, as a PM does through task_assign.
        assign_tasks_to(
            Task.objects.filter(phase__project=self.project,
                                assigned_role=Task.SITE_ENGINEER),
            self.se,
        )

    # -- helpers -------------------------------------------------------------

    def _se_task(self, due_date=None):
        """A Site Engineer task on this project, assigned to self.se.

        `due_date` is set directly rather than through the view, because the
        In-Progress guard is a precondition of the cases under test and not the
        thing under test.
        """
        task = Task.objects.filter(
            phase__project=self.project,
            assigned_role=Task.SITE_ENGINEER,
            assigned_to=self.se,
        ).order_by('pk').first()
        self.assertIsNotNone(task, 'fixture produced no assigned SE task')
        if due_date is not None:
            Task.objects.filter(pk=task.pk).update(due_date=due_date)
            task.refresh_from_db()
        return task

    def _ledger(self, task):
        """This task's ledger rows, oldest first."""
        return list(StatusTransition.objects.filter(
            subject_type=SUBJECT_TASK, subject_id=task.pk,
        ).order_by('occurred_at', 'pk'))

    def _status_logs(self, task):
        return list(ActivityLog.objects.filter(
            entity_type='Task', entity_id=task.pk,
            action_code__startswith='task_status_',
        ).order_by('pk'))


class TaskStatusPathContract:
    """Every assertion in here runs through BOTH entry points.

    Subclasses set `ENTRY` to a URL name and inherit the whole contract. Anything
    asserted here is a property of `_apply_task_status_change()`, which is to say a
    property of the system rather than of one screen — so a change that breaks it on
    one screen only cannot pass.
    """

    ENTRY = None  # set by the concrete subclasses below

    def _post(self, task, data, **extra):
        return _client_for(self.se).post(
            reverse(self.ENTRY, args=[self.project.project_id, task.pk]),
            data, **extra
        )

    # -- 1. legal transitions ------------------------------------------------

    def test_not_started_to_in_progress_is_applied(self):
        task = self._se_task(due_date=date.today())
        self._post(task, {'status': Task.IN_PROGRESS})
        task.refresh_from_db()
        self.assertEqual(task.status, Task.IN_PROGRESS)

    def test_in_progress_to_done_is_applied_and_stamps_completed_at(self):
        task = self._se_task(due_date=date.today())
        Task.objects.filter(pk=task.pk).update(status=Task.IN_PROGRESS)
        task.refresh_from_db()
        self._post(task, {'status': Task.DONE})
        task.refresh_from_db()
        self.assertEqual(task.status, Task.DONE)
        self.assertIsNotNone(task.completed_at,
                             'Done must stamp completed_at through either screen')

    def test_not_started_straight_to_done_is_applied(self):
        """The transition table allows it, so both screens must."""
        task = self._se_task(due_date=date.today())
        self._post(task, {'status': Task.DONE})
        task.refresh_from_db()
        self.assertEqual(task.status, Task.DONE)

    def test_in_progress_requires_a_due_date_through_both(self):
        task = self._se_task()
        self.assertIsNone(task.due_date, 'fixture task unexpectedly has a due date')
        self._post(task, {'status': Task.IN_PROGRESS})
        task.refresh_from_db()
        self.assertEqual(task.status, Task.NOT_STARTED)
        self.assertEqual(self._ledger(task), [],
                         'a refused move must not write a ledger row')

    def test_an_inline_due_date_satisfies_the_guard_through_both(self):
        task = self._se_task()
        self._post(task, {'status': Task.IN_PROGRESS,
                          'due_date': date.today().isoformat()})
        task.refresh_from_db()
        self.assertEqual(task.status, Task.IN_PROGRESS)
        self.assertEqual(task.due_date, date.today())

    # -- 2. illegal transitions ----------------------------------------------

    def test_done_may_not_return_to_in_progress_through_either(self):
        """Done → In Progress is refused: it is how completion gets gamed."""
        task = self._se_task(due_date=date.today())
        Task.objects.filter(pk=task.pk).update(status=Task.DONE)
        task.refresh_from_db()
        self._post(task, {'status': Task.IN_PROGRESS})
        task.refresh_from_db()
        self.assertEqual(task.status, Task.DONE)
        self.assertEqual(self._ledger(task), [])

    def test_done_may_not_return_to_not_started_through_either(self):
        task = self._se_task(due_date=date.today())
        Task.objects.filter(pk=task.pk).update(status=Task.DONE)
        task.refresh_from_db()
        self._post(task, {'status': Task.NOT_STARTED})
        task.refresh_from_db()
        self.assertEqual(task.status, Task.DONE)

    def test_a_status_outside_the_choices_is_refused_through_either(self):
        task = self._se_task(due_date=date.today())
        self._post(task, {'status': 'Nearly Done'})
        task.refresh_from_db()
        self.assertEqual(task.status, Task.NOT_STARTED)
        self.assertEqual(self._ledger(task), [])

    def test_an_empty_status_is_refused_through_either(self):
        task = self._se_task(due_date=date.today())
        self._post(task, {'status': ''})
        task.refresh_from_db()
        self.assertEqual(task.status, Task.NOT_STARTED)

    # -- 3. the blocked branch -----------------------------------------------

    def test_blocking_without_a_reason_is_refused_through_either(self):
        task = self._se_task(due_date=date.today())
        self._post(task, {'status': Task.BLOCKED})
        task.refresh_from_db()
        self.assertEqual(task.status, Task.NOT_STARTED)
        self.assertEqual(Issue.objects.filter(task=task).count(), 0)
        self.assertEqual(self._ledger(task), [])

    def test_blocking_with_a_reason_creates_the_issue_through_either(self):
        task = self._se_task(due_date=date.today())
        self._post(task, {'status': Task.BLOCKED,
                          'block_issue_title': 'Crane unavailable'})
        task.refresh_from_db()
        self.assertEqual(task.status, Task.BLOCKED)
        issue = Issue.objects.get(task=task)
        self.assertEqual(issue.title, 'Crane unavailable')
        self.assertEqual(issue.status, Issue.OPEN)
        self.assertEqual(issue.raised_by, self.se)

    def test_blocking_stamps_blocked_since_through_either(self):
        task = self._se_task(due_date=date.today())
        self._post(task, {'status': Task.BLOCKED,
                          'block_issue_title': 'Crane unavailable'})
        task.refresh_from_db()
        self.assertIsNotNone(task.blocked_since,
                             'the CEO aged-KPI depends on this stamp, from either screen')

    def test_unblocking_clears_blocked_since_through_either(self):
        """Re-blocking must re-age from zero, not from the original block date."""
        task = self._se_task(due_date=date.today())
        self._post(task, {'status': Task.BLOCKED,
                          'block_issue_title': 'Crane unavailable'})
        self._post(task, {'status': Task.IN_PROGRESS})
        task.refresh_from_db()
        self.assertEqual(task.status, Task.IN_PROGRESS)
        self.assertIsNone(task.blocked_since)

    # -- 4. the ledger row ---------------------------------------------------

    def test_a_ledger_row_is_written_with_the_same_shape_through_either(self):
        task = self._se_task(due_date=date.today())
        self._post(task, {'status': Task.IN_PROGRESS})
        rows = self._ledger(task)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.subject_type, SUBJECT_TASK)
        self.assertEqual(row.subject_id, task.pk)
        self.assertEqual(row.from_status, Task.NOT_STARTED)
        self.assertEqual(row.to_status, Task.IN_PROGRESS)
        self.assertEqual(row.actor, self.se)

    def test_an_ordinary_ladder_move_carries_no_reason_code_through_either(self):
        """Inventing a reason would be a lie in a column people will group by."""
        task = self._se_task(due_date=date.today())
        self._post(task, {'status': Task.IN_PROGRESS})
        self.assertEqual(self._ledger(task)[0].reason_code, '')

    def test_blocking_records_the_reason_and_the_hold_text_through_either(self):
        task = self._se_task(due_date=date.today())
        self._post(task, {'status': Task.BLOCKED,
                          'block_issue_title': 'Crane unavailable'})
        row = self._ledger(task)[-1]
        self.assertEqual(row.to_status, Task.BLOCKED)
        self.assertEqual(row.reason_code, REASON_BLOCKED)
        self.assertEqual(row.remark, 'Crane unavailable')

    def test_leaving_blocked_records_unblocked_through_either(self):
        task = self._se_task(due_date=date.today())
        self._post(task, {'status': Task.BLOCKED,
                          'block_issue_title': 'Crane unavailable'})
        self._post(task, {'status': Task.IN_PROGRESS})
        row = self._ledger(task)[-1]
        self.assertEqual(row.from_status, Task.BLOCKED)
        self.assertEqual(row.to_status, Task.IN_PROGRESS)
        self.assertEqual(row.reason_code, REASON_UNBLOCKED)

    # -- 5. the activity log -------------------------------------------------

    def test_an_activity_row_is_written_through_either(self):
        task = self._se_task(due_date=date.today())
        self._post(task, {'status': Task.IN_PROGRESS})
        logs = self._status_logs(task)
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0].action_code, 'task_status_in_progress')
        self.assertEqual(logs[0].actor, self.se)
        self.assertEqual(logs[0].project, self.project)

    def test_the_blocked_branch_logs_the_issue_not_the_status_through_either(self):
        """Blocking has its own log line; it must not also write a status line."""
        task = self._se_task(due_date=date.today())
        self._post(task, {'status': Task.BLOCKED,
                          'block_issue_title': 'Crane unavailable'})
        self.assertEqual(self._status_logs(task), [])
        self.assertEqual(
            ActivityLog.objects.filter(entity_type='Issue',
                                       action_code='issue_created').count(), 1)

    def test_a_refused_move_writes_no_activity_row_through_either(self):
        task = self._se_task(due_date=date.today())
        Task.objects.filter(pk=task.pk).update(status=Task.DONE)
        self._post(task, {'status': Task.IN_PROGRESS})
        self.assertEqual(self._status_logs(task), [])


class OverviewRowPathTests(TaskStatusPathContract, TaskStatusPathFixture):
    """The contract, driven through the project-overview row control (① ②)."""

    ENTRY = 'task_status_update'


class TaskDetailPathTests(TaskStatusPathContract, TaskStatusPathFixture):
    """The contract, driven through the task-detail status block (③ ④).

    Before B8 this screen had ONE test in the entire repository.
    """

    ENTRY = 'task_detail_status_update'


# ---------------------------------------------------------------------------
# The differences that were decided, not discovered
# ---------------------------------------------------------------------------

class DecidedDifferenceTests(TaskStatusPathFixture):
    """The four ways the two screens still differ, each pinned deliberately.

    B8's pre-flight found these and did NOT resolve them: every non-preserve answer
    would have been a behaviour change, which B8's remit forbade. They are recorded
    in EXECUTION_MODULE_DEFERRED.md B12-B15 for a later session to decide.

    These tests exist so that "still differs" is a fact the suite asserts, rather
    than an omission nobody notices. If a later prompt closes one of these, the
    matching test here fails LOUDLY and points at the deferred entry — which is the
    intended way to find out that a decision was made.
    """

    # -- B12: the project-scope gate is on one screen only --------------------

    def test_the_overview_path_refuses_a_user_who_cannot_view_the_project(self):
        """0.2 lockdown: the role-matcher alone let any SE move any SE task."""
        outsider = _profile('b8_outsider_pm', 'PM')
        task = self._se_task(due_date=date.today())
        Task.objects.filter(pk=task.pk).update(assigned_to=outsider)
        task.refresh_from_db()
        response = _client_for(outsider).post(
            reverse('task_status_update', args=[self.project.project_id, task.pk]),
            {'status': Task.IN_PROGRESS},
        )
        self.assertEqual(response.status_code, 404)
        task.refresh_from_db()
        self.assertEqual(task.status, Task.NOT_STARTED)

    def test_the_detail_path_has_no_project_scope_gate_and_lets_the_same_user_through(self):
        """B12, PINNED AS-IS AND NOT ENDORSED.

        The same PM-role user, on the same task, on a project they neither manage
        nor can otherwise see, is refused by the overview screen above and admitted
        here — because `task_detail_status_update` has no `user_can_view_project`
        check at all. Its only gate is `assigned_to`.

        Preserved by B8 rather than closed, because closing it is a new gate. When a
        later prompt adds the gate, THIS TEST SHOULD FAIL and be replaced by its
        opposite. See EXECUTION_MODULE_DEFERRED.md B12.
        """
        outsider = _profile('b8_outsider_pm2', 'PM')
        task = self._se_task(due_date=date.today())
        Task.objects.filter(pk=task.pk).update(assigned_to=outsider)
        task.refresh_from_db()
        response = _client_for(outsider).post(
            reverse('task_detail_status_update',
                    args=[self.project.project_id, task.pk]),
            {'status': Task.IN_PROGRESS},
        )
        self.assertEqual(response.status_code, 302)
        task.refresh_from_db()
        self.assertEqual(task.status, Task.IN_PROGRESS)

    # -- B13: unassigned task, 400-JSON vs 403 --------------------------------

    def test_the_overview_path_answers_an_unassigned_task_with_400_json(self):
        task = Task.objects.filter(
            phase__project=self.project, assigned_to__isnull=True,
        ).order_by('pk').first()
        self.assertIsNotNone(task)
        response = _client_for(self.pm).post(
            reverse('task_status_update', args=[self.project.project_id, task.pk]),
            {'status': Task.IN_PROGRESS},
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()['success'])

    def test_the_detail_path_answers_an_unassigned_task_with_403(self):
        """B13: same condition, different answer. Preserved, not endorsed."""
        task = Task.objects.filter(
            phase__project=self.project, assigned_to__isnull=True,
        ).order_by('pk').first()
        response = _client_for(self.pm).post(
            reverse('task_detail_status_update',
                    args=[self.project.project_id, task.pk]),
            {'status': Task.IN_PROGRESS},
        )
        self.assertEqual(response.status_code, 403)

    # -- B14: HTMX permission refusal, rendered row vs bare 403 ---------------

    def test_the_overview_path_renders_a_row_when_refusing_over_htmx(self):
        """A 403 body swapped into a row target is not a usable answer.

        The actor is this project's Design user: `assigned_design` makes them able to
        SEE the project, so the scope gate passes and the request reaches the
        role-match refusal — which is the branch under test. A stranger would 404 at
        the gate and pin nothing.
        """
        task = self._se_task(due_date=date.today())
        response = _client_for(self.design).post(
            reverse('task_status_update', args=[self.project.project_id, task.pk]),
            {'status': Task.IN_PROGRESS},
            HTTP_HX_REQUEST='true',
        )
        self.assertEqual(response.status_code, 200)
        task.refresh_from_db()
        self.assertEqual(task.status, Task.NOT_STARTED)

    def test_the_detail_path_returns_a_bare_403_when_refusing_over_htmx(self):
        """B14: preserved, not endorsed."""
        task = self._se_task(due_date=date.today())
        response = _client_for(self.design).post(
            reverse('task_detail_status_update',
                    args=[self.project.project_id, task.pk]),
            {'status': Task.IN_PROGRESS},
            HTTP_HX_REQUEST='true',
        )
        self.assertEqual(response.status_code, 403)

    # -- B15: ?next= honoured on one screen only ------------------------------

    def test_the_overview_path_honours_a_local_next(self):
        task = self._se_task(due_date=date.today())
        response = _client_for(self.se).post(
            reverse('task_status_update', args=[self.project.project_id, task.pk]),
            {'status': Task.IN_PROGRESS, 'next': '/dashboard/se/'},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], '/dashboard/se/')

    def test_the_overview_path_ignores_an_offsite_next(self):
        task = self._se_task(due_date=date.today())
        response = _client_for(self.se).post(
            reverse('task_status_update', args=[self.project.project_id, task.pk]),
            {'status': Task.IN_PROGRESS, 'next': 'https://evil.example.com/'},
        )
        self.assertEqual(response.status_code, 302)
        self.assertNotIn('evil.example.com', response['Location'])

    def test_the_detail_path_ignores_next_entirely(self):
        """B15: preserved, not endorsed — the detail screen always returns to itself."""
        task = self._se_task(due_date=date.today())
        response = _client_for(self.se).post(
            reverse('task_detail_status_update',
                    args=[self.project.project_id, task.pk]),
            {'status': Task.IN_PROGRESS, 'next': '/dashboard/se/'},
        )
        self.assertEqual(response.status_code, 302)
        self.assertNotEqual(response['Location'], '/dashboard/se/')
