from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('projects', '0025_userprofile_notification_prefs'),
    ]

    operations = [
        migrations.CreateModel(
            name='NotificationLog',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('channel', models.CharField(choices=[('in_app', 'In App'), ('whatsapp', 'WhatsApp'), ('email', 'Email')], max_length=10)),
                ('status', models.CharField(choices=[('sent', 'Sent'), ('failed', 'Failed'), ('skipped', 'Skipped')], max_length=10)),
                ('message', models.TextField()),
                ('template_name', models.CharField(blank=True, max_length=100)),
                ('error_detail', models.TextField(blank=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('actor', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='notifications_triggered', to='projects.userprofile')),
                ('recipient', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='notification_logs', to='projects.userprofile')),
                ('related_project', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to='projects.project')),
            ],
            options={
                'ordering': ['-created_at'],
            },
        ),
    ]
