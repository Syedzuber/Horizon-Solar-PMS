"""T3b — golden tests for task-assignment and change-request notifications.

WHAT IS PINNED
  Task assignment (utils._notify_assignment, reached through assign_task_to(notify=True)
  directly and through task_assign and task_assign_design_head): who receives it on each
  channel, the email subject exactly, the email text's sentences and link, the in-app
  text and link, the WhatsApp template name and params exactly, the NotificationLog label,
  related_project and actor. The cooldown: 1st assign_task, 2nd assign_tasks_bulk with
  count 2, 3rd nothing, a second project its own assign_task, two assignments in one
  transaction, and the circuit breaker at exactly its threshold.

  Change requests (the seven design_views change-request views, eight log labels): the
  same per channel, plus every piece of content the email's HTML part carries (the noun,
  the project, who acted and in what capacity, the quote label and the quoted note, the
  link).

  These tests were written and run green against the code BEFORE the move, then kept.
  Every assertion changed afterwards is marked CHANGED AT T3b with its reason; the rest
  are untouched.

INTERCEPTION: the transport only, as tests_notify_migration_a. `requests.post` records
the Interakt and ZeptoMail payloads and answers 200; `requests.sessions.Session.request`
raises. Both master switches are ON and every person has an email address and a phone
number, so every gate in send_notification() runs as in production. Each act runs inside
captureOnCommitCallbacks(execute=True), so a send registered on commit is seen.

Run with:
    python manage.py test projects.tests_notify_migration_b --settings=solarpms.test_settings
"""
import html as html_lib
import re
from datetime import date, timedelta
from decimal import Decimal
from io import StringIO
from unittest import mock

import requests
from django.contrib.auth.models import User
from django.core.management import call_command
from django.db import transaction
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import notify_admin as A
from .design_views import NO_PM_FALLBACK_TEXT
from .models import (
    Notification, NotificationLog, NotificationTemplate, Project, ProjectPhase,
    SystemSettings, Task, CHANGE_REQUEST_PENDING, CHANGE_REQUEST_WITH_PM,
)
from .notify_registry import NOTIFICATION_EVENTS
from .tests_change_request_routing import RoutingBase
from .utils import (ASSIGN_CIRCUIT_BREAKER_ROWS, ASSIGN_COOLDOWN_WINDOW, SITE_BASE_URL,
                    assign_task_to)

# The origin both the assignment email and notify() build absolute links from. utils reads
# APP_BASE_URL once at import, so the override below pins notify() to the same value.
BASE_URL = SITE_BASE_URL

# The host the change-request emails' link carried before T3b: build_absolute_uri() on
# the test client's own request. Since T3b it must not appear (C3).
CHANGE_EMAIL_HOST = 'http://testserver'


def collapse(text):
    return ' '.join((text or '').split())


def html_text(html):
    """The words an HTML email shows: comments and tags removed, entities decoded,
    whitespace collapsed. Layout-free, so the same content in another layout compares
    equal."""
    body = re.sub(r'<!--.*?-->', ' ', html or '', flags=re.S)
    body = re.sub(r'<[^>]+>', ' ', body)
    return collapse(html_lib.unescape(body))


@override_settings(APP_BASE_URL=BASE_URL, INTERAKT_API_KEY='test-interakt-key',
                   ZEPTOMAIL_API_KEY='test-zepto-key',
                   ZEPTOMAIL_FROM_EMAIL='noreply@horizonrenewablepower.com')
class TransportBase(TestCase):
    """Both switches on and the transport recorded; act() keeps only what one act sent."""

    def setUp(self):
        super().setUp()
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
        self.notes, self.logs = [], []

    def act(self, fn):
        """Run `fn` and its on-commit callbacks, then keep only what it sent."""
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
        """{phone: (template name, params)}, params as send_notification's flat list."""
        out = {}
        for payload in self.wa_payloads:
            phone = payload['phoneNumber']
            self.assertNotIn(phone, out, f'{phone} was sent WhatsApp twice')
            template = payload['template']
            out[phone] = (template['name'], template['headerValues'] + template['bodyValues'])
        return out

    def in_app(self):
        """{profile: Notification}; a person told twice fails here."""
        out = {}
        for note in self.notes:
            self.assertNotIn(note.recipient, out, f'{note.recipient} was told twice')
            out[note.recipient] = note
        return out

    @staticmethod
    def name(profile):
        return profile.user.get_full_name() or profile.user.username


# ===========================================================================
# Task assignment
# ===========================================================================

_PHONES = iter(range(1, 10_000))


def _person(username, role, **flags):
    """A User with a name, an email and a phone; the UserProfile is the auto-created one."""
    user = User.objects.create_user(username=username, password='pw12345',
                                    email=f'{username}@example.com',
                                    first_name=username.split('_')[-1].title())
    profile = user.profile
    profile.role = role
    profile.phone_number = f'91{next(_PHONES):08d}'
    for flag, value in flags.items():
        setattr(profile, flag, value)
    profile.save()
    return profile


def _project(pm, name):
    return Project.objects.create(
        customer_name=name, customer_phone='9876543210', site_address='1 Sun Rd',
        city='Lucknow', project_type='Residential', dc_capacity_kw=Decimal('3.00'),
        contract_value=Decimal('100000.00'), target_commissioning_date=date(2026, 12, 1),
        status='Active', assigned_pm=pm)


def _task(project, name, role=Task.SITE_ENGINEER):
    phase, _ = ProjectPhase.objects.get_or_create(
        project=project, phase_name='Phase 1', defaults={'phase_order': 1})
    order = phase.tasks.count() + 1
    return Task.objects.create(phase=phase, task_name=name, task_order=order,
                               assigned_role=role)


class AssignGoldenBase(TransportBase):

    def setUp(self):
        super().setUp()
        self.pm = _person('ga_pm', 'PM')
        self.pm2 = _person('ga_pmtwo', 'PM')
        self.se = _person('ga_se', 'Site Engineer')
        self.se2 = _person('ga_setwo', 'Site Engineer')
        self.head = _person('ga_head', 'Design', is_design_head=True)
        self.designer = _person('ga_des', 'Design')
        self.alpha = _project(self.pm, 'Alpha Residence')
        self.bravo = _project(self.pm, 'Bravo Residence')

    def assign(self, task, user, actor=None):
        return self.act(lambda: assign_task_to(task, user, notify=True,
                                               actor=actor or self.pm))

    def labels(self):
        """The in-app label of what the last act sent, or None when it sent nothing."""
        rows = [row.template_name for row in self.logs if row.channel == 'in_app']
        self.assertLessEqual(len(rows), 1, rows)
        return rows[0] if rows else None

    # -- the two messages, exactly ----------------------------------------------

    def assert_sent(self, recipient, actor, project, *, label, subject, paragraphs, link,
                    wa_params):
        # Recipients per channel: the one assignee on all three.
        self.assertEqual(list(self.in_app()), [recipient])
        self.assertEqual(list(self.emails()), [recipient.user.email])
        self.assertEqual(self.whatsapps(),
                         {recipient.phone_number: (label, [str(p) for p in wa_params])})

        note = self.in_app()[recipient]
        # CHANGED AT T3b (T3a ruling Q1, same rule). Before the move the in-app text was
        # the email text, ending "View in Horizon Solar PMS:" and the absolute link. That
        # line is gone: Notification.link already makes the row a link.
        self.assertEqual(collapse(note.message), collapse(' '.join(paragraphs)))
        self.assertNotIn('View in Horizon Solar PMS', note.message)
        self.assertEqual(note.link, link)

        mail = self.emails()[recipient.user.email]
        self.assertEqual(mail['subject'], subject)
        for paragraph in paragraphs:
            self.assertIn(paragraph, mail['textbody'])
        self.assertIn(f'{BASE_URL}{link}', mail['textbody'])

        # One row per channel, every one labelled, about the project, naming the actor.
        self.assertEqual(sorted(row.channel for row in self.logs),
                         ['email', 'in_app', 'whatsapp'])
        for row in self.logs:
            self.assertEqual((row.template_name, row.related_project_id, row.actor_id,
                              row.recipient_id, row.status),
                             (label, project.pk, actor.pk, recipient.pk, 'sent'), row.channel)

    def assert_single(self, recipient, actor, task, wa_link_base=BASE_URL):
        project = task.phase.project
        name = self.name(recipient)
        link = f'/projects/{project.project_id}/tasks/{task.pk}/'
        self.assert_sent(
            recipient, actor, project, label='assign_task',
            subject=f'Task Assigned: {task.task_name} — {project.customer_name}',
            paragraphs=[f'Hi {name},',
                        f'The task "{task.task_name}" on project {project.customer_name} '
                        f'has been assigned to you.',
                        'Please login to review details and update progress.'],
            link=link,
            wa_params=[project.customer_name, name, task.task_name, project.customer_name,
                       f'{wa_link_base}{link}'])

    def assert_bulk(self, recipient, actor, project, count, wa_link_base=BASE_URL):
        name = self.name(recipient)
        link = f'/projects/{project.project_id}/overview/'
        self.assert_sent(
            recipient, actor, project, label='assign_tasks_bulk',
            subject=f'{count} Tasks Assigned — {project.customer_name}',
            paragraphs=[f'Hi {name},',
                        f'{count} tasks on project {project.customer_name} have been '
                        f'assigned to you.',
                        'Please login to review details and update progress.'],
            link=link,
            wa_params=[project.customer_name, name, count, project.customer_name,
                       f'{wa_link_base}{link}'])

    def assert_nothing_sent(self):
        self.assertEqual((self.notes, self.logs, self.wa_payloads, self.mail_payloads),
                         ([], [], [], []))


class AssignContentGoldenTests(AssignGoldenBase):

    def test_first_assignment_direct(self):
        task = _task(self.alpha, 'Site Survey')
        self.assign(task, self.se)
        self.assert_single(self.se, self.pm, task)

    def test_first_assignment_through_task_assign(self):
        """The view passes its request, so the WhatsApp link carries the request's host."""
        task = _task(self.alpha, 'Site Survey')
        client = Client(SERVER_NAME='localhost')
        client.force_login(self.pm.user)
        self.act(lambda: client.post(
            reverse('task_assign', args=[self.alpha.project_id, task.pk]),
            {'assigned_to': str(self.se.pk)}))
        self.assert_single(self.se, self.pm, task, wa_link_base='http://localhost')

    def test_first_assignment_through_task_assign_design_head(self):
        task = _task(self.alpha, 'Layout Drawing', role=Task.DESIGN)
        client = Client(SERVER_NAME='localhost')
        client.force_login(self.head.user)
        self.act(lambda: client.post(
            reverse('task_assign_design_head', args=[self.alpha.project_id, task.pk]),
            {'assigned_to': str(self.designer.pk)}))
        self.assert_single(self.designer, self.head, task, wa_link_base='http://localhost')

    def test_second_assignment_is_the_bulk_message_with_count_two(self):
        self.assign(_task(self.alpha, 'Task 1'), self.se)
        self.assign(_task(self.alpha, 'Task 2'), self.se)
        self.assert_bulk(self.se, self.pm, self.alpha, 2)

    def test_second_assignment_through_task_assign_is_the_bulk_message(self):
        client = Client(SERVER_NAME='localhost')
        client.force_login(self.pm.user)
        for name in ('Task 1', 'Task 2'):
            task = _task(self.alpha, name)
            self.act(lambda: client.post(
                reverse('task_assign', args=[self.alpha.project_id, task.pk]),
                {'assigned_to': str(self.se.pk)}))
        self.assert_bulk(self.se, self.pm, self.alpha, 2, wa_link_base='http://localhost')


class CooldownGoldenTests(AssignGoldenBase):

    def test_first_second_third(self):
        self.assign(_task(self.alpha, 'Task 1'), self.se)
        self.assertEqual(self.labels(), 'assign_task')
        self.assign(_task(self.alpha, 'Task 2'), self.se)
        self.assertEqual(self.labels(), 'assign_tasks_bulk')
        task = _task(self.alpha, 'Task 3')
        self.assign(task, self.se)
        self.assert_nothing_sent()
        task.refresh_from_db()
        self.assertEqual(task.assigned_to, self.se, 'the third is assigned, only not told')

    def test_the_same_person_on_a_second_project_gets_its_own_assign_task(self):
        for n in (1, 2, 3):
            self.assign(_task(self.alpha, f'Task {n}'), self.se)
        task = _task(self.bravo, 'Task B1')
        self.assign(task, self.se)
        self.assert_single(self.se, self.pm, task)

    def test_another_person_on_the_same_project_gets_their_own_assign_task(self):
        self.assign(_task(self.alpha, 'Task 1'), self.se)
        self.assign(_task(self.alpha, 'Task 2'), self.se)
        task = _task(self.alpha, 'Task 3')
        self.assign(task, self.se2)
        self.assert_single(self.se2, self.pm, task)

    def test_window_expiry_starts_again_at_assign_task(self):
        self.assign(_task(self.alpha, 'Task 1'), self.se)
        self.assign(_task(self.alpha, 'Task 2'), self.se)
        NotificationLog.objects.update(
            created_at=timezone.now() - ASSIGN_COOLDOWN_WINDOW - timedelta(minutes=1))
        self.assign(_task(self.alpha, 'Task 3'), self.se)
        self.assertEqual(self.labels(), 'assign_task')

    def test_two_assignments_in_one_transaction(self):
        """Two assignments to one person and project committed together: assign_task,
        then assign_tasks_bulk with count 2, never two assign_task."""
        first, second = _task(self.alpha, 'Task 1'), _task(self.alpha, 'Task 2')

        def both():
            with transaction.atomic():
                assign_task_to(first, self.se, notify=True, actor=self.pm)
                assign_task_to(second, self.se, notify=True, actor=self.pm)

        self.act(both)
        in_app = [row.template_name for row in self.logs if row.channel == 'in_app']
        self.assertEqual(in_app, ['assign_task', 'assign_tasks_bulk'])
        bulk = next(n for n in self.notes if '2 tasks' in n.message)
        self.assertIn('2 tasks on project Alpha Residence', bulk.message)

    def _breaker_rows(self, n):
        # On the WhatsApp channel, which the cooldown does not count, so only the
        # breaker can stop the send.
        for _ in range(n):
            NotificationLog.objects.create(
                recipient=self.se, related_project=self.alpha, channel='whatsapp',
                status='sent', message='x', template_name='assign_task')

    def test_circuit_breaker_one_row_below_its_threshold_still_sends(self):
        self._breaker_rows(ASSIGN_CIRCUIT_BREAKER_ROWS - 1)
        task = _task(self.alpha, 'Task 1')
        self.assign(task, self.se)
        self.assert_single(self.se, self.pm, task)

    def test_circuit_breaker_trips_at_its_threshold(self):
        self._breaker_rows(ASSIGN_CIRCUIT_BREAKER_ROWS)
        task = _task(self.alpha, 'Task 1')
        with self.assertLogs('projects.utils', level='ERROR') as logs:
            self.assign(task, self.se)
        self.assert_nothing_sent()
        self.assertIn('circuit breaker tripped', logs.output[0])
        task.refresh_from_db()
        self.assertEqual(task.assigned_to, self.se)


class AssignRecipientGoldenTests(AssignGoldenBase):

    def test_self_assignment(self):
        """The PM gives themself a task, then another PM gives them a second one.

        CHANGED AT T3b (ruling Q1). Before the move the self-assignment sent assign_task on
        all three channels and was counted, so the second assignment sent
        assign_tasks_bulk with count 2. Now the actor is told nothing on any channel, the
        self-assignment is not counted, and the second is the first of the window."""
        first = _task(self.alpha, 'Task 1', role=Task.PM)
        self.assign(first, self.pm, actor=self.pm)
        self.assert_nothing_sent()
        first.refresh_from_db()
        self.assertEqual(first.assigned_to, self.pm, 'the assignment itself stands')

        second = _task(self.alpha, 'Task 2', role=Task.PM)
        self.assign(second, self.pm, actor=self.pm2)
        self.assert_single(self.pm, self.pm2, second)

    def test_inactive_login_with_an_active_profile(self):
        """CHANGED AT T3b (ruling Q2). Before the move an assignee whose login was off
        (User.is_active False, profile active) was sent assign_task on all three channels;
        now resolve_recipients() drops them on every channel."""
        self.se.user.is_active = False
        self.se.user.save(update_fields=['is_active'])
        self.assertTrue(self.se.is_active, 'the premise: the profile is still active')
        task = _task(self.alpha, 'Task 1')
        self.assign(task, self.se)
        self.assert_nothing_sent()


# ===========================================================================
# Change requests
# ===========================================================================

class ChangeGoldenBase(TransportBase, RoutingBase):
    """RoutingBase's released sites, people and helpers, with this file's transport."""

    def setUp(self):
        super().setUp()
        for profile in (self.head, self.qc, self.designer, self.pm, self.coord, self.scm,
                        self.finance, self.scm2):
            profile.user.email = f'{profile.user.username}@example.com'
            profile.user.save(update_fields=['email'])

    # -- fixtures, as ScreensBase builds them ------------------------------------

    def _with_pm(self, code):
        site, assignment = self._released(code)
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

    def form(self, site):
        return reverse('design_change_request_form', kwargs={'project_id': site.project_id})

    def qc_review(self, site):
        return reverse('design_qc_review', kwargs={'project_id': site.project_id})

    def workspace(self, site):
        return reverse('design_site_workspace', kwargs={'project_id': site.project_id})

    # -- the one assertion every change-request send goes through ------------------

    def assert_text_part(self, text, body, quote_label, quote, link):
        """CHANGED AT T3b (ruling Q5). Before the move the plain-text part was the bell
        text: the body, then the quote inline as `Reason: "…"`, and no link (the link was
        in the HTML part only). Now it is the registry's default body in the shared
        layout: the sentences, the quote under its own label with its line breaks, then
        the button's line with the absolute link (and the reply-to footer)."""
        self.assertTrue(
            text.startswith(f'{body}\n\n{quote_label}:\n{quote}\n\n'
                            f'Open in Horizon Solar PMS: {BASE_URL}{link}'), text)

    def assert_change_sent(self, site, label, actor, subject, expected):
        """`expected` is {profile: (body, link, quote_label, quote)}."""
        self.assertEqual(set(self.in_app()), set(expected))
        self.assertEqual(set(self.emails()), {p.user.email for p in expected})
        self.assertEqual(self.wa_payloads, [], 'change requests never send WhatsApp')

        for person, (body, link, quote_label, quote) in expected.items():
            with self.subTest(recipient=person.user.username):
                note = self.in_app()[person]
                self.assertEqual(collapse(note.message),
                                 collapse(f'{body} {quote_label}: "{quote}"'))
                self.assertEqual(note.link, link)

                mail = self.emails()[person.user.email]
                self.assertEqual(mail['subject'], subject)
                self.assert_text_part(mail['textbody'], body, quote_label, quote, link)

                # Every piece of content the HTML part carries: the sentences (noun,
                # project, who acted and as what), the quote label, the quote, the link.
                shown = html_text(mail['htmlbody'])
                self.assertIn(collapse(body), shown)
                self.assertIn(quote_label, shown)
                self.assertIn(collapse(quote), shown)
                # CHANGED AT T3b (C3): the host was the actor's request host
                # (build_absolute_uri, CHANGE_EMAIL_HOST); it is APP_BASE_URL now.
                self.assertIn(f'href="{BASE_URL}{link}"', mail['htmlbody'])
                self.assertNotIn(CHANGE_EMAIL_HOST, mail['htmlbody'])

                rows = [row for row in self.logs if row.recipient_id == person.pk]
                self.assertEqual(sorted((r.channel, r.status) for r in rows),
                                 [('email', 'sent'), ('in_app', 'sent')])
        for row in self.logs:
            self.assertEqual((row.template_name, row.related_project_id, row.actor_id),
                             (label, site.pk, actor.pk), (row.channel, row.recipient))


class RaiseGoldenTests(ChangeGoldenBase):

    def test_raised_by_the_pm(self):
        site, _ = self._released('GB-R1')
        reason = 'Client moved the array.\nAnd added a second inverter.'
        self.act(lambda: self._raise(self.pm, site, reason=reason))
        body = ('GB-R1: a design change request is waiting for the Design Head. cw_pm (PM) '
                'raised it on attempt 1.')
        self.assert_change_sent(
            site, 'design_change_request_raised', self.pm,
            'GB-R1: design change request raised',
            {self.head:  (body, self.qc_review(site), 'Reason', reason),
             self.coord: (body, self.form(site), 'Reason', reason)})

    def test_raised_during_review_says_the_review_is_suspended(self):
        site, _ = self._pre_release('GB-R2')
        self.act(lambda: self._raise(self.pm, site, reason='Move it.'))
        body = ('GB-R2: a design change request is waiting for the Design Head. cw_pm (PM) '
                'raised it on attempt 1. The review in progress is suspended until it is '
                'decided.')
        self.assert_change_sent(
            site, 'design_change_request_raised', self.pm,
            'GB-R2: design change request raised',
            {self.head:  (body, self.qc_review(site), 'Reason', 'Move it.'),
             self.coord: (body, self.form(site), 'Reason', 'Move it.')})

    def test_raised_by_scm_with_no_pm_goes_to_the_heads(self):
        site, _ = self._pm_less('GB-R3')
        self.act(lambda: self._raise(self.scm, site, reason='Wrong cable size.'))
        body = ('GB-R3: a BOQ change request is waiting for the Design Head. cw_scm (SCM) '
                f'raised it on attempt 1. It skipped the PM stage: {NO_PM_FALLBACK_TEXT}.')
        self.assert_change_sent(
            site, 'design_change_request_raised', self.scm,
            'GB-R3: BOQ change request raised',
            {self.head: (body, self.qc_review(site), 'Reason', 'Wrong cable size.')})

    def test_raised_by_scm_with_the_pm(self):
        site, _ = self._released('GB-R4')
        self.act(lambda: self._raise(self.scm, site, reason='Wrong cable size.'))
        body = ("GB-R4: a BOQ change request is waiting for the site's PM. cw_scm (SCM) "
                'raised it on attempt 1. Forward it to the Design Head or reject it.')
        self.assert_change_sent(
            site, 'design_change_request_raised_with_pm', self.scm,
            'GB-R4: BOQ change request raised',
            {self.pm:    (body, self.form(site), 'Reason', 'Wrong cable size.'),
             self.coord: (body, self.form(site), 'Reason', 'Wrong cable size.')})


class PmStageGoldenTests(ChangeGoldenBase):

    REASON = 'Client moved the array to the north shed.'

    def test_forwarded(self):
        site, _, change = self._with_pm('GB-F1')
        note = 'Quantities need the Head — forwarding.'
        self.act(lambda: self._forward(change, note=note))
        to_head = ('GB-F1: a BOQ change request is waiting for the Design Head. cw_scm '
                   '(SCM) raised it on attempt 1 and cw_pm (PM) forwarded it.')
        to_requester = ('GB-F1: cw_pm (PM) forwarded your BOQ change request on attempt 1 '
                        'to the Design Head. No new attempt has been opened.')
        self.assert_change_sent(
            site, 'design_change_request_forwarded', self.pm,
            'GB-F1: BOQ change request forwarded to the Design Head',
            {self.head: (to_head, self.qc_review(site), 'Request', self.REASON),
             self.scm:  (to_requester, self.form(site), 'PM note', note)})

    def test_rejected_by_the_pm(self):
        site, _, change = self._with_pm('GB-P1')
        note = 'Not needed — the BOQ is right.'
        self.act(lambda: self._pm_reject(change, note=note))
        body = ('GB-P1: cw_pm (PM) rejected your BOQ change request on attempt 1. It did not '
                'reach the Design Head and nothing was reopened.')
        self.assert_change_sent(
            site, 'design_change_request_pm_rejected', self.pm,
            'GB-P1: BOQ change request rejected by the PM',
            {self.scm: (body, self.form(site), 'PM note', note)})

    def test_withdrawn_by_the_requester(self):
        site, _, change = self._with_pm('GB-W1')
        note = 'Raised in error — withdrawing.'
        self.act(lambda: self._withdraw(change, note=note))
        body = ('GB-W1: cw_scm (SCM) withdrew the BOQ change request they raised on attempt '
                '1. It is no longer waiting for you.')
        self.assert_change_sent(
            site, 'design_change_request_withdrawn', self.scm,
            'GB-W1: BOQ change request withdrawn',
            {self.pm:    (body, self.form(site), 'Withdrawal note', note),
             self.coord: (body, self.form(site), 'Withdrawal note', note)})

    def test_withdrawn_by_another_scm_user(self):
        site, _, change = self._with_pm('GB-W2')
        note = 'Raised in error — withdrawing.'
        self.act(lambda: self._withdraw(change, profile=self.scm2, note=note))
        body = ('GB-W2: cr_scm2 (SCM) withdrew the BOQ change request cw_scm raised on '
                'attempt 1. It is no longer waiting for you.')
        self.assert_change_sent(
            site, 'design_change_request_withdrawn', self.scm2,
            'GB-W2: BOQ change request withdrawn',
            {self.pm:    (body, self.form(site), 'Withdrawal note', note),
             self.coord: (body, self.form(site), 'Withdrawal note', note)})


class HeadDecisionGoldenTests(ChangeGoldenBase):

    REASON = 'Client moved the array to the north shed.'

    def test_accepted_scm_request(self):
        site, assignment, change = self._pending_scm('GB-A1')
        self.act(lambda: self._accept(change))
        mine = ('GB-A1: cw_head (Design Head) accepted your BOQ change request. Attempt 2 is '
                'open and the site is back with the designer.')
        theirs = ('GB-A1: cw_head (Design Head) accepted the BOQ change request cw_scm '
                  'raised. Attempt 2 is open and the site is back with the designer.')
        self.assert_change_sent(
            site, 'design_change_request_accepted', self.head,
            'GB-A1: BOQ change request accepted — attempt 2 opened',
            {self.scm:      (mine, self.form(site), 'Request', self.REASON),
             self.pm:       (theirs, self.form(site), 'Request', self.REASON),
             self.coord:    (theirs, self.form(site), 'Request', self.REASON),
             self.designer: (theirs, self.workspace(site), 'Request', self.REASON)})

    def test_accepted_pm_request_on_a_draft_group_site_tells_the_group_owner(self):
        site, assignment = self._released('GB-A2')
        self._group('Batch G', [site])
        self._raise(self.pm, site)
        change = self._requests(assignment).get()
        self.act(lambda: self._accept(change))
        tail = (' Attempt 2 is open and the site is back with the designer. The site left '
                'procurement group "Batch G".')
        mine = 'GB-A2: cw_head (Design Head) accepted your design change request.' + tail
        theirs = ('GB-A2: cw_head (Design Head) accepted the design change request cw_pm '
                  'raised.' + tail)
        self.assert_change_sent(
            site, 'design_change_request_accepted', self.head,
            'GB-A2: design change request accepted — attempt 2 opened',
            {self.pm:       (mine, self.form(site), 'Request', self.REASON),
             self.coord:    (theirs, self.form(site), 'Request', self.REASON),
             self.designer: (theirs, self.workspace(site), 'Request', self.REASON),
             self.scm:      (theirs, self.form(site), 'Request', self.REASON)})

    def test_rejected_pm_request(self):
        site, assignment = self._released('GB-J1')
        self._raise(self.pm, site)
        change = self._requests(assignment).get()
        reason = 'The tender fixes the inverter make.'
        self.act(lambda: self._reject(change, reason=reason))
        body = ('GB-J1: cw_head (Design Head) rejected your design change request on attempt '
                '1. The current design stands and no new attempt was opened.')
        self.assert_change_sent(
            site, 'design_change_request_rejected', self.head,
            'GB-J1: design change request rejected',
            {self.pm: (body, self.form(site), 'Reason', reason)})

    def test_rejected_scm_request(self):
        site, _, change = self._pending_scm('GB-J2')
        reason = 'The tender fixes the inverter make.'
        self.act(lambda: self._reject(change, reason=reason))
        tail = ' on attempt 1. The current design stands and no new attempt was opened.'
        mine = 'GB-J2: cw_head (Design Head) rejected your BOQ change request' + tail
        theirs = ('GB-J2: cw_head (Design Head) rejected the BOQ change request cw_scm '
                  'raised' + tail)
        self.assert_change_sent(
            site, 'design_change_request_rejected', self.head,
            'GB-J2: BOQ change request rejected',
            {self.scm:   (mine, self.form(site), 'Reason', reason),
             self.pm:    (theirs, self.form(site), 'Reason', reason),
             self.coord: (theirs, self.form(site), 'Reason', reason)})

    def test_corrected(self):
        site, _, change = self._pending_scm('GB-C1')
        self._boq_correct(site, 12)
        note = 'Raised the module count to 12.'
        self.act(lambda: self._correct(change, note=note))
        tail = (' on attempt 1 as corrected in the BOQ. No new attempt was opened and the '
                'design did not move. Changed: CW-001 10 → 12.')
        mine = 'GB-C1: cw_head (Design Head) recorded your BOQ change request' + tail
        theirs = ('GB-C1: cw_head (Design Head) recorded the BOQ change request cw_scm '
                  'raised' + tail)
        self.assert_change_sent(
            site, 'design_change_request_corrected', self.head,
            'GB-C1: BOQ change request recorded as corrected in the BOQ',
            {self.scm:   (mine, self.form(site), 'Note', note),
             self.pm:    (theirs, self.form(site), 'Note', note),
             self.coord: (theirs, self.form(site), 'Note', note)})


# ===========================================================================
# After the move
# ===========================================================================

class _Rollback(Exception):
    pass


def _admin(username='gb_admin'):
    user = User.objects.create_user(username=username, password='x')
    profile = user.profile
    profile.role = 'Admin'
    profile.save()
    return profile


class AfterCommitAssignTests(AssignGoldenBase):
    """A2: the cooldown decision and every channel run in one after-commit callback."""

    def test_a_rolled_back_assignment_sends_nothing_and_is_not_counted(self):
        """Before T3b the count, the in-app row and the WhatsApp and email HTTP calls all
        happened inside the transaction; a rollback removed the rows but not the messages
        already sent."""
        task = _task(self.alpha, 'Task 1')

        def rolled_back():
            try:
                with transaction.atomic():
                    assign_task_to(task, self.se, notify=True, actor=self.pm)
                    raise _Rollback
            except _Rollback:
                pass

        self.act(rolled_back)
        self.assert_nothing_sent()
        task.refresh_from_db()
        self.assertIsNone(task.assigned_to)

        # Nothing was counted: the next assignment is the first of the window.
        self.assign(task, self.se)
        self.assert_single(self.se, self.pm, task)

    def test_a_paused_event_skips_email_and_still_sends_in_app_and_whatsapp(self):
        A.set_state('task_assigned', 'email', False, _admin(), 'Wording under review')
        self.assign(_task(self.alpha, 'Task 1'), self.se)
        self.assertEqual(self.mail_payloads, [])
        self.assertEqual(list(self.in_app()), [self.se])
        self.assertEqual(list(self.whatsapps()), [self.se.phone_number])
        email = [row for row in self.logs if row.channel == 'email']
        self.assertEqual(
            [(r.status, r.error_detail, r.template_name, r.event_code) for r in email],
            [('skipped', 'event paused', 'assign_task', 'task_assigned')])

        # The cooldown still counts the in-app row: the second is the bulk message, sent
        # in full, because tasks_assigned_bulk is a separate event and is not paused.
        self.assign(_task(self.alpha, 'Task 2'), self.se)
        self.assert_bulk(self.se, self.pm, self.alpha, 2)

    def test_log_rows_carry_the_event_code_except_the_whatsapp_leg(self):
        self.assign(_task(self.alpha, 'Task 1'), self.se)
        self.assertEqual({(r.channel, r.event_code) for r in self.logs},
                         {('in_app', 'task_assigned'), ('email', 'task_assigned'),
                          ('whatsapp', '')})


class AfterCommitChangeTests(ChangeGoldenBase):

    def test_a_paused_change_request_event_skips_email_and_still_sends_in_app(self):
        A.set_state('change_request_raised', 'email', False, _admin(),
                    'Wording under review')
        site, _ = self._released('GB-X1')
        self.act(lambda: self._raise(self.pm, site))
        self.assertEqual(self.mail_payloads, [])
        self.assertEqual(set(self.in_app()), {self.head, self.coord})
        self.assertEqual(
            {(r.recipient, r.status, r.error_detail) for r in self.logs
             if r.channel == 'email'},
            {(self.head, 'skipped', 'event paused'), (self.coord, 'skipped', 'event paused')})

    def test_the_forward_sends_two_events_under_one_label(self):
        site, _, change = self._with_pm('GB-X2')
        self.act(lambda: self._forward(change))
        self.assertEqual(
            {(r.recipient, r.event_code, r.template_name) for r in self.logs},
            {(self.head, 'change_request_forwarded_head', 'design_change_request_forwarded'),
             (self.scm, 'change_request_forwarded_requester',
              'design_change_request_forwarded')})

    def test_nothing_is_sent_before_commit(self):
        site, _ = self._released('GB-X3')
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            mark = Notification.objects.count()
            self._raise(self.pm, site)
            self.assertEqual(Notification.objects.count(), mark)
        self.assertEqual(len(callbacks), 2, 'one per recipient: the Head and the coordinator')


class WhatsAppSeparatelyTests(TestCase):
    """P2: events whose call site also sends WhatsApp say so on the Admin screens."""

    TEXT = "This event's WhatsApp message is sent separately and is not paused here."
    FLAGGED = {'issue_raised', 'issue_resolved', 'project_pm_assigned', 'boq_acknowledged',
               'payment_milestone_reached', 'task_assigned', 'tasks_assigned_bulk'}

    def setUp(self):
        self.client = Client(SERVER_NAME='localhost')
        self.client.force_login(_admin().user)

    def test_the_flag_is_on_exactly_the_events_that_send_whatsapp_themselves(self):
        self.assertEqual({code for code, event in NOTIFICATION_EVENTS.items()
                          if event.whatsapp_sent_separately}, self.FLAGGED)

    def test_the_event_page_says_so_only_on_flagged_events(self):
        for code in NOTIFICATION_EVENTS:
            with self.subTest(event=code):
                response = self.client.get(reverse('admin_notification_event', args=[code]))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(f'Email only. {self.TEXT}' in response.content.decode(),
                                 code in self.FLAGGED)

    def test_the_list_says_so_once_per_flagged_event(self):
        content = self.client.get(reverse('admin_notification_events')).content.decode()
        self.assertEqual(content.count(self.TEXT), len(self.FLAGGED))


class SeedNewEventsTests(TestCase):
    """The seed command creates version 1 for the T3b events and skips the T3a ones."""

    NEW = {'task_assigned', 'tasks_assigned_bulk', 'change_request_raised',
           'change_request_raised_with_pm', 'change_request_forwarded_head',
           'change_request_forwarded_requester', 'change_request_pm_rejected',
           'change_request_withdrawn', 'change_request_accepted', 'change_request_rejected',
           'change_request_corrected'}

    def _run(self, *args):
        out = StringIO()
        call_command('seed_notification_templates', '--as', 'gb_admin', *args, stdout=out)
        return out.getvalue()

    def test_a_run_after_t3a_seeds_only_the_new_events(self):
        _admin()
        old = [code for code in NOTIFICATION_EVENTS if code not in self.NEW]
        for code in old:
            self._run('--event', code)
        self.assertEqual(set(NotificationTemplate.objects.values_list('event_code', flat=True)),
                         set(old))

        created = {line.split()[3].rstrip(':') for line in self._run('--dry-run').splitlines()
                   if line.startswith('[dry run] create')}
        self.assertEqual(created, self.NEW)

        self._run()
        after = NotificationTemplate.objects.exclude(event_code__in=old)
        self.assertEqual(set(after.values_list('event_code', flat=True)), self.NEW)
        for row in after:
            event = NOTIFICATION_EVENTS[row.event_code]
            self.assertEqual((row.version_no, row.status, row.subject, row.body),
                             (1, NotificationTemplate.ACTIVE, event.default_subject,
                              event.default_body))
