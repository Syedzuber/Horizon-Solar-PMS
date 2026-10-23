"""T1 — notification template foundation (notify_registry, notify, notify_admin).

WHAT IS PINNED
  Registry   duplicate codes and undeclared placeholders are refused at build time;
             reserved names may not be declared.
  Versions   one ACTIVE per event and channel; activate() archives the previous one;
             a non-draft's content cannot be saved; a status-only save passes; a
             rollback is a NEW draft equal to the old version.
  Validation unknown and undeclared placeholders, empty subject, body length; activate
             is refused while there are errors.
  Rendering  values escaped in HTML; paragraphs and bold; `{% load %}` printed
             literally, `{{ request }}` refused — stored text never reaches the
             template engine.
  notify()   actor and inactive people dropped; duplicates sent once; a paused event
             logs 'skipped' and sends no email; code default used when no template is
             active; event_code and template_version recorded; a render failure logs
             'failed', does not raise, and does not stop the other channel; a
             rolled-back transaction sends nothing.
  Unchanged  send_notification() without the new arguments creates its log rows with
             exactly the keyword arguments it always did.
  Switches   set_state needs a reason, writes one history row; history is append-only.

INTERCEPTION, as tests_approval_notices: `projects.notifications._zeptomail_post` is a
recorder, `requests.sessions.Session.request` raises, and the email switch is ON in the
throwaway database. notify() sends after commit, so sends run inside
captureOnCommitCallbacks(execute=True).

Run with:
    python manage.py test projects.tests_notify --settings=solarpms.test_settings
"""
import dataclasses
from unittest import mock

import requests
from django.core.exceptions import ImproperlyConfigured
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings

from . import notify as N
from . import notify_admin as A
from . import permissions
from .models import (
    AppendOnlyViolation, Notification, NotificationEventSetting,
    NotificationEventSettingChange, NotificationLog, NotificationTemplate, SystemSettings,
    TemplateVersionLocked,
)
from .notifications import send_notification
from .notify_registry import (
    NOTIFICATION_EVENTS, NotificationEvent, Variable, _build_registry, get_event,
)
from .tests_approvals import _profile

EVENT = 'admin_test_message'


def _event(**overrides):
    """A valid event to build registries from, with `overrides` applied."""
    fields = dict(
        code='sample_event', label='Sample', module='Tests', tier='KNOW',
        channels=('email',), variables=(Variable('thing', 'A thing', 'Widget'),),
        in_app_text='{{ thing }} happened.', default_subject='About {{ thing }}',
        default_body='Hi {{ recipient_name }}, {{ thing }}. {{ link }}',
        button_label='Open', recipients_rule='Nobody.', default_state='active',
    )
    fields.update(overrides)
    return NotificationEvent(**fields)


class RegistryTests(TestCase):

    def test_a_valid_registry_builds(self):
        self.assertEqual(list(_build_registry([_event()])), ['sample_event'])

    def test_duplicate_codes_are_refused(self):
        with self.assertRaisesRegex(ImproperlyConfigured, 'duplicate code'):
            _build_registry([_event(), _event(label='Again')])

    def test_undeclared_placeholders_are_refused_in_each_text(self):
        for field in ('in_app_text', 'default_subject', 'default_body'):
            with self.subTest(field=field):
                with self.assertRaisesRegex(ImproperlyConfigured, 'undeclared placeholder'):
                    _build_registry([_event(**{field: 'x {{ undeclared }} y'})])

    def test_reserved_names_may_not_be_declared(self):
        with self.assertRaisesRegex(ImproperlyConfigured, 'reserved'):
            _build_registry([_event(variables=(Variable('link', 'm', 's'),))])

    def test_the_shipped_registry_holds_the_admin_test_message(self):
        # T3a added the first moved sends after it; the admin test message stays first.
        self.assertEqual(list(NOTIFICATION_EVENTS), [
            EVENT, 'issue_raised', 'issue_resolved', 'project_pm_assigned',
            'project_unassigned_alert', 'boq_acknowledged', 'payment_milestone_reached'])
        event = get_event(EVENT)
        self.assertEqual((event.tier, event.default_state, event.channels),
                         ('KNOW', 'active', ('email',)))

    def test_an_unknown_code_is_a_key_error(self):
        with self.assertRaises(KeyError):
            get_event('no_such_event')


class Base(TestCase):

    def setUp(self):
        self.admin = _profile('nt_admin', 'Admin')
        self.alice = _profile('nt_alice', 'SCM')
        self.bob = _profile('nt_bob', 'Finance')
        for profile in (self.admin, self.alice, self.bob):
            profile.user.email = f'{profile.user.username}@example.com'
            profile.user.save(update_fields=['email'])
        switches = SystemSettings.get()
        switches.email_enabled = True
        switches.save()

        self.mail = []
        self.log_mark = 0

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

    def draft(self, **content):
        template = A.create_draft(EVENT, 'email', self.admin)
        if content:
            A.update_draft(template, self.admin,
                           content.get('subject', template.subject),
                           content.get('body', template.body),
                           content.get('button_label', template.button_label),
                           content.get('note', 'test change'))
        return template

    def send(self, recipients, values=None, **kwargs):
        """notify() as production runs it: the send happens after commit."""
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            N.notify(EVENT, recipients=recipients,
                     values={'sender_name': 'Asha'} if values is None else values,
                     **kwargs)
        return callbacks

    def rows(self, **filters):
        return list(NotificationLog.objects.filter(pk__gt=self.log_mark, **filters)
                    .order_by('pk'))

    def go_live(self, template):
        """Test-send, then activate: T2 (A6) refuses activation without a test. The
        test's email and log row are then set aside, so a test reads only what follows."""
        A.send_test(EVENT, 'email', self.admin, template=template)
        A.activate(template, self.admin)
        self.mail.clear()
        self.log_mark = (NotificationLog.objects.order_by('-pk')
                         .values_list('pk', flat=True).first() or 0)


class VersioningTests(Base):

    def test_activate_archives_the_previous_active_version(self):
        v1 = self.draft()
        self.go_live(v1)
        v2 = self.draft(subject='Second')
        self.go_live(v2)
        v1.refresh_from_db()
        self.assertEqual((v1.status, v2.status), ('archived', 'active'))
        self.assertEqual(NotificationTemplate.objects.filter(
            event_code=EVENT, channel='email', status='active').count(), 1)

    def test_the_database_allows_one_active_version_per_event_and_channel(self):
        self.go_live(self.draft())
        with self.assertRaises(IntegrityError), transaction.atomic():
            NotificationTemplate.objects.create(
                event_code=EVENT, channel='email', version_no=9, status='active',
                subject='s', body='b', created_by=self.admin, change_note='n')

    def test_a_non_draft_cannot_be_edited(self):
        template = self.draft()
        self.go_live(template)
        template.subject = 'Changed live'
        with self.assertRaises(TemplateVersionLocked):
            template.save()
        template.refresh_from_db()
        with self.assertRaises(A.NotificationAdminRefused):
            A.update_draft(template, self.admin, 'x', 'y', 'z', 'note')

    def test_setting_status_back_to_draft_does_not_unlock_content(self):
        template = self.draft()
        self.go_live(template)
        template.status = 'draft'
        template.body = 'Rewritten'
        with self.assertRaises(TemplateVersionLocked):
            template.save()

    def test_a_status_only_save_passes(self):
        template = self.draft()
        self.go_live(template)
        template.status = 'archived'
        template.save()
        template.refresh_from_db()
        self.assertEqual(template.status, 'archived')

    def test_rollback_is_a_new_draft_equal_to_the_old_version(self):
        v1 = self.draft(subject='First {{ sender_name }}', body='Old body')
        self.go_live(v1)
        self.go_live(self.draft(subject='Second', body='New body'))
        v1.refresh_from_db()
        v3 = A.create_draft(EVENT, 'email', self.admin, from_version=v1)
        self.assertEqual((v3.version_no, v3.status), (3, 'draft'))
        self.assertEqual((v3.subject, v3.body, v3.button_label),
                         (v1.subject, v1.body, v1.button_label))
        self.assertIn('version 1', v3.change_note)

    def test_the_first_draft_copies_the_code_default(self):
        template = A.create_draft(EVENT, 'email', self.admin)
        event = get_event(EVENT)
        self.assertEqual((template.version_no, template.subject, template.body,
                          template.button_label),
                         (1, event.default_subject, event.default_body, event.button_label))

    def test_a_change_note_is_required(self):
        with self.assertRaises(A.NotificationAdminRefused):
            A.update_draft(self.draft(), self.admin, 's', 'b', 'l', '   ')

    def test_unknown_event_and_undeclared_channel_are_refused(self):
        with self.assertRaises(A.NotificationAdminRefused):
            A.create_draft('no_such_event', 'email', self.admin)
        with self.assertRaises(A.NotificationAdminRefused):
            A.create_draft(EVENT, 'whatsapp', self.admin)
        with self.assertRaises(A.NotificationAdminRefused):
            A.set_state(EVENT, 'whatsapp', False, self.admin, 'reason')


class ValidationTests(Base):

    def test_unknown_and_undeclared_placeholders(self):
        template = self.draft(subject='Hi {{ request.user }}', body='{{ request }} {{ thing }}')
        errors = A.validate_template(template)
        self.assertEqual(len(errors), 3, errors)
        self.assertIn('unknown placeholder {{ request.user }}', errors[0])
        self.assertIn('{{ request }} is not a variable', errors[1])
        self.assertIn('{{ thing }} is not a variable', errors[2])

    def test_declared_and_reserved_placeholders_pass(self):
        template = self.draft(subject='{{ sender_name }}',
                              body='{{ recipient_name }} {{ link }} {{sender_name}}')
        self.assertEqual(A.validate_template(template), [])

    def test_empty_subject_and_long_body(self):
        template = self.draft(subject='  ', body='x' * 5001)
        errors = A.validate_template(template)
        self.assertEqual(len(errors), 2, errors)

    def test_activate_is_refused_while_there_are_errors(self):
        template = self.draft(body='{{ request }}')
        with self.assertRaises(A.NotificationAdminRefused):
            A.activate(template, self.admin)
        template.refresh_from_db()
        self.assertEqual(template.status, 'draft')


class RenderingTests(Base):

    def render(self, body, values=None):
        template = NotificationTemplate(event_code=EVENT, channel='email', subject='S',
                                        body=body, button_label='Go')
        values = {'sender_name': 'Asha', 'recipient_name': 'Bob', **(values or {})}
        return N.render_email(get_event(EVENT), template, values, '/x/')

    def test_a_value_is_escaped_in_html_and_plain_in_text(self):
        _, text, html = self.render('From {{ sender_name }}',
                                    {'sender_name': '<script>alert(1)</script>'})
        self.assertNotIn('<script>', html)
        self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;', html)
        self.assertIn('From <script>alert(1)</script>', text)

    def test_typed_html_in_the_body_is_escaped(self):
        _, _, html = self.render('<b>hi</b>')
        self.assertIn('&lt;b&gt;hi&lt;/b&gt;', html)

    def test_paragraphs_and_bold(self):
        _, text, html = self.render('One **two** three\n\n  \nFour')
        self.assertIn('>One <strong>two</strong> three</p><p', html)
        self.assertIn('>Four</p>', html)
        self.assertIn('One two three\n\nFour', text)

    def test_a_value_cannot_become_bold_or_a_placeholder(self):
        _, text, html = self.render('{{ sender_name }}', {'sender_name': '**x** {{ link }}'})
        self.assertNotIn('<strong>', html)
        self.assertIn('**x** {{ link }}', text)

    def test_template_tags_are_printed_literally(self):
        _, text, html = self.render('{% load static %}{% now "Y" %} done')
        self.assertIn('{% load static %}{% now &quot;Y&quot; %} done', html)
        self.assertIn('{% load static %}{% now "Y" %} done', text)

    def test_an_undeclared_name_is_refused_even_with_a_value(self):
        for body in ('{{ request }}', '{{ request.user }}', '{{ sender_name.upper }}'):
            with self.subTest(body=body), self.assertRaises(N.RenderError):
                self.render(body, {'request': 'leak'})

    def test_the_button_appears_only_with_a_link(self):
        event = get_event(EVENT)
        values = {'sender_name': 'A', 'recipient_name': 'B'}
        _, text, html = N.render_email(event, None, values, '')
        self.assertNotIn('<a href', html)
        self.assertNotIn('Open Horizon Solar PMS', text)
        with override_settings(APP_BASE_URL='https://pms.example.com/'):
            _, text, html = N.render_email(event, None, values, '/tasks/1/')
        self.assertIn('href="https://pms.example.com/tasks/1/"', html)
        self.assertIn('Open Horizon Solar PMS: https://pms.example.com/tasks/1/', text)

    def test_the_layout_comment_never_reaches_the_email(self):
        _, text, html = self.render('Body')
        self.assertNotIn('Shared layout', html)
        self.assertNotIn('<!--', html)
        self.assertNotIn('Autoescape', text)

    def test_render_preview_uses_the_samples(self):
        subject, text, html = A.render_preview(self.draft())
        self.assertEqual(subject, 'Test message from Horizon Solar PMS')
        self.assertIn('Hi Priya Sharma,', text)
        self.assertIn('<strong>Asha Rao</strong>', html)


class NotifyTests(Base):

    def test_actor_inactive_and_duplicates(self):
        gone = _profile('nt_gone', 'SCM')
        gone.is_active = False
        gone.save()
        locked = _profile('nt_locked', 'SCM')
        locked.user.is_active = False
        locked.user.save()
        self.send([self.alice, self.alice, self.bob, self.admin, gone, locked],
                  actor=self.admin)
        self.assertEqual(sorted(m['to'] for m in self.mail),
                         ['nt_alice@example.com', 'nt_bob@example.com'])
        self.assertEqual(Notification.objects.count(), 2)
        self.assertEqual(sorted(r.recipient.user.username for r in self.rows(channel='email')),
                         ['nt_alice', 'nt_bob'])

    def test_code_default_is_used_when_no_template_is_active(self):
        self.send([self.alice], link='/approvals/')
        email = self.rows(channel='email')[0]
        self.assertEqual((email.status, email.event_code, email.template_version),
                         ('sent', EVENT, None))
        in_app = self.rows(channel='in_app')[0]
        self.assertEqual((in_app.status, in_app.event_code, in_app.template_version),
                         ('sent', EVENT, None))
        self.assertEqual(Notification.objects.get().message, 'Test message from Asha.')
        self.assertEqual(Notification.objects.get().link, '/approvals/')
        self.assertEqual(self.mail[0]['subject'], 'Test message from Horizon Solar PMS')
        self.assertIn('<strong>Asha</strong>', self.mail[0]['html'])
        self.assertIn('Hi Nt_Alice,', self.mail[0]['text'])
        self.assertIn('/approvals/', self.mail[0]['html'])

    def test_the_active_template_is_used_and_recorded(self):
        template = self.draft(subject='Custom for {{ recipient_name }}')
        self.go_live(template)
        self.send([self.alice])
        self.assertEqual(self.mail[0]['subject'], 'Custom for Nt_Alice')
        email = self.rows(channel='email')[0]
        self.assertEqual((email.event_code, email.template_version_id), (EVENT, template.pk))
        self.assertIsNone(self.rows(channel='in_app')[0].template_version)

    def test_a_paused_event_logs_skipped_and_sends_no_email(self):
        A.set_state(EVENT, 'email', False, self.admin, 'Too noisy')
        self.send([self.alice])
        self.assertEqual(self.mail, [])
        email = self.rows(channel='email')[0]
        self.assertEqual((email.status, email.error_detail, email.event_code),
                         ('skipped', 'event paused', EVENT))
        self.assertEqual(self.rows(channel='in_app')[0].status, 'sent')

    def test_an_email_render_failure_logs_failed_and_in_app_still_goes(self):
        template = self.draft()
        template.body = 'Broken {{ nope }}'
        template.change_note = 'bypassing validation on purpose'
        template.save()
        template.activate()   # the model method; notify_admin.activate would refuse it
        with self.assertLogs('projects.notify', 'ERROR'):
            self.send([self.alice, self.bob])
        self.assertEqual(self.mail, [])
        failed = self.rows(channel='email')
        self.assertEqual([r.status for r in failed], ['failed', 'failed'])
        self.assertIn('RenderError', failed[0].error_detail)
        self.assertEqual((failed[0].event_code, failed[0].template_version_id),
                         (EVENT, template.pk))
        self.assertEqual([r.status for r in self.rows(channel='in_app')], ['sent', 'sent'])

    def test_an_in_app_render_failure_logs_failed_and_email_still_goes(self):
        broken = dataclasses.replace(get_event(EVENT), in_app_text='{{ nope }}')
        with mock.patch.dict(NOTIFICATION_EVENTS, {EVENT: broken}), \
                self.assertLogs('projects.notify', 'ERROR'):
            self.send([self.alice])
        in_app = self.rows(channel='in_app')[0]
        self.assertEqual((in_app.status, in_app.event_code, in_app.template_version),
                         ('failed', EVENT, None))
        self.assertEqual(Notification.objects.count(), 0)
        self.assertEqual(len(self.mail), 1)

    def test_a_missing_value_never_raises_to_the_caller(self):
        with self.assertLogs('projects.notify', 'ERROR'):
            self.send([self.alice], values={})
        self.assertEqual([r.status for r in self.rows()], ['failed', 'failed'])

    def test_a_rolled_back_transaction_sends_nothing(self):
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            try:
                with transaction.atomic():
                    N.notify(EVENT, recipients=[self.alice], values={'sender_name': 'A'})
                    raise RuntimeError('the action failed')
            except RuntimeError:
                pass
        self.assertEqual(callbacks, [])
        self.assertEqual((self.mail, self.rows()), ([], []))

    def test_an_unknown_event_raises(self):
        with self.assertRaises(KeyError):
            N.notify('no_such_event', recipients=[self.alice], values={})

    def test_nobody_left_registers_nothing(self):
        callbacks = self.send([self.admin], actor=self.admin)
        self.assertEqual(callbacks, [])


class SendNotificationUnchangedTests(Base):

    OLD_FIELDS = {'recipient', 'related_project', 'actor', 'message', 'template_name',
                  'channel', 'status', 'error_detail', 'interakt_message_id'}

    def test_without_the_new_arguments_the_log_rows_are_created_as_before(self):
        manager = NotificationLog.objects
        with mock.patch.object(manager, 'create', wraps=manager.create) as create:
            send_notification(self.alice, 'Hello', channels=['in_app', 'email', 'whatsapp'],
                              subject='S', template='some_label')
        self.assertEqual(create.call_count, 3)
        for call in create.call_args_list:
            self.assertEqual(set(call.kwargs), self.OLD_FIELDS)
        for row in self.rows():
            self.assertEqual((row.event_code, row.template_version), ('', None))
            self.assertEqual(row.template_name, 'some_label')


class SwitchTests(Base):

    def test_a_reason_is_required(self):
        for reason in ('', '   ', None):
            with self.subTest(reason=reason), self.assertRaises(A.NotificationAdminRefused):
                A.set_state(EVENT, 'email', False, self.admin, reason)
        self.assertFalse(NotificationEventSetting.objects.exists())
        self.assertFalse(NotificationEventSettingChange.objects.exists())

    def test_each_switch_writes_one_history_row(self):
        A.set_state(EVENT, 'email', False, self.admin, 'Pause it')
        A.send_test(EVENT, 'email', self.admin)   # T2 (A7): switching on needs a test
        A.set_state(EVENT, 'email', True, self.admin, 'Back on')
        setting = NotificationEventSetting.objects.get()
        self.assertEqual((setting.is_active, setting.reason), (True, 'Back on'))
        history = list(NotificationEventSettingChange.objects.order_by('pk')
                       .values_list('from_state', 'to_state', 'reason'))
        self.assertEqual(history, [('active', 'paused', 'Pause it'),
                                   ('paused', 'active', 'Back on')])

    def test_switching_to_the_current_state_is_refused(self):
        with self.assertRaises(A.NotificationAdminRefused):
            A.set_state(EVENT, 'email', True, self.admin, 'Already on')
        self.assertFalse(NotificationEventSettingChange.objects.exists())

    def test_history_rows_cannot_be_updated_or_deleted(self):
        A.set_state(EVENT, 'email', False, self.admin, 'Pause it')
        change = NotificationEventSettingChange.objects.get()
        change.reason = 'Rewritten'
        with self.assertRaises(AppendOnlyViolation):
            change.save()
        with self.assertRaises(AppendOnlyViolation):
            change.delete()


class RecipientHelperTests(Base):

    def test_role_and_flag_helpers_require_both_active_flags(self):
        system_admin = _profile('nt_sysadmin', 'System Admin')
        head = _profile('nt_head', 'Design', is_design_head=True)
        qc = _profile('nt_qc', 'PM', is_design_qc=True)
        retired_ceo = _profile('nt_ceo_old', 'CEO')
        retired_ceo.user.is_active = False
        retired_ceo.user.save()
        ceo = _profile('nt_ceo', 'CEO')

        def names(qs):
            return sorted(qs.values_list('user__username', flat=True))

        self.assertEqual(names(permissions.active_admins()), ['nt_admin'])
        self.assertNotIn(system_admin.user.username, names(permissions.active_admins()))
        self.assertEqual(names(permissions.active_design_heads()), [head.user.username])
        self.assertEqual(names(permissions.active_design_qc()), [qc.user.username])
        self.assertEqual(names(permissions.active_scm_users()), ['nt_alice'])
        self.assertEqual(names(permissions.active_finance_users()), ['nt_bob'])
        self.assertEqual(names(permissions.active_ceos()), [ceo.user.username])
