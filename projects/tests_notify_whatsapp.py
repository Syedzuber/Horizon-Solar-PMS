"""NTF-DCG Session 3a — WhatsApp as a registry channel for deferred events.

WHAT IS PINNED
  W4         sanitise_params(): breaks, tabs and 4+ spaces to one space, trimmed, empty to
             "-", header cut so the RENDERED header fits HEADER_LIMIT, body values cut to
             BODY_VALUE_LIMIT, the rendered body kept under BODY_LIMIT by cutting the
             longest values first; Hindi kept intact, and a cut keeps a letter's marks.
  W1/Q5      the import-time registry check: deferred required, channel and spec
             together, param_count against the builder, Meta's layout rules on the
             declared header and body text, fixed lengths against the text.
  W2         a WhatsApp channel is paused by default whatever default_state says; the
             screens show the same state channel_is_active() decides (Q1).
  W3/W5/W9   the send path, with a TEST-ONLY event patched into the registry (W8):
             paused ⇒ skipped row, nothing sent; active ⇒ one pending row, then sent with
             the values in the right slots; non-2xx ⇒ failed with the response cut; no
             or bad number ⇒ failed with the plain reason; master off ⇒ skipped; rollback
             ⇒ nothing; one message per phone per send; email unaffected.
  Q3         set_state() switches WhatsApp on only after a sent W7 test of the current
             template within 14 days.
  W7/W10     test_whatsapp --event: refusals, dry run, the logged test row; legacy mode
             prints no part of the API key.
  Legacy     a legacy send_notification() WhatsApp posts the same payload as before S3a.
  Shipped    no real registry event declares WhatsApp in this session (W8).

INTERCEPTION. Interakt is `projects.notifications.requests.post` (a recorder); ZeptoMail
is `_zeptomail_post`; `requests.sessions.Session.request` raises, so any other HTTP call
fails the test. NOTIFY_DEFERRED_SYNC (test_settings) runs the deferred job inline.

Run with:
    python manage.py test projects.tests_notify_whatsapp --settings=solarpms.test_settings
"""
import dataclasses
import io
import json
import unicodedata
from datetime import timedelta
from unittest import mock

import requests
from django.core.exceptions import ImproperlyConfigured
from django.core.management import CommandError, call_command
from django.db import transaction
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import notify as N
from . import notify_admin as A
from .models import NotificationEventSetting, NotificationLog, SystemSettings
from .notifications import NO_VALID_WHATSAPP_NUMBER, send_notification
from .notify_registry import (
    NOTIFICATION_EVENTS, TIER_KNOW, NotificationEvent, Variable, WhatsAppSpec,
    _build_registry,
)
from .tests_approvals import _profile
from .whatsapp_params import (
    BODY_LIMIT, BODY_VALUE_LIMIT, ELLIPSIS, HEADER_LIMIT, clean_value, cut,
    describe_params, sanitise_params,
)

# ---------------------------------------------------------------------------
# The test-only event (W8). Never added to NOTIFICATION_EVENTS outside a patch.
# ---------------------------------------------------------------------------

HEADER_TEXT = 'Delivery at {{1}} today'
BODY_TEXT = 'Hello {{1}}, challan {{2}} was created. Open {{3}} to see it.'
TEMPLATE = 's3a_test_template'
WA_EVENT = 's3a_wa_test'
MIXED_EVENT = 's3a_mixed_test'


def _builder(values):
    return [values['site_name'], values['recipient_name'], values['dc_number'],
            values['link']]


def _spec(**changes):
    spec = WhatsAppSpec(
        template=TEMPLATE, language='en',
        header_text=HEADER_TEXT, body_text=BODY_TEXT,
        header_fixed_chars=len('Delivery at  today'),
        body_fixed_chars=len('Hello , challan  was created. Open  to see it.'),
        param_count=4, params=_builder)
    return dataclasses.replace(spec, **changes)


def _event(code=WA_EVENT, **changes):
    event = NotificationEvent(
        code=code, label='S3a WhatsApp test', module='Tests', tier=TIER_KNOW,
        channels=('whatsapp',),
        variables=(Variable('site_name', 'Site', 'Sharma Residence'),
                   Variable('dc_number', 'Challan number', 'DC-001')),
        in_app_text='Challan {{ dc_number }} at {{ site_name }}.',
        default_subject='Challan {{ dc_number }}',
        default_body='Hi {{ recipient_name }}, challan {{ dc_number }} at {{ site_name }}.',
        button_label='Open', recipients_rule='Tests only.',
        # 'active' on purpose: the WhatsApp channel must still start paused (W2).
        default_state='active', deferred=True, whatsapp=_spec())
    return dataclasses.replace(event, **changes)


def _registry_patch():
    """Both test events in the registry for one test, built by the real check."""
    events = _build_registry([
        _event(),
        # Email and WhatsApp together; email paused by default, so a test can show the
        # channels are independent.
        _event(MIXED_EVENT, channels=('email', 'whatsapp'), default_state='paused'),
    ])
    return mock.patch.dict(NOTIFICATION_EVENTS, events)


def _response(status=201, text='{"result": true, "id": "wamid-1"}', data=None):
    response = mock.Mock(status_code=status, text=text)
    response.json.return_value = data if data is not None else {'id': 'wamid-1'}
    return response


# ---------------------------------------------------------------------------
# W4 — the pure helper
# ---------------------------------------------------------------------------

class CleanValueTests(SimpleTestCase):

    def test_newlines_become_one_space(self):
        self.assertEqual(clean_value('Line one\nLine two\r\n\r\nthree'),
                         'Line one Line two three')

    def test_tabs_become_one_space(self):
        self.assertEqual(clean_value('a\tb \t c'), 'a b c')

    def test_four_or_more_spaces_become_one_and_three_are_kept(self):
        self.assertEqual(clean_value('a    b'), 'a b')
        self.assertEqual(clean_value('a          b'), 'a b')
        self.assertEqual(clean_value('a   b'), 'a   b')

    def test_trimmed(self):
        self.assertEqual(clean_value('   padded \n'), 'padded')

    def test_empty_and_none_become_a_dash(self):
        for value in ('', '   ', '\n\t', None):
            self.assertEqual(clean_value(value), '-')

    def test_non_strings_are_stringified(self):
        self.assertEqual(clean_value(42), '42')


class CutTests(SimpleTestCase):

    def test_short_text_untouched(self):
        self.assertEqual(cut('abc', 3), 'abc')

    def test_cut_ends_with_ellipsis_within_the_limit(self):
        out = cut('abcdefghij', 5)
        self.assertEqual(out, 'abcd' + ELLIPSIS)
        self.assertEqual(len(out), 5)

    def test_hindi_kept_intact_when_it_fits(self):
        text = 'साइट पर चालान बनाया गया'
        self.assertEqual(cut(text, 250), text)
        self.assertEqual(sanitise_params(['शर्मा निवास', text])[1], text)

    def test_a_cut_never_leaves_a_stray_vowel_sign(self):
        # 'कि' is क + the vowel sign ि (a combining mark). Cutting between them would
        # leave a bare क or, worse, a dangling sign; the letter goes with its mark.
        text = 'नमस्ते किसान'
        for limit in range(1, len(text)):
            out = cut(text, limit)
            self.assertLessEqual(len(out), limit)
            kept = out[:-1]
            self.assertTrue(text.startswith(kept))
            if kept:
                # The next character dropped is never a mark of the last one kept.
                self.assertFalse(unicodedata.category(text[len(kept)]).startswith('M'),
                                 (limit, out))


class SanitiseTests(SimpleTestCase):

    def test_header_cut_counts_its_fixed_text(self):
        out = sanitise_params(['x' * 100, 'b'], header_fixed_chars=12)
        self.assertEqual(len(out[0]) + 12, HEADER_LIMIT)
        self.assertTrue(out[0].endswith(ELLIPSIS))

    def test_body_value_cut_to_its_limit(self):
        out = sanitise_params(['h', 'y' * 400, 'short'])
        self.assertEqual(len(out[1]), BODY_VALUE_LIMIT)
        self.assertTrue(out[1].endswith(ELLIPSIS))
        self.assertEqual(out[2], 'short')

    def test_total_body_overflow_cuts_the_longest_values_first(self):
        fixed = 400
        params = ['h', 'a' * 250, 'b' * 250, 'c' * 120, 'short']
        out = sanitise_params(params, body_fixed_chars=fixed)
        body = out[1:]
        self.assertLessEqual(sum(len(v) for v in body) + fixed, BODY_LIMIT)
        # 250+250+120+5 = 625 > 624: the two longest are cut, the others untouched.
        self.assertEqual(body[2], 'c' * 120)
        self.assertEqual(body[3], 'short')
        self.assertTrue(body[0].endswith(ELLIPSIS) and body[1].endswith(ELLIPSIS))

    def test_heavy_overflow_still_fits_and_never_empties_a_value(self):
        out = sanitise_params(['h'] + ['z' * 250] * 4, body_fixed_chars=1000)
        self.assertLessEqual(sum(len(v) for v in out[1:]) + 1000, BODY_LIMIT)
        self.assertTrue(all(out[1:]))

    def test_cleaning_happens_before_cutting(self):
        out = sanitise_params(['Site\nA', 'one\ttwo', ''])
        self.assertEqual(out, ['Site A', 'one two', '-'])

    def test_describe_marks_the_slots(self):
        self.assertEqual(describe_params(['H', 'a', 'b']), 'header: H | body: a | b')
        self.assertEqual(describe_params(['H']), 'header: H')


# ---------------------------------------------------------------------------
# W1 / Q5 — the registry check
# ---------------------------------------------------------------------------

class RegistryCheckTests(SimpleTestCase):

    def assertRefused(self, event, fragment):
        with self.assertRaises(ImproperlyConfigured) as caught:
            _build_registry([event])
        self.assertIn(fragment, str(caught.exception))

    def test_the_test_event_is_valid(self):
        self.assertIn(WA_EVENT, _build_registry([_event()]))

    def test_a_non_deferred_event_declaring_whatsapp_is_an_error(self):
        self.assertRefused(_event(deferred=False), 'declares WhatsApp but is not deferred')

    def test_channel_and_spec_go_together(self):
        self.assertRefused(_event(whatsapp=None),
                           'lists the whatsapp channel but declares no WhatsAppSpec')
        self.assertRefused(_event(channels=('email',)),
                           'does not list the whatsapp channel')

    def test_builder_length_must_equal_param_count(self):
        self.assertRefused(_event(whatsapp=_spec(params=lambda v: ['a', 'b'])),
                           'returns 2 values, param_count is 4')

    def test_a_builder_that_fails_on_the_samples_is_an_error(self):
        self.assertRefused(_event(whatsapp=_spec(params=lambda v: [v['nope']] * 4)),
                           'fails on the samples')

    def test_header_may_begin_or_end_with_its_variable(self):
        # NTF-DCG 3b, ruling D1: Meta approved headers ending in their variable, so the
        # begin/end rule is the body's only (the body test below is unchanged).
        for header in ('{{1}} delivery', 'Delivery {{1}}'):
            with self.subTest(header=header):
                _build_registry([_event(whatsapp=_spec(header_text=header,
                                                       header_fixed_chars=9))])

    def test_body_may_not_begin_or_end_with_a_variable(self):
        body = '{{1}} challan {{2}} open {{3}} now'
        self.assertRefused(_event(whatsapp=_spec(body_text=body,
                                                 body_fixed_chars=len(' challan  open  now'))),
                           'body may not begin with a variable')
        body = 'Hi {{1}} challan {{2}} open {{3}}'
        self.assertRefused(_event(whatsapp=_spec(body_text=body,
                                                 body_fixed_chars=len('Hi  challan  open '))),
                           'body may not end with a variable')

    def test_body_may_not_hold_two_adjacent_variables(self):
        body = 'Hi {{1}} {{2}} open {{3}} now'
        self.assertRefused(_event(whatsapp=_spec(body_text=body,
                                                 body_fixed_chars=len('Hi   open  now'))),
                           'two variables side by side')

    def test_variables_must_number_one_to_n(self):
        body = 'Hello {{1}}, challan {{2}} was created. Open {{5}} to see it.'
        self.assertRefused(_event(whatsapp=_spec(body_text=body)), 'once each')

    def test_a_non_numeric_placeholder_is_an_error(self):
        body = 'Hello {{1}}, {{ name }} challan {{2}} was created. Open {{3}} to see it.'
        self.assertRefused(_event(whatsapp=_spec(body_text=body)), 'not a number')

    def test_fixed_chars_must_match_the_text(self):
        self.assertRefused(_event(whatsapp=_spec(header_fixed_chars=3)),
                           'header_fixed_chars is 3, the text has 18')
        self.assertRefused(_event(whatsapp=_spec(body_fixed_chars=1)),
                           'body_fixed_chars is 1')

    def test_default_state_for_pauses_whatsapp_only(self):
        event = _event()
        self.assertEqual(event.default_state_for('whatsapp'), 'paused')
        self.assertEqual(event.default_state_for('email'), 'active')

    def test_no_shipped_event_declares_whatsapp(self):
        # W8: infrastructure only in 3a; NTF-DCG 3b added the two challan events.
        # NTF-DCG 5b (N6): each is asserted by name; the full WhatsApp list is not pinned.
        from .tests_notify import DCG_3B_EVENTS, SHIPPED_BEFORE_DCG  # lazy only to keep the N6 change inside this test; tests_notify never imports this module, so no circular import
        for code in SHIPPED_BEFORE_DCG:
            with self.subTest(event=code):
                event = NOTIFICATION_EVENTS[code]
                self.assertTrue(event.whatsapp is None and 'whatsapp' not in event.channels)
        for code in DCG_3B_EVENTS:
            with self.subTest(event=code):
                event = NOTIFICATION_EVENTS[code]
                self.assertTrue(event.whatsapp is not None and 'whatsapp' in event.channels)
        self.assertNotIn(WA_EVENT, NOTIFICATION_EVENTS)


# ---------------------------------------------------------------------------
# W2, W3, W5, W9 — the send path
# ---------------------------------------------------------------------------

@override_settings(INTERAKT_API_KEY='test-key')
class SendBase(TestCase):

    def setUp(self):
        self.admin = _profile('wa_admin', 'Admin', phone_number='9000000009')
        self.alice = _profile('wa_alice', 'Site Engineer', phone_number='9876500001')
        self.bob = _profile('wa_bob', 'Site Engineer', phone_number='9876500002')
        for profile in (self.admin, self.alice, self.bob):
            profile.user.email = f'{profile.user.username}@example.com'
            profile.user.save(update_fields=['email'])
        switches = SystemSettings.get()
        switches.whatsapp_enabled = True
        switches.email_enabled = True
        switches.save()

        self.posts = []
        self.status_at_post = []
        self.response = _response()

        def post(url, headers=None, json=None, timeout=None):
            self.status_at_post.append(list(
                NotificationLog.objects.filter(channel='whatsapp')
                .values_list('status', flat=True)))
            self.posts.append({'url': url, 'headers': headers, 'json': json})
            return self.response

        patches = [
            mock.patch('projects.notifications.requests.post', side_effect=post),
            mock.patch('projects.notifications._zeptomail_post', return_value=(True, '')),
            mock.patch.object(requests.sessions.Session, 'request',
                              side_effect=AssertionError('an HTTP request was attempted')),
            _registry_patch(),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def activate(self, code=WA_EVENT, channel='whatsapp', on=True):
        NotificationEventSetting.objects.update_or_create(
            event_code=code, channel=channel,
            defaults={'is_active': on, 'changed_by': self.admin, 'reason': 'test'})

    def send(self, recipients=None, code=WA_EVENT, values=None):
        with self.captureOnCommitCallbacks(execute=True):
            N.notify(code, recipients=recipients or [self.alice],
                     values=values or {'site_name': 'Sharma Residence', 'dc_number': 'DC-7'},
                     link='/challans/7/', actor=self.admin)

    def wa_rows(self):
        return list(NotificationLog.objects.filter(channel='whatsapp').order_by('pk'))


class PauseTests(SendBase):

    def test_paused_by_default_even_when_default_state_is_active(self):
        event = NOTIFICATION_EVENTS[WA_EVENT]
        self.assertEqual(event.default_state, 'active')
        self.assertFalse(N.channel_is_active(event, 'whatsapp'))

    def test_paused_gives_one_skipped_row_and_sends_nothing(self):
        self.send()
        rows = self.wa_rows()
        self.assertEqual([(r.status, r.error_detail, r.template_name, r.event_code)
                          for r in rows],
                         [('skipped', 'event paused', TEMPLATE, WA_EVENT)])
        self.assertIsNone(rows[0].finished_at)
        self.assertEqual(self.posts, [])
        # In-app is still sent: the channels are independent.
        self.assertTrue(NotificationLog.objects.filter(channel='in_app', status='sent',
                                                       recipient=self.alice).exists())

    def test_an_explicit_pause_row_also_skips(self):
        self.activate(on=False)
        self.send()
        self.assertEqual([r.status for r in self.wa_rows()], ['skipped'])
        self.assertEqual(self.posts, [])


class ActiveSendTests(SendBase):

    def setUp(self):
        super().setUp()
        self.activate()

    def test_pending_then_sent_with_values_in_the_right_slots(self):
        self.send()
        # The row existed, pending, when Interakt was called (W3).
        self.assertEqual(self.status_at_post, [['pending']])
        [row] = self.wa_rows()
        self.assertEqual((row.status, row.interakt_message_id, row.template_name,
                          row.event_code), ('sent', 'wamid-1', TEMPLATE, WA_EVENT))
        self.assertIsNotNone(row.finished_at)
        [post] = self.posts
        template = post['json']['template']
        self.assertEqual(template['name'], TEMPLATE)
        self.assertEqual(template['languageCode'], 'en')
        self.assertEqual(template['headerValues'], ['Sharma Residence'])
        self.assertEqual(template['bodyValues'][:2], ['Wa_Alice', 'DC-7'])
        self.assertTrue(template['bodyValues'][2].endswith('/challans/7/'))
        self.assertEqual(post['json']['phoneNumber'], '9876500001')
        # Q7: the row records exactly what was sent, slots marked.
        self.assertEqual(row.message, 'header: Sharma Residence | body: '
                         + ' | '.join(template['bodyValues']))

    def test_values_are_sanitised_before_the_send(self):
        self.send(values={'site_name': 'Sharma\nResidence', 'dc_number': '   '})
        template = self.posts[0]['json']['template']
        self.assertEqual(template['headerValues'], ['Sharma Residence'])
        self.assertEqual(template['bodyValues'][1], '-')

    def test_one_row_per_recipient(self):
        self.send(recipients=[self.alice, self.bob])
        self.assertEqual([(r.recipient_id, r.status) for r in self.wa_rows()],
                         [(self.alice.pk, 'sent'), (self.bob.pk, 'sent')])
        self.assertEqual(len(self.posts), 2)

    def test_non_2xx_is_failed_with_the_response_cut(self):
        self.response = _response(status=400, text='E' * 5000)
        self.send()
        [row] = self.wa_rows()
        self.assertEqual(row.status, 'failed')
        self.assertTrue(row.error_detail.startswith('HTTP 400: EEE'))
        self.assertLessEqual(len(row.error_detail), 1000)
        self.assertEqual(row.interakt_message_id, '')

    def test_any_2xx_is_sent_even_without_an_id(self):
        self.response = _response(status=202, text='accepted', data={})
        self.send()
        self.assertEqual([(r.status, r.interakt_message_id) for r in self.wa_rows()],
                         [('sent', '')])

    def test_no_or_malformed_number_is_failed_with_the_plain_reason(self):
        for phone in ('', '12345', '5876500001'):
            NotificationLog.objects.all().delete()
            self.alice.phone_number = phone
            self.alice.save(update_fields=['phone_number'])
            self.send()
            self.assertEqual([(r.status, r.error_detail) for r in self.wa_rows()],
                             [('failed', NO_VALID_WHATSAPP_NUMBER)], phone)
        self.assertEqual(self.posts, [])

    def test_master_switch_off_is_skipped(self):
        switches = SystemSettings.get()
        switches.whatsapp_enabled = False
        switches.save()
        self.send()
        self.assertEqual([(r.status, r.error_detail) for r in self.wa_rows()],
                         [('skipped', 'Master switch off')])
        self.assertEqual(self.posts, [])

    def test_preference_off_is_skipped(self):
        self.alice.whatsapp_notifications = False
        self.alice.save(update_fields=['whatsapp_notifications'])
        self.send()
        self.assertEqual([(r.status, r.error_detail) for r in self.wa_rows()],
                         [('skipped', 'User preference off')])

    def test_rollback_sends_and_logs_nothing(self):
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            try:
                with transaction.atomic():
                    N.notify(WA_EVENT, recipients=[self.alice],
                             values={'site_name': 'S', 'dc_number': 'D'})
                    raise RuntimeError('the action failed')
            except RuntimeError:
                pass
        self.assertEqual(callbacks, [])
        self.assertEqual((self.posts, list(NotificationLog.objects.all())), ([], []))

    def test_a_builder_returning_the_wrong_count_at_send_time_is_failed(self):
        events = {WA_EVENT: dataclasses.replace(
            NOTIFICATION_EVENTS[WA_EVENT],
            whatsapp=_spec(params=lambda v: ['only one']))}
        with mock.patch.dict(NOTIFICATION_EVENTS, events):
            self.send()
        [row] = self.wa_rows()
        self.assertEqual(row.status, 'failed')
        self.assertIn('param_count is 4', row.error_detail)
        self.assertEqual(self.posts, [])


class DuplicatePhoneTests(SendBase):

    def setUp(self):
        super().setUp()
        self.bob.phone_number = self.alice.phone_number
        self.bob.save(update_fields=['phone_number'])

    def test_one_message_per_phone_first_recipient_wins(self):
        self.activate()
        self.send(recipients=[self.alice, self.bob])
        self.assertEqual([(r.recipient_id, r.status, r.error_detail) for r in self.wa_rows()],
                         [(self.alice.pk, 'sent', ''),
                          (self.bob.pk, 'skipped', 'duplicate phone in this send')])
        self.assertEqual(len(self.posts), 1)

    def test_email_is_unaffected(self):
        self.activate(MIXED_EVENT, 'whatsapp')
        self.activate(MIXED_EVENT, 'email')
        self.send(recipients=[self.alice, self.bob], code=MIXED_EVENT)
        email = NotificationLog.objects.filter(channel='email').order_by('pk')
        self.assertEqual([(r.recipient_id, r.status) for r in email],
                         [(self.alice.pk, 'sent'), (self.bob.pk, 'sent')])
        self.assertEqual([r.status for r in self.wa_rows()], ['sent', 'skipped'])

    def test_separate_sends_each_reach_the_phone(self):
        self.activate()
        self.send(recipients=[self.alice])
        self.send(recipients=[self.bob])
        self.assertEqual([r.status for r in self.wa_rows()], ['sent', 'sent'])


class ChannelIndependenceTests(SendBase):

    def test_paused_email_does_not_stop_whatsapp(self):
        # MIXED_EVENT's email is paused by default_state; WhatsApp is switched on.
        self.activate(MIXED_EVENT, 'whatsapp')
        self.send(code=MIXED_EVENT)
        self.assertEqual(
            sorted(NotificationLog.objects.exclude(channel='in_app')
                   .values_list('channel', 'status')),
            [('email', 'skipped'), ('whatsapp', 'sent')])


# ---------------------------------------------------------------------------
# Q1 — the screens show the state notify() uses
# ---------------------------------------------------------------------------

class ScreenStateTests(SendBase):

    def screen_states(self):
        client = Client(SERVER_NAME='localhost')
        client.force_login(self.admin.user)
        listing = client.get(reverse('admin_notification_events'))
        self.assertEqual(listing.status_code, 200)
        from_list = {(row['event'].code, ch['channel']): ch['active']
                     for _, rows in listing.context['groups'] for row in rows
                     for ch in row['channels'] if row['event'].code == MIXED_EVENT}
        page = client.get(reverse('admin_notification_event', args=[MIXED_EVENT]))
        self.assertEqual(page.status_code, 200)
        from_page = {(MIXED_EVENT, ch['channel']): ch['active']
                     for ch in page.context['channels']}
        return from_list, from_page

    def expected(self):
        event = NOTIFICATION_EVENTS[MIXED_EVENT]
        return {(MIXED_EVENT, ch): N.channel_is_active(event, ch) for ch in event.channels}

    def test_screens_match_channel_is_active_with_no_rows(self):
        from_list, from_page = self.screen_states()
        self.assertEqual(self.expected(),
                         {(MIXED_EVENT, 'email'): False, (MIXED_EVENT, 'whatsapp'): False})
        self.assertEqual(from_list, self.expected())
        self.assertEqual(from_page, self.expected())

    def test_screens_match_with_a_default_active_event_and_with_rows(self):
        events = {MIXED_EVENT: dataclasses.replace(NOTIFICATION_EVENTS[MIXED_EVENT],
                                                   default_state='active')}
        with mock.patch.dict(NOTIFICATION_EVENTS, events):
            from_list, from_page = self.screen_states()
            # email active by default, WhatsApp still paused by default.
            self.assertEqual(self.expected(), {(MIXED_EVENT, 'email'): True,
                                               (MIXED_EVENT, 'whatsapp'): False})
            self.assertEqual((from_list, from_page), (self.expected(), self.expected()))
            self.activate(MIXED_EVENT, 'whatsapp')
            self.activate(MIXED_EVENT, 'email', on=False)
            from_list, from_page = self.screen_states()
            self.assertEqual(self.expected(), {(MIXED_EVENT, 'email'): False,
                                               (MIXED_EVENT, 'whatsapp'): True})
            self.assertEqual((from_list, from_page), (self.expected(), self.expected()))


# ---------------------------------------------------------------------------
# Q3 — set_state's WhatsApp gate
# ---------------------------------------------------------------------------

class SwitchOnGateTests(SendBase):

    def make_row(self, status='sent', template=TEMPLATE, marker=True, age_days=0):
        row = NotificationLog.objects.create(
            recipient=self.admin, channel='whatsapp', status=status,
            message=(A.WHATSAPP_TEST_MARKER if marker else '') + 'header: x',
            template_name=template, event_code=WA_EVENT)
        if age_days:
            NotificationLog.objects.filter(pk=row.pk).update(
                created_at=timezone.now() - timedelta(days=age_days))
        return row

    def switch_on(self):
        return A.set_state(WA_EVENT, 'whatsapp', True, self.admin, 'phone test passed')

    def assertGateRefuses(self):
        with self.assertRaises(A.NotificationAdminRefused) as caught:
            self.switch_on()
        self.assertIn('test_whatsapp --event s3a_wa_test', str(caught.exception))
        self.assertFalse(NotificationEventSetting.objects.filter(event_code=WA_EVENT).exists())

    def test_refused_with_no_test(self):
        self.assertGateRefuses()

    def test_refused_with_a_failed_test(self):
        self.make_row(status='failed')
        self.assertGateRefuses()

    def test_refused_with_a_test_of_a_different_template(self):
        self.make_row(template='dc_created_old')
        self.assertGateRefuses()

    def test_refused_with_a_real_send_that_is_not_a_test(self):
        self.make_row(marker=False)
        self.assertGateRefuses()

    def test_refused_after_14_days(self):
        self.make_row(age_days=15)
        self.assertGateRefuses()

    def test_allowed_with_a_recent_sent_test(self):
        self.make_row(age_days=13)
        setting = self.switch_on()
        self.assertTrue(setting.is_active)
        self.assertTrue(N.channel_is_active(NOTIFICATION_EVENTS[WA_EVENT], 'whatsapp'))

    def test_pausing_is_never_gated(self):
        self.make_row()
        self.switch_on()
        NotificationLog.objects.all().delete()
        setting = A.set_state(WA_EVENT, 'whatsapp', False, self.admin, 'stop it')
        self.assertFalse(setting.is_active)


# ---------------------------------------------------------------------------
# W7 / W10 — test_whatsapp
# ---------------------------------------------------------------------------

class TestWhatsappCommandTests(SendBase):

    def run_command(self, *args):
        out = io.StringIO()
        call_command('test_whatsapp', *args, stdout=out)
        return out.getvalue()

    def test_unknown_event_and_event_without_whatsapp_are_refused(self):
        with self.assertRaisesMessage(CommandError, "No registry event 'nope'"):
            self.run_command('--event', 'nope', '--user', 'wa_alice')
        with self.assertRaisesMessage(CommandError, 'does not declare WhatsApp'):
            self.run_command('--event', 'admin_test_message', '--user', 'wa_alice')

    def test_the_test_only_event_is_refused_outside_the_patch(self):
        with mock.patch.dict(NOTIFICATION_EVENTS, clear=False):
            NOTIFICATION_EVENTS.pop(WA_EVENT)
            with self.assertRaisesMessage(CommandError, 'No registry event'):
                self.run_command('--event', WA_EVENT, '--user', 'wa_alice')

    def test_event_and_user_go_together(self):
        with self.assertRaisesMessage(CommandError, 'both required'):
            self.run_command('--event', WA_EVENT)
        with self.assertRaisesMessage(CommandError, 'both required'):
            self.run_command('--dry-run')

    def test_invalid_number_is_refused(self):
        self.alice.phone_number = '12345'
        self.alice.save(update_fields=['phone_number'])
        with self.assertRaisesMessage(CommandError, NO_VALID_WHATSAPP_NUMBER):
            self.run_command('--event', WA_EVENT, '--user', 'wa_alice')

    def test_dry_run_prints_values_and_payload_and_writes_nothing(self):
        output = self.run_command('--event', WA_EVENT, '--user', 'wa_alice', '--dry-run')
        self.assertIn("Header value      : 'Sharma Residence'", output)
        self.assertIn("Body value {{1}}    : 'Priya Sharma'", output)
        self.assertIn('"headerValues": ["Sharma Residence"]', output)
        self.assertIn('DRY RUN', output)
        self.assertEqual((self.posts, list(NotificationLog.objects.all())), ([], []))

    def test_master_switch_off_is_refused_before_any_row(self):
        switches = SystemSettings.get()
        switches.whatsapp_enabled = False
        switches.save()
        with self.assertRaisesMessage(CommandError, 'master switch is OFF'):
            self.run_command('--event', WA_EVENT, '--user', 'wa_alice')
        self.assertEqual((self.posts, list(NotificationLog.objects.all())), ([], []))

    def test_the_pause_is_bypassed_and_the_test_row_opens_the_gate(self):
        event = NOTIFICATION_EVENTS[WA_EVENT]
        self.assertFalse(N.channel_is_active(event, 'whatsapp'))
        output = self.run_command('--event', WA_EVENT, '--user', 'wa_alice')
        self.assertIn('HTTP     : 201', output)
        self.assertIn('RESPONSE : {"result": true, "id": "wamid-1"}', output)
        [row] = self.wa_rows()
        self.assertEqual((row.status, row.template_name, row.event_code, row.recipient_id,
                          row.interakt_message_id),
                         ('sent', TEMPLATE, WA_EVENT, self.alice.pk, 'wamid-1'))
        self.assertTrue(row.message.startswith('[TEST] header: Sharma Residence | body: '))
        [post] = self.posts
        self.assertEqual(post['json']['phoneNumber'], '9876500001')
        self.assertTrue(A.whatsapp_tested(event))
        A.set_state(WA_EVENT, 'whatsapp', True, self.admin, 'phone test passed')

    def test_a_failed_test_row_does_not_open_the_gate(self):
        self.response = _response(status=400, text='{"message": "template not approved"}')
        output = self.run_command('--event', WA_EVENT, '--user', 'wa_alice')
        self.assertIn('HTTP     : 400', output)
        self.assertEqual([r.status for r in self.wa_rows()], ['failed'])
        self.assertFalse(A.whatsapp_tested(NOTIFICATION_EVENTS[WA_EVENT]))

    @override_settings(INTERAKT_API_KEY='SECRETKEY1234567890')
    def test_legacy_mode_prints_no_part_of_the_key(self):
        _profile('chetan', 'Admin', phone_number='9876500009')
        legacy_post = mock.patch(
            'projects.management.commands.test_whatsapp._requests.post',
            return_value=_response())
        with legacy_post as posted, mock.patch(
                'projects.management.commands.test_whatsapp.time.sleep'):
            output = self.run_command()
        self.assertNotIn('SECRETKE', output)
        self.assertIn('API key           : set', output)
        self.assertEqual(posted.call_count, 7)   # otherwise unchanged: every template


# ---------------------------------------------------------------------------
# Legacy WhatsApp payloads are byte-identical
# ---------------------------------------------------------------------------

class LegacyPayloadTests(SendBase):

    def old_payload(self, phone_digits, template, params, pk):
        """The payload exactly as _send_whatsapp built it before S3a."""
        params = [str(p) for p in params]
        return {
            'countryCode': '+91',
            'phoneNumber': phone_digits,
            'callbackData': f'solarpms-{pk}',
            'type': 'Template',
            'template': {
                'name': template,
                'languageCode': 'en',
                'headerValues': params[:1],
                'bodyValues':   params[1:],
            },
        }

    def test_payload_url_and_headers_are_unchanged(self):
        params = ['Miracle Hospital', 'Subhash', 3, 'Line\nbreak kept as legacy did']
        send_notification(self.alice, 'msg', channels=['whatsapp'], template='assign_task',
                          template_params=params)
        [post] = self.posts
        self.assertEqual(post['url'], 'https://api.interakt.ai/v1/public/message/')
        self.assertEqual(post['headers'], {'Authorization': 'Basic test-key',
                                           'Content-Type': 'application/json'})
        expected = self.old_payload('9876500001', 'assign_task', params, self.alice.pk)
        self.assertEqual(json.dumps(post['json']), json.dumps(expected))
        [row] = self.wa_rows()
        self.assertEqual((row.status, row.template_name, row.message, row.event_code),
                         ('sent', 'assign_task', 'msg', ''))

    def test_legacy_keeps_its_looser_number_check_and_wording(self):
        self.alice.phone_number = '12345'
        self.alice.save(update_fields=['phone_number'])
        send_notification(self.alice, 'msg', channels=['whatsapp'], template='t',
                          template_params=['a'])
        self.assertEqual(self.posts[0]['json']['phoneNumber'], '12345')
        self.alice.phone_number = ''
        self.alice.save(update_fields=['phone_number'])
        send_notification(self.alice, 'msg', channels=['whatsapp'], template='t',
                          template_params=['a'])
        self.assertEqual(self.wa_rows()[-1].error_detail, 'Recipient has no phone number')
