# Task Action Reload Elimination — Phase 1 Audit Findings

**Status:** Phase 1 (read-only audit). **No code was changed.**
**Scope:** Inventory every task-related action that currently causes a full page reload, so Prompt B can design one reusable HTMX partial pattern and Prompt C+ can roll it out without surprises.
**Method:** Traced `urls.py` → view → template for every task-action route; read each view function and rendering template directly; cross-referenced against the completed `user_can_manage_project()` normalization.

> Cross-reference only, **not merged**: `Project_Overview_Access_Isolation_Audit_Prompt.md`. Where this audit brushed access-isolation behavior (PM/SE project-level isolation), it is noted and left to that separate tracked finding.

---

## 0. Environment facts (confirmed for Prompt B)

| Item | Finding |
|---|---|
| Existing HTMX / hx-* usage | **None.** Grep of `templates/`, `static/`, `staticfiles/` returned zero `htmx` / `hx-post` / `hx-get` references. |
| Current front-end libs (CDN) | Bootstrap 5.3.3 (CSS+JS), Bootstrap Icons 1.11.3, Alpine.js 3.x (on *some* dashboards only), Lucide, Tailwind (admin/subadmin/login only). All via `cdn.jsdelivr.net` / `unpkg.com`. |
| CSP / `Content-Security-Policy` | **None present.** No `django-csp` in `MIDDLEWARE` (`solarpms/settings.py:62`), no CSP headers set. Adding the HTMX CDN `<script>` will **not** be blocked. |
| CSRF | Standard Django default — `django.middleware.csrf.CsrfViewMiddleware` (`settings.py:67`). Every POST form audited carries `{% csrf_token %}` (verified individually below). `CSRF_TRUSTED_ORIGINS` is env-driven (`settings.py:41`). **No CSRF changes needed** — HTMX can send the token via the standard hidden input or an `hx-headers` `X-CSRFToken`. |
| Alpine-rule impact | HTMX is a separate library doing a separate job (server round-trips), orthogonal to the Alpine-for-UI-state rule. No conflict. |

**Conclusion:** CDN-only HTMX is compatible. No build step, no CSP allow-listing, no CSRF reconfiguration required.

---

## 1. Full call-site inventory

Every **live** task-action call site. `_pm_owns_project()` is a thin adapter over the canonical `user_can_manage_project()` (`views.py:1609–1615`), so **every site listed as using `_pm_owns_project` already routes through the normalized helper** — none do raw `assigned_pm` / role-string comparisons.

### Legend
- **Response type**: `redirect` = HTTP 302 → full page load; the reload we are eliminating.
- **Perm location**: `view` = enforced server-side in Python; `template` = UI-gated by `{% if %}`; `both` = gated in template *and* enforced in view.

| # | View fn (file:line) | URL name / pattern | Rendered by (template : line) | UI type | Response type | Perm location | Roles that can trigger today | Uses `user_can_manage_project()` | CSRF | Program-hierarchy risk |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | `task_status_update` (`views.py:1898`) | `task_status_update` — `projects/<id>/tasks/<tid>/update/` | `project_overview.html:857` | POST form (`<select onchange=submit>`) | `redirect` (+ `?next=` honored; `JsonResponse 400` if unassigned; `403` if wrong role) | **both** | Task's `assigned_role` user **or** PM/Coordinator | **yes** (via `_pm_owns_project`) | ✔ (L858) | None — task→`phase__project` scoped; no Program assumption |
| 2 | `task_detail_status_update` (`views.py:2109`) | `task_detail_status_update` — `.../detail-status/` | `task_detail.html:26` | POST form (`<select onchange=submit>`) | `redirect` to `task_detail` (`403` if not assignee) | **both** | **Assigned user only** (`task.assigned_to == profile`) | n/a (user-level, not PM-level) | ✔ (L28) | None |
| 3 | `task_assign` (`views.py:2283`) | `task_assign` — `.../assign/` | `project_overview.html` assign modal `#assignForm` (action built in JS, `openAssignModal` L1342–1354) **+** standalone `task_assign_form.html` (GET renders the form page) | POST (modal) / 2-step standalone page | `redirect` to `project_overview` | **both** | PM/Coordinator only (`@role_required(['PM','Project Coordinator'])` + `_pm_owns_project`) | **yes** | ✔ (modal L1171 area; form L30) | None |
| 4 | `task_assign_design_head` (`views.py:2356`) | `task_assign_design_head` — `.../assign-design/` | `project_overview.html:849` (GET `<a href>` → standalone `task_assign_form.html`) | **GET link → page**, then POST | `redirect` to `project_overview` | **both** | `is_design_head` flag, Design-role tasks only | no (uses `is_design_head`, by design) | ✔ (form L30) | None |
| 5 | `task_set_due_date` (`views.py:2426`) | `task_set_due_date` — `.../due-date/` | `project_overview.html:898` (PM) & `:908` (non-PM) | POST form (`<input date onchange=submit>`) | `redirect` to `project_overview` | **both** | PM/Coordinator (any task, cascades) **or** role-owner when cascade OFF | **yes** | ✔ (L899, L909) | None — but see §6 (cascade note) |
| 6 | `task_add` (`views.py:1858`) | `task_add` — `.../tasks/add/` | `project_overview.html:52` (GET `<a href>` → standalone `task_add_form.html`) | **GET link → page**, then POST | `redirect` to `project_overview` | **both** | PM/Coordinator, Active projects only | **yes** | ✔ (form L20) | None |
| 7 | `upload_task_attachment` (`views.py:4576`) | `upload_task_attachment` — `.../attachments/upload/` | `task_detail.html:96` **and** `project_overview.html:1217` (`#quickAttachForm`, action set in JS, `?next=` hidden) | POST multipart | `redirect` (+ `?next=` honored) | **view** (any project-access role; PM-isolation only) | All roles with project access | **yes** (PM-isolation branch) | ✔ (L98 / L1218 area) | None |
| 8 | `delete_task_attachment` (`views.py:4664`) | `delete_task_attachment` — `.../attachments/<pk>/delete/` | `task_detail.html:149` | POST form | `redirect` (+ `?next=` honored) | **both** | Uploader **or** Admin | **yes** (PM-isolation branch) | ✔ (L152) | None |
| 9 | `create_task_comment` (`views.py:5272`) | `create_task_comment` — `.../comments/create/` | `task_detail.html:369` (reply) & `:449` (new) | POST form | `redirect` to `task_detail` | **view** (login only; not per-role gated — by design, all roles comment) | All roles with project access | n/a | ✔ (L370 / L450) | None |

### Notes on the inventory
- **#1 mixed response type:** `task_status_update` returns a `JsonResponse 400` for the unassigned-task case (`views.py:1913–1917`) and `HttpResponseForbidden`/`403` for the wrong-role case, but the **happy path is a 302 redirect**. When migrating to `hx-post`, the JSON branch and the redirect branch will both need an HTMX-aware response shape.
- **#3 `task_assign` has no `{% url %}` in markup:** the modal form's `action` is assembled as a raw string in JavaScript (`project_overview.html:1353` → `'/projects/'+projId+'/tasks/'+taskId+'/assign/'`). A `{% url 'task_assign' %}` grep misses it — the route is real and live.
- **#4 & #6 are two-step (GET page → POST):** these are the pencil-icon reuse from Design Head (#4) and Add-Task (#6). They navigate to a **standalone full page** (`task_assign_form.html` / `task_add_form.html`, both `{% extends 'base.html' %}`) and then POST. See §5 edge cases — their real reload count is **2 per action**, not 1.
- **#7 exists on two pages** (task detail + the project-overview quick-attach) but **both hit the same view** — no divergent second implementation.

---

## 2. Pattern-reuse assessment (for Prompt B)

**There is no shared task-row partial today. Every task-action block is inline.**

- **`{% include %}` for task-row/task-action markup: NONE.** Confirmed across `project_overview.html`, `task_detail.html`, `task_assign_form.html`, `task_add_form.html`, all eight role dashboards, `tasks/task_drill_down.html`, and `project_detail.html`. Zero includes pull task-row markup.
- The task table + row action forms live **inline in `project_overview.html`** (rows at L826–931; assign modal L1164–1194; quick-attach form L1217).
- `task_detail.html` inlines its own status/attachment/comment forms independently (no include, no Alpine — reveals use vanilla JS + Bootstrap `.d-none` toggles + Bootstrap modals).
- **Duplication exists in exactly one place — and it is dead code:** `project_detail.html` mirrors the `task_status_update`, `task_set_due_date`, `task_add`, and JS-built `task_assign` markup (L26, L197–214, L229–236, L456–505). **But `project_detail.html` is orphaned** — the `project_detail` view is now a pure `redirect('project_overview')` (`views.py:1667–1669`) and **no view renders that template** (grep: zero `render(...'project_detail.html')`). See §5.

**Implication for Prompt B:** the reusable HTMX partial must be *extracted from scratch* out of `project_overview.html`'s inline row markup — there is no existing include to hook into. The single duplicate (`project_detail.html`) should be **ignored/deleted, not migrated** (out of scope for this audit — flagged, not fixed).

### Dashboards do NOT duplicate task-action forms
All eight role dashboards (`admin`, `bd`, `ceo`, `design`, `finance`, `pm`, `scm`, `site-engineer`), the `_milestone_badge*` partials, and `tasks/task_drill_down.html` contain **zero** task-action forms. They surface tasks only as **plain GET navigation links** to `project_overview`, `task_detail`, or the aggregate `tasks_due_today` / `tasks_due_soon` / `tasks_overdue` list pages. The forms they *do* contain (raise-issue, raise-payment, activate-designer, milestone-invoice) are out of scope. **So there is no second live implementation of any task action to keep in sync** — the only live call sites are `project_overview.html`, `task_detail.html`, and the two standalone form pages.

---

## 3. Flagged: button-hidden-but-unenforced permission gaps

**Report-only — not fixed (per do-not-touch list).**

Good news first: **every task-action view enforces its permission server-side.** No task action is protected by template-only hiding with a missing server check. The gates are `both` (template + view) for #1–6 and #8, and deliberately open (`view`, login-only) for #7 and #9. So there is **no classic hidden-button-with-no-server-check hole** to inherit when these move to browser-visible `hx-post` endpoints.

Two lower-severity **divergences** worth Zuber's awareness (neither is an exploit today, both become visible in the Network tab once actions are `hx-post`):

1. **`task_set_due_date` — template stricter than server for Finance (`views.py:2441–2468` vs `project_overview.html:906`).**
   The template hides the non-PM due-date editor when `role == 'Finance'` or `'Admin'` (L906). The **server** for the non-PM branch only checks `task.assigned_role != user_task_role → PermissionDenied` — it does **not** exclude Finance. A Finance user could `POST` a due-date to a Finance-role task even though the UI hides the control. Impact is benign (editing one's own role's task date), but the layers disagree. Direction is *safe* (UI more restrictive than server), so not a leak — just an inconsistency to reconcile when the endpoint becomes directly callable.

2. **`task_status_update` disabled-select is client-only, but the server backs it (`project_overview.html:860` vs `views.py:1913`).**
   The `<select>` is rendered `disabled` when `not task.assigned_to`. That is purely cosmetic, **but** the view independently rejects unassigned tasks with `JsonResponse 400`. This is **correctly enforced** — listed here only to confirm it is *not* a gap.

> No action taken on either. Both are pre-existing and orthogonal to reload elimination.

---

## 4. Flagged: Program / Project hierarchy hardcoding risks

**Report-only — informational.**

- **No `Program` parent entity exists yet.** `OPEX` / `CAPEX` are values of `Project.PROJECT_TYPE_CHOICES` (`models.py:8–12`) — a *type field on Project*, not a parent Program record. The only `parent` FK in the models (`models.py:699`) is `Comment.parent` (self-referential reply threading), unrelated to program hierarchy.
- **No task-action view hardcodes a hierarchy assumption that would block a future `Program` parent.** All nine views resolve authority and scope through the `Task → phase → project` chain and `user_can_manage_project(user, project)`. When a `Program(parent, OPEX/CAPEX)` entity is introduced, authority logic would be extended *inside* `permissions.user_can_manage_project()` (the single canonical path) — no task-action call site would need per-site edits, because none compare `assigned_pm` / roles directly.
- **Forward note (no action):** `project_managers()` and the milestone/notification recipient logic in #1/#2 target `Finance`/`BD`/`CEO` by flat role string. If Program roll-ups later require program-level managers to be notified, that recipient assembly (`views.py:2070–2072`, `2249–2251`) is where it would change — but that is well outside reload elimination.

---

## 5. Edge cases checked

| Edge case (from prompt §5) | Finding |
|---|---|
| Same action on >1 page | Only `upload_task_attachment` (#7) truly renders on two pages (task detail + overview quick-attach) — **both call the same view**, no divergent impl. The `project_detail.html` duplicate is **dead code** (view is redirect-only; template unrendered). |
| Plain `<a href>` GET vs POST form | **GET links (need `hx-get`, or a rethink):** `task_add` (#6, overview L52) and `task_assign_design_head` (#4, overview L849) navigate to standalone pages. **POST forms (need `hx-post`):** #1, #2, #3-modal, #5, #7, #8, #9. |
| `assigned_to = null` under stricter server enforcement | **Safe today.** `task_status_update` returns `JsonResponse 400` (`:1913`); `task_detail_status_update` returns `403` (`:2129`); `task_assign` handles null via unassign path; `task_set_due_date` does not depend on assignee. No null-assignee path can crash the server. |
| Action split across two round trips | **Yes — `task_add` (#6) and standalone `task_assign` / `task_assign_design_head` (#3/#4) are 2 reloads each** (navigate to full form page = reload 1, submit = reload 2). The "3 actions = 3 reloads" framing **undercounts** these: doing an assign via the standalone page is already 2 reloads by itself. The overview assign *modal* path (#3) is 1 reload because it posts without a page navigation. HTMX collapses both to zero. |

---

## 6. Cross-reference notes (do NOT resolve here)

- **Cascade scheduling** (`task_set_due_date` PM path, `views.py:2475–2477` → `recalculate_from_task`): #5 touches cascade recalculation. Per instructions, cascade logic was **not re-verified**; noted only that this call site invokes it, so an HTMX response for #5 must still surface the "N task(s) recalculated" outcome to the user.
- **Access isolation:** `project_overview` docstring claims "PM and SE isolation applies" but the view only enforces PM isolation explicitly (`views.py:3845`). This overlaps `Project_Overview_Access_Isolation_Audit_Prompt.md` — **left there, not touched here.**
- **Ownership normalization:** all PM-authority task sites (#1, #3, #5, #6, #8) already route through `user_can_manage_project()` via `_pm_owns_project`. **No un-normalized raw `assigned_pm` / role-string comparison exists in any task-action view.** The raw `assigned_pm` references elsewhere in `views.py` (e.g. `:1649`, `:1715`, `:4276`, `:6614`) are in project-create / admin-assign-pm / notification code — **outside** this audit's scope.

---

## 7. Pre-flight simulation results

- **Cross-checked against `urls.py`:** every route with a task-action shape was traced; grepping `views.py` alone would have missed nothing here, but the `urls.py` pass confirmed #3 `task_assign` (whose markup uses a JS-built URL, invisible to a `{% url %}` grep) is live. No route missed.
- **No `TBD` cells:** every inventory field in §1 is filled for all nine call sites.
- **Categories separated:** reload-elimination scope (§1, §2) is kept distinct from pre-existing gaps/divergences (§3) and hierarchy risks (§4), so each can be triaged independently.

---

## Deliverable checklist (prompt §Deliverable)

1. ✅ Full call-site inventory table — §1 (9 live sites, all fields populated).
2. ✅ Pattern-reuse section — §2 (no shared partial; single dead-code duplicate; dashboards link-only).
3. ✅ Flagged button-hidden-but-unenforced gaps — §3 (none are true holes; two benign layer-divergences noted).
4. ✅ Flagged Program/Project hierarchy hardcoding risks — §4 (no Program entity exists; no task-action site hardcodes hierarchy).
5. ✅ No code changes — audit only.
