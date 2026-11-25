"""
FIN-M-B5-3 — record a client receipt: one page per payment milestone, phone first.

Reached from the "Record receipt" button on the project page's milestone card. The page
shows the milestone (agreed amount or "none yet", what Finance has confirmed and what is
not yet confirmed), the form, and the milestone's receipts newest first.

THE GATE, IN THIS ORDER, ON EVERY REQUEST (GET and POST alike)
  a) receipts_live()                   -> 404 for everyone while CLIENT_RECEIPTS_LIVE is off (E1)
  b) _active_project                   -> 404 on a soft-deleted or unknown site
  c) user_can_view_project             -> refuse_site_view_404 (handover redirect or 404):
                                          someone who cannot see the site is not told the page exists
  d) the milestone belongs to the site -> 404
  e) the site is Residential (R2)      -> 404: receipts do not exist on tender sites
  f) user_can_record_receipt           -> 403: can see the site but may not record here
                                          (another BD, the site's BD on M2/M3, CEO/Admin/System
                                          Admin, SE, anyone on a Cancelled site)

THE SAVE (POST): form -> E4 warnings -> receipts.record_receipt() -> redirect (E6).
  * The view writes nothing itself: record_receipt is the only call that writes, and it
    uploads the proof, checks permission again under the lock, saves and logs.
  * NO OUTER TRANSACTION (FR31). record_receipt uploads the proof BEFORE its own atomic
    block and removes it if that block fails; an outer block rolling back after it
    returned would orphan the file. Nothing here opens one, and settings has no
    ATOMIC_REQUESTS (checked in B5-3 pre-flight), so non_atomic_requests is not needed.
    Do not wrap this view in transaction.atomic.

E4 — WARNINGS NEED A SECOND TAP. A likely duplicate (receipts.possible_duplicates) or a
total over the agreed amount (receipts.overpayment_warnings — the one copy of that rule)
re-renders the page with the values kept and a "Record anyway" tick. The page carries a
key of exactly the warnings it showed; the save goes ahead on the tick only when the key
recomputed on THIS request matches, so a tick from an older page cannot wave through a
duplicate that appeared since. A warning that appears only under the writer's lock (a
race) is shown under the success message.

FIN-M-B5-4a — ONE PAGE PER RECEIPT (receipt_detail) where everything else happens:
the figures, status, proof link, the query and its answer, the history and likely
duplicates, and below them only the actions the viewer may take. Each action is its own
small POST form to its own URL (receipt_confirm / _query / _answer / _reject /
_unconfirm), and each calls exactly one writer in receipts.py — the view writes nothing
itself, opens no transaction (FR31), and shows a writer's refusal as a message on the
receipt page. The proof opens through receipt_proof_open: a link signed per click (never
stored or put in the page), one access line logged per open.

THE RECEIPT GATE, IN THIS ORDER (_receipt_gate), on every request:
  a) receipts_live()                   -> 404 while CLIENT_RECEIPTS_LIVE is off
  b) _active_project                   -> 404 on a soft-deleted or unknown site
  c) user_can_view_project             -> refuse_site_view_404 (handover redirect or 404)
  d) the receipt belongs to the site   -> 404
  e) the site is Residential (R2)      -> 404
  f) user_can_view_receipt             -> 403: Finance, the recorder and whoever may record
                                          on the milestone now pass; CEO/Admin/System
                                          Admin, another BD, SE and the rest are refused

RETURN (F8, ruling 1): an action goes back to the receipt page with a one-line message,
unless the request carries Finance work's own FIN-M-RT marker (return=finance_work&rq=…,
views_finance_work.finance_work_return — a fixed name, never a URL), in which case it
goes back to that page's "Receipts to confirm" section with the same filters.

WHAT THIS MODULE NEVER DOES (E8, F10): notify anyone (B5-4b), or touch the milestone,
its tasks or the old money paths (the overview pencil, milestone_receive, the task
sync — B5-5).
"""
import hashlib

from django.contrib import messages
from django.http import Http404, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_POST, require_safe

from .decorators import login_required
from .forms_receipts import ReceiptRecordForm
from .models import (
    SUBJECT_PAYMENT_RECEIPT, PaymentReceipt, PaymentReceiptQuery, StatusTransition,
    log_activity,
)
from .money_format import format_rupees
from .permissions import (
    RECEIPT_PROJECT_TYPE, user_can_answer_receipt_query, user_can_confirm_receipt,
    user_can_query_receipt, user_can_record_receipt, user_can_reject_receipt,
    user_can_unconfirm_receipt, user_can_view_project, user_can_view_receipt,
)
from .receipt_storage import signed_receipt_proof_url
from .receipts import (
    RECEIPT_ENTITY, ReceiptRefused, answer_receipt_query, confirm_overpayment,
    confirm_receipt, overpayment_warnings, possible_duplicates, query_receipt,
    receipt_totals, receipts_live, record_receipt, reject_receipt, unconfirm_receipt,
)
from .site_bd import display_name
from .site_pm import refuse_site_view_404
from .views import _active_project, _milestone_rupees
# Not circular: views_finance_work imports views (loaded above) and never this module.
from .views_finance_work import (
    RECEIPTS_TO_CONFIRM_ANCHOR, finance_work_return, finance_work_url, return_query,
)

TEMPLATE = 'projects/receipt_record.html'

#: The milestone card on the project page: where a save returns to (E6).
CARD_ANCHOR = '#payment-milestones'

SAVED = 'Receipt recorded. Finance will check it against the bank.'

#: E3 / ruling 7: each row's status in words. A queried row is still "not yet confirmed"
#: in the totals, but the list says who it is waiting on.
STATUS_WORDS = {
    PaymentReceipt.RECORDED:      'Waiting for Finance',
    PaymentReceipt.QUERIED:       'Queried by Finance',
    PaymentReceipt.CONFIRMED:     'Confirmed',
    PaymentReceipt.REJECTED:      'Rejected',
    PaymentReceipt.AMOUNT_NEEDED: 'Old receipt - Finance to add the amount',
}

#: The list's badge colour per status; anything unlisted is grey.
STATUS_BADGES = {
    PaymentReceipt.CONFIRMED: 'bg-success',
    PaymentReceipt.REJECTED:  'bg-danger',
    PaymentReceipt.QUERIED:   'bg-warning text-dark',
}

#: FR16: a migrated row whose recorder cannot be recovered names no person.
MIGRATED_RECORDER = 'migrated — entered by Finance before 17 Nov'


def _receipt_milestone(request, project_id, milestone_pk):
    """Run gate steps a-f. Returns (project, milestone, None) when `request.user` may
    record on this milestone, else (None, None, response) — or raises Http404, which is
    the same refusal."""
    # a) E1: the screens are off until the B5 release — the page does not exist.
    if not receipts_live():
        raise Http404
    # b) Soft-deleted sites resolve to 404 here (R3). assigned_pm is joined so the
    #    permission helpers below compare it without a query.
    project = _active_project(project_id, select_related=('assigned_pm',))
    # c) Visibility first: someone who cannot see the site gets the site pages' 404 (or an
    #    outgoing PM's handover redirect), never a 403 that confirms the site exists.
    if not user_can_view_project(request.user, project):
        return None, None, refuse_site_view_404(request, project)
    # d) Through the related manager, so the milestone's .project is this loaded row (no
    #    second fetch) and a milestone of another site is a 404.
    milestone = get_object_or_404(project.milestones, pk=milestone_pk)
    # e) R2: receipts are a Residential thing; on any other site the page does not exist.
    if project.project_type != RECEIPT_PROJECT_TYPE:
        raise Http404
    # f) D6/FR12: the site's BD on M1, the PM side and Finance on any milestone. Everyone
    #    else who can see the site — another BD, the BD on M2/M3, CEO/Admin/System Admin
    #    (they do not take client money), SE, anyone on a Cancelled site — gets a bare 403.
    if not user_can_record_receipt(request.user, milestone):
        return None, None, HttpResponseForbidden()
    return project, milestone, None


def _duplicate_words(receipt):
    """'₹50,000 received 12 Nov 2026 by UPI (ref UTR123), recorded by Asha Rao — Waiting
    for Finance' for the duplicate warning."""
    ref = f' (ref {receipt.reference})' if receipt.reference else ''
    return (f'{_milestone_rupees(receipt.amount)} received '
            f'{receipt.date_received:%d %b %Y} by {receipt.get_mode_display()}{ref}, '
            f'recorded by {_recorder_words(receipt)} — '
            f'{STATUS_WORDS.get(receipt.status, receipt.status)}')


def _recorder_words(receipt):
    if receipt.recorded_by_id is None:
        return MIGRATED_RECORDER
    return display_name(receipt.recorded_by)


def _warnings(milestone, data):
    """E4: (duplicate receipts, overpayment warning texts, key). Read only.

    The key is a digest of exactly what was shown — the duplicates' ids and the
    overpayment text (which carries the totals) — so any change between the page and the
    second tap gives a different key and the page is shown again."""
    duplicates = list(possible_duplicates(milestone, data['amount'], data['date_received'],
                                          data['reference'])
                      .select_related('recorded_by__user').order_by('-recorded_at', '-pk'))
    # E5: the writer's own rule, asked before saving. The writer asks it again under its
    # lock; the page never adds receipts up itself.
    over = overpayment_warnings(milestone, data['amount'])
    if not duplicates and not over:
        return duplicates, over, ''
    shown = '|'.join([f'd{r.pk}' for r in duplicates] + list(over))
    return duplicates, over, hashlib.sha256(shown.encode('utf-8')).hexdigest()[:32]


def _receipt_rows(milestone, user):
    """This milestone's receipts, newest first, worded for the list (E3), each linking to
    its receipt page (F6). TWO queries for any number of receipts: the list, with the
    recorder's user joined for the name, and the milestone's unanswered queries (fetched
    by milestone rather than prefetched, so the count does not depend on the data).

    F6: a queried row says "Queried by Finance — answer" to a viewer who may answer it.
    Everyone on this page passed user_can_record_receipt for this milestone (gate f), so
    can_record=True is already known and the answer check costs no lookup per row; what
    is left is "not the person who raised the query"."""
    receipts = (PaymentReceipt.objects.filter(milestone=milestone)
                .select_related('recorded_by__user').order_by('-recorded_at', '-pk'))
    open_queries = {q.receipt_id: q for q in PaymentReceiptQuery.objects.filter(
        receipt__milestone=milestone, answered_at__isnull=True)}
    rows = []
    for r in receipts:
        # The loaded milestone (its site already joined), so the predicate reads no row.
        r.milestone = milestone
        old = r.status == PaymentReceipt.AMOUNT_NEEDED
        open_query = open_queries.get(r.pk)
        rows.append({
            'pk': r.pk,
            'href': reverse('receipt_detail', args=[milestone.project.project_id, r.pk]),
            'can_answer': (open_query is not None and user_can_answer_receipt_query(
                user, r, open_query, can_record=True)),
            'status': r.status,
            'status_words': STATUS_WORDS.get(r.status, r.get_status_display()),
            'badge': STATUS_BADGES.get(r.status, 'bg-secondary'),
            # ruling 5: an amount_needed row has no figures yet; say so instead of blanks,
            # so a recorder can see this money is already on record.
            'amount': None if old else _milestone_rupees(r.amount),
            'date_received': r.date_received,
            'mode': r.get_mode_display() if r.mode else '',
            'reference': r.reference,
            'recorded_by': _recorder_words(r),
        })
    return rows


def _render(request, project, milestone, form, *, duplicates=(), over=(), warning_key='',
            reattach=False):
    """The page, with `form` bound after a refusal or a warning so the typed values and
    errors show. Fixed query count whatever the number of receipts: one totals aggregate
    and one list query."""
    totals = receipt_totals([milestone])[milestone.pk]
    mode_value = form['mode'].value() if form.is_bound else ''
    return render(request, TEMPLATE, {
        'project': project,
        'milestone': milestone,
        'agreed': (_milestone_rupees(milestone.amount)
                   if milestone.amount is not None else None),
        'confirmed_total': _milestone_rupees(totals.confirmed),
        'unconfirmed_total': _milestone_rupees(totals.unconfirmed),
        'amount_needed_count': totals.amount_needed,
        'form': form,
        'modes': PaymentReceipt.MODE_CHOICES,
        'mode_value': mode_value,
        'other_mode': PaymentReceipt.OTHER,
        # E9: the one piece of Alpine state ("note required when Other"), decided here so
        # the template prints a literal true/false, never a value echoed from the request.
        'note_required': mode_value == PaymentReceipt.OTHER,
        'today': timezone.localdate(),
        'duplicates': [_duplicate_words(r) for r in duplicates],
        'over': over,
        'warning_key': warning_key,
        # E4: browsers clear a chosen file when a page is drawn again; say so.
        'reattach': reattach,
        'rows': _receipt_rows(milestone, request.user),
    })


@login_required
@require_http_methods(['GET', 'POST'])
def receipt_record(request, project_id, milestone_pk):
    """Show and save the record-a-receipt form for one payment milestone (FIN-M-B5-3).

    Access: the site's BD on M1 (FR12); the site's assigned PM or an active coordinator,
    and any active Finance user, on M1-M3 (FR8, FR11). Never CEO, Admin or System Admin
    (D6). Only on a live, non-Cancelled Residential site, and only while
    CLIENT_RECEIPTS_LIVE is on. 404 while the switch is off, on a deleted or non-
    Residential site, or for someone who cannot see the site; 403 for anyone else who can
    see it. Checked again on POST, and again by the writer under its lock.

    Runs outside any transaction (FR31): see the module docstring.
    """
    project, milestone, refusal = _receipt_milestone(request, project_id, milestone_pk)
    if refusal is not None:
        return refusal

    if request.method == 'GET':
        return _render(request, project, milestone, ReceiptRecordForm())

    form = ReceiptRecordForm(request.POST, request.FILES)
    # A file was chosen on this POST; if the page is drawn again it will be gone.
    reattach = bool(request.FILES.get('proof'))
    if not form.is_valid():
        return _render(request, project, milestone, form, reattach=reattach)
    data = form.cleaned_data

    # E4: recomputed on every POST. The tick counts only when this request's warnings are
    # the ones the page showed (same key); otherwise the page is drawn again with the
    # current warnings. The template never draws the tick already ticked.
    duplicates, over, key = _warnings(milestone, data)
    if key and not (data['record_anyway'] and data['warning_key'] == key):
        return _render(request, project, milestone, form, duplicates=duplicates,
                       over=over, warning_key=key, reattach=reattach)

    try:
        result = record_receipt(request.user, milestone, data['amount'],
                                data['date_received'], data['mode'],
                                reference=data['reference'], note=data['note'],
                                proof=data['proof'])
    except ReceiptRefused as exc:
        # Everything a field could say was said by the form; what the writer still refuses
        # (the site changed under the lock, the upload failed, the bucket is off) belongs
        # to no one field, so it goes at the top. The writer has already removed any file
        # it stored.
        form.add_error(None, str(exc))
        return _render(request, project, milestone, form, reattach=reattach)

    messages.success(request, SAVED)
    # A warning the person already saw and ticked is not repeated; one that appeared only
    # under the writer's lock (another receipt saved meanwhile) is shown under the success.
    for warning in result.warnings:
        if warning not in over:
            messages.warning(request, warning)
    return redirect(reverse('project_overview', args=[project.project_id]) + CARD_ANCHOR)


# ===========================================================================
# FIN-M-B5-4a — the receipt page and Finance's actions
# ===========================================================================

DETAIL_TEMPLATE = 'projects/receipt_detail.html'

#: F8: the one-line message after each action. "Query sent to …" names the recorder.
CONFIRMED = 'Confirmed.'
REJECTED = 'Rejected.'
UNCONFIRMED = 'Confirmation undone — waiting for Finance again.'
ANSWERED = 'Answer sent — waiting for Finance again.'

#: What each history row says, by its (from, to) move; the remark's label beside it.
#: An unlisted move (only a future migration's creation rows) falls back to its status.
HISTORY_WORDS = {
    ('', PaymentReceipt.RECORDED):                       ('Recorded', 'Note:'),
    (PaymentReceipt.RECORDED, PaymentReceipt.QUERIED):   ('Queried', 'Reason:'),
    (PaymentReceipt.QUERIED, PaymentReceipt.RECORDED):   ('Query answered', 'Answer:'),
    (PaymentReceipt.RECORDED, PaymentReceipt.CONFIRMED): ('Confirmed', 'Over the agreed amount:'),
    (PaymentReceipt.CONFIRMED, PaymentReceipt.RECORDED): ('Confirmation undone', 'Note:'),
    (PaymentReceipt.RECORDED, PaymentReceipt.REJECTED):  ('Rejected', 'Reason:'),
    (PaymentReceipt.QUERIED, PaymentReceipt.REJECTED):   ('Rejected', 'Reason:'),
    (PaymentReceipt.AMOUNT_NEEDED, PaymentReceipt.RECORDED): ('Amount added by Finance', 'Note:'),
}


def _person(profile):
    """A person's name for the page, or '' for no one."""
    return display_name(profile) if profile is not None else ''


def _receipt_gate(request, project_id, receipt_pk):
    """Run gate steps a-f (module docstring). Returns (project, receipt, None) when
    `request.user` may open this receipt, else (None, None, response) — or raises
    Http404, which is the same refusal."""
    # a) The screens are off until the B5 release — the page does not exist.
    if not receipts_live():
        raise Http404
    # b) Soft-deleted sites resolve to 404 here (R3). assigned_pm is joined so the
    #    permission helpers compare it without a query.
    project = _active_project(project_id, select_related=('assigned_pm',))
    # c) Someone who cannot see the site gets the site pages' 404 (or an outgoing PM's
    #    handover redirect), never a 403 that confirms the site exists.
    if not user_can_view_project(request.user, project):
        return None, None, refuse_site_view_404(request, project)
    # d) The receipt with everyone the page names joined (one query); a receipt of
    #    another site is a 404.
    receipt = get_object_or_404(
        PaymentReceipt.objects.select_related(
            'milestone', 'recorded_by__user', 'completed_by__user', 'confirmed_by__user',
            'rejected_by__user'),
        pk=receipt_pk, milestone__project=project)
    # The loaded site on the milestone, so no helper below fetches it again.
    receipt.milestone.project = project
    # e) R2: receipts are a Residential thing.
    if project.project_type != RECEIPT_PROJECT_TYPE:
        raise Http404
    # f) F5: Finance, the recorder, and whoever may record on the milestone now. Everyone
    #    else who can see the site — CEO/Admin/System Admin (they read totals, not
    #    receipts), another BD, the BD on M2/M3, SE — gets a bare 403, as on the record page.
    if not user_can_view_receipt(request.user, receipt):
        return None, None, HttpResponseForbidden()
    return project, receipt, None


def _receipt_url(project, receipt, marker=None):
    """The receipt page, carrying Finance work's return marker when there is one."""
    url = reverse('receipt_detail', args=[project.project_id, receipt.pk])
    return f'{url}?{return_query(marker)}' if marker else url


def _receipt_history(receipt):
    """Every move of the receipt, oldest first, each naming who and when, with its remark
    (the query reason, answer, overpayment note, undo note, rejection reason). ONE query
    on the ledger: every move writes exactly one row there (receipts.py), so nothing else
    needs merging — a confirmation that was later undone keeps its row and shows here."""
    rows = (StatusTransition.objects
            .filter(subject_type=SUBJECT_PAYMENT_RECEIPT, subject_id=receipt.pk)
            .select_related('actor__user').order_by('occurred_at', 'pk'))
    labels = dict(PaymentReceipt.STATUS_CHOICES)
    events = []
    for row in rows:
        what, remark_label = HISTORY_WORDS.get(
            (row.from_status, row.to_status), (labels.get(row.to_status, row.to_status), 'Note:'))
        events.append({'what': what, 'at': row.occurred_at,
                       'actor': _person(row.actor) or 'System',
                       'remark': row.remark, 'remark_label': remark_label})
    return events


def _queries(receipt):
    """The receipt's queries, oldest first, each with who asked and who answered. One
    query. `open` marks the one still waiting: unanswered AND the receipt still queried
    (ruling Q5 — a rejected receipt's unanswered query was overruled, not open)."""
    rows = []
    for q in (receipt.queries.select_related('raised_by__user', 'answered_by__user')
              .order_by('raised_at', 'pk')):
        rows.append({
            'query': q, 'raised_by': _person(q.raised_by),
            'answered_by': _person(q.answered_by),
            'open': q.answered_at is None and receipt.status == PaymentReceipt.QUERIED,
            'overruled': q.answered_at is None and receipt.status != PaymentReceipt.QUERIED,
        })
    return rows


@login_required
@never_cache
@require_safe
def receipt_detail(request, project_id, receipt_pk):
    """
    Show one client receipt: its figures, status, proof link, queries and answers,
    history and likely duplicates, and the actions the viewer may take (FIN-M-B5-4a).
    Access: active Finance; the receipt's recorder; anyone who may record on its
    milestone now (the PM side, the site's BD on M1). 403 for anyone else who can see the
    site (CEO, Admin and System Admin included); 404 for someone who cannot, on a deleted
    site, and for everyone while CLIENT_RECEIPTS_LIVE is off. GET only — every action
    posts to its own URL. never_cache, so Back after an action never shows the old state.
    """
    project, receipt, refusal = _receipt_gate(request, project_id, receipt_pk)
    if refusal is not None:
        return refusal
    user = request.user
    milestone = receipt.milestone
    marker = finance_work_return(request.GET, user)
    totals = receipt_totals([milestone])[milestone.pk]
    queries = _queries(receipt)
    open_query = next((q['query'] for q in queries if q['open']), None)

    # Each action is drawn only where its own helper allows it; the writer asks the same
    # helper again (and again under its lock), so a hidden button is never the control.
    can_confirm = user_can_confirm_receipt(user, receipt)
    # FR20: the note box only when confirming would go over the agreed amount — the
    # writer's own rule (receipts.confirm_overpayment), asked only when it could matter.
    over = confirm_overpayment(milestone, receipt) if can_confirm else ''
    # G9 (FIN-M-B5-4b): a confirm refused for going over without a note comes back here
    # with that same sentence as an error message, which base.html prints; the form then
    # leaves out its own copy and keeps only the note box, so the sentence shows once.
    # Reading the messages here does not consume them for this page: the storage keeps
    # what it loaded, and base.html iterates the same list in this response.
    over_in_messages = bool(over) and any(
        str(m) == over for m in messages.get_messages(request))
    duplicates = []
    if receipt.amount is not None:
        # Other live receipts on this milestone that look like the same money (same
        # amount and date, or the same reference): possible_duplicates already leaves
        # rejected ones out; this receipt itself is excluded here. One query.
        for d in (possible_duplicates(milestone, receipt.amount, receipt.date_received,
                                      receipt.reference)
                  .exclude(pk=receipt.pk).select_related('recorded_by__user')
                  .order_by('-recorded_at', '-pk')):
            duplicates.append({'words': _duplicate_words(d), 'href': _receipt_url(project, d)})

    return render(request, DETAIL_TEMPLATE, {
        'project': project,
        'milestone': milestone,
        'receipt': receipt,
        'agreed': (_milestone_rupees(milestone.amount)
                   if milestone.amount is not None else None),
        'confirmed_total': _milestone_rupees(totals.confirmed),
        'unconfirmed_total': _milestone_rupees(totals.unconfirmed),
        'amount': _milestone_rupees(receipt.amount) if receipt.amount is not None else None,
        'mode': receipt.get_mode_display() if receipt.mode else '',
        'status_words': STATUS_WORDS.get(receipt.status, receipt.get_status_display()),
        'badge': STATUS_BADGES.get(receipt.status, 'bg-secondary'),
        'recorded_by': _recorder_words(receipt),
        'completed_by': _person(receipt.completed_by),
        'confirmed_by': _person(receipt.confirmed_by),
        'rejected_by': _person(receipt.rejected_by),
        'has_proof': bool(receipt.proof_path),
        'queries': queries,
        'can_answer': (open_query is not None
                       and user_can_answer_receipt_query(user, receipt, open_query)),
        'can_confirm': can_confirm,
        'over': over,
        'over_in_messages': over_in_messages,
        'can_query': user_can_query_receipt(user, receipt),
        'can_reject': user_can_reject_receipt(user, receipt),
        'can_unconfirm': user_can_unconfirm_receipt(user, receipt),
        'duplicates': duplicates,
        'history': _receipt_history(receipt),
        'marker': marker,
        'finance_work_href': (finance_work_url(marker) + RECEIPTS_TO_CONFIRM_ANCHOR
                              if marker else None),
        'record_href': (reverse('receipt_record', args=[project.project_id, milestone.pk])
                        if user_can_record_receipt(user, milestone) else None),
    })


def _act(request, project_id, receipt_pk, action):
    """Shared body of the five action views: the receipt gate, then `action(receipt)` —
    one writer call returning the success message — then F8's return. A ReceiptRefused
    (not allowed, a blank reason, over the agreed amount without a note, the receipt
    moved meanwhile) is shown as a message on the receipt page, never a 500; the writer
    has written nothing then. No transaction is opened here (FR31)."""
    project, receipt, refusal = _receipt_gate(request, project_id, receipt_pk)
    if refusal is not None:
        return refusal
    marker = finance_work_return(request.POST, request.user)
    try:
        done = action(receipt)
    except ReceiptRefused as exc:
        messages.error(request, str(exc))
        return redirect(_receipt_url(project, receipt, marker))
    messages.success(request, done)
    if marker:
        return redirect(finance_work_url(marker) + RECEIPTS_TO_CONFIRM_ANCHOR)
    return redirect(_receipt_url(project, receipt))


@login_required
@require_POST
def receipt_confirm(request, project_id, receipt_pk):
    """
    Confirm a receipt against the bank (FR2). Access: active Finance, never the receipt's
    recorder (FR11), only while it is `recorded`; over the agreed amount only with a note
    (FR20). POST only. receipts.confirm_receipt decides and writes.
    """
    def action(receipt):
        confirm_receipt(request.user, receipt,
                        overpayment_note=request.POST.get('overpayment_note', ''))
        return CONFIRMED
    return _act(request, project_id, receipt_pk, action)


@login_required
@require_POST
def receipt_query(request, project_id, receipt_pk):
    """
    Query a recorded receipt with a reason (FR19). Access: active Finance, never the
    receipt's recorder. POST only. receipts.query_receipt decides and writes.
    """
    def action(receipt):
        query_receipt(request.user, receipt, request.POST.get('reason', ''))
        return f'Query sent to {_recorder_words(receipt)}.'
    return _act(request, project_id, receipt_pk, action)


@login_required
@require_POST
def receipt_answer(request, project_id, receipt_pk):
    """
    Answer the open query on a receipt, in text only (FR19, FR27). Access: the recorder,
    or anyone who may record on the milestone now, never whoever raised the query. POST
    only. receipts.answer_receipt_query decides and writes.
    """
    def action(receipt):
        answer_receipt_query(request.user, receipt, request.POST.get('answer', ''))
        return ANSWERED
    return _act(request, project_id, receipt_pk, action)


@login_required
@require_POST
def receipt_reject(request, project_id, receipt_pk):
    """
    Reject a recorded or queried receipt with a reason (FR19). Access: active Finance.
    Final. POST only. receipts.reject_receipt decides and writes.
    """
    def action(receipt):
        reject_receipt(request.user, receipt, request.POST.get('reason', ''))
        return REJECTED
    return _act(request, project_id, receipt_pk, action)


@login_required
@require_POST
def receipt_unconfirm(request, project_id, receipt_pk):
    """
    Undo a receipt's confirmation with a mandatory note (FR35): back to waiting for
    Finance, the confirmation kept in the history. Access: any active Finance user. POST
    only. receipts.unconfirm_receipt decides and writes.
    """
    def action(receipt):
        unconfirm_receipt(request.user, receipt, request.POST.get('note', ''))
        return UNCONFIRMED
    return _act(request, project_id, receipt_pk, action)


@login_required
@never_cache
@require_safe
def receipt_proof_open(request, project_id, receipt_pk):
    """
    Open a receipt's proof: a redirect to a link signed now, valid for 300 seconds
    (receipt_storage.signed_receipt_proof_url), and one access line per open, written
    after the link is minted so a failed open logs nothing (the challan documents'
    pattern). Access: whoever may open the receipt page (F5); 403 for anyone else who can
    see the site, 404 without a proof or while CLIENT_RECEIPTS_LIVE is off. GET only.
    """
    project, receipt, refusal = _receipt_gate(request, project_id, receipt_pk)
    if refusal is not None:
        return refusal
    if not receipt.proof_path:
        raise Http404
    url = signed_receipt_proof_url(receipt)
    if not url:
        messages.error(request, 'The proof cannot be opened right now. Try again, or tell '
                                'Finance if it keeps failing.')
        return redirect(_receipt_url(project, receipt))
    # Ruling 2: the line names the amount, so it is a money line (MONEY_ACTIVITY_CODES:
    # only people who may see the site's money see it) and an access record (kept off the
    # overview's Recent activity; *_opened never counts as site activity). Never the file
    # name: it is the uploader's text and may name the client.
    log_activity(project, request.user.profile,
                 f'Opened the proof of the {format_rupees(receipt.amount)} receipt on '
                 f'{receipt.milestone.milestone_name}',
                 entity_type=RECEIPT_ENTITY, entity_id=receipt.pk,
                 action_code='receipt_proof_opened')
    return redirect(url)
