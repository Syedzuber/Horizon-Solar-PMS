"""CAPEX-3 — CAPEX kept out of Programs and procurement group raises, and the project-type
wording users read.

WHAT IS PINNED, AND WHY
-----------------------
  * CAPEX STANDS ALONE. ProgramForm offers RESCO only; a hand-posted CAPEX is refused
    with a message naming the "New Project" button, and no Program row is written.
    An existing CAPEX Program is refused on its edit URL, GET and POST, and its row
    is left exactly as it was (it can no longer be edited without converting it).
    The model still allows CAPEX, so existing rows stay valid.
  * THE GROUP RAISE RECORDS RESCO SITES ONLY. A CAPEX or Residential site id posted
    directly is refused in the display label's words, and no order, order site or
    payment request is written. The check lives in _parse_group_submission, NOT in
    _parse_group_sites: the purchases workspace shares that helper and records
    Residential on purpose (tests_purchases_workspace keeps pinning that).
  * THE APPROVER IS NEVER NAMED AS "the project manager or QA/QC". On CAPEX the per-site
    QA/QC engineer cannot approve, so the Done refusal and the four approval strings
    say "someone other than the submitter", on both types alike.
  * DISPLAY LABELS: the vendor order detail says RESCO, the subadmin badge gives each of
    the three stored values its own colour, and the is_qaqc help text covers RESCO and
    CAPEX.

    python manage.py test projects.tests_capex_boundary --settings=solarpms.test_settings
"""
import re
from decimal import Decimal

from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import reverse

from .forms import AdminUserEditForm, ProgramForm
from .models import (
    PaymentRequest, Program, Project, Task, VendorOrder, VendorOrderSite,
)
from .tests_capex_two_step import _SiteFixture
from .tests_program_access import _client, _profile
from .tests_two_step_completion import _client_for
from .tests_vendor_order_group import GroupRaiseFixture

APPROVER_NAMED = re.compile(r'project manager or QA/QC|QA/QC (to )?approve', re.I)
SOMEONE_ELSE = 'someone other than the submitter'


def _messages(response):
    return ' '.join(str(m) for m in get_messages(response.wsgi_request))


def _flat(html):
    """Collapse whitespace, so a sentence the template wraps over two lines matches."""
    return re.sub(r'\s+', ' ', html)


# ---------------------------------------------------------------------------
# Part 1 — Programs
# ---------------------------------------------------------------------------

class ProgramTypeTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.admin = _profile('cb_admin', 'Admin')
        cls.pm = _profile('cb_pm', 'PM')

    def _payload(self, program_type, **overrides):
        data = {'program_type': program_type, 'name': 'Boundary Tender',
                'client_name': 'Boundary Client', 'status': 'Draft',
                'short_tender_code': 'BOUND26', 'planned_site_count': '3'}
        data.update(overrides)
        return data

    def test_capex_is_absent_from_the_create_form_choices(self):
        values = [value for value, _ in ProgramForm().fields['program_type'].choices]
        self.assertIn('OPEX', values)
        self.assertNotIn('CAPEX', values)

        html = _client(self.pm).get(reverse('program_create')).content.decode()
        self.assertIn('<option value="OPEX"', html)
        self.assertNotIn('value="CAPEX"', html)

    def test_a_capex_post_is_refused_and_writes_no_program(self):
        response = _client(self.pm).post(reverse('program_create'),
                                         self._payload('CAPEX', short_tender_code=''))
        self.assertEqual(response.status_code, 200, 'the form must re-render, not save')
        self.assertContains(response, ProgramForm.PROGRAM_TYPE_REFUSED)
        self.assertIn('New Project', ProgramForm.PROGRAM_TYPE_REFUSED)
        self.assertNotContains(response, 'Select a valid choice')
        self.assertFalse(Program.objects.exists())

    def test_an_opex_create_is_unchanged(self):
        response = _client(self.pm).post(reverse('program_create'), self._payload('OPEX'))
        program = Program.objects.get()
        self.assertRedirects(response, reverse('program_detail', args=[program.pk]),
                             fetch_redirect_response=False)
        self.assertEqual(program.program_type, 'OPEX')
        self.assertEqual(program.short_tender_code, 'BOUND26')
        self.assertEqual(program.created_by, self.pm.user)

    def test_an_opex_program_cannot_be_edited_into_capex(self):
        program = Program.objects.create(
            program_type='OPEX', name='Edit Tender', client_name='Edit Client',
            short_tender_code='EDIT26', status='Draft', created_by=self.admin.user)
        url = reverse('program_edit', args=[program.pk])
        self.assertNotIn('value="CAPEX"', _client(self.admin).get(url).content.decode())

        response = _client(self.admin).post(url, self._payload(
            'CAPEX', name='Edit Tender', short_tender_code='EDIT26'))
        self.assertContains(response, ProgramForm.PROGRAM_TYPE_REFUSED)
        program.refresh_from_db()
        self.assertEqual(program.program_type, 'OPEX')

    def test_a_capex_program_edit_is_refused_on_get_and_post_and_the_row_is_unchanged(self):
        program = Program.objects.create(
            program_type='CAPEX', name='Legacy CAPEX', client_name='Legacy Client',
            status='Draft', created_by=self.admin.user)
        before = Program.objects.filter(pk=program.pk).values().get()
        url = reverse('program_edit', args=[program.pk])
        detail = reverse('program_detail', args=[program.pk])

        for method in ('get', 'post'):
            with self.subTest(method=method):
                client = _client(self.admin)
                if method == 'get':
                    response = client.get(url)
                else:
                    response = client.post(url, self._payload(
                        'OPEX', name='Renamed', client_name='Other', status='Active'))
                self.assertRedirects(response, detail, fetch_redirect_response=False)
                self.assertIn('This CAPEX Program can no longer be edited',
                              _messages(response))
                self.assertEqual(Program.objects.filter(pk=program.pk).values().get(),
                                 before, 'the CAPEX Program row changed')


# ---------------------------------------------------------------------------
# Part 1 — the procurement group raise
# ---------------------------------------------------------------------------

class GroupRaiseTypeTests(GroupRaiseFixture):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.capex = Project.objects.create(
            project_id='HRP-CAP-TEST-001', customer_name='Boundary CAPEX',
            customer_phone='9876543210', site_address='1 Plant Road', city='Delhi',
            state='Delhi', project_type='CAPEX', dc_capacity_kw=Decimal('500.00'),
            status='Active')

    def test_a_capex_site_id_is_refused_and_nothing_is_written(self):
        response, storage = self.post(sites=[self.capex])
        self.assertEqual(response.status_code, 400)
        self.assertContains(
            response, 'HRP-CAP-TEST-001 is a CAPEX project. Only RESCO sites can be '
                      'recorded on a group payment request.', status_code=400)
        self.assertNothingCreated()
        storage.storage.from_.return_value.upload.assert_not_called()

    def test_a_residential_site_id_is_refused_and_nothing_is_written(self):
        response, storage = self.post(sites=[self.project])
        self.assertEqual(response.status_code, 400)
        self.assertContains(
            response, f'{self.project.project_id} is a Residential project. Only RESCO '
                      'sites can be recorded on a group payment request.', status_code=400)
        self.assertNothingCreated()
        storage.storage.from_.return_value.upload.assert_not_called()

    def test_an_opex_raise_is_unchanged(self):
        response, _ = self.post(sites=[(self.a1, self.locked_a)], programs=[self.tender])
        order = VendorOrder.objects.get()
        self.assertRedirects(response, reverse('vendor_order_detail', args=[order.pk]),
                             fetch_redirect_response=False)
        self.assertEqual(order.project_type, 'OPEX')
        self.assertEqual(VendorOrderSite.objects.get().project, self.a1)
        self.assertEqual(PaymentRequest.objects.count(), 1)

    def test_vendor_order_detail_shows_resco_never_bare_opex(self):
        self.post(sites=[(self.a1, self.locked_a)], programs=[self.tender])
        order = VendorOrder.objects.get()
        html = _flat(_client(self.scm).get(
            reverse('vendor_order_detail', args=[order.pk])).content.decode())
        self.assertIn(f'{self.vendor.name} · RESCO</small>', html)
        self.assertRegex(html, r'>Type</dt> <dd class="[^"]*">RESCO</dd>')
        self.assertNotIn('· OPEX', html)
        self.assertNotIn('>OPEX<', html)


# ---------------------------------------------------------------------------
# Part 2 — the Done refusal and the four approval strings
# ---------------------------------------------------------------------------

class _ApprovalWordingContract:
    """Runs on a CAPEX site and on an OPEX site: the meaning must be the same on both."""

    def _overview(self, profile):
        response = _client_for(profile).get(
            reverse('project_overview', args=[self.site.project_id]))
        self.assertEqual(response.status_code, 200)
        return _flat(response.content.decode())

    def _detail(self, task, profile):
        response = _client_for(profile).get(
            reverse('task_detail', args=[self.site.project_id, task.pk]))
        self.assertEqual(response.status_code, 200)
        return _flat(response.content.decode())

    def assertNoApproverNamed(self, html):
        self.assertIsNone(APPROVER_NAMED.search(html), APPROVER_NAMED.search(html)
                          and APPROVER_NAMED.search(html).group(0))

    def test_the_done_refusal_names_no_approver_and_no_stored_code(self):
        task = self._in_progress(self.se_task, self.se)
        response = self._post('task_status_update', task, {'status': Task.DONE},
                              profile=self.se)
        text = self._messages(response)
        self.assertIn(
            f"'{task.task_name}' cannot be marked Done directly — a "
            f"{self.site.get_project_type_display()} task must be submitted for approval "
            f"and approved by {SOMEONE_ELSE} before it can be completed.", text)
        self.assertNotIn('QA/QC', text)
        self.assertNotIn('OPEX', text)

    def test_the_submit_modal_line(self):
        self._in_progress(self.se_task, self.se)
        html = self._overview(self.se)
        self.assertIn(f'stays In Progress until {SOMEONE_ELSE} approves it.', html)
        self.assertNoApproverNamed(html)

    def test_the_row_badge_title(self):
        self._submitted_by_se()
        html = self._overview(self.pm)
        self.assertIn(f'— waiting on approval by {SOMEONE_ELSE}.">Awaiting approval', html)
        self.assertNoApproverNamed(html)

    def test_the_panel_while_waiting(self):
        task = self._submitted_by_se()
        html = self._detail(task, self.se)
        self.assertIn(f'Waiting on {SOMEONE_ELSE} to approve this work.', html)
        self.assertNoApproverNamed(html)

    def test_the_panel_explaining_two_steps(self):
        # Drawn for a viewer who may neither submit nor approve; the PM may submit
        # (self-certify) any task on their site, so the CEO is that viewer.
        task = self._in_progress(self.se_task, self.se)
        html = self._detail(task, _profile(f'cb_ceo_{self.PROJECT_TYPE}', 'CEO'))
        self.assertIn('This task is completed in two steps: the assignee submits it, '
                      f'then {SOMEONE_ELSE} approves it.', html)
        self.assertNoApproverNamed(html)


class CapexApprovalWordingTests(_ApprovalWordingContract, _SiteFixture):
    PROJECT_TYPE = 'CAPEX'


class OpexApprovalWordingTests(_ApprovalWordingContract, _SiteFixture):
    PROJECT_TYPE = 'OPEX'


# ---------------------------------------------------------------------------
# Part 2 — subadmin badge and is_qaqc help text
# ---------------------------------------------------------------------------

class SubadminBadgeTests(TestCase):

    BADGE = {'Residential': 'bg-blue-50 text-blue-700',
             'OPEX':        'bg-purple-50 text-purple-700',
             'CAPEX':       'bg-green-50 text-green-700'}
    FALLBACK = 'bg-yellow-50 text-yellow-700'

    @classmethod
    def setUpTestData(cls):
        cls.sysadmin = _profile('cb_sysadmin', 'System Admin')
        cls.pm = _profile('cb_badge_pm', 'PM')
        cls.projects = {}
        for project_type in cls.BADGE:
            for suffix, pm in (('U', None), ('A', cls.pm)):
                cls.projects[(project_type, suffix)] = Project.objects.create(
                    project_id=f'CB-{project_type[:3].upper()}-{suffix}',
                    customer_name=f'Badge {project_type}', customer_phone='9876543210',
                    site_address='1 Badge Road', city='Delhi', project_type=project_type,
                    dc_capacity_kw=Decimal('10.00'), status='Draft', assigned_pm=pm)

    def test_each_type_gets_its_own_badge_in_both_tables(self):
        html = _flat(_client(self.sysadmin).get(reverse('subadmin_projects'))
                     .content.decode())
        labels = dict(Project.PROJECT_TYPE_CHOICES)
        for (project_type, suffix), project in self.projects.items():
            with self.subTest(type=project_type, table=suffix):
                row = re.search(re.escape(project.project_id) + r'</td>(.*?)</span>', html)
                self.assertIsNotNone(row, f'{project.project_id} row not found')
                badge = row.group(1)
                self.assertIn(self.BADGE[project_type], badge)
                self.assertNotIn(self.FALLBACK, badge)
                self.assertTrue(badge.rstrip().endswith(f'"> {labels[project_type]}'),
                                badge[-60:])


class QaqcHelpTextTests(TestCase):

    TEXT = 'May approve or reject submitted tasks on RESCO and CAPEX sites they can view.'

    def test_the_form_help_text(self):
        help_text = AdminUserEditForm.base_fields['is_qaqc'].help_text
        self.assertTrue(help_text.startswith(self.TEXT))
        self.assertIn('A rejection raises a punch point.', help_text)
        self.assertNotIn('RESCO task', help_text)

    def test_the_admin_user_edit_page(self):
        admin = _profile('cb_qa_admin', 'Admin')
        target = _profile('cb_qa_target', 'Site Engineer')
        html = _flat(_client(admin).get(
            reverse('admin_user_edit', args=[target.user.pk])).content.decode())
        self.assertIn('QA/QC — may approve or reject submitted tasks on RESCO and CAPEX '
                      'sites they can view; a rejection raises a punch point.', html)
        self.assertNotIn('submitted RESCO work', html)
