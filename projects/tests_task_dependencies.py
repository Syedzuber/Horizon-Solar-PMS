"""Task dependencies — prompt 1.4a.

WHY THIS FILE EXISTS
--------------------
1.4a builds the dependency structure and the read-side predicate for B-08. It changes
nothing a user can see: no view, no template, no permission file, and no status-change
behaviour. 1.4b wires the warning into the status-change path.

THE MOST IMPORTANT TEST IN THIS FILE is
`PredicateReportsNeverRefusesTests.test_incomplete_predecessors_returns_rather_than_raises`.
B-08 was answered by the product owner on 30 Aug 2026: a dependent task may be started
before its predecessor is Done, BY ANYONE, with a mandatory reason and a warning — no
hard block, no role gate, no approval step. The likeliest future mistake is a session
reading the word "dependency" and adding a refusal. That test exists to fail loudly the
moment somebody does.

The second thing worth knowing: cycle detection is tested at THREE nodes as well as two.
A naive check that only compares the proposed edge against its own reverse catches
A->B->A and sails straight past A->B->C->A, and a cycle is invisible until somebody
tries to work.

The fixture builds a SMALL template of its own (`TESTDEP`) rather than the 52-task
RESIDENTIAL one. `materialise_task_dependencies()` is deliberately callable in isolation
(1.4a does not wire it into `attach_residential_template()`), so it is tested in
isolation; one test at the end runs it against a real activated Residential project to
prove it is safe against the shape the product actually builds.

Run with:
    python manage.py test projects.tests_task_dependencies --settings=solarpms.test_settings
"""
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import Client, TestCase
from django.urls import reverse

from .models import (
    DependencyCycle, Project, ProjectPhase, Task, TaskDependency, TaskTemplate,
    TaskTemplatePhase, TaskTemplateTask, TaskTemplateTaskDependency,
    TemplateVersionLocked,
)
from .task_dependencies import incomplete_predecessors, materialise_task_dependencies
from .utils import RESIDENTIAL_FINANCE_ASSIGNEE_EMAIL


def _profile(username, role, email=''):
    """Create a User and set its auto-created UserProfile's role.

    A post_save signal on User creates the UserProfile, so we fetch and mutate rather
    than creating a second one. Same helper shape as tests_task_template.py.
    """
    user    = User.objects.create_user(username=username, password='pw12345', email=email)
    profile = user.profile
    profile.role      = role
    profile.is_active = True
    profile.save()
    return profile


class DependencyBase(TestCase):
    """A small draft template of three tasks in one phase, plus a PM to own projects."""

    def setUp(self):
        self.pm = _profile('pm_dep', 'PM')
        self.template = self._template(code='TESTDEP', version_no=1)
        self.a, self.b, self.c = self._tasks(self.template, 'A', 'B', 'C')

    # -- fixture helpers -----------------------------------------------------

    def _template(self, code, version_no=1, label='Test Dependency Template'):
        """A DRAFT template version. Content guards only permit writes while draft."""
        return TaskTemplate.objects.create(
            code=code, label=label, project_type='Residential',
            version_no=version_no, status=TaskTemplate.DRAFT,
        )

    def _tasks(self, template, *labels):
        """One phase on `template`, holding one task per label, in order."""
        phase = TaskTemplatePhase.objects.create(
            template=template, code=f'PH-{template.code}-{template.version_no}',
            label='Only Phase', sort_order=1,
        )
        return [
            TaskTemplateTask.objects.create(
                phase=phase, code=f'{template.code}-{label}', label=label,
                sort_order=i, assigned_role=Task.PM, task_type=Task.INTERNAL,
                duration_days=1,
            )
            for i, label in enumerate(labels, start=1)
        ]

    def _edge(self, predecessor, successor):
        edge = TaskTemplateTaskDependency(predecessor=predecessor, successor=successor)
        edge.save()
        return edge

    # -- instance-side fixture ----------------------------------------------

    def _project(self, customer_name='Dependency House'):
        return Project.objects.create(
            customer_name=customer_name,
            customer_phone='9876543210',
            site_address='1 Sun Road',
            city='Lucknow',
            project_type='Residential',
            capacity_kw=Decimal('5.00'),
            status='Active',
            assigned_pm=self.pm,
            target_commissioning_date=date.today() + timedelta(days=90),
        )

    def _build_from_template(self, project, template):
        """Create the project's phases and tasks from `template`, stamping template_task.

        A local restatement of what `attach_residential_template()` does at
        `bulk_create` — 1.4a may not modify that function, and this fixture must
        produce rows of the same shape for `materialise_task_dependencies()` to walk.
        """
        made = {}
        for tpl_phase in template.phases.all():
            phase = ProjectPhase.objects.create(
                project=project, phase_name=tpl_phase.label,
                phase_order=tpl_phase.sort_order,
            )
            for tpl_task in tpl_phase.tasks.all():
                made[tpl_task.pk] = Task.objects.create(
                    phase=phase, task_name=tpl_task.label,
                    task_order=tpl_task.sort_order,
                    assigned_role=tpl_task.assigned_role,
                    task_type=tpl_task.task_type,
                    duration_days=tpl_task.duration_days,
                    is_payment_milestone=tpl_task.is_payment_milestone,
                    template_task=tpl_task,
                )
        return made


# ---------------------------------------------------------------------------
# 1 — The template-side edge
# ---------------------------------------------------------------------------

class TemplateEdgeTests(DependencyBase):

    def test_an_edge_between_two_tasks_in_the_same_version_saves(self):
        edge = self._edge(self.a, self.b)

        self.assertEqual(TaskTemplateTaskDependency.objects.count(), 1)
        self.assertEqual(edge.predecessor, self.a)
        self.assertEqual(edge.successor, self.b)

    def test_the_related_names_read_correctly_from_either_end(self):
        """`task.dependencies` is what it waits on; `task.dependents` is what waits on it."""
        self._edge(self.a, self.b)

        self.assertEqual([e.predecessor for e in self.b.dependencies.all()], [self.a])
        self.assertEqual([e.successor   for e in self.a.dependents.all()],   [self.b])
        self.assertEqual(list(self.a.dependencies.all()), [])
        self.assertEqual(list(self.b.dependents.all()),   [])

    def test_an_edge_spanning_two_template_versions_is_refused(self):
        other = self._template(code='TESTDEP2', version_no=1)
        (foreign,) = self._tasks(other, 'Foreign')

        with self.assertRaises(ValidationError) as ctx:
            self._edge(self.a, foreign)

        self.assertIn('SAME template version', str(ctx.exception))
        self.assertEqual(TaskTemplateTaskDependency.objects.count(), 0)

    def test_an_edge_spanning_two_versions_of_the_SAME_code_is_refused(self):
        """v1 and v2 share a `code`; they are still two versions, and the edge is refused."""
        v2 = self._template(code='TESTDEP', version_no=2)
        (later,) = self._tasks(v2, 'A')

        with self.assertRaises(ValidationError):
            self._edge(self.a, later)

    def test_a_self_edge_is_refused_on_save(self):
        with self.assertRaises(ValidationError) as ctx:
            self._edge(self.a, self.a)

        self.assertIn('cannot depend on itself', str(ctx.exception))
        self.assertEqual(TaskTemplateTaskDependency.objects.count(), 0)

    def test_a_self_edge_is_refused_by_the_database_too(self):
        """The save() guard is a readable message; the CHECK constraint is the guarantee.

        bulk_create() bypasses save(), which is exactly the documented enforcement limit
        — so this asserts the half that survives it.
        """
        with self.assertRaises(IntegrityError), transaction.atomic():
            TaskTemplateTaskDependency.objects.bulk_create([
                TaskTemplateTaskDependency(predecessor=self.a, successor=self.a),
            ])

    def test_a_duplicate_edge_is_refused(self):
        self._edge(self.a, self.b)

        with self.assertRaises(IntegrityError), transaction.atomic():
            TaskTemplateTaskDependency.objects.create(predecessor=self.a, successor=self.b)

        self.assertEqual(TaskTemplateTaskDependency.objects.count(), 1)

    def test_the_reverse_of_an_existing_edge_is_a_cycle_not_a_duplicate(self):
        """B->A after A->B is a two-node loop, and must be refused as one."""
        self._edge(self.a, self.b)

        with self.assertRaises(DependencyCycle):
            self._edge(self.b, self.a)


# ---------------------------------------------------------------------------
# 2 — Cycles
#
# The three-node case is the one a naive check misses.
# ---------------------------------------------------------------------------

class TemplateCycleTests(DependencyBase):

    def test_a_two_node_cycle_is_refused_and_names_the_closing_edge(self):
        self._edge(self.a, self.b)

        with self.assertRaises(DependencyCycle) as ctx:
            self._edge(self.b, self.a)

        message = str(ctx.exception)
        self.assertIn("'B' -> 'A'", message, 'the message must name the closing edge')
        self.assertIn('close a dependency loop', message)
        self.assertEqual(TaskTemplateTaskDependency.objects.count(), 1)

    def test_a_three_node_cycle_is_refused_and_names_the_closing_edge(self):
        """A->B->C exists; C->A closes it. A check that only looks one hop back misses this."""
        self._edge(self.a, self.b)
        self._edge(self.b, self.c)

        with self.assertRaises(DependencyCycle) as ctx:
            self._edge(self.c, self.a)

        message = str(ctx.exception)
        self.assertIn("'C' -> 'A'", message, 'the message must name the closing edge')
        self.assertIn('2 edge(s)', message, 'the message must name the path that closes')
        self.assertEqual(TaskTemplateTaskDependency.objects.count(), 2)

    def test_a_four_node_cycle_is_refused(self):
        d4, = self._tasks_in_existing_phase('D')
        self._edge(self.a, self.b)
        self._edge(self.b, self.c)
        self._edge(self.c, d4)

        with self.assertRaises(DependencyCycle):
            self._edge(d4, self.a)

    def _tasks_in_existing_phase(self, *labels):
        phase = self.template.phases.first()
        return [
            TaskTemplateTask.objects.create(
                phase=phase, code=f'X-{label}', label=label,
                sort_order=90 + i, assigned_role=Task.PM,
            )
            for i, label in enumerate(labels)
        ]

    def test_a_diamond_is_not_a_cycle(self):
        """A->B, A->C, B->D, C->D. Two paths to the same node is convergence, not a loop."""
        (d,) = self._tasks_in_existing_phase('D')
        self._edge(self.a, self.b)
        self._edge(self.a, self.c)
        self._edge(self.b, d)
        self._edge(self.c, d)

        self.assertEqual(TaskTemplateTaskDependency.objects.count(), 4)

    def test_a_cycle_in_another_template_version_does_not_block_this_one(self):
        """The walk is scoped to one version's own edges."""
        other = self._template(code='OTHERDEP', version_no=1)
        x, y = self._tasks(other, 'X', 'Y')
        self._edge(x, y)

        self._edge(self.a, self.b)          # same shape, different version — allowed
        self.assertEqual(TaskTemplateTaskDependency.objects.count(), 2)


# ---------------------------------------------------------------------------
# 3 — R-7: an edge is content of a template version
# ---------------------------------------------------------------------------

class TemplateEdgeImmutabilityTests(DependencyBase):

    def test_an_edge_cannot_be_added_to_an_active_version(self):
        self.template.activate()
        self.template.refresh_from_db()
        self.assertEqual(self.template.status, TaskTemplate.ACTIVE)

        with self.assertRaises(TemplateVersionLocked) as ctx:
            self._edge(self.a, self.b)

        self.assertIn('not a draft', str(ctx.exception))
        self.assertEqual(TaskTemplateTaskDependency.objects.count(), 0)

    def test_an_edge_cannot_be_added_to_an_archived_version(self):
        """Archived is frozen as hard as active — it is the record of what was built."""
        self.template.activate()
        successor_version = self._template(code='TESTDEP', version_no=2)
        self._tasks(successor_version, 'A2')
        successor_version.activate()
        self.template.refresh_from_db()
        self.assertEqual(self.template.status, TaskTemplate.ARCHIVED)

        with self.assertRaises(TemplateVersionLocked):
            self._edge(self.a, self.b)

    def test_an_existing_edge_cannot_be_deleted_from_an_active_version(self):
        edge = self._edge(self.a, self.b)
        self.template.activate()

        with self.assertRaises(TemplateVersionLocked):
            edge.delete()

        self.assertEqual(TaskTemplateTaskDependency.objects.count(), 1)

    def test_edges_are_authored_on_a_draft_then_activated_with_it(self):
        """The supported flow, asserted so the guard above reads as a rule not a wall."""
        self._edge(self.a, self.b)
        self._edge(self.b, self.c)
        self.template.activate()

        self.assertEqual(TaskTemplateTaskDependency.objects.count(), 2)


# ---------------------------------------------------------------------------
# 4 — Materialisation onto a project's own Task rows
# ---------------------------------------------------------------------------

class MaterialisationTests(DependencyBase):

    def setUp(self):
        super().setUp()
        self._edge(self.a, self.b)
        self._edge(self.b, self.c)
        self._edge(self.a, self.c)          # three edges: A->B, B->C, A->C
        self.template.activate()

        self.project = self._project()
        self.made    = self._build_from_template(self.project, self.template)

    def test_three_template_edges_become_three_instance_edges(self):
        created = materialise_task_dependencies(self.project)

        self.assertEqual(created, 3)
        self.assertEqual(TaskDependency.objects.count(), 3)

    def test_the_instance_edges_point_at_this_project_s_own_task_rows(self):
        materialise_task_dependencies(self.project)

        for edge in TaskDependency.objects.select_related(
            'predecessor__phase', 'successor__phase',
        ):
            self.assertEqual(edge.predecessor.phase.project_id, self.project.pk)
            self.assertEqual(edge.successor.phase.project_id, self.project.pk)

        pairs = {
            (e.predecessor.task_name, e.successor.task_name)
            for e in TaskDependency.objects.select_related('predecessor', 'successor')
        }
        self.assertEqual(pairs, {('A', 'B'), ('B', 'C'), ('A', 'C')})

    def test_materialisation_is_idempotent(self):
        materialise_task_dependencies(self.project)
        second = materialise_task_dependencies(self.project)

        self.assertEqual(second, 0, 'the second run created edges it should have skipped')
        self.assertEqual(TaskDependency.objects.count(), 3)

    def test_a_second_project_gets_its_own_edges_not_the_first_project_s(self):
        materialise_task_dependencies(self.project)

        other = self._project(customer_name='Second House')
        self._build_from_template(other, self.template)
        materialise_task_dependencies(other)

        self.assertEqual(TaskDependency.objects.count(), 6)
        self.assertEqual(
            TaskDependency.objects.filter(
                predecessor__phase__project=other).count(), 3)

    def test_a_superseded_template_version_does_not_change_an_existing_project(self):
        """B-10, restated for edges: the copy is the point.

        v2 is published with a different edge set. The already-materialised project
        keeps the edges it was built with, because they are rows of its own.
        """
        materialise_task_dependencies(self.project)
        before = set(TaskDependency.objects.values_list('predecessor_id', 'successor_id'))

        v2 = self._template(code='TESTDEP', version_no=2)
        p, q, r = self._tasks(v2, 'A', 'B', 'C')
        TaskTemplateTaskDependency(predecessor=r, successor=q).save()   # a different shape
        v2.activate()

        self.assertEqual(
            set(TaskDependency.objects.values_list('predecessor_id', 'successor_id')),
            before,
        )

    def test_a_hand_added_task_with_no_template_provenance_gets_no_edges(self):
        Task.objects.create(
            phase=self.project.phases.first(), task_name='Added by hand',
            task_order=99, assigned_role=Task.PM,
        )

        self.assertEqual(materialise_task_dependencies(self.project), 3)

    def test_a_project_with_no_template_tasks_materialises_nothing(self):
        bare = self._project(customer_name='Bare House')
        phase = ProjectPhase.objects.create(project=bare, phase_name='P', phase_order=1)
        Task.objects.create(phase=phase, task_name='Only', task_order=1)

        self.assertEqual(materialise_task_dependencies(bare), 0)
        self.assertEqual(
            TaskDependency.objects.filter(predecessor__phase__project=bare).count(), 0)


# ---------------------------------------------------------------------------
# 5 — The instance-side edge holds the same rules as the template-side one
# ---------------------------------------------------------------------------

class InstanceEdgeTests(DependencyBase):

    def setUp(self):
        super().setUp()
        self.template.activate()
        self.project = self._project()
        made = self._build_from_template(self.project, self.template)
        self.ta, self.tb, self.tc = (made[self.a.pk], made[self.b.pk], made[self.c.pk])

    def test_an_edge_between_two_tasks_of_one_project_saves(self):
        TaskDependency(predecessor=self.ta, successor=self.tb).save()

        self.assertEqual(TaskDependency.objects.count(), 1)

    def test_an_edge_spanning_two_projects_is_refused(self):
        other = self._project(customer_name='Other House')
        other_made = self._build_from_template(other, self.template)

        with self.assertRaises(ValidationError) as ctx:
            TaskDependency(predecessor=self.ta,
                           successor=other_made[self.b.pk]).save()

        self.assertIn('SAME project', str(ctx.exception))
        self.assertEqual(TaskDependency.objects.count(), 0)

    def test_a_self_edge_is_refused(self):
        with self.assertRaises(ValidationError):
            TaskDependency(predecessor=self.ta, successor=self.ta).save()

        with self.assertRaises(IntegrityError), transaction.atomic():
            TaskDependency.objects.bulk_create([
                TaskDependency(predecessor=self.ta, successor=self.ta),
            ])

    def test_a_duplicate_edge_is_refused(self):
        TaskDependency(predecessor=self.ta, successor=self.tb).save()

        with self.assertRaises(IntegrityError), transaction.atomic():
            TaskDependency.objects.create(predecessor=self.ta, successor=self.tb)

    def test_a_three_node_cycle_is_refused_on_the_instance_side_too(self):
        TaskDependency(predecessor=self.ta, successor=self.tb).save()
        TaskDependency(predecessor=self.tb, successor=self.tc).save()

        with self.assertRaises(DependencyCycle) as ctx:
            TaskDependency(predecessor=self.tc, successor=self.ta).save()

        self.assertIn('close a dependency loop', str(ctx.exception))
        self.assertEqual(TaskDependency.objects.count(), 2)

    def test_deleting_a_task_takes_its_edges_with_it(self):
        """on_delete=CASCADE, chosen deliberately — see the TaskDependency docstring."""
        TaskDependency(predecessor=self.ta, successor=self.tb).save()
        TaskDependency(predecessor=self.tb, successor=self.tc).save()

        self.tb.delete()

        self.assertEqual(TaskDependency.objects.count(), 0)


# ---------------------------------------------------------------------------
# 6 — incomplete_predecessors(): THE PREDICATE
#
# It reports. It never refuses. B-08.
# ---------------------------------------------------------------------------

class PredicateReportsNeverRefusesTests(DependencyBase):

    def setUp(self):
        super().setUp()
        self._edge(self.a, self.c)
        self._edge(self.b, self.c)          # C waits on both A and B
        self.template.activate()
        self.project = self._project()
        made = self._build_from_template(self.project, self.template)
        self.ta, self.tb, self.tc = (made[self.a.pk], made[self.b.pk], made[self.c.pk])
        materialise_task_dependencies(self.project)

    def test_it_returns_the_predecessors_that_are_not_done(self):
        self.ta.status = Task.DONE
        self.ta.save(update_fields=['status'])

        result = incomplete_predecessors(self.tc)

        self.assertEqual([t.pk for t in result], [self.tb.pk])

    def test_it_returns_every_predecessor_when_none_is_done(self):
        result = incomplete_predecessors(self.tc)

        self.assertEqual({t.pk for t in result}, {self.ta.pk, self.tb.pk})

    def test_it_returns_empty_when_every_predecessor_is_done(self):
        Task.objects.filter(pk__in=[self.ta.pk, self.tb.pk]).update(status=Task.DONE)

        self.assertEqual(incomplete_predecessors(self.tc), [])

    def test_blocked_and_in_progress_predecessors_still_count_as_incomplete(self):
        """Done is the only status that clears a predecessor. Not started, not blocked."""
        self.ta.status = Task.IN_PROGRESS
        self.ta.save(update_fields=['status'])
        self.tb.status = Task.BLOCKED
        self.tb.save(update_fields=['status'])

        self.assertEqual({t.pk for t in incomplete_predecessors(self.tc)},
                         {self.ta.pk, self.tb.pk})

    def test_incomplete_predecessors_returns_rather_than_raises(self):
        """B-08, pinned against a future session's instinct to block.

        A dependent task may be started early by anyone. This function REPORTS what is
        being jumped; refusing is not its job and must never become its job. If this
        test fails because the call now raises, the feature has been rewritten into
        something the product owner explicitly rejected — a hard block, which gets
        routed around by marking the predecessor Done and destroys the record the block
        existed to protect.
        """
        try:
            result = incomplete_predecessors(self.tc)
        except Exception as exc:                                    # noqa: BLE001
            self.fail(
                f'incomplete_predecessors() raised {type(exc).__name__}: {exc}. '
                f'B-08 says it reports and never refuses.'
            )

        self.assertTrue(result, 'the fixture has two incomplete predecessors')

    def test_the_early_start_itself_is_not_prevented(self):
        """Nothing in 1.4a stops the successor moving while its predecessors are open."""
        self.assertTrue(incomplete_predecessors(self.tc))

        self.tc.status = Task.IN_PROGRESS
        self.tc.save(update_fields=['status'])

        self.tc.refresh_from_db()
        self.assertEqual(self.tc.status, Task.IN_PROGRESS)

    def test_calling_it_changes_nothing(self):
        """Read-only. No I/O beyond ORM reads, no side effects."""
        before = dict(Task.objects.filter(phase__project=self.project)
                      .values_list('pk', 'status'))
        edges_before = TaskDependency.objects.count()

        incomplete_predecessors(self.tc)

        self.assertEqual(dict(Task.objects.filter(phase__project=self.project)
                              .values_list('pk', 'status')), before)
        self.assertEqual(TaskDependency.objects.count(), edges_before)

    def test_a_task_with_no_predecessors_returns_empty_in_one_query(self):
        """One query is the floor; a second would mean the predicate grew a side trip."""
        with self.assertNumQueries(1):
            result = incomplete_predecessors(self.ta)

        self.assertEqual(result, [])

    def test_a_successor_on_another_project_is_not_reported(self):
        other = self._project(customer_name='Third House')
        other_made = self._build_from_template(other, self.template)
        materialise_task_dependencies(other)

        result = incomplete_predecessors(other_made[self.c.pk])

        self.assertEqual({t.phase.project_id for t in result}, {other.pk})

    def test_the_result_is_ordered_the_way_the_tasks_appear_on_screen(self):
        result = incomplete_predecessors(self.tc)

        self.assertEqual(
            [(t.phase.phase_order, t.task_order) for t in result],
            sorted((t.phase.phase_order, t.task_order) for t in result),
        )


# ---------------------------------------------------------------------------
# 7 — Against the shape the product actually builds
#
# RESIDENTIAL v1 authors no edges. Materialising a real activated project must
# therefore be a well-behaved no-op rather than an error — 1.4a does not wire the
# call into activation, and whoever does must find it already safe here.
# ---------------------------------------------------------------------------

class RealResidentialActivationTests(TestCase):

    def setUp(self):
        self.finance = _profile('fin_dep', 'Finance',
                                email=RESIDENTIAL_FINANCE_ASSIGNEE_EMAIL)
        self.pm      = _profile('pm_real_dep', 'PM')
        self.design  = _profile('design_dep',  'Design')

    def test_materialising_a_real_residential_project_creates_no_edges_and_no_error(self):
        project = Project.objects.create(
            customer_name='Real House', customer_phone='9876543210',
            site_address='2 Sun Road', city='Lucknow', project_type='Residential',
            capacity_kw=Decimal('5.00'), status='Draft', assigned_pm=self.pm,
            target_commissioning_date=date.today() + timedelta(days=90),
        )
        client = Client()
        client.force_login(self.pm.user)
        response = client.post(
            reverse('project_activate', args=[project.project_id]),
            {'assigned_design_id': self.design.pk},
        )
        self.assertEqual(response.status_code, 302)
        project.refresh_from_db()
        self.assertEqual(project.status, 'Active')

        self.assertEqual(materialise_task_dependencies(project), 0)
        self.assertEqual(TaskDependency.objects.count(), 0)

    def test_every_task_of_a_real_project_reports_no_incomplete_predecessors(self):
        """1.4a ships no edges into production, so the predicate is empty everywhere.

        That is the property that makes 1.4b's warning safe to wire in: until somebody
        authors edges on a template version, nothing changes for anyone.
        """
        project = Project.objects.create(
            customer_name='Quiet House', customer_phone='9876543210',
            site_address='3 Sun Road', city='Lucknow', project_type='Residential',
            capacity_kw=Decimal('5.00'), status='Draft', assigned_pm=self.pm,
            target_commissioning_date=date.today() + timedelta(days=90),
        )
        client = Client()
        client.force_login(self.pm.user)
        client.post(reverse('project_activate', args=[project.project_id]),
                    {'assigned_design_id': self.design.pk})

        for task in Task.objects.filter(phase__project=project):
            self.assertEqual(incomplete_predecessors(task), [])
