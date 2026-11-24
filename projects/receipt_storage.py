"""
FIN-M-B5-2 — the receipt proof file's private storage (FR17). THE ONLY PLACE RECEIPTS
TOUCH SUPABASE. A copy of tender_documents.py's storage half, with its own path prefix.

THREE FUNCTIONS, AND NO OTHERS
------------------------------
  store_receipt_proof(project_id, uploaded_file)
      validate (validate_dc_document, unchanged: PDF, JPG or PNG, not empty, at most
      20 MB, a matching MIME claim and matching leading bytes), then upload to the private
      delivery bucket at receipt_proofs/<project pk>/<uuid4>.<ext>. Returns a StoredProof
      (bucket, path, name, content_type, size); never a URL.
  discard_receipt_proof(bucket, path)
      best-effort removal of an object stored above whose PaymentReceipt row was then
      refused or rolled back. Refuses a path any row records; never raises.
  signed_receipt_proof_url(receipt)
      a link signed now, in the bucket RECORDED on the row, valid for 300 seconds, or None.
      No view calls it yet (B5-3/B5-4 add the link).

The bucket, the client and the signer are the challan documents' (delivery_storage.py),
reused as they are: delivery_bucket() read at call time, so no new setting exists and the
app boots without one. Nothing in delivery_storage.py or design_storage.py is changed.

NO ROLE OR STATUS STRING appears here. Who may store or open a proof is the caller's
question (receipts.py and, later, the B5-3/B5-4 views).
"""
import logging
import uuid
from dataclasses import dataclass

from .delivery_storage import DeliveryStorageError, delivery_bucket, validate_dc_document
from .design_storage import DesignStorageError, _client, get_design_file_url
from .models import PaymentReceipt

logger = logging.getLogger(__name__)

#: How long a signed link works. Minted on click, so it only has to survive the redirect;
#: challan documents and tender documents use the same 300 seconds.
RECEIPT_PROOF_LINK_SECONDS = 300

#: The first path segment for every receipt proof in the delivery bucket. Challan files
#: start with a project id and tender documents with 'program', and every path ends in a
#: fresh uuid4, so the three kinds cannot collide.
RECEIPT_PROOF_PATH_PREFIX = 'receipt_proofs'

RECEIPT_STORAGE_OFF = ('Receipt proofs cannot be stored right now: the private delivery '
                       'bucket is not configured. Nothing was saved — record the receipt '
                       'without the file, or try again later.')


@dataclass(frozen=True)
class StoredProof:
    """Where a proof was stored — the PaymentReceipt row's proof_* fields, in order."""
    bucket: str
    path: str
    name: str
    content_type: str
    size: int


def _receipt_proof_path(project_id, filename):
    """receipt_proofs/<project pk>/<uuid4 hex>.<ext>. The original name never reaches the
    path — only its extension, which validate_dc_document has already limited to pdf, jpg,
    jpeg or png — so user-supplied text cannot choose the storage location."""
    ext = filename.rsplit('.', 1)[-1].lower() if '.' in (filename or '') else ''
    suffix = f'.{ext}' if ext else ''
    return f'{RECEIPT_PROOF_PATH_PREFIX}/{project_id}/{uuid.uuid4().hex}{suffix}'


def store_receipt_proof(project_id, uploaded_file):
    """Validate `uploaded_file` and store it in the private delivery bucket under
    `project_id` (the site's pk). Returns a StoredProof; never a URL.

    Raises DeliveryStorageError, whose str() is the message for the person recording:
      * the bucket is not configured (refused before any check or network call, as
        upload_dc_document does);
      * validate_dc_document refuses the file;
      * the file cannot be read or the upload fails.
    The caller therefore writes no row pointing at an object that does not exist, and
    removes the object with discard_receipt_proof() if its row is then not written.
    """
    bucket = delivery_bucket()
    # With the bucket unset nothing is checked or sent, and the person is told nothing was
    # saved — the same refusal as upload_dc_document and store_tender_document_file.
    if not bucket:
        raise DeliveryStorageError(RECEIPT_STORAGE_OFF)
    content_type = validate_dc_document(uploaded_file)
    path = _receipt_proof_path(project_id, uploaded_file.name)
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
        logger.error('Receipt proof upload failed — %s', exc)
        raise DeliveryStorageError(f'Upload to storage failed: {type(exc).__name__}. '
                                   f'Nothing was saved.')
    return StoredProof(bucket=bucket, path=path, name=uploaded_file.name,
                       content_type=content_type, size=uploaded_file.size)


def discard_receipt_proof(bucket, path):
    """Remove an object store_receipt_proof() stored whose PaymentReceipt row was then
    refused or rolled back. A copy of tender_documents.discard_tender_document_file.

    Refuses to remove anything a PaymentReceipt row records — a recorded proof is part of
    the money trail and is never deleted — and anything outside the delivery bucket.
    Never raises: a failed cleanup leaves an orphan object, which is logged, never a failed
    save. Returns True when the object was removed.
    """
    if not bucket or not path or bucket != delivery_bucket():
        return False
    # A recorded path is evidence someone can still open or audit; only an unrecorded
    # upload is ours to remove. Checked by bucket and path, the pair the signer uses.
    if PaymentReceipt.objects.filter(proof_bucket=bucket, proof_path=path).exists():
        return False
    try:
        _client().storage.from_(bucket).remove([path])
        return True
    except Exception as exc:
        logger.error('Receipt proof cleanup failed for %s/%s — %s', bucket, path, exc)
        return False


def signed_receipt_proof_url(receipt):
    """A link to `receipt`'s proof, signed now for RECEIPT_PROOF_LINK_SECONDS in the bucket
    RECORDED on the row, or None when no proof is recorded or signing fails (the caller
    says the file cannot be opened right now). Access is NOT checked here: the caller asks
    its permission helper first. Signed in the recorded bucket, never today's setting, so a
    proof stored before a bucket rename still opens (the TD-B1a rule)."""
    if not receipt.proof_bucket or not receipt.proof_path:
        return None
    try:
        return get_design_file_url(receipt.proof_bucket, receipt.proof_path,
                                   expires_in=RECEIPT_PROOF_LINK_SECONDS)
    except DesignStorageError as exc:
        logger.error('Receipt proof link failed for %s — %s', receipt.pk, exc)
        return None
