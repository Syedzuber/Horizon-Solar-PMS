from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('projects', '0006_task_assigned_to_task_duration_days_task_task_type'),
    ]

    operations = [
        migrations.CreateModel(
            name='DueDateChangeLog',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('old_date', models.DateField(blank=True, null=True)),
                ('new_date', models.DateField(blank=True, null=True)),
                ('changed_at', models.DateTimeField(auto_now_add=True)),
                ('changed_by', models.ForeignKey(
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    to='projects.userprofile',
                )),
                ('task', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='due_date_changes',
                    to='projects.task',
                )),
            ],
            options={
                'ordering': ['-changed_at'],
            },
        ),
    ]
