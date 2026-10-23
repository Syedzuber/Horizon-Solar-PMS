"""
Approvals 2b — who hears about an approval action, what they read, and the email.

approvals.py decides; this module only tells people. Every function here that sends is
an AFTER-COMMIT callback: approvals.py registers exactly one per action, inside the
action's own transaction.atomic(), with transaction.on_commit(..., robust=True), and
passes it ids (plus, for a decision, the status the request moved to). A refused or
rolled-back action therefore registers nothing that runs, and a notice that raises
after commit is logged by Django and never turns a recorded decision into an error.

THE CALLBACKS, ONE PER ACTION, AND WHAT EACH SENDS

  after_create(approval_pk, actor_pk)
      E1  every step activated in round 1 -> its assignee (in-app + email), and for a
          design step the assignee Head's named deputy (in-app + email).
  after_decision(step_pk, actor_pk, closed_as)
      E1  a later sequence this decision activated (a contractor bill's PM once the
          Site Engineer approves). Its rows are the round's steps whose activated_at
          equals the decided step's decided_at: approvals.py writes both from one `now`.
      E2  `closed_as` (approved / changes_requested / rejected; None while the round is
          still open) -> the raiser (in-app + email).
      E2b a close by changes requested or rejected -> each assignee whose pending,
          ACTIVATED step that close superseded (in-app only; no deputies). Its rows:
          superseded_by the decider, superseded_at equal to the decided step's
          decided_at (one `now`).
      E6  a proxy decision -> the person it was recorded for (in-app only).
  after_resubmit(approval_pk, round_no, actor_pk, replaced_step_pks)
      E1  every non-carried step activated in the new round (+ design deputy).
      R5  each previous-round step whose approver the override replaced -> its assignee
          (in-app only), if that step had been activated (ruling 4's rule).
      E5  each carried step -> the original decider (in-app only).
  after_withdraw(approval_pk, actor_pk)
      E3  every step the withdrawal superseded that had been activated -> its assignee
          (in-app + email). Its rows: superseded_by the actor, superseded_at equal to
          the request's closed_at (one `now` again).
  after_reassign(old_step_pk, new_step_pk, actor_pk)
      E4  the old assignee, if their step had been activated (in-app only).
      E1  the new assignee (+ design deputy), if the replacement is active now. A
          replacement still waiting on an earlier sequence is told when that sequence
          completes, by after_decision.

THE RULES EVERY CALLBACK SHARES (rulings 2 and 3)

  * NEVER THE ACTOR. Whoever performed the action (the SCM user who typed a proxy
    decision, the decider otherwise) is never told about it. A proxy notice goes to the
    person it was recorded FOR, who did not act in PMS.
  * ONE NOTICE PER PERSON PER ACTION. Each callback builds its whole list and keeps the
    FIRST notice per profile, in this order: E1 (you are asked to act) before E2, E2b,
    the replaced-approver notice, E3, E4 and E5. An assignee's E1 comes before any deputy's
    E1, so a deputy who is also an assignee on the request is told as the assignee. E6
    goes to the proxy's decider, whom E1 and E2 exclude, so it is never a second notice.
  * NOBODY INACTIVE. A profile or auth.User that is no longer active is skipped.

APPROVAL NOTICES REACH A DESIGN HEAD'S DEPUTY; DESIGN CHANGE-REQUEST NOTICES DELIBERATELY
DO NOT (design_views D4). The two modules differ on purpose: an approval's design step
may be decided by the deputy (user_can_decide_approval_step), so the deputy is someone
who can act on it. The deputy is `assignee.design_head_deputy`, the FK the Head's own
profile holds; the assignee of a design step is always a Head
(profile_can_be_approval_assignee).

EMAIL (T3c). Every emailed notice is a notify_registry event sent with notify_now(): the
bell text is the event's in_app_text, the email its template in the shared layout, under
the same NotificationLog label as before. The in-app-only notices (E2b, R5, E4, E5, E6)
still call send_notification() directly. related_project stays None on every row.

LINKS. The in-app link is relative (/approvals/<pk>/). notify builds the email's absolute
link with absolute_url() on APP_BASE_URL, production's host even for a send from a local
database (ruling 1); it is the "Open the approval" button and the text part's last line.

MATERIAL LINES (28 Sep 2026). Every approval email of a material request carries one line
saying what it is about — "120 Nos — Solar module 540W, +2 more" (lines_summary): the
first line, and how many others. It is the event's summary_line, "Material: ...", a
paragraph of its own in both email parts, dropped when there is nothing to say. The bell
text stays one sentence.

CONTRACTOR BILLS (4a-2). A bill's emails carry a one-line bill summary in the same place
and the same way — "Civil Co · HRP-RES-2026-001 · bill CB-7 · ₹12,500.00" (bill_summary):
the contractor, the site, the contractor's bill number and the amount — labelled "Bill"
instead of "Material". The PM's E1, the raiser's E2 and a PM's E3 carry it (ruling 4). NO
EMAIL AND NO IN-APP NOTICE EVER CARRIES THE BILL PDF, A LINK TO IT, OR WHERE IT IS STORED:
the summary is built from those four values only, and this module does not import
bill_storage.

THE SITE ENGINEER CONFIRMS THE WORK, NEVER THE BILL (4a-3, D-A53). A notice to a Site
Engineer — asked to confirm (E1), or told the bill was withdrawn (E3) — carries no amount,
bill number or PDF: its email summary is the work — contractor · site · tasks
("Foundation, +1 more", work_summary), labelled "Work". It asks them "to confirm the
work", never to approve. Their two events (approval_work_to_confirm,
approval_withdrawn_work) declare work_summary and no variable that can hold an amount
(T3c, Q1 and M4). The PM, SCM and the raiser keep the full bill summary, including the
raiser's approval_work_not_done.
"""
import logging

from django.urls import reverse

from .bill_rules import format_bill_amount
from .models import (
    ApprovalRequest, ApprovalStep, ContractorBillDetail, MaterialApprovalLine, UserProfile,
    APPROVAL_APPROVED, APPROVAL_CHANGES_REQUESTED, APPROVAL_REJECTED,
    APPROVAL_KIND_CHOICES, APPROVAL_KIND_CONTRACTOR_BILL, APPROVAL_PARTY_CHOICES,
    APPROVAL_PARTY_DESIGN, APPROVAL_PARTY_SITE_ENGINEER,
    APPROVAL_PROXY_EMAIL, APPROVAL_PROXY_IN_PERSON, APPROVAL_PROXY_PHONE,
    APPROVAL_PROXY_WHATSAPP,
    APPROVAL_STEP_APPROVED, APPROVAL_STEP_CHANGES_REQUESTED, APPROVAL_STEP_REJECTED,
    APPROVAL_STEP_SUPERSEDED,
)
from .notifications import send_notification
from .notify import notify_now
from .units import format_quantity

logger = logging.getLogger(__name__)

IN_APP = ['in_app']

_KIND_LABELS = dict(APPROVAL_KIND_CHOICES)
_PARTY_LABELS = dict(APPROVAL_PARTY_CHOICES)

#: E6's "<verdict>": what the person gave, as a noun.
_VERDICT_NOUNS = {
    APPROVAL_STEP_APPROVED: 'approval',
    APPROVAL_STEP_CHANGES_REQUESTED: 'request for changes',
    APPROVAL_STEP_REJECTED: 'rejection',
}
#: E6's "from <channel>", worded so each reads as a sentence.
_CHANNEL_PHRASES = {
    APPROVAL_PROXY_WHATSAPP: 'WhatsApp',
    APPROVAL_PROXY_PHONE: 'a phone call',
    APPROVAL_PROXY_EMAIL: 'an email',
    APPROVAL_PROXY_IN_PERSON: 'an in-person conversation',
}

# NotificationLog.template_name labels, one per notice.
T_ACTIVATED = 'approval_step_activated'
T_ACTIVATED_DEPUTY = 'approval_step_activated_deputy'
T_CLOSED = {
    APPROVAL_APPROVED: 'approval_approved',
    APPROVAL_CHANGES_REQUESTED: 'approval_changes_requested',
    APPROVAL_REJECTED: 'approval_rejected',
}
T_SUPERSEDED = 'approval_step_superseded'
T_WITHDRAWN = 'approval_withdrawn'
T_REASSIGNED_AWAY = 'approval_reassigned_away'
T_REPLACED = 'approval_replaced_on_resubmit'
T_KEPT = 'approval_kept'
T_PROXY = 'approval_proxy_recorded'


#: E6's "<verdict>" for a Site Engineer (4a-3): what they said about the work.
_SITE_ENGINEER_VERDICT_NOUNS = {
    APPROVAL_STEP_APPROVED: 'confirmation that the work is done',
    APPROVAL_STEP_CHANGES_REQUESTED: 'report that the work is not done',
}


class _Notice:
    """One message to one person. An emailed notice names its registry `event` and that
    event's `values` and goes through notify_now() (T3c); an in-app-only notice carries
    its `message` and goes through send_notification() as before (M5).

    `work_only` (4a-3, T3c Q1): the event carries the bill's work summary, never the
    bill's (D-A53). Every other emailed event carries summary_line."""

    def __init__(self, recipient, template, *, event=None, values=None, message='',
                 work_only=False):
        self.recipient = recipient
        self.template = template
        self.event = event
        self.values = values or {}
        self.message = message
        self.work_only = work_only


# ---------------------------------------------------------------------------
# Shared pieces
# ---------------------------------------------------------------------------

def _name(profile):
    return profile.user.get_full_name() or profile.user.username


def _is_active(profile):
    return (profile is not None and profile.is_active
            and profile.user is not None and profile.user.is_active)


def _request(approval_pk):
    return (ApprovalRequest.objects.select_related('raised_by__user')
            .get(pk=approval_pk))


def _steps(**filters):
    return list(ApprovalStep.objects.filter(**filters)
                .select_related('assignee__user',
                                'assignee__design_head_deputy__user',
                                'decided_by__user', 'recorded_by__user',
                                'superseded_by__user')
                .order_by('sequence', 'party', 'pk'))


def _link(approval):
    return reverse('approval_detail', args=[approval.pk])


def lines_summary(approval):
    """What a material request is about, in one line for an email: the first material
    line as "<quantity> <unit> — <description>", then ", +N more" for the rest. '' for a
    request with no lines — a contractor bill, or one raised before lines. One query."""
    # Every line, not just the first: the count of the rest comes from the same query.
    lines = list(MaterialApprovalLine.objects.filter(detail__request_id=approval.pk)
                 .order_by('position'))
    if not lines:
        return ''
    first = lines[0]
    summary = (f'{format_quantity(first.quantity, first.unit)} {first.unit} — '
               f'{first.description}')
    if len(lines) > 1:
        summary += f', +{len(lines) - 1} more'
    return summary


def bill_summary(approval):
    """What a contractor bill is, in one line for an email: the contractor, the site, the
    contractor's bill number and the amount — "Civil Co · HRP-RES-2026-001 · bill CB-7 ·
    ₹12,500.00". Never the PDF or where it is stored. '' for a request with no bill. One
    query."""
    # The bill with the two names the line needs: the request's vendor and the site.
    detail = (ContractorBillDetail.objects.select_related('request__vendor', 'project')
              .filter(request_id=approval.pk).first())
    if detail is None:
        return ''
    vendor = detail.request.vendor
    return (f'{vendor.name if vendor else "—"} · {detail.project.project_id} · '
            f'bill {detail.bill_number} · {format_bill_amount(detail.amount)}')


def work_summary(approval):
    """What a contractor bill's Site Engineer is asked about, in one line (4a-3, D-A53):
    the contractor, the site and the tasks — "Civil Co · HRP-RES-2026-001 · Foundation, +1
    more". Never the amount, the bill number or the PDF. '' for a request with no bill.
    Two queries."""
    # The bill with the contractor and the site, as bill_summary reads it.
    detail = (ContractorBillDetail.objects.select_related('request__vendor', 'project')
              .filter(request_id=approval.pk).first())
    if detail is None:
        return ''
    # The bill's tasks in the order it names them; only the first is named.
    tasks = list(detail.task_links.select_related('task').order_by('pk'))
    vendor = detail.request.vendor
    summary = f'{vendor.name if vendor else "—"} · {detail.project.project_id}'
    if tasks:
        first = tasks[0].task
        label = (f'{first.task_name} — {first.location_label}' if first.location_label
                 else first.task_name)
        summary += f' · {label}'
        if len(tasks) > 1:
            summary += f', +{len(tasks) - 1} more'
    return summary


def email_summary(approval, work_only=False):
    """(summary, label) for an approval email: a bill's bill_summary under "Bill" — or,
    for its Site Engineer (`work_only`, 4a-3), work_summary under "Work" — and any other
    request's lines_summary under "Material". The summary is '' when there is nothing to
    say. One or two queries."""
    if approval.kind == APPROVAL_KIND_CONTRACTOR_BILL:
        if work_only:
            return work_summary(approval), 'Work'
        return bill_summary(approval), 'Bill'
    return lines_summary(approval), 'Material'


def _send(approval, notices, excluded, actor):
    """Send `notices` in order: the actor and anyone already told in this action are
    skipped, as is anyone inactive. One failing send is logged and the rest still go.

    An emailed notice goes through notify_now() (T3c): in-app from the event's text and
    the email from its template, both logged under the notice's label. notify_now(), not
    notify(): every caller here already runs on commit (approvals.py registers it), and a
    notify() would queue a second callback behind this one. The request's one-line summary
    is read once per action per kind of reader — the request's own (summary_line), or a
    bill's work-only line (work_summary, 4a-3) — and only if someone of that kind is
    emailed. related_project stays None, as it was before T3c."""
    told = set(excluded)
    link = _link(approval)
    summaries = {}
    for notice in notices:
        person = notice.recipient
        if not _is_active(person) or person.pk in told:
            continue
        told.add(person.pk)
        try:
            if notice.event is None:
                send_notification(
                    recipient=person, message=notice.message, channels=IN_APP, link=link,
                    template=notice.template, related_project=None, actor=actor)
                continue
            if notice.work_only not in summaries:
                summaries[notice.work_only] = email_summary(approval, notice.work_only)
            summary, label = summaries[notice.work_only]
            values = dict(notice.values)
            if notice.work_only:
                values['work_summary'] = summary
            else:
                # A paragraph of its own in the email; '' drops it (notify, T3c Q3).
                values['summary_line'] = f'{label}: {summary}' if summary else ''
            notify_now(notice.event, recipients=[person], values=values, actor=actor,
                       related_project=None, link=link, log_label=notice.template)
        except Exception:
            logger.exception('approval_notices: the %s notice on approval #%s to %s '
                             'failed; the approval action stands.',
                             notice.template, approval.pk, person.pk)


def _kind(approval):
    """The request's kind as every notice names it, e.g. 'Material — before order'."""
    return _KIND_LABELS.get(approval.kind, approval.kind)


def _activated_notices(approval, steps, asked_by):
    """E1 for `steps`: each assignee first, then each design step's deputy. `asked_by`
    is the sentence start saying who is asking, e.g. 'Sana asks you': one value for the
    four ways a step is activated (T3c, Q2)."""
    about = dict(approval_title=approval.title, kind_label=_kind(approval))
    notices, deputies = [], []
    for step in steps:
        party = _PARTY_LABELS.get(step.party, step.party)
        if step.party == APPROVAL_PARTY_SITE_ENGINEER:
            # The Site Engineer confirms the work, never the bill (D-A53, Q4).
            notices.append(_Notice(step.assignee, T_ACTIVATED,
                                   event='approval_work_to_confirm',
                                   values=dict(about, asked_by=asked_by), work_only=True))
            continue
        notices.append(_Notice(step.assignee, T_ACTIVATED, event='approval_step_activated',
                               values=dict(about, asked_by=asked_by, party_label=party)))
        deputy = step.assignee.design_head_deputy if step.party == APPROVAL_PARTY_DESIGN \
            else None
        if deputy is not None and deputy.pk != approval.raised_by_id:
            deputies.append(_Notice(deputy, T_ACTIVATED_DEPUTY,
                                    event='approval_step_activated_deputy',
                                    values=dict(about, head_name=_name(step.assignee))))
    return notices + deputies


# ---------------------------------------------------------------------------
# The after-commit callbacks
# ---------------------------------------------------------------------------

def after_create(approval_pk, actor_pk):
    """E1 for round 1's active steps."""
    approval = _request(approval_pk)
    actor = approval.raised_by
    steps = _steps(request=approval, round=1, activated_at__isnull=False,
                   carried_from__isnull=True)
    notices = _activated_notices(approval, steps, f'{_name(actor)} asks you')
    _send(approval, notices, {actor_pk}, actor)


def after_decision(step_pk, actor_pk, closed_as=None):
    """E1 for a sequence this decision activated, E2 if it closed the round, E6 if it
    was recorded on someone's behalf."""
    step = (ApprovalStep.objects
            .select_related('decided_by__user', 'recorded_by__user')
            .get(pk=step_pk))
    approval = _request(step.request_id)
    decider, recorder = step.decided_by, step.recorded_by
    party = _PARTY_LABELS.get(step.party, step.party)
    notices = []

    activated = [s for s in _steps(request=approval, round=step.round,
                                   activated_at=step.decided_at,
                                   carried_from__isnull=True)
                 if s.pk != step.pk]
    # A Site Engineer confirmed the work; they did not approve the bill (D-A53, Q4).
    did = ('confirmed the work done' if step.party == APPROVAL_PARTY_SITE_ENGINEER
           else 'approved')
    notices += _activated_notices(
        approval, activated,
        f'{_name(decider)} {did} as the {party}; you are now asked')

    if closed_as in T_CLOSED:
        values = dict(approval_title=approval.title, kind_label=_kind(approval),
                      decided_by_name=_name(decider), party_label=party,
                      note_clause=f' Note: "{step.note}"' if step.note else '')
        if closed_as == APPROVAL_APPROVED:
            event = 'approval_approved'
        elif closed_as == APPROVAL_CHANGES_REQUESTED \
                and step.party == APPROVAL_PARTY_SITE_ENGINEER:
            # "Work not done" (D-A53): the Site Engineer's changes request, as History
            # words it. It goes to the raiser, so it keeps the bill summary.
            event = 'approval_work_not_done'
        elif closed_as == APPROVAL_CHANGES_REQUESTED:
            event = 'approval_changes_requested'
        else:
            event = 'approval_rejected'
        notices.append(_Notice(approval.raised_by, T_CLOSED[closed_as], event=event,
                               values=values))

    if closed_as in (APPROVAL_CHANGES_REQUESTED, APPROVAL_REJECTED):
        what = 'requested changes' if closed_as == APPROVAL_CHANGES_REQUESTED \
            else 'rejected'
        for other in _steps(request=approval, round=step.round,
                            verdict=APPROVAL_STEP_SUPERSEDED, superseded_by=decider,
                            superseded_at=step.decided_at, activated_at__isnull=False):
            message = (f'You no longer need to decide "{approval.title}". '
                       f'{_name(decider)} {what} as the {party}.')
            notices.append(_Notice(other.assignee, T_SUPERSEDED, message=message))

    # E1, E2 and E2b never go to the decider; E6 goes to nobody else.
    excluded = {actor_pk, decider.pk}
    _send(approval, notices, excluded, recorder)

    if step.is_proxy and decider.pk != actor_pk:
        # A Site Engineer's decision is about the work, never an approval (4a-3).
        nouns = (_SITE_ENGINEER_VERDICT_NOUNS
                 if step.party == APPROVAL_PARTY_SITE_ENGINEER else _VERDICT_NOUNS)
        message = (f'{_name(recorder)} recorded your '
                   f'{nouns.get(step.verdict, step.verdict)} on '
                   f'"{approval.title}" from '
                   f'{_CHANNEL_PHRASES.get(step.proxy_channel, step.proxy_channel)}.')
        # Told nothing else in this action: E1 and E2 above excluded the decider.
        _send(approval, [_Notice(decider, T_PROXY, message=message)], {actor_pk},
              recorder)


def after_resubmit(approval_pk, round_no, actor_pk, replaced_step_pks=()):
    """E1 for the new round's active steps, the replaced-approver notice (ruling 5),
    and E5 for each kept approval."""
    approval = _request(approval_pk)
    steps = _steps(request=approval, round=round_no)
    actor = UserProfile.objects.select_related('user').get(pk=actor_pk)
    live = {s.party: s for s in steps if s.verdict != APPROVAL_STEP_SUPERSEDED}

    activated = [s for s in steps if s.activated_at is not None
                 and s.carried_from_id is None and s.verdict != APPROVAL_STEP_SUPERSEDED]
    notices = _activated_notices(
        approval, activated,
        f'{_name(actor)} revised it (round {round_no}) and asks you')

    for old in _steps(pk__in=list(replaced_step_pks), activated_at__isnull=False):
        new = live.get(old.party)
        party = _PARTY_LABELS.get(old.party, old.party)
        successor = f' {_name(new.assignee)} is asked instead.' if new else ''
        message = (f'You are no longer asked to approve "{approval.title}". '
                   f'{_name(actor)} named a different {party} for round {round_no}.'
                   f'{successor}')
        notices.append(_Notice(old.assignee, T_REPLACED, message=message))

    for kept in (s for s in steps if s.carried_from_id is not None):
        message = (f'{_name(actor)} kept your approval of "{approval.title}" for round '
                   f'{round_no}, so you are not asked again. '
                   f'Reason: "{kept.carry_reason}"')
        notices.append(_Notice(kept.decided_by, T_KEPT, message=message))

    _send(approval, notices, {actor_pk}, actor)


def after_withdraw(approval_pk, actor_pk):
    """E3 to each assignee whose ACTIVE pending step the withdrawal superseded."""
    approval = _request(approval_pk)
    steps = _steps(request=approval, round=approval.current_round,
                   verdict=APPROVAL_STEP_SUPERSEDED, superseded_by_id=actor_pk,
                   superseded_at=approval.closed_at, activated_at__isnull=False)
    if not steps:
        return
    actor = steps[0].superseded_by
    values = dict(withdrawn_by_name=_name(actor), approval_title=approval.title,
                  kind_label=_kind(approval), reason=approval.withdrawal_note)
    # A Site Engineer's E3 is its own event, carrying the work summary, not the bill's
    # (4a-3, T3c Q1).
    notices = [_Notice(s.assignee, T_WITHDRAWN,
                       event=('approval_withdrawn_work'
                              if s.party == APPROVAL_PARTY_SITE_ENGINEER
                              else 'approval_withdrawn'),
                       values=values, work_only=s.party == APPROVAL_PARTY_SITE_ENGINEER)
               for s in steps]
    _send(approval, notices, {actor_pk}, actor)


def after_reassign(old_step_pk, new_step_pk, actor_pk):
    """E1 to the new assignee (if their step is active now), E4 to the old one (if
    theirs had been)."""
    old = (ApprovalStep.objects
           .select_related('assignee__user', 'superseded_by__user').get(pk=old_step_pk))
    approval = _request(old.request_id)
    actor = old.superseded_by
    new_steps = _steps(pk=new_step_pk, activated_at__isnull=False)
    notices = _activated_notices(approval, new_steps,
                                 f'{_name(actor)} reassigned it to you and asks you')
    if old.activated_at is not None:
        new = new_steps[0] if new_steps else ApprovalStep.objects.select_related(
            'assignee__user').get(pk=new_step_pk)
        message = (f'You are no longer asked to approve "{approval.title}". '
                   f'{_name(actor)} reassigned it to {_name(new.assignee)}. '
                   f'Reason: "{old.note}"')
        notices.append(_Notice(old.assignee, T_REASSIGNED_AWAY, message=message))
    _send(approval, notices, {actor_pk}, actor)
