"""Phase-rule warnings, OPEX template v1 only (phase_rules.py, PhaseRuleOverride, 0117).

B-08 STANDS: nothing here may refuse a start or a challan. Every test that bypasses a
rule also asserts the work went ahead. What a bypassed rule produces is one override row
per rule, written with the start, its reason left for the site's PM side
(user_can_manage_project) — B-08 as amended on 20 Oct 2026.

EVERY OPEX FIXTURE IS A REALLY ACTIVATED SITE (opex_site_activate), and every start goes
through the real status views, as tests_two_step_completion does: a hand-made status
proves the `if` and nothing about whether production reaches it.

Run with:
    python manage.py test projects.tests_phase_rules --settings=solarpms.test_settings
"""
from datetime import date, timedelta
from decimal import Decimal
from importlib import import_module

from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.test import Client, RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from .models import (
    DESIGN_IN_DESIGN, DESIGN_RELEASED, ActivityLog, AppendOnlyViolation, DCLineItem,
    DeliveryChallan, DesignAssignment, Notification, PhaseRuleOverride, Project,
    ProjectPhase, Task, TaskTemplate, TaskTemplatePhase, TaskTemplateTask, UserProfile,
)
from .phase_rules import (
    ALREADY_EXPLAINED, EXPLAINED, SiteRuleState, explain_phase_rule_override,
    phase_rule_warnings, start_warnings,
)
from .utils import assign_task_to, resolve_residential_template


class _ConcreteApps:
    """Stands in for the `apps` registry 0075's RunPython function is handed."""

    _MODELS = {
        'TaskTemplate':      TaskTemplate,
        'TaskTemplatePhase': TaskTemplatePhase,
        'TaskTemplateTask':  TaskTemplateTask,
        'Task':              Task,
    }

    def get_model(self, app_label, model_name):
        assert app_label == 'projects'
        return self._MODELS[model_name]


def _seed_opex():
    import_module('projects.migrations.0075_seed_opex_template_v1').seed_opex_v1(
        _ConcreteApps(), None)


def _profile(username, role, **flags):
    """A user whose signal-created profile is given `role` (updated, never created —
    signals.py creates the profile on post_save)."""
    user = User.objects.create_user(username=username, password='x',
                                    first_name=username.title(), last_name='Test')
    profile = user.profile
    profile.role = role
    for field, value in flags.items():
        setattr(profile, field, value)
    profile.save()
    return profile


def _client(profile):
    client = Client()
    client.force_login(profile.user)
    return client


AWAITING_LINK = '#started-early'


class PhaseRuleFixture(TestCase):
    """One really activated OPEX site with its PM, a coordinator, a site engineer
    holding every Site Engineer task, an SCM user and an outsider."""

    @classmethod
    def setUpTestData(cls):
        resolve_residential_template()
        _seed_opex()
        cls.pm = _profile('pr_pm', 'PM')
        cls.coord = _profile('pr_coord', 'Project Coordinator')
        cls.se = _profile('pr_se', 'Site Engineer')
        cls.scm = _profile('pr_scm', 'SCM')
        cls.outsider = _profile('pr_out', 'Site Engineer')

    def setUp(self):
        self.site = Project.objects.create(
            customer_name='Phase Rule Site', customer_phone='9876543210',
            site_address='1 Sequence Road', city='Lucknow', project_type='OPEX',
            dc_capacity_kw=Decimal('100.00'), status='Draft', assigned_pm=self.pm,
        )
        response = _client(self.pm).post(
            reverse('opex_site_activate', args=[self.site.project_id]))
        self.assertEqual(response.status_code, 302)
        self.site.refresh_from_db()
        self.assertEqual(self.site.status, 'Active')
        self.site.coordinators.add(self.coord)
        for task in Task.objects.filter(phase__project=self.site,
                                        assigned_role='Site Engineer', is_mirror=False):
            assign_task_to(task, self.se, notify=False)
        for task in Task.objects.filter(phase__project=self.site, is_mirror=False,
                                        assigned_to__isnull=True):
            assign_task_to(task, self.pm, notify=False)

    # -- helpers -----------------------------------------------------------------

    def task(self, name):
        return Task.objects.get(phase__project=self.site, task_name=name)

    def start(self, task, who, reason=None, hx=False, status=Task.IN_PROGRESS, view='task_status_update'):
        data = {'status': status, 'due_date': (date.today() + timedelta(days=10)).isoformat()}
        if reason is not None:
            data['phase_rule_reason'] = reason
        extra = {'HTTP_HX_REQUEST': 'true'} if hx else {}
        with self.captureOnCommitCallbacks(execute=True):
            response = _client(who).post(
                reverse(view, args=[self.site.project_id, task.pk]), data, **extra)
        self.assertIn(response.status_code, (200, 302))
        return response

    def block(self, task, who):
        with self.captureOnCommitCallbacks(execute=True):
            _client(who).post(
                reverse('task_status_update', args=[self.site.project_id, task.pk]),
                {'status': Task.BLOCKED, 'block_issue_title': 'Waiting on access'})
        self.assertEqual(Task.objects.get(pk=task.pk).status, Task.BLOCKED)

    def release_design(self, status=DESIGN_RELEASED):
        # A fixture, not the design flow: what is under test is the rule reading the
        # status, and the design module's own suites own how it gets there.
        assignment, _ = DesignAssignment.objects.get_or_create(project=self.site)
        DesignAssignment.objects.filter(pk=assignment.pk).update(status=status)

    def receive(self, category=DCLineItem.SOLAR_MODULES, received=5, damaged=0,
                dc_status=DeliveryChallan.PARTIALLY_RECEIVED, number='FX-1'):
        dc = DeliveryChallan.objects.create(
            project=self.site, dc_number=number, dc_date=date.today(), status=dc_status)
        DCLineItem.objects.create(
            challan=dc, boq_category=category, item_description='fixture',
            ordered_quantity=Decimal('10'), received_quantity=Decimal(received),
            damaged_quantity=damaged)
        return dc

    def rows(self, task=None, **kw):
        qs = PhaseRuleOverride.objects.filter(project=self.site, **kw)
        return qs.filter(task=task) if task is not None else qs

    def notices(self, profile):
        return Notification.objects.filter(recipient=profile, link__endswith=AWAITING_LINK)

    def create_dc(self, who=None, number='DC-PR-1'):
        data = {'dc_number': number, 'dc_date': date.today().isoformat(),
                'line_item_category_0': DCLineItem.SOLAR_MODULES,
                'line_item_description_0': 'Module 550Wp', 'line_item_qty_0': '10',
                'line_item_unit_0': 'Nos'}
        with self.captureOnCommitCallbacks(execute=True):
            response = _client(who or self.scm).post(
                reverse('create_delivery_challan', args=[self.site.project_id]), data)
        self.assertEqual(response.status_code, 302)
        return DeliveryChallan.objects.get(project=self.site, dc_number=number)


class EachRuleWritesARowAndProceedsTests(PhaseRuleFixture):
    """R1..R6: the work goes ahead and one row awaiting a reason records the rule."""

    def assert_started_with_row(self, task, rule):
        self.assertEqual(Task.objects.get(pk=task.pk).status, Task.IN_PROGRESS,
                         'B-08: a phase rule never refuses the start')
        row = self.rows(task).get()
        self.assertEqual(row.rule_code, rule)
        self.assertEqual(row.rule_set, 'OPEX v1')
        self.assertIsNone(row.reason)
        self.assertIsNone(row.explained_at)
        return row

    def test_r1_challan_before_design_release(self):
        dc = self.create_dc()
        row = self.rows(delivery_challan=dc).get()
        self.assertEqual(row.rule_code, 'R1')
        self.assertEqual(row.rule_text, 'Design not released yet')
        self.assertEqual(row.started_by, self.scm)
        self.assertIsNone(row.task)

    def test_r1_silent_once_design_released(self):
        self.release_design()
        self.create_dc()
        self.assertFalse(self.rows().exists())

    def test_r2_net_metering_before_design_release(self):
        task = self.task('Net Metering Approval')
        self.start(task, self.pm)
        self.assert_started_with_row(task, 'R2')

    def test_r2_ceig_silent_once_released(self):
        self.release_design()
        task = self.task('CEIG Approval')
        self.start(task, self.pm)
        self.assertEqual(Task.objects.get(pk=task.pk).status, Task.IN_PROGRESS)
        self.assertFalse(self.rows(task).exists())

    def test_r3_installation_with_no_material(self):
        task = self.task('Module Installation')
        self.start(task, self.se)
        row = self.assert_started_with_row(task, 'R3')
        self.assertEqual(row.rule_text, 'No material received at site yet')
        self.assertEqual(row.started_by, self.se)

    def test_r3_satisfied_by_damaged_material(self):
        self.receive(received=3, damaged=3)
        task = self.task('Module Installation')
        self.start(task, self.se)
        self.assertFalse(self.rows(task).exists())

    def test_r3_satisfied_by_a_rejected_challan_with_received_quantity(self):
        """Ruling Q2: any line with received_quantity > 0, whatever the DC's status."""
        self.receive(received=2, damaged=1, dc_status=DeliveryChallan.REJECTED)
        task = self.task('Civil Work and MMS Installation')
        self.start(task, self.se)
        self.assertEqual(Task.objects.get(pk=task.pk).status, Task.IN_PROGRESS)
        self.assertFalse(self.rows(task).exists())

    def test_r3_not_satisfied_by_a_challan_with_nothing_received(self):
        self.receive(received=0, dc_status=DeliveryChallan.REJECTED)
        task = self.task('Module Installation')
        self.start(task, self.se)
        self.assert_started_with_row(task, 'R3')

    def test_r4_testing_before_any_installation(self):
        self.receive()
        self.start(self.task('Testing & Commissioning'), self.se)
        self.assert_started_with_row(self.task('Testing & Commissioning'), 'R4')

    def test_r4_silent_once_installation_started(self):
        self.receive()
        self.start(self.task('Module Installation'), self.se)
        task = self.task('Net Meter Installation')
        self.start(task, self.se)
        self.assertFalse(self.rows(task).exists())

    def test_r5_post_installation_while_testing_not_done(self):
        task = self.task('Post-Installation Approvals')
        self.start(task, self.pm)
        self.assert_started_with_row(task, 'R5')

    def test_r5_all_na_testing_phase_counts_as_finished(self):
        Task.objects.filter(phase__project=self.site,
                            phase__phase_name='Testing & Commissioning').update(is_not_applicable=True)
        task = self.task('Post-Installation Approvals')
        self.start(task, self.pm)
        self.assertFalse(self.rows(task).exists())

    def test_r6_completion_certificates_before_post_installation(self):
        task = self.task('Completion Certificates (Paperwork)')
        self.start(task, self.pm)
        self.assert_started_with_row(task, 'R6')

    def test_r6_silent_once_post_installation_started(self):
        self.start(self.task('Post-Installation Approvals'), self.pm)
        task = self.task('Completion Certificates (Paperwork)')
        self.start(task, self.pm)
        self.assertFalse(self.rows(task).exists())

    def test_the_task_detail_screen_records_too(self):
        task = self.task('Module Installation')
        self.start(task, self.se, view='task_detail_status_update')
        self.assert_started_with_row(task, 'R3')

    def test_activity_log_lines(self):
        task = self.task('Module Installation')
        self.start(task, self.se)
        row = self.rows(task).get()
        self.assertTrue(ActivityLog.objects.filter(
            action_code='phase_rule_override', entity_id=row.pk).exists())
        explain_phase_rule_override(row, 'Client asked for early mobilisation.', self.pm)
        self.assertTrue(ActivityLog.objects.filter(
            action_code='phase_rule_override_explained', entity_id=row.pk).exists())


class NotApplicableNeverStartedTests(PhaseRuleFixture):
    """Ruling Q3: an N/A task never counts as started, whatever its stored status."""

    def test_na_installation_task_in_progress_does_not_satisfy_r4(self):
        self.receive()
        Task.objects.filter(pk=self.task('Module Installation').pk).update(
            status=Task.IN_PROGRESS, is_not_applicable=True)
        task = self.task('Testing & Commissioning')
        self.start(task, self.se)
        self.assertEqual(self.rows(task).get().rule_code, 'R4')


class FirstStartTests(PhaseRuleFixture):
    """Ruling Q1: Not Started -> Blocked is not checked; Blocked -> In Progress is,
    when the task has never been In Progress before."""

    def test_not_started_to_blocked_writes_nothing(self):
        task = self.task('Module Installation')
        self.block(task, self.se)
        self.assertFalse(self.rows(task).exists())

    def test_blocked_to_in_progress_of_a_never_started_task_writes_a_row(self):
        task = self.task('Module Installation')
        self.block(task, self.se)
        self.start(task, self.se)
        self.assertEqual(Task.objects.get(pk=task.pk).status, Task.IN_PROGRESS)
        self.assertEqual(self.rows(task).get().rule_code, 'R3')

    def test_restart_after_a_real_start_writes_no_second_row(self):
        task = self.task('Module Installation')
        self.start(task, self.se)
        self.block(task, self.se)
        self.start(task, self.se)
        self.assertEqual(self.rows(task).count(), 1)

    def test_blocked_restart_of_a_started_task_is_not_a_first_start(self):
        """Started before the rule could be checked (no row): not asked again."""
        self.receive()
        task = self.task('Module Installation')
        self.start(task, self.se)            # material present: no row
        DCLineItem.objects.all().delete()    # material gone from the facts
        self.block(task, self.se)
        self.start(task, self.se)
        self.assertFalse(self.rows(task).exists())


class ReasonTests(PhaseRuleFixture):
    """Who gives the reason, and when (B-08 as amended; rulings Q5)."""

    def test_se_start_shows_no_reason_form(self):
        task = self.task('Module Installation')
        detail = _client(self.se).get(reverse('task_detail', args=[self.site.project_id, task.pk]))
        self.assertNotContains(detail, 'name="phase_rule_reason"')
        overview = _client(self.se).get(reverse('project_overview', args=[self.site.project_id]))
        self.assertNotContains(overview, 'data-phase-rules=')
        self.assertNotContains(overview, 'id="phaseRuleStartModal"')

    def test_se_cannot_explain_inline_reason_is_ignored(self):
        task = self.task('Module Installation')
        self.start(task, self.se, reason='I decided to.')
        row = self.rows(task).get()
        self.assertIsNone(row.reason)
        self.assertIsNone(row.explained_by)

    def test_pm_sees_the_rules_and_the_reason_field(self):
        task = self.task('Net Metering Approval')
        detail = _client(self.pm).get(reverse('task_detail', args=[self.site.project_id, task.pk]))
        self.assertContains(detail, 'name="phase_rule_reason"')
        self.assertContains(detail, 'Design not released yet')
        overview = _client(self.pm).get(reverse('project_overview', args=[self.site.project_id]))
        self.assertContains(overview, 'data-phase-rules="Design not released yet"')
        self.assertContains(overview, 'id="phaseRuleStartModal"')

    def test_pm_start_carries_an_inline_reason(self):
        task = self.task('Net Metering Approval')
        self.start(task, self.pm, reason='DISCOM window closes Friday.')
        row = self.rows(task).get()
        self.assertEqual(row.reason, 'DISCOM window closes Friday.')
        self.assertEqual(row.explained_by, self.pm)
        self.assertIsNotNone(row.explained_at)
        self.assertEqual(self.notices(self.coord).count(), 0,
                         'an explained row awaits nothing, so nobody is asked')

    def test_manager_blank_inline_reason_leaves_the_row_awaiting(self):
        task = self.task('Net Metering Approval')
        self.start(task, self.pm, reason='   ')
        row = self.rows(task).get()
        self.assertIsNone(row.reason)
        self.assertIsNone(row.explained_at)

    def test_coordinator_counts_as_the_pm_side(self):
        task = self.task('Completion Certificates (Paperwork)')
        assign_task_to(task, self.coord, notify=False)
        self.start(task, self.coord, reason='Client paperwork ready early.',
                   view='task_detail_status_update')
        self.assertEqual(self.rows(task).get().explained_by, self.coord)


class ExplainTests(PhaseRuleFixture):
    """Only the PM side explains; explained rows are immutable."""

    def setUp(self):
        super().setUp()
        self.target = self.task('Module Installation')
        self.start(self.target, self.se)
        self.row = self.rows(self.target).get()
        self.url = reverse('phase_rule_override_explain', args=[self.site.project_id, self.row.pk])

    def explain(self, who, reason='Crane only available this week.', hx=False):
        extra = {'HTTP_HX_REQUEST': 'true'} if hx else {}
        return _client(who).post(self.url, {'reason': reason}, **extra)

    def test_pm_explains_from_the_overview(self):
        response = self.explain(self.pm, hx=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Crane only available this week.')
        self.row = PhaseRuleOverride.objects.get(pk=self.row.pk)
        self.assertEqual(self.row.explained_by, self.pm)

    def test_coordinator_may_explain(self):
        self.assertEqual(self.explain(self.coord).status_code, 302)
        self.assertEqual(PhaseRuleOverride.objects.get(pk=self.row.pk).explained_by, self.coord)

    def test_site_engineer_may_not_explain(self):
        self.assertEqual(self.explain(self.se).status_code, 403)
        self.assertIsNone(PhaseRuleOverride.objects.get(pk=self.row.pk).reason)

    def test_outsider_gets_404(self):
        self.assertEqual(self.explain(self.outsider).status_code, 404)

    def test_blank_reason_changes_nothing(self):
        self.explain(self.pm, reason='  ')
        self.assertIsNone(PhaseRuleOverride.objects.get(pk=self.row.pk).explained_at)

    def test_explained_row_is_immutable(self):
        self.explain(self.pm, reason='First reason.')
        self.explain(self.coord, reason='Second reason.')
        row = PhaseRuleOverride.objects.get(pk=self.row.pk)
        self.assertEqual(row.reason, 'First reason.')
        self.assertEqual(row.explained_by, self.pm)
        with self.assertRaises(AppendOnlyViolation):
            row.reason = 'Edited'
            row.save()
        with self.assertRaises(AppendOnlyViolation):
            row.delete()

    def test_two_concurrent_explains_leave_exactly_one_explanation(self):
        """Both managers loaded the row while it was awaiting; both submit. The
        conditional update matches the row once."""
        first = PhaseRuleOverride.objects.get(pk=self.row.pk)
        second = PhaseRuleOverride.objects.get(pk=self.row.pk)
        self.assertEqual(explain_phase_rule_override(first, 'From the PM.', self.pm), EXPLAINED)
        self.assertEqual(explain_phase_rule_override(second, 'From the coordinator.', self.coord),
                         ALREADY_EXPLAINED)
        row = PhaseRuleOverride.objects.get(pk=self.row.pk)
        self.assertEqual((row.reason, row.explained_by), ('From the PM.', self.pm))
        self.assertEqual(ActivityLog.objects.filter(
            action_code='phase_rule_override_explained', entity_id=row.pk).count(), 1)

    def test_database_refuses_a_half_explanation(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            PhaseRuleOverride.objects.create(
                project=self.site, task=self.task('LA and Earthing Installation'),
                rule_code='R3', rule_set='OPEX v1', rule_text='x', started_by=self.se,
                reason='Half', explained_by=None, explained_at=None)

    def test_database_refuses_a_task_row_for_r1(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            PhaseRuleOverride.objects.create(
                project=self.site, task=self.task('LA and Earthing Installation'),
                rule_code='R1', rule_set='OPEX v1', rule_text='x', started_by=self.se)

    def test_overview_card_lists_the_row(self):
        page = _client(self.se).get(reverse('project_overview', args=[self.site.project_id]))
        self.assertContains(page, 'id="started-early"')
        self.assertContains(page, 'Awaiting PM reason')
        self.assertNotContains(page, 'name="reason"', msg_prefix='no form for an SE')
        page = _client(self.pm).get(reverse('project_overview', args=[self.site.project_id]))
        self.assertContains(page, 'name="reason"')
        self.assertContains(page, 'Started early')

    def test_task_page_lists_the_override(self):
        page = _client(self.pm).get(
            reverse('task_detail', args=[self.site.project_id, self.target.pk]))
        self.assertContains(page, 'No material received at site yet')
        self.assertContains(page, 'Awaiting PM reason')


class NotificationTests(PhaseRuleFixture):
    """CHANGED at go-ahead: notify the site's PM side only when the awaiting count goes
    from 0 to 1; never the person who started the work (Q6)."""

    def test_first_awaiting_row_notifies_the_pm_side_once(self):
        self.start(self.task('Module Installation'), self.se)
        self.assertEqual(self.notices(self.pm).count(), 1)
        self.assertEqual(self.notices(self.coord).count(), 1)
        self.assertEqual(self.notices(self.se).count(), 0)
        notice = self.notices(self.pm).get()
        self.assertIn(self.site.project_id, notice.message)

    def test_rows_while_one_is_awaiting_send_nothing(self):
        self.start(self.task('Module Installation'), self.se)
        self.start(self.task('LA and Earthing Installation'), self.se)
        self.create_dc()
        self.assertEqual(self.rows().filter(explained_at__isnull=True).count(), 3)
        self.assertEqual(self.notices(self.pm).count(), 1)

    def test_after_everything_is_explained_the_next_row_notifies_again(self):
        self.start(self.task('Module Installation'), self.se)
        explain_phase_rule_override(self.rows().get(), 'Reason.', self.pm)
        self.start(self.task('LA and Earthing Installation'), self.se)
        self.assertEqual(self.notices(self.pm).count(), 2)

    def test_the_manager_who_started_it_is_not_notified(self):
        self.start(self.task('Net Metering Approval'), self.pm)
        self.assertEqual(self.notices(self.pm).count(), 0)
        self.assertEqual(self.notices(self.coord).count(), 1)

    def test_notice_is_in_app_only(self):
        from .models import NotificationLog
        self.start(self.task('Module Installation'), self.se)
        channels = set(NotificationLog.objects.filter(
            template_name='phase_rule_override_awaiting').values_list('channel', flat=True))
        self.assertEqual(channels, {'in_app'})


class ScopeTests(PhaseRuleFixture):
    """Mirrors, Residential, hand-added tasks and design un-release."""

    def test_mirrors_never_write_a_row(self):
        # A challan moves the delivery mirrors Not Started -> In Progress through their
        # own writer; only the challan's R1 row may appear.
        self.create_dc()
        moved = Task.objects.filter(phase__project=self.site, is_mirror=True,
                                    status=Task.IN_PROGRESS)
        self.assertTrue(moved.exists(), 'fixture: the challan moved a delivery mirror')
        self.assertFalse(PhaseRuleOverride.objects.filter(task__is_mirror=True).exists())
        state = SiteRuleState(self.site)
        for mirror in Task.objects.filter(phase__project=self.site, is_mirror=True):
            self.assertEqual(phase_rule_warnings(mirror, state=state), [])
            self.assertEqual(start_warnings(mirror, Task.IN_PROGRESS, self.site), [])

    def test_hand_added_task_follows_its_phase(self):
        phase = self.task('Module Installation').phase
        added = Task.objects.create(phase=phase, task_name='Extra earthing pit',
                                    task_order=99, assigned_role='Site Engineer',
                                    assigned_to=self.se)
        self.start(added, self.se)
        self.assertEqual(self.rows(added).get().rule_code, 'R3')

    def test_hand_added_task_gets_no_task_rule(self):
        phase = self.task('Net Metering Approval').phase
        added = Task.objects.create(phase=phase, task_name='Extra DISCOM letter',
                                    task_order=99, assigned_role='PM', assigned_to=self.pm)
        self.start(added, self.pm)
        self.assertEqual(Task.objects.get(pk=added.pk).status, Task.IN_PROGRESS)
        self.assertFalse(self.rows(added).exists())

    def test_hand_added_task_in_a_phase_with_no_template_task_gets_no_rule(self):
        phase = ProjectPhase.objects.create(project=self.site, phase_name='Extra works',
                                            phase_order=99)
        added = Task.objects.create(phase=phase, task_name='Fence', task_order=1,
                                    assigned_role='Site Engineer', assigned_to=self.se)
        self.start(added, self.se)
        self.assertFalse(self.rows(added).exists())

    def test_unrelease_then_new_start_writes_a_row(self):
        self.release_design()
        net = self.task('Net Metering Approval')
        self.start(net, self.pm)
        self.assertFalse(self.rows(net).exists())
        self.release_design(DESIGN_IN_DESIGN)    # a change request reopened design
        ceig = self.task('CEIG Approval')
        self.start(ceig, self.pm)
        self.assertEqual(self.rows(ceig).get().rule_code, 'R2')
        self.assertFalse(self.rows(net).exists(), 'started work is left alone')

    def test_residential_never_writes_a_row(self):
        res = Project.objects.create(
            customer_name='Res Site', customer_phone='9876500000', site_address='2 Road',
            city='Lucknow', project_type='Residential', status='Active', assigned_pm=self.pm)
        phase = ProjectPhase.objects.create(project=res, phase_name='Installation', phase_order=1)
        task = Task.objects.create(phase=phase, task_name='Module Installation', task_order=1,
                                   assigned_role='PM', assigned_to=self.pm)
        with self.assertNumQueries(0):
            self.assertEqual(start_warnings(task, Task.IN_PROGRESS, res), [])
        with self.captureOnCommitCallbacks(execute=True):
            _client(self.pm).post(reverse('task_status_update', args=[res.project_id, task.pk]),
                                  {'status': Task.IN_PROGRESS,
                                   'due_date': (date.today() + timedelta(days=3)).isoformat()})
        self.assertEqual(Task.objects.get(pk=task.pk).status, Task.IN_PROGRESS)
        self.assertFalse(PhaseRuleOverride.objects.filter(project=res).exists())


class InlineWarningTests(PhaseRuleFixture):
    """Ruling Q9: the chip plus the rule wording in the redrawn row and status block;
    the chip alone after a reload."""

    def test_row_response_carries_chip_and_wording(self):
        task = self.task('Module Installation')
        response = self.start(task, self.se, hx=True)
        self.assertContains(response, 'Started early')
        self.assertContains(response, 'phase-rule-wording')
        self.assertContains(response, 'No material received at site yet')

    def test_status_block_response_carries_chip_and_wording(self):
        task = self.task('Module Installation')
        response = self.start(task, self.se, hx=True, view='task_detail_status_update')
        self.assertContains(response, 'Started early')
        self.assertContains(response, 'phase-rule-wording')

    def test_after_reload_the_row_shows_the_chip_only(self):
        task = self.task('Module Installation')
        self.start(task, self.se)
        page = _client(self.se).get(reverse('project_overview', args=[self.site.project_id]))
        self.assertContains(page, 'started-early-chip')
        self.assertNotContains(page, 'class="small text-warning-emphasis mt-1 phase-rule-wording"')


class QueryCountTests(PhaseRuleFixture):
    """The evaluator's query count does not grow with the task count."""

    def _count_for_all_tasks(self):
        tasks = list(Task.objects.filter(phase__project=self.site))
        state = SiteRuleState(self.site)
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        with CaptureQueriesContext(connection) as ctx:
            for task in tasks:
                phase_rule_warnings(task, state=state)
        return len(ctx.captured_queries)

    def test_fixed_whatever_the_task_count(self):
        before = self._count_for_all_tasks()
        phase = self.task('Module Installation').phase
        for n in range(6):
            Task.objects.create(phase=phase, task_name=f'Extra {n}', task_order=50 + n,
                                assigned_role='Site Engineer')
        self.assertEqual(self._count_for_all_tasks(), before)
        self.assertLessEqual(before, 3)

    def test_row_flags_fixed_for_a_manager(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        from .views import _attach_phase_rule_flags
        request = RequestFactory().get('/')
        request.user = self.coord.user   # coordinator: the permission check is a query

        def count():
            # A fresh Project each time, so the permission check's assigned_pm load is
            # paid on every call and not only the first.
            site = Project.objects.get(pk=self.site.pk)
            tasks = list(Task.objects.filter(phase__project=site))
            with CaptureQueriesContext(connection) as ctx:
                _attach_phase_rule_flags(tasks, site, request)
            return len(ctx.captured_queries)

        before = count()
        phase = self.task('Module Installation').phase
        for n in range(6):
            Task.objects.create(phase=phase, task_name=f'Extra {n}', task_order=50 + n,
                                assigned_role='Site Engineer')
        self.assertEqual(count(), before)
        # chips 1 + permission 2 (assigned_pm, coordinators) + site facts 3
        self.assertLessEqual(before, 6)


class FollowUpTests(PhaseRuleFixture):
    """Follow-up of 20 Oct 2026: R1 before activation, and entirely-N/A predecessors."""

    def draft_site(self, project_type='OPEX', pm=None):
        return Project.objects.create(
            customer_name='Unactivated Site', customer_phone='9876511111',
            site_address='3 Draft Road', city='Lucknow', project_type=project_type,
            dc_capacity_kw=Decimal('50.00'), status='Draft', assigned_pm=pm)

    def create_dc_on(self, site, number):
        data = {'dc_number': number, 'dc_date': date.today().isoformat(),
                'line_item_category_0': DCLineItem.INVERTER,
                'line_item_description_0': 'String inverter', 'line_item_qty_0': '1',
                'line_item_unit_0': 'Nos'}
        with self.captureOnCommitCallbacks(execute=True):
            response = _client(self.scm).post(
                reverse('create_delivery_challan', args=[site.project_id]), data)
        self.assertEqual(response.status_code, 302)
        return DeliveryChallan.objects.get(project=site, dc_number=number)

    def test_r1_on_a_non_activated_opex_site_writes_a_row(self):
        site = self.draft_site()
        self.assertFalse(Task.objects.filter(phase__project=site).exists(),
                         'fixture: the site has no tasks')
        dc = self.create_dc_on(site, 'DC-DRAFT-1')
        row = PhaseRuleOverride.objects.get(delivery_challan=dc)
        self.assertEqual((row.rule_code, row.rule_set), ('R1', 'OPEX v1'),
                         "the active OPEX template's rule set")
        self.assertIsNone(row.explained_at)

    def test_r1_on_a_site_with_no_pm_is_on_the_card_and_notifies_nobody(self):
        site = self.draft_site(pm=None)
        before = Notification.objects.filter(link__endswith=AWAITING_LINK).count()
        self.create_dc_on(site, 'DC-NOPM-1')
        self.assertEqual(Notification.objects.filter(link__endswith=AWAITING_LINK).count(), before)
        page = _client(self.scm).get(reverse('project_overview', args=[site.project_id]))
        self.assertContains(page, 'id="started-early"')
        self.assertContains(page, 'Delivery challan DC-NOPM-1')
        self.assertContains(page, 'Awaiting PM reason')
        self.assertNotContains(page, 'name="reason"', msg_prefix='SCM is not the PM side')

    def test_r1_on_a_non_activated_site_silent_once_design_released(self):
        site = self.draft_site()
        DesignAssignment.objects.create(project=site, status=DESIGN_RELEASED)
        self.create_dc_on(site, 'DC-DRAFT-2')
        self.assertFalse(PhaseRuleOverride.objects.filter(project=site).exists())

    def test_r1_on_a_residential_site_writes_nothing(self):
        site = self.draft_site(project_type='Residential')
        self.create_dc_on(site, 'DC-RES-1')
        self.assertFalse(PhaseRuleOverride.objects.filter(project=site).exists())

    def test_r4_with_every_installation_task_na_writes_nothing(self):
        Task.objects.filter(phase__project=self.site,
                            template_task__phase__code='INSTALLATION').update(is_not_applicable=True)
        task = self.task('Testing & Commissioning')
        self.start(task, self.se)
        self.assertEqual(Task.objects.get(pk=task.pk).status, Task.IN_PROGRESS)
        self.assertFalse(self.rows(task).exists())

    def test_r4_partial_na_still_warns(self):
        installation = Task.objects.filter(phase__project=self.site,
                                           template_task__phase__code='INSTALLATION')
        keep = installation.get(task_name='Module Installation')
        installation.exclude(pk=keep.pk).update(is_not_applicable=True)
        task = self.task('Testing & Commissioning')
        self.start(task, self.se)
        self.assertEqual(self.rows(task).get().rule_code, 'R4')

    def test_r6_with_post_installation_approvals_na_writes_nothing(self):
        Task.objects.filter(pk=self.task('Post-Installation Approvals').pk).update(
            is_not_applicable=True)
        task = self.task('Completion Certificates (Paperwork)')
        self.start(task, self.pm)
        self.assertEqual(Task.objects.get(pk=task.pk).status, Task.IN_PROGRESS)
        self.assertFalse(self.rows(task).exists())

    def test_r6_partial_na_still_warns(self):
        """Two Post-Installation Approvals copies (per location): one N/A, one not
        started. Only the applicable one counts, and it has not started."""
        pia = self.task('Post-Installation Approvals')
        Task.objects.create(phase=pia.phase, task_name='Post-Installation Approvals — Block B',
                            location_label='Block B', template_task=pia.template_task,
                            task_order=pia.task_order + 1, assigned_role='PM', assigned_to=self.pm)
        Task.objects.filter(pk=pia.pk).update(is_not_applicable=True)
        task = self.task('Completion Certificates (Paperwork)')
        self.start(task, self.pm)
        self.assertEqual(self.rows(task).get().rule_code, 'R6')

    def test_na_task_with_a_started_status_still_does_not_count_when_another_applies(self):
        pia = self.task('Post-Installation Approvals')
        Task.objects.create(phase=pia.phase, task_name='Post-Installation Approvals — Block B',
                            location_label='Block B', template_task=pia.template_task,
                            task_order=pia.task_order + 1, assigned_role='PM', assigned_to=self.pm)
        Task.objects.filter(pk=pia.pk).update(is_not_applicable=True, status=Task.IN_PROGRESS)
        task = self.task('Completion Certificates (Paperwork)')
        self.start(task, self.pm)
        self.assertEqual(self.rows(task).get().rule_code, 'R6')

    def test_taskless_site_pays_one_query_for_the_rule_set(self):
        site = self.draft_site()
        from .phase_rules import dc_creation_warnings
        # tasks, active template, design
        with self.assertNumQueries(3):
            self.assertEqual([r.code for r in dc_creation_warnings(site)], ['R1'])
