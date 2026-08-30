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

> **✅ DONE. The schema half is prompt 1.1a (migration `0071_site_group_type`); the consumer half is prompt 1.1b. D-1 is implemented.**
>
> **This box previously said the fix was to alter the constraint to `(project, group_type)`. That was not implementable as written**, and `SITE_GROUP_AUDIT.md` F-1 is why: a `UniqueConstraint`'s `fields` must be **local columns**, and `group_type` lived only on `SiteGroup`. There is no way to reach it from `SiteGroupMembership` inside a constraint.
>
> **The shape actually built** — audit Shape A, approved 29 Aug: `group_type` is **denormalised onto `SiteGroupMembership` as well as `SiteGroup`**, copied from the group at insert by `SiteGroupMembership.save()`, and refused any later change by that same guard (and by `SiteGroup.save()`, so the source cannot drift away from its copies). The constraint is then written over the membership's **own** column and is named `uniq_active_site_group_membership_per_type`. This is the only shape in which the **database** enforces D-1 rather than a view. The membership's `group_type` is never a second source of truth and is never joined on — `StatusTransition.actor_role_code` is the same pattern.
>
> **1.1a changed no behaviour and left the caller problem open. 1.1b closed it.** Live code depended on the *old* guarantee: `active_group_membership()` documented that its `.first()` was "picking the only row that can exist, not the first of several", which was true only while no execution membership could exist. Every consumer has now been narrowed to `procurement`, and each says at its call site why.
>
> **`active_group_membership(project, group_type)` takes a REQUIRED type argument, by design — it is deliberately not defaulted to `procurement`.** A default is the exact failure D-1 introduces: a future execution caller that forgets the argument would be handed a procurement row silently, and every fact computed from it would answer a question it did not ask. Required means a new call site cannot be written without deciding. **Do not add a default to make a call site shorter.**
>
> **What 1.1b narrowed** — `design_views.py`: `active_group_membership()` and all three of its callers (`design_change_request`, `design_change_request_form`, `_add_sites`), `_group_or_404()`, `_group_rows()`, `post_qc_pool()`, and `site_group_create()` (which now states `group_type` rather than leaning on the model default). `permissions.py`: `project_boq_is_group_locked()`.
>
> **`_group_or_404()` is the load-bearing one.** Six views resolve their group through it, including both write paths — adding sites and locking — so `site_group_detail`, `site_group_remove_site` and `_group_member_ids` need no type filter of their own and deliberately do not have one. If that filter is ever removed, all six widen at once.
>
> **`post_qc_pool()` was the live regression.** It excluded any site with any live membership. Unnarrowed, the first execution group ever created would have deleted its sites from SCM's post-QC queue — released, unprocured, and invisible to the one screen that would have said so. Silent, and it would have surfaced at go-live rather than in review.
>
> **`remove_from_group()` was NOT narrowed**, and should not be: it is handed a row a caller already resolved, and its mechanism suits both types. Its *log text* hardcoded the word "procurement" and now comes from the row.
>
> **One consumer is knowingly unnarrowed:** `views.py : boq_detail`'s `locked_group` banner lookup. Cosmetic-only while `locked` is a procurement-only status, out of 1.1b's write scope, recorded as `EXECUTION_MODULE_DEFERRED.md` §B7.
>
> **Tests must manufacture the execution row.** Nothing in the product creates an execution group, so the suite passes whether or not a consumer is narrowed. `tests_site_group_type.ConsumerNarrowingTests` and two tests in `tests_design_groups.PostQCPoolTests` each build one by hand. A 1.1b-style test that does not is testing nothing.

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

**R-18 · Task status changes have one decision path: `_apply_task_status_change()`.** *(Established by prompt B8, 30 Aug 2026.)* **A new task-status rule is added to the helper, never to a view.**

`projects/views.py` holds one implementation of the task status decision — whether the transition is allowed, what it requires, the field writes, the `StatusTransition` row, the `ActivityLog` row, the milestone sync and the notification. Both user-facing entry points call it:

| | Entry point | Screen |
|---|---|---|
| ① ② | `task_status_update` | the project-overview task row |
| ③ ④ | `task_detail_status_update` | the task-detail status block |

**Why this is a rule and not a tidy-up.** Before B8 these were two near-identical ~180-line copies, reached from two screens the same person uses interchangeably. As 1.4a put it: *a rule added to one is not enforced, merely avoidable* — and the drift is silent, because the rule works on one screen and the person avoiding it never knows they are. Five features are queued behind this path (the mirror-task read-only refusal, the dependency early-start warning, two-step completion, the HSE gate, the QA/QC gate). Consolidated, each is written once.

**What stays in the view**, following the house precedent `_apply_boq_acknowledgement()` — extracted by 0.2b after the same defect, and whose docstring already rules that "gates and status preconditions stay with the callers": resolving the task, the permission gate, and the response. The helper never returns an `HttpResponse` and does not know which screen called it. **The permission gate is deliberately NOT shared** — the overview row is role-or-PM, the detail block is assignee-only, and those admit different people on purpose.

**The two screens still differ in four preserved ways** — the project-scope gate, the unassigned-task answer, the HTMX refusal shape, and `?next=` handling. B8 found and preserved them rather than resolving them, because every non-preserve answer is a behaviour change and B8's remit was explicitly none; per R-12 they are recorded as **B12–B15** in `EXECUTION_MODULE_DEFERRED.md` rather than fixed, and each is pinned by a test in `tests_task_status_path.DecidedDifferenceTests`. **Preserved is not endorsed** — a later prompt decides them.

**Enforced by** `projects/tests_task_status_path.py`: one contract mixin run through both entry points as `OverviewRowPathTests` and `TaskDetailPathTests`, so a rule that stops holding on one screen fails a named test. ⑤ `milestone_receive` and ⑥ the `update_milestone` branch of `project_overview` are **outside this rule** — see §13.

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
| Task dependencies | `TaskTemplateTaskDependency`, `TaskDependency` | **Built by 1.4a** (migration `0073`), answering B-08. **Finish-to-Start only, no lag, no `dependency_type`** — all three are decisions, see §12. The template-side model is **content of a template version** (R-7, guarded through the shared `_require_draft_template()`); the instance-side model is the project's **own copy**, written at activation by `materialise_task_dependencies()`, with **no FK back to the template edge** — the copy is the point (B-10 restated for edges). Both refuse self-edges (a database `CHECK`, so `bulk_create()` cannot get past it), duplicate edges (`UniqueConstraint`), cross-scope edges and **cycles** (`DependencyCycle`, naming the closing edge). `on_delete=CASCADE` on both ends of `TaskDependency`, chosen rather than inherited — an edge whose task is gone has no subject left. **1.4a did NOT wire `materialise_task_dependencies()` into `attach_residential_template()`**, and **no template version authors any edge today**, so the predicate is empty everywhere in production until somebody authors one |
| Task durations (superseded) | `TaskDurationTemplate` | `unique_together = ('project_type', 'task_name')` ✔ — **`phase_name` is stored and displayed but never matched on**. No version, no snapshot ✔. **Superseded by 0.4:** nothing reads it at runtime any more, its two editor screens are read-only and render the active `TaskTemplate`, and the table is deliberately not dropped. Do not repoint anything back at it |
| Checklists | `Checklist`, `ChecklistItem`, `ChecklistTaskLink`, `ChecklistItemCompletion` | Linked by `(task_name, project_type)` string match ✔ — still, until 2.4. **Corrected 29 Aug by 0.5:** the completion now carries `item_text_snapshot`, and the item FK is nullable `SET_NULL`, not `CASCADE`. `Checklist` is versioned (`code`/`version_no`/`status`) and its content is immutable once active. See §15 |
| Punch points | `Issue` | Carries severity, status, raised_by, assigned_to, due_date, resolution_note ✔; links to both `Task` and `DeliveryChallan`, both nullable `SET_NULL` ✔ |
| Warehouses / stock locations | `StockLocation` | **Built by 1.2a** (migration `0072`), answering B-14. One row per physical place material rests — warehouse, store, site container. `name`, unique `code`, optional `address`, `is_active`, and `keeper` → `UserProfile` (`null=True`, `SET_NULL`, `related_name='keeper_of'`, **not unique**). **Rows, never constants** — the three warehouses Horizon runs today are data the product owner enters, and nothing here seeds them. **Authority follows the warehouse, not the tender.** **No `is_deleted`** — `is_active` is the only retirement, deliberately, because this codebase has no custom managers. **Nothing reads it yet**; its consumer is 4.1. It is the *only* warehouse model — do not create a second |
| Delivery and GRN | `DeliveryChallan`, `DCLineItem` | Project-scoped; no warehouse, movement type or serials ✔ — **`StockLocation` is now where a warehouse would be named, when 4.1 links them.** **`DCLineItem` has no FK to `BOQItem` or `BOQItemMaster`** ✔ — only `boq_category` (CharField) and free-text `item_description`. Its `CATEGORY_CHOICES` has four values against `BOQItem`'s five ✔; anything in `Other` is unreconcilable by construction |
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
| `_require_draft_template()` / `TemplateVersionLocked` | `models.py` | **0.4**, shared by **0.5**, extended by **1.4a** | The R-7 immutability guard. `TaskTemplatePhase`/`TaskTemplateTask`, `ChecklistItem` and now `TaskTemplateTaskDependency` all raise through it. **Two versioned template families, one guard** — do not write a third |
| `incomplete_predecessors(task)` | `task_dependencies.py` | **1.4a** | **The B-08 predicate.** Returns the `Task` rows whose work is not yet Done that this task waits on — the rows themselves, not a boolean and not a count, because 1.4b has to name them in the warning. **It reports; it never refuses**, and its docstring is the design of the feature. One query, ordered the way the tasks appear on screen. Not in `views.py`, and not to grow a refusal |
| `materialise_task_dependencies(project)` | `task_dependencies.py` | **1.4a** | Copies a project's template dependency edges onto its own `Task` rows, deriving the template version from `Task.template_task` rather than re-resolving the active one. **Idempotent.** Writes one `save()` at a time and deliberately avoids `bulk_create()`, which would bypass every guard on `TaskDependency`. **Not yet called from anywhere** — its call site is inside `attach_residential_template()`'s existing atomic block, and 1.4b or a later session puts it there |

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
| ~~B-08~~ | ~~Can a site override a template task dependency?~~ **CLOSED 30 Aug 2026 — answered by the product owner and built at the model layer by prompt 1.4a.** The answer is **yes, by anyone, with a mandatory reason and a warning — no hard block and no role gate.** A dependent task may be started early; the system says what is being jumped and refuses to record it without a stated reason, and then allows it. Deliberately **not** a PM-only waiver and **not** a refusal: site sequence slips for reasons the template cannot know about, and a hard block would be routed around by marking the predecessor Done, which destroys the record the block existed to protect. The reason text is the deliverable — an override nobody explained is the failure mode, not an override itself. **What 1.4a built:** `TaskTemplateTaskDependency` and `TaskDependency` (migration `0073`), and `incomplete_predecessors()` in `projects/task_dependencies.py`, whose docstring *is* the design — *"Read-only. Empty result means nothing blocks a normal start. A non-empty result does NOT forbid the start."* **What is not built yet is the enforcement half — the warning and the mandatory reason on the status-change path — which is 1.4b**, tracked in `EXECUTION_PROMPT_LOG.md` rather than here, because the *question* is settled and only the wiring remains. **The way this decision gets undone is a future session reading the word "dependency" and adding a block**; `tests_task_dependencies.PredicateReportsNeverRefusesTests.test_incomplete_predecessors_returns_rather_than_raises` exists to fail loudly when one does. | — |
| B-09 | Final phase and task list for the execution template (Residential is 52 tasks / 9 phases; OPEX has none). **0.4 made an OPEX template possible; it did not make one.** | 1.3, 2.4 |
| ~~B-10~~ | ~~What happens to an in-flight project when its template is upgraded?~~ **CLOSED 28 Aug 2026 by prompt 0.4 — see §14.** The answer is *nothing*, and it is structural rather than a matter of policy: `Task.task_name`, `assigned_role`, `task_type`, `duration_days` and `is_payment_milestone` are **copies taken at `bulk_create`**, not reads through a foreign key, so an in-flight project holds its own rows and cannot be reached by a later version. Publishing v2 changes what the *next* activation produces and nothing else. `tests_task_template.InFlightProjectIsolationTests` proves it, and guards the one thing that would reopen the question — a code path resolving a task's name, role or duration *through* `Task.template_task`, which is provenance only. | — |
| B-12 | Who may waive a punch point, and does it need senior approval? **ANSWERED 30 Aug 2026 by the product owner — RECORDED, NOT CLOSED. It stays open until 2.3 builds it.** **The PM alone waives, at any severity, with no second signature.** Explicitly: **no severity threshold** — a critical punch point is waived by the same person and the same act as a trivial one, because a threshold only moves the argument to what counts as critical; and **no counter-signature** — there is no second approver role, and none is to be invented. The PM owns the site and the waiver is recorded against their name. Note this does **not** make the waiver a QA/QC act: `is_qaqc` grants raising a punch point and recording a verdict, never waiving one. | 2.3 |
| ~~B-14~~ | ~~How many warehouses, and how are keepers assigned to them?~~ **CLOSED 30 Aug 2026 by the product owner, built by prompt 1.2a.** **Three warehouses today — and the count must not be structural.** That is the substance of the answer, not a footnote to it: warehouses are **rows in `StockLocation`**, added through a screen when 4.1 builds one. Not a `choices` list, not a settings constant, not seeded by a migration — each of those spellings would make a fourth warehouse a code change and a deploy. **1.2a therefore seeds nothing**; entering the three that exist is a data task for the product owner. **One keeper per warehouse** (`StockLocation.keeper`, a single FK, not an M2M), and the FK is deliberately **not unique** — nothing says one person cannot cover two buildings, and on a three-warehouse operation with someone on leave they will. **Authority follows the warehouse, not the tender:** a keeper acts on everything inside their building — receiving, holding, issuing — whatever programme or tender paid for it, and on nothing inside any other building. This matches how SCM already works, where material lands at one shared drop point and cost is attributed **at issue, not at receipt**; a keeper who could only touch their own tender's material could not sign for the lorry that brought all of it. `is_warehouse_keeper` is a **flag on `UserProfile`**, not a role (R-15, §4). **Still open beside this: B-19** — who confirms a GRN — which is a process question about the same person and is not answered here. | — |
| B-15 | Net metering: hard block or warning, which project types, who owns it? | 5.1 |
| B-16 | Does a vendor work order or rate contract exist to validate verified amounts against? | 5.2 |
| B-17 | Retention, advances and debit notes — in PMS or in accounts? | 5.2 |
| B-18 | `DCLineItem` has no join key to `BOQItem`. Add the FK, or accept string matching? | 4.1, 4.3 |
| ~~**B-19**~~ | ~~**Who actually confirms a GRN — the engineer holding a task on that site, or whoever is standing at the warehouse when the lorry arrives?**~~ **ANSWERED 30 Aug 2026 by the product owner: the engineer holding a task on that site. 0.2's scoping was correct and needs no widening.** `confirm_grn` stays as 0.2 left it — scoped through `user_can_view_project()`, whose Site Engineer branch is exactly "holds a task on this project". Receipt is **not** recorded by whoever happens to be present. Note what this does and does not settle: it fixes who confirms a **GRN**, and it is not the same question as what a warehouse keeper may do inside their own building (B-14, answered separately). A keeper with no task on a site does not gain GRN confirmation on it by holding `is_warehouse_keeper` — that flag grants nothing on its own by construction. | — |
| ~~**B-20**~~ | ~~**Does a profile-less superuser exist on production?**~~ **ANSWERED 30 Aug 2026 by the product owner: NO. No such account exists, so 0.2's closure of the fail-open locks nobody out and no remedial step is needed before deployment.** The rule it established stands unchanged and is the part worth keeping: a user with no `UserProfile` is **denied**, never treated as `'Admin'`. If such an account is ever created, the fix is to give it a `UserProfile` with `role='Admin'` — **never** to restore the fallback. | — |

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
| **30 Aug** | **Task status changes have one decision path, and the rule is that new rules go in it (R-18).** Prompt B8 extracted `_apply_task_status_change()` from two near-identical ~180-line copies reached from two screens the same person uses interchangeably. The consolidation was worth doing *now* rather than later because of what is queued behind it — the mirror-task read-only refusal, the dependency early-start warning, two-step completion, the HSE gate and the QA/QC gate all land on this path. Written once each, instead of twice each with silent drift. |
| **30 Aug** | **B8 preserved all four behavioural differences between the two copies rather than resolving any of them, and that is the decision.** The project-scope gate, the unassigned-task status code, the HTMX refusal shape and `?next=` handling each differ between the screens. Every non-preserve answer is a behaviour change, which B8's remit excluded; one of them (the 400 on an unassigned task) is additionally pinned by a characterisation test. They are open as **B12–B15** and each is pinned by a test, so a later prompt that closes one gets a loud failure pointing at the entry. **Preserved is not endorsed.** |
| **30 Aug** | **⑤ `milestone_receive` and ⑥ `update_milestone` do NOT share the helper, and the ledger gap B8 was told to expect does not exist.** Both already call `record_transition()`. They are unattended syncs inside `except Exception: pass`; routing them through a helper that raises and emits `messages.*` would turn a visible failure silent and would apply five user-facing rules to machine writes. If ever revisited, the shape is a second, narrower helper — not a flag on this one (B16). |
| **30 Aug** | **B-14 answered and closed: three warehouses today, and the count must not be structural.** Warehouses are rows in `StockLocation`, added through a screen when 4.1 builds one — not a `choices` list, not a settings constant, not seeded by a migration, because each of those makes a fourth warehouse a code change and a deploy. 1.2a therefore **seeds nothing**. **One keeper per warehouse**, and the FK is **deliberately not unique** — one person may cover two buildings, and with three warehouses and someone on leave they will. **Authority follows the warehouse, not the tender:** a keeper acts on everything in their building whatever paid for it, matching SCM's existing model where material lands at a shared drop point and cost is attributed **at issue, not at receipt**. |
| **30 Aug** | **R-15 restated in code, with the reason attached: `is_qaqc`, `is_hse` and `is_warehouse_keeper` are boolean flags on `UserProfile`, never `ROLE_CHOICES` values.** The 24 Aug decision (R-15) had no code to point at; 1.2a gives it three columns and a class-level comment naming the cost. **The cost is `Task.assigned_role`**, which is matched against `UserProfile.role` as a plain string — a holder of a new role string matches no template task and cannot change a task's status, move a due date, or tick a checklist item, and every `@role_required`, every `role='...'` queryset and `_SA_EDITABLE_ROLE_CHOICES` would need widening beside it, failing silently if one is missed. This is not theory: `'Design Head'` was added as a role in migration `0048` and removed again in `0053` for exactly this. **The likeliest way this decision gets undone is a future session "fixing" a flag by promoting it to a role**, which is why the comment sits on the fields and `tests_capability_flags` asserts the names are absent from `ROLE_CHOICES`. **The 1.2a prompt cited this decision as "D-5"; there is no D-5 in this repository** — §2 holds D-1…D-4 only, and `EXECUTION_PROMPT_LOG.md` already records that D-5…D-9 appear nowhere. The decision is real, the label was not, and nothing has been written under it. **Cite R-15 and §4.** The three field names 1.2a shipped are exactly the ones §4 already specified. |
| **30 Aug** | **The capability flags ship with no UI and no permission helper, and that is the decision rather than the omission.** They are set from the shell or Django admin until a consumer exists — 2.2 for `is_hse`, 2.3 for `is_qaqc`, 4.1 for `is_warehouse_keeper` — and each consumer brings its own predicate with it (R-12). `user_is_keeper_of()` and its kind were **not** written here, because an unconsumed predicate is written against an imagined call site. **A capability flag grants nothing on its own**, and `tests_capability_flags.CapabilityFlagsGrantNothingTests` pins that as an absence: setting all three changes no role and gives `user_can_manage_project()` nothing. Both admin user-edit surfaces (`admin_user_edit` and the `subadmin_departments` edit branch) already carry the two design flags and can carry these three later **without a new screen** — noting that an unchecked box posts nothing, so every flag must be rendered wherever the profile is saved or a save silently clears it. |
| **30 Aug** | **`execution_groups_are_never_locked` — a rule that was prose becomes a `CheckConstraint`.** D-1 said the lock is procurement-only; until now that was upheld by six filter terms in the views and by nothing else. Both columns are local to `SiteGroup`, so it is expressible as a CHECK — unlike D-1's exclusivity, which needed F-1's denormalisation. **What it retires** is `EXECUTION_MODULE_DEFERRED.md` §B7: `boq_detail` reads a site's memberships without asking their type and was correct only *because* no locked execution group happened to exist. That is now a guarantee instead of a coincidence, and B7's remaining fix is cosmetic. §B7 **stays open** — the unnarrowed read is still there. `site_group_lock` needed no change: `_group_or_404()` resolves `group_type=procurement`, so the view cannot be handed an execution group and the constraint can never surface as an `IntegrityError` in a user's face. **One 1.1b test had to be rewritten** — it manufactured a locked execution group to prove the predicate ignored it, and that row is now unwritable. |
| **30 Aug** | **1.2 split into 1.2a and 1.2b; `ProjectAssignment` is not built.** 1.2a shipped the three additive changes above. `ProjectAssignment` is held because it needs a product-owner decision first: it would be a **second representation of project assignment** beside `assigned_pm`, `assigned_design` and `coordinators`, and whether it supersedes those or sits beside them is a policy question, not an implementation detail. Building it either way without the answer is how D-3's "no parallel model" rule gets broken by accident. Related and still open: Q-E3 and `ProgramAssignment`. |
| **30 Aug** | **B-08 answered and closed: a dependent task may be started early by ANYONE, with a mandatory reason and a warning.** No hard block, no role gate, no approval step. The reasoning is that a hard block gets routed around by marking the predecessor Done, which destroys the very record the block existed to protect — so the system reports what is being jumped, insists on a reason, and then allows it. **1.4a builds the reporting half** (`incomplete_predecessors()`); **1.4b builds the enforcement half** — the warning and the mandatory reason on the status-change path. The predicate's docstring is the design and is meant to be read as normative: *it reports; it never refuses.* |
| **30 Aug** | **Task dependencies are FINISH-TO-START ONLY. There is no `dependency_type` column, and its absence is a decision.** The original plan said "predecessors with type and lag". Nothing in the business has asked for start-to-start or finish-to-finish, and a type column that holds one value forever is worse than no column: it invites every read to branch on a value that never varies, and the first genuine second value then arrives into code that was never exercised on the first. Add the column when a real task list needs it — at which point it is one additive migration and the reads that must change are the ones the new value actually reaches. |
| **30 Aug** | **No lag on a dependency, same reasoning.** A `lag_days` column would be nullable-or-zero on every row in the system, and a zero that has never been anything else is a column nobody validates. Lag is also not the only thing it would be used for once it exists — it becomes the place people encode "wait a bit", which is a scheduling concern that belongs to whatever eventually answers B-05, not to an edge. |
| **30 Aug** | **Dependencies are authored at TEMPLATE level and MATERIALISED onto instances — two models, not one, and not a read-time resolution through the template.** `TaskTemplateTaskDependency` is content of a template version (R-7, editable only while draft, through the shared `_require_draft_template()`); `TaskDependency` is the project's own copy, written at activation by `materialise_task_dependencies()`. **The reason is B-10 restated for edges:** a template version can be superseded, and an in-flight project's dependencies must not change underneath it — the same reasoning that made `Task.task_name` a snapshot. There is deliberately **no FK from `TaskDependency` back to the template edge**, not even for provenance: the copy is the whole point, and a provenance FK is the first thing a later session would resolve behaviour through, which is exactly how B-10 reopens. **Per-project rewiring is deferred** — nothing today edits an instance edge, and the model does not stop it. |
| **30 Aug** | **Cycle prevention is a requirement, not a nicety, and it is enforced on both models.** A cycle makes every task in it permanently "waiting on a predecessor", and the loop is invisible until somebody tries to work — so it cannot be left to be noticed. The check is a plain breadth-first walk forward from the proposed successor over the version's (or project's) existing edges; if it reaches the proposed predecessor, the edge is refused with `DependencyCycle`, naming the closing edge and the chain that closes it. **A template is tens of tasks, so a straightforward traversal is correct and a clever one would only be harder to read.** The three-node case is the one a naive check misses — `A→B→C→A` sails past anything that only compares an edge with its own reverse — and `tests_task_dependencies.TemplateCycleTests` pins two, three and four nodes plus a diamond, which is convergence and must **not** be refused. |
| **30 Aug** | **1.4 split into 1.4a and 1.4b, and the reason is the split's whole justification.** 1.4a is model-layer only and ships one migration with no user-visible change. 1.4b edits `task_status_update` and `task_detail_status_update` — **the most-used write path in the product** — and is the first user-visible change in this programme. Riding that along with a migration would mean one review covering both a schema change and a behaviour change on the path every user touches daily. `EXECUTION_PROMPT_LOG.md` carries the enumeration of task status-write paths 1.4a's pre-flight produced, so 1.4b starts from a known list rather than rediscovering it. |

---

## 13. `StatusTransition` coverage — what is instrumented, and what is not

Added by prompt 0.3, 28 Aug 2026. **Read this before drawing any conclusion from a
missing row.**

A partly-populated ledger is worse than an empty one if nobody knows where it stops.
An absent `StatusTransition` row means one of two completely different things — "that
status change did not happen" or "that model was never instrumented" — and only this
table tells you which. Every session that adds or removes instrumentation updates it.

### Instrumented — every status write in the VIEW LAYER goes through `record_transition()` (R-2)

**Read the heading exactly as written.** It says *view layer*, and the qualifier is load-bearing —
see "The Django admin is not a view" below. Until 30 Aug 2026 this heading read "**every status
write goes through `record_transition()`**", with no qualifier, and that was **wrong**: the Django
admin change form wrote `Task.status` and `Project.status` directly, with no ledger row and no
mention anywhere in this section. If you have been reading this table since 0.3 and concluding
that a missing `task` row means the change did not happen, that conclusion was unsafe for admin
edits made before that date. ~~It is safe for `task` from B9 onward, and **still not safe for
`project`** — B10 is open.~~ **Corrected 30 Aug 2026 by prompt B10:** it is safe for `task` from
B9 onward and for `project` from B10 onward, both landed before the phase 1 deploy. For an admin
edit made *before* those two commits the conclusion stays unsafe and no later work can change
that — an unreconstructable gap is exactly the thing that cannot be reconstructed.

| `subject_type` | Model | Write sites covered |
|---|---|---|
| `project` | `Project` | `project_create` (→ Draft), `project_activate` (Draft → Active), `create_opex_site` (→ Draft), the Zoho webhook's project creation (→ Draft, `actor=None`) |
| `task` | `Task` | `_apply_task_status_change()` — the single decision path shared by `task_status_update` and `task_detail_status_update` since B8 (R-18), covering both the ordinary ladder and the auto-block branch — plus both directions of the task↔milestone sync, which stay separately instrumented in ⑤ `milestone_receive` and ⑥ `project_overview` and are **outside** R-18 by decision (deferred B16) |
| `boq` | `BOQ` | `boq_submit`, the inline `submit_design` branch of `boq_detail`, `_apply_boq_acknowledgement` (shared by both acknowledge paths), `boq_request_revision` |
| `delivery_challan` | `DeliveryChallan` | `create_delivery_challan` (→ Expected) and every outcome of `recalculate_dc_status()`, which is instrumented **inside the function** so `confirm_grn` and `override_grn` cannot diverge |
| `issue` | `Issue` | all five creation sites (→ Open), `update_issue_status`, `resolve_issue`, `close_issue`, `reopen_issue` |
| `payment_milestone` | `PaymentMilestone` | `milestone_invoice`, `milestone_receive`, the Finance branch of `project_overview`'s `update_milestone`, and both directions of the task↔milestone sync |

**Corrections to the record made while instrumenting.** ~~`Project.status` is written in
**four** places, not the two previously believed — `create_opex_site` and the Zoho
webhook are the other two.~~ **Corrected again 30 Aug 2026 by prompt B10: four was still short.**
`Project.status` was written in **five** places. The fifth was `ProjectAdmin`'s change form,
which 0.3 never counted because it went looking for views and the admin is not one — so the
"four" above was a true statement about `views.py` presented as a true statement about the
product, which is the same mistake in miniature that this whole section exists to stop.

The fifth site is now **closed rather than instrumented**: `status` is in
`ProjectAdmin.readonly_fields`, so the admin writes it nowhere and the count of instrumented
write sites correctly stays at four. Closing beat instrumenting for the reason B9 gave for
`Task` and one more that is specific to `Project` — `project_activate` is the only path that
attaches the phase and task template and stamps `activated_at`, so an admin form that wrote
`status` correctly would still produce an Active project with no phases. **The admin is not an
activation route.**

And 0.2b consolidated the BOQ *snapshot* and the BOQ
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

### The Django admin is not a view, and was writing statuses past this table

`ModelAdmin` saves a form's fields straight to the row. There is no place in that path for
`record_transition()`, so a status changed on an admin change form moves the record and leaves the
ledger silent — and a missing row is indistinguishable from "this model was never instrumented",
which is the one thing this whole section exists to prevent. It cannot be reconstructed afterwards.

| `ModelAdmin` | Status editable? | State |
|---|---|---|
| `TaskAdmin` (`projects/admin.py`) | No | **Closed 30 Aug 2026 by prompt B9** — `readonly_fields = ['status']`. `status` stays in `list_display`/`list_filter` (read paths) and must never enter `list_editable`, which writes past `readonly_fields`. Guarded by `AdminCannotWriteTaskStatusTests` in `projects/tests_status_transition.py`. |
| `ProjectAdmin` (`projects/admin.py`) | No | **Closed 30 Aug 2026 by prompt B10** — `status` added to `readonly_fields`. `list_editable` was already empty, so `readonly_fields` alone was the whole door. Guarded by `AdminCannotWriteProjectStatusTests`. |
| `BOQ`, `DeliveryChallan`, `Issue`, `PaymentMilestone` | n/a | Not registered on `admin.site` at all. Safe by absence, not by decision — **registering any of them for shell verification reopens this hole**, so add `readonly_fields = ['status']` in the same edit. No longer safe *silently*: see the standing guard below. |

The rule this leaves behind: **a status field belonging to any subject type in the first table
above must be in its `ModelAdmin`'s `readonly_fields`.** Instrumenting a seventh subject means
adding that line too, not only the three edits and the migration listed above.

**That rule is now a test, not a paragraph.**
`NoInstrumentedSubjectHasAnEditableAdminStatusTests` (B10, `projects/tests_status_transition.py`)
walks `utils._subject_type_registry()` itself — not a tuple copied from it — and for every subject
model that *is* registered on `admin.site`, asserts `status` is neither a bound field on the form
the admin builds nor present in `list_editable`. The four unregistered models pass trivially and
cost nothing; the day someone adds `admin.register(BOQ)` for shell convenience, the test fails and
names the reason. A seventh subject type is covered the moment it enters the registry, without
anyone remembering that test exists.

**And a warning about the surface both prompts were editing.** While B10 was proving the above
end to end, it found `ProjectAdmin` could not render at all: `DocumentInline.fields` named
`doc_type`, `title` and `file`, none of which `ProjectDocument` has, so `modelform_factory` raised
`FieldError` and **both admin project pages returned 500** — for an unknown length of time, with
every automated signal green. B10 had to drop the inline at runtime to finish its demonstration.

`python manage.py check` reported no issues and structurally cannot report this. Django lets a
`ModelAdmin.fields` entry name a field the *form* contributes rather than the model, so
`BaseModelAdminChecks._check_field_spec_item` treats any name it cannot find on the model as
presumed-valid and returns no error. That permission is correct and load-bearing; the cost is
that a typo and a genuine form-contributed field are indistinguishable until the form is built —
and building the form is something only a request does. **Admin field specs are checked at
request time. `manage.py check` is not cover for them.**

What covers them now is `EveryRegisteredAdminPageLoadsTests` (B11,
`projects/tests_admin_smoke.py`): for every model in `admin.site._registry` — the registry itself,
so a registration added later is covered without anyone remembering — it GETs the changelist and
the add form and asserts 200, naming the model and the admin class on failure. It needs no
fixtures, and it covers the add form rather than the change form; a `fields` entry that resolves
on add and fails on change would still slip past, which nothing in `projects/admin.py` does today.
B11 also corrected the inline: the names map to `file_name`, `file_type` and `file_url`, but it is
now **read-only and cannot add**, because a `ProjectDocument` row is a pointer into a Supabase
bucket and the admin cannot put an object there — same rule as `DesignFileAdmin`'s frozen
`bucket`/`path`.

One editable status remains reachable from `ProjectAdmin`, and it is deliberate:
`MilestoneInline` exposes `Milestone.status`. `Milestone` is the **legacy** model in the
"NOT instrumented" table above — superseded by `PaymentMilestone`, kept for schema compatibility,
never a subject type. Editing it writes past no ledger, because it has none by decision.

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
