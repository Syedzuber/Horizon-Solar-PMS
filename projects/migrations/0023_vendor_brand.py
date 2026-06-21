import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    """
    Replace the single Vendor.make_brand CharField (0022) with a VendorBrand model
    that allows multiple brand entries per vendor, each optionally scoped to a category.
    """

    dependencies = [
        ('projects', '0022_vendor_make_brand'),
    ]

    operations = [
        # Remove the single-value CharField added in 0022
        migrations.RemoveField(
            model_name='vendor',
            name='make_brand',
        ),
        # Add the one-to-many VendorBrand model
        migrations.CreateModel(
            name='VendorBrand',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('make_brand', models.CharField(max_length=200)),
                ('vendor', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='brands',
                    to='projects.vendor',
                )),
                ('category', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='vendor_brands',
                    to='projects.vendorcategory',
                )),
            ],
            options={
                'ordering': ['make_brand'],
            },
        ),
    ]
