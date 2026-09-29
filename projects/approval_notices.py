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

LINKS. The in-app link is relative (/approvals/<pk>/). The email's absolute link is
built with utils._abs_url(link, None): there is no HTTP request in approvals.py, so the
host is SITE_BASE_URL, production's, even for a send from a local database (ruling 1).
`message` is both the bell text and the email's plain-text part, so it carries no URL;
the absolute URL appears only in the HTML part, rendered from
projects/email/approval_notice.html (autoescaped).

MATERIAL LINES (28 Sep 2026). Every approval email of a material request carries one line
saying what it is about — "120 Nos — Solar module 540W, +2 more" (lines_summary): the
first line, and how many others. In BOTH email parts: the HTML part draws it as a row
(approval_notice.html), and the plain-text part is `message` plus a "Material: ..." line.
The bell text stays `message` alone, one sentence — so an email that carries a summary is
sent in a call of its own (_send), and the in-app notice in another.

CONTRACTOR BILLS (4a-2). A bill's emails carry a one-line bill summary in the same place
and the same way — "Civil Co · HRP-RES-2026-001 · bill CB-7 · ₹12,500.00" (bill_summary):
the contractor, the site, the contractor's bill number and the amount — labelled "Bill"
instead of "Material". Every bill email carries it (E1, E2 and E3, ruling 4). NO EMAIL AND
NO IN-APP NOTICE EVER CARRIES THE BILL PDF, A LINK TO IT, OR WHERE IT IS STORED: the summary
is built from those four values only, and this module does not import bill_storage.

THE SITE ENGINEER CONFIRMS THE WORK, NEVER THE BILL (4a-3, D-A53). A notice to a Site
Engineer — asked to confirm (E1), or told the bill was withdrawn (E3) — carries no amount,
bill number or PDF: its email summary is the work — contractor · site · tasks
("Foundation, +1 more", work_summary), labelled "Work". It asks them "to confirm the
work", never to approve. The PM, SCM and the raiser keep the full bill summary.
"""
import logging

from django.template.loader import render_to_string
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
from .units import format_quantity
from .utils import _abs_url

logger = logging.getLogger(__name__)

IN_APP = ['in_app']
IN_APP_AND_EMAIL = ['in_app', 'email']

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
    """One message to one person. `html` is None for an in-app-only notice.

    `work_only` (4a-3): the recipient is told as the bill's Site Engineer, so an email
    carries the work summary, never the bill's (D-A53)."""

    def __init__(self, recipient, message, channels, template, subject='',
                 body='', quote_label='', quote='', work_only=False):
        self.work_only = work_only
        self.recipient = recipient
        self.message = message
        self.channels = channels
        self.template = template
        self.subject = subject
        self.body = body
        self.quote_label = quote_label
        self.quote = quote


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


def email_html(approval, subject, body, quote_label='', quote='', summary=None,
               summary_label=None):
    """The HTML part of an approval email. Autoescaped, so text a user typed arrives as
    text; the one place the absolute URL appears. Carries the request's one-line summary
    — its material lines, or a contractor bill's (4a-2): `summary` and `summary_label` if
    the caller already read them, otherwise email_summary()."""
    if summary is None:
        summary, summary_label = email_summary(approval)
    return render_to_string('projects/email/approval_notice.html', {
        'subject': subject, 'body': body, 'quote_label': quote_label, 'quote': quote,
        'lines_summary': summary, 'summary_label': summary_label or 'Material',
        'url': _abs_url(_link(approval), None),
    })


def _send(approval, notices, excluded, actor):
    """Send `notices` in order: the actor and anyone already told in this action are
    skipped, as is anyone inactive. One failing send is logged and the rest still go.

    A material request's email carries its line summary — a contractor bill's, its bill
    summary (4a-2) — in the plain-text part as well as the HTML part. send_notification() sends one `message` as both the bell text and
    the email's text part, so such an email goes in a second call with the summary
    appended, and the bell keeps `message` alone. Same channels, same log rows, same
    template name; only the email row's message differs."""
    told = set(excluded)
    link = _link(approval)
    # Read once per action per kind of reader — the bill's own, or its Site Engineer's
    # work-only line (4a-3) — and only if someone of that kind is emailed.
    summaries = {}
    for notice in notices:
        person = notice.recipient
        if not _is_active(person) or person.pk in told:
            continue
        told.add(person.pk)
        try:
            html = None
            channels, email_text = notice.channels, notice.message
            if 'email' in notice.channels:
                if notice.work_only not in summaries:
                    summaries[notice.work_only] = email_summary(approval, notice.work_only)
                summary, label = summaries[notice.work_only]
                html = email_html(approval, notice.subject, notice.body,
                                  notice.quote_label, notice.quote, summary=summary,
                                  summary_label=label)
                if summary:
                    channels = [c for c in notice.channels if c != 'email']
                    email_text = f'{notice.message}\n\n{label}: {summary}'
            if channels:
                send_notification(
                    recipient=person, message=notice.message, channels=channels,
                    link=link, subject=notice.subject, html_message=html,
                    template=notice.template, related_project=None, actor=actor)
            if channels != notice.channels:
                send_notification(
                    recipient=person, message=email_text, channels=['email'],
                    link=link, subject=notice.subject, html_message=html,
                    template=notice.template, related_project=None, actor=actor)
        except Exception:
            logger.exception('approval_notices: the %s notice on approval #%s to %s '
                             'failed; the approval action stands.',
                             notice.template, approval.pk, person.pk)


def _about(approval):
    """'"<title>" (<kind>)' — how every notice names the request."""
    return f'"{approval.title}" ({_KIND_LABELS.get(approval.kind, approval.kind)})'


def _activated_notices(approval, steps, asked_by):
    """E1 for `steps`: each assignee first, then each design step's deputy. `asked_by`
    is the sentence start saying who is asking, e.g. 'Sana asks you'."""
    subject = f'Approval needed: {approval.title}'
    notices, deputies = [], []
    for step in steps:
        party = _PARTY_LABELS.get(step.party, step.party)
        if step.party == APPROVAL_PARTY_SITE_ENGINEER:
            # The Site Engineer confirms the work, never the bill (D-A53, Q4).
            body = (f'{asked_by} to confirm the work on {_about(approval)}. Open it to '
                    f'confirm the work done, or say it is not done.')
            notices.append(_Notice(step.assignee, body, IN_APP_AND_EMAIL, T_ACTIVATED,
                                   subject=f'Work to confirm: {approval.title}', body=body,
                                   work_only=True))
            continue
        body = (f'{asked_by} to approve {_about(approval)} as the {party}. '
                f'Open it to approve, request changes or reject.')
        notices.append(_Notice(step.assignee, body, IN_APP_AND_EMAIL, T_ACTIVATED,
                               subject=subject, body=body))
        deputy = step.assignee.design_head_deputy if step.party == APPROVAL_PARTY_DESIGN \
            else None
        if deputy is not None and deputy.pk != approval.raised_by_id:
            body = (f'{_about(approval)} needs a Design Head decision. It is with '
                    f'{_name(step.assignee)}; as their named deputy you may decide it.')
            deputies.append(_Notice(deputy, body, IN_APP_AND_EMAIL, T_ACTIVATED_DEPUTY,
                                    subject=subject, body=body))
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
        if closed_as == APPROVAL_APPROVED:
            subject = f'Approved: {approval.title}'
            body = (f'{_about(approval)} is approved. {_name(decider)} gave the last '
                    f'approval, as the {party}.')
        elif closed_as == APPROVAL_CHANGES_REQUESTED                 and step.party == APPROVAL_PARTY_SITE_ENGINEER:
            # "Work not done" (D-A53): the Site Engineer's changes request, as History
            # words it.
            subject = f'Changes requested: {approval.title}'
            body = (f'{_name(decider)} said the work on {_about(approval)} is not done, as '
                    f'the {party}. Revise and resubmit it, or withdraw it.')
        elif closed_as == APPROVAL_CHANGES_REQUESTED:
            subject = f'Changes requested: {approval.title}'
            body = (f'{_name(decider)} requested changes to {_about(approval)} as the '
                    f'{party}. Revise and resubmit it, or withdraw it.')
        else:
            subject = f'Rejected: {approval.title}'
            body = f'{_name(decider)} rejected {_about(approval)} as the {party}.'
        message = f'{body} Note: "{step.note}"' if step.note else body
        notices.append(_Notice(approval.raised_by, message, IN_APP_AND_EMAIL,
                               T_CLOSED[closed_as], subject=subject, body=body,
                               quote_label='Note' if step.note else '',
                               quote=step.note))

    if closed_as in (APPROVAL_CHANGES_REQUESTED, APPROVAL_REJECTED):
        what = 'requested changes' if closed_as == APPROVAL_CHANGES_REQUESTED \
            else 'rejected'
        for other in _steps(request=approval, round=step.round,
                            verdict=APPROVAL_STEP_SUPERSEDED, superseded_by=decider,
                            superseded_at=step.decided_at, activated_at__isnull=False):
            message = (f'You no longer need to decide "{approval.title}". '
                       f'{_name(decider)} {what} as the {party}.')
            notices.append(_Notice(other.assignee, message, IN_APP, T_SUPERSEDED))

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
        _send(approval, [_Notice(decider, message, IN_APP, T_PROXY)], {actor_pk},
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
        notices.append(_Notice(old.assignee, message, IN_APP, T_REPLACED))

    for kept in (s for s in steps if s.carried_from_id is not None):
        message = (f'{_name(actor)} kept your approval of "{approval.title}" for round '
                   f'{round_no}, so you are not asked again. '
                   f'Reason: "{kept.carry_reason}"')
        notices.append(_Notice(kept.decided_by, message, IN_APP, T_KEPT))

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
    subject = f'Withdrawn: {approval.title}'
    body = f'{_name(actor)} withdrew {_about(approval)}. You no longer need to decide it.'
    note = approval.withdrawal_note
    # A Site Engineer's E3 email carries the work summary, not the bill's (4a-3).
    notices = [_Notice(s.assignee, f'{body} Reason: "{note}"', IN_APP_AND_EMAIL,
                       T_WITHDRAWN, subject=subject, body=body,
                       quote_label='Reason', quote=note,
                       work_only=s.party == APPROVAL_PARTY_SITE_ENGINEER)
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
        notices.append(_Notice(old.assignee, message, IN_APP, T_REASSIGNED_AWAY))
    _send(approval, notices, {actor_pk}, actor)
