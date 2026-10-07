# Closeout mirrors, derived-tag behaviour, checklist readiness — audit

**Audit-only.** No application code, migration or data was changed. Local database
`solarpms_local` on `localhost` only; production was not contacted. HEAD at audit time:
`203ab65`. Everything is located by function, class or template name. Quoted code is
verbatim. Query output is pasted as returned.

The QMS-01 reference was found already in the repository working tree, gitignored:
`docs/QA QC Check List_Final/QA QC Check List_Final/QA-QC Checklist HRPPL_R00 Dt. 23.08.2026.pdf`
(14 pages, header `Doc. No. Project-QMS-01_R00_Dt. 23.8.26`, "Prepared By: Nirankar Mishra,
GM-Projects, HRPPL"). Part 4 reads that file.

---

## 0. Where the brief's CONTEXT is wrong or out of date

Each item below is a finding. Evidence for each is in the part named.

| # | The brief, or the code's own comment, says | What is true | Part |
|---|---|---|---|
| C-1 | Mirrors are Design, the four deliveries, COD, As-Built and HOTO | **Correct: eight mirrors.** But rung 0's own comment says "Five OPEX tasks carry the flag today (Design, Material Delivery, COD, As-Built Drawings, HOTO)". It predates the four-way delivery split. The `Task.is_mirror` model comment also says "five", and the test `tests_opex_activation.MirrorsSurviveTheAttachTests.test_five_mirrors_are_created_asserted_by_name` still has "five" in its name. | 1c, 2 |
| C-2 | Rung 0 refuses mirrors | **Correct.** But the `Task.is_mirror` field comment in `models.py` still says "THE HUMAN-WRITE REFUSAL IS STILL NOT BUILT". That comment is stale. | 1c |
| C-3 | Mirrors are excluded from overdue | **Partly true.** Most counters exclude them. Four overdue readers deliberately include them. `apply_mirror_status()`'s docstring says "R-20 keeps mirrors out of every overdue count", which is false. | 1d |
| C-4 | COD, As-Built and HOTO have no writer | **Confirmed.** No writer, model, field, view, flag or stub exists. `derivation_source` exists only as a comment. | 2c |
| C-5 | Completion Certificates is an entered task | **Confirmed.** `COMPLETION_CERTIFICATES_PAPERWORK`, `is_mirror=False`, role Project Coordinator. | 2 |
| C-6 | The HOTO override rule ("approval row carrying `is_override` and a mandatory reason") comes from `docs/OPEX_task_template_spec.md` | **Wrong source.** The spec says only "Refuses without as-built unless overridden". The `is_override` wording is in `docs/execution-model.md` §9 "Business rules confirmed with the Tenders team". | 3c |
| C-7 | "B-11 decided HSE clearance is once per site at mobilisation" | **Mislabelled.** The once-per-site decision is dated 24 Aug 2026 (`execution-model.md` §9 and its decision table) and labelled D-6 in `docs/PHASE_1_OUTCOME.md`. **B-11** is the *open* question: "The actual HSE mobilisation checklist items — a named list, not a category." Phase 2.2 is "Blocked on B-11". | 4c |
| C-8 | `docs/OPEX_task_template_spec.md` §4: "none has a derivation hook, so all 8 read Not Started" | **Stale.** Design and the four deliveries derive live today. | 2 |
| C-9 | `docs/CHECKLIST_CONTENT_MAPPING.md` "Task 6" describes the master QA/QC checklist as ending in "Accepted / Accepted-with-Punch-Points / Rejected" | **QMS-01 has four outcomes.** It adds **Re-inspection Required**. | 4d |

---

## Part 1 — the derived tag, end to end

### 1a. Which template renders the "Derived" chip, and on what condition

**Only one template renders it: `projects/partials/_task_row.html`.** The chip is shown on `task.is_mirror` alone.

```django
{% if task.is_mirror %}
  {% comment %}A MIRROR SAYS WHY IT CANNOT BE EDITED, not merely that it is special.

     Eight rows on an OPEX site never change status and, until this, the page gave
     no reason — ...
     PRESENTATION ONLY. The guarantee is B22's check at rung 0 of
     `_apply_task_status_change()`, which refuses a human write on is_mirror for both
     entry points. ...{% endcomment %}
  <span class="badge bg-secondary-subtle text-secondary-emphasis border ms-1"
        style="font-size:.62rem;font-weight:500;vertical-align:middle;"
        title="Derived from the workspace that owns this work — its status updates itself and cannot be set here.">
    <i data-lucide="link" style="width:9px;height:9px;vertical-align:-1px;"></i> Derived
  </span>
{% endif %}
```

The template comment's "Eight rows on an OPEX site never change status" is stale: five of the eight now change status by derivation (Part 2).

Every `is_mirror` occurrence in `projects/templates/` (grep):

```
projects/templates/projects/partials/_task_row.html:      {% if task.is_mirror %}   (chip)
projects/templates/projects/partials/_task_row.html:      (inside the chip comment)
projects/templates/projects/partials/_task_row.html:      {% comment %}`not task.is_mirror` added by prompt 1.5 ...
projects/templates/projects/partials/_task_row.html:      {% if not task.is_mirror and not task.is_not_applicable and is_assigned_pm or not task.is_mirror and not task.is_not_applicable and user_task_role == task.assigned_role %}
projects/templates/projects/partials/_task_row_approval.html: {% if two_step_completion and not task.is_mirror %}
```

**Not on the task-detail page.** `projects/task_detail.html` and `projects/partials/_task_detail_status.html` contain no `is_mirror` and no "Derived" text.

### 1b. What a human sees on a mirror row

**Project overview row (`_task_row.html`).**

- **Status: the control is hidden, not disabled.** A mirror falls through to the read-only badge branch:

  ```django
  {% else %}
    ...
    {% if task.is_not_applicable %}<span class="badge bg-light text-muted border" ...>Not Applicable</span>
    {% elif task.status == 'Done' %}<span class="badge" style="background:var(--hz-green)">Done</span>
    {% elif task.status == 'Blocked' %}<span class="badge bg-danger">Blocked</span>
    {% elif task.status == 'In Progress' %}<span class="badge bg-primary">In Progress</span>
    {% else %}<span class="badge bg-secondary">Not Started</span>{% endif %}
  {% endif %}
  ```

- **Source naming.** The chip's tooltip is the only text. It names no specific source ("the workspace that owns this work"). Delivery mirrors also show a consignment count, "N/M consignment(s) received" or "No consignments yet", from `delivery_detail_for_task()`.
- **Assign and due date are still shown on mirror rows.** These conditions have no mirror term:
  - Assign button: `{% if is_assigned_pm %} … onclick="openAssignModal(this)"`.
  - Design Head reassign: `{% if task.assigned_role == 'Design' and request.user.profile.is_design_head %}`.
  - Due-date input: `{% if is_assigned_pm %}` with the comment `{# PM: always editable #}`.
  - Non-PM due-date input: `{% elif not project.cascade_scheduling and task.assigned_role == user_task_role and role != 'Finance' and role != 'Admin' %}`.
  - Delayed / Due Today label, per its comment: "a Derived row is marked like any other".

**Task-detail page (`_task_detail_status.html`).** The control is **present but refused** whenever the viewer is the mirror's assignee:

```django
{% if is_assignee %}
  <form method="post"
        action="{% url 'task_detail_status_update' project.project_id task.pk %}" ...>
    ...
    <select name="status" ...>
```

`task_detail()` computes `is_assignee = task.assigned_to is not None and task.assigned_to == profile`. There is no mirror term and no chip. An assigned mirror shows its assignee a live select; picking a value posts, and rung 0 refuses with the message quoted in 1c. One mirror is assigned in the local DB (Part 1d), so this state exists.

The approval block (`_task_approval.html`, via `_task_approval_context`) is suppressed: `eligible = project.project_type == 'OPEX' and not task.is_mirror`. The Not Applicable controls are suppressed by `permissions.user_can_mark_task_not_applicable` (`if task.is_mirror: return False`).

### 1c. Rung 0, and every other path that writes `Task.status`

**Rung 0, `views._apply_task_status_change()`.** This is the function's first statement after the docstring and comment block, and it is unconditional:

```python
    if task.is_mirror:
        messages.error(
            request,
            f"'{task.task_name}' is a mirror task — its status is derived from the "
            f"workspace that owns the work and cannot be set here. It will update "
            f"itself when that record changes."
        )
        return _TASK_STATUS_REFUSED
```

It sits above the `valid_statuses` check, above rung 1 (the OPEX `approved_at` gate), and above the inline `due_date` write. A refused mirror move therefore writes nothing.

**Stale comments around it (findings, not defects):**

- The rung-0 comment: "Five OPEX tasks carry the flag today (Design, Material Delivery, COD, As-Built Drawings, HOTO)". There are eight (Part 2).
- The rung-0 comment: "The derivation hooks that will WRITE these statuses are unbuilt." Two are built.
- `models.Task.is_mirror` field comment: "THE HUMAN-WRITE REFUSAL IS STILL NOT BUILT … a mirror is protected only by having no assignee".

**Every other writer of `Task.status`.** Test files are excluded. Rows marked † were re-read directly for this report; the rest come from a read-only sweep that quoted the code verbatim.

| # | File · function | How it writes | `is_mirror` handling | Reached by | Ledger |
|---|---|---|---|---|---|
| A † | `design_views.apply_mirror_status` | `Task.objects.filter(pk=task.pk).update(**fields)` with `fields = {'status': new_status}` | **The designated mirror writer. It refuses non-mirrors:** `if not task.is_mirror: raise ValueError(...)` | Only B and C | `record_transition(task, …, actor=actor, reason_code=reason_code)` |
| B † | `design_views.sync_design_mirror` → A | indirect | Looks up the row with `is_mirror=True, template_task__code=DESIGN_MIRROR_CODE` | `apply_design_status` hook; `utils.attach_opex_template` reconcile; **`fix_fixtures.py` (repo root, see below)** | `REASON_MIRROR_DERIVED` |
| C † | `design_views.sync_delivery_mirrors` → A | indirect | Looks up rows with `is_mirror=True, template_task__code__in=DELIVERY_MIRROR_CODES` | `models.recalculate_dc_status` (← `confirm_grn`, `override_grn`); `views.create_delivery_challan` | actor `None`, `REASON_MIRROR_DERIVED` |
| D † | `views.milestone_receive` | `Task.objects.filter(phase__project=milestone.project, task_name=_sync_task_name, status__in=[…]).update(status=Task.DONE, completed_at=timezone.now())` | **No `is_mirror` check.** It is kept off mirrors only because it filters on name; the three names are Residential Finance tasks. | Finance | `REASON_MILESTONE_SYNC` |
| E † | `views.project_overview`, POST `update_milestone`, Finance branch | the same `.update(status=Task.DONE, …)` | **No `is_mirror` check.** Same name-only safety as D. | Finance | `REASON_MILESTONE_SYNC` |
| F | `management/commands/seed_opex_test_data.Command._vary_task_statuses` | `Task.objects.filter(pk=task.pk).update(**updates)` | **Skips mirrors:** `Task.objects.filter(phase__project=site, is_mirror=False)` | CLI seed only | actor = demo PM |
| G † | `views.task_set_due_date` | full-row `task.save()` with no `update_fields` (four sites in the function) | **No `is_mirror` check.** It does not change status on purpose, but it rewrites the status column from the copy loaded at request start. It can therefore overwrite a mirror status derived concurrently, with no ledger row. | PM (any task on their project) or the task's role | none |
| H | `utils.calculate_due_dates` | full-row `task.save()` in a loop | no check | `project_recalculate_dates`, `enable_cascade_scheduling`. Both refuse non-Residential (B18), so this never reaches a mirror. | none |

**Checked: these paths do not write `Task.status` (or route through rung 0):**

- `task_status_update` and `task_detail_status_update` call only `_apply_task_status_change`.
- `task_submit_for_approval`, `task_approve` and `task_reject` are gated by `_approval_preconditions`, which has `if task.is_mirror: … return False`. They then reach Done or In Progress only through `_apply_task_status_change`.
- `punch_point_waive` writes `PunchPoint.status` only.
- `task_set_not_applicable`: its docstring says "The task's `status` is never read or written here".
- `task_add` and `task_duplicate_locations_create` create tasks at the default status; duplicate sets `is_mirror=False`.
- `_attach_task_template` `bulk_create` passes no status and copies `is_mirror=t.is_mirror`.
- `task_rename_save` writes `task_name`/`location_label` only. `phase_tasks_reorder` writes `task_order`. `recalculate_from_task` writes `due_date`. `assign_task_to` and `assign_tasks_to` write `assigned_to`.
- Gantt: `compute_gantt_schedule` and `build_gantt_view` are read-only and Residential-only.
- **Django admin, `TaskAdmin`:** `readonly_fields = ['status', 'is_mirror']`, no `list_editable`, no actions. `ProjectAdmin` also has `status` read-only.
- `signals.py` has no Task writes. No data migration writes `Task.status`.
- `seed_walkthrough` drives the real views through the test `Client`, so it goes through rung 0.

**A third production caller of `sync_design_mirror` exists outside `projects/`.** `fix_fixtures.py` (repo root, tracked):

```python
actor = UserProfile.objects.get(user__username='demo.designhead')
...
        result = sync_design_mirror(p, da.status, actor)
```

`tests_design_mirror_derivation.CallerDisciplineTests.test_02_sync_design_mirror_has_exactly_two_production_callers` does not see it. All five actor-attributed Design-mirror ledger rows in the local DB came from this script (2e).

### 1d. Assignment and dates on mirrors; exclusion from counters and current phase

**Can a mirror be assigned? Yes. No assignment path checks `is_mirror`.**

- `views.task_assign` and `views.task_assign_design_head` contain zero occurrences of `is_mirror` (grep of each function body).
- The chokepoint `utils.assign_task_to` / `assign_tasks_to` goes straight to `.update(assigned_to=…)`.
- `TaskAdmin.save_model` routes through `assign_task_to`. `assigned_to` and `due_date` remain editable in the admin.
- Only `utils.attach_opex_template` excludes mirrors, and only when pre-assigning the PM: `Task.objects.filter(phase__project=project, assigned_role=Task.PM, is_mirror=False)`.
- `tests_mirror_readonly.TheTrapTests.test_assigning_a_mirror_is_still_permitted_and_still_pointless` pins this as intended.
- Local DB: `ASSIGNED MIRROR: 3396 DESIGN SCMPILOT01 demo.design (Design) Done`.

**Can a mirror be given a due date? Yes.** `views.task_set_due_date` has zero `is_mirror` occurrences, and the PM branch saves unconditionally. The inline due-date write inside `_apply_task_status_change` cannot reach a mirror because rung 0 comes first. Local DB: 0 of 48 mirrors carry a due date today.

**Metric exclusion.** The rule is stated in `utils.py`:

- `human_owned_tasks_q()` → `return Q(**{f'{prefix}is_mirror': False})`. Used for workload, overdue, pending and blocked.
- `site_progress_tasks_q()` → `return Q()`. Used for progress; mirrors stay in the denominator deliberately.
- `is_human_owned(task)` → `return not task.is_mirror`.
- `task_health.overdue_q` applies no mirror filter. Its docstring says scope is each caller's job.

*Overdue — copies that INCLUDE mirrors:*

| Copy | Evidence | Status |
|---|---|---|
| `views.dashboard_site_engineer` "next task" / `next_overdue` | "picks mirrors and N/A tasks (DEFERRED G33)" † | known, deferred |
| `views._get_ceo_dashboard_context` `at_risk_subq` (CEO At Risk badge; also feeds tender-card health) | "Mirrors are still not excluded, as before (DEFERRED G33)." † | known, deferred |
| `task_report.report_tasks_q` / `build_task_reports` (morning report) | "mirrors INCLUDED. Production assigns delivery mirror tasks to people …" † | deliberate |
| `views._attach_due_health` (Delayed / Due Today label on rows) | "Mirrors are included: a Derived task with a date can be late like any other." † | deliberate |
| `views._get_ceo_dashboard_context` `blocked_subq` (CEO Blocked badge) | `Task.objects.filter(phase__project=OuterRef('pk'), status=Task.BLOCKED)` | deliberate per `sync_design_mirror` docstring (Design Hold → Blocked) |

*Overdue — copies that EXCLUDE mirrors (base queryset carries `human_owned_tasks_q()`):*

- `tasks_drill_down`
- `dashboard_pm`: `_urgency`, `overdue_tasks_for_project`, `_pm_task_base`
- `dashboard_site_engineer`: annotations, `_se_task_base`
- `dashboard_design` `_design_task_base`
- `dashboard_scm` `_scm_task_base`
- `_ceo_task_base` and everything built on it, including `dashboard_ceo_overdue_tasks`
- `tender_stages` stuck-sites query
- `reports.build_user_status_rows`
- `send_eod_digest` (per-user, coordinator and company totals)

*Workload — copies that EXCLUDE mirrors:*

- PM headline and per-project cards
- SE `blocked_count` and SE "my work" bar
- the CEO ~40 department counters (on `_ceo_task_base`), `top_assignees` and `top_completed`
- `build_user_status_rows`
- EOD digest

The morning report **includes** mirrors, as noted above. `dashboard_pm`'s `due_today_tasks`, `blocked_tasks_list`, `pending_approvals_list`, `external_pending_list` and `team_due_today` include them, but these are dead context keys that no template reads.

*Progress % — INCLUDES mirrors, deliberately; excludes N/A:*

- `dashboard_pm` `internal_percent`
- CEO card `task_total_count` / `task_done_count`
- `project_overview` per-phase `pct`

`models.py`, beside the Not Applicable fields, gives the reason: "`is_mirror`, which stays IN the progress denominator precisely because an undelivered consignment IS outstanding work". `tender_stages.activated_progress` reads **only** delivery mirrors for "material delivered".

*Current phase — EXCLUDES mirrors; there is one implementation.* `utils.current_phase(project)`:

```python
if not is_human_owned(task) or not is_applicable(task):
    continue
```

`models.Project.get_current_phase()` delegates to it (`phase = current_phase(self)`). `dashboard_pm`, `dashboard_site_engineer` and `dashboard_bd` call it through the import alias `get_current_phase`. No other copy exists, and `tests_current_phase.py` pins the call sites.

**Stale claim:** `apply_mirror_status()` docstring, "R-20 keeps mirrors out of every overdue count". Four overdue readers above include them.

---

## Part 2 — every mirror, its source, and its writer

Template rows, from a local DB query of `TaskTemplateTask.objects.filter(is_mirror=True)`:

```
TEMPLATE {'id': 1, 'code': 'RESIDENTIAL', ..., 'version_no': 1, 'status': 'active', ...}
TEMPLATE {'id': 6, 'code': 'OPEX', 'label': 'OPEX Execution', 'project_type': 'OPEX', 'version_no': 1, 'status': 'active', 'effective_from': datetime.date(2026, 9, 1), ...}

{'id': 103, 'phase_id': 26, 'code': 'DESIGN', 'label': 'Design', ..., 'assigned_role': 'Design', ..., 'is_mirror': True, 'phase_name': 'OPEX v1 / Design'}
{'id': 106, 'phase_id': 28, 'code': 'DELIVERY_SOLAR_PANELS', ..., 'assigned_role': 'SCM', ..., 'phase_name': 'OPEX v1 / Procurement & Delivery'}
{'id': 107, 'phase_id': 28, 'code': 'DELIVERY_INVERTERS', ..., 'assigned_role': 'SCM', ...}
{'id': 108, 'phase_id': 28, 'code': 'DELIVERY_BOS_KIT', ..., 'assigned_role': 'SCM', ...}
{'id': 109, 'phase_id': 28, 'code': 'DELIVERY_MMS', ..., 'assigned_role': 'SCM', ...}
{'id': 122, 'phase_id': 32, 'code': 'COD', 'label': 'COD', 'sort_order': 1, 'assigned_role': 'PM', ..., 'phase_name': 'OPEX v1 / Closeout'}
{'id': 124, 'phase_id': 32, 'code': 'AS_BUILT_DRAWINGS', 'label': 'As-Built Drawings', 'sort_order': 3, 'assigned_role': 'Design', ...}
{'id': 125, 'phase_id': 32, 'code': 'HOTO', 'label': 'HOTO', 'sort_order': 4, 'assigned_role': 'PM', ...}
```

There is exactly one OPEX template version (v1, active), so no later version exists to audit. Residential has no mirrors. Closeout phase (`CLOSEOUT`, sort 7) contents:

```
1 COD                                PM                  mirror= True
2 COMPLETION_CERTIFICATES_PAPERWORK  Project Coordinator mirror= False
3 AS_BUILT_DRAWINGS                  Design              mirror= True
4 HOTO                               PM                  mirror= True
```

### The table

All five live writers write via `apply_mirror_status()` and record `reason_code='mirror_derived'` (`models.REASON_MIRROR_DERIVED`).

| code · name | phase | source object | writer | direction | StatusTransition actor / reason | tests |
|---|---|---|---|---|---|---|
| `DESIGN` · Design | 1 Design | `DesignAssignment.status` | `sync_design_mirror` ← hook at the end of `apply_design_status`; reconcile in `utils.attach_opex_template`; also root `fix_fixtures.py` | **reopen-capable**. `released → in_design` via `_open_next_attempt` ← `design_change_request_accept` moves the mirror Done → In Progress. `survey_returned` → Blocked and back. | Hook: the acting `UserProfile` passed to `apply_design_status`. Reconcile: `None` (system). `fix_fixtures.py`: `demo.designhead`. Reason `mirror_derived`. | `tests_design_mirror_derivation`: TheMappingTests, TheLookupTests, TheWriterTests, TheHookTests (`test_01_the_full_real_sequence` includes the reopen, driven via `apply_design_status`, not the accept view), ReconcileAtActivationTests, RungZeroStillRefusesTests, CallerDisciplineTests. Also `tests_mirror_readonly`, `tests_opex_template_correction`. |
| `DELIVERY_SOLAR_PANELS` · Delivery — Solar Panels | 3 Procurement & Delivery | `DCLineItem` rows with `boq_category` = `DCLineItem.SOLAR_MODULES`, across all challans on the site | `sync_delivery_mirrors` | **reopen-capable**: recomputed from the live line set every call. A new unconfirmed line in a Done bucket moves it to In Progress. | `None` → `actor_role_code='system'`; `mirror_derived` | `tests_delivery_detail.AgreementTests` (3 cases, one category constant `PANEL_CATEGORY`, via `recalculate_dc_status`); `CallerDisciplineTests.test_01b_sync_delivery_mirrors_is_called_only_by_the_delivery_paths`; `tests_grn_on_behalf.TaskPanelDisplayTests` (panel only) |
| `DELIVERY_INVERTERS` · Delivery — Inverters | 3 | `DCLineItem.INVERTER` | same | same | same | no bucket-specific test |
| `DELIVERY_BOS_KIT` · Delivery — BOS Kit | 3 | `DCLineItem.BOS` | same | same | same | no bucket-specific test |
| `DELIVERY_MMS` · Delivery — MMS | 3 | `DCLineItem.STRUCTURE` | same | same | same | no bucket-specific test |
| `COD` · COD | 7 Closeout | **none exists** | **NONE** | n/a | n/a: no row is ever written | refusal only: `tests_mirror_readonly.EveryMirrorOnARealSiteTests`, `tests_opex_template_correction.TheRefusalIsUnchangedTests`; identity/count only elsewhere |
| `AS_BUILT_DRAWINGS` · As-Built Drawings | 7 Closeout | **none exists** | **NONE** | n/a | n/a | refusal and identity only; `TheLookupTests.test_01_finds_the_design_mirror_and_not_as_built` proves the Design hook does *not* touch it |
| `HOTO` · HOTO | 7 Closeout | **none exists** | **NONE** | n/a | n/a | refusal and identity only |

### 2a. Design

**Hook.** At the end of `apply_design_status`:

```python
    if new_status is not None and new_status != from_status:
        record_transition(
            assignment, new_status, from_status=from_status, actor=actor,
            reason_code=reason_code, remark=remark,
        )
        ...
        sync_design_mirror(assignment.project, assignment.status, actor)
```

**Mapping.** `DESIGN_MIRROR_STATE_MAP`, a pure function of the stored status. `derive_design_mirror_state()` raises on an unknown value.

- `awaiting_survey`, `awaiting_allocation` → **Not Started**
- `allocated`, `due_date_proposed`, `in_design`, `arka_submitted`, `awaiting_head_arka`, `arka_rejected`, `artifacts_uploaded`, `in_qc`, `awaiting_head_qc`, `qc_failed`, `awaiting_pm_approval`, `pm_rejected` → **In Progress**
- `survey_returned` (Design Hold) → **Blocked**
- `released` → **Done**

**Reopen.** The map's comment:

> "NOT terminal … One reopen route exists (a PM change request accepted by the Head, design_change_request_accept -> _open_next_attempt), which moves released -> in_design and therefore Done -> In Progress with no special case."

`_open_next_attempt` calls `apply_design_status(…)` with target `DESIGN_IN_DESIGN`, and is itself called from:

- `design_qc_fail`
- `design_head_qc_fail`
- `design_head_send_back`
- `design_change_request_accept`

`apply_mirror_status` has no transition table and clears `completed_at` when leaving Done:

```python
    elif task.status == Task.DONE:
        ...
        fields['completed_at'] = None
```

**Activation reconcile** (`utils.attach_opex_template`):

```python
        from .design_views import sync_design_mirror
        assignment = getattr(project, 'design_assignment', None)
        if assignment is not None:
            sync_design_mirror(project, assignment.status, None)
```

### 2b. Deliveries

**Mapping.** `models.DC_CATEGORY_TO_MIRROR_CODE`:

```python
DC_CATEGORY_TO_MIRROR_CODE = {
    DCLineItem.SOLAR_MODULES: 'DELIVERY_SOLAR_PANELS',
    DCLineItem.INVERTER:      'DELIVERY_INVERTERS',
    DCLineItem.BOS:           'DELIVERY_BOS_KIT',
    DCLineItem.STRUCTURE:     'DELIVERY_MMS',
}
```

**Trigger points: exactly two call sites.**

1. **DC create.** `views.create_delivery_challan` calls `sync_delivery_mirrors(project)` inside the atomic block, after the line items are created. This moves a bucket Not Started → In Progress.
2. **GRN.** `models.recalculate_dc_status` calls `sync_delivery_mirrors(challan.project)` outside the "status changed" `if`, inside its atomic block. `recalculate_dc_status` is called by:
   - `views.confirm_grn`: `recalculate_dc_status(challan, actor=profile, reason_code=REASON_GRN_CONFIRMED)`
   - `views.override_grn`: `recalculate_dc_status(challan, actor=profile, reason_code=REASON_GRN_OVERRIDDEN)`

**Line edit.** No DC line-edit view exists. The only way to change received quantities after the fact is `override_grn`, which re-runs the sync.

**Delete.** No view deletes a `DeliveryChallan` or a `DCLineItem`. Neither model is registered in `projects/admin.py`; the registered list has no challan or line model. A shell delete would bypass the sync.

**Activation.** There is **no** delivery reconcile. Only the two call sites above exist (grep of `sync_delivery_mirrors(`). Material received before a site is activated leaves its buckets at Not Started until the next DC or GRN event on that site.

**Short or damaged delivery.** `sync_delivery_mirrors`:

```python
    for code, severities in buckets.items():
        if not severities:
            state = Task.NOT_STARTED
        elif all(sev == 'green' for sev in severities):
            state = Task.DONE
        else:
            state = Task.IN_PROGRESS
```

The severity comes from `models._dc_item_severity`, where green means "full quantity received, no damage". So:

- A short or damaged line holds the bucket at **In Progress**.
- An unconfirmed line (`received_qty is None` → `None`) also holds In Progress.
- A `Rejected` challan is deliberately not filtered out and holds In Progress ("material was ordered and did not properly arrive").

The shortfall itself is carried by `create_delivery_issue`, not by the mirror.

**Untested here** (grep of the test tree):

- the `create_delivery_challan` → In Progress move. No test file that drives DC creation asserts on a mirror.
- full-but-damaged → In Progress
- Rejected challan
- Done → In Progress reversal
- any bucket other than the one `PANEL_CATEGORY` used by `AgreementTests`

Commit `0a106bb`, which added the derivation, changed only `tests_design_mirror_derivation.py` (caller discipline).

### 2c. COD, As-Built, HOTO: is there any writer, model, field, view, flag or stub?

**None of these exists.** Case-insensitive searches across `projects/**/*.py` (excluding tests and migrations) and `projects/templates/**`:

- **`\bcod\b`.** Hits are comments only:
  - `design_views.delivery_detail_for_task` docstring ("would render an empty delivery panel on COD, HOTO, As-Built and Design")
  - `models.Task.is_mirror` comment
  - `utils.py` metric-chokepoint comments ("a COD record", "COD / HOTO / As-Built have no source object in existence to ever …")
  - `utils.attach_opex_template` ("COD and HOTO — PM-role MIRRORS — are left with assigned_to NULL")
  - `views.py` comments: the PM card, `top_assignees` ("holding 190 COD/HOTO mirrors"), and the rung-0 comment
- **`hoto`.** Apart from `photo`, the same comment set, plus `views.py` due-date commentary ("HOTO fall due activated_at + 22 days").
- **`as[_ -]?built`.** Comments in `design_views` (`DESIGN_MIRROR_CODE` block: "As-Built does NOT follow DesignAssignment (it is post-commissioning, and nothing in the design workspace records it)"), `models.py` and `views.py`, plus one **checklist item** seeded by `seed_opex_installation_checklists`: `'As-built documentation done(actual cable Inc data) for record'` (AC Cable Laying).
- **`handover`.** This is design-gate vocabulary (the Head's QC pass is a "handover to the PM"). Plus Residential `'Customer Handover'`, a plain PM task in `build_residential_phases`, and `gantt_constants` display labels.
- **`commission`.**
  - `Project.STATUS_CHOICES` includes `('Commissioned', 'Commissioned')`
  - the field `commissioned_at = models.DateField(null=True, blank=True)  # Set when project status changes to Commissioned`
  - Residential Commissioning-phase task names
  - `tender_stages` counting `status == 'Commissioned'`
  - no writer (Part 3b)

**`derivation_source` was never added.** It appears only in the `Task.is_mirror` comment:

```
    # unwritable. That is why this is a boolean and not a nullable derivation_source
    # enum — ...
    # therefore writable by anyone. `derivation_source` is added BESIDE this in phases
    # 3–5, under a check constraint `derivation_source IS NULL OR is_mirror`.
```

It has no model field and no migration. The live writers find their rows by `template_task__code` instead.

### 2d. A design-workspace artifact kind for As-Built

**Absent.** `DesignFile.kind` choices (`models.DESIGN_FILE_KIND_CHOICES`):

```
DesignFile ['id', 'attempt:ForeignKey', 'kind:CharField[cad_zip,boq_excel,boq_pdf,cad_pdf,cad_dwg]', 'version', ...,
            'derived_from_arka:ForeignKey', ...]
```

Two structural facts bear on any future as-built upload:

- `DesignFile.attempt` is a required FK to `DesignAttempt`, so a file belongs to a design attempt.
- `derived_from_arka` is mandatory:

  ```python
      derived_from_arka = models.ForeignKey(
          ArkaSubmission, on_delete=models.PROTECT, related_name='derived_files',
      )
  ```

The two general upload models have no kind vocabulary that could carry "as-built". `ProjectDocument.file_type` and `TaskAttachment.file_type` are `Document` / `Photo` only.

### 2e. Local DB: mirror tasks by code and status

```
Task.is_mirror rows by project_type:
   {'phase__project__project_type': 'OPEX', 'phase__project__is_deleted': False, 'n': 48}
Mirror tasks by template_task.code x status:
   AS_BUILT_DRAWINGS     Not Started  is_test=True  n=6
   COD                   Not Started  is_test=True  n=6
   DELIVERY_BOS_KIT      Not Started  is_test=True  n=6
   DELIVERY_INVERTERS    Done         is_test=True  n=1
   DELIVERY_INVERTERS    Not Started  is_test=True  n=5
   DELIVERY_MMS          Done         is_test=True  n=1
   DELIVERY_MMS          Not Started  is_test=True  n=5
   DELIVERY_SOLAR_PANELS Done         is_test=True  n=1
   DELIVERY_SOLAR_PANELS Not Started  is_test=True  n=5
   DESIGN                Done         is_test=True  n=5
   DESIGN                In Progress  is_test=True  n=1
   HOTO                  Not Started  is_test=True  n=6
Mirror tasks with assignee: 1  with due_date: 0  N/A: 0
Mirror tasks with NULL template_task: 0
Non-mirror tasks pointing at mirror template rows: 0
OPEX projects: 110  non-deleted: 109
OPEX projects with any tasks: 6
```

**Confirmed: COD, As-Built and HOTO are Not Started on all six activated OPEX sites.** All six are `is_test=True`; no real OPEX site is activated locally.

The ledger for mirror tasks (StatusTransition is generic: `subject_type='task'`, `subject_id`):

```
('task', 'DELIVERY_INVERTERS',    'Not Started', 'Done',        'mirror_derived', actor None, 'system') 1
('task', 'DELIVERY_MMS',          'Not Started', 'Done',        'mirror_derived', actor None, 'system') 1
('task', 'DELIVERY_SOLAR_PANELS', 'Not Started', 'Done',        'mirror_derived', actor None, 'system') 1
('task', 'DESIGN',                'Not Started', 'Done',        'mirror_derived', actor set,  'Design') 5
('task', 'DESIGN',                'Not Started', 'In Progress', 'mirror_derived', actor set,  'Design') 1
```

Per site:

```
SCMPILOT06 Not Started -> In Progress demo.designhead 2026-09-07
SCMPILOT01 Not Started -> Done demo.designhead 2026-09-07
SCMPILOT02 Not Started -> Done demo.designhead 2026-09-07
SCMPILOT03 Not Started -> Done demo.designhead 2026-09-07
SCMPILOT04 Not Started -> Done demo.designhead 2026-09-07
SCMPILOT05 Not Started -> Done demo.designhead 2026-09-07
```

These six rows are one-step jumps carrying `demo.designhead`. That fits neither the stepwise hook nor the actor-less reconcile; it is `fix_fixtures.py`'s `sync_design_mirror(p, da.status, actor)`. No COD, As-Built or HOTO row has ever been written.

---

## Part 3 — what the closeout sources could hang off

### 3a. PunchPoint

Model `models.PunchPoint`:

```
PunchPoint ['id', 'task:ForeignKey', 'reason:TextField', 'raised_by:ForeignKey', 'created_at:DateTimeField',
            'status:CharField[Open,Waived]', 'waived_by:ForeignKey', 'waived_at:DateTimeField', 'waiver_reason:TextField']
   constraints: []
```

**"Blocking" does not exist, as a field or as a rule.** The model has no severity or blocking column, and no `blocking` appears near PunchPoint in `models.py`.

**Statuses: two only. Nothing closes a point except a waiver.** From the class docstring:

> "There is no "resolved" state and that is deliberate: a punch point is not closed by someone declaring the work fixed, it is closed by the work being re-submitted and approved, which is a fact about the TASK. The only thing a person may do to a punch point directly is WAIVE it".

The code paths that touch the table (grep, non-test):

```
views.py  task_reject           PunchPoint.objects.create(task=task, reason=remarks, raised_by=profile)
views.py  punch_point_waive     PunchPoint.objects.filter(pk=punch_point.pk).update(
views.py  task_detail           PunchPoint.objects.filter(task=task)
management/commands/seed_walkthrough.py (fixture checks only)
```

**Finding.** `task_approve` never touches PunchPoint. A point raised on rejection stays `status='Open'` after the task is re-submitted and approved. "Open" in the column therefore does not mean "outstanding". Outstanding-ness can only be derived from the task's later approval, and no helper derives it.

**Waiver path.** `views.punch_point_waive`, authority `permissions.user_can_waive_punch_point`. Per the 2.3a decision (B-12) that is `user_can_manage_project`: the PM and coordinators. It is deliberately narrower than `user_can_approve_task`, which admits `is_qaqc`. The waiver writes `status=WAIVED`, `waived_by`, `waived_at` and `waiver_reason` on one row and does not touch the task.

**"Open blocking punch points for a project": nothing queries it.** The only read is per-task (`task_detail`: `PunchPoint.objects.filter(task=task)`). There is no project-level aggregate, and no reader filters on `task__phase__project`. Local DB: `PunchPoint rows: 0`.

### 3b. Project status

Values: `Project.STATUS_CHOICES = [('Draft', 'Draft'), ('Active', 'Active'), ('In Progress', 'In Progress'), ('Commissioned', 'Commissioned'), ('On Hold', 'On Hold'), ('Cancelled', 'Cancelled')]`. In use locally: `[{'status': 'Draft', 'n': 109}, {'status': 'Active', 'n': 46}]`.

Writers (grep of `project.status =`, Project `.update(status=`, and `commissioned_at =`, non-test):

```
views.py  project_create        project.status = 'Draft'
views.py  project_activate      project.status = 'Active'
views.py  opex_site_activate    project.status = 'Active'
management/commands/seed_opex_test_data.py  'Draft' / 'Active'
```

`ProjectAdmin` has `readonly_fields = ['project_id', 'status', 'created_at', 'activated_at', 'deleted_at']`. **Nothing can set Commissioned, In Progress, On Hold or Cancelled, and nothing writes `commissioned_at`. Defect B-3 still stands:**

> `docs/RESIDENTIAL_BASELINE.md`: "B-3 | app-wide | No code path sets `Project.status` to `'Commissioned'`, `'In Progress'`, `'On Hold'` or `'Cancelled'`, and nothing ever writes `commissioned_at`. **There is no project-closure workflow**"

**No commissioning hook fires.**

- Residential `Plant Commissioning` is a plain `Task.SITE_ENGINEER` Internal task in `build_residential_phases`, with no flag.
- `is_payment_milestone=True` sits on `Advance Payment Confirmation` (M1), `Pre Dispatch Payment Confirmation` (M2) and `100% Payment Confirmation` (M3, Finance Closure phase). None is a commissioning task.
- The only consumer is `_apply_task_status_change`: `if new_status == Task.DONE and task.is_payment_milestone:`.
- OPEX has no payment-milestone tasks.
- OPEX's `TESTING_COMMISSIONING` is an ordinary entered SE task.

### 3c. The override pattern

**`is_override` appears nowhere** (0 hits across `projects/`, migrations included). **HOTO's override would be the first of its kind.** The nearest existing shapes:

| Pattern | Where | Shape |
|---|---|---|
| Acting-on-behalf flag + mandatory reason | `DCLineItem.grn_on_behalf` / `grn_on_behalf_reason` | CHECK `grn_on_behalf_requires_reason` |
| Recorded on behalf of an absent decider | `ApprovalStep.is_proxy` / `proxy_channel` / `proxy_evidence` | CHECKs `approval_step_proxy_evidence`, `approval_step_self_recorded_unless_proxy` |
| Mandatory reason on a refusal | `payment_request_refusal_needs_reason`, `payment_hold_needs_reason`, `cr_rejection_reason_required_when_rejected`, `approval_step_carry_reason_iff_carried` | CHECK pairs |
| Early start past a dependency, mandatory reason | execution-model B-08 decision; 1.4b enforcement | **not built** |

The approval primitive (`approvals.py`) has `ApprovalRequest.kind` ∈ `material_pre_order`, `material_pre_dispatch`, `contractor_bill`. No closeout kind exists, and `ApprovalStep.party` ∈ `pm`, `design`, `site_engineer`.

---

## Part 4 — checklist readiness for QMS-01 and an HSE checklist

### 4a. The 272 seeded items: source documents per task code; overlap with QMS-01

From a local DB query:

```
10 OPEX-INST-CIVIL-MMS     Civil Work and MMS Installation v1 active photo=False items=46  -> CIVIL_WORK_AND_MMS_INSTALLATION
11 OPEX-INST-MODULE        Module Installation v1 active photo=False items=29              -> MODULE_INSTALLATION
12 OPEX-INST-LA-EARTHING   LA and Earthing Installation v1 active photo=False items=54     -> LA_AND_EARTHING_INSTALLATION
13 OPEX-INST-DC-CABLE      DC Cable Laying with Conduit v1 active photo=False items=40     -> DC_CABLE_LAYING_WITH_CONDUIT
14 OPEX-INST-DCDB-ACDB     DCDB and ACDB Installation v1 active photo=False items=58       -> DCDB_AND_ACDB_INSTALLATION
15 OPEX-INST-INVERTER      Inverter Installation v1 active photo=False items=34            -> INVERTER_INSTALLATION
16 OPEX-INST-AC-CABLE      AC Cable Laying v1 active photo=False items=11                  -> AC_CABLE_LAYING
 7 DEMOCHECKLIST           DemoChecklist v1 draft items=0                                  -> SLD (Residential)
active item total 272
completions total 11
completions on mirror tasks 0
```

None is linked to a mirror. Sources, from `seed_opex_installation_checklists` (`'sources'` keys and `# Source:` comments). All are `I&C` documents, R00, under `docs/Installation check list_Final/`.

| task code | cited documents |
|---|---|
| `CIVIL_WORK_AND_MMS_INSTALLATION` | HRPPL/CIVIL/BLOCK R00 (`I&C Check list for Concrete block_R00_23-8-26.pdf`); HRPPL/ELE/STR R00 (`I&C Check list for Structure_R00_26-6-26.pdf`) |
| `MODULE_INSTALLATION` | HRPPL/ELE/I&C/MOD R00 (`I&C Checklist  for PV Modules_R00_26-6-26.pdf`) |
| `LA_AND_EARTHING_INSTALLATION` | HRPPL/ELE/I&C/EAR R00; HRPPL/ELE/I&C/ESE LA R00 |
| `DC_CABLE_LAYING_WITH_CONDUIT` | HRPPL/ELE/I&C/SCB-AJB R00; HRPPL/ELE/I&C/CABLE R00 (DC half) |
| `DCDB_AND_ACDB_INSTALLATION` | HRPPL/ELE/I&C/DCDB R00; HRPPL/ELE/I&C/LVP R00 |
| `INVERTER_INSTALLATION` | HRPPL/ELE/I&C/INV R00 |
| `AC_CABLE_LAYING` | HRPPL/ELE/I&C/CABLE R00 (AC half) |

Not seeded, per `NOT_SEEDED` in the command: `RMS_INSTALLATION` ("Scada-RMS document exists, not seeded") and `SOLAR_GENERATION_METER_INSTALLATION` ("no source document supplied"). `TESTING_COMMISSIONING`, `POST_INSTALLATION_APPROVALS` and every Closeout task have no checklist.

**QMS-01 is not in the database.** Literal probes of ten distinctive QMS-01 lines all returned 0 rows:

```
'single-line diagram' 0 | 'Grid synchronization' 0 | 'Earth resistance measured' 0 | 'Module serial numbers recorded' 0
'Final punch points closed' 0 | 'Project handed over' 0 | 'Roof waterproofing' 0 | 'Anti-islanding' 0
'String open-circuit voltage' 0 | 'available at site' 0
```

No `.py`, `.md` or `.html` file under `projects/` or `docs/` names QMS-01, "QA-QC Checklist" or its author, except `docs/CHECKLIST_CONTENT_MAPPING.md` "Task 6", which declares the master QA/QC checklist out of scope.

**Overlap by section.** These are keyword probes over the 272 active item labels. They show topical overlap, not identical wording; the phrasing differs throughout.

| QMS-01 section | Overlap with seeded checklists | Evidence |
|---|---|---|
| 1 Documentation & pre-installation | **None in kind.** The 72 keyword hits are inline "as per approved drawing" references inside installation checks, not "document available at site" checks. One item contains "available". | e.g. CIVIL-MMS "Location Marking as per approved MMS layout GA/drawing" |
| 2 Material receiving (modules / MMS / electrical) | **Minimal.** 4 hits, all make/rating/size "as per approved BOM". No receiving-inspection lines (serials, transit damage, storage). | INVERTER "Check Inverter type, Rating & Make as per … BOM" |
| 3 Roof & structural | **Effectively none** (2 incidental hits) | — |
| 4 Mounting structure installation | **Strong**: CIVIL-MMS (Concrete Block, Fixed / Tracker structure sections) | section names |
| 5 PV module installation | **Strong**: MODULE (29 items) | — |
| 6 DC cable installation | **Strong**: DC-CABLE ("DC Solar Cable" section) | — |
| 7 DC string & combiner box | **Strong**: DC-CABLE ("String Combiner Box / Array Junction Box"); DCDB-ACDB ("DCDB") | — |
| 8 Inverter installation | **Strong**: INVERTER | — |
| 9 ACDB / AC side | **Strong**: DCDB-ACDB ("LT Panel / ACDB"); AC-CABLE | — |
| 10 Earthing & bonding | **Strong**: LA-EARTHING ("Earth Pits", "Earthing Installation") | — |
| 11 Lightning protection | **Strong**: LA-EARTHING ("Lightning Arrester Installation", "ESE Type Lightning Arrestor") | — |
| 12 Cable tray / conduit | **Partial**: 13 hits, mostly DC-CABLE | AC-CABLE "Cable tray/conduit support/clamping to be checked" |
| 13 Cable termination | **Partial**: 38 hits across all seven (lugs, glands, crimp, torque) | AC-CABLE "Cable termination(with proper lugging) and tightening" |
| 14 Testing & commissioning | **Partial**: 20 hits for IR, continuity, polarity and Voc inside installation checklists. 0 hits for IV curve and thermography. The `TESTING_COMMISSIONING` task has no checklist. | AC-CABLE "Insulation resistance test with IR tester and recorded" |
| 15 Inverter commissioning | **None** (0 hits: grid sync, anti-islanding, self-test, MPPT, PF, "commission") | — |
| 16 Performance & functional | **None** (1 incidental hit: surge counter) | — |
| 17 Safety inspection | **Labels only** (13 hits, mostly "identified with labels"). No PPE, LOTO, fire or fall-protection items. | — |
| 18 Housekeeping & handover | **Fragmentary** (5 hits: enclosure cleaning; one "As-built documentation done … for record") | — |
| 19 Required QA/QC records (22 rows) | **None** | — |
| 20 Final acceptance | **None** | — |

### 4b. A checklist on a MIRROR task

- **Can a checklist be linked to one?** Yes. Neither the link nor the lookup has a mirror term:
  - `admin_checklist_link_add` resolves its target through `_resolve_checklist_link_target`, which ends `return TaskTemplateTask.objects.filter(pk=pk).first()`.
  - `_checklist_task_link_for` joins on `template_task__code` and `template_task__phase__template__project_type` only.
- **Can it be filled in while the mirror's status is human-unwritable?** Yes, until the derived status reaches Done. Answering is gated by `views._user_can_complete_checklist_item`:

  ```python
      if not checklist_answers_open(task):
          return False
      ...
      if user_can_manage_project(user, project):
          return True
      normalised_user_role = _PROFILE_TO_TASK_ROLE.get(profile.role, profile.role)
      return normalised_user_role == task.assigned_role
  ```

  and `permissions.checklist_answers_open`:

  ```python
      return task.status != task.DONE and not task.is_awaiting_approval
  ```

  Two consequences:
  1. The task's assigned **role** may answer, or the PM/coordinator; **no assignee is needed**. Rung 0 does not apply because the checklist is a separate write path.
  2. A mirror can never be "awaiting approval", since `_approval_preconditions` refuses mirrors. So answers stay open until the **source object** drives the mirror to Done. At that moment answers close, whoever was mid-checklist. For COD, As-Built and HOTO, which have no writer, answers would stay open indefinitely.
- **Nothing gates a mirror's status on its checklist.** No checklist gate exists anywhere (B-6 is still deferred). `apply_mirror_status` does not read checklists.

### 4c. A checklist without a task (project- or event-level, e.g. HSE at mobilisation)

**Not supported. Every answer needs a host Task.**

```
ChecklistItemCompletion [... 'item:ForeignKey', 'task:ForeignKey', ...]
   constraints: ['uniq_checklist_completion_item_task', 'checklist_completion_no_requires_remarks']
```

`task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name='checklist_completions')` is non-null. `ChecklistTaskLink` targets a `TaskTemplateTask` and nothing else. `Checklist` has no project FK.

So a once-per-site HSE clearance needs a host task (the OPEX template has no mobilisation task: phases are Design, Approvals pre, Procurement & Delivery, Installation, Testing & Commissioning, Approvals post, Closeout) or a new structure.

Also:

- `UserProfile.is_hse` exists, commented "May grant a site its HSE mobilisation clearance, without which execution may not start". It has **no consumer** (grep: model, seed command, tests only).
- The item content is B-11, still open (`docs/PHASE_1_OUTCOME.md`: "**B-11** | The actual HSE mobilisation checklist items — a named list, not a category … | **2.2**").

### 4d. Does the checklist model support QMS-01's shape?

| Need | Verdict | Evidence |
|---|---|---|
| Per-site header: client, contractor, inspection date, checklist number, drawing revision | **Absent** as a checklist header. Partly derivable from elsewhere: `Project` has `customer_name`, `customer_contact_person`, `site_code`, `site_name`; `Program.client_name`; contractor exists only as `Vendor` kind `contractor` (no per-site contractor field on `Project`); drawing revision exists as `DesignFile.version` / Arka version but nothing ties it to a checklist; inspection date ≈ `checked_at` per item, not per sheet; no checklist-number field. | `Checklist` fields: `code, name, version_no, status, effective_from, requires_photo, created_by, created_at` |
| Document register (22 rows, per-row status and file upload) | **Partly.** Each register row could be an item with `answer` ∈ yes/no/na and `remarks`. Each completion holds **one** photo, and `checklist_item_complete` uploads with `allowed_extensions=ALLOWED_PHOTO_EXTENSIONS`, i.e. `['jpg', 'jpeg', 'png']`. A PDF report cannot be attached, and there are no per-row statuses beyond yes/no/na. | `ChecklistItemCompletion` fields; `views.ALLOWED_PHOTO_EXTENSIONS` |
| Overall verdict with four outcomes | **Absent.** There is nowhere to store a sheet-level value: answers are per (item, task). | — |
| Multiple named signatories, including an external client rep | **Partly / absent.** `checked_by` is one authenticated user per item. `witness_names` is free text, "an assertion by the completer, not a signature" (`help_text='Names asserted by the completer. Not verified signatures.'`). No client role or login exists. The nearest structured pattern for recording an outside party is `ApprovalStep.is_proxy` + `proxy_channel` + `proxy_evidence`. | `UserProfile.ROLE_CHOICES`: Admin, System Admin, PM, Project Coordinator, Site Engineer, Design, Finance, SCM, CEO, BD |

**A closer fit exists elsewhere.** QMS-01 §20's verdicts match existing *task* approval machinery more closely than checklist machinery:

| QMS-01 verdict | Existing machinery |
|---|---|
| Accepted | `task_approve` |
| Rejected | `task_reject` |
| Accepted with Punch Points | `task_reject` + `PunchPoint` (with waiver) |
| Re-inspection Required | **nothing** |

### 4e. Snapshot and versioning

**The phase-0 snapshot (prompt 0.5, R-8) still holds for text.**

- `ChecklistItemCompletion.item_text_snapshot` is written at answer time.
- `_checklist_context` renders the snapshot for checked rows.
- `item` is `SET_NULL`, so deleting an item keeps the answer.
- Versions are immutable once active (R-7: `ChecklistItem` save/delete guards, `uniq_active_checklist_per_code`).
- Pinned by `tests_checklist_snapshot`: `HistoryIsNoLongerRewritableTests`, `ChecklistImmutabilityTests`, `ChecklistVersioningTests`.

**It does not hold for version: a completed site would visibly lose its answers when R01 activates.** This is a finding. The resolver always serves the *active* version of the family:

```python
def _checklist_for_task(task, project):
    ...
    if link.checklist.status == Checklist.ACTIVE:
        return link.checklist
    return (Checklist.objects
            .filter(code=link.checklist.code, status=Checklist.ACTIVE)
            .first())
```

and `_checklist_context` fetches completions only for that version's items:

```python
    checklist = _checklist_for_task(task, project)
    items = list(checklist.items.all()) if checklist else []
    ...
            for c in ChecklistItemCompletion.objects.filter(task=task, item__in=items)
```

R01 is a new `Checklist` row whose items are new `ChecklistItem` rows. This is the shape `tests_checklist_snapshot.ChecklistResolutionTests.test_publishing_version_two_moves_the_task_onto_it` builds: `Checklist.objects.create(..., version_no=2)` then `ChecklistItem.objects.create(checklist=draft, …)`.

So once R01 is active, a task answered under R00:

- renders R01's items with **no** answers;
- has its R00 completions excluded by `item__in`. They still exist in the table; they are not rewritten, only no longer shown;
- if it is Done, cannot be re-answered, because `checklist_answers_open` is False.

That test asserts only that the task moves to v2. No test covers a completed task across a version change.

Local evidence of exposure: SCMPILOT01's Done Civil Work task holds 11 completions on `OPEX-INST-CIVIL-MMS` v1:

```
{'item__checklist__code': 'OPEX-INST-CIVIL-MMS', 'item__checklist__version_no': 1, 'task__status': 'Done', 'task__phase__project__project_id': 'SCMPILOT01', 'answer': 'no', 'n': 1}
{'item__checklist__code': 'OPEX-INST-CIVIL-MMS', 'item__checklist__version_no': 1, 'task__status': 'Done', 'task__phase__project__project_id': 'SCMPILOT01', 'answer': 'yes', 'n': 10}
```

This was **not** demonstrated by activating a v2 locally. Even a rolled-back transaction advances Postgres sequences, which this session may not change. The conclusion rests on the quoted code.

There is also no "publish next version" writer. The portal admin only refuses edits to a live version with "Editing a live checklist means publishing version N+1", and has no view that creates N+1 (`admin_checklist_*` views: list, create, edit, update, delete, item add/edit/delete/move, link add/delete). A revision today needs the shell or the Django admin.

---

## Part 5 — questions, not answers

Each question lists options and what the current code makes cheap or costly. No option is chosen here.

**Closeout sources**

1. **What is COD's source object?**
   - (a) A new commissioning/COD record, as the spec's "phase 5.3" has it. This is a new model plus a writer through `apply_mirror_status`; cheap to wire because the door and the `mirror_derived` reason exist.
   - (b) The QMS-01 §20 verdict. Costly: the checklist model cannot hold a sheet-level verdict (4d), so it needs a host or a new model first.
   - (c) A `Project.status → Commissioned` transition. Costly: there is no writer of `Project.status` past Active, and nothing writes `commissioned_at` (B-3).
   - (d) The approval of an entered task such as `TESTING_COMMISSIONING`. Cheap to read, but it makes COD a function of a human task, not a record.
2. **Is QMS-01's Final Acceptance the source for HOTO, for COD, for both, or for neither?** If both, one verdict drives two mirrors. If COD, does "Accepted with Punch Points" permit COD while those points are open?
3. **What does "Re-inspection Required" do?**
   - Is it a fourth task outcome (no machinery exists), a reject without a punch point, or a state on a new record?
   - `docs/CHECKLIST_CONTENT_MAPPING.md` Task 6 lists only three outcomes.
4. **Is "blocking" a field or a rule?**
   - (a) A `PunchPoint` column set at `task_reject`. This needs a migration and a decision about who sets it: the rejecting approver, often `is_qaqc`.
   - (b) Every open point blocks. Zero schema.
   - (c) Severity on the point.
5. **What makes a punch point stop counting?** Today only a waiver; re-approval leaves it `Open` (3a). Options:
   - (a) `task_approve` closes the task's open points. This is a new write on the approval path.
   - (b) Readers treat "open on a task that has since been approved" as cleared. This is a derived rule every reader must repeat.
   - (c) A third status.
6. **What scope does COD's punch-point gate read?** Every task on the site (`task__phase__project`), including N/A tasks and mirror tasks, or installation and testing tasks only?
7. **Should COD and HOTO be reopen-capable?** Spec rule 3 says mirrors follow their source both ways. A punch point raised after COD, or as-builts withdrawn after HOTO, would move them back out of Done. `apply_mirror_status` already supports this.

**As-Built**

8. **Where do as-builts come from?**
   - (a) A new `DesignFile` kind. Costly as it stands: `DesignFile.attempt` is required and `derived_from_arka` is NOT NULL / PROTECT, so a post-commissioning upload would need a fresh attempt and an Arka, or a schema change.
   - (b) A separate as-built model.
   - (c) QMS-01 §18 "As-built drawings submitted" or §19 row 19 "As-Built Drawings". Costly: per-item checklist answers carry images only (jpg/png), not drawings.
   - (d) A `ProjectDocument` upload. Untyped, so not distinguishable.
9. **Should As-Built stay a mirror at all?** `execution-model.md` §9 describes it as human-submitted: "Designer uploads; the task stays open in their bucket until submitted; submission is blocked until execution is complete". That reads as an *entered* task with a gate, which rung 0 forbids on a mirror. Converting it means a new OPEX template version. Keeping it means the designer works in a separate object that derives the mirror.
10. **What does "execution is complete" mean for the As-Built submission block?** Installation-phase tasks Done, COD Done, or QMS-01 accepted?

**HOTO override**

11. **Where does the override live?**
    - (a) A new `ApprovalRequest.kind` in the approvals primitive. Its step parties are pm/design/site_engineer, and it is built for SCM-raised rounds.
    - (b) A flag-and-reason pair with a CHECK on the HOTO record, mirroring `grn_on_behalf` / `grn_on_behalf_requires_reason`. Cheapest.
    - (c) A `StatusTransition` remark with a dedicated reason code. The ledger is append-only, but a remark is optional by schema.

    Nothing named `is_override` exists anywhere.
12. **Who may override HOTO?** The PM alone, or anyone passing `user_can_manage_project`, which also admits coordinators (the punch-point waiver uses that predicate)?

**Mirrors and people**

13. **Should a mirror be assignable and dateable?** Today it is both (1d). Assigned mirrors appear in the morning report, dated ones in the CEO At Risk badge (G33), and an assigned mirror shows its assignee a status select that always refuses (1b). Options:
    - (a) Refuse in `task_assign`, `task_assign_design_head` and `task_set_due_date`.
    - (b) Hide the controls only.
    - (c) Accept and document. `TheTrapTests` currently pins "permitted and pointless".
14. **Should the task-detail page carry the Derived chip and suppress the status select for mirrors**, as the overview row does?
15. **Should mirrors host checklists?** It is allowed today (4b), and answers close the moment the source drives the mirror to Done, which the person answering does not control.

**QMS-01**

16. **Where does QMS-01 live?**
    - (a) A checklist on `POST_INSTALLATION_APPROVALS`, which `CHECKLIST_CONTENT_MAPPING.md` names as the eventual home.
    - (b) A checklist on `TESTING_COMMISSIONING`.
    - (c) A new Closeout task, which needs a template version.
    - (d) A new site-level model.

    (a)–(c) still lack a header, a verdict, signatories and PDF uploads (4d).
17. **How should QMS-01 relate to the seven installation checklists?** Sections 4–13 overlap them heavily (4a). Options: answer both, trim QMS-01 to the non-overlapping sections (1–3, 14–20), or have QMS-01 sections reference installation-checklist completion.
18. **Is the §19 records register a list of uploads with statuses, or tick-boxes?** Uploads need non-image files on completions, which today accept jpg/jpeg/png only.
19. **Who may sign as Client Representative, and how is it recorded?**
    - (a) A free-text name in `witness_names`, labelled as an assertion.
    - (b) A proxy-recorded decision like `ApprovalStep.is_proxy` with channel and evidence.
    - (c) An external login. Vendor and other portal logins are on the out-of-scope list in `execution-model.md`.
20. **Who signs as "QA/QC Engineer" and "Construction Engineer"?** `is_qaqc` holders and the Site Engineer? "Construction Engineer" matches no role name.

**Checklist revisions**

21. **When R01 of any checklist activates, what should a task answered under R00 show?** Today it shows R01 blank (4e). Options:
    - (a) Pin the version per task on first answer. This needs somewhere to store it.
    - (b) Render a task's existing completions by snapshot regardless of version.
    - (c) Serve the new version only to tasks with no answers.
22. **Who publishes a new checklist version?** No portal screen creates version N+1 today.

**HSE**

23. **Where does the once-per-site HSE clearance live?** A host task (a new template version; activated sites keep v1 tasks) or a project-level record (a new model)? The item list (B-11) is still undecided, and `is_hse` has no consumer.

**Housekeeping surfaced by the audit**

24. **Should `fix_fixtures.py` stay tracked?** It is a third `sync_design_mirror` caller that the caller-discipline test cannot see.
25. **Should `derivation_source` still be added beside `is_mirror`?** It was promised for phases 3–5. The live writers identify rows by `template_task__code` instead.
26. **Should delivery mirrors reconcile at activation, as Design does?** Not closeout, but surfaced in 2b.

---

## What could not be determined

- **Production state.** Every count here is local. All six activated OPEX sites are `is_test=True`, so assigned or dated mirrors in production, the real distribution of mirror statuses, and whether any real punch points exist are unknown from this session.
- **The R01 version effect (4e) was not executed.** It is reasoned from quoted code; no v2 was activated, even in a rolled-back transaction.
- **Can `PaymentMilestone` rows exist on an OPEX project in production?** The name filter on writers D and E is the only thing keeping them off mirrors. `tests_opex_template_correction.PaymentMilestoneCardTests.test_the_create_endpoint_refuses_an_opex_project` says the UI cannot create one; shell or admin creation was not checked.
- **Whether QMS-01 R00 is final**, who owns its revisions, and whether it supersedes the eleven I&C documents or sits beside them. This is a content question.
- **Which role QMS-01's "Construction Engineer" maps to.**
- **Whether a per-site contractor can be derived** from contractor-bill or order records reliably enough to fill a header. Not traced.
- **The full `_resolve_checklist_link_target` validation** before its final `TaskTemplateTask.objects.filter(pk=pk).first()`, e.g. whether it restricts to active templates. Only its return statements were read. No mirror term appears in it.
- **Rows marked † in 1c** were re-read directly. The other writer rows and the 1d counter inventory come from a read-only sweep that quoted code verbatim; spot checks of `at_risk_subq`, `_attach_due_health`, `task_report`, `current_phase`, the milestone sync and `task_set_due_date` matched.
