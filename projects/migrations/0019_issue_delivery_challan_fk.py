import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    """
    Add nullable delivery_challan FK to Issue.
    Null for all existing issues (no data loss). SET_NULL on DC deletion
    so an issue persists even if its challan is deleted.
    """

    dependencies = [
        ('projects', '0018_delivery_challan'),
    ]

    operations = [
        migrations.AddField(
            model_name='issue',
            name='delivery_challan',
            field=models.ForeignKey(
                'projects.DeliveryChallan',
                null=True,
                blank=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='issues',
            ),
        ),
    ]
