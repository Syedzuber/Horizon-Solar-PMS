"""
log_activity() is safe to call inside any transaction.

WHY THIS EXISTS
---------------
log_activity() swallows its own exception so a failed log line never aborts the work
that called it. That was not enough: Model.save() flags the caller's atomic block for
rollback before the exception is swallowed (transaction.mark_for_rollback_on_error), so
the block's exit silently rolled back the caller's real work while the view reported
success. SCHED-1 proved this on task_add (tests_sched1_due_date_source). log_activity
now writes its row in its own savepoint and cuts every string to its column first.

THE FAILURE IS REAL, NOT MOCKED. A BEFORE INSERT trigger makes the database refuse the
row (_refuse_log_inserts, shared with the SCHED-1 tests), so on Postgres the
transaction genuinely enters the aborted state the savepoint has to contain.

The call site used is project_field_edit: one atomic block holding the field save, the
ProjectFieldEditLog rows and the log_activity line, the last statement in the block.

Run with:
    python manage.py test projects.tests_log_activity_safety --settings=solarpms.test_settings
and on Postgres:
    python manage.py test projects.tests_log_activity_safety --settings=solarpms.test_settings_postgres
"""
from decimal import Decimal

from django.db import transaction
from django.urls import reverse

from . import tests_residential_baseline as rb
from .models import ActivityLog, Project, ProjectFieldEditLog, log_activity
from .tests_sched1_due_date_source import _refuse_log_inserts


class LogActivityInsideATransactionTests(rb.ResidentialBaselineBase):

    def _edit_capacity(self, value):
        return rb._client_for(self.pm_a).post(
            reverse('project_field_edit', args=[self.project_a.project_id]),
            {'dc_capacity_kw': value,
             'target_commissioning_date': self.project_a.target_commissioning_date.isoformat(),
             'reason': 'resurvey'})

    def test_a_refused_log_insert_keeps_the_views_real_work(self):
        """project_field_edit's block saves the field, writes the edit-log row, then calls
        log_activity() last. With that INSERT refused, the field change and its
        ProjectFieldEditLog row must survive; only the activity line is lost."""
        _refuse_log_inserts(self, 'project_fields_edited')
        with self.assertLogs('projects.models', level='ERROR'):
            response = self._edit_capacity('7.50')
        self.assertEqual(response.status_code, 302)
        self.project_a.refresh_from_db()
        self.assertEqual(self.project_a.dc_capacity_kw, Decimal('7.50'))
        self.assertTrue(ProjectFieldEditLog.objects.filter(
            project=self.project_a, field_name='dc_capacity_kw',
            new_value='7.50').exists())
        self.assertFalse(ActivityLog.objects.filter(
            project=self.project_a, action__startswith='Edited project fields').exists())

    def test_without_a_refusal_the_line_is_written_as_before(self):
        self._edit_capacity('8.00')
        self.assertTrue(ActivityLog.objects.filter(
            project=self.project_a,
            action='Edited project fields: dc_capacity_kw').exists())

    def test_the_callers_transaction_stays_usable_after_a_refused_insert(self):
        """The same thing without a view: queries after the refused INSERT, inside the
        same block, run. On Postgres they would fail with 'current transaction is
        aborted' if the INSERT were not in its own savepoint."""
        _refuse_log_inserts(self, 'project_fields_edited')
        with self.assertLogs('projects.models', level='ERROR'):
            with transaction.atomic():
                Project.objects.filter(pk=self.project_a.pk).update(site_name='Kept')
                log_activity(self.project_a, self.pm_a, 'Edited project fields: x')
                self.assertEqual(Project.objects.get(pk=self.project_a.pk).site_name, 'Kept')
        self.assertEqual(Project.objects.get(pk=self.project_a.pk).site_name, 'Kept')


class LogActivityTruncationTests(rb.ResidentialBaselineBase):
    """Every string is cut to its column before saving. Asserted on stored lengths, so
    it proves the cut on SQLite too, which would otherwise store the over-long text."""

    def test_every_string_field_is_cut_to_its_column(self):
        log_activity(self.project_a, self.pm_a, 'A' * 400,
                     entity_type='E' * 80, entity_id=900001, action_code='C' * 80)
        row = ActivityLog.objects.get(project=self.project_a, entity_id=900001)
        field = ActivityLog._meta.get_field
        self.assertEqual(len(row.action), field('action').max_length)
        self.assertTrue(row.action.endswith('…'))
        self.assertEqual(row.entity_type, 'E' * field('entity_type').max_length)
        self.assertEqual(row.action_code, 'C' * field('action_code').max_length)

    def test_a_string_that_fits_is_stored_unchanged(self):
        log_activity(self.project_a, self.pm_a, 'Short line', entity_type='Project',
                     entity_id=900002, action_code='short_code')
        row = ActivityLog.objects.get(project=self.project_a, entity_id=900002)
        self.assertEqual((row.action, row.entity_type, row.action_code),
                         ('Short line', 'Project', 'short_code'))
