"""Session D — the seventh subject type in the state ledger.

CHOICES ONLY. `StatusTransition.subject_type` is a plain CharField whose vocabulary is
enforced by Django `choices` and by `utils._subject_type_registry()`, which refuses an
unregistered model class outright. There is NO database-level CHECK constraint mirroring
the list (0065 created none, and the model's Meta declares only ordering and two
indexes), so this AlterField changes no column type, no width and no constraint — on
PostgreSQL it is a no-op at the SQL level. It exists so `makemigrations --check` stays
quiet and so the migration history records when `design_assignment` became a legal value.

Nothing to reverse and nothing to backfill: no existing row can carry the new value,
because no code could write it before this commit.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('projects', '0078_boq_correction_record'),
    ]

    operations = [
        migrations.AlterField(
            model_name='statustransition',
            name='subject_type',
            field=models.CharField(choices=[('project', 'Project'), ('task', 'Task'), ('boq', 'BOQ'), ('delivery_challan', 'Delivery Challan'), ('issue', 'Issue'), ('payment_milestone', 'Payment Milestone'), ('design_assignment', 'Design Assignment')], db_index=True, max_length=30),
        ),
    ]
