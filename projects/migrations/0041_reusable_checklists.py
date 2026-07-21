import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    """
    Second checklist rework: discard the prior per-task-instance ChecklistItem model
    entirely and replace it with a named, reusable Checklist entity.

      - Checklist              — the reusable template (name, is_active, created_by)
      - ChecklistItem          — line items belonging to a Checklist (recreated fresh,
                                  FK now points at Checklist instead of Task)
      - ChecklistTaskLink      — assigns a checklist to a (task_name, project_type) pair;
                                  UNIQUE on that pair so a task has at most one checklist
      - ChecklistItemCompletion — per-(item, task) completion state + photo

    The old ChecklistItem carried per-task completion data inline; that model and all its
    rows are dropped (the prior model is being replaced, not migrated). Delete-then-create
    reuses the ChecklistItem name with an incompatible new shape in a single migration.
    """

    dependencies = [
        ('projects', '0040_checklistitem'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='Checklist',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=200)),
                ('is_active', models.BooleanField(default=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='created_checklists', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['name'],
            },
        ),
        # Drop the prior per-task ChecklistItem (and all its rows) before recreating the
        # name with a Checklist-scoped shape.
        migrations.DeleteModel(
            name='ChecklistItem',
        ),
        migrations.CreateModel(
            name='ChecklistItem',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('label', models.TextField()),
                ('order', models.PositiveIntegerField(default=0)),
                ('checklist', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='items', to='projects.checklist')),
            ],
            options={
                'ordering': ['order', 'pk'],
            },
        ),
        migrations.CreateModel(
            name='ChecklistTaskLink',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('task_name', models.CharField(max_length=200)),
                ('project_type', models.CharField(choices=[('Residential', 'Residential'), ('OPEX', 'OPEX'), ('CAPEX', 'CAPEX')], max_length=20)),
                ('checklist', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='task_links', to='projects.checklist')),
            ],
            options={
                'ordering': ['project_type', 'task_name'],
                'unique_together': {('task_name', 'project_type')},
            },
        ),
        migrations.CreateModel(
            name='ChecklistItemCompletion',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('is_checked', models.BooleanField(default=False)),
                ('photo_file_name', models.CharField(blank=True, default='', max_length=255)),
                ('photo_url', models.URLField(blank=True, default='', max_length=1000)),
                ('photo_supabase_path', models.CharField(blank=True, default='', max_length=500)),
                ('checked_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('checked_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='checked_checklist_items', to=settings.AUTH_USER_MODEL)),
                ('item', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='completions', to='projects.checklistitem')),
                ('task', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='checklist_completions', to='projects.task')),
            ],
            options={
                'ordering': ['pk'],
                'unique_together': {('item', 'task')},
            },
        ),
    ]
