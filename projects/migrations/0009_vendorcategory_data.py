from django.db import migrations


CATEGORIES = [
    'Solar Modules',
    'Structure',
    'Inverter',
    'BOS',
    'Services',
    'Other',
]


def seed_vendor_categories(apps, schema_editor):
    VendorCategory = apps.get_model('projects', 'VendorCategory')
    for name in CATEGORIES:
        VendorCategory.objects.get_or_create(name=name)


def reverse_seed(apps, schema_editor):
    VendorCategory = apps.get_model('projects', 'VendorCategory')
    VendorCategory.objects.filter(name__in=CATEGORIES).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('projects', '0008_vendor_master'),
    ]

    operations = [
        migrations.RunPython(seed_vendor_categories, reverse_code=reverse_seed),
    ]
