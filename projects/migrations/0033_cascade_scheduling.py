from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('projects', '0032_systemsettings_new_flags'),
    ]

    operations = [
        migrations.AddField(
            model_name='systemsettings',
            name='cascade_scheduling_enabled',
            field=models.BooleanField(
                default=False,
                help_text='When True, PMs can enable cascading date scheduling per project.',
            ),
        ),
        migrations.AddField(
            model_name='project',
            name='cascade_scheduling',
            field=models.BooleanField(
                default=False,
                help_text='When True, task due dates chain automatically. Cannot be reverted once enabled.',
            ),
        ),
    ]
