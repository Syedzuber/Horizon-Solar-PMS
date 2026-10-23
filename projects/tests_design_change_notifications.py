"""
Session 3.1c-ii — notifications for design change requests, in-app AND email.

WHAT IS PINNED, AND THE DECISION EACH TEST CITES
------------------------------------------------
  N-a  A raise reaches the site's PM, its active coordinators and every active Design Head.
  N-b  An accept reaches the requester, the PM, the coordinators and the designer; a reject
       reaches the requester alone, with the Head's reason.
  N-c  The actor is never told of their own act, and nobody is told twice.
  N-d  In-app links are relative. The email link is absolute; CHANGED AT T3b (C3): its
       host is settings.APP_BASE_URL, no longer the request's own.
  N-e  A send that raises does not unwind the request, and is logged with its traceback.
  D1   One call per recipient. The bell text carries no URL. The HTML part escapes the
       user's text; the plain text carries it as typed. CHANGED AT T3b: the views call
       notify() (in-app always, email per the change_request_* events), and the email's
       plain-text part now carries the link once, on the button's line (ruling Q5).
  T3b  Sends run after commit; ChangeNotificationBase runs each POST's on-commit callbacks.
  D4   A raise does not reach a Design Head's deputy.
  D5   Anyone whose UserProfile OR auth.User is inactive is dropped.
  D6   An accept that took the site out of a draft procurement group tells that group's
       SCM owner: whoever added the site, else whoever created the group.
  D7   The raiser is labelled PM, Project Coordinator or SCM; "requester" is unreachable
       through the view.
  D8   The requester reads "your design change request" in an accept.

INTERCEPTION, AT TWO LEVELS. `projects.notifications._zeptomail_post` — the last function
before the ZeptoMail HTTP call — is replaced by a recorder, so every email that WOULD have
been sent is captured with its subject, plain text and HTML. Beneath it,
`requests.sessions.Session.request` raises, so any real HTTP request fails the test
loudly; every test checks it was never called. Under solarpms.test_settings both API keys
are also empty (B0), which EmptyKeyTests proves stops the real sender before HTTP.

The email switch is set to production's position, ON, in the throwaway test database; the
Interakt switch keeps its model default, off. Every fixture user is given an example.com
address.
"""
import ast
import os
from unittest.mock import patch

import requests
from django.conf import settings
from django.urls import reverse

from . import notifications
from .design_views import _change_request_raiser_label
from .models import (
    Notification, NotificationLog, SiteGroupMembership, SystemSettings,
    CHANGE_REQUEST_ACCEPTED, CHANGE_REQUEST_PENDING, CHANGE_REQUEST_REJECTED,
    DESIGN_IN_DESIGN,
)
from .notify_registry import NOTIFICATION_EVENTS, get_event
from .tests_design_change_window import ChangeWindowBase, _profile

VIEW_LOGGER = 'projects.design_views'
TEMPLATES = ('design_change_request_raised', 'design_change_request_accepted',
             'design_change_request_rejected')
REAL_ZEPTOMAIL_POST = notifications._zeptomail_post


class ChangeNotificationBase(ChangeWindowBase):

    def setUp(self):
        super().setUp()
        for profile in (self.head, self.qc, self.designer, self.pm, self.coord, self.scm,
                        self.finance):
            self._give_email(profile)
        switches = SystemSettings.get()
        switches.email_enabled = True
        switches.save()

        # Every request as Railway's proxy sends it: the https marker, and a Host header.
        # Without the header Django builds the host from SERVER_NAME and SERVER_PORT, and
        # port 80 under https renders as "testserver:80", which no proxied request carries.
        self.client.defaults['HTTP_X_FORWARDED_PROTO'] = 'https'
        self.client.defaults['HTTP_HOST'] = 'testserver'

        self.mail = []

        def record(to_email, to_name, subject, text_body, html_body=None):
            self.mail.append({'to': to_email, 'subject': subject, 'text': text_body,
                              'html': html_body})
            return True, ''

        mail_patch = patch('projects.notifications._zeptomail_post', side_effect=record)
        http_patch = patch.object(requests.sessions.Session, 'request',
                                  side_effect=AssertionError('an HTTP request was attempted'))
        mail_patch.start()
        self.http = http_patch.start()
        self.addCleanup(mail_patch.stop)
        self.addCleanup(http_patch.stop)
        self.addCleanup(lambda: self.http.assert_not_called())

        # T3b: the change-request notifications go through notify(), after commit. Every
        # POST runs its on-commit callbacks, so a test reads what that request sent, as it
        # did when the views sent at once.
        post = self.client.post

        def post_and_commit(*args, **kwargs):
            with self.captureOnCommitCallbacks(execute=True):
                return post(*args, **kwargs)
        self.client.post = post_and_commit

    # ── helpers ─────────────────────────────────────────────────────────────

    @staticmethod
    def _give_email(profile):
        profile.user.email = f'{profile.user.username}@example.com'
        profile.user.save(update_fields=['email'])

    def _person(self, username, role, **flags):
        profile = _profile(username, role, **flags)
        self._give_email(profile)
        return profile

    @staticmethod
    def _mark():
        last = Notification.objects.order_by('-pk').first()
        return last.pk if last else 0

    @staticmethod
    def _notes(since):
        return list(Notification.objects.filter(pk__gt=since)
                    .select_related('recipient__user').order_by('pk'))

    def _told(self, since):
        """Usernames with an in-app notification after `since`, one entry per note."""
        return sorted(n.recipient.user.username for n in self._notes(since))

    def _mailed(self, start=0):
        return sorted(m['to'].split('@')[0] for m in self.mail[start:])

    def _note_for(self, since, profile):
        notes = [n for n in self._notes(since) if n.recipient_id == profile.pk]
        self.assertEqual(len(notes), 1, f'{profile.user.username}: {len(notes)} notes')
        return notes[0]

    def _mail_for(self, profile, start=0):
        mails = [m for m in self.mail[start:] if m['to'] == profile.user.email]
        self.assertEqual(len(mails), 1, f'{profile.user.username}: {len(mails)} emails')
        return mails[0]

    def _pending(self, assignment):
        return self._requests(assignment).get(verdict=CHANGE_REQUEST_PENDING)

    def _form(self, site):
        return reverse('design_change_request_form', kwargs={'project_id': site.project_id})


# ===========================================================================
# N-a, N-c, D4, D5 — who hears about a raise
# ===========================================================================

class RaiseRecipientTests(ChangeNotificationBase):

    def test_a1_raise_by_pm_tells_the_heads_and_coordinators_and_not_the_pm(self):
        head2 = self._person('cn_head2', 'Design', is_design_head=True)
        site, assignment = self._released('CN-A1')
        mark = self._mark()
        self._raise(self.pm, site)
        self.assertEqual(self._told(mark), ['cn_head2', 'cw_coord', 'cw_head'])
        self.assertEqual(self._mailed(), ['cn_head2', 'cw_coord', 'cw_head'])
        self.assertIn('(PM)', self._note_for(mark, head2).message)

    def test_a2_raise_by_scm_tells_the_pm_and_coordinators_and_not_scm_or_the_heads(self):
        # Session B2b (D-12): a request with the PM is not the Heads' until it is forwarded.
        site, assignment = self._released('CN-A2')
        mark = self._mark()
        self._raise(self.scm, site)
        self.assertEqual(self._told(mark), ['cw_coord', 'cw_pm'])
        self.assertEqual(self._mailed(), ['cw_coord', 'cw_pm'])
        self.assertIn('(SCM)', self._note_for(mark, self.pm).message)

    def test_a3_a_pm_who_is_also_a_design_head_is_told_once(self):
        # Session B2b: raised by the COORDINATOR, so the PM is reached twice over — as the
        # site's PM and as a Design Head — and the dedup is still what is asserted. An SCM
        # raise no longer reaches the Heads, so it could not test this any more.
        self.pm.is_design_head = True
        self.pm.save(update_fields=['is_design_head'])
        site, assignment = self._released('CN-A3')
        mark = self._mark()
        self._raise(self.coord, site)
        self.assertEqual(self._told(mark), ['cw_head', 'cw_pm'])
        self.assertEqual(self._mailed(), ['cw_head', 'cw_pm'])

    def test_a4_an_inactive_coordinator_is_told_nothing(self):
        site, assignment = self._released('CN-A4')
        self.coord.is_active = False
        self.coord.save(update_fields=['is_active'])
        mark = self._mark()
        self._raise(self.pm, site)
        self.assertEqual(self._told(mark), ['cw_head'])
        self.assertEqual(self._mailed(), ['cw_head'])

    def test_a5_an_inactive_auth_user_with_an_active_profile_is_told_nothing(self):
        head2 = self._person('cn_head2', 'Design', is_design_head=True)
        site, assignment = self._released('CN-A5')
        for profile in (self.coord, head2):
            profile.user.is_active = False
            profile.user.save(update_fields=['is_active'])
            self.assertTrue(profile.is_active, 'the premise: the profile is still active')
        mark = self._mark()
        self._raise(self.pm, site)
        self.assertEqual(self._told(mark), ['cw_head'])
        self.assertEqual(self._mailed(), ['cw_head'])

    def test_a6_a_raise_does_not_reach_a_design_heads_deputy(self):
        deputy = self._person('cn_dep', 'Design')
        self.head.design_head_deputy = deputy
        self.head.save(update_fields=['design_head_deputy'])
        site, assignment = self._released('CN-A6')
        mark = self._mark()
        self._raise(self.pm, site)
        self.assertNotIn('cn_dep', self._told(mark))
        self.assertNotIn('cn_dep', self._mailed())

    def test_a7_a_coordinator_raising_is_labelled_project_coordinator(self):
        site, assignment = self._released('CN-A7')
        mark = self._mark()
        self._raise(self.coord, site)
        self.assertEqual(self._told(mark), ['cw_head', 'cw_pm'])
        self.assertIn('(Project Coordinator)', self._note_for(mark, self.head).message)


# ===========================================================================
# N-d, D1 — channels, links, the host, escaping
# ===========================================================================

class ChannelAndLinkTests(ChangeNotificationBase):

    def test_d1_both_channels_relative_in_app_link_absolute_https_email_link(self):
        site, assignment = self._released('CN-D1')
        mark = self._mark()
        self._raise(self.pm, site)
        qc_review = reverse('design_qc_review', kwargs={'project_id': site.project_id})
        expected = {self.head: qc_review, self.coord: self._form(site)}
        for profile, link in expected.items():
            with self.subTest(recipient=profile.user.username):
                note = self._note_for(mark, profile)
                self.assertEqual(note.link, link)
                self.assertTrue(note.link.startswith('/') and not note.link.startswith('//'))
                self.assertEqual(
                    sorted(NotificationLog.objects
                           .filter(recipient=profile, template_name=TEMPLATES[0])
                           .values_list('channel', 'status')),
                    [('email', 'sent'), ('in_app', 'sent')])
                html = self._mail_for(profile)['html']
                # CHANGED AT T3b (C3): was https://testserver, the request's own host.
                self.assertIn(f'href="{settings.APP_BASE_URL}{link}"', html)
                # One link, one scheme separator: no other absolute URL, no http://.
                self.assertEqual(html.count('href="'), 1)
                self.assertEqual(html.count('://'), 1)

    def test_d2_the_email_host_is_app_base_url_whatever_the_requests_host(self):
        """CHANGED AT T3b (C3). Was test_d2_the_email_host_is_the_requests_own: the link
        carried the actor's host. notify() builds it from settings.APP_BASE_URL, so a raise
        from another host carries the same link."""
        site, assignment = self._released('CN-D2')
        self.client.defaults['HTTP_HOST'] = 'localhost'
        self._raise(self.pm, site)
        link = reverse('design_qc_review', kwargs={'project_id': site.project_id})
        html = self._mail_for(self.head)['html']
        self.assertIn(f'href="{settings.APP_BASE_URL}{link}"', html)
        self.assertNotIn('localhost', html)

    def test_d3_markup_in_a_reason_is_escaped_in_html_and_as_typed_in_plain_text(self):
        reason = '<script>alert("x")</script> & move the inverter'
        site, assignment = self._released('CN-D3')
        mark = self._mark()
        self._raise(self.pm, site, reason=reason)
        mail = self._mail_for(self.head)
        self.assertNotIn('<script>', mail['html'])
        self.assertIn('&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt; &amp; move the '
                      'inverter', mail['html'])
        # CHANGED AT T3b (ruling Q5): the text part ended with the quote inline,
        # `Reason: "…"`; the quote is now its own paragraph under "Reason:".
        self.assertIn(f'\n\nReason:\n{reason}\n\n', mail['text'])
        self.assertTrue(self._note_for(mark, self.head).message.endswith(f'Reason: "{reason}"'))

    def test_d4_no_message_carries_a_url_for_any_event(self):
        pool, pool_a = self._released('CN-D4A')
        other, other_a = self._released('CN-D4B')
        mark = self._mark()
        self._raise(self.scm, pool)
        self._forward(self._requests(pool_a).get())   # B2b: forwarding now notifies too
        self._accept(self._pending(pool_a))
        self._raise(self.pm, other)
        self._reject(self._pending(other_a), reason='See https://example.com — no.')
        notes = self._notes(mark)
        # All five events happened, and nothing else notified.
        self.assertEqual(set(NotificationLog.objects.values_list('template_name', flat=True)),
                         set(TEMPLATES) | {'design_change_request_raised_with_pm',
                                           'design_change_request_forwarded'})
        self.assertTrue(notes)
        for note in notes:
            with self.subTest(recipient=note.recipient.user.username, link=note.link):
                # The Head's reason on the rejection quotes a URL a user typed; that is
                # user text, so only the words before the quote are checked there.
                wording = note.message.split(' Reason: "')[0].split(' Request: "')[0]
                self.assertNotIn('http', wording.lower())
        for mail in self.mail:
            # CHANGED AT T3b (ruling Q5): the email's text part now carries the link, once,
            # on the button's line, as every notify() email does. Its sentences, the first
            # paragraph, still carry none; the quote (user text) follows them.
            wording = mail['text'].split('\n\n')[0]
            self.assertNotIn('http', wording.lower())
            self.assertEqual(mail['text'].count('Open in Horizon Solar PMS: '), 1)
            self.assertIn(f'Open in Horizon Solar PMS: {settings.APP_BASE_URL}/',
                          mail['text'])

    def test_d5_with_the_email_switch_off_in_app_is_delivered_and_email_is_skipped(self):
        switches = SystemSettings.get()
        switches.email_enabled = False
        switches.save()
        site, assignment = self._released('CN-D5')
        mark = self._mark()
        self._raise(self.pm, site)
        self.assertEqual(self._told(mark), ['cw_coord', 'cw_head'])
        self.assertEqual(self.mail, [])
        self.assertEqual(
            sorted(NotificationLog.objects.filter(template_name=TEMPLATES[0], channel='email')
                   .values_list('status', 'error_detail')),
            [('skipped', 'Master switch off')] * 2)


# ===========================================================================
# N-b, D6, D8 — accept and reject
# ===========================================================================

class TriageRecipientTests(ChangeNotificationBase):

    def test_v1_accept_tells_requester_pm_coordinators_and_designer_and_not_the_head(self):
        site, assignment = self._released('CN-V1')
        self._raise(self.scm, site)
        self._forward(self._requests(assignment).get())   # B2a: the PM sends it on first
        change = self._pending(assignment)
        mark, sent = self._mark(), len(self.mail)
        self._accept(change)
        self.assertEqual(self._requests(assignment).get().verdict, CHANGE_REQUEST_ACCEPTED)
        self.assertEqual(self._told(mark), ['cw_coord', 'cw_des', 'cw_pm', 'cw_scm'])
        self.assertEqual(self._mailed(sent), ['cw_coord', 'cw_des', 'cw_pm', 'cw_scm'])

        # B3b (D-18): an SCM-origin request is a BOQ change request — was 'accepted your
        # design change request' / 'accepted the design change request cw_scm raised'.
        self.assertIn('accepted your BOQ change request',
                      self._note_for(mark, self.scm).message)
        self.assertIn('accepted the BOQ change request cw_scm raised',
                      self._note_for(mark, self.pm).message)
        self.assertIn('(Design Head)', self._note_for(mark, self.pm).message)
        self.assertEqual(self._note_for(mark, self.designer).link,
                         reverse('design_site_workspace', kwargs={'project_id': 'CN-V1'}))
        self.assertEqual(self._note_for(mark, self.scm).link, self._form(site))
        self.assertIn('attempt 2 opened', self._mail_for(self.pm, sent)['subject'])
        self.assertTrue(self._note_for(mark, self.pm).message.endswith(
            f'Request: "{change.reason}"'))

    def test_v2_a_requester_who_is_the_pm_is_told_once_and_reads_your(self):
        site, assignment = self._released('CN-V2')
        self._raise(self.pm, site)
        mark, sent = self._mark(), len(self.mail)
        self._accept(self._pending(assignment))
        self.assertEqual(self._told(mark), ['cw_coord', 'cw_des', 'cw_pm'])
        self.assertEqual(self._mailed(sent), ['cw_coord', 'cw_des', 'cw_pm'])
        self.assertIn('your design change request', self._note_for(mark, self.pm).message)

    def test_v3_reject_tells_the_requester_alone_with_the_reason(self):
        site, assignment = self._released('CN-V3')
        self._raise(self.pm, site)
        mark, sent = self._mark(), len(self.mail)
        reason = 'The tender fixes the inverter make.'
        self._reject(self._pending(assignment), reason=reason)
        self.assertEqual(self._requests(assignment).get().verdict, CHANGE_REQUEST_REJECTED)
        self.assertEqual(self._told(mark), ['cw_pm'])
        self.assertEqual(self._mailed(sent), ['cw_pm'])
        note = self._note_for(mark, self.pm)
        self.assertIn('rejected your design change request on attempt 1', note.message)
        self.assertTrue(note.message.endswith(f'Reason: "{reason}"'))
        mail = self._mail_for(self.pm, sent)
        self.assertEqual(mail['subject'], 'CN-V3: design change request rejected')
        self.assertIn('The tender fixes the inverter make.', mail['html'])

    def test_v4_a_deputy_deciding_is_labelled_the_deputy(self):
        deputy = self._person('cn_dep', 'Design')
        self.head.design_head_deputy = deputy
        self.head.save(update_fields=['design_head_deputy'])
        site, assignment = self._released('CN-V4')
        self._raise(self.pm, site)
        mark = self._mark()
        self._login(deputy)
        self.client.post(reverse('design_change_request_reject',
                                 kwargs={'pk': self._pending(assignment).pk}),
                         {'rejection_reason': 'Stands.'})
        self.client.logout()
        self.assertIn("(Design Head's deputy)", self._note_for(mark, self.pm).message)

    def test_v5_accept_of_a_draft_group_site_tells_the_scm_who_added_it(self):
        grouped, grouped_a = self._released('CN-V5G')
        pool, pool_a = self._released('CN-V5P')
        self._group('Batch N', [grouped])

        self._raise(self.pm, grouped)
        mark = self._mark()
        self._accept(self._pending(grouped_a))
        self.assertEqual(self._told(mark), ['cw_coord', 'cw_des', 'cw_pm', 'cw_scm'])
        self.assertIn('The site left procurement group "Batch N".',
                      self._note_for(mark, self.scm).message)
        self.assertIn('the design change request cw_pm raised',
                      self._note_for(mark, self.scm).message)

        # A pool site's acceptance tells no SCM user.
        self._raise(self.pm, pool)
        mark = self._mark()
        self._accept(self._pending(pool_a))
        self.assertEqual(self._told(mark), ['cw_coord', 'cw_des', 'cw_pm'])

    def test_v6_an_inactive_adder_falls_back_to_the_groups_creator(self):
        grouped, grouped_a = self._released('CN-V6')
        group = self._group('Batch F', [grouped])
        lapsed = self._person('cn_scm_lapsed', 'SCM')
        lapsed.is_active = False
        lapsed.save(update_fields=['is_active'])
        SiteGroupMembership.objects.filter(group=group).update(added_by=lapsed)
        self.assertEqual(group.created_by, self.scm)

        self._raise(self.pm, grouped)
        mark = self._mark()
        self._accept(self._pending(grouped_a))
        self.assertEqual(self._told(mark), ['cw_coord', 'cw_des', 'cw_pm', 'cw_scm'])


# ===========================================================================
# N-e — a raising send unwinds nothing
# ===========================================================================

class RaisingSendTests(ChangeNotificationBase):

    def _raising(self):
        # CHANGED AT T3b: the views send through notify(), not send_notification().
        return patch('projects.design_views.notify',
                     side_effect=RuntimeError('notification outage'))

    def _assert_logged(self, logs, view, site):
        self.assertEqual(len(logs.records), 1, logs.output)
        record = logs.records[0]
        self.assertIn(view, record.getMessage())
        self.assertIn(site.project_id, record.getMessage())
        self.assertIsInstance(record.exc_info[1], RuntimeError)

    def test_e1_raise_accept_and_reject_all_stand(self):
        accepted, accepted_a = self._released('CN-E1A')
        rejected, rejected_a = self._released('CN-E1R')

        self._login(self.pm)
        with self._raising(), self.assertLogs(VIEW_LOGGER, level='ERROR') as logs:
            response = self.client.post(
                reverse('design_change_request', kwargs={'project_id': 'CN-E1A'}),
                {'reason': 'Move the array.'})
        self.client.logout()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._requests(accepted_a).get().verdict, CHANGE_REQUEST_PENDING)
        self._assert_logged(logs, 'design_change_request', accepted)

        self._login(self.head)
        with self._raising(), self.assertLogs(VIEW_LOGGER, level='ERROR') as logs:
            response = self.client.post(reverse(
                'design_change_request_accept', kwargs={'pk': self._pending(accepted_a).pk}))
        self.assertEqual(response.status_code, 302)
        accepted_a.refresh_from_db()
        self.assertEqual((accepted_a.status, accepted_a.current_attempt_number),
                         (DESIGN_IN_DESIGN, 2))
        self.assertEqual(self._requests(accepted_a).get().verdict, CHANGE_REQUEST_ACCEPTED)
        self._assert_logged(logs, 'design_change_request_accept', accepted)
        self.client.logout()

        self._raise(self.pm, rejected)
        self._login(self.head)
        with self._raising(), self.assertLogs(VIEW_LOGGER, level='ERROR') as logs:
            response = self.client.post(
                reverse('design_change_request_reject',
                        kwargs={'pk': self._pending(rejected_a).pk}),
                {'rejection_reason': 'Stands.'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._requests(rejected_a).get().verdict, CHANGE_REQUEST_REJECTED)
        self._assert_logged(logs, 'design_change_request_reject', rejected)


# ===========================================================================
# D7 — the "requester" label is unreachable through the view
# ===========================================================================

class RaiserLabelTests(ChangeNotificationBase):

    def test_l1_an_admin_cannot_raise_so_requester_is_never_rendered_by_a_raise(self):
        admin = self._person('cn_admin', 'Admin')
        site, assignment = self._released('CN-L1')
        self.assertEqual(self._raise(admin, site).status_code, 403)
        self.assertFalse(self._requests(assignment).exists())
        # The fall-through itself, called directly.
        self.assertEqual(_change_request_raiser_label(admin.user, site), 'requester')
        self.assertEqual(_change_request_raiser_label(self.pm.user, site), 'PM')
        self.assertEqual(_change_request_raiser_label(self.coord.user, site),
                         'Project Coordinator')
        self.assertEqual(_change_request_raiser_label(self.scm.user, site), 'SCM')


# ===========================================================================
# B0 and the channel pin at every call site
# ===========================================================================

class EmptyKeyTests(ChangeNotificationBase):

    def test_k1_the_real_sender_stops_before_http_with_the_test_settings_keys(self):
        self.assertEqual(settings.ZEPTOMAIL_API_KEY, '')
        self.assertEqual(settings.INTERAKT_API_KEY, '')
        self.assertEqual(REAL_ZEPTOMAIL_POST('a@example.com', 'A', 'subject', 'text'),
                         (False, 'ZEPTOMAIL_API_KEY or ZEPTOMAIL_FROM_EMAIL not configured'))
        self.http.assert_not_called()


class CallSitePinTests(ChangeNotificationBase):
    """By parse: the three change-request views name ['in_app', 'email'], the four gate
    views still name ['in_app'], and no send in the design module names any other channel."""

    GATE = {'design_head_qc_pass', 'design_pm_reject', 'design_head_return_to_pm',
            'design_head_send_back'}
    CHANGE_REQUEST = {'design_change_request', 'design_change_request_accept',
                      'design_change_request_reject',
                      # Session B2b (D-12) — the four SCM-route views.
                      'design_change_request_forward', 'design_change_request_pm_reject',
                      'design_change_request_withdraw', 'design_change_request_correct'}

    def test_p1_channels_at_every_send(self):
        """CHANGED AT T3b. send_notification() is now called by the four gate views only,
        naming ['in_app']; the seven change-request views call notify(), whose events
        declare the email channel only (in-app is always sent), and nothing else."""
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'design_views.py')
        with open(path, encoding='utf-8-sig') as fh:
            tree = ast.parse(fh.read())
        sites, notify_sites = [], set()
        for fn in (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)):
            for call in (n for n in ast.walk(fn) if isinstance(n, ast.Call)):
                if getattr(call.func, 'id', None) == 'send_notification':
                    channels = next(k.value for k in call.keywords if k.arg == 'channels')
                    sites.append((fn.name, [e.value for e in channels.elts]))
                elif getattr(call.func, 'id', None) == 'notify':
                    notify_sites.add(fn.name)
        self.assertEqual(sorted(sites), sorted((fn, ['in_app']) for fn in self.GATE))
        self.assertEqual(notify_sites, self.CHANGE_REQUEST)
        self.assertTrue(all(get_event(code).channels == ('email',)
                            for code in NOTIFICATION_EVENTS if code.startswith('change_request_')))
