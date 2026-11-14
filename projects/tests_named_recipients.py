"""
NTF-DCG 5b — named recipients per event (rulings N1-N6, go-ahead answers 5b-Q1..Q6).

Every test uses TEST-ONLY events patched into the registry: no shipped event opts in to
named recipients in 5b. No HTTP leaves: ZeptoMail and Interakt are mocked, and any other
requests.Session call fails the test.

    NR_EVENT     email + WhatsApp, deferred, named_recipients=True
    NR_EMAIL     email only, named_recipients=True (badges for declared channels only)
    NR_PLAIN     email only, named_recipients=False (refused everywhere)
"""

import dataclasses
from io import StringIO
from unittest import mock

import requests
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, transaction
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import notify as N
from . import notify_admin as A
from .models import (
    ActivityLog, AppendOnlyViolation, NamedRecipientsNotAllowed, Notification,
    NotificationEventRecipient, NotificationEventSetting, NotificationLog, SystemSettings,
)
from .notify_registry import (
    NOTIFICATION_EVENTS, TIER_KNOW, NotificationEvent, Variable, WhatsAppSpec,
    _build_registry,
)
from .tests_approvals import _profile

NR_EVENT = 'nr5b_test_event'
NR_EMAIL = 'nr5b_email_only'
NR_PLAIN = 'nr5b_plain'
TEMPLATE = 'nr5b_template'
REASON = 'Finance must see every receipt'


def _wa_spec():
    return WhatsAppSpec(
        template=TEMPLATE, language='en',
        header_text='Receipt at {{1}}', body_text='Hello {{1}}, receipt {{2}}.',
        header_fixed_chars=len('Receipt at '), body_fixed_chars=len('Hello , receipt .'),
        param_count=3,
        params=lambda v: [v['site_name'], v['recipient_name'], v['grn_number']])


def _event(code, **changes):
    event = NotificationEvent(
        code=code, label=f'Test {code}', module='Tests', tier=TIER_KNOW,
        channels=('email',),
        variables=(Variable('site_name', 'Site', 'Sharma Residence'),
                   Variable('grn_number', 'Receipt', 'GRN-000001')),
        in_app_text='Receipt {{ grn_number }} at {{ site_name }}.',
        default_subject='Receipt {{ grn_number }}',
        default_body='Hi {{ recipient_name }}, receipt {{ grn_number }} at {{ site_name }}.',
        button_label='Open', recipients_rule='Tests only.', default_state='active')
    return dataclasses.replace(event, **changes)


def _registry_patch():
    events = _build_registry([
        _event(NR_EVENT, channels=('email', 'whatsapp'), deferred=True,
               whatsapp=_wa_spec(), named_recipients=True),
        _event(NR_EMAIL, named_recipients=True),
        _event(NR_PLAIN),
    ])
    return mock.patch.dict(NOTIFICATION_EVENTS, events)


@override_settings(INTERAKT_API_KEY='test-key')
class Base(TestCase):

    def setUp(self):
        self.admin = _profile('nr_admin', 'Admin', phone_number='9000000009')
        self.alice = _profile('nr_alice', 'SCM', phone_number='9876500001')
        self.bob = _profile('nr_bob', 'Finance', phone_number='9876500002')
        self.carol = _profile('nr_carol', 'Finance', phone_number='9876500003')
        for profile in (self.admin, self.alice, self.bob, self.carol):
            profile.user.email = f'{profile.user.username}@example.com'
            profile.user.save(update_fields=['email'])
        switches = SystemSettings.get()
        switches.whatsapp_enabled = True
        switches.email_enabled = True
        switches.save()

        self.mail, self.posts = [], []

        def mail(to_email, to_name, subject, text_body, html_body=None):
            self.mail.append(to_email)
            return True, ''

        def post(url, headers=None, json=None, timeout=None):
            self.posts.append(json['phoneNumber'])
            response = mock.Mock(status_code=201, text='{"result": true, "id": "w1"}')
            response.json.return_value = {'id': 'w1'}
            return response

        for patch in (
            mock.patch('projects.notifications._zeptomail_post', side_effect=mail),
            mock.patch('projects.notifications.requests.post', side_effect=post),
            mock.patch.object(requests.sessions.Session, 'request',
                              side_effect=AssertionError('an HTTP request was attempted')),
            _registry_patch(),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def activate_whatsapp(self, code=NR_EVENT):
        NotificationEventSetting.objects.update_or_create(
            event_code=code, channel='whatsapp',
            defaults={'is_active': True, 'changed_by': self.admin, 'reason': 'test'})

    def pause_email(self, code=NR_EVENT):
        NotificationEventSetting.objects.update_or_create(
            event_code=code, channel='email',
            defaults={'is_active': False, 'changed_by': self.admin, 'reason': 'test'})

    def name(self, profile, code=NR_EVENT):
        return A.add_named_recipient(code, profile, self.admin, REASON)

    def send(self, recipients=None, code=NR_EVENT, actor='admin'):
        actor = self.admin if actor == 'admin' else actor
        with self.captureOnCommitCallbacks(execute=True):
            N.notify(code, recipients=[self.alice] if recipients is None else recipients,
                     values={'site_name': 'Sharma Residence', 'grn_number': 'GRN-000007'},
                     link='/challans/7/', actor=actor)

    def rows(self, profile):
        return sorted((r.channel, r.status, r.error_detail) for r in
                      NotificationLog.objects.filter(recipient=profile))


# ---------------------------------------------------------------------------
# N3 — sending
# ---------------------------------------------------------------------------

class SendTests(Base):

    def test_named_recipient_receives_on_every_declared_channel(self):
        self.activate_whatsapp()
        self.name(self.bob)
        self.send()
        self.assertEqual(self.rows(self.bob), [('email', 'sent', ''), ('in_app', 'sent', ''),
                                               ('whatsapp', 'sent', '')])
        self.assertEqual(self.rows(self.alice), [('email', 'sent', ''), ('in_app', 'sent', ''),
                                                 ('whatsapp', 'sent', '')])
        self.assertEqual(sorted(self.mail), ['nr_alice@example.com', 'nr_bob@example.com'])
        self.assertEqual(sorted(self.posts), ['9876500001', '9876500002'])
        self.assertTrue(Notification.objects.filter(recipient=self.bob).exists())

    def test_a_code_resolved_duplicate_gets_one_send(self):
        self.activate_whatsapp()
        self.name(self.alice)
        self.send(recipients=[self.alice])
        self.assertEqual(self.rows(self.alice), [('email', 'sent', ''), ('in_app', 'sent', ''),
                                                 ('whatsapp', 'sent', '')])
        self.assertEqual(self.mail, ['nr_alice@example.com'])
        self.assertEqual(self.posts, ['9876500001'])

    def test_email_off_named_recipient_gets_a_skipped_row(self):
        self.bob.email_notifications = False
        self.bob.save(update_fields=['email_notifications'])
        self.name(self.bob)
        self.send()
        self.assertIn(('email', 'skipped', 'User preference off'), self.rows(self.bob))
        self.assertNotIn('nr_bob@example.com', self.mail)

    def test_missing_email_address_fails_as_today(self):
        self.bob.user.email = ''
        self.bob.user.save(update_fields=['email'])
        self.name(self.bob)
        self.send()
        self.assertIn(('email', 'failed', 'Recipient has no email address'), self.rows(self.bob))

    def test_inactive_profile_gets_one_skipped_inactive_row_per_channel(self):
        self.activate_whatsapp()
        self.name(self.bob)
        self.bob.is_active = False
        self.bob.save(update_fields=['is_active'])
        self.send()
        self.assertEqual(self.rows(self.bob), [('email', 'skipped', 'inactive'),
                                               ('in_app', 'skipped', 'inactive'),
                                               ('whatsapp', 'skipped', 'inactive')])
        self.assertFalse(Notification.objects.filter(recipient=self.bob).exists())
        self.assertEqual(self.posts, ['9876500001'])
        wa_row = NotificationLog.objects.get(recipient=self.bob, channel='whatsapp')
        self.assertEqual((wa_row.template_name, wa_row.event_code), (TEMPLATE, NR_EVENT))

    def test_inactive_user_counts_too(self):
        self.name(self.bob)
        self.bob.user.is_active = False
        self.bob.user.save(update_fields=['is_active'])
        self.send()
        self.assertEqual({r[2] for r in self.rows(self.bob)}, {'inactive'})

    def test_inactive_wins_over_event_paused(self):
        # WhatsApp is paused by default (W2), email paused here.
        self.pause_email()
        self.name(self.bob)
        self.bob.is_active = False
        self.bob.save(update_fields=['is_active'])
        self.send()
        self.assertEqual(self.rows(self.bob), [('email', 'skipped', 'inactive'),
                                               ('in_app', 'skipped', 'inactive'),
                                               ('whatsapp', 'skipped', 'inactive')])

    def test_removed_recipient_gets_nothing(self):
        row = self.name(self.bob)
        A.remove_named_recipient(row, self.admin, 'Moved to another team')
        self.send()
        self.assertEqual(self.rows(self.bob), [])

    def test_event_paused_skips_the_named_recipient_as_anyone(self):
        self.pause_email()
        self.name(self.bob)
        self.send()
        self.assertEqual(self.rows(self.bob), [('email', 'skipped', 'event paused'),
                                               ('in_app', 'sent', ''),
                                               ('whatsapp', 'skipped', 'event paused')])
        self.assertEqual(self.mail, [])

    def test_named_actor_still_receives(self):
        # Ruling 5b-Q2: the list is a record trail; the actor is not dropped.
        self.name(self.admin)
        self.send(actor='admin')
        self.assertIn(('in_app', 'sent', ''), self.rows(self.admin))
        self.assertIn(('email', 'sent', ''), self.rows(self.admin))

    def test_code_resolved_actor_is_still_excluded(self):
        self.send(recipients=[self.admin, self.alice], actor='admin')
        self.assertEqual(self.rows(self.admin), [])
        self.assertIn(('in_app', 'sent', ''), self.rows(self.alice))

    def test_named_recipients_receive_when_the_code_resolves_nobody(self):
        # Ruling 5b-Q4: notify()'s early return does not fire for an opted-in event.
        self.name(self.bob)
        self.send(recipients=[])
        self.assertIn(('in_app', 'sent', ''), self.rows(self.bob))

    def test_notify_now_merges_too(self):
        self.name(self.bob)
        N.notify_now(NR_EVENT, recipients=[], values={'site_name': 'S', 'grn_number': 'G'})
        self.assertIn(('in_app', 'sent', ''), self.rows(self.bob))

    def test_a_rolled_back_action_sends_nothing_to_named_recipients(self):
        self.name(self.bob)
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            N.notify(NR_EVENT, recipients=[], values={'site_name': 'S', 'grn_number': 'G'})
        self.assertEqual(len(callbacks), 1)
        self.assertEqual(self.rows(self.bob), [])

    def test_a_non_opted_event_ignores_named_rows(self):
        # No row can exist for it (N2); even one forced past the model is not read.
        NotificationEventRecipient.objects.bulk_create([NotificationEventRecipient(
            event_code=NR_PLAIN, recipient=self.bob, added_by=self.admin, add_reason=REASON)])
        self.send(code=NR_PLAIN)
        self.assertEqual(self.rows(self.bob), [])
        self.assertIn(('in_app', 'sent', ''), self.rows(self.alice))

    def test_a_non_opted_event_with_nobody_resolved_schedules_nothing(self):
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            N.notify(NR_PLAIN, recipients=[], values={'site_name': 'S', 'grn_number': 'G'})
        self.assertEqual(callbacks, [])


# ---------------------------------------------------------------------------
# N1 / N2 — the model
# ---------------------------------------------------------------------------

class ModelTests(Base):

    def test_re_adding_after_removal_creates_a_new_row_and_keeps_history(self):
        first = self.name(self.bob)
        A.remove_named_recipient(first, self.admin, 'Moved to another team')
        second = self.name(self.bob)
        self.assertNotEqual(first.pk, second.pk)
        rows = list(NotificationEventRecipient.objects.filter(recipient=self.bob)
                    .order_by('pk'))
        self.assertEqual([r.removed_at is None for r in rows], [False, True])
        self.assertEqual(rows[0].remove_reason, 'Moved to another team')

    def test_a_second_active_row_is_refused_by_the_constraint(self):
        self.name(self.bob)
        with self.assertRaises(IntegrityError), transaction.atomic():
            NotificationEventRecipient.objects.create(
                event_code=NR_EVENT, recipient=self.bob, added_by=self.admin,
                add_reason=REASON)

    def test_the_same_person_may_be_active_on_two_events(self):
        self.name(self.bob)
        self.name(self.bob, code=NR_EMAIL)
        self.assertEqual(NotificationEventRecipient.objects.filter(
            recipient=self.bob, removed_at__isnull=True).count(), 2)

    def test_model_refuses_an_event_that_does_not_opt_in(self):
        for code in (NR_PLAIN, 'no_such_event'):
            with self.subTest(code=code), self.assertRaises(NamedRecipientsNotAllowed):
                NotificationEventRecipient.objects.create(
                    event_code=code, recipient=self.bob, added_by=self.admin,
                    add_reason=REASON)

    def test_add_reason_is_required_by_the_database(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            NotificationEventRecipient.objects.create(
                event_code=NR_EVENT, recipient=self.bob, added_by=self.admin, add_reason='')

    def test_removal_is_all_three_fields_or_none(self):
        row = self.name(self.bob)
        with self.assertRaises(IntegrityError), transaction.atomic():
            NotificationEventRecipient.objects.filter(pk=row.pk).update(
                removed_at=timezone.now())

    def test_a_removed_row_never_changes_again(self):
        row = A.remove_named_recipient(self.name(self.bob), self.admin, 'Left the company')
        row.remove_reason = 'Something else entirely'
        with self.assertRaises(AppendOnlyViolation):
            row.save()

    def test_an_active_row_changes_only_by_removal(self):
        row = self.name(self.bob)
        row.add_reason = 'A different reason entirely'
        with self.assertRaises(AppendOnlyViolation):
            row.save()
        row = NotificationEventRecipient.objects.get(pk=row.pk)
        with self.assertRaises(AppendOnlyViolation):
            row.save()   # no removal: nothing to save

    def test_rows_are_never_deleted(self):
        row = self.name(self.bob)
        with self.assertRaises(AppendOnlyViolation):
            row.delete()


# ---------------------------------------------------------------------------
# notify_admin — the rules and the activity log
# ---------------------------------------------------------------------------

class AdminServiceTests(Base):

    def test_reasons_need_ten_characters(self):
        with self.assertRaisesRegex(A.NotificationAdminRefused, 'at least 10'):
            A.add_named_recipient(NR_EVENT, self.bob, self.admin, '  too short ')
        row = self.name(self.bob)
        with self.assertRaisesRegex(A.NotificationAdminRefused, 'at least 10'):
            A.remove_named_recipient(row, self.admin, 'short')
        self.assertIsNone(NotificationEventRecipient.objects.get(pk=row.pk).removed_at)

    def test_non_opted_event_refused(self):
        with self.assertRaisesRegex(A.NotificationAdminRefused, 'does not take named'):
            A.add_named_recipient(NR_PLAIN, self.bob, self.admin, REASON)

    def test_deactivated_person_refused(self):
        self.bob.is_active = False
        self.bob.save(update_fields=['is_active'])
        with self.assertRaisesRegex(A.NotificationAdminRefused, 'deactivated'):
            self.name(self.bob)

    def test_already_active_refused(self):
        self.name(self.bob)
        with self.assertRaisesRegex(A.NotificationAdminRefused, 'already a named'):
            self.name(self.bob)

    def test_removing_twice_refused(self):
        row = self.name(self.bob)
        A.remove_named_recipient(row, self.admin, 'Moved to another team')
        with self.assertRaisesRegex(A.NotificationAdminRefused, 'already removed'):
            A.remove_named_recipient(row, self.admin, 'Moved to another team')

    def test_add_and_remove_each_write_one_activity_line(self):
        row = self.name(self.bob)
        A.remove_named_recipient(row, self.admin, 'Moved to another team')
        logs = ActivityLog.objects.filter(entity_type='NotificationEventRecipient',
                                          entity_id=row.pk).order_by('pk')
        self.assertEqual([l.action_code for l in logs],
                         [A.ACTION_NAMED_ADDED, A.ACTION_NAMED_REMOVED])
        self.assertTrue(all(l.actor == self.admin and l.project_id is None for l in logs))
        self.assertIn(REASON, logs[0].action)
        self.assertIn('Moved to another team', logs[1].action)


# ---------------------------------------------------------------------------
# N4 — the Admin screen
# ---------------------------------------------------------------------------

class ScreenTests(Base):

    def setUp(self):
        super().setUp()
        self.client = Client(SERVER_NAME='localhost')
        self.client.force_login(self.admin.user)

    def page(self, code=NR_EVENT):
        response = self.client.get(reverse('admin_notification_event', args=[code]))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def add(self, profile, reason=REASON, code=NR_EVENT, client=None):
        return (client or self.client).post(
            reverse('admin_notification_named_add', args=[code]),
            {'recipient': profile.pk, 'reason': reason})

    def remove(self, row, reason='Moved to another team', code=NR_EVENT, client=None):
        return (client or self.client).post(
            reverse('admin_notification_named_remove', args=[code, row.pk]),
            {'reason': reason})

    def test_card_shown_only_for_opted_in_events(self):
        self.assertIn('id="named-recipients"', self.page(NR_EVENT))
        self.assertNotIn('id="named-recipients"', self.page(NR_PLAIN))

    def test_admin_adds_and_removes(self):
        response = self.add(self.bob)
        self.assertRedirects(response, reverse('admin_notification_event', args=[NR_EVENT]),
                             fetch_redirect_response=False)
        row = NotificationEventRecipient.objects.get(event_code=NR_EVENT, recipient=self.bob)
        self.assertEqual((row.added_by, row.add_reason), (self.admin, REASON))
        content = self.page()
        self.assertIn('Nr_Bob', content)
        self.assertIn(REASON, content)
        self.remove(row)
        row.refresh_from_db()
        self.assertEqual((row.removed_by, row.remove_reason),
                         (self.admin, 'Moved to another team'))
        self.assertIn('Removed (1)', self.page())

    def test_reasons_required_on_the_screen(self):
        self.add(self.bob, reason='short')
        self.assertFalse(NotificationEventRecipient.objects.exists())
        row = self.name(self.bob)
        self.remove(row, reason='')
        row.refresh_from_db()
        self.assertIsNone(row.removed_at)

    def test_non_opted_event_refused_on_the_screen(self):
        self.add(self.bob, code=NR_PLAIN)
        self.assertFalse(NotificationEventRecipient.objects.exists())

    def test_others_get_403(self):
        row = self.name(self.bob)
        for role in ('System Admin', 'PM', 'Finance', 'CEO', 'SCM'):
            with self.subTest(role=role):
                other = _profile(f'nr_{role.lower().replace(" ", "_")}', role)
                client = Client(SERVER_NAME='localhost')
                client.force_login(other.user)
                self.assertEqual(self.add(self.carol, client=client).status_code, 403)
                self.assertEqual(self.remove(row, client=client).status_code, 403)
        self.assertFalse(NotificationEventRecipient.objects.filter(recipient=self.carol).exists())
        row.refresh_from_db()
        self.assertIsNone(row.removed_at)

    def test_another_events_row_is_a_404(self):
        row = self.name(self.bob, code=NR_EMAIL)
        self.assertEqual(self.remove(row, code=NR_EVENT).status_code, 404)

    def test_get_on_the_actions_changes_nothing(self):
        self.client.get(reverse('admin_notification_named_add', args=[NR_EVENT]))
        self.assertFalse(NotificationEventRecipient.objects.exists())

    def test_warning_badges_follow_the_declared_channels(self):
        self.bob.email_notifications = False
        self.bob.whatsapp_notifications = False
        self.bob.phone_number = ''
        self.bob.save()
        self.bob.user.email = ''
        self.bob.user.save(update_fields=['email'])
        self.name(self.bob)
        self.name(self.bob, code=NR_EMAIL)

        both = self.page(NR_EVENT)
        for badge in ('email off', 'WhatsApp off', 'no email address', 'no valid phone'):
            self.assertIn(f'>{badge}</span>', both)
        email_only = self.page(NR_EMAIL)
        for badge in ('email off', 'no email address'):
            self.assertIn(f'>{badge}</span>', email_only)
        for badge in ('WhatsApp off', 'no valid phone'):
            self.assertNotIn(f'>{badge}</span>', email_only)

    def test_inactive_badge_and_clean_recipient_has_none(self):
        self.name(self.bob)
        self.name(self.carol)
        self.carol.is_active = False
        self.carol.save(update_fields=['is_active'])
        content = self.page()
        self.assertEqual(content.count('>inactive</span>'), 1)
        self.assertEqual(content.count('class="named-warning'), 1)

    def test_deactivated_and_already_named_people_are_not_offered(self):
        self.name(self.bob)
        self.carol.is_active = False
        self.carol.save(update_fields=['is_active'])
        content = self.page()
        self.assertIn(f'<option value="{self.alice.pk}">', content)
        self.assertNotIn(f'<option value="{self.bob.pk}">', content)
        self.assertNotIn(f'<option value="{self.carol.pk}">', content)


# ---------------------------------------------------------------------------
# N5 — seed_named_recipients
# ---------------------------------------------------------------------------

class SeedCommandTests(Base):

    def run_seed(self, *args, event=NR_EVENT, users=('nr_bob', 'nr_carol')):
        out = StringIO()
        argv = ['--as', 'nr_admin', '--event', event, '--reason', REASON]
        for username in users:
            argv += ['--user', username]
        call_command('seed_named_recipients', *argv, *args, stdout=out)
        return out.getvalue()

    def test_dry_run_writes_nothing(self):
        output = self.run_seed()
        self.assertFalse(NotificationEventRecipient.objects.exists())
        self.assertFalse(ActivityLog.objects.filter(action_code=A.ACTION_NAMED_ADDED).exists())
        self.assertIn('[dry run] Would add 2', output)

    def test_apply_adds_and_is_idempotent(self):
        self.run_seed('--apply')
        rows = NotificationEventRecipient.objects.filter(event_code=NR_EVENT)
        self.assertEqual({r.recipient for r in rows}, {self.bob, self.carol})
        self.assertTrue(all(r.added_by == self.admin and r.add_reason == REASON for r in rows))
        output = self.run_seed('--apply')
        self.assertEqual(NotificationEventRecipient.objects.count(), 2)
        self.assertIn('Added 0; already active 2', output)
        self.assertEqual(ActivityLog.objects.filter(
            action_code=A.ACTION_NAMED_ADDED).count(), 2)

    def test_non_opted_event_refused(self):
        with self.assertRaisesRegex(CommandError, 'does not take named recipients'):
            self.run_seed('--apply', event=NR_PLAIN)
        self.assertFalse(NotificationEventRecipient.objects.exists())

    def test_only_an_active_admin_may_run_it(self):
        _profile('nr_sysadmin', 'System Admin')
        with self.assertRaisesRegex(CommandError, 'not an active Admin'):
            call_command('seed_named_recipients', '--as', 'nr_sysadmin', '--event', NR_EVENT,
                         '--user', 'nr_bob', '--reason', REASON, '--apply', stdout=StringIO())

    def test_unknown_and_deactivated_users_are_refused_not_fatal(self):
        self.carol.is_active = False
        self.carol.save(update_fields=['is_active'])
        output = self.run_seed('--apply', users=('nr_bob', 'nr_carol', 'nobody_here'))
        self.assertEqual(list(NotificationEventRecipient.objects.values_list(
            'recipient', flat=True)), [self.bob.pk])
        self.assertIn('refuse  nr_carol: deactivated', output)
        self.assertIn('refuse  nobody_here: no such user', output)

    def test_short_reason_refused(self):
        with self.assertRaisesRegex(CommandError, 'at least 10'):
            call_command('seed_named_recipients', '--as', 'nr_admin', '--event', NR_EVENT,
                         '--user', 'nr_bob', '--reason', 'short', stdout=StringIO())


class ShippedRegistryTests(TestCase):
    """5b was infrastructure only. Two events opt in: NTF-DCG 5c's goods-receipt email
    (ruling R5: Finance recipients are named by Admin, not coded) and SNAP-1's daily
    dashboard snapshot, whose named list is its only audience (the code names nobody)."""

    def test_the_events_that_take_named_recipients(self):
        self.assertEqual([c for c, e in NOTIFICATION_EVENTS.items() if e.named_recipients],
                         ['grn_recorded', 'daily_dashboard_snapshot'])

    def test_the_flag_defaults_to_false(self):
        field = {f.name: f for f in dataclasses.fields(NotificationEvent)}
        self.assertIs(field['named_recipients'].default, False)
