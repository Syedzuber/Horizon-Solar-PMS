"""
Session B2b — the SCM change-request route's screens and notifications.

WHAT IS PINNED, AND THE DECISION EACH TEST CITES
------------------------------------------------
  B1   The "corrected" button on the QC review banner renders for Head authority and NOT
       for a QC reviewer without it, with its mandatory note and the evidence count.
  B2   The PM queue's new section (D-10): with_pm requests on the viewer's triage sites,
       oldest first, age from requested_at (D-11); a Finance user gets a 200 with no rows
       (no role gate, by design); the renamed nav entry shows for PM and coordinator only.
  B3   The Head's counter (D-11): from pm_decided_at for a forwarded request, from
       requested_at for a PM-raised one, in the queue, the attention band and the
       timestamp under the badge.
  B4   Pending at: "PM (change request)" for a with_pm request; a pending one unchanged.
  B6   Every D-12 event notifies exactly its audience, actor excluded, nobody twice; the
       corrected message names the item code and both quantities; a send that raises
       leaves the transition intact; the four gate notifications are unchanged.
  A7   scm_change_request_triagers(): an inactive assigned PM is nobody, so the raise
       takes the Q5 path.

Mail is intercepted by ChangeNotificationBase (tests_design_change_notifications): the
ZeptoMail sender is a recorder and any real HTTP request fails the test.
"""
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import requests
from django.urls import reverse
from django.utils import dateformat, timezone

from .design_metrics import tender_metrics
from .design_views import NO_PM_FALLBACK_TEXT, CHANGE_REQUEST_NO_PM_ACTION_CODE, \
    _correction_summary
from .models import (
    ActivityLog, DesignChangeRequest, Notification, NotificationLog, SystemSettings,
    CHANGE_REQUEST_CORRECTED, CHANGE_REQUEST_PENDING, CHANGE_REQUEST_PM_REJECTED,
    CHANGE_REQUEST_WITH_PM, CHANGE_REQUEST_WITHDRAWN,
    PENDING_AT_CHANGE_REQUEST, PENDING_AT_CHANGE_REQUEST_WITH_PM, design_pending_at,
)
from .notifications import send_notification as real_send_notification
from .permissions import scm_change_request_triagers, user_can_triage_scm_change_request
from .tests_change_request_routing import RoutingBase
from .tests_design_change_notifications import ChangeNotificationBase
from .tests_design_gate_notifications import GateNotificationBase

VIEW_LOGGER = 'projects.design_views'
NAV_MARK = 'title="Design &amp; BOQ approvals"'


class ScreensBase(ChangeNotificationBase, RoutingBase):
    """ChangeNotificationBase's interception and people, plus RoutingBase's withdraw,
    corrected and BOQ-correction helpers."""

    def _get(self, profile, name, **kwargs):
        self._login(profile)
        response = self.client.get(reverse(name, kwargs=kwargs))
        self.client.logout()
        return response

    def _with_pm(self, code, **kwargs):
        site, assignment = self._released(code, **kwargs)
        self._raise(self.scm, site)
        change = self._requests(assignment).get()
        self.assertEqual(change.verdict, CHANGE_REQUEST_WITH_PM)
        return site, assignment, change

    def _pending_scm(self, code):
        site, assignment, change = self._with_pm(code)
        self._forward(change)
        change.refresh_from_db()
        self.assertEqual(change.verdict, CHANGE_REQUEST_PENDING)
        return site, assignment, change

    def _backdate(self, change, **fields):
        DesignChangeRequest.objects.filter(pk=change.pk).update(**fields)
        change.refresh_from_db()

    def _queue(self, profile):
        return self._get(profile, 'design_pm_approval_queue')


# ===========================================================================
# B1 — the corrected button on the QC review banner
# ===========================================================================

class CorrectedButtonTests(ScreensBase):

    def _banner(self, profile, site):
        return self._get(profile, 'design_qc_review',
                         project_id=site.project_id).content.decode()

    def test_b1_the_button_renders_for_the_head_with_its_note(self):
        site, assignment = self._released('CS-B1')
        self._raise(self.pm, site)
        change = self._pending(assignment)
        page = self._banner(self.head, site)
        self.assertIn(reverse('design_change_request_correct', kwargs={'pk': change.pk}),
                      page)
        self.assertIn('name="correction_note" required', page)
        self.assertIn('No BOQ correction has been made since this request was raised.', page)
        self.assertNotIn('SESSION B2b', page)   # the {% comment %} block does not render

    def test_b2_not_for_a_qc_reviewer_without_head_authority(self):
        site, assignment = self._released('CS-B2')
        self._raise(self.pm, site)
        change = self._pending(assignment)
        response = self._get(self.qc, 'design_qc_review', project_id=site.project_id)
        self.assertEqual(response.status_code, 200)
        page = response.content.decode()
        # The banner IS shown to them — only the actions are not.
        self.assertIn(change.reason, page)
        self.assertNotIn(reverse('design_change_request_correct', kwargs={'pk': change.pk}),
                         page)
        self.assertNotIn('correction_note', page)

    def test_b3_the_count_is_what_the_endpoint_would_cite(self):
        site, assignment = self._released('CS-B3')
        self._raise(self.pm, site)
        self._boq_correct(site, 12)
        page = self._banner(self.head, site)
        self.assertIn('1 BOQ correction\n              made since this request was raised '
                      'will be cited.', page)

    def test_b4_the_deputy_gets_the_button_as_accept_and_reject_do(self):
        deputy = self._person('cs_dep', 'Design')
        self.head.design_head_deputy = deputy
        self.head.save(update_fields=['design_head_deputy'])
        site, assignment = self._released('CS-B4')
        self._raise(self.pm, site)
        change = self._pending(assignment)
        page = self._banner(deputy, site)
        self.assertIn(reverse('design_change_request_correct', kwargs={'pk': change.pk}),
                      page)


# ===========================================================================
# B2 — the PM queue's change-request section and the nav entry
# ===========================================================================

class PMQueueTests(ScreensBase):

    def test_q1_lists_a_with_pm_request_on_a_managed_site_and_not_another_pms(self):
        pm2 = self._person('cs_pm2', 'PM')
        mine, _, change = self._with_pm('CS-Q1A')
        other, _, _ = self._with_pm('CS-Q1B', pm=pm2)
        other.coordinators.clear()
        rows = self._queue(self.pm).context['change_rows']
        self.assertEqual([r['change_request'].pk for r in rows], [change.pk])
        self.assertEqual([r['site'].project_id for r in self._queue(pm2).context['change_rows']],
                         ['CS-Q1B'])

    def test_q2_age_counts_from_requested_at_and_the_oldest_is_first(self):
        _, _, newer = self._with_pm('CS-Q2A')
        _, _, older = self._with_pm('CS-Q2B')
        now = timezone.now()
        self._backdate(newer, requested_at=now - timedelta(days=1))
        self._backdate(older, requested_at=now - timedelta(days=4))
        rows = self._queue(self.pm).context['change_rows']
        self.assertEqual([(r['change_request'].pk, r['age_days']) for r in rows],
                         [(older.pk, 4), (newer.pk, 1)])
        page = self._queue(self.pm).content.decode()
        self.assertIn('4 days', page)
        self.assertIn(reverse('design_change_request_form', kwargs={'project_id': 'CS-Q2B'}),
                      page)
        self.assertNotIn('SESSION B2b', page)
        self.assertNotIn('Session B2b', page)   # base.html's nav comment

    def test_q3_the_coordinator_sees_it_and_an_inactive_one_does_not(self):
        _, _, change = self._with_pm('CS-Q3')
        self.assertEqual([r['change_request'].pk
                          for r in self._queue(self.coord).context['change_rows']], [change.pk])
        self.coord.is_active = False
        self.coord.save(update_fields=['is_active'])
        self.assertEqual(self._queue(self.coord).context['change_rows'], [])

    def test_q4_the_section_is_the_triage_predicate_for_every_viewer(self):
        pm2 = self._person('cs_pm2', 'PM')
        site_a, _, _ = self._with_pm('CS-Q4A')
        site_b, _, _ = self._with_pm('CS-Q4B', pm=pm2)
        for viewer in (self.pm, pm2, self.coord, self.scm, self.head, self.finance):
            listed = {r['site'].project_id for r in self._queue(viewer).context['change_rows']}
            expected = {s.project_id for s in (site_a, site_b)
                        if user_can_triage_scm_change_request(viewer.user, s)}
            with self.subTest(viewer=viewer.user.username):
                self.assertEqual(listed, expected)

    def test_q5_a_pending_request_is_not_listed(self):
        self._pending_scm('CS-Q5')
        self.assertEqual(self._queue(self.pm).context['change_rows'], [])

    def test_q6_finance_gets_a_200_with_no_rows_and_no_nav_entry(self):
        self._with_pm('CS-Q6')
        response = self._queue(self.finance)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['change_rows'], [])
        self.assertEqual(response.context['rows'], [])
        self.assertNotIn(NAV_MARK, response.content.decode())

    def test_q7_the_nav_entry_is_renamed_and_shows_for_pm_and_coordinator_only(self):
        for viewer, shown in ((self.pm, True), (self.coord, True), (self.scm, False),
                              (self.head, False), (self.finance, False)):
            page = self._queue(viewer).content.decode()
            with self.subTest(viewer=viewer.user.username):
                self.assertEqual(NAV_MARK in page, shown)
                self.assertNotIn('title="Design approvals"', page)


# ===========================================================================
# B3 — the Head's counter (D-11)
# ===========================================================================

class HeadAgeingTests(ScreensBase):

    def _fixture(self):
        now = timezone.now()
        fwd_site, _, forwarded = self._pending_scm('CS-H1')
        self._backdate(forwarded, requested_at=now - timedelta(days=6),
                       pm_decided_at=now - timedelta(days=2))
        pm_site, pm_a = self._released('CS-H2')
        self._raise(self.pm, pm_site)
        raised = self._pending(pm_a)
        self._backdate(raised, requested_at=now - timedelta(days=5))
        return forwarded, raised

    def test_h1_the_queue_counts_from_the_forward_and_from_the_raise(self):
        forwarded, raised = self._fixture()
        rows = tender_metrics(self.program)['change_requests']
        by_pk = {r['change_request'].pk: r for r in rows}
        self.assertEqual(by_pk[forwarded.pk]['age_days'], 2)
        self.assertEqual(by_pk[forwarded.pk]['with_head_since'], forwarded.pm_decided_at)
        self.assertEqual(by_pk[raised.pk]['age_days'], 5)
        self.assertEqual(by_pk[raised.pk]['with_head_since'], raised.requested_at)
        # Oldest WITH THE HEAD first: the PM-raised one has waited on him longer.
        self.assertEqual([r['change_request'].pk for r in rows], [raised.pk, forwarded.pk])

    def test_h2_the_attention_band_counts_the_same_way(self):
        self._fixture()
        reasons = {r['project'].project_id: r['reason']
                   for r in tender_metrics(self.program)['attention']}
        self.assertIn('— 2 day(s)', reasons['CS-H1'])
        self.assertIn('— 5 day(s)', reasons['CS-H2'])

    def test_h3_the_timestamp_under_the_badge_is_the_one_the_badge_counts_from(self):
        forwarded, _ = self._fixture()
        page = self._get(self.head, 'design_tender_dashboard',
                         pk=self.program.pk).content.decode()
        self.assertIn(dateformat.format(timezone.localtime(forwarded.pm_decided_at),
                                        'd M Y H:i'), page)
        self.assertNotIn(dateformat.format(timezone.localtime(forwarded.requested_at),
                                           'd M Y H:i'), page)
        self.assertNotIn('Session B2b', page)


# ===========================================================================
# B4 — Pending at
# ===========================================================================

class PendingAtTests(ScreensBase):

    def _pending_at(self, site):
        rows = self._get(self.head, 'design_head_sites', pk=self.program.pk).context['rows']
        return next(r['pending_at'] for r in rows if r['site'].pk == site.pk)

    def test_p1_with_the_pm_then_with_the_head(self):
        site, _, change = self._with_pm('CS-P1')
        self.assertEqual(self._pending_at(site), PENDING_AT_CHANGE_REQUEST_WITH_PM)
        self._forward(change)
        self.assertEqual(self._pending_at(site), PENDING_AT_CHANGE_REQUEST)

    def test_p2_the_helper_keeps_the_pending_answer_and_its_precedence(self):
        assignment = SimpleNamespace(status='released', qc_assigned_to_id=None)
        self.assertEqual(design_pending_at(assignment, None, True), PENDING_AT_CHANGE_REQUEST)
        self.assertEqual(design_pending_at(assignment, None, True,
                                           change_request_with_pm=True),
                         PENDING_AT_CHANGE_REQUEST)
        self.assertEqual(design_pending_at(assignment, None, False,
                                           change_request_with_pm=True),
                         PENDING_AT_CHANGE_REQUEST_WITH_PM)
        self.assertEqual(design_pending_at(assignment), 'With SCM')


# ===========================================================================
# A7 — one eligibility list
# ===========================================================================

class TriagerEligibilityTests(ScreensBase):

    def test_e1_an_inactive_assigned_pm_with_no_active_coordinator_takes_the_q5_path(self):
        self.pm.is_active = False
        self.pm.save(update_fields=['is_active'])
        site, assignment = self._released('CS-E1')
        site.coordinators.clear()
        mark = self._mark()
        self._raise(self.scm, site)
        change = self._requests(assignment).get()
        self.assertEqual(change.verdict, CHANGE_REQUEST_PENDING)
        self.assertTrue(ActivityLog.objects.filter(
            entity_id=change.pk, action_code=CHANGE_REQUEST_NO_PM_ACTION_CODE).exists())
        self.assertEqual(self._told(mark), ['cw_head'])
        self.assertIn(NO_PM_FALLBACK_TEXT, self._note_for(mark, self.head).message)

    def test_e2_an_inactive_auth_user_is_nobody_either(self):
        site, _ = self._released('CS-E2')   # the PM releases it, so deactivate after
        self.pm.user.is_active = False
        self.pm.user.save(update_fields=['is_active'])
        site.refresh_from_db()
        self.assertEqual([p.pk for p in scm_change_request_triagers(site)], [self.coord.pk])
        self.assertFalse(user_can_triage_scm_change_request(self.pm.user, site))

    def test_e3_an_inactive_pm_may_not_forward(self):
        site, _, change = self._with_pm('CS-E3')
        self.pm.is_active = False
        self.pm.save(update_fields=['is_active'])
        self.assertEqual(self._forward(change).status_code, 403)
        change.refresh_from_db()
        self.assertEqual(change.verdict, CHANGE_REQUEST_WITH_PM)


# ===========================================================================
# B6 — who hears about each event (D-12)
# ===========================================================================

class AudienceTests(ScreensBase):

    def test_n1_scm_raise_with_the_pm_reaches_the_pm_and_coordinators_only(self):
        site, _ = self._released('CS-N1')
        mark = self._mark()
        self._raise(self.scm, site)
        self.assertEqual(self._told(mark), ['cw_coord', 'cw_pm'])
        self.assertEqual(self._mailed(), ['cw_coord', 'cw_pm'])
        note = self._note_for(mark, self.pm)
        self.assertIn("waiting for the site's PM", note.message)
        self.assertEqual(note.link, self._form(site))

    def test_n2_q5_fallback_reaches_the_heads_and_says_no_pm(self):
        site, _ = self._pm_less('CS-N2')
        mark = self._mark()
        self._raise(self.scm, site)
        self.assertEqual(self._told(mark), ['cw_head'])
        self.assertIn(f'It skipped the PM stage: {NO_PM_FALLBACK_TEXT}.',
                      self._note_for(mark, self.head).message)

    def test_n3_pm_raise_is_unchanged(self):
        site, _ = self._released('CS-N3')
        mark = self._mark()
        self._raise(self.pm, site)
        self.assertEqual(self._told(mark), ['cw_coord', 'cw_head'])

    def test_n4_forward_reaches_the_heads_and_the_requester(self):
        head2 = self._person('cs_head2', 'Design', is_design_head=True)
        site, _, change = self._with_pm('CS-N4')
        mark, sent = self._mark(), len(self.mail)
        self._forward(change)
        self.assertEqual(self._told(mark), ['cs_head2', 'cw_head', 'cw_scm'])
        self.assertEqual(self._mailed(sent), ['cs_head2', 'cw_head', 'cw_scm'])
        self.assertEqual(self._note_for(mark, head2).link,
                         reverse('design_qc_review', kwargs={'project_id': 'CS-N4'}))
        self.assertEqual(self._note_for(mark, self.scm).link, self._form(site))
        # B3b (D-18): was 'forwarded your design change request'.
        self.assertIn('forwarded your BOQ change request',
                      self._note_for(mark, self.scm).message)

    def test_n5_the_actor_is_never_told_and_a_person_in_two_roles_is_told_once(self):
        # The PM is also a Design Head, so the forward's Head leg names them: they are the
        # actor and must be dropped. The coordinator forwarding instead is dropped too.
        self.pm.is_design_head = True
        self.pm.save(update_fields=['is_design_head'])
        _, _, change = self._with_pm('CS-N5A')
        mark = self._mark()
        self._forward(change, profile=self.pm)
        self.assertEqual(self._told(mark), ['cw_head', 'cw_scm'])

        _, _, change = self._with_pm('CS-N5B')
        mark = self._mark()
        self._forward(change, profile=self.coord)
        # The PM is reached as a Head, once.
        self.assertEqual(self._told(mark), ['cw_head', 'cw_pm', 'cw_scm'])

    def test_n6_pm_reject_reaches_the_requester(self):
        _, _, change = self._with_pm('CS-N6')
        mark = self._mark()
        self._pm_reject(change, profile=self.coord, note='The BOQ matches the survey.')
        self.assertEqual(self._told(mark), ['cw_scm'])
        note = self._note_for(mark, self.scm)
        self.assertIn('did not reach the Design Head', note.message)
        self.assertTrue(note.message.endswith('PM note: "The BOQ matches the survey."'))

    def test_n7_withdraw_reaches_the_pm_and_coordinators(self):
        _, _, change = self._with_pm('CS-N7')
        mark = self._mark()
        self._withdraw(change, profile=self.scm2)
        self.assertEqual(self._told(mark), ['cw_coord', 'cw_pm'])
        # B3b (D-18): was 'the design change request cw_scm raised'.
        self.assertIn('the BOQ change request cw_scm raised',
                      self._note_for(mark, self.pm).message)

    def test_n8_withdraw_by_the_requester_is_not_told_back(self):
        _, _, change = self._with_pm('CS-N8')
        mark = self._mark()
        self._withdraw(change)
        self.assertEqual(self._told(mark), ['cw_coord', 'cw_pm'])
        # B3b (D-18): was 'the design change request they raised'.
        self.assertIn('the BOQ change request they raised',
                      self._note_for(mark, self.pm).message)

    def test_n9_head_reject_of_an_scm_request_also_tells_the_pm_and_coordinators(self):
        _, assignment, change = self._pending_scm('CS-N9')
        mark = self._mark()
        self._reject(change, reason='The tender fixes it.')
        self.assertEqual(self._told(mark), ['cw_coord', 'cw_pm', 'cw_scm'])
        # B3b (D-18): was 'rejected your design change request' / 'rejected the design
        # change request cw_scm raised'.
        self.assertIn('rejected your BOQ change request',
                      self._note_for(mark, self.scm).message)
        self.assertIn('rejected the BOQ change request cw_scm raised',
                      self._note_for(mark, self.pm).message)

    def test_n10_head_reject_of_a_pm_request_still_tells_the_requester_alone(self):
        site, assignment = self._released('CS-N10')
        self._raise(self.pm, site)
        mark = self._mark()
        self._reject(self._pending(assignment))
        self.assertEqual(self._told(mark), ['cw_pm'])

    def test_n11_head_accept_is_unchanged(self):
        _, assignment, change = self._pending_scm('CS-N11')
        mark = self._mark()
        self._accept(change)
        self.assertEqual(self._told(mark), ['cw_coord', 'cw_des', 'cw_pm', 'cw_scm'])

    def test_n12_corrected_reaches_requester_pm_and_coordinators_with_what_changed(self):
        site, assignment, change = self._pending_scm('CS-N12')
        self._boq_correct(site, 12)
        mark, sent = self._mark(), len(self.mail)
        self._correct(change)
        change.refresh_from_db()
        self.assertEqual(change.verdict, CHANGE_REQUEST_CORRECTED)
        self.assertEqual(self._told(mark), ['cw_coord', 'cw_pm', 'cw_scm'])
        self.assertEqual(self._mailed(sent), ['cw_coord', 'cw_pm', 'cw_scm'])
        for person in (self.scm, self.pm):
            with self.subTest(recipient=person.user.username):
                message = self._note_for(mark, person).message
                self.assertIn('Changed: CW-001 10 → 12.', message)
        self.assertIn('Changed: CW-001 10 → 12.', self._mail_for(self.scm, sent)['html'])
        # B3b (D-18): was 'recorded your design change request'.
        self.assertIn('recorded your BOQ change request',
                      self._note_for(mark, self.scm).message)

    def test_n13_a_pm_requester_who_is_also_a_triager_is_told_once(self):
        site, assignment = self._released('CS-N13')
        self._raise(self.pm, site)
        self._boq_correct(site, 11)
        mark = self._mark()
        self._correct(self._pending(assignment))
        self.assertEqual(self._told(mark), ['cw_coord', 'cw_pm'])

    def test_n14_the_summary_stops_at_three_and_counts_the_rest(self):
        def c(code, before, after):
            return SimpleNamespace(item_code=code, item_description='',
                                   quantity_before=before, quantity_after=after)
        corrections = [c('A-1', Decimal('10.00'), Decimal('12.50')),
                       c('A-2', None, Decimal('4.00')),
                       c('', None, None),
                       c('A-4', Decimal('1'), Decimal('2')),
                       c('A-5', Decimal('1'), Decimal('3'))]
        self.assertEqual(_correction_summary(corrections),
                         'A-1 10 → 12.5; A-2 new line → 4; an ad-hoc line new line → '
                         'no quantity, and 2 more')
        self.assertEqual(_correction_summary(corrections[:1]), 'A-1 10 → 12.5')

    def test_n15_every_new_send_names_both_channels_and_a_relative_link(self):
        _, _, a = self._with_pm('CS-N15A')
        self._forward(a)
        _, _, b = self._with_pm('CS-N15B')
        self._pm_reject(b)
        _, _, c = self._with_pm('CS-N15C')
        self._withdraw(c)
        labels = ('design_change_request_raised_with_pm', 'design_change_request_forwarded',
                  'design_change_request_pm_rejected', 'design_change_request_withdrawn')
        for label in labels:
            with self.subTest(label=label):
                self.assertEqual(
                    set(NotificationLog.objects.filter(template_name=label)
                        .values_list('channel', flat=True)), {'in_app', 'email'})
        for note in Notification.objects.all():
            self.assertTrue(note.link.startswith('/') and not note.link.startswith('//'))
        for mail in self.mail:
            self.assertIn('href="https://testserver/', mail['html'])


# ===========================================================================
# B6 — a send that raises unwinds nothing
# ===========================================================================

class FailedSendTests(ScreensBase):

    def _failing(self):
        return patch('projects.design_views.send_notification',
                     side_effect=RuntimeError('notification outage'))

    def test_f1_forward_stands(self):
        _, _, change = self._with_pm('CS-F1')
        with self._failing(), self.assertLogs(VIEW_LOGGER, 'ERROR') as logs:
            response = self._forward(change)
        self.assertEqual(response.status_code, 302)
        change.refresh_from_db()
        self.assertEqual((change.verdict, change.pm_decided_by), (CHANGE_REQUEST_PENDING,
                                                                  self.pm))
        self.assertIn('the forward stands', logs.output[0])

    def test_f2_pm_reject_stands(self):
        _, _, change = self._with_pm('CS-F2')
        with self._failing(), self.assertLogs(VIEW_LOGGER, 'ERROR'):
            self.assertEqual(self._pm_reject(change).status_code, 302)
        change.refresh_from_db()
        self.assertEqual(change.verdict, CHANGE_REQUEST_PM_REJECTED)

    def test_f3_withdraw_stands(self):
        _, _, change = self._with_pm('CS-F3')
        with self._failing(), self.assertLogs(VIEW_LOGGER, 'ERROR'):
            self.assertEqual(self._withdraw(change).status_code, 302)
        change.refresh_from_db()
        self.assertEqual(change.verdict, CHANGE_REQUEST_WITHDRAWN)

    def test_f4_corrected_stands_with_its_evidence(self):
        site, _, change = self._pending_scm('CS-F4')
        correction = self._boq_correct(site, 12)
        with self._failing(), self.assertLogs(VIEW_LOGGER, 'ERROR'):
            self.assertEqual(self._correct(change).status_code, 302)
        change.refresh_from_db()
        self.assertEqual(change.verdict, CHANGE_REQUEST_CORRECTED)
        self.assertEqual(list(change.boq_corrections.all()), [correction])

    def test_f5_an_scm_raise_to_the_pm_stands(self):
        site, assignment = self._released('CS-F5')
        with self._failing(), self.assertLogs(VIEW_LOGGER, 'ERROR'):
            self.assertEqual(self._raise(self.scm, site).status_code, 302)
        self.assertEqual(self._requests(assignment).get().verdict, CHANGE_REQUEST_WITH_PM)

    def test_f6_head_reject_of_an_scm_request_stands(self):
        _, _, change = self._pending_scm('CS-F6')
        with self._failing(), self.assertLogs(VIEW_LOGGER, 'ERROR'):
            self.assertEqual(self._reject(change).status_code, 302)
        change.refresh_from_db()
        self.assertEqual(change.rejection_reason, 'The current version stands.')


# ===========================================================================
# The four gate notifications: in-app only, same audience
# ===========================================================================

class GateUnchangedTests(GateNotificationBase):

    def test_g1_the_four_gate_sends_are_in_app_only_to_the_same_people(self):
        switches = SystemSettings.get()
        switches.email_enabled = True
        switches.save()
        with patch('projects.notifications._send_email') as sent_email, \
                patch.object(requests.sessions.Session, 'request',
                             side_effect=AssertionError('an HTTP request was attempted')), \
                patch('projects.design_views.send_notification',
                      wraps=real_send_notification) as spy:
            site = self._to_pm('CS-G1')                         # head pass   -> PM
            self._pm_reject(site, 'Wrong inverter make.')       # PM reject   -> Head
            self._return_to_pm(site, 'An approved equivalent.')  # Head return -> PM
            self._pm_reject(site, 'Still not accepted.')        # PM reject   -> Head
            self._send_back(site)                               # send-back   -> designer
        sent_email.assert_not_called()
        self.assertEqual([call.kwargs['channels'] for call in spy.call_args_list],
                         [['in_app']] * 5)
        self.assertEqual([call.kwargs['recipient'] for call in spy.call_args_list],
                         [self.pm, self.head, self.pm, self.head, self.designer])
        self.assertEqual(NotificationLog.objects.exclude(channel='in_app').count(), 0)
