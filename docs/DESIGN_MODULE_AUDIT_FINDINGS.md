# DESIGN MODULE — CODEBASE AUDIT FINDINGS

**Session type:** investigation only. No code written, no models/migrations/templates/views/URLs created or modified.
**Repo root audited:** `C:\SolarPMS\Horizon-Solar-PMS` (the git repo; `C:\SolarPMS` itself is not a git repository).
**Django apps:** exactly one — `projects`. (`solarpms/settings.py:50-60`, `INSTALLED_APPS`.)
**Django 6.0.6**, DB via `dj-database-url`, files on Supabase, WhatsApp via Interakt, email via ZeptoMail.

---

## HARD STOP CONDITIONS — RESULT

| # | Condition | Result |
|---|---|---|
| 1 | No parent entity above `Project` representing a tender/program | **NOT triggered.** `Program` exists — `projects/models.py:170-254`. |
| 2 | `BOQ` items stored as free text rather than referencing an item master model | **TRIGGERED.** See below. |
| 3 | `user_can_manage_project()` does not exist / is not canonical | **NOT triggered.** Exists at `projects/permissions.py:32-59` and is the canonical path. |
| 4 | Site creation (single and bulk) not present | **NOT triggered.** Both exist for OPEX. See §2 for the scope caveat. |

### HARD STOP #2 — what was found instead

There is **no item master model anywhere in the codebase.** BOQ line items are free text:

- `BOQItem.description = models.TextField()` — `projects/models.py:710`. No FK to any catalogue.
- `BOQItem.category` is a 5-value `CharField` choice list (`Solar Modules / Structure / Inverter / BOS / Other`) — `projects/models.py:689-695`.
- `BOQItem.uom` is a 7-value `CharField` choice list — `projects/models.py:697-705`.
- The "standard" 37-item list is a **hardcoded Python function returning dicts**, not a table:
  `get_standard_boq_items()` — `projects/models.py:612-655`. Each dict is `{serial_no, category, description, uom}`.
- The only master-data model in the BOQ neighbourhood is `Vendor` / `VendorCategory` / `VendorBrand`
  (`projects/models.py:541-609`), which supplies the **make/brand** dropdown — not the item itself.
  `BOQItem.make_preference` and `BOQItem.ordered_vendor` are FKs to `Vendor` (`models.py:711-717`, `721-727`).

Consequence: item text is copied **by value** into every `BOQItem` row at BOQ creation time. Editing
`get_standard_boq_items()` changes nothing on existing BOQs. There is no stable item identity that a
grouped, cross-site BOQ handoff to SCM could aggregate on other than the raw description string.

Reported as instructed; **no workaround attempted, no design proposed.**

---

## 1. Tender / Program parent entity

### What exists

**Model:** `Program` — `projects/models.py:170-254`.

Full field list (`models.py:191-238`):

| Field | Type | Notes |
|---|---|---|
| `program_type` | `CharField(max_length=20, choices=PROGRAM_TYPE_CHOICES)` | **This is the type discriminator.** Choices: `OPEX`, `CAPEX` only (`models.py:184-187`). Residential is deliberately absent. |
| `name` | `CharField(200)` | e.g. "IPGCL Delhi Tender" |
| `client_name` | `CharField(200)` | |
| `status` | `CharField(20, choices=STATUS_CHOICES)`, default `'Draft'` | Choices aliased directly from `Project.STATUS_CHOICES` (`models.py:189`) |
| `short_tender_code` | `CharField(20, blank=True, default='')` | OPEX-only, required by form. Globally unique — enforced in `ProgramForm.clean()` (`forms.py:533-568`), **not** by a DB constraint. |
| `total_capacity` | `DecimalField(10,2)` null | MW, storage only |
| `expected_completion_date` | `DateField` null | |
| `planned_site_count` | `PositiveIntegerField` null | informational only, never enforced as a cap |
| `tender_reference_number` | `CharField(100, blank, default='')` | OPEX |
| `bid_value` | `DecimalField(14,2)` null | OPEX |
| `award_date` | `DateField` null | OPEX |
| `ppa_reference` | `CharField(100, blank)` | OPEX |
| `ppa_signed_date` | `DateField` null | OPEX |
| `ppa_per_unit_rate` | `DecimalField(10,4)` null | OPEX |
| `ppa_escalation_percentage` | `DecimalField(5,2)` null | OPEX |
| `ppa_escalation_frequency` | `CharField(50, blank)` | OPEX |
| `financing_partner_name` | `CharField(200, blank)` | CAPEX placeholder |
| `financing_assistance_type` | `CharField(100, blank)` | CAPEX placeholder |
| `created_at` / `updated_at` | auto | |
| `created_by` | FK `auth.User`, `PROTECT`, null | |
| `is_deleted` / `deleted_at` | soft delete | No custom manager — the default manager returns soft-deleted rows too (deliberate, so uniqueness checks see them). |

All PPA and CAPEX financing fields are **storage only** — no calculation logic exists anywhere
(`models.py:224-230`).

**Relationship to `Project`:** `Project.program` — `projects/models.py:49-55`.
- FK name: `program`; `related_name='sites'`; `null=True, blank=True`; `on_delete=models.PROTECT`.
- Forward-only: pre-existing projects are never backfilled (`models.py:45-48`).
- Invariants enforced in `Project._validate_program_link()` (`models.py:128-151`), called on **every**
  `save()` (`models.py:155`): Residential may never link to a Program; `Project.project_type` must equal
  `Program.program_type`. Raises `ValidationError`, not `IntegrityError`.

**Rollup:** compute-live only, no denormalised counters.
`program_rollup_annotations()` (`models.py:263-279`) for list views, `get_program_rollup(program)`
(`models.py:282-298`) for detail. Both group by `Project.status` and exclude `is_deleted=True`.

**Is `Project` the unit representing a single site?** **Yes.** There is **no separate site model.**
A "site" is a `Project` row with `program` set and `project_type` in (`OPEX`, `CAPEX`).
`Project.site_code` (`models.py:58`, `CharField(30, null, blank)`) is the user-entered per-site code used
to compose `project_id` as `{short_tender_code}-{site_code}` (e.g. `IPGCL26-S045`).

**Program permission helper:** `user_can_manage_program()` — `permissions.py:154-174`. Derives authority
from child sites (manages any non-deleted child ⇒ manages the Program). View-layer gate is
`_can_access_program()` — `views.py:2305-2317` (Admin/CEO always; else `user_can_manage_program` OR
`program.created_by_id == request.user.id`).

**Views/templates:** `program_list` `views.py:2322`, `program_detail` `views.py:2359`,
`program_create` `views.py:2384`, `program_edit` `views.py:2406`, `program_delete` `views.py:2439`.
Form: `ProgramForm` `forms.py:483-575`. Templates: `projects/templates/projects/program_list.html`,
`program_detail.html`, `program_form.html`. URLs: `projects/urls.py:44-45` and neighbours.

### What does not exist

- No `Program`-level PM, coordinator, design lead, or any assignment field. Authority is derived only.
- No auto-generated `program_id` (deliberate — `Program.reference_display`, `models.py:247-254`).
- No Program-level workflow, phases, tasks, BOQ, or documents. `Program` groups and reports only.

### Contradictions with the prompt's assumptions

None for this item.

---

## 2. Site creation

### Single site creation (OPEX, under a Program)

- **Model:** `Project` — `projects/models.py:5-167`.
- **Core function:** `create_opex_site(program, data, creator, profile=None)` — `views.py:2467-2519`.
  Request-independent; callers own access control.
- **View:** `opex_site_create(request, pk)` — `views.py:2522-2544`. Decorated
  `@login_required @role_required(['Admin', 'PM'])`, plus `_can_access_program()`.
- **Form:** `OpexSiteForm` — `forms.py:578-682`.
- **Template:** `projects/templates/projects/opex_site_form.html`.
- **URL:** `projects/urls.py` (`programs/<int:pk>/sites/add/` region; bulk routes at `urls.py:44-45`).

### Bulk site creation (OPEX)

- **View:** `opex_site_bulk_upload(request, pk)` — `views.py:2708-2841`. Same gate as single-add.
- **Input format:** `.xlsx` (openpyxl). Reads only the sheet named `Sites`; falls back to the active
  sheet. Column map `_BULK_COLUMNS` — `views.py:2563-2572`:
  `Site Code`(req), `Site In-Charge Name`(req), `Site In-Charge Phone`(req), `Site In-Charge Email`,
  `Site Address`(req), `City`(req), `State`, `Capacity (kW)`.
  Per-upload cap `_BULK_MAX_ROWS = 500` (`views.py:2575`).
- **Flow:** two POST phases — `preview` (parse + per-row dry-run validation via
  `_validate_site_row_dry_run`, `views.py:2595-2625`, which runs the *real* `create_opex_site` inside a
  transaction and force-rolls-back with `_DryRunRollback`) then `commit` (all-or-nothing inside one
  `transaction.atomic()`, `views.py:2799-2824`).
- **In-file duplicate detection:** `_bulk_infile_duplicate_indices()` — `views.py:2691-2705`.
- **Template download:** `opex_site_bulk_template` — `views.py:2844-2880+`.
- **Template:** `projects/templates/projects/opex_site_bulk_upload.html`.

### Fields populated at creation (OPEX site)

Set explicitly in `create_opex_site` (`views.py:2498-2513`):
`program`, `project_type='OPEX'` (forced), `customer_name = program.client_name` (frozen copy),
`status='Draft'`, `assigned_pm` = creator's profile **only if** creator's role is `PM` else `None`,
`created_by`, `project_id = form.composed_project_id` (explicit — bypasses `generate_project_id()`).

From the form (`forms.py:604-607`): `site_code`, `customer_contact_person` (reused as Site In-Charge
Name, made required at `forms.py:635`), `customer_phone` (Site In-Charge phone), `customer_email`
(optional), `site_address`, `city`, `state`, `capacity_kw` (optional).

**Left null / unset at OPEX site creation:** `contract_value`, `survey_date`,
`target_commissioning_date`, `assigned_design`, `coordinators` (empty M2M), `activated_at`,
`commissioned_at`, `zoho_crm_id`, `zoho_deal_id`, `cascade_scheduling` (defaults False),
`is_deleted`/`deleted_at` (defaults). `assigned_pm` is null when an Admin creates the site.

### Residential / CAPEX creation (separate path)

- **View:** `project_create` — `views.py:1920-1947`, `@role_required(['PM'])`.
- **Form:** `ProjectCreateForm` — `forms.py:238-302`. **OPEX is stripped from the choice list**
  (`forms.py:265-275`) and rejected again in `clean_project_type()` (`forms.py:282-290`).
- **Template:** `projects/templates/projects/project_form.html`.
- Sets `assigned_pm = request.user.profile`, `status='Draft'`, `created_by`; `project_id` generated in
  `Project.save()` via `generate_project_id()` (`utils.py:7-43`) as `HRP-{RES|CAP}-{YEAR}-{NNN}`.
- **There is no bulk creation path for Residential or CAPEX.** Bulk is OPEX-only.

### Per-site status field

`Project.status` — `models.py:89`. Values (`models.py:15-22`):
`Draft`, `Active`, `In Progress`, `Commissioned`, `On Hold`, `Cancelled`.
This is the only per-site status field. There is **no design-specific status field on `Project`.**

### Contradictions with the prompt's assumptions

- The prompt says "site creation (single and bulk)". Bulk exists **only for OPEX sites under a Program**.
  Residential and CAPEX have single-creation only.

---

## 3. BOQ and item master

### What exists

| Model | File / lines | Attached to |
|---|---|---|
| `BOQ` | `models.py:658-683` | `OneToOneField(Project, related_name='boq')` — **one BOQ per Project** |
| `BOQItem` | `models.py:686-734` | `ForeignKey(BOQ, related_name='items')` |
| `BOQRevision` | `models.py:737-751` | `ForeignKey(BOQ, related_name='revisions')` |
| Item master | — | **DOES NOT EXIST** (see Hard Stop #2) |

**`BOQ` fields:** `project` (O2O, CASCADE), `submitted_by` (FK UserProfile, SET_NULL),
`submitted_at` (DateTime null), `status` (CharField 20, choices `Draft` / `Submitted` /
`Acknowledged` / `Revision Requested`, default `Draft`), `version` (IntegerField default 1),
`notes` (TextField null).

**`BOQItem` fields:** `boq` (FK CASCADE), `serial_no` (IntegerField), `category` (CharField 20 choices),
`description` (**TextField — free text**), `make_preference` (FK `Vendor`, SET_NULL),
`uom` (CharField 10 choices), `boq_quantity` (Decimal 10,2 null — Design's estimate),
`ordered_quantity` (Decimal 10,2 null — SCM's actual), `ordered_vendor` (FK `Vendor`, SET_NULL),
`is_standard_item` (Boolean default True — only `False` rows can be deleted). `Meta.ordering = ['serial_no']`.

**`BOQRevision` fields:** `boq` (FK CASCADE), `revised_by` (FK UserProfile SET_NULL),
`revised_at` (auto_now_add), `version` (IntegerField), `reason` (TextField),
`snapshot` (**JSONField** — full item list serialised at transition). `Meta.ordering = ['-version']`.

### How the Residential flow pre-populates a BOQ — traced code path

1. `boq_detail(request, project_id)` — `views.py:4162-4341`.
2. Role gate at `views.py:4175`: `if role not in ('Design', 'SCM', 'PM', 'Project Coordinator', 'Admin'): return HttpResponseForbidden()`.
3. `views.py:4179-4182` — try `project.boq`; on `BOQ.DoesNotExist` set `boq = None`.
4. `views.py:4184-4196` — **auto-creation happens only for a `Design`-role user**:
   ```python
   if boq is None:
       if role == 'Design':
           boq = BOQ.objects.create(project=project)
           BOQItem.objects.bulk_create([
               BOQItem(boq=boq, **item_data)
               for item_data in get_standard_boq_items()
           ])
       else:
           return render(... 'boq': None ...)
   ```
5. `get_standard_boq_items()` — `models.py:612-655` — returns 37 literal dicts. Values are **copied**
   into new `BOQItem` rows; `is_standard_item` takes its model default `True`.

There is **no project-type branch** in this path. The 37-item list is described as the Residential
standard template but is applied to any project whose BOQ a Design user opens, including OPEX sites.

The Residential template also carries a task named **`BOQ Preparation`** (Phase 3 "Design",
`task_order` 5, `assigned_role=Design`) — `utils.py:455`. That task is not linked to the `BOQ` row by
any FK; the association is by convention only.

### Who may edit BOQ quantities today — exact permission checks

Design (`boq_quantity`, `make_preference`) — `views.py:4205`:
```python
if action in ('save_design', 'submit_design') and role == 'Design' and boq.status in _DESIGN_EDITABLE:
```
where `_DESIGN_EDITABLE = ('Draft', 'Revision Requested', 'Acknowledged')` — `views.py:4203`.

SCM (`ordered_quantity`, `make_preference`, `ordered_vendor`) — `views.py:4249`:
```python
elif action in ('save_scm', 'acknowledge_scm') and role == 'SCM' and boq.status in ('Submitted', 'Acknowledged'):
```

Add row (Design) — `views.py:4285`; delete row (Design, non-standard rows only) — `views.py:4299-4302`.

Standalone endpoints with their own gates:
- `boq_submit` — `views.py:4358`: `if profile.role != 'Design': return HttpResponseForbidden()`; status must be `Draft` or `Revision Requested` (`views.py:4363`).
- `boq_acknowledge` — `views.py:4411`: `if profile.role != 'SCM': ...`; status must be `Submitted` (`views.py:4416`).
- `boq_request_revision` — `views.py:4440`: `if profile.role not in ('PM', 'Project Coordinator'): ...`; status must be `Submitted` or `Acknowledged` (`views.py:4445`).
- `boq_history` — `views.py:4492`: `if profile.role not in ('PM', 'Project Coordinator', 'Design', 'SCM', 'Admin'): ...`.

**None of these BOQ endpoints call `user_can_manage_project()`.** They are role-string gates only —
any user holding the `Design` role can edit any project's BOQ; any `SCM` user can edit any project's
ordered quantities; any PM/Coordinator (not only the project's own) can request a revision.

### Lock / freeze / immutability

- **The only lock is the status gate above.** `BOQ.status == 'Submitted'` is not in `_DESIGN_EDITABLE`,
  so Design editing is blocked while a BOQ sits with SCM. Enforced **in the view only** — there is no
  model-level `save()` guard, no DB constraint, no `is_locked` field.
- `BOQRevision` is described as an immutable snapshot (`models.py:738`) but nothing enforces
  immutability — it is an ordinary model with no override.
- `PostActivationFieldEditForm.clean_contract_value()` (`forms.py:384-396`) blocks changing
  `Project.contract_value` once payment-milestone amounts exist. This is the only freeze-style rule of
  its kind in the codebase and it is unrelated to BOQ.

### What does not exist

- No item master / catalogue model. No SKU, item code, or stable item identity.
- No cross-site or Program-level BOQ aggregation of any kind.
- No BOQ approval by Design Head. The only approval-ish transitions are SCM `Acknowledged` and PM
  `Revision Requested`.
- No BOQ lock/freeze flag, and no BOQ-to-Task FK.

### Contradictions with the prompt's assumptions

- The prompt assumes an item master exists. **It does not.** (Hard Stop #2.)
- `'Acknowledged'` is inside `_DESIGN_EDITABLE` (`views.py:4203`), so Design can still edit quantities
  *after* SCM has acknowledged the BOQ, without any status change or new revision. Flagging as an
  observed contradiction with the "acknowledge = settled" reading; not fixed, per session rules.

---

## 4. File storage

### Upload helper

**There is no dedicated storage-helper module beyond a client factory.** The upload path is a private
function inside `views.py`.

- `get_supabase_client()` — `projects/supabase_storage.py:4-18`. No arguments. Reads `SUPABASE_URL` /
  `SUPABASE_KEY` from `os.environ`, raises `ValueError` if unset, imports `supabase.create_client`
  lazily inside the function.
- `_validate_and_upload(file, supabase_client, bucket, supabase_path, allowed_extensions=None)` —
  `projects/views.py:5696-5718`. Validates extension, size, and MIME; then
  `supabase_client.storage.from_(bucket).upload(path=..., file=file.read(), file_options={"content-type": ...})`.
  Returns the lowercase extension string. Raises `ValueError` on validation failure.

### Signed URL helper

**DOES NOT EXIST.** A repository-wide case-insensitive search for `signed` / `create_signed` /
`sign_url` returns only `Program.ppa_signed_date` and prose comments. Every stored file URL is a
**public** Supabase object URL, built inline at each call site as:

```python
f"{settings.SUPABASE_URL}/storage/v1/object/public/{settings.SUPABASE_BUCKET}/{supabase_path}"
```

(`views.py:5819-5822`, `5963-5966`, `4751-4754`, `6129-6132`.) This string is duplicated at four call
sites; there is no shared helper for it either.

### Path / prefix conventions

| Prefix | Full pattern | Source |
|---|---|---|
| `project-documents/` | `project-documents/{project.project_id}/{uuid4()}_{file.name}` | `views.py:5812-5815` |
| `task-attachments/` | `task-attachments/{project.project_id}/{task.pk}/{uuid4()}_{file.name}` | `views.py:5956-5959` |
| `payment-requests/` | `payment-requests/{project.project_id}/{uuid4()}_{file.name}` | `views.py:4745-4748` |
| `checklist-photos/` | `checklist-photos/{project.project_id}/{task.pk}/{item.pk}/{uuid4()}_{file.name}` | `views.py:6115-6118` |

Bucket: `settings.SUPABASE_BUCKET`, default `'solarpms-files'` — `solarpms/settings.py:27`.
Retention: `FILE_RETENTION_DAYS`, default 90 — `solarpms/settings.py:30`.

### Model fields storing Supabase paths

| Model | Field(s) | Lines |
|---|---|---|
| `ProjectDocument` | `file_name`, `file_url`, `supabase_path` | `models.py:430-432` |
| `TaskAttachment` | `file_name`, `file_url`, `supabase_path` | `models.py:818-820` |
| `PaymentRequest` | `invoice_document_name`, `invoice_document_url`, `invoice_document_path` | `models.py:1312-1314` |
| `DesignSubmission` | `file_name`, `file_url`, `supabase_path` (all `blank=True, default=''`) | `models.py:1366-1368` |
| `ChecklistItemCompletion` | `photo_file_name`, `photo_url`, `photo_supabase_path` | `models.py:1498-1500` |

The convention is a consistent **three-field group** (original filename / public URL / bucket path).
No model uses Django's `FileField` or `ImageField` anywhere in the codebase.

### Validation

All in `_validate_and_upload` — `views.py:5696-5718`, using constants at `views.py:54-68`:

- `ALLOWED_DOCUMENT_EXTENSIONS = ['pdf', 'doc', 'docx', 'xls', 'xlsx']`
- `ALLOWED_PHOTO_EXTENSIONS = ['jpg', 'jpeg', 'png']`
- `ALLOWED_EXTENSIONS` = the union (the default allow-list)
- `MAX_FILE_SIZE_BYTES = 20 * 1024 * 1024` (20 MB)
- `MIME_TYPE_MAP` — 8 entries; the uploaded `content_type` must equal the mapped MIME **or** be
  `application/octet-stream` (`views.py:5707-5710`).

Only the checklist-photo call site narrows the allow-list, passing
`allowed_extensions=ALLOWED_PHOTO_EXTENSIONS` (`views.py:6121-6122`).

### Deletion

Soft delete on the model (`is_deleted` / `deleted_at` / `deleted_by`), with hard deletion from Supabase
by the management command `projects/management/commands/purge_deleted_files.py` (lines 30-52), which
covers `ProjectDocument` and `TaskAttachment` only.

---

## 5. Versioning

### What exists

Exactly one versioning mechanism, and it applies only to BOQ:

- **Version integer:** `BOQ.version = models.IntegerField(default=1)` — `models.py:679`.
  Incremented on resubmission after a revision request: `views.py:4228-4241` (inline path) and
  `views.py:4371-4391` (standalone `boq_submit`).
- **Separate snapshot table:** `BOQRevision` — `models.py:737-751`. One row per workflow transition
  (submit, acknowledge, revision-requested), storing `version`, `reason`, and a `JSONField` `snapshot`
  of the whole item list. Written at `views.py:4232-4235`, `4269-4274`, `4381-4384`, `4460-4464`.
  Snapshot builder: `_boq_snapshot(boq)` (called at `views.py:4231`, `4268`, `4459`); an alternative
  inline `.values(...)` snapshot exists at `views.py:4375-4379`.
- Display/history: `boq_history` — `views.py:4482-4520+`, template `projects/boq_history.html`.
  Event type is derived by **string-matching `BOQRevision.reason`** (`views.py:4499-4515`), not a code.

### What does not exist

- **No file versioning anywhere.** `ProjectDocument`, `TaskAttachment`, `DesignSubmission`,
  `PaymentRequest`, and `ChecklistItemCompletion` all store exactly one file and have no version field,
  no supersede pointer, and no revision table.
- **`django-reversion` is not installed.** `requirements.txt` (13 packages) has no versioning library;
  `INSTALLED_APPS` has no third-party app at all.
- No generic/abstract versioning base class or mixin.

### Related-but-not-versioning audit tables

- `DueDateChangeLog` — `models.py:450-463` (old/new date per Task change).
- `ProjectFieldEditLog` — `models.py:466-502` (one row per changed field for three post-activation
  Project fields, values stored as text).

These are change logs, not versioned records — they do not reconstruct a prior state of the object.

---

## 6. Roles and permissions

### `UserProfile` role field and choices

`UserProfile` — `models.py:505-538`. Field: `role = models.CharField(max_length=20, choices=ROLE_CHOICES, blank=True)` (`models.py:522`).

`ROLE_CHOICES` (`models.py:508-519`) — 10 values, stored value == label except where noted:

`Admin`, `System Admin`, `PM`, `Project Coordinator`, `Site Engineer`, `Design`, `Finance`, `SCM`, `CEO`, `BD`.

Notes:
- **There is no `Design Head` role value.** (`permissions.py:119` speculatively accepts `'Design Head'`
  as a future value, but nothing can currently store it.)
- `BD` is stored as `'BD'` on `UserProfile` but as `'BD / Sales'` on `Task.assigned_role`
  (`models.py:324`). Translation is done ad hoc via a local dict `_PROFILE_TO_TASK_ROLE = {'BD': 'BD / Sales'}`,
  redefined inside two functions — `views.py:3716` and near `views.py:1898`, `3717`.
- `max_length=20` — `'Project Coordinator'` is exactly 19 characters. `'Design Head'` would fit; a
  longer future role would not.

Other `UserProfile` fields: `user` (O2O `auth.User`, CASCADE, `related_name='profile'`), `phone_number`
(CharField 10), `is_active` (Boolean default True), `is_design_head`, `email_notifications` (default
True), `whatsapp_notifications` (default True), `created_at`, `created_by`.

### `is_design_head` — current state

- **Field type:** `models.BooleanField(default=False)` — `models.py:525`. Added by migration
  `0038_add_is_design_head_to_userprofile.py`. Not a role; explicitly role-independent.
- **How it is read — every site:**
  1. `views.py:3621` — `task_assign_design_head`: `if not request.user.profile.is_design_head: raise Http404`.
     This is the **only permission gate** the flag controls. `views.py:3612-3630`. It additionally
     requires `task.assigned_role == 'Design'` (`views.py:3627`).
  2. `permissions.py:119` — `user_can_view_project`:
     `if getattr(profile, 'is_design_head', False) or role == 'Design Head': return True` — grants
     **portfolio-wide visibility**.
  3. `views.py:7835`, `views.py:7865` — Admin user-edit form read/write (`AdminUserEditForm`, `forms.py:152-157`).
  4. `views.py:8775` — System Admin departments screen: `request.POST.get('is_design_head') == 'on'`.
  5. `admin.py:153-154` — Django admin `list_display` / `list_filter`.
  6. Templates: `projects/partials/_task_row.html:41`
     (`task.assigned_role == 'Design' and request.user.profile.is_design_head`),
     `projects/admin/user_edit.html:92-95`, `projects/subadmin/departments.html:128, 291`.
  7. Tests: `projects/tests_permissions.py:29-40, 237-266`.
- **URL:** `projects/urls.py:62` — `projects/<str:project_id>/tasks/<int:task_id>/assign-design/`.
- The flag confers **no** approval, verdict, or BOQ authority today. It grants exactly two things:
  reassigning Design-role tasks, and portfolio-wide read visibility.

### Delegation / deputy / "acting for"

**DOES NOT EXIST.** A repo-wide case-insensitive search for `delegat|deputy|acting_for|stand_in|on_behalf`
returns only two unrelated JavaScript comments (`vendors/vendor_form.html:224`,
`projects/project_detail.html:715`).

The nearest structural analogue is `Project.coordinators` (`models.py:81-86`) — an M2M to `UserProfile`
limited to role `Project Coordinator`, which **additively** shares the PM's authority. It is a
permanent grant, not a time-boxed or scoped delegation, and it has no expiry, no delegator field, and
no audit of when authority was handed over beyond ActivityLog free text.

### `user_can_manage_project()` — exact signature and full body

`projects/permissions.py:32-59`:

```python
def user_can_manage_project(user, project):
    """
    Return True if `user` has PM-level management authority on `project`.

    Authority is the UNCONDITIONAL OR of two additive sources:
        assigned PM  OR  a Project Coordinator on this project.

    This is the one canonical comparison path — every PM-ownership check routes
    through here, so adding coordinator support here gives every call site correct
    behaviour with no further edits.

    INVARIANT (additive-only): the assigned-PM check is evaluated FIRST and never
    gated on whether coordinators exist. Assigning a coordinator can only ever add
    a manager — it can never remove the PM's authority. Do not restructure this as
    "if coordinators: check coordinators else check PM" — that would silently lock
    the PM out. The OR is unconditional and lives here, not at any call site.

    `Project.assigned_pm` and `coordinators` are both to `UserProfile`, so we
    compare against `user.profile`. `getattr` guards a user with no profile
    (e.g. a superuser created via `createsuperuser`). A null `assigned_pm`
    compares False, matching the old `is not None` guards.
    """
    profile = getattr(user, 'profile', None)
    if profile is None:
        return False
    if project.assigned_pm == profile:          # PM authority — always checked, never gated
        return True
    return project.coordinators.filter(pk=profile.pk).exists()  # additive coordinator authority
```

**What it checks:** exactly two things — `project.assigned_pm == user.profile`, OR membership in
`project.coordinators`. It reads **no** role string, **no** `is_design_head`, **no** `is_superuser`,
**no** project status, `is_deleted`, `project_type`, or `program`.

Companions in the same module: `user_can_view_project()` (`permissions.py:62-151`) — the one function
allowed to branch on role strings for a project-scoped decision;
`PORTFOLIO_VIEW_ROLES = frozenset({'CEO', 'Finance', 'SCM', 'Admin'})` (`permissions.py:29`);
`user_can_manage_program()` (`permissions.py:154-174`); `project_managers()` (`permissions.py:177-196`).

Thin adapters that delegate to it (no ownership logic of their own):
- `_pm_owns_project(request, project)` — `views.py:1872-1878`.
- `_is_project_pm(profile, project)` — `views.py:6166-6175` (adds a role gate on top).

### Direct comparisons that bypass `user_can_manage_project()`

**`assigned_pm` / `coordinators` compared directly — NONE outside `permissions.py`.**
Repo-wide search for `assigned_pm ==`, `assigned_pm !=`, `assigned_pm_id ==`, `coordinators.filter`,
`coordinators.all` returns only:
- `permissions.py:57` — the canonical comparison itself.
- `permissions.py:59` — the canonical coordinator check.
- `permissions.py:192` — inside `project_managers()`.

One near-miss to note: `permissions.py:146` uses `project.assigned_design_id == profile.pk`
(a different FK, inside the canonical visibility function).

**Role strings compared directly in Python — 39 occurrences across 2 files.**
Full list from `projects/views.py` (all are `profile.role` / local `role` comparisons):

| Line | Comparison | Bypasses ownership check? |
|---|---|---|
| `2510` | `profile.role == 'PM'` (assign creator as PM) | n/a — creation-time role check |
| `3869`, `3923`, `3967`, `4011` | `profile.role not in ('SCM', 'Admin')` — Delivery Challan endpoints | **Yes** — no project-scope check |
| `4173`, `4175`, `4185`, `4205`, `4249`, `4285`, `4299` | BOQ detail role gates | **Yes** |
| `4358` | `profile.role != 'Design'` — `boq_submit` | **Yes** |
| `4411` | `profile.role != 'SCM'` — `boq_acknowledge` | **Yes** |
| `4440` | `profile.role not in ('PM', 'Project Coordinator')` — `boq_request_revision` | **Yes** |
| `4492` | `boq_history` role gate | **Yes** |
| `4696` | `profile.role != 'SCM'` | **Yes** |
| `4792` | `profile.role != 'Finance'` | **Yes** |
| `5128`, `5131`, `5138`, `5148`, `5405`, `5413`, `5418`, `5433` | `project_overview` role branches | mixed — `5131` pairs with `user_can_manage_project` |
| `5735`, `5792`, `5886`, `5926`, `6019`, `6190`, `6284`, `6382`, `6484`, `6523`, `6563`, `6699`, `6741`, `6802`, `6888`, `7175` | `profile.role == 'PM' and not user_can_manage_project(...)` | **No** — routes through the canonical helper, but the guard applies the scope check **only when the role is literally `'PM''`**; a Coordinator, Design, SE, Finance, SCM or BD user skips it entirely |
| `5891`, `6025`, `6851` | `... != profile and profile.role != 'Admin'` — own-object-or-Admin guards | **Yes** |
| `6175` | `profile.role in ('PM', 'Project Coordinator') and user_can_manage_project(...)` | No |
| `7171` | `profile.role not in ('SCM', 'PM', 'Project Coordinator', 'Site Engineer', 'Admin')` | **Yes** |
| `7442`, `7449`, `7456`, `7464` | `my_documents` section branches (`role == 'Design'` / `'SCM'`) | n/a — self-scoped querysets |
| `7486` | `profile != submission.submitted_by and profile.role not in ('PM', 'Admin')` — `design_submission_detail` | **Yes** |
| `7498` | `profile.role not in ('SCM', 'Finance', 'PM', 'Admin')` — `payment_request_detail` | **Yes** |
| `8724`, `8758`, `8801`, `8824` | `target_user.profile.role in _SA_EXCLUDED_ROLES` — System Admin user management | n/a — not project-scoped |

Plus `projects/management/commands/send_eod_digest.py` — 1 occurrence (role-based digest content branch).

**Also role-gated, not listed above:**
- `@role_required([...])` decorator — **56 uses** in `views.py`. Definition: `decorators.py:92-115`.
  It reads `request.user.profile.role` and falls back to treating a profile-less user as `'Admin'`
  (`decorators.py:107-109`).
- `_get_user_role(request)` — `views.py:1864-1869`, used by `_can_access_program` (`views.py:2312`),
  `program_list` (`views.py:2334`), `program_detail` (`views.py:2372`).
- `ROLE_DASHBOARD` map — `decorators.py:10-24`; `LANDING_ROLES = ('CEO', 'Finance', 'SCM')` — `decorators.py:32`.
- Task-role gates that compare `task.assigned_role` against a translated profile role:
  `views.py:1898-1899`, `3112`, `3122`, `3627`, `3717-3719`.
- **Templates: 30 `profile.role ==` / `!=` comparisons across 9 templates** —
  `base.html` (1), `issue_detail.html` (2), `admin/user_management.html` (9), `project_detail.html` (1),
  `project_overview.html` (1), `project_list.html` (5), `partials/_task_attachments.html` (1),
  `admin/reset_password.html` (8), `partials/_task_comments.html` (2).

### Contradictions with the prompt's assumptions

- The prompt's framing implies `is_design_head` is a settled Design-Head concept. Today it is a
  **single boolean granting two narrow capabilities** (Design-task reassignment + portfolio read).
  It carries no approval authority and there is no `Design Head` role value.
- The module description implies a Design Head *approval gate*. No approval gate of any kind exists
  for design work (see §7).

---

## 7. Tasks

### Is design work represented as a `Task` row?

**Yes.** Design work exists as ordinary `Task` rows inside the hardcoded Residential template,
`build_residential_phases()` — `projects/utils.py:419-535`.

Tasks with `assigned_role = Task.DESIGN`:

| Phase | order | Task name | Source line |
|---|---|---|---|
| 2 — Detail Engineering Visit | 4 | `DEV Inputs Validation` | `utils.py:444` |
| 3 — Design | 1 | `Design` | `utils.py:451` |
| 3 — Design | 2 | `Array Layout` | `utils.py:452` |
| 3 — Design | 3 | `SLD` | `utils.py:453` |
| 3 — Design | 4 | `Installation Drawings` | `utils.py:454` |
| 3 — Design | 5 | `BOQ Preparation` | `utils.py:455` |

Adjacent approval-named tasks that are **PM-owned, not Design-owned**:
- `Design Approval by Internal Team` — Phase 3 / order 6, `assigned_role=PM`, `Internal` (`utils.py:456`).
- `Design Approval by Customer` — Phase 3 / order 7, `assigned_role=PM`, `External` (`utils.py:457`).

These are plain tasks: completing them means setting `Task.status = 'Done'`. They carry **no verdict,
no approver field, no rejection reason, and no artifact link.**

Template attachment: `attach_residential_template(project)` — `utils.py:552+`. 9 phases, 53 tasks
(docstring at `utils.py:554` still says 52; `build_residential_phases` docstring at `utils.py:421` says
"9 phases / 52 tasks" — both are stale relative to the three added invoice tasks). Task names are also
exposed to the checklist admin via `get_residential_template_task_names()` — `utils.py:538-549`.

**There is no task template for OPEX or CAPEX.** `TaskDurationTemplate.PROJECT_TYPE_CHOICES` contains
only `('residential', 'Residential')` — `models.py:1394-1396`. An OPEX site created under a Program
receives **no phases and no tasks at all.**

### Full `Task` model field list

`projects/models.py:316-389`:

| Field | Type | Notes |
|---|---|---|
| `phase` | FK `ProjectPhase`, CASCADE, `related_name='tasks'` | Project reached via `phase.project` — **no direct FK to `Project`** |
| `task_name` | `CharField(200)` | |
| `task_order` | `PositiveIntegerField` | ascending within phase; drives the due-date chain |
| `assigned_role` | `CharField(20, choices=ROLE_CHOICES)`, default `PM` | Choices: `PM`, `Site Engineer`, `Finance`, `SCM`, `BD / Sales`, `Design` (`models.py:327-334`) |
| `assigned_to` | FK `UserProfile`, `SET_NULL`, null, `related_name='assigned_tasks'` | |
| `status` | `CharField(20, choices)`, default `Not Started` | `Not Started`, `In Progress`, `Done`, `Blocked` (`models.py:341-346`) |
| `task_type` | `CharField(10, choices)`, default `Internal` | `Internal` / `External` (`models.py:352-355`) |
| `duration_days` | `PositiveIntegerField`, default 1 | **calendar** days |
| `due_date` | `DateField(blank, null)` | |
| `completed_at` | `DateTimeField(blank, null)` | set on transition to `Done` |
| `blocked_since` | `DateTimeField(blank, null)` | set on transition to `Blocked`, cleared on unblock |
| `is_payment_milestone` | `BooleanField(default=False)` | triggers Finance notification when Done |
| `created_at` | `DateTimeField(auto_now_add=True)` | |

`Meta.ordering = ['task_order']`. Property `active_attachment_count` — `models.py:383-389`.

**There is no `start_date` field on `Task`.**

### How task due dates are set today, and who can change them

Three mechanisms:

1. **Bulk calculation on activation / recalculation** — `calculate_due_dates(project, user=None)`,
   `utils.py:128-167`. Anchors on `project.activated_at.date()`; Internal tasks chain sequentially
   (`prev + duration_days`), External tasks shadow the current internal position. Writes one summary
   ActivityLog with `action_code='due_dates_recalculated'`. Triggered by `project_recalculate_dates`
   — `views.py:2171`.
2. **Cascade recalculation from an anchor task** — `recalculate_from_task(project, anchor_task, new_date, user=None)`,
   `utils.py:54-125`. Only runs when `project.cascade_scheduling` is True. `bulk_update`s every
   downstream task and `bulk_create`s `DueDateChangeLog` rows.
3. **Manual per-task edit** — `task_set_due_date(request, project_id, task_id)`, `views.py:3698-3776`.

Who can change a due date (`views.py:3711-3767`):

- **PM / Coordinator** (`_pm_owns_project` ⇒ `user_can_manage_project`): may edit **any** task on the
  project. With `cascade_scheduling` ON the change ripples downstream; with it OFF only that task changes.
- **Any other role:** may edit **only** tasks whose `assigned_role` matches their (translated) profile
  role — `views.py:3717-3720`, else `PermissionDenied`. And only when `project.cascade_scheduling` is
  **OFF** — `views.py:3722-3726`.
- Clearing a date (empty POST value) sets `due_date = None`.
- Admin defaults per task name are editable via `TaskDurationTemplate` (`models.py:1387-1416`,
  admin view `views.py:8207`, `8969`); changes apply to **new projects only**.

Both paths write an ActivityLog line (`views.py:3734`, `3741`, `3760`, `3767`) — with **no `action_code`**.

### Does any task carry an approval or verdict field?

**No.** `Task` has no approval, verdict, approver, reviewer, rejection-reason, or QC field. The status
vocabulary is `Not Started / In Progress / Done / Blocked` only.

Approval/verdict semantics exist elsewhere, on non-Task models:
- `BOQ.status` — `Draft / Submitted / Acknowledged / Revision Requested` (`models.py:662-667`).
- `Issue.status` — `Open / In Progress / Resolved / Closed`, with `resolution_note`, `resolved_at`,
  `closed_at` (`models.py:854-885`).
- `DesignSubmission` — `status` (`Pending / Approved / Rejected`), `reviewed_by`, `reviewed_at`,
  `review_notes` (`models.py:1345-1384`). **See §5 / UNCERTAIN — this model has no write path.**
- `PaymentRequest.status` — `pending / confirmed` (`models.py:1288-1293`).
- `ChecklistItemCompletion.is_checked` + mandatory photo (`models.py:1483-1515`).

---

## 8. Notifications

### Chokepoint

Every outbound notification flows through one function — `projects/notifications.py:24-97`:

```python
def send_notification(
    recipient,
    message,
    channels=None,
    link='',
    subject='',
    template=None,
    template_params=None,
    related_project=None,
    actor=None,
    html_message=None,
):
```

`recipient` must be a `UserProfile`, never a raw `User`. `channels` defaults to `['in_app']`.
Per-channel dispatch at `notifications.py:77-97`; a failed send never propagates.

### WhatsApp

Internal helper (not called directly by views): `_send_whatsapp(recipient, template, template_params, base)`
— `notifications.py:114-177`.

Callers use `send_notification(..., channels=[..., 'whatsapp'], template='<name>', template_params=[...])`.
Gates, in order (`notifications.py:81-88`): `SystemSettings.whatsapp_enabled` → `recipient.whatsapp_notifications`.
Phone is stored as 10 digits; `+91` is added here, with guards against double-prefixing
(`notifications.py:124-131`). Endpoint: `https://api.interakt.ai/v1/public/message/`, auth
`Basic {settings.INTERAKT_API_KEY}`, 10s timeout.

Param convention (`notifications.py:138-153`): `template_params` is **one flat list** — element 0 is the
single header variable (`headerValues`), the remainder are `bodyValues` in Interakt's registered order.

### Email

Internal helpers: `_send_email(recipient, subject, message, base, html_message=None)` — `notifications.py:221-229`;
low-level `_zeptomail_post(to_email, to_name, subject, text_body, html_body=None) -> (ok, detail)` —
`notifications.py:180-218`. Endpoint `https://api.zeptomail.in/v1.1/email`.

Two non-UserProfile email paths:
- `send_raw_email(to_email, subject, body)` — `notifications.py:251-277`. **Does not check the master switch.**
- `send_aggregate_email(to_email, subject, text_body, html_body=None, log_recipient=None, template_name='')`
  — `notifications.py:280-323`. Gated by the master switch only (no per-recipient preference).

A commented-out SendGrid fallback sits at `notifications.py:231-248`.

### How templates are registered or mapped

**There is no registry, no mapping table, and no constants module.** A WhatsApp template is a bare
string literal passed at each call site, with its positional params assembled inline. Structure:

```python
send_notification(
    recipient=<UserProfile>,
    message=<str>,
    channels=['in_app', 'whatsapp', 'email'],
    subject=<str>,
    template='assign_task',                                  # <- literal, unregistered
    template_params=[project.customer_name, recipient_name,  # <- positional, per-template order
                     task.task_name, project.customer_name, task_url_abs],
    related_project=project,
    actor=profile,
)
```

All template names in use, with call sites:

| Template name | Call sites |
|---|---|
| `payment_notification` | `views.py:3296`, `3490` |
| `assign_task` | `views.py:3587`, `3668` |
| `boq_acknowledged` | `views.py:4101` |
| `invoice_paid` | `views.py:4854` |
| `assign_project` | `views.py:5677`, `8141` |
| `issue_created` | `views.py:6251`, `6347`, `6453` |
| `issue_resolved` | `views.py:6609` |
| `eod_digest` (log label only, email-only) | `management/commands/send_eod_digest.py:311` |
| `eod_digest_aggregate` (log label only) | `send_eod_digest.py:36` |

A standalone script `test_whatsapp_templates.py` sits in the repo root (7 KB, not part of the app).

### `NotificationLog` model fields

`projects/models.py:1202-1249`:

| Field | Type |
|---|---|
| `recipient` | FK `UserProfile`, CASCADE, `related_name='notification_logs'` — **required** |
| `channel` | `CharField(10, choices)` — `in_app` / `whatsapp` / `email` |
| `status` | `CharField(10, choices)` — `sent` / `failed` / `skipped` |
| `message` | `TextField` |
| `template_name` | `CharField(100, blank)` |
| `related_project` | FK `Project`, SET_NULL, null |
| `actor` | FK `UserProfile`, SET_NULL, null, `related_name='notifications_triggered'` |
| `error_detail` | `TextField(blank)` |
| `delivery_status` | `CharField(30, blank, default='')` — `message_api_sent` / `_delivered` / `_read` / `_failed` (Interakt webhook) |
| `interakt_message_id` | `CharField(100, blank, default='')` |
| `created_at` | `DateTimeField(auto_now_add=True)` |

`Meta.ordering = ['-created_at']`. Written only by `_log(base, channel, status, error_detail='', interakt_message_id='')`
— `notifications.py:326-337`.

The in-app model is separate: `Notification` — `models.py:754-771` (`recipient`, `message`, `link`,
`is_read`, `created_at`).

### Where `SystemSettings.email_enabled` is read

Model field: `models.py:1256` (`BooleanField(default=False)`). Accessor: `SystemSettings.get()` —
`models.py:1279-1282` (`get_or_create(pk=1)`).

Read sites:
1. `notifications.py:63` — inside `send_notification`, via `sys = SystemSettings.get()`; consumed at
   `notifications.py:91-93` (skip + log `'Master switch off'`).
2. `notifications.py:314` — inside `send_aggregate_email`; consumed at `notifications.py:318-320`.
3. `views.py:7513+` — `admin_master_switches` (Admin toggle screen), template
   `projects/templates/projects/admin/master_switches.html`.

`send_raw_email` deliberately does **not** read it (`notifications.py:255-256`).

---

## 9. Audit logging

### Model

`ActivityLog` — `projects/models.py:894-916`. Described as append-only; nothing enforces that.

| Field | Type | Notes |
|---|---|---|
| `project` | FK `Project`, CASCADE, null, blank, `related_name='activity_logs'` | null for non-project events |
| `actor` | FK `UserProfile`, SET_NULL, null, blank, `related_name='activity_logs'` | |
| `action` | `CharField(255)` | human-readable free text |
| `action_code` | `CharField(50, blank, db_index=True, default='')` | stable machine key; **blank on most call sites** |
| `entity_type` | `CharField(50, blank, default='')` | e.g. `Task`, `Issue`, `BOQ`, `File` |
| `entity_id` | `PositiveIntegerField(null, blank)` | |
| `timestamp` | `DateTimeField(auto_now_add=True)` | |

`Meta.ordering = ['-timestamp']`. Added by migration `0016_issue_activitylog.py`; `action_code` added by
`0043_activitylog_action_code.py`.

### Helper

`projects/models.py:1177-1199`:

```python
def log_activity(project, actor, action, entity_type='', entity_id=None, action_code=''):
```

Wraps `ActivityLog.objects.create(...)` in a bare `try/except Exception` and logs the failure to
`logging` — a failed audit write **never** aborts the calling operation (`models.py:1197-1199`).

### What currently writes audit entries

By `entity_type` (95 call sites in `views.py` + `utils.py` + `signals.py`):

| `entity_type` | Representative lines |
|---|---|
| `Project` | `views.py:2078`, `2159`, `2233`, `2517`, `3825`, `5216`, `5231`, `5613`, `8118`, `8672`; `utils.py:165` |
| `Program` | `views.py:2395`, `2426`, `2461`, `2814` |
| `Task` | `views.py:3225`, `3425`, `3524`, `3734`, `3741`, `3760`, `3767` |
| `Issue` | `views.py:3219`, `3420`, `6231`, `6327`, `6432`, `6543`, `6586`, `6646`, `6680`, `6716`, `6721` |
| `Milestone` | `views.py:3264`, `3460`, `4582`, `4626`, `5102`, `5161` |
| `BOQ` | `views.py:4394`, `4424`, `4468` |
| `PaymentRequest` | `views.py:4778`, `4825` |
| `File` | `views.py:5845`, `5850`, `5900`, `5990`, `5994`, `6034` |
| `ChecklistItemCompletion` | `views.py:6149` |
| `Comment` | `views.py:6781`, `6783`, `6834`, `6836`, `6863`, `6870` |
| `DeliveryChallan` | `views.py:7153`, `7277`, `7350` |
| `System` | `views.py:7535`, `7547`, `7570` |
| `User` | `views.py:7611`, `7623`, `7642`, `7745`, `7757`, `7776`, `7842`, `7850`, `8584`, `8743`, `8787`, `8810`, `8833`, `8919`; `signals.py:37`, `52` |
| `Notification` | `views.py:7694` |
| `TaskDurationTemplate` | `views.py:8207`, `8969` |
| `Checklist` | `views.py:8260`, `8301`, `8344`, `8363`, `8386`, `8410`, `8429`, `8464`, `8510`, `8530` |

Populated `action_code` values (the rest are blank — `models.py:904-907` says most call sites are not
retrofitted):

`due_dates_recalculated` (`utils.py:166`), `program_created` (`2395`), `program_edited` (`2426`),
`program_deleted` (`2461`), `opex_site_created` (`2517`), `opex_sites_bulk_created` (`2815`),
`issue_created` (`3219`, `3420`, `6231`, `6327`, `6432`), `issue_resolved` (`6586`),
`issue_closed` (`6646`), `issue_reopened` (`6680`), `design_bulk_assigned` (`5217`, `5232`),
`pm_assigned` (`8120`), `task_status_<lowercased status>` — dynamic, `f"task_status_{new_status.lower().replace(' ', '_')}"`
(`3226`, `3426`), and `task_assigned` / `task_reassigned` / `task_unassigned` produced by
`_log_task_assignment(project, actor, task, prev_assignee, new_assignee)` — `views.py:3503-3524`.

### Other audit tables

- `DueDateChangeLog` — `models.py:450-463`.
- `ProjectFieldEditLog` — `models.py:466-502`.
- `NotificationLog` — `models.py:1202-1249` (see §8).

### Read surfaces

- `project_timeline` — `views.py:6879-6905` (per-project, `Paginator(logs, 20)`).
- `portal_activity_log` — `views.py:6908+`, `@role_required(['Admin'])`, filters on
  `project_id` / `actor_id` / `entity_type` / `date_from` / `date_to`. **No `action_code` filter exists.**

### What does not exist

- No audit entry is written for BOQ **item-level** quantity edits (`save_design` / `save_scm` write no
  ActivityLog at all — `views.py:4205-4266`). Only submit / acknowledge / revision-request are logged.
- No audit entry for file *views* or downloads.
- No signal-based or model-level automatic auditing; every entry is an explicit call.

---

## 10. Dashboards and frontend

### Dashboard templates (one per role) — `projects/templates/dashboard/`

| Role | Template | Extends |
|---|---|---|
| Admin | `dashboard/admin.html` | `base.html` |
| BD | `dashboard/bd.html` | `base.html` |
| CEO | `dashboard/ceo.html` | `base.html` |
| Design | `dashboard/design.html` | `base.html` |
| Finance | `dashboard/finance.html` | `base.html` |
| PM (and Project Coordinator — shared) | `dashboard/pm.html` | `base.html` |
| SCM | `dashboard/scm.html` | `base.html` |
| Site Engineer | `dashboard/site-engineer.html` | `base.html` |

Partials (no `extends`): `dashboard/_milestone_badge.html`, `dashboard/_milestone_badge_ro.html`.
There is **no System Admin dashboard template** — that role lands on `/sub-admin/projects/`
(`decorators.py:15-16`), which renders `projects/subadmin/projects.html`.
Role→URL map: `ROLE_DASHBOARD` — `decorators.py:10-24`.

### Base templates

| Base | Framework | Notes |
|---|---|---|
| `templates/base.html` | **Bootstrap 5.3.3** (`base.html:8`, `140`) + Bootstrap Icons 1.11.3 (`base.html:9`) + **HTMX 2.0.4** (`base.html:141`) | `<body hx-headers='{"X-CSRFToken": ...}'>` at `base.html:41` |
| `templates/projects/admin/admin_base.html` | **Tailwind CDN** (`:7`) + Alpine 3.x (`:8`) + Lucide (`:9`) | Portal-admin shell; no Bootstrap, **no HTMX** |
| `templates/projects/subadmin/subadmin_base.html` | **Tailwind CDN** (`:7`) + Alpine 3.x (`:8`) + Lucide (`:9`) | System-Admin shell; **no HTMX** |

### Tailwind vs Bootstrap — page by page

**Tailwind (4 standalone/base pages + their children):**
- `templates/landing.html` (`:12`) — standalone, no `extends`.
- `templates/registration/login.html` (`:8`) — standalone.
- `projects/admin/admin_base.html` and its 10 children: `audit_log.html`, `checklist_edit.html`,
  `checklists.html`, `departments.html`, `master_switches.html`, `notification_prefs.html`,
  `projects_list.html`, `reset_password.html`, `send_records.html`, `task_durations.html`, `user_edit.html`,
  `user_management.html`.
- `projects/subadmin/subadmin_base.html` and its 3 children: `departments.html`, `projects.html`,
  `task_durations.html`.

**Bootstrap — everything else.** All 8 dashboards and all 40+ `projects/*.html` pages extend
`base.html`. Alpine.js is additionally loaded per-page inside `{% block extra_head %}` on
`dashboard/design.html:6`, `ceo.html:6`, `site-engineer.html:6`, `finance.html:6`, `bd.html:5`, and
`projects/admin_whatsapp_log.html:5`.

Note: some Bootstrap pages contain utility class names that look Tailwind-ish, but Tailwind's CDN is
**not** loaded on `base.html`, so those have no effect there.

### Is HTMX present?

**Yes** — HTMX 2.0.4 from unpkg, loaded once in `base.html:141`. Global wiring script at
`base.html:144-200+` (afterSwap re-init, OOB flash-message handling at `base.html:162`, `responseError`
handler at `base.html:177`, modal auto-close at `base.html:192`). Degrades gracefully: `if (!window.htmx) return;`
(`base.html:150`).

**50 `hx-*` attribute occurrences across 16 templates:**

| Template | count |
|---|---|
| `projects/project_overview.html` | 13 |
| `projects/partials/_task_row.html` | 9 |
| `projects/task_detail.html` | 4 |
| `projects/task_add_modal.html` | 3 |
| `projects/task_assign_design_head_modal.html` | 3 |
| `projects/partials/_project_field_edit_form.html` | 3 |
| `projects/partials/_checklist.html` | 2 |
| `projects/partials/_task_comments.html` | 2 |
| `projects/partials/_task_attachments.html` | 2 |
| `projects/partials/_task_detail_status.html` | 2 |
| `projects/partials/_task_add_success.html` | 2 |
| `projects/partials/_hx_messages.html` | 1 |
| `projects/partials/_checklist_response.html` | 1 |
| `projects/partials/_project_editable_fields.html` | 1 |
| `projects/partials/_task_attachments_response.html` | 1 |
| `projects/partials/_task_row_response.html` | 1 |

Server-side helpers: `_is_hx(request)`, `_render_task_row_hx(...)` (with `oob_tasks` support —
`views.py:3769-3774`), `_render_task_status_hx(...)`, `_checklist_context(...)` (`views.py:5770`).
Out-of-band flash fragment: `projects/partials/_hx_messages.html`.

**HTMX is confined to the Bootstrap (`base.html`) surface. Neither Tailwind shell loads it.**

### Paginated / filterable list view — examples

**Best example (both paginated and filterable):** `portal_activity_log` — `views.py:6908-6940+`,
`@login_required @role_required(['Admin'])`, template `projects/portal_activity_log.html`.
Pattern: build the base queryset with `select_related`, then apply each `request.GET` filter
conditionally (empty string = no filter), then `Paginator(...)` + `paginator.get_page(request.GET.get('page'))`.

**Simplest paginated example:** `project_timeline` — `views.py:6879-6905`:
```python
from django.core.paginator import Paginator
logs = project.activity_logs.select_related('actor__user').order_by('-timestamp')
paginator   = Paginator(logs, 20)
page_number = request.GET.get('page')
page_obj    = paginator.get_page(page_number)
```

**Role-scoped list with annotated rollup (no pagination):** `program_list` — `views.py:2320-2354`.
Note it materialises the queryset into a Python list for PMs (`views.py:2336`), so it cannot be paginated
at the DB layer as written.

**No pagination anywhere else.** Every dashboard and `my_documents` (`views.py:7423-7478`) uses hard
Python slices such as `[:50]` (`views.py:7433`, `7438`, `7445`, `7452`, `7459`, `7467`). `Paginator`
appears exactly twice in the codebase, both in `views.py` (`6882`, `6912`).

---

## 11. Migrations

### Highest migration number per app

There is **one** app with migrations: `projects`.

- **Highest:** `0046_alter_project_customer_name.py`.
- Total: 46 numbered migrations, `0001_initial` through `0046`, no gaps.
- Recent, relevant ones: `0012_assigned_design`, `0031_design_submission`, `0038_add_is_design_head_to_userprofile`,
  `0039_project_coordinators_alter_userprofile_role`, `0040_checklistitem`, `0041_reusable_checklists`,
  `0042_projectfieldeditlog`, `0043_activitylog_action_code`, `0044_gantt_settings`,
  `0045_project_site_code_alter_project_project_id_program_and_more`, `0046_alter_project_customer_name`.

`django.contrib.*` apps carry their own upstream migrations; none are authored in this repo.

### Unapplied migrations

- `python manage.py makemigrations --check --dry-run` → **`No changes detected`**. Models and migration
  files are in sync; no migration is missing.
- `python manage.py showmigrations projects` → **all 46 marked `[X]`** against the local database. No
  unapplied migration locally.

### Migration files not yet committed to git

`git status` shows three migration files as **untracked** (`??`), i.e. present in the working tree but
not in git history:
- `projects/migrations/0044_gantt_settings.py`
- `projects/migrations/0045_project_site_code_alter_project_project_id_program_and_more.py`
- `projects/migrations/0046_alter_project_customer_name.py`

This is a version-control state, not an unapplied-migration state.

---

## VERIFICATION — working-tree state

`DESIGN_MODULE_AUDIT_FINDINGS.md` (this file) was created in the repo root.

**The working tree was already dirty before this session began.** Baseline `git status --short`,
captured before any tool call that could write, listed these **pre-existing** entries — none of them
were touched by this session:

Modified (`M`): `projects/__pycache__/admin.cpython-314.pyc`, `projects/__pycache__/models.cpython-314.pyc`,
`projects/admin.py`, `projects/decorators.py`, `projects/forms.py`, `projects/models.py`,
`projects/permissions.py`, `projects/templates/base.html`, `projects/templates/dashboard/design.html`,
`projects/templates/dashboard/pm.html`, `projects/templates/projects/admin/master_switches.html`,
`projects/templates/projects/project_overview.html`, `projects/urls.py`, `projects/utils.py`,
`projects/views.py`, `requirements.txt`, `solarpms/__pycache__/settings.cpython-314.pyc`.

Untracked (`??`): `CONTEXT_SWITCHER_AUDIT.md`, `FINDINGS_SECONDARY.md`, `Program_Foundation_Investigate_Audit.md`,
`ROLE_PREFIX_AUDIT.md`, `docs/`, `projects/gantt_constants.py`, `projects/migrations/0044_gantt_settings.py`,
`projects/migrations/0045_project_site_code_alter_project_project_id_program_and_more.py`,
`projects/migrations/0046_alter_project_customer_name.py`, `projects/templates/landing.html`,
`projects/templates/projects/opex_site_bulk_upload.html`, `projects/templates/projects/opex_site_form.html`,
`projects/templates/projects/partials/_gantt_grid.html`, `projects/templates/projects/program_detail.html`,
`projects/templates/projects/program_form.html`, `projects/templates/projects/program_list.html`,
`projects/tests_gantt.py`, `projects/tests_permissions.py`.

Two read-only management commands were run against the local database
(`makemigrations --check --dry-run`, `showmigrations projects`). Neither writes files or schema.
Running `manage.py` may refresh `__pycache__/*.pyc` byte-code files; those were already listed as
modified in the baseline.

**Only `DESIGN_MODULE_AUDIT_FINDINGS.md` was added by this session.**

---

## UNCERTAIN

Items that could not be determined with confidence from the code alone.

1. **`DesignSubmission` has no write path.** The model exists (`models.py:1345-1384`, migration
   `0031_design_submission.py`) and is applied to the DB, but a repo-wide search finds **no**
   `DesignSubmission.objects.create(...)`, no form, no upload view, and no URL that produces one. The
   only references are a read-only list in `my_documents` (`views.py:7450`) and a read-only detail view
   `design_submission_detail` (`views.py:7482-7489`, template `projects/design_submission_detail.html`).
   I could not determine whether rows exist in production (created via Django admin or shell), whether
   the write path was removed, or whether it was never built. **This is the single most important
   uncertainty for the design module** — it is the closest existing analogue to a versioned design
   artifact with an approval verdict, and I cannot tell if it is live, dead, or half-built.

2. **Which BOQ submit path is actually reachable.** Two implementations exist:
   the inline `submit_design` branch in `boq_detail` (`views.py:4205-4247`) and the standalone
   `boq_submit` view (`views.py:4344-4396`). They differ — the standalone one allows only
   `Draft`/`Revision Requested` and builds its snapshot with an inline `.values(...)` rather than
   `_boq_snapshot()`; the inline one also allows `Acknowledged` and writes no ActivityLog. I did not
   trace the templates far enough to determine which one the UI posts to, or whether both are live.
   The same duplication exists for `boq_acknowledge` (`views.py:4399-4426`) versus the inline
   `acknowledge_scm` branch (`views.py:4249-4283`).

3. **Whether `get_standard_boq_items()`'s 37 rows are intended for non-Residential projects.**
   The docstring says "Residential" (`models.py:614`) but `boq_detail` applies it with no
   `project_type` branch. I cannot tell from code whether OPEX/CAPEX BOQs are expected to use it, use a
   different list, or not use this surface at all.

4. **Production data shape.** All conclusions here come from source. I did not query the Railway
   production database. In particular I could not verify: how many `UserProfile` rows have
   `is_design_head=True`; whether any `DesignSubmission` rows exist; the real distribution of
   `Task.due_date` nulls; or whether any `Program`/OPEX sites exist in production yet.

5. **Interakt template registration.** The template names (`assign_task`, `boq_acknowledged`, etc.)
   are approved and registered in the external Interakt console, not in this repo. I cannot verify from
   code which templates exist there, their approval state, or their true registered parameter order —
   `notifications.py:50-52` explicitly warns that the order must be read from the console, not the
   preview. A new design-module template would need that external step; I cannot confirm what that
   process is.

6. **`git status` on the OPEX/Program feature branch state.** `projects/models.py`, `views.py`,
   `urls.py`, `utils.py`, `forms.py`, and `permissions.py` all show as modified against HEAD, and three
   migrations are untracked. I audited the **working-tree** contents (what will run), not the committed
   HEAD. If any of this uncommitted work is later reverted, parts of §1, §2, and §6 would change.

7. **Whether `max_length=20` on `UserProfile.role` is a real constraint for a future `Design Head`
   role.** `'Design Head'` is 11 characters so it fits, and `permissions.py:119` already accepts it —
   but I found no migration, no choice entry, and no other code path expecting it. I could not determine
   whether promoting the flag to a role is planned, scheduled, or abandoned.

8. **Supabase bucket access policy.** All file URLs are built as `/object/public/...`. Whether the
   bucket is genuinely public-read, or whether that URL form fails and something else serves files, is
   an external Supabase configuration I could not inspect from the repository.
