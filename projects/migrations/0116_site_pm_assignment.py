# Site PM history (rulings PM-2, PM-3): the SitePmAssignment table plus its backfill.
#
# BACKFILL: one OPEN row (valid_to NULL) per site that has an assigned_pm today,
# soft-deleted sites included (ruling Q4) so a restored site never comes back with a
# PM and no history row.
#
#   pm            the site's current assigned_pm
#   valid_from    the site's created_at - NOT when this PM took over, which nothing
#                 recorded; is_backfilled=True says so and the model docstring forbids
#                 showing it as "PM since"
#   assigned_by   NULL (unknown)
#   reason        blank
#
# No earlier spans are reconstructed: ActivityLog holds too few PM-change lines to
# rebuild them, and a guessed history is worse than none.
#
# Reverse deletes only the backfilled rows (the table is dropped next anyway). The
# historical model has no append-only save()/delete(), so bulk_create and a queryset
# delete both work here.

import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


def backfill(apps, schema_editor):
    Project = apps.get_model('projects', 'Project')
    SitePmAssignment = apps.get_model('projects', 'SitePmAssignment')
    # Every site with a PM, deleted or not, in pk order so a re-run on a fresh
    # database writes the rows in the same order.
    sites = (Project.objects
             .filter(assigned_pm__isnull=False)
             .values_list('pk', 'assigned_pm_id', 'created_at')
             .order_by('pk'))
    SitePmAssignment.objects.bulk_create([
        SitePmAssignment(site_id=pk, pm_id=pm_id, valid_from=created_at,
                         is_backfilled=True)
        for pk, pm_id, created_at in sites
    ])


def unbackfill(apps, schema_editor):
    SitePmAssignment = apps.get_model('projects', 'SitePmAssignment')
    SitePmAssignment.objects.filter(is_backfilled=True).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('projects', '0115_punch_point_rectify'),
    ]

    operations = [
        migrations.CreateModel(
            name='SitePmAssignment',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('valid_from', models.DateTimeField(default=django.utils.timezone.now)),
                ('valid_to', models.DateTimeField(blank=True, null=True)),
                ('reason', models.TextField(blank=True, default='')),
                ('is_backfilled', models.BooleanField(default=False)),
                ('assigned_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='pm_assignments_made', to='projects.userprofile')),
                ('pm', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='pm_site_assignments', to='projects.userprofile')),
                ('site', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='pm_assignments', to='projects.project')),
            ],
            options={
                'ordering': ['site', '-valid_from', '-pk'],
                'constraints': [models.UniqueConstraint(condition=models.Q(('valid_to__isnull', True)), fields=('site',), name='uniq_site_pm_open'), models.CheckConstraint(condition=models.Q(('valid_to__isnull', True), ('valid_to__gte', models.F('valid_from')), _connector='OR'), name='site_pm_ends_after_start')],
            },
        ),
        migrations.RunPython(backfill, unbackfill),
    ]
