# Residential Lifecycle — Baseline

**Prompt 0.2a. Tests-and-documentation session. No `.py`, `.html`, `.css` or `.js` file was
modified. No migration was created.**

This is the end-to-end Residential lifecycle **as the code implements it today**, read from
source at commit `bb0f905` with the working tree as delivered. Every claim below is a claim
about current behaviour, not about intended behaviour. Where the two disagree, the disagreement
is called out.

It is the specification that `projects/tests_residential_baseline.py` encodes, and it is the
regression net prompt 0.2 must not tear. Read it with `ACCESS_ISOLATION_AUDIT.md` beside it:
that document lists fifteen findings which are also current behaviour, and **none of them are
pinned here or in the test file.**

---

## 0. Cast, and the one account without which nothing works

Ten roles in `UserProfile.ROLE_CHOICES` ([models.py:508](projects/models.py#L508)). The
Residential lifecycle uses seven of them, plus two flags it never touches.

| Actor | Role string | How they acquire the project |
|---|---|---|
| PM | `'PM'` | `Project.assigned_pm` — set at creation to the creating user |
| Project Coordinator | `'Project Coordinator'` | `Project.coordinators` M2M — PM-equivalent authority ✔ |
| Site Engineer | `'Site Engineer'` | Holds a `Task` — **no FK exists**, `assigned_site_engineer` was removed in migration 0037 |
| Design | `'Design'` | `Project.assigned_design` — set at **activation**, not at creation |
| Finance | `'Finance'` | Six named tasks, back-assigned at activation to one hardcoded email |
| SCM | `'SCM'` | **Nothing.** All 11 SCM tasks are created `assigned_to = NULL` and nothing ever assigns them |
| BD | `'BD'` | **Nothing.** The single BD task is created `assigned_to = NULL` |

### RESIDENTIAL_FINANCE_ASSIGNEE_EMAIL is required data, not configuration

`utils.py:679` hardcodes `'santosh@horizonrenewablepower.com'`. At activation,
`attach_residential_template()` resolves it and **raises `UserProfile.DoesNotExist` if the
account is absent** ([utils.py:895-901](projects/utils.py#L895-L901)). The raise is deliberate
and documented — an explicit `raise` rather than an `assert`, precisely so `python -O` cannot
strip it and silently re-enable create-unassigned behaviour.

The raise happens **inside `transaction.atomic()`**, and `project_activate` wraps the whole
thing in a second `atomic()` ([views.py:2602](projects/views.py#L2602)). So a missing Finance
account does not half-activate a project — it rolls back the status change, the
`assigned_design` stamp, all 9 phases, all 52 tasks and all 3 milestones. The project stays
`Draft`.

**Consequence for every test in this file:** no Residential project can be activated in a test
database that lacks that account. This is the first fixture line, not an afterthought.

---

## 1. Project creation and activation

### 1.1 Creation

| | |
|---|---|
| **Who** | PM only — `@role_required(['PM'])` ([views.py:2389](projects/views.py#L2389)) |
| **View** | `project_create` → `ProjectCreateForm` |
| **Writes** | One `Project` row. `assigned_pm = request.user.profile`, `status = 'Draft'`, `created_by = request.user`. `project_id` generated in `Project.save()` via `generate_project_id()` under `select_for_update()` |
| **Notifies** | Nothing |
| **Next** | The project sits in `draft_projects` on the PM dashboard until activated |

A second creation path exists: `zoho_deal_closed_webhook`
([views.py:7108](projects/views.py#L7108)) creates a `Project` with `status='Draft'` from a
Zoho CRM payload. It does **not** activate; the PM still must.

### 1.2 Activation — the one step that seeds everything

| | |
|---|---|
| **Who** | `@role_required(['PM', 'Project Coordinator'])` **and** `_pm_owns_project()` → 404 otherwise ([views.py:2588](projects/views.py#L2588)). Genuinely scoped |
| **View** | `project_activate`, POST only |
| **Preconditions** | `status == 'Draft'` (else a warning redirect), and a `assigned_design_id` naming an **active `Design`-role** `UserProfile` (else an error redirect) |

Inside one `transaction.atomic()`:

1. `project.assigned_design = designer`, `status = 'Active'`, `activated_at = now()`
2. If `project_type == 'Residential'` → `attach_residential_template(project)`
3. Three `PaymentMilestone` rows — `M1 'On Survey Completion'`, `M2 'On Material Supply'`,
   `M3 'On Commissioning'`, each `status='Pending'`, `amount=NULL`

Then, **after** the transaction commits, one `ActivityLog` row. No notification is sent.

### 1.3 Exactly what `attach_residential_template()` seeds

[utils.py:830-919](projects/utils.py#L830-L919), building from
`build_residential_phases()` [utils.py:697](projects/utils.py#L697).

**9 phases, 52 tasks.** 44 `Internal`, 8 `External`. All three counts are asserted **inside**
the atomic block ([utils.py:908-919](projects/utils.py#L908-L919)), so a template edit that
changes any of them rolls activation back rather than shipping a wrong project.

| # | Phase | Tasks | Roles |
|---:|---|---:|---|
| 1 | Sales & Documentation | 3 | BD ×1, Finance ×2 |
| 2 | Detail Engineering Visit | 4 | PM ×1, Site Engineer ×2, Design ×1 |
| 3 | Design | 7 | Design ×5, PM ×2 |
| 4 | Pre-Installation Approvals | 7 | PM ×6, SCM ×1 |
| 5 | Procurement | 7 | SCM ×5, Finance ×2 |
| 6 | Delivery | 5 | SCM ×5 |
| 7 | Installation | 8 | Site Engineer ×8 |
| 8 | Commissioning | 10 | PM ×5, Site Engineer ×4, Finance ×1 |
| 9 | Finance Closure | 1 | Finance ×1 |

Role totals: **PM 14, Site Engineer 14, Design 6, SCM 11, Finance 6, BD / Sales 1 = 52.**
(`Task.assigned_role` stores `'BD / Sales'`; `UserProfile.role` stores `'BD'`. They are
different vocabularies — see §5.1.)

`duration_days` comes from `TaskDurationTemplate` rows for `project_type='residential'` where
they exist, falling back to `RESIDENTIAL_DURATION_DEFAULTS`, then to `1`
([utils.py:661](projects/utils.py#L661)). Note the case: the template table stores
`'residential'` lowercase; `Project.project_type` stores `'Residential'`. They are different
vocabularies and are never compared to each other.

**Assignment at activation — two back-assignments and nothing else:**

```python
assign_tasks_to(Task.objects.filter(phase__project=project, assigned_role=Task.PM), pm_profile)
assign_tasks_to(
    Task.objects.filter(phase__project=project,
        task_name__in=INVOICE_TASK_NAMES + RESIDENTIAL_FINANCE_CONFIRMATION_TASK_NAMES),
    finance_assignee)
```

- **14 PM tasks** → `project.assigned_pm`
- **6 Finance tasks** → the hardcoded Finance assignee:
  `Send Invoice - Advance Payment`, `Advance Payment Confirmation`,
  `Send Invoice - Material Supply`, `Pre Dispatch Payment Confirmation`,
  `Send Invoice - Final Payment`, `100% Payment Confirmation`
- **14 Site Engineer, 6 Design, 11 SCM and 1 BD task are left `assigned_to = NULL`.** The
  docstring states this outright ([utils.py:875](projects/utils.py#L875)).

`assign_tasks_to()` is a bare `queryset.update()` and is **silent by design**
([utils.py:181](projects/utils.py#L181)) — no notification, no `ActivityLog`. A newly
activated project sends nobody anything.

**Three tasks carry `is_payment_milestone = True`** — the three Finance confirmation tasks, not
the commissioning task. Marking any of them `Done` fires the payment-milestone notification
(§6.4).

**Due dates are all `NULL` at activation.** Nothing is scheduled until the PM sets a date
(§5.2) or runs `project_recalculate_dates`.

> ### ⚠ The success message is wrong
> [views.py:2631](projects/views.py#L2631) says *"Project activated. **53 tasks** created."*
> The code creates 52 and asserts 52. The message has been wrong since the template last
> changed. It is a string, not a behaviour, so no test asserts it — but **assert against 52,
> never against the message.**

### 1.4 What activation does NOT do

- No `BOQ` row. The BOQ is created lazily on first Design page load (§3.1).
- No `DesignAssignment` row — that model is OPEX-only (`_opex_site()` refuses Residential).
- No Site Engineer is attached to the project in any way. **A freshly activated Residential
  project is invisible to every Site Engineer in the company** until a PM opens `task_assign`
  and hands one a task by name. Nothing in the product prompts the PM to do so.
- Same for SCM and BD: zero task relationship, ever, unless assigned by hand.

---

## 2. Design allocation through to design release

**On Residential, there is no design workflow.** This is the single largest gap between the
module documentation and the code.

`docs/execution-model.md` §5 describes `DesignAssignment`, `DesignAttempt`, `ArkaSubmission`,
`DesignFile`, `DesignChangeRequest`, allocation, QC gates, `DESIGN_RELEASED` and its single
exit. **Every one of those endpoints resolves its site through `_opex_site()`**
([design_views.py:218](projects/design_views.py#L218)):

```python
project = get_object_or_404(Project, project_id=project_id, is_deleted=False)
if project.project_type != 'OPEX':
    raise Http404('Design workflow applies to OPEX sites only.')
```

Residential and CAPEX are unreachable through the entire design module. There is no
allocation, no due-date handshake, no attempt numbering, no QC pass/fail, no design release,
and no `DESIGN_RELEASED` state on a Residential project.

**What Residential design actually is:** six template tasks with `assigned_role='Design'`
(`DEV Inputs Validation`, `Design`, `Array Layout`, `SLD`, `Installation Drawings`,
`BOQ Preparation`), created unassigned, moved through `Not Started → In Progress → Done` by
whoever holds the `Design` role, plus the BOQ authoring path in §3. The `assigned_design` FK
stamped at activation is what puts the project on that designer's dashboard
([views.py:926](projects/views.py#L926)) and what grants BOQ **write** authority
([permissions.py:326](projects/permissions.py#L326)).

`Design Approval by Internal Team` and `Design Approval by Customer` (phase 3, tasks 6 and 7)
are **PM**-role tasks with no gate of any kind behind them — no artifact, no per-file tick, no
package. They are two status dropdowns.

---

## 3. BOQ authoring, submission, acknowledgement, revision

### 3.1 Creation — lazily, on a GET, by the person who may write it

`boq_detail` ([views.py:4581](projects/views.py#L4581)) is the Residential BOQ screen for every
role.

- Read gate: `user_can_view_project_boq()` — the assigned designer, a designer holding a task
  on the project, SCM/Admin/CEO portfolio-wide, Design Head, Design QC, the named QC reviewer,
  or PM-level management authority. **Finance and BD are deliberately excluded**
  ([permissions.py:157](projects/permissions.py#L157)).
- If `project.boq` does not exist, the view **creates it on the GET** — but only for a user who
  passes `user_can_edit_project_boq()`, i.e. `role == 'Design' AND project.assigned_design_id ==
  profile.pk`. A reader cannot bring a BOQ into existence by loading the page.
- Seeding copies every active `BOQItemMaster` row with `project_type='Residential'` (37 rows in
  production) into `BOQItem`, with `serial_no` from the catalogue's `sort_order` and
  `item_master` carrying the stable join key. `get_standard_boq_items()` **raises
  `RuntimeError` on an empty catalogue** rather than falling back to a literal list.

### 3.2 The status ladder

`BOQ.STATUS_CHOICES`: `Draft → Submitted → Acknowledged`, with `Revision Requested` as the
loop-back. `BOQ.version` increments only on resubmission after a revision request.

| Step | Actor | Endpoint | Gate | Precondition | Writes |
|---|---|---|---|---|---|
| Author | assigned designer | `boq_detail` POST `save_design` / `add_item` / `delete_item` | `user_can_edit_project_boq()` AND not group-locked AND not design-locked | status in `Draft \| Revision Requested \| Acknowledged` | `BOQItem.boq_quantity`, `make_preference` |
| Submit | assigned designer | `boq_submit` ([:5920](projects/views.py#L5920)) | same three terms | status in `Draft \| Revision Requested`; **≥1 item with `boq_quantity > 0`** | `BOQRevision` snapshot; `status='Submitted'`, `submitted_by`, `submitted_at` |
| Acknowledge | **any** SCM user | `boq_acknowledge` ([:5993](projects/views.py#L5993)) | `profile.role != 'SCM'` → 403. **No project term, deliberately** | status `== 'Submitted'` | `status='Acknowledged'` |
| Request revision | **PM or Coordinator on this project** | `boq_request_revision` ([:6027](projects/views.py#L6027)) | `user_can_manage_project()` → 403 | status in `Submitted \| Acknowledged`; non-empty reason | `BOQRevision` snapshot; `status='Revision Requested'` |

> ### ⚠ The standalone `boq_submit` endpoint raises on every submission
> There are **two** implementations of "submit this BOQ", and they do not agree.
>
> The button in `boq_detail.html:293` posts `action=submit_design` back to `boq_detail`,
> which snapshots via `_boq_snapshot()` ([views.py:4444](projects/views.py#L4444)) — and that
> helper ends by coercing every `Decimal` to `float`, because `BOQRevision.snapshot` is a plain
> `JSONField` with no custom encoder and `json.dumps()` cannot serialise a `Decimal`.
>
> `boq_submit` ([views.py:5970-5978](projects/views.py#L5970)) builds its own snapshot inline
> from a raw `.values(...)` and **does not coerce**:
>
> ```python
> snapshot = list(boq.items.values(
>     'serial_no', 'category', 'description', 'uom',
>     'boq_quantity', 'ordered_quantity',
>     'make_preference__name', 'ordered_vendor__name',
> ))
> ```
>
> Submission requires at least one item with `boq_quantity > 0`, so **every** call that gets
> past the validation reaches `BOQRevision.objects.create()` with a `Decimal` in the payload and
> raises `TypeError: Object of type Decimal is not JSON serializable`. There is no `try/except`
> around it, unlike the inline branch, so it is an unhandled 500.
>
> **Not a SQLite artifact.** PostgreSQL's `adapt_json_value()` calls
> `Jsonb(value, dumps=get_json_dumps(encoder))`, and with `encoder=None` that is plain
> `json.dumps`. The endpoint fails identically on Railway.
>
> It has gone unnoticed because the UI never calls it — `boq_submit` is reachable only by typing
> the URL or by a client that posts to it directly.
>
> **The two paths also disagree on state.** `boq_submit` accepts `Draft | Revision Requested`;
> the inline branch accepts `Draft | Revision Requested | Acknowledged` and treats
> `Revision Requested | Acknowledged` as a revision. So an `Acknowledged` BOQ is resubmittable
> from the page and refused by the endpoint.
>
> Recorded as B-7. Not fixed. The test that would have pinned the endpoint is `@skip`ped in
> `tests_residential_baseline.py`; the tests for the working path drive `submit_design`.

Every transition writes one `ActivityLog` row. **No BOQ transition sends a notification** —
`_notify_boq_acknowledged()` exists at [views.py:4483](projects/views.py#L4483) but is called
from the **inline** `acknowledge_scm` branch of `boq_detail`, not from the standalone
`boq_acknowledge` endpoint. The two acknowledge paths are not equivalent.

> **Correction to the prompt.** Prompt 0.2a asks for a test in which *"SCM requests a
> revision"*. **SCM cannot.** `boq_request_revision` routes through `user_can_manage_project()`
> and returns 403 for SCM on every project. The revision requester is the PM or a Project
> Coordinator, and the docstring records that the older *"PM only"* wording was itself wrong
> because the role tuple always included coordinators. The test file asserts the PM path and
> separately asserts SCM's 403.

`boq_history` ([:6085](projects/views.py#L6085)) is gated on the same read helper as
`boq_detail` — "anyone who may read the BOQ may read how it got there, and nobody else."

### 3.3 The two locks that never fire on Residential

`project_boq_is_group_locked()` requires membership of a locked `SiteGroup`; `SiteGroup.program`
is non-nullable and Residential can never have a Program, so it is **structurally False on
every Residential project**. `project_boq_is_design_locked()` reads the OPEX design workflow
and is likewise always False. Both are still evaluated on the Residential path; both always
return `False`. Do not remove them — they exist so a hand-crafted POST to a Residential
endpoint cannot become an OPEX bypass.

---

## 4. Delivery challan and GRN

### 4.1 Create

| | |
|---|---|
| **Who** | `@role_required(['SCM'])` ([views.py:8655](projects/views.py#L8655)). Portfolio-wide by remit — SCM raises the DC that *creates* the delivery relationship, so there is no prior relationship to scope on |
| **View** | `create_delivery_challan`, GET renders the form, POST creates |
| **Requires** | `dc_number`, `dc_date` (ISO), **≥1 parseable line item**. A line item counts only if `category`, `description` and a non-zero `qty` are all present |
| **Writes** | One `DeliveryChallan` (`status = 'Expected'`, `created_by = SCM profile`) plus N `DCLineItem` rows, in one `atomic()`. Then one `ActivityLog` |
| **Notifies** | Nothing |
| **Next** | Redirects to `delivery_challan_detail`. `recalculate_dc_status()` is deliberately **not** called — new items have no `received_quantity`, so status stays `Expected` |

### 4.2 GRN confirmation

| | |
|---|---|
| **Who** | `@role_required(['Site Engineer'])` and nothing else — see the note below |
| **View** | `confirm_grn` ([views.py:8815](projects/views.py#L8815)), POST only |
| **Guards** | Cross-project: `challan.project.project_id != project_id` → 404. Status: an already-`Received` DC → 403 |
| **Per line item** | Reads `received_qty_<pk>`, `damaged_qty_<pk>`, `grn_notes_<pk>`. A blank or unparseable received quantity **skips that item silently**. `damaged_qty` is clamped to `[0, received_qty]` |
| **Writes** | `received_quantity`, `damaged_quantity`, derived `condition`, `grn_date = today`, `grn_confirmed_by = profile`, `grn_notes` |
| **Then** | `recalculate_dc_status(challan)` **once**, after the loop — never inside it |
| **Notifies** | Nothing |

`condition` is derived, not chosen: `damaged == 0 → Good`, `damaged >= received → Damaged`,
otherwise `Partial`. The comment is explicit that `damaged_quantity` is the source of truth and
`condition` is kept only for backward compatibility.

**Status rollup** ([models.py:1278-1332](projects/models.py#L1278)) — worst case across every
*confirmed* line item, unconfirmed items excluded:

| Per item | Condition |
|---|---|
| `red` | `received == 0`, **or** `received < ordered AND damaged > 0` |
| `green` | `received >= ordered AND damaged == 0` |
| `amber` | everything else — shortfall alone, or full quantity with some damage |

`green → Received`, `amber → Partially Received`, `red → **Rejected**`. Note the repurposing:
`Rejected` here means *severe delivery failure*, not *refused consignment*. No confirmed items
at all → `Expected`.

### 4.3 Override

`override_grn` ([:8894](projects/views.py#L8894)), `@role_required(['SCM'])`. Mirrors
`confirm_grn`'s parsing but **does not overwrite `grn_confirmed_by`** — the original SE is
preserved and the `ActivityLog` records who overrode. No status restriction: SCM can correct a
`Received` or `Rejected` DC.

> **Audit finding 1 lives here.** `confirm_grn` carries no project relationship test of any
> kind, so any Site Engineer can confirm a GRN on any challan in the portfolio. The test file
> asserts **the SE who holds a task on that project** succeeding, which is the legitimate
> behaviour 0.2 must preserve, and does **not** assert the unrelated-SE case.

---

## 5. Task progression, due dates, checklists

### 5.1 Status

`Task.STATUS_CHOICES`: `Not Started`, `In Progress`, `Done`, `Blocked`. The transition table
([views.py:3616](projects/views.py#L3616)) is enforced server-side:

| From | May go to |
|---|---|
| `Not Started` | In Progress, Blocked, Done |
| `In Progress` | Done, Blocked |
| `Blocked` | In Progress, Blocked |
| `Done` | **Blocked only** — "prevents gaming completion" |

Two write paths, with **different** permission models, both reachable from the same page:

| View | Gate |
|---|---|
| `task_status_update` ([:3564](projects/views.py#L3564)) — the task-row dropdown | `normalised_user_role == task.assigned_role` **OR** `user_can_manage_project()`. Plus a hard precondition: **`task.assigned_to` must not be NULL** (400 / inline error otherwise) |
| `task_detail_status_update` ([:3798](projects/views.py#L3798)) — the task detail page | `task.assigned_to == profile`. Strictly narrower, and correctly object-scoped |

`_PROFILE_TO_TASK_ROLE = {'BD': 'BD / Sales'}` normalises the one value where the two
vocabularies differ. This dict appears **seven times** in `views.py`, byte-identical.

Additional guards on `task_status_update`:
- `In Progress` requires a `due_date`. Finance may supply one inline in the same POST.
- `Blocked` (a fresh transition only) requires `block_issue_title` — an `Issue` is created and
  linked to the task in the same request, and `blocked_since` is stamped. Un-blocking clears
  `blocked_since` so a re-block ages from zero.
- `Done` stamps `completed_at`.

All writes use `Task.objects.filter(pk=...).update(...)` rather than `.save()`, to avoid a
lost-update race between two concurrent status changes.

### 5.2 Due dates and the cascade

`task_set_due_date` ([:4117](projects/views.py#L4117)) branches on management authority:

- **PM / Coordinator** — may set any task's date. If `project.cascade_scheduling` is on, calls
  `recalculate_from_task()`; otherwise sets the one date.
- **Anyone else** — may set a date only on a task whose `assigned_role` matches their
  (normalised) role, and **only while cascade is OFF**. With cascade on they get
  *"Due dates are managed automatically by cascading scheduling."*

`recalculate_from_task()` ([utils.py:332](projects/utils.py#L332)) walks every task after the
anchor in `(phase_order, task_order)` order:

- **Internal** task → `due_date = add_calendar_days(previous_internal_due, duration_days)`, and
  it becomes the new chain head.
- **External** task → mirrors the *current internal chain position* without advancing it.
  External tasks run in parallel; they never push the chain.

Every changed date writes a `DueDateChangeLog` row, and the whole set goes out in one
`bulk_update`. The return value is `(count, changed_tasks)`; the HTMX endpoint uses the list to
swap the moved rows out-of-band so the ripple renders without a reload.

`calculate_due_dates()` ([utils.py:406](projects/utils.py#L406)) is the bulk-reset variant,
anchored on `project.activated_at`, reachable via `project_recalculate_dates` (PM only).

`enable_cascade_scheduling` ([:2667](projects/views.py#L2667)) is PM-only, POST-only,
**irreversible**, and additionally gated on the global
`SystemSettings.cascade_scheduling_enabled` master flag.

### 5.3 Checklists

Linked by **string match**, not FK: `ChecklistTaskLink(task_name, project_type)` →
`Checklist` → `ChecklistItem`. `_checklist_for_task()` requires the checklist to be active.

`checklist_item_complete` ([:7677](projects/views.py#L7677)):

- Gate: `_user_can_complete_checklist_item()` ([:2349](projects/views.py#L2349)) — the *same*
  model as `task_status_update`: role-match **OR** management authority. Deliberately broader
  than `task_detail_status_update`'s assigned-user-only rule, and the docstring says so.
- The item must belong to the checklist currently linked to this task — a raw `item_id` is
  never trusted.
- **A photo is mandatory.** `is_checked = True` is written in the same `save()` as
  `photo_file_name`, `photo_url` and `photo_supabase_path`, so a checked item can never lack a
  photo. Upload goes to Supabase under
  `checklist-photos/{project_id}/{task_pk}/{item_pk}/{uuid}_{name}` with a photo-only extension
  allow-list.
- Already-checked is an idempotent no-op.
- Writes `ChecklistItemCompletion(item, task)` plus one `ActivityLog`. No notification.

> **Changed 29 Aug 2026 by prompt 0.5 — see `docs/execution-model.md` §15.** The completion
> now also writes `item_text_snapshot`, in the *same* `save()` as `is_checked` and the photo
> fields, and every read path renders the snapshot rather than `item.label`. The item FK is
> nullable `SET_NULL`, so deleting a checklist item no longer deletes its completions.
> `Checklist` is versioned (`code` / `version_no` / `status`, `is_active` is now a property
> over `status`), its items are immutable once the version is active, and
> `_checklist_for_task()` resolves the *active version of the linked family* — which for
> every checklist that existed before 0.5 is the identical row it returned before.
>
> Four fixtures in `tests_residential_baseline.py` and `tests_soft_delete.py` create an
> active checklist and *then* add an item, which R-7 now forbids; the fix is a fixture
> reorder that changes no assertion. Listed in §15.

**Nothing gates task completion on the checklist.** A task with an unfinished checklist can be
marked `Done`. `docs/execution-model.md` §9 lists "installation checklist blocking" as a
confirmed business rule; it is not implemented.

---

## 6. The three payment milestones

### 6.1 The rows

Created at activation, three per project, `amount = NULL`, `status = 'Pending'`.
`PaymentMilestone.STATUS_CHOICES`: `Pending → Invoiced → Received`.

### 6.2 Setting the agreed amounts

`set_milestone_amounts` ([:6642](projects/views.py#L6642)), `@role_required(['BD', 'PM'])`,
POST with a JSON body `{m1_amount, m2_amount, m3_amount}`. A `null` value skips that
milestone, which is how the BD dashboard's single-milestone pencil works.

**The contract-value check only fires when all three would be non-null after the update.** If
`project.contract_value` is set and `M1 + M2 + M3 != contract_value`, the view returns
`{'success': False, 'error': ...}` with the shortfall spelled out — and **HTTP 200**, not 4xx.
Every failure mode on this endpoint is a 200 with `success: False`. Writes go through
`update_or_create`, in one `atomic()`, plus one `ActivityLog`.

> **Audit finding 5 lives here.** There is no `_pm_owns_project()` call anywhere in the view.
> The test file asserts the PM setting amounts on **their own** project only.

### 6.3 The Finance actions

| Step | View | Gate | Precondition | Writes |
|---|---|---|---|---|
| Invoice | `milestone_invoice` ([:6166](projects/views.py#L6166)) | `@role_required(['Finance'])` | `status == 'Pending'` | `status='Invoiced'`, `invoice_date = today` |
| Receive | `milestone_receive` ([:6192](projects/views.py#L6192)) | `@role_required(['Finance'])` | `status != 'Received'`; `amount_received` required and parseable | `status='Received'`, `received_date`, `amount_received`, `variance_reason` |

`variance_reason` auto-fills to `'Overpayment'` when `amount_received > amount` and no reason
was given. Both redirect to `dashboard_finance`, both write one `ActivityLog`, **neither sends
a notification.**

### 6.4 The bidirectional sync — and the stale half of it

The milestone and its Finance task are kept in step in **both** directions.

**Task → Milestone** ([views.py:3706-3712](projects/views.py#L3706)), inside
`task_status_update`, when a task is marked `Done`:

```python
'Advance Payment Confirmation':      'M1'
'Pre Dispatch Payment Confirmation': 'M2'
'100% Payment Confirmation':         'M3'
```

All three names exist in the template. The milestone is flipped to `Received` (from `Pending`
or `Invoiced`), optionally carrying `amount_received` and `variance_reason` from the same POST,
and a second `ActivityLog` row names the actor and their role — deliberately, so a coordinator
completing a Finance task is not logged generically as "PM".

**Milestone → Task**, inside `milestone_receive`
([views.py:6232-6236](projects/views.py#L6232)):

```python
'M1': 'Advance Payment Confirmation'
'M2': 'Finance Confirmation'          # <-- does not exist
'M3': '100% Payment Confirmation'
```

> ### ⚠ `'Finance Confirmation'` is not a task in the template — and three of the four copies still name it
> The task was deleted, and `utils.py:686-688` records why: *"'Finance Confirmation' is
> intentionally NOT here — it is deleted from the template; its M2 milestone role passes to
> Pre Dispatch Payment Confirmation."*
>
> **The mapping is duplicated four times in `views.py`, and exactly one copy was updated:**
>
> | Copy | Direction | M2 key | M2 sync |
> |---|---|---|---|
> | [views.py:3710](projects/views.py#L3710) `task_status_update` — the task-row dropdown | task → milestone | `'Pre Dispatch Payment Confirmation'` | **works** |
> | [views.py:3909](projects/views.py#L3909) `task_detail_status_update` — the task detail page | task → milestone | `'Finance Confirmation'` | **broken** |
> | [views.py:6232](projects/views.py#L6232) `milestone_receive` | milestone → task | `'Finance Confirmation'` | **broken** |
> | [views.py:6766](projects/views.py#L6766) `project_overview` Finance milestone POST | milestone → task | `'Finance Confirmation'` | **broken** |
>
> **Two consequences.** First, whether completing `Pre Dispatch Payment Confirmation` flips M2
> to `Received` depends on *which button the user pressed* — the dropdown on the task row
> syncs, the identical control on the task detail page does not. Second, when Finance marks M2
> `Received` from either finance surface, the `Task.objects.filter(...)` matches zero rows and
> `Pre Dispatch Payment Confirmation` stays open forever.
>
> All four blocks are wrapped in `except Exception: pass` ("Non-critical — never block the
> milestone update"), and a zero-row `.update()` is not an exception in any case, so every
> failure here is silent. **M1 and M3 sync correctly in both directions; M2 syncs in one
> direction from one of two buttons.**
>
> Recorded as a finding. Not fixed — this session may not modify production code. The two
> tests that would have pinned the broken halves are `@skip`ped in
> `tests_residential_baseline.py` with a pointer to this section.

### 6.5 The payment-milestone notification

`Task.is_payment_milestone` is `True` on exactly three tasks — the three Finance confirmation
tasks, **not** on `Plant Commissioning`. Marking any of them `Done` through
`task_status_update` fires ([views.py:3745-3765](projects/views.py#L3745)):

- **Recipients, deduped in this order:** every active `Finance` profile, then
  `project_managers(project)` (PM + active coordinators), then every active `BD` and `CEO`
  profile.
- **Channels:** `['in_app', 'whatsapp', 'email']`, template `payment_notification`,
  `template_params = [customer_name, task_name, customer_name]`.

Everything goes through the single `send_notification()` chokepoint
([notifications.py:24](projects/notifications.py#L24)), which checks the master switch, then
per-user preference, logs every attempt to `NotificationLog`, and never lets a failed send
propagate.

> **Note against `project_notification_system` in memory:** that note records the M3 trigger as
> `Plant Commissioning`. In the template as it stands, `Plant Commissioning` (phase 8, task 6,
> Site Engineer) carries **no** `is_payment_milestone` flag. M3's trigger is
> `100% Payment Confirmation` in phase 9.

### 6.6 Vendor payment requests — a separate object entirely

`PaymentRequest` is SCM→Finance for **vendor invoices**, unrelated to the customer-facing
`PaymentMilestone`.

| Step | View | Gate | Requires |
|---|---|---|---|
| Raise | `raise_payment_request` ([:6292](projects/views.py#L6292)) | inline `role != 'SCM'` → 403 | vendor, **a BOQ item on this project**, invoice number, amount, **and an invoice document** — all five mandatory |
| Confirm | `confirm_payment_request` ([:6388](projects/views.py#L6388)) | inline `role != 'Finance'` → 403 | `status == 'pending'`; a parseable `payment_date` |

`STATUS_CHOICES` is `pending | confirmed` — lowercase, and there is no `due` and no `done`. The
model docstring says **"No edit/cancel by design"**. The BOQ item is scoped to the URL project
(`boq__project=project`), so another project's items are unreachable through the form.

Confirmation **does** notify: every active `SCM`, then `project_managers(project)`, then every
active `CEO`, deduped, on `['in_app', 'whatsapp', 'email']` with template `invoice_paid`.
Raising notifies nobody.

---

## 7. Issues

`Issue.STATUS_CHOICES`: `Open → In Progress → Resolved → Closed`, with `Resolved → Open` as the
reopen. Three creation endpoints, all POST-only, all writing `status='Open'` and
`raised_by = profile`:

| Endpoint | Extra link |
|---|---|
| `create_project_issue` ([:7788](projects/views.py#L7788)) | `task=None` |
| `create_task_issue` ([:7882](projects/views.py#L7882)) | `task = <task>` |
| `create_delivery_issue` ([:7978](projects/views.py#L7978)) | `delivery_challan = <dc>`, cross-checked against the URL project |

Plus a fourth: `task_status_update` creates one automatically when a task is blocked.

| Transition | View | Gate | Precondition |
|---|---|---|---|
| Open → In Progress | `update_issue_status` ([:8119](projects/views.py#L8119)) | PM-only stanza | **must have an assignee**; closed → 403 |
| In Progress → Resolved | `resolve_issue` ([:8158](projects/views.py#L8158)) | PM-only stanza | **resolution note required**; closed → 403 |
| Resolved → Closed | `close_issue` ([:8228](projects/views.py#L8228)) | **`_is_project_pm()`** → 403 | status `== 'Resolved'` |
| Resolved → Open | `reopen_issue` ([:8261](projects/views.py#L8261)) | **`_is_project_pm()`** → 403 | status `== 'Resolved'`; clears `resolved_at` and `resolution_note` |
| (re)assign | `assign_issue` ([:8295](projects/views.py#L8295)) | PM-only stanza | closed → 403 |

`_is_project_pm()` ([:7775](projects/views.py#L7775)) is
`role in ('PM', 'Project Coordinator') AND user_can_manage_project()` — a genuine object-level
check on both role and ownership. **Close and reopen are the only two of the ten Issue
endpoints that carry one.** All status writes use `filter(pk=..., status=<expected>).update()`
so a concurrent change is detected as `updated == 0` rather than silently overwritten.

**Notifications:**

- `create_project_issue` / `create_task_issue` / `create_delivery_issue` — if an assignee was
  named and is not the raiser, one `issue_created` notification to them on
  `['in_app','whatsapp','email']`. Then one **in-app-only** notification to each project
  manager who is neither the raiser nor the assignee.
- `resolve_issue` — `['in_app','whatsapp','email']`, template `issue_resolved`, to
  `project_managers(project)` **plus** `issue.assigned_to` **plus** `issue.raised_by`, deduped,
  **excluding the resolver**. This is the one Issue endpoint that reaches outside the system.
- `update_issue_status`, `assign_issue`, `close_issue`, `reopen_issue` — nothing.

> **Audit finding 2 lives here.** Eight of the ten endpoints carry only the PM-only stanza,
> which is a no-op for the other nine roles. The test file asserts **the project's own PM**
> driving the full lifecycle, and asserts that a Site Engineer is refused close and reopen. It
> does not assert any unrelated-user path.

---

## 8. Documents and attachments

Two upload endpoints, structurally identical:

| | `upload_project_document` ([:7388](projects/views.py#L7388)) | `upload_task_attachment` ([:7522](projects/views.py#L7522)) |
|---|---|---|
| Model | `ProjectDocument` | `TaskAttachment` |
| Supabase path | `project-documents/{project_id}/{uuid}_{name}` | `task-attachments/{project_id}/{task_pk}/{uuid}_{name}` |
| Field set | `file_name`, `file_url`, `supabase_path`, `file_type`, `file_size_kb`, `uploaded_by` | same, plus `task` |

Both accept `request.FILES.getlist('files')` — multi-file, per-file success/failure, and a
**partial success** outcome (`"N of M files uploaded"`) that still returns `ok: True`.
`file_type` is derived: `'Photo'` if the extension is in `ALLOWED_PHOTO_EXTENSIONS`, else
`'Document'`. `file_size_kb` is `max(1, size // 1024)` — a sub-KB file records as 1, never 0.

`_validate_and_upload()` ([:7305](projects/views.py#L7305)) enforces extension allow-list,
20 MB cap, and an extension↔MIME cross-check (with `application/octet-stream` accepted as
"browser didn't say"). It raises `ValueError` on any of the three; the caller turns that into a
per-file failure rather than aborting the batch.

**Deletion is correctly scoped and the upload is not.** `delete_project_document`
([:7483](projects/views.py#L7483)) and `delete_task_attachment` ([:7617](projects/views.py#L7617))
both require `uploaded_by == profile` **or** `role == 'Admin'` → 403 otherwise. Deletion is
soft: `is_deleted=True`, `deleted_at`, `deleted_by`; the Supabase object survives until
`purge_deleted_files` hard-deletes it after `FILE_RETENTION_DAYS`.

Both AJAX (`X-Requested-With: XMLHttpRequest` → JSON) and plain-POST (messages + redirect,
honouring a local-only `?next=`) response modes are supported.

---

## 9. Commissioning and project closure

### Commissioning

Phase 8 is ten tasks — five PM (`Pre Commissioning Visit by DISCOM`, `SCO Release`,
`Meter Installation by DISCOM`, `Commissioning Report Approved`, `Customer Handover`) and five
Site Engineer (`Meter Testing`, `RMS Configuration`, `Plant Commissioning`,
`Commissioning Report Prepared`, plus the Finance invoice task at position 10). Four of the PM
tasks are `External`. Phase 9 is a single Finance task, `100% Payment Confirmation`, carrying
`is_payment_milestone`.

Every one of them is an ordinary status dropdown. There is no commissioning gate, no report
artifact requirement, no HOTO object, and no as-built submission.

### Closure — **there is none**

> ### ⚠ `Project.status` can never become `'Commissioned'`
> `Project.STATUS_CHOICES` offers `Draft`, `Active`, `In Progress`, `Commissioned`, `On Hold`,
> `Cancelled` ([models.py:15-25](projects/models.py#L15-L25)), and
> `Project.commissioned_at` exists ([models.py:106](projects/models.py#L106)).
>
> A whole-repo search finds exactly **two** writes to `Project.status` in application code:
> `= 'Draft'` in `project_create` ([views.py:2402](projects/views.py#L2402)) and `= 'Active'`
> in `project_activate` ([views.py:2606](projects/views.py#L2606)). Neither
> `ProjectCreateForm` nor `ProjectEditForm` nor `PostActivationFieldEditForm` includes
> `status` in its `fields`. **Nothing anywhere writes `commissioned_at` at all.**
>
> So: `In Progress`, `Commissioned`, `On Hold` and `Cancelled` are unreachable through the
> product. Four templates render a `Commissioned` badge that no code path can produce. Every
> dashboard filters on `status__in=['Active', 'In Progress']`, which in practice means
> `'Active'` alone — a project that finishes stays "Active" forever, and there is no state in
> which a Residential project is done.
>
> The only exit from `Active` is `project_delete` (Admin-only soft delete), which sets
> `is_deleted=True` and **leaves `status` untouched**.
>
> This is not an access-isolation finding and it is not in `ACCESS_ISOLATION_AUDIT.md`. It is
> a workflow gap, recorded here because §9 of this document could not otherwise be written.
> The test file pins the two transitions that **do** exist and asserts nothing about the four
> that do not.

---

## 10. Reference — what fires a notification on a Residential project

Six triggers, all through `send_notification()`. Everything else in the lifecycle is silent.

| Trigger | Recipients | Channels | Template |
|---|---|---|---|
| Task assigned interactively (`task_assign`, `task_assign_design_head`) | the assignee | in_app + whatsapp + email | `task_assignment` |
| Issue created **with** an assignee | assignee | in_app + whatsapp + email | `issue_created` |
| Issue created (any) | each project manager ≠ raiser, ≠ assignee | **in_app only** | — |
| Issue resolved | project managers + assignee + raiser, minus the resolver | in_app + whatsapp + email | `issue_resolved` |
| Payment-milestone task marked `Done` | all Finance + project managers + all BD + all CEO | in_app + whatsapp + email | `payment_notification` |
| Vendor payment request confirmed | all SCM + project managers + all CEO | in_app + whatsapp + email | `invoice_paid` |

**Silent:** activation, template seeding, bulk assignment, every BOQ transition via the
standalone endpoints, DC creation, GRN confirmation, GRN override, milestone invoice, milestone
receive, payment-request raise, every document upload, every checklist completion, issue status
change, issue assignment, issue close, issue reopen.

Two documented bypasses exist and are **not** used by any Residential path:
`send_raw_email()` skips the master switch; `send_aggregate_email()` skips per-recipient
preference.

---

## 11. Things this document had to record as broken

Found while reading source for §1–§10. None were fixed — this session may not modify
production code. None of them is one of `ACCESS_ISOLATION_AUDIT.md`'s fifteen findings.

| # | Where | What |
|---|---|---|
| B-1 | [views.py:2631](projects/views.py#L2631) | Activation success message says "53 tasks created". The code creates and asserts **52** |
| B-2 | [views.py:3909](projects/views.py#L3909), [:6232](projects/views.py#L6232), [:6766](projects/views.py#L6766) | The M2 task-name mapping is duplicated four times and **three copies still name `'Finance Confirmation'`**, a task deleted from the template. M2 sync works only from the task-row dropdown, and never in the milestone→task direction. All four sites swallow the failure. See §6.4 |
| B-3 | app-wide | No code path sets `Project.status` to `'Commissioned'`, `'In Progress'`, `'On Hold'` or `'Cancelled'`, and nothing ever writes `commissioned_at`. **There is no project-closure workflow** |
| B-4 | [design_views.py:218](projects/design_views.py#L218) | The entire design module is OPEX-only. `docs/execution-model.md` §5 describes a design workflow that no Residential project can enter |
| B-5 | [views.py:5993](projects/views.py#L5993) vs [:4483](projects/views.py#L4483) | `_notify_boq_acknowledged()` is called from `boq_detail`'s inline acknowledge branch but **not** from the standalone `boq_acknowledge` endpoint. The same business event notifies or does not depending on which button was pressed |
| B-6 | [views.py:2349](projects/views.py#L2349) | No task-completion gate consults the checklist. `docs/execution-model.md` §9 lists "installation checklist blocking" as confirmed with the Tenders team; it is not implemented |
| B-7 | [views.py:5970](projects/views.py#L5970) | The standalone `boq_submit` endpoint snapshots raw `Decimal`s into a `JSONField` and raises an **unhandled `TypeError` on every submission**, on both SQLite and PostgreSQL. The inline `submit_design` branch coerces via `_boq_snapshot()` and works. The two also disagree on which statuses may be submitted. See §3.2 |

B-1, B-2, B-5 and B-7 are defects with contained fixes and belong in a prompt of their own —
B-7 is a 500 on a live URL and is the most urgent of the four. B-3, B-4 and B-6 are missing
workflow, not defects, and belong in the phase plan.

**A pattern runs through B-2, B-5 and B-7:** each is a business act implemented twice, in two
places, where only one copy was kept current. The M2 task name, the BOQ snapshot and the
acknowledgement notification all behave differently depending on which button the user pressed.
That is worth a deduplication pass in its own right, and it is exactly the shape of defect a
permissions lockdown will otherwise multiply — 0.2 will be applying a gate to each duplicate
separately.
