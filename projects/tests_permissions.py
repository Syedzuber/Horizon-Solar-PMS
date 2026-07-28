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
  * BOQReadWriteTests          — the READ/WRITE split on the two BOQ helpers (Part 6.5b).
  * DeputyBOQTests             — the deputy's BOQ read, and the absence of write (G4).
  * BDPortfolioPolicyTests     — BD's portfolio-wide read, asserted as POLICY.
  * GuardTests                 — null project / profile-less user do not raise.

The two BOQ classes were added by Part 6.5b. Before them, `user_can_view_project_boq()`
and `user_can_edit_project_boq()` had ZERO coverage — which is precisely where the 6.5a
audit found this module's changes would land. The deputy fix modified the read helper
after three parts of it being off-limits, so the split it enforces is now pinned:
a deputy READS every BOQ and WRITES none.

No migrations are involved. Run with:
    python manage.py test projects --settings=solarpms.test_settings
"""
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase

from .models import Project, ProjectPhase, Task, UserProfile
from .permissions import (
    user_can_edit_project_boq, user_can_manage_project, user_can_view_project,
    user_can_view_project_boq,
)


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

    def test_role_string_still_grants_portfolio_view(self):
        """The role-string branch must keep working even though the role is gone.

        'Design Head' was added to UserProfile.ROLE_CHOICES by Part 1 (migration 0048)
        and DELIBERATELY REMOVED by Part 6.5b (migration 0053) after the 6.5a audit — it
        was never held by anyone, but it was assignable from five Admin surfaces and
        broke an account in five ways that named no cause. Design Head authority is, and
        stays, the `is_design_head` boolean.

        So this test now pins the opposite of what its name used to promise. The role
        string is NOT a supported value any more; the branches that accept it in
        user_can_view_project() and user_can_view_project_boq() were left in place on
        purpose — they are harmless, and a future phase may reintroduce the role
        deliberately. This asserts they are still there, so that removing one is a
        decision somebody makes rather than a line somebody deletes in passing.

        Setting the field directly is what makes this possible: `choices` is not enforced
        on save(), only by form validation. That was already true when the value WAS a
        legal choice, and it is what lets the assertion outlive the choice's removal.
        See DESIGN_HEAD_ROLE_MIGRATION_AUDIT.md.
        """
        user, profile = _make_user('dh_role', 'Design')
        profile.role = 'Design Head'
        profile.is_design_head = False
        profile.save(update_fields=['role', 'is_design_head'])
        self.assertTrue(user_can_view_project(user, self.project))

    def test_design_head_role_is_not_an_assignable_choice(self):
        """Part 6.5b removed it. If it comes back, that must be a deliberate act."""
        self.assertNotIn(
            'Design Head', [value for value, _ in UserProfile.ROLE_CHOICES],
            "'Design Head' was removed from ROLE_CHOICES by Part 6.5b (migration 0053). "
            "If this fails, read DESIGN_HEAD_ROLE_MIGRATION_AUDIT.md before 'fixing' it.",
        )

    def test_design_head_gets_view_not_manage(self):
        user, _ = _make_user('dh_nomanage', 'Design', is_design_head=True)
        self.assertTrue(user_can_view_project(user, self.project))
        self.assertFalse(user_can_manage_project(user, self.project))


class BOQReadWriteTests(TestCase):
    """The READ/WRITE split on the two BOQ helpers. Added by Part 6.5b.

    BOTH helpers had ZERO coverage before this class, which the 6.5a audit called out
    as the sharpest gap in the module: they are the functions every design-module
    session was forbidden to touch, and nothing verified what they did.

    THE SPLIT IS THE POINT, and it is not symmetric by accident. READ is wide — five
    additive sources. WRITE is W-narrow: `role == 'Design'` AND `assigned_design` on
    THIS project, and nothing else. A role that reads every BOQ in the portfolio may
    still author none of them, and each test below pins one side of that.
    """

    def setUp(self):
        self.project = _make_project('BOQProject')
        self.other = _make_project('OtherBOQProject')

    # ── READ: portfolio-wide roles ──────────────────────────────────────────────
    def test_portfolio_read_roles_read_any_boq(self):
        """SCM / Admin / CEO — portfolio-wide by remit (BOQ_PORTFOLIO_READ_ROLES)."""
        for role in ('SCM', 'Admin', 'CEO'):
            with self.subTest(role=role):
                user, _ = _make_user(f'boqread_{role.lower().replace(" ", "_")}', role)
                self.assertTrue(user_can_view_project_boq(user, self.project))
                self.assertFalse(user_can_edit_project_boq(user, self.project))

    def test_finance_and_bd_get_no_boq_read(self):
        """Deliberately NOT PORTFOLIO_VIEW_ROLES — both see projects, neither sees BOQs.

        Pinned because the two sets look interchangeable and are not: widening project
        VISIBILITY must never widen BOQ access as a side effect.
        """
        for role in ('Finance', 'BD'):
            with self.subTest(role=role):
                user, _ = _make_user(f'boqnone_{role.lower()}', role)
                self.assertTrue(user_can_view_project(user, self.project))
                self.assertFalse(user_can_view_project_boq(user, self.project))
                self.assertFalse(user_can_edit_project_boq(user, self.project))

    # ── READ: management authority ──────────────────────────────────────────────
    def test_assigned_pm_reads_but_does_not_write(self):
        """A PM's lever is boq_request_revision(), not authorship."""
        user, profile = _make_user('boq_pm', 'PM')
        self.assertFalse(user_can_view_project_boq(user, self.project))
        self.project.assigned_pm = profile
        self.project.save(update_fields=['assigned_pm'])
        self.assertTrue(user_can_view_project_boq(user, self.project))
        self.assertFalse(user_can_edit_project_boq(user, self.project))

    def test_coordinator_reads_but_does_not_write(self):
        """Coordinators route through user_can_manage_project() like the PM."""
        user, profile = _make_user('boq_coord', 'Project Coordinator')
        self.assertFalse(user_can_view_project_boq(user, self.project))
        self.project.coordinators.add(profile)
        self.assertTrue(user_can_view_project_boq(user, self.project))
        self.assertFalse(user_can_edit_project_boq(user, self.project))

    # ── The Design branch: read is broad, write is W-narrow ─────────────────────
    def test_assigned_designer_reads_and_writes(self):
        user, profile = _make_user('boq_des_fk', 'Design')
        self.project.assigned_design = profile
        self.project.save(update_fields=['assigned_design'])
        self.assertTrue(user_can_view_project_boq(user, self.project))
        self.assertTrue(user_can_edit_project_boq(user, self.project))

    def test_unassigned_designer_gets_neither(self):
        """A Design user with no relationship to this project is refused both."""
        user, _ = _make_user('boq_des_none', 'Design')
        self.assertFalse(user_can_view_project_boq(user, self.project))
        self.assertFalse(user_can_edit_project_boq(user, self.project))

    def test_task_holding_designer_reads_but_does_not_write(self):
        """THE W-NARROW RULE, pinned. Holding a task lets a designer READ a BOQ, not
        author it — the asymmetry permissions.py warns against "restoring symmetry" on.
        """
        user, profile = _make_user('boq_des_task', 'Design')
        _assign_task(self.project, profile)
        self.assertTrue(user_can_view_project_boq(user, self.project))
        self.assertFalse(user_can_edit_project_boq(user, self.project))

    def test_designer_write_is_scoped_to_the_assigned_project(self):
        """assigned_design on one project confers nothing on another."""
        user, profile = _make_user('boq_des_scope', 'Design')
        self.project.assigned_design = profile
        self.project.save(update_fields=['assigned_design'])
        self.assertTrue(user_can_edit_project_boq(user, self.project))
        self.assertFalse(user_can_edit_project_boq(user, self.other))
        self.assertFalse(user_can_view_project_boq(user, self.other))

    # ── Design Head: read, never write ──────────────────────────────────────────
    def test_design_head_flag_reads_every_boq_and_writes_none(self):
        """The flag confers oversight, not authorship — finding C9, asserted."""
        user, _ = _make_user('boq_head', 'Design', is_design_head=True)
        self.assertTrue(user_can_view_project_boq(user, self.project))
        self.assertTrue(user_can_view_project_boq(user, self.other))
        self.assertFalse(user_can_edit_project_boq(user, self.project))

    def test_design_head_role_string_also_reads(self):
        """The kept role-string branch, on the BOQ helper this time. See
        DesignHeadTests.test_role_string_still_grants_portfolio_view for why it stays."""
        user, profile = _make_user('boq_head_role', 'Design')
        profile.role = 'Design Head'
        profile.is_design_head = False
        profile.save(update_fields=['role', 'is_design_head'])
        self.assertTrue(user_can_view_project_boq(user, self.project))
        self.assertFalse(user_can_edit_project_boq(user, self.project))

    # ── Guards ──────────────────────────────────────────────────────────────────
    def test_user_without_profile_is_refused_both(self):
        """A superuser created via createsuperuser has no UserProfile."""
        user = User.objects.create_user(username='boq_noprofile', password='pw12345')
        UserProfile.objects.filter(user=user).delete()
        user.refresh_from_db()
        self.assertFalse(user_can_view_project_boq(user, self.project))
        self.assertFalse(user_can_edit_project_boq(user, self.project))

    def test_null_project_is_refused_both(self):
        user, _ = _make_user('boq_nullproj', 'SCM')
        self.assertFalse(user_can_view_project_boq(user, None))
        self.assertFalse(user_can_edit_project_boq(user, None))

    def test_blank_role_gets_neither(self):
        """role='' is legal on the model (blank=True) and must grant nothing."""
        user, _ = _make_user('boq_blank', '')
        self.assertFalse(user_can_view_project_boq(user, self.project))
        self.assertFalse(user_can_edit_project_boq(user, self.project))


class DeputyBOQTests(TestCase):
    """The deputy's BOQ read — finding G4, closed by Part 6.5b.

    THE FAILURE THIS PINS: a named deputy could open a QC screen, and from Part 6 a
    procurement group screen whose aggregated BOQ they could read, then got a 403 from
    the per-site BOQ link on that same page. A QC reviewer who cannot see the BOQ is
    reviewing half a package.

    THE DEPUTY GETS READ AND NEVER WRITE. If a future change admits a deputy to
    user_can_edit_project_boq(), test_deputy_cannot_write_any_boq fails — and that is a
    product decision to confirm, not a test to update in passing.
    """

    def setUp(self):
        self.project = _make_project('DeputyProject')
        self.other = _make_project('DeputyOtherProject')
        self.head_user, self.head = _make_user('dep_head', 'Design', is_design_head=True)
        self.dep_user, self.deputy = _make_user('dep_deputy', 'Design')
        self.head.design_head_deputy = self.deputy
        self.head.save(update_fields=['design_head_deputy'])

    def test_deputy_reads_every_boq(self):
        """Portfolio-wide, on the same terms as the Head — including projects the
        deputy has no relationship to at all, which is the G4 case exactly."""
        self.assertTrue(user_can_view_project_boq(self.dep_user, self.project))
        self.assertTrue(user_can_view_project_boq(self.dep_user, self.other))

    def test_deputy_cannot_write_any_boq(self):
        """user_can_edit_project_boq() was NOT modified. W-narrow stands."""
        self.assertFalse(user_can_edit_project_boq(self.dep_user, self.project))
        self.assertFalse(user_can_edit_project_boq(self.dep_user, self.other))

    def test_deputy_read_is_revoked_when_the_head_loses_the_flag(self):
        """user_is_design_head_deputy() re-checks is_design_head on the NAMING profile,
        so clearing a Head's flag revokes their deputy in the same instant. The FK is
        untouched — this is the rule, not a side effect."""
        self.assertTrue(user_can_view_project_boq(self.dep_user, self.project))
        self.head.is_design_head = False
        self.head.save(update_fields=['is_design_head'])
        self.assertFalse(user_can_view_project_boq(self.dep_user, self.project))
        self.head.refresh_from_db()
        self.assertEqual(self.head.design_head_deputy_id, self.deputy.pk)

    def test_deputy_read_is_revoked_when_the_fk_is_cleared(self):
        """Presence of the FK is the whole rule — clearing it ends the authority."""
        self.assertTrue(user_can_view_project_boq(self.dep_user, self.project))
        self.head.design_head_deputy = None
        self.head.save(update_fields=['design_head_deputy'])
        self.assertFalse(user_can_view_project_boq(self.dep_user, self.project))

    def test_a_plain_designer_is_not_a_deputy(self):
        """Guards against the branch admitting every Design user by accident."""
        user, _ = _make_user('dep_plain', 'Design')
        self.assertFalse(user_can_view_project_boq(user, self.project))
        self.assertFalse(user_can_edit_project_boq(user, self.project))

    def test_deputy_keeps_write_on_a_project_they_are_assigned_to(self):
        """The deputy branch WIDENS read and must not narrow write. A deputy who is also
        the site's assigned_design still authors that site's BOQ."""
        self.project.assigned_design = self.deputy
        self.project.save(update_fields=['assigned_design'])
        self.assertTrue(user_can_edit_project_boq(self.dep_user, self.project))
        self.assertFalse(user_can_edit_project_boq(self.dep_user, self.other))

    def test_head_reads_but_does_not_write(self):
        """Stated alongside the deputy so the pair cannot drift apart."""
        self.assertTrue(user_can_view_project_boq(self.head_user, self.project))
        self.assertFalse(user_can_edit_project_boq(self.head_user, self.project))


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