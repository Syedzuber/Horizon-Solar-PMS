# Prompt 0.5 — schema half.
#
# Two independent changes that must ship together because the second is why the first
# matters: Checklist becomes a versioned family (R-7), and ChecklistItemCompletion stops
# being destroyed by its own foreign key (R-8).
#
# The Checklist uniqueness constraints are NOT added here — every row still carries
# code='' at this point. They are added in 0070, after 0069 has assigned the codes.
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('projects', '0067_seed_residential_template_v1'),
    ]

    operations = [
        # --- Checklist becomes one numbered version of a family ------------------
        migrations.AddField(
            model_name='checklist',
            name='code',
            # Temporary '' for existing rows; 0069 derives the real code from the name.
            # preserve_default=False keeps the model field itself defaultless, matching
            # TaskTemplate.code.
            field=models.CharField(default='', max_length=50),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='checklist',
            name='version_no',
            field=models.PositiveIntegerField(default=1),
        ),
        migrations.AddField(
            model_name='checklist',
            name='status',
            field=models.CharField(
                choices=[('draft', 'Draft'), ('active', 'Active'), ('archived', 'Archived')],
                default='draft', max_length=10,
            ),
        ),
        migrations.AddField(
            model_name='checklist',
            name='effective_from',
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AlterModelOptions(
            name='checklist',
            options={'ordering': ['name', '-version_no']},
        ),

        # --- The completion records the text it was answering --------------------
        migrations.AddField(
            model_name='checklistitemcompletion',
            name='item_text_snapshot',
            field=models.TextField(blank=True, default=''),
        ),

        # --- Deleting an item must never delete the record of answering it -------
        # Order matters: unique_together must come off BEFORE the column is made
        # nullable, and the partial replacement goes on after.
        migrations.AlterUniqueTogether(
            name='checklistitemcompletion',
            unique_together=set(),
        ),
        migrations.AlterField(
            model_name='checklistitemcompletion',
            name='item',
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='completions', to='projects.checklistitem',
            ),
        ),
        migrations.AddConstraint(
            model_name='checklistitemcompletion',
            constraint=models.UniqueConstraint(
                condition=models.Q(('item__isnull', False)),
                fields=('item', 'task'),
                name='uniq_checklist_completion_item_task',
            ),
        ),
    ]
