from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('projects', '0026_notification_log'),
    ]

    operations = [
        migrations.CreateModel(
            name='SystemSettings',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('whatsapp_enabled', models.BooleanField(default=False)),
                ('email_enabled', models.BooleanField(default=False)),
            ],
            options={
                'verbose_name': 'System Settings',
                'verbose_name_plural': 'System Settings',
            },
        ),
    ]
