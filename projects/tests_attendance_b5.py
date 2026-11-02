"""
Contractor attendance — ATT-B5: the group photo is optional.

WHAT IS PINNED HERE
-------------------
    migration 0129's CHECK: all three photo columns or none     PhotoColumnsTests
    the writers: no photo saves blank, photo_taken_at dropped   WriterTests
    same-day add / edit, correction, late entry without photo   AddTests, AddLaterTests,
                                                                LateTests
    the card: "No photo" chip, no thumbnail, no time line       CardTests
    the photo link without a photo (404, not 500)               CardTests
    photo-time flags never on an entry without a photo          FlagTests
    Summary "of which without photo"                            SummaryTests
    storage off: Save stays, a posted photo is still refused    StorageOffTests
    edge cases 1-10                                             named in each test's docstring

Fixtures, storage mocks and the 20:00 IST clock are tests_attendance.AttendanceFixture's;
past entries and the page helper come from tests_attendance_b2.B2Fixture.

Run with:
    python manage.py test projects.tests_attendance_b5 --settings=solarpms.test_settings
"""
from datetime import datetime, time, timedelta
from unittest import mock

from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

from .attendance import (
    ATTENDANCE_STORAGE_OFF, CORRECTION_NOTE_REQUIRED, attendance_snapshot, create_attendance,
    photo_time_flags, update_attendance, void_attendance,
)
from .attendance_views import attendance_summary
from .models import ContractorAttendance
from .tests_attendance import BUCKET, _client_for, _photo
from .tests_attendance_b2 import B2Fixture


class B5Fixture(B2Fixture):

    def _bare(self, days_ago=0, by=None, contractor='default', unlisted='', trade='CIVIL',
              headcount=7):
        """An entry saved without a photo through the same-day writer — today, or on an
        earlier day recorded at 08:05 that day (B2Fixture._past_entry's shape)."""
        if contractor == 'default':
            contractor = self.civil_co
        day = self.today - timedelta(days=days_ago)
        with self._on(day):
            entry = create_attendance(
                self.site, (by or self.se).user, contractor=contractor, unlisted_name=unlisted,
                trade=trade, trade_remark='', headcount=headcount, time_in=time(8, 0),
                stored_photo=None, photo_taken_at=None)
        if days_ago:
            on_day = timezone.make_aware(datetime.combine(day, time(8, 5)))
            ContractorAttendance.objects.filter(pk=entry.pk).update(recorded_at=on_day,
                                                                    updated_at=on_day)
            entry.revisions.update(changed_at=on_day)
            entry.refresh_from_db()
        return entry

    def _exif(self, day=None, hour=8):
        """A browser-read EXIF time as the page posts it."""
        return f'{(day or self.today):%Y:%m:%d} {hour:02d}:00:00'

    def _assert_no_photo(self, entry):
        entry.refresh_from_db()
        self.assertEqual((entry.photo_file_name, entry.photo_bucket, entry.photo_path,
                          entry.photo_size_kb, entry.photo_taken_at), ('', '', '', 0, None))


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

class PhotoColumnsTests(B5Fixture):

    def _row(self, **overrides):
        values = dict(
            project=self.site, date=self.today, contractor=self.civil_co,
            unlisted_contractor_name='', trade='CIVIL', trade_remark='', headcount=5,
            time_in=time(8, 0), photo_file_name='crew.jpg', photo_bucket=BUCKET,
            photo_path='p/attendance/x.jpg', recorded_by=self.se)
        values.update(overrides)
        return ContractorAttendance(**values)

    def test_all_blank_saves(self):
        self._row(photo_file_name='', photo_bucket='', photo_path='').save()

    def test_half_filled_photo_is_refused(self):
        """Ruling (e): the three columns are one fact — any one or two blank is refused."""
        for blank in (('photo_path',), ('photo_bucket',), ('photo_file_name',),
                      ('photo_path', 'photo_bucket'), ('photo_path', 'photo_file_name'),
                      ('photo_bucket', 'photo_file_name')):
            with self.assertRaises(IntegrityError, msg=blank), transaction.atomic():
                self._row(**{name: '' for name in blank}).save()
        self.assertFalse(ContractorAttendance.objects.exists())

    def test_model_defaults_are_blank(self):
        entry = ContractorAttendance()
        self.assertEqual((entry.photo_file_name, entry.photo_bucket, entry.photo_path),
                         ('', '', ''))


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------

class WriterTests(B5Fixture):

    def test_create_without_photo_drops_photo_taken_at(self):
        """A taken-at time with no photo describes nothing; it is not stored."""
        entry = create_attendance(
            self.site, self.se.user, contractor=self.civil_co, unlisted_name='',
            trade='CIVIL', trade_remark='', headcount=4, time_in=time(8, 0),
            stored_photo=None, photo_taken_at=timezone.now())
        self._assert_no_photo(entry)

    def test_snapshot_of_an_entry_without_photo_is_blank_strings(self):
        """Bug pre-emption: an empty photo snapshots as '', not a crash."""
        entry = self._bare()
        created = entry.revisions.get()
        for key in ('photo_file_name', 'photo_bucket', 'photo_path'):
            self.assertEqual(created.after[key], '', key)
        self.assertIsNone(created.after['photo_taken_at'])
        self.assertEqual(attendance_snapshot(entry)['photo_path'], '')

    def test_late_writer_without_photo(self):
        entry = self._late(stored_photo=None, photo_taken_at=timezone.now())
        self._assert_no_photo(entry)
        self.assertEqual(entry.revisions.get().note, 'SE phone was dead')

    def test_edit_without_photo_keeps_the_existing_one(self):
        """Edge case 10, writer half: no stored_photo never blanks a recorded photo."""
        entry = self._entry()
        path = entry.photo_path
        entry, changed = update_attendance(entry, self._data(entry, headcount=9), self.se.user)
        self.assertTrue(changed)
        entry.refresh_from_db()
        self.assertEqual(entry.photo_path, path)


# ---------------------------------------------------------------------------
# Screens
# ---------------------------------------------------------------------------

class AddTests(B5Fixture):

    def test_add_without_photo_saves_with_chip(self):
        """Edge case 1 / checklist 1."""
        response = self._add_post(photo=None, photo_taken_at=self._exif())
        self.assertEqual(response.status_code, 302)
        entry = ContractorAttendance.objects.get()
        self._assert_no_photo(entry)
        self.upload_client.return_value.storage.from_.return_value.upload.assert_not_called()
        self.assertContains(self._page(self.se), 'att-no-photo-chip')

    def test_add_form_photo_is_optional(self):
        response = _client_for(self.se).get(reverse('attendance_add', args=['ATT-A']))
        self.assertContains(response, 'Group photo <span class="small fw-normal text-muted">(optional)</span>')
        self.assertContains(response, 'capture="environment"')
        content = response.content.decode()
        photo_input = content[content.index('id="att-photo"'):]
        photo_input = photo_input[:photo_input.index('>')]
        self.assertNotIn('required', photo_input)

    def test_invalid_file_is_still_refused(self):
        """Edge case 2 / checklist 5: a PDF, and a file over the size ceiling."""
        response = self._add_post(photo=_photo('bill.pdf', b'%PDF-1.4', 'application/pdf'))
        self.assertContains(response, 'Photo rejected')
        with mock.patch('projects.views.MAX_FILE_SIZE_BYTES', 10):
            response = self._add_post(photo=_photo('big.jpg'))
        self.assertContains(response, 'exceeds 20 MB limit')
        self.assertFalse(ContractorAttendance.objects.exists())


class AddLaterTests(B5Fixture):

    def test_se_adds_a_photo_the_same_day(self):
        """Edge case 3 / checklist 2."""
        entry = self._bare()
        client = _client_for(self.se)
        url = reverse('attendance_edit', args=['ATT-A', entry.pk])
        self.assertContains(client.get(url), 'No photo yet — add one if you have it.')
        response = client.post(url, self._edit_data(entry, photo=_photo('later.jpg')))
        self.assertEqual(response.status_code, 302)
        entry.refresh_from_db()
        self.assertNotEqual(entry.photo_path, '')
        self.assertEqual(entry.photo_bucket, BUCKET)
        edited = entry.revisions.get(action='EDITED')
        self.assertEqual(edited.before['photo_path'], '')
        self.assertEqual(edited.after['photo_path'], entry.photo_path)
        page = self._page(self.se)
        self.assertContains(page, 'Photo added')
        self.assertNotContains(page, 'Photo replaced')
        self.assertNotContains(page, 'att-no-photo-chip')

    def test_replacing_a_photo_still_says_replaced(self):
        entry = self._entry()
        _client_for(self.se).post(reverse('attendance_edit', args=['ATT-A', entry.pk]),
                                  self._edit_data(entry, photo=_photo('new.jpg')))
        page = self._page(self.se)
        self.assertContains(page, 'Photo replaced')
        self.assertNotContains(page, 'Photo added')

    def test_edit_form_offers_no_photo_removal(self):
        """Edge case 10: a photo can be replaced, never removed."""
        entry = self._entry()
        response = _client_for(self.se).get(reverse('attendance_edit', args=['ATT-A', entry.pk]))
        self.assertContains(response, 'Leave the photo empty to keep the current one.')
        self.assertNotContains(response, 'No photo yet')
        self.assertNotContains(response, 'photo-clear')
        self.assertContains(response, 'name="photo"', count=1)
        # Posting the edit with no file keeps the recorded photo.
        path = entry.photo_path
        _client_for(self.se).post(reverse('attendance_edit', args=['ATT-A', entry.pk]),
                                  self._edit_data(entry, headcount='9'))
        entry.refresh_from_db()
        self.assertEqual(entry.photo_path, path)

    def test_pm_adds_a_photo_to_a_past_entry_with_a_note(self):
        """Edge case 4."""
        entry = self._bare(days_ago=2)
        client = _client_for(self.pm)
        url = reverse('attendance_correct', args=['ATT-A', entry.pk])
        self.assertContains(client.get(url), 'No photo yet — add one if you have it.')
        response = client.post(url, self._edit_data(entry, photo=_photo('later.jpg'),
                                                    reason=''))
        self.assertContains(response, CORRECTION_NOTE_REQUIRED)
        self._assert_no_photo(entry)
        response = client.post(url, self._edit_data(entry, photo=_photo('later.jpg'),
                                                    reason='Photo found on SE phone'))
        self.assertEqual(response.status_code, 302)
        entry.refresh_from_db()
        self.assertNotEqual(entry.photo_path, '')
        edited = entry.revisions.get(action='EDITED')
        self.assertEqual(edited.before['photo_path'], '')
        self.assertEqual(edited.note, 'Photo found on SE phone')
        self.assertEqual(edited.changed_by, self.pm)
        # Recorded as today, on a past entry: the history line carries the date.
        self.assertEqual(timezone.localtime(edited.changed_at).date(), self.today)
        page = self._page(self.pm)
        self.assertContains(page, 'Photo added')
        self.assertContains(page, 'Photo found on SE phone')


class LateTests(B5Fixture):

    def test_late_entry_without_photo(self):
        """Edge case 5 / checklist 3: both chips."""
        response = self._late_post(photo=False, photo_taken_at=self._exif())
        self.assertEqual(response.status_code, 302)
        entry = ContractorAttendance.objects.get()
        self._assert_no_photo(entry)
        page = self._page(self.pm)
        self.assertContains(page, 'att-late-chip')
        self.assertContains(page, 'att-no-photo-chip')


class CardTests(B5Fixture):

    def test_chip_shown_to_everyone_who_sees_the_entry(self):
        """Ruling 3: photo audience or not."""
        self._bare()
        for profile in (self.se, self.se2, self.pm, self.scm, self.finance, self.ceo, self.bd):
            self.assertContains(self._page(profile), 'att-no-photo-chip', count=1,
                                msg_prefix=profile.role)

    def test_expanded_card_says_no_photo_instead_of_thumbnail(self):
        """Ruling 3: no <img>, no "Photo time unknown"."""
        entry = self._bare()
        page = self._page(self.pm)
        self.assertContains(page, 'att-no-photo"')
        self.assertNotContains(page, reverse('attendance_photo', args=['ATT-A', entry.pk]))
        self.assertNotContains(page, 'Photo time unknown')

    def test_entry_with_photo_unchanged(self):
        entry = self._entry()
        page = self._page(self.pm)
        self.assertNotContains(page, 'att-no-photo')
        self.assertContains(page, reverse('attendance_photo', args=['ATT-A', entry.pk]))
        self.assertContains(page, 'Photo time unknown')

    def test_history_card_without_photo(self):
        self._bare(days_ago=2)
        page = self._page(self.pm)
        self.assertContains(page, 'att-no-photo-chip', count=1)
        self.assertNotContains(page, 'Photo time unknown')

    def test_photo_link_is_404_without_a_photo(self):
        """Edge case 6: 404, not 500, and nothing is signed."""
        entry = self._bare()
        url = reverse('attendance_photo', args=['ATT-A', entry.pk])
        for profile in (self.se, self.pm, self.admin):
            self.assertEqual(_client_for(profile).get(url).status_code, 404, profile.role)
        self.signer.assert_not_called()


class FlagTests(B5Fixture):

    def test_no_flags_without_a_photo(self):
        """Edge case 7 / ruling 6, even with a taken-at time far off the entry's day."""
        entry = self._bare()
        ContractorAttendance.objects.filter(pk=entry.pk).update(
            photo_taken_at=entry.recorded_at - timedelta(days=3))
        entry.refresh_from_db()
        self.assertEqual(photo_time_flags(entry), [])
        self.assertNotContains(self._page(self.pm), 'att-photo-flag')

    def test_flags_still_fire_with_a_photo(self):
        entry = self._entry()
        ContractorAttendance.objects.filter(pk=entry.pk).update(
            photo_taken_at=entry.recorded_at - timedelta(hours=3))
        self.assertContains(self._page(self.pm), 'Photo taken 3h before upload')


class SummaryTests(B5Fixture):

    def test_without_photo_figure(self):
        """Edge case 8 / checklist 4: 2 entries with a photo, 1 without (headcount 5)."""
        self._entry(trade='CIVIL', headcount=7)
        self._entry(contractor=self.both_co, trade='ELECTRICAL', headcount=3, n=2)
        self._bare(trade='STRUCTURE_MOUNTING', headcount=5)
        summary = attendance_summary(self.site, self.today, self.today)
        self.assertEqual(summary['billable'], 15)
        self.assertEqual(summary['billable_no_photo'], 5)
        page = self._page(self.pm)
        self.assertContains(page, 'of which without photo')
        self.assertContains(page, '<td class="text-end" id="att-billable-no-photo">5</td>',
                            html=True)
        self.assertContains(page, '<td class="text-end" id="att-billable">15</td>', html=True)

    def test_line_hidden_when_every_entry_has_a_photo(self):
        self._entry()
        self.assertEqual(attendance_summary(self.site, self.today,
                                            self.today)['billable_no_photo'], 0)
        self.assertNotContains(self._page(self.pm), 'of which without photo')

    def test_unmapped_entry_without_photo_waits_not_counted(self):
        """Edge case 9."""
        self._entry(headcount=7)
        self._bare(contractor=None, unlisted='Raju civil', headcount=4)
        summary = attendance_summary(self.site, self.today, self.today)
        self.assertEqual((summary['billable'], summary['billable_no_photo'],
                          summary['waiting']), (7, 0, 4))
        page = self._page(self.pm)
        self.assertNotContains(page, 'of which without photo')
        self.assertContains(page, '<td class="text-end" id="att-waiting">4</td>', html=True)

    def test_voided_entry_without_photo_not_counted(self):
        entry = self._bare(headcount=5)
        void_attendance(entry, 'Wrong site', self.se.user)
        self.assertEqual(attendance_summary(self.site, self.today,
                                            self.today)['billable_no_photo'], 0)


class StorageOffTests(B5Fixture):

    def test_save_enabled_and_entry_saves_without_photo(self):
        """Ruling (c)."""
        with self.settings(SUPABASE_BILLS_BUCKET=''):
            form = _client_for(self.se).get(reverse('attendance_add', args=['ATT-A']))
            self.assertContains(form, 'You can still save without a photo.')
            self.assertContains(form, '<button type="submit" class="btn btn-brand">Save</button>',
                                html=True)
            self.assertEqual(self._add_post(photo=None).status_code, 302)
        self._assert_no_photo(ContractorAttendance.objects.get())

    def test_attached_photo_still_refused(self):
        with self.settings(SUPABASE_BILLS_BUCKET=''):
            response = self._add_post()
        self.assertContains(response, ATTENDANCE_STORAGE_OFF)
        self.assertFalse(ContractorAttendance.objects.exists())
