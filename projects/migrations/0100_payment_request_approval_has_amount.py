"""O4b — an approved or paid payment request always says how much was approved.

The second half of 0099, in its own transaction for the reason given there. 0099 filled
every approved and confirmed row, so the constraint has nothing to trip on.

Reverse: the constraint goes.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('projects', '0099_payment_request_approved_amount'),
    ]

    operations = [
        migrations.AddConstraint(
            model_name='paymentrequest',
            constraint=models.CheckConstraint(
                condition=models.Q(models.Q(('status__in', ['approved', 'confirmed'])
                                            , _negated=True),
                                   ('approved_amount__isnull', False), _connector='OR'),
                name='payment_request_approval_has_amount'),
        ),
    ]
