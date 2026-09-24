"""O4b — an approver may approve part of a payment request.

PaymentRequest.approved_amount records how much of the request was approved; `amount`
stays what was requested. The first CHECK (0 < approved_amount <= amount, or NULL) is
added here, with the column.

THE BACKFILL: every existing row in `approved` or `confirmed` was approved in full —
there was no other kind of approval before this migration — so it takes approved_amount =
amount, and no order's arithmetic moves. Rows in any other status stay NULL.

TWO MIGRATIONS, NOT ONE, as 0092 and 0097/0098 did: this one UPDATEs
projects_paymentrequest, and on PostgreSQL an ALTER TABLE on a table with rows updated
earlier in the same transaction can fail with "pending trigger events". 0100 adds the
constraint that requires the backfill, in its own transaction.

Reverse: the column and its constraint are dropped; nothing else was changed.
"""

from django.db import migrations, models
from django.db.models import F


def backfill_approved_amount(apps, schema_editor):
    PaymentRequest = apps.get_model('projects', 'PaymentRequest')
    (PaymentRequest.objects
     .filter(status__in=['approved', 'confirmed'], approved_amount__isnull=True)
     .update(approved_amount=F('amount')))


class Migration(migrations.Migration):

    dependencies = [
        ('projects', '0098_vendor_order_total_amount_required'),
    ]

    operations = [
        migrations.AddField(
            model_name='paymentrequest',
            name='approved_amount',
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=14, null=True),
        ),
        migrations.AddConstraint(
            model_name='paymentrequest',
            constraint=models.CheckConstraint(
                condition=models.Q(('approved_amount__isnull', True),
                                   models.Q(('approved_amount__gt', 0),
                                            ('approved_amount__lte', models.F('amount'))),
                                   _connector='OR'),
                name='payment_request_approved_amount_within_requested'),
        ),
        migrations.RunPython(backfill_approved_amount, migrations.RunPython.noop),
    ]
