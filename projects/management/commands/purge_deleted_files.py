import logging
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    """
    Management command: hard-delete files that were soft-deleted beyond FILE_RETENTION_DAYS ago.
    Run on a cron schedule (e.g. nightly) to permanently remove data that users have already
    deleted from the UI. Two-step process:
      1. Remove the file from Supabase storage.
      2. Delete the DB row.
    Failures on individual files are logged and skipped — the command continues so
    one bad file does not block purging of all other files.
    """
    help = 'Hard delete files soft-deleted beyond FILE_RETENTION_DAYS'

    def handle(self, *args, **options):
        # FILE_RETENTION_DAYS defaults to 90 if not set in settings/environment
        retention_days = getattr(settings, 'FILE_RETENTION_DAYS', 90)
        cutoff = timezone.now() - timedelta(days=retention_days)

        # import inside handle() to avoid circular imports at module load time
        from projects.models import ProjectDocument, TaskAttachment
        from projects.supabase_storage import get_supabase_client

        # SUPABASE_BUCKET defaults to 'solarpms-files' if not overridden
        bucket     = getattr(settings, 'SUPABASE_BUCKET', 'solarpms-files')
        doc_count  = 0
        att_count  = 0

        # Purge soft-deleted project documents older than the retention window
        for doc in ProjectDocument.objects.filter(is_deleted=True, deleted_at__lt=cutoff):
            try:
                client = get_supabase_client()
                client.storage.from_(bucket).remove([doc.supabase_path])
                doc.delete()
                doc_count += 1
            except Exception as exc:
                logger.error('Failed to purge ProjectDocument %s: %s', doc.pk, exc)
                self.stderr.write(f"Failed to purge ProjectDocument {doc.pk}: {exc}")

        # Purge soft-deleted task attachments older than the retention window
        for attach in TaskAttachment.objects.filter(is_deleted=True, deleted_at__lt=cutoff):
            try:
                client = get_supabase_client()
                client.storage.from_(bucket).remove([attach.supabase_path])
                attach.delete()
                att_count += 1
            except Exception as exc:
                logger.error('Failed to purge TaskAttachment %s: %s', attach.pk, exc)
                self.stderr.write(f"Failed to purge TaskAttachment {attach.pk}: {exc}")

        msg = f"Purged {doc_count} project documents, {att_count} task attachments"
        self.stdout.write(msg)
        logger.info(msg)
