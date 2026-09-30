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

  * payment_type_q() / payment_project_type() — a payment's project type, from its order
    or (5a) its contractor bill's site. Every tab, tile and count filters through them.

  * ceo_payment_strip() — the CEO Tenders strip (S2): the same scope as payment_counts()
    with test-site payments dropped, in four buckets. A separate function, so the
    queue and Finance keep counting every row.

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
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Count, DecimalField, Exists, F, Min, OuterRef, Q, Sum
from django.db.models.functions import Coalesce
from django.urls import reverse
from django.utils import timezone

from .models import (
    PaymentRequest, PaymentRequestHold, UserProfile, VendorOrderSite,
    effective_amount_sum, log_activity,
)
from .notifications import send_notification
from .permissions import user_can_mark_paid
from .utils import record_transition

logger = logging.getLogger(__name__)


#: One status's figures: how many requests, what they stand for (effective_amount, O4b)
#: and what was asked for (amount). The two sums differ only where an approval was for
#: less than the request — the screens then show both.
PaymentTotals = namedtuple('PaymentTotals', 'count amount requested')


def payment_type_q(project_types):
    """A Q over PaymentRequest: the payment's project type is one of `project_types`.

    THE ONE DEFINITION of a payment's project type (5a), which every tab, tile and count
    filters through: the ORDER's type for a PO/PI payment, the BILL's site's type for a
    contractor bill's payment (D-A10: one site per bill). Exactly one of the two is set
    (payment_request_order_xor_bill), and both arms are many-to-one, so the OR can never
    repeat a row. Never `PaymentRequest.project`, the nullable display anchor (O2d).
    """
    types = list(project_types)
    return (Q(vendor_order__project_type__in=types)
            | Q(contractor_bill__project__project_type__in=types))


def payment_project_type(payment):
    """payment_type_q() for one row in hand: the order's type, or the bill site's type.
    Costs no query when the caller select_related the side it reads."""
    if payment.contractor_bill_id is not None:
        return payment.contractor_bill.project.project_type
    return payment.vendor_order.project_type


def payment_counts(project_types=None):
    """{status: PaymentTotals} for every one of the five statuses, zero-filled. ONE query.

    THE SCOPE IS THE ORDER'S — or, for a contractor bill's payment (5a), the bill's site's:
    `project_types` filters through payment_type_q() (None = every type); nothing here
    joins through `PaymentRequest.project`, which is a nullable display anchor (O2d), and
    nothing filters on a project's status. A site-less payment, and a payment on a Draft
    OPEX site, are counted like any other — each is money somebody has to approve or pay.
    Test sites are NOT excluded, for orders and bills alike (audit I; only the CEO strip
    drops them).

    `amount` sums effective_amount, never `amount` (O4b): an approved figure totals what
    was approved. `requested` sums what was asked for, so a screen can show both.
    """
    queryset = PaymentRequest.objects.all()
    if project_types is not None:
        queryset = queryset.filter(payment_type_q(project_types))
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


#: One card of the CEO payment strip (S2). `days` is the oldest raise (awaiting) or the
#: longest open hold (on_hold), None where the card shows no age; `partial` is how many
#: paid requests were approved for less than asked (paid_month only), else None.
StripBucket = namedtuple('StripBucket', 'count amount days partial')


def ceo_payment_strip(project_types, today):
    """The CEO Tenders payment strip (S2): {'awaiting', 'on_hold', 'approved',
    'paid_month'} -> StripBucket. TWO queries.

    SCOPED LIKE payment_counts() — through payment_type_q(), never through `project` —
    PLUS ONE TERM payment_counts() does not have: a request is dropped when EVERY site its
    order was sized against is test data (Project.is_test). A request whose order names at
    least one real site is kept, and so is one whose order names no site at all: "no
    project link" is not evidence of test data. A contractor bill's payment (5a) is
    dropped when the bill's one site is test data — the same meaning, ruling Q2.
    payment_counts() itself is left alone because the queue and Finance must keep
    counting every row.

    WHICH AMOUNT EACH CARD SUMS IS A DECISION, NOT AN ACCIDENT:
      awaiting / on_hold — `amount`, what SCM asked for. A request re-awaiting after a
        hold on an approval carries an approved ceiling, but nobody has approved it now.
      approved / paid_month — effective_amount (the approved figure, O4b), the money
        that is due or that left.
    REJECTED is on no card.

    `today` is the IST calendar date (the caller passes timezone.localdate()). Ages are
    whole IST days, and "this month" is today's IST month, compared against
    `payment_date` — the date Finance recorded the money leaving, not the moment the
    row was marked paid.
    """
    queryset = PaymentRequest.objects.all()
    if project_types is not None:
        queryset = queryset.filter(payment_type_q(project_types))
    # Two Exists rather than a join through vendor_order__sites: a join multiplies each
    # request by its site count and every Sum below would be counted that many times.
    # A bill's payment has no order sites, so this term never drops it; the next one does.
    order_sites = VendorOrderSite.objects.filter(order=OuterRef('vendor_order'))
    queryset = queryset.exclude(
        Exists(order_sites) & ~Exists(order_sites.filter(project__is_test=False)))
    # A bill names one site through a many-to-one chain, so this is a join, not a
    # multiplier; a PO/PI payment has no bill and is never dropped by it.
    queryset = queryset.exclude(contractor_bill__project__is_test=True)

    month_start = today.replace(day=1)
    next_month_start = (month_start + timedelta(days=32)).replace(day=1)

    awaiting = Q(status=PaymentRequest.PENDING_APPROVAL)
    on_hold  = Q(status=PaymentRequest.ON_HOLD)
    approved = Q(status=PaymentRequest.APPROVED)
    paid     = Q(status=PaymentRequest.CONFIRMED, payment_date__gte=month_start,
                 payment_date__lt=next_month_start)
    # effective_amount as SQL — the expression models.effective_amount_sum() sums,
    # restated because that helper takes no filter and models.py was outside S2.
    effective = Coalesce('approved_amount', 'amount',
                         output_field=DecimalField(max_digits=14, decimal_places=2))
    agg =queryset.aggregate(
        awaiting_n=Count('pk', filter=awaiting),
        awaiting_sum=Sum('amount', filter=awaiting),
        awaiting_oldest=Min('requested_date', filter=awaiting),
        hold_n=Count('pk', filter=on_hold),
        hold_sum=Sum('amount', filter=on_hold),
        approved_n=Count('pk', filter=approved),
        approved_sum=Sum(effective, filter=approved),
        paid_n=Count('pk', filter=paid),
        paid_sum=Sum(effective, filter=paid),
        paid_partial=Count('pk', filter=paid & Q(approved_amount__lt=F('amount'))),
    )
    # Second query, on the holds themselves: an on-hold request has exactly one open
    # hold (uniq_open_hold_per_payment_request), and joining holds into the aggregate
    # above would repeat each request once per hold it has EVER had, inflating hold_sum.
    held_since = (PaymentRequestHold.objects
                  .filter(responded_at__isnull=True,
                          payment_request__in=queryset.filter(on_hold))
                  .aggregate(m=Min('held_at'))['m'])

    def _age(moment):
        return (today - timezone.localdate(moment)).days if moment else None

    zero = Decimal('0')
    return {
        'awaiting':   StripBucket(agg['awaiting_n'], agg['awaiting_sum'] or zero,
                                  _age(agg['awaiting_oldest']), None),
        'on_hold':    StripBucket(agg['hold_n'], agg['hold_sum'] or zero,
                                  _age(held_since), None),
        'approved':   StripBucket(agg['approved_n'], agg['approved_sum'] or zero, None, None),
        'paid_month': StripBucket(agg['paid_n'], agg['paid_sum'] or zero, None,
                                  agg['paid_partial']),
    }


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


def _payment_link(payment):
    """Where a payment's notice points: its order's page, or — for a contractor bill's
    payment (5a) — the bill's approval page. Reads the bill row the caller
    select_related, so no query."""
    if payment.contractor_bill_id is not None:
        return reverse('approval_detail', args=[payment.contractor_bill.request_id])
    return reverse('vendor_order_detail', args=[payment.vendor_order_id])


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
        # contractor_bill rides the same query (a LEFT JOIN, null on a PO/PI payment) so
        # _payment_link() reads a bill's request id without a second query.
        payment = (PaymentRequest.objects
                   .select_related('vendor', 'project', 'requested_by__profile',
                                   'contractor_bill')
                   .filter(pk=transition.subject_id).first())
        if payment is None:
            return
        recipients, message = _notice(transition, payment)
        # A LINK THAT CANNOT BE BUILT NEVER COSTS THE NOTICE (5a ruling). It used to sit
        # inside this try with nothing else guarding it, so a failing reverse() logged one
        # line and told nobody anything. The link is a convenience; the message is the
        # notice — so the failure is logged and the notice goes out without a link.
        try:
            link = _payment_link(payment)
        except Exception as exc:
            logger.error('send_payment_notices: no link for payment %s — %s', payment.pk, exc)
            link = ''
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
