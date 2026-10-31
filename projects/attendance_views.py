"""
Contractor attendance screens — ATT-B1 (the same day) and ATT-B2 (after the day).

    attendance_today     the site's attendance page: Today, + Add entry; then (ATT-B2)
                         Summary for a period and History of past days
    attendance_add       log a crew (Site Engineer on the site, PM, coordinators)
    attendance_edit      edit an entry today, with Void this entry
    attendance_time_out  POST: the crew's time out
    attendance_void      POST: void with a note — today's, or a past day's by a corrector
    attendance_photo     one entry's group photo, a link signed at the click
    attendance_correct   ATT-B2: correct a past day's entry with a reason, with Void
    attendance_add_late  ATT-B2: an entry for a day 1-7 days back, with a reason
    attendance_to_map    ATT-B3: SCM's "Contractors to map" queue, Map all per group
    attendance_map       ATT-B3: POST, map one not-listed entry from its card
    attendance_remap     ATT-B3: POST, change a mapping from its card, with a note

ATT-B3 also adds the photo-time chips on the cards (attendance.photo_time_flags) and the
CONFIRMED answer for a correction that changes nothing. Open-entry reporting and
notifications are later sessions (ATT-B4, ATT-C) and are not here.

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
from datetime import datetime, timedelta
from itertools import groupby
from operator import attrgetter
from urllib.parse import urlencode

from django.contrib import messages
from django.db.models import Count, Max, Min, Prefetch, Q, Sum
from django.db.models.functions import Lower
from django.http import Http404, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from .attendance import (
    ATTENDANCE_PHOTO_KIND, ATTENDANCE_STORAGE_OFF, DUPLICATE_ENTRY, DUPLICATE_ENTRY_PAST_DAY,
    NOT_CHANGEABLE, NOT_CORRECTABLE, AttendanceRefused, correct_attendance, create_attendance,
    create_late_attendance, discard_unrecorded_attendance_photo, is_late_entry,
    late_entry_window, live_duplicate_exists, map_attendance, map_attendance_group,
    normalise_unlisted_name, photo_time_flags, remap_attendance, set_time_out,
    update_attendance, void_attendance,
)
from .bill_storage import bill_pdf_url, bills_bucket
from .decorators import get_user_dashboard, login_required
from .forms_attendance import (
    UNLISTED, AttendanceForm, CorrectionForm, LateAttendanceForm, MapForm, MapGroupForm,
    RemapForm, contractor_choices,
)
from .models import ContractorAttendance, ContractorAttendanceRevision
from .permissions import (
    attendance_entry_correctable, attendance_entry_mappable, attendance_entry_remappable,
    attendance_photo_entry_ids, user_can_add_late_attendance, user_can_change_attendance_today,
    user_can_correct_attendance, user_can_log_attendance, user_can_map_attendance,
    user_can_view_attendance_history, user_can_view_attendance_photo, user_can_view_project,
    user_is_attendance_corrector, user_is_todays_attendance_recorder,
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

#: The Summary's periods (ATT-B2 ruling 10), as the ?period= values the page accepts.
SUMMARY_PERIODS = [
    ('this_week', 'This week'),
    ('last_week', 'Last week'),
    ('this_month', 'This month'),
    ('last_month', 'Last month'),
]
DEFAULT_PERIOD = 'this_week'

#: How many past days History shows (ruling 11): 30 by default, 90 after "Show older".
HISTORY_DAYS = 30
HISTORY_DAYS_OLDER = 90


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
    """The entry's revisions as display rows, oldest first: who, when, what. An edit or a
    mapping (ATT-B3) names each field that changed, before → after; a photo change says so
    without the path; a confirmation has no changes, only its note. Reads the prefetched
    revisions, so it costs no query."""
    rows = []
    for rev in entry.revisions.all():
        changes = []
        if rev.action in (ContractorAttendanceRevision.EDITED,
                          ContractorAttendanceRevision.MAPPED) and rev.before:
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
                     'at': rev.changed_at, 'changes': changes, 'note': rev.note,
                     # A correction or late entry is written on a later day than the
                     # entry's own; the line then shows the date, not just the time.
                     'other_day': timezone.localtime(rev.changed_at).date() != entry.date})
    return rows


def _revisions_prefetch():
    """Every revision with its author, oldest first — what _history() reads."""
    return Prefetch('revisions', queryset=ContractorAttendanceRevision.objects
                    .select_related('changed_by__user').order_by('changed_at', 'pk'))


def period_bounds(period, today):
    """(start, end), both inclusive, of a Summary period around `today` — an IST date
    from timezone.localdate() (ruling 10). Weeks run Monday to Sunday: a Sunday belongs
    to the week before it, a Monday starts the next (edge case 8). "This" periods end
    today; "last" periods are whole. Anything unknown is This week."""
    week_start = today - timedelta(days=today.weekday())
    if period == 'last_week':
        return week_start - timedelta(days=7), week_start - timedelta(days=1)
    month_start = today.replace(day=1)
    if period == 'this_month':
        return month_start, today
    if period == 'last_month':
        last_month_end = month_start - timedelta(days=1)
        return last_month_end.replace(day=1), last_month_end
    return week_start, today


def summary_queryset(project, start, end):
    """One grouped query: live entries on `project` from `start` to `end`, one row per
    contractor (or not-listed name) and trade, with days present and man-days.

    Not-listed names group on their lower-cased value — the writer has already stripped
    them and collapsed inner whitespace, so this is the unique index's own comparison and
    "Raju  Civil" meets "raju civil" (edge case 9); the row shows one of the typed
    spellings (Min). Days present counts distinct dates:
    one live entry per slot per day makes it equal to the entry count today, but distinct
    keeps it right if that ever loosens. Man-days are the headcount sum (ruling 9: one
    person per entry is one man-day; hours are never counted). Voided entries are out.
    No is_active filter on the contractor, so a deactivated one still appears by name."""
    return (ContractorAttendance.objects
            .filter(project=project, is_voided=False, date__gte=start, date__lte=end)
            .annotate(unlisted_key=Lower('unlisted_contractor_name'))
            .values('contractor_id', 'contractor__name', 'unlisted_key', 'trade')
            .annotate(days=Count('date', distinct=True), man_days=Sum('headcount'),
                      unlisted_display=Min('unlisted_contractor_name'))
            .order_by('contractor__name', 'unlisted_key', 'trade'))


def attendance_summary(project, start, end):
    """The Summary section: rows (listed contractors first, then not-listed names), the
    billable man-days of listed contractors, and the man-days waiting for SCM mapping
    (not-listed names, never billable). The totals add up the grouped rows the database
    returned, not the entries."""
    rows = [{
        'name': row['contractor__name'] if row['contractor_id'] else row['unlisted_display'],
        'listed': row['contractor_id'] is not None,
        'trade': TRADE_LABELS.get(row['trade'], row['trade']),
        'days': row['days'],
        'man_days': row['man_days'] or 0,
    } for row in summary_queryset(project, start, end)]
    rows.sort(key=lambda r: not r['listed'])  # stable: keeps the query's name order inside each group
    return {
        'rows': rows,
        'billable': sum(r['man_days'] for r in rows if r['listed']),
        'waiting': sum(r['man_days'] for r in rows if not r['listed']),
    }


def attendance_history(user, project, today, span_days):
    """The History section: past days from `span_days` back to yesterday, newest first,
    each with its cards and header counts (crews, people, left open — live entries only).
    Bounded by the date range, never the whole table.

    Who may correct is asked once for the site (user_is_attendance_corrector) and paired
    per card with the entry's own state; photo rights come from the bulk helper; who may
    map (role only) and who could log on the site (for the Locked line) are asked once
    too. Every entry is handed the page's one `project`, so the managers are looked up
    once — a month of history costs a fixed handful of queries, not a few per card.
    """
    entries = list(ContractorAttendance.objects
                   .filter(project=project, date__gte=today - timedelta(days=span_days),
                           date__lt=today)
                   .select_related('contractor', 'recorded_by__user', 'voided_by__user')
                   .prefetch_related(_revisions_prefetch())
                   .order_by('-date', 'is_voided', 'time_in', 'pk'))
    for entry in entries:
        entry.project = project
    corrector = user_is_attendance_corrector(user, project)
    photo_ids = attendance_photo_entry_ids(user, project, entries)
    mapper = user_can_map_attendance(user)
    # The "Locked. Ask the PM to correct." line is for the people who log crews here (Site
    # Engineers) and might expect to change a past day; Finance, the CEO and other readers
    # never could, so the line would only puzzle them (ATT-B3 ruling 14).
    logger = user_can_log_attendance(user, project)
    days = []
    for day, group in groupby(entries, key=attrgetter('date')):
        cards = []
        for entry in group:
            can_correct = corrector and attendance_entry_correctable(entry)
            can_see_photo = entry.pk in photo_ids
            cards.append({
                'entry': entry,
                'past': True,
                'can_change': False,  # a past day is never in the same-day flow
                'can_correct': can_correct,
                'locked': logger and not can_correct and not entry.is_voided,
                'can_see_photo': can_see_photo,
                'photo_flags': photo_time_flags(entry) if can_see_photo else [],
                'can_map': mapper and attendance_entry_mappable(entry),
                'can_remap': mapper and attendance_entry_remappable(entry),
                'is_late': is_late_entry(entry),
                'history': _history(entry),
            })
        live = [c['entry'] for c in cards if not c['entry'].is_voided]
        days.append({
            'date': day,
            'cards': cards,
            'crews': len(live),
            'people': sum(e.headcount for e in live),
            'left_open': sum(1 for e in live if e.time_out is None),
            'voided': len(cards) - len(live),
        })
    return days


@login_required
@require_GET
def attendance_today(request, project_id):
    """
    The site's attendance page: Today — the header counts, + Add entry, and today's
    entries (live first, voided struck through at the bottom), each with its photo (if
    allowed), recorder and edit history. Then (ATT-B2) Summary — man-days per contractor
    and trade for ?period= (this_week by default) — and History — past days, newest
    first, 30 days or 90 with ?older=1, with + Add late entry for a corrector.

    Access: anyone who can see the site (user_can_view_project) sees all three; a Site
    Engineer whose tasks were moved off the site today sees only the live entries they
    recorded today (ruling F) — no Summary, no History, and the GET parameters are
    ignored. Everyone else gets the site 404.
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
                  .prefetch_related(_revisions_prefetch())
                  .order_by('is_voided', 'time_in', 'pk'))
    if scope == 'own':
        entries_qs = entries_qs.filter(recorded_by=profile, is_voided=False)

    # SCM / Admin map not-listed entries from their cards (ATT-B3 ruling 6). Role only, so
    # asked once; the picker's vendor list is read only for a mapper.
    mapper = user_can_map_attendance(request.user)
    cards = []
    for entry in entries_qs:
        can_see_photo = user_can_view_attendance_photo(request.user, entry)
        cards.append({
            'entry': entry,
            # Per entry, because the recorder term differs card by card; a day has a
            # handful of crews, so the helper's queries stay small.
            'can_change': user_can_change_attendance_today(request.user, entry),
            'can_see_photo': can_see_photo,
            # The chip would reveal the photo's time, so only the photo's audience gets it.
            'photo_flags': photo_time_flags(entry) if can_see_photo else [],
            'can_map': mapper and attendance_entry_mappable(entry),
            'can_remap': mapper and attendance_entry_remappable(entry),
            'history': _history(entry),
        })
    live = [c['entry'] for c in cards if not c['entry'].is_voided]
    context = {
        'project': project,
        'today': today,
        'cards': cards,
        'crews': len(live),
        'people': sum(e.headcount for e in live),
        'open_count': sum(1 for e in live if e.time_out is None),
        'can_add': scope == 'all' and user_can_log_attendance(request.user, project),
        'own_only': scope == 'own',
        'now_hhmm': _now_hhmm(),
        'show_history': False,
        'map_vendors': contractor_choices() if mapper else [],
    }
    # Summary and History: every viewer of the site, never the ruling-F recorder, whose
    # GET parameters are then never read (ruling 13).
    if scope == 'all' and user_can_view_attendance_history(request.user, project):
        period = request.GET.get('period')
        if period not in dict(SUMMARY_PERIODS):
            period = DEFAULT_PERIOD
        older = request.GET.get('older') == '1'
        start, end = period_bounds(period, today)
        context.update({
            'show_history': True,
            'periods': SUMMARY_PERIODS,
            'period': period,
            'period_start': start,
            'period_end': end,
            'summary': attendance_summary(project, start, end),
            'older': older,
            'history_days': HISTORY_DAYS_OLDER if older else HISTORY_DAYS,
            'history': attendance_history(request.user, project, today,
                                          HISTORY_DAYS_OLDER if older else HISTORY_DAYS),
            # PM, coordinators and Admin add an entry for a missed day (ruling 4).
            'can_add_late': user_can_add_late_attendance(request.user, project),
        })
    return render(request, 'projects/attendance/attendance_page.html', context)


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

def _form_context(project, form, entry, errors, mode=None):
    """What the add / edit / correct / late template reads. `mode` is 'add', 'edit',
    'correct' or 'late'; left out, it is 'edit' with an entry and 'add' without. The
    picker is rendered from `form.vendors` server-side; Alpine filters those rows in the
    page, so no JSON is needed."""
    mode = mode or ('edit' if entry is not None else 'add')
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
    context = {
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
        'mode': mode,
        # A new entry needs a photo; an edit or correction keeps the current one.
        'photo_required': mode in ('add', 'late'),
        # The time-out field: never on the same-day add (the crew is arriving).
        'show_time_out': mode != 'add',
    }
    if mode == 'correct':
        # The correction page shows the entry's edit history below the form (one query).
        context['history'] = _history(entry)
        context['is_late'] = is_late_entry(entry)
    if mode == 'late':
        earliest, latest = late_entry_window()
        context.update({'late_min': earliest, 'late_max': latest,
                        'date_value': form['date'].value() or latest.isoformat()})
    return context


def _entry_initial(entry):
    """An entry's current values as the edit and correction forms' initial data."""
    return {
        'unlisted_contractor_name': entry.unlisted_contractor_name,
        'trade': entry.trade, 'trade_remark': entry.trade_remark,
        'headcount': entry.headcount,
        'time_in': entry.time_in.strftime('%H:%M'),
        'time_out': entry.time_out.strftime('%H:%M') if entry.time_out else '',
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
        form = AttendanceForm(entry=entry, initial=_entry_initial(entry))
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
    Void an entry with a note (Void this entry on the edit page, or on the ATT-B2
    correction page for a past day). The entry stays on record, struck through, and
    frees its slot. POST only.

    Access: today's entry as attendance_edit — user_can_change_attendance_today; a past
    day's entry by a corrector — user_can_correct_attendance (PM, coordinators, Admin).
    The writer re-asks both under the row lock. A blank note is refused back to the page
    it came from.
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
        # Back to the form the void came from, when the user may still use it.
        if user_can_change_attendance_today(request.user, entry):
            return redirect('attendance_edit', project_id=project.project_id,
                            entry_pk=entry.pk)
        if user_can_correct_attendance(request.user, entry):
            return redirect('attendance_correct', project_id=project.project_id,
                            entry_pk=entry.pk)
        return redirect(_after_write_url(request, project))
    messages.success(request, f"Entry voided: {entry.contractor_label} — {entry.trade_label}.")
    return redirect(_after_write_url(request, project))


@login_required
def attendance_correct(request, project_id, entry_pk):
    """
    ATT-B2: correct a past day's entry — contractor, not-listed name, trade, remark,
    headcount, time in, time out, photo — with a required reason, written on the EDITED
    revision. The date is shown, never posted. The page carries the entry's edit history
    and Void this entry (attendance_void). GET draws the form; POST saves and returns to
    the attendance page.

    Access: user_can_correct_attendance — the entry is from before today and not voided,
    and the user is the site's PM, a coordinator, or Admin. Someone who cannot see the
    site gets the site 404; anyone else (a Site Engineer, today's entry, a voided one)
    is sent back to the attendance page with a message.
    """
    project = _active_project(project_id)
    entry = _entry_or_404(project, entry_pk)
    # Visibility first, so a site the user cannot see stays indistinguishable from none.
    # A ruling-F recorder never reaches a past entry: they are not a viewer.
    if not user_can_view_project(request.user, project):
        return refuse_site_view_404(request, project)
    # Only PM / coordinators / Admin correct, and only a live entry from before today
    # (rulings 1, 3); Site Engineers never touch a past day. The writer asks again.
    if not user_can_correct_attendance(request.user, entry):
        messages.error(request, NOT_CORRECTABLE)
        return redirect('attendance_today', project_id=project.project_id)

    if request.method != 'POST':
        form = CorrectionForm(entry=entry, initial=_entry_initial(entry))
        return render(request, 'projects/attendance/attendance_form.html',
                      _form_context(project, form, entry, [], mode='correct'))

    form = CorrectionForm(request.POST, request.FILES, entry=entry)
    if not form.is_valid():
        return render(request, 'projects/attendance/attendance_form.html',
                      _form_context(project, form, entry, _form_errors(form), mode='correct'))
    data = form.cleaned_data
    stored = None
    try:
        stored = _upload(request, project)
        entry, changed = correct_attendance(entry, {
            'contractor': data['contractor'],
            'unlisted_name': data['unlisted_contractor_name'],
            'trade': data['trade'], 'trade_remark': data['trade_remark'],
            'headcount': data['headcount'], 'time_in': data['time_in'],
            'time_out': data['time_out'],
            'stored_photo': stored, 'photo_taken_at': data['photo_taken_at'],
        }, request.user, data['reason'])
    except AttendanceRefused as exc:
        discard_unrecorded_attendance_photo(stored)
        return render(request, 'projects/attendance/attendance_form.html',
                      _form_context(project, form, entry, [str(exc)], mode='correct'))
    if changed:
        messages.success(request, f"Entry corrected: {entry.contractor_label} — "
                                  f"{entry.trade_label}, {entry.date:%d %b}.")
    else:
        # The writer recorded a CONFIRMED revision with the reason (ATT-B3 ruling 10).
        messages.success(request, 'Entry confirmed — no changes, reason recorded.')
    return redirect('attendance_today', project_id=project.project_id)


@login_required
def attendance_add_late(request, project_id):
    """
    ATT-B2: add an entry for a missed day, 1 to 7 days before today, with a group photo
    (a gallery photo is expected — the page does not force the camera), an optional time
    out and a required reason, written on the CREATED revision. GET draws the form; POST
    records it and returns to the attendance page.

    Access: user_can_add_late_attendance — the site's PM, a coordinator, or Admin, on any
    site that is not soft-deleted. Someone who cannot see the site gets the site 404; a
    viewer without that authority gets 403.
    """
    project = _active_project(project_id)
    # Visibility first, so a site the user cannot see stays indistinguishable from none.
    if not user_can_view_project(request.user, project):
        return refuse_site_view_404(request, project)
    # Only PM / coordinators / Admin add a missed day (ruling 4); a Site Engineer logs
    # today only, and the other portfolio roles read.
    if not user_can_add_late_attendance(request.user, project):
        return HttpResponseForbidden()

    if request.method != 'POST':
        form = LateAttendanceForm()
        return render(request, 'projects/attendance/attendance_form.html',
                      _form_context(project, form, None, [], mode='late'))

    form = LateAttendanceForm(request.POST, request.FILES)
    if not form.is_valid():
        return render(request, 'projects/attendance/attendance_form.html',
                      _form_context(project, form, None, _form_errors(form), mode='late'))
    data = form.cleaned_data
    unlisted_name = normalise_unlisted_name(data['unlisted_contractor_name'])
    # The friendly duplicate answer before anything is uploaded (edge case 6); the writer
    # asks again and the partial unique index is the backstop for a race.
    if live_duplicate_exists(project, data['date'], data['contractor'], unlisted_name,
                             data['trade']):
        return render(request, 'projects/attendance/attendance_form.html',
                      _form_context(project, form, None, [DUPLICATE_ENTRY_PAST_DAY],
                                    mode='late'))
    stored = None
    try:
        stored = _upload(request, project)
        entry = create_late_attendance(project, data['date'], {
            'contractor': data['contractor'], 'unlisted_name': unlisted_name,
            'trade': data['trade'], 'trade_remark': data['trade_remark'],
            'headcount': data['headcount'], 'time_in': data['time_in'],
            'time_out': data['time_out'],
            'stored_photo': stored, 'photo_taken_at': data['photo_taken_at'],
        }, request.user, data['reason'])
    except AttendanceRefused as exc:
        discard_unrecorded_attendance_photo(stored)
        return render(request, 'projects/attendance/attendance_form.html',
                      _form_context(project, form, None, [str(exc)], mode='late'))
    messages.success(request, f"Late entry saved for {entry.date:%d %b}: "
                              f"{entry.contractor_label} — {entry.trade_label}, "
                              f"{entry.headcount} "
                              f"{'person' if entry.headcount == 1 else 'people'}.")
    return redirect('attendance_today', project_id=project.project_id)


# ---------------------------------------------------------------------------
# SCM mapping — ATT-B3
# ---------------------------------------------------------------------------

def _unmapped_entries():
    """Every entry waiting for SCM mapping: live, no contractor, on a site that is not
    soft-deleted (edge case 11), with the group key the queue and the bulk writer share —
    the typed name lower-cased, exactly the unique index's LOWER(...) on a name the writers
    already stripped and whitespace-collapsed, so "Raju civil" and "RAJU  Civil" are one
    crew (edge case 7)."""
    return (ContractorAttendance.objects
            .filter(is_voided=False, contractor__isnull=True, project__is_deleted=False)
            .annotate(name_key=Lower('unlisted_contractor_name')))


def unmapped_queue_queryset():
    """The queue's ONE grouped query: a row per site + typed name + trade, with entries,
    first and last date, man-days and one spelling of the name (Min). Split by trade
    because a list of trades needs a Postgres-only aggregate and the tests run on SQLite;
    unmapped_queue_groups() folds the trades back into one group per site + name."""
    return (_unmapped_entries()
            .values('project_id', 'project__project_id', 'name_key', 'trade')
            .annotate(entries=Count('pk'), first_date=Min('date'), last_date=Max('date'),
                      man_days=Sum('headcount'), display=Min('unlisted_contractor_name'))
            .order_by('project__project_id', 'name_key', 'trade'))


def unmapped_queue_groups():
    """The "Contractors to map" groups, oldest first (by first date, then site and name):
    site code, typed name (one spelling), trades, entries, first and last date, man-days.
    One query (unmapped_queue_queryset); the fold is in Python."""
    groups = {}
    for row in unmapped_queue_queryset():
        key = (row['project_id'], row['name_key'])
        group = groups.get(key)
        if group is None:
            group = groups[key] = {
                'site': row['project__project_id'], 'name_key': row['name_key'],
                'name': row['display'], 'trades': [], 'entries': 0, 'man_days': 0,
                'first': row['first_date'], 'last': row['last_date'],
            }
        group['trades'].append(TRADE_LABELS.get(row['trade'], row['trade']))
        group['entries'] += row['entries']
        group['man_days'] += row['man_days'] or 0
        group['first'] = min(group['first'], row['first_date'])
        group['last'] = max(group['last'], row['last_date'])
        group['name'] = min(group['name'], row['display'])
    return sorted(groups.values(), key=lambda g: (g['first'], g['site'], g['name_key']))


def attendance_unmapped_group_count():
    """How many site + typed-name groups wait for mapping — the SCM dashboard link's
    count (ATT-B3 ruling 9). order_by() clears the model's default ordering, which would
    otherwise join the DISTINCT and count entries instead of groups."""
    return _unmapped_entries().values('project_id', 'name_key').order_by().distinct().count()


def _first_form_error(form):
    errors = _form_errors(form)
    return errors[0] if errors else 'Check the form and try again.'


def _skipped_line(entry, reason):
    """One skipped entry of a bulk mapping, by date, trade and reason (ATT-B3 ruling 5)."""
    return f"Not mapped — {entry.date.day} {entry.date:%b} · {entry.trade_label}: {reason}"


@login_required
def attendance_to_map(request):
    """
    ATT-B3: "Contractors to map" — every live "Contractor not listed" entry on a site that
    is not soft-deleted, grouped by site + typed name (case and spacing ignored), oldest
    first, with a searchable contractor picker and Map all per group. ?site=<site code>
    narrows the list. POST maps one group (map_attendance_group: one transaction per
    entry; a collision skips that entry and says why) and returns here.

    Access: user_can_map_attendance — SCM and Admin, on every site. Everyone else 403.
    """
    # Only SCM / Admin map (ruling 1): the PM corrects through ATT-B2, other roles read.
    if not user_can_map_attendance(request.user):
        return HttpResponseForbidden()
    site = (request.GET.get('site') or '').strip()

    if request.method == 'POST':
        form = MapGroupForm(request.POST)
        back = reverse('attendance_to_map')
        if site:
            back += '?' + urlencode({'site': site})
        if not form.is_valid():
            messages.error(request, _first_form_error(form))
            return redirect(back)
        data = form.cleaned_data
        project = _active_project(data['site'])
        try:
            mapped, skipped = map_attendance_group(project, data['name_key'],
                                                   data['contractor'], request.user)
        except AttendanceRefused as exc:
            messages.error(request, str(exc))
            return redirect(back)
        if mapped:
            messages.success(request, f"Mapped {len(mapped)} "
                                      f"{'entry' if len(mapped) == 1 else 'entries'} on "
                                      f"{project.project_id} to {data['contractor'].name}.")
        for entry, reason in skipped:
            messages.warning(request, _skipped_line(entry, reason))
        if not mapped and not skipped:
            messages.info(request, 'Nothing left to map in that group — it was already mapped.')
        return redirect(back)

    groups = unmapped_queue_groups()
    sites = sorted({g['site'] for g in groups})
    if site:
        groups = [g for g in groups if g['site'] == site]
    return render(request, 'projects/attendance/attendance_to_map.html', {
        'groups': groups,
        'sites': sites,
        'site': site,
        'vendors': contractor_choices(),
    })


def _card_url(project, entry):
    return reverse('attendance_today', args=[project.project_id]) + f'#att-entry-{entry.pk}'


@login_required
@require_POST
def attendance_map(request, project_id, entry_pk):
    """
    ATT-B3: map one "Contractor not listed" entry to a contractor, from its card on the
    site's attendance page (ruling 6) — the same writer as the queue. POST only; back to
    the attendance page with a message either way.

    Access: a viewer of the site (both mapper roles see every site) who is a mapper —
    SCM or Admin (user_can_map_attendance). Anyone else who can see the site gets 403;
    the writer re-asks under the row lock.
    """
    project = _active_project(project_id)
    entry = _entry_or_404(project, entry_pk)
    # Visibility first, so a site the user cannot see stays indistinguishable from none.
    if not user_can_view_project(request.user, project):
        return refuse_site_view_404(request, project)
    # Only SCM / Admin map (ruling 1); the PM, Site Engineers, Finance and the CEO do not.
    if not user_can_map_attendance(request.user):
        return HttpResponseForbidden()
    form = MapForm(request.POST)
    if not form.is_valid():
        messages.error(request, _first_form_error(form))
        return redirect(_card_url(project, entry))
    typed = entry.unlisted_contractor_name
    try:
        entry = map_attendance(entry, form.cleaned_data['contractor'], request.user)
    except AttendanceRefused as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"Mapped ‘{typed}’ to {entry.contractor.name} — "
                                  f"{entry.trade_label}, {entry.date:%d %b}.")
    return redirect(_card_url(project, entry))


@login_required
@require_POST
def attendance_remap(request, project_id, entry_pk):
    """
    ATT-B3: change the contractor a not-listed entry was mapped to, with a required note
    (ruling 3). There is no un-map: a wrong mapping is fixed by mapping again (ruling 4).
    POST only, from the entry's card; back to the attendance page with a message.

    Access: as attendance_map — a viewer of the site who is SCM or Admin. Only an entry
    that was logged as a typed name and is mapped can be changed
    (attendance_entry_remappable, asked by the writer under the row lock).
    """
    project = _active_project(project_id)
    entry = _entry_or_404(project, entry_pk)
    # Visibility first, so a site the user cannot see stays indistinguishable from none.
    if not user_can_view_project(request.user, project):
        return refuse_site_view_404(request, project)
    # Only SCM / Admin change a mapping (ruling 3); everyone else is refused.
    if not user_can_map_attendance(request.user):
        return HttpResponseForbidden()
    form = RemapForm(request.POST)
    if not form.is_valid():
        messages.error(request, _first_form_error(form))
        return redirect(_card_url(project, entry))
    try:
        entry = remap_attendance(entry, form.cleaned_data['contractor'], request.user,
                                 form.cleaned_data['note'])
    except AttendanceRefused as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"Mapping changed to {entry.contractor.name} — "
                                  f"{entry.trade_label}, {entry.date:%d %b}.")
    return redirect(_card_url(project, entry))
