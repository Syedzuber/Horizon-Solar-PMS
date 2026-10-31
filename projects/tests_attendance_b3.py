"""
Contractor attendance — ATT-B3: SCM mapping, confirmations, photo-time flag.

WHAT IS PINNED HERE
-------------------
    permission helpers (allowed and refused)        MapperHelperTests, MappableHelperTests
    map / remap / group writers                     MapWriterTests, RemapWriterTests,
                                                    GroupWriterTests
    CONFIRMED (no-change correction)                ConfirmTests
    migration 0127's CHECK and snapshot schema 2    RevisionShapeTests
    photo_time_flags and who sees the chip          PhotoFlagTests, PhotoFlagPageTests
    the queue page and the SCM dashboard link       QueueTests
    the entry card's Map / Change mapping, chips    CardTests
    the Locked line (ruling 14)                     LockedLineTests
    edge cases 1-12                                 named in each test's docstring

Edge case 6 (two SCM users mapping one group at the same moment) is MANUAL ONLY for the
lock itself: the suite runs on SQLite, which has no row locks. What the second user then
sees — the already-mapped entries skipped, not an error — is pinned here by handing the
group writer a stale list (GroupWriterTests.test_entry_mapped_meanwhile_is_skipped).

Fixtures, storage mocks and the 20:00 IST clock are tests_attendance.AttendanceFixture's;
past entries come from tests_attendance_b2.B2Fixture._past_entry().

Run with:
    python manage.py test projects.tests_attendance_b3 --settings=solarpms.test_settings
"""
from datetime import datetime, time, timedelta
from unittest import mock

from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

from .attendance import (
    ALREADY_MAPPED, CORRECTION_NOTE_REQUIRED, MAP_CONTRACTOR_NOT_ALLOWED, NOT_ALLOWED_TO_MAP,
    NOT_MAPPABLE, NOT_REMAPPABLE, REMAP_NOTE_REQUIRED, REMAP_SAME_CONTRACTOR, SNAPSHOT_SCHEMA,
    AttendanceRefused, correct_attendance, map_attendance, map_attendance_group,
    mapping_collision_message, photo_time_flags, remap_attendance, void_attendance,
)
from .attendance_views import (
    attendance_summary, attendance_unmapped_group_count, unmapped_queue_groups,
)
from .models import ContractorAttendance, ContractorAttendanceRevision
from .permissions import (
    attendance_entry_mappable, attendance_entry_remappable, user_can_map_attendance,
)
from .tests_attendance import _client_for, _messages
from .tests_attendance_b2 import B2Fixture


class B3Fixture(B2Fixture):

    def _unlisted(self, days_ago=0, name='Raju civil', trade='CIVIL', n=1, **kwargs):
        """A "Contractor not listed" entry, today (days_ago=0) or on an earlier day."""
        if days_ago:
            return self._past_entry(days_ago, contractor=None, unlisted=name, trade=trade,
                                    n=n, **kwargs)
        return self._entry(contractor=None, unlisted=name, trade=trade, n=n, **kwargs)

    def _mapped(self, **kwargs):
        entry = self._unlisted(**kwargs)
        return map_attendance(entry, self.civil_co, self.scm.user)

    def _queue_post(self, profile, name_key, contractor=None, site='ATT-A', query=''):
        return _client_for(profile).post(
            reverse('attendance_to_map') + query,
            {'site': site, 'name_key': name_key,
             'contractor': str((contractor or self.civil_co).pk)})


# ---------------------------------------------------------------------------
# Permission helpers
# ---------------------------------------------------------------------------

class MapperHelperTests(B3Fixture):

    def test_scm_and_admin_map(self):
        for profile in (self.scm, self.admin):
            self.assertTrue(user_can_map_attendance(profile.user), profile.role)

    def test_everyone_else_is_refused(self):
        """Edge case 5 (the helper)."""
        for profile in (self.pm, self.coord, self.se, self.finance, self.ceo, self.sysadmin,
                        self.bd):
            self.assertFalse(user_can_map_attendance(profile.user), profile.role)


class MappableHelperTests(B3Fixture):

    def test_live_unlisted_entry_is_mappable_not_remappable(self):
        entry = self._unlisted()
        self.assertTrue(attendance_entry_mappable(entry))
        self.assertFalse(attendance_entry_remappable(entry))

    def test_listed_entry_is_neither(self):
        entry = self._entry()
        self.assertFalse(attendance_entry_mappable(entry))
        self.assertFalse(attendance_entry_remappable(entry))

    def test_mapped_entry_is_remappable_only(self):
        entry = self._mapped()
        self.assertFalse(attendance_entry_mappable(entry))
        self.assertTrue(attendance_entry_remappable(entry))

    def test_voided_entries_are_neither(self):
        unlisted = void_attendance(self._unlisted(), 'Wrong site', self.se.user)
        self.assertFalse(attendance_entry_mappable(unlisted))
        mapped = void_attendance(self._mapped(name='Other crew', n=2), 'Duplicate',
                                 self.se.user)
        self.assertFalse(attendance_entry_remappable(mapped))

    def test_soft_deleted_site_is_not_mappable(self):
        entry = self._unlisted()
        entry.project.is_deleted = True
        self.assertFalse(attendance_entry_mappable(entry))

    def test_corrected_back_to_unlisted_is_mappable_not_remappable(self):
        """Zuber's ruling C: original name set but no contractor -> Map, not Change."""
        entry = self._mapped(days_ago=2)
        entry, _ = correct_attendance(entry, self._data(entry, contractor=None,
                                                        unlisted_name='Raju civil'),
                                      self.pm.user, 'Mapped to the wrong crew')
        self.assertTrue(entry.original_unlisted_name)
        self.assertTrue(attendance_entry_mappable(entry))
        self.assertFalse(attendance_entry_remappable(entry))


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------

class MapWriterTests(B3Fixture):

    def test_map_moves_the_name_and_writes_mapped(self):
        entry = self._unlisted(name='  Raju   civil ')
        before_updated = entry.updated_at
        entry = map_attendance(entry, self.civil_co, self.scm.user)
        entry.refresh_from_db()
        self.assertEqual(entry.contractor, self.civil_co)
        self.assertEqual(entry.unlisted_contractor_name, '')
        self.assertEqual(entry.original_unlisted_name, 'Raju civil')
        self.assertGreaterEqual(entry.updated_at, before_updated)
        rev = entry.revisions.order_by('pk').last()
        self.assertEqual(rev.action, ContractorAttendanceRevision.MAPPED)
        self.assertEqual(rev.before['unlisted_contractor_name'], 'Raju civil')
        self.assertIsNone(rev.before['contractor_id'])
        self.assertEqual(rev.after['contractor_name'], 'Sharma Civil Works')
        self.assertEqual(rev.after['original_unlisted_name'], 'Raju civil')
        self.assertEqual(rev.changed_by, self.scm)
        self.assertEqual(rev.note, '')

    def test_admin_maps_a_past_entry_on_any_status(self):
        """Ruling 1: not limited by entry date or site status."""
        entry = self._unlisted(days_ago=20)
        self.site.status = 'Completed'
        self.site.save()
        entry = map_attendance(entry, self.both_co, self.admin.user)
        self.assertEqual(entry.contractor, self.both_co)

    def test_voided_entry_is_refused(self):
        """Edge case 2."""
        entry = void_attendance(self._unlisted(), 'Wrong site', self.se.user)
        with self.assertRaisesMessage(AttendanceRefused, NOT_MAPPABLE):
            map_attendance(entry, self.civil_co, self.scm.user)

    def test_already_mapped_entry_is_refused(self):
        """Edge case 2: an already-mapped entry through the map path."""
        entry = self._mapped()
        with self.assertRaisesMessage(AttendanceRefused,
                                      ALREADY_MAPPED.format(name='Sharma Civil Works')):
            map_attendance(entry, self.both_co, self.scm.user)
        self.assertEqual(self._actions(entry), ['CREATED', 'MAPPED'])

    def test_listed_from_start_is_not_mappable(self):
        entry = self._entry()
        with self.assertRaisesMessage(AttendanceRefused, NOT_MAPPABLE):
            map_attendance(entry, self.both_co, self.scm.user)

    def test_inactive_or_supplier_vendor_is_refused(self):
        """Edge case 3 (the writer)."""
        entry = self._unlisted()
        for vendor in (self.gone_co, self.supplier, None):
            with self.assertRaisesMessage(AttendanceRefused, MAP_CONTRACTOR_NOT_ALLOWED):
                map_attendance(entry, vendor, self.scm.user)
        self.assertEqual(self._actions(entry), ['CREATED'])

    def test_vendor_deactivated_after_the_page_was_drawn_is_refused(self):
        entry = self._unlisted()
        stale = self.civil_co
        type(stale).objects.filter(pk=stale.pk).update(is_active=False)
        with self.assertRaisesMessage(AttendanceRefused, MAP_CONTRACTOR_NOT_ALLOWED):
            map_attendance(entry, stale, self.scm.user)

    def test_non_mapper_is_refused(self):
        """Edge case 5 (the writer)."""
        entry = self._unlisted()
        for profile in (self.pm, self.se, self.finance, self.ceo):
            with self.assertRaisesMessage(AttendanceRefused, NOT_ALLOWED_TO_MAP):
                map_attendance(entry, self.civil_co, profile.user)
        entry.refresh_from_db()
        self.assertIsNone(entry.contractor_id)

    def test_collision_is_refused_and_writes_nothing(self):
        """Edge case 1 (single entry)."""
        self._entry(trade='CIVIL', n=1)
        entry = self._unlisted(trade='CIVIL', n=2)
        with self.assertRaisesMessage(AttendanceRefused,
                                      mapping_collision_message(entry, self.civil_co)):
            map_attendance(entry, self.civil_co, self.scm.user)
        entry.refresh_from_db()
        self.assertIsNone(entry.contractor_id)
        self.assertEqual(entry.unlisted_contractor_name, 'Raju civil')
        self.assertEqual(self._actions(entry), ['CREATED'])

    def test_collision_message_wording(self):
        entry = self._unlisted(days_ago=3)
        day = entry.date
        self.assertEqual(mapping_collision_message(entry, self.civil_co),
                         f'Already logged for Sharma Civil Works · Civil on {day.day} '
                         f'{day:%b} — ask the PM to void one.')

    def test_a_voided_listed_entry_does_not_collide(self):
        listed = self._entry(trade='CIVIL', n=1)
        void_attendance(listed, 'Logged twice', self.se.user)
        entry = map_attendance(self._unlisted(trade='CIVIL', n=2), self.civil_co,
                               self.scm.user)
        self.assertEqual(entry.contractor, self.civil_co)


class RemapWriterTests(B3Fixture):

    def test_remap_keeps_the_original_name_and_records_the_note(self):
        entry = self._mapped()
        entry = remap_attendance(entry, self.both_co, self.scm.user, ' Wrong crew picked ')
        entry.refresh_from_db()
        self.assertEqual(entry.contractor, self.both_co)
        self.assertEqual(entry.original_unlisted_name, 'Raju civil')
        rev = entry.revisions.order_by('pk').last()
        self.assertEqual(rev.action, 'MAPPED')
        self.assertEqual(rev.note, 'Wrong crew picked')
        self.assertEqual(rev.before['contractor_name'], 'Sharma Civil Works')
        self.assertEqual(rev.after['contractor_name'], 'Kumar Electricals')
        self.assertEqual(self._actions(entry), ['CREATED', 'MAPPED', 'MAPPED'])

    def test_remap_without_note_is_refused(self):
        """Edge case 4."""
        entry = self._mapped()
        for note in ('', '   ', None):
            with self.assertRaisesMessage(AttendanceRefused, REMAP_NOTE_REQUIRED):
                remap_attendance(entry, self.both_co, self.scm.user, note)
        self.assertEqual(self._actions(entry), ['CREATED', 'MAPPED'])

    def test_remap_of_an_entry_never_unlisted_is_refused(self):
        """Edge case 4."""
        entry = self._entry()
        with self.assertRaisesMessage(AttendanceRefused, NOT_REMAPPABLE):
            remap_attendance(entry, self.both_co, self.scm.user, 'Change it')

    def test_remap_of_an_unmapped_entry_is_refused(self):
        entry = self._unlisted()
        with self.assertRaisesMessage(AttendanceRefused, NOT_REMAPPABLE):
            remap_attendance(entry, self.both_co, self.scm.user, 'Change it')

    def test_remap_to_the_same_contractor_is_refused(self):
        entry = self._mapped()
        with self.assertRaisesMessage(AttendanceRefused, REMAP_SAME_CONTRACTOR):
            remap_attendance(entry, self.civil_co, self.scm.user, 'Same again')

    def test_remap_refuses_non_mapper_inactive_vendor_and_collision(self):
        entry = self._mapped(trade='CIVIL', n=1)
        with self.assertRaisesMessage(AttendanceRefused, NOT_ALLOWED_TO_MAP):
            remap_attendance(entry, self.both_co, self.pm.user, 'PM tries')
        with self.assertRaisesMessage(AttendanceRefused, MAP_CONTRACTOR_NOT_ALLOWED):
            remap_attendance(entry, self.gone_co, self.scm.user, 'Inactive')
        self._entry(contractor=self.both_co, trade='CIVIL', n=2)
        with self.assertRaisesMessage(AttendanceRefused,
                                      mapping_collision_message(entry, self.both_co)):
            remap_attendance(entry, self.both_co, self.scm.user, 'Collides')
        entry.refresh_from_db()
        self.assertEqual(entry.contractor, self.civil_co)


class GroupWriterTests(B3Fixture):

    def test_spellings_differing_in_case_and_spaces_map_together(self):
        """Edge case 7."""
        first = self._unlisted(days_ago=3, name='Raju civil', n=1)
        second = self._unlisted(days_ago=2, name='RAJU  Civil', n=2)
        mapped, skipped = map_attendance_group(self.site, 'raju civil', self.civil_co,
                                               self.scm.user)
        self.assertEqual({e.pk for e in mapped}, {first.pk, second.pk})
        self.assertEqual(skipped, [])
        second.refresh_from_db()
        self.assertEqual(second.original_unlisted_name, 'RAJU Civil')

    def test_collision_skips_that_entry_and_maps_the_rest(self):
        """Edge case 1: each entry in its own transaction — one collision rolls back
        nothing else."""
        clash = self._unlisted(days_ago=2, n=1)
        free = self._unlisted(days_ago=3, n=2)
        self._past_entry(2, contractor=self.civil_co, trade='CIVIL', n=3)
        mapped, skipped = map_attendance_group(self.site, 'Raju Civil', self.civil_co,
                                               self.scm.user)
        self.assertEqual([e.pk for e in mapped], [free.pk])
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0][0].pk, clash.pk)
        self.assertEqual(skipped[0][1], mapping_collision_message(clash, self.civil_co))
        clash.refresh_from_db()
        free.refresh_from_db()
        self.assertIsNone(clash.contractor_id)
        self.assertEqual(clash.unlisted_contractor_name, 'Raju civil')
        self.assertEqual(free.contractor, self.civil_co)

    def test_entry_mapped_meanwhile_is_skipped(self):
        """Edge case 6, the outcome: the second SCM user's stale list meets an entry the
        first already mapped — skipped with a reason, not an error. The lock itself is
        manual only (SQLite)."""
        entry = self._unlisted()
        stale = list(ContractorAttendance.objects.filter(pk=entry.pk))
        map_attendance(entry, self.civil_co, self.scm.user)
        with mock.patch('projects.attendance.unmapped_group_entries', return_value=stale):
            mapped, skipped = map_attendance_group(self.site, 'raju civil', self.both_co,
                                                   self.admin.user)
        self.assertEqual(mapped, [])
        self.assertEqual(skipped[0][1], ALREADY_MAPPED.format(name='Sharma Civil Works'))
        entry.refresh_from_db()
        self.assertEqual(entry.contractor, self.civil_co)

    def test_voided_and_other_site_entries_are_left_alone(self):
        voided = void_attendance(self._unlisted(n=1), 'Wrong', self.se.user)
        other = self._entry(by=self.res_se, project=self.res, contractor=None,
                            unlisted='Raju civil', n=2)
        mapped, _ = map_attendance_group(self.site, 'raju civil', self.civil_co,
                                         self.scm.user)
        self.assertEqual(mapped, [])
        voided.refresh_from_db()
        other.refresh_from_db()
        self.assertIsNone(voided.contractor_id)
        self.assertIsNone(other.contractor_id)

    def test_non_mapper_and_bad_vendor_refused_outright(self):
        self._unlisted()
        with self.assertRaisesMessage(AttendanceRefused, NOT_ALLOWED_TO_MAP):
            map_attendance_group(self.site, 'raju civil', self.civil_co, self.pm.user)
        with self.assertRaisesMessage(AttendanceRefused, MAP_CONTRACTOR_NOT_ALLOWED):
            map_attendance_group(self.site, 'raju civil', self.supplier, self.scm.user)
        self.assertEqual(ContractorAttendance.objects.filter(contractor__isnull=False).count(),
                         0)

    def test_summary_moves_man_days_to_billable(self):
        """Edge case 10."""
        self._unlisted(days_ago=0, headcount=5, n=1)
        self._unlisted(days_ago=0, name='RAJU CIVIL', trade='ELECTRICAL', headcount=3, n=2)
        today = self.today
        summary = attendance_summary(self.site, today, today)
        self.assertEqual((summary['billable'], summary['waiting']), (0, 8))
        map_attendance_group(self.site, 'raju civil', self.civil_co, self.scm.user)
        summary = attendance_summary(self.site, today, today)
        self.assertEqual((summary['billable'], summary['waiting']), (8, 0))
        rows = {(r['name'], r['trade']): r['man_days'] for r in summary['rows']}
        self.assertEqual(rows, {('Sharma Civil Works', 'Civil'): 5,
                                ('Sharma Civil Works', 'Electrical'): 3})


# ---------------------------------------------------------------------------
# Confirmations
# ---------------------------------------------------------------------------

class ConfirmTests(B3Fixture):

    def test_no_change_with_note_writes_confirmed_and_leaves_the_entry(self):
        """Edge case 8."""
        entry = self._past_entry(2)
        stamp = entry.updated_at
        entry, changed = correct_attendance(entry, self._data(entry), self.pm.user,
                                            ' Checked against the register ')
        self.assertFalse(changed)
        entry.refresh_from_db()
        self.assertEqual(entry.updated_at, stamp)
        rev = entry.revisions.order_by('pk').last()
        self.assertEqual(rev.action, 'CONFIRMED')
        self.assertEqual(rev.note, 'Checked against the register')
        self.assertEqual(rev.before, rev.after)
        self.assertEqual(rev.changed_by, self.pm)

    def test_no_change_without_note_is_refused(self):
        """Edge case 8."""
        entry = self._past_entry(2)
        with self.assertRaisesMessage(AttendanceRefused, CORRECTION_NOTE_REQUIRED):
            correct_attendance(entry, self._data(entry), self.pm.user, '  ')
        self.assertEqual(self._actions(entry), ['CREATED'])

    def test_a_real_change_still_writes_edited(self):
        entry = self._past_entry(2)
        correct_attendance(entry, self._data(entry, headcount=9), self.pm.user, 'Recount')
        self.assertEqual(self._actions(entry), ['CREATED', 'EDITED'])

    def test_screen_says_confirmed_and_history_shows_it(self):
        """Browser checklist 6."""
        entry = self._past_entry(2)
        response = _client_for(self.pm).post(
            reverse('attendance_correct', args=['ATT-A', entry.pk]),
            dict(self._edit_data(entry), reason='Checked with the SE'))
        self.assertIn('Entry confirmed — no changes, reason recorded.', _messages(response))
        page = self._page(self.pm).content.decode()
        self.assertIn('Confirmed by', page)
        self.assertIn('“Checked with the SE”', page)


class RevisionShapeTests(B3Fixture):

    def test_confirmed_without_note_violates_the_check(self):
        entry = self._entry()
        snap = {'schema': SNAPSHOT_SCHEMA}
        with self.assertRaises(IntegrityError), transaction.atomic():
            ContractorAttendanceRevision.objects.create(
                attendance=entry, action='CONFIRMED', before=snap, after=snap, note='',
                changed_by=self.pm)

    def test_mapped_and_confirmed_are_known_actions(self):
        entry = self._entry()
        snap = {'schema': SNAPSHOT_SCHEMA}
        for action in ('MAPPED', 'CONFIRMED'):
            ContractorAttendanceRevision.objects.create(
                attendance=entry, action=action, before=snap, after=snap, note='n',
                changed_by=self.scm)
        with self.assertRaises(IntegrityError), transaction.atomic():
            ContractorAttendanceRevision.objects.create(
                attendance=entry, action='UNMAPPED', before=snap, after=snap,
                changed_by=self.scm)

    def test_snapshot_schema_2_carries_the_original_name(self):
        entry = self._entry()
        after = entry.revisions.get().after
        self.assertEqual(after['schema'], 2)
        self.assertEqual(after['original_unlisted_name'], '')

    def test_schema_1_revision_still_renders(self):
        """Ruling B: old revisions are read with .get()."""
        entry = self._past_entry(2)
        snap = dict(entry.revisions.get().after)
        snap.pop('original_unlisted_name')
        snap['schema'] = 1
        ContractorAttendanceRevision.objects.create(
            attendance=entry, action='EDITED', before=snap, after=dict(snap, headcount=8),
            note='old', changed_by=self.pm)
        self.assertContains(self._page(self.pm), 'Headcount: 7 → 8')


# ---------------------------------------------------------------------------
# Photo-time flag
# ---------------------------------------------------------------------------

def _aware(day, hour, minute=0):
    return timezone.make_aware(datetime.combine(day, time(hour, minute)))


class PhotoFlagTests(B3Fixture):
    """Edge case 9 — photo_time_flags() reads only the entry's own fields."""

    def _shape(self, day, recorded, taken):
        return ContractorAttendance(date=day, recorded_at=recorded, photo_taken_at=taken)

    def test_same_day_two_hours_one_minute_flags(self):
        day = self.today
        entry = self._shape(day, _aware(day, 10, 1), _aware(day, 8, 0))
        self.assertEqual(photo_time_flags(entry), ['Photo taken 2h before upload'])

    def test_same_day_one_hour_fifty_nine_does_not(self):
        day = self.today
        entry = self._shape(day, _aware(day, 9, 59), _aware(day, 8, 0))
        self.assertEqual(photo_time_flags(entry), [])

    def test_exactly_two_hours_does_not(self):
        day = self.today
        self.assertEqual(photo_time_flags(self._shape(day, _aware(day, 10), _aware(day, 8))),
                         [])

    def test_photo_from_another_day_on_a_same_day_entry_flags_both(self):
        day = self.today
        yesterday = day - timedelta(days=1)
        entry = self._shape(day, _aware(day, 9), _aware(yesterday, 18))
        self.assertEqual(photo_time_flags(entry),
                         ['Photo taken 15h before upload',
                          f'Photo dated {yesterday.day} {yesterday:%b}'])

    def test_late_entry_with_a_photo_from_its_own_day_does_not(self):
        day = self.today - timedelta(days=3)
        entry = self._shape(day, _aware(self.today, 11), _aware(day, 8))
        self.assertEqual(photo_time_flags(entry), [])

    def test_late_entry_with_a_photo_from_another_day_gets_the_date_chip_only(self):
        day = self.today - timedelta(days=3)
        other = day - timedelta(days=1)
        entry = self._shape(day, _aware(self.today, 11), _aware(other, 8))
        self.assertEqual(photo_time_flags(entry), [f'Photo dated {other.day} {other:%b}'])

    def test_null_time_has_no_chip(self):
        day = self.today
        self.assertEqual(photo_time_flags(self._shape(day, _aware(day, 9), None)), [])

    def test_dates_are_compared_in_ist(self):
        """00:30 IST is the previous day in UTC; it must still read as the entry's day."""
        day = self.today
        entry = self._shape(day, _aware(day, 1, 0), _aware(day, 0, 30))
        self.assertEqual(photo_time_flags(entry), [])


class PhotoFlagPageTests(B3Fixture):

    def _flagged_today(self):
        entry = self._entry(by=self.se)
        ContractorAttendance.objects.filter(pk=entry.pk).update(
            photo_taken_at=entry.recorded_at - timedelta(hours=3))
        return entry

    def test_photo_audience_sees_the_chip(self):
        """Browser checklist 7."""
        self._flagged_today()
        for profile in (self.pm, self.se, self.scm):
            self.assertContains(self._page(profile), 'Photo taken 3h before upload',
                                msg_prefix=profile.role)

    def test_viewer_without_photo_access_sees_no_chip(self):
        """Bug pre-emption: the chip never reveals the photo's time."""
        self._flagged_today()
        for profile in (self.se2, self.finance, self.bd):
            page = self._page(profile)
            self.assertEqual(page.status_code, 200)
            self.assertNotContains(page, 'att-photo-flag', msg_prefix=profile.role)

    def test_history_card_carries_the_chip(self):
        entry = self._past_entry(2)
        ContractorAttendance.objects.filter(pk=entry.pk).update(
            photo_taken_at=_aware(entry.date - timedelta(days=1), 17))
        prev = entry.date - timedelta(days=1)
        page = self._page(self.pm)
        self.assertContains(page, f'Photo dated {prev.day} {prev:%b}')
        self.assertNotContains(self._page(self.finance), 'att-photo-flag')

    def test_unknown_time_shown_to_photo_audience_only(self):
        self._entry(by=self.se)
        self.assertContains(self._page(self.pm), 'Photo time unknown')
        self.assertNotContains(self._page(self.se2), 'Photo time unknown')


# ---------------------------------------------------------------------------
# Queue page and dashboard link
# ---------------------------------------------------------------------------

class QueueTests(B3Fixture):

    def test_one_group_per_site_and_name_oldest_first(self):
        """Edge case 7: one group for spellings differing in case / spaces."""
        self._unlisted(days_ago=2, name='Raju civil', n=1)
        self._unlisted(days_ago=1, name='RAJU  Civil', trade='ELECTRICAL', headcount=3, n=2)
        self._unlisted(days_ago=5, name='Babu Steel', trade='STRUCTURE_MOUNTING', n=3)
        groups = unmapped_queue_groups()
        self.assertEqual([g['name_key'] for g in groups], ['babu steel', 'raju civil'])
        raju = groups[1]
        self.assertEqual(raju['site'], 'ATT-A')
        self.assertEqual(raju['entries'], 2)
        self.assertEqual(raju['man_days'], 10)
        self.assertEqual(raju['first'], self.today - timedelta(days=2))
        self.assertEqual(raju['last'], self.today - timedelta(days=1))
        self.assertEqual(sorted(raju['trades']), ['Civil', 'Electrical'])
        self.assertIn(raju['name'], ('Raju civil', 'RAJU Civil'))
        self.assertEqual(attendance_unmapped_group_count(), 2)

    def test_soft_deleted_and_voided_entries_are_not_queued(self):
        """Edge case 11."""
        self._unlisted(n=1)
        void_attendance(self._unlisted(name='Voided crew', n=2), 'Wrong', self.se.user)
        self.assertEqual(attendance_unmapped_group_count(), 1)
        self.site.is_deleted = True
        self.site.save()
        self.assertEqual(unmapped_queue_groups(), [])
        self.assertEqual(attendance_unmapped_group_count(), 0)

    def test_page_lists_groups_and_filters_by_site(self):
        self._unlisted(n=1)
        self._entry(by=self.res_se, project=self.res, contractor=None, unlisted='Res crew',
                    n=2)
        page = _client_for(self.scm).get(reverse('attendance_to_map'))
        self.assertContains(page, '‘Raju civil’')
        self.assertContains(page, '‘Res crew’')
        self.assertContains(page, reverse('vendor_add'))
        page = _client_for(self.scm).get(reverse('attendance_to_map') + '?site=ATT-R')
        self.assertNotContains(page, '‘Raju civil’')
        self.assertContains(page, '‘Res crew’')

    def test_non_mappers_get_403(self):
        """Edge case 5 (the queue)."""
        for profile in (self.pm, self.se, self.finance, self.ceo):
            client = _client_for(profile)
            self.assertEqual(client.get(reverse('attendance_to_map')).status_code, 403,
                             profile.role)
            self.assertEqual(self._queue_post(profile, 'raju civil').status_code, 403,
                             profile.role)

    def test_map_all_posts_and_reports_skipped_entries(self):
        """Browser checklist 2-3, edge case 1 on the page."""
        clash = self._unlisted(days_ago=2, n=1)
        self._unlisted(days_ago=3, name='RAJU  CIVIL', n=2)
        self._past_entry(2, contractor=self.civil_co, trade='CIVIL', n=3)
        response = self._queue_post(self.scm, 'raju civil')
        self.assertRedirects(response, reverse('attendance_to_map'),
                             fetch_redirect_response=False)
        msgs = _messages(response)
        self.assertIn('Mapped 1 entry on ATT-A to Sharma Civil Works.', msgs)
        self.assertIn(f'Not mapped — {clash.date.day} {clash.date:%b} · Civil: '
                      f'{mapping_collision_message(clash, self.civil_co)}', msgs)
        self.assertEqual(attendance_unmapped_group_count(), 1)

    def test_second_post_on_a_mapped_group_is_not_an_error(self):
        """Edge case 6 on the page: nothing left — an info message, not an error page."""
        self._unlisted()
        self._queue_post(self.scm, 'raju civil')
        response = self._queue_post(self.admin, 'raju civil', contractor=self.both_co)
        self.assertEqual(response.status_code, 302)
        self.assertIn('Nothing left to map in that group — it was already mapped.',
                      _messages(response))

    def test_inactive_or_supplier_pick_is_refused_by_the_form(self):
        """Edge case 3 (the form)."""
        self._unlisted()
        for vendor in (self.gone_co, self.supplier):
            response = self._queue_post(self.scm, 'raju civil', contractor=vendor)
            self.assertIn(MAP_CONTRACTOR_NOT_ALLOWED, _messages(response))
        self.assertEqual(attendance_unmapped_group_count(), 1)

    def test_post_keeps_the_site_filter_and_refuses_a_deleted_site(self):
        self._unlisted()
        response = self._queue_post(self.scm, 'raju civil', query='?site=ATT-A')
        self.assertEqual(response['Location'], reverse('attendance_to_map') + '?site=ATT-A')
        self.res.is_deleted = True
        self.res.save()
        self.assertEqual(self._queue_post(self.scm, 'x', site='ATT-R').status_code, 404)

    def test_phone_layout_has_no_fixed_width_table(self):
        """Edge case 12, the part a test can see: stacked cards, no table, no fixed
        widths. The rest (no sideways scroll at phone width) is the browser checklist."""
        self._unlisted()
        html = _client_for(self.scm).get(reverse('attendance_to_map')).content.decode()
        self.assertNotIn('<table', html)
        self.assertIn('max-width:720px', html)
        self.assertIn('att-picker', html)

    def test_scm_dashboard_links_with_the_group_count(self):
        """Browser checklist 2, ruling 9."""
        self._unlisted(n=1)
        self._unlisted(name='raju  CIVIL', days_ago=1, n=2)
        page = _client_for(self.scm).get(reverse('dashboard_scm'))
        self.assertContains(page, reverse('attendance_to_map'))
        self.assertContains(page, 'Contractors to map (1)')


# ---------------------------------------------------------------------------
# Entry card
# ---------------------------------------------------------------------------

class CardTests(B3Fixture):

    def test_mapper_sees_map_on_an_unlisted_card(self):
        entry = self._unlisted()
        for profile in (self.scm, self.admin):
            page = self._page(profile)
            self.assertContains(page, reverse('attendance_map', args=['ATT-A', entry.pk]),
                                msg_prefix=profile.role)

    def test_pm_and_se_see_no_map_buttons(self):
        """Browser checklist 5."""
        unlisted = self._unlisted(n=1)
        mapped = self._mapped(name='Other crew', n=2)
        for profile in (self.pm, self.se, self.finance, self.ceo):
            page = self._page(profile)
            self.assertNotContains(page, reverse('attendance_map', args=['ATT-A', unlisted.pk]))
            self.assertNotContains(page, reverse('attendance_remap', args=['ATT-A', mapped.pk]))
            self.assertNotContains(page, 'att-map-btn')

    def test_card_map_and_remap_round_trip(self):
        """Browser checklist 4: chip, Change mapping without then with a note, history."""
        entry = self._unlisted()
        client = _client_for(self.scm)
        response = client.post(reverse('attendance_map', args=['ATT-A', entry.pk]),
                               {'contractor': str(self.civil_co.pk)})
        self.assertIn('Mapped ‘Raju civil’ to Sharma Civil Works', _messages(response)[0])
        page = self._page(self.scm)
        self.assertContains(page, 'Originally logged as ‘Raju civil’')
        self.assertContains(page, reverse('attendance_remap', args=['ATT-A', entry.pk]))
        response = client.post(reverse('attendance_remap', args=['ATT-A', entry.pk]),
                               {'contractor': str(self.both_co.pk), 'note': ''})
        self.assertIn(REMAP_NOTE_REQUIRED, _messages(response))
        response = client.post(reverse('attendance_remap', args=['ATT-A', entry.pk]),
                               {'contractor': str(self.both_co.pk), 'note': 'Kumar’s crew'})
        # Earlier messages are still queued on this client (no page was drawn between).
        self.assertTrue(any(m.startswith('Mapping changed to Kumar Electricals')
                            for m in _messages(response)))
        html = self._page(self.scm).content.decode()
        self.assertEqual(html.count('Mapped by'), 2)
        self.assertIn('“Kumar’s crew”', html)
        self.assertIn('Originally logged as ‘Raju civil’', html)

    def test_original_chip_is_shown_to_every_viewer(self):
        self._mapped()
        for profile in (self.pm, self.se, self.finance):
            self.assertContains(self._page(profile), 'Originally logged as ‘Raju civil’')

    def test_map_urls_refuse_non_mappers_and_get(self):
        """Edge case 5 (the card URLs)."""
        entry = self._unlisted()
        url = reverse('attendance_map', args=['ATT-A', entry.pk])
        for profile in (self.pm, self.se, self.finance, self.ceo):
            response = _client_for(profile).post(url, {'contractor': str(self.civil_co.pk)})
            self.assertEqual(response.status_code, 403, profile.role)
        remap = reverse('attendance_remap', args=['ATT-A', entry.pk])
        self.assertEqual(_client_for(self.pm).post(remap, {}).status_code, 403)
        self.assertEqual(_client_for(self.scm).get(url).status_code, 405)
        entry.refresh_from_db()
        self.assertIsNone(entry.contractor_id)

    def test_card_map_of_a_voided_entry_says_why(self):
        """Edge case 2 on the page."""
        entry = void_attendance(self._unlisted(), 'Wrong', self.se.user)
        response = _client_for(self.scm).post(
            reverse('attendance_map', args=['ATT-A', entry.pk]),
            {'contractor': str(self.civil_co.pk)})
        self.assertIn(NOT_MAPPABLE, _messages(response))

    def test_history_card_offers_map_to_a_mapper(self):
        entry = self._unlisted(days_ago=4)
        self.assertContains(self._page(self.scm),
                            reverse('attendance_map', args=['ATT-A', entry.pk]))


class LockedLineTests(B3Fixture):
    """Ruling 14: the Locked line only for people who could log on the site."""

    def test_site_engineer_sees_it(self):
        self._past_entry(2)
        self.assertContains(self._page(self.se), 'Locked. Ask the PM to correct.')

    def test_finance_ceo_and_scm_do_not(self):
        """Browser checklist 8."""
        self._past_entry(2)
        for profile in (self.finance, self.ceo, self.scm, self.bd):
            page = self._page(profile)
            self.assertEqual(page.status_code, 200)
            self.assertNotContains(page, 'att-locked', msg_prefix=profile.role)
