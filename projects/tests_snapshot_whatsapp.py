"""
SNAP-2 — the daily snapshot on WhatsApp (template daily_snapshot_pm, image header).

WHAT THIS PINS
    Sanitiser      an image header's URL is passed through whole (never cut to 60
                   characters, never "cleaned"); body values still are; a text header is
                   cut exactly as before.
    Registry       an image spec has no header text and a body-only builder; a
                   command_only event may declare WhatsApp without being deferred, may
                   not be deferred; any other non-deferred event declaring WhatsApp is
                   still refused; the snapshot event declares the approved template.
    notify()       refuses a command_only event; the event code is named only by the
                   registry, management commands and tests (no view can fire it).
    Payloads       interakt_payload() and an existing deferred event (dc_created) post
                   byte-identical JSON, through the pending row as before.
    Command        image header = the signed link, body {{1}} = the collection time; two
                   images -> two messages per recipient, in order; no valid number ->
                   failed row with the plain reason, the others still sent, exit
                   non-zero; paused or master switch off -> skipped, nothing uploaded,
                   email still sent, exit 0; a failed upload -> failed rows, email sent,
                   exit non-zero; --to -> one person on both channels; rows name the
                   storage path, never the link; a new object per run; 7-day link;
                   --dry-run uploads nothing and prints each message.
    Storage        upload_private_bytes() posts to the design bucket without upsert.
    Phone test     test_whatsapp --image: required for the image event, refused for
                   others; uploads only past the switch checks; the [TEST] row opens the
                   14-day gate; nothing printed or logged holds the link.

INTERCEPTION. Interakt is `projects.notifications.requests.post` (a recorder), ZeptoMail
`_zeptomail_post`; storage is the command module's `upload_private_bytes` and
`get_design_file_url`; any other HTTP request fails the test. No network, ever.

Run with:
    python manage.py test projects.tests_snapshot_whatsapp --settings=solarpms.test_settings
"""
import dataclasses
import io
import json
import re
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest import mock

import requests
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase, override_settings

from . import notify as N
from . import notify_admin as A
from .design_storage import DESIGN_BUCKET, DesignStorageError, upload_private_bytes
from .management.commands import send_dashboard_snapshot as C
from .models import NotificationEventSetting, NotificationLog, SystemSettings
from .notifications import NO_VALID_WHATSAPP_NUMBER, interakt_payload, send_notification
from .notify_admin import add_named_recipient
from .notify_registry import (
    HEADER_MEDIA_IMAGE, NOTIFICATION_EVENTS, SNAPSHOT_WA_BODY, TIER_KNOW,
    NotificationEvent, Variable, WhatsAppSpec, _build_registry, get_event,
)
from .tests_dashboard_snapshot import NOW, PNG_SIGNATURE, SnapshotFixture
from .tests_two_step_completion import _profile
from .whatsapp_params import HEADER_LIMIT, sanitise_params

EVENT = 'daily_dashboard_snapshot'
TAKEN_AT = '9 Nov 2026, 8:00 PM IST'           # NOW as taken_at_label prints it
PATH_1 = 'snapshots/2026-11-09/site-dashboard-2026-11-09-1-200000.png'
PATH_2 = 'snapshots/2026-11-09/site-dashboard-2026-11-09-2-200000.png'
CMD = 'projects.management.commands.send_dashboard_snapshot'


def _signed(bucket, path, expires_in):
    """A signed link longer than HEADER_LIMIT, with characters clean_value would touch
    if it were text (4+ spaces would be squeezed), so a cut or a clean shows."""
    return (f'https://dev.supabase.co/storage/v1/object/sign/{bucket}/{path}'
            f'?token=eyJ{"x" * 180}.sig&exp={expires_in}')


def _response():
    response = mock.Mock(status_code=201, text='{"result": true, "id": "wamid-1"}')
    response.json.return_value = {'id': 'wamid-1'}
    return response


# ---------------------------------------------------------------------------
# 1. Sanitiser (P3)
# ---------------------------------------------------------------------------

class MediaHeaderSanitiseTests(SimpleTestCase):

    URL = _signed('Horizon-PMS-Design', PATH_1, 604800)

    def test_the_url_is_passed_whole(self):
        self.assertGreater(len(self.URL), HEADER_LIMIT)
        out = sanitise_params([self.URL, TAKEN_AT], body_fixed_chars=64, media_header=True)
        self.assertEqual(out, [self.URL, TAKEN_AT])

    def test_body_values_are_still_cleaned_and_cut(self):
        out = sanitise_params([self.URL, ' 9 Nov\n2026 ', 'x' * 400], media_header=True)
        self.assertEqual(out[0], self.URL)
        self.assertEqual(out[1], '9 Nov 2026')
        self.assertEqual(len(out[2]), 250)

    def test_a_text_header_is_cut_as_before(self):
        out = sanitise_params([self.URL, TAKEN_AT])
        self.assertEqual(len(out[0]), HEADER_LIMIT)
        self.assertTrue(out[0].endswith('…'))


# ---------------------------------------------------------------------------
# 2. Registry (P2, T2)
# ---------------------------------------------------------------------------

def _image_event(**changes):
    spec = WhatsAppSpec(template='snap2_test', language='en', header_text='',
                        body_text='As of {{1}}. Thanks', header_fixed_chars=0,
                        body_fixed_chars=len('As of . Thanks'), param_count=2,
                        params=lambda v: [v['taken_at']], header_media=HEADER_MEDIA_IMAGE)
    event = NotificationEvent(
        code='snap2_test', label='SNAP-2 test', module='Tests', tier=TIER_KNOW,
        channels=('email', 'whatsapp'),
        variables=(Variable('taken_at', 'When', '9 Nov 2026, 8:00 PM IST'),),
        in_app_text='As of {{ taken_at }}.', default_subject='S', default_body='B',
        button_label='', recipients_rule='Tests only.', default_state='paused',
        command_only=True, whatsapp=spec)
    spec_changes = {k: changes.pop(k) for k in list(changes)
                    if k in {f.name for f in dataclasses.fields(WhatsAppSpec)}}
    return dataclasses.replace(event, whatsapp=dataclasses.replace(spec, **spec_changes),
                               **changes)


class RegistryTests(SimpleTestCase):

    def assertRefused(self, event, fragment):
        with self.assertRaises(ImproperlyConfigured) as caught:
            _build_registry([event])
        self.assertIn(fragment, str(caught.exception))

    def test_a_command_only_image_event_is_valid(self):
        self.assertIn('snap2_test', _build_registry([_image_event()]))

    def test_a_non_deferred_event_without_the_allowance_is_still_refused(self):
        # The view-fired case: every event but a command_only one must be deferred.
        self.assertRefused(_image_event(command_only=False),
                           'declares WhatsApp but is not deferred')

    def test_a_command_only_event_may_not_be_deferred(self):
        self.assertRefused(_image_event(deferred=True), 'may not be deferred')

    def test_an_image_header_has_no_text(self):
        self.assertRefused(_image_event(header_text='Snapshot {{1}}'),
                           'an image header has no text')
        self.assertRefused(_image_event(header_fixed_chars=3), 'an image header has no text')

    def test_an_image_builder_returns_the_body_only(self):
        self.assertRefused(_image_event(params=lambda v: ['url', v['taken_at']]),
                           'returns 2 values, param_count is 2 (image header')

    def test_an_unknown_header_media_is_refused(self):
        self.assertRefused(_image_event(header_media='video'), 'header_media must be one of')

    def test_the_snapshot_event_declares_the_approved_template(self):
        event = get_event(EVENT)
        spec = event.whatsapp
        self.assertEqual((spec.template, spec.language, spec.header_media, spec.header_text,
                          spec.param_count, spec.body_fixed_chars),
                         ('daily_snapshot_pm', 'en', 'image', '', 2, 64))
        self.assertEqual(SNAPSHOT_WA_BODY,
                         'Daily site status as of {{1}}. Figures are from Horizon PMS.\n'
                         '- Thanks')
        self.assertEqual(spec.params({'taken_at': TAKEN_AT}), [TAKEN_AT])
        self.assertTrue(event.command_only)
        self.assertFalse(event.deferred)
        self.assertEqual(event.default_state_for('whatsapp'), 'paused')   # W5

    def test_only_the_snapshot_event_is_command_only(self):
        self.assertEqual([code for code, e in NOTIFICATION_EVENTS.items() if e.command_only],
                         [EVENT])


class CommandOnlyGuardTests(TestCase):

    def test_notify_refuses_a_command_only_event(self):
        person = _profile('snap2_guard', 'Admin')
        with self.assertRaisesMessage(ValueError, 'command-only'):
            N.notify(EVENT, recipients=[person], values={})

    def test_the_event_code_is_named_only_by_the_registry_commands_and_tests(self):
        root = Path(__file__).resolve().parent
        codes = [code for code, e in NOTIFICATION_EVENTS.items() if e.command_only]
        found = set()
        for path in list(root.rglob('*.py')) + list(root.rglob('*.html')):
            rel = path.relative_to(root).as_posix()
            if path.name.startswith('tests') or 'migrations' in path.parts:
                continue
            text = path.read_text(encoding='utf-8')
            if any(code in text for code in codes):
                found.add(rel)
        allowed = {rel for rel in found if rel == 'notify_registry.py'
                   or rel.startswith('management/commands/')}
        self.assertEqual(found, allowed)
        self.assertIn('notify_registry.py', found)


# ---------------------------------------------------------------------------
# 3. Existing payloads byte-identical (P1)
# ---------------------------------------------------------------------------

@override_settings(INTERAKT_API_KEY='test-key')
class ExistingPayloadTests(TestCase):

    def setUp(self):
        switches = SystemSettings.get()
        switches.whatsapp_enabled = True
        switches.save()
        self.posts, self.status_at_post = [], []

        def post(url, headers=None, json=None, timeout=None):
            self.status_at_post.append(list(NotificationLog.objects.filter(
                channel='whatsapp').values_list('status', flat=True)))
            self.posts.append(json)
            return _response()

        for patch in (mock.patch('projects.notifications.requests.post', side_effect=post),
                      mock.patch.object(requests.sessions.Session, 'request',
                                        side_effect=AssertionError('HTTP attempted'))):
            patch.start()
            self.addCleanup(patch.stop)

    def test_interakt_payload_shape_is_unchanged(self):
        payload = interakt_payload('9876500001', 'tmpl', ['H', 'b1', 'b2'], 'solarpms-7', 'en')
        self.assertEqual(json.dumps(payload), json.dumps({
            'countryCode': '+91', 'phoneNumber': '9876500001', 'callbackData': 'solarpms-7',
            'type': 'Template',
            'template': {'name': 'tmpl', 'languageCode': 'en', 'headerValues': ['H'],
                         'bodyValues': ['b1', 'b2']}}))

    def test_a_legacy_send_posts_the_same_bytes(self):
        person = _profile('snap2_legacy', 'PM', phone_number='+919876500003')
        send_notification(person, 'Msg', channels=['whatsapp'], template='assign_task',
                          template_params=['Site', 'A', 'Task', 'Site', 'abc.com'])
        self.assertEqual(json.dumps(self.posts[0]), json.dumps({
            'countryCode': '+91', 'phoneNumber': '9876500003',
            'callbackData': f'solarpms-{person.pk}', 'type': 'Template',
            'template': {'name': 'assign_task', 'languageCode': 'en',
                         'headerValues': ['Site'],
                         'bodyValues': ['A', 'Task', 'Site', 'abc.com']}}))

    def test_dc_created_is_still_deferred_and_posts_the_same_bytes(self):
        admin = _profile('snap2_dc_admin', 'Admin')
        alice = _profile('snap2_alice', 'Site Engineer', phone_number='9876500001')
        NotificationEventSetting.objects.create(event_code='dc_created', channel='whatsapp',
                                                is_active=True, changed_by=admin, reason='t')
        values = {k: v for k, v in get_event('dc_created').samples().items()
                  if k not in ('link', 'recipient_name', 'recipient_first_name')}
        with self.captureOnCommitCallbacks(execute=True):
            N.notify('dc_created', recipients=[alice], values=values, link='/challans/7/',
                     actor=admin)
        self.assertEqual(self.status_at_post, [['pending']])   # the deferred path, as before
        self.assertEqual(json.dumps(self.posts[0]), json.dumps({
            'countryCode': '+91', 'phoneNumber': '9876500001',
            'callbackData': f'solarpms-{alice.pk}', 'type': 'Template',
            'template': {'name': 'dc_created', 'languageCode': 'en',
                         'headerValues': ['UKRU001'],
                         'bodyValues': ['Snap2_Alice', '004', 'Rashtrapati Udyan-Dehradun',
                                        '1C x 4 Sqmm DC solar cable 2000 Mtr; MC4 Connector '
                                        '200 Pair', '08 Nov 2026',
                                        N.absolute_url('/challans/7/')]}}))


# ---------------------------------------------------------------------------
# 4. Storage (P4)
# ---------------------------------------------------------------------------

class StorageTests(SimpleTestCase):

    def test_upload_private_bytes_posts_to_the_design_bucket_without_upsert(self):
        client = mock.Mock()
        with mock.patch('projects.design_storage._client', return_value=client):
            result = upload_private_bytes(PATH_1, b'png', 'image/png')
        self.assertEqual(result, (DESIGN_BUCKET, PATH_1))
        client.storage.from_.assert_called_once_with(DESIGN_BUCKET)
        client.storage.from_.return_value.upload.assert_called_once_with(
            path=PATH_1, file=b'png', file_options={'content-type': 'image/png'})

    def test_a_storage_error_is_a_design_storage_error(self):
        client = mock.Mock()
        client.storage.from_.return_value.upload.side_effect = RuntimeError('409 Duplicate')
        with mock.patch('projects.design_storage._client', return_value=client):
            with self.assertRaisesMessage(DesignStorageError,
                                          'Upload to storage failed: RuntimeError'):
                upload_private_bytes(PATH_1, b'png', 'image/png')
        with self.assertRaisesMessage(DesignStorageError, 'Nothing to upload'):
            upload_private_bytes(PATH_1, b'', 'image/png')

    def test_a_new_object_every_run(self):
        self.assertEqual(C.snapshot_object_path(NOW, 1), PATH_1)
        later = NOW.replace(minute=5, second=9)
        self.assertEqual(C.snapshot_object_path(later, 1),
                         'snapshots/2026-11-09/site-dashboard-2026-11-09-1-200509.png')

    def test_the_link_is_signed_for_seven_days(self):
        with mock.patch(f'{CMD}.upload_private_bytes',
                        return_value=('Horizon-PMS-Design', PATH_1)), \
                mock.patch(f'{CMD}.get_design_file_url', side_effect=_signed) as sign:
            image = C.upload_snapshot_image(PATH_1, b'png')
        sign.assert_called_once_with('Horizon-PMS-Design', PATH_1, 7 * 24 * 3600)
        self.assertEqual((image['path'], image['error']), (PATH_1, ''))
        self.assertTrue(image['url'].startswith('https://'))


# ---------------------------------------------------------------------------
# 5. The command (T3)
# ---------------------------------------------------------------------------

@override_settings(INTERAKT_API_KEY='test-key')
class SnapshotWhatsAppBase(SnapshotFixture):

    def setUp(self):
        super().setUp()
        self.zuber = _profile('snap2_zuber', 'Admin', email='zuber@example.com',
                              phone_number='9811100001')
        self.sysadmin = _profile('snap2_sys', 'System Admin', email='sys@example.com',
                                 phone_number='9811100002')
        for person in (self.zuber, self.sysadmin):
            add_named_recipient(EVENT, person, self.admin, 'Forwards the evening snapshot')
        SystemSettings.objects.update_or_create(
            pk=1, defaults={'email_enabled': True, 'whatsapp_enabled': True})
        self.posts, self.uploads = [], []
        self.dir = tempfile.mkdtemp()

        def post(url, headers=None, json=None, timeout=None):
            self.posts.append(json)
            return _response()

        def upload(path, content, content_type):
            self.uploads.append((path, content, content_type))
            return 'Horizon-PMS-Design', path

        self.upload_mock = mock.Mock(side_effect=upload)
        for patch in (
                mock.patch('projects.notifications.requests.post', side_effect=post),
                mock.patch('projects.notifications._zeptomail_post', return_value=(True, '')),
                mock.patch(f'{CMD}.upload_private_bytes', self.upload_mock),
                mock.patch(f'{CMD}.get_design_file_url', side_effect=_signed),
                mock.patch.object(requests.sessions.Session, 'request',
                                  side_effect=AssertionError('an HTTP request was attempted'))):
            patch.start()
            self.addCleanup(patch.stop)

    def switch_on(self, *channels):
        for channel in channels:
            NotificationEventSetting.objects.create(
                event_code=EVENT, channel=channel, is_active=True, changed_by=self.admin,
                reason='switched on for the test')

    def run_command(self, *args):
        """(output, CommandError or None): the output is wanted even when it fails."""
        out = io.StringIO()
        error = None
        with mock.patch('django.utils.timezone.now', return_value=NOW):
            try:
                call_command('send_dashboard_snapshot', *args, stdout=out)
            except CommandError as exc:
                error = exc
        return out.getvalue(), error

    def rows(self, channel='whatsapp'):
        return list(NotificationLog.objects.filter(event_code=EVENT, channel=channel)
                    .order_by('pk')
                    .values_list('recipient__user__username', 'status', 'error_detail'))


class CommandSendTests(SnapshotWhatsAppBase):

    def setUp(self):
        super().setUp()
        self.switch_on('email', 'whatsapp')

    def test_image_header_and_body_in_the_right_slots(self):
        out, error = self.run_command()
        self.assertIsNone(error, out)
        self.assertEqual(self.uploads, [(PATH_1, mock.ANY, 'image/png')])
        self.assertTrue(self.uploads[0][1].startswith(PNG_SIGNATURE))
        self.assertEqual(len(self.posts), 2)          # one image, two recipients
        expected_url = _signed('Horizon-PMS-Design', PATH_1, 604800)
        for payload, phone in zip(self.posts, ('9811100001', '9811100002')):
            self.assertEqual(payload['phoneNumber'], phone)
            self.assertEqual(payload['template'], {
                'name': 'daily_snapshot_pm', 'languageCode': 'en',
                'headerValues': [expected_url],            # whole: not cut, not cleaned
                'bodyValues': [TAKEN_AT]})
        self.assertEqual(self.rows(), [('snap2_zuber', 'sent', ''), ('snap2_sys', 'sent', '')])
        self.assertIn('Sent 2 email(s).', out)
        self.assertIn('Sent 2 WhatsApp message(s).', out)

    def test_rows_name_the_path_never_the_link(self):
        self.run_command()
        messages = list(NotificationLog.objects.filter(event_code=EVENT, channel='whatsapp')
                        .values_list('message', flat=True))
        self.assertEqual(messages, [f'header: image {PATH_1} | body: {TAKEN_AT}'] * 2)
        self.assertFalse(NotificationLog.objects.filter(message__contains='token=').exists())
        self.assertFalse(NotificationLog.objects.filter(
            error_detail__contains='token=').exists())

    def test_the_body_value_is_sanitised(self):
        real = C.collect_snapshot

        def messy(now):
            payload = real(now)
            payload['taken_at_label'] = ' 9 Nov 2026,\n8:00 PM IST '
            return payload

        with mock.patch(f'{CMD}.collect_snapshot', side_effect=messy):
            out, error = self.run_command()
        self.assertIsNone(error, out)
        self.assertEqual(self.posts[0]['template']['bodyValues'], ['9 Nov 2026, 8:00 PM IST'])

    def test_two_images_two_messages_per_recipient_in_order(self):
        two = [PNG_SIGNATURE + b'one', PNG_SIGNATURE + b'two']
        with mock.patch(f'{CMD}.render_snapshot_images', return_value=two):
            out, error = self.run_command()
        self.assertIsNone(error, out)
        self.assertEqual([path for path, _, _ in self.uploads], [PATH_1, PATH_2])
        self.assertEqual([content for _, content, _ in self.uploads], two)
        headers = [(p['phoneNumber'], p['template']['headerValues'][0]) for p in self.posts]
        url_1 = _signed('Horizon-PMS-Design', PATH_1, 604800)
        url_2 = _signed('Horizon-PMS-Design', PATH_2, 604800)
        self.assertEqual(headers, [('9811100001', url_1), ('9811100001', url_2),
                                   ('9811100002', url_1), ('9811100002', url_2)])
        self.assertTrue(all(p['template']['bodyValues'] == [TAKEN_AT] for p in self.posts))
        self.assertEqual(len(self.uploads), 2)        # uploaded once, not per recipient
        self.assertIn('Sent 4 WhatsApp message(s).', out)

    def test_no_valid_number_fails_that_row_others_sent_exit_non_zero(self):
        nophone = _profile('snap2_nophone', 'Admin', email='np@example.com')
        add_named_recipient(EVENT, nophone, self.admin, 'Forwards the evening snapshot')
        out, error = self.run_command()
        self.assertIsNotNone(error)
        self.assertIn(f'whatsapp to snap2_nophone failed: {NO_VALID_WHATSAPP_NUMBER}',
                      str(error))
        self.assertEqual(self.rows(), [('snap2_zuber', 'sent', ''), ('snap2_sys', 'sent', ''),
                                       ('snap2_nophone', 'failed', NO_VALID_WHATSAPP_NUMBER)])
        self.assertEqual([r[1] for r in self.rows('email')], ['sent'] * 3)
        self.assertEqual(len(self.posts), 2)

    def test_a_failed_upload_fails_the_whatsapp_rows_email_still_sent(self):
        self.upload_mock.side_effect = DesignStorageError('Upload to storage failed: HTTPError')
        out, error = self.run_command()
        self.assertIsNotNone(error)
        self.assertIn(f'Upload failed for {PATH_1}', out)
        reason = 'image not uploaded: Upload to storage failed: HTTPError'
        self.assertEqual(self.rows(), [('snap2_zuber', 'failed', reason),
                                       ('snap2_sys', 'failed', reason)])
        self.assertEqual([r[1] for r in self.rows('email')], ['sent', 'sent'])
        self.assertEqual(self.posts, [])
        self.assertEqual(str(error).count('failed: image not uploaded'), 2)

    def test_to_sends_to_one_person_on_both_channels(self):
        out, error = self.run_command('--to', 'snap2_sys')
        self.assertIsNone(error, out)
        self.assertEqual(self.rows(), [('snap2_sys', 'sent', '')])
        self.assertEqual(self.rows('email'), [('snap2_sys', 'sent', '')])
        self.assertEqual([p['phoneNumber'] for p in self.posts], ['9811100002'])

    def test_an_interakt_error_exits_non_zero(self):
        failing = mock.Mock(status_code=400, text='{"message": "bad media"}')
        with mock.patch('projects.notifications.requests.post', return_value=failing):
            out, error = self.run_command()
        self.assertIsNotNone(error)
        self.assertRegex(str(error), r'whatsapp to snap2_zuber failed: HTTP 400')
        self.assertEqual([r[1] for r in self.rows('email')], ['sent', 'sent'])


class CommandSkipTests(SnapshotWhatsAppBase):

    def test_paused_whatsapp_is_skipped_nothing_uploaded_email_sent_exit_zero(self):
        self.switch_on('email')
        out, error = self.run_command()
        self.assertIsNone(error, out)
        self.assertEqual(self.uploads, [])
        self.assertEqual(self.posts, [])
        self.assertEqual(self.rows(), [('snap2_zuber', 'skipped', 'event paused'),
                                       ('snap2_sys', 'skipped', 'event paused')])
        self.assertEqual([r[1] for r in self.rows('email')], ['sent', 'sent'])
        self.assertIn('Sent 2 email(s).', out)
        self.assertIn('WhatsApp skipped: event paused.', out)

    def test_master_switch_off_is_skipped_nothing_uploaded_exit_zero(self):
        self.switch_on('email', 'whatsapp')
        SystemSettings.objects.filter(pk=1).update(whatsapp_enabled=False)
        out, error = self.run_command()
        self.assertIsNone(error, out)
        self.assertEqual((self.uploads, self.posts), ([], []))
        self.assertEqual([r[1:] for r in self.rows()],
                         [('skipped', 'Master switch off')] * 2)
        self.assertIn('WhatsApp skipped: Master switch off.', out)

    def test_dry_run_prints_each_message_and_uploads_nothing(self):
        nophone = _profile('snap2_nophone2', 'Admin')
        add_named_recipient(EVENT, nophone, self.admin, 'Forwards the evening snapshot')
        out, error = self.run_command('--dry-run', '--out-dir', self.dir)
        self.assertIsNone(error, out)
        self.assertIn('WhatsApp: channel paused, master switch ON; template '
                      'daily_snapshot_pm (en), 1 message(s) per recipient', out)
        self.assertIn(f"snap2_zuber #1: header: signed URL for {PATH_1} | body {{{{1}}}}: "
                      f"'{TAKEN_AT}'", out)
        self.assertIn(f'snap2_nophone2: {NO_VALID_WHATSAPP_NUMBER} (would fail)', out)
        self.assertIn('Dry run: nothing uploaded.', out)
        self.upload_mock.assert_not_called()
        self.assertEqual(self.posts, [])
        self.assertFalse(NotificationLog.objects.exists())


# ---------------------------------------------------------------------------
# 6. The phone test (P6)
# ---------------------------------------------------------------------------

class PhoneTestCommandTests(SnapshotWhatsAppBase):

    TEST_PATH = 'snapshots/2026-11-09/whatsapp-test-2026-11-09-1-200000.png'

    def setUp(self):
        super().setUp()
        self.png = Path(self.dir) / 'snap.png'
        self.png.write_bytes(PNG_SIGNATURE + b'body')

    def phone_test(self, *args):
        out = io.StringIO()
        with mock.patch('django.utils.timezone.now', return_value=NOW):
            call_command('test_whatsapp', '--event', EVENT, '--user', 'snap2_zuber', *args,
                         stdout=out)
        return out.getvalue()

    def test_the_image_is_required_and_only_for_an_image_event(self):
        with self.assertRaisesMessage(CommandError, 'give the PNG with --image'):
            self.phone_test()
        with self.assertRaisesMessage(CommandError, 'only for an event whose header is an image'):
            call_command('test_whatsapp', '--event', 'dc_created', '--user', 'snap2_zuber',
                         '--image', str(self.png), stdout=io.StringIO())
        text = Path(self.dir) / 'not.png'
        text.write_bytes(b'hello')
        with self.assertRaisesMessage(CommandError, 'is not a PNG'):
            self.phone_test('--image', str(text))

    def test_dry_run_uploads_and_writes_nothing(self):
        out = self.phone_test('--image', str(self.png), '--dry-run')
        self.assertIn(f"Header value      : 'signed URL for {self.TEST_PATH}'", out)
        self.assertIn("Body value {{1}}    : '14 Nov 2026, 8:00 PM IST'", out)
        self.upload_mock.assert_not_called()
        self.assertEqual((self.posts, list(NotificationLog.objects.all())), ([], []))

    def test_master_switch_off_refuses_before_any_upload(self):
        SystemSettings.objects.filter(pk=1).update(whatsapp_enabled=False)
        with self.assertRaisesMessage(CommandError, 'master switch is OFF'):
            self.phone_test('--image', str(self.png))
        self.upload_mock.assert_not_called()

    def test_a_failed_upload_sends_and_logs_nothing(self):
        self.upload_mock.side_effect = DesignStorageError('Upload to storage failed: X')
        with self.assertRaisesMessage(CommandError, 'nothing sent'):
            self.phone_test('--image', str(self.png))
        self.assertEqual((self.posts, list(NotificationLog.objects.all())), ([], []))

    def test_the_test_sends_the_image_and_opens_the_gate(self):
        event = get_event(EVENT)
        self.assertFalse(A.whatsapp_tested(event))
        out = self.phone_test('--image', str(self.png))
        self.assertEqual(self.uploads, [(self.TEST_PATH, self.png.read_bytes(), 'image/png')])
        [payload] = self.posts
        self.assertEqual(payload['template']['headerValues'],
                         [_signed('Horizon-PMS-Design', self.TEST_PATH, 604800)])
        self.assertEqual(payload['template']['bodyValues'], ['14 Nov 2026, 8:00 PM IST'])
        self.assertNotIn('token=', out)                 # the console shows the path only
        row = NotificationLog.objects.get(event_code=EVENT, channel='whatsapp')
        self.assertEqual((row.status, row.template_name, row.message),
                         ('sent', 'daily_snapshot_pm',
                          f'[TEST] header: image {self.TEST_PATH} | body: '
                          '14 Nov 2026, 8:00 PM IST'))
        # The phone test above wrote its row at NOW (9 Nov 2026); the 14-day gate in
        # whatsapp_tested() and set_state() must be read at that same moment, not the
        # real clock, or this test fails from about 23 Nov 2026.
        with mock.patch('django.utils.timezone.now', return_value=NOW):
            self.assertTrue(A.whatsapp_tested(event))
            A.set_state(EVENT, 'whatsapp', True, self.admin, 'phone test passed on Zuber phone')
            self.assertTrue(N.channel_is_active(event, 'whatsapp'))
        # The same row read 30 days after NOW is outside the window: this test depends on
        # the fixed time above, not on the date it happens to run.
        self.assertFalse(A.whatsapp_tested(event, now=NOW + timedelta(days=30)))
