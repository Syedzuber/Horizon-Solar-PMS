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
from django.db import transaction
from django.db.models import Max
from django.http import Http404, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_date

from .decorators import login_required
from .design_storage import (
    DesignStorageError, build_design_path, get_design_file_url, upload_design_file,
)
from .models import (
    Program, Project, UserProfile, BOQ, DesignAssignment, DueDateCommitment,
    DesignAttempt, ArkaSubmission, DesignFile, DesignChangeRequest, log_activity,
    DESIGN_AWAITING_SURVEY, DESIGN_AWAITING_ALLOCATION, DESIGN_ALLOCATED,
    DESIGN_DUE_DATE_PROPOSED, DESIGN_IN_DESIGN, DESIGN_SURVEY_RETURNED,
    DESIGN_ARKA_SUBMITTED, DESIGN_ARKA_REJECTED, DESIGN_ARTIFACTS_UPLOADED,
    DESIGN_IN_QC, DESIGN_QC_FAILED, DESIGN_RELEASED,
    ARKA_PENDING, ARKA_APPROVED, ARKA_REJECTED,
    QC_PENDING, QC_PASSED, QC_FAILED,
    ATTEMPT_REASON_INITIAL, ATTEMPT_REASON_QC_FAILED, ATTEMPT_REASON_PM_CHANGE_REQUEST,
    DESIGN_FILE_CAD_PDF, DESIGN_FILE_CAD_DWG,
    DESIGN_FILE_BOQ_EXCEL, DESIGN_FILE_BOQ_PDF, DESIGN_FILE_KIND_CHOICES,
)
from .permissions import (
    user_can_edit_project_boq, user_can_qc_design, user_can_request_design_change,
    user_can_view_design, user_has_design_head_authority, user_is_assigned_designer,
    user_is_design_head, user_is_design_head_deputy,
)

logger = logging.getLogger(__name__)

# Statuses at which the site has not yet started design work. Reallocation is allowed
# only while the assignment is still in one of these — see design_allocate().
REALLOCATABLE_STATUSES = (DESIGN_AWAITING_ALLOCATION, DESIGN_ALLOCATED,
                          DESIGN_DUE_DATE_PROPOSED)

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

# The two CAD kinds. At least one is required before an attempt can reach
# `artifacts_uploaded`; the BOQ kinds are optional attachments and never count.
CAD_KINDS = (DESIGN_FILE_CAD_PDF, DESIGN_FILE_CAD_DWG)

# Every kind design_artifact_upload() accepts. A `kind` outside this tuple is refused
# rather than silently defaulted — the whitelist is the enforcement point, not the
# select element in the template.
UPLOADABLE_KINDS = (DESIGN_FILE_CAD_PDF, DESIGN_FILE_CAD_DWG,
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
CHANGE_REQUEST_STATUSES = (DESIGN_IN_QC, DESIGN_QC_FAILED, DESIGN_IN_DESIGN,
                           DESIGN_ARKA_SUBMITTED, DESIGN_ARKA_REJECTED,
                           DESIGN_ARTIFACTS_UPLOADED)


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
    """Status to restore when the Head clears a block by uploading a replacement survey.

    DERIVED, not stored — Part 2 adds no schema. The prior state is recoverable from the
    rows that already exist: no designer means the site never left the allocation queue;
    an approved current commitment means design was under way; an unapproved one means
    the handshake was mid-flight.
    """
    if assignment.assigned_to_id is None:
        return DESIGN_AWAITING_ALLOCATION
    current = assignment.due_date_commitments.filter(is_current=True).first()
    if current is None:
        return DESIGN_ALLOCATED
    if current.approved_at is not None:
        return DESIGN_IN_DESIGN
    return DESIGN_DUE_DATE_PROPOSED


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
        current = None
        if assignment is not None:
            current = assignment.due_date_commitments.filter(is_current=True).first()
        rows.append({
            'site':          site,
            'assignment':    assignment,
            'status':        assignment.status if assignment else DESIGN_AWAITING_SURVEY,
            'has_survey':    bool(assignment and assignment.survey_file_path),
            'current_due':   current,
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
        current = assignment.due_date_commitments.filter(is_current=True).first()
        rows.append({
            'assignment':  assignment,
            'site':        assignment.project,
            'current_due': current,
            'can_propose': assignment.status == DESIGN_ALLOCATED,
            'awaiting':    assignment.status == DESIGN_DUE_DATE_PROPOSED,
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
            f'unless the designer has marked the site blocked.')
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
            action = (f'Blocked flag cleared by replacement survey; status restored to '
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
                                  f'block cleared ({assignment.get_status_display()}).')
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

def _allocate_one(assignment, designer, actor):
    """Core allocation, shared by the single and bulk paths so their rules cannot drift.
    Raises ValueError with a user-facing message; callers own the transaction.

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
        raise ValueError('is blocked on an inadequate survey — upload a replacement first')
    if assignment.status not in REALLOCATABLE_STATUSES:
        # Reallocation after work has started is out of scope for Part 2.
        raise ValueError(
            f'has already started design work (status {assignment.status}); '
            f'reallocation at this stage is not supported yet')

    previous = assignment.assigned_to
    assignment.assigned_to = designer
    assignment.assigned_by = actor
    assignment.assigned_at = timezone.now()
    assignment.status = DESIGN_ALLOCATED
    assignment.save()

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
    log_activity(assignment.project, actor, detail,
                 entity_type='DesignAssignment', entity_id=assignment.pk, action_code=code)


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
            _allocate_one(assignment, designer, request.user.profile)
    except ValueError as exc:
        messages.error(request, f'{project.project_id} {exc}.')
        return redirect('design_head_sites', pk=project.program_id)

    messages.success(request, f'{project.project_id} allocated to '
                              f'{designer.user.get_full_name() or designer.user.username}.')
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
    try:
        with transaction.atomic():
            allocated = []
            for project_id in project_ids:
                site = get_object_or_404(
                    Project, project_id=project_id, is_deleted=False,
                    project_type='OPEX', program=program)
                assignment = getattr(site, 'design_assignment', None)
                if assignment is None or not assignment.survey_file_path:
                    raise ValueError(f'{site.project_id} cannot be allocated before its '
                                     f'survey is uploaded')
                _allocate_one(assignment, designer, actor)
                allocated.append(site.project_id)
    except ValueError as exc:
        messages.error(request, f'Nothing was allocated — {exc}.')
        return redirect('design_head_sites', pk=program.pk)

    messages.success(
        request,
        f'{len(allocated)} site(s) allocated to '
        f'{designer.user.get_full_name() or designer.user.username}: {", ".join(allocated)}.')
    return redirect('design_head_sites', pk=program.pk)


# ---------------------------------------------------------------------------
# 4. Due date handshake
# ---------------------------------------------------------------------------

def _current_commitment(assignment):
    return assignment.due_date_commitments.filter(is_current=True).first()


@login_required
def design_due_date_propose(request, project_id):
    """The DESIGNER proposes a due date. Only the assigned designer — not the Head.

    One half of the two-sided handshake: if the Head could propose, approving his own
    proposal would make the handshake meaningless.
    """
    project = _opex_site(project_id)
    assignment = getattr(project, 'design_assignment', None)
    if assignment is None or not user_is_assigned_designer(request.user, assignment):
        return HttpResponseForbidden('Only the designer allocated to this site may propose a due date.')
    if request.method != 'POST':
        return redirect('design_my_sites')

    if assignment.status != DESIGN_ALLOCATED:
        return _deny(request, f'{project.project_id}: a due date can only be proposed '
                              f'while the site is allocated and not yet agreed.',
                     'design_my_sites')

    proposed = parse_date((request.POST.get('proposed_date') or '').strip())
    if proposed is None:
        return _deny(request, 'Please provide a valid date.', 'design_my_sites')
    if proposed < timezone.localdate():
        return _deny(request, 'The proposed due date cannot be in the past.', 'design_my_sites')

    profile = request.user.profile
    with transaction.atomic():
        # The partial unique constraint permits only one is_current row per assignment,
        # so any previous one is stood down first, in the same transaction.
        assignment.due_date_commitments.filter(is_current=True).update(is_current=False)
        DueDateCommitment.objects.create(
            assignment=assignment, proposed_date=proposed,
            proposed_by=profile, is_current=True)
        assignment.status = DESIGN_DUE_DATE_PROPOSED
        assignment.save(update_fields=['status', 'updated_at'])
        log_activity(project, profile, f'Due date {proposed} proposed',
                     entity_type='DesignAssignment', entity_id=assignment.pk,
                     action_code='design_due_date_proposed')

    messages.success(request, f'{project.project_id}: due date {proposed} proposed for approval.')
    return redirect('design_my_sites')


@login_required
def design_due_date_approve(request, project_id):
    """The HEAD approves the current proposal. Completes the handshake."""
    project = _opex_site(project_id)
    if not user_has_design_head_authority(request.user):
        return HttpResponseForbidden('Design Head only.')
    if request.method != 'POST':
        return redirect('design_head_sites', pk=project.program_id)

    assignment = getattr(project, 'design_assignment', None)
    back = project.program_id
    if assignment is None or assignment.status != DESIGN_DUE_DATE_PROPOSED:
        messages.error(request, f'{project.project_id}: there is no due date awaiting approval.')
        return redirect('design_head_sites', pk=back)

    commitment = _current_commitment(assignment)
    if commitment is None:
        messages.error(request, f'{project.project_id}: no current due date commitment found.')
        return redirect('design_head_sites', pk=back)

    profile = request.user.profile
    with transaction.atomic():
        commitment.approved_by = profile
        commitment.approved_at = timezone.now()
        commitment.save(update_fields=['approved_by', 'approved_at'])
        assignment.status = DESIGN_IN_DESIGN
        assignment.save(update_fields=['status', 'updated_at'])
        log_activity(project, profile, f'Due date {commitment.proposed_date} approved',
                     entity_type='DesignAssignment', entity_id=assignment.pk,
                     action_code='design_due_date_approved')

    messages.success(request, f'{project.project_id}: due date {commitment.proposed_date} '
                              f'approved — design can start.')
    return redirect('design_head_sites', pk=back)


@login_required
def design_due_date_reject(request, project_id):
    """The HEAD rejects the proposal, returning the site to `allocated` so the designer
    proposes again. The rejected commitment is stood down, not deleted — the history of
    what was proposed and refused stays on the record."""
    project = _opex_site(project_id)
    if not user_has_design_head_authority(request.user):
        return HttpResponseForbidden('Design Head only.')
    if request.method != 'POST':
        return redirect('design_head_sites', pk=project.program_id)

    assignment = getattr(project, 'design_assignment', None)
    back = project.program_id
    if assignment is None or assignment.status != DESIGN_DUE_DATE_PROPOSED:
        messages.error(request, f'{project.project_id}: there is no due date awaiting approval.')
        return redirect('design_head_sites', pk=back)

    reason = (request.POST.get('reason') or '').strip()
    profile = request.user.profile
    with transaction.atomic():
        commitment = _current_commitment(assignment)
        assignment.due_date_commitments.filter(is_current=True).update(is_current=False)
        assignment.status = DESIGN_ALLOCATED
        assignment.save(update_fields=['status', 'updated_at'])
        log_activity(
            project, profile,
            f'Due date {commitment.proposed_date if commitment else ""} rejected'
            + (f': {reason}' if reason else ''),
            entity_type='DesignAssignment', entity_id=assignment.pk,
            action_code='design_due_date_rejected')

    messages.success(request, f'{project.project_id}: due date rejected — the designer '
                              f'has been asked to propose another.')
    return redirect('design_head_sites', pk=back)


@login_required
def design_due_date_change(request, project_id):
    """Change an ALREADY-APPROVED due date.

    Never an in-place edit: a new DueDateCommitment is inserted with a mandatory
    change_reason and the previous row is stood down in the same transaction. The
    revision count is therefore `commitments - 1`, derived by counting rows — there is
    no counter field to drift.

    Either party may initiate, and the new date is a proposal: status returns to
    due_date_proposed so the Head must approve it, exactly like the first one.
    """
    project = _opex_site(project_id)
    assignment = getattr(project, 'design_assignment', None)
    if assignment is None:
        raise Http404('No design assignment for this site.')

    is_head     = user_has_design_head_authority(request.user)
    is_designer = user_is_assigned_designer(request.user, assignment)
    if not (is_head or is_designer):
        return HttpResponseForbidden('Only the Design Head or the allocated designer may '
                                     'change an agreed due date.')
    fallback = 'design_head_sites' if is_head else 'design_my_sites'
    if request.method != 'POST':
        return (redirect(fallback, pk=project.program_id) if is_head
                else redirect(fallback))

    def _back(msg, ok=False):
        (messages.success if ok else messages.error)(request, msg)
        return (redirect('design_head_sites', pk=project.program_id) if is_head
                else redirect('design_my_sites'))

    if assignment.status != DESIGN_IN_DESIGN:
        return _back(f'{project.project_id}: only an agreed due date can be changed this way.')

    reason = (request.POST.get('change_reason') or '').strip()
    if not reason:
        return _back(f'{project.project_id}: a reason is required to change an agreed due date.')

    proposed = parse_date((request.POST.get('proposed_date') or '').strip())
    if proposed is None:
        return _back('Please provide a valid date.')
    if proposed < timezone.localdate():
        return _back('The new due date cannot be in the past.')

    profile = request.user.profile
    with transaction.atomic():
        # Stand the old row down BEFORE inserting, or the partial unique constraint
        # (one is_current row per assignment) rejects the insert.
        assignment.due_date_commitments.filter(is_current=True).update(is_current=False)
        DueDateCommitment.objects.create(
            assignment=assignment, proposed_date=proposed, proposed_by=profile,
            change_reason=reason, is_current=True)
        assignment.status = DESIGN_DUE_DATE_PROPOSED
        assignment.save(update_fields=['status', 'updated_at'])
        log_activity(project, profile,
                     f'Agreed due date changed to {proposed}: {reason}',
                     entity_type='DesignAssignment', entity_id=assignment.pk,
                     action_code='design_due_date_changed')

    return _back(f'{project.project_id}: new due date {proposed} proposed — '
                 f'awaiting Design Head approval.', ok=True)


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
        return _deny(request, f'{project.project_id} is already flagged as blocked.',
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
        log_activity(project, profile, f'Site flagged blocked — survey inadequate: {reason}',
                     entity_type='DesignAssignment', entity_id=assignment.pk,
                     action_code='design_blocked')

    messages.success(request, f'{project.project_id} flagged as blocked. The Design Head '
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
    """The current Arka ONLY IF it has been approved. This is the object CAD and BOQ
    artifacts pair to — deliberately not "the most recently approved version", so a
    superseded approval can never become the parent of a new artifact."""
    arka = _current_arka(attempt)
    if arka is not None and arka.verdict == ARKA_APPROVED:
        return arka
    return None


def _require_approved_arka(attempt):
    """Return the approved current Arka, or raise ValueError with the message the
    designer needs to see.

    Single chokepoint for settled decision 1. Every artifact write path calls this;
    none of them re-derives the rule, so the three call sites cannot drift.
    """
    if attempt is None:
        raise ValueError('no design attempt has been opened yet — submit an Arka first')
    arka = _current_arka(attempt)
    if arka is None:
        raise ValueError('no Arka has been submitted yet. CAD and BOQ can only be '
                         'uploaded against an approved Arka')
    if arka.verdict == ARKA_PENDING:
        raise ValueError(f'Arka v{arka.version} is still awaiting the Design Head\'s '
                         f'approval. CAD and BOQ cannot be uploaded until it is approved')
    if arka.verdict == ARKA_REJECTED:
        raise ValueError(f'Arka v{arka.version} was rejected. Submit a new Arka version '
                         f'and have it approved before uploading CAD or BOQ')
    return arka


def _maybe_advance_to_artifacts_uploaded(assignment, attempt, actor):
    """Section 5 — evaluate the progression rule after every Part 3 write.

    The attempt moves `arka_submitted` -> `artifacts_uploaded` once it has ALL THREE:
    an approved current Arka, at least one current CAD file, and boq_submitted_at set.

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
    if not attempt.design_files.filter(kind__in=CAD_KINDS, is_current=True).exists():
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
                               'submitted_by__user', 'reviewed_by__user')
                               .order_by('-version')) if attempt else []),
        'files':          files,
        'has_cad':        any(f.kind in CAD_KINDS and f.is_current for f in files),
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
        'can_edit_boq': user_can_edit_project_boq(request.user, project),
    })
    return render(request, 'projects/design/site_workspace.html', ctx)


@login_required
def design_head_review(request, project_id):
    """The Design Head's Arka review screen for one site: the pending Arka with its
    capacity and link, the approve and reject-with-reason actions, the full version
    history, and the artifacts submitted so far."""
    project = _opex_site(project_id)
    if not user_has_design_head_authority(request.user):
        return HttpResponseForbidden('Design Head only.')

    assignment = getattr(project, 'design_assignment', None)
    if assignment is None:
        raise Http404('No design assignment for this site.')

    ctx = _workspace_context(project, assignment)
    arka = ctx['arka']
    ctx['can_verdict'] = bool(
        arka is not None
        and arka.verdict == ARKA_PENDING
        and assignment.status == DESIGN_ARKA_SUBMITTED
    )
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
    """The Arka a verdict may be recorded against, or a (None, message) pair.

    Shared by approve and reject so the two cannot disagree about which submission is
    reviewable. The designer cannot reach either endpoint: both are gated on
    `user_has_design_head_authority` by their callers, so nobody can approve their own Arka.
    """
    assignment = getattr(project, 'design_assignment', None)
    if assignment is None:
        return None, None, f'{project.project_id}: no design assignment for this site.'
    if assignment.status != DESIGN_ARKA_SUBMITTED:
        return None, None, (f'{project.project_id}: there is no Arka awaiting a verdict '
                            f'(status "{assignment.get_status_display()}").')
    attempt = _current_attempt(assignment)
    arka = _current_arka(attempt)
    if arka is None:
        return None, None, f'{project.project_id}: no current Arka submission found.'
    if arka.verdict != ARKA_PENDING:
        return None, None, (f'{project.project_id}: Arka v{arka.version} has already '
                            f'been {arka.get_verdict_display().lower()}.')
    return assignment, arka, None


@login_required
def design_arka_approve(request, project_id):
    """The Design HEAD approves the current Arka version.

    Status deliberately STAYS at `arka_submitted`: approval unlocks CAD and BOQ upload,
    it does not complete the package. The next status is `artifacts_uploaded` and it is
    reached by _maybe_advance_to_artifacts_uploaded(), not here — no new status value
    is invented for "Arka approved, artifacts outstanding".
    """
    project = _opex_site(project_id)
    if not user_has_design_head_authority(request.user):
        return HttpResponseForbidden('Design Head only.')
    if request.method != 'POST':
        return redirect('design_head_review', project_id=project.project_id)

    assignment, arka, error = _verdict_target(request, project)
    if error:
        messages.error(request, error)
        return redirect('design_head_review', project_id=project.project_id)

    profile = request.user.profile
    with transaction.atomic():
        arka.verdict     = ARKA_APPROVED
        arka.reviewed_by = profile
        arka.reviewed_at = timezone.now()
        arka.save(update_fields=['verdict', 'reviewed_by', 'reviewed_at'])
        log_activity(project, profile,
                     f'Arka v{arka.version} approved ({arka.capacity_kw} kW)',
                     entity_type='ArkaSubmission', entity_id=arka.pk,
                     action_code='design_arka_approved')
        # A re-approval on an attempt that already carries CAD and a submitted BOQ
        # would otherwise leave the status behind; evaluating here costs one query.
        _maybe_advance_to_artifacts_uploaded(
            assignment, _current_attempt(assignment), profile)

    messages.success(request, f'{project.project_id}: Arka v{arka.version} approved — '
                              f'the designer can now upload CAD and enter the BOQ.')
    return redirect('design_head_review', project_id=project.project_id)


@login_required
def design_arka_reject(request, project_id):
    """The Design HEAD rejects the current Arka version. A reason is MANDATORY.

    The reason is checked here so the designer gets a usable message, and the Part 1
    CHECK constraint `rejection_reason_required_when_rejected` enforces the same rule
    at the database level for any writer that bypasses this view. Both were verified
    this session.

    The rejected submission stays is_current=True — it is the record of what was
    rejected — and is stood down only when the designer submits the replacement.
    """
    project = _opex_site(project_id)
    if not user_has_design_head_authority(request.user):
        return HttpResponseForbidden('Design Head only.')
    if request.method != 'POST':
        return redirect('design_head_review', project_id=project.project_id)

    assignment, arka, error = _verdict_target(request, project)
    if error:
        messages.error(request, error)
        return redirect('design_head_review', project_id=project.project_id)

    reason = (request.POST.get('rejection_reason') or '').strip()
    if not reason:
        messages.error(request, f'{project.project_id}: a rejection reason is required '
                                f'— the designer cannot act on "rejected" alone.')
        return redirect('design_head_review', project_id=project.project_id)

    profile = request.user.profile
    with transaction.atomic():
        arka.verdict          = ARKA_REJECTED
        arka.rejection_reason = reason
        arka.reviewed_by      = profile
        arka.reviewed_at      = timezone.now()
        arka.save(update_fields=['verdict', 'rejection_reason',
                                 'reviewed_by', 'reviewed_at'])
        assignment.status = DESIGN_ARKA_REJECTED
        assignment.save(update_fields=['status', 'updated_at'])
        log_activity(project, profile,
                     f'Arka v{arka.version} rejected: {reason}',
                     entity_type='ArkaSubmission', entity_id=arka.pk,
                     action_code='design_arka_rejected')

    messages.success(request, f'{project.project_id}: Arka v{arka.version} rejected — '
                              f'the designer has been asked to submit a new version.')
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
    """Whether an attempt actually has a reviewable package: approved current Arka, at
    least one current CAD file, and a BOQ marked complete.

    Same three conditions Part 3's _maybe_advance_to_artifacts_uploaded() evaluates,
    re-checked here at QC start rather than trusting the status alone — the status is a
    cached conclusion, these rows are the evidence.
    """
    if attempt is None:
        return False
    if _approved_arka(attempt) is None:
        return False
    if not attempt.design_files.filter(kind__in=CAD_KINDS, is_current=True).exists():
        return False
    return attempt.boq_submitted_at is not None


# ---------------------------------------------------------------------------
# 12. QC review
# ---------------------------------------------------------------------------

def _qc_guard(request, project, required_statuses):
    """Shared entry checks for the three QC endpoints.

    Returns (assignment, attempt, error), where `error` is:
        None  -> refuse with 403. The caller has no QC authority here.
        ''    -> proceed.
        str   -> refuse with this message and a redirect. Authorised, wrong state.

    ORDER IS DELIBERATE: authority is decided FIRST, before anything about the site's
    state is revealed. A user with no QC authority gets an identical 403 whether the
    site is mid-review, already released, or has no design assignment at all — the
    refusal never doubles as a state oracle.

    The authority question is asked through user_can_qc_design(), which is where the
    self-QC exclusion lives, so a designer reviewing their own site is refused
    identically at all three endpoints — including when that designer is the Head or
    his named deputy.
    """
    assignment = getattr(project, 'design_assignment', None)

    # `assignment` may be None here; user_can_qc_design() returns False for that, which
    # is the correct answer — there is nothing to QC and nobody may QC it.
    if not user_can_qc_design(request.user, assignment):
        return None, None, None
    if assignment.status not in required_statuses:
        return assignment, None, (
            f'{project.project_id}: QC is not available at this stage '
            f'(status "{assignment.get_status_display()}").')
    return assignment, _current_attempt(assignment), ''


@login_required
def design_qc_start(request, project_id):
    """Head or deputy takes a completed package into review.

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
        return HttpResponseForbidden(
            'QC is for the Design Head or his named deputy, and never for the designer '
            'allocated to this site.')
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


@login_required
def design_qc_pass(request, project_id):
    """QC passes: the attempt closes, the site is released.

    Release sets `released_at` / `released_by` on the assignment and moves it to
    `released`. THAT IS ALL IT DOES (settled decision 9). It does not lock, group or
    hand over the BOQ — those are Part 6 and reading anything into `released` beyond
    "design is finished" would pre-empt decisions that have not been made.
    """
    project = _opex_site(project_id)
    assignment, attempt, error = _qc_guard(request, project, (DESIGN_IN_QC,))
    if error is None:
        return HttpResponseForbidden(
            'QC is for the Design Head or his named deputy, and never for the designer '
            'allocated to this site.')
    if request.method != 'POST':
        return redirect('design_qc_review', project_id=project.project_id)
    if error:
        messages.error(request, error)
        return redirect('design_qc_queue')

    # An unresolved change request means this package is already known to need rework.
    # Judging it anyway would record a verdict about a design nobody intends to build.
    open_crs = _open_change_requests(attempt)
    if open_crs.exists():
        messages.error(request, f'{project.project_id}: a PM change request on this '
                                f'attempt is still unresolved — it must be actioned '
                                f'before a QC verdict can be recorded.')
        return redirect('design_qc_review', project_id=project.project_id)

    profile = request.user.profile
    now = timezone.now()
    with transaction.atomic():
        attempt.qc_verdict     = QC_PASSED
        attempt.qc_reviewed_by = profile
        attempt.qc_reviewed_at = now
        attempt.closed_at      = now
        attempt.save(update_fields=['qc_verdict', 'qc_reviewed_by', 'qc_reviewed_at',
                                    'closed_at'])
        assignment.released_at = now
        assignment.released_by = profile
        assignment.status      = DESIGN_RELEASED
        assignment.save(update_fields=['released_at', 'released_by', 'status', 'updated_at'])
        log_activity(project, profile,
                     f'QC passed on attempt {attempt.attempt_number} — design released',
                     entity_type='DesignAttempt', entity_id=attempt.pk,
                     action_code='design_qc_passed')

    messages.success(request, f'{project.project_id}: QC passed — design released on '
                              f'attempt {attempt.attempt_number}.')
    return redirect('design_qc_review', project_id=project.project_id)


@login_required
def design_qc_fail(request, project_id):
    """QC fails: the attempt closes with remarks, and attempt N+1 opens for rework.

    `qc_remarks` is mandatory. Checked here so the reviewer gets a usable message, and
    enforced underneath by the Part 1 CHECK constraint
    `qc_remarks_required_when_qc_failed` for any writer that bypasses this view. A
    failure with no remarks is not a review — the designer has nothing to act on.

    The new attempt starts at `in_design` and the designer resubmits an Arka. Allocation,
    survey and the approved due date are all preserved (settled decision 2) — see
    _open_next_attempt().
    """
    project = _opex_site(project_id)
    assignment, attempt, error = _qc_guard(request, project, (DESIGN_IN_QC,))
    if error is None:
        return HttpResponseForbidden(
            'QC is for the Design Head or his named deputy, and never for the designer '
            'allocated to this site.')
    if request.method != 'POST':
        return redirect('design_qc_review', project_id=project.project_id)
    if error:
        messages.error(request, error)
        return redirect('design_qc_queue')

    if _open_change_requests(attempt).exists():
        messages.error(request, f'{project.project_id}: a PM change request on this '
                                f'attempt is still unresolved — it must be actioned '
                                f'before a QC verdict can be recorded.')
        return redirect('design_qc_review', project_id=project.project_id)

    remarks = (request.POST.get('qc_remarks') or '').strip()
    if not remarks:
        messages.error(request, f'{project.project_id}: QC remarks are required to fail '
                                f'a package — the designer cannot act on "failed" alone.')
        return redirect('design_qc_review', project_id=project.project_id)

    profile = request.user.profile
    now = timezone.now()
    with transaction.atomic():
        attempt.qc_verdict     = QC_FAILED
        attempt.qc_remarks     = remarks
        attempt.qc_reviewed_by = profile
        attempt.qc_reviewed_at = now
        attempt.save(update_fields=['qc_verdict', 'qc_remarks', 'qc_reviewed_by',
                                    'qc_reviewed_at'])
        # Status passes THROUGH qc_failed on its way back to in_design. Recorded as its
        # own log line so the failure is visible in the trail even though the stored
        # status moves straight on to the new attempt's in_design.
        assignment.status = DESIGN_QC_FAILED
        assignment.save(update_fields=['status', 'updated_at'])
        log_activity(project, profile,
                     f'QC failed on attempt {attempt.attempt_number}: {remarks}',
                     entity_type='DesignAttempt', entity_id=attempt.pk,
                     action_code='design_qc_failed')

        new_attempt = _open_next_attempt(
            assignment, ATTEMPT_REASON_QC_FAILED, profile,
            f'QC failure on attempt {attempt.attempt_number}')

    messages.success(request, f'{project.project_id}: QC failed — attempt '
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

    if assignment.status == DESIGN_RELEASED:
        return _back(f'{project.project_id}: the design is already released — a change '
                     f'now is a new scope of work, not a change request.')

    if attempt.qc_started_at is None:
        return _back(f'{project.project_id}: QC has not started on this package yet, so '
                     f'there is nothing settled to raise a change against. Talk to the '
                     f'Design Head — a change at this stage does not need a formal '
                     f'request.')

    if assignment.status not in CHANGE_REQUEST_STATUSES:
        return _back(f'{project.project_id}: a change request cannot be raised at this '
                     f'stage (status "{assignment.get_status_display()}").')

    profile = request.user.profile
    was_in_qc = assignment.status == DESIGN_IN_QC
    with transaction.atomic():
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
    return _back(msg, ok=True)


# ---------------------------------------------------------------------------
# 14. Part 4 screens
# ---------------------------------------------------------------------------

def _attempt_history(assignment):
    """Every attempt on an assignment, oldest first, with the change requests that
    closed each one. The two rework loops must be tellable apart at a glance, which is
    what `opened_reason` renders as on the screens."""
    return list(assignment.attempts
                .select_related('qc_reviewed_by__user', 'boq_submitted_by__user')
                .prefetch_related('change_requests__requested_by__user',
                                  'arka_submissions')
                .order_by('attempt_number'))


@login_required
def design_qc_queue(request):
    """Head / deputy: every site waiting for QC, plus everything currently in review.

    Deliberately NOT the Design Head dashboard — no metrics, no workload, no capacity,
    no overdue logic. It is a worklist of sites at `artifacts_uploaded` and `in_qc`, and
    nothing else; the dashboard is Part 5.
    """
    if not user_has_design_head_authority(request.user):
        return HttpResponseForbidden('Design Head or named deputy only.')

    assignments = (DesignAssignment.objects
                   .filter(status__in=(DESIGN_ARTIFACTS_UPLOADED, DESIGN_IN_QC),
                           project__is_deleted=False)
                   .select_related('project', 'project__program', 'assigned_to__user')
                   .order_by('status', 'project__project_id'))

    rows = []
    for assignment in assignments:
        attempt = _current_attempt(assignment)
        rows.append({
            'assignment':  assignment,
            'site':        assignment.project,
            'attempt':     attempt,
            'arka':        _current_arka(attempt),
            'in_qc':       assignment.status == DESIGN_IN_QC,
            'open_crs':    list(_open_change_requests(attempt)),
            # Self-QC is refused per site, so the button state has to be per row: the
            # deputy may QC most sites but not the ones allocated to them.
            'can_qc':      user_can_qc_design(request.user, assignment),
        })

    return render(request, 'projects/design/qc_queue.html', {
        'rows':      rows,
        'is_deputy': user_is_design_head_deputy(request.user) and not user_is_design_head(request.user),
    })


@login_required
def design_qc_review(request, project_id):
    """Head / deputy: the full package for one site — Arka link and capacity, CAD and
    BOQ files by signed URL, BOQ link, attempt history — with the QC actions."""
    project = _opex_site(project_id)
    assignment = getattr(project, 'design_assignment', None)
    if assignment is None:
        raise Http404('No design assignment for this site.')
    if not user_has_design_head_authority(request.user):
        return HttpResponseForbidden('Design Head or named deputy only.')

    ctx = _workspace_context(project, assignment)
    attempt = ctx['attempt']
    open_crs = list(_open_change_requests(attempt))
    can_qc = user_can_qc_design(request.user, assignment)
    ctx.update({
        'history':       _attempt_history(assignment),
        'open_crs':      open_crs,
        'can_qc':        can_qc,
        'can_start_qc':  can_qc and assignment.status == DESIGN_ARTIFACTS_UPLOADED
                         and _package_is_complete(attempt),
        'can_verdict':   can_qc and assignment.status == DESIGN_IN_QC and not open_crs,
        'is_self_qc':    user_is_assigned_designer(request.user, assignment),
        'is_deputy':     user_is_design_head_deputy(request.user) and not user_is_design_head(request.user),
        'released':      assignment.status == DESIGN_RELEASED,
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
    DESIGN_ARTIFACTS_UPLOADED:  ('none', '', 'Package complete — waiting for QC to start.'),
    DESIGN_IN_QC:               ('none', '', 'In QC review with the Design Head.'),
    DESIGN_RELEASED:            ('none', '', 'Design released. Nothing further to do.'),
    DESIGN_SURVEY_RETURNED:     ('none', '', 'Blocked — waiting for a replacement survey.'),
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
        current_due = next((c for c in commitments if c.is_current), None)

        # `arka_submitted` is the one status whose next step depends on the Arka verdict
        # rather than on the status alone, so it is resolved here instead of in the table.
        kind, label, waiting = _DESIGNER_ACTIONS.get(
            assignment.status, ('none', '', ''))
        if assignment.status == DESIGN_ARKA_SUBMITTED:
            if arka is not None and arka.verdict == ARKA_APPROVED:
                has_cad = bool(attempt and attempt.design_files.filter(
                    kind__in=CAD_KINDS, is_current=True).exists())
                if not has_cad:
                    kind, label, waiting = 'link', 'Upload CAD', ''
                elif attempt.boq_submitted_at is None:
                    kind, label, waiting = 'link', 'Enter BOQ', ''
                else:
                    kind, label, waiting = 'none', '', 'Package complete — waiting for QC.'
            else:
                kind, label, waiting = 'none', '', 'Waiting for the Design Head to approve your Arka.'

        # The QC remarks the designer has to act on: the most recent FAILED attempt.
        # Read off the attempts already loaded rather than re-querying per card.
        last_failed = None
        for a in sorted(assignment.attempts.all(), key=lambda a: a.attempt_number, reverse=True):
            if a.qc_verdict == QC_FAILED and a.qc_remarks:
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
        'awaiting_arka':       base.filter(status=DESIGN_ARKA_SUBMITTED,
                                           attempts__arka_submissions__is_current=True,
                                           attempts__arka_submissions__verdict=ARKA_PENDING
                                           ).distinct().count(),
        'awaiting_qc':         base.filter(status=DESIGN_ARTIFACTS_UPLOADED).count(),
        'in_qc':               base.filter(status=DESIGN_IN_QC).count(),
        'programs':            list(Program.objects.filter(is_deleted=False,
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
    window_open = bool(attempt and attempt.qc_started_at
                       and assignment.status in CHANGE_REQUEST_STATUSES
                       and assignment.status != DESIGN_RELEASED)

    return render(request, 'projects/design/change_request.html', {
        'project':     project,
        'assignment':  assignment,
        'attempt':     attempt,
        'history':     _attempt_history(assignment),
        'window_open': window_open,
        'released':    assignment.status == DESIGN_RELEASED,
        'requests':    list(DesignChangeRequest.objects
                            .filter(attempt__assignment=assignment)
                            .select_related('requested_by__user', 'attempt',
                                            'resulting_attempt')
                            .order_by('-requested_at')),
    })
