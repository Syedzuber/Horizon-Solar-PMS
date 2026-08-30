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
| 1.2a | **Capability flags, warehouses and the execution lock.** `is_qaqc` / `is_hse` / `is_warehouse_keeper` on `UserProfile` (R-15); `StockLocation` (B-14 closed); `execution_groups_are_never_locked` on `SiteGroup`; migration `0072`. 23 tests. **Entirely additive — no behaviour change, no view touched.** Rewrote one 1.1b test whose fixture the constraint made unwritable. | ✅ |
| 1.2b | **`ProjectAssignment` — BLOCKED on a product-owner decision.** Not started. See below | ☐ |
| — | **A-1.3 audit** — `OPEX_TEMPLATE_AUDIT.md`. Read-only; wrote no code. **Three of the seven claims in its own prompt were wrong**, and it found that the OPEX spec could not be built as written — which is why 1.3 split into three. | ✅ |
| 1.3a | **The OPEX template as data, and `is_mirror`.** `OPEX` v1 seeded by migration `0075` — 7 phases, 22 tasks, 5 mirrors, active. `is_mirror` boolean on `TaskTemplateTask` and on `Task` (indexed on `Task`), and `'Project Coordinator'` added to `Task.ROLE_CHOICES`; migration `0074`. One line added to `seed_task_template_version()` so the shared helper carries the flag. 19 tests. **Nothing attaches the template and nothing reads `is_mirror`** — no view, template, counter or activation path touched. | ✅ |
| 1.3b | **Mirror exclusion across the task counters.** The 20 counters the A-1.3 audit enumerated — 12 querysets across 6 files. **MUST land before 1.3c.** Also closes the Coordinator gap: the CEO dashboard has six `dept_*` blocks and no Coordinator one. **No migration.** | ☐ |
| 1.3c | **OPEX execution start.** Manual PM activation; drop `project_activate`'s `assigned_design_id` refusal (design allocation lives on `DesignAssignment.assigned_to`, so **only 5 of 96 OPEX sites carry that FK and 91 are structurally unactivatable**); stop minting M1/M2/M3 Residential milestones on tender sites (285 such rows already exist and are left alone by decision); attach the template. Adds the **R-18 mirror refusal in `_apply_task_status_change()`** — the helper, never a view. **Two traps recorded for it:** both status views refuse an *unassigned* task **before** the helper runs, so a refusal test must use an owned mirror or it passes without proving anything; and `calculate_due_dates()` on 22 Internal tasks at the default duration makes every site a 22-day project (§B18). | ☐ |
| 1.4a | **Task dependencies — the model half.** `TaskTemplateTaskDependency` and `TaskDependency`, `DependencyCycle`, `incomplete_predecessors()` and `materialise_task_dependencies()` in the new `projects/task_dependencies.py`, migration `0073`. 42 tests. **Entirely additive — no view, no template, no permission file, no status-change behaviour, and nothing that can prevent a task from being started.** Closes B-08 at the model layer. | ✅ |
| 1.4b | **Task dependencies — the enforcement half.** Wires the early-start **warning** and the **mandatory reason** into the status-change path, and records the override as a `StatusTransition` with a remark under a new `REASON_EARLY_START`. **No migration** — a `REASON_*` constant is module-level by R-10, and `SUBJECT_TASK` already exists. **The first user-visible change in this programme**, on the most-used write path in the product. **The write paths it must cover — enumerated by 1.4a's pre-flight, so this session does not rediscover them:**<br>① `views.py:task_status_update` — blocked branch (`update()` at ~3840)<br>② `views.py:task_status_update` — ordinary ladder (~3884)<br>③ `views.py:task_detail_status_update` — blocked branch (~4078)<br>④ `views.py:task_detail_status_update` — ordinary ladder (~4118)<br>⑤ `views.py:milestone_receive` — milestone→task sync (~6569)<br>⑥ `views.py:project_overview` — Finance `update_milestone` branch, milestone→task sync (~7142)<br>**①–④ are two near-identical copies of one ~180-line function** (§B8). The warning must land in **both**, or a user routes around it by using the other screen. **⑤ and ⑥ are not a user starting a task** — they are an automatic sync wrapped in `except Exception: pass`, and a mandatory reason must **not** be imposed on them. **A seventh path exists and is uninstrumented: the Django admin's `TaskAdmin` change form** (§B9) — out of 1.4b's scope, but it is why "every task status write is instrumented" is not quite true today. | ☐ |

## Deferred-item prompts — run out of sequence, from `EXECUTION_MODULE_DEFERRED.md`

These are not phase sessions. Each closes one entry that a phase prompt found and was
forbidden to fix (R-12), and each is numbered by the entry it closes.

| # | Session | Done |
|---|---|---|
| B9 | **`TaskAdmin` stops writing task status past the ledger.** `readonly_fields = ['status']` with a DO-NOT-REMOVE comment; `AdminCannotWriteTaskStatusTests`. Closed §B9, opened §B10. | ✅ |
| B10 | **`ProjectAdmin` — the identical hole, plus the worse half.** Activation is a view-layer action, so typing 'Active' into the admin form left a project Active and empty, a state the product cannot produce. `AdminCannotWriteProjectStatusTests` and a registry walk over every instrumented subject. Closed §B10, opened §B11. | ✅ |
| B8 | **One task status-change path.** `_apply_task_status_change()` extracted from two near-identical ~180-line copies; **R-18** added. `views.py` 11,417 → 11,288. 49 new tests, the contract half run through **both** entry points. Closed §B8, opened §B12–§B16. **No rule added, removed or altered** — the four behavioural differences between the copies were preserved and pinned, not resolved. | ✅ |
| B11 | **Both admin project pages were 500ing, and nothing could tell you.** `DocumentInline.fields` named three columns `ProjectDocument` has never had. `manage.py check` reported no issues and structurally cannot — an unknown `fields` entry is presumed form-contributed — so the defect survived with every signal green. Inline corrected to real names and made **read-only**: the row is a pointer into a Supabase bucket the admin cannot write. The durable half is `EveryRegisteredAdminPageLoadsTests`, which GETs the changelist and add form of **every** model in `admin.site._registry`. Closed §B11. | ✅ |
| K5 | **One profile↔task role mapping, and Project Coordinator wired through it.** The two local `_TASK_TO_PROFILE_ROLE` copies became one module-level constant **derived** from `_PROFILE_TO_TASK_ROLE`; **R-19** added. 16 tests, four of them structural. Closed §A3 and `DESIGN_MODULE_DEFERRED` §K5 — the latter half-closed since 0.2b. **No migration, no model file, no `permissions.py` edit, no behaviour change.** Two findings: **Project Coordinator needed no map entry** (differences-only map, identical strings, `.get(x, x)` passthrough), and **the forward map is unreachable for that role on the status path** — `user_can_view_project()` and `user_can_manage_project()` are the same predicate for a coordinator, and the 0.2 scope lockdown runs first. The `assigned_role` match-site enumeration in the prompt said four; **there are eight in Python plus five in templates.** | ✅ |

---

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

### Why 1.2 became 1.2a and 1.2b

Split on 30 Aug 2026, **before either half was written**, and for a different reason than
1.1's. 1.1 split on a technical finding. **1.2 split because one of its four pieces needs
an answer from the product owner that no session can supply.**

1.2 as scoped carried four things: the three capability flags, `StockLocation`, the
execution-lock constraint, and **`ProjectAssignment`**. The first three are additive,
independent of each other, and answer questions that were already settled — R-15 (24 Aug),
B-14, and D-1's procurement-only lock. They shipped as 1.2a.

**`ProjectAssignment` is held because it would be a SECOND representation of project
assignment.** `Project` already carries `assigned_pm`, `assigned_design` and a
`coordinators` M2M, and every scoping helper in `permissions.py` reads them —
`user_can_manage_project()` and `manageable_projects_q()` are built on exactly those three
terms. A `ProjectAssignment` table either **supersedes** them, in which case this is a
migration plus a rewrite of the access layer and every dashboard queryset that scopes on a
PM, or it **sits beside** them, in which case two tables answer "who is on this project"
and the product acquires the drift D-3 exists to forbid.

**That is a policy question, not an implementation detail, and either answer is defensible
— which is exactly why a session must not pick one.** Building it the wrong way is not a
bug that surfaces in review; it is a second source of truth that looks correct until the
two disagree in production.

**Nothing in 1.2a anticipates it.** No assignment model, no join table, no nullable FK
left "for later", and no permission helper written against a call site that does not exist
(R-12). The three capability flags are deliberately **not** an assignment mechanism: they
say what a person may be, never which projects they are on. If `ProjectAssignment` later
supersedes the three existing FKs, 1.2a's work is unaffected — it touches none of them.

**Related and still open:** Q-E3 (do Finance and SCM get Program-level assignment?) and
`ProgramAssignment`, designed in `ACCESS_ISOLATION_AUDIT.md` Task E and deliberately not
built. These are the same question one level up, and whoever answers 1.2b should answer
them together rather than one at a time.

### 1.2a — three notes

**One existing test was rewritten, and it was the right kind of breakage.**
`tests_site_group_type.ConsumerNarrowingTests.test_an_execution_group_never_locks_the_boq`
(formerly `..._whatever_its_status`) forced an execution group to `locked` to prove
`project_boq_is_group_locked()` ignored it. `execution_groups_are_never_locked` makes that
row unwritable, so the test died in its own fixture rather than in its assertion. The fear
it was written against is **retired, not unproven**: what used to be a predicate coping
with a bad row is now a row the database refuses. The constraint itself is pinned in the
new `ExecutionGroupsAreNeverLockedTests`.

**"D-5" does not exist.** The 1.2a prompt cited the flags-not-roles decision as D-5. This
file already records that D-5…D-9 appear nowhere in the repository, and they still do not.
The decision is real and is **R-15**, restated in `docs/execution-model.md` §4, which had
already specified all three field names exactly as built. Nothing was written under a D-5
heading.

**The migration was hand-adjusted in one respect, and it is stated in the file.**
`makemigrations` emitted four operations, splitting `StockLocation.keeper` into an
`AddField` after the `CreateModel` — its habit with a created model's outbound FKs. It was
folded back in: `projects.userprofile` predates `0072` by seventy revisions, so no circular
dependency forced the split. `makemigrations --check` is clean afterwards and the migration
reverses and re-applies.

### Why 1.3 became 1.3a, 1.3b and 1.3c — and why the order is fixed

1.1 split on a technical finding. 1.2 split because one piece needed a product-owner
decision. **1.3 split on a sequencing hazard**: the three pieces are not independent, and
running them in the wrong order corrupts numbers that people have already looked at.

- **1.3a — the template exists as data; `is_mirror` exists as a column.** Nothing uses
  either. One schema migration, one seed migration, no view touched.
- **1.3b — mirror exclusion across the 20 task counters** the A-1.3 audit enumerated:
  12 querysets, 6 files, no migration.
- **1.3c — OPEX execution start**: manual PM activation, the `assigned_design_id` refusal
  dropped, Residential milestones no longer minted on tender sites, the template attached,
  and the R-18 mirror refusal added to `_apply_task_status_change()`.

**1.3b must land before 1.3c, and that is the whole reason for the middle session.** A
mirror is nobody's task — it is another team's queue, displayed on this site's spine. The
first activated OPEX site puts 22 rows into every counter that has not yet learned to
exclude the 5 mirrors among them. Worse, `Project.activated_at` **is** the definition of
"active" for both the CEO per-user report (`reports._active_project_filter()`) and
`send_eod_digest`, so **95 tender sites join two portfolio-wide surfaces on the day 1.3c
runs**, regardless of anything else that session does. Shipping 1.3c first means the
exclusion arrives as a correction to numbers already in circulation, and the audit's
prediction is that the correction gets argued with rather than accepted.

**A second reason the split is real, not bookkeeping.** The A-1.3 audit found the spec
could not be built as written — Punch Points had no source concept, the six delivery
mirrors had no join and no category mapping, and two assigned roles were unstorable. The
spec was amended to v1.2 (and one unstorable role still survived that revision — §B17).
Separating "write down what the template is" from "start running sites on it" is what made
that discoverable before 95 sites were built from it.

### 1.3a — three notes

**Nothing attaches the template, and that is the deliverable, not a shortfall.**
`project_activate` still refuses OPEX sites, still mints Residential milestones, and still
resolves only `RESIDENTIAL`. `is_mirror` is written on 5 rows and read by nothing. A
session that finds itself editing a counter or an activation path is in 1.3b or 1.3c.

**`projects/utils.py` was on 1.3a's forbidden list, and one line was authorised into it.**
`seed_task_template_version()` builds each task row from a fixed field list and had no
`is_mirror` passthrough. The alternatives were worse: a post-activation
`QuerySet.update()` — which works only because `update()` **bypasses** the R-7 guard, and
would have made the migration that introduces the field also the first place in the
codebase to author template content past that guard — or an OPEX-only copy of the seeder,
abandoning the "one shared helper so the two paths cannot drift" property the helper's own
docstring exists to state. The line added is `is_mirror=t.get('is_mirror', False)`,
optional exactly like `is_payment_milestone` beside it, so Residential's phase dicts seed
`False` unchanged.

**The OPEX phase data lives in the migration, not beside `build_residential_phases()`.**
That looks like a departure from 0067 and is not: 0067's own header says
`build_residential_phases()` *"IS the thing being migrated, and after this runs it stops
being executed at runtime"*. There was no pre-existing OPEX builder to migrate out of
runtime source and nothing at runtime needs one. `projects/tests_opex_template.py` holds a
**second, independent transcription** of the spec table and asserts the seeded rows against
it — two transcriptions, so a typo in one fails against the other, where a test importing
the migration's own data would agree with any typo it contained.

### Why 1.4 became 1.4a and 1.4b

Split on 30 Aug 2026, **before either half was written**, and for a third distinct reason.
1.1 split on a technical finding. 1.2 split because one piece needed a product-owner
answer. **1.4 splits because its two halves carry different kinds of risk, and bundling
them would put both under one review.**

**1.4a is a migration with no user-visible change.** Two tables, one exception, one new
module, forty-two tests. Nothing reads the new tables, nothing calls the new functions, and
no template version authors an edge — so on the day it deploys, the product behaves
identically. That is a schema review.

**1.4b edits `task_status_update` and `task_detail_status_update`, which is the most-used
write path in the system, and it is the first change in this whole programme that a user
can see.** Every PM, site engineer, designer and finance user moves tasks through that
ladder every day. That is a behaviour review, and it deserves to be read on its own rather
than alongside a `CREATE TABLE`.

**The enumeration of status-write paths is 1.4b's, and it is in the row above rather than
here**, so that session starts from a list instead of a search. Two findings came out of
producing it and both are recorded in `EXECUTION_MODULE_DEFERRED.md` §B: ①–④ are **two
copies of the same function** (§B8), and the **Django admin can change a task's status with
no transition row at all** (§B9).

### 1.4a — four notes

**B-08 is closed, and the closure is narrower than it looks.** The *question* — may a site
override a template task dependency — is answered and the mechanism now exists. The
*enforcement* is 1.4b. B-08 was carried as "ANSWERED, NOT CLOSED" precisely because an
answer with nothing built is a note rather than a decision; that is no longer true, and the
remaining work is tracked as a prompt rather than as an open question.

**Three narrowings were taken deliberately and are recorded in §12 so a later session can
tell them from omissions:** Finish-to-Start only (no `dependency_type` column), no lag, and
template-level authoring with instance materialisation. The first two are the same
argument — a column that holds one value forever is worse than no column, and it is one
additive migration to add when something real needs it. The third is B-10 restated for
edges.

**Cycle prevention was built, not deferred.** A cycle makes every task in it permanently
waiting on a predecessor and is invisible until somebody tries to work, so it cannot be
left to be noticed in production. Both models refuse one, on save, with a `DependencyCycle`
that names the closing edge and the chain that closes it. Two-, three- and four-node cycles
are pinned, and so is a **diamond**, which is convergence and must *not* be refused.

**`materialise_task_dependencies()` exists but is called by nothing.** Its call site is
inside `attach_residential_template()`'s existing atomic block, immediately after the phase
and task loop — and 1.4a was forbidden to touch that function, correctly, because it is the
single most load-bearing function in the product. The function is written so it can be
called and tested in isolation, and one test runs it against a **really activated**
Residential project to prove it is a well-behaved no-op against the shape production
actually builds. It deliberately does **not** use `bulk_create()`: that would bypass every
guard the same session just wrote, and the materialiser must not be the thing that walks
around them.
