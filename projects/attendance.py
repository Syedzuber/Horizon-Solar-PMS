"""
Contractor attendance — the ONLY writer of ContractorAttendance and
ContractorAttendanceRevision (ATT-B1).

    create_attendance()   a new entry for today, with a CREATED revision
    update_attendance()   a same-day edit, with an EDITED revision (none if nothing changed)
    set_time_out()        the crew's time out, with a TIME_OUT revision
    void_attendance()     a void with its note, with a VOIDED revision — today's by the
                          site's loggers or recorder, a past day's by a corrector (ATT-B2)

ATT-B2, after the day (PM / coordinators / Admin, permissions.user_is_attendance_corrector):
    correct_attendance()      a past-day correction with its note, an EDITED revision
    create_late_attendance()  an entry for a day 1-7 days back, a CREATED revision with its note

Each runs in one transaction and writes the entry change and its revision row together.
Each re-asks the permission helper itself — under the entry's row lock for the three that
change an existing entry — so the same-day rule (timezone.localdate(), ruling 10) and the
voided state are enforced here whatever the view did: a POST replayed after midnight is
refused by the writer, not just by the page.

Photos are uploaded BEFORE these run (the view calls qaqc_views.upload_visit_photo), so no
network call happens inside a transaction; a refused write hands the upload back to
discard_unrecorded_attendance_photo(), which removes only that file and only when nothing
records it.
"""
import logging
import re
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from .bill_storage import bills_bucket
from .design_storage import _client
from .models import (
    VENDOR_BILLABLE_KINDS, ContractorAttendance, ContractorAttendanceRevision,
)
from .permissions import (
    user_can_add_late_attendance, user_can_change_attendance_today, user_can_correct_attendance,
    user_can_log_attendance,
)

logger = logging.getLogger(__name__)

#: The storage path segment for attendance photos, in the private bills bucket.
ATTENDANCE_PHOTO_KIND = 'attendance'

ATTENDANCE_STORAGE_OFF = ('Attendance photos cannot be stored right now: the private store '
                          'is not configured. Nothing was saved.')

DUPLICATE_ENTRY = 'This contractor and trade is already logged today. Edit that entry instead.'
NOT_ALLOWED_TO_LOG = 'You cannot log attendance on this site.'
NOT_CHANGEABLE = ('This entry can no longer be changed: only a live entry from today can be '
                  'edited, timed out or voided.')
TIME_OUT_NOT_AFTER_IN = 'Time out must be later than time in, on the same day.'
TIME_OUT_ALREADY = 'Time out is already recorded. Edit the entry to change it.'
VOID_NOTE_REQUIRED = 'Say why this entry is being voided.'
CONTRACTOR_NOT_ALLOWED = 'Pick an active contractor from the list, or choose "Contractor not listed".'
TIME_LATER_THAN_NOW = "Time can't be later than now."

# ATT-B2 — after the day.
DUPLICATE_ENTRY_PAST_DAY = ('This contractor and trade is already logged on that day. '
                            'Correct that entry instead.')
CORRECTION_NOTE_REQUIRED = 'Say why this entry is being corrected.'
LATE_NOTE_REQUIRED = 'Say why this entry is late.'
NOT_CORRECTABLE = ("This entry cannot be corrected: only a live entry from before today can "
                   "be corrected, by the site's PM, a coordinator or Admin.")
NOT_ALLOWED_LATE = 'You cannot add a late entry on this site.'
LATE_DATE_OUT_OF_WINDOW = 'A late entry must be dated 1 to 7 days before today.'

#: How many days back a late entry may be dated (ATT-B2 ruling 4). Today is the normal
#: add; anything older is refused.
LATE_ENTRY_MAX_DAYS = 7

#: How far past the current IST time a time in / time out may be (ATT-B1a). The slack
#: covers a phone clock running a few minutes fast and a time typed as the crew walks in;
#: anything later is a time that has not happened yet.
FUTURE_TIME_GRACE = timedelta(minutes=15)

SNAPSHOT_SCHEMA = 1

_WHITESPACE = re.compile(r'\s+')


class AttendanceRefused(Exception):
    """A rule refused an attendance write. `str(exc)` is the user-facing message."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def normalise_unlisted_name(raw):
    """A typed contractor name as stored: stripped, inner whitespace collapsed to one
    space. Case is kept for display; the unique index compares it lower-cased, so
    "Raju  Civil" and " raju civil" are the same crew (edge case 8)."""
    return _WHITESPACE.sub(' ', (raw or '').strip())


def local_now():
    """The current IST datetime. One function, read by time_is_too_late() for the writers
    and the form alike, so a test moves both clocks with one patch."""
    return timezone.localtime()


def time_is_too_late(value):
    """True when `value` — a time on today's IST date — is later than now plus
    FUTURE_TIME_GRACE (ATT-B1a). Exactly now + 15 minutes is allowed.

    A bare time is read as today's because every writer acts on today's entries only (the
    same-day rule). When now + 15 minutes has already crossed midnight, every time left
    today is within the grace, so nothing is too late — comparing against the wrapped
    00:05 would refuse every time of the day instead.
    """
    if value is None:
        return False
    now = local_now()
    latest = now + FUTURE_TIME_GRACE
    if latest.date() != now.date():
        return False
    return value > latest.time()


def late_entry_window():
    """(earliest, latest) dates a late entry may carry: 7 days back to yesterday, IST
    (ruling 4). Read by the writer and the form's date picker alike; the picker's limits
    are only a convenience, the writer asks again."""
    today = timezone.localdate()
    return today - timedelta(days=LATE_ENTRY_MAX_DAYS), today - timedelta(days=1)


def is_late_entry(entry):
    """True when `entry` was recorded after its day — the derived "late entry" (ruling 4):
    its date is earlier than the IST date of its recorded_at. No field stores it."""
    return entry.date < timezone.localtime(entry.recorded_at).date()


def attendance_snapshot(entry):
    """The entry as a revision records it ("schema": 1): every field a write can change,
    the contractor's name beside its id (a later rename does not change a past revision),
    times as HH:MM strings, the photo's bucket and path (a replaced photo stays
    findable)."""
    def hhmm(value):
        return value.strftime('%H:%M') if value else None
    return {
        'schema': SNAPSHOT_SCHEMA,
        'contractor_id': entry.contractor_id,
        'contractor_name': entry.contractor.name if entry.contractor_id else None,
        'unlisted_contractor_name': entry.unlisted_contractor_name,
        'trade': entry.trade,
        'trade_remark': entry.trade_remark,
        'headcount': entry.headcount,
        'time_in': hhmm(entry.time_in),
        'time_out': hhmm(entry.time_out),
        'photo_file_name': entry.photo_file_name,
        'photo_bucket': entry.photo_bucket,
        'photo_path': entry.photo_path,
        'photo_taken_at': entry.photo_taken_at.isoformat() if entry.photo_taken_at else None,
        'is_voided': entry.is_voided,
        'void_note': entry.void_note,
    }


def _check_values(contractor, unlisted_name, trade, trade_remark, headcount, time_in,
                  time_out, current_contractor_id=None, day=None):
    """The value rules the form already applied, asked again so no caller can write past
    them; the database CHECKs are the last line. `current_contractor_id` lets an edit keep
    a contractor that has since been deactivated (edge case 9) — never pick one anew.

    `day` is the entry's date. The ATT-B1a "not later than now" rule applies only when it
    is today (ATT-B2 ruling 6): every time on a past day has already happened. The
    same-day writers pass no day, so for them the rule is asked exactly as in B1a."""
    if contractor is not None:
        keeping = current_contractor_id is not None and contractor.pk == current_contractor_id
        if not keeping and (contractor.kind not in VENDOR_BILLABLE_KINDS or not contractor.is_active):
            raise AttendanceRefused(CONTRACTOR_NOT_ALLOWED)
    elif not unlisted_name:
        raise AttendanceRefused('Type the contractor\'s name.')
    if trade not in dict(ContractorAttendance.TRADE_CHOICES):
        raise AttendanceRefused('Pick a trade.')
    if trade == ContractorAttendance.OTHER and not trade_remark:
        raise AttendanceRefused('Say what the trade is when you pick Other.')
    if (not isinstance(headcount, int) or headcount < ContractorAttendance.HEADCOUNT_MIN
            or headcount > ContractorAttendance.HEADCOUNT_MAX):
        raise AttendanceRefused(
            f'Headcount must be between {ContractorAttendance.HEADCOUNT_MIN} and '
            f'{ContractorAttendance.HEADCOUNT_MAX}.')
    if time_in is None:
        raise AttendanceRefused('Enter the time in.')
    if day is None or day == timezone.localdate():
        if time_is_too_late(time_in) or time_is_too_late(time_out):
            raise AttendanceRefused(TIME_LATER_THAN_NOW)
    if time_out is not None and time_out <= time_in:
        raise AttendanceRefused(TIME_OUT_NOT_AFTER_IN)


def live_duplicate_exists(project, day, contractor, unlisted_name, trade, exclude_pk=None):
    """True when another live (not voided) entry already holds this site + contractor (or
    not-listed name, case-blind) + trade + day. The friendly early answer; the partial
    unique indexes are the backstop for two requests racing past it."""
    qs = ContractorAttendance.objects.filter(project=project, date=day, trade=trade,
                                             is_voided=False)
    if contractor is not None:
        qs = qs.filter(contractor=contractor)
    else:
        qs = qs.filter(contractor__isnull=True, unlisted_contractor_name__iexact=unlisted_name)
    if exclude_pk is not None:
        qs = qs.exclude(pk=exclude_pk)
    return qs.exists()


def _save_or_refuse_duplicate(entry, duplicate_message=DUPLICATE_ENTRY, **save_kwargs):
    """Save `entry` inside a savepoint. An IntegrityError from a lost race on the slot is
    turned into `duplicate_message` (edge case 1, double-tap Save; the past-day writers
    pass DUPLICATE_ENTRY_PAST_DAY); any other IntegrityError is a broken rule upstream and
    is re-raised. The savepoint keeps the outer transaction usable for the duplicate query
    after the failed INSERT/UPDATE on Postgres."""
    try:
        with transaction.atomic():
            entry.save(**save_kwargs)
    except IntegrityError:
        if live_duplicate_exists(entry.project, entry.date, entry.contractor,
                                  entry.unlisted_contractor_name, entry.trade,
                                  exclude_pk=entry.pk):
            raise AttendanceRefused(duplicate_message)
        raise


def _lock(entry):
    """The entry re-read under a row lock, with what the permission helpers and the
    snapshot read. select_for_update() serialises two writes to one entry (two taps of
    Mark time out, an edit racing a void), so each sees the other's result."""
    return (ContractorAttendance.objects.select_for_update(of=('self',))
            .select_related('project', 'contractor')
            .get(pk=entry.pk))


def _revision(entry, action, before, by_profile, note='', now=None):
    return ContractorAttendanceRevision.objects.create(
        attendance=entry, action=action, before=before,
        after=attendance_snapshot(entry), note=note, changed_by=by_profile,
        changed_at=now or timezone.now(),
    )


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------

def create_attendance(project, user, *, contractor, unlisted_name, trade, trade_remark,
                      headcount, time_in, stored_photo, photo_taken_at):
    """Record a new entry for today on `project`, by `user`, with its CREATED revision.

    `date` is timezone.localdate() here, never a form value (ruling 10). `stored_photo` is
    upload_visit_photo()'s dict and is required (ruling 5). Raises AttendanceRefused when
    `user` may not log on the site, a value rule fails, or the slot is taken
    (DUPLICATE_ENTRY). Returns the entry.
    """
    unlisted_name = '' if contractor is not None else normalise_unlisted_name(unlisted_name)
    trade_remark = (trade_remark or '').strip() if trade == ContractorAttendance.OTHER else ''
    _check_values(contractor, unlisted_name, trade, trade_remark, headcount, time_in, None)
    if not stored_photo:
        raise AttendanceRefused('Add a group photo of the crew.')
    profile = user.profile
    with transaction.atomic():
        # Asked inside the transaction so the date it compares against is the one stored.
        if not user_can_log_attendance(user, project):
            raise AttendanceRefused(NOT_ALLOWED_TO_LOG)
        today = timezone.localdate()
        if live_duplicate_exists(project, today, contractor, unlisted_name, trade):
            raise AttendanceRefused(DUPLICATE_ENTRY)
        now = timezone.now()
        entry = ContractorAttendance(
            project=project, date=today, contractor=contractor,
            unlisted_contractor_name=unlisted_name, trade=trade, trade_remark=trade_remark,
            headcount=headcount, time_in=time_in, time_out=None,
            photo_file_name=stored_photo['file_name'], photo_bucket=stored_photo['bucket'],
            photo_path=stored_photo['path'], photo_size_kb=stored_photo['file_size_kb'],
            photo_taken_at=photo_taken_at, recorded_by=profile, recorded_at=now,
            updated_at=now,
        )
        _save_or_refuse_duplicate(entry)
        _revision(entry, ContractorAttendanceRevision.CREATED, None, profile, now=now)
    return entry


def update_attendance(entry, data, by):
    """Apply a same-day edit to `entry` by `by` (a User), with an EDITED revision.

    `data` holds contractor, unlisted_name, trade, trade_remark, headcount, time_in,
    time_out, and optionally stored_photo + photo_taken_at (a new photo; absent keeps the
    current one and its taken-at, edge case 11). Date, project and recorder are never
    in `data` and never change. Same-day SE edits carry no note (ruling 6).

    Returns (entry, changed). When the edit changes nothing, no row is written — no
    save, no revision — and changed is False. Raises AttendanceRefused when the entry is
    no longer changeable by `by` (another day, voided, site not live, not theirs), a value
    rule fails, or the new contractor/trade takes a slot already held (DUPLICATE_ENTRY).
    """
    contractor = data.get('contractor')
    unlisted_name = '' if contractor is not None else normalise_unlisted_name(
        data.get('unlisted_name'))
    trade = data.get('trade')
    trade_remark = ((data.get('trade_remark') or '').strip()
                    if trade == ContractorAttendance.OTHER else '')
    stored_photo = data.get('stored_photo')
    with transaction.atomic():
        locked = _lock(entry)
        if not user_can_change_attendance_today(by, locked):
            raise AttendanceRefused(NOT_CHANGEABLE)
        _check_values(contractor, unlisted_name, trade, trade_remark, data.get('headcount'),
                      data.get('time_in'), data.get('time_out'),
                      current_contractor_id=locked.contractor_id)
        before = attendance_snapshot(locked)
        locked.contractor = contractor
        locked.unlisted_contractor_name = unlisted_name
        locked.trade = trade
        locked.trade_remark = trade_remark
        locked.headcount = data.get('headcount')
        locked.time_in = data.get('time_in')
        locked.time_out = data.get('time_out')
        if stored_photo:
            locked.photo_file_name = stored_photo['file_name']
            locked.photo_bucket = stored_photo['bucket']
            locked.photo_path = stored_photo['path']
            locked.photo_size_kb = stored_photo['file_size_kb']
            locked.photo_taken_at = data.get('photo_taken_at')
        after = attendance_snapshot(locked)
        if after == before:
            return locked, False
        locked.updated_at = timezone.now()
        _save_or_refuse_duplicate(locked)
        _revision(locked, ContractorAttendanceRevision.EDITED, before, by.profile,
                  now=locked.updated_at)
    return locked, True


def set_time_out(entry, time_out, by):
    """Record the crew's time out on `entry` by `by` (a User), with a TIME_OUT revision.

    Refused when the entry is not changeable by `by`, a time out is already recorded (a
    second tap, or a replay — the edit form changes one), `time_out` is more than
    FUTURE_TIME_GRACE past now (ATT-B1a), or it is not later than the time in (ruling 4:
    same day, no night shifts). Returns the entry.
    """
    if time_out is None:
        raise AttendanceRefused('Enter the time out.')
    with transaction.atomic():
        locked = _lock(entry)
        if not user_can_change_attendance_today(by, locked):
            raise AttendanceRefused(NOT_CHANGEABLE)
        if locked.time_out is not None:
            raise AttendanceRefused(TIME_OUT_ALREADY)
        if time_is_too_late(time_out):
            raise AttendanceRefused(TIME_LATER_THAN_NOW)
        if time_out <= locked.time_in:
            raise AttendanceRefused(TIME_OUT_NOT_AFTER_IN)
        before = attendance_snapshot(locked)
        locked.time_out = time_out
        locked.updated_at = timezone.now()
        locked.save(update_fields=['time_out', 'updated_at'])
        _revision(locked, ContractorAttendanceRevision.TIME_OUT, before, by.profile,
                  now=locked.updated_at)
    return locked


def void_attendance(entry, note, by):
    """Void `entry` by `by` (a User) with `note`, with a VOIDED revision carrying it.

    A void always says why (ruling 6). The entry stays on record, stops counting, and
    frees its contractor/trade/day slot (ruling 11). Today's entry is voided by whoever
    may change it today; a past day's by a corrector — the site's PM, a coordinator or
    Admin (ATT-B2 ruling 1). Refused when the note is blank or neither applies, which
    includes an entry already voided (no un-void, ATT-B2 ruling 3). Returns the entry.
    """
    note = (note or '').strip()
    if not note:
        raise AttendanceRefused(VOID_NOTE_REQUIRED)
    with transaction.atomic():
        locked = _lock(entry)
        # Today's entry: the site's loggers or its recorder. A past day's: a corrector.
        # Both helpers refuse a voided entry.
        if not (user_can_change_attendance_today(by, locked)
                or user_can_correct_attendance(by, locked)):
            raise AttendanceRefused(NOT_CHANGEABLE)
        before = attendance_snapshot(locked)
        now = timezone.now()
        locked.is_voided = True
        locked.void_note = note
        locked.voided_by = by.profile
        locked.voided_at = now
        locked.updated_at = now
        locked.save(update_fields=['is_voided', 'void_note', 'voided_by', 'voided_at',
                                   'updated_at'])
        _revision(locked, ContractorAttendanceRevision.VOIDED, before, by.profile,
                  note=note, now=now)
    return locked


def correct_attendance(entry, data, by, note):
    """Correct a past day's `entry` by `by` (a User) with `note`, as an EDITED revision
    carrying the note (ATT-B2 rulings 2, 5, 8).

    `data` is update_attendance()'s: contractor, unlisted_name, trade, trade_remark,
    headcount, time_in, time_out, and optionally stored_photo + photo_taken_at (a new
    photo; the old path stays in the revision's `before`, nothing is deleted — ruling 7).
    Date, project and recorder are never in `data` and never change. Times are free of
    the B1a clock (the day is past) but time out is still after time in.

    Returns (entry, changed). When the correction changes nothing, nothing is written
    and changed is False. Raises AttendanceRefused for a blank note, an entry `by` may
    not correct (today's, voided, not a corrector — re-asked under the row lock, so a
    replayed POST or a coordinator just taken off the site is refused), a value rule, or
    a slot already held that day (DUPLICATE_ENTRY_PAST_DAY).
    """
    note = (note or '').strip()
    if not note:
        raise AttendanceRefused(CORRECTION_NOTE_REQUIRED)
    contractor = data.get('contractor')
    unlisted_name = '' if contractor is not None else normalise_unlisted_name(
        data.get('unlisted_name'))
    trade = data.get('trade')
    trade_remark = ((data.get('trade_remark') or '').strip()
                    if trade == ContractorAttendance.OTHER else '')
    stored_photo = data.get('stored_photo')
    with transaction.atomic():
        locked = _lock(entry)
        if not user_can_correct_attendance(by, locked):
            raise AttendanceRefused(NOT_CORRECTABLE)
        _check_values(contractor, unlisted_name, trade, trade_remark, data.get('headcount'),
                      data.get('time_in'), data.get('time_out'),
                      current_contractor_id=locked.contractor_id, day=locked.date)
        # The friendly answer for a correction that moves into a held slot (edge case 1);
        # the partial unique indexes are the backstop for a race.
        if live_duplicate_exists(locked.project, locked.date, contractor, unlisted_name,
                                 trade, exclude_pk=locked.pk):
            raise AttendanceRefused(DUPLICATE_ENTRY_PAST_DAY)
        before = attendance_snapshot(locked)
        locked.contractor = contractor
        locked.unlisted_contractor_name = unlisted_name
        locked.trade = trade
        locked.trade_remark = trade_remark
        locked.headcount = data.get('headcount')
        locked.time_in = data.get('time_in')
        locked.time_out = data.get('time_out')
        if stored_photo:
            locked.photo_file_name = stored_photo['file_name']
            locked.photo_bucket = stored_photo['bucket']
            locked.photo_path = stored_photo['path']
            locked.photo_size_kb = stored_photo['file_size_kb']
            locked.photo_taken_at = data.get('photo_taken_at')
        if attendance_snapshot(locked) == before:
            return locked, False
        locked.updated_at = timezone.now()
        _save_or_refuse_duplicate(locked, duplicate_message=DUPLICATE_ENTRY_PAST_DAY)
        _revision(locked, ContractorAttendanceRevision.EDITED, before, by.profile,
                  note=note, now=locked.updated_at)
    return locked, True


def create_late_attendance(project, day, data, by, note):
    """Record an entry for a missed `day` on `project` by `by` (a User), with a CREATED
    revision carrying `note` (ATT-B2 ruling 4).

    `day` must fall 1-7 days before today (late_entry_window) — never today, which is the
    normal add, and never the future. `data` holds contractor, unlisted_name, trade,
    trade_remark, headcount, time_in, an optional time_out (after time in), stored_photo
    (required; a gallery photo is expected) and photo_taken_at. `by` becomes the
    recorder; "late" is derived from recorded_at (is_late_entry), no field marks it.

    Raises AttendanceRefused for a blank note, a user who is not a corrector on the site,
    a day outside the window, a value rule, or a slot already held that day
    (DUPLICATE_ENTRY_PAST_DAY). Returns the entry.
    """
    note = (note or '').strip()
    if not note:
        raise AttendanceRefused(LATE_NOTE_REQUIRED)
    contractor = data.get('contractor')
    unlisted_name = '' if contractor is not None else normalise_unlisted_name(
        data.get('unlisted_name'))
    trade = data.get('trade')
    trade_remark = ((data.get('trade_remark') or '').strip()
                    if trade == ContractorAttendance.OTHER else '')
    time_in, time_out = data.get('time_in'), data.get('time_out')
    stored_photo = data.get('stored_photo')
    if not stored_photo:
        raise AttendanceRefused('Add a group photo of the crew.')
    profile = by.profile
    with transaction.atomic():
        # Asked inside the transaction, as create_attendance does, so the window is read
        # against the same "today" the row is written under.
        if not user_can_add_late_attendance(by, project):
            raise AttendanceRefused(NOT_ALLOWED_LATE)
        earliest, latest = late_entry_window()
        if day is None or not earliest <= day <= latest:
            raise AttendanceRefused(LATE_DATE_OUT_OF_WINDOW)
        _check_values(contractor, unlisted_name, trade, trade_remark, data.get('headcount'),
                      time_in, time_out, day=day)
        if live_duplicate_exists(project, day, contractor, unlisted_name, trade):
            raise AttendanceRefused(DUPLICATE_ENTRY_PAST_DAY)
        now = timezone.now()
        entry = ContractorAttendance(
            project=project, date=day, contractor=contractor,
            unlisted_contractor_name=unlisted_name, trade=trade, trade_remark=trade_remark,
            headcount=data.get('headcount'), time_in=time_in, time_out=time_out,
            photo_file_name=stored_photo['file_name'], photo_bucket=stored_photo['bucket'],
            photo_path=stored_photo['path'], photo_size_kb=stored_photo['file_size_kb'],
            photo_taken_at=data.get('photo_taken_at'), recorded_by=profile, recorded_at=now,
            updated_at=now,
        )
        _save_or_refuse_duplicate(entry, duplicate_message=DUPLICATE_ENTRY_PAST_DAY)
        _revision(entry, ContractorAttendanceRevision.CREATED, None, profile, note=note,
                  now=now)
    return entry


# ---------------------------------------------------------------------------
# Photo cleanup
# ---------------------------------------------------------------------------

def discard_unrecorded_attendance_photo(stored):
    """Remove a photo uploaded in THIS request for a write that was then refused. Never
    raises: a failed cleanup leaves an orphan object, which is logged, never a failed page.

    Refuses anything outside the bills bucket, and any path an entry or a revision
    snapshot records — so a recorded photo, or one an edit replaced (kept for the
    history, ruling 5), is never reachable from here. Callers pass only the dict their
    own upload returned. Returns True when the object was removed.
    """
    bucket, path = (stored or {}).get('bucket'), (stored or {}).get('path')
    if not bucket or not path or bucket != bills_bucket():
        return False
    if ContractorAttendance.objects.filter(photo_bucket=bucket, photo_path=path).exists():
        return False
    if ContractorAttendanceRevision.objects.filter(
            Q(before__photo_path=path) | Q(after__photo_path=path)).exists():
        return False
    try:
        _client().storage.from_(bucket).remove([path])
        return True
    except Exception as exc:
        logger.error('Attendance photo cleanup failed for %s/%s — %s', bucket, path, exc)
        return False
