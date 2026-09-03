# Prompt 0.5 — schema finalisation. Runs only after 0069 has given every checklist a
# code and a status.
#
# is_active is REMOVED, not deprecated in place: two columns answering "is this live"
# is exactly the drift this session exists to stop. It survives as a read/write property
# over status on the model, so existing callers keep working against one stored truth.
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('projects', '0069_backfill_checklist_versions'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='checklist',
            name='is_active',
        ),
        migrations.AddConstraint(
            model_name='checklist',
            constraint=models.UniqueConstraint(
                fields=('code', 'version_no'),
                name='uniq_checklist_code_version',
            ),
        ),
        # At most ONE active version per code. Partial (condition=) so any number of
        # draft and archived versions coexist beside it — the history is kept and the
        # exclusivity is not weakened by keeping it. Same shape as
        # uniq_active_task_template_per_code and uniq_active_site_group_membership.
        migrations.AddConstraint(
            model_name='checklist',
            constraint=models.UniqueConstraint(
                condition=models.Q(('status', 'active')),
                fields=('code',),
                name='uniq_active_checklist_per_code',
            ),
        ),
    ]
