# Design Module — Deferred Findings

Items found but deliberately **not fixed**. Opened by Part 0.5 (BOQ Item Master).
Nothing in this file was changed by that session.

---

## A. Explicitly out of scope for Part 0.5 (carried forward)

| # | Finding | Location |
|---|---------|----------|
| A1 | ~~BOQ endpoints are gated by role string (`if role not in ('Design','SCM','PM','Project Coordinator','Admin')`) rather than `user_can_manage_project()`. A PM who does not manage the project still passes.~~ **CLOSED by Part 0.6** — see section C. | `projects/views.py` — `boq_detail`, `boq_submit`, `boq_acknowledge`, `boq_request_revision`, `boq_history` |
| A2 | `'Acknowledged'` is present in `_DESIGN_EDITABLE`, so Design can still edit a BOQ that SCM has already acknowledged. | `projects/views.py:4203` (`boq_detail`) |
| A3 | Supabase object URLs are public with no signed-URL helper. | `projects/supabase_storage.py` |
| A4 | `DesignSubmission` has no write path — the model exists but nothing creates rows. | `projects/models.py` |
| A5 | Role-string comparisons (`profile.role == 'X'`, `role in (...)`) are used throughout instead of a permission helper. | codebase-wide |

---

## B. Found during Part 0.5

### B1 — Two legacy BOQ rows will never aggregate

Local DB: 149 `BOQItem` rows, 147 linked, **2 left null**. Both are variants of catalogue
item 36 (`ITM-036`):

```
'Miscellaneous - (net metering,transportation,rubber mat,fire extinguishers,warning boards'
    → the catalogue string truncated: the trailing ")" is missing
'Miscellaneous net metering transportation rubber mat fire extinguisher warning boards'
    → punctuation stripped, "extinguishers" singular
```

Back-linking was specified as exact-string-match only, so these stay null by design.
They will be silently excluded from any cross-site quantity roll-up. Fixing them means
a one-off manual `item_master` assignment (2 rows), not a code change.

**Not verified on production.** These counts are from the local Postgres DB. Migration
`0047` has not been run on Railway; the linked/null split there may differ, and the
migration prints the same three numbers when it runs, so check that output.

> **UPDATE (Part 1 session).** `0047` has now run on Railway. Production reported
> **186 `BOQItem` rows, 184 linked, 2 left null**, and — unlike local — **one** distinct
> unmatched description (`'Miscellaneous net metering transportation rubber mat fire
> extinguisher warning boards'`), so both production rows share the same text while the
> two local rows differ from each other. Production PKs were never obtained (the
> environment blocked the read), so the one-off `item_master` fix is still open and
> cannot be written as a hardcoded-PK migration until those PKs are read.

### B2 — `unit` and `category` widths/choices disagree between catalogue and BOQ row

| | `BOQItemMaster` | `BOQItem` |
|---|---|---|
| unit | `unit` — `CharField(max_length=20)`, free text | `uom` — `CharField(max_length=10, choices=UOM_CHOICES)` |
| category | `category` — `CharField(max_length=64)`, free text | `category` — `CharField(max_length=20, choices=CATEGORY_CHOICES)` |

An Admin creating a catalogue entry with a unit longer than 10 characters, or a category
longer than 20, will make the *next* BOQ creation fail with a Postgres
`value too long for type character varying` error — the failure surfaces at BOQ creation,
not at catalogue save. An off-list-but-short value (e.g. unit `Roll`) saves fine and
produces a `BOQItem` whose `uom` is outside `UOM_CHOICES`; choices are not enforced at the
DB level and `bulk_create` skips validation, so it goes in silently and renders as-is.

Neither field was widened or constrained — changing `BOQItem` was out of scope. The
practical fix is either to validate `unit`/`category` against the `BOQItem` choice lists in
`BOQItemMasterForm`, or to widen `BOQItem.uom`/`category` and drop the choice lists.

### B3 — `serial_no` derives from `sort_order`, which is not unique

`get_standard_boq_items()` now returns `serial_no = BOQItemMaster.sort_order`. This is
deliberate: it keeps serial numbers stable when a catalogue entry is deactivated (position
in the list would not). But `sort_order` has `default=0` and no uniqueness constraint, so:

- a catalogue row saved with the raw default produces `BOQItem.serial_no = 0`
- two rows sharing a `sort_order` produce two BOQ rows with the same serial number

Mitigated only by the create form pre-filling `sort_order` with `max + 1`. Not enforced.

### B4 — Ad-hoc BOQ rows carry no catalogue link

`add_item` (`projects/views.py:4291`) creates rows with free-text `description` and
`is_standard_item=False`. `item_master` stays null, so ad-hoc rows are invisible to grouped
procurement. This is correct for Part 0.5 (there is no catalogue entry to point at), but
the design module will need a decision: either force ad-hoc rows to pick a catalogue entry,
or accept that they aggregate separately.

### B5 — Test suite could not be run locally

`manage.py test projects` fails with `permission denied to create database` — the local
Postgres role cannot create the test database, with or without `--keepdb`. The 51 existing
tests were therefore **not executed** this session. No existing test references BOQ
(`grep` over `tests.py`, `tests_gantt.py`, `tests_permissions.py` returns nothing), and the
new migration seeds the catalogue on any fresh test database, so BOQ creation inside a test
run will work — but that is reasoning, not a passing run.

---

## C. Found during Part 0.6 (BOQ permission gating) — reviewed, deliberately not fixed

Part 0.6 closed A1: BOQ read/write is now project-scoped through
`user_can_view_project_boq()` / `user_can_edit_project_boq()` in `projects/permissions.py`.
Everything below was found while doing that and was left alone on purpose.

| # | Finding | Location |
|---|---------|----------|
| C1 | BOQ auto-creates and seeds ~53 catalogue rows on **GET**, not on an explicit user action. Now gated on `user_can_edit_project_boq()`, so only an authorised designer can trigger it — but a side-effecting GET is still wrong, is not idempotent, and is created by a page load rather than an intent. Converting it to a POST action is its own session. | `projects/views.py:4184-4196` |
| C2 | `'Acknowledged'` is present in `_DESIGN_EDITABLE`, so Design can still edit quantities on a BOQ that SCM has already acknowledged. (Duplicate of A2, restated with the current line.) | `projects/views.py:4218` |
| C3 | `dashboard_scm` serialises **every** active project's BOQ items to JSON into the template for the raise-payment-request dropdown. SCM is portfolio-wide by remit so this is not a privilege breach, but it is a portfolio-wide BOQ data dump that no BOQ gate covers, and it grows linearly with the project count. | `projects/views.py:1397-1447` |
| C4 | `raise_payment_request` is SCM-role-gated with no project relationship check. The BOQ item lookup is correctly scoped (`boq__project=project`), so it cannot select another project's item — but the endpoint itself is reachable for any project. | `projects/views.py:4700-4702` |
| C5 | Project lookups do not filter `is_deleted`: 36 call sites use `get_object_or_404(Project, project_id=project_id)` against 3 that add `is_deleted=False`. Soft-deleted projects' BOQs stay reachable. Codebase-wide pattern, not BOQ-specific. | `projects/views.py`, codebase-wide |
| C6 | Supabase object URLs are public with no signed-URL helper, 4 inline call sites. (Carried from A3.) | `projects/supabase_storage.py` |
| C7 | `DesignSubmission` has a model and read views but no write path — nothing creates rows. (Carried from A4.) | `projects/models.py` |
| C8 | 39 role-string comparisons, 56 `@role_required` decorators, and 30 template role checks remain outside BOQ. (Carried from A5.) | codebase-wide |
| C9 | `is_design_head` confers no approval authority. Part 0.6 gave Design Head portfolio-wide BOQ **read** and deliberately no write, so the flag still grants oversight without any approval lever. | `projects/models.py:525`, `projects/permissions.py` |

### C10 — `boq_submit` crashes for every authorised caller (pre-existing, found by verification)

`boq_submit` builds its `BOQRevision.snapshot` with a raw `.values()` call that leaves
`boq_quantity` / `ordered_quantity` as `Decimal`:

```python
snapshot = list(boq.items.values(
    'serial_no', 'category', 'description', 'uom',
    'boq_quantity', 'ordered_quantity', ...
))
```

`BOQRevision.snapshot` is a `JSONField`, so psycopg raises
`TypeError: Object of type Decimal is not JSON serializable` on save. The endpoint therefore
**500s for any BOQ that has a quantity set** — which is every BOQ that passes its own
"at least one item must have a quantity" guard immediately above. It cannot ever have
succeeded.

The sibling helper `_boq_snapshot()` (`projects/views.py:4029`) does the `Decimal → float`
conversion correctly, and `boq_detail`'s inline `submit_design` branch uses it. The template
posts `submit_design` to `boq_detail` (`boq_detail.html:270`) and never targets
`/boq/submit/`, so the broken endpoint is unreachable from the UI and the crash has gone
unnoticed.

Not fixed here: Part 0.6 changes authorisation only, and this is a serialisation bug behind
the gate. The fix is one line — call `_boq_snapshot(boq)` — but the real question is whether
this duplicate endpoint should exist at all, since `boq_acknowledge` duplicates the SCM path
the same way. Both were gated this session and both remain dead code.

**Location:** `projects/views.py:4402-4407`

### ~~C11 — Precondition ratio was measured on the local database, not Railway~~ — **CLOSED**

> **CLOSED (Part 1 session).** The precondition was re-measured on live Railway data:
> **25 active projects, 3 with a null `assigned_design` = 12%**, below the 20% threshold.
> `user_can_edit_project_boq()` was narrowed to **W-narrow** (`assigned_design` only; the
> task-holding fallback was deleted) in commit `83739b6`. The original entry is kept below
> for the record — its "W-broad is selected" statement no longer describes the code.

The Part 0.6 precondition (share of active projects with a null `assigned_design`, threshold
20%, selecting W-broad vs W-narrow for the write rule) was run against the local Postgres
(`solarpms_local`): **14 active projects, 6 with a null `assigned_design` = 42.9% → W-broad**.
Catalogue rows: 37.

Two attempts to read the same figures from the Railway production database were blocked by
the environment's permission classifier. The selected rule is therefore based on local data.
If Railway's ratio is 20% or below, the write rule should narrow to W-narrow by deleting the
task-holding fallback on the last line of `user_can_edit_project_boq()` — no other change is
needed. W-broad is the safer direction to be wrong in: it can only over-grant write to a
designer who holds a task on the project, whereas W-narrow chosen wrongly would lock
designers out of live projects that were never stamped with an `assigned_design`.

**Location:** `projects/permissions.py` — `user_can_edit_project_boq()`

---

## D. Found during Part 1 (OPEX design data model) — recorded, deliberately not fixed

Part 1 added `'Design Head'` to `UserProfile.ROLE_CHOICES` as a real role. No user was
migrated onto it and `is_design_head` was not removed, so nothing below is currently
live — but each is a gap that opens the moment the first user is given the new role.

| # | Finding | Location |
|---|---------|----------|
| D1 | `_SA_EDITABLE_ROLE_CHOICES` is a hardcoded role list that does **not** include `'Design Head'`, so a System Admin cannot assign the new role. Meanwhile the user create/edit dropdowns derive from `UserProfile.ROLE_CHOICES` and now *do* offer it — the two role pickers in the product disagree about which roles exist. Part 4 needs this resolved before a Design Head can be provisioned through the UI. | `projects/views.py:8758` |
| D2 | Docstring is now factually false: it states `'Design Head' is not yet in UserProfile.ROLE_CHOICES`, which Part 1 changed. The test still passes — it sets `profile.role` directly and `choices` is not enforced on `save()` — so this is a stale comment, not a broken test. | `projects/tests_permissions.py:256` |
| D3 | `EOD_DIGEST_EXCLUDED_ROLES` has no entry for `'Design Head'`, so the first holder of the role will start receiving end-of-day digests built for delivery roles. Decide whether Design Head is excluded (like CEO/Admin/System Admin) or gets its own content branch. | EOD digest command / notification config |

### D4 — Conditional-requirement rules: two enforced, one deliberately not

Three fields in the Part 1 models carry "required when *condition*" rules. Two were added
as DB `CheckConstraint`s in migration `0049`:

- `DesignAttempt.qc_remarks` — required when `qc_verdict = 'failed'`
- `ArkaSubmission.rejection_reason` — required when `verdict = 'rejected'`

`DueDateCommitment.change_reason` ("required when this is not the first commitment") was
**not** constrained. A `CHECK` sees only the row being written, and this rule depends on
whether sibling rows exist for the same assignment, so it cannot be expressed as one. It
remains a view-layer rule and is currently unenforced anywhere — the first code that
creates a superseding commitment must enforce it.

Both constraints test for the empty string only. The fields are `NOT NULL` with
`default=''`, so there is no null case, but a **whitespace-only** value still passes.
Rejecting that belongs with form validation in a later part.

### D5 — `DesignSubmission` overlaps the new design models

`DesignSubmission` (model + two read views, no write path — carried from A4/C7) covers
roughly the same ground as the new `ArkaSubmission` + `DesignFile` pair: a design artifact
submitted by a Design user, with a pending/approved/rejected verdict plus `reviewed_by` /
`reviewed_at` / `review_notes`, and a stored file. It differs in being flat per-project
with no attempt, no versioning, no Arka pairing, and a public `file_url` rather than the
bucket + path the new models use.

Part 1 left it entirely alone by instruction — not repurposed, extended or removed. The
open question is whether it should be deleted once the OPEX design module lands, since it
is dead code today and its presence invites a future contributor to wire the two together.

---

## E. Found during Part 2 (storage, survey, allocation, due-date handshake)

| # | Finding | Location |
|---|---------|----------|
| E1 | **The two new screens have no navigation entry point.** `/programs/<pk>/design/` and `/design/my-sites/` are reachable only by typing the URL. Adding links means editing `base.html`, which Part 2's hard rules forbid ("any existing view or template must change" is a stop condition). A Design Head and designer cannot find these screens unaided until a nav link is added. | `projects/templates/base.html` |
| E2 | `get_supabase_client()` reads `os.environ` directly, while `settings.py` reads the same values through `python-decouple`. Railway populates `os.environ`, so it works in production, but locally (where credentials live in `.env`) the helper raises `SUPABASE_URL and SUPABASE_KEY must be configured`. Any local testing of the four existing public-bucket upload paths is therefore impossible without exporting the vars by hand. The new `design_storage.py` reads `settings` instead and works in both. | `projects/supabase_storage.py:12-13` |
| E3 | Part 2's brief specified "Tailwind + Alpine + Lucide per project convention", but Tailwind and Alpine load **only** in `projects/admin/admin_base.html` (the portal-admin chrome). Every user-facing screen extends the Bootstrap `base.html`. The two new screens follow the sibling screens (Bootstrap) rather than adding a Tailwind CDN to `base.html`. If Tailwind is genuinely wanted for user-facing screens, that is a base-template decision, not a per-screen one. | `projects/templates/base.html` vs `projects/admin/admin_base.html` |
| E4 | **Blocked duration is recoverable but not a first-class field.** Clearing a block leaves `survey_returned_at` in place and sets `survey_uploaded_at`, so the most recent stopped-clock interval is the difference between them, and the full history is in `ActivityLog` (`design_blocked` / `design_survey_unblocked`). A second block overwrites `survey_returned_at`, so only the latest interval is directly queryable. Part 5's overdue maths may want a dedicated `blocked_seconds` accumulator or a separate block-history row; adding one is a schema change and was out of scope for Part 2. | `projects/models.py` — `DesignAssignment` |
| E5 | Replaced survey objects are never deleted from the private bucket. `build_design_path()` mints a fresh uuid4 per upload, so a replacement never overwrites its predecessor — deliberate, since an older `DesignAssignment` state may still reference the old path, but it means orphaned objects accumulate. `purge_deleted_files` does not know about the design bucket. | `projects/design_storage.py`, `projects/management/commands/purge_deleted_files.py` |
| E6 | Reallocation after design work has started is refused with a message rather than supported. `_allocate_one()` rejects any status outside `awaiting_allocation` / `allocated` / `due_date_proposed`. Handing a half-finished site to another designer is a real scenario that Part 3+ will need to decide on (what happens to the in-flight attempt, the agreed due date, and the artifacts). | `projects/design_views.py` — `_allocate_one` |
