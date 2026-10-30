"""
SCM dashboard: challans on a soft-deleted project are counted in neither
"Deliveries today" nor "Overdue".

A soft-deleted project keeps its status (usually Active), so dashboard_scm's
project__status__in filter alone let its challans through; both counters now also
filter project__is_deleted=False.

Run with:
    python manage.py test projects.tests_scm_deleted_dcs --settings=solarpms.test_settings
"""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from .models import DeliveryChallan, Project


def _profile(username, role):
    user = User.objects.create_user(username=username, password='x',
                                    first_name=username.title(), last_name='Test')
    profile = user.profile          # auto-created by the post_save signal
    profile.role = role
    profile.save(update_fields=['role'])
    return profile


def _project(project_id, is_deleted=False):
    return Project.objects.create(
        project_id=project_id, customer_name=project_id, customer_phone='9876543210',
        site_address='1 Sun Road', city='Delhi', project_type='Residential',
        dc_capacity_kw=Decimal('5.00'), status='Active', is_deleted=is_deleted,
        target_commissioning_date=timezone.localdate() + timedelta(days=90))


class ScmDeletedProjectChallanTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.scm = _profile('del_scm', 'SCM')
        # The view reads the IST date, so the fixture does too.
        today = timezone.localdate()
        # One challan due today and one overdue on each project; only the live
        # project's two may be counted.
        for project in (_project('DEL-LIVE'), _project('DEL-GONE', is_deleted=True)):
            for n, expected in ((1, today), (2, today - timedelta(days=3))):
                DeliveryChallan.objects.create(
                    project=project, dc_number=f'{project.project_id}-DC{n}',
                    dc_date=today - timedelta(days=5), expected_delivery_date=expected,
                    status=DeliveryChallan.EXPECTED)

    def test_a_dc_on_a_deleted_project_is_counted_in_neither(self):
        client = Client(SERVER_NAME='localhost')
        client.force_login(self.scm.user)
        response = client.get(reverse('dashboard_scm'))
        self.assertEqual(response.status_code, 200)
        summary = response.context['summary']
        self.assertEqual(summary['deliveries_today'], 1)
        self.assertEqual(summary['overdue'], 1)
