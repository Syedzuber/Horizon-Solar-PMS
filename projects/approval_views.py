"""
Approvals S2a — the material pre-order screens: list, raise, read, decide, record on
someone's behalf, resubmit, withdraw, reassign.

A separate module for the reason order_views.py and purchases_views.py are: its own URL
group (approvals/). urls.py imports it beside them.

SCREENS ONLY. approvals.py (S1) is the only writer of every approval table. Each POST here
calls exactly ONE of its five entry points and writes nothing itself; approval_forms.py
reads the POST into that call's arguments. No notifications (Session 2b).

TWO KINDS OF "NO", AS IN payment_mark_paid:

  * WHO IS ASKING — a fact about the caller. Somebody who may not read the request, or
    who is not the person a step is for, or who is not SCM on an SCM action, gets a 403
    with decorators._forbidden()'s body, and nothing is called.
  * WHAT CHANGED WHILE THE PAGE WAS OPEN — the step was decided, the request withdrawn.
    That is the chokepoint's ApprovalRefused: a message and a return to the request,
    never a 500 and never a 403. A closed step stays readable to everyone who may read
    the request.

ATTACHMENTS: validate every file, upload every file, THEN call the chokepoint; on
ApprovalRefused or any other error remove what was uploaded. Built from the vendor-order
parts — views._validate_upload_file / _validate_and_upload, order_views._remove_uploaded,
supabase_storage.get_supabase_client — because order_views._upload_and_record_documents
writes VendorOrderDocument rows itself, and here the chokepoint writes ApprovalAttachment
rows inside its own transaction. The upload loop is therefore a copy (SECONDARY_FINDINGS,
Approvals S2a). Links are built the way vendor_order_document_url() builds them: public
URLs, no expiry (also SECONDARY_FINDINGS).
"""
import logging
import uuid as _uuid

from django.conf import settings
from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Prefetch
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from .approval_forms import (
    ATTACHMENT_LIMIT, assignee_choices, design_authority_choices, parse_assignee_overrides,
    parse_attachments, parse_client_uuid, parse_create, parse_new_assignee, parse_proxy,
    person_name, scope_choices, vendor_choices,
)
from .approvals import (
    ApprovalRefused, ProxyDecision, apply_approval_decision, create_approval_request,
    reassign_approval_step, resubmit_approval_request, withdraw_approval_request,
)
from .decorators import _forbidden, login_required
from .models import (
    ApprovalAttachment, ApprovalRequest, ApprovalStep, StatusTransition,
    APPROVAL_KIND_CHOICES, APPROVAL_KIND_MATERIAL_PRE_ORDER, APPROVAL_OPEN,
    APPROVAL_PARTY_CHOICES, APPROVAL_PARTY_DESIGN, APPROVAL_PROXY_CHANNEL_CHOICES,
    APPROVAL_STATUS_CHOICES, APPROVAL_STEP_APPROVED, APPROVAL_STEP_CHANGES_REQUESTED,
    APPROVAL_STEP_PENDING, APPROVAL_STEP_REJECTED, APPROVAL_STEP_SUPERSEDED,
    REASON_CREATED, REASON_RESUBMITTED, SUBJECT_APPROVAL_REQUEST,
)
from .order_views import _remove_uploaded
from .permissions import (
    approval_request_visibility_q, user_can_decide_approval_step,
    user_can_raise_approval_request, user_can_reassign_approval_step,
    user_can_record_proxy_decision, user_can_resubmit_approval_request,
    user_can_view_approval_list, user_can_view_approval_request,
    user_can_withdraw_approval_request, user_may_answer_approval_step,
)
from .supabase_storage import get_supabase_client, vendor_order_document_url
from .views import _validate_and_upload

logger = logging.getLogger(__name__)

#: Requests per page on the list.
PAGE_SIZE = 50

_PARTY_LABELS = dict(APPROVAL_PARTY_CHOICES)
_STATUS_LABELS = dict(APPROVAL_STATUS_CHOICES)
_KIND_LABELS = dict(APPROVAL_KIND_CHOICES)

# The decision buttons, in the order drawn. Plain English, never the stored value.
_DECISIONS = [
    (APPROVAL_STEP_APPROVED,          'Approve'),
    (APPROVAL_STEP_CHANGES_REQUESTED, 'Request changes'),
    (APPROVAL_STEP_REJECTED,          'Reject'),
]

# "Recorded by <name> from <channel>" — the channel as it reads in that sentence.
_CHANNEL_PHRASES = {
    'whatsapp':  'WhatsApp',
    'phone':     'a phone call',
    'email':     'email',
    'in_person': 'an in-person conversation',
}

_STEP_BADGES = {
    APPROVAL_STEP_PENDING:           'text-bg-warning',
    APPROVAL_STEP_APPROVED:          'text-bg-success',
    APPROVAL_STEP_CHANGES_REQUESTED: 'text-bg-info',
    APPROVAL_STEP_REJECTED:          'text-bg-danger',
    APPROVAL_STEP_SUPERSEDED:        'text-bg-secondary',
}

_STATUS_BADGES = {
    'open':              'text-bg-warning',
    'changes_requested': 'text-bg-info',
    'approved':          'text-bg-success',
    'rejected':          'text-bg-danger',
    'withdrawn':         'text-bg-secondary',
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _duration_text(delta):
    """A turnaround for people: '3 d 4 h', '2 h 15 min', 'under a minute'."""
    minutes = int(delta.total_seconds() // 60)
    if minutes < 1:
        return 'under a minute'
    days, rest = divmod(minutes, 60 * 24)
    hours, mins = divmod(rest, 60)
    parts = []
    if days:
        parts.append(f'{days} d')
    if hours:
        parts.append(f'{hours} h')
    if mins and not days:
        parts.append(f'{mins} min')
    return ' '.join(parts)


def step_turnaround(step):
    """decided_at − activated_at, FROM THE STEP ROW (spec: the step row is the
    accountability record). Never from the ledger: a ledger row's actor is the decider,
    its time the status move, and a proxy step or a mid-round approval has no status
    move of its own. None until the step is decided."""
    if step.decided_at is None or step.activated_at is None:
        return None
    return step.decided_at - step.activated_at


def _detail(approval_pk):
    return redirect('approval_detail', approval_pk=approval_pk)


def _request_for_read(approval_pk):
    """The request with everything the detail page and its predicates walk, prefetched."""
    step_rows = ApprovalStep.objects.select_related(
        'assignee__user', 'decided_by__user', 'recorded_by__user', 'superseded_by__user')
    return get_object_or_404(
        ApprovalRequest.objects.select_related(
            'raised_by__user', 'vendor', 'material_detail').prefetch_related(
            Prefetch('steps', queryset=step_rows),
            Prefetch('attachments',
                     queryset=ApprovalAttachment.objects.select_related('uploaded_by__user')),
            'programs', 'site_groups__program',
        ),
        pk=approval_pk)


def _step_for_action(step_pk):
    """A step and its request, for a POST. The chokepoint re-reads both under its lock;
    this read only decides who is asking."""
    step = get_object_or_404(ApprovalStep.objects.select_related('request'), pk=step_pk)
    return step, step.request


class _AttachmentUploadFailed(Exception):
    """Storage was unavailable or a file failed to upload. Anything stored was removed
    again; nothing was written. str(exc) is the message for the person."""


def _upload_attachments(files, folder):
    """Store `files` under approvals/<folder>/. Returns (stored, cleanup).

    `stored` is the list of dicts create_approval_request() and
    resubmit_approval_request() take as `attachments`; `cleanup()` removes every stored
    file again — the caller calls it when the chokepoint refuses or anything else fails.
    No files, no storage client: a request without attachments never touches Supabase.

    The loop is order_views._upload_and_record_documents' step 1, re-written against the
    same three parts (validator + storage call, cleanup, client), because that function
    also writes VendorOrderDocument rows (SECONDARY_FINDINGS, Approvals S2a).
    """
    if not files:
        return [], lambda: None
    bucket = settings.SUPABASE_BUCKET
    try:
        client = get_supabase_client()
    except Exception as exc:
        logger.error('Approval: storage unavailable — %s', exc)
        raise _AttachmentUploadFailed(
            'The upload service is unavailable. Nothing was saved; try again.')

    stored, paths = [], []
    try:
        for upload in files:
            path = f'approvals/{folder}/{_uuid.uuid4()}_{upload.name}'
            _validate_and_upload(upload, client, bucket, path)
            paths.append(path)
            stored.append({
                'file_name': upload.name, 'bucket': bucket, 'path': path,
                'file_type': (upload.content_type or '')[:100],
                'file_size_kb': max(1, upload.size // 1024),
            })
    except Exception as exc:
        logger.error('Approval: upload failed — %s', exc)
        _remove_uploaded(client, bucket, paths)
        raise _AttachmentUploadFailed(
            'An attachment could not be uploaded. Nothing was saved; try again.')
    return stored, lambda: _remove_uploaded(client, bucket, paths)


def _proxy_line(step):
    """'Recorded by <name> from <channel>' — or None for a step its decider typed."""
    if not step.is_proxy or step.recorded_by is None:
        return None
    channel = _CHANNEL_PHRASES.get(step.proxy_channel, step.get_proxy_channel_display())
    return f'Recorded by {person_name(step.recorded_by)} from {channel}'


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------

@login_required
def approval_list(request):
    """Every approval request the caller may read, newest first; filter by status and
    kind.

    Access: user_can_view_approval_list — SCM, PM, CEO, Admin, System Admin, Design Head
    authority. Rows: approval_request_visibility_q — all for SCM, CEO, Admin and System
    Admin; for everyone else the requests they raised, are or were named on, decided, or
    (Design Head authority) that carry a design step. The same rule as the detail page.
    """
    if not user_can_view_approval_list(request.user):
        return _forbidden(request)

    status = request.GET.get('status', '')
    kind = request.GET.get('kind', '')
    rows = (ApprovalRequest.objects.filter(approval_request_visibility_q(request.user))
            .distinct())
    if status in _STATUS_LABELS:
        rows = rows.filter(status=status)
    else:
        status = ''
    if kind in _KIND_LABELS:
        rows = rows.filter(kind=kind)
    else:
        kind = ''
    rows = (rows.select_related('raised_by__user', 'vendor')
            .prefetch_related(Prefetch('steps', queryset=ApprovalStep.objects
                                       .select_related('assignee__user')))
            .order_by('-raised_at', '-pk'))

    page = Paginator(rows, PAGE_SIZE).get_page(request.GET.get('page'))
    items = []
    for approval in page.object_list:
        waiting = [s for s in approval.steps.all()
                   if s.round == approval.current_round
                   and s.verdict == APPROVAL_STEP_PENDING and s.activated_at is not None]
        items.append({
            'approval':    approval,
            'badge':       _STATUS_BADGES.get(approval.status, 'text-bg-secondary'),
            'waiting_on':  [f'{_PARTY_LABELS[s.party]}: {person_name(s.assignee)}'
                            for s in waiting] if approval.status == APPROVAL_OPEN else [],
        })

    return render(request, 'projects/approvals/list.html', {
        'items':          items,
        'page':           page,
        'status':         status,
        'kind':           kind,
        'status_choices': APPROVAL_STATUS_CHOICES,
        'kind_choices':   APPROVAL_KIND_CHOICES,
        'can_raise':      user_can_raise_approval_request(request.user),
    })


# ---------------------------------------------------------------------------
# Raise
# ---------------------------------------------------------------------------

def _create_context(post=None, client_uuid=None, already=None):
    heads = assignee_choices(APPROVAL_PARTY_DESIGN)
    context = {
        'post':             post,
        'client_uuid':      client_uuid,
        # The request this form's key already raised, if any: the page then says so
        # and offers it, instead of a second Raise button.
        'already':          already,
        'vendors':          vendor_choices(),
        'pms':              assignee_choices('pm'),
        'heads':            heads,
        # Pre-selected only when there is exactly one to choose (spec T1).
        'default_head_pk':  heads[0].pk if len(heads) == 1 else None,
        'attachment_limit': ATTACHMENT_LIMIT,
        'picked': {
            'program':    set(post.getlist('program')) if post else set(),
            'project':    set(post.getlist('project')) if post else set(),
            'site_group': set(post.getlist('site_group')) if post else set(),
        },
    }
    context.update(scope_choices())
    return context


def _refuse_create(request, errors, client_uuid):
    for error in errors:
        messages.error(request, error)
    return render(request, 'projects/approvals/create.html',
                  _create_context(request.POST, client_uuid), status=400)


@login_required
def approval_create(request):
    """Raise a material approval before an order (material_pre_order — the only kind
    this session). GET draws the form; POST validates, uploads, and calls
    create_approval_request().

    Access: user_can_raise_approval_request — SCM.

    DOUBLE-SUBMIT SAFE ON client_uuid (R-14), INCLUDING THE BACK BUTTON. The key lives in
    the URL (?key=), not only in a hidden field: a GET without one redirects to a fresh
    one, so the key is part of the browser's history entry. Going Back after raising
    returns to the SAME key even when the browser fetches the page again — a key minted
    per GET was replaced on every re-fetch, which is how two requests got raised from one
    form (walkthrough, 27 Sep). A GET whose key has already raised a request says so and
    offers that request instead of a Raise button; a POST under it returns that request
    before anything is uploaded; and if two submits race past that read, the chokepoint
    returns the first one's request and the second's files — which it did not attach —
    are removed again.
    """
    if not user_can_raise_approval_request(request.user):
        return _forbidden(request)
    key = parse_client_uuid(request.GET, 'key')
    if request.method != 'POST':
        if key is None:
            return redirect(f"{reverse('approval_create')}?key={_uuid.uuid4()}")
        already = ApprovalRequest.objects.filter(client_uuid=key).first()
        return render(request, 'projects/approvals/create.html',
                      _create_context(client_uuid=key, already=already))

    cleaned, errors = parse_create(request)
    client_uuid = cleaned.pop('client_uuid') or key
    files = cleaned.pop('files')
    if client_uuid is not None:
        existing = ApprovalRequest.objects.filter(client_uuid=client_uuid).first()
        if existing is not None:
            messages.info(request, f'This form already raised request #{existing.pk} — '
                                   f'here it is. Nothing new was raised.')
            return _detail(existing.pk)
    else:
        client_uuid = _uuid.uuid4()   # a page without its key still gets one folder
    if errors:
        return _refuse_create(request, errors, client_uuid)

    try:
        stored, cleanup = _upload_attachments(files, client_uuid)
    except _AttachmentUploadFailed as exc:
        return _refuse_create(request, [str(exc)], client_uuid)

    try:
        approval = create_approval_request(
            kind=APPROVAL_KIND_MATERIAL_PRE_ORDER, raised_by=request.user.profile,
            attachments=stored, client_uuid=client_uuid, **cleaned)
    except ApprovalRefused as exc:
        cleanup()
        return _refuse_create(request, [str(exc)], client_uuid)
    except Exception:
        cleanup()
        # A racing twin wrote the same client_uuid between the read above and this
        # create: its request is the answer. Anything else is a real failure.
        approval = ApprovalRequest.objects.filter(client_uuid=client_uuid).first()
        if approval is None:
            raise

    if stored and not approval.attachments.filter(
            path__in=[item['path'] for item in stored]).exists():
        cleanup()   # the chokepoint returned an earlier submit's request

    messages.success(request, f'Approval request raised: {approval.title}.')
    return _detail(approval.pk)


# ---------------------------------------------------------------------------
# Detail
# ---------------------------------------------------------------------------

def _history(approval):
    """The request's ledger, oldest first, in words. What a resubmit or a withdrawal
    SAID lives here (the remark); what each step decided lives on the steps."""
    rows = (StatusTransition.objects
            .filter(subject_type=SUBJECT_APPROVAL_REQUEST, subject_id=approval.pk)
            .select_related('actor__user').order_by('occurred_at', 'pk'))
    history = []
    for row in rows:
        if row.reason_code == REASON_CREATED:
            what = 'Raised'
        elif row.reason_code == REASON_RESUBMITTED:
            what = 'Resubmitted'
        else:
            what = _STATUS_LABELS.get(row.to_status, row.to_status)
        history.append({'what': what, 'row': row,
                        'actor': person_name(row.actor) if row.actor else 'System'})
    return history


def _step_row(user, step, approval, deciders):
    """One step as the page draws it, with the action forms its predicates allow."""
    step.request = approval   # the predicates read step.request; no query
    turnaround = step_turnaround(step)
    row = {
        'step':         step,
        'party':        _PARTY_LABELS.get(step.party, step.party),
        'badge':        _STEP_BADGES.get(step.verdict, 'text-bg-secondary'),
        'turnaround':   _duration_text(turnaround) if turnaround is not None else None,
        'proxy_line':   _proxy_line(step),
        'can_decide':   user_can_decide_approval_step(user, step),
        'can_proxy':    user_can_record_proxy_decision(user, step),
        'can_reassign': user_can_reassign_approval_step(user, step),
    }
    if row['can_proxy']:
        # Whose decision it was. A PM step is decided by its assignee and nobody else;
        # a design step by anyone with Design Head authority.
        row['proxy_deciders'] = (deciders() if step.party == APPROVAL_PARTY_DESIGN
                                 else [step.assignee])
    if row['can_reassign']:
        row['reassign_choices'] = [p for p in assignee_choices(step.party)
                                   if p.pk != step.assignee_id]
    return row


@login_required
def approval_detail(request, approval_pk):
    """One request: its details, its attachments by round, and every round's steps —
    verdict, note, who decided, who typed it, the proxy channel and evidence, when it
    was asked, when it was answered, and the turnaround from the step row.

    Access: user_can_view_approval_request — SCM, CEO, Admin, System Admin; the raiser;
    anyone named on or deciding any step; Design Head authority where there is a design
    step. A CLOSED step is shown read-only to all of them, never a 403. Each action form
    is drawn only where its own predicate passes, and its view asks again on POST.
    """
    approval = _request_for_read(approval_pk)
    user = request.user
    if not user_can_view_approval_request(user, approval):
        return _forbidden(request)

    cache = {}

    def deciders():
        if 'design' not in cache:
            cache['design'] = design_authority_choices()
        return cache['design']

    steps = list(approval.steps.all())
    attachments = list(approval.attachments.all())
    rounds = []
    for round_no in range(approval.current_round, 0, -1):     # newest first
        rounds.append({
            'number':      round_no,
            'is_current':  round_no == approval.current_round,
            'steps':       [_step_row(user, s, approval, deciders)
                            for s in steps if s.round == round_no],
            'attachments': [{'file': a, 'url': vendor_order_document_url(a)}
                            for a in attachments if a.round == round_no],
        })

    # A contractor bill has none; the reverse accessor's DoesNotExist is an AttributeError.
    material = getattr(approval, 'material_detail', None)

    return render(request, 'projects/approvals/detail.html', {
        'approval':       approval,
        'status_badge':   _STATUS_BADGES.get(approval.status, 'text-bg-secondary'),
        'material':       material,
        'boq_items':      list(material.boq_items.all()) if material else [],
        'programs':       list(approval.programs.all()),
        # Scope is for record; a soft-deleted site is not drawn (models.ApprovalRequest).
        'projects':       list(approval.projects.filter(is_deleted=False)
                               .only('pk', 'project_id', 'customer_name')),
        'site_groups':    list(approval.site_groups.all()),
        'rounds':         rounds,
        'history':        _history(approval),
        'decisions':      _DECISIONS,
        'channels':       APPROVAL_PROXY_CHANNEL_CHOICES,
        'can_withdraw':   user_can_withdraw_approval_request(user, approval),
        'can_resubmit':   user_can_resubmit_approval_request(user, approval),
    })


# ---------------------------------------------------------------------------
# Actions — each calls one approvals.py entry point
# ---------------------------------------------------------------------------

@login_required
def approval_decide(request, step_pk):
    """POST: approve, request changes, or reject one step, as its decider.

    Access: user_can_view_approval_request AND user_may_answer_approval_step — the step's
    assignee, or Design Head authority on a design step. Anyone else: 403. The raiser is
    never the assignee of a PM step; a raiser who holds Design Head authority is refused
    by the chokepoint's same-person rule, as a message. A stale page (the step already
    closed) is the chokepoint's refusal, as a message. Calls apply_approval_decision().
    """
    step, approval = _step_for_action(step_pk)
    if not (user_can_view_approval_request(request.user, approval)
            and user_may_answer_approval_step(request.user, step)):
        return _forbidden(request)
    if request.method != 'POST':
        return _detail(approval.pk)

    verdict = request.POST.get('verdict', '')
    try:
        apply_approval_decision(step, verdict, request.user.profile,
                                note=request.POST.get('note', ''))
    except ApprovalRefused as exc:
        messages.error(request, str(exc))
        return _detail(approval.pk)
    label = dict(_DECISIONS).get(verdict, verdict)
    messages.success(request, f'Recorded: {label.lower()} for the '
                              f'{_PARTY_LABELS.get(step.party, step.party)} step.')
    return _detail(approval.pk)


@login_required
def approval_record_proxy(request, step_pk):
    """POST: SCM records a decision made outside PMS on the decider's behalf — who
    decided, over which channel, and what was said as evidence.

    Access: user_can_view_approval_request AND user_can_raise_approval_request — SCM.
    Anyone else: 403. Whether the step is still open and whether the named person may
    decide it is the chokepoint's. Calls apply_approval_decision(proxy=...).
    """
    step, approval = _step_for_action(step_pk)
    if not (user_can_view_approval_request(request.user, approval)
            and user_can_raise_approval_request(request.user)):
        return _forbidden(request)
    if request.method != 'POST':
        return _detail(approval.pk)

    decided_by, channel, evidence = parse_proxy(request.POST)
    verdict = request.POST.get('verdict', '')
    try:
        apply_approval_decision(step, verdict, request.user.profile,
                                note=request.POST.get('note', ''),
                                proxy=ProxyDecision(decided_by, channel, evidence))
    except ApprovalRefused as exc:
        messages.error(request, str(exc))
        return _detail(approval.pk)
    messages.success(request, f'Recorded {person_name(decided_by)}\'s decision on the '
                              f'{_PARTY_LABELS.get(step.party, step.party)} step.')
    return _detail(approval.pk)


@login_required
def approval_resubmit(request, approval_pk):
    """GET: the resubmit form. POST: open the next round with a note saying what
    changed, any new attachments, and optionally a different approver for a party.

    The request's details are NOT edited — resubmit_approval_request() takes none — and
    the form says so above the note.

    Access: user_can_view_approval_request AND user_can_raise_approval_request — SCM.
    Anyone else: 403. A request no longer waiting for changes: a message and back to the
    request. Calls resubmit_approval_request().
    """
    approval = _request_for_read(approval_pk)
    if not (user_can_view_approval_request(request.user, approval)
            and user_can_raise_approval_request(request.user)):
        return _forbidden(request)
    if not user_can_resubmit_approval_request(request.user, approval):
        messages.error(request, 'This request is not waiting for changes, so there is '
                                'nothing to resubmit.')
        return _detail(approval.pk)

    # Who holds each party now: the last row per party in the round that asked for
    # changes — the same rule resubmit_approval_request() applies.
    holders = {}
    for step in sorted((s for s in approval.steps.all()
                        if s.round == approval.current_round), key=lambda s: s.pk):
        holders[step.party] = step.assignee
    parties = [{'party': party, 'label': _PARTY_LABELS.get(party, party),
                'holder': holder, 'choices': assignee_choices(party)}
               for party, holder in holders.items()]

    def form(status=200):
        return render(request, 'projects/approvals/resubmit.html', {
            'approval': approval, 'parties': parties, 'post': request.POST or None,
            'attachment_limit': ATTACHMENT_LIMIT,
        }, status=status)

    if request.method != 'POST':
        return form()

    errors = []
    files = parse_attachments(request, errors)
    overrides = parse_assignee_overrides(request.POST, holders, errors)
    if errors:
        for error in errors:
            messages.error(request, error)
        return form(status=400)

    try:
        stored, cleanup = _upload_attachments(
            files, f'{approval.pk}/round-{approval.current_round + 1}')
    except _AttachmentUploadFailed as exc:
        messages.error(request, str(exc))
        return form(status=400)

    try:
        approval = resubmit_approval_request(approval, request.user.profile,
                                             request.POST.get('note', ''),
                                             attachments=stored,
                                             assignees=overrides or None)
    except ApprovalRefused as exc:
        cleanup()
        messages.error(request, str(exc))
        return _detail(approval.pk)
    except Exception:
        cleanup()
        raise
    messages.success(request, f'Resubmitted as round {approval.current_round}.')
    return _detail(approval.pk)


@login_required
def approval_withdraw(request, approval_pk):
    """POST: SCM withdraws the request, with a note.

    Access: user_can_view_approval_request AND user_can_raise_approval_request — SCM.
    Anyone else: 403. A request already closed: the chokepoint's refusal, as a message.
    Calls withdraw_approval_request().
    """
    approval = get_object_or_404(ApprovalRequest.objects.prefetch_related('steps'),
                                 pk=approval_pk)
    if not (user_can_view_approval_request(request.user, approval)
            and user_can_raise_approval_request(request.user)):
        return _forbidden(request)
    if request.method != 'POST':
        return _detail(approval.pk)
    try:
        withdraw_approval_request(approval, request.user.profile,
                                  request.POST.get('note', ''))
    except ApprovalRefused as exc:
        messages.error(request, str(exc))
        return _detail(approval.pk)
    messages.success(request, 'Request withdrawn.')
    return _detail(approval.pk)


@login_required
def approval_reassign(request, step_pk):
    """POST: SCM hands a pending step to someone else, with a note.

    Access: user_can_view_approval_request AND user_can_raise_approval_request — SCM.
    Anyone else: 403. A step no longer pending, an ineligible person, or the same person
    again: the chokepoint's refusal, as a message. Calls reassign_approval_step().
    """
    step, approval = _step_for_action(step_pk)
    if not (user_can_view_approval_request(request.user, approval)
            and user_can_raise_approval_request(request.user)):
        return _forbidden(request)
    if request.method != 'POST':
        return _detail(approval.pk)
    new_assignee = parse_new_assignee(request.POST)
    try:
        replacement = reassign_approval_step(step, new_assignee, request.user.profile,
                                             request.POST.get('note', ''))
    except ApprovalRefused as exc:
        messages.error(request, str(exc))
        return _detail(approval.pk)
    messages.success(request, f'The {_PARTY_LABELS.get(step.party, step.party)} step is '
                              f'now with {person_name(replacement.assignee)}.')
    return _detail(approval.pk)
