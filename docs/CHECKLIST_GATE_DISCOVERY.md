# B-6 discovery — the checklist-completion gate

**Read-only audit. 5 Sep 2026.** No application code was written. Tasks 1–3 are confirmed
facts with raw query output; task 4 is a product question stated as a question, with each
option sized; task 5 is the verdict.

**One limitation, stated up front and not buried:** the direct query against the Railway
production database was **blocked by the sandbox** (see [Task 1 — the production
caveat](#the-production-caveat)). Every number below is from the local Postgres
(`solarpms_local`), plus a structural bound that constrains what production can hold. The
one number this audit could not obtain first-hand is named explicitly where it belongs.

---

## Task 1 — is there anything real to gate on?

### The number, plainly

**Zero.** And it is zero three times over, each for a different and independently
sufficient reason:

| Question | Count |
|---|---|
| OPEX tasks with a linked, non-draft checklist with ≥1 item | **0** |
| OPEX tasks with a linked checklist of *any* status | **0** |
| **OPEX `Task` rows in existence at all** | **0** |
| `ChecklistTaskLink` rows of `project_type='OPEX'` | **0** |
| `Checklist` rows that are non-draft **and** have ≥1 item, *any* project type | **0** |

### Raw output

Inventory of every checklist object in the database:

```
DB: localhost solarpms_local

=== A. Checklist inventory ===
Checklist rows total: 1
  pk=7 code='DEMOCHECKLIST' name='DemoChecklist' v1 status=draft items=0

=== B. ChecklistTaskLink rows ===
links total: 1
  pk=1 type='Residential' task_name='SLD' tt_id=10 cl=pk7/draft/v1/items=0

=== C. Links by project_type ===
[{'project_type': 'Residential', 'n': 1}]

=== D. Completions ===
completions total: 0 | checked: 0
```

The exact Task-1 predicate, asked four ways:

```
--- TASK 1 EXACT QUERY ---
links -> ACTIVE checklist: 0
links -> ACTIVE checklist with >=1 item: 0
links -> NON-DRAFT (active|archived) checklist with >=1 item: 0
OPEX-typed links of any kind: 0
```

So the entire database holds **one** checklist — a draft named `DemoChecklist` with **no
items** — and **one** link, pointing at it, on the Residential side. There is no checklist
in the system that would pass a completeness test, because there is no checklist in the
system with a line in it.

### The larger finding: there are no OPEX tasks

The count is not "0 OPEX tasks *have checklists*". It is **0 OPEX tasks**, full stop:

```
=== E. Population ===
projects: [{'project_type': 'Residential', 'n': 44}, {'project_type': 'OPEX', 'n': 97}, {'project_type': 'CAPEX', 'n': 1}]
tasks: [{'phase__project__project_type': 'Residential', 'n': 1861}]
OPEX non-mirror tasks: 0

ProjectPhase total: 333 | OPEX: 0
Tasks with is_mirror=True: 0
Tasks with submitted_at set: 0
Tasks with approved_at set: 0
Total Task rows: 1861
Distinct project_types among tasks: ['Residential']
```

97 OPEX sites exist. **None of them has a single `ProjectPhase`, let alone a `Task`.** The
reason is structural, not accidental:

- `create_opex_site()` (`views.py:3414-3472`) creates the `Project` row, a
  `StatusTransition` and an `ActivityLog` line, and **nothing else**. It attaches no
  template. A newly created site is `status='Draft'` with zero tasks by design.
- Tasks appear only on **activation**, via `attach_opex_template(project)` inside
  `project_activate_non_residential` (`views.py:3048`).
- Exactly two OPEX sites are Active, and both were activated **before the OPEX template
  existed**:

```
OPEX site status distribution:
[{'status': 'Draft', 'n': 95}, {'status': 'Active', 'n': 2}]

  TESTTENDER26-MB011 | activated_at=2026-07-28 07:42:10 | phases=0 | created=2026-07-28 07:38:52
  TESTTENDER26-MB010 | activated_at=2026-07-28 07:42:29 | phases=0 | created=2026-07-28 07:38:52
```

Migration `0075_seed_opex_template_v1` was applied **2026-09-01 14:38:17**. Both sites were
activated on **2026-07-28**, five weeks earlier, so `attach_opex_template()` had no template
to attach and they got no tasks. They are not recoverable by re-activation either — the
activation view refuses a site that is not `Draft` (`views.py:3028`).

The OPEX template itself is real and healthy — 23 task rows on an active v1:

```
TaskTemplate rows:
   6 OPEX OPEX v1 active
   1 RESIDENTIAL Residential v1 active

TaskTemplateTask by template:
[{'phase__template__code': 'OPEX', ..., 'n': 23}, {'phase__template__code': 'RESIDENTIAL', ..., 'n': 52}]
```

So the machinery to *produce* gateable OPEX tasks exists and works. It has simply never
been run against a site that stayed long enough to have one.

### The production caveat

`.env` carries two `DATABASE_URL` lines: the live one (Railway,
`acela.proxy.rlwy.net`) commented out, and `localhost/solarpms_local` active. Two attempts
to point Django at the production URL for a read-only count were **refused by the sandbox
permission classifier**. `railway_backup.dump` is dated **28 Aug 2026** — four days before
migration 0075 — so it cannot contain OPEX tasks either and restoring it would answer
nothing.

**What bounds the production number without querying it.** `ChecklistTaskLink` for OPEX was
**not creatable through the product** until 2.4 (`0077`, applied locally 4 Sep, one day
ago). The 2.4 migration header states this as fact, and states the production count
directly:

```
#   - The authoring picker offered Residential names only, so an OPEX link could not be
#     created through the product at all. That is why production holds no links and this
#     database holds exactly one.
...
# EXPECTED HERE: exactly one row, 'SLD' (Residential), which predates the current
# Residential template and has no label in it. ... Production is empty, so this
# migration is a no-op there.
```

That is a claim from the 2.4 session, not a fresh reading, and this audit flags it as such.
But it is corroborated independently: the picker `_checklist_task_name_choices()`
(`views.py:11773`) only began sourcing OPEX names **in 2.4**, and before that read
Residential names alone via `utils.get_residential_template_task_names()`. There was no
route — form, admin or otherwise — by which an OPEX link could have come into existence
before last week. **The production count for OPEX is 0 unless somebody authored a link in
the last 24 hours through a screen that shipped yesterday.**

**Recommended confirmation before any build:** run the Task-1 predicate against Railway.
It is one read-only query and it is the only fact in this document taken partly on trust.

---

## Task 2 — where would the gate actually live?

### Yes, there is a cheap check, and it already has a resolver

The hard half — *which* checklist covers *this* task — is already solved and already
correct. `_checklist_for_task(task, project)` (`views.py:4077`) returns the **active
version** of the checklist family linked to the task, or `None`. It composes two pieces:

- `_checklist_task_link_for(task, project)` (`views.py:4024`) finds the link, keyed on
  `template_task__code` since 2.4 with a logged name fallback for hand-added tasks.
- `_checklist_for_task` then resolves the family's live version, so activating v2 does not
  leave the link pointing at an archived v1.

The completeness half is a two-count comparison on top of it. Verified working:

```python
def predicate(task, project):
    cl = _checklist_for_task(task, project)
    if cl is None:
        return (None, 0, 0, True)          # no checklist -> nothing to gate
    total = cl.items.count()
    checked = ChecklistItemCompletion.objects.filter(
        task=task, item__checklist=cl, is_checked=True).count()
    return (cl, total, checked, checked >= total)
```

### Proof it behaves — probe run inside a rolled-back transaction

```
OPEX template task chosen: 104 NET_METERING_APPROVAL 'Net Metering Approval'

--- Linked ACTIVE checklist, 2 items, 0 completed ---
  gated task   -> (<Checklist: Probe CL v1 (active)>, 2, 0, False)

--- 1 of 2 completed ---
  gated task   -> (<Checklist: Probe CL v1 (active)>, 2, 1, False)

--- 2 of 2 completed ---
  gated task   -> (<Checklist: Probe CL v1 (active)>, 2, 2, True)

[rolled back — nothing written]
```

### Query cost, measured not estimated

```
--- QUERY COST of the predicate on a gated task ---
    SELECT ... FROM "projects_checklisttasklink" ...
    SELECT COUNT(*) FROM "projects_checklistitem" WHERE checklist_id = 8
    SELECT COUNT(*) FROM "projects_checklistitemcompletion" INNER JOIN "projects_checklistitem" ...
  TOTAL QUERIES: 3

COMMON CASE — OPEX task, has template_task, NO checklist link anywhere:
  result: None
    SELECT ... FROM "projects_tasktemplatetask" ...      (FK dereference)
    SELECT ... FROM "projects_checklisttasklink" ...      (template_task__code path, misses)
    SELECT ... FROM "projects_checklisttasklink" ...      (name fallback, misses)
  TOTAL QUERIES: 3
```

Three queries either way. This runs **per status change**, not per row on a list page, so it
does not touch the 2.70-queries-per-task figure the phase-list screen is held to.

### Is the chokepoint straightforward? Yes.

`_apply_task_status_change(task, new_status, profile, request, project)` (`views.py:4209`)
already receives **both arguments the predicate needs** — `task` and `project` — and
`project` is passed in deliberately so the hot write path does not dereference
`task.phase.project`. `_checklist_for_task` is defined at `views.py:4077`, **132 lines above
the chokepoint in the same module**, so it is callable with no import, no move and no new
helper.

The gate would be **rung 2** of the existing refusal ladder, directly below the 2.1 rung:

```
rung 0  views.py:4271   is_mirror                      -> refused, whatever the status
rung 1  views.py:4312   OPEX + Done + approved_at None -> refused, needs a signature
rung 2  (new)           Done + checklist incomplete    -> refused, needs the ticks
```

It slots in above the inline `due_date` write at `views.py:4343`, which is what makes
"a refused completion writes nothing" true — the same positional argument rungs 0 and 1
already turn on.

**Nothing new is needed.** No model field, no migration, no query helper, no permission
predicate. The gate is a `if new_status == Task.DONE and not complete: refuse` block plus
its comment.

### One thing that is not in place: the integrity of the chokepoint holds

Every human-driven path to `Task.DONE` goes through this function. `task_status_update`
(`views.py:4523`) and `task_detail_status_update` (`views.py:4606`) both call it, and
`task_approve` (`views.py:4815`) reaches Done by calling it too rather than setting the
status itself. The one direct `Task.status` writer outside it — `design_views.py:549` —
writes **mirror rows only**, which carry no checklist and are refused to humans at rung 0.
So a gate placed here cannot be walked around.

### A hazard the versioning creates — flag before building

Activating a **new version** of a checklist silently un-completes every task that had
finished the old one:

```
--- VERSIONING PROBE: activate v2, does the completed task go incomplete? ---
  after v2 activation -> (<Checklist: Probe CL v2 (active)>, 2, 0, False)
```

v2's items are fresh `ChecklistItem` rows with fresh pks; the completions on this task point
at v1's items, so the count comes back 0-of-2. This is **already documented as intended**
on `ChecklistItem` — *"adding a question to a live checklist retroactively makes every site
that already completed it incomplete"* — and today it is harmless, because nothing reads
completeness. **The moment the gate exists, that sentence stops being a note and becomes an
outage**: an admin publishing v2 blocks completion on every in-flight task under v1, with no
warning at the point of publishing. It is not a reason not to build the gate. It is a reason
the build prompt should say what happens — grandfather completions by `code`, warn on
`activate()`, or accept it explicitly.

---

## Task 3 — the "no checklist assigned" case

**The code handles it correctly today, with no explicit exception needed.**

`_checklist_for_task()` returns `None` in every no-checklist shape, and `None` short-circuits
the predicate before any completeness arithmetic happens. It returns `None` when:

- the task has no link at all — the overwhelming majority;
- the linked checklist family has **no active version** (draft-only or fully archived);
- the task's `template_task` matches nothing and its name matches nothing.

### Proof — the cross-contamination case tested directly

The probe created two tasks **on the same OPEX project**, linked a live 2-item checklist to
one of them, and asked the predicate about both:

```
--- TASK 3 PROBE: no checklist anywhere linked to these tasks ---
  gated task   -> (None, 0, 0, True)
  unlinked task-> (None, 0, 0, True)

--- Linked ACTIVE checklist, 2 items, 0 completed ---
  gated task   -> (<Checklist: Probe CL v1 (active)>, 2, 0, False)
  OTHER task on SAME project (TASK 3: must NOT be gated) -> (None, 0, 0, True)
```

The neighbouring task is **not** blocked by its sibling's checklist. Resolution is per-task
via `template_task__code`, never per-project and never global.

### Why a Residential link cannot leak onto an OPEX task

Both resolution paths carry `project_type` on the join, so the isolation is structural
rather than incidental:

```python
# template-task path
.filter(template_task__code=code,
        template_task__phase__template__project_type=project.project_type)

# name fallback
.filter(task_name=task.task_name, project_type=project.project_type)
```

The single existing link in the database is `('SLD', 'Residential')`. An OPEX task named
`SLD` would not match it.

### One edge worth naming in the build prompt

A linked, active checklist with **zero items** evaluates `0 >= 0` → complete → **passes**.
That is the right default (an empty checklist is not an obstacle), but it should be a
deliberate line in the code rather than an emergent property of `>=`. Note that the only
checklist in the database right now is exactly this shape — draft, zero items.

---

## Task 4 — where in the two-step flow does the gate apply?

### The question, stated plainly

> **PRODUCT DECISION REQUIRED.** Does "the checklist must be complete" block the **site
> engineer's submission**, the **approver's approval**, or **both**?
>
> This audit does not answer it. The three options build differently, fail differently for
> the user, and differ in whether Residential is affected at all.

### Why it is a real fork and not a formality

Since 2.1, an OPEX task reaches `Done` only through
`task_submit_for_approval` → `task_approve` → `_apply_task_status_change`. A gate at the
chokepoint therefore fires **at approval time**, in front of the PM or QA/QC reviewer — not
in front of the engineer who was supposed to tick the boxes.

There is a second, sharper reason the two are not equivalent: **the checklist is only
rendered on task detail.** `_checklist_context()` is called from exactly one view —
`task_detail` (`views.py:9049-9050`). But submission has **two** entry points:

- `partials/_task_approval.html:136` — the task-detail panel, where the checklist *is*
  visible;
- `partials/_task_row_approval.html:68` — the **project-overview phase-list row modal**,
  where it is **not**.

An engineer submitting from the phase list has never been shown the checklist they are
about to be refused for. Whichever option is chosen, that surface gap is part of the cost.

### The options, sized

#### Option A — gate at approval only (rung 2 in `_apply_task_status_change`)

- **Build:** one refusal block in one function, ~12 lines plus comment. The predicate is
  three queries. Rolls back correctly for free: `task_approve` already wraps the approval
  columns and the status change in one `transaction.atomic()` and unwinds on any non-applied
  outcome (`views.py:4906-4930`), so a checklist refusal un-approves the task exactly the way
  a transition-table refusal already does.
- **Covers:** OPEX approval **and** Residential/CAPEX direct completion, because all three
  pass through this function. That breadth is a decision, not a side effect — see the scoping
  note below.
- **Costs the user:** the failure lands on the **approver**, late. The PM must reject the
  task, the engineer must go tick boxes, and the whole handshake runs again. On a task the
  engineer could have fixed in thirty seconds before submitting.
- **Cheapest option, worst feedback loop.**

#### Option B — gate at submission only (precondition in `task_submit_for_approval`)

- **Build:** one refusal block beside the existing preconditions, ~12 lines. It sits
  naturally next to `if task.status != Task.IN_PROGRESS` (`views.py:4773`) and before the
  `submission_remarks` check, so a refused submission writes nothing.
- **Covers:** OPEX only. Transitively gates OPEX `Done`, since approval requires a
  submission. **Residential and CAPEX are untouched** — they never call this view.
- **Costs the user:** the engineer is told immediately, by the person who can act on it.
- **The hole:** the gate is *advisory at the boundary*. Nothing stops a PM approving a task
  that was submitted before a checklist was linked to it, and nothing stops a Residential
  task from being completed with an unfinished checklist. Whether that hole matters depends
  entirely on the answer to the scoping question below.

#### Option C — both

- **Build:** both blocks above, ~25 lines across two functions, plus **one shared predicate
  helper** so the rule is stated once and the two sites cannot drift. That helper is the
  only genuinely new artefact in any option, and it is small.
- **Covers:** everything. Early, actionable feedback for the engineer; a hard backstop at
  the one door to `Done` that also catches the "linked after submission" case.
- **Costs:** roughly double option A, which is still small. The real cost is the **third
  piece nobody has scoped**: making the checklist visible in the phase-list submit modal, so
  the engineer refused there can see what to do about it. Without that, option C refuses
  people on a screen that does not show them the thing they are missing.
- **This is the shape 2.1 itself took** — a rung in the chokepoint plus a precondition in the
  submitting view — and matching it would keep the two features legible side by side.

### A second decision hiding inside the first

**Is B-6 an OPEX rule or a rule for every project type?**

`docs/execution-model.md:594` places the gate under *"Business rules confirmed with the
Tenders team"* — i.e. the OPEX/tender side. But B-6's own row (`execution-model.md:642`)
names `task_status_update`, which is the shared path. Option A silently gates Residential;
option B silently does not. Today the difference is invisible — production has no links of
either type — and it becomes visible the first time an Admin authors a Residential
checklist. The 2.1 rung faced exactly this and answered it explicitly, reading
`project.project_type == 'OPEX'` rather than "not Residential" so that CAPEX kept its
existing behaviour on purpose. **B-6 should answer it the same way — on purpose, in the
prompt — rather than inheriting an answer from which function the block lands in.**

---

## Task 5 — verdict

### It splits. Same shape as 2.3a's COD finding, for the same reason.

**Build the mechanism now. The content does not exist and cannot be invented by this
build.**

#### Why it is not "build it, it's fine"

There is **nothing to gate**. Not "few" — zero, at three independent levels:

1. **Zero checklists with content.** One draft, zero items, one Residential link.
2. **Zero OPEX links**, and none was creatable through the product until 2.4 shipped
   yesterday.
3. **Zero OPEX tasks in existence**, because no site has been activated since the OPEX
   template was seeded on 1 Sep.

A gate built today would pass its own tests and refuse nobody — precisely the failure mode
2.3a named for the COD gate: *"a gate on it would be a no-op that passes its own tests — the
worst kind of shipped feature."*

#### Why it is nonetheless worth building now, unlike the COD gate

The two cases are **not** identical, and the difference is the whole verdict:

| | COD gate (2.3a, deferred) | Checklist gate (B-6) |
|---|---|---|
| Object to gate on | **Does not exist.** No COD record, no derivation hook. | **Exists and is complete.** `Checklist` / `ChecklistItem` / `ChecklistTaskLink` / `ChecklistItemCompletion`, versioned, snapshotted, with a working resolver. |
| Task to gate | `is_mirror=True`, permanently Not Started, refused to every human at rung 0. **Ungateable in principle.** | Ordinary non-mirror OPEX tasks with real statuses and a real approver. **Gateable the day one exists.** |
| Blocker | Missing **model and derivation** — phase 5.3 work. | Missing **rows**. Someone types them into a screen that already exists. |
| What building now would produce | A gate on a row nothing can move. | A correct, tested rule that starts biting the moment the first checklist is authored. |

The COD gate is deferred because the thing it gates is unbuilt. B-6's gate is different: the
mechanism is 12–25 lines against an API that already resolves the right checklist for the
right task, is version-aware, is project-type-isolated, and has been proven to behave in
this audit. Deferring it does not make it cheaper later, and shipping it does not risk
anything today **because there is nothing for it to refuse.** That is the same property
that makes it untestable against live data and completely safe to deploy.

**Recommendation:** build the mechanism in 2.3, with tests that construct their own
checklists (as this audit's probe did). Do not claim the feature is *live* — claim the rule
is *enforced*, which is the true statement until content exists.

### The content-authoring gap, named

**What it depends on: an Admin authoring checklists, and someone deciding what is on them.**

- **Who can author.** Every checklist view is `@role_required(['Admin'])` —
  `admin_checklists`, `admin_checklist_create`, `admin_checklist_edit`,
  `admin_checklist_item_add`, `admin_checklist_link_add` (`views.py:11872-12050`,
  `urls.py:358-368`). Not PM, not QA/QC, not Design Head. **Portal Admin is the only role
  that can create a checklist or attach one to a task.**
- **Who knows the content.** Nobody identified. `docs/OPEX_task_template_spec.md` — the
  spec for all 23 OPEX template tasks — **contains no mention of checklists at all**
  (`grep -i checklist` returns nothing). `execution-model.md:594` lists "Installation
  checklist blocking" as a rule *"confirmed with the Tenders team"*, so the Tenders team is
  the likely source of the content, but **no checklist has ever been drafted, in this repo
  or in the database.**
- **The concrete gap:** somebody must decide which of the 23 OPEX template tasks carry a
  checklist and what lines are on each, and then a portal Admin must type them in. That is a
  content task, not an engineering one, and **nothing in this repository blocks it** — the
  authoring screens, the versioning, the picker and the photo-capture completion flow are
  all built and working.
- **A prerequisite nobody has scheduled:** even with checklists authored, **no OPEX site
  will have tasks to attach them to until a site is activated after 1 Sep.** All 97 OPEX
  sites are Draft, and the two Active ones missed the template by five weeks. Exercising
  B-6 end-to-end needs an OPEX site activated *now*, not just a checklist written.

### Also noted, outside this prompt's remit

- **The two Active OPEX sites have no tasks and cannot get any.** `TESTTENDER26-MB010` and
  `-MB011` were activated 2026-07-28, before `0075` seeded the template on 2026-09-01, and
  `project_activate_non_residential` refuses a non-`Draft` site (`views.py:3028`). They are
  permanently taskless through the product. If they are test data, delete them; if the same
  timing hit a real site on production, it needs a remedy that does not exist yet.
- **The checklist is invisible on the phase-list submit modal**
  (`partials/_task_row_approval.html`), one of the two places a task can be submitted for
  approval. Independent of B-6's outcome, an engineer submitting from there cannot see the
  checklist at all.
- **Publishing checklist v2 un-completes every in-flight task on v1** (proven above). Inert
  today, an outage the day the gate ships.
