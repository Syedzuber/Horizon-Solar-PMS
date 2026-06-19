from django.db import migrations, models


class Migration(migrations.Migration):
    """
    Add damaged_quantity to DCLineItem.
    Existing rows default to 0 (no damage recorded) — no data loss.
    This is the precise source of truth for how many received units are damaged,
    replacing the coarse condition-dropdown approach.
    """

    dependencies = [
        ('projects', '0019_issue_delivery_challan_fk'),
    ]

    operations = [
        migrations.AddField(
            model_name='dclineitem',
            name='damaged_quantity',
            field=models.PositiveIntegerField(default=0),
        ),
    ]
