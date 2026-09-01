# OPEX Execution Task Template — Specification v1.5

Horizon Solar PMS · 1 Sep 2026 · for prompt 1.3

Source: the Tenders team task list, plus the decisions taken 30 Aug. **All decisions are closed.**
This is the input to the 1.3 build and the document the Tenders team signs off on.

**v1.1 — revised after the A-1.3 audit.** Four changes, each because the code contradicted v1.0:
Punch Points dropped (no "punch point" or "blocking" concept exists, and the Blocked branch
auto-creates `Issue` rows, so the mirror would conflate task blockers with the commissioning punch
list — returns at phase 2.3); the six delivery mirrors collapse to one; durations left unset for
the team to decide; all tasks `task_type = Internal`.
**v1.2** — Survey dropped, and with it Phase 1: survey is done before execution begins and is
recorded outside PMS. Activation is manual, triggered by the assigned PM, with no gate.
**22 tasks in 7 phases**, not 29 in 8.
**v1.3** — Completion Certificates is owned by **Project Coordinator**, not "PM / Coordinator".
`Task.assigned_role` cannot store the latter; 1.3a added `'Project Coordinator'` as a valid value.

**v1.4 — after the browser test.** Two changes, both from seeing the screen rather than the spec.
The two **inspections are removed**: a factory or warehouse inspection happens once for a
consignment covering many sites, so putting it on every site dashboard asks many people to record
one event — it belongs to SCM and inventory (phase 4.5). **Material Delivery splits into four**:
Solar Panels, Inverters, BOS Kit, MMS, because material arrival is what a PM looks at first and one
undifferentiated row does not say whether panels have landed or only cable. This reverses v1.1's
collapse to one — the reason for that collapse has not gone away, so **all four read Not Started
until SCM maps the catalogue and the join key exists.** Four rows now is honest structure, not more
information. **23 tasks in 7 phases, 8 mirrors.**

**v1.5 — after verifying v1.4 against the repository.** No decision changes. Every factual claim
about the codebase was checked on branch `execution-phase-1` on 1 Sep 2026, and the claims that
were wrong are corrected here rather than left to be discovered during the build:

- §2a described two activation defects as pending. **Both were fixed by 1.3c**, and not the way
  §2a implied — see §2a.
- "285 Residential milestones already exist on tender sites" was **false**. See §2a.
- "120 of 207 catalogue items map to no named bucket" was v1.2's figure for six buckets. Under
  v1.4's four it is **155**. See Phase 3.
- The counts, the delivery-mirror count and the premortem row arithmetic were **stale in the
  prose** while correct in the tables. Corrected throughout.
- §5 listed **Inspections twice, with opposite decisions.** The v1.4 removal stands; the v1.3 row
  is gone.
- §6's "1.3 seeds this as OPEX template v1" **already happened**, and v1.4's "needs a version bump
  to OPEX v2" **is not true** given where production actually is. See §6.

Every factual claim about the codebase in this document must be verified by a session with repo
access before anything is built on it — six such claims have now turned out wrong in this
programme.

---

## 1. The architecture, in one paragraph

The OPEX task list is the **site's spine**. It shows every phase and task on one dashboard, so a
PM or engineer can see where a site actually is. But most of the work it displays is owned
elsewhere — Design in the design workspace, punch points as `Issue` rows, deliveries as
`DeliveryChallan` lines, COD and HOTO as milestones.

Those tasks appear as **derived mirrors**: read-only to every human, written only by the
authoritative object. A mirror cannot disagree with its source, because nobody can type into it.
Everything else is an **entered** task the assigned person completes normally.

---

## 2. Mirror mechanics — the rules that make this safe

These apply to every mirror task and are the whole reason the design works. Build state is stated
against each rule as verified on 1 Sep 2026.

1. **`is_mirror` is a flag on the template task**, inherited by every `Task` instance created
   from it. **BUILT** — migration 0074; `_attach_task_template()` copies it as the seventh
   snapshot field.
2. **The consolidated status-change function refuses a human status write on a mirror task.** Not
   hidden in the UI — refused in the one place all status writes pass through. This is the single
   most important line in the feature, and it is why the B8 consolidation had to land before the
   template was seeded. **BUILT** — B8 consolidated the two copies, and B22 put the refusal at
   rung 0 of `_apply_task_status_change()`, above the transition table and above the inline
   `due_date` write, so a refused move writes nothing. `tests_mirror_readonly.py` posts a mirror
   status change and asserts the refusal. **Premortem #2 is discharged.**
3. **Mirrors follow their source in both directions.** If a design is reopened, the Design mirror
   returns to In Progress. A mirror that only ever moves forward is an entered task with extra
   steps. **NOT BUILT for any of the 8**, Design included. No derivation hook exists yet, so every
   mirror sits at its seeded status. Phases 3–5 own this.
4. **Every mirror write goes through `record_transition()`**, like any other status change, so the
   ledger stays complete. **Not yet exercised** — nothing writes a mirror. The requirement stands
   for whoever builds the first derivation hook.
5. **Mirrors are excluded from overdue counts and from per-user workload counts.** A mirror is
   nobody's task; counting it against the site engineer blames him for another team's queue.
   **BUILT** — 1.3b routes every task count through `human_owned_tasks_q` / `is_human_owned`.
6. **Mirrors carry visible ageing on the site dashboard** — `Design — In Progress, 41 days`. This
   is what replaces overdue: attributable, precise, and it answers "is this site stuck and where".
   **NOT BUILT, and this is now urgent rather than pending.** No template in the repository reads
   `is_mirror`; `partials/_task_row.html` renders a mirror identically to an entered task, with the
   same status control. Rule 5 has already shipped, so mirrors are out of every counter *and* show
   nothing in place of what was removed. **Premortem #3 is live, not hypothetical.**
7. **Portfolio metrics read the source object, never the mirror.** Design ageing from
   `DesignAssignment`, open punch points from `Issue`, COD from the milestone. The CEO report
   already has this exact gap today — OPEX designers show zero tasks because their work lives in
   `DesignAssignment` — and it is one fix, in the dashboard work, not in the template.
8. **The `StatusTransition` actor on a mirror write is the actor of the source event** — the
   Design Head who released, the SCM user who confirmed the GRN. The transition reason names the
   derivation, so the ledger reads truthfully rather than attributing the write to a system user.

---

## 2a. Execution start

**Activation is manual and ungated.** The assigned PM activates an OPEX site; that transition
attaches this template. There is no precondition — survey is done before execution begins, is
recorded outside PMS, and does not appear as a task.

**Both defects the A-1.3 audit found are fixed, and 1.3c fixed them by adding a route rather than
by editing the existing one.** `project_activate` is unchanged: it still demands
`assigned_design_id` and still mints M1/M2/M3 unconditionally, because 92 characterisation tests
pin it and the Residential path is safer left untouched than guarded. The non-Residential opening
transition is a separate view, `opex_site_activate`, and the Activate button branches on
`project_type`. That view deliberately does **not**:

- **Require `assigned_design_id`.** Design allocation for OPEX lives on
  `DesignAssignment.assigned_to`, not `Project.assigned_design`. This is what made the tender
  sites structurally unactivatable.
- **Mint M1/M2/M3 payment milestones.** M1 On Survey Completion / M2 On Material Supply /
  M3 On Commissioning describe a three-milestone residential contract, not a tender.
- **Call `calculate_due_dates()`.** Every task here carries `duration_days = 1` and is Internal, so
  the chain would make HOTO fall due `activated_at + 23` days and put the portfolio overdue within
  a month. A null due date says "not scheduled"; 23 sequential days says something specific and
  false. Durations are the team's to set (see §5).

**Correction to v1.4.** v1.4 stated that "285 such rows already exist on tender sites" and §5
decided to leave them alone. **No such rows exist.** The 285 figure comes from the A-1.3 audit,
where it was a projection of what activating 95 sites *would* mint — not a count of anything. Both
`PaymentMilestone` creation paths require an activated project and a PM action, and **production
has no active projects**. There is no bad data here to have a decision about.

## 3. The template — 7 phases, 23 tasks

Legend: **M** = derived mirror (read-only) · **E** = entered

### Phase 1 — Design

| Task | | Role | Source / notes |
|---|---|---|---|
| Design | **M** | Design | `DesignAssignment.status`. Not Started until allocated · In Progress from allocation · Done at `DESIGN_RELEASED` · returns to In Progress on reopen. `DESIGN_RELEASED` is not terminal — one reopen route exists, which is what makes rule 3 achievable here. The 11 drawing types stay inside the design workspace and are **not** tasks. |

### Phase 2 — Approvals (Pre-Installation)

| Task | | Role | Source / notes |
|---|---|---|---|
| Net Metering Approval | **E** | PM | No statutory approval record exists until phase 5.1. Entered now, converts to mirror in a later template version. |
| CEIG Approval | **E** | PM | Same. CEIG applies to OPEX/CAPEX only — **but there is no CAPEX template**, and `attach_opex_template()` raises rather than inventing one. If this template is to serve CAPEX, that is a separate seed and a separate decision. |

### Phase 3 — Procurement & Delivery

| Task | | Role | Source / notes |
|---|---|---|---|
| Delivery — Solar Panels | **M** | SCM | `DCLineItem` accepted quantity against the site BOQ, joined via the `BOQItem` FK (B-18). |
| Delivery — Inverters | **M** | SCM | " |
| Delivery — BOS Kit | **M** | SCM | " |
| Delivery — MMS | **M** | SCM | " |

The two inspections that stood here in v1.0–v1.3 are **removed**. An inspection at a vendor's works
covers a consignment, not a site; recording it once per site is wrong. Phase 4.5 owns it.

**Which catalogue items belong to which bucket does not yet exist, and the split makes the gap
wider, not the same.** Of the 207 OPEX catalogue rows (migration 0057, a frozen literal that
asserts its own length), the four buckets above match `Module`, `Inverter`, `BOS` and `MMS` —
**52 rows. 155 map to nothing.** v1.4 carried forward v1.2's figure of 120, which was neither
current nor about these buckets: under v1.2's six buckets, 84 map and 123 do not. There is no
category named RMS under any grouping. **SCM must produce that mapping before any of these four
can derive.** The argument for splitting into four survives this correction — a PM reads material
arrival first — but nobody should believe the mapping is nearly done.

**Derivation rule:** Not Started = no accepted quantity · In Progress = some accepted, below the
site BOQ quantity · Done = accepted ≥ BOQ quantity. Damaged quantity excluded from accepted.

**All four read Not Started until B-18 lands.** `DCLineItem` carries `boq_category` as a plain
string and has no FK to `BOQItem`, so there is nothing to join on. That is deliberate and honest —
visible, and no conversion of live data later. Purchase-order status is out of PMS this phase, so
"PO placed" is not represented; when it arrives it becomes a second signal on the same rows, not
more tasks.

**Phase 3 now holds only mirrors.** Like Phase 1, it can never be a site's current phase (R-21) and
its progress bar reads 0/0 permanently until derivation lands. That is correct and needs to be
legible on screen, not explained in a document.

### Phase 4 — Installation

All **E**, role **Site Engineer**. The source list said "PM, Site Engineers and Project Coordinator as applicable"; `assigned_role` stores one value, and the site engineer is the owner of installation work.

Civil Work and MMS Installation · Module Installation · LA and Earthing Installation · DC Cable
Laying with Conduit · DCDB and ACDB Installation · Inverter Installation · AC Cable Laying · RMS
Installation · Solar Generation Meter Installation

*(9 tasks)*

### Phase 5 — Testing & Commissioning

Punch Points was a mirror in v1.0 and is **dropped**. `Issue` exists per site, but "punch point"
and "blocking" do not, and `_apply_task_status_change()`'s Blocked branch auto-creates `Issue`
rows — so the mirror would show one number conflating task blockers with the commissioning punch
list. It returns at phase 2.3, when punch points become a real concept.

| Task | | Role | Source / notes |
|---|---|---|---|
| Testing & Commissioning | **E** | Site Engineer | |
| Net Meter Installation | **E** | Site Engineer | The physical install. Distinct from the approval in Phase 2 — a locked decision. |

### Phase 6 — Approvals (Post-Installation)

| Task | | Role | Source / notes |
|---|---|---|---|
| Post-Installation Approvals | **E** | PM | One task covering a dynamic set of underlying approvals. Entered until phase 5.1 builds statutory approval records; converts to a mirror then, in the same template version as Phase 2's two. |

### Phase 7 — Closeout

| Task | | Role | Source / notes |
|---|---|---|---|
| COD | **M** | PM | Milestone / COD record (phase 5.3). Refuses while a blocking punch point is open. |
| Completion Certificates (Paperwork) | **E** | Project Coordinator | Coordinators own execution paperwork. |
| As-Built Drawings | **M** | Design | Design workspace. Post-commissioning, so it sits in Closeout rather than Phase 1 despite appearing under Design in the source list. Blocks HOTO. |
| HOTO | **M** | PM | Phase 5.3. Refuses without as-built unless overridden. |

---

## 4. Counts

**23 tasks · 8 mirrors · 15 entered · 7 phases.**

Of the 8 mirrors, **1 has a usable source object today** — Design — and **none has a derivation
hook**, so all 8 read Not Started until phases 3–5 build them. The four delivery mirrors need both
B-18 and SCM's catalogue mapping. COD, HOTO and As-Built derive from objects phase 5 builds.

`is_mirror` is a **boolean**, indexed, not a nullable derivation-source enum. A source enum would
force COD, HOTO and As-Built to hold either invented inert values or NULL, and NULL means writable
by anyone — premortem #1 arriving through the schema on day one. It is indexed on `Task`, where
1.3b filters on it in a dozen querysets, and deliberately not on `TaskTemplateTask`, which holds
under a hundred rows and is only ever read whole. `derivation_source` is added beside it in
phases 3–5 under `source IS NULL OR is_mirror`.

---

## 5. Decisions closed on this spec

| | Decision |
|---|---|
| Mirror model | Derived, not entered. Mirrors are read-only to every human and written only by their source object. |
| Survey | Not a task. Done before execution begins and recorded outside PMS. Phase 1 removed. |
| Pre-installation approvals | Two entered tasks — Net Metering, CEIG. |
| Post-installation approvals | One entered task. Converts to a mirror at phase 5.1, alongside the pre-installation pair. |
| Inspections | **Not site tasks.** Removed in v1.4; SCM and inventory own them (phase 4.5). |
| Deliveries | Four mirrors — Solar Panels, Inverters, BOS Kit, MMS. Re-split or consolidate when the catalogue mapping exists; it is cheap to change before it derives and expensive after. |
| Delivery derivation | Accepted quantity against BOQ quantity, three states as above. Damaged excluded. |
| Mirror transition actor | The source event's actor, with the reason naming the derivation. |
| Overdue | Mirrors excluded from overdue and from per-user workload. Ageing shown instead — **and the ageing half is not built.** |
| Precedences | Deferred to the next update, together with 1.4b. |
| Purchase orders | Out of PMS this phase. |
| Payment milestones | Not applicable to OPEX. Activation no longer creates them. **The card must not render on an OPEX site — still unbuilt**, `project_overview.html` gates it on role only. |
| Punch Points | Dropped from v1. Returns at phase 2.3. |
| Durations | Unset. Every task carries `duration_days = 1` and nothing calls `calculate_due_dates()` for OPEX. The team decides per task later, as a template version bump. |
| Task type | All 23 are `Internal`. |
| Mirror flag | Boolean `is_mirror`, indexed on `Task`. Not a nullable source enum. |
| Activation | Manual, triggered by the assigned PM, ungated, through `opex_site_activate`. No `assigned_design_id` requirement; no Residential milestones; no due-date chain. |
| Existing bad data | **None.** The 285 milestone rows v1.4 described do not exist — see §2a. |

Nothing on this spec is open.

## 6. What this changes in the build queue

**Where the code actually is, verified 1 Sep 2026 on branch `execution-phase-1`:**

| | State |
|---|---|
| B8 consolidation | **Landed.** One status decision, shared by both views. |
| Mirror read-only refusal (B22) | **Landed**, at rung 0 of `_apply_task_status_change()`, with a test that posts and asserts refusal. |
| Mirror counter exclusion (1.3b) | **Landed**, through one chokepoint. |
| OPEX template seed | **Landed as migration 0075** — but as the v1.3 table: 7 phases, 22 tasks, 5 mirrors. |
| OPEX activation route (1.3c) | **Landed** as `opex_site_activate`. |
| Mirror ageing | **Not built.** Nothing renders `is_mirror` anywhere. |
| Any mirror derivation hook | **Not built**, including Design. |
| Payment-milestone card gating | **Not built.** |
| B-18 (`DCLineItem` → `BOQItem`) | **Not built.** |

- **B-18 blocks the four delivery mirrors** from ever leaving Not Started. Confirmed for build.
- **Ageing (rule 6) is the next thing that should ship**, not a phase-2.5 nicety. The exclusion it
  was paired with has already shipped without it.
- **1.4b defers** with the precedences.
- **Correct migration 0075 in place; do not bump to OPEX v2.** v1.4 asked for a version bump, on
  the assumption that OPEX v1 is live and frozen by R-7. It is not. `origin/main` is at migration
  **0064**, this branch is 29 commits ahead, and **0074, 0075 and the whole execution phase have
  never been deployed** — production has never held an OPEX template, an OPEX task, or an active
  project of any type. Seeding a v2 would archive a v1 that no site was ever attached to: a
  version record of something that never existed. Editing 0075's seed literal to the table in §3
  lets production see this template once, correct, the first time it runs.

  The condition is that no database except a developer machine has applied 0075. That holds today,
  and 0075 ships a working reverse (`unseed_opex_v1`), so a local re-seed is
  `migrate projects 0074` then `migrate` — not manual surgery. **If that condition ever stops
  holding, the answer flips back to a v2 bump and R-7 decides it, not convenience.**

  Correcting the template after deployment costs a migration and a version bump, so the list in §3
  is worth arguing about now rather than in October.

- **A note on precedent.** §6 previously said 1.3 seeds this "the way the 52-task Residential
  template was migrated in by 0.4". That is a sound engineering precedent — 0067 is idempotent,
  uses the shared seed helper and ships a reverse — but it has not reached production either.
  Nobody should read it as "production already works this way."

---

## 7. Premortem

**Most likely.** Within two months, phase 4 has slipped, the four delivery mirrors still read Not
Started, and somebody asks for them to be tickable "just for now". The request will be entirely
reasonable and granting it silently converts the architecture back to the entered version with no
decision ever taken. **Decide the answer now:** the answer is no, and the fix is to ship B-18.

**Second — DISCHARGED.** The risk was that the mirror read-only refusal gets implemented in the UI
— hidden dropdowns — rather than in the consolidated status function, looking identical in testing
and failing the first time someone posts directly. It did not happen: B22 put the refusal in the
function, at rung 0, and `tests_mirror_readonly.py` posts a mirror status change and asserts it.
The rule now is to keep it there — no second copy in a template, no UI-only variant.

**Third — LIVE NOW, not a risk.** Ageing was deferred as display work while the exclusion it was
paired with shipped in 1.3b. So today a Design mirror stuck for 41 days looks exactly like one
stuck for 2, and both are invisible to every counter. This is the state the premortem warned
about, and the predicted reaction — "put mirrors back into overdue" — is the wrong fix. Ageing
ships with the site dashboard, or rule 5 should be reconsidered as a whole.

**Fourth, and the one I'd watch longest.** 23 tasks per site across ~95 sites is roughly 2,200 rows
the day this attaches, on a system where nobody is currently logging in. If the site dashboard is
slow or the list is unreadable on a phone, the tasks will be ignored and updated over WhatsApp,
exactly as today. The template is necessary and not sufficient; the thing that decides adoption is
the screen in phase 2.5, not this list.

**Fifth, new in v1.5.** The gap between this branch and production is now 29 commits and 11
migrations, and everything above is verified only on the branch. The longer that gap stays open,
the more of this document describes a system nobody is running — and the deployment itself becomes
a single large event rather than a series of small ones. That is a delivery risk, not a design
risk, and it is the one thing on this page that gets worse purely by waiting.
