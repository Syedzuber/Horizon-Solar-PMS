"""The PM gate's four notifications — in-app ONLY, after the save, to the right people
(prompt 3.1b-3).

WHY THIS FILE EXISTS
--------------------
The PM gate went live in 1f2a137 and told nobody. These are the design module's FIRST
notifications of any kind, and the safety property is CHANNEL RESTRICTION, not inertness:
SystemSettings.email_enabled is ON in production, every UserProfile's email_notifications
defaults to True, and the development .env carries live ZeptoMail and Interakt keys.

  (a) THE CHANNEL PROOF, which is the point of this module. All four transitions are driven
      with the SWITCHES ON (production's position) in the throwaway test database, and every
      path that could transmit is intercepted at FOUR layers:
        1. projects.notifications._send_email / _send_whatsapp — the per-channel senders,
           replaced outright, so an email or WhatsApp attempt is caught before any config
           check could return early and make a lower layer's silence meaningless;
        2. requests.sessions.Session.request — every requests verb funnels through it, and
           requests.post is the only transport in projects/ (the one other importer, the
           test_whatsapp command, is not reachable from a view);
        3. the ZeptoMail and Interakt keys overridden to '' for the class;
        4. NotificationLog, queried for email and WhatsApp rows and finding none.
      The interception is itself proven armed (a1b), so the zeros below are a negative
      result, not an absence of evidence. And every call site is parsed (a2): it names
      channels=['in_app'] as a literal, sits after its atomic block, inside a try.
  (b) one notification per recipient per transition, deduplicated
  (c) a raising send does NOT unwind the transition, AND the failure is logged
  (d) the recipient sets, including the Design Head chain and its logged fall-back
  (e) nothing is sent for a refused transition
"""
import ast
import os
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

import requests
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from .design_views import (
    GATE_HEAD_PASSED, GATE_HEAD_RETURNED, GATE_PM_REJECTED, GATE_SENT_BACK,
    design_gate_next_actors,
)
from .models import (
    Notification, NotificationLog, Project, StatusTransition, SystemSettings,
    DESIGN_AWAITING_HEAD_QC, DESIGN_AWAITING_PM_APPROVAL, DESIGN_IN_DESIGN, DESIGN_PM_REJECTED,
    ERR_LAYOUT, REASON_DESIGN_HEAD_PASSED, REASON_DESIGN_HEAD_RETURNED_TO_PM, REASON_DESIGN_PM_REJECTED,
    SUBJECT_DESIGN_ASSIGNMENT,
)
from .notifications import send_notification as real_send_notification
from .tests_design_pm_gate_live import LiveGateBase, _profile

VIEW_LOGGER = 'projects.design_views'


class GateNotificationBase(LiveGateBase):
    """LiveGateBase's people and route — every step a real view — plus two more Heads, a
    deputy and a Coordinator for the recipient tests."""

    def setUp(self):
        super().setUp()
        self.head2 = _profile('gn_head2', 'Design', is_design_head=True)
        self.head3 = _profile('gn_head3', 'Design', is_design_head=True)
        self.deputy = _profile('gn_dep', 'Design')
        self.head.design_head_deputy = self.deputy
        self.head.save()

    def _notes(self, **match):
        return list(Notification.objects.filter(**match).order_by('pk'))

    def _new_notes(self, since):
        """Notifications written after pk `since`, as (recipient, link) pairs."""
        return [(n.recipient, n.link) for n in self._notes(pk__gt=since)]

    def _mark(self):
        last = Notification.objects.order_by('-pk').first()
        return last.pk if last else 0

    def _at_head_qc(self, code):
        site = self._new_site(code)
        self._allocate(site)
        self._drive_package(site)
        return site

    def _rejected(self, code):
        site = self._to_pm(code)
        self._pm_reject(site, 'The layout blocks the fire path.')
        return site

    def _deactivate(self, profile):
        profile.is_active = False
        profile.save(update_fields=['is_active'])


# ===========================================================================
# (a) THE CHANNEL PROOF
# ===========================================================================

def _intercepted():
    """The four-layer interception, as context managers. Layer 3 is a class decorator."""
    return (patch('projects.notifications._send_email'),
            patch('projects.notifications._send_whatsapp'),
            patch.object(requests.sessions.Session, 'request',
                         side_effect=AssertionError('an HTTP request was attempted')))


@override_settings(ZEPTOMAIL_API_KEY='', ZEPTOMAIL_FROM_EMAIL='', INTERAKT_API_KEY='')
class ChannelProofTests(GateNotificationBase):

    def setUp(self):
        super().setUp()
        # PRODUCTION'S POSITION, in the test database only (in-memory SQLite, discarded with
        # the test): both external switches ON, and every profile at its default
        # email_notifications=True / whatsapp_notifications=True. If a call site named
        # 'email' or 'whatsapp', these are the conditions under which it would send.
        switches = SystemSettings.get()
        switches.email_enabled = True
        switches.whatsapp_enabled = True
        switches.save()
        self.assertTrue(all(p.email_notifications and p.whatsapp_notifications
                            for p in (self.pm, self.head, self.designer)))

    def test_a1_all_four_transitions_write_in_app_and_nothing_else(self):
        email, whatsapp, http = _intercepted()
        with email as sent_email, whatsapp as sent_whatsapp, http as sent_http, \
                patch('projects.design_views.send_notification',
                      wraps=real_send_notification) as spy:
            site = self._to_pm('GN-A1')                      # head pass      -> PM
            self._pm_reject(site, 'Wrong inverter make.')    # PM reject      -> Head
            self._return_to_pm(site, 'An approved equivalent.')  # Head return -> PM
            self._pm_reject(site, 'Still not accepted.')     # PM reject      -> Head
            self._send_back(site)                            # Head send-back -> designer

        # Layers 1 and 2: nothing was even attempted.
        sent_email.assert_not_called()
        sent_whatsapp.assert_not_called()
        sent_http.assert_not_called()
        # Every call named in_app, explicitly, and nothing else.
        self.assertEqual(spy.call_count, 5)
        for call in spy.call_args_list:
            self.assertEqual(call.kwargs['channels'], ['in_app'])
        # Layer 4, queried for and found empty.
        self.assertEqual(NotificationLog.objects.filter(channel='email').count(), 0)
        self.assertEqual(NotificationLog.objects.filter(channel='whatsapp').count(), 0)
        self.assertEqual(NotificationLog.objects.exclude(channel='in_app').count(), 0)
        self.assertEqual(set(NotificationLog.objects.values_list('channel', 'status')),
                         {('in_app', 'sent')})
        # One Notification per in-app log row, and exactly the five expected.
        self.assertEqual(Notification.objects.count(), 5)
        self.assertEqual(NotificationLog.objects.count(), 5)

    def test_a1b_the_interception_is_armed_so_the_zeros_mean_something(self):
        """With the same patches and switches, a send that DOES name email and WhatsApp
        reaches both mocked senders, and a raw requests.post is caught. So a1's zeros are
        the product declining to send, not the harness failing to see."""
        email, whatsapp, http = _intercepted()
        with email as sent_email, whatsapp as sent_whatsapp, http as sent_http:
            real_send_notification(self.pm, 'probe', channels=['email', 'whatsapp'],
                                   template='probe')
            with self.assertRaises(AssertionError):
                requests.post('https://example.invalid/', json={})
        sent_email.assert_called_once()
        sent_whatsapp.assert_called_once()
        sent_http.assert_called_once()

    def test_a3_no_message_carries_a_host_and_every_link_is_relative(self):
        email, whatsapp, http = _intercepted()
        with email, whatsapp, http:
            site = self._rejected('GN-A3')
            self._return_to_pm(site, 'An approved equivalent.')
            self._pm_reject(site, 'Still not accepted.')
            self._send_back(site)
        notes = self._notes()
        self.assertEqual(len(notes), 5)
        for note in notes:
            with self.subTest(recipient=note.recipient.user.username, link=note.link):
                for host_mark in ('://', 'www.', 'railway', 'horizonrenewablepower'):
                    self.assertNotIn(host_mark, note.message.lower())
                # A quoted remark closes the message, so it is never followed by more text.
                self.assertNotIn('".', note.message)
                self.assertTrue(note.link.startswith('/'))
                self.assertFalse(note.link.startswith('//'))


class CallSiteParseTests(GateNotificationBase):
    """Every send in the design module, found by PARSE: in_app named as a literal, after the
    atomic block, inside a try; the recipient helper called once per view, never in a loop."""

    INTENDED = {'design_head_qc_pass', 'design_pm_reject', 'design_head_return_to_pm',
                'design_head_send_back'}
    # Session 3.1c-ii: the change-request sends, which name in_app AND email. Pinned in
    # tests_design_change_notifications; listed here so the gate pin above still counts
    # every send in the module.
    CHANGE_REQUEST = {'design_change_request', 'design_change_request_accept',
                      'design_change_request_reject'}

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'design_views.py')
        with open(path, encoding='utf-8-sig') as fh:
            tree = ast.parse(fh.read())
        cls.calls = {'send_notification': [], 'design_gate_next_actors': []}

        def visit(node, fn, ancestors):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                fn = node.name
            if isinstance(node, ast.Call):
                name = getattr(node.func, 'id', None)
                if name in cls.calls:
                    cls.calls[name].append((fn, node, tuple(ancestors)))
            for child in ast.iter_child_nodes(node):
                visit(child, fn, ancestors + [node])

        visit(tree, '<module>', [])

    @staticmethod
    def _inside_atomic(ancestors):
        return any(isinstance(a, ast.With)
                   and any('atomic' in ast.unparse(item.context_expr) for item in a.items)
                   for a in ancestors)

    @staticmethod
    def _inside_try(ancestors):
        return any(isinstance(a, ast.Try)
                   and any(h.type is not None and ast.unparse(h.type) == 'Exception'
                           for h in a.handlers)
                   for a in ancestors)

    @staticmethod
    def _repeated(node, ancestors):
        """True if `node` is evaluated once per iteration of some loop. A call that IS a
        for-loop's iterable is evaluated once, so that position does not count."""
        chain = list(ancestors) + [node]
        for i, a in enumerate(ancestors):
            if isinstance(a, (ast.While, ast.comprehension, ast.ListComp, ast.SetComp,
                              ast.DictComp, ast.GeneratorExp)):
                return True
            if isinstance(a, ast.For) and chain[i + 1] is not a.iter:
                return True
        return False

    def test_a2_every_send_names_in_app_as_a_literal_after_the_block_inside_a_try(self):
        sends = self.calls['send_notification']
        self.assertEqual({fn for fn, _, _ in sends}, self.INTENDED | self.CHANGE_REQUEST)
        self.assertEqual(len(sends), 7, [fn for fn, _, _ in sends])
        for fn, node, ancestors in sends:
            with self.subTest(view=fn):
                channels = next((k.value for k in node.keywords if k.arg == 'channels'), None)
                self.assertIsInstance(channels, ast.List, f'{fn}: channels not a literal list')
                self.assertEqual([getattr(e, 'value', None) for e in channels.elts],
                                 ['in_app'] if fn in self.INTENDED else ['in_app', 'email'])
                self.assertFalse(self._inside_atomic(ancestors),
                                 f'{fn}: the send is inside a transaction.atomic() block')
                self.assertTrue(self._inside_try(ancestors), f'{fn}: the send is not guarded')
                if fn in self.INTENDED:
                    link = next((k.value for k in node.keywords if k.arg == 'link'), None)
                    self.assertEqual(getattr(getattr(link, 'func', None), 'id', None),
                                     'reverse')

    def test_7_the_recipient_helper_is_called_once_per_view_and_never_in_a_loop(self):
        helper_calls = self.calls['design_gate_next_actors']
        self.assertEqual(sorted(fn for fn, _, _ in helper_calls),
                         sorted(self.INTENDED | self.CHANGE_REQUEST))
        for fn, node, ancestors in helper_calls:
            with self.subTest(view=fn):
                self.assertFalse(self._repeated(node, ancestors), f'{fn}: helper in a loop')
                self.assertFalse(self._inside_atomic(ancestors))


# ===========================================================================
# (b) One notification per recipient per transition
# ===========================================================================

class DedupTests(GateNotificationBase):

    def test_b1_a_pm_who_is_also_a_coordinator_is_told_once(self):
        coordinator = _profile('gn_coord', 'Project Coordinator')
        lapsed = _profile('gn_coord_lapsed', 'Project Coordinator')
        site = self._at_head_qc('GN-B1')
        site.coordinators.add(self.pm, coordinator, lapsed)
        self._deactivate(lapsed)

        mark = self._mark()
        self._head_pass(site)
        queue = reverse('design_pm_approval_queue')
        self.assertEqual(self._new_notes(mark), [(self.pm, queue), (coordinator, queue)])

        self._pm_reject(site, 'Wrong tilt.')
        mark = self._mark()
        self._return_to_pm(site, 'The tilt is per the tender.')
        self.assertEqual(self._new_notes(mark), [(self.pm, queue), (coordinator, queue)])

    def test_b2_each_transition_writes_one_notification_per_recipient(self):
        site = self._at_head_qc('GN-B2')
        counts = []
        for step in (lambda: self._head_pass(site),
                     lambda: self._pm_reject(site, 'Wrong make.'),
                     lambda: self._return_to_pm(site, 'Equivalent.'),
                     lambda: self._pm_reject(site, 'Still wrong.'),
                     lambda: self._send_back(site)):
            mark = self._mark()
            step()
            notes = self._new_notes(mark)
            counts.append(len(notes))
            self.assertEqual(len({r.pk for r, _ in notes}), len(notes))
        self.assertEqual(counts, [1, 1, 1, 1, 1])


# ===========================================================================
# (c) A raising send does not unwind the transition — and the failure is logged
# ===========================================================================

class RaisingSendTests(GateNotificationBase):

    def _raising(self):
        return patch('projects.design_views.send_notification',
                     side_effect=RuntimeError('notification outage'))

    def _last_row(self, site):
        return (StatusTransition.objects
                .filter(subject_type=SUBJECT_DESIGN_ASSIGNMENT, subject_id=self._a(site).pk)
                .order_by('-occurred_at', '-pk').first())

    def _assert_logged(self, logs, view, site):
        self.assertEqual(len(logs.records), 1, logs.output)
        record = logs.records[0]
        self.assertEqual(record.levelname, 'ERROR')
        self.assertIn(view, record.getMessage())
        self.assertIn(site.project_id, record.getMessage())
        # logger.exception, not logger.error: the traceback is on the record.
        self.assertIsNotNone(record.exc_info)
        self.assertIsInstance(record.exc_info[1], RuntimeError)

    def test_c1_head_pass_stands(self):
        site = self._at_head_qc('GN-C1')
        with self._raising(), self.assertLogs(VIEW_LOGGER, level='ERROR') as logs:
            response = self._post(self.head, 'design_head_qc_pass', site)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._a(site).status, DESIGN_AWAITING_PM_APPROVAL)
        self.assertEqual(self._last_row(site).reason_code, REASON_DESIGN_HEAD_PASSED)
        self._assert_logged(logs, 'design_head_qc_pass', site)
        self.assertEqual(Notification.objects.count(), 0)

    def test_c2_pm_reject_stands(self):
        site = self._to_pm('GN-C2')
        before = Notification.objects.count()
        with self._raising(), self.assertLogs(VIEW_LOGGER, level='ERROR') as logs:
            response = self._post(self.pm, 'design_pm_reject', site, {'remark': 'Wrong make.'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._a(site).status, DESIGN_PM_REJECTED)
        self.assertEqual(self._last_row(site).reason_code, REASON_DESIGN_PM_REJECTED)
        self._assert_logged(logs, 'design_pm_reject', site)
        self.assertEqual(Notification.objects.count(), before)

    def test_c3_head_return_stands(self):
        site = self._rejected('GN-C3')
        before = Notification.objects.count()
        with self._raising(), self.assertLogs(VIEW_LOGGER, level='ERROR') as logs:
            response = self._post(self.head, 'design_head_return_to_pm', site,
                                  {'remark': 'Equivalent.'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._a(site).status, DESIGN_AWAITING_PM_APPROVAL)
        self.assertEqual(self._last_row(site).reason_code, REASON_DESIGN_HEAD_RETURNED_TO_PM)
        self._assert_logged(logs, 'design_head_return_to_pm', site)
        self.assertEqual(Notification.objects.count(), before)

    def test_c4_send_back_stands_with_attempt_2_open(self):
        site = self._rejected('GN-C4')
        before = Notification.objects.count()
        with self._raising(), self.assertLogs(VIEW_LOGGER, level='ERROR') as logs:
            response = self._post(self.head, 'design_head_send_back', site, {
                'error_category': ERR_LAYOUT,
                'pm_rejection_remarks': 'Move the inverters clear of the fire path.',
                'redo_scope_submitted': '1', 'redo': ['arka', 'cad', 'boq']})
        self.assertEqual(response.status_code, 302)
        a = self._a(site)
        self.assertEqual((a.status, a.current_attempt_number), (DESIGN_IN_DESIGN, 2))
        last = self._last_row(site)
        self.assertEqual((last.from_status, last.to_status), (DESIGN_PM_REJECTED, DESIGN_IN_DESIGN))
        self._assert_logged(logs, 'design_head_send_back', site)
        self.assertEqual(Notification.objects.count(), before)


# ===========================================================================
# (d) The recipient sets
# ===========================================================================

class RecipientTests(GateNotificationBase):

    def test_d1_head_pass_tells_the_sites_pm(self):
        site = self._at_head_qc('GN-D1')
        mark = self._mark()
        self._head_pass(site)
        self.assertEqual(self._new_notes(mark), [(self.pm, reverse('design_pm_approval_queue'))])

    def test_d2_pm_reject_tells_the_head_who_passed_it(self):
        site = self._to_pm('GN-D2')
        mark = self._mark()
        self._pm_reject(site, 'Wrong make.')
        self.assertEqual(self._new_notes(mark),
                         [(self.head, reverse('design_qc_review', args=['GN-D2']))])

    def test_d3_a_deputy_who_passed_it_is_told_and_the_head_is_not(self):
        site = self._at_head_qc('GN-D3')
        self._step(self.deputy, 'design_head_qc_pass', site, DESIGN_AWAITING_PM_APPROVAL)
        mark = self._mark()
        self._pm_reject(site, 'Wrong make.')
        self.assertEqual([r for r, _ in self._new_notes(mark)], [self.deputy])

    def test_d4_after_a_return_the_second_rejection_tells_whoever_returned_it(self):
        """THE CASE THAT DECIDED THE RULE. head passed it; head2 returned it. The second
        rejection answers head2 — and head_reviewed_by still names head, because the return
        writes nothing to the attempt."""
        site = self._rejected('GN-D4')
        self._step(self.head2, 'design_head_return_to_pm', site, DESIGN_AWAITING_PM_APPROVAL,
                   {'remark': 'Equivalent make.'})
        self.assertEqual(self._a(site).attempts.get(attempt_number=1).head_reviewed_by,
                         self.head, 'the premise: head_reviewed_by names the passer')
        mark = self._mark()
        self._pm_reject(site, 'Not accepted.')
        self.assertEqual([r for r, _ in self._new_notes(mark)], [self.head2])

    def test_d5_an_inactive_returner_falls_back_to_the_passer_and_says_so(self):
        site = self._rejected('GN-D5')
        self._step(self.head2, 'design_head_return_to_pm', site, DESIGN_AWAITING_PM_APPROVAL,
                   {'remark': 'Equivalent make.'})
        self._deactivate(self.head2)
        mark = self._mark()
        with self.assertLogs(VIEW_LOGGER, level='WARNING') as logs:
            self._pm_reject(site, 'Not accepted.')
        self.assertEqual([r for r, _ in self._new_notes(mark)], [self.head])
        self.assertIn('GN-D5', logs.output[0])
        self.assertIn('lg_head', logs.output[0])

    def test_d6_nobody_active_to_answer_fans_out_to_every_active_head_and_logs_it(self):
        site = self._to_pm('GN-D6')
        self._deactivate(self.head)            # the passer: ledger actor AND head_reviewed_by
        mark = self._mark()
        with self.assertLogs(VIEW_LOGGER, level='WARNING') as logs:
            self._pm_reject(site, 'Not accepted.')
        self.assertEqual([r for r, _ in self._new_notes(mark)], [self.head2, self.head3])
        fanout = [line for line in logs.output if 'ALL 2 active Design Head' in line]
        self.assertEqual(len(fanout), 1, logs.output)
        self.assertIn('GN-D6', fanout[0])
        self.assertIn('gn_head2, gn_head3', fanout[0])

    def test_d7_head_return_tells_the_sites_pm(self):
        site = self._rejected('GN-D7')
        mark = self._mark()
        self._return_to_pm(site, 'Equivalent make.')
        self.assertEqual(self._new_notes(mark), [(self.pm, reverse('design_pm_approval_queue'))])

    def test_d8_send_back_tells_the_designer_after_the_next_attempt_opens(self):
        site = self._rejected('GN-D8')
        mark = self._mark()
        self._send_back(site)
        a = self._a(site)
        self.assertEqual(a.current_attempt_number, 2)
        self.assertEqual(a.assigned_to, self.designer)
        self.assertEqual(self._new_notes(mark),
                         [(self.designer, reverse('design_site_workspace', args=['GN-D8']))])
        note = self._notes(pk__gt=mark)[0]
        self.assertIn('attempt 2 is open', note.message)
        # The Head's words close the message, so their own full stop is the last character.
        self.assertTrue(note.message.endswith('"Move the inverters clear of the fire path."'))

    def test_d9_an_unknown_transition_is_refused(self):
        site = self._new_site('GN-D9')
        with self.assertRaises(ValueError):
            design_gate_next_actors(self._a(site), 'released')

    def test_7_query_cost_per_transition(self):
        """The helper's own cost, measured on an assignment fetched the way the views fetch
        it (through the project, so `assignment.project` is already cached)."""
        site = self._rejected('GN-Q')

        def cost(transition):
            assignment = Project.objects.get(pk=site.pk).design_assignment
            with CaptureQueriesContext(connection) as ctx:
                design_gate_next_actors(assignment, transition)
            return len(ctx.captured_queries)

        self.assertEqual({t: cost(t) for t in (GATE_HEAD_PASSED, GATE_PM_REJECTED,
                                               GATE_HEAD_RETURNED, GATE_SENT_BACK)},
                         {GATE_HEAD_PASSED: 2, GATE_PM_REJECTED: 1,
                          GATE_HEAD_RETURNED: 2, GATE_SENT_BACK: 1})


# ===========================================================================
# (e) Nothing is sent for a refused transition
# ===========================================================================

class RefusalTests(GateNotificationBase):

    def _assert_silent(self, profile, name, site, data=None, method='post'):
        notes, logs = Notification.objects.count(), NotificationLog.objects.count()
        status = self._a(site).status
        self._as(profile)
        url = reverse(name, kwargs={'project_id': site.project_id})
        response = (self.client.post(url, data or {}) if method == 'post'
                    else self.client.get(url))
        self.assertIn(response.status_code, (302, 403))
        self.assertEqual(self._a(site).status, status, f'{name} moved the site')
        self.assertEqual(Notification.objects.count(), notes, f'{name} notified')
        self.assertEqual(NotificationLog.objects.count(), logs, f'{name} logged a send')
        return response

    def test_e1_blank_remarks(self):
        with_pm = self._to_pm('GN-E1A')
        self._assert_silent(self.pm, 'design_pm_reject', with_pm, {'remark': '   '})
        rejected = self._rejected('GN-E1B')
        self._assert_silent(self.head, 'design_head_return_to_pm', rejected, {'remark': ''})
        self._assert_silent(self.head, 'design_head_send_back', rejected, {
            'error_category': ERR_LAYOUT, 'pm_rejection_remarks': ' ',
            'redo_scope_submitted': '1', 'redo': ['arka']})
        self._assert_silent(self.head, 'design_head_send_back', rejected, {
            'error_category': '', 'pm_rejection_remarks': 'Fix it.',
            'redo_scope_submitted': '1', 'redo': ['arka']})

    def test_e2_wrong_status(self):
        with_pm = self._to_pm('GN-E2A')
        self._assert_silent(self.head, 'design_head_qc_pass', with_pm)
        self._assert_silent(self.head, 'design_head_return_to_pm', with_pm, {'remark': 'x'})
        self._assert_silent(self.head, 'design_head_send_back', with_pm, {
            'error_category': ERR_LAYOUT, 'pm_rejection_remarks': 'x',
            'redo_scope_submitted': '1', 'redo': ['arka']})
        at_head = self._at_head_qc('GN-E2B')
        self.assertEqual(self._a(at_head).status, DESIGN_AWAITING_HEAD_QC)
        self._assert_silent(self.pm, 'design_pm_reject', at_head, {'remark': 'x'})

    def test_e3_unauthorised_users(self):
        with_pm = self._to_pm('GN-E3A')
        for outsider in (self.designer, self.head, self.scm):
            response = self._assert_silent(outsider, 'design_pm_reject', with_pm,
                                           {'remark': 'x'})
            self.assertEqual(response.status_code, 403)
        rejected = self._rejected('GN-E3B')
        for outsider in (self.pm, self.designer):
            self.assertEqual(self._assert_silent(outsider, 'design_head_return_to_pm',
                                                 rejected, {'remark': 'x'}).status_code, 403)
        at_head = self._at_head_qc('GN-E3C')
        self.assertEqual(self._assert_silent(self.pm, 'design_head_qc_pass',
                                             at_head).status_code, 403)

    def test_e4_get_is_not_a_transition(self):
        at_head = self._at_head_qc('GN-E4')
        self._assert_silent(self.head, 'design_head_qc_pass', at_head, method='get')

    def test_e5_a_send_back_refused_inside_the_block_sends_nothing(self):
        """The open-extension refusal returns from INSIDE the atomic block, so the send
        after the block is never reached."""
        rejected = self._rejected('GN-E5')
        with patch('projects.design_views._pending_extension',
                   return_value=SimpleNamespace(proposed_date=date(2026, 12, 1))):
            self._assert_silent(self.head, 'design_head_send_back', rejected, {
                'error_category': ERR_LAYOUT, 'pm_rejection_remarks': 'Fix it.',
                'redo_scope_submitted': '1', 'redo': ['arka']})
