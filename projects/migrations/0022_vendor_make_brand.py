from django.db import migrations, models


class Migration(migrations.Migration):
    """Add make_brand to Vendor — brand label shown in BOQ Make/Preference column."""

    dependencies = [
        ('projects', '0021_add_payment_request'),
    ]

    operations = [
        migrations.AddField(
            model_name='vendor',
            name='make_brand',
            field=models.CharField(blank=True, default='', max_length=200),
        ),
    ]
