"""
COD record — closeout step 4 (docs/CLOSEOUT_SPEC.md CL-2, CL-3).

A PM or coordinator records an OPEX site's COD: the date, one piece of evidence (a
DISCOM letter, the net-meter installation, or HRPPL's commissioning report) as a PDF,
and a mandatory note. The active record drives the site's COD mirror to Done;
withdrawing it (with a reason) drives the mirror back to Not Started. Records are
append-only, one active per site.

CL-2: recording is refused while open_punch_points_for_project(project) is non-empty,
and the refusal names each open point and its task. Withdrawing is never refused for
punch points (go-ahead Q13): it only ever moves COD backwards.

A separate module from views.py for the same reason qaqc_views.py is one: a
self-contained set of screens with their own URLs. urls.py imports it beside `views`.

THE ONLY WRITER of CodRecord: record_cod() and withdraw_cod() below. The model's save()
refuses an update and delete() always refuses, so withdrawing is this module's single
filter().update(). Neither writer touches the COD Task: each calls
design_views.sync_cod_mirror(), the named composition in front of the single mirror
writer.

THE PDF IS PRIVATE (D-A40). It goes to the private bills bucket (SUPABASE_BILLS_BUCKET,
go-ahead Q6) under a `cod` path segment, and is read only through cod_record_pdf, which
checks the reader can see the site and then redirects to a link signed at that moment.
No URL is stored and the overview page signs nothing.
"""

import logging

from django.contrib import messages
from django.db import IntegrityError, transaction
from django.http import Http404, HttpResponseForbidden
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone

from .bill_storage import (
    BILL_PDF_MAX_BYTES, BILL_PDF_MIME_TYPES, bill_pdf_url, bills_bucket,
)
from .decorators import login_required
from .design_storage import DesignStorageError, _client, build_design_path
from .design_views import cod_mirror_task, sync_cod_mirror
from .forms import check_typed_date
from .models import CodRecord, Project, Task, UserProfile, log_activity
from .notifications import send_notification
from .permissions import project_managers, user_can_record_cod, user_can_view_project
from .punch_points import open_punch_points_for_project
from .views import _active_project

logger = logging.getLogger(__name__)

IN_APP_AND_EMAIL = ['in_app', 'email']

# NotificationLog.template_name labels. Not Interakt templates: WhatsApp is not among
# the channels, so these only name the event on the log row.
T_RECORDED = 'cod_recorded'
T_WITHDRAWN = 'cod_withdrawn'

# The storage path segment: {project_id}/cod/{uuid}.pdf in the bills bucket. Also the
# guard that keeps discard_unrecorded_cod_pdf() from ever removing a bill's PDF.
COD_PATH_KIND = 'cod'

# The template code of the site's Testing & Commissioning task (OPEX v1), read for the
# record form's one warning (go-ahead Q4).
TESTING_COMMISSIONING_CODE = 'TESTING_COMMISSIONING'

COD_NOT_ACTIVATED = 'COD can be recorded once the site is activated.'

# The Project.status values a COD may be recorded in (closeout 4b, Q3). Recording COD
# does not change the status: COD is read from the record (tender_stages.has_active_cod).
COD_RECORDABLE_STATUSES = ('Active', 'In Progress')
COD_OPEX_ONLY = 'COD is recorded on RESCO sites only.'
COD_ALREADY_RECORDED = ('This site already has a COD on record. Withdraw it first to '
                        'record a different one.')
COD_STORAGE_OFF = ('COD evidence cannot be stored right now: the private store for '
                   'COD PDFs is not configured. Nothing was saved.')
# What the record page says BEFORE anything is submitted while the bucket is unset.
# COD_STORAGE_OFF ends "Nothing was saved", which reads wrongly on an unsubmitted page.
COD_STORAGE_NOT_READY = ('COD cannot be recorded yet: the private store for COD '
                         'evidence PDFs is not configured.')
TC_NOT_DONE_WARNING = 'Testing & Commissioning is not marked Done on this site.'


class CodRefused(Exception):
    """A rule refused recording or withdrawing. `str(exc)` is the user-facing message;
    `open_points` lists the punch points behind a CL-2 refusal, for the page to name."""

    def __init__(self, message, open_points=None):
        super().__init__(message)
        self.open_points = open_points or []


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def _person(profile):
    return profile.user.get_full_name() or profile.user.username


def _site_label(project):
    """The site as a person names it: its code, and the customer name when there is one."""
    if project.customer_name and project.customer_name != project.project_id:
        return f"{project.project_id} ({project.customer_name})"
    return project.project_id


def _task_label(task):
    """A task as a refusal names it: "Module Installation — Block A" for a per-location copy."""
    return f'{task.task_name} — {task.location_label}' if task.location_label else task.task_name


def active_cod_record(project):
    """The site's active CodRecord with its recorder joined, or None."""
    return (CodRecord.objects
            .filter(project=project, withdrawn_at__isnull=True)
            .select_related('recorded_by__user')
            .first())


def open_point_rows(project):
    """CL-2's open points as the refusal shows them: the task and the defect, oldest
    first. Reads open_punch_points_for_project() and nothing else — the one definition."""
    return [
        {'task': _task_label(point.task), 'reason': point.reason,
         'raised_at': point.created_at}
        for point in open_punch_points_for_project(project)
    ]


def open_points_message(rows):
    count = len(rows)
    return (f"COD cannot be recorded while {count} punch point"
            f"{'s are' if count != 1 else ' is'} open on this site. Each must be closed "
            f"(by re-approving its task) or waived first.")


def tc_warning(project):
    """The record form's one warning (go-ahead Q4), or None. Never a refusal: the PM may
    record COD with Testing & Commissioning still open. Silent when the site has no such
    task — a site without one has nothing to warn about."""
    task = (Task.objects
            .filter(phase__project=project, template_task__code=TESTING_COMMISSIONING_CODE)
            .only('status').first())
    if task is not None and task.status != Task.DONE:
        return TC_NOT_DONE_WARNING
    return None


def record_refusal(project):
    """Raise CodRefused if COD cannot be recorded on `project` as it stands now: not an
    OPEX site, not activated (no COD mirror, go-ahead Q1), not in a working status (4b),
    a COD already on record, or open punch points (CL-2). Asked by the screen before
    anything is uploaded, and again by record_cod() under its lock."""
    if project.project_type != 'OPEX' or project.is_deleted:
        raise CodRefused(COD_OPEX_ONLY)
    if cod_mirror_task(project) is None:
        raise CodRefused(COD_NOT_ACTIVATED)
    # Only a site being worked on can reach COD (closeout 4b, Q3). On Hold, Cancelled
    # and Commissioned are refused by name. None of them has a writer today (B-3), so
    # this is inert now; it stops a COD being recorded on a held or cancelled site once
    # one exists. Draft never gets here: it has no COD mirror, refused just above.
    if project.status not in COD_RECORDABLE_STATUSES:
        article = 'an' if project.status[:1] in 'AEIOU' else 'a'   # "an On Hold site"
        raise CodRefused(f'COD cannot be recorded on {article} {project.status} site.')
    if CodRecord.objects.filter(project=project, withdrawn_at__isnull=True).exists():
        raise CodRefused(COD_ALREADY_RECORDED)
    rows = open_point_rows(project)
    if rows:
        raise CodRefused(open_points_message(rows), open_points=rows)


def cod_card_context(project, user):
    """Context for the COD card on project_overview. OPEX only: on any other site the
    card is not drawn (`show_cod_card` False) and no query is spent.

    Three queries: the history (with every person on it joined), the COD mirror's
    existence, and the manage check. The PDF is never signed here — the card links to
    cod_record_pdf, which signs at the click.
    """
    if project.project_type != 'OPEX':
        return {'show_cod_card': False}
    history = list(
        CodRecord.objects.filter(project=project)
        .select_related('recorded_by__user', 'withdrawn_by__user')
        .order_by('-recorded_at', '-pk'))
    active = next((r for r in history if r.withdrawn_at is None), None)
    can_record_cod = user_can_record_cod(user, project)
    return {
        'show_cod_card':   True,
        'cod_active':      active,
        'cod_history':     history,
        'cod_activated':   cod_mirror_task(project) is not None,
        'can_record_cod':  can_record_cod,
    }


# ---------------------------------------------------------------------------
# The evidence PDF — private bucket (D-A40), bill_storage's pattern and constants
# ---------------------------------------------------------------------------

def validate_cod_pdf(file_obj):
    """Refuse anything but a real PDF — validate_bill_pdf()'s rules, COD's wording
    (go-ahead Q8): a .pdf name, not empty, at most the bill ceiling (20 MB), a PDF or
    generic MIME type, and bytes that start with %PDF-. Raises CodRefused; touches no
    storage, so the screen refuses before anything is uploaded."""
    name = getattr(file_obj, 'name', '') or ''
    if not name.lower().endswith('.pdf'):
        raise CodRefused('The COD evidence must be a PDF.')
    size = getattr(file_obj, 'size', 0) or 0
    if size == 0:
        raise CodRefused('The COD evidence PDF is empty.')
    if size > BILL_PDF_MAX_BYTES:
        raise CodRefused(
            f'The COD evidence PDF is {size / 1024 / 1024:.1f} MB — the limit is '
            f'{BILL_PDF_MAX_BYTES // 1024 // 1024} MB.')
    mime = (getattr(file_obj, 'content_type', '') or '').split(';')[0].strip().lower()
    if mime and mime not in BILL_PDF_MIME_TYPES:
        raise CodRefused('The COD evidence must be a PDF.')
    # The header, because a name and a MIME type are both whatever the client says.
    try:
        file_obj.seek(0)
        head = file_obj.read(5)
        file_obj.seek(0)
    except Exception:
        raise CodRefused('The COD evidence PDF could not be read.')
    if head != b'%PDF-':
        raise CodRefused('That file is not a PDF, although its name ends in .pdf.')


def upload_cod_pdf(file_obj, project):
    """Store one COD evidence PDF in the private bills bucket under the `cod` segment.

    Returns {file_name, bucket, path, file_size_kb}; never a URL. Raises CodRefused when
    storage is off (fails closed, go-ahead Q6), the file is refused, or the upload
    fails, so the caller writes no row pointing at an object that does not exist.
    """
    bucket = bills_bucket()
    if not bucket:
        raise CodRefused(COD_STORAGE_OFF)
    validate_cod_pdf(file_obj)
    path = build_design_path(project.project_id, COD_PATH_KIND, file_obj.name)
    try:
        file_obj.seek(0)
        payload = file_obj.read()
    except Exception:
        raise CodRefused('The COD evidence PDF could not be read.')
    try:
        _client().storage.from_(bucket).upload(
            path=path, file=payload, file_options={'content-type': 'application/pdf'})
    except DesignStorageError as exc:
        raise CodRefused(str(exc))
    except Exception as exc:
        logger.error('COD PDF upload failed — %s', exc)
        raise CodRefused(f'Upload to storage failed: {type(exc).__name__}. '
                         f'Nothing was saved.')
    return {'file_name': file_obj.name, 'bucket': bucket, 'path': path,
            'file_size_kb': max(1, file_obj.size // 1024)}


def discard_unrecorded_cod_pdf(stored):
    """Remove a PDF upload_cod_pdf() stored for a COD the writer then refused.

    Refuses to remove anything a CodRecord records (a recorded COD PDF is never
    deleted), anything outside the bills bucket, and anything not under a `cod` path
    segment — so a bill's PDF in the same bucket can never be reached from here. Never
    raises: a failed cleanup leaves an orphan object, which is logged, never a failed
    page. Returns True when the object was removed.
    """
    bucket, path = (stored or {}).get('bucket'), (stored or {}).get('path')
    if not bucket or not path or bucket != bills_bucket():
        return False
    if f'/{COD_PATH_KIND}/' not in path:
        return False
    if CodRecord.objects.filter(pdf_bucket=bucket, pdf_path=path).exists():
        return False
    try:
        _client().storage.from_(bucket).remove([path])
        return True
    except Exception as exc:
        logger.error('COD PDF cleanup failed for %s/%s — %s', bucket, path, exc)
        return False


# ---------------------------------------------------------------------------
# Notices — PM, coordinators and the CEO, minus whoever acted (go-ahead Q5)
# ---------------------------------------------------------------------------

def _recipient_pks(project, actor_pk):
    """Who hears about a COD: the site's PM and active coordinators, and every active
    CEO, without duplicates and without the person who acted."""
    pks = [p.pk for p in project_managers(project)]
    pks += list(UserProfile.objects
                .filter(role='CEO', is_active=True, user__is_active=True)
                .values_list('pk', flat=True))
    seen, ordered = set(), []
    for pk in pks:
        if pk != actor_pk and pk not in seen:
            seen.add(pk)
            ordered.append(pk)
    return ordered


def _send(recipient_pks, message, project_pk, actor_pk, template):
    """One in-app + email notice per recipient, after commit. Re-reads by pk so the
    callback holds no ORM objects from the writer's transaction. A person deactivated
    on either model is not told: they cannot sign in to see it."""
    project = Project.objects.filter(pk=project_pk).first()
    actor = UserProfile.objects.filter(pk=actor_pk).first()
    link = reverse('project_overview', args=[project.project_id]) if project else ''
    recipients = (UserProfile.objects.select_related('user')
                  .filter(pk__in=recipient_pks, is_active=True, user__is_active=True))
    for recipient in recipients:
        send_notification(
            recipient, message, channels=IN_APP_AND_EMAIL, link=link, subject=message,
            template=template, related_project=project, actor=actor,
        )


def _after_commit(project, actor, action, record_pk, action_code, message, template):
    """Register the ActivityLog line and the notices to run once the writer commits.
    A refused or rolled-back write therefore logs nothing and tells nobody."""
    project_pk, actor_pk = project.pk, actor.pk
    recipient_pks = _recipient_pks(project, actor_pk)

    def log():
        # Re-read: the callback must hold no ORM object from the writer's transaction.
        site = Project.objects.filter(pk=project_pk).first()
        who = UserProfile.objects.filter(pk=actor_pk).first()
        log_activity(site, who, action, entity_type='CodRecord', entity_id=record_pk,
                     action_code=action_code)

    # Lambdas, not functools.partial: on_commit(robust=True) names the callback in its
    # log line and the approval notices settled on this form (approval_notices.py).
    transaction.on_commit(log, robust=True)
    transaction.on_commit(
        lambda: _send(recipient_pks, message, project_pk, actor_pk, template),
        robust=True)


# ---------------------------------------------------------------------------
# Writers — the only code that creates or withdraws a CodRecord
# ---------------------------------------------------------------------------

def record_cod(project, user, *, cod_date, evidence_type, note, stored_pdf):
    """Record `project`'s COD and move its COD mirror to Done.

    Atomic: the project row is locked; the permission, the site rules and CL-2 are
    re-checked under the lock; the record is inserted; sync_cod_mirror() moves the
    mirror with `user` as the transition actor; the ActivityLog line and the notices
    run after commit. Returns the new row; raises CodRefused otherwise — the caller then
    discards the uploaded PDF, which no row records.
    """
    actor = user.profile
    note = (note or '').strip()
    if not note:
        raise CodRefused('Write a note — it is mandatory for a COD.')
    if cod_date > timezone.localdate():
        raise CodRefused('The COD date cannot be in the future.')
    if evidence_type not in dict(CodRecord.EVIDENCE_TYPE_CHOICES):
        raise CodRefused('Choose the evidence for this COD.')

    with transaction.atomic():
        # Serialises two managers recording at once, and a punch point being raised
        # while the COD is recorded: the second writer waits here and re-checks.
        locked = Project.objects.select_for_update().get(pk=project.pk)
        # Only the site's PM and coordinators record COD (user_can_record_cod); asked
        # again here because a coordinator removed since the page loaded must not.
        if not user_can_record_cod(user, locked):
            raise CodRefused('Only the site\'s PM or a coordinator can record COD.')
        record_refusal(locked)
        try:
            # A savepoint, so the backstop constraint's refusal leaves the outer
            # transaction usable for the clean rollback below.
            with transaction.atomic():
                row = CodRecord.objects.create(
                    project=locked, cod_date=cod_date, evidence_type=evidence_type,
                    pdf_file_name=stored_pdf['file_name'], pdf_bucket=stored_pdf['bucket'],
                    pdf_path=stored_pdf['path'], pdf_size_kb=stored_pdf['file_size_kb'],
                    note=note, recorded_by=actor,
                )
        except IntegrityError:
            # uniq_cod_record_active: only reachable if the lock above was bypassed.
            raise CodRefused(COD_ALREADY_RECORDED)
        sync_cod_mirror(locked, actor)
        message = (f"COD recorded for {_site_label(locked)}: "
                   f"{cod_date:%d %b %Y}, evidence {row.get_evidence_type_display()}. "
                   f"Recorded by {_person(actor)}.")
        _after_commit(
            locked, actor,
            f"Recorded COD {cod_date:%d %b %Y} ({row.get_evidence_type_display()}): {note}",
            row.pk, 'cod_recorded', message, T_RECORDED)
    return row


def withdraw_cod(project, user, record_pk, reason):
    """Withdraw `project`'s active COD record `record_pk` with a mandatory reason, and
    move the COD mirror back to Not Started. Never refused for open punch points
    (go-ahead Q13). Returns the withdrawn row (re-read); raises CodRefused otherwise."""
    actor = user.profile
    reason = (reason or '').strip()
    if not reason:
        raise CodRefused('Give a reason for withdrawing the COD.')

    with transaction.atomic():
        locked = Project.objects.select_for_update().get(pk=project.pk)
        # Same authority as recording (user_can_record_cod): the PM and coordinators.
        if not user_can_record_cod(user, locked):
            raise CodRefused('Only the site\'s PM or a coordinator can withdraw COD.')
        # Race: the caller holds the project row lock, so no second writer can be
        # withdrawing this row; the withdrawn_at__isnull=True term still makes the
        # update a no-op rather than a double-stamp if that lock is ever bypassed, and
        # the 0 is reported. project=locked keeps a pk from another site out.
        updated = (CodRecord.objects
                   .filter(pk=record_pk, project=locked, withdrawn_at__isnull=True)
                   .update(withdrawn_at=timezone.now(), withdrawn_by=actor,
                           withdraw_reason=reason))
        if updated != 1:
            raise CodRefused('This COD is not on record any more — it was already '
                             'withdrawn. Reload the page.')
        row = CodRecord.objects.get(pk=record_pk)
        sync_cod_mirror(locked, actor)
        message = (f"COD withdrawn for {_site_label(locked)} by {_person(actor)}: "
                   f"{reason}")
        _after_commit(
            locked, actor,
            f"Withdrew COD {row.cod_date:%d %b %Y}: {reason}",
            row.pk, 'cod_withdrawn', message, T_WITHDRAWN)
    return row


# ---------------------------------------------------------------------------
# Screens
# ---------------------------------------------------------------------------

def _cod_site(request, project_id):
    """Resolve the site for a COD screen, or raise Http404. Every COD URL answers 404 to
    a person who cannot see the site (the 0.2 shape) and on any non-OPEX site — COD is
    recorded on RESCO sites only, so for Residential and CAPEX the URLs do not exist."""
    project = _active_project(project_id)
    if not user_can_view_project(request.user, project):
        raise Http404
    if project.project_type != 'OPEX':
        raise Http404
    return project


def _clean_record_post(request):
    """The record form's POST. Returns (cleaned, errors). Settles everything that must
    be settled before the PDF reaches storage; touches no storage."""
    post, errors = request.POST, []
    cod_date = None
    raw_date = post.get('cod_date', '')
    if not raw_date.strip():
        errors.append('Enter the COD date.')
    else:
        cod_date, message = check_typed_date(raw_date)
        if message:
            errors.append(message)
        elif cod_date > timezone.localdate():
            errors.append('The COD date cannot be in the future.')
            cod_date = None
    evidence_type = post.get('evidence_type', '')
    if evidence_type not in dict(CodRecord.EVIDENCE_TYPE_CHOICES):
        errors.append('Choose the evidence for this COD.')
    note = post.get('note', '').strip()
    if not note:
        errors.append('Write a note — it is mandatory for a COD.')
    pdf = request.FILES.get('cod_pdf')
    if pdf is None:
        errors.append('Attach the evidence as a PDF.')
    else:
        try:
            validate_cod_pdf(pdf)
        except CodRefused as exc:
            errors.append(str(exc))
    cleaned = {'cod_date': cod_date, 'evidence_type': evidence_type, 'note': note,
               'pdf': pdf}
    return cleaned, errors


@login_required
def cod_record(request, project_id):
    """
    Record an OPEX site's COD: date, evidence type, evidence PDF and a mandatory note.
    GET shows the form — or, while the site cannot take a COD, why not (not activated,
    already recorded, or the open punch points by task). POST records it.

    Order on POST: validate the form → pre-check the site rules and CL-2 → upload the
    PDF → record_cod() (lock, re-check, write, mirror, after-commit log and notices) →
    if the writer refuses after the upload, delete the orphan PDF.

    Access: the site's PM and its coordinators (user_can_record_cod). 404 for anyone
    who cannot see the site and on Residential / CAPEX sites; 403 for a viewer who
    does not manage it.
    """
    project = _cod_site(request, project_id)
    # Only the site's managers record COD — it is the PM's declaration to the client.
    # Everyone else who can see the site reads the card and opens the PDF, no more.
    if not user_can_record_cod(request.user, project):
        return HttpResponseForbidden()

    storage_ready = bool(bills_bucket())

    def page(errors=(), refusal=None, values=None, status=200):
        if refusal is None:
            try:
                record_refusal(project)
            except CodRefused as exc:
                refusal = exc
        return render(request, 'projects/cod_record.html', {
            'project':          project,
            'evidence_choices': CodRecord.EVIDENCE_TYPE_CHOICES,
            'errors':           list(errors),
            'refusal':          str(refusal) if refusal else '',
            'open_points':      refusal.open_points if refusal else [],
            'warning':          tc_warning(project),
            'storage_ready':    storage_ready,
            'today':            timezone.localdate(),
            'values':           values or {},
        }, status=status)

    if request.method != 'POST':
        return page()

    cleaned, errors = _clean_record_post(request)
    values = {'cod_date': request.POST.get('cod_date', ''),
              'evidence_type': cleaned['evidence_type'], 'note': cleaned['note']}
    try:
        record_refusal(project)
    except CodRefused as exc:
        # Nothing is uploaded for a site that cannot take a COD.
        return page(refusal=exc, values=values)
    if errors:
        return page(errors, values=values)

    try:
        stored = upload_cod_pdf(cleaned['pdf'], project)
    except CodRefused as exc:
        return page([str(exc)], values=values)
    try:
        record_cod(project, request.user, cod_date=cleaned['cod_date'],
                   evidence_type=cleaned['evidence_type'], note=cleaned['note'],
                   stored_pdf=stored)
    except CodRefused as exc:
        discard_unrecorded_cod_pdf(stored)
        # Shown as the page's refusal, never also as a form error: the writer's
        # refusals are the site rules re-asked under its lock (or the permission).
        return page(refusal=exc, values=values)

    messages.success(request, f"COD recorded for {project.project_id}.")
    return redirect(reverse('project_overview', args=[project.project_id]) + '#cod-card')


@login_required
def cod_withdraw(request, project_id, record_pk):
    """
    Withdraw a site's active COD record with a mandatory reason; the COD task returns to
    Not Started and the record stays in the history. GET shows the record and the reason
    box; POST withdraws. Never refused for open punch points.

    Access: the site's PM and its coordinators (user_can_record_cod). 404 for anyone
    who cannot see the site, on Residential / CAPEX sites, and for a record that is not
    this site's; 403 for a viewer who does not manage it.
    """
    project = _cod_site(request, project_id)
    # The same authority as recording: withdrawing a COD unsays the PM's declaration.
    if not user_can_record_cod(request.user, project):
        return HttpResponseForbidden()
    record = (CodRecord.objects.select_related('recorded_by__user')
              .filter(pk=record_pk, project=project).first())
    if record is None:
        raise Http404

    errors = []
    if request.method == 'POST':
        try:
            withdraw_cod(project, request.user, record.pk, request.POST.get('reason', ''))
        except CodRefused as exc:
            errors.append(str(exc))
        else:
            messages.success(request, f"COD withdrawn for {project.project_id}.")
            return redirect(reverse('project_overview', args=[project.project_id])
                            + '#cod-card')

    return render(request, 'projects/cod_withdraw.html', {
        'project': project,
        'record':  record,
        'errors':  errors,
        'reason':  request.POST.get('reason', '') if request.method == 'POST' else '',
    })


@login_required
def cod_record_pdf(request, project_id, record_pk):
    """
    Open a COD record's evidence PDF: a redirect to a link signed now, valid for
    bill_storage.BILL_LINK_SECONDS. Withdrawn records' PDFs open too — they are history.

    Access: every user who can see the site (user_can_view_project, go-ahead Q10). 404
    for anyone else, on Residential / CAPEX sites, and for a record that is not this
    site's. When no link can be signed (storage off, or signing failed) the viewer is
    sent back to the site with a message, never to a stored or public URL.
    """
    # Every viewer of the site may read the evidence (go-ahead Q10); _cod_site 404s anyone
    # who cannot see the site, so a forwarded link opens nothing for them.
    project = _cod_site(request, project_id)
    record = CodRecord.objects.filter(pk=record_pk, project=project).first()
    if record is None:
        raise Http404
    url = bill_pdf_url(record.pdf_bucket, record.pdf_path)
    if url is None:
        messages.error(request, 'The COD evidence PDF cannot be opened right now.')
        return redirect(reverse('project_overview', args=[project.project_id]) + '#cod-card')
    return redirect(url)
