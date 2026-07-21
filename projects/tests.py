"""
Tests for the post-activation project field-edit path (views.project_field_edit).

Covers the spec's pre-flight simulation: capacity/target-date/contract edits on a
non-Draft project apply immediately, write one ProjectFieldEditLog row per CHANGED
field, never touch task due dates (no cascade), block contract_value once milestone
amounts exist, reject Draft projects, and are gated by user_can_manage_project().

The test Client is created with SERVER_NAME='localhost' so requests pass the
env-driven ALLOWED_HOSTS check.
"""
from datetime import date, timedelta
from decimal import Decimal

from django.test import TestCase, Client
from django.contrib.auth.models import User

from .models import (
    Project, ProjectPhase, Task, UserProfile, PaymentMilestone, ProjectFieldEditLog,
)


def _make_user(username, role):
    # A post_save signal on User auto-creates the UserProfile — fetch and set its role
    # rather than creating a second one (which would violate the OneToOne constraint).
    user = User.objects.create_user(username=username, password='pw12345')
    profile = user.profile
    profile.role = role
    profile.save(update_fields=['role'])
    return user, profile


class PostActivationFieldEditTests(TestCase):
    def setUp(self):
        self.client = Client(SERVER_NAME='localhost')

        self.pm_user, self.pm = _make_user('pm1', 'PM')
        self.coord_user, self.coord = _make_user('coord1', 'Project Coordinator')
        self.other_user, self.other = _make_user('pm2', 'PM')  # a PM who does NOT own the project

        self.project = Project.objects.create(
            customer_name='Acme',
            customer_phone='9876543210',
            site_address='1 Sun Rd',
            city='Lucknow',
            project_type='Residential',
            capacity_kw=Decimal('3.00'),
            contract_value=Decimal('100000.00'),
            target_commissioning_date=date(2026, 12, 1),
            status='Active',
            assigned_pm=self.pm,
        )
        self.project.coordinators.add(self.coord)

        # One internal task with a fixed due date — used to prove no cascade recalc.
        self.phase = ProjectPhase.objects.create(
            project=self.project, phase_name='Phase 1', phase_order=1,
        )
        self.task = Task.objects.create(
            phase=self.phase, task_name='Survey', task_order=1,
            assigned_role=Task.PM, task_type=Task.INTERNAL,
            due_date=date(2026, 6, 1),
        )

        self.url = f'/projects/{self.project.project_id}/fields/edit/'

    def _post(self, data, hx=True):
        headers = {'HTTP_HX_REQUEST': 'true'} if hx else {}
        return self.client.post(self.url, data, **headers)

    # 1 — PM edits capacity; audit row written, value saved, no cascade.
    def test_pm_edits_capacity(self):
        self.client.login(username='pm1', password='pw12345')
        resp = self._post({
            'capacity_kw': '5.00',
            'contract_value': '100000.00',
            'target_commissioning_date': '2026-12-01',
            'reason': 'As-built revision',
        })
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp['HX-Trigger'], 'fieldEditDone')

        self.project.refresh_from_db()
        self.assertEqual(self.project.capacity_kw, Decimal('5.00'))

        logs = ProjectFieldEditLog.objects.filter(project=self.project)
        self.assertEqual(logs.count(), 1)
        log = logs.first()
        self.assertEqual(log.field_name, 'capacity_kw')
        self.assertEqual(log.old_value, '3.00')
        self.assertEqual(log.new_value, '5.00')
        self.assertEqual(log.edited_by, self.pm)
        self.assertEqual(log.reason, 'As-built revision')

        # No cascade — the task's due date is untouched.
        self.task.refresh_from_db()
        self.assertEqual(self.task.due_date, date(2026, 6, 1))

    # 2 — Coordinator edits target date; field saved, task dates unchanged, logged.
    def test_coordinator_edits_target_date(self):
        self.client.login(username='coord1', password='pw12345')
        new_date = date(2026, 12, 15)
        resp = self._post({
            'capacity_kw': '3.00',
            'contract_value': '100000.00',
            'target_commissioning_date': new_date.isoformat(),
        })
        self.assertEqual(resp.status_code, 200)

        self.project.refresh_from_db()
        self.assertEqual(self.project.target_commissioning_date, new_date)

        logs = ProjectFieldEditLog.objects.filter(project=self.project)
        self.assertEqual(logs.count(), 1)
        self.assertEqual(logs.first().field_name, 'target_commissioning_date')
        self.assertEqual(logs.first().edited_by, self.coord)

        self.task.refresh_from_db()
        self.assertEqual(self.task.due_date, date(2026, 6, 1))

    # 3 — Contract value edit blocked when a milestone amount already exists.
    def test_contract_value_blocked_when_milestone_amount_set(self):
        PaymentMilestone.objects.create(
            project=self.project, milestone_name='M1', amount=Decimal('40000.00'),
        )
        self.client.login(username='pm1', password='pw12345')
        resp = self._post({
            'capacity_kw': '3.00',
            'contract_value': '250000.00',   # attempt to change
            'target_commissioning_date': '2026-12-01',
        })
        # Invalid form re-renders the modal (no HX-Trigger to close it).
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn('HX-Trigger', resp)
        self.assertContains(resp, 'cannot be changed after payment milestone amounts are set')

        self.project.refresh_from_db()
        self.assertEqual(self.project.contract_value, Decimal('100000.00'))  # unchanged
        self.assertEqual(ProjectFieldEditLog.objects.count(), 0)

    # 4 — Contract value edit allowed when no milestone amounts are set.
    def test_contract_value_allowed_without_milestone_amounts(self):
        # A milestone row exists but with a NULL amount — not yet "set".
        PaymentMilestone.objects.create(project=self.project, milestone_name='M1', amount=None)
        self.client.login(username='pm1', password='pw12345')
        resp = self._post({
            'capacity_kw': '3.00',
            'contract_value': '250000.00',
            'target_commissioning_date': '2026-12-01',
        })
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp['HX-Trigger'], 'fieldEditDone')

        self.project.refresh_from_db()
        self.assertEqual(self.project.contract_value, Decimal('250000.00'))
        logs = ProjectFieldEditLog.objects.filter(field_name='contract_value')
        self.assertEqual(logs.count(), 1)
        self.assertEqual(logs.first().new_value, '250000.00')

    # 5 — No-op submit (same values) writes zero log rows.
    def test_noop_edit_writes_no_log(self):
        self.client.login(username='pm1', password='pw12345')
        resp = self._post({
            'capacity_kw': '3.00',
            'contract_value': '100000.00',
            'target_commissioning_date': '2026-12-01',
        })
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(ProjectFieldEditLog.objects.count(), 0)

    # 6 — Draft project via this endpoint is rejected, unchanged, unlogged.
    def test_draft_project_rejected(self):
        self.project.status = 'Draft'
        self.project.save(update_fields=['status'])
        self.client.login(username='pm1', password='pw12345')
        resp = self._post({
            'capacity_kw': '9.00',
            'contract_value': '100000.00',
            'target_commissioning_date': '2026-12-01',
        })
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn('HX-Trigger', resp)  # not the success response
        self.project.refresh_from_db()
        self.assertEqual(self.project.capacity_kw, Decimal('3.00'))  # unchanged
        self.assertEqual(ProjectFieldEditLog.objects.count(), 0)

    # 7 — A user who is neither the PM nor a coordinator gets a 404.
    def test_non_manager_forbidden(self):
        self.client.login(username='pm2', password='pw12345')
        resp = self._post({
            'capacity_kw': '9.00',
            'contract_value': '100000.00',
            'target_commissioning_date': '2026-12-01',
        })
        self.assertEqual(resp.status_code, 404)
        self.project.refresh_from_db()
        self.assertEqual(self.project.capacity_kw, Decimal('3.00'))
        self.assertEqual(ProjectFieldEditLog.objects.count(), 0)

    # GET returns the modal body prefilled.
    def test_get_returns_modal(self):
        self.client.login(username='pm1', password='pw12345')
        resp = self.client.get(self.url, HTTP_HX_REQUEST='true')
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Edit Project Fields')
