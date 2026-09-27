"""
Approvals S1 — the shared approval primitive's chokepoint (26 Sep 2026).

THIS MODULE IS THE ONLY WRITER OF ApprovalRequest.status AND ApprovalStep.verdict.
Nothing else in the codebase assigns either field, saves either model with a changed
value, or updates either column in SQL. Every other column of a step (who decided, when,
who typed it, the proxy evidence, the supersede stamps) is written here too, and only
through filter().update() once the row exists. The same pattern as payments.py,
utils.assign_task_to() and utils.record_transition(): one function family owns one
table's writes, with no signals and no save() overrides.

The five entry points:

  create_approval_request()   the request, its material detail, its scope, its round-1
                              steps and attachments; sequence-1 steps activated at once
  apply_approval_decision()   one step's verdict, by the decider or by SCM on their behalf
  resubmit_approval_request() SCM's revision after "changes requested" — round N+1
  withdraw_approval_request() SCM closes a request that is open or awaiting changes
  reassign_approval_step()    SCM hands a pending step to someone else, same round

THE ORDER INSIDE EVERY WRITE IS FIXED (Layer 5 #1): take select_for_update() on the
REQUEST row first, then read any step. Two parallel approvers each lock the one request
row, so the second always reads the first's committed verdict. Locking only the step
rows would let each of them see the other as pending, and the request would never
reach Approved.

ROUNDS ARE NEVER REWRITTEN (Layer 5 #2). A resubmit creates new step rows; no previous
round's row is ever updated again once its round has closed.

EVERY ROUND IS SNAPSHOTTED (S1.1, D-A19). create and resubmit write one
ApprovalRoundSnapshot for the round they open, in the same transaction, after any
revision is applied: what that round's approvers were asked about, with display names
beside ids. A resubmit may revise the request's own fields (D-A18), so the live row is
the request NOW and the snapshot is the request AS ROUND N SAW IT. round_snapshot()
returns None for a round raised before S1.1.

A CARRIED STEP IS NOT A DECISION (D-A20). A resubmit may keep a party's approval from
the previous round; the new round's step is created approved with `carried_from` set.
Turnaround and aging figures must read steps through exclude_carried_steps().

NOTIFICATIONS (Approvals 2b) ARE REGISTERED HERE AND BUILT ELSEWHERE. Each entry point
registers exactly one transaction.on_commit(..., robust=True) callback from
approval_notices.py, inside its own atomic block, passing ids: a refused or rolled-back
action sends nothing, and a notice that fails after commit never undoes the action.
Who hears what, and the wording, live in approval_notices.py. record_transition() is
still called with its default notify=True: the only StatusTransition receiver
(signals.payment_transition_notices) returns at once for any subject other than
payment_request, so for this subject it sends and queues nothing.

Refusals raise ApprovalRefused with a message for the person who asked. Nothing is
written when a refusal is raised.
"""
from collections import namedtuple

from django.db import transaction
from django.utils import timezone

from .models import (
    ApprovalAttachment, ApprovalRequest, ApprovalRoundSnapshot, ApprovalStep,
    MaterialApprovalDetail,
    APPROVAL_KIND_CHOICES, APPROVAL_KIND_CONTRACTOR_BILL, APPROVAL_KIND_MATERIAL_PRE_DISPATCH,
    APPROVAL_KIND_MATERIAL_PRE_ORDER, APPROVAL_MATERIAL_KINDS,
    APPROVAL_OPEN, APPROVAL_CHANGES_REQUESTED, APPROVAL_APPROVED, APPROVAL_REJECTED,
    APPROVAL_WITHDRAWN, APPROVAL_TERMINAL_STATUSES,
    APPROVAL_PARTY_CHOICES, APPROVAL_PARTY_DESIGN, APPROVAL_PARTY_PM,
    APPROVAL_PARTY_SITE_ENGINEER,
    APPROVAL_STEP_PENDING, APPROVAL_STEP_APPROVED, APPROVAL_STEP_CHANGES_REQUESTED,
    APPROVAL_STEP_SUPERSEDED, APPROVAL_STEP_DECISIONS, APPROVAL_STEP_NOTE_REQUIRED,
    APPROVAL_PROXY_CHANNEL_CHOICES,
    REASON_CREATED, REASON_RESUBMITTED,
)
from .permissions import (
    profile_can_be_approval_assignee, user_can_decide_approval_step,
    user_can_raise_approval_request, user_can_reassign_approval_step,
    user_can_record_proxy_decision, user_can_withdraw_approval_request,
)
from .utils import record_transition
from . import approval_notices


class ApprovalRefused(Exception):
    """An approval write was refused. Nothing was written; str(exc) is the message for
    the person who asked. A refusal, never a 500 — the caller turns it into a message."""


#: A decision made outside PMS, recorded by SCM on the decider's behalf (D-A5).
#: `decided_by` is the UserProfile who decided; `channel` one of
#: APPROVAL_PROXY_CHANNEL_CHOICES; `evidence` what was said, and when. `files` (S1.1,
#: D-A21, optional) are evidence files the caller has ALREADY uploaded, each a dict of
#: file_name, bucket, path and optionally file_type, file_size_kb, label; the chokepoint
#: writes their ApprovalAttachment rows linked to the step. The text evidence stays
#: mandatory with or without files.
ProxyDecision = namedtuple('ProxyDecision', 'decided_by channel evidence files',
                           defaults=((),))

_KINDS = {value for value, _ in APPROVAL_KIND_CHOICES}
_KIND_LABELS = dict(APPROVAL_KIND_CHOICES)

#: The keys resubmit_approval_request(revision=...) accepts (D-A18). The request's own
#: fields, its scope, and the material detail's proposal. Not the kind, not design
#: sign-off (that changes who is asked), not the vendor order or pre-order link.
_REVISABLE_REQUEST = ('title', 'description', 'vendor')
_REVISABLE_SCOPE = ('programs', 'projects', 'site_groups')
_REVISABLE_MATERIAL = ('proposed_make', 'specification', 'quantity_note', 'boq_items')
_REVISABLE = _REVISABLE_REQUEST + _REVISABLE_SCOPE + _REVISABLE_MATERIAL
_PROXY_CHANNELS = {value for value, _ in APPROVAL_PROXY_CHANNEL_CHOICES}
_PARTY_LABELS = dict(APPROVAL_PARTY_CHOICES)


def _clean(text):
    return (text or '').strip()


def _name(profile):
    return profile.user.get_full_name() or profile.user.username


def _party_label(party):
    return _PARTY_LABELS.get(party, party)


def _sentence(text):
    """`text` ending in exactly one full stop — "kept" and "kept." both read "kept."
    in a ledger remark, never "kept..". A ? or ! already ends it."""
    return text if text[-1:] in ('.', '?', '!') else f'{text}.'


# ---------------------------------------------------------------------------
# The same-person rule — THE ONE COPY (Layer 5 #8)
# ---------------------------------------------------------------------------

def _same_person_refusal(raised_by_id, profile, party, others):
    """THE same-person rule. Returns the refusal message, or None.

    `profile` is about to act for `party` on one request — be named its assignee, or
    decide its step. Refused when:

      * `profile` raised the request. The raiser is never an approver and never a
        decider (the chokepoint compares DECIDED_BY, so SCM recording a WhatsApp answer
        as `recorded_by` on a request they raised is allowed — that is not deciding);
      * `profile` already acts for a DIFFERENT party on the same request: they decided
        another party's step in any round, or they are another party's live assignee in
        the round at hand. One person deciding for two parties is one decision with two
        clicks. The SAME party in a later round is not a conflict: the PM who asked for
        changes in round 1 decides round 2.

    `others` is an iterable of (party, profile_pk) pairs — see _other_parties(). Plain
    pairs rather than rows, so create and resubmit can pass the steps they are about to
    write alongside the ones already written.

    Not detectable here, and recorded as a known limit: one human holding two profiles
    (a PM account and a Design Head account) is two people as far as PMS knows.
    """
    if profile.pk == raised_by_id:
        return (f'{_name(profile)} raised this request and cannot also approve it. '
                f'Choose someone else for the {_party_label(party)}.')
    for other_party, other_pk in others:
        if other_party != party and other_pk == profile.pk:
            return (f'{_name(profile)} already acts for the {_party_label(other_party)} '
                    f'on this request, so they cannot also decide for the '
                    f'{_party_label(party)}.')
    return None


def _other_parties(approval, party, round_no):
    """The (party, profile_pk) pairs _same_person_refusal() compares against, read from
    the request's rows: every DECIDER of another party in any round, and every live
    (not superseded) ASSIGNEE of another party in `round_no`."""
    pairs = set()
    rows = (ApprovalStep.objects.filter(request=approval).exclude(party=party)
            .values_list('party', 'decided_by_id', 'assignee_id', 'verdict', 'round'))
    for row_party, decided_by_id, assignee_id, verdict, row_round in rows:
        if decided_by_id is not None:
            pairs.add((row_party, decided_by_id))
        if row_round == round_no and verdict != APPROVAL_STEP_SUPERSEDED:
            pairs.add((row_party, assignee_id))
    return pairs


# ---------------------------------------------------------------------------
# Shared pieces
# ---------------------------------------------------------------------------

def _plan(kind, design_signoff_required):
    """[(party, sequence)] for a round of `kind`. Material: the PM, plus the Design Head
    when SCM ticked design sign-off, all at sequence 1 — parallel (D-A7). Contractor
    bill: the Site Engineer confirms first, then the PM (D-A11)."""
    if kind == APPROVAL_KIND_CONTRACTOR_BILL:
        return [(APPROVAL_PARTY_SITE_ENGINEER, 1), (APPROVAL_PARTY_PM, 2)]
    plan = [(APPROVAL_PARTY_PM, 1)]
    if design_signoff_required:
        plan.append((APPROVAL_PARTY_DESIGN, 1))
    return plan


def _check_assignees(raised_by_id, assignees, others=()):
    """Refuse an ineligible assignee, the raiser, or one profile named for two parties.
    `assignees` is {party: profile}; `others` adds pairs already on the request."""
    for party, profile in assignees.items():
        if profile is None:
            raise ApprovalRefused(f'Choose the {_party_label(party)} who will approve this.')
        if not profile_can_be_approval_assignee(profile, party):
            raise ApprovalRefused(
                f'{_name(profile)} cannot be named as the {_party_label(party)} approver.')
    planned = {(party, profile.pk) for party, profile in assignees.items()}
    for party, profile in assignees.items():
        message = _same_person_refusal(raised_by_id, profile, party,
                                       planned | set(others))
        if message:
            raise ApprovalRefused(message)


def _open_round(approval, round_no, plan, assignees, now, carries=None, carried_by=None):
    """Write one round's steps. The lowest sequence with a step still to decide is
    activated now; the rest wait.

    `carries` ({party: (previous_step, reason)}, S1.1) are written already approved: the
    previous round's decider as `decided_by`, `carried_by` (the SCM user resubmitting)
    as `recorded_by`, activated and decided now. A carried step never holds up a later
    sequence — a contractor bill whose Site Engineer approval is kept goes straight to
    the PM."""
    carries = carries or {}
    first = min(sequence for party, sequence in plan if party not in carries)
    for party, sequence in plan:
        if party in carries:
            previous, reason = carries[party]
            ApprovalStep.objects.create(
                request=approval, round=round_no, party=party, sequence=sequence,
                assignee=assignees[party], activated_at=now,
                verdict=APPROVAL_STEP_APPROVED, decided_by_id=previous.decided_by_id,
                decided_at=now, recorded_by=carried_by,
                carried_from=previous, carry_reason=reason)
            continue
        ApprovalStep.objects.create(
            request=approval, round=round_no, party=party, sequence=sequence,
            assignee=assignees[party],
            activated_at=now if sequence == first else None)


def _add_attachments(approval, round_no, attachments, uploaded_by, step=None):
    """Append each file to `round_no`. `attachments` is an iterable of dicts carrying
    file_name, bucket, path and optionally file_type, file_size_kb, label — the upload
    itself is the caller's, done before this is called.

    With `step` (S1.1, D-A21) the files are evidence for that step's PROXY decision and
    are linked to it. Refused unless the step, as the database holds it now, is a proxy
    record of this request and round — evidence never attaches to a decision the decider
    typed themselves."""
    attachments = list(attachments)
    if step is not None and attachments:
        is_proxy_step = ApprovalStep.objects.filter(
            pk=step.pk, request=approval, round=round_no, is_proxy=True).exists()
        if not is_proxy_step:
            raise ApprovalRefused(
                'Evidence files are attached only to a decision recorded on someone '
                'else\'s behalf.')
    for item in attachments:
        ApprovalAttachment.objects.create(
            request=approval, round=round_no, step=step,
            label=_clean(item.get('label')),
            file_name=item['file_name'], bucket=item['bucket'], path=item['path'],
            file_type=item.get('file_type', ''),
            file_size_kb=item.get('file_size_kb', 0),
            uploaded_by=uploaded_by)


# ---------------------------------------------------------------------------
# Round snapshots and carried steps (S1.1)
# ---------------------------------------------------------------------------

def _origin_round(step):
    """The round in which a (possibly carried) approval was actually decided."""
    while step.carried_from_id is not None:
        step = step.carried_from
    return step.round


def _round_snapshot_payload(approval, round_no):
    """What round `round_no`'s approvers are asked about, read from the database as this
    transaction holds it. JSON-safe; display names stored beside ids (D-A19).

    Schema 1:
      schema, round, kind, kind_label, title, description, design_signoff_required,
      vendor {id, name} | null,
      programs [{id, name, short_tender_code}],
      projects [{id, project_id, customer_name, is_deleted}],
      site_groups [{id, name, program_id, program_name}],
      material null | {proposed_make, specification, quantity_note,
                       boq_items [{id, code, description, unit}],
                       vendor_order {id, po_number, pi_number} | null,
                       pre_order_request {id, title} | null},
      steps [{party, party_label, sequence, assignee_id, assignee_name, carried,
              carried_from_step, carried_from_round, decided_in_round,
              carried_decider_name}]
    Lists are in pk order; steps in (sequence, party) order, live rows only.
    """
    request = (ApprovalRequest.objects.select_related('vendor')
               .get(pk=approval.pk))
    material = (MaterialApprovalDetail.objects
                .select_related('vendor_order', 'pre_order_request')
                .filter(request=request).first())
    payload = {
        'schema': 1,
        'round': round_no,
        'kind': request.kind,
        'kind_label': _KIND_LABELS.get(request.kind, request.kind),
        'title': request.title,
        'description': request.description,
        'design_signoff_required': request.design_signoff_required,
        'vendor': ({'id': request.vendor.pk, 'name': request.vendor.name}
                   if request.vendor is not None else None),
        'programs': [{'id': p.pk, 'name': p.name, 'short_tender_code': p.short_tender_code}
                     for p in request.programs.order_by('pk')],
        'projects': [{'id': p.pk, 'project_id': p.project_id,
                      'customer_name': p.customer_name, 'is_deleted': p.is_deleted}
                     for p in request.projects.order_by('pk')],
        'site_groups': [{'id': g.pk, 'name': g.name, 'program_id': g.program_id,
                         'program_name': g.program.name if g.program_id else None}
                        for g in request.site_groups.select_related('program')
                        .order_by('pk')],
        'material': None,
        'steps': [],
    }
    if material is not None:
        order, pre_order = material.vendor_order, material.pre_order_request
        payload['material'] = {
            'proposed_make': material.proposed_make,
            'specification': material.specification,
            'quantity_note': material.quantity_note,
            'boq_items': [{'id': i.pk, 'code': i.code, 'description': i.description,
                           'unit': i.unit} for i in material.boq_items.order_by('pk')],
            'vendor_order': ({'id': order.pk, 'po_number': order.po_number,
                              'pi_number': order.pi_number}
                             if order is not None else None),
            'pre_order_request': ({'id': pre_order.pk, 'title': pre_order.title}
                                  if pre_order is not None else None),
        }
    steps = (ApprovalStep.objects.filter(request=request, round=round_no)
             .exclude(verdict=APPROVAL_STEP_SUPERSEDED)
             .select_related('assignee__user', 'decided_by__user', 'carried_from')
             .order_by('sequence', 'party', 'pk'))
    for row in steps:
        carried = row.carried_from_id is not None
        payload['steps'].append({
            'party': row.party,
            'party_label': _party_label(row.party),
            'sequence': row.sequence,
            'assignee_id': row.assignee_id,
            'assignee_name': _name(row.assignee),
            'carried': carried,
            'carried_from_step': row.carried_from_id,
            'carried_from_round': row.carried_from.round if carried else None,
            'decided_in_round': _origin_round(row) if carried else None,
            'carried_decider_name': _name(row.decided_by) if carried else None,
        })
    return payload


def _write_round_snapshot(approval, round_no, created_by):
    """The one writer of ApprovalRoundSnapshot. Called inside the caller's transaction,
    after the round's steps and any revision are written."""
    ApprovalRoundSnapshot.objects.create(
        request=approval, round=round_no, created_by=created_by,
        snapshot=_round_snapshot_payload(approval, round_no))


def round_snapshot(approval, round_no):
    """The snapshot dict of `approval`'s round `round_no`, or None when that round has
    none (a request raised before S1.1). Never raises for a missing round."""
    row = (ApprovalRoundSnapshot.objects.filter(request=approval, round=round_no)
           .only('snapshot').first())
    return row.snapshot if row is not None else None


def exclude_carried_steps(steps=None):
    """`steps` (an ApprovalStep queryset; all steps when omitted) without carried steps.

    A carried step is activated and decided at the same instant by nobody's fresh act,
    so it would read as a zero-second turnaround. Every turnaround and aging figure reads
    steps through this."""
    if steps is None:
        steps = ApprovalStep.objects.all()
    return steps.filter(carried_from__isnull=True)


def _lock(request_pk):
    """The request row, locked. EVERY write calls this before reading any step — by pk,
    so not even `step.request` is read before the lock is held."""
    return ApprovalRequest.objects.select_for_update().get(pk=request_pk)


def _locked_step(step, approval):
    """`step` re-read under the request lock, with the locked request attached so the
    permission predicates read the state this transaction holds."""
    fresh = ApprovalStep.objects.get(pk=step.pk)
    if fresh.request_id != approval.pk:
        raise ApprovalRefused('That step does not belong to this request.')
    fresh.request = approval
    return fresh


def _step_state_refusal(step, approval):
    """Why nobody may act on `step` now, or None."""
    if approval.status != APPROVAL_OPEN:
        return (f'This request is {approval.get_status_display().lower()}; '
                f'nothing more can be decided on it.')
    if step.round != approval.current_round:
        return 'That step belongs to an earlier round.'
    if step.verdict != APPROVAL_STEP_PENDING:
        return (f'The {_party_label(step.party)} step is already '
                f'{step.get_verdict_display().lower()}; nothing was recorded.')
    if step.activated_at is None:
        return (f'The {_party_label(step.party)} step is not due yet — an earlier step '
                f'in this round has still to be approved.')
    return None


# ---------------------------------------------------------------------------
# 1. Create
# ---------------------------------------------------------------------------

def create_approval_request(*, kind, raised_by, title, description, pm_assignee,
                            design_assignee=None, site_engineer_assignee=None,
                            design_signoff_required=False, vendor=None, material=None,
                            programs=(), projects=(), site_groups=(), attachments=(),
                            client_uuid=None):
    """Raise a request and open round 1. Returns the ApprovalRequest.

    `material` is a dict for the two material kinds (proposed_make, specification,
    quantity_note, boq_items, vendor_order, pre_order_request) and must be None for a
    contractor bill. Kind-specific rules enforced here:

      * a vendor for pre-dispatch and the contractor bill (a CHECK holds it as well);
      * a vendor order (PO/PI record) for pre-dispatch, placed with that same vendor, and
        none on a pre-order request;
      * a linked pre-order approval only on pre-dispatch, only of the pre-order kind, only
        once APPROVED, and only for the same vendor as the request (Approvals 3a);
      * no design sign-off on a contractor bill (a CHECK holds it as well);
      * a Design Head named exactly when design sign-off is ticked, a Site Engineer
        exactly for a contractor bill.

    CONTRACTOR BILLS: S1 builds the sequencing only. Session 4 adds ContractorBillDetail
    and its one required project; until then nothing calls this with that kind.

    Idempotent on `client_uuid` (R-14): a repeat returns the request already written.
    """
    if client_uuid:
        existing = ApprovalRequest.objects.filter(client_uuid=client_uuid).first()
        if existing is not None:
            return existing

    if kind not in _KINDS:
        raise ApprovalRefused('Choose what kind of approval this is.')
    if not user_can_raise_approval_request(raised_by.user):
        raise ApprovalRefused('Only SCM can raise an approval request.')
    title, description = _clean(title), _clean(description)
    if not title or not description:
        raise ApprovalRefused('A title and a description are required.')
    if len(title) > 200:
        raise ApprovalRefused('The title must be 200 characters or fewer.')

    is_bill = kind == APPROVAL_KIND_CONTRACTOR_BILL
    if kind in (APPROVAL_KIND_MATERIAL_PRE_DISPATCH, APPROVAL_KIND_CONTRACTOR_BILL) \
            and vendor is None:
        raise ApprovalRefused('Choose the vendor this request is about.')
    if is_bill and design_signoff_required:
        raise ApprovalRefused('A contractor bill does not take design sign-off.')
    if design_signoff_required and design_assignee is None:
        raise ApprovalRefused('Choose the Design Head who will sign off.')
    if not design_signoff_required and design_assignee is not None:
        raise ApprovalRefused('A Design Head is named only when design sign-off is ticked.')
    if is_bill and site_engineer_assignee is None:
        raise ApprovalRefused('Choose the Site Engineer who will confirm the work.')
    if not is_bill and site_engineer_assignee is not None:
        raise ApprovalRefused('Only a contractor bill is confirmed by a Site Engineer.')

    detail = None
    if kind in APPROVAL_MATERIAL_KINDS:
        detail = dict(material or {})
        vendor_order = detail.get('vendor_order')
        pre_order = detail.get('pre_order_request')
        if kind == APPROVAL_KIND_MATERIAL_PRE_DISPATCH:
            if vendor_order is None:
                raise ApprovalRefused('A pre-dispatch approval names the PO/PI record it ships against.')
            if vendor_order.vendor_id != vendor.pk:
                raise ApprovalRefused('The PO/PI record was placed with a different vendor.')
        elif vendor_order is not None:
            raise ApprovalRefused('A pre-order approval comes before the order, so it names '
                                  'no PO/PI record.')
        if pre_order is not None:
            if kind != APPROVAL_KIND_MATERIAL_PRE_DISPATCH:
                raise ApprovalRefused('Only a pre-dispatch approval links a pre-order approval.')
            if pre_order.kind != APPROVAL_KIND_MATERIAL_PRE_ORDER:
                raise ApprovalRefused('The linked approval is not a pre-order approval.')
            if pre_order.status != APPROVAL_APPROVED:
                raise ApprovalRefused('The linked pre-order approval has not been approved.')
            if pre_order.vendor_id != vendor.pk:
                raise ApprovalRefused('The linked pre-order approval names a different vendor.')
    elif material:
        raise ApprovalRefused('A contractor bill carries no material detail.')

    projects = list(projects)
    if any(project.is_deleted for project in projects):
        raise ApprovalRefused('A deleted project cannot be named in the scope.')

    plan = _plan(kind, design_signoff_required)
    supplied = {APPROVAL_PARTY_PM: pm_assignee,
                APPROVAL_PARTY_DESIGN: design_assignee,
                APPROVAL_PARTY_SITE_ENGINEER: site_engineer_assignee}
    assignees = {party: supplied[party] for party, _ in plan}
    _check_assignees(raised_by.pk, assignees)

    with transaction.atomic():
        approval = ApprovalRequest.objects.create(
            kind=kind, title=title, description=description, vendor=vendor,
            design_signoff_required=design_signoff_required, raised_by=raised_by,
            client_uuid=client_uuid or None)
        approval.programs.set(programs)
        approval.projects.set(projects)
        approval.site_groups.set(site_groups)

        if detail is not None:
            material_row = MaterialApprovalDetail.objects.create(
                request=approval,
                proposed_make=_clean(detail.get('proposed_make')),
                specification=_clean(detail.get('specification')),
                quantity_note=_clean(detail.get('quantity_note')),
                vendor_order=detail.get('vendor_order'),
                pre_order_request=detail.get('pre_order_request'))
            material_row.boq_items.set(detail.get('boq_items') or ())

        now = timezone.now()
        _open_round(approval, 1, plan, assignees, now)
        _add_attachments(approval, 1, attachments, raised_by)
        _write_round_snapshot(approval, 1, raised_by)
        record_transition(approval, to_status=APPROVAL_OPEN, actor=raised_by,
                          reason_code=REASON_CREATED)
        transaction.on_commit(
            lambda: approval_notices.after_create(approval.pk, raised_by.pk),
            robust=True)
    return approval


# ---------------------------------------------------------------------------
# 2. Decide
# ---------------------------------------------------------------------------

def apply_approval_decision(step, verdict, actor, note='', proxy=None):
    """Record one verdict on `step`. Returns the request, re-read.

    `actor` is who is typing it into PMS (`recorded_by`). Without `proxy` the actor is
    also the decider. With `proxy` (a ProxyDecision) the actor must be SCM, and
    `proxy.decided_by` is who decided, over `proxy.channel`, with `proxy.evidence`.
    `proxy.files` (already uploaded by the caller) become ApprovalAttachment rows linked
    to the step, uploaded_by the actor, written after the step inside this transaction —
    so a refused decision leaves no attachment row.

    Under the request lock, in this order: re-read the step; refuse a step that is not
    live; refuse a recorder or decider without the authority; apply the same-person
    rule to the DECIDER; write the step; then

      * a non-approval closes the round at once (fail-fast): every other pending step
        of the round is superseded, and the request moves to changes_requested or
        rejected;
      * an approval that completes its sequence activates the next sequence; one that
        completes the round approves the request.

    Every request status move writes one ledger row, remark = the note, actor = the
    decider (whose act moved it; the step row says who typed it).
    """
    if verdict not in APPROVAL_STEP_DECISIONS:
        raise ApprovalRefused('Choose approve, request changes, or reject.')
    note = _clean(note)
    if verdict in APPROVAL_STEP_NOTE_REQUIRED and not note:
        raise ApprovalRefused('Say what has to change, or why it is rejected.')
    if proxy is not None:
        if proxy.decided_by is None:
            raise ApprovalRefused('Name the person whose decision you are recording.')
        if proxy.channel not in _PROXY_CHANNELS:
            raise ApprovalRefused('Say how the decision reached you.')
        if not _clean(proxy.evidence):
            raise ApprovalRefused('Record what was said, and when, as evidence.')
        for item in proxy.files or ():
            if not all(_clean(item.get(key)) for key in ('file_name', 'bucket', 'path')):
                raise ApprovalRefused('An evidence file is missing its name or where it '
                                      'was stored.')
    decider = proxy.decided_by if proxy is not None else actor

    with transaction.atomic():
        approval = _lock(step.request_id)
        step = _locked_step(step, approval)

        message = _step_state_refusal(step, approval)
        if message:
            raise ApprovalRefused(message)
        if proxy is not None and not user_can_record_proxy_decision(actor.user, step):
            raise ApprovalRefused('Only SCM can record a decision on someone else\'s behalf.')
        if not user_can_decide_approval_step(decider.user, step):
            raise ApprovalRefused(
                f'{_name(decider)} cannot decide the {_party_label(step.party)} step.')
        message = _same_person_refusal(
            approval.raised_by_id, decider, step.party,
            _other_parties(approval, step.party, approval.current_round))
        if message:
            raise ApprovalRefused(message)

        now = timezone.now()
        # approval_notices matches on this timestamp equality; stamp both from the same `now`.
        written = ApprovalStep.objects.filter(
            pk=step.pk, verdict=APPROVAL_STEP_PENDING,
        ).update(
            verdict=verdict, note=note, decided_by=decider, decided_at=now,
            recorded_by=actor, is_proxy=proxy is not None,
            proxy_channel=proxy.channel if proxy is not None else '',
            proxy_evidence=_clean(proxy.evidence) if proxy is not None else '')
        if written != 1:   # impossible under the lock; never silently carry on
            raise ApprovalRefused('The step changed while you were deciding it.')
        if proxy is not None:
            _add_attachments(approval, step.round, proxy.files or (), actor, step=step)

        round_filter = dict(request=approval, round=approval.current_round)
        if verdict in APPROVAL_STEP_NOTE_REQUIRED:
            # approval_notices matches on this timestamp equality; stamp both from the same `now`.
            ApprovalStep.objects.filter(
                verdict=APPROVAL_STEP_PENDING, **round_filter,
            ).update(verdict=APPROVAL_STEP_SUPERSEDED, superseded_at=now,
                     superseded_by=decider)
            new_status = (APPROVAL_CHANGES_REQUESTED
                          if verdict == APPROVAL_STEP_CHANGES_REQUESTED
                          else APPROVAL_REJECTED)
        else:
            live = list(ApprovalStep.objects.filter(**round_filter)
                        .exclude(verdict=APPROVAL_STEP_SUPERSEDED)
                        .values_list('sequence', 'verdict'))
            if all(v == APPROVAL_STEP_APPROVED for _, v in live):
                new_status = APPROVAL_APPROVED
            else:
                new_status = APPROVAL_OPEN
                done = all(v == APPROVAL_STEP_APPROVED
                           for s, v in live if s == step.sequence)
                later = [s for s, _ in live if s > step.sequence]
                if done and later:
                    # approval_notices matches on this timestamp equality; stamp both from the same `now`.
                    ApprovalStep.objects.filter(
                        sequence=min(later), verdict=APPROVAL_STEP_PENDING,
                        activated_at__isnull=True, **round_filter,
                    ).update(activated_at=now)

        if new_status != APPROVAL_OPEN:
            ApprovalRequest.objects.filter(pk=approval.pk, status=APPROVAL_OPEN).update(
                status=new_status,
                closed_at=now if new_status in APPROVAL_TERMINAL_STATUSES else None)
            record_transition(approval, to_status=new_status, from_status=APPROVAL_OPEN,
                              actor=decider, remark=note)
        transaction.on_commit(
            lambda: approval_notices.after_decision(
                step.pk, actor.pk, new_status if new_status != APPROVAL_OPEN else None),
            robust=True)

    approval.refresh_from_db()
    return approval


# ---------------------------------------------------------------------------
# 3. Resubmit
# ---------------------------------------------------------------------------

def _revision_writes(approval, revision):
    """Validate `revision` against `approval` (locked) and return what to write:
    (request_fields, scope, material_fields, boq_items). The kind's rules are re-checked
    against the request AS REVISED. Refuses an unknown key, a material key on a request
    with no material detail, a blank title or description, a missing or mismatched
    vendor, or a deleted project in the scope. Writes nothing."""
    revision = dict(revision or {})
    for key in revision:
        if key not in _REVISABLE:
            raise ApprovalRefused(f'"{key}" cannot be changed on a resubmit.')
    material = (MaterialApprovalDetail.objects.select_related('vendor_order')
                .filter(request=approval).first())
    if material is None and any(key in revision for key in _REVISABLE_MATERIAL):
        raise ApprovalRefused('A contractor bill carries no material detail.')

    request_fields = {}
    for key in ('title', 'description'):
        if key in revision:
            request_fields[key] = _clean(revision[key])
    if 'vendor' in revision:
        request_fields['vendor'] = revision['vendor']
    title = request_fields.get('title', approval.title)
    description = request_fields.get('description', approval.description)
    vendor = request_fields.get('vendor', approval.vendor)
    if not title or not description:
        raise ApprovalRefused('A title and a description are required.')
    if len(title) > 200:
        raise ApprovalRefused('The title must be 200 characters or fewer.')
    if approval.kind in (APPROVAL_KIND_MATERIAL_PRE_DISPATCH, APPROVAL_KIND_CONTRACTOR_BILL) \
            and vendor is None:
        raise ApprovalRefused('Choose the vendor this request is about.')
    if approval.kind == APPROVAL_KIND_MATERIAL_PRE_DISPATCH and material is not None \
            and material.vendor_order is not None \
            and material.vendor_order.vendor_id != vendor.pk:
        raise ApprovalRefused('The vendor order was placed with a different vendor.')

    scope = {key: list(revision[key]) for key in _REVISABLE_SCOPE if key in revision}
    if any(project.is_deleted for project in scope.get('projects', ())):
        raise ApprovalRefused('A deleted project cannot be named in the scope.')

    material_fields = {key: _clean(revision[key])
                       for key in ('proposed_make', 'specification', 'quantity_note')
                       if key in revision}
    if len(material_fields.get('proposed_make', '')) > 200:
        raise ApprovalRefused('The proposed make must be 200 characters or fewer.')
    boq_items = list(revision['boq_items']) if 'boq_items' in revision else None
    return request_fields, scope, material_fields, boq_items


def resubmit_approval_request(approval, actor, note, attachments=(), assignees=None,
                              revision=None, carry=None):
    """SCM's revision after changes were requested: open round N+1. Returns the request.

    New step rows for the same parties, same sequences; the previous round's rows are
    not touched. Each party goes to whoever held it last in the previous round, unless
    `assignees` ({party: profile}) names someone else — the way round N+1 reaches a
    replacement when the round-N assignee has left, since a closed round's step cannot
    be reassigned.

    Only from changes_requested. Open is still being decided; approved, rejected and
    withdrawn are terminal (SCM raises a fresh request). Any SCM user may resubmit any
    request (D-A22); the ledger row and the round snapshot name who did.

    AN OVERRIDE THAT CHANGES AN APPROVER IS A REASSIGNMENT, and is held to that bar:

      * the note is mandatory — as it is for every resubmit, so the one check below
        covers it;
      * the ledger remark states the change before the note, one segment per changed
        party in round order: "Approver changed — pm: <old> → <new>. <note>";
      * it needs user_can_reassign_approval_step(). That predicate refuses any step that
        is not pending in an open request's current round, so it cannot be asked of the
        previous round's closed step. It is asked of the step actually being handed over
        — the new round's row for that party — right after that row is written, inside
        this transaction, so a refusal rolls the whole resubmit back.

    Naming the same person again is not a change: no segment, no reassign check.

    REVISION (S1.1, D-A18). `revision` is a dict of any of title, description, vendor,
    programs, projects, site_groups, proposed_make, specification, quantity_note,
    boq_items. Omitted keys stay as they are; a scope or boq_items key replaces that
    whole set. The kind's rules are re-checked against the revised request
    (_revision_writes). Kind, design sign-off, vendor order and pre-order link are not
    revisable.

    CARRY-FORWARD (S1.1, D-A20). `carry` is {party: reason}: keep that party's APPROVAL
    from the previous round instead of asking again. Refused without a reason; refused
    if it would leave nobody to decide the new round (a resubmit never opens a round
    that is already approved); refused unless the party's step in the immediately
    previous round is approved — superseded, changes-requested and rejected steps are
    not approvals; refused for a party whose approver this resubmit changes. Any SCM
    user may carry (D-A22) — the same user_can_raise_approval_request() check as the
    resubmit itself. The new round's step is created approved: decided_by the original
    decider, recorded_by the actor, activated and decided now, carried_from the previous
    round's step. The ledger remark states each carry after any approver change and
    before the note, in round order:
    "Approval kept — pm: <decider> (round N). Reason: <reason>. " — N being the round the
    approval was actually decided in, which is earlier than the previous round when a
    carried approval is carried again. A reason that already ends in a full stop is not
    given a second one (_sentence).

    Every write, including the new round's snapshot, is inside one transaction: a
    refusal leaves nothing behind.
    """
    note = _clean(note)
    if not note:
        raise ApprovalRefused('Say what changed in this revision.')
    if not user_can_raise_approval_request(actor.user):
        raise ApprovalRefused('Only SCM can resubmit an approval request.')
    carry_reasons = {party: _clean(reason) for party, reason in (carry or {}).items()}
    for party, reason in carry_reasons.items():
        if not reason:
            raise ApprovalRefused(
                f'Say why the {_party_label(party)} approval is being kept.')

    with transaction.atomic():
        approval = _lock(approval.pk)
        if approval.status != APPROVAL_CHANGES_REQUESTED:
            if approval.status in APPROVAL_TERMINAL_STATUSES:
                raise ApprovalRefused(
                    f'This request is {approval.get_status_display().lower()}, which is '
                    f'final. Raise a new request instead.')
            raise ApprovalRefused('This request is still being decided; nothing to resubmit.')

        request_fields, scope, material_fields, boq_items = _revision_writes(
            approval, revision)

        previous = approval.current_round
        latest = {}
        for row in (ApprovalStep.objects.filter(request=approval, round=previous)
                    .select_related('assignee__user', 'decided_by__user', 'carried_from')
                    .order_by('pk')):
            latest[row.party] = row      # the last row per party wins
        plan = sorted({(row.party, row.sequence) for row in latest.values()},
                      key=lambda pair: (pair[1], pair[0]))
        chosen = {party: latest[party].assignee for party, _ in plan}
        for party, profile in (assignees or {}).items():
            if party not in chosen:
                raise ApprovalRefused(
                    f'This request has no {_party_label(party)} step to assign.')
            chosen[party] = profile
        changed = [party for party, _ in plan
                   if chosen[party] is not None
                   and chosen[party].pk != latest[party].assignee_id]

        # Carries: the whole-round guard first, so "keep everything" is always answered
        # by it, then each party on its own.
        if carry_reasons and all(party in carry_reasons for party, _ in plan):
            raise ApprovalRefused(
                'At least one approver must decide the new round — a resubmit cannot '
                'keep every approval.')
        carries = {}
        for party, reason in carry_reasons.items():
            if party not in latest:
                raise ApprovalRefused(
                    f'This request has no {_party_label(party)} step to keep.')
            kept = latest[party]
            if kept.verdict != APPROVAL_STEP_APPROVED:
                raise ApprovalRefused(
                    f'The {_party_label(party)} step in round {previous} was '
                    f'{kept.get_verdict_display().lower()}, not approved; only an '
                    f'approval can be kept.')
            if party in changed:
                raise ApprovalRefused(
                    f'The {_party_label(party)} approval cannot be kept while its '
                    f'approver is being changed.')
            carries[party] = (kept, reason)

        # Every decider in every earlier round, by party. The new round has no rows yet,
        # so there are no live assignees to add; the planned ones are _check_assignees'.
        deciders = set(ApprovalStep.objects.filter(
            request=approval, decided_by__isnull=False,
        ).values_list('party', 'decided_by_id'))
        _check_assignees(approval.raised_by_id, chosen, deciders)

        new_round = previous + 1
        now = timezone.now()
        ApprovalRequest.objects.filter(
            pk=approval.pk, status=APPROVAL_CHANGES_REQUESTED,
        ).update(status=APPROVAL_OPEN, current_round=new_round, **request_fields)
        for key, rows in scope.items():
            getattr(approval, key).set(rows)
        if material_fields or boq_items is not None:
            material = MaterialApprovalDetail.objects.get(request=approval)
            if material_fields:
                MaterialApprovalDetail.objects.filter(pk=material.pk).update(
                    **material_fields)
            if boq_items is not None:
                material.boq_items.set(boq_items)
        _open_round(approval, new_round, plan, chosen, now,
                    carries=carries, carried_by=actor)

        changed_segments = kept_segments = ''
        if changed:
            reopened = _lock(approval.pk)     # already held; re-read for the new state
            for party in changed:
                handed_over = ApprovalStep.objects.get(
                    request=approval, round=new_round, party=party)
                handed_over.request = reopened
                if not user_can_reassign_approval_step(actor.user, handed_over):
                    raise ApprovalRefused(
                        f'Only someone who may reassign approval steps can change the '
                        f'{_party_label(party)} approver.')
            changed_segments = ''.join(
                f'Approver changed — {party}: {_name(latest[party].assignee)} → '
                f'{_name(chosen[party])}. ' for party in changed)
        if carries:
            kept_segments = ''.join(
                f'Approval kept — {party}: {_name(carries[party][0].decided_by)} '
                f'(round {_origin_round(carries[party][0])}). '
                f'Reason: {_sentence(carries[party][1])} '
                for party, _ in plan if party in carries)
        remark = changed_segments + kept_segments + note

        _add_attachments(approval, new_round, attachments, actor)
        _write_round_snapshot(approval, new_round, actor)
        record_transition(approval, to_status=APPROVAL_OPEN,
                          from_status=APPROVAL_CHANGES_REQUESTED, actor=actor,
                          reason_code=REASON_RESUBMITTED, remark=remark)
        transaction.on_commit(
            lambda: approval_notices.after_resubmit(
                approval.pk, new_round, actor.pk, [latest[party].pk for party in changed]),
            robust=True)

    approval.refresh_from_db()
    return approval


# ---------------------------------------------------------------------------
# 4. Withdraw
# ---------------------------------------------------------------------------

def withdraw_approval_request(approval, actor, note):
    """SCM withdraws `approval`, with a note, while it is open or awaiting changes.
    Returns the request.

    Every pending step of the current round is superseded, so nobody is left in the
    aging list waiting on a request that no longer exists. No same-person rule applies:
    withdrawing decides nothing on anyone's behalf, and the raiser withdrawing their own
    request is the ordinary case.
    """
    note = _clean(note)
    if not note:
        raise ApprovalRefused('Say why this request is being withdrawn.')

    with transaction.atomic():
        approval = _lock(approval.pk)
        if approval.status in APPROVAL_TERMINAL_STATUSES:
            raise ApprovalRefused(
                f'This request is already {approval.get_status_display().lower()}.')
        if not user_can_withdraw_approval_request(actor.user, approval):
            raise ApprovalRefused('Only SCM can withdraw an approval request.')

        now = timezone.now()
        from_status = approval.status
        # approval_notices matches on this timestamp equality; stamp both from the same `now`.
        ApprovalStep.objects.filter(
            request=approval, round=approval.current_round,
            verdict=APPROVAL_STEP_PENDING,
        ).update(verdict=APPROVAL_STEP_SUPERSEDED, superseded_at=now, superseded_by=actor)
        # approval_notices matches on this timestamp equality; stamp both from the same `now`.
        ApprovalRequest.objects.filter(pk=approval.pk, status=from_status).update(
            status=APPROVAL_WITHDRAWN, closed_at=now, withdrawal_note=note)
        record_transition(approval, to_status=APPROVAL_WITHDRAWN, from_status=from_status,
                          actor=actor, remark=note)
        transaction.on_commit(
            lambda: approval_notices.after_withdraw(approval.pk, actor.pk),
            robust=True)

    approval.refresh_from_db()
    return approval


# ---------------------------------------------------------------------------
# 5. Reassign
# ---------------------------------------------------------------------------

def reassign_approval_step(step, new_assignee, actor, note):
    """SCM hands a pending step to `new_assignee`, with a note. Returns the NEW step.

    The old row is superseded — its `note` becomes SCM's reason and `superseded_by` SCM
    — and a new row is created for the same party, round and sequence. The new row is
    activated now if the old one was active (so the new assignee's turnaround starts
    when they were asked, not when their predecessor was), and left waiting if not.

    The request's status does not move, so no ledger row is written.
    """
    note = _clean(note)
    if not note:
        raise ApprovalRefused('Say why this step is being reassigned.')

    with transaction.atomic():
        approval = _lock(step.request_id)
        step = _locked_step(step, approval)
        if not user_can_reassign_approval_step(actor.user, step):
            if step.verdict != APPROVAL_STEP_PENDING or approval.status != APPROVAL_OPEN:
                raise ApprovalRefused('Only a pending step of an open request can be reassigned.')
            raise ApprovalRefused('Only SCM can reassign an approval step.')
        if new_assignee is None or new_assignee.pk == step.assignee_id:
            raise ApprovalRefused('Choose someone other than the current assignee.')
        _check_assignees(approval.raised_by_id, {step.party: new_assignee},
                         _other_parties(approval, step.party, approval.current_round))

        now = timezone.now()
        ApprovalStep.objects.filter(pk=step.pk, verdict=APPROVAL_STEP_PENDING).update(
            verdict=APPROVAL_STEP_SUPERSEDED, superseded_at=now, superseded_by=actor,
            note=note)
        replacement = ApprovalStep.objects.create(
            request=approval, round=step.round, party=step.party, sequence=step.sequence,
            assignee=new_assignee,
            activated_at=now if step.activated_at is not None else None)
        transaction.on_commit(
            lambda: approval_notices.after_reassign(step.pk, replacement.pk, actor.pk),
            robust=True)
    return replacement
