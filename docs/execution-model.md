# Execution Module — Model and Conventions

**Commit to `docs/execution-model.md`.**

Read this file at the start of every execution-module session. It is the authority on vocabulary, structure and rules for this module. Where it conflicts with an older document, this file wins. Where it is silent, `PROJECT_CONTEXT.md` and `code commenting standard.txt` apply unchanged.

**Version 1.2 — 25 Aug 2026.** *(Prompt 0.3 appended §13 on 28 Aug 2026 — `StatusTransition` coverage. The version string is left at 1.2 deliberately: several later prompts hard-check for it, and a bump would trip their own stop conditions over an additive section.)* Built from v1.0 by the prompt 0.1 verification session (25 claims confirmed, 11 corrected), then extended by the A-0.2 access audit and the 0.2a regression baseline. Claims marked ✔ were verified against source. **§11 lists seven known defects found by 0.2a; §12 is the decision log.**

> **If the copy in `docs/` says "Last updated: 23 Aug 2026" it is v1.0 and has eleven errors in it. Replace it.**

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

> **⚠ This is blocked by an existing constraint and cannot be built naively.**
> `uniq_active_site_group_membership` is a partial unique constraint on **`project` alone** where `removed_at IS NULL` ✔ — at most one live membership per project across every group in the system. Two simultaneous memberships require altering it to `(project, group_type)`, which is a migration, which under R-1 means stop-and-propose.
>
> Live code depends on the constraint's guarantee. `active_group_membership()` documents that its `.first()` is "picking the only row that can exist, not the first of several" ✔, and its caller uses that single result to compute `in_draft_group`, the gate admitting a released design to a change request. **Adding a second membership type without fixing every caller makes that gate order-dependent.** Prompt 1.1 must audit and fix every caller, and the design change-request gate must explicitly ask for `procurement`.

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

---

## 4. Roles and ownership

`UserProfile.ROLE_CHOICES` contains **ten** values ✔: Admin, System Admin, PM, Project Coordinator, Site Engineer, Design, Finance, SCM, CEO, BD. CEO, Finance and SCM are first-class roles with their own dashboards, not incidental references.

The stored BD value is **`'BD'`** ✔. `'BD / Sales'` is a `Task.assigned_role` value, not a `UserProfile.role` value — do not confuse them.

`Task.ROLE_CHOICES` has six values ✔ and lacks Admin, System Admin, Project Coordinator and CEO. **A Project Coordinator therefore matches no `assigned_role` and cannot be assigned a task today** — relevant to prompts 1.2 and 2.1.

`_PROFILE_TO_TASK_ROLE` appears **seven** times in `views.py` ✔, every copy byte-identical and containing a single entry: `{'BD': 'BD / Sales'}`, applied as `.get(role, role)`. The two vocabularies differ in exactly one value; they differ in *membership* more than in naming. Prompt 1.2 consolidates the seven copies.

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
| Task template | `TaskDurationTemplate` | `unique_together = ('project_type', 'task_name')` ✔ — **`phase_name` is stored and displayed but never matched on**. Two identically-named tasks in different phases cannot carry different durations. No version, no snapshot ✔ |
| Checklists | `Checklist`, `ChecklistItem`, `ChecklistTaskLink`, `ChecklistItemCompletion` | Linked by `(task_name, project_type)` string match ✔. Completion FKs the item with **no snapshot field of any kind** ✔, and the FK is `CASCADE` — deleting an item deletes its history outright |
| Punch points | `Issue` | Carries severity, status, raised_by, assigned_to, due_date, resolution_note ✔; links to both `Task` and `DeliveryChallan`, both nullable `SET_NULL` ✔ |
| Delivery and GRN | `DeliveryChallan`, `DCLineItem` | Project-scoped; no warehouse, movement type or serials ✔. **`DCLineItem` has no FK to `BOQItem` or `BOQItemMaster`** ✔ — only `boq_category` (CharField) and free-text `item_description`. Its `CATEGORY_CHOICES` has four values against `BOQItem`'s five ✔; anything in `Other` is unreconcilable by construction |
| Vendor invoices | `PaymentRequest` | Live. `pending \| confirmed` ✔. "No edit/cancel by design" ✔ |
| Item catalogue | `BOQItemMaster` | What the BOQ is built from |
| BOQ | `BOQ`, `BOQItem`, `BOQRevision` | `BOQRevision.snapshot` is a `JSONField` ✔ — existing precedent for R-8 |
| Documents | `ProjectDocument`, `TaskAttachment`, `DesignFile` | Design bucket isolated by a hard guard ✔. `DESIGN_FILE_KIND_CHOICES` has three current kinds plus two legacy ✔, and **no per-file approval field exists anywhere** |
| Notifications | `send_notification()` | Single chokepoint with master switch then per-user preference ✔ — **with two documented bypasses**: `send_raw_email()` skips the master switch, `send_aggregate_email()` skips per-recipient preference ✔ |
| Activity feed | `ActivityLog` + `log_activity()` | Keeps its role — see R-3 |
| Design workflow | `DesignAssignment`, `DesignAttempt`, `ArkaSubmission`, `DesignFile`, `DesignChangeRequest` | **OPEX ONLY — see below.** `DESIGN_RELEASED` has exactly one exit ✔; a PM approval gate does not exist ✔ |

### ⚠ The design module is OPEX-only

Every design endpoint resolves its site through `_opex_site()`, which raises `Http404` unless `project_type == 'OPEX'` ✔. **Residential and CAPEX cannot enter the design workflow at all** — no allocation, no attempts, no QC gates, no `DESIGN_RELEASED`.

Residential "design" is six template tasks with `assigned_role='Design'`, moved through the ordinary status ladder, plus the BOQ authoring path. `Design Approval by Internal Team` and `Design Approval by Customer` are PM-role tasks with no artifact and no gate behind them.

**Decided 25 Aug: phase 3 of the build plan targets OPEX/tender only.** Residential keeps its six design tasks unchanged. Do not port the design module to a second project type.

`DesignSubmission` has a model and two read views and **no write path at all** ✔ — one URL, no create or update view. Recorded independently as A4 in `DESIGN_MODULE_DEFERRED.md`. Treat it as dead; do not confuse it with phase 3's design package.

---

## 6. Known problems — stated accurately

- **Delivery state is deliberately duplicated on the CEO dashboard, not the SCM one.** `dashboard_scm` reads `DeliveryChallan`/`DCLineItem` directly ✔ and reflects actual receipt state. The task-derived proxy is on the **CEO** dashboard ✔, and the code carries an explicit instruction: *"Do not 'improve' it by cross-checking against the SCM models — the two will disagree, and the disagreement is not a bug in either."* The reason given is coverage: production holds 1 challan and 2 BOQ rows across 28 active projects, so a challan-based card would be blank on 27 of 28. **This is a considered trade, not a defect.** It may deserve revisiting once real execution data exists — but only by decision, never by a session deciding to tidy it.
- **The "BOQ is finished" signals are separate on purpose.** `project_boq_is_design_locked()` and `project_boq_is_group_locked()` each carry a written rationale for being distinct predicates answering different questions — authority over a user versus state of a site ✔. Do not collapse them without an explicit decision.
- **No general transition history.** There are ad-hoc terminal stamps — `Task.completed_at`, `Task.blocked_since`, `Issue.resolved_at`/`closed_at`, several on `DesignAttempt` ✔ — but no from-status, no actor role, no reason. Dwell time in Blocked is partly answerable today; dwell time between two arbitrary states is not. Prompt 0.3 fixes the general case.
- **Checklist history is rewritable and deletable.** Prompt 0.5 fixes it. Until then, treat every newly authored checklist as carrying the defect.
- **Access scoping** — see D-4 for what is already correct and what is not.
- **`log_activity()` catches bare `Exception`** ✔ and can silently lose rows. Do not copy that pattern into `record_transition()`.
- **Soft delete has no custom managers** ✔ — zero `objects =` or `models.Manager` in `models.py`. Every queryset must filter `is_deleted=False` itself.
- **Tests can be run.** `solarpms/test_settings.py` exists and disables migrations so the schema builds from model state without `CREATE DATABASE` privilege: `python manage.py test projects --settings=solarpms.test_settings`. `SESSION_T_TEST_UNBLOCK.md` records the suite running green through this path. **There is no excuse for an unverified claim of "tests pass".**

---

## 7. Scope boundaries — not being built

Vendor portal logins · offline mode · stock balances or valuation · purchase orders · vendor work orders and rate contracts · HSE incidents, near-miss, toolbox talks and PPE registers · retention, advances and debit notes · mobile app · splitting `views.py`.

If a prompt appears to require one of these, **stop and ask**. Do not build a small version of it.

---

## 8. Open questions — do not answer these by guessing

| Ref | Question | Blocks |
|---|---|---|
| Q-E1 | Residential sites cannot join a SiteGroup (`program` is non-nullable). Do Residential projects need execution grouping at all? | 1.1 |
| Q-E2 | BD and Design Head scoping — BD is portfolio-wide by a written settled decision; Design Head is an authority flag rather than a project role. Confirm both. | 0.2 |
| Q-E3 | Do Finance and SCM get Program-level assignment as well as task-level, and who maintains it? | 0.2, 1.2 |
| B-05 | Do execution groups carry their own schedule and milestones, or are they only a grouping? | 1.1 |
| B-06 | Does a PM rejection of a design package open a design attempt, and under which reason? | 3.2 |
| B-07 | Who may mark a drawing not-applicable — designer, or Design Head at review? | 3.1 |
| B-08 | Can a site override a template task dependency? | 1.4 |
| B-09 | Final phase and task list for the execution template (Residential is 52 tasks / 9 phases; OPEX has none) | 1.3, 2.4 |
| ~~B-10~~ | ~~What happens to an in-flight project when its template is upgraded?~~ **CLOSED 28 Aug 2026 by prompt 0.4 — see §14.** The answer is *nothing*. | — |
| B-12 | Who may waive a punch point, and does it need senior approval? | 2.3 |
| B-14 | How many warehouses, and how are keepers assigned to them? **Now needed in phase 1** — a keeper logs in | 1.2, 4.1 |
| B-15 | Net metering: hard block or warning, which project types, who owns it? | 5.1 |
| B-16 | Does a vendor work order or rate contract exist to validate verified amounts against? | 5.2 |
| B-17 | Retention, advances and debit notes — in PMS or in accounts? | 5.2 |
| B-18 | `DCLineItem` has no join key to `BOQItem`. Add the FK, or accept string matching? | 4.1, 4.3 |

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

## 10. Decision log

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

## 11. Known defects — found by the 0.2a baseline session, none fixed

All seven are current behaviour, read from source. None is one of the fifteen findings in `ACCESS_ISOLATION_AUDIT.md`, and none is pinned by `projects/tests_residential_baseline.py`.

| # | Where | What |
|---|---|---|
| **B-1** | `views.py:2631` | Activation message says "53 tasks created". The code creates and asserts **52**. |
| **B-2** | `views.py:3909`, `:6232`, `:6766` | The M2 task-name mapping is duplicated **four** times; three copies still name `'Finance Confirmation'`, a task deleted from the template. M2 syncs only from the task-row dropdown, never from the task detail page, and never in the milestone→task direction. All four sites swallow the failure, and a zero-row `.update()` raises nothing. |
| **B-3** | app-wide | **There is no project-closure workflow.** Only `'Draft'` and `'Active'` are ever written to `Project.status`; `Commissioned`, `In Progress`, `On Hold` and `Cancelled` are unreachable and `commissioned_at` is never written. A finished project stays Active forever. *Decided 25 Aug: fixed in phase 5 alongside COD and HOTO.* |
| **B-4** | `design_views.py:218` | The design module is OPEX-only — see §5. |
| **B-5** | `views.py:5993` vs `:4483` | `_notify_boq_acknowledged()` fires from `boq_detail`'s inline acknowledge branch but not from the standalone `boq_acknowledge` endpoint. The same business event notifies or does not depending on which button was pressed. |
| **B-6** | `views.py:2349` | No task-completion gate consults the checklist. §9's "installation checklist blocking" is a target, not current behaviour. |
| **B-7** | `views.py:5970` | The standalone `boq_submit` endpoint snapshots raw `Decimal`s into a plain `JSONField` and raises an **unhandled `TypeError` on every submission**, on Postgres as well as SQLite. The inline `submit_design` branch coerces via `_boq_snapshot()` and works. A live 500 on a reachable URL. |

**The pattern that matters more than any single item:** B-2, B-5 and B-7 are each a business act implemented twice, where only one copy was kept current. Whether the system behaves correctly depends on which button the user pressed. A permissions lockdown applied to each duplicate separately multiplies this class of defect rather than exposing it — which is why prompt 0.2b consolidates before 0.2 gates.

## 12. Decision log

| Date | Decision |
|---|---|
| 23 Aug | D-1 SiteGroup carries `group_type`; lock stays procurement-only |
| 23 Aug | D-2 `PaymentRequest` is extended, never replaced |
| 23 Aug | D-3 Extend `Task`/`ProjectPhase`; no parallel task model |
| 24 Aug | D-4 restated: CEO and Admin unrestricted; per-site roles scoped by assignment |
| 24 Aug | D-1 requires altering `uniq_active_site_group_membership` to `(project, group_type)` plus a full caller audit, in prompt 1.1 |
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
and a `SUBJECT_TYPE_CHOICES` migration — a schema change, so R-1 applies. Add this row to
§13's "NOT instrumented" table when reading it:

| Model | Statuses | Why not |
|---|---|---|
| `TaskTemplate` | `draft \| active \| archived` (`activate()`) | New in 0.4. A new `subject_type` is a schema change (R-1). Version history is legible without it: the rows themselves are the record, `effective_from` stamps the promotion, and R-7 makes them immutable. |

The §13 "Instrumented" table is **unchanged** — the six subject types 0.3 covered are still
exactly the six that are covered.

### Decision log addition

| Date | Decision |
|---|---|
| **28 Aug** | **Templates are versioned data, immutable once active (R-7).** The Residential list is `RESIDENTIAL` v1; `build_residential_phases()` is kept as its seed and no longer runs at activation. |
| **28 Aug** | **B-10 closed: nothing happens to an in-flight project on upgrade.** Instances hold copied rows. `Task.template_task` is provenance only and must never become a behaviour lookup. |
| **28 Aug** | **No `label_snapshot` on `Task`.** `task_name` already is the snapshot; a second copy would only be a second thing to keep in sync. |
| **28 Aug** | **`TaskDurationTemplate`'s two editors made read-only, not repointed.** Repointing them to edit would either break R-7 or require the authoring UI 0.4 does not build. The table stays; dropping it is its own decision. |
