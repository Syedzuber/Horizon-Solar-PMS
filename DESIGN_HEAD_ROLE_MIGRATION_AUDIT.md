# Design Head Role Migration — Audit

**Part 6.5a. Investigation only. No code was changed by this session.**

| | |
|---|---|
| Local HEAD | `931182e` — *OPEX design Part 6: site groups, aggregated BOQ, BOQ lock, SCM handoff* |
| Deployed SHA (Railway `triumphant-forgiveness` / `Horizon-Solar-PMS` / production) | `931182e5e6230fa12f5764654892a7c2394646ec` — deployment `175a6029`, SUCCESS |
| Match | Yes |
| Working tree at session start | clean |
| Database used for measurements | **local Postgres** (`solarpms_local`). Production is NOT measured — see item 5. |

Line numbers are as of `931182e`.

---

## 0. The headline correction

`projects/permissions.py:307-309` states:

```python
# Part 1 added 'Design Head' to ROLE_CHOICES, but no user holds it and 56 existing
# @role_required decorators still match 'Design' literally — switching now would lock
# the real Design Head out of every screen he already uses.
```

**The number 56 is wrong, and it is wrong in a way that matters.** Counted mechanically over
every non-migration `.py` file in `projects/`:

```
total @role_required decorator uses: 61
of which include 'Design':           1        <- projects/views.py:893
```

Distribution of the 61:

```
 33  @role_required(['Admin'])
  7  @role_required(['PM', 'Project Coordinator'])
  5  @role_required(['Admin', 'PM'])
  3  @role_required(['Finance'])
  3  @role_required(['SCM'])
  2  @role_required(['Site Engineer'])
  2  @role_required(['Admin', 'PM', 'CEO'])
  1  @role_required(list(LANDING_ROLES))          # ('CEO','Finance','SCM')
  1  @role_required(['Design'])                   # dashboard_design
  1  @role_required(['PM', 'Admin', 'CEO'])
  1  @role_required(['PM'])
  1  @role_required(['BD'])
  1  @role_required(['BD', 'PM'])
plus decorators.py:118  system_admin_required = role_required(['System Admin'])
```

56 was (approximately) the *total* decorator count when that comment was written, not the
count that lists `'Design'`. The decorator surface a role migration has to cross is **one
decorator**, not fifty-six. The real exposure is elsewhere — in inline `role == 'Design'`
comparisons, template gates, and the `is_design_head` flag that the entire OPEX design
module is built on.

---

## 1. Every place `'Design'` appears as a role value

Scope note: the literal `'Design'` is also used for things that are **not** `UserProfile.role`
— `Task.assigned_role` (`Task.DESIGN = 'Design'`, models.py:325), residential phase names
(`utils.py:448`, `views.py:8385`, `views.py:8909`, `migrations/0034`), a Gantt display name
(`gantt_constants.py:21,60`), and a task-duration key (`utils.py:336`). Those are listed
separately at the end of this item and are **not** affected by a role migration.

### 1a. `@role_required([...])` decorators that include `'Design'`

| # | Location | View | What a `'Design Head'` user loses |
|---|---|---|---|
| 1 | [views.py:893](projects/views.py#L893) `@role_required(['Design'])` | `dashboard_design` | **The entire Design dashboard**, and with it the Part 4.5 Design Head strip (`head_counts`, views.py:1039), which is computed *inside* this view. Measured: `GET /dashboard/design/` → `302 → /dashboard/admin/`. |

That is the complete list. One decorator.

### 1b. Python comparisons on `UserProfile.role`

| # | Location | Form | What a `'Design Head'` user loses (or gains) |
|---|---|---|---|
| 1 | [decorators.py:19](projects/decorators.py#L19) | `ROLE_DASHBOARD = {… 'Design': '/dashboard/design/' …}` | No entry for `'Design Head'`, so `get_user_dashboard()` falls through to `/dashboard/admin/` (decorators.py:50). **Loses their landing page**; lands on the generic admin shell. Measured: `ROLE_DASHBOARD.get('Design Head') is None`. |
| 2 | [views.py:353](projects/views.py#L353) | `_ROLE_DASHBOARD = {… 'Design': 'dashboard_design' …}` | Second, duplicate dashboard map. Same loss. (Two maps that must be kept in step — neither references the other.) |
| 3 | [views.py:393](projects/views.py#L393) | `elif role == 'Design':` in `tasks_drill_down` | **This is a WIDENING, not a loss.** The branch chain ends in a comment `# SCM and others: all active non-deleted projects` (views.py:403) with no per-user term, so an unmatched role falls into portfolio-wide scope. Measured on `/tasks/overdue/`: **2 project groups as `'Design'` → 4 as `'Design Head'`**. Same on due-today (1 → 2) and due-soon (1 → 2). |
| 4 | [views.py:718](projects/views.py#L718) | `design_candidates = UserProfile.objects.filter(role='Design', …)` (PM dashboard) | Praveen **disappears from the PM's assign-designer dropdown**. |
| 5 | [views.py:2204](projects/views.py#L2204) | `get_object_or_404(UserProfile, pk=…, role='Design', …)` in `project_activate` | Praveen **cannot be selected as `assigned_design` when a project is activated** — the lookup 404s. |
| 6 | [views.py:3700](projects/views.py#L3700) | `candidates = UserProfile.objects.filter(role='Design', …)` in `task_assign_design_head` | Praveen is absent from the candidate list on **his own** reassignment screen — he can reassign Design tasks to others but not to himself. |
| 7 | [views.py:3706](projects/views.py#L3706) | `get_object_or_404(UserProfile, pk=…, role='Design', …)` same view | Assigning a Design task **to** Praveen 404s. |
| 8 | [views.py:5362](projects/views.py#L5362) | `UserProfile.objects.get(pk=…, role='Design', …)` — `assign_design` action on `project_overview` | The PM's later "assign design" action cannot select Praveen: *"Invalid design user selected."* |
| 9 | [views.py:5565](projects/views.py#L5565) | `design_candidates = UserProfile.objects.filter(role='Design', …)` — `project_overview` | Praveen absent from that dropdown. |
| 10 | [views.py:7604](projects/views.py#L7604) | `if role == 'Design':` — `my_documents` Section B | **Loses the "BOQ Submissions" section** on My Documents. Measured: marker present as `'Design'`, absent as `'Design Head'`. |
| 11 | [views.py:7611](projects/views.py#L7611) | `if role == 'Design':` — `my_documents` Section C | Loses the "Design Submissions" section. |
| 12 | [views.py:5595](projects/views.py#L5595) | `gantt_can_view_client = role in ('PM', 'Project Coordinator', 'CEO')` | No change — `'Design'` is not in this tuple today, so `'Design Head'` loses nothing. Listed because it is a role tuple in the same family. |
| 13 | [views.py:3192](projects/views.py#L3192) | `if normalised_user_role != task.assigned_role and not is_pm:` — `task_status_update` | **Cannot change the status of any task**, including Design tasks assigned to them personally. `'Design Head' != 'Design'`. Measured: `302` (success) as `'Design'` → **`403`** as `'Design Head'`. Praveen currently holds **7 tasks**. |
| 14 | [views.py:1969](projects/views.py#L1969) | `return normalised_user_role == task.assigned_role` — `_user_can_complete_checklist_item` | **Cannot tick or photograph any checklist item** on a Design task they hold (unless they are also PM/coordinator, which Praveen is not). |
| 15 | [views.py:3789](projects/views.py#L3789) | `if task.assigned_role != user_task_role: raise PermissionDenied` — task due-date edit | **Cannot set the due date** on a Design task they hold. |
| 16 | [views.py:3615-3624](projects/views.py#L3615-L3624) | `_TASK_TO_PROFILE_ROLE`, `role=profile_role` — `task_assign` | A PM assigning a Design task cannot pick Praveen. Same class as #4-#9. |
| 17 | [permissions.py:143](projects/permissions.py#L143) | `if role == 'Design':` — `user_can_view_project()` Design branch | Not a loss: `'Design Head'` is caught **earlier** by the flag/string branch at permissions.py:119 and returns True portfolio-wide. Listed because it is the branch `'Design Head'` no longer reaches. |
| 18 | [permissions.py:220](projects/permissions.py#L220) | `if profile.role == 'Design':` — `user_can_view_project_boq()` Design branch | Not a loss: caught earlier at permissions.py:217, which already accepts the string `'Design Head'`. Measured: BOQ read still `200`. |
| 19 | [permissions.py:271](projects/permissions.py#L271) | `if profile.role != 'Design': return False` — `user_can_edit_project_boq()` | **Loses BOQ WRITE on every project, permanently.** This is a hard `!=` with no Design Head branch — by design (permissions.py:244: *"Design Head — portfolio-wide READ only"*). Praveen is `assigned_design` on `HRP-RES-2026-005` today, so this is a live loss, not theoretical. |
| 20 | [design_views.py:182](projects/design_views.py#L182) | `.filter(role='Design', is_active=True)` — designer list on `design_head_sites` | Praveen **cannot allocate an OPEX site to himself**. (Arguably correct — `user_can_qc_design()` already refuses self-QC — but it is a behaviour change, not a no-op.) |
| 21 | [design_views.py:396](projects/design_views.py#L396) | `get_object_or_404(UserProfile, pk=raw_id, role='Design', …)` — `_resolve_designer` | Allocating **to** Praveen raises `ValueError` → the allocation is refused. |
| 22 | [seed_opex_test_data.py:87-89](projects/management/commands/seed_opex_test_data.py#L87-L89) | `if profile.role != 'Design': raise …` | The OPEX seed command **hard-fails** with *"has role 'Design Head', expected 'Design'"*. Test-data tooling only, but it stops working the moment the migration lands. |
| 23 | [models.py:75](projects/models.py#L75) | `limit_choices_to={'role': 'Design'}` on `Project.assigned_design` | Django admin and any `ModelForm` on that FK **exclude Praveen from the picker**. This is enforced by forms only, not by the database — an existing `assigned_design` row pointing at him is not invalidated. |

### 1c. Template checks on role

| # | Location | What a `'Design Head'` user loses |
|---|---|---|
| 1 | [boq_detail.html:59](projects/templates/projects/boq_detail.html#L59) | The three out-of-table Design forms (`saveDesignForm`, `addRowForm`, per-item delete). **No BOQ editing UI at all.** |
| 2 | [boq_detail.html:98](projects/templates/projects/boq_detail.html#L98) | The editable Notes textarea; falls back to the read-only rendering. |
| 3 | [boq_detail.html:131](projects/templates/projects/boq_detail.html#L131) | The Design column headers; falls through to the `{% else %}` "PM / Admin — read-only" column layout. |
| 4 | [boq_detail.html:158](projects/templates/projects/boq_detail.html#L158) | The Design row cells — quantity `<input>`, make-preference `<select>`, delete button. Measured: `name="boq_qty_` **absent**. |
| 5 | [boq_detail.html:264](projects/templates/projects/boq_detail.html#L264) | The `+ Add Row` footer. |
| 6 | [boq_detail.html:287](projects/templates/projects/boq_detail.html#L287) | **Save Draft / Submit to SCM buttons.** Measured: `save_design` **absent** from the body. |
| 7 | [boq_detail.html:330](projects/templates/projects/boq_detail.html#L330) | The Add-Row modal. |
| 8 | [my_documents.html:100](projects/templates/projects/my_documents.html#L100) | The BOQ Submissions card. Measured: **absent**. |
| 9 | [project_overview.html:394](projects/templates/projects/project_overview.html#L394) | **The entire BOQ card on the project page** — `'Design'` is one of five roles in this `{% if %}`. Measured: `boqCollapse` **absent**. |
| 10 | [project_overview.html:435](projects/templates/projects/project_overview.html#L435) | The "Update BOQ" button (falls back to "View full BOQ"). Moot given #9. |
| 11 | [project_overview.html:442](projects/templates/projects/project_overview.html#L442) | The "Create BOQ" empty-state button. Moot given #9. |
| 12 | [_task_row.html:41](projects/templates/projects/partials/_task_row.html#L41) | `{% if task.assigned_role == 'Design' and request.user.profile.is_design_head %}` — this gates on the **flag**, not the role, so it survives a role change **only if the flag is retained**. |
| 13 | [_task_row.html:52](projects/templates/projects/partials/_task_row.html#L52) | `{% if is_assigned_pm or user_task_role == task.assigned_role %}` — the status `<select>`. Loses it; matches the server-side loss at 1b#13. |

Cosmetic-only role checks (colour swatches and avatar tints — a `'Design Head'` user simply
falls to the default colour, no functional loss): `admin/departments.html:53`,
`admin/reset_password.html:43`, `admin/user_management.html:34`,
`subadmin/departments.html:67`, `issue_detail.html:202,267`,
`partials/_task_comments.html:20,92`, `portal_activity_log.html:123`,
`project_timeline.html:44`.

`base.html:51` gates the **Programs** nav link on `Admin / PM / CEO` — `'Design'` is not
there either, so nothing changes (this is deferred finding H4, unaffected).

### 1d. Constants, tuples, frozensets and choice lists

| # | Location | Contains `'Design'`? | Contains `'Design Head'`? | What a `'Design Head'` user loses |
|---|---|---|---|---|
| 1 | [models.py:508-524](projects/models.py#L508-L524) `UserProfile.ROLE_CHOICES` | yes | **yes** (line 519) | Nothing. The role is a legal value. `role` is `max_length=20`; `'Design Head'` is 11 chars. |
| 2 | [models.py:327-334](projects/models.py#L327-L334) `Task.ROLE_CHOICES` | yes (`Task.DESIGN`) | **no** | This is `Task.assigned_role`, a *department*, not a user role. A `'Design Head'` user never matches any `assigned_role` — the root cause of 1b#13/#14/#15. |
| 3 | [views.py:8883-8892](projects/views.py#L8883-L8892) `_SA_EDITABLE_ROLE_CHOICES` | yes | **NO** | System Admin **cannot assign** `'Design Head'`, and cannot edit a user who already holds it back to anything (`new_role not in valid_roles` → *"Invalid role selection."*, views.py:9038). This is deferred finding D1. |
| 4 | [permissions.py:29](projects/permissions.py#L29) `PORTFOLIO_VIEW_ROLES = frozenset({'CEO','Finance','SCM','Admin'})` | no | no | Nothing — Design Head has its own branch at permissions.py:119 which accepts both flag and string. |
| 5 | [permissions.py:160](projects/permissions.py#L160) `BOQ_PORTFOLIO_READ_ROLES = frozenset({'SCM','Admin','CEO'})` | no | no | Nothing — own branch at permissions.py:217, accepts both forms. |
| 6 | [settings.py:158](solarpms/settings.py#L158) `EOD_DIGEST_EXCLUDED_ROLES = ['CEO','Admin','System Admin']` | no | **no** | Nothing is lost — but a `'Design Head'` user **starts receiving** the individual EOD digest built for delivery roles, with no content branch of its own (`send_eod_digest.py:120-121` branches only on `'Project Coordinator'`). This is deferred finding D3. |
| 7 | [decorators.py:32](projects/decorators.py#L32) `LANDING_ROLES = ('CEO','Finance','SCM')` | no | no | Nothing. |
| 8 | [views.py:7874-7883](projects/views.py#L7874-L7883) `DEPT_NAMES` (admin_departments) | yes | **no** | The Departments screen groups by raw role string, so `'Design Head'` renders as its own group labelled `Design Head` (the `.get(role_key, role_key)` fallback at views.py:7957). Cosmetic. |
| 9 | [views.py:8894-8903](projects/views.py#L8894-L8903) `_SA_DEPT_NAMES` | yes | **no** | Same, on the System Admin departments screen. |
| 10 | [views.py:8880](projects/views.py#L8880) `_SA_EXCLUDED_ROLES = ['Admin','System Admin']` | no | no | Nothing. A `'Design Head'` user remains editable-in-principle by System Admin — except that #3 blocks it. |
| 11 | `_PROFILE_TO_TASK_ROLE = {'BD': 'BD / Sales'}` at [views.py:1967](projects/views.py#L1967), [3189](projects/views.py#L3189), [3786](projects/views.py#L3786), [5570](projects/views.py#L5570) | — | no mapping | The existing precedent for reconciling a `UserProfile.role` with a `Task.assigned_role`. There is **no** `'Design Head' → 'Design'` entry. Four separate copies of the same two-entry dict. |
| 12 | `_TASK_TO_PROFILE_ROLE = {'BD / Sales': 'BD'}` at [views.py:3614](projects/views.py#L3614), [5554](projects/views.py#L5554) | — | no mapping | Inverse of #11, two more copies. |

### Occurrences of the literal `'Design'` that are NOT a user role (no action needed)

`models.py:325,333` (`Task.DESIGN`), `utils.py:336` (duration key), `utils.py:448-455`
(phase name + `Task.DESIGN`), `gantt_constants.py:21,60` (Gantt display name),
`views.py:8385`, `views.py:8909` (`_PHASE_ORDER`), `views.py:1724` (department label on the
CEO dashboard), `views.py:1669-1671,1687-1688` (`Q(assigned_role=Task.DESIGN)` aggregates),
`migrations/0034` (TaskDurationTemplate seed), and every historical `ROLE_CHOICES` snapshot
inside `migrations/0004, 0005, 0012, 0013, 0035, 0036, 0039, 0048`.

---

## 2. Every place `is_design_head` is read

| # | Location | Authority conferred |
|---|---|---|
| 1 | [permissions.py:119](projects/permissions.py#L119) `user_can_view_project()` | Portfolio-wide project **VIEW**. Accepts flag **OR** the string `'Design Head'`. |
| 2 | [permissions.py:217](projects/permissions.py#L217) `user_can_view_project_boq()` | Portfolio-wide BOQ **READ**, no write. Accepts flag **OR** the string. |
| 3 | [permissions.py:334](projects/permissions.py#L334) `user_is_design_head()` | `return bool(profile.is_design_head)` — **flag only, no string branch.** |
| 4 | [permissions.py:353](projects/permissions.py#L353) `user_is_design_head_deputy()` | `profile.deputy_for.filter(is_design_head=True).exists()` — a deputy is a deputy only of somebody whose **flag** is set. **Flag only.** |
| 5 | [permissions.py:375](projects/permissions.py#L375) `user_has_design_head_authority()` | `user_is_design_head(user) or user_is_design_head_deputy(user)` — inherits the flag-only rule from #3 and #4. |
| 6 | [permissions.py:399](projects/permissions.py#L399) `user_can_qc_design()` | Calls #5, then refuses the site's own designer. Flag-derived. |
| 7 | [permissions.py:449](projects/permissions.py#L449) `user_can_view_design()` | `user_can_view_project(...) or user_has_design_head_authority(...)`. First term accepts the string, second is flag-derived. |
| 8 | [permissions.py:487](projects/permissions.py#L487) `user_can_view_site_groups()` (Part 6) | Falls through to #5. Flag-derived. |
| 9 | [views.py:3691](projects/views.py#L3691) `task_assign_design_head` | `if not request.user.profile.is_design_head: raise Http404`. **Raw flag read, no helper, no string branch.** |
| 10 | [views.py:1039](projects/views.py#L1039) `dashboard_design` context | `design_head_dashboard_counts(request.user)` → #5. |
| 11 | [_task_row.html:41](projects/templates/projects/partials/_task_row.html#L41) | Renders the Design-task reassign button. **Raw flag read in a template.** |
| 12 | [forms.py:152](projects/forms.py#L152) `AdminUserEditForm.is_design_head` | Where an Admin sets the flag. |
| 13 | [views.py:7997](projects/views.py#L7997), [views.py:8027](projects/views.py#L8027) | Admin user-edit write and initial. |
| 14 | [views.py:9048](projects/views.py#L9048) | System Admin departments write: `is_design_head = request.POST.get('is_design_head') == 'on'`. |
| 15 | [admin.py:155-156](projects/admin.py#L155-L156) | Django-admin `list_display` / `list_filter`. |
| 16 | [seed_opex_test_data.py:92-95,115](projects/management/commands/seed_opex_test_data.py#L92-L95) | Pre-flight assertion in the test-data seeder. |
| 17 | [tests_permissions.py:29,39-40,243,250,261-262,266](projects/tests_permissions.py#L29) | Test fixtures. |

**Every entry point of the OPEX design module (Parts 2-6) routes through #5, which is
flag-only.** Eighteen views use `user_has_design_head_authority()` or a derivative:
`design_head_sites`, `design_survey_upload`, `design_allocate`, `design_bulk_allocate`,
`design_due_date_approve`, `design_due_date_reject`, `design_due_date_change`,
`design_site_workspace`, `design_head_review`, `design_arka_approve`, `design_arka_reject`,
`design_qc_start`, `design_qc_pass`, `design_qc_fail`, `design_qc_queue`,
`design_qc_review`, `design_tender_dashboard`, `site_group_list`, `site_group_detail`.

### After a role migration, does `is_design_head` remain the authority mechanism?

**As the code stands today: it MUST remain, or the whole design module closes.** Nothing in
`user_is_design_head()` or `user_is_design_head_deputy()` consults the role string, so
setting `role='Design Head'` and clearing the flag revokes every design-module authority.

Measured (all three scenarios run inside a rolled-back transaction on the local database):

| Surface | A: `Design` + flag | B: `Design Head` + flag | C: `Design Head`, **no** flag |
|---|---|---|---|
| `/design/qc/` | 200 | 200 | **403** |
| `/programs/18/design/` | 200 | 200 | **403** |
| `/programs/18/design/dashboard/` | 200 | 200 | **403** |
| `/programs/18/site-groups/` | 200 | 200 | **403** |
| `/design/Test-Site-01/work/` | 200 | 200 | **403** |
| `/design/Test-Site-01/review/` | 200 | 200 | **403** |
| `/design/Test-Site-01/qc/` | 200 | 200 | **403** |
| `…/tasks/<id>/assign-design/` | 200 | 200 | **404** |

So the three options are:

* **coexist** — role added, flag kept. Zero design-module change; both `permissions.py:119`
  and `:217` already accept either form. This is what the code is currently built for.
* **retire the flag** — requires adding a role-string branch to `user_is_design_head()`
  (permissions.py:319-334) and to the raw flag read at `views.py:3691` and
  `_task_row.html:41`. Also requires re-expressing `user_is_design_head_deputy()`, whose
  entire rule is `deputy_for.filter(is_design_head=True)` — with the flag gone there is no
  predicate identifying the *naming* profile as a Head.
* **redundant** — not currently reachable. The flag is documented as deliberately
  role-independent (`models.py:530`, and `test_flag_is_independent_of_role` asserts it works
  on `PM`, `Site Engineer` and a blank role), so "redundant" would mean deleting a
  documented, tested capability rather than just stopping using it.

---

## 3. Every place `'Design Head'` is ALREADY recognised

| # | Location | Form |
|---|---|---|
| 1 | [models.py:519](projects/models.py#L519) | `('Design Head', 'Design Head')` in `UserProfile.ROLE_CHOICES` (migration `0048`). |
| 2 | [permissions.py:119](projects/permissions.py#L119) | `if getattr(profile, 'is_design_head', False) or role == 'Design Head':` — `user_can_view_project()`. |
| 3 | [permissions.py:217](projects/permissions.py#L217) | `if getattr(profile, 'is_design_head', False) or profile.role == 'Design Head':` — `user_can_view_project_boq()`. |
| 4 | [tests_permissions.py:253-263](projects/tests_permissions.py#L253-L263) | `test_future_role_string_grants_portfolio_view` — sets `role='Design Head'`, `is_design_head=False`, asserts view is granted. |

**That is all four.** Confirmed by measurement: in scenario C (role only, no flag),
`/projects/HRP-RES-2026-005/boq/` and `/projects/Test-Site-01/boq/` both still return
`200` — #3 is doing that work.

`forms.py:154` (`label='Design Head'`) is a checkbox label for the boolean, not a role
recognition. Every other occurrence of the string in the codebase is prose in a docstring,
comment or template comment.

---

## 4. The role assignment surface

### Where role is set

| Surface | View | Form / mechanism | Template | Choice source |
|---|---|---|---|---|
| Admin → create user | `admin_user_create` [views.py:1837-1875](projects/views.py#L1837) | `UserCreateForm` | `projects/admin/user_create.html` | `UserProfile.ROLE_CHOICES` ([forms.py:27-30](projects/forms.py#L27-L30)) |
| Admin → edit user (basic) | `admin_user_edit` [views.py:1877-1930](projects/views.py#L1877) | `UserEditForm` | `projects/admin/user_edit.html` | `UserProfile.ROLE_CHOICES` ([forms.py:90-93](projects/forms.py#L90-L93)) |
| Admin → edit user (full, incl. flag) | [views.py:7970-8035](projects/views.py#L7970) | `AdminUserEditForm` | `projects/admin/user_edit.html` | `UserProfile.ROLE_CHOICES` ([forms.py:151](projects/forms.py#L151)) |
| Admin → User Management inline | `admin_user_management` [views.py:7744](projects/views.py#L7744) | raw POST `change_role` | `projects/admin/user_management.html` | `UserProfile.ROLE_CHOICES` (views.py:7748, validated 7792-7793) |
| Admin → Departments inline | `admin_departments` [views.py:7869](projects/views.py#L7869) | raw POST `change_role` | `projects/admin/departments.html` | `UserProfile.ROLE_CHOICES` (views.py:7926, 7964) |
| System Admin → Departments | `subadmin_departments` [views.py:~8990-9064](projects/views.py#L9022) | raw POST `edit_user` / `create_user` | `projects/subadmin/departments.html` | **`_SA_EDITABLE_ROLE_CHOICES`** (views.py:9001, 9035, 9136, 9157) |
| Django admin | `admin.py:155` | `UserProfileAdmin` | Django admin | model `choices` |

### `_SA_EDITABLE_ROLE_CHOICES` — quoted verbatim

Now at **views.py:8883** (was 8758 before Part 6 added lines above it):

```python
# Roles that System Admin must never see, query, or be able to assign
_SA_EXCLUDED_ROLES = ['Admin', 'System Admin']

# Operational roles System Admin may create/edit — never includes Admin or System Admin
_SA_EDITABLE_ROLE_CHOICES = [
    ('PM',                  'PM'),
    ('Project Coordinator', 'Project Coordinator'),
    ('Site Engineer',       'Site Engineer'),
    ('Design',              'Design'),
    ('Finance',             'Finance'),
    ('SCM',                 'SCM'),
    ('CEO',                 'CEO'),
    ('BD',                  'BD'),
]
```

**It does NOT include `'Design Head'`.**

### Do the create/edit dropdowns derive from `ROLE_CHOICES` directly?

Yes — quoted:

```python
# forms.py:27-30  (UserCreateForm)
role = forms.ChoiceField(
    choices=UserProfile.ROLE_CHOICES,
    widget=forms.Select(attrs={'class': 'form-control'}),
)

# forms.py:90-93  (UserEditForm)
role = forms.ChoiceField(
    choices=UserProfile.ROLE_CHOICES,
    widget=forms.Select(attrs={'class': 'form-control'}),
)

# forms.py:151  (AdminUserEditForm)
role       = forms.ChoiceField(choices=UserProfile.ROLE_CHOICES)
```

and the two inline role-change endpoints validate against the same list:

```python
# views.py:7792-7793  (admin_user_management)
valid_roles = [r[0] for r in role_choices]          # role_choices = UserProfile.ROLE_CHOICES (7748)
if new_role not in valid_roles:

# views.py:7926-7927  (admin_departments)
valid_roles = [r[0] for r in UserProfile.ROLE_CHOICES]
if new_role not in valid_roles:
```

against the System Admin path:

```python
# views.py:9034-9040  (subadmin_departments, edit_user)
new_role = request.POST.get('new_role', '').strip()
valid_roles = [r[0] for r in _SA_EDITABLE_ROLE_CHOICES]

# Defense-in-depth: cannot promote anyone to Admin or System Admin
if new_role not in valid_roles:
    messages.error(request, 'Invalid role selection.')
```

### The exact inconsistency

**Can assign `'Design Head'` today (5 surfaces):**
1. Admin → Create User (`UserCreateForm`)
2. Admin → Edit User (`UserEditForm`)
3. Admin → Edit User full form (`AdminUserEditForm`)
4. Admin → User Management, inline `change_role`
5. Admin → Departments, inline `change_role`

*(plus Django admin for a superuser)*

**Cannot assign `'Design Head'` (2 surfaces):**
6. System Admin → Departments, `create_user`
7. System Admin → Departments, `edit_user`

The sharper half of the inconsistency is on the **read** side. `subadmin_departments`'s
`edit_user` validates `new_role not in _SA_EDITABLE_ROLE_CHOICES` **before** applying any
edit (views.py:9034-9040). So once an Admin sets a user to `'Design Head'`, a System Admin
opening that user's edit modal and clicking Save — even to change only a phone number —
gets *"Invalid role selection."* and the whole edit is refused. `_SA_EXCLUDED_ROLES` does
**not** contain `'Design Head'`, so the user is still listed and still appears editable;
the refusal only happens on submit.

---

## 5. Who holds what today (LOCAL database)

Command as specified, run against `solarpms_local`:

```
Counter({'Design': 4, 'Site Engineer': 2, 'PM': 2, 'Admin': 1, 'CEO': 1, 'SCM': 1,
         'System Admin': 1, 'Project Coordinator': 1, 'Finance': 1, 'BD': 1})
FLAG: 13 praveen 'Design'
```

* `role='Design Head'` — **no rows**.
* `design_head_deputy` set — **no rows**.
* Exactly one flag holder: `praveen`, pk 13, `role='Design'`, `is_design_head=True`,
  `is_active=True`, `design_head_deputy=None`.

What Praveen is attached to on local data:

```
assigned_design on : ['HRP-RES-2026-005']
assigned_pm on     : []
coordinator on     : []
tasks assigned     : 7
BOQs submitted_by  : 0
design assignments allocated to him : 0
```

> **PRODUCTION IS NOT MEASURED.** Every number above is from the local Postgres. The user
> must run the same snippet against Railway before deciding anything. Specifically unknown
> on production: how many users hold `is_design_head=True`, whether anybody already holds
> `role='Design Head'`, whether any `design_head_deputy` is set, and how many tasks /
> projects / BOQs the real Design Head is attached to. Local data is seeded and diverges
> from production in known ways (deferred findings B1 and F2 both record local/production
> splits).

---

## 6. Deputy interaction (finding G4)

### Still true — confirmed

`user_can_view_project_boq()` has no Design Head **authority** branch; it has a Design Head
**identity** branch, and a deputy is neither the flag holder nor the role holder. Quoted
verbatim, [permissions.py:204-225](projects/permissions.py#L204-L225):

```python
    if project is None:
        return False
    profile = getattr(user, 'profile', None)
    if profile is None:
        return False

    # Management authority always implies BOQ read. Routed through the canonical path —
    # assigned_pm / coordinators are never re-compared here.
    if user_can_manage_project(user, project):
        return True

    if profile.role in BOQ_PORTFOLIO_READ_ROLES:
        return True

    if getattr(profile, 'is_design_head', False) or profile.role == 'Design Head':
        return True

    if profile.role == 'Design':
        if project.assigned_design_id == profile.pk:
            return True
        return _user_holds_task_on_project(profile, project)

    return False
```

A deputy is typically a plain `role='Design'` user with `is_design_head=False`, so they fall
to the last branch and are refused on any project where they are neither `assigned_design`
nor a task-holder. Compare the design-module gate, which **does** admit them
([permissions.py:449](projects/permissions.py#L449)):

```python
    return user_can_view_project(user, project) or user_has_design_head_authority(user)
```

No deputy exists on the local database today (item 5), so G4 could not be re-measured this
session; the Part 4 session measured it directly and the code on both sides is unchanged
since.

### Every screen or action where a deputy hits the same gap

| # | Where | The link / action that 403s |
|---|---|---|
| 1 | `design_qc_review` → `projects/design/qc_review.html` | The **View BOQ** button (the original G4). |
| 2 | `design_site_workspace` → `projects/design/site_workspace.html` | The BOQ link; `can_edit_boq` is also False, so the "assigned_design names a different user" warning fires misleadingly. |
| 3 | `design_head_review` → `projects/design/head_review.html` | Any BOQ link on the Arka review screen. |
| 4 | **`site_group_detail` → `projects/design/site_group_detail.html` (NEW, Part 6)** | The **View** button on **every member site row** of a procurement group. A deputy can open the group screen (`user_can_view_site_groups` → `user_has_design_head_authority`) and read the aggregated BOQ, then 403 on every per-site BOQ behind it. |
| 5 | `boq_history` [views.py:4644](projects/views.py#L4644) | Same read gate as `boq_detail` — refused for the same reason. |
| 6 | `design_tender_dashboard` (Part 5) | No direct BOQ link, but the drill-down leads to `design_head_sites` → workspace → #2. |

Item 4 is new since Part 4 recorded G4 and is worse than the original: the group screen
*aggregates* the very BOQs it then refuses to let the deputy open.

### Does fixing G4 require modifying a Part 0.6 BOQ helper?

**Yes.** The fix is to make `user_can_view_project_boq()` ask for Design Head *authority*
rather than Design Head *identity* — i.e. replace `getattr(profile, 'is_design_head', False)
or profile.role == 'Design Head'` at permissions.py:217 with a call to
`user_has_design_head_authority(user)`. There is no caller-side alternative: unlike the
Part 6 group lock, which could be ANDed in at the caller because it only ever **narrows**
access, admitting a deputy **widens** access, and a caller cannot widen a gate that has
already returned False.

The one exception is `design_views.py`'s own screens, which could render deputy-safe BOQ
content themselves rather than linking to `boq_detail` — but that duplicates the BOQ page
rather than fixing the gate.

---

## 7. Blast radius — Praveen `'Design'` → `'Design Head'`, no other change

Measured on the local database, each scenario applied inside a transaction that was rolled
back; the role and flag were re-read afterwards and confirmed unchanged
(`'Design'` / `True`).

### Scenario B — role changed, `is_design_head` LEFT SET (the literal "no other change")

**LOST:**

| # | What | Evidence |
|---|---|---|
| 1 | **The Design dashboard.** `/dashboard/design/` → `302 /dashboard/admin/` | `@role_required(['Design'])`, views.py:893 |
| 2 | **The Part 4.5 Design Head strip** (allocation / Arka / QC queue counts and the tender links) | computed inside `dashboard_design`; marker absent from the followed body |
| 3 | **His landing page.** Post-login and every wrong-role bounce goes to `/dashboard/admin/` | `ROLE_DASHBOARD.get('Design Head') is None` |
| 4 | **Task status changes** on his own 7 tasks | `POST …/tasks/<id>/update/` `302` → **`403`** |
| 5 | **Task due-date edits** on tasks he holds | views.py:3789 `PermissionDenied` |
| 6 | **Checklist item completion** on tasks he holds | views.py:1969 |
| 7 | The status `<select>` and its surrounding controls on every task row | `_task_row.html:52` |
| 8 | **BOQ write, everywhere** — quantities, make preference, ad-hoc rows, submit | `user_can_edit_project_boq()` returns False at permissions.py:271; `save_design` marker absent |
| 9 | **The BOQ editing UI** — inputs, Save Draft, Submit to SCM, Add Row, the notes textarea | `name="boq_qty_` absent, `save_design` absent |
| 10 | **The whole BOQ card on `project_overview`** | `boqCollapse` absent |
| 11 | **"BOQ Submissions" and "Design Submissions" on My Documents** | markers absent |
| 12 | **Being selectable as a designer** — PM dashboard dropdown, `project_activate`, `project_overview` assign-design, `task_assign`, `task_assign_design_head` candidates, OPEX allocation | six `role='Design'` querysets, 1b#4-#9 and #20-#21 |
| 13 | **Being editable by a System Admin at all** — any save on his row is refused | views.py:9038 |
| 14 | The OPEX seed command refuses to run | seed_opex_test_data.py:87 |
| 15 | His picker entry on `Project.assigned_design` in Django admin / any ModelForm | `limit_choices_to` at models.py:75 |

**GAINED:**

| # | What | Evidence |
|---|---|---|
| 1 | **Portfolio-wide task drill-downs.** `tasks_drill_down` no longer matches the Design scoping branch and falls through to the no-per-user-term default | `/tasks/overdue/` **2 → 4** project groups; due-today 1 → 2; due-soon 1 → 2 |
| 2 | An individual EOD digest with no content branch of its own | `EOD_DIGEST_EXCLUDED_ROLES` does not list the role |
| 3 | His own group on the Departments screens, labelled "Design Head" | `DEPT_NAMES.get(role_key, role_key)` fallback |
| 4 | Nothing else. Portfolio project VIEW and BOQ READ were already his via the flag; the role string reaches the same two branches. | permissions.py:119, :217 |

**UNCHANGED** (all still 200): every OPEX design-module screen, `/design/qc/`,
`/programs/18/design/`, `/programs/18/design/dashboard/`, `/programs/18/site-groups/`,
`/design/<site>/work|review|qc/`, `/design/my-sites/`, BOQ **read** on both a Residential
and an OPEX project, `boq_history`, `project_overview` (page loads; the BOQ card is gone),
Notifications, My Documents (page loads; two sections gone), `task_assign_design_head`.

### Scenario C — role changed AND `is_design_head` cleared

Everything in Scenario B, **plus** the entire OPEX design module:
`/design/qc/`, `/programs/18/design/`, `/programs/18/design/dashboard/`,
`/programs/18/site-groups/`, `/design/<site>/work/`, `/design/<site>/review/`,
`/design/<site>/qc/` all → **403**, and `task_assign_design_head` → **404**. Survey upload,
allocation, due-date approval, Arka verdicts, QC verdicts, release and the Part 6 group
screens all become unreachable. See the table in item 2.

---

## 8. Reverse risk — what rollback actually requires

Setting `role` back to `'Design'` **is sufficient for authorisation**, because the role
migration itself writes exactly one field and nothing derives state from it:

* `UserProfile.role` is a plain `CharField` with no signal, no `save()` override, and no
  denormalised copy anywhere. Verified: `projects/signals.py` contains no reference to
  `role`.
* No model stores a role value on another row. `Task.assigned_role` is set from the
  residential template at project activation and is never rewritten from a user's profile.
* `Project.assigned_design` is a FK to `UserProfile`, not a role string. `limit_choices_to`
  is a **form-level** filter — an existing FK pointing at a `'Design Head'` user is not
  invalidated, does not error, and survives the round trip.
* Measured: three role/flag flips inside rolled-back transactions left
  `role='Design'`, `is_design_head=True` intact each time, and all 17 probed URLs returned
  to their original status codes.

**What is NOT covered by flipping `role` back:**

| # | Item | Why |
|---|---|---|
| 1 | `is_design_head`, **if it was also cleared** | Two fields changed, two must be restored. Restoring only `role` leaves the design module 403'd (scenario C). |
| 2 | Any `design_head_deputy` FK **pointing at** or **set by** the migrated user | `user_is_design_head_deputy()` re-checks `is_design_head=True` on the *naming* profile, so clearing the Head's flag silently revokes the deputy's authority too — and restoring the Head's flag silently restores it. The FK itself is untouched either way. |
| 3 | **Any role edit made by a System Admin while the user held `'Design Head'`** | Not applicable — such edits are refused outright (views.py:9038). This is a blocker, not a corruption risk. |
| 4 | Rows written *by* the user while migrated | e.g. an `ActivityLog` entry, a QC verdict, a group action. These are correct history and must not be rolled back. No row stores the actor's role, so none is stale. |
| 5 | **Other code changed in the same deployment** | If the migration session also edits `_SA_EDITABLE_ROLE_CHOICES`, `ROLE_DASHBOARD`, the `@role_required` at views.py:893, template gates, or `_PROFILE_TO_TASK_ROLE`, then rolling the *user* back is not rolling the *system* back. A `'Design'` user on a codebase that has been rewritten around `'Design Head'` is a third state that nobody has tested. |
| 6 | Django sessions | Not an issue — role is read fresh from the profile on every request; no role is cached in the session. |

There is **no data migration** implied by a role change, so there is nothing to reverse
beyond the one or two fields. The risk is entirely in item 5: rollback is trivial if the
user is migrated *before* the code, and is not trivial if code and data move together.

---

## 9. Non-role dependencies

Searched: `projects/*.py` (all), `projects/management/commands/*`, `projects/templates/**`,
`solarpms/settings.py`, `projects/admin.py`, `projects/signals.py`,
`projects/notifications.py`, `projects/context_processors.py`, `projects/design_metrics.py`.

| Area | Finding |
|---|---|
| **Management commands** | `send_eod_digest.py` — reads `EOD_DIGEST_EXCLUDED_ROLES` (line 102) and branches on `'Project Coordinator'` (line 121). No `'Design'` reference. A `'Design Head'` user would receive the generic digest. `seed_opex_test_data.py:87-95` — hard-asserts `profile.role == 'Design'` **and** `is_design_head`, and refuses to run otherwise. No other command references role. |
| **EOD digest** | See above. Deferred finding D3 remains open: no `'Design Head'` entry in the exclusion list, no content branch. |
| **Notification recipient selection** | `projects/notifications.py` contains **no** reference to `role` at all. Recipients are chosen by FK (`project_managers()`, `boq.submitted_by`, `task.assigned_to`) throughout. **Nothing to change.** |
| **Dashboard routing** | Two independent maps, neither with a `'Design Head'` entry: `decorators.ROLE_DASHBOARD` (line 19) used by `get_user_dashboard()` / `get_post_login_url()` / `role_required`'s denial redirect, and `views._ROLE_DASHBOARD` (line 353). `dashboard_admin` ([views.py:278](projects/views.py#L278)) is `@login_required` with **no role restriction**, so the fallback lands successfully rather than looping. |
| **`Task.assigned_to` filtering** | `Task.assigned_to` is a **FK to UserProfile** — never filtered by role. The role dependency is on `Task.assigned_role`, which is compared to the *user's* role in four places (views.py:1969, 3192, 3789, and `_task_row.html:52`) through two copies of `_PROFILE_TO_TASK_ROLE`. This is the single largest non-permission dependency and the reason a `'Design Head'` user cannot touch any task. |
| **Department / CEO aggregates** | `views.py:1669-1671, 1687-1688` count `Q(assigned_role=Task.DESIGN)` — unaffected, these count *tasks*, not users. `views.py:1724` labels the row `'Design'` — cosmetic. |
| **Admin screens** | `admin.py:155-156` lists and filters `UserProfile` by `role` and `is_design_head` — both keep working with a new value. `admin_departments` / `subadmin_departments` group by raw role string with a `.get(key, key)` fallback, so an unmapped role renders as its own group. |
| **`limit_choices_to`** | `models.py:75` on `Project.assigned_design`, mirrored in `migrations/0012`. Form-level only. |
| **Gantt** | `views.py:5595` `gantt_can_view_client = role in ('PM','Project Coordinator','CEO')` — `'Design'` is absent today, so no change. |
| **Context switcher / landing** | `decorators.LANDING_ROLES = ('CEO','Finance','SCM')` — no change. |
| **Signals** | `projects/signals.py` — no role reference. The `post_save` on `User` creates a `UserProfile` with a blank role; it does not read one. |
| **Context processors** | No role reference. |
| **`design_metrics.py`** | No role reference at all — every query is by FK. Part 5's dashboard is role-independent. |
| **Zoho webhook** | `views.py:5369` matches `assigned_pm` on **email with no role filter** (documented in `tests_permissions.py:222-228`). Unaffected by a role change. |

---

## 10. Test coverage

### The suite could not be executed this session

```
Using existing test database for alias 'default'...
Got an error creating the test database: permission denied to create database
Found 51 test(s).
```

Deferred finding **B5 is still open** — the local Postgres role cannot create the test
database, with or without `--keepdb`. **The 51 tests were not run.** Everything below is
read from the source, not from a passing run.

### Tests that assert role-based behaviour for `'Design'`

| # | Test | File / line | Asserts |
|---|---|---|---|
| 1 | `ASSIGNMENT_ROWS` matrix, incl. `('Design', 'Design')` | [tests_permissions.py:91](projects/tests_permissions.py#L91) | Feeds #2, #3, #4. |
| 2 | `test_assignment_roles_view_only_where_related` | [:132](projects/tests_permissions.py#L132) | Design VIEW is True when assigned PM / coordinator, False when unrelated. |
| 3 | `test_manage_is_assignment_based_for_every_role` | [:141](projects/tests_permissions.py#L141) | MANAGE is role-blind for all 10 rows. |
| 4 | `test_view_never_narrower_than_manage` | [:156](projects/tests_permissions.py#L156) | Invariant across all rows. |
| 5 | `test_design_sees_project_via_assigned_design_fk` | [:207](projects/tests_permissions.py#L207) | `role='Design'` + `assigned_design` → view True, manage False. |
| 6 | `test_design_sees_project_via_assigned_task` | [:215](projects/tests_permissions.py#L215) | `role='Design'` + task → view True, and per-project. |
| 7 | `test_non_pm_role_set_as_assigned_pm_can_still_view` | [:222](projects/tests_permissions.py#L222) | A `'Design'` profile set as `assigned_pm` (Zoho path) can view and manage. |
| 8 | `PORTFOLIO_ROWS` row `('Design Head', 'Design', True)` | [:83](projects/tests_permissions.py#L83) | Design Head-by-flag has portfolio view — the flag set **on a Design user**. |
| 9 | `test_flag_grants_portfolio_view_today` | [:242](projects/tests_permissions.py#L242) | flag on `'Design'` → portfolio view. |
| 10 | `test_flag_is_independent_of_role` | [:246](projects/tests_permissions.py#L246) | flag works on `'Design'`, `'PM'`, `'Site Engineer'`, and `''`. |
| 11 | `test_future_role_string_grants_portfolio_view` | [:253](projects/tests_permissions.py#L253) | role string alone, flag cleared → portfolio view. |
| 12 | `test_design_head_gets_view_not_manage` | [:265](projects/tests_permissions.py#L265) | flag grants view, never manage. |
| 13 | `test_other_roles_get_no_client_rows` | [tests_gantt.py:260-267](projects/tests_gantt.py#L260) | `'Design'` (with SE/Finance/SCM/BD) gets no Gantt client rows. |

`tests.py` uses only `'PM'` and `'Project Coordinator'` and is unaffected.

### `tests_permissions.py:256` — quoted, and is it now false?

```python
    def test_future_role_string_grants_portfolio_view(self):
        """Phase 2 promotes Design Head to a role; the check must already accept it.

        'Design Head' is not yet in UserProfile.ROLE_CHOICES, so this sets the field
        directly. `choices` is not enforced on save(), only by form validation.
        """
```

**The first sentence of the second paragraph is FALSE.** `'Design Head'` **is** in
`UserProfile.ROLE_CHOICES` — added by Part 1 at `models.py:519` and shipped in migration
`0048`. Confirmed by reading the live choices:

```
('Admin','Admin') ('System Admin','System Admin') ('PM','PM')
('Project Coordinator','Project Coordinator') ('Site Engineer','Site Engineer')
('Design','Design') ('Design Head','Design Head') ('Finance','Finance')
('SCM','SCM') ('CEO','CEO') ('BD','BD')
```

The **second** sentence remains true and is what actually makes the test work: `choices` is
not enforced on `save()`, so setting the field directly is legal — but it is no longer
*necessary*, since the value is now a valid choice. This is deferred finding **D2**, still
open. It is a stale docstring, not a broken test — the assertion itself
(`user_can_view_project` accepts the role string) is correct and passes.

### Would any existing test fail if `'Design Head'` were added to every relevant tuple?

Reasoning from source, since the suite cannot be run:

* **No test would fail from adding `'Design Head'` to `_SA_EDITABLE_ROLE_CHOICES`,
  `ROLE_DASHBOARD`, `_ROLE_DASHBOARD`, `DEPT_NAMES`, `_SA_DEPT_NAMES`, or
  `EOD_DIGEST_EXCLUDED_ROLES`** — none of these is referenced by any test.
* **No test would fail from adding `'Design Head'` to `@role_required(['Design'])`** at
  views.py:893 — no test exercises `dashboard_design`.
* **`test_flag_is_independent_of_role` (#10) is the one to watch.** It loops
  `('Design', 'PM', 'Site Engineer', '')` with the flag set and asserts portfolio view. If
  a migration *removes* the flag branch from `user_can_view_project()` (permissions.py:119)
  in favour of the role string, this test fails on all four rows. Adding the string
  alongside the flag does not affect it.
* **`test_future_role_string_grants_portfolio_view` (#11)** likewise fails if the string
  branch is removed, and passes unchanged otherwise.
* **The `PORTFOLIO_ROWS` / `ASSIGNMENT_ROWS` matrices would need a new row** if `'Design
  Head'` is to be covered as a role. Adding one is additive; not adding one leaves the new
  role untested by the truth table, which currently covers 10 role rows and would then
  cover 10 of 11 legal values.
* **`user_can_edit_project_boq()` has no test at all.** No test in any of the three test
  files calls it, or `user_can_view_project_boq()`, or any design-module permission helper
  (`user_is_design_head`, `user_has_design_head_authority`, `user_can_qc_design`,
  `user_can_view_design`, `user_can_manage_site_groups`, `project_boq_is_group_locked`).
  The BOQ write rule and the entire design-module authority surface — which is where items
  1, 2 and 7 say the damage lands — **would be changed with zero test coverage.**

---

## UNCERTAIN

Things this audit could **not** determine from the code, listed as open rather than guessed.

1. **Production data.** Item 5's numbers are local only. Unknown on Railway: how many users
   hold `is_design_head=True`; whether any user already holds `role='Design Head'` (an Admin
   could have set it since `0048` shipped — five UI surfaces allow it); whether any
   `design_head_deputy` FK is set; and how many tasks, projects and BOQs the real Design
   Head is attached to. **The user must run the item-5 snippet against production.**
2. **The 51 tests do not pass or fail — they did not run** (finding B5). Every statement in
   item 10 about test outcomes is read from source, not observed.
3. **Whether Praveen's 7 local tasks correspond to real production work.** If the real
   Design Head personally executes Design tasks (rather than only reassigning them), losses
   1b#13/#14/#15 are operational, not theoretical. Local data cannot answer this.
4. **Whether the real Design Head authors BOQs.** Praveen is `assigned_design` on exactly
   one local project and has submitted **zero** BOQs. If he authors BOQs in production, loss
   1b#19 is severe; if he only reviews them, it may be intended. `user_can_edit_project_boq`
   deliberately excludes Design Head today (permissions.py:244), so this may already be the
   settled answer — but nobody has confirmed it against real behaviour.
5. **Whether `'Design Head'` needs to appear in `Task.ROLE_CHOICES`.** A Design Head cannot
   hold a task of their own role because no `assigned_role='Design Head'` exists. Whether
   that is correct (Head never holds tasks) or a gap (Head holds Design tasks and must be
   mapped to `'Design'` via `_PROFILE_TO_TASK_ROLE`) is a product question this audit cannot
   answer.
6. **Whether the deputy is meant to be portfolio-wide.** Finding G3 already records that a
   named deputy holds Head authority over every OPEX site with no per-tender scoping. Its
   interaction with a role migration was not investigated because no deputy exists locally.
7. **G4's current behaviour was not re-measured.** No deputy exists on the local database,
   so item 6 confirms the gap by reading unchanged code, not by observation. The Part 4
   session measured it directly.
8. **Whether any external system reads `UserProfile.role`.** The Zoho webhook writes into
   the product; nothing was found that exports role outward. But WhatsApp/email templates
   and any BI or spreadsheet export outside this repository were not searched.
9. **Whether `'Design Head'` fits every column it will land in.** `UserProfile.role` is
   `max_length=20` and the value is 11 characters, so that column is fine. No other column
   stores a `UserProfile.role` value — but this was verified by search, not by an exhaustive
   schema walk.

---

## DECISION POINTS

Stated neutrally. Each must be answered before implementation; this document does not
recommend an answer.

1. **Is `is_design_head` retained alongside the role, or retired?**
   Retained: zero change to the design module (measured — scenario B leaves all 18
   design-module views at 200), but two sources of truth persist indefinitely, and
   `test_flag_is_independent_of_role` documents the flag as working on *any* role including
   a blank one. Retired: `user_is_design_head()` (permissions.py:319-334),
   `user_is_design_head_deputy()` (:337-353), the raw flag read at views.py:3691, and
   `_task_row.html:41` all need role-string branches — and the deputy rule
   (`deputy_for.filter(is_design_head=True)`) loses the predicate that identifies a Head at
   all, so it needs re-expressing regardless.

2. **Is `'Design Head'` added to the existing `'Design'` tuples, or given its own narrower
   set?**
   The tuples in question are enumerated in items 1a, 1b and 1d. They divide into at least
   four groups that could be answered differently:
   *(a)* **routing** — `ROLE_DASHBOARD`, `_ROLE_DASHBOARD`, `@role_required(['Design'])`
   at views.py:893, `tasks_drill_down`'s branch chain (note this one is a *widening* if left
   alone, not a loss);
   *(b)* **"is this person a designer" querysets** — the six `role='Design'` lookups that
   decide who can be selected as `assigned_design` or given a Design task, plus
   `limit_choices_to` at models.py:75. Whether a Head should be assignable as a site's
   designer is a product question, not a migration mechanic;
   *(c)* **task-role matching** — `_PROFILE_TO_TASK_ROLE` (four copies) and
   `_TASK_TO_PROFILE_ROLE` (two copies), which control whether a Head can update, schedule
   or check off a task. A `'Design Head' → 'Design'` entry is the minimal shape; adding
   `'Design Head'` to `Task.ROLE_CHOICES` is the alternative;
   *(d)* **BOQ authorship** — `user_can_edit_project_boq()` (permissions.py:271) and the
   seven `role == 'Design'` gates in `boq_detail.html`. permissions.py:244 currently states
   Design Head gets read and no write **on purpose**; a migration either honours that
   (Head loses BOQ write) or overturns it.

3. **Is the deputy gap (G4) fixed in the same session, or separately?**
   Item 6 establishes that fixing it requires modifying `user_can_view_project_boq()`, a
   Part 0.6 helper that every session since has been forbidden to touch, because a deputy
   can only be admitted by *widening* the gate and a caller cannot widen a False. Part 6
   added a fourth surface where the gap is hit (the group detail screen's per-site BOQ
   links), so it now touches procurement as well as QC. Doing it in the same session means
   one change to that helper instead of two; doing it separately means the role migration
   does not have to carry a Part 0.6 helper edit.

4. **Is the user migrated before, with, or after the code?**
   Item 8 establishes that reversing a role change is a one-field update **provided the code
   has not moved underneath it**. If code and data ship together, "roll back the user" is
   not a rollback — a `'Design'` user on a codebase rewritten around `'Design Head'` is an
   untested third state.

5. **Does `'Design Head'` go into `_SA_EDITABLE_ROLE_CHOICES`?**
   Item 4 shows that leaving it out does not merely prevent a System Admin *assigning* the
   role — it makes any System Admin edit of a user who already holds it fail outright, while
   still listing that user as editable. Adding it also grants System Admins the ability to
   create Design Heads, which may or may not be intended.

6. **What happens to the EOD digest for the new role?**
   Item 1d#6 / item 9: `'Design Head'` is neither in `EOD_DIGEST_EXCLUDED_ROLES` nor given a
   content branch, so a migrated user starts receiving the generic delivery-role digest.
   Options are: hard-exclude (like CEO/Admin/System Admin), leave on the generic digest, or
   add a branch. This is deferred finding D3 becoming live.

7. **Does the truth-table test suite gain a `'Design Head'` row, and does the BOQ /
   design-module permission surface gain any tests at all?**
   Item 10: `user_can_edit_project_boq()`, `user_can_view_project_boq()` and every
   design-module helper currently have **zero** test coverage, and they are precisely the
   functions items 1, 2 and 7 identify as where the migration lands.
