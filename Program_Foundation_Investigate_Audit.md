# Program Foundation — Investigate-Only Audit (pre-implementation)

**Status:** Investigate-only. NO code written. Awaiting Zuber sign-off before an implementation prompt is issued.
**Scope:** Confirm the three hard-stop items in §6 of the spec, verify the DO-NOT-DO list is achievable, and surface the decisions the spec explicitly delegates to this audit.

---

## (a) Current `Project` structure + `generate_project_id()` — CONFIRMED

**`Project` model:** [projects/models.py:5-107](projects/models.py#L5-L107).

- Currently has **no parent / upward FK of any kind.** Every relationship is either a *child* pointing **to** Project (`ProjectPhase`, `Task` via phase, `Milestone`, `ProjectDocument`, `BOQ`, `PaymentMilestone`, `Issue`, `Comment`, `DeliveryChallan`, `ActivityLog`, `PaymentRequest`, `DesignSubmission`, `ProjectFieldEditLog`, `NotificationLog.related_project`) or an FK **from** Project to `UserProfile`/`User` (`assigned_pm`, `assigned_design`, `coordinators` M2M, `created_by`). Adding `program = FK(Program, null=True, blank=True, related_name='sites')` would be the **first parent FK on Project** — see (c) for breakage analysis.
- `project_type` choices are `'Residential' / 'OPEX' / 'CAPEX'` ([models.py:8-12](projects/models.py#L8-L12)) — **title-case, and includes Residential.** The spec's `Program.program_type` is `opex_tender / capex_contract`. The type-mismatch validation (§4) needs an explicit mapping (`opex_tender ↔ 'OPEX'`, `capex_contract ↔ 'CAPEX'`), and a decision on whether a `Residential` project may ever link to a Program (recommend: never).
- `status` choices are title-case with 6 values: `Draft / Active / In Progress / Commissioned / On Hold / Cancelled` ([models.py:15-22](projects/models.py#L15-L22)). The spec's proposed `Program.status` (`draft/active/completed/on_hold`, lowercase) does **not** mirror these. **Decision needed:** match Project's casing/values or intentionally diverge.
- Soft-delete is `is_deleted` (bool, default False) + `deleted_at` ([models.py:80-81](projects/models.py#L80-L81)). There is **no custom manager** — the default `Project.objects` returns soft-deleted rows too; filtering by `is_deleted=False` is done per-query at the call sites. Replicate exactly on `Program` (do not add a filtering manager, or the uniqueness-across-soft-deleted rule in §5 silently breaks).

**`generate_project_id()`:** [projects/utils.py:7-43](projects/utils.py#L7-L43). This is the *fixed* implementation the spec wants replicated.

- Format: `HRP-{PREFIX}-{YEAR}-{NNN}` where PREFIX ∈ `{RES, OPX, CAP}`, zero-padded to 3 digits. Stored in `project_id` `CharField(max_length=20, unique=True, editable=False)`.
- Correctly derives the next number from the **highest existing numeric suffix**, not a row count ([utils.py:36-43](projects/utils.py#L36-L43)).
- Uses `select_for_update()` and is **required to run inside `transaction.atomic()`** — `Project.save()` wraps the first-save ID generation in `with transaction.atomic()` ([models.py:97-107](projects/models.py#L97-L107)). `Program.save()` must do the same or `select_for_update()` raises.
- Queries the **unfiltered** manager on purpose so soft-deleted rows still reserve their number ([utils.py:26-34](projects/utils.py#L26-L34)) — matches the §5 soft-delete requirement.

> ⚠️ **`program_id` format mismatch to resolve:** the spec's examples `PROG-OPEX-0001 / PROG-CAPEX-0001` have **no year segment** and a different token layout than `HRP-…-{YEAR}-{NNN}`. Replicate the *suffix-parsing algorithm* (that's the anti-collision fix), but the string template and whether to embed the year are an explicit decision. Also size the new `program_id` field for the chosen format (Project's is `max_length=20`).

---

## (b) `user_can_manage_project` call sites — CONFIRMED, model `user_can_manage_program` identically

**Canonical definition:** [projects/permissions.py:12-39](projects/permissions.py#L12-L39). Authority = unconditional `OR` of (assigned PM) and (a Project Coordinator on the project), evaluated PM-first. Companion helper `project_managers()` at [permissions.py:42-61](projects/permissions.py#L42-L61).

**Thin adapter:** `_pm_owns_project(request, project)` at [views.py:1664-1670](projects/views.py#L1664-L1670) — pure pass-through, no comparison logic of its own.

**Every live call site (all in `projects/views.py`, plus the import):**

| Location | Shape |
|---|---|
| [views.py:37](projects/views.py#L37) | import |
| [views.py:1670](projects/views.py#L1670) | inside `_pm_owns_project` adapter |
| [views.py:1686](projects/views.py#L1686) | `_user_can_complete_checklist_item` |
| [views.py:1831](projects/views.py#L1831), [2987](projects/views.py#L2987) | bare `if not user_can_manage_project(...)` |
| [views.py:4323](projects/views.py#L4323), [4326](projects/views.py#L4326) | role-gated + capability var |
| [views.py:5367](projects/views.py#L5367) | `role in ('PM','Project Coordinator') and user_can_manage_project(...)` |
| [views.py:4927](projects/views.py#L4927), [4984](projects/views.py#L4984), [5078](projects/views.py#L5078), [5118](projects/views.py#L5118), [5211](projects/views.py#L5211), [5382](projects/views.py#L5382), [5476](projects/views.py#L5476), [5574](projects/views.py#L5574), [5676](projects/views.py#L5676), [5715](projects/views.py#L5715), [5755](projects/views.py#L5755), [5891](projects/views.py#L5891), [5933](projects/views.py#L5933), [5994](projects/views.py#L5994), [6080](projects/views.py#L6080), [6367](projects/views.py#L6367) | canonical `if profile.role == 'PM' and not user_can_manage_project(request.user, project):` guard |

**Dominant pattern to mirror:** `profile.role == 'PM' and not user_can_manage_project(request.user, project)`. Build `user_can_manage_program(user, program)` in `permissions.py` next to the existing function, following the exact same `getattr(user,'profile',None)` guard + additive-OR structure. **No new ad-hoc pattern, no direct `assigned_pm`/`coordinators`/role-string comparison** — satisfies the §5 DO-NOT-DO. (Open design point for the *implementation* spec, not this audit: Program has no `assigned_pm`/`coordinators` of its own yet — decide whether program authority derives from "manages any child site" or from Program-level fields to be added.)

---

## (c) Does any code assume Project has zero parent relationships? — NO breakage found

A nullable, blank FK is purely additive at the DB layer. Checked every way it could still leak:

- **Forms:** `ProjectCreateForm` / `ProjectEditForm` / `PostActivationFieldEditForm` all use **explicit `fields = [...]` lists**, never `'__all__'` ([forms.py:242-255](projects/forms.py#L242-L255), [288-300](projects/forms.py#L288-L300), [348](projects/forms.py#L348)). New FK will not appear in any form unless deliberately added. ✅
- **Admin:** `ProjectAdmin` uses explicit `fieldsets` + `list_display` ([admin.py:30-62](projects/admin.py#L30-L62)); a new field only shows if added. Harmless if it does (nullable). ✅
- **No `fields='__all__'`, no `model_to_dict`, no `_meta.fields` iteration** anywhere in the app (grep clean; the only `.values()` hits are `dict.values()` at [models.py:976](projects/models.py#L976), [views.py:250](projects/views.py#L250), [989](projects/views.py#L989)). ✅
- **No full-Project serialization into a `<script>` block** — so the §5 autoescape/`json_script` gotcha is not triggered by the FK itself (still applies to the future Program aggregate widget). ✅
- **Instantiation** is keyword-only (`Project.objects.create(customer_name=…, …)` at [views.py:4774-4790](projects/views.py#L4774-L4790)); no positional constructor to break. ✅
- **`Meta.ordering = ['-created_at']`** and all child `related_name`s are unaffected. ✅
- **Next migration number: `0045`** (latest is [0044_gantt_settings.py](projects/migrations/0044_gantt_settings.py)).

**Conclusion:** adding `program` FK is safe. Every existing view for `program=null` projects renders unchanged (§4 "Unlinked Projects" / §Pre-flight #9 requirement is structurally satisfied by nullability).

---

## Decisions the spec routes to this audit — flagged for Zuber

1. **Rollup: compute-live vs. signal-synced counter — RECOMMEND COMPUTE-LIVE.**
   Precedent in this codebase is compute-live: `get_material_status()` ([models.py:922](projects/models.py#L922)) and the Gantt engine both compute from live state rather than denormalizing. The Program **detail** page is a single-program view (no N+1 risk — same as `get_material_status`'s stated safe usage). The Program **list** page must use a `.annotate()`/aggregate queryset to avoid N+1 across programs (mirror the "view-level annotated queryset" note at [models.py:967-983](projects/models.py#L967-L983)). This satisfies §4 "do not denormalize a counter that can silently drift."

2. **⚠️ Execution gate has no existing hook point for CAPEX — BIGGEST OPEN ITEM.**
   The only lifecycle-state transition today is `project_activate` ([views.py:1896-1958](projects/views.py#L1896-L1958)) — and it is **Residential-specific in effect**: it attaches the 53-task template (including the `Installation` phase) only when `project_type == 'Residential'` ([views.py:1934-1935](projects/views.py#L1934-L1935)); for OPEX/CAPEX it just stamps `activated_at` and tells the PM to "Add tasks manually" ([views.py:1955-1956](projects/views.py#L1955-L1956)). **There is no "installation/execution begins" event on a CAPEX project to hang the `loan_status == disbursed` gate on.** Before the gate in §Pre-flight #7 can be built, the implementation spec must **define what action constitutes "starting execution" on a CAPEX site** (e.g., activation of a CAPEX project under a Program, or first task-status move on an Installation-phase task once such tasks exist). This is a scope dependency, not just an implementation detail — surface to Zuber.

3. **Self-funding bypass (§4 / Pre-flight #8):** the gate must key off "financing actually involved," not merely `loan_status`. Since the fields feeding this (`financing_partner_name`, `financing_assistance_type`) are provisional/nullable, define the "no financing" predicate precisely (recommend: gate applies only when `financing_partner_name` is set AND `loan_status != not_applicable`) so self-funded CAPEX Programs are never blocked.

4. **Enum-casing consistency:** `Program.status` and `Program.program_type` casing/values vs. Project's title-case conventions and the `program_type ↔ project_type` validation mapping (see (a)). Pick one convention deliberately.

5. **`program_id` format + field size** — see (a) warning box.

---

## DO-NOT-DO list (§5) — all achievable as specified
- Permission helper: mirror `permissions.py` canonical pattern ✅ (see (b))
- No dashboard/list-view edits: none required by the foundation ✅
- No backfill: `program=null` default, forward-only ✅ (nullable FK)
- No bulk-site UI: out of scope, not touched ✅
- Storage-only financial fields (except the live loan gate): honored ✅
- ID generation: replicate suffix-parsing, not row count ✅ (see (a))
- Soft-delete-aware uniqueness: default manager already includes soft-deleted rows ✅ (see (a))
- Autoescape gotcha: not triggered by the FK; applies to the future aggregate widget only ✅

**No blockers found. Two items need a product decision before implementation: the CAPEX execution-start hook (#2) and the enum/`program_id`-format conventions (#4, #5).**

---

# v2 Addendum — audit of the revised spec (locked decisions + OPEX site-ID scheme)

The revised spec locks several of the decisions above (program_type reuses Project's `OPEX`/`CAPEX` vocabulary; status matches Project's title-case; Residential excluded; CAPEX loan/gate fully deferred — so §Decision #2 and #3 above are now out of scope here). It also adds two new audit obligations: **(d)** the Zoho webhook check, and a **second checkpoint** for the new OPEX site-ID scheme. Findings below.

## (d) Zoho webhook cannot create an OPEX project — CONFIRMED (defensive check passed)

`zoho_deal_closed_webhook` ([views.py:4691-4790](projects/views.py#L4691-L4790)) is the only automated creation path. `project_type='Residential'` is a **hardcoded literal** in the sole `Project.objects.create(...)` call ([views.py:4782](projects/views.py#L4782)). Nothing in the Zoho field-mapping block ([views.py:4743-4771](projects/views.py#L4743-L4771)) reads or maps a `project_type` from the payload — the deal fields consumed are name/phone/email/city/state/capacity/amount/PM/date only. **There is no code path by which a Zoho deal can produce `project_type='OPEX'`.** The business rule is enforced structurally, not by validation that could be bypassed. No change needed; matches spec §6(d). ✅

## Second checkpoint — OPEX site-ID scheme (`{short_tender_code}-{site_code}`)

This is genuinely new logic. Concrete findings and risks the implementation prompt must address:

1. **`project_id` is `max_length=20`** ([models.py:24](projects/models.py#L24)). `IPGCL26-S045` fits (12 chars), but a long `short_tender_code` + `site_code` can overflow. **Decide:** either bound the two inputs so `len(code) + 1 + len(site_code) ≤ 20`, or widen the column via migration. This is a hard constraint, not cosmetic — an over-length combined ID raises a DB error at save.

2. **The OPEX creation path cannot reuse the existing create flow as-is.** Both current create paths let `Project.save()` own ID assignment: `save()` calls `generate_project_id()` **only when `not self.project_id`** ([models.py:97-107](projects/models.py#L97-L107)); `project_create` does `form.save(commit=False)` then `project.save()` ([views.py:1724-1728](projects/views.py#L1724-L1728)) with no `program`/`site_code` in scope. For an OPEX site the view must **validate `short_tender_code`+`site_code`, compose the ID, and set `project.project_id` explicitly BEFORE `save()`** so the `generate_project_id()` branch is skipped entirely. This satisfies the spec's "do not force OPEX through the suffix-parser" rule — but it means new plumbing (a Program-scoped OPEX-site creation view/form that also captures `program` + `site_code`), **not** a tweak to `project_create`. Surface this as real scope.

3. **Immutability on Program rename is already structurally guaranteed — IF the ID is stored, not computed.** `project_id` is `editable=False` and `save()` never regenerates once set. A Program rename only touches Program fields. So spec §4/§5#8/§Pre-flight #10 are satisfied **provided** the OPEX `project_id` is persisted to the column at creation. **The one real risk to forbid:** do NOT render/derive an OPEX site's ID dynamically from `program.short_tender_code` at read time — that would make a rename retroactively change displayed IDs. Store once at creation.

4. **No collision with `HRP-` IDs.** Legacy/CAPEX/Residential IDs are 4-segment `HRP-{PREFIX}-{YEAR}-{NNN}`; OPEX tender IDs are 2-segment `{code}-{site}`. The suffix-parser filters on `project_id__startswith="HRP-OPX-{year}-"` ([utils.py:29-34](projects/utils.py#L29-L34)), so new OPEX IDs never pollute it, and vice-versa. **Recommend** normalization guards anyway: uppercase both inputs, restrict to `[A-Z0-9]` (no spaces/hyphens inside a token, which would blur the single delimiter), and explicitly reject a `short_tender_code` of `HRP`. Note: legacy standalone OPEX projects keep their old `HRP-OPX-…` IDs — **two ID formats will coexist for `project_type=OPEX`**; anything that parses OPEX IDs must tolerate both.

5. **Duplicate-`site_code` and `short_tender_code` validation must be explicit, soft-delete-aware, and pre-save.** A partial unique index can't cleanly express "unique per program, null elsewhere," so both checks (spec §4, §5, §6#8-9) must be explicit `.filter(...).exists()` queries returning a specific message — never a raw `IntegrityError`. Critically, both queries must **include soft-deleted rows** (use the default manager, no `is_deleted=False` filter), mirroring `generate_project_id`'s deliberate choice — otherwise a soft-deleted site's `site_code` could be reused and regenerate a colliding, globally-unique `project_id`. `short_tender_code` uniqueness across Programs has the same requirement (query soft-deleted Programs too — the documented "can't re-add" gotcha).

## Enforcement point for "OPEX is never standalone" (spec §4)

`project_create` ([views.py:1714](projects/views.py#L1714), `@role_required(['PM'])`) is the **only** manual path, and `ProjectCreateForm` includes `project_type` in its field list ([forms.py:249](projects/forms.py#L249)) with a Residential default ([views.py:1733](projects/views.py#L1733)) — so today a PM can create a standalone OPEX project here. Given finding #2 above, the cleanest enforcement is to **remove OPEX from this generic form's choices entirely** and route all new OPEX sites through the dedicated Program-scoped creation path (which is where `program`+`site_code`+ID composition must live anyway). The webhook path is already safe (see (d)). No third creation path exists. Enforce at the view/form layer, never as a DB `NOT NULL` on `program` (would break legacy `program=null` rows) — matches spec §4.

## Net for v2

- (a)/(b)/(c) from the base audit stand unchanged and are all satisfied.
- (d) verified — webhook is structurally OPEX-incapable. ✅
- OPEX site-ID scheme is safe to build but is **new code with three concrete constraints**: the 20-char `project_id` ceiling (#1), explicit pre-save set of `project_id` bypassing `generate_project_id()` (#2), and store-don't-compute for rename immutability (#3). None are blockers; all need to be written into the implementation prompt.
- Still-open product decision carried over: the rollup remains **compute-live** (recommended, unchanged). CAPEX execution-gate is now explicitly deferred out of this spec, closing the biggest prior open item for *this* prompt.
