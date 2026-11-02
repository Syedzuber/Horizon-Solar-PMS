# DEL-A — Admin deletion screen: discovery audit

Read-only audit, 2026-11-02. No code, migration or data was changed. The only files
written are this one and an append to `SECONDARY_FINDINGS.md`.

**Scope after the pre-flight rulings:**
- #3 GRN and #5 task assignment were dropped. A GRN is a set of fields on `DCLineItem`, not a
  row. A task assignment is the `Task.assigned_to` field, which `TaskAdmin`/`assign_task_to`
  already edit.
- What remains: **#1 contractor attendance**, **#2 delivery challan** (with every child) and
  **#4 Task**.
- The screen was then re-scoped to a **test-data purge** (rows on `Project.is_test=True`
  projects only), and after the findings below it was **withdrawn** (ruling 2 Nov, §10). Real
  data is corrected through the existing writers.

Evidence comes from three places:
- Model metadata, read with no database: scratchpad scripts `t1_fkmap.py` and
  `t2_static_walk.py`.
- Source code: file and line references given inline.
- One read-only Postgres run on a fresh local restore: `dela_readonly.py`. It refuses any
  database other than the named local one, runs inside `transaction.atomic()` with
  `SET TRANSACTION READ ONLY`, and forces a rollback at the end.

---

## 0. Governance finding — read this first

The planned page conflicts with two standing rulings in the repository. Both rulings allow
the append-only guards to be bypassed only once, under a bounded manifest, and never in
production.

`docs/execution-model.md:146`:
> **R-4 · The transition log is append-only.** Never `UPDATE` and never `DELETE` a `StatusTransition` row.

`docs/execution-model.md:852` (the 14 Oct production wipe):
> … the single sanctioned exception to R-4 … **This is not a precedent.** `teardown_opex_test_data` remains the only standing bypass and is local-only; another production wipe needs its own ruling and its own one-off.

`docs/demo-data.md:196-199`:
> It is only safe because the **manifest bounds it** to rows a seed created, on a database the interlock has already proved is local.
> **Do not generalise it.** There is no other caller and there must not be one.

Sections 2 and 4 show what a hard delete of a challan, or of any task whose status ever
moved, does to the ledger. Either it leaves `StatusTransition` rows orphaned (they cannot be
cleaned up later), or it deletes them with `QuerySet.delete()`, which is the R-4 bypass. A
standing superuser page on production that does the second needs an explicit new ruling that
amends `execution-model.md` §852 and `demo-data.md` §"Do not generalise". Without that ruling,
the only compliant purge is one that **leaves the ledger orphaned and says so**, and the
existing TaskAdmin docstring calls that state "unrecoverable in both directions".

---

## 1. Pre-flight (answered earlier, summarised)

| Target | Model | Admin today |
|---|---|---|
| Attendance | `projects.ContractorAttendance` (`models.py:8173`) + `ContractorAttendanceRevision` (`:8329`) | Registered, view-only (`_ApprovalRecordAdmin`, `admin.py:1004`, `:1012`) |
| Delivery challan | `projects.DeliveryChallan` (`:1581`); children `DCLineItem` (`:1651`), `DeliveryChallanAttachment` (`:1750`), `DCLineDamagePhoto` (`:1829`), `DeliveryChallanRevision` (`:1918`) | None registered. Attachments and damage photos are "no admin registration" by design (`:1760`, `:1841`) |
| Task | `projects.Task` (`:361`) | `TaskAdmin` (`admin.py:279`): `has_delete_permission` returns False, `delete_selected` popped |

There is no custom AdminSite and no `get_urls` override. `admin.site` is mounted at
`solarpms/urls.py:6`, ahead of `include('projects.urls')`, so `/admin/cleanup/` has to be added
through `admin.site` (a `get_urls` override or a subclassed site). `AdminAccessMiddleware` gates
`/admin/` on `profile.role == 'Admin'`, not on `is_superuser`, so a superuser-only page must
check `is_superuser` itself (see `SECONDARY_FINDINGS.md`).

---

## 2. T1 — inbound FK map

Generated from `_meta.related_objects` (no database access). Raw definitions follow each table.

### 2.1 ContractorAttendance

| Model.field | Type | on_delete | null |
|---|---|---|---|
| ContractorAttendanceRevision.attendance | FK | **PROTECT** | False |

```python
# projects/models.py:8362-8363
    attendance = models.ForeignKey(ContractorAttendance, on_delete=models.PROTECT,
                                   related_name='revisions')
```
`ContractorAttendanceRevision` itself has no inbound relations.

### 2.2 DeliveryChallan and its children

| Model.field | Type | on_delete | null |
|---|---|---|---|
| DCLineItem.challan | FK | CASCADE | False |
| DeliveryChallanAttachment.challan | FK | **PROTECT** | False |
| DeliveryChallanRevision.challan | FK | **PROTECT** | False |
| PhaseRuleOverride.delivery_challan | FK | **PROTECT** | True |
| Issue.delivery_challan | FK | SET_NULL | True |
| *DCLineItem ←* DCLineDamagePhoto.line | FK | **PROTECT** | False |
| *(DCLineDamagePhoto →)* .issue → Issue | FK | **PROTECT** | True |

```python
# projects/models.py:1676-1678
    challan          = models.ForeignKey(
        DeliveryChallan, on_delete=models.CASCADE, related_name='line_items'
    )
# projects/models.py:1766-1768
    challan = models.ForeignKey(
        DeliveryChallan, on_delete=models.PROTECT, related_name='attachments',
    )
# projects/models.py:1850-1855
    line = models.ForeignKey(
        DCLineItem, on_delete=models.PROTECT, related_name='damage_photos',
    )
    issue = models.ForeignKey(  # the damage Issue it was recorded under; set once, at create
        Issue, on_delete=models.PROTECT, null=True, blank=True, related_name='damage_photos',
    )
# projects/models.py:1937-1939
    challan = models.ForeignKey(
        DeliveryChallan, on_delete=models.PROTECT, related_name='revisions',
    )
# projects/models.py:7832-7835
    delivery_challan = models.ForeignKey(
        DeliveryChallan, on_delete=models.PROTECT, null=True, blank=True,
        related_name='phase_rule_overrides',
    )
# projects/models.py:1495-1498
    delivery_challan = models.ForeignKey(  # Null unless the issue was raised directly against a specific delivery
        'DeliveryChallan', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='issues',
    )
```

**What the brief asked about `DeliveryChallanAttachment` toward DC:**
- It is **PROTECT**. A challan carrying any document, live or withdrawn, cannot be deleted by
  the collector. The model says so itself (`models.py:1763-1764`): "PROTECT on the challan: a
  challan carrying a document cannot be deleted, which also makes the teardown commands and a
  project hard-delete refuse it".
- `delete()` on the instance raises (`:1824-1826`):
  `'DeliveryChallanAttachment is append-only — a withdrawn document stays.'`
- So a DC delete erases "withdrawn, never deleted" attachments **only if the purge first
  deletes them with `QuerySet.delete()`**, which bypasses the guard. Their storage objects
  (`bucket` + `path`, `:1774-1775`) would then be orphaned, unless the purge removes them
  explicitly, because no file field or signal is involved (T4).
- `DCLineDamagePhoto` and `DeliveryChallanRevision` have the same PROTECT plus raising
  `delete()`.

### 2.3 Task

| Model.field | Type | on_delete | null |
|---|---|---|---|
| DueDateChangeLog.task | FK | CASCADE | False |
| TaskAttachment.task | FK | CASCADE | False |
| Comment.task | FK | CASCADE | True |
| TaskDependency.predecessor | FK | CASCADE | False |
| TaskDependency.successor | FK | CASCADE | False |
| ChecklistItemCompletion.task | FK | CASCADE | False |
| PunchPoint.task | FK | CASCADE | False |
| Issue.task | FK | SET_NULL | True |
| QaqcVisitAnswer.task | FK | **PROTECT** | False |
| ContractorBillTask.task (through table of `ContractorBillDetail.tasks` M2M) | FK | **PROTECT** | False |
| PhaseRuleOverride.task | FK | **PROTECT** | True |

```python
# projects/models.py:688
    task       = models.ForeignKey('Task', on_delete=models.CASCADE, related_name='due_date_changes')
# projects/models.py:1441
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name='attachments')
# projects/models.py:1494
    task = models.ForeignKey(Task, on_delete=models.SET_NULL, null=True, blank=True, related_name='issues')  # Null for project-level issues not tied to a task
# projects/models.py:1553-1555
    task = models.ForeignKey(
        Task, on_delete=models.CASCADE, null=True, blank=True, related_name='comments'
    )
# projects/models.py:3840-3845
    predecessor = models.ForeignKey('Task', on_delete=models.CASCADE, related_name='dependents', )
    successor = models.ForeignKey('Task', on_delete=models.CASCADE, related_name='dependencies', )
# projects/models.py:4366
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name='checklist_completions')
# projects/models.py:6474-6476
    task = models.ForeignKey(
        Task, on_delete=models.CASCADE, related_name='punch_points',
    )
# projects/models.py:7067
    task = models.ForeignKey(Task, on_delete=models.PROTECT, related_name='qaqc_answers')
# projects/models.py:7759
    tasks = models.ManyToManyField('Task', through='ContractorBillTask', blank=True, related_name='contractor_bills')
# projects/models.py:7792
    task = models.ForeignKey('Task', on_delete=models.PROTECT, related_name='bill_links')
# projects/models.py:7828-7831
    task = models.ForeignKey(
        'Task', on_delete=models.PROTECT, null=True, blank=True,
        related_name='phase_rule_overrides',
    )
```

### 2.4 Soft references (not FKs, so the collector does not see them)

```python
# projects/models.py:2373-2377  StatusTransition
    subject_type = models.CharField(max_length=30, choices=SUBJECT_TYPE_CHOICES, db_index=True,)
    # Not a FK: the subject is polymorphic across six unrelated models.
    subject_id   = models.PositiveIntegerField()
# projects/models.py:1536-1537  ActivityLog
    entity_type = models.CharField(max_length=50, blank=True, default='')   # e.g. 'Task', 'Issue', 'BOQ', 'File'
    entity_id   = models.PositiveIntegerField(null=True, blank=True)         # PK of the affected object
```
- `SUBJECT_TASK = 'task'` and `SUBJECT_DELIVERY_CHALLAN = 'delivery_challan'` are both ledger
  subjects.
- Attendance writes **no** ledger or ActivityLog rows: running
  `grep -nE "record_transition|log_activity|ActivityLog"` over `attendance.py` and
  `attendance_views.py` returns nothing.
- A contractor-bill approval round snapshot stores task ids in JSON
  (`approvals.py:558-562`, `'tasks': [{'id': link.task.pk, 'task_name': …}]`). The display
  reads the stored name (`approval_views.py:632`), so a dangling id there is cosmetic.

---

## 3. T2 — what the collector would delete

### 3.1 Static walk (model metadata, CASCADE followed, PROTECT marked)

```
=== ContractorAttendance
- ContractorAttendanceRevision.attendance [PROTECT] delete()-raises

=== DeliveryChallan
- Issue.delivery_challan [SET_NULL]
- DCLineItem.challan [CASCADE]
   - DCLineDamagePhoto.line [PROTECT] delete()-raises storage:bucket/path
- DeliveryChallanAttachment.challan [PROTECT] delete()-raises storage:bucket/path
- DeliveryChallanRevision.challan [PROTECT] delete()-raises
- PhaseRuleOverride.delivery_challan [PROTECT] delete()-raises

=== Task
- DueDateChangeLog.task [CASCADE]
- TaskAttachment.task [CASCADE] storage:supabase_path
- Issue.task [SET_NULL]
- Comment.task [CASCADE]
   - Comment.parent [CASCADE]
- TaskDependency.predecessor [CASCADE]
- TaskDependency.successor [CASCADE]
- ChecklistItemCompletion.task [CASCADE] storage:photo_supabase_path
- PunchPoint.task [CASCADE]
   - PunchPointEvent.punch_point [PROTECT] delete()-raises storage:photo_path
   - QaqcVisitAnswer.punch_point [PROTECT] delete()-raises storage:photo_path
- QaqcVisitAnswer.task [PROTECT] delete()-raises storage:photo_path
- ContractorBillTask.task [PROTECT]
- PhaseRuleOverride.task [PROTECT] delete()-raises
```

**Attendance is always refused by the collector.** Every entry receives a CREATED revision in
the same transaction it is created in:
```python
# projects/attendance.py:349-350  (create_attendance)
        _save_or_refuse_duplicate(entry)
        _revision(entry, ContractorAttendanceRevision.CREATED, None, profile, now=now)
# projects/attendance.py:582  (create_late_attendance)
        _revision(entry, ContractorAttendanceRevision.CREATED, None, profile, note=note,
```
So no attendance row can be hard-deleted without first deleting its revisions with
`QuerySet.delete()`, and that is the append-only bypass.

### 3.2 Live collector run

**Not run, by ruling (2 Nov).** With hard delete out of v1 (§10), the result no longer
decides anything. The static walk above stands as the record.

---

## 4. T3 — stored derived state that a delete leaves stale

| # | Stored state | Source | Written at | Recomputed on read? | Stale after hard delete? |
|---|---|---|---|---|---|
| S1 | **Delivery mirror `Task.status`** (4 buckets: DELIVERY_SOLAR_PANELS / INVERTERS / BOS_KIT / MMS), OPEX **and CAPEX** | DC lines on the site | `design_views.sync_delivery_mirrors()` (`design_views.py:862`, writes via `apply_mirror_status`, `:994`), called **only** from `recalculate_dc_status()` (`models.py:2137`) | No | **Yes.** Deleting a DC calls neither function, so a bucket stays Done or In Progress for material that no longer exists. Fixable: `sync_delivery_mirrors(project)` recomputes from the live lines, including "no lines → Not Started" (`design_views.py:984-985`) |
| S2 | `StatusTransition` rows, subject `delivery_challan` | every DC (a `created` row at birth, `views.py:13991-13994`) and every status change | `record_transition` | n/a (ledger) | **Orphaned**, and cannot be removed without the R-4 bypass (§0) |
| S3 | `StatusTransition` rows, subject `task` | any task whose status ever moved | `record_transition` | n/a | **Orphaned**, same as S2 |
| S4 | `ActivityLog` rows with `entity_type` `'DeliveryChallan'` / `'Task'` / `'Issue'` | DC create, GRN, task actions | `log_activity` | n/a | Orphaned (soft reference). Unlike the ledger, ActivityLog has **no** `delete()` guard, so it can be cleaned up |
| S5 | `Issue` rows (delivery and damage issues) | `create_delivery_issue` (`views.py:12988`), `grn_damage.record_damage` (`grn_damage.py:329`) | — | — | `Issue.delivery_challan` is SET_NULL: the Issue stays Open and counted, unhooked from its DC. A damage Issue stays held by PROTECT from its photos |
| S6 | Blocking `Issue` of a Blocked task | task block flow | — | — | `Issue.task` is SET_NULL: the Issue stays Open with no task |
| S7 | `PhaseRuleOverride` rows (R1 on DC, R2–R6 on Task) | `phase_rules.record_phase_rule_overrides()` | — | — | Not stale, because PROTECT **blocks** the delete. Append-only (`models.py:7898-7900`) |
| S8 | Successor `Task.due_date` under `Project.cascade_scheduling=True` | predecessor chain | `project_recalculate_dates` | No | Possibly stale: the `TaskDependency` edge is cascaded away and successor dates are not recomputed. Low impact, since due_date is mostly NULL (memory note, Gantt audit) |
| S9 | Design / COD mirror `Task.status`, if the **mirror task itself** is deleted | DesignAssignment / CodRecord | `sync_design_mirror` / `sync_cod_mirror` | — | Nothing goes stale. The derivation loses its target and logs a warning from then on (e.g. `design_views.py:953-958` for deliveries) |

**Checked and found clean (computed on read, nothing stored):**
- Attendance man-days, crews and no-photo man-days: aggregated per request
  (`attendance_views.py:120`, `:234-235`, `:845-846`). The alert command reads live rows. No
  stored counter exists.
- Material status per category: `get_material_status()` (`models.py:2140`) aggregates live.
- Phase rule R3 "first component received": `first_component_received()` (`models.py:2011`)
  queries live.
- Stock balances: there is **no** stock ledger model. `grep '^class \w*(Stock|Inventory|Ledger|Movement)'`
  finds only `StockLocation` (`models.py:6328`), a place, not a balance.
- Progress %, phase and program rollups: no stored field
  (`grep progress|percent|_count|rollup` over model fields finds only
  `Program.planned_site_count`, an input).
- Payment milestones are written milestone → task (`views.py:10622`, `:10670`, `:11117`), never
  from tasks, so a task delete leaves nothing stale on a milestone.

---

## 5. T4 — hidden side effects

### 5.1 Signals
```
projects/signals.py:12:@receiver(post_save, sender=StatusTransition)
projects/signals.py:44:@receiver(post_save, sender=User)
projects/signals.py:61:@receiver(user_logged_in)
projects/signals.py:78:@receiver(user_logged_out)
```
There is no `pre_delete`, `post_delete` or `m2m_changed` receiver anywhere outside the tests.

### 5.2 `delete()` overrides reachable from the three targets
All of these raise `AppendOnlyViolation`:
- `ContractorAttendanceRevision` (`:8405`)
- `DeliveryChallanAttachment` (`:1824`)
- `DCLineDamagePhoto` (`:1913`)
- `DeliveryChallanRevision` (`:1972`)
- `PhaseRuleOverride` (`:7898`)
- `PunchPointEvent` (`:6634`)
- `QaqcVisitAnswer` (`:7123`)
- `StatusTransition` (`:2444`)

Each guards only `instance.delete()`. The collector's own cascade and `QuerySet.delete()` run
SQL and never call them, as the models state, for example `models.py:2363-2365`:
> QuerySet.update() and QuerySet.delete() operate in SQL and bypass both overrides entirely.

So the guard that actually stops a purge is the **PROTECT** on each FK, not the `delete()`
override.

### 5.3 Storage objects (no FileField anywhere; every file is a bucket + path string)

| Model | Columns | Reached by |
|---|---|---|
| ContractorAttendance | `photo_bucket`, `photo_path` (`:8235-8236`) | attendance delete |
| DeliveryChallanAttachment | `bucket`, `path` (`:1774-1775`) | DC delete (PROTECT) |
| DCLineDamagePhoto | `bucket`, `path` (`:1861-1862`) | DC → line (PROTECT) |
| TaskAttachment | `supabase_path` (`:1448`) | Task CASCADE |
| ChecklistItemCompletion | `photo_supabase_path` (`:4416`) | Task CASCADE |
| PunchPointEvent / QaqcVisitAnswer | `photo_bucket`, `photo_path` | Task → PunchPoint (PROTECT) |

A row delete removes none of these objects. Attendance revisions keep replaced photo paths in
their `before` JSON (`models.py:8198-8199`), so the set of objects for one attendance entry is
the current path plus every path in its revisions.

### 5.4 TaskAdmin docstring, checked claim by claim (`admin.py:316-346`)

| Docstring claim | Verdict | Evidence |
|---|---|---|
| "CASCADES six children away — TaskAttachment, Comment, DueDateChangeLog, ChecklistItemCompletion, PunchPoint, and TaskDependency on BOTH" | **True**, and incomplete: Comment also cascades to reply Comments via `Comment.parent` (`models.py:1561`) | §2.3, §3.1 |
| "**Nothing pointing at Task is PROTECT**, so none of this raises" | **FALSE today.** `QaqcVisitAnswer.task`, `ContractorBillTask.task` and `PhaseRuleOverride.task` are PROTECT, and the PunchPoint cascade reaches PROTECT on `PunchPointEvent.punch_point` and `QaqcVisitAnswer.punch_point` | `models.py:7067`, `:7792`, `:7828`; walk in §3.1 |
| "StatusTransition reaches a Task through `subject_type`/`subject_id`, not a FK … left pointing at a subject_id that no longer resolves" | True | `models.py:2373-2377` |
| "They cannot be cleaned up afterwards either: `delete()` raises AppendOnlyViolation by R-4" | True for `instance.delete()`. `QuerySet.delete()` can remove them; that is the bypass §0 rules on | `models.py:2444-2447`, `:2363-2365` |
| "ActivityLog reaches a Task the same soft way and orphans the same way" | True that it orphans. **Unlike** StatusTransition, ActivityLog has no `delete()` override and can be cleaned up | `models.py:1536-1537`; ActivityLog absent from the override list |
| "STRANDS Supabase objects. TaskAttachment carries `supabase_path`, and `purge_deleted_files` only ever walks rows with `is_deleted=True`" | True, and incomplete: `ChecklistItemCompletion.photo_supabase_path` is stranded the same way | `purge_deleted_files.py:38`, `:49` filter `is_deleted=True`; `models.py:4416` |

Recorded in `SECONDARY_FINDINGS.md`; the docstring is not edited here (read-only session).

---

## 6. T5 — billing, approval and COD/HOTO exposure

| Target | Reference path | Effect |
|---|---|---|
| ContractorAttendance | **None.** No FK into it apart from its own revisions (§2.1). `ContractorBillDetail` has no attendance field (`models.py` `ContractorBillDetail`: request, project, amount, bill_number, bill_date, pdf_*, tasks). `grep -il attendance` over `approvals.py`, `approval_*.py`, `payments.py`, `payment_views.py` and `bill_rules.py` returns nothing | Billing reads attendance only as a human input (the Summary's man-days, `models.py:8175-8176` "A contractor-billing input"). There is no stored link, so deleting an entry silently changes the man-days any later look-up shows for a period that may already have been billed |
| DeliveryChallan (+children) | **None** in money, approval or closeout code (`grep -il "challan|DCLineItem"` over `approvals.py`, `approval_*.py`, `payments.py`, `payment_views.py`, `order_views.py`, `purchases_views.py`, `bill_rules.py`: no hits). `po_number` is free text (`:1603-1605`) | None |
| Task | `ContractorBillTask.task` PROTECT (`:7792`) → `ContractorBillDetail.request` O2O → `ApprovalRequest`. Round snapshots hold task ids in JSON (`approvals.py:558`) | A billed task cannot be deleted (PROTECT) |
| Task | COD: `CodRecord` hangs off `project`, not a task (FKs: project, recorded_by, withdrawn_by). HOTO has no model | None directly. Deleting the COD or HOTO **mirror task** only stops its sync (S9) |

---

## 7. T6 — snapshot feasibility

- There are no custom field classes (`grep "^class \w+\((models\.)?\w*Field\)"` finds none) and
  no FileField or ImageField on any model in scope.
- Field types in scope: BigAutoField, Boolean, Char, Date, DateTime, Decimal, ForeignKey, JSON,
  PositiveInteger, PositiveSmallInteger, Text, Time, UUID (`DeliveryChallan.client_uuid`). All
  are handled by `django.core.serializers` ('json').
- A snapshot records `bucket` and `path` only, **not file bytes**. A purge that removes storage
  objects cannot be undone from the DeletionLog.
- Live serialisation of collected sets: **not run, by ruling (2 Nov)**, for the same reason as
  §3.2. The verdict above rests on field types only.

---

## 8. T7 — counts (fresh restore)

Recorded only, by ruling. No decision rests on these numbers now that hard delete is out.

**Source:**
- Restore: local database `solarpms_0211`, from a production dump taken **2026-11-02 19:07 IST**
  (archive header 19:07:27). The user ran both the dump and the restore.
- Run: `dela_readonly.py solarpms_0211` (READ ONLY transaction, rolled back). Server time at the
  run was 2026-11-02 15:14:31 UTC.
- Freshness (latest rows in the restore, UTC):
  ```
  ActivityLog max timestamp:            2026-11-02 13:30:30
  StatusTransition max occurred_at:     2026-11-02 10:43:12
  ContractorAttendance max recorded_at: 2026-11-02 12:17:58
  ```
  The latest ActivityLog row is 13:30 UTC, which is 19:00 IST: seven minutes before the dump.

Test flag: **`Project.is_test`** (`models.py:145-148`):
```python
    # Seed / walkthrough / pilot data. Set by `flag_test_projects` or the Django admin, never
    # inferred from a project_id prefix in code (decision D4) …
    is_test                   = models.BooleanField(
        default=False, db_index=True,
        help_text="Test or demo data. Hidden from the CEO dashboard.",
```

**Raw output** (columns: `test` = rows on `is_test` projects, `real` = everything else,
`soft_deleted` = rows on `is_deleted` projects, counted within `real`):
```
== T7 test flag: Project.is_test
  projects: {'total': 273, 'test': 11, 'soft_deleted': 17}
  test-flagged project ids: ['TESTTENDER26-MB001' … 'TESTTENDER26-MB011']   (11, consecutive)

== T7 Attendance
  ContractorAttendance: {'total': 7, 'test': 0, 'real': 7, 'soft_deleted': 0}
    voided: 0 | date range: {'a': 2026-10-31, 'b': 2026-11-02}
  ContractorAttendanceRevision: {'total': 7, 'test': 0, 'real': 7, 'soft_deleted': 0}
  entries with zero revisions: 0
  entries with a photo: 2

== T7 Delivery challan
  DeliveryChallan: {'total': 20, 'test': 0, 'real': 20, 'soft_deleted': 5}
  DCLineItem: {'total': 34, 'test': 0, 'real': 34, 'soft_deleted': 11}
  DeliveryChallanAttachment: {'total': 0, ...} withdrawn: 0
  DeliveryChallanRevision: {'total': 1, 'test': 0, 'real': 1, 'soft_deleted': 0}
  DCLineDamagePhoto: {'total': 0, ...} withdrawn: 0
  PhaseRuleOverride on a DC: {'total': 10, 'test': 0, 'real': 10, 'soft_deleted': 0}
  Issue.delivery_challan set: {'total': 1, 'test': 0, 'real': 1, 'soft_deleted': 1}
  lines with a GRN (received_quantity set): 21
  DCs blocked by PROTECT (any attachment/revision/override/damage photo): 10 of 20 | of which on test projects: 0
  StatusTransition rows on DCs: 24 | orphaned already (subject gone): 0
  ActivityLog rows naming a DC (entity_type): [{'entity_type': 'DeliveryChallan', 'n': 42}]
  DC on OPEX projects (mirror sync applies): {'total': 14, 'test': 0, 'real': 14, 'soft_deleted': 0}

== T7 Task
  Task: {'total': 4536, 'test': 0, 'real': 4536, 'soft_deleted': 551}
  Tasks with a direct PROTECT child (QaqcVisitAnswer/ContractorBillTask/PhaseRuleOverride): 4 | on test projects: 0
    by kind: qaqc 0 bill 0 override 4
  StatusTransition rows on tasks: 631 | tasks with >=1 ledger row: 432 | orphaned already: 0
  ActivityLog rows with entity_type=Task: 2473 | orphaned already: 4
  TaskAttachment (live / soft-deleted): 12 / 5
  ChecklistItemCompletion with photo path: 0
  PunchPoint: 0 | with events: 0
  mirror tasks (is_mirror=True): {'total': 920, 'test': 0, 'real': 920, 'soft_deleted': 0}
```

**What the counts say:**
- **A test-data purge would have had nothing to purge.** All 11 test projects have zero
  attendance, zero challans and zero tasks. Every row in the three target tables is on a real
  project.
- **Attendance:** 7 entries, all real, none voided, and each with exactly one revision (the
  CREATED row). Each one sits behind the `ContractorAttendanceRevision.attendance` PROTECT (§2.1).
- **Challans:** 10 of the 20 are already blocked by PROTECT. The blockers are 1 revision and
  10 phase-rule overrides; there are no attachments and no damage photos. 21 of the 34 lines
  carry a GRN. 14 of the 20 are on OPEX sites, where a delete would leave the delivery mirrors
  stale (§4 S1).
- **Tasks:** 4 are blocked by a PhaseRuleOverride PROTECT. 432 tasks have ledger rows, which a
  delete would orphan (§4 S2). 4 ActivityLog Task rows are already orphaned today (their
  `entity_id` names no task). They were left by an earlier delete.

**Staff users without a `UserProfile`** (the `AdminAccessMiddleware` exposure, §9 constraint 4 and
`SECONDARY_FINDINGS.md`):
```
== Staff users without a UserProfile
  count: 0
  superusers: [(1, 'admin', True)]
  is_staff users by profile role: [{'profile__role': 'Admin', 'n': 1}]
```
Cross-checked in psql (`BEGIN READ ONLY … ROLLBACK`):
```
 id | username | is_staff | is_superuser | is_active | role
----+----------+----------+--------------+-----------+-------
  1 | admin    | t        | t            | t         | Admin
(1 row)

 users_total | users_without_profile
-------------+-----------------------
          73 |                     0
```
No user anywhere lacks a profile today. Exactly one `is_staff` user exists, and it is the
superuser `admin`, with role Admin. The middleware gap is therefore latent, not live.

---

## 9. T8 — voiding attendance from the admin

Signature (`projects/attendance.py:434-464`):
```python
def void_attendance(entry, note, by):
    note = (note or '').strip()
    if not note:
        raise AttendanceRefused(VOID_NOTE_REQUIRED)
    with transaction.atomic():
        locked = _lock(entry)
        if not (user_can_change_attendance_today(by, locked)
                or user_can_correct_attendance(by, locked)):
            raise AttendanceRefused(NOT_CHANGEABLE)
        before = attendance_snapshot(locked)
        now = timezone.now()
        locked.is_voided = True
        locked.void_note = note
        locked.voided_by = by.profile
        ...
        _revision(locked, ContractorAttendanceRevision.VOIDED, before, by.profile,
                  note=note, now=now)
    return locked
```
`_lock` is `select_for_update(of=('self',))` (`attendance.py:287-293`).

**Can an admin bulk action call it once per row? Yes.** Doing so preserves everything the
writer guarantees: the row lock, the VOIDED revision with its `before` snapshot, the void
CHECKs and the freed duplicate slot. These are the constraints the action must respect:

1. **Same-day entries are refused for an Admin.** `user_can_change_attendance_today`
   (`permissions.py:3605-3630`) passes only `user_can_log_attendance` (SE on the site, or
   PM/coordinator via `user_can_manage_project`) or the entry's own recorder.
   `user_can_manage_project` (`permissions.py:39-66`) is PM or coordinator only; there is no
   Admin arm. Past-day entries pass through `user_can_correct_attendance` →
   `user_is_attendance_corrector`, where `ATTENDANCE_CORRECTOR_ROLES = frozenset({'Admin'})`
   (`:3662`, `:3685-3686`), with **no date window**. A void of today's entry is therefore
   refused with `NOT_CHANGEABLE` until midnight IST.
2. **Already-voided entries are refused** (no un-void; both helpers return False on
   `is_voided`).
3. **Soft-deleted sites are refused** (`user_is_attendance_corrector`, `:3679`). Any other
   project status is accepted.
4. **`by` must have a `profile`**, because `by.profile` is dereferenced. `AdminAccessMiddleware`
   admits profile-less staff as 'Admin', so the action must refuse such a user up front. (No
   such user exists today, §8, but nothing prevents one being created.)
5. **One transaction per row.** A bulk action is partial by nature. It must catch
   `AttendanceRefused` per row and report "n voided, m refused (reason)", never wrap the whole
   batch in one atomic block (a single refusal would roll back the others).
6. **Note:** one note for the batch, entered in an intermediate confirmation form (the
   `delete_selected` pattern), stripped, non-blank.
7. The admin class is `_ApprovalRecordAdmin` (no change permission). A custom action with an
   explicit superuser check does not need the change permission and does not write through
   the form, so the "an admin form would write past both" reason at `admin.py:1001-1003` still
   holds.

---

## 10. Recommendation for v1 (ruled 2 Nov 2026)

**There is no general hard-delete screen.** The test-data purge idea is withdrawn along with
it. v1 is these three items:

1. **Attendance: an admin bulk "Void" action** on `ContractorAttendanceAdmin` that calls
   `attendance.void_attendance(entry, note, request.user)` once per selected row.
   - Mandatory note, entered once in an intermediate confirmation form (the `delete_selected`
     pattern), stripped and non-blank.
   - Each row in its own transaction, as the writer already provides. Catch
     `AttendanceRefused` per row and report "n voided, m refused" with the reasons.
   - Superuser check in the action; refuse a user with no `profile` before calling the writer
     (T8 point 4).
   - Nothing else changes: the row lock, the VOIDED revision with its `before` snapshot and the
     freed duplicate slot all come from the writer. Every T8 constraint applies.
2. **An Admin path in the same-day void check.** Today `user_can_change_attendance_today`
   (`permissions.py:3605-3630`) has no Admin arm, so an Admin can void only past-day entries
   (T8 point 1). Add one, scoped as `user_is_attendance_corrector` already is (role in
   `ATTENDANCE_CORRECTOR_ROLES`, site not soft-deleted). That is a `permissions.py` change and
   its tests belong to the build session. The same helper also gates same-day **edits** and
   **time out**, so the build must decide whether the Admin arm covers voids only (a separate
   predicate inside `void_attendance`) or every same-day change.
3. **Hard delete stays out.** The 14 Oct ruling stands: "This is not a precedent"
   (`execution-model.md:852`), and `demo-data.md:199` ("Do not generalise it") is unchanged.
   `TaskAdmin.has_delete_permission` stays False. Challans, attachments, damage photos and
   revisions keep their PROTECT and withdraw-not-delete design. Any future production wipe goes
   through its own ruling and its own one-off command, as on 14 Oct.

**What each target's status is now:**

| Target | v1 | Why |
|---|---|---|
| Contractor attendance | **Void, not delete** (items 1, 2) | Every entry has a revision (PROTECT); the designed undo exists and keeps history (§3.1, §9) |
| Delivery challan (+children) | **Excluded** | PROTECT from attachments, revisions, damage photos and R1 overrides; an orphaned ledger row at birth; stale delivery mirrors S1 (§2.2, §4) |
| Task | **Excluded** | Five PROTECT paths, ledger orphaning, stranded storage objects (§2.3, §5.4) |

If a hard delete is ever reopened, these findings set its minimum rules:
- refuse on any PROTECT child, never bypass it;
- call `sync_delivery_mirrors(project)` after a DC delete;
- remove storage objects only after commit;
- decide the R-4 question explicitly before anything is built.
