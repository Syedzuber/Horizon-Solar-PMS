# Execution Module — Model and Conventions

**Commit to `docs/execution-model.md`.**

Read this file at the start of every execution-module session. It is the authority on vocabulary, structure and rules for this module. Where it conflicts with an older document, this file wins. Where it is silent, `PROJECT_CONTEXT.md` and `code commenting standard.txt` apply unchanged.

**Version 1.3 — 29 Aug 2026.** Phase 0 is complete, and this revision makes the file describe the codebase as it now stands rather than as 0.1 found it: §5 gains every structure phase 0 built, §6 separates the problems that were fixed from the ones that are still live, §8 closes the questions phase 0 answered, §11 marks B-1/B-2/B-5/B-7 fixed, and R-16 and R-17 are added. Sections 13, 14 and 15 — appended by prompts 0.3, 0.4 and 0.5 — are unchanged and remain the detailed record for `StatusTransition`, task templates and checklists.

*Version history.* v1.0 (23 Aug) → v1.1/v1.2 built by the prompt 0.1 verification session (25 claims confirmed, 11 corrected), then extended by the A-0.2 access audit and the 0.2a regression baseline. 0.3 appended §13 while deliberately holding the version string at 1.2, because later prompts hard-checked for it. Phase 0 has now closed, so the bump is safe to take. Claims marked ✔ were verified against source; claims added in v1.3 were read from the committed code, not from the prompts' own reports.

> **If the copy in `docs/` says "Last updated: 23 Aug 2026" it is v1.0 and has eleven errors in it. Replace it.**
> **If it says "Version 1.2" it predates the phase 0 close-out and describes four defects that are fixed, a checklist that is not versioned, and no `StatusTransition`. Replace it.**

---

## 1. Vocabulary — use these words, never synonyms

| Term | Means | Do not call it |
|---|---|---|
| **Program** | The tender (OPEX) or multi-site CAPEX contract. Residential never has one ✔ (enforced in `_validate_program_link()`). Groups and reports; owns no per-site workflow ✔. | Tender, in code |
| **Project** | The unit of work. Owns phases, tasks, BOQ, milestones. `Project.program` is its only parent FK ✔, `related_name='sites'` ✔. | Site, in code |
| **Site** | Informal word for a Project that has a Program. **Not a model** ✔. Same row as Project. | — |
| **SiteGroup** | A grouping of Projects **within one Program** — `SiteGroup.program` is a non-nullable FK ✔. From this module onward it carries `group_type`. | Lot, batch, cluster |
| **ProjectPhase / Task** | The existing execution structures. This module **extends** them. | — |
| **DesignAttempt** | One numbered pass at designing a site. Design module only. | — |

There is no `exec_task`, no `Lot`, and no `Site` model ✔. "Lot" is the Tenders team's word for a SiteGroup with `group_type = 'execution'`.

**Consequence of `SiteGroup.program` being non-nullable:** Residential projects can never be grouped, because Residential can never have a Program. Execution grouping applies to OPEX and CAPEX only. A cross-Program execution batch is impossible without a schema change. Do not assume otherwise; see Q-E1 in §8.

---

## 2. The four architecture decisions

### D-1 · SiteGroup serves two purposes

`group_type` ∈ `procurement | execution`. A Project may hold one active membership of each type simultaneously, because SCM's delivery batch and the PM's execution batch legitimately contain different sites.

**The lock is procurement-only.** A locked procurement group means a purchase order has gone out, and there is deliberately no unlock. An execution group has its own lifecycle — planning, active, closed — and stays re-plannable.

> **⚠ The schema half is DONE (prompt 1.1a, migration `0071_site_group_type`). The consumers are not (1.1b).**
>
> **This box previously said the fix was to alter the constraint to `(project, group_type)`. That was not implementable as written**, and `SITE_GROUP_AUDIT.md` F-1 is why: a `UniqueConstraint`'s `fields` must be **local columns**, and `group_type` lived only on `SiteGroup`. There is no way to reach it from `SiteGroupMembership` inside a constraint.
>
> **The shape actually built** — audit Shape A, approved 29 Aug: `group_type` is **denormalised onto `SiteGroupMembership` as well as `SiteGroup`**, copied from the group at insert by `SiteGroupMembership.save()`, and refused any later change by that same guard (and by `SiteGroup.save()`, so the source cannot drift away from its copies). The constraint is then written over the membership's **own** column and is named `uniq_active_site_group_membership_per_type`. This is the only shape in which the **database** enforces D-1 rather than a view. The membership's `group_type` is never a second source of truth and is never joined on — `StatusTransition.actor_role_code` is the same pattern.
>
> **1.1a changed no behaviour, and the caller problem is still open.** Live code depends on the *old* guarantee. `active_group_membership()` documents that its `.first()` is "picking the only row that can exist, not the first of several" ✔, and its caller uses that single result to compute `in_draft_group`, the gate admitting a released design to a change request. That sentence is **true today only because no execution membership exists yet**. **Prompt 1.1b must narrow every caller, and the design change-request gate must explicitly ask for `procurement`** — until it does, creating the first execution membership makes that gate order-dependent.

### D-2 · `PaymentRequest` is live and stays

SCM raises payment requests to finance against it. The payment itself happens outside PMS.

Corrections from verification: `STATUS_CHOICES` is **`pending | confirmed`** ✔ — there is no `due` and no `done`. The model's docstring states **"No edit/cancel by design"** ✔ and `invoice_document` is mandatory at creation ✔. Any extension adding a verification step must not assume an editable row.

Site-team **work verification** is new. It **extends** `PaymentRequest`; it does not replace it, and no second invoice model may be created.

### D-3 · Extend, do not run parallel

Execution extends `Task` and `ProjectPhase`. Creating a parallel task model is forbidden. The codebase already carries more than one representation of delivery state; this module must not add another. See §6 for what is and is not actually divergent.

### D-4 · Access is scoped by assignment, with two unrestricted roles

Decided 24 Aug 2026, superseding the looser v1.0 wording.

| Role | Scope |
|---|---|
| **CEO** | Unrestricted — sees everything |
| **Admin / System Admin** | Unrestricted — sees everything |
| **PM** | Projects they manage ✔ (already scoped) |
| **Project Coordinator** | Projects they coordinate ✔ (already scoped) |
| **Site Engineer** | Projects where they hold a Task ✔ (already scoped, via `phases__tasks__assigned_to`) |
| **Design** | `assigned_design` or task relation ✔ (already scoped) |
| **Finance** | Projects and Programs where they hold an assignment — **new**, currently portfolio-wide |
| **SCM** | Projects and Programs where they hold an assignment — **new**, currently portfolio-wide |
| **BD** | **Unresolved** — currently portfolio-wide by a written settled decision. See Q-E2 |
| **Design Head** | **Unresolved** — an authority flag, not a project role. See Q-E2 |
| **QA/QC, HSE** | Projects where they hold the relevant assignment — new roles, see §4 |

`Task.ROLE_CHOICES` already includes Finance and SCM ✔, so task-based assignment is the existing mechanism for both. **Program-level assignment does not exist** — `Program` has no `assigned_pm`, no manager FK, no M2M ✔, only `created_by`. A `ProgramAssignment` model is therefore required before Finance/SCM program scoping can work, and before §4's claim that "the PM owns a whole Program" has any representation at all.

Access isolation ships before any site engineer or warehouse keeper receives a login.

---

## 3. Standing rules

**R-1 · No schema change inside a feature.** Do not create, alter or drop a table as part of implementing a feature. If a feature needs a schema change, stop and output the proposed migration and its rationale for approval before writing any other code. One prompt, one reviewed migration.

**R-2 · Every status change writes a transition row.** Once `StatusTransition` exists (prompt 0.3), every status change goes through `record_transition()`, inside the same transaction as the change itself. Unlike `log_activity()` — which catches bare `Exception` ✔ and can therefore lose rows silently — this helper must **not** swallow exceptions.

**R-3 · `ActivityLog` and `StatusTransition` are different things.** `ActivityLog` is the human-readable activity feed and keeps its existing behaviour and helper. `StatusTransition` is the machine-readable state ledger: from-status, to-status, actor, the actor's role at the time, reason, remark, timestamp. Never merge them; never replace one with the other.

**R-4 · The transition log is append-only.** Never `UPDATE` and never `DELETE` a `StatusTransition` row. A correction is a new row. Any `current_status` column is a cache, written by the same function that writes the transition, in the same transaction, and by nothing else.

**R-5 · One status field answers one question.** No status value may contain "and", "but", "pending", or a percentage. Work state, hold reason, progress percentage, QC state and HSE state are **separate fields**. If a proposed status name needs a conjunction, it is two fields pretending to be one.

**R-6 · Every status needs a defined exit.** For each status value introduced, state who can move it, to what, and under what condition.

**R-7 · Templates are versioned and immutable once active.** Editing an active template version creates version+1. It never modifies rows in an existing active version.

**R-8 · Instances snapshot the text they were filled against.** A checklist completion stores the item's wording as it stood when it was answered. `BOQRevision.snapshot` ✔ is the existing precedent in this codebase for exactly this pattern.

**R-9 · Remarks are mandatory.** `remark` is `NOT NULL` on every execution status transition, enforced at the database level rather than in a form.

**R-10 · Statuses stay hardcoded constants.** Do not introduce a `workflow_status` lookup table or any rule-builder screen. The codebase has 64 migrations ✔ of module-level status constants; match that convention.

**R-11 · A screen ships with its navigation entry.** Any prompt that adds a user-facing screen adds the way to reach it, in the same prompt.

**R-12 · Never fix an unrelated finding in the same session.** Record it in `EXECUTION_MODULE_DEFERRED.md` and continue.

**R-13 · Permissions only through helpers.** Never compare role strings inline. Extend `permissions.py`.

**R-14 · Idempotency keys go in now.** Any table a site engineer writes to in the field carries a nullable `client_uuid` with a unique constraint, so a queued offline submission can be replayed safely later.

**R-15 · Prefer a boolean flag over a new `ROLE_CHOICES` value.** `'Design Head'` was added in migration 0048 and deliberately removed in 0053 ✔, because a new role value costs its holder every `Task.assigned_role` match, BOQ write authority, the System Admin edit path, and portfolio-wide task visibility. The pattern that replaced it — `is_design_head`, then `is_design_qc` — sits a capability flag on a user who already holds a real role and costs none of that. Follow it.

**R-16 · Resolve a Project through `_active_project()`.** *(Established by prompt 0.2c, 28 Aug 2026.)* Every view that turns a `project_id` from a URL into a `Project` calls `_active_project()` — never a hand-written `get_object_or_404(Project, …)`. The helper is one line of query, and the reason it must be the only path is that a soft-deleted project is otherwise indistinguishable from a live one: `project_delete` sets `is_deleted=True` and leaves `status` alone, so a deleted Draft still satisfies every status precondition in the codebase, and with no custom managers (§6) nothing filters it for you. `_opex_site()` in `design_views.py` is the precedent this copies, and it is exactly why the design module carried none of the soft-delete findings the A-0.2 audit raised against `views.py`.

> **What the code actually looks like, stated honestly.** Four resolutions in `views.py` and one in `design_views.py` still call `get_object_or_404(Project, …)` directly — `project_delete`, `task_assign_design_head`, `admin_assign_pm`, `subadmin_projects`, and `_opex_site`. **Every one of them already passes `is_deleted=False` explicitly, which is why 0.2c left them alone**; two of them also key on `pk` rather than `project_id`, which `_active_project()` does not accept. So the *safety* property holds everywhere, and the *single path* property does not quite. Do not read a bare `get_object_or_404(Project, …)` as automatically a bug — read it as needing the filter checked by hand, which is the whole cost this rule exists to remove. New code takes the helper.
>
> The rule stops at `Project`. `Issue`, `DeliveryChallan`, `BOQ` and relation traversals such as `phase__project__is_deleted` each still need their own explicit filter — `ACCESS_ISOLATION_AUDIT.md` G.5 notes that `Issue` has nothing of its own to filter *on*, so it must be reached through its project. Finding 14 is the record.

**R-17 · Versioned templates follow the `TaskTemplate` pattern.** *(Established by prompt 0.4, extended by 0.5, 28–29 Aug 2026.)* Any template family that instances are built from is versioned the same way, and there is exactly one way:

- an integer `version_no` on the row, and a `code` that is the identity **across** versions — when v2 rewords a label, `code` is what says it is still the same row;
- a `status` of `draft | active | archived`, with a **partial unique constraint** making "at most one active version per code" true at the database rather than by convention (`uniq_active_task_template_per_code`, `uniq_active_checklist_per_code`);
- content editable **only** while `status='draft'`, enforced by `save()`/`delete()` overrides raising `TemplateVersionLocked` through the **shared** `_require_draft_template()`. Archived is frozen as hard as active, because an archived version is the record of what last month's projects were built from;
- an `activate()` that promotes the draft and archives the outgoing version **in one transaction**;
- instances take **copies**, never a live FK read. Any FK back to the template row is provenance only (`Task.template_task`, `ChecklistItemCompletion.item`) and nothing may resolve behaviour through it — the moment something does, B-10 reopens.

**Adding is content too.** A question added to a live checklist retroactively makes every site that already completed it incomplete, so the guard blocks adds as well as edits.

**Two families now share one guard and one exception. Do not write a third design.** And state the limit rather than implying it: `QuerySet.update()`, `QuerySet.delete()` and an FK cascade from deleting the parent all operate in SQL and bypass the overrides entirely — the same half-measure `StatusTransition` documents in §13, and honest about itself for the same reason.

---

## 4. Roles and ownership

`UserProfile.ROLE_CHOICES` contains **ten** values ✔: Admin, System Admin, PM, Project Coordinator, Site Engineer, Design, Finance, SCM, CEO, BD. CEO, Finance and SCM are first-class roles with their own dashboards, not incidental references.

The stored BD value is **`'BD'`** ✔. `'BD / Sales'` is a `Task.assigned_role` value, not a `UserProfile.role` value — do not confuse them.

`Task.ROLE_CHOICES` has six values ✔ and lacks Admin, System Admin, Project Coordinator and CEO. **A Project Coordinator therefore matches no `assigned_role` and cannot be assigned a task today** — relevant to prompts 1.2 and 2.1.

`_PROFILE_TO_TASK_ROLE` is **one module-level constant** at `views.py:413` — `{'BD': 'BD / Sales'}`, applied as `.get(role, role)` at seven call sites. **Corrected 29 Aug by 0.6:** it used to be seven byte-identical *local* copies, and 0.2b consolidated them ahead of prompt 1.2's schedule. The two vocabularies differ in exactly one value; they differ in *membership* more than in naming.

**Its inverse was not consolidated.** `_TASK_TO_PROFILE_ROLE = {'BD / Sales': 'BD'}` is still declared locally at `views.py:4252` and `views.py:7372`. 0.2b's scope named three duplications and this was not one — recorded as A3 in `EXECUTION_MODULE_DEFERRED.md`, and it is the remaining half of `DESIGN_MODULE_DEFERRED.md`'s K5.

### New capabilities in this module — flags, not roles

Per R-15, and decided 24 Aug 2026:

| Capability | Representation | Why |
|---|---|---|
| **QA/QC** | `is_qaqc` boolean on `UserProfile` | Records a verdict on a task someone else completed, raises punch points, verifies resolution. That is authority over an action, not a task assignment. No `ROLE_CHOICES` value, no `Task.ROLE_CHOICES` value. Mirrors `is_design_qc`. |
| **HSE** | `is_hse` boolean on `UserProfile` | Signs one mobilisation clearance per site — see §9. One signature per site, not per task. |
| **Warehouse Keeper** | `is_warehouse_keeper` boolean on `UserProfile` | **A distinct person logs in and records receipt.** Needs a warehouse-scoped view and appears in phase 1, not deferred. `DeliveryChallan.grn_confirmed_by` already exists ✔ and is the field they fill. |

**No new `UserProfile.ROLE_CHOICES` values are added by this module.**

### Ownership chain

- The **PM** owns a whole Program — **but this has no representation today** ✔ and requires `ProgramAssignment` (see D-4).
- The PM cuts a Program into execution SiteGroups and assigns a **Project Coordinator** to each. A coordinator handles many sites.
- Every site has a **Site Engineer** who owns execution on the ground.

Assignments are **effective-dated rows**, never a foreign key on `Project`. There is precedent: `assigned_site_engineer` existed as an FK and was removed in migration `0037_remove_assigned_site_engineer.py` ✔. `assigned_pm` and `coordinators` keep their existing additive behaviour ✔ and are not replaced.

---

## 5. What already exists — extend these, do not duplicate

| Need | Existing model | Verified note |
|---|---|---|
| Execution phases and tasks | `ProjectPhase`, `Task` | **52** tasks across 9 phases for Residential ✔ — asserted at `utils.py:908` inside the atomic block, so a mismatch rolls back activation. 44 internal / 8 external. **OPEX/CAPEX have no template at all** ✔ |
| Task template | `TaskTemplate`, `TaskTemplatePhase`, `TaskTemplateTask` | **Built by 0.4** (migrations `0066`, `0067`). Versioned data — `RESIDENTIAL` v1 is 9 phases / 52 tasks. `attach_residential_template()` reads the active version at activation. See §14 |
| Task durations (superseded) | `TaskDurationTemplate` | `unique_together = ('project_type', 'task_name')` ✔ — **`phase_name` is stored and displayed but never matched on**. No version, no snapshot ✔. **Superseded by 0.4:** nothing reads it at runtime any more, its two editor screens are read-only and render the active `TaskTemplate`, and the table is deliberately not dropped. Do not repoint anything back at it |
| Checklists | `Checklist`, `ChecklistItem`, `ChecklistTaskLink`, `ChecklistItemCompletion` | Linked by `(task_name, project_type)` string match ✔ — still, until 2.4. **Corrected 29 Aug by 0.5:** the completion now carries `item_text_snapshot`, and the item FK is nullable `SET_NULL`, not `CASCADE`. `Checklist` is versioned (`code`/`version_no`/`status`) and its content is immutable once active. See §15 |
| Punch points | `Issue` | Carries severity, status, raised_by, assigned_to, due_date, resolution_note ✔; links to both `Task` and `DeliveryChallan`, both nullable `SET_NULL` ✔ |
| Delivery and GRN | `DeliveryChallan`, `DCLineItem` | Project-scoped; no warehouse, movement type or serials ✔. **`DCLineItem` has no FK to `BOQItem` or `BOQItemMaster`** ✔ — only `boq_category` (CharField) and free-text `item_description`. Its `CATEGORY_CHOICES` has four values against `BOQItem`'s five ✔; anything in `Other` is unreconcilable by construction |
| Vendor invoices | `PaymentRequest` | Live. `pending \| confirmed` ✔. "No edit/cancel by design" ✔ |
| Item catalogue | `BOQItemMaster` | What the BOQ is built from |
| BOQ | `BOQ`, `BOQItem`, `BOQRevision` | `BOQRevision.snapshot` is a `JSONField` ✔ — existing precedent for R-8 |
| Documents | `ProjectDocument`, `TaskAttachment`, `DesignFile` | Design bucket isolated by a hard guard ✔. `DESIGN_FILE_KIND_CHOICES` has three current kinds plus two legacy ✔, and **no per-file approval field exists anywhere** |
| Notifications | `send_notification()` | Single chokepoint with master switch then per-user preference ✔ — **with two documented bypasses**: `send_raw_email()` skips the master switch, `send_aggregate_email()` skips per-recipient preference ✔ |
| Activity feed | `ActivityLog` + `log_activity()` | Keeps its role — see R-3 |
| State ledger | `StatusTransition` + `utils.record_transition()` | **Built by 0.3** (migration `0065`). Six subject types instrumented; the helper raises rather than swallowing (R-2), and append-only is enforced by `save()`/`delete()` overrides (R-4). See §13 for exactly what is and is not covered |
| Design workflow | `DesignAssignment`, `DesignAttempt`, `ArkaSubmission`, `DesignFile`, `DesignChangeRequest` | **OPEX ONLY — see below.** `DESIGN_RELEASED` has exactly one exit ✔; a PM approval gate does not exist ✔ |

### Helpers and constants phase 0 introduced — call these, do not re-derive them

Every one of these exists because the same act was previously implemented two or more
times and the copies drifted. Re-inlining any of them recreates a defect that has already
been paid for once.

| Helper | Where | Introduced | What it is for |
|---|---|---|---|
| `_active_project(project_id, select_related=None)` | `views.py:2409` | **0.2c** | **The single Project resolution path.** Returns a live project or 404s. `project_delete` sets `is_deleted=True` and leaves `status` untouched, so a deleted project still satisfies every status-based precondition in the codebase; with no custom managers (§6) nothing applies the filter for us. 43 call sites. See R-16 |
| `record_transition(subject, to_status, …)` | `utils.py:246` | **0.3** | Writes one `StatusTransition`. Must be called inside the caller's own `transaction.atomic()`. **Raises; never swallows** — that is the whole difference from `log_activity()` (R-2, R-3) |
| `_boq_snapshot(boq)` | `views.py:4678` | pre-existing, made single by **0.2b** | Builds a JSON-safe `BOQRevision.snapshot`, coercing `Decimal`. `boq_submit` used to build its own from a raw `.values()` and 500 on every call (B-7) |
| `_apply_boq_acknowledgement(boq, profile, request)` | `views.py:4763` | **0.2b** | Status write + `ActivityLog` + notification + transition row for an SCM acknowledgement, shared by the inline `acknowledge_scm` branch and the standalone `boq_acknowledge` endpoint (B-5). **Gates and status preconditions stay with the callers.** The `BOQRevision` snapshot also stays with the inline caller — that asymmetry is open as **B-8** in `EXECUTION_MODULE_DEFERRED.md` |
| `_FINANCE_TASK_TO_MILESTONE` / `_MILESTONE_TO_FINANCE_TASK` | `views.py:400` | **0.2b** | The Finance-task ↔ `PaymentMilestone` map, one definition for the module. Four copies previously drifted, three still naming `'Finance Confirmation'`, a task deleted from the template (B-2). **The reverse map is derived**, so the two directions cannot diverge |
| `_PROFILE_TO_TASK_ROLE` | `views.py:413` | **0.2b** | `UserProfile.role` → `Task.assigned_role` normalisation. See §4 |
| `manageable_projects_q(profile, prefix='')` | `permissions.py` | **0.2** | The **queryset form** of `user_can_manage_project()`, for list surfaces that cannot load the portfolio and filter in Python. The two are pinned to each other by a stated invariant: change one, change the other in the same edit. Callers must `.distinct()` — the coordinators leg traverses an M2M |
| `_require_draft_template()` / `TemplateVersionLocked` | `models.py` | **0.4**, shared by **0.5** | The R-7 immutability guard. `TaskTemplatePhase`/`TaskTemplateTask` and `ChecklistItem` all raise through it. **Two versioned template families, one guard** — do not write a third |

### ⚠ The design module is OPEX-only

Every design endpoint resolves its site through `_opex_site()`, which raises `Http404` unless `project_type == 'OPEX'` ✔. **Residential and CAPEX cannot enter the design workflow at all** — no allocation, no attempts, no QC gates, no `DESIGN_RELEASED`.

Residential "design" is six template tasks with `assigned_role='Design'`, moved through the ordinary status ladder, plus the BOQ authoring path. `Design Approval by Internal Team` and `Design Approval by Customer` are PM-role tasks with no artifact and no gate behind them.

**Decided 25 Aug: phase 3 of the build plan targets OPEX/tender only.** Residential keeps its six design tasks unchanged. Do not port the design module to a second project type.

`DesignSubmission` has a model and two read views and **no write path at all** ✔ — one URL, no create or update view. Recorded independently as A4 in `DESIGN_MODULE_DEFERRED.md`. Treat it as dead; do not confuse it with phase 3's design package.

---

## 6. Known problems — stated accurately

**Read the fixed list too.** Nothing here is deleted once it is closed, because a problem
that vanishes from the record gets rediscovered, re-audited and re-reported by the next
session that trips over its scar tissue. If an entry is struck through, the code has moved
and the entry tells you which prompt moved it.

### Still open

- **Delivery state is deliberately duplicated on the CEO dashboard, not the SCM one.** `dashboard_scm` reads `DeliveryChallan`/`DCLineItem` directly ✔ and reflects actual receipt state. The task-derived proxy is on the **CEO** dashboard ✔ (`_get_ceo_dashboard_context`, `views.py:1873`), and the code carries an explicit instruction: *"Do not 'improve' it by cross-checking against the SCM models — the two will disagree, and the disagreement is not a bug in either."* The reason given is coverage: production holds 1 challan and 2 BOQ rows across 28 active projects, so a challan-based card would be blank on 27 of 28. **This is a considered trade, not a defect.** It may deserve revisiting once real execution data exists — but only by decision, never by a session deciding to tidy it. **Written up in full, with the revisit condition, in [`docs/delivery-state-authority.md`](delivery-state-authority.md) — read that before touching either surface.** *(Correction 29 Aug by 0.6: the challan-backed read lives in `_build_delivery_lookup()`, which serves **both** `dashboard_pm` and `dashboard_scm`, not `dashboard_scm` alone. Three surfaces, two sources.)*
- **The "BOQ is finished" signals are separate on purpose.** `project_boq_is_design_locked()` and `project_boq_is_group_locked()` each carry a written rationale for being distinct predicates answering different questions — authority over a user versus state of a site ✔. Do not collapse them without an explicit decision.
- **`log_activity()` catches bare `Exception`** ✔ (`models.py:1453`) and can silently lose rows. Still true, and deliberately so — R-3 says the feed and the ledger are allowed to fail differently. Do **not** copy that pattern into `record_transition()`, and do not "harmonise" the two.
- **Soft delete has no custom managers** ✔ — still zero `objects =` or `models.Manager` in `models.py`. Every queryset must filter `is_deleted=False` itself. 0.2c made the *view-layer* Project resolution safe (R-16); it did not and could not fix relation traversals like `phase__project__is_deleted`, aggregate queries, or any model other than `Project`.
- **Access scoping is closed for the endpoint surface and open for the role policy.** 0.2 fixed the endpoints; what remains is a *policy* gap, not a code gap — Finance, SCM and BD are still portfolio-wide because there is nothing in the database to scope them on. See D-4, the 25 Aug decisions in §12, and Q-E3.
- **Tests can be run.** `solarpms/test_settings.py` exists and disables migrations so the schema builds from model state without `CREATE DATABASE` privilege: `python manage.py test projects --settings=solarpms.test_settings`. `SESSION_T_TEST_UNBLOCK.md` records the suite running green through this path. **There is no excuse for an unverified claim of "tests pass".**

### Fixed by phase 0 — kept so they are not rediscovered

- ~~**No general transition history.**~~ **FIXED 28 Aug 2026 by prompt 0.3.** There were only ad-hoc terminal stamps — `Task.completed_at`, `Task.blocked_since`, `Issue.resolved_at`/`closed_at`, several on `DesignAttempt` ✔ — with no from-status, no actor role and no reason. `StatusTransition` now records all of it for six subject types. **The general case is fixed; the coverage is not universal** — §13 is the authority on where the ledger stops, and a missing row still means "not instrumented" for `DesignAssignment`, `DesignAttempt`, `PaymentRequest`, `SiteGroup`, `TaskTemplate` and `Checklist`.
- ~~**Checklist history is rewritable and deletable.**~~ **FIXED 29 Aug 2026 by prompt 0.5 — see §15.** The completion snapshots the text it answered, and the item FK is `SET_NULL`. What the fix cannot do is recover wordings edited before 0.5 shipped; the backfill records the label as it stands today.
- ~~**Eighteen detail endpoints carry no object-level check at all, and eight Issue endpoints let any authenticated user write to any project.**~~ **FIXED 28 Aug 2026 by prompt 0.2.** Routed through **`user_can_view_project()`**, for writes as well as reads. **Note the divergence from the audit:** `ACCESS_ISOLATION_AUDIT.md`'s proposed scope named "a new `user_can_act_on_project()`" for the write half; **that helper was never created and does not exist in `permissions.py`.** The audit's own Step 1 also observes that visibility and write authority coincide under today's policy, so the one helper is behaviourally sufficient — but a future session looking for `user_can_act_on_project()` will not find it, and the day the two questions need different answers, this is the seam. Pinned by `tests_access_isolation.py`.
- ~~**`role_required()` treated a profile-less user as `'Admin'`, and the navigation helpers sent them to `/dashboard/admin/`.**~~ **FIXED 28 Aug 2026 by prompt 0.2.** Both now deny; `role_required()` returns 403 rather than redirecting. **This is the one phase 0 change with a deployment consequence** — see the operational items in `PHASE_0_COMPLETION.md`.
- ~~**Thirty-plus write paths could mutate or resurrect a soft-deleted project.**~~ **FIXED 28 Aug 2026 by prompt 0.2c** via `_active_project()` (R-16). Pinned by `tests_soft_delete.py`.
- ~~**Four business acts implemented twice, with only one copy kept current.**~~ **FIXED 28 Aug 2026 by prompt 0.2b** — B-1, B-2, B-5 and B-7 in §11. This was the pattern 0.2a called out as the one that matters more than any single item.
- ~~**Templates and checklists were mutable in place, so editing one silently rewrote history that had already been recorded against it.**~~ **FIXED by 0.4 and 0.5** — R-7, enforced by a shared guard. See §14 and §15.

---

## 7. Scope boundaries — not being built

Vendor portal logins · offline mode · stock balances or valuation · purchase orders · vendor work orders and rate contracts · HSE incidents, near-miss, toolbox talks and PPE registers · retention, advances and debit notes · mobile app · splitting `views.py`.

If a prompt appears to require one of these, **stop and ask**. Do not build a small version of it.

---

## 8. Open questions — do not answer these by guessing

| Ref | Question | Blocks |
|---|---|---|
| ~~Q-E1~~ | ~~Residential sites cannot join a SiteGroup (`program` is non-nullable). Do Residential projects need execution grouping at all?~~ **CLOSED 29 Aug 2026 by the product owner, ahead of prompt 1.1a — the answer is NO.** PMs do not batch residential installs by crew or by area; a residential install is scheduled as one site, by itself. There is therefore nothing for an execution group to hold. **`SiteGroup.program` stays non-nullable**, and Residential keeps the structural exclusion `RESIDENTIAL_BASELINE.md` §3.3 describes — Residential can never have a Program, so it can never be grouped, and that is now a decision rather than an accident of the schema. Reopening it means making `program` nullable, which is a migration and a fresh answer to "what does a group of residential sites even mean". | — |
| ~~Q-E2~~ | ~~BD and Design Head scoping — confirm both.~~ **CLOSED 25 Aug 2026, and the closure held through 0.2.** Both stay portfolio-wide, because **neither has a per-user term anywhere in the database to scope on**: `ACCESS_ISOLATION_AUDIT.md` F.1 found no BD field on `Project` at all, and F.2 found Design Head to be a capability flag (`is_design_head`) that grants visibility and authority through two separate mechanisms. 0.2 left both unchanged as decided. **Not "confirmed correct" — confirmed *unscopeable today***; the question reopens when an assignment table exists. | — |
| Q-E3 | Do Finance and SCM get Program-level assignment as well as task-level, and who maintains it? **Still open, and now the gating question for the whole D-4 policy.** 0.2 deferred the narrowing on the 25 Aug decision; `ProgramAssignment` was designed in the audit (Task E) and **was not built** — no such model exists. Phase 0 answered "not yet", not "what". | 1.2 |
| B-05 | Do execution groups carry their own schedule and milestones, or are they only a grouping? **Still open, and it never blocked 1.1 — amended 29 Aug 2026.** It was listed against 1.1 as though the schema could not be written without the answer. It could: execution scheduling is **additive** — whatever the answer, it arrives as new columns or a new table hanging off `SiteGroup`, and neither shape changes `group_type` or the exclusivity constraint 1.1a shipped. Re-pointed at the prompt that actually builds it — **which has no number yet**, because no prompt in the plan builds execution scheduling. Numbering it here would be inventing a roadmap entry; it blocks that work whenever it is scheduled, and nothing before it. | execution scheduling (unscheduled) |
| B-06 | Does a PM rejection of a design package open a design attempt, and under which reason? | 3.2 |
| B-07 | Who may mark a drawing not-applicable — designer, or Design Head at review? | 3.1 |
| B-08 | Can a site override a template task dependency? | 1.4 |
| B-09 | Final phase and task list for the execution template (Residential is 52 tasks / 9 phases; OPEX has none). **0.4 made an OPEX template possible; it did not make one.** | 1.3, 2.4 |
| ~~B-10~~ | ~~What happens to an in-flight project when its template is upgraded?~~ **CLOSED 28 Aug 2026 by prompt 0.4 — see §14.** The answer is *nothing*, and it is structural rather than a matter of policy: `Task.task_name`, `assigned_role`, `task_type`, `duration_days` and `is_payment_milestone` are **copies taken at `bulk_create`**, not reads through a foreign key, so an in-flight project holds its own rows and cannot be reached by a later version. Publishing v2 changes what the *next* activation produces and nothing else. `tests_task_template.InFlightProjectIsolationTests` proves it, and guards the one thing that would reopen the question — a code path resolving a task's name, role or duration *through* `Task.template_task`, which is provenance only. | — |
| B-12 | Who may waive a punch point, and does it need senior approval? | 2.3 |
| B-14 | How many warehouses, and how are keepers assigned to them? **Now needed in phase 1** — a keeper logs in | 1.2, 4.1 |
| B-15 | Net metering: hard block or warning, which project types, who owns it? | 5.1 |
| B-16 | Does a vendor work order or rate contract exist to validate verified amounts against? | 5.2 |
| B-17 | Retention, advances and debit notes — in PMS or in accounts? | 5.2 |
| B-18 | `DCLineItem` has no join key to `BOQItem`. Add the FK, or accept string matching? | 4.1, 4.3 |
| **B-19** | **Who actually confirms a GRN — the engineer holding a task on that site, or whoever is standing at the warehouse when the lorry arrives?** 0.2 scoped `confirm_grn` to task-holders (`user_can_view_project()`, whose Site Engineer branch is exactly "holds a task on this project"), which is the tighter of the two readings and the one the audit's finding 1 demanded. If receipt is in practice recorded by whoever is present, that is a **process** answer this code now refuses, and it must be answered before a warehouse keeper is given a login. **This is for the product owner, not a session.** | 4.1 |
| **B-20** | **Does a profile-less superuser exist on production?** 0.2 closed the fail-open that treated a user with no `UserProfile` as `'Admin'` — it admitted them to all 33 `@role_required(['Admin'])` screens while every `permissions.py` helper correctly refused them. **If an installation administers itself through a `createsuperuser` account with no `UserProfile`, that account is now locked out.** The fix is to give it a `UserProfile` with `role='Admin'`, **never** to restore the fallback. **Operational, not a session's to decide.** | deployment |

---

## 9. Business rules confirmed with the Tenders team

**Gates — target state, not current state.** None of these is implemented today: nothing consults a checklist before allowing a task to be marked `Done` (defect B-6). Installation checklist blocking · QA/QC blocking at task completion · **HSE clearance signed once per site at mobilisation** — decided 24 Aug 2026. It is not a per-task gate: on a thirty-site lot that would be ~1,560 signatures and would be rubber-stamped within a fortnight. The clearance blocks the site entering execution, not each individual task.

**Sign-off.** Two-step: site engineer submits, PM or QA approves. Remarks required at every step.

**Design handover.** Eleven drawing types uploaded individually, approved as one package. Design Head and PM each tick per-file boxes, then mark the package complete or incomplete. PM rejection returns the package to the **Design Head**, who reassigns to a designer — never straight to the designer. Note that no per-file approval field exists today; phase 3 creates it.

**As-built.** Designer uploads; the task stays open in their bucket until submitted; submission is blocked until execution is complete and the site is ready for HOTO; it blocks HOTO. The PM may override, recorded as an approval with `is_override` and a mandatory reason.

**Material.** PMS verifies transitions of material; it never holds stock. Serial tracking for modules, inverters and meters; quantity tracking for MMS and BOS. Deliveries reconcile against the site BOQ as a computed view, never a stored balance — **subject to B-18, since no join key exists today**. Direct-to-site handover to a vendor needs the vendor's acknowledgement, recorded by the engineer as proxy in phase 1. Warehouse receipt is recorded by a warehouse keeper who logs in.

**Statutory.** CEIG applies to OPEX and CAPEX only, never Residential, and warns rather than blocks. The PM owns statutory approvals and coordinates with the customer. Net-meter *installation* is an execution task; net-metering *approval* is a statutory record. They are separate objects and must not share a status.

**Vendor.** Two flows differ only in ordering — invoice first then site verification, or site verification first then invoice. Both end at "released to finance".

---

## 10. Decision log — *superseded, see §12*

> **Two decision logs exist in this file and §12 is the authoritative one.** This table is
> the 24 Aug snapshot; §12 is a strict superset and carries every decision from 25 Aug
> onwards. Kept rather than deleted only because prompts and reports cite "§10". **Add new
> decisions to §12 and nowhere else.**

| Date | Decision |
|---|---|
| 23 Aug | D-1 SiteGroup carries `group_type`; lock stays procurement-only |
| 23 Aug | D-2 `PaymentRequest` is extended, never replaced |
| 23 Aug | D-3 Extend `Task`/`ProjectPhase`; no parallel task model |
| 24 Aug | D-4 restated: CEO and Admin unrestricted; Finance and SCM scoped by task and Program assignment; per-site roles scoped by assignment |
| 24 Aug | D-1 confirmed to require altering `uniq_active_site_group_membership` to `(project, group_type)` plus a full caller audit, in prompt 1.1 |
| 24 Aug | QA/QC, HSE and Warehouse Keeper are boolean flags, not `ROLE_CHOICES` values (R-15) |
| 24 Aug | Warehouse keeper is a real logged-in user in phase 1 |
| 24 Aug | HSE clearance is once per site at mobilisation, not per task |
| 24 Aug | Prompt 0.6 rescoped to documentation only — the delivery/task duplication is a deliberate coverage trade and stays |

---

## 11. Known defects — found by the 0.2a baseline session; four fixed, three scheduled

Found by 0.2a, read from source. None is one of the fifteen findings in `ACCESS_ISOLATION_AUDIT.md`. **Fixed entries are struck, not removed** — a defect deleted from the record is a defect the next session rediscovers.

| # | Status | Where | What |
|---|---|---|---|
| ~~**B-1**~~ | **FIXED by 0.2b** | `views.py:2752` | ~~Activation message says "53 tasks created". The code creates and asserts **52**.~~ The message now counts the rows actually created rather than restating a template size, so it cannot drift again when the template changes. |
| ~~**B-2**~~ | **FIXED by 0.2b** | `views.py:400` | ~~The M2 task-name mapping is duplicated **four** times; three copies still name `'Finance Confirmation'`, a task deleted from the template.~~ One module-level `_FINANCE_TASK_TO_MILESTONE`, with the reverse map **derived** so the two directions cannot drift apart. **The behavioural half is not fixed:** M2 still syncs only from the task-row dropdown, never from the task detail page; every site keeps its "non-critical" except-pass wrapper, and a zero-row `.update()` still raises nothing. Only the *drift* was closed. |
| **B-3** | **OPEN — scheduled phase 5** | app-wide | **There is no project-closure workflow.** Only `'Draft'` and `'Active'` are ever written to `Project.status`; `Commissioned`, `In Progress`, `On Hold` and `Cancelled` are unreachable and `commissioned_at` is never written. A finished project stays Active forever. *Decided 25 Aug: fixed in **phase 5** alongside COD and HOTO; prompt 1.3 owns only the opening transition.* Re-verified 29 Aug — still zero writes of `'Commissioned'` or `commissioned_at` anywhere in `views.py` or `utils.py`. |
| **B-4** | **OPEN by decision — phase 3** | `design_views.py:221` | The design module is OPEX-only — see §5. *Decided 25 Aug: **phase 3** targets OPEX/tender only, and Residential keeps its six design tasks unchanged.* This is a scope decision, not a defect awaiting a fix — do not port the design module to a second project type. |
| ~~**B-5**~~ | **FIXED by 0.2b** | `views.py:4763` | ~~`_notify_boq_acknowledged()` fires from `boq_detail`'s inline acknowledge branch but not from the standalone `boq_acknowledge` endpoint.~~ Status write, `ActivityLog`, notification and (since 0.3) the transition row all live in `_apply_boq_acknowledgement()`, called by both. **One asymmetry survives on purpose:** only the inline path writes a `BOQRevision` snapshot — open as **B-8** in `EXECUTION_MODULE_DEFERRED.md`. |
| **B-6** | **OPEN — scheduled 2.3** | `views.py:3718` | No task-completion gate consults the checklist. §9's "installation checklist blocking" is a target, not current behaviour. 0.5 versioned and snapshotted the checklist but explicitly did **not** add the gate. Re-verified 29 Aug: `task_status_update` reads no checklist state, and `views.py:8067` records the omission as intentional. **Prompt 2.3 owns it.** |
| ~~**B-7**~~ | **FIXED by 0.2b** | `views.py:6254` | ~~The standalone `boq_submit` endpoint snapshots raw `Decimal`s into a plain `JSONField` and raises an **unhandled `TypeError` on every submission**. A live 500 on a reachable URL.~~ It now calls `_boq_snapshot()`. **0.2b also changed behaviour here deliberately:** `boq_submit`'s status precondition adopted the inline branch's rule — `Draft \| Revision Requested \| Acknowledged`, the latter two treated as a resubmission — so the two paths agree on what may be submitted. That is the one intentional behaviour change in the 0.2b commit. |

**The pattern that mattered more than any single item:** B-2, B-5 and B-7 were each a business act implemented twice, where only one copy was kept current — whether the system behaved correctly depended on which button the user pressed. A permissions lockdown applied to each duplicate separately would have multiplied that class of defect rather than exposing it, which is why 0.2b consolidated **before** 0.2's gates went on. **That ordering is the reusable lesson: deduplicate the act, then gate it once.**

## 12. Decision log

| Date | Decision |
|---|---|
| 23 Aug | D-1 SiteGroup carries `group_type`; lock stays procurement-only |
| 23 Aug | D-2 `PaymentRequest` is extended, never replaced |
| 23 Aug | D-3 Extend `Task`/`ProjectPhase`; no parallel task model |
| 24 Aug | D-4 restated: CEO and Admin unrestricted; per-site roles scoped by assignment |
| 24 Aug | ~~D-1 requires altering `uniq_active_site_group_membership` to `(project, group_type)` plus a full caller audit, in prompt 1.1~~ **SUPERSEDED 29 Aug — the alteration as written does not compile** (a constraint's fields must be local columns; audit F-1). Kept as the record of what was believed on 24 Aug. See the 29 Aug Shape A entry below. The *caller audit* half of this row stands and is 1.1b's. |
| 24 Aug | QA/QC, HSE and Warehouse Keeper are boolean flags, not `ROLE_CHOICES` values (R-15) |
| 24 Aug | Warehouse keeper is a real logged-in user in phase 1 |
| 24 Aug | HSE clearance is once per site at mobilisation, not per task |
| 24 Aug | Prompt 0.6 rescoped to documentation only |
| **25 Aug** | **Finance and SCM stay portfolio-wide in 0.2.** Task-based scoping gives SCM nothing (all 11 template tasks are created unassigned) and Finance one hardcoded person. Narrowing them would break `boq_acknowledge`, `create_delivery_challan` and the payment path system-wide. Revisit once an assignment table exists. |
| **25 Aug** | **BD and Design Head unchanged.** Neither has a per-user term to scope on. |
| **25 Aug** | **Soft-delete correctness is its own prompt (0.2c)**, not folded into the lockdown. |
| **25 Aug** | **Phase 3 targets OPEX/tender only.** Residential has no design workflow to extend. |
| **25 Aug** | **Project closure is fixed in phase 5** with COD and HOTO. Prompt 1.3 owns only the opening transition. |
| **25 Aug** | **Duplicate business paths are consolidated in 0.2b, before the lockdown.** |
| **28 Aug** | **`StatusTransition` ships in 0.3 covering six subject types, not all of them.** The design module is a session of its own — see §13. |
| **28 Aug** | **System Admin is unrestricted, as its own branch in `user_can_view_project()` — not by being added to `PORTFOLIO_VIEW_ROLES`.** That set means "roles whose dashboards already query every active project with no per-user term"; System Admin is not one, and conflating the two would make the set mean two things. The branch is load-bearing: 0.2 removed the PM-only guard that had been letting System Admin through by omission, so without it System Admin loses the whole product in the same edit. |
| **28 Aug** | **`role_required()` denies with 403 instead of redirecting, and a profile-less user is denied rather than treated as `'Admin'`.** Holding no role is not a reason to be given the most powerful one. **Has a deployment consequence — B-20.** |
| **28 Aug** | **GRN confirmation is scoped to engineers holding a task on that site.** The role gate is kept and the scope check added beside it: two independent questions, two independent answers. **Whether this matches how receipt actually happens is B-19, and it is the product owner's call.** |
| **28 Aug** | **One helper, `user_can_view_project()`, gates both reads and writes.** The audit proposed a separate `user_can_act_on_project()`; it was not built, because visibility and write authority coincide under today's policy. Recorded so the next session knows it is absent by decision rather than by oversight. |
| **28 Aug** | **`boq_submit` adopts the inline branch's status precondition** (`Draft \| Revision Requested \| Acknowledged`). The deliberate behaviour change in 0.2b — the point of consolidating was that the two paths agree, and the inline rule is the one the UI exercises. |
| **28 Aug** | **`_active_project()` is the Project resolution path (R-16).** Four pre-existing call sites that already filtered `is_deleted` were left as they were: 0.2c's scope was closing the gaps, not churning the safe lines. |
| **29 Aug** | **`docs/delivery-state-authority.md` is written rather than the duplication being reconciled.** Prompt 0.6 was rescoped to documentation on 24 Aug precisely because the duplication is a considered coverage trade. The file names the condition under which the trade should be revisited, so a future session can tell deliberate from rotten without asking anybody. |
| **29 Aug** | **Phase 0 closes with the Finance/SCM/BD policy gap open, not resolved.** `ProgramAssignment` was designed in `ACCESS_ISOLATION_AUDIT.md` Task E and deliberately not built — the endpoint surface is fixed, the role policy waits for the assignment table (Q-E3). |
| **29 Aug** | **R-16 and R-17 are recorded as rules because phase 0 established them in code**, not as aspirations. R-16 is stated with its own exceptions named, because a rule that overstates what the code does is worse than no rule. |
| **29 Aug** | **Q-E1 closed: NO — Residential does not need execution grouping.** PMs do not batch residential installs by crew or by area; one install is scheduled by itself. `SiteGroup.program` **stays non-nullable**, so the Residential exclusion in `RESIDENTIAL_BASELINE.md` §3.3 is now a decision rather than a side effect of the schema. |
| **29 Aug** | **Shape A approved: `group_type` is denormalised onto `SiteGroupMembership`, and F-1 is the reason.** A `UniqueConstraint`'s `fields` must be local columns, so `(project, group_type)` cannot reach through the FK to `SiteGroup` — D-1's old ⚠ box proposed something that does not compile. The copy is taken from the group in `save()`, may never be changed on either row, and is never joined on (`StatusTransition.actor_role_code` is the precedent). This is the only shape in which the **database** enforces D-1 instead of a view. Its known limit is stated in the model: `QuerySet.update()` and `bulk_create()` bypass `save()` and therefore both guards. |
| **29 Aug** | **Rollback position for `0071_site_group_type`, stated precisely.** The migration is not reversible once any project holds two live memberships — the reverse restores a `(project)`-unique index that those rows violate. **It does not follow that a backup is the only rollback**, which is what the audit implied. *While no execution membership exists*, a plain code revert is a **complete** rollback: every row is `procurement`, so `(project, group_type)` unique is behaviourally identical to `(project)` unique, and the extra column is inert. Once execution memberships exist, the recovery is to **stamp `removed_at` on them** — the constraint is partial, so tombstoned rows stop counting and the old invariant is restored **without touching schema**. Take a backup before applying regardless. |

---

## 13. `StatusTransition` coverage — what is instrumented, and what is not

Added by prompt 0.3, 28 Aug 2026. **Read this before drawing any conclusion from a
missing row.**

A partly-populated ledger is worse than an empty one if nobody knows where it stops.
An absent `StatusTransition` row means one of two completely different things — "that
status change did not happen" or "that model was never instrumented" — and only this
table tells you which. Every session that adds or removes instrumentation updates it.

### Instrumented — every status write goes through `record_transition()` (R-2)

| `subject_type` | Model | Write sites covered |
|---|---|---|
| `project` | `Project` | `project_create` (→ Draft), `project_activate` (Draft → Active), `create_opex_site` (→ Draft), the Zoho webhook's project creation (→ Draft, `actor=None`) |
| `task` | `Task` | `task_status_update` and `task_detail_status_update` — both the ordinary ladder and the auto-block branch — plus both directions of the task↔milestone sync |
| `boq` | `BOQ` | `boq_submit`, the inline `submit_design` branch of `boq_detail`, `_apply_boq_acknowledgement` (shared by both acknowledge paths), `boq_request_revision` |
| `delivery_challan` | `DeliveryChallan` | `create_delivery_challan` (→ Expected) and every outcome of `recalculate_dc_status()`, which is instrumented **inside the function** so `confirm_grn` and `override_grn` cannot diverge |
| `issue` | `Issue` | all five creation sites (→ Open), `update_issue_status`, `resolve_issue`, `close_issue`, `reopen_issue` |
| `payment_milestone` | `PaymentMilestone` | `milestone_invoice`, `milestone_receive`, the Finance branch of `project_overview`'s `update_milestone`, and both directions of the task↔milestone sync |

**Corrections to the record made while instrumenting.** `Project.status` is written in
**four** places, not the two previously believed — `create_opex_site` and the Zoho
webhook are the other two. And 0.2b consolidated the BOQ *snapshot* and the BOQ
*acknowledgement*, but **not** the BOQ *submit*: `boq_submit` and the inline
`submit_design` branch still both write `status = 'Submitted'`. Both are instrumented;
consolidating them is 0.2b-shaped work and was not done in 0.3 (R-12).

### NOT instrumented — a missing row here means nothing

| Model | Statuses | Why not |
|---|---|---|
| `DesignAssignment` | 14 (`DESIGN_ASSIGNMENT_STATUS_CHOICES`) | The richest workflow in the product. Instrumenting it means editing `design_views.py`, which has been correctly scoped and untouched all programme. **It is a session of its own.** |
| `DesignAttempt` | its own lifecycle, plus several existing terminal stamps | Same reason. Note it already carries ad-hoc stamps, so a future session must decide whether those become transitions or stay beside them. |
| `PaymentRequest` | `pending \| confirmed` (`payment_request_confirm`) | A seventh status-bearing model outside 0.3's six-value subject vocabulary. Adding it means a new `subject_type` constant and a `SUBJECT_TYPE_CHOICES` migration — a schema change, so R-1 applies. |
| `SiteGroup` | `SITE_GROUP_STATUS_CHOICES` | Not yet reached by the execution build plan; prompt 1.1 owns it. |
| `Milestone` (legacy) | `pending \| in_progress \| completed \| delayed` | Superseded by `PaymentMilestone` and kept only for schema compatibility. Do not instrument it; do not revive it. |
| `TaskTemplate` | `draft \| active \| archived` (`activate()`) | **Added by 0.4.** A new `subject_type` is a schema change (R-1). Version history is legible without it: the rows themselves are the record, `effective_from` stamps the promotion, and R-7 makes them immutable. |
| `Checklist` | `draft \| active \| archived` (`activate()`) | **Added by 0.5.** Same reasoning as `TaskTemplate` above. |

*(The last two rows were folded in by the 0.6 close-out. §14 and §15 each asked the reader to
add their own row here when reading; that is now done, so this table is the whole answer and
needs no mental patching.)*

Adding a subject type is three coordinated edits and one migration: the `SUBJECT_*`
constant and `SUBJECT_TYPE_CHOICES` entry in `models.py`, the model-class entry in
`utils._subject_type_registry()` and `_SUBJECT_PROJECT_RESOLVERS`, the call sites
themselves — **and a row moved from the second table above to the first.**

### Two properties that are enforced in code, not in the schema

- **R-9 (mandatory remarks)** is enforced by `record_transition()` against
  `REMARK_REQUIRED_SUBJECT_TYPES`, which is **empty today**. It cannot be a `NOT NULL`
  column while the retrofitted paths collect no remark; making it one would 500 every
  task status change. Phase 2 adds `task` to the set when two-step completion ships.
- **R-4 (append-only)** is enforced by `save()`/`delete()` overrides on the model. Those
  are bypassed by `QuerySet.update()` and `QuerySet.delete()`. The stronger form is a
  database-level `REVOKE UPDATE, DELETE ON projects_statustransition`, deliberately left
  for a deployment task.

---

## 14. Task templates — versioned, and what that does and does not change

Added by prompt 0.4, 28 Aug 2026. **Read this before assuming a project reflects the
template it was built from.**

### The template is data now

`build_residential_phases()` no longer runs at activation. `attach_residential_template()`
reads the **active `TaskTemplate`** for the project type instead. Migration `0067` seeded
the Residential list as `RESIDENTIAL` v1 — 9 phases, 52 tasks, durations resolved by the
same `_get_duration()` call the old code made, against the live `TaskDurationTemplate`
rows — so a project activated after the migration gets exactly what one activated before
it got. The function stays in `utils.py` as the seed the migration read and as the source
the virgin-database bootstrap re-seeds from; it is not executed at runtime any more.

| Model | Holds |
|---|---|
| `TaskTemplate` | one numbered version — `code`, `label`, `project_type`, `version_no`, `status`, `effective_from` |
| `TaskTemplatePhase` | `code`, `label`, `sort_order` — becomes a `ProjectPhase` |
| `TaskTemplateTask` | `code`, `label`, `sort_order`, `assigned_role`, `task_type`, `duration_days`, `is_payment_milestone` — becomes a `Task` |

`assigned_role` and `task_type` use `Task.ROLE_CHOICES` and `Task.TYPE_CHOICES`. No new
role or type vocabulary was introduced. `code` is the cross-version identity — when v2
rewords a label, `code` is what says it is still the same row.

### B-10 is CLOSED — the answer is *nothing*

> *"What happens to an in-flight project when its template is upgraded?"*

**Nothing happens to it, and nothing can.** `Task.task_name`, `assigned_role`,
`task_type`, `duration_days` and `is_payment_milestone` are **copies taken at
`bulk_create`**, not reads through a foreign key. An in-flight project holds its own rows
and is structurally immune to any later version. Publishing v2 changes what the *next*
activation produces and nothing else.

That also settles R-8 for tasks without a new column: **`Task.task_name` is already the
snapshot.** A `label_snapshot` field was considered and deliberately **not** added — it
would duplicate `task_name` and become a second thing to keep in sync. The only thing
`Task` gained is `template_task`, a nullable `SET_NULL` FK recording **provenance only**.
Nothing reads it back to decide behaviour, and nothing may start to: the moment a code
path resolves a task's name, role or duration *through* that FK, B-10 reopens.
`tests_task_template.InFlightProjectIsolationTests` is the guard on that.

### R-7 is enforced, not documented

A version's content is editable **only while `status='draft'`**. `TaskTemplatePhase` and
`TaskTemplateTask` override `save()` and `delete()` to raise `TemplateVersionLocked`
otherwise — archived is frozen as hard as active, because an archived version is the
record of what last month's projects were built from. `TaskTemplate.activate()` promotes a
draft and archives the outgoing version **in one transaction**; the partial unique
constraint `uniq_active_task_template_per_code` is what makes "at most one active version
per code" true at the database rather than by convention.

**Same enforcement limit as `StatusTransition` (§13), stated rather than implied:**
`QuerySet.update()`, `QuerySet.delete()` and the FK cascade from deleting a `TaskTemplate`
all operate in SQL and bypass the overrides entirely.

### `TaskDurationTemplate` is superseded and no longer read by anything

Its two editors — `admin_task_durations` (Admin) and `subadmin_task_durations` (System
Admin), which were byte-similar duplicates of one POST handler — are **read-only** and now
render the **active `TaskTemplate`**. Repointing them to *edit* was rejected: editing a
duration in place would violate R-7, and doing it correctly means "create v+1, edit,
activate", which is the template-authoring UI that 0.4 explicitly does not build. Showing
the stale table read-only was rejected for the same reason the editor was — it would still
be presenting a number nothing acts on.

**The table is deliberately not dropped.** That is a separate decision with its own
migration. Do not repoint anything back at the model.

### What 0.4 did NOT do

- **No OPEX or CAPEX template exists.** 0.4 makes one *possible*; prompt 1.3 creates it,
  and it needs the phase and task list from the projects team first (**B-09**).
- **No template-authoring UI.** Templates are seeded and edited through migrations or the
  Django admin, which refuses to open a form on a non-draft version. A real UI is phase 1
  at the earliest.
- **No change to any in-flight project.** Verified by fingerprinting every pre-existing
  column of `projects_task`, `projects_projectphase` and `projects_project` before and
  after the migration; all three hashes are identical.
- **`get_residential_template_task_names()`** (the checklist admin's task-name picker) now
  reads the active template, so it cannot drift from what activation creates. It falls
  back to `build_residential_phases()` only on a database that holds no template at all.

### Instrumentation — no change to §13

0.4 adds **no** `StatusTransition` subject type. `TaskTemplate.status`
(`draft | active | archived`) is **not instrumented**: it is an eighth status-bearing model
outside 0.3's six-value subject vocabulary, and adding it means a new `SUBJECT_*` constant
and a `SUBJECT_TYPE_CHOICES` migration — a schema change, so R-1 applies. **The 0.6 close-out
folded this row into §13's "NOT instrumented" table**, so that table is now complete and this
paragraph is the reasoning behind one of its rows rather than an instruction to patch it.

The §13 "Instrumented" table is **unchanged** — the six subject types 0.3 covered are still
exactly the six that are covered.

### Decision log addition

| Date | Decision |
|---|---|
| **28 Aug** | **Templates are versioned data, immutable once active (R-7).** The Residential list is `RESIDENTIAL` v1; `build_residential_phases()` is kept as its seed and no longer runs at activation. |
| **28 Aug** | **B-10 closed: nothing happens to an in-flight project on upgrade.** Instances hold copied rows. `Task.template_task` is provenance only and must never become a behaviour lookup. |
| **28 Aug** | **No `label_snapshot` on `Task`.** `task_name` already is the snapshot; a second copy would only be a second thing to keep in sync. |
| **28 Aug** | **`TaskDurationTemplate`'s two editors made read-only, not repointed.** Repointing them to edit would either break R-7 or require the authoring UI 0.4 does not build. The table stays; dropping it is its own decision. |

---

## 15. Checklists — versioned, snapshotted, and no longer destroyed by their own FK

Added by prompt 0.5, 29 Aug 2026. **Read this before drawing any conclusion from a
checklist completion, and before assuming a checklist can be edited in place.**

### The two defects it closes

`ChecklistItemCompletion` foreign-keyed `ChecklistItem` and stored no copy of the text it
was answering. That produced two failures with the same root:

- **Rewriting.** Rewording a label changed the wording shown against every completion
  already recorded against it. Sites completed in March displayed April's question, and a
  tick recorded against the old one appeared to answer the new one.
- **Deletion.** The FK was `CASCADE`, so deleting an item deleted every completion of it —
  the tick, the photo reference, who checked it and when. Not rewritten; gone. One admin
  tidying a checklist erased the inspection record of every site that had answered it.

For CEIG paperwork, completion certificates and warranty claims that is a compromised
audit trail rather than a display bug.

### R-8 — the completion stores the text it answered

`ChecklistItemCompletion.item_text_snapshot` is written from `item.label` in the **same
`save()`** that writes `is_checked` and the three `photo_*` fields, so a checked item can
no more lack the question it answered than it can lack its photo. `BOQRevision.snapshot`
is the existing precedent for the pattern.

**The FK is now `null=True, on_delete=SET_NULL`.** It is provenance; the snapshot is the
record. A completion whose `item` is null answers a question that no longer exists, and it
is still evidence.

`unique_together ('item', 'task')` became a **partial** `UniqueConstraint` with
`condition=Q(item__isnull=False)`. Live rows keep exactly the old guarantee — one
completion per (item, task). Orphans are deliberately unconstrained, because two
completions orphaned on one task answered two genuinely different questions and both must
survive. Making the condition explicit rather than relying on per-backend NULL semantics
is the point; the plain form would have degraded to the same behaviour by accident.

**Every read path renders the snapshot** — `_checklist_context()` computes the label once
and `_checklist.html` renders `row.label`; `ChecklistItemCompletionAdmin` shows an
`answered_text` column. An unchecked row still shows the live label, because it is the
question being asked now, not one that was answered.

### R-7 — the checklist is versioned, on `TaskTemplate`'s design

Deliberately the same shape, constraint style and enforcement as 0.4's task templates,
because two versioned template families in one codebase must not carry two designs.
`_require_draft_template()` and `TemplateVersionLocked` are **shared**, not duplicated.

| | |
|---|---|
| Identity across versions | `Checklist.code` — derived from the name on first save, since the admin screen only asks for a name. **Version 2+ must pass the family code explicitly**; deriving it again would produce a disambiguated code and a different family. |
| Constraints | `uniq_checklist_code_version`, and `uniq_active_checklist_per_code` (partial, `status='active'`) |
| Immutability | `ChecklistItem.save()`/`delete()` raise unless the parent is `draft`. Archived is frozen as hard as active. **Adding** is content too: a question added to a live checklist retroactively makes every site that already completed it incomplete. |
| Promotion | `Checklist.activate()` — archives the outgoing version and promotes the draft in one transaction |

**`is_active` is gone as a column** (migration 0070) and survives as a read/write property
over `status`: reads answer "is this the live version", writes map `True` to active and
`False` to archived. One stored answer, and no second thing to keep in sync.

`_checklist_for_task()` **resolves through the family, not the linked row**:
`ChecklistTaskLink` says which family a task uses, `status` says which version is live.
Every checklist migrated in as the sole v1 of its own code, so for every task that exists
today it returns exactly the row `link.checklist.is_active` returned. Without it,
publishing v2 would leave the link on the archived v1 and the checklist would vanish off
the task. `ChecklistTaskLink` is therefore **not** version content and stays editable on a
published version — it also could not be per-version, because `unique_together
('task_name', 'project_type')` forbids v1 and v2 both linking the same task name.

### Enforcement limits, stated rather than implied

- The R-7 guards are `save()`/`delete()` overrides. `QuerySet.update()`,
  `QuerySet.delete()` and the FK cascade from deleting a `Checklist` bypass them, exactly
  as for `TaskTemplate` (§14) and `StatusTransition` (§13).
- `SET_NULL` is enforced by Django's collector, **not** by a database-level
  `ON DELETE SET NULL` — Django emits `NO ACTION`. A raw SQL `DELETE` against
  `projects_checklistitem` now **raises a foreign-key violation** instead of cascading.
  That is strictly safer than the old behaviour, which deleted the history silently, but
  it is not the same as the ORM path.
- The backfill is **best effort and cannot be otherwise.** A label reworded before 29 Aug
  2026 was recorded as it stood on that date, not as it was answered. That history was
  already unrecoverable. And a completion destroyed by the old cascade was *deleted*, not
  orphaned — there is no null-item row standing in for it.

### Authoring changed shape, and this is a real cost

The portal-admin flow is now **create draft, add items, publish**. A new checklist is a
draft and is shown on no task until published. The "Active" checkbox is replaced by
Publish and Archive actions, and **archiving is not reversible** — republishing means
publishing version+1. Creating version 2 is not a screen 0.5 builds; it is done through
the Django admin or a migration.

### Instrumentation — no change to §13

0.5 adds **no** `StatusTransition` subject type. `Checklist.status` is not instrumented,
for the same reason `TaskTemplate.status` is not: a new `subject_type` is a
`SUBJECT_TYPE_CHOICES` migration, and R-1 applies. **The 0.6 close-out folded this row into
§13's "NOT instrumented" table**, so that table is complete as it stands.

### What 0.5 did NOT do

- **No relink to template tasks.** `ChecklistTaskLink` still matches on
  `(task_name, project_type)`; prompt 2.4 replaces the string match with a
  `TaskTemplateTask` reference now that 0.4 has created those rows.
- **No response types, acceptance rules or `fail_raises_punch`.** Phase 2.
- **No checklist gate on task completion.** That is **B-6** and it belongs to 2.3.
- **No checklist-authoring UI.**

### The regression net paid for this — settled by prompt 1.0

Four tests in the two files 0.5 was not allowed to modify built their fixture as *create
active checklist, then add an item*, which is precisely what R-7 forbids, and failed with
`TemplateVersionLocked` at the fixture line. **Prompt 1.0 reordered all three copies of that
fixture to *create draft, add items, `activate()`* — no assertion changed, no non-test file
changed.** The suite is 645 tests, 1 failure, 0 errors, that one failure being the known
SQLite constraint-name message difference in `tests_design_part46`. The fixture turned out to
be written out three times rather than shared once; that duplication is recorded as **B3** in
`EXECUTION_MODULE_DEFERRED.md`, along with **B4**, the `is_active` setter that let an active
checklist be created without passing through `activate()` in the first place.

### Decision log addition

| Date | Decision |
|---|---|
| **29 Aug** | **Checklists are versioned data, immutable once active (R-7)** — the same design as `TaskTemplate`, sharing its guard and its exception rather than restating them. |
| **29 Aug** | **Completions snapshot the answered text (R-8) and the item FK is `SET_NULL`.** The FK is provenance; the snapshot is the record. |
| **29 Aug** | **`is_active` removed as a column, kept as a property over `status`.** Two columns answering "is this live" is the drift the session existed to stop. |
| **29 Aug** | **`ChecklistTaskLink` is not version content.** It names the family; `status` names the live version. `_checklist_for_task()` resolves through the family so publishing v2 moves the task onto it. |
| **29 Aug** | **Archiving is terminal.** Republishing means publishing version+1 — a change from the old reversible Active checkbox, accepted as the cost of R-7. |
| **29 Aug** | **Full R-7 parity chosen over keeping the regression net green.** Blocking *adds* as well as edits costs four fixture lines in files this session may not touch; a weaker guard would have cost a second immutability design. |
