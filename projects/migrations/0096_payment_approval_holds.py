"""O4 — the payment approval gate: a default that changes, and the hold table.

THREE SCHEMA FACTS, NO DATA MIGRATION.

  * `PaymentRequest.status` now defaults to `pending_approval` instead of `approved`.
    This is a default, not a value: **no existing row is touched**, and none needed to
    be — this database held zero `PaymentRequest` rows when the migration was written,
    and every raise path states the status explicitly anyway. If a deployment DOES carry
    rows raised before O4, they stay `approved` and correctly so: they were approved
    under the rule that was in force, and back-dating them into a gate that did not exist
    would invent a decision nobody made.
  * `PaymentRequestHold` is the new table. One row per hold, written once and never
    edited; `uniq_open_hold_per_payment_request` is a PARTIAL unique index
    (`responded_at IS NULL`) so a request may be held any number of times in sequence and
    never twice at once.
  * `payment_request_refusal_needs_reason` NARROWS from ['rejected', 'on_hold'] to
    ['rejected']. It is not a relaxation: a hold's reason is now required by
    `payment_hold_needs_reason` on the hold row, which also records who held it, when,
    and the answer. Leaving `on_hold` in the old constraint would demand the same text
    in two places and make ON_HOLD unreachable without writing it twice. Existing rows
    are unaffected, because the new condition admits every row the old one did.

REVERSIBLE. Reversing restores the `approved` default and drops the hold table, which is
a real loss of the held/answered conversation — so reverse only before any hold exists.
PROTECT on `payment_request` and `held_by` means a payment with holds cannot be deleted
out from under them either way.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('projects', '0095_vendor_order_programs_and_nullable_payment_anchor'),
    ]

    operations = [
        # The hold's reason moved to PaymentRequestHold, so `on_hold` leaves this
        # constraint and `rejected` keeps it. See the note on the constraint in
        # models.py: the rule did not weaken, it moved to the row that can carry the
        # whole exchange. Dropping and re-adding is how Django expresses an altered
        # CHECK; no row is read or rewritten either way.
        migrations.RemoveConstraint(
            model_name='paymentrequest',
            name='payment_request_refusal_needs_reason',
        ),
        migrations.AddConstraint(
            model_name='paymentrequest',
            constraint=models.CheckConstraint(
                condition=models.Q(models.Q(('status', 'rejected'), _negated=True),
                                   models.Q(('decision_reason', ''), _negated=True),
                                   _connector='OR'),
                name='payment_request_refusal_needs_reason'),
        ),
        migrations.AlterField(
            model_name='paymentrequest',
            name='status',
            field=models.CharField(choices=[('pending_approval', 'Awaiting approval'), ('approved', 'Approved — awaiting payment'), ('on_hold', 'On hold'), ('rejected', 'Rejected'), ('confirmed', 'Paid')], default='pending_approval', max_length=20),
        ),
        migrations.CreateModel(
            name='PaymentRequestHold',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('reason', models.TextField()),
                ('held_at', models.DateTimeField(auto_now_add=True)),
                ('response', models.TextField(blank=True, default='')),
                ('responded_at', models.DateTimeField(blank=True, null=True)),
                ('held_by', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='payment_holds_taken', to='projects.userprofile')),
                ('payment_request', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='holds', to='projects.paymentrequest')),
                ('responded_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='payment_holds_answered', to='projects.userprofile')),
            ],
            options={
                'ordering': ['-held_at'],
                'constraints': [models.CheckConstraint(condition=models.Q(('reason', ''), _negated=True), name='payment_hold_needs_reason'), models.CheckConstraint(condition=models.Q(('response', ''), models.Q(('responded_by__isnull', False), ('responded_at__isnull', False)), _connector='OR'), name='payment_hold_response_needs_responder'), models.UniqueConstraint(condition=models.Q(('responded_at__isnull', True)), fields=('payment_request',), name='uniq_open_hold_per_payment_request')],
            },
        ),
    ]
