"""
Tests for the one profile<->task role mapping (R-19), and for Project Coordinator
reaching tasks through it.

Prompt K5. `_PROFILE_TO_TASK_ROLE` became a module-level constant in 0.2b; its
inverse stayed as two local dicts in `task_assign` and `project_overview` until
this session derived it from the forward map. `EXECUTION_MODULE_DEFERRED.md` A3
and `DESIGN_MODULE_DEFERRED.md` K5 are the entries that asked for it.

TWO KINDS OF TEST HERE, AND THE STRUCTURAL ONE IS THE DURABLE ONE.

The behavioural tests below prove a coordinator can act on a coordinator task
today. The structural test proves the mapping stays total tomorrow — it walks
`Task.ROLE_CHOICES` and fails the day someone adds a role string without wiring
it, which is exactly what 1.3a did and what this session found.

WHAT K5'S PRE-FLIGHT SET OUT TO TEST, AND WHAT IS ACTUALLY TESTABLE — read this
before adding a test here that "should" pass.

The intent was to isolate the forward mapping on `task_status_update` by using a
coordinator who is NOT on `project.coordinators`, so that the gate's
`role_mismatch AND NOT is_pm` would turn on the role comparison alone. THAT
FIXTURE CANNOT EXIST. For a Project Coordinator, `user_can_view_project()` and
`user_can_manage_project()` are the SAME PREDICATE — the role has no
task-relation branch of its own, unlike Site Engineer and Design, so it sees
exactly the projects it coordinates (permissions.py, `user_can_view_project`
docstring, "ASSIGNMENT-BASED"). `task_status_update` applies the 0.2 view-scope
lockdown BEFORE the role gate. So:

  * a coordinator NOT on the project is refused at the scope gate — 404, never
    reaching the role comparison;
  * a coordinator ON the project has `is_pm=True`, so the role comparison is
    short-circuited.

THE FORWARD MAP IS THEREFORE UNREACHABLE FOR THIS ROLE on `task_status_update`,
on `task_set_due_date` and in `_user_can_complete_checklist_item` — all three
check manage authority first. That is a finding, not a gap to paper over, and
the tests below pin the rules AS THEY ARE rather than asserting a path that does
not exist. What genuinely decides coordinator behaviour is the INVERSE map, in
the candidate lists (`CoordinatorMatchSiteTests`), and `assigned_to` on the
detail page — the one status route with no view-scope gate (B12) and no role
comparison at all.

A second trap, found the same way: `_apply_task_status_change()` refuses
IN_PROGRESS on a task with no `due_date`. A fixture without one is refused for
that reason and a permission test built on it proves nothing. Every task here
carries a due date.
"""
from datetime import date
from decimal import Decimal

from django.test import TestCase, Client
from django.contrib.auth.models import User

from .models import Project, ProjectPhase, Task, UserProfile
from .views import (
    _PROFILE_TO_TASK_ROLE,
    _TASK_TO_PROFILE_ROLE,
    _apply_task_status_change,
)


def _make_user(username, role):
    # A post_save signal on User auto-creates the UserProfile — fetch and set its role
    # rather than creating a second one (which would violate the OneToOne constraint).
    user = User.objects.create_user(username=username, password='pw12345')
    profile = user.profile
    profile.role = role
    profile.save(update_fields=['role'])
    return user, profile


class RoleMappingStructureTests(TestCase):
    """The guard. No fixtures, no HTTP — this is about the constants alone."""

    # 4 — THE GUARD (Task 3 #4, shape B).
    #
    # Every Task.ROLE_CHOICES value, mapped backwards, must land on a real
    # UserProfile.ROLE_CHOICES value. This is the invariant whose violation IS the
    # silent permission refusal: a task role that resolves to a profile role nobody
    # can hold gives its owner a 403 with nothing logged and nothing naming the cause.
    #
    # Shape B, deliberately, over "every task role has a dict entry": the mapping is
    # differences-only, so an identity entry {'Foo': 'Foo'} would satisfy a literal
    # membership check while proving nothing about whether 'Foo' is a role a user can
    # actually hold. This assertion catches BOTH directions of the Design Head class
    # of bug — a task role with no profile counterpart, and (below) the reverse.
    #
    # The list is DERIVED from the choices, never hardcoded: a hardcoded tuple is a
    # third place to forget to add the role, which is the bug this test exists for.
    def test_every_task_role_maps_back_to_a_real_profile_role(self):
        profile_roles = {value for value, _ in UserProfile.ROLE_CHOICES}
        unmapped = []
        for task_role, _ in Task.ROLE_CHOICES:
            resolved = _TASK_TO_PROFILE_ROLE.get(task_role, task_role)
            if resolved not in profile_roles:
                unmapped.append((task_role, resolved))

        self.assertEqual(
            unmapped, [],
            'Task.ROLE_CHOICES values that do not resolve to a real '
            'UserProfile.ROLE_CHOICES value: '
            + '; '.join(f'{t!r} -> {r!r}' for t, r in unmapped)
            + '. Add the role to _PROFILE_TO_TASK_ROLE in views.py (R-19), or to '
              'UserProfile.ROLE_CHOICES. A task role nobody can hold gives its owner '
              'a silent permission refusal.'
        )

    # The reverse direction, for the Design Head case specifically: a profile role
    # that resolves to no task role can never act on a task. This is NOT asserted as
    # a failure — Admin, CEO, System Admin and Project Coordinator legitimately have
    # no Residential template task. It is pinned as a RECORD of which roles are in
    # that position, so that moving a role into or out of it is a visible diff.
    def test_profile_roles_with_no_task_counterpart_are_the_expected_set(self):
        task_roles = {value for value, _ in Task.ROLE_CHOICES}
        without = sorted(
            value for value, _ in UserProfile.ROLE_CHOICES
            if _PROFILE_TO_TASK_ROLE.get(value, value) not in task_roles
        )
        self.assertEqual(without, ['Admin', 'CEO', 'System Admin'])

    # 3 — round-trip, every entry, both directions.
    def test_forward_and_inverse_round_trip(self):
        for profile_role, task_role in _PROFILE_TO_TASK_ROLE.items():
            self.assertEqual(_TASK_TO_PROFILE_ROLE[task_role], profile_role)
        for task_role, profile_role in _TASK_TO_PROFILE_ROLE.items():
            self.assertEqual(_PROFILE_TO_TASK_ROLE[profile_role], task_role)

    # The inverse is DERIVED, so a collision would silently drop an entry rather than
    # raise. Same cardinality proves the forward map's values were unique.
    def test_inverse_is_a_strict_inverse(self):
        self.assertEqual(len(_TASK_TO_PROFILE_ROLE), len(_PROFILE_TO_TASK_ROLE))

    # Project Coordinator needs no dict entry — the two sides spell it identically and
    # `.get(x, x)` passes it through. Pinned because "add the role to the map" is the
    # obvious wrong fix, and an identity entry here would be dead weight that a later
    # reader would copy for the next role.
    def test_coordinator_resolves_by_passthrough_not_by_an_entry(self):
        self.assertNotIn('Project Coordinator', _PROFILE_TO_TASK_ROLE)
        self.assertEqual(
            _PROFILE_TO_TASK_ROLE.get('Project Coordinator', 'Project Coordinator'),
            Task.PROJECT_COORDINATOR,
        )


class CoordinatorTaskActionTests(TestCase):
    """A Project Coordinator acting on a task whose assigned_role is theirs."""

    def setUp(self):
        self.client = Client(SERVER_NAME='localhost')

        self.pm_user, self.pm = _make_user('k5pm', 'PM')

        # ON the project. For a coordinator this is the ONLY way to reach a task at
        # all — see the module docstring — and it carries manage authority with it.
        self.member_coord_user, self.member_coord = _make_user('k5coord_on', 'Project Coordinator')

        # NOT on the project. Cannot see it: used to pin the scope refusal.
        self.outsider_coord_user, self.outsider_coord = _make_user('k5coord_off', 'Project Coordinator')

        self.se_user, self.se = _make_user('k5se', 'Site Engineer')

        self.project = Project.objects.create(
            customer_name='Coordinator Co',
            customer_phone='9876543211',
            site_address='2 Sun Rd',
            city='Lucknow',
            project_type='Residential',
            capacity_kw=Decimal('4.00'),
            contract_value=Decimal('120000.00'),
            target_commissioning_date=date(2026, 12, 1),
            status='Active',
            assigned_pm=self.pm,
        )
        self.project.coordinators.add(self.member_coord)

        self.phase = ProjectPhase.objects.create(
            project=self.project, phase_name='Phase 1', phase_order=1,
        )
        # Assigned, and with a due date: both entry points refuse an UNASSIGNED task
        # before the role gate is reached, and the helper refuses IN_PROGRESS with no
        # due date. Either omission would refuse for the wrong reason and let a
        # permission test pass without proving anything — the shape of trap 1.3a
        # recorded for the mirror refusal.
        self.coord_task = Task.objects.create(
            phase=self.phase, task_name='Completion Certificates', task_order=1,
            assigned_role=Task.PROJECT_COORDINATOR, task_type=Task.INTERNAL,
            assigned_to=self.member_coord, due_date=date(2026, 11, 1),
        )
        self.se_task = Task.objects.create(
            phase=self.phase, task_name='MMS Installation', task_order=2,
            assigned_role=Task.SITE_ENGINEER, task_type=Task.INTERNAL,
            assigned_to=self.se, due_date=date(2026, 11, 1),
        )

    def _overview_url(self, task):
        return f'/projects/{self.project.project_id}/tasks/{task.pk}/update/'

    def _detail_url(self, task):
        return f'/projects/{self.project.project_id}/tasks/{task.pk}/detail-status/'

    # THE LOAD-BEARING FACT, pinned first because every other test in this class
    # depends on it: for a Project Coordinator, seeing a project and managing it are
    # the same predicate. This is why the forward map cannot decide anything for this
    # role on the status path, and it is what a future session must re-check before
    # concluding the mapping is what let a coordinator through.
    def test_for_a_coordinator_view_authority_equals_manage_authority(self):
        from .permissions import user_can_view_project, user_can_manage_project
        for user in (self.member_coord_user, self.outsider_coord_user):
            self.assertEqual(
                user_can_view_project(user, self.project),
                user_can_manage_project(user, self.project),
                f'{user.username}: coordinator view and manage authority diverged — '
                'the role has gained a task-relation branch in user_can_view_project(), '
                'which makes the forward role map reachable for it. Revisit this module.',
            )

    # 1a — ENTRY POINT ONE: task_status_update, the project-overview row control.
    # Reached, and the status lands. Note WHY it is allowed: manage authority, not the
    # role match — the role comparison is short-circuited by `is_pm`. Asserted anyway,
    # because a coordinator being able to move their own task is the behaviour 1.3c
    # depends on, whichever clause delivers it.
    def test_coordinator_moves_own_task_via_overview_row(self):
        self.client.login(username='k5coord_on', password='pw12345')
        resp = self.client.post(self._overview_url(self.coord_task), {'status': Task.IN_PROGRESS})
        self.assertIn(resp.status_code, (200, 302))
        self.coord_task.refresh_from_db()
        self.assertEqual(self.coord_task.status, Task.IN_PROGRESS)

    # 1b — ENTRY POINT TWO: task_detail_status_update, the task-detail status block.
    # User-level gate (assigned_to). This route has NO view-scope check (B12) and no
    # role comparison, so it is the one status path where a coordinator's own task
    # moves on the strength of the assignment alone.
    def test_coordinator_moves_own_task_via_detail_page(self):
        self.client.login(username='k5coord_on', password='pw12345')
        resp = self.client.post(self._detail_url(self.coord_task), {'status': Task.IN_PROGRESS})
        self.assertIn(resp.status_code, (200, 302))
        self.coord_task.refresh_from_db()
        self.assertEqual(self.coord_task.status, Task.IN_PROGRESS)

    # The helper itself, called directly — no view, no gate, no HTTP. Proves
    # _apply_task_status_change() accepts a coordinator profile and that the write
    # lands, independent of either caller's permission model.
    def test_helper_applies_a_coordinator_status_change(self):
        from django.test import RequestFactory
        from django.contrib.messages.storage.fallback import FallbackStorage

        request = RequestFactory().post('/')
        request.user = self.member_coord_user
        request.session = self.client.session
        request._messages = FallbackStorage(request)

        _apply_task_status_change(
            self.coord_task, Task.IN_PROGRESS, self.member_coord, request, self.project,
        )
        self.coord_task.refresh_from_db()
        self.assertEqual(self.coord_task.status, Task.IN_PROGRESS)

    # 2 — a coordinator with no relationship to the project is refused. 404, NOT 403:
    # the 0.2 view-scope lockdown fires before the role gate, so the answer is "no such
    # project", not "not your task". Pinned at 404 deliberately — a future change that
    # turned this into a 403 would mean the scope gate had moved or gone.
    def test_outsider_coordinator_cannot_reach_a_task_at_all(self):
        self.client.login(username='k5coord_off', password='pw12345')
        resp = self.client.post(self._overview_url(self.se_task), {'status': Task.IN_PROGRESS})
        self.assertEqual(resp.status_code, 404)
        self.se_task.refresh_from_db()
        self.assertEqual(self.se_task.status, Task.NOT_STARTED)

        # ...and not on their own role's task either. Scope, not role.
        resp = self.client.post(self._overview_url(self.coord_task), {'status': Task.IN_PROGRESS})
        self.assertEqual(resp.status_code, 404)

    # 2b — the rule that lets a coordinator touch ANOTHER role's task, pinned as it
    # actually is rather than as it might seem it should be: a coordinator on the
    # project has PM-level authority through user_can_manage_project(), so the role
    # mismatch is irrelevant to them. Existing, deliberate drizzle-down authority —
    # NOT a hole this session opened, and NOT something the role mapping decides.
    def test_project_coordinator_may_move_another_roles_task_via_manage_authority(self):
        self.client.login(username='k5coord_on', password='pw12345')
        resp = self.client.post(self._overview_url(self.se_task), {'status': Task.IN_PROGRESS})
        self.assertIn(resp.status_code, (200, 302))
        self.se_task.refresh_from_db()
        self.assertEqual(self.se_task.status, Task.IN_PROGRESS)

    # The detail page is assignee-only, so it refuses a coordinator on someone else's
    # task even WITH manage authority. Pinned because it is the one place the two entry
    # points deliberately disagree.
    def test_detail_page_refuses_a_managing_coordinator_on_someone_elses_task(self):
        self.client.login(username='k5coord_on', password='pw12345')
        resp = self.client.post(self._detail_url(self.se_task), {'status': Task.IN_PROGRESS})
        self.assertEqual(resp.status_code, 403)
        self.se_task.refresh_from_db()
        self.assertEqual(self.se_task.status, Task.NOT_STARTED)


class CoordinatorMatchSiteTests(TestCase):
    """The other assigned_role match sites, for a coordinator.

    Enumerated by K5's pre-flight; these cover the ones the mapping decides.
    """

    def setUp(self):
        self.client = Client(SERVER_NAME='localhost')
        self.pm_user, self.pm = _make_user('k5pm2', 'PM')
        self.coord_user, self.coord = _make_user('k5coord2', 'Project Coordinator')

        self.project = Project.objects.create(
            customer_name='Match Site Co',
            customer_phone='9876543212',
            site_address='3 Sun Rd',
            city='Lucknow',
            project_type='Residential',
            capacity_kw=Decimal('4.00'),
            contract_value=Decimal('120000.00'),
            target_commissioning_date=date(2026, 12, 1),
            status='Active',
            assigned_pm=self.pm,
        )
        # On the project: for a coordinator this is the only way to reach any of these
        # surfaces at all (see the CoordinatorTaskActionTests docstring).
        self.project.coordinators.add(self.coord)

        self.phase = ProjectPhase.objects.create(
            project=self.project, phase_name='Phase 1', phase_order=1,
        )
        self.task = Task.objects.create(
            phase=self.phase, task_name='Completion Certificates', task_order=1,
            assigned_role=Task.PROJECT_COORDINATOR, task_type=Task.INTERNAL,
            assigned_to=self.coord,
        )

    # Match site: _user_can_complete_checklist_item() — views.py:2461.
    def test_checklist_helper_admits_a_coordinator_on_a_coordinator_task(self):
        from .views import _user_can_complete_checklist_item
        self.assertTrue(
            _user_can_complete_checklist_item(self.coord_user, self.task, self.project)
        )

    # Match site: task_assign() — views.py:4149, the inverse map building the candidate
    # queryset. A coordinator task must offer coordinator users, not an empty list.
    def test_task_assign_candidate_list_offers_coordinators(self):
        self.client.login(username='k5pm2', password='pw12345')
        resp = self.client.get(
            f'/projects/{self.project.project_id}/tasks/{self.task.pk}/assign/'
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn(self.coord, list(resp.context['candidates']))

    # Match site: task_set_due_date() — views.py:4267.
    def test_coordinator_sets_a_due_date_on_their_own_task(self):
        self.client.login(username='k5coord2', password='pw12345')
        resp = self.client.post(
            f'/projects/{self.project.project_id}/tasks/{self.task.pk}/due-date/',
            {'due_date': '2026-12-15'},
        )
        self.assertIn(resp.status_code, (200, 302))
        self.task.refresh_from_db()
        self.assertEqual(self.task.due_date, date(2026, 12, 15))

    # Match site: project_overview() — views.py:7273, candidates_by_role. The loop is
    # `for role_key, _ in Task.ROLE_CHOICES`, so a new role gets a bucket for free;
    # pinned so that stays true if the loop is ever rewritten against a fixed list.
    def test_project_overview_builds_a_candidate_bucket_for_every_task_role(self):
        self.client.login(username='k5pm2', password='pw12345')
        resp = self.client.get(f'/projects/{self.project.project_id}/overview/')
        self.assertEqual(resp.status_code, 200)
        buckets = resp.context['candidates_by_role']
        for role_key, _ in Task.ROLE_CHOICES:
            self.assertIn(role_key, buckets)
        self.assertIn(
            self.coord.pk,
            [c['pk'] for c in buckets[Task.PROJECT_COORDINATOR]],
        )
