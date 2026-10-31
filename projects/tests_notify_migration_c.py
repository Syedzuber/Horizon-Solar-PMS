"""T3c — golden tests for the approval, contractor-bill, COD, punch-point, QA/QC and
PM-handover notifications.

WHAT IS PINNED, PER LOG LABEL
  Who is told on each channel, the NotificationLog label, actor and related_project, the
  in-app text (whitespace collapsed) and link, the email subject exactly, the email text
  part, and every piece of content the HTML part carries (titles, the Material / Bill /
  Work summary line, notes and reasons, who acted, the round, the link).

    approval_step_activated            create, a sequence advance, resubmit, reassign;
                                       the approver and the Site Engineer wordings
    approval_step_activated_deputy     a design step's deputy
    approval_approved / _changes_requested / _rejected
                                       the raiser; the Site Engineer's "work not done"
    approval_withdrawn                 every activated pending assignee; the Site
                                       Engineer's work-only wording
    the in-app-only approval notices   superseded, reassigned away, replaced, kept, proxy
    cod_recorded, cod_withdrawn
    punch_point_raised_by_visit, punch_point_rectified, punch_point_reverify_rejected
    site_qaqc_assigned, site_qaqc_removed, qaqc_visit_submitted
    pm_handover_incoming, pm_handover_outgoing, pm_handover_scm

  The Site Engineer's emails carry no amount, rate, total, bill number or PDF (D-A53).

  These tests were written and run green against the code BEFORE the move, then kept.
  Every assertion changed afterwards is marked CHANGED AT T3c with its reason; the rest
  are untouched. The changes: the email layout and its link line (accepted), and a
  rejection reason's cut losing a trailing space in the subject (ruling Q4).

AFTER THE MOVE (the classes below "After the move"): the registry's T3c events and M8,
the Site Engineer events' money guard (M4), a rolled-back approval decision and COD record
sending nothing, a paused event skipping email and still sending in-app, the shared log
labels, and the seed command picking up only the T3c events.

INTERCEPTION: the transport only. `projects.notifications._zeptomail_post` records each
email, `requests.sessions.Session.request` raises, the email master switch is ON and
every person has an email address, so every gate in send_notification() runs as in
production. Each act runs its on-commit callbacks (captureOnCommitCallbacks), so a send
registered on commit is seen.

Run with:
    python manage.py test projects.tests_notify_migration_c --settings=solarpms.test_settings
"""
import html as html_lib
import re
from datetime import timedelta
from io import StringIO
from unittest import mock

import requests
from django.conf import settings
from django.contrib.auth.models import User
from django.core.management import call_command
from django.db import transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from . import notify_admin as A
from .approvals import (
    ProxyDecision, apply_approval_decision, reassign_approval_step,
    resubmit_approval_request, withdraw_approval_request,
)
from .cod_views import record_cod
from .decorators import get_user_dashboard
from .models import (
    CodRecord, Notification, NotificationLog, NotificationTemplate, PunchPoint, SystemSettings,
    APPROVAL_PARTY_DESIGN, APPROVAL_PARTY_PM, APPROVAL_PARTY_SITE_ENGINEER,
    APPROVAL_PROXY_WHATSAPP, APPROVAL_STEP_APPROVED, APPROVAL_STEP_CHANGES_REQUESTED,
    APPROVAL_STEP_REJECTED,
)
from .notify import render_email
from .notify_registry import NOTIFICATION_EVENTS
from .qaqc_views import end_site_qaqc
from .site_pm import hand_over_sites
from .tests_approvals import SE_PHOTO, ApprovalFixture
from .tests_cod_record import CodFixture, _pdf
from .tests_pm_handover import REASON, HandoverFixture
from .tests_punch_rectify import RectifyFixture
from .tests_qaqc_assignment import QaqcFixture, _client_for
from .tests_qaqc_visit import CIVIL, MODULE
from .utils import SITE_BASE_URL

APPROVE, CHANGES, REJECT = (APPROVAL_STEP_APPROVED, APPROVAL_STEP_CHANGES_REQUESTED,
                            APPROVAL_STEP_REJECTED)

# The origin both the approval emails and notify() build absolute links from. utils reads
# APP_BASE_URL once at import, so the setting is pinned to the same value in setUp.
BASE_URL = SITE_BASE_URL

# What a Site Engineer's email must never carry: the bill's amount in every spelling,
# its number, its PDF and where that is stored (raise_bill's fixture values).
BILL_SECRETS = ('₹', '12,500', '12500', 'CB-001', 'bill.pdf', 'fixture-bills')


def collapse(text):
    return ' '.join((text or '').split())


def html_text(html):
    """The words an HTML email shows: comments and tags removed, entities decoded,
    whitespace collapsed. Layout-free, so the same content in another layout compares
    equal."""
    body = re.sub(r'<!--.*?-->', ' ', html or '', flags=re.S)
    body = re.sub(r'<[^>]+>', ' ', body)
    return collapse(html_lib.unescape(body))


def hrefs(html):
    return re.findall(r'href="([^"]*)"', html or '')


def name(profile):
    return profile.user.get_full_name() or profile.user.username


class Golden:
    """Mixed in FIRST, before a fixture TestCase: the switch on, the transport recorded,
    and run() keeps only what one act sent."""

    def setUp(self):
        super().setUp()
        pinned = self.settings(APP_BASE_URL=BASE_URL)
        pinned.enable()
        self.addCleanup(pinned.disable)
        # Fixtures that create people without an address would log 'failed' for them.
        for user in User.objects.filter(email=''):
            user.email = f'{user.username}@example.com'
            user.save(update_fields=['email'])
        switches = SystemSettings.get()
        switches.email_enabled = True
        switches.save()

        self.outbox = []

        def record(to_email, to_name, subject, text_body, html_body=None):
            self.outbox.append({'to': to_email, 'subject': subject, 'text': text_body,
                                'html': html_body})
            return True, ''

        mail_patch = mock.patch('projects.notifications._zeptomail_post',
                                side_effect=record)
        http_patch = mock.patch.object(
            requests.sessions.Session, 'request',
            side_effect=AssertionError('an HTTP request was attempted'))
        mail_patch.start()
        http_patch.start()
        self.addCleanup(mail_patch.stop)
        self.addCleanup(http_patch.stop)
        self.notes, self.logs = [], []

    def run_act(self, fn, *args, capture=True, **kwargs):
        """Run `fn` and its on-commit callbacks, then keep only what it sent. capture=False
        for a fixture helper that already runs its own callbacks: nesting a second capture
        would run them twice."""
        self.outbox.clear()
        note_mark = Notification.objects.order_by('-pk').values_list('pk', flat=True).first() or 0
        log_mark = NotificationLog.objects.order_by('-pk').values_list('pk', flat=True).first() or 0
        if capture:
            with self.captureOnCommitCallbacks(execute=True):
                result = fn(*args, **kwargs)
        else:
            result = fn(*args, **kwargs)
        self.notes = list(Notification.objects.filter(pk__gt=note_mark).order_by('pk'))
        self.logs = list(NotificationLog.objects.filter(pk__gt=log_mark).order_by('pk'))
        return result

    # -- assertions --------------------------------------------------------------------

    def told(self, label=None):
        """{username} told in-app in the last act, optionally under one label only."""
        return {r.recipient.user.username for r in self.logs
                if r.channel == 'in_app' and (label is None or r.template_name == label)}

    def mails_to(self, profile):
        # Read the address from the database: setUp gave it to the row, while a fixture's
        # class-level profile still holds the User it was created with.
        address = User.objects.values_list('email', flat=True).get(pk=profile.user_id)
        return [m for m in self.outbox if m['to'] == address]

    def assert_notice(self, label, person, *, in_app, link, actor, project, email=None,
                      count=1):
        """`person` was told once (or `count` times, all alike) under `label`: in-app with
        `in_app` and `link`, and by email when `email` is a dict for assert_email()."""
        notes = [n for n in self.notes if n.recipient_id == person.pk and n.link == link]
        self.assertEqual(len(notes), count, f'{person} in-app notices with link {link}')
        for note in notes:
            self.assertEqual(collapse(note.message), collapse(in_app))
        rows = [r for r in self.logs if r.recipient_id == person.pk
                and collapse(r.message if r.channel == 'in_app' else in_app) == collapse(in_app)]
        channels = sorted(r.channel for r in rows if r.template_name == label)
        expected = (['email', 'in_app'] if email else ['in_app']) * count
        self.assertEqual(channels, sorted(expected), f'{person} channels under {label}')
        for row in rows:
            self.assertEqual(row.template_name, label)
            self.assertEqual(row.actor_id, actor.pk if actor else None)
            self.assertEqual(row.related_project_id, project.pk if project else None)
            if row.channel == 'email':
                self.assertEqual(row.status, 'sent')
        if email:
            self.assert_email(person, link=link, **email)
        else:
            self.assertEqual(self.mails_to(person), [], f'{person} was emailed')

    def assert_email(self, person, *, link, subject, body, html_has=(), layout='plain'):
        """The one email to `person`. `body` is the text part's content; `html_has` is what
        the HTML part must show. `layout` is how the send drew it before T3c: 'approval'
        (approval_notice.html: the link only in the HTML part) or 'plain' (no HTML part,
        no link anywhere). Since T3c both are base_email, and `layout` names the button."""
        mails = self.mails_to(person)
        self.assertEqual(len(mails), 1, f'{person} emails')
        mail = mails[0]
        self.assertEqual(mail['subject'], subject)
        # CHANGED AT T3c (accepted, go-ahead "Changes accepted"). Before: the text part was
        # `body` alone, with no URL in it (approval emails had the link only in the HTML
        # part; the other sends had none at all). Now base_email.txt follows the body with
        # the button's label and absolute link, then the reply-to footer.
        button = 'Open the approval' if layout == 'approval' else 'Open in Horizon Solar PMS'
        self.assertEqual(
            mail['text'],
            f'{body}\n\n{button}: {BASE_URL}{link}\n\n--\nReplies to this email reach the '
            f'PMS team at {settings.NOTIFY_REPLY_TO}.\n\n')
        # CHANGED AT T3c (accepted). Before: approval emails drew approval_notice.html
        # (the subject in a blue header, a note in a quote block) and the other sends had
        # no HTML part. Now every one is base_email.html: the same content, the one link
        # as the button.
        self.assertEqual(hrefs(mail['html']), [BASE_URL + link])
        shown = html_text(mail['html'])
        for piece in tuple(html_has) + (button,):
            self.assertIn(collapse(piece), shown)

    def assert_no_bill(self, person):
        """Nothing in any part of any email to `person` names the bill's money or file."""
        mails = self.mails_to(person)
        self.assertTrue(mails, f'{person} was not emailed')
        for mail in mails:
            for part in ('subject', 'text', 'html'):
                for secret in BILL_SECRETS:
                    self.assertNotIn(secret, mail[part] or '', f'{secret} in the {part}')


# ===========================================================================
# Approvals
# ===========================================================================

MAT = '"Module make" (Material — before order)'
MAT_SUMMARY = 'Material: 120 Nos — Solar module 545 Wp'
BILL = '"Civil works bill" (Contractor bill)'


class ApprovalGoldenBase(Golden, ApprovalFixture):

    def link(self, approval):
        return reverse('approval_detail', args=[approval.pk])

    def bill_summary(self, approval):
        site = approval.bill_detail.project.project_id
        return f'Bill: Approval Contractor · {site} · bill CB-001 · ₹12,500.00'

    def work_summary(self, approval):
        site = approval.bill_detail.project.project_id
        return f'Work: Approval Contractor · {site} · Foundation'

    def decide(self, approval, party, verdict, who, note='', proxy=None, files=()):
        return self.run_act(apply_approval_decision, self.step(approval, party), verdict,
                            who, note=note, proxy=proxy, files=files)

    def silently(self, fn, *args, **kwargs):
        """Set-up that is not under test: callbacks are discarded unrun."""
        with self.captureOnCommitCallbacks(execute=False):
            return fn(*args, **kwargs)

    def assert_asked(self, approval, person, asked_by, party, actor, summary,
                     title='Module make', about=MAT):
        """E1 to an approver."""
        sentence = (f'{asked_by} to approve {about} as the {party}. '
                    f'Open it to approve, request changes or reject.')
        self.assert_notice(
            'approval_step_activated', person, in_app=sentence, link=self.link(approval),
            actor=actor, project=None,
            email=dict(subject=f'Approval needed: {title}', body=f'{sentence}\n\n{summary}',
                       html_has=(f'Approval needed: {title}', sentence, summary),
                       layout='approval'))

    def assert_work_to_confirm(self, approval, person, asked_by, actor):
        """E1 to a Site Engineer: the work, never the bill."""
        sentence = (f'{asked_by} to confirm the work on {BILL}. Open it to confirm the work '
                    f'done, or say it is not done.')
        work = self.work_summary(approval)
        self.assert_notice(
            'approval_step_activated', person, in_app=sentence, link=self.link(approval),
            actor=actor, project=None,
            email=dict(subject='Work to confirm: Civil works bill',
                       body=f'{sentence}\n\n{work}',
                       html_has=('Work to confirm: Civil works bill', sentence, work),
                       layout='approval'))
        self.assert_no_bill(person)


class ApprovalActivatedGoldenTests(ApprovalGoldenBase):

    def test_create_asks_the_pm_the_head_and_the_heads_deputy(self):
        approval = self.run_act(self.raise_material, design=True)
        self.assertEqual(self.told(), {'ap_pm', 'ap_head', 'ap_deputy'})
        self.assert_asked(approval, self.pm, 'Ap_Scm asks you', 'PM', self.scm, MAT_SUMMARY)
        self.assert_asked(approval, self.head, 'Ap_Scm asks you', 'Design Head', self.scm,
                          MAT_SUMMARY)
        sentence = (f'{MAT} needs a Design Head decision. It is with Ap_Head; as their '
                    f'named deputy you may decide it.')
        self.assert_notice(
            'approval_step_activated_deputy', self.deputy, in_app=sentence,
            link=self.link(approval), actor=self.scm, project=None,
            email=dict(subject='Approval needed: Module make',
                       body=f'{sentence}\n\n{MAT_SUMMARY}',
                       html_has=('Approval needed: Module make', sentence, MAT_SUMMARY),
                       layout='approval'))

    def test_a_bill_asks_the_site_engineer_to_confirm_the_work(self):
        approval = self.run_act(self.raise_bill)
        self.assertEqual(self.told(), {'ap_se'})
        self.assert_work_to_confirm(approval, self.se, 'Ap_Scm asks you', self.scm)

    def test_the_site_engineers_confirmation_asks_the_pm_with_the_bill(self):
        approval = self.silently(self.raise_bill)
        self.decide(approval, APPROVAL_PARTY_SITE_ENGINEER, APPROVE, self.se,
                    files=[dict(SE_PHOTO)])
        self.assertEqual(self.told(), {'ap_pm'})
        self.assert_asked(
            approval, self.pm,
            'Ap_Se confirmed the work done as the Site Engineer; you are now asked', 'PM',
            self.se, self.bill_summary(approval), title='Civil works bill', about=BILL)

    def test_a_resubmit_asks_again_naming_the_round(self):
        approval = self.silently(self.raise_material, design=True)
        self.silently(apply_approval_decision, self.step(approval, APPROVAL_PARTY_PM),
                      CHANGES, self.pm, note='Quote the 550 Wp variant.')
        self.run_act(resubmit_approval_request, approval, self.scm, 'Spec fixed.')
        self.assertEqual(self.told(), {'ap_pm', 'ap_head', 'ap_deputy'})
        asked = 'Ap_Scm revised it (round 2) and asks you'
        self.assert_asked(approval, self.pm, asked, 'PM', self.scm, MAT_SUMMARY)
        self.assert_asked(approval, self.head, asked, 'Design Head', self.scm, MAT_SUMMARY)

    def test_a_resubmitted_bill_asks_the_site_engineer_again(self):
        approval = self.silently(self.raise_bill)
        self.silently(apply_approval_decision,
                      self.step(approval, APPROVAL_PARTY_SITE_ENGINEER), CHANGES, self.se,
                      note='Measurements missing.')
        self.run_act(resubmit_approval_request, approval, self.scm, 'Added.')
        self.assertEqual(self.told(), {'ap_se'})
        self.assert_work_to_confirm(approval, self.se,
                                    'Ap_Scm revised it (round 2) and asks you', self.scm)

    def test_a_reassignment_asks_the_new_assignee_and_tells_the_old_in_app(self):
        approval = self.silently(self.raise_material, design=False)
        self.run_act(reassign_approval_step, self.step(approval, APPROVAL_PARTY_PM),
                     self.pm_b, self.scm, 'On leave.')
        self.assertEqual(self.told(), {'ap_pm_b', 'ap_pm'})
        self.assert_asked(approval, self.pm_b, 'Ap_Scm reassigned it to you and asks you',
                          'PM', self.scm, MAT_SUMMARY)
        self.assert_notice(
            'approval_reassigned_away', self.pm,
            in_app='You are no longer asked to approve "Module make". Ap_Scm reassigned it '
                   'to Ap_Pm_B. Reason: "On leave."',
            link=self.link(approval), actor=self.scm, project=None)

    def test_a_reassigned_site_engineer_step_asks_the_new_site_engineer(self):
        approval = self.silently(self.raise_bill)
        self.run_act(reassign_approval_step,
                     self.step(approval, APPROVAL_PARTY_SITE_ENGINEER), self.se_b, self.scm,
                     'Ap_Se on leave.')
        self.assertEqual(self.told(), {'ap_se_b', 'ap_se'})
        self.assert_work_to_confirm(approval, self.se_b,
                                    'Ap_Scm reassigned it to you and asks you', self.scm)
        self.assert_notice(
            'approval_reassigned_away', self.se,
            in_app='You are no longer asked to approve "Civil works bill". Ap_Scm '
                   'reassigned it to Ap_Se_B. Reason: "Ap_Se on leave."',
            link=self.link(approval), actor=self.scm, project=None)


class ApprovalClosedGoldenTests(ApprovalGoldenBase):

    def assert_closed(self, approval, label, subject, sentence, summary, actor, note=''):
        in_app = f'{sentence} Note: "{note}"' if note else sentence
        html_has = (subject, sentence, summary) + (('Note', note) if note else ())
        self.assert_notice(
            label, self.scm, in_app=in_app, link=self.link(approval), actor=actor,
            project=None,
            email=dict(subject=subject, body=f'{in_app}\n\n{summary}', html_has=html_has,
                       layout='approval'))

    def test_approved_with_a_note(self):
        approval = self.silently(self.raise_material, design=False)
        self.decide(approval, APPROVAL_PARTY_PM, APPROVE, self.pm, note='Go with Waaree.')
        self.assertEqual(self.told(), {'ap_scm'})
        self.assert_closed(approval, 'approval_approved', 'Approved: Module make',
                           f'{MAT} is approved. Ap_Pm gave the last approval, as the PM.',
                           MAT_SUMMARY, self.pm, note='Go with Waaree.')

    def test_a_bill_approved_without_a_note_carries_the_bill(self):
        approval = self.silently(self.raise_bill)
        self.silently(apply_approval_decision,
                      self.step(approval, APPROVAL_PARTY_SITE_ENGINEER), APPROVE, self.se,
                      files=[dict(SE_PHOTO)])
        self.decide(approval, APPROVAL_PARTY_PM, APPROVE, self.pm)
        self.assertEqual(self.told(), {'ap_scm'})
        self.assert_closed(approval, 'approval_approved', 'Approved: Civil works bill',
                           f'{BILL} is approved. Ap_Pm gave the last approval, as the PM.',
                           self.bill_summary(approval), self.pm)

    def test_changes_requested_tells_the_raiser_and_supersedes_the_head_in_app(self):
        approval = self.silently(self.raise_material, design=True)
        self.decide(approval, APPROVAL_PARTY_PM, CHANGES, self.pm,
                    note='Quote the 550 Wp variant.')
        self.assertEqual(self.told(), {'ap_scm', 'ap_head'})
        self.assert_closed(
            approval, 'approval_changes_requested', 'Changes requested: Module make',
            f'Ap_Pm requested changes to {MAT} as the PM. Revise and resubmit it, or '
            f'withdraw it.', MAT_SUMMARY, self.pm, note='Quote the 550 Wp variant.')
        self.assert_notice(
            'approval_step_superseded', self.head,
            in_app='You no longer need to decide "Module make". Ap_Pm requested changes as '
                   'the PM.',
            link=self.link(approval), actor=self.pm, project=None)

    def test_the_site_engineer_saying_the_work_is_not_done(self):
        approval = self.silently(self.raise_bill)
        self.decide(approval, APPROVAL_PARTY_SITE_ENGINEER, CHANGES, self.se,
                    note='Block B not finished.')
        self.assertEqual(self.told(), {'ap_scm'})
        self.assert_closed(
            approval, 'approval_changes_requested', 'Changes requested: Civil works bill',
            f'Ap_Se said the work on {BILL} is not done, as the Site Engineer. Revise and '
            f'resubmit it, or withdraw it.', self.bill_summary(approval), self.se,
            note='Block B not finished.')

    def test_rejected(self):
        approval = self.silently(self.raise_material, design=False)
        self.decide(approval, APPROVAL_PARTY_PM, REJECT, self.pm, note='Vendor blacklisted.')
        self.assertEqual(self.told(), {'ap_scm'})
        self.assert_closed(approval, 'approval_rejected', 'Rejected: Module make',
                           f'Ap_Pm rejected {MAT} as the PM.', MAT_SUMMARY, self.pm,
                           note='Vendor blacklisted.')

    def test_a_proxy_decision_tells_the_raiser_and_the_decider_in_app(self):
        approval = self.silently(self.raise_material, design=False)
        proxy = ProxyDecision(self.pm, APPROVAL_PROXY_WHATSAPP, 'PM said yes, 10:40.')
        self.decide(approval, APPROVAL_PARTY_PM, APPROVE, self.scm_b, proxy=proxy)
        self.assertEqual(self.told(), {'ap_scm', 'ap_pm'})
        # The recorder is the actor on the raiser's notice; the decider is named.
        self.assert_closed(approval, 'approval_approved', 'Approved: Module make',
                           f'{MAT} is approved. Ap_Pm gave the last approval, as the PM.',
                           MAT_SUMMARY, self.scm_b)
        self.assert_notice(
            'approval_proxy_recorded', self.pm,
            in_app='Ap_Scm_B recorded your approval on "Module make" from WhatsApp.',
            link=self.link(approval), actor=self.scm_b, project=None)


class ApprovalWithdrawnGoldenTests(ApprovalGoldenBase):

    def withdrawn(self, approval, person, about, summary, reason):
        sentence = f'Ap_Scm withdrew {about}. You no longer need to decide it.'
        in_app = f'{sentence} Reason: "{reason}"'
        title = approval.title
        self.assert_notice(
            'approval_withdrawn', person, in_app=in_app, link=self.link(approval),
            actor=self.scm, project=None,
            email=dict(subject=f'Withdrawn: {title}', body=f'{in_app}\n\n{summary}',
                       html_has=(f'Withdrawn: {title}', sentence, summary, 'Reason', reason),
                       layout='approval'))

    def test_a_material_request_tells_each_active_assignee(self):
        approval = self.silently(self.raise_material, design=True)
        self.run_act(withdraw_approval_request, approval, self.scm, 'Vendor dropped.')
        self.assertEqual(self.told(), {'ap_pm', 'ap_head'})
        self.withdrawn(approval, self.pm, MAT, MAT_SUMMARY, 'Vendor dropped.')
        self.withdrawn(approval, self.head, MAT, MAT_SUMMARY, 'Vendor dropped.')

    def test_the_site_engineer_is_told_with_the_work_only(self):
        approval = self.silently(self.raise_bill)
        self.run_act(withdraw_approval_request, approval, self.scm, 'Bill resent.')
        self.assertEqual(self.told(), {'ap_se'})
        self.withdrawn(approval, self.se, BILL, self.work_summary(approval), 'Bill resent.')
        self.assert_no_bill(self.se)

    def test_the_pm_is_told_with_the_bill(self):
        approval = self.silently(self.raise_bill)
        self.silently(apply_approval_decision,
                      self.step(approval, APPROVAL_PARTY_SITE_ENGINEER), APPROVE, self.se,
                      files=[dict(SE_PHOTO)])
        self.run_act(withdraw_approval_request, approval, self.scm, 'Raised twice.')
        self.assertEqual(self.told(), {'ap_pm'})
        self.withdrawn(approval, self.pm, BILL, self.bill_summary(approval), 'Raised twice.')


class ApprovalResubmitInAppGoldenTests(ApprovalGoldenBase):

    def test_a_kept_approval_tells_its_decider_in_app(self):
        approval = self.silently(self.raise_bill)
        self.silently(apply_approval_decision,
                      self.step(approval, APPROVAL_PARTY_SITE_ENGINEER), APPROVE, self.se,
                      files=[dict(SE_PHOTO)])
        self.silently(apply_approval_decision, self.step(approval, APPROVAL_PARTY_PM),
                      CHANGES, self.pm, note='Wrong rate.')
        self.run_act(resubmit_approval_request, approval, self.scm, 'Rate fixed.',
                     carry={APPROVAL_PARTY_SITE_ENGINEER: 'Work unchanged'})
        self.assertEqual(self.told(), {'ap_pm', 'ap_se'})
        self.assert_asked(approval, self.pm, 'Ap_Scm revised it (round 2) and asks you',
                          'PM', self.scm, self.bill_summary(approval),
                          title='Civil works bill', about=BILL)
        self.assert_notice(
            'approval_kept', self.se,
            in_app='Ap_Scm kept your approval of "Civil works bill" for round 2, so you are '
                   'not asked again. Reason: "Work unchanged"',
            link=self.link(approval), actor=self.scm, project=None)

    def test_an_override_tells_the_replaced_approver_in_app(self):
        approval = self.silently(self.raise_material, design=False)
        self.silently(apply_approval_decision, self.step(approval, APPROVAL_PARTY_PM),
                      CHANGES, self.pm, note='Datasheet.')
        self.run_act(resubmit_approval_request, approval, self.scm, 'Attached.',
                     assignees={APPROVAL_PARTY_PM: self.pm_b})
        self.assertEqual(self.told(), {'ap_pm_b', 'ap_pm'})
        self.assert_asked(approval, self.pm_b, 'Ap_Scm revised it (round 2) and asks you',
                          'PM', self.scm, MAT_SUMMARY)
        self.assert_notice(
            'approval_replaced_on_resubmit', self.pm,
            in_app='You are no longer asked to approve "Module make". Ap_Scm named a '
                   'different PM for round 2. Ap_Pm_B is asked instead.',
            link=self.link(approval), actor=self.scm, project=None)


# ===========================================================================
# COD
# ===========================================================================

class CodGoldenBase(Golden, CodFixture):

    def label(self):
        return f'{self.site.project_id} (COD Site Alpha)'

    def post_record(self, by):
        return _client_for(by).post(reverse('cod_record', args=[self.site.project_id]), {
            'cod_date': (timezone.localdate() - timedelta(days=1)).isoformat(),
            'evidence_type': CodRecord.EVIDENCE_DISCOM_LETTER,
            'note': 'DISCOM letter received; plant exporting since yesterday.',
            'cod_pdf': _pdf()})

    def assert_cod(self, label, person, message, actor):
        link = reverse('project_overview', args=[self.site.project_id])
        self.assert_notice(label, person, in_app=message, link=link, actor=actor,
                           project=self.site,
                           email=dict(subject=message, body=message, html_has=(message,)))


class CodGoldenTests(CodGoldenBase):

    def test_recorded_tells_the_pm_and_the_ceo_not_the_coordinator_who_acted(self):
        response = self.run_act(self.post_record, self.coord)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.told('cod_recorded'), {'cod_pm', 'cod_ceo'})
        day = timezone.localdate() - timedelta(days=1)
        message = (f'COD recorded for {self.label()}: {day:%d %b %Y}, evidence DISCOM '
                   f'letter. Recorded by Cod_Coord Test.')
        self.assert_cod('cod_recorded', self.pm, message, self.coord)
        self.assert_cod('cod_recorded', self.ceo, message, self.coord)

    def test_withdrawn_tells_the_coordinator_and_the_ceo_not_the_pm_who_acted(self):
        with self.captureOnCommitCallbacks(execute=False):
            self.post_record(self.pm)
        record = CodRecord.objects.get(project=self.site)
        self.run_act(lambda: _client_for(self.pm).post(
            reverse('cod_withdraw', args=[self.site.project_id, record.pk]),
            {'reason': 'DISCOM letter was for the wrong meter.'}))
        self.assertEqual(self.told('cod_withdrawn'), {'cod_coord', 'cod_ceo'})
        message = (f'COD withdrawn for {self.label()} by Cod_Pm Test: DISCOM letter was '
                   f'for the wrong meter.')
        self.assert_cod('cod_withdrawn', self.coord, message, self.pm)
        self.assert_cod('cod_withdrawn', self.ceo, message, self.pm)


# ===========================================================================
# QA/QC visits and punch points
# ===========================================================================

class VisitGoldenBase(Golden, RectifyFixture):

    def label(self):
        return f'{self.site.project_id} (QV Site Alpha)'

    def task_link(self, task, point=None):
        link = reverse('task_detail', args=[self.site.project_id, task.pk])
        return f'{link}#punch-point-{point.pk}' if point else f'{link}#punchPointsBlock'

    def assert_plain(self, label, person, message, link, actor):
        self.assert_notice(label, person, in_app=message, link=link, actor=actor,
                           project=self.site,
                           email=dict(subject=message, body=message, html_has=(message,)))


class VisitSubmittedGoldenTests(VisitGoldenBase):

    def test_the_visit_tells_the_managers_and_each_task_assignee_once_per_task(self):
        points = self.run_act(self._raise, (self.civil, 0), (self.civil, 1),
                              (self.module, 0), capture=False)
        visit = points[0].qaqc_answer.visit
        self.assertEqual(self.told('qaqc_visit_submitted'), {'qv_pm', 'qv_coord'})
        message = (f'QA/QC visit submitted for {self.label()} by Qv_Qaqc Test: 3 points '
                   f'checked, 3 No, 3 punch points raised.')
        link = reverse('qaqc_visit_detail', args=[self.site.project_id, visit.pk])
        self.assert_plain('qaqc_visit_submitted', self.pm, message, link, self.qaqc)
        self.assert_plain('qaqc_visit_submitted', self.coord, message, link, self.qaqc)

        self.assertEqual(self.told('punch_point_raised_by_visit'), {'qv_worker'})
        day = f'{visit.visit_date:%d %b %Y}'
        civil = (f"QA/QC visit {day} raised 2 punch points on your task '{CIVIL}' at "
                 f"{self.label()}. Rectify each with a remark and a photo.")
        module = (f"QA/QC visit {day} raised 1 punch point on your task '{MODULE}' at "
                  f"{self.label()}. Rectify each with a remark and a photo.")
        module_point = PunchPoint.objects.get(qaqc_answer__visit=visit, task=self.module)
        for message, link in ((civil, self.task_link(self.civil)),
                              (module, self.task_link(self.module, module_point))):
            notes = [n for n in self.notes if n.recipient_id == self.worker.pk
                     and n.link == link]
            self.assertEqual([collapse(n.message) for n in notes], [message])
            rows = [r for r in self.logs if r.recipient_id == self.worker.pk
                    and r.template_name == 'punch_point_raised_by_visit']
            self.assertTrue(all(r.actor_id == self.qaqc.pk
                                and r.related_project_id == self.site.pk for r in rows))
        self.assertEqual(sorted(r.channel for r in self.logs
                                if r.recipient_id == self.worker.pk),
                         ['email', 'email', 'in_app', 'in_app'])
        mails = self.mails_to(self.worker)
        # CHANGED AT T3c (accepted): the text part ends with the button's link and the
        # reply-to footer, and there is an HTML part whose one link is the button. Before:
        # the text part was the sentence alone and there was no HTML part.
        footer = (f'\n\n--\nReplies to this email reach the PMS team at '
                  f'{settings.NOTIFY_REPLY_TO}.\n\n')
        expected = [(message, f'{message}\n\nOpen in Horizon Solar PMS: {BASE_URL}{link}'
                              f'{footer}', [BASE_URL + link])
                    for message, link in ((civil, self.task_link(self.civil)),
                                          (module, self.task_link(self.module, module_point)))]
        self.assertEqual(sorted((m['subject'], m['text'], hrefs(m['html'])) for m in mails),
                         sorted(expected))
        for mail in mails:
            self.assertIn(mail['subject'], html_text(mail['html']))


class PunchPointGoldenTests(VisitGoldenBase):

    def test_rectified_tells_the_qaqc_engineer(self):
        point = self._one_point()
        self.run_act(self._rectify, point, capture=False)
        self.assertEqual(self.told(), {'qv_qaqc'})
        message = (f"Punch point on '{CIVIL}' at {self.label()} was marked rectified and is "
                   f"awaiting re-verification.")
        self.assert_plain('punch_point_rectified', self.qaqc, message,
                          self.task_link(self.civil, point), self.worker)

    def test_rectified_with_no_engineer_tells_the_managers_minus_the_actor(self):
        point = self._one_point()
        with self.captureOnCommitCallbacks(execute=False):
            end_site_qaqc(self.site, self.pm)
        self.run_act(self._rectify, point, by=self.coord, capture=False)
        self.assertEqual(self.told(), {'qv_pm'})
        message = (f"Punch point on '{CIVIL}' at {self.label()} was marked rectified and is "
                   f"awaiting re-verification.")
        self.assert_plain('punch_point_rectified', self.pm, message,
                          self.task_link(self.civil, point), self.coord)

    def test_a_rejected_rectification_tells_the_assignee(self):
        point = self._one_point()
        self._rectify(point)
        self.run_act(self._reverify, point, 'reject', remark='Still loose.', capture=False)
        self.assertEqual(self.told(), {'qv_worker'})
        message = (f"The rectification of a punch point on '{CIVIL}' at {self.label()} was "
                   f"not accepted: Still loose.")
        self.assert_plain('punch_point_reverify_rejected', self.worker, message,
                          self.task_link(self.civil, point), self.qaqc)

    def test_a_long_rejection_reason_is_cut_at_500_characters(self):
        point = self._one_point()
        self._rectify(point)
        reason = ' '.join(f'clamp{i}' for i in range(120))
        self.run_act(self._reverify, point, 'reject', remark=reason, capture=False)
        message = (f"The rectification of a punch point on '{CIVIL}' at {self.label()} was "
                   f"not accepted: {reason}")[:500]
        self.assertEqual(len(message), 500)
        # CHANGED AT T3c (ruling Q4): the reason is cut, not the sentence, at the same
        # place. This cut lands just after a space; the subject is rendered one-line by
        # notify and loses that trailing space (before: `message` exactly, space included).
        # The in-app text (compared collapsed) and the text body are unchanged.
        self.assertTrue(message.endswith(' '))
        link = self.task_link(self.civil, point)
        self.assert_notice('punch_point_reverify_rejected', self.worker, in_app=message,
                           link=link, actor=self.qaqc, project=self.site,
                           email=dict(subject=message.rstrip(), body=message,
                                      html_has=(message,)))
        [note] = [n for n in self.notes if n.recipient_id == self.worker.pk]
        self.assertLessEqual(len(note.message), 500)


# ===========================================================================
# QA/QC assignment
# ===========================================================================

class QaqcAssignmentGoldenTests(Golden, QaqcFixture):

    def label(self):
        return f'{self.site_a.project_id} (QA Site Alpha)'

    def link(self):
        return reverse('project_overview', args=[self.site_a.project_id])

    def post(self, by, data):
        return _client_for(by).post(reverse('site_qaqc', args=[self.site_a.project_id]), data)

    def assigned(self, person, actor):
        message = (f'You are now the QA/QC engineer for {self.label()}. You can view the site '
                   f'and approve or reject its submitted tasks.')
        self.assert_notice('site_qaqc_assigned', person, in_app=message, link=self.link(),
                           actor=actor, project=self.site_a,
                           email=dict(subject=message, body=message, html_has=(message,)))

    def removed(self, person, actor):
        message = f'You are no longer the QA/QC engineer for {self.label()}.'
        self.assert_notice('site_qaqc_removed', person, in_app=message, link=self.link(),
                           actor=actor, project=self.site_a,
                           email=dict(subject=message, body=message, html_has=(message,)))

    def test_assigned(self):
        self.run_act(self.post, self.pm_a, {'action': 'assign', 'engineer_id': self.qaqc.pk})
        self.assertEqual(self.told(), {'qq_qaqc'})
        self.assigned(self.qaqc, self.pm_a)

    def test_replaced_tells_the_new_and_the_outgoing_engineer(self):
        with self.captureOnCommitCallbacks(execute=False):
            self.post(self.pm_a, {'action': 'assign', 'engineer_id': self.qaqc.pk})
        self.run_act(self.post, self.coord_a, {'action': 'assign',
                                               'engineer_id': self.spare.pk})
        self.assertEqual(self.told(), {'qq_spare', 'qq_qaqc'})
        self.assigned(self.spare, self.coord_a)
        self.removed(self.qaqc, self.coord_a)

    def test_ended(self):
        with self.captureOnCommitCallbacks(execute=False):
            self.post(self.pm_a, {'action': 'assign', 'engineer_id': self.qaqc.pk})
        self.run_act(self.post, self.coord_a, {'action': 'end'})
        self.assertEqual(self.told(), {'qq_qaqc'})
        self.removed(self.qaqc, self.coord_a)


# ===========================================================================
# PM handover
# ===========================================================================

class HandoverGoldenTests(Golden, HandoverFixture):

    def hand_over(self, sites, from_pm, program=None):
        return self.run_act(hand_over_sites, sites, from_pm, self.pm_b, self.admin, REASON,
                            program=program)

    def assert_pm(self, label, person, message, link, project):
        self.assert_notice(label, person, in_app=message, link=link, actor=self.admin,
                           project=project,
                           email=dict(subject=message, body=message, html_has=(message,)))

    def assert_scm(self, message, project):
        for scm in (self.scm, self.scm_2):
            self.assert_notice('pm_handover_scm', scm, in_app=message,
                               link=reverse('approval_list'), actor=self.admin,
                               project=project)

    def test_one_site_with_a_material_step_left_behind(self):
        handover = self.hand_over([self.s1], self.pm_a)
        self.assertEqual((handover.tasks_moved, handover.bill_steps_moved,
                          len(handover.material_steps)), (2, 1, 4))
        self.assertEqual(self.told(), {'ho_pm_a', 'ho_pm_b', 'ho_scm', 'ho_scm_2'})
        moved = '2 open tasks and 1 bill approval step'
        self.assert_pm(
            'pm_handover_incoming', self.pm_b,
            f'Ho_Admin Test handed over HOS001 to you from Ho_Pm_A Test: {moved} moved to '
            f'you. Reason: {REASON}',
            reverse('project_overview', args=['HOS001']), self.s1)
        self.assert_pm(
            'pm_handover_outgoing', self.pm_a,
            f'Ho_Admin Test handed over HOS001 from you to Ho_Pm_B Test: {moved} moved to '
            f'Ho_Pm_B Test. 4 material approval steps still name you; SCM will reassign '
            f'them. Reason: {REASON}',
            get_user_dashboard(self.pm_a.user), self.s1)
        self.assert_scm(
            'PM handover: HOS001 moved from Ho_Pm_A Test to Ho_Pm_B Test. 4 material '
            'approval steps still name Ho_Pm_A Test as PM and did not move — reassign them.',
            self.s1)

    def test_a_programs_sites_link_the_incoming_pm_to_the_program(self):
        handover = self.hand_over([self.s1, self.s2, self.s3], self.pm_a, program=self.program)
        self.assertEqual((handover.tasks_moved, handover.bill_steps_moved,
                          len(handover.material_steps)), (6, 2, 4))
        what = '3 sites in MPUVNL Test (HOS001, HOS002, HOS003)'
        moved = '6 open tasks and 2 bill approval steps'
        self.assert_pm(
            'pm_handover_incoming', self.pm_b,
            f'Ho_Admin Test handed over {what} to you from Ho_Pm_A Test: {moved} moved to '
            f'you. Reason: {REASON}',
            reverse('program_detail', args=[self.program.pk]), None)
        self.assert_pm(
            'pm_handover_outgoing', self.pm_a,
            f'Ho_Admin Test handed over {what} from you to Ho_Pm_B Test: {moved} moved to '
            f'Ho_Pm_B Test. 4 material approval steps still name you; SCM will reassign '
            f'them. Reason: {REASON}',
            get_user_dashboard(self.pm_a.user), None)
        self.assert_scm(
            f'PM handover: {what} moved from Ho_Pm_A Test to Ho_Pm_B Test. 4 material '
            f'approval steps still name Ho_Pm_A Test as PM and did not move — reassign them.',
            None)

    def test_no_material_step_no_scm_notice(self):
        handover = self.hand_over([self.other], self.pm_c)
        self.assertEqual(handover.material_steps, [])
        self.assertEqual(self.told(), {'ho_pm_b', 'ho_pm_c'})
        moved = f'{handover.tasks_moved} open tasks and 0 bill approval steps'
        self.assert_pm(
            'pm_handover_incoming', self.pm_b,
            f'Ho_Admin Test handed over HOS010 to you from Ho_Pm_C Test: {moved} moved to '
            f'you. Reason: {REASON}',
            reverse('project_overview', args=['HOS010']), self.other)
        self.assert_pm(
            'pm_handover_outgoing', self.pm_c,
            f'Ho_Admin Test handed over HOS010 from you to Ho_Pm_B Test: {moved} moved to '
            f'Ho_Pm_B Test. Reason: {REASON}',
            get_user_dashboard(self.pm_c.user), self.other)


# ===========================================================================
# After the move
# ===========================================================================

T3C_EVENTS = (
    'approval_step_activated', 'approval_work_to_confirm', 'approval_step_activated_deputy',
    'approval_approved', 'approval_changes_requested', 'approval_work_not_done',
    'approval_rejected', 'approval_withdrawn', 'approval_withdrawn_work',
    'cod_recorded', 'cod_withdrawn', 'punch_point_raised_by_visit', 'punch_point_rectified',
    'punch_point_reverify_rejected', 'site_qaqc_assigned', 'site_qaqc_removed',
    'qaqc_visit_submitted', 'pm_handover_incoming', 'pm_handover_outgoing',
)
# The two events sent TO a Site Engineer. approval_work_not_done goes to the raiser and
# carries the bill summary, as before T3c.
SITE_ENGINEER_EVENTS = ('approval_work_to_confirm', 'approval_withdrawn_work')


class _Rollback(Exception):
    pass


def _admin(username='gc_admin'):
    user = User.objects.create_user(username=username, password='x')
    profile = user.profile
    profile.role = 'Admin'
    profile.save()
    return profile


class RegistryShapeTests(TestCase):

    def test_the_nineteen_events_are_registered_in_order_after_t3b(self):
        self.assertEqual(len(T3C_EVENTS), 19)
        self.assertEqual(list(NOTIFICATION_EVENTS)[-len(T3C_EVENTS) - 1:-1], list(T3C_EVENTS))  # ATT-B4's attendance_missing follows them

    def test_none_sends_whatsapp_or_says_it_does(self):
        """M8: email only, and no WhatsApp sent beside them."""
        for code in T3C_EVENTS:
            event = NOTIFICATION_EVENTS[code]
            self.assertEqual(event.channels, ('email',), code)
            self.assertFalse(event.whatsapp_sent_separately, code)

    def test_every_event_renders_its_samples(self):
        for code in T3C_EVENTS:
            event = NOTIFICATION_EVENTS[code]
            subject, text, html = render_email(event, None, event.samples(), '/x/')
            self.assertTrue(subject and text and html, code)


class SiteEngineerMoneyGuardTests(TestCase):
    """M4: the two events sent to a Site Engineer declare no variable that can carry an amount, rate,
    total or payment. Adding one fails here: the variable list is pinned exactly, and no
    name or sample may look like money."""

    MONEY = re.compile(r'amount|rate|total|payment|price|cost|bill_number|summary_line|'
                       r'rupee|value', re.I)

    def test_the_variables_are_exactly_these(self):
        self.assertEqual(
            {code: tuple(v.name for v in NOTIFICATION_EVENTS[code].variables)
             for code in SITE_ENGINEER_EVENTS},
            {'approval_work_to_confirm': ('asked_by', 'approval_title', 'kind_label',
                                          'work_summary'),
             'approval_withdrawn_work': ('withdrawn_by_name', 'approval_title', 'kind_label',
                                         'reason', 'work_summary')})

    def test_no_variable_is_named_or_sampled_like_money(self):
        for code in SITE_ENGINEER_EVENTS:
            for variable in NOTIFICATION_EVENTS[code].variables:
                self.assertIsNone(self.MONEY.search(variable.name), f'{code}.{variable.name}')
                self.assertIsNone(re.search(r'₹|\d,\d{3}', variable.sample),
                                  f'{code}.{variable.name} sample')


class AfterCommitApprovalTests(ApprovalGoldenBase):

    def test_a_rolled_back_decision_sends_nothing(self):
        approval = self.silently(self.raise_material, design=False)

        def rolled_back():
            try:
                with transaction.atomic():
                    apply_approval_decision(self.step(approval, APPROVAL_PARTY_PM), APPROVE,
                                            self.pm, note='Go.')
                    raise _Rollback
            except _Rollback:
                pass

        self.run_act(rolled_back)
        self.assertEqual((self.notes, self.logs, self.outbox), ([], [], []))

    def test_a_paused_event_skips_email_and_still_sends_in_app(self):
        A.set_state('approval_step_activated', 'email', False, _admin(), 'Wording review')
        approval = self.run_act(self.raise_material, design=True)
        # The approvers' email is paused; the deputy's is its own event and still goes.
        self.assertEqual([m['to'] for m in self.outbox], ['ap_deputy@example.com'])
        self.assertEqual(self.told(), {'ap_pm', 'ap_head', 'ap_deputy'})
        email = sorted((r.recipient.user.username, r.status, r.error_detail, r.template_name,
                        r.event_code) for r in self.logs if r.channel == 'email')
        self.assertEqual(email, [
            ('ap_deputy', 'sent', '', 'approval_step_activated_deputy',
             'approval_step_activated_deputy'),
            ('ap_head', 'skipped', 'event paused', 'approval_step_activated',
             'approval_step_activated'),
            ('ap_pm', 'skipped', 'event paused', 'approval_step_activated',
             'approval_step_activated')])
        self.assertEqual(Notification.objects.get(recipient=self.pm).link,
                         self.link(approval))

    def test_the_site_engineer_events_log_under_the_shared_labels(self):
        approval = self.run_act(self.raise_bill)
        self.assertEqual({(r.channel, r.template_name, r.event_code) for r in self.logs},
                         {('in_app', 'approval_step_activated', 'approval_work_to_confirm'),
                          ('email', 'approval_step_activated', 'approval_work_to_confirm')})
        self.decide(approval, APPROVAL_PARTY_SITE_ENGINEER, CHANGES, self.se, note='No.')
        self.assertEqual({(r.template_name, r.event_code) for r in self.logs},
                         {('approval_changes_requested', 'approval_work_not_done')})

    def test_approval_rows_have_no_related_project(self):
        """M6: as before T3c; recorded in SECONDARY_FINDINGS.md."""
        self.run_act(self.raise_bill)
        self.assertTrue(self.logs)
        self.assertTrue(all(r.related_project_id is None for r in self.logs))

    def test_a_raising_send_is_logged_and_undoes_nothing(self):
        approval = self.silently(self.raise_material, design=False)
        with mock.patch('projects.notify.send_notification', side_effect=RuntimeError('x')):
            with self.assertLogs('projects.notify', 'ERROR'):
                self.decide(approval, APPROVAL_PARTY_PM, APPROVE, self.pm)
        approval.refresh_from_db()
        self.assertEqual(approval.status, 'approved')


class AfterCommitCodTests(CodGoldenBase):

    def test_a_rolled_back_record_sends_nothing(self):
        def rolled_back():
            try:
                with transaction.atomic():
                    record_cod(self.site, self.coord.user,
                               cod_date=timezone.localdate() - timedelta(days=1),
                               evidence_type=CodRecord.EVIDENCE_DISCOM_LETTER, note='Done.',
                               stored_pdf={'file_name': 'e.pdf', 'bucket': 'b',
                                           'path': 'p/e.pdf', 'file_size_kb': 1})
                    raise _Rollback
            except _Rollback:
                pass

        self.run_act(rolled_back)
        self.assertEqual((self.notes, self.logs, self.outbox), ([], [], []))
        self.assertFalse(CodRecord.objects.filter(project=self.site).exists())

    def test_a_paused_cod_event_skips_email_and_still_sends_in_app(self):
        A.set_state('cod_recorded', 'email', False, _admin(), 'Wording review')
        self.run_act(self.post_record, self.coord)
        self.assertEqual(self.outbox, [])
        self.assertEqual(self.told('cod_recorded'), {'cod_pm', 'cod_ceo'})
        self.assertEqual(
            {(r.status, r.error_detail, r.event_code) for r in self.logs
             if r.channel == 'email'},
            {('skipped', 'event paused', 'cod_recorded')})


class SeedT3cEventsTests(TestCase):
    """The seed command creates version 1 for the T3c events and skips the T3a and T3b
    ones, already seeded."""

    def _run(self, *args):
        out = StringIO()
        call_command('seed_notification_templates', '--as', 'gc_admin', *args, stdout=out)
        return out.getvalue()

    def test_a_run_after_t3b_seeds_only_the_new_events(self):
        _admin()
        old = [code for code in NOTIFICATION_EVENTS if code not in T3C_EVENTS]
        for code in old:
            self._run('--event', code)
        created = {line.split()[3].rstrip(':') for line in self._run('--dry-run').splitlines()
                   if line.startswith('[dry run] create')}
        self.assertEqual(created, set(T3C_EVENTS))
        self._run()
        after = NotificationTemplate.objects.exclude(event_code__in=old)
        self.assertEqual(set(after.values_list('event_code', flat=True)), set(T3C_EVENTS))
        for row in after:
            event = NOTIFICATION_EVENTS[row.event_code]
            self.assertEqual((row.version_no, row.status, row.subject, row.body),
                             (1, NotificationTemplate.ACTIVE, event.default_subject,
                              event.default_body))
