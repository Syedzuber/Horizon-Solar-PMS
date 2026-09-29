"""
Contractor bills 4a-1 — the bill PDF's private storage (D-A39, D-A40).

A contractor's bill is a money document, so it goes to a PRIVATE bucket and is read only
through a signed link that expires, never a stored or public URL. This module is the
design_storage.py pattern applied to one more bucket: the same Supabase client, the same
path convention (build_design_path) and the same signer (get_design_file_url, reused
as-is — it takes the bucket as an argument and pins nothing).

THE BUCKET IS OFF UNTIL IT IS SET. settings.SUPABASE_BILLS_BUCKET defaults to empty, and
it is read at CALL time, never at import, so the app boots and every page works without
it. With it empty:

  * upload_bill_pdf() refuses, with BILL_STORAGE_OFF, before any network call;
  * bill_pdf_url() returns None, and the page says the file is unavailable;
  * approvals.create_approval_request() refuses a bill whose PDF is not recorded in the
    configured bucket — nothing equals an empty name, so it fails closed as well.

Photos with a bill are ordinary approval attachments in the existing public bucket
(D-A37, D-A42); they do not come through here.
"""
import logging

from django.conf import settings

from .design_storage import (
    DesignStorageError, _client, build_design_path, get_design_file_url,
)
from .models import ApprovalRoundSnapshot, ContractorBillDetail

logger = logging.getLogger(__name__)

#: How long a signed bill link works. Links are minted per page render, so a forwarded
#: or bookmarked link dies quickly; someone who leaves the page open reloads it.
BILL_LINK_SECONDS = 900

#: The same 20 MB ceiling as every other upload (views._validate_upload_file).
BILL_PDF_MAX_BYTES = 20 * 1024 * 1024

#: What the browser may call a PDF. octet-stream is what several browsers send for a file
#: they cannot classify; the %PDF- header check below is what actually looks at the bytes.
BILL_PDF_MIME_TYPES = frozenset({'application/pdf', 'application/octet-stream'})

BILL_STORAGE_OFF = ('Bill PDFs cannot be stored right now: the private bills bucket is not '
                    'configured. Nothing was saved.')

#: What the raise page says BEFORE anything is submitted while the bucket is unset (4a-2).
#: BILL_STORAGE_OFF ends "Nothing was saved", which reads wrongly on a page nobody has
#: submitted yet; a POST is still refused with BILL_STORAGE_OFF itself.
BILL_STORAGE_NOT_READY = ('Contractor bills cannot be raised yet: the private store for '
                          'bill PDFs is not configured.')


class BillStorageError(Exception):
    """A bill PDF was refused or could not be stored. str(exc) is the message for the
    person uploading; it never carries anything secret."""


def bills_bucket():
    """The configured private bills bucket, or '' when bill storage is off."""
    return (getattr(settings, 'SUPABASE_BILLS_BUCKET', '') or '').strip()


def validate_bill_pdf(file_obj):
    """Refuse anything but a real PDF: a .pdf name, not empty, at most 20 MB, a PDF or
    generic MIME type, and bytes that start with %PDF-. Raises BillStorageError; touches
    no storage, so a caller can refuse the submission before anything is uploaded."""
    name = getattr(file_obj, 'name', '') or ''
    if not name.lower().endswith('.pdf'):
        raise BillStorageError('The bill must be a PDF.')
    size = getattr(file_obj, 'size', 0) or 0
    if size == 0:
        raise BillStorageError('The bill PDF is empty.')
    if size > BILL_PDF_MAX_BYTES:
        raise BillStorageError(
            f'The bill PDF is {size / 1024 / 1024:.1f} MB — the limit is '
            f'{BILL_PDF_MAX_BYTES // 1024 // 1024} MB.')
    mime = (getattr(file_obj, 'content_type', '') or '').split(';')[0].strip().lower()
    if mime and mime not in BILL_PDF_MIME_TYPES:
        raise BillStorageError('The bill must be a PDF.')
    # The header, because a name and a MIME type are both whatever the client says.
    try:
        file_obj.seek(0)
        head = file_obj.read(5)
        file_obj.seek(0)
    except Exception:
        raise BillStorageError('The bill PDF could not be read.')
    if head != b'%PDF-':
        raise BillStorageError('That file is not a PDF, although its name ends in .pdf.')


def upload_bill_pdf(file_obj, project):
    """Store one bill PDF in the private bills bucket, under the bill's project.

    Returns {file_name, bucket, path, file_size_kb} — the `pdf` of approvals.ContractorBill.
    Never a URL. Raises BillStorageError when storage is off, the file is refused, or the
    upload fails, so the caller writes no row pointing at an object that does not exist.
    """
    bucket = bills_bucket()
    if not bucket:
        raise BillStorageError(BILL_STORAGE_OFF)
    validate_bill_pdf(file_obj)
    path = build_design_path(project.project_id, 'bill', file_obj.name)
    try:
        file_obj.seek(0)
        payload = file_obj.read()
    except Exception:
        raise BillStorageError('The bill PDF could not be read.')
    try:
        _client().storage.from_(bucket).upload(
            path=path, file=payload, file_options={'content-type': 'application/pdf'})
    except DesignStorageError as exc:
        raise BillStorageError(str(exc))
    except Exception as exc:
        logger.error('Bill PDF upload failed — %s', exc)
        raise BillStorageError(f'Upload to storage failed: {type(exc).__name__}. '
                               f'Nothing was saved.')
    return {'file_name': file_obj.name, 'bucket': bucket, 'path': path,
            'file_size_kb': max(1, file_obj.size // 1024)}


def discard_unrecorded_bill_pdf(stored):
    """Remove a PDF that upload_bill_pdf() stored for a bill the chokepoint then refused.
    Refuses to remove anything a ContractorBillDetail records — a recorded bill PDF is
    never deleted — or anything outside the bills bucket. Never raises: a failed cleanup
    leaves an orphan object, which is logged, never a failed page. Returns True when the
    object was removed.

    4b-1: nor anything a round snapshot records. Once a resubmit replaces a bill's PDF,
    the old one is on no ContractorBillDetail — only in its round's schema-3 snapshot —
    and it must stay reachable from there."""
    bucket, path = (stored or {}).get('bucket'), (stored or {}).get('path')
    if not bucket or not path or bucket != bills_bucket():
        return False
    if ContractorBillDetail.objects.filter(pdf_bucket=bucket, pdf_path=path).exists():
        return False
    # Every round snapshot whose bill block names this very file (schema 3 only; other
    # snapshots have no `bill` key). Asked only when no live bill records it.
    if ApprovalRoundSnapshot.objects.filter(snapshot__bill__pdf__bucket=bucket,
                                            snapshot__bill__pdf__path=path).exists():
        return False
    try:
        _client().storage.from_(bucket).remove([path])
        return True
    except Exception as exc:
        logger.error('Bill PDF cleanup failed for %s/%s — %s', bucket, path, exc)
        return False


def bill_pdf_url(bucket, path, expires_in=BILL_LINK_SECONDS):
    """A signed link to a stored bill PDF, minted now, or None.

    None — and the page says "file unavailable" — when bill storage is off, when no file
    is recorded, when the recorded bucket is not the configured bills bucket (a link is
    never signed into any other bucket, the public one included), or when signing fails.
    Takes bucket and path rather than a row, so a round snapshot's PDF (schema 3) signs
    the same way as the live detail's."""
    configured = bills_bucket()
    if not configured or not bucket or not path:
        return None
    if bucket != configured:
        logger.warning('Bill PDF recorded in %r, not the bills bucket %r — no link.',
                       bucket, configured)
        return None
    try:
        return get_design_file_url(bucket, path, expires_in)
    except DesignStorageError as exc:
        logger.error('Bill PDF link failed for %s/%s — %s', bucket, path, exc)
        return None
