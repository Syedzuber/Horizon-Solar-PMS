"""Checklist mechanism, three structural gaps and one recording gap.

  * Checklist.requires_photo — the photo demand becomes the checklist author's decision
    instead of an assumption baked into the completion view. DEFAULT True, so every
    checklist that exists when this applies keeps the rule it already had and nothing
    that was mandatory becomes optional by upgrading.

  * ChecklistItem.section — a presentational heading, blank by default, so every existing
    item lands in the one ungrouped bucket and renders exactly as it does today.

  * ChecklistItemCompletion.answer / .remarks / .witness_names — a real No, its mandatory
    reason, and the names of people who were in the room.

WHY THE BACKFILL EXISTS EVEN THOUGH IT MOVES NOTHING TODAY. `answer` splits a meaning out
of `is_checked`, which is the one operation in this migration that could silently drop
information: a ticked row meant "Yes" and would arrive with answer='' — indistinguishable
from unanswered — if nobody wrote it. So it is written. Both databases this applies to
(local and production, checked 05 Sep 2026) hold ZERO completions, so the UPDATE touches
no rows on either; it is here because a migration that is correct only because a table
happens to be empty is a migration that is wrong the first time it is not.

The constraint is added AFTER the backfill, deliberately: adding it first would leave a
window in which the data migration's own writes are being checked against a rule the rows
have not been prepared for. Nothing here writes a 'no', so it would hold either way —
ordering it correctly costs nothing and does not depend on that staying true.
"""

from django.conf import settings
from django.db import migrations, models


def backfill_answer_from_is_checked(apps, schema_editor):
    """Every completion that was ticked before `answer` existed was a Yes.

    A tick was the only response the surface could record: there was one checkbox, and it
    meant the line was satisfied. Writing YES onto those rows preserves that meaning
    exactly. Rows never ticked keep answer='' — unanswered — which is what they are.
    """
    Completion = apps.get_model('projects', 'ChecklistItemCompletion')
    Completion.objects.filter(is_checked=True).exclude(answer='yes').update(answer='yes')


def unbackfill_answer(apps, schema_editor):
    """Reverse: drop the derived answers, leaving is_checked as the sole record again.

    Only ever clears 'yes', which is the only value this migration wrote. A 'no' or 'na'
    recorded after it applied is real user input the forward pass never invented, and
    reversing a schema change must not delete it.
    """
    Completion = apps.get_model('projects', 'ChecklistItemCompletion')
    Completion.objects.filter(answer='yes').update(answer='')


class Migration(migrations.Migration):

    dependencies = [
        ('projects', '0080_punch_point'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='checklist',
            name='requires_photo',
            field=models.BooleanField(default=True, help_text='When on, an item on this checklist cannot be answered without a photo.'),
        ),
        migrations.AddField(
            model_name='checklistitem',
            name='section',
            field=models.CharField(blank=True, default='', max_length=120),
        ),
        migrations.AddField(
            model_name='checklistitemcompletion',
            name='answer',
            field=models.CharField(blank=True, choices=[('yes', 'Yes'), ('no', 'No'), ('na', 'Not Applicable')], default='', max_length=3),
        ),
        migrations.AddField(
            model_name='checklistitemcompletion',
            name='remarks',
            field=models.TextField(blank=True, default=''),
        ),
        migrations.AddField(
            model_name='checklistitemcompletion',
            name='witness_names',
            field=models.TextField(blank=True, default='', help_text='Names asserted by the completer. Not verified signatures.'),
        ),
        migrations.RunPython(
            backfill_answer_from_is_checked,
            unbackfill_answer,
        ),
        migrations.AddConstraint(
            model_name='checklistitemcompletion',
            constraint=models.CheckConstraint(condition=models.Q(('answer', 'no'), ('remarks', ''), _negated=True), name='checklist_completion_no_requires_remarks'),
        ),
    ]
