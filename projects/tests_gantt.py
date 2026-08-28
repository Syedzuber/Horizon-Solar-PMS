"""
Tests for the Residential Gantt feature (Phase 1.5).

Covers the compute-live engine (chain math, per-phase buffer cascade, external-task
display width, milestone markers, null-activation), the render-ready grid builder,
role-gated visibility in project_overview, the admin buffer round-trip, and a
display-map desync guard against the Residential template.

Engine dates are DERIVED from Project.activated_at + Task.duration_days — no task has
a stored start_date, and due_date is intentionally left null here (mirrors production).
The test Client uses SERVER_NAME='localhost' to satisfy the env-driven ALLOWED_HOSTS.
"""
import datetime
from decimal import Decimal

from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone
from django.contrib.auth.models import User

from .models import Project, ProjectPhase, Task, UserProfile, SystemSettings
from .utils import (
    compute_gantt_schedule, build_gantt_view, build_residential_phases,
    get_residential_template_task_names,
)
from .gantt_constants import (
    GANTT_TASK_DISPLAY_NAME_MAP, GANTT_PHASE_DISPLAY_NAME_MAP,
)

ACTIVATED = timezone.make_aware(datetime.datetime(2026, 3, 18, 10, 0))  # Wed 18 Mar 2026
ACT_DATE  = ACTIVATED.date()


def _make_user(username, role):
    user = User.objects.create_user(username=username, password='pw12345')
    profile = user.profile          # auto-created by post_save signal
    profile.role = role
    profile.save(update_fields=['role'])
    return user, profile


def _make_project(pm, project_type='Residential', activated=ACTIVATED, status='Active'):
    project = Project.objects.create(
        customer_name='Acme', customer_phone='9876543210', site_address='1 Sun Rd',
        city='Lucknow', project_type=project_type, capacity_kw=Decimal('3.00'),
        contract_value=Decimal('100000.00'),
        target_commissioning_date=datetime.date(2026, 12, 1),
        status=status, assigned_pm=pm,
    )
    if activated is not None:
        project.activated_at = activated
        project.save(update_fields=['activated_at'])
    return project


def _phase(project, name, order):
    return ProjectPhase.objects.create(project=project, phase_name=name, phase_order=order)


def _task(phase, name, order, dur, ttype=Task.INTERNAL):
    return Task.objects.create(
        phase=phase, task_name=name, task_order=order,
        assigned_role=Task.PM, task_type=ttype, duration_days=dur,
    )


def _by_name(rows):
    return {r['task_name']: r for r in rows}


class GanttEngineTests(TestCase):
    def setUp(self):
        _, self.pm = _make_user('pm_eng', 'PM')

    def test_anchor_is_activation_date(self):
        p = _make_project(self.pm)
        ph = _phase(p, 'Sales & Documentation', 1)
        _task(ph, 'A', 1, 2)
        rows = compute_gantt_schedule(p, 0, 0)
        self.assertEqual(rows[0]['start'], ACT_DATE)
        self.assertEqual(rows[0]['end'], ACT_DATE + datetime.timedelta(days=2))

    def test_sequential_internal_chain(self):
        p = _make_project(self.pm)
        ph = _phase(p, 'P1', 1)
        _task(ph, 'A', 1, 2)
        _task(ph, 'B', 2, 3)
        r = _by_name(compute_gantt_schedule(p, 0, 0))
        self.assertEqual(r['A']['end'], ACT_DATE + datetime.timedelta(days=2))
        self.assertEqual(r['B']['start'], r['A']['end'])          # B starts where A ends
        self.assertEqual(r['B']['end'], ACT_DATE + datetime.timedelta(days=5))

    def test_buffer_cascade_divergence_per_phase(self):
        p = _make_project(self.pm)
        for i in range(1, 4):                                     # 3 phases, 2 internal tasks each
            ph = _phase(p, f'P{i}', i)
            _task(ph, f'P{i}-a', 1, 2)
            _task(ph, f'P{i}-b', 2, 1)
        buffer = 5
        internal = _by_name(compute_gantt_schedule(p, 0, 0))
        client   = _by_name(compute_gantt_schedule(p, buffer, 0))
        for name, irow in internal.items():
            crow = client[name]
            shift = (irow['phase_order'] - 1) * buffer            # phase p diverges by (p-1)*buffer
            self.assertEqual((crow['start'] - irow['start']).days, shift, name)
            self.assertEqual((crow['end'] - irow['end']).days, shift, name)

    def test_buffer_zero_matches_raw_chain(self):
        p = _make_project(self.pm)
        ph = _phase(p, 'P1', 1); _task(ph, 'A', 1, 2)
        ph2 = _phase(p, 'P2', 2); _task(ph2, 'B', 1, 2)
        a = compute_gantt_schedule(p, 0, 0)
        b = compute_gantt_schedule(p, 0, 99)                      # ext_min irrelevant (no external tasks)
        self.assertEqual([(r['start'], r['end']) for r in a],
                         [(r['start'], r['end']) for r in b])

    def test_external_width_and_parallel_non_blocking(self):
        p = _make_project(self.pm)
        ph = _phase(p, 'P1', 1)
        _task(ph, 'A', 1, 2)                                      # internal, dur 2
        _task(ph, 'X', 2, 1, ttype=Task.EXTERNAL)                # external, own dur 1
        _task(ph, 'B', 3, 3)                                      # internal after external
        r = _by_name(compute_gantt_schedule(p, 0, external_min_days=5))
        self.assertTrue(r['X']['is_external'])
        self.assertFalse(r['X']['is_marker'])
        self.assertEqual((r['X']['end'] - r['X']['start']).days, 5)   # floored to ext_min
        self.assertEqual(r['X']['start'], r['A']['end'])             # external starts at chain position
        self.assertEqual(r['B']['start'], r['A']['end'])            # external did NOT advance the chain

    def test_zero_duration_internal_is_marker(self):
        p = _make_project(self.pm)
        ph = _phase(p, 'P1', 1)
        _task(ph, 'M', 1, 0)                                      # 0-duration internal milestone
        _task(ph, 'B', 2, 2)
        r = _by_name(compute_gantt_schedule(p, 0, 0))
        self.assertTrue(r['M']['is_marker'])
        self.assertEqual(r['M']['start'], r['M']['end'])
        self.assertEqual(r['M']['start'], ACT_DATE)
        self.assertEqual(r['B']['start'], ACT_DATE)               # marker didn't advance the chain

    def test_null_activation_returns_empty(self):
        p = _make_project(self.pm, activated=None, status='Draft')
        _phase(p, 'P1', 1)
        self.assertEqual(compute_gantt_schedule(p, 0, 0), [])


class GanttHybridDateSourceTests(TestCase):
    """Stored due_date drives the bar when present; else the computed chain is used."""

    def setUp(self):
        _, self.pm = _make_user('pm_hy', 'PM')

    def _set_due(self, task, d):
        task.due_date = d
        task.save(update_fields=['due_date'])

    def test_stored_due_date_drives_end_and_downstream(self):
        p = _make_project(self.pm)
        ph = _phase(p, 'P1', 1)
        a = _task(ph, 'A', 1, 2)                                   # computed end would be ACT+2
        b = _task(ph, 'B', 2, 3)                                   # null -> computed
        self._set_due(a, ACT_DATE + datetime.timedelta(days=10))  # PM pushes A's due date out
        r = _by_name(compute_gantt_schedule(p, 0, 0))
        self.assertEqual(r['A']['end'], ACT_DATE + datetime.timedelta(days=10))   # stored wins
        self.assertEqual(r['B']['start'], r['A']['end'])                          # B chains off it
        self.assertEqual(r['B']['end'], ACT_DATE + datetime.timedelta(days=13))

    def test_inverted_due_date_is_guarded(self):
        p = _make_project(self.pm)
        ph = _phase(p, 'P1', 1)
        a = _task(ph, 'A', 1, 2)
        self._set_due(a, ACT_DATE - datetime.timedelta(days=5))    # earlier than its start
        r = _by_name(compute_gantt_schedule(p, 0, 0))
        self.assertGreaterEqual(r['A']['end'], r['A']['start'])    # never negative width
        self.assertEqual(r['A']['end'], ACT_DATE)

    def test_buffer_offset_applies_to_stored_dates(self):
        p = _make_project(self.pm)
        ph1 = _phase(p, 'P1', 1); a = _task(ph1, 'A', 1, 2)
        ph2 = _phase(p, 'P2', 2); b = _task(ph2, 'B', 1, 3)
        self._set_due(a, ACT_DATE + datetime.timedelta(days=4))
        internal = _by_name(compute_gantt_schedule(p, 0, 0))
        client   = _by_name(compute_gantt_schedule(p, 5, 0))
        self.assertEqual((client['A']['end'] - internal['A']['end']).days, 0)   # phase 1: no offset
        self.assertEqual((client['B']['start'] - internal['B']['start']).days, 5)  # phase 2: +5

    def test_zero_duration_with_due_date_is_marker_at_due_date(self):
        p = _make_project(self.pm)
        ph = _phase(p, 'P1', 1)
        m = _task(ph, 'M', 1, 0)
        self._set_due(m, ACT_DATE + datetime.timedelta(days=6))
        r = _by_name(compute_gantt_schedule(p, 0, 0))
        self.assertTrue(r['M']['is_marker'])
        self.assertEqual(r['M']['start'], r['M']['end'])
        self.assertEqual(r['M']['start'], ACT_DATE + datetime.timedelta(days=6))


class GanttGridBuilderTests(TestCase):
    def setUp(self):
        _, self.pm = _make_user('pm_grid', 'PM')

    def test_weeks_and_cells(self):
        p = _make_project(self.pm)
        ph = _phase(p, 'P1', 1)
        _task(ph, 'A', 1, 10)                                     # spans ~2 weeks
        grid = build_gantt_view(compute_gantt_schedule(p, 0, 0))
        self.assertTrue(len(grid['weeks']) >= 2)
        row = grid['rows'][0]
        self.assertTrue(row['has_dates'])
        self.assertTrue(any(c['filled'] for c in row['cells']))
        self.assertTrue(any(c['first'] for c in row['cells']))
        self.assertTrue(any(c['last'] for c in row['cells']))

    def test_empty_rows_no_weeks(self):
        grid = build_gantt_view([])
        self.assertEqual(grid['weeks'], [])
        self.assertEqual(grid['rows'], [])

    def test_client_label_mapping_and_fallback(self):
        p = _make_project(self.pm)
        ph = _phase(p, 'Design', 1)
        _task(ph, 'SLD', 1, 2)                                    # mapped in the constants
        _task(ph, 'Nonexistent Custom Task', 2, 2)               # unmapped -> falls back
        grid = build_gantt_view(
            compute_gantt_schedule(p, 0, 0),
            GANTT_PHASE_DISPLAY_NAME_MAP, GANTT_TASK_DISPLAY_NAME_MAP,
        )
        labels = {r['label'] for r in grid['rows']}
        self.assertIn('Electrical Design (Single-Line Diagram)', labels)   # mapped
        self.assertIn('Nonexistent Custom Task', labels)                   # fallback, never blank
        self.assertEqual(grid['rows'][0]['phase_label'], 'System Design')  # phase mapped


class GanttRoleVisibilityTests(TestCase):
    """Which roles see the CLIENT Gantt view on project_overview.

    THE SUBJECT HERE IS ROLE GATING, NOT PROJECT SCOPE. Every actor below is therefore
    given a genuine relationship to the project before its role is tested — the per-site
    roles by assignment, the portfolio roles by remit.

    That was not true before the 0.2 lockdown. These tests used to log in a bare
    Coordinator / Site Engineer / Design user with NO connection to the project and still
    get a 200 from project_overview, because that endpoint's guard only ever checked
    `role == 'PM'`. That is audit finding 6, and closing it turned these into 404s —
    KeyError on ctx['gantt_can_view_client'], since a 404 response carries no context.

    The fixtures below were repaired rather than the gate loosened: an unrelated
    coordinator reaching another PM's project was never the behaviour this file meant to
    assert, only a convenient way to obtain a non-PM user.
    """

    def setUp(self):
        self.client = Client(SERVER_NAME='localhost')
        self.pm_user, self.pm = _make_user('pm_view', 'PM')
        self.project = _make_project(self.pm)
        ph = _phase(self.project, 'P1', 1)
        self.task = _task(ph, 'A', 1, 2)
        self.url = reverse('project_overview', args=[self.project.project_id])

    def _ctx(self, user):
        self.client.force_login(user)
        return self.client.get(self.url).context

    def test_pm_owner_sees_client_view(self):
        ctx = self._ctx(self.pm_user)
        self.assertTrue(ctx['gantt_available'])
        self.assertTrue(ctx['gantt_can_view_client'])
        self.assertIsNotNone(ctx['gantt_client'])

    def test_coordinator_and_ceo_see_client_view(self):
        # The coordinator is coordinator OF THIS PROJECT; the CEO is portfolio-wide.
        coord_user, coord = _make_user('coord_v', 'Project Coordinator')
        self.project.coordinators.add(coord)
        ceo_user, _ = _make_user('ceo_v', 'CEO')

        for user, role in ((coord_user, 'Project Coordinator'), (ceo_user, 'CEO')):
            ctx = self._ctx(user)
            self.assertTrue(ctx['gantt_can_view_client'], role)
            self.assertIsNotNone(ctx['gantt_client'], role)

    def test_other_roles_get_no_client_rows(self):
        # Site Engineer reaches this project by holding a task on it, Design by being its
        # assigned_design — the two relationships user_can_view_project() scopes them on.
        # Finance, SCM and BD are portfolio-wide and need no assignment.
        se_user, se = _make_user('se_v', 'Site Engineer')
        Task.objects.filter(pk=self.task.pk).update(assigned_to=se)

        des_user, des = _make_user('des_v', 'Design')
        self.project.assigned_design = des
        self.project.save(update_fields=['assigned_design'])

        actors = [(se_user, 'Site Engineer'), (des_user, 'Design')]
        for uname, role in (('fin_v', 'Finance'), ('scm_v', 'SCM'), ('bd_v', 'BD')):
            u, _ = _make_user(uname, role)
            actors.append((u, role))

        for user, role in actors:
            ctx = self._ctx(user)
            self.assertFalse(ctx['gantt_can_view_client'], role)
            self.assertIsNone(ctx['gantt_client'], role)          # not in DOM for these roles
            self.assertIsNotNone(ctx['gantt_internal'], role)     # internal still visible

    def test_non_residential_hides_gantt(self):
        u, _ = _make_user('ceo_opex', 'CEO')
        opex = _make_project(self.pm, project_type='OPEX')
        self.client.force_login(u)
        ctx = self.client.get(reverse('project_overview', args=[opex.project_id])).context
        self.assertFalse(ctx['gantt_available'])
        self.assertIsNone(ctx['gantt_client'])

    def test_not_activated_flag(self):
        u, _ = _make_user('ceo_draft', 'CEO')
        draft = _make_project(self.pm, activated=None, status='Draft')
        self.client.force_login(u)
        ctx = self.client.get(reverse('project_overview', args=[draft.project_id])).context
        self.assertTrue(ctx['gantt_available'])
        self.assertTrue(ctx['gantt_not_activated'])


class GanttAdminSettingsTests(TestCase):
    def setUp(self):
        self.client = Client(SERVER_NAME='localhost')
        self.admin_user, _ = _make_user('admin_g', 'Admin')
        self.url = reverse('admin_master_switches')

    def _post(self, **extra):
        self.client.force_login(self.admin_user)
        data = {'gantt_client_buffer_days': '3', 'gantt_external_min_display_days': '3'}
        data.update(extra)
        return self.client.post(self.url, data)

    def test_buffer_round_trip(self):
        self._post(gantt_client_buffer_days='7')
        self.assertEqual(SystemSettings.get().gantt_client_buffer_days, 7)

    def test_negative_and_garbage_rejected(self):
        s = SystemSettings.get()
        s.gantt_client_buffer_days = 4
        s.save(update_fields=['gantt_client_buffer_days'])
        self._post(gantt_client_buffer_days='-2')
        self.assertEqual(SystemSettings.get().gantt_client_buffer_days, 4)   # unchanged
        self._post(gantt_client_buffer_days='abc')
        self.assertEqual(SystemSettings.get().gantt_client_buffer_days, 4)   # unchanged


class GanttDesyncGuardTests(TestCase):
    """The display maps are hardcoded — assert they stay in sync with the template."""

    def test_task_map_keys_exist_in_template(self):
        template_names = {name for _phase_name, name in get_residential_template_task_names()}
        for key in GANTT_TASK_DISPLAY_NAME_MAP:
            self.assertIn(key, template_names, f"Gantt task map key desynced: {key!r}")

    def test_phase_map_keys_exist_in_template(self):
        phase_names = {ph['phase_name'] for ph in build_residential_phases()}
        for key in GANTT_PHASE_DISPLAY_NAME_MAP:
            self.assertIn(key, phase_names, f"Gantt phase map key desynced: {key!r}")
