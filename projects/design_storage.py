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
import uuid

from django.conf import settings

# Bucket name comes from the environment with a clear default — never inlined at a
# call site. Distinct from SUPABASE_BUCKET, which remains the public bucket.
DESIGN_BUCKET = getattr(settings, 'SUPABASE_DESIGN_BUCKET', 'Horizon-PMS-Design')

# Server-side validation. Deliberately NARROWER than the public bucket's list: design
# artifacts are documents and drawings, never arbitrary images.
ALLOWED_DESIGN_EXTENSIONS = ['pdf', 'doc', 'docx', 'xls', 'xlsx', 'jpg', 'jpeg', 'png',
                             'dwg', 'zip']
MAX_DESIGN_FILE_BYTES = 25 * 1024 * 1024   # 25 MB

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

    expected = DESIGN_MIME_TYPE_MAP.get(ext, '')
    actual = (getattr(file_obj, 'content_type', '') or '').split(';')[0].strip()
    # Same tolerance as the existing uploader: browsers send octet-stream for many of
    # these types, so that is accepted rather than rejected.
    if expected and actual and actual not in (expected, 'application/octet-stream'):
        raise DesignStorageError('File content type does not match its extension.')

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
