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
"""
import logging
import uuid as _uuid
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Prefetch
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from .approval_forms import (
    ATTACHMENT_LIMIT, EVIDENCE_EXTENSIONS, KEEP_REASON_MIN, assignee_choices,
    current_scope_pks, design_authority_choices, keepable_steps, parse_assignee_overrides,
    parse_attachments, parse_carry, parse_client_uuid, parse_create, parse_evidence_files,
    parse_linked_order, parse_new_assignee, parse_proxy, parse_revision, person_name,
    order_link_choices, po_pi_record_choices, po_pi_record_label, pre_order_choices,
    scope_choices, vendor_choices,
)
from .approval_queries import AGING_WINDOW_DAYS, aging_rows
from .approvals import (
    ApprovalRefused, ProxyDecision, apply_approval_decision, create_approval_request,
    link_order_to_approval, reassign_approval_step, resubmit_approval_request,
    round_snapshot, unlink_order_from_approval, withdraw_approval_request,
)
from .decorators import _forbidden, login_required
from .models import (
    ApprovalAttachment, ApprovalOrderLink, ApprovalRequest, ApprovalStep, StatusTransition,
    VendorOrder,
    APPROVAL_APPROVED, APPROVAL_CHANGES_REQUESTED, APPROVAL_REJECTED,
    APPROVAL_KIND_CHOICES, APPROVAL_KIND_MATERIAL_PRE_DISPATCH,
    APPROVAL_KIND_MATERIAL_PRE_ORDER, APPROVAL_OPEN,
    APPROVAL_PARTY_CHOICES, APPROVAL_PARTY_DESIGN, APPROVAL_PROXY_CHANNEL_CHOICES,
    APPROVAL_STATUS_CHOICES, APPROVAL_STEP_APPROVED, APPROVAL_STEP_CHANGES_REQUESTED,
    APPROVAL_STEP_PENDING, APPROVAL_STEP_REJECTED, APPROVAL_STEP_SUPERSEDED,
    REASON_CREATED, REASON_RESUBMITTED, SUBJECT_APPROVAL_REQUEST,
)
from .order_views import _remove_uploaded
from .permissions import (
    approval_request_visibility_q, user_can_decide_approval_step,
    user_can_link_approval_order, user_can_raise_approval_request,
    user_can_reassign_approval_step, user_can_record_proxy_decision,
    user_can_resubmit_approval_request,
    user_can_view_approval_aging, user_can_view_approval_list,
    user_can_view_approval_request, user_can_view_vendor_order,
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

#: The kinds the raise page takes as ?kind= (Approvals 3a). No ?kind= at all is a
#: pre-order request, so every link and bookmark from before 3a still works.
_RAISE_KINDS = (APPROVAL_KIND_MATERIAL_PRE_ORDER, APPROVAL_KIND_MATERIAL_PRE_DISPATCH)

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


def _current_details(approval, material, boq_items, programs, projects, site_groups):
    """The request as it stands NOW, in round_snapshot()'s shape, for a round that has no
    snapshot (raised before S1.1). The page draws it under a label saying so — it is not
    what that round's approvers saw if anything was revised since."""
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
            'proposed_make': material.proposed_make,
            'specification': material.specification,
            'quantity_note': material.quantity_note,
            'boq_items': [{'id': i.pk, 'code': i.code, 'description': i.description}
                          for i in boq_items],
        },
    }


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
_CHANGE_MATERIAL_TEXT = (('proposed_make', 'Proposed make'),
                         ('specification', 'Specification'),
                         ('quantity_note', 'Quantity'))
_CHANGE_SCOPE_LISTS = (
    ('programs',    'Tenders',     lambda i: i['name']),
    ('projects',    'Sites',       lambda i: f"{i['project_id']} — {i['customer_name']}"),
    ('site_groups', 'Site groups', lambda i: f"{i.get('program_name') or '—'} · {i['name']}"),
)


def _describe_boq(item):
    return f"{item['code']} — {item['description']}"


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
    for a list. [] when nothing did. None when either snapshot is missing — the page then
    says the change list is unavailable, never guesses.

    Compared: title, description, the material's make, specification, quantity and BOQ
    items, the vendor (by id, shown by name), and the three scope lists. Not compared:
    the kind, design sign-off, the vendor order and pre-order link (none is revisable),
    and the steps — an approver change or a kept approval is in History and on the step.
    """
    if old is None or new is None:
        return None
    changes = []

    def single(label, before, after):
        if (before or '') != (after or ''):
            changes.append({'label': label, 'old': before or '—', 'new': after or '—'})

    for key, label in _CHANGE_REQUEST_TEXT:
        single(label, old.get(key), new.get(key))
    old_material, new_material = old.get('material') or {}, new.get('material') or {}
    for key, label in _CHANGE_MATERIAL_TEXT:
        single(label, old_material.get(key), new_material.get(key))
    boq = _list_change('BOQ items', old_material.get('boq_items'),
                       new_material.get('boq_items'), _describe_boq)
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
# Detail
# ---------------------------------------------------------------------------

_DECISION_VERBS = {
    APPROVAL_STEP_APPROVED:          'approved',
    APPROVAL_STEP_CHANGES_REQUESTED: 'requested changes',
    APPROVAL_STEP_REJECTED:          'rejected',
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
        event = _event(step.decided_at, (1, step.pk),
                       f'{party} {_DECISION_VERBS[step.verdict]} (round {step.round})',
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


def _step_row(user, step, approval, deciders, by_pk=None, evidence=()):
    """One step as the page draws it, with the action forms its predicates allow. A
    carried step is described as kept (_kept) and is never timed: it was activated and
    decided in the same instant by nobody's fresh act (approvals.exclude_carried_steps)."""
    step.request = approval   # the predicates read step.request; no query
    kept = _kept(step, by_pk or {})
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
        'evidence':     list(evidence),
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
    boq_items = list(material.boq_items.all()) if material else []
    programs = list(approval.programs.all())
    # Scope is for record; a soft-deleted site is not drawn (models.ApprovalRequest).
    projects = list(approval.projects.filter(is_deleted=False)
                    .only('pk', 'project_id', 'customer_name', 'is_deleted'))
    site_groups = list(approval.site_groups.all())
    current = _current_details(approval, material, boq_items, programs, projects,
                               site_groups)

    steps = list(approval.steps.all())
    by_pk = {s.pk: s for s in steps}
    attachments = [{'file': a, 'url': vendor_order_document_url(a)}
                   for a in approval.attachments.all()]
    snapshots = {n: round_snapshot(approval, n)
                 for n in range(1, approval.current_round + 1)}
    dispatch, round_dispatch = _dispatch_against(user, approval, material, snapshots)
    links = _order_links(approval)
    rounds = []
    for round_no in range(approval.current_round, 0, -1):     # newest first
        snapshot = snapshots[round_no]
        rounds.append({
            'number':       round_no,
            'is_current':   round_no == approval.current_round,
            'steps':        [_step_row(user, s, approval, deciders, by_pk,
                                       [a for a in attachments if a['file'].step_id == s.pk])
                             for s in steps if s.round == round_no],
            # A proxy's evidence is drawn under its step, not with the round's files.
            'attachments':  [a for a in attachments
                             if a['file'].round == round_no and a['file'].step_id is None],
            'details':      snapshot if snapshot is not None else current,
            'dispatch':     round_dispatch.get(round_no),
            'has_snapshot': snapshot is not None,
            'changes':      (round_changes(snapshots[round_no - 1], snapshot)
                             if round_no > 1 else None),
        })

    return render(request, 'projects/approvals/detail.html', {
        'approval':       approval,
        'status_badge':   _STATUS_BADGES.get(approval.status, 'text-bg-secondary'),
        'material':       material,
        'dispatch':       dispatch,
        'boq_items':      boq_items,
        'programs':       programs,
        'projects':       projects,
        'site_groups':    site_groups,
        'rounds':         rounds,
        'order_links':    _order_links_section(user, approval, links),
        'history':        _history(approval, steps, links),
        'evidence_accept': ','.join(f'.{ext}' for ext in EVIDENCE_EXTENSIONS),
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

    EVIDENCE FILES (D-A21, optional): PDFs and photos (EVIDENCE_EXTENSIONS), validated
    before any reaches storage, uploaded, then passed as ProxyDecision.files — the
    chokepoint writes their rows, linked to the step, inside its transaction. On any
    refusal or error the uploaded files are removed again (_remove_uploaded, via the
    upload helper's cleanup), so a refused proxy leaves neither a row nor a file.
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


def _resubmit_context(approval, material, latest, keepable, by_pk, post):
    """The resubmit form's context. Every value is pre-filled from the request as it
    stands, or — after a refusal — from what was posted, so nothing typed is lost."""
    if post is not None:
        values = {key: post.get(key, '') for key in
                  ('title', 'description', 'vendor', 'proposed_make', 'specification',
                   'quantity_note', 'note')}
        picked = {'program':    set(post.getlist('program')),
                  'project':    set(post.getlist('project')),
                  'site_group': set(post.getlist('site_group'))}
    else:
        values = {'title': approval.title, 'description': approval.description,
                  'vendor': str(approval.vendor_id or ''), 'note': '',
                  'proposed_make': material.proposed_make if material else '',
                  'specification': material.specification if material else '',
                  'quantity_note': material.quantity_note if material else ''}
        scope = current_scope_pks(approval)
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

    context = {
        'approval':         approval,
        'material':         material,
        # The vendor is the PO/PI record's (Approvals 3a): drawn read-only, never posted.
        'is_pre_dispatch':  approval.kind == APPROVAL_KIND_MATERIAL_PRE_DISPATCH,
        'boq_items':        list(material.boq_items.all()) if material else [],
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
    title, description, vendor, the three scope lists, proposed make, specification,
    quantity note (D-A18) — of which ONLY the changed ones are sent as `revision`
    (parse_revision); BOQ items and design sign-off are shown read-only. For each party
    whose step in the round that asked for changes is APPROVED, a "Keep <name>'s
    approval" box with a reason (D-A20; parse_carry holds the form rules). An approver
    override per party, as before. The note ("Describe what you changed") and new
    attachments.

    Access: user_can_view_approval_request AND user_can_raise_approval_request — SCM.
    Anyone else: 403. A request no longer waiting for changes: a message and back to the
    request. Calls resubmit_approval_request().

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
                      _resubmit_context(approval, material, latest, keepable, by_pk, post),
                      status=status)

    if request.method != 'POST':
        return form()

    errors = []
    files = parse_attachments(request, errors)
    overrides = parse_assignee_overrides(request.POST, holders, errors)
    revision = parse_revision(request.POST, approval, material, errors)
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
                                                carry=carry or None)
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
