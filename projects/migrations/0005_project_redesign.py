"""
Manual migration — full replacement of the scaffold Project model.

Strategy: use SeparateDatabaseAndState to DROP the old tables via raw SQL
(CASCADE handles FK children), delete them from migration state, then
CreateModel operations rebuild everything fresh. This avoids the interactive
"rename or add default" prompts caused by incompatible field changes.

Old data: confirmed disposable — no production project records existed.
"""
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('projects', '0004_alter_userprofile_role'),
        ('auth', '0012_alter_user_first_name_max_length'),
    ]

    operations = [

        # ── Step 1: drop old tables + remove from state ──────────────────────
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(
                    "DROP TABLE IF EXISTS projects_milestone CASCADE;",
                    reverse_sql=migrations.RunSQL.noop,
                ),
                migrations.RunSQL(
                    "DROP TABLE IF EXISTS projects_projectdocument CASCADE;",
                    reverse_sql=migrations.RunSQL.noop,
                ),
                migrations.RunSQL(
                    "DROP TABLE IF EXISTS projects_project CASCADE;",
                    reverse_sql=migrations.RunSQL.noop,
                ),
            ],
            state_operations=[
                migrations.DeleteModel('Milestone'),
                migrations.DeleteModel('ProjectDocument'),
                migrations.DeleteModel('Project'),
            ],
        ),

        # ── Step 2: create new Project ────────────────────────────────────────
        migrations.CreateModel(
            name='Project',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('project_id', models.CharField(blank=True, editable=False, max_length=20, unique=True)),
                ('customer_name', models.CharField(max_length=100)),
                ('customer_phone', models.CharField(max_length=10)),
                ('customer_email', models.EmailField(blank=True, max_length=254, null=True)),
                ('site_address', models.TextField()),
                ('city', models.CharField(max_length=50)),
                ('state', models.CharField(default='Uttar Pradesh', max_length=50)),
                ('project_type', models.CharField(
                    choices=[('Residential', 'Residential'), ('OPEX', 'OPEX'), ('CAPEX', 'CAPEX')],
                    max_length=20,
                )),
                ('capacity_kw', models.DecimalField(decimal_places=2, max_digits=6)),
                ('contract_value', models.DecimalField(decimal_places=2, max_digits=12)),
                ('survey_date', models.DateField(blank=True, null=True)),
                ('target_commissioning_date', models.DateField()),
                ('status', models.CharField(
                    choices=[
                        ('Draft',        'Draft'),
                        ('Active',       'Active'),
                        ('In Progress',  'In Progress'),
                        ('Commissioned', 'Commissioned'),
                        ('On Hold',      'On Hold'),
                        ('Cancelled',    'Cancelled'),
                    ],
                    default='Draft',
                    max_length=20,
                )),
                ('zoho_crm_id', models.CharField(blank=True, max_length=50, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('activated_at', models.DateTimeField(blank=True, null=True)),
                ('assigned_pm', models.ForeignKey(
                    limit_choices_to={'role': 'PM'},
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name='pm_projects',
                    to='projects.userprofile',
                )),
                ('assigned_site_engineer', models.ForeignKey(
                    limit_choices_to={'is_active': True, 'role': 'Site Engineer'},
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name='se_projects',
                    to='projects.userprofile',
                )),
                ('created_by', models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name='created_projects',
                    to='auth.user',
                )),
            ],
            options={
                'ordering': ['-created_at'],
            },
        ),

        # ── Step 3: recreate Milestone ────────────────────────────────────────
        migrations.CreateModel(
            name='Milestone',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('title', models.CharField(max_length=200)),
                ('description', models.TextField(blank=True)),
                ('due_date', models.DateField(blank=True, null=True)),
                ('completed_date', models.DateField(blank=True, null=True)),
                ('status', models.CharField(
                    choices=[
                        ('pending',     'Pending'),
                        ('in_progress', 'In Progress'),
                        ('completed',   'Completed'),
                        ('delayed',     'Delayed'),
                    ],
                    default='pending',
                    max_length=20,
                )),
                ('order', models.IntegerField(default=0)),
                ('assigned_to', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    to='auth.user',
                )),
                ('project', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='milestones',
                    to='projects.project',
                )),
            ],
            options={
                'ordering': ['order', 'due_date'],
            },
        ),

        # ── Step 4: recreate ProjectDocument ─────────────────────────────────
        migrations.CreateModel(
            name='ProjectDocument',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('doc_type', models.CharField(
                    choices=[
                        ('contract',  'Contract'),
                        ('drawing',   'Drawing / Layout'),
                        ('approval',  'Government Approval'),
                        ('subsidy',   'Subsidy Document'),
                        ('invoice',   'Invoice'),
                        ('photo',     'Site Photo'),
                        ('other',     'Other'),
                    ],
                    max_length=20,
                )),
                ('title', models.CharField(max_length=200)),
                ('file', models.FileField(upload_to='project_docs/%Y/%m/')),
                ('notes', models.TextField(blank=True)),
                ('uploaded_at', models.DateTimeField(auto_now_add=True)),
                ('project', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='documents',
                    to='projects.project',
                )),
                ('uploaded_by', models.ForeignKey(
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    to='auth.user',
                )),
            ],
        ),

        # ── Step 5: create ProjectPhase ───────────────────────────────────────
        migrations.CreateModel(
            name='ProjectPhase',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('phase_name', models.CharField(max_length=100)),
                ('phase_order', models.PositiveIntegerField()),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('project', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='phases',
                    to='projects.project',
                )),
            ],
            options={
                'ordering': ['phase_order'],
            },
        ),

        # ── Step 6: create Task ───────────────────────────────────────────────
        migrations.CreateModel(
            name='Task',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('task_name', models.CharField(max_length=200)),
                ('task_order', models.PositiveIntegerField()),
                ('assigned_role', models.CharField(
                    choices=[
                        ('PM',            'PM'),
                        ('Site Engineer', 'Site Engineer'),
                        ('Finance',       'Finance'),
                        ('SCM',           'SCM'),
                        ('BD / Sales',    'BD / Sales'),
                        ('Design',        'Design'),
                    ],
                    default='PM',
                    max_length=20,
                )),
                ('status', models.CharField(
                    choices=[
                        ('Not Started', 'Not Started'),
                        ('In Progress', 'In Progress'),
                        ('Done',        'Done'),
                        ('Blocked',     'Blocked'),
                    ],
                    default='Not Started',
                    max_length=20,
                )),
                ('due_date', models.DateField(blank=True, null=True)),
                ('completed_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('phase', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='tasks',
                    to='projects.projectphase',
                )),
            ],
            options={
                'ordering': ['task_order'],
            },
        ),

    ]
