"""T3a — golden tests for the first group of sends moved onto managed templates.

WHAT IS PINNED
  For every send T3a moves (issue raised x3, issue resolved, PM assigned from the Admin
  screen and from Zoho, unassigned Zoho project, BOQ acknowledged, payment milestone):
  who receives it on each channel, the email subject exactly, the email text's body
  sentences and link, the in-app text and link, the WhatsApp template name and params
  exactly, the NotificationLog label and related_project.

  These tests were written and run green against the code BEFORE the move, then kept.
  Every assertion changed afterwards is marked CHANGED AT T3a with its reason (the
  recipient rule P2 / ruling Q3, the in-app ruling Q1, the shared PM wording Q2, the
  Admin alert T-16); the rest are untouched.

  After the move (the classes below the golden ones): a rolled-back milestone sends
  nothing; a paused event skips email and still sends in-app and WhatsApp; deactivated
  people are dropped on every channel; every active Admin gets the unassigned alert and
  the old personal address is gone from projects/; reply-to on a notify() email and a
  digest; log_label lands in template_name; a multi-line value is <br> in HTML; the seed
  command.

INTERCEPTION: the transport only. `requests.post` records the Interakt and ZeptoMail
payloads and answers 200, `requests.sessions.Session.request` raises, both master
switches are ON in the throwaway database and every person has an email address and a
phone number, so every gate in send_notification() runs as in production. Each act runs
inside captureOnCommitCallbacks(execute=True), so a send registered on commit is seen.

Run with:
    python manage.py test projects.tests_notify_migration_a --settings=solarpms.test_settings
"""
import json
from datetime import date
from io import StringIO
from pathlib import Path
from unittest import mock

import requests
from django.core.management import CommandError, call_command
from django.db import transaction
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from . import notify as N
from . import notify_admin as A
from .models import (
    BOQ, ActivityLog, Issue, Notification, NotificationLog, NotificationTemplate, Project,
    SystemSettings, Task, UserProfile,
)
from .notifications import send_aggregate_email
from .notify_registry import NOTIFICATION_EVENTS, get_event
from .tests_residential_baseline import ResidentialBaselineBase, _client_for, _profile

BASE_URL = 'https://horizon-solar-pms-production.up.railway.app'
WEBHOOK_SECRET = 'goldensecret123'


@override_settings(APP_BASE_URL=BASE_URL, ZOHO_WEBHOOK_SECRET=WEBHOOK_SECRET,
                   INTERAKT_API_KEY='test-interakt-key',
                   ZEPTOMAIL_API_KEY='test-zepto-key',
                   ZEPTOMAIL_FROM_EMAIL='noreply@horizonrenewablepower.com')
class GoldenBase(ResidentialBaselineBase):

    def setUp(self):
        super().setUp()
        # Every profile reachable by these sends gets an address and a phone, so a
        # missing contact never hides a recipient from the transport.
        self.by_phone = {}
        for number, profile in enumerate(UserProfile.objects.select_related('user'), 1):
            if not profile.user.email:
                profile.user.email = f'{profile.user.username}@example.com'
                profile.user.save(update_fields=['email'])
            profile.phone_number = f'90000{number:05d}'
            profile.save(update_fields=['phone_number'])
            self.by_phone[profile.phone_number] = profile
        for attr in ('finance', 'pm_a', 'coord_a', 'se_a', 'design_a', 'pm_b', 'scm',
                     'bd', 'ceo', 'admin', 'sysadmin'):
            getattr(self, attr).refresh_from_db()

        switches = SystemSettings.get()
        switches.email_enabled = True
        switches.whatsapp_enabled = True
        switches.save()

        self.wa_payloads, self.mail_payloads = [], []

        def record(url, headers=None, json=None, timeout=None):
            if 'interakt' in url:
                self.wa_payloads.append(json)
            elif 'zeptomail' in url:
                self.mail_payloads.append(json)
            else:
                raise AssertionError(f'unexpected POST to {url}')
            response = mock.Mock(status_code=200, text='')
            response.json.return_value = {'id': 'msg-1'}
            return response

        post_patch = mock.patch('requests.post', side_effect=record)
        http_patch = mock.patch.object(
            requests.sessions.Session, 'request',
            side_effect=AssertionError('an unrecorded HTTP request was attempted'))
        post_patch.start()
        http_patch.start()
        self.addCleanup(post_patch.stop)
        self.addCleanup(http_patch.stop)

    # -- the act and what it sent ------------------------------------------------

    def act(self, fn):
        """Run `fn`, then keep only what it sent: payloads, in-app rows and log rows."""
        self.wa_payloads.clear()
        self.mail_payloads.clear()
        note_mark = Notification.objects.order_by('-pk').values_list('pk', flat=True).first() or 0
        log_mark = NotificationLog.objects.order_by('-pk').values_list('pk', flat=True).first() or 0
        with self.captureOnCommitCallbacks(execute=True):
            result = fn()
        self.notes = list(Notification.objects.filter(pk__gt=note_mark).order_by('pk'))
        self.logs = list(NotificationLog.objects.filter(pk__gt=log_mark).order_by('pk'))
        return result

    def emails(self):
        """{address: ZeptoMail payload}; an address emailed twice fails here."""
        out = {}
        for payload in self.mail_payloads:
            address = payload['to'][0]['email_address']['address']
            self.assertNotIn(address, out, f'{address} was emailed twice')
            out[address] = payload
        return out

    def whatsapps(self):
        """{profile: (template name, params)}; params rebuilt as send_notification's flat
        list (the header value first, then the body values)."""
        out = {}
        for payload in self.wa_payloads:
            profile = self.by_phone[payload['phoneNumber']]
            self.assertNotIn(profile, out, f'{profile} was sent WhatsApp twice')
            template = payload['template']
            out[profile] = (template['name'],
                            template['headerValues'] + template['bodyValues'])
        return out

    def in_app(self):
        """{profile: Notification}; a person told twice fails here."""
        out = {}
        for note in self.notes:
            self.assertNotIn(note.recipient, out, f'{note.recipient} was told twice')
            out[note.recipient] = note
        return out

    # -- assertions shared by every send -------------------------------------------

    def assert_in_app(self, note, paragraphs, link):
        """The in-app text is the paragraphs, compared with whitespace collapsed, and the
        row carries the relative link the bell opens.

        CHANGED AT T3a (ruling Q1). Before the move the in-app text was the email text:
        the paragraphs, then "View in Horizon Solar PMS:" and the absolute URL. The URL
        line is gone because Notification.link already makes the row a link
        (notifications.html wraps each row in <a href="{{ notif.link }}">), and
        render_plain() collapses the line breaks, which the page never showed anyway.
        """
        self.assertEqual(' '.join(note.message.split()), ' '.join(' '.join(paragraphs).split()))
        self.assertNotIn('View in Horizon Solar PMS', note.message)
        self.assertEqual(note.link, link)

    def assert_email(self, payload, subject, paragraphs, link):
        self.assertEqual(payload['subject'], subject)
        for paragraph in paragraphs:
            self.assertIn(paragraph, payload['textbody'])
        self.assertIn(f'{BASE_URL}{link}', payload['textbody'])

    def assert_logs(self, project, labelled, label):
        """Every row is about `project`; the rows of `labelled` people carry `label`."""
        self.assertTrue(self.logs)
        for row in self.logs:
            self.assertEqual(row.related_project_id, project.pk)
            if row.recipient in labelled:
                self.assertEqual(row.template_name, label, (row.channel, row.recipient))

    @staticmethod
    def address(profile):
        return profile.user.email

    @staticmethod
    def name(profile):
        return profile.user.get_full_name() or profile.user.username


# ---------------------------------------------------------------------------
# Issue raised — create_project_issue, create_task_issue, create_delivery_issue
# ---------------------------------------------------------------------------

class IssueRaisedGoldenTests(GoldenBase):

    def _check(self, post):
        """The assignee gets all three channels; each other manager an in-app nudge."""
        self.act(post)
        issue = Issue.objects.get(title='Pump leak')
        link = f'/issues/{issue.pk}/'
        paragraphs = [
            'A new issue has been raised on project Alpha Residence: "Pump leak".',
            'Please login to review and coordinate resolution.',
        ]
        notes = self.in_app()
        self.assertEqual(set(notes), {self.se_a, self.coord_a})
        self.assert_in_app(notes[self.se_a], paragraphs, link)
        # The manager's nudge is not moved by T3a.
        self.assertEqual(notes[self.coord_a].message,
                         f'Issue "Pump leak" raised on {self.project_a.project_id} — Alpha Residence.')
        self.assertEqual(notes[self.coord_a].link, link)

        mails = self.emails()
        self.assertEqual(set(mails), {self.address(self.se_a)})
        self.assert_email(mails[self.address(self.se_a)],
                          'New Issue Raised — Alpha Residence', paragraphs, link)

        self.assertEqual(self.whatsapps(), {self.se_a: (
            'issue_created',
            ['Alpha Residence', self.name(self.se_a), 'Alpha Residence', self.name(self.pm_a)],
        )})
        self.assert_logs(self.project_a, {self.se_a}, 'issue_created')

    def _form(self):
        return {'title': 'Pump leak', 'severity': Issue.HIGH, 'assigned_to': str(self.se_a.pk)}

    def test_project_issue(self):
        self._check(lambda: _client_for(self.pm_a).post(
            reverse('create_project_issue', args=[self.project_a.project_id]), self._form()))

    def test_task_issue(self):
        task = self._task(self.project_a, 'Earthing Work')
        self._check(lambda: _client_for(self.pm_a).post(
            reverse('create_task_issue', args=[self.project_a.project_id, task.pk]),
            self._form()))

    def test_delivery_issue(self):
        challan = self._make_dc(self.project_a)
        self._check(lambda: _client_for(self.pm_a).post(
            reverse('create_delivery_issue', args=[self.project_a.project_id, challan.pk]),
            self._form()))


# ---------------------------------------------------------------------------
# Issue resolved — resolve_issue
# ---------------------------------------------------------------------------

class IssueResolvedGoldenTests(GoldenBase):

    def test_managers_and_assignee_hear_the_resolver_does_not(self):
        client = _client_for(self.pm_a)
        client.post(reverse('create_project_issue', args=[self.project_a.project_id]),
                    {'title': 'Pump leak', 'severity': Issue.HIGH,
                     'assigned_to': str(self.se_a.pk)})
        issue = Issue.objects.get(title='Pump leak')
        client.post(reverse('update_issue_status', args=[issue.pk]))

        self.act(lambda: client.post(reverse('resolve_issue', args=[issue.pk]),
                                     {'resolution_note': 'Sorted on site.'}))

        link = f'/issues/{issue.pk}/'
        paragraphs = [f'The issue "Pump leak" on project Alpha Residence has been marked '
                      f'as resolved by {self.name(self.pm_a)}.']
        expected = {self.coord_a, self.se_a}

        notes = self.in_app()
        self.assertEqual(set(notes), expected)
        mails = self.emails()
        self.assertEqual(set(mails), {self.address(p) for p in expected})
        for person in expected:
            self.assert_in_app(notes[person], paragraphs, link)
            self.assert_email(mails[self.address(person)],
                              'Issue Resolved — Alpha Residence', paragraphs, link)
        params = ['Alpha Residence', 'Pump leak', self.name(self.pm_a),
                  f'http://testserver{link}']
        self.assertEqual(self.whatsapps(),
                         {person: ('issue_resolved', params) for person in expected})
        self.assert_logs(self.project_a, expected, 'issue_resolved')


# ---------------------------------------------------------------------------
# PM assigned — admin_assign_pm (first assignment) and zoho_deal_closed_webhook
# ---------------------------------------------------------------------------

class PmAssignedGoldenTests(GoldenBase):

    def _draft(self, city):
        return Project.objects.create(
            customer_name='Gamma Villa', customer_phone='9876543210',
            site_address='2 Sun Road', city=city, project_type='Residential',
            status='Draft')

    def _admin_assign(self, project):
        self.act(lambda: _client_for(self.admin).post(
            reverse('admin_assign_pm', args=[project.project_id]),
            {'pm_user_id': str(self.pm_b.pk), 'expected_pm_id': ''}))
        project.refresh_from_db()
        self.assertEqual(project.assigned_pm, self.pm_b)

    def _check(self, project, paragraphs, link, url_param):
        notes = self.in_app()
        self.assertEqual(set(notes), {self.pm_b})
        self.assert_in_app(notes[self.pm_b], paragraphs, link)
        mails = self.emails()
        self.assertEqual(set(mails), {self.address(self.pm_b)})
        self.assert_email(mails[self.address(self.pm_b)],
                          'New Project Assigned: Gamma Villa', paragraphs, link)
        self.assertEqual(self.whatsapps(), {self.pm_b: (
            'assign_project', ['Gamma Villa', self.name(self.pm_b), url_param])})
        self.assert_logs(project, {self.pm_b}, 'assign_project')
        # Nobody acted on the PM's behalf in either path: no actor on the rows.
        self.assertTrue(all(row.actor_id is None for row in self.logs))

    def _admin_paragraphs(self, place):
        return [f'Hi {self.name(self.pm_b)},',
                f'You have been assigned as Project Manager for Gamma Villa{place}.',
                'Please review the project details and activate when ready.']

    def test_admin_screen_with_a_city(self):
        project = self._draft('Kanpur')
        self._admin_assign(project)
        link = f'/projects/{project.project_id}/overview/'
        self._check(project, self._admin_paragraphs(' (Kanpur)'), link,
                    f'http://testserver{link}')

    def test_admin_screen_without_a_city_drops_the_bracket(self):
        project = self._draft('')
        self._admin_assign(project)
        link = f'/projects/{project.project_id}/overview/'
        self._check(project, self._admin_paragraphs(''), link, f'http://testserver{link}')
        self.assertNotIn('()', self.in_app()[self.pm_b].message)

    def test_zoho_match(self):
        self.pm_b.user.email = 'zoho.pm@example.com'
        self.pm_b.user.save(update_fields=['email'])
        payload = {'id': 'GOLD-Z1', 'Stage': 'Closed Won', 'Account_Name': 'Gamma Villa',
                   'Assign_PM': 'zoho.pm@example.com', 'City': 'Kanpur', 'Capacity_kW': '5'}
        self.act(lambda: Client().post(
            reverse('zoho_webhook'), data=json.dumps(payload),
            content_type='application/json', HTTP_X_WEBHOOK_TOKEN=WEBHOOK_SECRET))
        project = Project.objects.get(zoho_deal_id='GOLD-Z1')
        self.assertEqual(project.assigned_pm, self.pm_b)
        # CHANGED AT T3a (ruling Q2): one project_pm_assigned event for both paths, with
        # the Admin screen's wording and link. Before: "Please review the project details
        # and begin onboarding." and the link /projects/<id>/.
        # The WhatsApp URL is built from the integer pk, not project_id. Known wrong
        # (SECONDARY_FINDINGS); pinned as is, because WhatsApp params change only in the
        # WhatsApp phase.
        self._check(project, self._admin_paragraphs(' (Kanpur)'),
                    f'/projects/{project.project_id}/overview/',
                    f'http://testserver/projects/{project.pk}/overview/')


# ---------------------------------------------------------------------------
# Unassigned project — zoho_deal_closed_webhook, no PM matched
# ---------------------------------------------------------------------------

class UnassignedProjectGoldenTests(GoldenBase):
    """CHANGED AT T3a (ruling T-16). Before: an in-app note to the first Admin row, and
    an email through send_raw_email to a hard-coded personal address, outside the master
    switch and NotificationLog. Now: in-app and email to every active Admin through
    notify(), the email to each Admin's own address, logged. The fixture's one Admin is
    the only active Admin (System Admin is not an Admin, ruling T-1)."""

    def test_active_admins_are_told_in_app_and_by_email(self):
        payload = {'id': 'GOLD-Z2', 'Stage': 'Closed Won', 'Account_Name': 'Delta Farm',
                   'Assign_PM': 'nobody@example.com', 'City': 'Agra', 'Capacity_kW': '5'}
        self.act(lambda: Client().post(
            reverse('zoho_webhook'), data=json.dumps(payload),
            content_type='application/json', HTTP_X_WEBHOOK_TOKEN=WEBHOOK_SECRET))
        project = Project.objects.get(zoho_deal_id='GOLD-Z2')
        self.assertIsNone(project.assigned_pm)
        pid = project.project_id
        link = f'/projects/{pid}/overview/'

        notes = self.in_app()
        self.assertEqual(set(notes), {self.admin})
        self.assertEqual(notes[self.admin].message,
                         f'New Draft project {pid} created from Zoho CRM (deal GOLD-Z2) — '
                         f'PM could not be assigned. Please assign a PM.')
        self.assertEqual(notes[self.admin].link, link)

        mails = self.emails()
        self.assertEqual(set(mails), {self.address(self.admin)})
        mail = mails[self.address(self.admin)]
        self.assertEqual(mail['subject'], f'[SolarPMS] Unassigned Project: {pid}')
        for line in ('A new Draft project was created via Zoho CRM but no PM could be assigned.',
                     f'Project ID : {pid}', 'Customer   : Delta Farm', 'City       : Agra',
                     'Zoho Deal  : GOLD-Z2', 'PM email from Zoho: nobody@example.com',
                     'Please log in to the Admin Panel and assign a PM:'):
            self.assertIn(line, mail['textbody'])
        self.assertIn(f'{BASE_URL}{link}', mail['textbody'])
        self.assertEqual(self.whatsapps(), {})
        self.assert_logs(project, {self.admin}, '')


# ---------------------------------------------------------------------------
# BOQ acknowledged — _notify_boq_acknowledged, via boq_acknowledge
# ---------------------------------------------------------------------------

class BoqAcknowledgedGoldenTests(GoldenBase):

    def test_managers_hear_the_acknowledging_scm_user_does_not(self):
        boq = self._seed_boq(self.project_a)
        BOQ.objects.filter(pk=boq.pk).update(status='Submitted', submitted_by=self.design_a)

        self.act(lambda: _client_for(self.scm).post(
            reverse('boq_acknowledge', args=[self.project_a.project_id])))
        boq.refresh_from_db()
        self.assertEqual(boq.status, 'Acknowledged')

        link = f'/projects/{self.project_a.project_id}/boq/'
        paragraphs = [f'The Bill of Quantities for project Alpha Residence has been '
                      f'acknowledged by {self.name(self.scm)}.',
                      'Material procurement can now proceed.']
        # CHANGED AT T3a (P2, ruling Q3): the acknowledging SCM user is the actor and is
        # dropped on every channel. Before, they were sent a copy of their own act.
        expected = {self.pm_a, self.coord_a}

        notes = self.in_app()
        self.assertEqual(set(notes), expected)
        mails = self.emails()
        self.assertEqual(set(mails), {self.address(p) for p in expected})
        for person in expected:
            self.assert_in_app(notes[person], paragraphs, link)
            self.assert_email(mails[self.address(person)],
                              'BOQ Acknowledged — Alpha Residence', paragraphs, link)
        self.assertEqual(self.whatsapps(), {
            person: ('boq_acknowledged', ['Alpha Residence', self.name(self.scm)])
            for person in expected})
        self.assert_logs(self.project_a, expected, 'boq_acknowledged')


# ---------------------------------------------------------------------------
# Payment milestone reached — _apply_task_status_change, Done on a milestone task
# ---------------------------------------------------------------------------

class PaymentMilestoneGoldenTests(GoldenBase):

    def test_managers_bd_and_ceo_hear_the_finance_user_who_completed_it_does_not(self):
        task = self._task(self.project_a, 'Advance Payment Confirmation')
        self.assertTrue(task.is_payment_milestone)
        Task.objects.filter(pk=task.pk).update(due_date=date.today())

        self.act(lambda: _client_for(self.finance).post(
            reverse('task_status_update', args=[self.project_a.project_id, task.pk]),
            {'status': Task.DONE}))
        task.refresh_from_db()
        self.assertEqual(task.status, Task.DONE)

        pid = self.project_a.project_id
        link = f'/projects/{pid}/'
        paragraphs = [f'Project {pid} (Alpha Residence) has reached payment milestone: '
                      f'"Advance Payment Confirmation".',
                      'Please initiate collection at the earliest.']
        # CHANGED AT T3a (P2, ruling Q3): the Finance user completed the task, so as the
        # actor they are dropped on every channel. Before, they were told too.
        expected = {self.pm_a, self.coord_a, self.bd, self.ceo}

        notes = self.in_app()
        self.assertEqual(set(notes), expected)
        mails = self.emails()
        self.assertEqual(set(mails), {self.address(p) for p in expected})
        for person in expected:
            self.assert_in_app(notes[person], paragraphs, link)
            self.assert_email(mails[self.address(person)],
                              'Payment Milestone Reached — Alpha Residence', paragraphs, link)
        params = ['Alpha Residence', 'Advance Payment Confirmation', 'Alpha Residence']
        self.assertEqual(self.whatsapps(),
                         {person: ('payment_notification', params) for person in expected})
        self.assert_logs(self.project_a, expected, 'payment_notification')


# ===========================================================================
# After the move: the behaviour T3a adds
# ===========================================================================

class _Rollback(Exception):
    pass


class AfterCommitTests(GoldenBase):

    def _complete_milestone(self):
        task = self._task(self.project_a, 'Advance Payment Confirmation')
        Task.objects.filter(pk=task.pk).update(due_date=date.today())
        return _client_for(self.finance).post(
            reverse('task_status_update', args=[self.project_a.project_id, task.pk]),
            {'status': Task.DONE})

    def test_a_rolled_back_milestone_sends_nothing_on_any_channel(self):
        """Ruling T-15: the send is registered on commit, so the rollback that
        task_approve or the self-certify path performs after a refusal takes the
        message with it. Before T3a all three channels had already gone out."""
        def rolled_back():
            try:
                with transaction.atomic():
                    self._complete_milestone()
                    raise _Rollback
            except _Rollback:
                pass
        self.act(rolled_back)
        self.assertEqual(self.mail_payloads, [])
        self.assertEqual(self.wa_payloads, [])
        self.assertEqual(self.notes, [])
        self.assertEqual(self.logs, [])

    def test_a_paused_event_skips_email_and_still_sends_in_app_and_whatsapp(self):
        A.set_state('payment_milestone_reached', 'email', False, self.admin,
                    'Wording under review')
        self.act(self._complete_milestone)
        expected = {self.pm_a, self.coord_a, self.bd, self.ceo}
        self.assertEqual(self.mail_payloads, [])
        self.assertEqual(set(self.in_app()), expected)
        self.assertEqual(set(self.whatsapps()), expected)
        skipped = [row for row in self.logs if row.channel == 'email']
        self.assertEqual({row.recipient for row in skipped}, expected)
        for row in skipped:
            self.assertEqual((row.status, row.error_detail), ('skipped', 'event paused'))
            self.assertEqual(row.template_name, 'payment_notification')
            self.assertEqual(row.event_code, 'payment_milestone_reached')


class RecipientRuleTests(GoldenBase):
    """Ruling Q3: the actor and deactivated people are dropped on EVERY channel."""

    def test_a_deactivated_assignee_hears_nothing_on_any_channel(self):
        client = _client_for(self.pm_a)
        client.post(reverse('create_project_issue', args=[self.project_a.project_id]),
                    {'title': 'Pump leak', 'severity': Issue.HIGH,
                     'assigned_to': str(self.se_a.pk)})
        issue = Issue.objects.get(title='Pump leak')
        client.post(reverse('update_issue_status', args=[issue.pk]))
        self.se_a.user.is_active = False
        self.se_a.user.save(update_fields=['is_active'])

        self.act(lambda: client.post(reverse('resolve_issue', args=[issue.pk]),
                                     {'resolution_note': 'Sorted.'}))
        self.assertEqual(set(self.in_app()), {self.coord_a})
        self.assertEqual(set(self.emails()), {self.address(self.coord_a)})
        self.assertEqual(set(self.whatsapps()), {self.coord_a})

    def test_resolve_recipients_drops_actor_inactive_and_duplicates(self):
        self.bd.is_active = False
        self.bd.save(update_fields=['is_active'])
        got = N.resolve_recipients(
            [self.pm_a, self.coord_a, self.pm_a, None, self.bd, self.ceo], self.coord_a)
        self.assertEqual(got, [self.pm_a, self.ceo])


class UnassignedAlertTests(GoldenBase):

    def test_every_active_admin_and_no_one_else_is_told(self):
        second = _profile('admin_two', 'Admin', email='admin_two@example.com')
        gone = _profile('admin_gone', 'Admin', email='admin_gone@example.com')
        gone.is_active = False
        gone.save(update_fields=['is_active'])
        payload = {'id': 'GOLD-Z3', 'Stage': 'Closed Won', 'Account_Name': 'Echo Farm',
                   'Assign_PM': '', 'Capacity_kW': '5'}
        self.act(lambda: Client().post(
            reverse('zoho_webhook'), data=json.dumps(payload),
            content_type='application/json', HTTP_X_WEBHOOK_TOKEN=WEBHOOK_SECRET))
        self.assertEqual(set(self.in_app()), {self.admin, second})
        self.assertEqual(set(self.emails()), {self.address(self.admin), second.user.email})
        text = self.emails()[second.user.email]['textbody']
        self.assertIn('City       : —', text)
        self.assertIn('PM email from Zoho: (blank)', text)
        for row in self.logs:
            self.assertEqual(row.event_code, 'project_unassigned_alert')

    def test_the_old_personal_address_is_gone_from_the_code(self):
        """Ruling T-16. Scans every Python file and template under projects/. The one
        remaining copy, settings.ADMIN_DIGEST_EMAIL, is outside projects/ and recorded
        in SECONDARY_FINDINGS (ruling Q6)."""
        needle = 'smzk' + '07'   # split so this file does not match itself
        root = Path(__file__).resolve().parent
        hits = [str(path.relative_to(root))
                for path in root.rglob('*')
                if path.suffix in ('.py', '.html', '.txt') and '__pycache__' not in path.parts
                and needle in path.read_text(encoding='utf-8', errors='ignore')]
        self.assertEqual(hits, [])


class ReplyToTests(GoldenBase):

    @override_settings(NOTIFY_REPLY_TO='pms@horizonrenewablepower.com')
    def test_a_notify_email_carries_reply_to_and_the_footer(self):
        boq = self._seed_boq(self.project_a)
        BOQ.objects.filter(pk=boq.pk).update(status='Submitted', submitted_by=self.design_a)
        self.act(lambda: _client_for(self.scm).post(
            reverse('boq_acknowledge', args=[self.project_a.project_id])))
        self.assertTrue(self.mail_payloads)
        footer = 'Replies to this email reach the PMS team at pms@horizonrenewablepower.com.'
        for payload in self.mail_payloads:
            self.assertEqual(payload['reply_to'], [
                {'address': 'pms@horizonrenewablepower.com', 'name': 'Horizon Solar PMS'}])
            self.assertIn(footer, payload['textbody'])
            self.assertIn(footer, payload['htmlbody'])

    @override_settings(NOTIFY_REPLY_TO='pms@horizonrenewablepower.com')
    def test_a_digest_email_carries_reply_to(self):
        send_aggregate_email('digest@example.com', 'Digest', 'Body text')
        self.assertEqual(len(self.mail_payloads), 1)
        self.assertEqual(self.mail_payloads[0]['reply_to'], [
            {'address': 'pms@horizonrenewablepower.com', 'name': 'Horizon Solar PMS'}])


class PlumbingTests(GoldenBase):

    def test_log_label_lands_in_template_name_with_event_code_beside_it(self):
        self.act(lambda: N.notify(
            'boq_acknowledged', recipients=[self.pm_a], related_project=self.project_a,
            link='/x/', log_label='some_label',
            values={'customer_name': 'C', 'project_id': 'P',
                    'acknowledged_by_name': 'S'}))
        self.assertEqual({row.channel for row in self.logs}, {'in_app', 'email'})
        for row in self.logs:
            self.assertEqual((row.template_name, row.event_code),
                             ('some_label', 'boq_acknowledged'))

    def test_a_multi_line_value_is_br_in_html_and_a_line_break_in_text(self):
        event = get_event('project_unassigned_alert')
        values = dict(event.samples(), customer_name='Line one\nLine <two>')
        _subject, text, html = N.render_email(event, None, values, '/x/')
        self.assertIn('Line one\nLine <two>', text)
        self.assertIn('Line one<br>\nLine &lt;two&gt;', html)


class SeedCommandTests(TestCase):

    def setUp(self):
        self.admin = _profile('seed_admin', 'Admin')
        self.email_events = [code for code, event in NOTIFICATION_EVENTS.items()
                             if 'email' in event.channels]

    def _run(self, *args):
        out = StringIO()
        call_command('seed_notification_templates', *args, stdout=out)
        return out.getvalue()

    def test_dry_run_writes_nothing(self):
        output = self._run('--as', 'seed_admin', '--dry-run')
        self.assertFalse(NotificationTemplate.objects.exists())
        self.assertFalse(ActivityLog.objects.filter(
            action_code='notify_template_seeded').exists())
        self.assertIn(f'Would create {len(self.email_events)} version(s)', output)

    def test_a_run_creates_one_active_version_1_per_event_and_a_second_run_nothing(self):
        self._run('--as', 'seed_admin')
        rows = NotificationTemplate.objects.all()
        self.assertEqual(sorted(r.event_code for r in rows), sorted(self.email_events))
        for row in rows:
            event = get_event(row.event_code)
            self.assertEqual((row.version_no, row.status, row.channel),
                             (1, NotificationTemplate.ACTIVE, 'email'))
            self.assertEqual((row.subject, row.body, row.button_label),
                             (event.default_subject, event.default_body, event.button_label))
            self.assertEqual(row.change_note, 'Version 1 from the wording in code at T3')
            self.assertEqual(row.created_by, self.admin)
        logs = ActivityLog.objects.filter(action_code='notify_template_seeded')
        self.assertEqual(logs.count(), len(self.email_events))
        self.assertTrue(all(log.project_id is None and log.actor == self.admin
                            for log in logs))

        output = self._run('--as', 'seed_admin')
        self.assertEqual(NotificationTemplate.objects.count(), len(self.email_events))
        self.assertIn(f'Created 0 version(s); skipped {len(self.email_events)}', output)

    def test_an_event_with_any_version_is_skipped(self):
        A.create_draft('issue_raised', 'email', self.admin)
        self._run('--as', 'seed_admin', '--event', 'issue_raised')
        self.assertEqual(NotificationTemplate.objects.get().status,
                         NotificationTemplate.DRAFT)

    def test_a_non_admin_is_refused(self):
        for username, role in (('seed_scm', 'SCM'), ('seed_sa', 'System Admin')):
            _profile(username, role)
            with self.subTest(role=role), self.assertRaises(CommandError):
                self._run('--as', username)
        self.assertFalse(NotificationTemplate.objects.exists())

    def test_an_unknown_event_is_refused(self):
        with self.assertRaises(CommandError):
            self._run('--as', 'seed_admin', '--event', 'no_such_event')
