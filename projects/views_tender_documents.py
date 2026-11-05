"""
TD-B2 — tender documents: the manage page, upload (with optional supersede) and void.

Every view is site-scoped (B2-a): the URL names a site, the documents belong to that
site's Program, and the page says they apply to all of the tender's live sites.

THE GATE, IN THIS ORDER, ON EVERY REQUEST (the challan withdraw view's order)
  a) user_can_view_project            -> refuse_site_view_404 (handover redirect or 404)
  b) tender_docs_enabled_for_site     -> 404 (Residential, CAPEX, Cancelled; R2, R8)
  c) user_can_manage_tender_documents -> 403 (R1: Admin or a manager of a site in the
                                         tender)
Every POST runs the gate again itself; nothing trusts the page that drew the form.

WHAT THIS MODULE NEVER DOES
  * write ActivityLog (R5): the rows are the audit trail — who uploaded, who replaced,
    who voided and why;
  * notify anyone (B2-c): send_notification / notify are not imported;
  * delete a stored file: a voided or replaced document's object stays in the bucket.
    The only removal is discard_tender_document_file() for an object whose row failed
    to save, which refuses any path a row records;
  * link to a file (B2-d): B3 adds the open view.
"""
import logging
from datetime import timedelta

from django.contrib import messages
from django.db import transaction
from django.http import Http404, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from .decorators import login_required
from .delivery_storage import DeliveryStorageError
from .forms_tender_documents import TenderDocumentUploadForm, active_tender_documents
from .models import (
    TENDER_DOC_SITE_TEAM, TENDER_DOC_VOID_REASON_MIN, TenderDocument,
)
from .permissions import (
    tender_docs_enabled_for_site, user_can_manage_tender_documents,
    user_can_view_project, user_can_view_tender_document_history,
)
from .site_pm import refuse_site_view_404
from .tender_documents import discard_tender_document_file, store_tender_document_file
from .views import _active_project

logger = logging.getLogger(__name__)

#: The duplicate-submit window: the same user, file name and size in the same Program
#: within this many seconds is taken as a second click on one upload.
DUPLICATE_WINDOW_SECONDS = 60

TEMPLATE = 'projects/tender_documents_manage.html'


class _UploadRefused(Exception):
    """Raised inside the upload's transaction to roll it back with a message for the
    uploader. Its str() is that message."""


def _tender_site(request, project_id):
    """Resolve `project_id` and run the three-step gate. Returns (project, None) when the
    user may manage this site's tender documents, else (None, response) — or raises
    Http404, which is the same refusal."""
    project = _active_project(project_id, select_related=['program'])
    # a) Someone who cannot see the site gets the site pages' 404 (or an outgoing PM's
    #    handover redirect), so nothing says the page exists.
    if not user_can_view_project(request.user, project):
        return None, refuse_site_view_404(request, project)
    # b) Tender documents exist only on a live OPEX site under a Program (R2, R8):
    #    for a Residential, CAPEX or Cancelled site the page does not exist.
    if not tender_docs_enabled_for_site(project):
        raise Http404
    # c) Admin, or anyone managing a site in this tender, manages its documents (R1).
    #    A Site Engineer on the site, SCM, Finance, CEO and System Admin can see the site
    #    but not change what the whole tender reads, so they get a bare 403.
    if not user_can_manage_tender_documents(request.user, project.program):
        return None, HttpResponseForbidden()
    return project, None


def _live_site_count(program):
    """How many of `program`'s sites the documents apply to (B2-a banner): those where
    tender_docs_enabled_for_site() is true — not soft-deleted, not Cancelled.

    Asked of the permission helper per site rather than filtered on status here, so the
    banner's count and the card's appearance can never disagree, and no status string
    is compared outside permissions.py. A tender has at most a few hundred sites, and
    only the four columns the helper reads are fetched."""
    sites = (program.sites.filter(is_deleted=False)
             .only('project_type', 'program_id', 'is_deleted', 'status'))
    return sum(1 for site in sites if tender_docs_enabled_for_site(site))


def _manage_url(project):
    return reverse('tender_documents_manage', args=[project.project_id])


def _render_manage(request, project, form):
    """The manage page for `project`, with `form` as the upload form (bound after a
    refused upload, so its errors show). The caller has already run _tender_site()."""
    program = project.program
    # Current documents with their uploader's name in the same query; the template
    # prints uploaded_by.user for every row.
    documents = (active_tender_documents(program)
                 .select_related('uploaded_by__user'))
    show_history = user_can_view_tender_document_history(request.user, project)
    history = None
    if show_history:
        # Every row that is no longer current: replaced (with what it was replaced by)
        # or voided (by whom). Newest first, as the model orders them.
        history = (TenderDocument.objects
                   .filter(program=program)
                   .exclude(voided_at__isnull=True, superseded_by__isnull=True)
                   .select_related('uploaded_by__user', 'voided_by__user',
                                   'superseded_by'))
    # The tick's initial display, decided here so the template's Alpine state is a
    # literal true/false and never a value echoed from the request.
    site_team_selected = (form.is_bound
                          and form.data.get('visibility') == TENDER_DOC_SITE_TEAM)
    return render(request, TEMPLATE, {
        'project': project,
        'program': program,
        'site_count': _live_site_count(program),
        'documents': documents,
        'form': form,
        'site_team_value': TENDER_DOC_SITE_TEAM,
        'site_team_selected': site_team_selected,
        'show_history': show_history,
        'history': history,
        'void_reason_min': TENDER_DOC_VOID_REASON_MIN,
    })


@login_required
@require_GET
def tender_documents_manage(request, project_id):
    """Show the tender's current documents, the upload form, a void form per document
    and, for management, the replaced and voided history (B2-a, R7).

    Access: Admin, or anyone managing a site in this tender (R1), on a site where tender
    documents apply (R2, R8). 404 for anyone who cannot see the site and on Residential,
    CAPEX and Cancelled sites; 403 for a viewer who does not manage the tender.
    """
    project, refusal = _tender_site(request, project_id)
    if refusal is not None:
        return refusal
    form = TenderDocumentUploadForm(program=project.program)
    return _render_manage(request, project, form)


@login_required
@require_POST
def tender_documents_upload(request, project_id):
    """Upload one tender document (B2-b), optionally replacing a current one (R7).

    Order: gate -> form -> duplicate guard -> store the file -> one transaction that
    locks and re-checks the replaced row, writes the new row and marks the old one
    superseded. If the transaction fails the stored file is discarded and nothing is
    saved. No ActivityLog row (R5), no notification (B2-c).

    Access: as tender_documents_manage, re-checked here. POST only.
    """
    # The gate runs again here: the page that drew the form proves nothing about the
    # user who posts it now (a handover may have happened in between).
    project, refusal = _tender_site(request, project_id)
    if refusal is not None:
        return refusal
    program = project.program
    profile = request.user.profile
    form = TenderDocumentUploadForm(request.POST, request.FILES, program=program)
    if not form.is_valid():
        return _render_manage(request, project, form)
    data = form.cleaned_data
    uploaded = data['file']

    # A second click on a slow upload arrives as a second POST with the same file. If
    # this user stored a file of this name and size in this tender a moment ago, the
    # first click already did the work: say so and store nothing.
    # TODO: check-then-write — two near-simultaneous POSTs can both pass this check and
    # both be saved. Closing that needs a form key with a unique index (the DC-K
    # client_uuid pattern), which is a migration; deferred, see SECONDARY_FINDINGS.md
    # (TD-B2). The page also disables the button on submit.
    recent = timezone.now() - timedelta(seconds=DUPLICATE_WINDOW_SECONDS)
    if TenderDocument.objects.filter(
            program=program, uploaded_by=profile, original_filename=uploaded.name,
            size_bytes=uploaded.size, uploaded_at__gte=recent).exists():
        messages.error(request, f'{uploaded.name} was uploaded by you less than a minute '
                                f'ago. Nothing was saved a second time.')
        return redirect(_manage_url(project))

    # Stored before the row is written, so a storage failure refuses the upload with no
    # row at all (the challan create's order).
    try:
        bucket, path, content_type, size_bytes = store_tender_document_file(
            program.pk, uploaded)
    except DeliveryStorageError as exc:
        form.add_error('file', str(exc))
        return _render_manage(request, project, form)

    chosen = data['replaces']
    site_team = data['visibility'] == TENDER_DOC_SITE_TEAM
    try:
        with transaction.atomic():
            if chosen is not None:
                # RACE: the form checked the row before the upload; another manager may
                # have voided or replaced it since. The row lock holds it until this
                # transaction ends, and the re-check reads it fresh under that lock, so
                # two uploads replacing one document cannot both succeed (R7: one level).
                locked = (TenderDocument.objects.select_for_update()
                          .filter(pk=chosen.pk).first())
                if (locked is None or locked.program_id != program.pk
                        or locked.voided_at is not None
                        or locked.superseded_by_id is not None):
                    raise _UploadRefused(
                        f'"{chosen.title}" was voided or replaced while you were '
                        f'uploading. Nothing was saved.')
            now = timezone.now()
            document = TenderDocument.objects.create(
                program=program, title=data['title'], category=data['category'],
                visibility=data['visibility'], storage_bucket=bucket,
                storage_path=path, original_filename=uploaded.name,
                content_type=content_type, size_bytes=size_bytes, uploaded_by=profile,
                # R9: the confirmation is stored only for a Site team document, as who
                # and when; for Management only nobody was asked, so nothing is stored.
                no_prices_confirmed_by=profile if site_team else None,
                no_prices_confirmed_at=now if site_team else None,
            )
            if chosen is not None:
                # RACE: already locked above, so this cannot lose to a void or another
                # supersede; the conditional terms are kept so a row that somehow changed
                # is never stamped twice — the update then touches nothing and the whole
                # upload rolls back.
                updated = (TenderDocument.objects
                           .filter(pk=chosen.pk, voided_at__isnull=True,
                                   superseded_by__isnull=True)
                           .update(superseded_by=document, superseded_at=now))
                if updated != 1:
                    raise _UploadRefused(
                        f'"{chosen.title}" was voided or replaced while you were '
                        f'uploading. Nothing was saved.')
    except _UploadRefused as exc:
        discard_tender_document_file(bucket, path)
        form.add_error('replaces', str(exc))
        return _render_manage(request, project, form)
    except Exception:
        # The transaction rolled back, so no row records the object and the discard
        # removes it. Logged with the traceback; the uploader is told nothing was saved.
        logger.exception('Tender document row failed to save for program %s', program.pk)
        discard_tender_document_file(bucket, path)
        form.add_error(None, 'The document could not be saved. Nothing was saved; '
                             'please try again.')
        return _render_manage(request, project, form)

    if chosen is not None:
        messages.success(request, f'"{document.title}" uploaded. It replaces '
                                  f'"{chosen.title}", which moved to the history.')
    else:
        messages.success(request, f'"{document.title}" uploaded.')
    return redirect(_manage_url(project))


@login_required
@require_POST
def tender_documents_void(request, project_id, doc_id):
    """Void one current tender document with a reason of at least
    TENDER_DOC_VOID_REASON_MIN characters (after stripping). The row is stamped (when,
    who, why) and moves to the history; the stored file is NOT deleted. No ActivityLog
    row (R5), no notification (B2-c).

    Access: as tender_documents_manage, re-checked here. POST only. 404 for a document
    of another tender.
    """
    # The gate runs again here, for the same reason as the upload.
    project, refusal = _tender_site(request, project_id)
    if refusal is not None:
        return refusal
    # Only this site's tender: another tender's document id is the same bare 404 as one
    # that does not exist.
    document = get_object_or_404(TenderDocument, pk=doc_id, program_id=project.program_id)
    back = _manage_url(project)

    # Stripped before measuring: the database CHECK counts characters as stored, and
    # "   short   " is not a reason.
    reason = (request.POST.get('void_reason') or '').strip()
    if len(reason) < TENDER_DOC_VOID_REASON_MIN:
        messages.error(request, f'Give a reason of at least {TENDER_DOC_VOID_REASON_MIN} '
                                f'characters to void "{document.title}".')
        return redirect(back)

    # RACE: two voids, or a void and a supersede, of the same document at once. The
    # voided_at IS NULL and superseded_by IS NULL terms make the update conditional in
    # SQL, so exactly one change wins; the loser updates nothing and is told so — never
    # a second stamp over the first. (A supersede holds the row lock, so this waits for
    # it and then finds superseded_by set.)
    updated = (TenderDocument.objects
               .filter(pk=document.pk, voided_at__isnull=True, superseded_by__isnull=True)
               .update(voided_at=timezone.now(), voided_by=request.user.profile,
                       void_reason=reason))
    if updated != 1:
        messages.error(request, f'"{document.title}" was already voided or replaced. '
                                f'Nothing changed.')
        return redirect(back)

    messages.success(request, f'"{document.title}" was voided. It is in the history; '
                              f'its file is kept.')
    return redirect(back)
