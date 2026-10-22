"""T2 — Admin screens for notification templates (notify_views) and the service
corrections they needed (notify_admin A1-A7, notify A8).

WHAT IS PINNED
  Access   Admin gets 200 on every screen; System Admin, CEO, PM, SCM get 403 and
           anonymous a login redirect, on every GET and every POST, and a refused POST
           changes nothing.
  Flow     create draft -> invalid save shows every error and saves nothing -> save ->
           test send -> activate. Activation refused without a test, and when the draft
           was saved after its test. One open draft. Discard deletes an untested draft
           and keeps a tested one as discarded; an active version cannot be discarded.
           Restore creates a draft equal to the old version.
  Switch   switching on refused without a test of the live content (built-in default:
           matched by text, so a code change stops old tests counting; active version:
           test after activated_at); reason required; history written.
  Tests    send_test logs notify_test with event_code and version, prefixes [TEST],
           ignores the event switch, and the screen names the switch that stopped it.
  Safety   <script> in a body is escaped twice in srcdoc; the iframe is sandboxed with
           nothing allowed; {{ request }} is refused.
  Queries  the list and the event page do not grow with events or log rows.
  Records  send-records gains an Event column and filter; other filters unchanged.

INTERCEPTION, as tests_notify: `projects.notifications._zeptomail_post` is a recorder,
`requests.sessions.Session.request` raises, and the email switch is ON.

Run with:
    python manage.py test projects.tests_notify_views --settings=solarpms.test_settings
"""
import dataclasses
import re
from unittest import mock

import requests
from django.db import connection
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from . import notify_admin as A
from .models import (
    ActivityLog, NotificationEventSetting, NotificationEventSettingChange, NotificationLog,
    NotificationTemplate, SystemSettings, TemplateVersionLocked,
)
from .notify import render_email
from .notify_registry import NOTIFICATION_EVENTS, NotificationEvent, Variable, get_event
from .tests_approvals import _profile

EVENT = 'admin_test_message'
CH = 'email'


class ScreenBase(TestCase):

    def setUp(self):
        self.admin = _profile('nv_admin', 'Admin')
        self.admin.user.email = 'nv_admin@example.com'
        self.admin.user.save(update_fields=['email'])
        switches = SystemSettings.get()
        switches.email_enabled = True
        switches.save()

        self.mail = []

        def record(to_email, to_name, subject, text_body, html_body=None):
            self.mail.append({'to': to_email, 'subject': subject, 'text': text_body,
                              'html': html_body})
            return True, ''

        mail_patch = mock.patch('projects.notifications._zeptomail_post',
                                side_effect=record)
        http_patch = mock.patch.object(
            requests.sessions.Session, 'request',
            side_effect=AssertionError('an HTTP request was attempted'))
        mail_patch.start()
        self.http = http_patch.start()
        self.addCleanup(mail_patch.stop)
        self.addCleanup(http_patch.stop)
        self.addCleanup(lambda: self.http.assert_not_called())

        self.client = Client(SERVER_NAME='localhost')
        self.client.force_login(self.admin.user)

    # ── URLs ────────────────────────────────────────────────────────────────

    @staticmethod
    def url(name, *args):
        return reverse(name, args=args)

    def event_url(self):
        return self.url('admin_notification_event', EVENT)

    def draft_url(self, t, action=''):
        if action:
            return self.url(f'admin_notification_draft_{action}', EVENT, CH, t.pk)
        return self.url('admin_notification_draft', EVENT, CH, t.pk)

    # ── actions through the screens ─────────────────────────────────────────

    def create_draft(self):
        response = self.client.post(self.url('admin_notification_draft_create', EVENT, CH))
        draft = NotificationTemplate.objects.get(event_code=EVENT, status='draft')
        self.assertRedirects(response, self.draft_url(draft), fetch_redirect_response=False)
        return draft

    def save(self, draft, *, subject='Hello {{ recipient_name }}',
             body='From **{{ sender_name }}**', button_label='Open', note='Reworded',
             action='save'):
        return self.client.post(self.draft_url(draft, 'save'), {
            'subject': subject, 'body': body, 'button_label': button_label,
            'change_note': note, 'action': action})

    def send_a_test(self, draft):
        return self.client.post(self.draft_url(draft, 'test'))

    def activate(self, draft):
        return self.client.post(self.draft_url(draft, 'activate'))

    def messages_of(self, response):
        return [str(m) for m in response.wsgi_request._messages]

    def live_version(self):
        draft = self.create_draft()
        self.save(draft)
        self.send_a_test(draft)
        self.activate(draft)
        draft.refresh_from_db()
        self.assertEqual(draft.status, 'active')
        return draft


class AccessTests(ScreenBase):

    def setUp(self):
        super().setUp()
        self.draft = A.create_draft(EVENT, CH, self.admin)
        self.others = [_profile('nv_sys', 'System Admin'), _profile('nv_ceo', 'CEO'),
                       _profile('nv_pm', 'PM'), _profile('nv_scm', 'SCM')]

    def gets(self):
        return [self.url('admin_notification_events'), self.event_url(),
                self.draft_url(self.draft),
                self.url('admin_notification_version', EVENT, CH, self.draft.pk)]

    def posts(self):
        return [self.url('admin_notification_set_state', EVENT, CH),
                self.url('admin_notification_test_live', EVENT, CH),
                self.url('admin_notification_draft_create', EVENT, CH),
                self.draft_url(self.draft, 'save'), self.draft_url(self.draft, 'test'),
                self.draft_url(self.draft, 'activate'), self.draft_url(self.draft, 'discard'),
                self.url('admin_notification_version_restore', EVENT, CH, self.draft.pk)]

    def snapshot(self):
        return (list(NotificationTemplate.objects.values_list('pk', 'status', 'body')),
                NotificationEventSetting.objects.count(), NotificationLog.objects.count())

    def test_admin_gets_every_screen(self):
        for url in self.gets():
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_every_other_role_is_refused_everywhere(self):
        before = self.snapshot()
        for profile in self.others:
            client = Client(SERVER_NAME='localhost')
            client.force_login(profile.user)
            for url in self.gets():
                with self.subTest(role=profile.role, url=url):
                    self.assertEqual(client.get(url).status_code, 403)
            for url in self.posts():
                with self.subTest(role=profile.role, url=url, method='POST'):
                    response = client.post(url, {'state': 'paused', 'reason': 'x',
                                                 'subject': 's', 'body': 'b',
                                                 'change_note': 'n'})
                    self.assertEqual(response.status_code, 403)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.mail, [])

    def test_anonymous_is_sent_to_login(self):
        client = Client(SERVER_NAME='localhost')
        for url in self.gets() + self.posts():
            with self.subTest(url=url):
                response = client.post(url) if url in self.posts() else client.get(url)
                self.assertEqual(response.status_code, 302)
                self.assertIn('/login/', response['Location'])

    def test_unknown_event_or_channel_is_404(self):
        self.assertEqual(self.client.get(self.url('admin_notification_event', 'nope')).status_code, 404)
        self.assertEqual(self.client.post(
            self.url('admin_notification_draft_create', EVENT, 'whatsapp')).status_code, 404)


class DraftFlowTests(ScreenBase):

    def test_create_edit_invalid_save_test_activate(self):
        draft = self.create_draft()
        original_body = draft.body

        response = self.save(draft, body='Hi {{ request }} and {{ thing.attr }}', note='')
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn('Nothing was saved', content)
        self.assertIn('{{ request }} is not a variable', content)
        self.assertIn('unknown placeholder {{ thing.attr }}', content)
        self.assertIn('Change note: say what changed and why.', content)
        self.assertIn('Hi {{ request }} and {{ thing.attr }}', content)   # what was typed
        draft.refresh_from_db()
        self.assertEqual((draft.body, draft.updated_at), (original_body, None))

        response = self.save(draft, action='preview')
        self.assertRedirects(response, self.draft_url(draft) + '?preview=1',
                             fetch_redirect_response=False)
        draft.refresh_from_db()
        self.assertEqual((draft.subject, draft.updated_by), ('Hello {{ recipient_name }}',
                                                             self.admin))
        self.assertIsNotNone(draft.updated_at)
        preview = self.client.get(self.draft_url(draft) + '?preview=1').content.decode()
        self.assertIn('Hello Priya Sharma', preview)

        response = self.send_a_test(draft)
        self.assertEqual(len(self.mail), 1)
        self.assertEqual(self.mail[0]['subject'], '[TEST] Hello Priya Sharma')
        self.assertIn('Test sent to nv_admin@example.com', self.messages_of(response)[-1])

        response = self.activate(draft)
        self.assertRedirects(response, self.event_url(), fetch_redirect_response=False)
        draft.refresh_from_db()
        self.assertEqual(draft.status, 'active')
        self.assertIsNotNone(draft.activated_at)
        self.assertTrue(ActivityLog.objects.filter(action__contains='version 1 activated').exists())

    def test_the_change_note_starts_empty_and_an_empty_note_saves_nothing(self):
        draft = self.create_draft()
        content = self.client.get(self.draft_url(draft)).content.decode()
        self.assertRegex(content, r'name="change_note" value=""')
        self.assertIn('Current note: Draft from the code default', content)

        response = self.save(draft, subject='Valid {{ sender_name }}', note='   ')
        self.assertEqual(response.status_code, 200)
        self.assertIn('Change note: say what changed and why.', response.content.decode())
        draft.refresh_from_db()
        self.assertEqual((draft.subject, draft.change_note, draft.updated_at),
                         (get_event(EVENT).default_subject, 'Draft from the code default',
                          None))

    def test_activate_refused_without_a_test(self):
        draft = self.create_draft()
        response = self.activate(draft)
        self.assertIn('Send a test of version 1', self.messages_of(response)[-1])
        draft.refresh_from_db()
        self.assertEqual(draft.status, 'draft')

    def test_activate_refused_when_saved_after_the_test(self):
        draft = self.create_draft()
        self.send_a_test(draft)
        self.save(draft, subject='Changed after the test')
        self.activate(draft)
        draft.refresh_from_db()
        self.assertEqual(draft.status, 'draft')

    def test_a_second_draft_is_refused(self):
        first = self.create_draft()
        response = self.client.post(self.url('admin_notification_draft_create', EVENT, CH))
        self.assertIn(f'Version {first.version_no} is already an open draft',
                      self.messages_of(response)[-1])
        self.assertEqual(NotificationTemplate.objects.count(), 1)

    def test_discard_deletes_an_untested_draft(self):
        draft = self.create_draft()
        self.client.post(self.draft_url(draft, 'discard'))
        self.assertFalse(NotificationTemplate.objects.exists())

    def test_discard_keeps_a_tested_draft_as_discarded_and_frees_the_slot(self):
        draft = self.create_draft()
        self.send_a_test(draft)
        response = self.client.post(self.draft_url(draft, 'discard'))
        self.assertIn('kept in the history as discarded', self.messages_of(response)[-1])
        draft.refresh_from_db()
        self.assertEqual(draft.status, 'discarded')
        self.assertEqual(self.create_draft().version_no, 2)

    def test_discard_of_an_active_version_is_refused(self):
        live = self.live_version()
        response = self.client.post(self.draft_url(live, 'discard'))
        self.assertIn('only a draft can be discarded', self.messages_of(response)[-1])
        live.refresh_from_db()
        self.assertEqual(live.status, 'active')
        with self.assertRaises(TemplateVersionLocked):
            live.delete()

    def test_restore_creates_a_draft_equal_to_the_old_version(self):
        v1 = self.live_version()
        v2 = self.create_draft()
        self.save(v2, subject='Second wording')
        self.send_a_test(v2)
        self.activate(v2)
        response = self.client.post(self.url('admin_notification_version_restore', EVENT, CH, v1.pk))
        v3 = NotificationTemplate.objects.get(status='draft')
        self.assertRedirects(response, self.draft_url(v3), fetch_redirect_response=False)
        self.assertEqual((v3.version_no, v3.subject, v3.body, v3.button_label),
                         (3, v1.subject, v1.body, v1.button_label))
        self.assertTrue(ActivityLog.objects.filter(action__contains='restored as draft').exists())

    def test_a_non_draft_opens_read_only(self):
        live = self.live_version()
        response = self.client.get(self.draft_url(live))
        self.assertRedirects(response, self.url('admin_notification_version', EVENT, CH, live.pk),
                             fetch_redirect_response=False)


class SwitchTests(ScreenBase):

    def switch(self, state, reason='Because'):
        return self.client.post(self.url('admin_notification_set_state', EVENT, CH),
                                {'state': state, 'reason': reason})

    def test_reason_required(self):
        response = self.switch('paused', reason='  ')
        self.assertIn('Give a reason', self.messages_of(response)[-1])
        self.assertFalse(NotificationEventSettingChange.objects.exists())

    def test_on_needs_a_test_of_the_built_in_default(self):
        self.switch('paused', 'Quiet week')
        response = self.switch('active', 'Back')
        self.assertIn('Send a test of the live content', self.messages_of(response)[-1])
        self.client.post(self.url('admin_notification_test_live', EVENT, CH))
        self.switch('active', 'Back')
        self.assertEqual(list(NotificationEventSettingChange.objects.order_by('pk')
                              .values_list('from_state', 'to_state', 'reason')),
                         [('active', 'paused', 'Quiet week'), ('paused', 'active', 'Back')])
        self.assertEqual(ActivityLog.objects.filter(action__contains='switched').count(), 2)

    def test_a_changed_code_default_makes_old_tests_stop_counting(self):
        A.send_test(EVENT, CH, self.admin)
        self.assertTrue(A.live_content_tested(EVENT, CH))
        reworded = dataclasses.replace(get_event(EVENT), default_body='New default wording.')
        with mock.patch.dict(NOTIFICATION_EVENTS, {EVENT: reworded}):
            self.assertFalse(A.live_content_tested(EVENT, CH))

    def test_an_active_version_needs_a_test_after_activation(self):
        self.switch('paused', 'Quiet week')
        self.live_version()           # its own test was sent before activation
        response = self.switch('active', 'Back')
        self.assertIn('Send a test of the live content', self.messages_of(response)[-1])
        self.client.post(self.url('admin_notification_test_live', EVENT, CH))
        self.switch('active', 'Back')
        self.assertTrue(NotificationEventSetting.objects.get().is_active)


class TestSendTests(ScreenBase):

    def test_the_log_row_names_the_test_and_the_version(self):
        draft = A.create_draft(EVENT, CH, self.admin)
        row = A.send_test(EVENT, CH, self.admin, template=draft)
        self.assertEqual((row.status, row.template_name, row.event_code, row.template_version),
                         ('sent', 'notify_test', EVENT, draft))
        self.assertIsNone(row.actor)
        default_row = A.send_test(EVENT, CH, self.admin)
        self.assertIsNone(default_row.template_version)

    def test_archived_and_discarded_versions_cannot_be_test_sent(self):
        v1 = self.live_version()
        v2 = self.create_draft()
        self.save(v2, subject='Second')
        self.send_a_test(v2)
        self.activate(v2)
        v1.refresh_from_db()
        v3 = self.create_draft()
        self.send_a_test(v3)
        self.client.post(self.draft_url(v3, 'discard'))
        v3.refresh_from_db()
        self.assertEqual((v1.status, v3.status), ('archived', 'discarded'))
        sent_before = len(self.mail)
        for version in (v1, v3):
            with self.subTest(status=version.status):
                with self.assertRaisesRegex(A.NotificationAdminRefused,
                                            'Restore it as a new draft'):
                    A.send_test(EVENT, CH, self.admin, template=version)
                response = self.client.post(self.draft_url(version, 'test'))
                self.assertIn('cannot be test-sent', self.messages_of(response)[-1])
        self.assertEqual(len(self.mail), sent_before)
        version_page = self.client.get(
            self.url('admin_notification_version', EVENT, CH, v1.pk)).content.decode()
        self.assertNotIn('Send test', version_page)

    def test_a_paused_event_can_still_be_tested(self):
        A.set_state(EVENT, CH, False, self.admin, 'Paused')
        self.assertEqual(A.send_test(EVENT, CH, self.admin).status, 'sent')

    def test_whatsapp_and_inactive_admin_are_refused(self):
        with self.assertRaises(A.NotificationAdminRefused):
            A.send_test(EVENT, 'whatsapp', self.admin)
        self.admin.user.is_active = False
        self.admin.user.save()
        with self.assertRaises(A.NotificationAdminRefused):
            A.send_test(EVENT, CH, self.admin)

    def test_the_screen_names_the_switch_that_stopped_the_test(self):
        switches = SystemSettings.get()
        switches.email_enabled = False
        switches.save()
        response = self.client.post(self.url('admin_notification_test_live', EVENT, CH))
        self.assertIn('Settings → Master switches', self.messages_of(response)[-1])

        switches.email_enabled = True
        switches.save()
        self.admin.email_notifications = False
        self.admin.save()
        response = self.client.post(self.url('admin_notification_test_live', EVENT, CH))
        self.assertIn('Settings → Notification prefs', self.messages_of(response)[-1])
        self.assertEqual(self.mail, [])
        self.assertFalse(A.live_content_tested(EVENT, CH))


class SafetyTests(ScreenBase):

    def test_script_in_a_body_is_escaped_inside_a_sandboxed_iframe(self):
        draft = self.create_draft()
        self.save(draft, body='<script>alert(1)</script> **bold**', action='preview')
        content = self.client.get(self.draft_url(draft) + '?preview=1').content.decode()
        self.assertNotIn('<script>alert(1)', content)
        # Escaped once by the renderer, then again into the srcdoc attribute.
        self.assertIn('&amp;lt;script&amp;gt;alert(1)', content)
        iframes = re.findall(r'<iframe[^>]*>', content)
        self.assertEqual(len(iframes), 1)
        self.assertIn('sandbox=""', iframes[0])
        self.assertNotIn('allow-', content.split('<iframe', 1)[1].split('>', 1)[0])

    def test_every_preview_iframe_is_sandboxed(self):
        live = self.live_version()
        for url in (self.event_url(), self.url('admin_notification_version', EVENT, CH, live.pk)):
            content = self.client.get(url).content.decode()
            iframes = re.findall(r'<iframe[^>]*>', content)
            self.assertTrue(iframes)
            for tag in iframes:
                self.assertIn('sandbox=""', tag)
                self.assertNotIn('allow-', tag)

    def test_template_syntax_is_refused_or_literal(self):
        draft = self.create_draft()
        response = self.save(draft, body='{{ request }}')
        self.assertEqual(response.status_code, 200)
        draft.refresh_from_db()
        self.assertNotIn('request', draft.body)
        self.save(draft, body='{% load static %} {% now "Y" %}', action='preview')
        _, text, _ = A.render_preview(NotificationTemplate.objects.get(pk=draft.pk))
        self.assertIn('{% load static %} {% now "Y" %}', text)


class RenderingTests(ScreenBase):

    def test_a_single_line_break_is_br_in_html_and_kept_in_text(self):
        template = NotificationTemplate(event_code=EVENT, channel=CH, subject='S',
                                        body='Line one\nLine two\n\nNext', button_label='Go')
        _, text, html = render_email(get_event(EVENT), template,
                                     {'sender_name': 'A', 'recipient_name': 'B'}, '')
        self.assertIn('>Line one<br>\nLine two</p>', html)
        self.assertIn('>Next</p>', html)
        self.assertIn('Line one\nLine two\n\nNext', text)

    def test_a_newline_in_a_value_is_not_a_br(self):
        template = NotificationTemplate(event_code=EVENT, channel=CH, subject='S',
                                        body='{{ sender_name }}', button_label='Go')
        _, _, html = render_email(get_event(EVENT), template,
                                  {'sender_name': 'A\nB', 'recipient_name': 'B'}, '')
        self.assertNotIn('A<br>', html)


def _events(n):
    """n synthetic email events across two modules, for the query-count tests."""
    return {f'qc_event_{i}': NotificationEvent(
        code=f'qc_event_{i}', label=f'Event {i}', module=f'Module {i % 2}', tier='KNOW',
        channels=('email',), variables=(Variable('thing', 'A thing', 'Widget'),),
        in_app_text='{{ thing }}', default_subject='About {{ thing }}',
        default_body='Hi {{ recipient_name }}, {{ thing }}.', button_label='Open',
        recipients_rule='Nobody.', default_state='active') for i in range(n)}


class QueryCountTests(ScreenBase):

    def count(self, url):
        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(self.client.get(url).status_code, 200)
        return len(queries)

    def test_the_list_does_not_grow_with_events(self):
        counts = []
        for n in (1, 20):
            events = _events(n)
            for code in events:
                NotificationLog.objects.create(recipient=self.admin, channel='email',
                                               status='sent', message='m', event_code=code)
            with mock.patch.dict(NOTIFICATION_EVENTS, events, clear=True):
                counts.append(self.count(self.url('admin_notification_events')))
        self.assertEqual(counts[0], counts[1])

    def test_the_event_page_does_not_grow_with_events_or_log_rows(self):
        counts = []
        for n_events, n_rows in ((1, 1), (20, 80)):
            events = _events(n_events)
            for _ in range(n_rows):
                NotificationLog.objects.create(recipient=self.admin, channel='email',
                                               status='sent', message='m', event_code='qc_event_0')
            with mock.patch.dict(NOTIFICATION_EVENTS, events, clear=True):
                counts.append(self.count(self.url('admin_notification_event', 'qc_event_0')))
        self.assertEqual(counts[0], counts[1])


class SendRecordsTests(ScreenBase):

    def test_event_column_and_filter_beside_the_old_filters(self):
        NotificationLog.objects.create(recipient=self.admin, channel='email', status='sent',
                                       message='m', event_code=EVENT)
        NotificationLog.objects.create(recipient=self.admin, channel='whatsapp',
                                       status='failed', message='m', template_name='old_send')
        url = self.url('admin_send_records')
        content = self.client.get(url).content.decode()
        self.assertIn('>Event</th>', content)
        self.assertIn(f'<option value="{EVENT}"', content)
        self.assertEqual(len(self.client.get(url).context['page_obj']), 2)
        self.assertEqual([r.event_code for r in
                          self.client.get(url, {'event': EVENT}).context['page_obj']], [EVENT])
        self.assertEqual([r.template_name for r in
                          self.client.get(url, {'channel': 'whatsapp'}).context['page_obj']],
                         ['old_send'])
        self.assertEqual([r.status for r in
                          self.client.get(url, {'status': 'failed'}).context['page_obj']],
                         ['failed'])
