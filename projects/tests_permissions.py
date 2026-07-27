"""
Direct unit tests for the project-scoped permission helpers in projects/permissions.py.

`user_can_manage_project()` had no direct tests before this file — every prior
assertion about it was indirect, through a view. These tests exercise both helpers
against the model layer with no HTTP involved, so a change to either function fails
here first rather than surfacing as a confusing view-level 404.

Structure:
  * TruthTableTests            — the full view/manage matrix: 10 role rows x 3 project
                                 relationships (assigned PM / coordinator / unrelated).
  * AssignmentScopingTests     — the Site Engineer and Design assignment branches.
  * DesignHeadTests            — the flag-or-role forward-compatibility expression.
  * BDPortfolioPolicyTests     — BD's portfolio-wide read, asserted as POLICY.
  * GuardTests                 — null project / profile-less user do not raise.

No migrations are involved. Run with:
    python manage.py test projects --settings=solarpms.test_settings
"""
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase

from .models import Project, ProjectPhase, Task, UserProfile
from .permissions import user_can_manage_project, user_can_view_project


def _make_user(username, role, is_design_head=False):
    """Create a User and set its auto-created UserProfile's role.

    A post_save signal on User creates the UserProfile, so we fetch and mutate it
    rather than creating a second one (which would violate the OneToOne constraint).
    Same helper shape as tests.py / tests_gantt.py.
    """
    user = User.objects.create_user(username=username, password='pw12345')
    profile = user.profile
    profile.role = role
    profile.is_design_head = is_design_head
    profile.save(update_fields=['role', 'is_design_head'])
    return user, profile


def _make_project(customer_name='Acme', assigned_pm=None, assigned_design=None):
    return Project.objects.create(
        customer_name=customer_name,
        customer_phone='9876543210',
        site_address='1 Sun Rd',
        city='Lucknow',
        project_type='Residential',
        capacity_kw=Decimal('3.00'),
        status='Active',
        assigned_pm=assigned_pm,
        assigned_design=assigned_design,
    )


def _assign_task(project, profile):
    """Give `profile` a task on `project`, creating a phase if needed.

    This is the relation both dashboard_site_engineer and dashboard_design scope on
    (`phases__tasks__assigned_to`), and therefore the one the SE/Design view branches
    must agree with.
    """
    phase = project.phases.first() or ProjectPhase.objects.create(
        project=project, phase_name='Phase 1', phase_order=1,
    )
    return Task.objects.create(
        phase=phase, task_name='Survey', task_order=1,
        assigned_role=Task.PM, task_type=Task.INTERNAL, assigned_to=profile,
    )


# Every role in UserProfile.ROLE_CHOICES that has portfolio-wide VIEW, paired with
# whether it is granted by the role string or by the is_design_head flag.
# ('Design Head' is a flag today, not a role — see permissions.user_can_view_project.)
PORTFOLIO_ROWS = [
    ('CEO',         'CEO',         False),
    ('Finance',     'Finance',     False),
    ('SCM',         'SCM',         False),
    ('Admin',       'Admin',       False),
    ('BD',          'BD',          False),
    ('Design Head', 'Design',      True),   # flag set on a Design user
]

# Roles whose VIEW access is assignment-based.
ASSIGNMENT_ROWS = [
    ('PM',                  'PM'),
    ('Project Coordinator', 'Project Coordinator'),
    ('Site Engineer',       'Site Engineer'),
    ('Design',              'Design'),
]


class TruthTableTests(TestCase):
    """The full matrix: 10 role rows x 3 project relationships, for both helpers.

    Relationships tested per row:
      1. assigned as PM  — project.assigned_pm is this user's profile
      2. coordinator     — this user's profile is in project.coordinators
      3. no relationship — neither, and no task assigned
    """

    def setUp(self):
        # Three projects; each user below is attached to them in a different way.
        self.p_pm = _make_project('AsPM')
        self.p_coord = _make_project('AsCoordinator')
        self.p_none = _make_project('Unrelated')

    def _profiles_for(self, label, role, flag=False):
        """Build three users of the same role, one per relationship."""
        slug = label.lower().replace(' ', '_').replace('&', 'and')
        u_pm, pr_pm = _make_user(f'{slug}_pm', role, flag)
        u_co, pr_co = _make_user(f'{slug}_co', role, flag)
        u_no, _ = _make_user(f'{slug}_no', role, flag)

        self.p_pm.assigned_pm = pr_pm
        self.p_pm.save(update_fields=['assigned_pm'])
        self.p_coord.coordinators.add(pr_co)

        return u_pm, u_co, u_no

    def test_portfolio_roles_view_every_project(self):
        """CEO / Finance / SCM / Admin / BD / Design Head: view True in all 3 cases."""
        for label, role, flag in PORTFOLIO_ROWS:
            with self.subTest(role=label):
                u_pm, u_co, u_no = self._profiles_for(label, role, flag)
                self.assertTrue(user_can_view_project(u_pm, self.p_pm), f'{label}/assigned-pm')
                self.assertTrue(user_can_view_project(u_co, self.p_coord), f'{label}/coordinator')
                self.assertTrue(user_can_view_project(u_no, self.p_none), f'{label}/unrelated')

    def test_assignment_roles_view_only_where_related(self):
        """PM / Coordinator / SE / Design: view True when related, False when not."""
        for label, role in ASSIGNMENT_ROWS:
            with self.subTest(role=label):
                u_pm, u_co, u_no = self._profiles_for(label, role)
                self.assertTrue(user_can_view_project(u_pm, self.p_pm), f'{label}/assigned-pm')
                self.assertTrue(user_can_view_project(u_co, self.p_coord), f'{label}/coordinator')
                self.assertFalse(user_can_view_project(u_no, self.p_none), f'{label}/unrelated')

    def test_manage_is_assignment_based_for_every_role(self):
        """MANAGE is role-blind: True iff assigned PM or coordinator, for all 10 rows.

        This pins the current behaviour of user_can_manage_project() — Prompt A must
        not change it, and no later prompt should change it without failing here.
        """
        rows = [(lbl, role, flag) for lbl, role, flag in PORTFOLIO_ROWS]
        rows += [(lbl, role, False) for lbl, role in ASSIGNMENT_ROWS]
        for label, role, flag in rows:
            with self.subTest(role=label):
                u_pm, u_co, u_no = self._profiles_for(label, role, flag)
                self.assertTrue(user_can_manage_project(u_pm, self.p_pm), f'{label}/assigned-pm')
                self.assertTrue(user_can_manage_project(u_co, self.p_coord), f'{label}/coordinator')
                self.assertFalse(user_can_manage_project(u_no, self.p_none), f'{label}/unrelated')

    def test_view_never_narrower_than_manage(self):
        """Invariant: anyone who can manage a project can see it. No exceptions."""
        rows = [(lbl, role, flag) for lbl, role, flag in PORTFOLIO_ROWS]
        rows += [(lbl, role, False) for lbl, role in ASSIGNMENT_ROWS]
        for label, role, flag in rows:
            with self.subTest(role=label):
                u_pm, u_co, u_no = self._profiles_for(label, role, flag)
                for user, project in ((u_pm, self.p_pm), (u_co, self.p_coord), (u_no, self.p_none)):
                    if user_can_manage_project(user, project):
                        self.assertTrue(user_can_view_project(user, project), label)

    def test_system_admin_is_assignment_based(self):
        """System Admin has no branch of its own, so it falls through to management.

        NOT in the design's truth table — see the audit note. Pinned here so the
        fall-through default is a decision on record rather than an accident.
        """
        u_pm, u_co, u_no = self._profiles_for('System Admin', 'System Admin', False)
        self.assertTrue(user_can_view_project(u_pm, self.p_pm))
        self.assertTrue(user_can_view_project(u_co, self.p_coord))
        self.assertFalse(user_can_view_project(u_no, self.p_none))

    def test_blank_role_is_assignment_based(self):
        """role='' is permitted by the model (blank=True) and must not grant anything."""
        u_pm, u_co, u_no = self._profiles_for('blank', '', False)
        self.assertTrue(user_can_view_project(u_pm, self.p_pm))
        self.assertFalse(user_can_view_project(u_no, self.p_none))


class AssignmentScopingTests(TestCase):
    """Site Engineer and Design see projects they hold work on, matching their dashboards."""

    def setUp(self):
        self.project = _make_project()
        self.other = _make_project('Other')

    def test_site_engineer_sees_project_with_assigned_task(self):
        user, profile = _make_user('se_task', 'Site Engineer')
        self.assertFalse(user_can_view_project(user, self.project))  # no task yet
        _assign_task(self.project, profile)
        self.assertTrue(user_can_view_project(user, self.project))
        # Scoping is per-project, not global.
        self.assertFalse(user_can_view_project(user, self.other))

    def test_site_engineer_task_grants_view_but_not_manage(self):
        """The whole point of the split: work on a project is not authority over it."""
        user, profile = _make_user('se_split', 'Site Engineer')
        _assign_task(self.project, profile)
        self.assertTrue(user_can_view_project(user, self.project))
        self.assertFalse(user_can_manage_project(user, self.project))

    def test_design_sees_project_via_assigned_design_fk(self):
        user, profile = _make_user('des_fk', 'Design')
        self.assertFalse(user_can_view_project(user, self.project))
        self.project.assigned_design = profile
        self.project.save(update_fields=['assigned_design'])
        self.assertTrue(user_can_view_project(user, self.project))
        self.assertFalse(user_can_manage_project(user, self.project))

    def test_design_sees_project_via_assigned_task(self):
        """A designer given work by the Design lead, without holding the FK."""
        user, profile = _make_user('des_task', 'Design')
        _assign_task(self.project, profile)
        self.assertTrue(user_can_view_project(user, self.project))
        self.assertFalse(user_can_view_project(user, self.other))

    def test_non_pm_role_set_as_assigned_pm_can_still_view(self):
        """The Zoho webhook can set assigned_pm to any profile, regardless of role.

        views.py:5369 matches on email with no role filter, so a Finance or Design
        profile can legitimately end up as assigned_pm. They must still see their
        own project.
        """
        user, profile = _make_user('des_as_pm', 'Design')
        self.project.assigned_pm = profile
        self.project.save(update_fields=['assigned_pm'])
        self.assertTrue(user_can_view_project(user, self.project))
        self.assertTrue(user_can_manage_project(user, self.project))


class DesignHeadTests(TestCase):
    """Design Head is the is_design_head flag today and a role string after Phase 2."""

    def setUp(self):
        self.project = _make_project()

    def test_flag_grants_portfolio_view_today(self):
        user, _ = _make_user('dh_flag', 'Design', is_design_head=True)
        self.assertTrue(user_can_view_project(user, self.project))

    def test_flag_is_independent_of_role(self):
        """The flag is documented as role-independent, so it must work on any role."""
        for role in ('Design', 'PM', 'Site Engineer', ''):
            with self.subTest(role=role or '(blank)'):
                user, _ = _make_user(f'dh_{role or "blank"}', role, is_design_head=True)
                self.assertTrue(user_can_view_project(user, self.project))

    def test_future_role_string_grants_portfolio_view(self):
        """Phase 2 promotes Design Head to a role; the check must already accept it.

        'Design Head' is not yet in UserProfile.ROLE_CHOICES, so this sets the field
        directly. `choices` is not enforced on save(), only by form validation.
        """
        user, profile = _make_user('dh_role', 'Design')
        profile.role = 'Design Head'
        profile.is_design_head = False
        profile.save(update_fields=['role', 'is_design_head'])
        self.assertTrue(user_can_view_project(user, self.project))

    def test_design_head_gets_view_not_manage(self):
        user, _ = _make_user('dh_nomanage', 'Design', is_design_head=True)
        self.assertTrue(user_can_view_project(user, self.project))
        self.assertFalse(user_can_manage_project(user, self.project))


class BDPortfolioPolicyTests(TestCase):
    """Sales & BD portfolio-wide read is DELIBERATE, SETTLED POLICY — not incidental.

    If a future change narrows BD to deal-linked or otherwise-scoped projects, the
    test below will fail. That failure is NOT a bug report: it means someone has
    changed a product decision the owner made explicitly. Treat a failure here as a
    prompt to confirm the policy change was intended, then update this test to match
    the new policy — do not "fix" permissions.py to make it pass again.

    Rationale on record: BD's existing workflow (dashboard_bd renders a flat portfolio
    queryset with no per-user term, views.py:4676-4694) works and is not being changed.
    There is no deal-scoping initiative pending.
    """

    def test_bd_can_view_project_with_no_relationship_settled_policy(self):
        user, _ = _make_user('bd_policy', 'BD')
        project = _make_project('NoRelationshipToThisBDUser')
        self.assertTrue(
            user_can_view_project(user, project),
            'BD portfolio-wide read is settled policy. If this now fails, confirm the '
            'policy change was intended before altering permissions.py — see the '
            'BDPortfolioPolicyTests docstring.',
        )

    def test_bd_portfolio_view_confers_no_management_authority(self):
        """BD sees everything; BD manages nothing it is not assigned to."""
        user, _ = _make_user('bd_nomanage', 'BD')
        project = _make_project('NoRelationshipEither')
        self.assertTrue(user_can_view_project(user, project))
        self.assertFalse(user_can_manage_project(user, project))


class GuardTests(TestCase):
    """Null inputs return False rather than raising."""

    def test_none_project_returns_false(self):
        user, _ = _make_user('guard_ceo', 'CEO')
        self.assertFalse(user_can_view_project(user, None))

    def test_user_without_profile_returns_false(self):
        """A superuser created via createsuperuser has no UserProfile."""
        project = _make_project()
        user = User.objects.create_superuser(
            username='root_noprofile', email='root@example.com', password='pw12345',
        )
        UserProfile.objects.filter(user=user).delete()
        user.refresh_from_db()
        self.assertFalse(user_can_view_project(user, project))
        self.assertFalse(user_can_manage_project(user, project))

    def test_none_project_returns_false_for_every_role(self):
        for label, role, flag in PORTFOLIO_ROWS:
            with self.subTest(role=label):
                user, _ = _make_user(f'guard_{label.lower().replace(" ", "_")}', role, flag)
                self.assertFalse(user_can_view_project(user, None))