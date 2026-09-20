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

# The ONLY import in this module, and deliberately so. `Q` is the ORM query API, not a
# model — importing it keeps the "no model imports" property that has kept this module
# free of circular imports across every phase (every relationship below is still reached
# by reverse relation). It is needed by manageable_projects_q(), which gives list views
# the same canonical ownership rule the per-object helpers already have.
from django.db.models import Q

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


def manageable_projects_q(profile, prefix=''):
    """Return the Q() selecting the projects `profile` manages — the QUERYSET form of
    user_can_manage_project().

    `prefix` is the ORM path from the model being filtered to Project, including its
    trailing '__'. Filtering Project itself takes the default ''; filtering Task takes
    'phase__project__'.

    WHY THIS EXISTS. user_can_manage_project() answers the question for ONE project in
    hand, and a list view cannot use it without loading the whole portfolio and filtering
    in Python. So the rule was being hand-written as `Q(assigned_pm=...) |
    Q(coordinators=...)` at each list surface instead — exactly the duplication the
    module docstring's "do not compare assigned_pm anywhere else" rule exists to prevent.
    This gives the list surfaces the canonical path the object surfaces already have.

    INVARIANT: this must select precisely the projects user_can_manage_project() returns
    True for — assigned PM OR coordinator, unconditionally OR'd, additive-only. If one of
    the pair changes, change the other in the same edit. They are two encodings of one
    rule, and the same additive-only warning applies here: do not restructure this as
    "coordinators if any, else PM".

    CALLERS MUST .distinct(). The coordinators leg traverses an M2M, so the join can
    duplicate rows.
    """
    return (Q(**{f'{prefix}assigned_pm': profile})
            | Q(**{f'{prefix}coordinators': profile}))


def user_can_view_project(user, project):
    """
    Return True if `user` may SEE `project`. This is visibility only — it confers no
    authority to change anything. For that, call user_can_manage_project().

    Two shapes of answer, because visibility genuinely is role-shaped:

      PORTFOLIO-WIDE (True for every project)
        CEO / Finance / SCM / Admin  — portfolio-wide by remit (PORTFOLIO_VIEW_ROLES)
        System Admin                 — its own branch, see below
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

    Roles with no branch of their own (today: a blank role) fall through to management
    authority alone: visible only where the user is assigned PM or coordinator. This is
    the conservative default — a role is portfolio-wide only by being listed explicitly,
    never by omission.

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

    # System Admin — unrestricted, exactly as Admin is. `docs/execution-model.md` §2 D-4:
    # "Admin and System Admin are unrestricted."
    #
    # THIS BRANCH IS LOAD-BEARING AND MUST STAY ABOVE THE FALL-THROUGH. Before the 0.2
    # lockdown this function had no System Admin branch and returned False for them on
    # every project they did not personally manage — they reached projects only because
    # the endpoints' PM-only guard (`role == 'PM' and not user_can_manage_project(...)`)
    # never named them. The lockdown removes that guard, so without this line System
    # Admin loses the entire product in the same edit.
    #
    # Kept as its own branch rather than added to PORTFOLIO_VIEW_ROLES on purpose, for the
    # same reason the BD branch below is separate. That set is documented as roles whose
    # DASHBOARDS already query every active project with no per-user term (dashboard_ceo,
    # dashboard_finance, dashboard_scm, admin_project_list). System Admin is not one of
    # those — it is unrestricted because D-4 says the administrative roles are, which is a
    # different reason for the same answer. Same outcome, isolated branch, and the set's
    # own comment stays true.
    #
    # DELIBERATELY DOES NOT WIDEN BOQ. user_can_view_project_boq() does not call this
    # function; it branches on its own BOQ_PORTFOLIO_READ_ROLES frozenset, which does not
    # list System Admin. That separation is exactly what the frozenset exists for (see its
    # comment), so this grants project visibility and no BOQ read.
    if role == 'System Admin':
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

    Access is the OR of five additive sources:

        PM / coordinator     — user_can_manage_project(), the one canonical path
        SCM / Admin / CEO    — portfolio-wide by remit (BOQ_PORTFOLIO_READ_ROLES)
        Design Head          — portfolio-wide read, no write (see below)
        Design Head's deputy — portfolio-wide read, no write (Part 6.5b, closes G4)
        Design QC            — portfolio-wide read, no write (Part 9, closes G4 again)
        Design               — assigned_design on this project, OR holds a task on it

    The Design branch is the SAME union as user_can_view_project()'s Design branch
    (permissions.py:143-148): a designer sees the BOQ of a project they own via
    `assigned_design`, or one they were given task work on by the Design lead. It is
    expressed here through _user_holds_task_on_project() rather than duplicated inline, so
    the two helpers in this pair cannot drift apart from each other.

    Design Head accepts BOTH `is_design_head` and the role string 'Design Head'. The
    string branch is kept even though Part 6.5b removed 'Design Head' from ROLE_CHOICES
    (see the note there) — it is harmless, and a future phase may reintroduce the role
    deliberately. Design Head gets portfolio-wide READ and no write — it is deliberately
    absent from user_can_edit_project_boq().

    THE DEPUTY BRANCH CLOSES FINDING G4, and it is the reason this function was modified
    at all after three parts of being off-limits. A named deputy could open a QC screen
    (user_can_view_design admits them) and, from Part 6, a procurement group screen whose
    aggregated BOQ they could read — then got a 403 from the per-site BOQ link on that
    very page. A QC reviewer who cannot see the BOQ is reviewing half a package, and an
    aggregate you may read over sites you may not is worse than either.

    It had to go HERE and could not be an AND at the caller, the way the Part 6 group
    lock was: the lock only ever NARROWS, and a caller can narrow a True. Admitting a
    deputy WIDENS, and no caller can widen a gate that has already returned False.

    Deputy resolution is user_is_design_head_deputy() — the Part 4 helper, not a second
    copy of the `deputy_for` query. Its rule (the naming profile must still be a Head)
    therefore applies here too: clear a Head's flag and their deputy loses BOQ read in
    the same instant they lose everything else.

    PART 9 ADDS DESIGN QC, AND IT IS G4 ALL OVER AGAIN. The QC gate reviews the BOQ — two
    of the sixteen error categories are `boq_quantity` and `boq_specification`, so a
    reviewer who cannot open the BOQ cannot record the failure the system asks them for.
    Reported from live use: Design QC reached the QC screen, and the "View BOQ" button on
    that very screen returned 403.

    It has to go HERE for the same reason the deputy branch did: this WIDENS, and no
    caller can widen a gate that has already returned False. A Design QC reviewer is a
    plain `role='Design'` user who is by construction NOT the site's `assigned_design` —
    the assigned designer is the one person forbidden from reviewing it — so the Design
    branch below refuses them every time.

    Kept as its own branch rather than folded into the deputy line above, so "is the
    Head", "acts for the Head" and "is the other gate" stay tellable apart here exactly as
    they do in the Part 9 helpers.

    READ ONLY, DELIBERATELY. user_can_edit_project_boq() is NOT modified and admits
    neither a deputy nor Design QC. A reviewer reads a BOQ; they do not author one.
    W-narrow stands.

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

    # Part 6.5b — the Head's named deputy, on the same terms as the Head: read, never
    # write. Kept as its own branch rather than folded into the line above so that "is
    # the Head" and "may act for the Head" stay tellable apart here, exactly as
    # user_is_design_head() and user_has_design_head_authority() keep them apart.
    if user_is_design_head_deputy(user):
        return True

    # Part 9 — the FIRST review gate, on the same terms again: read, never write. Two of
    # the error categories a QC reviewer must choose between are about the BOQ, so this
    # is the difference between reviewing a package and guessing at one.
    if user_is_design_qc(user):
        return True

    # Session B.1 — the reviewer NAMED on this site, flag or no flag. Same reason as the
    # branch above and scoped one site narrower: two of the sixteen error categories are
    # about the BOQ, so an assigned reviewer who cannot open it cannot record the failure
    # the system asks them for. The Design branch below refuses them by construction — a
    # reviewer is never the site's own `assigned_design` — so without this the person the
    # Head just handed the site to is the one person who cannot read its BOQ.
    if user_is_assigned_qc_reviewer(user, project):
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
# OPEX design module (Parts 2-4)
#
# Same rule as the rest of this file: no view compares a role string or an ownership
# field inline. Every design-workflow authority question is answered here.
#
# DESIGN HEAD AUTHORITY IS THE `is_design_head` BOOLEAN, AND THAT IS NOW PERMANENT.
#
# This comment previously said "56 existing @role_required decorators still match
# 'Design' literally". THAT NUMBER WAS WRONG. Counted mechanically over every
# non-migration module in `projects/`: 61 @role_required uses exist, and exactly ONE
# lists 'Design' — dashboard_design at views.py:893. 56 was the total decorator count
# when the comment was written, not the exposure.
#
# The correction did not change the conclusion; it changed the reason. Part 6.5a audited
# the whole surface and Part 6.5b acted on it: 'Design Head' was REMOVED from
# UserProfile.ROLE_CHOICES (migration 0053) rather than migrated to. Every one of the
# eighteen views below gates on user_is_design_head(), which reads the boolean and has
# no role-string branch, so the role would have added a SECOND authority mechanism
# beside a working one rather than replacing it — while costing its holder BOQ write,
# every Task.assigned_role match, and the ability to be edited by a System Admin.
#
# Full reasoning, measurements and the open decision points: see
# DESIGN_HEAD_ROLE_MIGRATION_AUDIT.md in the repo root.
#
# The role string is still accepted by user_can_view_project() and
# user_can_view_project_boq() above — deliberately left in place, harmless, and ready
# if a future phase reintroduces the role on purpose. It is NOT consulted anywhere in
# this section, and the flag is the single source of truth here.
#
# PART 4 ADDS THE DEPUTY. `design_head_deputy` on the Head's UserProfile names one
# person who may act for him. Every Part 2 and Part 3 view that asked
# `user_is_design_head()` now asks `user_has_design_head_authority()` instead, so the
# deputy is admitted everywhere the Head is admitted, in one edit rather than thirteen.
# ---------------------------------------------------------------------------

def user_is_design_head(user):
    """Return True if `user` IS the Design Head — the flag itself, deputy excluded.

    Deliberately kept narrow and separate from user_has_design_head_authority(). This
    is the predicate for "is this person the Head", which is a different question from
    "may this person act with the Head's authority"; conflating them would make the
    deputy indistinguishable from the Head in any future audit, log line or screen that
    needs to tell them apart.

    Views should call user_has_design_head_authority(), not this. Reads
    UserProfile.is_design_head only, and returns False rather than raising for a user
    with no profile, matching the guard style of the helpers above."""
    profile = getattr(user, 'profile', None)
    if profile is None:
        return False
    return bool(profile.is_design_head)


def user_is_design_head_deputy(user):
    """Return True if `user` is the named deputy of somebody who is a Design Head.

    THE PRESENCE OF THE FK IS THE WHOLE RULE (settled decision 6). There is no absence
    schedule, no date range and no on/off switch — if the Head has named you, you may
    act, and when he clears the field you may not. Anything richer is a scheduling
    feature nobody asked for.

    `is_design_head=True` is re-checked on the NAMING profile, not assumed: a deputy is
    only a deputy of an actual Head, so clearing someone's Head flag silently revokes
    the authority of anyone they had deputised. Reverse relation `deputy_for` comes from
    the self-FK's related_name (models.py — UserProfile.design_head_deputy).
    """
    profile = getattr(user, 'profile', None)
    if profile is None:
        return False
    return profile.deputy_for.filter(is_design_head=True).exists()


def user_has_design_head_authority(user):
    """Return True if `user` may act with Design Head authority — the Head OR his
    named deputy.

    THIS IS THE FUNCTION VIEWS SHOULD CALL. It is the single gate for survey upload,
    allocation, due-date approval and rejection, Arka approve and reject, and QC start
    and verdict — everywhere Parts 2 and 3 used `is_design_head` directly.

    It deliberately does NOT confer portfolio-wide project or BOQ visibility. The Head
    gets that from his own branches in user_can_view_project() and
    user_can_view_project_boq(), which are out of scope for this part and are not
    touched; a deputy sees design surfaces through user_can_view_design() below and
    otherwise keeps whatever visibility their own role gives them. Widening a deputy's
    read access across the whole portfolio would be a much larger decision than
    "somebody is covering QC this week".

    NOT SUFFICIENT FOR QC ON ITS OWN — see user_can_qc_design(), which additionally
    refuses the assigned designer.
    """
    return user_is_design_head(user) or user_is_design_head_deputy(user)


def user_can_qc_design(user, assignment):
    """Return True if `user` may start or decide QC on `assignment`.

    Two conditions, and BOTH are required:

        Design Head authority (Head or named deputy)
        AND NOT the designer this site is allocated to

    THE SECOND CONDITION IS THE POINT. Nobody QCs their own package — not a designer,
    not a deputy who happens to be the allocated designer, and not the Head himself if
    he has taken a site on personally (settled decision 1). QC is a second pair of eyes
    or it is nothing, and a self-review that passes is indistinguishable in the data
    from one that was never done.

    Expressed here rather than inline in the QC views so the two of them (start and
    verdict) cannot drift apart, and so the rule is greppable. Note this is a NARROWING
    of user_has_design_head_authority(), never a widening — it can only ever refuse
    somebody that function would have admitted.
    """
    if assignment is None:
        return False
    if not user_has_design_head_authority(user):
        return False
    return not user_is_assigned_designer(user, assignment)


# ---------------------------------------------------------------------------
# OPEX design module — the SECOND gate (Part 9)
#
# TWO GATES, TWO PREDICATES, ONE SHARED EXCLUSION.
#
#   user_can_qc_gate_design()    gate 1 — Design QC.   not the designer, AND THEN
#                                                        unassigned site -> is_design_qc
#                                                        assigned site   -> be the assignee
#   user_can_head_gate_design()  gate 2 — Design Head. Head authority AND not the designer
#
# Both carry the SAME self-review exclusion, because settled decision 3 is absolute: the
# assigned designer records no verdict on their own site at either gate, whatever flags
# they hold. Gate 2 is a narrowing of a flag by that exclusion and nothing else.
#
# GATE 1 IS NO LONGER A NARROWING OF A FLAG (Session B.1). It has two ways in, and the flag
# governs only the unassigned one. A named reviewer needs no flag; the flag is what lets
# somebody take unclaimed work instead. Both routes still pass through the exclusion.
#
# WHAT THESE DELIBERATELY DO NOT ANSWER: whether this particular actor has already
# recorded the OTHER gate's verdict on THIS artifact (settled decision 2). That is a
# question about a row, not about a user, and it needs the ArkaSubmission or DesignAttempt
# in hand — so it lives in design_views._other_gate_actor_conflict(), applied by all four
# verdict endpoints. Splitting it that way keeps this module free of model imports, which
# is the property that has kept it stable across nine parts.
#
# THE DEPUTY IS GATE 2 ONLY (settled decision 9). A Design Head's deputy acts for the
# Head; there is no deputy for Design QC, so user_can_qc_gate_design() never consults
# deputy status — a deputy reaches gate 1 only by holding `is_design_qc` themselves, and
# then reaches it as a QC holder rather than as a deputy.
#
# SESSION B GAVE GATE 1 A PER-SITE TERM. user_can_qc_gate_design() now also reads
# DesignAssignment.qc_assigned_to — the one place either predicate looks at a field on the
# row rather than at the user. Null there means open pool, which is the Part 9 behaviour
# unchanged, so this widened nothing and can only ever refuse somebody Part 9 admitted.
# Gate 2 is untouched by it.
# ---------------------------------------------------------------------------

def user_is_design_qc(user):
    """Return True if `user` holds the Design QC flag — the flag itself, nothing more.

    The gate-1 counterpart of user_is_design_head(), and kept just as narrow. "Holds the
    flag" and "may record a QC verdict on this site" are different questions; the second
    one is user_can_qc_gate_design() and additionally refuses the assigned designer.

    Reads UserProfile.is_design_qc only, and returns False rather than raising for a user
    with no profile, matching every other helper in this module.
    """
    profile = getattr(user, 'profile', None)
    if profile is None:
        return False
    return bool(profile.is_design_qc)


def user_can_qc_gate_design(user, assignment):
    """Return True if `user` may record the DESIGN QC (first-gate) verdict on `assignment`.

        NOT the designer this site is allocated to, AND THEN:
            qc_assigned_to is null  ->  the is_design_qc flag (the open pool)
            qc_assigned_to is set   ->  BE that person (flag or no flag)

    TWO WAYS IN, AND THE FLAG GOVERNS ONLY ONE OF THEM (Session B.1). Assignment does not
    narrow the pool, it OVERRIDES it: a site the Head has named somebody to is that
    person's to review, and `is_design_qc` has nothing to say about it. A site nobody has
    named is the pool's, and there the flag is the whole rule — exactly as in Part 9.

    So `is_design_qc` is not deprecated by assignment and must not be removed. It answers
    "who may pick work up unbidden", which is a different question from "who was handed
    this site", and only the first has anything to do with a flag on a person.

    A designer WITHOUT the flag therefore reviews only what they were explicitly given.
    They are not in the pool, cannot see it, and gain nothing portfolio-wide — see
    user_can_view_qc_queue() and user_is_assigned_qc_reviewer() for the read side.

    The Design Head does NOT satisfy the open-pool branch by virtue of being the Head — the
    two flags are independent, and a Head who has not been given `is_design_qc` reviews at
    gate 2 only. That is the whole point of a second gate: two people, not one person twice.
    He can, however, be ASSIGNED here like anyone else, which is worth knowing: doing so
    bars him from gate 2 on that site by settled decision 2, and with one Head and no named
    deputy that leaves the site with nobody to clear gate 2.

    ORDER MATTERS AND IS NOT COSMETIC. The designer exclusion is tested BEFORE either
    branch, so naming a site's own designer as its QC reviewer cannot let them through
    here: assignment can only ever take authority away from the pool, never hand it to
    somebody the self-review rule refuses. Assignment-time validation refuses that pairing
    as well (design_views._resolve_qc_reviewer), but this ordering is what makes the
    refusal true even for a row written some other way.

    Settled decision 2 is NOT restated here. Whether this actor already recorded the OTHER
    gate's verdict on this artifact is a question about a row, not about a user, and stays
    in design_views._other_gate_actor_conflict() — applied by every verdict endpoint on top
    of this. Neither assignment nor the flag exempts anyone from it.
    """
    if assignment is None:
        return False
    if user_is_assigned_designer(user, assignment):
        return False
    profile = getattr(user, 'profile', None)
    if profile is None:
        return False
    if assignment.qc_assigned_to_id is None:
        return user_is_design_qc(user)
    return assignment.qc_assigned_to_id == profile.pk


def user_can_head_gate_design(user, assignment):
    """Return True if `user` may record the DESIGN HEAD (second-gate) verdict.

    This is the Part 4 rule under its Part 9 name, and it delegates to
    user_can_qc_design() rather than restating it — the rule has not changed, only what we
    call the gate it guards. Kept as a named function anyway so that gate 1 and gate 2 read
    symmetrically at every call site, instead of one of them reaching for a helper whose
    name still says "qc" and means "head".
    """
    return user_can_qc_design(user, assignment)


def user_can_correct_boq(user, project):
    """Return True if `user` may CORRECT `project`'s BOQ as a reviewer — change a quantity
    on a row that is already there, or add a catalogue line the designer missed.

    THIS IS A THIRD BOQ WRITE AUTHORITY AND IT IS DELIBERATELY NOT A WIDENING OF THE
    SECOND. `user_can_edit_project_boq()` answers "is this person the author of this BOQ",
    is W-narrow, and is NOT MODIFIED — a reviewer is still not an author and still cannot
    submit, seed, upload or delete. This answers a different question: "may this person
    correct what the author handed them", which is a reviewer's act and nobody else's.

        Correction authority == VERDICT authority at either design gate.

    If you may fail this attempt for `boq_quantity`, you may fix the quantity. Expressed by
    delegating to the two gate predicates rather than re-deriving "is this the QC reviewer"
    from `qc_assigned_to` here — that rule has two branches (a named reviewer, or the open
    `is_design_qc` pool on an unnamed site) and a second copy of it would drift. A site
    with `qc_assigned_to` null is the deliberate default, so a re-derived narrow rule would
    leave correction dead on most sites.

    THE ASSIGNED DESIGNER IS FALSE, STRUCTURALLY RATHER THAN BY A LINE HERE. Both gate
    predicates already refuse `assignment.assigned_to` (settled decision 3), so the
    exclusion this needs is the exclusion they already carry. Restating it would be a
    fourth copy of the self-review rule.

    OPEX ONLY, STRUCTURALLY. A DesignAssignment only ever exists on an OPEX site, so this
    returns False on every Residential project at the first guard — the same property that
    makes `project_boq_is_design_locked()` safe to AND into the shared write gate. A
    Residential BOQ has no reviewer to correct it and nothing here changes that.

    INDEPENDENT OF project_boq_is_design_locked() BY DESIGN, and that is the whole point of
    it being a separate predicate. The correction window IS the locked window: the designer
    marked the BOQ complete, the stamp went on, and their own path closed. A reviewer
    reading it then finds the wrong quantity and has nowhere to put the fix. So this
    function does not consult that lock, and the LOCK IS NOT LOOSENED EITHER — it is still
    ANDed against `user_can_edit_project_boq()` at every author path exactly as before
    (boq_detail, opex_boq_entry, opex_boq_upload are unchanged). The designer stays locked
    out of their own BOQ once they submit it; only this separate path is exempt.

    NOT INDEPENDENT OF THE PROCUREMENT LOCK, which is a different thing and is ANDed at the
    caller the way Part 6 requires. `project_boq_is_group_locked()` is final — the
    quantities have been committed to a purchase and the answer is a variance against the
    order, not an edit. Correction must not become a way around it, and the correction view
    refuses on it before it looks at anything else.

    CONFERS NO DELETE. Removing a line is not in this authority and is not built anywhere
    for a reviewer: `delete_item` and the picker's reconciliation both take
    `user_can_edit_project_boq()`, which this does not touch, and both refuse a reviewer
    today for the same reason they always did.
    """
    if project is None:
        return False
    assignment = getattr(project, 'design_assignment', None)
    if assignment is None:
        return False
    return (user_can_qc_gate_design(user, assignment)
            or user_can_head_gate_design(user, assignment))


def user_can_view_design_qc_dashboard(user):
    """Return True if `user` may open the Design QC dashboard (Part 9 §6).

    Design QC OR Design Head authority. The Head is admitted because the QC dashboard is a
    strict SUBSET of his own — refusing him a narrower view of data he already sees in
    full would be an access rule with nothing behind it.

    Read only. It confers no authority to record any verdict; both gates are decided
    per site by the two predicates above.

    DELIBERATELY NOT WIDENED BY SESSION B.1. This guards the per-TENDER QC dashboard as
    well as the queue, and that screen carries stage counts and workload for every site in
    a tender — data an assigned reviewer has no claim on just because one site was handed
    to them. The queue has its own, narrower gate: user_can_view_qc_queue().
    """
    return user_is_design_qc(user) or user_has_design_head_authority(user)


def user_is_assigned_qc_reviewer(user, project):
    """Return True if `user` is the gate-1 reviewer NAMED on `project`'s design assignment.

    THE PER-SITE READ KEY FOR SESSION B.1, and the reason that session is more than a
    widened dropdown. Assignment made a designer without `is_design_qc` able to RECORD a
    gate-1 verdict; without this they could not OPEN the site to form one — the package
    listing rendered and every artifact link behind it returned 403. That is not a review,
    and it is the exact failure Part 9 hit with Design QC and fixed the same way.

    SCOPED TO ONE SITE, ALWAYS. It answers "were you handed THIS site", never "are you a
    reviewer", so it can only ever admit somebody to the row they were named on. Every
    caller adds it as one more OR beside the existing branches; none replaces a flag test
    with it.

    Reaches the assignment by reverse relation rather than importing DesignAssignment,
    keeping this module model-import-free exactly as user_is_design_head_deputy() does.
    """
    if project is None:
        return False
    assignment = getattr(project, 'design_assignment', None)
    if assignment is None:
        return False
    profile = getattr(user, 'profile', None)
    if profile is None:
        return False
    return assignment.qc_assigned_to_id == profile.pk


def user_can_view_qc_queue(user):
    """Return True if `user` may open the gate-1 review QUEUE (Session B.1).

    Wider than user_can_view_design_qc_dashboard() by exactly one case: a designer with no
    QC flag who has been assigned at least one site. Without this they could be handed work
    and have no screen on which to find it — and since nothing in this module notifies
    anyone, a screen they cannot open is work they will never do.

    ADMITTING THEM TO THE SCREEN IS NOT SHOWING THEM THE POOL. design_qc_queue() filters
    its rows through design_views._qc_scope(), which gives a non-flag reviewer their own
    assignments and nothing else. This decides whether the door opens; that decides what is
    behind it, and the two must be read together.

    `qc_design_assignments` is the reverse of DesignAssignment.qc_assigned_to, so this
    needs no model import — same pattern as user_is_design_head_deputy().
    """
    if user_can_view_design_qc_dashboard(user):
        return True
    profile = getattr(user, 'profile', None)
    if profile is None:
        return False
    return profile.qc_design_assignments.exists()


# D-b (Zuber, 16 Sep 2026, session 3.1c-i): SCM may raise a design change request as well
# as the PM and the site's coordinators. This answers WHO; WHEN is a separate question —
# design_change_window_open() below, and design_views.change_request_window_open(),
# which gives the two audiences different windows.
def user_can_request_design_change(user, project):
    """Return True if `user` may raise a design change request against `project` at all.

    The OR of two sources, and nothing else:

        user_can_manage_project()             the assigned PM or a Project Coordinator on
                                              the site (settled decision 5) — the one
                                              canonical PM-authority path, not re-derived
        user_may_raise_design_change_as_scm() any SCM user (D-b)

    AUTHORITY, NOT THE WINDOW. Passing this does not mean a request may be raised now: the
    PM and SCM windows differ (Q1 of 3.1c-i), and every entry point asks
    design_views.change_request_window_open() for that. This decides the 403.
    """
    if project is None:
        return False
    return (user_can_manage_project(user, project)
            or user_may_raise_design_change_as_scm(user))


def can_approve_design_release(user, project):
    """Who may approve or reject a design package before release.

    Routed straight through user_can_manage_project(): the site's assigned PM or any of
    its Project Coordinators, by identity, and nobody else. THE SAME PREDICATE AS THE
    CHANGE-REQUEST PATH (user_can_request_design_change above), on purpose — a PM's
    authority over a design means one thing and not two. If it ever needs to differ, that
    is a product decision to take in the open, not a drift between two helpers.

    Its queryset form, for the PM's queue, is manageable_projects_q(); the invariant on
    that function keeps the two in step.

    NO DEPUTY. design_head_qc_pass() admits the Design Head's named deputy, and this
    deliberately does not. The Head's deputy exists so that REVIEW capacity is not one
    person; this gate is the CUSTOMER SIDE accepting the package, and a Design deputy
    accepting on the PM's behalf would be Design approving its own work — the one thing a
    second gate exists to prevent. Cover for an absent PM is a Project Coordinator on the
    site, which this already honours, not a design flag.
    """
    if project is None:
        return False
    return user_can_manage_project(user, project)


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
    """Return True if `user` may SEE the design workflow (including a signed survey or
    CAD link) for `project`.

    Routed through the existing user_can_view_project() so design visibility can never
    drift from project visibility — deliberately NOT re-derived here.

    PART 4 ADDS THE DEPUTY as a second, additive branch. It is needed, not cosmetic: a
    deputy is typically a plain Design user, and user_can_view_project()'s Design branch
    only admits them where they are `assigned_design` or hold a task. Without this a
    deputy could pass QC on a site whose CAD file they were not allowed to open — which
    is not a review, it is a rubber stamp. The Head already reaches every site through
    his own branch in user_can_view_project(); this widens the same design surfaces to
    whoever he has named, and nothing else. user_can_view_project() itself is untouched.

    PART 9 ADDS DESIGN QC on exactly the same grounds, and for exactly the same reason the
    deputy branch exists. A Design QC reviewer is a plain `role='Design'` user who is NOT
    the site's `assigned_design` — by construction, since the assigned designer is the one
    person forbidden from reviewing it. So user_can_view_project()'s Design branch refuses
    them, and without this branch they could open the QC screen and then get "You do not
    have access to this site" from the CAD download link ON THAT SCREEN. Reported from
    live use: gate 1 could see the package listing and open none of it.

    SESSION B.1 ADDS THE ASSIGNED REVIEWER, and it is the Part 9 paragraph above word for
    word with the flag taken out. A designer named as gate-1 reviewer is, by the same
    construction, NOT the site's `assigned_design` — so user_can_view_project() refuses
    them — and they hold no flag, so the branch above refuses them too. This is the branch
    that lets them open the survey and the CAD they are being asked to judge. Per site,
    never portfolio-wide: the whole difference between this and the line above it is that
    this one takes `project`.
    """
    return (user_can_view_project(user, project)
            or user_has_design_head_authority(user)
            or user_is_design_qc(user)
            or user_is_assigned_qc_reviewer(user, project))


# ---------------------------------------------------------------------------
# OPEX site groups (Part 6)
#
# Group formation is SCM's, not Design's and not the PM's (settled decision 3): SCM
# knows order economics and lead times, and grouping is a commercial decision made off
# the system and executed here. So the WRITE gate is the SCM role alone.
#
# READ is wider than write on purpose. Admin reaches every screen in the product, and
# the Design Head has to be able to see which of his released sites have been picked up
# for procurement and which are still sitting in the pool — but he must not be able to
# form, change or lock a group, because he does not own the order.
# ---------------------------------------------------------------------------

def user_can_manage_site_groups(user):
    """Return True if `user` may CREATE a group, add or remove sites, or lock it.

    SCM and nobody else (settled decision 3). Admin is deliberately absent: Part 6's
    §1 and §3 name `role='SCM'` for every write, and only §5 (the view) adds Admin. A
    group is a commitment to a supplier, so the person who signs it is the person who
    forms it.
    """
    profile = getattr(user, 'profile', None)
    if profile is None:
        return False
    return profile.role == 'SCM'


def user_can_view_site_groups(user):
    """Return True if `user` may SEE groups, their members and the aggregated BOQ.

    Read only — confers no authority to change anything. For that, call
    user_can_manage_site_groups(). SCM + Admin (Part 6 §5), plus Design Head authority
    so the Head can see what happened to the sites he released. Head authority is the
    Part 4 helper, so the deputy is admitted alongside him and the 'Design Head' role
    string is not consulted here either.
    """
    profile = getattr(user, 'profile', None)
    if profile is None:
        return False
    if user_can_manage_site_groups(user):
        return True
    if profile.role == 'Admin':
        return True
    return user_has_design_head_authority(user)


def project_boq_is_group_locked(project):
    """Return True if `project` sits in a LOCKED site group, i.e. its BOQ quantities are
    frozen.

    THIS IS THE WHOLE GROUP-LOCK ENFORCEMENT MECHANISM, and it is deliberately a
    SEPARATE predicate rather than a new term inside user_can_edit_project_boq().

    The two answer different questions. `user_can_edit_project_boq()` asks "is this
    person the site's designer" — an authority question about a user. This asks "has
    this site's BOQ been committed to a purchase" — a state question about a site, with
    no user in it at all. Folding the second into the first would mean a Part 0.6 helper
    silently returning False for the right person, and the caller would have no way to
    tell "you are not the designer" from "the BOQ is locked" in order to say so.

    Callers AND the two together: see views.py boq_detail / boq_submit. Every BOQ WRITE
    path takes this term; the SCM branch does NOT, because it writes `ordered_quantity`
    and locking the group is precisely the signal for SCM to start ordering.

    Reverse relation only (`Project.group_memberships` from SiteGroupMembership), keeping
    this module import-free like the rest of it. A removed membership does not count —
    a site that has left a group is free again, which is what makes settled decision 6
    (a change request pulls the site out of a draft group — at the Design Head's acceptance
    since session 3.1c-i) work at all.

    PROCUREMENT ONLY (prompt 1.1b, D-1). A BOQ freeze is a procurement act: it means a
    purchase order has gone out against these quantities, which is why there is no
    unlock. An execution group is the PM's delivery batch and has its own re-plannable
    lifecycle — it must NEVER freeze a BOQ, and this is the predicate that would let it.

    NOTHING BREAKS TODAY WITHOUT THIS FILTER, AND THAT IS THE WHOLE REASON TO ADD IT.
    `group__status='locked'` already restricts the match to a status only procurement
    groups use, so the unnarrowed query is correct — by coincidence, and only until an
    execution lifecycle reuses that word or shares this column. The narrowing makes the
    guarantee structural rather than a fact about which strings happen to be in use.

    `group_type` IS SPELLED ON THE MEMBERSHIP, NOT `group__group_type`. The two cannot
    disagree (SiteGroupMembership.save() copies one from the other and refuses any later
    change), and the local column is the one the partial unique constraint is written
    over — so this filter states the same condition the exclusivity guarantee does, and
    adds no join. `'procurement'` is a literal here rather than GROUP_TYPE_PROCUREMENT
    for the same reason `'locked'` on the line below is: this module imports no models,
    deliberately, and that is not being changed for one constant.
    """
    if project is None:
        return False
    return project.group_memberships.filter(
        removed_at__isnull=True, group__status='locked',
        group_type='procurement',
    ).exists()


# D-b (Zuber, 16 Sep 2026, session 3.1c-i). A DISTINCT HELPER, although today it answers
# exactly what user_can_manage_site_groups() answers. The precedent is
# BOQ_PORTFOLIO_READ_ROLES, which is its own set rather than PORTFOLIO_VIEW_ROLES "so that
# widening project VISIBILITY never silently widens BOQ access as a side effect". Same
# here: forming a procurement group and asking Design to change a released site are two
# rights that happen to belong to the same people. It delegates rather than comparing the
# role string a second time; the day the two must differ, THIS body changes and
# user_can_manage_site_groups() does not.
def user_may_raise_design_change_as_scm(user):
    """Return True if `user` may raise a design change request in SCM's capacity.

    Any SCM user, on any site (D-b) — not scoped to a site, because SCM's remit over
    released sites is portfolio-wide. Confers no triage authority; the Design Head decides.
    """
    return user_can_manage_site_groups(user)


# D-a (Zuber, 16 Sep 2026, session 3.1c-i). ONE window for a RELEASED design, the same for
# the PM, the site's coordinators and SCM: released, and not committed to a purchase. It
# replaces "released AND in a draft group" — a released site in no group is now as
# change-requestable as one in a draft group.
def design_change_window_open(assignment):
    """Return True if a design change request may be raised against a RELEASED design.

    `status == 'released'` AND the site holds no live membership of a LOCKED procurement
    group. The second half is project_boq_is_group_locked(), reused rather than restated,
    so "the BOQ is committed" means the same thing to the BOQ write paths and to this.

    NOT THE WHOLE ANSWER FOR A PM. The PM and coordinators also keep the pre-release
    window (QC started, not yet released), which needs the current attempt and the design
    module's status tuple; design_views.change_request_window_open() combines the two per
    user. SCM gets this window only.

    'released' is a literal for the reason 'locked' and 'procurement' are literals above:
    this module imports no models.
    """
    if assignment is None:
        return False
    return (assignment.status == 'released'
            and not project_boq_is_group_locked(assignment.project))


def project_boq_is_design_locked(project):
    """Return True if `project`'s BOQ is frozen by the DESIGN review loop — the designer
    has handed it to review and has not been sent back.

    PART 11. THE SECOND OF TWO LOCKS, AND THE REVERSIBLE ONE. It is a third separate
    predicate for exactly the reason project_boq_is_group_locked() is a second: the Part
    0.6 helpers answer "is this person the designer", and this answers "has this site's
    BOQ been submitted for review" — a state question about a site with no user in it.
    Folding it into user_can_edit_project_boq() would make a load-bearing Residential
    helper return False for the right person, and the caller could no longer tell "you are
    not the designer" from "the BOQ is with QC" in order to say which.

    THE CONDITION IS ONE FIELD: the CURRENT attempt's `boq_submitted_at`. That single test
    produces every row of the Part 11 lock progression, because the Part 9 rework loop
    already maintains that stamp exactly as the progression describes:

        designer saving drafts        stamp null      -> editable
        marks BOQ complete            stamp set       -> frozen
        Design QC rejects             new attempt     -> reopens (see below)
        Design QC approves            same attempt    -> stays frozen
        Design Head rejects           new attempt     -> reopens
        Design Head approves          same attempt    -> DESIGN LOCK
        PM change request             new attempt     -> reopens

    READ THAT TABLE AS A LIST OF EVENTS, NOT AS AN ORDER. It once doubled as a narrative —
    complete the BOQ, then the reviewers rule on it — and that reading is no longer
    available: the BOQ is not gated on the Arka, so a designer may stamp it before the
    Arka is submitted, before either gate has ruled, or long after. The rows above say
    what each EVENT does to the stamp; none of them says which happens first.

    Three consequences worth naming, because a reader who assumed the old order would get
    each of them wrong:

      * "DESIGN LOCK" on the Head-approval row is the stamp SURVIVING that event on the
        same attempt, not the approval conferring a lock. A BOQ stamped before any Arka
        existed is frozen from the moment of the stamp, with no verdict anywhere in sight.
      * The rows are not exhaustive of the Arka's life. An Arka REJECTED and replaced
        within one attempt opens no attempt, so it appears nowhere above and does nothing
        to the stamp — a BOQ completed against Arka v1 stays frozen and stays stamped
        under Arka v2. That is a real gap, surfaced by the "completed under Arka vN, not
        the current version" badge on the reviewer screens and closed by nothing.
      * The lock is state, not authority, and it always was — which is why the reviewer's
        correction path (user_can_correct_boq) sits outside it rather than loosening it.

    The PREDICATE below is unaffected by all of this and is deliberately not touched: one
    field, read live. Only the story around it needed correcting.

    REOPENING IS TOTAL, NOT QUANTITY-ONLY. Nothing here is per-row: when the stamp clears,
    the whole entry screen comes back with its picker, so the designer can add an item that
    was never on the sheet. That is the point — 14 of the 16 error categories map to a redo
    set containing REDO_BOQ, and the two BOQ-specific ones (`boq_quantity`,
    `boq_specification`) are precisely the failures that may need a NEW line rather than a
    corrected number. Restoring only the quantity fields would leave the designer unable to
    fix the thing they were failed for.

    A REJECTION THAT WAS NOT ABOUT THE BOQ LEAVES IT FROZEN, and that is deliberate rather
    than a gap. Part 9.1 scopes rework: `_carry_forward_artifacts()` carries the completion
    stamp to the new attempt when REDO_BOQ is not in the reviewer's redo set. Only
    `drawing_incomplete` does that, and a designer redoing a drawing has not been asked to
    touch the bill. Reopening it anyway would overrule the reviewer's own scoping.

    Reverse relations only, keeping this module import-free like the rest of it.

    ALWAYS FALSE FOR RESIDENTIAL, structurally: a DesignAssignment only ever exists on an
    OPEX site (design_views._opex_site 404s everything else), so a Residential project has
    no `design_assignment` and returns at the first guard. That is what makes it safe to
    AND this term into the shared boq_detail write gate.
    """
    if project is None:
        return False
    assignment = getattr(project, 'design_assignment', None)
    if assignment is None or not assignment.current_attempt_number:
        return False
    return assignment.attempts.filter(
        attempt_number=assignment.current_attempt_number,
        boq_submitted_at__isnull=False,
    ).exists()


# Roles who may read the per-user status report — every individual's task workload and
# whether they logged in. Deliberately NOT PORTFOLIO_VIEW_ROLES: that set contains
# Finance and SCM, whose portfolio-wide remit is over PROJECTS, not over their
# colleagues. This report is about people, and seeing every project is not a reason to
# see every person's login and workload.
#
# Kept as its own frozenset for exactly the reason BOQ_PORTFOLIO_READ_ROLES is
# (permissions.py:156-159): so that widening one visibility can never silently widen
# another as a side effect. Adding a role to PORTFOLIO_VIEW_ROLES must not hand out
# per-person surveillance by accident.
USER_STATUS_REPORT_ROLES = frozenset({'CEO', 'Admin', 'System Admin'})


def can_view_user_status_report(user):
    """Who may see per-user task and login counts for every user.

    True for CEO, Admin and System Admin only (USER_STATUS_REPORT_ROLES). This is not a
    project-scoped question — there is no project argument — so it takes the user alone.

    The `getattr` guard mirrors every other helper in this module: a superuser created
    via `createsuperuser` may have no UserProfile, and a user with no profile holds no
    role and therefore no access.
    """
    profile = getattr(user, 'profile', None)
    if profile is None:
        return False
    return profile.role in USER_STATUS_REPORT_ROLES


# Who may read the reviewer-correction trail on a BOQ's history page — who changed which
# quantity on somebody else's bill, and when.
#
# ITS OWN FROZENSET, for exactly the reason USER_STATUS_REPORT_ROLES is its own and not
# PORTFOLIO_VIEW_ROLES: so widening one visibility can never silently widen another.
# BOQ_PORTFOLIO_READ_ROLES admits SCM, whose portfolio-wide remit is over the QUANTITIES
# they will order — not over which of two reviewers overruled the other on the way there.
#
# NOT THE PM AND NOT THE DESIGNER, deliberately, and the designer's exclusion is the
# load-bearing one: this trail is an audit record and NOT designer rework accounting (prior
# decision). Putting it on the designer's own screen is how it starts being read as a tally
# against them.
BOQ_CORRECTION_AUDIT_ROLES = frozenset({'CEO', 'Admin', 'System Admin'})


def can_view_boq_corrections(user):
    """Who may see the reviewer-correction trail on a BOQ.

    Takes the user alone, not a project: it is a question about a role's remit, and the
    per-BOQ read gate (user_can_view_project_boq) is ANDed at the caller — this can only
    narrow who reaches the trail, never widen who reaches the BOQ.
    """
    profile = getattr(user, 'profile', None)
    if profile is None:
        return False
    return profile.role in BOQ_CORRECTION_AUDIT_ROLES


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


# ---------------------------------------------------------------------------
# Two-step task completion (2.1) — OPEX only
#
# TWO AUTHORITIES, TWO PREDICATES, AND THEY MUST NOT COLLAPSE INTO ONE. The whole
# point of a two-step completion is that the hand that does the work is not the
# hand that signs it off, so "may submit" and "may approve" are separate questions
# with deliberately different answers. A single `user_can_act_on_approval()` would
# make the PM's dual membership invisible and would make widening one side silently
# widen the other.
#
# THE PM IS IN BOTH SETS, BUT NOT FOR THE SAME TASK. A PM may submit a task and a
# PM may approve one; the same PM may not do both to the same task. That refusal
# is NOT here — it lives in `task_approve`, because it is a fact about one task's
# current submission (which changes as the task is rejected and resubmitted by
# different people) rather than about who holds authority on the project. See the
# note at the check itself for why keeping the two apart matters: this predicate
# is also what the screen asks to decide whether to offer a verdict at all, and it
# is shared with REJECT, where withdrawing your own submission is legitimate.
#
# SCOPE IS NOT DECIDED HERE. Both helpers answer "does this person hold the
# authority", not "is this an OPEX task" — the OPEX restriction is a property of
# the RULE, lives with the rung in `_apply_task_status_change()` and with the view
# gates, and is checked before either of these is called. Mixing it in would give
# two places to read the scope from.
# ---------------------------------------------------------------------------

def user_can_submit_task_for_approval(user, task, project):
    """
    Return True if `user` may submit `task` on `project` for approval.

    The assigned engineer OR PM-level authority on the project. `task.assigned_to`
    and not the ROLE, because submitting is a claim about work this specific person
    did — a second site engineer holding the same role did not do it and has nothing
    to attest to. That is the same user-level rule `task_detail_status_update`
    already applies to the status control on the very same screen.

    PM authority comes through user_can_manage_project(), so a Project Coordinator
    is admitted alongside the assigned PM exactly as everywhere else in this module.
    """
    profile = getattr(user, 'profile', None)
    if profile is None:
        return False
    if task.assigned_to is not None and task.assigned_to == profile:
        return True
    return user_can_manage_project(user, project)


def user_can_approve_task(user, project):
    """
    Return True if `user` may approve or reject a submitted task on `project`.

    PM-level authority on the project, OR the QA/QC capability flag plus visibility
    of the project.

    WHY `is_qaqc` IS PAIRED WITH VISIBILITY AND NOT USED ALONE. UserProfile.is_qaqc
    is a PORTFOLIO-WIDE boolean — there is no per-project QA/QC assignment anywhere
    in the schema today. Read on its own it would let one QA/QC holder sign off work
    on every site in the company, including sites they cannot open. Anding it with
    user_can_view_project() scopes the capability to the projects the person already
    reaches by their role, which is the closest thing to "QA/QC on that project" the
    data model can currently express. When a per-project QA/QC assignment arrives,
    it replaces the visibility term HERE and nowhere else.

    No `task` argument: approval authority is a property of the project and the
    person, identical for every task on it. The per-task questions — is this thing
    actually submitted, and is the person asking the one who submitted it — are
    state, not permission, and are asked by `task_approve`.
    """
    profile = getattr(user, 'profile', None)
    if profile is None:
        return False
    if user_can_manage_project(user, project):
        return True
    return bool(profile.is_qaqc) and user_can_view_project(user, project)


def task_has_independent_approver(project, submitter_profile):
    """
    Return True if at least one ACTIVE user OTHER THAN `submitter_profile` passes
    `user_can_approve_task()` for `project`.

    This is the question `task_approve`'s self-approval guard creates and never
    answers: that guard refuses the submitter, and on a project whose only approver
    IS the submitter it therefore refuses everybody. Verified on production 20 Sep
    2026 — nought `is_qaqc` holders company-wide and nought OPEX projects with a
    coordinator, so every OPEX project's approver pool was exactly its assigned PM
    and every PM submission deadlocked. Answering it here lets
    `task_submit_for_approval` complete such a task as self-certified instead.

    THE TRIGGER IS THE EMPTY POOL, NOT A ROLE. Nothing in this function looks at who
    the submitter is or what they do — a PM submitting their own task, a PM
    submitting on an engineer's behalf and an engineer on a site whose PM has been
    deactivated all reach the same answer by the same route. A "skip the step when
    the assignee is the PM" rule was considered and rejected: it would have been a
    second, role-shaped copy of the authority model, true today only because the
    pool happens to be one person.

    IT CANNOT DRIFT FROM `user_can_approve_task()`, BY CONSTRUCTION. The set built
    below is a CANDIDATE set, not an answer: it exists only to bound the number of
    users this has to ask about, and every candidate is then put through
    `user_can_approve_task()` itself. Widening or narrowing that predicate changes
    this function's result with no edit here. What the candidate set must stay is a
    SUPERSET of everyone the predicate admits, and it is exactly that by reading the
    predicate's own two arms: `user_can_manage_project()` admits the assigned PM and
    this project's coordinators, and the second arm admits `is_qaqc` holders. There
    is no third source. Should a third ever be added to `user_can_approve_task()`,
    it must be added to the candidate set here too — that is the one coupling, and
    it is stated rather than hidden.

    ACTIVE ON BOTH MODELS. `UserProfile.is_active` is the portal's soft
    deactivation and `User.is_active` is Django's login gate; a person switched off
    by either cannot sign anything, so counting them would hand the site an approver
    who can never arrive. This is the only term NOT delegated to the predicate,
    which has no opinion on deactivation because every other caller reaches it
    through `request.user` and Django has already refused an inactive login.

    NO MODEL IMPORT, deliberately — see the note at the top of this module. The
    `UserProfile` class is reached through `project.coordinators.model`, the target
    of the relation, so this module stays free of the circular imports that the
    no-model-imports rule exists to prevent.
    """
    if project is None:
        return False

    user_profile_model = project.coordinators.model

    candidate_pks = set()
    # Read from the FK id, not `project.assigned_pm`, so an unset PM costs no query.
    if project.assigned_pm_id is not None:
        candidate_pks.add(project.assigned_pm_id)
    candidate_pks.update(project.coordinators.values_list('pk', flat=True))
    candidate_pks.update(
        user_profile_model.objects.filter(is_qaqc=True).values_list('pk', flat=True)
    )

    # The whole point of the function: the submitter is not their own second
    # signature. Dropped from the CANDIDATES rather than tested inside the loop, so
    # a project whose only approver is the submitter does not even reach the
    # predicate.
    if submitter_profile is not None:
        candidate_pks.discard(submitter_profile.pk)

    if not candidate_pks:
        return False

    candidates = (
        user_profile_model.objects
        .filter(pk__in=candidate_pks, is_active=True, user__is_active=True)
        .select_related('user')
    )
    for candidate in candidates:
        if user_can_approve_task(candidate.user, project):
            return True
    return False


def user_may_self_certify(user, project):
    """
    Return True if `user` submitting a task on `project` would COMPLETE it under
    their own name rather than queue it for someone else's signature.

    TWO TERMS, AND BOTH ARE NECESSARY:

        user_can_approve_task(user, project)              — they hold the signature
        not task_has_independent_approver(project, ...)   — nobody else does

    THE FIRST TERM IS THE ONE THIS FUNCTION EXISTS FOR. Self-certification collapses
    two signatures into one; it can only do that for somebody who was entitled to
    give the second one. Without this term an empty pool promoted WHOEVER happened
    to be submitting — on a site whose PM had been deactivated, a site engineer
    self-certified their own work, which is not one person doing two jobs but a
    person doing a job that was never theirs. That case now takes the ordinary path
    and waits at "Awaiting approval" for a manager who must be reassigned; see the
    departed-PM entry in the deferred log.

    SO AN EMPTY POOL NO LONGER IMPLIES SELF-CERTIFICATION, and the two helpers stay
    separate for that reason. `task_has_independent_approver()` answers a question
    about the PROJECT ("is there a second signature available"), which the deferred
    work on departed PMs will want on its own to warn about sites that have no
    active approver at all. This answers a question about a PERSON on that project,
    and only this one may relabel a button or complete a task.

    ONE FUNCTION, THREE CALLERS, DELIBERATELY. `task_submit_for_approval` decides
    with it, and `_task_approval_context()` and `_phase_list_two_step()` label the
    button with it. A button that offered to complete a task the endpoint would
    merely queue — or the reverse — is the one failure this feature cannot have,
    because it would take a completion under someone's name while the control they
    pressed said it was asking for review. Reassembling the two terms at each site
    is exactly how that drift starts, so they are assembled once, here.
    """
    profile = getattr(user, 'profile', None)
    if profile is None:
        return False
    # Cheap term first, and it is also the one that refuses most callers: every site
    # engineer on a healthy project fails here without the pool ever being built.
    if not user_can_approve_task(user, project):
        return False
    return not task_has_independent_approver(project, profile)


def user_can_waive_punch_point(user, project):
    """
    Return True if `user` may waive a punch point on `project` — accept a recorded
    defect and let the site proceed with it.

    PM-LEVEL AUTHORITY ONLY. This is deliberately NARROWER than
    `user_can_approve_task()` above, and the difference is the entire point of
    B-12: the QA/QC holder is the person who RAISES the defect (a rejection creates
    the punch point), so letting the same capability wave it away would make the
    record self-cancelling — the one signature that is supposed to cost something
    would be available to the person who wrote the finding. A user holding
    `is_qaqc` and nothing else is refused here even though the same user may reject
    the very task the punch point hangs off.

    B-12 also settles what is NOT here: no severity threshold above which a waiver
    needs more authority, and no second signature. One PM, one reason, recorded.
    When a threshold arrives it belongs in this function and nowhere else.

    Routed through `user_can_manage_project()` like every other PM-ownership check
    in this module, so the assigned PM and a Project Coordinator on the project are
    both admitted — coordinator authority is additive PM authority everywhere else
    in the portal and inventing a second, narrower notion of "PM" here would be the
    only place it means something different.
    """
    profile = getattr(user, 'profile', None)
    if profile is None:
        return False
    return user_can_manage_project(user, project)
