# OPEX Execution Task Template — Specification v1.0

Horizon Solar PMS · 30 Aug 2026 · for prompt 1.3

Source: the Tenders team task list, plus the decisions taken 30 Aug. **All decisions are closed.**
This is the input to the 1.3 audit and build, and the document the Tenders team signs off on. Every factual claim about the codebase in it must be verified by a
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

## 3. The template — 8 phases, 29 tasks

Legend: **M** = derived mirror (read-only) · **E** = entered

### Phase 1 — Survey

| Task | | Role | Source / notes |
|---|---|---|---|
| Survey | **E** | Site Engineer | Recorded manually by decision — no survey subsystem exists and none is planned near-term. |

### Phase 2 — Design

| Task | | Role | Source / notes |
|---|---|---|---|
| Design | **M** | Design | `DesignAssignment.status`. Not Started until allocated · In Progress from allocation · Done at `DESIGN_RELEASED` · returns to In Progress on reopen. The 11 drawing types stay inside the design workspace and are **not** tasks. |

### Phase 3 — Approvals (Pre-Installation)

| Task | | Role | Source / notes |
|---|---|---|---|
| Net Metering Approval | **E** | PM | No statutory approval record exists until phase 5.1. Entered now, converts to mirror in a later template version. |
| CEIG Approval | **E** | PM | Same. CEIG applies to OPEX/CAPEX only. |

### Phase 4 — Procurement & Delivery

| Task | | Role | Source / notes |
|---|---|---|---|
| Inspection — Factory / Vendor | **E** | SCM | No inspection record until phase 4.5. Entered now, converts to a mirror then. |
| Inspection — Post-Delivery / Unloading | **E** | SCM | Same. |
| Delivery — MMS | **M** | SCM | `DCLineItem` accepted quantity against the site BOQ, joined via the `BOQItem` FK (B-18). |
| Delivery — Module | **M** | SCM | " |
| Delivery — Energy Meter (Solar Generation Meter & CT) | **M** | SCM | " |
| Delivery — BOS Materials | **M** | SCM | " |
| Delivery — DCDB, ACDB and Inverter | **M** | SCM | " |
| Delivery — RMS | **M** | SCM | " |

**Derivation rule for the six:** Not Started = no accepted quantity ·
In Progress = some accepted, below the BOQ quantity · Done = accepted ≥ BOQ quantity. Damaged
quantity excluded from accepted.

**These read Not Started until B-18 lands.** That is deliberate and honest — visible, and no
conversion of live data later. Purchase-order status is out of PMS this phase, so "PO placed" is
not represented; when it arrives it becomes a second signal on the same six rows, not six more
tasks.

### Phase 5 — Installation

All **E**, role **Site Engineer** (PM / Coordinator as applicable).

Civil Work and MMS Installation · Module Installation · LA and Earthing Installation · DC Cable
Laying with Conduit · DCDB and ACDB Installation · Inverter Installation · AC Cable Laying · RMS
Installation · Solar Generation Meter Installation

*(9 tasks)*

### Phase 6 — Testing & Commissioning

| Task | | Role | Source / notes |
|---|---|---|---|
| Testing & Commissioning | **E** | Site Engineer | |
| Punch Points | **M** | — | `Issue` rows on the site. Not Started = none raised · In Progress = one or more open · Done = all closed. **Never "Done" by default** — a site with no punch points raised shows Not Started, not complete. |
| Net Meter Installation | **E** | Site Engineer | The physical install. Distinct from the approval in Phase 3 — a locked decision. |

### Phase 7 — Approvals (Post-Installation)

| Task | | Role | Source / notes |
|---|---|---|---|
| Post-Installation Approvals | **E** | PM | One task covering a dynamic set of underlying approvals. Entered until phase 5.1 builds statutory approval records; converts to a mirror then, in the same template version as Phase 3's two.

### Phase 8 — Closeout

| Task | | Role | Source / notes |
|---|---|---|---|
| COD | **M** | PM | Milestone / COD record (phase 5.3). Refuses while a blocking punch point is open. |
| Completion Certificates (Paperwork) | **E** | PM / Coordinator | |
| As-Built Drawings | **M** | Design | Design workspace. Post-commissioning, so it sits in Closeout rather than Phase 2 despite appearing under Design in the source list. Blocks HOTO. |
| HOTO | **M** | PM | Phase 5.3. Refuses without as-built unless overridden. |

---

## 4. Counts

29 tasks · 11 mirrors · 18 entered · 8 phases.

Of the 11 mirrors, **8 have a live source today or once B-18 lands** (Design, Punch Points, and
the six deliveries). COD, HOTO and As-Built derive from objects phase 5 builds and read Not
Started until then.

---

## 5. Decisions closed on this spec

| | Decision |
|---|---|
| Mirror model | Derived, not entered. Mirrors are read-only to every human and written only by their source object. |
| Survey | Entered, owned by Site Engineer. Manual until a survey subsystem exists; none planned near-term. |
| Pre-installation approvals | Two entered tasks — Net Metering, CEIG. |
| Post-installation approvals | One entered task. Converts to a mirror at phase 5.1, alongside the pre-installation pair. |
| Inspections | Two entered tasks. Convert to mirrors at phase 4.5. |
| Delivery derivation | Accepted quantity against BOQ quantity, three states as above. Damaged excluded. |
| Mirror transition actor | The source event's actor, with the reason naming the derivation. |
| Overdue | Mirrors excluded from overdue and from per-user workload. Ageing shown instead. |
| Precedences | Deferred to the next update, together with 1.4b. |
| Purchase orders | Out of PMS this phase. |

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

**Fourth, and the one I'd watch longest.** 29 tasks per site across 95 sites is 2,755 rows the day
1.3 runs, on a system where nobody is currently logging in. If the site dashboard is slow or the
list is unreadable on a phone, the tasks will be ignored and updated over WhatsApp, exactly as
today. The template is necessary and not sufficient; the thing that decides adoption is the screen
in phase 2.5, not this list.
