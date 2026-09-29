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

A MATERIAL REQUEST IS ITS LINES (28 Sep 2026, D-A28 .. D-A30, D-A43). create and resubmit
take `lines`, each a description, make, specification, quantity and unit, and this module
is their only writer. The unit decides the quantity: a whole number for a count unit, up
to two places for a measured one (projects/units.py), read by number_input's
parse_decimal_input() and refused — never rounded — otherwise; three CHECKs on
MaterialApprovalLine hold the same rules. A resubmit replaces the set: lines may be added,
edited or removed. The request-wide make, specification and quantity note that lines
replaced stay on older requests, read-only; the BOQ-item picks are gone.

A CONTRACTOR BILL IS ITS DETAIL (4a-1, D-A10, D-A31 .. D-A41). create takes `bill`, a
ContractorBill: one site, the tasks it covers on that site, the amount as billed, the
contractor's own bill number and date, and the PDF already stored in the private bills
bucket (bill_storage.py). This module writes its ContractorBillDetail and task links,
and sets the request's record-only scope to that one site. What SCM is only WARNED about
— an unfinished task, a task on another bill, a repeated bill number, a Site Engineer or
PM not on the site — is bill_rules.py's, and never refuses anything here. Nothing here
checks the amount against a rate (B-16). A resubmit (4b-1) may revise the amount, number,
date, tasks and PDF, never the contractor or the site; a replaced PDF stays recorded in
its round's snapshot and is never deleted.

THE SITE ENGINEER CONFIRMS THE WORK, NEVER THE BILL (4a-3, D-A53, D-A54). A Site Engineer
step is approved only with at least one site photo, linked to the step, and never
rejected; "work not done" is a changes request. apply_approval_decision holds the rules
(_site_engineer_refusal), so no screen can go around them.

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

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F, Max, Q
from django.utils import timezone

from .models import (
    ApprovalAttachment, ApprovalOrderLink, ApprovalRequest, ApprovalRoundSnapshot,
    ApprovalStep, ContractorBillDetail, ContractorBillTask, MaterialApprovalDetail,
    MaterialApprovalLine, Task,
    APPROVAL_KIND_CHOICES, APPROVAL_KIND_CONTRACTOR_BILL, APPROVAL_KIND_MATERIAL_PRE_DISPATCH,
    APPROVAL_KIND_MATERIAL_PRE_ORDER, APPROVAL_MATERIAL_KINDS,
    APPROVAL_OPEN, APPROVAL_CHANGES_REQUESTED, APPROVAL_APPROVED, APPROVAL_REJECTED,
    APPROVAL_WITHDRAWN, APPROVAL_TERMINAL_STATUSES,
    APPROVAL_PARTY_CHOICES, APPROVAL_PARTY_DESIGN, APPROVAL_PARTY_PM,
    APPROVAL_PARTY_SITE_ENGINEER,
    APPROVAL_STEP_PENDING, APPROVAL_STEP_APPROVED, APPROVAL_STEP_CHANGES_REQUESTED,
    APPROVAL_STEP_REJECTED, APPROVAL_STEP_SUPERSEDED, APPROVAL_STEP_DECISIONS,
    APPROVAL_STEP_NOTE_REQUIRED,
    APPROVAL_PROXY_CHANNEL_CHOICES,
    REASON_CREATED, REASON_RESUBMITTED, VENDOR_BILLABLE_KINDS,
)
from .bill_storage import bills_bucket
# forms.py is imported for check_typed_date(), the one typed-date range rule (2020 to
# today + 5 years); forms imports models, utils and permissions, never this module.
from .forms import check_typed_date
from .permissions import (
    profile_can_be_approval_assignee, user_can_decide_approval_step,
    user_can_raise_approval_request, user_can_reassign_approval_step,
    user_can_record_proxy_decision, user_can_withdraw_approval_request,
)
from .number_input import decimal_field_max, parse_decimal_input
from .units import COUNT_UNITS, format_quantity, unit_places
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

#: A contractor bill's own details (4a-1), given to create_approval_request(bill=...).
#: `project` the one site (D-A10); `tasks` the Tasks on it the bill covers, at least one;
#: `amount` as billed (a Decimal or the typed string); `bill_number` and `bill_date` as
#: printed on the contractor's bill (D-A41; the date a date or an ISO string); `pdf` the
#: bill PDF ALREADY stored by bill_storage.upload_bill_pdf() — a dict of file_name,
#: bucket, path and optionally file_size_kb (D-A39, D-A40).
ContractorBill = namedtuple('ContractorBill',
                            'project tasks amount bill_number bill_date pdf')

_KINDS = {value for value, _ in APPROVAL_KIND_CHOICES}
_KIND_LABELS = dict(APPROVAL_KIND_CHOICES)

#: The keys resubmit_approval_request(revision=...) accepts (D-A18): the request's own
#: fields and its scope. Not the kind, not design sign-off (that changes who is asked),
#: not the vendor order or pre-order link. The material is revised through the `lines`
#: argument instead, as one set (material lines); the request-wide make, specification,
#: quantity note and BOQ items it replaced are no longer written by anything.
_REVISABLE_REQUEST = ('title', 'description', 'vendor')
_REVISABLE_SCOPE = ('programs', 'projects', 'site_groups')
#: A contractor bill's own revisable fields (4b-1): `tasks` replaces the set, `pdf` is a
#: NEW file already stored by bill_storage.upload_bill_pdf(). A bill's contractor and site
#: are locked — `vendor` and the scope keys are refused on a bill unless they repeat what
#: it already holds (_revision_writes).
_REVISABLE_BILL = ('amount', 'bill_number', 'bill_date', 'tasks', 'pdf')
_REVISABLE = _REVISABLE_REQUEST + _REVISABLE_SCOPE + _REVISABLE_BILL

#: What a material line is given as, in create_approval_request(lines=...) and
#: resubmit_approval_request(lines=...). `id` only on a resubmit, naming a live line.
_LINE_FIELDS = ('id', 'description', 'make', 'specification', 'quantity', 'unit')
#: The request-wide material fields lines replaced. Refused if a caller still sends one,
#: so nothing writes them again (they stay on older requests, read-only).
_LEGACY_MATERIAL = ('proposed_make', 'specification', 'quantity_note', 'boq_items')
_PROXY_CHANNELS = {value for value, _ in APPROVAL_PROXY_CHANNEL_CHOICES}
_PARTY_LABELS = dict(APPROVAL_PARTY_CHOICES)

#: What counts as a site photo on a Site Engineer's confirmation (D-A53, 4a-3): the file
#: name's extension, the same three types the screens accept (approval_forms
#: .BILL_PHOTO_EXTENSIONS, which this module cannot import — approval_forms imports views).
_SITE_PHOTO_SUFFIXES = ('.jpg', '.jpeg', '.png')


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
    typed themselves. EXCEPT a Site Engineer's step (4a-3, D-A53): the Site Engineer's own
    site photos are linked to their decision, typed by them or recorded by SCM."""
    attachments = list(attachments)
    if step is not None and attachments:
        # The step as written in this transaction: a proxy record of this round, or any
        # decision on a Site Engineer step of it.
        may_carry_files = ApprovalStep.objects.filter(
            Q(is_proxy=True) | Q(party=APPROVAL_PARTY_SITE_ENGINEER),
            pk=step.pk, request=approval, round=round_no).exists()
        if not may_carry_files:
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
# Material lines (D-A28 .. D-A30)
# ---------------------------------------------------------------------------

def _clean_lines(lines, live_ids=frozenset()):
    """Validate material lines. Returns [{id, description, make, specification, quantity,
    unit}] in the order given, quantity a Decimal and texts stripped. Writes nothing;
    raises ApprovalRefused, naming the line, for the first problem found.

    Each line needs a description (300 characters at most; make 200), a unit from
    projects/units.py, and a quantity above zero that its unit allows (D-A30):
    parse_decimal_input() at places=0 for a count unit and places=2 for a measured one.
    REFUSED, NEVER ROUNDED — 2.5 Nos and 2.505 KWp are refused, not stored as 3 and 2.51.
    At least one line is required.

    `id` is for a resubmit: the pk of one of the request's live lines (`live_ids`), which
    that line then replaces in place. A line without one is new. On create `live_ids` is
    empty, so any id is refused.
    """
    lines = list(lines or ())
    if not lines:
        raise ApprovalRefused('Add at least one material line.')
    ceiling = decimal_field_max(MaterialApprovalLine, 'quantity')
    cleaned, named = [], set()
    for number, line in enumerate(lines, start=1):
        unknown = sorted(set(line) - set(_LINE_FIELDS))
        if unknown:
            raise ApprovalRefused(f'Line {number}: "{unknown[0]}" is not part of a line.')
        description = _clean(line.get('description'))
        make = _clean(line.get('make'))
        unit = _clean(line.get('unit'))
        if not description:
            raise ApprovalRefused(f'Line {number}: say what the material is.')
        if len(description) > 300:
            raise ApprovalRefused(f'Line {number}: the description must be 300 characters '
                                  f'or fewer.')
        if len(make) > 200:
            raise ApprovalRefused(f'Line {number}: the make must be 200 characters or fewer.')
        places = unit_places(unit)
        if places is None:
            raise ApprovalRefused(f'Line {number}: choose a unit from the list.')
        # The unit is named in the label of a count unit, so "must be a whole number"
        # says why: Nos, Set, Packet, Pair and Lot count whole things.
        label = (f'Line {number} quantity ({unit})' if unit in COUNT_UNITS
                 else f'Line {number} quantity')
        try:
            quantity = parse_decimal_input(line.get('quantity'), places=places,
                                           max_value=ceiling, field_label=label)
        except ValidationError as exc:
            raise ApprovalRefused(exc.messages[0])
        if quantity is None:
            raise ApprovalRefused(f'{label}: enter how many or how much.')
        if quantity <= 0:
            raise ApprovalRefused(f'{label}: must be more than zero.')
        line_id = line.get('id')
        if line_id in (None, ''):
            line_id = None
        elif line_id not in live_ids or line_id in named:
            raise ApprovalRefused(f'Line {number} is not one of this request\'s lines. '
                                  f'Open the request again and start over.')
        else:
            named.add(line_id)
        cleaned.append({'id': line_id, 'description': description, 'make': make,
                        'specification': _clean(line.get('specification')),
                        'quantity': quantity, 'unit': unit})
    return cleaned


def _line_fields(line, position):
    return {'position': position, 'description': line['description'], 'make': line['make'],
            'specification': line['specification'], 'quantity': line['quantity'],
            'unit': line['unit']}


def _replace_lines(material, cleaned):
    """Make `material`'s lines exactly `cleaned` (from _clean_lines), numbered 1..n in
    that order. A line with an id is updated in place, so round_changes can follow it
    from round to round by id; one without is created; a live line not named is DELETED.

    Hard-deleting is safe because every round's lines are in its ApprovalRoundSnapshot
    (D-A19): create and resubmit write the snapshot of the round they open, after their
    lines, in the same transaction, so the round being replaced was snapshotted when it
    opened. A request raised before snapshots existed (S1.1) predates lines too, so it
    has no lines to delete.

    The caller holds the request's row lock (_lock), and every writer of lines takes it
    first, so no other transaction writes these lines meanwhile."""
    live = MaterialApprovalLine.objects.filter(detail=material)
    kept = [line['id'] for line in cleaned if line['id'] is not None]
    live.exclude(pk__in=kept).delete()
    # (detail, position) is UNIQUE and checked row by row, so renumbering in place could
    # collide (line 3 moving to 2 while line 2 still holds it). Park the kept lines above
    # every position in use and every final position first; then each one moves to its
    # final position into a gap. Race: none — the request lock is held (docstring).
    top = max(live.aggregate(top=Max('position'))['top'] or 0, len(cleaned))
    live.filter(pk__in=kept).update(position=F('position') + top)
    for position, line in enumerate(cleaned, start=1):
        if line['id'] is None:
            MaterialApprovalLine.objects.create(detail=material, **_line_fields(line, position))
        else:
            # Race: none — the request lock is held (docstring).
            live.filter(pk=line['id']).update(**_line_fields(line, position))


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

    Schema 2 (material lines):
      schema, round, kind, kind_label, title, description, design_signoff_required,
      vendor {id, name} | null,
      programs [{id, name, short_tender_code}],
      projects [{id, project_id, customer_name, is_deleted}],
      site_groups [{id, name, program_id, program_name}],
      material null | {lines [{id, position, description, make, specification,
                               quantity, unit}],
                       legacy {proposed_make?, specification?, quantity_note?}
                              — present only when one of them is non-empty, holding
                              only the non-empty ones,
                       vendor_order {id, po_number, pi_number} | null,
                       pre_order_request {id, title} | null},
      steps [{party, party_label, sequence, assignee_id, assignee_name, carried,
              carried_from_step, carried_from_round, decided_in_round,
              carried_decider_name}]
    Lines are in position order, `quantity` a string as units.format_quantity() writes
    it ("120" for Nos, "2.50" for KWp) — a JSON number would be a float. Other lists are
    in pk order; steps in (sequence, party) order, live rows only.

    Schema 1 (before lines) differs only in `material`: proposed_make, specification and
    quantity_note always present, boq_items [{id, code, description, unit}], no lines,
    no legacy. Rows already written stay schema 1; readers accept both.

    Schema 3 (contractor bills, 4a-1) is schema 2 — `material` null — plus
      bill {project {id, project_id, customer_name}, amount, bill_number, bill_date,
            pdf {file_name, bucket, path, size_kb},
            tasks [{id, task_name, location_label, phase_name}]}
    `amount` a string with two places ("12500.00"), `bill_date` ISO, tasks in the order
    the bill names them. The PDF's bucket and path are kept so a round's own PDF stays
    reachable after a later round replaces it (4b). ONLY A BILL IS SCHEMA 3: a material
    request's snapshot stays schema 2, with no `bill` key, exactly as before.
    """
    request = (ApprovalRequest.objects.select_related('vendor')
               .get(pk=approval.pk))
    material = (MaterialApprovalDetail.objects
                .select_related('vendor_order', 'pre_order_request')
                .filter(request=request).first())
    # Asked for a bill only, so a material snapshot costs the same queries as before.
    bill = (ContractorBillDetail.objects.select_related('project')
            .filter(request=request).first()
            if request.kind == APPROVAL_KIND_CONTRACTOR_BILL else None)
    payload = {
        'schema': 3 if bill is not None else 2,
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
            'lines': [{'id': line.pk, 'position': line.position,
                       'description': line.description, 'make': line.make,
                       'specification': line.specification,
                       'quantity': format_quantity(line.quantity, line.unit),
                       'unit': line.unit}
                      for line in material.lines.order_by('position')],
            'vendor_order': ({'id': order.pk, 'po_number': order.po_number,
                              'pi_number': order.pi_number}
                             if order is not None else None),
            'pre_order_request': ({'id': pre_order.pk, 'title': pre_order.title}
                                  if pre_order is not None else None),
        }
        legacy = {key: getattr(material, key)
                  for key in ('proposed_make', 'specification', 'quantity_note')
                  if getattr(material, key)}
        if legacy:
            payload['material']['legacy'] = legacy
    if bill is not None:
        payload['bill'] = {
            'project': {'id': bill.project.pk, 'project_id': bill.project.project_id,
                        'customer_name': bill.project.customer_name},
            'amount': format(bill.amount, '.2f'),
            'bill_number': bill.bill_number,
            'bill_date': bill.bill_date.isoformat(),
            'pdf': {'file_name': bill.pdf_file_name, 'bucket': bill.pdf_bucket,
                    'path': bill.pdf_path, 'size_kb': bill.pdf_size_kb},
            'tasks': [{'id': link.task.pk, 'task_name': link.task.task_name,
                       'location_label': link.task.location_label,
                       'phase_name': link.task.phase.phase_name}
                      for link in bill.task_links.select_related('task__phase')
                      .order_by('pk')],
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


def _site_engineer_refusal(step, verdict, files):
    """Why this verdict with these files cannot be recorded on `step` — the Site Engineer
    rules (4a-3, D-A53, D-A54) — or None. `step` is the row re-read under the lock;
    `files` the photos the decision carries (the Site Engineer's own, or a proxy's).

      * files on any other party's step: refused — photos are the Site Engineer's;
      * a Site Engineer reject: refused — they confirm the work, never the bill;
      * a Site Engineer approval ("work confirmed") with no .jpg/.jpeg/.png among the
        files: refused. A proxy's evidence may also carry a PDF, but it needs one photo.
    """
    if step.party != APPROVAL_PARTY_SITE_ENGINEER:
        # Nothing to add for other parties: a proxy's evidence files are allowed on any
        # step (D-A21), and apply_approval_decision refuses the Site Engineer's own
        # `files` on any other step itself.
        return None
    if verdict == APPROVAL_STEP_REJECTED:
        return ('A Site Engineer confirms the work, or says it is not done; a bill is '
                'never rejected at this step.')
    if verdict == APPROVAL_STEP_APPROVED and not any(
            _clean(item.get('file_name')).lower().endswith(_SITE_PHOTO_SUFFIXES)
            for item in files):
        return 'Attach at least one photo of the work on site (JPG or PNG) to confirm it.'
    return None


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
# Contractor bills (4a-1)
# ---------------------------------------------------------------------------

def _task_label(task):
    if task.location_label:
        return f'{task.task_name} — {task.location_label}'
    return task.task_name


def _clean_bill(bill, vendor, programs, projects, site_groups):
    """Validate a contractor bill's details. Returns a dict of project, tasks (re-read,
    in the order given, de-duplicated), amount (Decimal), bill_number, bill_date and pdf.
    Writes nothing; raises ApprovalRefused for the first problem found, in this order:
    the vendor, the site, the scope, the tasks, the amount, the bill number and date,
    the PDF. `vendor` is already known to be present (checked before this is called).

    4b-1 split it into its parts so a resubmit can ask only what it revises: the
    contractor is locked on a resubmit, so _clean_bill_contractor is not asked again, and
    the PDF is judged only when a new one is posted (_clean_bill_pdf). Create asks every
    part, in the order above, exactly as before."""
    if bill is None:
        raise ApprovalRefused('Enter the contractor bill\'s details.')
    _clean_bill_contractor(vendor)
    project = bill.project
    _clean_bill_site(project, programs, projects, site_groups)
    cleaned = _clean_bill_values(project, bill.tasks, bill.amount, bill.bill_number,
                                 bill.bill_date)
    cleaned['pdf'] = _clean_bill_pdf(bill.pdf)
    return cleaned


def _clean_bill_contractor(vendor):
    """Refuse a vendor who may not send a bill. Create only: a resubmit cannot change the
    contractor, so it does not ask again (4b-1, Q1)."""
    # D-A14 / D-A38: only a vendor recorded as a contractor (or both) may send a bill; a
    # supplier's invoice is a purchase, paid through the PO/PI record, not approved here.
    if vendor.kind not in VENDOR_BILLABLE_KINDS:
        raise ApprovalRefused(f'{vendor.name} is recorded as a supplier, not a contractor. '
                              f'Only a contractor\'s bill can be raised.')
    if not vendor.is_active:
        raise ApprovalRefused(f'{vendor.name} is inactive.')


def _clean_bill_site(project, programs, projects, site_groups):
    """Refuse a missing, deleted or Draft site, and any scope but that one site."""
    if project is None:
        raise ApprovalRefused('Choose the site this bill is for.')
    if project.is_deleted:
        raise ApprovalRefused('That site has been deleted.')
    # A Draft site has not been activated, so no work on it can have been done. Every
    # other status may be billed: work finished before a hold, a cancellation or
    # commissioning is still owed.
    if project.status == 'Draft':
        raise ApprovalRefused(f'{project.project_id} is Draft; a bill cannot be raised '
                              f'against it.')
    # D-A10 / D-A31: one site per bill. The chokepoint sets the scope to that site itself,
    # so a caller may pass it or nothing — never anything else.
    if list(programs) or list(site_groups) or any(p.pk != project.pk for p in projects):
        raise ApprovalRefused('A contractor bill is about its one site; it names no other '
                              'tenders, sites or site groups.')


def _clean_bill_values(project, tasks, amount, bill_number, bill_date):
    """The tasks, amount, bill number and date, validated against `project`. Returns a
    dict of project, tasks (re-read, in the order given, de-duplicated), amount (Decimal),
    bill_number and bill_date. Writes nothing."""
    wanted = list(dict.fromkeys(task.pk for task in (tasks or ())))
    if not wanted:
        raise ApprovalRefused('Choose at least one task this bill covers.')
    # Re-read by pk with the phase joined: the phase's project is what the "task is on
    # this site" rule compares, and a stale instance must not decide it.
    found = Task.objects.select_related('phase').in_bulk(wanted)
    if len(found) != len(wanted):
        raise ApprovalRefused('A task you chose no longer exists. Choose again.')
    tasks = [found[pk] for pk in wanted]
    for task in tasks:
        if task.phase.project_id != project.pk:
            raise ApprovalRefused(f"'{_task_label(task)}' is not a task on "
                                  f"{project.project_id}.")
        # A mirror's status is derived from another record; there is no contractor work
        # of its own to confirm, so it is never billed.
        if task.is_mirror:
            raise ApprovalRefused(f"'{_task_label(task)}' is a mirror task — it records "
                                  f"another workspace's progress, so it cannot be billed.")

    try:
        amount = parse_decimal_input(
            amount, places=2,
            max_value=decimal_field_max(ContractorBillDetail, 'amount'),
            field_label='The bill amount')
    except ValidationError as exc:
        raise ApprovalRefused(exc.messages[0])
    if amount is None:
        raise ApprovalRefused('Enter the bill amount.')
    if amount <= 0:
        raise ApprovalRefused('The bill amount must be more than zero.')

    bill_number = _clean(bill_number)
    if not bill_number:
        raise ApprovalRefused('Enter the contractor\'s bill number.')
    if len(bill_number) > 100:
        raise ApprovalRefused('The bill number must be 100 characters or fewer.')

    if bill_date in (None, ''):
        raise ApprovalRefused('Enter the bill date.')
    bill_date, error = check_typed_date(bill_date)
    if error:
        raise ApprovalRefused(f'Bill date: {error}')
    if bill_date > timezone.localdate():
        raise ApprovalRefused('The bill date cannot be in the future.')

    return {'project': project, 'tasks': tasks, 'amount': amount,
            'bill_number': bill_number, 'bill_date': bill_date}


def _clean_bill_pdf(pdf):
    """The bill PDF as recorded: a .pdf in the private bills bucket. Returns the dict."""
    pdf = dict(pdf or {})
    if not all(_clean(pdf.get(key)) for key in ('file_name', 'bucket', 'path')):
        raise ApprovalRefused('Attach the contractor\'s bill as a PDF.')
    if not pdf['file_name'].lower().endswith('.pdf'):
        raise ApprovalRefused('The bill must be a PDF.')
    # D-A40: only a file stored in the PRIVATE bills bucket is recorded. With the setting
    # empty nothing matches, so a bill is refused rather than pointed at a public file.
    if pdf['bucket'] != bills_bucket():
        raise ApprovalRefused('The bill PDF must be stored privately, and it was not. '
                              'Nothing was saved.')
    return pdf


# ---------------------------------------------------------------------------
# 1. Create
# ---------------------------------------------------------------------------

def create_approval_request(*, kind, raised_by, title, description, pm_assignee,
                            design_assignee=None, site_engineer_assignee=None,
                            design_signoff_required=False, vendor=None, material=None,
                            lines=None, programs=(), projects=(), site_groups=(),
                            attachments=(), client_uuid=None, bill=None):
    """Raise a request and open round 1. Returns the ApprovalRequest.

    `material` is a dict for the two material kinds (vendor_order, pre_order_request)
    and must be None for a contractor bill. `lines` is the material, one dict per line
    (description, make, specification, quantity, unit — _clean_lines): at least one for
    a material kind (D-A28), none for a contractor bill. The request-wide proposed_make,
    specification, quantity_note and boq_items that lines replaced are refused in
    `material`. Kind-specific rules enforced here:

      * a vendor for pre-dispatch and the contractor bill (a CHECK holds it as well);
      * a vendor order (PO/PI record) for pre-dispatch, placed with that same vendor, and
        none on a pre-order request;
      * a linked pre-order approval only on pre-dispatch, only of the pre-order kind, only
        once APPROVED, and only for the same vendor as the request (Approvals 3a);
      * no design sign-off on a contractor bill (a CHECK holds it as well);
      * a Design Head named exactly when design sign-off is ticked, a Site Engineer
        exactly for a contractor bill.

    CONTRACTOR BILLS (4a-1). `bill` (a ContractorBill) is required for that kind and
    refused for the others; _clean_bill() gives its refusals and their order. The vendor
    must be an active contractor (D-A14, D-A38); the site not deleted and not Draft; the
    tasks at least one, all on that site, none a mirror; the amount above zero; the bill
    number and date present, the date not in the future; the PDF recorded in the private
    bills bucket (D-A39, D-A40). The request's scope is SET to the bill's one site: a
    caller may pass that site or nothing, and any other scope is refused (D-A31). Written
    in the transaction, before the ledger row, so the row's project resolves (D-A32).
    Warnings are bill_rules.py's and are not asked here.

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

    detail = new_lines = None
    if kind in APPROVAL_MATERIAL_KINDS:
        detail = dict(material or {})
        for key in _LEGACY_MATERIAL:
            if key in detail:
                raise ApprovalRefused(
                    f'"{key}" is not recorded on a request any more: each material line '
                    f'carries its own make, specification, quantity and unit.')
        new_lines = _clean_lines(lines)
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
    elif lines:
        raise ApprovalRefused('A contractor bill carries no material lines.')

    bill_row = None
    if is_bill:
        bill_row = _clean_bill(bill, vendor, programs, projects, site_groups)
        programs, projects, site_groups = (), [bill_row['project']], ()
    elif bill is not None:
        raise ApprovalRefused('Only a contractor bill carries bill details.')

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
                vendor_order=detail.get('vendor_order'),
                pre_order_request=detail.get('pre_order_request'))
            MaterialApprovalLine.objects.bulk_create(
                MaterialApprovalLine(detail=material_row, **_line_fields(line, position))
                for position, line in enumerate(new_lines, start=1))
        if bill_row is not None:
            pdf = bill_row['pdf']
            bill_detail = ContractorBillDetail.objects.create(
                request=approval, project=bill_row['project'], amount=bill_row['amount'],
                bill_number=bill_row['bill_number'], bill_date=bill_row['bill_date'],
                pdf_file_name=_clean(pdf['file_name']), pdf_bucket=pdf['bucket'],
                pdf_path=pdf['path'], pdf_size_kb=pdf.get('file_size_kb') or 0)
            ContractorBillTask.objects.bulk_create(
                ContractorBillTask(detail=bill_detail, task=task)
                for task in bill_row['tasks'])

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

def apply_approval_decision(step, verdict, actor, note='', proxy=None, files=()):
    """Record one verdict on `step`. Returns the request, re-read.

    `actor` is who is typing it into PMS (`recorded_by`). Without `proxy` the actor is
    also the decider. With `proxy` (a ProxyDecision) the actor must be SCM, and
    `proxy.decided_by` is who decided, over `proxy.channel`, with `proxy.evidence`.
    `proxy.files` (already uploaded by the caller) become ApprovalAttachment rows linked
    to the step, uploaded_by the actor, written after the step inside this transaction —
    so a refused decision leaves no attachment row.

    THE SITE ENGINEER CONFIRMS THE WORK, NEVER THE BILL (4a-3, D-A53, D-A54). On a Site
    Engineer step:

      * approved means "work confirmed done", and needs at least one site photo (a .jpg,
        .jpeg or .png): in `files` when the Site Engineer decides it, in `proxy.files`
        when SCM records it on their behalf (the photos they sent on WhatsApp);
      * changes requested means "work not done" — the note is required, as for every
        changes request, and the bill goes back to SCM;
      * rejected is refused: whether a bill is paid is not the Site Engineer's question.

    `files` (4a-3) are the Site Engineer's own photos, already uploaded by the caller,
    written like proxy files, linked to the step. Refused on any other party's step, and
    alongside `proxy` (a proxy's files are `proxy.files`). The party is read from the step
    re-read UNDER THE LOCK, never before it (Layer 5 #1).

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
    files = list(files or ())
    if files and proxy is not None:
        raise ApprovalRefused('A decision recorded on someone\'s behalf carries its files '
                              'as evidence.')
    for item in files:
        if not all(_clean(item.get(key)) for key in ('file_name', 'bucket', 'path')):
            raise ApprovalRefused('A photo is missing its name or where it was stored.')
    decider = proxy.decided_by if proxy is not None else actor

    with transaction.atomic():
        approval = _lock(step.request_id)
        step = _locked_step(step, approval)

        message = _step_state_refusal(step, approval)
        if message:
            raise ApprovalRefused(message)
        if files and step.party != APPROVAL_PARTY_SITE_ENGINEER:
            raise ApprovalRefused("Photos are attached only to a Site Engineer's decision.")
        message = _site_engineer_refusal(
            step, verdict, list(proxy.files or ()) if proxy is not None else files)
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
        elif files:
            _add_attachments(approval, step.round, files, actor, step=step)

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

#: 4b-1: what a resubmit that tries to move a bill to another contractor or site reads.
BILL_CONTRACTOR_LOCKED = ('The contractor on a bill cannot be changed. Withdraw it and '
                          'raise a new bill from the other contractor.')
BILL_SITE_LOCKED = ('The site on a bill cannot be changed. Withdraw it and raise a new '
                    'bill for the other site.')
#: 4b-1 (D-A20, B): the Site Engineer confirmed the tasks the bill named; other tasks are
#: work they have not confirmed.
BILL_KEEP_TASKS_CHANGED = ('The Site Engineer\'s confirmation cannot be kept: the tasks on '
                           'this bill changed, so the Site Engineer must confirm the work '
                           'again.')


def _bill_revision(approval, revision):
    """A contractor bill's part of a revision (4b-1): validate it against the bill and
    return what to write — {detail, fields, add, remove, tasks_changed}. `fields` holds
    only the ContractorBillDetail columns whose value changes; `add` the Tasks to link,
    `remove` the task pks to unlink. Writes nothing.

    THE SITE IS LOCKED. A scope key that names exactly the bill's one site (projects) or
    nothing (programs, site_groups) repeats what the bill holds and is dropped; anything
    else is refused with BILL_SITE_LOCKED. (The contractor lock is _revision_writes'.)

    When any bill key is sent, the bill as revised is judged by create's own parts: the
    site (_clean_bill_site — not deleted, not Draft) and the tasks, amount, number and
    date (_clean_bill_values). Not the contractor — it cannot change (Q1) — and the PDF
    only when a new one is sent (_clean_bill_pdf), so an unchanged PDF is never refused
    for a bucket setting changed since."""
    # The bill with its site: the lock compares against the site, and the revised tasks
    # must be on it.
    detail = ContractorBillDetail.objects.select_related('project').get(request=approval)
    site = detail.project
    for key in _REVISABLE_SCOPE:
        if key in revision:
            wanted = [row.pk for row in revision[key]]
            if wanted != ([site.pk] if key == 'projects' else []):
                raise ApprovalRefused(BILL_SITE_LOCKED)
    # The bill's tasks now, in the order it names them: the default when `tasks` is not
    # revised, and what "the tasks changed" (B) is measured against.
    current = [link.task for link in detail.task_links.select_related('task').order_by('pk')]
    result = {'detail': detail, 'fields': {}, 'add': [], 'remove': [],
              'tasks_changed': False}
    if not any(key in revision for key in _REVISABLE_BILL):
        return result

    _clean_bill_site(site, (), [site], ())
    cleaned = _clean_bill_values(
        site, revision.get('tasks', current), revision.get('amount', detail.amount),
        revision.get('bill_number', detail.bill_number),
        revision.get('bill_date', detail.bill_date))
    fields = result['fields']
    for key in ('amount', 'bill_number', 'bill_date'):
        if cleaned[key] != getattr(detail, key):
            fields[key] = cleaned[key]
    if 'pdf' in revision:
        pdf = _clean_bill_pdf(revision['pdf'])
        fields.update(pdf_file_name=_clean(pdf['file_name']), pdf_bucket=pdf['bucket'],
                      pdf_path=pdf['path'], pdf_size_kb=pdf.get('file_size_kb') or 0)
    held = {task.pk for task in current}
    kept = {task.pk for task in cleaned['tasks']}
    result['add'] = [task for task in cleaned['tasks'] if task.pk not in held]
    result['remove'] = [pk for pk in held if pk not in kept]
    result['tasks_changed'] = bool(result['add'] or result['remove'])
    return result


def _write_bill_revision(bill):
    """Write _bill_revision()'s result. Called inside resubmit's transaction, under the
    request lock, before the new round's snapshot, so that snapshot records the revised
    bill and the previous round's keeps the old one — its PDF included (never deleted)."""
    if bill['fields']:
        # Race: this is the only UPDATE of a bill's detail, and resubmit holds the request
        # row lock (_lock) for its whole transaction, so two resubmits of one bill run one
        # after the other — and the second is refused, the bill no longer waiting.
        ContractorBillDetail.objects.filter(pk=bill['detail'].pk).update(**bill['fields'])
    if bill['remove']:
        ContractorBillTask.objects.filter(detail=bill['detail'],
                                          task_id__in=bill['remove']).delete()
    # Tasks still on the bill keep their link rows (and their place in its order); the
    # added ones follow, in the order given.
    ContractorBillTask.objects.bulk_create(
        ContractorBillTask(detail=bill['detail'], task=task) for task in bill['add'])


def _revision_writes(approval, revision, lines):
    """Validate `revision` and `lines` against `approval` (locked) and return what to
    write: (request_fields, scope, material, new_lines, bill). The kind's rules are
    re-checked against the request AS REVISED. Refuses an unknown key (the retired
    material keys among them), a bill's key on any other kind, a blank title or
    description, a missing or mismatched vendor, a deleted project in the scope, lines on
    a request with no material detail, and invalid lines (_clean_lines).

    `new_lines` is None when `lines` is None: the lines stay as they are. That is refused
    for a material request that has NO lines — one raised before lines existed: its
    resubmit must record the quantity as lines (at least one).

    `bill` (4b-1) is None for a material request, else _bill_revision()'s result. A
    bill's contractor is LOCKED: a `vendor` naming another one is refused
    (BILL_CONTRACTOR_LOCKED) — after the "Choose the vendor" check, which still answers a
    vendor of None — and the same vendor is dropped. Its site is locked the same way
    (_bill_revision), so `scope` is always empty for a bill. Writes nothing."""
    revision = dict(revision or {})
    for key in revision:
        if key not in _REVISABLE:
            raise ApprovalRefused(f'"{key}" cannot be changed on a resubmit.')
    is_bill = approval.kind == APPROVAL_KIND_CONTRACTOR_BILL
    if not is_bill:
        for key in _REVISABLE_BILL:
            if key in revision:
                raise ApprovalRefused(f'"{key}" belongs to a contractor bill; this request '
                                      f'is not one.')
    material = (MaterialApprovalDetail.objects.select_related('vendor_order')
                .filter(request=approval).first())
    if material is None:
        if lines is not None:
            raise ApprovalRefused('A contractor bill carries no material lines.')
        new_lines = None
    else:
        live_ids = set(material.lines.values_list('pk', flat=True))
        if lines is not None:
            new_lines = _clean_lines(lines, live_ids)
        elif not live_ids:
            raise ApprovalRefused('This request was raised before material lines. Record '
                                  'its material as lines — at least one — to resubmit it.')
        else:
            new_lines = None

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

    if is_bill:
        if 'vendor' in request_fields:
            if request_fields['vendor'].pk != approval.vendor_id:
                raise ApprovalRefused(BILL_CONTRACTOR_LOCKED)
            del request_fields['vendor']
        return request_fields, {}, material, new_lines, _bill_revision(approval, revision)

    scope = {key: list(revision[key]) for key in _REVISABLE_SCOPE if key in revision}
    if any(project.is_deleted for project in scope.get('projects', ())):
        raise ApprovalRefused('A deleted project cannot be named in the scope.')
    return request_fields, scope, material, new_lines, None


def resubmit_approval_request(approval, actor, note, attachments=(), assignees=None,
                              revision=None, carry=None, lines=None):
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
    programs, projects, site_groups. Omitted keys stay as they are; a scope key replaces
    that whole set. The kind's rules are re-checked against the revised request
    (_revision_writes). Kind, design sign-off, vendor order and pre-order link are not
    revisable.

    A CONTRACTOR BILL (4b-1) may also revise amount, bill_number, bill_date, tasks (the
    set, replaced) and pdf (a NEW file already in the private bucket; the old one stays
    recorded in the previous round's snapshot and is never deleted). Its contractor and
    site are LOCKED: a vendor or scope naming anything else is refused; repeating what the
    bill holds is dropped. The revised values meet create's refusals (_bill_revision).
    A Site Engineer's confirmation may be kept only while the tasks are unchanged: with a
    task added or removed, keeping it is refused (BILL_KEEP_TASKS_CHANGED).

    LINES (material lines). `lines`, when given, REPLACES the material's lines as a set
    (_replace_lines): a line carrying the `id` of a live line edits it, one without adds
    a line, a live line left out is removed. At least one; each validated as on create.
    None leaves the lines as they are — refused for a request raised before lines, which
    has none. The round snapshot written below records the new set; the replaced set is
    in the previous round's.

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

        request_fields, scope, material, new_lines, bill = _revision_writes(
            approval, revision, lines)

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
        # B (4b-1, D-A20): the Site Engineer confirmed the tasks the bill named; with a
        # task added or removed there is work they have not confirmed.
        if bill is not None and bill['tasks_changed'] \
                and APPROVAL_PARTY_SITE_ENGINEER in carries:
            raise ApprovalRefused(BILL_KEEP_TASKS_CHANGED)

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
        if new_lines is not None:
            _replace_lines(material, new_lines)
        if bill is not None:
            _write_bill_revision(bill)
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


# ---------------------------------------------------------------------------
# 6. PO/PI records covered by a pre-order approval (Approvals 3b)
# ---------------------------------------------------------------------------
#
# The only writers of ApprovalOrderLink. Linking is OPTIONAL: nothing warns about,
# blocks or counts a record without a link. The request's status does not move, so no
# ledger row is written — History reads the link rows themselves, as it reads a
# reassignment's superseded step. No notifications.

def link_order_to_approval(approval, vendor_order, actor):
    """SCM links `vendor_order` (a PO/PI record) to the approved pre-order `approval`.
    Returns the new ApprovalOrderLink.

    Refused unless the actor is SCM, the request is a pre-order request and approved,
    and — when the request names a vendor — the record was placed with that vendor. A
    request naming no vendor may cover a record of any vendor. Refused while the same
    record is already actively linked; a removed link does not count, so a record can
    be linked again. Under the request lock, so two links of one pair run one after the
    other; the partial UNIQUE holds it as well.
    """
    if vendor_order is None:
        raise ApprovalRefused('Choose the PO/PI record to link.')

    with transaction.atomic():
        approval = _lock(approval.pk)
        if not user_can_raise_approval_request(actor.user):
            raise ApprovalRefused('Only SCM can link a PO/PI record to an approval.')
        if approval.kind != APPROVAL_KIND_MATERIAL_PRE_ORDER:
            raise ApprovalRefused('Only a pre-order approval covers PO/PI records.')
        if approval.status != APPROVAL_APPROVED:
            raise ApprovalRefused('This pre-order approval has not been approved.')
        if approval.vendor_id is not None and vendor_order.vendor_id != approval.vendor_id:
            raise ApprovalRefused('The PO/PI record was placed with a different vendor.')
        if ApprovalOrderLink.objects.filter(
                approval=approval, vendor_order=vendor_order,
                removed_at__isnull=True).exists():
            raise ApprovalRefused('That PO/PI record is already linked to this approval.')
        return ApprovalOrderLink.objects.create(
            approval=approval, vendor_order=vendor_order, linked_by=actor)


def unlink_order_from_approval(link, actor, note):
    """SCM removes `link`, with a reason. Returns the link, re-read.

    The link is never deleted: removed_by, removed_at and removal_note are stamped once,
    here — ApprovalOrderLink's only update path. Refused without a reason, unless the
    actor is SCM, and when the link was already removed.
    """
    note = _clean(note)
    if not note:
        raise ApprovalRefused('Say why this link is being removed.')

    with transaction.atomic():
        approval = _lock(link.approval_id)
        link = ApprovalOrderLink.objects.select_for_update().get(pk=link.pk)
        if not user_can_raise_approval_request(actor.user):
            raise ApprovalRefused('Only SCM can remove a PO/PI record link.')
        if link.removed_at is not None:
            raise ApprovalRefused('That link was already removed.')
        written = ApprovalOrderLink.objects.filter(
            pk=link.pk, approval=approval, removed_at__isnull=True,
        ).update(removed_by=actor, removed_at=timezone.now(), removal_note=note)
        if written != 1:   # impossible under the lock; never silently carry on
            raise ApprovalRefused('The link changed while you were removing it.')

    link.refresh_from_db()
    return link
