"""
SCM dashboard: the ?context= switch applies to every section.

THE RULE: residential = project_type Residential; tenders = OPEX (RESCO) and CAPEX.
One mapping, CONTEXT_PROJECT_TYPES in views.py, read by `_context_filter` (counters,
project list) and `_context_includes` (the tender-grouping section).

WHAT IS PINNED HERE:
  * a Residential project is listed under residential only; an OPEX and a CAPEX project
    are listed under tenders only;
  * the "Tenders — grouped procurement" section renders under tenders and is ABSENT
    (not empty) under residential and with no context;
  * each of the six counters counts only its context's projects.

Run with:
    python manage.py test projects.tests_scm_context_filter --settings=solarpms.test_settings
"""
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse

from .models import (
    BOQ, DESIGN_IN_DESIGN, DeliveryChallan, DesignAssignment, Program, Project,
    ProjectPhase, Task,
)

SECTION_HEADING = 'Tenders — grouped procurement'
COUNTERS = ('boq_awaiting', 'deliveries_today', 'overdue',
            'tasks_due_today', 'tasks_due_soon', 'tasks_overdue')


def _profile(username, role):
    user = User.objects.create_user(username=username, password='x',
                                    first_name=username.title(), last_name='Test')
    profile = user.profile          # auto-created by the post_save signal
    profile.role = role
    profile.save(update_fields=['role'])
    return profile


class ScmContextFilterTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.scm = _profile('ctx_scm', 'SCM')
        cls.designer = _profile('ctx_designer', 'Design')
        opex_program = Program.objects.create(
            name='Ctx OPEX Tender', program_type='OPEX', client_name='C',
            status='Active', short_tender_code='CTX')
        capex_program = Program.objects.create(
            name='Ctx CAPEX Plant', program_type='CAPEX', client_name='C', status='Active')

        cls.resi  = cls._project('CTX-RES', 'Residential')
        cls.opex  = cls._project('CTX-OPX', 'OPEX', program=opex_program, site_code='CTX-OPX')
        cls.capex = cls._project('CTX-CPX', 'CAPEX', program=capex_program)

        # The OPEX site has design work, so its tender has a grouping row.
        DesignAssignment.objects.create(
            project=cls.opex, status=DESIGN_IN_DESIGN, assigned_to=cls.designer,
            survey_file_bucket='b', survey_file_path='CTX-OPX/survey/x.pdf')

        # One of every counter on every project.
        today = date.today()
        for project in (cls.resi, cls.opex, cls.capex):
            BOQ.objects.create(project=project, status='Submitted')
            for n, expected in ((1, today), (2, today - timedelta(days=3))):
                DeliveryChallan.objects.create(
                    project=project, dc_number=f'{project.project_id}-DC{n}',
                    dc_date=today - timedelta(days=5), expected_delivery_date=expected,
                    status=DeliveryChallan.EXPECTED)
            phase = ProjectPhase.objects.create(project=project, phase_name='P1', phase_order=1)
            for n, due in enumerate((today, today + timedelta(days=3),
                                     today - timedelta(days=2)), start=1):
                Task.objects.create(phase=phase, task_name=f'T{n}', task_order=n,
                                    due_date=due, status=Task.NOT_STARTED)

    @staticmethod
    def _project(project_id, project_type, **extra):
        return Project.objects.create(
            project_id=project_id, customer_name=project_id, customer_phone='9876543210',
            site_address='1 Sun Road', city='Delhi', project_type=project_type,
            dc_capacity_kw=Decimal('5.00'), status='Active',
            target_commissioning_date=date.today() + timedelta(days=90),
            **extra)

    def setUp(self):
        self.client = Client(SERVER_NAME='localhost')
        self.client.force_login(self.scm.user)

    def _get(self, context=None):
        params = {'context': context} if context else {}
        response = self.client.get(reverse('dashboard_scm'), params)
        self.assertEqual(response.status_code, 200)
        return response

    def _listed(self, response):
        return {row['project'].project_id for row in response.context['project_rows']}

    # ── project list ────────────────────────────────────────────────────────

    def test_residential_project_listed_only_under_residential(self):
        self.assertEqual(self._listed(self._get('residential')), {'CTX-RES'})
        self.assertNotIn('CTX-RES', self._listed(self._get('tenders')))

    def test_opex_and_capex_projects_listed_only_under_tenders(self):
        self.assertEqual(self._listed(self._get('tenders')), {'CTX-OPX', 'CTX-CPX'})
        listed = self._listed(self._get('residential'))
        self.assertNotIn('CTX-OPX', listed)
        self.assertNotIn('CTX-CPX', listed)

    # ── tender-grouping section ────────────────────────────────────────────

    def test_grouping_section_renders_under_tenders(self):
        response = self._get('tenders')
        self.assertEqual(len(response.context['opex_tender_rows']), 1)
        self.assertContains(response, SECTION_HEADING)
        self.assertContains(response, 'Ctx OPEX Tender')

    def test_grouping_section_absent_under_residential(self):
        response = self._get('residential')
        self.assertEqual(response.context['opex_tender_rows'], [])
        self.assertNotContains(response, SECTION_HEADING)
        self.assertNotContains(response, 'Ctx OPEX Tender')

    def test_grouping_section_absent_with_no_context(self):
        response = self._get()
        self.assertEqual(response.context['opex_tender_rows'], [])
        self.assertNotContains(response, SECTION_HEADING)

    # ── counters ────────────────────────────────────────────────────────────

    def test_each_counter_matches_its_context(self):
        # One of each per project: residential = 1 project, tenders = 2, no context = 3.
        for context, expected in (('residential', 1), ('tenders', 2), (None, 3)):
            summary = self._get(context).context['summary']
            for key in COUNTERS:
                with self.subTest(context=context, counter=key):
                    self.assertEqual(summary[key], expected)
