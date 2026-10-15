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

QA/QC VISITS — closeout step 7 (CL-5, CL-B, CL-C; decisions V-1 to V-10). The assigned
engineer starts a dated visit, answers chosen checklist points (Yes / No / NA, a photo on
every answer, a remark on No) and submits; every "No" then raises a punch point on its
task. THE ONLY WRITERS of QaqcVisit and QaqcVisitAnswer: start_qaqc_visit(),
answer_qaqc_point(), submit_qaqc_visit(), and the lapse inside assign_site_qaqc() /
end_site_qaqc(). A visit never writes ChecklistItemCompletion (V-4).

PUNCH POINT RECTIFY / RE-VERIFY — closeout step 7b (PP-3, PP-4). The screens at the end of
this module take a visit-raised point's photo through the visit's own upload path (its own
`punch-point` segment) and hand it to the writers in punch_points.py, which hold the rules.
On submit, each task's assignee is told what the visit raised on their task (PP-6, Q8).
"""

import logging
import re
from datetime import datetime, timedelta

from django.contrib import messages
from django.db import IntegrityError, transaction
from django.db.models import Count, Max, Q
from django.http import Http404, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from .bill_storage import bill_pdf_url, bills_bucket
from .decorators import login_required
from .design_storage import DesignStorageError, _client, build_design_path
from .models import (
    Checklist, ChecklistItem, ChecklistItemCompletion, ChecklistTaskLink, Project,
    PunchPoint, PunchPointEvent, QaqcVisit, QaqcVisitAnswer, SiteQaqcAssignment, Task,
    UserProfile, log_activity,
)
from .notifications import send_notification
from .permissions import (
    project_managers, user_can_conduct_qaqc_visit, user_can_manage_project,
    user_can_rectify_punch_point, user_can_reverify_punch_point, user_can_view_project,
)
from .site_pm import refuse_site_view_404
from .punch_points import (
    ACCEPT_PHOTO_REQUIRED, RECTIFY_PHOTO_REQUIRED, RECTIFY_REMARK_REQUIRED,
    REJECT_REASON_REQUIRED, PunchPointRefused, queue_visit_raise_notices,
    raise_punch_point, rectify_punch_point, rectify_refusal, reverify_accept,
    reverify_refusal, reverify_reject,
)
from .views import (
    ALLOWED_PHOTO_EXTENSIONS, MIME_TYPE_MAP, _active_project, _checklist_for_task, _is_hx,
    _validate_upload_file,
)

logger = logging.getLogger(__name__)

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

    Three queries whatever the number of sites: the assignments, one grouped count, and
    the visits that decide the row's visit button (closeout step 7). "Awaiting approval"
    is Task.is_awaiting_approval as a filter — submitted, not yet approved — the queue
    this engineer is there to clear.

    Each row also carries `open_visit` (the site's open visit, shown "open since <date>"
    so a forgotten one is visible, Q3) and `visited_today` (a submitted visit dated
    today, so V-1's one-per-day rule is visible before the button refuses).
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
    # The open visit on each site (at most one, uniq_qaqc_visit_open_per_site) and any
    # visit submitted today; lapsed visits decide nothing about the button.
    visits = list(
        QaqcVisit.objects
        .filter(project_id__in=[a.project_id for a in assignments])
        .filter(Q(status=QaqcVisit.OPEN)
                | Q(status=QaqcVisit.SUBMITTED, visit_date=timezone.localdate()))
    )
    open_by_site = {v.project_id: v for v in visits if v.status == QaqcVisit.OPEN}
    visited_today = {v.project_id for v in visits if v.status == QaqcVisit.SUBMITTED}
    return [
        {'project': a.project, 'assigned_at': a.assigned_at,
         'awaiting_count': awaiting.get(a.project_id, 0),
         'open_visit': open_by_site.get(a.project_id),
         'visited_today': a.project_id in visited_today}
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
        lapsed = 0
        if current is not None:
            _end_row(current, actor, SiteQaqcAssignment.END_REPLACED, now)
            # Closeout step 7 (Q2): the outgoing engineer's open visit lapses in this
            # same transaction, so the new engineer can start one today.
            lapsed = _lapse_open_visits(locked, QaqcVisit.LAPSE_REPLACED, now)
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
    if lapsed:
        _log_lapse(project, actor, current.engineer, 'replaced')
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
        now = timezone.now()
        _end_row(current, actor, SiteQaqcAssignment.END_ENDED, now)
        # Closeout step 7 (Q2): their open visit lapses with the assignment.
        lapsed = _lapse_open_visits(locked, QaqcVisit.LAPSE_ENDED, now)
        _notify_removed(locked, current.engineer_id, actor.pk)

    log_activity(project, actor, f"Ended QA/QC engineer: {_person(current.engineer)}",
                 entity_type='SiteQaqcAssignment', entity_id=current.pk,
                 action_code='site_qaqc_ended')
    if lapsed:
        _log_lapse(project, actor, current.engineer, 'ended')
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
    # PM-11: a PM who was handed off this site gets the handover redirect on a GET
    # instead of the 404; the POST keeps the 404 (R4).
    if not user_can_view_project(request.user, project):
        return refuse_site_view_404(request, project)
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


# ===========================================================================
# QA/QC visits — closeout step 7 (CL-5, CL-B, CL-C; V-1 to V-10)
# ===========================================================================

# The storage path segment: {project_id}/qaqc-visit/{uuid}.{ext} in the bills bucket.
# Also the guard that keeps discard_unrecorded_visit_photo() off bill and COD PDFs.
VISIT_PHOTO_KIND = 'qaqc-visit'
# Closeout 7b: rectify / re-verify photos, same bucket, their own segment, so each
# discard helper can only ever reach its own kind of object.
PUNCH_PHOTO_KIND = 'punch-point'

# NotificationLog.template_name label (not an Interakt template: no WhatsApp channel).
T_VISIT_SUBMITTED = 'qaqc_visit_submitted'

VISIT_STORAGE_NOT_READY = ('QA/QC visits cannot be started yet: the private store for '
                           'visit photos is not configured.')
VISIT_STORAGE_OFF = ('Visit photos cannot be stored right now: the private store is not '
                     'configured. Nothing was saved.')
PUNCH_STORAGE_OFF = ('Punch point photos cannot be stored right now: the private store is '
                     'not configured. Nothing was saved.')
VISIT_NOT_YOURS = "Only this site's assigned QA/QC engineer can do that."
VISIT_NOT_OPEN = ('This visit is no longer open — it was submitted, or it lapsed when the '
                  'QA/QC engineer changed. Nothing was saved.')
VISIT_ONE_PER_DAY = ('This site has already had its QA/QC visit today. One visit per site '
                     'per day.')
VISIT_EMPTY = 'Check at least one point before submitting the visit.'
PHOTO_REQUIRED = 'A photo is required on every answer.'
REMARK_REQUIRED = 'Answering "No" requires a remark saying why.'
ANSWER_INVALID = 'Choose Yes, No or Not Applicable.'

# A camera clock that was never set reports 1970 or 2000; such a timestamp is not
# evidence of anything, so photo_taken_at is left NULL rather than stored.
PHOTO_TAKEN_EARLIEST_YEAR = 2020
EXIF_DATETIME = re.compile(r'^(\d{4})[:-](\d{2})[:-](\d{2})[ T](\d{2}):(\d{2}):(\d{2})')


class QaqcVisitRefused(Exception):
    """A rule refused a visit action. `str(exc)` is the user-facing message."""


# ---------------------------------------------------------------------------
# Points — which (task, checklist item) pairs a visit may check (V-2)
# ---------------------------------------------------------------------------

def resolve_checklists_for_tasks(tasks, project):
    """{task.pk: Checklist or None} — the checklist version each task is answering,
    resolved exactly as views._checklist_for_task() resolves one task, in at most three
    queries (links, pins, versions) whatever the number of tasks.

    THE BATCH TWIN OF _checklist_for_task() (go-ahead Q1). The rule is that function's,
    restated in bulk: the link by template-task code (project type on the join), else by
    task name; then the non-draft version of the link's family holding this task's
    completions, latest-answered first (checked_at, then pk); else the family's active
    version. EDIT BOTH OR NEITHER: tests_qaqc_visit.BatchResolverEquivalenceTests asserts
    the two agree for every task across pinned, spanning, no-active, by-name and draft
    cases. Writes never trust this: answer_qaqc_point() asks _checklist_for_task() for
    its one task.

    One deliberate difference: the by-name fallback is not logged here. The single
    resolver warns once per task-page view; this runs on every visit page and site card
    render, and the warning would repeat for every fallback task each time.

    `tasks` should carry template_task (select_related), or each code read is a query.
    """
    tasks = list(tasks)
    if not tasks:
        return {}
    ptype = project.project_type
    # Every link either path could pick, in the Meta ordering both of the single
    # resolver's .first() calls use, so setdefault() keeps the row .first() would.
    links = list(
        ChecklistTaskLink.objects
        .select_related('checklist', 'template_task__phase__template')
        .filter(Q(template_task__phase__template__project_type=ptype) | Q(project_type=ptype))
    )
    by_code, by_name = {}, {}
    for link in links:
        tt = link.template_task
        if tt is not None and tt.phase.template.project_type == ptype:
            by_code.setdefault(tt.code, link)
        if link.project_type == ptype:
            by_name.setdefault(link.task_name, link)
    link_for = {}
    for task in tasks:
        link = by_code.get(task.template_task.code) if task.template_task_id else None
        link_for[task.pk] = link or by_name.get(task.task_name)

    linked = [pk for pk, link in link_for.items() if link is not None]
    best = {}   # task pk -> (sort key, pinned checklist pk)
    if linked:
        # One row per (task, version) holding that task's completions, with the latest
        # checked_at and pk among them — the single resolver's annotate, grouped by task.
        # order_by() clears the Meta ordering, which would otherwise join the GROUP BY.
        rows = (ChecklistItemCompletion.objects
                .filter(task_id__in=linked, item__isnull=False)
                .exclude(item__checklist__status=Checklist.DRAFT)
                .values('task_id', 'item__checklist_id', 'item__checklist__code')
                .annotate(last_at=Max('checked_at'), last_pk=Max('pk'))
                .order_by())
        for row in rows:
            if row['item__checklist__code'] != link_for[row['task_id']].checklist.code:
                continue   # pinned within the linked family only (ruling Q3, §G40)
            # checked_at DESC NULLS LAST, then pk DESC: a dated answer beats an undated one.
            key = ((1, row['last_at'], row['last_pk']) if row['last_at'] is not None
                   else (0, 0, row['last_pk']))
            current = best.get(row['task_id'])
            if current is None or key > current[0]:
                best[row['task_id']] = (key, row['item__checklist_id'])

    pinned_pks = {checklist_pk for _key, checklist_pk in best.values()}
    need_active = {link_for[pk].checklist.code for pk in linked
                   if pk not in best and link_for[pk].checklist.status != Checklist.ACTIVE}
    by_pk, active_by_code = {}, {}
    if pinned_pks or need_active:
        for checklist in Checklist.objects.filter(
                Q(pk__in=pinned_pks) | Q(code__in=need_active, status=Checklist.ACTIVE)):
            by_pk[checklist.pk] = checklist
            if checklist.status == Checklist.ACTIVE:
                active_by_code[checklist.code] = checklist

    resolved = {}
    for task in tasks:
        link = link_for[task.pk]
        if link is None:
            resolved[task.pk] = None
        elif task.pk in best:
            resolved[task.pk] = by_pk[best[task.pk][1]]
        elif link.checklist.status == Checklist.ACTIVE:
            resolved[task.pk] = link.checklist     # the single resolver's fast path
        else:
            resolved[task.pk] = active_by_code.get(link.checklist.code)
    return resolved


def site_checklist_points(project):
    """The site's QA/QC points (V-2): one entry per task that has a checklist, in site
    order, as [{'task', 'checklist', 'items'}] — the items of the version that task
    resolves to (closeout 1a pinning, through resolve_checklists_for_tasks()).

    MIRRORS AND NOT APPLICABLE TASKS ARE LEFT OUT (pre-flight 5 ruling): a mirror holds
    no human work to check, and a punch point on one could never close by approval; a
    Not Applicable task's work was never done. An archived version is included whenever
    a task is pinned to it; a task whose family has no active version and no pin
    resolves to None and contributes nothing.

    At most five queries whatever the size of the site: the tasks, the three resolver
    queries, and the items. None at all on a non-OPEX site.
    """
    if project.project_type != 'OPEX':
        return []
    tasks = list(
        Task.objects
        .filter(phase__project=project, is_mirror=False, is_not_applicable=False)
        .select_related('template_task', 'phase')
        .order_by('phase__phase_order', 'task_order', 'pk')
    )
    resolved = resolve_checklists_for_tasks(tasks, project)
    checklist_pks = {c.pk for c in resolved.values() if c is not None}
    items_by_checklist = {}
    if checklist_pks:
        # ChecklistItem's Meta ordering (order, pk) is the checklist's own numbering.
        for item in ChecklistItem.objects.filter(checklist_id__in=checklist_pks):
            items_by_checklist.setdefault(item.checklist_id, []).append(item)
    entries = []
    for task in tasks:
        checklist = resolved.get(task.pk)
        items = items_by_checklist.get(checklist.pk, []) if checklist else []
        if items:
            entries.append({'task': task, 'checklist': checklist, 'items': items})
    return entries


def point_keys(points):
    """Every (task pk, item pk) in site_checklist_points()' result."""
    return {(entry['task'].pk, item.pk) for entry in points for item in entry['items']}


def site_coverage(project, points):
    """V-6: distinct points ever verified on a SUBMITTED visit ÷ the site's current
    points. One query.

    The numerator is intersected with the current points, so a point that left the site
    (its task became Not Applicable, or an unanswered task moved to a new checklist
    version) leaves both sides together and coverage never passes 100%. Open and lapsed
    visits verify nothing.
    """
    keys = point_keys(points)
    verified = set()
    if keys:
        # order_by() clears QaqcVisitAnswer's Meta ordering, which would put pk into the
        # SELECT DISTINCT and count every answer instead of every point.
        verified = set(
            QaqcVisitAnswer.objects
            .filter(visit__project=project, visit__status=QaqcVisit.SUBMITTED)
            .values_list('task_id', 'item_id').distinct().order_by()
        ) & keys
    total = len(keys)
    return {
        'verified':      len(verified),
        'total':         total,
        'percent':       (len(verified) * 100) // total if total else 0,
        'verified_keys': verified,
    }


def site_has_submitted_qaqc_visit(project):
    """CL-C readiness (V-9): True once the site has at least one SUBMITTED QA/QC visit.

    THE ONE DEFINITION. Step 8's HOTO gate calls this and must not spell its own. Open
    and lapsed visits never count; there is no coverage threshold (CL-C).
    """
    return QaqcVisit.objects.filter(project=project, status=QaqcVisit.SUBMITTED).exists()


# ---------------------------------------------------------------------------
# Who reads and who answers a visit (V-1, V-7)
# ---------------------------------------------------------------------------

def user_can_read_visit(user, visit, project):
    """V-7. The caller has already confirmed `user` can see the site.

    A submitted visit, photos included, is read by every viewer of the site. An open or
    lapsed one only by its engineer and the site's managers: it is work in progress, or
    work that was never finished, and neither is a finding yet.
    """
    if visit.status == QaqcVisit.SUBMITTED:
        return True
    profile = getattr(user, 'profile', None)
    if profile is not None and visit.engineer_id == profile.pk:
        return True
    return user_can_manage_project(user, project)


def user_can_answer_visit(user, visit, project):
    """True if `user` may answer and submit `visit` now: it is open, it is their own,
    and they are still the site's QA/QC engineer (user_can_conduct_qaqc_visit). The
    writers ask the same three terms again under their locks."""
    profile = getattr(user, 'profile', None)
    return (visit.status == QaqcVisit.OPEN and profile is not None
            and visit.engineer_id == profile.pk
            and user_can_conduct_qaqc_visit(user, project))


def _visit_counts(visit_qs):
    """Annotate each visit with its points checked and its "No" answers."""
    return visit_qs.annotate(
        points_checked=Count('answers'),
        no_count=Count('answers', filter=Q(answers__answer=QaqcVisitAnswer.NO)),
    )


def qaqc_visit_card_context(project, user):
    """Context for the "QA/QC visits" card on project_overview. OPEX only: on any other
    site the card is not drawn (`show_qaqc_visit_card` False) and no query is spent.

    Every viewer: coverage, the last submitted visit and the five most recent. The open
    visit: its engineer and the site's managers (V-7), shown "open since <date>" (Q3).
    The Start / Continue button: the assigned engineer only. About ten queries, fixed.
    """
    if project.project_type != 'OPEX':
        return {'show_qaqc_visit_card': False}
    profile = getattr(user, 'profile', None)
    submitted = list(_visit_counts(
        QaqcVisit.objects.filter(project=project, status=QaqcVisit.SUBMITTED)
        .select_related('engineer__user').order_by('-visit_date', '-pk'))[:5])
    open_visit = (QaqcVisit.objects.filter(project=project, status=QaqcVisit.OPEN)
                  .select_related('engineer__user').first())
    if open_visit is not None and not user_can_read_visit(user, open_visit, project):
        open_visit = None
    points = site_checklist_points(project)
    today = timezone.localdate()
    return {
        'show_qaqc_visit_card':  True,
        'qaqc_visits_recent':    submitted,
        'qaqc_visit_last':       submitted[0] if submitted else None,
        'qaqc_visit_open':       open_visit,
        'qaqc_visit_coverage':   site_coverage(project, points),
        'qaqc_can_conduct':      user_can_conduct_qaqc_visit(user, project),
        'qaqc_open_is_mine':     (open_visit is not None and profile is not None
                                  and open_visit.engineer_id == profile.pk),
        'qaqc_visited_today':    any(v.visit_date == today for v in submitted),
        'qaqc_storage_ready':    bool(bills_bucket()),
    }


def last_visit_lines(projects, today):
    """V-10 for the PM dashboard: {project pk: days since the last submitted visit, or
    None for "No QA/QC visit yet"} for each OPEX project in `projects` that has an
    active QA/QC assignment or a submitted visit. Projects with neither are absent and
    draw no line.

    Two queries for the whole dashboard, never one per card; none without an OPEX site.
    """
    pks = [p.pk for p in projects if p.project_type == 'OPEX']
    if not pks:
        return {}
    assigned = set(SiteQaqcAssignment.objects
                   .filter(project_id__in=pks, ended_at__isnull=True)
                   .values_list('project_id', flat=True))
    # order_by() clears the Meta ordering, which would otherwise join the GROUP BY.
    last = dict(QaqcVisit.objects
                .filter(project_id__in=pks, status=QaqcVisit.SUBMITTED)
                .values('project_id').annotate(last=Max('visit_date')).order_by()
                .values_list('project_id', 'last'))
    lines = {}
    for pk in pks:
        if pk in last:
            lines[pk] = (today - last[pk]).days
        elif pk in assigned:
            lines[pk] = None
    return lines


# ---------------------------------------------------------------------------
# Visit photos — private bucket (go-ahead Q4), the COD pattern
# ---------------------------------------------------------------------------

def validate_visit_photo(file_obj):
    """The checklist photo rules (views._validate_upload_file with the photo allow-list:
    jpg / jpeg / png, the 20 MB ceiling, MIME matching the extension), plus non-empty.
    Raises QaqcVisitRefused; touches no storage."""
    if not (getattr(file_obj, 'size', 0) or 0):
        raise QaqcVisitRefused('Photo rejected: the file is empty.')
    try:
        _validate_upload_file(file_obj, allowed_extensions=ALLOWED_PHOTO_EXTENSIONS)
    except ValueError as exc:
        raise QaqcVisitRefused(f'Photo rejected: {exc}')


def upload_visit_photo(file_obj, project, kind=VISIT_PHOTO_KIND, storage_off=VISIT_STORAGE_OFF):
    """Store one visit photo in the private bills bucket under the `kind` segment —
    qaqc-visit for a visit answer, punch-point for a rectify / re-verify round (7b).

    Returns {file_name, bucket, path, file_size_kb}; never a URL. Raises
    QaqcVisitRefused when storage is off (with `storage_off` as the message), the photo
    is refused, or the upload fails, so the caller writes no row pointing at an object
    that does not exist.
    """
    bucket = bills_bucket()
    if not bucket:
        raise QaqcVisitRefused(storage_off)
    validate_visit_photo(file_obj)
    ext = file_obj.name.rsplit('.', 1)[-1].lower()
    path = build_design_path(project.project_id, kind, file_obj.name)
    try:
        file_obj.seek(0)
        payload = file_obj.read()
    except Exception:
        raise QaqcVisitRefused('The photo could not be read.')
    try:
        _client().storage.from_(bucket).upload(
            path=path, file=payload,
            file_options={'content-type': MIME_TYPE_MAP.get(ext, 'application/octet-stream')})
    except DesignStorageError as exc:
        raise QaqcVisitRefused(str(exc))
    except Exception as exc:
        logger.error('QA/QC visit photo upload failed — %s', exc)
        raise QaqcVisitRefused(f'Photo upload failed: {type(exc).__name__}. Nothing was saved.')
    return {'file_name': file_obj.name, 'bucket': bucket, 'path': path,
            'file_size_kb': max(1, file_obj.size // 1024)}


def discard_unrecorded_visit_photo(stored):
    """Remove a photo no answer records: one stored for an answer the writer then
    refused, or the one an edit replaced (Q9). Refuses anything an answer records,
    anything outside the bills bucket, and anything not under the qaqc-visit segment, so
    a bill or COD PDF in the same bucket is never reachable from here. Never raises."""
    bucket, path = (stored or {}).get('bucket'), (stored or {}).get('path')
    if not bucket or not path or bucket != bills_bucket():
        return False
    if f'/{VISIT_PHOTO_KIND}/' not in path:
        return False
    if QaqcVisitAnswer.objects.filter(photo_bucket=bucket, photo_path=path).exists():
        return False
    try:
        _client().storage.from_(bucket).remove([path])
        return True
    except Exception as exc:
        logger.error('QA/QC visit photo cleanup failed for %s/%s — %s', bucket, path, exc)
        return False


def discard_unrecorded_punch_photo(stored):
    """The punch-point twin of discard_unrecorded_visit_photo(): remove a rectify /
    re-verify photo the writer then refused. Refuses anything a PunchPointEvent records,
    anything outside the bills bucket, and anything not under the punch-point segment,
    so neither a visit photo nor a bill or COD PDF is reachable from here. Never raises."""
    bucket, path = (stored or {}).get('bucket'), (stored or {}).get('path')
    if not bucket or not path or bucket != bills_bucket():
        return False
    if f'/{PUNCH_PHOTO_KIND}/' not in path:
        return False
    if PunchPointEvent.objects.filter(photo_bucket=bucket, photo_path=path).exists():
        return False
    try:
        _client().storage.from_(bucket).remove([path])
        return True
    except Exception as exc:
        logger.error('Punch point photo cleanup failed for %s/%s — %s', bucket, path, exc)
        return False


def parse_photo_taken_at(raw):
    """The photo's EXIF DateTimeOriginal as the page sent it, or None (go-ahead Q5).

    Never refuses: a photo with no readable timestamp is still a photo, and the answer
    stands on answered_at. EXIF writes local camera time with no zone ("2026:10:08
    14:32:10"); it is read as IST, the site's clock. A time before 2020 (an unset camera
    clock) or more than a day ahead is not evidence of anything and is dropped.
    """
    match = EXIF_DATETIME.match((raw or '').strip())
    if not match:
        return None
    try:
        taken = timezone.make_aware(datetime(*(int(part) for part in match.groups())))
    except (ValueError, OverflowError):
        return None
    if taken.year < PHOTO_TAKEN_EARLIEST_YEAR or taken > timezone.now() + timedelta(days=1):
        return None
    return taken


# ---------------------------------------------------------------------------
# Writers — the only code that creates or moves a QaqcVisit or QaqcVisitAnswer
# ---------------------------------------------------------------------------

def _lapse_open_visits(project, reason, now):
    """Lapse the site's open visit, if any (Q2). Called by assign_site_qaqc() and
    end_site_qaqc() inside their transaction. Returns the number lapsed (0 or 1)."""
    # Race: the caller holds the project row lock, which start_qaqc_visit() and
    # submit_qaqc_visit() also take, so no visit here can be started or submitted
    # meanwhile. An answer being saved holds the visit row; this UPDATE waits for it,
    # and status=OPEN in the WHERE then lapses the visit with that answer kept.
    return (QaqcVisit.objects.filter(project=project, status=QaqcVisit.OPEN)
            .update(status=QaqcVisit.LAPSED, lapsed_at=now, lapse_reason=reason))


def _log_lapse(project, actor, engineer, why):
    log_activity(project, actor,
                 f"QA/QC visit by {_person(engineer)} lapsed (engineer {why})",
                 entity_type='QaqcVisit', action_code='qaqc_visit_lapsed')


def _log_after_commit(project, actor, action, visit_pk, action_code):
    """The ActivityLog line, run once the writer commits — a refused write logs nothing.
    Re-reads by pk so the callback holds no ORM object from the writer's transaction."""
    project_pk, actor_pk = project.pk, actor.pk

    def log():
        log_activity(Project.objects.filter(pk=project_pk).first(),
                     UserProfile.objects.filter(pk=actor_pk).first(), action[:255],
                     entity_type='QaqcVisit', entity_id=visit_pk, action_code=action_code)

    transaction.on_commit(log, robust=True)


def start_qaqc_visit(project, user):
    """Start today's QA/QC visit on `project` (V-1). Returns the new open visit.

    Refused when the private photo store is not configured (Q4: a visit that cannot take
    a photo cannot take an answer), when `user` is not the site's assigned QA/QC
    engineer, when the site already has an open visit (Q3), or when it has had a visit
    today (V-1). Atomic under the project row lock.
    """
    if not bills_bucket():
        raise QaqcVisitRefused(VISIT_STORAGE_NOT_READY)
    profile = user.profile
    today = timezone.localdate()
    with transaction.atomic():
        # Serialises two starts on one site, and a start racing the PM replacing or
        # ending the engineer (those writers lock the same row): the second waits here
        # and re-checks everything below.
        locked = Project.objects.select_for_update().get(pk=project.pk)
        if not user_can_conduct_qaqc_visit(user, locked):
            raise QaqcVisitRefused(VISIT_NOT_YOURS)
        open_visit = QaqcVisit.objects.filter(project=locked, status=QaqcVisit.OPEN).first()
        if open_visit is not None:
            raise QaqcVisitRefused(
                f"This site already has an open visit, started "
                f"{open_visit.visit_date:%d %b %Y}. Continue it and submit it first.")
        if (QaqcVisit.objects.filter(project=locked, visit_date=today)
                .exclude(status=QaqcVisit.LAPSED).exists()):
            raise QaqcVisitRefused(VISIT_ONE_PER_DAY)
        # user_can_conduct_qaqc_visit() above has just confirmed this row names `user`.
        assignment = SiteQaqcAssignment.objects.get(project=locked, ended_at__isnull=True)
        try:
            # A savepoint, so a backstop constraint's refusal leaves the outer
            # transaction usable for the clean rollback below.
            with transaction.atomic():
                visit = QaqcVisit.objects.create(
                    project=locked, engineer=profile, assignment=assignment,
                    visit_date=today)
        except IntegrityError:
            # uniq_qaqc_visit_site_day / _open_per_site: only if the lock was bypassed.
            raise QaqcVisitRefused(VISIT_ONE_PER_DAY)
        _log_after_commit(locked, profile, f"Started QA/QC visit {today:%d %b %Y}",
                          visit.pk, 'qaqc_visit_started')
    return visit


def _refuse_unless_answerable(visit, user, project):
    if visit.status != QaqcVisit.OPEN:
        raise QaqcVisitRefused(VISIT_NOT_OPEN)
    if not user_can_answer_visit(user, visit, project):
        raise QaqcVisitRefused(VISIT_NOT_YOURS)


def point_refusal(project, task, item):
    """Raise QaqcVisitRefused unless (task, item) is a point on `project` now (V-2).

    Asks views._checklist_for_task() itself, never the batch twin, so a write is always
    checked by the resolver every other checklist write uses. A stale page posting an
    item from another version of the family is refused with the reason; an item from an
    unrelated checklist is a forged id and is a 404. Nothing is written either way."""
    if task.phase.project_id != project.pk:
        raise Http404
    if task.is_mirror or task.is_not_applicable:
        raise QaqcVisitRefused(
            f"'{task.task_name}' is not checked on a QA/QC visit "
            f"({'a derived task' if task.is_mirror else 'marked Not Applicable'}).")
    checklist = _checklist_for_task(task, project)
    if checklist is None:
        raise QaqcVisitRefused(f"'{task.task_name}' has no checklist to check.")
    if item.checklist_id != checklist.pk:
        if item.checklist.code == checklist.code:
            raise QaqcVisitRefused(
                f"That point is from version {item.checklist.version_no} of the checklist, "
                f"but this task is now answering version {checklist.version_no}. Nothing "
                f"was recorded; reload the visit.")
        raise Http404


def answer_qaqc_point(visit, user, project, task, item, *, answer, remark, stored_photo,
                      photo_taken_at):
    """Record or edit the answer to one point (task, item) on an open visit (V-3).

    `stored_photo` is upload_visit_photo()'s result, or None to keep the existing photo
    on an edit (Q9); a first answer without one is refused. The item's label is
    snapshotted on every write. Returns (answer row, the replaced photo or None) — the
    caller discards the replaced photo once this has committed. Raises QaqcVisitRefused;
    the caller then discards `stored_photo`.
    """
    remark = (remark or '').strip()
    if answer not in dict(QaqcVisitAnswer.ANSWER_CHOICES):
        raise QaqcVisitRefused(ANSWER_INVALID)
    if answer == QaqcVisitAnswer.NO and not remark:
        raise QaqcVisitRefused(REMARK_REQUIRED)
    with transaction.atomic():
        # Locks the visit row only (no select_related: joined rows would be locked too):
        # serialises two saves of one point, and a save racing the submit or a lapse,
        # both of which update this row — after either, the re-check below refuses.
        locked = QaqcVisit.objects.select_for_update().get(pk=visit.pk)
        _refuse_unless_answerable(locked, user, project)
        point_refusal(project, task, item)
        row = QaqcVisitAnswer.objects.filter(visit=locked, task=task, item=item).first()
        replaced = None
        if row is None:
            if stored_photo is None:
                raise QaqcVisitRefused(PHOTO_REQUIRED)
            row = QaqcVisitAnswer(visit=locked, task=task, item=item)
        elif stored_photo is not None:
            replaced = {'bucket': row.photo_bucket, 'path': row.photo_path}
        row.answer = answer
        row.remark = remark
        row.item_text_snapshot = item.label
        row.answered_at = timezone.now()
        if stored_photo is not None:
            row.photo_file_name = stored_photo['file_name']
            row.photo_bucket = stored_photo['bucket']
            row.photo_path = stored_photo['path']
            row.photo_size_kb = stored_photo['file_size_kb']
            row.photo_taken_at = photo_taken_at
        row.save()
    return row, replaced


def _punch_reason(visit, answer):
    """V-5: the item text and the remark, dated by the visit."""
    return (f"QA/QC visit {visit.visit_date:%d %b %Y} — {answer.item_text_snapshot}: "
            f"{answer.remark}")


def _plural(count, word):
    return f"{count} {word}{'' if count == 1 else 's'}"


def _send_visit_notices(recipient_pks, message, project_pk, actor_pk, visit_pk):
    """One in-app + email notice per recipient, after commit. Re-reads by pk; a person
    deactivated on either model is not told — they cannot sign in to read the visit."""
    project = Project.objects.filter(pk=project_pk).first()
    actor = UserProfile.objects.filter(pk=actor_pk).first()
    link = (reverse('qaqc_visit_detail', args=[project.project_id, visit_pk])
            if project else '')
    for recipient in (UserProfile.objects.select_related('user')
                      .filter(pk__in=recipient_pks, is_active=True, user__is_active=True)):
        send_notification(
            recipient, message, channels=IN_APP_AND_EMAIL, link=link, subject=message,
            template=T_VISIT_SUBMITTED, related_project=project, actor=actor,
        )


def submit_qaqc_visit(visit, user, project):
    """Submit an open visit (V-5, V-8). Every "No" raises one punch point on its task,
    raised by the engineer, through punch_points.raise_punch_point(); Yes and NA raise
    none. Refused for a visit with no answers (Q7).

    Returns {'points_checked', 'no_count', 'punch_points'}. The ActivityLog line and the
    notices to the site's PM and coordinators (never the engineer) run after commit.
    """
    profile = user.profile
    with transaction.atomic():
        # Project first, then the visit. record_cod() reads the open punch points under
        # this same project lock, so a COD cannot be recorded between this visit
        # raising its points and committing them (CL-2).
        locked_project = Project.objects.select_for_update().get(pk=project.pk)
        locked = QaqcVisit.objects.select_for_update().get(pk=visit.pk)
        _refuse_unless_answerable(locked, user, locked_project)
        answers = list(QaqcVisitAnswer.objects.filter(visit=locked)
                       .select_related('task').order_by('pk'))
        if not answers:
            raise QaqcVisitRefused(VISIT_EMPTY)
        raised_points = []
        for answer in answers:
            if answer.answer != QaqcVisitAnswer.NO:
                continue
            point = raise_punch_point(answer.task, _punch_reason(locked, answer), profile)
            # Race: the visit row is locked and still open, so no other submit is linking
            # this answer; punch_point__isnull=True keeps a replay from re-pointing it.
            QaqcVisitAnswer.objects.filter(pk=answer.pk, punch_point__isnull=True).update(
                punch_point=point)
            raised_points.append(point)
        raised = len(raised_points)
        # Race: status=OPEN in the UPDATE's own WHERE as well as the check above; the
        # locks make this a formality, and a 0 is refused rather than half-submitted.
        updated = (QaqcVisit.objects.filter(pk=locked.pk, status=QaqcVisit.OPEN)
                   .update(status=QaqcVisit.SUBMITTED, submitted_at=timezone.now()))
        if updated != 1:
            raise QaqcVisitRefused(VISIT_NOT_OPEN)

        checked = len(answers)
        _log_after_commit(
            locked_project, profile,
            f"Submitted QA/QC visit {locked.visit_date:%d %b %Y}: "
            f"{_plural(checked, 'point')} checked, {_plural(raised, 'punch point')} raised",
            locked.pk, 'qaqc_visit_submitted')
        message = (f"QA/QC visit submitted for {_site_label(locked_project)} by "
                   f"{_person(profile)}: {_plural(checked, 'point')} checked, {raised} No, "
                   f"{_plural(raised, 'punch point')} raised.")
        # V-8: the site's PM and active coordinators, never the engineer who submitted.
        recipient_pks = [p.pk for p in project_managers(locked_project) if p.pk != profile.pk]
        project_pk, actor_pk, visit_pk = locked_project.pk, profile.pk, locked.pk
        # A lambda, not functools.partial, as in approval_notices.py.
        transaction.on_commit(
            lambda: _send_visit_notices(recipient_pks, message, project_pk, actor_pk,
                                        visit_pk),
            robust=True)
        # PP-6 / go-ahead Q8: each task's assignee, once per task, after commit.
        queue_visit_raise_notices(raised_points, locked, locked_project, profile)
    return {'points_checked': checked, 'no_count': raised, 'punch_points': raised}


# ---------------------------------------------------------------------------
# Visit screens
# ---------------------------------------------------------------------------

def _visit_site(request, project_id):
    """Resolve the site for a visit screen, or raise Http404. A site you cannot see does
    not exist for you (the 0.2 shape), and visits exist on RESCO (OPEX) sites only, so
    on Residential and CAPEX sites every visit URL is a 404."""
    project = _active_project(project_id)
    if not user_can_view_project(request.user, project):
        raise Http404
    if project.project_type != 'OPEX':
        raise Http404
    return project


def _readable_visit(request, project, visit_pk):
    """The visit `visit_pk` on `project` if `request.user` may read it (V-7), else 404 —
    an open visit is not acknowledged to someone who may not see it."""
    visit = (QaqcVisit.objects.select_related('engineer__user')
             .filter(pk=visit_pk, project=project).first())
    if visit is None or not user_can_read_visit(request.user, visit, project):
        raise Http404
    return visit


def _visit_page_context(request, project, visit):
    """The visit page. The engineer answering it sees every point on the site, task by
    task; everyone else, and every visit that is no longer open, sees the answered
    points only. The site engineer's own checklist answer is never shown (Q8): the
    check is independent, and the dossier compares the two."""
    can_answer = user_can_answer_visit(request.user, visit, project)
    # Every answer on this visit, keyed by point, with the punch point a "No" raised.
    answers = {(a.task_id, a.item_id): a
               for a in visit.answers.select_related('punch_point', 'task')}
    points = site_checklist_points(project)
    coverage = site_coverage(project, points)
    groups, current_keys = [], set()
    for entry in points:
        rows = []
        for number, item in enumerate(entry['items'], start=1):
            key = (entry['task'].pk, item.pk)
            current_keys.add(key)
            answer = answers.get(key)
            if answer is None and not can_answer:
                continue
            rows.append({'item': item, 'number': number, 'answer': answer,
                         'verified_before': key in coverage['verified_keys']})
        if rows:
            groups.append({'task': entry['task'], 'checklist': entry['checklist'],
                           'rows': rows, 'point_count': len(entry['items']),
                           'answered': sum(1 for r in rows if r['answer'])})
    # Answers whose point has left the site since (version drift, Not Applicable): kept
    # and shown read-only, never offered again (pre-flight 5).
    stale = [a for key, a in answers.items() if key not in current_keys]
    return {
        'project':        project,
        'visit':          visit,
        'can_answer':     can_answer,
        'groups':         groups,
        'stale_answers':  stale,
        'answered_count': len(answers),
        'no_count':       sum(1 for a in answers.values() if a.answer == QaqcVisitAnswer.NO),
        'coverage':       coverage,
    }


@login_required
def qaqc_visit_start(request, project_id):
    """
    Start today's QA/QC visit on an OPEX site and open it (V-1). POST only.

    Access: the site's assigned QA/QC engineer (user_can_conduct_qaqc_visit). 404 for
    anyone who cannot see the site and on Residential / CAPEX sites; 403 for any other
    viewer.
    """
    project = _visit_site(request, project_id)
    card = reverse('project_overview', args=[project.project_id]) + '#qaqc-visits-card'
    if request.method != 'POST':
        return redirect(card)
    # Only the assigned QA/QC engineer starts a visit (V-1): it is the independent second
    # check (CL-5), so the PM, coordinators and the site's own engineers are refused.
    if not user_can_conduct_qaqc_visit(request.user, project):
        return HttpResponseForbidden()
    try:
        visit = start_qaqc_visit(project, request.user)
    except QaqcVisitRefused as exc:
        messages.error(request, str(exc))
        return redirect(card)
    messages.success(request, f"QA/QC visit started for {project.project_id}.")
    return redirect('qaqc_visit_detail', project_id=project.project_id, visit_pk=visit.pk)


@login_required
def qaqc_visit_detail(request, project_id, visit_pk):
    """
    One QA/QC visit. For its engineer while it is open: every point on the site, task by
    task, each answered in place (HTMX). For everyone else, and once it is submitted or
    lapsed: the answered points read-only, with their photos.

    Access (V-7): a submitted visit — every viewer of the site; an open or lapsed visit
    — its engineer and the site's PM and coordinators. 404 otherwise, and on
    Residential / CAPEX sites.
    """
    project = _visit_site(request, project_id)
    visit = _readable_visit(request, project, visit_pk)
    return render(request, 'projects/qaqc_visit.html',
                  _visit_page_context(request, project, visit))


def _visit_header_counts(visit):
    """The visit header's "N points checked, M No": every answer on the visit, stale
    points included, as _visit_page_context() counts them. One query."""
    counts = visit.answers.aggregate(
        answered=Count('pk'), no=Count('pk', filter=Q(answer=QaqcVisitAnswer.NO)))
    return counts['answered'], counts['no']


def _task_point_count(visit, project, task):
    """{'answered', 'point_count'} for `task`'s group on the visit page, or None when the
    page draws no group for it (a mirror, Not Applicable, or no checklist). The answered
    count is limited to the version the task resolves to now, as the page's group is."""
    if task.is_mirror or task.is_not_applicable:
        return None
    checklist = _checklist_for_task(task, project)
    if checklist is None:
        return None
    point_count = checklist.items.count()
    if not point_count:
        return None
    return {'answered': QaqcVisitAnswer.objects.filter(visit=visit, task=task,
                                                       item__checklist=checklist).count(),
            'point_count': point_count}


def _point_response(request, project, visit, task, item, *, form, error=''):
    """Render one point's row (partials/_qaqc_point.html): its form when `form`, else
    its current state. Reached only by the visit's engineer, so the row's Edit / Check
    buttons are always drawn.

    On a POST (saved, edited or refused) the response also carries the header's counts
    and this task's "N / M" as out-of-band swaps: the row swap alone left them at their
    page-load values until the page was reloaded."""
    answer = QaqcVisitAnswer.objects.filter(visit=visit, task=task, item=item).first()
    # What the form shows pre-filled: what was just posted when it was refused, else the
    # stored answer when editing. Decided here so the template needs no default filter
    # over a nullable `answer`.
    if error and request.method == 'POST':
        selected, remark = request.POST.get('answer', ''), request.POST.get('remark', '')
    elif answer is not None:
        selected, remark = answer.answer, answer.remark
    else:
        selected, remark = '', ''
    context = {
        'project': project, 'visit': visit, 'task': task, 'item': item,
        'answer': answer, 'show_form': form, 'error': error, 'can_answer': True,
        'selected_answer': selected, 'remark_value': remark,
    }
    if request.method != 'POST':
        return render(request, 'projects/partials/_qaqc_point.html', context)
    context['answered_count'], context['no_count'] = _visit_header_counts(visit)
    context['task_count'] = _task_point_count(visit, project, task)
    return render(request, 'projects/partials/_qaqc_point_response.html', context)


@login_required
def qaqc_visit_point(request, project_id, visit_pk, task_id, item_id):
    """
    One point on an open visit. GET: the answer form for that row (HTMX), or the row
    itself with ?row=1 (Cancel). POST: record or edit the answer — Yes / No / NA, a
    remark (required on No), a photo (required on a first answer; an edit keeps the
    existing one unless a new one is sent, Q9) and the photo's EXIF time if the page
    read one (Q5).

    Order on POST: validate the form → check the point → upload the photo →
    answer_qaqc_point() (lock, re-check, write) → if it refuses, delete the photo just
    uploaded; if an edit replaced a photo, delete the old one.

    Access: the visit's own engineer, while it is open and they are still the site's
    QA/QC engineer (user_can_answer_visit). 404 for a reader who may not see the visit;
    403 for one who may read but not answer it.
    """
    project = _visit_site(request, project_id)
    visit = _readable_visit(request, project, visit_pk)
    # Only the visit's engineer answers it (V-1, V-3); a manager reading an open visit
    # is refused, as is the engineer once the visit is submitted or lapsed.
    if not user_can_answer_visit(request.user, visit, project):
        return HttpResponseForbidden()
    task = get_object_or_404(Task.objects.select_related('phase'),
                             pk=task_id, phase__project=project)
    item = get_object_or_404(ChecklistItem.objects.select_related('checklist'), pk=item_id)

    if request.method != 'POST':
        try:
            point_refusal(project, task, item)
        except QaqcVisitRefused as exc:
            return _point_response(request, project, visit, task, item, form=False,
                                   error=str(exc))
        return _point_response(request, project, visit, task, item,
                               form=request.GET.get('row') != '1')

    def refused(message):
        if _is_hx(request):
            return _point_response(request, project, visit, task, item, form=True,
                                   error=message)
        messages.error(request, message)
        return redirect(reverse('qaqc_visit_detail', args=[project.project_id, visit.pk])
                        + f'#pt-{task.pk}-{item.pk}')

    answer = (request.POST.get('answer') or '').strip().lower()
    remark = (request.POST.get('remark') or '').strip()
    photo = request.FILES.get('photo')
    has_answer = QaqcVisitAnswer.objects.filter(visit=visit, task=task, item=item).exists()
    # Everything that can be refused is refused BEFORE the upload, so a refused answer
    # never leaves an orphan photo in the bucket. The writer asks again under its lock.
    if answer not in dict(QaqcVisitAnswer.ANSWER_CHOICES):
        return refused(ANSWER_INVALID)
    if answer == QaqcVisitAnswer.NO and not remark:
        return refused(REMARK_REQUIRED)
    if photo is None and not has_answer:
        return refused(PHOTO_REQUIRED)
    try:
        point_refusal(project, task, item)
        if photo is not None:
            validate_visit_photo(photo)
    except QaqcVisitRefused as exc:
        return refused(str(exc))

    stored = None
    try:
        if photo is not None:
            stored = upload_visit_photo(photo, project)
        _row, replaced = answer_qaqc_point(
            visit, request.user, project, task, item, answer=answer, remark=remark,
            stored_photo=stored,
            photo_taken_at=parse_photo_taken_at(request.POST.get('photo_taken_at')))
    except QaqcVisitRefused as exc:
        discard_unrecorded_visit_photo(stored)
        return refused(str(exc))
    if replaced:
        discard_unrecorded_visit_photo(replaced)

    if _is_hx(request):
        return _point_response(request, project, visit, task, item, form=False)
    messages.success(request, 'Answer recorded.')
    return redirect(reverse('qaqc_visit_detail', args=[project.project_id, visit.pk])
                    + f'#pt-{task.pk}-{item.pk}')


@login_required
def qaqc_visit_submit(request, project_id, visit_pk):
    """
    Submit an open visit. GET: the summary to confirm — points checked, and every "No"
    with the task it will raise a punch point on. POST: submit (V-5, V-8).

    Access: the visit's own engineer while it is open (user_can_answer_visit). 404 for a
    reader who may not see the visit; 403 for one who may read but not submit it —
    except the visit's own engineer POSTing once it is no longer open, who gets
    VISIT_NOT_OPEN as a message and is sent back to the visit.
    """
    project = _visit_site(request, project_id)
    visit = _readable_visit(request, project, visit_pk)
    back = reverse('qaqc_visit_detail', args=[project.project_id, visit.pk])
    # Only the visit's engineer submits it; the PM reads it and decides nothing here.
    if not user_can_answer_visit(request.user, visit, project):
        # The one readable refusal: the visit's own engineer POSTing a visit that is no
        # longer open — a double-click, or Back and resubmit, after it was submitted (or
        # lapsed). They can still view it (_readable_visit above 404s otherwise), so they
        # are told why and sent to it. Everyone else, and every GET, keeps the bare 403.
        profile = getattr(request.user, 'profile', None)
        if (request.method == 'POST' and visit.status != QaqcVisit.OPEN
                and profile is not None and visit.engineer_id == profile.pk):
            messages.error(request, VISIT_NOT_OPEN)
            return redirect(back)
        return HttpResponseForbidden()
    if request.method == 'POST':
        try:
            summary = submit_qaqc_visit(visit, request.user, project)
        except QaqcVisitRefused as exc:
            messages.error(request, str(exc))
            return redirect(back)
        messages.success(
            request,
            f"Visit submitted: {_plural(summary['points_checked'], 'point')} checked, "
            f"{_plural(summary['punch_points'], 'punch point')} raised.")
        return redirect(back)
    answers = list(visit.answers.select_related('task').order_by('pk'))
    return render(request, 'projects/qaqc_visit_submit.html', {
        'project':    project,
        'visit':      visit,
        'answers':    answers,
        'no_answers': [a for a in answers if a.answer == QaqcVisitAnswer.NO],
    })


@login_required
def qaqc_visit_photo(request, project_id, answer_pk):
    """
    One visit answer's photo: a redirect to a link signed now (bill_storage's signer and
    expiry), the COD pattern. The page signs nothing; each <img> asks here.

    Access: whoever may read the answer's visit (V-7). 404 for anyone else, on
    Residential / CAPEX sites, for an answer that is not this site's, and when no link
    can be signed — an image tag cannot follow a redirect to a page with a message.
    """
    project = _visit_site(request, project_id)
    answer = (QaqcVisitAnswer.objects.select_related('visit')
              .filter(pk=answer_pk, visit__project=project).first())
    if answer is None or not user_can_read_visit(request.user, answer.visit, project):
        raise Http404
    url = bill_pdf_url(answer.photo_bucket, answer.photo_path)
    if url is None:
        raise Http404
    return redirect(url)


# ===========================================================================
# Punch point rectify / re-verify — closeout step 7b (PP-3, PP-4)
#
# The screens for the loop. The writers live in punch_points.py and re-check source,
# authority and state under a row lock; these views refuse what they can BEFORE the
# photo upload (so a refused act leaves no orphan object) and delete the photo if the
# writer refuses anyway. The forms are on the task page's punch points card.
# ===========================================================================

def _point_site(request, project_id):
    """The site for a punch point screen, or 404 (the 0.2 shape). OPEX only: a
    visit-raised point exists nowhere else, so every loop URL on Residential / CAPEX is
    a 404 rather than a refusal message."""
    project = _active_project(project_id)
    if not user_can_view_project(request.user, project):
        raise Http404
    if project.project_type != 'OPEX':
        raise Http404
    return project


def _point_back(project, point):
    return redirect(reverse('task_detail', args=[project.project_id, point.task_id])
                    + f'#punch-point-{point.pk}')


def _stored_round_photo(request, project):
    """Validate and upload the round's photo, if one was sent. Returns the stored dict or
    None. Raises QaqcVisitRefused on a refused or failed upload."""
    photo = request.FILES.get('photo')
    if photo is None:
        return None
    validate_visit_photo(photo)
    return upload_visit_photo(photo, project, kind=PUNCH_PHOTO_KIND,
                              storage_off=PUNCH_STORAGE_OFF)


@login_required
def punch_point_rectify(request, project_id, punch_point_id):
    """
    Mark a QA/QC-raised punch point Rectified: a remark and a photo (PP-3). POST only;
    the form is on the task page.

    Access: the task's assignee, the PM or a coordinator — never the site's QA/QC
    engineer (permissions.user_can_rectify_punch_point). 404 for anyone who cannot see
    the site and on Residential / CAPEX; 403 for any other viewer. A rejection-raised
    point is refused with a message (PP-5).
    """
    project = _point_site(request, project_id)
    point = get_object_or_404(PunchPoint.objects.select_related('task'),
                              pk=punch_point_id, task__phase__project=project)
    if request.method != 'POST':
        return _point_back(project, point)
    # The assignee, PM or a coordinator claim the fix; the QA/QC engineer who checks it
    # and every other viewer (other SEs, SCM, Finance) are refused outright.
    if not user_can_rectify_punch_point(request.user, point.task, project):
        return HttpResponseForbidden()

    remark = (request.POST.get('remark') or '').strip()
    # Everything refusable is refused before the upload, so a refusal leaves no photo
    # behind. The writer asks every one of these again under its lock.
    refusal = rectify_refusal(point, request.user, project)
    if refusal is None and not remark:
        refusal = RECTIFY_REMARK_REQUIRED
    if refusal is None and request.FILES.get('photo') is None:
        refusal = RECTIFY_PHOTO_REQUIRED
    if refusal:
        messages.error(request, refusal)
        return _point_back(project, point)

    stored = None
    try:
        stored = _stored_round_photo(request, project)
        rectify_punch_point(
            point.pk, request.user, project, remark=remark, stored_photo=stored,
            photo_taken_at=parse_photo_taken_at(request.POST.get('photo_taken_at')))
    except (QaqcVisitRefused, PunchPointRefused) as exc:
        discard_unrecorded_punch_photo(stored)
        messages.error(request, str(exc))
        return _point_back(project, point)
    messages.success(request, f"Punch point on '{point.task.task_name}' marked rectified. "
                              f"It now waits for re-verification.")
    return _point_back(project, point)


@login_required
def punch_point_reverify(request, project_id, punch_point_id):
    """
    Re-verify a Rectified QA/QC punch point (PP-4). POST `decision`:
    `accept` (photo required) closes it as re-verified; `reject` (reason required) sends
    it back to Open. POST only; the form is on the task page.

    Access: the site's effective QA/QC engineer; the PM or a coordinator only when no
    engineer can act (permissions.user_can_reverify_punch_point). Whoever recorded the
    latest rectify round is refused that round (go-ahead Q5). 404 for anyone who cannot
    see the site and on Residential / CAPEX; 403 for any other viewer.
    """
    project = _point_site(request, project_id)
    point = get_object_or_404(PunchPoint.objects.select_related('task'),
                              pk=punch_point_id, task__phase__project=project)
    if request.method != 'POST':
        return _point_back(project, point)
    # The site's QA/QC engineer re-verifies; managers only stand in when no engineer can
    # act. Everyone else — including legacy is_qaqc holders — is refused.
    if not user_can_reverify_punch_point(request.user, project):
        return HttpResponseForbidden()

    decision = (request.POST.get('decision') or '').strip()
    remark = (request.POST.get('remark') or '').strip()
    refusal = reverify_refusal(point, request.user, project)
    if refusal is None and decision not in ('accept', 'reject'):
        refusal = 'Choose Accept or Reject.'
    if refusal is None and decision == 'accept' and request.FILES.get('photo') is None:
        refusal = ACCEPT_PHOTO_REQUIRED
    if refusal is None and decision == 'reject' and not remark:
        refusal = REJECT_REASON_REQUIRED
    if refusal:
        messages.error(request, refusal)
        return _point_back(project, point)

    stored = None
    taken = parse_photo_taken_at(request.POST.get('photo_taken_at'))
    try:
        stored = _stored_round_photo(request, project)
        writer = reverify_accept if decision == 'accept' else reverify_reject
        writer(point.pk, request.user, project, remark=remark, stored_photo=stored,
               photo_taken_at=taken)
    except (QaqcVisitRefused, PunchPointRefused) as exc:
        discard_unrecorded_punch_photo(stored)
        messages.error(request, str(exc))
        return _point_back(project, point)
    if decision == 'accept':
        messages.success(request, f"Punch point on '{point.task.task_name}' re-verified "
                                  f"and closed.")
    else:
        messages.warning(request, f"Rectification not accepted — the punch point on "
                                  f"'{point.task.task_name}' is open again.")
    return _point_back(project, point)


@login_required
def punch_point_event_photo(request, project_id, event_pk):
    """
    One rectify / re-verify round's photo: a redirect to a link signed now (the visit
    photo pattern). Access: every viewer of the site — the punch points card, rounds
    included, is shown to all of them. 404 otherwise, on Residential / CAPEX, for a
    round on another site, for a round with no photo, and when no link can be signed.
    """
    project = _point_site(request, project_id)
    event = (PunchPointEvent.objects
             .filter(pk=event_pk, punch_point__task__phase__project=project).first())
    if event is None or not event.photo_path:
        raise Http404
    url = bill_pdf_url(event.photo_bucket, event.photo_path)
    if url is None:
        raise Http404
    return redirect(url)
