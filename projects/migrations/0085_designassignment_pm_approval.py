"""Add the design PM-approval status (unreachable) and its two stamp fields.

NOTHING WRITES THE NEW STATUS OR EITHER FIELD. The transition is prompt 3.1b.
No RunPython, no RemoveField, no data touched: two nullable AddFields (every existing
row lands NULL) and an AlterField on `choices` only — no type, width or constraint
change, so on PostgreSQL the AlterField is a no-op at the SQL level.
"""
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('projects', '0084_split_site_capacity_add_site_fields'),
    ]

    operations = [
        migrations.AddField(
            model_name='designassignment',
            name='pm_approved_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='designassignment',
            name='pm_approved_by',
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name='pm_approved_design_assignments', to='projects.userprofile'),
        ),
        migrations.AlterField(
            model_name='designassignment',
            name='status',
            field=models.CharField(choices=[
                ('awaiting_survey', 'Awaiting survey'),
                ('awaiting_allocation', 'Awaiting allocation'),
                ('allocated', 'Allocated'),
                ('due_date_proposed', 'Due date proposed'),
                ('in_design', 'In design'),
                ('arka_submitted', 'Arka submitted'),
                ('awaiting_head_arka', 'Arka — awaiting Design Head'),
                ('arka_rejected', 'Arka rejected'),
                ('artifacts_uploaded', 'Artifacts uploaded'),
                ('in_qc', 'In QC'),
                ('awaiting_head_qc', 'QC passed — awaiting Design Head'),
                ('qc_failed', 'QC failed'),
                ('awaiting_pm_approval', 'Awaiting PM approval'),
                ('released', 'Released'),
                ('survey_returned', 'Design Hold — survey inadequate'),
            ], default='awaiting_survey', max_length=30),
        ),
    ]
