# Generated for Audit Log Coverage — adds ActivityLog.action_code

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('projects', '0042_projectfieldeditlog'),
    ]

    operations = [
        migrations.AddField(
            model_name='activitylog',
            name='action_code',
            field=models.CharField(blank=True, db_index=True, default='', max_length=50),
        ),
    ]
