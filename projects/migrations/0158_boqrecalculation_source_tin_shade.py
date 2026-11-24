# B-2c — BOQRecalculation.source gains 'tin_shade_fix' (fix_tin_shade_ballast --apply-counts
# now recalculates the BOQ it writes a count on; Zuber Q1, 24 Nov).
# Choices only, like 0154: sqlmigrate emits no SQL, so this is additive and touches no row.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('projects', '0157_boqlineoverride_set_as'),
    ]

    operations = [
        migrations.AlterField(
            model_name='boqrecalculation',
            name='source',
            field=models.CharField(choices=[('picker', 'BOQ picker save'), ('upload', 'BOQ spreadsheet upload'), ('correction', 'Reviewer correction'), ('override_set', 'Override set'), ('override_cleared', 'Override cleared'), ('copy', 'BOQ copied from another site'), ('tin_shade_fix', 'Tin Shade / Ballast count fix')], max_length=20),
        ),
    ]
