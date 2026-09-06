# `create_delivery_issue` — audit ahead of the SCM pilot

**Investigate-only.** No `.py`, `.html`, `urls.py` or `permissions.py` was edited. No migration was
created. Every walkthrough ran inside `transaction.atomic()` terminated by a `Rollback` exception;
before/after row counts are printed at the end of each and match exactly. Outbound WhatsApp and
email HTTP was blocked at `projects.notifications.requests.post` and
`projects.notifications._zeptomail_post` for the duration — both master switches are **ON** in the
local DB, so this was necessary, not decorative.

Environment: local Postgres `solarpms_local`, `./venv/Scripts/python.exe`, real
`solarpms.settings`, `django.test.Client(SERVER_NAME='localhost')`.

---

## The real question

**Yes — the pilot can demonstrate a shortfall becoming a tracked issue end to end, and the Site
Engineer can be the one who raises it.** `create_delivery_issue` is gated by `@login_required` plus
`user_can_view_project()` and nothing else; the SE branch of that helper is "holds a task on this
project", which the pilot's SE satisfies by definition. The SE's own GRN screen —
`delivery_challan_detail` — carries a **Raise Issue** button visible to the Site Engineer role, and
the POST from that SE returned **302** and wrote an `Issue` row. So the flow does *not* have to
cross two people, and the pilot script does not need a hand-off. What it *does* have to work around
is three real mismatches: **(a)** a shortfall creates nothing on its own — the SE must remember to
raise the issue as a separate act; **(b)** the `Issue` is **challan-level, not line-level** — there
is no FK to `DCLineItem` and no quantity, so "which line is short and by how much" lives only in
the free-text title/description; and **(c)** the SE cannot finish what they start — closure is
`_is_project_pm`-gated, so the PM must close, and there is no auto-close even after a corrected GRN
brings the DC back to `Received`. The pilot can be run by one person up to Resolved; the last step
needs the PM.

---

## DI-1 — The view, exactly

`projects/views.py`, located by function name `create_delivery_issue`. Full decorator stack and
docstring, verbatim:

```python
@login_required
def create_delivery_issue(request, project_id, dc_id):
    """
    Raise an issue linked to a specific DeliveryChallan (delivery_challan.issues relation).
    Follows the same pattern as create_task_issue but scoped to DC instead of Task.
    Access: all roles with project access; PM isolation applies. POST only.
    """
    if request.method != 'POST':
        return redirect('delivery_challan_detail', project_id=project_id, dc_id=dc_id)

    project = _active_project(project_id)
    profile = request.user.profile

    # PM isolation: PMs can only interact with their own projects
    # 0.2 lockdown: project scope for EVERY role, not just PM. This was a PM-only
    # guard, so Project Coordinator, Site Engineer and Design reached any project in
    # the portfolio. Role and authority checks elsewhere in this view are unchanged --
    # scope is a separate question and gets its own answer.
    if not user_can_view_project(request.user, project):
        raise Http404

    # Cross-project guard: DC must belong to the project in the URL
    challan = get_object_or_404(DeliveryChallan, pk=dc_id, project__is_deleted=False)
    if challan.project.project_id != project_id:
        raise Http404
```

Route (`projects/urls.py`):

```python
    # Raise issue against a specific DC — all roles with project access
    path('projects/<str:project_id>/delivery-challans/<int:dc_id>/issues/create/',
         views.create_delivery_issue, name='create_delivery_issue'),
```

**Which roles it admits.** There is **no `@role_required`**. The only gate is
`user_can_view_project(request.user, project)`. From `projects/permissions.py`:

- portfolio-wide, always True: `PORTFOLIO_VIEW_ROLES = frozenset({'CEO', 'Finance', 'SCM', 'Admin'})`,
  plus System Admin, Design Head (`is_design_head` flag or role string), and Sales & BD (own branch)
- assignment-based: PM / Project Coordinator (assigned or coordinating), **Site Engineer (holds a
  task on this project)**, Design (`assigned_design` or holds a task)

So: **SCM admitted. Site Engineer admitted when they hold a task on the site. PM admitted.**
Also admitted, with no screen that offers the control: CEO, Finance, Admin, System Admin, Design
Head, Sales & BD.

**Does the docstring match the decorator?** Yes, literally — "all roles with project access; PM
isolation applies" is exactly what the in-body `user_can_view_project` check enforces, and the
`@login_required` decorator adds nothing beyond authentication. The docstring's first line names
`create_task_issue` as the pattern, which is accurate. The one thing the docstring does *not* say
is that this endpoint is **wider than the page it lives on**: `delivery_challan_detail` gates on
`profile.role not in ('SCM','PM','Project Coordinator','Site Engineer','Admin')` → 403, but
`create_delivery_issue` has no such role list. A Finance or CEO user who cannot open the DC page can
still POST to this URL successfully. Recorded, not fixed.

---

## DI-2 — Reachability

`create_delivery_issue` appears in exactly one template and is constructed as a raw string in one
view.

**1. `projects/templates/projects/delivery_challan_detail.html` — the SE's own GRN screen.**

The button, with its surrounding condition verbatim:

```django
        {# Raise Issue — available to SE and SCM who are actively receiving/managing this DC #}
        {% if role == 'Site Engineer' or role == 'SCM' or role == 'PM' or role == 'Project Coordinator' or role == 'Admin' %}
          <button type="button" class="btn btn-outline-danger btn-sm"
                  data-bs-toggle="modal" data-bs-target="#raiseDcIssueModal">
            Raise Issue
          </button>
        {% endif %}
```

The form it opens:

```django
      <form method="post" action="{% url 'create_delivery_issue' project.project_id challan.pk %}">
        {% csrf_token %}
        ...
          <p class="text-muted small mb-3">
            This issue will be linked to DC {{ challan.dc_number }} for {{ project.project_id }}.
            Use this for delivery problems: damaged goods, quantity shortfall, wrong items, etc.
          </p>
```

The modal is rendered **unconditionally** (outside any `{% if %}`); only the button that opens it is
role-gated. This is the same page that carries the SE's GRN form:

```django
{# ── SE GRN confirmation form — SE only, not Received status ─────────────── #}
{% if role == 'Site Engineer' and challan.status != 'Received' %}
```

**Verified live** (rolled back): SE `GET` of the DC detail page → `200`, `'raiseDcIssueModal' in
body: True`, GRN form present: `True`. **So yes — there is a route from the GRN screen the SE
actually uses. It is the same screen, one button away from the GRN form.**

The SE reaches that screen from `project_overview` (`{% url 'delivery_challan_detail' project.project_id dc.pk %}`,
twice — the DC number link and a View button) and from `my_documents.html`.

**2. `projects/templates/dashboard/scm.html` — the SCM's dashboard.** Not a `{% url %}`; the target
is computed in `dashboard_scm` and passed as a data attribute:

```python
        # raise_issue_url: scope to the most recent pending DC if one exists
        latest_pending_dc = (pending_challans[0] if pending_challans
                             else (challans[0] if challans else None))
        if latest_pending_dc:
            raise_issue_url = (f'/projects/{pid}/delivery-challans/'
                               f'{latest_pending_dc.pk}/issues/create/')
        else:
            raise_issue_url = f'/projects/{pid}/issues/create/'
```

```django
        <div class="col-6 col-sm-3">
          <button type="button"
                  class="btn btn-outline-danger btn-sm w-100 js-scm-raise-issue"
                  data-project-id="{{ row.project.project_id }}"
                  data-issue-url="{{ row.raise_issue_url }}">
            <i class="bi bi-flag me-1"></i>Raise Issue
          </button>
        </div>
```

Note this silently falls back to the **project-level** `create_project_issue` endpoint when the
project has no challan at all — the SCM button is not always a *delivery* issue.

**Raw-URL-only for some role?** Yes. CEO, Finance, System Admin, Design Head and Sales & BD pass
`user_can_view_project` and would be accepted by the endpoint, but no template renders a control for
them, and `delivery_challan_detail` 403s them, so they have no way to discover it. Not a pilot
concern; recorded.

---

## DI-3 — What it writes

The `Issue.objects.create(...)` call, verbatim, and the field parsing above it:

```python
    title       = request.POST.get('title', '').strip()
    description = request.POST.get('description', '').strip()
    severity    = request.POST.get('severity', Issue.MEDIUM)
    due_date_s  = request.POST.get('due_date', '').strip()
    assignee_id = request.POST.get('assigned_to', '').strip()

    if not title:
        messages.error(request, 'Issue title is required.')
        return redirect('delivery_challan_detail', project_id=project_id, dc_id=dc_id)

    if severity not in dict(Issue.SEVERITY_CHOICES):
        severity = Issue.MEDIUM

    due_date = None
    if due_date_s:
        try:
            due_date = date.fromisoformat(due_date_s)
        except ValueError:
            pass

    assigned_to = None
    if assignee_id:
        try:
            assigned_to = UserProfile.objects.get(pk=assignee_id)
        except UserProfile.DoesNotExist:
            pass

    with transaction.atomic():
        issue = Issue.objects.create(
            project=project,
            delivery_challan=challan,
            task=None,
            title=title,
            description=description,
            severity=severity,
            status=Issue.OPEN,
            raised_by=profile,
            assigned_to=assigned_to,
            due_date=due_date,
        )
        record_transition(
            issue, to_status=Issue.OPEN, actor=profile,
            reason_code=REASON_CREATED, remark=title,
        )
```

Fields the view sets: `project`, `delivery_challan`, `task` (explicitly `None`), `title`,
`description`, `severity`, `status`, `raised_by`, `assigned_to`, `due_date`.

Fields it does **not** set, and their model defaults (`projects/models.py`, `class Issue`):

| field | default when unset |
|---|---|
| `raised_at` | `auto_now_add=True` — set at insert |
| `resolved_at` | `null=True, blank=True` → `None` |
| `closed_at` | `null=True, blank=True` → `None` |
| `resolution_note` | `blank=True, default=''` → `''` |

Explicit answers:

- **FK to the `DeliveryChallan`?** **Yes.** `Issue.delivery_challan` is a real FK:
  ```python
    delivery_challan = models.ForeignKey(  # Null unless the issue was raised directly against a specific delivery
        'DeliveryChallan', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='issues',
    )
  ```
  Note `on_delete=models.SET_NULL` — deleting the challan orphans the issue rather than removing it.
- **FK to a specific `DCLineItem`?** **No. Challan-level only.** `Issue` has no line-item FK, no
  quantity field, and no shortfall amount. **A shortfall is per line; the issue is per challan. This
  is a real mismatch and the pilot script has to work around it** — "which item, how many short" can
  only be carried in the free-text `title` / `description`. Note the placeholder in the modal is
  already doing that work by convention: `placeholder="e.g. 2 solar panels arrived cracked"`.
- **FK to a `Task` or a `Project`?** `task=None`, always and explicitly — a delivery issue is never
  task-linked. `project` **yes**, set to the URL project (`on_delete=models.CASCADE`). The project FK
  is what makes it visible on project-scoped screens; see DI-6.
- **`severity` and `status` at creation, and who chose them.** `status` is **`Issue.OPEN`, chosen by
  the view** — hard-coded, not posted, not from a form (it happens to equal the model default too).
  `severity` is **chosen by the submitter**, from the POST body, defaulting to `Issue.MEDIUM` when
  absent or not in `SEVERITY_CHOICES`. There is **no Django `Form` class anywhere in this path** —
  `request.POST.get` throughout, hand-validated. The DC-detail modal offers Low/Medium/High/Critical
  with Medium pre-selected.

Two things this parsing does that are worth recording:

1. `assigned_to` is resolved with a bare `UserProfile.objects.get(pk=assignee_id)` — **any active or
   inactive profile in the company**, with no check that they have a relationship to the project.
   The DC-detail modal populates its dropdown from `delivery_challan_detail`'s context, which is
   `UserProfile.objects.select_related('user').filter(is_active=True)` — the whole company directory.
   Both diverge from the `_issue_assignable_profiles(project)` narrowing that the 0.2 lockdown
   applied to `issue_detail`/`assign_issue`. Recorded, not fixed.
2. The modal marks "Assign To" as required in JS only (`dcIssueAssignError`); the view accepts an
   unassigned issue silently. Confirmed in DI-5: the SCM POST without `assigned_to` produced
   `assigned_to = None`.

---

## DI-4 — Does a shortfall do anything on its own?

## Does a shortfall create anything automatically — **NO**

Traced in full. `confirm_grn` (`@login_required`, `@role_required(['Site Engineer'])`) loops the line
items and writes only receipt data:

```python
        item.received_quantity = received_qty
        item.damaged_quantity  = damaged_qty
        item.condition         = derived_condition
        item.grn_date          = today
        item.grn_confirmed_by  = profile
        item.grn_notes         = grn_notes
        item.save()

    # Recalculate DC status ONCE after all line items saved — never inside the loop
    # (calling it inside the loop causes status to oscillate incorrectly).
    # actor/reason feed the state ledger; recalculate_dc_status owns the row (R-2).
    recalculate_dc_status(challan, actor=profile, reason_code=REASON_GRN_CONFIRMED)
    challan.refresh_from_db()

    log_activity(
        project, profile,
        f"SE confirmed GRN for DC {challan.dc_number} — {challan.status}",
        entity_type='DeliveryChallan', entity_id=challan.pk,
    )
```

`recalculate_dc_status` (`projects/models.py`) rolls the per-line severity up to a DC status and
writes one ledger row when the status actually moves:

```python
def _dc_item_severity(received_qty, ordered_qty, damaged_qty):
    if received_qty is None:
        return None  # Not yet confirmed — excluded from rollup
    if received_qty == 0:
        return 'red'   # Nothing arrived
    if received_qty < ordered_qty and damaged_qty > 0:
        return 'red'   # Two stacked problems
    if received_qty >= ordered_qty and damaged_qty == 0:
        return 'green'
    return 'amber'  # Shortfall only, or full qty with some damage
```

Neither function contains the string `Issue`, imports it, or calls `send_notification`. The only
imports inside `recalculate_dc_status` are `from .utils import record_transition`.

**Verified live** (rolled back), OPEX site `MS019`, DC ordered 10 / received 6 / damaged 2:

```
DC status after short+damaged GRN: Rejected
DI-4 Issues created by the shortfall: 0
DI-4 Notifications created by the shortfall: 0
StatusTransition rows written by the GRN: 1
   LEDGER subject=delivery_challan id=21 'Expected' -> 'Rejected' reason='grn_confirmed' actor=abhishek role=Site Engineer remark=''
DI-4 ActivityLog rows from GRN: [('SE confirmed GRN for DC AUDIT-DI-1 — Rejected', '')]
```

**What a shortfall does produce instead**, and where it becomes visible:

| written | where it surfaces |
|---|---|
| `DCLineItem.received_quantity`, `.damaged_quantity`, `.condition`, `.grn_date`, `.grn_confirmed_by`, `.grn_notes` | the line-items table on `delivery_challan_detail`, as a Partial/Damaged/Good badge |
| `DeliveryChallan.status` → `Partially Received` (shortfall **or** damage alone) or `Rejected` (shortfall **and** damage, or nothing received) | DC badge on `project_overview`, `my_documents`, the SCM dashboard material summary, and `_project_material_badge` on PM cards |
| one `StatusTransition` row (`subject_type='delivery_challan'`, `reason_code='grn_confirmed'`, `remark=''`) | the state ledger only |
| one `ActivityLog` row, `entity_type='DeliveryChallan'`, **`action_code=''`** | the project timeline / recent-activity feed |

Note `'Rejected'` here means *severe delivery failure*, not a refused consignment — the model
comment says so, and `tests_residential_baseline` pins it. Nobody is notified, nothing is assigned,
and nothing appears in any Issue list. **A shortfall is a colour on a badge until a human types a
title into the Raise Issue modal.**

---

## DI-5 — Who can raise it. Exercised, both roles.

Setup, all inside one rolled-back `transaction.atomic()`: the first Draft OPEX site with an assigned
PM (`MS019`) was activated through the real endpoint `opex_site_activate` (302, 7 phases, 23 tasks),
one of its tasks was assigned to a Site Engineer, a `DeliveryChallan` + one `DCLineItem`
(ordered 10) were created, and the SE confirmed a GRN of received 6 / damaged 2 → DC `Rejected`.

Note on user selection: the first SE profile returned by `role='Site Engineer', is_active=True` has
`auth.User.is_active = False`, so `force_login` produced a session that
`AuthenticationMiddleware` discards — every request came back `302 → /login/`. `UserProfile.is_active`
and `User.is_active` are separate flags and disagree in this database. The run below filters on
`user__is_active=True`. **This is a live pilot hazard: some Site Engineer profiles marked active
cannot log in.**

Raw output:

```
BEFORE: {'Issue': 29, 'Notification': 1089, 'NotificationLog': 5469, 'ActivityLog': 2027,
         'DeliveryChallan': 6, 'DCLineItem': 14, 'Project_active': 40, 'ProjectPhase': 333, 'Task': 1861}
site=MS019 pm=nirankar scm=subhash se=abhishek se_stranger=dipesh
activate -> 302 status: Active phases: 7 tasks: 23
task holder set: Design -> abhishek
  user_can_view_project(subhash/SCM) = True
  user_can_view_project(abhishek/Site Engineer) = True
  user_can_view_project(dipesh/Site Engineer) = False
  user_can_view_project(nirankar/PM) = True
DC created pk 18 status Expected
confirm_grn -> 302 | DC status: Rejected | recv: 6.00 dmg: 2 cond: Partial
DI-4 Issues created by the shortfall: 0
DI-4 Notifications created by the shortfall: 0

--- DI-5a: SCM POSTs create_delivery_issue ---
status: 302 redirect: /projects/MS019/delivery-challans/18/ | issues + 1

--- DI-5b: Site Engineer (holds a task) POSTs create_delivery_issue ---
status: 302 redirect: /projects/MS019/delivery-challans/18/ | issues + 1

--- DI-5c: Site Engineer with NO task on this site ---
status: 404 | body: b'<!DOCTYPE html>\n<html lang="en">\n<head>\n  <meta http-equiv="content-type" ...<title>P'
       | issues + 0
```

Every field of both `Issue` rows:

```
   37 id                 = 37
   37 project            = 238
   37 task               = None
   37 delivery_challan   = 18
   37 title              = 'AUDIT SCM shortfall'
   37 description        = '4 short 2 damaged'
   37 severity           = 'High'
   37 status             = 'Open'
   37 raised_by          = 3          (subhash, SCM)
   37 assigned_to        = None
   37 raised_at          = datetime.datetime(2026, 9, 6, 14, 38, 22, 409535, tzinfo=datetime.timezone.utc)
   37 due_date           = None
   37 resolved_at        = None
   37 closed_at          = None
   37 resolution_note    = ''
   ---
   38 id                 = 38
   38 project            = 238
   38 task               = None
   38 delivery_challan   = 18
   38 title              = 'AUDIT SE shortfall'
   38 description        = 'raised at GRN by SE'
   38 severity           = 'Critical'
   38 status             = 'Open'
   38 raised_by          = 8          (abhishek, Site Engineer)
   38 assigned_to        = 3          (subhash, SCM)
   38 raised_at          = datetime.datetime(2026, 9, 6, 14, 38, 22, 524499, tzinfo=datetime.timezone.utc)
   38 due_date           = None
   38 resolved_at        = None
   38 closed_at          = None
   38 resolution_note    = ''
   ---
```

Rollback confirmation:

```
*** rolled back ***
AFTER:  {'Issue': 29, 'Notification': 1089, 'NotificationLog': 5469, 'ActivityLog': 2027,
         'DeliveryChallan': 6, 'DCLineItem': 14, 'Project_active': 40, 'ProjectPhase': 333, 'Task': 1861}
MATCH: True
```

A second, independent walkthrough (`walk2`) produced the same result and also rolled back clean
(`MATCH: True`).

## Can the Site Engineer raise a delivery issue — **YES**

**How the refused SE is refused:** the SE with no task on the site gets **404** — `raise Http404`
from the `user_can_view_project` guard, rendering the project's standard 404 page. Not a 403, not a
bare `HttpResponseForbidden`, no message. That is the correct shape for a scope failure (it does not
confirm the DC exists), but it means a genuinely-assigned SE whose task assignment is missing will
see "not found" rather than anything actionable. Worth a line in the pilot script.

---

## DI-6 — Who sees it afterwards

Verified live by GET-ing each screen as each role inside the same rolled-back transaction and
string-matching both issue titles in the rendered HTML:

```
  SCM                GET /projects/MS019/delivery-challans/18/   -> 200 | SE-issue shown: True | SCM-issue shown: True
  Site Engineer      GET /projects/MS019/delivery-challans/18/   -> 200 | SE-issue shown: True | SCM-issue shown: True
  PM                 GET /projects/MS019/delivery-challans/18/   -> 200 | SE-issue shown: True | SCM-issue shown: True
  PM                 GET /projects/MS019/overview/               -> 200 | SE-issue shown: True | SCM-issue shown: True
  Site Engineer      GET /projects/MS019/overview/               -> 200 | SE-issue shown: True | SCM-issue shown: True
  SCM                GET /dashboard/scm/                         -> 200 | SE-issue shown: True | SCM-issue shown: True
  PM                 GET /dashboard/pm/                          -> 200 | SE-issue shown: True | SCM-issue shown: True
  Site Engineer      GET /dashboard/site-engineer/               -> 200 | SE-issue shown: False | SCM-issue shown: False
```

Screen by screen, with the selecting queryset:

**1. `delivery_challan_detail` — YES, and it names the DC.** Roles: SCM, PM, Project Coordinator,
Site Engineer, Admin, each scoped by `user_can_view_project`.

```python
    # Issues raised against this specific DC — shown below line items for visibility
    dc_issues = (
        Issue.objects.filter(delivery_challan=challan)
        .select_related('raised_by__user', 'assigned_to__user')
        .order_by('-raised_at')
    )
```

Rendered as a `Delivery Issues` card (title, severity, status, raised-by, date, View → `issue_detail`).
Note it is **unfiltered by status** — Closed delivery issues stay on the card forever.

**2. `project_overview` — YES, but as project issues, not delivery issues.**

```python
    project_issues = (
        Issue.objects.filter(project=project)
        .select_related('raised_by__user', 'assigned_to__user', 'task')
    )
```

No `delivery_challan` term, no status filter — the delivery issue appears mixed in with every other
issue on the project and the challan is not shown. The SE reaches this page (confirmed 200 above).

**3. `dashboard_scm` — YES, dedicated "Open Delivery Issues" card, portfolio-wide.**

```python
    # Delivery issues: open/in-progress issues linked to DCs on active SCM-tracked projects
    # SCM scope: all active projects (SCM is not PM-scoped; it sees all active projects)
    delivery_issues = (
        Issue.objects.filter(
            delivery_challan__project__status__in=['Active', 'In Progress'],
            status__in=[Issue.OPEN, Issue.IN_PROGRESS],
            **_context_filter(ctx, 'delivery_challan__project__'),
        )
        .select_related('project', 'delivery_challan', 'raised_by__user')
        .order_by('-raised_at')[:20]
    )
```

Capped at 20 and restricted to `Active`/`In Progress` projects. This is the best delivery-issue
surface in the product — it links to both `issue_detail` and `delivery_challan_detail`.

**4. `dashboard_pm` — YES, per project card, but only for the PM's own projects.**

```python
    # Delivery issues — one batch query grouped by project, attached to each row
    if projects_with_progress:
        _all_dc_issues = list(
            Issue.objects.filter(
                delivery_challan__project_id__in=managed_project_ids,
                status__in=[Issue.OPEN, Issue.IN_PROGRESS],
            )
            .select_related('project', 'delivery_challan', 'raised_by__user')
            .order_by('-raised_at')
        )
```

**5. `dashboard_site_engineer` — NO, not as an issue.** The SE dashboard has no delivery-issue
query at all. It has one project-level count:

```python
        issue_count=Count(
            'issues',
            filter=Q(issues__status__in=[Issue.OPEN, Issue.IN_PROGRESS]),
            distinct=True,
        ),
```

rendered only as a number:

```django
      {% if p.issue_count > 0 %}
      <span class="badge rounded-pill" style="background:#243a6b;">
        {{ p.issue_count }} Issue{{ p.issue_count|pluralize }}
      </span>
      {% endif %}
```

The count **does** include delivery issues — there is no `delivery_challan__isnull` term — verified
read-only against the one delivery-linked Issue already in the database:

```
existing delivery-linked Issue: 20 "only 1 solar panel recieved in good usable condition out of 5 dispatched"
  | project: HRP-RES-2026-003 | status: Open | task: None
SE-dashboard annotation for that project (project_id, issue_count): ('HRP-RES-2026-003', 4)
all open/in-progress issues on that project:
  [(20, 'only 1 solar panel...', 2), (4, 'survey not submitted', None),
   (3, 'Engineer not available', None), (1, 'Issue 001', None)]
```

**So: the issue the SE raised reaches the SE only as a badge that increments from 3 to 4.** No
title, no link, nothing that says it was a delivery problem. To see it, the SE must navigate to the
project overview or back to the DC. There is no global issue list view in `urls.py` — the only
issue-shaped URLs are `/issues/<id>/` and its actions.

**6. `issue_detail`, and the in-app notification.** The SE-raised issue notified two people
(DI-8), each with a `/issues/38/` link. `issue_detail` is gated by `user_can_view_project`, so the
raiser, the assignee, the PM and the coordinators can all open it. **`raised_by` is never used as a
visibility term anywhere** — the SE sees it because they hold a task on the site, not because they
raised it.

**Summary against the question asked:** it reaches the SCM user (dedicated card), the PM (dedicated
card + overview + DC page), and the SE (DC page and project overview if they navigate there;
in-app notification only if they were assigned). It reaches the SE's *dashboard* as a number, not
as an issue.

---

## DI-7 — Closure

There is one closure view, `close_issue`, shared by every issue type — no delivery-specific path.

```python
@login_required
def close_issue(request, issue_id):
    """
    Close a Resolved issue. Only the project PM can close.
    filter().update() used to prevent race condition on concurrent status changes.
    Access: project PM only. POST only.
    """
    ...
    if not _is_project_pm(profile, project):
        return HttpResponseForbidden('Only the project PM can close issues.')

    if issue.status != Issue.RESOLVED:
        messages.warning(request, 'Issue must be Resolved before it can be closed.')
        return redirect('issue_detail', issue_id=issue_id)
```

`_is_project_pm` is `profile.role in ('PM', 'Project Coordinator') and user_can_manage_project(profile.user, project)`
— **the assigned PM or a coordinator on that specific project. Not SCM, not the SE, not Admin.**

The full ladder, and what each rung requires:

| step | view | gate | requires |
|---|---|---|---|
| Open → In Progress | `update_issue_status` | `user_can_view_project` | an assignee must already be set; refuses if `assigned_to is None` |
| In Progress → Resolved | `resolve_issue` | `user_can_view_project` | **a non-blank `resolution_note`** — refused with `messages.error` otherwise |
| Resolved → Closed | `close_issue` | `_is_project_pm` | nothing but the role + ownership; **no note, no assignment** |
| Resolved → Open | `reopen_issue` | `_is_project_pm` | PM only; clears `resolved_at` and `resolution_note` |

Walked live on the SE-raised issue (rolled back):

```
  Site Engineer      update_issue_status  -> 302 | status now In Progress | body b''
  SCM                resolve_issue        -> 302 | status now Resolved    | body b''
  Site Engineer      close_issue          -> 403 | status now Resolved    | body b'Only the project PM can close issues.'
  PM                 close_issue          -> 302 | status now Closed      | body b''
```

The SE's refusal at close is a bare `HttpResponseForbidden` with a plain-text body — no styled page,
no redirect. In the browser the SE would see the string "Only the project PM can close issues." on a
white page.

**Any automatic close, e.g. by a later corrected GRN?** **No.** Verified: with an Open delivery issue
on the challan, SCM ran `override_grn` correcting the line to received 10 / damaged 0, which moved the
DC all the way back to `Received`:

```
after corrected GRN -> DC status: Received | issue status: Open | resolved_at: None | closed_at: None
```

Nothing in `override_grn`, `confirm_grn` or `recalculate_dc_status` reads or writes `Issue`. A
delivery issue outlives the delivery problem that caused it until a human resolves and a PM closes.

---

## DI-8 — Notifications

**Read-only. No notification was fired** — `projects.notifications.requests.post` and
`_zeptomail_post` were patched out for the whole run; the counters confirm zero real sends
(`outbound whatsapp HTTP attempts: 1` — that one was the harness intercepting a call and raising, so
the request never left the process; `zeptomail attempts: 0`).

`create_delivery_issue` calls `send_notification()` on two paths.

**Path 1 — the assignee**, only when an assignee was chosen and is not the raiser:

```python
    if assigned_to and assigned_to != profile:
        ...
        send_notification(
            recipient=assigned_to,
            message=_ic_email_message,
            channels=['in_app', 'whatsapp', 'email'],
            link=_ic_link,
            subject=f'New Issue Raised — {project.customer_name}',
            template='issue_created',
            template_params=[project.customer_name, recipient_name, project.customer_name, raiser_name],
            related_project=project,
            actor=profile,
        )
```

**Path 2 — every project manager**, in-app only:

```python
    # Notify every project manager (PM + coordinators), skipping the raiser and the
    # assignee (who are notified through other paths). project_managers() dedupes.
    for _mgr in project_managers(project):
        if _mgr != profile and _mgr != assigned_to:
            send_notification(
                recipient=_mgr,
                message=f'Issue "{title}" raised on {project.project_id} — {project.customer_name}.',
                channels=['in_app'],
                link=f'/issues/{issue.pk}/',
                related_project=project,
                actor=profile,
            )
```

- **Template name:** `issue_created` (assignee path only; the PM path passes no template because
  in-app needs none).
- **Channels:** assignee → `in_app`, `whatsapp`, `email`. PMs/coordinators → `in_app` only.
- **Recipients:** the assignee (if any, and not the raiser), plus every PM/coordinator on the
  project except the raiser and the assignee. **The SE who raised or observed the shortfall is
  notified only if someone assigns the issue to them.**

Observed, from the run where the SE raised and assigned to SCM (`subhash`), PM is `nirankar`:

```
  IN_APP -> nirankar | Issue "AUDIT SCM shortfall" raised on MS019 — MPUVNL. | /issues/37/
  IN_APP -> subhash  | A new issue has been raised on project MPUVNL: "AUDIT SE shortfall". ... | /issues/38/
  IN_APP -> nirankar | Issue "AUDIT SE shortfall" raised on MS019 — MPUVNL. | /issues/38/
  LOG in_app   sent    -> nirankar | tmpl:
  LOG in_app   sent    -> subhash  | tmpl: issue_created
  LOG whatsapp failed  -> subhash  | tmpl: issue_created
  LOG email    skipped -> subhash  | tmpl: issue_created
  LOG in_app   sent    -> nirankar | tmpl:
```

(`whatsapp failed` and `email skipped` are the harness and this recipient's own per-user preference,
not the product.)

**Would it actually send in a local pilot?** **Yes, WhatsApp and email both — and this is a
hazard.** Read from the local DB: `SystemSettings.whatsapp_enabled = True`,
`SystemSettings.email_enabled = True`. `INTERAKT_API_KEY` and `ZEPTOMAIL_API_KEY` are both read from
the local `.env` and both are live keys. `send_notification` gates on, in order: the master switch,
then `recipient.whatsapp_notifications` / `recipient.email_notifications`, then a configured API key.
With the switches on and live keys present, **assigning a delivery issue to a real staff profile
during a local pilot will send that person a real WhatsApp message and a real email.** The
`email skipped` line above was that one recipient's personal preference being off — not a safety net.
Either patch the senders, point the assignee at a throwaway profile, or turn the master switches off
before the pilot. Recorded, not changed.

---

## DI-9 — Tests

**The premise that `create_delivery_issue` has never been exercised is not quite right — there is
exactly one test, and it passes.** It covers the FK and the cross-project guard, and nothing else.

`projects/tests_residential_baseline.py`, class `IssueLifecycleTests`:

```python
    def test_a_delivery_issue_links_to_its_challan_and_is_cross_project_guarded(self):
        challan = self._make_dc(self.project_a, 'DC-ISSUE')
        _client_for(self.pm_a).post(
            reverse('create_delivery_issue',
                    args=[self.project_a.project_id, challan.pk]),
            {'title': 'Short delivery', 'severity': Issue.MEDIUM},
        )
        issue = Issue.objects.get(title='Short delivery')
        self.assertEqual(issue.delivery_challan, challan)

        other = self._make_dc(self.project_b, 'DC-B-ISSUE')
        response = _client_for(self.pm_b).post(
            reverse('create_delivery_issue',
                    args=[self.project_a.project_id, other.pk]),
            {'title': 'cross project', 'severity': Issue.LOW},
        )
        self.assertEqual(response.status_code, 404)
```

The actor is the **PM**. There is **no test in which an SCM or a Site Engineer raises a delivery
issue**, no test that a delivery issue is assignable, notifies anyone, or is closable, and no test
that connects a shortfall to an issue at all.

**GRN shortfall path — covered, and covered well**, same file, class `DeliveryGRNWorkflowTests`:

- `test_a_shortfall_alone_rolls_up_to_partially_received` — received 7 of 10, damaged 0 → `condition`
  reads `Good` (derived from damage alone) while the DC reads `Partially Received`; the disagreement
  is asserted deliberately with a comment explaining it
- `test_a_shortfall_with_damage_rolls_up_to_rejected` — received 6 of 10, damaged 2 → `Rejected`
- `test_a_confirmed_dc_from_another_project_is_unreachable_through_this_url` — 404
- `test_scm_overrides_a_grn_without_overwriting_the_original_engineer` — `grn_confirmed_by` preserved

None of these assert anything about `Issue` — which is consistent with DI-4's "no".

Also touching `confirm_grn`: `tests_access_isolation.py` (cross-project 404 for a stranger SE),
`tests_soft_delete.py` (refuses a challan on a deleted project), `tests_status_transition.py` (the
ledger row).

**Delivery issue closure — no test.** `IssueLifecycleTests` covers the Open → In Progress → Resolved
→ Closed ladder and the PM-only close/reopen/assign gates, but always on a **project-level** issue
(`self._raise(client, self.project_a)` → `create_project_issue`), never on a delivery-linked one.

Suite run, unchanged working tree:

```
$ ./venv/Scripts/python.exe manage.py test projects.tests_residential_baseline --settings=solarpms.test_settings
Ran 92 tests in 60.106s

OK
```

---

## Answers on their own lines

**Does a shortfall create anything automatically — no**

**Can the Site Engineer raise a delivery issue — yes**

**Is the issue challan-level or line-level — challan-level.** `Issue.delivery_challan` is a real FK;
there is no `DCLineItem` FK, no quantity, no shortfall figure. Which line and how many short exist
only as free text in `title` / `description`.

---

## Anything broken, unfixed

Recorded, none acted on. Ordered by what would bite a pilot first.

1. **Some Site Engineer profiles cannot log in.** `UserProfile.is_active = True` while
   `auth.User.is_active = False` on at least one SE in the local database; `force_login` succeeds
   but every subsequent request redirects to `/login/`, because `AuthenticationMiddleware` discards
   the session for an inactive user. This cost the first walkthrough entirely. **Check the pilot
   SE's `auth.User.is_active`, not just the profile flag, before the demo.**
2. **Live notification keys with both master switches ON locally.** `SystemSettings.whatsapp_enabled`
   and `.email_enabled` are both `True` in `solarpms_local`, and `.env` carries live Interakt and
   ZeptoMail keys. Assigning a delivery issue to a real profile during a local pilot sends a real
   WhatsApp and a real email to that person. Already documented as a hazard in the project's memory;
   restating because this specific endpoint is a sender.
3. **The issue is challan-level while the shortfall is per line.** The product-owner decision "a
   shortfall creates a delivery issue" cannot currently be satisfied per line. The pilot script must
   have the SE type the item and quantity into the title.
4. **`create_delivery_issue` accepts any `UserProfile` as `assigned_to`,** via a bare
   `UserProfile.objects.get(pk=assignee_id)` with no project-relationship check and no `is_active`
   check — including inactive profiles. The DC-detail dropdown that feeds it is the entire company
   directory (`UserProfile.objects.filter(is_active=True)` in `delivery_challan_detail`). Both
   diverge from the 0.2 lockdown's `_issue_assignable_profiles(project)`, which narrowed exactly this
   for `issue_detail` and re-validated it in `assign_issue`. The dropdown was narrowed on one screen
   and not the other.
5. **The endpoint is wider than the page it lives on.** `delivery_challan_detail` returns 403 for any
   role outside `('SCM','PM','Project Coordinator','Site Engineer','Admin')`, but
   `create_delivery_issue` has no role gate — CEO, Finance, System Admin, Design Head and Sales & BD
   all pass `user_can_view_project` and can POST it by raw URL. No screen offers them the control.
6. **`override_grn` has no project-scope guard.** It is `@role_required(['SCM'])` plus the
   cross-project DC guard, but unlike its sibling `confirm_grn` it never calls
   `user_can_view_project`. SCM is portfolio-wide so the practical effect is nil today; it is an
   asymmetry between two functions that were otherwise written as mirrors, and it is the shape of
   the bug the 0.2 lockdown existed to remove. Out of scope for this audit — noted because the trace
   for DI-4 went through it.
7. **The DC-detail Delivery Issues card has no status filter.** `Issue.objects.filter(delivery_challan=challan)`
   returns Closed issues too, so a resolved-and-closed shortfall stays on the challan page
   indefinitely with a grey badge. Every other delivery-issue surface filters to Open/In Progress.
8. **`confirm_grn`'s `ActivityLog` row has an empty `action_code`.** `entity_type='DeliveryChallan'`
   is set but `action_code=''`, so a GRN confirmation is invisible to any query that filters on the
   machine-readable code (the EOD digest, for one). The `StatusTransition` ledger does record it.
9. **The SE dashboard shows a delivery issue only as a number.** No title, no link, no delivery
   context — the badge goes from N to N+1. The SE who raised the shortfall has no dashboard route
   back to it.
10. **The GRN ledger row carries an empty `remark`** even though `grn_notes` were supplied per line
    ("4 short, 2 cracked"). `recalculate_dc_status` accepts a `remark` parameter and both GRN callers
    pass only `reason_code`.

---

*Hard stop, pending sign-off. No build proposed, none written.*
