# Session B - BOQCorrection: the per-row record of a reviewer's BOQ correction.
#
# ADDITIVE AND EMPTY ON ARRIVAL. One new table, no column added to and no column changed on
# BOQ, BOQItem, DesignAttempt or DesignAssignment - nothing existing is touched, so there
# is nothing to backfill and no site behaves differently the moment this applies. The first
# row appears when a reviewer makes their first correction.
#
# NO STATUS FIELD, DELIBERATELY. This BOQ already carries three unreconciled "is it
# finished" signals - BOQ.status, DesignAttempt.boq_submitted_at and the SiteGroup
# procurement lock (DESIGN_MODULE_DEFERRED J8). Every column below is a fact about an edit
# that already happened; none of them is a gate, and nothing in the product branches on
# this table. See models.BOQCorrection for the full reasoning.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('projects', '0077_checklist_task_link_template_task'),
    ]

    operations = [
        migrations.CreateModel(
            name='BOQCorrection',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('item_code', models.CharField(blank=True, default='', max_length=30)),
                ('item_description', models.TextField(blank=True, default='')),
                ('action', models.CharField(choices=[('line_added', 'Line added'), ('quantity_changed', 'Quantity changed')], max_length=20)),
                ('quantity_before', models.DecimalField(blank=True, decimal_places=2, max_digits=10, null=True)),
                ('quantity_after', models.DecimalField(blank=True, decimal_places=2, max_digits=10, null=True)),
                ('corrected_at', models.DateTimeField(auto_now_add=True)),
                ('boq', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='corrections', to='projects.boq')),
                ('corrected_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='boq_corrections', to='projects.userprofile')),
                ('item', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='corrections', to='projects.boqitem')),
            ],
            options={
                'ordering': ['-corrected_at', '-pk'],
            },
        ),
    ]
