import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('projects', '0014_webhook_fields'),
    ]

    operations = [
        # Remove old ProjectDocument (FileField-based, auth.User FK)
        migrations.DeleteModel(name='ProjectDocument'),

        # Create new ProjectDocument (Supabase-based, UserProfile FK)
        migrations.CreateModel(
            name='ProjectDocument',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('file_name', models.CharField(max_length=255)),
                ('file_url', models.URLField(max_length=1000)),
                ('supabase_path', models.CharField(max_length=500)),
                ('file_type', models.CharField(
                    choices=[('Document', 'Document'), ('Photo', 'Photo')],
                    max_length=20,
                )),
                ('file_size_kb', models.PositiveIntegerField(default=0)),
                ('uploaded_at', models.DateTimeField(auto_now_add=True)),
                ('is_deleted', models.BooleanField(default=False)),
                ('deleted_at', models.DateTimeField(blank=True, null=True)),
                ('project', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='documents',
                    to='projects.project',
                )),
                ('uploaded_by', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='project_documents',
                    to='projects.userprofile',
                )),
                ('deleted_by', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='deleted_project_documents',
                    to='projects.userprofile',
                )),
            ],
            options={'ordering': ['-uploaded_at']},
        ),

        # Create new TaskAttachment (first migration for this model)
        migrations.CreateModel(
            name='TaskAttachment',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('file_name', models.CharField(max_length=255)),
                ('file_url', models.URLField(max_length=1000)),
                ('supabase_path', models.CharField(max_length=500)),
                ('file_type', models.CharField(
                    choices=[('Document', 'Document'), ('Photo', 'Photo')],
                    max_length=20,
                )),
                ('file_size_kb', models.PositiveIntegerField(default=0)),
                ('uploaded_at', models.DateTimeField(auto_now_add=True)),
                ('is_deleted', models.BooleanField(default=False)),
                ('deleted_at', models.DateTimeField(blank=True, null=True)),
                ('task', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='attachments',
                    to='projects.task',
                )),
                ('uploaded_by', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='task_attachments',
                    to='projects.userprofile',
                )),
                ('deleted_by', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='deleted_task_attachments',
                    to='projects.userprofile',
                )),
            ],
            options={'ordering': ['-uploaded_at']},
        ),
    ]
