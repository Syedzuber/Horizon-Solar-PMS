"""
Contractor attendance screens — ATT-B1: the same-day flow only.

    attendance_today     the site's attendance page: today's entries, + Add entry
    attendance_add       log a crew (Site Engineer on the site, PM, coordinators)
    attendance_edit      edit an entry today, with Void this entry
    attendance_time_out  POST: the crew's time out
    attendance_void      POST: void with a note
    attendance_photo     one entry's group photo, a link signed at the click

Past days, the Summary, corrections after the day, SCM mapping of not-listed names and
notifications are ATT-B2 / ATT-B3 and are not here.

Every write goes through projects/attendance.py. Photos are uploaded before the writer
runs, with qaqc_views.upload_visit_photo (the private bills bucket, the QA/QC checks);
a refused write discards the photo it just uploaded and nothing else.

ACCESS. Every URL resolves the site with views._active_project (soft-deleted → 404).
Who may open a page, in this order:
    user_can_view_project()               the whole page (ruling: the existing view helper)
    user_is_todays_attendance_recorder()  their own live entries of today, nothing else (ruling F)
    anyone else                           the 0.2 refusal: refuse_site_view_404
A viewer without the authority to act gets a 403 on add, and a redirect with a message
when an entry can no longer be changed (another day, voided).
"""
from datetime import datetime

from django.contrib import messages
from django.db.models import Count, Prefetch, Q, Sum
from django.http import Http404, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from .attendance import (
    ATTENDANCE_PHOTO_KIND, ATTENDANCE_STORAGE_OFF, DUPLICATE_ENTRY, NOT_CHANGEABLE,
    AttendanceRefused, create_attendance, discard_unrecorded_attendance_photo,
    live_duplicate_exists, normalise_unlisted_name, set_time_out, update_attendance,
    void_attendance,
)
from .bill_storage import bill_pdf_url, bills_bucket
from .decorators import get_user_dashboard, login_required
from .forms_attendance import UNLISTED, AttendanceForm
from .models import ContractorAttendance, ContractorAttendanceRevision
from .permissions import (
    user_can_change_attendance_today, user_can_log_attendance, user_can_view_attendance_photo,
    user_can_view_project, user_is_todays_attendance_recorder,
)
from .qaqc_views import QaqcVisitRefused, upload_visit_photo
from .site_pm import refuse_site_view_404
from .views import _active_project

#: What a revision's "Edited" line names when a field changed (snapshot key → label).
CHANGE_LABELS = [
    ('contractor_name', 'Contractor'),
    ('unlisted_contractor_name', 'Contractor (not listed)'),
    ('trade', 'Trade'),
    ('trade_remark', 'Trade remark'),
    ('headcount', 'Headcount'),
    ('time_in', 'Time in'),
    ('time_out', 'Time out'),
]
TRADE_LABELS = dict(ContractorAttendance.TRADE_CHOICES)


def _person(profile):
    return profile.user.get_full_name() or profile.user.username


def _now_hhmm():
    """The current IST time as HH:MM, the time in / time out prefill."""
    return timezone.localtime().strftime('%H:%M')


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def attendance_card_context(project, user):
    """The overview's "Attendance today" card: live crews and people, open (no time out)
    and not-listed counts, and whether `user` may add an entry. One aggregate query plus
    the permission helper's. Called by views.project_overview, which has already refused
    anyone who cannot see the site."""
    # Live entries of today only: a voided entry stops counting (ruling 11).
    totals = (ContractorAttendance.objects
              .filter(project=project, date=timezone.localdate(), is_voided=False)
              .aggregate(crews=Count('pk'), people=Sum('headcount'),
                         open=Count('pk', filter=Q(time_out__isnull=True)),
                         not_listed=Count('pk', filter=Q(contractor__isnull=True))))
    return {
        'crews': totals['crews'] or 0,
        'people': totals['people'] or 0,
        'open': totals['open'] or 0,
        'not_listed': totals['not_listed'] or 0,
        'can_add': user_can_log_attendance(user, project),
    }


def _page_scope(request, project):
    """'all' when the user may see the site, 'own' when they are only today's recorder of
    an entry on it (ruling F), else None."""
    if user_can_view_project(request.user, project):
        return 'all'
    if user_is_todays_attendance_recorder(request.user, project):
        return 'own'
    return None


def _can_open_entry(user, project, entry):
    """Whether `user` may reach `entry` at all: any viewer of the site, or its own
    recorder while it is still theirs to change today (ruling F) — a recorder who no
    longer sees the site never reaches someone else's entry, or their own from another
    day."""
    if user_can_view_project(user, project):
        return True
    profile = getattr(user, 'profile', None)
    return (profile is not None and entry.recorded_by_id == profile.pk
            and user_can_change_attendance_today(user, entry))


def _entry_or_404(project, entry_pk):
    return get_object_or_404(
        ContractorAttendance.objects.select_related('project', 'contractor',
                                                    'recorded_by__user', 'voided_by__user'),
        pk=entry_pk, project=project)


def _history(entry):
    """The entry's revisions as display rows, oldest first: who, when, what. An edit
    names each field that changed, before → after; a photo change says so without the
    path. Reads the prefetched revisions, so it costs no query."""
    rows = []
    for rev in entry.revisions.all():
        changes = []
        if rev.action == ContractorAttendanceRevision.EDITED and rev.before:
            for key, label in CHANGE_LABELS:
                old, new = rev.before.get(key), rev.after.get(key)
                if old == new:
                    continue
                if key == 'trade':
                    old, new = TRADE_LABELS.get(old, old), TRADE_LABELS.get(new, new)
                changes.append(f"{label}: {old if old not in (None, '') else '—'} → "
                               f"{new if new not in (None, '') else '—'}")
            if rev.before.get('photo_path') != rev.after.get('photo_path'):
                changes.append('Photo replaced')
        elif rev.action == ContractorAttendanceRevision.TIME_OUT:
            changes.append(f"Time out {rev.after.get('time_out')}")
        rows.append({'action': rev.get_action_display(), 'by': _person(rev.changed_by),
                     'at': rev.changed_at, 'changes': changes, 'note': rev.note})
    return rows


@login_required
@require_GET
def attendance_today(request, project_id):
    """
    The site's attendance page for today: the header counts, + Add entry, and today's
    entries (live first, voided struck through at the bottom), each with its photo (if
    allowed), recorder and edit history. Past days are not shown in B1.

    Access: anyone who can see the site (user_can_view_project); a Site Engineer whose
    tasks were moved off the site today sees only the live entries they recorded today
    (ruling F). Everyone else gets the site 404.
    """
    project = _active_project(project_id)
    scope = _page_scope(request, project)
    # Only a viewer of the site, or today's recorder of an entry on it, opens the page;
    # anyone else is refused exactly as the overview refuses them.
    if scope is None:
        return refuse_site_view_404(request, project)
    profile = request.user.profile
    today = timezone.localdate()

    # Today's entries on this site, with what each card shows: contractor, recorder,
    # voider, and every revision with its author (prefetched — _history() reads them
    # without a query per card). A recorder-only visitor sees their own live ones only.
    entries_qs = (ContractorAttendance.objects
                  .filter(project=project, date=today)
                  .select_related('project', 'contractor', 'recorded_by__user',
                                  'voided_by__user')
                  .prefetch_related(Prefetch(
                      'revisions',
                      queryset=ContractorAttendanceRevision.objects
                      .select_related('changed_by__user').order_by('changed_at', 'pk')))
                  .order_by('is_voided', 'time_in', 'pk'))
    if scope == 'own':
        entries_qs = entries_qs.filter(recorded_by=profile, is_voided=False)

    cards = []
    for entry in entries_qs:
        cards.append({
            'entry': entry,
            # Per entry, because the recorder term differs card by card; a day has a
            # handful of crews, so the helper's queries stay small.
            'can_change': user_can_change_attendance_today(request.user, entry),
            'can_see_photo': user_can_view_attendance_photo(request.user, entry),
            'history': _history(entry),
        })
    live = [c['entry'] for c in cards if not c['entry'].is_voided]
    return render(request, 'projects/attendance/attendance_page.html', {
        'project': project,
        'today': today,
        'cards': cards,
        'crews': len(live),
        'people': sum(e.headcount for e in live),
        'open_count': sum(1 for e in live if e.time_out is None),
        'can_add': scope == 'all' and user_can_log_attendance(request.user, project),
        'own_only': scope == 'own',
        'now_hhmm': _now_hhmm(),
    })


@login_required
@require_GET
def attendance_photo(request, project_id, entry_pk):
    """
    One entry's group photo: a redirect to a link signed now (the bills bucket's signer,
    900 s). The page signs nothing; each <img> asks here when its card is opened.

    Access: whoever may reach the entry (_can_open_entry) AND may see its photo
    (user_can_view_attendance_photo — recorder, PM / coordinators, SCM, Admin, CEO).
    404 for anyone else, and when no link can be signed — an image tag cannot follow a
    redirect to a page with a message.
    """
    project = _active_project(project_id)
    entry = _entry_or_404(project, entry_pk)
    # Reaching the entry first, then the narrower photo audience (ruling 8): a viewer
    # outside it sees the entry without the photo, and gets 404 here.
    if not _can_open_entry(request.user, project, entry):
        return refuse_site_view_404(request, project)
    if not user_can_view_attendance_photo(request.user, entry):
        raise Http404
    url = bill_pdf_url(entry.photo_bucket, entry.photo_path)
    if url is None:
        raise Http404
    return redirect(url)


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

def _form_context(project, form, entry, errors):
    """What the add / edit template reads. The picker is rendered from `form.vendors`
    server-side; Alpine filters those rows in the page, so no JSON is needed."""
    posted = form.data if form.is_bound else {}
    if entry is not None and not form.is_bound:
        picked = str(entry.contractor_id) if entry.contractor_id else UNLISTED
    else:
        picked = posted.get('contractor', '')
    # The stepper's starting number goes into an Alpine expression, so it must be an
    # integer whatever was posted ("abc", blank) — the form's own error says what was wrong.
    try:
        headcount_value = int(form['headcount'].value())
    except (TypeError, ValueError):
        headcount_value = ContractorAttendance.HEADCOUNT_MIN
    return {
        'trade_value': form['trade'].value() or '',
        'headcount_value': headcount_value,
        'project': project,
        'entry': entry,
        'form': form,
        'errors': errors,
        'vendors': form.vendors,
        'unlisted_value': UNLISTED,
        'picked': picked,
        'trade_choices': ContractorAttendance.TRADE_CHOICES,
        'storage_ready': bool(bills_bucket()),
        'today': timezone.localdate(),
        'now_hhmm': _now_hhmm(),
    }


def _form_errors(form):
    """Every message from a bound form, field errors first, flattened for one list."""
    return [message for field_errors in form.errors.values() for message in field_errors]


def _upload(request, project):
    """Upload the posted photo, if any, and return its stored dict, or None when no photo
    was posted. Raises AttendanceRefused on a refused or failed upload."""
    photo = request.FILES.get('photo')
    if not photo:
        return None
    try:
        return upload_visit_photo(photo, project, kind=ATTENDANCE_PHOTO_KIND,
                                  storage_off=ATTENDANCE_STORAGE_OFF)
    except QaqcVisitRefused as exc:
        raise AttendanceRefused(str(exc))


def _after_write_url(request, project):
    """The attendance page — or, for a recorder whose last entry on a site they no longer
    see has just been voided, their dashboard, since the page would now refuse them."""
    if _page_scope(request, project) is None:
        return get_user_dashboard(request.user)
    return reverse('attendance_today', args=[project.project_id])


@login_required
def attendance_add(request, project_id):
    """
    Log a contractor crew on the site for today: contractor (or not listed), trade,
    headcount, time in and a group photo. GET draws the form; POST records it and
    redirects to the attendance page.

    Access: user_can_log_attendance — a Site Engineer working on the site, the PM or a
    coordinator, on a live site. Someone who cannot see the site gets the site 404; a
    viewer without that authority gets 403.
    """
    project = _active_project(project_id)
    # Visibility first, so a site the user cannot see stays indistinguishable from none.
    if not user_can_view_project(request.user, project):
        return refuse_site_view_404(request, project)
    # Only a Site Engineer on the site or its PM / coordinators log crews (rulings 6, A);
    # portfolio roles and other viewers read the page but do not write it.
    if not user_can_log_attendance(request.user, project):
        return HttpResponseForbidden()

    if request.method != 'POST':
        form = AttendanceForm()
        return render(request, 'projects/attendance/attendance_form.html',
                      _form_context(project, form, None, []))

    form = AttendanceForm(request.POST, request.FILES)
    if not form.is_valid():
        return render(request, 'projects/attendance/attendance_form.html',
                      _form_context(project, form, None, _form_errors(form)))
    data = form.cleaned_data
    unlisted_name = normalise_unlisted_name(data['unlisted_contractor_name'])
    # The friendly duplicate answer before anything is uploaded (edge case 1); the writer
    # asks again and the partial unique index is the backstop for a race.
    if live_duplicate_exists(project, timezone.localdate(), data['contractor'],
                             unlisted_name, data['trade']):
        return render(request, 'projects/attendance/attendance_form.html',
                      _form_context(project, form, None, [DUPLICATE_ENTRY]))
    stored = None
    try:
        stored = _upload(request, project)
        entry = create_attendance(
            project, request.user, contractor=data['contractor'], unlisted_name=unlisted_name,
            trade=data['trade'], trade_remark=data['trade_remark'],
            headcount=data['headcount'], time_in=data['time_in'], stored_photo=stored,
            photo_taken_at=data['photo_taken_at'])
    except AttendanceRefused as exc:
        discard_unrecorded_attendance_photo(stored)
        return render(request, 'projects/attendance/attendance_form.html',
                      _form_context(project, form, None, [str(exc)]))
    messages.success(request, f"Attendance saved: {entry.contractor_label} — "
                              f"{entry.trade_label}, {entry.headcount} "
                              f"{'person' if entry.headcount == 1 else 'people'}.")
    return redirect('attendance_today', project_id=project.project_id)


@login_required
def attendance_edit(request, project_id, entry_pk):
    """
    Edit one of today's entries: every field of the add form plus time out; a new photo
    replaces the current one (the old path stays in the history), no photo keeps it.
    Same-day edits need no note. The page also carries Void this entry.

    Access: user_can_change_attendance_today — the entry's day is today, it is not
    voided, and the user could log on the site or recorded this entry. Someone who cannot
    reach the entry gets the site 404; an entry that can no longer be changed (another
    day, voided) redirects to the attendance page with a message (edge case 2).
    """
    project = _active_project(project_id)
    entry = _entry_or_404(project, entry_pk)
    # Reaching the entry: a viewer of the site, or its recorder today (ruling F).
    if not _can_open_entry(request.user, project, entry):
        return refuse_site_view_404(request, project)
    # Changing it: today only, live only, the site's loggers or the entry's recorder.
    if not user_can_change_attendance_today(request.user, entry):
        messages.error(request, NOT_CHANGEABLE)
        return redirect(_after_write_url(request, project))

    if request.method != 'POST':
        form = AttendanceForm(entry=entry, initial={
            'unlisted_contractor_name': entry.unlisted_contractor_name,
            'trade': entry.trade, 'trade_remark': entry.trade_remark,
            'headcount': entry.headcount,
            'time_in': entry.time_in.strftime('%H:%M'),
            'time_out': entry.time_out.strftime('%H:%M') if entry.time_out else '',
        })
        return render(request, 'projects/attendance/attendance_form.html',
                      _form_context(project, form, entry, []))

    form = AttendanceForm(request.POST, request.FILES, entry=entry)
    if not form.is_valid():
        return render(request, 'projects/attendance/attendance_form.html',
                      _form_context(project, form, entry, _form_errors(form)))
    data = form.cleaned_data
    stored = None
    try:
        stored = _upload(request, project)
        entry, changed = update_attendance(entry, {
            'contractor': data['contractor'],
            'unlisted_name': data['unlisted_contractor_name'],
            'trade': data['trade'], 'trade_remark': data['trade_remark'],
            'headcount': data['headcount'], 'time_in': data['time_in'],
            'time_out': data['time_out'],
            'stored_photo': stored, 'photo_taken_at': data['photo_taken_at'],
        }, request.user)
    except AttendanceRefused as exc:
        discard_unrecorded_attendance_photo(stored)
        return render(request, 'projects/attendance/attendance_form.html',
                      _form_context(project, form, entry, [str(exc)]))
    if changed:
        messages.success(request, f"Entry updated: {entry.contractor_label} — {entry.trade_label}.")
    else:
        messages.info(request, 'Nothing changed.')
    return redirect('attendance_today', project_id=project.project_id)


@login_required
@require_POST
def attendance_time_out(request, project_id, entry_pk):
    """
    Record a crew's time out (the card's Mark time out). POST only; redirects to the
    attendance page with a message either way.

    Access: as attendance_edit — user_can_change_attendance_today, re-asked by the writer
    under the row lock.
    """
    project = _active_project(project_id)
    entry = _entry_or_404(project, entry_pk)
    # Reaching the entry: a viewer of the site, or its recorder today (ruling F).
    if not _can_open_entry(request.user, project, entry):
        return refuse_site_view_404(request, project)
    raw = (request.POST.get('time_out') or '').strip()
    try:
        time_out = datetime.strptime(raw, '%H:%M').time() if raw else None
    except ValueError:
        time_out = None
    try:
        # The writer asks user_can_change_attendance_today itself (today, live, the
        # site's loggers or the recorder) — the authority check lives there.
        entry = set_time_out(entry, time_out, request.user)
    except AttendanceRefused as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"Time out {entry.time_out.strftime('%H:%M')} recorded for "
                                  f"{entry.contractor_label} — {entry.trade_label}.")
    return redirect(_after_write_url(request, project))


@login_required
@require_POST
def attendance_void(request, project_id, entry_pk):
    """
    Void one of today's entries with a note (the edit page's Void this entry). The
    entry stays on record, struck through, and frees its slot. POST only.

    Access: as attendance_edit — user_can_change_attendance_today, re-asked by the writer
    under the row lock. A blank note is refused back to the edit page.
    """
    project = _active_project(project_id)
    entry = _entry_or_404(project, entry_pk)
    # Reaching the entry: a viewer of the site, or its recorder today (ruling F).
    if not _can_open_entry(request.user, project, entry):
        return refuse_site_view_404(request, project)
    try:
        # The writer asks user_can_change_attendance_today itself — the authority check
        # lives there.
        entry = void_attendance(entry, request.POST.get('void_note'), request.user)
    except AttendanceRefused as exc:
        messages.error(request, str(exc))
        if user_can_change_attendance_today(request.user, entry):
            return redirect('attendance_edit', project_id=project.project_id,
                            entry_pk=entry.pk)
        return redirect(_after_write_url(request, project))
    messages.success(request, f"Entry voided: {entry.contractor_label} — {entry.trade_label}.")
    return redirect(_after_write_url(request, project))
