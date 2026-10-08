"""
Site QA/QC engineer — closeout step 3 (docs/CLOSEOUT_SPEC.md CL-9, CL-A, CL-D).

A PM or coordinator names one active Site Engineer as an OPEX site's QA/QC engineer.
The assignment grants that engineer view of the site and the right to approve or
reject its submitted tasks (permissions.user_is_site_qaqc); nothing else.

A separate module from views.py for the same reason order_views.py is one: a
self-contained screen with its own URL. urls.py imports it beside `views`.

THE ONLY WRITER of SiteQaqcAssignment: assign_site_qaqc() and end_site_qaqc() below.
The model's save() refuses an update and delete() always refuses, so ending a row is
this module's single filter().update().

The project page's card and the Site Engineer dashboard's "QA/QC sites" section read
their context from here too, so views.py imports this module lazily (it is imported
BY this module for _active_project; a top-level import there would be circular).
"""

from django.contrib import messages
from django.db import IntegrityError, transaction
from django.db.models import Count
from django.http import Http404, HttpResponseForbidden
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone

from .decorators import login_required
from .models import Project, SiteQaqcAssignment, Task, UserProfile, log_activity
from .notifications import send_notification
from .permissions import user_can_manage_project, user_can_view_project
from .views import _active_project

IN_APP_AND_EMAIL = ['in_app', 'email']

# NotificationLog.template_name labels. Not Interakt templates: WhatsApp is not among
# the channels, so these only name the event on the log row.
T_ASSIGNED = 'site_qaqc_assigned'
T_REMOVED = 'site_qaqc_removed'

#: How many held tasks the CL-A refusal names before summarising the rest.
HELD_TASKS_NAMED = 5


class QaqcAssignmentRefused(Exception):
    """A rule refused the assignment or ending. `str(exc)` is the user-facing message."""


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def _person(profile):
    return profile.user.get_full_name() or profile.user.username


def _site_label(project):
    """The site as a person names it: its code, and the customer name when there is one."""
    if project.customer_name and project.customer_name != project.project_id:
        return f"{project.project_id} ({project.customer_name})"
    return project.project_id


def current_site_qaqc(project):
    """The site's active assignment row, with its engineer's user joined, or None.

    For DISPLAY (the card, the screen). Authority never reads this: it goes through
    permissions.user_is_site_qaqc(), which re-checks the engineer's role and status.
    """
    if project.project_type != 'OPEX':
        return None
    return (SiteQaqcAssignment.objects
            .filter(project=project, ended_at__isnull=True)
            .select_related('engineer__user', 'assigned_by__user')
            .first())


def engineer_is_stale(profile):
    """True if the engineer named on an active row can no longer act as one.

    The same two terms user_is_site_qaqc() re-checks (role, profile active), plus the
    login flag, so the card can tell the PM the row needs replacing or ending. The row
    itself is never ended automatically: history is the PM's to write.
    """
    return (profile.role != 'Site Engineer' or not profile.is_active
            or not profile.user.is_active)


def tasks_held_on_site(profile, project):
    """Every task on `project` assigned to `profile` — any status, mirrors and Not
    Applicable rows included. CL-A: a person who holds (or held to completion) work on
    a site is not independent of it, so a Done task disqualifies as much as an open one.
    Tasks have no soft delete, so nothing else needs filtering."""
    return list(
        Task.objects.filter(phase__project=project, assigned_to=profile)
        .order_by('phase__phase_order', 'pk')
        .values_list('task_name', flat=True)
    )


def eligible_qaqc_engineers(project):
    """The Site Engineers who may be named QA/QC engineer on `project` now.

    Active on both models (UserProfile.is_active is the portal's deactivation,
    User.is_active Django's login gate — a person off by either can never sign
    anything), holding no task on the site (CL-A), and not already its engineer.
    One query: the held-task exclusion is a subquery on the same statement.
    """
    holders = (Task.objects.filter(phase__project=project, assigned_to__isnull=False)
               .values('assigned_to'))
    qs = (UserProfile.objects
          .filter(role='Site Engineer', is_active=True, user__is_active=True)
          .exclude(pk__in=holders)
          .select_related('user')
          .order_by('user__first_name', 'user__last_name', 'user__username'))
    current = current_site_qaqc(project)
    if current is not None:
        qs = qs.exclude(pk=current.engineer_id)
    return qs


def site_qaqc_card_context(project, can_manage):
    """Context for the QA/QC card on project_overview. OPEX only: on any other site the
    card is not drawn (`show_qaqc_card` False) and no query is spent."""
    if project.project_type != 'OPEX':
        return {'show_qaqc_card': False, 'qaqc_assignment': None}
    assignment = current_site_qaqc(project)
    return {
        'show_qaqc_card':  True,
        'qaqc_assignment': assignment,
        'qaqc_stale':      assignment is not None and engineer_is_stale(assignment.engineer),
        'can_manage_qaqc': can_manage,
    }


def qaqc_sites_for_engineer(profile):
    """The Site Engineer dashboard's "QA/QC sites" rows: every live site this engineer
    is the active QA/QC engineer for, each with its count of tasks awaiting approval.

    Two queries whatever the number of sites: the assignments, then one grouped count.
    "Awaiting approval" is Task.is_awaiting_approval as a filter — submitted, not yet
    approved — the queue this engineer is there to clear.
    """
    assignments = list(
        SiteQaqcAssignment.objects
        .filter(engineer=profile, ended_at__isnull=True,
                project__is_deleted=False, project__project_type='OPEX')
        .select_related('project')
        .order_by('project__project_id')
    )
    if not assignments:
        return []
    awaiting = dict(
        Task.objects
        .filter(phase__project_id__in=[a.project_id for a in assignments],
                submitted_at__isnull=False, approved_at__isnull=True)
        .values('phase__project_id')
        .annotate(n=Count('pk'))
        .values_list('phase__project_id', 'n')
    )
    return [
        {'project': a.project, 'assigned_at': a.assigned_at,
         'awaiting_count': awaiting.get(a.project_id, 0)}
        for a in assignments
    ]


# ---------------------------------------------------------------------------
# Writers — the only code that creates or ends a SiteQaqcAssignment
# ---------------------------------------------------------------------------

def _refuse_ineligible(project, engineer):
    """Raise QaqcAssignmentRefused unless `engineer` may be named on `project`.
    Re-asked inside the writer's lock, so a task handed to the engineer between the
    screen loading and the POST still refuses."""
    if project.project_type != 'OPEX' or project.is_deleted:
        raise QaqcAssignmentRefused(
            'A QA/QC engineer is assigned on RESCO sites only.')
    if engineer_is_stale(engineer):
        raise QaqcAssignmentRefused(
            f"{_person(engineer)} is not an active site engineer.")
    held = tasks_held_on_site(engineer, project)
    if held:
        named = ', '.join(held[:HELD_TASKS_NAMED])
        more = len(held) - HELD_TASKS_NAMED
        if more > 0:
            named += f' and {more} more'
        raise QaqcAssignmentRefused(
            f"{_person(engineer)} holds {len(held)} task{'s' if len(held) != 1 else ''} "
            f"on this site ({named}). A site's QA/QC engineer may not hold a task on "
            f"the site they check — reassign those tasks or choose another engineer.")


def _notify(recipient_pk, message, project_pk, actor_pk, template):
    """One in-app + email notice, sent after the assignment commits. Re-reads by pk so
    the callback holds no ORM objects from the request's transaction. A person
    deactivated on either model is not told: they cannot sign in to act on it."""
    recipient = (UserProfile.objects.select_related('user')
                 .filter(pk=recipient_pk, is_active=True, user__is_active=True).first())
    if recipient is None:
        return
    project = Project.objects.filter(pk=project_pk).first()
    actor = UserProfile.objects.filter(pk=actor_pk).first()
    send_notification(
        recipient, message, channels=IN_APP_AND_EMAIL,
        link=reverse('project_overview', args=[project.project_id]) if project else '',
        subject=message, template=template, related_project=project, actor=actor,
    )


def _notify_assigned(row):
    project = row.project
    message = (f"You are now the QA/QC engineer for {_site_label(project)}. You can "
               f"view the site and approve or reject its submitted tasks.")
    engineer_pk, project_pk, actor_pk = row.engineer_id, project.pk, row.assigned_by_id
    # Lambdas, not functools.partial: on_commit(robust=True) names the callback in its
    # log line and the approval notices settled on this form (approval_notices.py).
    transaction.on_commit(
        lambda: _notify(engineer_pk, message, project_pk, actor_pk, T_ASSIGNED),
        robust=True)


def _notify_removed(project, engineer_pk, actor_pk):
    # Q-D: someone losing access to a site without notice will think the system broke.
    message = f"You are no longer the QA/QC engineer for {_site_label(project)}."
    project_pk = project.pk
    transaction.on_commit(
        lambda: _notify(engineer_pk, message, project_pk, actor_pk, T_REMOVED),
        robust=True)


def _end_row(row, actor, reason, now):
    """End one active row. Raises QaqcAssignmentRefused if it was already ended."""
    # Race: the caller holds the project row lock, so no second writer can be ending
    # this row; the ended_at__isnull=True term still makes the update a no-op rather
    # than a double-stamp if that lock is ever bypassed, and the 0 is reported.
    updated = (SiteQaqcAssignment.objects
               .filter(pk=row.pk, ended_at__isnull=True)
               .update(ended_at=now, ended_by=actor, end_reason=reason))
    if updated != 1:
        raise QaqcAssignmentRefused(
            'This site\'s QA/QC assignment was changed by someone else. Reload and try again.')


def assign_site_qaqc(project, engineer, actor):
    """Name `engineer` as `project`'s QA/QC engineer, replacing any current one.

    Atomic: the project row is locked, eligibility is re-checked under the lock, the
    current row (if any) is ended as `replaced`, and the new row is inserted. Returns
    the new row; raises QaqcAssignmentRefused with the user-facing reason otherwise.
    Notices (new engineer, and the outgoing one) are sent after commit.
    """
    with transaction.atomic():
        # Serialises two managers assigning at once: the second waits here, then sees
        # the first one's row as current and replaces it (or is refused as a no-op).
        locked = Project.objects.select_for_update().get(pk=project.pk)
        current = (SiteQaqcAssignment.objects
                   .filter(project=locked, ended_at__isnull=True)
                   .select_related('engineer__user').first())
        if current is not None and current.engineer_id == engineer.pk:
            raise QaqcAssignmentRefused(
                f"{_person(engineer)} is already this site's QA/QC engineer.")
        _refuse_ineligible(locked, engineer)
        now = timezone.now()
        if current is not None:
            _end_row(current, actor, SiteQaqcAssignment.END_REPLACED, now)
        try:
            # A savepoint, so the backstop constraint's refusal leaves the outer
            # transaction usable for the clean rollback below.
            with transaction.atomic():
                row = SiteQaqcAssignment.objects.create(
                    project=locked, engineer=engineer, assigned_by=actor, assigned_at=now,
                )
        except IntegrityError:
            # uniq_site_qaqc_active: only reachable if the lock above was bypassed.
            raise QaqcAssignmentRefused(
                'This site already has a QA/QC engineer. Reload and try again.')
        _notify_assigned(row)
        if current is not None:
            _notify_removed(locked, current.engineer_id, actor.pk)

    if current is not None:
        action = (f"Assigned QA/QC engineer: {_person(engineer)} "
                  f"(replacing {_person(current.engineer)})")
    else:
        action = f"Assigned QA/QC engineer: {_person(engineer)}"
    log_activity(project, actor, action, entity_type='SiteQaqcAssignment',
                 entity_id=row.pk, action_code='site_qaqc_assigned')
    return row


def end_site_qaqc(project, actor):
    """End `project`'s active QA/QC assignment with no successor. Returns the ended
    row; raises QaqcAssignmentRefused if there is none. The engineer's access stops
    with the commit, and they are told after it."""
    with transaction.atomic():
        locked = Project.objects.select_for_update().get(pk=project.pk)
        current = (SiteQaqcAssignment.objects
                   .filter(project=locked, ended_at__isnull=True)
                   .select_related('engineer__user').first())
        if current is None:
            raise QaqcAssignmentRefused('This site has no QA/QC engineer to end.')
        _end_row(current, actor, SiteQaqcAssignment.END_ENDED, timezone.now())
        _notify_removed(locked, current.engineer_id, actor.pk)

    log_activity(project, actor, f"Ended QA/QC engineer: {_person(current.engineer)}",
                 entity_type='SiteQaqcAssignment', entity_id=current.pk,
                 action_code='site_qaqc_ended')
    return current


# ---------------------------------------------------------------------------
# Screen
# ---------------------------------------------------------------------------

@login_required
def site_qaqc(request, project_id):
    """
    Assign, replace or end an OPEX site's QA/QC engineer, and show the history.
    Access: the site's PM and its coordinators (user_can_manage_project). GET shows the
    screen; POST action=assign (engineer_id) or action=end.
    """
    project = _active_project(project_id)

    # A site you cannot see does not exist for you (404, the 0.2 shape); one you can
    # see but do not manage refuses the screen (403). Only the site's managers name its
    # QA/QC engineer, consistent with every other site-level authority (go-ahead ruling);
    # the engineer, other PMs, Design, SCM and Finance are refused.
    if not user_can_view_project(request.user, project):
        raise Http404
    if not user_can_manage_project(request.user, project):
        return HttpResponseForbidden()
    # Residential and CAPEX sites have no QA/QC assignment (CL-9): the screen does not
    # exist for them.
    if project.project_type != 'OPEX':
        raise Http404

    actor = request.user.profile

    if request.method == 'POST':
        action = request.POST.get('action', '')
        try:
            if action == 'assign':
                engineer = (UserProfile.objects.select_related('user')
                            .filter(pk=request.POST.get('engineer_id') or None).first())
                if engineer is None:
                    raise QaqcAssignmentRefused('Choose a site engineer to assign.')
                row = assign_site_qaqc(project, engineer, actor)
                messages.success(
                    request, f"{_person(row.engineer)} is now this site's QA/QC engineer.")
            elif action == 'end':
                row = end_site_qaqc(project, actor)
                messages.success(
                    request, f"{_person(row.engineer)} is no longer this site's QA/QC engineer.")
            else:
                raise QaqcAssignmentRefused('Unknown action.')
        except QaqcAssignmentRefused as exc:
            messages.error(request, str(exc))
        return redirect('site_qaqc', project_id=project.project_id)

    current = current_site_qaqc(project)
    # History newest first, every row including the active one; who and when for both
    # ends of each span.
    history = (SiteQaqcAssignment.objects.filter(project=project)
               .select_related('engineer__user', 'assigned_by__user', 'ended_by__user')
               .order_by('-assigned_at', '-pk'))
    return render(request, 'projects/site_qaqc.html', {
        'project':    project,
        'current':    current,
        'stale':      current is not None and engineer_is_stale(current.engineer),
        'eligible':   eligible_qaqc_engineers(project),
        'history':    history,
    })
