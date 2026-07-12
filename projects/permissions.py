"""
Central authorization helpers for project-scoped access.

`user_can_manage_project()` is the SINGLE canonical place PM-level management
authority is decided. Do NOT compare `Project.assigned_pm` (or, in a future
change, coordinators) or role strings directly anywhere else in the codebase —
call this function instead. Keeping one comparison path prevents new code from
copying whichever nearby pattern happens to be wrong for its context.
"""


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
