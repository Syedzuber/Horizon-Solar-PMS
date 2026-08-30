# OPEX Execution Task Template — Specification v1.2

Horizon Solar PMS · 30 Aug 2026 · for prompt 1.3

Source: the Tenders team task list, plus the decisions taken 30 Aug. **All decisions are closed.**
This is the input to the 1.3 build and the document the Tenders team signs off on.

**v1.1 — revised after the A-1.3 audit.** Four changes, each because the code contradicted v1.0:
Punch Points dropped (no "punch point" or "blocking" concept exists, and the Blocked branch
auto-creates `Issue` rows, so the mirror would conflate task blockers with the commissioning punch
list — returns at phase 2.3); the six delivery mirrors collapse to one (120 of 207 catalogue items
map to no named bucket and there is no RMS category — expands to six when SCM maps the catalogue);
durations left unset for the team to decide; all 29 tasks `task_type = Internal`.
**v1.2** — Survey dropped, and with it Phase 1: survey is done before execution begins and is
recorded outside PMS. Activation is manual, triggered by the assigned PM, with no gate.
**22 tasks in 7 phases**, not 29 in 8. Every factual claim about the codebase in it must be verified by a
session with repo access before anything is built on it — five such claims have already turned out
wrong in this programme.

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

These apply to every mirror task and are the whole reason the design works.

1. **`is_mirror` is a flag on the template task**, inherited by every `Task` instance created
   from it.
2. **The consolidated status-change function refuses a human status write on a mirror task.** Not
   hidden in the UI — refused in the one place all status writes pass through. This is the single
   most important line in the feature, and it is why the B8 consolidation must land before the
   template is seeded.
3. **Mirrors follow their source in both directions.** If a design is reopened, the Design mirror
   returns to In Progress. A mirror that only ever moves forward is an entered task with extra
   steps.
4. **Every mirror write goes through `record_transition()`**, like any other status change, so the
   ledger stays complete.
5. **Mirrors are excluded from overdue counts and from per-user workload counts.** A mirror is
   nobody's task; counting it against the site engineer blames him for another team's queue.
6. **Mirrors carry visible ageing on the site dashboard** — `Design — In Progress, 41 days`. This
   is what replaces overdue: attributable, precise, and it answers "is this site stuck and where".
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

Two things the A-1.3 audit found, both fixed as part of the same transition:

- `project_activate` currently **refuses an OPEX site without `assigned_design_id`**. Design
  allocation actually lives on `DesignAssignment.assigned_to`, so only 5 of 96 OPEX sites carry
  that FK and 91 are structurally unactivatable. OPEX activation must not require it.
- Activation **mints M1/M2/M3 Residential payment milestones unconditionally.** 285 such rows
  already exist on tender sites. Residential milestones do not apply to tenders; OPEX activation
  must not create them. The 285 existing rows are left alone by decision.

## 3. The template — 7 phases, 22 tasks

Legend: **M** = derived mirror (read-only) · **E** = entered

### Phase 1 — Design

| Task | | Role | Source / notes |
|---|---|---|---|
| Design | **M** | Design | `DesignAssignment.status`. Not Started until allocated · In Progress from allocation · Done at `DESIGN_RELEASED` · returns to In Progress on reopen. The 11 drawing types stay inside the design workspace and are **not** tasks. |

### Phase 2 — Approvals (Pre-Installation)

| Task | | Role | Source / notes |
|---|---|---|---|
| Net Metering Approval | **E** | PM | No statutory approval record exists until phase 5.1. Entered now, converts to mirror in a later template version. |
| CEIG Approval | **E** | PM | Same. CEIG applies to OPEX/CAPEX only. |

### Phase 3 — Procurement & Delivery

| Task | | Role | Source / notes |
|---|---|---|---|
| Inspection — Factory / Vendor | **E** | SCM | No inspection record until phase 4.5. Entered now, converts to a mirror then. |
| Inspection — Post-Delivery / Unloading | **E** | SCM | Same. |
| Material Delivery | **M** | SCM | `DCLineItem` accepted quantity against the site BOQ, joined via the `BOQItem` FK (B-18). **One task covering all materials.** Expands to six — MMS, Module, Energy Meter, BOS, DCDB/ACDB/Inverter, RMS — once SCM maps all 207 OPEX catalogue items to those buckets. 120 currently map to none, and no category is named RMS. |

**Derivation rule:** Not Started = no accepted quantity · In Progress = some accepted, below the
site BOQ quantity · Done = accepted ≥ BOQ quantity. Damaged quantity excluded from accepted.

**This reads Not Started until B-18 lands.** That is deliberate and honest — visible, and no
conversion of live data later. Purchase-order status is out of PMS this phase, so "PO placed" is
not represented; when it arrives it becomes a second signal on the same row, not more tasks.

### Phase 4 — Installation

All **E**, role **Site Engineer** (PM / Coordinator as applicable).

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
| Post-Installation Approvals | **E** | PM | One task covering a dynamic set of underlying approvals. Entered until phase 5.1 builds statutory approval records; converts to a mirror then, in the same template version as Phase 2's two.

### Phase 7 — Closeout

| Task | | Role | Source / notes |
|---|---|---|---|
| COD | **M** | PM | Milestone / COD record (phase 5.3). Refuses while a blocking punch point is open. |
| Completion Certificates (Paperwork) | **E** | PM / Coordinator | |
| As-Built Drawings | **M** | Design | Design workspace. Post-commissioning, so it sits in Closeout rather than Phase 1 despite appearing under Design in the source list. Blocks HOTO. |
| HOTO | **M** | PM | Phase 5.3. Refuses without as-built unless overridden. |

---

## 4. Counts

22 tasks · 5 mirrors · 17 entered · 7 phases.

Of the 5 mirrors, **2 have a usable source** — Design today, Material Delivery once B-18 lands.
COD, HOTO and As-Built derive from objects phase 5 builds and read Not Started until then.

`is_mirror` is a **boolean**, indexed, not a nullable derivation-source enum. A source enum would
force COD, HOTO and As-Built to hold either invented inert values or NULL, and NULL means writable
by anyone — premortem #1 arriving through the schema on day one. `derivation_source` is added
beside it in phases 3–5 under `source IS NULL OR is_mirror`.

---

## 5. Decisions closed on this spec

| | Decision |
|---|---|
| Mirror model | Derived, not entered. Mirrors are read-only to every human and written only by their source object. |
| Survey | Not a task. Done before execution begins and recorded outside PMS. Phase 1 removed. |
| Pre-installation approvals | Two entered tasks — Net Metering, CEIG. |
| Post-installation approvals | One entered task. Converts to a mirror at phase 5.1, alongside the pre-installation pair. |
| Inspections | Two entered tasks. Convert to mirrors at phase 4.5. |
| Delivery derivation | Accepted quantity against BOQ quantity, three states as above. Damaged excluded. |
| Mirror transition actor | The source event's actor, with the reason naming the derivation. |
| Overdue | Mirrors excluded from overdue and from per-user workload. Ageing shown instead. |
| Precedences | Deferred to the next update, together with 1.4b. |
| Purchase orders | Out of PMS this phase. |
| Punch Points | Dropped from v1. Returns at phase 2.3. |
| Deliveries | One Material Delivery mirror. Expands to six when SCM maps the catalogue. |
| Durations | Unset in v1. The team decides per task later. |
| Task type | All 22 are `Internal`. |
| Mirror flag | Boolean `is_mirror`, indexed. Not a nullable source enum. |
| Activation | Manual, triggered by the assigned PM, ungated. No `assigned_design_id` requirement; no Residential milestones. |
| Existing bad data | The 285 Residential milestones on tender sites are left as they are. |

Nothing on this spec is open.

## 6. What this changes in the build queue

- **B8 consolidation is now load-bearing**, not hygiene. Mirror read-only enforcement lives in the
  consolidated function. It must land before the template is seeded.
- **B-18 blocks the six delivery mirrors** from ever leaving Not Started. Confirmed for build.
- **1.4b defers** with the precedences.
- **1.3 seeds this as OPEX template v1 by migration**, the way the 52-task Residential template
  was migrated in by 0.4. No authoring UI this phase — which means correcting the template later
  costs a migration, so the list above is worth arguing about now rather than in October.

---

## 7. Premortem

**Most likely.** Within two months, phase 4 has slipped, the six delivery mirrors still read Not
Started, and somebody asks for them to be tickable "just for now". The request will be entirely
reasonable and granting it silently converts the architecture back to the entered version with no
decision ever taken. **Decide the answer now:** the answer is no, and the fix is to ship B-18.

**Second.** The mirror read-only refusal gets implemented in the UI — hidden dropdowns — rather
than in the consolidated status function. It looks identical in testing. It fails the first time
someone posts directly, or the first time a second screen renders the same task. The refusal must
be in the function, and there must be a test that posts a mirror status change and asserts refusal.

**Third.** Ageing gets deferred as "nice to have" because it is display work. Without it, mirrors
are excluded from overdue and show nothing else — so a Design mirror stuck for 41 days looks
exactly like one stuck for 2. That would make the exclusion decision look wrong when it isn't, and
the likely reaction is to put mirrors back into overdue. Ageing ships with the site dashboard or
the exclusion should not ship at all.

**Fourth, and the one I'd watch longest.** 22 tasks per site across 95 sites is 2,090 rows the day
1.3 runs, on a system where nobody is currently logging in. If the site dashboard is slow or the
list is unreadable on a phone, the tasks will be ignored and updated over WhatsApp, exactly as
today. The template is necessary and not sufficient; the thing that decides adoption is the screen
in phase 2.5, not this list.
