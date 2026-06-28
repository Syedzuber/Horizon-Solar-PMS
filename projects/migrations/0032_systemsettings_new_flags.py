from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('projects', '0031_design_submission'),
    ]

    operations = [
        migrations.AddField(
            model_name='systemsettings',
            name='in_app_notifications_enabled',
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name='systemsettings',
            name='maintenance_mode',
            field=models.BooleanField(default=False),
        ),
    ]
