"""
DR-2 — the released design's drawings and BOQ quantities on project_overview, for the
people building an OPEX site. READ-ONLY for every viewer; nothing here writes design or
BOQ state.

TWO ENTRY POINTS
----------------
  site_drawings_context(project)   the card's context, called by views.project_overview
  site_drawing_open(request, pk)   one drawing PDF: a redirect to a link signed at the
                                   click, logged to the site's activity feed

Both read the released design through _design_state() and _released_drawings(), so the
card can never offer a link the open view would refuse, or the reverse.

WHAT "RELEASED" MEANS HERE
--------------------------
`DesignAssignment.status == 'released'`, and the released attempt is the one
`current_attempt_number` points at: release (design_pm_approve) does not move that
pointer, and an accepted change request moves the site out of `released` in the same
write that opens the next attempt. The attempt term is load-bearing — older attempts keep
their own `is_current=True` zips, so "the project's current zip" would pick a wrong one.

THE STATES (exactly one)
------------------------
  not_opex        Residential / CAPEX: the card is not drawn (DR-6)
  not_released    no assignment, or never released (DR-1)
  under_revision  not released now, but a StatusTransition out of `released` exists:
                  the design the site was building from has been reopened (DR-2)
  released        drawings + BOQ, with `pending_change` when a change request is open

QUERIES: one for the assignment (its three flags are subqueries on that row), then — for
a released site only — one each for the zip, its PDF rows and the BOQ rows. Constant in
the number of PDFs and BOQ lines (DR-5); a carried-forward chain longer than two hops
costs one more per hop inside cad_pdf_source(), which no real chain reaches.
"""
import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Exists, OuterRef, Subquery, Value
from django.db.models.functions import Coalesce
from django.http import Http404
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone

from .design_storage import (
    DesignStorageError, cad_pdf_source, cad_zip_pdf_entries, get_design_file_url,
)
from .models import (
    CAD_PDF_EXTRACTED, CHANGE_REQUEST_OPEN_VERDICTS, DESIGN_FILE_CAD_ZIP, DESIGN_RELEASED,
    SUBJECT_DESIGN_ASSIGNMENT, BOQCorrection, BOQItem, DesignAssignment,
    DesignChangeRequest, DesignFile, DesignFilePdf, StatusTransition, VendorBrand,
    log_activity,
)
from .permissions import user_can_view_site_drawings
from .site_pm import refuse_site_view_404

logger = logging.getLogger(__name__)

STATE_NOT_OPEX       = 'not_opex'
STATE_NOT_RELEASED   = 'not_released'
STATE_UNDER_REVISION = 'under_revision'
STATE_RELEASED       = 'released'

#: DR-5. Long enough for a site phone's PDF viewer to finish its range requests on a slow
#: connection, short enough that a forwarded link is dead before it is useful.
DRAWING_LINK_SECONDS = 300

#: DR-10. A PDF the designer put in the CAD zip that is really the BOQ sheet. It may carry
#: rates, so it is never shown; the card shows the allowlisted BOQ columns instead.
HIDDEN_NAME_MARKER = 'boq'


def _is_hidden(pdf):
    """DR-10: an extracted PDF whose display_name contains 'boq', in any case."""
    return HIDDEN_NAME_MARKER in pdf.display_name.casefold()


def _design_state(project):
    """(state, assignment) for `project`. One query, none on a non-OPEX site.

    The assignment row carries three subqueries the card needs, so no state costs a
    second query to decide:

      open_change       an OPEN change request (with_pm or pending) on the current
                        attempt. On a released site that attempt is the released one,
                        and every open request on it was raised after release: an open
                        request blocks both review gates, and none can be raised at
                        awaiting_pm_approval, so a site cannot be released with one open.
      left_released_at  the latest StatusTransition out of `released`. Its existence is
                        what separates under_revision from not_released (DR-2): the
                        acceptance clears `released_at`, so the assignment row alone
                        cannot say the site was ever released. The ledger row is written
                        in the same transaction as the acceptance.
      corrected_at      the latest BOQCorrection after `released_at` (DR-3). Corrections
                        made during review, before release, are not news to the site.
    """
    if project.project_type != 'OPEX':
        return STATE_NOT_OPEX, None
    assignment = (
        DesignAssignment.objects.filter(project=project)
        .annotate(
            open_change=Exists(DesignChangeRequest.objects.filter(
                attempt__assignment=OuterRef('pk'),
                attempt__attempt_number=OuterRef('current_attempt_number'),
                verdict__in=CHANGE_REQUEST_OPEN_VERDICTS)),
            left_released_at=Subquery(
                StatusTransition.objects.filter(
                    subject_type=SUBJECT_DESIGN_ASSIGNMENT, subject_id=OuterRef('pk'),
                    from_status=DESIGN_RELEASED)
                .order_by('-occurred_at').values('occurred_at')[:1]),
            corrected_at=Subquery(
                BOQCorrection.objects.filter(
                    boq__project_id=OuterRef('project_id'),
                    corrected_at__gt=OuterRef('released_at'))
                .order_by('-corrected_at').values('corrected_at')[:1]),
        )
        .first()
    )
    if assignment is None:
        return STATE_NOT_RELEASED, None
    if assignment.status == DESIGN_RELEASED:
        return STATE_RELEASED, assignment
    if assignment.left_released_at is not None:
        return STATE_UNDER_REVISION, assignment
    return STATE_NOT_RELEASED, assignment


def _released_drawings(assignment):
    """The released zip's PDFs that may be shown: (drawings, hidden_count, unavailable).

    Two queries: the released attempt's current cad_zip, then every DesignFilePdf row of
    its source (all outcomes, because "unavailable" needs the entries that have no
    extracted row as well as the ones that do).

    `unavailable` (DR-9 and legacy) is True when the released attempt has no zip (a
    legacy cad_pdf / cad_dwg package, or none), when the zip lists no .pdf entry at all
    (only a zip older than the pdf+dwg upload rule can), or when any .pdf entry has no
    `extracted` row — not extracted yet, or extraction refused it (encrypted, too large,
    not really a PDF). Each of those is a drawing the site cannot open from here, and
    Design is who can fix it.

    boq_pdf and boq_excel DesignFiles are never read: the kind filter is cad_zip only.
    """
    zip_row = (
        DesignFile.objects
        .filter(attempt__assignment=assignment,
                attempt__attempt_number=assignment.current_attempt_number,
                kind=DESIGN_FILE_CAD_ZIP, is_current=True)
        # Two hops of carried_forward_from preloaded, so cad_pdf_source() walks a real
        # chain (a carry happens once per rework loop) without a query of its own.
        .select_related('carried_forward_from__carried_forward_from')
        .order_by('-version')
        .first()
    )
    if zip_row is None:
        return [], 0, True
    try:
        # EVERY reader resolves through cad_pdf_source(): a carried copy's PDFs are
        # recorded against its original (DR-1).
        source = cad_pdf_source(zip_row)
    except DesignStorageError:
        logger.warning('site drawings: DesignFile %s has an unresolvable carry chain.',
                       zip_row.pk, exc_info=True)
        return [], 0, True

    rows = list(source.extracted_pdfs.all())
    extracted = [row for row in rows if row.outcome == CAD_PDF_EXTRACTED]
    drawings = [row for row in extracted if not _is_hidden(row)]
    extracted_entries = {row.entry_name for row in extracted}
    candidates = cad_zip_pdf_entries(source)
    unavailable = (not candidates
                   or any(name not in extracted_entries for name in candidates))
    return drawings, len(extracted) - len(drawings), unavailable


def _make_label(**brand_filter):
    """The VendorBrand label for a BOQ row's make_preference, as a subquery (DR-3).

    Same lookup boq_detail uses for its static Make cell — a brand scoped to the row's
    category first, then an unscoped one, the greater make_brand winning a tie as that
    dict's last write does — WITHOUT its fallback to Vendor.name: the card never shows a
    vendor name, so a make with no brand label is blank.
    """
    return Subquery(
        VendorBrand.objects.filter(vendor_id=OuterRef('make_preference_id'), **brand_filter)
        .order_by('-make_brand').values('make_brand')[:1])


def _boq_rows(project):
    """The site's BOQ lines as dicts with EXACTLY the six DR-3 keys. One query.

    THE ALLOWLIST IS THE SELECT LIST. values_list() names the five columns and the brand
    label, so ordered_quantity, ordered_vendor and every other column are never read from
    the database at all, let alone passed to the template. Never model_to_dict(), never a
    bare .values().
    """
    rows = (
        BOQItem.objects.filter(boq__project=project)
        .annotate(make_label=Coalesce(_make_label(category__name=OuterRef('category')),
                                      _make_label(category__isnull=True),
                                      Value('')))
        .order_by('serial_no', 'pk')
        .values_list('item_master__code', 'description', 'category', 'uom',
                     'boq_quantity', 'make_label')
    )
    return [
        {'code': code or '', 'description': description, 'category': category,
         'unit': unit, 'quantity': quantity, 'make': make}
        for code, description, category, unit, quantity, make in rows
    ]


def _local_date(value):
    return timezone.localtime(value).date() if value is not None else None


def site_drawings_context(project):
    """The site drawings card's context for `project`: a dict with exactly one `state`.

    Visibility is the CALLER's: project_overview has already asked user_can_view_project,
    and `not_opex` here is the other half of user_can_view_site_drawings().

      not_opex        {'state'}                         — not drawn (DR-6)
      not_released    {'state'}
      under_revision  {'state', 'since'}                — no drawings, no BOQ (DR-2)
      released        {'state', 'pending_change', 'drawings', 'hidden_count',
                       'unavailable', 'boq_rows', 'corrected_on'}
    """
    state, assignment = _design_state(project)
    if state == STATE_UNDER_REVISION:
        return {'state': state, 'since': _local_date(assignment.left_released_at)}
    if state != STATE_RELEASED:
        return {'state': state}
    drawings, hidden_count, unavailable = _released_drawings(assignment)
    return {
        'state':          state,
        'pending_change': assignment.open_change,
        'drawings':       drawings,
        'hidden_count':   hidden_count,
        'unavailable':    unavailable,
        'boq_rows':       _boq_rows(project),
        'corrected_on':   _local_date(assignment.corrected_at),
    }


@login_required
def site_drawing_open(request, pk):
    """Open one released drawing PDF: a redirect to a link signed now, valid for
    DRAWING_LINK_SECONDS, opened inline (no download name, so the non-ASCII display_name
    of S2 never reaches a header). One activity row per open.

    Access: anyone user_can_view_site_drawings() admits — every viewer of an OPEX site.
    Everyone else gets the bare 404 of refuse_site_view_404(), as does a PDF that is not
    one the card would list: not of the site's CURRENT released zip (a superseded zip, or
    a site no longer released), not extracted, or hidden by DR-10. The 404 is identical
    for a pk that does not exist, so a guessed id says nothing about what is there.
    """
    pdf = (DesignFilePdf.objects
           .select_related('design_file__attempt__assignment__project')
           .filter(pk=pk).first())
    if pdf is None:
        raise Http404
    project = pdf.design_file.attempt.assignment.project
    if project.is_deleted:
        raise Http404
    # Every viewer of an OPEX site may read its released drawings (DR-4); a user who
    # cannot see the site, or a non-OPEX site, gets the site pages' own 404.
    if not user_can_view_site_drawings(request.user, project):
        return refuse_site_view_404(request, project)

    state, assignment = _design_state(project)
    if state != STATE_RELEASED:
        raise Http404
    drawings, _hidden, _unavailable = _released_drawings(assignment)
    if pdf.pk not in {row.pk for row in drawings}:
        raise Http404

    try:
        url = get_design_file_url(pdf.bucket, pdf.path, expires_in=DRAWING_LINK_SECONDS)
    except DesignStorageError:
        logger.exception('site_drawing_open: could not sign DesignFilePdf %s.', pdf.pk)
        url = None
    if not url:
        messages.error(request, 'This drawing cannot be opened right now. Try again, or '
                                'contact Design if it keeps failing.')
        return redirect(reverse('project_overview', args=[project.project_id])
                        + '#site-drawings-card')
    log_activity(project, request.user.profile, f'Opened drawing {pdf.display_name}',
                 entity_type='DesignFilePdf', entity_id=pdf.pk,
                 action_code='site_drawing_opened')
    return redirect(url)
