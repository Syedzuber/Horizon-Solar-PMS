"""
Contractor attendance — ATT-B1: model, edit history, same-day add / edit / time out / void.

WHAT IS PINNED HERE
-------------------
    permission helpers (allowed and refused)        LogHelperTests, ChangeHelperTests,
                                                    PhotoHelperTests, RecorderHelperTests
    every database constraint                       ConstraintTests, RevisionConstraintTests
    the writers: a revision on every write, none
      on a no-change edit, duplicate -> refusal      WriterTests
    the screens, edge cases 1-12                    AddScreenTests, EditScreenTests,
                                                    TimeOutAndVoidTests, AccessTests,
                                                    PhotoScreenTests, OverviewCardTests
    same-day lock after midnight                    MidnightTests
    recorder rights after task removal              RecorderMovedOffTests
    ATT-B1a: no time later than now + 15 minutes    FutureTimeTests

"Today" is moved by patching django.utils.timezone.localdate, which every attendance
check reads. "Now" (ATT-B1a) is attendance.local_now(), pinned at 20:00 IST by the fixture. Storage is mocked at qaqc_views._client (upload), attendance._client (discard)
and bill_storage's signer. No test asserts on a substring of a storage path: the path is a
uuid, so a substring check would pass or fail by chance (SECONDARY_FINDINGS, ATT-B1).

Run with:
    python manage.py test projects.tests_attendance --settings=solarpms.test_settings
"""
from datetime import datetime, time, timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.contrib.messages import get_messages
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from . import attendance
from .attendance import (
    DUPLICATE_ENTRY, AttendanceRefused, create_attendance, set_time_out, update_attendance,
    void_attendance,
)
from .models import (
    AppendOnlyViolation, ContractorAttendance, ContractorAttendanceRevision, Program, Project,
    ProjectPhase, SiteQaqcAssignment, Task, Vendor,
)
from .permissions import (
    user_can_change_attendance_today, user_can_log_attendance, user_can_view_attendance_photo,
    user_is_todays_attendance_recorder,
)

BUCKET = 'attendance-test-private'
SIGNED = 'https://storage.example/signed/attendance.jpg?token=abc'
JPEG = b'\xff\xd8\xff\xe0 fake jpeg bytes'


def _profile(username, role):
    """A user whose signal-created profile carries `role`."""
    user = User.objects.create_user(username=username, password='x',
                                    first_name=username.title(), last_name='Test')
    profile = user.profile
    profile.role = role
    profile.is_active = True
    profile.save()
    return profile


def _client_for(profile):
    client = Client()
    client.force_login(profile.user)
    return client


def _messages(response):
    return [str(m) for m in get_messages(response.wsgi_request)]


def _photo(name='crew.jpg', content=JPEG, content_type='image/jpeg'):
    return SimpleUploadedFile(name, content, content_type=content_type)


def _vendor(name, kind='contractor', is_active=True):
    return Vendor.objects.create(name=name, contact_person='C', phone='9876543210',
                                 kind=kind, is_active=is_active)


def _tomorrow():
    return timezone.localdate() + timedelta(days=1)


def _at(hour, minute, second=0):
    """Today at hour:minute:second IST, as an aware datetime — the value local_now()
    returns."""
    return timezone.make_aware(datetime.combine(timezone.localdate(),
                                                time(hour, minute, second)))


class AttendanceFixture(TestCase):
    """
    site (OPEX, Active): pm + coord; se and se2 hold Site Engineer tasks; qaqc is the
        site's QA/QC engineer and also holds a task (works only that task, never the site).
    res (Residential, Active): pm; res_se holds a task.
    stranger: Site Engineer with no task on either site.
    Portfolio roles: finance, scm, admin, ceo, sysadmin, bd.
    Vendors: civil_co (contractor), both_co (both), supplier (supplier), gone_co
    (contractor, inactive).
    """

    def setUp(self):
        self.pm       = _profile('at_pm', 'PM')
        self.coord    = _profile('at_coord', 'Project Coordinator')
        self.se       = _profile('at_se', 'Site Engineer')
        self.se2      = _profile('at_se2', 'Site Engineer')
        self.qaqc     = _profile('at_qaqc', 'Site Engineer')
        self.res_se   = _profile('at_res_se', 'Site Engineer')
        self.stranger = _profile('at_stranger', 'Site Engineer')
        self.finance  = _profile('at_fin', 'Finance')
        self.scm      = _profile('at_scm', 'SCM')
        self.admin    = _profile('at_admin', 'Admin')
        self.ceo      = _profile('at_ceo', 'CEO')
        self.sysadmin = _profile('at_sysadmin', 'System Admin')
        self.bd       = _profile('at_bd', 'BD')

        program = Program.objects.create(name='Test-ATT', program_type='OPEX',
                                         client_name='ATTClient', status='Active',
                                         short_tender_code='ATT')
        self.site = Project.objects.create(
            project_id='ATT-A', customer_name='ATTClient', customer_phone='9876543210',
            site_address='1 Sun Rd', city='Delhi', project_type='OPEX', program=program,
            site_code='ATT-A', dc_capacity_kw=Decimal('100.00'), status='Active',
            assigned_pm=self.pm)
        self.site.coordinators.add(self.coord)
        self.res = Project.objects.create(
            project_id='ATT-R', customer_name='ATTRes', customer_phone='9876543210',
            site_address='2 Sun Rd', city='Delhi', project_type='Residential',
            dc_capacity_kw=Decimal('5.00'), status='Active', assigned_pm=self.pm)

        phase = ProjectPhase.objects.create(project=self.site, phase_name='Install',
                                            phase_order=1)
        self.se_task = Task.objects.create(phase=phase, task_name='Mount structure',
                                           task_order=1, assigned_role=Task.SITE_ENGINEER,
                                           assigned_to=self.se)
        Task.objects.create(phase=phase, task_name='Lay cable', task_order=2,
                            assigned_role=Task.SITE_ENGINEER, assigned_to=self.se2)
        Task.objects.create(phase=phase, task_name='Earthing', task_order=3,
                            assigned_role=Task.SITE_ENGINEER, assigned_to=self.qaqc)
        SiteQaqcAssignment.objects.create(project=self.site, engineer=self.qaqc,
                                          assigned_by=self.pm)
        res_phase = ProjectPhase.objects.create(project=self.res, phase_name='Install',
                                                phase_order=1)
        Task.objects.create(phase=res_phase, task_name='Mount panels', task_order=1,
                            assigned_role=Task.SITE_ENGINEER, assigned_to=self.res_se)

        self.civil_co = _vendor('Sharma Civil Works')
        self.both_co = _vendor('Kumar Electricals', kind='both')
        self.supplier = _vendor('Panel Supplies', kind='supplier')
        self.gone_co = _vendor('Old Crew Co', is_active=False)

        self.storage = self.settings(SUPABASE_BILLS_BUCKET=BUCKET)
        self.storage.enable()
        self.addCleanup(self.storage.disable)
        upload_patch = mock.patch('projects.qaqc_views._client')
        self.upload_client = upload_patch.start()
        self.addCleanup(upload_patch.stop)
        discard_patch = mock.patch('projects.attendance._client')
        self.discard_client = discard_patch.start()
        self.addCleanup(discard_patch.stop)
        signer_patch = mock.patch('projects.bill_storage.get_design_file_url',
                                  return_value=SIGNED)
        self.signer = signer_patch.start()
        self.addCleanup(signer_patch.stop)
        # ATT-B1a: the "not later than now" rule reads attendance.local_now(). Pinned at
        # 20:00 IST today so the fixed times below (08:00 in, 17:30 out) never depend on
        # when the suite runs; FutureTimeTests moves it.
        clock_patch = mock.patch('projects.attendance.local_now',
                                 return_value=_at(20, 0))
        self.clock = clock_patch.start()
        self.addCleanup(clock_patch.stop)

    # -- builders -------------------------------------------------------------

    def _stored(self, n=1):
        return {'file_name': f'crew{n}.jpg', 'bucket': BUCKET,
                'path': f'{self.site.project_id}/attendance/fixture{n}.jpg', 'file_size_kb': 12}

    def _entry(self, by=None, project=None, contractor='default', unlisted='', trade='CIVIL',
               headcount=7, time_in=time(8, 0), remark='', n=1):
        """An entry made through the writer, as the add screen makes one."""
        if contractor == 'default':
            contractor = self.civil_co
        return create_attendance(
            project or self.site, (by or self.se).user, contractor=contractor,
            unlisted_name=unlisted, trade=trade, trade_remark=remark, headcount=headcount,
            time_in=time_in, stored_photo=self._stored(n), photo_taken_at=None)

    def _add_post(self, by=None, project=None, photo=True, **overrides):
        data = {'contractor': str(self.civil_co.pk), 'trade': 'STRUCTURE_MOUNTING',
                'headcount': '7', 'time_in': '08:00', 'photo_taken_at': ''}
        if photo is True:
            data['photo'] = _photo()
        elif photo:
            data['photo'] = photo
        data.update(overrides)
        return _client_for(by or self.se).post(
            reverse('attendance_add', args=[(project or self.site).project_id]), data)

    def _edit_data(self, entry, **overrides):
        data = {'contractor': str(entry.contractor_id) if entry.contractor_id else 'unlisted',
                'unlisted_contractor_name': entry.unlisted_contractor_name,
                'trade': entry.trade, 'trade_remark': entry.trade_remark,
                'headcount': str(entry.headcount), 'time_in': entry.time_in.strftime('%H:%M'),
                'time_out': entry.time_out.strftime('%H:%M') if entry.time_out else '',
                'photo_taken_at': ''}
        data.update(overrides)
        return data

    def _actions(self, entry):
        return list(entry.revisions.order_by('pk').values_list('action', flat=True))


# ---------------------------------------------------------------------------
# Permission helpers
# ---------------------------------------------------------------------------

class LogHelperTests(AttendanceFixture):

    def test_site_engineer_on_site_and_managers_may_log(self):
        for profile in (self.se, self.se2, self.pm, self.coord):
            self.assertTrue(user_can_log_attendance(profile.user, self.site), profile.role)

    def test_residential_site_engineer_may_log(self):
        self.assertTrue(user_can_log_attendance(self.res_se.user, self.res))

    def test_portfolio_roles_and_strangers_may_not(self):
        for profile in (self.finance, self.scm, self.admin, self.ceo, self.sysadmin, self.bd,
                        self.stranger, self.res_se):
            self.assertFalse(user_can_log_attendance(profile.user, self.site),
                             profile.user.username)

    def test_site_qaqc_engineer_holding_a_task_may_not(self):
        self.assertFalse(user_can_log_attendance(self.qaqc.user, self.site))

    def test_site_not_live_refuses_everyone(self):
        for status in ('Draft', 'On Hold', 'Commissioned', 'Cancelled'):
            self.site.status = status
            self.site.save(update_fields=['status'])
            self.assertFalse(user_can_log_attendance(self.se.user, self.site), status)
            self.assertFalse(user_can_log_attendance(self.pm.user, self.site), status)
        self.site.status = 'In Progress'
        self.site.save(update_fields=['status'])
        self.assertTrue(user_can_log_attendance(self.se.user, self.site))

    def test_soft_deleted_site_refuses(self):
        self.site.is_deleted = True
        self.site.save(update_fields=['is_deleted'])
        self.assertFalse(user_can_log_attendance(self.pm.user, self.site))


class ChangeHelperTests(AttendanceFixture):

    def test_today_live_entry_changeable_by_loggers_and_recorder(self):
        entry = self._entry(by=self.se)
        for profile in (self.se, self.se2, self.pm, self.coord):
            self.assertTrue(user_can_change_attendance_today(profile.user, entry), profile.role)

    def test_refused_for_non_loggers(self):
        entry = self._entry(by=self.se)
        for profile in (self.finance, self.scm, self.admin, self.ceo, self.stranger, self.qaqc):
            self.assertFalse(user_can_change_attendance_today(profile.user, entry),
                             profile.user.username)

    def test_refused_after_midnight(self):
        entry = self._entry(by=self.se)
        with mock.patch('django.utils.timezone.localdate', return_value=_tomorrow()):
            for profile in (self.se, self.pm, self.coord):
                self.assertFalse(user_can_change_attendance_today(profile.user, entry))

    def test_refused_once_voided(self):
        entry = void_attendance(self._entry(by=self.se), 'Wrong crew', self.se.user)
        self.assertFalse(user_can_change_attendance_today(self.se.user, entry))
        self.assertFalse(user_can_change_attendance_today(self.pm.user, entry))

    def test_recorder_keeps_rights_after_task_moved_off(self):
        entry = self._entry(by=self.se)
        self.se_task.assigned_to = self.se2
        self.se_task.save(update_fields=['assigned_to'])
        self.assertFalse(user_can_log_attendance(self.se.user, self.site))
        self.assertTrue(user_can_change_attendance_today(self.se.user, entry))

    def test_moved_off_engineer_has_no_rights_on_others_entries(self):
        entry = self._entry(by=self.se2)
        self.se_task.assigned_to = self.se2
        self.se_task.save(update_fields=['assigned_to'])
        self.assertFalse(user_can_change_attendance_today(self.se.user, entry))


class PhotoHelperTests(AttendanceFixture):

    def test_photo_audience(self):
        entry = self._entry(by=self.se)
        for profile in (self.se, self.pm, self.coord, self.scm, self.admin, self.ceo):
            self.assertTrue(user_can_view_attendance_photo(profile.user, entry),
                            profile.user.username)

    def test_everyone_else_sees_no_photo(self):
        entry = self._entry(by=self.se)
        for profile in (self.se2, self.finance, self.bd, self.sysadmin, self.qaqc):
            self.assertFalse(user_can_view_attendance_photo(profile.user, entry),
                             profile.user.username)


class RecorderHelperTests(AttendanceFixture):

    def test_true_for_own_live_entry_today_only(self):
        self.assertFalse(user_is_todays_attendance_recorder(self.se.user, self.site))
        entry = self._entry(by=self.se)
        self.assertTrue(user_is_todays_attendance_recorder(self.se.user, self.site))
        self.assertFalse(user_is_todays_attendance_recorder(self.se2.user, self.site))
        with mock.patch('django.utils.timezone.localdate', return_value=_tomorrow()):
            self.assertFalse(user_is_todays_attendance_recorder(self.se.user, self.site))
        void_attendance(entry, 'Wrong crew', self.se.user)
        self.assertFalse(user_is_todays_attendance_recorder(self.se.user, self.site))


# ---------------------------------------------------------------------------
# Database constraints
# ---------------------------------------------------------------------------

class ConstraintTests(AttendanceFixture):

    def _row(self, **overrides):
        values = dict(
            project=self.site, date=timezone.localdate(), contractor=self.civil_co,
            unlisted_contractor_name='', trade='CIVIL', trade_remark='', headcount=5,
            time_in=time(8, 0), time_out=None, photo_file_name='crew.jpg',
            photo_bucket=BUCKET, photo_path='p/attendance/x.jpg', recorded_by=self.se)
        values.update(overrides)
        return ContractorAttendance(**values)

    def _refused(self, **overrides):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self._row(**overrides).save()

    def test_a_valid_row_saves(self):
        self._row().save()

    def test_exactly_one_contractor_source(self):
        self._refused(unlisted_contractor_name='Raju Civil')
        self._refused(contractor=None, unlisted_contractor_name='')

    def test_headcount_range(self):
        self._refused(headcount=0)
        self._refused(headcount=501)
        self._row(headcount=500).save()

    def test_time_out_after_time_in(self):
        self._refused(time_out=time(8, 0))
        self._refused(time_out=time(7, 59))

    def test_trade_known_and_other_needs_remark(self):
        self._refused(trade='PLUMBING')
        self._refused(trade='OTHER', trade_remark='')
        self._row(trade='OTHER', trade_remark='Painting').save()

    def test_photo_required(self):
        self._refused(photo_path='')
        self._refused(photo_bucket='')
        self._refused(photo_file_name='')

    def test_void_needs_note_and_who_and_when(self):
        self._refused(is_voided=True, void_note='', voided_by=self.se, voided_at=timezone.now())
        self._refused(is_voided=True, void_note='Wrong', voided_by=None, voided_at=None)
        self._refused(is_voided=False, voided_by=self.se, voided_at=timezone.now())

    def test_one_live_entry_per_contractor_trade_day(self):
        self._row().save()
        self._refused()
        self._row(trade='ELECTRICAL').save()          # another trade
        self._row(project=self.res).save()           # another site

    def test_one_live_not_listed_entry_per_name_trade_day_case_blind(self):
        self._row(contractor=None, unlisted_contractor_name='Raju Civil').save()
        self._refused(contractor=None, unlisted_contractor_name='raju civil')

    def test_a_voided_entry_frees_the_slot(self):
        self._row(is_voided=True, void_note='Wrong', voided_by=self.se,
                  voided_at=timezone.now()).save()
        self._row().save()


class RevisionConstraintTests(AttendanceFixture):

    def test_before_only_absent_on_created(self):
        entry = self._entry()
        for action, before in (('CREATED', {'x': 1}), ('EDITED', None)):
            with self.assertRaises(IntegrityError):
                with transaction.atomic():
                    ContractorAttendanceRevision.objects.create(
                        attendance=entry, action=action, before=before, after={},
                        changed_by=self.se)

    def test_void_revision_needs_note_and_action_known(self):
        entry = self._entry()
        for action, note in (('VOIDED', ''), ('DELETED', 'x')):
            with self.assertRaises(IntegrityError):
                with transaction.atomic():
                    ContractorAttendanceRevision.objects.create(
                        attendance=entry, action=action, before={}, after={}, note=note,
                        changed_by=self.se)

    def test_append_only(self):
        revision = self._entry().revisions.get()
        revision.note = 'rewritten'
        with self.assertRaises(AppendOnlyViolation):
            revision.save()
        with self.assertRaises(AppendOnlyViolation):
            revision.delete()


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------

class WriterTests(AttendanceFixture):

    def test_every_write_has_its_revision(self):
        entry = self._entry()
        self.assertEqual(entry.date, timezone.localdate())
        entry, changed = update_attendance(entry, {
            'contractor': self.civil_co, 'trade': 'CIVIL', 'headcount': 9,
            'time_in': time(8, 0), 'time_out': None}, self.se.user)
        self.assertTrue(changed)
        entry = set_time_out(entry, time(17, 30), self.se.user)
        entry = void_attendance(entry, '  Logged on the wrong site  ', self.pm.user)
        self.assertEqual(self._actions(entry), ['CREATED', 'EDITED', 'TIME_OUT', 'VOIDED'])
        revisions = list(entry.revisions.order_by('pk'))
        self.assertIsNone(revisions[0].before)
        self.assertEqual(revisions[1].before['headcount'], 7)
        self.assertEqual(revisions[1].after['headcount'], 9)
        self.assertEqual(revisions[2].after['time_out'], '17:30')
        self.assertEqual(revisions[3].note, 'Logged on the wrong site')
        self.assertEqual(revisions[3].changed_by, self.pm)
        self.assertEqual(entry.void_note, 'Logged on the wrong site')

    def test_no_change_edit_writes_nothing(self):
        entry = self._entry()
        before_updated = entry.updated_at
        entry, changed = update_attendance(entry, {
            'contractor': self.civil_co, 'trade': 'CIVIL', 'trade_remark': 'ignored',
            'headcount': 7, 'time_in': time(8, 0), 'time_out': None}, self.se.user)
        self.assertFalse(changed)
        self.assertEqual(self._actions(entry), ['CREATED'])
        entry.refresh_from_db()
        self.assertEqual(entry.updated_at, before_updated)

    def test_duplicate_is_a_refusal_not_an_error(self):
        self._entry()
        with self.assertRaisesMessage(AttendanceRefused, DUPLICATE_ENTRY):
            self._entry(n=2)
        self.assertEqual(ContractorAttendance.objects.count(), 1)

    def test_duplicate_race_past_the_early_check_is_still_a_refusal(self):
        """Edge case 1 backstop: two requests both pass the friendly check; the partial
        unique index refuses the second, and the writer turns that into the message."""
        self._entry()
        with mock.patch('projects.attendance.live_duplicate_exists',
                        side_effect=[False, True]):
            with self.assertRaisesMessage(AttendanceRefused, DUPLICATE_ENTRY):
                self._entry(n=2)
        self.assertEqual(ContractorAttendance.objects.count(), 1)

    def test_edit_into_a_held_slot_is_refused(self):
        self._entry(trade='CIVIL')
        other = self._entry(trade='ELECTRICAL', n=2)
        with self.assertRaisesMessage(AttendanceRefused, DUPLICATE_ENTRY):
            update_attendance(other, {'contractor': self.civil_co, 'trade': 'CIVIL',
                                      'headcount': 7, 'time_in': time(8, 0),
                                      'time_out': None}, self.se.user)
        other.refresh_from_db()
        self.assertEqual(other.trade, 'ELECTRICAL')
        self.assertEqual(self._actions(other), ['CREATED'])

    def test_not_listed_names_normalised_and_collide(self):
        entry = self._entry(contractor=None, unlisted='  Raju   Civil ')
        self.assertEqual(entry.unlisted_contractor_name, 'Raju Civil')
        with self.assertRaisesMessage(AttendanceRefused, DUPLICATE_ENTRY):
            self._entry(contractor=None, unlisted=' raju  civil', n=2)
        # The backstop compares the same normalised, case-blind value.
        with mock.patch('projects.attendance.live_duplicate_exists',
                        side_effect=[False, True]):
            with self.assertRaisesMessage(AttendanceRefused, DUPLICATE_ENTRY):
                self._entry(contractor=None, unlisted='RAJU CIVIL', n=3)

    def test_writer_refuses_a_supplier_or_inactive_contractor(self):
        for vendor in (self.supplier, self.gone_co):
            with self.assertRaises(AttendanceRefused):
                self._entry(contractor=vendor)

    def test_writer_refuses_someone_who_may_not_log(self):
        with self.assertRaises(AttendanceRefused):
            self._entry(by=self.finance)

    def test_time_out_rules(self):
        entry = self._entry(time_in=time(9, 0))
        for bad in (time(9, 0), time(8, 0)):
            with self.assertRaises(AttendanceRefused):
                set_time_out(entry, bad, self.se.user)
        set_time_out(entry, time(17, 0), self.se.user)
        with self.assertRaisesMessage(AttendanceRefused, attendance.TIME_OUT_ALREADY):
            set_time_out(entry, time(18, 0), self.se.user)

    def test_void_needs_a_note_and_happens_once(self):
        entry = self._entry()
        with self.assertRaisesMessage(AttendanceRefused, attendance.VOID_NOTE_REQUIRED):
            void_attendance(entry, '   ', self.se.user)
        void_attendance(entry, 'Wrong crew', self.se.user)
        with self.assertRaises(AttendanceRefused):
            void_attendance(entry, 'Again', self.se.user)
        self.assertEqual(self._actions(entry), ['CREATED', 'VOIDED'])


# ---------------------------------------------------------------------------
# Screens
# ---------------------------------------------------------------------------

class AddScreenTests(AttendanceFixture):

    def test_add_records_entry_for_today_with_server_side_date_and_recorder(self):
        response = self._add_post(date='2020-01-01', recorded_by=str(self.pm.pk),
                                  photo_taken_at='2026:10:30 08:01:00')
        self.assertRedirects(response, reverse('attendance_today', args=['ATT-A']),
                             fetch_redirect_response=False)
        entry = ContractorAttendance.objects.get()
        self.assertEqual(entry.date, timezone.localdate())
        self.assertEqual(entry.recorded_by, self.se)
        self.assertEqual(entry.trade, 'STRUCTURE_MOUNTING')
        self.assertEqual(entry.headcount, 7)
        self.assertIsNotNone(entry.photo_taken_at)
        self.assertEqual(self._actions(entry), ['CREATED'])
        self.assertTrue(any('Attendance saved' in m for m in _messages(response)))

    def test_double_tap_save_gives_the_duplicate_message_and_one_row(self):
        """Edge case 1."""
        self._add_post()
        response = self._add_post()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'This contractor and trade is already logged today.')
        self.assertEqual(ContractorAttendance.objects.count(), 1)

    def test_headcount_out_of_range_rejected(self):
        """Edge case 6."""
        for value in ('0', '-1', '', '501', 'abc'):
            response = self._add_post(headcount=value)
            self.assertEqual(response.status_code, 200, value)
        self.assertFalse(ContractorAttendance.objects.exists())

    def test_photo_missing_or_not_an_image_rejected(self):
        """Edge case 7."""
        response = self._add_post(photo=None)
        self.assertContains(response, 'Add a group photo of the crew.')
        response = self._add_post(photo=_photo('notes.txt', b'hello', 'text/plain'))
        self.assertContains(response, 'Photo rejected')
        self.assertFalse(ContractorAttendance.objects.exists())

    def test_photo_taken_at_missing_is_null(self):
        """Edge case 12."""
        self._add_post(photo_taken_at='')
        self.assertIsNone(ContractorAttendance.objects.get().photo_taken_at)

    def test_other_trade_needs_remark(self):
        response = self._add_post(trade='OTHER', trade_remark='')
        self.assertContains(response, 'Say what the trade is when you pick Other.')
        self._add_post(trade='OTHER', trade_remark='Painting')
        self.assertEqual(ContractorAttendance.objects.get().trade_remark, 'Painting')

    def test_remark_dropped_for_a_named_trade(self):
        self._add_post(trade='CIVIL', trade_remark='left over from Other')
        self.assertEqual(ContractorAttendance.objects.get().trade_remark, '')

    def test_not_listed_names_differing_in_case_and_spaces_collide(self):
        """Edge case 8 (ruling: "Raju  Civil" and " raju civil")."""
        self._add_post(contractor='unlisted', unlisted_contractor_name='Raju  Civil')
        response = self._add_post(contractor='unlisted', unlisted_contractor_name=' raju civil')
        self.assertContains(response, 'This contractor and trade is already logged today.')
        entry = ContractorAttendance.objects.get()
        self.assertIsNone(entry.contractor)
        self.assertEqual(entry.unlisted_contractor_name, 'Raju Civil')

    def test_supplier_or_inactive_contractor_cannot_be_posted(self):
        for vendor in (self.supplier, self.gone_co):
            response = self._add_post(contractor=str(vendor.pk))
            self.assertEqual(response.status_code, 200)
        self.assertFalse(ContractorAttendance.objects.exists())

    def test_picker_lists_active_contractors_only(self):
        response = _client_for(self.se).get(reverse('attendance_add', args=['ATT-A']))
        self.assertContains(response, 'Sharma Civil Works')
        self.assertContains(response, 'Kumar Electricals')
        self.assertNotContains(response, 'Panel Supplies')
        self.assertNotContains(response, 'Old Crew Co')
        self.assertContains(response, 'Contractor not listed')
        self.assertContains(response, 'capture="environment"')

    def test_refused_write_discards_only_its_own_upload(self):
        self._entry()
        with mock.patch('projects.attendance_views.live_duplicate_exists', return_value=False):
            response = self._add_post(trade='CIVIL')
        self.assertContains(response, 'This contractor and trade is already logged today.')
        remove = self.discard_client.return_value.storage.from_.return_value.remove
        remove.assert_called_once()
        removed_path = remove.call_args[0][0][0]
        self.assertNotEqual(removed_path, ContractorAttendance.objects.get().photo_path)

    def test_residential_flow_works(self):
        """Ruling 9: no OPEX-only gate."""
        response = self._add_post(by=self.res_se, project=self.res)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(ContractorAttendance.objects.get().project, self.res)


class EditScreenTests(AttendanceFixture):

    def test_edit_records_history_and_keeps_photo_without_a_new_one(self):
        """Edge case 11, first half."""
        entry = self._entry()
        old_path = entry.photo_path
        response = _client_for(self.se).post(
            reverse('attendance_edit', args=['ATT-A', entry.pk]),
            self._edit_data(entry, headcount='9'))
        self.assertEqual(response.status_code, 302)
        entry.refresh_from_db()
        self.assertEqual(entry.headcount, 9)
        self.assertEqual(entry.photo_path, old_path)
        self.assertEqual(self._actions(entry), ['CREATED', 'EDITED'])
        page = _client_for(self.se).get(reverse('attendance_today', args=['ATT-A']))
        self.assertContains(page, 'Headcount: 7 → 9')

    def test_new_photo_replaces_path_and_keeps_old_in_history(self):
        """Edge case 11, second half: the old file is never deleted."""
        entry = self._entry()
        old_path = entry.photo_path
        _client_for(self.se).post(reverse('attendance_edit', args=['ATT-A', entry.pk]),
                                  self._edit_data(entry, photo=_photo('new.jpg')))
        entry.refresh_from_db()
        self.assertNotEqual(entry.photo_path, old_path)
        revision = entry.revisions.get(action='EDITED')
        self.assertEqual(revision.before['photo_path'], old_path)
        self.assertEqual(revision.after['photo_path'], entry.photo_path)
        self.discard_client.return_value.storage.from_.return_value.remove.assert_not_called()

    def test_no_change_edit_says_so_and_writes_no_revision(self):
        entry = self._entry()
        response = _client_for(self.se).post(
            reverse('attendance_edit', args=['ATT-A', entry.pk]), self._edit_data(entry))
        self.assertIn('Nothing changed.', _messages(response))
        self.assertEqual(self._actions(entry), ['CREATED'])

    def test_time_out_not_after_time_in_rejected_on_edit(self):
        """Edge case 5 on the edit form."""
        entry = self._entry()
        response = _client_for(self.se).post(
            reverse('attendance_edit', args=['ATT-A', entry.pk]),
            self._edit_data(entry, time_out='08:00'))
        self.assertContains(response, 'Time out must be later than time in')
        entry.refresh_from_db()
        self.assertIsNone(entry.time_out)

    def test_deactivated_contractor_still_renders_and_edit_keeps_it(self):
        """Edge case 9."""
        entry = self._entry()
        self.civil_co.is_active = False
        self.civil_co.save(update_fields=['is_active'])
        page = _client_for(self.pm).get(reverse('attendance_today', args=['ATT-A']))
        self.assertContains(page, 'Sharma Civil Works')
        add = _client_for(self.se).get(reverse('attendance_add', args=['ATT-A']))
        self.assertNotContains(add, 'Sharma Civil Works')
        response = _client_for(self.se).post(
            reverse('attendance_edit', args=['ATT-A', entry.pk]),
            self._edit_data(entry, headcount='8'))
        self.assertEqual(response.status_code, 302)
        entry.refresh_from_db()
        self.assertEqual((entry.contractor, entry.headcount), (self.civil_co, 8))

    def test_edit_form_has_no_date_project_or_recorder_field(self):
        entry = self._entry()
        response = _client_for(self.se).get(reverse('attendance_edit', args=['ATT-A', entry.pk]))
        for name in ('name="date"', 'name="project"', 'name="recorded_by"'):
            self.assertNotContains(response, name)


class TimeOutAndVoidTests(AttendanceFixture):

    def test_mark_time_out(self):
        """Edge case 5 and checklist 3."""
        entry = self._entry()
        url = reverse('attendance_time_out', args=['ATT-A', entry.pk])
        response = _client_for(self.se).post(url, {'time_out': '07:30'})
        self.assertIn(attendance.TIME_OUT_NOT_AFTER_IN, _messages(response))
        response = _client_for(self.se).post(url, {'time_out': '08:00'})
        self.assertIn(attendance.TIME_OUT_NOT_AFTER_IN, _messages(response))
        _client_for(self.se).post(url, {'time_out': '17:15'})
        entry.refresh_from_db()
        self.assertEqual(entry.time_out, time(17, 15))
        self.assertEqual(self._actions(entry), ['CREATED', 'TIME_OUT'])

    def test_time_out_is_post_only(self):
        entry = self._entry()
        response = _client_for(self.se).get(
            reverse('attendance_time_out', args=['ATT-A', entry.pk]))
        self.assertEqual(response.status_code, 405)

    def test_void_without_note_refused_then_with_note_frees_the_slot(self):
        """Edge case 4 and checklist 6."""
        entry = self._entry()
        url = reverse('attendance_void', args=['ATT-A', entry.pk])
        response = _client_for(self.se).post(url, {'void_note': ''})
        self.assertIn(attendance.VOID_NOTE_REQUIRED, _messages(response))
        entry.refresh_from_db()
        self.assertFalse(entry.is_voided)
        _client_for(self.se).post(url, {'void_note': 'Counted twice'})
        entry.refresh_from_db()
        self.assertTrue(entry.is_voided)
        self.assertEqual(entry.voided_by, self.se)

        # Voided: cannot be edited, timed out or voided again.
        edit = _client_for(self.se).get(reverse('attendance_edit', args=['ATT-A', entry.pk]))
        self.assertIn(attendance.NOT_CHANGEABLE, _messages(edit))
        _client_for(self.se).post(reverse('attendance_time_out', args=['ATT-A', entry.pk]),
                                  {'time_out': '17:00'})
        _client_for(self.se).post(url, {'void_note': 'Again'})
        entry.refresh_from_db()
        self.assertIsNone(entry.time_out)
        self.assertEqual(self._actions(entry), ['CREATED', 'VOIDED'])

        # The slot is free again.
        response = self._add_post(trade='CIVIL')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(ContractorAttendance.objects.filter(is_voided=False).count(), 1)

    def test_voided_entry_struck_through_and_not_counted(self):
        entry = self._entry(headcount=4)
        self._entry(trade='ELECTRICAL', headcount=6, n=2)
        void_attendance(entry, 'Counted twice', self.se.user)
        page = _client_for(self.se).get(reverse('attendance_today', args=['ATT-A']))
        self.assertContains(page, '1 crew · 6 people')
        self.assertContains(page, 'att-voided')
        self.assertContains(page, 'Counted twice')


class MidnightTests(AttendanceFixture):
    """Edge case 2: after midnight the same-day edit and time out refuse everyone, whether
    by the page, a direct URL or a replayed POST, and the Site Engineer cannot void.
    ATT-B2 ruling 1: the PM may now void a past-day entry with a note (a correction)."""

    def test_after_midnight_every_change_is_refused(self):
        entry = self._entry()
        with mock.patch('django.utils.timezone.localdate', return_value=_tomorrow()):
            for profile in (self.se, self.pm):
                client = _client_for(profile)
                edit = client.get(reverse('attendance_edit', args=['ATT-A', entry.pk]))
                self.assertEqual(edit.status_code, 302)
                self.assertIn(attendance.NOT_CHANGEABLE, _messages(edit))
                client.post(reverse('attendance_edit', args=['ATT-A', entry.pk]),
                            self._edit_data(entry, headcount='12'))
                client.post(reverse('attendance_time_out', args=['ATT-A', entry.pk]),
                            {'time_out': '17:00'})
            _client_for(self.se).post(reverse('attendance_void', args=['ATT-A', entry.pk]),
                                      {'void_note': 'Late void'})
            # The writers refuse a replay that skipped the views altogether.
            with self.assertRaises(AttendanceRefused):
                update_attendance(entry, {'contractor': self.civil_co, 'trade': 'CIVIL',
                                          'headcount': 12, 'time_in': time(8, 0),
                                          'time_out': None}, self.pm.user)
            with self.assertRaises(AttendanceRefused):
                set_time_out(entry, time(17, 0), self.pm.user)
            with self.assertRaises(AttendanceRefused):
                void_attendance(entry, 'Late void', self.se.user)
            # Yesterday's entries are not on today's page.
            page = _client_for(self.pm).get(reverse('attendance_today', args=['ATT-A']))
            self.assertContains(page, 'No crews logged today.')
            # ATT-B2 ruling 1: the PM's past-day void with a note succeeds.
            void_attendance(entry, 'Late void', self.pm.user)
        entry.refresh_from_db()
        self.assertEqual((entry.headcount, entry.time_out, entry.is_voided), (7, None, True))
        self.assertEqual(self._actions(entry), ['CREATED', 'VOIDED'])


class RecorderMovedOffTests(AttendanceFixture):
    """Edge case 3, ruling F."""

    def setUp(self):
        super().setUp()
        self.own = self._entry(by=self.se)
        self.others = self._entry(by=self.se2, trade='ELECTRICAL', n=2)
        self.se_task.assigned_to = self.se2
        self.se_task.save(update_fields=['assigned_to'])

    def test_page_shows_only_own_live_entries(self):
        page = _client_for(self.se).get(reverse('attendance_today', args=['ATT-A']))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, f'att-entry-{self.own.pk}')
        self.assertNotContains(page, f'att-entry-{self.others.pk}')
        self.assertNotContains(page, reverse('attendance_add', args=['ATT-A']))

    def test_overview_still_refused(self):
        response = _client_for(self.se).get(reverse('project_overview', args=['ATT-A']))
        self.assertEqual(response.status_code, 404)

    def test_can_edit_and_time_out_own_entry(self):
        client = _client_for(self.se)
        response = client.post(reverse('attendance_edit', args=['ATT-A', self.own.pk]),
                               self._edit_data(self.own, headcount='5'))
        self.assertEqual(response.status_code, 302)
        client.post(reverse('attendance_time_out', args=['ATT-A', self.own.pk]),
                    {'time_out': '16:00'})
        self.own.refresh_from_db()
        self.assertEqual((self.own.headcount, self.own.time_out), (5, time(16, 0)))

    def test_cannot_create_or_touch_others_entries(self):
        client = _client_for(self.se)
        self.assertEqual(self._add_post(trade='OTHER', trade_remark='Painting').status_code, 404)
        self.assertEqual(client.get(reverse('attendance_edit', args=['ATT-A', self.others.pk]))
                         .status_code, 404)
        self.assertEqual(client.post(reverse('attendance_void', args=['ATT-A', self.others.pk]),
                                     {'void_note': 'Not mine'}).status_code, 404)

    def test_voiding_last_own_entry_leaves_for_the_dashboard(self):
        response = _client_for(self.se).post(
            reverse('attendance_void', args=['ATT-A', self.own.pk]), {'void_note': 'Wrong site'})
        self.assertEqual(response.status_code, 302)
        self.assertNotEqual(response['Location'], reverse('attendance_today', args=['ATT-A']))
        page = _client_for(self.se).get(reverse('attendance_today', args=['ATT-A']))
        self.assertEqual(page.status_code, 404)


class AccessTests(AttendanceFixture):
    """Edge case 10."""

    def test_no_access_gets_404_on_every_url(self):
        entry = self._entry()
        client = _client_for(self.stranger)
        for name, args in (('attendance_today', ['ATT-A']), ('attendance_add', ['ATT-A']),
                           ('attendance_edit', ['ATT-A', entry.pk]),
                           ('attendance_photo', ['ATT-A', entry.pk])):
            self.assertEqual(client.get(reverse(name, args=args)).status_code, 404, name)
        for name in ('attendance_time_out', 'attendance_void'):
            self.assertEqual(client.post(reverse(name, args=['ATT-A', entry.pk]),
                                         {'time_out': '17:00', 'void_note': 'x'}).status_code,
                             404, name)

    def test_viewer_without_authority_reads_but_cannot_add_or_change(self):
        entry = self._entry()
        client = _client_for(self.finance)
        page = client.get(reverse('attendance_today', args=['ATT-A']))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'Sharma Civil Works')
        self.assertNotContains(page, reverse('attendance_add', args=['ATT-A']))
        self.assertNotContains(page, reverse('attendance_edit', args=['ATT-A', entry.pk]))
        self.assertEqual(client.get(reverse('attendance_add', args=['ATT-A'])).status_code, 403)
        client.post(reverse('attendance_void', args=['ATT-A', entry.pk]), {'void_note': 'x'})
        entry.refresh_from_db()
        self.assertFalse(entry.is_voided)

    def test_entry_of_another_site_is_404(self):
        entry = self._entry(by=self.res_se, project=self.res)
        response = _client_for(self.pm).get(reverse('attendance_edit', args=['ATT-A', entry.pk]))
        self.assertEqual(response.status_code, 404)

    def test_soft_deleted_site_is_404(self):
        self.site.is_deleted = True
        self.site.save(update_fields=['is_deleted'])
        response = _client_for(self.pm).get(reverse('attendance_today', args=['ATT-A']))
        self.assertEqual(response.status_code, 404)


class PhotoScreenTests(AttendanceFixture):
    """Ruling 8."""

    def test_photo_link_for_its_audience_only(self):
        entry = self._entry()
        url = reverse('attendance_photo', args=['ATT-A', entry.pk])
        for profile in (self.se, self.pm, self.scm, self.ceo):
            response = _client_for(profile).get(url)
            self.assertRedirects(response, SIGNED, fetch_redirect_response=False)
        for profile in (self.se2, self.finance, self.sysadmin):
            self.assertEqual(_client_for(profile).get(url).status_code, 404)

    def test_page_draws_photo_only_for_its_audience(self):
        entry = self._entry()
        photo_url = reverse('attendance_photo', args=['ATT-A', entry.pk])
        self.assertContains(_client_for(self.pm).get(reverse('attendance_today', args=['ATT-A'])),
                            photo_url)
        self.assertNotContains(_client_for(self.se2).get(reverse('attendance_today',
                                                                 args=['ATT-A'])), photo_url)


class OverviewCardTests(AttendanceFixture):

    def test_card_counts_and_add_button(self):
        self._entry(headcount=7)
        self._entry(contractor=None, unlisted='Raju Civil', headcount=3, n=2)
        response = _client_for(self.se).get(reverse('project_overview', args=['ATT-A']))
        self.assertContains(response, 'Attendance today')
        self.assertContains(response, '2 crews · 10 people')
        self.assertContains(response, '2 open')
        self.assertContains(response, '1 not listed')
        self.assertContains(response, reverse('attendance_add', args=['ATT-A']))

    def test_card_without_add_for_a_reader(self):
        response = _client_for(self.finance).get(reverse('project_overview', args=['ATT-A']))
        self.assertContains(response, 'Attendance today')
        self.assertNotContains(response, reverse('attendance_add', args=['ATT-A']))

    def test_page_header_after_first_entry(self):
        """Checklist 1: "1 crew · 7 people · 1 open"."""
        self._add_post()
        page = _client_for(self.se).get(reverse('attendance_today', args=['ATT-A']))
        self.assertContains(page, '1 crew · 7 people')
        self.assertContains(page, '1 open')
        self.assertContains(page, 'Not listed · waiting for SCM', count=0)


class FutureTimeTests(AttendanceFixture):
    """ATT-B1a: a time in or time out later than now + 15 minutes is refused by every
    writer and by the form; exactly now + 15 minutes is allowed. The clock reads 10:00 IST
    unless a test moves it."""

    def setUp(self):
        super().setUp()
        self.clock.return_value = _at(10, 0)

    def _update(self, entry, **overrides):
        data = {'contractor': self.civil_co, 'trade': 'CIVIL', 'headcount': entry.headcount,
                'time_in': entry.time_in, 'time_out': entry.time_out}
        data.update(overrides)
        return update_attendance(entry, data, self.se.user)

    # -- create_attendance --------------------------------------------------------

    def test_create_refuses_time_in_past_the_grace(self):
        with self.assertRaisesMessage(AttendanceRefused, attendance.TIME_LATER_THAN_NOW):
            self._entry(time_in=time(10, 16))
        self.assertFalse(ContractorAttendance.objects.exists())

    def test_create_allows_time_in_at_the_15_minute_boundary(self):
        entry = self._entry(time_in=time(10, 15))
        self.assertEqual(entry.time_in, time(10, 15))

    def test_boundary_holds_when_now_has_seconds(self):
        """10:00:30 -> latest 10:15:30: 10:15 is in, 10:16 is out."""
        self.clock.return_value = _at(10, 0, 30)
        self._entry(time_in=time(10, 15))
        with self.assertRaisesMessage(AttendanceRefused, attendance.TIME_LATER_THAN_NOW):
            self._entry(time_in=time(10, 16), trade='ELECTRICAL', n=2)

    # -- update_attendance --------------------------------------------------------

    def test_update_refuses_time_in_or_time_out_past_the_grace(self):
        entry = self._entry(time_in=time(8, 0))
        for overrides in ({'time_in': time(10, 16)}, {'time_out': time(10, 16)}):
            with self.assertRaisesMessage(AttendanceRefused, attendance.TIME_LATER_THAN_NOW):
                self._update(entry, **overrides)
        entry.refresh_from_db()
        self.assertEqual((entry.time_in, entry.time_out), (time(8, 0), None))
        self.assertEqual(self._actions(entry), ['CREATED'])

    def test_update_allows_the_boundary(self):
        entry = self._entry(time_in=time(8, 0))
        entry, changed = self._update(entry, time_in=time(10, 0), time_out=time(10, 15))
        self.assertTrue(changed)
        self.assertEqual((entry.time_in, entry.time_out), (time(10, 0), time(10, 15)))

    # -- set_time_out -------------------------------------------------------------

    def test_set_time_out_refuses_past_the_grace_and_allows_the_boundary(self):
        entry = self._entry(time_in=time(8, 0))
        with self.assertRaisesMessage(AttendanceRefused, attendance.TIME_LATER_THAN_NOW):
            set_time_out(entry, time(10, 16), self.se.user)
        entry.refresh_from_db()
        self.assertIsNone(entry.time_out)
        entry = set_time_out(entry, time(10, 15), self.se.user)
        self.assertEqual(entry.time_out, time(10, 15))
        self.assertEqual(self._actions(entry), ['CREATED', 'TIME_OUT'])

    # -- near midnight ------------------------------------------------------------

    def test_grace_crossing_midnight_refuses_nothing_left_today(self):
        """23:50 + 15 min is 00:05 tomorrow: 23:59 today is not "later than now + 15"."""
        self.clock.return_value = _at(23, 50)
        entry = self._entry(time_in=time(23, 0))
        set_time_out(entry, time(23, 59), self.se.user)
        entry.refresh_from_db()
        self.assertEqual(entry.time_out, time(23, 59))

    # -- the screens --------------------------------------------------------------

    def test_add_form_says_time_cant_be_later_than_now(self):
        response = self._add_post(time_in='10:16')
        self.assertContains(response, 'Time can&#x27;t be later than now.')
        self.assertFalse(ContractorAttendance.objects.exists())
        self.upload_client.return_value.storage.from_.return_value.upload.assert_not_called()
        self.assertEqual(self._add_post(time_in='10:15').status_code, 302)

    def test_edit_form_refuses_a_future_time_out(self):
        entry = self._entry(time_in=time(8, 0))
        response = _client_for(self.se).post(
            reverse('attendance_edit', args=['ATT-A', entry.pk]),
            self._edit_data(entry, time_out='10:30'))
        self.assertContains(response, 'Time can&#x27;t be later than now.')
        entry.refresh_from_db()
        self.assertIsNone(entry.time_out)

    def test_mark_time_out_screen_refuses_a_future_time(self):
        entry = self._entry(time_in=time(8, 0))
        response = _client_for(self.se).post(
            reverse('attendance_time_out', args=['ATT-A', entry.pk]), {'time_out': '11:00'})
        self.assertIn(attendance.TIME_LATER_THAN_NOW, _messages(response))
        entry.refresh_from_db()
        self.assertIsNone(entry.time_out)
