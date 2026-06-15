import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('projects', '0017_comment'),
    ]

    operations = [
        migrations.CreateModel(
            name='DeliveryChallan',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('po_number', models.CharField(blank=True, default='', max_length=100)),
                ('dc_number', models.CharField(max_length=100)),
                ('dc_date', models.DateField()),
                ('expected_delivery_date', models.DateField(blank=True, null=True)),
                ('status', models.CharField(
                    choices=[
                        ('Expected', 'Expected'),
                        ('Partially Received', 'Partially Received'),
                        ('Received', 'Received'),
                        ('Rejected', 'Rejected'),
                    ],
                    default='Expected',
                    max_length=30,
                )),
                ('notes', models.TextField(blank=True, default='')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('created_by', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='created_challans',
                    to='projects.userprofile',
                )),
                ('project', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='delivery_challans',
                    to='projects.project',
                )),
                ('vendor', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='delivery_challans',
                    to='projects.vendor',
                )),
            ],
            options={'ordering': ['-created_at']},
        ),
        migrations.CreateModel(
            name='DCLineItem',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('boq_category', models.CharField(
                    choices=[
                        ('Solar Modules', 'Solar Modules'),
                        ('Structure', 'Structure'),
                        ('Inverter', 'Inverter'),
                        ('BOS', 'BOS'),
                    ],
                    max_length=50,
                )),
                ('item_description', models.CharField(max_length=255)),
                ('ordered_quantity', models.DecimalField(decimal_places=2, max_digits=10)),
                ('unit', models.CharField(default='Nos', max_length=20)),
                ('received_quantity', models.DecimalField(blank=True, decimal_places=2, max_digits=10, null=True)),
                ('condition', models.CharField(
                    blank=True,
                    choices=[
                        ('Good', 'Good'),
                        ('Damaged', 'Damaged'),
                        ('Partial', 'Partial'),
                    ],
                    max_length=20,
                    null=True,
                )),
                ('grn_date', models.DateField(blank=True, null=True)),
                ('grn_notes', models.TextField(blank=True, default='')),
                ('challan', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='line_items',
                    to='projects.deliverychallan',
                )),
                ('grn_confirmed_by', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='confirmed_line_items',
                    to='projects.userprofile',
                )),
            ],
            options={'ordering': ['boq_category']},
        ),
    ]
