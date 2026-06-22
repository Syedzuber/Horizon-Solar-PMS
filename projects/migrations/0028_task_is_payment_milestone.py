from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('projects', '0027_system_settings'),
    ]

    operations = [
        migrations.AddField(
            model_name='task',
            name='is_payment_milestone',
            field=models.BooleanField(default=False),
        ),
    ]
