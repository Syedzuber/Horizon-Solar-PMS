# Generated for the Residential Gantt feature — adds admin-tunable Gantt settings.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('projects', '0043_activitylog_action_code'),
    ]

    operations = [
        migrations.AddField(
            model_name='systemsettings',
            name='gantt_client_buffer_days',
            field=models.PositiveIntegerField(
                default=3,
                help_text='Calendar days added to each phase end in the Client Gantt view, cascading downstream.',
            ),
        ),
        migrations.AddField(
            model_name='systemsettings',
            name='gantt_external_min_display_days',
            field=models.PositiveIntegerField(
                default=3,
                help_text="Minimum visual bar width (days) for external/third-party Gantt tasks so they don't render too thin.",
            ),
        ),
    ]
