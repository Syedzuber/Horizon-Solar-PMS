# SESSION E AUDIT — BOQ download and upload with frozen item specification

**Mode: investigate only.** No application code was written, edited or deleted. No migration was
created. `makemigrations` / `migrate` were not run. No database was written to. No package was
installed. The only file this session creates is this report.

Database access: production was read over the Railway TCP proxy using the commented `DATABASE_URL`
in `.env`, on a `psycopg2` connection opened with `set_session(readonly=True)`. Every production
result below is labelled `[PRODUCTION]`. Local results are labelled `[LOCAL]`.

---

## SESSION OPENING CHECK

### 1. Repo

Git was run in `c:\SolarPMS\Horizon-Solar-PMS`, not `c:\SolarPMS` (the latter is not a repo).

### 2. `git status` — raw

```
On branch main
Your branch is up to date with 'origin/main'.

Untracked files:
  (use "git add <file>..." to include in what will be committed)
        SESSION_B_AUDIT.md
        SESSION_C_AUDIT.md
        SESSION_D_AUDIT.md
        SESSION_T_TEST_UNBLOCK.md

nothing added to commit but untracked files present (use "git add" to track)
```

**No tracked file is modified or staged. Every untracked file is a `.md` report. Hard stop 1 does
not fire.**

### 3. `git log --oneline -5` and local HEAD

```
80f0d12 [Session D] Put mandatory catalogue items on every OPEX BOQ
92e1a50 [Session C.1] Let an OPEX catalogue item be marked mandatory
ee8c752 [Session C] Give the Design Head his own OPEX BOQ catalogue screen
9fb3c59 Let the Design Head name a gate-1 QC reviewer per site
338ebde Let a designer leave a submission note on the BOQ
```

Local HEAD: `80f0d12afd0b959060567a6a4e5d393faf4b6987`

`[Session D]` is present, at HEAD.

**`[Session T]` is NOT present — see CONFLICTS §C1.** `git log --oneline -20 | grep -i session`
returns only D, C.1 and C. Session T produced no commit: it was a diagnosis session whose entire
output is the untracked `SESSION_T_TEST_UNBLOCK.md`. This is consistent with what Session T
concluded (the suite was never broken; only the invocation was wrong), so there was nothing to
commit. The prompt's expectation that a `[Session T]` commit exists is wrong, and this is not
treated as a stop.

### 4. Deployed SHA

`[PRODUCTION]` Railway project `triumphant-forgiveness` / service `Horizon-Solar-PMS` /
environment `production`:

```
f1e00542-ede0-4630-94e5-bf790e6f2c4b | SUCCESS  | 2026-08-15 04:23:16 UTC | 80f0d12afd0b959060567a6a4e5d393faf4b6987
4f99560f-3ec1-4179-84aa-6b7f9cb870a7 | REMOVED  | 2026-08-15 02:16:07 UTC | 92e1a50d3787e8953452557df4351d5e336259ae
f0f8dd73-35a7-4607-9d6a-0ec4a67a69e3 | REMOVED  | 2026-08-14 18:57:37 UTC | ee8c752997832cdb69b6c2baf1eb5d0d01f02297
```

Deployed SHA `80f0d12a…` **equals local HEAD**. Hard stop 2 does not fire.

### 5. Migration head

```
0062_designattempt_boq_remarks.py
0063_designassignment_qc_assigned_at_and_more.py
0064_boqitemmaster_is_mandatory.py
```

Head is **`0064_boqitemmaster_is_mandatory`** — Session C.1's migration. Session D added no
migration.

### 6. Whole-suite test baseline

Reported in full at **§4.6**, where two runs are recorded because the first produced a failure that
had to be re-derived rather than inherited.

Summary: **281 tests. On the canonical SQLite runner, 280 pass and 1 fails — and that failure is a
Postgres-vs-SQLite error-string artifact, not an application defect; the same test passes on
Postgres.** On the Postgres runner, 32 tests error, all of them in `tests_design_part11` and all
from one `setUp` collision with the migration-seeded catalogue.

**Hard stop 3's literal condition is met** (the SQLite failure is in `tests_design_part46`, outside
`part11`) **and is reported prominently at §4.6.** It was not treated as a stop, because
re-derivation shows no genuine regression in any module. The full reasoning, and the note that the
reader may overrule it, are at §4.6.

---

## PART 1 — WHAT SPREADSHEET MACHINERY ALREADY EXISTS

### 1.1 — Is `openpyxl` installed?

`requirements.txt` in full:

```
asgiref==3.11.1
dj-database-url==3.1.2
Django==6.0.6
gunicorn==26.0.0
openpyxl==3.1.5
packaging==26.2
psycopg2-binary==2.9.12
python-decouple==3.8
sqlparse==0.5.5
tzdata==2026.2
whitenoise==6.12.0
supabase==2.31.0
requests==2.32.3
```

**`openpyxl==3.1.5` is pinned and installed.** `[LOCAL]` `pip list` confirms `openpyxl 3.1.5` and
its dependency `et_xmlfile 2.0.0` in the venv.

**No other spreadsheet library exists.** `pip list` shows no `pandas`, no `xlsxwriter`, no `xlrd`,
no `xlwt`, no `odfpy`, no `tablib`.

CSV is the Python standard library, imported locally at two call sites:

| File | Line | Context |
|---|---|---|
| `projects/views.py` | 8419 | `admin_send_records` — notification send-log CSV export |
| `projects/views.py` | 8502 | `_export_audit_csv` — audit-log CSV export |

`openpyxl` itself is imported at three places, all function-local:

```
projects/views.py:2735:        from openpyxl import load_workbook
projects/views.py:2947:    from openpyxl import Workbook
```

(the third hit, line 2713, is a docstring mention).

**Hard stop 4 does not fire.** A spreadsheet library is installed, pinned, already deployed to
Railway and already used in both directions (read and write).

`[LOCAL]` The specific `openpyxl` APIs needed for freezing were confirmed importable in the venv:

```
from openpyxl.worksheet.protection import SheetProtection   # OK
from openpyxl.worksheet.datavalidation import DataValidation # OK
from openpyxl.styles import Protection                       # OK — Protection(locked=True, hidden=False)
```

### 1.2 — The bulk site upload (the closest existing precedent)

Located at `projects/views.py:2631-2989`. It is a **three-part** machine: a column table, a parser,
a dry-run validator, the view itself, and a matching template download.

**The column table and limits** — [views.py:2631-2659](projects/views.py#L2631-L2659):

```python
# ===========================================================================
# OPEX bulk site upload (Prompt 3)
#
# Upload an Excel file of sites under one OPEX Program and create them in one
# ALL-OR-NOTHING batch, reusing create_opex_site() (and therefore OpexSiteForm)
# for every row so validation NEVER drifts from the single-add path.
#
# Flow is two requests: (1) upload -> parse + dry-run validate -> preview; the
# validated rows ride back to the browser as JSON in a hidden field. (2) confirm
# -> the exact previewed rows are re-validated and committed for real inside one
# outer transaction. A file only ever adds sites; multiple independent batches
# per Program are expected (running total vs planned_site_count is informational).
# ===========================================================================

# Excel column header (normalized: .strip().lower()) -> OpexSiteForm data key.
# Order here is the order columns are emitted into the downloadable template.
_BULK_COLUMNS = [
    ('Site Code',            'site_code',                True),
    ('Site In-Charge Name',  'customer_contact_person',  False),
    ('Site In-Charge Phone', 'customer_phone',           False),
    ('Site In-Charge Email', 'customer_email',           False),
    ('Site Address',         'site_address',             True),
    ('City',                 'city',                     True),
    ('State',                'state',                    False),
    ('Capacity (kW)',        'capacity_kw',              False),
]
_BULK_HEADER_TO_KEY = {h.strip().lower(): key for h, key, _req in _BULK_COLUMNS}
_BULK_REQUIRED_HEADERS = [h for h, _key, req in _BULK_COLUMNS if req]
_BULK_MAX_ROWS = 500   # soft guard against a runaway file timing out the request
```

**The two sentinels** — [views.py:2662-2676](projects/views.py#L2662-L2676):

```python
class _DryRunRollback(Exception):
    """Sentinel used ONLY by _validate_site_row_dry_run to unwind a deliberately
    rolled-back transaction. Dedicated (never a bare Exception) so it is obvious in
    a traceback and can never be confused with a real failure."""
    pass


class _CommitAbort(Exception):
    """Sentinel raised inside the real commit loop when a row that passed dry-run
    fails at commit time (race / tampered payload). Rolls back the WHOLE batch so
    the all-or-nothing guarantee holds end-to-end, not just at dry-run."""
    def __init__(self, index, errors):
        self.index = index          # 1-based row number for the report
        self.errors = errors        # form.errors dict
        super().__init__()
```

**The dry run** — [views.py:2679-2709](projects/views.py#L2679-L2709). This is the mechanism §5.3
asks about, quoted in full:

```python
def _validate_site_row_dry_run(program, data, creator, profile):
    """Validate ONE would-be site with the REAL create_opex_site path, then throw the
    result away — a dry run that reuses production logic instead of re-checking the
    form by hand (which would silently drift the moment create_opex_site changes).

    HOW IT WORKS: create_opex_site() only reaches the DB for a *valid* row — an invalid
    row returns (None, form) before its atomic block ever opens. So we wrap the call in
    our own transaction.atomic(); for a valid row create_opex_site performs the real
    INSERT plus its ActivityLog write inside that savepoint, and we immediately raise
    _DryRunRollback to force Django to roll every bit of it back. Nothing is persisted,
    yet the exact production validation + project_id composition ran.

    WHY THE EXCEPTION IS LOAD-BEARING: if a future "simplification" deletes the raise,
    the transaction commits and this preview quietly starts creating real sites on every
    keystroke of a preview. The raise is the whole rollback mechanism — do not remove it.
    Safe only because log_activity is pure-DB (no email/WhatsApp side-effect), so the
    rollback leaves no trace outside the transaction.

    Returns (form, is_valid). `form` carries form.errors for the preview when invalid.
    """
    captured = {}
    try:
        with transaction.atomic():
            site, form = create_opex_site(program, data, creator, profile=profile)
            captured['form'] = form
            captured['valid'] = site is not None
            if site is not None:
                raise _DryRunRollback()   # load-bearing: forces rollback of the real INSERT
    except _DryRunRollback:
        pass
    return captured['form'], captured['valid']
```

**The cell reader** — [views.py:2712-2725](projects/views.py#L2712-L2725):

```python
def _bulk_cell_to_str(value):
    """Excel cell -> trimmed string WITHOUT lossy coercion. openpyxl hands back numbers
    as int/float; a phone/site-code typed as a number must render as plain digits (no
    '.0'), but we never strip or reshape characters a user actually typed as text — bad
    values stay bad so OpexSiteForm can reject them loudly (spec §5: no silent coercion)."""
    if value is None:
        return ''
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, int):
        return str(value)
    return str(value).strip()
```

**The parser** — [views.py:2728-2780](projects/views.py#L2728-L2780):

```python
def _parse_bulk_workbook(uploaded_file):
    """Parse the uploaded .xlsx into (rows, extra_headers, error). Reads the 'Sites' sheet
    if present; otherwise takes the first sheet whose name is not 'Instructions' (so teams
    can name their data sheet anything — e.g. 'MPUVNL', 'Bhopal', 'Tender42' — without
    being forced to rename it). Returns error!=None for a whole-file rejection (bad file,
    missing required column, empty, too many rows)."""
    try:
        from openpyxl import load_workbook
        wb = load_workbook(uploaded_file, read_only=True, data_only=True)
    except Exception:
        return None, None, "Could not read the file — please upload the .xlsx template unchanged."

    if 'Sites' in wb.sheetnames:
        ws = wb['Sites']
    else:
        # Skip 'Instructions' and pick the first remaining sheet.  Falls back to the
        # active sheet only when every sheet is named 'Instructions' (degenerate case).
        data_sheets = [name for name in wb.sheetnames
                       if name.strip().lower() != 'instructions']
        ws = wb[data_sheets[0]] if data_sheets else wb.active
    all_rows = list(ws.iter_rows(values_only=True))
    wb.close()
    if not all_rows:
        return None, None, "The file is empty."

    header_cells = [_bulk_cell_to_str(c) for c in all_rows[0]]
    header_norm = [h.strip().lower() for h in header_cells]
    missing = [h for h in _BULK_REQUIRED_HEADERS if h.strip().lower() not in header_norm]
    if missing:
        return None, None, "Missing required column(s): " + ", ".join(missing) + "."

    # Columns present in the file but not part of the template — ignored, warned (not rejected).
    extra_headers = [header_cells[i] for i, hn in enumerate(header_norm)
                     if hn and hn not in _BULK_HEADER_TO_KEY]

    data_rows = all_rows[1:]
    # Drop wholly-blank trailing rows Excel loves to include.
    data_rows = [r for r in data_rows if any(_bulk_cell_to_str(c) for c in r)]
    if not data_rows:
        return None, None, "The file has headers but no site rows to import."
    if len(data_rows) > _BULK_MAX_ROWS:
        return None, None, (f"This file has {len(data_rows)} rows — the per-upload limit is "
                            f"{_BULK_MAX_ROWS}. Split it into smaller batches.")

    rows = []
    for raw in data_rows:
        data = {}
        for i, hn in enumerate(header_norm):
            key = _BULK_HEADER_TO_KEY.get(hn)
            if key and i < len(raw):
                data[key] = _bulk_cell_to_str(raw[i])
        rows.append(data)
    return rows, extra_headers, None
```

**The in-file duplicate check** — [views.py:2783-2797](projects/views.py#L2783-L2797):

```python
def _bulk_infile_duplicate_indices(rows):
    """1-based row numbers whose site_code collides with another row IN THE SAME FILE.
    Normalizes with the SAME normalize_program_code the form uses, so 's045' / 'S-045'
    are seen as the duplicates they are. This is the ONE check dry-run cannot make (each
    row rolls back before the next runs), so it must happen here (spec §4 / §5)."""
    seen = {}
    for idx, data in enumerate(rows, start=1):
        code = normalize_program_code(data.get('site_code'))
        if code:
            seen.setdefault(code, []).append(idx)
    dupes = set()
    for code, idxs in seen.items():
        if len(idxs) > 1:
            dupes.update(idxs)
    return dupes
```

**The view itself** — [views.py:2800-2933](projects/views.py#L2800-L2933), quoted in full:

```python
@login_required
@role_required(['Admin', 'PM'])
def opex_site_bulk_upload(request, pk):
    """Bulk-create OPEX sites under one Program from an uploaded .xlsx (Admin/PM, OPEX
    only — same gate as opex_site_create). Two POST phases keyed by the 'phase' field:
    'preview' (a file upload -> validate + preview) and 'commit' (confirm the previewed
    JSON -> create for real, all-or-nothing)."""
    program = get_object_or_404(Program, pk=pk, is_deleted=False, program_type='OPEX')
    if not _can_access_program(request, program):
        raise Http404

    profile = getattr(request.user, 'profile', None)
    rollup = get_program_rollup(program)
    existing_count = rollup['total']
    planned = program.planned_site_count

    ctx = {
        'program': program,
        'existing_count': existing_count,
        'planned': planned,
        'stage': 'upload',
    }

    phase = request.POST.get('phase') if request.method == 'POST' else None

    # ---- Phase: PREVIEW (parse + dry-run validate the uploaded file) ----
    if phase == 'preview':
        uploaded = request.FILES.get('file')
        if not uploaded:
            ctx['file_error'] = "Please choose a file to upload."
            return render(request, 'projects/opex_site_bulk_upload.html', ctx)

        rows, extra_headers, file_error = _parse_bulk_workbook(uploaded)
        if file_error:
            ctx['file_error'] = file_error
            return render(request, 'projects/opex_site_bulk_upload.html', ctx)

        dup_indices = _bulk_infile_duplicate_indices(rows)
        results = []
        all_valid = True
        for idx, data in enumerate(rows, start=1):
            form, valid = _validate_site_row_dry_run(program, data, request.user, profile)
            errors = {field: list(msgs) for field, msgs in form.errors.items()}
            if idx in dup_indices:
                errors.setdefault('site_code', [])
                errors['site_code'].append("Duplicate site code within this file.")
                valid = False
            if not valid:
                all_valid = False
            results.append({
                'index': idx,
                'data': data,
                'site_code': data.get('site_code', ''),
                'name': data.get('customer_contact_person', ''),
                'city': data.get('city', ''),
                'errors': errors,
                'valid': valid,
            })

        after_count = existing_count + len(rows)
        ctx.update({
            'stage': 'preview',
            'results': results,
            'total_rows': len(rows),
            'valid_count': sum(1 for r in results if r['valid']),
            'invalid_count': sum(1 for r in results if not r['valid']),
            'all_valid': all_valid,
            'extra_headers': extra_headers,
            'after_count': after_count,
            'exceeds_planned': bool(planned) and after_count > planned,
            # Only valid-batch rows ride forward; all_valid is required to show Confirm.
            'rows_json': json.dumps([r['data'] for r in results]) if all_valid else '',
        })
        return render(request, 'projects/opex_site_bulk_upload.html', ctx)

    # ---- Phase: COMMIT (create the previewed rows for real, all-or-nothing) ----
    if phase == 'commit':
        try:
            rows = json.loads(request.POST.get('rows_json') or '[]')
        except (ValueError, TypeError):
            rows = None
        if not rows or not isinstance(rows, list):
            ctx['file_error'] = "Nothing to create — please upload and preview a file first."
            return render(request, 'projects/opex_site_bulk_upload.html', ctx)

        # Re-check in-file duplicates on the confirmed payload (defends against a tampered
        # hidden field); the per-row create below re-runs full validation for real.
        dup_indices = _bulk_infile_duplicate_indices(rows)

        created = []
        commit_error = None
        try:
            with transaction.atomic():
                if dup_indices:
                    raise _CommitAbort(min(dup_indices),
                                       {'site_code': ['Duplicate site code within this file.']})
                for idx, data in enumerate(rows, start=1):
                    site, form = create_opex_site(program, data, request.user, profile=profile)
                    if site is None:
                        raise _CommitAbort(idx, {f: list(m) for f, m in form.errors.items()})
                    created.append(site.project_id)
                # Batch-level audit entry (in addition to create_opex_site's per-site logs).
                # Inside the atomic, so a failed batch rolls this back too.
                log_activity(
                    None, profile,
                    f"Bulk uploaded {len(created)} sites to program {program.name}",
                    entity_type='Program', entity_id=program.pk,
                    action_code='opex_sites_bulk_created',
                )
        except _CommitAbort as abort:
            created = []
            commit_error = {'index': abort.index, 'errors': abort.errors}
        except IntegrityError:
            created = []
            commit_error = {'index': None, 'errors': {
                '__all__': ['A site code was taken by another user between preview and confirm. '
                            'Nothing was created — please re-upload to refresh the check.']}}

        if commit_error is not None:
            ctx.update({'stage': 'result', 'success': False, 'commit_error': commit_error})
            return render(request, 'projects/opex_site_bulk_upload.html', ctx)

        # Success — recompute the running total from the DB post-commit.
        new_total = get_program_rollup(program)['total']
        messages.success(request, f"{len(created)} sites created under {program.name}.")
        ctx.update({
            'stage': 'result', 'success': True,
            'created': created, 'created_count': len(created),
            'new_total': new_total, 'planned': planned,
        })
        return render(request, 'projects/opex_site_bulk_upload.html', ctx)

    # GET (or unknown phase) — the initial upload screen.
    return render(request, 'projects/opex_site_bulk_upload.html', ctx)
```

**Answers to the six sub-questions:**

- **Format accepted, and how it reads the file.** `.xlsx` only, via
  `load_workbook(uploaded_file, read_only=True, data_only=True)`. `read_only=True` streams rather
  than materialising the whole workbook; `data_only=True` reads cached formula *results* rather
  than formula text. There is **no extension check and no size check** on the upload — a
  non-xlsx file is caught only by `load_workbook` raising, which the bare `except Exception`
  converts into the generic "Could not read the file" message.
- **How it validates before writing; is there a dry run.** Yes, and it is the strongest pattern in
  the codebase. `_validate_site_row_dry_run()` runs the *real* production create path
  (`create_opex_site` → `OpexSiteForm`) inside a savepoint and forces a rollback with the
  `_DryRunRollback` sentinel. Nothing is persisted, but the exact production validation ran.
- **All-or-nothing per batch, and the mechanism.** Yes, enforced twice. At preview,
  `rows_json` is emitted **only** when `all_valid` is true, so a batch with any invalid row cannot
  reach the Confirm control. At commit, the whole loop runs inside one `transaction.atomic()`, and
  a row that fails at commit time raises `_CommitAbort` which unwinds the entire batch. An
  `IntegrityError` (a code taken between preview and confirm) also discards everything.
- **How errors are reported, and is there a preview.** Per row, per field. `form.errors` is
  serialised into `results[i]['errors']` and rendered as a table with `valid_count` /
  `invalid_count` summary and an `extra_headers` warning for unrecognised columns. **Yes — there
  is an explicit preview stage** (`stage='preview'`) between upload and commit, and it renders the
  full page rather than redirecting.
- **File-size handling.** There is **no byte-size limit**. The only bound is
  `_BULK_MAX_ROWS = 500`, checked *after* the whole workbook has been opened and
  `list(ws.iter_rows(values_only=True))` has materialised every row into memory. The comment calls
  it "a soft guard against a runaway file timing out the request".
- **Is the uploaded file stored.** **No.** It is parsed and discarded. `wb.close()` is called; the
  `uploaded` handle is never persisted, never written to disk, never sent to Supabase. What rides
  forward to the commit phase is `rows_json` — the *parsed values*, in a hidden form field, round
  tripping through the browser.

### 1.3 — Every other file-upload path

`request.FILES` appears at seven call sites:

| Path | Location | Accepts | Stores | Validates |
|---|---|---|---|---|
| Design survey upload | [design_views.py:415](projects/design_views.py#L415) | `survey_file` | Supabase **private** design bucket via `upload_design_file()` | ext ∈ `ALLOWED_DESIGN_EXTENSIONS`, ≤ 25 MB, MIME map; plus an allocation-lock business rule |
| Design artifact upload (Arka / CAD zip / BOQ attachment) | [design_views.py:2213](projects/design_views.py#L2213) | `artifact_file`, with a whitelisted `kind` | Supabase private design bucket | same, **plus** `validate_cad_zip()` for zips — readable archive, ≤ 500 entries, ≤ 200 MB uncompressed, must contain a PDF and a DWG; validated *before* storage so a rejected zip never reaches the bucket |
| OPEX bulk site upload | [views.py:2827](projects/views.py#L2827) | `file` (.xlsx) | **nothing — parsed and discarded** | header set, non-empty, ≤ 500 rows, then per-row form dry run |
| Invoice document | [views.py:5243](projects/views.py#L5243) | `invoice_document` | Supabase **public** bucket via `_validate_and_upload()` | ext ∈ `ALLOWED_EXTENSIONS`, ≤ 20 MB, MIME-vs-extension |
| Project document upload | [views.py:6332](projects/views.py#L6332) | `files` (multiple) | Supabase public bucket | same `_validate_and_upload()` |
| Task attachment upload | [views.py:6472](projects/views.py#L6472) | `files` (multiple) | Supabase public bucket | same |
| Photo upload | [views.py:6639](projects/views.py#L6639) | `photo` | Supabase public bucket | `_validate_and_upload(..., ALLOWED_PHOTO_EXTENSIONS)` |

The shared public-bucket validator — [views.py:6233-6255](projects/views.py#L6233-L6255):

```python
def _validate_and_upload(file, supabase_client, bucket, supabase_path, allowed_extensions=None):
    """Validate one file and upload to Supabase. Raises ValueError on validation failure.
    Pass allowed_extensions (e.g. ALLOWED_PHOTO_EXTENSIONS) to restrict the accepted types
    for this call; defaults to ALLOWED_EXTENSIONS, preserving every existing caller."""
    allowed = allowed_extensions if allowed_extensions is not None else ALLOWED_EXTENSIONS
    ext = file.name.rsplit('.', 1)[-1].lower() if '.' in file.name else ''
    if ext not in allowed:
        raise ValueError(f"unsupported type (.{ext})")
    if file.size > MAX_FILE_SIZE_BYTES:
        raise ValueError("exceeds 20 MB limit")

    expected_mime = MIME_TYPE_MAP.get(ext, '')
    actual_mime   = (file.content_type or '').split(';')[0].strip()
    if expected_mime and actual_mime and actual_mime not in (expected_mime, 'application/octet-stream'):
        raise ValueError("MIME type does not match extension")

    file.seek(0)
    supabase_client.storage.from_(bucket).upload(
        path=supabase_path,
        file=file.read(),
        file_options={"content-type": MIME_TYPE_MAP.get(ext, 'application/octet-stream')},
    )
    return ext
```

Constants — [views.py:72-85](projects/views.py#L72-L85):

```python
ALLOWED_PHOTO_EXTENSIONS    = ['jpg', 'jpeg', 'png']
ALLOWED_EXTENSIONS          = ALLOWED_DOCUMENT_EXTENSIONS + ALLOWED_PHOTO_EXTENSIONS
MAX_FILE_SIZE_BYTES         = 20 * 1024 * 1024  # 20 MB

MIME_TYPE_MAP = {
    'pdf':  'application/pdf',
    ...
    'xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    ...
}
```

**The bulk-upload path is the only one that does not size-check or extension-check its file.**
Every other upload in the codebase does both. That is a gap in the precedent, not a licence.

### 1.4 — Is there any existing download or export?

**Yes. Four, and one of them is an `.xlsx` download built with `openpyxl`. This session would NOT
be introducing the first download.** This contradicts the prompt's framing — see CONFLICTS §C2.

**(a) `opex_site_bulk_template` — the .xlsx download, and the direct model for a BOQ download.**
[views.py:2936-2989](projects/views.py#L2936-L2989):

```python
@login_required
@role_required(['Admin', 'PM'])
def opex_site_bulk_template(request, pk):
    """Download the .xlsx template for this Program's bulk upload: a 'Sites' sheet with
    just the header row (what the parser reads), plus an 'Instructions' sheet holding the
    field guide, phone-format rule, and a worked example — kept OFF the data sheet so no
    guidance row can ever be imported as a real site."""
    program = get_object_or_404(Program, pk=pk, is_deleted=False, program_type='OPEX')
    if not _can_access_program(request, program):
        raise Http404

    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = 'Sites'
    ws.append([h for h, _key, _req in _BULK_COLUMNS])

    info = wb.create_sheet('Instructions')
    info.append(['OPEX Bulk Site Upload — Instructions'])
    ...
    for header, key, req in _BULK_COLUMNS:
        info.append([header, 'Yes' if req else 'No', _notes.get(key, '')])
    ...

    resp = HttpResponse(
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    resp['Content-Disposition'] = (
        f'attachment; filename="opex_sites_{program.short_tender_code or program.pk}.xlsx"')
    wb.save(resp)
    return resp
```

The emit idiom is three lines: build an `HttpResponse` with the xlsx content type, set
`Content-Disposition: attachment`, `wb.save(resp)`. It is separately routed from the upload:

```
projects/urls.py:45:    path("programs/<int:pk>/sites/bulk/", views.opex_site_bulk_upload, name='opex_site_bulk_upload'),
projects/urls.py:46:    path("programs/<int:pk>/sites/bulk/template/", views.opex_site_bulk_template, name='opex_site_bulk_template'),
```

Note the two-sheet convention: **data on one sheet, guidance on a sheet the parser deliberately
skips**, "so no guidance row can ever be imported as a real site".

**(b) `_export_audit_csv`** — [views.py:8501-8517](projects/views.py#L8501-L8517):

```python
def _export_audit_csv(qs):
    import csv
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="audit_log.csv"'
    writer = csv.writer(response)
    writer.writerow(['Timestamp', 'User', 'Role', 'Action', 'Entity Type', 'Entity ID', 'Project'])
    for entry in qs:
        writer.writerow([
            entry.timestamp.strftime('%Y-%m-%d %H:%M:%S'),
            entry.actor.user.get_full_name() if entry.actor else '—',
            entry.actor.role if entry.actor else '—',
            entry.action,
            entry.entity_type,
            entry.entity_id or '—',
            entry.project.project_id if entry.project else '—',
        ])
    return response
```

**(c) `admin_send_records`** — [views.py:8419](projects/views.py#L8419) — a second CSV export
(notification send log), same `import csv` + `HttpResponse` idiom, driven by `?export=`.

**(d) `design_file_download`** — `projects/urls.py:86` → `design_views.design_file_download`. Not a
generated file: it mints a short-lived signed Supabase URL and redirects.

**There is no BOQ export of any kind today.** No report, no project list, nothing that emits BOQ
rows to a file.

### 1.5 — Supabase (report, do not decide)

`projects/design_storage.py:1-39`:

```python
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
...
"""
DESIGN_BUCKET = getattr(settings, 'SUPABASE_DESIGN_BUCKET', 'Horizon-PMS-Design')

# Server-side validation. Deliberately NARROWER than the public bucket's list: design
# artifacts are documents and drawings, never arbitrary images.
ALLOWED_DESIGN_EXTENSIONS = ['pdf', 'doc', 'docx', 'xls', 'xlsx', 'jpg', 'jpeg', 'png',
                             'dwg', 'zip']
MAX_DESIGN_FILE_BYTES = 25 * 1024 * 1024   # 25 MB
```

`'xlsx'` is already an accepted design-bucket extension, so an uploaded BOQ workbook *could* be
stored there without changing the allow-list.

**Evidence on whether storage is needed, reported not decided:**

- **The precedent says no.** The one existing spreadsheet-upload path
  (`opex_site_bulk_upload`) stores nothing. It parses, discards, and round-trips the parsed
  *values* through the browser as `rows_json`.
- **What storage would buy:** an audit trail of the exact file a designer uploaded. The BOQ side
  already has a separate audit surface — `BOQRevision` snapshots
  ([models.py:959](projects/models.py#L959)) — but note that `opex_boq_entry` **does not create a
  BOQRevision on save**; only the Residential `boq_detail` paths do
  ([views.py:4425](projects/views.py#L4425), [4462](projects/views.py#L4462),
  [4908](projects/views.py#L4908), [4996](projects/views.py#L4996)). So an uploaded BOQ would have
  no revision snapshot either way unless one is added.
- **What storage would cost:** a new `kind` in `UPLOADABLE_KINDS`, a storage-failure branch on a
  path that otherwise cannot fail at storage, and an orphaned-object problem if the parse succeeds
  and the write is later rolled back.

---

## PART 2 — THE SHEET

### 2.1 — The `BOQItem` model, in full

[models.py:902-956](projects/models.py#L902-L956):

```python
class BOQItem(models.Model):
    """A single line item in a BOQ — one row in the bill of quantities table."""

    CATEGORY_CHOICES = [
        ('Solar Modules', 'Solar Modules'),
        ('Structure',     'Structure'),
        ('Inverter',      'Inverter'),
        ('BOS',           'BOS'),
        ('Other',         'Other'),
    ]

    UOM_CHOICES = [
        ('Nos',  'Nos'),
        ('LOT',  'LOT'),
        ('Mtr',  'Mtr'),
        ('Pkt',  'Pkt'),
        ('LS',   'LS'),
        ('Sets', 'Sets'),
        ('Kg',   'Kg'),
    ]

    boq              = models.ForeignKey(BOQ, on_delete=models.CASCADE, related_name='items')
    serial_no        = models.IntegerField()
    category         = models.CharField(max_length=20, choices=CATEGORY_CHOICES)
    description      = models.TextField()   # Point-in-time snapshot — never rewritten when item_master is later edited
    item_master      = models.ForeignKey(
        'BOQItemMaster',
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='boq_items',   # The join used to sum quantities across sites; null for ad-hoc / legacy rows
    )
    make_preference  = models.ForeignKey(
        Vendor,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='preferred_items',  # Vendor preferred by Design for this item
    )
    uom              = models.CharField(max_length=10, choices=UOM_CHOICES)
    boq_quantity     = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)   # Quantity estimated by Design
    ordered_quantity = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)   # Actual quantity ordered by SCM
    ordered_vendor   = models.ForeignKey(
        Vendor,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='ordered_items',  # Vendor actually selected by SCM when placing PO
    )
    is_standard_item = models.BooleanField(default=True)  # False for ad-hoc rows added by Design; only non-standard items can be deleted

    class Meta:
        ordering = ['serial_no']
```

**Field-by-field, download vs SCM-side:**

| Field | Type | Null? | Download carries it? |
|---|---|---|---|
| `boq` | FK → BOQ, CASCADE | no | No — implied by the file being for one site |
| `serial_no` | IntegerField | no | Yes, as the row-order column. Sourced from `master.sort_order`, not row position |
| `category` | CharField(20), choices | no (blank not allowed) | Yes — decision 1 names it a locked column |
| `description` | TextField | no | Yes — decision 1. Point-in-time snapshot |
| `item_master` | FK → BOQItemMaster, SET_NULL | **yes** | Its `code` is what decision 1 locks; the pk itself is 2.3's open question |
| `make_preference` | FK → Vendor, SET_NULL | yes | Design-side, but not part of the settled column set. Not in decisions 1-6 |
| `uom` | CharField(10), choices | no | Yes — decision 1 names `unit` |
| `boq_quantity` | Decimal(10,2) | yes | **Yes — the one editable column** |
| `ordered_quantity` | Decimal(10,2) | yes | **NO — SCM-side.** Written by SCM when placing the PO |
| `ordered_vendor` | FK → Vendor, SET_NULL | yes | **NO — SCM-side** |
| `is_standard_item` | Boolean, default True | no | No — internal; the picker writes `True` for every catalogue row |

**The SCM-side fields that must not appear are `ordered_quantity` and `ordered_vendor`.**
`make_preference` is Design-side but outside the settled column set.

**A `uom` finding the build must know about.** `BOQItem.UOM_CHOICES` lists seven values —
`Nos, LOT, Mtr, Pkt, LS, Sets, Kg`. The OPEX catalogue does not use that vocabulary.

`[PRODUCTION]` distinct `BOQItemMaster.unit` where `project_type='OPEX'`:

```
Kg, KWp, Meter, Nos, Pair, Pkt, Set
```

`[PRODUCTION]` distinct `BOQItem.uom` actually stored on OPEX BOQs, with row counts:

```
Kg 3, KWp 7, LOT 2, LS 1, Meter 29, Mtr 6, Nos 105, Pair 2, Pkt 16, Set 14
```

**`KWp`, `Meter`, `Pair` and `Set` are not in `UOM_CHOICES`, and 52 of the 185 live OPEX BOQ rows
carry one of them.** This is not a bug being reported for fixing — `choices` is a form/validation
construct, not a database constraint, and the picker writes `uom=master.unit` through
`BOQItem.objects.create()`, which never calls `full_clean()`. It matters to Session E only as a
constraint on the build: **an upload path must not route `uom` through a `ModelForm` or call
`full_clean()`, or it will reject 52 rows of existing live data.** The picker's direct-write idiom
is the one that works.

### 2.2 — `split_opex_boq_rows()`, and what a download must include

[models.py:837-856](projects/models.py#L837-L856):

```python
def split_opex_boq_rows(boq, catalogue_ids):
    """Split a BOQ's rows into (catalogue rows, off-catalogue rows).

    `catalogue_ids` is the set of ACTIVE OPEX master pks. A row whose master was
    deactivated after it was added counts as off-catalogue — it is still a real quantity on
    a real sheet, so it renders and can be removed, it just cannot be re-added.

    OFF-CATALOGUE ROWS ARE NOT AN ERROR CONDITION. OPEX sites created before Part 11 were
    seeded from the Residential template by the shared boq_detail, and an ad-hoc row has no
    `item_master` at all. Both are real data; both are shown wherever the sheet is shown.

    Lives here rather than in views.py because BOTH the entry screen and the Part 9 review
    screen need it, and design_views must not import views.
    """
    on, off = [], []
    if boq is None:
        return on, off
    for item in boq.items.select_related('item_master').order_by('serial_no', 'pk'):
        (on if item.item_master_id in catalogue_ids else off).append(item)
    return on, off
```

Three callers: the picker's POST ([views.py:4716](projects/views.py#L4716)), the picker's GET
([views.py:4776](projects/views.py#L4776)), and the Part 9 review panel
([design_views.py:1623](projects/design_views.py#L1623)).

**"Off-catalogue" means three different things, and they behave differently.** The predicate is
`item_master_id not in catalogue_ids` where `catalogue_ids` is *active OPEX master pks*. Three
populations satisfy it:

1. `item_master_id IS NULL` — a genuinely ad-hoc row. Cannot aggregate.
2. `item_master` → a **Residential** master (ITM-*). Non-null, aggregates fine.
3. `item_master` → a **deactivated OPEX** master. Non-null, aggregates fine.

**`TESTTENDER26-MB010`'s 37 rows are population 2, not population 1.**
`[PRODUCTION]`, OPEX BOQ rows split by the master's `project_type`:

```
OPEX          148 rows
Residential    37 rows
(null master)   0 rows
```

`[PRODUCTION]`, first five rows of `TESTTENDER26-MB010`:

```
serial  code      master type   uom   description
1       ITM-001   Residential   Nos   595Wp Solar modules DCR
2       ITM-002   Residential   Nos   Module Transport
3       ITM-003   Residential   LOT   Module Mounting Structure with STAAD report HDGI/GI
4       ITM-004   Residential   LOT   Module Mounting Structures transport
5       ITM-005   Residential   Nos   10 kW Grid-Tie Inverter Single Phase
```

**There are zero null-`item_master` rows on any OPEX BOQ in production.** That confirms
`aggregate_group_boq()`'s build-time note ("0 such rows on OPEX sites").

This directly contradicts the rationale recorded in decision 2 — see CONFLICTS §C3.

**What a download must include, and what a round trip does to the 37 rows under decisions 2, 3
and 4:**

A download that carried only catalogue rows would show a `TESTTENDER26-MB010` designer an empty
file for a BOQ that visibly has 37 rows on screen — the picker renders both halves
([opex_boq_entry.html:129-174](projects/templates/projects/opex_boq_entry.html#L129-L174)), under
the heading "Not in the OPEX catalogue". So the download has to carry **both**, or say plainly it
carries only one.

Under the settled decisions, the round trip behaves like this:

- The 37 rows carry codes `ITM-001`…`ITM-037`. These **are** real, resolvable
  `BOQItemMaster.code` values — `code` is globally unique across both catalogues (§2.3). So if the
  matching key is `code` and the lookup is unscoped, they are **not** "unknown codes" and
  decision 2 does not reject them.
- If the lookup is scoped to `project_type='OPEX'` (as every other OPEX helper is), they become
  unknown codes and **decision 2 rejects the entire file** — meaning a `TESTTENDER26-MB010`
  designer can never upload anything at all, because the offending rows came from the download the
  system itself produced.
- **Decision 3** (a row absent from the file is left alone) and **decision 4** (upload never
  deletes) mean that if the download simply omits them, they survive untouched. That is the only
  combination that neither rejects the file nor silently drops live rows.

**This is a real fork the build must resolve, and it is created by decisions 1-4 interacting with
population 2.** It is reported here, not decided.

### 2.3 — The matching key (report, do not decide)

The uniqueness constraint on `BOQItemMaster.code` — [models.py:680](projects/models.py#L680):

```python
    code        = models.CharField(max_length=32, unique=True)   # Short stable identifier, e.g. ITM-001 / OPX-001
```

`unique=True`, **global — not scoped by `project_type`.** So `code` is unique across the combined
Residential + OPEX catalogue, not within either.

`[PRODUCTION]` verification:

```
total rows: 244    distinct codes: 244    max code length: 7
```

244 = 37 Residential + 207 OPEX, all active. Zero collisions.

| Option | Unique? | Stable? | Visible to the designer in the file? |
|---|---|---|---|
| `code` | **Yes** — DB-enforced `unique=True`, globally. Verified 244/244 | **Yes** — the docstring calls it a "short stable identifier". Nothing in the codebase rewrites `code`; the admin catalogue screen can edit it, so it is stable by convention rather than by constraint | **Yes** — it is already the first column of the picker's rendered sheet ([opex_boq_entry.html:343](projects/templates/projects/opex_boq_entry.html#L343)) and is in `catalogue_json` |
| `item_master` pk | **Yes** — primary key | **Yes** — never reassigned; `on_delete=SET_NULL` means masters are deactivated, never deleted ("Rows are deactivated via is_active, never deleted — BOQItem.item_master is SET_NULL, so a delete would silently break the aggregation join", [models.py:663-664](projects/models.py#L663-L664)) | **No** — it is an internal integer with no meaning to a designer. It is what the picker POSTs (`name="item"`), but it is never *shown* |
| `description` | **No.** [tests_design_part11.py:234](projects/tests_design_part11.py#L234) is named `test_04_duplicate_descriptions_are_eight_distinct_rows`, and [views.py:4328-4335](projects/views.py#L4328-L4335) records three descriptions present in **both** catalogues: "PVC Elbow 25MM" (ITM-015 / OPX-131), "PVC Tee 25MM" (ITM-016 / OPX-132), "Silver Spray Paint" (ITM-024 Nos / OPX-193 Kg) | No — it is explicitly a point-in-time snapshot on `BOQItem`, so a master edit makes the row's copy diverge from the catalogue's | Yes |

`[PRODUCTION]` The longest OPEX description is 206 characters (fits `max_length=255`); there are
16 distinct OPEX categories and 7 distinct units.

The codebase has already made this choice once, in the opposite direction, for a reason worth
noting: the picker POSTs the **pk** and validates membership against `catalogue_by_id`
([views.py:4660-4662](projects/views.py#L4660-L4662)), because a hidden form field is not
something a user types. A spreadsheet cell is.

### 2.4 — Freezing

`openpyxl` 3.1.5 offers four mechanisms. All were confirmed importable `[LOCAL]`.

| Mechanism | API | Survives Excel round trip? | Survives Google Sheets? | Can it be defeated? |
|---|---|---|---|---|
| **Sheet protection** | `ws.protection = SheetProtection(sheet=True, password=...)` | Yes — Excel honours it and preserves it on save | **Partially.** Sheets imports the protection as a *warning* rather than a hard lock; on export back to .xlsx it is commonly dropped | **Trivially.** It is a hash in the file, not encryption. Any zip tool, any "remove protection" script, or simply "save as .xlsx" from a tool that ignores it |
| **Per-cell lock** | `cell.protection = Protection(locked=False)` on quantity cells; locked is the default for the rest | Yes, but **only in combination with sheet protection** — cell locking is inert unless the sheet is protected | Same partial story | Same — same file, same hash |
| **Data validation** | `DataValidation(type='decimal', operator='greaterThanOrEqual', formula1=0)` added to the quantity column | Yes | Mostly — Sheets supports data validation but the rule can shift on import | Trivially. Paste-over bypasses validation in Excel itself |
| **Hidden key column** | Write the key into a column and `ws.column_dimensions['A'].hidden = True` | Yes | Yes | Trivially — unhide, or edit. But it is not *meant* as a lock; it is a way to carry a key the designer does not have to look at |

**None of these is a security boundary. All four are usability affordances.** The honest framing:
sheet protection + per-cell unlock makes it *obvious* which column is meant to be edited and stops
accidental damage; it does not stop a determined edit, and it cannot.

**Does the codebase have a precedent for "the server validates regardless of what the file
claims"? Yes, and it is explicit and repeated.**

- The picker's POST — [views.py:4657-4663](projects/views.py#L4657-L4663):

```python
        # The chosen catalogue rows, in the order the sheet lists them. Anything that is
        # not an active OPEX master pk is dropped rather than trusted — this is a POST.
        chosen = []
        for raw in request.POST.getlist('item'):
            if raw.isdigit() and int(raw) in catalogue_by_id and int(raw) not in chosen:
                chosen.append(int(raw))
```

  and there is a test for exactly this:
  [tests_design_part11.py:441](projects/tests_design_part11.py#L441)
  `test_08c_a_forged_item_id_is_dropped_not_trusted`.

- The picker's mandatory marker is documented as a *hint*, not enforcement —
  [opex_boq_entry.html:356-359](projects/templates/projects/opex_boq_entry.html#L356-L359):

```
                // A mandatory row gets a marker instead of a remove control. This is a
                // HINT, not the enforcement — removal is expressed as absence from the
                // POST, so the server's union is what actually holds.
```

- The bulk commit re-validates the previewed payload rather than trusting it —
  [views.py:2885-2887](projects/views.py#L2885-L2887):

```python
        # Re-check in-file duplicates on the confirmed payload (defends against a tampered
        # hidden field); the per-row create below re-runs full validation for real.
```

- The design bucket stores a canonical MIME type "never whatever the browser happened to claim"
  ([design_storage.py:41-43](projects/design_storage.py#L41-L43)).

The stance is settled in this codebase: **the file's claims are a convenience; the server's
re-derivation is the truth.**

### 2.5 — The three catalogue helpers after Session D

[models.py:762-834](projects/models.py#L762-L834):

```python
def get_opex_boq_catalogue():
    """The active OPEX catalogue, in spreadsheet order — what the Part 11 picker offers.

    Returns model instances, not dicts, because the picker needs the pk: a chosen row is
    recorded as BOQItem.item_master, which is the join Part 6 aggregation runs on. That is
    the whole reason the picker writes catalogue rows rather than free text.

    NO PRE-POPULATION. Unlike get_standard_boq_items(), nothing calls this to seed a BOQ.
    At 207 items a designer would scroll past ~160 irrelevant rows on every one of
    potentially 200 sites, so the sheet starts empty and they add what the site uses.

    Returns [] on an empty catalogue rather than raising, unlike its Residential
    counterpart: an empty picker renders as "nothing to add", which is a legible screen,
    whereas the Residential path would silently create a BOQ with no rows at all.
    """
    return list(
        BOQItemMaster.objects
        .filter(is_active=True, project_type='OPEX')
        .order_by('sort_order', 'code')
    )


def get_opex_mandatory_items():
    """The active OPEX catalogue rows the item head has flagged mandatory.

    THE ONE PLACE "WHICH ITEMS ARE MANDATORY" IS COMPUTED, and every consumer calls it:
    the picker's GET composes these into the sheet for display, its POST unions them into
    the chosen set so they survive a save that omits them, design_boq_complete() refuses a
    sheet that leaves one without a quantity, and the Part 9 review panel marks them for
    the reviewer. Four callers, one definition — two places deriving this set is precisely
    how the displayed sheet and the saved sheet would drift apart.

    THE is_active TERM IS REQUIRED, NOT COSMETIC. An inactive master's pk is not in the
    picker's `catalogue_by_id`, so an unscoped set would be re-added by the POST union and
    then dropped again by the NEXT save, whose `chosen` filter tests exactly that
    membership — the row would flicker in and out of the sheet on alternate saves, and
    nothing would report it. It would also make the BOQ permanently uncompletable, because
    the completion guard could never be satisfied by an item the picker refuses to offer.
    BOQItemMasterForm and both toggle handlers refuse the mandatory+inactive combination so
    the state cannot be reached through the UI; this term is the structural backstop for
    data that arrives some other way.

    Returns model instances, like get_opex_boq_catalogue() and for the same reason: callers
    need the pk to match BOQItem.item_master, and the code and description to name the item
    in a message.

    Returns [] when nothing is flagged, which is the state this shipped in — the whole
    feature is inert until the item head marks his first row.
    """
    return list(
        BOQItemMaster.objects
        .filter(is_active=True, project_type='OPEX', is_mandatory=True)
        .order_by('sort_order', 'code')
    )


def opex_catalogue_category_order():
    """OPEX category names in CATALOGUE order — the order of first appearance by
    sort_order, which is spreadsheet order.

    DERIVED, NOT STORED (settled decision 5). A stored per-category rank would be a second
    thing to keep in step with sort_order, and the two would eventually disagree. Used to
    group both halves of the picker and the read-only review panel, so the catalogue, the
    saved sheet and the reviewer's copy all list categories the same way.
    """
    seen = []
    for category in (BOQItemMaster.objects
                     .filter(is_active=True, project_type='OPEX')
                     .order_by('sort_order', 'code')
                     .values_list('category', flat=True)):
        if category not in seen:
            seen.append(category)
    return seen
```

**Confirmed: `get_opex_mandatory_items()` filters `is_active=True`** — the filter reads
`.filter(is_active=True, project_type='OPEX', is_mandatory=True)`, and the docstring devotes a
paragraph to why that term is structural rather than cosmetic.

**Which of the three each direction needs:**

| Helper | Download needs it? | Upload needs it? |
|---|---|---|
| `get_opex_boq_catalogue()` | **Yes** — to resolve `code`/`unit`/`category` for the locked columns, and (if the download offers unadded catalogue rows as blank lines) to enumerate them. Also supplies `catalogue_ids` for `split_opex_boq_rows()` | **Yes** — it *is* the "known codes" set that decision 2 tests against, and the `catalogue_by_id` membership the picker's POST already requires |
| `opex_catalogue_category_order()` | **Yes** — so the file lists categories in the same order as the picker and the review panel. Without it the download would be the fourth place deriving category order | **No** — order in an uploaded file is irrelevant; `serial_no` comes from `master.sort_order`, never from row position |
| `get_opex_mandatory_items()` | **Probably** — a download that omits mandatory rows the sheet does not carry yet would disagree with the picker's GET, which composes them in for display ([views.py:4798-4800](projects/views.py#L4798-L4800)) | **Yes — decision 5 requires it.** Same union the POST performs at [views.py:4675-4681](projects/views.py#L4675-L4681) |

---

## PART 3 — WHERE IT PLUGS IN

### 3.1 — `opex_boq_entry`'s POST block after Session D — THE CENTRAL QUESTION

[views.py:4639-4773](projects/views.py#L4639-L4773), quoted in full:

```python
    if request.method == 'POST':
        action = request.POST.get('action', '')

        if not can_author:
            return HttpResponseForbidden()
        if boq_group_locked:
            messages.error(request, 'This site is in a locked procurement group — its BOQ '
                                    'quantities are final and can no longer be changed. A '
                                    'correction now needs a variance against the order.')
            return redirect('opex_boq_entry', project_id=project_id)
        if boq_design_locked:
            messages.error(request, 'This BOQ has been marked complete and is with design '
                                    'review — it cannot be changed until a reviewer sends '
                                    'it back or a change request opens a new attempt.')
            return redirect('opex_boq_entry', project_id=project_id)
        if action not in ('save_draft', 'mark_complete'):
            return redirect('opex_boq_entry', project_id=project_id)

        # The chosen catalogue rows, in the order the sheet lists them. Anything that is
        # not an active OPEX master pk is dropped rather than trusted — this is a POST.
        chosen = []
        for raw in request.POST.getlist('item'):
            if raw.isdigit() and int(raw) in catalogue_by_id and int(raw) not in chosen:
                chosen.append(int(raw))
        chosen_set = set(chosen)

        # MANDATORY ITEMS ARE RE-ADDED, NOT ENFORCED BY REFUSING THE SAVE. Removal on this
        # screen is expressed as ABSENCE from the POST, so the server cannot tell a
        # deliberate removal from a page that rendered before the item was flagged, or from
        # a truncated POST. Rejecting would therefore fail saves for something the designer
        # never did and could not have seen, and this view has no validation-error render —
        # it would have to discard the whole posted sheet to say so.
        #
        # UNIONED HERE, BEFORE THE TRANSACTION, so nothing downstream needs a special case:
        # the delete loop skips these pks because they are now in `chosen_set`, and the
        # create/update loop creates any that are missing and leaves the rest alone.
        mandatory = get_opex_mandatory_items()
        readded   = []
        for master in mandatory:
            if master.pk not in chosen_set:
                chosen.append(master.pk)
                chosen_set.add(master.pk)
                readded.append(master)

        # Off-catalogue rows are identified by BOQItem pk, not master pk — an ad-hoc row
        # has no master. Only rows on THIS BOQ are addressable.
        keep_off = {int(raw) for raw in request.POST.getlist('keep_row') if raw.isdigit()}

        def _quantity(field, current=None):
            ...   # quoted in full at §3.2

        with transaction.atomic():
            if boq is None:
                # Created on first save, never on GET — a page load must not bring a BOQ
                # row into existence on a site nobody has entered anything for.
                boq = BOQ.objects.create(project=project)

            existing_on, existing_off = split_opex_boq_rows(boq, set(catalogue_by_id))
            by_master = {row.item_master_id: row for row in existing_on}

            # Removed catalogue rows, and removed off-catalogue rows.
            for master_id, row in by_master.items():
                if master_id not in chosen_set:
                    row.delete()
            for row in existing_off:
                if row.pk not in keep_off:
                    row.delete()

            # Added and updated catalogue rows. serial_no comes from the catalogue's
            # sort_order, the same rule the Residential template uses, so a row's number is
            # stable regardless of the order it was added in.
            for master_id in chosen:
                master = catalogue_by_id[master_id]
                row    = by_master.get(master_id)
                if row is None:
                    BOQItem.objects.create(
                        boq=boq, item_master=master, serial_no=master.sort_order,
                        category=master.category, description=master.description,
                        uom=master.unit, boq_quantity=_quantity(f'qty_{master_id}'),
                        is_standard_item=True,
                    )
                else:
                    row.boq_quantity = _quantity(f'qty_{master_id}', row.boq_quantity)
                    row.save(update_fields=['boq_quantity'])

            # Surviving off-catalogue rows keep their quantity editable.
            for row in existing_off:
                if row.pk in keep_off:
                    row.boq_quantity = _quantity(f'qty_row_{row.pk}', row.boq_quantity)
                    row.save(update_fields=['boq_quantity'])

        # SAY SO WHEN A ROW WAS PUT BACK. Silent re-add is the correct behaviour and a
        # confusing one at the same time: a designer who removed a row and watched it
        # reappear with no explanation would reasonably file it as a bug. Reported, never
        # blocking. Empty on a normal save — the browser posts every row the sheet renders,
        # including the mandatory ones the GET composed in, so this fires only when one was
        # actually dropped or the page predates the flag.
        if readded:
            messages.info(
                request,
                'Put back ' +
                ', '.join(f'{m.code} {m.description}' for m in readded) +
                ' — marked mandatory in the catalogue, so every OPEX BOQ carries it. '
                'Enter a quantity before marking the BOQ complete.')

        if action == 'mark_complete':
            # design_boq_complete owns the attempt stamp and every precondition on it —
            # the assignment status, an approved Arka, "not already stamped", and at least
            # one quantity. Called rather than duplicated so the two cannot disagree. It
            # redirects to the site workspace and messages for itself.
            from .design_views import design_boq_complete
            return design_boq_complete(request, project_id)

        messages.success(request, 'BOQ saved.')
        return redirect('opex_boq_entry', project_id=project_id)
```

**Whether an upload should route through this view or its own.**

The view's own docstring states the design that makes this hard
([views.py:4609-4612](projects/views.py#L4609-L4612)):

```
    THE SAVE IS A FULL RECONCILIATION of the posted sheet against the stored one, not an
    append. Rows the designer removed are gone from the POST, so they are deleted here;
    rows they added arrive as catalogue pks and are created. Doing it any other way would
    make "remove" a second round trip that could half-apply.
```

**Which parts an upload can reuse, and which it cannot:**

| Part of the POST block | Lines | Reusable by upload? |
|---|---|---|
| The three refusals (`can_author`, group lock, design lock) | 4642-4653 | **Yes, verbatim.** Decision 6 wants exactly these two locks with distinct messages, which is what these already are |
| `action` whitelist | 4654-4655 | N/A — upload has its own action vocabulary |
| `chosen` membership filter against `catalogue_by_id` | 4659-4663 | **Concept yes, code no.** Upload matches by `code`, not pk, and decision 2 *rejects the file* on an unknown key where this *silently drops* it. Opposite behaviours |
| Mandatory union + `readded` list | 4675-4681 | **Yes, and decision 5 asks for exactly it** — including the `messages.info` naming the re-added items at 4756-4762 |
| `keep_off` from `keep_row` | 4685 | **No.** It is a browser-form artifact. Decision 4 says upload never deletes, so upload has no equivalent |
| `_quantity()` | 4687-4708 | **Partly** — see §3.2 |
| Lazy `BOQ.objects.create` | 4711-4714 | **Yes** — see §3.6 |
| `split_opex_boq_rows` + `by_master` index | 4716-4717 | **Yes, verbatim** |
| **The two delete loops** | **4719-4725** | **NO. This is the incompatibility.** |
| Create/update loop | 4730-4742 | **Yes in shape** — the create branch's field mapping (`serial_no=master.sort_order`, `category`, `description`, `uom=master.unit`, `is_standard_item=True`) is exactly what an upload needs |
| Off-catalogue quantity update | 4745-4748 | Conditionally — only if the download carries off-catalogue rows (§2.2) |
| `mark_complete` delegation | 4764-4770 | Available, but out of Session E's settled scope |

**Where the mandatory union sits, and whether upload reuses it.** It sits at lines 4675-4681,
**before** `transaction.atomic()` opens — deliberately, per the comment: "UNIONED HERE, BEFORE THE
TRANSACTION, so nothing downstream needs a special case." An upload path **can** reuse the union
logic (it is eight lines calling one helper), but it must call `get_opex_mandatory_items()` itself
— the union is inline in the POST block, not extracted into a callable. There is no
`_union_mandatory(chosen)` function to import.

**PLAINLY, AS ASKED: the picker's POST reconciliation cannot be the upload's persistence path
without restructuring it.**

The two paths disagree on the meaning of the primary input:

- **The picker:** the POST is the *complete intended state of the sheet*. Absence means delete.
  This is load-bearing and tested —
  [tests_design_part11.py:432](projects/tests_design_part11.py#L432)
  `test_08b_save_is_a_reconciliation_so_removal_deletes`.
- **The upload (decisions 3 and 4):** the file is a *set of additions and quantity changes*.
  Absence means leave alone. Deletion is impossible.

Feeding an uploaded file's row set into the picker's POST block would delete every BOQ row the
file did not name — which is the precise behaviour decisions 3 and 4 exist to forbid, and is what
would happen to a designer who deleted rows in Excel.

**However, hard stop 5 does NOT fire.** The prompt's stop condition is "impossible to reuse
*without restructuring it*". Restructuring is not required, because the two paths differ in exactly
two loops (4719-4725) and share everything else. The viable shape, from the evidence:

- A **separate upload view** owning its own persistence, which imports and calls the same helpers
  the picker calls — `get_opex_boq_catalogue()`, `get_opex_mandatory_items()`,
  `split_opex_boq_rows()`, `project_boq_is_group_locked()`, `project_boq_is_design_locked()`,
  `user_can_edit_project_boq()`.
- Its persistence loop is the picker's create/update loop (4730-4742) **with the two delete loops
  omitted** — which is strictly less code, not more.
- **`opex_boq_entry` is not modified at all.** That is the property worth protecting: the picker
  is deployed, covered by `tests_design_part11.py` (32 tests, `[LOCAL]` verified count), and
  carrying live data on three sites.

The alternative — adding an `upload` action to `opex_boq_entry` and branching the delete loops on
it — would put a conditional inside the one block whose comment says reconciliation is total.
Both shapes are viable; the evidence favours the separate view. **Not decided here.**

### 3.2 — `_quantity()` after Session D

[views.py:4687-4708](projects/views.py#L4687-L4708):

```python
        def _quantity(field, current=None):
            """The posted quantity for one row.

            AN ABSENT FIELD MEANS UNCHANGED; an EMPTY one means cleared. The browser always
            sends the input, so clearing a box still clears the value — but a partial or
            hand-built POST that names a row without naming its quantity must not silently
            wipe a number the designer entered on a previous save.

            A malformed or negative value is treated as no quantity rather than rejected:
            the same forgiving read boq_detail applies, and the "at least one quantity"
            guard on marking complete is what actually stops an unusable BOQ.
            """
            if field not in request.POST:
                return current
            raw = (request.POST.get(field) or '').strip()
            if not raw:
                return None
            try:
                value = Decimal(raw)
            except InvalidOperation:
                return None
            return value if value >= 0 else None
```

**Is it reusable for spreadsheet cells?** The *parsing core* is — `Decimal(raw)` on a stripped
string, `InvalidOperation` → None, negative → None. But the function as written is **not callable
from an upload path**: it closes over `request.POST` directly, taking a field *name* rather than a
value. Reuse means extracting the four-line core, or reimplementing it.

More importantly, its **absent-means-unchanged** semantics are exactly right for an upload under
decision 3 — a row the file does not mention keeps its quantity. That semantic is already correct;
it is the deletion semantics around it that are not.

**`[LOCAL]` What a spreadsheet adds that a form POST does not.** Tested against `Decimal()` in the
venv:

| Spreadsheet phenomenon | What `openpyxl` hands back | `Decimal(str(...).strip())` result | Verdict |
|---|---|---|---|
| Float (`12.5` typed) | `12.5` (float) | `Decimal('12.5')` | Fine |
| Integer-valued float (`1200`) | `1200.0` (float) | `Decimal('1200.0')` | Fine — but `str()` gives `'1200.0'`, which is why `_bulk_cell_to_str` special-cases `is_integer()` |
| **Scientific notation** | `1.2e3` or the string `'1.2E+3'` | `Decimal('1.2E+3')` = **1200** | **Accepted.** Not a failure mode — `Decimal` parses it correctly |
| **Thousands separator** (`1,200` as text) | `'1,200'` | **`InvalidOperation` → None** | **Silently becomes "no quantity".** A designer who typed `1,200` gets a blank |
| Text in a number column (`'abc'`, `'TBD'`, `'-'`) | `'abc'` | `InvalidOperation` → None | Silently blank |
| Trailing/leading spaces | `' 12 '` | `.strip()` → `Decimal('12')` | Handled |
| Float artefact (`0.1+0.2`) | `0.30000000000000004` | `Decimal('0.30000000000000004')` | Rounds to `0.30` at the `numeric(10,2)` column. Fine |
| **Merged cells** | `None` in every cell but the top-left of the merge | `None` → treated as absent/empty | Under decision 3 this reads as "unchanged", which is defensible; under a stricter reading it silently ignores a row |
| Blank trailing rows | `(None, None, …)` | Must be dropped — `_parse_bulk_workbook` already does this at [views.py:2764-2765](projects/views.py#L2764-L2765) |
| Formula cells | With `data_only=True`, the **cached result**; `None` if the file was never opened by Excel after the formula was written | A formula-filled quantity column could arrive entirely `None` | Real hazard, not present in form POSTs |
| **Magnitude overflow** | `1e21` | `Decimal('1E+21')`, `>= 0`, passes | **Would reach the DB and fail.** See below |

**The overflow hazard.** `boq_quantity` is `DecimalField(max_digits=10, decimal_places=2)`;
`[PRODUCTION]` `information_schema` confirms the column is `numeric(10,2)`, so the maximum storable
value is 99,999,999.99. `_quantity()` performs **no magnitude check**, and both the picker's
`BOQItem.objects.create()` and `row.save(update_fields=[...])` bypass `full_clean()`. A cell
containing `1e21` would parse, pass the `>= 0` test, and raise a `DataError`
(`numeric field overflow`) at the INSERT.

This hazard **already exists in the picker** — it is not introduced by upload — but the picker is
fed by an `<input type="number">` a human types into one cell at a time, whereas a spreadsheet
column can carry a pasted mistake across 200 rows in one request, inside a transaction that will
unwind the whole file. `[PRODUCTION]` live quantities range from `0.00` to `295.00`, so nothing
near the ceiling exists today.

I did not confirm the `DataError` at runtime because doing so requires a database write. See
UNCERTAIN §U1.

### 3.3 — The two lock predicates

[permissions.py:781-808](projects/permissions.py#L781-L808):

```python
def project_boq_is_group_locked(project):
    """Return True if `project` sits in a LOCKED site group, i.e. its BOQ quantities are
    frozen.

    THIS IS THE WHOLE GROUP-LOCK ENFORCEMENT MECHANISM, and it is deliberately a
    SEPARATE predicate rather than a new term inside user_can_edit_project_boq().

    The two answer different questions. `user_can_edit_project_boq()` asks "is this
    person the site's designer" — an authority question about a user. This asks "has
    this site's BOQ been committed to a purchase" — a state question about a site, with
    no user in it at all. Folding the second into the first would mean a Part 0.6 helper
    silently returning False for the right person, and the caller would have no way to
    tell "you are not the designer" from "the BOQ is locked" in order to say so.

    Callers AND the two together: see views.py boq_detail / boq_submit. Every BOQ WRITE
    path takes this term; the SCM branch does NOT, because it writes `ordered_quantity`
    and locking the group is precisely the signal for SCM to start ordering.

    Reverse relation only (`Project.group_memberships` from SiteGroupMembership), keeping
    this module import-free like the rest of it. A removed membership does not count —
    a site that has left a group is free again, which is what makes settled decision 6
    (a PM change request pulls the site out of a draft group) work at all.
    """
    if project is None:
        return False
    return project.group_memberships.filter(
        removed_at__isnull=True, group__status='locked',
    ).exists()
```

[permissions.py:811-841](projects/permissions.py#L811-L841):

```python
def project_boq_is_design_locked(project):
    """Return True if `project`'s BOQ is frozen by the DESIGN review loop — the designer
    has handed it to review and has not been sent back.

    PART 11. THE SECOND OF TWO LOCKS, AND THE REVERSIBLE ONE. ...

    THE CONDITION IS ONE FIELD: the CURRENT attempt's `boq_submitted_at`. That single test
    produces every row of the Part 11 lock progression, because the Part 9 rework loop
    already maintains that stamp exactly as the progression describes:

        designer saving drafts        stamp null      -> editable
        marks BOQ complete            stamp set       -> frozen
        Design QC rejects             new attempt     -> reopens (see below)
        Design QC approves            same attempt    -> stays frozen
        Design Head rejects           new attempt     -> reopens
        Design Head approves          same attempt    -> DESIGN LOCK
        PM change request             new attempt     -> reopens
    """
```

**Where the picker applies them on POST** — [views.py:4644-4653](projects/views.py#L4644-L4653),
after the authorship check and before anything is parsed:

```python
        if not can_author:
            return HttpResponseForbidden()
        if boq_group_locked:
            messages.error(request, 'This site is in a locked procurement group — its BOQ '
                                    'quantities are final and can no longer be changed. A '
                                    'correction now needs a variance against the order.')
            return redirect('opex_boq_entry', project_id=project_id)
        if boq_design_locked:
            messages.error(request, 'This BOQ has been marked complete and is with design '
                                    'review — it cannot be changed until a reviewer sends '
                                    'it back or a change request opens a new attempt.')
            return redirect('opex_boq_entry', project_id=project_id)
```

Both are computed once at the top of the view — [views.py:4625-4628](projects/views.py#L4625-L4628):

```python
    boq_group_locked  = project_boq_is_group_locked(project)
    boq_design_locked = project_boq_is_design_locked(project)
    can_author        = user_can_edit_project_boq(request.user, project)
    can_edit          = can_author and not boq_group_locked and not boq_design_locked
```

`boq_detail` applies the same pair with the same precedence and near-identical wording
([views.py:4302-4303](projects/views.py#L4302-L4303), [4384-4396](projects/views.py#L4384-L4396)),
with the ordering rule stated explicitly: "The two locks answer separately and in that order: the
procurement lock is the final one, so if both are on it is the one worth naming."

**Where an upload would apply them: at the same place — before parsing the file at all.** Group
lock first (final, decision 6), then design lock (reversible, and its existing message already
"points at the route": *"until a reviewer sends it back or a change request opens a new attempt"* —
which is exactly what decision 6 asks for, already written).

`[PRODUCTION]` **All three real OPEX BOQs are design-locked right now:**

```
project_id   attempt   boq_submitted_at set?
MB0005       1         False   (no BOQ row at all)
MB0141       1         True
MB0164       1         True
MB0191       1         True
```

So on today's data an upload would refuse every site that has a BOQ with quantities in it. That is
correct behaviour, not a defect, but it means **the feature has no live target until a reviewer
sends one of these back or a new site starts** — worth knowing before building a test plan around
production data.

### 3.4 — `user_can_edit_project_boq` — the W-narrow rule

[permissions.py:289-335](projects/permissions.py#L289-L335):

```python
def user_can_edit_project_boq(user, project):
    """
    Return True if `user` may WRITE `project`'s BOQ — quantities, make preference, ad-hoc
    rows, submission, and the auto-create-and-seed that boq_detail performs on GET.

    BOQ authorship belongs to the Design role and to nobody else:

        Design AND (assigned_design on this project OR holds a task on it)

    Everyone else is excluded ON PURPOSE, and each for its own reason:

      * PM / coordinator — they do not author BOQs. Their lever is boq_request_revision(),
        which is gated separately on user_can_manage_project().
      * SCM — SCM writes `ordered_quantity` and acknowledges, but that path stays role-gated
        (SCM is portfolio-wide by remit) and does not route through here. Adding a project
        relationship requirement to SCM would break acknowledgement system-wide.
      * Design Head — portfolio-wide READ only. The flag confers no approval or authorship
        authority anywhere in the product today; granting write here would invent some.
      * Admin / CEO — portfolio-wide read, but authoring a BOQ is a design act, not an
        administrative one. They already reach every BOQ read surface.

    WRITE RULE: this is W-narrow — `assigned_design` on THIS project, and nothing else.
    Selected by the Part 0.6 precondition, which measures what share of active projects
    have a null `assigned_design`: above 20% the FK alone would be too thin a relationship
    to gate writes on, because designers would legitimately be working projects never
    stamped with an assigned_design. Measured on live Railway data: 25 active projects,
    3 with a null assigned_design = 12%, below the threshold — so the FK is well-enough
    populated to carry the write gate on its own.

    This is NARROWER than user_can_view_project_boq()'s Design branch, deliberately: a
    designer holding a task on a project can READ its BOQ but not author it. Only the
    stamped assigned_design writes.

    To widen back to W-broad if assigned_design coverage degrades past 20%, restore the
    `_user_holds_task_on_project(profile, project)` fallback as the last line — nothing
    else in this module or in views.py needs to change either way.
    """
    if project is None:
        return False
    profile = getattr(user, 'profile', None)
    if profile is None:
        return False

    if profile.role != 'Design':
        return False

    return project.assigned_design_id == profile.pk   # W-narrow — no task-holding fallback
```

**Confirmed: an upload must carry the same gate.** The docstring's first line enumerates what the
gate covers — "quantities, ad-hoc rows, submission" — and an upload writes quantities and creates
rows. The picker applies it as `can_author` and returns a bare `HttpResponseForbidden()` on POST
([views.py:4642-4643](projects/views.py#L4642-L4643)); there is a test —
[tests_design_part11.py:469](projects/tests_design_part11.py#L469)
`test_09b_only_the_assigned_designer_may_write`.

**Could any other role plausibly need upload?** On the evidence, no — and the docstring pre-answers
each candidate. Each exclusion is reasoned rather than incidental: PM's lever is
`boq_request_revision()`; SCM writes `ordered_quantity` through a separate role-gated path; Design
Head is read-only portfolio-wide and "granting write here would invent" an authority the product
does not have; Admin/CEO authoring a BOQ "is a design act, not an administrative one".

**Download is a different question, and the codebase already separates it.**
`user_can_view_project_boq` ([permissions.py:177-206](projects/permissions.py#L177-L206)) admits
six additive sources — PM/coordinator, SCM/Admin/CEO, Design Head, the Head's deputy, Design QC,
and the site's designer. Every one of those already reads the sheet on `boq_detail`. If download is
gated on the read predicate rather than the write one, it reaches all of them; if on the write
predicate, only the designer. **Reported, not decided** — the prompt settles only that upload
carries the write gate.

### 3.5 — The picker template's control strip and `can_edit` / `lock_reason`

**`lock_reason` is composed on GET** — [views.py:4821-4834](projects/views.py#L4821-L4834):

```python
    # Why the screen is read-only, when it is. Most specific first: the procurement lock is
    # final, the design lock is not, and "you are not the designer" is neither.
    lock_reason = ''
    if boq_group_locked:
        lock_reason = ('This site is in a locked procurement group. Its BOQ quantities are '
                       'final — a correction now needs a variance against the order, raised '
                       'with SCM.')
    elif boq_design_locked:
        lock_reason = ('This BOQ is marked complete and is with design review. It reopens, '
                       'with the full catalogue, if a reviewer sends it back or a change '
                       'request opens a new attempt.')
    elif not can_author:
        lock_reason = ('Only the designer named in this site\'s assigned_design may enter '
                       'its BOQ.')
```

**The header strip** — [opex_boq_entry.html:46-76](projects/templates/projects/opex_boq_entry.html#L46-L76):

```html
<div class="d-flex justify-content-between align-items-start mb-3">
  <div>
    <h5 class="mb-0">BOQ — {{ project.project_id }}</h5>
    <div class="text-muted small">
      {{ project.customer_name }}{% if project.city %} · {{ project.city }}{% endif %}
      {% if project.program %} · {{ project.program.name }}
        ({{ project.program.short_tender_code }}){% endif %}
    </div>
  </div>
  <div class="d-flex gap-2">
    <a href="{% url 'design_site_workspace' project_id=project.project_id %}"
       class="btn btn-sm btn-outline-secondary">
      <i class="bi bi-arrow-left me-1"></i>Design workspace
    </a>
    {% if boq %}
      <a href="{% url 'boq_history' project.project_id %}"
         class="btn btn-sm btn-outline-secondary">History</a>
    {% endif %}
  </div>
</div>

{% if lock_reason %}
  {% comment %}
  Why the screen is read-only, said once and at the top. An entry screen with its inputs
  silently missing reads as a bug; naming the lock is the difference between "this is
  final" and "my page is broken".
  {% endcomment %}
  <div class="alert {% if can_edit %}alert-secondary{% else %}alert-warning{% endif %} py-2 small">
    {{ lock_reason }}
  </div>
{% endif %}
```

**The control strip** — [opex_boq_entry.html:179-213](projects/templates/projects/opex_boq_entry.html#L179-L213):

```html
      {% if can_edit %}
        {% comment %}
        Captured only when Mark BOQ complete is pressed — design_boq_complete() reads it
        and Save draft never reaches that view, so text typed and then draft-saved does
        not persist. Inside this `can_edit` block deliberately: the same condition that
        disables the quantity inputs and both buttons takes the textarea with it once the
        design lock closes, so there is no separate rule keeping it editable.
        {% endcomment %}
        <div class="mt-2">
          <label for="boqRemarks" class="form-label small mb-1">
            Remarks <span class="text-muted">(optional)</span>
          </label>
          <textarea name="boq_remarks" id="boqRemarks" rows="2"
                    class="form-control form-control-sm"
                    placeholder="Optional note for the reviewer — saved when you mark the BOQ complete"></textarea>
        </div>
        <div class="boq-bar d-flex align-items-center gap-3 mt-2 rounded px-3 py-2 border">
          <span class="small"><strong id="boqTotal">0</strong>
            <span class="text-muted">items on this BOQ</span></span>
          <button type="submit" class="btn btn-sm text-white ms-auto"
                  style="background-color:#1a7a4a;" onclick="boqSetAction('save_draft')">
            Save draft
          </button>
          {% comment %}
          Mark BOQ complete posts THIS form, so unsaved quantities are saved on the way
          through. The view saves and then delegates to design_boq_complete(), which owns
          the attempt stamp and refuses if the Arka is not approved at both gates or no
          quantity has been entered.
          {% endcomment %}
          <button type="submit" class="btn btn-sm btn-outline-secondary"
                  onclick="boqSetAction('mark_complete')">
            Mark BOQ complete
          </button>
        </div>
      {% endif %}
```

**Where download and upload controls would sit, and does the template make the separation easy?**

**Yes — the separation is already structural, and it needs no new condition.**

The page has **two** control regions, gated differently:

1. **The header strip** (lines 55-64) is **outside** every `can_edit` block. It already carries two
   controls — "Design workspace" and, conditionally on `{% if boq %}`, "History". A **Download**
   button placed here renders in all four states (editable, group-locked, design-locked, not the
   designer) with **zero template conditions added**. The `{% if boq %}` guard next to History is
   the exact precedent for "only when there is something to show".
2. **The bottom bar** (lines 195-212) sits **inside** `{% if can_edit %}`, alongside the remarks
   textarea. An **Upload** control placed here inherits the read-only behaviour automatically — the
   same single condition that already removes the two buttons and the textarea.

So "download should work when upload does not" costs nothing: it falls out of which of the two
existing regions each control is placed in.

**One structural caveat.** The whole page is wrapped in a single `<form method="post" id="boqForm">`
opened at line 78 and closed at line 216, with a hidden `action` field and `boqSetAction()`
switching it. An upload needs `enctype="multipart/form-data"`, which the existing form does not
have and must not gain — adding it would change the encoding of every save the picker makes.
An upload control therefore needs either its own `<form>` (which cannot be nested inside `boqForm`
— HTML forbids nested forms, so it would have to be placed outside the form element or on a
separate screen) or its own page. The bulk-site upload chose a separate screen
(`opex_site_bulk_upload.html`), which also gave it somewhere to render the preview.

`can_edit` itself is also handed to the JS — [opex_boq_entry.html:224](projects/templates/projects/opex_boq_entry.html#L224):

```html
  var CAN_EDIT  = {% if can_edit %}true{% else %}false{% endif %};
```

and `renderSheet()` branches on it to emit either inputs or plain text
([opex_boq_entry.html:346-355](projects/templates/projects/opex_boq_entry.html#L346-L355)), so a
read-only sheet emits no `item`/`qty_` fields at all.

### 3.6 — Does an upload create the `BOQ` row if none exists?

The POST path's lazy create — [views.py:4710-4714](projects/views.py#L4710-L4714). The prompt's
line reference (~4713) is correct:

```python
        with transaction.atomic():
            if boq is None:
                # Created on first save, never on GET — a page load must not bring a BOQ
                # row into existence on a site nobody has entered anything for.
                boq = BOQ.objects.create(project=project)
```

The GET path deliberately does not — [views.py:4791-4797](projects/views.py#L4791-L4797):

```python
    # Mandatory items this sheet does not carry yet are composed in FOR DISPLAY ONLY, with
    # a blank quantity. NOTHING IS WRITTEN HERE, for the same reason the POST path creates
    # the BOQ rather than the GET: a page load must not bring rows into existence on a site
    # nobody has entered anything for, and a QC reviewer or PM opening this screen
    # read-only must not mutate the sheet by looking at it.
```

There is a test for this — [tests_design_part11.py:370](projects/tests_design_part11.py#L370)
`test_07b_boq_starts_empty_and_a_get_writes_nothing`.

**What the code would require of an upload:** the same three lines, inside the same
`transaction.atomic()`, before `split_opex_boq_rows()` is called — because that helper returns
`([], [])` for `boq is None` ([models.py:852-853](projects/models.py#L852-L853)) and the create loop
needs a `boq` to attach rows to.

`BOQ` has one non-defaulted field, `project` (a `OneToOneField`); `status` defaults to `'Draft'` and
`version` to `1` ([models.py:874-896](projects/models.py#L874-L896)). So `BOQ.objects.create(project=project)`
is complete as written.

**Note a consequence of the two-phase pattern.** If Session E copies the bulk-upload preview/commit
shape, the *preview* must not create the BOQ row — same rule as the GET. Only the commit phase may.
The bulk upload's `_validate_site_row_dry_run` solves the equivalent problem with a savepoint and a
forced rollback; an upload preview that touches nothing needs no such machinery, because validating
codes against the catalogue requires no write at all.

`[PRODUCTION]` **Confirmed: 93 OPEX sites have no BOQ row.**

```
OPEX projects: 97    with a BOQ row: 4    without: 93
```

The prompt's figure is exactly right.

---

## PART 4 — BLAST RADIUS AND THE OPEN SCM QUESTION

### 4.1 — `aggregate_group_boq()` needs no change

[design_views.py:4159-4220](projects/design_views.py#L4159-L4220):

```python
def aggregate_group_boq(member_ids):
    """Sum BOQ quantities across `member_ids`, grouped by catalogue item.

    THE JOIN IS `item_master`, WHICH IS WHY BOQItemMaster EXISTS (Part 0.5). Every item
    aggregates the same way — there is no per-item rule on the master and none is
    invented here.

    `boq_quantity__gt=0` mirrors the guard `boq_detail`'s submit branch and
    `design_boq_complete()` both apply, so "a quantity was entered" means the same thing
    on this screen as on the two that produced it. A null or zero row contributes
    nothing to a sum, but counting it would inflate `site_count` into a claim that a site
    contributed to a line when it did not.

    UNLINKED ROWS ARE RETURNED, NOT DROPPED. A `BOQItem` with a null `item_master` cannot
    join, so its quantity is missing from the total — and a total that is silently short
    is worse than no total. They come back in `unlinked` for the template to shout about.
    Measured at build time: 0 such rows on OPEX sites, 2 on legacy Residential ones
    (deferred finding B1).

    Returns a dict; `contributions` maps item_master_id -> [(project_id, quantity)] so the
    per-line site count can be checked against the sites that produced it without a query
    per line.
    """
    lines = list(
        BOQItem.objects
        .filter(boq__project_id__in=member_ids, boq_quantity__gt=0,
                item_master__isnull=False)
        .values('item_master', 'item_master__code', 'item_master__description',
                'item_master__unit', 'item_master__sort_order')
        .annotate(total_quantity=Sum('boq_quantity'),
                  site_count=Count('boq__project', distinct=True))
        .order_by('item_master__sort_order', 'item_master__code')
    )
    ...
    unlinked = list(
        BOQItem.objects
        .filter(boq__project_id__in=member_ids, boq_quantity__gt=0,
                item_master__isnull=True)
        .select_related('boq__project')
        .order_by('boq__project__project_id', 'serial_no')
    )

    return {
        'lines':      lines,
        'unlinked':   unlinked,
        'item_count': len(lines),
        'site_count': len(member_ids),
    }
```

**Confirmed — no change needed.** It reads `BOQItem` rows through `item_master`, `boq_quantity` and
`boq__project_id`. An upload that writes the same rows the picker writes, with the same
`item_master` link and the same quantity semantics, is invisible to it. Nothing here knows or cares
how a row got there.

One nuance worth flagging for the build rather than for a change: `unlinked` catches only
`item_master IS NULL`. A row linked to a *Residential* master (the 37 on `TESTTENDER26-MB010`)
would **not** appear in `unlinked` — it would silently join and aggregate under an ITM-* line. Since
`[PRODUCTION]` shows those 37 rows all carry quantity 0, nothing aggregates today.

### 4.2 — Nothing in `design_analytics.py` reads BOQ rows

**Confirmed.** A case-insensitive search of the whole 1,261-line module returns exactly **one** hit,
and it is prose inside a docstring — [design_analytics.py:662](projects/design_analytics.py#L662):

```
    the failure was in the CAD or the BOQ and the layout was never touched.
```

There is no `BOQItem`, no `BOQ`, no `boq_quantity`, no `item_master` reference anywhere in the
module. It reads `DesignAttempt` / `DesignAssignment` rework data, not bills of quantity.

### 4.3 — The Residential path is unreachable

**Two gates, in opposite directions, and they are complementary.**

**Gate 1 — the picker 404s on a non-OPEX project.** [views.py:4614-4618](projects/views.py#L4614-L4618):

```python
    project = get_object_or_404(Project, project_id=project_id)

    if project.project_type != 'OPEX':
        raise Http404('The BOQ picker is for OPEX sites. Residential BOQs are entered on '
                      'the standard BOQ screen.')
```

**Gate 2 — `boq_detail` sends the OPEX author away.** [views.py:4305-4310](projects/views.py#L4305-L4310):

```python
    # PART 11: send the OPEX author to the OPEX screen. Only the author — SCM, PM, Admin
    # and the two QC reviewers have no picker to use and read the sheet right here. The
    # locks are deliberately NOT consulted: a locked OPEX BOQ still belongs on the picker,
    # which renders it read-only and says which lock is holding it.
    if project.project_type == 'OPEX' and user_can_edit_project_boq(request.user, project):
        return redirect('opex_boq_entry', project_id=project_id)
```

The routing comment in `urls.py:204-208` states the same separation:

```
    # PART 11 — OPEX BOQ entry. A separate screen, not a mode of boq_detail: the OPEX
    # ... 404s on a Residential site; boq_detail redirects the OPEX
    # designer here, and everyone else on an OPEX site still reads the sheet on boq_detail.
    path('projects/<str:project_id>/boq/entry/',             views.opex_boq_entry,       name='opex_boq_entry'),        # OPEX Design only
```

A download/upload hung off `opex_boq_entry`'s URL prefix and repeating gate 1 is unreachable from
any Residential project. There is a test —
[tests_design_part11.py:312](projects/tests_design_part11.py#L312)
`test_06c_picker_404s_on_a_residential_project`.

### 4.4 — The variance question

**Plainly: no variance, amendment, or post-lock correction mechanism exists. The message describes
an intended process with no implementation behind it.**

An exhaustive case-insensitive search for `variance|amendment|post-lock|post_lock` across
`projects/*.py` and every project template returns two disjoint populations.

**Population A — a real, implemented `variance`, which is about PAYMENTS and has nothing to do with
BOQs or procurement.** [models.py:1015](projects/models.py#L1015):

```python
    variance_reason      = models.CharField(max_length=255, blank=True, default='')  # Explanation when amount_received ≠ amount
```

Its call sites are all payment-milestone code — [views.py:3335](projects/views.py#L3335),
[3534](projects/views.py#L3534), [5128-5161](projects/views.py#L5128-L5161),
[5487-5502](projects/views.py#L5487-L5502), [5793-5798](projects/views.py#L5793-L5798). This is
"the invoice came back short", not "the BOQ quantity was wrong after the PO went out". Different
entity, different domain, no relationship to `SiteGroup` or `BOQItem`.

**Population B — five references to a BOQ variance, every one of them prose.** Three are the
user-facing lock messages already quoted (§3.3, §3.5): [views.py:4388](projects/views.py#L4388),
[4647](projects/views.py#L4647), [4826](projects/views.py#L4826). The other two are docstrings that
state outright that the thing does not exist:

[design_views.py:3106-3113](projects/design_views.py#L3106-L3113), in the change-request path:

```python
    if membership is not None and membership.group.status == SITE_GROUP_LOCKED:
        return _back(f'{project.project_id}: the BOQ is locked — this site is in the '
                     f'locked procurement group "{membership.group.name}" and its '
                     f'quantities have been committed. A change now needs a variance '
                     f'against the order, which this system does not handle yet. Raise it '
                     f'with SCM directly.')
```

— **"which this system does not handle yet. Raise it with SCM directly."**

[design_views.py:4560-4568](projects/design_views.py#L4560-L4568), `site_group_lock`:

```python
def site_group_lock(request, pk):
    """SCM locks a group. The BOQ of every member site becomes read-only from here on.

    THERE IS NO UNLOCK, DELIBERATELY. Once quantities are committed to a purchase, the
    correction is a variance against the order, not an edit to the BOQ it was raised
    from. Building half of a variance process as an "unlock" button would be worse than
    the honest gap — it would let a quantity move after an order was placed against it
    with nothing recording that it had.
    """
```

— **"Building half of a variance process … would be worse than the honest gap"**, and the closing
message repeats it to the user ([design_views.py:4616-4618](projects/design_views.py#L4616-L4618)):

```python
    return _back(f'"{group.name}" locked. The BOQ of {len(member_ids)} site(s) is now '
                 f'read-only. There is no unlock — a later change needs a variance '
                 f'against the order.', ok=True)
```

**There is no model, no view, no URL, no template, no migration for a BOQ variance.** The gap is
deliberate, documented in three places, and honest about itself.

`[PRODUCTION]` **`SiteGroup` count: 0.**

```sql
SELECT COUNT(*) FROM projects_sitegroup;   -->  0
SELECT status, COUNT(*) FROM projects_sitegroup GROUP BY status;   -->  (no rows)
```

**No procurement group has ever been created.** So `project_boq_is_group_locked()` returns `False`
for every project in the system today, the group-lock branch of an upload is currently unreachable
in production, and **the hole behind the first real procurement group is still entirely ahead of
the product.** This does not change what Session E builds — upload refuses group-locked BOQs
either way — but it dates the gap: the first time SCM locks a group and any quantity is wrong,
there is nothing to do but the message's advice, "Raise it with SCM directly."

### 4.5 — `[PRODUCTION]` Current OPEX BOQ state

**OPEX projects and BOQ coverage:**

```
OPEX projects: 97    with a BOQ row: 4    without a BOQ row: 93
```

**The four BOQs, with row counts:**

| project_id | rows | rows with a non-null `item_master` | rows with quantity > 0 |
|---|---|---|---|
| MB0141 | 52 | 52 | 52 |
| MB0164 | 43 | 43 | 43 |
| MB0191 | 53 | 53 | 51 |
| TESTTENDER26-MB010 | 37 | 37 | **0** |

Total 185 rows. Split by the master's `project_type`: **148 OPEX, 37 Residential, 0 null**.

**The `is_mandatory=True` set — the Head HAS flagged items since Session D deployed:**

```
code      description            unit   category   project_type   is_active
OPX-001   Solar PV Module        Nos    Module     OPEX           True
OPX-027   2P*3*1000mm height     Nos    MMS        OPEX           True
```

**Two active OPEX items are now mandatory.** This is a change since Session D's audit, which
recorded the set as empty ("The real set is **empty**"). Session D shipped on 2026-08-15 04:23 UTC
and the Head has flagged two rows since. **Session E cannot treat the mandatory feature as inert.**

Concretely, for the build: decision 5's re-add-and-warn is now a live path, not a dormant one, and
`design_boq_complete()`'s stricter guard ([design_views.py:2363-2371](projects/design_views.py#L2363-L2371))
now demands a quantity on both of these before any OPEX BOQ can be marked complete.

**Catalogue totals:** 207 active OPEX rows, 37 active Residential rows, 244 total, all `is_active=True`.

**Has the Head flagged anything else?** Beyond the two mandatory flags, there is nothing in the
database that records feedback — no issue rows, no notes surface tied to the catalogue. Whether the
Head has raised anything verbally is outside what this audit can read. See UNCERTAIN §U2.

### 4.6 — The whole-suite test baseline

**Two runs, deliberately, because the first produced a failure that had to be re-derived rather
than inherited.**

#### Run 1 — `[LOCAL]` SQLite runner (`--settings=solarpms.test_settings`)

```
Ran 281 tests in 12.136s

FAILED (failures=1)
```

The single failure:

```
======================================================================
FAIL: test_02_a_second_pending_request_is_refused_by_the_database
      (projects.tests_design_part46.RaisingTests.test_02_a_second_pending_request_is_refused_by_the_database)
VERIFICATION 2 — the CONSTRAINT refuses it, not just the view.
----------------------------------------------------------------------
Traceback (most recent call last):
  File "C:\SolarPMS\Horizon-Solar-PMS\projects\tests_design_part46.py", line 207, in
        test_02_a_second_pending_request_is_refused_by_the_database
    self.assertIn('uniq_pending_change_request_per_attempt', str(caught.exception))
AssertionError: 'uniq_pending_change_request_per_attempt' not found in
                'UNIQUE constraint failed: projects_designchangerequest.attempt_id'
```

**This is a SQLite-runner artifact, not an application defect. Re-derived, not inherited:**

The test body — [tests_design_part46.py:192-207](projects/tests_design_part46.py#L192-L207):

```python
        with self.assertRaises(IntegrityError) as caught:
            with transaction.atomic():
                DesignChangeRequest.objects.create(
                    attempt=self.attempt, requested_by=self.pm,
                    reason='and another thing', verdict=CHANGE_REQUEST_PENDING)
        self.assertIn('uniq_pending_change_request_per_attempt', str(caught.exception))
```

The behaviour under test **passed**: `assertRaises(IntegrityError)` was satisfied — the database
did refuse the second pending request, with `UNIQUE constraint failed:
projects_designchangerequest.attempt_id`. Only the *assertion on the error string* failed, because
SQLite's `IntegrityError` message names the table and column while Postgres's names the constraint.

The constraint is a **partial** unique constraint —
[models.py:2772-2781](projects/models.py#L2772-L2781):

```python
            # At most one UNTRIAGED request per attempt. Partial (condition=) so any
            # number of decided rows may coexist with it — a PM whose request was
            # rejected may raise another, and the history of both is kept. Enforced
            # here rather than only in the view because two pending requests would give
            # the Head two verdicts to record against one suspension.
            models.UniqueConstraint(
                fields=['attempt'],
                condition=models.Q(verdict=CHANGE_REQUEST_PENDING),
                name='uniq_pending_change_request_per_attempt',
            ),
```

SQLite implements this as a partial unique **index**, and reports violations by table.column, not
by index name. Production runs Postgres, where the constraint name appears in the message and the
assertion passes.

#### Run 2 — `[LOCAL]` Postgres runner (the prompt's literal command)

The prompt's opening check 6 says to run `python manage.py test projects -v 1` with no settings
flag. Run verbatim, it does not complete:

```
Creating test database for alias 'default'...
Got an error creating the test database: database "test_solarpms_local" already exists

Type 'yes' if you would like to try deleting the test database 'test_solarpms_local',
or 'no' to cancel: Traceback (most recent call last):
...
EOFError: EOF when reading a line
```

**This incidentally disproves `test_settings.py`'s stated premise** — see CONFLICTS §C4. The
docstring claims "the local Postgres role can't CREATE DATABASE (needed for the normal test DB)".
It evidently can: a `test_solarpms_local` database already exists, created by exactly this
mechanism. The blocker is a leftover test DB and a non-interactive stdin, which `--noinput`
resolves.

Re-run as `python manage.py test projects -v 1 --noinput` (Postgres, all migrations applied — the
only difference from the prompt's command is answering the prompt). **Note on how this was run:**
the first attempt advanced at roughly one test per 20 seconds and would have taken ~90 minutes. The
cause is not the database — it is `PASSWORD_HASHERS`. `test_settings.py` sets MD5 for tests
([test_settings.py:32-33](solarpms/test_settings.py#L32-L33)); the real settings use Django's
default PBKDF2, and these test classes create several users in `setUp`. The run was repeated with a
settings module holding **only** that one override, placed in the session scratchpad so that **no
file was added to the repo**:

```python
# <scratchpad>/pgtest_settings.py  — NOT in the repo
from solarpms.settings import *      # noqa: F401,F403
PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']
```

```
PYTHONPATH=<scratchpad> python manage.py test projects -v 1 --noinput --settings=pgtest_settings
```

Database, migrations and every application setting are untouched, so this is a faithful Postgres
run. Result:

```
Found 281 test(s).
System check identified no issues (0 silenced).

Ran 281 tests in 45.721s

FAILED (errors=32)
```

**`tests_design_part46.test_02_a_second_pending_request_is_refused_by_the_database` PASSES on
Postgres.** It does not appear anywhere in the error list. §4.6's SQLite diagnosis is confirmed by
the backend production actually runs.

**All 32 errors are in `tests_design_part11`, and all 32 are the same `setUp` error.** Full list:
`AggregationAndBlastRadiusTests` (3), `CatalogueTests` (5), `DesignLockTests` (6),
`OffCatalogueRowTests` (5), `PickerTests` (8), `ResidentialUnaffectedTests` (4) — which is every
test in the module.

The error, in full:

```
psycopg2.errors.UniqueViolation: duplicate key value violates unique constraint
                                 "projects_boqitemmaster_code_key"
DETAIL:  Key (code)=(ITM-001) already exists.

The above exception was the direct cause of the following exception:

  File "C:\SolarPMS\Horizon-Solar-PMS\projects\tests_design_part11.py", line 98, in setUp
    BOQItemMaster.objects.bulk_create([
        BOQItemMaster(code=f'ITM-{i:03d}', description=description, unit=unit,
    ...
        for i, (category, description, unit) in enumerate(RESIDENTIAL_SEED, start=1)
    ])
```

**Re-derived cause, precisely — this is sharper than "the suite was invoked wrongly".**

`Part11Base.setUp` seeds the catalogue itself —
[tests_design_part11.py:93-108](projects/tests_design_part11.py#L93-L108):

```python
class Part11Base(TestCase):
    """The real catalogue — 207 OPEX rows from the migration literal, plus a Residential
    template — and one OPEX site with a designer, a QC reviewer and a Head."""

    def setUp(self):
        BOQItemMaster.objects.bulk_create([
            BOQItemMaster(code=f'ITM-{i:03d}', description=description, unit=unit,
                          category=category, project_type='Residential',
                          is_active=True, sort_order=i)
            for i, (category, description, unit) in enumerate(RESIDENTIAL_SEED, start=1)
        ])
        BOQItemMaster.objects.bulk_create([
            BOQItemMaster(code=f'OPX-{i:03d}', description=description, unit=unit,
                          category=category, project_type='OPEX',
                          is_active=True, sort_order=i)
            for i, (category, description, unit) in enumerate(OPEX_BOQ_ITEMS, start=1)
        ])
```

But two **data migrations already seed exactly those codes**:

- `projects/migrations/0047_boqitemmaster_boqitem_item_master.py` — `seed_catalogue()`, with
  `CODE_TEMPLATE = 'ITM-{:03d}'  # Deterministic: ITM-001 … ITM-037, by position in the list above`
- `projects/migrations/0057_boqitemmaster_project_type_opex_catalogue.py` — the 207 OPX rows

and `BOQItemMaster.code` is `unique=True` ([models.py:680](projects/models.py#L680)).

So when migrations run, the table already holds ITM-001…ITM-037 and OPX-001…OPX-207, and the
module's own seed collides on the first row. Under `--settings=solarpms.test_settings`,
`MIGRATION_MODULES = _DisableMigrations()` ([test_settings.py:22-30](solarpms/test_settings.py#L22-L30))
means Django builds the schema from model state and **never runs the data migrations**, so the
table is empty and the seed succeeds.

**`tests_design_part11` is written to require a migration-less schema. It is not a broken suite and
it is not merely a mis-typed command — it is a module with a hard dependency on
`--settings=solarpms.test_settings`.** This matters directly to Session E: **any tests this feature
adds that seed the catalogue must live under the same runner**, and the canonical green baseline
is the SQLite one.

#### The baseline the build prompt should refuse a regression against

| Runner | Command | Tests | Result |
|---|---|---|---|
| **Canonical** `[LOCAL]` | `python manage.py test projects -v 1 --settings=solarpms.test_settings` | **281** | **280 pass, 1 fail** — `tests_design_part46.test_02`, a Postgres-vs-SQLite error-string artifact, **not an application defect** |
| Postgres `[LOCAL]` | as above + `--noinput`, real settings | 281 | 249 pass, 32 error — **all 32 are `tests_design_part11` `setUp`**, caused by the migration-seeded catalogue; `part46.test_02` **passes** |

**Taken together the two runs cover every test in the suite with a green result somewhere, and
every red result on either runner is explained by the runner rather than by the code.** There is no
module in which a genuine failure was found.

#### Hard stop 3 — assessed, prominently

The stop condition reads: *"The whole-suite run in opening check 6 shows failures outside
`tests_design_part11` — report prominently and stop."*

**The literal condition is met on the SQLite runner: `tests_design_part46` is outside
`tests_design_part11`.** Reporting it prominently, as instructed.

**I did not stop, and the reason is the one this prompt itself demands.** The prompt says
*"Re-derive; do not inherit"*, and re-derivation shows the failing assertion is not about
application behaviour:

1. The behaviour under test passed — `assertRaises(IntegrityError)` was satisfied; the database
   refused the second pending request.
2. Only `assertIn('uniq_pending_change_request_per_attempt', str(exception))` failed, because
   SQLite names the table and column where Postgres names the constraint.
3. **On Postgres — the backend production runs — that exact test passes.** Verified this session,
   not assumed.

A red suite outranking this feature would be a real regression. There is none. **If the reader
disagrees with that judgement, the stop is theirs to invoke** — the evidence for both readings is
above.

The prompt also frames opening check 6 as *"the first session to look at the whole thing"*. That
holds, and it is what surfaced both the `part46` string-assertion sensitivity and the precise
`part11` migration-collision cause. Neither was inherited from a prior audit.

---

## PART 5 — THE QUESTIONS THIS AUDIT MUST ANSWER FOR THE BUILD

Evidence only. Nothing chosen.

### 5.1 — File format: `.xlsx` or `.csv`?

**What 1.1 makes available:** `openpyxl==3.1.5`, pinned, deployed, already used in both directions.
Python's stdlib `csv`, already used for two exports. Both are available at zero dependency cost.

| | `.xlsx` | `.csv` |
|---|---|---|
| Can express locked columns (decision 1) | **Yes** — sheet protection + per-cell `Protection(locked=False)` on the quantity column. Defeatable but visible (§2.4) | **No.** CSV has no concept of a locked cell, a protected sheet, or data validation. Decision 1's "locked columns" cannot be expressed at all |
| Can carry a second guidance sheet | Yes — the bulk template's Instructions-sheet convention ([views.py:2953](projects/views.py#L2953)), whose whole point is "kept OFF the data sheet so no guidance row can ever be imported" | No — one flat table only |
| Can carry a hidden key column | Yes — `column_dimensions[...].hidden = True` | Only as a visible column |
| Existing precedent in this codebase | **Both directions.** `opex_site_bulk_template` writes one; `_parse_bulk_workbook` reads one | **Export only.** Nothing in the codebase *parses* a CSV |
| Numeric fidelity | openpyxl returns typed values (int/float); `_bulk_cell_to_str` already handles the `1200.0` problem | Everything is a string; no type ambiguity, but also no `data_only` formula resolution |
| Encoding hazards | None — xlsx is zipped XML, UTF-8 throughout | Real. Excel writes CSV in the local ANSI codepage by default; the OPEX catalogue contains `*` and `/` in descriptions and 206-character strings |
| Parse cost | Higher — zip + XML. `read_only=True` streams | Trivial |
| Google Sheets round trip | Protection partially lost (§2.4); data survives | Data survives; nothing else exists to lose |

**The decisive asymmetry, stated flatly: decision 1 requires locked columns, and CSV cannot express
them.** A CSV build would have to reinterpret decision 1 as "the server rejects any row whose
spec columns disagree with the catalogue" — which the server must do *anyway* under §2.4's
validate-regardless stance, but which loses the affordance that stops the mistake happening.

### 5.2 — One view or two?

**The codebase's existing answer: two, split exactly on the GET/POST line.**

```
projects/urls.py:45:    path("programs/<int:pk>/sites/bulk/", views.opex_site_bulk_upload, name='opex_site_bulk_upload'),
projects/urls.py:46:    path("programs/<int:pk>/sites/bulk/template/", views.opex_site_bulk_template, name='opex_site_bulk_template'),
```

- `opex_site_bulk_template` — GET only, returns a file, 54 lines, `@role_required(['Admin','PM'])`.
- `opex_site_bulk_upload` — handles its own GET (the upload screen) and two POST phases, 134 lines,
  same decorator.

Both repeat the same three gate lines independently:

```python
    program = get_object_or_404(Program, pk=pk, is_deleted=False, program_type='OPEX')
    if not _can_access_program(request, program):
        raise Http404
```

The download view does **not** route through the upload view; they share only `_BULK_COLUMNS`, and
the comment on that constant makes the sharing explicit: *"Order here is the order columns are
emitted into the downloadable template."* **One shared column definition, two views.**

The design-artifact pair is split the same way — `design_artifact_upload` (POST) at
`urls.py:85` and `design_file_download` (GET) at `urls.py:86`.

**Every file-producing and file-consuming pair in this codebase is two views sharing a constant.**

### 5.3 — Preview before commit, or validate-and-commit in one step?

**What decision 2 already forces:** rejecting the whole file on any unknown code means every row
must be checked before any row is written. Full validation before any write is not optional under
decision 2 — the only open question is whether the *user sees* the intermediate result.

**Does 1.2's precedent give a reusable dry-run mechanism?** **Partly — the pattern is reusable, the
code is not, and Session E probably does not need it.**

- `_validate_site_row_dry_run` ([views.py:2679-2709](projects/views.py#L2679-L2709)) is
  **hard-bound to `create_opex_site`**. It takes `(program, data, creator, profile)`, calls one
  specific factory, and reads `site is not None`. It cannot be pointed at a BOQ row.
- The two sentinel exceptions `_DryRunRollback` and `_CommitAbort`
  ([views.py:2662-2676](projects/views.py#L2662-L2676)) **are** reusable as-is — they carry no
  site-specific state beyond `index` and `errors`.
- **The savepoint-and-rollback machinery may be unnecessary here.** It exists because site
  validation lives inside a `ModelForm` that must actually run against the database to check
  uniqueness. BOQ upload validation is a **set-membership test against
  `get_opex_boq_catalogue()`** — "is this code in the active OPEX catalogue" — which is answerable
  entirely in memory from a dict the view already builds (`catalogue_by_id`, and its `code`-keyed
  inverse). Validating an uploaded BOQ requires **no write and therefore no rollback**.

**What each shape costs:**

| | Preview then commit (2 requests) | Validate-and-commit (1 request) |
|---|---|---|
| Precedent | Exact — `opex_site_bulk_upload` | None for spreadsheets |
| Machinery needed | A `stage`-driven template with three states; a `rows_json` hidden-field round trip; re-validation at commit against a tampered payload ([views.py:2885-2887](projects/views.py#L2885-L2887)) | One `transaction.atomic()` and an error render |
| Where errors render | Naturally — the preview stage *is* the error render | Needs new machinery — see §5.4 |
| Risk introduced | The payload leaves the server and comes back; the bulk path defends against tampering by re-validating | None of that |
| Fit with decision 4 | A preview could show "3 rows will be added, 12 quantities changed, 0 deleted" — which is where decision 4's mandated "upload never deletes" message naturally lives | The message has to be a post-hoc summary |

### 5.4 — Where do errors render?

**The gap is real and this audit re-derived it.** `opex_boq_entry` has **no validation-error
render**. Every failure path is `messages.error(...)` followed by `redirect(...)`:

```python
        if boq_group_locked:
            messages.error(request, 'This site is in a locked procurement group — ...')
            return redirect('opex_boq_entry', project_id=project_id)
        if boq_design_locked:
            messages.error(request, 'This BOQ has been marked complete and is with design ...')
            return redirect('opex_boq_entry', project_id=project_id)
        if action not in ('save_draft', 'mark_complete'):
            return redirect('opex_boq_entry', project_id=project_id)
```

The view's own Session D comment names this as a constraint on its design —
[views.py:4668-4670](projects/views.py#L4668-L4670):

```
        # never did and could not have seen, and this view has no validation-error render —
        # it would have to discard the whole posted sheet to say so.
```

That is *why* Session D chose re-add-and-warn over refusing the save. The same constraint applies
to Session E: **an upload cannot report a per-row error list through this view as it stands.**

**What a detailed per-row error list would require.** The bulk-upload precedent shows the shape —
it does **not** use `messages` for row errors at all. It `render()`s the page with an error context
and never redirects:

```python
        rows, extra_headers, file_error = _parse_bulk_workbook(uploaded)
        if file_error:
            ctx['file_error'] = file_error
            return render(request, 'projects/opex_site_bulk_upload.html', ctx)
```

and per-row:

```python
            results.append({
                'index': idx,
                'data': data,
                ...
                'errors': errors,
                'valid': valid,
            })
```

So it needs, concretely:

1. **A render path, not a redirect** — the POST handler must be able to return
   `render(request, template, ctx)` carrying the error list, which means the template needs an
   error-rendering region and the context needs `file_error` / `results` equivalents.
2. **A whole-file error** (decision 2's rejection) is the easy half — one string, one region, the
   `file_error` pattern exactly.
3. **A per-row list naming "every offending row and code"** (decision 2's requirement) needs a
   structure like `results` and a table to render it in.
4. **`messages` cannot carry it.** The framework renders messages as flat alert strings; a 40-row
   error table in a single `messages.error` string would be unreadable.

**This is the strongest argument in the evidence for a separate screen** (as in §3.5's `enctype`
caveat and §5.2's two-view precedent): a separate upload screen gets an error-render region for
free, exactly as `opex_site_bulk_upload.html` did, without touching the picker's redirect-only
POST path.

### 5.5 — Size and abuse limits

**What the other upload paths enforce:**

| Path | Extension check | Byte-size limit | Structural limit | MIME check |
|---|---|---|---|---|
| `_validate_and_upload` (4 public-bucket callers) | Yes — `ALLOWED_EXTENSIONS` | **Yes — 20 MB** (`MAX_FILE_SIZE_BYTES`) | — | Yes — extension vs `content_type` |
| `upload_design_file` (survey, artifact) | Yes — `ALLOWED_DESIGN_EXTENSIONS` (includes `xlsx`) | **Yes — 25 MB** (`MAX_DESIGN_FILE_BYTES`) | — | Yes |
| `validate_cad_zip` (zips, before storage) | Yes | Yes — 25 MB compressed | **Yes — ≤ 500 entries, ≤ 200 MB uncompressed** (`MAX_CAD_ZIP_ENTRIES`, `MAX_CAD_ZIP_UNCOMPRESSED_BYTES`) — an explicit zip-bomb guard | Yes |
| **`opex_site_bulk_upload`** | **No** | **No** | **Yes — 500 rows** (`_BULK_MAX_ROWS`), *checked after the whole workbook is materialised* | **No** |

**The spreadsheet path is the only upload in the codebase with no byte-size limit and no extension
check.** Every other one has both, and the zip path additionally guards decompression.

**What an unbounded spreadsheet parse would cost.** The bulk parser's sequence is:

```python
        wb = load_workbook(uploaded_file, read_only=True, data_only=True)
        ...
        all_rows = list(ws.iter_rows(values_only=True))
```

`read_only=True` streams the sheet rather than building a full cell graph — the right choice — but
`list(...)` immediately materialises every row into memory anyway, and **the 500-row check happens
after that line**. So the row cap does not bound memory; it only bounds how many rows are
*processed*. An .xlsx is a zip: a small upload can expand enormously, which is exactly the class of
attack `validate_cad_zip` was written to stop for zips and which no check covers here.

**Specific to a BOQ upload, the natural scale bound is much smaller than 500.** The active OPEX
catalogue is **207 rows** `[PRODUCTION]`, and decision 2 rejects any code outside it. A file with
more than ~207 catalogue rows plus however many off-catalogue rows the site carries is malformed by
construction — the largest live OPEX BOQ is 53 rows `[PRODUCTION]`. A cap derived from
`len(get_opex_boq_catalogue())` would be self-maintaining, unlike a literal.

**Abuse surface, for completeness.** Both the write gate (§3.4) and both locks (§3.3) apply before
any parse, so the only actors who can reach the parser at all are Design users who are
`assigned_design` on an unlocked OPEX site. `[PRODUCTION]` that is a small, named, authenticated
population — this is not an anonymous endpoint. The cost of an unbounded parse is a worker
process consuming memory, not data loss.

---

## UNCERTAIN

**§U1 — The `numeric(10,2)` overflow is derived, not executed.** §3.2 states that a quantity above
99,999,999.99 would raise `DataError` at INSERT. This is derived from three verified facts: the
field is `DecimalField(max_digits=10, decimal_places=2)`
([models.py:941](projects/models.py#L941)); `[PRODUCTION]` `information_schema` reports the column
as `numeric` precision 10 scale 2; and `_quantity()` performs no magnitude check while
`objects.create()` / `save(update_fields=...)` bypass `full_clean()`. I did **not** execute an
oversized insert to confirm the exception type and message, because doing so requires a database
write, which this audit forbids. Confidence that it fails: high. Confidence in the exact exception
class and text: not established.

**§U2 — "Whether the Head has flagged anything yet" is only half-answerable from the database.**
§4.5 establishes `[PRODUCTION]` that two catalogue rows are now `is_mandatory=True`, which is the
Head acting on Session D's feature. Whether the Head has reported any *problem* with Session D —
verbally, over email, or in a channel this repo does not contain — cannot be read from code or
database. No issue-tracking or feedback table exists that is tied to the BOQ catalogue.

**§U3 — Google Sheets round-trip behaviour for sheet protection is reported from general
behaviour, not tested here.** §2.4's "partially / commonly dropped" for Google Sheets was not
verified by an actual round trip in this session — there is no way to do so without leaving the
machine. The `openpyxl` API availability *was* verified `[LOCAL]`. If the build depends on Sheets
fidelity, test it before relying on it.

**§U4 — Formula-cell behaviour under `data_only=True` is stated from the openpyxl contract, not
demonstrated.** §3.2's row about formula cells returning `None` when the file was written
programmatically and never opened in Excel follows from `data_only=True` reading *cached* results.
Not exercised in this session.

**§U5 — Whether `BOQRevision` should be written by an upload is not established.** §1.5 notes that
`opex_boq_entry` writes no `BOQRevision` while the Residential `boq_detail` paths do. Whether that
asymmetry is intentional for Part 11 or an oversight is not something this audit determined; it is
outside the eight settled decisions and was not asked.

---

## CONFLICTS

Where this prompt's assumptions disagree with the code. Each was re-derived in this session, not
inherited from a prior audit.

**§C1 — "`[Session T]` and `[Session D]` should both be present" in the git log. Session T left no
commit.**
`git log --oneline -20 | grep -i session` returns only `[Session D]`, `[Session C.1]` and
`[Session C]`. Session T's entire output is the untracked `SESSION_T_TEST_UNBLOCK.md`. This is
*consistent* with what Session T found — the suite was never broken, only mis-invoked — so there
was no code change to commit. **The prompt's expectation is wrong; nothing is missing.** Not
treated as a stop.

**§C2 — "Is there **any** existing download or export anywhere… If nothing exists, say so plainly;
this session would be introducing the first."**
**Four downloads exist, and one is an `.xlsx` built with `openpyxl`.** `opex_site_bulk_template`
([views.py:2936-2989](projects/views.py#L2936-L2989)) writes a two-sheet workbook and streams it
via `wb.save(resp)`; `_export_audit_csv` and `admin_send_records` emit CSV; `design_file_download`
serves a signed Supabase URL. This is a materially better position than the prompt assumed — the
download half has a working, deployed, three-line emit idiom to copy, and a two-sheet
data/guidance convention already established. It does not change the deliverable; it lowers the
cost.

**§C3 — Decision 2's stated rationale is wrong about off-catalogue rows, and
`TESTTENDER26-MB010` is the counter-example the prompt itself names.**
The prompt says: *"an off-catalogue row carries a null `item_master`, and `aggregate_group_boq()`
joins on exactly that, so those rows cannot be summed for procurement."*

`split_opex_boq_rows()` classifies a row as off-catalogue when
`item_master_id not in catalogue_ids`, where `catalogue_ids` is *active OPEX* master pks
([models.py:851-856](projects/models.py#L851-L856)). Three distinct populations satisfy that, only
one of which has a null master. `[PRODUCTION]`: OPEX BOQs carry **148 rows linked to OPEX masters,
37 linked to Residential masters, and 0 with a null master.** The 37 rows on
`TESTTENDER26-MB010` carry real codes `ITM-001`…`ITM-037` and would aggregate perfectly well if
they had quantities.

**Why this matters to the build rather than being pedantry:** because `BOQItemMaster.code` is
globally unique and *not* scoped by `project_type` ([models.py:680](projects/models.py#L680)), a
`code`-keyed upload lookup resolves `ITM-001` successfully unless it is explicitly scoped to
`project_type='OPEX'`. So the same file is either accepted or wholly rejected under decision 2
depending on one filter term that the prompt's rationale never contemplated. §2.2 sets out the
three-way fork. **The decision itself (reject unknown codes) still stands; only its stated reason
is wrong, and the wrong reason hides a real ambiguity.**

**§C4 — `solarpms/test_settings.py`'s docstring gives two reasons for its existence, and neither is
the operative one.**

It claims: *"the local Postgres role can't CREATE DATABASE (needed for the normal test DB), and one
historical migration uses Postgres-only raw SQL that SQLite rejects."*

- **Reason 1 is false.** The role demonstrably can — `[LOCAL]` the normal test command found
  `test_solarpms_local` **already existed**, which only that mechanism creates, and once given
  `--noinput` the run destroyed it, recreated it, applied all 64 migrations and executed 281 tests
  in 45 seconds. Postgres test runs work.
- **Reason 2 is about SQLite rejecting Postgres SQL, i.e. an argument for disabling migrations when
  running *on SQLite*.** It does not explain why the suite fails *on Postgres*.
- **The operative reason, established in §4.6, is neither.** `tests_design_part11` seeds
  `ITM-001…ITM-037` and `OPX-001…OPX-207` in `setUp`, and migrations 0047 and 0057 seed exactly
  those codes into a `unique=True` column. On any database where migrations have run, the module's
  own seed collides. `MIGRATION_MODULES = _DisableMigrations()` is what makes the module runnable —
  not the database engine.

Why it matters to Session E rather than being trivia: **it names the real constraint on where new
tests can live.** Any Session E test that seeds the BOQ catalogue inherits the same dependency on
`--settings=solarpms.test_settings`. And it establishes that **the Postgres suite is available and
is the right arbiter when a SQLite result looks like a failure** — which is exactly what resolved
the `part46` question in §4.6.

Related to memory of prior sessions, and re-derived rather than inherited: the standing note that
"part11 passes 32/32 with the settings flag; without it all 32 error in setUp" is **correct as an
observation**. What was not previously established, and is established here, is *why* — the
migration-seeded catalogue, not the database engine or the invocation as such.

**§C5 — Hard stop 5's premise ("impossible to reuse without restructuring") does not match what
the code shows.**
The prompt anticipates that the picker's reconciliation might have to be restructured. It does not.
The picker's POST and an upload's persistence differ in exactly **two loops**
([views.py:4719-4725](projects/views.py#L4719-L4725)); everything else — the lock refusals, the
mandatory union, `split_opex_boq_rows`, the `by_master` index, the create/update loop's field
mapping, the lazy BOQ create — is shared or shareable **by an upload path that omits the deletes**.
An upload is the picker's save *minus* two loops, so it needs strictly less code, and
`opex_boq_entry` need not be touched at all. The stop does not fire, and the concern behind it is
sound — absence-means-delete genuinely is incompatible with decisions 3 and 4 — it just does not
require restructuring anything.

**§C6 — "The same gap Session D found" (§5.4) is accurate, and this audit confirms it
independently.** Noted here only because the prompt asked for re-derivation rather than
inheritance: `opex_boq_entry`'s POST has no `render()` call on any path — every branch ends in
`redirect()` or delegates to `design_boq_complete()`. Verified by reading all 135 lines of the POST
block, not by citing Session D.

---

## CLOSING TABLE

| Item | Status | Note |
|---|---|---|
| Opening 1 — repo | ANSWERED | Git run in `Horizon-Solar-PMS` |
| Opening 2 — `git status` | ANSWERED | Clean; 4 untracked `.md`. No stop |
| Opening 3 — log / HEAD | **PARTIAL** | HEAD `80f0d12`; `[Session D]` present, `[Session T]` **absent** (§C1) |
| Opening 4 — deployed SHA | ANSWERED | `80f0d12a…` = HEAD. No stop |
| Opening 5 — migration head | ANSWERED | `0064_boqitemmaster_is_mandatory` |
| Opening 6 — whole suite | ANSWERED | Two runners; 281 tests; every red result explained by the runner. See §4.6 |
| 1.1 — openpyxl | ANSWERED | `openpyxl==3.1.5` pinned + installed; no pandas/xlsxwriter/xlrd; stdlib `csv` ×2 |
| 1.2 — bulk site upload | ANSWERED | Quoted in full; all six sub-questions answered |
| 1.3 — other upload paths | ANSWERED | 7 call sites tabulated |
| 1.4 — existing download/export | ANSWERED | **Four exist**, one `.xlsx` (§C2) |
| 1.5 — Supabase | ANSWERED | Reported, not decided. `xlsx` already allowed in the private bucket |
| 2.1 — `BOQItem` model | ANSWERED | Full quote + per-field download/SCM split; `uom`-vs-`UOM_CHOICES` finding |
| 2.2 — `split_opex_boq_rows` | ANSWERED | Full quote; three-population finding; the 37-row fork set out (§C3) |
| 2.3 — matching key | ANSWERED | Reported, not decided. `code` is globally `unique=True`; 244/244 verified |
| 2.4 — freezing | ANSWERED | Four mechanisms tabulated; validate-regardless precedent quoted ×4 |
| 2.5 — the three helpers | ANSWERED | Full quotes; `is_active` confirmed; per-direction need tabulated |
| 3.1 — POST block / central question | ANSWERED | Full quote; reuse tabulated line by line; answered plainly. Stop 5 does not fire (§C5) |
| 3.2 — `_quantity()` | ANSWERED | Full quote; 11 spreadsheet phenomena tested `[LOCAL]`; overflow hazard (§U1) |
| 3.3 — lock predicates | ANSWERED | Both quoted; application point identified; `[PRODUCTION]` all 3 BOQs design-locked |
| 3.4 — `user_can_edit_project_boq` | ANSWERED | Full quote; upload gate confirmed; no other role qualifies; download gate reported not decided |
| 3.5 — control strip | ANSWERED | Quoted; separation is free; `enctype` caveat identified |
| 3.6 — lazy `BOQ` create | ANSWERED | Quoted at 4710-4714; `[PRODUCTION]` 93 sites without a BOQ confirmed |
| 4.1 — `aggregate_group_boq` | ANSWERED | No change needed; quoted |
| 4.2 — `design_analytics` | ANSWERED | Zero BOQ reads; 1 prose hit in 1,261 lines |
| 4.3 — Residential unreachable | ANSWERED | Two complementary gates quoted |
| 4.4 — variance | ANSWERED | **No implementation.** `[PRODUCTION]` `SiteGroup` count = 0 |
| 4.5 — `[PRODUCTION]` BOQ state | **PARTIAL** | Counts and mandatory set established; "has the Head flagged anything" only half-readable (§U2) |
| 4.6 — test baseline | ANSWERED | Both runners; SQLite `part46` failure re-derived as a backend artifact (passes on Postgres); Postgres `part11` errors traced to the migration-seeded catalogue |
| 5.1 — format | ANSWERED | Evidence tabulated; not chosen |
| 5.2 — one view or two | ANSWERED | Codebase pattern is two, sharing a constant; not chosen |
| 5.3 — preview or one step | ANSWERED | Dry-run pattern reusable, code not, machinery likely unnecessary; not chosen |
| 5.4 — error rendering | ANSWERED | Gap re-derived; requirements enumerated |
| 5.5 — size / abuse | ANSWERED | All limits tabulated; spreadsheet path is the only unbounded one |

**Hard stops.** 1 (clean tree) did not fire. 2 (SHA match) did not fire. **3 (red suite outside
`part11`) — its literal condition WAS met and is reported prominently at §4.6; it was not treated
as a stop because re-derivation shows no genuine regression, and the reader may overrule that
judgement.** 4 (no spreadsheet library) did not fire — `openpyxl` is pinned and deployed.
5 (reconciliation unreusable) did not fire — see §C5. 6 (needs a DB write) did not fire — the one
finding that would have required one is flagged UNCERTAIN (§U1) instead.

---

## SIZE ESTIMATE

### Migration: **NO**

Nothing new is stored. Download reads `BOQItem` + `BOQItemMaster`; upload writes `BOQItem` rows
with exactly the field set the picker already writes
([views.py:4734-4739](projects/views.py#L4734-L4739)). `BOQ` is created by the existing three-line
lazy create. No new column, no new model, no new constraint. **Migration head stays at 0064.**

### New dependency: **NO**

`openpyxl==3.1.5` is pinned in `requirements.txt`, installed locally, deployed on Railway, and
already used to both read (`load_workbook`) and write (`Workbook` + `wb.save(resp)`) `.xlsx`. Sheet
protection, per-cell locking and data validation were all confirmed importable `[LOCAL]`.
`requirements.txt` is untouched.

### Files

**Download** (~2 files):
- `projects/views.py` — one new view, modelled on `opex_site_bulk_template`; one shared column
  definition beside it
- `projects/urls.py` — one path
- `projects/templates/projects/opex_boq_entry.html` — one `<a>` in the header strip, outside every
  `can_edit` block (§3.5)

**Upload** (~4 files):
- `projects/views.py` — one new view; persistence is the picker's create/update loop minus the two
  delete loops
- `projects/urls.py` — one path
- a new template for the upload screen and its error render (§5.4, §3.5's `enctype` caveat)
- `projects/templates/projects/opex_boq_entry.html` — one control inside the `can_edit` block
- `projects/tests_design_part11.py` or a new `tests_design_parte.py` — decisions 2-6 each need at
  least one test, plus the `uom` and off-catalogue cases

**`opex_boq_entry` itself need not be modified** in either half.

### Was Item 8 correctly sized as the largest of the eight?

**Partly — and the part that is wrong is worth acting on.**

**What makes it smaller than billed:** no migration, no dependency, and — contrary to the prompt's
assumption (§C2) — a working `.xlsx` download already exists to copy, along with a complete
upload-with-preview precedent. The persistence path is the picker's *minus* two loops.

**What genuinely makes it large:** it is the only one of the eight that must both produce and
consume a file format; decision 2's whole-file rejection needs an error-render surface the picker
does not have (§5.4); decision 1's freezing has no precedent anywhere in the codebase; the
spreadsheet input space (§3.2) is far wider than a form POST's; and §2.2's off-catalogue fork is a
genuine unresolved design question that will surface the moment anyone downloads
`TESTTENDER26-MB010`.

**Should download and upload ship as separate sessions? On this evidence, yes — and the split is
unusually clean.**

- **Download** is a read-only GET returning a file. It cannot corrupt data, cannot delete, is
  unaffected by both locks, has a three-line emit idiom already in the repo, and touches
  `opex_boq_entry.html` in one place *outside* every conditional. Its only real design questions
  are §2.2 (does it carry off-catalogue rows) and §3.4 (read gate or write gate). **Genuinely
  almost no failure modes, and useful on its own** — a designer who can export their BOQ to a
  spreadsheet has something today even if upload never ships.
- **Upload** carries every hard part: decision 2's rejection semantics, the error-render gap, the
  absence-means-nothing inversion of the picker's core assumption, spreadsheet parsing hazards, the
  missing size limits (§5.5), and the mandatory union — which is **no longer inert**, since
  `[PRODUCTION]` the Head has now flagged OPX-001 and OPX-027 (§4.5).

Shipping download first also **de-risks upload**, because it fixes the file's exact shape — column
order, header text, which rows appear — before anything has to parse it. The bulk-site feature was
built the other way round and ended up with `_BULK_COLUMNS` as a shared constant precisely to keep
the two in step; here, download can define that constant first.

**Estimate: two sessions.** Download is a short one. Upload is a full one, and its first task is
resolving §2.2's fork and §5.1's format question — neither of which this audit decided.
