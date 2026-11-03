"""
BC-3 — Project.assigned_design and DesignAssignment.assigned_to stay in step.

`user_can_edit_project_boq()` reads the first, and `user_can_correct_boq()` excludes the
designer through the second. If they name two people, one of them can author a BOQ and
later correct it in review. BC-3 closed the two routes that wrote one field only
(DESIGN_MODULE_DEFERRED V1):

  I1  project_activate refuses every non-Residential project before any write, and
      before it reads assigned_design_id. Tested FOR Residential, so an unknown type is
      refused too.
  I2  DesignAssignment.assigned_to is read-only in the admin, on the add form as well as
      the change form. The Project admin never offered assigned_design; that is pinned
      here so a future fieldset edit cannot add it back unnoticed.

Residential activation is pinned by tests_residential_baseline and tests_opex_activation;
the one test here only proves the guard lets it through.
"""
from django.contrib import admin as django_admin
from django.contrib.auth.models import User
from django.contrib.messages import get_messages
from django.test import Client, RequestFactory
from django.urls import reverse
from django.utils import timezone

from .models import (
    DesignAssignment, PaymentMilestone, Project, ProjectPhase, StatusTransition,
    DESIGN_IN_DESIGN,
)
from .tests_opex_activation import OpexActivationBase, _client_for, _profile

REFUSAL = 'RESCO and CAPEX sites are activated from the site activation screen.'


class ActivateGuardBase(OpexActivationBase):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.other_designer = _profile('bc3des2', 'Design')

    def _allocated(self, project):
        """The site as _allocate_one leaves it: both fields naming the same designer."""
        Project.objects.filter(pk=project.pk).update(assigned_design=self.designer)
        DesignAssignment.objects.create(
            project=project, status=DESIGN_IN_DESIGN, assigned_to=self.designer,
            assigned_by=self.designer, assigned_at=timezone.now())
        project.refresh_from_db()
        return project

    def _post_activate(self, project, data):
        response = _client_for(self.pm).post(
            reverse('project_activate', args=[project.project_id]), data)
        project.refresh_from_db()
        return response

    def _assert_refused_and_untouched(self, project, response):
        self.assertRedirects(
            response, reverse('project_overview', args=[project.project_id]),
            fetch_redirect_response=False)
        self.assertIn(REFUSAL, [str(m) for m in get_messages(response.wsgi_request)])
        self.assertEqual(project.status, 'Draft')
        self.assertIsNone(project.activated_at)
        self.assertFalse(PaymentMilestone.objects.filter(project=project).exists())
        self.assertFalse(ProjectPhase.objects.filter(project=project).exists())
        self.assertFalse(StatusTransition.objects.filter(
            project=project, to_status='Active').exists())


# ===========================================================================
# I1 — project_activate is Residential only
# ===========================================================================

class ProjectActivateGuardTests(ActivateGuardBase):

    def test_01_an_allocated_opex_site_is_refused_and_nothing_changes(self):
        site = self._allocated(self.site)
        response = self._post_activate(site, {'assigned_design_id': self.other_designer.pk})
        self._assert_refused_and_untouched(site, response)
        self.assertEqual(site.assigned_design, self.designer)
        self.assertEqual(DesignAssignment.objects.get(project=site).assigned_to,
                         self.designer)

    def test_02_a_capex_draft_site_is_refused_the_same_way(self):
        site = Project.objects.create(
            customer_name='BC3 CAPEX', customer_phone='9876543210',
            site_address='2 Tender Road', city='Lucknow', project_type='CAPEX',
            status='Draft', assigned_pm=self.pm)
        response = self._post_activate(site, {'assigned_design_id': self.other_designer.pk})
        self._assert_refused_and_untouched(site, response)
        self.assertIsNone(site.assigned_design)

    def test_03_an_unknown_project_type_is_refused_by_default(self):
        """The guard tests FOR Residential. A type nobody has ruled on yet is refused."""
        site = self._make_opex_site('BC3 Future')
        # Fixture write: save() derives the project_id prefix from the type and has none
        # for an unknown one, so the type is switched after the row exists.
        Project.objects.filter(pk=site.pk).update(project_type='HYBRID')
        site.refresh_from_db()
        response = self._post_activate(site, {'assigned_design_id': self.designer.pk})
        self._assert_refused_and_untouched(site, response)
        self.assertIsNone(site.assigned_design)

    def test_04_the_refusal_comes_before_the_designer_is_read(self):
        """No designer at all, then one that does not exist: still the type refusal, not
        "select a Designer" and not a 404 from the designer lookup."""
        for data in ({}, {'assigned_design_id': '999999'}):
            response = self._post_activate(self.site, data)
            self.assertEqual(response.status_code, 302)
            messages = [str(m) for m in get_messages(response.wsgi_request)]
            self.assertEqual(messages, [REFUSAL])

    def test_05_residential_still_activates(self):
        house = self._make_residential('BC3 House')
        response = self._post_activate(house, {'assigned_design_id': self.designer.pk})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(house.status, 'Active')
        self.assertEqual(house.assigned_design, self.designer)
        self.assertEqual(
            sorted(PaymentMilestone.objects.filter(project=house)
                   .values_list('milestone_name', flat=True)), ['M1', 'M2', 'M3'])
        self.assertNotIn(REFUSAL, [str(m) for m in get_messages(response.wsgi_request)])


# ===========================================================================
# I2 — the admin cannot move one field without the other
# ===========================================================================

class AdminReadOnlyTests(ActivateGuardBase):

    def setUp(self):
        super().setUp()
        self.superuser = User.objects.create_superuser('bc3_admin', 'bc3@example.com', 'pw')
        # AdminAccessMiddleware gates /admin/ on UserProfile.role == 'Admin'.
        self.superuser.profile.role = 'Admin'
        self.superuser.profile.save(update_fields=['role'])
        self.client = Client()
        self.client.force_login(self.superuser)
        self.site = self._allocated(self.site)
        self.assignment = DesignAssignment.objects.get(project=self.site)

    def _request(self):
        request = RequestFactory().get('/')
        request.user = self.superuser
        return request

    def _form_class(self, model, obj):
        model_admin = django_admin.site._registry[model]
        return model_admin.get_form(self._request(), obj, change=obj is not None)

    def test_06_assigned_to_is_not_a_form_field_on_change_or_add(self):
        for obj in (self.assignment, None):
            self.assertNotIn('assigned_to', self._form_class(DesignAssignment, obj).base_fields)

    def test_07_the_change_page_renders_assigned_to_as_text(self):
        url = reverse('admin:projects_designassignment_change', args=[self.assignment.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertNotIn('name="assigned_to"', html)
        self.assertIn(self.designer.user.username, html)

    def test_08_a_post_changing_assigned_to_leaves_it_unchanged(self):
        url = reverse('admin:projects_designassignment_change', args=[self.assignment.pk])
        form = self._form_class(DesignAssignment, self.assignment)(instance=self.assignment)
        data = {}
        for name, field in form.fields.items():
            value = form.initial.get(name, field.initial)
            if value is None or value == '':
                continue
            if name == 'assigned_at':   # a split datetime widget posts two halves
                local = timezone.localtime(value)
                data['assigned_at_0'] = local.strftime('%Y-%m-%d')
                data['assigned_at_1'] = local.strftime('%H:%M:%S')
                continue
            data[name] = getattr(value, 'pk', value)
        data['assigned_to'] = self.other_designer.pk   # the edit being attempted

        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 302,
                         'the submit was rejected for an unrelated reason, so this test '
                         'proved nothing — fix the posted data')
        self.assignment.refresh_from_db()
        self.site.refresh_from_db()
        self.assertEqual(self.assignment.assigned_to, self.designer)
        self.assertEqual(self.site.assigned_design, self.designer)

    def test_09_the_project_admin_does_not_offer_assigned_design(self):
        """Pre-flight 5: the Project admin's fieldsets never included it, on any type."""
        house = self._make_residential('BC3 Admin House')
        for project in (self.site, house, None):
            self.assertNotIn('assigned_design',
                             self._form_class(Project, project).base_fields)
