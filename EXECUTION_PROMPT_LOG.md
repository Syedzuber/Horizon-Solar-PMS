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
| 1.1b | **`group_type` — the consumer half.** `active_group_membership()` takes a **required** type argument; all three callers, `_group_or_404`, `_group_rows`, `post_qc_pool`, `site_group_create` and `project_boq_is_group_locked` narrowed to `procurement`. 12 tests, each building an execution membership by hand. **No migration.** Touched `design_views.py`, `permissions.py` — **not `views.py`** (see below). **D-1 is complete.** | ✅ |
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

### 1.1b — two scope notes, recorded because a later session will hit both

**`views.py` was in the row above and out of the prompt's MODE.** This log's own 1.1b row
originally said the session touched `design_views.py`, `views.py` and `permissions.py`; the
1.1b prompt's MODE listed `views.py` as **forbidden**. The audit is right that
`boq_detail`'s `locked_group` banner lookup is an unnarrowed membership read — the prompt's
scope list won, and it is deferred as §B7 rather than fixed. It is cosmetic-only while
`locked` remains a procurement-only status, but it is a real narrowing owed.

**This file has never held a deviation table or an open-questions table.** The 1.1b prompt's
pre-conditions expected `EXECUTION_PROMPT_LOG.md` to contain a deviation table running D-1 to
D-9 and an open-questions table running B-09 to Q-E3, and to stop if it did not. It does not,
and never did — it was created by 1.1a as a session log and nothing more. Both tables live in
`docs/execution-model.md`: the deviations are §2, and there are **four** of them (D-1 … D-4),
not nine — D-5 through D-9 do not exist anywhere in this repository. The open questions are
§8, running Q-E3 and B-05 … B-18. The pre-condition was **waived by the product owner on 29
Aug 2026** on that finding. A later prompt should point at `docs/execution-model.md` §2 and §8
rather than at this file.
