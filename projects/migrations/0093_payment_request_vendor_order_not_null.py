"""O2 — PaymentRequest.vendor_order becomes NOT NULL.

O2 retired raise_payment_request, the only path that created a payment with no order;
every payment is now raised on vendor_order_create's page, against the order it records.

THE CHECK REFUSES, IT NEVER DELETES. A row with no order is a real payment somebody
raised, and may be one Finance has already paid. If any exist, this migration stops and
names them; attaching each to an order (or removing a test row) is an operator decision,
made before this runs again.

One migration rather than two (0092 split its data step out): the check only READS, so it
cannot leave pending trigger events for the ALTER TABLE that follows on PostgreSQL.

Reverse: the check does nothing and the column becomes nullable again.
"""

from django.db import migrations, models
import django.db.models.deletion


def refuse_orphan_payments(apps, schema_editor):
    PaymentRequest = apps.get_model('projects', 'PaymentRequest')
    orphans = list(PaymentRequest.objects.filter(vendor_order__isnull=True)
                   .order_by('pk').values_list('pk', flat=True))
    if orphans:
        raise RuntimeError(
            f'0093 refused: {len(orphans)} PaymentRequest row(s) have no vendor_order '
            f'(pk {", ".join(str(pk) for pk in orphans)}). Attach each to a VendorOrder '
            f'or remove it deliberately, then migrate again. Nothing was changed.'
        )


class Migration(migrations.Migration):

    dependencies = [
        ('projects', '0092_payment_request_pending_to_approved'),
    ]

    operations = [
        migrations.RunPython(refuse_orphan_payments, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='paymentrequest',
            name='vendor_order',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name='payments', to='projects.vendororder',
            ),
        ),
    ]
