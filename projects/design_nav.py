"""DW-8: where each design screen leads back to.

Navigation was the design team's top day-to-day complaint (DW-A, T9). Many screens offered
only "← Project", QC and the Head had no link back to their own queue, and several links
pointed at screens that refuse the person reading them. This module answers two questions
for one viewer on one screen, from the existing permission helpers only:

  * design_queue(): the "← My queue" target (N1), or None for someone with no design queue;
  * the small link-visibility facts a screen needs to hide a link whose target would refuse
    the viewer (Zuber, DW-8 answer 5). Each one asks the TARGET's own gate, so a link and
    the screen behind it cannot disagree.

NO GATE LIVES HERE. Nothing in this module admits or refuses a request. The two role lists
below are the gates of their views, moved here so the decorator and the link read one list.

Imports permissions, models constants and urls only, never design_views or views, so both
of those can import it at module level without a cycle.
"""
from django.urls import reverse

from .models import DESIGN_AWAITING_PM_APPROVAL
from .permissions import (
    can_approve_design_release,
    user_has_design_head_authority,
    user_is_assigned_designer,
    user_is_design_qc,
)

#: The roles views.dashboard_design admits (its @role_required). One list, so the
#: "← Design dashboard" links and the decorator cannot drift apart.
DESIGN_DASHBOARD_ROLES = ['Design']

#: The roles views.program_detail admits (its @role_required), for the same reason: the
#: Head's site list links to the tender page only for someone it would let in.
PROGRAM_DETAIL_ROLES = ['Admin', 'PM', 'CEO']

#: Which queue a viewer is sent to (the include's data-queue attribute, read by tests).
QUEUE_DESIGNER = 'designer'
QUEUE_QC = 'qc'
QUEUE_HEAD = 'head'

_UNSET = object()


def user_can_open_design_dashboard(user):
    """True if views.dashboard_design would admit `user` (its role list, above)."""
    profile = getattr(user, 'profile', None)
    return profile is not None and profile.role in DESIGN_DASHBOARD_ROLES


def user_can_open_program_detail(user):
    """True if views.program_detail would admit `user` (its role list, above)."""
    profile = getattr(user, 'profile', None)
    return profile is not None and profile.role in PROGRAM_DETAIL_ROLES


def design_workspace_access(user, project, assignment, has_head_authority=None):
    """(allowed, is_designer, pm_reviewing): design_site_workspace()'s gate, in one place.

    THE VIEW'S GATE, MOVED HERE UNCHANGED, so the workspace's own refusal and every
    "design workspace" link on the review screens and the picker ask the same question.
    The allocated designer, Design Head authority (the Head or his deputy), or the site's
    PM while the package waits on the PM's verdict (prompt 3.1b-1). Everyone else, a QC
    reviewer included, is refused, which is why those links were dead for QC (DW-8 P2).

    `has_head_authority`: a caller that already knows the answer passes it, or a
    zero-argument callable that gives it, so the deputy lookup is not repeated. The
    expression and its order of evaluation are the view's own (`is_designer or Head
    authority or pm_reviewing`), so Head authority is read only when the user is not the
    designer. `assignment` must not be None: the view 404s first, and DesignNav returns
    False before it calls.
    """
    is_designer = user_is_assigned_designer(user, assignment)
    pm_reviewing = (assignment.status == DESIGN_AWAITING_PM_APPROVAL
                    and can_approve_design_release(user, project))

    def head_authority():
        if has_head_authority is None:
            return user_has_design_head_authority(user)
        if callable(has_head_authority):
            return has_head_authority()
        return has_head_authority

    allowed = bool(is_designer or head_authority() or pm_reviewing)
    return allowed, is_designer, pm_reviewing


class DesignNav:
    """One viewer on one screen: the "← My queue" target and the link facts, worked out
    lazily so a screen pays only for what its template reads.

    `project` / `assignment`: the site the screen is about, if any. `program`: a screen
    about a whole tender (the Head's site list) passes it instead. `head_authority`: a
    view that has already evaluated Design Head authority passes the answer, or a
    zero-argument callable that gives it (DesignReviewerTurn.is_head, whose cached lookup
    a BOQ screen has often paid already), so the deputy lookup is made at most once.
    """

    def __init__(self, user, project=None, assignment=_UNSET, program=None,
                 head_authority=None):
        self.user = user
        self.project = project
        if assignment is _UNSET:
            # Reverse one-to-one: cached on the project once read, a miss included, so a
            # view that already touched project.design_assignment pays nothing here.
            assignment = (getattr(project, 'design_assignment', None)
                          if project is not None else None)
        self.assignment = assignment
        self.program = program
        self._head_source = head_authority
        self._head = None
        self._queue = _UNSET

    @property
    def has_head_authority(self):
        """Design Head authority (the Head or his named deputy). Cached."""
        if self._head is None:
            source = self._head_source
            if source is None:
                self._head = user_has_design_head_authority(self.user)
            elif callable(source):
                self._head = bool(source())
            else:
                self._head = bool(source)
        return self._head

    def _tender_program(self):
        """The OPEX tender whose Head dashboard this screen's site or tender belongs to, or
        None. design_tender_dashboard() 404s anything else, so the link is offered only
        for a live OPEX program (program_type and is_deleted are its own filter terms)."""
        program = self.program
        if program is None and self.project is not None and self.project.program_id:
            program = self.project.program
        if program is None or program.is_deleted or program.program_type != 'OPEX':
            return None
        return program

    def queue(self):
        """{'kind', 'url'} for "← My queue", or None. THE N1 RULE, first match wins:

            1. the site's allocated designer            -> design_my_sites (answer 1)
            2. the site's named QC reviewer              -> design_qc_queue
            3. Design Head authority                     -> the site's tender dashboard
                                                            (the Design dashboard, his
                                                            strip, off a site)
            4. the Design QC flag (the open pool)        -> design_qc_queue
            5. any other Design user                     -> design_my_sites
            6. anyone else (PM, SCM, ...)                -> None: they keep today's links

        "The most specific role for this site" is why 1 and 2 come before the Head: a Head
        who took a site on personally is its designer there, and a person named as this
        site's reviewer reviews it whatever else they hold. Every target is a screen the
        user can open: my sites has no gate beyond a profile, the QC queue admits the flag
        and any named reviewer (user_can_view_qc_queue), the tender dashboard admits Head
        authority, and the Design dashboard is checked against its own role list.

        Queries: none for rules 1 and 2 and for the Head himself; the deputy lookup for
        everyone else, once; the program row for the Head when the view has not read it.
        """
        if self._queue is not _UNSET:
            return self._queue
        self._queue = self._work_out_queue()
        return self._queue

    def _work_out_queue(self):
        user = self.user
        profile = getattr(user, 'profile', None)
        if profile is None:
            return None
        assignment = self.assignment
        if assignment is not None:
            if user_is_assigned_designer(user, assignment):
                return {'kind': QUEUE_DESIGNER, 'url': reverse('design_my_sites')}
            if assignment.qc_assigned_to_id == profile.pk:
                return {'kind': QUEUE_QC, 'url': reverse('design_qc_queue')}
        if self.has_head_authority:
            program = self._tender_program()
            if program is not None:
                return {'kind': QUEUE_HEAD,
                        'url': reverse('design_tender_dashboard', kwargs={'pk': program.pk})}
            if user_can_open_design_dashboard(user):
                return {'kind': QUEUE_HEAD, 'url': reverse('dashboard_design')}
            # A deputy outside the Design role, off a tender: the QC queue is the one
            # screen of theirs that admits Head authority without a program.
            return {'kind': QUEUE_HEAD, 'url': reverse('design_qc_queue')}
        if user_is_design_qc(user):
            return {'kind': QUEUE_QC, 'url': reverse('design_qc_queue')}
        if profile.role == 'Design':
            return {'kind': QUEUE_DESIGNER, 'url': reverse('design_my_sites')}
        return None

    @property
    def queue_is_design_dashboard(self):
        """True when "← My queue" already points at the Design dashboard, so a screen that
        also carries a "← Design dashboard" link shows it once, not twice."""
        q = self.queue()
        return q is not None and q['url'] == reverse('dashboard_design')

    @property
    def can_open_design_dashboard(self):
        return user_can_open_design_dashboard(self.user)

    @property
    def can_open_workspace(self):
        """design_site_workspace()'s own gate (design_workspace_access), for the links that
        point there from the review screens and the picker."""
        if self.assignment is None:
            return False
        # Lazy, so the designer and the reviewing PM pay no deputy lookup for this link.
        allowed, _is_designer, _pm = design_workspace_access(
            self.user, self.project, self.assignment,
            has_head_authority=lambda: self.has_head_authority)
        return allowed

    @property
    def can_open_head_sites(self):
        """design_head_sites() admits Design Head authority and nobody else."""
        return self.has_head_authority

    @property
    def can_open_program_detail(self):
        return user_can_open_program_detail(self.user)
