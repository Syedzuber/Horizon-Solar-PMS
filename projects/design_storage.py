"""
Private-bucket storage for OPEX design artifacts — surveys (Part 2), CAD and BOQ
attachments (Part 3).

WHY A SECOND MODULE RATHER THAN EXTENDING supabase_storage.py
-------------------------------------------------------------
The existing bucket is PUBLIC and its four call sites build long-lived public URLs by
string concatenation and store them in the database. That bucket, its helper and those
call sites are deliberately untouched here — this module is the correct-by-construction
replacement used by new code only.

Two rules this module enforces that the old path does not:

  1. NO URL IS EVER STORED. `upload_design_file()` returns (bucket, path) and nothing
     else. Callers persist those two strings; a URL is minted per request by
     `get_design_file_url()` and expires.
  2. The bucket is PRIVATE. Verified: fetching an object's public URL returns HTTP 400,
     a signed URL returns 200, and a signed URL stops working once it expires.

CLIENT CREDENTIALS
------------------
`supabase_storage.get_supabase_client()` reads os.environ directly, which is populated
on Railway but NOT from the local .env (that is read by python-decouple into Django
settings). This module reads `settings` instead, so it behaves identically in both
environments. The old helper is left exactly as it is.
"""
import logging
import posixpath
import uuid
import zipfile

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction

# models.py imports nothing from this module (only .units), so this adds no cycle.
from .models import (
    CAD_PDF_ENCRYPTED, CAD_PDF_EXTRACTED, CAD_PDF_FAILED, CAD_PDF_NOT_A_PDF,
    CAD_PDF_TOO_LARGE, DESIGN_FILE_CAD_ZIP, DesignFilePdf,
)

logger = logging.getLogger(__name__)

# Bucket name comes from the environment with a clear default — never inlined at a
# call site. Distinct from SUPABASE_BUCKET, which remains the public bucket.
DESIGN_BUCKET = getattr(settings, 'SUPABASE_DESIGN_BUCKET', 'Horizon-PMS-Design')

# Server-side validation. Deliberately NARROWER than the public bucket's list: design
# artifacts are documents and drawings, never arbitrary images.
ALLOWED_DESIGN_EXTENSIONS = ['pdf', 'doc', 'docx', 'xls', 'xlsx', 'jpg', 'jpeg', 'png',
                             'dwg', 'zip']
MAX_DESIGN_FILE_BYTES = 25 * 1024 * 1024   # 25 MB

# The CANONICAL type per extension. This is what gets STORED on the object in Supabase,
# so it must be the one correct value — never whatever the browser happened to claim.
DESIGN_MIME_TYPE_MAP = {
    'pdf':  'application/pdf',
    'doc':  'application/msword',
    'docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'xls':  'application/vnd.ms-excel',
    'xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    'jpg':  'image/jpeg',
    'jpeg': 'image/jpeg',
    'png':  'image/png',
    'dwg':  'application/acad',
    'zip':  'application/zip',
}

# What we ACCEPT from the browser per extension, which is a much wider set than what we
# store. THERE IS NO SINGLE CORRECT MIME TYPE FOR MOST OF THESE, and the value arrives
# from the client, not from inspecting the bytes:
#
#   .zip  Chrome on Windows sends `application/x-zip-compressed` — it reads the type from
#         the HKCR registry entry, and that is what Windows puts there. Firefox and every
#         browser on macOS/Linux send `application/zip`. Accepting only the latter meant
#         .zip upload was broken for every Windows Chrome user, which is most of them.
#         This is exactly the bug that was reported.
#   .dwg  has no registered IANA type at all; `application/acad`, `image/vnd.dwg`,
#         `application/x-dwg` and friends are all in the wild depending on what CAD
#         software is installed.
#   .doc/.xls  the legacy Office types are frequently reported as their modern
#         counterparts or as octet-stream.
#
# THIS CHECK IS A TYPO GUARD, NOT A SECURITY CONTROL, and widening it costs nothing in
# safety. `content_type` is supplied by the client and is trivially forged — a hostile
# uploader would simply send the expected string. What actually protects the system is
# the extension whitelist above, the size limit, and for archives validate_cad_zip(),
# which OPENS the file and reads its central directory rather than trusting any header.
DESIGN_ACCEPTED_MIME_TYPES = {
    'pdf':  frozenset({'application/pdf'}),
    'doc':  frozenset({'application/msword'}),
    'docx': frozenset({'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                       'application/msword'}),
    'xls':  frozenset({'application/vnd.ms-excel'}),
    'xlsx': frozenset({'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                       'application/vnd.ms-excel'}),
    'jpg':  frozenset({'image/jpeg', 'image/pjpeg'}),
    'jpeg': frozenset({'image/jpeg', 'image/pjpeg'}),
    'png':  frozenset({'image/png', 'image/x-png'}),
    'dwg':  frozenset({'application/acad', 'application/x-acad',
                       'application/autocad_dwg', 'application/dwg', 'application/x-dwg',
                       'application/x-autocad', 'image/vnd.dwg', 'image/x-dwg',
                       'drawing/dwg'}),
    'zip':  frozenset({'application/zip', 'application/x-zip-compressed',
                       'application/x-zip', 'application/x-compressed',
                       'multipart/x-zip'}),
}

# Sent by browsers for anything they cannot classify, and by several for .dwg and .zip.
# Accepted for every extension rather than listed in each set above.
GENERIC_BINARY_MIME_TYPES = frozenset({'application/octet-stream', 'binary/octet-stream'})


class DesignStorageError(Exception):
    """Upload or validation failure. Callers catch this and surface the message —
    it never carries anything secret."""


def _client():
    """Supabase client built from Django settings (not os.environ — see module docstring)."""
    url = settings.SUPABASE_URL
    key = settings.SUPABASE_KEY
    if not url or not key:
        raise DesignStorageError('Supabase is not configured (SUPABASE_URL / SUPABASE_KEY).')
    from supabase import create_client
    return create_client(url, key)


def build_design_path(project_id, kind, filename):
    """
    Storage path convention:

        {project_id}/{kind}/{uuid4-hex}.{ext}

    The project identifier makes objects greppable and lets a whole site be located or
    purged by prefix. `kind` ('survey' now; 'cad'/'boq' in Part 3) separates artifact
    types. A fresh uuid4 is the filename, so two uploads of the same original filename
    can NEVER collide and a replaced survey does not overwrite its predecessor — the
    old object stays addressable by the path already recorded on any prior row.
    The original filename is not used in the path at all, so user-supplied text can
    never influence the storage location.
    """
    ext = filename.rsplit('.', 1)[-1].lower() if '.' in (filename or '') else ''
    suffix = f'.{ext}' if ext else ''
    return f'{project_id}/{kind}/{uuid.uuid4().hex}{suffix}'


def validate_design_file(file_obj):
    """Server-side validation — extension, size, and MIME consistency. Raises
    DesignStorageError. Called by upload_design_file(), so it cannot be skipped by a
    caller that forgets; the form layer is a convenience, not the enforcement point."""
    name = getattr(file_obj, 'name', '') or ''
    ext = name.rsplit('.', 1)[-1].lower() if '.' in name else ''
    if ext not in ALLOWED_DESIGN_EXTENSIONS:
        raise DesignStorageError(
            f'Unsupported file type (.{ext or "none"}). Allowed: '
            f'{", ".join(ALLOWED_DESIGN_EXTENSIONS)}.'
        )
    size = getattr(file_obj, 'size', 0) or 0
    if size > MAX_DESIGN_FILE_BYTES:
        raise DesignStorageError(
            f'File is {size / 1024 / 1024:.1f} MB — the limit is '
            f'{MAX_DESIGN_FILE_BYTES // 1024 // 1024} MB.'
        )
    if size == 0:
        raise DesignStorageError('File is empty.')

    accepted = DESIGN_ACCEPTED_MIME_TYPES.get(ext, frozenset())
    actual = (getattr(file_obj, 'content_type', '') or '').split(';')[0].strip().lower()
    # An empty content_type is accepted: some clients send none at all, and refusing on a
    # missing header would block a valid file over something the uploader cannot control.
    if accepted and actual and actual not in accepted | GENERIC_BINARY_MIME_TYPES:
        raise DesignStorageError(
            f'This does not look like a .{ext} file — the browser described it as '
            f'"{actual}". If the file really is a .{ext}, re-save it and try again.')

    return ext


# ---------------------------------------------------------------------------
# CAD ARCHIVE VALIDATION (Part 8)
# ---------------------------------------------------------------------------
# CAD used to arrive as two files and the system could see both. It now arrives as one
# zip, which hides its own contents: without validation here the QC reviewer downloads
# the archive and only THEN discovers the DWG is missing, having already queued the
# package for review. So the archive is opened and checked at upload, and its listing is
# recorded on the row so the contents are visible without downloading anything.
#
# THE ARCHIVE IS NEVER EXTRACTED TO DISK. Everything below reads the zip CENTRAL
# DIRECTORY — the index at the end of the file — which gives names and declared sizes
# without decompressing a single byte.

#: Largest cad_zip accepted, as it arrives over the wire.
MAX_CAD_ZIP_BYTES = 25 * 1024 * 1024              # 25 MB — same as any design file

#: Largest TOTAL UNCOMPRESSED size the archive may declare.
#
# This is the zip-bomb guard and it is the reason the check exists at all. A 25 MB zip of
# highly compressible data can declare hundreds of gigabytes uncompressed; anything that
# later decompresses it — a preview, a virus scanner, the reviewer's own machine —
# exhausts memory or disk. 200 MB is comfortably above a real CAD package (a heavy DWG
# plus drawing sheets runs to tens of MB) and far below anything that could hurt.
MAX_CAD_ZIP_UNCOMPRESSED_BYTES = 200 * 1024 * 1024   # 200 MB

#: Ceiling on the number of entries, so a listing cannot itself become the payload.
MAX_CAD_ZIP_ENTRIES = 500

#: What a CAD archive must contain, by extension. Case-insensitive.
REQUIRED_CAD_ZIP_EXTENSIONS = ('pdf', 'dwg')


def validate_cad_zip(file_obj):
    """Validate a cad_zip upload and return its listing.

    Returns ``[{'name': str, 'size': int}, ...]`` — `size` is the entry's UNCOMPRESSED
    size as declared in the central directory. Raises DesignStorageError with a message
    naming exactly what is wrong.

    Checks, in order:
      1. it is a readable zip archive (not merely a file named .zip)
      2. it declares no more than MAX_CAD_ZIP_ENTRIES entries
      3. its total uncompressed size is within MAX_CAD_ZIP_UNCOMPRESSED_BYTES
      4. it contains at least one .pdf AND at least one .dwg

    The size check runs BEFORE the content check on purpose: a hostile archive should be
    rejected on the cheapest possible evidence, and summing the central directory never
    decompresses anything.
    """
    import zipfile

    size = getattr(file_obj, 'size', 0) or 0
    if size > MAX_CAD_ZIP_BYTES:
        raise DesignStorageError(
            f'Archive is {size / 1024 / 1024:.1f} MB — the limit is '
            f'{MAX_CAD_ZIP_BYTES // 1024 // 1024} MB.')

    # Read from the start regardless of what an earlier validator left the pointer on.
    try:
        file_obj.seek(0)
    except (AttributeError, OSError):
        pass

    try:
        archive = zipfile.ZipFile(file_obj)
        # testzip() would decompress every entry; namelist() reads the index only.
        infos = archive.infolist()
    except zipfile.BadZipFile:
        raise DesignStorageError(
            'That file is not a readable zip archive. Upload the CAD package as a '
            'single .zip containing the PDF and the DWG.')
    except Exception:
        raise DesignStorageError('The zip archive could not be read.')
    finally:
        try:
            file_obj.seek(0)
        except (AttributeError, OSError):
            pass

    if len(infos) > MAX_CAD_ZIP_ENTRIES:
        raise DesignStorageError(
            f'Archive declares {len(infos)} entries — the limit is '
            f'{MAX_CAD_ZIP_ENTRIES}.')

    total_uncompressed = sum(i.file_size for i in infos)
    if total_uncompressed > MAX_CAD_ZIP_UNCOMPRESSED_BYTES:
        raise DesignStorageError(
            f'Archive expands to {total_uncompressed / 1024 / 1024:.0f} MB uncompressed '
            f'— the limit is {MAX_CAD_ZIP_UNCOMPRESSED_BYTES // 1024 // 1024} MB. '
            f'Upload only the drawings for this site.')

    # Directory entries carry a trailing slash and a zero size; they are not contents.
    entries = [i for i in infos if not i.is_dir()]
    if not entries:
        raise DesignStorageError('The zip archive is empty.')

    present = {name.rsplit('.', 1)[-1].lower()
               for name in (i.filename for i in entries) if '.' in name}
    missing = [ext for ext in REQUIRED_CAD_ZIP_EXTENSIONS if ext not in present]
    if missing:
        raise DesignStorageError(
            'The CAD archive is missing '
            + ' and '.join(f'a .{ext.upper()} file' for ext in missing)
            + '. It must contain at least one PDF and at least one DWG.')

    return [{'name': i.filename, 'size': i.file_size} for i in entries]


def upload_design_file(file_obj, path):
    """
    Upload one file to the PRIVATE design bucket.

    Returns (bucket, path) — never a URL. Raises DesignStorageError on validation or
    upload failure, so the caller can abort its transaction before writing any row and
    never leave a database record pointing at an object that does not exist.
    """
    ext = validate_design_file(file_obj)

    try:
        file_obj.seek(0)
        payload = file_obj.read()
    except Exception as exc:
        raise DesignStorageError(f'Could not read the uploaded file: {exc}')

    try:
        _client().storage.from_(DESIGN_BUCKET).upload(
            path=path,
            file=payload,
            file_options={'content-type': DESIGN_MIME_TYPE_MAP.get(ext, 'application/octet-stream')},
        )
    except Exception as exc:
        # Message is surfaced to the user, so keep it short and non-leaky.
        raise DesignStorageError(f'Upload to storage failed: {type(exc).__name__}')

    return DESIGN_BUCKET, path


def delete_design_objects(objects):
    """
    Delete stored objects from the PRIVATE design bucket. Used by
    `teardown_opex_test_data` (E5) so tearing down test rows does not leave the bucket
    holding orphaned surveys, CAD files and BOQ attachments.

    `objects` is an iterable of (bucket, path) pairs. Returns a list of
    (bucket, path, ok, error) tuples — ONE PER OBJECT, and it never raises. Deleting
    test data must not become impossible because storage is unreachable, so every
    failure is reported back to the caller to print rather than propagated.

    Objects are removed ONE AT A TIME rather than in a batch, so a single failure is
    attributed to the object that caused it instead of losing the whole batch.

    THE PUBLIC BUCKET IS UNREACHABLE FROM HERE. Any pair naming a bucket other than
    DESIGN_BUCKET is refused, not deleted — a corrupt or hand-edited `bucket` column
    must never be able to point a delete at `SUPABASE_BUCKET`.
    """
    results = []
    client = None
    for bucket, path in objects:
        if not bucket or not path:
            continue
        if bucket != DESIGN_BUCKET:
            results.append((bucket, path, False,
                            f'refused: not the design bucket ({DESIGN_BUCKET!r})'))
            continue
        try:
            if client is None:
                client = _client()
            client.storage.from_(bucket).remove([path])
            results.append((bucket, path, True, ''))
        except Exception as exc:
            results.append((bucket, path, False, f'{type(exc).__name__}: {exc}'))
    return results


def get_design_file_url(bucket, path, expires_in=3600):
    """
    Mint a signed URL for a stored object, at request time.

    Nothing calls this at write time and no result is ever persisted — that is the
    whole point of the private bucket. Returns None when there is no file recorded, so
    templates can simply test the value.
    """
    if not bucket or not path:
        return None
    try:
        res = _client().storage.from_(bucket).create_signed_url(path, expires_in)
    except Exception as exc:
        raise DesignStorageError(f'Could not generate a link: {type(exc).__name__}')
    # storage3 has used several key spellings across versions.
    return res.get('signedURL') or res.get('signedUrl') or res.get('signed_url')


def download_design_file(bucket, path):
    """
    Read one stored object back from the PRIVATE design bucket and return its bytes.

    The only reader in this module: everything else uploads, deletes or signs. Used by the
    `extract_cad_pdfs` command, which has no upload in hand and must fetch the zip.

    THE PUBLIC BUCKET IS UNREACHABLE FROM HERE, on the same rule as
    delete_design_objects(): a pair naming any bucket but DESIGN_BUCKET is refused, so a
    hand-edited `bucket` column cannot point a read at `SUPABASE_BUCKET`.
    """
    if not bucket or not path:
        raise DesignStorageError('No stored object is recorded.')
    if bucket != DESIGN_BUCKET:
        raise DesignStorageError(f'Refused: {bucket!r} is not the design bucket.')
    try:
        return _client().storage.from_(bucket).download(path)
    except Exception as exc:
        raise DesignStorageError(f'Download from storage failed: {type(exc).__name__}')


# ---------------------------------------------------------------------------
# CAD PDF EXTRACTION (DR-1)
# ---------------------------------------------------------------------------
# A CAD zip's drawings are visible by name (archive_listing) but not openable without
# downloading the whole archive. Each `.pdf` entry is therefore copied out ONCE into its
# own private object and recorded as a DesignFilePdf row, so a later screen can sign a
# link to one drawing. Nothing user-visible reads these rows yet.
#
# Same rules as validate_cad_zip(): the archive is read in memory, one entry at a time,
# and NEVER EXTRACTED TO DISK. Non-PDF entries are never decompressed at all.

#: How far cad_pdf_source() walks before refusing. A carry happens once per rework loop,
#: so a real chain is a handful of rows; this only has to outlast every real one.
MAX_CARRY_CHAIN = 100

#: The first bytes of every PDF. A `.pdf` NAME is the uploader's claim about an entry;
#: these bytes are the entry's own evidence, and only they decide that it is a PDF.
PDF_MAGIC = b'%PDF-'

#: An entry name longer than the column cannot be recorded, so it cannot be made
#: idempotent either; such an entry is logged and left alone rather than uploaded.
_ENTRY_NAME_MAX = DesignFilePdf._meta.get_field('entry_name').max_length


def cad_pdf_source(design_file):
    """The DesignFile whose stored object `design_file` addresses: the end of its
    `carried_forward_from` chain. EVERY reader and writer of DesignFilePdf goes through
    this, because a carried copy shares its original's object and so its PDFs.

    Bounded and cycle-checked. _carry_forward_artifacts() always points a copy at an OLDER
    row, so neither a cycle nor a chain past MAX_CARRY_CHAIN can arise from the product;
    either means a row was edited by hand, and is refused rather than followed.
    """
    seen = {design_file.pk}
    current = design_file
    for _hop in range(MAX_CARRY_CHAIN):
        parent_id = current.carried_forward_from_id
        if parent_id is None:
            return current
        if parent_id in seen:
            raise DesignStorageError(
                f'DesignFile {design_file.pk}: its carried_forward_from chain is a cycle.')
        seen.add(parent_id)
        current = current.carried_forward_from
    raise DesignStorageError(
        f'DesignFile {design_file.pk}: carried_forward_from chain is longer than '
        f'{MAX_CARRY_CHAIN}.')


def cad_zip_pdf_entries(design_file):
    """Entry names ending `.pdf` in the zip's upload-time listing — the candidates.

    The listing was read from the same object at upload, so it names the same entries the
    archive holds. It can say what to TRY; only extract_cad_pdfs(), reading the bytes, can
    say whether each one is really a PDF.
    """
    return [entry['name'] for entry in (design_file.archive_listing or [])
            if str(entry.get('name', '')).lower().endswith('.pdf')]


def unrecorded_cad_pdf_entries(source):
    """Candidates from cad_zip_pdf_entries() that have no DesignFilePdf row yet.

    `source` must already be resolved through cad_pdf_source(). This is the derived
    extraction state: no candidates is "none found", none unrecorded is "done", anything
    else is still to do. One query, over the source's own rows.
    """
    recorded = set(source.extracted_pdfs.values_list('entry_name', flat=True))
    return [name for name in cad_zip_pdf_entries(source) if name not in recorded]


def _record_cad_pdf(source, info, outcome, bucket='', path=''):
    """Write one DesignFilePdf row, or return None if another run already wrote it.

    The savepoint is what makes the IntegrityError survivable inside a caller's
    transaction. Losing that race after an upload leaves the object just uploaded with no
    row: an orphan, never deleted (docs/DESIGN_MODULE_DEFERRED.md), which is the price of
    never holding a row lock across a network call.
    """
    try:
        with transaction.atomic():
            return DesignFilePdf.objects.create(
                design_file=source, entry_name=info.filename,
                display_name=posixpath.basename(info.filename),
                bucket=bucket, path=path, size_bytes=info.file_size, outcome=outcome)
    except IntegrityError:
        logger.warning('extract_cad_pdfs: %r in DesignFile %s was recorded by another run; '
                       'object %r is orphaned.', info.filename, source.pk, path or None)
        return None


def extract_cad_pdfs(design_file, file_obj):
    """Copy every `.pdf` entry of a CAD zip into its own private object. Returns the
    DesignFilePdf rows written by this call (empty when there was nothing new to do).

    `design_file` may be a carried copy; the rows always attach to cad_pdf_source(). The
    caller passes the zip as an open file object (the upload still in memory, or the
    command's download wrapped in BytesIO), and it is rewound here before reading.

    PER ENTRY, ONE OUTCOME, RECORDED ONCE. An entry is a candidate only if its name ends
    `.pdf`, case-insensitively. Then, in order:

        encrypted   the zip entry is password-protected: never opened
        too_large   declared size over MAX_DESIGN_FILE_BYTES: never decompressed
        failed      the entry could not be read (corrupt data, CRC mismatch)
        not_a_pdf   the bytes do not start with %PDF-
        extracted   uploaded through upload_design_file(), content-type application/pdf

    An entry that already has a row is skipped, so a second call is a no-op.

    A STORAGE FAILURE IS NOT AN OUTCOME. It is the one failure that may succeed next time,
    so it records nothing for that entry and raises DesignStorageError: rows already
    written stay, and the next run picks up the rest. An unreadable archive or an invalid
    carry chain raises the same way, before anything is written.
    """
    source = cad_pdf_source(design_file)
    if source.kind != DESIGN_FILE_CAD_ZIP:
        raise DesignStorageError(f'DesignFile {source.pk} is {source.kind}, not a CAD zip.')

    recorded = set(source.extracted_pdfs.values_list('entry_name', flat=True))
    # Derived from the ZIP's path, so a site's PDFs sit beside the archive they came from:
    # {project_id}/cad_zip/{zip-uuid}/pdf/{uuid}.pdf. The uuid filename is build_design_path's
    # rule: the entry's own name never reaches the storage path.
    stem = posixpath.splitext(source.path)[0]

    try:
        file_obj.seek(0)
        archive = zipfile.ZipFile(file_obj)
        infos = archive.infolist()
    except Exception as exc:
        raise DesignStorageError(f'The CAD zip could not be read: {type(exc).__name__}')

    written = []
    for info in infos:
        name = info.filename
        if info.is_dir() or not name.lower().endswith('.pdf') or name in recorded:
            continue
        if len(name) > _ENTRY_NAME_MAX:
            logger.warning('extract_cad_pdfs: DesignFile %s entry name is %d characters, '
                           'over %d; left alone.', source.pk, len(name), _ENTRY_NAME_MAX)
            continue

        # Bit 0 is set for every encrypted entry, "strong" encryption (bit 6) included.
        # Checked before open(), which would raise asking for a password nobody has.
        if info.flag_bits & 0x1:
            row = _record_cad_pdf(source, info, CAD_PDF_ENCRYPTED)
        elif info.file_size > MAX_DESIGN_FILE_BYTES:
            # The declared size, from the central directory: refused without decompressing.
            row = _record_cad_pdf(source, info, CAD_PDF_TOO_LARGE)
        else:
            try:
                with archive.open(info) as entry:
                    head = entry.read(len(PDF_MAGIC))
                    data = head + entry.read() if head == PDF_MAGIC else None
            except Exception as exc:
                # The bytes are already in memory, so nothing here is transient: a corrupt
                # entry will be corrupt next time too, and is recorded rather than retried.
                logger.warning('extract_cad_pdfs: DesignFile %s entry %r unreadable: %s',
                               source.pk, name, exc)
                row = _record_cad_pdf(source, info, CAD_PDF_FAILED)
            else:
                if data is None:
                    row = _record_cad_pdf(source, info, CAD_PDF_NOT_A_PDF)
                else:
                    pdf = SimpleUploadedFile(posixpath.basename(name), data,
                                             content_type='application/pdf')
                    # Raises DesignStorageError on a storage failure, deliberately not
                    # caught: see "A STORAGE FAILURE IS NOT AN OUTCOME" above.
                    bucket, path = upload_design_file(
                        pdf, f'{stem}/pdf/{uuid.uuid4().hex}.pdf')
                    del data, pdf
                    row = _record_cad_pdf(source, info, CAD_PDF_EXTRACTED, bucket, path)
        if row is not None:
            written.append(row)
            recorded.add(name)
    return written
