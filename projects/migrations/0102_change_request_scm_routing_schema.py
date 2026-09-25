"""Session B1 — SCM change-request routing: SCHEMA ONLY.

Nothing in the product writes a new verdict value, a PM-stage field, a withdrawal or a
correction after this migration. B2 builds the routing that does. The one new column
written today is `origin`, by the raise view, at creation.

`origin` IS ADDED WITH A ONE-OFF DEFAULT OF 'pm' AND preserve_default=False, so the model
carries no default afterwards. A CharField with no default stores '' when a caller forgets
it rather than raising, so `cr_origin_valid` is what makes forgetting it an IntegrityError.

THE BACKFILL RUNS LAST, after every AddConstraint. No constraint here depends on it: every
existing row reads 'pm' from the one-off default and holds a pre-B1 verdict, so all six
constraints hold before it runs and after. Running it last also means no ALTER TABLE
follows the UPDATE, so PostgreSQL cannot refuse with "pending trigger events".

The backfill reads the raise view's own activity line. Since session 3.1c-i that line
begins 'Change request raised by SCM' exactly when user_can_manage_project() was false for
the raiser, which is the predicate that now sets `origin`. Any row whose line is missing
(log_activity() swallows its own failures) or older than 3.1c-i, when SCM could not raise,
stays 'pm'. Production held zero DesignChangeRequest rows on 25 Sep 2026, so there it is
a no-op; locally it sets pk 4 (ORDDEMOB03, raised by demo.scm) to 'scm'.

Reverse: the backfill does nothing, and the RemoveField reversals drop the column it
wrote. The opened_reason AlterField changes a choices label only, which lives in Python;
it emits no SQL and touches no row.
"""

import django.db.models.deletion
from django.db import migrations, models

SCM_RAISE_PREFIX = 'Change request raised by SCM'


def backfill_origin_from_raise_log(apps, schema_editor):
    ActivityLog = apps.get_model('projects', 'ActivityLog')
    DesignChangeRequest = apps.get_model('projects', 'DesignChangeRequest')
    scm_pks = set(ActivityLog.objects
                  .filter(action_code='design_change_requested',
                          entity_type='DesignChangeRequest',
                          action__startswith=SCM_RAISE_PREFIX,
                          entity_id__isnull=False)
                  .values_list('entity_id', flat=True))
    updated = (DesignChangeRequest.objects.filter(pk__in=scm_pks)
               .update(origin='scm'))
    total = DesignChangeRequest.objects.count()
    print(f'\n    B1: {updated} change request(s) set to origin=scm; '
          f'{total - updated} left at origin=pm.')


class Migration(migrations.Migration):

    dependencies = [
        ('projects', '0101_payment_request_drop_legacy_fields'),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='designchangerequest',
            name='uniq_pending_change_request_per_attempt',
        ),
        migrations.AddField(
            model_name='designchangerequest',
            name='boq_corrections',
            field=models.ManyToManyField(blank=True, related_name='+', to='projects.boqcorrection'),
        ),
        migrations.AddField(
            model_name='designchangerequest',
            name='corrected_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='designchangerequest',
            name='corrected_by',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='corrected_design_change_requests', to='projects.userprofile'),
        ),
        migrations.AddField(
            model_name='designchangerequest',
            name='correction_note',
            field=models.TextField(blank=True, default=''),
        ),
        migrations.AddField(
            model_name='designchangerequest',
            name='origin',
            field=models.CharField(choices=[('pm', 'PM'), ('scm', 'SCM')], default='pm', max_length=3),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='designchangerequest',
            name='pm_decided_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='designchangerequest',
            name='pm_decided_by',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='pm_triaged_design_change_requests', to='projects.userprofile'),
        ),
        migrations.AddField(
            model_name='designchangerequest',
            name='pm_note',
            field=models.TextField(blank=True, default=''),
        ),
        migrations.AddField(
            model_name='designchangerequest',
            name='withdrawal_note',
            field=models.TextField(blank=True, default=''),
        ),
        migrations.AddField(
            model_name='designchangerequest',
            name='withdrawn_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='designchangerequest',
            name='withdrawn_by',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='withdrawn_design_change_requests', to='projects.userprofile'),
        ),
        migrations.AlterField(
            model_name='designattempt',
            name='opened_reason',
            field=models.CharField(choices=[('initial', 'Initial'), ('qc_failed', 'QC failed'), ('pm_change_request', 'Change request'), ('pm_rejected', 'PM rejected')], default='initial', max_length=20),
        ),
        migrations.AlterField(
            model_name='designchangerequest',
            name='verdict',
            field=models.CharField(choices=[('pending', 'Pending'), ('accepted', 'Accepted'), ('rejected', 'Rejected'), ('with_pm', 'With the PM'), ('pm_rejected', 'Rejected by the PM'), ('withdrawn', 'Withdrawn'), ('corrected', 'Corrected in the BOQ')], default='pending', max_length=20),
        ),
        migrations.AddConstraint(
            model_name='designchangerequest',
            constraint=models.UniqueConstraint(condition=models.Q(('verdict__in', ['with_pm', 'pending'])), fields=('attempt',), name='uniq_pending_change_request_per_attempt'),
        ),
        migrations.AddConstraint(
            model_name='designchangerequest',
            constraint=models.CheckConstraint(condition=models.Q(('origin__in', ['pm', 'scm'])), name='cr_origin_valid'),
        ),
        migrations.AddConstraint(
            model_name='designchangerequest',
            constraint=models.CheckConstraint(condition=models.Q(('pm_decided_at__isnull', True), models.Q(('pm_note', ''), _negated=True), _connector='OR'), name='cr_pm_note_required_when_pm_decided'),
        ),
        migrations.AddConstraint(
            model_name='designchangerequest',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('verdict', 'withdrawn'), _negated=True), models.Q(('withdrawn_by__isnull', False), ('withdrawn_at__isnull', False), models.Q(('withdrawal_note', ''), _negated=True)), _connector='OR'), name='cr_withdrawal_fields_required_when_withdrawn'),
        ),
        migrations.AddConstraint(
            model_name='designchangerequest',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('verdict', 'corrected'), _negated=True), models.Q(('corrected_by__isnull', False), ('corrected_at__isnull', False), models.Q(('correction_note', ''), _negated=True)), _connector='OR'), name='cr_correction_fields_required_when_corrected'),
        ),
        migrations.AddConstraint(
            model_name='designchangerequest',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('origin', 'pm'), _negated=True), models.Q(('verdict__in', ['with_pm', 'pm_rejected', 'withdrawn']), _negated=True), _connector='OR'), name='cr_pm_origin_excludes_scm_stages'),
        ),
        # LAST — see the module docstring.
        migrations.RunPython(backfill_origin_from_raise_log, migrations.RunPython.noop),
    ]
