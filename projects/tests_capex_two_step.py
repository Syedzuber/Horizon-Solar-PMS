"""CAPEX-2 — two-step task completion applies to CAPEX exactly as to OPEX (RESCO).

THE RULE UNDER TEST. permissions.uses_two_step_completion(project) is the single
authority for which projects finish tasks by submit -> approve: True for OPEX and
CAPEX, False for Residential. Every two-step condition reads it, so a CAPEX task now
has the whole OPEX loop: the row withholds Done and offers Submit (or Complete
(self-certified) when nobody else can approve), the server refuses a direct Done, an
approval completes the task, a rejection raises a punch point, and the submitter may
not approve their own work.

ONE CONTRACT, BOTH TYPES. `LoopContract` holds one test per path and runs twice: on a
CAPEX site (`CapexLoopTests`, the new behaviour) and on an OPEX site
(`OpexLoopUnchangedTests`, the regression guard). A path that held on one type and not
the other would fail in exactly one of the two classes.

EVERY SITE IS A REALLY ACTIVATED ONE. The template comes from the migrations' own seed
functions (0075 for OPEX, 0128 for CAPEX) and activation goes through
opex_site_activate, the view the Activate button posts to for every non-Residential
project — the tasks under test are the rows production would hold.

THE APPROVER POOL ON CAPEX is the PM, the coordinators and legacy is_qaqc holders: the
per-site QA/QC engineer stays OPEX-only (site_qaqc_engineer_id). The fixture uses the
PM as the second signature, which is the realistic CAPEX shape.

Run with:
    python manage.py test projects.tests_capex_two_step --settings=solarpms.test_settings
"""
from datetime import date, timedelta
from decimal import Decimal
from importlib import import_module

from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Project, PunchPoint, Task
from .permissions import user_can_approve_task, uses_two_step_completion
from .tests_capex_template import _seed_capex
from .tests_row_render_flags import _options, _rows
from .tests_two_step_completion import _client_for, _profile, _seed_opex
from .utils import RESIDENTIAL_FINANCE_ASSIGNEE_EMAIL, assign_task_to, resolve_residential_template

# A non-mirror Site Engineer task in both templates (0128 copies 0075's rows).
SE_TASK_NAME = 'Module Installation'
# A non-mirror PM task the attach pre-assigns to the site PM, on both types.
PM_TASK_NAME = 'Net Metering Approval'

SUBMIT_LABEL = 'Submit for approval'
SELF_CERTIFY_LABEL = 'Complete (self-certified)'


def _future(days=7):
    return (date.today() + timedelta(days=days)).isoformat()


class HelperTests(TestCase):
    """The helper itself: the whole scope of the rule is these three answers."""

    def _project(self, project_type):
        return Project(project_type=project_type)

    def test_true_for_opex_and_capex(self):
        self.assertTrue(uses_two_step_completion(self._project('OPEX')))
        self.assertTrue(uses_two_step_completion(self._project('CAPEX')))

    def test_false_for_residential_and_none(self):
        self.assertFalse(uses_two_step_completion(self._project('Residential')))
        self.assertFalse(uses_two_step_completion(None))


class _SiteFixture(TestCase):
    """One activated site of PROJECT_TYPE: its PM (holding the PM task), a site engineer
    holding the SE task, and a coordinator NOT yet on the site (tests add them when
    they need a second approver besides the PM)."""

    PROJECT_TYPE = None

    @classmethod
    def setUpTestData(cls):
        resolve_residential_template()
        _seed_opex()
        _seed_capex()
        cls.pm    = _profile('c2_pm', 'PM')
        cls.se    = _profile('c2_se', 'Site Engineer')
        cls.coord = _profile('c2_coord', 'Project Coordinator')

    def setUp(self):
        self.site = Project.objects.create(
            customer_name=f'CAPEX-2 {self.PROJECT_TYPE} Site', customer_phone='9876543210',
            site_address='1 Approval Road', city='Lucknow', project_type=self.PROJECT_TYPE,
            dc_capacity_kw=Decimal('100.00'), status='Draft', assigned_pm=self.pm,
        )
        response = _client_for(self.pm).post(
            reverse('opex_site_activate', args=[self.site.project_id]))
        self.assertEqual(response.status_code, 302, 'activation did not redirect')
        self.site.refresh_from_db()
        self.assertEqual(self.site.status, 'Active')

        self.se_task = Task.objects.get(phase__project=self.site, task_name=SE_TASK_NAME)
        self.assertFalse(self.se_task.is_mirror)
        assign_task_to(self.se_task, self.se, notify=False)
        self.se_task.refresh_from_db()

        self.pm_task = Task.objects.get(phase__project=self.site, task_name=PM_TASK_NAME)
        self.assertEqual(self.pm_task.assigned_to, self.pm,
                         'the attach pre-assigns non-mirror PM tasks to the site PM')

    # -- helpers --------------------------------------------------------------

    def _post(self, url_name, task, data=None, profile=None, **headers):
        return _client_for(profile or self.pm).post(
            reverse(url_name, args=[self.site.project_id, task.pk]), data or {}, **headers)

    def _in_progress(self, task, profile):
        self._post('task_status_update', task,
                   {'status': Task.IN_PROGRESS, 'due_date': _future()}, profile=profile)
        task.refresh_from_db()
        self.assertEqual(task.status, Task.IN_PROGRESS, 'fixture could not start the task')
        return task

    def _submitted_by_se(self):
        task = self._in_progress(self.se_task, self.se)
        self._post('task_submit_for_approval', task,
                   {'submission_remarks': 'Modules mounted.'}, profile=self.se)
        task.refresh_from_db()
        self.assertEqual(task.submitted_by, self.se)
        self.assertIsNone(task.approved_at, 'an SE submission must queue for the PM')
        return task

    def _messages(self, response):
        return ' '.join(str(m) for m in get_messages(response.wsgi_request))

    def _overview_row(self, task, profile):
        response = _client_for(profile).get(
            reverse('project_overview', args=[self.site.project_id]))
        self.assertEqual(response.status_code, 200)
        return _rows(response.content.decode())[task.pk]


class LoopContract:
    """One test per two-step path; runs on CAPEX and on OPEX."""

    def test_row_offers_submit_and_no_done(self):
        task = self._in_progress(self.se_task, self.se)
        row = self._overview_row(task, self.se)
        self.assertIn('<select name="status"', row, 'the SE row lost its status control')
        self.assertNotIn(Task.DONE, _options(row), 'the row offers a Done the rung refuses')
        self.assertIn(SUBMIT_LABEL, row)
        self.assertNotIn(SELF_CERTIFY_LABEL, row)

    def test_direct_done_is_refused_from_both_status_screens(self):
        task = self._in_progress(self.se_task, self.se)
        for entry in ('task_status_update', 'task_detail_status_update'):
            with self.subTest(entry=entry):
                response = self._post(entry, task, {'status': Task.DONE}, profile=self.se)
                task.refresh_from_db()
                self.assertEqual(task.status, Task.IN_PROGRESS)
                self.assertIsNone(task.completed_at)
                text = self._messages(response)
                self.assertIn(f'a {self.site.get_project_type_display()} task must be '
                              f'submitted for approval', text)
                self.assertNotIn('OPEX', text)

    def test_submit_then_approve_completes_the_task(self):
        task = self._submitted_by_se()
        self._post('task_approve', task, {'approval_remarks': 'Checked on site.'})
        task.refresh_from_db()
        self.assertEqual(task.status, Task.DONE)
        self.assertEqual(task.approved_by, self.pm)
        self.assertIsNotNone(task.approved_at)
        self.assertFalse(task.approval_self_certified)

    def test_submit_then_reject_raises_one_punch_point(self):
        task = self._submitted_by_se()
        self._post('task_reject', task, {'approval_remarks': 'Clamps loose.'})
        task.refresh_from_db()
        self.assertIsNone(task.submitted_at)
        self.assertEqual(task.status, Task.IN_PROGRESS)
        points = list(PunchPoint.objects.filter(task=task))
        self.assertEqual(len(points), 1)
        self.assertEqual(points[0].reason, 'Clamps loose.')
        self.assertEqual(points[0].raised_by, self.pm)
        self.assertEqual(points[0].status, PunchPoint.OPEN)

    def test_self_approval_is_refused(self):
        # A coordinator on the site is a second approver, so the PM's submission of
        # their own task queues instead of self-certifying.
        self.site.coordinators.add(self.coord)
        task = self._in_progress(self.pm_task, self.pm)
        self._post('task_submit_for_approval', task, {'submission_remarks': 'Filed.'})
        task.refresh_from_db()
        self.assertEqual(task.submitted_by, self.pm)
        self.assertIsNone(task.approved_at, 'with a coordinator the submission must queue')

        response = self._post('task_approve', task, {'approval_remarks': 'Mine, fine.'})
        task.refresh_from_db()
        self.assertIsNone(task.approved_at, 'the submitter approved their own task')
        self.assertEqual(task.status, Task.IN_PROGRESS)
        self.assertIn(f"yourself — a {self.site.get_project_type_display()} task must be "
                      f"approved by someone other", self._messages(response))

        # The coordinator, a different person, may.
        self._post('task_approve', task, {'approval_remarks': 'Seen.'}, profile=self.coord)
        task.refresh_from_db()
        self.assertEqual(task.status, Task.DONE)
        self.assertEqual(task.approved_by, self.coord)


class CapexLoopTests(LoopContract, _SiteFixture):
    PROJECT_TYPE = 'CAPEX'

    def test_self_certified_completion_when_no_other_approver(self):
        """No coordinator, no is_qaqc holder: the PM is the whole pool, so the row says
        Complete (self-certified) and the submission completes the task."""
        task = self._in_progress(self.pm_task, self.pm)
        row = self._overview_row(task, self.pm)
        self.assertIn(SELF_CERTIFY_LABEL, row)
        self.assertNotIn(Task.DONE, _options(row))

        self._post('task_submit_for_approval', task, {'submission_remarks': 'Filed.'})
        task.refresh_from_db()
        self.assertEqual(task.status, Task.DONE)
        self.assertEqual(task.approved_by, self.pm)
        self.assertTrue(task.approval_self_certified)

    def test_rejected_task_row_shows_one_open_punch_point(self):
        task = self._submitted_by_se()
        self._post('task_reject', task, {'approval_remarks': 'Clamps loose.'})
        row = self._overview_row(task, self.pm)
        self.assertIn('open-punch-badge', row, 'the CAPEX row dropped the punch badge')
        self.assertIn('>1 punch point</a>', row)

    def test_the_task_detail_panel_offers_submit(self):
        task = self._in_progress(self.se_task, self.se)
        response = _client_for(self.se).get(
            reverse('task_detail', args=[self.site.project_id, task.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['show_approval_panel'])
        self.assertTrue(response.context['can_submit_task'])

    def test_the_pm_is_the_approver_and_no_site_qaqc_engineer_exists(self):
        """The per-site QA/QC engineer stays OPEX-only; on CAPEX the PM signs."""
        self.assertTrue(user_can_approve_task(self.pm.user, self.site))
        self.assertFalse(user_can_approve_task(self.se.user, self.site))

    def test_a_mirror_is_still_refused_as_a_mirror(self):
        mirror = Task.objects.filter(phase__project=self.site, is_mirror=True).first()
        response = self._post('task_submit_for_approval', mirror,
                              {'submission_remarks': 'x'})
        mirror.refresh_from_db()
        self.assertIsNone(mirror.submitted_at)
        self.assertIn('mirror task', self._messages(response))

    def test_a_done_task_without_approval_stays_done(self):
        """Tasks Done before CAPEX-2 stay Done: the rung refuses a MOVE to Done, never
        re-reads a finished row. A Done CAPEX row with no approval still renders Done."""
        Task.objects.filter(pk=self.se_task.pk).update(
            status=Task.DONE, completed_at=timezone.now())
        row = self._overview_row(self.se_task, self.se)
        self.assertIn(Task.DONE, row)
        self.se_task.refresh_from_db()
        self.assertEqual(self.se_task.status, Task.DONE)


class OpexLoopUnchangedTests(LoopContract, _SiteFixture):
    PROJECT_TYPE = 'OPEX'


class CapexRowResponderTests(_SiteFixture):
    """Every HTMX responder that redraws a CAPEX row draws the two-step row: no Done on a
    row that is not Done, Submit on the In Progress SE task (the PM manages the site, so
    sees the button on every row it may submit).

    A coordinator is on the site so the PM's submission has a second signature and the
    button reads "Submit for approval" (tests_row_render_flags adds a QA/QC holder for
    the same reason); without one the PM is the whole pool and it reads "Complete
    (self-certified)", which CapexLoopTests covers."""

    PROJECT_TYPE = 'CAPEX'

    def setUp(self):
        super().setUp()
        self.site.coordinators.add(self.coord)
        self._in_progress(self.se_task, self.se)
        self.phase = self.se_task.phase

    def _hx(self, url_name, args, data):
        response = _client_for(self.pm).post(
            reverse(url_name, args=args), data, HTTP_HX_REQUEST='true')
        self.assertEqual(response.status_code, 200, response.content[:300])
        return _rows(response.content.decode())

    def assertTwoStepRows(self, rows):
        self.assertIn(self.se_task.pk, rows, 'the In Progress task was not redrawn')
        statuses = dict(Task.objects.filter(pk__in=rows).values_list('pk', 'status'))
        for pk, row in rows.items():
            if '<select name="status"' in row and statuses[pk] != Task.DONE:
                self.assertNotIn(Task.DONE, _options(row), f'row {pk} offers Done')
        self.assertIn('openSubmitApprovalModal(this)', rows[self.se_task.pk])
        self.assertIn(SUBMIT_LABEL, rows[self.se_task.pk])

    def test_task_add(self):
        self.assertTwoStepRows(self._hx('task_add', [self.site.project_id], {
            'task_name': 'Extra installation step', 'phase': self.phase.pk,
            'assigned_role': Task.SITE_ENGINEER, 'assigned_to': self.se.pk,
            'due_date': timezone.localdate().isoformat()}))

    def test_duplicate_for_locations(self):
        source = (Task.objects.filter(phase=self.phase, is_mirror=False,
                                      status=Task.NOT_STARTED)
                  .exclude(pk=self.se_task.pk).first())
        self.assertTwoStepRows(self._hx(
            'task_duplicate_locations_create', [self.site.project_id, source.pk],
            {'existing_locations': [], 'new_locations': 'Block A',
             'assigned_to': self.se.pk}))

    def test_rename(self):
        self.assertTwoStepRows(self._hx(
            'task_rename_save', [self.site.project_id, self.se_task.pk],
            {'value': 'Module Installation Block B'}))

    def test_reorder(self):
        order = list(Task.objects.filter(phase=self.phase)
                     .order_by('task_order', 'pk').values_list('pk', flat=True))
        self.assertTwoStepRows(self._hx(
            'phase_tasks_reorder', [self.site.project_id, self.phase.pk],
            {'order': ','.join(str(pk) for pk in reversed(order))}))


class ResidentialIsOneStepTests(TestCase):
    """A Residential task still goes to Done in one move and the approval endpoints
    refuse it, naming Residential and never a stored code."""

    @classmethod
    def setUpTestData(cls):
        resolve_residential_template()
        cls.pm       = _profile('c2r_pm', 'PM')
        cls.designer = _profile('c2r_des', 'Design')
        cls.finance  = _profile('c2r_fin', 'Finance',
                                email=RESIDENTIAL_FINANCE_ASSIGNEE_EMAIL)

    def setUp(self):
        self.project = Project.objects.create(
            customer_name='CAPEX-2 House', customer_phone='9876543211',
            site_address='2 Baseline Lane', city='Lucknow', project_type='Residential',
            dc_capacity_kw=Decimal('5.00'), status='Draft', assigned_pm=self.pm,
            target_commissioning_date=date.today() + timedelta(days=90),
        )
        response = _client_for(self.pm).post(
            reverse('project_activate', args=[self.project.project_id]),
            {'assigned_design_id': self.designer.pk})
        self.assertEqual(response.status_code, 302)
        self.task = Task.objects.filter(
            phase__project=self.project, assigned_role=Task.PM, is_mirror=False).first()
        assign_task_to(self.task, self.pm, notify=False)

    def _status(self, status, **extra):
        return _client_for(self.pm).post(
            reverse('task_status_update', args=[self.project.project_id, self.task.pk]),
            {'status': status, **extra})

    def test_done_in_one_move(self):
        self._status(Task.IN_PROGRESS, due_date=_future(3))
        self._status(Task.DONE)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, Task.DONE)
        self.assertIsNone(self.task.approved_at)

    def test_approval_endpoints_refuse_with_the_display_label(self):
        response = _client_for(self.pm).post(
            reverse('task_submit_for_approval',
                    args=[self.project.project_id, self.task.pk]),
            {'submission_remarks': 'done'})
        self.task.refresh_from_db()
        self.assertIsNone(self.task.submitted_at)
        text = ' '.join(str(m) for m in get_messages(response.wsgi_request))
        self.assertIn('does not apply to Residential sites', text)
        self.assertNotIn('OPEX', text)
