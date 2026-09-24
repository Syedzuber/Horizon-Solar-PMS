"""O3r — VendorOrder.total_amount becomes required and positive.

The second half of 0097, in its own transaction for the reason given there. 0097 filled
every existing row or refused, so the ALTER has no NULL to trip on.

Reverse: the constraint goes and the column becomes nullable again.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('projects', '0097_vendor_order_total_amount'),
    ]

    operations = [
        migrations.AlterField(
            model_name='vendororder',
            name='total_amount',
            field=models.DecimalField(decimal_places=2, max_digits=14),
        ),
        migrations.AddConstraint(
            model_name='vendororder',
            constraint=models.CheckConstraint(
                condition=models.Q(('total_amount__gt', 0)),
                name='vendor_order_total_amount_positive'),
        ),
    ]
