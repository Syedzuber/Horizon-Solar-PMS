"""
OPEX design workflow views — Part 2: survey upload, allocation, due-date handshake,
and the blocked flag. Part 3: Arka submission and verdict, CAD upload, BOQ entry
and the artifact-pairing rules that bind them together. Part 4: QC review, the
attempt lifecycle, the Design Head's deputy, PM change requests and release.

A separate module from views.py (which is ~9,000 lines) because this is a self-contained
new subsystem; urls.py imports it alongside `views`. No existing view is modified.

STATUS TRANSITIONS LIVE HERE, NOT ON THE MODEL. Part 1 deliberately left the models
inert — no save() override, no signal, no state machine. Every status change in this
module is an explicit assignment in a view, inside a transaction, next to the
permission check that authorises it.

OPEX ONLY. Every entry point re-checks `project_type == 'OPEX'`; Residential design work
continues to run on its six design Task rows with PM-owned approval and is not reachable
from here.
"""
import logging
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.db import IntegrityError, transaction
from django.db.models import Count, Max, Q, Sum
from django.http import Http404, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_date

from .decorators import login_required
from .design_metrics import (
    STAGE_LABELS, effective_commitment, pending_extension, tender_metrics,
)
from .utils import design_due_date
from .design_storage import (
    DesignStorageError, build_design_path, get_design_file_url, upload_design_file,
    validate_cad_zip,
)
from .models import (
    Program, Project, UserProfile, BOQ, BOQItem, DesignAssignment, DueDateCommitment,
    DesignAttempt, ArkaSubmission, DesignFile, DesignChangeRequest, log_activity,
    SiteGroup, SiteGroupMembership, SITE_GROUP_DRAFT, SITE_GROUP_LOCKED,
    DESIGN_AWAITING_SURVEY, DESIGN_AWAITING_ALLOCATION, DESIGN_ALLOCATED,
    DESIGN_DUE_DATE_PROPOSED, DESIGN_IN_DESIGN, DESIGN_SURVEY_RETURNED,
    DESIGN_ARKA_SUBMITTED, DESIGN_ARKA_REJECTED, DESIGN_ARTIFACTS_UPLOADED,
    DESIGN_IN_QC, DESIGN_QC_FAILED, DESIGN_RELEASED,
    DESIGN_AWAITING_HEAD_ARKA, DESIGN_AWAITING_HEAD_QC,
    ARKA_PENDING, ARKA_APPROVED, ARKA_REJECTED,
    QC_PENDING, QC_PASSED, QC_FAILED,
    ATTEMPT_REASON_INITIAL, ATTEMPT_REASON_QC_FAILED, ATTEMPT_REASON_PM_CHANGE_REQUEST,
    DESIGN_FILE_CAD_ZIP, DESIGN_FILE_CAD_PDF, DESIGN_FILE_CAD_DWG,
    DESIGN_FILE_BOQ_EXCEL, DESIGN_FILE_BOQ_PDF, DESIGN_FILE_KIND_CHOICES,
    DESIGN_FILE_CAD_KINDS, DESIGN_FILE_LEGACY_KINDS,
    DESIGN_ERROR_CATEGORIES, DESIGN_ERROR_CATEGORY_CHOICES,
    DESIGN_ERROR_CATEGORY_LABELS,
)
from .permissions import (
    project_boq_is_group_locked, user_can_edit_project_boq,
    user_can_manage_site_groups, user_can_qc_design, user_can_request_design_change,
    user_can_view_design, user_can_view_site_groups, user_has_design_head_authority,
    user_is_assigned_designer, user_is_design_head, user_is_design_head_deputy,
    user_can_qc_gate_design, user_can_head_gate_design, user_is_design_qc,
    user_can_view_design_qc_dashboard,
)

logger = logging.getLogger(__name__)

# Statuses at which the site has not yet started design work. Reallocation is allowed
# only while the assignment is still in one of these — see design_allocate().
#
# PART 8 ADDED `in_design`, and it is not optional. Allocation now lands a site straight
# in `in_design`, so without it the Head would lose the ability to correct a
# mis-allocation the instant he made one: every allocated site would be past the
# reallocation gate before he could see the result. `in_design` means work is under way
# with no Arka submitted yet, so nothing is orphaned by moving the site to a different
# designer. `arka_submitted` and everything after it remain closed — reallocating once an
# Arka exists would leave artifacts pointing at another designer's layout.
#
# `allocated` and `due_date_proposed` are kept here for the rows that already carry them;
# no new row can reach either. See DESIGN_MODULE_DEFERRED.md.
REALLOCATABLE_STATUSES = (DESIGN_AWAITING_ALLOCATION, DESIGN_ALLOCATED,
                          DESIGN_DUE_DATE_PROPOSED, DESIGN_IN_DESIGN)

# ── Part 3 ─────────────────────────────────────────────────────────────────────
# The two statuses from which a designer may submit an Arka version: the first one
# from `in_design`, and every replacement from `arka_rejected`.
#
# `arka_submitted` is deliberately EXCLUDED. Once a version is submitted it is either
# awaiting a verdict (resubmitting would leave the Head reviewing a version that no
# longer exists) or approved (resubmitting would silently orphan the CAD and BOQ
# artifacts already paired to it via derived_from_arka). Either way the Head must act
# first. See design_arka_submit().
ARKA_SUBMITTABLE_STATUSES = (DESIGN_IN_DESIGN, DESIGN_ARKA_REJECTED)

# Every CAD kind, current and legacy. READ PATHS ONLY — listing, download, history.
# The progression rule deliberately does NOT use this; see PROGRESSION_CAD_KINDS.
CAD_KINDS = DESIGN_FILE_CAD_KINDS

# What satisfies "CAD is present" for the move to `artifacts_uploaded` (Part 8).
#
# Part 3's rule was "at least one CAD file present", which the two-file world made
# reasonable. It is now a VALID cad_zip and nothing else. A legacy cad_pdf on its own no
# longer advances an attempt: the whole point of the zip is that QC gets the PDF and the
# DWG together, validated at upload, and accepting a lone legacy file would reopen the
# gap the zip closes. Legacy rows stay readable — they just do not satisfy this gate,
# and no attempt still in flight has one, since the legacy kinds can no longer be
# uploaded.
PROGRESSION_CAD_KINDS = (DESIGN_FILE_CAD_ZIP,)

# Every kind design_artifact_upload() accepts. A `kind` outside this tuple is refused
# rather than silently defaulted — the whitelist is the enforcement point, not the
# select element in the template.
#
# The legacy CAD kinds are ABSENT: they are readable but no longer uploadable, which is
# enforced here rather than by hiding the option in the template.
UPLOADABLE_KINDS = (DESIGN_FILE_CAD_ZIP,
                    DESIGN_FILE_BOQ_EXCEL, DESIGN_FILE_BOQ_PDF)

KIND_LABELS = dict(DESIGN_FILE_KIND_CHOICES)

# ── Part 4 ─────────────────────────────────────────────────────────────────────
# Statuses during which a PM change request is permitted. The window OPENS at QC start
# (settled decision 3) — before that, `qc_started_at` is null and a change is a
# conversation, not a system action — and closes at release, because BOQ locking (the
# real close condition) is Part 6 and does not exist yet.
#
# `qc_failed` is included: the package is back with the designer, and a PM who spots a
# requirement change at that moment should not have to wait for the next QC round to
# say so. `released` is excluded, which is the close condition standing in for the lock.
#
# PART 9 added the two waiting-room statuses. `awaiting_head_qc` genuinely needs to be
# here: the package has passed Design QC and is sitting with the Head, `qc_started_at` is
# set and the site is not released, so the window is open by every term of the rule above.
# `awaiting_head_arka` is included for completeness and is unreachable in practice —
# `qc_started_at` is still null at the Arka stage, so the check below refuses it first.
CHANGE_REQUEST_STATUSES = (DESIGN_IN_QC, DESIGN_QC_FAILED, DESIGN_IN_DESIGN,
                           DESIGN_ARKA_SUBMITTED, DESIGN_ARKA_REJECTED,
                           DESIGN_ARTIFACTS_UPLOADED,
                           DESIGN_AWAITING_HEAD_ARKA, DESIGN_AWAITING_HEAD_QC)


# ── Part 9 ─────────────────────────────────────────────────────────────────────
# THE TWO GATES, AND THE FOUR CHECKS EVERY VERDICT ENDPOINT MAKES.
#
# Design QC reviews first, the Design Head second. Both the Arka and the QC package go
# through both gates, and all four verdict endpoints enforce the same four conditions
# server-side, so a direct POST is refused exactly as a hidden button would have been:
#
#   1. the actor holds the correct FLAG for that gate      permissions.user_can_*_gate_design
#   2. the actor is not assignment.assigned_to             (same helpers — decision 3)
#   3. the actor did not record the OTHER gate's verdict   _other_gate_actor_conflict()
#      on THIS artifact                                    (decision 2)
#   4. the PRECEDING gate has passed                       the per-artifact target resolvers
#
# Checks 1 and 2 are about a user and live in permissions.py. Checks 3 and 4 are about a
# ROW and live here, because they need the ArkaSubmission or DesignAttempt in hand.
#
# WHY CHECK 3 EXISTS AT ALL: one person holding both flags could otherwise clear a site
# with two clicks, and two clicks by one person is not a second gate. The refusal is per
# ARTIFACT, not per user — a dual-flag holder may still record the Head verdict on a site
# whose QC verdict somebody else recorded, which is the normal way a Head with the QC flag
# actually works.
#
# STATUS AFTER A HEAD ARKA APPROVAL IS `arka_submitted`, NOT a new value. Part 3 settled
# that there is no status for "Arka approved, artifacts outstanding" — the verdict on the
# current Arka disambiguates, and design_metrics._classify() already splits the two. Part 9
# keeps that exactly: QC approval moves the site to `awaiting_head_arka`, and the Head's
# approval returns it to `arka_submitted`, now carrying head_verdict='approved'. The site
# is not going backwards; `awaiting_head_arka` means strictly "the Head has not ruled", so
# the site must leave it the moment he does.


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _opex_site(project_id):
    """Fetch a non-deleted OPEX site or 404. Residential and CAPEX are unreachable
    through every view in this module."""
    project = get_object_or_404(Project, project_id=project_id, is_deleted=False)
    if project.project_type != 'OPEX':
        raise Http404('Design workflow applies to OPEX sites only.')
    return project


def _get_or_create_assignment(project):
    """The DesignAssignment is created lazily by the first survey upload, so seeded or
    imported sites do not all carry an empty row from day one."""
    assignment = getattr(project, 'design_assignment', None)
    if assignment is None:
        assignment = DesignAssignment.objects.create(project=project)
    return assignment


def _status_after_unblock(assignment):
    """Status to restore when the Head clears a Design Hold by uploading a replacement
    survey.

    DERIVED, not stored — Part 2 adds no schema. The prior state is recoverable from the
    rows that already exist: no designer means the site never left the allocation queue;
    an APPROVED commitment means design was under way.

    PART 8 reads the approved commitment rather than the `is_current` one. A site can now
    be on hold with an extension request pending, and `is_current` would then be the
    unapproved row — restoring such a site to `due_date_proposed` would drop it back to a
    pre-design stage it had long since left. An approved date is the evidence design had
    started, and a pending request does not undo it.
    """
    if assignment.assigned_to_id is None:
        return DESIGN_AWAITING_ALLOCATION
    if _effective_commitment(assignment) is not None:
        return DESIGN_IN_DESIGN
    # No approved date has ever existed for this site — it was allocated under the Part 2
    # handshake and never got past it. Those rows are the only ones that can still land
    # here; nothing allocated under Part 8 can reach this line.
    return DESIGN_ALLOCATED


def _deny(request, message, redirect_to):
    messages.error(request, message)
    return redirect(redirect_to)


# ---------------------------------------------------------------------------
# Screens
# ---------------------------------------------------------------------------

@login_required
def design_head_sites(request, pk):
    """Design Head's working screen for one tender: every site, its status, designer,
    current due date and blocked flag, with the actions for each."""
    if not user_has_design_head_authority(request.user):
        return HttpResponseForbidden('Design Head only.')

    program = get_object_or_404(Program, pk=pk, is_deleted=False, program_type='OPEX')
    sites = (program.sites.filter(is_deleted=False)
             .select_related('design_assignment', 'design_assignment__assigned_to__user')
             .order_by('project_id'))

    rows = []
    for site in sites:
        assignment = getattr(site, 'design_assignment', None)
        current = pending = None
        if assignment is not None:
            # The AGREED date, not the is_current row — a pending extension must not
            # change what this screen says the site is committed to (Part 8).
            current = _effective_commitment(assignment)
            pending = _pending_extension(assignment)
        rows.append({
            'site':          site,
            'assignment':    assignment,
            'status':        assignment.status if assignment else DESIGN_AWAITING_SURVEY,
            'has_survey':    bool(assignment and assignment.survey_file_path),
            'current_due':   current,
            'pending_extension': pending,
            'revisions':     (assignment.due_date_commitments.count() - 1) if assignment else 0,
            'is_blocked':    bool(assignment and assignment.status == DESIGN_SURVEY_RETURNED),
            'allocatable':   bool(assignment and assignment.survey_file_path
                                  and assignment.status in REALLOCATABLE_STATUSES),
        })

    designers = (UserProfile.objects.select_related('user')
                 .filter(role='Design', is_active=True)
                 .order_by('user__first_name', 'user__username'))

    return render(request, 'projects/design/head_sites.html', {
        'program':   program,
        'rows':      rows,
        'designers': designers,
    })


@login_required
def design_my_sites(request):
    """Designer's own queue: the sites allocated to them, with their actions."""
    profile = getattr(request.user, 'profile', None)
    if profile is None:
        return HttpResponseForbidden('No profile.')

    assignments = (DesignAssignment.objects
                   .filter(assigned_to=profile, project__is_deleted=False)
                   .select_related('project', 'project__program')
                   .order_by('project__project_id'))

    rows = []
    for assignment in assignments:
        current = _effective_commitment(assignment)
        pending = _pending_extension(assignment)
        rows.append({
            'assignment':  assignment,
            'site':        assignment.project,
            'current_due': current,
            'pending_extension': pending,
            # PART 8: the designer asks for an EXTENSION, and only once there is an
            # agreed date to extend, no request already in flight, and the site is
            # still live.
            'can_request_extension': bool(current is not None and pending is None
                                          and assignment.status != DESIGN_RELEASED),
            'awaiting':    pending is not None,
            'is_blocked':  assignment.status == DESIGN_SURVEY_RETURNED,
            'revisions':   assignment.due_date_commitments.count() - 1,
        })

    return render(request, 'projects/design/my_sites.html', {'rows': rows})


# ---------------------------------------------------------------------------
# 2. Survey upload / view
# ---------------------------------------------------------------------------

@login_required
def design_survey_upload(request, project_id):
    """Design Head uploads (or replaces) the survey file for an OPEX site.

    Upload happens BEFORE any row is written and the row write is inside a transaction,
    so a storage failure can never leave a DesignAssignment pointing at an object that
    does not exist.

    Replacing a survey is how the Head clears a designer's blocked flag: when the site
    is blocked, status returns to whatever it was before the block (derived, see
    _status_after_unblock), preserving the allocation and any approved due date.
    """
    project = _opex_site(project_id)
    if not user_has_design_head_authority(request.user):
        return HttpResponseForbidden('Design Head only.')
    if request.method != 'POST':
        return redirect('design_head_sites', pk=project.program_id)

    upload = request.FILES.get('survey_file')
    if not upload:
        messages.error(request, 'Please choose a survey file to upload.')
        return redirect('design_head_sites', pk=project.program_id)

    assignment = getattr(project, 'design_assignment', None)
    was_blocked = bool(assignment and assignment.status == DESIGN_SURVEY_RETURNED)

    # Allocation locks the survey EXCEPT when clearing a block — otherwise a survey
    # could be swapped underneath a designer who has already started.
    if (assignment and assignment.survey_file_path and not was_blocked
            and assignment.status not in (DESIGN_AWAITING_SURVEY, DESIGN_AWAITING_ALLOCATION)):
        messages.error(
            request,
            f'{project.project_id}: the survey cannot be replaced after allocation '
            f'unless the designer has placed the site on Design Hold.')
        return redirect('design_head_sites', pk=project.program_id)

    path = build_design_path(project.project_id, 'survey', upload.name)
    try:
        bucket, stored_path = upload_design_file(upload, path)
    except DesignStorageError as exc:
        messages.error(request, f'{project.project_id}: {exc}')
        return redirect('design_head_sites', pk=project.program_id)

    profile = request.user.profile
    with transaction.atomic():
        assignment = _get_or_create_assignment(project)
        replacing = bool(assignment.survey_file_path)

        assignment.survey_file_bucket = bucket
        assignment.survey_file_path   = stored_path
        assignment.survey_uploaded_by = profile
        assignment.survey_uploaded_at = timezone.now()

        if was_blocked:
            # Clearing the block. survey_returned_at / _by / _reason are deliberately
            # LEFT IN PLACE: together with survey_uploaded_at they are the record of how
            # long the clock was stopped, without adding a schema field this session.
            assignment.status = _status_after_unblock(assignment)
            action = (f'Design Hold cleared by replacement survey; status restored to '
                      f'{assignment.status}')
            code = 'design_survey_unblocked'
        elif assignment.status == DESIGN_AWAITING_SURVEY:
            assignment.status = DESIGN_AWAITING_ALLOCATION
            action = 'Survey uploaded; site ready for allocation'
            code = 'design_survey_uploaded'
        else:
            action = 'Survey file replaced'
            code = 'design_survey_replaced'

        assignment.save()
        log_activity(project, profile, action,
                     entity_type='DesignAssignment', entity_id=assignment.pk,
                     action_code=code)

    if was_blocked:
        messages.success(request, f'{project.project_id}: replacement survey uploaded, '
                                  f'Design Hold cleared ({assignment.get_status_display()}).')
    elif replacing:
        messages.success(request, f'{project.project_id}: survey replaced.')
    else:
        messages.success(request, f'{project.project_id}: survey uploaded — ready to allocate.')
    return redirect('design_head_sites', pk=project.program_id)


@login_required
def design_survey_download(request, project_id):
    """Redirect to a freshly-signed, short-lived URL for the survey file.

    The URL is minted per request and never stored. Visibility is the ordinary project
    visibility rule, so anyone who can see the site can open its survey.
    """
    project = _opex_site(project_id)
    if not user_can_view_design(request.user, project):
        return HttpResponseForbidden('You do not have access to this site.')

    assignment = getattr(project, 'design_assignment', None)
    if assignment is None or not assignment.survey_file_path:
        raise Http404('No survey file for this site.')

    try:
        url = get_design_file_url(assignment.survey_file_bucket, assignment.survey_file_path)
    except DesignStorageError as exc:
        messages.error(request, str(exc))
        return redirect('design_my_sites')
    if not url:
        raise Http404('No survey file for this site.')
    return redirect(url)


# ---------------------------------------------------------------------------
# 3. Allocation
# ---------------------------------------------------------------------------

def _allocate_one(assignment, designer, actor, allocated_on=None):
    """Core allocation, shared by the single and bulk paths so their rules cannot drift.
    Raises ValueError with a user-facing message; callers own the transaction.

    PART 8 — THE DUE DATE IS SET HERE, AUTOMATICALLY, AND IS ALREADY APPROVED.
    This inverts the Part 2 handshake. The designer no longer proposes the initial date
    and the Head no longer approves it; the date is computed from the allocation date
    (`utils.design_due_date` — +2 calendar days, rolled to the next working day) and
    written as a DueDateCommitment that is approved at the moment it is created, with the
    allocating Head as both proposer and approver. The Head IS the approving authority,
    so recording him on both sides is accurate rather than a fudge — the alternative,
    leaving `approved_by` null, would make the row indistinguishable from a pending
    extension request everywhere downstream.

    The status therefore goes `awaiting_allocation` -> `in_design` DIRECTLY, skipping
    `allocated` and `due_date_proposed` entirely on the initial path.

    `allocated_on` is the date the due date is computed from. Bulk allocation passes ONE
    timestamp for the whole batch so every site in it gets the same date; a batch that
    straddles midnight would otherwise silently split across two due dates.

    ALLOCATION ALSO STAMPS Project.assigned_design (Part 4.5, finding F1).

    ALLOCATION ALSO STAMPS Project.assigned_design (Part 4.5, finding F1).
    Two fields name the designer of a site and they used to be free to diverge:
    `DesignAssignment.assigned_to`, which the design workflow allocates, and
    `Project.assigned_design`, which `user_can_edit_project_boq()` gates BOQ authorship
    on and which the Design dashboard keys its cards off. When they disagreed, the Head
    could allocate a site to a designer who then could not enter its BOQ and never saw
    the site on their own dashboard. Measured before this change: 3 of 5 allocated sites
    had diverged.

    Keeping them in step HERE, at the one moment a designer is chosen, is what makes the
    dashboard integration and BOQ entry work for the allocated designer. The Part 0.6
    BOQ helper is deliberately NOT modified — this feeds it the right value instead.
    Migration 0050 backfilled the rows that had already diverged.
    """
    if not assignment.survey_file_path:
        raise ValueError('cannot be allocated before its survey is uploaded')
    if assignment.status == DESIGN_SURVEY_RETURNED:
        raise ValueError('is on Design Hold over an inadequate survey — '
                         'upload a replacement first')
    if assignment.status not in REALLOCATABLE_STATUSES:
        # Reallocation after work has started is out of scope for Part 2.
        raise ValueError(
            f'has already started design work (status {assignment.status}); '
            f'reallocation at this stage is not supported yet')

    now = timezone.now()
    allocated_on = allocated_on or timezone.localdate(now)
    due = design_due_date(allocated_on)

    previous = assignment.assigned_to
    assignment.assigned_to = designer
    assignment.assigned_by = actor
    assignment.assigned_at = now
    assignment.status = DESIGN_IN_DESIGN
    assignment.save()

    # The auto-approved commitment. Any earlier row is stood down first — reallocation
    # of an already-allocated site re-runs this, and the partial unique constraint
    # (one is_current row per assignment) rejects the insert otherwise.
    assignment.due_date_commitments.filter(is_current=True).update(is_current=False)
    DueDateCommitment.objects.create(
        assignment=assignment, proposed_date=due,
        proposed_by=actor, approved_by=actor, approved_at=now,
        is_current=True)

    # OPEX ONLY. Residential projects carry assigned_design from project_activate and
    # have no DesignAssignment row, so this can never touch one.
    project = assignment.project
    if project.assigned_design_id != designer.pk:
        project.assigned_design = designer
        project.save(update_fields=['assigned_design'])

    if previous and previous.pk != designer.pk:
        detail = (f'Site reallocated from {previous.user.get_full_name() or previous.user.username} '
                  f'to {designer.user.get_full_name() or designer.user.username}')
        code = 'design_reallocated'
    else:
        detail = f'Site allocated to {designer.user.get_full_name() or designer.user.username}'
        code = 'design_allocated'
    log_activity(assignment.project, actor, f'{detail}; due {due}',
                 entity_type='DesignAssignment', entity_id=assignment.pk, action_code=code)
    return due


def _resolve_designer(raw_id):
    """A designer must hold role='Design' and be active. Raises ValueError otherwise."""
    if not (raw_id or '').strip():
        raise ValueError('Please choose a designer.')
    try:
        return UserProfile.objects.select_related('user').get(
            pk=raw_id, role='Design', is_active=True)
    except (UserProfile.DoesNotExist, ValueError):
        raise ValueError('Selected user is not an active Design user.')


@login_required
def design_allocate(request, project_id):
    """Allocate one OPEX site to a designer. Design Head only, POST only."""
    project = _opex_site(project_id)
    if not user_has_design_head_authority(request.user):
        return HttpResponseForbidden('Design Head only.')
    if request.method != 'POST':
        return redirect('design_head_sites', pk=project.program_id)

    try:
        designer = _resolve_designer(request.POST.get('designer_id', ''))
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect('design_head_sites', pk=project.program_id)

    assignment = getattr(project, 'design_assignment', None)
    if assignment is None or not assignment.survey_file_path:
        messages.error(request, f'{project.project_id} cannot be allocated before its '
                                f'survey is uploaded.')
        return redirect('design_head_sites', pk=project.program_id)

    try:
        with transaction.atomic():
            due = _allocate_one(assignment, designer, request.user.profile)
    except ValueError as exc:
        messages.error(request, f'{project.project_id} {exc}.')
        return redirect('design_head_sites', pk=project.program_id)

    messages.success(request, f'{project.project_id} allocated to '
                              f'{designer.user.get_full_name() or designer.user.username} — '
                              f'due {due}.')
    return redirect('design_head_sites', pk=project.program_id)


@login_required
def design_bulk_allocate(request, pk):
    """Allocate several sites of one tender to a single designer.

    ALL OR NOTHING: one transaction, and the first site that fails any rule aborts the
    whole batch. A partially-applied bulk allocation would be worse than none — the Head
    would have to work out which half landed.
    """
    if not user_has_design_head_authority(request.user):
        return HttpResponseForbidden('Design Head only.')
    program = get_object_or_404(Program, pk=pk, is_deleted=False, program_type='OPEX')
    if request.method != 'POST':
        return redirect('design_head_sites', pk=program.pk)

    try:
        designer = _resolve_designer(request.POST.get('designer_id', ''))
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect('design_head_sites', pk=program.pk)

    project_ids = request.POST.getlist('site_ids')
    if not project_ids:
        messages.error(request, 'Select at least one site to allocate.')
        return redirect('design_head_sites', pk=program.pk)

    actor = request.user.profile
    # ONE allocation date for the whole batch (Part 8). Computed before the loop, not
    # per site: a bulk run that crossed midnight would otherwise hand the first sites a
    # different due date from the last, and the Head has no way to see that happened.
    batch_date = timezone.localdate()
    try:
        with transaction.atomic():
            allocated = []
            due = None
            for project_id in project_ids:
                site = get_object_or_404(
                    Project, project_id=project_id, is_deleted=False,
                    project_type='OPEX', program=program)
                assignment = getattr(site, 'design_assignment', None)
                if assignment is None or not assignment.survey_file_path:
                    raise ValueError(f'{site.project_id} cannot be allocated before its '
                                     f'survey is uploaded')
                due = _allocate_one(assignment, designer, actor, allocated_on=batch_date)
                allocated.append(site.project_id)
    except ValueError as exc:
        messages.error(request, f'Nothing was allocated — {exc}.')
        return redirect('design_head_sites', pk=program.pk)

    messages.success(
        request,
        f'{len(allocated)} site(s) allocated to '
        f'{designer.user.get_full_name() or designer.user.username}, due {due}: '
        f'{", ".join(allocated)}.')
    return redirect('design_head_sites', pk=program.pk)


# ---------------------------------------------------------------------------
# 4. Due date handshake
# ---------------------------------------------------------------------------

def _current_commitment(assignment):
    """The row the approve/reject views act on — i.e. the pending extension request.

    NOT the date the site is committed to. Use `_effective_commitment` for that; see the
    effective/pending note at the top of design_metrics.
    """
    return assignment.due_date_commitments.filter(is_current=True).first()


def _effective_commitment(assignment):
    """The APPROVED due date in force, ignoring any pending extension request.

    Every read surface must use this. Ordering matches design_metrics.effective_commitment
    — most recently approved wins, pk as the tiebreak.
    """
    return (assignment.due_date_commitments
            .filter(approved_at__isnull=False)
            .order_by('-approved_at', '-pk')
            .first())


def _pending_extension(assignment):
    """The extension request awaiting a verdict, or None."""
    current = _current_commitment(assignment)
    return current if (current is not None and current.approved_at is None) else None


# ---------------------------------------------------------------------------
# PART 8 — the due-date views are now the EXTENSION flow
# ---------------------------------------------------------------------------
# The initial date is no longer proposed by anybody: `_allocate_one` computes it and
# approves it in the same breath. What remains for these views to do is the case that
# still needs two parties — the designer needs MORE TIME than the automatic date gave
# them, and the Head has to agree before the commitment moves.
#
# The views are repurposed rather than replaced, deliberately: the propose/approve/reject
# trio already implements exactly this shape (insert a proposal, stand the old row down,
# let the Head rule on it), and their URLs are already linked from the designer and Head
# screens. Rewriting them would have meant new URLs and new templates for behaviour that
# was already correct.

@login_required
def design_due_date_propose(request, project_id):
    """The DESIGNER requests an EXTENSION to the agreed due date (Part 8).

    Only the assigned designer. The Head does not request extensions from himself — he
    holds the approving authority, so a Head-initiated change is `design_due_date_change`.

    A REASON IS MANDATORY. The automatic date was already granted without anyone asking
    for it; the only thing that justifies moving it is why, and an extension with a blank
    reason is unauditable a month later when the Head is asked why a site slipped.

    THE APPROVED DATE DOES NOT MOVE HERE. This inserts an unapproved row and takes over
    `is_current`, but every surface reads `_effective_commitment` — the site stays
    committed to, and overdue against, the previously approved date until the Head rules.
    """
    project = _opex_site(project_id)
    assignment = getattr(project, 'design_assignment', None)
    if assignment is None or not user_is_assigned_designer(request.user, assignment):
        return HttpResponseForbidden('Only the designer allocated to this site may '
                                     'request an extension.')
    if request.method != 'POST':
        return redirect('design_my_sites')

    approved = _effective_commitment(assignment)
    if approved is None:
        return _deny(request, f'{project.project_id}: there is no agreed due date to extend.',
                     'design_my_sites')
    if _pending_extension(assignment) is not None:
        return _deny(request, f'{project.project_id}: an extension request is already '
                              f'awaiting the Design Head.', 'design_my_sites')
    if assignment.status == DESIGN_RELEASED:
        return _deny(request, f'{project.project_id}: this site is released — its due date '
                              f'can no longer be changed.', 'design_my_sites')

    reason = (request.POST.get('change_reason') or '').strip()
    if not reason:
        return _deny(request, f'{project.project_id}: a reason is required to request an '
                              f'extension.', 'design_my_sites')

    proposed = parse_date((request.POST.get('proposed_date') or '').strip())
    if proposed is None:
        return _deny(request, 'Please provide a valid date.', 'design_my_sites')
    if proposed < timezone.localdate():
        return _deny(request, 'The requested due date cannot be in the past.', 'design_my_sites')
    if proposed <= approved.proposed_date:
        return _deny(request, f'{project.project_id}: {proposed} is not later than the '
                              f'agreed date {approved.proposed_date} — an extension must '
                              f'move the date out.', 'design_my_sites')

    profile = request.user.profile
    with transaction.atomic():
        # The partial unique constraint permits only one is_current row per assignment,
        # so the approved one is stood down first, in the same transaction. It keeps
        # approved_at, which is what makes it findable as the effective date meanwhile.
        assignment.due_date_commitments.filter(is_current=True).update(is_current=False)
        DueDateCommitment.objects.create(
            assignment=assignment, proposed_date=proposed,
            proposed_by=profile, change_reason=reason, is_current=True)
        # STATUS IS NOT TOUCHED. The site stays in whatever stage the work is actually in;
        # an extension request is not a workflow stage. Moving it to due_date_proposed
        # here would rewind a site that is mid-Arka back to a pre-design stage.
        log_activity(project, profile,
                     f'Extension requested to {proposed} (agreed {approved.proposed_date}): {reason}',
                     entity_type='DesignAssignment', entity_id=assignment.pk,
                     action_code='design_due_date_extension_requested')

    messages.success(request, f'{project.project_id}: extension to {proposed} requested — '
                              f'the agreed date {approved.proposed_date} stands until the '
                              f'Design Head approves.')
    return redirect('design_my_sites')


@login_required
def design_due_date_approve(request, project_id):
    """The HEAD approves the pending extension. The new date becomes effective.

    Approving stamps `approved_at` on the pending row, which is what promotes it past the
    previously approved row in `_effective_commitment`'s ordering. Nothing else has to
    move for the new date to take effect everywhere, and the superseded row keeps its own
    `approved_at` so the history of what was agreed and when stays intact.
    """
    project = _opex_site(project_id)
    if not user_has_design_head_authority(request.user):
        return HttpResponseForbidden('Design Head only.')
    if request.method != 'POST':
        return redirect('design_head_sites', pk=project.program_id)

    assignment = getattr(project, 'design_assignment', None)
    back = project.program_id
    if assignment is None:
        messages.error(request, f'{project.project_id}: no design assignment.')
        return redirect('design_head_sites', pk=back)

    # Gated on a pending ROW, not on a status: an extension can be requested from any
    # working stage, so there is no one status that means "awaiting a due-date verdict".
    commitment = _pending_extension(assignment)
    if commitment is None:
        messages.error(request, f'{project.project_id}: there is no extension request '
                                f'awaiting approval.')
        return redirect('design_head_sites', pk=back)

    profile = request.user.profile
    with transaction.atomic():
        commitment.approved_by = profile
        commitment.approved_at = timezone.now()
        commitment.save(update_fields=['approved_by', 'approved_at'])
        # STATUS IS NOT TOUCHED — see design_due_date_propose. Part 2 moved the site to
        # in_design here because approval was what STARTED the design; under Part 8 the
        # design started at allocation and is already somewhere further on.
        log_activity(project, profile,
                     f'Extension to {commitment.proposed_date} approved',
                     entity_type='DesignAssignment', entity_id=assignment.pk,
                     action_code='design_due_date_extension_approved')

    messages.success(request, f'{project.project_id}: extension approved — the due date is '
                              f'now {commitment.proposed_date}.')
    return redirect('design_head_sites', pk=back)


@login_required
def design_due_date_reject(request, project_id):
    """The HEAD refuses the extension. The PREVIOUS commitment is restored as current.

    The refused row is stood down, not deleted — the record of what was asked for and
    turned down is the point. Restoring the previously approved row to `is_current` is
    not strictly required for the date to be right (`_effective_commitment` already
    ignores `is_current`), but leaving no current row at all would break the partial
    unique constraint's usefulness as "the row under discussion" and would make the next
    extension request harder to reason about. The two are flipped in one transaction.
    """
    project = _opex_site(project_id)
    if not user_has_design_head_authority(request.user):
        return HttpResponseForbidden('Design Head only.')
    if request.method != 'POST':
        return redirect('design_head_sites', pk=project.program_id)

    assignment = getattr(project, 'design_assignment', None)
    back = project.program_id
    if assignment is None:
        messages.error(request, f'{project.project_id}: no design assignment.')
        return redirect('design_head_sites', pk=back)

    commitment = _pending_extension(assignment)
    if commitment is None:
        messages.error(request, f'{project.project_id}: there is no extension request '
                                f'awaiting approval.')
        return redirect('design_head_sites', pk=back)

    reason = (request.POST.get('reason') or '').strip()
    profile = request.user.profile
    with transaction.atomic():
        restored = _effective_commitment(assignment)
        # Stand the refused row down BEFORE restoring, or the two would momentarily both
        # be current and the partial unique constraint would reject the second write.
        assignment.due_date_commitments.filter(is_current=True).update(is_current=False)
        if restored is not None:
            DueDateCommitment.objects.filter(pk=restored.pk).update(is_current=True)
        log_activity(
            project, profile,
            f'Extension to {commitment.proposed_date} rejected'
            + (f': {reason}' if reason else '')
            + (f' — due date remains {restored.proposed_date}' if restored else ''),
            entity_type='DesignAssignment', entity_id=assignment.pk,
            action_code='design_due_date_extension_rejected')

    messages.success(
        request,
        f'{project.project_id}: extension rejected — the due date remains '
        f'{restored.proposed_date}.' if restored else
        f'{project.project_id}: extension rejected.')
    return redirect('design_head_sites', pk=back)


@login_required
def design_due_date_change(request, project_id):
    """The HEAD revises the agreed due date directly. Auto-approved (Part 8).

    Never an in-place edit: a new DueDateCommitment is inserted with a mandatory
    change_reason and the previous row is stood down in the same transaction. The
    revision count is therefore `commitments - 1`, derived by counting rows — there is
    no counter field to drift.

    PART 8 CHANGED WHO MAY USE THIS AND WHAT IT PRODUCES.

    Under Part 2 either party could initiate and the result was a proposal the Head then
    approved. Both halves of that are wrong now. The Head holds the approving authority,
    so routing his own revision through a pending state means he approves his own request
    — a handshake with one hand. And leaving the designer on this path would give the
    system TWO ways to create a pending row, which the partial unique constraint forbids:
    the second would fail with an integrity error rather than a message. So:

        Head      -> here. The new date is approved on creation and takes effect at once.
        Designer  -> design_due_date_propose, which requests an extension for a verdict.

    Unlike an extension this may move the date EARLIER — pulling a date in is a decision
    the Head is entitled to make, and nothing downstream requires dates to be monotonic.
    """
    project = _opex_site(project_id)
    assignment = getattr(project, 'design_assignment', None)
    if assignment is None:
        raise Http404('No design assignment for this site.')

    if not user_has_design_head_authority(request.user):
        if user_is_assigned_designer(request.user, assignment):
            return _deny(request, f'{project.project_id}: request an extension instead — '
                                  f'the Design Head approves any change to the agreed date.',
                         'design_my_sites')
        return HttpResponseForbidden('Only the Design Head may revise an agreed due date.')
    if request.method != 'POST':
        return redirect('design_head_sites', pk=project.program_id)

    def _back(msg, ok=False):
        (messages.success if ok else messages.error)(request, msg)
        return redirect('design_head_sites', pk=project.program_id)

    if assignment.status == DESIGN_RELEASED:
        return _back(f'{project.project_id}: this site is released — its due date can no '
                     f'longer be changed.')
    if _effective_commitment(assignment) is None:
        return _back(f'{project.project_id}: there is no agreed due date to revise.')

    reason = (request.POST.get('change_reason') or '').strip()
    if not reason:
        return _back(f'{project.project_id}: a reason is required to change an agreed due date.')

    proposed = parse_date((request.POST.get('proposed_date') or '').strip())
    if proposed is None:
        return _back('Please provide a valid date.')
    if proposed < timezone.localdate():
        return _back('The new due date cannot be in the past.')

    profile = request.user.profile
    now = timezone.now()
    with transaction.atomic():
        # Stand the old row down BEFORE inserting, or the partial unique constraint
        # (one is_current row per assignment) rejects the insert. If an extension request
        # was pending, this supersedes it — the Head has ruled on the date by setting it.
        assignment.due_date_commitments.filter(is_current=True).update(is_current=False)
        DueDateCommitment.objects.create(
            assignment=assignment, proposed_date=proposed, proposed_by=profile,
            approved_by=profile, approved_at=now,
            change_reason=reason, is_current=True)
        # Status untouched: the site stays in whatever stage the work is actually in.
        log_activity(project, profile,
                     f'Agreed due date changed to {proposed}: {reason}',
                     entity_type='DesignAssignment', entity_id=assignment.pk,
                     action_code='design_due_date_changed')

    return _back(f'{project.project_id}: due date is now {proposed}.', ok=True)


# ---------------------------------------------------------------------------
# 5. Blocked flag
# ---------------------------------------------------------------------------

@login_required
def design_mark_blocked(request, project_id):
    """The allocated DESIGNER marks the site blocked on an inadequate survey.

    This stops their clock and surfaces on the Head's screen. Resolution happens by
    conversation; the flag is the record. Cleared by the Head uploading a replacement
    survey (see design_survey_upload), which restores the previous status along with the
    allocation and any approved due date.
    """
    project = _opex_site(project_id)
    assignment = getattr(project, 'design_assignment', None)
    if assignment is None or not user_is_assigned_designer(request.user, assignment):
        return HttpResponseForbidden('Only the designer allocated to this site may flag it.')
    if request.method != 'POST':
        return redirect('design_my_sites')

    if assignment.status == DESIGN_SURVEY_RETURNED:
        return _deny(request, f'{project.project_id} is already on Design Hold.',
                     'design_my_sites')

    reason = (request.POST.get('reason') or '').strip()
    if not reason:
        return _deny(request, 'Please say what is inadequate about the survey.',
                     'design_my_sites')

    profile = request.user.profile
    with transaction.atomic():
        assignment.survey_returned_at    = timezone.now()
        assignment.survey_returned_by    = profile
        assignment.survey_return_reason  = reason
        assignment.status = DESIGN_SURVEY_RETURNED
        assignment.save()
        log_activity(project, profile, f'Site placed on Design Hold — survey inadequate: {reason}',
                     entity_type='DesignAssignment', entity_id=assignment.pk,
                     action_code='design_blocked')

    messages.success(request, f'{project.project_id} placed on Design Hold. The Design Head '
                              f'can see the reason and your clock is stopped.')
    return redirect('design_my_sites')


# ===========================================================================
# PART 3 — Arka, CAD, BOQ and versioning
# ===========================================================================
#
# THE ONE RULE THIS SECTION EXISTS TO ENFORCE
# -------------------------------------------
# No CAD file and no BOQ submission may exist until the CURRENT Arka version has
# verdict='approved', and every artifact records WHICH Arka version it was drawn
# against (DesignFile.derived_from_arka, NOT NULL from Part 1).
#
# Both halves are enforced HERE, in the view, not by hiding a button: every write
# endpoint below calls _require_approved_arka() before it touches storage or the
# database, so a direct POST to a bare URL is refused exactly as a click would be.
# Verified by direct POST — see the session's verification run.
#
# WHY THE PAIRING MATTERS: CAD and BOQ both derive from the approved layout. A CAD
# drawn against a superseded Arka is rework that QC has no way to detect once the
# versions have moved on, so the version it was built from is recorded at write time
# rather than inferred later from timestamps.
#
# ATTEMPTS ARE OPENED LAZILY. Part 2 left `current_attempt_number` at 0 and created
# no DesignAttempt rows, so the first Arka submission opens attempt 1
# (opened_reason='initial'). Attempts 2+ are opened by a QC failure or a PM change
# request, which are Parts 4 and 5 — nothing here ever opens a second attempt.


# ---------------------------------------------------------------------------
# Part 3 helpers
# ---------------------------------------------------------------------------

def _current_attempt(assignment):
    """The attempt design work is currently happening on, or None before the first
    Arka submission. Read from `current_attempt_number` rather than "the latest row",
    so the pointer the rest of the module maintains is the single source of truth."""
    if not assignment.current_attempt_number:
        return None
    return assignment.attempts.filter(
        attempt_number=assignment.current_attempt_number).first()


def _open_first_attempt(assignment):
    """Open attempt 1 for an assignment that has none, and move the pointer.

    Callers own the transaction. Only ever creates attempt 1: a second attempt is a
    rework loop (QC failure / PM change request) and belongs to a later part.
    """
    attempt = DesignAttempt.objects.create(
        assignment=assignment, attempt_number=1,
        opened_reason=ATTEMPT_REASON_INITIAL,
    )
    assignment.current_attempt_number = 1
    assignment.save(update_fields=['current_attempt_number', 'updated_at'])
    return attempt


def _current_arka(attempt):
    """The live Arka version for an attempt. The partial unique constraint from Part 1
    guarantees there is at most one."""
    if attempt is None:
        return None
    return attempt.arka_submissions.filter(is_current=True).first()


def _approved_arka(attempt):
    """The current Arka ONLY IF it has cleared BOTH gates. This is the object CAD and BOQ
    artifacts pair to — deliberately not "the most recently approved version", so a
    superseded approval can never become the parent of a new artifact.

    PART 9 MOVED THE TEST FROM `verdict` TO `head_verdict`, and that is the whole change
    to the Part 3 gate. `verdict='approved'` now means only that Design QC passed it; an
    Arka the Head has not yet approved is not a layout anybody may build against, so CAD
    and BOQ stay locked until the second gate clears. Because the serial rule guarantees
    head_verdict can only be 'approved' after verdict is, testing the Head field alone is
    sufficient and there is no need to test both.
    """
    arka = _current_arka(attempt)
    if arka is not None and arka.head_verdict == ARKA_APPROVED:
        return arka
    return None


def _require_approved_arka(attempt):
    """Return the fully approved current Arka, or raise ValueError with the message the
    designer needs to see.

    Single chokepoint for settled decision 1. Every artifact write path calls this; none
    of them re-derives the rule, so the call sites cannot drift.

    The messages name WHICH gate is outstanding, because "waiting for approval" is not
    actionable when there are two approvers — the designer needs to know whether to chase
    Design QC or the Design Head.
    """
    if attempt is None:
        raise ValueError('no design attempt has been opened yet — submit an Arka first')
    arka = _current_arka(attempt)
    if arka is None:
        raise ValueError('no Arka has been submitted yet. CAD and BOQ can only be '
                         'uploaded against an approved Arka')
    if arka.verdict == ARKA_PENDING:
        raise ValueError(f'Arka v{arka.version} is still awaiting the Design QC review. '
                         f'CAD and BOQ cannot be uploaded until it is approved')
    if arka.verdict == ARKA_REJECTED:
        raise ValueError(f'Arka v{arka.version} was rejected at Design QC. Submit a new '
                         f'Arka version and have it approved before uploading CAD or BOQ')
    if arka.head_verdict == ARKA_PENDING:
        raise ValueError(f'Arka v{arka.version} passed Design QC but is still awaiting '
                         f'the Design Head\'s approval. CAD and BOQ cannot be uploaded '
                         f'until the Head has approved it')
    if arka.head_verdict == ARKA_REJECTED:
        raise ValueError(f'Arka v{arka.version} was rejected by the Design Head. Submit a '
                         f'new Arka version and have it approved before uploading CAD or BOQ')
    return arka


# ---------------------------------------------------------------------------
# Part 9 — shared gate enforcement
# ---------------------------------------------------------------------------

def _other_gate_actor_conflict(profile, other_gate_reviewer_id):
    """Settled decision 2: one person cannot record BOTH verdicts on the same artifact.

    `other_gate_reviewer_id` is the *_reviewed_by_id already stored by the opposite gate
    on this exact row. Returns True if it is this same person, which is the refusal case.

    PER ARTIFACT, NOT PER USER. Somebody holding both flags is entirely legitimate and is
    refused only the SECOND verdict on an artifact they have already ruled on — they may
    record either gate's verdict on any other site, and may record the Head verdict here
    if a different person passed it through Design QC. A flag says what you may be; this
    says what you may not do twice.

    A null reviewer (no verdict recorded at that gate yet, or a historical row backfilled
    by migration 0055 with head_reviewed_by left null) compares False and admits the
    actor — correct in both cases, because no person is being asked to agree with
    themselves.
    """
    if profile is None or other_gate_reviewer_id is None:
        return False
    return other_gate_reviewer_id == profile.pk


def _posted_error_category(request):
    """Return (category, error_message). The category is MANDATORY on every rejection and
    every failure, at BOTH gates (settled decision 7).

    Validated against DESIGN_ERROR_CATEGORIES rather than trusted, because the field is
    not CHECK-constrained (see the note on DesignAttempt.head_failure_category) — this
    function is the only thing standing between a hand-crafted POST and an uncountable
    value in a column the rework multiplier reads.

    ONE category, never several. A multi-select would let a reviewer tag a rejection with
    four causes, and a rejection with four causes cannot be counted under any of them.
    """
    category = (request.POST.get('error_category') or '').strip()
    if not category:
        return '', ('an error category is required — "rejected" on its own cannot be '
                    'counted, coached against, or told apart from a bad survey')
    if category not in DESIGN_ERROR_CATEGORIES:
        return '', 'that error category is not recognised — please choose one from the list'
    return category, ''


def _maybe_advance_to_artifacts_uploaded(assignment, attempt, actor):
    """Section 5 — evaluate the progression rule after every Part 3 write.

    The attempt moves `arka_submitted` -> `artifacts_uploaded` once it has ALL THREE:
    an approved current Arka, a current cad_zip, and boq_submitted_at set.

    PART 8 TIGHTENED THE CAD CONDITION from "at least one CAD file" to "a valid cad_zip".
    Validity is established at upload — a row of kind cad_zip only exists because
    `_validate_cad_zip` accepted it — so this is a presence check, not a re-validation.
    A legacy cad_pdf/cad_dwg does NOT satisfy it; see PROGRESSION_CAD_KINDS.

    Called explicitly at the end of each action rather than wired to a signal, so the
    transition is visible next to the write that could have caused it. Idempotent —
    it only fires from `arka_submitted`, so calling it twice does nothing the second
    time. Returns True if it advanced.

    Deliberately does NOT continue on to `in_qc`; handing the package to QC is Part 4.
    """
    if assignment.status != DESIGN_ARKA_SUBMITTED:
        return False
    if attempt is None or _approved_arka(attempt) is None:
        return False
    if not attempt.design_files.filter(
            kind__in=PROGRESSION_CAD_KINDS, is_current=True).exists():
        return False
    if attempt.boq_submitted_at is None:
        return False

    assignment.status = DESIGN_ARTIFACTS_UPLOADED
    assignment.save(update_fields=['status', 'updated_at'])
    log_activity(assignment.project, actor,
                 f'Design package complete on attempt {attempt.attempt_number} — '
                 f'approved Arka, CAD and BOQ all present',
                 entity_type='DesignAttempt', entity_id=attempt.pk,
                 action_code='design_artifacts_uploaded')
    return True


def _attempt_files(attempt):
    """Every DesignFile on an attempt, newest version of each kind first, with the Arka
    version it derives from preloaded — the pairing is what the screens exist to show."""
    if attempt is None:
        return []
    return list(attempt.design_files
                .select_related('derived_from_arka', 'uploaded_by__user')
                .order_by('kind', '-version'))


def _designer_boq(project):
    """The project's BOQ, or None. Read-only — this module never creates, seeds or
    writes a BOQ row (settled decision 4); `boq_detail` owns all of that."""
    try:
        return project.boq
    except BOQ.DoesNotExist:
        return None


# ---------------------------------------------------------------------------
# 6. Screens
# ---------------------------------------------------------------------------

def _workspace_context(project, assignment):
    """Shared context for both Part 3 screens, so the designer and the Head are looking
    at the same computed truth rather than two templates deriving it separately."""
    attempt = _current_attempt(assignment)
    arka    = _current_arka(attempt)
    boq     = _designer_boq(project)
    files   = _attempt_files(attempt)
    return {
        'project':        project,
        'assignment':     assignment,
        'attempt':        attempt,
        'arka':           arka,
        'arka_approved':  _approved_arka(attempt) is not None,
        'arka_history':   (list(attempt.arka_submissions.select_related(
                               'submitted_by__user', 'reviewed_by__user',
                               'head_reviewed_by__user')
                               .order_by('-version')) if attempt else []),
        'files':          files,
        # Reports the PROGRESSION rule, not "any CAD-ish file exists" — a chip saying
        # "CAD: Uploaded" while the gate still refuses to advance would be a lie the
        # designer cannot debug. Legacy files still LIST above; they just do not tick
        # this chip. See PROGRESSION_CAD_KINDS.
        'has_cad':        any(f.kind in PROGRESSION_CAD_KINDS and f.is_current
                              for f in files),
        'boq':            boq,
        'boq_complete':   bool(attempt and attempt.boq_submitted_at),
        'cad_kinds':      [(k, KIND_LABELS[k]) for k in UPLOADABLE_KINDS],
        'status':         assignment.status,
    }


@login_required
def design_site_workspace(request, project_id):
    """The designer's per-site design screen: submit an Arka, read the rejection
    reason, upload CAD, see every uploaded version with the Arka it derives from,
    reach BOQ entry and mark the BOQ complete.

    Read access is the allocated designer or the Design Head — the Head needs to see
    exactly what the designer sees when a question comes up. Every ACTION on the
    screen is separately gated in its own view; being able to load this page confers
    nothing.
    """
    project = _opex_site(project_id)
    assignment = getattr(project, 'design_assignment', None)
    if assignment is None:
        raise Http404('No design assignment for this site.')

    is_designer = user_is_assigned_designer(request.user, assignment)
    if not (is_designer or user_has_design_head_authority(request.user)):
        return HttpResponseForbidden('Only the allocated designer or the Design Head '
                                     'may open this site\'s design workspace.')

    boq_group_locked = project_boq_is_group_locked(project)

    ctx = _workspace_context(project, assignment)
    ctx.update({
        'is_designer':  is_designer,
        # Part 4: the designer reads the QC verdict, the QC remarks and any PM change
        # request off the shared attempt-history partial, so they see the same record of
        # what happened as the Head does on the QC screen.
        'history':      _attempt_history(assignment),
        'can_submit_arka': is_designer and assignment.status in ARKA_SUBMITTABLE_STATUSES,
        # The BOQ link is only useful if the existing Part 0.6 gate lets this user in.
        # Surfaced rather than hidden: a designer allocated to a site whose
        # `assigned_design` names someone else is locked out of BOQ entry, and being
        # told so beats a bare 403 from boq_detail.
        # Part 6: the same caller-side AND the BOQ views apply. A designer whose site has
        # been picked up into a locked procurement group is not locked out by authority —
        # the quantities are simply final, and the banner below says which group froze them.
        'can_edit_boq': (user_can_edit_project_boq(request.user, project)
                         and not boq_group_locked),
        'boq_group_locked': boq_group_locked,
    })
    return render(request, 'projects/design/site_workspace.html', ctx)


@login_required
def design_head_review(request, project_id):
    """The Arka review screen for one site, serving BOTH gates (Part 9).

    One screen rather than two, because the reviewers are looking at exactly the same
    thing — the Arka, its capacity, its link and its version history. What differs is
    which action they may take, and that is decided per gate below and rendered as at most
    one action block. A user with neither authority gets the read-only view if they can
    see the site at all.

    `can_qc_verdict` and `can_head_verdict` are mutually exclusive by construction: they
    require different statuses. Each additionally carries the settled-decision-2 check, so
    a dual-flag holder who recorded gate 1 sees no gate-2 form — the refusal is visible
    before the click, and re-checked server-side when it happens anyway.
    """
    project = _opex_site(project_id)
    assignment = getattr(project, 'design_assignment', None)
    if assignment is None:
        raise Http404('No design assignment for this site.')

    can_qc_gate   = user_can_qc_gate_design(request.user, assignment)
    can_head_gate = user_can_head_gate_design(request.user, assignment)
    if not (can_qc_gate or can_head_gate or user_has_design_head_authority(request.user)):
        return HttpResponseForbidden(
            'The Arka review screen is for Design QC, the Design Head, or his named deputy.')

    ctx = _workspace_context(project, assignment)
    arka = ctx['arka']
    profile = getattr(request.user, 'profile', None)

    # Gate 1 — Design QC owes a verdict.
    ctx['can_qc_verdict'] = bool(
        can_qc_gate
        and arka is not None
        and arka.verdict == ARKA_PENDING
        and assignment.status == DESIGN_ARKA_SUBMITTED
        and not _other_gate_actor_conflict(profile, arka.head_reviewed_by_id)
    )
    # Gate 2 — the Head owes a verdict on what QC passed.
    ctx['can_head_verdict'] = bool(
        can_head_gate
        and arka is not None
        and arka.verdict == ARKA_APPROVED
        and arka.head_verdict == ARKA_PENDING
        and assignment.status == DESIGN_AWAITING_HEAD_ARKA
        and not _other_gate_actor_conflict(profile, arka.reviewed_by_id)
    )
    # Rendered as an explanation when the Head is looking at an Arka he passed through
    # Design QC himself. Without it the screen simply shows no buttons and he has no way
    # to tell "already decided" from "you are refused, and here is why".
    ctx['blocked_by_own_qc_verdict'] = bool(
        can_head_gate
        and arka is not None
        and assignment.status == DESIGN_AWAITING_HEAD_ARKA
        and arka.head_verdict == ARKA_PENDING
        and _other_gate_actor_conflict(profile, arka.reviewed_by_id)
    )
    ctx['error_categories'] = DESIGN_ERROR_CATEGORY_CHOICES
    ctx['is_design_qc']     = can_qc_gate
    return render(request, 'projects/design/head_review.html', ctx)


# ---------------------------------------------------------------------------
# 7. Arka submission
# ---------------------------------------------------------------------------

@login_required
def design_arka_submit(request, project_id):
    """The allocated DESIGNER submits an Arka version.

    Permission is `assignment.assigned_to == profile` and nobody else — explicitly
    including the Design Head, who reviews these and must not be able to author one.

    VERSIONING: version is max(version for this attempt) + 1, starting at 1, and any
    previous submission is flipped to is_current=False BEFORE the insert, in the same
    transaction. The partial unique constraint `uniq_current_arka_per_attempt` rejects
    the insert otherwise — the ordering is load-bearing, not stylistic.

    A rejected version stays is_current=True until this replacement lands, so the
    designer can read what was rejected and why right up to the moment they fix it.
    """
    project = _opex_site(project_id)
    assignment = getattr(project, 'design_assignment', None)
    if assignment is None or not user_is_assigned_designer(request.user, assignment):
        return HttpResponseForbidden('Only the designer allocated to this site may '
                                     'submit an Arka.')
    if request.method != 'POST':
        return redirect('design_site_workspace', project_id=project.project_id)

    def _back(msg, ok=False):
        (messages.success if ok else messages.error)(request, msg)
        return redirect('design_site_workspace', project_id=project.project_id)

    if assignment.status not in ARKA_SUBMITTABLE_STATUSES:
        return _back(f'{project.project_id}: an Arka can only be submitted while the '
                     f'site is in design or its previous Arka was rejected '
                     f'(this site is "{assignment.get_status_display()}").')

    # ── capacity: recorded, NOT validated against anything (settled decision 5) ──
    # Positive-number validation only. There is deliberately no comparison against
    # tendered capacity, no mismatch gate and no rollup — that is Part 5 reporting and
    # a separate commercial decision.
    raw_capacity = (request.POST.get('capacity_kw') or '').strip()
    try:
        capacity = Decimal(raw_capacity)
    except (InvalidOperation, ValueError):
        return _back('Please enter the designed capacity in kW as a number.')
    if capacity <= 0:
        return _back('Designed capacity must be greater than zero.')

    arka_link = (request.POST.get('arka_link') or '').strip()
    if not arka_link:
        return _back('An Arka link is required.')
    try:
        URLValidator()(arka_link)
    except ValidationError:
        return _back('Please enter a valid Arka link (a full URL, including https://).')

    profile = request.user.profile
    with transaction.atomic():
        attempt = _current_attempt(assignment)
        if attempt is None:
            attempt = _open_first_attempt(assignment)

        # Stand the previous version down BEFORE inserting — see the docstring.
        attempt.arka_submissions.filter(is_current=True).update(is_current=False)
        next_version = (attempt.arka_submissions.aggregate(
            m=Max('version'))['m'] or 0) + 1

        arka = ArkaSubmission.objects.create(
            attempt=attempt, version=next_version,
            capacity_kw=capacity, arka_link=arka_link,
            submitted_by=profile, verdict=ARKA_PENDING, is_current=True,
        )
        assignment.status = DESIGN_ARKA_SUBMITTED
        assignment.save(update_fields=['status', 'updated_at'])
        log_activity(project, profile,
                     f'Arka v{arka.version} submitted ({capacity} kW) for approval',
                     entity_type='ArkaSubmission', entity_id=arka.pk,
                     action_code='design_arka_submitted')

    return _back(f'{project.project_id}: Arka v{next_version} submitted — awaiting '
                 f'Design Head approval.', ok=True)


# ---------------------------------------------------------------------------
# 8. Arka verdict
# ---------------------------------------------------------------------------

def _verdict_target(request, project):
    """The Arka a DESIGN QC (gate 1) verdict may be recorded against, or an error message.

    Shared by the QC approve and reject views so the two cannot disagree about which
    submission is reviewable. The designer cannot reach either endpoint: both are gated on
    `user_can_qc_gate_design`, which refuses the assigned designer.
    """
    assignment = getattr(project, 'design_assignment', None)
    if assignment is None:
        return None, None, f'{project.project_id}: no design assignment for this site.'
    if assignment.status != DESIGN_ARKA_SUBMITTED:
        return None, None, (f'{project.project_id}: there is no Arka awaiting a Design QC '
                            f'verdict (status "{assignment.get_status_display()}").')
    attempt = _current_attempt(assignment)
    arka = _current_arka(attempt)
    if arka is None:
        return None, None, f'{project.project_id}: no current Arka submission found.'
    if arka.verdict != ARKA_PENDING:
        return None, None, (f'{project.project_id}: Arka v{arka.version} has already '
                            f'been {arka.get_verdict_display().lower()} by Design QC.')
    return assignment, arka, None


def _head_verdict_target(request, project):
    """The Arka a DESIGN HEAD (gate 2) verdict may be recorded against, or an error message.

    THIS IS WHERE THE SERIAL RULE IS ENFORCED for the Arka. The status test does the work:
    `awaiting_head_arka` is reachable only from a Design QC approval, so a Head arriving
    before QC has ruled finds the site still at `arka_submitted` and is refused with a
    message naming what he is waiting for. The explicit `verdict != ARKA_APPROVED` test
    below is a belt-and-braces re-check against the row itself rather than the cached
    status, in the same spirit as _package_is_complete().
    """
    assignment = getattr(project, 'design_assignment', None)
    if assignment is None:
        return None, None, f'{project.project_id}: no design assignment for this site.'
    if assignment.status != DESIGN_AWAITING_HEAD_ARKA:
        return None, None, (
            f'{project.project_id}: there is no Arka awaiting your verdict — Design QC '
            f'must approve it first (status "{assignment.get_status_display()}").')
    attempt = _current_attempt(assignment)
    arka = _current_arka(attempt)
    if arka is None:
        return None, None, f'{project.project_id}: no current Arka submission found.'
    if arka.verdict != ARKA_APPROVED:
        return None, None, (f'{project.project_id}: Arka v{arka.version} has not been '
                            f'approved by Design QC, so there is nothing for you to '
                            f'countersign.')
    if arka.head_verdict != ARKA_PENDING:
        return None, None, (f'{project.project_id}: Arka v{arka.version} has already '
                            f'been {arka.get_head_verdict_display().lower()} by the '
                            f'Design Head.')
    return assignment, arka, None


@login_required
def design_arka_approve(request, project_id):
    """GATE 1 — DESIGN QC approves the current Arka version.

    Moves the site to `awaiting_head_arka`. It does NOT unlock CAD and BOQ upload: that
    now needs head_verdict='approved' (see _approved_arka), which is the Part 9 change to
    the Part 3 gate.
    """
    project = _opex_site(project_id)
    assignment_pre = getattr(project, 'design_assignment', None)
    if not user_can_qc_gate_design(request.user, assignment_pre):
        return HttpResponseForbidden(
            'The Arka Design QC review is for a Design QC reviewer, and never for the '
            'designer allocated to this site.')
    if request.method != 'POST':
        return redirect('design_head_review', project_id=project.project_id)

    assignment, arka, error = _verdict_target(request, project)
    if error:
        messages.error(request, error)
        return redirect('design_head_review', project_id=project.project_id)

    profile = request.user.profile
    # Decision 2, gate 1 side: refuse if this same person already recorded the HEAD
    # verdict on this Arka. Unreachable through the UI (the Head verdict cannot be first)
    # but checked anyway — every gate enforces the rule from its own side, so neither
    # depends on the other having run.
    if _other_gate_actor_conflict(profile, arka.head_reviewed_by_id):
        messages.error(request, f'{project.project_id}: you have already recorded the '
                                f'Design Head verdict on Arka v{arka.version}. Two '
                                f'verdicts by one person is not a second review.')
        return redirect('design_head_review', project_id=project.project_id)

    with transaction.atomic():
        arka.verdict     = ARKA_APPROVED
        arka.reviewed_by = profile
        arka.reviewed_at = timezone.now()
        arka.save(update_fields=['verdict', 'reviewed_by', 'reviewed_at'])
        assignment.status = DESIGN_AWAITING_HEAD_ARKA
        assignment.save(update_fields=['status', 'updated_at'])
        log_activity(project, profile,
                     f'Arka v{arka.version} passed Design QC ({arka.capacity_kw} kW) — '
                     f'awaiting Design Head approval',
                     entity_type='ArkaSubmission', entity_id=arka.pk,
                     action_code='design_arka_qc_approved')

    messages.success(request, f'{project.project_id}: Arka v{arka.version} passed Design '
                              f'QC — it now needs the Design Head\'s approval before the '
                              f'designer can upload CAD or enter the BOQ.')
    return redirect('design_head_review', project_id=project.project_id)


@login_required
def design_arka_reject(request, project_id):
    """GATE 1 — DESIGN QC rejects the current Arka version.

    A reason AND an error category are both MANDATORY (settled decision 7). The reason is
    checked here so the designer gets a usable message, and the Part 1 CHECK constraint
    `rejection_reason_required_when_rejected` enforces the same rule at the database level
    for any writer that bypasses this view. The category is view-enforced only — see the
    note on DesignAttempt.head_failure_category for why it is not a CHECK.

    The rejected submission stays is_current=True — it is the record of what was
    rejected — and is stood down only when the designer submits the replacement.
    """
    project = _opex_site(project_id)
    assignment_pre = getattr(project, 'design_assignment', None)
    if not user_can_qc_gate_design(request.user, assignment_pre):
        return HttpResponseForbidden(
            'The Arka Design QC review is for a Design QC reviewer, and never for the '
            'designer allocated to this site.')
    if request.method != 'POST':
        return redirect('design_head_review', project_id=project.project_id)

    assignment, arka, error = _verdict_target(request, project)
    if error:
        messages.error(request, error)
        return redirect('design_head_review', project_id=project.project_id)

    profile = request.user.profile
    if _other_gate_actor_conflict(profile, arka.head_reviewed_by_id):
        messages.error(request, f'{project.project_id}: you have already recorded the '
                                f'Design Head verdict on Arka v{arka.version}. Two '
                                f'verdicts by one person is not a second review.')
        return redirect('design_head_review', project_id=project.project_id)

    reason = (request.POST.get('rejection_reason') or '').strip()
    if not reason:
        messages.error(request, f'{project.project_id}: a rejection reason is required '
                                f'— the designer cannot act on "rejected" alone.')
        return redirect('design_head_review', project_id=project.project_id)

    category, cat_error = _posted_error_category(request)
    if cat_error:
        messages.error(request, f'{project.project_id}: {cat_error}.')
        return redirect('design_head_review', project_id=project.project_id)

    with transaction.atomic():
        arka.verdict             = ARKA_REJECTED
        arka.rejection_reason    = reason
        arka.qc_failure_category = category
        arka.reviewed_by         = profile
        arka.reviewed_at         = timezone.now()
        arka.save(update_fields=['verdict', 'rejection_reason', 'qc_failure_category',
                                 'reviewed_by', 'reviewed_at'])
        assignment.status = DESIGN_ARKA_REJECTED
        assignment.save(update_fields=['status', 'updated_at'])
        log_activity(project, profile,
                     f'Arka v{arka.version} rejected at Design QC '
                     f'[{DESIGN_ERROR_CATEGORY_LABELS.get(category, category)}]: {reason}',
                     entity_type='ArkaSubmission', entity_id=arka.pk,
                     action_code='design_arka_qc_rejected')

    messages.success(request, f'{project.project_id}: Arka v{arka.version} rejected at '
                              f'Design QC — the designer has been asked to submit a new '
                              f'version.')
    return redirect('design_head_review', project_id=project.project_id)


@login_required
def design_arka_head_approve(request, project_id):
    """GATE 2 — the DESIGN HEAD approves an Arka that Design QC has already passed.

    THIS is the approval that unlocks CAD and BOQ upload. Status returns to
    `arka_submitted`, which carries head_verdict='approved' and therefore classifies as
    "Arka approved, artifacts incomplete" — see the Part 9 note at the top of this module
    for why no new status is invented for that state.
    """
    project = _opex_site(project_id)
    assignment_pre = getattr(project, 'design_assignment', None)
    if not user_can_head_gate_design(request.user, assignment_pre):
        return HttpResponseForbidden(
            'The Design Head Arka review is for the Design Head or his named deputy, and '
            'never for the designer allocated to this site.')
    if request.method != 'POST':
        return redirect('design_head_review', project_id=project.project_id)

    assignment, arka, error = _head_verdict_target(request, project)
    if error:
        messages.error(request, error)
        return redirect('design_head_review', project_id=project.project_id)

    profile = request.user.profile
    if _other_gate_actor_conflict(profile, arka.reviewed_by_id):
        messages.error(request, f'{project.project_id}: you recorded the Design QC verdict '
                                f'on Arka v{arka.version} yourself, so you cannot also '
                                f'countersign it as Design Head. It needs a second pair '
                                f'of eyes.')
        return redirect('design_head_review', project_id=project.project_id)

    with transaction.atomic():
        arka.head_verdict     = ARKA_APPROVED
        arka.head_reviewed_by = profile
        arka.head_reviewed_at = timezone.now()
        # QC approved and the Head approved — the gates agree, so no overturn.
        arka.head_overturned_qc = False
        arka.save(update_fields=['head_verdict', 'head_reviewed_by', 'head_reviewed_at',
                                 'head_overturned_qc'])
        assignment.status = DESIGN_ARKA_SUBMITTED
        assignment.save(update_fields=['status', 'updated_at'])
        log_activity(project, profile,
                     f'Arka v{arka.version} approved by the Design Head '
                     f'({arka.capacity_kw} kW)',
                     entity_type='ArkaSubmission', entity_id=arka.pk,
                     action_code='design_arka_head_approved')
        # A re-approval on an attempt that already carries CAD and a submitted BOQ would
        # otherwise leave the status behind; evaluating here costs one query.
        _maybe_advance_to_artifacts_uploaded(
            assignment, _current_attempt(assignment), profile)

    messages.success(request, f'{project.project_id}: Arka v{arka.version} approved — '
                              f'the designer can now upload CAD and enter the BOQ.')
    return redirect('design_head_review', project_id=project.project_id)


@login_required
def design_arka_head_reject(request, project_id):
    """GATE 2 — the DESIGN HEAD rejects an Arka that Design QC passed.

    Reason and category are both mandatory, exactly as at gate 1, and the site returns to
    the designer identically (settled decision 5). The two rejections are RECORDED
    distinctly — different columns, different action_code — so "how often does the Head
    reject what QC passed" stays answerable.

    THIS IS THE OVERTURN CASE for an Arka: QC approved it and the Head did not, so
    head_overturned_qc is set. See the note on the field for why it is stored rather than
    derived.
    """
    project = _opex_site(project_id)
    assignment_pre = getattr(project, 'design_assignment', None)
    if not user_can_head_gate_design(request.user, assignment_pre):
        return HttpResponseForbidden(
            'The Design Head Arka review is for the Design Head or his named deputy, and '
            'never for the designer allocated to this site.')
    if request.method != 'POST':
        return redirect('design_head_review', project_id=project.project_id)

    assignment, arka, error = _head_verdict_target(request, project)
    if error:
        messages.error(request, error)
        return redirect('design_head_review', project_id=project.project_id)

    profile = request.user.profile
    if _other_gate_actor_conflict(profile, arka.reviewed_by_id):
        messages.error(request, f'{project.project_id}: you recorded the Design QC verdict '
                                f'on Arka v{arka.version} yourself, so you cannot also '
                                f'rule on it as Design Head. It needs a second pair of eyes.')
        return redirect('design_head_review', project_id=project.project_id)

    reason = (request.POST.get('rejection_reason') or '').strip()
    if not reason:
        messages.error(request, f'{project.project_id}: a rejection reason is required '
                                f'— the designer cannot act on "rejected" alone.')
        return redirect('design_head_review', project_id=project.project_id)

    category, cat_error = _posted_error_category(request)
    if cat_error:
        messages.error(request, f'{project.project_id}: {cat_error}.')
        return redirect('design_head_review', project_id=project.project_id)

    with transaction.atomic():
        arka.head_verdict           = ARKA_REJECTED
        arka.head_rejection_reason  = reason
        arka.head_failure_category  = category
        arka.head_reviewed_by       = profile
        arka.head_reviewed_at       = timezone.now()
        # QC approved, the Head rejected — the gates disagree. This is the signal
        # settled decision 6 exists to capture.
        arka.head_overturned_qc     = True
        arka.save(update_fields=['head_verdict', 'head_rejection_reason',
                                 'head_failure_category', 'head_reviewed_by',
                                 'head_reviewed_at', 'head_overturned_qc'])
        assignment.status = DESIGN_ARKA_REJECTED
        assignment.save(update_fields=['status', 'updated_at'])
        log_activity(project, profile,
                     f'Arka v{arka.version} rejected by the Design Head, overturning '
                     f'Design QC '
                     f'[{DESIGN_ERROR_CATEGORY_LABELS.get(category, category)}]: {reason}',
                     entity_type='ArkaSubmission', entity_id=arka.pk,
                     action_code='design_arka_head_rejected')

    messages.success(request, f'{project.project_id}: Arka v{arka.version} rejected — the '
                              f'designer has been asked to submit a new version, and the '
                              f'Design QC approval has been recorded as overturned.')
    return redirect('design_head_review', project_id=project.project_id)


# ---------------------------------------------------------------------------
# 9. CAD and BOQ artifact upload
# ---------------------------------------------------------------------------

@login_required
def design_artifact_upload(request, project_id):
    """The allocated DESIGNER uploads a CAD (pdf/dwg) or optional BOQ (xlsx/pdf) file.

    REFUSED unless the current Arka is approved — settled decision 1, enforced here and
    not by the template. A direct POST to this URL with an unapproved Arka gets the same
    refusal a hidden button would have prevented.

    PAIRING: derived_from_arka is set to the value _require_approved_arka() returns,
    which is the CURRENT approved version. It is never read off an older submission and
    never inferred from timestamps.

    VERSIONING is per (attempt, kind). Re-uploading a kind creates version N+1, flips
    the previous row to is_current=False and sets its superseded_by to the new row —
    which is why the new row is created first and the old one updated second.

    ORDERING: the file goes to storage BEFORE the transaction opens, exactly as the
    Part 2 survey upload does, so a storage failure aborts before any row is written
    and no DesignFile can ever point at an object that does not exist.
    """
    project = _opex_site(project_id)
    assignment = getattr(project, 'design_assignment', None)
    if assignment is None or not user_is_assigned_designer(request.user, assignment):
        return HttpResponseForbidden('Only the designer allocated to this site may '
                                     'upload design artifacts.')
    if request.method != 'POST':
        return redirect('design_site_workspace', project_id=project.project_id)

    def _back(msg, ok=False):
        (messages.success if ok else messages.error)(request, msg)
        return redirect('design_site_workspace', project_id=project.project_id)

    kind = (request.POST.get('kind') or '').strip()
    if kind not in UPLOADABLE_KINDS:
        return _back('Choose which kind of file this is.')

    upload = request.FILES.get('artifact_file')
    if not upload:
        return _back('Please choose a file to upload.')

    attempt = _current_attempt(assignment)
    try:
        arka = _require_approved_arka(attempt)
    except ValueError as exc:
        return _back(f'{project.project_id}: {exc}.')

    # ARCHIVE VALIDATION BEFORE STORAGE (Part 8). A zip that is unreadable, oversized
    # when expanded, or missing a PDF or a DWG is refused here — so a rejected archive
    # never reaches the bucket and never leaves an orphaned object behind.
    listing = []
    if kind == DESIGN_FILE_CAD_ZIP:
        try:
            listing = validate_cad_zip(upload)
        except DesignStorageError as exc:
            return _back(f'{project.project_id}: {exc}')

    # `kind` is whitelisted above, so it is safe as a storage path segment.
    path = build_design_path(project.project_id, kind, upload.name)
    try:
        bucket, stored_path = upload_design_file(upload, path)
    except DesignStorageError as exc:
        return _back(f'{project.project_id}: {exc}')

    profile = request.user.profile
    with transaction.atomic():
        previous = attempt.design_files.filter(kind=kind, is_current=True).first()
        next_version = (attempt.design_files.filter(kind=kind)
                        .aggregate(m=Max('version'))['m'] or 0) + 1

        design_file = DesignFile.objects.create(
            attempt=attempt, kind=kind, version=next_version,
            bucket=bucket, path=stored_path,
            original_filename=(upload.name or '')[:255],
            size_bytes=getattr(upload, 'size', None),
            content_type=(getattr(upload, 'content_type', '') or '')[:100],
            archive_listing=listing,
            derived_from_arka=arka,
            uploaded_by=profile, is_current=True,
        )
        if previous is not None:
            previous.is_current    = False
            previous.superseded_by = design_file
            previous.save(update_fields=['is_current', 'superseded_by'])

        log_activity(project, profile,
                     f'{KIND_LABELS[kind]} v{next_version} uploaded, derived from '
                     f'Arka v{arka.version}',
                     entity_type='DesignFile', entity_id=design_file.pk,
                     action_code='design_artifact_uploaded')

        advanced = _maybe_advance_to_artifacts_uploaded(assignment, attempt, profile)

    msg = (f'{project.project_id}: {KIND_LABELS[kind]} v{next_version} uploaded '
           f'(derived from Arka v{arka.version}).')
    if advanced:
        msg += ' The design package is now complete.'
    return _back(msg, ok=True)


@login_required
def design_file_download(request, project_id, pk):
    """Redirect to a freshly-signed, short-lived URL for one DesignFile.

    Same shape as the Part 2 survey download: the URL is minted per request, never
    stored, and visibility is the ordinary project visibility rule. The file is looked
    up THROUGH this project's assignment, so a pk belonging to another site's attempt
    is a 404 rather than a signed link.
    """
    project = _opex_site(project_id)
    if not user_can_view_design(request.user, project):
        return HttpResponseForbidden('You do not have access to this site.')

    design_file = get_object_or_404(
        DesignFile, pk=pk, attempt__assignment__project=project)
    try:
        url = get_design_file_url(design_file.bucket, design_file.path)
    except DesignStorageError as exc:
        messages.error(request, str(exc))
        return redirect('design_site_workspace', project_id=project.project_id)
    if not url:
        raise Http404('No stored object for this file.')
    return redirect(url)


# ---------------------------------------------------------------------------
# 10. BOQ completion
# ---------------------------------------------------------------------------

@login_required
def design_boq_complete(request, project_id):
    """The allocated DESIGNER marks the BOQ complete for the current attempt.

    THE BOQ ITSELF IS NOT DUPLICATED (settled decision 4). Quantities live in the
    existing BOQ / BOQItem rows and are entered through the existing `boq_detail`
    screen under the Part 0.6 permission helpers, neither of which this module touches.
    All that happens here is that the ATTEMPT records boq_submitted_at /
    boq_submitted_by — the design workflow's own note that this step is done.

    REFUSED unless the current Arka is approved, for the same reason CAD is: a BOQ
    priced off an unapproved layout is rework.

    The "at least one quantity" guard reads the existing BOQ and mirrors the check
    `boq_detail`'s own submit branch applies (views.py — `boq_quantity__gt=0`), so this
    stamp cannot be set on an empty BOQ. It is a READ of BOQ rows; nothing here writes
    one.
    """
    project = _opex_site(project_id)
    assignment = getattr(project, 'design_assignment', None)
    if assignment is None or not user_is_assigned_designer(request.user, assignment):
        return HttpResponseForbidden('Only the designer allocated to this site may mark '
                                     'its BOQ complete.')
    if request.method != 'POST':
        return redirect('design_site_workspace', project_id=project.project_id)

    def _back(msg, ok=False):
        (messages.success if ok else messages.error)(request, msg)
        return redirect('design_site_workspace', project_id=project.project_id)

    attempt = _current_attempt(assignment)
    try:
        _require_approved_arka(attempt)
    except ValueError as exc:
        return _back(f'{project.project_id}: {exc}.')

    if attempt.boq_submitted_at is not None:
        return _back(f'{project.project_id}: the BOQ for attempt '
                     f'{attempt.attempt_number} is already marked complete.')

    boq = _designer_boq(project)
    if boq is None or not boq.items.filter(boq_quantity__gt=0).exists():
        return _back(f'{project.project_id}: enter a quantity for at least one BOQ item '
                     f'before marking the BOQ complete.')

    profile = request.user.profile
    with transaction.atomic():
        attempt.boq_submitted_at = timezone.now()
        attempt.boq_submitted_by = profile
        attempt.save(update_fields=['boq_submitted_at', 'boq_submitted_by'])
        log_activity(project, profile,
                     f'BOQ marked complete for attempt {attempt.attempt_number}',
                     entity_type='DesignAttempt', entity_id=attempt.pk,
                     action_code='design_boq_submitted')
        advanced = _maybe_advance_to_artifacts_uploaded(assignment, attempt, profile)

    msg = f'{project.project_id}: BOQ marked complete.'
    if advanced:
        msg += ' The design package is now complete.'
    return _back(msg, ok=True)


# ===========================================================================
# PART 4 — QC review, attempt lifecycle, deputy, PM change requests, release
# ===========================================================================
#
# TWO REWORK LOOPS, COUNTED SEPARATELY AND NEVER COLLAPSED
# --------------------------------------------------------
# A new attempt opens for exactly two reasons, and which one it was is the whole
# point of recording it:
#
#   opened_reason='qc_failed'          the package was wrong    — execution error
#   opened_reason='pm_change_request'  the brief changed        — moving requirement
#
# A tender whose rework is mostly `qc_failed` has a design quality problem. One whose
# rework is mostly `pm_change_request` has a requirements problem, and no amount of
# designer coaching will fix it. Merging the two fields would erase the only signal
# that tells them apart, so:
#
#   AN ATTEMPT CLOSED BY A PM CHANGE REQUEST KEEPS qc_verdict='pending'.
#   It was never judged. Writing 'failed' there would inflate the QC failure rate with
#   rework the designer did not cause. This is enforced in design_change_request()
#   below, which touches closed_at and nothing else on the outgoing attempt.
#
# WHO MAY DO WHAT
# ---------------
# QC is `user_can_qc_design()` — Head-or-deputy AND NOT the allocated designer. The
# self-QC exclusion is in the permission helper, not here, so the two QC entry points
# (start and verdict) cannot drift. Change requests are
# `user_can_request_design_change()`, which routes to the untouched
# `user_can_manage_project()`.


# ---------------------------------------------------------------------------
# 11. Attempt lifecycle — the one place a new attempt is opened
# ---------------------------------------------------------------------------

def _open_next_attempt(assignment, reason, actor, detail):
    """Close the current attempt and open the next one. THE ONLY PLACE THIS HAPPENS.

    Both rework loops call this with a different `reason`; the mechanics are identical
    and are deliberately written once. Duplicating them across the QC-fail and
    change-request views is exactly how the two would drift into disagreeing about
    which fields get set.

    Caller owns the transaction — both call sites already have one open for their own
    writes, and closing one attempt while failing to open the next would be the worst
    possible partial state.

    What it does NOT touch is as important as what it does:
      * `qc_verdict` on the outgoing attempt — the caller owns that. QC-fail sets it to
        'failed' before calling; a change request leaves it 'pending' forever.
      * `assigned_to`, the survey, and the approved DueDateCommitment — all preserved.
        Rework does not reopen the allocation or renegotiate the due date; the site
        goes back to the same designer under the same commitment (settled decision 2).

    Returns the new DesignAttempt.
    """
    now = timezone.now()
    current = _current_attempt(assignment)
    if current is not None and current.closed_at is None:
        current.closed_at = now
        current.save(update_fields=['closed_at'])

    next_number = (assignment.attempts.aggregate(m=Max('attempt_number'))['m'] or 0) + 1
    new_attempt = DesignAttempt.objects.create(
        assignment=assignment, attempt_number=next_number, opened_reason=reason,
    )

    assignment.current_attempt_number = next_number
    assignment.status = DESIGN_IN_DESIGN
    assignment.save(update_fields=['current_attempt_number', 'status', 'updated_at'])

    log_activity(assignment.project, actor,
                 f'Attempt {next_number} opened ({new_attempt.get_opened_reason_display()}): '
                 f'{detail}',
                 entity_type='DesignAttempt', entity_id=new_attempt.pk,
                 action_code=f'design_attempt_opened_{reason}')
    return new_attempt


def _open_change_requests(attempt):
    """Change requests on this attempt that have not yet produced a new attempt.

    An unresolved change request is one whose `resulting_attempt` is still null. It
    blocks a QC verdict: judging a package that is already known to need rework wastes
    the review and produces a verdict about a design nobody intends to build.
    """
    if attempt is None:
        return DesignChangeRequest.objects.none()
    return attempt.change_requests.filter(resulting_attempt__isnull=True)


def _package_is_complete(attempt):
    """Whether an attempt actually has a reviewable package: approved current Arka, a
    current cad_zip, and a BOQ marked complete.

    Same three conditions Part 3's _maybe_advance_to_artifacts_uploaded() evaluates,
    re-checked here at QC start rather than trusting the status alone — the status is a
    cached conclusion, these rows are the evidence. It must use the SAME kind tuple as
    that function or the two would disagree and a package could pass the gate that let it
    in and then fail the gate at QC.
    """
    if attempt is None:
        return False
    if _approved_arka(attempt) is None:
        return False
    if not attempt.design_files.filter(
            kind__in=PROGRESSION_CAD_KINDS, is_current=True).exists():
        return False
    return attempt.boq_submitted_at is not None


# ---------------------------------------------------------------------------
# 12. QC review
# ---------------------------------------------------------------------------

def _qc_guard(request, project, required_statuses, gate='qc'):
    """Shared entry checks for the five package-review endpoints, at either gate.

    Returns (assignment, attempt, error), where `error` is:
        None  -> refuse with 403. The caller has no authority at this gate.
        ''    -> proceed.
        str   -> refuse with this message and a redirect. Authorised, wrong state.

    `gate` selects the predicate: 'qc' -> user_can_qc_gate_design (the is_design_qc flag),
    'head' -> user_can_head_gate_design (Head authority or named deputy). Both refuse the
    assigned designer, so a designer reviewing their own package is refused identically at
    all five endpoints regardless of what flags they hold.

    ORDER IS DELIBERATE: authority is decided FIRST, before anything about the site's
    state is revealed. A user with no authority at this gate gets an identical 403 whether
    the site is mid-review, already released, or has no design assignment at all — the
    refusal never doubles as a state oracle.

    The SERIAL RULE is carried by `required_statuses`, not by a separate test: the Head's
    endpoints require `awaiting_head_qc`, which is reachable only from a Design QC pass.
    """
    assignment = getattr(project, 'design_assignment', None)

    # `assignment` may be None here; both predicates return False for that, which is the
    # correct answer — there is nothing to review and nobody may review it.
    allowed = (user_can_head_gate_design(request.user, assignment) if gate == 'head'
               else user_can_qc_gate_design(request.user, assignment))
    if not allowed:
        return None, None, None
    if assignment.status not in required_statuses:
        return assignment, None, (
            f'{project.project_id}: this review is not available at this stage '
            f'(status "{assignment.get_status_display()}").')
    return assignment, _current_attempt(assignment), ''


#: The 403 text for each gate. Kept together so the two refusals stay parallel and neither
#: leaks which gate the site is actually sitting at.
_GATE_FORBIDDEN = {
    'qc':   ('Package Design QC is for a Design QC reviewer, and never for the designer '
             'allocated to this site.'),
    'head': ('The Design Head package review is for the Design Head or his named deputy, '
             'and never for the designer allocated to this site.'),
}


@login_required
def design_qc_start(request, project_id):
    """GATE 1 — a DESIGN QC reviewer takes a completed package into review.

    `artifacts_uploaded` -> `in_qc`, and `qc_started_at` is stamped on the attempt.

    STAMPING qc_started_at IS WHAT OPENS THE PM CHANGE REQUEST WINDOW (settled
    decision 3). Before this moment a PM asking for a change is a conversation; after
    it, it is a system action that suspends the review and opens a new attempt.

    The three package conditions are re-checked from the rows rather than inferred from
    the status — a site could reach `artifacts_uploaded` and then have its only CAD file
    superseded by nothing, and QC should refuse that rather than review an empty package.
    """
    project = _opex_site(project_id)
    assignment, attempt, error = _qc_guard(request, project, (DESIGN_ARTIFACTS_UPLOADED,))
    if error is None:
        return HttpResponseForbidden(_GATE_FORBIDDEN['qc'])
    if request.method != 'POST':
        return redirect('design_qc_review', project_id=project.project_id)
    if error:
        messages.error(request, error)
        return redirect('design_qc_queue')

    if not _package_is_complete(attempt):
        messages.error(request, f'{project.project_id}: the package is incomplete — QC '
                                f'needs an approved Arka, at least one current CAD file '
                                f'and a BOQ marked complete.')
        return redirect('design_qc_queue')

    profile = request.user.profile
    with transaction.atomic():
        attempt.qc_started_at = timezone.now()
        attempt.save(update_fields=['qc_started_at'])
        assignment.status = DESIGN_IN_QC
        assignment.save(update_fields=['status', 'updated_at'])
        log_activity(project, profile,
                     f'QC started on attempt {attempt.attempt_number}',
                     entity_type='DesignAttempt', entity_id=attempt.pk,
                     action_code='design_qc_started')

    messages.success(request, f'{project.project_id}: QC started on attempt '
                              f'{attempt.attempt_number}. The PM can now raise a change '
                              f'request against this package.')
    return redirect('design_qc_review', project_id=project.project_id)


def _blocking_change_request(project, attempt):
    """The refusal message if an unresolved PM change request blocks a verdict, else ''.

    An unresolved change request means this package is already known to need rework, so
    judging it would record a verdict about a design nobody intends to build. Applies
    identically at BOTH gates, which is why it is a function rather than four copies.
    """
    if _open_change_requests(attempt).exists():
        return (f'{project.project_id}: a PM change request on this attempt is still '
                f'unresolved — it must be actioned before a verdict can be recorded.')
    return ''


@login_required
def design_qc_pass(request, project_id):
    """GATE 1 — DESIGN QC passes the package. THE SITE IS NOT RELEASED BY THIS.

    PART 9 CHANGED WHAT THIS DOES. In Part 4 a QC pass released the site; it now hands the
    package to the Design Head. `in_qc` -> `awaiting_head_qc`, `head_started_at` is
    stamped, and release happens at design_head_qc_pass() or not at all.

    The attempt is deliberately NOT closed here. It is still live — the Head has not
    ruled — and closing it would make the package look finished to every surface that
    reads closed_at.
    """
    project = _opex_site(project_id)
    assignment, attempt, error = _qc_guard(request, project, (DESIGN_IN_QC,))
    if error is None:
        return HttpResponseForbidden(_GATE_FORBIDDEN['qc'])
    if request.method != 'POST':
        return redirect('design_qc_review', project_id=project.project_id)
    if error:
        messages.error(request, error)
        return redirect('design_qc_queue')

    blocked = _blocking_change_request(project, attempt)
    if blocked:
        messages.error(request, blocked)
        return redirect('design_qc_review', project_id=project.project_id)

    profile = request.user.profile
    # Decision 2, gate 1 side. Unreachable through the UI but enforced from both sides.
    if _other_gate_actor_conflict(profile, attempt.head_reviewed_by_id):
        messages.error(request, f'{project.project_id}: you have already recorded the '
                                f'Design Head verdict on attempt {attempt.attempt_number}.')
        return redirect('design_qc_review', project_id=project.project_id)

    now = timezone.now()
    with transaction.atomic():
        attempt.qc_verdict     = QC_PASSED
        attempt.qc_reviewed_by = profile
        attempt.qc_reviewed_at = now
        attempt.head_started_at = now
        attempt.save(update_fields=['qc_verdict', 'qc_reviewed_by', 'qc_reviewed_at',
                                    'head_started_at'])
        assignment.status = DESIGN_AWAITING_HEAD_QC
        assignment.save(update_fields=['status', 'updated_at'])
        log_activity(project, profile,
                     f'Design QC passed attempt {attempt.attempt_number} — awaiting '
                     f'Design Head review',
                     entity_type='DesignAttempt', entity_id=attempt.pk,
                     action_code='design_qc_passed')

    messages.success(request, f'{project.project_id}: Design QC passed on attempt '
                              f'{attempt.attempt_number} — the package now needs the '
                              f'Design Head\'s review before the site can be released.')
    return redirect('design_qc_review', project_id=project.project_id)


@login_required
def design_qc_fail(request, project_id):
    """GATE 1 — DESIGN QC fails the package: the attempt closes and N+1 opens for rework.

    `qc_remarks` AND `qc_failure_category` are both mandatory (settled decision 7). The
    remarks are checked here so the reviewer gets a usable message, and enforced
    underneath by the Part 1 CHECK constraint `qc_remarks_required_when_qc_failed` for any
    writer that bypasses this view. The category is view-enforced only.

    A QC FAILURE ENDS THE ATTEMPT — the Head never sees it, and head_verdict stays
    'pending' forever on this row, meaning "not judged" exactly as it does on an attempt
    closed by a PM change request.

    THE CATEGORY'S GROUP DECIDES WHOSE REWORK THIS IS. A Group A failure counts toward the
    designer's multiplier; Group B and C do not (settled decision 8). Nothing is computed
    here — the category is stored, and design_metrics reads it back through
    error_category_group().
    """
    project = _opex_site(project_id)
    assignment, attempt, error = _qc_guard(request, project, (DESIGN_IN_QC,))
    if error is None:
        return HttpResponseForbidden(_GATE_FORBIDDEN['qc'])
    if request.method != 'POST':
        return redirect('design_qc_review', project_id=project.project_id)
    if error:
        messages.error(request, error)
        return redirect('design_qc_queue')

    blocked = _blocking_change_request(project, attempt)
    if blocked:
        messages.error(request, blocked)
        return redirect('design_qc_review', project_id=project.project_id)

    profile = request.user.profile
    if _other_gate_actor_conflict(profile, attempt.head_reviewed_by_id):
        messages.error(request, f'{project.project_id}: you have already recorded the '
                                f'Design Head verdict on attempt {attempt.attempt_number}.')
        return redirect('design_qc_review', project_id=project.project_id)

    remarks = (request.POST.get('qc_remarks') or '').strip()
    if not remarks:
        messages.error(request, f'{project.project_id}: QC remarks are required to fail '
                                f'a package — the designer cannot act on "failed" alone.')
        return redirect('design_qc_review', project_id=project.project_id)

    category, cat_error = _posted_error_category(request)
    if cat_error:
        messages.error(request, f'{project.project_id}: {cat_error}.')
        return redirect('design_qc_review', project_id=project.project_id)

    now = timezone.now()
    with transaction.atomic():
        attempt.qc_verdict          = QC_FAILED
        attempt.qc_remarks          = remarks
        attempt.qc_failure_category = category
        attempt.qc_reviewed_by      = profile
        attempt.qc_reviewed_at      = now
        attempt.save(update_fields=['qc_verdict', 'qc_remarks', 'qc_failure_category',
                                    'qc_reviewed_by', 'qc_reviewed_at'])
        # Status passes THROUGH qc_failed on its way back to in_design. Recorded as its
        # own log line so the failure is visible in the trail even though the stored
        # status moves straight on to the new attempt's in_design.
        assignment.status = DESIGN_QC_FAILED
        assignment.save(update_fields=['status', 'updated_at'])
        log_activity(project, profile,
                     f'Design QC failed attempt {attempt.attempt_number} '
                     f'[{DESIGN_ERROR_CATEGORY_LABELS.get(category, category)}]: {remarks}',
                     entity_type='DesignAttempt', entity_id=attempt.pk,
                     action_code='design_qc_failed')

        new_attempt = _open_next_attempt(
            assignment, ATTEMPT_REASON_QC_FAILED, profile,
            f'Design QC failure on attempt {attempt.attempt_number}')

    messages.success(request, f'{project.project_id}: Design QC failed — attempt '
                              f'{new_attempt.attempt_number} opened and the site is back '
                              f'with the designer.')
    return redirect('design_qc_review', project_id=project.project_id)


@login_required
def design_head_qc_pass(request, project_id):
    """GATE 2 — the DESIGN HEAD passes a package Design QC has already passed. RELEASE.

    Release sets `released_at` / `released_by` on the assignment and moves it to
    `released`. THAT IS ALL IT DOES (Part 4 settled decision 9). It does not lock, group
    or hand over the BOQ — those are Part 6, and reading anything into `released` beyond
    "design is finished" would pre-empt decisions that have not been made.

    This is where the attempt finally closes, and where Part 4's release behaviour now
    lives unchanged apart from which gate triggers it.
    """
    project = _opex_site(project_id)
    assignment, attempt, error = _qc_guard(request, project, (DESIGN_AWAITING_HEAD_QC,),
                                           gate='head')
    if error is None:
        return HttpResponseForbidden(_GATE_FORBIDDEN['head'])
    if request.method != 'POST':
        return redirect('design_qc_review', project_id=project.project_id)
    if error:
        messages.error(request, error)
        return redirect('design_qc_queue')

    blocked = _blocking_change_request(project, attempt)
    if blocked:
        messages.error(request, blocked)
        return redirect('design_qc_review', project_id=project.project_id)

    profile = request.user.profile
    if _other_gate_actor_conflict(profile, attempt.qc_reviewed_by_id):
        messages.error(request, f'{project.project_id}: you passed attempt '
                                f'{attempt.attempt_number} through Design QC yourself, so '
                                f'you cannot also release it as Design Head. It needs a '
                                f'second pair of eyes.')
        return redirect('design_qc_review', project_id=project.project_id)

    now = timezone.now()
    with transaction.atomic():
        attempt.head_verdict     = QC_PASSED
        attempt.head_reviewed_by = profile
        attempt.head_reviewed_at = now
        attempt.closed_at        = now
        # Both gates passed — they agree, so no overturn.
        attempt.head_overturned_qc = False
        attempt.save(update_fields=['head_verdict', 'head_reviewed_by', 'head_reviewed_at',
                                    'closed_at', 'head_overturned_qc'])
        assignment.released_at = now
        assignment.released_by = profile
        assignment.status      = DESIGN_RELEASED
        assignment.save(update_fields=['released_at', 'released_by', 'status', 'updated_at'])
        log_activity(project, profile,
                     f'Design Head passed attempt {attempt.attempt_number} — design released',
                     entity_type='DesignAttempt', entity_id=attempt.pk,
                     action_code='design_head_qc_passed')

    messages.success(request, f'{project.project_id}: both review gates passed — design '
                              f'released on attempt {attempt.attempt_number}.')
    return redirect('design_qc_review', project_id=project.project_id)


@login_required
def design_head_qc_fail(request, project_id):
    """GATE 2 — the DESIGN HEAD fails a package Design QC passed. THE OVERTURN CASE.

    Remarks and category are both mandatory, and the attempt closes and reopens exactly as
    a gate-1 failure does (settled decision 5) — the designer's experience is identical.
    What differs is where it is recorded: head_remarks / head_failure_category, a distinct
    action_code, and `head_overturned_qc=True`.

    THAT FLAG IS THE POINT OF THE SECOND GATE (settled decision 6). If after two tenders
    the Head never overturns Design QC, the second gate is a formality and this column is
    the only thing that will say so. It cannot be reconstructed later from the verdicts
    alone once the attempt has been superseded, which is why it is written now.
    """
    project = _opex_site(project_id)
    assignment, attempt, error = _qc_guard(request, project, (DESIGN_AWAITING_HEAD_QC,),
                                           gate='head')
    if error is None:
        return HttpResponseForbidden(_GATE_FORBIDDEN['head'])
    if request.method != 'POST':
        return redirect('design_qc_review', project_id=project.project_id)
    if error:
        messages.error(request, error)
        return redirect('design_qc_queue')

    blocked = _blocking_change_request(project, attempt)
    if blocked:
        messages.error(request, blocked)
        return redirect('design_qc_review', project_id=project.project_id)

    profile = request.user.profile
    if _other_gate_actor_conflict(profile, attempt.qc_reviewed_by_id):
        messages.error(request, f'{project.project_id}: you passed attempt '
                                f'{attempt.attempt_number} through Design QC yourself, so '
                                f'you cannot also rule on it as Design Head. It needs a '
                                f'second pair of eyes.')
        return redirect('design_qc_review', project_id=project.project_id)

    remarks = (request.POST.get('head_remarks') or '').strip()
    if not remarks:
        messages.error(request, f'{project.project_id}: remarks are required to fail a '
                                f'package — the designer cannot act on "failed" alone.')
        return redirect('design_qc_review', project_id=project.project_id)

    category, cat_error = _posted_error_category(request)
    if cat_error:
        messages.error(request, f'{project.project_id}: {cat_error}.')
        return redirect('design_qc_review', project_id=project.project_id)

    now = timezone.now()
    with transaction.atomic():
        attempt.head_verdict          = QC_FAILED
        attempt.head_remarks          = remarks
        attempt.head_failure_category = category
        attempt.head_reviewed_by      = profile
        attempt.head_reviewed_at      = now
        # Design QC passed this package and the Head did not. Recorded, countable.
        attempt.head_overturned_qc    = True
        attempt.save(update_fields=['head_verdict', 'head_remarks', 'head_failure_category',
                                    'head_reviewed_by', 'head_reviewed_at',
                                    'head_overturned_qc'])
        assignment.status = DESIGN_QC_FAILED
        assignment.save(update_fields=['status', 'updated_at'])
        log_activity(project, profile,
                     f'Design Head failed attempt {attempt.attempt_number}, overturning '
                     f'Design QC '
                     f'[{DESIGN_ERROR_CATEGORY_LABELS.get(category, category)}]: {remarks}',
                     entity_type='DesignAttempt', entity_id=attempt.pk,
                     action_code='design_head_qc_failed')

        new_attempt = _open_next_attempt(
            assignment, ATTEMPT_REASON_QC_FAILED, profile,
            f'Design Head failure on attempt {attempt.attempt_number}')

    messages.success(request, f'{project.project_id}: the Design Head failed the package, '
                              f'overturning Design QC — attempt '
                              f'{new_attempt.attempt_number} opened and the site is back '
                              f'with the designer.')
    return redirect('design_qc_review', project_id=project.project_id)


# ---------------------------------------------------------------------------
# 13. PM change requests
# ---------------------------------------------------------------------------

@login_required
def design_change_request(request, project_id):
    """The site's assigned PM (or a coordinator) requests a change.

    WINDOW: `qc_started_at` must be set on the current attempt, and the site must not be
    released (settled decision 3). Before QC starts there is nothing settled to raise a
    change against and the request is refused with a message; after release the design is
    finished. The real close condition is BOQ locking, which is Part 6 — until it exists,
    release stands in for it.

    IF QC IS IN FLIGHT, IT IS SUSPENDED (settled decision 4). The site returns to the
    designer immediately and no verdict is recorded. Critically, the outgoing attempt
    keeps `qc_verdict='pending'`: it was never judged, and marking it 'failed' would
    charge the designer with a rework loop the PM caused. That distinction is the reason
    both `opened_reason` values exist.

    PART 6 — THE GROUP DECIDES, IN THREE CASES (Part 6 §4):

      LOCKED group  -> REFUSED. This is where the real close condition finally lands. The
                       quantities have been committed to a purchase; the correction is a
                       variance against that order, which is a separate feature.
      DRAFT group   -> the SITE LEAVES THE GROUP and the request proceeds. The group is not
                       held hostage to one site, and SCM keeps procuring the rest. The
                       removal is explicit and logged — never an invisible side effect —
                       and it is what makes this branch reachable at all: a group member is
                       `released` by construction, which Part 4 refuses outright.
      NO group      -> unchanged from Part 4, including that refusal. A released ungrouped
                       site is still a new scope of work, not a change request.

    The resulting asymmetry (released-and-grouped is change-requestable, released-and-
    ungrouped is not) is Part 6 §4 as specified; it is recorded in
    DESIGN_MODULE_DEFERRED.md rather than resolved here.
    """
    project = _opex_site(project_id)
    assignment = getattr(project, 'design_assignment', None)
    if not user_can_request_design_change(request.user, project):
        return HttpResponseForbidden(
            'Only the PM assigned to this site (or one of its coordinators) may request '
            'a design change.')
    if request.method != 'POST':
        return redirect('design_change_request_form', project_id=project.project_id)

    def _back(msg, ok=False):
        (messages.success if ok else messages.error)(request, msg)
        return redirect('design_change_request_form', project_id=project.project_id)

    if assignment is None:
        return _back(f'{project.project_id}: design has not started on this site yet.')

    reason = (request.POST.get('reason') or '').strip()
    if not reason:
        return _back('Please say what needs to change — a reason is required.')

    attempt = _current_attempt(assignment)
    if attempt is None:
        return _back(f'{project.project_id}: design has not started on this site yet.')

    membership = active_group_membership(project)

    if membership is not None and membership.group.status == SITE_GROUP_LOCKED:
        return _back(f'{project.project_id}: the BOQ is locked — this site is in the '
                     f'locked procurement group "{membership.group.name}" and its '
                     f'quantities have been committed. A change now needs a variance '
                     f'against the order, which this system does not handle yet. Raise it '
                     f'with SCM directly.')

    # A draft-group member is `released` by construction, so admitting it here means
    # stepping past BOTH of Part 4's release guards — the explicit one below and the
    # absence of `released` from CHANGE_REQUEST_STATUSES. Nothing else is relaxed.
    in_draft_group = membership is not None
    allowed_statuses = (CHANGE_REQUEST_STATUSES + (DESIGN_RELEASED,)
                        if in_draft_group else CHANGE_REQUEST_STATUSES)

    if assignment.status == DESIGN_RELEASED and not in_draft_group:
        return _back(f'{project.project_id}: the design is already released — a change '
                     f'now is a new scope of work, not a change request.')

    if attempt.qc_started_at is None:
        return _back(f'{project.project_id}: QC has not started on this package yet, so '
                     f'there is nothing settled to raise a change against. Talk to the '
                     f'Design Head — a change at this stage does not need a formal '
                     f'request.')

    if assignment.status not in allowed_statuses:
        return _back(f'{project.project_id}: a change request cannot be raised at this '
                     f'stage (status "{assignment.get_status_display()}").')

    profile = request.user.profile
    # PART 9: a review is "in flight" at EITHER gate. A package sitting at
    # `awaiting_head_qc` has passed Design QC and is with the Head, and pulling it back
    # suspends his review exactly as it used to suspend the single one — so the message
    # must say so. The outgoing attempt keeps whatever verdicts it had: qc_verdict
    # 'passed' and head_verdict 'pending' is the honest record of what actually happened.
    was_in_qc = assignment.status in (DESIGN_IN_QC, DESIGN_AWAITING_HEAD_QC)
    with transaction.atomic():
        # The removal shares the change request's transaction on purpose: a site that
        # left a group without the request that pulled it out, or a request recorded
        # against a site still counted in an aggregate, are both worse than neither.
        if in_draft_group:
            remove_from_group(membership, profile, CHANGE_REQUEST_REMOVAL_REASON)

        change = DesignChangeRequest.objects.create(
            attempt=attempt, requested_by=profile, reason=reason)
        log_activity(project, profile,
                     f'PM change request on attempt {attempt.attempt_number}: {reason}'
                     + (' (QC in progress — review suspended)' if was_in_qc else ''),
                     entity_type='DesignChangeRequest', entity_id=change.pk,
                     action_code='design_change_requested')

        # The outgoing attempt's qc_verdict is deliberately left at 'pending' — see the
        # docstring. _open_next_attempt() sets closed_at and nothing else on it.
        new_attempt = _open_next_attempt(
            assignment, ATTEMPT_REASON_PM_CHANGE_REQUEST, profile,
            f'change requested on attempt {attempt.attempt_number}')

        change.resulting_attempt = new_attempt
        change.save(update_fields=['resulting_attempt'])

    msg = (f'{project.project_id}: change request recorded — attempt '
           f'{new_attempt.attempt_number} opened and the site is back with the designer.')
    if was_in_qc:
        msg += ' The QC review in progress was suspended without a verdict.'
    if in_draft_group:
        msg += (f' The site was removed from procurement group '
                f'"{membership.group.name}" — its quantities are no longer in that '
                f'group\'s aggregate.')
    return _back(msg, ok=True)


# ---------------------------------------------------------------------------
# 14. Part 4 screens
# ---------------------------------------------------------------------------

def _attempt_history(assignment):
    """Every attempt on an assignment, oldest first, with the change requests that
    closed each one. The two rework loops must be tellable apart at a glance, which is
    what `opened_reason` renders as on the screens."""
    return list(assignment.attempts
                .select_related('qc_reviewed_by__user', 'head_reviewed_by__user',
                                'boq_submitted_by__user')
                .prefetch_related('change_requests__requested_by__user',
                                  'arka_submissions')
                .order_by('attempt_number'))


@login_required
def design_qc_queue(request):
    """The review worklist for BOTH artifacts and BOTH gates.

    Deliberately NOT the Design Head dashboard — no metrics, no workload, no capacity, no
    overdue logic. It is a worklist of the five reviewable statuses and nothing else; the
    dashboards are Part 5 and Part 9 §6.

    PART 9 — ONE QUEUE, TWO AUDIENCES, TWO ARTIFACTS.

    Design QC and the Head share this screen and see the same rows, because knowing what is
    stacked up at the other gate is exactly the information a reviewer needs. What differs
    is which row each can ACT on, and that is computed PER ROW because both the self-review
    exclusion and the one-person-two-verdicts rule are per site.

    IT CARRIES ARKAS AS WELL AS PACKAGES, and it has to. The Arka is the FIRST thing
    Design QC reviews, and until Part 9 the only route to `design_head_review` was the
    Head's per-tender site list — a screen a Design QC reviewer cannot open, because it is
    gated on Head authority. Without the Arka section here, a QC reviewer's dashboard could
    correctly report "2 Arka awaiting your verdict" with nowhere to click, which is exactly
    the failure Part 4.5 called out about screens reachable only by typing a URL.

    Two querysets rather than one: the two artifacts live at different statuses, need
    different verdict URLs, and read better as separate sections than as one list where
    half the rows have no package to open.
    """
    if not user_can_view_design_qc_dashboard(request.user):
        return HttpResponseForbidden('Design QC, Design Head or named deputy only.')

    profile = getattr(request.user, 'profile', None)

    # ── Arkas: awaiting gate 1, or passed gate 1 and awaiting gate 2 ──────────
    arka_assignments = (DesignAssignment.objects
                        .filter(status__in=(DESIGN_ARKA_SUBMITTED,
                                            DESIGN_AWAITING_HEAD_ARKA),
                                project__is_deleted=False)
                        .select_related('project', 'project__program', 'assigned_to__user')
                        .order_by('status', 'project__project_id'))

    arka_rows = []
    for assignment in arka_assignments:
        attempt = _current_attempt(assignment)
        arka = _current_arka(attempt)
        if arka is None:
            continue
        # A site at `arka_submitted` whose Arka the Head has ALREADY approved is not in
        # anybody's review queue — it is with the designer, owing CAD and BOQ. See the
        # Part 9 status note: that combination is the "artifacts outstanding" state.
        if arka.head_verdict == ARKA_APPROVED:
            continue
        awaiting_head = assignment.status == DESIGN_AWAITING_HEAD_ARKA
        can_qc_gate   = user_can_qc_gate_design(request.user, assignment)
        can_head_gate = user_can_head_gate_design(request.user, assignment)
        own_qc_verdict = _other_gate_actor_conflict(profile, arka.reviewed_by_id)
        arka_rows.append({
            'assignment':    assignment,
            'site':          assignment.project,
            'attempt':       attempt,
            'arka':          arka,
            'awaiting_head': awaiting_head,
            'can_qc':        (can_qc_gate and not awaiting_head
                              and arka.verdict == ARKA_PENDING),
            'can_head':      (can_head_gate and awaiting_head
                              and arka.head_verdict == ARKA_PENDING
                              and not own_qc_verdict),
            'blocked_own':   (can_head_gate and awaiting_head
                              and arka.head_verdict == ARKA_PENDING
                              and own_qc_verdict),
        })

    # ── Packages: awaiting gate 1, in gate 1, or awaiting gate 2 ──────────────
    assignments = (DesignAssignment.objects
                   .filter(status__in=(DESIGN_ARTIFACTS_UPLOADED, DESIGN_IN_QC,
                                       DESIGN_AWAITING_HEAD_QC),
                           project__is_deleted=False)
                   .select_related('project', 'project__program', 'assigned_to__user')
                   .order_by('status', 'project__project_id'))

    rows = []
    for assignment in assignments:
        attempt = _current_attempt(assignment)
        awaiting_head = assignment.status == DESIGN_AWAITING_HEAD_QC
        can_qc_gate   = user_can_qc_gate_design(request.user, assignment)
        can_head_gate = user_can_head_gate_design(request.user, assignment)
        rows.append({
            'assignment':  assignment,
            'site':        assignment.project,
            'attempt':     attempt,
            'arka':        _current_arka(attempt),
            'in_qc':       assignment.status == DESIGN_IN_QC,
            'awaiting_head': awaiting_head,
            'open_crs':    list(_open_change_requests(attempt)),
            # Gate 1 actions: start review, then pass/fail. Not offered once the package
            # has moved past Design QC.
            'can_qc':      can_qc_gate and not awaiting_head,
            # Gate 2 actions. Withheld when this user recorded the QC verdict themselves —
            # settled decision 2, shown as a disabled state rather than a silent absence.
            'can_head':    (can_head_gate and awaiting_head and attempt is not None
                            and not _other_gate_actor_conflict(
                                profile, attempt.qc_reviewed_by_id)),
            'blocked_own': (can_head_gate and awaiting_head and attempt is not None
                            and _other_gate_actor_conflict(
                                profile, attempt.qc_reviewed_by_id)),
        })

    return render(request, 'projects/design/qc_queue.html', {
        'arka_rows': arka_rows,
        'rows':      rows,
        'is_deputy': user_is_design_head_deputy(request.user) and not user_is_design_head(request.user),
        'is_design_qc': user_is_design_qc(request.user),
        'has_head_authority': user_has_design_head_authority(request.user),
    })


@login_required
def design_qc_review(request, project_id):
    """Head / deputy: the full package for one site — Arka link and capacity, CAD and
    BOQ files by signed URL, BOQ link, attempt history — with the QC actions."""
    project = _opex_site(project_id)
    assignment = getattr(project, 'design_assignment', None)
    if assignment is None:
        raise Http404('No design assignment for this site.')
    if not user_can_view_design_qc_dashboard(request.user):
        return HttpResponseForbidden('Design QC, Design Head or named deputy only.')

    ctx = _workspace_context(project, assignment)
    attempt  = ctx['attempt']
    open_crs = list(_open_change_requests(attempt))
    profile  = getattr(request.user, 'profile', None)

    can_qc_gate   = user_can_qc_gate_design(request.user, assignment)
    can_head_gate = user_can_head_gate_design(request.user, assignment)
    awaiting_head = assignment.status == DESIGN_AWAITING_HEAD_QC

    # Settled decision 2, evaluated once and reused: did this actor record the OTHER
    # gate's verdict on this exact attempt?
    own_qc_verdict = bool(attempt is not None and _other_gate_actor_conflict(
        profile, attempt.qc_reviewed_by_id))

    ctx.update({
        'history':       _attempt_history(assignment),
        'open_crs':      open_crs,
        'can_qc':        can_qc_gate,
        'can_start_qc':  can_qc_gate and assignment.status == DESIGN_ARTIFACTS_UPLOADED
                         and _package_is_complete(attempt),
        # Gate 1 verdict form.
        'can_verdict':   can_qc_gate and assignment.status == DESIGN_IN_QC and not open_crs,
        # Gate 2 verdict form. Mutually exclusive with the above by status.
        'can_head_verdict': (can_head_gate and awaiting_head and not open_crs
                             and not own_qc_verdict),
        # Why the gate-2 form is absent, when it is absent for this reason and not
        # because of the status. An empty screen explains nothing.
        'blocked_by_own_qc_verdict': can_head_gate and awaiting_head and own_qc_verdict,
        'awaiting_head': awaiting_head,
        'is_self_qc':    user_is_assigned_designer(request.user, assignment),
        'is_deputy':     user_is_design_head_deputy(request.user) and not user_is_design_head(request.user),
        'released':      assignment.status == DESIGN_RELEASED,
        'error_categories': DESIGN_ERROR_CATEGORY_CHOICES,
    })
    return render(request, 'projects/design/qc_review.html', ctx)


# ---------------------------------------------------------------------------
# 15. Dashboard integration (Part 4.5)
#
# The OPEX design workflow's screens were URL-reachable only through Parts 2-4, which
# is unusable: nobody types a URL. These helpers give views.py the per-site context it
# needs to render design state and ONE contextual action inside the tender card that
# already exists on the Design dashboard.
#
# THEY COMPUTE CONTEXT AND NOTHING ELSE. No status is written, no row is created, no
# permission is decided here — the action a helper offers is only ever a link or a form
# pointing at a Part 2-4 endpoint that re-checks authority for itself. A button this
# code chooses to render is a convenience; the view behind it is the gate.
# ---------------------------------------------------------------------------

# One primary action per assignment status, from the allocated designer's point of view.
# `kind` tells the template what to render:
#   'propose_due'  — inline date form posting to design_due_date_propose
#   'link'         — a button to the screen carrying that step's form
#   'none'         — nothing to do; `waiting` explains who is holding the ball
#
# EXACTLY ONE ACTION IS OFFERED AT A TIME. Showing every button always is what makes a
# workflow screen unreadable, and the status already determines which single step is legal —
# every other endpoint would refuse anyway.
_DESIGNER_ACTIONS = {
    DESIGN_AWAITING_SURVEY:     ('none', '', 'The Design Head has not uploaded the survey yet.'),
    DESIGN_AWAITING_ALLOCATION: ('none', '', 'Waiting for the Design Head to allocate this site.'),
    DESIGN_ALLOCATED:           ('propose_due', 'Propose due date', ''),
    DESIGN_DUE_DATE_PROPOSED:   ('none', '', 'Waiting for the Design Head to approve your proposed date.'),
    DESIGN_IN_DESIGN:           ('link', 'Submit Arka', ''),
    DESIGN_ARKA_REJECTED:       ('link', 'Submit revised Arka', ''),
    DESIGN_ARTIFACTS_UPLOADED:  ('none', '', 'Package complete — waiting for Design QC to start.'),
    DESIGN_IN_QC:               ('none', '', 'In review with Design QC.'),
    DESIGN_RELEASED:            ('none', '', 'Design released. Nothing further to do.'),
    DESIGN_SURVEY_RETURNED:     ('none', '', 'Design Hold — waiting for a replacement survey.'),
    # PART 9 — the two waiting rooms. Both name WHICH reviewer is holding the ball, which
    # is the entire reason they are separate statuses rather than a flag.
    DESIGN_AWAITING_HEAD_ARKA:  ('none', '', 'Arka passed Design QC — waiting for the '
                                             'Design Head to approve it.'),
    DESIGN_AWAITING_HEAD_QC:    ('none', '', 'Package passed Design QC — waiting for the '
                                             'Design Head\'s review.'),
}


def designer_dashboard_context(profile, projects):
    """Per-site design context for the Design dashboard's tender cards.

    `projects` is the queryset the dashboard already fetched — this issues no project
    query of its own. Returns {project_pk: context_dict} covering ONLY OPEX sites that
    have a DesignAssignment; every other project is absent from the mapping, so the
    template renders nothing extra for Residential and the existing cards are untouched.

    `profile` is the viewing user. Actions are offered only where they are the allocated
    designer — a Design Head looking at somebody else's site gets the state and no
    buttons.
    """
    out = {}
    for project in projects:
        if project.project_type != 'OPEX':
            continue
        assignment = getattr(project, 'design_assignment', None)
        if assignment is None:
            continue

        is_designer = assignment.assigned_to_id == profile.pk
        attempt     = _current_attempt(assignment)
        arka        = _current_arka(attempt)
        commitments = list(assignment.due_date_commitments.all())
        # PART 8: the dashboard shows the AGREED date. Reading the is_current row here
        # would blank the due date on every card the moment its designer asked for an
        # extension — and would show the requested date as though it had been granted.
        current_due = effective_commitment(commitments)
        pending_due = pending_extension(commitments)

        # `arka_submitted` is the one status whose next step depends on the Arka verdict
        # rather than on the status alone, so it is resolved here instead of in the table.
        kind, label, waiting = _DESIGNER_ACTIONS.get(
            assignment.status, ('none', '', ''))
        # PART 9: the unlock is head_verdict, not verdict — an Arka that only Design QC
        # has passed does not let the designer upload anything, so telling them to
        # "Upload CAD" at that point would send them into a refusal.
        if assignment.status == DESIGN_ARKA_SUBMITTED:
            if arka is not None and arka.head_verdict == ARKA_APPROVED:
                has_cad = bool(attempt and attempt.design_files.filter(
                    kind__in=CAD_KINDS, is_current=True).exists())
                if not has_cad:
                    kind, label, waiting = 'link', 'Upload CAD', ''
                elif attempt.boq_submitted_at is None:
                    kind, label, waiting = 'link', 'Enter BOQ', ''
                else:
                    kind, label, waiting = 'none', '', 'Package complete — waiting for Design QC.'
            else:
                kind, label, waiting = 'none', '', 'Waiting for Design QC to review your Arka.'

        # The remarks the designer has to act on: the most recent FAILED attempt, at
        # EITHER gate. Part 9 made this two fields — a package failed by the Head carries
        # head_remarks and an empty qc_remarks, and reading only the QC field would leave
        # the designer with a reopened attempt and no visible reason for it.
        # Read off the attempts already loaded rather than re-querying per card.
        last_failed = None
        for a in sorted(assignment.attempts.all(), key=lambda a: a.attempt_number, reverse=True):
            if ((a.qc_verdict == QC_FAILED and a.qc_remarks)
                    or (a.head_verdict == QC_FAILED and a.head_remarks)):
                last_failed = a
                break

        out[project.pk] = {
            'assignment':     assignment,
            'status':         assignment.status,
            'status_label':   assignment.get_status_display(),
            'is_designer':    is_designer,
            'designer':       assignment.assigned_to,
            'attempt':        attempt,
            'attempt_number': assignment.current_attempt_number,
            # Only shown when the attempt is rework — an 'initial' attempt needs no label.
            'attempt_reason': (attempt.get_opened_reason_display()
                               if attempt and attempt.opened_reason != ATTEMPT_REASON_INITIAL
                               else ''),
            'arka':           arka,
            'due_date':       current_due.proposed_date if current_due else None,
            'due_approved':   bool(current_due and current_due.approved_at),
            # The requested date, shown as a pending chip beside the agreed one. It is
            # NOT what 'due_date' reports and never feeds an overdue calculation.
            'due_extension_requested': (pending_due.proposed_date if pending_due else None),
            # Revisions are derived by counting commitment rows, exactly as Part 2 does —
            # there is no stored counter to drift.
            'due_revised':    max(len(commitments) - 1, 0),
            'is_blocked':     assignment.status == DESIGN_SURVEY_RETURNED,
            'block_reason':   assignment.survey_return_reason,
            'qc_failed_attempt': last_failed,
            'action_kind':    kind if is_designer else 'none',
            'action_label':   label if is_designer else '',
            'waiting':        waiting,
            'can_mark_blocked': is_designer and assignment.status not in (
                DESIGN_SURVEY_RETURNED, DESIGN_RELEASED, DESIGN_AWAITING_SURVEY,
                DESIGN_AWAITING_ALLOCATION),
        }
    return out


def design_head_dashboard_counts(user):
    """The three queue sizes a Design Head can see for free — one COUNT each.

    NOT METRICS. No workload, no capacity, no overdue arithmetic, no sorting, no
    per-designer breakdown — that is Part 5 and is deliberately absent. These are the
    sizes of three worklists that already exist as screens, shown so the Head knows
    whether opening them is worth it.

    Returns None for a user without Head authority, so the template can test one value.

    It also carries the list of OPEX tenders, because the Head's per-tender screen
    (design_head_sites) is where survey upload and allocation live and he has no other
    way to reach it: `program_list` is @role_required(['Admin','PM','CEO']) and the real
    Design Head holds role='Design', so that nav link 403s for him. Modifying that
    decorator is out of scope, so the tenders are linked directly from here instead.
    """
    if not user_has_design_head_authority(user):
        return None
    base = DesignAssignment.objects.filter(project__is_deleted=False)
    return {
        'awaiting_allocation': base.filter(status=DESIGN_AWAITING_ALLOCATION).count(),
        # Gate 1's Arka queue — still keyed on the QC verdict, which is what `verdict`
        # now means. The status term alone would be enough; the verdict term is kept as
        # the same belt-and-braces re-check it has always been.
        'awaiting_arka':       base.filter(status=DESIGN_ARKA_SUBMITTED,
                                           attempts__arka_submissions__is_current=True,
                                           attempts__arka_submissions__verdict=ARKA_PENDING
                                           ).distinct().count(),
        # PART 9 — gate 2's two queues, counted separately from gate 1's. Merging them
        # would tell the Head how much work exists without telling him how much is his.
        'awaiting_head_arka':  base.filter(status=DESIGN_AWAITING_HEAD_ARKA).count(),
        'awaiting_head_qc':    base.filter(status=DESIGN_AWAITING_HEAD_QC).count(),
        'awaiting_qc':         base.filter(status=DESIGN_ARTIFACTS_UPLOADED).count(),
        'in_qc':               base.filter(status=DESIGN_IN_QC).count(),
        'programs':            list(Program.objects.filter(is_deleted=False,
                                                           program_type='OPEX')
                                    .order_by('name')),
    }


def design_qc_dashboard_counts(user):
    """The two queue sizes a DESIGN QC reviewer can see for free — one COUNT each.

    THE GATE-1 COUNTERPART OF design_head_dashboard_counts(), and it exists for the reason
    Part 4.5 gives for that one: a screen that is URL-reachable only is unusable, because
    nobody types a URL. Without this the Design QC dashboard and review queue would have no
    entry point anywhere in the product.

    Returns None for a user who does not hold `is_design_qc`, so the template can test one
    value — and deliberately NOT for a Head who lacks the QC flag, since his own strip
    already carries every number this one would.

    NOT METRICS. Two counts and a tender list; the dashboard is design_qc_dashboard().
    """
    if not user_is_design_qc(user):
        return None
    base = DesignAssignment.objects.filter(project__is_deleted=False)
    return {
        'awaiting_arka': base.filter(status=DESIGN_ARKA_SUBMITTED,
                                     attempts__arka_submissions__is_current=True,
                                     attempts__arka_submissions__verdict=ARKA_PENDING
                                     ).distinct().count(),
        'awaiting_qc':   base.filter(status=DESIGN_ARTIFACTS_UPLOADED).count(),
        'in_qc':         base.filter(status=DESIGN_IN_QC).count(),
        # Same reason as the Head's strip: `program_list` is
        # @role_required(['Admin','PM','CEO']) and a Design QC reviewer holds
        # role='Design', so that nav link 403s for them. Tenders are linked from here.
        'programs':      list(Program.objects.filter(is_deleted=False,
                                                     program_type='OPEX')
                              .order_by('name')),
    }


def pm_change_request_targets(user, projects):
    """Which of `projects` the PM may raise a design change request against right now.

    Returns {project_pk: True} for OPEX sites where the change window is open — QC has
    started on the current attempt and the site is not yet released (settled decision 3
    of Part 4). Authority is re-decided by design_change_request() on POST; this only
    decides whether to offer the link.
    """
    out = {}
    for project in projects:
        if project.project_type != 'OPEX':
            continue
        assignment = getattr(project, 'design_assignment', None)
        if assignment is None or assignment.status == DESIGN_RELEASED:
            continue
        if not user_can_request_design_change(user, project):
            continue
        attempt = _current_attempt(assignment)
        if attempt is not None and attempt.qc_started_at is not None:
            out[project.pk] = True
    return out


# ---------------------------------------------------------------------------
# 16. Design Head tender dashboard (Part 5) — READ ONLY
#
# A NEW SCREEN RATHER THAN AN EXTENSION OF design_head_sites().
# Settled decision 8 rules out a per-site table as the landing view, and head_sites IS
# that table — bolting metrics on top of it would make them an appendix to the thing the
# decision forbids. So this becomes the landing view for a tender and drills DOWN into a
# filtered list rendered on the same page; head_sites stays exactly as it was and remains
# the full editable site list, linked from here.
#
# This view writes nothing. Every number comes from design_metrics, which holds no write
# of any kind.
# ---------------------------------------------------------------------------

@login_required
def design_tender_dashboard(request, pk):
    """The Design Head's operational view of one tender.

    Permission is the Part 4 helper, so a named deputy gets in and the 'Design Head' role
    string is not consulted. A designer is refused outright — the workload table shows
    every designer's rework multiplier side by side, and on samples this small that number
    is noisy enough that the first reaction to a bad one is to argue with the metric
    rather than the work.

    DRILL-DOWN, NOT A LANDING TABLE. `?stage=` and `?designer=` filter a compact list
    rendered beneath the panels, from the rows already in memory — neither adds a query.
    """
    if not user_has_design_head_authority(request.user):
        return HttpResponseForbidden(
            'The tender design dashboard is for the Design Head or his named deputy.')

    program = get_object_or_404(Program, pk=pk, is_deleted=False, program_type='OPEX')
    metrics = tender_metrics(program)

    # Drill-down is filtered in Python over `metrics['sites']`, which is already loaded.
    stage    = (request.GET.get('stage') or '').strip()
    designer = (request.GET.get('designer') or '').strip()
    drill, drill_label = [], ''
    if stage in STAGE_LABELS:
        drill = [s for s in metrics['sites'] if s['stage'] == stage]
        drill_label = STAGE_LABELS[stage]
    elif designer.isdigit():
        did = int(designer)
        drill = [s for s in metrics['sites']
                 if s['designer'] and s['designer'].pk == did]
        drill_label = (drill[0]['designer'].user.get_full_name()
                       or drill[0]['designer'].user.username) if drill else 'designer'

    return render(request, 'projects/design/tender_dashboard.html', {
        'program':      program,
        'm':            metrics,
        'drill':        drill,
        'drill_label':  drill_label,
        'drill_stage':  stage if stage in STAGE_LABELS else '',
        'drill_designer': designer if designer.isdigit() else '',
        'is_deputy':    user_is_design_head_deputy(request.user)
                        and not user_is_design_head(request.user),
    })


# ---------------------------------------------------------------------------
# 17. Design QC tender dashboard (Part 9 §6) — READ ONLY, AND A STRICT SUBSET
#
# A SUBSET OF design_tender_dashboard(), NOT A SECOND DASHBOARD. It calls the same
# tender_metrics() — same batched reads, same `sites` list — and then passes THREE of its
# keys to the template. What it deliberately does not pass is the point of the screen:
#
#   workload     per-designer rework multipliers. PERFORMANCE DATA, HEAD ONLY. Part 5
#                already refuses designers this table because on samples this small the
#                first reaction to a bad number is to argue with the metric; a QC reviewer
#                holding their colleagues' multipliers is the same problem with a
#                reporting line attached.
#   capacity     designed-versus-tendered. COMMERCIAL, HEAD ONLY. A reviewer's job is
#                whether this design is right, not whether the tender is profitable.
#   attention    the full list, which leads on overdue sites, Design Holds and due-date
#                revisions — all of which are about how the tender is being RUN. QC gets
#                `qc_attention` instead: only items awaiting a QC action.
#
# The exclusion is enforced HERE, by not putting the data in the context, rather than by a
# template `{% if %}`. A key that is absent cannot be rendered by a future template edit;
# a key that is present and conditionally hidden can.
# ---------------------------------------------------------------------------

@login_required
def design_qc_dashboard(request, pk):
    """Design QC's operational view of one tender: stage counts, their own queue, and the
    items awaiting a QC action.

    The Design Head is admitted too — he sees everything Design QC sees plus his own full
    dashboard, so refusing him a narrower view of his own data would be an access rule
    with nothing behind it. A designer, PM, SCM or Site Engineer is refused outright.
    """
    if not user_can_view_design_qc_dashboard(request.user):
        return HttpResponseForbidden(
            'The Design QC dashboard is for Design QC, the Design Head, or his named deputy.')

    program = get_object_or_404(Program, pk=pk, is_deleted=False, program_type='OPEX')
    metrics = tender_metrics(program)

    # Same drill-down mechanism as the Head's dashboard, filtered in Python over rows
    # already in memory. The `designer=` filter is deliberately NOT offered: browsing a
    # tender by designer is the workload table by another route.
    stage = (request.GET.get('stage') or '').strip()
    drill, drill_label = [], ''
    if stage in STAGE_LABELS:
        drill = [s for s in metrics['sites'] if s['stage'] == stage]
        drill_label = STAGE_LABELS[stage]

    return render(request, 'projects/design/qc_dashboard.html', {
        'program':     program,
        # A DELIBERATELY NARROWED dict, not `metrics`. See the section note above.
        'm': {
            'program':          metrics['program'],
            'today':            metrics['today'],
            'total_sites':      metrics['total_sites'],
            'assigned_sites':   metrics['assigned_sites'],
            'unassigned_sites': metrics['unassigned_sites'],
            'stages':           metrics['stages'],
            'queue':            metrics['qc_queue'],
            'attention':        metrics['qc_attention'],
        },
        'drill':       drill,
        'drill_label': drill_label,
        'drill_stage': stage if stage in STAGE_LABELS else '',
        'is_head':     user_has_design_head_authority(request.user),
    })


@login_required
def design_change_request_form(request, project_id):
    """PM: raise a change request on a site they manage, and see the history of the ones
    already raised. GET only — the POST target is design_change_request()."""
    project = _opex_site(project_id)
    if not user_can_request_design_change(request.user, project):
        return HttpResponseForbidden(
            'Only the PM assigned to this site (or one of its coordinators) may request '
            'a design change.')

    assignment = getattr(project, 'design_assignment', None)
    if assignment is None:
        raise Http404('Design has not started on this site yet.')

    attempt = _current_attempt(assignment)

    # Part 6: the window the FORM offers must agree with the one design_change_request()
    # enforces, or the PM gets a button that 403s or a missing button that would have
    # worked. Same three cases, same order — see that view's docstring.
    membership   = active_group_membership(project)
    group_locked = membership is not None and membership.group.status == SITE_GROUP_LOCKED
    in_draft_group = membership is not None and not group_locked
    allowed_statuses = (CHANGE_REQUEST_STATUSES + (DESIGN_RELEASED,)
                        if in_draft_group else CHANGE_REQUEST_STATUSES)

    window_open = bool(attempt and attempt.qc_started_at
                       and assignment.status in allowed_statuses
                       and not group_locked)

    return render(request, 'projects/design/change_request.html', {
        'project':     project,
        'assignment':  assignment,
        'attempt':     attempt,
        'history':     _attempt_history(assignment),
        'window_open': window_open,
        'group_locked':   group_locked,
        'draft_group':    membership.group if in_draft_group else None,
        'released':    assignment.status == DESIGN_RELEASED and not in_draft_group,
        'requests':    list(DesignChangeRequest.objects
                            .filter(attempt__assignment=assignment)
                            .select_related('requested_by__user', 'attempt',
                                            'resulting_attempt')
                            .order_by('-requested_at')),
    })


# ===========================================================================
# PART 6 — site groups, aggregated BOQ, BOQ lock, SCM handoff
# ===========================================================================
#
# WHY A GROUP EXISTS AT ALL
# -------------------------
# Procurement never happens for a whole tender. A tender runs for months and its sites
# release in dribs and drabs; an order placed for all of them would either wait for the
# last site or be raised against quantities that are still moving. So SCM batches a set
# of released sites, prices that batch, and orders it. The batch is this module's
# SiteGroup, and forming one is a COMMERCIAL judgement — order economics, lead times,
# what a vendor will quote for — which is why SCM forms it and Design does not.
#
# THE AGGREGATE IS COMPUTED, NEVER STORED
# ---------------------------------------
# Nothing here writes a BOQ row, copies one, or snapshots one. The aggregate is a Sum
# over `BOQItem.boq_quantity` grouped by `item_master` across the member sites, run at
# read time. That is not a performance compromise, it is the requirement: the per-site
# BOQ has to survive intact underneath the aggregate for per-site profitability and
# expense tracking later, and a stored roll-up is exactly how the two drift apart.
#
# THE LOCK IS ENFORCED AT THE CALLER, NOT IN THE PERMISSION HELPER
# ---------------------------------------------------------------
# `permissions.project_boq_is_group_locked()` is a separate predicate ANDed beside
# `user_can_edit_project_boq()` at every BOQ write path. The Part 0.6 helper answers
# "is this person the designer" and is NOT modified. See that predicate's docstring.
#
# NO save() OVERRIDES, NO SIGNALS. Both transitions — locking a group, soft-removing a
# membership — are explicit assignments in the views below, inside a transaction, next
# to the permission check that authorises them. Same rule as every prior part.
# ---------------------------------------------------------------------------

# The reason string stamped on a membership pulled out by a PM change request. A
# constant because two places must agree on it exactly: the change-request view writes
# it, and the group screen reads it back to explain to SCM why a site left.
CHANGE_REQUEST_REMOVAL_REASON = 'PM change request'


def active_group_membership(project):
    """The site's one live membership, or None.

    "One" is guaranteed by the partial unique constraint on
    SiteGroupMembership, not by this query — `.first()` here is picking the only row
    that can exist, not the first of several.
    """
    return (project.group_memberships
            .filter(removed_at__isnull=True)
            .select_related('group', 'group__program').first())


def remove_from_group(membership, actor, reason):
    """Soft-remove one membership and log it. THE ONLY PLACE A SITE LEAVES A GROUP.

    Both callers — SCM removing a site by hand, and a PM change request pulling one out
    (settled decision 6) — go through here, so the two cannot drift into disagreeing
    about which fields get stamped or whether the departure is logged at all. A silent
    removal would leave SCM reading an aggregate that quietly changed under them.

    The caller owns the transaction. The change-request path has one open for its own
    writes and the removal must share it.
    """
    membership.removed_at     = timezone.now()
    membership.removed_by     = actor
    membership.removal_reason = reason
    membership.save(update_fields=['removed_at', 'removed_by', 'removal_reason'])
    log_activity(membership.project, actor,
                 f'Removed from procurement group "{membership.group.name}": {reason}',
                 entity_type='SiteGroupMembership', entity_id=membership.pk,
                 action_code='site_group_site_removed')
    return membership


def _group_member_ids(group):
    """Primary keys of the sites currently in `group` — active memberships only."""
    return list(group.memberships
                .filter(removed_at__isnull=True)
                .values_list('project_id', flat=True))


def aggregate_group_boq(member_ids):
    """Sum BOQ quantities across `member_ids`, grouped by catalogue item.

    THE JOIN IS `item_master`, WHICH IS WHY BOQItemMaster EXISTS (Part 0.5). Every item
    aggregates the same way — there is no per-item rule on the master and none is
    invented here.

    `boq_quantity__gt=0` mirrors the guard `boq_detail`'s submit branch and
    `design_boq_complete()` both apply, so "a quantity was entered" means the same thing
    on this screen as on the two that produced it. A null or zero row contributes
    nothing to a sum, but counting it would inflate `site_count` into a claim that a site
    contributed to a line when it did not.

    UNLINKED ROWS ARE RETURNED, NOT DROPPED. A `BOQItem` with a null `item_master` cannot
    join, so its quantity is missing from the total — and a total that is silently short
    is worse than no total. They come back in `unlinked` for the template to shout about.
    Measured at build time: 0 such rows on OPEX sites, 2 on legacy Residential ones
    (deferred finding B1).

    Returns a dict; `contributions` maps item_master_id -> [(project_id, quantity)] so the
    per-line site count can be checked against the sites that produced it without a query
    per line.
    """
    lines = list(
        BOQItem.objects
        .filter(boq__project_id__in=member_ids, boq_quantity__gt=0,
                item_master__isnull=False)
        .values('item_master', 'item_master__code', 'item_master__description',
                'item_master__unit', 'item_master__sort_order')
        .annotate(total_quantity=Sum('boq_quantity'),
                  site_count=Count('boq__project', distinct=True))
        .order_by('item_master__sort_order', 'item_master__code')
    )

    # Per-site breakdown, one query for the whole table. Lets the screen show WHICH sites
    # are behind each line, which is what makes the aggregate auditable rather than a
    # number SCM has to trust.
    contributions = {}
    for row in (BOQItem.objects
                .filter(boq__project_id__in=member_ids, boq_quantity__gt=0,
                        item_master__isnull=False)
                .values('item_master', 'boq__project__project_id', 'boq_quantity')
                .order_by('boq__project__project_id')):
        contributions.setdefault(row['item_master'], []).append(
            (row['boq__project__project_id'], row['boq_quantity']))
    for line in lines:
        line['contributions'] = contributions.get(line['item_master'], [])

    unlinked = list(
        BOQItem.objects
        .filter(boq__project_id__in=member_ids, boq_quantity__gt=0,
                item_master__isnull=True)
        .select_related('boq__project')
        .order_by('boq__project__project_id', 'serial_no')
    )

    return {
        'lines':      lines,
        'unlinked':   unlinked,
        'item_count': len(lines),
        'site_count': len(member_ids),
    }


def _age_days(stamp, now):
    """Whole days between `stamp` and `now`, or None. Used for pool ageing only."""
    if stamp is None:
        return None
    return (now - stamp).days


def post_qc_pool(program, now=None):
    """Released sites in this tender that are in NO group, oldest release first.

    THIS QUEUE IS THE POINT OF THE SCREEN. Without it, sites pass QC and pile up while
    procurement receives nothing — the failure is silent on both sides, because Design
    has finished and SCM was never told. Age is days since `released_at`.

    Sites with a LIVE membership are dropped; a site whose membership was REMOVED is back
    in the pool, which is what makes settled decision 6 recoverable — a change request
    returns the site to the queue rather than losing it.

    THE EXCLUSION IS AN EXPLICIT SUBQUERY ON project_id, NOT
    `.exclude(project__group_memberships__removed_at__isnull=True)`. That spelling is
    wrong and silently empties this screen. Django compiles it to

        NOT EXISTS (SELECT 1 FROM project U1
                    LEFT OUTER JOIN sitegroupmembership U2 ON U1.id = U2.project_id
                    WHERE U2.removed_at IS NULL AND U1.id = assignment.project_id)

    — a LEFT JOIN, so a project with NO membership rows still produces one phantom row
    whose U2.removed_at is NULL. The condition matches it, EXISTS is true, and the
    project is excluded. The result is a pool that omits precisely the sites it exists to
    show: every released site that has never been in a group. It only ever returned the
    sites that had once been grouped and removed, which is why it looked correct against
    data that had exactly that shape.

    Filtering `SiteGroupMembership` directly and excluding on `project__in` compiles to a
    plain subquery over rows that actually exist, with no outer join and no phantom row.
    """
    now = now or timezone.now()
    rows = list(
        DesignAssignment.objects
        .filter(project__program=program, project__is_deleted=False,
                status=DESIGN_RELEASED)
        .exclude(project__in=SiteGroupMembership.objects
                 .filter(removed_at__isnull=True).values('project_id'))
        .select_related('project', 'released_by__user')
        .order_by('released_at')
    )
    for a in rows:
        a.age_days = _age_days(a.released_at, now)
    return rows


def tender_release_completeness(program):
    """(released, total) for a tender — 'X of Y sites are released'.

    Shown beside every aggregate. SCM has to know whether they are looking at a final
    quantity or a running total, and the aggregate itself cannot tell them: a group of
    three sites looks identical whether the tender has three sites or thirty.

    Counts DesignAssignment rows, never `Project.status` (settled decision 9) — OPEX
    sites are `Draft` and stay `Draft`.
    """
    total = program.sites.filter(is_deleted=False).count()
    released = DesignAssignment.objects.filter(
        project__program=program, project__is_deleted=False,
        status=DESIGN_RELEASED).count()
    return released, total


def unresolved_change_requests_for(member_ids):
    """Change requests on these sites that never produced a new attempt.

    Same definition as `_open_change_requests()` (Part 4): `resulting_attempt` is null.
    Locking a group over one of these would freeze a BOQ that is already known to need
    rework. Note the UI cannot currently produce such a row — `design_change_request()`
    sets `resulting_attempt` in the same transaction that creates it (deferred finding
    G6) — so in practice this guard fires against rows created in Django admin, by an
    import, or by a future part that queues change requests for approval.
    """
    return list(DesignChangeRequest.objects
                .filter(attempt__assignment__project_id__in=member_ids,
                        resulting_attempt__isnull=True)
                .select_related('attempt__assignment__project', 'requested_by__user'))


def _group_rows(program):
    """Every group under a tender with its member count and lock state, newest first."""
    return list(
        program.site_groups
        .select_related('created_by__user', 'locked_by__user')
        .annotate(member_count=Count(
            'memberships', filter=Q(memberships__removed_at__isnull=True)))
        .order_by('-created_at')
    )


def _tender_or_404(pk):
    return get_object_or_404(Program, pk=pk, is_deleted=False, program_type='OPEX')


def _group_or_404(pk):
    return get_object_or_404(
        SiteGroup, pk=pk, program__is_deleted=False, program__program_type='OPEX')


# ---------------------------------------------------------------------------
# 17. Group formation
# ---------------------------------------------------------------------------

@login_required
def site_group_list(request, pk):
    """SCM's group screen for one tender: the groups, the post-QC pool, and the form
    that creates the next group.

    READ is SCM, Admin and Design Head authority; WRITE is SCM alone (Part 6 §1 and §3).
    The Head can see what became of the sites he released and nothing more — he does not
    own the order, so he does not form the batch.
    """
    if not user_can_view_site_groups(request.user):
        return HttpResponseForbidden(
            'Procurement groups are visible to SCM, Admin and the Design Head.')

    program = _tender_or_404(pk)
    released, total = tender_release_completeness(program)
    pool = post_qc_pool(program)

    return render(request, 'projects/design/site_groups.html', {
        'program':      program,
        'groups':       _group_rows(program),
        'pool':         pool,
        'released':     released,
        'total_sites':  total,
        'can_manage':   user_can_manage_site_groups(request.user),
    })


@login_required
def site_group_create(request, pk):
    """SCM creates a group under a tender and optionally seeds it with sites.

    Sites are optional at creation — a named empty draft is a legitimate intermediate
    state while SCM decides what goes in it. Locking an empty group is not (see
    site_group_lock).
    """
    if not user_can_manage_site_groups(request.user):
        return HttpResponseForbidden('Only SCM may create a procurement group.')
    program = _tender_or_404(pk)
    if request.method != 'POST':
        return redirect('site_group_list', pk=program.pk)

    name = (request.POST.get('name') or '').strip()
    if not name:
        messages.error(request, 'Give the group a name — SCM will be reading it on a '
                                'purchase order.')
        return redirect('site_group_list', pk=program.pk)

    profile = request.user.profile
    with transaction.atomic():
        group = SiteGroup.objects.create(
            program=program, name=name, status=SITE_GROUP_DRAFT,
            created_by=profile, notes=(request.POST.get('notes') or '').strip())

    added, refused = _add_sites(group, request.POST.getlist('project_ids'), profile)
    messages.success(request, f'Group "{group.name}" created.'
                              + (f' {len(added)} site(s) added.' if added else ''))
    for line in refused:
        messages.error(request, line)
    return redirect('site_group_detail', pk=group.pk)


def _add_sites(group, project_ids, actor):
    """Add each id to `group`, one at a time. Returns (added, refused_messages).

    PER-SITE, NOT BULK, AND EACH IN ITS OWN SAVEPOINT. Two reasons, both deliberate:

      * SCM selects ten sites and one of them is already spoken for. Failing the whole
        batch teaches them to add sites one at a time, which is worse than telling them
        which one it was.
      * The exclusivity rule is enforced by a PARTIAL UNIQUE CONSTRAINT in the database
        (settled decision 2), not by the pre-check above it. The pre-check exists for the
        error message; the `IntegrityError` catch is what makes the rule true under a
        concurrent add. Without the savepoint, one IntegrityError would poison the whole
        transaction and take the successful adds down with it.
    """
    added, refused = [], []
    for raw in project_ids:
        if not str(raw).isdigit():
            continue
        project = Project.objects.filter(pk=int(raw), is_deleted=False).first()
        if project is None:
            refused.append('One selected site no longer exists.')
            continue

        if project.program_id != group.program_id:
            refused.append(f'{project.project_id}: belongs to a different tender.')
            continue

        assignment = getattr(project, 'design_assignment', None)
        if assignment is None or assignment.status != DESIGN_RELEASED:
            state = assignment.get_status_display() if assignment else 'design not started'
            refused.append(f'{project.project_id}: not released ({state}) — only released '
                           f'sites can be grouped for procurement.')
            continue

        existing = active_group_membership(project)
        if existing is not None:
            refused.append(f'{project.project_id}: already in group '
                           f'"{existing.group.name}" ({existing.group.status}).')
            continue

        try:
            with transaction.atomic():
                membership = SiteGroupMembership.objects.create(
                    group=group, project=project, added_by=actor)
                log_activity(project, actor,
                             f'Added to procurement group "{group.name}"',
                             entity_type='SiteGroupMembership', entity_id=membership.pk,
                             action_code='site_group_site_added')
        except IntegrityError:
            # The database refused it. Reachable when two adds race, or when the view's
            # pre-check above is somehow bypassed — either way the constraint is the
            # authority and this is the message that says so.
            refused.append(f'{project.project_id}: refused by the database — a site may '
                           f'be in only one group at a time.')
            continue

        added.append(project)
    return added, refused


@login_required
def site_group_add_sites(request, pk):
    """SCM adds released, ungrouped sites to a DRAFT group."""
    if not user_can_manage_site_groups(request.user):
        return HttpResponseForbidden('Only SCM may add sites to a procurement group.')
    group = _group_or_404(pk)
    if request.method != 'POST':
        return redirect('site_group_detail', pk=group.pk)

    if group.status != SITE_GROUP_DRAFT:
        messages.error(request, f'"{group.name}" is locked — its membership is final.')
        return redirect('site_group_detail', pk=group.pk)

    added, refused = _add_sites(group, request.POST.getlist('project_ids'),
                                request.user.profile)
    if added:
        messages.success(request, f'{len(added)} site(s) added to "{group.name}".')
    for line in refused:
        messages.error(request, line)
    if not added and not refused:
        messages.error(request, 'No sites were selected.')
    return redirect('site_group_detail', pk=group.pk)


@login_required
def site_group_remove_site(request, pk):
    """SCM removes one site from a DRAFT group, with a reason."""
    if not user_can_manage_site_groups(request.user):
        return HttpResponseForbidden('Only SCM may remove a site from a procurement group.')
    group = _group_or_404(pk)
    if request.method != 'POST':
        return redirect('site_group_detail', pk=group.pk)

    if group.status != SITE_GROUP_DRAFT:
        messages.error(request, f'"{group.name}" is locked — its membership is final.')
        return redirect('site_group_detail', pk=group.pk)

    raw = request.POST.get('membership_id', '')
    if not raw.isdigit():
        messages.error(request, 'No site was selected.')
        return redirect('site_group_detail', pk=group.pk)

    # `group=group` scopes the membership to THIS group — an id from another group is a
    # 404, not a silent no-op reported as success.
    membership = get_object_or_404(
        SiteGroupMembership, pk=int(raw), group=group, removed_at__isnull=True)
    reason = (request.POST.get('reason') or '').strip() or 'Removed by SCM'

    with transaction.atomic():
        remove_from_group(membership, request.user.profile, reason)

    messages.success(request, f'{membership.project.project_id} removed from '
                              f'"{group.name}". Its own BOQ is unchanged.')
    return redirect('site_group_detail', pk=group.pk)


# ---------------------------------------------------------------------------
# 18. Aggregated BOQ and the lock
# ---------------------------------------------------------------------------

@login_required
def site_group_detail(request, pk):
    """One group: its members, its aggregated BOQ, what left it and why, and the lock.

    The consolidated requirement is what SCM raises an order against, so everything that
    qualifies it is on the same screen — how complete the tender is, which sites are
    behind each line, and any BOQ row that could not be aggregated.
    """
    if not user_can_view_site_groups(request.user):
        return HttpResponseForbidden(
            'Procurement groups are visible to SCM, Admin and the Design Head.')

    group = _group_or_404(pk)
    memberships = list(group.memberships
                       .filter(removed_at__isnull=True)
                       .select_related('project', 'project__design_assignment',
                                       'added_by__user')
                       .order_by('project__project_id'))
    removed = list(group.memberships
                   .filter(removed_at__isnull=False)
                   .select_related('project', 'removed_by__user')
                   .order_by('-removed_at'))

    member_ids = [m.project_id for m in memberships]
    agg = aggregate_group_boq(member_ids)
    released, total = tender_release_completeness(group.program)

    return render(request, 'projects/design/site_group_detail.html', {
        'program':      group.program,
        'group':        group,
        'memberships':  memberships,
        'removed':      removed,
        'agg':          agg,
        'released':     released,
        'total_sites':  total,
        'blockers':     (unresolved_change_requests_for(member_ids)
                         if group.status == SITE_GROUP_DRAFT else []),
        'pool':         (post_qc_pool(group.program)
                         if group.status == SITE_GROUP_DRAFT else []),
        'can_manage':   user_can_manage_site_groups(request.user),
        'change_request_reason': CHANGE_REQUEST_REMOVAL_REASON,
    })


@login_required
def site_group_lock(request, pk):
    """SCM locks a group. The BOQ of every member site becomes read-only from here on.

    THERE IS NO UNLOCK, DELIBERATELY. Once quantities are committed to a purchase, the
    correction is a variance against the order, not an edit to the BOQ it was raised
    from. Building half of a variance process as an "unlock" button would be worse than
    the honest gap — it would let a quantity move after an order was placed against it
    with nothing recording that it had.

    Two refusals before the write, and both are about not freezing something meaningless:
    an empty group locks nothing, and a member with an unresolved change request is
    already known to need rework.
    """
    if not user_can_manage_site_groups(request.user):
        return HttpResponseForbidden('Only SCM may lock a procurement group.')
    group = _group_or_404(pk)
    if request.method != 'POST':
        return redirect('site_group_detail', pk=group.pk)

    def _back(msg, ok=False):
        (messages.success if ok else messages.error)(request, msg)
        return redirect('site_group_detail', pk=group.pk)

    if group.status == SITE_GROUP_LOCKED:
        return _back(f'"{group.name}" is already locked.')

    member_ids = _group_member_ids(group)
    if not member_ids:
        return _back(f'"{group.name}" has no sites — there is nothing to lock.')

    blockers = unresolved_change_requests_for(member_ids)
    if blockers:
        sites = ', '.join(sorted({b.attempt.assignment.project.project_id
                                  for b in blockers}))
        return _back(f'"{group.name}" cannot be locked: {sites} '
                     f'{"has" if len(blockers) == 1 else "have"} an unresolved change '
                     f'request. Resolve it, or remove the site from the group first.')

    profile = request.user.profile
    now = timezone.now()
    with transaction.atomic():
        group.status    = SITE_GROUP_LOCKED
        group.locked_by = profile
        group.locked_at = now
        group.save(update_fields=['status', 'locked_by', 'locked_at'])

        # One log line per member site, on the site itself. The group is not a project, so
        # ActivityLog cannot hang the event off it — and the site is where a PM or designer
        # will look to find out why the BOQ stopped accepting edits.
        for project in Project.objects.filter(pk__in=member_ids):
            log_activity(project, profile,
                         f'BOQ locked — site group "{group.name}" locked for procurement',
                         entity_type='SiteGroup', entity_id=group.pk,
                         action_code='site_group_locked')

    return _back(f'"{group.name}" locked. The BOQ of {len(member_ids)} site(s) is now '
                 f'read-only. There is no unlock — a later change needs a variance '
                 f'against the order.', ok=True)


# ---------------------------------------------------------------------------
# 19. SCM handoff — the OPEX section of the SCM dashboard
# ---------------------------------------------------------------------------

def scm_opex_tender_rows(now=None):
    """Per-tender procurement rows for the SCM dashboard's OPEX section.

    KEYED OFF SiteGroup AND DesignAssignment, NEVER `Project.status` (settled decision 9).
    OPEX sites are created `Draft` and nothing promotes them (deferred finding H1), so a
    status filter would return an empty section forever. No existing dashboard queryset is
    read, widened or modified — this is its own query set from its own tables.

    Returns one row per OPEX tender that has at least one design assignment, so tenders
    where design has not started do not pad the section.
    """
    now = now or timezone.now()
    rows = []
    programs = (Program.objects
                .filter(is_deleted=False, program_type='OPEX')
                .order_by('name'))
    for program in programs:
        assignments = (DesignAssignment.objects
                       .filter(project__program=program, project__is_deleted=False)
                       .count())
        if not assignments:
            continue
        released, total = tender_release_completeness(program)
        groups = _group_rows(program)
        pool = post_qc_pool(program, now)
        rows.append({
            'program':      program,
            'total_sites':  total,
            'in_design':    assignments,
            'released':     released,
            'groups':       groups,
            'group_count':  len(groups),
            'locked_count': sum(1 for g in groups if g.status == SITE_GROUP_LOCKED),
            'pool':         pool,
            'pool_count':   len(pool),
            'oldest_pool_age': pool[0].age_days if pool else None,
        })
    return rows
