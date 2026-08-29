# Execution Module — Prompt Log

One row per session that actually ran, in the order it ran. This records **what was done
and what it was allowed to touch**, not what was planned; the plan lives in
`docs/execution-model.md`.

> **CREATED 29 Aug 2026 BY PROMPT 1.1a.** The file did not exist before — phase 0's record
> is `PHASE_0_COMPLETION.md`, written at the end of the phase rather than kept as it went.
> The phase 0 rows below are back-filled from that document and from the git history, and
> are a summary of it, not a second source of truth. **Phase 1 onward is written as it
> happens.**

---

## Phase 0 — foundations (closed 29 Aug 2026)

Nine sessions, not six: 0.2 was split three ways once it was underway. Full account in
`PHASE_0_COMPLETION.md`.

| # | Session | Done |
|---|---|---|
| 0.1 | The context pack — `docs/execution-model.md` | ✅ |
| 0.2a | The regression baseline — `RESIDENTIAL_BASELINE.md`, `tests_residential_baseline.py` | ✅ |
| 0.2 | Access isolation — `permissions.py`, `decorators.py`, 19 endpoints | ✅ |
| 0.2b | Consolidation — one BOQ snapshot, one acknowledgement, one M2 map | ✅ |
| 0.2c | One Project resolution path — `_active_project()` (R-16) | ✅ |
| 0.3 | The state ledger — `StatusTransition` + `record_transition()`, six subject types | ✅ |
| 0.4 | Versioned task templates — `TaskTemplate`, RESIDENTIAL v1 (R-7) | ✅ |
| 0.5 | Versioned checklists — `Checklist`, `item_text_snapshot` (R-8) | ✅ |
| 0.6 | Documentation reconciled — `docs/delivery-state-authority.md` | ✅ |

---

## Phase 1 — grouping, assignment and templates

| # | Session | Done |
|---|---|---|
| 1.0 | The regression net goes green — three fixtures reordered to draft, items, `activate()` | ✅ |
| — | **A-1.1 audit** — `SITE_GROUP_AUDIT.md`. Read-only; wrote no code. Found F-1, which is why 1.1 split. | ✅ |
| 1.1a | **`group_type` — the schema half.** Constants, the column on both models, the two `save()` guards, migration `0071_site_group_type`, `tests_site_group_type.py`. **Zero behaviour change.** | ✅ |
| 1.1b | **`group_type` — the consumer half.** Narrow `active_group_membership()` and every caller; the design change-request gate must ask for `procurement` explicitly. Touches `design_views.py`, `views.py`, `permissions.py`. | ☐ |
| 1.2 | Assignment — gated on Q-E3 (`ProgramAssignment` designed, not built) | ☐ |
| 1.3 | The OPEX/CAPEX task template — gated on B-09 | ☐ |
| 1.4 | Task dependencies — gated on B-08 | ☐ |

### Why 1.1 became 1.1a and 1.1b

Planned as one session. Split on 29 Aug 2026, **before either half was written**, on the
A-1.1 audit's finding.

**They have no shared failure mode.** 1.1a's risk is a migration against production data —
it either applies cleanly to every existing row or it does not, and the question that
settles it is a counting query run before it starts. 1.1b's risk is a **silent** one: an
`active_group_membership()` that returns the wrong row makes the design change-request gate
order-dependent, and nothing fails loudly when it does. A migration that works is no
evidence at all about a gate that reads the wrong row.

**They have no shared review question.** 1.1a is reviewed by reading SQL and a constraint
definition. 1.1b is reviewed by enumerating callers and arguing that the enumeration is
complete. Reviewing both in one sitting means doing the second job badly, and the second
job is the one where the audit says the bugs are.

**The ordering is forced, not chosen.** 1.1b cannot narrow a query by `group_type` before
the column exists. 1.1a can ship alone precisely *because* it changes no behaviour: while
no execution membership exists, `(project, group_type)` unique is behaviourally identical
to `(project)` unique, so the consumers stay correct until 1.1b's work makes them wrong.

**1.1a was forbidden from touching `design_views.py`, `views.py` or `permissions.py`, and
did not.** The one thing that could have forced it to — `_add_sites()` creating memberships
via `bulk_create()`, which bypasses `save()` and so bypasses the guard — was checked at
pre-flight. It uses `objects.create()` per row, deliberately and with its own reasons
documented, so the guard covers the only production creation path.
