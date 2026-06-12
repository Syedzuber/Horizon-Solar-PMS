import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('projects', '0013_paymentmilestone_commissioned_at'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # New fields for Zoho webhook
        migrations.AddField(
            model_name='project',
            name='customer_contact_person',
            field=models.CharField(blank=True, default='', max_length=255),
        ),
        migrations.AddField(
            model_name='project',
            name='zoho_deal_id',
            field=models.CharField(blank=True, default='', max_length=100),
        ),
        # Make fields nullable so webhook can create Draft projects with partial data
        migrations.AlterField(
            model_name='project',
            name='assigned_pm',
            field=models.ForeignKey(
                blank=True,
                limit_choices_to={'role': 'PM'},
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='pm_projects',
                to='projects.userprofile',
            ),
        ),
        migrations.AlterField(
            model_name='project',
            name='assigned_site_engineer',
            field=models.ForeignKey(
                blank=True,
                limit_choices_to={'is_active': True, 'role': 'Site Engineer'},
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='se_projects',
                to='projects.userprofile',
            ),
        ),
        migrations.AlterField(
            model_name='project',
            name='target_commissioning_date',
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name='project',
            name='capacity_kw',
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=6, null=True),
        ),
        migrations.AlterField(
            model_name='project',
            name='contract_value',
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True),
        ),
        migrations.AlterField(
            model_name='project',
            name='created_by',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='created_projects',
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
