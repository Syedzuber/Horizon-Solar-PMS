# Project Coordinator — Phase 1 Audit Report (Read-Only)

**Status:** AUDIT ONLY. No code was changed. This is the deliverable for Zuber's review before any Phase 2 implementation prompt is written.
**Date:** 2026-07-10
**Scope:** Current `Project` model only (Residential). OPEX/Program-level architecture explicitly out of scope.
**Merged with:** the standing `Project_Overview_Access_Isolation_Audit_Prompt.md` finding — this is one combined report.

---

## 1. Summary

The decision to give Project Coordinators identical execution authority to PM touches **~55 call sites** across `projects/views.py`, plus `projects/utils.py`, `projects/decorators.py`, the two project forms, and ~10 templates. The good news: PM-authority is **not** scattered as raw comparisons everywhere — it funnels through **three helper choke points** and **one repeated inline idiom**:

| Choke point | Location | What it gates | Callers |
|---|---|---|---|
| `_pm_owns_project(request, project)` | `views.py:1586` | Write permission on PM-only execution actions | 9 views |
| `_is_project_pm(profile, project)` | `views.py:4585` | Issue close/reopen + issue-detail template flag | 3 views |
| `is_assigned_pm` (inline in `project_overview`) | `views.py:3726` | The master "can manage this project" flag driving every button on the overview page | 1 view + ~10 template branches |
| Inline isolation guard `if role == 'PM' and project.assigned_pm != profile: raise Http404` | 17 views | View access / write for PM-role users | 17 views |

If Phase 2 introduces `user_can_manage_project(user, project)` and routes those three helpers plus the 17 inline guards through it, the vast majority of the change is mechanical and low-risk. The remaining work is: the PM-dashboard/drill-down querysets (Category 7), notification routing (Section 4), and a small set of judgment-call actions that may legitimately stay PM-only (Section 5).

**Two structural warnings that affect the whole design (read before Phase 2):**

- **`assigned_pm` is a `ForeignKey` to `UserProfile`** (`models.py:34`), but the planned `Project.coordinators` M2M is specified as "to `User`". The codebase already compares PM ownership **three different ways** — `assigned_pm != profile` (UserProfile compare, 17 sites), `assigned_pm.user == request.user` (User compare, the 2 helpers + `is_assigned_pm`), and `.filter(assigned_pm=profile)` (queryset, dashboards). `user_can_manage_project()` must pick one canonical type and every site must be normalised to it, or the OR-check will silently compare the wrong objects. **Recommendation for Zuber's consideration:** make `coordinators` an M2M to `UserProfile` for consistency with `assigned_pm`, not to `User`.
- `decorators.py` `ROLE_DASHBOARD` (line 10) has **no `Project Coordinator` entry** — a coordinator would silently fall back to the admin dashboard. Must be added.

---

## 2. Call site inventory

Legend for "Gates": **VIEW** = page access, **WRITE** = a state-mutating action, **BTN** = button/UI visibility, **QRY** = dashboard queryset, **NOTIFY** = notification recipient.

### Category A — The 3 helper choke points (change these first; most sites inherit the fix)

| File:Line | Current check | Gates | Proposed change |
|---|---|---|---|
| `views.py:1586-1588` | `def _pm_owns_project`: `return project.assigned_pm is not None and project.assigned_pm.user == request.user` | WRITE | Replace body with `user_can_manage_project(request.user, project)` (OR of assigned_pm + coordinators). **Single highest-leverage change.** |
| `views.py:4585-4587` | `def _is_project_pm`: `return profile.role == 'PM' and project.assigned_pm == profile` | WRITE (issue close/reopen) | Route through capability fn. **Note:** this one bakes in `role == 'PM'`; if a coordinator has role `Project Coordinator`, the role-string half must be dropped in favour of the capability check, or coordinators are silently excluded even when in `coordinators`. |
| `views.py:3726` | `is_assigned_pm = (project.assigned_pm is not None and project.assigned_pm.user == request.user)` | WRITE + BTN | Route through capability fn. Drives POST-action gate (3730), `can_assign_design` (3727), candidates (3968), `show_cascade_option` (3986), and ~10 template `{% if is_assigned_pm %}` branches. |

### Category B — `_pm_owns_project()` callers (all already `@role_required(['PM'])`)

Each of these is a PM-only execution action. **For coordinators, BOTH the `@role_required(['PM'])` decorator AND the `_pm_owns_project` call must admit coordinators.** These are exactly the "actions PM takes to execute the project" that must drizzle down.

| File:Line | View | Gates | Notes |
|---|---|---|---|
| `views.py:1668` | `project_edit` | WRITE | Draft-only edit. |
| `views.py:1709` | `project_activate` | WRITE | Activates project, seeds tasks + milestones + designer. High-impact execution action. |
| `views.py:1770` | `project_recalculate_dates` | WRITE | Bulk due-date reset. |
| `views.py:1798` | `enable_cascade_scheduling` | WRITE | **Irreversible** config toggle (raises `PermissionDenied`, not 404). See Section 5 — possibly PM-only. |
| `views.py:1839` | `task_add` | WRITE | |
| `views.py:1898` | `task_status_update` (`is_pm = _pm_owns_project`) | WRITE | Permission is `assigned_role match OR is_pm` (line 1904). Fixing the helper covers it. |
| `views.py:2244` | `task_assign` | WRITE | Assign a user to a task. |
| `views.py:2390` | `task_set_due_date` (`is_pm`) | WRITE | PM path cascades; non-PM path is role-owner only. |
| `views.py:3253` | `milestone_create` | WRITE | Creates M1/M2/M3 shells (not a Finance action — see Section 3). |

### Category C — `_is_project_pm()` callers

| File:Line | View | Gates | Notes |
|---|---|---|---|
| `views.py:4891` | `issue_detail` (`is_pm` → template) | BTN | Controls close/reopen button visibility. |
| `views.py:5035` | `close_issue` | WRITE | "Only the project PM can close." See Section 5 — supervisory vs execution decision. |
| `views.py:5068` | `reopen_issue` | WRITE | Same as above. |

### Category D — Inline isolation guard `if role == 'PM' and project.assigned_pm != profile: raise Http404`

These restrict **only** PM-role users to their own projects; every other role passes through unrestricted (this is the standing access-isolation gap — see Secondary findings). For coordinators, the guard shape must become `if role in ('PM', 'Project Coordinator') and not user_can_manage_project(...)`. **This is where the Exclusivity-bug risk is highest** — see Section "Invariant risk" per row.

| File:Line | View | Gates |
|---|---|---|
| `views.py:3723` | `project_overview` | VIEW |
| `views.py:4280` | `task_detail` | VIEW |
| `views.py:4333` | `upload_project_document` | WRITE |
| `views.py:4427` | `delete_project_document` | WRITE |
| `views.py:4467` | `upload_task_attachment` | WRITE |
| `views.py:4553` | `delete_task_attachment` | WRITE |
| `views.py:4602` | `create_project_issue` | WRITE |
| `views.py:4693` | `create_task_issue` | WRITE |
| `views.py:4788` | `create_delivery_issue` | WRITE |
| `views.py:4887` | `issue_detail` | VIEW |
| `views.py:4926` | `update_issue_status` | WRITE |
| `views.py:4966` | `resolve_issue` | WRITE |
| `views.py:5102` | `assign_issue` | WRITE |
| `views.py:5144` | `create_task_comment` | WRITE |
| `views.py:5194` | `create_issue_comment` | WRITE |
| `views.py:5280` | `project_timeline` | VIEW |
| `views.py:5567` | `delivery_challan_detail` | VIEW (plus role-list at 5563) |

### Category E — Role-list checks (no owner-scoping). Decide per-view whether Coordinator joins the list

| File:Line | View | Current check | Gates | Note |
|---|---|---|---|---|
| `views.py:2766` | `boq_detail` | `role not in ('Design','SCM','PM','Admin')` | VIEW+WRITE | BOQ page. Add coordinator → drizzles PM's BOQ visibility. |
| `views.py:3031` | `boq_request_revision` | `profile.role != 'PM'` | WRITE | **No owner check** — any PM can revise any project's BOQ. Should become capability check (also closes the isolation gap). |
| `views.py:3083` | `boq_history` | `role not in ('PM','Design','SCM','Admin')` | VIEW | Add coordinator. |
| `views.py:3630` | `set_milestone_amounts` | `@role_required(['BD','PM'])` | WRITE | **No owner check.** Setting amounts is execution → add coordinator. Note it is NOT confirmation (Section 3). |
| `views.py:5563` | `delivery_challan_detail` | `role not in ('SCM','PM','Site Engineer','Admin')` | VIEW | Add coordinator. |
| `views.py:5890` | `payment_request_detail` | `role not in ('SCM','Finance','PM','Admin')` | VIEW | Read-only. Add coordinator if they should see vendor-payment records. |
| `views.py:5878` | `design_submission_detail` | `profile != submitted_by and role not in ('PM','Admin')` | VIEW | Read-only. Consider coordinator. |
| `views.py:1592` | `project_list` (redirect hub) | `@role_required(['PM','Admin','CEO'])` | VIEW | Add coordinator; route to their landing dashboard. |

### Category F — `@role_required(['PM'])` decorators (view-access gate)

The decorator itself blocks coordinators before any body runs. Every Category-B view carries one; additionally:

| File:Line | View | Note |
|---|---|---|
| `views.py:254` | `dashboard_pm` | Coordinator dashboard is Phase 2 UI (out of scope), but note: a coordinator cannot currently reach any PM landing page. |
| `views.py:1610` | `project_create` | **See Section 5** — self-assigns `assigned_pm = request.user.profile` (line 1622). A coordinator is not a PM; likely stays PM-only. |

### Category G — PM dashboard & drill-down querysets (`assigned_pm=pm_profile`)

For a coordinator to have project visibility, these need `Q(assigned_pm=profile) | Q(coordinators=profile)`. The **PM's own** dashboard queryset does **not** need to change for the PM (adding coordinators elsewhere doesn't remove the PM's projects) — but see the edge case note.

| File:Line(s) | View | Query |
|---|---|---|
| `views.py:205` | `tasks_drill_down` (role=='PM' branch) | `phase__project__assigned_pm=profile` |
| `views.py:266,272,280,285,291,299,306` | `dashboard_pm` (summary + project loop) | `assigned_pm=pm_profile` / `phase__project__assigned_pm=pm_profile` |
| `views.py:409,423,430,435,441,447,454,459` | `dashboard_pm` (task lists, DC issues, due-date changes) | `...assigned_pm=pm_profile` |

**Edge case to flag:** a user who is a PM on project X but a *coordinator* on project Y would, on the current `dashboard_pm` queryset, not see Y. Whether a PM-role user can also be listed in another project's `coordinators` is a policy question for Zuber.

### Category H — Write paths that assign `assigned_pm` via the single-FK pattern (Overwrite-bug template)

These are **not** PM-authority checks, but they are the exact single-FK reassignment pattern the invariant warns Phase 2 must **not** copy for "assign Coordinator".

| File:Line | View | Pattern |
|---|---|---|
| `views.py:6476-6478` | `admin_assign_pm` | `old_pm = project.assigned_pm; project.assigned_pm = pm_profile; project.save(update_fields=['assigned_pm'])` |
| `views.py:6748-6749` | `subadmin_projects` | `project.assigned_pm = pm_profile; project.save(update_fields=['assigned_pm'])` (first-time only guard at 6743) |
| `views.py:3796-3805` | `project_overview` `assign_design` action | `project.assigned_design = design_user; project.save(update_fields=['assigned_design'])` — the **`assigned_design` single-FK reassignment** explicitly cited in the invariant as the anti-pattern. |
| `views.py:1622` | `project_create` | `project.assigned_pm = request.user.profile` |
| `views.py:4141` | `zoho_deal_closed_webhook` | `assigned_pm=assigned_pm` at creation |

`assigned_pm` is **not** exposed as a field on `ProjectCreateForm` (`forms.py:242`) or `ProjectEditForm` (`forms.py:288`) — so no form can silently overwrite it. Good. The overwrite risk is confined to the three assignment views above.

---

## 3. Finance / PM milestone boundary confirmation

**Assumption ("PM does NOT confirm payment milestones; that is exclusively Finance"): CONFIRMED — with one indirect caveat flagged below.**

Evidence:

| Action | View | Gate | Verdict |
|---|---|---|---|
| Mark milestone **Invoiced** (Pending→Invoiced) | `milestone_invoice` `views.py:3152` | `@role_required(['Finance'])` | Finance only ✅ |
| Mark milestone **Received** (Invoiced→Received) | `milestone_receive` `views.py:3178` | `@role_required(['Finance'])` | Finance only ✅ |
| Mark **Received** via overview inline | `project_overview` update_milestone, `views.py:3740-3769` | `_actor_role == 'Finance'` sets `status='Received'`; the PM branch (`views.py:3770-3782`) sets **only** description/amount/due_date, never status | Finance only for status ✅ |
| Confirm **vendor payment** (PaymentRequest) | `confirm_payment_request` `views.py:3383` | `if profile.role != 'Finance': 403` | Finance only ✅ |
| **Create** M1/M2/M3 shells | `milestone_create` `views.py:3241` | `@role_required(['PM'])` + owner | PM execution — SHOULD drizzle to coordinator |
| **Set** milestone amounts | `set_milestone_amounts` `views.py:3630` | `@role_required(['BD','PM'])` | BD/PM execution — SHOULD drizzle |

So the two roles are cleanly separated: **Finance confirms (Invoiced/Received); PM creates shells and sets amounts.** No PM path writes milestone `status`.

**Caveat (do not lose this):** there is an **indirect** route by which a PM already flips a milestone to `Received`. In `task_status_update` (`views.py:1998-2024`) and `task_detail_status_update` (`views.py:2171-2196`), marking a Finance-confirmation *task* (`Advance Payment Confirmation` / `Finance Confirmation` / `100% Payment Confirmation`) as **Done** auto-updates the matching `PaymentMilestone` to `status='Received'`. Those tasks have `assigned_role='Finance'`, and `task_status_update` permits the action if the actor is the assigned role **or** the project PM (`is_pm`, line 1904). Therefore a PM can today mark a Finance task Done and thereby set the milestone Received. Because coordinators inherit identical task authority, **coordinators would inherit this indirect milestone-Received capability**. This does not contradict the board's statement (PM does not use the *milestone confirm* action) but Zuber should be aware the boundary is not airtight at the task layer. **Flagged, not decided.**

---

## 4. Notification routing findings

Every place a notification is routed "to the PM" resolves the recipient via `project.assigned_pm` (a single `UserProfile`). None of them currently consider coordinators. When coordinators exist, each of these must decide: include coordinators, or keep PM-only.

| File:Line | Trigger | Current recipient resolution |
|---|---|---|
| `views.py:2031-2032` | Payment-milestone task Done | Finance (all) + `project.assigned_pm` + BD/CEO (all) |
| `views.py:2201-2202` | Payment-milestone task Done (task-detail path) | Same as above |
| `views.py:2671-2672` | BOQ acknowledged (`_notify_boq_acknowledged`) | `boq.project.assigned_pm` + acknowledging SCM |
| `views.py:3422-3423` | Vendor payment confirmed | SCM (all) + `project.assigned_pm` + CEO (all) |
| `views.py:4668-4670` | Project issue raised | `project.assigned_pm` (in-app), unless PM is raiser/assignee |
| `views.py:4761-4763` | Task issue raised | `project.assigned_pm` (in-app) |
| `views.py:4864-4866` | Delivery issue raised | `project.assigned_pm` (in-app) |
| `views.py:5003` | Issue resolved | `[project.assigned_pm, issue.assigned_to, issue.raised_by]` |
| `views.py:4219-4220` | New project (Zoho webhook) | `project.assigned_pm` |
| `views.py:6503-6516` | Admin assigns PM | the newly assigned PM (this *is* the assignment) |

**Observation for Phase 2:** all ten sites take `project.assigned_pm` as a single object and either append it to a recipient list or pass it directly. The cleanest change is a helper like `project_managers(project)` returning `[assigned_pm] + list(coordinators)` (deduped), used everywhere `project.assigned_pm` is currently appended. The board's "included or distinguished" question (do coordinators get the same WhatsApp template, or a variant?) is a product decision, not a code constraint — flagged for Zuber.

---

## 5. Checks that may NOT be simple "drizzle to coordinator" — flagged, not decided

These look like PM checks but may be seniority/approval/config rather than day-to-day execution. **I am not deciding these — surfacing them for Zuber.**

1. **`project_create` (`views.py:1610`, self-assigns `assigned_pm` at 1622).** Creating a brand-new project and becoming its PM is an ownership-establishing act, not execution on an existing project. A coordinator is not a PM; letting one create a project would either mis-set `assigned_pm` to a non-PM user or need a separate rule. **Likely stays PM-only.**
2. **`close_issue` / `reopen_issue` (`views.py:5022`, `5055`).** The code comments and the model docstring (`models.py:611`) frame close/reopen as a PM-gated lifecycle step ("Closed (by PM only)"). This reads as supervisory sign-off rather than execution. Board says coordinators get identical execution authority — so this probably drizzles — but it is the one issue action that is deliberately PM-restricted today. **Confirm intent.**
3. **`enable_cascade_scheduling` (`views.py:1787`).** Irreversible per-project scheduling-mode change gated behind an Admin feature flag. This is configuration, not routine execution. **Consider keeping PM-only.**
4. **Indirect milestone-Received via Finance-task completion** (Section 3 caveat). Coordinators would inherit it. **Confirm acceptable.**
5. **`set_milestone_amounts` / `milestone_create`** — execution and almost certainly should drizzle, but they currently have **no owner-scoping at all** (any BD/PM can act on any project). Applying `user_can_manage_project` here would both add coordinators *and* incidentally tighten the isolation gap. Note the dual effect so it is intentional, not a surprise.

---

## Invariant risk per site (per the Phase-1 mandate)

The prompt requires flagging, per site, whether the code shape makes the **Exclusivity bug** or **Overwrite bug** easy to introduce.

**Exclusivity-bug risk (locking PM out when a coordinator is assigned):**
- **HIGHEST at Category D (the 17 inline guards) and Category A helpers.** Each currently reads `assigned_pm != profile`. The tempting-but-wrong rewrite is `if project.coordinators.exists(): check coordinators else check assigned_pm`. The correct form is a flat OR: `assigned_pm == user OR user in coordinators`. Because these guards are copy-pasted verbatim 17×, a single wrong mental model gets replicated 17 times. **Phase 2 must implement the OR once inside `user_can_manage_project()` and never inline the branch.** Recommend the function body literally be `return project.assigned_pm_id == profile_id or project.coordinators.filter(pk=...).exists()` with no `if coordinators:` shortcut anywhere.
- The `_is_project_pm` helper (`views.py:4587`) additionally hard-codes `role == 'PM'`; naively keeping that conjunct excludes coordinators even when correctly listed. It must be dropped in favour of the pure capability check.

**Overwrite-bug risk (transferring ownership away from PM):**
- **HIGHEST if the "assign Coordinator" write path is modelled on Category H.** `admin_assign_pm` (6477), `subadmin_projects` (6748), and especially `project_overview`'s `assign_design` (3796) all use `project.<fk> = user; project.save(update_fields=[...])`. Copying that shape for coordinators would do `project.assigned_pm = coordinator` (ownership theft) or overwrite a single coordinator slot. The coordinator write path must use `project.coordinators.add(user)` / `.remove(user)` on the M2M and **must never touch `assigned_pm`**. Recommend the Phase 2 coordinator-assign view be written from scratch, not cloned from `admin_assign_pm`.

---

## 6. Secondary findings (out of scope — noted, not chased)

1. **Access-isolation gap (the merged standing finding), confirmed still open.** Every project-scoped view isolates **only** `role == 'PM'` to their own projects (Category D). All other authenticated roles — Design, SCM, Finance, Site Engineer, BD, CEO — can open **any** project's `project_overview`, `task_detail`, `project_timeline`, BOQ, DC, and issue pages by guessing/enumerating the URL. There is no per-project scoping for non-PM roles.
2. **Issue-keyed views have near-zero role gating even structurally.** `issue_detail`, `update_issue_status`, `resolve_issue`, `assign_issue` (issue IDs are sequential integers) apply only the PM-isolation guard; any authenticated non-PM user can view, start, resolve, or reassign **any** issue on **any** project by ID. Only `close_issue`/`reopen_issue` are PM-gated. This confirms the prompt's "zero role checks even for PM" concern — for these four, there is no role restriction at all beyond the PM-only-sees-own-projects clause.
3. **`boq_request_revision` (3031) and `set_milestone_amounts` (3630) have no owner-scoping** — any PM/BD can act cross-project. Called out in Category E; applying the capability function fixes it as a side effect.
4. **Type inconsistency** between `assigned_pm` (FK→`UserProfile`) and the planned `coordinators` (spec says M2M→`User`), plus three comparison idioms in the code. Detailed in Section 1. This is the single most important thing to settle **before** writing the capability function.
5. **`ROLE_DASHBOARD` / `get_user_dashboard` (`decorators.py:10-36`)** has no `Project Coordinator` key → coordinators silently land on the admin dashboard. Also `UserProfile.ROLE_CHOICES` (`models.py:268`) must gain the `Project Coordinator` choice, and there is **no** corresponding `Task.ROLE_CHOICES` entry (coordinators are not a task-executing role) — the `_TASK_TO_PROFILE_ROLE` / `_PROFILE_TO_TASK_ROLE` mapping dicts (e.g. `views.py:2250`, `3965`, `3981`) may need a no-op mapping so coordinator role strings don't leak into task-role comparisons.

---

## Hard stop

Report complete. No files were modified. Next step is Zuber's review; the Phase 2 implementation prompt (capability function + migration + call-site routing) should not be written or run until that review is done.
