"""A flash message on the delivery challan page is shown once. Session DG-B.

WHY THIS FILE EXISTS
--------------------
base.html renders `messages` above `{% block content %}`, and delivery_challan_detail.html
rendered them a second time inside its own block, so "GRN confirmed" and every other
message on that page appeared twice (audit DG-A). The page's loop is gone; these tests
follow the redirect from each GRN endpoint and count the message in the rendered page.

DG-C closed the same doubling on three more pages: delivery_challan_create.html and
boq_request_revision.html (base.html), and admin/reset_password.html (admin_base.html,
which renders messages above its own content block). Each is pinned by one refusal the
view renders directly, so the message and the page arrive in the same response.

Run with:
    python manage.py test projects.tests_dc_messages --settings=solarpms.test_settings
"""
import uuid
from datetime import date
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse

from .models import (
    BOQ, DCLineItem, DeliveryChallan, Project, ProjectPhase, Task, UserProfile,
)


def _profile(username, role):
    """Create a user and set their profile role. signals.py creates the UserProfile on
    post_save, so this UPDATES rather than creates (a second create breaks the OneToOne)."""
    user = User.objects.create_user(username=username, password='x',
                                    first_name=username.title(), last_name='Test')
    profile, _ = UserProfile.objects.get_or_create(user=user)
    profile.role = role
    profile.save(update_fields=['role'])
    return profile


class DcDetailMessageTests(TestCase):
    """One project, one challan with one line, an SE who works on the site and SCM."""

    @classmethod
    def setUpTestData(cls):
        cls.scm = _profile('dgb_scm', 'SCM')
        cls.se = _profile('dgb_se', 'Site Engineer')
        cls.pm = _profile('dgb_pm', 'PM')

    def setUp(self):
        self.project = Project.objects.create(
            customer_name='DG-B Customer', customer_phone='9876543210',
            site_address='1 Message Lane', city='Lucknow',
            project_type='Residential', status='Active', assigned_pm=self.pm,
        )
        # The task is what lets confirm_grn's user_works_on_site admit the SE.
        phase = ProjectPhase.objects.create(
            project=self.project, phase_name='Execution', phase_order=1)
        Task.objects.create(phase=phase, task_name='Receive material', task_order=1,
                            assigned_to=self.se, assigned_role='Site Engineer')
        self.challan = DeliveryChallan.objects.create(
            project=self.project, dc_number='DC-DGB-001', dc_date=date.today(),
            status=DeliveryChallan.EXPECTED, created_by=self.scm,
        )
        self.line = DCLineItem.objects.create(
            challan=self.challan, boq_category='Solar Modules',
            item_description='Solar Module 540Wp',
            ordered_quantity=Decimal('10.00'), unit='Nos',
        )

    def _post_and_follow(self, actor, url_name, payload):
        client = Client()
        client.force_login(actor.user)
        response = client.post(
            reverse(url_name, args=[self.project.project_id, self.challan.pk]),
            payload, follow=True)
        # Landed on the challan page itself, not a refusal or another page.
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'projects/delivery_challan_detail.html')
        return response.content.decode()

    def test_grn_confirmed_is_shown_once(self):
        page = self._post_and_follow(self.se, 'confirm_grn', {
            f'received_qty_{self.line.pk}': '10',
            f'damaged_qty_{self.line.pk}': '0',
        })
        self.assertEqual(page.count('GRN confirmed'), 1)

    def test_grn_overridden_is_shown_once(self):
        # A first-time line through the override takes the on-behalf branch, so it needs
        # the reason; without one the view refuses and no success message is set.
        page = self._post_and_follow(self.scm, 'override_grn', {
            f'received_qty_{self.line.pk}': '10',
            f'damaged_qty_{self.line.pk}': '0',
            'grn_on_behalf_reason': 'Site engineer unreachable; verified over call',
        })
        self.assertEqual(page.count('GRN overridden'), 1)


class OtherPageMessageTests(TestCase):
    """DG-C: one refusal per page, rendered by the view itself, counted once."""

    @classmethod
    def setUpTestData(cls):
        cls.scm = _profile('dgc_scm', 'SCM')
        cls.pm = _profile('dgc_pm', 'PM')
        cls.admin = _profile('dgc_admin', 'Admin')
        cls.target = _profile('dgc_target', 'Site Engineer')

    def setUp(self):
        self.project = Project.objects.create(
            customer_name='DG-C Customer', customer_phone='9876543210',
            site_address='2 Message Lane', city='Lucknow',
            project_type='Residential', status='Active', assigned_pm=self.pm,
        )

    def _post(self, actor, url, payload):
        client = Client()
        client.force_login(actor.user)
        response = client.post(url, payload)
        # A refusal that re-renders the form: 200, not a redirect.
        self.assertEqual(response.status_code, 200)
        return response

    def test_delivery_challan_create_refusal_is_shown_once(self):
        # Missing DC number is refused before any document is read or uploaded, so no
        # storage patch is needed. The key is a fresh one, so no earlier challan answers.
        response = self._post(
            self.scm, reverse('create_delivery_challan', args=[self.project.project_id]),
            {'client_uuid': str(uuid.uuid4()), 'dc_number': '',
             'dc_date': date.today().isoformat()})
        self.assertTemplateUsed(response, 'projects/delivery_challan_create.html')
        self.assertEqual(
            response.content.decode().count('DC Number and DC Date are required.'), 1)

    def test_boq_request_revision_refusal_is_shown_once(self):
        BOQ.objects.create(project=self.project, status='Submitted')
        response = self._post(
            self.pm, reverse('boq_request_revision', args=[self.project.project_id]),
            {'reason': '   '})
        self.assertTemplateUsed(response, 'projects/boq_request_revision.html')
        self.assertEqual(response.content.decode().count(
            'Please provide a reason for the revision request.'), 1)

    def test_admin_reset_password_refusal_is_shown_once(self):
        response = self._post(
            self.admin, reverse('admin_reset_password', args=[self.target.user.pk]),
            {'new_password': 'short', 'confirm_password': 'short'})
        self.assertTemplateUsed(response, 'projects/admin/reset_password.html')
        self.assertEqual(response.content.decode().count(
            'Password must be at least 8 characters.'), 1)
