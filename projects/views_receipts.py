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

WHAT THIS MODULE NEVER DOES (E8): notify anyone, show a proof link, act for Finance
(query, confirm, reject), or touch the old money paths (the overview pencil,
milestone_receive, the task sync). Those are B5-4 and B5-5.
"""
import hashlib

from django.contrib import messages
from django.http import Http404, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from .decorators import login_required
from .forms_receipts import ReceiptRecordForm
from .models import PaymentReceipt
from .permissions import (
    RECEIPT_PROJECT_TYPE, user_can_record_receipt, user_can_view_project,
)
from .receipts import (
    ReceiptRefused, overpayment_warnings, possible_duplicates, receipt_totals,
    receipts_live, record_receipt,
)
from .site_bd import display_name
from .site_pm import refuse_site_view_404
from .views import _active_project, _milestone_rupees

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


def _receipt_rows(milestone):
    """This milestone's receipts, newest first, worded for the list (E3). One query; the
    recorder's user is joined for the name. No proof link (B5-4)."""
    receipts = (PaymentReceipt.objects.filter(milestone=milestone)
                .select_related('recorded_by__user').order_by('-recorded_at', '-pk'))
    rows = []
    for r in receipts:
        old = r.status == PaymentReceipt.AMOUNT_NEEDED
        rows.append({
            'pk': r.pk,
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
        'rows': _receipt_rows(milestone),
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
