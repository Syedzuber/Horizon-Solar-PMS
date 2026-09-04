# OPEX Template Audit — prompt A-1.3

**Read-only audit run 30 Aug 2026 on branch `execution-phase-1`, commit `feae542`.**
Checks `docs/OPEX_task_template_spec.md` v1.0 against source before prompt 1.3 builds it.

Everything below was read from the code. Where a document and the code disagree, the code is
described and the document is named as wrong.

**Verification state.** `python manage.py check` — 0 issues. `manage.py test projects
--settings=solarpms.test_settings` — **795 tests, 1 failure, 0 errors**; the failure is the known
pre-existing SQLite constraint-name assertion in
`tests_design_part46.RaisingTests.test_02_a_second_pending_request_is_refused_by_the_database`.
Suite is at baseline.

**Volume figures below are read from the local database, which carries the production restore**
(95 Draft OPEX sites, 3 Programs, 87 DesignAssignments). They are stated as measured, not
estimated.

---

## 0 · PRE-CONDITION DEVIATION — read this first

`docs/OPEX_task_template_spec.md` is **present on disk and still untracked** (`?? docs/…` in
`git status`). Pre-condition 3 requires it committed. It is the third session in a row to find it
so.

The audit proceeded because the guard's stated purpose — *"do not reconstruct it, do not proceed
from a copy pasted into the session"* — is satisfied: this is the original file left by the
authoring session, not a reconstruction, and nothing was pasted into this session. **The record is
not forked. It is merely not yet in the repo.**

**Action for the product owner:** `git add docs/OPEX_task_template_spec.md` and commit it before
1.3 runs. This audit deliberately did not commit it — the MODE permits exactly one new file, and
silently committing someone else's document is not this session's call.

---

## 1 · TASK Z — the seven claims

### Corrections first

**Claim 1 — CORRECTED (materially).**
The true half: `attach_residential_template()` in `projects/utils.py` is the only
template-attachment path, and `projects/views.py : project_activate` is its only production caller
(the other references are tests and comments).

The false half: **OPEX/CAPEX activation does not "only stamp `activated_at`."** `project_activate`
does four more things for every project type, and two of them are hard blockers for 1.3:

| | What it does | Applies to OPEX? |
|---|---|---|
| a | `@role_required(['PM','Project Coordinator'])` + `_pm_owns_project()` | yes |
| b | **Refuses without `assigned_design_id` in POST**, then `get_object_or_404(UserProfile, role='Design')` | **yes — and this is a blocker** |
| c | `status='Active'`, `activated_at=now()`, `record_transition(...)` | yes |
| d | `attach_residential_template()` | Residential only |
| e | **Creates M1/M2/M3 `PaymentMilestone` rows** — unconditional, all project types | **yes** |
| f | Success message: Residential says "N tasks created", other types say *"Add tasks manually using Add Task."* | yes |

(b) is the finding. Of the 96 live OPEX sites, **96 have `assigned_pm` but only 5 have
`assigned_design`.** Design allocation for OPEX lives on `DesignAssignment.assigned_to`, not on
`Project.assigned_design` — 87 `DesignAssignment` rows exist against 5 populated FKs. So today a
PM cannot activate 91 of the 95 Draft sites without picking a designer through a control the
design module does not read. **1.3 cannot reuse `project_activate` unchanged.**

(e) is the second finding: activating 95 OPEX sites through this view mints **285 Residential
payment milestones** (M1 On Survey Completion / M2 On Material Supply / M3 On Commissioning) on
tender sites whose commercial model is a tender, not a three-milestone residential contract.

**Claim 3 — CONFIRMED, with the mechanism named.** Migration
`0067_seed_residential_template_v1.py` seeded RESIDENTIAL v1. Its shape is the precedent 1.3 must
follow, and it is more careful than "insert some rows":

- it **imports `projects.utils` deliberately** and says why in a header comment — the seeding
  helper takes its model classes as arguments, so it operates on `apps.get_model()` historical
  classes;
- it is **idempotent** — `if TaskTemplate.objects.filter(code=…).exists(): return`, guarding a
  database whose template was bootstrapped rather than migrated;
- it calls the shared `utils.seed_task_template_version()`, which creates the version as
  `draft`, writes phases and tasks, then flips it to `active` via `filter().update()` +
  `save(update_fields=['status'])` — **never created active**, because that is the only order the
  R-7 guards permit on the concrete models;
- it **backfills `Task.template_task`** by `(phase label, task label)`, restricted to
  `project_type='Residential'` so a hand-made OPEX task cannot collide by name, streamed with
  `.iterator(chunk_size=2000)` and `bulk_update(batch_size=1000)`;
- it ships a **real reverse** (`unseed_v1`) that nulls the provenance explicitly rather than
  leaning on `SET_NULL` cascade.

1.3's seed needs (a) idempotency, (b) `seed_task_template_version()`, (c) **no backfill** — there
are zero non-Residential `Task` rows to give provenance to (measured: all 1,861 tasks belong to
Residential projects), and (d) a reverse.

**Claim 7 — CORRECTED. `DESIGN_RELEASED` is set by the Design Head, but it is NOT terminal.**

Set in exactly one place: `design_views.py : design_head_qc_pass`, inside `transaction.atomic()`,
writing `released_at`, `released_by`, `status`. That half holds.

But there is one route out of `released`, and it matters to the spec:
`design_change_request_form` admits a change request against a `released` assignment **when the
site sits in a draft (unlocked) procurement group** (`in_draft_group` widens `allowed_statuses` by
`DESIGN_RELEASED`). If the Head then accepts it, `design_change_request_accept` calls
`_open_next_attempt()`, which writes `assignment.status = in_design` (or `arka_submitted` if an
approved Arka carried forward).

**This is good news for the spec, not bad.** Mirror rule 3 — *"mirrors follow their source in both
directions"* — requires a reopen to exist and be observable. It does: `_open_next_attempt()` is a
single shared function with exactly three callers (`design_qc_fail`, `design_head_qc_fail`,
`design_change_request_accept`) and it already emits
`log_activity(..., action_code='design_attempt_opened_<reason>')`. It is a usable hook point.

### Confirmed

**Claim 2 — CONFIRMED, in full.** `models.py` carries `TaskTemplate`, `TaskTemplatePhase`,
`TaskTemplateTask`. `TaskTemplate.Meta.constraints` holds `uniq_task_template_code_version`
(`code`+`version_no`) and the partial `uniq_active_task_template_per_code` (`fields=['code']`,
`condition=Q(status='active')`). Immutability is `save()`/`delete()` overrides on the two child
models calling the shared module-level `_require_draft_template()`, which raises
`TemplateVersionLocked`. The guard reads `template.DRAFT` off the version rather than naming a
class, so `TaskTemplate` and `Checklist` cannot drift. The class docstring states the enforcement
limit honestly: `QuerySet.update()`, `QuerySet.delete()` and an FK cascade all bypass it.

**Claim 4 — CONFIRMED.** `Task.task_name` is a plain `CharField` written by `bulk_create` from
`t.label`. `Task.template_task` is documented in-model as *"PROVENANCE ONLY — nothing reads it
back to decide behaviour"*, and the docstring explicitly names B-10. There is deliberately no
`label_snapshot` beside it because `task_name` **is** the snapshot.

**Claim 5 — CONFIRMED, and verified for bypasses.** `_apply_task_status_change()` exists in
`views.py`. `task_status_update` calls it once; `task_detail_status_update` calls it once. Neither
view writes `Task.status`, `completed_at` or `blocked_since` anywhere. Both keep only their own
gate and their own response, as R-18 states.

**Claim 6 — CONFIRMED, and it is worse than "no FK".** `DCLineItem` has no `boq_item` field at
all. Its only material handles are `boq_category` — a **4-value** choice (`Solar Modules`,
`Structure`, `Inverter`, `BOS`) — and `item_description`, free text. `BOQItem.category` has
**five** values (the four plus `Other`), so anything filed under `Other` is unreconcilable by
construction. `docs/delivery-state-authority.md` §4 already states this.

---

## 2 · TASK A — how a project enters execution today

### The Residential path, function by function

```
POST /projects/<id>/activate/
  views.project_activate
    ├─ _active_project(project_id)                    # R-16; soft-delete filter
    ├─ _pm_owns_project(request, project)             # 404 if not
    ├─ guard: project.status == 'Draft'
    ├─ guard: POST['assigned_design_id'] present      # ← Residential-shaped, see below
    ├─ get_object_or_404(UserProfile, role='Design', is_active=True)
    └─ transaction.atomic():
         ├─ project.assigned_design / status='Active' / activated_at=now() / save()
         ├─ utils.record_transition(project, to_status='Active', from_status='Draft')
         ├─ if project_type == 'Residential':
         │    utils.attach_residential_template(project)
         │      └─ transaction.atomic():                       # nested savepoint
         │           ├─ utils.resolve_residential_template()
         │           │    └─ utils.resolve_active_task_template('Residential')
         │           │         .prefetch_related('phases__tasks')
         │           │       (bootstraps v1 via seed_task_template_version() on a
         │           │        virgin DB; RAISES if rows exist but none is active)
         │           ├─ per template phase: ProjectPhase.objects.create(...)
         │           │                      Task.objects.bulk_create([...])
         │           ├─ utils.assign_tasks_to(<PM-role tasks>, project.assigned_pm)
         │           ├─ UserProfile lookup by RESIDENTIAL_FINANCE_ASSIGNEE_EMAIL
         │           │    (raises UserProfile.DoesNotExist if absent → rolls back)
         │           ├─ utils.assign_tasks_to(<invoice + finance-confirmation>, finance)
         │           └─ 5 assertions (non-empty, phases, total, internal, external)
         └─ 3 × PaymentMilestone.objects.create(M1/M2/M3)
  ── after commit ──
    ├─ utils.log_activity(...)
    └─ messages.success(f'{count} tasks created')     # counts rows, does not restate 52
```

### `Task` rows are created with `bulk_create()`

`Task.objects.bulk_create([...])`, one call per template phase. **`bulk_create` does not call
`Model.save()`** — and `Task` has no `save()` override anyway. Consequence for 1.3, stated
plainly: **there is no per-task model-level guard today and none can be added that this path would
honour.** A mirror flag must be a plain column written by the same `bulk_create`, and its
enforcement must live in `_apply_task_status_change()`, exactly as the spec says. This is the
codebase agreeing with the spec, not a constraint on it.

`materialise_task_dependencies()` (1.4a) makes the opposite choice — one `save()` at a time,
explicitly *"deliberately does NOT use `bulk_create()`"* — because it needs
`TaskDependency.clean()`. That function is built but **not yet wired into
`attach_residential_template()`**; 1.4b or later places the call.

### Snapshot vs FK on `Task`

| Field | Source | Kind |
|---|---|---|
| `task_name` | `TaskTemplateTask.label` | **snapshot** |
| `task_order` | `.sort_order` | **snapshot** |
| `assigned_role` | `.assigned_role` | **snapshot** |
| `duration_days` | `.duration_days` | **snapshot** |
| `task_type` | `.task_type` | **snapshot** |
| `is_payment_milestone` | `.is_payment_milestone` | **snapshot** |
| `template_task` | the row itself | **FK, provenance only, `SET_NULL`** |
| `status` | — | model default `Not Started` |
| `assigned_to`, `due_date`, `completed_at`, `blocked_since` | — | not from template |

Six snapshots and one provenance FK. `is_mirror` would be the **seventh snapshot, following an
established pattern exactly** — not a first of its kind.

### `activated_at`

`Project.activated_at = DateTimeField(null=True)`, commented *"Set when PM activates; used as
due-date chain anchor."*

**Written by:** `project_activate` only (one assignment in the whole codebase). Read-only in
`ProjectAdmin`.

**Read by:** `utils.calculate_due_dates()` and `utils.compute_gantt_schedule()` (both use
`activated_at.date()` as the chain origin and return `[]`/refuse when it is `None`);
`views.project_recalculate_dates`; `views.project_overview` (`gantt_not_activated`);
`reports._active_project_filter()` (`activated_at__isnull=False` **is the definition of "active"**
for the CEO per-user report); the same predicate again in `send_eod_digest`.

**Consequence 1.3 must know:** the moment an OPEX site gets `activated_at`, it enters the CEO
report's and the EOD digest's active-project universe. Both are portfolio-wide. **95 sites join
those two surfaces on the day 1.3 runs, whether or not anything else changes.**

### Atomicity

Yes. `project_activate`'s `transaction.atomic()` wraps the status write, the ledger row, the
template attach and the three milestones. `attach_residential_template()` opens its own nested
block (a savepoint), and the five assertions run **inside** it so a mismatch aborts rather than
shipping a half-seeded project. `resolve_residential_template()` is called inside that block so a
virgin-DB bootstrap rolls back with the activation that triggered it.

### Running activation twice

Refused, cleanly and before any write: `if project.status != 'Draft'` → `messages.warning('Project
is already active.')` → redirect. The check is placed before the atomic block precisely to avoid
partial state.

**But the refusal is `status`-based, not `activated_at`-based, and there is no uniqueness
constraint on `(project, phase_order)`.** `_phase_progress_subqueries()` says so in its docstring
and defends against it by taking the lowest-pk phase. If 1.3 introduces a second, non-`project_activate`
route into execution (a bulk command, say), **that route needs its own idempotency guard** — the
status check does not travel with it.

### What an OPEX site has and lacks today

| | State (measured on the production restore) |
|---|---|
| Has | `Program` FK — 96/96. `assigned_pm` — 96/96. `status='Draft'` — 95. `project_id` = `site_code`. A `DesignAssignment` — 87 of 96, created lazily on first survey upload. A ledger row (`record_transition(to_status='Draft')` at creation). |
| Lacks | **Any `ProjectPhase` — 0 of 96.** **Any `Task` — 0 of 96.** `activated_at` — set on 1 of 96. `assigned_design` FK — 5 of 96. Any task template of its own — the only `TaskTemplate` row in the database is `RESIDENTIAL v1 (active)`, 9 phases / 52 tasks. |

### What 1.3 must add — and no more

1. **An `OPEX` `TaskTemplate` v1** seeded by migration, `project_type='OPEX'`, via
   `seed_task_template_version()`, idempotent, reversible.
2. **A type-agnostic attach function.** `resolve_active_task_template(project_type)` is *already*
   parameterised and needs no change; `resolve_residential_template()` and
   `attach_residential_template()` are the Residential-specific wrappers. The generic attach must
   **not** carry: the Finance-assignee `raise`, the invoice/finance-confirmation name list, or the
   PM pre-assignment (see §7 on why the last one is the expensive default).
3. **An execution-start transition for a non-Residential site** that sets `status='Active'` +
   `activated_at` + `record_transition()`, and that **does not require `assigned_design_id`** and
   **does not create M1/M2/M3**. Whether that is a branch inside `project_activate` or a separate
   view is 1.3's call; the two exclusions are not optional.
4. **Nothing about closure.** No terminal status, no COD write, no HOTO. D-4 puts that in phase 5.

---

## 3 · TASK B — can the template tables carry a mirror flag?

### `TaskTemplateTask` — full field list

```python
phase                = FK(TaskTemplatePhase, related_name='tasks', on_delete=CASCADE)
code                 = CharField(max_length=100)    # stable across versions
label                = CharField(max_length=200)    # → Task.task_name
sort_order           = PositiveIntegerField()       # → Task.task_order
assigned_role        = CharField(max_length=20, choices=Task.ROLE_CHOICES, default=Task.PM)
task_type            = CharField(max_length=10, choices=Task.TYPE_CHOICES, default=Task.INTERNAL)
duration_days        = PositiveIntegerField(default=1)
is_payment_milestone = BooleanField(default=False)
# Meta: ordering=['sort_order']; UniqueConstraint(['phase','code'], 'uniq_task_template_task_code')
```

Immutability guard, on both `save()` and `delete()`:

```python
def save(self, *args, **kwargs):
    _require_draft_template(self.phase.template, f"task '{self.label}'")
    return super().save(*args, **kwargs)
```

### Adding a field: safe, and here is exactly why

**The guard does not block the migration.** Three independent reasons, any one sufficient:

1. A schema `AddField` is DDL. It never instantiates a model and never calls `save()`.
2. Migrations operate on `apps.get_model()` **historical** classes, which carry no custom methods
   at all — `seed_task_template_version()`'s comment says this in as many words: *"a historical
   model class carries no methods."*
3. `_require_draft_template()` fires on `save()`/`delete()` of a **child row**, not on an ALTER of
   the table.

**What happens to the existing active RESIDENTIAL v1 rows:** they take the column default. 52
`TaskTemplateTask` rows get `is_mirror=False` (or `derivation_source=NULL`). Nothing about them
changes semantically, no row is touched by Python, and R-7 is not violated — R-7 governs
*content authored into a version*, and a column with a uniform default carries no content.
`Task` takes the same treatment for its 1,861 existing rows.

**Do not** write a data migration that loops and `save()`s template rows — that *would* hit the
guard. Use the column default, or `QuerySet.update()` if a non-default backfill is ever needed
(which for 1.3 it is not: the OPEX template is seeded fresh, as a draft, so its mirror values are
authored normally before activation).

### Recommendation: **ship the boolean now; do not try to make one field do both jobs**

The prompt asks whether one field can be both a boolean ("nobody may type here") and a source
("this object writes here"). **It can, technically — `derivation_source IS NOT NULL` is a
perfectly good read-only predicate. I recommend against it, for a specific reason this codebase
has already paid for twice.**

**The two questions have different lifetimes, and they diverge in a direction the spec itself
predicts.** The spec's own §3 says four tasks are entered *now* and **become mirrors later**:
Net Metering Approval, CEIG Approval and Post-Installation Approvals convert at phase 5.1; the two
Inspections convert at 4.5. Under a single nullable-source field, each conversion is a template
version bump that changes a value with **two** meanings at once. Under two fields, `is_mirror`
flips and `derivation_source` fills in — and the read-only refusal, which is the load-bearing
rule, never depends on a field whose vocabulary is still growing.

There is a second, sharper reason. **Phase 5's COD and HOTO mirrors have no source object at all
today** (§5 below). A nullable-source field forces 1.3 to choose between two bad options: invent
enum members (`'cod'`, `'hoto'`, `'as_built'`) for objects that do not exist — inert values in a
choices list, which is exactly the `is_mandatory` situation the BOQ catalogue is already carrying
and documenting as *"INERT AS OF THIS COMMIT"* — or leave them `NULL`, which under
`NOT NULL ⇒ read-only` makes **COD and HOTO writable by anyone**. That is the spec's premortem #1
arriving on day one, through the schema rather than through a feature request.

**Recommendation.**

- **1.3 adds `is_mirror = BooleanField(default=False)`** to `TaskTemplateTask` and to `Task`.
  It answers the only question 1.3 needs answered, it is correct for all 11 mirrors including the
  three with no source, and it snapshots exactly like the six fields beside it.
- **Phases 3–5 add `derivation_source`** as a nullable enum **beside** it, populated per mirror as
  each source object is built. R-5 is the rule that says so: *"one status field answers one
  question."* Two questions, two fields.
- **Add a check constraint** when `derivation_source` arrives:
  `derivation_source IS NULL OR is_mirror` — so a source can never be named on a task humans may
  type into. That is the invariant worth enforcing in the database; the reverse
  (`is_mirror ⇒ source`) is deliberately **not** enforced, because COD/HOTO/As-Built are mirrors
  without sources and must stay legal.

**Also decide in 1.3, because the migration is the expensive part:** the mirror flag on `Task`
should be `db_index=True`. Every counter in §6 that needs the exclusion will filter on it, and
`Task` is 1,861 rows today and 4,616 the day 1.3 runs. Adding the index later is a second
migration for no reason.

---

## 4 · TASK C — the refusal point

### Every path that writes `Task.status`

| # | Path | Routes through `_apply_task_status_change()`? | Could it touch a mirror? |
|---|---|---|---|
| ① | `views.task_status_update` (project-overview row) | **yes** | yes — the refusal lands here |
| ② | `views.task_detail_status_update` (task-detail block) | **yes** | yes — the refusal lands here |
| ③ | `views.milestone_receive` → `.update(status=Task.DONE, completed_at=…)` | no, by decision (B16) | **no — see below** |
| ④ | `views.project_overview`, `update_milestone` branch → same `.update()` | no, by decision (B16) | **no — see below** |
| ⑤ | `admin.TaskAdmin` | n/a — **`readonly_fields = ['status']`**, closed by B9 | no |
| ⑥ | Django signals | **none exist.** `signals.py` holds only `create_user_profile`, `log_user_login`, `log_user_logout` | no |
| ⑦ | Management commands | **none write.** `send_eod_digest` only `.exclude(status=Task.DONE)`; `seed_scm_handoff_data` and `teardown_opex_test_data` are fixtures, not production paths | no |
| ⑧ | Data migrations | **none touch `Task.status`.** 0067 writes `template_task` only | no |

Nothing else in the codebase assigns `Task.status`. ① and ② are the only user-initiated writes and
both go through the helper. **R-18 holds with no hole.**

### ③ and ④ — the milestone sync cannot reach a mirror, but only by accident

Both filter on `task_name__in` a **three-name Residential-only map** declared once at module level:

```python
_FINANCE_TASK_TO_MILESTONE = {
    'Advance Payment Confirmation':      'M1',
    'Pre Dispatch Payment Confirmation': 'M2',
    '100% Payment Confirmation':         'M3',
}
_MILESTONE_TO_FINANCE_TASK = {v: k for k, v in _FINANCE_TASK_TO_MILESTONE.items()}
```

**None of the spec's 29 task names appears in that map.** The sync's queryset would match zero
rows on an OPEX site and write nothing. The refusal has no hole here.

**But the reason it has no hole is a name mismatch, not a guard** — and the near-miss is real,
because `project_activate` creates M1/M2/M3 on **every** project type (§2, finding (e)). Marking
an OPEX site's M1 "Received" today runs `.filter(phase__project=<the OPEX site>,
task_name='Advance Payment Confirmation')` and finds nothing. It is a live query against a mirror
site's task table that happens to miss.

**Recommendation for 1.3 (cheap, and it converts luck into a guarantee):** add
`is_mirror=False` to both sync querysets. Two words, no behaviour change on any existing row,
and the protection then survives anyone later naming an OPEX task after a Residential one. Do
**not** route ③/④ through the helper — B16 explains at length why that is the wrong move (they sit
inside `except Exception: pass` so a sync failure never blocks the milestone; the helper emits
`messages.*` and would convert a visible failure into a silent one).

### The trap in the refusal test — flagged because premortem #2 will otherwise pass falsely

The spec's premortem #2 demands *"a test that posts a mirror status change and asserts refusal."*
**Both entry points refuse an unassigned task before the helper is ever called:**

- `task_status_update`: `if task.assigned_to is None:` → error / `JsonResponse(400)`
- `task_detail_status_update`: `if task.assigned_to is None or task.assigned_to != profile:` → 403

So if mirrors are seeded **unassigned**, a test that POSTs a mirror status change gets a refusal
**for the wrong reason** — and passes. It would keep passing with the mirror check deleted.

The test must either (a) assign the mirror first, then POST, and assert the mirror-specific
message; or (b) call `_apply_task_status_change()` directly. `tests_task_status_path.py` already
has the right shape for this: one `TaskStatusPathContract` mixin run through both entry points as
`OverviewRowPathTests` and `TaskDetailPathTests`. The mirror refusal belongs in that mixin, so it
is asserted on both screens by construction.

---

## 5 · TASK D — the mirror sources, one by one

### Design — **wireable today.** Two clean chokepoints.

`DesignAssignment` is `OneToOneField(Project)`, so it is per-`Project` (= per site). Its `status`
has 14 values; the spec's mapping onto them is sound:

| Spec state | `DesignAssignment.status` |
|---|---|
| Not Started | `awaiting_survey`, `awaiting_allocation` |
| In Progress | `allocated` … `qc_failed`, plus `survey_returned` (Design Hold) |
| Done | `released` |

**Released transition:** one write site, `design_head_qc_pass`, inside `atomic()`.
**Reopen transition:** one shared function, `_open_next_attempt()`, three callers
(`design_qc_fail`, `design_head_qc_fail`, `design_change_request_accept`), already emitting
`action_code='design_attempt_opened_<reason>'`. Both directions exist and both are observable.

**Three caveats 1.3 must handle:**

1. **`DesignAssignment` is created lazily** by `_get_or_create_assignment()` on first survey
   upload — 87 rows against 96 sites. The derivation must read a missing assignment as Not
   Started, not crash.
2. **The design module is OPEX-only.** `design_views._opex_site()` raises `Http404('Design
   workflow applies to OPEX sites only.')` for anything else. **A CAPEX site can never acquire a
   `DesignAssignment` through the product.** If 1.3's template serves CAPEX as well as OPEX, the
   Design mirror is permanently Not Started on CAPEX — say so in the template, do not discover it
   later.
3. **Zero assignments are `released` today.** Distribution on the restore:
   `awaiting_allocation` 82, `artifacts_uploaded` 2, `in_design` 1, `in_qc` 1, `arka_submitted` 1.
   The Design mirror reads **Not Started on 82 of 87 sites** on day one. Expected, but worth
   knowing before the screen is judged.

### Punch Points — **the source exists; the concept does not.** Spec needs amending.

`Issue` is `FK(Project, related_name='issues')` — cleanly queryable per site. Statuses
`Open | In Progress | Resolved | Closed`. 29 rows exist (28 Residential, 1 CAPEX, **0 OPEX**).

Two problems, both substantive:

1. **There is no "punch point" and no "blocking" concept.** `Issue` has `severity`
   (`Low|Medium|High|Critical`), `task` (nullable FK), `delivery_challan` (nullable FK) — and no
   type, category or blocking flag. So the spec's derivation *"In Progress = one or more open"*
   would count **every** issue on the site: project issues, delivery issues, and task blockers.
   And the spec's COD rule — *"Refuses while a blocking punch point is open"* — has **no field to
   read**. `severity='Critical'` is the nearest thing and is not the same question.
2. **It creates a feedback loop.** `_apply_task_status_change()`'s Blocked branch **auto-creates
   an `Issue`** on the site (that is what `block_issue_title` / `block_issue_severity` /
   `block_issue_assigned_to` are for). So blocking *any* task on an OPEX site would flip the Punch
   Points mirror to In Progress. A commissioning punch list and "the site engineer marked the
   Survey task blocked" would be the same number.

**Verdict: the Punch Points mirror cannot be wired correctly in 1.3.** Either the spec accepts
"open issues" (and renames the task to say so), or `Issue` needs a discriminator — which is a
schema change, and R-1 says that is its own reviewed prompt, not a line inside 1.3. **Phase 2.4 is
"punch points" in the deferred doc's own phase map; this belongs there.**

### The six deliveries — **no join exists, and the six names do not map onto the data.**

Confirmed: `DCLineItem` has `boq_category` (4 values) and free-text `item_description`, and no
`boq_item` FK. **The current join between a challan line and a BOQ item is: none at all.** Not a
string match — nothing in the codebase attempts one. `docs/delivery-state-authority.md` §4
records this as B-18 and forbids touching it until challan coverage is real.

**A second problem the spec does not mention, and it is the larger one.** The OPEX BOQ catalogue
is `BOQItemMaster` scoped by `project_type='OPEX'` — 207 rows in **16 categories** (measured):

```
AC Cable 39 · ACDB 20 · BOS 20 · Cable Tray 5 · Civil 1 · Conduit 33 · Data Logger+ WMS 3
DC Cable 3 · DCDB 6 · Earthing 10 · Inverter 15 · MMS 16 · Module 1 · Pin Type Lug 4
Ring Type Lug 25 · Solar Meter + CT 6
```

Against the spec's six delivery tasks:

| Spec mirror | Catalogue category | |
|---|---|---|
| Delivery — MMS | `MMS` (16) | clean |
| Delivery — Module | `Module` (1) | clean |
| Delivery — Energy Meter (Solar Generation Meter & CT) | `Solar Meter + CT` (6) | clean |
| Delivery — DCDB, ACDB and Inverter | `DCDB` (6) + `ACDB` (20) + `Inverter` (15) | 3 categories → 1 task; fine, but must be stated |
| Delivery — RMS | **no category named RMS** | nearest is `Data Logger+ WMS` (3) — **an assumption, not a mapping** |
| Delivery — BOS Materials | `BOS` (20)? | **and what of the other 119 rows** — AC Cable 39, Conduit 33, Ring Type Lug 25, Earthing 10, Cable Tray 5, Pin Type Lug 4, DC Cable 3, Civil 1? |

**58 % of the OPEX catalogue (120 of 207 rows) belongs to no named delivery mirror.** Either
"BOS Materials" silently means "the other 120", or those materials are invisible to the delivery
mirrors entirely. **The spec does not say which, and this is a decision the Tenders team must
take, not the build session.**

**Sizing the FK addition (B-18).** `AddField(DCLineItem.boq_item, FK(BOQItem, null=True,
on_delete=SET_NULL))` is trivial DDL. **The backfill is not possible against anything.** There are
**14 `DCLineItem` rows in total**, all on Residential projects, and the only candidate match key
is free-text `item_description` against `BOQItem.description` — itself a point-in-time snapshot.
The honest answer is: **ship the FK nullable, backfill nothing, require it going forward.** With
14 historical rows, that costs nothing and invents nothing. `delivery-state-authority.md` §4.1
independently reaches the same conclusion — *"do not write a backfill that invents challans."*

**Verdict:** the six delivery mirrors read Not Started until B-18 ships **and** the
category→mirror mapping is decided. The spec is right that this is honest; it is wrong that the
only blocker is the FK.

### COD / HOTO / As-Built — **confirmed: no source object exists.**

Searched: there is no COD model, field or constant; no HOTO anything; no as-built model.
`PaymentMilestone` is the M1/M2/M3 payment structure, not a commissioning record. All three read
Not Started until phase 5 builds their sources — **which is precisely why `is_mirror` must be a
boolean rather than a non-null derivation source** (§3).

### Summary

| Mirror | Usable signal today? |
|---|---|
| Design | **Yes** — release + reopen both single-chokepoint. Caveats: lazy creation, OPEX-only, 0 released. |
| Punch Points | **No.** Source table exists; the *concept* (punch point, blocking) does not, and task-blocking pollutes it. **Spec must be amended.** |
| Six deliveries | **No** — B-18, *and* an undecided category→mirror mapping with 120 uncategorised rows. |
| COD / HOTO / As-Built | **No source object** — confirmed, as the spec states. |

---

## 6 · TASK E — every place that counts, sums or lists tasks

**This is the section to act on.** Read the two columns on the right together: a mirror inflates a
counter only if it is **(a)** seeded with a status the counter matches, and **(b)** either assigned
to somebody or counted regardless of assignment.

Two facts decide almost every row:

- **Mirrors will have `due_date = NULL`** (nothing sets it — `calculate_due_dates()` is a manual
  PM action, and 1,524 of 1,861 existing tasks already have none). Every `due_date__lt` /
  `due_date=` filter excludes NULL, so **every overdue and due-window counter is protected —
  accidentally.**
- **Mirrors assigned to nobody** (`assigned_to IS NULL`) drop out of every per-user counter. The
  Residential attach pre-assigns PM-role tasks to `project.assigned_pm`; **if 1.3's generic attach
  copies that, `is_mirror` protection is needed in ten more places.** See §8.

| # | File · function | What it counts | Mirror inflates it? |
|---|---|---|---|
| 1 | `reports.py : build_user_status_rows()` — CEO daily report | per-user `tasks_assigned`, `not_started`, `in_progress`, `completed`, `blocked`, `overdue`, `done_today`, `projects_assigned`; plus a totals row | **YES if assigned.** Filters `assigned_to__isnull=False` — that is the only protection. An assigned COD/HOTO mirror lands in `tasks_assigned` and `not_started` for the PM on all 95 sites. `overdue` protected by NULL `due_date`. **Also inflates `projects_assigned`** via the task-derived half of the union. |
| 2 | `views.py : _get_ceo_dashboard_context()` — QUERY 2, ~40 conditional counts in one `.aggregate()` | `task_total`, `task_unassigned`, `task_inprogress`, `task_completed`, `blocked_open`, `blocked_aged_7d`, `ext_closed`, and **`dept_{pm,se,scm,design,finance,bd}_{assigned,pending,overdue}`** | **YES, worst case.** `dept_*_pending` counts **by `assigned_role`, ignoring `assigned_to`** — so it inflates whether or not mirrors are assigned: **+6 SCM, +2 PM, +2 Design per site** = 570, 190, 190 across 95 sites. `task_total` +1,045. `task_unassigned` +1,045 if unassigned. Overdue/due-window terms protected by NULL `due_date`. |
| 3 | `views.py : _get_ceo_dashboard_context()` — QUERY 5 "Top People / OPEN" | top-5 assignees by open task count | **YES if assigned.** No `is_active` filter either (documented asymmetry). A PM holding 190 mirrors tops this card. |
| 4 | `views.py : _get_ceo_dashboard_context()` — QUERY 6 "Top People / COMPLETED" | Done in rolling 30 days, needs `completed_at` | **Only when a mirror completes.** A Design mirror reaching Done writes `completed_at` and counts as that person's completion — attributing another team's work. |
| 5 | `views.py : dashboard_pm()` — summary cards | `due_today`, `blocked_tasks`, **`pending_approvals`**, `external_pending` | **YES — `pending_approvals`.** It is `assigned_role=Task.PM, status='Not Started'`, **no assignment term**: +2 per site (COD, HOTO) = **+190**, on the PM's headline card. `external_pending` inflates if any mirror is `task_type='External'`. |
| 6 | `views.py : dashboard_pm()` — per-project loop | `total_tasks`, `done_tasks`, `internal_total`, `internal_done`, **`internal_percent`**, `ext_pending`, `overdue_count`, `blocked_count`, `urgency_count`, `due_today_count`, `current_phase` | **YES.** 11 of 29 rows are mirrors, so `internal_percent` measures 38 % work nobody can do. **`current_phase`** = first phase holding a not-Done task — a stuck Design mirror pins the whole site at Phase 2 forever. |
| 7 | `views.py : tasks_drill_down()` (due-today / due-soon / overdue) | grouped task lists + `total_count` | **No** — gated by `due_date__isnull=False` at the base queryset. Protected only by mirrors having no due date. |
| 8 | `views.py : dashboard_site_engineer()` | `overdue_count`, `blocked_count` annotations; `se_total`/`se_done`/`progress`; `current_phase`; next-task | **Only if a mirror is assigned to an SE.** No spec mirror carries the Site Engineer role, so **not at risk as specified.** `current_phase` here shares row 6's flaw. |
| 9 | `views.py : dashboard_design()` | project set (`phases__tasks__assigned_to`), `total_revisions` | **Visibility, not a count.** An assigned Design mirror admits that designer to the site — see row 17. |
| 10 | `views.py : dashboard_scm()` | `boq_awaiting`, `deliveries_today`, `overdue` — **all from `DeliveryChallan`, by design** | **No** for the delivery figures (the docstring says *"not Task proxies"*). **But `scm_tasks_due_today / due_soon / overdue`** at `_scm_task_base` are Task-based — protected only by `due_date__isnull=False`. |
| 11 | `views.py : dashboard_bd()` | `orc_done_project_ids` — `assigned_role=Task.BD, status=Done` | **No.** No spec mirror uses `BD / Sales`. Safe by naming, and the query has no `project_type` filter, so it is safe only for that reason. |
| 12 | `views.py : dashboard_finance()` | — | **No task queries.** |
| 13 | `views.py : project_overview()` — `phase_data_json` | per-phase `internal_done` / `internal_total` / `pct` / `ext_pending` | **YES.** Same defect as row 6, per phase. Phase 4 (Procurement & Delivery) is 6 mirrors + 2 entered — its percentage would be 25 % real. |
| 14 | `views.py : _phase_progress_subqueries()` / CEO Delivery & BOQ pills | phase-6 / phase-3 task completion | **No** — both carry `project__project_type='Residential'` inside the subquery. **But their in-code rationale dies:** the comment block at `views.py` above `RESIDENTIAL_DELIVERY_PHASE_ORDER` asserts *"There is no OPEX/CAPEX phase template in this codebase at all"* and *"no non-Residential project on production has a single phase row."* **1.3 falsifies both sentences.** The guard holds; the comment must be corrected in the same commit or it will mislead the next reader. |
| 15 | `utils.py : compute_gantt_schedule()` / `build_gantt_view()` | every task on the project, chained | **No today** — `project_overview` sets `gantt_available = (project.project_type == 'Residential')`. **If the site dashboard in phase 2.5 reuses the Gantt, mirrors enter the chain and consume calendar days** (`cursor = raw_end`). A mirror must not advance the cursor. |
| 16 | `send_eod_digest.py` — three counters: per-user "open tasks assigned", coordinator "open tasks across coordinated projects", `_company_totals` "assigned" | open (`!= Done`) task counts | **YES.** The coordinator counter has **no assignment term at all** — it counts every open task on every coordinated active project, so all 11 mirrors per site count. The per-user and company counters need assignment. **Worse than a wrong number:** the digest has open-work gating, so mirrors can flip a user from "no digest" to "digest", emailing people about work they cannot do. |
| 17 | `permissions.py : user_can_view_project()` (SE + Design branches), `_user_holds_task_on_project()` (BOQ read gate) | `project.phases.filter(tasks__assigned_to=profile).exists()` | **Not a counter — an access grant.** Assigning a mirror to a designer grants them **project visibility and BOQ read** on that site. Assigning six delivery mirrors to an SCM user does the same, ×95. |
| 18 | `task_dependencies.py : incomplete_predecessors()` | `.exclude(status=Task.DONE)` on predecessor edges | **Would be, once 1.4b wires dependency materialisation.** A never-completing mirror would block every successor forever. The spec defers precedences — **keep them deferred until the mirror interaction is decided.** |
| 19 | `projects/templates/.../project_overview.html:180,920` and `project_detail.html:146` | `{{ phase.tasks.count }}` rendered as "N tasks" | **YES** — cosmetic, but it is the number a PM reads next to a phase. |
| 20 | `models.py : get_program_rollup()` / `program_rollup_annotations()` | sites by `Project.status` | **No** — counts projects, not tasks. Noted because it is where 95 sites visibly move Draft → Active. |

### What this enumeration says

**Sixteen of twenty rows are Task-based, and only three are protected by a deliberate guard**
(rows 11, 14 and 20 — and 14's guard is a `project_type` filter whose *stated reason* 1.3
invalidates). Everything else is protected by **`due_date IS NULL`** or by **`assigned_to IS
NULL`** — two accidents, either of which a later prompt can undo without knowing it did.

The spec's rule 5 — *"mirrors are excluded from overdue counts and from per-user workload"* — is
**not achievable by adding one filter.** It is at minimum:

- `reports.build_user_status_rows()` — 1 queryset
- `_get_ceo_dashboard_context()` — 3 querysets (the big aggregate, Top-Open, Top-Completed)
- `dashboard_pm()` — 2 (summary cards, per-project loop)
- `project_overview()` — 1 (`phase_data_json`)
- `send_eod_digest` — 3
- `dashboard_scm()` — 1 (`_scm_task_base`)
- `tasks_drill_down()` — 1 (base queryset, for correctness even though NULL protects it)

**Twelve querysets, in six files.** That is the true cost of rule 5, and it is not 1.3-sized.

**Recommendation.** Do exactly what §D-3 warns about *not* doing — put the definition in one
place, once, before any consumer uses it. Add to `utils.py` alongside
`reports._active_project_filter()` (the existing precedent for a shared predicate):

```python
def exclude_mirrors(qs, prefix=''):
    """Drop derived-mirror tasks from a metric queryset. A mirror is nobody's
    work: counting it against a person blames them for another team's queue."""
    return qs.filter(**{f'{prefix}is_mirror': False})
```

One definition, one docstring, one place a future reader finds every consumer by grepping the
name. **The codebase already carries four representations of delivery state because a concept
shipped before its consumers were enumerated. This is that same moment, and it is still early.**

---

## 7 · TASK F — 29 tasks × 95 sites

### Query cost of one attach

Counted from the code path (Residential shape, adjusted to 8 phases):

| Step | Queries |
|---|---|
| `resolve_active_task_template()` with `prefetch_related('phases__tasks')` | 3 |
| per phase: `ProjectPhase.objects.create()` + `Task.objects.bulk_create()` | 2 × 8 = **16** |
| assignment `.update()` calls (`assign_tasks_to` is one UPDATE each) | 1–2 |
| integrity assertions (`phases`, `total`, `internal`, `external`) | 4 |
| **attach subtotal** | **≈ 24** |
| `project_activate` around it: `_active_project`, `project.save()`, `record_transition`, milestones, `log_activity` | ≈ 8 |
| **per site** | **≈ 32** |

`prefetch_related` matters: without it the attach is 1 + 8 = 9 extra queries, which is why
`resolve_active_task_template()` carries it and says so.

### 95 sites

**≈ 3,040 queries, 2,755 `Task` rows, 760 `ProjectPhase` rows** — plus 285 `PaymentMilestone` rows
if the current `project_activate` is reused (§2 finding (e)).

**Nothing does activation in bulk today.** `project_activate` is a per-project POST behind
`@role_required(['PM','Project Coordinator'])` and `_pm_owns_project()`. There is a bulk *creation*
path (`opex_site_bulk_upload`, xlsx) but no bulk activation.

**Recommendation:** if 95 sites are to move at once, that is a management command or a data
migration, not 95 POSTs — and per §2, **it needs its own idempotency guard**, because the
double-run protection is `project.status != 'Draft'` inside the view and does not travel.

### Does any screen degrade?

**Per-site: no.** 29 tasks is smaller than the 52 already rendered by `project_overview` on 26
Residential projects.

**Portfolio: the CEO dashboard is where it lands.** Row 2 of §6 is a single `.aggregate()` with
~40 conditional counts over `Task`. Today it scans 1,861 rows; after 1.3 it scans **4,616 — a
2.5× increase**, on the one screen that is already 12 flat queries.

`admin_project_list` prefetches `phases → tasks` for **every** project with no pagination. Today
that is 1,861 tasks; after 1.3, 4,616. It renders nothing per task (the template does not iterate
them), so **the prefetch is pure waste** and will roughly triple this page's memory. Not a
blocker, worth a note.

`dashboard_pm()` runs ~10 queries **per project** in its loop and carries a `TODO` saying so. A PM
who owns tender sites goes from a handful of projects to 95. **That is the screen that breaks
first** — not from row count but from the N+1. It is out of 1.3's scope; it should be on the
record before someone assigns all 95 sites to one PM.

### Production counts

Read from the local restore (Postgres):

| | Count |
|---|---|
| `Task` | **1,861** (all Residential) |
| `ProjectPhase` | **333** (all Residential) |
| `Task` with `due_date` set | 337 of 1,861 (**18 %**) |
| `Task` with `assigned_to` set | 1,336 of 1,861 |
| `Project` OPEX Draft (live) | **95** |
| `Project` OPEX Active (live) | 1 — `activated_at` set, **0 phases** |
| `Project` CAPEX Active | 1 |
| `Project` Residential Active (live) | 26 |
| `DesignAssignment` | 87 (`awaiting_allocation` 82) |
| `Issue` | 29 (Residential 28, CAPEX 1, **OPEX 0**) |
| `DCLineItem` | **14** |
| `Program` | 3 |
| `TaskTemplate` | **1** — `RESIDENTIAL v1 (active)`, 9 phases / 52 tasks |

To re-run against Railway:

```sql
SELECT count(*) FROM projects_task;
SELECT count(*) FROM projects_projectphase;
SELECT project_type, status, is_deleted, count(*)
  FROM projects_project GROUP BY 1,2,3 ORDER BY 1,2;
SELECT count(*) FROM projects_dclineitem;
SELECT status, count(*) FROM projects_designassignment GROUP BY 1 ORDER BY 2 DESC;
SELECT code, version_no, status, project_type FROM projects_tasktemplate;
```

---

## 8 · TASK G — roles, tests, and Residential assumptions

### The four roles all exist

`Task.ROLE_CHOICES` has **six** values:

```python
PM = 'PM'; SITE_ENGINEER = 'Site Engineer'; FINANCE = 'Finance'
SCM = 'SCM'; BD = 'BD / Sales'; DESIGN = 'Design'
```

Site Engineer, PM, SCM and Design are all present. `TaskTemplateTask.assigned_role` uses the same
constants, so the template can express all four.

**Two gaps the spec walks into:**

1. **Punch Points is given role "—" in the spec.** `assigned_role` is
   `CharField(max_length=20, choices=ROLE_CHOICES, default=PM)` — **not nullable, no blank
   choice.** "—" is not representable. It will silently become `PM` via the default, adding a
   third PM mirror to `dashboard_pm`'s `pending_approvals` (§6 row 5). **1.3 must pick a role or
   the spec must.**
2. **Project Coordinator is not a `Task.assigned_role` value** (execution-model §4 states this).
   The spec assigns "Completion Certificates (Paperwork)" to *"PM / Coordinator"* and Phase 5 to
   *"Site Engineer (PM / Coordinator as applicable)"* — **a Coordinator cannot hold a task today.**
   R-15 says the fix is a capability flag, not a new role value. Either way it is not 1.3's job;
   the template must store one real role per task.

### What "assigned to SCM" means for a mirror nobody may type into

`attach_residential_template()` pre-assigns **only** `assigned_role=PM` tasks (to
`project.assigned_pm`) and the six named Finance tasks (to a hardcoded email). Everything else —
including all 11 SCM-role tasks, which is D-2's observation — is created with `assigned_to =
NULL`. On the OPEX template, if the generic attach copies that rule:

- the **six SCM delivery mirrors** and the **two Design mirrors** are created **unassigned**;
- the **two PM mirrors (COD, HOTO)** are **assigned to the site's PM** — and inherit every
  per-user counter in §6.

**So "assigned to SCM" means: the role column reads `SCM`, `assigned_to` is NULL, and the six rows
appear in exactly one place — `dept_scm_pending` on the CEO dashboard (§6 row 2), which counts by
role and ignores assignment.** Six phantom pending SCM items per site, 570 across the portfolio,
attributed to a department that cannot act on them. It is a label, and its only live consumer
misreads it.

**Recommendation for 1.3:** seed **every mirror unassigned**, including the PM ones — do not let
the generic attach pre-assign PM-role tasks on the OPEX template. That single choice removes rows
1, 3, 4, 5(partly), 8, 9, 16 and 17 of §6 from the risk list in one move, and it is *correct*
rather than merely convenient: an unassigned mirror is an accurate statement that the row is
nobody's task. The `is_mirror` exclusion is still needed for rows 2, 6, 13, 16 and 19, which count
regardless of assignment.

### Tests that touch template attachment or activation

| File | Classes |
|---|---|
| `tests_task_template.py` | `TaskTemplateBase`, `ActivationUnchangedTests`, `TemplateDurationTests`, `TemplateImmutabilityTests`, `TemplateVersioningTests`, `InFlightProjectIsolationTests`, `TemplateProvenanceTests`, `DurationScreensAreReadOnlyTests`, `SeedHelperTests` |
| `tests_residential_baseline.py` | `ResidentialBaselineBase`, `ActivationInvariantTests` (+ the 9 workflow classes that build on the fixture) |
| `tests_soft_delete.py` | `SoftDeleteBase`, `LiveProjectStillWorksTests` |
| `tests_assignment_throttle.py` | `Base` (calls `attach_residential_template()` directly at line 256) |
| `tests_task_dependencies.py` | `DependencyBase` (a local `TESTDEP` template), `MaterialisationTests`, `RealResidentialActivationTests` |
| `tests_access_isolation.py` | fixture depends on the Finance assignee account existing |
| `tests_task_status_path.py` | `TaskStatusPathFixture`, `TaskStatusPathContract`, `OverviewRowPathTests`, `TaskDetailPathTests`, `DecidedDifferenceTests` — **where the mirror refusal test belongs** |

### Does any test assume "exactly Residential" or "exactly 52"?

**Every 52/9 assertion is scoped to a specific project**
(`Task.objects.filter(phase__project=self.project).count() == 52`), so an OPEX template adds rows
those queries never see. Checked all sixteen occurrences.

**Three that need a second look, and all three are safe:**

1. `tests_task_template.DurationScreensAreReadOnlyTests` asserts `context['total'] == 52` and
   `len(context['grouped']) == 9`. Backed by `_residential_template_duration_rows()`, which calls
   `resolve_active_task_template('Residential')` — **hardcoded**. Safe.
2. `SeedHelperTests` asserts `TaskTemplateTask.objects.filter(phase__template=template)` — scoped
   to one template. Safe.
3. `resolve_residential_template()`'s **third outcome raises** when rows exist under
   `RESIDENTIAL` but none is active. It filters on `code=RESIDENTIAL_TEMPLATE_CODE`, so an OPEX
   template with a different code cannot trigger it. Safe — **provided 1.3 uses a distinct
   `code`.** `resolve_active_task_template()` filters on `project_type`, and the partial unique is
   on `code`, so `code='OPEX'` + `project_type='OPEX'` is the shape that keeps both true.

**One non-test assumption that 1.3 does break:** `views.py`'s comment above
`RESIDENTIAL_DELIVERY_PHASE_ORDER` — *"There is no OPEX/CAPEX phase template in this codebase at
all"* and *"no non-Residential project on production has a single phase row."* The **code** is
guarded by `project_type='Residential'`; the **comment** becomes false the day 1.3 ships. Correct
it in the same commit.

**Not broken, worth knowing:** `_checklist_task_name_choices()` offers only
`get_residential_template_task_names()`, so no OPEX task can be given a checklist. And
`ChecklistTaskLink` is unique on `(task_name, project_type)` — so "Design", which exists in both
templates, gets separate namespaces. No collision.

---

## 9 · What the code contradicts in the spec

Ordered by cost of getting it wrong.

1. **§2 rule 5's exclusion is 12 querysets in 6 files, not a filter.** The spec presents it as a
   property. It is a cross-cutting change. §6 has the list. **This is the one that decides whether
   1.3 is one prompt or two.**
2. **Punch Points has no source concept.** `Issue` exists per site; "punch point" and "blocking"
   do not, and `_apply_task_status_change()`'s Blocked branch auto-creates `Issue` rows, so task
   blockers and commissioning punch lists would be one number. The COD rule *"refuses while a
   blocking punch point is open"* has no field to read. **Amend the spec.**
3. **The six delivery mirrors have no category mapping, and B-18 is not the only blocker.** 120 of
   207 OPEX catalogue rows fall under no named mirror, and no category is named RMS. **A Tenders
   decision, not a build decision.**
4. **§3's roles cannot all be stored.** Punch Points' "—" is not a legal `assigned_role`; "PM /
   Coordinator" is not one value; Project Coordinator is not a `Task.ROLE_CHOICES` member at all.
5. **§3 assigns no `task_type` and no `duration_days` to any of the 29 tasks.**
   `task_type` (`Internal`/`External`) drives eight of the counters in §6 and the Gantt's
   parallel-vs-blocking rendering; `duration_days` drives the whole due-date chain.
   `seed_task_template_version()` **requires** a `duration_resolver`, and
   `RESIDENTIAL_DURATION_DEFAULTS` is Residential-only, so absent a decision all 29 tasks get
   `duration_days=1` and `task_type='Internal'` by model default. **A 29-day OPEX project is not
   what anyone means.** Decide both before the migration, because changing them afterwards costs a
   version bump.
6. **§2 rule 8 — "the `StatusTransition` actor is the actor of the source event" — is
   structurally available but is not how any current sync works.** `record_transition()` takes an
   `actor`, and both milestone syncs already pass `request.user.profile` with
   `reason_code=REASON_MILESTONE_SYNC`, so the pattern exists. But note **R-9: `remark` is
   mandatory on execution transitions**, and the spec's *"the transition reason names the
   derivation"* must therefore be a real string on every mirror write, not an empty default.
7. **§6's "1.3 seeds this the way 0.4 seeded Residential" understates what 0.4 did.** 0.4 was
   idempotent, used the shared seed helper, backfilled provenance and shipped a reverse. 1.3 needs
   three of those four (no backfill — there is nothing to backfill).
8. **§1's premise "OPEX/CAPEX activation only stamps `activated_at`" is wrong** — it also demands
   a designer (blocking 91 of 95 sites) and mints M1/M2/M3 (285 rows). §2 has the detail.
9. **The Design mirror cannot work on CAPEX at all** — `design_views._opex_site()` 404s
   non-OPEX. If this template serves CAPEX, say what the Design row means there.
10. **`DESIGN_RELEASED` is not terminal** — one reopen route exists
    (`design_change_request_accept` → `_open_next_attempt()`). This *helps* the spec (rule 3 needs
    it) but the spec asserts terminality it does not have.
11. **Premortem #2's test will pass for the wrong reason** if mirrors are seeded unassigned, because
    both views refuse unassigned tasks before the helper runs. §4 has the fix.

---

## 10 · `EXECUTION_MODULE_DEFERRED.md` §B — Phase 1

| | Status | Relevance to 1.3 |
|---|---|---|
| B1 | open | `in_draft_group` computed twice, two spellings — in the change-request path the Design mirror reads |
| B2 | open | change-request gate fix needs `design_views.py`, behind a standing scope boundary |
| B3 | open | checklist fixture written out three times |
| B4 | open | `is_active` setter shim writes `status` directly, bypassing `activate()` — **template-adjacent** |
| B5 | open | `save()` guards on `group_type` bypassed by `update()`/`bulk_create()` — **same class of limit R-17 documents for `_require_draft_template()`; the mirror flag inherits it** |
| B6 | open | four documents still name the pre-1.1a constraint |
| B7 | open | `boq_detail`'s `locked_group` banner is the one unnarrowed membership read |
| B8 | **CLOSED** | the consolidation the mirror refusal depends on |
| B9 | **CLOSED** | `TaskAdmin.status` read-only — §4 row ⑤ |
| B10 | **CLOSED** | `ProjectAdmin` equivalent |
| B11 | **CLOSED** | `ProjectAdmin` `FieldError`; every admin page now has a test |
| B12 | open | `task_detail_status_update` has no project-scope gate. **Pinned by `DecidedDifferenceTests.test_the_detail_path_has_no_project_scope_gate_and_lets_the_same_user_through`, which must fail when it is closed.** Does not affect the mirror refusal — that sits in the helper, which the detail path calls. |
| B13 | open | the two screens answer an unassigned task with different status codes — **directly relevant: §4's test trap** |
| B14 | open | HTMX permission refusal shaped differently on the two screens — the mirror refusal must not repeat this |
| B15 | open | `?next=` honoured on one screen, ignored on the other |
| B16 | open **question** | should ⑤/⑥ route through the helper? **Recorded answer: no** — they sit inside `except Exception: pass`, the helper emits `messages.*`, and routing them would apply all five R-18 features to unattended syncs. If revisited, the shape is a second narrower helper. §4 recommends adding `is_mirror=False` to their querysets **instead of** routing them. |

Sections C–F (phases 2–5) are placeholders. §G holds four open cross-cutting items, of which **G2
(`APP_BASE_URL` undefined) and G3 (`settings.py` overwriting digest addresses)** touch the EOD
digest that §6 row 16 identifies as mirror-sensitive.

---

## 11 · Stop conditions

| Condition | Met? |
|---|---|
| `docs/OPEX_task_template_spec.md` not in the repo | **Present on disk, untracked.** Audit proceeded; see §0. Commit it before 1.3. |
| A code file would need to change | **No.** Nothing was modified. Every recommendation is scoped to 1.3. |
| Suite red beyond the one known failure | **No.** 795 tests, 1 failure, 0 errors — the known SQLite constraint-name assertion. |
| A mirror has no observable source | **YES — three.** **Punch Points** (source table exists, concept does not, and task-blocking pollutes it) and **the six deliveries** (B-18 *and* an undecided category mapping). COD/HOTO/As-Built are correctly declared source-less by the spec itself. **The spec must be amended before 1.3, not worked around.** |

## 12 · Verification of MODE compliance

- No `.py`, `.html`, `.css` or `.js` file created or modified.
- No migration created. `projects/migrations/` still ends at `0073_task_dependencies.py`.
- Exactly one new file: `OPEX_TEMPLATE_AUDIT.md` at the repo root.
- Only read-only commands run: `manage.py check`, `manage.py test`, and read-only ORM queries in
  `manage.py shell` (SELECT/aggregate only — no writes, no transaction left open).
- `docs/OPEX_task_template_spec.md` remains untracked and unmodified; this session did not commit
  it.
