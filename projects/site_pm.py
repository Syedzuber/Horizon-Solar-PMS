"""The one writer of `Project.assigned_pm` (ruling PM-2).

Six places put a PM on a site — project_create, create_opex_site (single and bulk),
zoho_deal_closed_webhook, admin_assign_pm, subadmin_projects and the bulk handover
page (admin_pm_handover) — and every one of them calls change_site_pm(), the last two
of them through hand_over_sites(). ProjectAdmin shows `assigned_pm` read-only. The only
other write left is the seed command seed_opex_test_data, named in the source check in
tests_site_pm.py; anything else that writes the column fails that test.

What a change does, in one transaction (PM-1 to PM-5):
  - refuses a soft-deleted or Cancelled site, on every path (PM-1, Q6)
  - refuses when the seat no longer holds the PM the caller expected (PM-2 Q3)
  - closes the site's open SitePmAssignment row and opens the next (PM-3)
  - writes `assigned_pm` (filter().update(), under the row lock)
  - moves the outgoing PM's open tasks on that site to the incoming PM (PM-4)
  - moves their pending contractor-bill PM steps on that site (PM-5)
then logs ONE `pm_assigned` ActivityLog line in its own savepoint.

change_site_pm() sends nothing. A CHANGE made from a screen goes through
hand_over_sites(), which wraps any number of sites in one all-or-nothing transaction
(PM-13) and, after the commit, sends one summary to each PM (PM-6) and, when material
steps still name the outgoing PM, one in-app notice to SCM (Q6). A FIRST assignment
keeps the notices its caller always sent (admin_assign_pm's assign_project, Q8).

It also holds the one exception to "a site you cannot see does not exist for you"
(PM-11): refuse_site_view_404() / refuse_site_view_403(), at the bottom.
"""
import logging
from dataclasses import dataclass, field

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Count, F, OuterRef, Q, Subquery, Value
from django.db.models.functions import Coalesce
from django.http import Http404, HttpResponseForbidden
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone

from projects.approvals import ApprovalRefused, hand_over_pm_step
from projects.decorators import get_user_dashboard
from projects.models import (
    APPROVAL_CHANGES_REQUESTED, APPROVAL_KIND_CONTRACTOR_BILL, APPROVAL_MATERIAL_KINDS,
    APPROVAL_OPEN, APPROVAL_PARTY_PM, APPROVAL_STEP_PENDING,
    ApprovalStep, Project, SiteGroupMembership, SitePmAssignment, Task, UserProfile,
    log_activity,
)
from projects.notifications import send_notification
from projects.permissions import can_change_site_pm, user_can_view_project
from projects.utils import assign_tasks_to

logger = logging.getLogger(__name__)

IN_APP_AND_EMAIL = ['in_app', 'email']


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


def with_outgoing_pm_bill_step_count(projects_qs):
    """Annotate each project with `outgoing_pm_bill_step_count`: how many contractor-bill
    PM steps change_site_pm() would move away from its current PM right now (PM-5) — the
    M in "N open tasks and M bill approval steps will move". A correlated subquery for
    the same reason as with_outgoing_pm_task_count(); the same predicate as
    bill_pm_steps_held_by()."""
    held = (ApprovalStep.objects
            .filter(party=APPROVAL_PARTY_PM, assignee=OuterRef('assigned_pm'),
                    verdict=APPROVAL_STEP_PENDING,
                    request__kind=APPROVAL_KIND_CONTRACTOR_BILL,
                    request__status=APPROVAL_OPEN,
                    request__bill_detail__project=OuterRef('pk'),
                    round=F('request__current_round'))
            .order_by()
            .values('request__bill_detail__project')
            .annotate(n=Count('pk'))
            .values('n'))
    return projects_qs.annotate(outgoing_pm_bill_step_count=Coalesce(Subquery(held), Value(0)))


@dataclass(frozen=True)
class SitePmChange:
    """What one change_site_pm() call did.

    `changed` is False for the no-op (the site already had `new_pm`). `bill_steps_waiting`
    counts the moved bill steps still waiting on the Site Engineer: they moved, but nobody
    is asked to act on them yet (after_decision announces them when their turn comes).
    """
    changed: bool
    old_pm_id: object = None
    tasks_moved: int = 0
    bill_steps_moved: int = 0
    bill_steps_waiting: int = 0


def change_site_pm(project, new_pm, by, *, expected_outgoing_pm, reason='',
                   first_assignment_only=False):
    """Make `new_pm` the PM of `project`. Returns a SitePmChange.

    Args:
        project: the site. Re-read under a row lock here, so a stale instance is fine;
                 on success its in-memory `assigned_pm` is updated to match.
        new_pm:  the incoming PM's UserProfile. Its role is NOT checked here — the
                 callers each look the PM up with role='PM' already, except the Zoho
                 webhook, whose missing role filter is DEFERRED; refusing a non-PM here
                 would change that webhook's behaviour before its own fix lands.
        by:      the acting UserProfile, or None for the webhook (no human).
        expected_outgoing_pm: REQUIRED, keyword only (PM-2 pre-flight Q3) — the PM the
                 caller believes holds the seat now (a UserProfile), or None for "the
                 seat is empty". Compared under the lock: if the seat holds anyone else,
                 the change is refused. A page loaded before somebody else changed the
                 PM must never silently take the site from a third PM.
        reason:  required when the site already has a PM; optional on a first assignment.
        first_assignment_only: refuse if the site already has a PM — subadmin_projects,
                 whose screen only ever fills an empty seat. Checked under the lock, so
                 two System Admins racing for the same empty seat cannot both win.

    Raises:
        PermissionDenied     a change (the site already has a PM) by anyone who is not
                             Admin, or with no actor at all.
        SitePmChangeRefused  deleted or Cancelled site, an occupied seat with
                             first_assignment_only, a seat that no longer holds
                             `expected_outgoing_pm`, a missing reason on a change, or a
                             bill step the approval rules will not move.

    Assigning the PM the site already has is a no-op: no history row, no log line.
    Callers inside their own transaction.atomic() (project_create, create_opex_site,
    hand_over_sites) get a savepoint here; the webhook calls it OUTSIDE its creation block
    on purpose, so a failure here never rolls back the project (ruling C1).
    """
    reason = (reason or '').strip()
    expected_id = expected_outgoing_pm.pk if expected_outgoing_pm is not None else None

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
        if old_pm_id is not None and first_assignment_only:
            raise SitePmChangeRefused(
                f'{site.project_id} already has a PM assigned. '
                f'Reassignment is not permitted here.')
        if old_pm_id != expected_id:
            raise SitePmChangeRefused(
                f"{site.project_id}'s PM is now {_name_or_none(old_pm_id)}, not "
                f"{_name_or_none(expected_id)} — it changed after this page was loaded.")
        if old_pm_id == new_pm.pk:
            project.assigned_pm = new_pm
            return SitePmChange(changed=False, old_pm_id=old_pm_id)

        if old_pm_id is not None:
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

        moved = steps_moved = steps_waiting = 0
        old_pm = None
        if old_pm_id is not None:
            # PM-4: the outgoing PM's open work follows the seat. Done tasks never move —
            # they record who did the work. Silent, set-based (assign_tasks_to).
            moved = assign_tasks_to(open_tasks_held_by(site, old_pm_id), new_pm)
            # PM-5: so do their pending contractor-bill PM steps on this site, through the
            # approvals chokepoint's handover entry — inside this block, so a step the
            # approval rules refuse rolls the whole change back. Lock order is project row
            # (above), then each request row; no approvals write ever locks a project
            # row, so the two orders cannot deadlock. No per-step notice (Q2): the
            # screens send one summary per PM (send_handover_notices).
            old_pm = UserProfile.objects.select_related('user').get(pk=old_pm_id)
            for step in bill_pm_steps_held_by([site], old_pm_id):
                try:
                    replacement = hand_over_pm_step(step, old_pm, new_pm, by, reason)
                except ApprovalRefused as exc:
                    raise SitePmChangeRefused(
                        f'{site.project_id}: bill approval #{step.request_id} cannot move '
                        f'— {exc}') from exc
                if replacement is not None:
                    steps_moved += 1
                    steps_waiting += replacement.activated_at is None

    project.assigned_pm = new_pm

    # One line per change, the counts in it — not one line per moved task or step (the
    # design_bulk_assigned precedent). Its own savepoint (Q4): log_activity swallows its
    # own failures, but inside a caller's transaction (hand_over_sites) a failed INSERT
    # would otherwise leave Postgres refusing every later statement and lose the whole
    # handover; the savepoint rolls back the log line alone.
    if old_pm_id is None:
        action = f"PM assigned: {_name(new_pm)}"
    else:
        action = (f"PM changed from {_name(old_pm)} to {_name(new_pm)} — "
                  f"{_count(moved, 'open task')} moved, "
                  f"{_count(steps_moved, 'bill approval step')} moved. Reason: {reason}")
    with transaction.atomic():
        log_activity(project, by, action, entity_type='Project', entity_id=project.pk,
                     action_code='pm_assigned')
    return SitePmChange(changed=True, old_pm_id=old_pm_id, tasks_moved=moved,
                        bill_steps_moved=steps_moved, bill_steps_waiting=steps_waiting)


# ---------------------------------------------------------------------------
# PM-2 — handing sites over (rulings PM-5, PM-6, PM-12, PM-13)
#
# hand_over_sites() is the one path for a CHANGE made from a screen: the single-site
# Change PM on admin_project_list (N = 1, ruling Q8) and the bulk handover page both call
# it, so both send the same PM-6 pair and list the same things the change leaves behind.
# ---------------------------------------------------------------------------

@dataclass
class Handover:
    """One completed handover, for the result page and the PM-6 notices."""
    actor: object
    from_pm: object
    to_pm: object
    program: object
    reason: str
    sites: list
    tasks_moved: int = 0
    bill_steps_moved: int = 0
    bill_steps_waiting: int = 0
    material_steps: list = field(default_factory=list)   # [(ApprovalStep, tag)] — not moved
    returning_bills: list = field(default_factory=list)  # [ApprovalRequest] — not moved


def bill_pm_steps_held_by(sites, pm_id):
    """The bill steps a change of PM moves (PM-5): on an OPEN contractor bill raised
    against one of `sites`, the PM party's PENDING step of the request's CURRENT round,
    assigned to `pm_id` — activated, or still waiting on the Site Engineer.

    Not moved, on purpose: a carried step (created approved, so never pending); a PM step
    SCM gave to some other PM; and a bill waiting for changes, which has no live step
    (returning_bills_for() lists those instead).
    """
    return (ApprovalStep.objects
            .filter(party=APPROVAL_PARTY_PM, assignee_id=pm_id,
                    verdict=APPROVAL_STEP_PENDING,
                    request__kind=APPROVAL_KIND_CONTRACTOR_BILL,
                    request__status=APPROVAL_OPEN,
                    request__bill_detail__project__in=sites,
                    round=F('request__current_round'))
            .order_by('request_id', 'pk'))


def bill_step_counts(sites, pm_id):
    """{site pk: (steps that would move, of which still waiting on the Site Engineer)} for
    the handover preview — one grouped query over every site, the same predicate as
    bill_pm_steps_held_by()."""
    rows = (bill_pm_steps_held_by(sites, pm_id).order_by()
            .values('request__bill_detail__project')
            .annotate(n=Count('pk'), waiting=Count('pk', filter=Q(activated_at__isnull=True))))
    return {r['request__bill_detail__project']: (r['n'], r['waiting']) for r in rows}


def material_steps_held_by(pm_id, sites, program=None):
    """Every pending material-approval PM step assigned to `pm_id` (ruling Q6). These do
    NOT move with the PM (PM-5: free-form scope, D-A2; SCM chose the PM, D-A8); they are
    listed so SCM can reassign them.

    ALL of them, not only those about the handed-over sites: a material request's scope
    is record-only and often empty, so "about these sites" cannot be decided reliably.
    Each is TAGGED when its scope names one of `sites`, `program`, or a site group with
    one of `sites` as a live member. Returns [(step, tag or '')], oldest request first.
    At most five queries, whatever the count.
    """
    steps = list(ApprovalStep.objects
                 .filter(party=APPROVAL_PARTY_PM, assignee_id=pm_id,
                         verdict=APPROVAL_STEP_PENDING,
                         request__kind__in=APPROVAL_MATERIAL_KINDS,
                         request__status=APPROVAL_OPEN,
                         round=F('request__current_round'))
                 .select_related('request')
                 .prefetch_related('request__projects', 'request__programs',
                                   'request__site_groups')
                 .order_by('request__raised_at', 'pk'))
    if not steps:
        return []
    site_codes = {s.pk: s.project_id for s in sites}
    group_ids = {g.pk for s in steps for g in s.request.site_groups.all()}
    # A group "contains" a handed-over site only through a LIVE membership: a site
    # removed from the group no longer makes the group's request about it.
    groups_with_site = set(SiteGroupMembership.objects
                           .filter(group_id__in=group_ids, project_id__in=site_codes,
                                   removed_at__isnull=True)
                           .values_list('group_id', flat=True)) if group_ids else set()
    tagged = []
    for step in steps:
        request = step.request
        named = sorted(site_codes[p.pk] for p in request.projects.all() if p.pk in site_codes)
        on_program = program is not None and any(
            g.pk == program.pk for g in request.programs.all())
        groups = sorted(g.name for g in request.site_groups.all() if g.pk in groups_with_site)
        if named:
            tag = f"names {', '.join(named)}"
        elif on_program:
            tag = f'names {program.name}'
        elif groups:
            tag = f"group {', '.join(groups)} holds a handed-over site"
        else:
            tag = ''
        tagged.append((step, tag))
    return tagged


def returning_bills_for(pm_id, sites):
    """Contractor bills on `sites` waiting for changes whose last PM step names `pm_id`
    (ruling Q7). They have no live step, so nothing moves; but a resubmit gives each
    party to whoever held it last (resubmit_approval_request), so round N+1 asks the
    outgoing PM unless SCM changes the approver. Returns the ApprovalRequests, oldest
    first. One query."""
    last = {}
    for step in (ApprovalStep.objects
                 .filter(party=APPROVAL_PARTY_PM,
                         request__kind=APPROVAL_KIND_CONTRACTOR_BILL,
                         request__status=APPROVAL_CHANGES_REQUESTED,
                         request__bill_detail__project__in=sites,
                         round=F('request__current_round'))
                 .select_related('request', 'request__bill_detail__project')
                 .order_by('request_id', 'pk')):
        last[step.request_id] = step   # the last row per request wins, as resubmit reads it
    return sorted((s.request for s in last.values() if s.assignee_id == pm_id),
                  key=lambda r: (r.raised_at, r.pk))


def hand_over_sites(sites, from_pm, to_pm, by, reason, program=None):
    """Move every site in `sites` from `from_pm` to `to_pm`, ALL OR NOTHING (PM-13).
    Returns a Handover. When any site fails, nothing at all has moved and this raises
    SitePmChangeRefused("<site code> failed: <reason>"): the reason is the refusal's own
    message, or "unexpected error (logged)" for any other exception, which is logged with
    its traceback. PermissionDenied (not Admin) propagates as a 403.

    Every site goes through change_site_pm() with `from_pm` as the expected outgoing PM,
    in one transaction: a site whose PM changed since the page was loaded, a Cancelled
    site, or a bill step the approval rules refuse fails the whole handover. After the
    commit the PM-6 pair goes out — one notice to each PM, in-app and email, never one
    per site — and, when the outgoing PM still holds material steps, one in-app notice
    to every active SCM user (Q6).
    """
    reason = (reason or '').strip()
    if from_pm.pk == to_pm.pk:
        raise SitePmChangeRefused(f'{_name(to_pm)} is already the PM of these sites.')
    if not sites:
        raise SitePmChangeRefused('Choose at least one site to hand over.')
    if not reason:
        raise SitePmChangeRefused('A reason is required to change the PM.')

    handover = Handover(actor=by, from_pm=from_pm, to_pm=to_pm, program=program,
                        reason=reason, sites=list(sites))
    # The site being processed when something went wrong; None outside the loop, so an
    # error after it (the material / returning-bill reads) is not blamed on a site.
    failing = None
    # The try is OUTSIDE the atomic block on purpose: by the time an except clause runs,
    # the block has already rolled back, so nothing has moved and the connection is
    # usable again, even after a Postgres error, before anything is logged.
    try:
        with transaction.atomic():
            for site in handover.sites:
                failing = site
                result = change_site_pm(site, to_pm, by, expected_outgoing_pm=from_pm,
                                        reason=reason)
                handover.tasks_moved += result.tasks_moved
                handover.bill_steps_moved += result.bill_steps_moved
                handover.bill_steps_waiting += result.bill_steps_waiting
            failing = None
            handover.material_steps = material_steps_held_by(from_pm.pk, handover.sites,
                                                             program)
            handover.returning_bills = returning_bills_for(from_pm.pk, handover.sites)
            transaction.on_commit(lambda: send_handover_notices(handover), robust=True)
    except PermissionDenied:
        # Not Admin: a 403 by design (PM-7), not a failure of the site, so not reworded.
        raise
    except SitePmChangeRefused as exc:
        if failing is None:
            raise
        # A refusal is an expected outcome (stale page, Cancelled site, a bill step the
        # approval rules refuse), so a warning line without a traceback.
        logger.warning('hand_over_sites: %s failed, nothing was handed over: %s',
                       failing.project_id, exc)
        raise SitePmChangeRefused(f'{failing.project_id} failed: {exc}') from exc
    except Exception as exc:
        if failing is None:
            raise
        # Anything else is a bug or a database fault. The screens must still say which
        # site failed and that nothing moved, never a 500 (PM-13); the detail goes to
        # the log, not the page.
        logger.exception('hand_over_sites: %s failed with an unexpected error, nothing '
                         'was handed over', failing.project_id)
        raise SitePmChangeRefused(
            f'{failing.project_id} failed: unexpected error (logged)') from exc
    return handover


def _site_list(sites, program):
    """'MB0001' for one site; '3 sites in MPUVNL (MB0001, MB0002, MB0003)' for a few;
    the first three and a count beyond that, so the bell text stays one line."""
    codes = [s.project_id for s in sites]
    if len(codes) == 1:
        return codes[0]
    shown = ', '.join(codes[:3]) + (f', +{len(codes) - 3} more' if len(codes) > 3 else '')
    where = f' in {program.name}' if program is not None else ''
    return f'{len(codes)} sites{where} ({shown})'


def send_handover_notices(handover):
    """The PM-6 pair, plus the SCM notice (Q6). AFTER COMMIT ONLY — hand_over_sites()
    registers this with transaction.on_commit(robust=True), so a rolled-back handover
    tells nobody and a failed send never undoes one.

      incoming PM  in-app + email: what they now hold, and why
      outgoing PM  in-app + email: what left them, and how many material steps still
                   name them
      SCM          in-app only, every active SCM user, only when material steps > 0

    Never the actor; nobody inactive. No WhatsApp (PM-6). The message is both the bell
    text and the email's text part, the closeout notices' pattern (cod_views).
    """
    h = handover
    what = _site_list(h.sites, h.program)
    moved = (f"{_count(h.tasks_moved, 'open task')} and "
             f"{_count(h.bill_steps_moved, 'bill approval step')}")
    material = len(h.material_steps)
    single = h.sites[0] if len(h.sites) == 1 else None

    if single is not None:
        incoming_link = reverse('project_overview', args=[single.project_id])
    elif h.program is not None:
        incoming_link = reverse('program_detail', args=[h.program.pk])
    else:
        incoming_link = get_user_dashboard(h.to_pm.user)

    notices = [
        (h.to_pm, IN_APP_AND_EMAIL, 'pm_handover_incoming', incoming_link,
         f'{_name(h.actor)} handed over {what} to you from {_name(h.from_pm)}: '
         f'{moved} moved to you. Reason: {h.reason}'),
        # The outgoing PM's link is their dashboard: they can no longer open the sites.
        (h.from_pm, IN_APP_AND_EMAIL, 'pm_handover_outgoing',
         get_user_dashboard(h.from_pm.user),
         f'{_name(h.actor)} handed over {what} from you to {_name(h.to_pm)}: '
         f'{moved} moved to {_name(h.to_pm)}.'
         + (f" {_count(material, 'material approval step')} still name you; "
            f"SCM will reassign {'it' if material == 1 else 'them'}." if material else '')
         + f' Reason: {h.reason}'),
    ]
    if material:
        scm_message = (
            f'PM handover: {what} moved from {_name(h.from_pm)} to {_name(h.to_pm)}. '
            f"{_count(material, 'material approval step')} still name {_name(h.from_pm)} "
            f"as PM and did not move — reassign {'it' if material == 1 else 'them'}.")
        for scm in (UserProfile.objects.select_related('user')
                    .filter(role='SCM', is_active=True, user__is_active=True)):
            notices.append((scm, ['in_app'], 'pm_handover_scm', reverse('approval_list'),
                            scm_message))

    actor_pk = h.actor.pk if h.actor is not None else None
    for recipient, channels, template, link, message in notices:
        if recipient.pk == actor_pk or not recipient.is_active or not recipient.user.is_active:
            continue
        try:
            send_notification(
                recipient, message, channels=channels, link=link, subject=message,
                template=template, related_project=single, actor=h.actor,
            )
        except Exception:
            logger.exception('send_handover_notices: the %s notice to %s failed; the '
                             'handover stands.', template, recipient.pk)


def _count(n, noun):
    return f"{n} {noun}{'' if n == 1 else 's'}"


def _name_or_none(profile_pk):
    """A PM's display name by pk, or 'no PM' for an empty seat."""
    if profile_pk is None:
        return 'no PM'
    return _name(UserProfile.objects.select_related('user').get(pk=profile_pk))


# ---------------------------------------------------------------------------
# PM-11 — the handover message for an outgoing PM
#
# A site you cannot see answers 404 (the 0.2 non-disclosure shape), so a PM who loses a
# site and then follows an old link or notice gets "page not found" for a site they ran
# yesterday. The narrow exception: a user who once held this site's PM seat (a CLOSED
# SitePmAssignment row) and cannot see it now is redirected to their dashboard and told
# what happened. Everyone else — other PMs, Finance, SCM, anyone with no closed row here —
# still gets the view's existing refusal, unchanged.
#
# It discloses nothing new: the user already knows the site code (it is in their link),
# and they held the seat, so who holds it now is theirs to know.
#
# ONLY ON A DENIAL. The views call these helpers after their own visibility check has
# already failed, so an allowed request pays no query for this (verified on
# project_overview). Only plain GETs are redirected; a POST, an HTMX request or anything
# else gets the existing refusal (R4), because a redirect would break a form post or an
# HTMX swap.
# ---------------------------------------------------------------------------

def pm_handover_notice(user, project):
    """The handover message for `user` on `project`, or None.

    Not None only when the user cannot see the site now AND has a closed
    SitePmAssignment row on it. Uses their MOST RECENT closed row's `valid_to` (only
    change_site_pm() ever sets it, so it is a real date even on a backfilled row —
    `is_backfilled` is deliberately ignored, ruling R1) and the site's CURRENT PM, not
    whoever followed them (R2): after A -> B -> C, A and B are both told C.
    """
    profile = getattr(user, 'profile', None)
    if profile is None:
        return None
    # Someone who can see the site again (reassigned back, made a coordinator) is not
    # refused at all, so they must never be told they were handed over.
    if user_can_view_project(user, project):
        return None
    closed = (SitePmAssignment.objects
              .filter(site=project, pm=profile, valid_to__isnull=False)
              .order_by('-valid_to', '-pk')
              .values_list('valid_to', flat=True)
              .first())
    if closed is None:
        return None
    current = project.assigned_pm
    current_name = (current.user.get_full_name() or current.user.username) if current else 'not set'
    return (f"{project.project_id} was handed over from you on "
            f"{timezone.localtime(closed):%d %b %Y}. Its current PM is {current_name}. "
            f"Contact Admin if you still need access.")


def _handover_redirect(request, project):
    """A redirect to the user's dashboard carrying the handover message, or None when
    this request is not a handover case (not a plain GET, or no closed row)."""
    if request.method != 'GET' or request.headers.get('HX-Request'):
        return None
    notice = pm_handover_notice(request.user, project)
    if notice is None:
        return None
    messages.info(request, notice)
    return redirect(get_user_dashboard(request.user))


def refuse_site_view_404(request, project):
    """Refuse a site page whose existing refusal is a bare 404. Returns the handover
    redirect for an outgoing PM; raises the same bare Http404 for everyone else.
    Callers: `return refuse_site_view_404(request, project)`."""
    response = _handover_redirect(request, project)
    if response is None:
        raise Http404
    return response


def refuse_site_view_403(request, project):
    """Refuse a site page whose existing refusal is a bare 403 (boq_detail). Returns the
    handover redirect for an outgoing PM, otherwise the same empty HttpResponseForbidden
    the view returned before."""
    return _handover_redirect(request, project) or HttpResponseForbidden()
