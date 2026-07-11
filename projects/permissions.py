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

    Currently equivalent to "user is the project's assigned PM". This is the one
    canonical comparison path — every PM-ownership check routes through here.

    `Project.assigned_pm` is a ForeignKey to `UserProfile`, so we compare against
    `user.profile` (not the raw `User`). `getattr` guards against a user with no
    profile (e.g. a superuser created via `createsuperuser`), matching the
    pre-refactor behaviour where such a user never matched an `assigned_pm`.
    A null `assigned_pm` compares False, matching the old `is not None` guards.

    NOTE: Project Coordinator OR-logic will be added here in a later change.
    Do not add it now.
    """
    profile = getattr(user, 'profile', None)
    return profile is not None and project.assigned_pm == profile
