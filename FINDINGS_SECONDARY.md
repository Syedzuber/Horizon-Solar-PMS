# FINDINGS_SECONDARY — unrelated bugs noticed during the Context Switcher audit

**Date:** 2026-07-25
**Status: LOGGED ONLY. Nothing here was fixed, and no file was modified to produce this list.**

These are defects observed while tracing project-type discrimination, dashboard routing and permissions. None is in scope for the context switcher. Severity is my assessment; triage is the maintainer's call.

---

## 1. `dashboard_ceo` has no role gate — any authenticated user can open the CEO portfolio dashboard

**Severity: High (data exposure)**
**Location:** [views.py:1539-1544](projects/views.py#L1539-L1544)

```python
@login_required
def dashboard_ceo(request):
    """CEO portfolio overview. Access: CEO role only. Renders in 3 DB queries via _get_ceo_dashboard_context."""
```

The docstring claims *"Access: CEO role only"* but there is no `@role_required(['CEO'])`. Every other role-specific dashboard has one — PM [views.py:267](projects/views.py#L267), SE [views.py:525](projects/views.py#L525), Design [views.py:693](projects/views.py#L693), Finance [views.py:816](projects/views.py#L816), SCM [views.py:937](projects/views.py#L937), BD [views.py:4662](projects/views.py#L4662).

`ROLE_DASHBOARD` ([decorators.py:10-24](projects/decorators.py#L10-L24)) only routes CEOs there; it does not keep anyone else out. **Any logged-in user who types `/dashboard/ceo/` gets the full portfolio view** — every active project, portfolio contract value (`fin_client_contract_value`, [views.py:1474](projects/views.py#L1474)), outstanding vendor payments ([views.py:1469](projects/views.py#L1469)), client payment pending ([views.py:1483](projects/views.py#L1483)), the department performance rollup ([views.py:1452-1459](projects/views.py#L1452-L1459)) and the named top-5 assignee leaderboard ([views.py:1499-1518](projects/views.py#L1499-L1518)).

A Site Engineer, Design or BD user seeing company-wide financials is a role-inappropriate exposure of the same class the codebase already fixed once — compare the comment at [decorators.py:14-16](projects/decorators.py#L14-L16) about the Project Coordinator falling through to the Admin dashboard: *"a role-inappropriate-data-exposure risk, not a cosmetic bug."*

**Note on `dashboard_admin`** ([views.py:103-106](projects/views.py#L103-L106)): also `@login_required` only, but there the docstring says this is deliberate (*"any authenticated user (no role restriction here — Admin nav is in the template)"*) and the template renders three links, no data. Not flagged.

---

## 2. `_ROLE_DASHBOARD` in views.py is a stale duplicate with a key that can never match

**Severity: Low (broken back-link)**
**Location:** [views.py:177-185](projects/views.py#L177-L185), consumed at [views.py:253](projects/views.py#L253)

```python
_ROLE_DASHBOARD = {
    'PM': 'dashboard_pm', 'Design': 'dashboard_design', 'SCM': 'dashboard_scm',
    'Site Engineer': 'dashboard_site_engineer', 'Finance': 'dashboard_finance',
    'BD / Sales': 'dashboard_bd',          # <-- never matches
    'CEO': 'dashboard_ceo',
}
```

A second role→dashboard map, independent of the canonical `decorators.ROLE_DASHBOARD`. Three problems:

1. **`'BD / Sales'` is not a `UserProfile.role` value.** `ROLE_CHOICES` ([models.py:518](projects/models.py#L518)) has `'BD'`. `'BD / Sales'` is the `Task.BD` *assigned_role* constant — a different vocabulary, as the code itself notes at [views.py:1404](projects/views.py#L1404) (*"filter on assigned_role='BD / Sales' (Task.BD constant); 'BD' alone returns zero"*) and at [views.py:1690](projects/views.py#L1690) where a `{'BD': 'BD / Sales'}` normalisation map exists for exactly this mismatch. So a BD user on `tasks_drill_down` gets `back_url = reverse('dashboard_pm')` from the `.get(role, 'dashboard_pm')` fallback at [views.py:253](projects/views.py#L253) — a "Back" button pointing at a dashboard they cannot access, which `@role_required` [views.py:267](projects/views.py#L267) then bounces with an error.
2. **`'Project Coordinator'` is absent**, so coordinators hit the same fallback. It happens to land correctly (`dashboard_pm` *is* their dashboard) — right answer, wrong reason.
3. **`'Admin'` and `'System Admin'` are absent**, so both also fall back to `dashboard_pm`, which neither can access.

The two maps also use different value types — paths in `decorators.py`, URL names in `views.py` — so they cannot be trivially merged.

---

## 3. Three independent strategies for building absolute URLs in outbound notifications

**Severity: Medium (breaks on any domain change; already inconsistent)**

| Strategy | Sites |
|---|---|
| **Hardcoded** `https://horizon-solar-pms-production.up.railway.app` | [views.py:3081](projects/views.py#L3081), [3275](projects/views.py#L3275), [3360](projects/views.py#L3360), [3442](projects/views.py#L3442), [3885](projects/views.py#L3885), [4635](projects/views.py#L4635), [5442](projects/views.py#L5442), [5462](projects/views.py#L5462), [6036](projects/views.py#L6036), [6132](projects/views.py#L6132), [6238](projects/views.py#L6238), [6390](projects/views.py#L6390), [7926](projects/views.py#L7926) |
| **`request.build_absolute_uri()`** | [views.py:3352](projects/views.py#L3352), [3434](projects/views.py#L3434), [3873](projects/views.py#L3873), [5452](projects/views.py#L5452), [6382](projects/views.py#L6382), [7920](projects/views.py#L7920) |
| **`settings.APP_BASE_URL`** (defaulting to the same hardcoded host) | [send_eod_digest.py:92-93](projects/management/commands/send_eod_digest.py#L92-L93) |

Consequences: a custom domain or environment rename silently breaks 13 email bodies; the same notification can carry a `build_absolute_uri` host in its WhatsApp param and a hardcoded host in its email body; and `build_absolute_uri` is unavailable in management commands and webhook contexts without a request, which is presumably why the hardcoded literals accumulated. `settings.APP_BASE_URL` is the only strategy that is configurable, and it is used in exactly one file.

Related: `send_raw_email` at [views.py:5432](projects/views.py#L5432) hardcodes the recipient `smzk07@gmail.com` for the Zoho unassigned-project alert.

---

## 4. System Admin project list has dead type-badge branches using a vocabulary that does not exist

**Severity: Low (cosmetic; stale code)**
**Location:** [templates/projects/subadmin/projects.html:43-46](projects/templates/projects/subadmin/projects.html#L43-L46) and [:115-118](projects/templates/projects/subadmin/projects.html#L115-L118)

```django
{% if project.project_type == 'residential' %}bg-blue-50 text-blue-700
{% elif project.project_type == 'commercial_opex' %}bg-purple-50 text-purple-700
```

Neither `'residential'` (lowercase) nor `'commercial_opex'` is in `Project.PROJECT_TYPE_CHOICES` ([models.py:8-12](projects/models.py#L8-L12)), which is `Residential` / `OPEX` / `CAPEX`. Both branches are unreachable, so every row falls through to the default styling. Confirmed against live data: 0 rows carry either value.

This looks like a leftover from a pre-`0005_project_redesign` vocabulary. Note `TaskDurationTemplate` still legitimately uses lowercase `'residential'` ([models.py:1394-1396](projects/models.py#L1394-L1396)) — the two vocabularies genuinely differ, so this is not a simple find-and-replace.

`{{ project.get_project_type_display }}` on the following lines ([:46](projects/templates/projects/subadmin/projects.html#L46), [:118](projects/templates/projects/subadmin/projects.html#L118)) works correctly — only the CSS-class branches are dead.

---

## 5. `if profile.role == 'PM' and not user_can_manage_project(...)` — the role prefix disables the ownership check for every non-PM role, including Project Coordinator

**Severity: High (access control)**
**Locations (~20):** [views.py:4924](projects/views.py#L4924) (`project_overview`), [5528](projects/views.py#L5528), [5585](projects/views.py#L5585), [5679](projects/views.py#L5679), [5719](projects/views.py#L5719), [5812](projects/views.py#L5812), [5983](projects/views.py#L5983), [6077](projects/views.py#L6077), [6175](projects/views.py#L6175), [6277](projects/views.py#L6277), [6316](projects/views.py#L6316), [6356](projects/views.py#L6356), [6492](projects/views.py#L6492), [6534](projects/views.py#L6534), [6595](projects/views.py#L6595), [6681](projects/views.py#L6681), [6968](projects/views.py#L6968)

```python
if profile.role == 'PM' and not user_can_manage_project(request.user, project):
    raise Http404
```

`user_can_manage_project` was widened so PM **or** Project Coordinator confers authority ([permissions.py:12-39](projects/permissions.py#L12-L39)), and `Project.coordinators` was added with the stated invariant that coordinators are *additive* ([models.py:77-86](projects/models.py#L77-L86)). But at these call sites the `role == 'PM'` prefix is evaluated first: for a user whose role is `'Project Coordinator'` the condition is `False`, so **the ownership check never runs and the view proceeds**.

Concretely, at `project_overview` [views.py:4924](projects/views.py#L4924): a Project Coordinator assigned to project A can open project B, C, or any other project by URL. The role gate that would otherwise stop them is absent — `project_overview` carries only `@login_required` ([views.py:4907](projects/views.py#L4907)).

The same shape governs document upload/delete, issue creation, comment creation, delivery-challan views and GRN confirmation at the line numbers above.

Two observations that make this look like drift rather than intent:
- `dashboard_pm` **does** scope coordinators correctly ([views.py:283-285](projects/views.py#L283-L285)), so a coordinator's *dashboard* shows only their projects while *direct URLs* to other projects remain open — an inconsistency, not a coherent policy.
- The correct pattern already exists elsewhere in the same file: `project_edit` and friends call `_pm_owns_project(request, project)` with no role prefix ([views.py:1772](projects/views.py#L1772), [1914](projects/views.py#L1914), [1975](projects/views.py#L1975), [2003](projects/views.py#L2003), [2044](projects/views.py#L2044)), and `permissions.py`'s module docstring ([permissions.py:1-9](projects/permissions.py#L1-L9)) explicitly warns against comparing role strings directly.

Non-PM department roles (Finance, SCM, Design, BD, CEO) are *intended* to see all projects per §4.3 of the main audit, so for those the prefix is harmless. The defect is specifically that `Project Coordinator` — a PM-equivalent, project-scoped role — is swept into the same unchecked bucket.

---

## 6. `reverse('project_overview', args=[project.pk])` builds a URL that 404s, and it is what gets sent over WhatsApp

**Severity: Medium (broken link in a customer-facing notification)**
**Location:** [views.py:5451-5452](projects/views.py#L5451-L5452), consumed at [views.py:5474](projects/views.py#L5474)

```python
project_link    = reverse('project_overview', args=[project.pk])
project_url_abs = request.build_absolute_uri(project_link)
...
template_params=[
    project.customer_name,    # [0] header
    pm_display_name,          # [1] body[0] — user_name
    project_url_abs,          # [2] body[1] — project_url
],
```

`project_overview` is registered as `projects/<str:project_id>/overview/` ([urls.py:96](projects/urls.py#L96)), where `project_id` is the **`CharField`** `'HRP-RES-2026-001'` — not the integer PK. Passing `project.pk` produces `/projects/7/overview/`. The `<str:>` converter matches `7`, the view runs `get_object_or_404(Project, project_id='7')` ([views.py:4914-4919](projects/views.py#L4914-L4919)), no row matches, and the PM gets a 404.

This is in the Zoho `deal-closed` webhook path, in the "new project assigned" notification — the PM's first touchpoint with a new project. The WhatsApp message links to a dead page.

Three lines below, the *same* function builds the correct value: `_ap_link = f'/projects/{project.project_id}/'` ([views.py:5453](projects/views.py#L5453)), used for the in-app `link` and the email body ([views.py:5462](projects/views.py#L5462), [5468](projects/views.py#L5468)). So the in-app and email links work; only the WhatsApp `project_url` param is broken. Every other reverse/f-string in the file uses `project.project_id` — this is the lone `.pk`.

Fixing it requires no Interakt console change (the param position is unchanged), only the argument.

---

## 7. `next` POST parameter is validated by a netloc check, not by Django's host/scheme guard

> **CORRECTED 2026-07-26.** As first written, this entry claimed the six `next` redirect sites performed **no** validation and were open redirects. **That was wrong.** All six *do* validate. The corrected finding below is much narrower and is not an open redirect. Anyone who acted on the original text should disregard it.

**Severity: Low (hardening gap, not an open redirect)**
**Locations:** [views.py:2982-2984](projects/views.py#L2982-L2984), [3102-3106](projects/views.py#L3102-L3106), [5660-5662](projects/views.py#L5660-L5662), [5695-5697](projects/views.py#L5695-L5697), [5794-5796](projects/views.py#L5794-L5796), [5833-5835](projects/views.py#L5833-L5835)

Every one of the six sites checks that the parsed URL has an empty `netloc` before redirecting:

```python
next_url = request.POST.get('next', '')
if next_url and not _urlparse(next_url).netloc:
    return redirect(next_url)
return redirect('project_overview', project_id=project.project_id)
```

[views.py:3102-3106](projects/views.py#L3102-L3106) uses a local `from urllib.parse import urlparse` and spells the test `if urlparse(next_url).netloc == '':`, with the comment at [:3101](projects/views.py#L3101) — *"Honour the ?next= redirect if it's a local URL (netloc empty = same domain)"* — but is otherwise identical.

`https://evil.example/` and `//evil.example/` both parse with a non-empty `netloc` and are correctly rejected. **The absolute-URL open redirect I originally described does not exist.**

The residual gap is narrow: an empty-`netloc` check is not equivalent to Django's `url_has_allowed_host_and_scheme()`. Values that parse with an empty `netloc` but that some browsers normalise as protocol-relative — notably backslash forms such as `/\evil.example` and `/\/evil.example` — are not rejected. Django's helper handles the backslash case explicitly for exactly this reason. Whether any browser in use actually follows those forms is **UNKNOWN** and would need a live test against the target browsers to establish.

Practical exposure remains small: every `next` in the templates is emitted by a `{% url %}` tag — [project_overview.html:726](projects/templates/projects/project_overview.html#L726), [:764](projects/templates/projects/project_overview.html#L764), [:1251](projects/templates/projects/project_overview.html#L1251); [project_detail.html:351](projects/templates/projects/project_detail.html#L351), [:401](projects/templates/projects/project_detail.html#L401), [:425](projects/templates/projects/project_detail.html#L425); [base.html:172](projects/templates/base.html#L172) — and reaching the parameter at all requires an authenticated POST with a valid CSRF token.

---

## 8. Every `{% url 'project_detail' %}` costs an extra redirect hop

**Severity: Low (performance / tidiness)**

`project_detail` is now a pure shim: [views.py:1744-1746](projects/views.py#L1744-L1746) — *"Merged into project_overview — redirect all traffic there."* Eleven template references still point at it, so each click is a 302 followed by the real request:

[my_documents.html:50](projects/templates/projects/my_documents.html#L50), [:73](projects/templates/projects/my_documents.html#L73), [:124](projects/templates/projects/my_documents.html#L124), [:187](projects/templates/projects/my_documents.html#L187), [:251](projects/templates/projects/my_documents.html#L251), [:313](projects/templates/projects/my_documents.html#L313); [design_submission_detail.html:20](projects/templates/projects/design_submission_detail.html#L20), [:55](projects/templates/projects/design_submission_detail.html#L55); [payment_request_detail.html:20](projects/templates/projects/payment_request_detail.html#L20); [project_detail.html:274](projects/templates/projects/project_detail.html#L274), [:351](projects/templates/projects/project_detail.html#L351), [:401](projects/templates/projects/project_detail.html#L401), [:425](projects/templates/projects/project_detail.html#L425).

The last four are worse than cosmetic: [:274](projects/templates/projects/project_detail.html#L274) is a **form `action`**, and [:351](projects/templates/projects/project_detail.html#L351)/[:401](projects/templates/projects/project_detail.html#L401)/[:425](projects/templates/projects/project_detail.html#L425) are `next` values. A 302 on a POST is followed as a GET, so a POST to that action loses its body. Whether `projects/project_detail.html` is still reachable at all is worth checking — no view appears to render it, since `project_detail` only redirects.

Two notification links also target the shim rather than the canonical page: `_pm_link = f'/projects/{pid}/'` at [views.py:3073](projects/views.py#L3073) and [views.py:3267](projects/views.py#L3267), plus `_ap_link` at [views.py:5453](projects/views.py#L5453) and the audit-log href at [admin/audit_log.html:283](projects/templates/projects/admin/audit_log.html#L283).

---

## 9. `tasks_drill_down` gives unlisted roles the entire portfolio

**Severity: Low-Medium (over-broad default)**
**Location:** [views.py:211-229](projects/views.py#L211-L229)

```python
if role in ('PM', 'Project Coordinator'):   ...
elif role == 'Design':                      ...
elif role == 'Site Engineer':               ...
# SCM and others: all active non-deleted projects
```

The fall-through comment says "SCM and others", but `role` here is any of the ten `ROLE_CHOICES` — so **Admin, System Admin, CEO, Finance and BD also land in the unscoped branch** and receive every task on every active project. For Finance, SCM, CEO and BD that matches their dashboards' portfolio-wide scope, so it is arguably fine. For `System Admin` — whose landing page ([views.py:8440](projects/views.py#L8440)) is a first-time-PM-assignment tool, not an operational view — it is broader than any other surface grants them.

Compounding it: the `back_url` fallback at [views.py:253](projects/views.py#L253) sends all of these roles to `dashboard_pm`, which `@role_required` then refuses (finding #2).

The three drill-down URLs are unauthenticated-by-role — `@login_required` only, [views.py:194](projects/views.py#L194) — and registered at [urls.py:239-241](projects/urls.py#L239-L241).

---

## 10. Type-match and Residential-exclusion invariants have no database-level backstop

**Severity: Low (currently clean; latent)**
**Location:** [models.py:128-155](projects/models.py#L128-L155)

`_validate_program_link()` enforces two rules — Residential may never have a Program, and a site's `project_type` must equal its `program.program_type` — and is called from `save()`, so it covers form, view, webhook and shell paths.

It does **not** cover `queryset.update()`, `bulk_create()`, `bulk_update()`, or raw SQL, none of which invoke `save()`. PostgreSQL introspection of `projects_project` confirms **no `CHECK` constraint of any kind** on the table (constraint types present: `f`, `n`, `p`, `u` only) — nothing ties `project_type` to the parent's `program_type`, and `project_type` is `NOT NULL` but accepts `''`.

Live data is clean: 0 mismatched rows, 0 Residential rows linked to a Program, 0 rows with an empty `project_type`.

The model comment at [models.py:137-139](projects/models.py#L137-L139) states the placement is deliberate (*"because they live here rather than on any one form"*), which is sound reasoning for covering `save()` paths — it just does not extend to bulk operations. Worth noting because the OPEX bulk-upload path ([views.py:2508](projects/views.py#L2508) onward) is the kind of feature that tends to reach for `bulk_create`; it currently calls `create_opex_site()` per row ([views.py:2287-2312](projects/views.py#L2287-L2312)), which does go through `save()`, so it is safe today.

---

## 11. `get_user_dashboard` silently lands profile-less and unmapped users on the Admin dashboard

**Severity: Low (already flagged in-code as a TODO)**
**Location:** [decorators.py:27-40](projects/decorators.py#L27-L40)

```python
return ROLE_DASHBOARD.get(role, '/dashboard/admin/')
...
except Exception:
    logger.warning("No UserProfile found for user %s — falling back to admin dashboard", user.username)
    return '/dashboard/admin/'
```

Two separate paths to the Admin dashboard: an unmapped role string (e.g. a `role=''` profile, permitted by `blank=True` at [models.py:522](projects/models.py#L522)) hits the `.get` default; a user with no `UserProfile` hits the `except`.

`role_required` has the mirror-image behaviour, treating a profile-less user as `'Admin'` outright ([decorators.py:67-70](projects/decorators.py#L67-L70)) — so such a user is not merely *routed* to the Admin dashboard, they **pass every `@role_required(['Admin'])` gate**, including the entire `portal-admin/` panel ([urls.py:191-214](projects/urls.py#L191-L214)).

The existing TODO at [decorators.py:37-38](projects/decorators.py#L37-L38) notes the crash risk for admin-created users without a profile but not the privilege-escalation consequence. Mitigating factors: `projects/signals.py` may auto-create profiles (not verified in this session), and reaching this state requires a `createsuperuser` or a manually-created `User` row.

---

---

# Appended 2026-07-26 — from the Role-Prefix Permission Short-Circuit audit

Findings #12–#16 were found while producing `ROLE_PREFIX_AUDIT.md`. None is the role-prefix defect itself (that is finding #5, and the full analysis is in `ROLE_PREFIX_AUDIT.md` — it is not restated here). None was fixed.

---

## 12. Project-scoped BOQ, payment-request and milestone views have no per-project authority check at all

**Severity: High (access control) — arguably wider than finding #5**

A whole family of views takes a `project_id`, reads and writes that project's records, and gates on **role alone**. Unlike the finding #5 sites, these never call `user_can_manage_project` — so there is no check to short-circuit past. A PM reaches **every** project through them, not just their own.

| File:Line | View | Gate | R/W |
|---|---|---|---|
| [views.py:3968](projects/views.py#L3968) | `boq_detail` | `role not in ('Design','SCM','PM','Project Coordinator','Admin')` → 403 | **WRITE** — dispatches `save_design`, `submit_design`, `add_item`, `delete_item`, `save_scm`, `acknowledge_scm` |
| [views.py:4151](projects/views.py#L4151) | `boq_submit` | `role != 'Design'` → 403 | **WRITE** |
| [views.py:4204](projects/views.py#L4204) | `boq_acknowledge` | `role != 'SCM'` → 403 | **WRITE** |
| [views.py:4233](projects/views.py#L4233) | `boq_request_revision` | `role not in ('PM','Project Coordinator')` → 403 | **WRITE** |
| [views.py:4285](projects/views.py#L4285) | `boq_history` | `role not in ('PM','Project Coordinator','Design','SCM','Admin')` → 403 | Read |
| [views.py:4489](projects/views.py#L4489) | `raise_payment_request` | `role != 'SCM'` → 403 | **WRITE** |
| [views.py:4585](projects/views.py#L4585) | `confirm_payment_request` | `role != 'Finance'` → 403 | **WRITE** |
| [views.py:4355](projects/views.py#L4355) | `milestone_invoice` | `@role_required(['Finance'])` | **WRITE** |
| [views.py:4381](projects/views.py#L4381) | `milestone_receive` | `@role_required(['Finance'])` | **WRITE** |
| [views.py:7291](projects/views.py#L7291) | `payment_request_detail` | `role not in ('SCM','Finance','PM','Admin')` → 403 | Read |
| [views.py:7279](projects/views.py#L7279) | `design_submission_detail` | `profile != submitted_by and role not in ('PM','Admin')` | Read |

Concretely: any PM or Project Coordinator can request a BOQ revision on any project ([views.py:4233](projects/views.py#L4233)); any Design user can submit a BOQ against any project ([views.py:4151](projects/views.py#L4151)); any Finance user can invoice or mark received any milestone on any project ([views.py:4355](projects/views.py#L4355), [:4381](projects/views.py#L4381)).

For the department-level roles (Design, SCM, Finance) this may be intended — their dashboards are portfolio-wide by documented design. For **PM and Project Coordinator** it is inconsistent with `boq_request_revision`'s own docstring at [views.py:4228](projects/views.py#L4228), which says *"Access: PM only"* — meaning the project's PM, not any PM.

Called out because finding #5 remediation would leave every row above untouched.

---

## 13. `close_issue` / `reopen_issue` are correctly gated three lines from siblings that are not

**Severity: Informational (evidence of drift, not a defect)**

Within the same block of issue-lifecycle views:

| View | File:Line | Guard |
|---|---|---|
| `update_issue_status` | [views.py:6316](projects/views.py#L6316) | `if profile.role == 'PM' and not user_can_manage_project(...)` — **leaks** |
| `resolve_issue` | [views.py:6356](projects/views.py#L6356) | same — **leaks** |
| `close_issue` | [views.py:6425](projects/views.py#L6425) | `if not _is_project_pm(profile, project): return HttpResponseForbidden(...)` — **correct** |
| `reopen_issue` | [views.py:6458](projects/views.py#L6458) | same — **correct** |
| `assign_issue` | [views.py:6492](projects/views.py#L6492) | role-prefixed — **leaks** |

`_is_project_pm` ([views.py:5959-5968](projects/views.py#L5959-L5968)) is the correct conjunction (role ∈ {PM, Project Coordinator} **AND** `user_can_manage_project`). It is defined in the same file, immediately above these views, and used by only two of the five.

Logged because it is direct evidence that the correct pattern was available and understood when the leaking siblings were written — useful context for whoever decides the policy, and it identifies `_is_project_pm` as an existing in-repo reference implementation.

---

## 14. `user_can_manage_project` ignores `is_active`; `project_managers` does not

**Severity: Low-Medium (deactivated users retain authority)**

Two helpers in the same module disagree on whether a deactivated profile still counts:

- `user_can_manage_project` ([permissions.py:34-39](projects/permissions.py#L34-L39)) reads only `assigned_pm` identity and `coordinators` membership. It **never reads `is_active`**. A soft-deactivated PM (`UserProfile.is_active=False`, [models.py:524](projects/models.py#L524)) still returns `True` and retains full management authority on their projects.
- `project_managers` ([permissions.py:80](projects/permissions.py#L80)) **does** filter: `project.coordinators.filter(is_active=True)`.

So a deactivated coordinator can still act on the project but no longer receives its notifications — authority and notification scope diverge.

`is_active` is described as *"Soft deactivation — keeps history without deleting the user"* at [models.py:524](projects/models.py#L524). Whether it is meant to revoke authority is not stated anywhere I could find. Admin deactivation is at [views.py:7403](projects/views.py#L7403)/[:7415](projects/views.py#L7415); every assignee-candidate query filters `is_active=True` ([views.py:3341](projects/views.py#L3341), [:3423](projects/views.py#L3423), [:3593](projects/views.py#L3593), [:7870](projects/views.py#L7870), [:8457](projects/views.py#L8457)), which suggests deactivation is intended to remove someone from active duty.

Login is not blocked by `is_active` either — `login_view` ([views.py:80-88](projects/views.py#L80-L88)) calls `authenticate()` and checks no profile flag, so a deactivated user can still sign in. **UNKNOWN:** whether any deactivated profile currently holds an `assigned_pm` or coordinator relationship — resolvable with a read-only query against Railway.

---

## 15. The Zoho webhook assigns `assigned_pm` with no role filter

**Severity: Medium (data integrity; already known in-code but unguarded)**

[views.py:5366-5371](projects/views.py#L5366-L5371):

```python
pm_email = (deal.get('Assign_PM', '') or '').strip()
assigned_pm = None
if pm_email:
    profile_match = UserProfile.objects.filter(user__email__iexact=pm_email).first()
    if profile_match:
        assigned_pm = profile_match
```

No `role='PM'` filter and no `is_active=True` filter. Any profile whose email appears in the Zoho deal's `Assign_PM` field becomes the project's `assigned_pm` at [views.py:5386](projects/views.py#L5386).

`limit_choices_to={'role': 'PM'}` on the field ([models.py:63](projects/models.py#L63)) is a ModelForm/admin constraint only — it is not a DB constraint, is not enforced by `Model.save()`, and does not apply here.

Consequence: a Finance or Design user can become `assigned_pm`, at which point `user_can_manage_project` returns `True` for them on that project — granting full PM authority at all twelve correctly-gated sites, entirely legitimately, because the function is role-blind by design.

The codebase already knows about this: [views.py:5965-5966](projects/views.py#L5965-L5966) calls it *"the pathological webhook case — where `assigned_pm` could point to a non-manager profile"* and cites it as the reason `_is_project_pm` keeps a role gate. So the mitigation exists at one consumer while the source remains unguarded.

Every other assignment path *does* filter: `admin_assign_pm` [views.py:7895](projects/views.py#L7895), `subadmin_projects` [views.py:8457](projects/views.py#L8457), `create_opex_site` [views.py:2303](projects/views.py#L2303).

**UNKNOWN:** whether any live row has a non-PM `assigned_pm`. Resolvable read-only: `Project.objects.exclude(assigned_pm__role='PM').exclude(assigned_pm__isnull=True)`.

---

## 16. Gantt role-visibility tests assert the finding #5 behaviour as correct

**Severity: Medium (tests lock in current behaviour and will obstruct a fix)**

`GanttRoleVisibilityTests` ([tests_gantt.py:234-283](projects/tests_gantt.py#L234-L283)) builds one project owned by `pm_view` with **no coordinators** ([tests_gantt.py:237-238](projects/tests_gantt.py#L237-L238)), then asserts that users with **no relationship to it** receive a successfully rendered `project_overview`:

- [tests_gantt.py:260-267](projects/tests_gantt.py#L260-L267) — five unrelated users (Site Engineer, Finance, SCM, Design, BD); asserts `assertIsNotNone(ctx['gantt_internal'])`, i.e. a 200 with populated context.
- [tests_gantt.py:253-258](projects/tests_gantt.py#L253-L258) — an unrelated **Project Coordinator** (`coord_v`) and CEO; asserts both receive the client Gantt. The coordinator case is precisely the finding #5 leak, asserted as expected.
- [tests_gantt.py:269-283](projects/tests_gantt.py#L269-L283) — two more unrelated CEO users.

These tests pass **because of** the role prefix at [views.py:4924](projects/views.py#L4924). Remove it and all four fail: the view raises `Http404`, and `resp.context` no longer carries `gantt_available` / `gantt_client` / `gantt_internal`.

They were written to verify Gantt *visibility*, not authorization — the unrelated-user setup is incidental to their purpose. But their present effect is to encode the current access behaviour as expected, and they will read as regressions when the policy is changed. Full analysis in `ROLE_PREFIX_AUDIT.md` §5.3.

---

## 17. OPEX sites are all `status='Draft'`, so they have never appeared on the CEO / Finance / SCM dashboards

**Severity: Medium (invalidates the stated premise of the context-switcher feature; not caused by it)**

Found while implementing the context landing screen. Logged, not fixed.

The CEO, Finance and SCM dashboards all scope their portfolio to
`status__in=['Active', 'In Progress']` — `dashboard_finance` [views.py:826](projects/views.py#L826),
`dashboard_scm` [views.py:967](projects/views.py#L967), `_get_ceo_dashboard_context`
[views.py:1312](projects/views.py#L1312). This predates the context feature and was left unchanged.

At the start of this session every OPEX site in the local DB was `Draft`:

```
{'project_type': 'OPEX',        'status': 'Draft',  'n': 7}
{'project_type': 'Residential', 'status': 'Active', 'n': 7}
```

Mid-session `IPGCL26-MB007` was activated (`activated_at=2026-07-26 06:34:05+00:00`),
giving the current distribution:

```
{'project_type': 'OPEX',        'status': 'Active', 'n': 1}
{'project_type': 'OPEX',        'status': 'Draft',  'n': 6}
{'project_type': 'Residential', 'status': 'Active', 'n': 7}
```

Consequence: **6 of the 7 OPEX sites remain invisible to these three dashboards**,
not because of the context filter but because the pre-existing status scope excludes
`Draft`. The `Tenders` card currently reads **1**, not 7.

Nothing here is a defect in the context filter, which was verified working in both
directions: `?context=tenders` shows the one Active OPEX site and zero Residential;
`?context=residential` shows all 7 Residential and zero OPEX. The open question is a
product one — whether OPEX sites are meant to be activated individually, or whether
these dashboards should widen their status scope for tender sites. Both are out of
scope for the context feature and would be real behaviour changes.

**UNKNOWN:** the status distribution on Railway production, which was not queried.
Resolvable read-only:
`Project.objects.filter(is_deleted=False).values('project_type','status').annotate(n=Count('pk'))`.

---

## 18. Pre-existing multi-line `{# #}` comment renders as visible text on the vendor form

**Severity: Low (cosmetic, but developer notes are shown to end users)**

Logged, not fixed — `vendors/vendor_form.html` is untouched by this session
(`git status --porcelain` on it is empty).

Django's `{# #}` is a **single-line** comment delimiter. A `{#` with no closing `#}`
on the same line is not parsed as a comment at all — the text renders straight onto
the page. [vendor_form.html:34-36](projects/templates/vendors/vendor_form.html#L34-L36):

```
{# Make / Brand — multiple entries, each optionally scoped to one supply category.
   Submitted as parallel lists: make_brand[] and brand_category[].
   The + button clones the template row; × removes a row (keeps at least one). #}
```

Verified against the running view — `GET /vendors/add/` returns HTTP 200 with all
three lines present in the rendered body, so Admin/SCM users adding a vendor see
internal implementation notes above the Make / Brand field.

Fix is a one-line change: swap to `{% comment %} … {% endcomment %}`.

A sweep of all templates under `projects/templates/` found this as the only remaining
instance:

```python
for p in glob.glob('projects/templates/**/*.html', recursive=True):
    for n, l in enumerate(open(p, encoding='utf-8'), 1):
        if '{#' in l and '#}' not in l.split('{#', 1)[1]:
            print(p, n)
```

---

## 19. Activated OPEX sites render on the BD dashboard with a blank phase — they look unactivated

**Severity: Medium (misreads as a data-integrity problem; likely the cause of the
"unactivated projects on BD" report)**

Investigated on request, logged, not fixed. **No queryset was changed.**

The BD filter is exactly what the earlier audit recorded and cannot admit a Draft
project — [views.py:4884](projects/views.py#L4884):

```python
Project.objects.filter(is_deleted=False, status__in=['Active', 'In Progress'])
```

`Project.status` defaults to `'Draft'` ([models.py:89](projects/models.py#L89)), the Zoho
webhook explicitly creates `status='Draft'` ([views.py:5597](projects/views.py#L5597)), and
`'Draft'` is not in the filter. The only production write of `status='Active'` is
`project_activate` ([views.py:2138](projects/views.py#L2138)), which also stamps
`activated_at`. So a project cannot reach the BD dashboard without being activated.

The real mechanism is different. `project_activate` attaches the 53-task template
**only for Residential** ([views.py:2142-2143](projects/views.py#L2142-L2143)):

```python
if project.project_type == 'Residential':
    attach_residential_template(project)
```

An activated OPEX site therefore has **zero phases and zero tasks**. Verified locally —
`IPGCL26-MB007`: `status='Active'`, `activated_at=2026-07-26 06:34:05+00:00`, `phases=0`.
Its rendered BD row:

```
project_id       = 'IPGCL26-MB007'
phase            = None          <-- blank phase column
orc_status       = 'pending'
status_badge     = 'on_time'
```

A card with a blank phase and a permanently `pending` ORC is visually
indistinguishable from an unactivated project, which is almost certainly what was
reported. The row is legitimately in scope for BD; it just has no workflow behind it.

Open product question (not a code fix): whether OPEX sites should get a task template
of their own, be excluded from BD, or render a distinct "no workflow" state.

**UNKNOWN:** Railway's actual data was not queried. If projects there show
`status='Active'` with `activated_at IS NULL`, that would be a *different* and more
serious cause than this one. Resolvable read-only:
`Project.objects.filter(is_deleted=False, status__in=['Active','In Progress'], activated_at__isnull=True)`.

---

*End of secondary findings. Nothing above was modified.*