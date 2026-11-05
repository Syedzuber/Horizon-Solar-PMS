"""
TD-B1 — tender-wide documents: the storage wrapper. THE ONLY PLACE THIS FEATURE TOUCHES
SUPABASE (rulings R3, R5, R6).

THREE FUNCTIONS, AND NO OTHERS
------------------------------
  store_tender_document_file(program_id, uploaded_file)
      validate (validate_dc_document, unchanged — R3), then upload to the private
      delivery bucket at program/<program_id>/<uuid4>.<ext> (R6). Returns
      (storage_bucket, storage_path, content_type, size_bytes); never a URL.
  signed_tender_document_url(document)
      a link signed now in the bucket RECORDED on the row, valid for 300 seconds, or
      None (TD-B1a).
  log_tender_document_open(document, user, project)
      one TenderDocumentOpen row (R5) — the feature's own access log, never ActivityLog.

The bucket, the client and the signer are the challan documents' (delivery_storage.py),
reused as they are: delivery_bucket() read at call time, design_storage._client() imported
the same way delivery_storage.py imports it, and design_storage.get_design_file_url() as
the one signer. Nothing in those modules is changed.

NO ROLE OR STATUS STRING appears here (TD-B1 P2e). Who may store, sign or open is the
caller's question, answered by permissions.py's tender document helpers.
"""
import logging
import uuid

from .delivery_storage import DeliveryStorageError, delivery_bucket, validate_dc_document
from .design_storage import DesignStorageError, _client, get_design_file_url
from .models import TenderDocumentOpen

logger = logging.getLogger(__name__)

#: How long a signed link works. Minted on click, so it only has to survive the redirect;
#: the challan documents and site drawings use the same 300 seconds.
TENDER_DOCUMENT_LINK_SECONDS = 300

#: The first path segment for every tender document in the delivery bucket (R6). Challan
#: files there start with a project id instead, and every path ends in a fresh uuid4, so
#: the two kinds can never collide even if a project id were ever spelled "program".
TENDER_DOCUMENT_PATH_PREFIX = 'program'

TENDER_STORAGE_OFF = ('Tender documents cannot be stored right now: the private delivery '
                      'bucket is not configured. Nothing was saved.')


def _tender_document_path(program_id, filename):
    """program/<program_id>/<uuid4 hex>.<ext> (R6). The original name never reaches the
    path — only its extension, which validate_dc_document has already limited to pdf,
    jpg, jpeg or png — so user-supplied text cannot choose the storage location."""
    ext = filename.rsplit('.', 1)[-1].lower() if '.' in (filename or '') else ''
    suffix = f'.{ext}' if ext else ''
    return f'{TENDER_DOCUMENT_PATH_PREFIX}/{program_id}/{uuid.uuid4().hex}{suffix}'


def store_tender_document_file(program_id, uploaded_file):
    """Validate `uploaded_file` and store it in the private delivery bucket under
    `program_id`. Returns (storage_bucket, storage_path, content_type, size_bytes) — the
    TenderDocument row's storage fields; the caller (B2) saves the bucket on the row so
    every later link is signed where the file actually is. Never a URL.

    Raises DeliveryStorageError, whose str() is the message for the uploader:
      * the bucket is not configured (refused before any check or network call, as
        upload_dc_document does);
      * validate_dc_document refuses the file (type, emptiness, 20 MB, MIME claim,
        leading bytes) — R3;
      * the file cannot be read or the upload fails.
    The caller therefore writes no row pointing at an object that does not exist.

    TODO: no discard for an object stored here whose row then fails to save (the
    discard_unrecorded_dc_document pattern). Deferred to TD-B2, which writes the row;
    TD-B1's brief allows these three functions only.
    """
    bucket = delivery_bucket()
    # Same refusal as upload_dc_document (delivery_storage.py:122-124): with the bucket
    # unset nothing is checked or sent, and the uploader is told nothing was saved.
    if not bucket:
        raise DeliveryStorageError(TENDER_STORAGE_OFF)
    content_type = validate_dc_document(uploaded_file)
    path = _tender_document_path(program_id, uploaded_file.name)
    try:
        uploaded_file.seek(0)
        payload = uploaded_file.read()
    except Exception:
        raise DeliveryStorageError(f'{uploaded_file.name}: the file could not be read.')
    try:
        _client().storage.from_(bucket).upload(
            path=path, file=payload, file_options={'content-type': content_type})
    except DesignStorageError as exc:
        # _client() raises this when SUPABASE_URL / SUPABASE_KEY are unset; its message
        # carries nothing secret.
        raise DeliveryStorageError(str(exc))
    except Exception as exc:
        logger.error('Tender document upload failed — %s', exc)
        raise DeliveryStorageError(f'Upload to storage failed: {type(exc).__name__}. '
                                   f'Nothing was saved.')
    return bucket, path, content_type, uploaded_file.size


def signed_tender_document_url(document):
    """A link to `document`'s stored file, signed now for TENDER_DOCUMENT_LINK_SECONDS in
    the bucket RECORDED on the row, or None when no bucket or path is recorded or signing
    fails (the caller tells the user the file cannot be opened right now). Access is NOT
    checked here: the caller has already asked user_can_view_tender_document().

    Pinned to the recorded bucket the way challan documents are: dc_document_url signs
    `attachment.bucket`, never the configured name (delivery_storage.py:178), and returns
    None when no bucket or path is recorded (delivery_storage.py:171-172). Its further
    refusal of a recorded bucket that differs from today's setting (delivery_storage.py:
    173-176) is deliberately NOT copied (TD-B1a): a document stored before a bucket rename
    must still open from the bucket it was stored in. storage_bucket is written only from
    store_tender_document_file's return, which is always the delivery bucket at the time.
    """
    if not document.storage_bucket or not document.storage_path:
        return None
    try:
        return get_design_file_url(document.storage_bucket, document.storage_path,
                                   expires_in=TENDER_DOCUMENT_LINK_SECONDS)
    except DesignStorageError as exc:
        logger.error('Tender document link failed for %s — %s', document.pk, exc)
        return None


def log_tender_document_open(document, user, project):
    """Record one open of `document` by `user` (an auth User) from `project`'s card (R5).
    Returns the TenderDocumentOpen row. `project` may be None for an open from elsewhere.

    Written to TenderDocumentOpen only, never ActivityLog: the site timeline is read by
    everyone on the site, and a Management only document's open must not appear there.
    Call it after the link is minted, so a failed open logs nothing.
    """
    return TenderDocumentOpen.objects.create(document=document, user=user.profile,
                                             project=project)
