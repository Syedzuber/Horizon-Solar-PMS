# Execution Module — Phase 1 Outcome & Phase 2 Handover

Last updated: 03 Sep 2026. Written to carry the programme into a new chat session.

Branch: `execution-phase-1`, **merged to `main` and deployed to production on 03 Sep**. Production
is at migration **0075**.

---

## Working model

Zuber is product owner and verification gatekeeper. Claude (chat) writes the plan and one
self-contained prompt per session, reads the reports back, and keeps the documents current. Claude
Code implements. Claude does not touch the repo.

**One prompt, one fresh Claude Code session, one commit.** Named files only, never `git add -A` —
`railway_backup.dump`, a production database dump, sits gitignored in the repo directory.

Each prompt carries: a MODE block naming what may and may not be touched, a **pre-flight section
where the session stops and waits**, the tasks, stop conditions, a verification bar demanding raw
output, and a report-back list ending in a commit message.

**Nothing is done until Zuber has tested it.** Claude Code self-reports are claims, not evidence.

---

## What phase 1 delivered

Nineteen sessions. Every one committed.

| Prompt | What it did |
|---|---|
| 1.0 | Repaired four checklist fixture errors — two of six regression files were red |
| A-1.1 | **Audit** — SiteGroup membership; found D-1 as specified could not compile |
| 1.1a | `group_type` on `SiteGroup` and `SiteGroupMembership`, constraint renamed, `save()` guards |
| 1.1b | 21 consumers narrowed to `procurement`; `active_group_membership()` takes a required type |
| 1.2a | Capability flags (`is_qaqc`, `is_hse`, `is_warehouse_keeper`), `StockLocation`, `execution_groups_are_never_locked` |
| K5 | Inverse role map consolidated; closed A3 and K5. Found the map is differences-only with passthrough |
| 1.4a | Task dependency models, cycle detection, `incomplete_predecessors()` — reports, never refuses |
| B8 | **Two copies of the 180-line task status function became one.** `_apply_task_status_change()`, R-18 |
| B9 / B10 / B11 | Admin surface closed: `status` read-only on Task and Project, admin pages stopped 500ing, registry-walking guards |
| A-1.3 | **Audit** — OPEX template; three of seven claims wrong, mirror exclusion found to be 12 querysets across 6 files |
| 1.3a | OPEX template seeded as versioned data; `is_mirror` as an indexed boolean |
| 1.3b | Mirrors excluded from 21 counting sites |
| 1.3c | **OPEX execution start** — 91 structurally unactivatable sites can now activate |
| B22 | The mirror write refusal, at rung 0 of the status helper |
| B18/B23/B26 | Auto-scheduling disabled for OPEX; two dead controls fixed; `is_mirror` read-only in admin |
| B21 | Four copies of "current phase" became one, mirrors excluded |
| DEMO-1 | Manifest-based local demo data with a non-local database interlock |
| 1.5 | OPEX template corrected — inspections removed, delivery split four ways, mirror badge, milestone card hidden |
| 1.6 | **Progress and workload separated** — R-20 rewritten as two rules |

**Suite: 1,060 tests.** Regression net is now ten-plus test modules.

**Browser-tested by hand, 02 Sep**, against the demo dataset — the first time any of this was
opened in a browser. Every blocker passed, including the phase 0 access lockdown across six roles
and a full Residential regression sweep.

---

## The central architectural idea: derived mirrors

The OPEX task list is the **site's spine** — one dashboard showing every phase and task. But most
of the work it displays is owned elsewhere: Design in the design workspace, deliveries as
`DeliveryChallan` lines, COD and HOTO as milestones.

Those appear as **derived mirrors**: `is_mirror=True`, **read-only to every human**, status written
only by the authoritative object. The refusal sits at **rung 0** of
`_apply_task_status_change()` — above the transition table and above the inline `due_date` write,
which is the only position from which "a refused mirror move writes nothing" is true.

**OPEX template v1 (live): 7 phases, 23 tasks, 8 mirrors, 15 entered.**

| Phase | Tasks | Mirrors |
|---|---|---|
| 1 Design | 1 | Design |
| 2 Approvals (Pre-Installation) | 2 | — |
| 3 Procurement & Delivery | 4 | all four deliveries |
| 4 Installation | 9 | — |
| 5 Testing & Commissioning | 2 | — |
| 6 Approvals (Post-Installation) | 1 | — |
| 7 Closeout | 4 | COD, As-Built, HOTO |

Spec: `docs/OPEX_task_template_spec.md` v1.5.

---

## What is NOT built — read this before planning anything

**No derivation hook exists. Not one.** All 8 mirrors sit at Not Started permanently.

- **Design** has a live, clean source and no hook. `DESIGN_RELEASED` is set by the Design Head and
  is **not terminal** — `design_change_request_accept` → `_open_next_attempt()` reopens it, a single
  chokepoint, which is what a two-directional mirror needs.
- **The four deliveries** need **both** B-18 (`DCLineItem` has no FK to `BOQItem`) **and** SCM's
  catalogue mapping. Only **52 of 207** OPEX catalogue rows map to the four buckets today.
- **COD, HOTO, As-Built** need phase 5 and phase 3 objects that do not exist.

**Phase 1 and Phase 3 hold only mirrors**, so neither can ever be a site's current phase (R-21) and
both progress bars read 0/n until derivation lands.

**Three things phase 1 built that have no user-facing surface:**

- **`group_type='execution'` cannot be created through any screen.** `site_group_create` hardcodes
  procurement. D-1 is unreachable through the product.
- **`StockLocation`** — one table, no writer, no view, no admin.
- **The three capability flags** — no writer anywhere, not even `UserProfileAdmin`. Shell only.

**SCM and Design own no actionable OPEX task.** Removing the inspections took SCM's only entered
rows. Both have mirrors only.

**No scheduling.** `duration_days = 1` on all 23 template tasks — placeholders. Auto-scheduling is
refused for OPEX in three places. Dates are manual, per task. `compute_gantt_schedule()` falls back
to the duration chain precisely for null-date projects, i.e. every OPEX site, so **the Gantt must
use dates only for OPEX and render no bar for a dateless task.** Recorded against B18.

**91 OPEX sites remain in Draft and none needs activating.** No bulk activation route exists, and
the `status != 'Draft'` guard lives **inside the view** with no database constraint on
`(project, phase_order)` — so a loop that saves clicks would give a site 46 tasks on a rerun
(B24).

---

## The deploy, and the failure that matters most

The 03 Sep merge **took production down.** Railway runs
`migrate --run-syncdb && collectstatic && gunicorn`; migration **0067** failed, so gunicorn never
started.

`seed_task_template_version()` in `utils.py` is shared by migrations 0067 and 0075. Prompt 1.3a
added `is_mirror=` to it for 0075. **0067 runs against the historical `TaskTemplateTask` from state
0067, which has no such field.**

**Why nothing caught it:** `solarpms/test_settings.py` disabled migrations. Its own docstring said
so — *"builds the schema directly from current model state."* **No test in this programme had ever
run a migration.** Every "migrate forward, reverse, forward" verification ran against a local
database where 0067 had been applied for months, so it stepped back one migration and forward
again; 0067 never re-ran.

Fixed by HOTFIX-1 and redeployed successfully. `ALTER ROLE solarpms_user CREATEDB;` was granted, so
the chain is now runnable locally.

**Two things phase 2 must inherit from this:**

1. **A migration must never call live application code whose signature can change.** Migrations run
   against historical models. A shared helper crossing that boundary must tolerate fields that did
   not exist yet.
2. **Every session must run the migration chain before reporting done.** A fast suite that skips
   migrations is how nineteen sessions of green ticks sat on a broken one. Confirm what HOTFIX-1
   settled about `test_settings.py` before writing a phase 2 prompt.

---

## Decisions carried into phase 2

- **R-18** — a new task-status rule goes in `_apply_task_status_change()`, **never in a view**. Five
  features are queued behind this: HSE clearance, QA/QC gate, two-step completion, checklists,
  dependency warnings. Each is now written once.
- **R-20 (rewritten by 1.6)** — **the question a number answers decides whether mirrors are in it.**
  *"How much of this site is done"* includes mirrors (7 PROGRESS sites). *"How much work does this
  person owe"* excludes them (25 WORKLOAD sites). A differential sweep guard enforces both
  directions: a WORKLOAD number must not move when mirrors are added; a PROGRESS number must.
- **R-21** — one implementation of "current phase", in `utils.current_phase()`, mirrors excluded.
- **R-7** — a template version is immutable once active. Correcting the OPEX template now requires a
  **v2 bump**, not an `UPDATE`. Editing 0075 in place was licensed only because it had never
  deployed; **that licence is spent.**
- **B-08 closed** — a dependent task may be started early by **anyone**, with a mandatory reason and
  a warning. No hard block, no role gate. 1.4a built the predicate; **1.4b is not built.**
- **B-12 closed** — the PM alone waives a punch point. No threshold, no second signature.
- **B-14 closed** — warehouses are data, not constants. One keeper per warehouse. **Authority follows
  the warehouse** — a keeper handles whatever is in their building regardless of tender.
- **Q-E1 closed: no** — Residential does not need execution grouping. `SiteGroup.program` stays
  non-nullable.
- **1.2b (`ProjectAssignment`) dropped from phase 1.** Without effective dating it delivers no new
  capability — it is a pure refactor of the access-control substrate that 38 isolation tests pin.
  Do it staged, post-deploy, with a caller audit in front of it. **Check first whether per-site
  scoping for QA/QC and HSE actually needs it**, or whether the task-derived route that already
  scopes Site Engineers extends to the flagged capabilities. If it does, it may never need to exist.
- **1.4b deferred** with task precedences. With no edges authored there is nothing to warn about,
  and it edits the most-used write path in the system. **Its prompt is stale** — it names four write
  sites; B8 made it one.
- **Mirror due dates are settable.** No gate exists to extend. Harmless — mirrors are out of every
  overdue counter — and pinned as current behaviour.
- **Roles.** `'Project Coordinator'` was added to `Task.ROLE_CHOICES`. The role map is
  **differences-only with passthrough**, so a new task role needs no mapping change — and a **bad**
  one stores and resolves silently. One invariant guards it: every value in `Task.ROLE_CHOICES`
  inverse-maps to a member of `UserProfile.ROLE_CHOICES`.

---

## Open questions blocking specific phase 2 prompts

| Ref | Question | Blocks |
|---|---|---|
| **B-11** | The actual HSE mobilisation checklist items — a named list, not a category. Needs the person who owns site safety. | **2.2** |
| **B-09 (partial)** | Task durations per OPEX task. Placeholders of 1 day are live. A correction is a v2 bump. | Gantt, scheduling |
| **Catalogue mapping** | Which of 207 OPEX catalogue items belong to Solar Panels / Inverters / BOS Kit / MMS. 155 map to none. Needs SCM. | the four delivery mirrors |
| **B-18** | Add the `DCLineItem` → `BOQItem` FK. **Confirmed for build, unbuilt.** | 4.1, 4.3, delivery mirrors |
| **§B23** | `_apply_task_status_change()` requires a `request` and **has no non-HTTP home.** Derivation hooks fire from source events, not requests — and per its own docstring **will not call it.** So the transition table, `completed_at` and `blocked_since` get restated somewhere or they diverge. **This is the fifth-representation problem aimed at the mechanism the whole mirror architecture depends on, and the Design hook is where it lands.** | every derivation hook |
| **B20** | The CEO department rollup has no Project Coordinator row. Completion Certificates is counted in the totals and in no department — one task per site, silently under-covering. | CEO dashboard |
| **B28** | **Twelve `PaymentMilestone` rows exist on non-Residential projects and nobody knows what created them.** Spec §2a's reasoning that they cannot exist is sound; the rows exist anyway. Watch whether the number moves. | — |
| B-15 / B-16 / B-17 | Net metering block-or-warn · vendor work order · retention and advances | 5.1, 5.2 |
| Q-E2 / Q-E3 | BD and Design Head scoping · who maintains Finance/SCM assignments | later |

---

## Phase 2 as planned — and what has changed under it

| # | Prompt | Note |
|---|---|---|
| 2.1 | Two-step task completion — engineer submits, PM/QA approves | Lands in `_apply_task_status_change()` per R-18 |
| 2.2 | HSE mobilisation clearance — **once per site**, not per task (D-6) | **Blocked on B-11** |
| 2.3 | QA/QC gate and punch points on `Issue` | The Blocked branch **already auto-creates `Issue` rows**, so "punch point" must be distinguishable from a task blocker — this is why Punch Points was dropped from the template |
| 2.4 | Installation checklists per step, linked by id not name string | |
| 2.5 | **Site engineer screen, with navigation** — first user-visible output | The screen that decides adoption |
| — | **PILOT GATE** — one real OPEX site end to end with the actual site engineer | Nothing in phase 3 or 4 starts first |

**My recommendation on ordering:** the **Design derivation hook** should come before or early in
phase 2. It is the only mirror with a live source, it proves the mechanism on the easiest of the
eight rather than discovering the pattern is wrong when COD depends on it in phase 5, and it forces
a real answer to §B23 — which is the largest unanswered architectural question in the programme.

---

## Lessons that cost something

**Seven phantom facts.** Documents asserted things about the codebase that were false — the
constraint shape (F-1, killed D-1 as written), the caller count of `active_group_membership()`, a
`D-5` reference that never existed, `EXECUTION_PROMPT_LOG.md`'s contents, §13's instrumentation
coverage, `PHASE_0_BROWSER_TEST_PLAN.md` (a file *and* a test number cited for a document nobody
wrote), and **285 payment milestone rows** (an audit projection repeated as a count; the real number
is 12). **Two were introduced by Claude, not inherited.**

**Every factual claim about the codebase must be verified by a session with repo access before
anything is built on it.** The pre-flight stop is what makes that a deliverable rather than a hope,
and it worked every time it was asked for.

**Negative verification is the standard.** The best sessions broke their own guard and showed which
tests failed. B22 neutralised its check and failed 34 assertions across both entry points; B21
neutralised the mirror exclusion and every Residential test still passed, proving the mechanism
cannot touch Residential. **A test that has been shown to pass is not a test that has been shown to
work.**

**Guards must derive their scope from something live.** The registry-walking pattern — deriving the
checked set from `admin.site._registry`, `Task.ROLE_CHOICES`, `StatusTransition`'s subject registry
— has worked five times and survives future additions. A hand-maintained list documents rather than
enforces.

**Consolidate before adding rules.** B8 turned two 180-line copies into one before five features
landed on that path. A rule added to one of two copies is not enforced, merely avoidable — and the
person avoiding it never knows.

**Sessions stepped outside MODE three times, each on good judgment and each disclosed.** That is the
right failure mode, but it is a pattern: name scope narrowly and read the diff anyway.

**The programme's own verification was hollow and nobody checked.** Nineteen prompts asked for
"migrate forward, reverse, forward" and every one was satisfied by a round trip that never re-ran
the migration that was broken.

---

## Opening a fresh chat

Attach or reference: `docs/execution-model.md` (current version), `docs/OPEX_task_template_spec.md`
v1.5, `EXECUTION_PROMPT_LOG.md`, `EXECUTION_MODULE_DEFERRED.md`, `PHASE_0_COMPLETION.md`,
`ACCESS_ISOLATION_AUDIT.md`, `RESIDENTIAL_BASELINE.md`, `BROWSER_TEST_PLAN.md`, `DEPLOY_PLAN.md`,
and this document.

State: the working model above; that phase 1 is complete, merged and deployed; that production is at
migration 0075 with 144 projects, 1,861 tasks and no activated OPEX site that matters; that **no
derivation hook exists**; that **§B23 is unanswered and blocks every one of them**; and that every
factual claim about the codebase must be verified by a session with repo access before being built
on.
