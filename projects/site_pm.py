"""The one writer of `Project.assigned_pm` (ruling PM-2).

Five places put a PM on a site — project_create, create_opex_site (single and bulk),
zoho_deal_closed_webhook, admin_assign_pm and subadmin_projects — and every one of them
calls change_site_pm(). ProjectAdmin shows `assigned_pm` read-only. The only other
write left is the seed command seed_opex_test_data, named in the source check in
tests_site_pm.py; anything else that writes the column fails that test.

What a change does, in one transaction (PM-1 to PM-4):
  - refuses a soft-deleted or Cancelled site, on every path (PM-1, Q6)
  - closes the site's open SitePmAssignment row and opens the next (PM-3)
  - writes `assigned_pm` (filter().update(), under the row lock)
  - moves the outgoing PM's open tasks on that site to the incoming PM (PM-4)
then logs ONE `pm_assigned` ActivityLog line after the writes commit.

It sends nothing. admin_assign_pm and the webhook still send their own existing
notifications after calling this; no new notification was added (ruling Q5).
"""
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Count, OuterRef, Subquery, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from projects.models import Project, SitePmAssignment, Task, UserProfile, log_activity
from projects.permissions import can_change_site_pm
from projects.utils import assign_tasks_to


# A Cancelled site keeps whoever was PM when it was cancelled. Giving it a new one would
# hand a closed site's open tasks to someone who never worked it (PM-1, Q6).
SITE_PM_REFUSED_STATUSES = frozenset({'Cancelled'})


class SitePmChangeRefused(Exception):
    """The site, its state or the request makes this PM write impossible. The message is
    written for the person who asked and is safe to show them. A caller who is not
    allowed to change the PM gets PermissionDenied (a 403) instead."""


def _name(profile):
    return profile.user.get_full_name() or profile.user.username


def open_tasks_held_by(project, pm_id):
    """The tasks a change of PM moves: everything on `project` assigned to `pm_id`
    that is not Done (PM-4), whatever its assigned_role.

    "Not Done" on purpose, not a list of statuses: Blocked and In Progress move too,
    and so do Not Applicable tasks, which keep their status beside the flag. A task
    submitted for approval but not yet approved is not Done either, so it moves as
    well; only `assigned_to` changes — `submitted_by` / `submitted_at` still name who
    submitted it.

    with_outgoing_pm_task_count() counts the same set for the confirmation line; if
    this predicate changes, change that one too.
    """
    return (Task.objects
            .filter(phase__project=project, assigned_to_id=pm_id)
            .exclude(status=Task.DONE))


def with_outgoing_pm_task_count(projects_qs):
    """Annotate each project with `outgoing_pm_task_count`: how many tasks
    change_site_pm() would move away from its current PM right now — the N in the
    "N open tasks will move from X to Y" confirmation on admin_project_list.

    A correlated subquery, not Count() over a join, so it cannot multiply rows or
    collide with the caller's own prefetches and annotations; one extra column, no
    extra query. Same predicate as open_tasks_held_by().
    """
    held = (Task.objects
            .filter(phase__project=OuterRef('pk'), assigned_to=OuterRef('assigned_pm'))
            .exclude(status=Task.DONE)
            .order_by()
            .values('phase__project')
            .annotate(n=Count('pk'))
            .values('n'))
    return projects_qs.annotate(outgoing_pm_task_count=Coalesce(Subquery(held), Value(0)))


def change_site_pm(project, new_pm, by, reason='', first_assignment_only=False):
    """Make `new_pm` the PM of `project`. Returns the number of open tasks moved.

    Args:
        project: the site. Re-read under a row lock here, so a stale instance is fine;
                 on success its in-memory `assigned_pm` is updated to match.
        new_pm:  the incoming PM's UserProfile. Its role is NOT checked here — the
                 callers each look the PM up with role='PM' already, except the Zoho
                 webhook, whose missing role filter is DEFERRED; refusing a non-PM here
                 would change that webhook's behaviour before its own fix lands.
        by:      the acting UserProfile, or None for the webhook (no human).
        reason:  required when the site already has a PM; optional on a first assignment.
        first_assignment_only: refuse if the site already has a PM — subadmin_projects,
                 whose screen only ever fills an empty seat. Checked under the lock, so
                 two System Admins racing for the same empty seat cannot both win.

    Raises:
        PermissionDenied     a change (the site already has a PM) by anyone who is not
                             Admin, or with no actor at all.
        SitePmChangeRefused  deleted or Cancelled site, missing reason on a change, or
                             an occupied seat with first_assignment_only.

    Assigning the PM the site already has is a no-op: no history row, no log line.
    Callers inside their own transaction.atomic() (project_create, create_opex_site)
    get a savepoint here; the webhook calls it OUTSIDE its creation block on purpose,
    so a failure here never rolls back the project (ruling C1).
    """
    reason = (reason or '').strip()

    with transaction.atomic():
        # Lock the project row first so two concurrent changes on one site serialise:
        # the second re-reads assigned_pm after the first commits and is judged against
        # the PM the first one installed, not the one it saw on screen.
        site = Project.objects.select_for_update().get(pk=project.pk)
        old_pm_id = site.assigned_pm_id

        if site.is_deleted:
            raise SitePmChangeRefused(f'{site.project_id} has been deleted.')
        if site.status in SITE_PM_REFUSED_STATUSES:
            raise SitePmChangeRefused(
                f'{site.project_id} is {site.status} — its PM cannot be changed.')
        if old_pm_id == new_pm.pk:
            project.assigned_pm = new_pm
            return 0

        if old_pm_id is not None:
            if first_assignment_only:
                raise SitePmChangeRefused(
                    f'{site.project_id} already has a PM assigned. '
                    f'Reassignment is not permitted here.')
            # Only Admin may take a site from one PM and give it to another (PM-7, Q3):
            # the change moves the outgoing PM's open work and removes their access.
            # No actor (the webhook) can only ever fill an empty seat.
            if by is None or not can_change_site_pm(by.user):
                raise PermissionDenied('Only an Admin can change a site\'s PM.')
            if not reason:
                raise SitePmChangeRefused('A reason is required to change the PM.')

        now = timezone.now()
        # Close the open span. Race: safe — the project row lock above is held, so no
        # other change_site_pm() on this site can be between this update and the insert
        # below; uniq_site_pm_open is the backstop if that lock is ever bypassed.
        SitePmAssignment.objects.filter(site=site, valid_to__isnull=True).update(valid_to=now)
        SitePmAssignment.objects.create(
            site=site, pm=new_pm, valid_from=now, assigned_by=by, reason=reason,
        )
        # Race: safe for the same reason — the row is locked. filter().update() rather
        # than save() so this writes the one column and cannot overwrite a field another
        # request changed on a stale instance the caller is holding.
        Project.objects.filter(pk=site.pk).update(assigned_pm=new_pm)

        moved = 0
        if old_pm_id is not None:
            # PM-4: the outgoing PM's open work follows the seat. Done tasks never move —
            # they record who did the work. Silent, set-based (assign_tasks_to).
            moved = assign_tasks_to(open_tasks_held_by(site, old_pm_id), new_pm)

    project.assigned_pm = new_pm

    # After the writes, as everywhere else: log_activity swallows its own failures, so a
    # failed log line never undoes a PM change. One line per change, the task count in
    # it — not one line per moved task (the design_bulk_assigned precedent).
    if old_pm_id is None:
        action = f"PM assigned: {_name(new_pm)}"
    else:
        old_pm = UserProfile.objects.select_related('user').get(pk=old_pm_id)
        action = (f"PM changed from {_name(old_pm)} to {_name(new_pm)} — "
                  f"{moved} open task{'s' if moved != 1 else ''} moved. Reason: {reason}")
    log_activity(project, by, action, entity_type='Project', entity_id=project.pk,
                 action_code='pm_assigned')
    return moved
