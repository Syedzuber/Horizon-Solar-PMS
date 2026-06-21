from django.db import migrations, models


class Migration(migrations.Migration):
    """
    Add Task.blocked_since — a DateTimeField set when a task transitions TO 'Blocked'
    and cleared when it un-blocks. Enables the "blocked tasks aged 7d+" CEO-dashboard KPI.
    """

    dependencies = [
        ('projects', '0023_vendor_brand'),
    ]

    operations = [
        migrations.AddField(
            model_name='task',
            name='blocked_since',
            field=models.DateTimeField(
                blank=True,
                null=True,
                # Set when status transitions TO 'Blocked'; cleared on un-block so re-blocks re-age from zero
            ),
        ),
    ]
