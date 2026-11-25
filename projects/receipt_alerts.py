"""
FIN-M-B5-4b — the alerts a client receipt sends (ruling FR36, 25 Nov 2026).

One function per event. Each builds the recipients and the values, then calls
notify.notify(), which sends AFTER COMMIT: the receipt writer (projects/receipts.py)
calls these inside its own transaction.atomic(), after the change, so a refused or
rolled-back action sends nothing. No second on_commit here — notify() already is one.

    receipt_recorded()     -> receipt_to_confirm           every active Finance user
    receipt_queried()      -> receipt_queried              the recorder (G4 fallback)
    receipt_rejected()     -> receipt_rejected             the recorder (G4 fallback)
    receipt_confirmed()    -> receipt_payment_received     PM side, site BD, CEO
    receipt_unconfirmed()  -> receipt_confirmation_undone  the same, plus the recorder

answer_receipt_query() and complete_receipt_amount() send nothing (G10).

RULES EVERY FUNCTION FOLLOWS:
  * G3 — active people only, never the person who acted, one alert per person.
    notify()'s resolve_recipients() applies all three (both active flags, the actor
    dropped, de-duplicated by pk), so a list built here may hold someone twice.
  * G5 — a test site (Project.is_test) sends nothing. NEW: no alert before this one
    skipped test sites (only figures did). A Cancelled site sends as normal (FR28).
  * G6 — site code, milestone, amount (the shared rupee formatter), date received, the
    actor's name and the reason or note. Never the proof file's name (its text is the
    uploader's). Links are relative; notify() makes the email's absolute.
  * G8 — CLIENT_RECEIPTS_LIVE is not read: the writer does not read it either.

Role strings stay in permissions.py: recipients come from its helpers
(active_finance_users, active_ceos, project_managers, site_assigned_pm,
profile_is_active_bd).
"""
import functools
import logging

from django.db import transaction
from django.urls import reverse

from .money_format import format_rupees
from .notify import notify
from .permissions import (
    active_ceos, active_finance_users, profile_is_active_bd, project_managers,
    site_assigned_pm,
)
from .site_bd import display_name

logger = logging.getLogger(__name__)

TO_CONFIRM = 'receipt_to_confirm'
QUERIED = 'receipt_queried'
REJECTED = 'receipt_rejected'
PAYMENT_RECEIVED = 'receipt_payment_received'
CONFIRMATION_UNDONE = 'receipt_confirmation_undone'
EVENTS = (TO_CONFIRM, QUERIED, REJECTED, PAYMENT_RECEIVED, CONFIRMATION_UNDONE)

#: The milestone card on the site overview — the link for the PM side, BD and CEO, who
#: may not all open a single receipt (F5: CEO never; the BD only on M1). G6 ruling (a).
MILESTONE_CARD_ANCHOR = '#payment-milestones'

#: G4 wording, shown only to someone told INSTEAD of the recorder.
RECORDER_INACTIVE = 'Sent to you because this receipt\'s recorder, {name}, is no longer active.'
RECORDER_BLANK = 'Sent to you because this receipt has no recorder on file.'
NO_RECIPIENT = 'receipt_alerts: %s for receipt %s on %s found no recipient'


def _is_active(profile):
    """Profile and login both active — the pair every recipient query uses."""
    return profile is not None and profile.is_active and profile.user.is_active


def _day(value):
    """'12 Nov 2026', the date style of the other Payments alerts (invoice_due)."""
    return f'{value.day} {value:%b %Y}'


def _common(receipt, project):
    """The values every receipt event declares."""
    return {
        'site_code': project.project_id,
        'milestone': receipt.milestone.milestone_name,
        'amount': format_rupees(receipt.amount),
        'date_received': _day(receipt.date_received) if receipt.date_received else 'blank',
    }


def _receipt_link(project, receipt):
    """The receipt page: Finance and the recorder (or whoever may record now) open it."""
    return reverse('receipt_detail', args=[project.project_id, receipt.pk])


def _milestone_card_link(project):
    return reverse('project_overview', args=[project.project_id]) + MILESTONE_CARD_ANCHOR


def _money_side(project):
    """FR36 item 4's people: the PM side (assigned PM + active coordinators, the FR8
    helper), the site's BD when set and active, and every active CEO. Inactive people
    in the list are dropped by notify() as well; checking the BD here keeps a blank or
    deactivated seat from costing a lookup later."""
    people = list(project_managers(project))
    if profile_is_active_bd(project.bd):
        people.append(project.bd)
    # user is loaded with them because resolve_recipients() reads user.is_active.
    people += list(active_ceos().select_related('user'))
    return people


def _recorder_or_fallback(receipt, project, actor):
    """(recipients, fallback_note) for Queried and Rejected (G4, rulings 25 Nov).

    The recorder while active. Otherwise — deactivated, or a migrated row with none —
    the first that exists of: the legacy completer (completed_by) if active, the site's
    assigned PM if active, its active coordinators. Each fallback carries a line saying
    why they were told. Nobody at all -> ([], ...) and the caller logs one line.

    SKIP THE ACTOR (ruling Q1, 25 Nov): a fallback who is the person who acted (e.g. the
    completer rejecting the row) is passed over and the chain goes on to the next step;
    G3 still keeps them out, but the chain does not stop at them. The recorder step is
    NOT a fallback: an active recorder who is the actor (a Finance recorder rejecting
    their own receipt) is chosen and then dropped by notify(), so nobody is told."""
    recorder = receipt.recorded_by
    if _is_active(recorder):
        return [recorder], ''
    note = (RECORDER_INACTIVE.format(name=display_name(recorder)) if recorder is not None
            else RECORDER_BLANK)
    actor_pk = getattr(actor, 'pk', None)

    def usable(profile):
        return _is_active(profile) and profile.pk != actor_pk

    if usable(receipt.completed_by):
        return [receipt.completed_by], note
    pm = site_assigned_pm(project)
    if pm is not None and pm.pk != actor_pk:
        return [pm], note
    # Active coordinators, the actor excluded here too, so a list holding only the actor
    # counts as nobody (and is logged) rather than reaching notify() to be emptied there.
    coordinators = [c for c in project.coordinators.filter(is_active=True, user__is_active=True)
                    .select_related('user') if c.pk != actor_pk]
    return coordinators, note


def _never_raises(func):
    """An alert can never undo the money action that fired it (notify()'s own rule,
    extended to the recipient queries here). The body runs in a savepoint, so a fault —
    a bug, or a failed query — rolls back only the alert (its scheduled send included,
    as Django discards a rolled-back savepoint's on_commit callbacks) and is logged; the
    writer's transaction goes on and commits."""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            with transaction.atomic():
                func(*args, **kwargs)
        except Exception:
            logger.exception('receipt_alerts: %s failed', func.__name__)
    return wrapper


# ---------------------------------------------------------------------------
# One function per event, called by the writer inside its transaction
# ---------------------------------------------------------------------------

@_never_raises
def receipt_recorded(receipt, project, actor):
    """Receipt to confirm (FR36 item 1): every active Finance user except the recorder
    (notify() drops the actor). A PM or BD recorder leaves every Finance user in."""
    if project.is_test:
        return
    values = _common(receipt, project)
    values['recorded_by_name'] = display_name(actor)
    values['record_note_line'] = (f"Recorder's note: {receipt.note}" if receipt.note else '')
    notify(TO_CONFIRM, recipients=list(active_finance_users().select_related('user')),
           actor=actor, related_project=project, link=_receipt_link(project, receipt),
           values=values)


def _to_recorder(event_code, receipt, project, actor, name_key, reason):
    """Queried / Rejected: the recorder, or the G4 fallback with its line."""
    if project.is_test:
        return
    recipients, note = _recorder_or_fallback(receipt, project, actor)
    if not recipients:
        logger.warning(NO_RECIPIENT, event_code, receipt.pk, project.project_id)
        return
    values = _common(receipt, project)
    values[name_key] = display_name(actor)
    values['reason'] = reason
    values['fallback_note'] = note
    notify(event_code, recipients=recipients, actor=actor, related_project=project,
           link=_receipt_link(project, receipt), values=values)


@_never_raises
def receipt_queried(receipt, project, actor, reason):
    """Receipt queried (FR36 item 2): the recorder, with the reason and the link."""
    _to_recorder(QUERIED, receipt, project, actor, 'queried_by_name', reason)


@_never_raises
def receipt_rejected(receipt, project, actor, reason):
    """Receipt rejected (FR36 item 3): the recorder, with the reason."""
    _to_recorder(REJECTED, receipt, project, actor, 'rejected_by_name', reason)


@_never_raises
def receipt_confirmed(receipt, project, actor):
    """Payment received (FR36 item 4), once per confirmed receipt: PM side, site BD, CEO.
    The confirmer never receives it (notify() drops the actor); a PM who recorded it
    still does, since Finance acted."""
    if project.is_test:
        return
    values = _common(receipt, project)
    values['confirmed_by_name'] = display_name(actor)
    values['over_note_line'] = (
        f'Confirmed over the agreed amount: {receipt.overpayment_note}'
        if receipt.overpayment_note else '')
    notify(PAYMENT_RECEIVED, recipients=_money_side(project), actor=actor,
           related_project=project, link=_milestone_card_link(project), values=values)


@_never_raises
def receipt_unconfirmed(receipt, project, actor, note):
    """Confirmation undone (FR36 item 5): Payment received's people plus the recorder,
    the undoer excluded. No G4 substitute for a missing recorder (ruling 25 Nov): the
    PM side already receives it."""
    if project.is_test:
        return
    recipients = _money_side(project)
    if _is_active(receipt.recorded_by):
        recipients.append(receipt.recorded_by)
    values = _common(receipt, project)
    values['undone_by_name'] = display_name(actor)
    values['note'] = note
    notify(CONFIRMATION_UNDONE, recipients=recipients, actor=actor, related_project=project,
           link=_milestone_card_link(project), values=values)
