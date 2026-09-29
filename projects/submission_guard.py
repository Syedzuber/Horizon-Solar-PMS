"""
Duplicate submission on the PO / PI, purchases and payment screens (28 Sep 2026).

Two guards, both shared by the five create paths and the documents append:

1. THE URL-HELD KEY (approval_create's pattern, Approvals 2a). A form GET without a
   valid ?key= is redirected to the same URL with a fresh one, and the form's hidden
   client_uuid carries that key. The key is therefore part of the browser's history
   entry: Back and reload return the SAME key even when the browser fetches the page
   again. A key minted per GET was replaced on every re-fetch, and a re-fetched form
   posted with the same values made a second record (audit, 28 Sep, A2 b_refetch).

   A key that has already made a record is answered by already_submitted(): the record,
   with a message saying so (D2). R-14's unique client_uuid columns are unchanged; only
   how the key is issued, and what a repeat says, moved.

2. THE CONTENT-MATCH WARNING (D3). A fresh form (no key, so a new one) with the same
   values still makes a second record. The matchers below find a recent record that
   looks like the same thing (for a PO / PI record, also one of any age carrying the
   same vendor and PO or PI number, D-A44), and the view re-renders its form with a warning and an
   override tick box instead of writing. NEVER A BLOCK: with confirm_duplicate=1 in the
   POST the view proceeds exactly as before. The check runs before any upload, so a
   warning leaves no file in storage.

NO CACHE HEADERS, DELIBERATELY. no-store would force a re-fetch on Back; with the key in
the URL that is now harmless, but before it the re-fetch was the bug, and nothing here
needs the browser to forget the page.

Everything here reads; nothing here writes a row.
"""
import uuid as _uuid
from datetime import timedelta

from django.contrib import messages
from django.db.models import Q
from django.http import HttpResponseForbidden
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import dateformat, timezone
from django.utils.html import format_html

from .models import (
    PaymentRequest, VendorOrder, VendorOrderDocument, committed_total,
    VENDOR_ORDER_DOC_TYPE_CHOICES,
)
from .permissions import user_can_view_vendor_order

#: How far back a content match looks. Long enough to cover a form abandoned, reopened
#: and filled again; short enough that a genuine second order of the same size a day
#: later is not questioned.
DUPLICATE_WINDOW = timedelta(minutes=30)

#: D2's wording, shown on every key match.
ALREADY_SUBMITTED = 'This form was already submitted. You are viewing the record it created.'

_DOC_TYPE_LABELS = dict(VENDOR_ORDER_DOC_TYPE_CHOICES)


# ---------------------------------------------------------------------------
# 1. The URL-held key
# ---------------------------------------------------------------------------

def url_key(request):
    """The page's ?key= as a UUID, or None when absent or malformed. A malformed key is
    treated exactly as a missing one: the caller issues a new key."""
    try:
        return _uuid.UUID((request.GET.get('key') or '').strip())
    except ValueError:
        return None


def keyed_redirect(request):
    """The same page, every other query parameter kept (?program=, ?project=, ?order=),
    with a fresh ?key= — approval_create's redirect, generalised to the page's own path."""
    params = request.GET.copy()
    params['key'] = str(_uuid.uuid4())
    return redirect(f'{request.path}?{params.urlencode()}')


def fresh_form_url(request):
    """This form with no key, so opening it issues a new one. Other parameters stay."""
    params = request.GET.copy()
    params.pop('key', None)
    query = params.urlencode()
    return f'{request.path}?{query}' if query else request.path


def already_submitted(request, order):
    """The answer to a key that has already made a record (D2): the record `order` (for a
    payment, its order), with the message and a link to a fresh form.

    NEVER SILENT, AND NEVER A LEAK. When the person asking cannot open `order`, the
    message is still shown, followed by a 403 — and the response names nothing about the
    record, not even its pk, which a redirect to it would have put in the address bar.
    The body is built here rather than from 403.html because that page is standalone and
    renders no messages (see its header), so the message would otherwise surface on some
    later page instead.
    """
    text = format_html('{} <a href="{}">Open a fresh form</a>.',
                       ALREADY_SUBMITTED, fresh_form_url(request))
    # Who may be sent to the record: whoever may read it — the order page's own
    # predicate. Anyone else gets the message and the 403, never the record.
    if not user_can_view_vendor_order(request.user, order):
        return HttpResponseForbidden(format_html('<p>{}</p>', text))
    messages.warning(request, text)
    return redirect('vendor_order_detail', order_pk=order.pk)


def confirmed(request):
    """True when the person ticked the warning's override ("Create anyway" /
    "Attach anyway")."""
    return request.POST.get('confirm_duplicate') == '1'


# ---------------------------------------------------------------------------
# 2. The content-match warning
# ---------------------------------------------------------------------------

def _person(user):
    return user.get_full_name() or user.username


def _age(when, now):
    minutes = int((now - when).total_seconds() // 60)
    if minutes < 1:
        return 'less than a minute ago'
    return '1 minute ago' if minutes == 1 else f'{minutes} minutes ago'


def _order_ref(order):
    """How a record is named on screen — order_views._order_ref, which this module cannot
    import (order_views imports this module)."""
    return order.po_number or order.pi_number or f'#{order.pk}'


def _warning(heading, lines, url, override_label, notes=()):
    """The context a form template draws its warning from. The payment and documents
    warnings pass text lines and one `url`; the order warning passes _order_line() dicts,
    each with its own link, `url=None`, and `notes` drawn once below the lines."""
    return {'heading': heading, 'lines': lines, 'url': url,
            'override_label': override_label, 'notes': list(notes)}


def document_size_kb(upload):
    """The size recorded on VendorOrderDocument.file_size_kb. One definition, used by
    the write and by the documents matcher, so a match compares like with like."""
    return max(1, upload.size // 1024)


# ── Orders ────────────────────────────────────────────────────────────────────
#
# A LIST OF RULES, SO A SECOND ONE IS ONE ENTRY. Each entry is a (matcher, describer)
# pair. The matcher takes the candidate (a dict of created_by, vendor, total_amount,
# po_number, pi_number) and the window's start, and returns a VendorOrder queryset. The
# describer takes the candidate, that queryset (newest first, vendor and creator joined)
# and now, and returns [(order, line)]: the records it names and what it says of each.
# A rule carries its own wording because the two rules tell SCM different things — "you
# may have just submitted this" against "this PO already has a record; add to it".

def _same_person_vendor_total(candidate, since):
    """The re-fetch duplicate: the same person, the same vendor and the same total from
    the PO, within the window — unless the two records DISAGREE on a reference number.
    VendorOrder has no soft delete, so there is nothing to exclude on that account.

    Two different PO numbers (or two different PI numbers), both filled in, are two
    different documents from the vendor, whatever else matches: that is two orders, not
    one entered twice. A blank number never rules a match out — it says nothing, and the
    re-fetch duplicate is exactly the case where a number may be left off the second time.
    """
    matches = VendorOrder.objects.filter(
        created_by=candidate['created_by'], vendor=candidate['vendor'],
        total_amount=candidate['total_amount'], created_at__gte=since)
    for field in ('po_number', 'pi_number'):
        number = candidate[field]
        if number:
            # Exclude only a record whose number is filled in AND different; a blank
            # one stays a match.
            matches = matches.exclude(~Q(**{field: ''}) & ~Q(**{field: number}))
    return matches


def _describe_recent(candidate, matches, now):
    """The re-fetch duplicate's wording (D3), for the newest match only."""
    match = matches.first()
    if match is None:
        return []
    return [(match, _order_line(
        f'{_order_ref(match)} with {match.vendor.name}, total ₹{match.total_amount}, '
        f'recorded by {_person(match.created_by.user)} {_age(match.created_at, now)}.',
        match, 'Open it', ' to check before you continue.'))]


def _same_vendor_number(candidate, since):
    """D-A44: the same vendor and the same PO number, or the same vendor and the same PI
    number, recorded at ANY time by ANYONE — `since` is deliberately not read. One PO is
    one record; a further payment or document on it belongs on the record already there.

    Case-insensitive on the trimmed numbers (_parse_header trims), so "po-100" finds
    "PO-100". A BLANK NUMBER NEVER MATCHES: it names no document, and two records that
    both left the PO number off share nothing.
    """
    numbers = Q()
    for field in ('po_number', 'pi_number'):
        if candidate[field]:
            numbers |= Q(**{f'{field}__iexact': candidate[field]})
    if not numbers:
        # Unreachable from the three raise paths (_parse_header refuses a record with
        # neither number), but a numberless candidate must match nothing, not every
        # numberless record.
        return VendorOrder.objects.none()
    return VendorOrder.objects.filter(vendor=candidate['vendor']).filter(numbers)


#: D-A44's closing advice, drawn once below the lines however many records matched.
#: Value changes go to "Create anyway" because a PO / PI record has no edit path yet
#: (SECONDARY_FINDINGS.md) — its total cannot be raised to take a PI of a new value.
NUMBER_MATCH_ADVICE = ("Create anyway only for a revised PO, a blanket/rate PO, or a PI "
                       "that changes the order's value. PO/PI records cannot be edited yet.")


def _describe_number(candidate, matches, now):
    """D-A44's wording: the newest record carrying the PO number and the newest carrying
    the PI number — one line, worded "PO", when that is the same record; two when not.

    Reads every match, newest first, because the newest PI match may sit behind older
    PO matches. The rows are one vendor's records of one or two numbers: a handful.
    scope_label reads each record's sites (and their projects) and programs, so they are
    prefetched — at most four queries, and none when nothing matched (an empty result
    prefetches nothing).
    """
    found = {}
    for order in matches.prefetch_related('sites__project', 'programs__program'):
        for kind, field in (('PO', 'po_number'), ('PI', 'pi_number')):
            number = candidate[field]
            if (kind not in found and number
                    and getattr(order, field).lower() == number.lower()):
                found[kind] = order
    described = []
    for kind, field in (('PO', 'po_number'), ('PI', 'pi_number')):
        order = found.get(kind)
        # PO comes first, so a record both numbers found is already named, as "PO".
        if order is None or any(named.pk == order.pk for named, _ in described):
            continue
        created = dateformat.format(timezone.localtime(order.created_at), 'j M Y')
        described.append((order, _order_line(
            f'{kind} {getattr(order, field)} is already recorded for {order.vendor.name} '
            f'({order.scope_label}, created {created} by {_person(order.created_by.user)}). '
            f'If this is another payment or document on the same {kind}, add it to that '
            f'record instead.',
            order, 'Open record', note=NUMBER_MATCH_ADVICE)))
    return described


#: Least specific first: a record found by more than one rule is described by the LAST
#: of them (see order_duplicate), so D-A44's "add it to that record" wins over the
#: re-fetch wording for the same record.
ORDER_MATCH_RULES = (
    (_same_person_vendor_total, _describe_recent),
    (_same_vendor_number, _describe_number),
)


def _order_line(text, order, link, link_tail='', note=''):
    """One record named in the order warning, with its own link — two rules can name two
    different records, so the warning's single `url` cannot serve."""
    return {'text': text, 'url': reverse('vendor_order_detail', args=[order.pk]),
            'link': link, 'link_tail': link_tail, 'note': note}


def order_duplicate(created_by, vendor, total_amount, po_number='', pi_number=''):
    """The warning for a new PO / PI record that looks like one already recorded, or
    None. Runs before any upload.

    EVERY RULE RUNS — one query each, plus a matching rule's naming queries — so a record
    the re-fetch rule found and a different one found by its number are both named. ONE
    LINE PER RECORD: a record found twice keeps the place it first appeared and takes the
    later rule's wording (reassigning a dict key keeps its position).
    """
    now = timezone.now()
    since = now - DUPLICATE_WINDOW
    candidate = {'created_by': created_by, 'vendor': vendor, 'total_amount': total_amount,
                 'po_number': po_number, 'pi_number': pi_number}
    lines = {}
    for matcher, describe in ORDER_MATCH_RULES:
        # Newest first: the most recent match is the one the person most likely means.
        # select_related because every wording names the vendor and the creator.
        matches = (matcher(candidate, since).select_related('vendor', 'created_by__user')
                   .order_by('-created_at', '-pk'))
        for order, line in describe(candidate, matches, now):
            lines[order.pk] = line
    if not lines:
        return None
    lines = list(lines.values())
    return _warning('This looks like a PO / PI already recorded.', lines, None,
                    'Create anyway',
                    notes=dict.fromkeys(line['note'] for line in lines if line['note']))


# ── Payments ──────────────────────────────────────────────────────────────────

def payment_duplicate(order, amount):
    """The warning for a payment request that looks like one already raised against
    `order`: the same amount within the window, by anyone. None when there is none.

    Called OUTSIDE the order-row lock (D3): a warning is advice, not arithmetic, and the
    lock in _create_order_payment exists for the balance check alone.
    """
    now = timezone.now()
    # The window's payments of this amount on this order, newest first; a handful of
    # rows at most, filtered by committed_total() below.
    rows = (PaymentRequest.objects
            .filter(vendor_order=order, amount=amount,
                    requested_date__gte=now - DUPLICATE_WINDOW)
            .select_related('requested_by')
            .order_by('-requested_date', '-pk'))
    for payment in rows:
        # COMMITTED_TOTAL'S OWN RULE decides whether this payment still stands, rather
        # than a second copy of its status list: a payment it does not count (rejected)
        # is not a repeat, because re-raising it is how a rejection is recovered. Every
        # payment counts at a positive effective_amount (amount > 0 on entry, and a CHECK
        # holds approved_amount > 0), so a total above zero means "counted".
        if committed_total([payment]) > 0:
            return _warning(
                'This looks like a payment request already raised.',
                [f'₹{payment.amount} against {_order_ref(order)}, requested by '
                 f'{_person(payment.requested_by)} {_age(payment.requested_date, now)}.'],
                reverse('vendor_order_detail', args=[order.pk]),
                'Create anyway')
    return None


# ── Documents ─────────────────────────────────────────────────────────────────

def documents_duplicate(order, documents):
    """The warning for documents identical to ones attached to `order` within the
    window — same type, file name and size in KB — naming every such file. None when
    no file matches. `documents` is _validate_document_slots()' list."""
    now = timezone.now()
    # Every document attached to this order within the window — its raise's own files
    # included, since re-attaching the PO that came with the order is the same repeat.
    recent = list(VendorOrderDocument.objects
                  .filter(order=order, uploaded_at__gte=now - DUPLICATE_WINDOW)
                  .select_related('uploaded_by__user')
                  .order_by('-uploaded_at', '-pk'))
    lines = []
    for doc in documents:
        size_kb = document_size_kb(doc['file'])
        for row in recent:
            if (row.doc_type == doc['doc_type'] and row.file_name == doc['file'].name
                    and row.file_size_kb == size_kb):
                lines.append(f'{row.file_name} ({_DOC_TYPE_LABELS.get(row.doc_type, row.doc_type)}), '
                             f'attached by {_person(row.uploaded_by.user)} '
                             f'{_age(row.uploaded_at, now)}.')
                break
    if not lines:
        return None
    return _warning('These files look like ones already attached to this order.', lines,
                    reverse('vendor_order_detail', args=[order.pk]), 'Attach anyway')
