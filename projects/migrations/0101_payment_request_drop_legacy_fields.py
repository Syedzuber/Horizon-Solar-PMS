"""O6 — PaymentRequest loses its five legacy columns.

boq_item, invoice_number and the three invoice_document_* fields were written by the
stand-alone project-page raise, retired in O2. Since then every raise path wrote them
blank (and boq_item NULL): what was bought is VendorOrderLine, and an invoice is a
VendorOrderDocument(doc_type='invoice') carrying its own number and amount. O6 rewrote the
last readers (card 4b, the payment detail page, My Documents, the Finance dashboard's
rows), so nothing reads them.

THE CHECK REFUSES, IT NEVER DISCARDS. A row holding a value in any of the five is a
payment raised before O2, and its invoice number or document link may be the only record
of what was paid for. If any exist, this migration stops and names them; moving each
value onto a VendorOrderDocument (or deciding it may go) is an operator decision, made
before this runs again. "Non-empty" is exact: any text but '' refuses, and a boq_item
that is not NULL refuses.

The check is plain SQL over a table name (legacy_value_pks), not the historical ORM, so
tests_payment_readers_o6 can run the same query against a real table carrying the five
columns — the test suite builds its schema from today's models, which no longer have
them. It only READS, so it leaves no pending trigger events for the ALTER TABLEs that
follow on PostgreSQL (the reason 0093 could be one migration too).

Reverse: the check does nothing and the five columns come back — the text columns as ''
on every row, boq_item as NULL. The values cannot come back, and none were lost: forward
refused while any existed.

WHY FOUR AlterFields BEFORE THE DROPS. The four text columns were NOT NULL with no default,
and a reversed RemoveField re-adds a column exactly as the state before it defines it — so
on a table with rows the re-add failed ("contains null values"), found by reversing this
migration on local Postgres. Giving the four a default of '' first changes the migration
STATE only (Django keeps defaults in Python, so the forward ALTER is a no-op) and hands the
reverse re-add a value to fill existing rows with.
"""

from django.db import migrations, models

#: The five columns as stored — boq_item is the FK's column, boq_item_id.
LEGACY_TEXT_COLUMNS = ('invoice_number', 'invoice_document_name',
                       'invoice_document_url', 'invoice_document_path')
LEGACY_FK_COLUMN = 'boq_item_id'


def legacy_value_pks(connection, table):
    """Primary keys of the rows in `table` holding a value in any legacy column, in pk
    order. COALESCE so a NULL text column (never written by Django, but a hand edit could)
    counts as empty rather than slipping past `<> ''`."""
    q = connection.ops.quote_name
    holds_value = ' OR '.join(
        [f'{q(LEGACY_FK_COLUMN)} IS NOT NULL']
        + [f"COALESCE({q(column)}, '') <> ''" for column in LEGACY_TEXT_COLUMNS])
    with connection.cursor() as cursor:
        cursor.execute(f'SELECT {q("id")} FROM {q(table)} WHERE {holds_value} '
                       f'ORDER BY {q("id")}')
        return [row[0] for row in cursor.fetchall()]


def refuse_legacy_values(apps, schema_editor):
    table = apps.get_model('projects', 'PaymentRequest')._meta.db_table
    pks = legacy_value_pks(schema_editor.connection, table)
    if pks:
        raise RuntimeError(
            f'0101 refused: {len(pks)} PaymentRequest row(s) hold a value in a legacy '
            f'column (boq_item, invoice_number, invoice_document_name, '
            f'invoice_document_url or invoice_document_path) — pk '
            f'{", ".join(str(pk) for pk in pks)}. Move each value onto a '
            f'VendorOrderDocument, or clear it deliberately, then migrate again. '
            f'Nothing was changed.'
        )


class Migration(migrations.Migration):

    dependencies = [
        ('projects', '0100_payment_request_approval_has_amount'),
    ]

    operations = [
        migrations.RunPython(refuse_legacy_values, migrations.RunPython.noop),
        # State only: see "WHY FOUR AlterFields" above.
        migrations.AlterField(
            model_name='paymentrequest', name='invoice_number',
            field=models.CharField(max_length=100, default=''),
        ),
        migrations.AlterField(
            model_name='paymentrequest', name='invoice_document_name',
            field=models.CharField(max_length=255, default=''),
        ),
        migrations.AlterField(
            model_name='paymentrequest', name='invoice_document_url',
            field=models.URLField(max_length=1000, default=''),
        ),
        migrations.AlterField(
            model_name='paymentrequest', name='invoice_document_path',
            field=models.CharField(max_length=500, default=''),
        ),
        migrations.RemoveField(
            model_name='paymentrequest',
            name='boq_item',
        ),
        migrations.RemoveField(
            model_name='paymentrequest',
            name='invoice_document_name',
        ),
        migrations.RemoveField(
            model_name='paymentrequest',
            name='invoice_document_path',
        ),
        migrations.RemoveField(
            model_name='paymentrequest',
            name='invoice_document_url',
        ),
        migrations.RemoveField(
            model_name='paymentrequest',
            name='invoice_number',
        ),
    ]
