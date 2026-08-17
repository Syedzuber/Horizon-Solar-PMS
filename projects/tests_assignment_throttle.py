"""
Tests for the assignment notification throttle.

Two things are under test:

  1. assign_task_to() / assign_tasks_to() — the chokepoint where Task.assigned_to
     is written and the notification decision is made. `notify` defaults to False
     because seven of the nine write sites are silent today and must stay silent.

  2. The per-recipient, per-project, 1-hour cooldown: 1st assignment sends
     assign_task, 2nd sends assign_tasks_bulk with the count, 3rd and beyond send
     nothing until the window expires.

The bulk paths (activation, assign_design) send nothing today and the tests here
assert they still send nothing — that is the regression this session most needs
to be protected against, alongside "a single manual assignment still notifies".

Interakt is never called: SystemSettings.whatsapp_enabled stays False, so
send_notification() records a 'skipped' row for WhatsApp. The cooldown counts the
in_app channel, which always logs regardless of switches or preferences, so the
throttle behaves identically either way — asserted directly in
CooldownCountingTests. The test Client uses SERVER_NAME='localhost' to satisfy
the env-driven ALLOWED_HOSTS.
"""
import datetime
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase, Client
from django.utils import timezone

from .models import (Project, ProjectPhase, Task, UserProfile, NotificationLog,
                     SystemSettings)
from .utils import (assign_task_to, assign_tasks_to, attach_residential_template,
                    ASSIGN_COOLDOWN_WINDOW, ASSIGN_CIRCUIT_BREAKER_ROWS,
                    RESIDENTIAL_FINANCE_ASSIGNEE_EMAIL)


def _make_user(username, role, email=''):
    user = User.objects.create_user(username=username, password='pw12345',
                                    email=email, first_name=username.title())
    profile = user.profile          # auto-created by post_save signal
    profile.role = role
    profile.phone_number = '9876543210'
    profile.save(update_fields=['role', 'phone_number'])
    return user, profile


def _make_project(pm, name='Acme'):
    return Project.objects.create(
        customer_name=name, customer_phone='9876543210', site_address='1 Sun Rd',
        city='Lucknow', project_type='Residential', capacity_kw=Decimal('3.00'),
        contract_value=Decimal('100000.00'),
        target_commissioning_date=datetime.date(2026, 12, 1),
        status='Active', assigned_pm=pm,
    )


def _task(project, name='Survey', role=Task.SITE_ENGINEER, order=1):
    phase, _ = ProjectPhase.objects.get_or_create(
        project=project, phase_name='Phase 1', defaults={'phase_order': 1})
    return Task.objects.create(phase=phase, task_name=name, task_order=order,
                               assigned_role=role)


def _sends(recipient=None, template=None):
    """Notification EVENTS, not log rows — one send writes one row per channel."""
    qs = NotificationLog.objects.filter(channel='in_app')
    if recipient is not None:
        qs = qs.filter(recipient=recipient)
    if template is not None:
        qs = qs.filter(template_name=template)
    return qs.count()


class Base(TestCase):
    def setUp(self):
        SystemSettings.objects.all().delete()      # master switches default False
        _, self.pm = _make_user('pm', 'PM')
        _, self.se = _make_user('se', 'Site Engineer')
        _, self.se2 = _make_user('se2', 'Site Engineer')
        self.projA = _make_project(self.pm, 'Alpha')
        self.projB = _make_project(self.pm, 'Bravo')


# ---------------------------------------------------------------------------
# The chokepoint itself
# ---------------------------------------------------------------------------

class ChokepointTests(Base):

    def test_notify_defaults_to_false(self):
        """The whole session hinges on this: routing a silent path through the
        chokepoint must not make it noisy."""
        task = _task(self.projA)
        self.assertTrue(assign_task_to(task, self.se))
        task.refresh_from_db()
        self.assertEqual(task.assigned_to, self.se)
        self.assertEqual(NotificationLog.objects.count(), 0)

    def test_notify_true_sends(self):
        task = _task(self.projA)
        assign_task_to(task, self.se, notify=True, actor=self.pm)
        self.assertEqual(_sends(self.se, 'assign_task'), 1)

    def test_reassigning_the_same_person_is_a_no_op(self):
        """Replaces the old 10-second double-submit guard."""
        task = _task(self.projA)
        assign_task_to(task, self.se, notify=True, actor=self.pm)
        before = NotificationLog.objects.count()

        self.assertFalse(assign_task_to(task, self.se, notify=True, actor=self.pm))
        self.assertEqual(NotificationLog.objects.count(), before)

    def test_unassign_never_notifies(self):
        task = _task(self.projA)
        assign_task_to(task, self.se)
        assign_task_to(task, None, notify=True, actor=self.pm)
        task.refresh_from_db()
        self.assertIsNone(task.assigned_to)
        self.assertEqual(NotificationLog.objects.count(), 0)

    def test_bulk_helper_is_structurally_silent(self):
        tasks = [_task(self.projA, f'T{i}', order=i) for i in range(5)]
        n = assign_tasks_to(Task.objects.filter(pk__in=[t.pk for t in tasks]), self.se)
        self.assertEqual(n, 5)
        self.assertEqual(NotificationLog.objects.count(), 0)


# ---------------------------------------------------------------------------
# The cooldown
# ---------------------------------------------------------------------------

class CooldownTests(Base):

    def _assign(self, project, n, user=None):
        user = user or self.se
        task = _task(project, f'Task {n}', order=n)
        assign_task_to(task, user, notify=True, actor=self.pm)
        return task

    def test_first_second_third(self):
        self._assign(self.projA, 1)
        self.assertEqual(_sends(self.se, 'assign_task'), 1)
        self.assertEqual(_sends(self.se, 'assign_tasks_bulk'), 0)

        self._assign(self.projA, 2)
        self.assertEqual(_sends(self.se, 'assign_task'), 1)
        self.assertEqual(_sends(self.se, 'assign_tasks_bulk'), 1)

        for n in range(3, 8):
            self._assign(self.projA, n)
        self.assertEqual(_sends(self.se), 2, 'never more than two per window')

    def test_third_assignment_is_still_written(self):
        """Suppressing the message must not suppress the assignment."""
        self._assign(self.projA, 1)
        self._assign(self.projA, 2)
        task = self._assign(self.projA, 3)
        task.refresh_from_db()
        self.assertEqual(task.assigned_to, self.se)

    def test_bulk_message_carries_the_count(self):
        self._assign(self.projA, 1)
        self._assign(self.projA, 2)
        row = NotificationLog.objects.get(template_name='assign_tasks_bulk',
                                          channel='in_app')
        self.assertIn('2 tasks', row.message)

    def test_bulk_message_links_to_the_project_not_a_task(self):
        self._assign(self.projA, 1)
        self._assign(self.projA, 2)
        note = self.se.notifications.filter(message__contains='2 tasks').first()
        self.assertEqual(note.link, f'/projects/{self.projA.project_id}/overview/')

    def test_cooldown_does_not_cross_projects(self):
        self._assign(self.projA, 1)
        self._assign(self.projA, 2)
        self._assign(self.projA, 3)          # suppressed on A
        self.assertEqual(_sends(self.se), 2)

        self._assign(self.projB, 1)          # different project — must get through
        self.assertEqual(_sends(self.se), 3)
        self.assertEqual(
            NotificationLog.objects.filter(related_project=self.projB,
                                           channel='in_app').count(), 1)

    def test_cooldown_does_not_cross_recipients(self):
        self._assign(self.projA, 1)
        self._assign(self.projA, 2)
        self._assign(self.projA, 3, user=self.se2)
        self.assertEqual(_sends(self.se2, 'assign_task'), 1)

    def test_window_expiry_restores_the_normal_message(self):
        self._assign(self.projA, 1)
        self._assign(self.projA, 2)
        self.assertEqual(_sends(self.se), 2)

        # Age every existing row past the window.
        NotificationLog.objects.update(
            created_at=timezone.now() - ASSIGN_COOLDOWN_WINDOW - datetime.timedelta(minutes=1))

        self._assign(self.projA, 3)
        self.assertEqual(_sends(self.se, 'assign_task'), 2,
                         'a fresh window starts again at assign_task')

    def test_circuit_breaker_aborts_the_send(self):
        task = _task(self.projA)
        for i in range(ASSIGN_CIRCUIT_BREAKER_ROWS):
            NotificationLog.objects.create(
                recipient=self.se, related_project=self.projA, channel='whatsapp',
                status='sent', message='x', template_name='assign_task')
        before = NotificationLog.objects.count()
        assign_task_to(task, self.se, notify=True, actor=self.pm)

        task.refresh_from_db()
        self.assertEqual(task.assigned_to, self.se, 'assignment still happens')
        self.assertEqual(NotificationLog.objects.count(), before, 'but nothing is sent')


class CooldownCountingTests(Base):
    """The cooldown counts notification events, never delivery outcomes."""

    def test_recipient_with_whatsapp_off_is_still_throttled(self):
        self.se.whatsapp_notifications = False
        self.se.email_notifications = False
        self.se.save(update_fields=['whatsapp_notifications', 'email_notifications'])

        for n in range(1, 4):
            assign_task_to(_task(self.projA, f'T{n}', order=n), self.se,
                           notify=True, actor=self.pm)

        self.assertEqual(_sends(self.se, 'assign_task'), 1)
        self.assertEqual(_sends(self.se, 'assign_tasks_bulk'), 1)

    def test_one_send_writes_one_in_app_row_per_event(self):
        """The invariant the cooldown count rests on."""
        assign_task_to(_task(self.projA), self.se, notify=True, actor=self.pm)
        self.assertEqual(
            NotificationLog.objects.filter(channel='in_app').count(), 1)
        self.assertEqual(NotificationLog.objects.count(), 3,
                         'one row per channel: in_app + whatsapp + email')


# ---------------------------------------------------------------------------
# The paths that are silent today and must stay silent
# ---------------------------------------------------------------------------

class SilentPathTests(Base):

    def test_activation_sends_nothing(self):
        _make_user('finance', 'Finance', email=RESIDENTIAL_FINANCE_ASSIGNEE_EMAIL)
        draft = _make_project(self.pm, 'Charlie')
        ProjectPhase.objects.filter(project=draft).delete()

        attach_residential_template(draft)

        assigned = Task.objects.filter(phase__project=draft, assigned_to__isnull=False)
        self.assertEqual(assigned.count(), 20)
        self.assertEqual(len(set(assigned.values_list('assigned_to', flat=True))), 2)
        self.assertEqual(NotificationLog.objects.count(), 0,
                         'activation pre-assigns 20 tasks and says nothing — unchanged')

    def test_assign_design_bulk_sends_nothing(self):
        _, designer = _make_user('designer', 'Design')
        for i in range(6):
            _task(self.projA, f'Design {i}', role=Task.DESIGN, order=i)

        n = assign_tasks_to(
            Task.objects.filter(phase__project=self.projA, assigned_role=Task.DESIGN),
            designer)

        self.assertEqual(n, 6)
        self.assertEqual(NotificationLog.objects.count(), 0)


# ---------------------------------------------------------------------------
# End-to-end through the real views
# ---------------------------------------------------------------------------

class ViewTests(Base):

    def setUp(self):
        super().setUp()
        self.client = Client(SERVER_NAME='localhost')
        self.client.force_login(self.pm.user)

    def _post_assign(self, project, task, profile):
        return self.client.post(
            f'/projects/{project.project_id}/tasks/{task.pk}/assign/',
            {'assigned_to': str(profile.pk)})

    def test_single_manual_assignment_still_notifies(self):
        """The regression this session is most likely to cause."""
        task = _task(self.projA)
        resp = self._post_assign(self.projA, task, self.se)

        self.assertEqual(resp.status_code, 302)
        task.refresh_from_db()
        self.assertEqual(task.assigned_to, self.se)
        self.assertEqual(_sends(self.se, 'assign_task'), 1)

    def test_second_then_third_through_the_view(self):
        for n in range(1, 4):
            self._post_assign(self.projA, _task(self.projA, f'T{n}', order=n), self.se)

        self.assertEqual(_sends(self.se, 'assign_task'), 1)
        self.assertEqual(_sends(self.se, 'assign_tasks_bulk'), 1)
        self.assertEqual(_sends(self.se), 2)

    def test_clearing_an_assignment_through_the_view_notifies_nobody(self):
        task = _task(self.projA)
        self._post_assign(self.projA, task, self.se)
        before = NotificationLog.objects.count()

        self.client.post(f'/projects/{self.projA.project_id}/tasks/{task.pk}/assign/',
                         {'assigned_to': ''})

        task.refresh_from_db()
        self.assertIsNone(task.assigned_to)
        self.assertEqual(NotificationLog.objects.count(), before)
