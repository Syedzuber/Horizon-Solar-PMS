"""
OPEX design workflow views — Part 2: survey upload, allocation, due-date handshake,
and the blocked flag.

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

from django.contrib import messages
from django.db import transaction
from django.http import Http404, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_date

from .decorators import login_required
from .design_storage import (
    DesignStorageError, build_design_path, get_design_file_url, upload_design_file,
)
from .models import (
    Program, Project, UserProfile, DesignAssignment, DueDateCommitment, log_activity,
    DESIGN_AWAITING_SURVEY, DESIGN_AWAITING_ALLOCATION, DESIGN_ALLOCATED,
    DESIGN_DUE_DATE_PROPOSED, DESIGN_IN_DESIGN, DESIGN_SURVEY_RETURNED,
)
from .permissions import (
    user_can_view_design, user_is_assigned_designer, user_is_design_head,
)

logger = logging.getLogger(__name__)

# Statuses at which the site has not yet started design work. Reallocation is allowed
# only while the assignment is still in one of these — see design_allocate().
REALLOCATABLE_STATUSES = (DESIGN_AWAITING_ALLOCATION, DESIGN_ALLOCATED,
                          DESIGN_DUE_DATE_PROPOSED)


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
    if not user_is_design_head(request.user):
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
    if not user_is_design_head(request.user):
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
    Raises ValueError with a user-facing message; callers own the transaction."""
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
    if not user_is_design_head(request.user):
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
    if not user_is_design_head(request.user):
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
    if not user_is_design_head(request.user):
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
    if not user_is_design_head(request.user):
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

    is_head     = user_is_design_head(request.user)
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
