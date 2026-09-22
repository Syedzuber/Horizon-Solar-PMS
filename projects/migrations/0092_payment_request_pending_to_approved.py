"""O1 — rename the stored PaymentRequest status 'pending' to 'approved'.

'pending' meant "with Finance, awaiting payment", which is exactly what APPROVED means
under the approval lifecycle 0091 introduces. Every other column is left alone: no
backfill of approved_by / approved_at, because nobody approved these rows — they were
raised before approval existed.

Its own migration, after 0091's schema change, rather than a RunPython inside it, so the
data step never shares a transaction with ALTER TABLE on the same table.

Reversible: 'approved' goes back to 'pending'. Safe only because nothing between 0091
and a reversal can create a genuinely approved row — no view writes APPROVED except
raise_payment_request, whose rows were 'pending' before this migration anyway.
"""

from django.db import migrations


def forwards(apps, schema_editor):
    PaymentRequest = apps.get_model('projects', 'PaymentRequest')
    PaymentRequest.objects.filter(status='pending').update(status='approved')


def backwards(apps, schema_editor):
    PaymentRequest = apps.get_model('projects', 'PaymentRequest')
    PaymentRequest.objects.filter(status='approved').update(status='pending')


class Migration(migrations.Migration):

    dependencies = [
        ('projects', '0091_vendor_orders_and_payment_approval'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
