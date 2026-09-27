"""Approvals 2b — notifications for approval actions (approval_notices.py).

WHAT IS PINNED
  E1  A step activated (create, resubmit, a contractor bill's sequence advance, reassign)
      tells its assignee, in-app + email; a design step also tells the Head's deputy. A
      carried step is never "activated".
  E2  A round closed as approved, changes requested or rejected tells the raiser.
  E3  A withdrawal tells every assignee whose ACTIVE step was pending.
  E4  A reassignment tells the old assignee, in-app only, if their step was active.
  R5  A resubmit override tells the replaced approver, in-app only, if their
      previous-round step was active.
  E5  A kept approval tells its original decider, in-app only.
  E6  A proxy decision tells the person it was recorded for, in-app only.
  And: never the actor; one notice per person per action; a refused action sends
  nothing and registers no callback; a notice that raises undoes nothing; the email's
  one link is the approval detail page.

INTERCEPTION, as tests_design_change_notifications: `projects.notifications.
_zeptomail_post` is a recorder, `requests.sessions.Session.request` raises, and the
email switch is ON in the throwaway database. Sends happen after commit, so every action
runs inside captureOnCommitCallbacks(execute=True), as tests_payment_queue does.

Run with:
    python manage.py test projects.tests_approval_notices --settings=solarpms.test_settings
"""
import re
from unittest import mock
from urllib.parse import urlparse

import requests
from django.urls import resolve, reverse

from . import approval_notices
from .approvals import (
    ApprovalRefused, ProxyDecision, apply_approval_decision, create_approval_request,
    reassign_approval_step, resubmit_approval_request, withdraw_approval_request,
)
from .models import (
    ApprovalRequest, ApprovalStep, Notification, NotificationLog, SystemSettings,
    APPROVAL_APPROVED, APPROVAL_CHANGES_REQUESTED, APPROVAL_REJECTED, APPROVAL_WITHDRAWN,
    APPROVAL_PARTY_DESIGN, APPROVAL_PARTY_PM, APPROVAL_PARTY_SITE_ENGINEER,
    APPROVAL_PROXY_PHONE, APPROVAL_PROXY_WHATSAPP,
    APPROVAL_STEP_APPROVED, APPROVAL_STEP_CHANGES_REQUESTED, APPROVAL_STEP_REJECTED,
)
from .tests_approvals import ApprovalFixture, _profile
from .utils import SITE_BASE_URL

A = approval_notices
BOTH = ('email', 'in_app')
IN_APP = ('in_app',)
APPROVE, CHANGES, REJECT = (APPROVAL_STEP_APPROVED, APPROVAL_STEP_CHANGES_REQUESTED,
                            APPROVAL_STEP_REJECTED)


class NoticeBase(ApprovalFixture):

    def setUp(self):
        super().setUp()
        for profile in (self.scm, self.scm_b, self.pm, self.pm_b, self.head, self.deputy,
                        self.designer, self.se, self.se_b):
            self._give_email(profile)
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

    # ── helpers ─────────────────────────────────────────────────────────────

    @staticmethod
    def _give_email(profile):
        profile.user.email = f'{profile.user.username}@example.com'
        profile.user.save(update_fields=['email'])

    def act(self, fn, *args, **kwargs):
        """Run one action the way production does: its callbacks run after commit.
        Returns (result, what was sent), where what was sent is
        {username: (template, channels)} and fails if anyone got two notices."""
        mark = NotificationLog.objects.order_by('-pk').values_list('pk', flat=True).first() or 0
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            result = fn(*args, **kwargs)
        self.assertEqual(len(callbacks), 1, 'one callback per action')
        return result, self.sent_since(mark)

    def sent_since(self, mark):
        sent = {}
        for username, template, channel in (
                NotificationLog.objects.filter(pk__gt=mark)
                .values_list('recipient__user__username', 'template_name', 'channel')):
            templates, channels = sent.setdefault(username, (set(), set()))
            templates.add(template)
            channels.add(channel)
        for username, (templates, _) in sent.items():
            self.assertEqual(len(templates), 1, f'{username} got {templates}')
        return {username: (templates.pop(), tuple(sorted(channels)))
                for username, (templates, channels) in sent.items()}

    def quiet(self, fn, *args, **kwargs):
        """A refused action: it raises ApprovalRefused, registers no callback that
        survives, and writes no NotificationLog or Notification row."""
        logs, notes = NotificationLog.objects.count(), Notification.objects.count()
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            with self.assertRaises(ApprovalRefused):
                fn(*args, **kwargs)
        self.assertEqual(callbacks, [])
        self.assertEqual(NotificationLog.objects.count(), logs)
        self.assertEqual(Notification.objects.count(), notes)
        self.assertEqual(self.mail, [])

    def note_for(self, profile):
        return Notification.objects.filter(recipient=profile).latest('pk').message

    def create_material(self, design=True, **overrides):
        return self.act(self.raise_material, design=design, **overrides)

    def decide(self, approval, party, verdict, who, note='', proxy=None):
        return self.act(apply_approval_decision, self.step(approval, party), verdict, who,
                        note=note, proxy=proxy)

    def silently(self, fn, *args, **kwargs):
        """Set-up that is not under test: callbacks are discarded unrun."""
        with self.captureOnCommitCallbacks(execute=False):
            result = fn(*args, **kwargs)
        return result


# ---------------------------------------------------------------------------
# E1 — activation
# ---------------------------------------------------------------------------

class ActivationTests(NoticeBase):

    def test_create_with_design_signoff_asks_both_parallel_assignees_and_the_deputy(self):
        _, sent = self.create_material(design=True)
        self.assertEqual(sent, {
            'ap_pm': (A.T_ACTIVATED, BOTH),
            'ap_head': (A.T_ACTIVATED, BOTH),
            'ap_deputy': (A.T_ACTIVATED_DEPUTY, BOTH),
        })
        self.assertEqual(sorted(m['to'] for m in self.mail),
                         ['ap_deputy@example.com', 'ap_head@example.com',
                          'ap_pm@example.com'])
        self.assertTrue(all(m['subject'] == 'Approval needed: Module make'
                            for m in self.mail))
        self.assertEqual(
            self.note_for(self.pm),
            'Ap_Scm asks you to approve "Module make" (Material — before order) as the '
            'PM. Open it to approve, request changes or reject.')
        self.assertIn('as their named deputy you may decide it',
                      self.note_for(self.deputy))

    def test_create_without_design_signoff_asks_the_pm_only(self):
        _, sent = self.create_material(design=False)
        self.assertEqual(sent, {'ap_pm': (A.T_ACTIVATED, BOTH)})

    def test_contractor_bill_asks_the_se_first_and_the_pm_only_when_the_se_approves(self):
        approval, sent = self.act(self.raise_bill)
        self.assertEqual(sent, {'ap_se': (A.T_ACTIVATED, BOTH)})

        _, sent = self.decide(approval, APPROVAL_PARTY_SITE_ENGINEER, APPROVE, self.se)
        self.assertEqual(sent, {'ap_pm': (A.T_ACTIVATED, BOTH)})
        self.assertIn('Ap_Se approved as the Site Engineer; you are now asked to approve',
                      self.note_for(self.pm))

    def test_a_deputy_who_is_also_an_assignee_is_told_once_as_the_assignee(self):
        self.head.design_head_deputy = self.pm
        self.head.save(update_fields=['design_head_deputy'])
        _, sent = self.create_material(design=True)
        self.assertEqual(sent, {'ap_pm': (A.T_ACTIVATED, BOTH),
                                'ap_head': (A.T_ACTIVATED, BOTH)})
        self.assertEqual(Notification.objects.filter(recipient=self.pm).count(), 1)
        self.assertEqual(len([m for m in self.mail if m['to'] == 'ap_pm@example.com']), 1)

    def test_an_inactive_deputy_is_not_told(self):
        self.deputy.is_active = False
        self.deputy.save(update_fields=['is_active'])
        _, sent = self.create_material(design=True)
        self.assertNotIn('ap_deputy', sent)

    def test_a_head_with_no_deputy_is_told_alone(self):
        self.head.design_head_deputy = None
        self.head.save(update_fields=['design_head_deputy'])
        _, sent = self.create_material(design=True)
        self.assertEqual(set(sent), {'ap_pm', 'ap_head'})

    def test_a_parallel_approval_that_leaves_the_round_open_tells_nobody(self):
        approval, _ = self.create_material(design=True)
        _, sent = self.decide(approval, APPROVAL_PARTY_PM, APPROVE, self.pm)
        self.assertEqual(sent, {})

    def test_an_idempotent_replay_of_create_sends_nothing(self):
        key = '0b6f6d2e-5a1f-4b58-9d38-7c1d8c3f2a11'
        self.create_material(client_uuid=key)
        mark = NotificationLog.objects.latest('pk').pk
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            self.raise_material(client_uuid=key)
        self.assertEqual(callbacks, [])
        self.assertEqual(self.sent_since(mark), {})


# ---------------------------------------------------------------------------
# E2 — the round closes
# ---------------------------------------------------------------------------

class RoundClosedTests(NoticeBase):

    def test_the_last_approval_tells_the_raiser(self):
        approval, _ = self.create_material(design=True)
        self.decide(approval, APPROVAL_PARTY_PM, APPROVE, self.pm)
        _, sent = self.decide(approval, APPROVAL_PARTY_DESIGN, APPROVE, self.head)
        self.assertEqual(sent, {'ap_scm': (A.T_CLOSED[APPROVAL_APPROVED], BOTH)})
        self.assertEqual(self.mail[-1]['subject'], 'Approved: Module make')

    def test_changes_requested_tells_the_raiser_with_the_note(self):
        approval, _ = self.create_material(design=True)
        _, sent = self.decide(approval, APPROVAL_PARTY_PM, CHANGES, self.pm,
                              note='Quote the 550 Wp variant.')
        self.assertEqual(sent['ap_scm'], (A.T_CLOSED[APPROVAL_CHANGES_REQUESTED], BOTH))
        mail = self.mail[-1]
        self.assertEqual(mail['subject'], 'Changes requested: Module make')
        self.assertIn('Note: "Quote the 550 Wp variant."', mail['text'])
        self.assertIn('Quote the 550 Wp variant.', mail['html'])

    def test_a_rejection_tells_the_raiser(self):
        approval, _ = self.create_material(design=False)
        _, sent = self.decide(approval, APPROVAL_PARTY_PM, REJECT, self.pm,
                              note='Not on the approved make list.')
        self.assertEqual(sent, {'ap_scm': (A.T_CLOSED[APPROVAL_REJECTED], BOTH)})
        self.assertEqual(self.mail[-1]['subject'], 'Rejected: Module make')

    def test_a_deputy_decision_names_the_deputy(self):
        approval, _ = self.create_material(design=True)
        self.decide(approval, APPROVAL_PARTY_PM, APPROVE, self.pm)
        _, sent = self.decide(approval, APPROVAL_PARTY_DESIGN, APPROVE, self.deputy)
        self.assertEqual(sent, {'ap_scm': (A.T_CLOSED[APPROVAL_APPROVED], BOTH)})
        self.assertIn('Ap_Deputy gave the last approval, as the Design Head.',
                      self.note_for(self.scm))


# ---------------------------------------------------------------------------
# E2b — an approver whose step the close superseded
# ---------------------------------------------------------------------------

class SupersededTests(NoticeBase):

    def test_pm_requesting_changes_tells_the_head_in_app_only(self):
        approval, _ = self.create_material(design=True)
        start = len(self.mail)
        _, sent = self.decide(approval, APPROVAL_PARTY_PM, CHANGES, self.pm,
                              note='Quote the 550 Wp variant.')
        self.assertEqual(sent, {
            'ap_scm': (A.T_CLOSED[APPROVAL_CHANGES_REQUESTED], BOTH),
            'ap_head': (A.T_SUPERSEDED, IN_APP),
        })
        self.assertEqual(self.note_for(self.head),
                         'You no longer need to decide "Module make". Ap_Pm requested '
                         'changes as the PM.')
        self.assertEqual([m['to'] for m in self.mail[start:]], ['ap_scm@example.com'])

    def test_pm_rejecting_tells_the_head_in_app_only(self):
        approval, _ = self.create_material(design=True)
        _, sent = self.decide(approval, APPROVAL_PARTY_PM, REJECT, self.pm,
                              note='Not on the approved make list.')
        self.assertEqual(sent, {
            'ap_scm': (A.T_CLOSED[APPROVAL_REJECTED], BOTH),
            'ap_head': (A.T_SUPERSEDED, IN_APP),
        })
        self.assertEqual(self.note_for(self.head),
                         'You no longer need to decide "Module make". Ap_Pm rejected as '
                         'the PM.')

    def test_the_deputy_is_not_told(self):
        approval, _ = self.create_material(design=True)
        _, sent = self.decide(approval, APPROVAL_PARTY_PM, CHANGES, self.pm, note='n')
        self.assertNotIn('ap_deputy', sent)

    def test_a_step_never_activated_is_not_told(self):
        approval, _ = self.act(self.raise_bill)
        _, sent = self.decide(approval, APPROVAL_PARTY_SITE_ENGINEER, CHANGES, self.se,
                              note='Measurements missing.')
        self.assertEqual(sent, {'ap_scm': (A.T_CLOSED[APPROVAL_CHANGES_REQUESTED], BOTH)})

    def test_a_head_step_never_activated_is_not_told(self):
        # No product path leaves a design step unactivated: it is sequence 1, activated
        # at create, and a reassignment inherits activation. The state the rule turns on
        # is therefore set directly. The product case is the bill test above.
        approval, _ = self.create_material(design=True)
        ApprovalStep.objects.filter(request=approval, party=APPROVAL_PARTY_DESIGN).update(
            activated_at=None)
        _, sent = self.decide(approval, APPROVAL_PARTY_PM, CHANGES, self.pm, note='n')
        self.assertEqual(sent, {'ap_scm': (A.T_CLOSED[APPROVAL_CHANGES_REQUESTED], BOTH)})

    def test_an_inactive_head_is_not_told(self):
        approval, _ = self.create_material(design=True)
        self.head.is_active = False
        self.head.save(update_fields=['is_active'])
        _, sent = self.decide(approval, APPROVAL_PARTY_PM, CHANGES, self.pm, note='n')
        self.assertNotIn('ap_head', sent)


# ---------------------------------------------------------------------------
# E6 — a proxy decision
# ---------------------------------------------------------------------------

class ProxyTests(NoticeBase):

    def test_a_proxy_decision_tells_the_decider_in_app_and_the_raiser_of_the_close(self):
        approval, _ = self.create_material(design=False)
        proxy = ProxyDecision(self.pm, APPROVAL_PROXY_WHATSAPP, 'PM said yes, 10:40.')
        _, sent = self.decide(approval, APPROVAL_PARTY_PM, APPROVE, self.scm_b,
                              proxy=proxy)
        self.assertEqual(sent, {
            'ap_pm': (A.T_PROXY, IN_APP),
            'ap_scm': (A.T_CLOSED[APPROVAL_APPROVED], BOTH),
        })
        self.assertEqual(self.note_for(self.pm),
                         'Ap_Scm_B recorded your approval on "Module make" from WhatsApp.')

    def test_the_raiser_recording_a_proxy_is_not_told_of_the_close(self):
        approval, _ = self.create_material(design=False)
        proxy = ProxyDecision(self.pm, APPROVAL_PROXY_PHONE, 'Call at 11:05.')
        _, sent = self.decide(approval, APPROVAL_PARTY_PM, CHANGES, self.scm,
                              note='Needs the datasheet.', proxy=proxy)
        self.assertEqual(sent, {'ap_pm': (A.T_PROXY, IN_APP)})
        self.assertEqual(
            self.note_for(self.pm),
            'Ap_Scm recorded your request for changes on "Module make" from a phone call.')

    def test_a_proxy_that_advances_a_bill_tells_the_pm_and_the_se(self):
        approval, _ = self.act(self.raise_bill)
        proxy = ProxyDecision(self.se, APPROVAL_PROXY_WHATSAPP, 'Measured, OK.')
        _, sent = self.decide(approval, APPROVAL_PARTY_SITE_ENGINEER, APPROVE, self.scm,
                              proxy=proxy)
        self.assertEqual(sent, {'ap_pm': (A.T_ACTIVATED, BOTH),
                                'ap_se': (A.T_PROXY, IN_APP)})


# ---------------------------------------------------------------------------
# E3 — withdrawal
# ---------------------------------------------------------------------------

class WithdrawTests(NoticeBase):

    def test_withdrawing_an_open_request_tells_each_active_assignee(self):
        approval, _ = self.create_material(design=True)
        _, sent = self.act(withdraw_approval_request, approval, self.scm, 'Vendor dropped.')
        self.assertEqual(sent, {'ap_pm': (A.T_WITHDRAWN, BOTH),
                                'ap_head': (A.T_WITHDRAWN, BOTH)})
        self.assertEqual(self.mail[-1]['subject'], 'Withdrawn: Module make')
        self.assertIn('Reason: "Vendor dropped."', self.note_for(self.pm))

    def test_an_assignee_who_already_decided_is_not_told(self):
        approval, _ = self.create_material(design=True)
        self.decide(approval, APPROVAL_PARTY_PM, APPROVE, self.pm)
        _, sent = self.act(withdraw_approval_request, approval, self.scm, 'Vendor dropped.')
        self.assertEqual(sent, {'ap_head': (A.T_WITHDRAWN, BOTH)})

    def test_a_step_never_activated_is_not_told(self):
        approval, _ = self.act(self.raise_bill)
        _, sent = self.act(withdraw_approval_request, approval, self.scm, 'Bill resent.')
        self.assertEqual(sent, {'ap_se': (A.T_WITHDRAWN, BOTH)})

    def test_withdrawing_while_changes_are_awaited_tells_nobody(self):
        approval, _ = self.create_material(design=False)
        self.decide(approval, APPROVAL_PARTY_PM, CHANGES, self.pm, note='Datasheet.')
        _, sent = self.act(withdraw_approval_request, approval, self.scm, 'Dropped.')
        self.assertEqual(sent, {})
        approval.refresh_from_db()
        self.assertEqual(approval.status, APPROVAL_WITHDRAWN)


# ---------------------------------------------------------------------------
# E4 — reassignment
# ---------------------------------------------------------------------------

class ReassignTests(NoticeBase):

    def test_reassigning_an_active_step_tells_both(self):
        approval, _ = self.create_material(design=False)
        _, sent = self.act(reassign_approval_step, self.step(approval, APPROVAL_PARTY_PM),
                           self.pm_b, self.scm, 'On leave.')
        self.assertEqual(sent, {'ap_pm_b': (A.T_ACTIVATED, BOTH),
                                'ap_pm': (A.T_REASSIGNED_AWAY, IN_APP)})
        self.assertEqual(self.note_for(self.pm),
                         'You are no longer asked to approve "Module make". Ap_Scm '
                         'reassigned it to Ap_Pm_B. Reason: "On leave."')

    def test_reassigning_a_step_not_yet_active_tells_nobody_until_it_activates(self):
        approval, _ = self.act(self.raise_bill)
        _, sent = self.act(reassign_approval_step, self.step(approval, APPROVAL_PARTY_PM),
                           self.pm_b, self.scm, 'Site handed over.')
        self.assertEqual(sent, {})
        _, sent = self.decide(approval, APPROVAL_PARTY_SITE_ENGINEER, APPROVE, self.se)
        self.assertEqual(sent, {'ap_pm_b': (A.T_ACTIVATED, BOTH)})

    def test_reassigning_a_design_step_tells_the_new_heads_deputy(self):
        head_2 = _profile('ap_head_2', 'Design', is_design_head=True)
        deputy_2 = _profile('ap_deputy_2', 'Design')
        head_2.design_head_deputy = deputy_2
        head_2.save(update_fields=['design_head_deputy'])
        self._give_email(head_2)
        self._give_email(deputy_2)
        approval, _ = self.create_material(design=True)
        _, sent = self.act(reassign_approval_step,
                           self.step(approval, APPROVAL_PARTY_DESIGN), head_2, self.scm,
                           'Head 1 travelling.')
        self.assertEqual(sent, {'ap_head_2': (A.T_ACTIVATED, BOTH),
                                'ap_deputy_2': (A.T_ACTIVATED_DEPUTY, BOTH),
                                'ap_head': (A.T_REASSIGNED_AWAY, IN_APP)})


# ---------------------------------------------------------------------------
# Resubmit — E1, R5, E5
# ---------------------------------------------------------------------------

class ResubmitTests(NoticeBase):

    def changes_requested_material(self, pm_first=True):
        approval, _ = self.create_material(design=True)
        if pm_first:
            self.decide(approval, APPROVAL_PARTY_PM, APPROVE, self.pm)
        self.decide(approval, APPROVAL_PARTY_DESIGN, CHANGES, self.head,
                    note='Wrong bifacial spec.')
        return approval

    def test_a_plain_resubmit_asks_every_party_again(self):
        approval = self.changes_requested_material()
        _, sent = self.act(resubmit_approval_request, approval, self.scm, 'Spec fixed.')
        self.assertEqual(sent, {'ap_pm': (A.T_ACTIVATED, BOTH),
                                'ap_head': (A.T_ACTIVATED, BOTH),
                                'ap_deputy': (A.T_ACTIVATED_DEPUTY, BOTH)})
        self.assertIn('Ap_Scm revised it (round 2) and asks you to approve',
                      self.note_for(self.pm))

    def test_a_kept_approval_is_not_asked_and_its_decider_is_told(self):
        approval = self.changes_requested_material()
        _, sent = self.act(resubmit_approval_request, approval, self.scm, 'Spec fixed.',
                           carry={APPROVAL_PARTY_PM: 'Only the design spec changed'})
        self.assertEqual(sent, {'ap_pm': (A.T_KEPT, IN_APP),
                                'ap_head': (A.T_ACTIVATED, BOTH),
                                'ap_deputy': (A.T_ACTIVATED_DEPUTY, BOTH)})
        self.assertEqual(self.note_for(self.pm),
                         'Ap_Scm kept your approval of "Module make" for round 2, so you '
                         'are not asked again. Reason: "Only the design spec changed"')

    def test_a_kept_site_engineer_approval_sends_the_bill_straight_to_the_pm(self):
        approval, _ = self.act(self.raise_bill)
        self.decide(approval, APPROVAL_PARTY_SITE_ENGINEER, APPROVE, self.se)
        self.decide(approval, APPROVAL_PARTY_PM, CHANGES, self.pm, note='Rate wrong.')
        _, sent = self.act(resubmit_approval_request, approval, self.scm, 'Rate fixed.',
                           carry={APPROVAL_PARTY_SITE_ENGINEER: 'Work unchanged'})
        self.assertEqual(sent, {'ap_pm': (A.T_ACTIVATED, BOTH),
                                'ap_se': (A.T_KEPT, IN_APP)})

    def test_an_override_tells_the_replaced_approver_in_app(self):
        approval, _ = self.create_material(design=False)
        self.decide(approval, APPROVAL_PARTY_PM, CHANGES, self.pm, note='Datasheet.')
        _, sent = self.act(resubmit_approval_request, approval, self.scm, 'Attached.',
                           assignees={APPROVAL_PARTY_PM: self.pm_b})
        self.assertEqual(sent, {'ap_pm_b': (A.T_ACTIVATED, BOTH),
                                'ap_pm': (A.T_REPLACED, IN_APP)})
        self.assertEqual(self.note_for(self.pm),
                         'You are no longer asked to approve "Module make". Ap_Scm named '
                         'a different PM for round 2. Ap_Pm_B is asked instead.')

    def test_an_override_of_a_step_never_activated_does_not_tell_its_assignee(self):
        approval, _ = self.act(self.raise_bill)
        self.decide(approval, APPROVAL_PARTY_SITE_ENGINEER, CHANGES, self.se,
                    note='Measurements missing.')
        _, sent = self.act(resubmit_approval_request, approval, self.scm, 'Added.',
                           assignees={APPROVAL_PARTY_PM: self.pm_b})
        self.assertEqual(sent, {'ap_se': (A.T_ACTIVATED, BOTH)})

    def test_a_refused_override_sends_nothing(self):
        approval, _ = self.create_material(design=False)
        self.decide(approval, APPROVAL_PARTY_PM, CHANGES, self.pm, note='Datasheet.')
        self.mail.clear()
        with mock.patch('projects.approvals.user_can_reassign_approval_step',
                        return_value=False):
            self.quiet(resubmit_approval_request, approval, self.scm, 'Attached.',
                       assignees={APPROVAL_PARTY_PM: self.pm_b})
        approval.refresh_from_db()
        self.assertEqual(approval.status, APPROVAL_CHANGES_REQUESTED)


# ---------------------------------------------------------------------------
# Refusals send nothing
# ---------------------------------------------------------------------------

class RefusalTests(NoticeBase):

    def setUp(self):
        super().setUp()
        self.approval = self.silently(self.raise_material, design=True)
        self.bill = self.silently(self.raise_bill)

    def test_create_refusals(self):
        self.quiet(self.raise_material, title='')
        self.quiet(self.raise_material, pm_assignee=self.scm)
        self.quiet(self.raise_material, design=True, design_assignee=self.designer)
        self.quiet(self.raise_material, raised_by=self.pm)

    def test_decision_refusals(self):
        pm_step = self.step(self.approval, APPROVAL_PARTY_PM)
        self.quiet(apply_approval_decision, pm_step, CHANGES, self.pm, note='')
        self.quiet(apply_approval_decision, pm_step, APPROVE, self.pm_b)
        self.quiet(apply_approval_decision, pm_step, APPROVE, self.scm)
        self.quiet(apply_approval_decision, pm_step, APPROVE, self.scm_b,
                   proxy=ProxyDecision(self.pm, APPROVAL_PROXY_WHATSAPP, ''))
        self.quiet(apply_approval_decision, pm_step, APPROVE, self.pm_b,
                   proxy=ProxyDecision(self.pm, APPROVAL_PROXY_WHATSAPP, 'yes'))
        # The PM holds no Design Head authority, so may not decide the design step.
        self.quiet(apply_approval_decision, self.step(self.approval, APPROVAL_PARTY_DESIGN),
                   APPROVE, self.pm)
        # A bill's PM step is not due while the Site Engineer decides.
        self.quiet(apply_approval_decision, self.step(self.bill, APPROVAL_PARTY_PM),
                   APPROVE, self.pm)
        self.silently(apply_approval_decision, pm_step, APPROVE, self.pm)
        self.quiet(apply_approval_decision, pm_step, APPROVE, self.pm)

    def test_resubmit_refusals(self):
        self.quiet(resubmit_approval_request, self.approval, self.scm, 'x')
        self.silently(apply_approval_decision, self.step(self.approval, APPROVAL_PARTY_PM),
                      APPROVE, self.pm)
        self.silently(apply_approval_decision,
                      self.step(self.approval, APPROVAL_PARTY_DESIGN), CHANGES, self.head,
                      note='Spec.')
        self.quiet(resubmit_approval_request, self.approval, self.scm, '')
        self.quiet(resubmit_approval_request, self.approval, self.pm, 'x')
        self.quiet(resubmit_approval_request, self.approval, self.scm, 'x',
                   carry={APPROVAL_PARTY_PM: 'a', APPROVAL_PARTY_DESIGN: 'b'})
        self.quiet(resubmit_approval_request, self.approval, self.scm, 'x',
                   carry={APPROVAL_PARTY_DESIGN: 'kept'})
        self.quiet(resubmit_approval_request, self.approval, self.scm, 'x',
                   assignees={APPROVAL_PARTY_PM: self.scm})
        self.quiet(resubmit_approval_request, self.approval, self.scm, 'x',
                   revision={'title': ''})

    def test_withdraw_refusals(self):
        self.quiet(withdraw_approval_request, self.approval, self.scm, '')
        self.quiet(withdraw_approval_request, self.approval, self.pm, 'x')
        self.silently(withdraw_approval_request, self.approval, self.scm, 'Dropped.')
        self.quiet(withdraw_approval_request, self.approval, self.scm, 'again')

    def test_reassign_refusals(self):
        pm_step = self.step(self.approval, APPROVAL_PARTY_PM)
        self.quiet(reassign_approval_step, pm_step, self.pm_b, self.scm, '')
        self.quiet(reassign_approval_step, pm_step, self.pm, self.scm, 'x')
        self.quiet(reassign_approval_step, pm_step, self.pm_b, self.pm, 'x')
        self.quiet(reassign_approval_step, pm_step, self.scm, self.scm, 'x')
        self.quiet(reassign_approval_step, pm_step, self.head, self.scm, 'x')


# ---------------------------------------------------------------------------
# The actor, the link, robustness
# ---------------------------------------------------------------------------

class ActorTests(NoticeBase):

    def test_the_actor_is_never_told_about_their_own_action(self):
        # A second SCM user named as the Head's deputy. Not the raiser, so only the
        # never-the-actor rule stands between them and a notice about their own resubmit.
        self.head.design_head_deputy = self.scm_b
        self.head.save(update_fields=['design_head_deputy'])
        approval, sent = self.create_material(design=True)
        self.assertEqual(sent['ap_scm_b'], (A.T_ACTIVATED_DEPUTY, BOTH))   # not the actor
        self.decide(approval, APPROVAL_PARTY_PM, CHANGES, self.pm, note='n')
        _, sent = self.act(resubmit_approval_request, approval, self.scm_b, 'Fixed.')
        self.assertEqual(sent, {'ap_pm': (A.T_ACTIVATED, BOTH),
                                'ap_head': (A.T_ACTIVATED, BOTH)})

    def test_a_different_scm_user_acting_on_the_request_does_not_hear_of_it(self):
        approval, _ = self.create_material(design=False)
        _, sent = self.act(withdraw_approval_request, approval, self.scm_b, 'Dropped.')
        self.assertEqual(sent, {'ap_pm': (A.T_WITHDRAWN, BOTH)})


class EmailTests(NoticeBase):

    def test_the_email_link_is_the_approval_detail_page_and_only_in_the_html(self):
        approval, _ = self.create_material(design=False)
        mail = self.mail[-1]
        hrefs = re.findall(r'href="([^"]+)"', mail['html'])
        self.assertEqual(hrefs, [f'{SITE_BASE_URL}/approvals/{approval.pk}/'])
        match = resolve(urlparse(hrefs[0]).path)
        self.assertEqual(match.url_name, 'approval_detail')
        self.assertEqual(match.kwargs, {'approval_pk': approval.pk})
        self.assertNotIn('http', mail['text'])
        self.assertEqual(Notification.objects.get(recipient=self.pm).link,
                         reverse('approval_detail', args=[approval.pk]))

    def test_typed_text_is_escaped_in_the_html(self):
        approval, _ = self.create_material(design=False, title='<b>Module</b> & make')
        self.decide(approval, APPROVAL_PARTY_PM, CHANGES, self.pm, note='<script>x</script>')
        html = self.mail[-1]['html']
        self.assertNotIn('<script>', html)
        self.assertNotIn('<b>Module</b>', html)
        self.assertIn('&lt;script&gt;', html)

    def test_no_email_when_the_switch_is_off(self):
        switches = SystemSettings.get()
        switches.email_enabled = False
        switches.save()
        _, sent = self.create_material(design=False)
        self.assertEqual(sent, {'ap_pm': (A.T_ACTIVATED, BOTH)})   # 'email' row = skipped
        self.assertEqual(self.mail, [])
        self.assertEqual(NotificationLog.objects.get(channel='email').status, 'skipped')


class RobustTests(NoticeBase):

    def test_a_send_that_raises_does_not_undo_the_decision(self):
        approval, _ = self.create_material(design=False)
        with mock.patch('projects.approval_notices.send_notification',
                        side_effect=RuntimeError('mail down')):
            with self.assertLogs('projects.approval_notices', 'ERROR'):
                self.decide(approval, APPROVAL_PARTY_PM, APPROVE, self.pm)
        approval.refresh_from_db()
        self.assertEqual(approval.status, APPROVAL_APPROVED)

    def test_a_callback_that_raises_does_not_undo_the_decision(self):
        approval, _ = self.create_material(design=False)
        with mock.patch('projects.approval_notices.after_decision',
                        side_effect=RuntimeError('boom')):
            with self.assertLogs('django.test', 'ERROR'):
                with self.captureOnCommitCallbacks(execute=True):
                    result = apply_approval_decision(
                        self.step(approval, APPROVAL_PARTY_PM), APPROVE, self.pm)
        self.assertEqual(result.status, APPROVAL_APPROVED)
        self.assertEqual(ApprovalRequest.objects.get(pk=approval.pk).status,
                         APPROVAL_APPROVED)
        self.assertEqual(ApprovalStep.objects.get(
            request=approval, party=APPROVAL_PARTY_PM).verdict, APPROVE)
