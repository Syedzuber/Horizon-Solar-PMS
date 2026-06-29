from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


RESIDENTIAL_DEFAULTS = [
    # (phase_name, task_name, task_type, duration_days)
    ('Sales & Documentation',     'OCR, Documentation & Verification',    'Internal', 2),
    ('Sales & Documentation',     'Advance Payment Confirmation',          'Internal', 1),
    ('Detail Engineering Visit',  'DEV Schedule',                          'Internal', 1),
    ('Detail Engineering Visit',  'DEV Conduct',                           'Internal', 2),
    ('Detail Engineering Visit',  'DEV Data to Design',                    'Internal', 1),
    ('Detail Engineering Visit',  'DEV Inputs Validation',                 'Internal', 1),
    ('Design',                    'Design',                                 'Internal', 2),
    ('Design',                    'Array Layout',                          'Internal', 2),
    ('Design',                    'SLD',                                   'Internal', 2),
    ('Design',                    'Installation Drawings',                 'Internal', 1),
    ('Design',                    'BOQ Preparation',                       'Internal', 1),
    ('Design',                    'Design Approval by Internal Team',      'Internal', 1),
    ('Design',                    'Design Approval by Customer',           'External', 1),
    ('Pre-Installation Approvals', 'Pre Installation Approvals',           'Internal', 2),
    ('Pre-Installation Approvals', 'LC / PC / NC Required',                'External', 2),
    ('Pre-Installation Approvals', 'Vendor Registration',                  'External', 2),
    ('Pre-Installation Approvals', 'Document Preparation',                 'Internal', 2),
    ('Pre-Installation Approvals', 'Signing Document by Customer',         'External', 2),
    ('Pre-Installation Approvals', 'Net Metering Application Submission',  'Internal', 2),
    ('Pre-Installation Approvals', 'TFR Received',                         'External', 2),
    ('Procurement',               'Procurement Schedule',                  'Internal', 1),
    ('Procurement',               'PO Placed MMS',                         'Internal', 1),
    ('Procurement',               'PO Placed Module',                      'Internal', 1),
    ('Procurement',               'PO Placed Inverter',                    'Internal', 1),
    ('Procurement',               'PO for B & C Class Items',              'Internal', 1),
    ('Procurement',               'Finance Confirmation',                  'Internal', 1),
    ('Procurement',               'Pre Dispatch Payment Confirmation',     'Internal', 1),
    ('Delivery',                  'Delivery Schedule',                     'Internal', 1),
    ('Delivery',                  'Delivery of MMS',                       'Internal', 1),
    ('Delivery',                  'Delivery of B & C Class Items',         'Internal', 1),
    ('Delivery',                  'Delivery of Module',                    'Internal', 1),
    ('Delivery',                  'Delivery of Inverter',                  'Internal', 1),
    ('Installation',              'MMS Installation',                      'Internal', 1),
    ('Installation',              'Earthing Work',                         'Internal', 1),
    ('Installation',              'Module Installation',                   'Internal', 1),
    ('Installation',              'Inverter Installation',                 'Internal', 1),
    ('Installation',              'DC Wire Work',                          'Internal', 1),
    ('Installation',              'AC Cable Work',                         'Internal', 1),
    ('Installation',              'Connections and Voc Testing',           'Internal', 1),
    ('Installation',              'Pre Commissioning Check List',          'Internal', 0),
    ('Commissioning',             'Pre Commissioning Visit by DISCOM',     'External', 2),
    ('Commissioning',             'Meter Testing',                         'Internal', 1),
    ('Commissioning',             'SCO Release',                           'External', 2),
    ('Commissioning',             'Meter Installation by DISCOM',          'External', 2),
    ('Commissioning',             'RMS Configuration',                     'Internal', 1),
    ('Commissioning',             'Plant Commissioning',                   'Internal', 1),
    ('Commissioning',             'Commissioning Report Prepared',         'Internal', 1),
    ('Commissioning',             'Commissioning Report Approved',         'Internal', 0),
    ('Commissioning',             'Customer Handover',                     'Internal', 0),
    ('Finance Closure',           '100% Payment Confirmation',             'Internal', 2),
]


def seed_duration_template(apps, schema_editor):
    TaskDurationTemplate = apps.get_model('projects', 'TaskDurationTemplate')
    for phase, task, ttype, days in RESIDENTIAL_DEFAULTS:
        TaskDurationTemplate.objects.get_or_create(
            project_type='residential',
            task_name=task,
            defaults={
                'phase_name':    phase,
                'task_type':     ttype,
                'duration_days': days,
            },
        )


def unseed_duration_template(apps, schema_editor):
    TaskDurationTemplate = apps.get_model('projects', 'TaskDurationTemplate')
    TaskDurationTemplate.objects.filter(project_type='residential').delete()


class Migration(migrations.Migration):

    dependencies = [
        ('projects', '0033_cascade_scheduling'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='TaskDurationTemplate',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('project_type',  models.CharField(choices=[('residential', 'Residential')], default='residential', max_length=50)),
                ('phase_name',    models.CharField(max_length=100)),
                ('task_name',     models.CharField(max_length=200)),
                ('task_type',     models.CharField(max_length=20)),
                ('duration_days', models.PositiveIntegerField(default=1)),
                ('updated_at',    models.DateTimeField(auto_now=True)),
                ('updated_by',    models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='duration_template_edits',
                    to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={
                'ordering': ['project_type', 'phase_name', 'task_name'],
            },
        ),
        migrations.AlterUniqueTogether(
            name='taskdurationtemplate',
            unique_together={('project_type', 'task_name')},
        ),
        migrations.RunPython(seed_duration_template, reverse_code=unseed_duration_template),
    ]
