# Phase 0 — Completion and Handover

**Closed 29 Aug 2026 by prompt 0.6.** Seven sessions: 0.1, 0.2a, 0.2, 0.2b, 0.2c, 0.3, 0.4,
0.5, 0.6.

Written for somebody who was not here. It assumes you have read nothing, and it tells you
what was built, what was fixed, what was deliberately left alone, what now stands guard,
and the two things nobody in a session is allowed to decide.

**The one-sentence version.** Phase 0 did not build a feature. It made the execution module
*safe to build features on*: it wrote down what actually exists, closed the access holes
that made a site-engineer login impossible, deduplicated four business acts that behaved
differently depending on which button you pressed, and gave the module the three
foundations every later phase assumes — a state ledger, versioned task templates, and
versioned checklists.

**Where to look next.** [`docs/execution-model.md`](docs/execution-model.md) v1.3 is the
authority on vocabulary, structure and rules — read it before anything else, including
this. This file is the narrative; that file is the contract.

---

## 1. What phase 0 built

### The context pack — `docs/execution-model.md` (0.1)

Not code. Before 0.1 there was no single statement of what the execution module *is*, so
each session re-derived vocabulary and re-discovered structure from source, and got it
subtly different each time. 0.1 verified 25 claims about the codebase against source and
**corrected 11 of them** — which is the number that justifies the exercise. A document
nobody checks is worse than none; this one was checked, and one claim in three was wrong.

It carries the vocabulary (§1), the four architecture decisions (§2), the standing rules
R-1…R-17 (§3), what already exists (§5), and the decision log (§12). Every phase 1 session
reads it first.

**Problem solved:** sessions building on a remembered system rather than the real one.

### The regression baseline — `RESIDENTIAL_BASELINE.md` + `tests_residential_baseline.py` (0.2a)

A read-only session that traced the entire Residential lifecycle from creation through
activation, design, BOQ, delivery, GRN, tasks, checklists, milestones, issues and documents
— and then pinned it with 92 tests. It found seven defects on the way (§11 of the
execution model, B-1…B-7) and fixed none of them, by instruction.

The tests exist to answer one question for every later session: *did I break something I
was not looking at?* Their design rule is stated in the file and is worth repeating —
**assert the relationship, never the gate.** A baseline test that pins "SCM may acknowledge
a BOQ" survives a permissions lockdown; one that pins "role == 'SCM'" fails it and teaches
nobody anything.

**Problem solved:** no way to tell a deliberate change from a regression.

### Access isolation — `permissions.py`, `decorators.py`, 19 endpoints (0.2)

The largest single risk reduction in phase 0. `ACCESS_ISOLATION_AUDIT.md` had inventoried
every endpoint and found **18 detail endpoints with no object-level check at all** and 8
Issue endpoints that let any authenticated user write to any project. 0.2 routed them
through `permissions.user_can_view_project()`, added `manageable_projects_q()` as the
queryset form of the ownership rule for list surfaces, gave System Admin an explicit
unrestricted branch, gated `dashboard_ceo`, gave every role in `tasks_drill_down` an
explicit branch with a denying `else`, and closed the `role_required()` fail-open.

**Problem solved:** *access isolation ships before any site engineer or warehouse keeper
receives a login* (D-4). Until 0.2, giving a site engineer an account meant giving them
GRN confirmation on every challan in the company.

### Consolidation — one BOQ snapshot, one acknowledgement, one M2 map (0.2b)

Four business acts were implemented two or more times with only one copy kept current.
0.2b made each one singular: `_boq_snapshot()`, `_apply_boq_acknowledgement()`,
`_FINANCE_TASK_TO_MILESTONE` with a **derived** inverse, and one module-level
`_PROFILE_TO_TASK_ROLE`.

**Why it ran between 0.2 and 0.2c rather than later:** a permissions lockdown applied to
each duplicate separately multiplies this class of defect instead of exposing it. You
deduplicate the act, then gate it once. That ordering is the transferable lesson of phase 0.

**Problem solved:** whether the system behaved correctly depended on which button the user
pressed.

### One Project resolution path — `_active_project()` (0.2c)

`project_delete` sets `is_deleted=True` and leaves `status` untouched. There are no custom
model managers. So a soft-deleted project keeps `status='Draft'` or `'Active'`, satisfies
every status precondition in the codebase, and roughly thirty write paths would happily act
on it — `project_activate` would seed 52 tasks and 3 milestones onto a record the Admin
believed was gone.

`_active_project()` (`views.py:2409`) is now the single resolution path, copying
`_opex_site()` — the precedent that is exactly why the design module carried none of these
findings. **R-16.**

**Problem solved:** deleted projects were not deleted in any sense that mattered.

### The state ledger — `StatusTransition` + `record_transition()` (0.3)

The codebase had ad-hoc terminal stamps — `Task.completed_at`, `Task.blocked_since`,
`Issue.resolved_at` — and no general history: no from-status, no actor role, no reason. You
could partly answer "how long was this blocked" and could not answer "how long between any
two states".

`StatusTransition` records subject, from, to, actor, **the actor's role copied at write
time**, reason, remark and timestamp, for six subject types. `record_transition()` **raises
and never swallows** — the deliberate opposite of `log_activity()`, and R-3 says the feed
and the ledger are allowed to fail differently. Append-only per R-4. It carries a
`client_uuid` from day one (R-14), because an idempotency key cannot be retrofitted once
real rows exist.

**Read §13 of the execution model before drawing any conclusion from a missing row.** A
partly-populated ledger is worse than an empty one if nobody knows where it stops.

**Problem solved:** no general answer to "who moved this, when, and why".

### Versioned task templates — `TaskTemplate` (0.4)

`build_residential_phases()` — a Python function — was the template. Editing it changed what
every future project got, with no version, no record of what last month's projects were
built from, and no way to have two.

The template is data now: `TaskTemplate` / `TaskTemplatePhase` / `TaskTemplateTask`,
seeded as `RESIDENTIAL` v1 (9 phases, 52 tasks) by migration `0067`, immutable once active
(**R-7**), with a partial unique constraint making "one active version per code" true at the
database rather than by convention. Verified not to have touched a single in-flight project
by fingerprinting three tables before and after.

**Problem solved:** the template could not be changed safely, and nothing recorded what any
given project had been built from.

### Versioned checklists — `Checklist` (0.5)

`ChecklistItemCompletion` foreign-keyed `ChecklistItem` with `CASCADE` and stored no copy of
the text it answered. Two failures with one root: rewording a label rewrote the question
against every completion already recorded, and **deleting an item deleted every completion
of it** — the tick, the photo, who checked it and when. One admin tidying a checklist erased
the inspection record of every site that had answered it. For CEIG paperwork and warranty
claims that is a compromised audit trail.

0.5 added `item_text_snapshot`, written in the same `save()` as `is_checked` (**R-8**), made
the FK `SET_NULL` — provenance, not the record — and versioned `Checklist` on **exactly**
`TaskTemplate`'s design, **sharing** its guard and its exception rather than restating them.

**Problem solved:** the inspection record was rewritable and destructible by ordinary admin
use.

---

## 2. What phase 0 fixed

### The access findings

`ACCESS_ISOLATION_AUDIT.md` ranked fifteen. Closed **fully**: 1 (any Site Engineer could
confirm a GRN on any project), 2 (any authenticated user could resolve/assign/comment on any
Issue), 3 (any role-matching user could change task status and due dates anywhere), 4 (30
write paths could mutate or resurrect a soft-deleted project — by 0.2c), 5 (any PM or BD
could rewrite milestone amounts), 6 (every role except PM could open any project overview),
7 (any authenticated user could upload files to any project), 8 (`dashboard_ceo` had no role
gate), 10 (System Admin's access came from a permissions module granting it nothing), 11,
12, 13, 14, 15 (the `role_required` fail-open).

Closed **partially**, and the distinction matters: **finding 9** (`tasks_drill_down` fell
through to the whole portfolio for six roles). Every role now has an explicit branch and the
final `else` denies, so a blank role — or any role added to `ROLE_CHOICES` later — sees
nothing until somebody chooses what it should see. **But Finance, SCM, BD, CEO and the admin
roles keep portfolio-wide task visibility**, now by an explicit branch instead of by falling
off the end of a chain. That is the 25 Aug decision, not an oversight;
`test_the_task_drill_down_is_still_portfolio_wide_for_scm` pins it and names itself as the
test to revisit when SCM is narrowed.

### The four defects

**B-1** (activation claimed "53 tasks created" against a template of 52), **B-2** (the M2
mapping duplicated four times, three copies naming a deleted task), **B-5** (BOQ
acknowledgement notified or did not, depending on the button), **B-7** (`boq_submit` raised
an unhandled `TypeError` on every call — a live 500 on a reachable URL). All four by 0.2b.

Two caveats worth carrying forward, both recorded in §11 of the execution model:

- **B-2 is fixed as a drift, not as behaviour.** M2 still syncs only from the task-row
  dropdown, never from the task detail page; every site keeps its except-pass wrapper, and a
  zero-row `.update()` still raises nothing.
- **B-7's fix included a deliberate behaviour change.** `boq_submit` adopted the inline
  branch's status precondition, so the two paths now agree on what may be submitted. It is
  the one intentional behaviour change in the 0.2b commit and it is named as such.

### The soft-delete gaps

0.2c replaced roughly thirty unfiltered `get_object_or_404(Project, …)` calls with
`_active_project()`, and pinned the result with 32 tests covering write refusal, child-object
refusal (an Issue or a challan reached through a deleted project), read refusal, and — the
half that is easy to forget — that a **live** project still works.

**The rule stops at `Project`.** `Issue`, `DeliveryChallan`, `BOQ` and relation traversals
such as `phase__project__is_deleted` each still need their own filter. There are still no
custom managers.

---

## 3. What phase 0 deliberately did not do

Each of these is a decision with a reason, recorded in §12 of the execution model. **None is
an oversight, and none should be "fixed" by a session that notices it.**

### The Finance and SCM narrowing

**Not done.** Task-based scoping gives **SCM exactly zero** visibility — all 11 SCM template
tasks are created unassigned and nothing ever assigns them — and gives **Finance one
hardcoded person everything**, because all 6 Finance tasks are back-assigned to a single
address. There is no third source of truth in the database.

Narrowing them would empty two dashboards on day one and break `boq_acknowledge`,
`create_delivery_challan` and the payment path system-wide. The audit's own recommendation
was to defer, and it was taken. **Revisit when an assignment table exists** (Q-E3).

### `ProgramAssignment`

**Designed and not built.** `ACCESS_ISOLATION_AUDIT.md` Task E specifies its shape,
constraints, interaction with `user_can_view_project()`, and backfill policy — including the
D-1 lesson about partial unique constraints, which applies directly.

It is required before Finance/SCM program scoping can work **and before §4's claim that "the
PM owns a whole Program" has any representation at all**: `Program` has no `assigned_pm`, no
manager FK and no M2M, only `created_by`. Phase 0 answered "not yet", not "what".

### BD and Design Head scoping

**Confirmed unchanged, and note precisely what was confirmed.** Neither has a per-user term
anywhere in the database to scope on — there is no BD field on `Project` at all, and Design
Head is a capability flag granting visibility and authority through two separate mechanisms.
They were not confirmed *correct*; they were confirmed *unscopeable today*. Q-E2 is closed on
that basis and reopens with the assignment table.

### The design module's transitions

**Not instrumented, and this is the largest deliberate gap in the ledger.**
`DesignAssignment` has 14 statuses — the richest workflow in the product — and
`DesignAttempt` has its own lifecycle plus several existing ad-hoc terminal stamps. Neither
writes a `StatusTransition` row.

Instrumenting them means editing `design_views.py`, which has been correctly scoped and left
untouched all programme. **It is a session of its own**, and it must also decide whether the
existing stamps become transitions or stay beside them. Until then, §13 is the only thing
that tells you a missing design row means "not instrumented" rather than "did not happen".

### Also not done

- **No OPEX or CAPEX task template.** 0.4 made one possible; prompt 1.3 creates it, and it
  needs the phase and task list from the projects team first (B-09).
- **No template- or checklist-authoring UI.** Versions are created through migrations or the
  Django admin, which refuses to open a form on a non-draft version.
- **No checklist gate on task completion** — that is B-6, and it belongs to 2.3. 0.5
  versioned and snapshotted the checklist and explicitly did not gate on it.
- **No project closure** — B-3, phase 5, alongside COD and HOTO.
- **`TaskDurationTemplate` not dropped.** Superseded and unread, but dropping a table is its
  own decision with its own migration.

---

## 4. What phase 1 inherits

### The regression net — six files, 287 tests

| File | Tests | What it protects |
|---|---|---|
| `tests_residential_baseline.py` | 92 | **The lifecycle itself.** Activation invariants (52 tasks / 9 phases / 3 milestones / the Finance back-assignment), dashboard reachability, the BOQ ladder, delivery and GRN, milestones and payment, task progression and due-date cascade, the issue lifecycle, document upload, notifications. Asserts *relationships*, never gates — which is why it survived the 0.2 lockdown |
| `tests_access_isolation.py` | 38 | **The lockdown.** The 18 previously-unscoped endpoints, the 8 Issue endpoints, issue-assignee narrowing, role-gate scoping, the dashboard gates, the profile-less user, the denial *shape* (403, not a redirect), and System Admin staying unrestricted. Several tests name the audit finding they close |
| `tests_soft_delete.py` | 32 | **R-16.** That deletion leaves `status` alone, that writes and reads refuse a deleted project, that child objects reached *through* one refuse too — and that a live project still works, which is the half that catches an over-tight fix |
| `tests_status_transition.py` | 39 | **The ledger's contract.** That `record_transition()` raises rather than swallows, that `actor_role_code` is a copy and not a join, append-only enforcement, atomicity with the status change it records, `client_uuid` replay, every instrumented subject type, and dwell-time computation |
| `tests_task_template.py` | 43 | **R-7 for tasks, and B-10.** That activation output is unchanged by the migration, durations resolve identically, versions are immutable, `activate()` promotes and archives in one transaction, the duration screens stay read-only — and `InFlightProjectIsolationTests`, which is the guard on B-10 staying closed |
| `tests_checklist_snapshot.py` | 43 | **R-8 and R-7 for checklists.** That the snapshot is written in the same `save()`, that history is no longer rewritable or destructible, immutability, versioning, that `_checklist_for_task()` resolves through the *family* so publishing v2 moves the task onto it, and that the `is_active` property shim behaves like the column it replaced |

**Run them the only way that works:**

```
python manage.py test projects --settings=solarpms.test_settings
```

Without `--settings`, suites error in `setUp` — that is a wrong invocation, not a broken
suite. There is **one known pre-existing failure**: a SQLite constraint-name assertion in
`tests_design_part46`. Anything else is yours.

### One outstanding chore in the net

0.5's R-7 guard forbids adding an item to an active checklist — correctly — and four tests
in two files 0.5 was not allowed to modify build their fixture as *create active checklist,
then add an item*. They fail at the fixture line with `TemplateVersionLocked`, not at an
assertion:

- `tests_residential_baseline.py` — `test_the_assigned_user_completes_a_checklist_item_with_a_photo`
- `tests_residential_baseline.py` — `test_a_checklist_item_cannot_be_checked_without_a_photo`
- `tests_soft_delete.py` — `test_checklist_item_complete_refuses_a_deleted_project`
- `tests_soft_delete.py` — `test_a_live_project_still_takes_a_checklist_completion`

**All four are fixed by reordering the fixture to *create draft, add items, `activate()`*.
No assertion changes.** The behaviour each asserts is covered meanwhile by
`tests_checklist_snapshot.py`. Whoever is next allowed to touch those files should make the
reorder and delete §15's subsection about it.

### The documents

| File | What it is for |
|---|---|
| [`docs/execution-model.md`](docs/execution-model.md) **v1.3** | **The contract.** Read first, every session. Vocabulary, the four architecture decisions, R-1…R-17, what exists, known problems, open questions, known defects, the decision log, and §13/§14/§15 on the ledger, templates and checklists |
| [`docs/delivery-state-authority.md`](docs/delivery-state-authority.md) | Why delivery state has two sources, and the condition under which that should be revisited. Read before touching the CEO, PM or SCM delivery surfaces |
| `EXECUTION_MODULE_DEFERRED.md` | Findings recorded and deliberately not fixed (**R-12**). Phase 0 added A1–A5 |
| `RESIDENTIAL_BASELINE.md` | What the Residential lifecycle actually does, step by step |
| `ACCESS_ISOLATION_AUDIT.md` | The endpoint inventory, the role matrix, the fifteen findings, and the `ProgramAssignment` design |
| `SECONDARY_FINDINGS.md` | The production data measurements behind the delivery-state trade |
| `DESIGN_MODULE_DEFERRED.md` | The design module's own deferrals. J3 and K5 are referenced by phase 0's A2 and A3 |

### The rules that are new since phase 0 started

**R-16** — a Project is resolved through `_active_project()`. **R-17** — versioned templates
follow the `TaskTemplate` pattern: integer version on the row, partial unique for one active
version, immutability in `save()`/`delete()`, instances take copies and any FK back is
provenance only. Both were established in code by phase 0, not proposed by it. R-16 is
written with its five surviving exceptions named, because a rule that overstates what the
code does is worse than no rule.

### The immediate next step

**Prompt 1.1 is the riskiest session in the programme and needs its own review before it is
written.** It must alter `uniq_active_site_group_membership` from `(project)` to
`(project, group_type)` — a migration, so **R-1** applies — and audit *every* caller of
`active_group_membership()`, whose `.first()` currently documents itself as "picking the only
row that can exist, not the first of several". That single result computes `in_draft_group`,
the gate admitting a released design to a change request. Adding a second membership type
without fixing every caller makes that gate order-dependent.

---

## 5. The two operational items

**Neither is a session's to decide.** Both are for the product owner. Both are recorded as
B-19 and B-20 in §8 of the execution model so that no future session answers them by guessing.

### B-19 — who actually confirms a GRN?

0.2 scoped `confirm_grn` to **engineers holding a task on that site**. The role gate is kept
and the scope check added beside it — two independent questions, two independent answers —
and the scope check routes through `user_can_view_project()`, whose Site Engineer branch is
exactly `project.phases.filter(tasks__assigned_to=profile).exists()`, the same relationship
`dashboard_site_engineer` scopes on.

**That is the tighter of the two readings**, and it is the one audit finding 1 demanded: before
it, any site engineer in the company could sign off receipt of materials at a site they had
never been to.

**The question the code cannot answer is whether that matches the job.** If receipt is in
practice recorded by whoever happens to be at the warehouse when the lorry arrives, rather
than by the engineer who holds the task, then this gate refuses a legitimate act and someone
will work around it. **This must be settled before a warehouse keeper is given a login** —
which the execution model schedules for phase 1, not for deferral (§4, and B-14 on how many
warehouses there are and how keepers are assigned to them).

If the answer is "whoever is present", the fix is a warehouse-scoped grant, not a loosening
of the site-engineer rule back to role-only.

### B-20 — does a profile-less superuser exist on production?

0.2 closed a fail-open in `role_required()`: a user with **no `UserProfile`** was treated as
`'Admin'` and admitted to all 33 `@role_required(['Admin'])` screens in `views.py` — the whole
admin panel, master switches included — while every helper in `permissions.py` correctly
refused the same user. A `createsuperuser` account has no `UserProfile`, so "avoid a hard
crash" had quietly become "hand out the admin panel". The navigation helpers had the matching
half: they answered `/dashboard/admin/` for the same user, a destination the authorisation
layer would then refuse everywhere.

Both now deny, and `role_required()` returns **403** rather than redirecting.

**The operational consequence:** if this installation is administered through a profile-less
superuser, **that account is now locked out.** The code carries the deployment note inline.

**The fix is to give that account a `UserProfile` with `role='Admin'` — never to restore the
fallback.** Holding no role is not a reason to be given the most powerful one.

---

## 6. How to read a phase 0 claim you doubt

Every structure above was verified against committed source during the 0.6 close-out, not
transcribed from the prompts' own reports — and the close-out found four places where the two
diverged, all now recorded (§6 and §11 of the execution model, and A3–A5 in
`EXECUTION_MODULE_DEFERRED.md`).

**Do the same.** If a document and the code disagree, the code is what runs. Report the
contradiction, describe the code, and correct the document — that is what R-12 and this
file's existence are both for.
