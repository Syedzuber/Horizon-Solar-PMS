"""Punch points are raised here, leave Open here, and the site-level open-points query
lives here. Closeout steps 2, 7 and 7b (docs/CLOSEOUT_SPEC.md: CL-1, CL-2, CL-B,
PP-1 to PP-6).

`raise_punch_point()` is the only code that creates a PunchPoint (closeout step 7,
go-ahead Q6). Two callers: `task_reject`, and `qaqc_views.submit_qaqc_visit()` once
per "No". tests_qaqc_visit pins that `PunchPoint.objects.create` appears nowhere else.

TWO SOURCES, TWO WAYS OUT (PP-1, PP-5). Where a point came from is read from the
existing `QaqcVisitAnswer.punch_point` link through `visit_raised_q()`, never stored.
    - Raised by a REJECTION: `close_punch_points_on_approval()` closes it when the task
      is resubmitted and approved (CL-1). It never enters the rectify loop.
    - Raised by a QA/QC VISIT: approval never closes it, and the task cannot be approved
      while it is Open or Rectified (`approval_blockers()`, PP-2). It leaves through the
      loop - `rectify_punch_point()` then `reverify_accept()` (or `reverify_reject()`
      back to Open) - each round kept as a PunchPointEvent.
    - Either source: the PM's waiver (`views.punch_point_waive`, its own write).

`close_punch_points_on_approval()` and `reverify_accept()` are the only runtime writers
of `PunchPoint.CLOSED` (migration 0111 wrote it once, for points that predate CL-1).
Re-completing an already-approved task through the status screens (Done -> Blocked ->
In Progress -> Done) is not an approval and closes nothing (DEFERRED G63: nor does it
ask PP-2's question).

`next_action()` is the one place that says, in words, who acts next on a point and
what they do (closeout 7c). Screens read it; none spells its own.

`open_punch_points_for_project()` is the CL-2 query. The COD and HOTO gates read it, and
must not spell their own. Rectified counts: it is a claim, not yet a re-verification.
"""
from typing import NamedTuple

from django.db import transaction
from django.db.models import Count, Prefetch, Q
from django.urls import reverse
from django.utils import timezone

from .models import (
    Project, PunchPoint, PunchPointEvent, SiteQaqcAssignment, UserProfile, log_activity,
)
from .notifications import send_notification
from .permissions import (
    QAQC_ENGINEER_ROLES, project_managers, site_has_effective_qaqc, site_qaqc_engineer_id,
    user_can_rectify_punch_point, user_can_reverify_punch_point,
)

IN_APP_AND_EMAIL = ['in_app', 'email']

# NotificationLog.template_name labels. Not Interakt templates: no WhatsApp channel.
T_RAISED_BY_VISIT  = 'punch_point_raised_by_visit'
T_RECTIFIED        = 'punch_point_rectified'
T_REVERIFY_REJECTED = 'punch_point_reverify_rejected'

APPROVAL_BLOCKED_TAIL = 'Rectify and re-verify it, or the PM waives it.'
NOT_VISIT_RAISED = ('This punch point was raised by a rejection. It closes when the task '
                    'is resubmitted and approved; it is not rectified point by point.')
RECTIFY_NOT_OPEN = ('This punch point is not open — it was already marked rectified, '
                    'closed or waived. Nothing was saved.')
REVERIFY_NOT_RECTIFIED = ('This punch point is not awaiting re-verification — it is open, '
                          'closed or waived. Nothing was saved.')
REVERIFY_OWN_ROUND = ('You marked this punch point rectified, so someone else must '
                      're-verify it. The PM can also waive it.')
RECTIFY_REMARK_REQUIRED = 'Say what was done to rectify it.'
RECTIFY_PHOTO_REQUIRED = 'A photo of the rectified work is required.'
ACCEPT_PHOTO_REQUIRED = 'A photo is required to accept the rectification.'
REJECT_REASON_REQUIRED = 'Say why the rectification is not accepted.'
NOT_ALLOWED_RECTIFY = ("Only the task's assignee, the PM or a coordinator can mark this "
                       "punch point rectified.")
NOT_ALLOWED_REVERIFY = ("Only the site's QA/QC engineer can re-verify this punch point "
                        "(the PM or a coordinator when no engineer can act).")


class PunchPointRefused(Exception):
    """A rule refused a rectify / re-verify. `str(exc)` is the user-facing message."""


# ---------------------------------------------------------------------------
# Source — where a point came from
# ---------------------------------------------------------------------------

def visit_raised_q(prefix=''):
    """Q selecting punch points raised by a QA/QC visit: those a QaqcVisitAnswer links to
    (`QaqcVisitAnswer.punch_point`, set by `submit_qaqc_visit()` on a "No", V-5).

    `prefix` is the ORM path from the filtered model to PunchPoint, with its trailing
    '__' (filtering Task: 'punch_points__').

    A POINT WITH NO QaqcVisitAnswer LINK IS TREATED AS REJECTION-RAISED and closes on
    approval (PP-1). Today `task_reject` is the only other creator, so that is exact. Any
    future non-visit source must decide its own closure rule explicitly - it must not
    inherit "closes on approval" by accident of having no visit link.

    The link is set in the same locked transaction that creates the point, so no other
    session ever sees a visit-raised point without it.
    """
    return Q(**{f'{prefix}qaqc_answer__isnull': False})


def rejection_raised_q(prefix=''):
    """The complement of `visit_raised_q()`: no QaqcVisitAnswer link (see its docstring)."""
    return Q(**{f'{prefix}qaqc_answer__isnull': True})


def source_answer(point):
    """The QaqcVisitAnswer that raised `point`, or None for a rejection-raised point.
    Free when the caller select_related('qaqc_answer'); one query otherwise.
    RelatedObjectDoesNotExist subclasses AttributeError, so getattr's default catches it."""
    return getattr(point, 'qaqc_answer', None)


# ---------------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------------

def raise_punch_point(task, reason, raised_by):
    """Create one Open punch point on `task` and return it.

    THE ONE CREATION PATH. A rejection raises one (`task_reject`, with the rejection
    reason) and a QA/QC visit raises one per "No" at submit (CL-B, V-5). Both callers
    run inside their own transaction, so a refused submit leaves no point behind.

    `reason` must be non-blank — the model says why. `raised_by` is the UserProfile
    who found the defect: the approver who rejected, or the QA/QC engineer.
    """
    if not (reason or '').strip():
        raise ValueError('raise_punch_point needs a reason')
    return PunchPoint.objects.create(task=task, reason=reason, raised_by=raised_by)


# ---------------------------------------------------------------------------
# Approval — PP-1 (closure) and PP-2 (the guard)
# ---------------------------------------------------------------------------

def close_punch_points_on_approval(task, actor, method, project):
    """Close every Open REJECTION-RAISED punch point on `task`, crediting `actor` at the
    approval time (CL-1 as amended by PP-1).

    Called INSIDE the approval's `transaction.atomic()` block, after the status
    function has accepted Done. A completion that is refused unwinds the closure with
    it, so a point is never Closed against an approval that did not happen.

    VISIT-RAISED POINTS ARE NEVER CLOSED HERE (PP-1). They close only by re-verification
    or waiver; the approval paths refuse while one is outstanding (`approval_blockers`),
    so by the time this runs none is Open or Rectified on the task anyway. The source
    term stays in the WHERE so this function does not rely on its callers' guard.

    `task.approved_at` must already be set on the instance (both callers set it
    before asking for Done). It is the closure time, so the ledger shows the point
    closing at the moment of approval rather than a few milliseconds after it.

    `method` is `PunchPoint.CLOSED_ON_APPROVAL` or `CLOSED_ON_SELF_CERTIFIED`.
    Waived and already-Closed points are left exactly as they are.

    Returns the number of points closed.
    """
    if task.approved_at is None:
        raise ValueError('close_punch_points_on_approval needs an approved task')

    # Race: `status=OPEN` is in the UPDATE's own WHERE, not checked beforehand, so a
    # PM waiving the same point at the same moment cannot be overwritten. Whichever
    # write commits first wins the row, and the second matches nothing. The CHECK
    # punchpoint_closure_columns_match_status refuses any mixed result.
    closed = (PunchPoint.objects
              .filter(task=task, status=PunchPoint.OPEN)
              .filter(rejection_raised_q())
              .update(
                  status=PunchPoint.CLOSED,
                  closed_by=actor,
                  closed_at=task.approved_at,
                  closure_method=method,
              ))

    if closed:
        label = dict(PunchPoint.CLOSURE_METHOD_CHOICES)[method]
        text = (f"{label}: {closed} punch point{'s' if closed != 1 else ''} "
                f"on {task.task_name}")[:255]
        # After commit, not inline. log_activity swallows its own errors, but on
        # Postgres a failed INSERT inside this atomic block would still poison the
        # transaction and roll the approval back. A lambda rather than
        # functools.partial, as in approval_notices.py.
        transaction.on_commit(
            lambda: log_activity(
                project, actor, text,
                entity_type='Task', entity_id=task.pk,
                action_code='punch_points_closed',
            )
        )
    return closed


def approval_blockers(task):
    """The visit-raised punch points that stop `task` being approved (PP-2): Open or
    Rectified, oldest first. Empty means approval may proceed as far as punch points go.

    The approval paths call this INSIDE their atomic block after locking the project row
    — the lock `submit_qaqc_visit()` takes before raising points — so a visit cannot add
    a point between this check and the approval committing.
    """
    return (PunchPoint.objects
            .filter(task=task, status__in=PunchPoint.OUTSTANDING)
            .filter(visit_raised_q())
            .order_by('created_at', 'pk'))


def approval_refusal_message(task, blockers):
    """The PP-2 refusal: names each point and its state, then the way out."""
    named = '; '.join(
        f"“{point.reason}” ({'rectified, awaiting re-verification' if point.status == PunchPoint.RECTIFIED else 'open'})"
        for point in blockers)
    count = len(blockers)
    return (f"'{task.task_name}' cannot be approved while {count} QA/QC punch "
            f"point{'s are' if count != 1 else ' is'} outstanding: {named}. "
            f"{APPROVAL_BLOCKED_TAIL}")


# ---------------------------------------------------------------------------
# Reads — CL-2 and the PP-6 screens. Every one is a fixed number of queries.
# ---------------------------------------------------------------------------

def open_punch_points_for_project(project):
    """Every outstanding punch point on `project`, across every task on the site (CL-2).

    THE ONE DEFINITION. The COD gate (closeout step 4) and the HOTO gate (step 8)
    both refuse while this is non-empty. Neither may filter it further or spell
    its own version.

    - Open and Rectified count (`PunchPoint.OUTSTANDING`). Rectified is a claim that has
      not been re-verified yet (PP-4), so it is still open as far as COD is concerned.
    - Closed and Waived never count.
    - Both sources count: a rejection-raised Open point blocks COD as before.
    - Every task counts, Not Applicable ones included (ruling Q3, 8 Oct 2026). A
      defect on work later found not applicable is still a recorded defect until the
      PM waives it.
    - There is no severity and no "blocking" field (CL-2). Every outstanding point blocks.

    Returns a queryset, oldest first, so a gate can say how many points there are
    and list them.
    """
    return (
        PunchPoint.objects
        .filter(task__phase__project=project, status__in=PunchPoint.OUTSTANDING)
        .select_related('task')
        .order_by('created_at', 'pk')
    )


def open_punch_point_counts(project_ids):
    """{project pk: number of outstanding punch points} for the PM dashboard's site cards
    (PP-6). The bulk form of `open_punch_points_for_project()` - the same two terms, so a
    card's number is the count COD would refuse on. Sites with none are absent.

    One grouped query for the whole dashboard, never one per card.
    """
    if not project_ids:
        return {}
    # order_by() clears Meta.ordering, which would otherwise join the GROUP BY.
    return dict(
        PunchPoint.objects
        .filter(task__phase__project_id__in=project_ids,
                status__in=PunchPoint.OUTSTANDING)
        .values('task__phase__project_id').annotate(n=Count('pk')).order_by()
        .values_list('task__phase__project_id', 'n'))


def open_punch_point_counts_by_task(task_ids):
    """{task pk: number of outstanding punch points} for the task-row badge (PP-6). Both
    sources, Open and Rectified - the same membership as CL-2. One grouped query."""
    if not task_ids:
        return {}
    return dict(
        PunchPoint.objects
        .filter(task_id__in=task_ids, status__in=PunchPoint.OUTSTANDING)
        .values('task_id').annotate(n=Count('pk')).order_by()
        .values_list('task_id', 'n'))


def latest_rectify_prefetch():
    """Each point's rectify rounds, newest first, as `point.rectify_rounds`, so the latest
    is `[0]`. One query for the whole list. Also what a batch caller of next_action()
    prefetches (the COD refusal, the approval panel)."""
    return Prefetch(
        'events',
        queryset=(PunchPointEvent.objects.filter(action=PunchPointEvent.RECTIFIED)
                  .select_related('actor__user').order_by('-acted_at', '-pk')),
        to_attr='rectify_rounds')


def open_points_on_my_tasks(profile):
    """The SE dashboard's "Open punch points on my tasks" (PP-6, go-ahead Q6): every
    outstanding VISIT-RAISED point on a task assigned to `profile`, on a live site, oldest
    first, with the task, site and the raising answer (for its photo) joined.

    Visit-raised only: a rejection-raised point is the rejected submission itself, which
    the engineer already acts on by resubmitting (PP-5). One query.
    """
    return list(
        PunchPoint.objects
        .filter(task__assigned_to=profile, status__in=PunchPoint.OUTSTANDING,
                task__phase__project__is_deleted=False)
        .filter(visit_raised_q())
        .select_related('task__phase__project', 'qaqc_answer__visit')
        .order_by('created_at', 'pk'))


def awaiting_reverification(project_ids):
    """The PM dashboard's "Awaiting re-verification" list (PP-6): every Rectified point on
    the given sites, oldest first, each with `rectify_rounds` (latest first, for who
    rectified and when) and `site_has_engineer` (whether the PM's Re-verify button can
    apply - only when no engineer can act, go-ahead Q4).

    Three queries whatever the length: the points, the prefetch, and the active
    effective assignments for the sites on the list.
    """
    if not project_ids:
        return []
    points = list(
        PunchPoint.objects
        .filter(task__phase__project_id__in=project_ids, status=PunchPoint.RECTIFIED,
                task__phase__project__is_deleted=False)
        .select_related('task__phase__project', 'qaqc_answer__visit')
        .prefetch_related(latest_rectify_prefetch())
        .order_by('created_at', 'pk'))
    if not points:
        return []
    # The bulk form of permissions.site_has_effective_qaqc(): the same three terms.
    with_engineer = set(
        SiteQaqcAssignment.objects
        .filter(project_id__in={p.task.phase.project_id for p in points},
                ended_at__isnull=True, engineer__role__in=QAQC_ENGINEER_ROLES,
                engineer__is_active=True)
        .values_list('project_id', flat=True))
    for point in points:
        point.site_has_engineer = point.task.phase.project_id in with_engineer
    return points


def task_punch_points(task):
    """Every punch point on `task` for the task page's card, oldest first: the people,
    the raising answer and its visit joined, and every round prefetched in order. Two
    queries (the prefetch fires only when there is at least one point)."""
    return list(
        PunchPoint.objects.filter(task=task)
        .select_related('raised_by__user', 'waived_by__user', 'closed_by__user',
                        'qaqc_answer__visit')
        .prefetch_related(Prefetch(
            'events',
            queryset=PunchPointEvent.objects.select_related('actor__user')
            .order_by('acted_at', 'pk'),
            to_attr='rounds')))


# ---------------------------------------------------------------------------
# Next action — who acts next on a point, in words (closeout 7c)
# ---------------------------------------------------------------------------

class NextAction(NamedTuple):
    """What `next_action()` answers. `line` is the sentence every screen shows; the
    other three are its parts, for a caller that lays them out itself."""
    state: str   # the point's status as shown: Open / Rectified / Closed / Waived
    who: str     # display name of whoever acts next; '' once nobody has to
    does: str    # what they do; '' once nobody has to
    line: str


WAIVER_TAIL = 'or the PM waives it'


def _name(profile):
    return profile.user.get_full_name() or profile.user.username


def effective_qaqc_engineer(project):
    """`project`'s QA/QC engineer who can act right now (UserProfile with user), or None.

    TWIN OF permissions.site_has_effective_qaqc(), which answers "is there one?" where
    this answers "who?". The same three terms - an active assignment, the engineer's role
    still in QAQC_ENGINEER_ROLES, their profile active. Edit both or neither: if they
    drift, next_action() names someone the re-verify writer refuses, or sends the PM to
    re-verify when the writer still wants the engineer. tests_punch_rectify
    EffectiveEngineerTwinTests pins them equal. One query; none for a non-OPEX site, as
    there.
    """
    if project is None or project.project_type != 'OPEX':
        return None
    assignment = (SiteQaqcAssignment.objects
                  .filter(project=project, ended_at__isnull=True,
                          engineer__role__in=QAQC_ENGINEER_ROLES, engineer__is_active=True)
                  .select_related('engineer__user').first())
    return assignment.engineer if assignment is not None else None


def _latest_rectify_round(point):
    """`point`'s latest RECTIFIED PunchPointEvent, or None. Reads whichever list the
    caller prefetched - `rounds` (oldest first, task_punch_points) or `rectify_rounds`
    (newest first, awaiting_reverification) - else one query."""
    rounds = getattr(point, 'rounds', None)
    if rounds is not None:
        return next((e for e in reversed(rounds)
                     if e.action == PunchPointEvent.RECTIFIED), None)
    rectify_rounds = getattr(point, 'rectify_rounds', None)
    if rectify_rounds is not None:
        return rectify_rounds[0] if rectify_rounds else None
    return (PunchPointEvent.objects
            .filter(punch_point=point, action=PunchPointEvent.RECTIFIED)
            .select_related('actor__user').order_by('-acted_at', '-pk').first())


_ASK = object()   # next_action(engineer=...) not supplied: look it up


def next_action(point, *, engineer=_ASK):
    """The point's state, who acts next and what they do (closeout 7c), as a NextAction.

    THE ONE DERIVATION. The COD refusal, the approval panel's blockers and any other
    screen naming a point's next step read this; no template spells its own.
        - rejection-raised Open: the assignee fixes and resubmits the task (PP-5);
        - visit-raised Open: waiting for the assignee to rectify (PP-3). Unassigned: the
          PM or a coordinator, who may also rectify;
        - Rectified with an effective QA/QC engineer: waiting for that engineer to
          re-verify (PP-4). If the engineer recorded the latest rectify round they may
          not re-verify it (Q5), and the PM may not while an engineer can act (Q4), so
          the line says so and only the waiver remains (ruling 12 Oct 2026);
        - Rectified with no effective engineer: waiting for the PM or a coordinator; one
          who recorded the round is named as excluded (Q5);
        - Closed / Waived: nothing more.
    Every outstanding line but that one ends "or the PM waives it" (the waiver is always
    open to PM-level authority).

    The words follow the writers' rules (reverify_refusal, user_can_rectify_punch_point,
    user_can_reverify_punch_point) without calling them: they ask about a given user,
    this asks who that user is.

    BATCH CALLERS add no query per point: select_related('qaqc_answer',
    'task__assigned_to__user'), prefetch the rectify rounds (`rounds` or
    `rectify_rounds`, as `_latest_rectify_round` reads), and pass `engineer` - the
    site's `effective_qaqc_engineer()`, asked once - for points on one site. Otherwise
    each of those is looked up for this point.
    """
    status = point.status
    if status not in PunchPoint.OUTSTANDING:
        return NextAction(status, '', '', status)
    assignee = point.task.assigned_to
    if source_answer(point) is None:
        # Rejection-raised: only Open is reachable; it closes on resubmit + approval.
        who = _name(assignee) if assignee is not None else 'whoever is assigned the task'
        does = 'fixes and resubmits the task'
        return NextAction(status, who, does, f'{status} — {who} {does}, {WAIVER_TAIL}')
    if status == PunchPoint.OPEN:
        who = _name(assignee) if assignee is not None else 'the PM or a coordinator'
        return NextAction(status, who, 'rectify',
                          f'{status} — waiting for {who} to rectify, {WAIVER_TAIL}')

    if engineer is _ASK:
        engineer = effective_qaqc_engineer(point.task.phase.project)
    latest = _latest_rectify_round(point)
    rectifier_id = latest.actor_id if latest is not None else None
    if engineer is not None:
        if rectifier_id == engineer.pk:
            who = _name(engineer)
            return NextAction(
                status, '', '',
                f"{status} — {who} recorded this rectification and may not re-verify "
                f"it; only the PM's waiver closes it")
        who = _name(engineer)
    elif latest is not None and rectifier_id != point.task.assigned_to_id:
        # Only the assignee, the PM or a coordinator can rectify, so a round recorded by
        # anyone but the assignee was a manager's, and that manager may not re-verify it.
        who = f'the PM or a coordinator other than {_name(latest.actor)}'
    else:
        who = 'the PM or a coordinator'
    return NextAction(status, who, 're-verify',
                      f'{status} — waiting for {who} to re-verify, {WAIVER_TAIL}')


def latest_rectifier_id(point):
    """The UserProfile pk who recorded `point`'s latest rectify round, or None. Reads
    `point.rounds` (oldest first) when the caller prefetched it, else one query."""
    rounds = getattr(point, 'rounds', None)
    if rounds is not None:
        for event in reversed(rounds):
            if event.action == PunchPointEvent.RECTIFIED:
                return event.actor_id
        return None
    return (PunchPointEvent.objects
            .filter(punch_point=point, action=PunchPointEvent.RECTIFIED)
            .order_by('-acted_at', '-pk')
            .values_list('actor_id', flat=True).first())


# ---------------------------------------------------------------------------
# Writers — the rectify / re-verify loop (PP-3, PP-4)
# ---------------------------------------------------------------------------

def _lock_point(point_pk, project):
    """The punch point `point_pk` on `project`, row-locked, with its task. Raises
    PunchPoint.DoesNotExist for a point on another site."""
    # of=('self',): lock the point only. Its task is read for the assignee, which no
    # concurrent writer here changes, and locking it would serialise unrelated task edits.
    return (PunchPoint.objects.select_for_update(of=('self',))
            .select_related('task')
            .get(pk=point_pk, task__phase__project=project))


def _is_visit_raised(point):
    # Asked of the database under the caller's lock rather than off the instance, so a
    # stale cached relation can never route a rejection-raised point into the loop.
    return PunchPoint.objects.filter(pk=point.pk).filter(visit_raised_q()).exists()


def _event(point, action, actor, remark, stored_photo, photo_taken_at, now):
    photo = stored_photo or {}
    return PunchPointEvent.objects.create(
        punch_point=point, action=action, actor=actor, acted_at=now, remark=remark,
        photo_file_name=photo.get('file_name', ''), photo_bucket=photo.get('bucket', ''),
        photo_path=photo.get('path', ''), photo_size_kb=photo.get('file_size_kb', 0),
        photo_taken_at=photo_taken_at if photo else None,
    )


def _log(project, actor, text, task_pk, action_code):
    """ActivityLog after commit, for the reason close_punch_points_on_approval gives."""
    text = text[:255]
    transaction.on_commit(
        lambda: log_activity(project, actor, text, entity_type='Task',
                             entity_id=task_pk, action_code=action_code),
        robust=True)


def rectify_refusal(point, user, project):
    """The reason `user` may not mark `point` Rectified now, or None. Shared by the
    screen (to offer the form) and the writer (under its lock). Order: source, then
    authority, then state."""
    if not _is_visit_raised(point):
        return NOT_VISIT_RAISED
    if not user_can_rectify_punch_point(user, point.task, project):
        return NOT_ALLOWED_RECTIFY
    if point.status != PunchPoint.OPEN:
        return RECTIFY_NOT_OPEN
    return None


def reverify_refusal(point, user, project):
    """The reason `user` may not re-verify `point` now, or None. Source, authority, state,
    then go-ahead Q5: whoever recorded the latest rectify round may not re-verify it - the
    same rule as task_approve's "nobody approves their own submission". The waiver stays."""
    if not _is_visit_raised(point):
        return NOT_VISIT_RAISED
    if not user_can_reverify_punch_point(user, project):
        return NOT_ALLOWED_REVERIFY
    if point.status != PunchPoint.RECTIFIED:
        return REVERIFY_NOT_RECTIFIED
    profile = getattr(user, 'profile', None)
    if profile is not None and latest_rectifier_id(point) == profile.pk:
        return REVERIFY_OWN_ROUND
    return None


def rectify_punch_point(point_pk, user, project, *, remark, stored_photo,
                        photo_taken_at=None):
    """Mark a visit-raised Open punch point Rectified (PP-3): a remark and a photo, a
    PunchPointEvent row, the status moved to Rectified. Re-checks source, authority and
    state under a row lock. Raises PunchPointRefused.

    Notifies the site's effective QA/QC engineer, else the PM and active coordinators
    minus the actor (go-ahead Q7), after commit.
    """
    remark = (remark or '').strip()
    if not remark:
        raise PunchPointRefused(RECTIFY_REMARK_REQUIRED)
    if not stored_photo:
        raise PunchPointRefused(RECTIFY_PHOTO_REQUIRED)
    profile = user.profile
    with transaction.atomic():
        point = _lock_point(point_pk, project)
        refusal = rectify_refusal(point, user, project)
        if refusal:
            raise PunchPointRefused(refusal)
        now = timezone.now()
        _event(point, PunchPointEvent.RECTIFIED, profile, remark, stored_photo,
               photo_taken_at, now)
        # Race: the row is locked and its status re-checked above; `status=OPEN` in the
        # UPDATE's WHERE as well means a waiver that somehow committed first leaves this
        # matching nothing, and the round row above unwinds with the refusal.
        if not PunchPoint.objects.filter(pk=point.pk, status=PunchPoint.OPEN).update(
                status=PunchPoint.RECTIFIED):
            raise PunchPointRefused(RECTIFY_NOT_OPEN)
        task = point.task
        _log(project, profile, f"Punch point marked rectified on {task.task_name}: {remark}",
             task.pk, 'punch_point_rectified')
        _queue_rectified_notice(point, task, project, profile)
    return point


def reverify_accept(point_pk, user, project, *, remark='', stored_photo,
                    photo_taken_at=None):
    """Accept a Rectified visit-raised punch point (PP-4): the verifier's photo, a
    PunchPointEvent row, the point Closed with method `re_verified`, closed by the
    verifier at the round's time. Raises PunchPointRefused."""
    if not stored_photo:
        raise PunchPointRefused(ACCEPT_PHOTO_REQUIRED)
    remark = (remark or '').strip()
    profile = user.profile
    with transaction.atomic():
        point = _lock_point(point_pk, project)
        refusal = reverify_refusal(point, user, project)
        if refusal:
            raise PunchPointRefused(refusal)
        now = timezone.now()
        _event(point, PunchPointEvent.REVERIFY_ACCEPTED, profile, remark, stored_photo,
               photo_taken_at, now)
        # Race: locked and re-checked above; `status=RECTIFIED` in the WHERE too, so a
        # waiver racing this cannot be overwritten with Closed. The closure CHECK holds
        # all three closure columns together.
        if not PunchPoint.objects.filter(pk=point.pk, status=PunchPoint.RECTIFIED).update(
                status=PunchPoint.CLOSED, closed_by=profile, closed_at=now,
                closure_method=PunchPoint.CLOSED_ON_REVERIFICATION):
            raise PunchPointRefused(REVERIFY_NOT_RECTIFIED)
        task = point.task
        _log(project, profile, f"Punch point re-verified and closed on {task.task_name}",
             task.pk, 'punch_point_reverified')
    return point


def reverify_reject(point_pk, user, project, *, remark, stored_photo=None,
                    photo_taken_at=None):
    """Reject a Rectified visit-raised punch point (PP-4): a reason (photo optional), a
    PunchPointEvent row, the point back to Open for another rectify round. Notifies the
    task's assignee after commit. Raises PunchPointRefused."""
    remark = (remark or '').strip()
    if not remark:
        raise PunchPointRefused(REJECT_REASON_REQUIRED)
    profile = user.profile
    with transaction.atomic():
        point = _lock_point(point_pk, project)
        refusal = reverify_refusal(point, user, project)
        if refusal:
            raise PunchPointRefused(refusal)
        now = timezone.now()
        _event(point, PunchPointEvent.REVERIFY_REJECTED, profile, remark, stored_photo,
               photo_taken_at, now)
        # Race: as in reverify_accept - `status=RECTIFIED` in the WHERE as well as the
        # locked re-check, so a racing waiver is never turned back into Open.
        if not PunchPoint.objects.filter(pk=point.pk, status=PunchPoint.RECTIFIED).update(
                status=PunchPoint.OPEN):
            raise PunchPointRefused(REVERIFY_NOT_RECTIFIED)
        task = point.task
        _log(project, profile,
             f"Punch point rectification rejected on {task.task_name}: {remark}",
             task.pk, 'punch_point_reverify_rejected')
        _queue_reverify_rejected_notice(point, task, project, profile, remark)
    return point


# ---------------------------------------------------------------------------
# Notices (PP-6) — in-app + email, after commit, re-read by pk
# ---------------------------------------------------------------------------

def _point_link(project_id, task_pk, point_pk=None):
    link = reverse('task_detail', args=[project_id, task_pk])
    return f'{link}#punch-point-{point_pk}' if point_pk else f'{link}#punchPointsBlock'


def _send_point_notices(recipient_pks, message, project_pk, actor_pk, task_pk, point_pk,
                        template):
    """One notice per recipient. A person deactivated on either model is not told: they
    cannot sign in to act on it."""
    project = Project.objects.filter(pk=project_pk).first()
    actor = UserProfile.objects.filter(pk=actor_pk).first()
    link = _point_link(project.project_id, task_pk, point_pk) if project else ''
    for recipient in (UserProfile.objects.select_related('user')
                      .filter(pk__in=recipient_pks, is_active=True, user__is_active=True)):
        send_notification(
            recipient, message, channels=IN_APP_AND_EMAIL, link=link, subject=message,
            template=template, related_project=project, actor=actor,
        )


def _on_commit_notice(recipient_pks, message, project_pk, actor_pk, task_pk, point_pk,
                      template):
    recipient_pks = [pk for pk in recipient_pks if pk is not None and pk != actor_pk]
    if not recipient_pks:
        return
    # A lambda, not functools.partial, as in approval_notices.py.
    transaction.on_commit(
        lambda: _send_point_notices(recipient_pks, message, project_pk, actor_pk,
                                    task_pk, point_pk, template),
        robust=True)


def _site_label(project):
    if project.customer_name and project.customer_name != project.project_id:
        return f"{project.project_id} ({project.customer_name})"
    return project.project_id


def queue_visit_raise_notices(points, visit, project, actor):
    """Go-ahead Q8: one notice per (assignee, task) for the points a submitted visit
    raised, linking to the task. Called by `qaqc_views.submit_qaqc_visit()` inside its
    transaction; sent after commit. An unassigned task tells nobody here - the PM and
    coordinators already get the visit's own notice (V-8)."""
    by_task = {}
    for point in points:
        by_task.setdefault(point.task_id, (point.task, []))[1].append(point)
    for task, task_points in by_task.values():
        if task.assigned_to_id is None:
            continue
        count = len(task_points)
        message = (f"QA/QC visit {visit.visit_date:%d %b %Y} raised {count} punch "
                   f"point{'s' if count != 1 else ''} on your task '{task.task_name}' at "
                   f"{_site_label(project)}. Rectify each with a remark and a photo.")
        _on_commit_notice([task.assigned_to_id], message, project.pk, actor.pk, task.pk,
                          task_points[0].pk if count == 1 else None, T_RAISED_BY_VISIT)


def _queue_rectified_notice(point, task, project, actor):
    """PP-6 / go-ahead Q7: the site's effective QA/QC engineer, else the PM and active
    coordinators, minus the actor."""
    if site_has_effective_qaqc(project):
        recipients = [site_qaqc_engineer_id(project)]
    else:
        recipients = [p.pk for p in project_managers(project)]
    message = (f"Punch point on '{task.task_name}' at {_site_label(project)} was marked "
               f"rectified and is awaiting re-verification.")
    _on_commit_notice(recipients, message, project.pk, actor.pk, task.pk, point.pk,
                      T_RECTIFIED)


def _queue_reverify_rejected_notice(point, task, project, actor, reason):
    message = (f"The rectification of a punch point on '{task.task_name}' at "
               f"{_site_label(project)} was not accepted: {reason}")[:500]
    _on_commit_notice([task.assigned_to_id], message, project.pk, actor.pk, task.pk,
                      point.pk, T_REVERIFY_REJECTED)
