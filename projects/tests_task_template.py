"""
Versioned task templates — prompt 0.4.

WHY THIS FILE EXISTS
--------------------
Prompt 0.4 moves the Residential template out of Python source (`build_residential_phases()`)
and into the database (`TaskTemplate` / `TaskTemplatePhase` / `TaskTemplateTask`), then
points `attach_residential_template()` at the rows instead of the literals.

That is a swap of the single most load-bearing function in the product — every Residential
project in the system was built by it — so the first section here DELIBERATELY DUPLICATES
`ActivationInvariantTests` from `tests_residential_baseline.py`. Those ten tests are the
regression net for this session and must pass UNMODIFIED; these restate the same facts
against the new mechanism so a failure says which half broke.

The rest tests what is genuinely new: R-7 immutability, the one-active-version constraint,
version 2 archiving version 1, and B-10.

B-10 — "what happens to an in-flight project when its template is upgraded?" — is answered
by `test_a_project_activated_under_v1_is_untouched_when_v2_activates`. The answer is
NOTHING, and it should pass trivially, because `Task.task_name`, `assigned_role`,
`task_type` and `duration_days` are plain COPIES taken at bulk_create rather than lookups
through a foreign key. A property that holds by construction is exactly the kind that gets
quietly broken later by someone adding the FK read that looks tidier. Asserted anyway.

NOTE ON THE FIXTURE: `test_settings` disables migrations, so migration 0067 never runs here
and the database starts with no template at all. That is the point — activation's
virgin-database bootstrap (`resolve_residential_template()`) is what these tests exercise,
and it seeds from the same `build_residential_phases()` the migration reads, through the
same `seed_task_template_version()` helper.

Run with:
    python manage.py test projects --settings=solarpms.test_settings
"""
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.test import Client, TestCase
from django.urls import reverse

from .models import (
    PaymentMilestone, Project, ProjectPhase, Task, TaskDurationTemplate, TaskTemplate,
    TaskTemplatePhase, TaskTemplateTask, TemplateVersionLocked, UserProfile,
)
from .utils import (
    INVOICE_TASK_NAMES, RESIDENTIAL_DURATION_DEFAULTS, RESIDENTIAL_FINANCE_ASSIGNEE_EMAIL,
    RESIDENTIAL_FINANCE_CONFIRMATION_TASK_NAMES, RESIDENTIAL_TEMPLATE_CODE,
    _get_duration, attach_residential_template, build_residential_phases,
    resolve_active_task_template, resolve_residential_template, seed_task_template_version,
)

#: The six task names activation back-assigns to the Finance assignee.
FINANCE_TASK_NAMES = INVOICE_TASK_NAMES + RESIDENTIAL_FINANCE_CONFIRMATION_TASK_NAMES


def _profile(username, role, email=''):
    """Create a User and set its auto-created UserProfile's role.

    A post_save signal on User creates the UserProfile, so we fetch and mutate rather
    than creating a second one. Same helper shape as tests_residential_baseline.py.
    """
    user = User.objects.create_user(username=username, password='pw12345', email=email)
    profile = user.profile
    profile.role = role
    profile.is_active = True
    profile.save()
    return profile


def _client_for(profile):
    client = Client()
    client.force_login(profile.user)
    return client


class TaskTemplateBase(TestCase):
    """One PM, one designer, and the Finance account without which nothing activates."""

    def setUp(self):
        # Required data: activation raises and rolls back without this account.
        self.finance = _profile('fin_tt', 'Finance',
                                email=RESIDENTIAL_FINANCE_ASSIGNEE_EMAIL)
        self.pm      = _profile('pm_tt',     'PM')
        self.design  = _profile('design_tt', 'Design')

    # -- fixture helpers -----------------------------------------------------

    def _draft(self, customer_name='Alpha Residence'):
        return Project.objects.create(
            customer_name=customer_name,
            customer_phone='9876543210',
            site_address='1 Sun Road',
            city='Lucknow',
            project_type='Residential',
            capacity_kw=Decimal('5.00'),
            status='Draft',
            assigned_pm=self.pm,
            target_commissioning_date=date.today() + timedelta(days=90),
        )

    def _activate(self, project):
        """Activate THROUGH THE REAL VIEW, so the fixture itself asserts that the
        PM-owns-project gate, the designer requirement and the atomic seed still work."""
        response = _client_for(self.pm).post(
            reverse('project_activate', args=[project.project_id]),
            {'assigned_design_id': self.design.pk},
        )
        self.assertEqual(response.status_code, 302, 'activation did not redirect')
        project.refresh_from_db()
        self.assertEqual(project.status, 'Active')
        return project

    def _make_and_activate(self, customer_name='Alpha Residence'):
        return self._activate(self._draft(customer_name))

    def _new_draft_version(self, version_no=2):
        """A second version of RESIDENTIAL, copied from the active one, left in draft."""
        active = resolve_active_task_template('Residential')
        draft = TaskTemplate.objects.create(
            code=RESIDENTIAL_TEMPLATE_CODE, label='Residential EPC',
            project_type='Residential', version_no=version_no,
            status=TaskTemplate.DRAFT,
        )
        for tpl_phase in active.phases.all():
            phase = TaskTemplatePhase.objects.create(
                template=draft, code=tpl_phase.code, label=tpl_phase.label,
                sort_order=tpl_phase.sort_order,
            )
            for tpl_task in tpl_phase.tasks.all():
                TaskTemplateTask.objects.create(
                    phase=phase, code=tpl_task.code, label=tpl_task.label,
                    sort_order=tpl_task.sort_order,
                    assigned_role=tpl_task.assigned_role,
                    task_type=tpl_task.task_type,
                    duration_days=tpl_task.duration_days,
                    is_payment_milestone=tpl_task.is_payment_milestone,
                )
        return draft


# ---------------------------------------------------------------------------
# 1 — Activation still produces exactly what it did before
#
# Deliberately duplicates ActivationInvariantTests. If both files fail, the template
# swap broke activation; if only this one fails, the duplication itself has drifted.
# ---------------------------------------------------------------------------

class ActivationUnchangedTests(TaskTemplateBase):

    def setUp(self):
        super().setUp()
        self.project = self._make_and_activate()

    def test_nine_phases_and_fifty_two_tasks(self):
        self.assertEqual(self.project.phases.count(), 9)
        self.assertEqual(Task.objects.filter(phase__project=self.project).count(), 52)

    def test_forty_four_internal_eight_external(self):
        tasks = Task.objects.filter(phase__project=self.project)
        self.assertEqual(tasks.filter(task_type=Task.INTERNAL).count(), 44)
        self.assertEqual(tasks.filter(task_type=Task.EXTERNAL).count(), 8)

    def test_three_payment_milestones_created_pending(self):
        milestones = self.project.milestones.order_by('milestone_name')
        self.assertEqual([m.milestone_name for m in milestones], ['M1', 'M2', 'M3'])
        for milestone in milestones:
            self.assertEqual(milestone.status, PaymentMilestone.PENDING)
            self.assertIsNone(milestone.amount)

    def test_pm_role_tasks_are_assigned_to_the_projects_pm(self):
        pm_tasks = Task.objects.filter(phase__project=self.project, assigned_role=Task.PM)
        self.assertEqual(pm_tasks.count(), 14)
        self.assertEqual(pm_tasks.exclude(assigned_to=self.pm).count(), 0)

    def test_the_six_named_finance_tasks_go_to_the_finance_assignee(self):
        finance_tasks = Task.objects.filter(phase__project=self.project,
                                            task_name__in=FINANCE_TASK_NAMES)
        self.assertEqual(finance_tasks.count(), 6)
        self.assertEqual(finance_tasks.exclude(assigned_to=self.finance).count(), 0)
        self.assertEqual(
            Task.objects.filter(phase__project=self.project,
                                assigned_role=Task.FINANCE).count(), 6,
        )

    def test_se_scm_design_and_bd_tasks_are_left_unassigned(self):
        """Activation back-assigns PM and the six Finance tasks and nothing else."""
        for role, expected in [(Task.SITE_ENGINEER, 14), (Task.SCM, 11),
                               (Task.DESIGN, 6), (Task.BD, 1)]:
            tasks = Task.objects.filter(phase__project=self.project, assigned_role=role)
            self.assertEqual(tasks.count(), expected, role)
            self.assertEqual(tasks.filter(assigned_to__isnull=True).count(), expected, role)

    def test_the_three_payment_milestone_flags_sit_on_the_finance_tasks(self):
        flagged = list(
            Task.objects.filter(phase__project=self.project, is_payment_milestone=True)
            .values_list('task_name', flat=True)
        )
        self.assertCountEqual(flagged, list(RESIDENTIAL_FINANCE_CONFIRMATION_TASK_NAMES))

    def test_phase_names_and_order_match_the_source_template(self):
        """The 9 phase names and their order, verbatim, against the seed the migration read."""
        expected = [(p['phase_order'], p['phase_name']) for p in build_residential_phases()]
        actual = list(self.project.phases.order_by('phase_order')
                      .values_list('phase_order', 'phase_name'))
        self.assertEqual(actual, expected)

    def test_every_task_name_role_type_and_order_matches_the_source_template(self):
        """The full 52-row comparison — name, role, type, order and milestone flag."""
        expected = sorted(
            (p['phase_name'], t['task_order'], t['task_name'], t['assigned_role'],
             t['task_type'], t.get('is_payment_milestone', False))
            for p in build_residential_phases() for t in p['tasks']
        )
        actual = sorted(
            (t.phase.phase_name, t.task_order, t.task_name, t.assigned_role,
             t.task_type, t.is_payment_milestone)
            for t in Task.objects.filter(phase__project=self.project).select_related('phase')
        )
        self.assertEqual(actual, expected)

    def test_due_dates_start_null(self):
        self.assertEqual(
            Task.objects.filter(phase__project=self.project,
                                due_date__isnull=False).count(), 0,
        )

    def test_activation_rolls_back_entirely_without_the_finance_assignee(self):
        """Activation is atomic, and reading the template from the database did not
        change that. The project must stay Draft with zero phases, tasks and milestones."""
        UserProfile.objects.filter(user__email=RESIDENTIAL_FINANCE_ASSIGNEE_EMAIL).delete()
        User.objects.filter(email=RESIDENTIAL_FINANCE_ASSIGNEE_EMAIL).delete()

        draft = self._draft('Charlie Residence')
        with self.assertRaises(UserProfile.DoesNotExist):
            _client_for(self.pm).post(
                reverse('project_activate', args=[draft.project_id]),
                {'assigned_design_id': self.design.pk},
            )

        draft.refresh_from_db()
        self.assertEqual(draft.status, 'Draft')
        self.assertIsNone(draft.activated_at)
        self.assertEqual(draft.phases.count(), 0)
        self.assertEqual(Task.objects.filter(phase__project=draft).count(), 0)
        self.assertEqual(draft.milestones.count(), 0)


# ---------------------------------------------------------------------------
# 2 — Durations resolve exactly as _get_duration() did
# ---------------------------------------------------------------------------

class TemplateDurationTests(TaskTemplateBase):

    def test_seeded_durations_match_the_old_resolution_for_all_52_tasks(self):
        """No TaskDurationTemplate rows: every task falls through to
        RESIDENTIAL_DURATION_DEFAULTS, then to 1."""
        self.assertEqual(TaskDurationTemplate.objects.count(), 0)
        template = resolve_residential_template()

        for tpl_task in TaskTemplateTask.objects.filter(phase__template=template):
            self.assertEqual(
                tpl_task.duration_days, _get_duration(tpl_task.label, {}),
                f'{tpl_task.label} duration diverged from the old resolution',
            )

    def test_the_three_invoice_tasks_fall_through_to_one_day(self):
        """They are in neither TaskDurationTemplate's seed nor
        RESIDENTIAL_DURATION_DEFAULTS, so the final `else 1` is what answers for them."""
        for name in INVOICE_TASK_NAMES:
            self.assertNotIn(name, RESIDENTIAL_DURATION_DEFAULTS)
        template = resolve_residential_template()
        for name in INVOICE_TASK_NAMES:
            tpl_task = TaskTemplateTask.objects.get(phase__template=template, label=name)
            self.assertEqual(tpl_task.duration_days, 1)

    def test_a_task_duration_template_override_is_carried_into_the_seeded_template(self):
        """THE OVERRIDE CASE. A TaskDurationTemplate row must still win over the
        hardcoded default, exactly as it did when _get_duration() ran at activation."""
        # 'Design' defaults to 2. Override it with a value that is neither the default
        # nor the fallback of 1, so a regression cannot pass by coincidence.
        self.assertEqual(RESIDENTIAL_DURATION_DEFAULTS['Design'], 2)
        TaskDurationTemplate.objects.create(
            project_type='residential', phase_name='Design', task_name='Design',
            task_type='Internal', duration_days=9,
        )

        template = resolve_residential_template()
        tpl_task = TaskTemplateTask.objects.get(phase__template=template, label='Design')
        self.assertEqual(tpl_task.duration_days, 9)

        # ...and it reaches the project, which is the thing that actually matters.
        project = self._make_and_activate()
        self.assertEqual(
            Task.objects.get(phase__project=project, task_name='Design').duration_days, 9,
        )

    def test_every_activated_task_carries_its_templates_duration(self):
        project = self._make_and_activate()
        for task in Task.objects.filter(phase__project=project).select_related('template_task'):
            self.assertEqual(task.duration_days, task.template_task.duration_days)


# ---------------------------------------------------------------------------
# 3 — R-7: a version is immutable once it leaves draft
# ---------------------------------------------------------------------------

class TemplateImmutabilityTests(TaskTemplateBase):

    def setUp(self):
        super().setUp()
        self.active = resolve_residential_template()
        self.phase  = self.active.phases.first()
        self.task   = self.phase.tasks.first()

    def test_editing_a_phase_on_an_active_template_raises(self):
        self.phase.label = 'Renamed'
        with self.assertRaises(TemplateVersionLocked):
            self.phase.save()

    def test_editing_a_task_on_an_active_template_raises(self):
        self.task.duration_days = 99
        with self.assertRaises(TemplateVersionLocked):
            self.task.save()

    def test_adding_a_task_to_an_active_template_raises(self):
        with self.assertRaises(TemplateVersionLocked):
            TaskTemplateTask.objects.create(
                phase=self.phase, code='SMUGGLED', label='Smuggled', sort_order=99,
                assigned_role=Task.PM, task_type=Task.INTERNAL,
            )

    def test_deleting_a_phase_or_task_on_an_active_template_raises(self):
        with self.assertRaises(TemplateVersionLocked):
            self.task.delete()
        with self.assertRaises(TemplateVersionLocked):
            self.phase.delete()

    def test_an_archived_version_is_frozen_just_as_hard(self):
        """Archived is not "retired and therefore harmless" — it is the record of what a
        project built last month was built from, and rewriting it would make that a lie."""
        self._new_draft_version(2).activate()
        self.active.refresh_from_db()
        self.assertEqual(self.active.status, TaskTemplate.ARCHIVED)

        stale = TaskTemplateTask.objects.get(pk=self.task.pk)
        stale.duration_days = 99
        with self.assertRaises(TemplateVersionLocked):
            stale.save()

    def test_editing_a_draft_template_succeeds(self):
        draft = self._new_draft_version(2)
        phase = draft.phases.first()
        phase.label = 'Renamed In Draft'
        phase.save()
        phase.refresh_from_db()
        self.assertEqual(phase.label, 'Renamed In Draft')

        task = phase.tasks.first()
        task.duration_days = 7
        task.save()
        task.refresh_from_db()
        self.assertEqual(task.duration_days, 7)

        # ...and adding and removing rows works too.
        extra = TaskTemplateTask.objects.create(
            phase=phase, code='EXTRA', label='Extra Draft Task', sort_order=99,
            assigned_role=Task.PM, task_type=Task.INTERNAL,
        )
        extra.delete()
        self.assertFalse(TaskTemplateTask.objects.filter(code='EXTRA').exists())

    def test_activating_a_non_draft_version_raises(self):
        with self.assertRaises(TemplateVersionLocked):
            self.active.activate()


# ---------------------------------------------------------------------------
# 4 — Versioning: one active version, and the v1 -> v2 handover
# ---------------------------------------------------------------------------

class TemplateVersioningTests(TaskTemplateBase):

    def setUp(self):
        super().setUp()
        self.v1 = resolve_residential_template()

    def test_two_active_versions_of_one_code_cannot_exist(self):
        """The partial unique constraint, not application code, is what forbids it."""
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                TaskTemplate.objects.create(
                    code=RESIDENTIAL_TEMPLATE_CODE, label='Rival', version_no=2,
                    project_type='Residential', status=TaskTemplate.ACTIVE,
                )

    def test_two_versions_with_the_same_number_cannot_exist(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                TaskTemplate.objects.create(
                    code=RESIDENTIAL_TEMPLATE_CODE, label='Rival', version_no=1,
                    project_type='Residential', status=TaskTemplate.DRAFT,
                )

    def test_drafts_and_archives_may_coexist_beside_the_active_version(self):
        """The constraint is partial for exactly this reason — the history is kept."""
        self._new_draft_version(2)
        self._new_draft_version(3)
        self.assertEqual(
            TaskTemplate.objects.filter(code=RESIDENTIAL_TEMPLATE_CODE).count(), 3)
        self.assertEqual(
            TaskTemplate.objects.filter(code=RESIDENTIAL_TEMPLATE_CODE,
                                        status=TaskTemplate.ACTIVE).count(), 1)

    def test_activating_v2_archives_v1_in_one_transaction(self):
        v2 = self._new_draft_version(2)
        v2.activate()

        self.v1.refresh_from_db()
        v2.refresh_from_db()
        self.assertEqual(self.v1.status, TaskTemplate.ARCHIVED)
        self.assertEqual(v2.status, TaskTemplate.ACTIVE)
        self.assertIsNotNone(v2.effective_from)
        # There is no instant, and no end state, with two active versions or none.
        self.assertEqual(
            TaskTemplate.objects.filter(code=RESIDENTIAL_TEMPLATE_CODE,
                                        status=TaskTemplate.ACTIVE).count(), 1)

    def test_activation_reads_the_new_version_once_it_is_active(self):
        v2 = self._new_draft_version(2)
        extra_phase = TaskTemplatePhase.objects.create(
            template=v2, code='HANDOVER', label='Handover', sort_order=10)
        TaskTemplateTask.objects.create(
            phase=extra_phase, code='FINAL_WALKTHROUGH', label='Final Walkthrough',
            sort_order=1, assigned_role=Task.PM, task_type=Task.INTERNAL,
            duration_days=3,
        )
        v2.activate()

        project = self._make_and_activate('Delta Residence')
        self.assertEqual(project.phases.count(), 10)
        self.assertEqual(Task.objects.filter(phase__project=project).count(), 53)
        walkthrough = Task.objects.get(phase__project=project, task_name='Final Walkthrough')
        self.assertEqual(walkthrough.duration_days, 3)
        self.assertEqual(walkthrough.assigned_to, self.pm)   # PM back-assignment still applies

    def test_activation_raises_when_a_template_exists_but_none_is_active(self):
        """The third outcome of resolve_residential_template(): rows exist, none active.
        That is an operator error, and inventing a version to paper over it would hide
        it. Contrast with the virgin-database bootstrap, which every other test uses."""
        TaskTemplate.objects.filter(pk=self.v1.pk).update(status=TaskTemplate.ARCHIVED)
        with self.assertRaises(TaskTemplate.DoesNotExist):
            resolve_residential_template()

    def test_the_bootstrap_seeds_exactly_one_version_and_is_not_repeated(self):
        """resolve_residential_template() seeds v1 on a virgin database and then leaves
        it alone — a second call must not create v2."""
        self.assertEqual(self.v1.version_no, 1)
        self.assertEqual(self.v1.status, TaskTemplate.ACTIVE)
        again = resolve_residential_template()
        self.assertEqual(again.pk, self.v1.pk)
        self.assertEqual(
            TaskTemplate.objects.filter(code=RESIDENTIAL_TEMPLATE_CODE).count(), 1)

    def test_the_seeded_template_carries_stable_codes(self):
        """`code` is the cross-version identity: v2 may reword a label, and `code` is
        what says it is still the same row."""
        phase = TaskTemplatePhase.objects.get(template=self.v1, label='Sales & Documentation')
        self.assertEqual(phase.code, 'SALES_DOCUMENTATION')
        task = TaskTemplateTask.objects.get(phase__template=self.v1,
                                            label='OCR, Documentation & Verification')
        self.assertEqual(task.code, 'OCR_DOCUMENTATION_VERIFICATION')

    def test_codes_are_unique_within_their_scope_across_the_whole_template(self):
        phase_codes = list(TaskTemplatePhase.objects.filter(template=self.v1)
                           .values_list('code', flat=True))
        self.assertEqual(len(phase_codes), len(set(phase_codes)))
        task_codes = list(TaskTemplateTask.objects.filter(phase__template=self.v1)
                          .values_list('code', flat=True))
        self.assertEqual(len(task_codes), len(set(task_codes)))


# ---------------------------------------------------------------------------
# 5 — B-10: an in-flight project is untouched by a template upgrade
# ---------------------------------------------------------------------------

class InFlightProjectIsolationTests(TaskTemplateBase):
    """B-10, answered. In-flight projects hold their own COPIED rows, so a template
    upgrade cannot reach them. This should pass trivially; that is the reason to assert
    it, not a reason to skip it — a property that holds by construction is exactly the
    kind someone later breaks by replacing the copy with the FK read that looks tidier."""

    def setUp(self):
        super().setUp()
        self.v1 = resolve_residential_template()
        self.project = self._make_and_activate('Echo Residence')
        self.before = sorted(
            (t.phase.phase_name, t.task_order, t.task_name, t.assigned_role,
             t.task_type, t.duration_days, t.is_payment_milestone)
            for t in Task.objects.filter(phase__project=self.project).select_related('phase')
        )

    def _publish_v2_that_changes_everything(self):
        v2 = self._new_draft_version(2)
        # Reword, re-time, re-role and re-flag every task, and drop a whole phase.
        for tpl_task in TaskTemplateTask.objects.filter(phase__template=v2):
            tpl_task.label = f'V2 {tpl_task.label}'
            tpl_task.duration_days = 42
            tpl_task.assigned_role = Task.SCM
            tpl_task.task_type = Task.EXTERNAL
            tpl_task.is_payment_milestone = False
            tpl_task.save()
        v2.phases.filter(code='FINANCE_CLOSURE').first().delete()
        return v2.activate()

    def test_a_project_activated_under_v1_is_untouched_when_v2_activates(self):
        self._publish_v2_that_changes_everything()

        after = sorted(
            (t.phase.phase_name, t.task_order, t.task_name, t.assigned_role,
             t.task_type, t.duration_days, t.is_payment_milestone)
            for t in Task.objects.filter(phase__project=self.project).select_related('phase')
        )
        self.assertEqual(after, self.before,
                         'a template upgrade reached an in-flight project — B-10 is open again')
        self.assertEqual(self.project.phases.count(), 9)
        self.assertEqual(Task.objects.filter(phase__project=self.project).count(), 52)

    def test_the_next_project_gets_v2_while_the_old_one_keeps_v1(self):
        self._publish_v2_that_changes_everything()
        fresh = self._make_and_activate('Foxtrot Residence')

        self.assertEqual(fresh.phases.count(), 8)          # Finance Closure dropped
        self.assertEqual(Task.objects.filter(phase__project=fresh).count(), 51)
        self.assertEqual(self.project.phases.count(), 9)   # ...and the old one is intact
        self.assertEqual(Task.objects.filter(phase__project=self.project).count(), 52)

    def test_retiring_a_template_version_never_cascades_a_projects_tasks_away(self):
        """template_task is SET_NULL. Provenance can be lost; the work cannot."""
        v1_tasks = list(TaskTemplateTask.objects.filter(phase__template=self.v1))
        self.assertEqual(len(v1_tasks), 52)
        # Hard-delete the whole version — the harshest thing that can happen to it.
        TaskTemplate.objects.filter(pk=self.v1.pk).delete()

        self.assertEqual(Task.objects.filter(phase__project=self.project).count(), 52)
        self.assertEqual(
            Task.objects.filter(phase__project=self.project,
                                template_task__isnull=True).count(), 52)


# ---------------------------------------------------------------------------
# 6 — Task.template_task provenance
# ---------------------------------------------------------------------------

class TemplateProvenanceTests(TaskTemplateBase):

    def test_template_task_is_populated_for_a_newly_activated_project(self):
        project = self._make_and_activate()
        tasks = Task.objects.filter(phase__project=project).select_related('template_task')
        self.assertEqual(tasks.count(), 52)
        self.assertEqual(tasks.filter(template_task__isnull=True).count(), 0)

    def test_each_task_points_at_the_template_row_it_was_copied_from(self):
        project = self._make_and_activate()
        for task in (Task.objects.filter(phase__project=project)
                     .select_related('template_task', 'template_task__phase', 'phase')):
            self.assertEqual(task.task_name, task.template_task.label)
            self.assertEqual(task.phase.phase_name, task.template_task.phase.label)
            self.assertEqual(task.task_order, task.template_task.sort_order)

    def test_a_hand_added_task_carries_no_provenance(self):
        """The backfill leaves hand-added tasks null on purpose — they belong to no
        template row, and inventing one for them would be a false record."""
        project = self._make_and_activate()
        hand_added = Task.objects.create(
            phase=project.phases.first(), task_name='Site Survey', task_order=99,
            assigned_role=Task.SITE_ENGINEER, task_type=Task.INTERNAL,
        )
        self.assertIsNone(hand_added.template_task)


# ---------------------------------------------------------------------------
# 7 — The screens that used to edit TaskDurationTemplate
# ---------------------------------------------------------------------------

class DurationScreensAreReadOnlyTests(TaskTemplateBase):
    """Both screens, together. Repointing one and not the other would be the exact
    "behaviour depends on which button you pressed" defect this codebase carries three
    of already (B-2, B-5, B-7)."""

    SCREENS = [
        ('admin_task_durations',    'Admin'),
        ('subadmin_task_durations', 'System Admin'),
    ]

    def setUp(self):
        super().setUp()
        self.template = resolve_residential_template()
        self.actors = {
            'Admin':        _profile('admin_tt', 'Admin'),
            'System Admin': _profile('sa_tt',    'System Admin'),
        }

    def test_both_screens_render_the_active_templates_durations(self):
        for url_name, role in self.SCREENS:
            with self.subTest(screen=url_name):
                response = _client_for(self.actors[role]).get(reverse(url_name))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context['total'], 52)
                self.assertEqual(response.context['template'].pk, self.template.pk)
                self.assertEqual(len(response.context['grouped']), 9)

    def test_neither_screen_writes_anything_on_post(self):
        """A bookmarked old form must not silently save a value nothing reads."""
        tpl_task = TaskTemplateTask.objects.get(phase__template=self.template, label='Design')
        original = tpl_task.duration_days

        for url_name, role in self.SCREENS:
            with self.subTest(screen=url_name):
                response = _client_for(self.actors[role]).post(
                    reverse(url_name), {f'duration_{tpl_task.pk}': '99'})
                self.assertEqual(response.status_code, 302)
                tpl_task.refresh_from_db()
                self.assertEqual(tpl_task.duration_days, original)
                self.assertEqual(TaskDurationTemplate.objects.count(), 0)

    def test_the_screens_do_not_read_task_duration_template(self):
        """A TaskDurationTemplate row that disagrees with the active template must not
        appear — the whole point is that this table no longer answers for anything."""
        TaskDurationTemplate.objects.create(
            project_type='residential', phase_name='Design', task_name='Design',
            task_type='Internal', duration_days=77,
        )
        for url_name, role in self.SCREENS:
            with self.subTest(screen=url_name):
                response = _client_for(self.actors[role]).get(reverse(url_name))
                self.assertNotContains(response, '77')


# ---------------------------------------------------------------------------
# 8 — The seeding helper is shared with migration 0067
# ---------------------------------------------------------------------------

class SeedHelperTests(TaskTemplateBase):
    """Migration 0067 and the runtime bootstrap call the same function, so a template
    seeded from model state is the one the migration would have written."""

    def test_the_helper_creates_a_draft_and_leaves_it_active(self):
        template = seed_task_template_version(
            template_model=TaskTemplate, phase_model=TaskTemplatePhase,
            task_model=TaskTemplateTask,
            code='TESTCODE', label='Test', project_type='Residential', version_no=1,
            phases=build_residential_phases(),
            duration_resolver=lambda name: _get_duration(name, {}),
        )
        self.assertEqual(template.status, TaskTemplate.ACTIVE)
        self.assertIsNotNone(template.effective_from)
        self.assertEqual(template.phases.count(), 9)
        self.assertEqual(TaskTemplateTask.objects.filter(phase__template=template).count(), 52)

    def test_the_helper_archives_a_previous_active_version_of_the_same_code(self):
        v1 = resolve_residential_template()
        seed_task_template_version(
            template_model=TaskTemplate, phase_model=TaskTemplatePhase,
            task_model=TaskTemplateTask,
            code=RESIDENTIAL_TEMPLATE_CODE, label='Residential EPC',
            project_type='Residential', version_no=2,
            phases=build_residential_phases(),
            duration_resolver=lambda name: _get_duration(name, {}),
        )
        v1.refresh_from_db()
        self.assertEqual(v1.status, TaskTemplate.ARCHIVED)
        self.assertEqual(
            TaskTemplate.objects.filter(code=RESIDENTIAL_TEMPLATE_CODE,
                                        status=TaskTemplate.ACTIVE).count(), 1)

    def test_attach_residential_template_can_be_called_directly(self):
        """Not every caller comes through project_activate — the function must stand on
        its own, bootstrap included."""
        project = self._draft('Golf Residence')
        project.activated_at = None
        attach_residential_template(project)
        self.assertEqual(ProjectPhase.objects.filter(project=project).count(), 9)
        self.assertEqual(Task.objects.filter(phase__project=project).count(), 52)
