"""
FIN-M-B5-2 — client payment receipts: THE ONLY WRITER of PaymentReceipt and
PaymentReceiptQuery, plus read helpers for the sessions that add screens (B5-3 onward).

THE LIFE OF A RECEIPT
---------------------
    record_receipt()           ''            -> recorded       (BD on M1 / PM side / Finance)
    query_receipt()            recorded      -> queried        (Finance, not the recorder)
    answer_receipt_query()     queried       -> recorded       (the recorder, or whoever may record now)
    confirm_receipt()          recorded      -> confirmed      (Finance, not the recorder) — FINAL
    reject_receipt()           recorded|queried -> rejected    (Finance, reason required) — FINAL
    complete_receipt_amount()  amount_needed -> recorded       (Finance types a legacy row's figures)

EVERY WRITER, THE SAME SHAPE: ask the permission helper (permissions.py) and validate the
input BEFORE anything is locked or uploaded; then, inside one transaction.atomic(), lock
the row(s) with select_for_update(), ask the permission helper AGAIN on the locked row (a
second Finance user may have moved it since the page was drawn), write, record exactly one
StatusTransition and one ActivityLog line. A refusal raises ReceiptRefused, whose message
is written for the person who asked (the B5-3/B5-4 forms show it as is), and writes
nothing. Every function returns a ReceiptResult: the receipt and any warnings.

LOGGING INSIDE THE TRANSACTION. log_activity() runs its INSERT in its own savepoint
(models.log_activity: `with transaction.atomic(): ActivityLog.objects.create(...)` inside
try/except), so a failed log line cannot poison this transaction, and a move that rolls
back leaves no log line behind. Hence no on_commit here.

WHAT THIS MODULE NEVER DOES (D11): write PaymentMilestone (status, amount_received,
received_date), touch the milestone's tasks, or send a notification. The old and new
money paths stay apart until FIN-M-B5-5.

LOCK ORDER: milestone before receipt wherever both are locked (record, confirm, complete),
so two writers can never wait on each other in opposite orders.
"""
import datetime
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Count, Q, Sum
from django.utils import timezone

from .delivery_storage import DeliveryStorageError
from .models import (
    REASON_CREATED, PaymentMilestone, PaymentReceipt, PaymentReceiptQuery, log_activity,
)
from .permissions import (
    user_can_answer_receipt_query, user_can_complete_receipt_amount,
    user_can_confirm_receipt, user_can_query_receipt, user_can_record_receipt,
    user_can_reject_receipt,
)
from .receipt_storage import discard_receipt_proof, store_receipt_proof
from .site_bd import display_name
from .utils import record_transition

#: ActivityLog.entity_type for every receipt line, so a reader can link it to the row.
RECEIPT_ENTITY = 'PaymentReceipt'

#: PaymentMilestone.amount's precision: 12 digits, 2 after the point.
AMOUNT_PLACES = Decimal('0.01')
AMOUNT_LIMIT = Decimal('10000000000')  # 10 integer digits: the largest a 12,2 column holds
REFERENCE_LIMIT = 100                   # PaymentReceipt.reference max_length

#: Receipts whose amount counts toward the milestone's total for the FR20 warning (Q10):
#: every receipt with an amount that is not rejected. amount_needed rows have none.
LIVE_STATUSES = (PaymentReceipt.RECORDED, PaymentReceipt.QUERIED, PaymentReceipt.CONFIRMED)
#: Recorded but not yet confirmed (FR2): shown apart from confirmed money.
UNCONFIRMED_STATUSES = (PaymentReceipt.RECORDED, PaymentReceipt.QUERIED)

NOT_ALLOWED = 'You are not allowed to do this on this receipt.'
STALE = ('This receipt changed while you were looking at it, or you may no longer act on '
         'it. Nothing was saved — reload and check its current state.')


class ReceiptRefused(Exception):
    """The write is not allowed or the input is wrong. The message is written for the
    person who asked and is safe to show them; nothing was written."""


@dataclass(frozen=True)
class ReceiptResult:
    """What a writer returns: the receipt as saved, and any warnings (FR20) to show."""
    receipt: PaymentReceipt
    warnings: tuple = ()


@dataclass(frozen=True)
class ReceiptFigures:
    """Validated figures for a receipt (D9)."""
    amount: Decimal
    date_received: datetime.date
    mode: str
    reference: str
    note: str


@dataclass(frozen=True)
class ReceiptTotals:
    """One milestone's receipts, summed (receipt_totals)."""
    confirmed: Decimal = Decimal('0.00')
    unconfirmed: Decimal = Decimal('0.00')
    amount_needed: int = 0


# ---------------------------------------------------------------------------
# Validation (D9) — before any lock or upload
# ---------------------------------------------------------------------------

def _clean_amount(amount):
    """The amount as a 2-place Decimal > 0 that fits the column, or ReceiptRefused (FR3)."""
    if amount is None or (isinstance(amount, str) and not amount.strip()):
        raise ReceiptRefused('Enter the amount received.')
    if isinstance(amount, float):
        # A float has already lost the paise (0.1 + 0.2); the forms hand over a Decimal
        # or text, so a float here is a caller's bug, refused rather than rounded.
        raise ReceiptRefused('The amount must be given as a number with at most two '
                             'decimal places.')
    try:
        value = Decimal(str(amount).strip().replace(',', ''))
    except (InvalidOperation, ValueError):
        raise ReceiptRefused('The amount must be a number, like 50000 or 50000.50.')
    if not value.is_finite():
        raise ReceiptRefused('The amount must be a number, like 50000 or 50000.50.')
    if value <= 0:
        raise ReceiptRefused('The amount must be more than zero.')
    if value != value.quantize(AMOUNT_PLACES):
        raise ReceiptRefused('The amount can have at most two decimal places (paise).')
    if value >= AMOUNT_LIMIT:
        raise ReceiptRefused('The amount is too large.')
    return value.quantize(AMOUNT_PLACES)


def _clean_date(date_received):
    """The date received, not after today in IST (D9, FR13), or ReceiptRefused (FR3).
    There is no lower bound: an old receipt may be recorded late."""
    if date_received is None or date_received == '':
        raise ReceiptRefused('Enter the date the money was received.')
    if isinstance(date_received, datetime.datetime):
        # A datetime is a date subclass; take its IST calendar day, never its UTC one.
        date_received = (timezone.localtime(date_received).date()
                         if timezone.is_aware(date_received) else date_received.date())
    if not isinstance(date_received, datetime.date):
        raise ReceiptRefused('The date received must be a date.')
    # timezone.localdate() is today in settings.TIME_ZONE (Asia/Kolkata): at 00:30 IST the
    # UTC date is still yesterday, and the IST day is the one the person means.
    if date_received > timezone.localdate():
        raise ReceiptRefused('The date received cannot be after today.')
    return date_received


def _clean_figures(amount, date_received, mode, reference, note):
    """Every D9 rule at once; returns ReceiptFigures or raises ReceiptRefused."""
    clean_amount = _clean_amount(amount)
    clean_date = _clean_date(date_received)
    if mode not in dict(PaymentReceipt.MODE_CHOICES):
        raise ReceiptRefused('Choose how the money was paid.')
    reference = (reference or '').strip()
    if len(reference) > REFERENCE_LIMIT:
        raise ReceiptRefused(f'The reference can be at most {REFERENCE_LIMIT} characters.')
    note = (note or '').strip()
    # D9/FR18: "Other" says nothing by itself; the note is what tells Finance what it was.
    if mode == PaymentReceipt.OTHER and not note:
        raise ReceiptRefused('Add a note saying how the money was paid when the mode is '
                             'Other.')
    return ReceiptFigures(clean_amount, clean_date, mode, reference, note)


def _required_text(text, message):
    """Stripped `text`, or ReceiptRefused(message) when blank."""
    text = (text or '').strip()
    if not text:
        raise ReceiptRefused(message)
    return text


# ---------------------------------------------------------------------------
# Shared pieces
# ---------------------------------------------------------------------------

def _lock_milestone(pk):
    """The milestone row, locked until the transaction ends. Locked alone, not with its
    project: select_related under FOR UPDATE would also lock the site row on Postgres and
    stall unrelated edits to the site."""
    return PaymentMilestone.objects.select_for_update().get(pk=pk)


def _lock_receipt(pk):
    """The receipt row, locked until the transaction ends (alone, for the same reason)."""
    return PaymentReceipt.objects.select_for_update().get(pk=pk)


def _sum_amounts(milestone, statuses, exclude_pk=None):
    """Sum of the amounts of `milestone`'s receipts in `statuses`, optionally leaving one
    receipt out. One aggregate query; Decimal 0 when there are none."""
    receipts = PaymentReceipt.objects.filter(milestone=milestone, status__in=statuses)
    if exclude_pk is not None:
        receipts = receipts.exclude(pk=exclude_pk)
    return receipts.aggregate(total=Sum('amount'))['total'] or Decimal('0.00')


def _overpayment_warnings(milestone, adding, exclude_pk=None):
    """FR20 / D10 on recording: a warning, never a refusal, when the milestone's live
    receipts (recorded, queried, confirmed — Q10) plus `adding` exceed its agreed amount.
    None while the agreed amount is blank (FR24): there is nothing to exceed."""
    if milestone.amount is None:
        return ()
    total = _sum_amounts(milestone, LIVE_STATUSES, exclude_pk=exclude_pk) + adding
    if total <= milestone.amount:
        return ()
    return (f'Receipts recorded on {milestone.milestone_name} now total ₹{total}, more than '
            f'its agreed amount of ₹{milestone.amount} (by ₹{total - milestone.amount}). '
            f'Finance will need a note to confirm beyond the agreed amount.',)


def _profile(actor):
    """The UserProfile of `actor` (an auth User). Writers take the User because the
    permission helpers do; rows and log lines name the profile."""
    return actor.profile


def _mode_label(receipt):
    return dict(PaymentReceipt.MODE_CHOICES).get(receipt.mode, receipt.mode)


def _received_words(receipt):
    """'received 12 Nov 2026 by UPI' — the date and mode half of every log line. Never the
    proof file's name (it is the uploader's text and may name the client)."""
    return f'received {receipt.date_received:%d %b %Y} by {_mode_label(receipt)}'


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------

def record_receipt(actor, milestone, amount, date_received, mode, reference='', note='',
                   proof=None):
    """Record a client payment received on `milestone`: '' -> recorded (FR1, FR3, D6, D9).

    `actor` is an auth User; `proof` an optional uploaded file (FR17). Returns a
    ReceiptResult whose warnings carry the FR20 overpayment warning (D10) — recording
    still succeeds. Raises ReceiptRefused when the actor may not record here or the
    figures are wrong; nothing is written then.

    THE PROOF IS UPLOADED BEFORE THE TRANSACTION (no network call while a row is locked)
    and removed again if the save is refused or the transaction rolls back, so a refused
    receipt never leaves a file behind.
    """
    # D6: who may record on this milestone — asked first, so a refused person's file is
    # never even uploaded.
    if not user_can_record_receipt(actor, milestone):
        raise ReceiptRefused(NOT_ALLOWED)
    figures = _clean_figures(amount, date_received, mode, reference, note)
    profile = _profile(actor)

    stored = None
    if proof is not None:
        try:
            stored = store_receipt_proof(milestone.project_id, proof)
        except DeliveryStorageError as exc:
            raise ReceiptRefused(str(exc))

    try:
        with transaction.atomic():
            locked = _lock_milestone(milestone.pk)
            # D6 again under the lock: the site may have been cancelled, or the actor's
            # place on it changed, since the first check.
            if not user_can_record_receipt(actor, locked):
                raise ReceiptRefused(STALE)
            warnings = _overpayment_warnings(locked, figures.amount)
            receipt = PaymentReceipt.objects.create(
                milestone=locked,
                status=PaymentReceipt.RECORDED,
                amount=figures.amount,
                date_received=figures.date_received,
                mode=figures.mode,
                reference=figures.reference,
                note=figures.note,
                proof_bucket=stored.bucket if stored else '',
                proof_path=stored.path if stored else '',
                proof_name=stored.name if stored else '',
                proof_content_type=stored.content_type if stored else '',
                proof_size=stored.size if stored else None,
                source=PaymentReceipt.ENTERED,
                recorded_by=profile,
            )
            record_transition(receipt, to_status=PaymentReceipt.RECORDED, from_status='',
                              actor=profile, reason_code=REASON_CREATED,
                              remark=figures.note, project=locked.project)
            log_activity(locked.project, profile,
                         f'Receipt recorded on {locked.milestone_name}: ₹{receipt.amount} '
                         f'{_received_words(receipt)} — recorded by {display_name(profile)}',
                         entity_type=RECEIPT_ENTITY, entity_id=receipt.pk,
                         action_code='receipt_recorded')
    except BaseException:
        # Refused under the lock, or the transaction rolled back for any other reason:
        # the row does not exist, so the file it would have pointed at goes too.
        # discard_receipt_proof() refuses a path a row still records, so this can never
        # remove the evidence of a saved receipt.
        if stored is not None:
            discard_receipt_proof(stored.bucket, stored.path)
        raise
    # TODO: an OUTER transaction that rolls back after this returns (a caller's own
    # atomic block) leaves the uploaded proof orphaned — Django has no on-rollback hook.
    # Deferred: no caller wraps this today (no screens); B5-3 must call it outside any
    # outer atomic, or a periodic orphan sweep is needed.
    return ReceiptResult(receipt, warnings)


def query_receipt(actor, receipt, reason):
    """Finance queries a recorded receipt with a reason: recorded -> queried, and an open
    PaymentReceiptQuery (FR19, D8). Raises ReceiptRefused when not allowed or the reason
    is blank."""
    reason = _required_text(reason, 'A query must say what is wrong — the reason is what '
                                    'the recorder answers.')
    # D8/FR11: Finance only, never the receipt's own recorder or legacy completer.
    if not user_can_query_receipt(actor, receipt):
        raise ReceiptRefused(NOT_ALLOWED)
    profile = _profile(actor)
    with transaction.atomic():
        locked = _lock_receipt(receipt.pk)
        # Again under the lock: another Finance user may have queried, confirmed or
        # rejected it meanwhile. The partial unique index would also refuse a second open
        # query; this check makes that a clear message instead of an IntegrityError.
        if not user_can_query_receipt(actor, locked):
            raise ReceiptRefused(STALE)
        PaymentReceiptQuery.objects.create(receipt=locked, reason=reason, raised_by=profile)
        from_status = locked.status
        locked.status = PaymentReceipt.QUERIED
        locked.save(update_fields=['status'])
        record_transition(locked, to_status=PaymentReceipt.QUERIED, from_status=from_status,
                          actor=profile, remark=reason)
        log_activity(locked.milestone.project, profile,
                     f'Receipt queried on {locked.milestone.milestone_name}: '
                     f'₹{locked.amount} {_received_words(locked)} — queried by '
                     f'{display_name(profile)}: {reason}',
                     entity_type=RECEIPT_ENTITY, entity_id=locked.pk,
                     action_code='receipt_queried')
    return ReceiptResult(locked)


def answer_receipt_query(actor, receipt, answer):
    """The recorder (or whoever may record on the milestone now) answers the open query:
    queried -> recorded (D7, FR19). TEXT ONLY (D3): the amount, date and mode are never
    changed — if they are wrong, Finance rejects and a new receipt is recorded."""
    answer = _required_text(answer, 'An answer must say something. Nothing was saved.')
    open_query = receipt.queries.filter(answered_at__isnull=True).first()
    # D7: the recorder, or anyone who may record here now; never the person who asked.
    if open_query is None or not user_can_answer_receipt_query(actor, receipt, open_query):
        raise ReceiptRefused(NOT_ALLOWED)
    profile = _profile(actor)
    with transaction.atomic():
        locked = _lock_receipt(receipt.pk)
        # Re-read the open query under the receipt's lock: Finance may have rejected the
        # receipt (leaving its query open, Q5) or another person may have answered.
        open_query = locked.queries.filter(answered_at__isnull=True).first()
        if open_query is None or not user_can_answer_receipt_query(actor, locked, open_query):
            raise ReceiptRefused(STALE)
        open_query.answer = answer
        open_query.answered_by = profile
        open_query.answered_at = timezone.now()
        open_query.save(update_fields=['answer', 'answered_by', 'answered_at'])
        from_status = locked.status
        locked.status = PaymentReceipt.RECORDED
        locked.save(update_fields=['status'])
        record_transition(locked, to_status=PaymentReceipt.RECORDED, from_status=from_status,
                          actor=profile, remark=answer)
        log_activity(locked.milestone.project, profile,
                     f'Receipt query answered on {locked.milestone.milestone_name}: '
                     f'₹{locked.amount} {_received_words(locked)} — answered by '
                     f'{display_name(profile)}: {answer}',
                     entity_type=RECEIPT_ENTITY, entity_id=locked.pk,
                     action_code='receipt_query_answered')
    return ReceiptResult(locked)


def confirm_receipt(actor, receipt, overpayment_note=''):
    """Finance confirms a recorded receipt against the bank: recorded -> confirmed (FR2,
    D4, D8). FINAL — there is no un-confirm in this build (D5, Q11).

    FR20 / D10: refused when the milestone's CONFIRMED receipts plus this one would exceed
    its agreed amount, unless `overpayment_note` says why; the note is stored on the
    receipt. No check while the agreed amount is blank (FR24)."""
    note = (overpayment_note or '').strip()
    # D8/FR11: Finance only, never the recorder or the legacy completer; D4: not while
    # a query is open.
    if not user_can_confirm_receipt(actor, receipt):
        raise ReceiptRefused(NOT_ALLOWED)
    profile = _profile(actor)
    with transaction.atomic():
        milestone = _lock_milestone(receipt.milestone_id)
        locked = _lock_receipt(receipt.pk)
        # Again under the locks: another Finance user may have confirmed, queried or
        # rejected it since the first check — a second confirmation is refused here.
        if not user_can_confirm_receipt(actor, locked):
            raise ReceiptRefused(STALE)
        if milestone.amount is not None:
            confirmed = _sum_amounts(milestone, (PaymentReceipt.CONFIRMED,))
            if confirmed + locked.amount > milestone.amount and not note:
                raise ReceiptRefused(
                    f'Confirming this receipt would bring confirmed money on '
                    f'{milestone.milestone_name} to ₹{confirmed + locked.amount}, more than '
                    f'its agreed amount of ₹{milestone.amount}. Add a note saying why to '
                    f'confirm it anyway.')
        from_status = locked.status
        locked.status = PaymentReceipt.CONFIRMED
        locked.confirmed_by = profile
        locked.confirmed_at = timezone.now()
        locked.overpayment_note = note
        locked.save(update_fields=['status', 'confirmed_by', 'confirmed_at',
                                   'overpayment_note'])
        record_transition(locked, to_status=PaymentReceipt.CONFIRMED, from_status=from_status,
                          actor=profile, remark=note, project=milestone.project)
        log_activity(milestone.project, profile,
                     f'Receipt confirmed on {milestone.milestone_name}: ₹{locked.amount} '
                     f'{_received_words(locked)} — confirmed by {display_name(profile)}'
                     + (f' (over the agreed amount: {note})' if note else ''),
                     entity_type=RECEIPT_ENTITY, entity_id=locked.pk,
                     action_code='receipt_confirmed')
    return ReceiptResult(locked)


def reject_receipt(actor, receipt, reason):
    """Finance rejects an unconfirmed receipt with a reason: recorded|queried -> rejected
    (FR19, D8). FINAL. The row is kept; a corrected receipt is a new record.

    AN OPEN QUERY IS LEFT OPEN (ruling Q5, the PaymentRequestHold precedent): it was
    overruled, not answered. open_receipt_queries() therefore never counts it."""
    reason = _required_text(reason, 'A rejection must say why. Nothing was changed.')
    # D8: any active Finance user, from recorded or queried only.
    if not user_can_reject_receipt(actor, receipt):
        raise ReceiptRefused(NOT_ALLOWED)
    profile = _profile(actor)
    with transaction.atomic():
        locked = _lock_receipt(receipt.pk)
        # Again under the lock: it may have been confirmed or rejected meanwhile.
        if not user_can_reject_receipt(actor, locked):
            raise ReceiptRefused(STALE)
        from_status = locked.status
        locked.status = PaymentReceipt.REJECTED
        locked.rejected_by = profile
        locked.rejected_at = timezone.now()
        locked.reject_reason = reason
        locked.save(update_fields=['status', 'rejected_by', 'rejected_at', 'reject_reason'])
        record_transition(locked, to_status=PaymentReceipt.REJECTED, from_status=from_status,
                          actor=profile, remark=reason)
        log_activity(locked.milestone.project, profile,
                     f'Receipt rejected on {locked.milestone.milestone_name}: '
                     f'₹{locked.amount} {_received_words(locked)} — rejected by '
                     f'{display_name(profile)}: {reason}',
                     entity_type=RECEIPT_ENTITY, entity_id=locked.pk,
                     action_code='receipt_rejected')
    return ReceiptResult(locked)


def complete_receipt_amount(actor, receipt, amount, date_received, mode, reference='',
                            note=''):
    """Finance types a legacy receipt's figures from the bank statement: amount_needed ->
    recorded (D2, FR16). The completer is stored and, as its recorder (Q9), may not query
    or confirm it — a DIFFERENT Finance user confirms (FR11). Returns the FR20 warning as
    record_receipt does."""
    # D2: active Finance, on an amount_needed row only.
    if not user_can_complete_receipt_amount(actor, receipt):
        raise ReceiptRefused(NOT_ALLOWED)
    figures = _clean_figures(amount, date_received, mode, reference, note)
    profile = _profile(actor)
    with transaction.atomic():
        milestone = _lock_milestone(receipt.milestone_id)
        locked = _lock_receipt(receipt.pk)
        # Again under the locks: another Finance user may have completed it meanwhile.
        if not user_can_complete_receipt_amount(actor, locked):
            raise ReceiptRefused(STALE)
        warnings = _overpayment_warnings(milestone, figures.amount, exclude_pk=locked.pk)
        from_status = locked.status
        locked.amount = figures.amount
        locked.date_received = figures.date_received
        locked.mode = figures.mode
        locked.reference = figures.reference
        locked.note = figures.note
        locked.status = PaymentReceipt.RECORDED
        locked.completed_by = profile
        locked.completed_at = timezone.now()
        locked.save(update_fields=['amount', 'date_received', 'mode', 'reference', 'note',
                                   'status', 'completed_by', 'completed_at'])
        record_transition(locked, to_status=PaymentReceipt.RECORDED, from_status=from_status,
                          actor=profile, remark=figures.note, project=milestone.project)
        log_activity(milestone.project, profile,
                     f'Receipt amount completed on {milestone.milestone_name}: '
                     f'₹{locked.amount} {_received_words(locked)} — completed by '
                     f'{display_name(profile)}',
                     entity_type=RECEIPT_ENTITY, entity_id=locked.pk,
                     action_code='receipt_amount_completed')
    return ReceiptResult(locked, warnings)


# FIN-M-B5-7 — MIGRATED RECORD CREATOR GOES HERE. Not written in this session (FR16): the
# migration command will add one function that creates, per old receipt, either a
# `confirmed` row (amount known; source='migrated', confirmed_by=None, shown as "migrated —
# entered by Finance before 17 Nov") or an `amount_needed` row (amount, date and mode
# blank), each with its own StatusTransition and a non-activity log code.


# ---------------------------------------------------------------------------
# Read helpers for later sessions (no page uses them yet)
# ---------------------------------------------------------------------------

def receipt_totals(milestones):
    """{milestone pk: ReceiptTotals} for every milestone in `milestones` (instances or
    pks): the confirmed sum, the unconfirmed sum (recorded + queried, FR2) and the count
    of legacy rows still needing an amount. Rejected receipts count nowhere.

    ONE aggregate query whatever the number of milestones (grouped by milestone, each sum
    filtered by status). Pass loaded instances or pks; an unevaluated queryset would be
    evaluated here first, at one more query."""
    pks = [getattr(m, 'pk', m) for m in milestones]
    totals = {pk: ReceiptTotals() for pk in pks}
    if not pks:
        return totals
    rows = (PaymentReceipt.objects.filter(milestone_id__in=pks)
            .values('milestone_id')
            .annotate(
                confirmed=Sum('amount', filter=Q(status=PaymentReceipt.CONFIRMED)),
                unconfirmed=Sum('amount', filter=Q(status__in=UNCONFIRMED_STATUSES)),
                amount_needed=Count('pk', filter=Q(status=PaymentReceipt.AMOUNT_NEEDED)))
            .order_by())
    for row in rows:
        totals[row['milestone_id']] = ReceiptTotals(
            confirmed=row['confirmed'] or Decimal('0.00'),
            unconfirmed=row['unconfirmed'] or Decimal('0.00'),
            amount_needed=row['amount_needed'])
    return totals


def possible_duplicates(milestone, amount, date_received, reference=''):
    """Live (not rejected) receipts on `milestone` with the same amount AND date received,
    or the same non-blank reference (compared trimmed, case-insensitive). For B5-3's
    duplicate guard; it refuses nothing here. A queryset, one query when evaluated."""
    match = Q(pk__in=[])
    if amount is not None and date_received is not None:
        match |= Q(amount=amount, date_received=date_received)
    reference = (reference or '').strip()
    if reference:
        match |= Q(reference__iexact=reference)
    return (PaymentReceipt.objects.filter(milestone=milestone)
            .exclude(status=PaymentReceipt.REJECTED)
            .filter(match))


def open_receipt_queries():
    """Every query still waiting for an answer: unanswered AND on a receipt that is still
    `queried`. A rejected receipt's unanswered query is NOT open (ruling Q5) — it was
    overruled — so every counter or list of open queries must read this, never
    answered_at IS NULL alone."""
    return PaymentReceiptQuery.objects.filter(answered_at__isnull=True,
                                              receipt__status=PaymentReceipt.QUERIED)
