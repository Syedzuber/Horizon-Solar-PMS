# Prompt 0.5 — data half. Two backfills, no schema.
#
# Imports derive_checklist_code from projects.models by the same deliberate exception
# 0067 took: it is a pure string function that takes its model class as an argument, so
# it operates on the HISTORICAL model handed to it and not on the concrete one.
from django.db import migrations

from projects.models import derive_checklist_code

BATCH = 500


def forwards(apps, schema_editor):
    Checklist  = apps.get_model('projects', 'Checklist')
    Completion = apps.get_model('projects', 'ChecklistItemCompletion')

    # --- 1. Every existing checklist becomes v1 of its own family ----------------
    # is_active is READ here for the last time and dropped in 0070, so from that point
    # there is exactly one answer to "is this live":
    #   is_active=True  -> 'active'   (and effective_from stamped from created_at)
    #   is_active=False -> 'archived' (an inactive checklist is not a draft; it was
    #                                  once offered and is now withdrawn)
    migrated = 0
    for checklist in Checklist.objects.all().order_by('pk'):
        checklist.code = derive_checklist_code(
            checklist.name, Checklist, exclude_pk=checklist.pk)
        checklist.version_no = 1
        if checklist.is_active:
            checklist.status = 'active'
            checklist.effective_from = (
                checklist.created_at.date() if checklist.created_at else None)
        else:
            checklist.status = 'archived'
        checklist.save(update_fields=['code', 'version_no', 'status', 'effective_from'])
        migrated += 1

    # --- 2. Every existing completion snapshots the label it answered ------------
    # BEST EFFORT, AND IT CANNOT BE OTHERWISE. If a label was reworded before today,
    # this records the CURRENT text, not the text that was actually answered. That
    # history is already unrecoverable — the defect has been live for as long as the
    # model has existed. This stops the bleeding; it does not undo it.
    #
    # Writes item_text_snapshot and NOTHING else. is_checked, the three photo_* fields,
    # checked_by and checked_at are read-only here, and no row is deleted.
    updated = 0
    pending = []
    qs = (Completion.objects
          .filter(item__isnull=False)
          .exclude(item_text_snapshot__gt='')     # re-runnable: skip anything already set
          .select_related('item').order_by('pk'))
    for completion in qs.iterator(chunk_size=BATCH):
        completion.item_text_snapshot = completion.item.label
        pending.append(completion)
        if len(pending) >= BATCH:
            Completion.objects.bulk_update(pending, ['item_text_snapshot'])
            updated += len(pending)
            pending = []
    if pending:
        Completion.objects.bulk_update(pending, ['item_text_snapshot'])
        updated += len(pending)

    # Zero by construction on this run: item was NOT NULL until 0068, so a prior cascade
    # did not orphan a completion — it deleted the row outright. Those are gone and stay
    # gone. The count is printed so the number means something on every LATER database.
    orphaned = Completion.objects.filter(item__isnull=True).count()

    print(f'  [0069] Checklists migrated to v1: {migrated}')
    print(f'  [0069] Completions backfilled with item_text_snapshot: {updated}')
    print(f'  [0069] Completions with a null item (already lost, left null): {orphaned}')


def backwards(apps, schema_editor):
    # Reversible only in the direction that matters: 0070 restores is_active from status,
    # and clearing the snapshot here would destroy the only copy of the answered text on
    # any row completed since. Deliberately a no-op.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('projects', '0068_checklist_versioning'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
