import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('projects', '0015_file_uploads'),
    ]

    operations = [
        migrations.CreateModel(
            name='Issue',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('title', models.CharField(max_length=200)),
                ('description', models.TextField(blank=True, default='')),
                ('severity', models.CharField(
                    max_length=20,
                    choices=[('Low', 'Low'), ('Medium', 'Medium'), ('High', 'High'), ('Critical', 'Critical')],
                    default='Medium',
                )),
                ('status', models.CharField(
                    max_length=20,
                    choices=[('Open', 'Open'), ('In Progress', 'In Progress'), ('Resolved', 'Resolved'), ('Closed', 'Closed')],
                    default='Open',
                )),
                ('raised_at', models.DateTimeField(auto_now_add=True)),
                ('due_date', models.DateField(null=True, blank=True)),
                ('resolved_at', models.DateTimeField(null=True, blank=True)),
                ('closed_at', models.DateTimeField(null=True, blank=True)),
                ('resolution_note', models.TextField(blank=True, default='')),
                ('project', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='issues',
                    to='projects.project',
                )),
                ('task', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='issues',
                    to='projects.task',
                )),
                ('raised_by', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='raised_issues',
                    to='projects.userprofile',
                )),
                ('assigned_to', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='assigned_issues',
                    to='projects.userprofile',
                )),
            ],
            options={'ordering': ['-raised_at']},
        ),
        migrations.CreateModel(
            name='ActivityLog',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('action', models.CharField(max_length=255)),
                ('entity_type', models.CharField(max_length=50, blank=True, default='')),
                ('entity_id', models.PositiveIntegerField(null=True, blank=True)),
                ('timestamp', models.DateTimeField(auto_now_add=True)),
                ('project', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='activity_logs',
                    to='projects.project',
                )),
                ('actor', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='activity_logs',
                    to='projects.userprofile',
                )),
            ],
            options={'ordering': ['-timestamp']},
        ),
    ]
