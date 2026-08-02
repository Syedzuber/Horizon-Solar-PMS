"""
Part 10 — the Design Head's quality analytics selection.

ONE NEW TABLE AND NOTHING ELSE. No workflow model is touched, no field is added to any
design model, and no data operation runs. Every metric on the Part 10 screen is computed
from rows that already exist; this table stores only which of those metrics one person
wants on screen.

A SEPARATE TABLE RATHER THAN A COLUMN ON UserProfile — see the note above the model. The
migration is reversible with no data loss beyond the selections themselves, which is the
correct blast radius for a reporting preference.
"""
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('projects', '0058_part46_change_request_triage'),
    ]

    operations = [
        migrations.CreateModel(
            name='DesignAnalyticsPreference',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False, verbose_name='ID')),
                ('metrics', models.JSONField(blank=True, default=list)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('profile', models.OneToOneField(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='design_analytics_preference',
                    to='projects.userprofile')),
            ],
        ),
    ]
