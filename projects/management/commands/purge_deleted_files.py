import logging
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Hard delete files soft-deleted beyond FILE_RETENTION_DAYS'

    def handle(self, *args, **options):
        retention_days = getattr(settings, 'FILE_RETENTION_DAYS', 90)
        cutoff = timezone.now() - timedelta(days=retention_days)

        from projects.models import ProjectDocument, TaskAttachment
        from projects.supabase_storage import get_supabase_client

        bucket     = getattr(settings, 'SUPABASE_BUCKET', 'solarpms-files')
        doc_count  = 0
        att_count  = 0

        for doc in ProjectDocument.objects.filter(is_deleted=True, deleted_at__lt=cutoff):
            try:
                client = get_supabase_client()
                client.storage.from_(bucket).remove([doc.supabase_path])
                doc.delete()
                doc_count += 1
            except Exception as exc:
                logger.error('Failed to purge ProjectDocument %s: %s', doc.pk, exc)
                self.stderr.write(f"Failed to purge ProjectDocument {doc.pk}: {exc}")

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
