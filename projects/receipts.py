"""
FIN-M-B5-2 — client payment receipts: THE ONLY WRITER of PaymentReceipt and
PaymentReceiptQuery, plus read helpers for the sessions that add screens (B5-3 onward).

THE LIFE OF A RECEIPT
---------------------
    record_receipt()           ''            -> recorded       (BD on M1 / PM side / Finance)
    query_receipt()            recorded      -> queried        (Finance, not the recorder)
    answer_receipt_query()     queried       -> recorded       (the recorder, or whoever may record now)
    confirm_receipt()          recorded      -> confirmed      (Finance, not the recorder)
    unconfirm_receipt()        confirmed     -> recorded       (any Finance, note required — FR35)
    reject_receipt()           recorded|queried -> rejected    (Finance, reason required) — FINAL
    complete_receipt_amount()  amount_needed -> recorded       (Finance types a legacy row's figures)

A confirmation can be UNDONE (FIN-M-B5-4a, ruling FR35, 25 Nov): any active Finance user,
with a note, puts the receipt back to `recorded`; the confirmation stays in the history
(its StatusTransition row is never touched). Only `rejected` is final.

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
back leaves no log line behind. The log line needs no on_commit.

ALERTS (FIN-M-B5-4b, ruling FR36 — overrules D11's "never sends a notification"). Each of
record, query, reject, confirm and unconfirm calls one projects/receipt_alerts.py function
INSIDE its transaction.atomic(), after the change. That function calls notify.notify(),
which itself defers the send to after commit (transaction.on_commit), so a refused move
or a rolled-back transaction — this one or a caller's outer one — sends nothing. No
second on_commit here. answer_receipt_query() and complete_receipt_amount() send nothing.

THE MILESTONE FOLLOWS ITS CONFIRMED RECEIPTS (FIN-M-B5-5a, FR10/FR24/FR37). D11 is
narrowed, not dropped: the writers above still never write PaymentMilestone themselves.
recompute_milestone_from_receipts() is the ONE function here that does — status,
amount_received and received_date only — called by confirm_receipt(), unconfirm_receipt()
and the three agreed-amount writers. It does nothing while CLIENT_RECEIPTS_LIVE is off
(FR38). It never sends an alert (H7): the old milestone "Payment received"
(payment_milestone_reached) is fired only from the task-status chokepoint in views.py,
which recompute never calls, and since FIN-M-B5-5b not at all while the switch is on.

THE CONFIRMATION TASK FOLLOWS THE MILESTONE (FIN-M-B5-5b, FR14). When recompute changes a
milestone's status into or out of Received, follow_confirmation_task() moves that
milestone's Finance confirmation task to Done or back to Not Started. It is the ONE writer
of those tasks' status while the switch is on — a dedicated door like the mirror writer
(design_views.apply_mirror_status), not the human chokepoint, which refuses every person
on these tasks then ("This task follows Finance-confirmed receipts.").

LOCK ORDER: milestone before receipt wherever both are locked (record, confirm, unconfirm,
complete),
so two writers can never wait on each other in opposite orders. Recompute locks the
milestone, which its callers either already hold or do not hold at all, and then — only
through follow_confirmation_task() — the milestone's confirmation task: milestone before
task. No path locks a confirmation task and then its milestone (the human task path
commits the task before it touches a milestone, and is refused on these tasks while the
switch is on).
"""
import datetime
import logging
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import transaction
from django.db.models import Count, Max, Q, Sum
from django.utils import timezone

from .delivery_storage import DeliveryStorageError
from .models import (
    REASON_CREATED, REASON_RECEIPTS, PaymentMilestone, PaymentReceipt, PaymentReceiptQuery,
    Task, log_activity,
)
from .money_format import format_rupees
from .permissions import (
    user_can_answer_receipt_query, user_can_complete_receipt_amount,
    user_can_confirm_receipt, user_can_query_receipt, user_can_record_receipt,
    user_can_reject_receipt, user_can_unconfirm_receipt,
)
from . import receipt_alerts
from .receipt_storage import discard_receipt_proof, store_receipt_proof
from .scheduling import schedule_site_safely, status_change_reschedules
from .site_bd import display_name
from .utils import record_transition

logger = logging.getLogger(__name__)

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
#: The two figure messages the B5-3 form also places beside their fields
#: (forms_receipts.py imports these, so the form and the writer cannot drift apart).
MODE_REQUIRED = 'Choose how the money was paid.'
OTHER_NEEDS_NOTE = 'Add a note saying how the money was paid when the mode is Other.'
STALE = ('This receipt changed while you were looking at it, or you may no longer act on '
         'it. Nothing was saved — reload and check its current state.')
#: FIN-M-B5-5b (ruling Q1): the milestone's confirmation task could not follow, so the
#: whole action was undone rather than leaving money and task disagreeing.
FOLLOW_FAILED = ("The milestone's Finance confirmation task could not be updated, so "
                 "nothing was saved. Try again; if it happens again, tell Admin.")


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

def clean_receipt_amount(amount):
    """The amount as a 2-place Decimal > 0 that fits the column, or ReceiptRefused (FR3).

    Public because the B5-3 form (forms_receipts.py) runs this same check for its early,
    field-level message: one rule and one wording, whichever runs first. The writer
    still runs it again, so the form is never the only check."""
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


def clean_receipt_date(date_received):
    """The date received, not after today in IST (D9, FR13), or ReceiptRefused (FR3).
    There is no lower bound: an old receipt may be recorded late. Public for the B5-3
    form, for the same reason as clean_receipt_amount()."""
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
    clean_amount = clean_receipt_amount(amount)
    clean_date = clean_receipt_date(date_received)
    if mode not in dict(PaymentReceipt.MODE_CHOICES):
        raise ReceiptRefused(MODE_REQUIRED)
    reference = (reference or '').strip()
    if len(reference) > REFERENCE_LIMIT:
        raise ReceiptRefused(f'The reference can be at most {REFERENCE_LIMIT} characters.')
    note = (note or '').strip()
    # D9/FR18: "Other" says nothing by itself; the note is what tells Finance what it was.
    if mode == PaymentReceipt.OTHER and not note:
        raise ReceiptRefused(OTHER_NEEDS_NOTE)
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


def overpayment_warnings(milestone, adding, exclude_pk=None):
    """FR20 / D10 on recording: a warning, never a refusal, when the milestone's live
    receipts (recorded, queried, confirmed — Q10) plus `adding` exceed its agreed amount.
    Empty while the agreed amount is blank (FR24): there is nothing to exceed.

    THE ONE PLACE THE RULE LIVES (B5-3, E5). The record page asks it BEFORE saving, to
    decide whether the person must tick "Record anyway"; the writers ask it again under
    the milestone lock and return what it says. No view re-implements the sum. Reads
    only — one aggregate query, no lock, no write — so a page may call it freely. The
    wording reads true both before and after the save ("with this receipt"), because the
    same text is shown in both places."""
    if milestone.amount is None:
        return ()
    total = _sum_amounts(milestone, LIVE_STATUSES, exclude_pk=exclude_pk) + adding
    if total <= milestone.amount:
        return ()
    # FIN-M-B5-4a: amounts in Indian grouping (money_format, the one formatter).
    return (f'With this receipt, receipts on {milestone.milestone_name} total '
            f'{format_rupees(total)} — more than the agreed amount of '
            f'{format_rupees(milestone.amount)}, by {format_rupees(total - milestone.amount)}. '
            f'Finance will need a note to confirm beyond the agreed amount.',)


def confirm_overpayment(milestone, receipt):
    """FR20 / D10 on confirming: the refusal text when confirming `receipt` would bring
    `milestone`'s CONFIRMED money above its agreed amount, or '' when it would not (or
    the agreed amount is blank, FR24 — nothing to exceed).

    THE ONE PLACE THE CONFIRM RULE LIVES (FIN-M-B5-4a). The receipt page asks it to decide
    whether to draw the overpayment note box; confirm_receipt() asks it again under the
    milestone lock, refuses with this text when no note was given, and drops a note given
    when it is '' (ruling 3). Reads only — one aggregate query, none when the agreed
    amount is blank. The receipt itself is never counted twice: only `recorded` receipts
    are confirmed, so it is not among the confirmed ones summed here."""
    if milestone.amount is None or receipt.amount is None:
        return ''
    after = _sum_amounts(milestone, (PaymentReceipt.CONFIRMED,)) + receipt.amount
    if after <= milestone.amount:
        return ''
    return (f'Confirming this receipt would bring confirmed money on '
            f'{milestone.milestone_name} to {format_rupees(after)}, more than its agreed '
            f'amount of {format_rupees(milestone.amount)}. Add a note saying why to confirm '
            f'it anyway.')


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
            warnings = overpayment_warnings(locked, figures.amount)
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
            # FR36 item 1, sent after commit by notify() (module docstring, ALERTS).
            receipt_alerts.receipt_recorded(receipt, locked.project, profile)
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
    # Deferred: B5-3's record page (views_receipts.receipt_record) calls this outside any
    # atomic block (no ATOMIC_REQUESTS); an orphan sweep stays on the follow-up list.
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
        # FR36 item 2, sent after commit by notify() (module docstring, ALERTS).
        receipt_alerts.receipt_queried(locked, locked.milestone.project, profile, reason)
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
    D4, D8). Undone only by unconfirm_receipt() (FR35), which keeps this in the history.

    FR20 / D10: refused when the milestone's CONFIRMED receipts plus this one would exceed
    its agreed amount, unless `overpayment_note` says why; the note is then stored on the
    receipt. No check while the agreed amount is blank (FR24). A note given when the
    amount is NOT over is ignored (ruling 3, B5-4a): nothing is stored, and neither the
    history nor the log says "over the agreed amount"."""
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
        # FR20 under the milestone lock: no other confirmation on this milestone can
        # land between this sum and the save.
        over = confirm_overpayment(milestone, locked)
        if over and not note:
            raise ReceiptRefused(over)
        if not over:
            # Ruling 3: a note is only ever a reason for going over; one sent when the
            # amount is not over (a stale page, a hand-made POST) is dropped, not stored.
            note = ''
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
        # FR36 item 4, once per confirmed receipt, sent after commit by notify().
        receipt_alerts.receipt_confirmed(locked, milestone.project, profile)
        # FIN-M-B5-5a (H5): the milestone follows, in this transaction and under the
        # milestone lock already held, so the sum includes the receipt just confirmed.
        recompute_milestone_from_receipts(milestone, profile, 'receipt confirmed')
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
        # FR36 item 3, sent after commit by notify() (module docstring, ALERTS).
        receipt_alerts.receipt_rejected(locked, locked.milestone.project, profile, reason)
    return ReceiptResult(locked)


def unconfirm_receipt(actor, receipt, note):
    """Finance undoes a confirmation, with a note: confirmed -> recorded (FIN-M-B5-4a,
    ruling FR35). Any active Finance user may, the original confirmer included.

    The row's confirmed_by, confirmed_at and overpayment_note are cleared — the receipt is
    unconfirmed again and its next confirmation stamps them afresh — while the history
    keeps them: the confirmation's StatusTransition row (actor, time, the overpayment note
    as its remark) and log line are never touched, and this move adds one of each, with
    the note. Re-confirming then follows FR11 as before (never the recorder).

    The milestone follows through recompute_milestone_from_receipts() (FIN-M-B5-5a; a
    no-op while the switch is off). Raises ReceiptRefused when not allowed or the note is
    blank; nothing is written then."""
    note = _required_text(note, 'Undoing a confirmation must say why. Nothing was changed.')
    # FR35: any active Finance user, on a confirmed receipt on a live site.
    if not user_can_unconfirm_receipt(actor, receipt):
        raise ReceiptRefused(NOT_ALLOWED)
    profile = _profile(actor)
    with transaction.atomic():
        # Milestone first, as confirm_receipt locks it: a re-confirmation sums this
        # milestone's confirmed money under that lock, so the two must queue in one order.
        milestone = _lock_milestone(receipt.milestone_id)
        locked = _lock_receipt(receipt.pk)
        # Again under the locks: another Finance user may have undone it meanwhile.
        if not user_can_unconfirm_receipt(actor, locked):
            raise ReceiptRefused(STALE)
        from_status = locked.status
        locked.status = PaymentReceipt.RECORDED
        locked.confirmed_by = None
        locked.confirmed_at = None
        locked.overpayment_note = ''
        locked.save(update_fields=['status', 'confirmed_by', 'confirmed_at',
                                   'overpayment_note'])
        record_transition(locked, to_status=PaymentReceipt.RECORDED, from_status=from_status,
                          actor=profile, remark=note, project=milestone.project)
        log_activity(milestone.project, profile,
                     f'Receipt confirmation undone on {milestone.milestone_name}: '
                     f'₹{locked.amount} {_received_words(locked)} — undone by '
                     f'{display_name(profile)}: {note}',
                     entity_type=RECEIPT_ENTITY, entity_id=locked.pk,
                     action_code='receipt_unconfirmed')
        # FR36 item 5, sent after commit by notify() (module docstring, ALERTS).
        receipt_alerts.receipt_unconfirmed(locked, milestone.project, profile, note)
        # FIN-M-B5-5a (H5, FR35): the milestone follows — back to Invoiced/Pending when
        # this undo takes confirmed money below the agreed amount.
        recompute_milestone_from_receipts(milestone, profile, 'receipt confirmation undone')
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
        warnings = overpayment_warnings(milestone, figures.amount, exclude_pk=locked.pk)
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


# ---------------------------------------------------------------------------
# The milestone follows its confirmed receipts (FIN-M-B5-5a)
# ---------------------------------------------------------------------------

#: R15 plain-line suffix on every milestone change recompute makes (ruling Q6).
RECOMPUTE_SUFFIX = ' (from confirmed receipts)'


def recompute_milestone_from_receipts(milestone, actor, reason=''):
    """Bring `milestone`'s status, amount_received and received_date in step with its
    CONFIRMED receipts (H1, H4). `milestone` is an instance or a pk; `actor` the
    UserProfile whose act caused it (the confirmer, the undoer, or whoever set the agreed
    amount); `reason` a few words kept as the StatusTransition remark. Returns the locked,
    saved milestone, or None while the switch is off.

    THE SWITCH (FR38). While CLIENT_RECEIPTS_LIVE is off this returns before opening a
    transaction or running a query, so every caller behaves exactly as before B5-5a.

    THE RULE, read under the milestone's row lock:
      - Left alone (ruling Q1): Received, NOT marked received_before_receipts, and no
        receipt row of any status. It was received through an old path after the marking
        command ran; mark_legacy_received_milestones picks it up on its next run. Any
        receipt row at all means the rules below apply.
      - Legacy (received_before_receipts, FR37): stays Received. Money = the confirmed
        receipts when any exist, else the legacy amount and date the command copied.
      - Otherwise: Received when an agreed amount is set and confirmed money reaches it
        (FR10; over it still counts, H9). Else Invoiced when an invoice was recorded
        (invoice_date — the invoice writer always sets it), or Pending; amount_received
        = confirmed money so far (blank when none, today's convention) and received_date
        blank. No agreed amount can never turn Received (FR24).
      - received_date when Received = the LATEST date_received among confirmed receipts
        (ruling Q3: by the day the money arrived, FR13 — not the order of confirmation).

    variance_reason is never touched (ruling Q4). Writes only when a value changes; then
    one StatusTransition when the status moved (reason REASON_RECEIPTS) and the R15
    old -> new lines through views._log_milestone_changes, suffixed RECOMPUTE_SUFFIX. No
    alert (H7). When the STATUS moved, the milestone's confirmation task follows
    (FIN-M-B5-5b, K1) through follow_confirmation_task(), inside this transaction and
    with `actor`; an amount-only change, a no-change recompute and a legacy milestone
    (whose status never changes, FR37) leave the task alone.

    OWN ATOMIC BLOCK, LOCK FIRST. It opens transaction.atomic() before select_for_update,
    so the PM edit path, which has no transaction, gets one; inside a caller's
    transaction (confirm, undo, BD, import) it is a savepoint and the lock is the one the
    caller already holds or the first lock taken on that row. Recompute takes no other
    lock, so it adds no new lock order (milestone before receipt is kept)."""
    if not receipts_live():
        return None
    # Imported here, not at the top: views.py imports this module when it loads, so a
    # module-level import of views from here would be circular.
    from .views import _log_milestone_changes, _milestone_values
    with transaction.atomic():
        locked = _lock_milestone(getattr(milestone, 'pk', milestone))
        # One aggregate: every receipt row (for the Q1 test), and the confirmed sum and
        # latest day received. Rejected, recorded, queried and amount_needed rows never
        # count as money (FR2).
        confirmed = Q(status=PaymentReceipt.CONFIRMED)
        figures = PaymentReceipt.objects.filter(milestone=locked).aggregate(
            rows=Count('pk'),
            total=Sum('amount', filter=confirmed),
            latest=Max('date_received', filter=confirmed))
        if (locked.status == PaymentMilestone.RECEIVED
                and not locked.received_before_receipts and not figures['rows']):
            return locked

        total = figures['total']            # None when nothing is confirmed
        if locked.received_before_receipts:
            status = PaymentMilestone.RECEIVED
            if total is not None:
                received, received_on = total, figures['latest']
            else:
                received, received_on = locked.legacy_amount_received, locked.legacy_received_date
        elif locked.amount is not None and total is not None and total >= locked.amount:
            status, received, received_on = PaymentMilestone.RECEIVED, total, figures['latest']
        else:
            status = (PaymentMilestone.INVOICED if locked.invoice_date is not None
                      else PaymentMilestone.PENDING)
            received, received_on = total, None

        new = {'status': status, 'amount_received': received, 'received_date': received_on}
        changed = [field for field, value in new.items() if getattr(locked, field) != value]
        if not changed:
            return locked
        before = _milestone_values(locked)
        from_status = locked.status
        for field in changed:
            setattr(locked, field, new[field])
        locked.save(update_fields=changed)
        if 'status' in changed:
            # R-2: the status write and its ledger row in the same transaction.
            record_transition(locked, to_status=status, from_status=from_status,
                              actor=actor, reason_code=REASON_RECEIPTS, remark=reason)
        try:
            _log_milestone_changes(locked.project, actor, locked, before, changed,
                                   plain_suffix=RECOMPUTE_SUFFIX)
        except Exception:
            # As the invoice writer (B9a ruling 7): a change-log failure is logged for
            # Admin only; the milestone change it describes stands.
            logger.exception('FIN-M-B5-5a: change-log line not written for the recompute '
                             'of milestone %s', locked.pk)
        # FIN-M-B5-5b (K1/K2): the task follows a STATUS change only. Legacy milestones
        # never reach here with a status change (they stay Received), and the guard says
        # so rather than relying on it. A failure is NEVER swallowed (ruling Q1, B5-5b
        # report): the task and the milestone commit together or not at all, so money
        # and task can never disagree. It is logged for Admin and re-raised as
        # ReceiptRefused, which unwinds this block and the caller's (confirm, undo) and
        # which their views already show Finance as an error message — the receipt stays
        # as it was, nothing is half-saved. B9a's "the money stands, warn" rule was for a
        # person completing a task, not for Finance confirming money.
        if 'status' in changed and not locked.received_before_receipts:
            try:
                follow_confirmation_task(locked, from_status, actor, reason)
            except Exception:
                logger.exception('FIN-M-B5-5b: confirmation task of milestone %s could not '
                                 'follow its status; nothing was saved', locked.pk)
                raise ReceiptRefused(FOLLOW_FAILED)
    return locked


def follow_confirmation_task(milestone, from_status, actor, reason=''):
    """Move `milestone`'s Finance confirmation task after its status changed from
    `from_status` (FIN-M-B5-5b, K1). Called only by recompute_milestone_from_receipts(),
    inside its transaction with the milestone already locked; never while the switch is
    off (recompute returns before reaching it). `reason` is recompute's own (e.g. "receipt
    confirmed", "agreed amount set") and becomes the task's ledger remark too (ruling Q2),
    so both rows of one event say why.

      * milestone became Received          -> task Done
      * milestone left Received            -> task Not Started (Q3, mirror precedent)
      * any other move (Pending <-> Invoiced) -> nothing

    The task is found by the existing lookup — the name map plus the project, never the
    is_payment_milestone flag (Q1). A task the PM marked Not Applicable is left as it is
    and a warning is logged (Q5, B9a ruling b precedent). A Blocked task is moved anyway
    (Q5): with the switch on it has no human meaning, and K4 stops new Blocks."""
    if milestone.status == PaymentMilestone.RECEIVED:
        target = Task.DONE
    elif from_status == PaymentMilestone.RECEIVED:
        target = Task.NOT_STARTED
    else:
        return
    # Imported here, not at the top: views.py imports this module when it loads.
    from .views import _MILESTONE_TO_FINANCE_TASK
    task_name = _MILESTONE_TO_FINANCE_TASK.get(milestone.milestone_name)
    if not task_name:
        return
    # The milestone's confirmation task(s) on its project — exactly one on every live
    # Residential site (B5-5b pre-flight), but every match follows, as B9a's sync did.
    # Locked here, after the milestone recompute holds: milestone -> task. of=('self',)
    # so the phase row the filter joins through is read, not locked.
    tasks = list(Task.objects.select_for_update(of=('self',))
                 .filter(phase__project_id=milestone.project_id, task_name=task_name)
                 .select_related('phase').order_by('pk'))
    for task in tasks:
        if task.is_not_applicable:
            logger.warning(
                'FIN-M-B5-5b: milestone %s (%s) on project %s moved to %s; its task %s '
                "('%s') is marked Not Applicable and was left as it is",
                milestone.pk, milestone.milestone_name, milestone.project_id,
                milestone.status, task.pk, task.task_name)
            continue
        apply_confirmation_task_status(task, milestone, target, actor, remark=reason)


def apply_confirmation_task_status(task, milestone, new_status, actor, remark=''):
    """THE ONE PLACE A FINANCE CONFIRMATION TASK'S STATUS IS WRITTEN while the receipt
    switch is on (FIN-M-B5-5b, ruling Q2). Caller owns the atomic block and has locked
    the milestone and `task`.

    The confirmation-task counterpart of design_views.apply_mirror_status(): it refuses
    (ValueError) any task that is not `milestone`'s confirmation task by the name map and
    the project, exactly as the human chokepoint refuses every person on these tasks. It
    never calls _apply_task_status_change(), the task -> milestone money sync or the old
    "Payment received" alert; it needs no request, so the spreadsheet import command
    reaches it too.

    Writes, only when the status differs (otherwise nothing at all):
      * status; completed_at set on Done, cleared on reopen (mirror precedent — a reopened
        task with a completed_at would still read as finished to completion metrics);
        blocked_since cleared when it leaves Blocked, as the chokepoint does;
      * one StatusTransition, reason REASON_RECEIPTS, actor = recompute's actor, remark =
        `remark` (the milestone row's remark, ruling Q2);
      * the activity line in the human wording plus RECOMPUTE_SUFFIX;
      * the site reschedule after commit, under the chokepoint's own test (no
        confirmation task is a schedule-block task today, so it never fires).
    Returns True if the row was written."""
    # Imported here, not at the top: views.py imports this module when it loads, so a
    # module-level import of views from here would be circular.
    from .views import _MILESTONE_TO_FINANCE_TASK
    expected = _MILESTONE_TO_FINANCE_TASK.get(milestone.milestone_name)
    if not expected or task.task_name != expected:
        raise ValueError(
            f'apply_confirmation_task_status(): task {task.pk} ({task.task_name!r}) is not '
            f"{milestone.milestone_name}'s confirmation task.")
    if task.phase.project_id != milestone.project_id:
        raise ValueError(
            f'apply_confirmation_task_status(): task {task.pk} is on another project than '
            f'milestone {milestone.pk}.')
    old_status = task.status
    if old_status == new_status:
        return False
    task.status = new_status
    task.completed_at = timezone.now() if new_status == Task.DONE else None
    fields = ['status', 'completed_at']
    if old_status == Task.BLOCKED:
        task.blocked_since = None
        fields.append('blocked_since')
    # The row is locked by the caller (follow_confirmation_task), so no other writer can
    # land between the read of old_status above and this save.
    task.save(update_fields=fields)
    record_transition(task, to_status=new_status, from_status=old_status, actor=actor,
                      reason_code=REASON_RECEIPTS, remark=remark)
    project = milestone.project
    log_activity(project, actor,
                 f'Changed task status to {new_status}: {task.task_name}{RECOMPUTE_SUFFIX}',
                 entity_type='Task', entity_id=task.pk,
                 action_code=f"task_status_{new_status.lower().replace(' ', '_')}")
    if status_change_reschedules(task, old_status, new_status):
        transaction.on_commit(lambda: schedule_site_safely(project, actor))
    return True


def milestone_receipt_label(milestone, totals):
    """The words a page shows for `milestone`'s money state (H6), from its stored status
    and `totals` (its ReceiptTotals from receipt_totals()). No stored status is added:
    "Part received" and "Amount needed" are display only. Read by client_money (the
    milestone card, BD and Finance work, FIN-M-B5-6) and the Finance Head item list
    (FIN-M-B5-8).

      Received                      status Received (and money confirmed, or not legacy)
      Received before receipts      legacy (FR37) Received, no confirmed money yet
      Amount needed                 not Received, no agreed amount (FR24), nothing confirmed
      Amount needed — ₹x confirmed  not Received, no agreed amount, some money confirmed
      Part received ₹x of ₹y        not Received, some confirmed money below the amount
      Invoiced / Pending            otherwise, by status
    """
    confirmed = totals.confirmed if totals is not None else Decimal('0.00')
    if milestone.status == PaymentMilestone.RECEIVED:
        if milestone.received_before_receipts and not confirmed:
            return 'Received before receipts'
        return 'Received'
    if milestone.amount is None:
        # Ruling 25 Nov: "— ₹0 confirmed" says nothing, so the figure shows only when > 0.
        if not confirmed:
            return 'Amount needed'
        return f'Amount needed — {format_rupees(confirmed)} confirmed'
    if confirmed:
        return f'Part received {format_rupees(confirmed)} of {format_rupees(milestone.amount)}'
    return milestone.status


# FIN-M-B5-7 — MIGRATED RECORD CREATOR GOES HERE. Not written in this session (FR16): the
# migration command will add one function that creates, per old receipt, either a
# `confirmed` row (amount known; source='migrated', confirmed_by=None, shown as "migrated —
# entered by Finance before 17 Nov") or an `amount_needed` row (amount, date and mode
# blank), each with its own StatusTransition and a non-activity log code.


# ---------------------------------------------------------------------------
# Read helpers (the B5-3 record page uses receipts_live, receipt_totals,
# possible_duplicates and overpayment_warnings)
# ---------------------------------------------------------------------------

def receipts_live():
    """True when the receipt SCREENS are switched on (settings.CLIENT_RECEIPTS_LIVE, B5-3
    E1). Read at call time so tests can switch it with override_settings.

    The record page and the milestone card's button ask this. THE RECEIPT WRITERS DO NOT:
    the switch hides screens until the B5 release (main may be pushed before B5-6/B5-8,
    and money recorded then would be counted by no page), and a command or a later screen
    must not be blocked by a display setting.

    ONE EXCEPTION, BY RULING FR38 (FIN-M-B5-5a): recompute_milestone_from_receipts() asks
    it and does nothing while it is off, so an early push of main moves no milestone. The
    switch is therefore also "receipts drive milestones" from B5-5a on."""
    return bool(getattr(settings, 'CLIENT_RECEIPTS_LIVE', False))


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
