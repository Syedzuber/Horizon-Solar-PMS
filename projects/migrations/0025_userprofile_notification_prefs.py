from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('projects', '0024_task_blocked_since'),
    ]

    operations = [
        migrations.AddField(
            model_name='userprofile',
            name='email_notifications',
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name='userprofile',
            name='whatsapp_notifications',
            field=models.BooleanField(default=True),
        ),
    ]
