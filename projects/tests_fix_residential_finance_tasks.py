"""FIN-M-B9b (R13) — fix_residential_finance_tasks corrects the Finance confirmation tasks
on older Residential projects.

WHY THIS FILE EXISTS
--------------------
Residential projects created before 21 Jul 2026 carry a legacy 'Finance Confirmation'
task holding the M2 payment flag, while their 'Pre Dispatch Payment Confirmation' (the
task the name map syncs to M2) has none. Completing M2's task recorded money and alerted
nobody. Rulings (Zuber, 12 Nov 2026): on non-deleted Residential projects only, move the
flag to 'Pre Dispatch', clear it on 'Finance Confirmation', mark an open 'Finance
Confirmation' Not Applicable, log every change, change no task status or milestone.

Run with:
    python manage.py test projects.tests_fix_residential_finance_tasks --settings=solarpms.test_settings
"""
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.contrib.auth.models import User
from django.core.management import CommandError, call_command
from django.test import Client, TestCase
from django.urls import reverse

from .models import ActivityLog, PaymentMilestone, Project, ProjectPhase, Task

COMMAND = 'fix_residential_finance_tasks'
NA_REASON = ('Replaced by Pre Dispatch Payment Confirmation when the Residential '
             'template changed on 21 Jul 2026.')
CODES = ('task_payment_flag_corrected', 'task_marked_not_applicable')


def _profile(username, role):
    """A post_save signal creates the UserProfile; fetch and set, never create."""
    user = User.objects.create_user(username=username, password='x',
                                    first_name=username.title(), last_name='Test')
    profile = user.profile
    profile.role = role
    profile.is_active = True
    profile.save()
    return profile


class FixFinanceTasksBase(TestCase):

    def setUp(self):
        self.pm = _profile('ff_pm', 'PM')
        self.finance = _profile('ff_fin', 'Finance')
        self.admin = _profile('ff_admin', 'Admin')
        self._n = 0

    def make_project(self, legacy_status=Task.NOT_STARTED, project_type='Residential',
                     is_deleted=False, legacy=True, legacy_count=1, current=False):
        """A project shaped like the pre-21-Jul template (legacy=True): Advance, Finance
        Confirmation (flagged), Pre Dispatch (unflagged), 100%. current=True gives the
        present template's flags and no legacy task."""
        self._n += 1
        project = Project.objects.create(
            customer_name=f'Fix {self._n}', status='Active', project_type=project_type,
            customer_phone='9876543210', site_address='1 Sun Road', city='Lucknow',
            state='Uttar Pradesh', dc_capacity_kw=Decimal('5.00'), assigned_pm=self.pm,
            cascade_scheduling=False, is_deleted=is_deleted)
        sales = ProjectPhase.objects.create(project=project, phase_name='Sales',
                                            phase_order=1)
        proc = ProjectPhase.objects.create(project=project, phase_name='Procurement',
                                           phase_order=5)
        closure = ProjectPhase.objects.create(project=project, phase_name='Finance Closure',
                                              phase_order=9)
        common = dict(assigned_role=Task.FINANCE, assigned_to=self.finance)
        Task.objects.create(phase=sales, task_name='Advance Payment Confirmation',
                            task_order=2, is_payment_milestone=True, **common)
        if legacy:
            for i in range(legacy_count):
                Task.objects.create(phase=proc, task_name='Finance Confirmation',
                                    task_order=6 + i * 10, status=legacy_status,
                                    is_payment_milestone=True, **common)
        Task.objects.create(phase=proc, task_name='Pre Dispatch Payment Confirmation',
                            task_order=7, is_payment_milestone=current, **common)
        Task.objects.create(phase=closure, task_name='100% Payment Confirmation',
                            task_order=1, is_payment_milestone=True, **common)
        for label in ('M1', 'M2', 'M3'):
            PaymentMilestone.objects.create(project=project, milestone_name=label,
                                            milestone_description=label,
                                            amount=Decimal('100000.00'), created_by=self.pm)
        return project

    def task(self, project, name):
        return Task.objects.get(phase__project=project, task_name=name)

    def run_command(self, *args):
        out = StringIO()
        call_command(COMMAND, *args, stdout=out)
        return out.getvalue()

    def apply(self):
        return self.run_command('--apply', '--actor', self.admin.user.username)

    def snapshot(self):
        """Everything the command must NOT change: every task's status and completion,
        names, and every milestone's values."""
        tasks = sorted(Task.objects.values_list('pk', 'task_name', 'status', 'completed_at'))
        milestones = sorted(PaymentMilestone.objects.values_list(
            'pk', 'status', 'amount', 'amount_received', 'received_date', 'invoice_date'))
        return tasks, milestones

    def lines(self):
        return list(ActivityLog.objects.filter(action_code__in=CODES)
                    .order_by('pk').values_list('action_code', 'action', 'actor_id',
                                                'entity_id'))


class DryRunTests(FixFinanceTasksBase):

    def test_dry_run_writes_nothing_and_prints_the_plan(self):
        project = self.make_project()
        before = (self.snapshot(),
                  list(Task.objects.values_list('pk', 'is_payment_milestone',
                                                'is_not_applicable')))
        out = self.run_command()

        after = (self.snapshot(),
                 list(Task.objects.values_list('pk', 'is_payment_milestone',
                                               'is_not_applicable')))
        self.assertEqual(before, after)
        self.assertEqual(self.lines(), [])
        pid = project.project_id
        self.assertIn('MODE: DRY RUN', out)
        self.assertIn(f'{pid} | Pre Dispatch Payment Confirmation | payment flag: off -> on', out)
        self.assertIn(f'{pid} | Finance Confirmation | payment flag: on -> off', out)
        self.assertIn(f'{pid} | Finance Confirmation | not applicable: no -> yes '
                      f'(status stays Not Started)', out)
        self.assertIn('WOULD CHANGE: 1 projects, 2 flag changes, 1 Not Applicable marks. '
                      'Already correct: 0 projects. Outliers: 0. Projects checked: 1.', out)
        self.assertIn('Dry run: nothing written.', out)


class ApplyTests(FixFinanceTasksBase):

    def test_apply_makes_exactly_the_approved_changes_on_an_open_legacy_task(self):
        project = self.make_project()
        before = self.snapshot()
        out = self.apply()

        m2 = self.task(project, 'Pre Dispatch Payment Confirmation')
        legacy = self.task(project, 'Finance Confirmation')
        self.assertTrue(m2.is_payment_milestone)
        self.assertFalse(legacy.is_payment_milestone)
        self.assertTrue(legacy.is_not_applicable)
        self.assertIn(f'] Marked not applicable: {NA_REASON}', legacy.not_applicable_reason)
        self.assertEqual(legacy.not_applicable_marked_by, self.admin)
        self.assertIsNotNone(legacy.not_applicable_marked_at)
        # The other two Finance tasks are untouched.
        self.assertTrue(self.task(project, 'Advance Payment Confirmation').is_payment_milestone)
        self.assertTrue(self.task(project, '100% Payment Confirmation').is_payment_milestone)
        self.assertFalse(m2.is_not_applicable)
        # No status, name or milestone changed.
        self.assertEqual(self.snapshot(), before)
        # One line per change, under the actor.
        self.assertEqual(self.lines(), [
            ('task_payment_flag_corrected',
             "Payment-milestone flag on 'Pre Dispatch Payment Confirmation': off → on "
             "(FIN-M-B9b)", self.admin.pk, m2.pk),
            ('task_payment_flag_corrected',
             "Payment-milestone flag on 'Finance Confirmation': on → off (FIN-M-B9b)",
             self.admin.pk, legacy.pk),
            ('task_marked_not_applicable',
             f'Marked task Not Applicable: Finance Confirmation — {NA_REASON}',
             self.admin.pk, legacy.pk),
        ])
        self.assertIn('CHANGED: 1 projects, 2 flag changes, 1 Not Applicable marks.', out)
        self.assertIn('ActivityLog rows written: 3 (expected 3).', out)

    def test_a_done_legacy_task_keeps_its_status_and_is_not_marked_not_applicable(self):
        project = self.make_project(legacy_status=Task.DONE)
        self.apply()
        legacy = self.task(project, 'Finance Confirmation')
        self.assertEqual(legacy.status, Task.DONE)
        self.assertFalse(legacy.is_payment_milestone)
        self.assertFalse(legacy.is_not_applicable)
        self.assertEqual([c for c, *_ in self.lines()],
                         ['task_payment_flag_corrected', 'task_payment_flag_corrected'])

    def test_an_earlier_not_applicable_history_is_appended_to(self):
        project = self.make_project()
        Task.objects.filter(phase__project=project, task_name='Finance Confirmation').update(
            not_applicable_reason='[01 Aug 2026 10:00] Un-marked, now applicable: test')
        self.apply()
        reason = self.task(project, 'Finance Confirmation').not_applicable_reason
        self.assertTrue(reason.startswith('[01 Aug 2026 10:00] Un-marked, now applicable: test\n['))
        self.assertTrue(reason.endswith(NA_REASON))

    def test_second_apply_changes_nothing(self):
        self.make_project()
        self.make_project(legacy_status=Task.DONE)
        self.apply()
        state = (self.snapshot(),
                 sorted(Task.objects.values_list('pk', 'is_payment_milestone',
                                                 'is_not_applicable', 'not_applicable_reason')))
        lines = self.lines()

        out = self.apply()

        self.assertEqual((self.snapshot(),
                          sorted(Task.objects.values_list('pk', 'is_payment_milestone',
                                                          'is_not_applicable',
                                                          'not_applicable_reason'))), state)
        self.assertEqual(self.lines(), lines)
        self.assertIn('CHANGED: 0 projects, 0 flag changes, 0 Not Applicable marks. '
                      'Already correct: 2 projects.', out)
        self.assertIn('ActivityLog rows written: 0 (expected 0).', out)

    def test_apply_requires_an_active_actor(self):
        self.make_project()
        with self.assertRaisesMessage(CommandError, '--apply requires --actor'):
            self.run_command('--apply')
        with self.assertRaisesMessage(CommandError, 'no such user'):
            self.run_command('--apply', '--actor', 'nobody')
        self.assertEqual(self.lines(), [])
        self.assertFalse(Task.objects.filter(task_name='Pre Dispatch Payment Confirmation',
                                             is_payment_milestone=True).exists())


class ScopeTests(FixFinanceTasksBase):

    def flags(self, project):
        return sorted(Task.objects.filter(phase__project=project).values_list(
            'task_name', 'is_payment_milestone', 'is_not_applicable'))

    def test_deleted_current_opex_and_capex_projects_are_untouched(self):
        deleted = self.make_project(is_deleted=True)
        current = self.make_project(legacy=False, current=True)
        opex = self.make_project(project_type='OPEX')
        capex = self.make_project(project_type='CAPEX')
        untouched = {p.pk: self.flags(p) for p in (deleted, current, opex, capex)}

        out = self.apply()

        for p in (deleted, current, opex, capex):
            self.assertEqual(self.flags(p), untouched[p.pk])
        self.assertEqual(self.lines(), [])
        self.assertIn('Projects checked: 0.', out)

    def test_an_outlier_is_listed_and_not_touched(self):
        outlier = self.make_project(legacy_count=2)
        fixed = self.make_project()
        before = self.flags(outlier)

        out = self.apply()

        self.assertEqual(self.flags(outlier), before)
        self.assertIn(f"OUTLIER, not touched: {outlier.project_id}: 2 x 'Finance "
                      f"Confirmation', 1 x 'Pre Dispatch Payment Confirmation'", out)
        self.assertTrue(self.task(fixed, 'Pre Dispatch Payment Confirmation').is_payment_milestone)
        self.assertIn('Outliers: 1. Projects checked: 2.', out)


class AlertPathAfterFixTests(FixFinanceTasksBase):
    """The point of the fix: completing M2's task alerts and records M2, through the
    FIN-M-B9a chokepoint."""

    def complete_m2_task(self, project):
        task = self.task(project, 'Pre Dispatch Payment Confirmation')
        client = Client(SERVER_NAME='localhost')
        client.force_login(self.finance.user)
        client.post(reverse('task_status_update', args=[project.project_id, task.pk]),
                    {'status': Task.DONE})

    @mock.patch('projects.views.notify')
    def test_before_the_fix_m2_records_money_but_alerts_nobody(self, notify_mock):
        project = self.make_project()
        self.complete_m2_task(project)
        self.assertEqual(project.milestones.get(milestone_name='M2').status, 'Received')
        self.assertEqual(notify_mock.call_count, 0)

    @mock.patch('projects.views.notify')
    def test_after_the_fix_m2_alerts_and_records(self, notify_mock):
        project = self.make_project()
        self.apply()
        self.complete_m2_task(project)
        self.assertEqual(project.milestones.get(milestone_name='M2').status, 'Received')
        self.assertEqual([c.args[0] for c in notify_mock.call_args_list],
                         ['payment_milestone_reached'])
