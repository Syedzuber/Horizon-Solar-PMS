"""
Central authorization helpers for project-scoped access.

There are TWO project-scoped questions, and they have two different answers:

    user_can_view_project(user, project)    — may this user SEE this project?
    user_can_manage_project(user, project)  — may this user ACT on this project?

`user_can_manage_project()` is the SINGLE canonical place PM-level management
authority is decided. Do NOT compare `Project.assigned_pm` (or, in a future
change, coordinators) or role strings directly anywhere else in the codebase —
call this function instead. Keeping one comparison path prevents new code from
copying whichever nearby pattern happens to be wrong for its context.

`user_can_view_project()` is the corresponding single home for VISIBILITY, and it
is the ONE function in the codebase that is allowed to branch on role strings for
a project-scoped decision. It exists because visibility genuinely is role-shaped:
several roles are portfolio-wide by remit and are not scoped to individual
projects anywhere in the product. Read the "do not compare role strings" rule
above as: do not compare them AT A CALL SITE — route the comparison through here
instead, so there is still exactly one place to change.
"""

# Roles whose remit is the whole portfolio by definition — they are never scoped to
# individual projects at any surface in the product, and their dashboards already
# query every active project with no per-user term (dashboard_finance, dashboard_scm,
# dashboard_ceo, admin_project_list). Membership here means "sees everything".
# Sales & BD is deliberately NOT in this set — see user_can_view_project().
PORTFOLIO_VIEW_ROLES = frozenset({'CEO', 'Finance', 'SCM', 'Admin'})


def user_can_manage_project(user, project):
    """
    Return True if `user` has PM-level management authority on `project`.

    Authority is the UNCONDITIONAL OR of two additive sources:
        assigned PM  OR  a Project Coordinator on this project.

    This is the one canonical comparison path — every PM-ownership check routes
    through here, so adding coordinator support here gives every call site correct
    behaviour with no further edits.

    INVARIANT (additive-only): the assigned-PM check is evaluated FIRST and never
    gated on whether coordinators exist. Assigning a coordinator can only ever add
    a manager — it can never remove the PM's authority. Do not restructure this as
    "if coordinators: check coordinators else check PM" — that would silently lock
    the PM out. The OR is unconditional and lives here, not at any call site.

    `Project.assigned_pm` and `coordinators` are both to `UserProfile`, so we
    compare against `user.profile`. `getattr` guards a user with no profile
    (e.g. a superuser created via `createsuperuser`). A null `assigned_pm`
    compares False, matching the old `is not None` guards.
    """
    profile = getattr(user, 'profile', None)
    if profile is None:
        return False
    if project.assigned_pm == profile:          # PM authority — always checked, never gated
        return True
    return project.coordinators.filter(pk=profile.pk).exists()  # additive coordinator authority


def user_can_view_project(user, project):
    """
    Return True if `user` may SEE `project`. This is visibility only — it confers no
    authority to change anything. For that, call user_can_manage_project().

    Two shapes of answer, because visibility genuinely is role-shaped:

      PORTFOLIO-WIDE (True for every project)
        CEO / Finance / SCM / Admin  — portfolio-wide by remit (PORTFOLIO_VIEW_ROLES)
        Design Head                  — portfolio-wide by remit (see the flag note below)
        Sales & BD                   — its own branch, see below

      ASSIGNMENT-BASED (True only where the user has a relationship)
        PM                  — assigned PM (or coordinator) on this project
        Project Coordinator — coordinator (or assigned PM) on this project
        Site Engineer       — holds a task on this project
        Design              — is assigned_design, or holds a task on this project

    Every assignment-based branch is a strict SUPERSET of user_can_manage_project():
    anyone who can manage a project can necessarily see it. That is why each branch
    starts from `can_manage` rather than re-deriving ownership — the assigned-PM and
    coordinator comparison stays in exactly one place, and a user who ends up as
    `assigned_pm` despite not holding the PM role (reachable via the Zoho webhook,
    which matches on email with no role filter) can still see their own project.

    The Site Engineer and Design branches mirror those roles' dashboard querysets
    exactly — `phases__tasks__assigned_to` for SE, and `assigned_design` OR the same
    task relation for Design — so this function agrees with what each dashboard
    already shows them, rather than inventing a second, narrower scoping rule.

    Roles with no branch of their own (System Admin, and a blank role) fall through
    to management authority alone: visible only where the user is assigned PM or
    coordinator. This is the conservative default — a role is portfolio-wide only by
    being listed explicitly, never by omission.

    Returns False rather than raising for a null `project` or a user with no
    UserProfile (e.g. a superuser created via `createsuperuser`), matching
    user_can_manage_project()'s guard style.
    """
    if project is None:
        return False
    profile = getattr(user, 'profile', None)
    if profile is None:
        return False

    role = profile.role

    # Management authority always implies visibility. Computed once here and reused by
    # every assignment-based branch below, so ownership is never re-derived locally.
    can_manage = user_can_manage_project(user, project)
    if can_manage:
        return True

    # Design Head is currently the boolean UserProfile.is_design_head, not a role
    # string; Phase 2 promotes it to a role. Accept BOTH forms so that promotion needs
    # no second edit here. Checked independently of `role` because the flag is
    # deliberately role-independent (see UserProfile.is_design_head).
    if getattr(profile, 'is_design_head', False) or role == 'Design Head':
        return True

    if role in PORTFOLIO_VIEW_ROLES:
        return True

    # Sales & BD — portfolio-wide read. This is SETTLED CURRENT POLICY, decided by the
    # product owner: BD's existing workflow (dashboard_bd renders a flat portfolio
    # queryset with no per-user term) works and is not being changed.
    #
    # Kept as its own branch rather than folded into PORTFOLIO_VIEW_ROLES on purpose.
    # CEO/Finance/SCM/Admin are portfolio-wide because their roles are portfolio-wide
    # by definition. BD is portfolio-wide because that is how the system works today
    # and it is functioning well. Same outcome, different reasons — so if BD's scope is
    # ever revisited, the branch to change is already isolated and this comment says
    # why it is separate.
    if role == 'BD':
        return True

    if role == 'Site Engineer':
        # Mirrors dashboard_site_engineer's scoping: any task on this project assigned
        # to this user. Reverse relations only — keeps this module import-free.
        return project.phases.filter(tasks__assigned_to=profile).exists()

    if role == 'Design':
        # Mirrors dashboard_design's union: the assigned_design FK, OR a task on this
        # project assigned to this user (a designer given work by the Design lead).
        if project.assigned_design_id == profile.pk:
            return True
        return project.phases.filter(tasks__assigned_to=profile).exists()

    # System Admin, blank role, or any future unlisted role: management authority only.
    return can_manage


# Roles with portfolio-wide BOQ READ. Deliberately NOT PORTFOLIO_VIEW_ROLES: that set
# contains Finance, which has no BOQ surface anywhere in the product, and BD is separately
# portfolio-wide for visibility but likewise has no BOQ access. BOQ read is its own set so
# that widening project VISIBILITY never silently widens BOQ access as a side effect.
# Design Head is handled as its own branch below, not listed here, because it is currently
# a boolean flag rather than a role string (see user_can_view_project_boq).
BOQ_PORTFOLIO_READ_ROLES = frozenset({'SCM', 'Admin', 'CEO'})


def _user_holds_task_on_project(profile, project):
    """
    Return True if `profile` is assigned any task on `project`.

    Used by the BOQ READ gate only. The BOQ WRITE gate is W-narrow and deliberately does
    NOT call this — holding a task lets a designer read a BOQ, not author it. Do not
    "restore symmetry" by adding it back to user_can_edit_project_boq(); see the WRITE
    RULE note there. The relation traversal mirrors the one already used by
    user_can_view_project()'s Design branch — reverse relations only, keeping this
    module import-free.
    """
    return project.phases.filter(tasks__assigned_to=profile).exists()


def user_can_view_project_boq(user, project):
    """
    Return True if `user` may READ `project`'s BOQ. Read only — confers no authority to
    change it. For that, call user_can_edit_project_boq().

    Access is the OR of four additive sources:

        PM / coordinator     — user_can_manage_project(), the one canonical path
        SCM / Admin / CEO    — portfolio-wide by remit (BOQ_PORTFOLIO_READ_ROLES)
        Design Head          — portfolio-wide read, no write (see below)
        Design               — assigned_design on this project, OR holds a task on it

    The Design branch is the SAME union as user_can_view_project()'s Design branch
    (permissions.py:143-148): a designer sees the BOQ of a project they own via
    `assigned_design`, or one they were given task work on by the Design lead. It is
    expressed here through _user_holds_task_on_project() rather than duplicated inline, so
    the two helpers in this pair cannot drift apart from each other.

    Design Head accepts BOTH `is_design_head` and the role string 'Design Head', matching
    the pattern at permissions.py:119-120, so the Phase 2 promotion of the flag to a role
    needs no second edit here. Design Head gets portfolio-wide READ and no write — it is
    deliberately absent from user_can_edit_project_boq().

    Returns False rather than raising for a null `project` or a user with no UserProfile,
    matching user_can_manage_project()'s guard style.
    """
    if project is None:
        return False
    profile = getattr(user, 'profile', None)
    if profile is None:
        return False

    # Management authority always implies BOQ read. Routed through the canonical path —
    # assigned_pm / coordinators are never re-compared here.
    if user_can_manage_project(user, project):
        return True

    if profile.role in BOQ_PORTFOLIO_READ_ROLES:
        return True

    if getattr(profile, 'is_design_head', False) or profile.role == 'Design Head':
        return True

    if profile.role == 'Design':
        if project.assigned_design_id == profile.pk:
            return True
        return _user_holds_task_on_project(profile, project)

    return False


def user_can_edit_project_boq(user, project):
    """
    Return True if `user` may WRITE `project`'s BOQ — quantities, make preference, ad-hoc
    rows, submission, and the auto-create-and-seed that boq_detail performs on GET.

    BOQ authorship belongs to the Design role and to nobody else:

        Design AND (assigned_design on this project OR holds a task on it)

    Everyone else is excluded ON PURPOSE, and each for its own reason:

      * PM / coordinator — they do not author BOQs. Their lever is boq_request_revision(),
        which is gated separately on user_can_manage_project().
      * SCM — SCM writes `ordered_quantity` and acknowledges, but that path stays role-gated
        (SCM is portfolio-wide by remit) and does not route through here. Adding a project
        relationship requirement to SCM would break acknowledgement system-wide.
      * Design Head — portfolio-wide READ only. The flag confers no approval or authorship
        authority anywhere in the product today; granting write here would invent some.
      * Admin / CEO — portfolio-wide read, but authoring a BOQ is a design act, not an
        administrative one. They already reach every BOQ read surface.

    WRITE RULE: this is W-narrow — `assigned_design` on THIS project, and nothing else.
    Selected by the Part 0.6 precondition, which measures what share of active projects
    have a null `assigned_design`: above 20% the FK alone would be too thin a relationship
    to gate writes on, because designers would legitimately be working projects never
    stamped with an assigned_design. Measured on live Railway data: 25 active projects,
    3 with a null assigned_design = 12%, below the threshold — so the FK is well-enough
    populated to carry the write gate on its own.

    This is NARROWER than user_can_view_project_boq()'s Design branch, deliberately: a
    designer holding a task on a project can READ its BOQ but not author it. Only the
    stamped assigned_design writes.

    To widen back to W-broad if assigned_design coverage degrades past 20%, restore the
    `_user_holds_task_on_project(profile, project)` fallback as the last line — nothing
    else in this module or in views.py needs to change either way.
    """
    if project is None:
        return False
    profile = getattr(user, 'profile', None)
    if profile is None:
        return False

    if profile.role != 'Design':
        return False

    return project.assigned_design_id == profile.pk   # W-narrow — no task-holding fallback


def user_can_manage_program(user, program):
    """Return True if `user` has PM-level management authority over `program`.

    A Program has no assigned_pm / coordinators of its own — its authority is DERIVED
    from its child sites: a user manages the Program if they manage ANY non-deleted
    child site, decided through the one canonical `user_can_manage_project()` path
    (never by re-comparing assigned_pm / coordinators / role strings here). This keeps
    Program access on the exact same comparison rule as project access.

    NOTE (deliberate): this returns False for a Program with no sites the user manages
    (e.g. a brand-new empty Program). Admin / CEO reach Program views through the
    view-layer role gate, not this helper — same split as user_can_manage_project,
    which only decides PM-level authority. The `getattr` guard mirrors the project
    helper (a superuser without a profile is not a manager)."""
    profile = getattr(user, 'profile', None)
    if profile is None:
        return False
    for site in program.sites.filter(is_deleted=False):
        if user_can_manage_project(user, site):
            return True
    return False


# ---------------------------------------------------------------------------
# OPEX design module (Part 2)
#
# Same rule as the rest of this file: no view compares a role string or an ownership
# field inline. Every design-workflow authority question is answered here.
#
# DESIGN HEAD AUTHORITY IS THE `is_design_head` BOOLEAN, NOT THE ROLE STRING.
# Part 1 added 'Design Head' to ROLE_CHOICES, but no user holds it and 56 existing
# @role_required decorators still match 'Design' literally — switching now would lock
# the real Design Head out of every screen he already uses. Part 4 migrates this
# properly. Until then the flag is the single source of truth, and the role string is
# deliberately NOT consulted anywhere in this section.
#
# `design_head_deputy` exists on UserProfile from Part 1 and is deliberately NOT read
# here — acting-for-the-Head is Part 4.
# ---------------------------------------------------------------------------

def user_is_design_head(user):
    """Return True if `user` holds Design Head authority.

    Reads UserProfile.is_design_head only. Returns False rather than raising for a user
    with no profile, matching the guard style of the helpers above."""
    profile = getattr(user, 'profile', None)
    if profile is None:
        return False
    return bool(profile.is_design_head)


def user_is_assigned_designer(user, assignment):
    """Return True if `user` is the designer this assignment is allocated to.

    Deliberately strict: it is an identity check against `assignment.assigned_to` and
    nothing else. The Design Head does NOT satisfy it — proposing a due date is the
    designer's act, and the Head approving his own proposal would collapse the
    two-sided handshake into one side."""
    if assignment is None:
        return False
    profile = getattr(user, 'profile', None)
    if profile is None:
        return False
    return assignment.assigned_to_id == profile.pk


def user_can_view_design(user, project):
    """Return True if `user` may SEE the design workflow (including a signed survey
    link) for `project`.

    Routed straight through the existing user_can_view_project() so design visibility
    can never drift from project visibility — deliberately NOT re-derived here."""
    return user_can_view_project(user, project)


def project_managers(project):
    """
    Return the list of UserProfiles with PM-level authority on `project`:
    the assigned PM plus every active Project Coordinator, deduplicated, PM first.

    Use this everywhere a notification currently targets `project.assigned_pm`, so
    coordinators receive the same operational notifications as the PM. Returns an
    empty list if there is no PM and no coordinators.
    """
    managers = []
    seen = set()
    pm = project.assigned_pm
    if pm is not None:
        managers.append(pm)
        seen.add(pm.pk)
    for coord in project.coordinators.filter(is_active=True):
        if coord.pk not in seen:
            seen.add(coord.pk)
            managers.append(coord)
    return managers
