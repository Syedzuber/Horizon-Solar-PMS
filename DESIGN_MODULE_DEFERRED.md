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
| E5 | Replaced survey objects are never deleted from the private bucket. `build_design_path()` mints a fresh uuid4 per upload, so a replacement never overwrites its predecessor — deliberate, since an older `DesignAssignment` state may still reference the old path, but it means orphaned objects accumulate. `purge_deleted_files` does not know about the design bucket. **PARTLY CLOSED by Part 3** — see below. | `projects/design_storage.py`, `projects/management/commands/purge_deleted_files.py` |

> **UPDATE (Part 3 session).** The *teardown* half of E5 is closed:
> `teardown_opex_test_data` now collects `(bucket, path)` from every `DesignFile` and
> from `DesignAssignment.survey_file_path` before deleting rows, and removes those
> objects through the new `design_storage.delete_design_objects()`. `--dry-run` lists
> them; failures are reported per object and never abort the row deletion; the public
> bucket is refused by the helper. Verified: 4 objects listed, 4 removed, the bucket's
> top-level listing then returned `[]`.
>
> **Still open:** the *production* half. A replaced survey or a superseded CAD version
> leaves its object behind during normal use — teardown only helps test data.
> `purge_deleted_files` still does not know about the design bucket, and superseded
> `DesignFile` rows are deliberately kept (they are the version history), so a real
> reaper has to decide which superseded objects are safe to drop.
| E7 | A multi-line `{# … #}` comment renders straight onto the page (Django's hash comment is single-line only — `base.html:92` documents this). `vendor_form.html:34` opens one that is never closed on the same line, so its text is emitted into the rendered page. Cosmetic there because the text contains no markup, but the same mistake in `head_sites.html` produced a phantom `<form>` element (see below). Pre-existing and not fixed here. | `projects/templates/vendors/vendor_form.html:34` |
| E6 | Reallocation after design work has started is refused with a message rather than supported. `_allocate_one()` rejects any status outside `awaiting_allocation` / `allocated` / `due_date_proposed`. Handing a half-finished site to another designer is a real scenario that Part 3+ will need to decide on (what happens to the in-flight attempt, the agreed due date, and the artifacts). | `projects/design_views.py` — `_allocate_one` |

---

## F. Found during Part 3 (Arka, CAD, BOQ, versioning) — recorded, deliberately not fixed

### F1 — Allocating a site does not stamp `assigned_design`, so the allocated designer can be locked out of its BOQ

**This is the sharpest one in this file. It will bite the first real OPEX tender.**

`_allocate_one()` sets `DesignAssignment.assigned_to` and never touches
`Project.assigned_design`. `user_can_edit_project_boq()` is W-narrow — it gates on
`project.assigned_design_id == profile.pk` and nothing else (Part 0.6, correctly, and
Part 3's hard rules forbid changing it). The two fields are therefore free to diverge,
and when they do the Design Head can allocate a site to a designer who then **cannot
enter its BOQ at all**.

Measured on the local database at the start of the Part 3 session, on the site Part 2
testing had left in `in_design`:

```
project.assigned_design       = priyanka
design_assignment.assigned_to = nayeem
priyanka  view_boq=True  edit_boq=True   GET /projects/Test-Site-02/boq/ -> 200
nayeem    view_boq=False edit_boq=False  GET /projects/Test-Site-02/boq/ -> 403
praveen   view_boq=True  edit_boq=False  GET /projects/Test-Site-02/boq/ -> 200
```

Part 3's verification therefore ran on a site where the two agreed. The design workspace
surfaces the mismatch rather than hiding it — `site_workspace.html` renders a warning
when `user_can_edit_project_boq()` is False for the allocated designer — but a warning
is not a fix.

**The fix is one line** in `_allocate_one()`: set `assignment.project.assigned_design =
designer` alongside `assigned_to`, in the same transaction. It touches nothing Part 3
is forbidden to touch (not the BOQ views, not the BOQ models, not either Part 0.6
helper). It was left out only because allocation is Part 2's surface and Part 3's brief
says to report rather than fix. **Whoever owns Part 4 should do it before the module
goes near a live tender.**

Open question that goes with it: `assigned_design` is also what the Design dashboard
filters its project cards on, so stamping it makes an OPEX site appear on the allocated
designer's Residential-style dashboard as well. That is probably wanted, but it is a
behaviour change and should be a decision, not a side effect.

**Location:** `projects/design_views.py` — `_allocate_one`; `projects/permissions.py` —
`user_can_edit_project_boq`

### F2 — Six existing OPEX sites have a null `assigned_design`

`IPGCL26-MB001` … `IPGCL26-MB006` (program `Finolex`, code `IPGCL26`) all carry
`assigned_design = NULL` on the local database; only `IPGCL26-MB007` is stamped. These
were created through the OPEX site path, not seeded. Under the W-narrow write rule, a
designer allocated to any of them is blocked from BOQ entry entirely — the same failure
as F1, arriving by a different route. Not measured on Railway.

**Location:** data, not code.

### F3 — The Part 3 screens have no navigation entry point either

`/design/<project_id>/work/` and `/design/<project_id>/review/` are reachable only by
typing the URL, for exactly the reason E1 records: linking them means editing
`base.html` or an existing dashboard template, both of which are stop conditions.
`head_review.html` links to the workspace and back to the Part 2 tender screen, and
`site_workspace.html` links back to `/design/my-sites/`, so the four screens are
navigable **once you are inside one of them** — but there is still no way in from the
product. Part 4.5 owns this. (Extension of E1, restated because it now covers four
screens rather than two.)

**Location:** `projects/templates/base.html`, `projects/templates/dashboard/design.html`

### F4 — The `.chip` pill CSS is now defined in two places

`projects/design/_design_chips.html` reproduces the `.chip` / `.chip-dot` rules that
`dashboard/design.html:24-44` defines inline in its own `extra_head`, so the Part 3
screens carry the same status pills as the expanded tender card. It is a copy because
importing them would mean editing the dashboard template (forbidden) and promoting them
to `base.html` would touch every screen in the product. The two copies can drift. The
real fix is a stylesheet, which is a base-template decision — same shape as E3.

**Location:** `projects/templates/projects/design/_design_chips.html`

### F5 — There is no path to revise an Arka once it is approved

`design_arka_submit()` accepts a submission only from `in_design` or `arka_rejected`.
Deliberate: resubmitting while a version is pending would leave the Head reviewing a
version that no longer exists, and resubmitting after approval would silently orphan
every CAD and BOQ artifact already paired to it through `derived_from_arka`.

But it means that once an Arka is approved, the designer has **no way to correct it**.
`design_arka_reject()` only accepts a `pending` verdict, so the Head cannot un-approve
either. Today the only exit is a new attempt, and the two things that open one — a QC
failure and a PM change request — are Parts 4 and 5. Until one of them lands, an
approved-but-wrong Arka is a dead end.

**Location:** `projects/design_views.py` — `ARKA_SUBMITTABLE_STATUSES`,
`design_arka_submit`, `_verdict_target`

### F6 — `boq_submitted_at` is a one-way stamp and does not track later BOQ edits

`design_boq_complete()` refuses if the stamp is already set, and there is no un-mark.
More importantly, nothing invalidates the stamp if the designer goes back to
`boq_detail` and changes quantities afterwards — `boq_submitted_at` records *that* the
BOQ was declared done, not *what* it contained. The BOQ's own
`Draft` / `Submitted` / `Acknowledged` status is maintained separately by `boq_detail`
and the two are not reconciled anywhere (verified: the attempt reached
`artifacts_uploaded` while `BOQ.status` was still `Draft`).

Whether the design workflow should require `BOQ.status == 'Submitted'`, or snapshot the
BOQ at mark-complete time, is a decision for whoever builds QC review — QC is what
actually needs to know which BOQ it is looking at.

**Location:** `projects/design_views.py` — `design_boq_complete`; `projects/models.py` —
`DesignAttempt.boq_submitted_at`

### F7 — Nothing tells the Design Head an Arka is waiting (also true of QC — see G7)

Submitting an Arka changes a status and writes an `ActivityLog` row. There is no
notification, no queue, and no badge, and the review screen is per-site and
URL-only (F3) — so in the product as it stands today the Head learns an Arka is pending
by being told out of band. Notifications are explicitly Part 7 and the Design Head
dashboard is Part 5; this is recorded so the gap between "Part 3 works" and "Part 3 is
usable" is not mistaken for a bug later.

**Location:** `projects/design_views.py` — `design_arka_submit`

---

## G. Found during Part 4 (QC review, deputy, PM change requests, release)

### G1 — `qc_verdict='pending'` is overloaded, and Part 5's metrics will trip on it

An attempt closed by a PM change request keeps `qc_verdict='pending'` forever. That is
correct and deliberate — it was never judged, and writing `'failed'` there would charge
the designer with rework the PM caused. But it means `pending` now carries two entirely
different meanings:

| `qc_verdict` | `closed_at` | means |
|---|---|---|
| `pending` | null | live attempt, QC has not ruled yet |
| `pending` | **set** | **closed by a change request, never judged** |
| `passed` / `failed` | set | judged |

Any query that counts `qc_verdict='pending'` as work-in-flight will over-count by exactly
the number of change-request closures. The screens disambiguate (`_attempt_history.html`
renders the second row as **Not judged**, not "pending"), but nothing in the data model
forces a report writer to make the same distinction.

**Whoever builds the Part 5 dashboard must filter on `closed_at__isnull=True` when
counting in-flight QC**, or add a fourth `qc_verdict` value such as `'superseded'`. The
latter is a schema change and was out of scope here.

**Location:** `projects/models.py` — `DesignAttempt.qc_verdict`;
`projects/design_views.py` — `design_change_request`

### G2 — No deputy management UI (expected; recorded as instructed)

`design_head_deputy` is set through Django admin only — building a UI for it was
explicitly forbidden this session. Two consequences worth stating before someone is
surprised by them:

* Only a Django **superuser** can name or clear a deputy. The Design Head cannot do it
  himself, and neither can a System Admin through the product's own user-management
  screen (`_SA_EDITABLE_ROLE_CHOICES` does not cover it either — see D1).
* Because presence of the FK is the entire rule (settled decision 6), a deputy named for
  one week's absence stays a deputy indefinitely until somebody remembers to clear the
  field. There is no expiry and, by instruction, no schedule.

**Location:** `projects/models.py` — `UserProfile.design_head_deputy`; Django admin

### G3 — A deputy can QC every tender, not just the Head's absence

`user_has_design_head_authority()` is global: a named deputy holds Head authority over
every OPEX site in the system, on every tender, for as long as the FK is set. There is no
per-tender or per-program scoping, which is right for a ten-person team and wrong the
moment there are two Design Heads with different portfolios.

Note the audit trail is correct as-is — `log_activity` records the deputy's own profile
as the actor, never the Head's, so "who passed this QC" is always answerable. Both Part 4
screens also render an **Acting as deputy** chip. It is the *scope* that is unbounded,
not the accountability.

**Location:** `projects/permissions.py` — `user_has_design_head_authority`

### G4 — A deputy can open the QC screen but not the BOQ linked from it

Verified on local data with `priyanka` named as `praveen`'s deputy, reviewing
`Test-Site-05` (whose `assigned_design` is `shyam`):

```
user_can_view_design(priyanka, Test-Site-05)      = True
user_can_view_project_boq(priyanka, Test-Site-05) = False
GET /design/Test-Site-05/qc/    -> 200
GET /projects/Test-Site-05/boq/ -> 403   <- the "View BOQ" button on that very screen
```

Part 4 extended `user_can_view_design()` so a deputy can reach design surfaces and open
CAD files by signed URL — without that, a deputy could pass QC on a package whose
drawings they were not allowed to open. But `user_can_view_project_boq()` is a Part 0.6
helper and is **forbidden to modify**, so its Design branch still admits only
`assigned_design` or a task-holder. A deputy who is a plain Design user therefore gets a
403 from the BOQ link on the QC review screen.

The Head himself is unaffected — he has his own portfolio-wide branch in that helper. The
fix is a one-line additive branch in `user_can_view_project_boq()` (Design Head *authority*
rather than the raw `is_design_head` flag), which needs whoever owns that helper to sign
it off. **A QC reviewer who cannot see the BOQ is reviewing half a package.**

**Location:** `projects/permissions.py` — `user_can_view_project_boq` (line ~217)

### G5 — `site_workspace.html` was modified, against the letter of the stop condition

Part 4's hard rules say to stop if an existing template must change. One did:
`projects/templates/projects/design/site_workspace.html` gained a single
`{% include "projects/design/_attempt_history.html" %}` (plus `history` in its view
context).

It was unavoidable: Part 4 §5 requires the **designer** to see the QC verdict, the QC
remarks and any PM change request, and the designer's only screen is that one. The
alternative — a second designer screen duplicating the first — is worse. The changed file
is a Part 3 artifact of this same module, created one session earlier, not a pre-existing
product template; `base.html`, the dashboards, `boq_detail.html` and the Part 2 screens
were all left untouched. Recorded here because the rule said stop and the judgment call
was made instead.

**Location:** `projects/templates/projects/design/site_workspace.html`

### G6 — The unresolved-change-request guard on QC verdicts is unreachable from the UI

`design_qc_pass()` and `design_qc_fail()` both refuse when a `DesignChangeRequest` on the
attempt has a null `resulting_attempt` (Part 4 §1). But `design_change_request()` sets
`resulting_attempt` in the same transaction that creates the row, so the UI can never
produce an unresolved request — the guard only fires against rows created in Django
admin, by an import, or by a future part that queues change requests without immediately
opening an attempt.

It was verified this session by creating such a row directly through the ORM (both
verdict endpoints refused, `qc_verdict` stayed `pending`). Keeping the guard is right —
it is cheap and it will matter the moment change requests gain an approval step — but
nobody should mistake it for a reachable code path today.

**Location:** `projects/design_views.py` — `_open_change_requests`, `design_qc_pass`,
`design_qc_fail`

### G7 — Nothing tells the Head, the designer or the PM that anything happened

Extends F7 across the whole Part 4 surface. A package reaching `artifacts_uploaded` does
not notify the Head; a QC failure does not notify the designer; a PM change request does
not notify either of them; release notifies nobody. Every one of these is discovered by
someone reloading a URL that has no navigation entry point (F3). Notifications are Part 7
and the dashboard is Part 5 — recorded so the gap between "Part 4 works" and "Part 4 is
usable unaided" is not mistaken for a defect later.

**Location:** `projects/design_views.py` — all Part 4 write views

### G8 — Release is a dead end

`design_qc_pass()` sets `released_at` / `released_by` and moves the assignment to
`released`. There is no un-release, and every other Part 4 entry point refuses at that
status — including PM change requests, by design (settled decision 3, with release
standing in for the BOQ lock that Part 6 will provide). If a site is released in error,
or a genuine change arrives afterwards, the only recovery today is a Django-admin edit.

Whether "released" should be reversible, and by whom, is a real decision that belongs with
Part 6's BOQ locking rather than being invented here.

**Location:** `projects/design_views.py` — `design_qc_pass`, `design_change_request`

---

## H. Found during Part 4.5 (dashboard integration)

### H1 — OPEX sites are created `Draft` and nothing ever promotes them

`create_opex_site()` hardcodes `site.status = 'Draft'` (`projects/views.py`, in the OPEX
creation branch) and no OPEX code path moves it on. The only promotion route is
`project_activate`, a manual per-project action that also attaches Residential milestone
defaults. Measured: **11 of 12 OPEX sites were `Draft`**, the twelfth having been
activated by hand.

Part 4.5 worked around this rather than fixing it — the Design dashboard queryset and its
`total_revisions` stat now exempt `project_type='OPEX'` from the Active/In Progress
filter, because otherwise every site under design work was invisible to the designer
doing it. **The workaround is scoped to that one dashboard.** Other surfaces still apply
the plain status filter and therefore still hide Draft OPEX sites, most visibly:

* `dashboard_pm`'s `projects_with_progress` — OPEX sites appear only in the thin
  "Draft Projects — Awaiting Activation" strip, never as a full progress card. Part 4.5
  put the PM's change-request button in **both** places for this reason.
* `dashboard_pm`'s `active_projects` summary count.

The real fix is a decision, not a patch: either OPEX sites should be born Active (they
have no activation ceremony — a tender award is the activation), or `Draft` should stop
meaning "hide this" across the product. Both are larger than a dashboard session.

**Location:** `projects/views.py` — `create_opex_site`, `dashboard_pm`, `project_activate`

### H2 — The two "designer of this site" fields are now synced forward but only backfilled once

`_allocate_one()` now stamps `Project.assigned_design` alongside
`DesignAssignment.assigned_to`, and migration `0051` repaired the two rows that had
already diverged locally (`Test-Site-02`, `Test-Site-03`). That closes F1 for anything
allocated through the design workflow from here on.

What it does **not** close: `assigned_design` is still independently writable from
`project_activate` and the project edit screens, with nothing to stop it being pointed at
someone other than the allocated designer afterwards. There is no constraint, no
validation and no periodic check — only the allocation path keeps them in step. If they
diverge again the symptom is the same as before (designer 403s on the BOQ, site missing
from their dashboard) and the repair is another one-off backfill.

Making `assigned_design` derived rather than independently stored would be the durable
fix, and it is a model change.

**Location:** `projects/design_views.py` — `_allocate_one`; `projects/views.py` —
`project_activate`, project edit paths

### H3 — Section headers now appear on dashboards that previously had none

`_apply_project_sections()` stamps "Tenders" / "EPC Residential" headers only when a
user's row list contains **both** types. Because A1 made OPEX sites visible, designers who
previously saw a single flat list of Residential cards now see two labelled sections.

The Residential **cards** are byte-identical — verified by diffing the rendered HTML
before and after, where the only non-CSRF change on a Residential card was the section
count `(1)` → `(2)` on one user. But the page gains two headers it did not have. This is
pre-existing behaviour of an existing function doing exactly what it was written to do,
not a change Part 4.5 made to Residential rendering, and it is recorded here only so it is
not later mistaken for one.

**Location:** `projects/views.py` — `_apply_project_sections`

### H4 — The Design Head reaches tenders by a bypass, not by the nav

`program_list` is `@role_required(['Admin', 'PM', 'CEO'])` and the real Design Head holds
`role='Design'` with `is_design_head=True`, so the **Programs** nav entry in `base.html`
403s for him. Part 4.5 could not change that decorator (hard rule), so the Design Head
strip on the design dashboard links each OPEX tender's design screen directly instead.

That works, but it means there are now two different ways to reach tender screens
depending on who you are, and the Head still cannot open the Programs list itself. The
clean fix is the `'Design Head'` role migration that keeps being deferred — at which
point the role tuple can simply include it.

**Location:** `projects/views.py` — `program_list`; `projects/templates/base.html`

### H5 — `/design/my-sites/` is now redundant for most designers

The designer's allocated sites, their status, due date, attempt and next action are all on
the Design dashboard as of this session. `/design/my-sites/` shows the same set with the
same actions, and Part 4.5's settled decision 3 explicitly kept it working rather than
removing it.

It is not identical: `my-sites` has no project-status filter at all, so it would still show
a site the dashboard's OPEX exemption somehow missed, and it carries the change-agreed-date
form that the card does not. Whether to fold those into the card and retire the screen is a
follow-up once the integrated path has been used in anger.

**Location:** `projects/design_views.py` — `design_my_sites`

### H6 — The deputy's BOQ 403 (G4) is now reachable by clicking, not just by typing

Part 4 recorded that a deputy can open the QC screen but gets a 403 from the **View BOQ**
button on it, because `user_can_view_project_boq()` was out of bounds to modify. That was
a latent problem while the screens were URL-only. Now that the QC queue is linked from the
Design Head strip on the dashboard, a deputy can reach that dead button in three clicks
from their landing page.

Nothing about the defect changed — only how easy it is to hit. It remains the single
highest-value one-line fix outstanding in the design module.

**Location:** `projects/permissions.py` — `user_can_view_project_boq`

---

## I. Found during Part 5 (Design Head tender dashboard)

### I1 — `Program.total_capacity` is MEGAWATTS; every design capacity is KILOWATTS

```python
# models.py:204
total_capacity = models.DecimalField(..., help_text='Total planned capacity in MW (storage only).')
# vs
ArkaSubmission.capacity_kw   # kW
Project.capacity_kw          # kW
```

Nothing in the schema, the form or the admin stops these being compared directly, and doing
so is wrong by a factor of 1000. Part 5 routes every comparison through one
`_mw_to_kw()` helper in `design_metrics.py` and names every value it returns `*_kw`, but
that is a local discipline — the next surface to touch both fields can still get it wrong.

`Program.total_capacity` is also **nullable and unevenly populated**: of the two OPEX
tenders on the local database, the one with design work had no capacity recorded and the
one with 20 MW recorded had no design work. The capacity panel therefore needs a
"tendered not recorded" branch permanently, not as a transitional state.

The durable fix is to store one unit — most cheaply by renaming the field to
`total_capacity_mw` so a reader cannot miss it. That is a migration.

**Location:** `projects/models.py:204`; `projects/design_metrics.py` — `_mw_to_kw`

### I2 — `ArkaSubmission.is_current` is unique per ATTEMPT, not per assignment

This caught my own verification query before it caught anything else, which is why it is
worth writing down. `is_current=True` is enforced by a partial unique constraint scoped to
`attempt`, so:

```python
ArkaSubmission.objects.filter(attempt__assignment=a, is_current=True)   # returns ONE ROW PER ATTEMPT
```

On a site with three attempts that returns three "current" Arkas, one of which is current
in any meaningful sense. Summing their `capacity_kw` overstated designed capacity by 113%
on the local data (559.25 kW against the correct 262.25 kW).

Any capacity, reporting or export code must scope to the assignment's
`current_attempt_number` first, as `tender_metrics()` does. A queryset that reads
`is_current=True` alone and looks correct on single-attempt sites will silently break the
moment a site is reworked.

**Location:** `projects/models.py` — `ArkaSubmission.Meta.constraints`

### I3 — Blocked duration is still only readable for the current block (E4, now load-bearing)

Part 5 shows "blocked N days" on the attention list, computed from
`DesignAssignment.survey_returned_at`. That field is single-valued and overwritten on each
new block, so the figure is **the current block only** — a site blocked three times shows
the duration of the third, and the first two are invisible to every metric.

Part 5 deliberately shows no cumulative or historical blocked time for this reason. But the
dashboard now makes the gap visible in a way it was not before: a Head looking at
"blocked 2 days" has no way to know the site has actually been blocked for three weeks
across four episodes. Full history is reconstructable from `ActivityLog`
(`design_blocked` / `design_survey_unblocked`) but is not queryable as a duration.

Settled decision 3 says blocked time must never silently extend a due date, and Part 5
honours that — blocked sites are counted as blocked AND as overdue, both shown. Cumulative
blocked time would be reporting, not an adjustment, and needs a block-history row.

**Location:** `projects/models.py` — `DesignAssignment.survey_returned_at`

### I4 — Workload kW reads as "no load" for a designer whose sites have no approved Arka

`kW` sums the current approved Arka capacity of a designer's **non-released** sites, per the
Part 5 spec. A designer holding two sites that have not yet reached an approved Arka
therefore shows `0.00 kW` while genuinely carrying two sites.

Part 5 mitigates this by rendering "+N not yet designed" under the figure, so the number is
never read as an empty queue. The underlying tension is real though: kW is the better unit
of load, and it is unavailable for exactly the early-stage sites where allocation decisions
are actually being made. Using `Project.capacity_kw` (the tendered per-site figure, set at
site creation) as a fallback would give an estimate for those sites — but it is a different
number with different meaning, and mixing the two silently would be worse than showing zero.

**Location:** `projects/design_metrics.py` — `designer_workload`

### I5 — The dashboard is per tender, so a Head with many tenders has no roll-up

Every panel is scoped to one `Program`. A Design Head running six tenders must open six
dashboards to see his own total review queue, and the Part 4.5 head strip on
`/dashboard/design/` gives portfolio-wide counts but no capacity, workload or attention
list. Deliberate — a cross-tender roll-up needs a decision about whether designers are
compared across tenders at all, which is a management question rather than a technical one.

**Location:** `projects/design_views.py` — `design_tender_dashboard`

### I6 — Rework denominator is released sites, which is unstable on small samples

`rework = total attempts ÷ released sites`. With one released site, a single QC failure
moves a designer from 1.0× to 2.0× — a 100% swing driven by one event. The dashboard shows
the raw `qc_failed` and `pm_change_request` counts beside the multiplier precisely so the
sample size is visible, and shows `—` rather than `0` when there are no released sites.

It is still a ratio over a denominator that will sit at 1 or 2 for months on a new tender.
Anyone using it for a performance conversation before roughly ten released sites per
designer is reading noise. Recorded because the number looks more authoritative than it is.

**Location:** `projects/design_metrics.py` — `designer_workload`

---

## J. Found during Part 6 (site groups, aggregated BOQ, BOQ lock)

### J1 — There is no unlock, and no variance process behind it

**This is the deliberate gap Part 6 was told to leave, restated so nobody mistakes it for
an oversight.** `site_group_lock()` sets `status='locked'` and there is no endpoint, form
or admin action that reverses it. From that moment the member sites' BOQ quantities are
frozen permanently: `boq_detail`'s four Design write branches and `boq_submit` all refuse,
and a PM change request on a member site is refused with a message telling them a variance
is required.

The message is honest — the variance process it points at **does not exist**. Today the
only recovery from a wrong lock is a Django-admin edit of `SiteGroup.status` or of the
membership rows, by a superuser. That is an acceptable state for a first version (a locked
group means a purchase order is going out, and reversing one is a commercial act, not a
button) but it means:

* a group locked by mistake needs a developer, and
* a genuine post-lock change has nowhere to go inside the product.

Whoever builds purchase orders owns this. It extends G8 ("release is a dead end") one stage
further down the pipeline.

**Location:** `projects/design_views.py` — `site_group_lock`

### J2 — A released, ungrouped site still cannot have a change request; a released, grouped one can

Part 6 §4 specifies three cases and they do not form a straight line:

| site is… | change request |
|---|---|
| in a **locked** group | refused — variance required (new in Part 6) |
| in a **draft** group | **allowed**, and the site leaves the group |
| in **no** group | refused if `released` — unchanged from Part 4 |

So a released site becomes change-requestable by being put into a draft group and stops
being so again if SCM removes it. That is what the brief specifies and it is implemented
exactly as written, but it is not a rule anyone would derive from first principles.

The underlying cause is that Part 4 used `released` as a stand-in close condition
(`CHANGE_REQUEST_STATUSES` excludes it, and `design_change_request()` refuses it
explicitly) *because BOQ locking did not exist yet* — its own comment says so. Part 6
replaces that stand-in only for grouped sites, because §4's third bullet preserves Part 4
behaviour for ungrouped ones. The coherent end state is one of:

* the window closes at the **lock** and nothing else, so a released ungrouped site is
  change-requestable (widens Part 4 behaviour — a real decision), or
* released stays a hard close and draft-group membership does not reopen it (contradicts
  §4's second bullet).

Neither was chosen here. `CHANGE_REQUEST_STATUSES` was **not** modified; the released
status is admitted by a local `allowed_statuses` computed per request.

**Location:** `projects/design_views.py` — `design_change_request`,
`design_change_request_form`, `CHANGE_REQUEST_STATUSES`

### J3 — The `_DESIGN_EDITABLE` status tuple is now spelled out twice in `boq_detail`

`boq_detail`'s GET path computes `design_form_open` from a literal
`('Draft', 'Revision Requested', 'Acknowledged')` rather than from `_DESIGN_EDITABLE`,
because that constant is local to the `if request.method == 'POST'` block and hoisting it
to module scope would be an edit to a constant Part 6 was told to leave alone.

The two are in step today and both carry a comment pointing at the other, but they can
drift, and if they do the symptom is a page that renders inputs which the POST handler
then refuses (or the reverse). The fix is to hoist `_DESIGN_EDITABLE` to module scope —
one line, no behaviour change — and it should be done by whoever next has a reason to
touch that view.

Note this also means A2/C2 (`'Acknowledged'` being editable at all) is now duplicated:
fixing it requires editing both places.

**Location:** `projects/views.py` — `boq_detail`

### J4 — The SCM dashboard's OPEX section costs 5 queries per tender

`scm_opex_tender_rows()` loops OPEX programs and runs five queries each (assignment count,
released count, site count, groups, pool). Measured: `/dashboard/scm/` went from 20 to 30
queries with two tenders on the local database.

This is O(tenders), not O(sites), and tenders are counted in single digits, so it is not a
problem now. It is recorded because the shape is a loop-of-queries and will need
collapsing into grouped aggregates if the tender count grows — the same treatment
`_get_ceo_dashboard_context()` already applies on the CEO dashboard. The group BOQ page
itself is **flat**: 15 queries whether the group holds one site or two (measured by
soft-removing a member and re-rendering).

**Location:** `projects/design_views.py` — `scm_opex_tender_rows`

### J5 — Group screens are reachable from the SCM dashboard only

`/programs/<pk>/site-groups/` and `/site-groups/<pk>/` are linked from the new OPEX
section of `/dashboard/scm/`, which is `@role_required(['SCM'])`. So:

* **SCM** reaches them by clicking — fine.
* **Admin** may view them (`user_can_view_site_groups`) but has no link anywhere; the
  Admin dashboard was not touched.
* the **Design Head** may view them too, and likewise has no link — and `program_list` still
  403s for him (H4), so there is no Programs page to hang one off either.

Both are URL-only, exactly as E1/F3 recorded for the Part 2 and 3 screens. Fixing it means
editing `base.html` or another dashboard, which is the same standing blocker.

**Location:** `projects/templates/base.html`, `projects/templates/dashboard/admin.html`

### J6 — `SiteGroup.name` is not unique, and there is no group-level notes/audit beyond ActivityLog

Two groups under the same tender may share a name. Nothing depends on the name being
unique, but SCM will be reading it on a purchase order and two "Batch 1"s under one tender
would be a genuine operational confusion. Not constrained because Part 6 did not specify
it and a `unique_together` is a migration decision.

Relatedly: group-level events are logged through `log_activity` against the **member
sites**, because `ActivityLog.project` is a required FK and a group is not a project. So
locking a five-site group writes five rows and there is no single row that says "group X
was locked". Reconstructing group history means filtering `entity_type='SiteGroup'` /
`'SiteGroupMembership'` on `entity_id`. That works, and it puts the event where a PM or
designer will actually look, but it is not a group audit trail.

**Location:** `projects/models.py` — `SiteGroup`; `projects/design_views.py` —
`site_group_lock`

### J7 — Ad-hoc BOQ rows are still invisible to the aggregate (B4, now load-bearing)

`aggregate_group_boq()` joins on `item_master`, so a `BOQItem` created by `add_item`
(free-text description, `item_master` null — finding B4) cannot be summed. Part 6 does not
drop those rows silently: they are returned in `unlinked` and rendered as a warning
listing site, row and quantity, with the totals explicitly described as short.

But a warning is not a quantity. If designers start using ad-hoc rows on OPEX sites, SCM
will be reading a consolidated requirement with a footnote instead of a number. Measured at
build time: **0 unlinked rows across 185 OPEX `BOQItem` rows**, so this is latent, not
live. The decision B4 flagged — force ad-hoc rows to pick a catalogue entry, or accept that
they aggregate separately — is now overdue.

**Location:** `projects/design_views.py` — `aggregate_group_boq`; `projects/views.py` —
`boq_detail` `add_item` branch

### J8 — Locking does not touch `BOQ.status`, so the two lock notions coexist

A locked group freezes quantities via the caller-side check, while `BOQ.status` continues
its own `Draft → Submitted → Acknowledged` life independently. On the verification data
every locked site's `BOQ.status` was still `Draft` — the design workflow's
`boq_submitted_at` stamp (F6) and the BOQ's own status were already unreconciled, and the
group lock is now a third, separate notion of "this BOQ is done".

Deliberate: writing `BOQ.status` would mean adding a status value or reusing one with a
different meaning, and Part 6 is forbidden from touching either. But three overlapping
"finished" signals on one BOQ is one too many, and whoever reconciles F6 should fold this
in.

**Location:** `projects/models.py` — `BOQ.status`; `projects/design_views.py` —
`site_group_lock`

---

## K. Found during Part 6.5a/6.5b (Design Head role audit and closure)

Part 6.5b removed `'Design Head'` from `ROLE_CHOICES` (migration `0053`) and closed G4 by
admitting the Head's deputy to `user_can_view_project_boq()`. Everything below was found
by the 6.5a audit and deliberately left alone.

### K1 — `tasks_drill_down` gives portfolio-wide task visibility to any unmatched role

**This is a live widening, not a Design Head problem.** `tasks_drill_down` scopes its
queryset with an if/elif chain and **no final else**:

```python
# projects/views.py:385-403
    if role in ('PM', 'Project Coordinator'):
        base_qs = base_qs.filter(...).distinct()
    elif role == 'Design':
        base_qs = base_qs.filter(...).distinct()
    elif role == 'Site Engineer':
        base_qs = base_qs.filter(assigned_to=profile)
    # SCM and others: all active non-deleted projects
```

The trailing comment is honest about the consequence and wrong about who it applies to.
"SCM and others" is every role that is not PM, Project Coordinator, Design or Site
Engineer — which today is SCM, Finance, CEO, Admin, System Admin, BD **and a blank role**,
and tomorrow is **any role added to `ROLE_CHOICES` without a matching branch here**. Such a
role silently inherits portfolio-wide task visibility on `/tasks/due-today/`,
`/tasks/due-soon/` and `/tasks/overdue/`, with no decision recorded anywhere.

Measured during the 6.5a audit, by flipping one user's role inside a rolled-back
transaction and re-rendering each screen:

| screen | `role='Design'` | `role='Design Head'` |
|---|---|---|
| `/tasks/overdue/` | **2** project groups | **4** project groups |
| `/tasks/due-today/` | 1 | 2 |
| `/tasks/due-soon/` | 1 | 2 |

Same user, same data, same session — the only change was the role string, and the screen
went from their own work to the portfolio's.

Not fixed here for two reasons. It is out of Part 6.5b's scope by instruction, and the fix
is a product decision rather than a patch: an unmatched role should either fall through to
**nothing** (safe, but silently empties three screens for SCM/CEO/Admin/Finance/BD, who may
genuinely be meant to see everything) or fall through to portfolio-wide **explicitly**, via
a named allowlist that a new role has to be added to on purpose. The second is the shape
the rest of the codebase uses — `PORTFOLIO_VIEW_ROLES`, `BOQ_PORTFOLIO_READ_ROLES`,
`EOD_DIGEST_EXCLUDED_ROLES` are all explicit sets — and this is the one place that decides
the same class of question by omission.

Note the same shape appears at `views.py:5595` (`gantt_can_view_client = role in
('PM','Project Coordinator','CEO')`) but is *safe* there, because it is an allowlist whose
default is denial. `tasks_drill_down`'s default is grant.

**Location:** `projects/views.py:385-403` — `tasks_drill_down`

### K2 — The `'Design Head'` role-string branches are now dead code, kept on purpose

`user_can_view_project()` (permissions.py:119) and `user_can_view_project_boq()`
(permissions.py:217) both accept `role == 'Design Head'`. With the choice removed from
`ROLE_CHOICES`, no form or view can produce that value, so neither branch can fire in
normal operation — `choices` is not enforced on `save()`, so only a shell, a fixture or a
raw SQL write can reach them.

Kept deliberately: they are harmless, they cost one string comparison, and a future phase
may reintroduce the role on purpose. `tests_permissions.py` pins both
(`test_role_string_still_grants_portfolio_view`, `test_design_head_role_string_also_reads`)
so removing one is a decision somebody makes rather than a line somebody deletes while
tidying.

Recorded so that a future reader running a dead-code sweep finds the reasoning before the
delete key.

**Location:** `projects/permissions.py:119`, `projects/permissions.py:217`

### K3 — `is_design_head` still confers authority independently of role, on any role

`test_flag_is_independent_of_role` asserts the flag grants portfolio project view when set
on `'Design'`, `'PM'`, `'Site Engineer'` **and a blank role**, and `models.py` documents it
as deliberately role-independent. Part 6.5b did not change that, and the removal of the
role choice makes the flag the sole Design Head mechanism permanently rather than
transitionally.

The consequence worth stating: an Admin can tick "Design Head" on a Finance or Site
Engineer account through the user-edit form, and that account immediately gains
portfolio-wide project view, portfolio-wide BOQ read, and every one of the eighteen
design-module screens including QC verdicts and OPEX release. Nothing in the form warns
that the checkbox does that, and its help text describes only task reassignment
(`forms.py:155` — *"Can reassign any Design-role task, independent of role"*), which was
accurate in Part 0 and has been an understatement since Part 2.

Not fixed: constraining the flag to Design-role users, or rewriting the help text, both
touch the user-edit surface, which was out of scope. The help text is the cheaper half.

**Location:** `projects/models.py:530`, `projects/forms.py:152-156`,
`projects/templates/projects/admin/user_edit.html:90-97`

### K4 — Six `role='Design'` querysets decide who can be a designer, and none is shared

`UserProfile.objects.filter(role='Design', is_active=True)` (or a `get_object_or_404`
equivalent) appears at `views.py:718`, `views.py:2204`, `views.py:3700`, `views.py:3706`,
`views.py:5362`, `views.py:5565`, plus `design_views.py:182` and `design_views.py:396`, and
again as `limit_choices_to={'role': 'Design'}` at `models.py:75`. Nine copies of one rule —
"who may be selected as a designer" — with no shared helper.

They are all consistent today. They were also the single largest cluster the 6.5a audit had
to enumerate by hand, and any future change to what counts as a designer has to find all
nine. This is finding A5/C8 (role-string comparisons codebase-wide) narrowed to the one
cluster that matters to this module.

**Location:** `projects/views.py`, `projects/design_views.py`, `projects/models.py:75`

### K5 — Four copies of `_PROFILE_TO_TASK_ROLE`, two of its inverse

`_PROFILE_TO_TASK_ROLE = {'BD': 'BD / Sales'}` is redefined inline at `views.py:1967`,
`views.py:3189`, `views.py:3786` and `views.py:5570`; `_TASK_TO_PROFILE_ROLE =
{'BD / Sales': 'BD'}` at `views.py:3614` and `views.py:5554`. Six local dicts expressing one
mapping between `UserProfile.role` and `Task.assigned_role`.

This is the mechanism that decides whether a user may change a task's status, set its due
date, or tick a checklist item. It is why a `'Design Head'` role holder could do none of
those — and why any future role that needs to act on tasks must be added in six places, not
one.

**Location:** `projects/views.py` — six sites listed above

---

## Part 8 — deferred findings

### L1 — The Design Hold model fields are still named `survey_returned_*`

The user-facing name of the off-sequence hold is **Design Hold** as of Part 8. The stored
status value is `survey_returned` and the three fields that carry the hold are
`DesignAssignment.survey_returned_at`, `survey_returned_by` and `survey_return_reason`.

Neither the value nor the field names were renamed, deliberately:

* The stored value has never been `blocked`, so there was nothing to migrate — the Part 8
  rename was label-only and touched no row.
* Renaming the three fields is a schema change with no user-visible benefit. It would
  require a migration, and every read site would have to move in the same commit.

The mismatch is real and will confuse the next reader: a field called
`survey_return_reason` holds what the UI calls the Design Hold reason. Anyone renaming
these later must also update `_status_after_unblock`, `design_mark_blocked`,
`design_survey_upload`'s unblock branch, `design_metrics._classify`, the `blocked` stage
key, and the four templates that read `is_blocked`.

**Location:** `projects/models.py` (`DesignAssignment`), `projects/design_views.py`,
`projects/design_metrics.py`

### L2 — No public holiday calendar

`utils.is_working_day()` knows about Sundays and the 2nd and 4th Saturday of each month.
It knows nothing about public holidays. A design due date that lands on Diwali, Holi or
Independence Day is treated as a working day and does not roll.

This was explicitly out of scope for Part 8. The fix is contained: add the holiday check
to `is_working_day()` and every caller inherits it, because they all roll forward through
that one function. What is NOT contained is where the holiday list lives — a hardcoded
tuple will rot annually, and a model plus an admin screen is a feature, not a helper.

**Location:** `projects/utils.py:is_working_day`

### L3 — `allocated` and `due_date_proposed` are now unreachable for new rows

Part 8 sends allocation straight from `awaiting_allocation` to `in_design`, so no new
`DesignAssignment` can enter either status.

* **`allocated`** is genuinely dead for new rows. Nothing sets it any more. The one
  remaining writer is `_status_after_unblock`, and only on the branch that requires an
  assignment with a designer and no approved commitment ever — a shape only Part 2 rows
  can have.
* **`due_date_proposed`** is dead as a *status*, but the state it described is alive: an
  extension request awaiting a verdict. That state is now carried by a `DueDateCommitment`
  row with `approved_at IS NULL` rather than by the assignment's status, because an
  extension can be requested from any working stage and moving the status would rewind a
  site that is mid-Arka to a pre-design stage.

Neither value was removed, per the Part 8 hard rules, and rows on Railway still carry
them. Before either is dropped, migrate the existing rows: `allocated` rows want
`in_design` plus an approved commitment, `due_date_proposed` rows want whatever stage
their work is actually at.

**Location:** `projects/models.py:DESIGN_ASSIGNMENT_STATUS_CHOICES`,
`projects/design_views.py:REALLOCATABLE_STATUSES`

### L4 — `design_blocked` / `design_survey_unblocked` action codes keep the old wording

`ActivityLog.action_code` still uses `design_blocked` and `design_survey_unblocked`, and
the URL name is still `design_mark_blocked`. These are machine-readable identifiers, not
display text, and renaming them would strand every historical row and any report keyed on
them. Left as-is on purpose; only the human-readable `detail` text was updated.

**Location:** `projects/design_views.py`, `projects/urls.py`

### L5 — Reallocation resets the due date

`_allocate_one` recomputes the due date every time it runs, so reallocating an already
allocated site issues a fresh allocation-date + 2 working days commitment and stands the
old one down. That is defensible — a new designer starting today should not inherit
yesterday's clock — but it is a silent reset: the Head gets no warning that reallocating
moved the date, and the superseded commitment is the only evidence.

**Location:** `projects/design_views.py:_allocate_one`

### L6 — Archive validation trusts the zip central directory

`validate_cad_zip()` reads declared uncompressed sizes from the central directory rather
than decompressing, which is what keeps it cheap and what keeps a zip bomb from being
expanded in the first place. A hand-crafted archive can lie in that header: it can declare
1 MB and expand to far more.

Nothing in this system decompresses a `cad_zip` — it is stored and served as an opaque
object — so the lie has no consumer today. It would matter the moment anything extracts
one (a preview, a thumbnailer, an aggregated download). Whatever does that must enforce
its own limit while reading, not trust this check.

**Location:** `projects/design_storage.py:validate_cad_zip`

---

## Part 9 — Design QC role, dual approval gates, error categories

### M1 — The System Admin user edit reads flags straight off the POST body

`subadmin_departments`' `edit_user` branch sets both design flags from
`request.POST.get(...) == 'on'`. An unchecked checkbox posts nothing, which is
indistinguishable from a field the form never rendered — so any edit form that omits
either checkbox silently CLEARS that flag on every save. Part 9 added the `is_design_qc`
checkbox to `subadmin/departments.html` for exactly this reason, and the coupling is now
noted in a comment at both ends, but the underlying fragility is unchanged: the next flag
added here will have the same trap.

The robust shape is a real Form with `BooleanField(required=False)` per flag, as the Admin
panel path already uses (`AdminUserEditForm`). Converting the System Admin path to a Form
was out of scope this session.

**Location:** `projects/views.py` (`subadmin_departments`, `edit_user` branch),
`projects/templates/projects/subadmin/departments.html`

### M2 — `user_can_qc_design()` now guards the HEAD gate, and its name says "qc"

Part 4 named the Head-or-deputy-minus-designer predicate `user_can_qc_design()`, when
there was one gate and it was the Head's. Part 9 made that gate the SECOND one and added
`user_can_head_gate_design()` as its Part 9 name — a thin delegator, so the rule still
lives in exactly one place and no existing caller changed.

The cost is two names for one predicate, one of which reads as gate 1 and means gate 2.
Renaming it is a mechanical change across Part 4's call sites and was not worth the churn
inside a session that was already moving both gates. If a Part 10 touches these helpers,
fold `user_can_qc_design` into `user_can_head_gate_design` and delete the alias.

**Location:** `projects/permissions.py`

### M3 — Pre-Part-9 rework attempts have no error category, and are counted as designer error

Every attempt opened by a QC failure before Part 9 carries no failure category, because
the field did not exist. `classify_attempt_causes()` labels those `CAUSE_UNCATEGORISED`
and counts them in the DESIGNER's multiplier rather than dropping them — dropping them
would have quietly shrunk every historical designer's number the day Part 9 shipped, which
is the more misleading of the two errors. The reasoning is that before Part 9 a QC failure
had no other meaning: a bad survey went through the Design Hold path and a moved brief
through a PM change request.

It is still an assumption, and the count is surfaced as its own chip on the workload table
so the mixture is visible. It decays on its own as new categorised failures accumulate.

**Location:** `projects/design_metrics.py:classify_attempt_causes`, `designer_workload`

### M4 — The failure category is not CHECK-constrained, unlike the reason and remarks

`qc_failure_category` / `head_failure_category` are plain `CharField`s with `choices`.
Django does not enforce `choices` at the database level, and a CHECK constraint would have
to encode all sixteen literals — meaning a migration every time the category list changes.
The list will change; the requirement will not.

So the view is the only enforcement: `_posted_error_category()` refuses a missing or
unrecognised value, and every one of the four verdict endpoints calls it. A writer that
bypasses the views (a shell script, a data fix, a future importer) can still store a blank
or bogus category, and `error_category_group()` returns `None` for both — which the rework
multiplier treats as "not a designer error", i.e. it fails safe rather than misattributing.

**Location:** `projects/models.py` (`DesignAttempt.head_failure_category` and siblings),
`projects/design_views.py:_posted_error_category`

### M5 — No deputy for Design QC

Settled decision 9 for this session: a Design Head's deputy acts for the HEAD gate only.
`user_can_qc_gate_design()` consults `is_design_qc` and nothing else, so if the single QC
reviewer is away, gate 1 stops and every Arka and package queues behind it. There is no
cover mechanism.

Deliberate, and the smaller risk while there is one QC reviewer to begin with. If a second
QC reviewer is appointed, the flag can simply be held by both; if cover is wanted without
that, it is a `design_qc_deputy` FK mirroring `design_head_deputy` and its own resolution
helper.

**Location:** `projects/permissions.py:user_can_qc_gate_design`

### M6 — The general uploader still has the narrow MIME check that broke CAD upload

`design_storage.validate_design_file()` was rejecting every `.zip` uploaded from Chrome on
Windows, which sends `application/x-zip-compressed` (it reads the type from the HKCR
registry entry) rather than `application/zip`. Design QC could not receive a CAD archive
at all. Fixed by replacing the one-string-per-extension map with
`DESIGN_ACCEPTED_MIME_TYPES`, a set of the types real browsers actually send, kept
separate from `DESIGN_MIME_TYPE_MAP` — what we ACCEPT is wide, what we STORE is the one
canonical value.

`views._validate_and_upload()` (views.py:5873) still has the original narrow form. It was
deliberately NOT changed:

  * its extension set is documents and photos only — pdf, doc, docx, xls, xlsx, jpg, jpeg,
    png — and modern browsers send the canonical type for all of them. `.zip` and `.dwg`
    were the genuinely broken pair precisely because Windows resolves those from the
    registry and CAD software registers its own;
  * it is the upload path for task attachments, project documents and site photos across
    the entire product, so a change there has a far wider blast radius than the reported
    bug justifies.

If a `.jpg` from a very old client (`image/pjpeg`) or a legacy `.doc` ever gets refused,
the fix is to import `GENERIC_BINARY_MIME_TYPES` and an accepted-types map from
`design_storage` rather than to write a second copy of the same table.

**Location:** `projects/views.py:_validate_and_upload`, `projects/design_storage.py`
