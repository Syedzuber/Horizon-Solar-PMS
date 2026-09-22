"""
Reorder tasks within a phase — a PM or coordinator drags a phase's rows into a new order.

THE RULES PINNED HERE, each by a test named for it
    Eligibility   permissions.can_reorder_phase(): task_add's gate (PM / Project
                  Coordinator who manages the project), Active, not soft-deleted, and
                  project_type OPEX or CAPEX. Refused otherwise; the POST writes nothing.
    Stale list    a submitted id list that is not EXACTLY the phase's current task set
                  (missing, extra, duplicated, another phase's, another project's,
                  malformed) answers 409 with the phase re-rendered and writes nothing.
    Write         task_order becomes 1..N in the submitted order and nothing else
                  changes; an unchanged order writes and logs nothing; one ActivityLog
                  row (phase_tasks_reordered) names each moved task; no notification,
                  no StatusTransition.
    Handle        rendered iff the predicate is True, on project_overview and on every
                  HTMX responder that redraws a row or a phase; its cost is per request,
                  not per row.
    P2 (gate)     _gate_task_pk() follows position on OPEX too, and the BD milestone
                  gate still renders for no viewer there.

Run with:
    python manage.py test projects.tests_task_reorder --settings=solarpms.test_settings
"""
from decimal import Decimal
from unittest.mock import patch

from django.db import connection
from django.template import Context, Template
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from .models import (
    ActivityLog, NotificationLog, Project, ProjectPhase, StatusTransition, Task,
)
from .permissions import can_reorder_phase
from .tests_checklist_task_link import _client_for, _profile, _seed_opex
from .views import _REORDER_STALE_MESSAGE, _gate_task_pk

PHASE_NAME   = 'Installation'           # OPEX v1: nine Site Engineer tasks, no mirrors
SOURCE_NAME  = 'Module Installation'    # in that phase; a location-duplication source
HANDLE_MARK  = 'aria-label="Drag to reorder '
GATE_MARK    = 'data-is-milestone-gate'
STABLE_FIELDS = ('status', 'task_name', 'assigned_to_id', 'due_date', 'template_task_id',
                 'location_label', 'phase_id', 'assigned_role', 'is_mirror',
                 'is_not_applicable', 'completed_at', 'submitted_at', 'approved_at')


def _logs():
    """ActivityLog rows, less the user_login row every force_login writes."""
    return ActivityLog.objects.exclude(action_code='user_login').count()


def _task_writes(ctx):
    return [q['sql'] for q in ctx.captured_queries
            if q['sql'].lstrip().upper().startswith('UPDATE')
            and 'projects_task"' in q['sql'].split('SET')[0]]


class ReorderFixture(TestCase):

    @classmethod
    def setUpTestData(cls):
        _seed_opex()
        cls.pm       = _profile('ro_pm', 'PM')
        cls.other_pm = _profile('ro_other_pm', 'PM')
        cls.coord    = _profile('ro_coord', 'Project Coordinator')
        cls.se       = _profile('ro_se', 'Site Engineer')
        cls.finance  = _profile('ro_fin', 'Finance')
        cls.scm      = _profile('ro_scm', 'SCM')
        cls.admin    = _profile('ro_admin', 'Admin')
        cls.bd       = _profile('ro_bd', 'BD')
        cls.design   = _profile('ro_design', 'Design')

    def setUp(self):
        self.site  = self._activate_opex('Reorder Site A')
        self.phase = ProjectPhase.objects.get(project=self.site, phase_name=PHASE_NAME)

    # -- fixture helpers ------------------------------------------------------

    def _activate_opex(self, name):
        site = Project.objects.create(
            customer_name=name, customer_phone='9876543211', site_address='1 Ro Road',
            city='Lucknow', project_type='OPEX', dc_capacity_kw=Decimal('100.00'),
            status='Draft', assigned_pm=self.pm,
        )
        response = _client_for(self.pm).post(reverse('opex_site_activate', args=[site.project_id]))
        self.assertEqual(response.status_code, 302, 'OPEX activation did not redirect')
        site.refresh_from_db()
        self.assertEqual(site.status, 'Active')
        return site

    def _bare_project(self, project_type, name='Bare', tasks=3):
        """An Active project built by hand, with two phases of `tasks` rows each: the
        shape a CAPEX site would have (no real CAPEX project has phases yet)."""
        project = Project.objects.create(
            customer_name=name, customer_phone='9876543219', site_address='9 Ro Road',
            city='Lucknow', project_type=project_type, dc_capacity_kw=Decimal('50.00'),
            status='Active', assigned_pm=self.pm,
        )
        phases = []
        for order in (1, 2):
            phase = ProjectPhase.objects.create(project=project, phase_name=f'Stage {order}',
                                                phase_order=order)
            for n in range(1, tasks + 1):
                Task.objects.create(phase=phase, task_name=f'Stage {order} task {n}',
                                    task_order=n, assigned_role=Task.SITE_ENGINEER)
            phases.append(phase)
        return project, phases

    def _ids(self, phase=None):
        return list(Task.objects.filter(phase=phase or self.phase)
                    .order_by('task_order', 'pk').values_list('pk', flat=True))

    def _orders(self, phase=None):
        return list(Task.objects.filter(phase=phase or self.phase)
                    .order_by('pk').values_list('pk', 'task_order'))

    def _snapshot(self, project=None):
        return {row['id']: row for row in
                Task.objects.filter(phase__project=project or self.site).values()}

    def _post(self, order, profile=None, phase=None, hx=True):
        phase = phase or self.phase
        value = order if isinstance(order, str) else ','.join(str(pk) for pk in order)
        headers = {'HTTP_HX_REQUEST': 'true'} if hx else {}
        return _client_for(profile or self.pm).post(
            reverse('phase_tasks_reorder', args=[phase.project.project_id, phase.pk]),
            {'order': value}, **headers)

    def _overview(self, profile, project=None):
        return _client_for(profile).get(
            reverse('project_overview', args=[(project or self.site).project_id]))

    def assertRefused(self, profile=None, phase=None):
        phase = phase or self.phase
        project = phase.project
        before, logs = self._snapshot(project), _logs()
        ids = self._ids(phase)
        response = self._post(list(reversed(ids)), profile, phase)
        self.assertIn(response.status_code, (403, 404))
        self.assertEqual(self._snapshot(project), before, 'a refused POST changed a task')
        self.assertEqual(_logs(), logs)


# ---------------------------------------------------------------------------
# 1 — Eligibility.
# ---------------------------------------------------------------------------

class EligibilityTests(ReorderFixture):

    def test_the_owning_pm_is_admitted(self):
        self.assertTrue(can_reorder_phase(self.pm.user, self.phase))
        self.assertEqual(self._post(list(reversed(self._ids()))).status_code, 200)

    def test_a_coordinator_on_the_project_is_admitted(self):
        self.site.coordinators.add(self.coord)
        self.assertEqual(self._post(list(reversed(self._ids())), self.coord).status_code, 200)

    def test_a_capex_project_is_admitted(self):
        project, phases = self._bare_project('CAPEX')
        self.assertTrue(can_reorder_phase(self.pm.user, phases[0]))
        ids = self._ids(phases[0])
        self.assertEqual(self._post(list(reversed(ids)), phase=phases[0]).status_code, 200)
        self.assertEqual(self._ids(phases[0]), list(reversed(ids)))

    def test_refused_on_a_residential_project(self):
        project, phases = self._bare_project('Residential')
        self.assertFalse(can_reorder_phase(self.pm.user, phases[0]))
        self.assertRefused(phase=phases[0])

    def test_refused_for_an_inactive_project(self):
        Project.objects.filter(pk=self.site.pk).update(status='On Hold')
        self.assertRefused()

    def test_refused_for_a_soft_deleted_project(self):
        Project.objects.filter(pk=self.site.pk).update(is_deleted=True)
        self.assertRefused()

    def test_refused_for_a_pm_who_does_not_own_the_project(self):
        self.assertRefused(self.other_pm)

    def test_refused_for_a_site_engineer(self):
        self.assertRefused(self.se)

    def test_refused_for_finance(self):
        self.assertRefused(self.finance)

    def test_refused_for_scm(self):
        self.assertRefused(self.scm)

    def test_refused_for_admin(self):
        self.assertRefused(self.admin)

    def test_refused_for_a_phase_of_another_project(self):
        other = self._activate_opex('Reorder Site B')
        phase = ProjectPhase.objects.get(project=other, phase_name=PHASE_NAME)
        before = self._snapshot(other)
        response = _client_for(self.pm).post(
            reverse('phase_tasks_reorder', args=[self.site.project_id, phase.pk]),
            {'order': ','.join(str(pk) for pk in reversed(self._ids(phase)))},
            HTTP_HX_REQUEST='true')
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self._snapshot(other), before)

    def test_a_get_writes_nothing(self):
        before = self._snapshot()
        response = _client_for(self.pm).get(
            reverse('phase_tasks_reorder', args=[self.site.project_id, self.phase.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._snapshot(), before)


# ---------------------------------------------------------------------------
# 2 — A list that is not exactly the phase: 409, zero writes.
# ---------------------------------------------------------------------------

class StaleListTests(ReorderFixture):

    def assertStale(self, order):
        before, logs = self._snapshot(), _logs()
        with CaptureQueriesContext(connection) as ctx:
            response = self._post(order)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(_task_writes(ctx), [], 'a stale list wrote to a task')
        self.assertEqual(self._snapshot(), before)
        self.assertEqual(_logs(), logs)
        html = response.content.decode()
        self.assertIn(_REORDER_STALE_MESSAGE, html)
        self.assertIn(f'id="phase-tasks-{self.phase.pk}" hx-swap-oob="true"', html)
        return html

    def test_a_missing_id(self):
        self.assertStale(self._ids()[:-1])

    def test_an_extra_id(self):
        self.assertStale(self._ids() + [999999])

    def test_a_duplicated_id(self):
        ids = self._ids()
        self.assertStale(ids + [ids[0]])

    def test_a_duplicated_id_in_place_of_another(self):
        ids = self._ids()
        self.assertStale([ids[0]] + ids[:-1])

    def test_an_id_from_another_phase(self):
        foreign = Task.objects.filter(phase__project=self.site).exclude(phase=self.phase).first()
        ids = self._ids()
        self.assertStale(ids[:-1] + [foreign.pk])

    def test_an_id_from_another_project(self):
        other = self._activate_opex('Reorder Site C')
        foreign = Task.objects.filter(phase__project=other, phase__phase_name=PHASE_NAME).first()
        ids = self._ids()
        self.assertStale(ids[:-1] + [foreign.pk])

    def test_a_malformed_list(self):
        self.assertStale('1,two,3')

    def test_an_empty_list(self):
        self.assertStale('')

    def test_the_refreshed_phase_is_the_current_order(self):
        html = self.assertStale(self._ids()[:-1])
        names = list(Task.objects.filter(phase=self.phase).order_by('task_order')
                     .values_list('task_name', flat=True))
        positions = [html.index(f'>{name}</a>') for name in names]
        self.assertEqual(positions, sorted(positions))


# ---------------------------------------------------------------------------
# 3 — The write.
# ---------------------------------------------------------------------------

class WriteTests(ReorderFixture):

    def test_orders_become_one_to_n_in_the_submitted_order(self):
        ids = self._ids()
        submitted = ids[3:] + ids[:3]
        self.assertEqual(self._post(submitted).status_code, 200)
        self.assertEqual(self._ids(), submitted)
        self.assertEqual(
            list(Task.objects.filter(phase=self.phase).order_by('task_order')
                 .values_list('task_order', flat=True)),
            list(range(1, len(ids) + 1)))

    def test_gaps_in_the_old_numbering_are_closed(self):
        ids = self._ids()
        for n, pk in enumerate(ids):
            Task.objects.filter(pk=pk).update(task_order=10 * (n + 1))
        submitted = list(reversed(ids))
        self._post(submitted)
        self.assertEqual([order for _, order in sorted(self._orders(), key=lambda r: submitted.index(r[0]))],
                         list(range(1, len(ids) + 1)))

    def test_no_field_other_than_task_order_changes(self):
        ids = self._ids()
        Task.objects.filter(pk=ids[0]).update(assigned_to=self.se, due_date=timezone.localdate(),
                                              status=Task.IN_PROGRESS, location_label='Roof')
        before = self._snapshot()
        self._post(list(reversed(ids)))
        after = self._snapshot()
        self.assertEqual(before.keys(), after.keys())
        for pk, row in before.items():
            for field in STABLE_FIELDS:
                self.assertEqual(after[pk][field], row[field], f'{field} changed on task {pk}')
            if pk not in ids:
                self.assertEqual(after[pk], row, 'a task outside the phase changed')

    def test_the_update_sets_task_order_only(self):
        with CaptureQueriesContext(connection) as ctx:
            self._post(list(reversed(self._ids())))
        writes = _task_writes(ctx)
        self.assertGreater(len(writes), 0)
        for sql in writes:
            assignments = sql.split(' SET ', 1)[1].split(' WHERE ', 1)[0]
            self.assertIn('"task_order"', assignments)
            for field in ('"status"', '"task_name"', '"assigned_to_id"', '"due_date"',
                          '"location_label"', '"template_task_id"'):
                self.assertNotIn(field, assignments)

    def test_an_unchanged_order_writes_nothing_and_logs_nothing(self):
        before, logs = self._snapshot(), _logs()
        with CaptureQueriesContext(connection) as ctx:
            response = self._post(self._ids())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(_task_writes(ctx), [])
        self.assertEqual(self._snapshot(), before)
        self.assertEqual(_logs(), logs)

    def test_exactly_one_activity_log_naming_the_moved_tasks(self):
        ids = self._ids()
        first, second = Task.objects.get(pk=ids[0]), Task.objects.get(pk=ids[1])
        logs = _logs()
        self._post([ids[1], ids[0]] + ids[2:])
        self.assertEqual(_logs(), logs + 1)
        log = ActivityLog.objects.exclude(action_code='user_login').latest('pk')
        self.assertEqual(log.action_code, 'phase_tasks_reordered')
        self.assertEqual(log.entity_type, 'ProjectPhase')
        self.assertEqual(log.entity_id, self.phase.pk)
        self.assertEqual(log.actor, self.pm)
        self.assertIn(f"'{PHASE_NAME}'", log.action)
        self.assertIn(f'{first.task_name}: {first.task_order} → {second.task_order}', log.action)
        self.assertIn(f'{second.task_name}: {second.task_order} → {first.task_order}', log.action)
        unmoved = Task.objects.get(pk=ids[2]).task_name
        self.assertNotIn(unmoved, log.action)

    def test_a_long_log_is_cut_to_the_column(self):
        ids = self._ids()
        Task.objects.filter(pk__in=ids).update(task_name='A very long task name ' * 5)
        self._post(list(reversed(ids)))
        log = ActivityLog.objects.get(action_code='phase_tasks_reordered')
        max_length = ActivityLog._meta.get_field('action').max_length
        self.assertEqual(len(log.action), max_length)
        self.assertTrue(log.action.endswith('…'))

    def test_no_notification_and_no_status_transition(self):
        notes, transitions = NotificationLog.objects.count(), StatusTransition.objects.count()
        self._post(list(reversed(self._ids())))
        self.assertEqual(NotificationLog.objects.count(), notes)
        self.assertEqual(StatusTransition.objects.count(), transitions)

    def test_the_response_redraws_the_phase_in_the_new_order(self):
        ids = self._ids()
        submitted = list(reversed(ids))
        html = self._post(submitted).content.decode()
        self.assertIn(f'id="phase-tasks-{self.phase.pk}" hx-swap-oob="true"', html)
        self.assertIn(f'id="phase-count-{self.phase.pk}" hx-swap-oob="true"', html)
        positions = [html.index(f'id="task-row-{pk}"') for pk in submitted]
        self.assertEqual(positions, sorted(positions))
        self.assertNotIn('taskFormDone', response_headers(self._post(submitted)))

    def test_the_response_keeps_opex_rows_without_a_done_option(self):
        """The two-step flags travel with the redraw, as they do on the page."""
        Task.objects.filter(phase=self.phase).update(assigned_to=self.se)
        html = self._post(list(reversed(self._ids()))).content.decode()
        self.assertEqual(html.count('<select name="status"'), Task.objects.filter(phase=self.phase).count())
        self.assertEqual(html.count('value="Done"'), 0)


def response_headers(response):
    return ' '.join(f'{k}: {v}' for k, v in response.items())


# ---------------------------------------------------------------------------
# 4 — The handle.
# ---------------------------------------------------------------------------

class HandleTests(ReorderFixture):

    def _site_task_count(self, project=None):
        return Task.objects.filter(phase__project=project or self.site).count()

    def test_handle_on_every_row_for_the_owning_pm(self):
        html = self._overview(self.pm).content.decode()
        self.assertEqual(html.count(HANDLE_MARK), self._site_task_count())
        self.assertIn(f'{HANDLE_MARK}{SOURCE_NAME}"', html)
        self.assertIn('task-drag-handle d-none d-md-inline-flex', html)
        self.assertIn('sortablejs@1.15.7/Sortable.min.js', html)

    def test_handle_for_a_coordinator_on_the_project(self):
        self.site.coordinators.add(self.coord)
        html = self._overview(self.coord).content.decode()
        self.assertEqual(html.count(HANDLE_MARK), self._site_task_count())

    def test_no_handle_for_a_viewer_who_may_not_reorder(self):
        for profile in (self.se, self.other_pm, self.admin):
            with self.subTest(role=profile.role, user=profile.user.username):
                response = self._overview(profile)
                if response.status_code == 200:
                    self.assertEqual(response.content.decode().count(HANDLE_MARK), 0)

    def test_no_handle_on_an_inactive_project(self):
        Project.objects.filter(pk=self.site.pk).update(status='On Hold')
        self.assertEqual(self._overview(self.pm).content.decode().count(HANDLE_MARK), 0)

    def test_no_handle_on_a_residential_project(self):
        project, _ = self._bare_project('Residential')
        html = self._overview(self.pm, project).content.decode()
        self.assertIn('Stage 1 task 1', html)
        self.assertEqual(html.count(HANDLE_MARK), 0)

    def test_handle_on_a_capex_project_with_phases(self):
        project, _ = self._bare_project('CAPEX')
        html = self._overview(self.pm, project).content.decode()
        self.assertEqual(html.count(HANDLE_MARK), self._site_task_count(project))

    # -- every HTMX responder that redraws a row or a phase (P1) ---------------

    def _task(self):
        return Task.objects.get(phase=self.phase, task_name=SOURCE_NAME)

    def test_the_row_swap_carries_the_handle(self):
        """_render_task_row_hx: status, assign, due date (and rename's success)."""
        task = self._task()
        Task.objects.filter(pk=task.pk).update(due_date=timezone.localdate())
        client = _client_for(self.pm)
        html = client.post(reverse('task_assign', args=[self.site.project_id, task.pk]),
                           {'assigned_to': self.se.pk}, HTTP_HX_REQUEST='true').content.decode()
        self.assertIn(f'{HANDLE_MARK}{SOURCE_NAME}"', html)
        html = client.post(reverse('task_status_update', args=[self.site.project_id, task.pk]),
                           {'status': Task.IN_PROGRESS}, HTTP_HX_REQUEST='true').content.decode()
        self.assertEqual(Task.objects.get(pk=task.pk).status, Task.IN_PROGRESS)
        self.assertIn(f'{HANDLE_MARK}{SOURCE_NAME}"', html)
        html = client.post(reverse('task_set_due_date', args=[self.site.project_id, task.pk]),
                           {'due_date': timezone.localdate().isoformat()},
                           HTTP_HX_REQUEST='true').content.decode()
        self.assertIn(f'{HANDLE_MARK}{SOURCE_NAME}"', html)

    def test_the_row_swap_has_no_handle_for_a_site_engineer(self):
        task = self._task()
        Task.objects.filter(pk=task.pk).update(assigned_to=self.se, due_date=timezone.localdate())
        html = _client_for(self.se).post(
            reverse('task_status_update', args=[self.site.project_id, task.pk]),
            {'status': Task.IN_PROGRESS}, HTTP_HX_REQUEST='true').content.decode()
        self.assertIn(f'id="task-row-{task.pk}"', html)
        self.assertNotIn(HANDLE_MARK, html)

    def test_the_design_head_modal_swap_carries_the_handle(self):
        """_render_task_assign_design_success_hx (#4), asked by a PM who is also a
        Design Head, on a Design-role task."""
        self.pm.is_design_head = True
        self.pm.save(update_fields=['is_design_head'])
        task = Task.objects.filter(phase__project=self.site, assigned_role=Task.DESIGN).first()
        html = _client_for(self.pm).post(
            reverse('task_assign_design_head', args=[self.site.project_id, task.pk]),
            {'assigned_to': self.design.pk}, HTTP_HX_REQUEST='true').content.decode()
        self.assertIn(f'id="task-row-{task.pk}" hx-swap-oob="true"', html)
        self.assertIn(f'{HANDLE_MARK}{task.task_name}"', html)

    def test_the_task_add_tbody_carries_the_handle(self):
        html = _client_for(self.pm).post(
            reverse('task_add', args=[self.site.project_id]),
            {'task_name': 'Extra installation step', 'phase': self.phase.pk,
             'assigned_role': Task.SITE_ENGINEER, 'assigned_to': self.se.pk,
             'due_date': timezone.localdate().isoformat()},
            HTTP_HX_REQUEST='true').content.decode()
        self.assertIn(f'id="phase-tasks-{self.phase.pk}" hx-swap-oob="true"', html)
        self.assertEqual(html.count(HANDLE_MARK), Task.objects.filter(phase=self.phase).count())
        self.assertIn(f'{HANDLE_MARK}Extra installation step"', html)

    def test_the_duplicate_tbody_carries_the_handle(self):
        task = self._task()
        html = _client_for(self.pm).post(
            reverse('task_duplicate_locations_create', args=[self.site.project_id, task.pk]),
            {'existing_locations': [], 'new_locations': 'Block A',
             'assigned_to': self.se.pk},
            HTTP_HX_REQUEST='true').content.decode()
        self.assertIn(f'id="phase-tasks-{self.phase.pk}" hx-swap-oob="true"', html)
        self.assertEqual(html.count(HANDLE_MARK), Task.objects.filter(phase=self.phase).count())
        self.assertIn('Block A', html)

    def test_the_reorder_response_carries_the_handle(self):
        html = self._post(list(reversed(self._ids()))).content.decode()
        self.assertEqual(html.count(HANDLE_MARK), Task.objects.filter(phase=self.phase).count())

    # -- cost ----------------------------------------------------------------

    def test_the_rule_is_asked_once_per_request_not_per_row(self):
        tasks = list(Task.objects.filter(phase__project=self.site).select_related('phase'))
        template = Template('{% load duplicate_tags %}{% for t in tasks %}'
                            '{% if t|can_reorder:user %}x{% endif %}{% endfor %}')
        with patch('projects.permissions.user_can_manage_project', return_value=True) as manage:
            html = template.render(Context({'tasks': tasks, 'user': self.pm.user}))
        self.assertEqual(manage.call_count, 1)
        self.assertEqual(html, 'x' * len(tasks))

    def test_overview_queries_for_the_handle_do_not_grow_with_rows(self):
        """The page already costs one query per row (active_attachment_count), so the
        absolute count is not the measure. The handle's own cost is: the difference
        between the page with the filter live and with it stubbed out must be the
        same at N rows and at N + 6."""
        def cost():
            with CaptureQueriesContext(connection) as live:
                self._overview(self.pm)
            with patch('projects.templatetags.duplicate_tags.can_reorder_phase',
                       return_value=False):
                with CaptureQueriesContext(connection) as stub:
                    self._overview(self.pm)
            return len(live.captured_queries) - len(stub.captured_queries)

        self._overview(self.pm)   # warm-up: the first request carries one-off queries
        small = cost()
        last = Task.objects.filter(phase=self.phase).order_by('-task_order').first().task_order
        for n in range(1, 7):
            Task.objects.create(phase=self.phase, task_name=f'Padding {n}',
                                task_order=last + n, assigned_role=Task.SITE_ENGINEER)
        self.assertEqual(cost(), small)


# ---------------------------------------------------------------------------
# 5 — P2: the milestone gate is positional on every type, and inert on OPEX.
# ---------------------------------------------------------------------------

class MilestoneGatePositionTests(ReorderFixture):

    def test_opex_reorder_moves_the_gate_candidate_but_no_viewer_sees_the_gate(self):
        """_gate_task_pk() picks the first task of the first phase on ANY project type
        (EXECUTION_MODULE_DEFERRED G19). On OPEX that choice is inert: the gate markup
        renders only for a BD viewer holding the row's role, and no OPEX task can be BD.
        This pins both halves, so a BD task on a non-Residential template fails here."""
        first_phase = ProjectPhase.objects.filter(project=self.site).order_by('phase_order').first()
        added = Task.objects.create(phase=first_phase, task_name='Design review',
                                    task_order=2, assigned_role=Task.DESIGN,
                                    assigned_to=self.design)
        ids = self._ids(first_phase)
        self.assertEqual(_gate_task_pk(self.site), ids[0])

        self.assertEqual(self._post([added.pk] + [pk for pk in ids if pk != added.pk],
                                    phase=first_phase).status_code, 200)
        self.assertEqual(_gate_task_pk(self.site), added.pk)

        self.site.coordinators.add(self.coord)
        for profile in (self.pm, self.coord, self.se, self.design, self.scm, self.finance,
                        self.admin, self.bd):
            with self.subTest(role=profile.role):
                response = self._overview(profile)
                if response.status_code == 200:
                    self.assertNotIn(GATE_MARK, response.content.decode())
        self.assertFalse(Task.objects.filter(phase__project__project_type='OPEX',
                                             assigned_role=Task.BD).exists())
