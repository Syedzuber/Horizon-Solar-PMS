"""
Site PM reassignment — rulings PM-1 to PM-4, PM-7 (narrowed to Admin, Q3), PM-10.

WHAT IS PINNED HERE
-------------------
site_pm.change_site_pm() is the one writer of Project.assigned_pm (PM-2). It:

    - refuses a deleted or Cancelled site, first assignment included (PM-1, Q6)
    - admits a CHANGE only from an Admin, with a reason (PM-7, PM-3)
    - closes the open SitePmAssignment row and opens the next (PM-3)
    - moves the outgoing PM's open (not Done) tasks on the site (PM-4)
    - logs one `pm_assigned` line

The five writers all call it; the Zoho webhook calls it outside its creation block so
a failure never loses the deal (C1). The source check at the bottom fails if anything
else in application code writes the column (C2).

PM-11 (HandoverMessageTests): an outgoing PM opening one of the 8 covered site pages
by GET is redirected to their dashboard with the handover message; everyone else gets
the page's existing refusal, byte for byte.

Run with:
    python manage.py test projects.tests_site_pm --settings=solarpms.test_settings
"""
import ast
import json
from decimal import Decimal
from importlib import import_module
from pathlib import Path
from unittest import mock

from django.apps import apps as django_apps
from django.conf import settings
from django.contrib import admin as django_admin
from django.contrib.auth.models import User
from django.contrib.messages import get_messages
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, connection, transaction
from django.test import Client, RequestFactory, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from .decorators import get_user_dashboard
from .models import (
    ActivityLog, AppendOnlyViolation, DeliveryChallan, Issue, Program, Project,
    ProjectPhase, SitePmAssignment, Task, UserProfile,
)
from .permissions import can_change_site_pm, user_can_view_project
from .site_pm import (
    SitePmChangeRefused, change_site_pm, open_tasks_held_by, with_outgoing_pm_task_count,
)
from .views import create_opex_site


def _profile(username, role, email=''):
    """A user whose signal-created profile carries `role`."""
    user = User.objects.create_user(
        username=username, password='x', email=email or f'{username}@example.com',
        first_name=username.title(), last_name='Test',
    )
    profile = user.profile
    profile.role = role
    profile.is_active = True
    profile.save()
    return profile


def _client(profile):
    c = Client(SERVER_NAME='localhost')
    c.force_login(profile.user)
    return c


def _site(name='PM Site', status='Active', **extra):
    return Project.objects.create(
        customer_name=name, customer_phone='9876543210', site_address='1 Sun Road',
        city='Lucknow', state='Uttar Pradesh', project_type='Residential',
        dc_capacity_kw=Decimal('5.00'), status=status, **extra,
    )


class _Fixture(TestCase):
    """One Active site whose PM is pm_a, given through the chokepoint, with a task of
    every shape a move has to get right."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = _profile('spm_admin', 'Admin')
        cls.sysadmin = _profile('spm_sysadmin', 'System Admin')
        cls.pm_a = _profile('spm_pm_a', 'PM')
        cls.pm_b = _profile('spm_pm_b', 'PM')
        cls.finance = _profile('spm_finance', 'Finance')
        cls.scm = _profile('spm_scm', 'SCM')
        cls.se = _profile('spm_se', 'Site Engineer')

        cls.site = _site()
        change_site_pm(cls.site, cls.pm_a, by=cls.admin,   # first assignment, no reason
                       expected_outgoing_pm=None)

        phase = ProjectPhase.objects.create(project=cls.site, phase_name='Execution',
                                            phase_order=1)
        order = iter(range(1, 100))

        def task(name, status='Not Started', role='PM', holder=None, **extra):
            return Task.objects.create(
                phase=phase, task_name=name, task_order=next(order), assigned_role=role,
                assigned_to=cls.pm_a if holder is None else holder, status=status, **extra)

        cls.t_not_started = task('Not started')
        cls.t_in_progress = task('In progress', status='In Progress')
        cls.t_blocked = task('Blocked', status='Blocked')
        cls.t_non_pm_role = task('SE-role task held by the PM', role='Site Engineer')
        cls.t_not_applicable = task('Not applicable', is_not_applicable=True,
                                    not_applicable_reason='n/a here')
        cls.submitted_at = timezone.now()
        cls.t_submitted = task('Submitted for approval', status='In Progress',
                               submitted_by=cls.pm_a, submitted_at=cls.submitted_at)
        cls.t_done = task('Done', status='Done', completed_at=timezone.now())
        cls.t_someone_else = task('Held by the SE', role='Site Engineer', holder=cls.se)
        cls.moving = [cls.t_not_started, cls.t_in_progress, cls.t_blocked, cls.t_non_pm_role,
                      cls.t_not_applicable, cls.t_submitted]

    def _change_via_view(self, actor, reason='PM moved to the Kanpur cluster', pm=None):
        return _client(actor).post(
            reverse('admin_assign_pm', args=[self.site.project_id]),
            {'pm_user_id': (pm or self.pm_b).pk, 'reason': reason,
             'expected_pm_id': self.pm_a.pk})

    def _site_pm_id(self):
        return Project.objects.values_list('assigned_pm_id', flat=True).get(pk=self.site.pk)


# ---------------------------------------------------------------------------
# VERIFY 1–3, C3 — a change through the real Admin screen
# ---------------------------------------------------------------------------

class ChangeThroughAdminScreenTests(_Fixture):

    def test_the_confirmation_count_matches_what_moves(self):
        counted = with_outgoing_pm_task_count(
            Project.objects.filter(pk=self.site.pk)).get().outgoing_pm_task_count
        self.assertEqual(counted, len(self.moving))
        response = self._change_via_view(self.admin)
        self.assertEqual(response.status_code, 302)
        msgs = [str(m) for m in get_messages(response.wsgi_request)]
        self.assertTrue(any(f'{counted} open tasks moved' in m for m in msgs), msgs)

    def test_outgoing_pm_loses_access_and_holds_no_open_task(self):
        self.assertTrue(user_can_view_project(self.pm_a.user, self.site))
        self._change_via_view(self.admin)
        site = Project.objects.get(pk=self.site.pk)
        self.assertFalse(user_can_view_project(self.pm_a.user, site))
        self.assertFalse(open_tasks_held_by(site, self.pm_a.pk).exists())

    def test_incoming_pm_sees_the_site_and_holds_the_moved_tasks(self):
        self.assertFalse(user_can_view_project(self.pm_b.user, self.site))
        self._change_via_view(self.admin)
        site = Project.objects.get(pk=self.site.pk)
        self.assertEqual(site.assigned_pm_id, self.pm_b.pk)
        self.assertTrue(user_can_view_project(self.pm_b.user, site))
        for t in self.moving:
            t.refresh_from_db()
            self.assertEqual(t.assigned_to_id, self.pm_b.pk, t.task_name)

    def test_done_tasks_keep_the_original_assignee(self):
        self._change_via_view(self.admin)
        self.t_done.refresh_from_db()
        self.assertEqual(self.t_done.assigned_to_id, self.pm_a.pk)

    def test_a_task_held_by_someone_else_is_untouched(self):
        self._change_via_view(self.admin)
        self.t_someone_else.refresh_from_db()
        self.assertEqual(self.t_someone_else.assigned_to_id, self.se.pk)

    def test_submitted_task_moves_its_assignee_but_keeps_its_submitter(self):
        """C3: awaiting approval is not Done, so it moves; who submitted it does not."""
        self._change_via_view(self.admin)
        self.t_submitted.refresh_from_db()
        self.assertEqual(self.t_submitted.assigned_to_id, self.pm_b.pk)
        self.assertEqual(self.t_submitted.submitted_by_id, self.pm_a.pk)
        self.assertEqual(self.t_submitted.submitted_at, self.submitted_at)
        self.assertEqual(self.t_submitted.status, 'In Progress')

    def test_history_closes_the_old_span_and_opens_the_new(self):
        self._change_via_view(self.admin, reason='  Workload  ')
        rows = list(SitePmAssignment.objects.filter(site=self.site).order_by('pk'))
        self.assertEqual(len(rows), 2)
        old, new = rows
        self.assertEqual(old.pm_id, self.pm_a.pk)
        self.assertIsNotNone(old.valid_to)
        self.assertEqual(new.pm_id, self.pm_b.pk)
        self.assertIsNone(new.valid_to)
        self.assertEqual(new.valid_from, old.valid_to)
        self.assertEqual(new.assigned_by_id, self.admin.pk)
        self.assertEqual(new.reason, 'Workload')
        self.assertFalse(new.is_backfilled)

    def test_one_log_line_with_the_shared_code(self):
        before = ActivityLog.objects.filter(project=self.site, action_code='pm_assigned').count()
        self._change_via_view(self.admin)
        lines = ActivityLog.objects.filter(project=self.site, action_code='pm_assigned')
        self.assertEqual(lines.count(), before + 1)
        line = lines.order_by('-pk').first()
        self.assertEqual(line.actor_id, self.admin.pk)
        self.assertIn(f'{len(self.moving)} open tasks moved', line.action)
        self.assertEqual(
            ActivityLog.objects.filter(project=self.site, entity_type='Task').count(), 0,
            'a PM change writes one summary line, not one line per moved task')

    def test_reason_is_required_on_a_change(self):
        response = self._change_via_view(self.admin, reason='   ')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._site_pm_id(), self.pm_a.pk)
        self.assertEqual(SitePmAssignment.objects.filter(site=self.site).count(), 1)
        self.t_not_started.refresh_from_db()
        self.assertEqual(self.t_not_started.assigned_to_id, self.pm_a.pk)

    def test_same_pm_is_a_no_op(self):
        result = change_site_pm(self.site, self.pm_a, by=self.admin,
                                expected_outgoing_pm=self.pm_a, reason='again')
        self.assertFalse(result.changed)
        self.assertEqual(result.tasks_moved, 0)
        self.assertEqual(SitePmAssignment.objects.filter(site=self.site).count(), 1)


# ---------------------------------------------------------------------------
# VERIFY 4 — the database refuses two open rows
# ---------------------------------------------------------------------------

class OneOpenRowTests(_Fixture):

    def test_two_open_rows_for_one_site_are_refused(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                SitePmAssignment.objects.create(site=self.site, pm=self.pm_b)

    def test_rows_are_append_only(self):
        row = SitePmAssignment.objects.get(site=self.site)
        with self.assertRaises(AppendOnlyViolation):
            row.save()
        with self.assertRaises(AppendOnlyViolation):
            row.delete()


# ---------------------------------------------------------------------------
# VERIFY 5 — Cancelled is refused on every path
# ---------------------------------------------------------------------------

class CancelledSiteTests(_Fixture):

    def setUp(self):
        Project.objects.filter(pk=self.site.pk).update(status='Cancelled')

    def test_change_on_a_cancelled_site_is_refused(self):
        with self.assertRaises(SitePmChangeRefused):
            change_site_pm(self.site, self.pm_b, by=self.admin,
                           expected_outgoing_pm=self.pm_a, reason='x')
        response = self._change_via_view(self.admin)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._site_pm_id(), self.pm_a.pk)
        self.t_not_started.refresh_from_db()
        self.assertEqual(self.t_not_started.assigned_to_id, self.pm_a.pk)

    def test_first_assignment_on_a_cancelled_site_is_refused(self):
        empty = _site('Cancelled, no PM', status='Cancelled')
        response = _client(self.sysadmin).post(
            reverse('subadmin_projects'),
            {'project_id': empty.pk, 'pm_user_id': self.pm_b.pk})
        self.assertEqual(response.status_code, 302)
        empty.refresh_from_db()
        self.assertIsNone(empty.assigned_pm_id)
        self.assertFalse(SitePmAssignment.objects.filter(site=empty).exists())

    def test_the_admin_list_offers_no_control_on_a_cancelled_site(self):
        response = _client(self.admin).get(reverse('admin_project_list'))
        self.assertNotContains(response, 'data-testid="change-pm-button"')


# ---------------------------------------------------------------------------
# VERIFY 6 — who may change (PM-7, Q3: Admin only)
# ---------------------------------------------------------------------------

class WhoMayChangeTests(_Fixture):

    def test_pm_finance_and_scm_get_403_with_a_body(self):
        for actor in (self.pm_a, self.pm_b, self.finance, self.scm):
            response = self._change_via_view(actor)
            self.assertEqual(response.status_code, 403, actor.role)
            self.assertTrue(response.content.strip(), f'{actor.role}: empty 403 body')
        self.assertEqual(self._site_pm_id(), self.pm_a.pk)

    def test_the_chokepoint_refuses_every_non_admin_change(self):
        for actor in (self.pm_a, self.finance, self.scm, self.sysadmin, None):
            with self.assertRaises(PermissionDenied):
                change_site_pm(self.site, self.pm_b, by=actor,
                               expected_outgoing_pm=self.pm_a, reason='x')
        self.assertEqual(self._site_pm_id(), self.pm_a.pk)

    def test_helper(self):
        self.assertTrue(can_change_site_pm(self.admin.user))
        for p in (self.sysadmin, self.pm_a, self.finance, self.scm):
            self.assertFalse(can_change_site_pm(p.user), p.role)
        bare = User.objects.create_user('spm_bare', password='x')
        UserProfile.objects.filter(user=bare).delete()
        self.assertFalse(can_change_site_pm(User.objects.get(pk=bare.pk)))

    def test_system_admin_screen_only_fills_an_empty_seat(self):
        with self.assertRaises(SitePmChangeRefused):
            change_site_pm(self.site, self.pm_b, by=self.sysadmin,
                           expected_outgoing_pm=None, first_assignment_only=True)
        empty = _site('Empty seat', status='Draft')
        response = _client(self.sysadmin).post(
            reverse('subadmin_projects'),
            {'project_id': empty.pk, 'pm_user_id': self.pm_b.pk})
        self.assertEqual(response.status_code, 302)
        row = SitePmAssignment.objects.get(site=empty)
        self.assertEqual((row.pm_id, row.assigned_by_id, row.reason), (self.pm_b.pk, self.sysadmin.pk, ''))
        self.assertTrue(ActivityLog.objects.filter(project=empty, action_code='pm_assigned').exists())


# ---------------------------------------------------------------------------
# The admin list's Change PM control (Q2)
# ---------------------------------------------------------------------------

class AdminListControlTests(_Fixture):

    def test_change_control_carries_the_count_and_current_pm(self):
        response = _client(self.admin).get(reverse('admin_project_list'))
        self.assertContains(response, 'data-testid="change-pm-button"')
        self.assertContains(response, f'data-open-count="{len(self.moving)}"')
        self.assertContains(response, f'data-current-pm-id="{self.pm_a.pk}"')
        self.assertContains(response, 'name="reason"')


# ---------------------------------------------------------------------------
# The first-assignment writers
# ---------------------------------------------------------------------------

class FirstAssignmentWriterTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.pm = _profile('spm_creator_pm', 'PM')
        cls.admin = _profile('spm_creator_admin', 'Admin')
        _profile('spm_alert_admin', 'Admin')

    def _assert_first_row(self, project, pm, by):
        row = SitePmAssignment.objects.get(site=project)
        self.assertEqual((row.pm_id, row.assigned_by_id), (pm.pk, by.pk if by else None))
        self.assertIsNone(row.valid_to)

    def test_project_create(self):
        response = _client(self.pm).post(reverse('project_create'), {
            'customer_name': 'Created With PM', 'customer_phone': '9876543210',
            'site_address': '1 Sun Road', 'city': 'Lucknow', 'state': 'Uttar Pradesh',
            'dc_capacity_kw': '5.00', 'project_type': 'Residential'})
        self.assertEqual(response.status_code, 302)
        project = Project.objects.get(customer_name='Created With PM')
        self.assertEqual(project.assigned_pm_id, self.pm.pk)
        self._assert_first_row(project, self.pm, self.pm)

    def _program(self):
        return Program.objects.create(name='SPM Tender', program_type='OPEX',
                                      client_name='Client', status='Active',
                                      short_tender_code='SPMT')

    def _opex_data(self, code):
        return {'site_code': code, 'customer_phone': '9876543210',
                'site_address': '1 Sun Road', 'city': 'Lucknow',
                'state': 'Uttar Pradesh', 'dc_capacity_kw': '5.00'}

    def test_create_opex_site_by_a_pm(self):
        site, form = create_opex_site(self._program(), self._opex_data('SPM001'),
                                      self.pm.user, profile=self.pm)
        self.assertIsNotNone(site, form.errors)
        site.refresh_from_db()
        self.assertEqual(site.assigned_pm_id, self.pm.pk)
        self._assert_first_row(site, self.pm, self.pm)

    def test_create_opex_site_by_an_admin_leaves_it_unassigned(self):
        site, form = create_opex_site(self._program(), self._opex_data('SPM002'),
                                      self.admin.user, profile=self.admin)
        self.assertIsNotNone(site, form.errors)
        site.refresh_from_db()
        self.assertIsNone(site.assigned_pm_id)
        self.assertFalse(SitePmAssignment.objects.filter(site=site).exists())


@override_settings(ZOHO_WEBHOOK_SECRET='spmsecret123')
class WebhookTests(TestCase):
    """C1: the PM step is separate from creation; its failure never loses the deal."""

    @classmethod
    def setUpTestData(cls):
        cls.pm = _profile('spm_zoho_pm', 'PM', email='zoho.pm@example.com')
        _profile('spm_zoho_admin', 'Admin')

    def _post(self, deal_id):
        payload = {'id': deal_id, 'Stage': 'Closed Won', 'Account_Name': f'Deal {deal_id}',
                   'Assign_PM': 'zoho.pm@example.com', 'Capacity_kW': '5'}
        return Client(SERVER_NAME='localhost').post(
            reverse('zoho_webhook'), data=json.dumps(payload),
            content_type='application/json', HTTP_X_WEBHOOK_TOKEN='spmsecret123')

    @mock.patch('projects.views.send_raw_email')
    @mock.patch('projects.views.send_notification')
    def test_matched_pm_is_assigned_through_the_chokepoint(self, _notify, _email):
        self.assertEqual(self._post('SPM-Z1').status_code, 200)
        project = Project.objects.get(zoho_deal_id='SPM-Z1')
        self.assertEqual(project.assigned_pm_id, self.pm.pk)
        row = SitePmAssignment.objects.get(site=project)
        self.assertEqual((row.pm_id, row.assigned_by_id), (self.pm.pk, None))
        _email.assert_not_called()

    @mock.patch('projects.views.send_raw_email')
    @mock.patch('projects.views.send_notification')
    def test_a_failed_pm_step_keeps_the_project_and_alerts_admin(self, _notify, _email):
        with mock.patch('projects.views.change_site_pm', side_effect=RuntimeError('boom')):
            self.assertEqual(self._post('SPM-Z2').status_code, 200)
        project = Project.objects.get(zoho_deal_id='SPM-Z2')
        self.assertIsNone(project.assigned_pm_id)
        self.assertFalse(SitePmAssignment.objects.filter(site=project).exists())
        _email.assert_called_once()                    # the existing no-PM path ran
        self.assertEqual(_notify.call_args.kwargs['channels'], ['in_app'])


# ---------------------------------------------------------------------------
# ProjectAdmin, the backfill, and the one-writer source check
# ---------------------------------------------------------------------------

class ProjectAdminReadOnlyTests(TestCase):

    def test_assigned_pm_is_read_only(self):
        model_admin = django_admin.site._registry[Project]
        request = RequestFactory().get('/')
        request.user = User.objects.create_superuser('spm_su', 'su@example.com', 'pw')
        project = _site('Admin form')
        self.assertIn('assigned_pm', model_admin.get_readonly_fields(request, project))
        self.assertNotIn('assigned_pm', model_admin.get_form(request, project).base_fields)
        self.assertNotIn('assigned_pm', tuple(model_admin.list_editable or ()))


class BackfillTests(TestCase):

    def test_one_open_backfilled_row_per_site_with_a_pm_deleted_included(self):
        pm = _profile('spm_backfill_pm', 'PM')
        live = _site('Live', assigned_pm=pm)              # tests may write the column directly
        gone = _site('Deleted', assigned_pm=pm, is_deleted=True)
        none = _site('No PM')
        migration = import_module('projects.migrations.0116_site_pm_assignment')
        migration.backfill(django_apps, None)

        rows = {r.site_id: r for r in SitePmAssignment.objects.all()}
        self.assertEqual(set(rows), {live.pk, gone.pk})
        self.assertNotIn(none.pk, rows)
        for project in (live, gone):
            row = rows[project.pk]
            self.assertTrue(row.is_backfilled)
            self.assertIsNone(row.valid_to)
            self.assertIsNone(row.assigned_by_id)
            self.assertEqual(row.valid_from, project.created_at)

        migration.unbackfill(django_apps, None)
        self.assertFalse(SitePmAssignment.objects.exists())


# Application files outside site_pm.py that may write Project.assigned_pm. Each entry
# must still write it — test_allowlist_has_no_stale_entries fails otherwise.
#   seed_opex_test_data: builds a demo project with form.save(commit=False) and sets
#   the PM before its first save; a seed, not a product path.
ASSIGNED_PM_WRITER_ALLOWLIST = frozenset({
    'projects/management/commands/seed_opex_test_data.py',
})
CHOKEPOINT = 'projects/site_pm.py'
_FIELDS = {'assigned_pm', 'assigned_pm_id'}
_WRITE_CALLS = {'create', 'update', 'get_or_create', 'update_or_create', 'Project'}


def _assigned_pm_writes(source):
    """Line numbers in `source` that write assigned_pm: attribute assignment,
    setattr, or a keyword / defaults key on create/update/Project(...). Filters and
    Q() objects take the same keyword but are reads, so only write calls are named."""
    hits = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(t, ast.Attribute) and t.attr in _FIELDS for t in targets):
                hits.append(node.lineno)
        elif isinstance(node, ast.Call):
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, 'id', '')
            if name == 'setattr' and len(node.args) > 1 \
                    and isinstance(node.args[1], ast.Constant) and node.args[1].value in _FIELDS:
                hits.append(node.lineno)
            if name in _WRITE_CALLS:
                for kw in node.keywords:
                    if kw.arg in _FIELDS:
                        hits.append(node.lineno)
                    if kw.arg == 'defaults' and isinstance(kw.value, ast.Dict) and any(
                            isinstance(k, ast.Constant) and k.value in _FIELDS
                            for k in kw.value.keys):
                        hits.append(node.lineno)
    return hits


def _application_files():
    root = Path(settings.BASE_DIR)
    for top in ('projects', 'solarpms'):
        for path in sorted((root / top).rglob('*.py')):
            rel = path.relative_to(root).as_posix()
            if '/migrations/' in rel or path.name.startswith('tests') or '/tests/' in rel:
                continue
            yield rel, path.read_text(encoding='utf-8-sig')


class OneWriterSourceTests(TestCase):

    def test_only_the_chokepoint_writes_assigned_pm(self):
        offenders = []
        for rel, source in _application_files():
            if rel == CHOKEPOINT or rel in ASSIGNED_PM_WRITER_ALLOWLIST:
                continue
            offenders += [f'{rel}:{line}' for line in _assigned_pm_writes(source)]
        self.assertEqual(
            offenders, [],
            'Project.assigned_pm is written outside site_pm.change_site_pm() (PM-2). '
            'Route the write through change_site_pm() so the history row, the task move '
            'and the Admin / Cancelled rules all apply.')

    def test_the_chokepoint_itself_is_seen_by_the_scan(self):
        source = dict(_application_files())[CHOKEPOINT]
        self.assertTrue(_assigned_pm_writes(source),
                        'the scan no longer recognises the chokepoint\'s own write — '
                        'it would pass vacuously')

    def test_allowlist_has_no_stale_entries(self):
        files = dict(_application_files())
        for rel in ASSIGNED_PM_WRITER_ALLOWLIST:
            self.assertIn(rel, files)
            self.assertTrue(_assigned_pm_writes(files[rel]),
                            f'{rel} no longer writes assigned_pm — remove it from the allowlist')


# ---------------------------------------------------------------------------
# PM-11 — the handover message for an outgoing PM
# ---------------------------------------------------------------------------

class HandoverMessageTests(TestCase):
    """One OPEX site handed from pm_a to pm_b through the chokepoint, with a task, an
    issue and a delivery challan so every covered page has something to resolve."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = _profile('ho_admin', 'Admin')
        cls.pm_a = _profile('ho_pm_a', 'PM')
        cls.pm_b = _profile('ho_pm_b', 'PM')
        cls.pm_c = _profile('ho_pm_c', 'PM')
        cls.pm_never = _profile('ho_pm_never', 'PM')
        cls.finance = _profile('ho_finance', 'Finance')
        cls.scm = _profile('ho_scm', 'SCM')
        cls.se = _profile('ho_se', 'Site Engineer')

        cls.site = Project.objects.create(
            customer_name='Handover Site', customer_phone='9876543210',
            site_address='1 Sun Road', city='Lucknow', state='Uttar Pradesh',
            project_type='OPEX', dc_capacity_kw=Decimal('5.00'), status='Active',
            project_id='HOSITE01')
        change_site_pm(cls.site, cls.pm_a, by=cls.admin, expected_outgoing_pm=None)
        phase = ProjectPhase.objects.create(project=cls.site, phase_name='Execution',
                                            phase_order=1)
        cls.task = Task.objects.create(phase=phase, task_name='Open task', task_order=1,
                                       assigned_role='PM', assigned_to=cls.pm_a)
        cls.issue = Issue.objects.create(project=cls.site, title='Handover issue',
                                         raised_by=cls.pm_a)
        cls.challan = DeliveryChallan.objects.create(
            project=cls.site, dc_number='DC-HO-1', dc_date=timezone.localdate(),
            status=DeliveryChallan.EXPECTED, created_by=cls.scm)
        change_site_pm(cls.site, cls.pm_b, by=cls.admin, expected_outgoing_pm=cls.pm_a,
                       reason='Workload')

    def pages(self):
        """(name, url, existing refusal status) for the 8 covered pages (R3)."""
        pid = self.site.project_id
        return [
            ('project_overview', reverse('project_overview', args=[pid]), 404),
            ('task_detail', reverse('task_detail', args=[pid, self.task.pk]), 404),
            ('issue_detail', reverse('issue_detail', args=[self.issue.pk]), 404),
            ('project_timeline', reverse('project_timeline', args=[pid]), 404),
            ('delivery_challan_detail',
             reverse('delivery_challan_detail', args=[pid, self.challan.pk]), 404),
            ('cod_record', reverse('cod_record', args=[pid]), 404),
            ('site_qaqc', reverse('site_qaqc', args=[pid]), 404),
            ('boq_detail', reverse('boq_detail', args=[pid]), 403),
        ]

    def _closed_date(self, profile, site=None):
        valid_to = (SitePmAssignment.objects
                    .filter(site=site or self.site, pm=profile, valid_to__isnull=False)
                    .latest('valid_to').valid_to)
        return f'{timezone.localtime(valid_to):%d %b %Y}'

    def _expected(self, profile, current):
        return (f'{self.site.project_id} was handed over from you on '
                f'{self._closed_date(profile)}. Its current PM is '
                f'{current.user.get_full_name()}. Contact Admin if you still need access.')

    def _before(self, client, url):
        """The response as it was before PM-11: the handover branch switched off, so
        the view's own refusal runs exactly as it did."""
        with mock.patch('projects.site_pm._handover_redirect', return_value=None):
            return client.get(url)

    # VERIFY 1
    def test_outgoing_pm_is_redirected_with_the_message_on_every_page(self):
        dashboard = get_user_dashboard(self.pm_a.user)
        for name, url, _status in self.pages():
            # A fresh client per page: the redirect is not followed here, so one client
            # would carry page 1's unread message into page 2's list.
            response = _client(self.pm_a).get(url)
            self.assertRedirects(response, dashboard, fetch_redirect_response=False,
                                 msg_prefix=name)
            msgs = [str(m) for m in get_messages(response.wsgi_request)]
            self.assertEqual(msgs, [self._expected(self.pm_a, self.pm_b)], name)

    def test_the_message_shows_on_the_dashboard(self):
        response = _client(self.pm_a).get(
            reverse('project_overview', args=[self.site.project_id]), follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'was handed over from you on')
        self.assertContains(response, 'Its current PM is Ho_Pm_B Test.')

    # VERIFY 2
    def test_a_to_b_to_c_names_the_current_pm_to_both(self):
        change_site_pm(self.site, self.pm_c, by=self.admin, expected_outgoing_pm=self.pm_b,
                       reason='Second change')
        url = reverse('project_overview', args=[self.site.project_id])
        for outgoing in (self.pm_a, self.pm_b):
            response = _client(outgoing).get(url)
            msgs = [str(m) for m in get_messages(response.wsgi_request)]
            self.assertEqual(msgs, [self._expected(outgoing, self.pm_c)],
                             outgoing.user.username)
            self.assertIn('Its current PM is Ho_Pm_C Test.', msgs[0])

    # VERIFY 3
    def test_a_pm_with_no_row_gets_the_unchanged_refusal(self):
        client = _client(self.pm_never)
        for name, url, status in self.pages():
            response = client.get(url)
            before = self._before(client, url)
            self.assertEqual(response.status_code, status, name)
            self.assertEqual(response.status_code, before.status_code, name)
            self.assertEqual(response.content, before.content, name)
        boq = client.get(reverse('boq_detail', args=[self.site.project_id]))
        self.assertEqual(boq.content, b'', 'boq_detail keeps its empty 403')

    # VERIFY 4
    def test_finance_scm_and_se_with_no_row_are_unchanged(self):
        """Status always; body too wherever it is a refusal. A 200 page's body carries a
        fresh CSRF token per request, so two renders are never byte-equal."""
        for profile in (self.finance, self.scm, self.se):
            client = _client(profile)
            for name, url, _status in self.pages():
                response = client.get(url)
                before = self._before(client, url)
                label = f'{profile.role} on {name}'
                self.assertEqual(response.status_code, before.status_code, label)
                self.assertNotEqual(response.status_code, 302, label)
                if response.status_code != 200:
                    self.assertEqual(response.content, before.content, label)

    # VERIFY 5
    def test_reassigned_away_then_back_has_normal_access_and_no_message(self):
        change_site_pm(self.site, self.pm_a, by=self.admin, expected_outgoing_pm=self.pm_b,
                       reason='Back again')
        response = _client(self.pm_a).get(
            reverse('project_overview', args=[self.site.project_id]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(get_messages(response.wsgi_request)), [])

    # VERIFY 6 (R4)
    def test_post_and_htmx_requests_keep_the_404(self):
        client = _client(self.pm_a)
        pid = self.site.project_id
        posts = [reverse('task_status_update', args=[pid, self.task.pk]),
                 reverse('cod_record', args=[pid]),
                 reverse('site_qaqc', args=[pid])]
        for url in posts:
            self.assertEqual(client.post(url, {'status': 'In Progress'}).status_code, 404, url)
        htmx = client.get(reverse('task_detail', args=[pid, self.task.pk]),
                          HTTP_HX_REQUEST='true')
        self.assertEqual(htmx.status_code, 404)

    def test_a_closed_backfilled_row_triggers_the_message(self):
        """R1: is_backfilled is ignored; only valid_to matters."""
        old = _site('Backfilled site', assigned_pm=self.pm_c)   # tests may write it directly
        SitePmAssignment.objects.create(site=old, pm=self.pm_c, is_backfilled=True,
                                        valid_from=old.created_at)
        change_site_pm(old, self.pm_b, by=self.admin, expected_outgoing_pm=self.pm_c,
                       reason='First real change')
        response = _client(self.pm_c).get(reverse('project_overview', args=[old.project_id]))
        self.assertEqual(response.status_code, 302)
        msgs = [str(m) for m in get_messages(response.wsgi_request)]
        self.assertEqual(msgs, [
            f'{old.project_id} was handed over from you on {self._closed_date(self.pm_c, old)}. '
            f'Its current PM is Ho_Pm_B Test. Contact Admin if you still need access.'])

    # VERIFY 7
    def test_an_allowed_request_pays_no_query_for_the_handover_check(self):
        client = _client(self.pm_b)
        url = reverse('project_overview', args=[self.site.project_id])
        client.get(url)                                   # warm per-process caches
        with CaptureQueriesContext(connection) as plain:
            self.assertEqual(client.get(url).status_code, 200)
        with mock.patch('projects.site_pm.pm_handover_notice',
                        side_effect=AssertionError('called on an allowed request')):
            with CaptureQueriesContext(connection) as guarded:
                self.assertEqual(client.get(url).status_code, 200)
        self.assertEqual(len(plain), len(guarded))
        self.assertFalse([q for q in plain.captured_queries
                          if 'sitepmassignment' in q['sql'].lower()])
