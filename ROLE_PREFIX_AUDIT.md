# Role-Prefix Permission Short-Circuit — Investigate-Only Audit

**Date:** 2026-07-26
**Scope:** Establish the current effective access matrix produced by role-prefixed permission checks.
**Nature:** Investigation only. No code written, edited, or deleted outside this file and `FINDINGS_SECONDARY.md`. No site fixed. No fix, refactor, or policy proposed. No migration. No write command against the database.

**Method:** Derived entirely by reading source. No runtime probing was performed — nothing in this report requires a live request to establish, and where a claim *would* require one, it is marked UNKNOWN.

---

## HARD STOP CONDITIONS — evaluated

| Condition | Result |
|---|---|
| Total site count across all variants exceeds **30** | **NOT triggered** — total is **22**. See §1.7. |
| `user_can_manage_project` contains role-specific branching internally | **NOT triggered** — the function is entirely role-blind. Full body at §2.1; it reads no role field on any code path. See §2.3. |
| Any site's role prefix guards a permission function **other than** `user_can_manage_project` | **NOT triggered — but this was a close call.** See the judgment call below. |
| More than one function claims to be the canonical project-authority check | **NOT triggered** — exactly one claims it; the other two explicitly disclaim. See §2.4. |

### Judgment call on hard-stop condition 3 — recorded so it can be overruled

`_can_access_program` ([views.py:2098-2110](projects/views.py#L2098-L2110)) contains a role check that short-circuits a **different** permission function, `user_can_manage_program`:

```python
role = _get_user_role(request)
if role in ('Admin', 'CEO'):
    return True                                    # <- short-circuits the call below
if user_can_manage_program(request.user, program):
    return True
return program.created_by_id == request.user.id
```

I judged this **not** to trigger the stop, for three reasons:

1. **Opposite polarity.** The pattern under audit is `role == X and not perm(): deny` — a role prefix that causes a **deny-check to be skipped**, so unmatched roles fall through *unexamined*. Here the role check is an **early-return grant**, and every role receives an explicit decision on some branch. Nobody falls through unexamined.
2. **Different object.** `user_can_manage_program` decides authority over a `Program`, not a `Project`. This audit's subject is project authority.
3. **Different function, and it is documented as intentional** — [views.py:2100-2101](projects/views.py#L2100-L2101): *"Admin / CEO reach every Program (role_required already limits who gets here)."*

Every one of the 22 enumerated Section 1 sites guards `user_can_manage_project`. If you consider `_can_access_program` in scope, say so and I will re-scope — I have not analysed Program-side authority beyond noting its existence here.

**No hard stop applies. Full audit proceeds.**

---

# SECTION 1 — Complete enumeration

Six structurally distinct variants were found. I searched for all shapes listed in the prompt plus regex sweeps for multiline role/permission conjunctions, precomputed-permission-result combinations, and direct `assigned_pm`/`coordinators` comparisons adjacent to role checks.

## 1.1 Variant A — `role == 'PM' and not user_can_manage_project(...)` → `raise Http404`

**17 sites.** The dominant shape. Two textual forms, semantically identical: 16 use `profile.role`, one ([views.py:4924](projects/views.py#L4924)) uses a local `role` variable bound from `profile.role` at [views.py:4921](projects/views.py#L4921).

| # | File:Line | Enclosing function | Decorators | Exact expression | URL name(s) |
|---|---|---|---|---|---|
| A1 | [views.py:4924](projects/views.py#L4924) | `project_overview` ([:4908](projects/views.py#L4908)) | `@login_required` | `if role == 'PM' and not user_can_manage_project(request.user, project):` | `project_overview` ([urls.py:96](projects/urls.py#L96)); also reached via `project_detail` ([urls.py:48](projects/urls.py#L48)) which 302s to it ([views.py:1746](projects/views.py#L1746)) |
| A2 | [views.py:5528](projects/views.py#L5528) | `task_detail` ([:5519](projects/views.py#L5519)) | `@login_required` | `if profile.role == 'PM' and not user_can_manage_project(request.user, project):` | `task_detail` ([urls.py:117](projects/urls.py#L117)) |
| A3 | [views.py:5585](projects/views.py#L5585) | `upload_project_document` ([:5572](projects/views.py#L5572)) | `@login_required` | same | `upload_project_document` ([urls.py:123](projects/urls.py#L123)) |
| A4 | [views.py:5679](projects/views.py#L5679) | `delete_project_document` ([:5667](projects/views.py#L5667)) | `@login_required` | same | `delete_project_document` ([urls.py:125](projects/urls.py#L125)) |
| A5 | [views.py:5719](projects/views.py#L5719) | `upload_task_attachment` ([:5706](projects/views.py#L5706)) | `@login_required` | same | `upload_task_attachment` ([urls.py:131](projects/urls.py#L131)) |
| A6 | [views.py:5812](projects/views.py#L5812) | `delete_task_attachment` ([:5801](projects/views.py#L5801)) | `@login_required` | same | `delete_task_attachment` ([urls.py:133](projects/urls.py#L133)) |
| A7 | [views.py:5983](projects/views.py#L5983) | `create_project_issue` ([:5972](projects/views.py#L5972)) | `@login_required` | same | `create_project_issue` ([urls.py:146](projects/urls.py#L146)) |
| A8 | [views.py:6077](projects/views.py#L6077) | `create_task_issue` ([:6066](projects/views.py#L6066)) | `@login_required` | same | `create_task_issue` ([urls.py:148](projects/urls.py#L148)) |
| A9 | [views.py:6175](projects/views.py#L6175) | `create_delivery_issue` ([:6162](projects/views.py#L6162)) | `@login_required` | same | `create_delivery_issue` ([urls.py:259](projects/urls.py#L259)) |
| A10 | [views.py:6277](projects/views.py#L6277) | `issue_detail` ([:6268](projects/views.py#L6268)) | `@login_required` | same | `issue_detail` ([urls.py:150](projects/urls.py#L150)) |
| A11 | [views.py:6316](projects/views.py#L6316) | `update_issue_status` ([:6303](projects/views.py#L6303)) | `@login_required` | same | `update_issue_status` ([urls.py:152](projects/urls.py#L152)) |
| A12 | [views.py:6356](projects/views.py#L6356) | `resolve_issue` ([:6342](projects/views.py#L6342)) | `@login_required` | same | `resolve_issue` ([urls.py:154](projects/urls.py#L154)) |
| A13 | [views.py:6492](projects/views.py#L6492) | `assign_issue` ([:6479](projects/views.py#L6479)) | `@login_required` | same | `assign_issue` ([urls.py:160](projects/urls.py#L160)) |
| A14 | [views.py:6534](projects/views.py#L6534) | `create_task_comment` ([:6524](projects/views.py#L6524)) | `@login_required` | same | `create_task_comment` ([urls.py:167](projects/urls.py#L167)) |
| A15 | [views.py:6595](projects/views.py#L6595) | `create_issue_comment` ([:6585](projects/views.py#L6585)) | `@login_required` | same | `create_issue_comment` ([urls.py:170](projects/urls.py#L170)) |
| A16 | [views.py:6681](projects/views.py#L6681) | `project_timeline` ([:6673](projects/views.py#L6673)) | `@login_required` | same | `project_timeline` ([urls.py:180](projects/urls.py#L180)) |
| A17 | [views.py:6968](projects/views.py#L6968) | `delivery_challan_detail` ([:6953](projects/views.py#L6953)) | `@login_required` | same | `delivery_challan_detail` ([urls.py:250](projects/urls.py#L250)) |

**A17 is materially different from the other sixteen** and must not be lumped with them: it is preceded at [views.py:6964](projects/views.py#L6964) by a role allowlist that returns 403 —
```python
if profile.role not in ('SCM', 'PM', 'Project Coordinator', 'Site Engineer', 'Admin'):
    return HttpResponseForbidden()
```
so only five roles ever reach the short-circuit. Design, Finance, BD and CEO are excluded before it.

## 1.2 Variant B — role-set conjunction inside a helper

**1 site.**

| # | File:Line | Function | Exact expression |
|---|---|---|---|
| B1 | [views.py:5968](projects/views.py#L5968) | `_is_project_pm(profile, project)` ([:5959](projects/views.py#L5959)) | `return profile.role in ('PM', 'Project Coordinator') and user_can_manage_project(profile.user, project)` |

**This is the one variant that does not leak**, and it is the closest thing to a stated intent in the codebase. Its docstring ([views.py:5960-5967](projects/views.py#L5960-L5967)) explains the role gate is deliberate: *"the role gate is kept (and now includes Project Coordinator) so the pathological webhook case — where `assigned_pm` could point to a non-manager profile — is still excluded."*

That pathological case is **real and reachable**: the Zoho webhook at [views.py:5369](projects/views.py#L5369) sets `assigned_pm` from an email match with **no role filter** —
```python
profile_match = UserProfile.objects.filter(user__email__iexact=pm_email).first()
```
so a Finance or Design profile whose email appears in the Zoho `Assign_PM` field becomes `assigned_pm`. `limit_choices_to={'role': 'PM'}` on the field ([models.py:63](projects/models.py#L63)) is a form-layer constraint only and does not apply here.

**Consumers of B1** — these are correctly gated (role AND permission, both required):

| Consumer | File:Line | Behaviour on failure |
|---|---|---|
| `close_issue` | [views.py:6425](projects/views.py#L6425) | `HttpResponseForbidden('Only the project PM can close issues.')` |
| `reopen_issue` | [views.py:6458](projects/views.py#L6458) | `HttpResponseForbidden('Only the project PM can reopen issues.')` |
| `issue_detail` (template flag `is_pm`) | [views.py:6281](projects/views.py#L6281) | Renders controls hidden, page still shown |

## 1.3 Variant C — permission result OR'd with a role comparison

**1 site.**

| # | File:Line | Function | Exact expression |
|---|---|---|---|
| C1 | [views.py:4931](projects/views.py#L4931) | `project_overview` POST dispatch | `if request.method == 'POST' and (is_assigned_pm or (role == 'Finance' and request.POST.get('action') == 'update_milestone')):` |

`is_assigned_pm` is the permission result computed at [views.py:4927](projects/views.py#L4927). This is a **grant**, not a deny — it widens POST authority to Finance for one action. Unlike Variant A it does not skip a check; it adds an alternative. Listed for completeness per the prompt's "any conjunction or disjunction of a role comparison with a permission call".

## 1.4 Variant D — role AND'd with a precomputed permission result

**1 site.**

| # | File:Line | Function | Exact expression |
|---|---|---|---|
| D1 | [views.py:5206](projects/views.py#L5206) | `project_overview` (template context) | `show_cascade_option = (_sys.cascade_scheduling_enabled and role == 'PM' and is_assigned_pm)` |

A conjunction, so it **narrows** rather than leaks. Its visible effect: a **Project Coordinator who genuinely manages the project** is denied the cascade-scheduling option, because `role == 'PM'` excludes them even though `is_assigned_pm` is True. That is the inverse of the Variant A defect — over-restriction, not under-restriction. UI-only; the enforcing endpoint is `enable_cascade_scheduling` ([urls.py:60](projects/urls.py#L60)), gated by `_pm_owns_project` at [views.py:2044](projects/views.py#L2044) with no role prefix.

## 1.5 Variant E — De Morgan form, task-role compared against a permission result

**1 site.**

| # | File:Line | Function | Exact expression |
|---|---|---|---|
| E1 | [views.py:2915](projects/views.py#L2915) | `task_status_update` ([:2877](projects/views.py#L2877)) | `if normalised_user_role != task.assigned_role and not is_pm:` → `HttpResponseForbidden()` |

`is_pm` is `_pm_owns_project(request, project)` from [views.py:2909](projects/views.py#L2909); `normalised_user_role` maps `UserProfile.role` through `{'BD': 'BD / Sales'}` at [views.py:2912-2913](projects/views.py#L2912-L2913). Logically `deny unless (role matches the task's assigned_role) OR (manages the project)`.

**This one does not leak by role**, because the comparison is against `task.assigned_role` — a per-task value — not a fixed role literal. A Finance user reaches this only for tasks whose `assigned_role` is Finance. URL: `task_status_update` ([urls.py:55](projects/urls.py#L55)).

## 1.6 Variant F — permission OR role-match, inside a helper

**1 site.**

| # | File:Line | Function | Exact expression |
|---|---|---|---|
| F1 | [views.py:1687-1692](projects/views.py#L1687-L1692) | `_user_can_complete_checklist_item(user, task, project)` ([:1674](projects/views.py#L1674)) | `if user_can_manage_project(user, project): return True` … `return normalised_user_role == task.assigned_role` |

Same logical shape as E1 (permission OR task-role match), expressed as sequential returns. Does not leak by role for the same reason. Consumed by `checklist_item_complete` at [views.py:5880](projects/views.py#L5880) → 403 on failure; URL `checklist_item_complete` ([urls.py:140](projects/urls.py#L140)).

## 1.7 Exact total

| Variant | Shape | Sites | Leaks by role? |
|---|---|---|---|
| A | `role == 'PM' and not perm()` → deny | **17** | **YES — 16 broadly, 1 (A17) narrowed by a preceding allowlist** |
| B | `role in (...) and perm()` | 1 | No (conjunction) |
| C | `perm() or (role == 'Finance' and …)` | 1 | Grant, by design |
| D | `role == 'PM' and perm_result` | 1 | No — over-restricts instead |
| E | `role != task.assigned_role and not perm_result` → deny | 1 | No (compares to a per-task value) |
| F | `perm() or role == task.assigned_role` | 1 | No (same reason as E) |
| **Total** | | **22** | **17 leak-shaped, of which 16 leak broadly** |

**The total is 22, against the prompt's estimate of ~20. Stating this explicitly as instructed: the figure differs, by +2.** The difference is not new Variant-A sites — Variant A is exactly the 17 you would find searching for the literal string. The extra sites are Variants C, D, E and F, which only surface when searching for disjunctions, precomputed permission results, and De Morgan forms. Four of those five non-A variants do **not** exhibit the defect.

## 1.8 `assigned_pm` / `coordinators` compared directly, adjacent to a role check

Per the prompt's final search item. None of these calls `user_can_manage_project`; all re-express ownership at the queryset level, where a single-object helper cannot be used.

| File:Line | Context | Expression | Adjacent role check |
|---|---|---|---|
| [views.py:283-285](projects/views.py#L283-L285) | `dashboard_pm` | `Q(assigned_pm=pm_profile) \| Q(coordinators=pm_profile)` | `@role_required(['PM','Project Coordinator'])` at [:267](projects/views.py#L267) |
| [views.py:215-218](projects/views.py#L215-L218) | `tasks_drill_down` | `Q(phase__project__assigned_pm=profile) \| Q(phase__project__coordinators=profile)` | `if role in ('PM','Project Coordinator')` at [:211](projects/views.py#L211) |
| [views.py:707-708](projects/views.py#L707-L708) | `dashboard_design` | `Q(assigned_design=design_profile) \| Q(phases__tasks__assigned_to=design_profile)` | `@role_required(['Design'])` at [:693](projects/views.py#L693) |
| [views.py:2303](projects/views.py#L2303) | `create_opex_site` | `site.assigned_pm = profile if (profile and profile.role == 'PM') else None` | inline |
| [views.py:8453](projects/views.py#L8453) | `subadmin_projects` | `if project.assigned_pm_id:` (first-assignment guard) | `@role_required(['System Admin'])` at [:8439](projects/urls.py#L223) |

These are **three separate re-expressions of the PM-or-coordinator rule** (rows 1, 2 and the canonical helper), each of which must be edited in lockstep with `permissions.py`. They are not Section 1 defect sites — they are maintenance coupling.

---

# SECTION 2 — `user_can_manage_project` ground truth

## 2.1 Full body

[permissions.py:12-39](projects/permissions.py#L12-L39). Executable lines are [:34-39](projects/permissions.py#L34-L39).

```python
def user_can_manage_project(user, project):
    """
    Return True if `user` has PM-level management authority on `project`.

    Authority is the UNCONDITIONAL OR of two additive sources:
        assigned PM  OR  a Project Coordinator on this project.

    This is the one canonical comparison path — every PM-ownership check routes
    through here, so adding coordinator support here gives every call site correct
    behaviour with no further edits.

    INVARIANT (additive-only): the assigned-PM check is evaluated FIRST and never
    gated on whether coordinators exist. Assigning a coordinator can only ever add
    a manager — it can never remove the PM's authority. Do not restructure this as
    "if coordinators: check coordinators else check PM" — that would silently lock
    the PM out. The OR is unconditional and lives here, not at any call site.

    `Project.assigned_pm` and `coordinators` are both to `UserProfile`, so we
    compare against `user.profile`. `getattr` guards a user with no profile
    (e.g. a superuser created via `createsuperuser`). A null `assigned_pm`
    compares False, matching the old `is not None` guards.
    """
    profile = getattr(user, 'profile', None)
    if profile is None:
        return False
    if project.assigned_pm == profile:          # PM authority — always checked, never gated
        return True
    return project.coordinators.filter(pk=profile.pk).exists()  # additive coordinator authority
```

## 2.2 Return-value truth table

**The function is role-blind.** It branches only on two relational facts: does `project.assigned_pm` equal this profile, and is this profile in `project.coordinators`. `UserProfile.role` is never read. Therefore **every row of the table is identical**, and that uniformity is the finding — not an artefact of how the table is laid out.

| Role | Assigned as PM on this project | Coordinator on this project | No relationship |
|---|---|---|---|
| PM | **True** — [:37-38](projects/permissions.py#L37-L38) | **True** — [:39](projects/permissions.py#L39) | **False** — [:39](projects/permissions.py#L39) |
| Site Engineer | **True** | **True** | **False** |
| SCM | **True** | **True** | **False** |
| Finance | **True** | **True** | **False** |
| Design | **True** | **True** | **False** |
| Sales & BD (`'BD'`) | **True** | **True** | **False** |
| CEO | **True** | **True** | **False** |
| Admin | **True** | **True** | **False** — no superuser/staff/Admin bypass exists |
| Project Coordinator | **True** | **True** | **False** |

### Reachability of the two True columns for non-PM / non-Coordinator roles

The columns are not hypothetical. `limit_choices_to` on both fields — `{'role': 'PM'}` at [models.py:63](projects/models.py#L63) and `{'role': 'Project Coordinator'}` at [models.py:85](projects/models.py#L85) — is a **ModelForm/admin choice-narrowing constraint only**. It is not a DB constraint, is not enforced by `Model.save()`, and does not apply to direct assignment. Confirmed reachable by:

- **The Zoho webhook**, [views.py:5366-5371](projects/views.py#L5366-L5371) — `UserProfile.objects.filter(user__email__iexact=pm_email).first()` with **no role filter**, result assigned straight to `assigned_pm` at [views.py:5386](projects/views.py#L5386). Any role whose email is in the deal's `Assign_PM` field lands in column 1.
- **`manage.py shell` / bulk operations** — no runtime guard anywhere.

The codebase already acknowledges this: [views.py:5965-5966](projects/views.py#L5965-L5966) names it *"the pathological webhook case — where `assigned_pm` could point to a non-manager profile"*.

Counterpaths that **do** filter: `admin_assign_pm` [views.py:7895](projects/views.py#L7895), `subadmin_projects` [views.py:8457](projects/views.py#L8457), `assign_coordinators` candidate list [views.py:3593](projects/views.py#L3593) — all filter `role='PM'` / `role='Project Coordinator'` explicitly.

### Additional rows not in the prompt's list of 9

| Case | Return | Evidence |
|---|---|---|
| Any user with **no `UserProfile`** (e.g. `createsuperuser`) | **False** on all three columns | [:34-36](projects/permissions.py#L34-L36) — `getattr(user, 'profile', None) is None → False` |
| `role=''` (permitted, `blank=True` at [models.py:522](projects/models.py#L522)) | Same as every other row — relations only | Function never reads `role` |
| `project.assigned_pm is None` | Falls through to the coordinator check | [:37](projects/permissions.py#L37) — `None == profile` is False |
| `profile.is_active=False` | **True** if the relation exists — **deactivation does not revoke authority here** | Function never reads `is_active`. Contrast `project_managers` at [permissions.py:80](projects/permissions.py#L80), which *does* filter `is_active=True`. |

The `is_active` asymmetry is a genuine divergence between the two helpers in the same module. Logged as FINDINGS_SECONDARY #14.

**No cell is UNKNOWN.** Every one is determined by reading [permissions.py:34-39](projects/permissions.py#L34-L39).

## 2.3 Inputs the function reads

Complete list — three, all relational:

| Input | Line | Note |
|---|---|---|
| `user.profile` (existence + identity) | [:34](projects/permissions.py#L34) | via `getattr`, defaulting to `None` |
| `project.assigned_pm` | [:37](projects/permissions.py#L37) | FK identity comparison |
| `project.coordinators` | [:39](projects/permissions.py#L39) | M2M existence query |

**It does NOT read:** `is_superuser`, `is_staff`, `is_design_head`, `UserProfile.role`, `UserProfile.is_active`, `Project.status`, `Project.is_deleted`, `Project.project_type`, `Project.program`, or any `SystemSettings` flag. Verified by reading all six executable lines — there are no other statements in the function body.

**Consequence:** a Django superuser created via `createsuperuser` (no `UserProfile`) gets `False` from this function on every project. Their access comes entirely from `role_required`'s separate fallback, which treats a profile-less user as `'Admin'` ([decorators.py:67-70](projects/decorators.py#L67-L70)) — a different code path with the opposite default.

## 2.4 Canonical-check claims (hard-stop condition 4)

| Function | File:Line | Claim |
|---|---|---|
| `user_can_manage_project` | [permissions.py:12](projects/permissions.py#L12) | **Claims canonical** — module docstring [:4](projects/permissions.py#L4) *"the SINGLE canonical place"*; body docstring [:19](projects/permissions.py#L19) *"the one canonical comparison path"* |
| `_pm_owns_project` | [views.py:1665](projects/views.py#L1665) | **Disclaims** — [:1668-1670](projects/views.py#L1668-L1670) *"Thin adapter over the canonical `user_can_manage_project()`… No ownership comparison lives here anymore."* Body is a one-line pass-through ([:1671](projects/views.py#L1671)). |
| `_is_project_pm` | [views.py:5959](projects/views.py#L5959) | **Disclaims** — [:5963-5964](projects/views.py#L5963-L5964) *"The ownership comparison routes through the canonical `user_can_manage_project()`."* |
| `user_can_manage_program` | [permissions.py:42](projects/permissions.py#L42) | Program authority, explicitly derived *"through the one canonical `user_can_manage_project()` path"* ([:47](projects/permissions.py#L47)) |

Exactly one claims canonicity. **Condition 4 not triggered.**

---

# SECTION 3 — Per-site classification

**Write** = can create, modify, delete, or transition any record, send a notification, or trigger any side effect. **Read** = everything else.

"Reach the permission call" = the role comparison evaluates such that `user_can_manage_project` is actually invoked. For Variant A that is **PM only**. The nine roles are abbreviated: PM, PC (Project Coordinator), SE (Site Engineer), SCM, Fin (Finance), Des (Design), BD, CEO, Adm (Admin). System Admin is a tenth role in `ROLE_CHOICES` ([models.py:510](projects/models.py#L510)) and behaves identically to the other non-PM roles; it is folded into "all others" below.

## 3.1 WRITE-PATH SITES — the consequential subset

| # | File:Line | View / URL name | R/W | What is guarded | Reaches the call | **Short-circuits past it** |
|---|---|---|---|---|---|---|
| **A3** | [views.py:5585](projects/views.py#L5585) | `upload_project_document` | **WRITE** | Upload N files to Supabase; create `ProjectDocument` rows ([:5640-5655](projects/views.py#L5640-L5655)) | PM | **PC, SE, SCM, Fin, Des, BD, CEO, Adm** |
| **A4** | [views.py:5679](projects/views.py#L5679) | `delete_project_document` | **WRITE** | Soft-delete a document ([:5687-5690](projects/views.py#L5687-L5690)); `log_activity` ([:5693](projects/views.py#L5693)) | PM | **all others** — *but* backstopped at [:5684](projects/views.py#L5684): `if doc.uploaded_by != profile and profile.role != 'Admin': return HttpResponseForbidden()`. **Effective write is limited to the uploader or an Admin.** |
| **A5** | [views.py:5719](projects/views.py#L5719) | `upload_task_attachment` | **WRITE** | Upload files; create `TaskAttachment` rows | PM | **all others** |
| **A6** | [views.py:5812](projects/views.py#L5812) | `delete_task_attachment` | **WRITE** | Soft-delete an attachment ([:5821-5824](projects/views.py#L5821-L5824)) | PM | **all others** — backstopped identically at [:5818](projects/views.py#L5818) (uploader or Admin) |
| **A7** | [views.py:5983](projects/views.py#L5983) | `create_project_issue` | **WRITE** | Create `Issue`; `log_activity`; **notifies every `project_managers(project)`** ([:6051](projects/views.py#L6051)) + assignee ([:6057](projects/views.py#L6057)) via in-app/WhatsApp/email | PM | **all others — no backstop** |
| **A8** | [views.py:6077](projects/views.py#L6077) | `create_task_issue` | **WRITE** | Create task-linked `Issue`; notifies `project_managers` ([:6147](projects/views.py#L6147)) + assignee ([:6153](projects/views.py#L6153)) | PM | **all others — no backstop** |
| **A9** | [views.py:6175](projects/views.py#L6175) | `create_delivery_issue` | **WRITE** | Create DC-linked `Issue`; notifies `project_managers` ([:6253](projects/views.py#L6253)) + assignee ([:6259](projects/views.py#L6259)) | PM | **all others — no backstop** (DC cross-project guard at [:6180](projects/views.py#L6180) checks the DC belongs to the project, not the user) |
| **A11** | [views.py:6316](projects/views.py#L6316) | `update_issue_status` | **WRITE** | Transition Issue Open → In Progress ([:6332](projects/views.py#L6332)); `log_activity` | PM | **all others — no backstop** beyond status preconditions ([:6319-6328](projects/views.py#L6319-L6328)) |
| **A12** | [views.py:6356](projects/views.py#L6356) | `resolve_issue` | **WRITE** | Transition In Progress → Resolved, set `resolved_at` + `resolution_note` ([:6371-6375](projects/views.py#L6371-L6375)); notifies `project_managers` + assignee + raiser ([:6393](projects/views.py#L6393)) | PM | **all others — no backstop** |
| **A13** | [views.py:6492](projects/views.py#L6492) | `assign_issue` | **WRITE** | Set/clear `Issue.assigned_to` to **any `UserProfile` by pk** ([:6501-6505](projects/views.py#L6501-L6505)); `log_activity` | PM | **all others — no backstop.** The assignee lookup is `UserProfile.objects.get(pk=assignee_id)` with no role or project scoping. |
| **A14** | [views.py:6534](projects/views.py#L6534) | `create_task_comment` | **WRITE** | Create `Comment` ([:6562-6569](projects/views.py#L6562-L6569)); `log_activity` | PM | **all others — no backstop** |
| **A15** | [views.py:6595](projects/views.py#L6595) | `create_issue_comment` | **WRITE** | Create `Comment` on an issue; `log_activity` | PM | **all others — no backstop** |
| **C1** | [views.py:4931](projects/views.py#L4931) | `project_overview` (POST) | **WRITE** | Milestone updates, design assignment, and the other POST actions | PM/PC via `is_assigned_pm`; **Fin** additionally for `action='update_milestone'` | n/a — this is an explicit grant, not a skipped check |

**12 write-path Variant-A sites. Nine have no secondary guard at all** (A3, A5, A7, A8, A9, A11, A12, A13, A14, A15 — of which A3 and A5 are creation-only and the rest are issue/comment lifecycle). Two (A4, A6) are effectively contained by an uploader-or-Admin check.

## 3.2 READ-PATH SITES

| # | File:Line | View / URL name | R/W | What is guarded | Reaches the call | **Short-circuits past it** |
|---|---|---|---|---|---|---|
| A1 | [views.py:4924](projects/views.py#L4924) | `project_overview` (GET) | Read | The entire project page: info, financials, milestones, tasks, documents, Gantt | PM | **PC, SE, SCM, Fin, Des, BD, CEO, Adm** |
| A2 | [views.py:5528](projects/views.py#L5528) | `task_detail` | Read | Task page: attachments, issues, threaded comments, checklist | PM | **all others** |
| A10 | [views.py:6277](projects/views.py#L6277) | `issue_detail` | Read | Issue page + comments; sets `is_pm` for control visibility ([:6281](projects/views.py#L6281)) | PM | **all others** |
| A16 | [views.py:6681](projects/views.py#L6681) | `project_timeline` | Read | Full paginated `ActivityLog` for the project | PM | **all others** |
| A17 | [views.py:6968](projects/views.py#L6968) | `delivery_challan_detail` | Read | DC line items, GRN state, DC issues | PM | **PC, SE, SCM, Adm only** — Des, Fin, BD, CEO are blocked earlier by the 403 allowlist at [:6964](projects/views.py#L6964) |
| D1 | [views.py:5206](projects/views.py#L5206) | `project_overview` (context) | Read | Cascade-scheduling UI option | PM (conjunction) | none — **over-restricts PC** |

## 3.3 SITES THAT DO NOT LEAK — for contrast

| # | File:Line | View | R/W | Why it holds |
|---|---|---|---|---|
| B1 | [views.py:5968](projects/views.py#L5968) | `_is_project_pm` → `close_issue` [:6425](projects/views.py#L6425), `reopen_issue` [:6458](projects/views.py#L6458) | **WRITE** | Conjunction: role ∈ {PM, PC} **AND** `user_can_manage_project`. Both required. 403 otherwise. |
| E1 | [views.py:2915](projects/views.py#L2915) | `task_status_update` | **WRITE** | Compares to `task.assigned_role`, a per-task value — not a fixed role literal |
| F1 | [views.py:1687](projects/views.py#L1687) | `checklist_item_complete` | **WRITE** | Same shape as E1 |

`close_issue` / `reopen_issue` are the direct counter-example: **the same issue-lifecycle surface, guarded correctly**, three lines away from A11/A12/A13 which are not.

---

# SECTION 4 — Current effective access matrix

**The question answered here: for each role, what can it do TODAY on a project it has no relationship to?**

Because `user_can_manage_project` returns `False` uniformly for the no-relationship case (§2.2), and because Variant A only invokes it when `role == 'PM'`, the matrix collapses to two outcomes.

## 4.1 The matrix

| Role | READ on an unrelated project | WRITE on an unrelated project |
|---|---|---|
| **PM** | **Blocked** — 404 at A1, A2, A10, A16, A17 | **Blocked** — 404 at all 12 write sites |
| **Project Coordinator** | **Full** — A1, A2, A10, A16, A17 | **Full** — all 12 write sites |
| **Site Engineer** | **Full** — A1, A2, A10, A16, A17 | **Full** — all 12 write sites |
| **SCM** | **Full** — A1, A2, A10, A16, A17 | **Full** — all 12 write sites |
| **Finance** | **Full** — A1, A2, A10, A16; **blocked at A17** ([:6964](projects/views.py#L6964)) | **Full** — all 12, plus the C1 milestone grant |
| **Design** | **Full** — A1, A2, A10, A16; **blocked at A17** | **Full** — all 12 write sites |
| **Sales & BD** | **Full** — A1, A2, A10, A16; **blocked at A17** | **Full** — all 12 write sites |
| **CEO** | **Full** — A1, A2, A10, A16; **blocked at A17** | **Full** — all 12 write sites |
| **Admin** | **Full** — A1, A2, A10, A16, A17 | **Full** — all 12, and the only role that passes the A4/A6 uploader backstop for others' files |
| *(System Admin)* | **Full** — A1, A2, A10, A16; blocked at A17 | **Full** — all 12 write sites |

**Read the PM row against every other row.** The role the check names is the only role it constrains. Eight of nine roles have unrestricted read and write on every project in the system through these endpoints.

Two things this matrix does **not** say:
- It does not cover endpoints outside the 22 sites. Views gated by `@role_required` with no project-authority call (`task_assign`, `project_activate`, `boq_*`, `milestone_*`) are governed by their own decorators — see §6.1.
- It does not assert intent. Whether "Finance can read any project" is correct policy is exactly the decision this audit exists to inform.

## 4.2 (prompt 4.1) Do Finance, SCM, CEO and BD reach project pages *only* via the short-circuit?

**No — and this is the single most important correction to the framing.** Their dashboards already grant them the whole portfolio, independently of these 22 sites. Evidence, all from the previous audit's Section 4.3 and re-verified:

| Role | Dashboard queryset | Scope |
|---|---|---|
| Finance | [views.py:825-838](projects/views.py#L825-L838) | `Project.objects.filter(is_deleted=False, status__in=['Active','In Progress'])` — no user term. Rationale in-code at [:821-822](projects/views.py#L821-L822): *"Finance is department-level — no assigned_finance field on Project. Show all active/in-progress projects across the portfolio."* |
| SCM | [views.py:966-970](projects/views.py#L966-L970) | Same filter. Rationale at [:1192](projects/views.py#L1192): *"SCM scope: all active projects (SCM is not PM-scoped; it sees all active projects)."* |
| CEO | [views.py:1310-1318](projects/views.py#L1310-L1318) | Same filter, portfolio-wide by definition |
| BD | [views.py:4676-4694](projects/views.py#L4676-L4694) | Same filter. Rationale at [:4667-4668](projects/views.py#L4667-L4668): *"BD is department-level — no assigned_bd field on Project confirmed. BD sees all active/in-progress projects."* |

Each dashboard renders a card per project linking to `project_overview` — [finance.html:256](projects/templates/dashboard/finance.html#L256), [scm.html:218](projects/templates/dashboard/scm.html#L218), [ceo.html:622](projects/templates/dashboard/ceo.html#L622), [bd.html:243](projects/templates/dashboard/bd.html#L243).

**So for these four roles the short-circuit is not what grants portfolio access — the dashboard queryset is.** Removing the role prefix at A1 would break their *documented* workflow: they would see project cards on their own dashboard and get a 404 clicking through. That is a coherent, in-code-documented design, not an accident.

**Site Engineer and Design are different.** Their dashboards *are* scoped:
- SE: [views.py:546-550](projects/views.py#L546-L550) — `phases__tasks__assigned_to=se_profile`
- Design: [views.py:705-715](projects/views.py#L705-L715) — `Q(assigned_design=…) | Q(phases__tasks__assigned_to=…)`

For SE and Design the short-circuit grants access **their own dashboards deliberately withhold**. Their portfolio-wide reach exists only by URL, through these 22 sites.

**Project Coordinator is the sharpest case.** `dashboard_pm` scopes them to coordinated projects ([views.py:283-285](projects/views.py#L283-L285)), `user_can_manage_project` was *specifically widened* to include them ([permissions.py:16-27](projects/permissions.py#L16-L27)), and `_is_project_pm` names them explicitly ([views.py:5968](projects/views.py#L5968)) — yet at all 17 Variant-A sites the `role == 'PM'` literal excludes them from the very check built for them.

**Summary of 4.1:** Finance, SCM, CEO and BD do **not** depend on the short-circuit — their dashboards grant equivalent read scope directly. SE, Design and Project Coordinator **do** — for them the short-circuit is the sole source of cross-project reach. Admin depends on it for A1/A2/A10/A16/A17 (no Admin bypass exists inside `user_can_manage_project`, per §2.3).

## 4.3 (prompt 4.2) Is `project_overview` affected?

**Yes — site A1, [views.py:4924](projects/views.py#L4924).** It is the most-reached page in the application (per the previous audit, effectively all project traffic converges there, including via the `project_detail` 302 shim at [views.py:1746](projects/views.py#L1746)).

Decorators: `@login_required` only ([views.py:4907](projects/views.py#L4907)). No `@role_required`.

**Roles that can currently open ANY project by URL through it: Project Coordinator, Site Engineer, SCM, Finance, Design, Sales & BD, CEO, Admin, System Admin — every role except PM.**

What renders for them ([views.py:5240-5279](projects/views.py#L5240-L5279) context, `projects/project_overview.html`): project info card including `contract_value` and `capacity_kw`, customer name/phone/email, full phase and task list, payment milestones, uploaded documents, issues, delivery challans, and — for PM/PC/CEO ([views.py:5226](projects/views.py#L5226)) — the buffered client Gantt.

`payment_requests` is separately role-gated at [views.py:5211](projects/views.py#L5211) to `('Finance','PM','SCM','Admin')`, so it is not exposed to SE/Design/BD/CEO. That is the only field-level narrowing on the page.

## 4.4 (prompt 4.3) Writes permitted by the short-circuit to unrelated roles — highest severity, listed first

**Ten sites. No secondary guard on any of them.** All ten are reachable by all eight non-PM roles.

| Rank | Site | File:Line | Effect on a project the actor has no relationship to |
|---|---|---|---|
| 1 | **A13** `assign_issue` | [views.py:6492](projects/views.py#L6492) | Reassign any issue to **any `UserProfile` in the system** — `UserProfile.objects.get(pk=assignee_id)` at [:6501](projects/views.py#L6501), unscoped by role or project |
| 2 | **A12** `resolve_issue` | [views.py:6356](projects/views.py#L6356) | Mark an issue Resolved, write `resolution_note`, set `resolved_at` — **and notify the project's PM, coordinators, assignee and raiser** ([:6393](projects/views.py#L6393)) over in-app + WhatsApp + email |
| 3 | **A11** `update_issue_status` | [views.py:6316](projects/views.py#L6316) | Transition an issue Open → In Progress ([:6332](projects/views.py#L6332)) |
| 4 | **A7** `create_project_issue` | [views.py:5983](projects/views.py#L5983) | Create a project issue; **notifies every project manager** ([:6051](projects/views.py#L6051)) + the chosen assignee ([:6057](projects/views.py#L6057)) |
| 5 | **A8** `create_task_issue` | [views.py:6077](projects/views.py#L6077) | Same, task-linked ([:6147](projects/views.py#L6147), [:6153](projects/views.py#L6153)) |
| 6 | **A9** `create_delivery_issue` | [views.py:6175](projects/views.py#L6175) | Same, DC-linked ([:6253](projects/views.py#L6253), [:6259](projects/views.py#L6259)) |
| 7 | **A3** `upload_project_document` | [views.py:5585](projects/views.py#L5585) | Upload arbitrary files into the project's Supabase folder + create `ProjectDocument` rows |
| 8 | **A5** `upload_task_attachment` | [views.py:5719](projects/views.py#L5719) | Upload arbitrary files against any task |
| 9 | **A14** `create_task_comment` | [views.py:6534](projects/views.py#L6534) | Post comments/replies on any task |
| 10 | **A15** `create_issue_comment` | [views.py:6595](projects/views.py#L6595) | Post comments/replies on any issue |

**Contained write sites** (short-circuit passed, but a second guard stops the write):

| Site | File:Line | Containment |
|---|---|---|
| A4 `delete_project_document` | [views.py:5679](projects/views.py#L5679) | [:5684](projects/views.py#L5684) — `doc.uploaded_by != profile and profile.role != 'Admin'` → 403 |
| A6 `delete_task_attachment` | [views.py:5812](projects/views.py#L5812) | [:5818](projects/views.py#L5818) — identical check |

**Highest-severity characteristic, stated plainly:** six of the ten (ranks 2, 4, 5, 6 and the notification arms of 1 and 3) do not merely mutate state — they **emit outbound WhatsApp and email to the project's actual managers**, through the `send_notification` chokepoint ([notifications.py:24](projects/notifications.py#L24)). A write by an unrelated actor is therefore not silent; it arrives in the real PM's inbox attributed to the project.

---

# SECTION 5 — Test coverage

## 5.1 Do any tests exercise these sites?

**Two of the 22.** Test files: [projects/tests.py](projects/tests.py) (206 lines) and [projects/tests_gantt.py](projects/tests_gantt.py) (322 lines). Confirmed to be the only two — `Glob **/test*.py` returned nothing else in the project outside `venv/`, plus two non-`TestCase` WhatsApp scripts ([test_whatsapp_templates.py](test_whatsapp_templates.py), [management/commands/test_whatsapp.py](projects/management/commands/test_whatsapp.py)).

| Site | Covered? | Test |
|---|---|---|
| **A1** `project_overview` | **Yes — indirectly, via the Gantt tests** | `GanttRoleVisibilityTests` — [tests_gantt.py:234-283](projects/tests_gantt.py#L234-L283) |
| **D1** `show_cascade_option` | No | — |
| All other 20 sites | **No** | — |

`projects/tests.py` covers only `project_field_edit` ([views.py:1832](projects/views.py#L1832)) — a site with **no role prefix**, correctly gated. Its negative test `test_non_manager_forbidden` ([tests.py:188-198](projects/tests.py#L188-L198)) uses `pm2`, whose role is **`'PM'`** ([tests.py:39](projects/tests.py#L39)). It therefore exercises the *correct* path and asserts 404 — it does not touch the short-circuit, because a PM never short-circuits.

**No test anywhere logs in as a non-PM role and asserts a project-scoped denial.**

## 5.2 Do any tests assert on `user_can_manage_project` directly?

**No.** Grepping both test files for `user_can_manage_project`, `_pm_owns_project`, `_is_project_pm` and `permissions` returns one hit: a prose mention in the `tests.py` module docstring at [tests.py:7](projects/tests.py#L7) (*"…and are gated by user_can_manage_project()"*). No import, no call, no assertion. The function has **zero direct unit tests**.

## 5.3 Would a change to this pattern be caught by the existing suite?

**Yes — but not as a safety net. As failures that encode the current behaviour as correct.** This is the most consequential finding in Section 5, and it inverts the expected answer.

`GanttRoleVisibilityTests` sets up one project owned by `pm_view` ([tests_gantt.py:237-238](projects/tests_gantt.py#L237-L238)) with **no coordinators added**, then:

**[tests_gantt.py:260-267](projects/tests_gantt.py#L260-L267)** — `test_other_roles_get_no_client_rows`:
```python
for uname, role in (('se_v', 'Site Engineer'), ('fin_v', 'Finance'),
                    ('scm_v', 'SCM'), ('des_v', 'Design'), ('bd_v', 'BD')):
    u, _ = _make_user(uname, role)
    ctx = self._ctx(u)
    self.assertFalse(ctx['gantt_can_view_client'], role)
    self.assertIsNone(ctx['gantt_client'], role)
    self.assertIsNotNone(ctx['gantt_internal'], role)   # <- asserts a successful render
```
Five freshly-created users, none with any relationship to the project, each **asserted to receive a rendered `project_overview` with populated Gantt context**. This test passes today *because of* the A1 short-circuit.

**[tests_gantt.py:253-258](projects/tests_gantt.py#L253-L258)** — `test_coordinator_and_ceo_see_client_view`: creates `coord_v` (Project Coordinator) and `ceo_v` (CEO), neither related to the project, and asserts both get the **client Gantt**. The Project Coordinator case is precisely the leak described in §4.2, asserted as expected behaviour.

**[tests_gantt.py:269-283](projects/tests_gantt.py#L269-L283)** — `test_non_residential_hides_gantt` and `test_not_activated_flag`: both use unrelated CEO users against projects owned by `self.pm`.

If the role prefix at A1 were removed, all four tests would fail — the view would `raise Http404`, and `resp.context` would no longer carry `gantt_available` / `gantt_client` / `gantt_internal`, raising `KeyError` or `TypeError` on the assertions.

**Stated plainly, as requested:**

- **Would a change be *detected*? Yes** — four tests in `tests_gantt.py` break immediately, all against site A1.
- **Would the change be *validated*? No.** Those tests were written to check Gantt visibility, not authorization; the unrelated-user setup is incidental to their purpose. They assert the current behaviour is correct without ever having decided that it is.
- **Are the other 21 sites covered? No — zero coverage.** All ten unguarded write paths in §4.4 (`assign_issue`, `resolve_issue`, `update_issue_status`, the three issue-creation views, both upload views, both comment views) could be changed in either direction — tightened or loosened — with the full suite still green.
- **Is there a regression test that would catch a *new* site adopting this pattern? No.**

---

# SECTION 6 — Blast radius

## 6.1 Every place that reads `UserProfile.role`

Grouped by relationship to a project-authority decision. Assignments of `role` (user administration) and `assigned_role` (a `Task` field, a different vocabulary) are excluded — `Task.assigned_role` uses `'BD / Sales'` where `UserProfile.role` uses `'BD'`, a mismatch normalised at [views.py:1690](projects/views.py#L1690), [:2912](projects/views.py#L2912), [:3510](projects/views.py#L3510), [:5184](projects/views.py#L5184).

### (a) Adjacent to a project-authority decision — the 22 Section 1 sites

[views.py:1691](projects/views.py#L1691), [2915](projects/views.py#L2915), [4924](projects/views.py#L4924), [4931](projects/views.py#L4931), [5206](projects/views.py#L5206), [5528](projects/views.py#L5528), [5585](projects/views.py#L5585), [5679](projects/views.py#L5679), [5719](projects/views.py#L5719), [5812](projects/views.py#L5812), [5968](projects/views.py#L5968), [5983](projects/views.py#L5983), [6077](projects/views.py#L6077), [6175](projects/views.py#L6175), [6277](projects/views.py#L6277), [6316](projects/views.py#L6316), [6356](projects/views.py#L6356), [6492](projects/views.py#L6492), [6534](projects/views.py#L6534), [6595](projects/views.py#L6595), [6681](projects/views.py#L6681), [6968](projects/views.py#L6968).

### (b) Project-scoped views gated by role ALONE — no authority call at all

**This is the larger population, and it is arguably more exposed than category (a):** these views take a `project_id`, act on that project, and never call any project-authority function. A PM reaches **any** project through them.

| File:Line | View | Gate | R/W |
|---|---|---|---|
| [views.py:3968](projects/views.py#L3968) | `boq_detail` | `role not in ('Design','SCM','PM','Project Coordinator','Admin')` → 403 | **WRITE** — handles `save_design`, `submit_design`, `add_item`, `delete_item`, `save_scm`, `acknowledge_scm` |
| [views.py:4151](projects/views.py#L4151) | `boq_submit` | `role != 'Design'` → 403 | **WRITE** |
| [views.py:4204](projects/views.py#L4204) | `boq_acknowledge` | `role != 'SCM'` → 403 | **WRITE** |
| [views.py:4233](projects/views.py#L4233) | `boq_request_revision` | `role not in ('PM','Project Coordinator')` → 403 | **WRITE** |
| [views.py:4285](projects/views.py#L4285) | `boq_history` | `role not in ('PM','Project Coordinator','Design','SCM','Admin')` → 403 | Read |
| [views.py:4489](projects/views.py#L4489) | `raise_payment_request` | `role != 'SCM'` → 403 | **WRITE** |
| [views.py:4585](projects/views.py#L4585) | `confirm_payment_request` | `role != 'Finance'` → 403 | **WRITE** |
| [views.py:7279](projects/views.py#L7279) | `design_submission_detail` | `profile != submitted_by and role not in ('PM','Admin')` | Read |
| [views.py:7291](projects/views.py#L7291) | `payment_request_detail` | `role not in ('SCM','Finance','PM','Admin')` → 403 | Read |
| [urls.py:88-89](projects/urls.py#L88-L89) | `milestone_invoice`, `milestone_receive` | `@role_required(['Finance'])` [views.py:4355](projects/views.py#L4355), [:4381](projects/views.py#L4381) | **WRITE** |
| [urls.py:52-53](projects/urls.py#L52-L53) | `project_activate`, `project_recalculate_dates` | `@role_required` + `_pm_owns_project` — **in category (c)** | **WRITE** |

Every row above except the last is a project-scoped endpoint with **no per-project authority check whatsoever**. Changing the Section 1 pattern would not touch them; they represent an independent, parallel gap.

### (c) Project-scoped views correctly gated (role decorator AND authority call, no prefix)

[views.py:1772](projects/views.py#L1772) `project_edit` · [1832](projects/views.py#L1832) `project_field_edit` · [1914](projects/views.py#L1914) `project_activate` · [1975](projects/views.py#L1975) `project_recalculate_dates` · [2003](projects/views.py#L2003) `task_add` · [2044](projects/views.py#L2044) `enable_cascade_scheduling` · [3331](projects/views.py#L3331) `task_assign` · [3507](projects/views.py#L3507) `task_assign_design_head` · [3588](projects/views.py#L3588) `assign_coordinators` · [4455](projects/views.py#L4455) `milestone_create` · [6425](projects/views.py#L6425) `close_issue` · [6458](projects/views.py#L6458) `reopen_issue`.

**Twelve sites use the correct pattern; seventeen use the leaking one.** The correct pattern is not rare or novel here — it is the second-most-common shape in the same file.

### (d) Not adjacent to project authority

Role→dashboard routing [decorators.py:35](projects/decorators.py#L35), [:66](projects/decorators.py#L66); `role_required` itself [decorators.py:71](projects/decorators.py#L71) · Non-project role gates: vendors [views.py:3662](projects/views.py#L3662), [3716](projects/views.py#L3716), [3760](projects/views.py#L3760), [3804](projects/views.py#L3804) · Notification recipient selection [views.py:3066](projects/views.py#L3066), [3260](projects/views.py#L3260), [4623](projects/views.py#L4623), [4625](projects/views.py#L4625), [5415](projects/views.py#L5415) · Assignee candidate filtering [views.py:518](projects/views.py#L518), [1927](projects/views.py#L1927), [3341](projects/views.py#L3341), [3347](projects/views.py#L3347), [3423](projects/views.py#L3423), [3429](projects/views.py#L3429), [3593](projects/views.py#L3593), [4993](projects/views.py#L4993), [5191](projects/views.py#L5191), [5196](projects/views.py#L5196) · User administration [views.py:1582](projects/views.py#L1582), [1626](projects/views.py#L1626), [7427-7429](projects/views.py#L7427-L7429), [7561-7563](projects/views.py#L7561-L7563), [7626](projects/views.py#L7626), [8517](projects/views.py#L8517), [8528-8530](projects/views.py#L8528-L8530), [8551](projects/views.py#L8551), [8562-8570](projects/views.py#L8562-L8570), [8594](projects/views.py#L8594), [8617](projects/views.py#L8617), [8703](projects/views.py#L8703) · Author/uploader ownership (not project authority) [views.py:5684](projects/views.py#L5684), [5818](projects/views.py#L5818), [6644](projects/views.py#L6644) · Display/grouping [views.py:1639](projects/views.py#L1639), [6990](projects/views.py#L6990), [7220](projects/views.py#L7220), [7583](projects/views.py#L7583), [7657](projects/views.py#L7657), [7724](projects/views.py#L7724), [7765](projects/views.py#L7765), [8644](projects/views.py#L8644) · Program access [views.py:2105](projects/views.py#L2105), [2127](projects/views.py#L2127), [2165](projects/views.py#L2165) · OPEX site creation [views.py:2303](projects/views.py#L2303) · Templates: [_task_row.html:118](projects/templates/projects/partials/_task_row.html#L118) (`is_assigned_pm`), [base.html:51](projects/templates/base.html#L51) (Programs nav).

## 6.2 Dashboard querysets depending on the same assumption

Cross-referenced against the dashboard inventory in `CONTEXT_SWITCHER_AUDIT.md` §2.

| Dashboard | Queryset | Encodes the same assumption? |
|---|---|---|
| `dashboard_pm` | [views.py:283-285](projects/views.py#L283-L285) — `Q(assigned_pm=…) \| Q(coordinators=…)` | **Yes, and it disagrees with the Section 1 sites.** It is the queryset-level equivalent of `user_can_manage_project`, correctly including coordinators. A PC's dashboard shows only their projects; the Section 1 sites let them open any. |
| `tasks_drill_down` | [views.py:215-218](projects/views.py#L215-L218) | **Yes — a third copy** of the PM-or-coordinator rule. Unlisted roles (Admin, System Admin, CEO, Finance, BD) fall through to all active projects at [:229](projects/views.py#L229), mirroring the short-circuit's effect. |
| `dashboard_site_engineer` | [views.py:546-550](projects/views.py#L546-L550) | **Contradicts it.** Scopes SE to task-assigned projects; the Section 1 sites grant SE everything by URL. |
| `dashboard_design` | [views.py:705-715](projects/views.py#L705-L715) | **Contradicts it**, same way. |
| `dashboard_finance` | [views.py:825-838](projects/views.py#L825-L838) | **Consistent** — portfolio-wide by documented design ([:821-822](projects/views.py#L821-L822)) |
| `dashboard_scm` | [views.py:966-970](projects/views.py#L966-L970) | **Consistent** ([:1192](projects/views.py#L1192)) |
| `dashboard_ceo` | [views.py:1310-1318](projects/views.py#L1310-L1318) | **Consistent** — portfolio-wide |
| `dashboard_bd` | [views.py:4676-4694](projects/views.py#L4676-L4694) | **Consistent** ([:4667-4668](projects/views.py#L4667-L4668)) |
| `admin_project_list` | [views.py:7858-7867](projects/views.py#L7858-L7867) | **Consistent** — all projects, Admin-gated |
| `subadmin_projects` | [views.py:8440](projects/views.py#L8440) | Lists all projects but **renders zero links** (no `href`/`window.location` in [subadmin/projects.html](projects/templates/projects/subadmin/projects.html)) |

**Summary:** four dashboards (Finance, SCM, CEO, BD) are *consistent* with the short-circuit — same scope by two different mechanisms. Three (`dashboard_pm`, SE, Design) *contradict* it — their querysets are narrower than what the Section 1 sites permit by URL. That split is the empirical shape of the current system: there is no single coherent assumption encoded, and any policy decision has to reconcile the two halves.

---

# What could not be determined by reading

| # | Question | What would resolve it |
|---|---|---|
| 1 | Whether any of the ten unguarded write paths in §4.4 has actually been exercised by an unrelated role in production | A read-only query over `ActivityLog` on the Railway database, joining `actor` → `UserProfile` against each project's `assigned_pm`/`coordinators` to find rows where the actor had no relationship. The data exists — `action_code` and `actor` are populated (migration `0043`) — but production was not queried in this session. |
| 2 | Whether `limit_choices_to` has ever been bypassed in live data — i.e. whether any `Project.assigned_pm` currently points to a non-PM profile, or any coordinator to a non-Coordinator profile | Read-only: `Project.objects.exclude(assigned_pm__role='PM').exclude(assigned_pm__isnull=True).count()` and the coordinator equivalent, against Railway. §2.2 establishes it is *possible* via [views.py:5369](projects/views.py#L5369); whether it has *occurred* is a data question. |
| 3 | Whether the Section 1 pattern was a deliberate policy that later drifted, or wrong from the start | Git history. **This repository is not a git repository** (confirmed: no VCS in the environment), so authorship and sequence cannot be recovered from the working tree. Only the in-code comments at [views.py:5960-5967](projects/views.py#L5960-L5967) and [permissions.py:1-9](projects/permissions.py#L1-L9) speak to intent, and neither addresses the Variant A sites. |

---

*New unrelated bugs found during this investigation, plus one correction to a previously-reported finding, are recorded in `FINDINGS_SECONDARY.md`. Nothing was fixed.*