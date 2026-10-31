"""
Contractor attendance — ATT-B4: the "no attendance logged" alert to PMs and SCM.

WHAT IS PINNED HERE
-------------------
    which sites are missing attendance (ruling 1)    SelectionTests
    who is told about which sites (ruling 4)         GroupingTests
    the site list and the email lines (ruling 6)     FormatTests
    the event: registered, paused, email only        EventTests
    the command: dry run, interlock, sends, the
      per-channel once-a-day check, Sunday, pause    CommandTests
    the WhatsApp leg and its parameter order         WhatsAppTests
    queries do not grow with the number of sites     QueryBoundTests

"Now" is Monday 2 Nov 2026, 10:30 AM IST, by patching django.utils.timezone.now, which
timezone.localdate(), timezone.localtime() and NotificationLog.created_at all read.
ContractorAttendance.recorded_at's default is bound at import, so it keeps the real time;
nothing here reads it. Outbound HTTP is mocked at notifications._zeptomail_post (email) and
notifications.requests.post (WhatsApp); nothing reaches ZeptoMail or Interakt.

Run with:
    python manage.py test projects.tests_attendance_b4 --settings=solarpms.test_settings
"""
from datetime import datetime, time, timezone as dt_timezone
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from .attendance_alerts import (
    EVENT_CODE, format_site_lines, format_site_list, group_by_recipient,
    sites_missing_attendance, sites_without_pm,
)
from .models import (
    ContractorAttendance, Notification, NotificationEventSetting, NotificationLog, Program,
    Project, ProjectPhase, SystemSettings, Task, TaskTemplate, TaskTemplatePhase,
    TaskTemplateTask, Vendor,
)
from .notify import channel_is_active, notify
from .notify_admin import TEST_TEMPLATE_NAME
from .notify_registry import get_event

# Monday 2 Nov 2026, 10:30 IST.
MONDAY_1030 = datetime(2026, 11, 2, 5, 0, tzinfo=dt_timezone.utc)
MONDAY = MONDAY_1030.date()


def _profile(username, role, phone='', active=True, first='', last='Test'):
    user = User.objects.create_user(username=username, password='x',
                                    email=f'{username}@example.test',
                                    first_name=first or username.title(), last_name=last)
    profile = user.profile
    profile.role = role
    profile.is_active = active
    profile.phone_number = phone
    profile.save()
    return profile


class B4Fixture(TestCase):
    """
    One OPEX template with an INSTALLATION and a DESIGN phase. Sites are made by _site():
    each has an Installation phase holding one templated task in the status asked for.
    People: pm, pm2 (PMs), coord (coordinator on every site), scm and scm2 (SCM, scm has
    a phone), se (records attendance), admin.
    """

    def setUp(self):
        clock = mock.patch('django.utils.timezone.now', return_value=MONDAY_1030)
        clock.start()
        self.addCleanup(clock.stop)

        self.pm = _profile('b4_pm', 'PM', first='Ravi', last='Kumar Singh')
        self.pm2 = _profile('b4_pm2', 'PM')
        self.coord = _profile('b4_coord', 'Project Coordinator')
        self.scm = _profile('b4_scm', 'SCM', phone='9876500001', first='Ishmeet')
        self.scm2 = _profile('b4_scm2', 'SCM')
        self.se = _profile('b4_se', 'Site Engineer')
        self.admin = _profile('b4_admin', 'Admin')

        self.program = Program.objects.create(name='Test-B4', program_type='OPEX',
                                              client_name='B4Client', status='Active',
                                              short_tender_code='B4')
        template = TaskTemplate.objects.create(code='OPEX', label='OPEX', project_type='OPEX',
                                               version_no=1)
        install = TaskTemplatePhase.objects.create(template=template, code='INSTALLATION',
                                                   label='Installation', sort_order=4)
        design = TaskTemplatePhase.objects.create(template=template, code='DESIGN',
                                                  label='Design', sort_order=1)
        self.install_tt = TaskTemplateTask.objects.create(
            phase=install, code='STRUCTURE_INSTALLATION', label='Structure', sort_order=1,
            assigned_role=Task.SITE_ENGINEER)
        self.design_tt = TaskTemplateTask.objects.create(
            phase=design, code='DESIGN_LAYOUT', label='Layout', sort_order=1)
        self.vendor = Vendor.objects.create(name='Sharma Civil Works', contact_person='C',
                                            phone='9876543210', kind='contractor')

    # -- builders -------------------------------------------------------------

    def _site(self, code, status=Task.IN_PROGRESS, pm='default', project_type='OPEX',
              site_status='Active'):
        """A live site whose Installation phase holds one templated task in `status`."""
        pm = self.pm if pm == 'default' else pm
        extra = ({'program': self.program, 'site_code': code} if project_type == 'OPEX'
                 else {})
        site = Project.objects.create(
            project_id=code, customer_name='B4Client', customer_phone='9876543210',
            site_address='1 Sun Rd', city='Delhi', project_type=project_type,
            dc_capacity_kw=Decimal('100.00'), status=site_status, assigned_pm=pm, **extra)
        site.coordinators.add(self.coord)
        phase = ProjectPhase.objects.create(project=site, phase_name='Installation',
                                            phase_order=4)
        site.install_task = Task.objects.create(
            phase=phase, task_name='Structure', task_order=1, assigned_role=Task.SITE_ENGINEER,
            template_task=self.install_tt, status=status)
        site.install_phase = phase
        return site

    def _attendance(self, site, day=MONDAY, voided=False, time_in=time(10, 29)):
        extra = ({'is_voided': True, 'void_note': 'Wrong site', 'voided_by': self.pm,
                  'voided_at': MONDAY_1030} if voided else {})
        return ContractorAttendance.objects.create(
            project=site, date=day, contractor=self.vendor, trade='CIVIL', headcount=4,
            time_in=time_in, photo_file_name='crew.jpg', photo_bucket='b',
            photo_path=f'{site.project_id}/attendance/x.jpg', recorded_by=self.se, **extra)

    def _codes(self, day=MONDAY):
        return [s.project_id for s in sites_missing_attendance(day)]

    def _email_on(self):
        NotificationEventSetting.objects.create(event_code=EVENT_CODE, channel='email',
                                                is_active=True, changed_by=self.admin,
                                                reason='Tested')

    def _switches(self, email=True, whatsapp=False):
        switches = SystemSettings.get()
        switches.email_enabled = email
        switches.whatsapp_enabled = whatsapp
        switches.save()

    def _run(self, *args):
        """The command, with on-commit callbacks run (notify() sends after commit)."""
        out, err = StringIO(), StringIO()
        with self.captureOnCommitCallbacks(execute=True):
            call_command('send_attendance_alerts', *args, stdout=out, stderr=err)
        return out.getvalue() + err.getvalue()

    def _send(self, *args):
        return self._run('--i-am-sending-to-real-people', *args)

    def _rows(self, **filters):
        return NotificationLog.objects.filter(event_code=EVENT_CODE, **filters)


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

class SelectionTests(B4Fixture):

    def test_in_progress_installation_without_attendance_is_listed(self):
        self._site('B4-A')
        self.assertEqual(self._codes(), ['B4-A'])

    def test_attendance_logged_before_the_run_unlists_the_site(self):
        """Edge case 1: logged at 10:29, run at 10:30."""
        site = self._site('B4-A')
        self._attendance(site, time_in=time(10, 29))
        self.assertEqual(self._codes(), [])

    def test_only_voided_attendance_still_lists_the_site(self):
        """Edge case 2."""
        site = self._site('B4-A')
        self._attendance(site, voided=True)
        self.assertEqual(self._codes(), ['B4-A'])

    def test_blocked_or_not_started_installation_is_not_listed(self):
        """Edge case 3: "started" is In Progress; Blocked does not count."""
        self._site('B4-BLOCKED', status=Task.BLOCKED)
        self._site('B4-NOT-STARTED', status=Task.NOT_STARTED)
        self._site('B4-DONE', status=Task.DONE)
        self.assertEqual(self._codes(), [])

    def test_soft_deleted_and_completed_sites_are_not_listed(self):
        """Edge case 4."""
        deleted = self._site('B4-DEL')
        Project.objects.filter(pk=deleted.pk).update(is_deleted=True)
        self._site('B4-DONE', site_status='Completed')
        self._site('B4-INPROG', site_status='In Progress')
        self.assertEqual(self._codes(), ['B4-INPROG'])

    def test_in_progress_work_in_another_phase_does_not_count(self):
        site = self._site('B4-A', status=Task.NOT_STARTED)
        design = ProjectPhase.objects.create(project=site, phase_name='Design', phase_order=1)
        Task.objects.create(phase=design, task_name='Layout', task_order=1,
                            template_task=self.design_tt, status=Task.IN_PROGRESS)
        self.assertEqual(self._codes(), [])

    def test_a_hand_added_task_in_the_installation_phase_counts(self):
        """Phase identity is phase_rules': any template task in the phase names it."""
        site = self._site('B4-A', status=Task.NOT_STARTED)
        Task.objects.create(phase=site.install_phase, task_name='Extra earthing',
                            task_order=2, status=Task.IN_PROGRESS)
        self.assertEqual(self._codes(), ['B4-A'])

    def test_a_phase_of_only_hand_added_tasks_is_not_installation(self):
        site = self._site('B4-A', status=Task.NOT_STARTED)
        loose = ProjectPhase.objects.create(project=site, phase_name='Installation',
                                            phase_order=5)
        Task.objects.create(phase=loose, task_name='Loose', task_order=1,
                            status=Task.IN_PROGRESS)
        self.assertEqual(self._codes(), [])

    def test_not_applicable_and_mirror_tasks_do_not_count(self):
        na = self._site('B4-NA')
        Task.objects.filter(pk=na.install_task.pk).update(is_not_applicable=True)
        mirror = self._site('B4-MIRROR')
        Task.objects.filter(pk=mirror.install_task.pk).update(is_mirror=True)
        self.assertEqual(self._codes(), [])

    def test_a_residential_site_with_installation_in_progress_is_listed(self):
        """Ruling 11: Residential has an identifiable Installation phase, so it is in."""
        self._site('B4-RES', project_type='Residential')
        self.assertEqual(self._codes(), ['B4-RES'])

    def test_attendance_on_another_day_does_not_count(self):
        site = self._site('B4-A')
        self._attendance(site, day=MONDAY.replace(day=1))
        self.assertEqual(self._codes(), ['B4-A'])

    def test_sites_come_in_code_order(self):
        for code in ('B4-C', 'B4-A', 'B4-B'):
            self._site(code)
        self.assertEqual(self._codes(), ['B4-A', 'B4-B', 'B4-C'])


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------

class GroupingTests(B4Fixture):

    def _groups(self):
        return {(p.user.username, role): [s.project_id for s in sites]
                for (p, role), sites in group_by_recipient(sites_missing_attendance(MONDAY)).items()}

    def test_a_pm_with_three_missing_sites_gets_one_group(self):
        """Edge case 5."""
        for code in ('B4-A', 'B4-B', 'B4-C'):
            self._site(code)
        self._site('B4-D', pm=self.pm2)
        self.assertEqual(self._groups(), {
            ('b4_pm', 'PM'): ['B4-A', 'B4-B', 'B4-C'],
            ('b4_pm2', 'PM'): ['B4-D'],
            ('b4_scm', 'SCM'): ['B4-A', 'B4-B', 'B4-C', 'B4-D'],
            ('b4_scm2', 'SCM'): ['B4-A', 'B4-B', 'B4-C', 'B4-D'],
        })

    def test_coordinators_are_not_told(self):
        self._site('B4-A')
        self.assertNotIn('b4_coord', {name for name, _ in self._groups()})

    def test_a_site_without_a_pm_still_reaches_scm(self):
        """Edge case 6: the gap is printed by the command."""
        self._site('B4-NOPM', pm=None)
        self.assertEqual(self._groups(), {('b4_scm', 'SCM'): ['B4-NOPM'],
                                          ('b4_scm2', 'SCM'): ['B4-NOPM']})
        self.assertEqual([s.project_id for s in sites_without_pm(sites_missing_attendance(MONDAY))],
                         ['B4-NOPM'])
        self.assertIn('[gap] B4-NOPM: no active assigned PM; SCM is still told',
                      self._run('--dry-run'))

    def test_inactive_pm_and_scm_users_are_left_out(self):
        """Edge case 11: either active flag off is enough."""
        self._site('B4-A')
        self.pm.is_active = False
        self.pm.save()
        self.scm2.user.is_active = False
        self.scm2.user.save()
        self.assertEqual(self._groups(), {('b4_scm', 'SCM'): ['B4-A']})

    def test_nothing_missing_means_no_groups(self):
        self.assertEqual(group_by_recipient([]), {})


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

class FormatTests(B4Fixture):

    def test_twenty_sites_show_fifteen_codes_and_five_more(self):
        """Edge case 7."""
        for n in range(20):
            self._site(f'B4-{n:02d}')
        sites = sites_missing_attendance(MONDAY)
        text = format_site_list(sites)
        self.assertEqual(text, ', '.join(f'B4-{n:02d}' for n in range(15)) + ' and 5 more')
        self.assertNotIn('\n', text)

    def test_the_limit_is_the_setting(self):
        for n in range(4):
            self._site(f'B4-{n}')
        with override_settings(ATTENDANCE_ALERT_SITE_LIST_LIMIT=2):
            self.assertEqual(format_site_list(sites_missing_attendance(MONDAY)),
                             'B4-0, B4-1 and 2 more')

    def test_short_list_has_no_more_suffix(self):
        self._site('B4-A')
        self._site('B4-B')
        self.assertEqual(format_site_list(sites_missing_attendance(MONDAY)), 'B4-A, B4-B')

    @override_settings(APP_BASE_URL='https://pms.example.test')
    def test_site_lines_link_each_site_to_its_attendance_page(self):
        for n in range(17):
            self._site(f'B4-{n:02d}')
        lines = format_site_lines(sites_missing_attendance(MONDAY)).split('\n')
        self.assertEqual(lines[0], 'B4-00 – https://pms.example.test/projects/B4-00/attendance/')
        self.assertEqual(len(lines), 16)
        self.assertEqual(lines[-1], 'and 2 more')


# ---------------------------------------------------------------------------
# The event
# ---------------------------------------------------------------------------

class EventTests(B4Fixture):

    def test_registered_paused_on_email_only(self):
        event = get_event(EVENT_CODE)
        self.assertEqual((event.default_state, event.channels, event.module),
                         ('paused', ('email',), 'Attendance'))
        self.assertFalse(channel_is_active(event, 'email'))


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------

@mock.patch('projects.notifications._zeptomail_post', return_value=(True, ''))
class CommandTests(B4Fixture):

    def setUp(self):
        super().setUp()
        self._switches(email=True)

    def test_dry_run_sends_and_writes_nothing_and_needs_no_flag(self, post):
        """Ruling 10."""
        self._site('B4-A')
        self._email_on()
        out = self._run('--dry-run')
        self.assertIn('[dry-run] 3 recipient(s). Nothing sent, nothing written.', out)
        self.assertIn('b4_pm (PM): channels in_app, email', out)
        self.assertIn('already sent today: nothing; sites: B4-A', out)
        self.assertIn('b4_scm (SCM): channels in_app, email', out)
        self.assertEqual(NotificationLog.objects.count(), 0)
        self.assertEqual(Notification.objects.count(), 0)
        post.assert_not_called()

    def test_a_real_run_without_the_flag_refuses(self, post):
        self._site('B4-A')
        with self.assertRaises(SystemExit) as raised:
            self._run()
        self.assertEqual(raised.exception.code, 1)
        self.assertEqual(NotificationLog.objects.count(), 0)

    @override_settings(APP_BASE_URL='https://pms.example.test')
    def test_one_message_per_recipient_listing_their_sites(self, post):
        """Edge case 5, sent: one email per person, the PM's listing all three sites."""
        for code in ('B4-A', 'B4-B', 'B4-C'):
            self._site(code)
        self._email_on()
        out = self._send()
        self.assertIn('Done. sites missing=3 recipients=3 in-app/email sends=3', out)
        self.assertEqual(post.call_count, 3)
        to_pm = [c for c in post.call_args_list if c.args[0] == 'b4_pm@example.test']
        self.assertEqual(len(to_pm), 1)
        _, _, subject, text, html = to_pm[0].args
        self.assertEqual(subject, 'Attendance not logged by 10:30 AM – 2 Nov')
        self.assertIn('Hello Ravi Kumar Singh,', text)
        self.assertIn('By 10:30 AM today no contractor attendance has been logged', text)
        self.assertIn('B4-C – https://pms.example.test/projects/B4-C/attendance/', text)
        self.assertIn('https://pms.example.test/dashboard/pm/', html)
        self.assertEqual(self._rows(channel='email', status='sent').count(), 3)
        self.assertEqual(self._rows(channel='in_app', status='sent').count(), 3)
        self.assertEqual(Notification.objects.get(recipient=self.scm).link, '/dashboard/scm/')

    def test_a_second_run_the_same_day_sends_nothing(self, post):
        """Edge case 8."""
        self._site('B4-A')
        self._email_on()
        self._send()
        rows = NotificationLog.objects.count()
        out = self._send()
        self.assertIn('[skip] b4_pm (PM): already sent today', out)
        self.assertIn('skipped (already sent)=3', out)
        self.assertEqual(NotificationLog.objects.count(), rows)
        self.assertEqual(post.call_count, 3)

    def test_sunday_sends_nothing_and_says_why(self, post):
        """Edge case 9."""
        self._site('B4-A')
        self._email_on()
        out = self._send('--date', '2026-11-01')
        self.assertIn('Skipped: Sunday is in ATTENDANCE_ALERT_SKIP_WEEKDAYS. Nothing sent.', out)
        self.assertEqual(NotificationLog.objects.count(), 0)

    def test_a_paused_event_reports_paused_not_sent(self, post):
        """Edge case 10 and ruling R2: in-app still goes (notify() ignores the pause for
        in-app); email is logged skipped; a second run adds nothing."""
        self._site('B4-A')
        out = self._send()
        self.assertIn('email paused=3', out)
        self.assertIn('[send] b4_pm (PM): in_app [email paused;', out)
        post.assert_not_called()
        self.assertEqual(self._rows(channel='email', status='skipped',
                                    error_detail='event paused').count(), 3)
        self.assertEqual(self._rows(channel='in_app', status='sent').count(), 3)
        before = NotificationLog.objects.count()
        self.assertIn('skipped (already sent)=3', self._send())
        self.assertEqual(NotificationLog.objects.count(), before)

    def test_email_switched_on_between_runs_is_sent_on_the_second(self, post):
        """Ruling R2: the check is per channel, so the email that was paused goes later."""
        self._site('B4-A')
        self._send()
        self._email_on()
        self._send()
        self.assertEqual(self._rows(channel='email', status='sent').count(), 3)
        self.assertEqual(post.call_count, 3)

    def test_nothing_missing_sends_nothing(self, post):
        """Ruling 8."""
        site = self._site('B4-A')
        self._attendance(site)
        self.assertIn('No site is missing attendance. Nothing sent.', self._send())
        self.assertEqual(NotificationLog.objects.count(), 0)

    def test_just_after_midnight_uses_the_ist_date(self, post):
        """Edge case 12: 19:00 UTC on 2 Nov is 00:30 IST on 3 Nov."""
        site = self._site('B4-A')
        self._attendance(site, day=MONDAY)
        with mock.patch('django.utils.timezone.now',
                        return_value=datetime(2026, 11, 2, 19, 0, tzinfo=dt_timezone.utc)):
            out = self._run('--dry-run')
        self.assertIn('[date] 2026-11-03 (Tuesday), cutoff 12:30 AM', out)
        self.assertIn('[sites] 1 missing attendance: B4-A', out)

    def test_an_admin_test_send_does_not_count_as_sent(self, post):
        self._site('B4-A')
        self._email_on()
        NotificationLog.objects.create(recipient=self.pm, channel='email', status='sent',
                                       message='x', template_name=TEST_TEMPLATE_NAME,
                                       event_code=EVENT_CODE)
        self.assertIn('b4_pm (PM): channels in_app, email', self._run('--dry-run'))

    def test_one_failing_recipient_does_not_stop_the_run(self, post):
        self._site('B4-A')

        def flaky(event_code, *, recipients, **kwargs):
            if recipients[0] == self.pm:
                raise RuntimeError('boom')
            return notify(event_code, recipients=recipients, **kwargs)

        with mock.patch('projects.management.commands.send_attendance_alerts.notify',
                        side_effect=flaky):
            out = self._send()
        self.assertIn('[fail] b4_pm (PM): RuntimeError: boom', out)
        self.assertIn('failed=1', out)
        self.assertEqual(self._rows(channel='in_app', status='sent').count(), 2)

    def test_a_bad_date_is_refused(self, post):
        with self.assertRaises(CommandError):
            self._run('--dry-run', '--date', '2-11-2026')


# ---------------------------------------------------------------------------
# WhatsApp
# ---------------------------------------------------------------------------

def _interakt_ok():
    response = mock.Mock(status_code=201, text='{"id": "m1"}')
    response.json.return_value = {'id': 'm1'}
    return response


@mock.patch('projects.notifications._zeptomail_post', return_value=(True, ''))
@mock.patch('projects.notifications.requests.post', return_value=_interakt_ok())
@override_settings(INTERAKT_API_KEY='test-key')
class WhatsAppTests(B4Fixture):

    def setUp(self):
        super().setUp()
        self._switches(email=True, whatsapp=True)

    def test_off_by_default(self, wa_post, email_post):
        self._site('B4-A')
        out = self._send()
        self.assertIn('whatsapp off (ATTENDANCE_ALERT_WHATSAPP_ENABLED)', out)
        wa_post.assert_not_called()

    @override_settings(ATTENDANCE_ALERT_WHATSAPP_ENABLED=True)
    def test_parameters_in_the_template_order(self, wa_post, email_post):
        """Header {{1}} the date; body {{1}} first name, {{2}} cutoff, {{3}} site list."""
        for n in range(17):
            self._site(f'B4-{n:02d}')
        self._send()
        wa_post.assert_called_once()
        payload = wa_post.call_args.kwargs['json']
        self.assertEqual(payload['phoneNumber'], '9876500001')
        self.assertEqual(payload['template']['name'], 'contractor_attendance_missing')
        self.assertEqual(payload['template']['headerValues'], ['2 Nov'])
        sites = ', '.join(f'B4-{n:02d}' for n in range(15)) + ' and 2 more'
        self.assertEqual(payload['template']['bodyValues'], ['Ishmeet', '10:30 AM', sites])
        for value in payload['template']['headerValues'] + payload['template']['bodyValues']:
            self.assertNotIn('\n', value)
        row = self._rows(channel='whatsapp').get()
        self.assertEqual((row.recipient, row.status, row.template_name),
                         (self.scm, 'sent', 'contractor_attendance_missing'))

    @override_settings(ATTENDANCE_ALERT_WHATSAPP_ENABLED=True)
    def test_no_phone_gets_no_whatsapp(self, wa_post, email_post):
        """Ruling 4: a user with no phone is not sent WhatsApp (and no failed row)."""
        self._site('B4-A')
        out = self._send()
        self.assertIn('[send] b4_pm (PM): in_app [email paused; whatsapp: no phone]', out)
        self.assertIn('no phone=2', out)
        self.assertEqual(self._rows(channel='whatsapp').count(), 1)

    @override_settings(ATTENDANCE_ALERT_WHATSAPP_ENABLED=True)
    def test_whatsapp_is_sent_once_a_day(self, wa_post, email_post):
        self._site('B4-A')
        self._send()
        self._send()
        wa_post.assert_called_once()

    @override_settings(ATTENDANCE_ALERT_WHATSAPP_ENABLED=True)
    def test_first_name_is_the_first_word_of_the_full_name(self, wa_post, email_post):
        self.scm.user.first_name = 'Ishmeet Kaur'
        self.scm.user.save()
        self._site('B4-A')
        self._send()
        self.assertEqual(wa_post.call_args.kwargs['json']['template']['bodyValues'][0],
                         'Ishmeet')


# ---------------------------------------------------------------------------
# Query bound
# ---------------------------------------------------------------------------

class QueryBoundTests(B4Fixture):

    def test_selection_is_one_query(self):
        for n in range(5):
            self._site(f'B4-{n}')
        with self.assertNumQueries(1):
            sites_missing_attendance(MONDAY)

    def test_grouping_is_one_query(self):
        for n in range(5):
            self._site(f'B4-{n}', pm=self.pm if n % 2 else self.pm2)
        sites = sites_missing_attendance(MONDAY)
        with self.assertNumQueries(1):
            group_by_recipient(sites)

    def _dry_run_queries(self):
        with CaptureQueriesContext(connection) as ctx:
            self._run('--dry-run')
        return len(ctx.captured_queries)

    def test_the_dry_run_does_not_grow_with_the_number_of_sites(self):
        # SystemSettings.get() creates its row on first use; made here so the first
        # measured run does not pay that insert and the second does not.
        SystemSettings.get()
        self._site('B4-00')
        self._site('B4-01')
        few = self._dry_run_queries()
        for n in range(2, 12):
            self._site(f'B4-{n:02d}')
        self.assertEqual(self._dry_run_queries(), few)
