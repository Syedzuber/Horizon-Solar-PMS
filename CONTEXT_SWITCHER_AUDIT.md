# Context Switcher — Investigate-Only Audit

**Date:** 2026-07-25
**Scope:** Project-type separation + dashboard routing, ahead of a two-context nav layer (`EPC Residential` / `Tenders`).
**Nature:** Investigation only. No code was written, edited, or deleted. No migration created. No write command run against the database.

**Database queried:** LOCAL PostgreSQL (`DATABASE_URL` from `.env`), read-only queries via `manage.py shell`. Railway production was **not** queried — all row counts in §1.4 are local-DB figures and are labelled as such.

---

## HARD STOP CONDITIONS — evaluated

| Condition | Result |
|---|---|
| `Program` model does not exist | **NOT triggered** — exists at [models.py:170](projects/models.py#L170) |
| More than one distinct mechanism determines a project's type | **NOT triggered** — exactly one: `Project.project_type`. See §1.3 for the full reasoning and the near-misses that were ruled out. |
| Any dashboard view cannot be traced to a single URL name | **NOT triggered** — all 8 dashboard views have exactly one URL pattern each ([urls.py:15-22](projects/urls.py#L15-L22)) |

No hard stop applies. Full audit proceeds.

---

# SECTION 1 — Project type discrimination

## 1.1 Does a `Program` model exist?

**Yes.** [models.py:170-254](projects/models.py#L170-L254).

### Full field list

| Field | Type | Definition |
|---|---|---|
| `program_type` | CharField(20), choices=PROGRAM_TYPE_CHOICES | [models.py:191](projects/models.py#L191) |
| `name` | CharField(200) | [models.py:192](projects/models.py#L192) |
| `client_name` | CharField(200) | [models.py:193](projects/models.py#L193) |
| `status` | CharField(20), choices=`Project.STATUS_CHOICES`, default='Draft' | [models.py:194](projects/models.py#L194) |
| `short_tender_code` | CharField(20), blank, default='' | [models.py:201](projects/models.py#L201) |
| `total_capacity` | Decimal(10,2), null, blank | [models.py:204](projects/models.py#L204) |
| `expected_completion_date` | DateField, null, blank | [models.py:208](projects/models.py#L208) |
| `planned_site_count` | PositiveInteger, null, blank | [models.py:209](projects/models.py#L209) |
| `tender_reference_number` | CharField(100), blank, default='' | [models.py:216](projects/models.py#L216) |
| `bid_value` | Decimal(14,2), null, blank | [models.py:217](projects/models.py#L217) |
| `award_date` | DateField, null, blank | [models.py:218](projects/models.py#L218) |
| `ppa_reference` | CharField(100), blank, default='' | [models.py:219](projects/models.py#L219) |
| `ppa_signed_date` | DateField, null, blank | [models.py:220](projects/models.py#L220) |
| `ppa_per_unit_rate` | Decimal(10,4), null, blank | [models.py:221](projects/models.py#L221) |
| `ppa_escalation_percentage` | Decimal(5,2), null, blank | [models.py:222](projects/models.py#L222) |
| `ppa_escalation_frequency` | CharField(50), blank, default='' | [models.py:223](projects/models.py#L223) |
| `financing_partner_name` | CharField(200), blank, default='' | [models.py:229](projects/models.py#L229) |
| `financing_assistance_type` | CharField(100), blank, default='' | [models.py:230](projects/models.py#L230) |
| `created_at` | DateTimeField(auto_now_add) | [models.py:232](projects/models.py#L232) |
| `updated_at` | DateTimeField(auto_now) | [models.py:233](projects/models.py#L233) |
| `created_by` | FK→`auth.User`, PROTECT, null, blank | [models.py:234](projects/models.py#L234) |
| `is_deleted` | Boolean, default=False | [models.py:237](projects/models.py#L237) |
| `deleted_at` | DateTimeField, null, blank | [models.py:238](projects/models.py#L238) |

There is **no** `program_id` / auto-generated reference field — confirmed deliberate at [models.py:250-251](projects/models.py#L250-L251).

### `program_type` choices

[models.py:184-187](projects/models.py#L184-L187):
```python
PROGRAM_TYPE_CHOICES = [
    ('OPEX',  'OPEX'),
    ('CAPEX', 'CAPEX'),
]
```
No `Residential`. Title-case, deliberately identical to `Project.PROJECT_TYPE_CHOICES` casing.

### Relationship to `Project`

One FK, defined **on `Project`**, not on `Program`: [models.py:49-55](projects/models.py#L49-L55)

```python
program = models.ForeignKey('Program', null=True, blank=True,
                            on_delete=models.PROTECT, related_name='sites')
```

- Cardinality: `Program` 1 — N `Project` (reverse accessor `program.sites`).
- Nullable, forward-only — no backfill of pre-existing projects.
- `on_delete=PROTECT` — a Program can never cascade-delete or orphan its sites.
- Rollup helpers: `program_rollup_annotations()` [models.py:263](projects/models.py#L263) (list view) and `get_program_rollup()` [models.py:282](projects/models.py#L282) (detail view). Both compute live from child-site `status`; no stored counter.

## 1.2 How is a `Project` identified as Residential vs OPEX vs CAPEX?

**One field:** `Project.project_type` — [models.py:44](projects/models.py#L44)

```python
project_type = models.CharField(max_length=20, choices=PROJECT_TYPE_CHOICES)
```

**Choice values** — [models.py:8-12](projects/models.py#L8-L12):
```python
PROJECT_TYPE_CHOICES = [
    ('Residential', 'Residential'),
    ('OPEX',        'OPEX'),
    ('CAPEX',       'CAPEX'),
]
```

Title-case string values. No integer codes, no separate flag field, no subclass/proxy model.

## 1.3 Is there more than one way this determination is made?

**No.** Every place a project's type is decided reads `Project.project_type` directly. Below is the exhaustive list of every read/compare/write site, followed by four constructs that *look* like alternative mechanisms and are ruled out with evidence.

### A. Every place `Project.project_type` is READ or COMPARED

| # | Location | Code | Purpose |
|---|---|---|---|
| 1 | [views.py:1935](projects/views.py#L1935) | `if project.project_type == 'Residential':` | Attach 53-task Residential template on activation |
| 2 | [views.py:1954](projects/views.py#L1954) | `if project.project_type == 'Residential':` | Success-message wording after activation |
| 3 | [views.py:2789](projects/views.py#L2789) | `.filter(task_name=…, project_type=project.project_type)` | Resolve the `ChecklistTaskLink` for a task |
| 4 | [views.py:5222](projects/views.py#L5222) | `gantt_available = (project.project_type == 'Residential')` | Gate the Gantt block on `project_overview` |
| 5 | [templates/projects/project_detail.html:38](projects/templates/projects/project_detail.html#L38) | `{{ project.project_type }}` | Display only |
| 6 | [templates/projects/project_overview.html:105](projects/templates/projects/project_overview.html#L105) | `{{ project.project_type }}` | Display only |
| 7 | [templates/projects/project_list.html:34](projects/templates/projects/project_list.html#L34) | `{{ project.project_type }}` | Display only |
| 8 | [templates/projects/admin/projects_list.html:47](projects/templates/projects/admin/projects_list.html#L47) | `{{ project.project_type }}` | Display only |
| 9 | [templates/dashboard/ceo.html:654](projects/templates/dashboard/ceo.html#L654) | `{{ p.project_type }}` | Display only, inside a project card |
| 10 | [templates/projects/project_form.html:79-88](projects/templates/projects/project_form.html#L79-L88) | `<select name="project_type">` / read-only echo | Create form input; Edit shows it disabled |
| 11 | [templates/projects/subadmin/projects.html:43-46](projects/templates/projects/subadmin/projects.html#L43-L46) | `== 'residential'` / `== 'commercial_opex'` | **Dead branches** — values not in `PROJECT_TYPE_CHOICES`. See FINDINGS_SECONDARY #4 |
| 12 | [templates/projects/subadmin/projects.html:115-118](projects/templates/projects/subadmin/projects.html#L115-L118) | same as above | **Dead branches**, same defect |

### B. Every place `Project.project_type` is WRITTEN

| # | Location | Code | Path |
|---|---|---|---|
| 1 | [views.py:1734](projects/views.py#L1734) | `initial={'project_type': 'Residential'}` | PM manual create — form default |
| 2 | [forms.py:249](projects/forms.py#L249) + [forms.py:265-290](projects/forms.py#L265-L290) | `ProjectCreateForm` field, OPEX stripped from choices + `clean_project_type` rejects OPEX | PM manual create |
| 3 | [views.py:2294](projects/views.py#L2294) | `site.project_type = 'OPEX'` (forced) | OPEX site creation under a Program (`create_opex_site`) |
| 4 | [views.py:5383](projects/views.py#L5383) | `project_type='Residential'` | Zoho `deal-closed` webhook — hardcoded |

`ProjectEditForm` deliberately **excludes** `project_type` ([forms.py:306](projects/forms.py#L306)) — type is immutable after creation via the UI.

### C. Constructs that resemble a second mechanism — ruled out

| Construct | Why it is NOT a second mechanism |
|---|---|
| **`Program.program_type`** ([models.py:191](projects/models.py#L191)) | Determines a **Program's** type, not a Project's. For a linked site the two are held equal by `Project._validate_program_link()` ([models.py:128-151](projects/models.py#L128-L151)), which runs inside `save()` ([models.py:155](projects/models.py#L155)) on every save path. No code reads `project.program.program_type` to decide what a project *is*. |
| **`project_id` prefix** (`HRP-RES-…`, `HRP-OPX-…`, `HRP-CAP-…`) | Encoded at generation time only — `PREFIX_MAP` in `generate_project_id()` ([utils.py:16-21](projects/utils.py#L16-L21)). Grepped for every `startswith` / `HRP-` in `projects/*.py`: the prefix is **never parsed back** to infer type. `project_id__startswith` at [utils.py:32](projects/utils.py#L32) is used solely to find the highest issued sequence number. |
| **`Project.program` being null / non-null** | Never used as a type proxy. All `program`-null tests are linkage tests (`_validate_program_link` short-circuit at [models.py:141](projects/models.py#L141); `program.sites` rollup filters). |
| **`TaskDurationTemplate.project_type` and `ChecklistTaskLink.project_type`** | Separate lookup-key columns on *other* models, used to select a template/checklist, not to classify a Project. Note the **vocabularies differ**: `TaskDurationTemplate.PROJECT_TYPE_CHOICES = [('residential','Residential')]` — lowercase — [models.py:1394-1396](projects/models.py#L1394-L1396), queried as `filter(project_type='residential')` at [views.py:8026](projects/views.py#L8026), [views.py:8776](projects/views.py#L8776), [utils.py:568](projects/utils.py#L568). `ChecklistTaskLink.project_type` reuses `Project.PROJECT_TYPE_CHOICES` (Title-case) — [models.py:1473](projects/models.py#L1473). These two never interoperate, so the casing split causes no live bug today, but it is a divergence a context switcher must not accidentally unify. |

**Conclusion:** exactly one mechanism. Any context switcher can key off `Project.project_type` alone.

## 1.4 Do OPEX and CAPEX exist as `Project` rows, `Program` rows, both, or neither?

**Local DB only** (Railway not queried — see header). Queried 2026-07-25.

### `Project` rows by `project_type`

| `project_type` | All rows | `is_deleted=False` |
|---|---|---|
| `Residential` | 15 | 7 |
| `OPEX` | 7 | 7 |
| `CAPEX` | **0** | **0** |
| **Total** | **22** | **14** |

### `Program` rows by `program_type`

| `program_type` | Rows |
|---|---|
| `OPEX` | 1 |
| `CAPEX` | **0** |
| **Total** | **1** |

### `Project.program` linkage

| | Rows |
|---|---|
| `program IS NULL` | 15 |
| `program IS NOT NULL` | 7 |

### Answer

- **OPEX** exists as **both** — 7 `Project` rows (all 7 linked to the single `Program`) and 1 `Program` row.
- **CAPEX** exists as **neither** — zero `Project` rows and zero `Program` rows. The CAPEX code paths (`PROGRAM_TYPE_CHOICES`, `financing_*` fields, `PREFIX_MAP['CAPEX']`) are present but have never been exercised against real data.
- The 15 `program IS NULL` rows are exactly the 15 `Residential` rows.

**UNKNOWN:** the corresponding Railway production counts. Determining them requires a read-only connection to the Railway `DATABASE_URL`, which was not opened in this session. A prior note in project memory records the Program foundation as "not yet on Railway", but that is not verified here.

## 1.5 Can a `Project` exist with no type, or with a type inconsistent with its parent `Program`?

### Enforcement summary

| Rule | DB level | Model level | Form level |
|---|---|---|---|
| `project_type` must be one of the 3 choices | **NO** | NO (choices are not validated by `save()`) | YES (`ProjectCreateForm`) |
| `project_type` must be non-empty | Partial — `NOT NULL` only | NO | YES |
| Site type must equal parent Program type | **NO** | **YES** — `_validate_program_link()` | n/a |
| Residential may never have a Program | **NO** | **YES** — `_validate_program_link()` | n/a |

### Evidence

**DB level** — introspection of `projects_project` on local PostgreSQL returned:
- `('projects_project_project_type_not_null', 'n', 'NOT NULL project_type')`
- `('projects_project_program_id_3eb7e51b_fk_projects_program_id', 'f', 'FOREIGN KEY (program_id) REFERENCES projects_program(id) DEFERRABLE INITIALLY DEFERRED')`
- Column nullability: `program_id` YES-nullable (bigint), `project_type` NOT NULL (varchar), `site_code` YES-nullable (varchar)
- **No `CHECK` constraint of any kind** on the table (constraint types returned were only `f`, `n`, `p`, `u`).

So at the DB layer: `project_type` cannot be `NULL`, but **`''` (empty string) is accepted**, and no constraint ties `project_type` to `program_id`'s `program_type`.

**Model level** — `Project._validate_program_link()` [models.py:128-151](projects/models.py#L128-L151), invoked unconditionally from `Project.save()` at [models.py:155](projects/models.py#L155), enforces two rules and raises `ValidationError`:
1. [models.py:144-145](projects/models.py#L144-L145) — a `Residential` project linked to any Program is rejected.
2. [models.py:146-151](projects/models.py#L146-L151) — a site whose `project_type` ≠ `program.program_type` is rejected.

Because it lives in `save()` rather than a form, it covers form, view, webhook, and shell paths. It **short-circuits when `program_id is None`** ([models.py:141-142](projects/models.py#L141-L142)) — so an unlinked project gets no type validation at all from this path.

**Can a Project exist with no type set?**
- Via any current UI path: **no** — `ProjectCreateForm` requires it, `OpexSiteForm` path forces `'OPEX'` ([views.py:2294](projects/views.py#L2294)), the Zoho webhook hardcodes `'Residential'` ([views.py:5383](projects/views.py#L5383)).
- Via `manage.py shell` / raw SQL / a future code path: **yes** — `Project.objects.create(project_type='', …)` would pass both `save()` and the DB. Nothing rejects an empty or out-of-vocabulary string.
- Current local data: 0 rows with `project_type=''`, 0 with `NULL`.

**Can a Project have a type inconsistent with its parent Program?**
- Not through `Model.save()` — the model-level check blocks it on every save path.
- **Yes** through `queryset.update()`, `bulk_create()`, `bulk_update()`, or raw SQL, all of which bypass `save()`. No DB constraint backstops this.
- Current local data: **0 mismatched rows, 0 Residential-linked rows** — the invariant holds in practice today.

---

# SECTION 2 — Dashboard routing inventory

## 2.1 Every dashboard view

All 8 are registered in [urls.py:15-22](projects/urls.py#L15-L22). Every one maps to exactly one URL pattern and one URL name.

| URL pattern | URL name | View | Template | Decorators / roles that reach it |
|---|---|---|---|---|
| `dashboard/admin/` | `dashboard_admin` | [views.py:104](projects/views.py#L104) | `dashboard/admin.html` | `@login_required` **only** — **any authenticated user**. Docstring at [views.py:105](projects/views.py#L105) states this is intentional ("Admin nav is in the template"). |
| `dashboard/pm/` | `dashboard_pm` | [views.py:268](projects/views.py#L268) | `dashboard/pm.html` | `@role_required(['PM', 'Project Coordinator'])` [views.py:267](projects/views.py#L267) |
| `dashboard/site-engineer/` | `dashboard_site_engineer` | [views.py:526](projects/views.py#L526) | `dashboard/site-engineer.html` | `@role_required(['Site Engineer'])` [views.py:525](projects/views.py#L525) |
| `dashboard/design/` | `dashboard_design` | [views.py:694](projects/views.py#L694) | `dashboard/design.html` | `@role_required(['Design'])` [views.py:693](projects/views.py#L693) |
| `dashboard/finance/` | `dashboard_finance` | [views.py:817](projects/views.py#L817) | `dashboard/finance.html` | `@role_required(['Finance'])` [views.py:816](projects/views.py#L816) |
| `dashboard/scm/` | `dashboard_scm` | [views.py:938](projects/views.py#L938) | `dashboard/scm.html` | `@role_required(['SCM'])` [views.py:937](projects/views.py#L937) |
| `dashboard/ceo/` | `dashboard_ceo` | [views.py:1540](projects/views.py#L1540) | `dashboard/ceo.html` | `@login_required` **only** [views.py:1539](projects/views.py#L1539) — **any authenticated user**, despite the docstring at [views.py:1541](projects/views.py#L1541) claiming "Access: CEO role only". See FINDINGS_SECONDARY #1. |
| `dashboard/bd/` | `dashboard_bd` | [views.py:4663](projects/views.py#L4663) | `dashboard/bd.html` | `@role_required(['BD'])` [views.py:4662](projects/views.py#L4662) |

### Adjacent landing pages that are dashboards in everything but name

Not under `/dashboard/`, but they are where two roles land after login and must be treated as context surfaces:

| URL pattern | URL name | View | Template | Roles |
|---|---|---|---|---|
| `sub-admin/projects/` | `subadmin_projects` | [views.py:8440](projects/views.py#L8440) | `projects/subadmin/projects.html` | `@role_required(['System Admin'])` — this is the **System Admin landing page** per [decorators.py:12](projects/decorators.py#L12) |
| `portal-admin/projects/` | `admin_project_list` | [views.py:7856](projects/views.py#L7856) | `projects/admin/projects_list.html` | `@role_required(['Admin'])` — the Admin all-projects table; `project_list` redirects Admins here |

## 2.2 Does each filter by project type?

| Dashboard | Filters by project type? | Project queryset (evidence) |
|---|---|---|
| `dashboard_admin` | **No** | Renders a static template; issues no project query at all ([views.py:106](projects/views.py#L106)) |
| `dashboard_pm` | **No** | `Project.objects.filter(Q(assigned_pm=…) \| Q(coordinators=…))` [views.py:282-286](projects/views.py#L282-L286); card loop at [views.py:333](projects/views.py#L333) filters on `status` + `is_deleted` only |
| `dashboard_site_engineer` | **No** | [views.py:546-550](projects/views.py#L546-L550) — `is_deleted`, `status`, `phases__tasks__assigned_to` |
| `dashboard_design` | **No** | [views.py:705-715](projects/views.py#L705-L715) — `assigned_design` ∪ task-assignment, plus `is_deleted`/`status` |
| `dashboard_finance` | **No** | [views.py:825-838](projects/views.py#L825-L838) — `is_deleted=False, status__in=['Active','In Progress']`. Explicitly portfolio-wide per comment at [views.py:821-822](projects/views.py#L821-L822) |
| `dashboard_scm` | **No** | [views.py:966-970](projects/views.py#L966-L970) — same portfolio-wide filter |
| `dashboard_ceo` | **No** | [views.py:1310-1318](projects/views.py#L1310-L1318) — same portfolio-wide filter. Type is *displayed* on the card ([ceo.html:654](projects/templates/dashboard/ceo.html#L654)) but never filtered on |
| `dashboard_bd` | **No** | [views.py:4676-4694](projects/views.py#L4676-L4694) — same portfolio-wide filter |
| `subadmin_projects` | **No** | All projects; the type-badge template branches are dead (§1.3 A#11-12) |
| `admin_project_list` | **No** | [views.py:7858-7867](projects/views.py#L7858-L7867) — `is_deleted=False` only |

**Zero of the ten filter by `project_type`.** The scoping axes actually in use are: `is_deleted`, `status`, and role-ownership (`assigned_pm` / `coordinators` / `assigned_design` / task assignment).

The **only** type-conditional behaviour anywhere in a project-facing view is the Gantt gate at [views.py:5222](projects/views.py#L5222), and that lives on `project_overview`, not a dashboard.

**Direct consequence for the context switcher:** the 7 live OPEX sites currently appear intermixed with the 7 live Residential projects on the Finance, SCM, CEO, and BD dashboards, and on `admin_project_list`. There is no existing filter to extend — the type axis must be introduced from scratch on each queryset listed above.

## 2.3 Single shared view with role branching, or separate views per role?

**Separate views per role.** Eight independent view functions, eight templates, eight URL patterns. There is no shared dashboard dispatcher and no `if role == …` branching *inside* a dashboard to select content.

The actual structure:

1. **Role → view is resolved before the view runs**, by URL. `ROLE_DASHBOARD` ([decorators.py:10-24](projects/decorators.py#L10-L24)) maps each role string to a hardcoded dashboard **path string** (not a URL name):
   ```python
   ROLE_DASHBOARD = {
       'Admin': '/dashboard/admin/', 'System Admin': '/sub-admin/projects/',
       'PM': '/dashboard/pm/', 'Project Coordinator': '/dashboard/pm/',
       'Site Engineer': '/dashboard/site-engineer/', 'Design': '/dashboard/design/',
       'Finance': '/dashboard/finance/', 'SCM': '/dashboard/scm/',
       'CEO': '/dashboard/ceo/', 'BD': '/dashboard/bd/',
   }
   ```
   `get_user_dashboard(user)` ([decorators.py:27-40](projects/decorators.py#L27-L40)) does a `.get(role, '/dashboard/admin/')` — **an unmapped or profile-less user silently lands on the Admin dashboard.**

2. **`@role_required` enforces the mapping** ([decorators.py:53-76](projects/decorators.py#L53-L76)): a role not in `allowed_roles` gets a message and is redirected to its own `get_user_dashboard(...)`. A user with no `UserProfile` is treated as `'Admin'` ([decorators.py:68-70](projects/decorators.py#L68-L70)).

3. **Two dashboards opt out of the mapping's enforcement**: `dashboard_admin` and `dashboard_ceo` carry only `@login_required`, so the redirect map is the *only* thing keeping other roles off them — a directly-typed URL reaches them.

4. **The only genuine in-view role branching** in the dashboard family is:
   - `dashboard_pm` computing `role_label` for PM vs Project Coordinator ([views.py:289-290](projects/views.py#L289-L290)) — cosmetic; the queryset is identical.
   - `tasks_drill_down` ([views.py:195-263](projects/views.py#L195-L263)), a shared drill-down page whose scoping mirrors the caller's dashboard via `if role in (...)` at [views.py:211-228](projects/views.py#L211-L228). Roles not matched (Admin, CEO, Finance, BD, System Admin) fall through the chain and receive **all** active non-deleted projects ([views.py:229](projects/views.py#L229)).

5. **Second role→dashboard map exists**, `_ROLE_DASHBOARD` at [views.py:177-185](projects/views.py#L177-L185), used only to compute `tasks_drill_down`'s back-link ([views.py:253](projects/views.py#L253)). It maps to **URL names** (not paths), omits `Admin`, `System Admin` and `Project Coordinator`, and keys BD as `'BD / Sales'` — which never matches `UserProfile.role == 'BD'`. See FINDINGS_SECONDARY #2.

---

# SECTION 3 — Entry point inventory

## 3.1 Every `redirect()` whose target is a dashboard or project view

### To a dashboard

| Location | Target | Trigger |
|---|---|---|
| [views.py:78](projects/views.py#L78) | `get_user_dashboard(request.user)` | Already-authenticated user hits `/login/` |
| [views.py:86](projects/views.py#L86) | `get_user_dashboard(user)` | Successful `POST /login/` |
| [decorators.py:73](projects/decorators.py#L73) | `get_user_dashboard(request.user)` | **Every** `@role_required` rejection, portal-wide |
| [views.py:1707](projects/views.py#L1707) | `admin_project_list` | `project_list` for role Admin |
| [views.py:1709](projects/views.py#L1709) | `dashboard_ceo` | `project_list` for role CEO |
| [views.py:1710](projects/views.py#L1710) | `dashboard_pm` | `project_list` fallback (PM) |
| [views.py:4362](projects/views.py#L4362), [4369](projects/views.py#L4369), [4377](projects/views.py#L4377), [4389](projects/views.py#L4389), [4396](projects/views.py#L4396), [4401](projects/views.py#L4401), [4407](projects/views.py#L4407), [4439](projects/views.py#L4439) | `dashboard_finance` | `milestone_invoice` / `milestone_receive` outcomes |
| [views.py:4485](projects/views.py#L4485), [4516](projects/views.py#L4516), [4522](projects/views.py#L4522), [4536](projects/views.py#L4536), [4550](projects/views.py#L4550), [4574](projects/views.py#L4574) | `dashboard_scm` | `raise_payment_request` outcomes |
| [views.py:7885](projects/views.py#L7885), [7892](projects/views.py#L7892), [7898](projects/views.py#L7898), [7949](projects/views.py#L7949) | `admin_project_list` | `admin_assign_pm` outcomes |
| [views.py:8448](projects/views.py#L8448), [8455](projects/views.py#L8455), [8469](projects/views.py#L8469) | `subadmin_projects` | System Admin PM-assignment outcomes |

### To a project view

| Location | Target | Trigger |
|---|---|---|
| [views.py:1732](projects/views.py#L1732) | `project_overview` | `project_create` success |
| [views.py:1746](projects/views.py#L1746) | `project_overview` | **`project_detail` is now a pure redirect to `project_overview`** ([views.py:1744-1746](projects/views.py#L1744-L1746)) |
| [views.py:1754](projects/views.py#L1754) | `project_overview` | `project_delete` non-POST |
| [views.py:1760](projects/views.py#L1760) | `project_list` | `project_delete` success (→ re-dispatches by role, above) |
| [views.py:1777](projects/views.py#L1777), [1784](projects/views.py#L1784), [1841](projects/views.py#L1841) | `project_overview` | `project_edit` / `project_field_edit` |
| [views.py:1884](projects/views.py#L1884), [1890](projects/views.py#L1890), [1910](projects/views.py#L1910), [1920](projects/views.py#L1920), [1925](projects/views.py#L1925), [1959](projects/views.py#L1959) | `project_overview` | `project_activate` |
| [views.py:1971](projects/views.py#L1971), [1980](projects/views.py#L1980), [1984](projects/views.py#L1984), [1988](projects/views.py#L1988) | `project_overview` | `project_recalculate_dates` |
| [views.py:1999](projects/views.py#L1999), [2009](projects/views.py#L2009), [2013](projects/views.py#L2013), [2031](projects/views.py#L2031) | `project_overview` | `task_add` |
| [views.py:2054](projects/views.py#L2054), [2073](projects/views.py#L2073) | `project_overview` | `enable_cascade_scheduling`, `assign_coordinators` |
| [views.py:2887](projects/views.py#L2887), [2927](projects/views.py#L2927), [2946](projects/views.py#L2946), [2963](projects/views.py#L2963), [2985](projects/views.py#L2985), [3107](projects/views.py#L3107) | `project_overview` | `task_status_update` family |
| [views.py:3396](projects/views.py#L3396), [3474](projects/views.py#L3474), [3500](projects/views.py#L3500), [3519](projects/views.py#L3519), [3538](projects/views.py#L3538), [3569](projects/views.py#L3569), [3639](projects/views.py#L3639) | `project_overview` | `task_set_due_date`, `task_assign*` |
| [views.py:4452](projects/views.py#L4452), [4474](projects/views.py#L4474), [4581](projects/views.py#L4581), [4599](projects/views.py#L4599), [4605](projects/views.py#L4605), [4654](projects/views.py#L4654) | `project_overview` | `milestone_create`, `set_milestone_amounts`, `confirm_payment_request` |
| [views.py:4987](projects/views.py#L4987), [4996](projects/views.py#L4996), [5028](projects/views.py#L5028) | `project_overview` | `project_overview` POST self-redirects |
| [views.py:5580](projects/views.py#L5580), [5591](projects/views.py#L5591), [5598](projects/views.py#L5598), [5663](projects/views.py#L5663), [5674](projects/views.py#L5674), [5698](projects/views.py#L5698) | `project_overview` | document upload / delete |
| [views.py:5978](projects/views.py#L5978), [5994](projects/views.py#L5994), [6062](projects/views.py#L6062) | `project_overview` | `create_project_issue` |
| [views.py:2191](projects/views.py#L2191), [2222](projects/views.py#L2222), [2236](projects/views.py#L2236), [2246](projects/views.py#L2246), [2333](projects/views.py#L2333) | `program_detail` | Program create/edit/delete, OPEX site create |
| [views.py:2257](projects/views.py#L2257) | `program_list` | `program_delete` success |

### Open-redirect surface

Nine sites honour an unvalidated `next` POST parameter and redirect to it verbatim: [views.py:2984](projects/views.py#L2984), [3106](projects/views.py#L3106), [5662](projects/views.py#L5662), [5697](projects/views.py#L5697), [5796](projects/views.py#L5796), [5835](projects/views.py#L5835). Each falls back to `project_overview` / `task_detail`. In practice `next` is always populated from a `{% url %}` in a template hidden input, but **nothing validates it server-side**. Relevant to the switcher: `next` is the mechanism by which a user returns to their originating page after an action, so any context parameter must survive it.

## 3.2 Project URLs constructed for notifications

**`notifications.py` itself constructs no URLs.** `send_notification(...)` takes `link=''` as a parameter ([notifications.py:28](projects/notifications.py#L28)) and stores it verbatim on the in-app `Notification` row ([notifications.py:107](projects/notifications.py#L107)). WhatsApp URLs travel as ordinary `template_params` entries; email URLs are embedded in the message body string. **Every URL is built at the call site in `views.py`.**

### Relative `link=` values (in-app notification click-through)

| Call site | Link expression | Resolved shape |
|---|---|---|
| [views.py:3073](projects/views.py#L3073) → [3087](projects/views.py#L3087) | `_pm_link = f'/projects/{project.project_id}/'` | project (→ redirects to `/overview/`) |
| [views.py:3267](projects/views.py#L3267) → [3281](projects/views.py#L3281) | `_pm_link = f'/projects/{project.project_id}/'` | project |
| [views.py:3351](projects/views.py#L3351) → [3378](projects/views.py#L3378) | `task_url = f'/projects/{pid}/tasks/{task.pk}/'` | task |
| [views.py:3433](projects/views.py#L3433) → [3459](projects/views.py#L3459) | `task_url = f'/projects/{pid}/tasks/{task.pk}/'` | task |
| [views.py:3623](projects/views.py#L3623) → [3632](projects/views.py#L3632) | `_link = f'/projects/{pid}/overview/'` | project overview |
| [views.py:3872](projects/views.py#L3872) → [3892](projects/views.py#L3892) | `boq_link = f'/projects/{pid}/boq/'` | BOQ |
| [views.py:4626](projects/views.py#L4626) → [4645](projects/views.py#L4645) | `_ip_link = f'/projects/{pid}/overview/'` | project overview |
| [views.py:5424](projects/views.py#L5424) | `f'/projects/{pid}/overview/'` | project overview (Zoho: unassigned-PM admin alert) |
| [views.py:5453](projects/views.py#L5453) → [5468](projects/views.py#L5468) | `_ap_link = f'/projects/{pid}/'` | project (Zoho: PM assignment) |
| [views.py:6028](projects/views.py#L6028), [6124](projects/views.py#L6124), [6230](projects/views.py#L6230) | `_ic_link = f'/issues/{issue.pk}/'` | issue |
| [views.py:6057](projects/views.py#L6057), [6153](projects/views.py#L6153), [6259](projects/views.py#L6259) | `f'/issues/{issue.pk}/'` | issue |
| [views.py:6381](projects/views.py#L6381) → [6400](projects/views.py#L6400) | `issue_link = f'/issues/{issue.pk}/'` | issue |
| [views.py:7919](projects/views.py#L7919) → [7932](projects/views.py#L7932) | `_link = f'/projects/{pid}/overview/'` | project overview (`admin_assign_pm`) |

Rendered as the anchor href in [templates/projects/notifications.html:18](projects/templates/projects/notifications.html#L18): `<a href="{{ notif.link|default:'#' }}">`.

### Absolute URLs — email (ZeptoMail) and WhatsApp (Interakt)

Two different absolute-URL strategies coexist:

**(a) Hardcoded production host**, `https://horizon-solar-pms-production.up.railway.app`, string-concatenated into the email body:
[views.py:3081](projects/views.py#L3081), [3275](projects/views.py#L3275), [3360](projects/views.py#L3360), [3442](projects/views.py#L3442), [3885](projects/views.py#L3885), [4635](projects/views.py#L4635), [5442](projects/views.py#L5442), [5462](projects/views.py#L5462), [6036](projects/views.py#L6036), [6132](projects/views.py#L6132), [6238](projects/views.py#L6238), [6390](projects/views.py#L6390), [7926](projects/views.py#L7926).

**(b) `request.build_absolute_uri(...)`**, used for the WhatsApp `template_params` URL:
[views.py:3352](projects/views.py#L3352), [3434](projects/views.py#L3434), [3873](projects/views.py#L3873), [5452](projects/views.py#L5452), [6382](projects/views.py#L6382), [7920](projects/views.py#L7920).

**(c) A third, settings-driven host** in the EOD digest command: `settings.APP_BASE_URL`, defaulting to the same Railway host — [management/commands/send_eod_digest.py:92-93](projects/management/commands/send_eod_digest.py#L92-L93), passed into the template context as `app_url` at [send_eod_digest.py:269](projects/management/commands/send_eod_digest.py#L269) and [:392](projects/management/commands/send_eod_digest.py#L392).

Three independent host-resolution strategies is a pre-existing inconsistency (FINDINGS_SECONDARY #3). It matters here because a context-scoped URL scheme would have to be applied at all three.

**Interakt (WhatsApp) payload shape:** [notifications.py:143-154](projects/notifications.py#L143-L154) — `headerValues = params[:1]`, `bodyValues = params[1:]`. The project URL is passed positionally, e.g. `project_url_abs` as `body[1]` at [views.py:5474](projects/views.py#L5474). Any change to a URL param's shape must be re-registered in the Interakt console; the ordering is not derivable from the code.

## 3.3 Every template `{% url %}` pointing at a dashboard or project view

### To a dashboard

| Template | Line | Target |
|---|---|---|
| `projects/admin_whatsapp_log.html` | [25](projects/templates/projects/admin_whatsapp_log.html#L25) | `dashboard_admin` |
| `projects/portal_activity_log.html` | [14](projects/templates/projects/portal_activity_log.html#L14) | `dashboard_admin` |
| `dashboard/ceo.html` | [63](projects/templates/dashboard/ceo.html#L63) | `dashboard_ceo` (self-link) |
| `projects/admin/admin_base.html` | [64](projects/templates/projects/admin/admin_base.html#L64), [70](projects/templates/projects/admin/admin_base.html#L70) | hardcoded `/dashboard/admin/` (not `{% url %}`) |
| `projects/admin/admin_base.html` | [86](projects/templates/projects/admin/admin_base.html#L86) | hardcoded `/portal-admin/projects/` |
| `projects/subadmin/subadmin_base.html` | [36](projects/templates/projects/subadmin/subadmin_base.html#L36), [54](projects/templates/projects/subadmin/subadmin_base.html#L54) | `subadmin_projects` |
| `dashboard/admin.html` | [10-12](projects/templates/dashboard/admin.html#L10-L12) | `user_list`, `admin_audit_log`, `admin_master_switches` |

### To `project_overview` (the canonical project page)

| Template | Line(s) |
|---|---|
| `dashboard/pm.html` | [71](projects/templates/dashboard/pm.html#L71) (draft card), [300](projects/templates/dashboard/pm.html#L300) (active card) |
| `dashboard/site-engineer.html` | [247](projects/templates/dashboard/site-engineer.html#L247) |
| `dashboard/design.html` | [253](projects/templates/dashboard/design.html#L253) |
| `dashboard/finance.html` | [256](projects/templates/dashboard/finance.html#L256) |
| `dashboard/scm.html` | [218](projects/templates/dashboard/scm.html#L218) |
| `dashboard/bd.html` | [243](projects/templates/dashboard/bd.html#L243) |
| `dashboard/ceo.html` | [622](projects/templates/dashboard/ceo.html#L622) |
| `projects/admin/projects_list.html` | [37](projects/templates/projects/admin/projects_list.html#L37) (row `onclick`), [40](projects/templates/projects/admin/projects_list.html#L40) |
| `projects/project_list.html` | [31](projects/templates/projects/project_list.html#L31) (row `onclick`), [32](projects/templates/projects/project_list.html#L32) |
| `projects/program_detail.html` | [95](projects/templates/projects/program_detail.html#L95) — **the Program→site link** |
| `projects/portal_activity_log.html` | [105](projects/templates/projects/portal_activity_log.html#L105) |
| `projects/issue_detail.html` | [12](projects/templates/projects/issue_detail.html#L12) |
| `projects/boq_detail.html` | [26](projects/templates/projects/boq_detail.html#L26) |
| `projects/task_detail.html` | [17](projects/templates/projects/task_detail.html#L17) |
| `projects/project_timeline.html` | [15](projects/templates/projects/project_timeline.html#L15) |
| `projects/delivery_challan_detail.html` | [40](projects/templates/projects/delivery_challan_detail.html#L40) |
| `projects/delivery_challan_create.html` | [11](projects/templates/projects/delivery_challan_create.html#L11), [138](projects/templates/projects/delivery_challan_create.html#L138) |
| `projects/task_add_form.html` | [7](projects/templates/projects/task_add_form.html#L7), [20](projects/templates/projects/task_add_form.html#L20) |
| `projects/task_assign_form.html` | [6](projects/templates/projects/task_assign_form.html#L6), [19](projects/templates/projects/task_assign_form.html#L19) |
| `projects/assign_coordinators_form.html` | [6](projects/templates/projects/assign_coordinators_form.html#L6), [38](projects/templates/projects/assign_coordinators_form.html#L38) |
| `projects/project_detail.html` | [29](projects/templates/projects/project_detail.html#L29) |
| `projects/project_overview.html` | [135](projects/templates/projects/project_overview.html#L135), [278](projects/templates/projects/project_overview.html#L278), [726](projects/templates/projects/project_overview.html#L726), [764](projects/templates/projects/project_overview.html#L764), [1251](projects/templates/projects/project_overview.html#L1251) (form actions + `next` hidden inputs) |

### To `project_detail` (which is a redirect shim → `project_overview`)

| Template | Line(s) |
|---|---|
| `projects/my_documents.html` | [50](projects/templates/projects/my_documents.html#L50), [73](projects/templates/projects/my_documents.html#L73), [124](projects/templates/projects/my_documents.html#L124), [187](projects/templates/projects/my_documents.html#L187), [251](projects/templates/projects/my_documents.html#L251), [313](projects/templates/projects/my_documents.html#L313) |
| `projects/design_submission_detail.html` | [20](projects/templates/projects/design_submission_detail.html#L20), [55](projects/templates/projects/design_submission_detail.html#L55) |
| `projects/payment_request_detail.html` | [20](projects/templates/projects/payment_request_detail.html#L20) |
| `projects/project_detail.html` | [274](projects/templates/projects/project_detail.html#L274), [351](projects/templates/projects/project_detail.html#L351), [401](projects/templates/projects/project_detail.html#L401), [425](projects/templates/projects/project_detail.html#L425) (form action + `next` inputs) |

Every one of these costs an extra 302 hop through [views.py:1746](projects/views.py#L1746).

### To `project_list` (role-dispatching shim)

`projects/project_overview.html` [96](projects/templates/projects/project_overview.html#L96), `projects/project_detail.html` [31](projects/templates/projects/project_detail.html#L31), `projects/project_form.html` [7](projects/templates/projects/project_form.html#L7) and [144](projects/templates/projects/project_form.html#L144).

This is the "← Projects" breadcrumb. It resolves per-role at [views.py:1705-1710](projects/views.py#L1705-L1710) and is `@role_required(['PM','Admin','CEO'])` at [views.py:1696](projects/views.py#L1696) — **so a Project Coordinator, Design, SCM, Finance, SE or BD user clicking "← Projects" on a project page is bounced to their own dashboard with an error message.**

### To Program views

`base.html` [52](projects/templates/base.html#L52) (**the global nav "Programs" link, gated to Admin/PM/CEO**), `projects/program_form.html` [7](projects/templates/projects/program_form.html#L7) and [127](projects/templates/projects/program_form.html#L127), `projects/program_detail.html` [7](projects/templates/projects/program_detail.html#L7), `projects/program_list.html` [30](projects/templates/projects/program_list.html#L30), `projects/opex_site_form.html` [7](projects/templates/projects/opex_site_form.html#L7) and [88](projects/templates/projects/opex_site_form.html#L88), `projects/opex_site_bulk_upload.html` [7](projects/templates/projects/opex_site_bulk_upload.html#L7), [46](projects/templates/projects/opex_site_bulk_upload.html#L46), [165](projects/templates/projects/opex_site_bulk_upload.html#L165).

### Raw hrefs (not `{% url %}`)

`projects/admin/audit_log.html` [283](projects/templates/projects/admin/audit_log.html#L283) → `/projects/{{ entry.project.project_id }}/`;
`projects/admin/projects_list.html` [11](projects/templates/projects/admin/projects_list.html#L11) → `/projects/create/`.
Plus every SCM dashboard action URL, which is f-string-built in the view rather than reversed: [views.py:1155-1167](projects/views.py#L1155-L1167) (`boq_url`, `schedule_delivery_url`, `finance_url`, `payment_request_url`, `raise_issue_url`), and PM dashboard `boq_url` at [views.py:336](projects/views.py#L336).

## 3.4 Search / recent projects / breadcrumb / listing features

| Feature | Present? | Evidence |
|---|---|---|
| **Global search** | **No.** No search box anywhere. | Grepped `projects/` for `name="q"`, `name="search"`, `request.GET.get('search'`, `icontains` — the sole `icontains` hit is `action__icontains` in the Admin audit-log keyword filter ([views.py:7806](projects/views.py#L7806)), which searches log text, not projects. |
| **"Recent projects"** | **No.** No such construct exists. | No view or template renders a recency-ordered per-user project list. |
| **Breadcrumbs** | **Yes** — single-level "← X" back-links only; no multi-level trail. | "← Projects" ([project_overview.html:96](projects/templates/projects/project_overview.html#L96)); "← Project" ([task_detail.html:17](projects/templates/projects/task_detail.html#L17), [boq_detail.html:26](projects/templates/projects/boq_detail.html#L26), [project_timeline.html:15](projects/templates/projects/project_timeline.html#L15)); "← Programs" ([program_detail.html:7](projects/templates/projects/program_detail.html#L7)); "← {{ program.name }}" ([opex_site_form.html:7](projects/templates/projects/opex_site_form.html#L7), [opex_site_bulk_upload.html:7](projects/templates/projects/opex_site_bulk_upload.html#L7)). |
| **Listing features linking to projects** | Yes — six | (1) `project_list` shim [views.py:1697](projects/views.py#L1697); (2) `admin_project_list` [views.py:7856](projects/views.py#L7856); (3) `subadmin_projects` [views.py:8440](projects/views.py#L8440) — renders a table but **contains zero links** (no `href`/`window.location` in [subadmin/projects.html](projects/templates/projects/subadmin/projects.html)); (4) `program_detail` child-site table [views.py:2160-2164](projects/views.py#L2160-L2164) → [program_detail.html:95](projects/templates/projects/program_detail.html#L95); (5) `my_documents` [views.py:7217](projects/views.py#L7217) — 6 sections all linking via `project_detail`; (6) `tasks_drill_down` [views.py:195](projects/views.py#L195) — tasks grouped by project. |
| **Notification list** | Yes — [views.py:4327](projects/views.py#L4327), rendering stored `link` values ([notifications.html:18](projects/templates/projects/notifications.html#L18)) |
| **Global nav** | [base.html:44-81](projects/templates/base.html#L44-L81) — contains **only** Programs (Admin/PM/CEO), the user dropdown (My Documents, Change Password), the notification bell, and Logout. **There is no "Home"/dashboard link in the global nav at all**, and no existing element a context switcher would naturally displace. |

## 3.5 The post-login redirect

Traced end to end:

1. **`LOGIN_REDIRECT_URL = '/dashboard/admin/'`** — [settings.py:164](solarpms/settings.py#L164). **This setting is dead.** It is only consulted by `django.contrib.auth.views.LoginView`, which is not wired: [urls.py:9](projects/urls.py#L9) routes `login/` to the project's own `views.login_view`. Nothing in the codebase reads `LOGIN_REDIRECT_URL`. (`LOGIN_URL = '/login/'` at [settings.py:163](solarpms/settings.py#L163) is likewise unused — the custom `login_required` at [decorators.py:43-50](projects/decorators.py#L43-L50) hardcodes `redirect('/login/')`.)

2. **`login_view`** — [views.py:72-90](projects/views.py#L72-L90):
   - Already authenticated on GET → `redirect(get_user_dashboard(request.user))` ([views.py:77-78](projects/views.py#L77-L78)).
   - `POST` with valid credentials → `authenticate()` → `login()` → `redirect(get_user_dashboard(user))` ([views.py:83-86](projects/views.py#L83-L86)).
   - Invalid credentials → re-render `registration/login.html` with an error ([views.py:87-88](projects/views.py#L87-L88)).
   - There is **no `?next=` handling** in `login_view`. A deep link that bounced an anonymous user to `/login/` is lost; after authenticating they always land on their role dashboard.

3. **`get_user_dashboard(user)`** — [decorators.py:27-40](projects/decorators.py#L27-L40): reads `user.profile.role`, returns `ROLE_DASHBOARD.get(role, '/dashboard/admin/')`. On any exception (no `UserProfile`) it logs a warning and returns `/dashboard/admin/`.

4. **Final rendered page**, by role:

| Role | Path | View | Template |
|---|---|---|---|
| Admin | `/dashboard/admin/` | `dashboard_admin` | `dashboard/admin.html` |
| System Admin | `/sub-admin/projects/` | `subadmin_projects` | `projects/subadmin/projects.html` |
| PM | `/dashboard/pm/` | `dashboard_pm` | `dashboard/pm.html` |
| Project Coordinator | `/dashboard/pm/` | `dashboard_pm` | `dashboard/pm.html` |
| Site Engineer | `/dashboard/site-engineer/` | `dashboard_site_engineer` | `dashboard/site-engineer.html` |
| Design | `/dashboard/design/` | `dashboard_design` | `dashboard/design.html` |
| Finance | `/dashboard/finance/` | `dashboard_finance` | `dashboard/finance.html` |
| SCM | `/dashboard/scm/` | `dashboard_scm` | `dashboard/scm.html` |
| CEO | `/dashboard/ceo/` | `dashboard_ceo` | `dashboard/ceo.html` |
| BD | `/dashboard/bd/` | `dashboard_bd` | `dashboard/bd.html` |
| *no profile / unmapped role* | `/dashboard/admin/` | `dashboard_admin` | `dashboard/admin.html` |

5. **No middleware intervenes.** `MIDDLEWARE` ([settings.py:62-72](solarpms/settings.py#L62-L72)) contains one custom entry, `solarpms.middleware.AdminAccessMiddleware`, which restricts `/admin/*` (Django admin) to staff and does not touch `/dashboard/*`.

**Consequence for the switcher:** login lands on a role dashboard with **no context signal of any kind** in the URL, session, or user record. There is exactly one funnel — `get_user_dashboard()` — so a context-aware landing decision has a single natural insertion point, but nothing currently carries the context into it.

---

# SECTION 4 — Permission layer

## 4.1 Does `user_can_manage_project(user, project)` exist?

**Yes.** [permissions.py:12-39](projects/permissions.py#L12-L39). Full body:

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

Two companions live in the same module:
- `user_can_manage_program(user, program)` — [permissions.py:42-62](projects/permissions.py#L42-L62). Derives Program authority by iterating `program.sites.filter(is_deleted=False)` and delegating each to `user_can_manage_project`. Returns `False` for an empty Program.
- `project_managers(project)` — [permissions.py:65-84](projects/permissions.py#L65-L84). Returns `[assigned_pm] + active coordinators`, deduped, PM first — the notification-fan-out list.

One thin adapter exists in views: `_pm_owns_project(request, project)` at [views.py:1665-1671](projects/views.py#L1665-L1671), which is a pure pass-through to `user_can_manage_project(request.user, project)`.

The module docstring ([permissions.py:1-9](projects/permissions.py#L1-L9)) states the intent explicitly: *"Do NOT compare `Project.assigned_pm` … or role strings directly anywhere else in the codebase."*

## 4.2 Which views use it, and which compare directly?

### Dashboards

| Dashboard | Uses `user_can_manage_project`? | What it actually does |
|---|---|---|
| `dashboard_admin` | n/a | No project query |
| `dashboard_pm` | **No — direct ORM comparison** | `Q(assigned_pm=pm_profile) \| Q(coordinators=pm_profile)` at **[views.py:283-285](projects/views.py#L283-L285)**. This is a *queryset-level* re-expression of the helper's rule. It is not a defect (the helper takes a single object and cannot be used in a filter), but it is a **second copy of the ownership rule** that must be edited in lockstep with `permissions.py`. |
| `dashboard_site_engineer` | **No — direct comparison** | `phases__tasks__assigned_to=se_profile` at **[views.py:549](projects/views.py#L549)** and in every annotation filter, **[views.py:555-566](projects/views.py#L555-L566)**. Task-assignment scoping, a different rule entirely. |
| `dashboard_design` | **No — direct comparison** | `Q(assigned_design=design_profile) \| Q(phases__tasks__assigned_to=design_profile)` at **[views.py:707-708](projects/views.py#L707-L708)**; repeated for the revisions stat at **[views.py:720-721](projects/views.py#L720-L721)**. |
| `dashboard_finance` | **No — no ownership check at all** | **[views.py:826](projects/views.py#L826)** — whole portfolio |
| `dashboard_scm` | **No — no ownership check at all** | **[views.py:967](projects/views.py#L967)** — whole portfolio |
| `dashboard_ceo` | **No — no ownership check at all** | **[views.py:1312](projects/views.py#L1312)** — whole portfolio |
| `dashboard_bd` | **No — no ownership check at all** | **[views.py:4677](projects/views.py#L4677)** — whole portfolio |
| `subadmin_projects` | **No** | All projects; PM assignment gated on `project.assigned_pm_id` truthiness at **[views.py:8453](projects/views.py#L8453)** |
| `admin_project_list` | **No** | **[views.py:7860](projects/views.py#L7860)** — `is_deleted=False` only |
| `tasks_drill_down` | **No — direct comparison** | `Q(phase__project__assigned_pm=profile) \| Q(phase__project__coordinators=profile)` at **[views.py:215-218](projects/views.py#L215-L218)** — a *third* copy of the PM-or-coordinator rule; role-string comparisons at **[views.py:211](projects/views.py#L211), [219](projects/views.py#L219), [227](projects/views.py#L227)** |

### Project views

`user_can_manage_project` (or its `_pm_owns_project` adapter) **is** used, at 40 call sites:

[views.py:1687](projects/views.py#L1687), [1772](projects/views.py#L1772), [1832](projects/views.py#L1832), [1914](projects/views.py#L1914), [1975](projects/views.py#L1975), [2003](projects/views.py#L2003), [2044](projects/views.py#L2044), [2727](projects/views.py#L2727), [2843](projects/views.py#L2843), [2867](projects/views.py#L2867), [2909](projects/views.py#L2909), [3331](projects/views.py#L3331), [3504](projects/views.py#L3504), [3588](projects/views.py#L3588), [4455](projects/views.py#L4455), [4924](projects/views.py#L4924), [4927](projects/views.py#L4927), [5528](projects/views.py#L5528), [5585](projects/views.py#L5585), [5679](projects/views.py#L5679), [5719](projects/views.py#L5719), [5812](projects/views.py#L5812), [5968](projects/views.py#L5968), [5983](projects/views.py#L5983), [6077](projects/views.py#L6077), [6175](projects/views.py#L6175), [6277](projects/views.py#L6277), [6316](projects/views.py#L6316), [6356](projects/views.py#L6356), [6492](projects/views.py#L6492), [6534](projects/views.py#L6534), [6595](projects/views.py#L6595), [6681](projects/views.py#L6681), [6968](projects/views.py#L6968).

Program views use `user_can_manage_program` via `_can_access_program` — [views.py:2098-2110](projects/views.py#L2098-L2110), called at [views.py:2156](projects/views.py#L2156), [2205](projects/views.py#L2205), [2325](projects/views.py#L2325).

### Every direct comparison — flagged with file:line

**Direct `assigned_pm` attribute access (assignment or read, not an authority decision):**
[views.py:1726](projects/views.py#L1726), [1792](projects/views.py#L1792), [2303](projects/views.py#L2303), [4787](projects/views.py#L4787), [4790-4791](projects/views.py#L4790-L4791), [5399](projects/views.py#L5399), [5412](projects/views.py#L5412), [5450](projects/views.py#L5450), [5465](projects/views.py#L5465), [7900-7901](projects/views.py#L7900-L7901), [8453](projects/views.py#L8453), [8458](projects/views.py#L8458). None of these decides authority; they set the PM, read a display name, or pick a notification recipient. Not defects, but they are the sites a context-aware ownership model would also have to consider.

**Direct role-string comparison used as an authorization gate — the significant pattern.** Approximately 20 project views are written as:

```python
if profile.role == 'PM' and not user_can_manage_project(request.user, project):
    raise Http404
```

at [views.py:5528](projects/views.py#L5528), [5585](projects/views.py#L5585), [5679](projects/views.py#L5679), [5719](projects/views.py#L5719), [5812](projects/views.py#L5812), [5983](projects/views.py#L5983), [6077](projects/views.py#L6077), [6175](projects/views.py#L6175), [6277](projects/views.py#L6277), [6316](projects/views.py#L6316), [6356](projects/views.py#L6356), [6492](projects/views.py#L6492), [6534](projects/views.py#L6534), [6595](projects/views.py#L6595), [6681](projects/views.py#L6681), [6968](projects/views.py#L6968), and — most consequentially — `project_overview` itself at [views.py:4924](projects/views.py#L4924).

The role-string prefix **short-circuits the canonical check for every non-PM role**, including `Project Coordinator`. This is an access-control defect independent of the context switcher; logged as FINDINGS_SECONDARY #5.

Other role-string gates: `project_overview` POST at [views.py:4931](projects/views.py#L4931) (`role == 'Finance'`); `_user_can_complete_checklist_item` at [views.py:1690-1692](projects/views.py#L1690-L1692) (normalises `'BD'`→`'BD / Sales'` before comparing to `task.assigned_role`); `_can_access_program` at [views.py:2105-2110](projects/views.py#L2105-L2110) (`role in ('Admin','CEO')`, plus a `created_by_id` fallback); `payment_requests` visibility at [views.py:5211](projects/views.py#L5211); `show_cascade_option` at [views.py:5206](projects/views.py#L5206); `gantt_can_view_client` at [views.py:5226](projects/views.py#L5226).

## 4.3 How do CEO, Finance and SCM get their project querysets?

**None of the three uses `user_can_manage_project`, and none uses any role-based ownership query. All three take the entire active portfolio unfiltered.** The code:

### CEO — `_get_ceo_dashboard_context()`, [views.py:1310-1318](projects/views.py#L1310-L1318)

```python
projects_qs = (
    Project.objects
    .filter(is_deleted=False, status__in=active_statuses)
    .annotate(
        has_blocked_task=Exists(blocked_subq),
        has_at_risk_task=Exists(at_risk_subq),
    )
    .order_by('target_commissioning_date', 'project_id')
)
```
`active_statuses = ['Active', 'In Progress']` ([views.py:1294](projects/views.py#L1294)). Its four sibling aggregates use the identical filter with no user term: tasks [views.py:1345-1347](projects/views.py#L1345-L1347), issues [views.py:1430-1432](projects/views.py#L1430-L1432), finance [views.py:1462-1465](projects/views.py#L1462-L1465), leaderboard [views.py:1500-1505](projects/views.py#L1500-L1505).

### Finance — `dashboard_finance`, [views.py:825-838](projects/views.py#L825-L838)

```python
projects_qs = (
    Project.objects.filter(is_deleted=False, status__in=['Active', 'In Progress'])
    .prefetch_related(
        'milestones',
        Prefetch(
            'payment_requests',
            queryset=PaymentRequest.objects.filter(status=PaymentRequest.PENDING).select_related('vendor'),
            to_attr='pending_payment_requests',
        ),
    )
    .order_by('project_id')
)
```
The rationale is stated in-code at [views.py:821-822](projects/views.py#L821-L822): *"Finance is department-level — no assigned_finance field on Project. Show all active/in-progress projects across the portfolio."* Its summary aggregates ([views.py:841-864](projects/views.py#L841-L864)) use the same unfiltered scope.

### SCM — `dashboard_scm`, [views.py:966-971](projects/views.py#L966-L971)

```python
active_projects = list(
    Project.objects.filter(is_deleted=False, status__in=['Active', 'In Progress'])
    .select_related('assigned_pm__user')
    .order_by('project_id')
)
active_project_ids = [p.project_id for p in active_projects]
```
Confirmed intentional at [views.py:1192](projects/views.py#L1192): *"SCM scope: all active projects (SCM is not PM-scoped; it sees all active projects)."* Downstream DC, BOQ, issue and vendor queries all key off `active_project_ids`.

**BD follows the same pattern** ([views.py:4676-4694](projects/views.py#L4676-L4694), rationale at [views.py:4667-4668](projects/views.py#L4667-L4668)).

**Summary answer:** it is "something else" — a **flat department-level scope**: `is_deleted=False` AND `status IN ('Active','In Progress')`, with no user, role, ownership, or type term. For the context switcher this is the most important finding in Section 4: these four dashboards are precisely where OPEX sites and Residential projects are commingled today, and there is no existing per-user scoping hook to piggyback a context filter onto — it would be a new filter term on each queryset.

---

# SECTION 5 — UserProfile state

## 5.1 Full current field list

[models.py:505-538](projects/models.py#L505-L538).

| Field | Type | Line |
|---|---|---|
| `user` | `OneToOneField(User, on_delete=CASCADE, related_name='profile')` | [521](projects/models.py#L521) |
| `role` | `CharField(max_length=20, choices=ROLE_CHOICES, blank=True)` | [522](projects/models.py#L522) |
| `phone_number` | `CharField(max_length=10, blank=True)` | [523](projects/models.py#L523) |
| `is_active` | `BooleanField(default=True)` — soft deactivation | [524](projects/models.py#L524) |
| `is_design_head` | `BooleanField(default=False)` — grants Design-task reassignment, independent of role | [525](projects/models.py#L525) |
| `email_notifications` | `BooleanField(default=True)` | [526](projects/models.py#L526) |
| `whatsapp_notifications` | `BooleanField(default=True)` | [527](projects/models.py#L527) |
| `created_at` | `DateTimeField(auto_now_add=True)` | [528](projects/models.py#L528) |
| `created_by` | `FK(User, on_delete=SET_NULL, null, blank, related_name='created_users')` | [529-535](projects/models.py#L529-L535) |

Implicit: `id` (auto PK). No `Meta` class — default ordering, default table name.

`ROLE_CHOICES` — [models.py:508-519](projects/models.py#L508-L519): `Admin`, `System Admin`, `PM`, `Project Coordinator`, `Site Engineer`, `Design`, `Finance`, `SCM`, `CEO`, `BD`. Ten values, and `role` is `blank=True` so an empty role is representable.

Reverse accessors relevant to a switcher: `pm_projects` ([models.py:64](projects/models.py#L64)), `coordinated_projects` ([models.py:83](projects/models.py#L83)), `design_projects` ([models.py:74](projects/models.py#L74)), `notifications` (used at [views.py:4334](projects/views.py#L4334)).

## 5.2 Any field storing a user preference, default view, or persisted UI state?

**Preferences: yes, two. Default view / persisted UI state: none.**

### Preference fields that exist

`email_notifications` and `whatsapp_notifications` ([models.py:526-527](projects/models.py#L526-L527)), added in [migrations/0025_userprofile_notification_prefs.py](projects/migrations/0025_userprofile_notification_prefs.py).

**The pattern they follow:**
1. **Plain nullable-free boolean column on `UserProfile`, defaulting to the permissive value** (`True`). No separate preferences model, no JSON blob, no key-value table.
2. **Read only at the notification chokepoint**, never in a view or template: [notifications.py:85](projects/notifications.py#L85) (`if not recipient.whatsapp_notifications: … 'User preference off'`) and [notifications.py:94](projects/notifications.py#L94) (email equivalent).
3. **Subordinate to a global master switch.** `SystemSettings.whatsapp_enabled` / `.email_enabled` are checked *first* ([notifications.py:61-67](projects/notifications.py#L61-L67), [82](projects/notifications.py#L82), [91](projects/notifications.py#L91)); the per-user preference can only further restrict, never enable.
4. **Every decision is logged** — a suppressed send writes a `NotificationLog` row with `status='skipped'` and a reason string ([notifications.py:83](projects/notifications.py#L83), [86](projects/notifications.py#L86), [92](projects/notifications.py#L92), [95](projects/notifications.py#L95)).
5. **Edited only by an Admin, in bulk, on a dedicated screen** — `admin_notification_prefs`, [views.py:7467](projects/views.py#L7467)/[7492](projects/views.py#L7492), at `portal-admin/notification-prefs/` ([urls.py:193](projects/urls.py#L193)). **There is no self-service preference page**: the user dropdown in [base.html:60-67](projects/templates/base.html#L60-L67) offers only My Documents and Change Password.

`is_design_head` ([models.py:525](projects/models.py#L525)) is a capability flag, not a preference — it grants an action, and is read as a permission at the `task_assign_design_head` endpoint ([urls.py:58](projects/urls.py#L58)).

### What does not exist

- No `default_view`, `default_context`, `last_context`, `preferred_dashboard`, `landing_page`, or equivalent.
- No persisted UI state of any kind — no collapsed-section memory, no sort/filter persistence, no last-visited project.
- No session-based UI state. Grep found no `request.session[...]` writes for view preferences.
- No cookie-based state. `base.html` has no `localStorage`/`sessionStorage` usage; its two inline scripts ([base.html:104-162](projects/templates/base.html#L104-L162), [228-302](projects/templates/base.html#L228-L302)) handle HTMX wiring and the username dropdown only.
- No global context processor carrying user state: `projects.context_processors.notifications` ([context_processors.py:1-14](projects/context_processors.py#L1-L14)) supplies only `unread_notification_count`.

**Bearing on the switcher:** there is a clean, single, well-understood precedent to copy (boolean column on `UserProfile` + admin-managed + logged), but **nothing today persists any user's view selection**, and no mechanism — session, cookie, or column — currently carries a UI choice across requests. A persisted context would be the first of its kind in this codebase.

## 5.3 Current migration number; unapplied migrations?

**Latest migration: `0046_alter_project_customer_name`** — [migrations/0046_alter_project_customer_name.py](projects/migrations/0046_alter_project_customer_name.py). 46 numbered migrations, `0001`–`0046`, no gaps, no branches.

The two most recent structural ones:
- `0045_project_site_code_alter_project_project_id_program_and_more` — created `Program`, added `Project.program` and `Project.site_code`, widened `Project.project_id` to 30.
- `0046_alter_project_customer_name` — widened `Project.customer_name` to 200 to match `Program.client_name`.

**Unapplied migrations: none.** `manage.py showmigrations projects` returns `[X]` for all 46, `0001` through `0046`, against the local database.

**Model/migration drift: none.** `manage.py makemigrations --check --dry-run` returned `No changes detected` with exit code 0 — no write occurred; `--check --dry-run` only reports.

**UNKNOWN:** the applied-migration state on **Railway production**. Establishing it requires either `showmigrations` executed against the Railway `DATABASE_URL` or a read of `django_migrations` on that database — neither was done in this session.

---

# UNKNOWNs — consolidated

| # | Question | What would resolve it |
|---|---|---|
| 1 | Railway production row counts by `project_type` and `program_type` (§1.4) | A read-only query against the Railway `DATABASE_URL` — e.g. `railway run python manage.py shell` with the same counting script, or `railway connect Postgres` + a `SELECT project_type, COUNT(*) FROM projects_project GROUP BY 1`. |
| 2 | Whether migrations `0045`/`0046` (the entire Program foundation) are applied on Railway (§5.3) | `showmigrations projects` against the Railway database, or `SELECT name FROM django_migrations WHERE app='projects' ORDER BY id DESC LIMIT 5`. |
| 3 | Whether any CAPEX `Program` or `Project` exists on Railway (§1.4) | Same query as #1. Locally both are zero, so **every CAPEX code path in the audit is unexercised** — an assumption the switcher design should not silently inherit. |
| 4 | The registered Interakt template variable order for each WhatsApp template that carries a project URL (§3.2) | The Interakt console. The code comment at [views.py:5471-5475](projects/views.py#L5471-L5475) and [notifications.py:138-141](projects/notifications.py#L138-L141) states the order is authoritative there, not in the code — it cannot be determined from this repository. |
| 5 | Whether `settings.APP_BASE_URL` is actually set in the Railway environment, or the hardcoded default is in force (§3.2) | `railway variables` / the Railway dashboard. `.env` locally does not define it — [send_eod_digest.py:92-93](projects/management/commands/send_eod_digest.py#L92-L93) falls back to the hardcoded host. |

---

# Findings most relevant to the context switcher

Stated as observations, not proposals — per the task's "report findings only" constraint.

1. **Type discrimination is clean and single-sourced.** `Project.project_type`, one field, three Title-case values, one write path per creation route. §1.3 rules out every apparent alternative. A switcher has one thing to key off.

2. **Not one dashboard filters by project type** (§2.2). The 7 live OPEX sites and 7 live Residential projects are commingled on the Finance, SCM, CEO, BD dashboards and on `admin_project_list`. There is no existing type filter to extend — 10 querysets would each need a new term.

3. **The Finance/SCM/CEO/BD querysets have no per-user scoping hook at all** (§4.3) — they are flat `is_deleted=False AND status IN ('Active','In Progress')`. Unlike the PM/SE/Design dashboards, there is no existing user-dependent clause a context term could ride along with.

4. **`get_user_dashboard()` is the single post-login funnel** (§3.5) and nothing carries a context into it. `LOGIN_REDIRECT_URL` is dead code; `login_view` ignores `?next=`.

5. **`UserProfile` has no persisted-UI-state precedent** (§5.2), only two notification-preference booleans that are admin-managed and never self-served. A persisted context selection would be the codebase's first.

6. **Project entry points are numerous but converge.** Effectively all project traffic reaches `project_overview` — either directly or via the `project_detail` redirect shim ([views.py:1746](projects/views.py#L1746)) — and all Program traffic reaches `program_detail`. Two convergence points, not dozens.

7. **The `project_list` breadcrumb is role-restricted to PM/Admin/CEO** ([views.py:1696](projects/views.py#L1696)) while "← Projects" is rendered on pages every role can reach ([project_overview.html:96](projects/templates/projects/project_overview.html#L96)) — six roles currently get bounced. Any context-aware listing inherits this.

8. **`base.html` global nav has no dashboard/home link at all** (§3.4) — only Programs (Admin/PM/CEO), the user dropdown, notifications, logout. There is no existing element a switcher would displace, and no per-page context indicator exists today.

9. **Three independent absolute-URL host strategies** feed outbound notifications (§3.2) — hardcoded literal, `request.build_absolute_uri()`, and `settings.APP_BASE_URL`. Any context-scoped URL scheme would need applying at all three.

10. **Two divergent role→dashboard maps exist** — `decorators.ROLE_DASHBOARD` (paths, 10 roles) and `views._ROLE_DASHBOARD` (URL names, 7 roles, one broken key). See FINDINGS_SECONDARY #2.

---

*Unrelated bugs noticed during this investigation are logged in `FINDINGS_SECONDARY.md`. None was fixed.*