"""
Contractor attendance — ATT-B2: past-day corrections, late entries, Summary and History.

WHAT IS PINNED HERE
-------------------
    permission helpers (allowed and refused)        CorrectHelperTests, LateHelperTests,
                                                    HistoryHelperTests, PhotoBulkHelperTests
    correct_attendance, past-day void               CorrectWriterTests, PastVoidTests
    create_late_attendance                          LateWriterTests
    the screens                                     CorrectScreenTests, LateScreenTests
    Summary periods and totals (fixed localdate)    PeriodBoundsTests, SummaryTests
    History: days, chips, voided, range, queries    HistoryPageTests
    edge cases 1-14                                 named in each test's docstring

"Today" is moved by patching django.utils.timezone.localdate, as in tests_attendance.
B2Fixture._past_entry() makes an entry through the same-day writer on an earlier day and
then sets its recorded_at back to that day, so it reads as recorded on time (not late).
Fixtures, storage mocks and the 20:00 IST clock are tests_attendance.AttendanceFixture's.

Run with:
    python manage.py test projects.tests_attendance projects.tests_attendance_b2 --settings=solarpms.test_settings
"""
import re
from datetime import date, datetime, time, timedelta
from unittest import mock

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from . import attendance
from .attendance import (
    CORRECTION_NOTE_REQUIRED, DUPLICATE_ENTRY_PAST_DAY, LATE_DATE_OUT_OF_WINDOW,
    LATE_NOTE_REQUIRED, NOT_ALLOWED_LATE, NOT_CHANGEABLE, NOT_CORRECTABLE, AttendanceRefused,
    correct_attendance, create_late_attendance, is_late_entry, set_time_out,
    update_attendance, void_attendance,
)
from .attendance_views import attendance_summary, period_bounds, summary_queryset
from .models import ContractorAttendance, ContractorAttendanceRevision
from .permissions import (
    attendance_photo_entry_ids, user_can_add_late_attendance, user_can_correct_attendance,
    user_can_view_attendance_history, user_can_view_attendance_photo,
)
from .tests_attendance import AttendanceFixture, _client_for, _messages, _photo


class B2Fixture(AttendanceFixture):

    def setUp(self):
        super().setUp()
        self.today = timezone.localdate()

    def _on(self, day):
        return mock.patch('django.utils.timezone.localdate', return_value=day)

    def _past_entry(self, days_ago, time_out=None, **kwargs):
        """An entry logged on its own day `days_ago` days back, through the same-day
        writers, recorded at 08:05 that day — a normal entry, not a late one."""
        day = self.today - timedelta(days=days_ago)
        with self._on(day):
            entry = self._entry(**kwargs)
            if time_out is not None:
                entry = set_time_out(entry, time_out, (kwargs.get('by') or self.se).user)
        on_day = timezone.make_aware(datetime.combine(day, time(8, 5)))
        ContractorAttendance.objects.filter(pk=entry.pk).update(recorded_at=on_day,
                                                                updated_at=on_day)
        ContractorAttendanceRevision.objects.filter(attendance=entry).update(changed_at=on_day)
        entry.refresh_from_db()
        return entry

    def _data(self, entry, **overrides):
        data = {'contractor': entry.contractor, 'unlisted_name': entry.unlisted_contractor_name,
                'trade': entry.trade, 'trade_remark': entry.trade_remark,
                'headcount': entry.headcount, 'time_in': entry.time_in,
                'time_out': entry.time_out}
        data.update(overrides)
        return data

    def _late(self, days_ago=3, by=None, note='SE phone was dead', **overrides):
        data = {'contractor': self.civil_co, 'unlisted_name': '', 'trade': 'CIVIL',
                'trade_remark': '', 'headcount': 6, 'time_in': time(8, 0), 'time_out': None,
                'stored_photo': self._stored(9), 'photo_taken_at': None}
        data.update(overrides)
        return create_late_attendance(self.site, self.today - timedelta(days=days_ago), data,
                                      (by or self.pm).user, note)

    def _late_post(self, by=None, days_ago=3, photo=True, **overrides):
        data = {'date': (self.today - timedelta(days=days_ago)).isoformat(),
                'contractor': str(self.civil_co.pk), 'trade': 'CIVIL', 'headcount': '6',
                'time_in': '08:00', 'time_out': '', 'photo_taken_at': '',
                'reason': 'SE phone was dead'}
        if photo:
            data['photo'] = _photo()
        data.update(overrides)
        return _client_for(by or self.pm).post(
            reverse('attendance_add_late', args=['ATT-A']), data)

    def _page(self, profile, query=''):
        return _client_for(profile).get(reverse('attendance_today', args=['ATT-A']) + query)


# ---------------------------------------------------------------------------
# Permission helpers
# ---------------------------------------------------------------------------

class CorrectHelperTests(B2Fixture):

    def test_pm_coordinator_and_admin_correct_a_past_entry(self):
        entry = self._past_entry(2)
        for profile in (self.pm, self.coord, self.admin):
            self.assertTrue(user_can_correct_attendance(profile.user, entry), profile.role)

    def test_everyone_else_is_refused(self):
        entry = self._past_entry(2)
        for profile in (self.se, self.se2, self.qaqc, self.stranger, self.finance, self.scm,
                        self.ceo, self.sysadmin, self.bd):
            self.assertFalse(user_can_correct_attendance(profile.user, entry),
                             profile.user.username)

    def test_todays_entry_is_not_a_correction(self):
        entry = self._entry()
        for profile in (self.pm, self.admin):
            self.assertFalse(user_can_correct_attendance(profile.user, entry))

    def test_voided_entry_is_not_correctable(self):
        """Edge case 7."""
        entry = self._past_entry(2)
        void_attendance(entry, 'Wrong site', self.pm.user)
        entry.refresh_from_db()
        self.assertFalse(user_can_correct_attendance(self.pm.user, entry))

    def test_coordinator_taken_off_the_site_loses_it(self):
        """Edge case 4."""
        entry = self._past_entry(2)
        self.site.coordinators.remove(self.coord)
        self.assertFalse(user_can_correct_attendance(self.coord.user, entry))

    def test_any_status_but_soft_deleted(self):
        """Zuber's ruling 4: a Completed site is still corrected; a soft-deleted one is not."""
        entry = self._past_entry(2)
        Project = type(self.site)
        Project.objects.filter(pk=self.site.pk).update(status='Completed')
        entry.refresh_from_db()
        self.assertTrue(user_can_correct_attendance(self.pm.user, entry))
        Project.objects.filter(pk=self.site.pk).update(is_deleted=True)
        entry = ContractorAttendance.objects.select_related('project').get(pk=entry.pk)
        self.assertFalse(user_can_correct_attendance(self.pm.user, entry))
        self.assertFalse(user_can_correct_attendance(self.admin.user, entry))


class LateHelperTests(B2Fixture):

    def test_allowed_and_refused(self):
        for profile in (self.pm, self.coord, self.admin):
            self.assertTrue(user_can_add_late_attendance(profile.user, self.site), profile.role)
        for profile in (self.se, self.finance, self.scm, self.ceo, self.sysadmin, self.bd,
                        self.stranger, self.qaqc):
            self.assertFalse(user_can_add_late_attendance(profile.user, self.site),
                             profile.user.username)

    def test_status_does_not_matter_soft_delete_does(self):
        self.site.status = 'Completed'
        self.site.save(update_fields=['status'])
        self.assertTrue(user_can_add_late_attendance(self.pm.user, self.site))
        self.site.is_deleted = True
        self.site.save(update_fields=['is_deleted'])
        self.assertFalse(user_can_add_late_attendance(self.pm.user, self.site))


class HistoryHelperTests(B2Fixture):

    def test_viewers_see_history(self):
        for profile in (self.pm, self.coord, self.se, self.finance, self.admin, self.qaqc):
            self.assertTrue(user_can_view_attendance_history(profile.user, self.site))
        self.assertFalse(user_can_view_attendance_history(self.stranger.user, self.site))

    def test_ruling_f_recorder_does_not(self):
        """Edge case 12 / ruling 13."""
        self._entry(by=self.se)
        self.se_task.assigned_to = self.se2
        self.se_task.save(update_fields=['assigned_to'])
        self.assertFalse(user_can_view_attendance_history(self.se.user, self.site))


class PhotoBulkHelperTests(B2Fixture):

    def test_bulk_matches_the_per_entry_helper(self):
        entries = [self._past_entry(2, by=self.se), self._past_entry(3, by=self.se2),
                   self._past_entry(4, by=self.pm)]
        for profile in (self.se, self.se2, self.pm, self.coord, self.finance, self.scm,
                        self.admin, self.ceo, self.sysadmin, self.bd, self.qaqc):
            expected = {e.pk for e in entries
                        if user_can_view_attendance_photo(profile.user, e)}
            self.assertEqual(attendance_photo_entry_ids(profile.user, self.site, entries),
                             expected, profile.user.username)


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------

class CorrectWriterTests(B2Fixture):

    def test_correction_writes_edited_with_the_note(self):
        entry = self._past_entry(2)
        entry, changed = correct_attendance(entry, self._data(entry, headcount=9), self.pm.user,
                                            '  Count was 9, SE typed 7  ')
        self.assertTrue(changed)
        self.assertEqual(entry.headcount, 9)
        rev = entry.revisions.order_by('pk').last()
        self.assertEqual(rev.action, 'EDITED')
        self.assertEqual(rev.note, 'Count was 9, SE typed 7')
        self.assertEqual(rev.changed_by, self.pm)
        self.assertEqual((rev.before['headcount'], rev.after['headcount']), (7, 9))

    def test_blank_or_spaces_note_is_refused(self):
        """Edge case 2."""
        entry = self._past_entry(2)
        for note in ('', '   ', None):
            with self.assertRaisesMessage(AttendanceRefused, CORRECTION_NOTE_REQUIRED):
                correct_attendance(entry, self._data(entry, headcount=9), self.pm.user, note)
        entry.refresh_from_db()
        self.assertEqual(entry.headcount, 7)
        self.assertEqual(self._actions(entry), ['CREATED'])

    def test_nothing_changed_writes_a_confirmation_only(self):
        """ATT-B3 ruling 10: a no-change correction is now a CONFIRMED revision."""
        entry = self._past_entry(2)
        entry, changed = correct_attendance(entry, self._data(entry), self.pm.user, 'Checked')
        self.assertFalse(changed)
        self.assertEqual(self._actions(entry), ['CREATED', 'CONFIRMED'])

    def test_site_engineer_is_refused(self):
        """Edge case 3 (the writer, whatever the view did)."""
        entry = self._past_entry(2)
        with self.assertRaisesMessage(AttendanceRefused, NOT_CORRECTABLE):
            correct_attendance(entry, self._data(entry, headcount=9), self.se.user, 'Mine')

    def test_todays_entry_is_refused(self):
        entry = self._entry()
        with self.assertRaisesMessage(AttendanceRefused, NOT_CORRECTABLE):
            correct_attendance(entry, self._data(entry, headcount=9), self.pm.user, 'Fix')

    def test_voided_entry_is_refused(self):
        """Edge case 7."""
        entry = self._past_entry(2)
        void_attendance(entry, 'Wrong crew', self.pm.user)
        with self.assertRaisesMessage(AttendanceRefused, NOT_CORRECTABLE):
            correct_attendance(entry, self._data(entry, headcount=9), self.pm.user, 'Fix')
        with self.assertRaisesMessage(AttendanceRefused, NOT_CHANGEABLE):
            void_attendance(entry, 'Again', self.pm.user)

    def test_move_into_a_held_slot_is_a_duplicate(self):
        """Edge case 1: changing trade or contractor into a slot held that day."""
        civil = self._past_entry(2, trade='CIVIL')
        self._past_entry(2, trade='ELECTRICAL', n=2)
        both = self._past_entry(2, contractor=self.both_co, trade='CIVIL', n=3)
        for entry, change in ((civil, {'trade': 'ELECTRICAL'}),
                              (both, {'contractor': self.civil_co})):
            with self.assertRaisesMessage(AttendanceRefused, DUPLICATE_ENTRY_PAST_DAY):
                correct_attendance(entry, self._data(entry, **change), self.pm.user, 'Move')
            entry.refresh_from_db()
            self.assertEqual(self._actions(entry), ['CREATED'])
        civil.refresh_from_db()
        self.assertEqual(civil.trade, 'CIVIL')

    def test_date_project_and_recorder_never_change(self):
        """Ruling 5: they are not in `data`; a stray key is ignored."""
        entry = self._past_entry(2)
        day, recorder = entry.date, entry.recorded_by_id
        entry, _ = correct_attendance(
            entry, self._data(entry, headcount=8, date=self.today, project=self.res,
                              recorded_by=self.pm), self.pm.user, 'Fix count')
        entry.refresh_from_db()
        self.assertEqual((entry.date, entry.project_id, entry.recorded_by_id),
                         (day, self.site.pk, recorder))

    def test_past_open_entry_time_out_has_no_clock_check(self):
        """Edge case 10: a time after "now" is fine on a past day; still after time in."""
        self.clock.return_value = timezone.make_aware(
            datetime.combine(self.today, time(9, 0)))
        entry = self._past_entry(2)
        self.assertIsNone(entry.time_out)
        entry, changed = correct_attendance(entry, self._data(entry, time_out=time(18, 30)),
                                            self.pm.user, 'Left 18:30 per the contractor')
        self.assertTrue(changed)
        self.assertEqual(entry.time_out, time(18, 30))
        with self.assertRaisesMessage(AttendanceRefused, attendance.TIME_OUT_NOT_AFTER_IN):
            correct_attendance(entry, self._data(entry, time_out=time(7, 0)), self.pm.user,
                               'Wrong')

    def test_today_entry_keeps_the_b1a_clock(self):
        """Ruling 6: the future-time rule still binds today's same-day edit."""
        self.clock.return_value = timezone.make_aware(
            datetime.combine(self.today, time(10, 0)))
        entry = self._entry()
        with self.assertRaisesMessage(AttendanceRefused, attendance.TIME_LATER_THAN_NOW):
            update_attendance(entry, self._data(entry, time_out=time(11, 0)), self.pm.user)

    def test_photo_replaced_old_path_kept_nothing_deleted(self):
        """Edge case 11."""
        entry = self._past_entry(2)
        old_path = entry.photo_path
        entry, _ = correct_attendance(entry, self._data(entry, stored_photo=self._stored(5),
                                                        photo_taken_at=None),
                                      self.pm.user, 'Clearer photo from the contractor')
        rev = entry.revisions.order_by('pk').last()
        self.assertEqual(rev.before['photo_path'], old_path)
        self.assertEqual(rev.after['photo_path'], self._stored(5)['path'])
        self.discard_client.return_value.storage.from_.return_value.remove.assert_not_called()

    def test_entry_at_2359_corrected_at_0005_is_a_past_day_correction(self):
        """Edge case 14."""
        self.clock.return_value = timezone.make_aware(
            datetime.combine(self.today, time(23, 59)))
        entry = self._entry(time_in=time(23, 0))
        tomorrow = self.today + timedelta(days=1)
        with self._on(tomorrow):
            self.clock.return_value = timezone.make_aware(datetime.combine(tomorrow, time(0, 5)))
            with self.assertRaisesMessage(AttendanceRefused, NOT_CHANGEABLE):
                update_attendance(entry, self._data(entry, time_out=time(23, 59)), self.pm.user)
            with self.assertRaisesMessage(AttendanceRefused, CORRECTION_NOTE_REQUIRED):
                correct_attendance(entry, self._data(entry, time_out=time(23, 59)),
                                   self.pm.user, '')
            entry, changed = correct_attendance(entry, self._data(entry, time_out=time(23, 59)),
                                                self.pm.user, 'Crew left at 23:59')
        self.assertTrue(changed)
        self.assertEqual(self._actions(entry), ['CREATED', 'EDITED'])

    def test_admin_with_no_assignment_corrects(self):
        """Edge case 13."""
        entry = self._past_entry(2)
        entry, changed = correct_attendance(entry, self._data(entry, headcount=3),
                                            self.admin.user, 'Contractor register')
        self.assertTrue(changed)
        self.assertEqual(entry.revisions.order_by('pk').last().changed_by, self.admin)


class PastVoidTests(B2Fixture):

    def test_correctors_void_a_past_entry_with_a_note(self):
        for n, profile in enumerate((self.pm, self.coord, self.admin), start=1):
            entry = self._past_entry(n)
            entry = void_attendance(entry, 'Crew was not on site', profile.user)
            self.assertTrue(entry.is_voided)
            rev = entry.revisions.order_by('pk').last()
            self.assertEqual((rev.action, rev.note), ('VOIDED', 'Crew was not on site'))

    def test_site_engineer_and_blank_note_are_refused(self):
        entry = self._past_entry(2)
        with self.assertRaisesMessage(AttendanceRefused, NOT_CHANGEABLE):
            void_attendance(entry, 'Mine', self.se.user)
        with self.assertRaisesMessage(AttendanceRefused, attendance.VOID_NOTE_REQUIRED):
            void_attendance(entry, '   ', self.pm.user)
        entry.refresh_from_db()
        self.assertFalse(entry.is_voided)


class LateWriterTests(B2Fixture):

    def test_late_entry_created_with_its_note(self):
        entry = self._late(days_ago=3, time_out=time(17, 0))
        self.assertEqual(entry.date, self.today - timedelta(days=3))
        self.assertEqual((entry.recorded_by, entry.time_out), (self.pm, time(17, 0)))
        self.assertTrue(is_late_entry(entry))
        rev = entry.revisions.get()
        self.assertEqual((rev.action, rev.note, rev.before), ('CREATED', 'SE phone was dead',
                                                              None))

    def test_window_is_one_to_seven_days_back(self):
        """Edge case 5."""
        for days_ago in (0, 8, -1):
            with self.assertRaisesMessage(AttendanceRefused, LATE_DATE_OUT_OF_WINDOW):
                self._late(days_ago=days_ago)
        self._late(days_ago=1)
        self._late(days_ago=7, trade='ELECTRICAL')
        self.assertEqual(ContractorAttendance.objects.count(), 2)

    def test_duplicate_of_a_live_entry_is_refused(self):
        """Edge case 6; a voided entry in the slot does not block."""
        existing = self._past_entry(3)
        with self.assertRaisesMessage(AttendanceRefused, DUPLICATE_ENTRY_PAST_DAY):
            self._late(days_ago=3)
        void_attendance(existing, 'Wrong contractor', self.pm.user)
        self._late(days_ago=3)
        self.assertEqual(ContractorAttendance.objects.filter(is_voided=False).count(), 1)

    def test_note_permission_photo_and_times(self):
        """Edge case 2 for late entries, plus who and what."""
        for note in ('', '  '):
            with self.assertRaisesMessage(AttendanceRefused, LATE_NOTE_REQUIRED):
                self._late(note=note)
        with self.assertRaisesMessage(AttendanceRefused, NOT_ALLOWED_LATE):
            self._late(by=self.se)
        with self.assertRaisesMessage(AttendanceRefused, 'Add a group photo'):
            self._late(stored_photo=None)
        with self.assertRaisesMessage(AttendanceRefused, attendance.TIME_OUT_NOT_AFTER_IN):
            self._late(time_out=time(7, 0))
        self.assertFalse(ContractorAttendance.objects.exists())

    def test_admin_adds_late_and_completed_site_allowed(self):
        """Edge case 13 and Zuber's ruling 4."""
        self.site.status = 'Completed'
        self.site.save(update_fields=['status'])
        entry = self._late(by=self.admin)
        self.assertEqual(entry.recorded_by, self.admin)

    def test_unlisted_name_is_normalised(self):
        entry = self._late(contractor=None, unlisted_name='  Raju   Civil ')
        self.assertEqual(entry.unlisted_contractor_name, 'Raju Civil')


# ---------------------------------------------------------------------------
# Screens
# ---------------------------------------------------------------------------

class CorrectScreenTests(B2Fixture):

    def _url(self, entry):
        return reverse('attendance_correct', args=['ATT-A', entry.pk])

    def test_pm_gets_the_correction_form(self):
        entry = self._past_entry(2)
        response = _client_for(self.pm).get(self._url(entry))
        self.assertContains(response, 'Correct entry')
        self.assertContains(response, 'Reason for correction')
        self.assertContains(response, 'the date cannot be changed')
        self.assertContains(response, 'name="time_out"')
        self.assertContains(response, 'Void this entry')
        self.assertNotContains(response, 'name="date"')

    def test_reason_required_then_saved_and_shown_in_history(self):
        """Browser checklist 1."""
        entry = self._past_entry(2)
        client = _client_for(self.pm)
        response = client.post(self._url(entry), self._edit_data(entry, headcount='9',
                                                                 reason='   '))
        self.assertContains(response, CORRECTION_NOTE_REQUIRED)
        entry.refresh_from_db()
        self.assertEqual(entry.headcount, 7)
        response = client.post(self._url(entry), self._edit_data(entry, headcount='9',
                                                                 reason='Count was 9'))
        self.assertEqual(response.status_code, 302)
        entry.refresh_from_db()
        self.assertEqual(entry.headcount, 9)
        page = client.get(self._url(entry))
        self.assertContains(page, 'Headcount: 7 → 9')
        self.assertContains(page, 'Count was 9')

    def test_site_engineer_is_refused_by_url_and_replay(self):
        """Edge case 3."""
        entry = self._past_entry(2)
        client = _client_for(self.se)
        response = client.get(self._url(entry))
        self.assertEqual(response.status_code, 302)
        self.assertIn(NOT_CORRECTABLE, _messages(response))
        client.post(self._url(entry), self._edit_data(entry, headcount='12', reason='Mine'))
        entry.refresh_from_db()
        self.assertEqual(entry.headcount, 7)
        self.assertEqual(self._actions(entry), ['CREATED'])

    def test_non_viewer_gets_404(self):
        entry = self._past_entry(2)
        response = _client_for(self.stranger).get(self._url(entry))
        self.assertEqual(response.status_code, 404)

    def test_voided_entry_no_correct_no_void(self):
        """Edge case 7 through the URLs."""
        entry = self._past_entry(2)
        client = _client_for(self.pm)
        client.post(reverse('attendance_void', args=['ATT-A', entry.pk]),
                    {'void_note': 'Not our crew'})
        entry.refresh_from_db()
        self.assertTrue(entry.is_voided)
        response = client.get(self._url(entry))
        self.assertIn(NOT_CORRECTABLE, _messages(response))
        response = client.post(reverse('attendance_void', args=['ATT-A', entry.pk]),
                               {'void_note': 'Again'})
        self.assertIn(NOT_CHANGEABLE, _messages(response))
        self.assertEqual(self._actions(entry), ['CREATED', 'VOIDED'])

    def test_se_cannot_void_past_by_url(self):
        entry = self._past_entry(2)
        _client_for(self.se).post(reverse('attendance_void', args=['ATT-A', entry.pk]),
                                  {'void_note': 'Mine'})
        entry.refresh_from_db()
        self.assertFalse(entry.is_voided)

    def test_blank_past_void_returns_to_the_correction_page(self):
        entry = self._past_entry(2)
        response = _client_for(self.pm).post(
            reverse('attendance_void', args=['ATT-A', entry.pk]), {'void_note': ' '})
        self.assertRedirects(response, self._url(entry), fetch_redirect_response=False)

    def test_duplicate_correction_message(self):
        """Edge case 1 through the screen."""
        entry = self._past_entry(2, trade='CIVIL')
        self._past_entry(2, trade='ELECTRICAL', n=2)
        response = _client_for(self.pm).post(self._url(entry), self._edit_data(
            entry, trade='ELECTRICAL', reason='Was electrical'))
        self.assertContains(response, DUPLICATE_ENTRY_PAST_DAY)

    def test_admin_corrects_but_cannot_add_today(self):
        """Edge case 13."""
        entry = self._past_entry(2)
        client = _client_for(self.admin)
        self.assertEqual(client.get(self._url(entry)).status_code, 200)
        self.assertEqual(client.get(reverse('attendance_add', args=['ATT-A'])).status_code, 403)


class LateScreenTests(B2Fixture):

    def test_form_has_window_reason_and_no_camera_capture(self):
        response = _client_for(self.pm).get(reverse('attendance_add_late', args=['ATT-A']))
        self.assertContains(response, 'Add late entry')
        self.assertContains(response, 'Why is this entry late?')
        self.assertContains(response, f'min="{(self.today - timedelta(days=7)).isoformat()}"')
        self.assertContains(response, f'max="{(self.today - timedelta(days=1)).isoformat()}"')
        self.assertNotContains(response, 'capture=')
        self.assertContains(response, 'name="time_out"')

    def test_same_day_add_keeps_the_camera(self):
        response = _client_for(self.se).get(reverse('attendance_add', args=['ATT-A']))
        self.assertContains(response, 'capture="environment"')

    def test_who_reaches_it(self):
        url = reverse('attendance_add_late', args=['ATT-A'])
        self.assertEqual(_client_for(self.se).get(url).status_code, 403)
        self.assertEqual(_client_for(self.finance).get(url).status_code, 403)
        self.assertEqual(_client_for(self.stranger).get(url).status_code, 404)
        self.assertEqual(_client_for(self.admin).get(url).status_code, 200)
        self.assertEqual(_client_for(self.coord).get(url).status_code, 200)

    def test_saved_with_chip_and_window_refused(self):
        """Browser checklist 3; edge case 5 through the form."""
        response = self._late_post(days_ago=8)
        self.assertContains(response, LATE_DATE_OUT_OF_WINDOW)
        response = self._late_post(days_ago=0)
        self.assertContains(response, LATE_DATE_OUT_OF_WINDOW)
        self.assertFalse(ContractorAttendance.objects.exists())
        response = self._late_post(days_ago=3, time_out='17:00')
        self.assertEqual(response.status_code, 302)
        entry = ContractorAttendance.objects.get()
        self.assertEqual((entry.date, entry.time_out), (self.today - timedelta(days=3),
                                                        time(17, 0)))
        self.assertEqual(entry.revisions.get().note, 'SE phone was dead')
        page = self._page(self.pm)
        self.assertContains(page, 'att-late-chip')

    def test_reason_required_and_duplicate(self):
        """Edge cases 2 and 6 through the form."""
        response = self._late_post(reason='  ')
        self.assertContains(response, LATE_NOTE_REQUIRED)
        self._past_entry(3)
        response = self._late_post(days_ago=3)
        self.assertContains(response, DUPLICATE_ENTRY_PAST_DAY)
        self.upload_client.return_value.storage.from_.return_value.upload.assert_not_called()
        self.assertEqual(ContractorAttendance.objects.count(), 1)

    def test_time_out_after_time_in(self):
        response = self._late_post(time_out='07:00')
        self.assertContains(response, 'Time out must be later than time in')
        self.assertFalse(ContractorAttendance.objects.exists())


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

class PeriodBoundsTests(B2Fixture):
    """Edge case 8, with a fixed localdate."""

    def test_weeks_run_monday_to_sunday(self):
        wednesday = date(2026, 10, 14)
        self.assertEqual(period_bounds('this_week', wednesday),
                         (date(2026, 10, 12), wednesday))
        self.assertEqual(period_bounds('last_week', wednesday),
                         (date(2026, 10, 5), date(2026, 10, 11)))
        sunday, monday = date(2026, 10, 11), date(2026, 10, 12)
        self.assertEqual(period_bounds('this_week', sunday)[0], date(2026, 10, 5))
        self.assertEqual(period_bounds('this_week', monday), (monday, monday))
        self.assertEqual(period_bounds('last_week', monday),
                         (date(2026, 10, 5), date(2026, 10, 11)))

    def test_months(self):
        first = date(2026, 10, 1)
        self.assertEqual(period_bounds('this_month', first), (first, first))
        self.assertEqual(period_bounds('last_month', first),
                         (date(2026, 9, 1), date(2026, 9, 30)))
        self.assertEqual(period_bounds('last_month', date(2026, 3, 15)),
                         (date(2026, 2, 1), date(2026, 2, 28)))
        self.assertEqual(period_bounds('last_month', date(2026, 1, 9)),
                         (date(2025, 12, 1), date(2025, 12, 31)))

    def test_unknown_period_is_this_week(self):
        self.assertEqual(period_bounds('bogus', date(2026, 10, 14)),
                         period_bounds('this_week', date(2026, 10, 14)))


class SummaryTests(B2Fixture):
    """Ruling 10, edge cases 8 and 9. "Today" is Thursday 1 Oct 2026: this week is
    Mon 28 Sep - Thu 1 Oct, last week Mon 21 - Sun 27 Sep, last month September."""

    TODAY = date(2026, 10, 1)

    def _on_day(self, day, **kwargs):
        with self._on(day):
            return self._entry(**kwargs)

    def setUp(self):
        super().setUp()
        self._on_day(date(2026, 9, 27), headcount=5)                    # Sunday: last week
        self._on_day(date(2026, 9, 28), headcount=7, n=2)               # Monday: this week
        self._on_day(date(2026, 10, 1), headcount=4, n=3)               # the 1st: this month
        voided = self._on_day(date(2026, 9, 30), trade='ELECTRICAL', headcount=3, n=4)
        with self._on(date(2026, 9, 30)):
            void_attendance(voided, 'Wrong crew', self.pm.user)
        self._on_day(date(2026, 9, 29), contractor=None, unlisted='Raju Civil', headcount=2, n=5)
        self._on_day(date(2026, 9, 30), contractor=None, unlisted='  RAJU   civil ', headcount=6,
                     n=6)
        self._on_day(date(2026, 10, 1), contractor=self.both_co, trade='ELECTRICAL',
                     headcount=1, n=7)
        self.both_co.is_active = False
        self.both_co.save(update_fields=['is_active'])

    def _summary(self, period):
        return attendance_summary(self.site, *period_bounds(period, self.TODAY))

    def _rows(self, summary):
        # A not-listed row shows one of its typed spellings (Min(), which differs by
        # collation between SQLite and Postgres), so it is compared case-blind.
        return [(r['name'] if r['listed'] else r['name'].lower(), r['trade'], r['days'],
                 r['man_days'], r['listed'])
                for r in summary['rows']]

    def test_this_week(self):
        s = self._summary('this_week')
        self.assertEqual(self._rows(s), [
            ('Kumar Electricals', 'Electrical', 1, 1, True),
            ('Sharma Civil Works', 'Civil', 2, 11, True),
            ('raju civil', 'Civil', 2, 8, False),
        ])
        self.assertEqual((s['billable'], s['waiting']), (12, 8))

    def test_last_week_has_the_sunday_only(self):
        s = self._summary('last_week')
        self.assertEqual(self._rows(s), [('Sharma Civil Works', 'Civil', 1, 5, True)])
        self.assertEqual((s['billable'], s['waiting']), (5, 0))

    def test_this_month_starts_on_the_1st(self):
        s = self._summary('this_month')
        self.assertEqual(self._rows(s), [
            ('Kumar Electricals', 'Electrical', 1, 1, True),
            ('Sharma Civil Works', 'Civil', 1, 4, True),
        ])
        self.assertEqual((s['billable'], s['waiting']), (5, 0))

    def test_last_month(self):
        s = self._summary('last_month')
        self.assertEqual(self._rows(s), [
            ('Sharma Civil Works', 'Civil', 2, 12, True),
            ('raju civil', 'Civil', 2, 8, False),
        ])
        self.assertEqual((s['billable'], s['waiting']), (12, 8))

    def test_one_grouped_query(self):
        with CaptureQueriesContext(connection) as ctx:
            self._summary('this_week')
        self.assertEqual(len(ctx.captured_queries), 1)
        self.assertIn('GROUP BY', ctx.captured_queries[0]['sql'])

    def test_page_switches_period_and_shows_no_photo_links(self):
        """Browser checklist 6."""
        with self._on(self.TODAY):
            for period, billable in (('this_week', 12), ('last_week', 5), ('this_month', 5),
                                     ('last_month', 12)):
                page = self._page(self.pm, f'?period={period}')
                html = page.content.decode()
                self.assertIn(f'<td class="text-end" id="att-billable">{billable}</td>', html)
                summary = html[html.index('id="att-summary"'):html.index('id="att-history"')]
                self.assertNotIn('/photo/', summary)
            page = self._page(self.pm)
            self.assertContains(page, 'id="att-waiting">8<')
            self.assertContains(page, 'Waiting for SCM mapping')


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------

class HistoryPageTests(B2Fixture):

    def test_days_counts_chips_and_voided(self):
        """Browser checklist 7; ruling 12."""
        closed = self._past_entry(2, time_out=time(17, 30))
        open_one = self._past_entry(2, trade='ELECTRICAL', n=2)
        voided = self._past_entry(2, trade='OTHER', remark='Painting', n=3)
        void_attendance(voided, 'Duplicate', self.pm.user)
        page = self._page(self.pm)
        html = page.content.decode()
        day = (self.today - timedelta(days=2)).isoformat()
        self.assertIn(f'id="att-day-{day}"', html)
        self.assertIn('2 crews · 14 people', html)
        self.assertIn('1 left open', html)
        self.assertIn('att-left-open-chip', html)
        self.assertNotIn('att-late-chip', html)
        self.assertRegex(html, r'<div x-show="showVoided" x-cloak>\s*'
                               r'(\{%.*?%\}\s*)?<div class="card[^"]*att-voided[^"]*"[^>]*'
                               rf'id="att-entry-{voided.pk}"')
        self.assertIn(reverse('attendance_correct', args=['ATT-A', closed.pk]), html)
        self.assertIn(reverse('attendance_correct', args=['ATT-A', open_one.pk]), html)
        self.assertNotIn(reverse('attendance_correct', args=['ATT-A', voided.pk]), html)
        self.assertIn(reverse('attendance_add_late', args=['ATT-A']), html)

    def test_site_engineer_sees_locked_cards(self):
        """Browser checklist 4."""
        entry = self._past_entry(2)
        page = self._page(self.se)
        self.assertContains(page, 'Locked. Ask the PM to correct.')
        self.assertNotContains(page, reverse('attendance_correct', args=['ATT-A', entry.pk]))
        self.assertNotContains(page, reverse('attendance_add_late', args=['ATT-A']))

    def test_range_30_then_90_days(self):
        recent = self._past_entry(30)
        older = self._past_entry(31, n=2)
        oldest = self._past_entry(91, n=3)
        page = self._page(self.pm).content.decode()
        self.assertIn(f'att-entry-{recent.pk}', page)
        self.assertNotIn(f'att-entry-{older.pk}', page)
        self.assertIn('att-show-older', page)
        page = self._page(self.pm, '?older=1').content.decode()
        self.assertIn(f'att-entry-{older.pk}', page)
        self.assertNotIn(f'att-entry-{oldest.pk}', page)
        self.assertNotIn('id="att-show-older"', page)

    def test_ruling_f_recorder_sees_today_only(self):
        """Edge case 12 / browser checklist 8: parameters ignored."""
        self._past_entry(2, by=self.se)
        self._entry(by=self.se, n=2)
        self.se_task.assigned_to = self.se2
        self.se_task.save(update_fields=['assigned_to'])
        page = self._page(self.se, '?period=last_month&older=1')
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, 'id="att-summary"')
        self.assertNotContains(page, 'id="att-history"')

    def test_query_count_does_not_grow_with_entries(self):
        """The History cards cost no query each: a coordinator (the dearest viewer — the
        photo and correct checks both reach the coordinator lookup) pays the same for 2
        past entries as for 8."""
        for n in range(1, 3):
            self._past_entry(n, n=n)
        with CaptureQueriesContext(connection) as small:
            self._page(self.coord)
        for n in range(3, 9):
            self._past_entry(n, n=n)
        with CaptureQueriesContext(connection) as large:
            self._page(self.coord)
        self.assertEqual(len(small.captured_queries), len(large.captured_queries))

    def test_history_shows_correction_date_and_note(self):
        entry = self._past_entry(2)
        correct_attendance(entry, self._data(entry, headcount=9), self.pm.user, 'Fixed count')
        page = self._page(self.se)
        self.assertContains(page, '“Fixed count”')
        self.assertContains(page, timezone.localtime().strftime('%d %b'))
