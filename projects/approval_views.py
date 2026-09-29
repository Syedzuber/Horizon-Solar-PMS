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

APPROVALS 2a-2 — THE REVISION SCREENS (27 Sep 2026) put S1.1 on the page:

  * the resubmit form edits the request's details (D-A18) and sends only what changed,
    and offers to keep a party's approval from the previous round, with a reason (D-A20);
  * each round shows the details its approvers saw, from round_snapshot() (D-A19), and
    from round 2 what changed since the round before;
  * a carried step reads as kept, never as a decision, and is not timed;
  * a proxy decision may carry evidence files (D-A21), listed under its step;
  * History names who did every action and when (D-A22), merged from three sources —
    the ledger, the decided step rows, and the superseded rows a reassignment leaves
    (it writes no ledger row). See _history().

APPROVALS 2c adds approval_aging, the read-only aging list for SCM, CEO, Admin and
System Admin. Its figures, and the dashboards' pending-approvals cards, come from
approval_queries.py.

APPROVALS 3a adds the pre-dispatch kind to the raise page (?kind=, one of the two
material kinds), a "Dispatch against" block on the detail page (the PO/PI record and any
linked pre-order approval, per round), and a read-only vendor on its resubmit form. A
VendorOrder is "PO/PI record" in every label.

APPROVALS 3b adds "PO/PI records covered" to an approved pre-order request: the records
it is linked to, a link form and a remove-with-reason action for SCM
(approval_link_order, approval_unlink_order — one approvals.py entry point each), and
the removed links, struck through, with who, when and why. Linking is optional. History
draws each link and each removal from the link rows (_history).

MATERIAL LINES (28 Sep 2026, D-A28) replace the request-wide make, specification and
quantity on the raise and resubmit forms (a lines table) and on the detail page. Every
round's material is drawn through material_display(), which reads snapshot schema 1 and
2 alike; a request's old make, specification and quantity, when it has them, are drawn
under "Recorded before line items". round_changes follows each line by id and names
each changed field.

CONTRACTOR BILLS 4a-2 (29 Sep 2026) put the 4a-1 bill on the page:

  * the raise page (?kind=contractor_bill) is two steps: choose the site, then the page
    reloads with ?project= in its URL and asks for the rest (_bill_create). The order of
    a POST is fixed: parse -> refusals (the bills bucket, the form, then
    approvals._clean_bill with a placeholder PDF, which writes nothing) -> warnings
    (bill_rules, a re-render with "Raise anyway", never a block) -> upload the PDF
    (private bucket) -> upload the photos (public bucket, jpg/png) -> the chokepoint. NO
    FILE IS STORED BEFORE THE WARNING CHECK. Anything stored for a bill that was then not
    written is removed again (bill_storage.discard_unrecorded_bill_pdf, _remove_uploaded);
  * the detail page draws the bill (_bill_now) — the amount beside a SIGNED, expiring
    link to the PDF (bill_storage.bill_pdf_url, never a stored or public URL), the tasks
    with their status now, and, while the bill is open or waiting for changes, the
    warnings — and each round's bill from its schema-3 snapshot (bill_display);
  * a bill's resubmit carries the note, photos, approver changes and kept approvals
    only. Any posted revision is IGNORED for a bill: the chokepoint does not yet refuse a
    scope or vendor change on one (SECONDARY_FINDINGS, 4a-2; 4b adds bill revision).

CONTRACTOR BILLS 4a-3 (D-A53, D-A54): the Site Engineer confirms the WORK, never the bill.
A viewer whose only standing is being the bill's Site Engineer (sees_bill) reads the site,
contractor, tasks, description, photos and task warnings — never the amount, bill number,
date or PDF. Their step is answered with "Confirm work done" (site photos required) or
"Work not done" (a note), in approval_decide; the rules are the chokepoint's.
"""
import logging
import uuid as _uuid
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Prefetch
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from .approval_forms import (
    ATTACHMENT_LIMIT, BILL_PHOTO_EXTENSIONS, EVIDENCE_EXTENSIONS, KEEP_REASON_MIN,
    assignee_choices, bill_site, bill_site_choices, bill_task_choices, contractor_choices,
    current_scope_pks, design_authority_choices, keepable_steps, line_rows,
    parse_assignee_overrides, parse_attachments, parse_bill_create, parse_bill_photos,
    parse_carry, parse_client_uuid, parse_site_photos,
    parse_create, parse_evidence_files, parse_linked_order, parse_lines, parse_new_assignee,
    parse_proxy, parse_revision, person_name, order_link_choices, po_pi_record_choices,
    po_pi_record_label, pre_order_choices, scope_choices, vendor_choices,
)
from .approval_queries import AGING_WINDOW_DAYS, aging_rows
# _clean_bill is the chokepoint's own bill validator, private by name. The raise page asks
# it BEFORE the PDF is uploaded (ruling 2, 4a-2), with a placeholder PDF, so a typo in the
# bill number is refused in the chokepoint's words without a 20 MB upload first. It only
# reads (the vendor, the site, the tasks, bills_bucket()) and writes nothing; the real
# create_approval_request() asks it again, under its own rules, after the upload.
from .approvals import (
    ApprovalRefused, ContractorBill, ProxyDecision, _clean_bill, apply_approval_decision,
    create_approval_request,
    link_order_to_approval, reassign_approval_step, resubmit_approval_request,
    round_snapshot, unlink_order_from_approval, withdraw_approval_request,
)
from .bill_rules import (
    bill_warnings, format_bill_amount, incomplete_task_warnings, site_engineer_choices,
    task_status_label,
)
from .bill_storage import (
    BILL_STORAGE_NOT_READY, BILL_STORAGE_OFF, BillStorageError, bill_pdf_url, bills_bucket,
    discard_unrecorded_bill_pdf, upload_bill_pdf,
)
from .decorators import _forbidden, login_required
from .models import (
    ApprovalAttachment, ApprovalOrderLink, ApprovalRequest, ApprovalStep,
    ContractorBillDetail, StatusTransition, VendorOrder,
    APPROVAL_APPROVED, APPROVAL_CHANGES_REQUESTED, APPROVAL_REJECTED,
    APPROVAL_KIND_CHOICES, APPROVAL_KIND_CONTRACTOR_BILL, APPROVAL_KIND_MATERIAL_PRE_DISPATCH,
    APPROVAL_KIND_MATERIAL_PRE_ORDER, APPROVAL_OPEN,
    APPROVAL_PARTY_PM, APPROVAL_PARTY_SITE_ENGINEER,
    APPROVAL_PARTY_CHOICES, APPROVAL_PARTY_DESIGN, APPROVAL_PROXY_CHANNEL_CHOICES,
    APPROVAL_STATUS_CHOICES, APPROVAL_STEP_APPROVED, APPROVAL_STEP_CHANGES_REQUESTED,
    APPROVAL_STEP_PENDING, APPROVAL_STEP_REJECTED, APPROVAL_STEP_SUPERSEDED,
    REASON_CREATED, REASON_RESUBMITTED, SUBJECT_APPROVAL_REQUEST,
)
from .order_views import _remove_uploaded
from .permissions import (
    APPROVAL_PORTFOLIO_ROLES, user_has_design_head_authority,
    approval_request_visibility_q, user_can_decide_approval_step,
    user_can_link_approval_order, user_can_raise_approval_request,
    user_can_reassign_approval_step, user_can_record_proxy_decision,
    user_can_resubmit_approval_request,
    user_can_view_approval_aging, user_can_view_approval_list,
    user_can_view_approval_request, user_can_view_vendor_order,
    user_can_withdraw_approval_request, user_may_answer_approval_step,
)
from .submission_guard import keyed_redirect
from .supabase_storage import get_supabase_client, vendor_order_document_url
from .units import UNIT_COUNT, UNIT_LABELS, UNITS, format_quantity
from .views import _validate_and_upload

logger = logging.getLogger(__name__)

#: Requests per page on the list.
PAGE_SIZE = 50

_PARTY_LABELS = dict(APPROVAL_PARTY_CHOICES)
_STATUS_LABELS = dict(APPROVAL_STATUS_CHOICES)
_KIND_LABELS = dict(APPROVAL_KIND_CHOICES)

#: The kinds the raise page takes as ?kind= (Approvals 3a; the contractor bill since
#: 4a-2, drawn by _bill_create). No ?kind= at all is a pre-order request, so every link
#: and bookmark from before 3a still works.
_RAISE_KINDS = (APPROVAL_KIND_MATERIAL_PRE_ORDER, APPROVAL_KIND_MATERIAL_PRE_DISPATCH,
                APPROVAL_KIND_CONTRACTOR_BILL)

#: B-16, on every bill: what an approval of a bill does and does not mean.
BILL_APPROVAL_NOTE = ('Approving confirms the work was done. Nobody has checked this amount '
                      'against a rate or work order.')

#: The bill statuses whose detail page shows the warnings (ruling 7, 4a-2): while someone
#: may still act on them. On a closed bill they would only be noise.
_BILL_WARNING_STATUSES = (APPROVAL_OPEN, APPROVAL_CHANGES_REQUESTED)

#: What a viewer who is only the bill's Site Engineer reads in place of B-16's note
#: (D-A53, 4a-3).
SITE_ENGINEER_WORK_NOTE = ('You are confirming the work on site. You are not approving '
                           'the bill or its amount.')

#: A Site Engineer step's verdicts as the page names them (D-A53): the Site Engineer
#: confirms the work or says it is not done; "approved" would read as approving the bill.
_SITE_ENGINEER_VERDICTS = {
    APPROVAL_STEP_APPROVED:          'Work confirmed',
    APPROVAL_STEP_CHANGES_REQUESTED: 'Work not done',
}

#: The Site Engineer's two outcomes, for the SCM proxy form on that step. No reject.
_SITE_ENGINEER_DECISIONS = [
    (APPROVAL_STEP_APPROVED,          'Confirm work done'),
    (APPROVAL_STEP_CHANGES_REQUESTED, 'Work not done'),
]


def sees_bill(user, approval, steps):
    """True when `user` may see a contractor bill's amount, bill number, bill date and
    PDF (D-A53, 4a-3): every reader of the request EXCEPT one whose ONLY standing on it is
    being its Site Engineer. The Site Engineer confirms the work, never the bill.

    Read as permissions.user_can_view_approval_request with the Site Engineer steps left
    out: SCM, CEO, Admin and System Admin; the raiser; anyone who is or was the assignee,
    or the decider, of any other party's step in any round; Design Head authority where
    there is a design step (never on a bill — kept so the two rules read alike). A past
    Site Engineer who is now a PM on the bill therefore sees it all. `steps` are the
    request's steps already in memory, so no query.

    Lives here, not in permissions.py, by ruling (Q6, 4a-3); it belongs there
    (SECONDARY_FINDINGS, 4a-3)."""
    profile = getattr(user, 'profile', None)
    if profile is None:
        return False
    # SCM, CEO, Admin and System Admin read every request in full; so does whoever raised it.
    if profile.role in APPROVAL_PORTFOLIO_ROLES or approval.raised_by_id == profile.pk:
        return True
    others = [s for s in steps if s.party != APPROVAL_PARTY_SITE_ENGINEER]
    if any(s.assignee_id == profile.pk or s.decided_by_id == profile.pk for s in others):
        return True
    return (any(s.party == APPROVAL_PARTY_DESIGN for s in others)
            and user_has_design_head_authority(user))

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


def _channel_phrase(step):
    return _CHANNEL_PHRASES.get(step.proxy_channel, step.get_proxy_channel_display())


def _proxy_line(step):
    """'Recorded by <name> from <channel>' — or None for a step its decider typed."""
    if not step.is_proxy or step.recorded_by is None:
        return None
    return f'Recorded by {person_name(step.recorded_by)} from {_channel_phrase(step)}'


def _origin_step(step, by_pk):
    """The step whose decision a carried `step` keeps, followed back through every carry
    (a kept approval can be kept again). Walks rows already in memory (`by_pk`: every step
    of the request), so no query per hop. approvals._origin_round answers the same
    question for the ledger but returns only the round, reading each hop from the
    database; the page also needs who decided, when, and whether it was a proxy."""
    while step.carried_from_id is not None and step.carried_from_id in by_pk:
        step = by_pk[step.carried_from_id]
    return step


def _kept(step, by_pk):
    """A carried step as the page states it (D-A20), or None for any other step: the
    round its approval was decided in, by whom and when; who kept it, when and why; and,
    when that original decision was recorded on the decider's behalf, by whom and from
    where. A carried step's own decided_at is when it was KEPT, not decided."""
    if step.carried_from_id is None:
        return None
    origin = _origin_step(step, by_pk)
    originally = None
    if origin.is_proxy and origin.recorded_by is not None:
        originally = (f'originally recorded by {person_name(origin.recorded_by)} '
                      f'from {_channel_phrase(origin)}')
    return {
        'round':      origin.round,
        'decider':    person_name(origin.decided_by),
        'decided_at': origin.decided_at,
        'kept_by':    person_name(step.recorded_by) if step.recorded_by else 'System',
        'kept_at':    step.decided_at,
        'reason':     step.carry_reason,
        'originally': originally,
    }


def _current_details(approval, material, lines, programs, projects, site_groups):
    """The request as it stands NOW, in round_snapshot()'s shape (schema 2), for a round
    that has no snapshot (raised before S1.1). The page draws it under a label saying so
    — it is not what that round's approvers saw if anything was revised since. `lines`
    are the material's live MaterialApprovalLine rows, in position order."""
    return {
        'title':                   approval.title,
        'description':             approval.description,
        'design_signoff_required': approval.design_signoff_required,
        'vendor': ({'id': approval.vendor.pk, 'name': approval.vendor.name}
                   if approval.vendor is not None else None),
        'programs':    [{'id': p.pk, 'name': p.name} for p in programs],
        'projects':    [{'id': p.pk, 'project_id': p.project_id,
                         'customer_name': p.customer_name, 'is_deleted': p.is_deleted}
                        for p in projects],
        'site_groups': [{'id': g.pk, 'name': g.name,
                         'program_name': g.program.name if g.program_id else None}
                        for g in site_groups],
        'material': None if material is None else {
            'lines': [{'id': line.pk, 'position': line.position,
                       'description': line.description, 'make': line.make,
                       'specification': line.specification,
                       'quantity': format_quantity(line.quantity, line.unit),
                       'unit': line.unit} for line in lines],
            'legacy': {key: getattr(material, key) for key in _LEGACY_MATERIAL_TEXT
                       if getattr(material, key)},
        },
    }


#: The request-wide material fields that lines replaced, with their labels — shown
#: under "Recorded before line items" when a request still carries one.
_LEGACY_MATERIAL_LABELS = (('proposed_make', 'Proposed make'),
                           ('specification', 'Specification'),
                           ('quantity_note', 'Quantity'))
_LEGACY_MATERIAL_TEXT = tuple(key for key, _ in _LEGACY_MATERIAL_LABELS)


def material_display(material):
    """A round's material — snapshot schema 1 or 2, or _current_details' — in the one
    shape the pages draw and round_changes compares. None stays None.

      lines   [{id, position, description, make, specification, quantity, unit,
                unit_label}] in position order; [] in schema 1, which had none
      legacy  [{key, label, value}] for each non-empty request-wide field lines
              replaced: schema 1 holds them at the top of `material`, schema 2 under
              `legacy` and only when non-empty
      boq_items  schema 1's BOQ-item picks ([] in every snapshot ever written; kept so
              one that is not still draws)

    Built from the dict alone, never a live row: a snapshot says what that round saw."""
    if material is None:
        return None
    legacy_source = material.get('legacy') or material
    lines = sorted(material.get('lines') or (), key=lambda line: line.get('position', 0))
    return {
        'lines': [dict(line, unit_label=UNIT_LABELS.get(line.get('unit'), line.get('unit')))
                  for line in lines],
        'legacy': [{'key': key, 'label': label, 'value': legacy_source.get(key)}
                   for key, label in _LEGACY_MATERIAL_LABELS if legacy_source.get(key)],
        'boq_items': list(material.get('boq_items') or ()),
    }


def _for_display(details):
    """`details` (a snapshot or _current_details) with its material normalised by
    material_display, for _round_details.html. A copy: the snapshot dict itself is left
    as stored, because round_changes reads it too."""
    if details is None:
        return None
    return dict(details, material=material_display(details.get('material')))


def _dispatch_against(user, approval, material, snapshots):
    """The "Dispatch against" block of a pre-dispatch request (Approvals 3a), for the
    request as it stands and for each round. Returns (current, {round: block}); (None, {})
    for any other kind. A round with a snapshot is drawn from it; one without gets the
    current block, as its other details do.

    PO and PI numbers and the vendor come from the snapshot — what that round saw. The
    RECORDED date comes from the live record, because the snapshot schema does not hold
    it and a VendorOrder has no edit path, so its created_at never changes.

    WHO SEES WHAT: numbers, vendor and date go to anyone who may read the request. The
    link to the record page only where user_can_view_vendor_order() passes; the pre-order
    approval's title always, its link only where user_can_view_approval_request() passes
    for the pre-order itself — so no link leads to a 403 (SECONDARY_FINDINGS, 3a).

    Two queries for the rows, two for the order's sites and their projects, one for the
    pre-orders' steps, whatever the number of rounds."""
    if approval.kind != APPROVAL_KIND_MATERIAL_PRE_DISPATCH or material is None:
        return None, {}
    seen = [(snapshot or {}).get('material') or {} for snapshot in snapshots.values()]
    order_ids = {material.vendor_order_id} | {
        (m.get('vendor_order') or {}).get('id') for m in seen}
    pre_ids = {material.pre_order_request_id} | {
        (m.get('pre_order_request') or {}).get('id') for m in seen}
    orders = {o.pk: o for o in VendorOrder.objects.filter(pk__in=order_ids - {None})
              .prefetch_related('sites__project')}
    pre_orders = {p.pk: p for p in ApprovalRequest.objects.filter(pk__in=pre_ids - {None})
                  .prefetch_related('steps')}
    may_open_order = {pk: user_can_view_vendor_order(user, o) for pk, o in orders.items()}
    may_open_pre = {pk: user_can_view_approval_request(user, p)
                    for pk, p in pre_orders.items()}

    def block(vendor, order_ref, pre_ref):
        if order_ref is None:
            return None
        live = orders.get(order_ref['id'])
        return {
            'vendor_name': (vendor or {}).get('name') or '—',
            'po_number':   order_ref.get('po_number') or '—',
            'pi_number':   order_ref.get('pi_number') or '—',
            'recorded_at': live.created_at if live is not None else None,
            'order_url':   (reverse('vendor_order_detail', args=[live.pk])
                            if live is not None and may_open_order[live.pk] else None),
            'pre_order':   None if pre_ref is None else {
                'title': pre_ref['title'],
                'url':   (reverse('approval_detail', args=[pre_ref['id']])
                          if may_open_pre.get(pre_ref['id']) else None),
            },
        }

    order = orders.get(material.vendor_order_id)
    pre_order = pre_orders.get(material.pre_order_request_id)
    current = block(
        {'name': approval.vendor.name} if approval.vendor is not None else None,
        ({'id': order.pk, 'po_number': order.po_number, 'pi_number': order.pi_number}
         if order is not None else None),
        {'id': pre_order.pk, 'title': pre_order.title} if pre_order is not None else None)
    by_round = {}
    for round_no, snapshot in snapshots.items():
        if snapshot is None:
            by_round[round_no] = current
            continue
        seen_material = snapshot.get('material') or {}
        by_round[round_no] = block(snapshot.get('vendor'),
                                   seen_material.get('vendor_order'),
                                   seen_material.get('pre_order_request'))
    return current, by_round


def _pdf_signer():
    """A signer for bill PDFs that mints each distinct file's link ONCE per page render.

    Every signing is a network call to storage, and until 4b every round of a bill
    records the same PDF, so the current block and each round would otherwise sign the
    same file again. The link itself only ever comes from bill_storage.bill_pdf_url():
    signed, expiring, None when storage is off or signing fails."""
    cache = {}

    def sign(bucket, path):
        if (bucket, path) not in cache:
            cache[(bucket, path)] = bill_pdf_url(bucket, path)
        return cache[(bucket, path)]
    return sign


def _bill_task_label(task_name, location_label):
    """A task as a bill names it: "Module Installation — Block A" for a per-location copy."""
    return f'{task_name} — {location_label}' if location_label else task_name


def _work_only(block):
    """`block` (a _bill_block.html dict) as a viewer who is only the bill's Site Engineer
    reads it (D-A53): the site, the contractor and the tasks, never the amount, the bill
    number, the bill date or the PDF. The keys stay, empty, so no template can draw them by
    accident; `work_only` tells the block to leave their rows out."""
    return dict(block, bill_number=None, bill_date=None, amount=None, pdf_name=None,
                pdf_size_kb=None, pdf_url=None, work_only=True)


def bill_display(bill, contractor_name, sign, full=True):
    """A round's bill — a schema-3 snapshot's `bill` block — in the shape _bill_block.html
    draws, or None when the round has none (every material round). Built from the dict
    alone, never a live row: a snapshot says what that round saw. The PDF is the one that
    ROUND recorded, signed through `sign` (_pdf_signer). No status and no warnings: those
    are about now, and are drawn on the request's own bill block (_bill_now).

    `full` False (sees_bill, 4a-3): the work-only block, and the PDF is not even signed."""
    if not bill:
        return None
    project, pdf = bill.get('project') or {}, bill.get('pdf') or {}
    try:
        bill_date = date.fromisoformat(bill.get('bill_date') or '')
    except ValueError:
        bill_date = None
    block = {
        'contractor':  contractor_name or '—',
        'site':        f"{project.get('project_id', '—')} — {project.get('customer_name', '')}",
        'bill_number': bill.get('bill_number') or '—',
        'bill_date':   bill_date,
        'amount':      format_bill_amount(bill.get('amount')),
        'pdf_name':    pdf.get('file_name') or 'Bill PDF',
        'pdf_size_kb': pdf.get('size_kb'),
        'pdf_url':     None,
        'tasks':       [{'label': _bill_task_label(t.get('task_name'), t.get('location_label')),
                         'phase': t.get('phase_name'), 'status': None}
                        for t in bill.get('tasks') or ()],
        'warnings':    [],
        'work_only':   False,
    }
    if not full:
        return _work_only(block)
    block['pdf_url'] = sign(pdf.get('bucket'), pdf.get('path'))
    return block


def _current_assignees(steps, round_no):
    """{party: profile} — who holds each party in `round_no` now: the last live
    (not superseded) row per party, the rows being in memory already."""
    held = {}
    for step in sorted((s for s in steps if s.round == round_no), key=lambda s: s.pk):
        if step.verdict != APPROVAL_STEP_SUPERSEDED:
            held[step.party] = step.assignee
    return held


def _bill_now(approval, steps, sign, full=True):
    """The request's bill as it stands, for the detail and resubmit pages, in
    bill_display()'s shape plus each task's status NOW (bill_rules.task_status_label) and,
    while the bill is open or waiting for changes, its warnings (bill_rules.bill_warnings,
    this bill excluded from "another bill", the Site Engineer and PM as they hold the
    current round). None for any other kind, at no query.

    `full` False — the viewer is only the bill's Site Engineer (sees_bill, 4a-3, D-A53):
    the work-only block, the PDF not signed, and only the task warnings (unfinished and
    Not Applicable, bill_rules.incomplete_task_warnings) — the others name bill numbers or
    are about who SCM chose (Q3).

    Two queries for the bill and its tasks, three more for the full warnings when drawn."""
    if approval.kind != APPROVAL_KIND_CONTRACTOR_BILL:
        return None
    # The bill with its site: the block names the site, and the warnings compare against
    # the site's assigned PM and its tasks.
    detail = (ContractorBillDetail.objects.select_related('project')
              .filter(request=approval).first())
    if detail is None:
        return None
    project = detail.project
    # The bill's task links in the order the bill names them (the snapshot's order), with
    # each task's phase for its heading.
    tasks = [link.task for link in detail.task_links.select_related('task__phase')
             .order_by('pk')]
    warnings = []
    if approval.status in _BILL_WARNING_STATUSES:
        if full:
            held = _current_assignees(steps, approval.current_round)
            warnings = bill_warnings(project, tasks, approval.vendor, detail.bill_number,
                                     held.get(APPROVAL_PARTY_SITE_ENGINEER),
                                     held.get(APPROVAL_PARTY_PM), exclude=approval)
        else:
            warnings = incomplete_task_warnings(project, tasks)
    block = {
        'contractor':  approval.vendor.name if approval.vendor is not None else '—',
        'site':        f'{project.project_id} — {project.customer_name}',
        'bill_number': detail.bill_number,
        'bill_date':   detail.bill_date,
        'amount':      format_bill_amount(detail.amount),
        'pdf_name':    detail.pdf_file_name,
        'pdf_size_kb': detail.pdf_size_kb,
        'pdf_url':     None,
        'tasks':       [{'label': _bill_task_label(t.task_name, t.location_label),
                         'phase': t.phase.phase_name,
                         'status': task_status_label(t, project.project_type)}
                        for t in tasks],
        'warnings':    warnings,
        'work_only':   False,
    }
    if not full:
        return _work_only(block)
    block['pdf_url'] = sign(detail.pdf_bucket, detail.pdf_path)
    return block


def _record_short(order):
    """A PO/PI record in one History line: vendor, PO and PI numbers."""
    return (f'{order.vendor.name} · PO {order.po_number or "—"} · '
            f'PI {order.pi_number or "—"}')


def _order_links(approval):
    """Every link of `approval`, removed ones included, oldest first, with what the
    section and History draw prefetched: the record's vendor, its sites with their
    projects and tenders (its label, and user_can_view_vendor_order), and the two people.
    Five queries whatever the number of links; none for a request of another kind, which
    cannot have links."""
    if approval.kind != APPROVAL_KIND_MATERIAL_PRE_ORDER:
        return []
    return list(ApprovalOrderLink.objects.filter(approval=approval)
                .select_related('vendor_order__vendor', 'linked_by__user',
                                'removed_by__user')
                .prefetch_related('vendor_order__sites__project',
                                  'vendor_order__programs__program')
                .order_by('linked_at', 'pk'))


def _order_links_section(user, approval, links):
    """The "PO/PI records covered" section of an APPROVED pre-order request (Approvals
    3b), or None for any other request. Active links, then removed ones (drawn struck
    through, with who, when and why), each oldest first. The record-page link only where
    user_can_view_vendor_order() passes. The link form and the remove actions only where
    user_can_link_approval_order() passes — SCM; the picker's queries are paid only then.
    """
    if (approval.kind != APPROVAL_KIND_MATERIAL_PRE_ORDER
            or approval.status != APPROVAL_APPROVED):
        return None
    can_link = user_can_link_approval_order(user, approval)

    def row(link):
        order = link.vendor_order
        return {
            'link':       link,
            'label':      po_pi_record_label(order),
            'url':        (reverse('vendor_order_detail', args=[order.pk])
                           if user_can_view_vendor_order(user, order) else None),
            'linked_by':  person_name(link.linked_by),
            'removed_by': person_name(link.removed_by) if link.removed_by else None,
        }

    return {
        'active':   [row(link) for link in links if link.removed_at is None],
        'removed':  [row(link) for link in links if link.removed_at is not None],
        'can_link': can_link,
        # Grouped by vendor only when the approval names none: it may then cover any
        # vendor's record. Otherwise its one vendor's records, as a flat list.
        'grouped':  approval.vendor_id is None,
        'choices':  order_link_choices(approval) if can_link else [],
    }


# The change list's fields, in the order the page draws a request's details.
_CHANGE_REQUEST_TEXT  = (('title', 'Title'), ('description', 'Description'))
# A line's fields, in the order its row draws them. Position is not compared: lines are
# renumbered 1..n on every resubmit, so a removal above a line moves it without anyone
# changing it. Each line is followed by its id instead.
_CHANGE_LINE_FIELDS = (('description', 'Description'), ('make', 'Make'),
                       ('specification', 'Specification'), ('quantity', 'Quantity'),
                       ('unit', 'Unit'))
_CHANGE_SCOPE_LISTS = (
    ('programs',    'Tenders',     lambda i: i['name']),
    ('projects',    'Sites',       lambda i: f"{i['project_id']} — {i['customer_name']}"),
    ('site_groups', 'Site groups', lambda i: f"{i.get('program_name') or '—'} · {i['name']}"),
)


def _describe_boq(item):
    return f"{item['code']} — {item['description']}"


def _describe_line(line, removed=False):
    """One added or removed line in a change list, as its own round saw it, the
    description always beside the position:

      added    'Line 3 · Mounting clamps — 40 Packet (Waaree)'
      removed  'Tracker capacity (was line 3) — 2.25 KWp'

    Lines are renumbered on every resubmit, so a new line can take a removed line's
    number; "(was line N)" and the description keep the two apart."""
    make = f" ({line['make']})" if line.get('make') else ''
    amount = f"{line['quantity']} {_unit_label(line.get('unit'))}"
    if removed:
        return f"{line['description']} (was line {line['position']}) — {amount}{make}"
    return f"Line {line['position']} · {line['description']} — {amount}{make}"


def _unit_label(unit):
    """A unit as the lines table shows it ("Packet", "Lump sum"), so the change list and
    the table use the same word. A code not on the list is shown as stored."""
    return UNIT_LABELS.get(unit, unit)


def _shown(field, value):
    """One line field's value as the change list shows it: a unit by its label."""
    return _unit_label(value) if field == 'unit' else value


def _same_line_value(field, before, after):
    """Whether one field of a line is unchanged. A quantity is compared as a number:
    "120" (Nos) and "120.00" (Meter) are the same quantity, and a unit change must not
    also read as a quantity change."""
    if field == 'quantity':
        try:
            return Decimal(str(before)) == Decimal(str(after))
        except (InvalidOperation, TypeError):
            pass
    return (before or '') == (after or '')


def _line_changes(old_lines, new_lines):
    """The change-list entries for a round's material lines, followed BY ID (every line
    keeps its pk across resubmits; approvals._replace_lines updates in place):

      * lines added and removed — one {label, added, removed} entry, each line described
        as its own round saw it, description beside position (_describe_line); a removed
        line reads "(was line N)";
      * each line still there with any field changed — {label: 'Line <n> · <what it
        was>', plus "(was line N)" if it moved, fields: [{label, old, new}]}, one entry
        per line, naming every changed field with its before and after;
      * a round with no lines followed by one with lines — a request raised before lines
        whose resubmit recorded them — reads 'Quantity recorded as lines' and lists them,
        instead of reading as every line added.
    """
    if not old_lines:
        if not new_lines:
            return []
        return [{'label': 'Quantity recorded as lines',
                 'added': [_describe_line(line) for line in new_lines], 'removed': []}]
    old = {line['id']: line for line in old_lines}
    new = {line['id']: line for line in new_lines}
    changes = []
    added = [_describe_line(new[key]) for key in new if key not in old]
    removed = [_describe_line(old[key], removed=True) for key in old if key not in new]
    if added or removed:
        changes.append({'label': 'Material lines', 'added': added, 'removed': removed})
    for key, after in new.items():
        before = old.get(key)
        if before is None:
            continue
        fields = [{'label': label, 'old': _shown(field, before.get(field)) or '—',
                   'new': _shown(field, after.get(field)) or '—'}
                  for field, label in _CHANGE_LINE_FIELDS
                  if not _same_line_value(field, before.get(field), after.get(field))]
        if fields:
            # The description as it was, beside the line's number now — and its old
            # number too when a removal above it moved it.
            moved = (f" (was line {before['position']})"
                     if before['position'] != after['position'] else '')
            changes.append({'label': f"Line {after['position']} · {before['description']}"
                                     f"{moved}",
                            'fields': fields})
    return changes


def _list_change(label, old_items, new_items, describe):
    """Added and removed items between two snapshot lists, by id. A removed item is
    described from the OLD snapshot and an added one from the NEW, so each reads as its
    round saw it even if it was renamed or deleted since. None when the ids match."""
    old = {item['id']: item for item in old_items or ()}
    new = {item['id']: item for item in new_items or ()}
    added = [describe(new[key]) for key in new if key not in old]
    removed = [describe(old[key]) for key in old if key not in new]
    if not (added or removed):
        return None
    return {'label': label, 'added': added, 'removed': removed}


def round_changes(old, new):
    """What changed from one round's snapshot to the next (D-A19), in the order the page
    draws the details: [{label, old, new}] for a single value, [{label, added, removed}]
    for a list, [{label, fields: [{label, old, new}]}] for one material line's edited
    fields. [] when nothing did. None when either snapshot is missing — the page then
    says the change list is unavailable, never guesses.

    Compared: title, description, the material lines (_line_changes: by id, field by
    field), the request-wide make, specification and quantity note recorded before lines,
    schema 1's BOQ items, the vendor (by id, shown by name), and the three scope lists.
    Either snapshot may be schema 1 or 2 (material_display reads both). Not compared: the
    kind, design sign-off, the vendor order and pre-order link (none is revisable), and
    the steps — an approver change or a kept approval is in History and on the step.
    """
    if old is None or new is None:
        return None
    changes = []

    def single(label, before, after):
        if (before or '') != (after or ''):
            changes.append({'label': label, 'old': before or '—', 'new': after or '—'})

    for key, label in _CHANGE_REQUEST_TEXT:
        single(label, old.get(key), new.get(key))
    old_material = material_display(old.get('material') or {})
    new_material = material_display(new.get('material') or {})
    changes += _line_changes(old_material['lines'], new_material['lines'])
    # Nothing writes these any more, so they change only if a schema-1 and a schema-2
    # snapshot disagree — which would be worth seeing.
    old_legacy = {item['key']: item['value'] for item in old_material['legacy']}
    new_legacy = {item['key']: item['value'] for item in new_material['legacy']}
    for key, label in _LEGACY_MATERIAL_LABELS:
        single(label, old_legacy.get(key), new_legacy.get(key))
    boq = _list_change('BOQ items', old_material['boq_items'],
                       new_material['boq_items'], _describe_boq)
    if boq:
        changes.append(boq)
    old_vendor, new_vendor = old.get('vendor'), new.get('vendor')
    if (old_vendor or {}).get('id') != (new_vendor or {}).get('id'):
        changes.append({'label': 'Vendor',
                        'old': old_vendor['name'] if old_vendor else '—',
                        'new': new_vendor['name'] if new_vendor else '—'})
    for key, label, describe in _CHANGE_SCOPE_LISTS:
        change = _list_change(label, old.get(key), new.get(key), describe)
        if change:
            changes.append(change)
    return changes


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
        'can_view_aging': user_can_view_approval_aging(request.user),
    })


@login_required
def approval_aging(request):
    """Approvals 2c — what is waiting on whom, and how each approver has answered over the
    last AGING_WINDOW_DAYS days. Read-only.

    Access: user_can_view_approval_aging — SCM, CEO, Admin, System Admin. Figures:
    approval_queries.aging_rows(), three queries whatever the size of the data, read from
    step rows with carried steps excluded (never the ledger).
    """
    if not user_can_view_approval_aging(request.user):
        return _forbidden(request)

    now = timezone.now()
    aging = aging_rows(now - timedelta(days=AGING_WINDOW_DAYS), now=now)
    return render(request, 'projects/approvals/aging.html', {
        'assignees':      aging['assignees'],
        'carried':        aging['carried'],
        'fresh_approved': aging['fresh_approved'],
        'carry_rate':     aging['carry_rate'],
        'carry_denominator': aging['carried'] + aging['fresh_approved'],
        'window_days':    AGING_WINDOW_DAYS,
        'total_pending':  sum(a['pending_count'] for a in aging['assignees']),
    })


# ---------------------------------------------------------------------------
# Raise
# ---------------------------------------------------------------------------

#: The unit select's options as the lines table draws them: (code, label, is_count). A
#: count unit's <option> carries data-count, so the page's script can set the quantity
#: input's step and min when the unit changes; the server's rule is approvals.py's.
_UNIT_OPTIONS = [(code, label, kind == UNIT_COUNT) for code, label, kind in UNITS]


def _line_form_rows(rows):
    """The lines table's rows to draw: `rows` (parse_lines' or line_rows' shape), or one
    empty row when there are none, so the table never opens with nothing to type into."""
    rows = list(rows)
    return rows or [{'id': '', 'description': '', 'make': '', 'specification': '',
                     'quantity': '', 'unit': ''}]


def _create_context(kind, post=None, client_uuid=None, already=None):
    heads = assignee_choices(APPROVAL_PARTY_DESIGN)
    is_pre_dispatch = kind == APPROVAL_KIND_MATERIAL_PRE_DISPATCH
    context = {
        'kind':             kind,
        'kind_label':       _KIND_LABELS[kind],
        'is_pre_dispatch':  is_pre_dispatch,
        'post':             post,
        'client_uuid':      client_uuid,
        # The request this form's key already raised, if any: the page then says so
        # and offers it, instead of a second Raise button.
        'already':          already,
        # Pre-dispatch names a PO/PI record and takes its vendor from it: no vendor list.
        'vendors':          [] if is_pre_dispatch else vendor_choices(),
        'po_pi_records':    po_pi_record_choices() if is_pre_dispatch else [],
        'pre_orders':       pre_order_choices() if is_pre_dispatch else [],
        'pms':              assignee_choices('pm'),
        'heads':            heads,
        # Pre-selected only when there is exactly one to choose (spec T1).
        'default_head_pk':  heads[0].pk if len(heads) == 1 else None,
        'attachment_limit': ATTACHMENT_LIMIT,
        # After a refusal, every line exactly as typed — gaps in the posted indices
        # closed up — so nothing has to be entered again.
        'lines':            _line_form_rows(parse_lines(post) if post else ()),
        'units':            _UNIT_OPTIONS,
        'picked': {
            'program':    set(post.getlist('program')) if post else set(),
            'project':    set(post.getlist('project')) if post else set(),
            'site_group': set(post.getlist('site_group')) if post else set(),
        },
    }
    context.update(scope_choices())
    return context


def _refuse_create(request, errors, client_uuid, kind):
    for error in errors:
        messages.error(request, error)
    return render(request, 'projects/approvals/create.html',
                  _create_context(kind, request.POST, client_uuid), status=400)


@login_required
def approval_create(request):
    """Raise a material approval — before an order (material_pre_order) or before
    dispatch (material_pre_dispatch). GET draws the form; POST validates, uploads, and
    calls create_approval_request().

    Access: user_can_raise_approval_request — SCM.

    ?kind=contractor_bill (4a-2) is handed to _bill_create, before anything below runs.

    THE KIND IS ?kind= IN THE URL (Approvals 3a), read on GET and POST alike, the way the
    key is: the form has no action, so it posts back to its own URL. Only the two
    material kinds are taken; anything else goes back to the list with a message. No
    ?kind= is a pre-order request, and its redirect is exactly ?key=<uuid> as before.
    A pre-dispatch request names one PO/PI record and takes its vendor from it
    (approval_forms.parse_create).

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
    kind = request.GET.get('kind')
    if kind is not None and kind not in _RAISE_KINDS:
        messages.error(request, 'Choose "Raise — before order" or "Raise — before '
                                'dispatch".')
        return redirect('approval_list')
    if kind == APPROVAL_KIND_CONTRACTOR_BILL:
        # A contractor bill is its own page (two steps, a PDF, warnings); the material
        # flow below is left exactly as it was.
        return _bill_create(request, parse_client_uuid(request.GET, 'key'))
    kind_query = f'kind={kind}&' if kind is not None else ''
    kind = kind or APPROVAL_KIND_MATERIAL_PRE_ORDER
    key = parse_client_uuid(request.GET, 'key')
    if request.method != 'POST':
        if key is None:
            return redirect(f"{reverse('approval_create')}?{kind_query}key={_uuid.uuid4()}")
        already = ApprovalRequest.objects.filter(client_uuid=key).first()
        return render(request, 'projects/approvals/create.html',
                      _create_context(kind, client_uuid=key, already=already))

    cleaned, errors = parse_create(request, kind)
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
        return _refuse_create(request, errors, client_uuid, kind)

    try:
        stored, cleanup = _upload_attachments(files, client_uuid)
    except _AttachmentUploadFailed as exc:
        return _refuse_create(request, [str(exc)], client_uuid, kind)

    try:
        approval = create_approval_request(
            kind=kind, raised_by=request.user.profile,
            attachments=stored, client_uuid=client_uuid, **cleaned)
    except ApprovalRefused as exc:
        cleanup()
        return _refuse_create(request, [str(exc)], client_uuid, kind)
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
# Raise — contractor bill (4a-2)
# ---------------------------------------------------------------------------

def _bill_step_url(key, project=None):
    """The bill raise page: step 1 (choose the site) under `key`, or step 2 for
    `project`. The key stays the same across the two steps and a change of site."""
    url = f"{reverse('approval_create')}?kind={APPROVAL_KIND_CONTRACTOR_BILL}&key={key}"
    return f'{url}&project={project.pk}' if project is not None else url


def _task_groups(project, tasks):
    """Step 2's task list: [{phase, tasks: [{task, status}]}], one entry per phase in the
    order bill_task_choices() gives. Each task shows its status now, so SCM sees an
    unfinished one before ticking it."""
    groups = []
    for task in tasks:
        if not groups or groups[-1]['phase_pk'] != task.phase_id:
            groups.append({'phase_pk': task.phase_id, 'phase': task.phase.phase_name,
                           'tasks': []})
        groups[-1]['tasks'].append({
            'task':   task,
            'label':  _bill_task_label(task.task_name, task.location_label),
            'status': task_status_label(task, project.project_type)})
    return groups


def _bill_create_context(key, project=None, post=None, already=None, warnings=None):
    """The bill raise page's context. Step 1 (no `project`): the sites. Step 2: the
    contractors, the site's tasks, its Site Engineers (bill_rules.site_engineer_choices —
    those holding a task on the site first) and the PMs, pre-filled from `post` after a
    refusal or a warning, otherwise with the defaults: the site's assigned PM when that
    person may be named, and the Site Engineer when exactly one holds a task on the site
    (ruling 8)."""
    context = {
        'kind_label':     dict(APPROVAL_KIND_CHOICES)[APPROVAL_KIND_CONTRACTOR_BILL],
        'client_uuid':    key,
        'already':        already,
        'storage_ready':  bool(bills_bucket()),
        'storage_notice': BILL_STORAGE_NOT_READY,
        'project':        project,
        'post':           post,
        'warnings':       warnings or [],
        'step1_url':      _bill_step_url(key),
    }
    if project is None:
        context['sites'] = bill_site_choices()
        return context
    engineers = site_engineer_choices(project)
    on_site = [profile for profile, holds in engineers if holds]
    pms = assignee_choices('pm')
    if post is not None:
        selected_se = post.get('site_engineer_assignee', '')
        selected_pm = post.get('pm_assignee', '')
        picked = set(post.getlist('task'))
    else:
        selected_se = str(on_site[0].pk) if len(on_site) == 1 else ''
        selected_pm = (str(project.assigned_pm_id)
                       if project.assigned_pm_id in {p.pk for p in pms} else '')
        picked = set()
    context.update({
        'contractors':   contractor_choices(),
        'task_groups':   _task_groups(project, bill_task_choices(project)),
        'picked_tasks':  picked,
        'engineers':     engineers,
        'pms':           pms,
        'selected_se':   selected_se,
        'selected_pm':   selected_pm,
        'today':         timezone.localdate(),
        'attachment_limit': ATTACHMENT_LIMIT,
        'photo_accept':  ','.join(f'.{ext}' for ext in BILL_PHOTO_EXTENSIONS),
        'approval_note': BILL_APPROVAL_NOTE,
    })
    return context


def _bill_create(request, key):
    """Raise a contractor bill: ?kind=contractor_bill on the raise page (4a-2).

    Access: SCM — approval_create asked user_can_raise_approval_request before handing
    over; nobody else reaches this function.

    TWO STEPS (ruling 1). Without ?project= the page asks for the site only (a GET form
    that keeps the key); with it, the rest. The site is read from the URL on GET and POST
    alike, as the kind is, and must be one bill_sites() offers — anything else goes back
    to step 1 with a message. The key (?key=, D-A27) is the same across both steps.

    THE ORDER OF A POST, and what each failure leaves behind:
      1. the key has already raised a bill: that bill, nothing uploaded;
      2. the bills bucket is unset: BILL_STORAGE_OFF, nothing uploaded (400);
      3. the form (parse_bill_create: the PDF's bytes, the photos, stale choices), then
         approvals._clean_bill with a placeholder PDF — the chokepoint's own refusals for
         the contractor, site, tasks, amount, number and date: 400, nothing uploaded;
      4. the warnings (bill_rules.bill_warnings), unless "Raise anyway" was ticked: the
         page again with them, status 200, the same key, nothing uploaded (D-A50);
      5. the PDF, to the private bucket — a failure is its message, nothing stored;
      6. the photos, to the public bucket — a failure removes the photos already stored
         (_upload_attachments) and the PDF (discard_unrecorded_bill_pdf);
      7. create_approval_request() — a refusal or error removes the photos and the PDF;
         if it returned a racing twin's bill, our PDF and photos are removed.
    A refusal after step 5 means choosing the files again; the rules asked in step 3 are
    the ones most often got wrong, so few refusals come that late.
    """
    raw_project = request.GET.get('project')
    project = bill_site(raw_project) if raw_project is not None else None

    if request.method != 'POST':
        if key is None:
            return keyed_redirect(request)
        already = ApprovalRequest.objects.filter(client_uuid=key).first()
        if raw_project is not None and project is None:
            messages.error(request, 'That site cannot take a bill: it is Draft, deleted, '
                                    'test data or not found. Choose the site again.')
            return redirect(_bill_step_url(key))
        return render(request, 'projects/approvals/create_bill.html',
                      _bill_create_context(key, project, already=already))

    if project is None:
        messages.error(request, 'Choose the site this bill is for.')
        return redirect(_bill_step_url(key or _uuid.uuid4()))

    cleaned, errors = parse_bill_create(request)
    client_uuid = cleaned.pop('client_uuid') or key
    if client_uuid is not None:
        existing = ApprovalRequest.objects.filter(client_uuid=client_uuid).first()
        if existing is not None:
            messages.info(request, f'This form already raised request #{existing.pk} — '
                                   f'here it is. Nothing new was raised.')
            return _detail(existing.pk)
    else:
        client_uuid = _uuid.uuid4()   # a page without its key still gets one folder

    def page(problems=(), status=400, warnings=None):
        for problem in problems:
            messages.error(request, problem)
        return render(request, 'projects/approvals/create_bill.html',
                      _bill_create_context(client_uuid, project, post=request.POST,
                                           warnings=warnings),
                      status=status)

    if not bills_bucket():
        return page([BILL_STORAGE_OFF])
    if errors:
        return page(errors)
    pdf_file, photos, vendor = cleaned['pdf'], cleaned['photos'], cleaned['vendor']
    try:
        # The placeholder names the configured private bucket, so _clean_bill judges
        # everything but a file that does not exist yet; the real one is judged in step 7.
        checked = _clean_bill(
            ContractorBill(project, cleaned['tasks'], cleaned['amount'],
                           cleaned['bill_number'], cleaned['bill_date'],
                           {'file_name': pdf_file.name, 'bucket': bills_bucket(),
                            'path': 'not-yet-uploaded'}),
            vendor, (), [project], ())
    except ApprovalRefused as exc:
        return page([str(exc)])

    if request.POST.get('confirm_warnings') != '1':
        warnings = bill_warnings(project, checked['tasks'], vendor, checked['bill_number'],
                                 cleaned['site_engineer_assignee'], cleaned['pm_assignee'])
        if warnings:
            return page(warnings=warnings, status=200)

    try:
        stored_pdf = upload_bill_pdf(pdf_file, project)
    except BillStorageError as exc:
        return page([str(exc)])
    try:
        stored_photos, cleanup = _upload_attachments(photos, client_uuid)
    except _AttachmentUploadFailed as exc:
        discard_unrecorded_bill_pdf(stored_pdf)
        return page([str(exc)])

    try:
        approval = create_approval_request(
            kind=APPROVAL_KIND_CONTRACTOR_BILL, raised_by=request.user.profile,
            title=cleaned['title'], description=cleaned['description'],
            pm_assignee=cleaned['pm_assignee'],
            site_engineer_assignee=cleaned['site_engineer_assignee'], vendor=vendor,
            bill=ContractorBill(project, cleaned['tasks'], cleaned['amount'],
                                cleaned['bill_number'], cleaned['bill_date'], stored_pdf),
            attachments=stored_photos, client_uuid=client_uuid)
    except ApprovalRefused as exc:
        cleanup()
        discard_unrecorded_bill_pdf(stored_pdf)
        return page([str(exc)])
    except Exception:
        cleanup()
        # A racing twin wrote the same client_uuid between the read above and this
        # create: its bill is the answer, and the discard below removes our PDF. Anything
        # else is a real failure, and our PDF is removed before it is raised.
        approval = ApprovalRequest.objects.filter(client_uuid=client_uuid).first()
        if approval is None:
            discard_unrecorded_bill_pdf(stored_pdf)
            raise

    # The chokepoint returned an earlier submit's bill: our PDF is recorded by no bill, so
    # this removes it (it refuses to remove a recorded PDF — the normal case — and does
    # nothing then). The same for our photos, by the material path's check.
    discard_unrecorded_bill_pdf(stored_pdf)
    if stored_photos and not approval.attachments.filter(
            path__in=[item['path'] for item in stored_photos]).exists():
        cleanup()

    messages.success(request, f'Contractor bill raised: {approval.title}.')
    return _detail(approval.pk)


# ---------------------------------------------------------------------------
# Detail
# ---------------------------------------------------------------------------

_DECISION_VERBS = {
    APPROVAL_STEP_APPROVED:          'approved',
    APPROVAL_STEP_CHANGES_REQUESTED: 'requested changes',
    APPROVAL_STEP_REJECTED:          'rejected',
}

#: A Site Engineer's decisions in History (D-A53, Q4): what they said about the work.
_SITE_ENGINEER_HISTORY = {
    APPROVAL_STEP_APPROVED:          'confirmed the work done',
    APPROVAL_STEP_CHANGES_REQUESTED: 'said the work is not done',
}

# The request status a decision moves it to, and the step verdict that moved it there.
_VERDICT_FOR_STATUS = {
    APPROVAL_APPROVED:          APPROVAL_STEP_APPROVED,
    APPROVAL_CHANGES_REQUESTED: APPROVAL_STEP_CHANGES_REQUESTED,
    APPROVAL_REJECTED:          APPROVAL_STEP_REJECTED,
}


def _event(at, rank, what, actor, lines=(), remark='', remark_label=''):
    return {'at': at, 'rank': rank, 'what': what, 'actor': actor, 'lines': list(lines),
            'remark': remark, 'remark_label': remark_label, 'outcome': ''}


def _history(approval, steps, links=()):
    """Everything done on the request, oldest first, each naming who and when (D-A22).

    No one table holds it all, so three sources are merged:

      * THE LEDGER (StatusTransition): raised, resubmitted (its remark carries any
        approver change and kept approval, then the note), withdrawn.
      * THE DECIDED STEP ROWS: one entry per decision, by the decider, at decided_at; a
        proxy adds who recorded it and from where. A decision that does not move the
        request (a PM approving while the Design Head is still to decide) has no ledger
        row, and a proxy's ledger row names the decider, never the SCM user who typed it
        — so the steps, not the ledger, are the record of decisions. A carried step is
        not a decision and has no entry: the resubmit that kept it says so.
      * REASSIGNMENTS: reassign_approval_step() writes no ledger row. It supersedes the
        old step (superseded_by = SCM, superseded_at, note = the reason) and creates a
        new row for the same round and party — and only a reassignment leaves a
        superseded step with a LATER row for its own round and party (a closed round or
        a withdrawal supersedes the last row of each). One entry per such step.
      * PO/PI RECORD LINKS (Approvals 3b, `links`): linking and removing a link write no
        ledger row either — the request's status does not move. Each link row gives
        "Linked PO/PI record …" by linked_by at linked_at, and a removed one also
        "Removed link to PO/PI record …" by removed_by at removed_at, with the reason.

    FOLDING. A decision that moved the request also wrote a ledger row, with the decider
    as actor. That row is matched to its decision — the latest decided step, not yet
    matched, with the verdict that status needs, by the ledger row's actor, decided at
    or before the row — and shown as "Request now …" on the decision instead of as a
    second line. NEVER DROP: a ledger row that matches no decision is drawn as its own
    line, with its actor and time.
    """
    events, decisions = [], {}
    for step in steps:
        if (step.decided_at is None or step.carried_from_id is not None
                or step.verdict not in _DECISION_VERBS):
            continue
        party = _PARTY_LABELS.get(step.party, step.party)
        proxy = _proxy_line(step)
        # A Site Engineer confirms the work or says it is not done (D-A53, Q4).
        verb = (_SITE_ENGINEER_HISTORY.get(step.verdict, _DECISION_VERBS[step.verdict])
                if step.party == APPROVAL_PARTY_SITE_ENGINEER
                else _DECISION_VERBS[step.verdict])
        event = _event(step.decided_at, (1, step.pk),
                       f'{party} {verb} (round {step.round})',
                       person_name(step.decided_by), lines=[proxy] if proxy else (),
                       remark=step.note)
        decisions[step.pk] = event
        events.append(event)

    for step in steps:
        if step.verdict != APPROVAL_STEP_SUPERSEDED or step.superseded_at is None:
            continue
        successor = min((s for s in steps if s.round == step.round
                         and s.party == step.party and s.pk > step.pk),
                        key=lambda s: s.pk, default=None)
        if successor is None:
            continue
        party = _PARTY_LABELS.get(step.party, step.party)
        events.append(_event(
            step.superseded_at, (1, step.pk),
            f'Reassigned the {party} step (round {step.round})',
            person_name(step.superseded_by) if step.superseded_by else 'System',
            lines=[f'{person_name(step.assignee)} → {person_name(successor.assignee)}'],
            remark=step.note, remark_label='Reason:'))

    for link in links:
        record = _record_short(link.vendor_order)
        events.append(_event(link.linked_at, (3, link.pk), f'Linked PO/PI record {record}',
                             person_name(link.linked_by)))
        if link.removed_at is not None:
            events.append(_event(
                link.removed_at, (4, link.pk), f'Removed link to PO/PI record {record}',
                person_name(link.removed_by) if link.removed_by else 'System',
                remark=link.removal_note, remark_label='Reason:'))

    rows = (StatusTransition.objects
            .filter(subject_type=SUBJECT_APPROVAL_REQUEST, subject_id=approval.pk)
            .select_related('actor__user').order_by('occurred_at', 'pk'))
    matched, resubmits = set(), 0
    for row in rows:
        actor = person_name(row.actor) if row.actor else 'System'
        if row.reason_code == REASON_CREATED:
            events.append(_event(row.occurred_at, (0, row.pk), 'Raised', actor,
                                 remark=row.remark))
            continue
        if row.reason_code == REASON_RESUBMITTED:
            resubmits += 1
            events.append(_event(row.occurred_at, (2, row.pk),
                                 f'Resubmitted as round {resubmits + 1}', actor,
                                 remark=row.remark))
            continue
        verdict = _VERDICT_FOR_STATUS.get(row.to_status)
        candidates = [s for s in steps
                      if verdict is not None and s.pk in decisions and s.pk not in matched
                      and s.verdict == verdict and s.decided_by_id == row.actor_id
                      and s.decided_at <= row.occurred_at]
        if candidates:
            step = max(candidates, key=lambda s: (s.decided_at, s.pk))
            matched.add(step.pk)
            decisions[step.pk]['outcome'] = (
                f'Request now {_STATUS_LABELS.get(row.to_status, row.to_status).lower()}')
            continue
        events.append(_event(row.occurred_at, (2, row.pk),
                             _STATUS_LABELS.get(row.to_status, row.to_status), actor,
                             remark=row.remark))

    events.sort(key=lambda e: (e['at'], e['rank']))
    return events


def _step_row(user, step, approval, deciders, by_pk=None, evidence=(), kept_photos=()):
    """One step as the page draws it, with the action forms its predicates allow. A
    carried step is described as kept (_kept) and is never timed: it was activated and
    decided in the same instant by nobody's fresh act (approvals.exclude_carried_steps).

    A Site Engineer step (4a-3, D-A53) reads as the work, not the bill: its verdict as
    "Work confirmed" / "Work not done", its own site photos as `evidence`, and — when it
    is kept — the photos of the step it keeps as `kept_photos` ("Photos from round N").
    Its decider gets two forms, confirm (photos required) and work not done (note
    required), instead of the three decision buttons; SCM's proxy form offers those two."""
    step.request = approval   # the predicates read step.request; no query
    kept = _kept(step, by_pk or {})
    is_site_engineer = step.party == APPROVAL_PARTY_SITE_ENGINEER
    turnaround = step_turnaround(step)
    if kept:
        turnaround_text = 'Not timed (kept)'
    else:
        turnaround_text = _duration_text(turnaround) if turnaround is not None else None
    row = {
        'step':         step,
        'party':        _PARTY_LABELS.get(step.party, step.party),
        'badge':        _STEP_BADGES.get(step.verdict, 'text-bg-secondary'),
        'turnaround':   turnaround_text,
        'kept':         kept,
        'kept_photos':  list(kept_photos),
        'evidence':     list(evidence),
        'is_site_engineer': is_site_engineer,
        'verdict_label': (_SITE_ENGINEER_VERDICTS.get(step.verdict)
                          if is_site_engineer else None) or step.get_verdict_display(),
        'proxy_decisions': _SITE_ENGINEER_DECISIONS if is_site_engineer else _DECISIONS,
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

    Each round also shows the details its approvers saw — round_snapshot(), or the
    request as it stands now under a label saying so when the round has none — and,
    from round 2, what changed since the round before (round_changes). A carried step
    reads as kept (_kept). A proxy's evidence files are listed under its step. History
    is _history(): every action, who and when.

    A contractor bill (4a-2) adds its bill block (_bill_now): the amount beside a signed
    link to the PDF ("File unavailable" when none can be minted), the tasks with their
    status now, the warnings while it is open or waiting for changes, and B-16's note.
    Each round draws the bill its snapshot recorded (bill_display). A material request
    pays no query for any of it.

    THE SITE ENGINEER SEES THE WORK, NOT THE BILL (4a-3, D-A53). When sees_bill() says the
    viewer's only standing is being the bill's Site Engineer, the Request card and every
    round draw the site, contractor and tasks without the amount, bill number, date or PDF
    (the PDF is not even signed), only the task warnings, and the Site Engineer's note in
    place of B-16's. A Site Engineer step shows its site photos; a kept one, the photos of
    the step it keeps.

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

    # A contractor bill has none; the reverse accessor's DoesNotExist is an AttributeError.
    material = getattr(approval, 'material_detail', None)
    lines = list(material.lines.order_by('position')) if material else []
    programs = list(approval.programs.all())
    # Scope is for record; a soft-deleted site is not drawn (models.ApprovalRequest).
    projects = list(approval.projects.filter(is_deleted=False)
                    .only('pk', 'project_id', 'customer_name', 'is_deleted'))
    site_groups = list(approval.site_groups.all())
    current = _current_details(approval, material, lines, programs, projects,
                               site_groups)

    steps = list(approval.steps.all())
    by_pk = {s.pk: s for s in steps}
    attachments = [{'file': a, 'url': vendor_order_document_url(a)}
                   for a in approval.attachments.all()]
    snapshots = {n: round_snapshot(approval, n)
                 for n in range(1, approval.current_round + 1)}
    dispatch, round_dispatch = _dispatch_against(user, approval, material, snapshots)
    links = _order_links(approval)
    # A contractor bill's PDF links are minted here, per render, never stored (D-A40).
    sign = _pdf_signer()
    # A viewer who is only the bill's Site Engineer reads the work, never the bill's
    # amount, number, date or PDF — on the Request card and on every round (D-A53).
    full = sees_bill(user, approval, steps)
    bill = _bill_now(approval, steps, sign, full=full)
    # Each step's own files: a proxy's evidence, or a Site Engineer's site photos.
    files_of = {}
    for a in attachments:
        if a['file'].step_id is not None:
            files_of.setdefault(a['file'].step_id, []).append(a)
    rounds = []
    for round_no in range(approval.current_round, 0, -1):     # newest first
        snapshot = snapshots[round_no]
        details = _for_display(snapshot if snapshot is not None else current)
        if bill is not None:
            details['bill'] = bill_display((snapshot or {}).get('bill'),
                                           ((snapshot or {}).get('vendor') or {}).get('name'),
                                           sign, full=full)
        rounds.append({
            'number':       round_no,
            'is_current':   round_no == approval.current_round,
            'steps':        [_step_row(user, s, approval, deciders, by_pk,
                                       files_of.get(s.pk, ()),
                                       # A kept Site Engineer step shows the photos of the
                                       # step it keeps (P6, 4a-3); a kept step has none.
                                       files_of.get(_origin_step(s, by_pk).pk, ())
                                       if s.carried_from_id is not None else ())
                             for s in steps if s.round == round_no],
            # A proxy's evidence is drawn under its step, not with the round's files.
            'attachments':  [a for a in attachments
                             if a['file'].round == round_no and a['file'].step_id is None],
            'details':      details,
            'dispatch':     round_dispatch.get(round_no),
            'has_snapshot': snapshot is not None,
            'changes':      (round_changes(snapshots[round_no - 1], snapshot)
                             if round_no > 1 else None),
        })

    return render(request, 'projects/approvals/detail.html', {
        'approval':       approval,
        'status_badge':   _STATUS_BADGES.get(approval.status, 'text-bg-secondary'),
        'material':       material,
        # The request's material as it stands, drawn as the rounds draw theirs.
        'material_now':   material_display(current['material']),
        'bill':           bill,
        # B-16's note for whoever sees the bill; the Site Engineer's own note otherwise.
        'bill_note':      BILL_APPROVAL_NOTE if full else SITE_ENGINEER_WORK_NOTE,
        'dispatch':       dispatch,
        'programs':       programs,
        'projects':       projects,
        'site_groups':    site_groups,
        'rounds':         rounds,
        'order_links':    _order_links_section(user, approval, links),
        'history':        _history(approval, steps, links),
        'evidence_accept': ','.join(f'.{ext}' for ext in EVIDENCE_EXTENSIONS),
        'photo_accept':   ','.join(f'.{ext}' for ext in BILL_PHOTO_EXTENSIONS),
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

    A SITE ENGINEER STEP (4a-3, D-A53): "Confirm work done" (approved) or "Work not done"
    (changes requested), each with site photos under `site_photos` — jpg/jpeg/png,
    checked before any is stored, uploaded, then passed as `files` and linked to the step
    by the chokepoint. At least one photo to confirm, and no reject, are the chokepoint's
    rules; on its refusal or any error the stored photos are removed again.
    """
    step, approval = _step_for_action(step_pk)
    # Only the person who answers this step (its assignee, or Design Head authority on a
    # design step) may post here; anyone else who can read the request is not its decider.
    if not (user_can_view_approval_request(request.user, approval)
            and user_may_answer_approval_step(request.user, step)):
        return _forbidden(request)
    if request.method != 'POST':
        return _detail(approval.pk)

    verdict = request.POST.get('verdict', '')
    is_site_engineer = step.party == APPROVAL_PARTY_SITE_ENGINEER
    stored, cleanup = [], lambda: None
    if is_site_engineer:
        errors = []
        photos = parse_site_photos(request, errors)
        if errors:
            for error in errors:
                messages.error(request, error)
            return _detail(approval.pk)
        try:
            stored, cleanup = _upload_attachments(
                photos, f'{approval.pk}/round-{step.round}/site-{step.pk}')
        except _AttachmentUploadFailed as exc:
            messages.error(request, str(exc))
            return _detail(approval.pk)
    try:
        apply_approval_decision(step, verdict, request.user.profile,
                                note=request.POST.get('note', ''), files=stored)
    except ApprovalRefused as exc:
        cleanup()
        messages.error(request, str(exc))
        return _detail(approval.pk)
    except Exception:
        cleanup()
        raise
    label = ((_SITE_ENGINEER_VERDICTS if is_site_engineer else dict(_DECISIONS))
             .get(verdict, verdict))
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

    EVIDENCE FILES (D-A21, optional): PDFs and photos (EVIDENCE_EXTENSIONS), validated
    before any reaches storage, uploaded, then passed as ProxyDecision.files — the
    chokepoint writes their rows, linked to the step, inside its transaction. On any
    refusal or error the uploaded files are removed again (_remove_uploaded, via the
    upload helper's cleanup), so a refused proxy leaves neither a row nor a file.

    On a Site Engineer step (4a-3) the form offers "Confirm work done" and "Work not
    done" only; confirming needs at least one photo among the evidence files (the ones the
    Site Engineer sent), which the chokepoint enforces.
    """
    step, approval = _step_for_action(step_pk)
    if not (user_can_view_approval_request(request.user, approval)
            and user_can_raise_approval_request(request.user)):
        return _forbidden(request)
    if request.method != 'POST':
        return _detail(approval.pk)

    decided_by, channel, evidence = parse_proxy(request.POST)
    verdict = request.POST.get('verdict', '')
    errors = []
    files = parse_evidence_files(request, errors)
    if errors:
        for error in errors:
            messages.error(request, error)
        return _detail(approval.pk)
    try:
        stored, cleanup = _upload_attachments(
            files, f'{approval.pk}/round-{step.round}/proxy-{step.pk}')
    except _AttachmentUploadFailed as exc:
        messages.error(request, str(exc))
        return _detail(approval.pk)

    try:
        apply_approval_decision(step, verdict, request.user.profile,
                                note=request.POST.get('note', ''),
                                proxy=ProxyDecision(decided_by, channel, evidence, stored))
    except ApprovalRefused as exc:
        cleanup()
        messages.error(request, str(exc))
        return _detail(approval.pk)
    except Exception:
        cleanup()
        raise
    messages.success(request, f'Recorded {person_name(decided_by)}\'s decision on the '
                              f'{_PARTY_LABELS.get(step.party, step.party)} step.')
    return _detail(approval.pk)


def _resubmit_context(approval, material, latest, keepable, by_pk, post, bill=None):
    """The resubmit form's context. Every value is pre-filled from the request as it
    stands, or — after a refusal — from what was posted, so nothing typed is lost.

    `bill` (4a-2) is a contractor bill's _bill_now() block: the page then draws it
    read-only in place of the request, material and scope cards, and offers photos only;
    the vendor and scope lists are not read, because nothing on a bill's page edits
    them."""
    if post is not None:
        values = {key: post.get(key, '') for key in
                  ('title', 'description', 'vendor', 'note')}
        lines = parse_lines(post)
        picked = {'program':    set(post.getlist('program')),
                  'project':    set(post.getlist('project')),
                  'site_group': set(post.getlist('site_group'))}
    else:
        values = {'title': approval.title, 'description': approval.description,
                  'vendor': str(approval.vendor_id or ''), 'note': ''}
        lines = line_rows(material)
        # A bill's page draws no scope pickers, so its scope is not read.
        scope = (current_scope_pks(approval) if bill is None
                 else {'programs': (), 'projects': (), 'site_groups': ()})
        picked = {'program':    {str(pk) for pk in scope['programs']},
                  'project':    {str(pk) for pk in scope['projects']},
                  'site_group': {str(pk) for pk in scope['site_groups']}}

    parties = []
    for party, step in latest.items():
        kept = keepable.get(party)
        parties.append({
            'party':    party,
            'label':    _PARTY_LABELS.get(party, party),
            'holder':   step.assignee,
            'choices':  assignee_choices(party),
            'selected': (post.get(f'assignee_{party}') if post is not None else None)
                        or str(step.assignee_id),
            'keep':     ({'decider': person_name(kept.decided_by),
                          'round': _origin_step(kept, by_pk).round}
                         if kept is not None else None),
            'keep_checked': post is not None and post.get(f'keep_{party}') == 'on',
            'keep_reason':  post.get(f'keep_reason_{party}', '') if post is not None else '',
        })

    if bill is not None:
        return {
            'approval':         approval,
            'bill':             bill,
            'bill_note':        BILL_APPROVAL_NOTE,
            'parties':          parties,
            'values':           values,
            'attachment_limit': ATTACHMENT_LIMIT,
            'attachment_accept': ','.join(f'.{ext}' for ext in BILL_PHOTO_EXTENSIONS),
            'keep_reason_min':  KEEP_REASON_MIN,
        }

    context = {
        'approval':         approval,
        'material':         material,
        # The vendor is the PO/PI record's (Approvals 3a): drawn read-only, never posted.
        'is_pre_dispatch':  approval.kind == APPROVAL_KIND_MATERIAL_PRE_DISPATCH,
        'lines':            _line_form_rows(lines) if material else [],
        'units':            _UNIT_OPTIONS,
        # A request raised before lines: its request-wide make, specification and
        # quantity, read-only above the table it must now fill in.
        'legacy':           ([{'label': label, 'value': getattr(material, key)}
                              for key, label in _LEGACY_MATERIAL_LABELS
                              if getattr(material, key)] if material else []),
        'parties':          parties,
        'values':           values,
        'picked':           picked,
        'vendors':          vendor_choices(),
        # The vendor the request names, deactivated since: still offered, so leaving the
        # select alone is not a change.
        'inactive_vendor':  (approval.vendor if approval.vendor is not None
                             and not approval.vendor.is_active else None),
        'attachment_limit': ATTACHMENT_LIMIT,
        'keep_reason_min':  KEEP_REASON_MIN,
    }
    context.update(scope_choices())
    return context


@login_required
def approval_resubmit(request, approval_pk):
    """GET: the resubmit form. POST: open the next round.

    The form carries (Approvals 2a-2): the request's revisable details, pre-filled —
    title, description, vendor, the three scope lists (D-A18) — of which ONLY the changed
    ones are sent as `revision` (parse_revision); design sign-off is shown read-only. The
    material lines table, pre-filled from the live lines, where lines may be added,
    edited or removed; it is sent whole as `lines` (at least one — a request raised
    before lines shows its old make, specification and quantity read-only above an
    empty table it must fill in). For each party
    whose step in the round that asked for changes is APPROVED, a "Keep <name>'s
    approval" box with a reason (D-A20; parse_carry holds the form rules). An approver
    override per party, as before. The note ("Describe what you changed") and new
    attachments.

    Access: user_can_view_approval_request AND user_can_raise_approval_request — SCM.
    Anyone else: 403. A request no longer waiting for changes: a message and back to the
    request. Calls resubmit_approval_request().

    A CONTRACTOR BILL (4a-2, ruling 3) carries the note, photos (jpg/jpeg/png), approver
    changes and kept approvals only: the bill is shown read-only and no revision is ever
    sent for it, whatever is posted.

    A REFUSAL — the form's or the chokepoint's — redraws the form (400) with its message
    and everything typed still in it, while the request is still waiting for changes.
    If it no longer is (another SCM user resubmitted or withdrew it meanwhile), the
    message goes back to the request instead: there is no form left to fill.
    """
    approval = _request_for_read(approval_pk)
    if not (user_can_view_approval_request(request.user, approval)
            and user_can_raise_approval_request(request.user)):
        return _forbidden(request)
    if not user_can_resubmit_approval_request(request.user, approval):
        messages.error(request, 'This request is not waiting for changes, so there is '
                                'nothing to resubmit.')
        return _detail(approval.pk)

    material = getattr(approval, 'material_detail', None)
    steps = list(approval.steps.all())
    by_pk = {s.pk: s for s in steps}
    is_bill = approval.kind == APPROVAL_KIND_CONTRACTOR_BILL
    # A bill is drawn read-only on its resubmit page; its PDF link is minted per render.
    bill = _bill_now(approval, steps, _pdf_signer()) if is_bill else None
    # Who holds each party now: the last row per party in the round that asked for
    # changes — the same rule resubmit_approval_request() applies.
    latest = {}
    for step in sorted((s for s in steps if s.round == approval.current_round),
                       key=lambda s: s.pk):
        latest[step.party] = step
    holders = {party: step.assignee for party, step in latest.items()}
    keepable = keepable_steps(latest)

    def form(status=200):
        post = request.POST if request.method == 'POST' else None
        return render(request, 'projects/approvals/resubmit.html',
                      _resubmit_context(approval, material, latest, keepable, by_pk, post,
                                        bill=bill),
                      status=status)

    if request.method != 'POST':
        return form()

    errors = []
    files = parse_bill_photos(request, errors) if is_bill else parse_attachments(request, errors)
    overrides = parse_assignee_overrides(request.POST, holders, errors)
    # A BILL IS NEVER REVISED HERE (ruling 3, 4a-2), whatever the POST carries: the page
    # draws no editable field for it, and the chokepoint does not yet refuse a scope or
    # vendor change on a bill — one would move the bill's recorded site off the site the
    # bill is for (SECONDARY_FINDINGS, 4a-2). 4b adds bill revision to the chokepoint.
    revision = {} if is_bill else parse_revision(request.POST, approval, errors)
    # The lines table is sent whole, as the set the next round is asked about — only
    # from a page that drew the editable fields (revise=1, as parse_revision), and only
    # for a material request. Unchanged lines are rewritten as they were; round_changes
    # compares by content, so that reads as no change.
    lines = (parse_lines(request.POST)
             if material is not None and request.POST.get('revise') == '1' else None)
    carry = parse_carry(request.POST, keepable, overrides, errors)
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
        resubmitted = resubmit_approval_request(approval, request.user.profile,
                                                request.POST.get('note', ''),
                                                attachments=stored,
                                                assignees=overrides or None,
                                                revision=revision or None,
                                                carry=carry or None,
                                                lines=lines)
    except ApprovalRefused as exc:
        cleanup()
        messages.error(request, str(exc))
        if ApprovalRequest.objects.filter(pk=approval.pk,
                                          status=APPROVAL_CHANGES_REQUESTED).exists():
            return form(status=400)
        return _detail(approval.pk)
    except Exception:
        cleanup()
        raise
    messages.success(request, f'Resubmitted as round {resubmitted.current_round}.')
    return _detail(resubmitted.pk)


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


# ---------------------------------------------------------------------------
# Approvals 3b — PO/PI records covered by an approved pre-order request
# ---------------------------------------------------------------------------

@login_required
def approval_link_order(request, approval_pk):
    """POST: SCM links a PO/PI record to an approved pre-order request.

    Access: user_can_view_approval_request AND user_can_raise_approval_request — SCM.
    Anyone else: 403. Everything about the request and the record — its kind, its status,
    the vendor, an existing link — is the chokepoint's refusal, as a message. Calls
    link_order_to_approval().
    """
    approval = get_object_or_404(ApprovalRequest.objects.prefetch_related('steps'),
                                 pk=approval_pk)
    if not (user_can_view_approval_request(request.user, approval)
            and user_can_raise_approval_request(request.user)):
        return _forbidden(request)
    if request.method != 'POST':
        return _detail(approval.pk)
    order = parse_linked_order(request.POST)
    try:
        link_order_to_approval(approval, order, request.user.profile)
    except ApprovalRefused as exc:
        messages.error(request, str(exc))
        return _detail(approval.pk)
    messages.success(request, f'Linked PO/PI record {_record_short(order)}.')
    return _detail(approval.pk)


@login_required
def approval_unlink_order(request, link_pk):
    """POST: SCM removes a PO/PI record link, with a reason. The link is kept, marked
    removed, and stays in History.

    Access: user_can_view_approval_request (on the link's request) AND
    user_can_raise_approval_request — SCM. Anyone else: 403. A missing reason or a link
    already removed: the chokepoint's refusal, as a message. Calls
    unlink_order_from_approval().
    """
    link = get_object_or_404(
        ApprovalOrderLink.objects.select_related('vendor_order__vendor'), pk=link_pk)
    approval = get_object_or_404(ApprovalRequest.objects.prefetch_related('steps'),
                                 pk=link.approval_id)
    if not (user_can_view_approval_request(request.user, approval)
            and user_can_raise_approval_request(request.user)):
        return _forbidden(request)
    if request.method != 'POST':
        return _detail(approval.pk)
    try:
        unlink_order_from_approval(link, request.user.profile, request.POST.get('note', ''))
    except ApprovalRefused as exc:
        messages.error(request, str(exc))
        return _detail(approval.pk)
    messages.success(request, f'Removed the link to PO/PI record '
                              f'{_record_short(link.vendor_order)}.')
    return _detail(approval.pk)
