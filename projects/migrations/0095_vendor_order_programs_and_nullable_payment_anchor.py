"""O2d — an order records the tenders it was sized against, and a payment's site anchor
becomes optional.

TWO SCHEMA FACTS, NO DATA REWRITE.

  * `VendorOrderProgram` is the new table: which tenders an order was sized against,
    unique per (order, program). It starts empty. Nothing backfills it from the existing
    orders' sites, because the only orders that exist are Residential and a Residential
    project is never under a Program — there is nothing to derive.
  * `PaymentRequest.project` becomes nullable. NOT NULL was 0093's doing; it is relaxed
    here because an order may now name zero sites, and a payment on such an order has no
    site to be anchored to. Every existing row keeps the project it has; this migration
    changes no value.

REVERSIBLE AS LONG AS NO NULL EXISTS. Reversing re-imposes NOT NULL, which Postgres will
refuse if a site-less payment has been recorded by then — correctly, because the reverse
would otherwise have to invent an anchor. Drop or re-anchor those rows first. The new
table reverses cleanly; PROTECT on both its FKs means the rows themselves must go first,
and there are none until an order is raised against a tender.

The docstring changes on VendorOrderSite, VendorOrder and PaymentRequest carry no schema
and so appear nowhere below: they are the point of the change and live in models.py.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('projects', '0094_uniq_invoice_number_per_order'),
    ]

    operations = [
        migrations.AlterField(
            model_name='paymentrequest',
            name='project',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='payment_requests', to='projects.project'),
        ),
        migrations.CreateModel(
            name='VendorOrderProgram',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('order', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='programs', to='projects.vendororder')),
                ('program', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='vendor_order_programs', to='projects.program')),
            ],
            options={
                'constraints': [models.UniqueConstraint(fields=('order', 'program'), name='uniq_vendor_order_program')],
            },
        ),
    ]
