"""SCHED-R — record each daily scheduler run on SystemSettings.

Two nullable columns, no data: an existing row starts at null, which the Site Dashboard
shows as "Dates not yet refreshed by the daily run" until the next 06:30 IST cron run
of `schedule_sites --all --apply` writes them. Nullable ADD COLUMN only, so it is
reversible and takes no table rewrite on Postgres.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('projects', '0141_sched2e_backfill_baseline'),
    ]

    operations = [
        migrations.AddField(
            model_name='systemsettings',
            name='schedule_last_run_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='systemsettings',
            name='schedule_last_run_sites',
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
    ]
