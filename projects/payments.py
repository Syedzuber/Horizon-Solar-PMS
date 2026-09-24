"""
O5 — marking a vendor payment paid, and the in-app notices that follow a payment's moves.
O6 — the one count of payments by status that every payment figure reads.

Three things live here, and all three are here because more than one door reaches them:

  * mark_payment_paid() — THE ONE WRITE of APPROVED -> CONFIRMED. Since O6 the Finance
    queue's mark-paid form (payment_views.payment_mark_paid) is its only caller: the
    project page's confirm button was retired, so there is one path to pay, not two.

  * payment_counts() — per-status counts and sums. The queue's tabs and tiles, the Finance
    dashboard's payment tiles and the CEO dashboard's finance tile all call it, so the
    three figures cannot disagree.

  * send_payment_notices() — who hears about a payment's move. It is called from ONE
    place: a post_save receiver on StatusTransition (signals.py), because a payment's
    status only ever moves where record_transition() writes its ledger row. Hooking the
    notice there, rather than in each view, is what lets the four O4 action views stay
    exactly as O4 left them while still telling SCM about a hold and the approvers about
    an answer.

Neither function imports views: views.py imports this module, and a cycle would follow.
"""
import logging
from collections import namedtuple
from decimal import Decimal

from django.db import transaction
from django.db.models import Count, Sum
from django.urls import reverse
from django.utils import timezone

from .models import PaymentRequest, UserProfile, effective_amount_sum, log_activity
from .notifications import send_notification
from .permissions import user_can_mark_paid
from .utils import record_transition

logger = logging.getLogger(__name__)


#: One status's figures: how many requests, what they stand for (effective_amount, O4b)
#: and what was asked for (amount). The two sums differ only where an approval was for
#: less than the request — the screens then show both.
PaymentTotals = namedtuple('PaymentTotals', 'count amount requested')


def payment_counts(project_types=None):
    """{status: PaymentTotals} for every one of the five statuses, zero-filled. ONE query.

    THE SCOPE IS THE ORDER'S. `project_types` filters on `vendor_order__project_type`
    (None = every type); nothing here joins through `PaymentRequest.project`, which is a
    nullable display anchor (O2d), and nothing filters on a project's status. A site-less
    payment, and a payment on a Draft OPEX site, are counted like any other — each is
    money somebody has to approve or pay.

    `amount` sums effective_amount, never `amount` (O4b): an approved figure totals what
    was approved. `requested` sums what was asked for, so a screen can show both.
    """
    queryset = PaymentRequest.objects.all()
    if project_types is not None:
        queryset = queryset.filter(vendor_order__project_type__in=list(project_types))
    rows = (queryset.values('status')
            .annotate(n=Count('pk'), effective_sum=effective_amount_sum(),
                      requested_sum=Sum('amount'))
            .order_by())
    zero = Decimal('0')
    counts = {status: PaymentTotals(0, zero, zero)
              for status, _ in PaymentRequest.STATUS_CHOICES}
    for row in rows:
        counts[row['status']] = PaymentTotals(
            row['n'], row['effective_sum'] or zero, row['requested_sum'] or zero)
    return counts


class PaymentRefused(Exception):
    """mark_payment_paid() refused. Nothing was written; str(exc) is the message for the
    person who asked. A refusal, never a 500 — the caller turns it into a message and a
    redirect."""


def _mark_paid_refusal(user, payment):
    """Why `user` may not mark `payment` paid, in words, or None when they may.

    The DECISION is user_can_mark_paid(); this only chooses which sentence to say when it
    refuses, most specific first, so a Finance user who approved the request is told that
    rather than a generic "not allowed".
    """
    if user_can_mark_paid(user, payment):
        return None
    profile = getattr(user, 'profile', None)
    if payment.status != PaymentRequest.APPROVED:
        return (f'This payment request is now "{payment.get_status_display()}" and '
                f'cannot be marked paid.')
    if profile is not None and payment.approved_by_id == profile.pk:
        return 'You approved this request, so another Finance user must mark it paid.'
    if payment.requested_by_id == user.pk:
        return 'You raised this request, so another Finance user must mark it paid.'
    return 'Only Finance marks a payment paid.'


def mark_payment_paid(payment, actor, payment_date, reference):
    """Record `payment` as PAID by `actor` (an auth.User) on `payment_date`, with
    `reference` (UTR / cheque number). Returns the updated row; raises PaymentRefused.

    THE REFERENCE IS MANDATORY HERE, and it was optional before O5: a payment with no
    reference cannot be matched to a bank line, and "paid" with nothing to trace it by is
    a claim, not a record. Rows confirmed before O5 legitimately carry none.

    THE DATE may not be in the future and may not precede the day the request was raised
    — money cannot leave before anyone asked for it. The calendar range itself (2020 to
    today + 5 years) is the caller's check_typed_date(), which runs first.

    ONE TRANSACTION: select_for_update() on the request alone (no join — see
    order_views._locked_payment for why), user_can_mark_paid() RE-CHECKED INSIDE THE LOCK,
    the four fields, and record_transition(APPROVED -> CONFIRMED) with the reference as
    its remark. Two Finance users pressing at once serialise here, and the second sees
    CONFIRMED and is refused. The feed line is written after the commit, as everywhere in
    the order module, because log_activity() never raises.

    NO AMOUNT IS WRITTEN (O4b). What is paid is the request's effective_amount — the
    approved amount, which an APPROVED row always carries — and every "paid" figure sums
    exactly that over CONFIRMED rows. Finance cannot change it here.
    """
    reference = (reference or '').strip()
    if not reference:
        raise PaymentRefused('A payment reference (UTR or cheque number) is required.')
    if len(reference) > 100:
        raise PaymentRefused('The payment reference is at most 100 characters.')
    if payment_date is None:
        raise PaymentRefused('A payment date is required.')
    if payment_date > timezone.localdate():
        raise PaymentRefused('The payment date cannot be in the future.')

    profile = actor.profile
    with transaction.atomic():
        locked = PaymentRequest.objects.select_for_update().get(pk=payment.pk)
        refusal = _mark_paid_refusal(actor, locked)
        if refusal:
            raise PaymentRefused(refusal)
        raised_on = timezone.localdate(locked.requested_date)
        if payment_date < raised_on:
            raise PaymentRefused(
                f'The payment date cannot be before the request was raised '
                f'({raised_on:%d %b %Y}).')
        locked.status            = PaymentRequest.CONFIRMED
        locked.payment_date      = payment_date
        locked.payment_reference = reference
        locked.confirmed_by      = actor
        locked.save(update_fields=['status', 'payment_date', 'payment_reference',
                                   'confirmed_by'])
        record_transition(locked, to_status=PaymentRequest.CONFIRMED,
                          from_status=PaymentRequest.APPROVED, actor=profile,
                          remark=reference, project=locked.project)

    vendor = payment.vendor.name if payment.vendor_id else 'vendor'
    log_activity(
        locked.project, profile,
        f'Marked paid: ₹{locked.effective_amount} to {vendor} (Ref: {reference})',
        entity_type='PaymentRequest', entity_id=locked.pk,
        action_code='payment_request_paid',
    )
    return locked


# ---------------------------------------------------------------------------
# In-app notices
# ---------------------------------------------------------------------------

def _approvers_except(payment):
    """Everyone who could approve `payment`: active holders of the flag, minus the person
    who raised it — the same-person term of user_can_approve_payment(), asked of the
    whole set at once."""
    return list(UserProfile.objects
                .filter(is_payment_approver=True, is_active=True)
                .exclude(user_id=payment.requested_by_id)
                .select_related('user'))


def _requester(payment):
    profile = getattr(payment.requested_by, 'profile', None)
    return [profile] if profile is not None and profile.is_active else []


def _name(profile):
    if profile is None:
        return 'Someone'
    return profile.user.get_full_name() or profile.user.username


def _notice(transition, payment):
    """(recipients, message) for one ledger row, or ([], '') when the move tells nobody.

    Five moves notify; a full approval and a rejection do not (not asked for in O5 — O7
    decides). The raise and SCM's answer both land on PENDING_APPROVAL, so FROM decides
    which one this is.

    A PARTIAL APPROVAL tells the requester (O4b): they asked for more than they got. It is
    told apart from a full one by the row itself, read after commit — approved_amount
    below amount — so the approve view needed no signal of its own.
    """
    amount = f'₹{payment.amount}'
    vendor = payment.vendor.name if payment.vendor_id else 'vendor'
    to, frm = transition.to_status, transition.from_status

    if to == PaymentRequest.ON_HOLD:
        return (_requester(payment),
                f'Payment of {amount} to {vendor} is on hold: {transition.remark}')
    if to == PaymentRequest.PENDING_APPROVAL and frm == PaymentRequest.ON_HOLD:
        return (_approvers_except(payment),
                f'{_name(transition.actor)} responded to the hold on {amount} to {vendor}')
    if to == PaymentRequest.PENDING_APPROVAL and not frm:
        requester = payment.requested_by
        who = requester.get_full_name() or requester.username
        return (_approvers_except(payment),
                f'{who} raised a payment of {amount} to {vendor} for approval')
    if to == PaymentRequest.APPROVED and payment.is_partially_approved:
        return (_requester(payment),
                # The remark already reads "approved ₹X of ₹Y: <reason>".
                f'Payment request to {vendor} {transition.remark}')
    if to == PaymentRequest.CONFIRMED:
        return (_requester(payment),
                f'Payment of ₹{payment.effective_amount} to {vendor} was paid on '
                f'{payment.payment_date:%d %b %Y} (Ref: {payment.payment_reference})')
    return [], ''


def send_payment_notices(transition):
    """Send the in-app notices for one payment_request ledger row. Called after commit by
    the StatusTransition receiver in signals.py, so a rolled-back move tells nobody.

    NEVER TO THE ACTOR. The same-person rules already keep the actor out of most of these
    sets; the exclusion here is the guarantee, not the predicates.

    NEVER RAISES. It runs after the move has committed, and a notice that fails must not
    turn a recorded payment into an error page — the same promise send_notification()
    makes for its own channels.
    """
    try:
        payment = (PaymentRequest.objects
                   .select_related('vendor', 'project', 'requested_by__profile')
                   .filter(pk=transition.subject_id).first())
        if payment is None:
            return
        recipients, message = _notice(transition, payment)
        link = reverse('vendor_order_detail', args=[payment.vendor_order_id])
        for recipient in recipients:
            if recipient.pk == transition.actor_id:
                continue
            # In-app only. Channels widen in O7, with the new payment templates.
            send_notification(
                recipient=recipient, message=message, channels=['in_app'], link=link,
                related_project=payment.project, actor=transition.actor,
            )
    except Exception as exc:
        logger.error('send_payment_notices: transition %s — %s', transition.pk, exc)
