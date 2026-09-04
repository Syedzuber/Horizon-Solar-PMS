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
| 1.3b | **Mirror exclusion across the task counters (R-20).** `human_owned_tasks_q(prefix='')` + `is_human_owned(task)` in `utils.py` — a `Q`, not a filtered queryset, because three consumers have no queryset to filter (two conditional aggregates and one prefetched list). Applied at **21 counter sites in 4 files**; the three `{{ phase.tasks.count }}` template captions deliberately left alone (they label lists that legitimately show mirrors). 28 tests. **No migration.** Product-owner decisions: progress denominators exclude mirrors (Option A), `current_phase` deferred, Coordinator dept row deferred. **Four findings.** The audit's enumeration was **stale and short**: the spec dropped to 5 mirrors per site from 11, so `dept_scm_pending` inflates by 95 not 570; and it **missed two sites** — the CEO project-card `task_total_count`/`task_done_count` pair, and a fourth copy of `current_phase` in `models.py`. The row-sum invariant the prompt called "pinned" **had no test at all** (deferred G5) — this session wrote it. And **`attach_residential_template()` never copies `is_mirror`**, so the whole exclusion is correct but inert until 1.3c wires it (**B19**). | ✅ |
| 1.3c | **OPEX execution start — the transition, and the line that made 1.3b real.** **① The seventh snapshot (B19, closed):** `is_mirror=t.is_mirror` added to the one `bulk_create`, which moved into a new `_attach_task_template()` shared by both project types. Until this line, no `Task` row could carry the flag and **all twelve of 1.3b's exclusions were correct and completely inert**. Verified on a real attached site: 5 mirrors, named. **② `attach_opex_template()`** — same core, none of the Residential afterthoughts (no Finance `raise`, no invoice name list), PM pre-assignment filtered to `is_mirror=False`, and **no virgin-DB bootstrap** (a missing active OPEX template raises rather than being invented). Extracted, **not renamed and not a sibling** — the Residential name has three call sites, two in test modules this prompt could not touch. **③ `opex_site_activate`, a SEPARATE VIEW** — `project_activate` was **not edited**, so the Residential path is untouched rather than merely guarded. No `assigned_design_id`, **no M1/M2/M3**, `REASON_EXECUTION_STARTED` on the ledger row, second activation refused. **④ One control**, branched on `project_type` in `project_overview.html`. **38 tests, no migration.** **Findings:** the human-write refusal promised to this prompt by three documents was **not built** (out of remit — **B22**); the PM dashboard's draft card still opens the designer modal (**B23**); a bulk route for the remaining 91 sites needs its own idempotency guard (**B24**); B18 now names the concrete dates (HOTO at `activated_at + 22`). **One file outside the stated MODE list: `projects/urls.py`**, one route line — a view is unreachable without it. | ✅ |
| 1.4a | **Task dependencies — the model half.** `TaskTemplateTaskDependency` and `TaskDependency`, `DependencyCycle`, `incomplete_predecessors()` and `materialise_task_dependencies()` in the new `projects/task_dependencies.py`, migration `0073`. 42 tests. **Entirely additive — no view, no template, no permission file, no status-change behaviour, and nothing that can prevent a task from being started.** Closes B-08 at the model layer. | ✅ |
| 1.4b | **Task dependencies — the enforcement half.** Wires the early-start **warning** and the **mandatory reason** into the status-change path, and records the override as a `StatusTransition` with a remark under a new `REASON_EARLY_START`. **No migration** — a `REASON_*` constant is module-level by R-10, and `SUBJECT_TASK` already exists. **The first user-visible change in this programme**, on the most-used write path in the product. **The write paths it must cover — enumerated by 1.4a's pre-flight, so this session does not rediscover them:**<br>① `views.py:task_status_update` — blocked branch (`update()` at ~3840)<br>② `views.py:task_status_update` — ordinary ladder (~3884)<br>③ `views.py:task_detail_status_update` — blocked branch (~4078)<br>④ `views.py:task_detail_status_update` — ordinary ladder (~4118)<br>⑤ `views.py:milestone_receive` — milestone→task sync (~6569)<br>⑥ `views.py:project_overview` — Finance `update_milestone` branch, milestone→task sync (~7142)<br>**①–④ are two near-identical copies of one ~180-line function** (§B8). The warning must land in **both**, or a user routes around it by using the other screen. **⑤ and ⑥ are not a user starting a task** — they are an automatic sync wrapped in `except Exception: pass`, and a mandatory reason must **not** be imposed on them. **A seventh path exists and is uninstrumented: the Django admin's `TaskAdmin` change form** (§B9) — out of 1.4b's scope, but it is why "every task status write is instrumented" is not quite true today. | ☐ |

### Where phase 1 stands after 1.3c — 31 Aug 2026

**The 1.3 sequence is complete.** 1.3a seeded the OPEX template, 1.3b excluded mirrors
from the counters, and 1.3c attached the template, copied the seventh snapshot that made
1.3b's work take effect, and shipped the opening transition. **The merge and the deploy
are the next decision, and they are a separate plan.**

**Phase 1 as a whole is NOT finished, and saying otherwise would hide two things:**

| | State |
|---|---|
| **1.2b — `ProjectAssignment`** | ☐ **BLOCKED** on the product-owner answer to Q-E3 (do Finance and SCM get Program-level assignment, and who maintains it). Not a scheduling gap — nothing to build until that is answered. |
| **1.4b — dependency enforcement** | ☐ Not started. 1.4a shipped the model layer; the warning and the mandatory reason on the status-change path are still owed. |

**What ships if `execution-phase-1` is merged today.** The behaviour change on production
is real but narrow: 91 previously unactivatable tender sites become activatable, one at a
time, by their PM. Nothing activates itself. The three things to expect on the day the
first sites move — none of them a fault:

1. `activated_at` is the definition of "active" for the CEO per-user report and the EOD
   digest, so each activated site joins both surfaces (§12, 30 Aug).
2. Every OPEX task carries `due_date = NULL`, so those sites appear unscheduled rather
   than overdue. That is deliberate — **B18**, still open, waiting on real durations.
3. Mirrors are excluded from every count but still **visible** in task lists. A phase can
   read 100% while its mirrors are Not Started; that is the accepted trade (R-20), not a
   bug report.

~~**And one thing that is genuinely owed before anyone leans on the mirror concept:** the
human-write refusal (**B22**). Today a mirror is unwritable only because it has no
assignee.~~ — **paid 31 Aug 2026 by prompt B22.** The refusal is one check at the top of
`_apply_task_status_change()`; a mirror is now unwritable by rule, whether or not it has
an assignee. What remains owed is the other half — the derivation hooks that will *write*
mirror statuses, which belong to the source objects in phases 3–5. Until they exist a
mirror sits at its seeded status, which is stated in `docs/execution-model.md` §14 rather
than left to be discovered.

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
| B22 | **Mirrors become read-only for real.** One `if task.is_mirror:` as the **first statement** of `_apply_task_status_change()` — above the transition table, above the inline `due_date` write — returning the existing `_TASK_STATUS_REFUSED`. **No fourth outcome constant, no per-screen message, neither caller edited**, and nothing written on a refusal: no `StatusTransition`, no `ActivityLog`, no notification. The message names the rule rather than a permission, because a user who reads "permission denied" asks for permission that cannot exist. **Before this, a mirror was unwritable by accident** — the five mirrors seed unassigned and both views refuse an unassigned task first — so assigning COD to a PM made it writable and nothing said otherwise. **24 tests in a new `tests_mirror_readonly.py`**, contract half through **both** entry points, on a **really activated** OPEX site; every test assigns the mirror first, and `_assign_mirror()` asserts the assignment landed, which is the trap this entry existed to warn about. Verified negatively: with the `if` neutralised, 34 assertions fail. 897 → 921 tests, same one pre-existing failure and one collection error. **No migration, no model, no template, no existing test module.** Closed §B22; **opened §B25 and §B26.** Two pre-flight findings: **neither Finance sync can reach a mirror** (both select by task name from a three-entry map, no mirror name is in it, and an OPEX site has no `PaymentMilestone` at all) — so the refusal has no hole the helper cannot close; but **`TaskAdmin` leaves `is_mirror` editable**, which is the one way that could stop being true. **Four paths can assign a mirror**, one of them — `project_overview`'s `assign_design` bulk — in bulk with no intent, missing the `is_mirror=False` filter its OPEX-attach counterpart has. | ✅ |
| B18 / B23 / B26 | **Manual dates only, and three controls that lied.** **① B18 — auto-scheduling is not in OPEX v1** (product decision, `docs/execution-model.md` §16). All 22 OPEX tasks carry `duration_days`'s default of **1** and all are `Internal`, so any bulk computation puts HOTO at `activated_at + 22` and the whole tender portfolio overdue within a month. `project_recalculate_dates` **and** `enable_cascade_scheduling` now refuse `project_type != 'Residential'`, view-side, with the template half where one renders. **② B23** — `dashboard/pm.html`'s draft card got the same four-line branch 1.3c applied to `project_overview.html`; no JS copied. **③ B26** — `is_mirror` joins `status` in `TaskAdmin.readonly_fields`, B9's `DO NOT REMOVE — R-10` comment **extended, not duplicated**. **36 tests** — a new `tests_opex_manual_dates.py` plus `AdminCannotWriteTaskMirrorFlagTests` beside B9's guard in `tests_status_transition.py` §8b. 921 → 957, same one pre-existing failure and one collection error. **No migration, no model, no `utils.py`, no `permissions.py`, no other `ModelAdmin`, no other existing test module.** Closed §B23 and §B26; **rewrote §B18** rather than closing it. **Two pre-flight corrections to B18's own text:** the *Recalculate dates* control is **on no template and never was** — its exposure was the view accepting a direct POST — and the entry **did not name the door that mattered**, `enable_cascade_scheduling`, which is rendered, irreversible, and once on locks every non-PM role owner out of `task_set_due_date`. **A PM can set a due date on a task of any role** (the role match lives inside `task_set_due_date`'s `if not is_pm:` arm), so manual scheduling was already possible and is now pinned. **A due date can still be set on a mirror** — reported, not built, recorded under B18. All four guards verified negatively: 14 tests fail with them neutralised. | ✅ |
| B21 | **One answer to "which phase is this site on".** Four copies of "first phase holding a not-Done task" — `Project.get_current_phase()`, `dashboard_pm`, `dashboard_site_engineer` and an inline loop in `dashboard_bd` — became **`utils.current_phase()`** (**R-21**), with mirrors excluded through the existing `is_human_owned()`. The BD loop was removed, not kept as a fast path. **A fresh OPEX site now reads `Approvals (Pre-Installation)`, not `Design`** — Phase 1 holds only the `Design` mirror, which no hook can ever complete, so before this every OPEX site displayed "Design" forever on all four screens with all nine installation tasks done. Pre-flight found the four **already disagreed** on completed projects (`None` vs the last phase), which was a live visible defect; settled by decision. `dashboard_pm` and `dashboard_site_engineer` gained the `Prefetch` the other two already had — six projects, **13 queries before, 0 after**. 27 new tests in `tests_current_phase.py`, `AgreementTests` parameterised over all four call sites. Closed §B21, four findings recorded. | ✅ |
| HOTFIX-1 | **The migration chain became a thing that is tested.** The 3 Sep merge to `main` **could not deploy**: migration `0067` raised `TypeError: TaskTemplateTask() got unexpected keyword arguments: 'is_mirror'`, `migrate` exited non-zero, gunicorn never started and production was rolled back to 0066. `seed_task_template_version()` is shared by 0067 and 0075; prompt 1.3a added `is_mirror=` for 0075, and `is_mirror` arrives in **0074 — seven migrations after 0067**. **Fixed at the helper, not the migration** — `utils.kwargs_for_model_state()` drops optional fields the handed-over model state does not carry, so the *next* field added for a future caller cannot break an older one. **R-22** added; **§18** added. The guard is a test module inside `projects/`, so the ordinary suite run executes it and no session has to remember a second command. **0067 was the only broken migration** — the whole chain now applies to an empty Postgres database. The larger finding is that `test_settings.py` disables migrations, so **no test in this programme had ever run one**, and a developer's local `migrate` was equally blind because 0067 had applied there months earlier. The shim **survives**, measured: **~75 s** here against **~1,350 s** under real settings, which reports **3 failures and 308 errors** — none of them product defects, and **306 of them one collision**: a shared fixture creating `BOQItemMaster` rows that migrations 0047/0057 already seeded. The disagreement runs both ways — `tests_design_part46`'s standing SQLite failure passes on Postgres. Opened §B30 and §B31. **No model, no view, no template, no existing test module, and no new migration.** | ✅ |

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
- **1.3b — mirror exclusion across the task counters (R-20).** ✅ **Done 30 Aug.** 21 sites
  in 4 files, one helper, no migration. **1.3c now carries a hard pre-condition from it:**
  `attach_residential_template()` copies six fields from the template task and `is_mirror`
  is not one of them, so every exclusion 1.3b shipped is inert until 1.3c adds that line
  (**B19**). Wiring the attach without it means 1.3c ships the mirrors and none of the
  protection.
- **1.3c — OPEX execution start.** ✅ **Done 31 Aug.** Manual PM activation with no
  precondition beyond ownership; no `assigned_design_id`; no Residential milestones; the
  attach. **The pre-condition from 1.3b was met first and verified before anything else
  was built** — `is_mirror` is copied, and a real attached OPEX site carries five flagged
  rows. **B19 is closed.** The mirror **refusal** promised to this session was not built
  and is now **B22**: it was the status path, not the opening transition, and every mirror
  ships unassigned so nothing can write one today — by accident rather than by rule.

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

---

## DEMO-1 — a demo dataset you can throw away

**Local-only tooling. Ships no product behaviour.** Nothing a user sees changes; no model,
view, template, migration or existing test module was touched.

### What it was for

Nobody has ever opened an activated OPEX site in a browser. B22's mirror refusal has been
proved in tests and in a shell and never seen by a human. `PHASE_0_BROWSER_TEST_PLAN.md` has
never been run. Production holds 96 OPEX sites in Draft and almost no execution data, because
the team is deliberately waiting for delivery. So the gap was a populated local environment to
hand-test against before the merge.

### Extend, not replace — and it cost three files, not two

The three pre-existing seed commands **still ran**, on the current schema, after 75
migrations: a full per-model census across a seed → seed → teardown cycle came back
**identical**, and three consecutive cycles were clean. So "too stale to save" was never the
argument. The argument was the *teardown's identification mechanism*, and it forced a
consolidation either way.

A second command pair would have taken the codebase from **2½ teardowns to 3½** —
`seed_scm_handoff_data` already carried its own second one as `--reset`, and all three files
were welded together by a shared `TEST_PREFIX` constant. That is the duplication B8 spent a
session undoing. Extending meant rewriting all three and ending at **two commands and one
manifest**:

- `seed_opex_test_data` — the whole demo environment, and it writes the manifest
- `seed_scm_handoff_data` — Part-6 SCM states layered on top, **appending to the same
  manifest**; `--reset` deleted
- `teardown_opex_test_data` — manifest-driven, and it refuses without one

`_demo_support.py` holds the interlock and the manifest. The leading underscore is
load-bearing: Django's `find_commands()` skips it, so it is importable by all three without
appearing as a command.

### The manifest, and the one thing it deletes that it should not be able to

The teardown used to find its targets with `project_id__startswith='Test-'` — a delete whose
blast radius was decided by a string comparison against live tables. It now deletes a recorded
list of primary keys in reverse creation order and **runs no query that could select a row a
seed did not create**. There is deliberately no fallback when the manifest is missing: a
fallback that pattern-matched live tables would reintroduce the replaced mechanism *as the
error path*, which is where it would be least tested.

**The refusal message names the resulting regression** — `Test-` rows from the old seed cannot
be removed by this version, and the fix is the old commit or a manual delete — because the
first person to hit a bare "no manifest found" would reasonably conclude the tool is broken.

**`StatusTransition` rows are deleted, through `QuerySet.delete()`, which bypasses the
`AppendOnlyViolation` guard.** That is R-4's central guarantee being stepped around, and it
reads as a decision in three places (the module docstring, the deletion loop, `docs/demo-
data.md`). It is narrow: `StatusTransition.project` is `SET_NULL` precisely so a hard-deleted
project cannot erase its own history, so without it every teardown would leave orphaned ledger
rows behind permanently, one set per cycle, in the table the dwell-time reports read. It is
only safe because the manifest bounds it to rows a seed created on a database the interlock
has already proved local.

A **leak sweep** closes the gap between "rows the seed constructs" and "rows the real code
paths wrote on the way past". pk high-water marks are taken for every model before the first
write; anything new and unrecorded is appended last, so the teardown deletes it first — the
safe order, because side-effect rows are leaves. This is also where the one dangerous bug of
the session was caught before it ran: the first draft of the SCM sweep passed a mark of `0`,
which would have recorded **every `ActivityLog` and `StatusTransition` row in the database**
into the manifest, and the teardown asks no questions about what is in the manifest.

### Two namespace facts that the product decided, not the prompt

- **OPEX demo IDs carry no hyphen** (`DEMOOPEX01`). `OpexSiteForm.clean_site_code()` runs the
  entered code through `normalize_program_code()`, which strips everything outside `[A-Z0-9]`.
  A `DEMO-` prefix is not achievable through the real creation path, and setting `project_id`
  explicitly to defeat that would mean the seed bypassing its own creation path to satisfy a
  cosmetic rule.
- **The Residential ID does** (`DEMO-RES-01`), and that IS an explicit bypass of
  `generate_project_id()`. Through the generator the demo project would take the next real
  `HRP-RES-2026-NNN` number on a database that is a production restore, be indistinguishable
  by eye from a real project, and hand the number back for reuse at teardown. What the bypass
  forgoes is stated in the code and in `docs/demo-data.md` so nobody reads "created through the
  real path" as including ID generation.

### Seven demo roles, not eight

`UserCreateForm.clean()` refuses a second Admin ("Only one Admin account is permitted") and
every real database already has one. Not bypassed. Demo tooling is exactly where a "just this
once" bypass gets copied later, and the rule is real. The operator is told to log in as the
existing Admin.

### The finding worth having

Building this was an unusually direct audit of *can this state be reached through the product
at all*, because a seed reaching for `objects.create()` has found a state the product cannot
produce. **Six did**, recorded as §B23. The two that go beyond "not built yet":

- **The three execution capability flags cannot be set even by an Admin.** They are absent
  from both user forms *and* from `UserProfileAdmin`, which does carry `is_design_head` and
  `is_design_qc`. Whichever consumer lands first (2.2 / 2.3 / 4.1) needs a writer; adding
  three names to `UserProfileAdmin` would make them settable today.
- **`_apply_task_status_change()` requires a `request`**, so the one decision path for task
  status (R-18) has no non-HTTP home. That matters beyond seeding: the mirror derivation hooks
  of phases 3–5 will write mirror statuses from source events and, per its own docstring,
  "will not call this function" — so the transition table, `completed_at` and `blocked_since`
  will have to be restated somewhere or diverge.

### Verification

`manage.py check` clean. Suite **984 tests, 1 failure + 1 error — the same two as baseline**
(the SQLite constraint-name assertion in `tests_design_part46`, and the `test_whatsapp_templates`
loader artefact; neither was this session's to fix). `tests_demo_data.py` adds 15 tests, all
passing. Three full seed → seed → teardown cycles were run against the real local database and
the per-model census came back identical each time.

---

## 1.5 — OPEX template v2, and three things the screen got wrong

**1 Sep 2026 · branch `execution-phase-1`.** The last session before deploy planning. The
browser test had passed every blocker and then found three things no test could, because all
three were about what the screen SAYS rather than what the database holds.

### It ran twice, and the first run stopped

The prompt required `docs/OPEX_task_template_spec.md` at **v1.4** and the file was at **v1.3** —
tree clean, no v1.4 anywhere in the repo. Pre-flight ran read-only, reported the stop, and
nothing was written. The spec then landed as **v1.5** (`8796e57`), which is what was built from.

Two of v1.5's own corrections mattered to this session:

- **The "285 Residential milestones on tender sites" do not exist.** §2a establishes 285 was the
  A-1.3 audit's *projection* of what activating 95 sites would mint, not a count of anything.
  The narrower "hide the card only when empty" gate that figure had justified was dropped for a
  plain `project_type` gate. **Corrected again by 1.6: the real number is 12**, counted in
  production 01 Sep 2026 — so §2a's *conclusion* ("no such rows exist") is wrong as well, and
  one of its premises must be. See **B28**; §2a itself is still uncorrected on disk.
- **§6 reversed the prompt's central instruction.** The prompt said "Do not edit OPEX v1 in
  place. R-7 forbids it. This is a new version." §6 says correct 0075 in place and do **not**
  bump to a v2. Its precondition was verified before acting on it, not taken on trust:
  `origin/main` is at migration **0064** and `0074`/`0075` are not on it.

### The two things the prompt did not anticipate

**Editing 0075 in place breaks 20 tests across five modules the MODE forbade.** They seed by
calling `seed_opex_v1()` directly (`test_settings` disables migrations), so correcting the
literal corrects their fixtures too — the exact cost a v2 bump would have avoided. All 20 were
stale-count assertions; none was weakened or deleted. MODE was widened by explicit decision to
`tests_opex_template`, `tests_opex_activation`, `tests_mirror_readonly`, `tests_opex_manual_dates`,
`tests_demo_data`, `tests_mirror_metrics` and `tests_current_phase`.

**A seventh module broke, and the full suite is what found it.** `tests_current_phase.py`
pins R-21's phase walk, and Procurement & Delivery holding only mirrors means R-21 now steps
over it exactly as it has always stepped over Design — so "phase 2 done" reads `Installation`
and the walk visits **five** human phases, not six. **R-21's implementation is untouched**; the
template changed underneath its tests. One test was rewritten rather than renumbered: it used to
complete the phase's two inspections and show it advanced with the mirror still open, and there
is nothing left in the phase to complete — the phase is now skipped on the strength of the flag
alone, which is a stronger statement of the same rule.

**Removing the two inspections left SCM with no entered task at all.** They were SCM's only
non-mirror rows. SCM now owns four mirrors and nothing else, the position Design was already in.
Confirmed as intended and recorded as **§B27** rather than absorbed silently: no SCM or Design
person has an actionable OPEX task, and none of their six mirrors can move until B-18 *and*
SCM's catalogue mapping both land.

### The mirrors-only phase, and the one carve-out of R-20

Phase 1 and Phase 3 both hold only mirrors and rendered **"0/0 done" above an empty bar** while
their own card headers said "1 task" and "4 tasks". The decision taken was that **one mirror not
updated is one task pending**, so the per-phase bar counts mirrors and Phase 3 reads `0/4`.

**Scoped deliberately to that one loop.** Overdue, per-user workload, the CEO rollup and every
drill-down still exclude mirrors. **R-21 is untouched**, so a mirror still never makes its phase
current and the B21 bug does not return. `tests_mirror_metrics.py` was updated on both sides: the
behavioural test now asserts the bar counts the pair (its old docstring said the opposite
asymmetry was "pinned so it is not 'fixed'", so the reversal is spelled out in place), and the
two `phase_data_json` keys joined `ALLOWED_BROKEN_COUNTS` with the reason. One of that module's
own self-tests had to move its probe to `ext_pending`, because the key it used to prove the sweep
bites is now legitimately allow-listed.

### The marker is not the guard

The `Derived` badge names the cause rather than the symptom, and the status `<select>` is not
rendered for a mirror — it falls through the read-only branch that already existed, so no new
markup and no new branch. **B22's refusal is unchanged and no second check was added in the
view.** `TheRefusalIsUnchangedTests` assigns every mirror first (so a refusal cannot come from the
unassigned gate) and posts to both entry points, asserting the message text to prove which rung
answered. That is the premortem's second risk arriving from the opposite direction — the risk was
a UI-only refusal; this is a UI affordance added *over* a real one — and being answered.

### Two bugs caught in the writing

- A Django `{# … #}` comment **cannot span lines**. Three multi-line ones raised
  `TemplateSyntaxError` and became `{% comment %}` blocks.
- `{% if project.project_type == 'Residential' and role == 'PM' or role == 'Finance' … %}`
  parses as `(Residential and PM) or Finance or …` — Django binds `and` tighter than `or` and
  templates have no parentheses. Finance, CEO, Admin and BD would still have seen the card on an
  OPEX site. The outer `{% if %}` is the parenthesis.

### Verification

`manage.py check` clean. Migration round trip run against the real local database: `migrate
projects 0074` removed OPEX and left RESIDENTIAL intact, `migrate` re-seeded **7 phases / 23 tasks
/ 8 mirrors**. Demo data torn down and re-seeded; both demo OPEX sites carry 23/8. The rendered
overview shows **8 `Derived` badges, 15 status controls, no Payment Milestones card**, and Phase 3
reading `0/4`; the Residential demo project shows the card and no badges. `tests_opex_template_correction.py`
adds **29 tests**. Suite **1028 tests, 1 failure + 1 error — the same two as baseline** (the SQLite
constraint-name assertion in `tests_design_part46` and the `test_whatsapp_templates` loader
artefact; neither was this session's).

---

## 1.6 — progress and workload are different questions

Run 1 Sep 2026 on `execution-phase-1`, after 1.5 (`f022b7d`). Baseline **1028 tests, 1
failure + 1 error**, both pre-existing (the SQLite constraint-name assertion in
`tests_design_part46`, and `test_whatsapp_templates` — a root-level module the loader
cannot import). Neither was this session's.

**A note on the invocation, because the counts disagree.** `manage.py test projects` gives
**1027 / 1 failure / 0 errors**; the bare `manage.py test` gives **1028 / 1 / 1**.
`test_whatsapp_templates.py` sits at the repo root, outside the `projects` package, so the
label form never loads it and the error never appears. The bare form is the baseline.

### What the session was for

1.3b applied one rule — *a task metric excludes mirrors* (R-20) — at 32 call sites. 1.5
removed the exclusion from `project_overview`'s per-phase bar so a delivery phase read 0/4
rather than 0/0, which was right: 0/4 tells a PM four deliveries are outstanding and 0/0
tells them nothing. **But it changed one screen.** `dashboard_pm`'s percentage and the CEO
project cards went on excluding mirrors, so one activated OPEX site reported its
completeness **out of 15 in two places and out of 23 in a third**, from the same rows at
the same moment. R-20 as written had become false.

### The rule, and the classification

**The question a number answers decides whether mirrors are in it.** "How much of this
SITE is done" includes them; "how much work does this PERSON or TEAM owe" excludes them.
R-20 was rewritten to state both halves.

All 32 live call sites were classified. **Seven are PROGRESS** (`dashboard_pm`'s four
per-project counts, the CEO card pair, `phase_data_json`'s two bar numbers); **twenty-five
are WORKLOAD** and kept 1.3b's exclusion unchanged. **Six lines actually changed** —
`phase_data_json` was already correct from 1.5.

**Two sites went to the product owner rather than being decided in the build**, both
because the rule and the prompt's own framing pointed different ways:

- **The SE progress figure** (`views.py` ~965) renders as a percentage, but its queryset
  carries `assigned_to=se_profile`, so two engineers on one project get two different
  numbers out of it — which a site-completeness figure cannot do. **Ruled WORKLOAD,
  unchanged**, and commented in place saying it looks like progress and is not, so nobody
  later "fixes" it to match the PM card.
- **`phase_data_json`'s `ext_pending`.** 1.5 changed one binding (`countable = tasks`) that
  **three** outputs read, and only two were the bar. `ext_pending` is a pending count and
  belongs to WORKLOAD; it was swept in by accident. **Restored**, three lines. Inert on
  every row in existence — all eight OPEX mirrors are `Internal` and Residential has none —
  and fixed anyway rather than filed, because a misclassified number nothing exercises is
  the kind that surfaces eighteen months later.

### One place, again

`utils.site_progress_tasks_q(prefix='')` sits beside `human_owned_tasks_q()`, named for the
question rather than the mechanism. **It returns an empty `Q()` and filters nothing** —
`FILTER (WHERE True)`, semantically identical to no filter at all. Its entire value is that
a counter which *deliberately* counts mirrors and one that *forgot* to exclude them are
otherwise byte-identical, and that ambiguity is the defect this session exists to close. It
accepts and ignores `prefix` so swapping the two helpers is a one-word diff.

### The structural guard now enforces two categories

1.3b's `ALLOWED_BROKEN_COUNTS` was an allow-list of exceptions to a rule that had stopped
being true, and after this session it would have needed six more entries. It became
**`PROGRESS_KEYS`** — a positive declaration of the second category — and a new guard reads
it in both directions.

`ProgressWorkloadDifferentialSweepTests` renders each dashboard **twice over the same
fixture, with 0 mirrors and with 6**, and diffs the two context walks key by key:

> **A WORKLOAD number does not move when mirrors are added. A PROGRESS number does.**

The changed set is therefore derived from the running code, and `changed == PROGRESS_KEYS`
catches a workload counter that starts counting mirrors (an undeclared mover) *and* a
progress counter that stops (a declared non-mover). **Proven to bite both ways**: breaking
`pending_approvals` to include mirrors named `summary.pending_approvals`; breaking
`total_tasks` to exclude them named `projects_with_progress[0].total_tasks`. The 1.3b
value sweep is kept alongside it.

**The categories could not be derived from anything live, and the entry says so.**
`is_mirror` is a property of a task; the category is a property of a *question*, and a
template-context integer carries no provenance back to the queryset that made it. What
changed is the polarity of the declaration, not its existence.

**Two properties written into the module docstring rather than implied away.** Ratios are
absent from `PROGRESS_KEYS` on purpose — `internal_percent` and `pct` do not move when
mirrors are added proportionally, so the set means "keys that must move", not "all progress
numbers". And there is one irreducible gap: a counter *meant* to be progress but written
with the exclusion passes both guards silently, because nothing records what question a
number was meant to answer. The guarantee is one-directional and is stated as such.

### Tests

`projects/tests_progress_vs_workload.py`, **24 tests**, all against a site taken through
the real `opex_site_activate` view rather than a hand-built fixture. Every PROGRESS number
counts all 23; every WORKLOAD number counts only the 15 entered; the CEO card invariant
(`pending + completed == total`) holds in three states including the real end state of an
OPEX site — **8 pending / 15 completed**, every human task done and no mirror closable; the
CEO report's row-sum invariant is unmoved; Residential is asserted identical under both
categories. `PmAndOverviewAgreeTests` is the one the session existed to create.

### The 285 figure

Corrected to **12** in `views.py`'s `opex_site_activate` docstring and in one comment line
of `tests_opex_activation.py`. Found but not corrected: `OPEX_TEMPLATE_AUDIT.md` (left
deliberately — it should keep its own record of what it projected) and
`docs/OPEX_task_template_spec.md` §2a, which says "No such rows exist" and is **wrong** —
outside this session's MODE. Recorded as **B28**: twelve rows exist, §2a's reasoning is
sound and its conclusion false, and nobody knows which path created them.

### Verification

`manage.py check` clean; `makemigrations --check --dry-run` reports no changes. No template
changed and none needed to. Suite **1060 tests** — the baseline 1028 plus 24 new plus 8
from the differential sweep — with the two pre-existing problems and **one new failure**
(below).

The shell artefact was produced by activating one OPEX site and one Residential project
through the real views, completing six human tasks on each, assigning the eight mirrors to
a real person so the workload counters had something to exclude, and printing every number
with its category. **Nothing disagreed.** PM `internal_total` 23 = overview 23; PM
`internal_done` 6 = overview 6; CEO card 17 + 6 = 23 while the department aggregate reads
15; Residential 52 tasks, both categories 52, both denominators 44, phase bars summing to
the card.

### One stop condition, raised and then closed

**B29.** `tests_opex_activation.CountersOnARealSiteTests.test_the_project_card_counts_exclude_all_eight_mirrors`
asserted `card['total_tasks'] == 15` and read 23 after the change. **Not a defect in the
change** — a correct 1.3b-era pin of the rule this session replaced. `tests_opex_activation.py`
sits outside MODE beyond the one 285 comment line, so the session stopped and reported rather
than editing it.

The product owner lifted MODE for that single method. It is now
`test_the_project_card_counts_every_task_including_mirrors`, asserts **23**, and its docstring
records what it asserted before and why that stopped being right. The class keeps its proof of
the WORKLOAD half regardless: counters 1 and 2 in the same class are the CEO `dept_rows` for
Design and SCM, which are workload numbers and still excluded. Suite back to baseline —
**1060 tests, 1 failure + 1 error**, both pre-existing.

---

## HOTFIX-1 — the migration chain, and why nothing caught it (3 Sep 2026)

### The failed deploy

The phase 1 merge (`89d8e6f`) was pushed to `main` on **3 Sep 2026**. Railway's start
command is `migrate --run-syncdb && collectstatic && gunicorn`. **Migration 0067 raised,
`migrate` exited non-zero, and gunicorn never started.** Production was down until the
previous deployment was restored, and now sits at **0066** — 0065 and 0066 applied, 0067
rolled back cleanly, 144 projects and 1,861 tasks intact and nothing half-written.

```
TypeError: TaskTemplateTask() got unexpected keyword arguments: 'is_mirror'
```

`seed_task_template_version()` is shared by **0067** and **0075**. Prompt 1.3a added
`is_mirror=` to it for 0075's benefit. **`is_mirror` arrives in 0074 — seven migrations
after 0067** — so against 0067's model state the keyword does not exist.

**0067 was the only broken migration.** The whole chain, run against a genuinely empty
Postgres database, now reaches `Applying projects.0075… OK`. Nothing between 0068 and 0075
is broken, and nothing before 0067 is either.

### The fix is in the helper, and that is the whole point

Three options were on the table: pass field values through a caller-controlled dict,
introspect the model, or give 0067 and 0075 different signatures. **Introspection won on
one criterion — not brevity, but that a future field added for a future migration cannot
break an older one.** The dict form moves the same trap into two call sites, and separate
signatures fix `is_mirror` and nothing else.

`utils.kwargs_for_model_state(model, required=…, optional=…)`: `required` raises **here**,
naming the model, when a field is genuinely absent; `optional` is included only if that
model state carries it. The line between them is not re-argued each time — **`t.get(name,
default)` in the seed data means optional, `t[name]` means required.**

**No migration was fixed, because none was broken.** 0067's body is byte-identical; only its
header comment changed, to correct an argument it made that was half right. It claimed
safety because the helper "takes its model classes as arguments" — true, and insufficient:
that stops the helper **reading** the wrong model and does nothing to stop it **writing** a
field the model does not have yet. The migration-editing licence this session was granted —
valid only while 0067–0075 have never applied on production — **was not needed and is
recorded as unspent.**

### The larger half: nothing could have caught it

`solarpms/test_settings.py` disables migrations. **No test in this programme has ever run
one.** All 1,060 build their tables from today's `models.py`. Every "migrate forward,
reverse, forward" a session reported ran against a local database where 0067 had applied
months earlier, so it stepped back one migration and forward again and **0067 never
re-ran**. This was not a bug that slipped through a good process; it was one the process
structurally could not see, and had been since phase 0.

`projects/tests_migration_chain.py` closes it: a throwaway Postgres database, a subprocess
running `manage.py migrate --run-syncdb` — Railway's command verbatim — under
`DJANGO_SETTINGS_MODULE=solarpms.settings`, so it holds **whichever settings the suite was
launched with**. It fails rather than skips when Postgres is unreachable. Proved by
deliberate breakage: re-passing `is_mirror` unconditionally makes it fail with
`FAILING MIGRATION: projects.0067_seed_residential_template_v1`.

### The shim survives, and the numbers are the argument

Both reasons its docstring gave are gone — the `CREATEDB` grant has been made, and the
Postgres-only raw SQL it names is `0005_project_redesign`'s `DROP TABLE … CASCADE`, which is
no longer a problem on Postgres. It was kept anyway, on measurement:

| | shim | real settings |
|---|---|---|
| Wall time | **~75 s** | **~1,350 s (22 min, 18×)** |
| Result | 1 failure (the standing SQLite constraint-name one) | **3 failures, 308 errors** |

**The 308 errors are the finding, not a regression.** VERIFY step 4 asked whether the two
runs agree on failures; they do not, by 310 — **and in both directions**, since
`tests_design_part46`'s standing SQLite failure passes under Postgres. None are product
defects. The suite is written against a schema with **no rows in it**, which is what the
shim produces; run the real chain and the data migrations have run too. **306 of the 308 are
a single collision** — a shared fixture creating `BOQItemMaster` rows whose codes migrations
0047 and 0057 already seeded (`Key (code)=(OPX-001) already exists`) — across
`tests_residential_baseline` (92), `tests_task_status_path` (49), `tests_boq_upload` (47),
`tests_status_transition` (39), `tests_soft_delete` (32), `tests_design_part11` (32) and
`tests_demo_data` (15). The other two errors and all three failures are
`TaskDurationTemplate` the same way: 0034 seeds 50 rows, the tests assert 0.

Making the suite pass under real settings is a programme across every test module, and every
one of them was outside this session's MODE. **Deleting the shim today would not give a
stricter suite; it would give a red one nobody could read.**

So the guard is a **test module inside `projects/`**, not a separate script: the ordinary
suite run picks it up — 11 s of the 75 — and **no session has to remember a second command**,
which is the only way a check like this stays true. It is also runnable alone in ~15 s after
touching a migration. `docs/execution-model.md` **§18** states both, and **§B31** records what
keeping the shim still costs along with the trigger that retires it.

### Verification

`manage.py check` clean. `makemigrations --check --dry-run` reports no changes, and is now
also a test. The chain applies from empty. The guard bites when a migration is broken and
names it. Suite under the shim: **1061 tests, 1 pre-existing failure, ~75 s** — the baseline
1059 plus this session's 2, and the standing `tests_design_part46` SQLite constraint-name
failure is the one. The guard passes inside both runs.

### Findings recorded

**§B30** — three migrations import live application code. 0067 and 0075 are fixed at the
helper; **0069 is still exposed** through `models.derive_checklist_code`, narrowly (one
field, `Checklist.code`) and not imminently. Not fixed: it is not broken, its trigger is a
rename of that field, and fixing an unbroken migration is a change with no test to prove it
right. **§B31** — the two suite runs check different things and nothing reconciles them.
