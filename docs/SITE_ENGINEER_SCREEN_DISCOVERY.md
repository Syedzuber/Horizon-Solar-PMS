# 2.5 discovery — is the existing Site Engineer path actually usable

**Audit-only.** No application code was touched. Every claim below is a grep hit, a raw
paste, or a query against the local development database (labelled as such where used).

---

## 0. A premise correction, first

The prompt says *"project_overview and task_detail were merged into one URL earlier"*.
That is not what was merged. `project_detail` and `project_overview` were merged;
`task_detail` was never part of it and is a separate URL to this day.

```
projects/views.py:2711
def project_detail(request, project_id):
    """Merged into project_overview — redirect all traffic there.
    ...
    return redirect('project_overview', project_id=project_id)
```

```
projects/views.py:8154
def project_overview(request, project_id):
    """
    Combined project page: info card, status blocks, documents, and phase/task list.
    Merges the former project_detail (task management) and project_overview (status summary)
    into a single URL. Access: all roles; PM and SE isolation applies.
    """
```

`task_detail` is its own view at its own URL ([urls.py:268-269](projects/urls.py#L268-L269),
[views.py:8865](projects/views.py#L8865)). This matters for Task 2: the answer there is
"the latter" — a separate per-task view — and it is separate by construction, not by
oversight.

---

## Task 1 — the two existing screens, exactly

### The phase-wise task list

| | |
|---|---|
| URL name | `project_overview` |
| Pattern | `projects/<str:project_id>/overview/` |
| Route | [urls.py:247](projects/urls.py#L247) |
| View | [views.py:8154](projects/views.py#L8154) |
| Template | [project_overview.html](projects/templates/projects/project_overview.html) (1920 lines) |
| Task list | §6, from line 953; rows are [partials/_task_row.html](projects/templates/projects/partials/_task_row.html) |

```
projects/urls.py:245-247
    # ---------------------------------------------------------------------------
    # Project overview — all roles with access to the project
    # ---------------------------------------------------------------------------
    path('projects/<str:project_id>/overview/', views.project_overview, name='project_overview'),
```

An SE reaches it from their dashboard card's **View Project** button
([dashboard/site-engineer.html:247](projects/templates/dashboard/site-engineer.html#L247)),
which is the only project link on that dashboard.

The `← Projects` control at the top of the overview is correctly role-gated away from an
SE, so there is no dead link — they get `← Dashboard` instead:

```
projects/templates/projects/project_overview.html:112-116
        {% if role == 'PM' or role == 'Admin' or role == 'CEO' %}
        <a href="{% url 'project_list' %}" class="btn btn-outline-secondary btn-sm">← Projects</a>
        {% else %}
        <a href="{{ user_dashboard_url }}" class="btn btn-outline-secondary btn-sm">← Dashboard</a>
        {% endif %}
```

(This gating is load-bearing: `project_list` redirects a non-Admin, non-CEO caller to
`dashboard_pm` [views.py:2666-2671](projects/views.py#L2666-L2671), which is
`@role_required(['PM', 'Project Coordinator'])` [views.py:574](projects/views.py#L574) and
now returns a hard 403 rather than a redirect. An SE following that link would be
refused. The template does not let them.)

### The GRN screen

| | |
|---|---|
| Screen | `delivery_challan_detail` — `projects/<project_id>/delivery-challans/<dc_id>/` — [urls.py:418](projects/urls.py#L418), [views.py:10502](projects/views.py#L10502) |
| Write endpoint | `confirm_grn` — `.../grn/` — [urls.py:421](projects/urls.py#L421), [views.py:10551](projects/views.py#L10551), POST only |
| Template | [delivery_challan_detail.html](projects/templates/projects/delivery_challan_detail.html), GRN form at lines 180-244 |

There is no GRN list screen. The DC list lives inside `project_overview` §3
(line 503 onward), and each row links to `delivery_challan_detail`
([project_overview.html:529](projects/templates/projects/project_overview.html#L529)).
The SE dashboard's **"N GRN Pending"** chip is a `<span class="badge">`, not a link
([dashboard/site-engineer.html:210-214](projects/templates/dashboard/site-engineer.html#L210-L214)).

### The scoping mechanism — still task-derived, confirmed

`Project.assigned_site_engineer` **does not exist**. It was removed:

```
$ grep -rn "assigned_site_engineer" --include=*.py --include=*.html . | grep -v venv
./projects/migrations/0005_project_redesign.py:88:    ('assigned_site_engineer', models.ForeignKey(
./projects/migrations/0014_webhook_fields.py:40:    name='assigned_site_engineer',
./projects/migrations/0037_remove_assigned_site_engineer.py:15:    name='assigned_site_engineer',
./projects/migrations/0038_add_is_design_head_to_userprofile.py:9:    ('projects', '0037_remove_assigned_site_engineer'),
./projects/reports.py:129:    # Project.assigned_site_engineer was removed in migration 0037, so a site engineer's
./projects/tests_residential_baseline.py:926: def test_the_assigned_site_engineer_moves_a_task_through_to_done(...)
```

The live rule is one line, in one place:

```
projects/permissions.py:199-202
    if role == 'Site Engineer':
        # Mirrors dashboard_site_engineer's scoping: any task on this project assigned
        # to this user. Reverse relations only — keeps this module import-free.
        return project.phases.filter(tasks__assigned_to=profile).exists()
```

`dashboard_site_engineer` selects on the same relation
([views.py:925-928](projects/views.py#L925-L928): `phases__tasks__assigned_to=se_profile`),
and `project_overview` gates on `user_can_view_project` at
[views.py:8169](projects/views.py#L8169). **Task-derived scoping is still accurate.**

One thing worth naming while we are here: `phases` in `project_overview` is *not* filtered
to the viewer's own tasks, and never was — the view says so explicitly
([views.py:8378-8379](projects/views.py#L8378-L8379): *"`phases` itself is NOT filtered and
never was"*). A Site Engineer opening a Residential site scans all 53 tasks to find their
own. The only affordance separating them is a CSS class on the row:

```
projects/templates/projects/partials/_task_row.html:19-20
<tr id="task-row-{{ task.pk }}"...
    {% if task.assigned_to and task.assigned_to == request.user.profile %}class="task-row-mine"{% endif %}
```

---

## Task 2 — is 2.1's approval action reachable from the task list?

**No. It is only reachable by drilling into `task_detail`.**

The panel is included in exactly one page:

```
$ grep -rn "_task_approval.html" --include=*.html --include=*.py projects/
projects/templates/projects/partials/_task_approval_response.html:11:{% include 'projects/partials/_task_approval.html' %}
projects/templates/projects/task_detail.html:56:  {% include 'projects/partials/_task_approval.html' %}
```

The second hit is the HTMX re-render of the first. `project_overview.html` contains no
match for `approval` at all:

```
$ grep -rn "checklist\|approval" projects/templates/projects/project_overview.html
(no output)
```

`_task_row.html` carries no submission badge, no approval badge, and no submit control —
the full row markup is six cells: order, name, assignee, status select, due date,
completed date.

### The dead end this creates

The row's status `<select>` renders **every** `Task.STATUS_CHOICES` value including `Done`
([_task_row.html:96-98](projects/templates/projects/partials/_task_row.html#L96-L98)). On
an OPEX project, choosing `Done` there is always refused — rung 1 of the shared decision
path:

```
projects/views.py:4283-4293
    if (new_status == Task.DONE
            and project.project_type == 'OPEX'
            and task.approved_at is None):
        messages.error(
            request,
            f"'{task.task_name}' cannot be marked Done directly — an OPEX task must "
            f"be submitted for approval and approved by the project manager or QA/QC "
            f"before it can be completed."
        )
        return _TASK_STATUS_REFUSED
```

So the one control the task list offers for finishing work is, on OPEX, a control that
cannot succeed — and the message tells the engineer what to do without giving them
anywhere to do it. The same dead option is offered again on `task_detail`
([_task_detail_status.html:19-25](projects/templates/projects/partials/_task_detail_status.html#L19-L25)),
directly above the Approval card that is the real path.

### How much of OPEX this covers

11 of the 23 OPEX template tasks are Site Engineer tasks, all non-mirror, all subject to
the gate — 9 of them in the Installation phase:

```
$ python -c "...TaskTemplateTask.objects.filter(phase__template__project_type='OPEX')..."
OPEX roles: Counter({'Site Engineer': 11, 'PM': 5, 'SCM': 4, 'Design': 2, 'Project Coordinator': 1})
('Installation', 'Civil Work and MMS Installation', False)
('Installation', 'Module Installation', False)
('Installation', 'LA and Earthing Installation', False)
('Installation', 'DC Cable Laying with Conduit', False)
('Installation', 'DCDB and ACDB Installation', False)
('Installation', 'Inverter Installation', False)
('Installation', 'AC Cable Laying', False)
('Installation', 'RMS Installation', False)
('Installation', 'Solar Generation Meter Installation', False)
('Testing & Commissioning', 'Testing & Commissioning', False)
('Testing & Commissioning', 'Net Meter Installation', False)
```

Nine Installation tasks are the shape of work that gets finished in a burst on one site
visit and ticked off together.

### The click count, from the SE landing page

An SE's landing is `dashboard_site_engineer` ([urls.py:23](projects/urls.py#L23)); there is
no project list they can reach (see Task 1). Two paths exist.

**Path A — from the project card (the general case):**

1. **View Project** → `project_overview` *(page load 1)*
2. Vertical-scroll past §1 info card, §2 phase progress + payment milestones, §3 BOQ +
   delivery challans + material status, §5 documents, §5.5 Gantt notice — five sections —
   to §6 at line 953
3. Find own row among all the project's tasks; **click the task name** → `task_detail`
   *(page load 2)*
4. If the task is `Not Started`: set the status select to **In Progress** *(POST)* — the
   submit form is not offered otherwise:
   ```
   _task_approval.html:127-149
   {% if task.status == 'In Progress' %}
     ... <form ... task_submit_for_approval> ...
   {% else %}
     <p class="text-muted small mb-0">
       This task must be In Progress before it can be submitted for approval.
   ```
5. Type **"What was done (required)"** into the textarea *(required, no default)*
6. **Submit for approval** *(POST)*
7. `← Project` back to `project_overview`, scroll all the way down again — repeat from 2.

**2 page loads + 3 interactions + 2 scroll operations per task**, and the whole of steps
2 and 7 repeat for every task.

**Path B — from the urgency stat blocks (the shortcut):** `Due Today` / `Due Soon` /
`Overdue` on the SE dashboard link to `tasks_drill_down`
([urls.py:397-399](projects/urls.py#L397-L399)), whose rows link **straight to
`task_detail`**:

```
projects/templates/tasks/task_drill_down.html:53
          <a href="{% url 'task_detail' group.project.project_id task.pk %}"
```

That is a genuinely short path — 2 loads, no overview hop. **But all three filters require
a non-null `due_date`** ([views.py:872-874](projects/views.py#L872-L874),
[views.py:1077](projects/views.py#L1077)), and most tasks have none:

```
local dev DB: tasks 1861, due_date null 1524 (81.9%)
```

The earlier Gantt audit put this at ~99.6% null on live Railway data. So for the great
majority of tasks the shortcut is empty and Path A is the only path.

### Is that real friction?

Yes, and specifically for the case this feature exists to serve. The friction is not the
two page loads — it is that **the task list gives no indication that submission is the
required action, offers a control that always refuses instead, and the real control is
below the fold of a second page.** An engineer finishing nine Installation tasks in one
afternoon makes that round trip nine times, having first discovered by trial and refusal
that the obvious control does not work. On a phone (Task 5) step 2 also costs a horizontal
scroll, because the status column is off-screen at rest.

---

## Task 3 — 2.4's checklist UI

**Same answer, same path: `task_detail` only.**

```
projects/templates/projects/task_detail.html:148-160
{% if checklist %}
<div class="card shadow-sm mb-4">
  <div class="card-header ...>
      {{ checklist.name }}
      <span id="checklistCountBadge">...</span>
  <div class="card-body p-0" id="checklistSection">
    {% include 'projects/partials/_checklist.html' %}
```

`_checklist.html` is included nowhere else but its own HTMX response partial. The context
builder `_checklist_context()` ([views.py:4072](projects/views.py#L4072)) is called from
exactly two places — the `task_detail` full render
([views.py:8916](projects/views.py#L8916)) and `_render_checklist_hx()`
([views.py:4114-4116](projects/views.py#L4114-L4116)). `project_overview` never calls it,
and no row shows whether a task even *has* a checklist, let alone how much of it is done.

**The actual path:** SE dashboard → View Project → scroll to §6 → click task name →
`task_detail` → scroll past the header card, the Approval card, and the Attachments card →
Checklist card → per-item form. Completion itself is one atomic action per item and a
**photo is mandatory**:

```
projects/templates/projects/partials/_checklist.html:55-65
            {# Check + photo are one atomic action — the file input is REQUIRED, so an item
            can never be ticked without a photo. #}
            <form method="post" enctype="multipart/form-data"
                  action="{% url 'checklist_item_complete' ... %}"
                  hx-post="..." hx-encoding="multipart/form-data"
                  hx-target="#checklistSection" hx-swap="innerHTML" ...>
              <input type="file" name="photo" accept=".jpg,.jpeg,.png" required
                     class="form-control form-control-sm">
              <button type="submit" class="btn btn-success btn-sm">Check + Upload Photo</button>
```

A mandatory photo per item is a *phone* interaction by definition — it is the one action in
this whole audit that cannot sensibly be done at a desk. It currently sits at the bottom of
the right-hand column of a table, on the third screen of a drill-down. See Task 5 for the
`accept` attribute problem.

**Coverage note, local dev DB only:** there is one checklist and it is a draft, linked to
Residential:

```
links by project_type: ['Residential']
checklists: [('DEMOCHECKLIST', 'DemoChecklist', 'draft')]
```

`_checklist_for_task()` treats a draft as unassigned
([views.py:4048-4049](projects/views.py#L4048-L4049)), so on this database **no task shows
a checklist at all**, and no OPEX task could — there is no OPEX link row. This is dev data
and production may differ, but it means the checklist surface has never been exercised
against OPEX, which is the project type 2.1's gate applies to.

---

## Task 4 — the GRN screen's current state

**URL/view:** confirmed in Task 1 — `delivery_challan_detail` at
[urls.py:418](projects/urls.py#L418) / [views.py:10502](projects/views.py#L10502), with the
SE write at `confirm_grn`, [urls.py:421](projects/urls.py#L421) /
[views.py:10551](projects/views.py#L10551).

**The gate is two independent checks, role and scope:**

```
projects/views.py:10549-10550
@login_required
@role_required(['Site Engineer'])
```
```
projects/views.py:10576
    if not user_can_view_project(request.user, project):
        raise Http404
```

which resolves, for a Site Engineer, to `project.phases.filter(tasks__assigned_to=profile).exists()`
(permissions.py:202 — pasted in Task 1).

### Was the open question confirmed? **Yes — it has been closed. The browser test plan is stale.**

The prompt asks me not to assume either way, so: the confirmation happened, and it is
recorded in the execution model's open-questions register, dated and attributed.

```
docs/execution-model.md:587
| ~~**B-19**~~ | ~~**Who actually confirms a GRN — the engineer holding a task on that site,
or whoever is standing at the warehouse when the lorry arrives?**~~ **ANSWERED 30 Aug 2026
by the product owner: the engineer holding a task on that site. 0.2's scoping was correct
and needs no widening.** `confirm_grn` stays as 0.2 left it — scoped through
`user_can_view_project()`, whose Site Engineer branch is exactly "holds a task on this
project". Receipt is **not** recorded by whoever happens to be present. Note what this does
and does not settle: it fixes who confirms a **GRN**, and it is not the same question as
what a warehouse keeper may do inside their own building (B-14, answered separately). A
keeper with no task on a site does not gain GRN confirmation on it by holding
`is_warehouse_keeper` — that flag grants nothing on its own by construction. |
```

The three documents that still describe it as open all predate 30 Aug 2026 and were moved
into `docs/` unchanged (`803f0c3`, 4 Sep 2026, *"Move planning docs into docs/ (uncommitted
since before phase 0)"*):

- [docs/BROWSER_TEST_PLAN.md:203-206](docs/BROWSER_TEST_PLAN.md#L203-L206) — *"**Confirm
  with Sudhir or your SCM lead that this matches how it is actually done**"* — **stale**
- [docs/DEPLOY_PLAN.md:118-119](docs/DEPLOY_PLAN.md#L118-L119) — *"You have confirmed this
  matches practice — but confirm again with Sudhir the first time someone actually records
  one"* — a *watch item*, weaker than the test plan's phrasing, and still reasonable as a
  first-real-use check
- [docs/PHASE_0_COMPLETION.md:337-352](docs/PHASE_0_COMPLETION.md#L337-L352) — states B-19
  as open; superseded by the execution model entry above

**Nothing in 2.5's scope should treat GRN scoping as an open question.** The residual item
is the deploy plan's operational watch — confirm on first real use — not a rule decision.

**Functional state of the screen itself:** unchanged since 0.2 and complete. It captures
received and damaged quantities separately per line item, derives `condition` for backward
compatibility, stamps `grn_confirmed_by`/`grn_date`, and calls `recalculate_dc_status()`
once after the loop with `actor` and `REASON_GRN_CONFIRMED` for the state ledger
([views.py:10634-10636](projects/views.py#L10634-L10636)). An already-`Received` DC is
refused ([views.py:10586-10587](projects/views.py#L10586-L10587)). No 2.1/2.4 work touched
it and it has no dependency on either.

---

## Task 5 — mobile reality check

**Both are desktop-first with a responsive fallback only.** Not "unusable", but not built
for a phone either, and the fallback is horizontal scroll in exactly the places an engineer
needs to type.

### What the page actually declares

The viewport meta is correct and Bootstrap 5.3.3 is loaded from CDN:

```
projects/templates/base.html:6-8
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
```

Beyond that there is **no mobile-specific CSS anywhere in the project**:

```
$ grep -rln "@media" projects/templates/
(no output)
$ find projects/static -name "*.css"
(no output)
$ grep -rn "d-none d-\|d-md-none\|d-lg-none\|d-sm-none" \
    project_overview.html delivery_challan_detail.html task_detail.html _task_row.html _checklist.html
(no output)
```

Zero media queries, zero stylesheets of our own, and not one responsive show/hide utility
on any of the four SE-facing templates. Every mobile behaviour below is a Bootstrap
default.

### The actual breakpoint behaviour, screen by screen

**`project_overview` §6 task table** — wrapped in `.table-responsive`, which in Bootstrap 5
is `overflow-x: auto` at *every* width (no breakpoint suffix is used), so it never
restacks; it scrolls sideways.

```
projects/templates/projects/project_overview.html:964-972
      <div class="table-responsive">
        <table class="table table-sm table-hover mb-0">
          <thead class="table-light">
            <tr>
              <th style="width:2.5rem;">#</th>
              <th>Task</th>
              <th style="width:13rem;">Assigned To</th>
              <th style="width:12rem;">Status</th>
              <th style="width:10rem;">Due Date</th>
              <th style="width:8rem;">Completed</th>
```

Fixed widths alone are 2.5 + 13 + 12 + 10 + 8 = **45.5rem ≈ 728px**, before the flexible
Task column takes anything (task names run to *"Solar Generation Meter Installation"*).
On a 375px-wide phone that is roughly two and a half screens of horizontal scroll, and the
**Status select — the only actionable control in the row — begins past 450px and is
entirely off-screen at rest.** The engineer scrolls right to reach it, at which point the
task name has scrolled out of view.

**`delivery_challan_detail` GRN form** — same construction, and worse, because all three
inputs are in the right-hand half:

```
projects/templates/projects/delivery_challan_detail.html:195-205
      <div class="table-responsive">
        <table class="table table-sm">
          <thead class="table-light">
            <tr>
              <th>Category</th>
              <th>Description</th>
              <th class="text-end">Ordered</th>
              <th style="width:10rem;">Received Qty</th>
              <th style="width:9rem;">Damaged</th>
              <th style="width:14rem;">GRN Notes</th>
```

Fixed widths 10 + 9 + 14 = **33rem = 528px** of inputs, sitting behind Category,
Description and Ordered — call it ~850px minimum table width. Typing a received quantity
on a phone means scrolling right, losing sight of which item the row describes, and
repeating that per line item. The card header and the explanatory paragraph above the table
are fine; the surrounding metadata grid uses `col-sm-6 col-md-3`
([lines 47-79](projects/templates/projects/delivery_challan_detail.html#L47-L79)) and does
stack properly below 576px. It is only the form that fails.

**`task_detail`** — genuinely the best of the three. It is a vertical stack of cards, and
its header grid is `col-sm-6 col-md-3` throughout
([task_detail.html:22-48](projects/templates/projects/task_detail.html#L22-L48)), so it
stacks to full width below 576px. The Approval card is the **second** card on the page
([lines 55-57](projects/templates/projects/task_detail.html#L55-L57)) — a good position —
and its form is a plain textarea plus a button, which works fine on a phone. **The approval
panel itself is the one 2.1/2.4 surface that is already mobile-usable.**

**The checklist table** — back to horizontal scroll, with the interactive column last:

```
projects/templates/projects/partials/_checklist.html:17-24
<div class="table-responsive">
  <table class="table table-sm table-hover align-middle mb-0">
      <tr>
        <th style="width:3rem;">#</th>
        <th>Item</th>
        <th style="width:11rem;">Photo</th>
        <th style="width:13rem;">Status</th>
```

3 + 11 + 13 = 27rem = 432px fixed plus the Item text; the file input and **Check + Upload
Photo** button are in the rightmost cell.

**The camera hint is missing.** The photo input is extension-filtered with no `capture`:

```
projects/templates/projects/partials/_checklist.html:61
              <input type="file" name="photo" accept=".jpg,.jpeg,.png" required
```
```
$ grep -rn "capture=" projects/templates/
(no output)
```

`accept="image/*"` is what reliably surfaces the camera alongside the gallery in the mobile
file picker; extension lists are handled inconsistently across mobile browsers, and there
is no `capture="environment"` to bias toward the rear camera. For a mandatory
photograph-the-work action this is a one-line gap with a disproportionate effect.

**Navigation** — the navbar has no `navbar-expand-*` class, no toggler, no collapse; it is
two flex `<div>`s that wrap onto more lines as the viewport narrows
([base.html:44-49](projects/templates/base.html#L44-L49)). This was independently
established in `SESSION_A_AUDIT.md:379-384`. Wrapping is ugly but not broken, and it costs
vertical space above every screen on a phone.

### Plainly

`task_detail`'s cards and forms are mobile-usable today. Every **table** on the SE path —
the task list, the GRN form, the checklist — is not: each puts its interactive column
beyond the right edge of a 375px viewport and relies on the engineer scrolling sideways to
find it. Nothing is broken; everything is awkward, and awkward in the specific places where
work gets recorded.

---

## Task 6 — options, sized. These need a scope decision; I am not making it.

### (a) Nothing further needed — **not true, and I will not claim it**

Two findings rule (a) out on their own:

1. 2.1's submit action is invisible from the screen the engineer scans, and the control
   that *is* there for finishing work (the row status select's `Done`) always refuses on
   OPEX. That is a dead end, not a preference.
2. The checklist photo input has no working camera affordance on the one action that is
   inherently a phone action.

Everything else in (b) is genuine polish. These two are not.

### (b) Targeted fixes to the existing screens — **recommended**

No new screen, no new URL, no new permission surface. Four items, listed most to least
valuable:

**b1 — surface approval state and the submit action on the task row.**
`task_submit_for_approval` ([views.py:4691](projects/views.py#L4691)), the permission
helper `user_can_submit_task_for_approval`
([permissions.py:1134](projects/permissions.py#L1134)), the shared context builder
`_task_approval_context()` ([views.py:3918](projects/views.py#L3918)) and the HTMX response
partial all already exist and are already used by the detail page. The work is a state
badge (Not submitted / Awaiting approval / Approved) in `_task_row.html` plus either an
inline submit form posting to the existing endpoint with `hx-target` on the row, or a
direct link to `task_detail#taskApprovalBlock`. The one real cost is that
`project_overview` currently passes only role-level flags into the row and would need
per-task approval context for its rows — cheap, but it must be built server-side, since the
codebase's own rule is that the panel *"decides nothing"* and a template that re-derived
the OPEX scope would be a second copy of it (`_task_approval.html:7-14`).
**~0.5-1 day including tests.**

**b2 — stop offering `Done` where it always refuses.** On an OPEX non-mirror task, remove
`Done` from both status selects (`_task_row.html` and `_task_detail_status.html`) via a
server-computed context flag — not an inline `project.project_type == 'OPEX'` test, for the
same reason as b1. Pairs naturally with b1: b1 gives them the right control, b2 takes away
the wrong one. **~2-3 hours including tests.**

**b3 — the camera one-liner.** `accept="image/*"` and `capture="environment"` on the
checklist photo input. Server-side validation of the uploaded file is unchanged and should
be checked to still accept what a phone camera produces (some Android/iOS pickers hand back
HEIC, or a JPEG blob with no filename extension — worth confirming
`checklist_item_complete` [views.py:9234](projects/views.py#L9234) tolerates that before
widening the accept). **~1 hour, plus whatever the HEIC check turns up.**

**b4 — a mobile card layout for the three tables.** The real mobile work: a `d-md-none`
stacked-card variant of the task row, the GRN line item and the checklist item, with the
existing table kept for `md` and up. Three templates, no view changes, no new rules — but
it is genuinely three pieces of UI. **~1.5-2 days.** This one is separable from b1-b3 and
could be decided later; b1-b3 are worth doing regardless of whether b4 happens.

**(b) total: ~1 day for b1-b3, ~3 days including b4.**

### (c) A new purpose-built mobile view — real, but a bigger commitment than it looks

A `/site/` SE-first screen: my tasks today across projects, tap to expand, submit inline,
checklist inline, GRN inline. Roughly **1-2 weeks**, and the honest costs are not in the
templates:

- It duplicates every rule now living in four screens. This codebase is explicitly
  organised around single decision paths — R-18's *"A NEW TASK-STATUS RULE IS ADDED HERE,
  NEVER TO A VIEW"* ([views.py:4189](projects/views.py#L4189)), one approval context
  builder, one checklist context builder. A fifth surface is a fifth place those can drift,
  and each would need its own tests to pin it.
- It needs its own scope gating, and `confirm_grn`'s scope rule was a CRITICAL audit
  finding once already (`ACCESS_ISOLATION_AUDIT.md:1205`).
- **"mobile app" is explicitly on the not-being-built list** — `docs/execution-model.md:562`,
  §7 Scope boundaries, whose instruction is *"If a prompt appears to require one of these,
  **stop and ask**. Do not build a small version of it."* A responsive web view is not a
  native app and arguably not what that line bans, but it is close enough that it needs the
  product owner to say so out loud rather than a session deciding it.

### What I would actually recommend

**(b), and specifically b1 + b2 + b3 first.** Those three are the difference between "the
action exists" and "the action is findable", they are about a day's work, and they reuse
every endpoint and permission helper 2.1 and 2.4 already built. Do them, then look at real
usage before committing to b4's card layouts — and certainly before (c).

The argument against (c) is not that it would be worse. It would probably be better. It is
that (b) is a day, (c) is a fortnight plus a scope-boundary conversation, and until b1 and
b2 exist nobody has yet seen whether the existing screens are adequate *once the action is
actually visible on them.* Right now we are measuring a path with a missing signpost and
concluding the road is wrong.

---

## Verification notes

- Every path, line number and quoted block above is a paste from the working tree at
  `70a6684`.
- Database figures are from the **local development database** via
  `venv/Scripts/python.exe` with `DJANGO_SETTINGS_MODULE=solarpms.settings`, and are
  labelled as such at each use. They are indicative, not production truth — noted where
  that matters (checklist coverage, `due_date` nullness).
- Nothing in this document was carried over from an older spec doc; where an existing doc
  is cited it is because its staleness is itself the finding (Task 4).
