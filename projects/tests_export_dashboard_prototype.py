"""Tests for the read-only `export_dashboard_prototype` command (dashboard prototype data)."""
import json
import os
import tempfile
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from .models import (
    SUBJECT_TASK, ActivityLog, Issue, Program, Project, ProjectPhase, StatusTransition, Task,
)


class ExportDashboardPrototypeTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.pm = User.objects.create_user(username='pm1', password='x', first_name='Chetan').profile
        cls.pm.role = 'PM'
        cls.pm.save(update_fields=['role'])
        cls.tender = Program.objects.create(
            name='IPGCL Delhi Tender', program_type='OPEX', client_name='IPGCL',
            status='Active', short_tender_code='IPGCL')

        def site(pid, status, **extra):
            return Project.objects.create(
                project_id=pid, customer_name='Secret Name', customer_phone='9876543210',
                site_address='1 Secret Road', city='Delhi', project_type='OPEX',
                program=cls.tender, dc_capacity_kw=Decimal('50.00'), status=status,
                assigned_pm=cls.pm, site_code=pid[-4:], **extra)

        cls.draft = site('IPGCL-1901', 'Draft')
        cls.live = site('IPGCL-2213', 'Active', activated_at=timezone.now() - timedelta(days=17),
                        target_commissioning_date=date.today() + timedelta(days=60))
        ph = ProjectPhase.objects.create(project=cls.live, phase_name='Design', phase_order=1)
        cls.t_done = Task.objects.create(phase=ph, task_name='Arka layout', task_order=1,
                                         status=Task.DONE, completed_at=timezone.now() - timedelta(days=5),
                                         due_date=date.today() - timedelta(days=4), duration_days=3)
        cls.t_late = Task.objects.create(phase=ph, task_name='Design release', task_order=2,
                                         status=Task.IN_PROGRESS, due_date=date.today() - timedelta(days=1))
        StatusTransition.objects.create(
            subject_type=SUBJECT_TASK, subject_id=cls.t_done.pk, project=cls.live,
            from_status=Task.NOT_STARTED, to_status=Task.IN_PROGRESS, actor_role_code='PM',
            occurred_at=timezone.now() - timedelta(days=9))
        Issue.objects.create(project=cls.live, title='Roof access')
        ActivityLog.objects.create(project=cls.live, actor=cls.pm, action='Marked Arka layout Done',
                                   action_code='task_done')
        # A read must never count as the latest activity.
        ActivityLog.objects.create(project=cls.live, actor=cls.pm, action='Opened drawing',
                                   action_code='site_drawing_opened')

    def _export(self):
        fd, path = tempfile.mkstemp(suffix='.json')
        os.close(fd)
        try:
            call_command('export_dashboard_prototype', out=path, stdout=open(os.devnull, 'w'))
            with open(path, encoding='utf-8') as fh:
                return json.load(fh)
        finally:
            os.remove(path)

    def test_shape_and_derived_fields(self):
        data = self._export()
        sites = {s['project_id']: s for s in data['sites']}
        self.assertEqual(set(sites), {'IPGCL-1901', 'IPGCL-2213'})
        live = sites['IPGCL-2213']
        self.assertEqual(live['pm'], 'Chetan')
        self.assertEqual(live['progress'], 50)
        self.assertEqual(live['overdue'], 1)
        self.assertEqual(live['issues'], 1)
        self.assertEqual(live['phases'][0]['done'], 1)
        self.assertIsNotNone(live['phases'][0]['start'])
        self.assertEqual(live['last']['what'], 'Marked Arka layout Done')
        self.assertEqual(len(live['feed']), 1)
        self.assertEqual(data['programs'][0]['code'], 'IPGCL')

    def test_no_personal_data(self):
        raw = json.dumps(self._export())
        self.assertNotIn('Secret Name', raw)
        self.assertNotIn('9876543210', raw)
        self.assertNotIn('Secret Road', raw)
