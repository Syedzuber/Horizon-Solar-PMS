"""
DC-B2 — the vendor's challan documents' private storage.

A challan document is read by Site Engineers, so it goes to a PRIVATE bucket and is read
only through a signed link minted when someone clicks it, never a stored or public URL.
This module is bill_storage.py applied to one more bucket: the same Supabase client, the
same path convention (build_design_path) and the same signer (get_design_file_url, reused
as-is — it takes the bucket as an argument and pins nothing).

THE BUCKET IS OFF UNTIL IT IS SET. settings.SUPABASE_DELIVERY_BUCKET defaults to empty,
and it is read at CALL time, never at import, so the app boots and every page works
without it. With it empty:

  * upload_dc_document() refuses, with DELIVERY_STORAGE_OFF, before any network call —
    and create_delivery_challan refuses the whole challan when a file was chosen, so a
    file is never dropped silently;
  * dc_document_url() returns None, and the open view says the file is unavailable.

A challan created with no file never comes through here.
"""
import logging

from django.conf import settings

from .design_storage import (
    DesignStorageError, _client, build_design_path, get_design_file_url,
)
from .models import DeliveryChallanAttachment

logger = logging.getLogger(__name__)

#: How long a signed link works. Minted on click, so the link only has to survive the
#: redirect; a forwarded or bookmarked one dies within minutes.
DC_DOCUMENT_LINK_SECONDS = 300

#: The same 20 MB ceiling as every other upload (views._validate_upload_file).
DC_DOCUMENT_MAX_BYTES = 20 * 1024 * 1024

#: At most this many documents on one challan; more refuses the whole create.
DC_DOCUMENTS_MAX_FILES = 10

#: Storage path segment, between the project id and the uuid filename.
DC_DOCUMENT_PATH_KIND = 'challan'

#: Per extension: the type stored on the object, what the browser may claim, and the
#: bytes the file must start with. The claim is a typo guard (any client can forge it);
#: the leading bytes are what actually look at the file.
_GENERIC_MIME = frozenset({'application/octet-stream', 'binary/octet-stream'})
DC_DOCUMENT_TYPES = {
    'pdf':  ('application/pdf', frozenset({'application/pdf'}), b'%PDF-'),
    'jpg':  ('image/jpeg', frozenset({'image/jpeg', 'image/pjpeg'}), b'\xff\xd8\xff'),
    'jpeg': ('image/jpeg', frozenset({'image/jpeg', 'image/pjpeg'}), b'\xff\xd8\xff'),
    'png':  ('image/png', frozenset({'image/png', 'image/x-png'}), b'\x89PNG\r\n\x1a\n'),
}

DELIVERY_STORAGE_OFF = ('Challan documents cannot be stored right now: the private '
                        'delivery bucket is not configured. Nothing was saved — remove '
                        'the attached files to create the challan without them.')


class DeliveryStorageError(Exception):
    """A challan document was refused or could not be stored. str(exc) is the message
    for the person uploading; it never carries anything secret."""


def delivery_bucket():
    """The configured private delivery bucket, or '' when document storage is off."""
    return (getattr(settings, 'SUPABASE_DELIVERY_BUCKET', '') or '').strip()


def validate_dc_document(file_obj):
    """Refuse anything but a real PDF, JPEG or PNG: an allowed extension, not empty, at
    most 20 MB, a matching or generic MIME type, and bytes that start the way that type
    starts. Returns the canonical MIME type. Raises DeliveryStorageError; touches no
    storage, so a caller can refuse every file before any is uploaded."""
    name = getattr(file_obj, 'name', '') or ''
    ext = name.rsplit('.', 1)[-1].lower() if '.' in name else ''
    if ext not in DC_DOCUMENT_TYPES:
        raise DeliveryStorageError(f'{name or "A file"}: must be a PDF, JPG or PNG.')
    stored_mime, accepted, magic = DC_DOCUMENT_TYPES[ext]
    size = getattr(file_obj, 'size', 0) or 0
    if size == 0:
        raise DeliveryStorageError(f'{name}: the file is empty.')
    if size > DC_DOCUMENT_MAX_BYTES:
        raise DeliveryStorageError(
            f'{name}: {size / 1024 / 1024:.1f} MB — the limit is '
            f'{DC_DOCUMENT_MAX_BYTES // 1024 // 1024} MB.')
    # An empty claim is accepted, as design_storage does: some clients send none.
    claimed = (getattr(file_obj, 'content_type', '') or '').split(';')[0].strip().lower()
    if claimed and claimed not in accepted | _GENERIC_MIME:
        raise DeliveryStorageError(f'{name}: this does not look like a .{ext} file.')
    try:
        file_obj.seek(0)
        head = file_obj.read(len(magic))
        file_obj.seek(0)
    except Exception:
        raise DeliveryStorageError(f'{name}: the file could not be read.')
    if head != magic:
        raise DeliveryStorageError(
            f'{name}: the contents are not a .{ext} file, although the name says so.')
    return stored_mime


def upload_dc_document(file_obj, project):
    """Store one challan document in the private delivery bucket, under the project.

    Returns {original_name, bucket, path, size, mime} — the attachment row's fields.
    Never a URL. Raises DeliveryStorageError when storage is off, the file is refused,
    or the upload fails, so the caller writes no row pointing at a missing object.
    """
    bucket = delivery_bucket()
    if not bucket:
        raise DeliveryStorageError(DELIVERY_STORAGE_OFF)
    mime = validate_dc_document(file_obj)
    path = build_design_path(project.project_id, DC_DOCUMENT_PATH_KIND, file_obj.name)
    try:
        file_obj.seek(0)
        payload = file_obj.read()
    except Exception:
        raise DeliveryStorageError(f'{file_obj.name}: the file could not be read.')
    try:
        _client().storage.from_(bucket).upload(
            path=path, file=payload, file_options={'content-type': mime})
    except DesignStorageError as exc:
        raise DeliveryStorageError(str(exc))
    except Exception as exc:
        logger.error('Challan document upload failed — %s', exc)
        raise DeliveryStorageError(f'Upload to storage failed: {type(exc).__name__}. '
                                   f'Nothing was saved.')
    return {'original_name': file_obj.name, 'bucket': bucket, 'path': path,
            'size': file_obj.size, 'mime': mime}


def discard_unrecorded_dc_document(stored):
    """Remove a document upload_dc_document() stored for a challan whose create then
    failed. Refuses to remove anything an attachment row records — a recorded document
    is never deleted, withdrawn or not — or anything outside the delivery bucket. Never
    raises: a failed cleanup leaves an orphan object, which is logged, never a failed
    page. Returns True when the object was removed."""
    bucket, path = (stored or {}).get('bucket'), (stored or {}).get('path')
    if not bucket or not path or bucket != delivery_bucket():
        return False
    if DeliveryChallanAttachment.objects.filter(bucket=bucket, path=path).exists():
        return False
    try:
        _client().storage.from_(bucket).remove([path])
        return True
    except Exception as exc:
        logger.error('Challan document cleanup failed for %s/%s — %s', bucket, path, exc)
        return False


def dc_document_url(attachment, expires_in=DC_DOCUMENT_LINK_SECONDS):
    """A signed link to a stored challan document, minted now, or None.

    None when storage is off, when the recorded bucket is not the configured delivery
    bucket (a link is never signed into any other bucket, the public one included), or
    when signing fails."""
    configured = delivery_bucket()
    if not configured or not attachment.bucket or not attachment.path:
        return None
    if attachment.bucket != configured:
        logger.warning('Challan document %s recorded in %r, not the delivery bucket %r '
                       '— no link.', attachment.pk, attachment.bucket, configured)
        return None
    try:
        return get_design_file_url(attachment.bucket, attachment.path, expires_in)
    except DesignStorageError as exc:
        logger.error('Challan document link failed for %s — %s', attachment.pk, exc)
        return None
