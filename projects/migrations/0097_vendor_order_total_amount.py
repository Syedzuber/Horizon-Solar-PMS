"""O3r — the order total becomes a stored figure; line quantity and amount become optional.

VendorOrder.total_amount is the value printed on the PO. Until now `total` was the sum
of the order's line amounts; from here the lines are the requirement the order was sized
against, with optional amounts, and the total is entered.

THE BACKFILL: every existing order takes the sum of its line amounts — exactly the figure
`total` returned for it before this migration, so no order's arithmetic moves.

THE CHECK REFUSES, IT NEVER INVENTS. An order whose lines sum to nothing has no figure to
carry forward, and 0098 is about to require one greater than 0. If any exist, this
migration stops and names them before writing anything; what their total should be is an
operator decision. (No raise path has ever created one — both required at least one
priced line — so on any real database this is expected to find none.)

TWO MIGRATIONS, NOT ONE, as 0092 did: this one UPDATEs projects_vendororder, and on
PostgreSQL an ALTER TABLE on a table with rows updated earlier in the same transaction can
fail with "pending trigger events". 0098 makes the column NOT NULL in its own transaction.

The line constraints are re-stated with an explicit IS NULL arm, so the names are kept
and the rule is unchanged for every row that has a value.

Reverse: the column is dropped and the line columns become NOT NULL again (which fails if
a null was written since — correctly: such a line did not exist before).
"""

from decimal import Decimal

from django.db import migrations, models
from django.db.models import Sum


def backfill_total_amount(apps, schema_editor):
    VendorOrder = apps.get_model('projects', 'VendorOrder')
    sums = dict(VendorOrder.objects.annotate(s=Sum('lines__amount'))
                .values_list('pk', 's'))
    empty = sorted(pk for pk, total in sums.items() if not total or total <= 0)
    if empty:
        raise RuntimeError(
            f'0097 refused: {len(empty)} VendorOrder row(s) have no priced line to take a '
            f'total from (pk {", ".join(str(pk) for pk in empty)}). Decide each order\'s '
            f'total, or remove it deliberately, then migrate again. Nothing was changed.'
        )
    for pk, total in sums.items():
        VendorOrder.objects.filter(pk=pk).update(total_amount=Decimal(total))


class Migration(migrations.Migration):

    dependencies = [
        ('projects', '0096_payment_approval_holds'),
    ]

    operations = [
        # The line table first: it is not written by the backfill below.
        migrations.RemoveConstraint(
            model_name='vendororderline',
            name='vendor_order_line_quantity_positive',
        ),
        migrations.RemoveConstraint(
            model_name='vendororderline',
            name='vendor_order_line_amount_positive',
        ),
        migrations.AlterField(
            model_name='vendororderline',
            name='quantity',
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True),
        ),
        migrations.AlterField(
            model_name='vendororderline',
            name='amount',
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=14, null=True),
        ),
        migrations.AddConstraint(
            model_name='vendororderline',
            constraint=models.CheckConstraint(
                condition=models.Q(('quantity__isnull', True), ('quantity__gt', 0),
                                   _connector='OR'),
                name='vendor_order_line_quantity_positive'),
        ),
        migrations.AddConstraint(
            model_name='vendororderline',
            constraint=models.CheckConstraint(
                condition=models.Q(('amount__isnull', True), ('amount__gt', 0),
                                   _connector='OR'),
                name='vendor_order_line_amount_positive'),
        ),
        migrations.AddField(
            model_name='vendororder',
            name='total_amount',
            field=models.DecimalField(decimal_places=2, max_digits=14, null=True),
        ),
        migrations.RunPython(backfill_total_amount, migrations.RunPython.noop),
    ]
