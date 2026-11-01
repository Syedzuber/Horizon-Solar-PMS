# CAPEX_AUDIT — what it takes for CAPEX to run the RESCO (stored 'OPEX') template

- **Date:** 2026-11-01
- **Mode:** audit only, read-only. Repo writes: this file, plus an append to `SECONDARY_FINDINGS.md`. No commit.
- **HEAD:** `22ff004` on `main`. The tracked tree was clean; 15 untracked files (`.env1` and 14 `*.dump` files) were present and left alone.
- **Database:** `solarpms_local` on `localhost`, proved by query (§0). Every SQL statement in this file ran inside `BEGIN; SET TRANSACTION READ ONLY; SET default_transaction_read_only = on; … ROLLBACK`.
- **Vocabulary:** `Project.PROJECT_TYPE_CHOICES` (`projects/models.py`, class `Project`) stores `'Residential'`, `'OPEX'` (label `'RESCO'`) and `'CAPEX'`. The literal `'RESIDENTIAL'` is a `TaskTemplate.code` (`utils.RESIDENTIAL_TEMPLATE_CODE`), never a project type. So the table B search covers `'Residential'` as well as `'RESIDENTIAL'`.

```python
# projects/models.py, class Project
PROJECT_TYPE_CHOICES = [
    ('Residential', 'Residential'),
    ('OPEX',        'RESCO'),
    ('CAPEX',       'CAPEX'),
]
```

---

## 0. Pre-flight

```
$ git status --short && git branch --show-current && git log -1 --oneline
?? .env1
?? horizon_prod_2026-10-07.dump        (… 13 more *.dump files, all untracked)
main
22ff004 Pin design_assign_qc as it behaves today
```

Database identity, queried through Django's own connection:

```
SET default_transaction_read_only = on
SELECT current_database(), now(), inet_server_addr(), inet_server_port()
  -> ('solarpms_local', 2026-11-01 05:16:08.278918+00:00, '::1', 5432)
SELECT max(last_login) FROM auth_user            -> 2026-10-31 08:52:53.615559+00:00
SELECT username, last_login FROM auth_user WHERE last_login IS NOT NULL ORDER BY last_login DESC LIMIT 1
                                                 -> ('admin', 2026-10-31 08:52:53.615559+00:00)
settings.DATABASES['default'] HOST/NAME          -> localhost / solarpms_local
```

Test suite:

```
$ ./venv/Scripts/python.exe manage.py test projects --settings=solarpms.test_settings
FAIL: test_02_a_second_pending_request_is_refused_by_the_database (projects.tests_design_part46.RaisingTests...)
AssertionError: 'uniq_pending_change_request_per_attempt' not found in 'UNIQUE constraint failed: projects_designchangerequest.attempt_id'
Ran 5524 tests in 654.530s
FAILED (failures=1, skipped=23, expected failures=1)
```

This matches the baseline (failures=1 in `tests_design_part46`, errors=0). Zuber said "proceed".

---

## A. Template storage and activation

### A.1 Source of truth

The template is **data in three tables**, not Python: `TaskTemplate` → `TaskTemplatePhase` → `TaskTemplateTask` (`projects/models.py`). The OPEX rows are written once, by **migration `0075_seed_opex_template_v1`**, function `seed_opex_v1()`. That function feeds `build_opex_phases()` (a literal inside the migration) into `utils.seed_task_template_version()`.

```python
# projects/migrations/0075_seed_opex_template_v1.py
OPEX_TEMPLATE_CODE  = 'OPEX'
OPEX_TEMPLATE_LABEL = 'OPEX Execution'
...
    template = seed_task_template_version(
        ...
        code=OPEX_TEMPLATE_CODE,
        label=OPEX_TEMPLATE_LABEL,
        project_type='OPEX',            # Project.project_type vocabulary
        version_no=1,
        phases=build_opex_phases(),
        duration_resolver=lambda name: OPEX_DEFAULT_DURATION_DAYS,
```

Phase and task `code`s are not authored by hand. `utils.seed_task_template_version()` derives them from labels with `template_code_from_label()`:

```python
# projects/utils.py, seed_task_template_version()
'code': template_code_from_label(phase_data['phase_name'], 50),
...
'code': template_code_from_label(t['task_name'], 100),
# projects/utils.py, template_code_from_label()
return re.sub(r'[^A-Za-z0-9]+', '_', label).strip('_').upper()[:max_length]
```

There is no runtime OPEX builder and no bootstrap. `utils.attach_opex_template()` says so in its docstring: "NO BOOTSTRAP … if no active OPEX template exists this RAISES". The connected DB holds exactly two templates (SQL F11, §F):

```
id | code        | label           | project_type | version_no | status
6  | OPEX        | OPEX Execution  | OPEX         | 1          | active
1  | RESIDENTIAL | Residential EPC | Residential  | 1          | active
```

There is no CAPEX template.

### A.2 How activation picks a template

`TaskTemplate.project_type` is a single column. Resolution filters on it:

```python
# projects/utils.py
def resolve_active_task_template(project_type):
    ...
    return (
        TaskTemplate.objects
        .filter(project_type=project_type, status=TaskTemplate.ACTIVE)
        .prefetch_related('phases__tasks')
        .first()
    )
```

Two activation views exist (`projects/urls.py`):

```python
path('projects/<str:project_id>/activate/',      views.project_activate,   name='project_activate'),   # ... RESIDENTIAL ...
path('projects/<str:project_id>/activate-site/', views.opex_site_activate, name='opex_site_activate'), # ... OPEX/CAPEX ...
```

Both buttons are branched on type: `project_overview.html` (Draft Activate) and `dashboard/pm.html` (Draft strip). Residential gets the designer modal (→ `project_activate`). **Every other type, CAPEX included, POSTs to `opex_site_activate`.**

```django
{# projects/templates/projects/project_overview.html, block content #}
{% if project.project_type == 'Residential' %}
  <button ... data-activate-url="{% url 'project_activate' project.project_id %}">Activate</button>
{% else %}
  <form method="post" action="{% url 'opex_site_activate' project.project_id %}" ...>
```

```python
# projects/views.py, opex_site_activate()
    if project.project_type == 'Residential':
        messages.error(request, 'Residential projects are activated from the Activate button.')
        return redirect('project_overview', project_id=project.project_id)
    ...
    with transaction.atomic():
        _prev_project_status = project.status   # 'Draft' — checked above
        project.status = 'Active'
        project.activated_at = timezone.now()
        project.save()
        record_transition(...)
        attach_opex_template(project)
```

```python
# projects/utils.py, attach_opex_template()
    with transaction.atomic():
        template = resolve_active_task_template(project.project_type)
        if template is None:
            raise TaskTemplate.DoesNotExist(
                f"No ACTIVE task template for project_type "
                f"'{project.project_type}'. Seed and activate one before activating a "
                ...
```

`opex_site_activate()` has no `try/except` around `attach_opex_template()` (the quoted block is the whole body between the status check and `log_activity`).

### A.3 What activating a CAPEX project does today

1. **Through the UI (the only rendered button):** POST to `opex_site_activate`. The Residential refusal passes, `project.save()` runs inside the atomic block, then `attach_opex_template()` calls `resolve_active_task_template('CAPEX')`. That returns `None` (F11 has no CAPEX row), so `TaskTemplate.DoesNotExist` is raised. The atomic block rolls back, so the project stays Draft. The exception is not caught in the view, so the request ends as an unhandled exception (HTTP 500 under `DEBUG=False`). **Blocked, with a server error.** Derived from the code path quoted above; not executed in this session because activation writes. The test fixture docstring `tests_opex_manual_dates::ManualDatesFixture::_activated_capex_site` records the same raise.
2. **Through `project_activate` (POSTed directly; no rendered template points a CAPEX project at it):** it needs a designer, sets Active, and attaches a template **only** `if project.project_type == 'Residential'`. It still creates M1/M2/M3 `PaymentMilestone` rows unconditionally, and its success message for non-Residential is `'Project activated. Add tasks manually using Add Task.'` → **zero tasks**. The single CAPEX project in the local DB is in exactly this state (§F: `HRP-CAP-2026-001`, Active, 0 phases, 0 tasks, `assigned_design_id=17`, 6 M-milestones).

The only template that references `project_activate` without branching is `project_detail.html`, which no view renders: `views.project_detail()` is `return redirect('project_overview', project_id=project_id)`.

### A.4 Uniqueness of codes (quoted)

```python
# projects/models.py, TaskTemplate.Meta
constraints = [
    models.UniqueConstraint(fields=['code', 'version_no'], name='uniq_task_template_code_version'),
    models.UniqueConstraint(fields=['code'], condition=models.Q(status='active'),
                            name='uniq_active_task_template_per_code'),
]
# projects/models.py, TaskTemplatePhase.Meta
models.UniqueConstraint(fields=['template', 'code'], name='uniq_task_template_phase_code')
# projects/models.py, TaskTemplateTask.Meta
models.UniqueConstraint(fields=['phase', 'code'], name='uniq_task_template_task_code')
```

- **`TaskTemplateTask.code` is unique per phase**, not per template and not globally. Two phases of one template may carry the same task code, and different templates share codes freely. The live DB already has three codes shared by OPEX and RESIDENTIAL (SQL D1, §D): `DESIGN`, `INVERTER_INSTALLATION`, `MODULE_INSTALLATION`.
- **`TaskTemplate.code` is unique per version, and at most one active version per code.** A second template cannot be an active `code='OPEX'` alongside OPEX v1.
- **No constraint limits active templates per `project_type`.** If two active templates share a `project_type`, `resolve_active_task_template()` returns `.first()` under `Meta.ordering = ['code', '-version_no']`, so the alphabetically-first code wins without any error.
- `TaskTemplateTask` has **no two-step field and no checklist field**. Its per-task flags are `is_payment_milestone` and `is_mirror` only (quoted model). Two-step applicability is computed from `project.project_type == 'OPEX'` at runtime (table B rows 47, 52, 55, 57).

---

## B. Every project-type branch

### B.0 Method

Scope: every `.py`, `.html` and `.txt` under `projects/`, excluding `migrations/`, `static/`, `fonts/`, `__pycache__/` and every `tests*` file (tests are §G). The search regex, verbatim:

```
'OPEX'|"OPEX"|'CAPEX'|"CAPEX"|'Residential'|"Residential"|'RESIDENTIAL'|"RESIDENTIAL"|project_type|program_type|is_opex|is_capex|is_residential|RESCO|_TENDER_TYPES|CONTEXT_PROJECT_TYPES|REORDERABLE_PROJECT_TYPES|CEO_TENDER_TYPES|RECORD_TYPES|payment_project_type|_opex_site\b|attach_opex_template|attach_residential_template|COD_OPEX_ONLY|STATE_NOT_OPEX
```

The helper names after `RESCO` came from a pass for wrappers:

```
grep -rnE "^def [a-z_]*(opex|capex|residential|project_type|resco)[a-z_]*|def (is|has)_[a-z_]*(opex|capex|residential)|^[A-Z_]*(OPEX|CAPEX|RESIDENTIAL|TYPES)[A-Z_]* *=" projects --include=*.py   (non-test, non-migration)
```

`is_opex`, `is_capex` and `is_residential` match **zero** lines; no such helper exists. Each line was classified by a script (Python `ast` for the enclosing function, `tokenize` for comment vs code; templates by nearest `{% block %}` and `{# #}` / `{% comment %}`):

```
total 649   code 486   comment 79   docstring 84
```

- Every **code** line is one row of the table in B.3 (486 rows, numbered).
- Comment-only and docstring-only lines (163) are listed raw in Appendix 1 so no hit is dropped.
- Derived flags were followed to their use sites inside the row text (`two_step`, `eligible`, `show_design_selector`, `gantt_available`, `tenders`, `residential`, …).
- Cross-check: the classified table carries exactly the same set of (file, line) pairs as the raw hit list (486 = 486, 0 missing, 0 extra), and every row has 8 cells.
- Three random rows were re-read against source: 137 (`design_change_request`), 251 (`ProgramForm.clean`), 333 (`resolve_checklists_for_tasks`); all three match.

The classification of the 486 rows was split across four parallel read-only passes, then merged and validated by the script above.

Per-file code-hit counts (from the script):

```
98 views.py · 65 design_views.py · 36 models.py · 26 forms.py · 21 purchases_views.py · 21 utils.py
15 qaqc_views.py · 14 admin.py · 13 payments.py · 11 order_views.py · 11 mgmt/seed_opex_test_data.py
10 payment_views.py · 9 permissions.py · 6 cod_views.py · 6 phase_rules.py · 6 mgmt/seed_scm_pilot.py
6 program_form.html · 6 project_form.html · 6 subadmin/projects.html · 5 bill_rules.py
5 mgmt/seed_order_demo.py · 5 mgmt/seed_scm_handoff_data.py · 5 mgmt/seed_walkthrough.py
5 admin/boq_items.html · 5 design/boq_catalogue.html · 5 project_overview.html · 5 purchases_new.html
4 mgmt/oneoff_ukru001_link_location_tasks.py · 4 program_detail.html · 3 mgmt/fixture_scmpilot_boq.py
3 site_drawings_views.py · 2 each: approval_views.py, mgmt/seed_opex_installation_checklists.py,
mgmt/wipe_demo_2026_10_14.py, admin/checklist_edit.html, admin/user_edit.html, design/quality_analytics.html,
opex_boq_entry.html, opex_boq_upload.html, partials/vendor_order_picker_script.html,
reports/ceo_daily_report.html, vendor_order_detail.html
1 each: approval_forms.py, punch_points.py, mgmt/design_figure_snapshot.py, mgmt/flag_test_projects.py,
mgmt/revert_design_allocation_31oct.py, dashboard/ceo.html, dashboard/design.html, dashboard/pm.html,
admin/checklists.html, admin/pm_handover.html, admin/projects_list.html, boq_correct.html,
design/boq_catalogue_form.html, design/qc_review.html, email/eod_digest.html, email/eod_digest.txt,
opex_site_form.html, partials/_task_row_submit_button.html, partials/vendor_order_picker.html,
program_list.html, project_detail.html, project_list.html, purchases_workspace.html, subadmin/departments.html
```

### B.1 Counts

| Category | Rows |
|---|---|
| OTHER (activation, creation, seed/demo commands, forms, ID generation) | 130 |
| SCM | 70 |
| DASHBOARD/UI | 61 |
| BOQ | 58 |
| DESIGN | 51 |
| PROGRAM | 45 |
| CLOSEOUT | 22 |
| APPROVAL | 21 |
| TASK | 15 |
| PHASE | 10 |
| NOTIFY | 2 |
| MIRROR | 1 |
| ATTENDANCE | 0 |
| **Total** | **486** |

| CAPEX today | Rows |
|---|---|
| excluded | 176 |
| n/a-display | 164 |
| included | 80 |
| n/a-data | 63 |
| n/a (generic helper) | 3 |

ATTENDANCE has 0 rows because no attendance module line matches the regex. ATT-B4's "CAPEX never qualifies" is a consequence of CAPEX having no template (no `INSTALLATION` phase), not of a type test (`SECONDARY_FINDINGS.md`, ATT-B4 section).

MIRROR has 1 row because the mirror derivations do **not** test project type; they look rows up by template code (§D). The one row is the log-branch test in `sync_delivery_mirrors` (row 102).

### B.2 Conditions that already include CAPEX (flagged)

Flag key:
- `NEQ-RES`: written as `!= 'Residential'`, or returns False only for Residential.
- `IN-INCL-CAPEX`: membership in a collection containing `'CAPEX'`.
- `ELSE-OF-RES`: `== 'Residential'` whose else-branch is the non-Residential behaviour, so CAPEX lands there.
- `ELSE-OF-OPEX`: `== 'OPEX'` whose else-branch is Residential behaviour, so CAPEX gets Residential behaviour.

**`!= 'Residential'`-style and `in (...)`-with-CAPEX hits: 23 rows** (NEQ-RES 5 + IN-INCL-CAPEX 14 + ELSE-OF-RES 4).

| Flag | Row | Location | Effect on CAPEX |
|---|---|---|---|
| NEQ-RES | 34 | `views.project_recalculate_dates` | CAPEX refused auto date recalculation, same as OPEX |
| NEQ-RES | 35 | `views.enable_cascade_scheduling` | CAPEX refused cascade scheduling, same as OPEX |
| NEQ-RES | 71 | `views.milestone_create` | CAPEX refused M1/M2/M3 creation (but `project_activate` mints them; A.3) |
| NEQ-RES | 293 | `permissions._project_allows_location_duplication` | **duplicate-for-locations already allowed on CAPEX** |
| NEQ-RES | 319 | `purchases_views._parse_record_submission` | `unfrozen` stored for non-Residential orders |
| IN-INCL-CAPEX | 2, 4–12 | `views.CONTEXT_PROJECT_TYPES`, `_context_filter`, `_context_types`, `_context_includes`, `_TENDER_TYPES`, `_apply_project_sections` | Tenders context / section on PM, Design, SCM, Finance dashboards includes CAPEX |
| IN-INCL-CAPEX | 24 | `views._get_ceo_dashboard_context` | `tender_design_subq` counts CAPEX DesignAssignments (CAPEX can have none; §C.3) |
| IN-INCL-CAPEX | 266 | `payment_views.QUEUE_TABS` | payment queue has a CAPEX tab |
| IN-INCL-CAPEX | 294, 295 | `permissions.REORDERABLE_PROJECT_TYPES`, `_project_allows_phase_reorder` | **task reorder already allowed on CAPEX** |
| ELSE-OF-RES | 25 | `views._get_ceo_dashboard_context` | CEO card design-done for CAPEX reads DesignAssignment |
| ELSE-OF-RES | 32 | `views.opex_site_activate` | **CAPEX passes the guard → `attach_opex_template` → raises (A.3)** |
| ELSE-OF-RES | 412 | `dashboard/pm.html` Draft strip | CAPEX Activate posts to `opex_site_activate` |
| ELSE-OF-RES | 465 | `project_overview.html` Activate | CAPEX Activate posts to `opex_site_activate` |
| ELSE-OF-OPEX | 59 | `views.boq_detail` | CAPEX BOQ stays on the Residential BOQ screen / catalogue |
| ELSE-OF-OPEX | 73 | `views.project_overview` `show_design_selector` | CAPEX gets the Residential `Project.assigned_design` selector |
| ELSE-OF-OPEX | 177, 179 | `Program.__str__`, `Program.reference_display` | display only |
| ELSE-OF-OPEX | 215, 216, 218, 219, 221 | `approval_views._bill_now`, `_task_groups`, `bill_rules.task_complete_for_bill`, `incomplete_task_warnings`, `task_status_label` | contractor bills treat CAPEX tasks as complete on Done alone |
| ELSE-OF-OPEX | 475 | `purchases_workspace.html` | prints raw `CAPEX` label |

**The four TASK features named in the goal:**

| Feature | Gate | CAPEX today | Evidence |
|---|---|---|---|
| Not Applicable | `permissions.user_can_mark_task_not_applicable` | **included** | no type test; docstring: "No project-type gate — this applies to Residential, OPEX and CAPEX alike" |
| Duplicate for locations | `permissions._project_allows_location_duplication` | **included** | row 293 |
| Rename | `permissions._project_allows_task_rename` | **included** | no type test; docstring: "Any project type, Residential included" |
| Reorder | `permissions._project_allows_phase_reorder` | **included** | rows 294–295 |

All four already admit CAPEX. None of them is reachable on a CAPEX project today only because a CAPEX project cannot obtain tasks (A.3).

### B.3 The table (486 rows)

Columns: # · file · function/block · line · exact condition · what it gates · CAPEX today · category · flag.

| # | file | function/block | line | exact condition | what it gates | CAPEX today | category | flag |
|---|---|---|---|---|---|---|---|---|
| 1 | projects/views.py | <module> (import) | 196 | `attach_residential_template, attach_opex_template,` | import of the two template-attach functions used by project_activate (3921) and opex_site_activate (4040) | n/a-data | OTHER | |
| 2 | projects/views.py | <module> CONTEXT_PROJECT_TYPES | 321 | `CONTEXT_PROJECT_TYPES = {` | the ?context= dashboard filter map read by _context_filter/_context_types/_context_includes/_TENDER_TYPES/_ceo_context_filter (CEO, Finance, SCM, PM, Design dashboards) | included (see 323) | DASHBOARD/UI | IN-INCL-CAPEX |
| 3 | projects/views.py | <module> CONTEXT_PROJECT_TYPES | 322 | `CONTEXT_RESIDENTIAL: ['Residential'],` | Residential context = Residential only | n/a-display | DASHBOARD/UI | |
| 4 | projects/views.py | <module> CONTEXT_PROJECT_TYPES | 323 | `CONTEXT_TENDERS: ['OPEX', 'CAPEX'],` | Tenders context includes CAPEX (comment above: "included so those sites appear automatically") | included | DASHBOARD/UI | IN-INCL-CAPEX |
| 5 | projects/views.py | _context_filter | 353 | `types = CONTEXT_PROJECT_TYPES.get(context)` | filter kwargs added to dashboard querysets under ?context= | included (tenders) | DASHBOARD/UI | IN-INCL-CAPEX |
| 6 | projects/views.py | _context_filter | 356 | `return {f'{prefix}project_type__in': types}` | the project_type__in filter itself (cont. of 353) | included (tenders) | DASHBOARD/UI | IN-INCL-CAPEX |
| 7 | projects/views.py | _context_types | 363 | `return CONTEXT_PROJECT_TYPES.get(context) or None` | type list passed to payments.payment_counts() for dashboard payment tiles | included (tenders) | DASHBOARD/UI | IN-INCL-CAPEX |
| 8 | projects/views.py | _context_includes | 366 | `def _context_includes(context, project_type):` | helper: does context show this type; only call site is dashboard_scm 2055 with 'OPEX' | n/a (generic helper) | DASHBOARD/UI | IN-INCL-CAPEX |
| 9 | projects/views.py | _context_includes | 375 | `return project_type in CONTEXT_PROJECT_TYPES.get(context, ())` | membership test (cont. of 366) | n/a (generic helper) | DASHBOARD/UI | IN-INCL-CAPEX |
| 10 | projects/views.py | <module> _TENDER_TYPES | 413 | `_TENDER_TYPES = tuple(CONTEXT_PROJECT_TYPES[CONTEXT_TENDERS])   # ('OPEX', 'CAPEX')` | section partition on PM/Design/SCM dashboards (_apply_project_sections) | included | DASHBOARD/UI | IN-INCL-CAPEX |
| 11 | projects/views.py | _apply_project_sections | 439 | `r['project'].project_type in _TENDER_TYPES` | rows placed under the "Tenders" section header | included (Tenders section) | DASHBOARD/UI | IN-INCL-CAPEX |
| 12 | projects/views.py | _apply_project_sections | 440 | `r['project'].project_type not in _TENDER_TYPES` | rows placed under "EPC Residential" section | excluded (CAPEX goes to Tenders) | DASHBOARD/UI | IN-INCL-CAPEX |
| 13 | projects/views.py | dashboard_design | 1456 | `Q(status__in=['Active', 'In Progress']) \| Q(project_type='OPEX')` | Design dashboard projects_qs: Draft exemption so Draft OPEX sites under design are visible; Draft CAPEX projects are hidden | excluded (Draft CAPEX hidden; Active/In Progress CAPEX shown) | DESIGN | |
| 14 | projects/views.py | dashboard_design | 1473 | `Q(project__status__in=['Active', 'In Progress']) \| Q(project__project_type='OPEX')` | Design dashboard total_revisions count (same Draft exemption) | excluded (Draft CAPEX not counted) | DESIGN | |
| 15 | projects/views.py | dashboard_scm | 2055 | `scm_opex_tender_rows() if _context_includes(ctx, 'OPEX') else []` | SCM dashboard "OPEX tenders" section (SiteGroup/DesignAssignment rows), built only under a context that includes OPEX | excluded (section is OPEX tender rows) | SCM | |
| 16 | projects/views.py | _phase_progress_subqueries | 2178 | `project__project_type='Residential',` | positional phase-progress subquery (BOQ / Delivery progress columns); NULL for non-Residential = row hidden | excluded | DASHBOARD/UI | |
| 17 | projects/views.py | <module> CEO_TENDER_TYPES | 2265 | `CEO_TENDER_TYPES = ['OPEX']` | CEO dashboard Tenders = RESCO only (decision D2); read by _ceo_context_filter, payment_counts call 2973, ceo_payment_strip 3167 | excluded | DASHBOARD/UI | |
| 18 | projects/views.py | _ceo_context_filter | 2286 | `types = CEO_TENDER_TYPES` | CEO dashboard Tenders context filter | excluded | DASHBOARD/UI | |
| 19 | projects/views.py | _ceo_context_filter | 2288 | `types = CONTEXT_PROJECT_TYPES.get(context)` | CEO non-tenders contexts (residential) | n/a (residential context); no-context keeps CAPEX | DASHBOARD/UI | |
| 20 | projects/views.py | _ceo_context_filter | 2291 | `return {f'{prefix}project_type__in': types, f'{prefix}is_test': False}` | the CEO filter kwargs (cont. of 2286/2288) | excluded under Tenders | DASHBOARD/UI | |
| 21 | projects/views.py | tender_sites_qs | 2341 | `project_type='OPEX',` | THE definition of "live tender site" for CEO Tenders header/cards/stages | excluded | DASHBOARD/UI | |
| 22 | projects/views.py | _capex_hidden_count | 2389 | `project_type='CAPEX', is_deleted=False, is_test=False,` | CEO Tenders "Not shown: CAPEX" count line | included (counted as hidden) | DASHBOARD/UI | |
| 23 | projects/views.py | _get_ceo_dashboard_context | 2660 | `phase__project__project_type='Residential',` | residential_design_status_subq (design pill from template position) | excluded | DASHBOARD/UI | |
| 24 | projects/views.py | _get_ceo_dashboard_context | 2669 | `project__project_type__in=['OPEX', 'CAPEX'],` | tender_design_subq: design "submitted" via DesignAssignment status | included | DASHBOARD/UI | IN-INCL-CAPEX |
| 25 | projects/views.py | _get_ceo_dashboard_context | 2807 | `if p.project_type == 'Residential': ... else: design_done = p.tender_design_submitted` | CEO project card design_done; else branch = DesignAssignment source | included (else branch) | DASHBOARD/UI | ELSE-OF-RES |
| 26 | projects/views.py | _get_ceo_dashboard_context | 2973 | `CEO_TENDER_TYPES if context == CONTEXT_TENDERS else _context_types(context)` | CEO finance tiles (payment_counts APPROVED) | excluded under Tenders; included with no context | DASHBOARD/UI | |
| 27 | projects/views.py | _get_ceo_dashboard_context | 3167 | `ceo_payment_strip(CEO_TENDER_TYPES, today))` | CEO Tenders payment strip | excluded | DASHBOARD/UI | |
| 28 | projects/views.py | project_create | 3699 | `ProjectCreateForm(initial={'project_type': 'Residential'})` | default value of the type field on manual create form | n/a-display | OTHER | |
| 29 | projects/views.py | project_activate | 3920 | `if project.project_type == 'Residential':` | activation: attaches Residential template only for Residential; CAPEX through this view = Active, designer required, ZERO tasks, M1/M2/M3 milestones still created | excluded (activated with no template) | OTHER | |
| 30 | projects/views.py | project_activate | 3921 | `attach_residential_template(project)` | the attach call (cont. of 3920) | excluded | OTHER | |
| 31 | projects/views.py | project_activate | 3939 | `if project.project_type == 'Residential':` | success message; else "Project activated. Add tasks manually using Add Task." | n/a-display (CAPEX gets "add tasks manually") | OTHER | |
| 32 | projects/views.py | opex_site_activate | 4015 | `if project.project_type == 'Residential': messages.error(...); return redirect` | activation: only Residential refused; any other type proceeds to attach_opex_template | included (passes guard) | OTHER | ELSE-OF-RES |
| 33 | projects/views.py | opex_site_activate | 4040 | `attach_opex_template(project)` | activation: resolves template by project.project_type ('CAPEX'), raises TaskTemplate.DoesNotExist inside the atomic block if no active CAPEX template (utils.attach_opex_template) | included but raises without an active CAPEX template | OTHER | |
| 34 | projects/views.py | project_recalculate_dates | 4094 | `if project.project_type != 'Residential':` | refuses automatic due-date recalculation ("not available for OPEX/CAPEX sites") | included in refusal (same as OPEX) | OTHER | NEQ-RES |
| 35 | projects/views.py | enable_cascade_scheduling | 4145 | `if project.project_type != 'Residential':` | refuses cascade scheduling | included in refusal (same as OPEX) | OTHER | NEQ-RES |
| 36 | projects/views.py | _rename_plan | 4690 | `ChecklistTaskLink.objects.filter(project_type=project.project_type, task_name__iexact=new_name)` | task rename: refuse renaming onto a name a legacy name-keyed checklist link uses for this type | included (type-generic) | TASK | |
| 37 | projects/views.py | program_create | 5031 | `f"Created {program.program_type} Program: {program.name}"` | activity log text | n/a-display | PROGRAM | |
| 38 | projects/views.py | create_opex_site | 5104 | `def create_opex_site(program, data, creator, profile=None):` | sole creation core for sites under a Program (single + bulk + dry-run) | excluded (only creates OPEX) | PROGRAM | |
| 39 | projects/views.py | create_opex_site | 5138 | `site.project_type = 'OPEX'          # forced` | every site created under a Program is OPEX | excluded | PROGRAM | |
| 40 | projects/views.py | create_opex_site | 5162 | `f"Created RESCO site {site.project_id} under program {program.name}"` | activity log text | n/a-display | PROGRAM | |
| 41 | projects/views.py | opex_site_create | 5177 | `get_object_or_404(Program, pk=pk, is_deleted=False, program_type='OPEX')` | add-site screen 404s for a CAPEX Program | excluded | PROGRAM | |
| 42 | projects/views.py | opex_site_create | 5183 | `create_opex_site(program, request.POST, ...)` | call (cont. of 5177) | excluded | PROGRAM | |
| 43 | projects/views.py | _validate_site_row_dry_run | 5285 | `create_opex_site(program, data, creator, profile=profile)` | bulk-upload preview validation (rolled back) | excluded | PROGRAM | |
| 44 | projects/views.py | opex_site_bulk_upload | 5390 | `get_object_or_404(Program, ..., program_type='OPEX')` | bulk site upload 404s for a CAPEX Program | excluded | PROGRAM | |
| 45 | projects/views.py | opex_site_bulk_upload | 5480 | `create_opex_site(program, data, request.user, profile=profile)` | bulk commit (cont. of 5390) | excluded | PROGRAM | |
| 46 | projects/views.py | opex_site_bulk_template | 5526 | `get_object_or_404(Program, ..., program_type='OPEX')` | bulk template download 404s for a CAPEX Program | excluded | PROGRAM | |
| 47 | projects/views.py | _phase_list_two_step | 5658 | `two_step = project.project_type == 'OPEX'` | context `two_step_completion` → _task_row.html:229 (Done option replaced) and _task_row_approval.html:56 (Submit/Approve buttons); self-certify answers | excluded | APPROVAL | |
| 48 | projects/views.py | _attach_open_punch_points | 5830 | `counts = ({} if project.project_type != 'OPEX' else open_punch_point_counts_by_task(...))` | open punch-point badge on task rows | excluded | CLOSEOUT | |
| 49 | projects/views.py | _attach_phase_rule_flags | 5858 | `if project.project_type != 'OPEX' or not tasks: return` | started-early / phase-rule preview flags on task rows | excluded | PHASE | |
| 50 | projects/views.py | _phase_rule_card_context | 5924 | `if project.project_type != 'OPEX':` | "Started early" card on project overview | excluded | PHASE | |
| 51 | projects/views.py | _phase_rule_task_context | 5999 | `if project.project_type != 'OPEX':` | phase-rule overrides/preview on task detail | excluded | PHASE | |
| 52 | projects/views.py | _task_approval_context | 6030 | `eligible = project.project_type == 'OPEX' and not task.is_mirror` | `show_approval_panel` + can_submit/approve on task detail | excluded | APPROVAL | |
| 53 | projects/views.py | _checklist_task_link_for | 6265 | `.filter(template_task__code=code, template_task__phase__template__project_type=project.project_type)` | checklist attached to a task: by template-task code within templates of the project's type | included (type-generic; finds links on CAPEX templates only) | OTHER | |
| 54 | projects/views.py | _checklist_task_link_for | 6276 | `.filter(task_name=task.task_name, project_type=project.project_type)` | legacy name-keyed checklist link fallback, scoped to project type | included (type-generic) | OTHER | |
| 55 | projects/views.py | _apply_task_status_change | 6641 | `new_status == Task.DONE and project.project_type == 'OPEX' and task.approved_at is None` | refuses direct Done (two-step rung) | excluded (comment: CAPEX deliberately keeps one-step) | APPROVAL | |
| 56 | projects/views.py | _apply_task_status_change | 6645 | `"... a RESCO task must be submitted for approval ..."` | refusal message text (cont. of 6641) | n/a-display | APPROVAL | |
| 57 | projects/views.py | _approval_preconditions | 7071 | `if project.project_type != 'OPEX':` | refuses submit/approve/reject ("Two-step approval applies to OPEX sites only") | excluded | APPROVAL | |
| 58 | projects/views.py | task_approve | 7434 | `"You submitted '...' yourself — a RESCO task must be approved by someone other ..."` | self-approval refusal message text | n/a-display | APPROVAL | |
| 59 | projects/views.py | boq_detail | 8666 | `if project.project_type == 'OPEX' and user_can_edit_project_boq(...)` | redirects OPEX BOQ author to opex_boq_entry picker; others (incl. CAPEX) stay on Residential BOQ screen | excluded (CAPEX gets Residential BOQ screen) | BOQ | ELSE-OF-OPEX |
| 60 | projects/views.py | boq_detail | 8696 | `BOQItemMaster.objects.filter(is_active=True, project_type='Residential')` | BOQ auto-create seeds get_standard_boq_items() with Residential catalogue masters — reached by any non-OPEX project incl. CAPEX | included (CAPEX BOQ seeded with Residential catalogue) | BOQ | |
| 61 | projects/views.py | opex_boq_entry | 9019 | `if project.project_type != 'OPEX': raise Http404` | OPEX BOQ picker | excluded | BOQ | |
| 62 | projects/views.py | opex_boq_entry | 9020 | `Http404('The BOQ picker is for RESCO sites. ...')` | message (cont. of 9019) | n/a-display | BOQ | |
| 63 | projects/views.py | opex_boq_download | 9402 | `if project.project_type != 'OPEX': raise Http404` | OPEX BOQ xlsx download | excluded | BOQ | |
| 64 | projects/views.py | opex_boq_download | 9403 | `Http404('The BOQ download is for RESCO sites. ...')` | message (cont. of 9402) | n/a-display | BOQ | |
| 65 | projects/views.py | _parse_boq_workbook | 9693 | `'more rows than there are items in the RESCO catalogue ...'` | upload row-cap error text | n/a-display | BOQ | |
| 66 | projects/views.py | _validate_boq_rows | 9737 | `'item code "{code}" is not in the RESCO catalogue.'` | upload row error text | n/a-display | BOQ | |
| 67 | projects/views.py | opex_boq_upload | 9782 | `if project.project_type != 'OPEX': raise Http404` | OPEX BOQ xlsx upload | excluded | BOQ | |
| 68 | projects/views.py | opex_boq_upload | 9783 | `Http404('The BOQ upload is for RESCO sites. ...')` | message (cont. of 9782) | n/a-display | BOQ | |
| 69 | projects/views.py | boq_correct | 10114 | `if project.project_type != 'OPEX': raise Http404` | QC reviewer BOQ correction | excluded | BOQ | |
| 70 | projects/views.py | boq_correct | 10115 | `Http404('BOQ correction is a RESCO design-review action. ...')` | message (cont. of 10114) | n/a-display | BOQ | |
| 71 | projects/views.py | milestone_create | 10713 | `if project.project_type != 'Residential':` | refuses creating M1/M2/M3 payment milestones | included in refusal (same as OPEX) | OTHER | NEQ-RES |
| 72 | projects/views.py | milestone_create | 10717 | `f'... do not apply to a {project.project_type} project.'` | message (cont. of 10713) | n/a-display | OTHER | |
| 73 | projects/views.py | project_overview | 11051 | `show_design_selector = project.project_type != 'OPEX'` | PM "assign designer" selector (Project.assigned_design) + can_assign_design + the assign_design POST refusal at 11148 | included (CAPEX gets Residential-style selector) | DESIGN | ELSE-OF-OPEX |
| 74 | projects/views.py | project_overview | 11151 | `'Design for RESCO sites is allocated by the Design Head ...'` | refusal message text | n/a-display | DESIGN | |
| 75 | projects/views.py | project_overview | 11530 | `and project.project_type == 'Residential'` | show_cascade_option (cascade scheduling button) | excluded | OTHER | |
| 76 | projects/views.py | project_overview | 11587 | `gantt_available = (project.project_type == 'Residential')` | Gantt card (internal + client views) | excluded | DASHBOARD/UI | |
| 77 | projects/views.py | zoho_deal_closed_webhook | 11784 | `project_type='Residential',` | Zoho intake hardcodes Residential; webhook never creates CAPEX | n/a-data (never created) | OTHER | |
| 78 | projects/views.py | admin_project_list | 15496 | `projects = projects.filter(project_type=selected_type)` | admin panel ?type= filter (any choice incl. CAPEX) | n/a-display | DASHBOARD/UI | |
| 79 | projects/views.py | _residential_template_duration_rows | 15809 | `resolve_active_task_template('Residential')` | admin duration-table page reads Residential template only | n/a-display | OTHER | |
| 80 | projects/views.py | _checklist_task_name_choices | 15905 | `for project_type, type_label in Project.PROJECT_TYPE_CHOICES:` | checklist-link task picker groups: one group per type with an active template | included if an active CAPEX template exists, else skipped | OTHER | |
| 81 | projects/views.py | _checklist_task_name_choices | 15906 | `template = resolve_active_task_template(project_type)` | (cont. of 15905) | included if active CAPEX template | OTHER | |
| 82 | projects/views.py | _checklist_task_name_choices | 15919 | `groups.append((project_type, type_label, tasks))` | (cont. of 15905) | included if active CAPEX template | OTHER | |
| 83 | projects/views.py | _resolve_checklist_link_target | 15951 | `project_type = (request_post.get('project_type') or '').strip()` | legacy name+type POST resolution of checklist link target | included (type-generic) | OTHER | |
| 84 | projects/views.py | _resolve_checklist_link_target | 15952 | `if not task_name or not project_type:` | (cont. of 15951) | included (type-generic) | OTHER | |
| 85 | projects/views.py | _resolve_checklist_link_target | 15955 | `if group_type != project_type: continue` | (cont. of 15951) | included (type-generic) | OTHER | |
| 86 | projects/views.py | admin_checklist_link_add | 16331 | `project_type = template_task.phase.template.project_type` | checklist link add: type derived from template | included (type-generic) | OTHER | |
| 87 | projects/views.py | admin_checklist_link_add | 16336 | `template_task__phase__template__project_type=project_type)` | duplicate-link check by code + template type | included (type-generic) | OTHER | |
| 88 | projects/views.py | admin_checklist_link_add | 16337 | `\| Q(task_name=task_name, project_type=project_type))` | duplicate-link check legacy name + type | included (type-generic) | OTHER | |
| 89 | projects/views.py | admin_checklist_link_add | 16341 | `f'"{task_name}" ({project_type}) is already assigned to this checklist.'` | message | n/a-display | OTHER | |
| 90 | projects/views.py | admin_checklist_link_add | 16345 | `f'"{task_name}" ({project_type}) is already assigned to checklist '` | message | n/a-display | OTHER | |
| 91 | projects/views.py | admin_checklist_link_add | 16356 | `f"({project_type}, template task {template_task.code})"` | activity log text | n/a-display | OTHER | |
| 92 | projects/views.py | admin_checklist_link_add | 16358 | `f'Assigned to "{task_name}" ({project_type}).'` | message | n/a-display | OTHER | |
| 93 | projects/views.py | admin_checklist_link_delete | 16372 | `task_name, project_type = link.task_name, link.project_type` | values for log/message | n/a-display | OTHER | |
| 94 | projects/views.py | admin_checklist_link_delete | 16376 | `f"Unassigned checklist ... ({project_type})"` | activity log text | n/a-display | OTHER | |
| 95 | projects/views.py | admin_checklist_link_delete | 16378 | `f'Unassigned from "{task_name}" ({project_type}).'` | message | n/a-display | OTHER | |
| 96 | projects/views.py | admin_boq_items | 16407 | `.order_by('project_type', 'sort_order', 'code'))` | admin BOQ catalogue ordering | n/a-display | BOQ | |
| 97 | projects/views.py | admin_boq_items | 16417 | `items = items.filter(project_type=type_filter)` | admin BOQ catalogue ?type= filter | n/a-display | BOQ | |
| 98 | projects/views.py | admin_boq_items | 16425 | `BOQItemMaster.objects.filter(project_type=value).count())` | per-type catalogue counts (CAPEX row shown) | n/a-display | BOQ | |
| 99 | projects/design_views.py | _opex_site | 281 | `def _opex_site(project_id):` | The single type guard for every OPEX design-workflow view in design_views.py (survey, allocate, QC, Arka, artifacts, PM approval, change request; call sites rows below). Docstring: "Residential and CAPEX are unreachable through every view in this module." | excluded | DESIGN | |
| 100 | projects/design_views.py | _opex_site | 285 | `if project.project_type != 'OPEX':` | Raises Http404 for any non-OPEX project → whole design workflow refuses CAPEX | excluded | DESIGN | |
| 101 | projects/design_views.py | _opex_site | 286 | `raise Http404('Design workflow applies to RESCO sites only.')` | cont. of line 285: the 404 message (label "RESCO") | excluded | DESIGN | |
| 102 | projects/design_views.py | sync_delivery_mirrors | 935 | `if project.project_type != 'OPEX':` | ONLY chooses the log branch when `_delivery_mirror_tasks()` returned `{}` (silent for non-OPEX vs debug/warning for OPEX). The sync itself is NOT type-gated: `_delivery_mirror_tasks()` looks up `is_mirror=True, template_task__code__in=DELIVERY_MIRROR_CODES` on the project. A CAPEX project carrying those mirror tasks would be synced; a CAPEX project without them returns 0 silently (no warning). Called from DeliveryChallan GRN path (models.py ~2137) and views.py ~13980 | included (sync code-keyed; this line is log-only) | MIRROR | |
| 103 | projects/design_views.py | design_head_sites | 1443 | `get_object_or_404(Program, pk=pk, is_deleted=False, program_type='OPEX')` | Design Head per-tender sites screen 404s for a CAPEX Program | excluded | DESIGN | |
| 104 | projects/design_views.py | design_survey_upload | 1678 | `project = _opex_site(project_id)` | Survey upload (creates DesignAssignment lazily) 404 for CAPEX | excluded | DESIGN | |
| 105 | projects/design_views.py | design_survey_link_set | 1804 | `project = _opex_site(project_id)` | Survey link set 404 for CAPEX | excluded | DESIGN | |
| 106 | projects/design_views.py | design_survey_download | 1902 | `project = _opex_site(project_id)` | Survey download 404 for CAPEX | excluded | DESIGN | |
| 107 | projects/design_views.py | design_allocate | 2050 | `project = _opex_site(project_id)` | Designer allocation 404 for CAPEX | excluded | DESIGN | |
| 108 | projects/design_views.py | design_bulk_allocate | 2091 | `get_object_or_404(Program, pk=pk, is_deleted=False, program_type='OPEX')` | Bulk designer allocation per tender 404 for CAPEX Program | excluded | DESIGN | |
| 109 | projects/design_views.py | design_bulk_allocate | 2118 | `get_object_or_404(Project, project_id=project_id, is_deleted=False, project_type='OPEX', program=program)` | Each site in the bulk batch must be OPEX; a CAPEX id aborts the batch with 404 | excluded | DESIGN | |
| 110 | projects/design_views.py | design_assign_qc | 2213 | `project = _opex_site(project_id)` | QC reviewer assignment 404 for CAPEX | excluded | DESIGN | |
| 111 | projects/design_views.py | design_due_date_propose | 2340 | `project = _opex_site(project_id)` | Design due-date propose 404 for CAPEX | excluded | DESIGN | |
| 112 | projects/design_views.py | design_due_date_approve | 2416 | `project = _opex_site(project_id)` | Design due-date approve 404 for CAPEX | excluded | DESIGN | |
| 113 | projects/design_views.py | design_due_date_reject | 2465 | `project = _opex_site(project_id)` | Design due-date reject 404 for CAPEX | excluded | DESIGN | |
| 114 | projects/design_views.py | design_due_date_change | 2532 | `project = _opex_site(project_id)` | Design due-date change 404 for CAPEX | excluded | DESIGN | |
| 115 | projects/design_views.py | design_mark_blocked | 2609 | `project = _opex_site(project_id)` | Design blocked flag 404 for CAPEX | excluded | DESIGN | |
| 116 | projects/design_views.py | design_site_workspace | 3131 | `project = _opex_site(project_id)` | Designer site workspace (incl. OPEX BOQ catalogue/mandatory ids) 404 for CAPEX | excluded | DESIGN | |
| 117 | projects/design_views.py | design_head_review | 3192 | `project = _opex_site(project_id)` | Design Head review screen 404 for CAPEX | excluded | DESIGN | |
| 118 | projects/design_views.py | design_arka_submit | 3258 | `project = _opex_site(project_id)` | Arka submit 404 for CAPEX | excluded | DESIGN | |
| 119 | projects/design_views.py | design_arka_approve | 3395 | `project = _opex_site(project_id)` | Arka approve 404 for CAPEX | excluded | DESIGN | |
| 120 | projects/design_views.py | design_arka_reject | 3455 | `project = _opex_site(project_id)` | Arka reject 404 for CAPEX | excluded | DESIGN | |
| 121 | projects/design_views.py | design_arka_head_approve | 3521 | `project = _opex_site(project_id)` | Arka Head approve 404 for CAPEX | excluded | DESIGN | |
| 122 | projects/design_views.py | design_arka_head_reject | 3586 | `project = _opex_site(project_id)` | Arka Head reject 404 for CAPEX | excluded | DESIGN | |
| 123 | projects/design_views.py | design_artifact_upload | 3670 | `project = _opex_site(project_id)` | Design artifact upload 404 for CAPEX | excluded | DESIGN | |
| 124 | projects/design_views.py | design_file_download | 3780 | `project = _opex_site(project_id)` | Design file download 404 for CAPEX | excluded | DESIGN | |
| 125 | projects/design_views.py | design_boq_complete | 3840 | `project = _opex_site(project_id)` | Designer marks BOQ complete (mandatory-item check via get_opex_mandatory_items) 404 for CAPEX | excluded | BOQ | |
| 126 | projects/design_views.py | design_qc_start | 4239 | `project = _opex_site(project_id)` | QC start 404 for CAPEX | excluded | DESIGN | |
| 127 | projects/design_views.py | design_qc_pass | 4312 | `project = _opex_site(project_id)` | QC pass 404 for CAPEX | excluded | DESIGN | |
| 128 | projects/design_views.py | design_qc_fail | 4373 | `project = _opex_site(project_id)` | QC fail 404 for CAPEX | excluded | DESIGN | |
| 129 | projects/design_views.py | design_head_qc_pass | 4463 | `project = _opex_site(project_id)` | Head QC pass 404 for CAPEX | excluded | DESIGN | |
| 130 | projects/design_views.py | design_head_qc_fail | 4554 | `project = _opex_site(project_id)` | Head QC fail 404 for CAPEX | excluded | DESIGN | |
| 131 | projects/design_views.py | design_pm_approval_queue | 4888 | `.filter(..., project__is_deleted=False, project__project_type='OPEX')` | PM design-approval queue rows (DesignAssignment awaiting PM approval) exclude CAPEX | excluded | APPROVAL | |
| 132 | projects/design_views.py | design_pm_approval_queue | 4944 | `attempt__assignment__project__project_type='OPEX')` | Change-request rows (waiting + history) on the PM queue exclude CAPEX | excluded | DESIGN | |
| 133 | projects/design_views.py | design_pm_approve | 5026 | `project = _opex_site(project_id)` | PM design approval 404 for CAPEX | excluded | APPROVAL | |
| 134 | projects/design_views.py | design_pm_reject | 5089 | `project = _opex_site(project_id)` | PM design rejection 404 for CAPEX | excluded | APPROVAL | |
| 135 | projects/design_views.py | design_head_return_to_pm | 5248 | `project = _opex_site(project_id)` | Head returns PM-rejected package to PM, 404 for CAPEX | excluded | APPROVAL | |
| 136 | projects/design_views.py | design_head_send_back | 5341 | `project = _opex_site(project_id)` | Head sends PM-rejected package back to designer, 404 for CAPEX | excluded | DESIGN | |
| 137 | projects/design_views.py | design_change_request | 5629 | `project = _opex_site(project_id)` | Raising a design/BOQ change request (POST) 404 for CAPEX | excluded | DESIGN | |
| 138 | projects/design_views.py | design_qc_review | 6832 | `project = _opex_site(project_id)` | QC review screen 404 for CAPEX | excluded | DESIGN | |
| 139 | projects/design_views.py | designer_dashboard_context | 7007 | `if project.project_type != 'OPEX': continue` | Per-site design state/actions on the Design dashboard (views.py ~1564) only built for OPEX; CAPEX cards get no design context | excluded | DASHBOARD/UI | |
| 140 | projects/design_views.py | design_head_dashboard_counts | 7164 | `Program.objects.filter(is_deleted=False, program_type='OPEX')` | Tender list on Design Head dashboard strip (views.py ~1580) omits CAPEX programs | excluded | DASHBOARD/UI | |
| 141 | projects/design_views.py | design_qc_dashboard_counts | 7253 | `Program.objects.filter(is_deleted=False, program_type='OPEX')` | Tender list on Design QC dashboard strip (views.py ~1584) omits CAPEX programs | excluded | DASHBOARD/UI | |
| 142 | projects/design_views.py | pm_change_request_targets | 7268 | `if project.project_type != 'OPEX': continue` | Which PM-dashboard sites get a "raise change request" link (views.py ~1033); CAPEX never | excluded | DESIGN | |
| 143 | projects/design_views.py | design_tender_dashboard | 7312 | `get_object_or_404(Program, ..., program_type='OPEX')` | Per-tender design dashboard 404 for CAPEX Program | excluded | DASHBOARD/UI | |
| 144 | projects/design_views.py | design_qc_dashboard | 7388 | `get_object_or_404(Program, ..., program_type='OPEX')` | Design QC per-tender dashboard 404 for CAPEX Program | excluded | DASHBOARD/UI | |
| 145 | projects/design_views.py | design_change_request_form | 7430 | `project = _opex_site(project_id)` | Change-request form (GET) 404 for CAPEX | excluded | DESIGN | |
| 146 | projects/design_views.py | _tender_or_404 | 7792 | `get_object_or_404(Program, pk=pk, is_deleted=False, program_type='OPEX')` | Resolves tender for SCM group screen + `site_group_create` (callers at ~7831, ~7875); CAPEX Program 404 | excluded | SCM | |
| 147 | projects/design_views.py | _group_or_404 | 7810 | `SiteGroup, pk=pk, program__is_deleted=False, program__program_type='OPEX', group_type=GROUP_TYPE_PROCUREMENT` | Resolves SCM procurement group for site_group_detail / add_sites / remove_site / lock (4 callers ~7978–8100); groups under a CAPEX Program 404 | excluded | SCM | |
| 148 | projects/design_views.py | scm_opex_tender_rows | 8179 | `Program.objects.filter(is_deleted=False, program_type='OPEX')` | SCM dashboard per-tender procurement rows (views.py ~2055, itself behind `_context_includes(ctx, 'OPEX')`); CAPEX programs never listed | excluded | SCM | |
| 149 | projects/design_views.py | _analytics_scope | 8265 | `Program.objects.filter(is_deleted=False, program_type='OPEX')` | All-tenders design quality analytics scope omits CAPEX | excluded | DASHBOARD/UI | |
| 150 | projects/design_views.py | _analytics_scope | 8267 | `get_object_or_404(Program, ..., program_type='OPEX')` | Single-tender analytics 404 for CAPEX Program | excluded | DASHBOARD/UI | |
| 151 | projects/design_views.py | design_quality_analytics | 8307 | `Program.objects.filter(is_deleted=False, program_type='OPEX')` | Tender selector choices on quality analytics omit CAPEX | excluded | DASHBOARD/UI | |
| 152 | projects/design_views.py | <module> | 8420 | `_CATALOGUE_FORBIDDEN = 'The RESCO BOQ catalogue is for the Design Head only.'` | Refusal message text for catalogue views | n/a-display | DASHBOARD/UI | |
| 153 | projects/design_views.py | _opex_catalogue_qs | 8430 | `BOQItemMaster.objects.filter(project_type='OPEX')` | Design Head's catalogue screen manages only OPEX BOQItemMaster rows; no CAPEX catalogue exists | excluded | BOQ | |
| 154 | projects/design_views.py | design_boq_catalogue_create | 8495 | `del form.fields['project_type']` | Hides type field; Head cannot pick a type | n/a-display | BOQ | |
| 155 | projects/design_views.py | design_boq_catalogue_create | 8498 | `item.project_type = 'OPEX'` | Every Head-created catalogue item is stamped OPEX | n/a-data | BOQ | |
| 156 | projects/design_views.py | design_boq_catalogue_create | 8501 | `f"Created RESCO BOQ catalogue item "` | Activity log text | n/a-display | BOQ | |
| 157 | projects/design_views.py | design_boq_catalogue_create | 8515 | `del form.fields['project_type']` | GET branch of same form, type hidden | n/a-display | BOQ | |
| 158 | projects/design_views.py | design_boq_catalogue_create | 8519 | `'title': 'Add RESCO Catalogue Item',` | Page title | n/a-display | BOQ | |
| 159 | projects/design_views.py | design_boq_catalogue_edit | 8550 | `del form.fields['project_type']` | Type not editable (POST) | n/a-display | BOQ | |
| 160 | projects/design_views.py | design_boq_catalogue_edit | 8554 | `f"Updated RESCO BOQ catalogue item '{item.code}' "` | Activity log text | n/a-display | BOQ | |
| 161 | projects/design_views.py | design_boq_catalogue_edit | 8563 | `del form.fields['project_type']` | Type not editable (GET) | n/a-display | BOQ | |
| 162 | projects/design_views.py | design_boq_catalogue_edit | 8567 | `'title': f'Edit RESCO Catalogue Item — {item.code}',` | Page title | n/a-display | BOQ | |
| 163 | projects/design_views.py | design_boq_catalogue_toggle | 8610 | `f"{state.capitalize()} RESCO BOQ catalogue item '{item.code}'"` | Activity log text | n/a-display | BOQ | |
| 164 | projects/models.py | Project | 16 | `('Residential', 'Residential'),` | PROJECT_TYPE_CHOICES vocabulary | n/a-display | DASHBOARD/UI | |
| 165 | projects/models.py | Project | 17 | `('OPEX', 'RESCO'),` | Stored 'OPEX', displayed 'RESCO' | n/a-display | DASHBOARD/UI | |
| 166 | projects/models.py | Project | 18 | `('CAPEX', 'CAPEX'),` | CAPEX is a valid stored value | n/a-display | DASHBOARD/UI | |
| 167 | projects/models.py | Project | 51 | `project_type = models.CharField(max_length=20, choices=PROJECT_TYPE_CHOICES)` | Field definition; no default, no constraint beyond choices | n/a-data | OTHER | |
| 168 | projects/models.py | Project._validate_program_link | 189 | `if self.project_type == 'Residential': raise ValidationError(...)` | Residential may never link to a Program; CAPEX passes this check | included | PROGRAM | |
| 169 | projects/models.py | Project._validate_program_link | 191 | `program_type = self.program.program_type` | cont. of 192 | included | PROGRAM | |
| 170 | projects/models.py | Project._validate_program_link | 192 | `if self.project_type != program_type:` | Site type must equal Program type: CAPEX site may join a CAPEX Program, refused under an OPEX Program (runs on every Project.save) | included (CAPEX→CAPEX Program only) | PROGRAM | |
| 171 | projects/models.py | Project._validate_program_link | 194 | `f'Type mismatch: a {self.project_type} site cannot belong to a '` | cont. of 192, error text | n/a-display | PROGRAM | |
| 172 | projects/models.py | Project._validate_program_link | 195 | `f'{program_type} Program.'` | cont. of 192, error text | n/a-display | PROGRAM | |
| 173 | projects/models.py | Project.save | 209 | `self.project_id = generate_project_id(self.project_type)` | ID prefix via utils.generate_project_id PREFIX_MAP: CAPEX → `HRP-CAP-{year}-NNN` (only when project_id not preset) | included | OTHER | |
| 174 | projects/models.py | Program | 230 | `('OPEX', 'RESCO'),` | PROGRAM_TYPE_CHOICES | n/a-display | PROGRAM | |
| 175 | projects/models.py | Program | 231 | `('CAPEX', 'CAPEX'),` | CAPEX is a valid Program type | n/a-display | PROGRAM | |
| 176 | projects/models.py | Program | 236 | `program_type = models.CharField(max_length=20, choices=PROGRAM_TYPE_CHOICES)` | Field definition | n/a-data | PROGRAM | |
| 177 | projects/models.py | Program.__str__ | 289 | `label = self.tender_reference_number if self.program_type == 'OPEX' else self.name` | String label: CAPEX shows name | n/a-display | PROGRAM | ELSE-OF-OPEX |
| 178 | projects/models.py | Program.__str__ | 290 | `return f"{label or self.name} ({self.program_type})"` | String label includes raw type | n/a-display | PROGRAM | |
| 179 | projects/models.py | Program.reference_display | 297 | `if self.program_type == 'OPEX' and self.tender_reference_number:` | Reference shown: CAPEX falls back to name | n/a-display | PROGRAM | ELSE-OF-OPEX |
| 180 | projects/models.py | BOQItemMaster | 1007 | `project_type = models.CharField(` | Catalogue scope field (Residential 37 / OPEX 207 rows); choices include CAPEX but no CAPEX rows are created by any screen (Head screen stamps OPEX) | n/a-data | BOQ | |
| 181 | projects/models.py | BOQItemMaster | 1010 | `default='Residential',` | Default scope for new master rows | n/a-data | BOQ | |
| 182 | projects/models.py | get_standard_boq_items | 1061 | `.filter(is_active=True, project_type='Residential')` | Seed rows for a new Residential BOQ (views.boq_detail ~8700); CAPEX never reads this list | excluded | BOQ | |
| 183 | projects/models.py | get_opex_boq_catalogue | 1098 | `.filter(is_active=True, project_type='OPEX')` | OPEX picker catalogue (design_site_workspace, views opex_boq_entry/download/upload); type-gating of the calling screens decides CAPEX access | excluded | BOQ | |
| 184 | projects/models.py | get_opex_mandatory_items | 1132 | `.filter(is_active=True, project_type='OPEX', is_mandatory=True)` | Mandatory items the designer must quantify (design_boq_complete ~3878, workspace ~3068). Keyed by BOQItemMaster.project_type, not by project/template | excluded | BOQ | |
| 185 | projects/models.py | opex_catalogue_category_order | 1148 | `.filter(is_active=True, project_type='OPEX')` | Category ordering for OPEX picker/sheet | excluded | BOQ | |
| 186 | projects/models.py | VendorOrder | 2984 | `project_type = models.CharField(max_length=20, choices=Project.PROJECT_TYPE_CHOICES)` | Order type stamped at raise; choices allow CAPEX but raise screens (purchases_views RECORD_TYPES) offer only Residential/OPEX | n/a-data | SCM | |
| 187 | projects/models.py | TaskDurationTemplate | 3375 | `('residential', 'Residential'),` | Separate lowercase vocabulary; only residential durations exist | n/a-data | OTHER | |
| 188 | projects/models.py | TaskDurationTemplate | 3378 | `project_type = models.CharField(max_length=50, choices=PROJECT_TYPE_CHOICES, default='residential')` | Duration rows read only by utils._residential_duration_resolver (filter project_type='residential'); no OPEX/CAPEX durations | excluded | OTHER | |
| 189 | projects/models.py | TaskDurationTemplate.Meta | 3392 | `unique_together = ('project_type', 'task_name')` | Uniqueness per lowercase type + task name | n/a-data | OTHER | |
| 190 | projects/models.py | TaskDurationTemplate.Meta | 3393 | `ordering = ['project_type', 'phase_name', 'task_name']` | Ordering | n/a-display | OTHER | |
| 191 | projects/models.py | TaskDurationTemplate.__str__ | 3396 | `return f"{self.project_type} \| ..."` | String label | n/a-display | OTHER | |
| 192 | projects/models.py | TaskTemplate | 3473 | `project_type = models.CharField(max_length=20, choices=Project.PROJECT_TYPE_CHOICES)` | activation: the template is chosen by `filter(project_type=..., status='active')` (index comment line 3486). Choices allow a CAPEX template row; uniqueness is (code, version_no) only, not per project_type | n/a-data | OTHER | |
| 193 | projects/models.py | TaskTemplate.Meta | 3487 | `models.Index(fields=['project_type', 'status'], name='tasktmpl_type_status_idx')` | activation lookup index | n/a-data | OTHER | |
| 194 | projects/models.py | ChecklistTaskLink | 4282 | `project_type = models.CharField(max_length=20, choices=Project.PROJECT_TYPE_CHOICES)` | Projection of template_task's template.project_type; used by the name-based fallback in views._checklist_for_task() (`filter(task_name=..., project_type=project.project_type)`) |  n/a-data | OTHER | |
| 195 | projects/models.py | ChecklistTaskLink.Meta | 4285 | `unique_together = ('task_name', 'project_type')` | One link per (label, type): a separate CAPEX template whose task labels match OPEX labels would not collide (different project_type); but two templates of the SAME type with same label collide | n/a-data | OTHER | |
| 196 | projects/models.py | ChecklistTaskLink.Meta | 4286 | `ordering = ['project_type', 'task_name']` | Ordering | n/a-display | OTHER | |
| 197 | projects/models.py | ChecklistTaskLink.save | 4307 | `self.project_type = tt.phase.template.project_type` | Link's type copied from the linked template task's template: link follows the template, not the project. Read by views._checklist_for_task, whose code path filters `template_task__code=code, template_task__phase__template__project_type=project.project_type` and whose name fallback filters `project_type=project.project_type` — so a CAPEX project whose tasks came from the OPEX template (template.project_type='OPEX') resolves NO checklist on either path | n/a-data | OTHER | |
| 198 | projects/models.py | ChecklistTaskLink.save | 4310 | `'task_name', 'project_type'}` | cont. of 4307: update_fields widened | n/a-data | OTHER | |
| 199 | projects/models.py | ChecklistTaskLink.__str__ | 4314 | `return f"{self.task_name} [{self.project_type}] → checklist {self.checklist_id}"` | String label | n/a-display | OTHER | |
| 200 | projects/admin.py | ProgramAdmin | 76 | `list_display = ['name', 'program_type', ...]` | Django admin Program changelist column | n/a-display | PROGRAM | |
| 201 | projects/admin.py | ProgramAdmin | 78 | `list_filter = ['program_type', ...]` | Django admin Program changelist filter (OPEX/CAPEX) | n/a-display | PROGRAM | |
| 202 | projects/admin.py | ProgramAdmin | 86 | `'fields': ('program_type', ...)` | Django admin Program edit form field (OPEX or CAPEX choosable) | n/a-display | PROGRAM | |
| 203 | projects/admin.py | ProgramAdmin | 90 | `('RESCO Tender', {'fields': ('tender_reference_number', ...)})` | collapsed fieldset label for OPEX-only tender/PPA fields; shown for CAPEX Programs too (no condition) | n/a-display | PROGRAM | |
| 204 | projects/admin.py | ProjectAdmin | 117 | `list_display = [..., 'project_type', ...]` | Django admin Project changelist column | n/a-display | DASHBOARD/UI | |
| 205 | projects/admin.py | ProjectAdmin | 121 | `list_filter = ['project_type', ...]` | Django admin Project changelist filter | n/a-display | DASHBOARD/UI | |
| 206 | projects/admin.py | ProjectAdmin | 159 | `'fields': ('project_id', 'project_type', ...)` | Django admin Project edit form field (type editable in admin, any choice incl. CAPEX) | n/a-display | DASHBOARD/UI | |
| 207 | projects/admin.py | ChecklistTaskLinkInline | 476 | `fields = ['template_task', 'task_name', 'project_type']` | inline on Checklist admin: link row shows derived project_type | n/a-display | TASK | |
| 208 | projects/admin.py | ChecklistTaskLinkInline | 477 | `readonly_fields = ['task_name', 'project_type']` | project_type read-only; derived from template_task FK by ChecklistTaskLink.save() | n/a-display | TASK | |
| 209 | projects/admin.py | ChecklistTaskLinkAdmin | 544 | `list_display = ['task_name', 'project_type', 'template_task_code', 'checklist']` | ChecklistTaskLink changelist column | n/a-display | TASK | |
| 210 | projects/admin.py | ChecklistTaskLinkAdmin | 545 | `list_filter = ['project_type']` | ChecklistTaskLink changelist filter | n/a-display | TASK | |
| 211 | projects/admin.py | ChecklistTaskLinkAdmin | 547 | `readonly_fields = ['task_name', 'project_type']` | project_type read-only (derived on save) | n/a-display | TASK | |
| 212 | projects/admin.py | TaskTemplateAdmin | 775 | `list_display = ['code', 'version_no', 'label', 'project_type', ...]` | TaskTemplate changelist column | n/a-display | OTHER | |
| 213 | projects/admin.py | TaskTemplateAdmin | 777 | `list_filter = ['code', 'project_type', 'status']` | TaskTemplate changelist filter | n/a-display | OTHER | |
| 214 | projects/approval_forms.py | bill_site_choices | 644 | `.only('pk', 'project_id', 'customer_name', 'project_type', 'status')` | column fetch for contractor-bill raise step-1 site list; bill_sites() has NO type filter (any live non-Draft non-test site, CAPEX included) | included | APPROVAL | |
| 215 | projects/approval_views.py | _bill_now | 725 | `task_status_label(t, project.project_type)` | bill detail task status text; OPEX shows "Done — approved / not yet approved", others plain status (see bill_rules.task_status_label) | included (Residential-style label) | APPROVAL | ELSE-OF-OPEX |
| 216 | projects/approval_views.py | _task_groups | 1332 | `task_status_label(task, project.project_type)` | bill raise step-2 task list status text; same rule as above | included (Residential-style label) | APPROVAL | ELSE-OF-OPEX |
| 217 | projects/bill_rules.py | task_complete_for_bill | 75 | `def task_complete_for_bill(task, project_type):` | signature; type passed in by incomplete_task_warnings (bill warnings D-A33) | n/a-data | APPROVAL | |
| 218 | projects/bill_rules.py | task_complete_for_bill | 89 | `if project_type == 'OPEX': return task.approved_at is not None` | bill "task complete" = Done AND approved for OPEX; Done alone for Residential and CAPEX | included (Done-only rule) | APPROVAL | ELSE-OF-OPEX |
| 219 | projects/bill_rules.py | incomplete_task_warnings | 102 | `elif task.status == Task.DONE and not task_complete_for_bill(task, project.project_type)` | "Done but not yet approved" warning on contractor bill raise/detail (via bill_warnings_tagged); never fires on CAPEX | excluded (warning OPEX-only) | APPROVAL | ELSE-OF-OPEX |
| 220 | projects/bill_rules.py | task_status_label | 211 | `def task_status_label(task, project_type):` | signature; called from approval_views._bill_now / _task_groups | n/a-data | APPROVAL | |
| 221 | projects/bill_rules.py | task_status_label | 217 | `if task.status == Task.DONE and project_type == 'OPEX':` | approval annotation on Done status in bill screens; CAPEX gets plain get_status_display() | excluded | APPROVAL | ELSE-OF-OPEX |
| 222 | projects/cod_views.py | <module> | 75 | `COD_OPEX_ONLY = 'COD is recorded on RESCO sites only.'` | refusal message raised by record_refusal for non-OPEX | n/a-display | CLOSEOUT | |
| 223 | projects/cod_views.py | record_refusal | 172 | `if project.project_type != 'OPEX' or project.is_deleted:` | refuses COD recording (screen pre-check + record_cod under lock) on any non-OPEX site | excluded | CLOSEOUT | |
| 224 | projects/cod_views.py | record_refusal | 173 | `raise CodRefused(COD_OPEX_ONLY)` | cont. of line 172 | excluded | CLOSEOUT | |
| 225 | projects/cod_views.py | cod_card_context | 198 | `if project.project_type != 'OPEX': return {'show_cod_card': False}` | COD card on project_overview (called from views.py project_overview) hidden on non-OPEX | excluded | CLOSEOUT | |
| 226 | projects/cod_views.py | _cod_site | 465 | `if project.project_type != 'OPEX': raise Http404` | 404 for cod_withdraw (POST) and cod_record_pdf on non-OPEX | excluded | CLOSEOUT | |
| 227 | projects/cod_views.py | cod_record | 527 | `if project.project_type != 'OPEX': raise Http404` | COD record screen 404 on non-OPEX | excluded | CLOSEOUT | |
| 228 | projects/forms.py | AdminUserEditForm | 170 | help_text `'Reviews RESCO designs at the FIRST gate...'` | is_design_qc flag help text (admin user edit) | n/a-display | DESIGN | |
| 229 | projects/forms.py | AdminUserEditForm | 178 | help_text `'May approve or reject a submitted RESCO task...'` | is_qaqc flag help text (admin user edit) | n/a-display | APPROVAL | |
| 230 | projects/forms.py | ProjectCreateForm.Meta | 365 | `'project_type',` in Meta.fields | generic project create form exposes project_type (Residential/CAPEX after __init__ trim) | included | OTHER | |
| 231 | projects/forms.py | ProjectCreateForm.__init__ | 390 | `self.fields['project_type'].choices = [` | trims choices of generic create form | included | OTHER | |
| 232 | projects/forms.py | ProjectCreateForm.__init__ | 391 | `for value, label in self.fields['project_type'].choices` | cont. of line 390 | included | OTHER | |
| 233 | projects/forms.py | ProjectCreateForm.__init__ | 392 | `if value != 'OPEX'` | removes OPEX from generic create; CAPEX remains selectable as a standalone (program-less) project via project_create | included | OTHER | |
| 234 | projects/forms.py | ProjectCreateForm.clean_project_type | 406 | `def clean_project_type(self):` | server-side type validation on generic create | included | OTHER | |
| 235 | projects/forms.py | ProjectCreateForm.clean_project_type | 409 | `value = self.cleaned_data.get('project_type')` | cont. of line 406 | included | OTHER | |
| 236 | projects/forms.py | ProjectCreateForm.clean_project_type | 410 | `if value == 'OPEX': raise ValidationError` | refuses smuggled OPEX; CAPEX passes, so CAPEX is created only through this generic form (no Program, no site_code) | included | OTHER | |
| 237 | projects/forms.py | ProjectCreateForm.clean_project_type | 412 | `'RESCO projects must be created under a Program, not from this form.'` | cont. of line 410 (message) | n/a-display | OTHER | |
| 238 | projects/forms.py | BOQItemMasterForm.Meta | 804 | `fields = ['code', 'project_type', ...]` | Admin BOQ catalogue item form type field (BOQItemMaster.project_type choices) | n/a-data | BOQ | |
| 239 | projects/forms.py | BOQItemMasterForm.__init__ | 818 | `self.fields['project_type'].help_text = (` | help text for catalogue type | n/a-display | BOQ | |
| 240 | projects/forms.py | BOQItemMasterForm.__init__ | 820 | `'...RESCO items are offered in the RESCO BOQ picker.'` | cont. of line 818 (text) | n/a-display | BOQ | |
| 241 | projects/forms.py | BOQItemMasterForm.__init__ | 823 | `'Residential: display grouping only. RESCO: groups the picker...'` | category help text | n/a-display | BOQ | |
| 242 | projects/forms.py | BOQItemMasterForm.__init__ | 824 | `'sheet, so it should match an existing RESCO category exactly.'` | cont. of line 823 | n/a-display | BOQ | |
| 243 | projects/forms.py | BOQItemMasterForm.__init__ | 829 | `'RESCO only. A mandatory item is added to every new RESCO BOQ...'` | is_mandatory help text | n/a-display | BOQ | |
| 244 | projects/forms.py | BOQItemMasterForm.clean | 874 | `if 'project_type' not in self.fields: return cleaned` | rule stands down on Design Head catalogue screens (which force 'OPEX') | n/a-data | BOQ | |
| 245 | projects/forms.py | BOQItemMasterForm.clean | 876 | `if cleaned.get('project_type') != 'OPEX':` | refuses is_mandatory on any non-OPEX catalogue row (Residential, and CAPEX if it were a choice) | excluded | BOQ | |
| 246 | projects/forms.py | BOQItemMasterForm.clean | 878 | `'Only RESCO items can be marked mandatory...'` | cont. of line 876 (message) | n/a-display | BOQ | |
| 247 | projects/forms.py | ProgramForm.Meta | 1014 | `'program_type', 'name', ...` in Meta.fields | Program create/edit form offers program_type (OPEX/CAPEX) | included | PROGRAM | |
| 248 | projects/forms.py | ProgramForm.Meta | 1018 | `'program_type': forms.Select(...)` | widget for the above | n/a-display | PROGRAM | |
| 249 | projects/forms.py | ProgramForm.Meta | 1028 | `'short_tender_code': 'Short tender code (RESCO)'` | label | n/a-display | PROGRAM | |
| 250 | projects/forms.py | ProgramForm.clean | 1040 | `program_type = cleaned.get('program_type')` | read for branch below | n/a-data | PROGRAM | |
| 251 | projects/forms.py | ProgramForm.clean | 1043 | `if program_type == 'OPEX':` | short_tender_code required + reserved/unique checks, tender-ref uniqueness | excluded | PROGRAM | |
| 252 | projects/forms.py | ProgramForm.clean | 1053 | `'Short tender code is required for a RESCO tender.'` | cont. of line 1043 (message) | n/a-display | PROGRAM | |
| 253 | projects/forms.py | ProgramForm.clean | 1086 | `elif program_type == 'CAPEX': cleaned['short_tender_code'] = ''` | CAPEX Program is creatable; its short_tender_code is blanked (so OPEX-style site id `{tender}-{site}` composition unavailable) | included | PROGRAM | |
| 254 | projects/order_views.py | vendor_order_create.write | 651 | `VendorOrder.objects.create(vendor=vendor, project_type=project.project_type, ...)` | single-site order stores the project's type; reachable only for Residential (user_can_raise_vendor_order) | excluded (gate upstream) | SCM | |
| 255 | projects/order_views.py | _raise_candidates | 1139 | `Program.objects.filter(program_type='OPEX', is_deleted=False)` | group-raise / purchases picker tenders and their sites + procurement groups: OPEX Programs only; CAPEX Programs/sites never offered | excluded | SCM | |
| 256 | projects/order_views.py | _raise_candidates | 1148 | `.only(..., 'project_type', ...)` | column fetch for candidate sites | n/a-data | SCM | |
| 257 | projects/order_views.py | _parse_group_sites | 1259 | `types = {project.project_type for project in resolved}` | refuses an order mixing site types; resolution accepts ANY site the user can view (no type filter) | included (not refused if posted) | SCM | |
| 258 | projects/order_views.py | _parse_group_sites | 1263 | `project_type = types.pop() if len(types) == 1 else None` | order type = sites' single type, so a hand-posted CAPEX site yields a CAPEX order on the group raise | included | SCM | |
| 259 | projects/order_views.py | _parse_group_sites | 1273 | `return sites, programs, project_type, unfrozen` | cont. of line 1263 | n/a-data | SCM | |
| 260 | projects/order_views.py | _parse_group_submission | 1404 | `project_type = site_type or 'OPEX'` | site-less group raise defaults to OPEX order; with sites takes their type (CAPEX passes through) | included (if sites posted) | SCM | |
| 261 | projects/order_views.py | _parse_group_submission | 1414 | `'project_type': project_type,` | carried to _write_order_record | n/a-data | SCM | |
| 262 | projects/order_views.py | _residential_candidates | 1431 | `.filter(project_type='Residential', is_deleted=False)` | purchases workspace picker's Residential project list | excluded | SCM | |
| 263 | projects/order_views.py | _residential_candidates | 1432 | `.only(..., 'project_type', ...)` | column fetch | n/a-data | SCM | |
| 264 | projects/order_views.py | _write_order_record | 1600 | `VendorOrder.objects.create(vendor=..., project_type=cleaned['project_type'], ...)` | persists order type from group raise / purchases record | n/a-data | SCM | |
| 265 | projects/payment_views.py | <module> | 56 | `import ... payment_project_type, payment_type_q` | import | n/a-data | SCM | |
| 266 | projects/payment_views.py | <module> | 67 | `QUEUE_TABS = [('Residential',...), ('OPEX','RESCO'), ('CAPEX','CAPEX')]` | payment queue tabs; CAPEX tab present (drawn conditionally) | included | SCM | IN-INCL-CAPEX |
| 267 | projects/payment_views.py | _invoice_awaited_orders | 105 | `def _invoice_awaited_orders(project_type):` | per-tab "invoice awaited" count | included | SCM | |
| 268 | projects/payment_views.py | _invoice_awaited_orders | 117 | `VendorOrder.objects.filter(project_type=project_type)` | cont. of line 105 | included | SCM | |
| 269 | projects/payment_views.py | payment_queue | 276 | `VendorOrder.objects.filter(project_type='CAPEX').values('pk')` | CAPEX tab drawn only if a CAPEX order exists... | included (conditional) | SCM | |
| 270 | projects/payment_views.py | payment_queue | 278 | `.filter(contractor_bill__project__project_type='CAPEX')` | ...or a payment on a CAPEX site's contractor bill exists | included (conditional) | SCM | |
| 271 | projects/payment_views.py | payment_queue | 281 | `if value != 'CAPEX' or capex_shown.exists()` | hides empty CAPEX tab | included (conditional) | SCM | |
| 272 | projects/payment_views.py | payment_queue.cell | 294 | `def cell(project_type, st):` | tab label backlog cell | included | SCM | |
| 273 | projects/payment_views.py | payment_queue.cell | 295 | `totals = counts[project_type][st]` | cont. of line 294 | included | SCM | |
| 274 | projects/payment_views.py | payment_mark_paid | 416 | `_query_string(tab=payment_project_type(payment))` | redirect back to the payment's own tab (CAPEX tab if CAPEX) | included | SCM | |
| 275 | projects/payments.py | payment_type_q | 62 | `def payment_type_q(project_types):` | the one payment-type predicate (queue tabs, Finance, CEO strip) | included (caller-chosen) | SCM | |
| 276 | projects/payments.py | payment_type_q | 71 | `types = list(project_types)` | cont. | included | SCM | |
| 277 | projects/payments.py | payment_type_q | 72 | `Q(vendor_order__project_type__in=types)` | order arm | included | SCM | |
| 278 | projects/payments.py | payment_type_q | 73 | `\| Q(contractor_bill__project__project_type__in=types)` | bill arm | included | SCM | |
| 279 | projects/payments.py | payment_project_type | 76 | `def payment_project_type(payment):` | single-row type (used by payment_mark_paid redirect) | included | SCM | |
| 280 | projects/payments.py | payment_project_type | 80 | `return payment.contractor_bill.project.project_type` | cont. | included | SCM | |
| 281 | projects/payments.py | payment_project_type | 81 | `return payment.vendor_order.project_type` | cont. | included | SCM | |
| 282 | projects/payments.py | payment_counts | 84 | `def payment_counts(project_types=None):` | counts for queue tabs, Finance dashboard (views.py:1640, 2972 via _context_types incl. CAPEX for 'tenders') | included | SCM | |
| 283 | projects/payments.py | payment_counts | 99 | `if project_types is not None:` | None = all types | included | SCM | |
| 284 | projects/payments.py | payment_counts | 100 | `queryset.filter(payment_type_q(project_types))` | cont. | included | SCM | |
| 285 | projects/payments.py | ceo_payment_strip | 120 | `def ceo_payment_strip(project_types, today):` | CEO Tenders money strip; caller passes CEO_TENDER_TYPES = ['OPEX'] (views.py:3167) | excluded (by caller) | DASHBOARD/UI | |
| 286 | projects/payments.py | ceo_payment_strip | 146 | `if project_types is not None:` | cont. | excluded (by caller) | DASHBOARD/UI | |
| 287 | projects/payments.py | ceo_payment_strip | 147 | `queryset.filter(payment_type_q(project_types))` | cont. | excluded (by caller) | DASHBOARD/UI | |
| 288 | projects/permissions.py | user_can_view_site_drawings | 1103 | `return user_can_view_project(user, project) and project.project_type == 'OPEX'` | site drawings card + site_drawing_open download (DR-2) | excluded | DESIGN | |
| 289 | projects/permissions.py | site_qaqc_engineer_id | 1610 | `if project is None or project.project_type != 'OPEX': return None` | active QA/QC engineer pk (overview, visit card, held-task exclusions) | excluded | CLOSEOUT | |
| 290 | projects/permissions.py | qaqc_engineer_projects_q | 1679 | `f'{prefix}project_type': 'OPEX',` | Q for "sites where profile is QA/QC engineer" (PM dashboard card/counts/drill-down G95, user_works_on_site) | excluded | CLOSEOUT | |
| 291 | projects/permissions.py | site_has_effective_qaqc | 2227 | `if project is None or project.project_type != 'OPEX': return False` | whether re-verify of punch points goes to an engineer (closeout 7b) | excluded | CLOSEOUT | |
| 292 | projects/permissions.py | user_can_conduct_qaqc_visit | 2301 | `if project is None or project.project_type != 'OPEX' or project.is_deleted: return False` | QA/QC visit start/answer/submit permission | excluded | CLOSEOUT | |
| 293 | projects/permissions.py | _project_allows_location_duplication | 2415 | `if project.project_type == 'Residential': return False` | duplicate-for-locations (can_duplicate_for_locations → task_duplicate view, row button): allowed on OPEX AND CAPEX | included | TASK | NEQ-RES |
| 294 | projects/permissions.py | <module> | 2524 | `REORDERABLE_PROJECT_TYPES = frozenset({'OPEX', 'CAPEX'})` | task reorder scope constant | included | TASK | IN-INCL-CAPEX |
| 295 | projects/permissions.py | _project_allows_phase_reorder | 2547 | `if project.project_type not in REORDERABLE_PROJECT_TYPES: return False` | can_reorder_phase → drag-reorder of a phase's tasks | included | TASK | IN-INCL-CAPEX |
| 296 | projects/permissions.py | user_can_raise_vendor_order | 2728 | `return project.project_type == 'Residential'` | single-site vendor order raise (vendor_order_create) and purchases per-site check | excluded | SCM | |
| 297 | projects/phase_rules.py | <module> | 78 | `('OPEX', 1): (PhaseRule('R1', ...), ...)` | RULES keyed by (TaskTemplate.code, version_no); only template code 'OPEX' v1 has rules | excluded (keyed on template code, not project type) | PHASE | |
| 298 | projects/phase_rules.py | SiteRuleState._load | 156 | `.filter(project_type=self.project.project_type, status=TaskTemplate.ACTIVE)` | task-less site reads the ACTIVE template of its own type for rules (R1) | excluded (no CAPEX template) | PHASE | |
| 299 | projects/phase_rules.py | phase_rule_warnings | 242 | `if project.project_type != 'OPEX': return []` | phase-rule warnings R1–Rn (views.py 5871 task rows, 6007 preview) | excluded | PHASE | |
| 300 | projects/phase_rules.py | phase_rule_warnings | 245 | `elif state.project.project_type != 'OPEX': return []` | same, when state passed | excluded | PHASE | |
| 301 | projects/phase_rules.py | start_warnings | 291 | `if project.project_type != 'OPEX' or task.is_mirror or task.is_not_applicable: return []` | chokepoint first-start rule warnings (views.py 6756 _apply_task_status_change) | excluded | PHASE | |
| 302 | projects/phase_rules.py | dc_creation_warnings | 300 | `if project.project_type != 'OPEX': return []` | R1 warning on create_delivery_challan | excluded | PHASE | |
| 303 | projects/punch_points.py | effective_qaqc_engineer | 383 | `if project is None or project.project_type != 'OPEX': return None` | names engineer for next_action() "who acts next" (closeout 7c) | excluded | CLOSEOUT | |
| 304 | projects/purchases_views.py | <module> | 66 | `RECORD_TYPES = [('Residential', 'Residential'), ('OPEX', 'RESCO')]` | purchases workspace type filter + Add PO/PI kind; CAPEX absent | excluded | SCM | |
| 305 | projects/purchases_views.py | <module> | 67 | `_RECORD_TYPE_LABELS = dict(RECORD_TYPES)` | validation set for the above | excluded | SCM | |
| 306 | projects/purchases_views.py | _recorded_against | 96 | `'residential': order.project_type == 'Residential',` | workspace row display flag | n/a-display | SCM | |
| 307 | projects/purchases_views.py | purchases_workspace | 143 | `orders = orders.filter(project_type=record_type)` | workspace filter (?type= only Residential/OPEX accepted); unfiltered list still shows CAPEX orders | n/a-display | SCM | |
| 308 | projects/purchases_views.py | purchases_workspace | 196 | `'record_types': RECORD_TYPES,` | filter chips | n/a-display | SCM | |
| 309 | projects/purchases_views.py | _record_project_type | 213 | `def _record_project_type(post, sites, programs, errors):` | derives Add PO/PI kind | excluded | SCM | |
| 310 | projects/purchases_views.py | _record_project_type | 225 | `site_kinds = {site['project'].project_type for site in sites}` | cont. | excluded | SCM | |
| 311 | projects/purchases_views.py | _record_project_type | 228 | `kinds = site_kinds \| {program.program_type for program in programs}` | cont. | excluded | SCM | |
| 312 | projects/purchases_views.py | _record_project_type | 230 | `errors.append('A PO / PI is recorded against Residential projects or against RESCO ...')` | mixed-kind refusal message | n/a-display | SCM | |
| 313 | projects/purchases_views.py | _record_project_type | 236 | `kind = post.get('project_type', '')` | asked kind when nothing recorded against | excluded | SCM | |
| 314 | projects/purchases_views.py | _record_project_type | 239 | `'Residential or RESCO.')` | message | n/a-display | SCM | |
| 315 | projects/purchases_views.py | _record_project_type | 242 | `errors.append('Only Residential and RESCO purchases are recorded here.')` | refuses CAPEX kind (kind not in _RECORD_TYPE_LABELS) | excluded | SCM | |
| 316 | projects/purchases_views.py | _parse_record_submission | 268 | `project_type = _record_project_type(post, sites, programs, errors)` | cont. | excluded | SCM | |
| 317 | projects/purchases_views.py | _parse_record_submission | 271 | `if site['project'].project_type == 'Residential']` | per-site user_can_raise_vendor_order check for Residential sites | n/a-data | SCM | |
| 318 | projects/purchases_views.py | _parse_record_submission | 295 | `'project_type': project_type,` | carried to _write_order_record | n/a-data | SCM | |
| 319 | projects/purchases_views.py | _parse_record_submission | 298 | `'unfrozen': unfrozen if project_type != 'Residential' else False,` | procurement-group frozen flag stored only for non-Residential orders | n/a-data | SCM | NEQ-RES |
| 320 | projects/purchases_views.py | _new_record_prefill | 314 | `if program is not None and program.program_type == 'OPEX':` | ?program= prefill ticks only OPEX tenders | excluded | SCM | |
| 321 | projects/purchases_views.py | _new_record_prefill | 319 | `.only('pk', 'project_type', 'program_id')` | column fetch | n/a-data | SCM | |
| 322 | projects/purchases_views.py | _new_record_prefill | 322 | `if project.project_type == 'OPEX' and project.program_id is not None:` | ?project= prefill also ticks site's tender for OPEX only | excluded | SCM | |
| 323 | projects/purchases_views.py | _new_record_context | 338 | `'record_types': RECORD_TYPES,` | Add PO/PI kind radio (Residential/RESCO) | excluded | SCM | |
| 324 | projects/purchases_views.py | _new_record_context | 344 | `'project_type': post.get('project_type', ''),` | re-render posted kind | n/a-data | SCM | |
| 325 | projects/qaqc_views.py | current_site_qaqc | 113 | `if project.project_type != 'OPEX': return None` | active QA/QC assignment for display (card, screen) | excluded | CLOSEOUT | |
| 326 | projects/qaqc_views.py | site_qaqc_card_context | 185 | `if project.project_type != 'OPEX': return {'show_qaqc_card': False, ...}` | QA/QC card on project_overview | excluded | CLOSEOUT | |
| 327 | projects/qaqc_views.py | qaqc_sites_for_engineer | 212 | `project__is_deleted=False, project__project_type='OPEX')` | engineer's "my QA/QC sites" list | excluded | CLOSEOUT | |
| 328 | projects/qaqc_views.py | _refuse_ineligible | 279 | `if project.project_type != 'OPEX' or project.is_deleted:` | refuses naming a QA/QC engineer on non-OPEX (writer, under lock) | excluded | CLOSEOUT | |
| 329 | projects/qaqc_views.py | _refuse_ineligible | 281 | `'A QA/QC engineer is assigned on RESCO sites only.')` | cont. (message) | n/a-display | CLOSEOUT | |
| 330 | projects/qaqc_views.py | site_qaqc | 469 | `if project.project_type != 'OPEX': raise Http404` | QA/QC assignment screen 404 | excluded | CLOSEOUT | |
| 331 | projects/qaqc_views.py | resolve_checklists_for_tasks | 582 | `ptype = project.project_type` | batch checklist resolver keyed on the PROJECT's type | excluded (no link matches 'CAPEX') | TASK | |
| 332 | projects/qaqc_views.py | resolve_checklists_for_tasks | 588 | `.filter(Q(template_task__phase__template__project_type=ptype) \| Q(project_type=ptype))` | links whose template's project_type == project's type; a CAPEX site on the OPEX template (template.project_type='OPEX') would match nothing | excluded | TASK | |
| 333 | projects/qaqc_views.py | resolve_checklists_for_tasks | 593 | `if tt is not None and tt.phase.template.project_type == ptype:` | by-code index (template_task.code) | excluded | TASK | |
| 334 | projects/qaqc_views.py | resolve_checklists_for_tasks | 595 | `if link.project_type == ptype:` | by-name fallback index | excluded | TASK | |
| 335 | projects/qaqc_views.py | site_checklist_points | 680 | `if project.project_type != 'OPEX': return []` | QA/QC visit checklist points for a site | excluded | CLOSEOUT | |
| 336 | projects/qaqc_views.py | qaqc_visit_card_context | 831 | `if project.project_type != 'OPEX': return {'show_qaqc_visit_card': False}` | QA/QC visit card on overview | excluded | CLOSEOUT | |
| 337 | projects/qaqc_views.py | last_visit_lines | 867 | `pks = [p.pk for p in projects if p.project_type == 'OPEX']` | dashboard "last QA/QC visit" line per project card | excluded | DASHBOARD/UI | |
| 338 | projects/qaqc_views.py | _visit_site | 1264 | `if project.project_type != 'OPEX': raise Http404` | every visit URL 404 on non-OPEX | excluded | CLOSEOUT | |
| 339 | projects/qaqc_views.py | _point_site | 1649 | `if project.project_type != 'OPEX': raise Http404` | punch-point loop URLs (rectify/re-verify) 404 on non-OPEX | excluded | CLOSEOUT | |
| 340 | projects/site_drawings_views.py | <module> | 61 | `STATE_NOT_OPEX = 'not_opex'` | design state constant for the drawings card | n/a-display | DESIGN | |
| 341 | projects/site_drawings_views.py | _design_state | 103 | `if project.project_type != 'OPEX':` | site drawings card design state (released / under revision) | excluded | DESIGN | |
| 342 | projects/site_drawings_views.py | _design_state | 104 | `return STATE_NOT_OPEX, None` | cont. | excluded | DESIGN | |
| 343 | projects/utils.py | generate_project_id | 683 | `def generate_project_id(project_type):` | auto project_id on Project.save() for non-explicit ids | included | OTHER | |
| 344 | projects/utils.py | generate_project_id | 693 | `'Residential': 'RES',` | prefix map | n/a-data | OTHER | |
| 345 | projects/utils.py | generate_project_id | 694 | `'OPEX': 'OPX',` | prefix map | n/a-data | OTHER | |
| 346 | projects/utils.py | generate_project_id | 695 | `'CAPEX': 'CAP',` | CAPEX ids HRP-CAP-YYYY-NNN | included | OTHER | |
| 347 | projects/utils.py | generate_project_id | 697 | `prefix = PREFIX_MAP[project_type]` | cont. | included | OTHER | |
| 348 | projects/utils.py | get_residential_template_task_names | 1318 | `template = resolve_active_task_template('Residential')` | Residential task-name picker source | excluded | TASK | |
| 349 | projects/utils.py | <module> | 1348 | `RESIDENTIAL_TEMPLATE_CODE = 'RESIDENTIAL'` | TaskTemplate.code of the Residential template (bootstrap check) | n/a-data | OTHER | |
| 350 | projects/utils.py | resolve_active_task_template | 1363 | `def resolve_active_task_template(project_type):` | activation template lookup: ACTIVE TaskTemplate by project_type (callers: attach_opex_template, resolve_residential_template, get_residential_template_task_names) | excluded (no ACTIVE CAPEX template → None) | OTHER | |
| 351 | projects/utils.py | resolve_active_task_template | 1375 | `.filter(project_type=project_type, status=TaskTemplate.ACTIVE)` | activation: one active template per project_type | excluded | OTHER | |
| 352 | projects/utils.py | seed_task_template_version | 1420 | `code, label, project_type, version_no, phases,` | seed signature (0067 Residential, 0075 OPEX, bootstrap) | n/a-data | OTHER | |
| 353 | projects/utils.py | seed_task_template_version | 1448 | `'project_type': project_type,` | written onto TaskTemplate | n/a-data | OTHER | |
| 354 | projects/utils.py | _residential_duration_resolver | 1524 | `TaskDurationTemplate.objects.filter(project_type='residential')` | Residential duration overrides (lowercase vocabulary, separate table) | excluded | OTHER | |
| 355 | projects/utils.py | resolve_residential_template | 1550 | `template = resolve_active_task_template('Residential')` | activation: Residential template | excluded | OTHER | |
| 356 | projects/utils.py | resolve_residential_template | 1571 | `project_type='Residential',` | bootstrap seed of Residential v1 | n/a-data | OTHER | |
| 357 | projects/utils.py | resolve_residential_template | 1581 | `template = resolve_active_task_template('Residential')` | re-read after bootstrap | excluded | OTHER | |
| 358 | projects/utils.py | attach_opex_template | 1707 | `def attach_opex_template(project):` | activation of non-Residential site (called from views.opex_site_activate, seed commands) | excluded (raises on CAPEX) | OTHER | |
| 359 | projects/utils.py | attach_opex_template | 1742 | `template = resolve_active_task_template(project.project_type)` | activation: resolves by the project's own type, so CAPEX looks for an ACTIVE 'CAPEX' template | excluded (none exists) | OTHER | |
| 360 | projects/utils.py | attach_opex_template | 1745 | `f"No ACTIVE task template for project_type "` | raises TaskTemplate.DoesNotExist for CAPEX today | excluded | OTHER | |
| 361 | projects/utils.py | attach_opex_template | 1746 | `f"'{project.project_type}'. Seed and activate one before activating a "` | cont. (message) | n/a-display | OTHER | |
| 362 | projects/utils.py | attach_opex_template | 1747 | `f"{project.project_type} site; a version is not created automatically "` | cont. (message) | n/a-display | OTHER | |
| 363 | projects/utils.py | attach_residential_template | 1800 | `def attach_residential_template(project):` | activation of Residential project (Residential template, finance assignee, PM pre-assign) | excluded | OTHER | |
| 364 | projects/management/commands/design_figure_snapshot.py | Command.handle | 84 | `.filter(is_deleted=False, program_type='OPEX')` | Which Programs the design-figure snapshot computes analytics over (read-only report command, mirrors design_quality_analytics "all RESCO tenders") | excluded | DESIGN |  |
| 365 | projects/management/commands/fixture_scmpilot_boq.py | Command.handle | 293 | `code__in=codes, project_type='OPEX', is_active=True` | Fixture loads OPEX catalogue BOQItemMaster rows for SCMPILOT sites (seed/demo data) | n/a-data | OTHER |  |
| 366 | projects/management/commands/fixture_scmpilot_boq.py | Command.handle | 308 | `if project.project_type != 'OPEX':` | Refuses to fixture a BOQ onto a non-OPEX project (seed/demo data) | excluded | OTHER |  |
| 367 | projects/management/commands/fixture_scmpilot_boq.py | Command.handle | 309 | `raise CommandError(f'{code} is {project.project_type}, not OPEX.')` | cont. of line 308 — error message | n/a-display | OTHER |  |
| 368 | projects/management/commands/flag_test_projects.py | Command.handle | 56 | `self.stdout.write(f'... {p.project_type} ...')` | Listing output only | n/a-display | OTHER |  |
| 369 | projects/management/commands/oneoff_ukru001_link_location_tasks.py | Command._validate | 165 | `if template.project_type != 'OPEX' or template.status != TaskTemplate.ACTIVE:` | One-off repair: refuses unless linking against the active OPEX TaskTemplate | excluded | OTHER |  |
| 370 | projects/management/commands/oneoff_ukru001_link_location_tasks.py | Command._validate | 167 | `f'({template.project_type}), not the active OPEX template')` | cont. of line 165 — error message | n/a-display | OTHER |  |
| 371 | projects/management/commands/oneoff_ukru001_link_location_tasks.py | Command._validate | 184 | `if project.project_type != 'OPEX':` | One-off repair: refuses tasks on non-OPEX projects | excluded | OTHER |  |
| 372 | projects/management/commands/oneoff_ukru001_link_location_tasks.py | Command._validate | 185 | `errors.append(f'... {project.project_type!r}, not OPEX')` | cont. of line 184 — error message | n/a-display | OTHER |  |
| 373 | projects/management/commands/revert_design_allocation_31oct.py | Command._site_problems | 271 | `if p.program_id != PROGRAM_ID or p.project_type != 'OPEX' or p.is_deleted:` | One-off revert: site must be OPEX under the named program | excluded | OTHER |  |
| 374 | projects/management/commands/seed_opex_installation_checklists.py | Command.handle | 540 | `TaskTemplate.objects.filter(project_type='OPEX', status=TaskTemplate.ACTIVE).first()` | Production data seed: the 7 installation checklists are linked ONLY to the active OPEX template's tasks (a CAPEX template would receive no checklist links from this command) | excluded | OTHER |  |
| 375 | projects/management/commands/seed_opex_installation_checklists.py | Command.handle | 577 | `template_task__code=spec['task_code'], template_task__phase__template__project_type='OPEX'` | Clash check for an existing ChecklistTaskLink, scoped to OPEX-template task codes | excluded | OTHER |  |
| 376 | projects/management/commands/seed_opex_test_data.py | Command._create_program | 337 | `'program_type': 'OPEX'` | seed/demo data | n/a-data | OTHER |  |
| 377 | projects/management/commands/seed_opex_test_data.py | Command._create_program | 351 | `f'Created {program.program_type} Program: ...'` | seed/demo data — log text | n/a-display | OTHER |  |
| 378 | projects/management/commands/seed_opex_test_data.py | Command._create_opex_sites | 361 | `from projects.views import create_opex_site` | seed/demo data via the real site-creation path | n/a-data | OTHER |  |
| 379 | projects/management/commands/seed_opex_test_data.py | Command._create_opex_sites | 366 | `site, form = create_opex_site(` | seed/demo data | n/a-data | OTHER |  |
| 380 | projects/management/commands/seed_opex_test_data.py | Command._create_opex_sites | 381 | `f'create_opex_site() refused {site_code!r}: ...'` | seed/demo data — error text | n/a-display | OTHER |  |
| 381 | projects/management/commands/seed_opex_test_data.py | Command._activate_opex_sites | 401 | `from projects.utils import attach_opex_template, record_transition` | seed/demo data | n/a-data | OTHER |  |
| 382 | projects/management/commands/seed_opex_test_data.py | Command._activate_opex_sites | 417 | `attach_opex_template(site)` | seed/demo data — attaches OPEX template | n/a-data | OTHER |  |
| 383 | projects/management/commands/seed_opex_test_data.py | Command._create_residential | 447 | `from projects.utils import attach_residential_template, record_transition` | seed/demo data | n/a-data | OTHER |  |
| 384 | projects/management/commands/seed_opex_test_data.py | Command._create_residential | 456 | `'project_type': 'Residential'` | seed/demo data | n/a-data | OTHER |  |
| 385 | projects/management/commands/seed_opex_test_data.py | Command._create_residential | 487 | `attach_residential_template(project)` | seed/demo data | n/a-data | OTHER |  |
| 386 | projects/management/commands/seed_opex_test_data.py | Command._seed_boqs | 646 | `is_active=True, project_type='Residential'` | seed/demo data — Residential catalogue rows | n/a-data | OTHER |  |
| 387 | projects/management/commands/seed_order_demo.py | Command._create_programs | 567 | `'program_type': 'OPEX'` | seed/demo data | n/a-data | OTHER |  |
| 388 | projects/management/commands/seed_order_demo.py | Command._create_programs | 579 | `f'Created {program.program_type} Program: ...'` | seed/demo data — log text | n/a-display | OTHER |  |
| 389 | projects/management/commands/seed_order_demo.py | Command._create_sites | 595 | `from projects.views import create_opex_site` | seed/demo data | n/a-data | OTHER |  |
| 390 | projects/management/commands/seed_order_demo.py | Command._create_sites | 601 | `site, form = create_opex_site(` | seed/demo data | n/a-data | OTHER |  |
| 391 | projects/management/commands/seed_order_demo.py | Command._create_sites | 615 | `raise CommandError(f'create_opex_site() refused ...'` | seed/demo data — error text | n/a-display | OTHER |  |
| 392 | projects/management/commands/seed_scm_handoff_data.py | Command._ensure_boq | 173 | `project_type='Residential')}` | seed/demo data — Residential catalogue rows | n/a-data | OTHER |  |
| 393 | projects/management/commands/seed_scm_handoff_data.py | Command.handle | 245 | `from projects.views import create_opex_site` | seed/demo data | n/a-data | OTHER |  |
| 394 | projects/management/commands/seed_scm_handoff_data.py | Command.handle | 307 | `'program_type': 'OPEX'` | seed/demo data | n/a-data | OTHER |  |
| 395 | projects/management/commands/seed_scm_handoff_data.py | Command.handle | 329 | `site, site_form = create_opex_site(` | seed/demo data | n/a-data | OTHER |  |
| 396 | projects/management/commands/seed_scm_handoff_data.py | Command.handle | 343 | `raise CommandError(f'create_opex_site() refused ...'` | seed/demo data — error text | n/a-display | OTHER |  |
| 397 | projects/management/commands/seed_scm_pilot.py | Command._program | 454 | `'program_type': 'OPEX'` | seed/demo data | n/a-data | OTHER |  |
| 398 | projects/management/commands/seed_scm_pilot.py | Command._program | 466 | `f'Created {program.program_type} Program: ...'` | seed/demo data — log text | n/a-display | OTHER |  |
| 399 | projects/management/commands/seed_scm_pilot.py | Command._sites | 483 | `from projects.views import create_opex_site` | seed/demo data | n/a-data | OTHER |  |
| 400 | projects/management/commands/seed_scm_pilot.py | Command._sites | 493 | `site, form = create_opex_site(` | seed/demo data | n/a-data | OTHER |  |
| 401 | projects/management/commands/seed_scm_pilot.py | Command._sites | 508 | `f'create_opex_site() refused ...'` | seed/demo data — error text | n/a-display | OTHER |  |
| 402 | projects/management/commands/seed_scm_pilot.py | Command._report | 904 | `% (program.name, program.program_type, ...)` | seed/demo data — report text | n/a-display | OTHER |  |
| 403 | projects/management/commands/seed_walkthrough.py | Command._program | 766 | `'program_type': 'OPEX'` | seed/demo data | n/a-data | OTHER |  |
| 404 | projects/management/commands/seed_walkthrough.py | Command._area_procurement | 1231 | `'program': [], 'project_type': 'OPEX'` | seed/demo data — POST body to purchases_new (kind radio = OPEX) | n/a-data | OTHER |  |
| 405 | projects/management/commands/seed_walkthrough.py | Command._area_residential.create | 1615 | `'project_type': 'Residential'` | seed/demo data | n/a-data | OTHER |  |
| 406 | projects/management/commands/seed_walkthrough.py | Command._area_fresh | 1796 | `'project_type': 'Residential'` | seed/demo data | n/a-data | OTHER |  |
| 407 | projects/management/commands/seed_walkthrough.py | Command._area_approvals | 1882 | `'site': [], 'program': [], 'project_type': 'OPEX'` | seed/demo data — POST body to approvals raise | n/a-data | OTHER |  |
| 408 | projects/management/commands/wipe_demo_2026_10_14.py | Command._check_a | 520 | `if (p.project_id, p.project_type, p.status, p.is_deleted) != (code, 'OPEX', 'Active', False):` | One-off wipe precondition check | excluded | OTHER |  |
| 409 | projects/management/commands/wipe_demo_2026_10_14.py | Command._check_a | 521 | `failures.append(f'a: Project {pk} is {p.project_id}/{p.project_type}/'` | cont. of line 520 — error text | n/a-display | OTHER |  |
| 410 | projects/templates/dashboard/ceo.html | block:content | 1205 | `{{ p.get_project_type_display }}` | dashboard_ceo project card info row — label only | n/a-display | DASHBOARD/UI |  |
| 411 | projects/templates/dashboard/design.html | block:content | 171 | `RESCO BOQ catalogue` (inside `{% if is_design_head %}`) | dashboard_design — label of the link to design_boq_catalogue; no type condition | n/a-display | DASHBOARD/UI |  |
| 412 | projects/templates/dashboard/pm.html | block:content | 96 | `{% if dp.project_type == 'Residential' %}` … `{% else %}` form → `opex_site_activate` | dashboard_pm Draft strip Activate control: Residential opens designer modal (project_activate); everything else POSTs to opex_site_activate (which refuses only Residential, then calls attach_opex_template) | included (CAPEX gets the opex_site_activate form) | OTHER | ELSE-OF-RES |
| 413 | projects/templates/projects/admin/boq_items.html | block:content | 18 | `Residential BOQ. <strong>RESCO</strong> items are not pre-populated...` | admin_boq_items help text only | n/a-display | BOQ |  |
| 414 | projects/templates/projects/admin/boq_items.html | block:content | 19 | `offered in the RESCO BOQ picker...` | cont. of line 18 — help text | n/a-display | BOQ |  |
| 415 | projects/templates/projects/admin/boq_items.html | block:content | 76 | `{% if item.project_type == 'OPEX' %}bg-blue-100 text-blue-700` | admin_boq_items type badge colour | n/a-display | BOQ |  |
| 416 | projects/templates/projects/admin/boq_items.html | block:content | 77 | `{% elif item.project_type == 'CAPEX' %}bg-amber-100 text-amber-700` | admin_boq_items type badge colour — explicit CAPEX colour | n/a-display | BOQ |  |
| 417 | projects/templates/projects/admin/boq_items.html | block:content | 78 | `{% else %}bg-gray-100 ...{{ item.get_project_type_display }}` | cont. — badge label | n/a-display | BOQ |  |
| 418 | projects/templates/projects/admin/checklist_edit.html | block:content | 162 | `{{ link.get_project_type_display }}` | admin_checklist_edit linked-task label | n/a-display | DASHBOARD/UI |  |
| 419 | projects/templates/projects/admin/checklist_edit.html | block:content | 195 | `{% for project_type, type_label, tasks in task_name_pairs %}` | admin_checklist_edit task picker optgroups from views._checklist_task_name_choices(), which loops Project.PROJECT_TYPE_CHOICES and skips a type with no active TaskTemplate (resolve_active_task_template) | excluded while no active CAPEX TaskTemplate exists; included automatically once one does | TASK |  |
| 420 | projects/templates/projects/admin/checklists.html | block:content | 54 | `{{ link.get_project_type_display }}` | admin_checklists list label | n/a-display | DASHBOARD/UI |  |
| 421 | projects/templates/projects/admin/pm_handover.html | block:content | 47 | `({{ p.get_program_type_display }})` | admin_pm_handover program dropdown label | n/a-display | PROGRAM |  |
| 422 | projects/templates/projects/admin/projects_list.html | block:content | 59 | `{{ project.get_project_type_display }}` | admin_project_list type column | n/a-display | DASHBOARD/UI |  |
| 423 | projects/templates/projects/admin/user_edit.html | block:content | 108 | `QA/QC — may approve or reject submitted RESCO work...` | admin_user_edit help text for QA/QC grant (no condition) | n/a-display | APPROVAL |  |
| 424 | projects/templates/projects/admin/user_edit.html | block:content | 146 | `Design QC — reviews RESCO designs before the Design Head...` | admin_user_edit help text for Design QC flag (no condition) | n/a-display | DESIGN |  |
| 425 | projects/templates/projects/boq_correct.html | block:content | 173 | `Not in the RESCO catalogue` | boq_correct off-catalogue row label | n/a-display | BOQ |  |
| 426 | projects/templates/projects/design/boq_catalogue.html | block:title | 20 | `RESCO BOQ Catalogue — SolarPMS` | design_boq_catalogue page title | n/a-display | BOQ |  |
| 427 | projects/templates/projects/design/boq_catalogue.html | block:content | 26 | `RESCO BOQ Catalogue` | design_boq_catalogue heading | n/a-display | BOQ |  |
| 428 | projects/templates/projects/design/boq_catalogue.html | block:content | 28 | `Every item the RESCO BOQ picker offers...` | design_boq_catalogue help text | n/a-display | BOQ |  |
| 429 | projects/templates/projects/design/boq_catalogue.html | block:content | 33 | `{{ total_count }} RESCO items.` | design_boq_catalogue count label | n/a-display | BOQ |  |
| 430 | projects/templates/projects/design/boq_catalogue.html | block:content | 124 | `confirm('Deactivate ...? It stops being offered in the RESCO BOQ picker...')` | design_boq_catalogue deactivate confirm text | n/a-display | BOQ |  |
| 431 | projects/templates/projects/design/boq_catalogue_form.html | block:content | 24 | `Back to RESCO BOQ Catalogue` | design_boq_catalogue_create/edit back link label | n/a-display | BOQ |  |
| 432 | projects/templates/projects/design/qc_review.html | block:content | 264 | `— not in the RESCO catalogue` | design_qc_review off-catalogue row label | n/a-display | DESIGN |  |
| 433 | projects/templates/projects/design/quality_analytics.html | block:content | 59 | `{% if program %}...{% else %} — all RESCO tenders{% endif %}` | design_quality_analytics heading label | n/a-display | DESIGN |  |
| 434 | projects/templates/projects/design/quality_analytics.html | block:content | 87 | `All RESCO tenders` | design_quality_analytics tender-filter chip label | n/a-display | DESIGN |  |
| 435 | projects/templates/projects/email/eod_digest.html | block:<top> | 259 | `RESCO and CAPEX design work is tracked separately.` | send_eod_digest email footnote text | n/a-display | NOTIFY |  |
| 436 | projects/templates/projects/email/eod_digest.txt | block:<top> | 28 | `RESCO and CAPEX design work is tracked separately.` | send_eod_digest plain-text footnote | n/a-display | NOTIFY |  |
| 437 | projects/templates/projects/opex_boq_entry.html | block:content | 151 | `Not in the RESCO catalogue` | opex_boq_entry off-catalogue label | n/a-display | BOQ |  |
| 438 | projects/templates/projects/opex_boq_entry.html | block:content | 391 | `'every RESCO BOQ, so it cannot be removed">Req</span>'` | opex_boq_entry mandatory-item badge tooltip (JS string) | n/a-display | BOQ |  |
| 439 | projects/templates/projects/opex_boq_upload.html | block:content | 95 | `An item code that is not in the RESCO catalogue rejects the whole file.` | opex_boq_upload help text | n/a-display | BOQ |  |
| 440 | projects/templates/projects/opex_boq_upload.html | block:content | 155 | `...so every RESCO BOQ carries them.` | opex_boq_upload mandatory-items note | n/a-display | BOQ |  |
| 441 | projects/templates/projects/opex_site_form.html | block:content | 8 | `Add RESCO Site` | opex_site_create heading | n/a-display | PROGRAM |  |
| 442 | projects/templates/projects/partials/_task_row_submit_button.html | block:<top> | 22 | `{% if self_certify %}...{% else %}title="...a RESCO task is completed by its approver..."` | Submit-for-approval button tooltip; included by _task_row_approval.html only under `{% if two_step_completion and not task.is_mirror %}` (rendered via project_overview / _phase_list_two_step) — the type gate is upstream in two_step_completion | n/a-display | APPROVAL |  |
| 443 | projects/templates/projects/partials/vendor_order_picker.html | block:<top> | 86 | `{% if picker_residential %}<span ...>RESCO · {{ t.program.name }}</span>{% endif %}` | Vendor-order site picker badge (order_views._picker_context, picker_residential = include_residential on purchases_new); label only, tenders listed upstream | n/a-display | SCM |  |
| 444 | projects/templates/projects/partials/vendor_order_picker_script.html | block:<top> | 60 | `/* A RESCO site is searchable once its tender is ticked...` | JS comment in picker script (purchases_new, vendor_order_group_form) — no gate | n/a-display | SCM |  |
| 445 | projects/templates/projects/partials/vendor_order_picker_script.html | block:<top> | 193 | `/* The kinds of what is added: {res: n, resco: n}. Ticked tenders count as RESCO. */` | JS comment — picker counts ticked tenders as RESCO kind (no CAPEX kind in the counter) | n/a-display | SCM |  |
| 446 | projects/templates/projects/program_detail.html | block:content | 10 | `{{ program.get_program_type_display }}` | program_detail type badge | n/a-display | PROGRAM |  |
| 447 | projects/templates/projects/program_detail.html | block:content | 77 | `{% if can_edit and program.program_type == 'OPEX' %}` | program_detail "Bulk Upload Sites" / "+ Add Site" buttons (opex_site_bulk_upload, opex_site_create) | excluded (a CAPEX Program shows no add-site buttons) | PROGRAM |  |
| 448 | projects/templates/projects/program_detail.html | block:content | 90 | `{% if program.program_type == 'OPEX' %}<th>Site Code</th>{% endif %}` | program_detail Site Code column header | excluded | PROGRAM |  |
| 449 | projects/templates/projects/program_detail.html | block:content | 100 | `{% if program.program_type == 'OPEX' %}<td>{{ s.site_code }}</td>{% endif %}` | program_detail Site Code cell | excluded | PROGRAM |  |
| 450 | projects/templates/projects/program_form.html | block:content | 26 | `{{ form.program_type }}` | program_create/program_edit type select (choices OPEX/CAPEX from Program.PROGRAM_TYPE_CHOICES) | included (CAPEX selectable) | PROGRAM |  |
| 451 | projects/templates/projects/program_form.html | block:content | 27 | `{% for e in form.program_type.errors %}` | program_form error display | n/a-display | PROGRAM |  |
| 452 | projects/templates/projects/program_form.html | block:content | 93 | `// Show the tender-code field only for RESCO...` | JS comment | n/a-display | PROGRAM |  |
| 453 | projects/templates/projects/program_form.html | block:content | 94 | `// still validates by program_type regardless...` | JS comment | n/a-display | PROGRAM |  |
| 454 | projects/templates/projects/program_form.html | block:content | 100 | `document.querySelector('[name="program_type"]')` | JS: reads type select to toggle short_tender_code field | n/a-display | PROGRAM |  |
| 455 | projects/templates/projects/program_form.html | block:content | 103 | `const isOpex = (typeSel ? typeSel.value : '') === 'OPEX';` | JS: short_tender_code field shown only for OPEX | excluded (field hidden for CAPEX) | PROGRAM |  |
| 456 | projects/templates/projects/program_list.html | block:content | 35 | `{{ p.get_program_type_display }}` | program_list type badge | n/a-display | PROGRAM |  |
| 457 | projects/templates/projects/project_detail.html | block:content | 38 | `{{ project.get_project_type_display }}` | Label in project_detail.html — no view renders this template (only a comment reference in design_views.sync_design_mirror docstring; tests_number_input.py:346 calls it dead) | n/a-display | DASHBOARD/UI |  |
| 458 | projects/templates/projects/project_form.html | block:content | 79 | `<select name="project_type"` | project_create type select (Create only) | n/a-display | OTHER |  |
| 459 | projects/templates/projects/project_form.html | block:content | 80 | `{% if form.project_type.errors %} is-invalid` | cont. — error styling | n/a-display | OTHER |  |
| 460 | projects/templates/projects/project_form.html | block:content | 82 | `{% for val, label in form.fields.project_type.choices %}` | Options = ProjectCreateForm choices, which drop only 'OPEX' (forms.ProjectCreateForm.__init__ `if value != 'OPEX'`) — Residential and CAPEX offered for standalone create | included (CAPEX can be created standalone here) | OTHER |  |
| 461 | projects/templates/projects/project_form.html | block:content | 83 | `{% if form.project_type.value == val %}selected` | cont. — selected option | n/a-display | OTHER |  |
| 462 | projects/templates/projects/project_form.html | block:content | 86 | `{% for e in form.project_type.errors %}` | cont. — errors | n/a-display | OTHER |  |
| 463 | projects/templates/projects/project_form.html | block:content | 88 | `value="{{ project.get_project_type_display }}" disabled` | project_edit read-only type | n/a-display | OTHER |  |
| 464 | projects/templates/projects/project_list.html | block:content | 34 | `{{ project.get_project_type_display }}` | Label in project_list.html — views.project_list only redirects (admin_project_list / dashboard_ceo / dashboard_pm); no render of this template found | n/a-display | DASHBOARD/UI |  |
| 465 | projects/templates/projects/project_overview.html | block:content | 116 | `{% if project.project_type == 'Residential' %}` … `{% else %}` form → `opex_site_activate` | project_overview Draft Activate button: Residential → project_activate modal; all others → opex_site_activate (refuses Residential only; CAPEX gets status Active + attach_opex_template) | included (CAPEX activated via opex_site_activate) | OTHER | ELSE-OF-RES |
| 466 | projects/templates/projects/project_overview.html | block:content | 152 | `{{ project.get_project_type_display }}` | project_overview info pill | n/a-display | DASHBOARD/UI |  |
| 467 | projects/templates/projects/project_overview.html | block:content | 300 | `{% if project.project_type == 'Residential' %}` (outer of role check) | project_overview Payment Milestones card | excluded | DASHBOARD/UI |  |
| 468 | projects/templates/projects/project_overview.html | block:content | 732 | `?tab={{ project.project_type\|urlencode }}` | Vendor-orders card link to payment_queue tab; payment_views.payment_queue shows the CAPEX tab only if a CAPEX VendorOrder or CAPEX contractor-bill payment exists, else falls back to the first tab (Residential) | included (link renders; lands on Residential tab when CAPEX tab hidden) | SCM |  |
| 469 | projects/templates/projects/project_overview.html | block:content | 1284 | `{% if is_assigned_pm and project.project_type == 'OPEX' %}` | Phase-rule "Starting out of sequence" modal (phaseRuleStartModal) for the PM; rows get data-phase-rules from views._attach_phase_rule_flags | excluded | PHASE |  |
| 470 | projects/templates/projects/purchases_new.html | block:content | 94 | `...is this PO / PI Residential or RESCO?` | purchases_new kind question label | n/a-display | SCM |  |
| 471 | projects/templates/projects/purchases_new.html | block:content | 98 | `type="radio" name="project_type" value="{{ value }}"` (loop over `record_types`) | purchases_new kind radios = purchases_views.RECORD_TYPES = [('Residential',..),('OPEX','RESCO')] — no CAPEX option | excluded | SCM |  |
| 472 | projects/templates/projects/purchases_new.html | block:content | 99 | `{% if form.project_type == value %} checked` | cont. — radio checked state | n/a-display | SCM |  |
| 473 | projects/templates/projects/purchases_new.html | block:content | 190 | `problems.push('...against Residential projects or against RESCO tenders and sites — not both.')` | Client-side validation message | n/a-display | SCM |  |
| 474 | projects/templates/projects/purchases_new.html | block:content | 192 | `problems.push('...choose whether this PO / PI is Residential or RESCO.')` | Client-side validation message | n/a-display | SCM |  |
| 475 | projects/templates/projects/purchases_workspace.html | block:content | 72 | `{% if row.order.project_type == 'OPEX' %}RESCO{% else %}{{ row.order.project_type }}{% endif %}` | purchases_workspace order row type label (CAPEX prints raw "CAPEX") | n/a-display | SCM | ELSE-OF-OPEX |
| 476 | projects/templates/projects/reports/ceo_daily_report.html | block:content | 158 | `RESCO and CAPEX design work is` | ceo_daily_report footnote | n/a-display | DASHBOARD/UI |  |
| 477 | projects/templates/projects/reports/ceo_daily_report.html | block:content | 163 | `...on RESCO sites, submitted by them and approved by a reviewer.` | ceo_daily_report footnote describing Done Today (two-step) | n/a-display | DASHBOARD/UI |  |
| 478 | projects/templates/projects/subadmin/departments.html | block:content | 306 | `Design QC — reviews RESCO designs before the Design Head...` | subadmin_departments help text | n/a-display | DESIGN |  |
| 479 | projects/templates/projects/subadmin/projects.html | block:content | 43 | `{% if project.project_type == 'residential' %}bg-blue-50` | subadmin_projects badge colour — compares Project.project_type to lowercase 'residential', which is not a Project value ('Residential'), so never true | n/a-display | DASHBOARD/UI |  |
| 480 | projects/templates/projects/subadmin/projects.html | block:content | 44 | `{% elif project.project_type == 'commercial_opex' %}` | cont. — 'commercial_opex' is not a Project.PROJECT_TYPE_CHOICES value, never true; every type gets the yellow else badge | n/a-display | DASHBOARD/UI |  |
| 481 | projects/templates/projects/subadmin/projects.html | block:content | 46 | `{{ project.get_project_type_display }}` | subadmin_projects badge label (unassigned table) | n/a-display | DASHBOARD/UI |  |
| 482 | projects/templates/projects/subadmin/projects.html | block:content | 115 | `{% if project.project_type == 'residential' %}bg-blue-50` | Same dead comparison, assigned table | n/a-display | DASHBOARD/UI |  |
| 483 | projects/templates/projects/subadmin/projects.html | block:content | 116 | `{% elif project.project_type == 'commercial_opex' %}` | cont. — dead comparison | n/a-display | DASHBOARD/UI |  |
| 484 | projects/templates/projects/subadmin/projects.html | block:content | 118 | `{{ project.get_project_type_display }}` | subadmin_projects badge label (assigned table) | n/a-display | DASHBOARD/UI |  |
| 485 | projects/templates/projects/vendor_order_detail.html | block:content | 9 | `{{ order.project_type }}` | vendor_order_detail subtitle — prints raw stored value ("OPEX", not "RESCO") | n/a-display | SCM |  |
| 486 | projects/templates/projects/vendor_order_detail.html | block:content | 45 | `<dd>{{ order.project_type }}</dd>` | vendor_order_detail type field — raw stored value | n/a-display | SCM |  |

---

## C. OPEX template inventory

Source: the active OPEX `TaskTemplate` (id 6) in `solarpms_local`, SQL C1/C2 (raw output in Appendix 2). It matches migration 0075's `build_opex_phases()` literal row for row.

### C.1 Phases

| sort | phase code | label | tasks |
|---|---|---|---|
| 1 | DESIGN | Design | 1 |
| 2 | APPROVALS_PRE_INSTALLATION | Approvals (Pre-Installation) | 2 |
| 3 | PROCUREMENT_DELIVERY | Procurement & Delivery | 4 |
| 4 | INSTALLATION | Installation | 9 |
| 5 | TESTING_COMMISSIONING | Testing & Commissioning | 2 |
| 6 | APPROVALS_POST_INSTALLATION | Approvals (Post-Installation) | 1 |
| 7 | CLOSEOUT | Closeout | 4 |

**7 phases, 23 tasks, 8 mirrors, 0 payment milestones.** All tasks are `Internal` with `duration_days=1`.

### C.2 Tasks

- **Two-step:** there is no stored flag (A.4). The rule is `project.project_type == 'OPEX' and not task.is_mirror` (rows 52, 55; `_approval_preconditions` row 57 plus its `is_mirror` refusal). "Y" means two-step applies on an OPEX site.
- **Checklist:** a `ChecklistTaskLink` keyed to this code on an OPEX-typed template (SQL C2 `links_by_code`).

| # | code | name | phase | role | two-step | is_payment_milestone | checklist | mirror |
|---|---|---|---|---|---|---|---|---|
| 1 | DESIGN | Design | DESIGN | Design | N (mirror) | N | N | **Y** |
| 2 | NET_METERING_APPROVAL | Net Metering Approval | APPROVALS_PRE_INSTALLATION | PM | Y | N | N | N |
| 3 | CEIG_APPROVAL | CEIG Approval | APPROVALS_PRE_INSTALLATION | PM | Y | N | N | N |
| 4 | DELIVERY_SOLAR_PANELS | Delivery — Solar Panels | PROCUREMENT_DELIVERY | SCM | N (mirror) | N | N | **Y** |
| 5 | DELIVERY_INVERTERS | Delivery — Inverters | PROCUREMENT_DELIVERY | SCM | N (mirror) | N | N | **Y** |
| 6 | DELIVERY_BOS_KIT | Delivery — BOS Kit | PROCUREMENT_DELIVERY | SCM | N (mirror) | N | N | **Y** |
| 7 | DELIVERY_MMS | Delivery — MMS | PROCUREMENT_DELIVERY | SCM | N (mirror) | N | N | **Y** |
| 8 | CIVIL_WORK_AND_MMS_INSTALLATION | Civil Work and MMS Installation | INSTALLATION | Site Engineer | Y | N | Y (OPEX-INST-CIVIL-MMS v1) | N |
| 9 | MODULE_INSTALLATION | Module Installation | INSTALLATION | Site Engineer | Y | N | Y (OPEX-INST-MODULE v1) | N |
| 10 | LA_AND_EARTHING_INSTALLATION | LA and Earthing Installation | INSTALLATION | Site Engineer | Y | N | Y (OPEX-INST-LA-EARTHING v1) | N |
| 11 | DC_CABLE_LAYING_WITH_CONDUIT | DC Cable Laying with Conduit | INSTALLATION | Site Engineer | Y | N | Y (OPEX-INST-DC-CABLE v1) | N |
| 12 | DCDB_AND_ACDB_INSTALLATION | DCDB and ACDB Installation | INSTALLATION | Site Engineer | Y | N | Y (OPEX-INST-DCDB-ACDB v1) | N |
| 13 | INVERTER_INSTALLATION | Inverter Installation | INSTALLATION | Site Engineer | Y | N | Y (OPEX-INST-INVERTER v1) | N |
| 14 | AC_CABLE_LAYING | AC Cable Laying | INSTALLATION | Site Engineer | Y | N | Y (OPEX-INST-AC-CABLE v1) | N |
| 15 | RMS_INSTALLATION | RMS Installation | INSTALLATION | Site Engineer | Y | N | N | N |
| 16 | SOLAR_GENERATION_METER_INSTALLATION | Solar Generation Meter Installation | INSTALLATION | Site Engineer | Y | N | N | N |
| 17 | TESTING_COMMISSIONING | Testing & Commissioning | TESTING_COMMISSIONING | Site Engineer | Y | N | N | N |
| 18 | NET_METER_INSTALLATION | Net Meter Installation | TESTING_COMMISSIONING | Site Engineer | Y | N | N | N |
| 19 | POST_INSTALLATION_APPROVALS | Post-Installation Approvals | APPROVALS_POST_INSTALLATION | PM | Y | N | N | N |
| 20 | COD | COD | CLOSEOUT | PM | N (mirror) | N | N | **Y** |
| 21 | COMPLETION_CERTIFICATES_PAPERWORK | Completion Certificates (Paperwork) | CLOSEOUT | Project Coordinator | Y | N | N | N |
| 22 | AS_BUILT_DRAWINGS | As-Built Drawings | CLOSEOUT | Design | N (mirror) | N | N | **Y** |
| 23 | HOTO | HOTO | CLOSEOUT | PM | N (mirror) | N | N | **Y** |

### C.3 Mirrors: the hook that completes each one, and the gate on that hook

Humans can never write a mirror. Rung 0 of `views._apply_task_status_change()` (`if task.is_mirror:` refusal) and `permissions.user_can_mark_task_not_applicable()` (refuses mirrors) both block it. A mirror therefore completes only through its hook.

| Mirror code(s) | Hook (writer) | Callers | Type gate on the path | Table B row |
|---|---|---|---|---|
| DESIGN | `design_views.sync_design_mirror()` → `apply_mirror_status()` | `design_views.apply_design_status()` (every design status move); `utils.attach_opex_template()` (activation reconcile, reads `project.design_assignment`) | Every view that creates or moves a `DesignAssignment` opens with `project = _opex_site(project_id)`, and `_opex_site` does `if project.project_type != 'OPEX': raise Http404('Design workflow applies to RESCO sites only.')` | 100 (guard); the 36 `_opex_site(project_id)` call-site rows are 104–145 (`design_views`) |
| DELIVERY_SOLAR_PANELS, DELIVERY_INVERTERS, DELIVERY_BOS_KIT, DELIVERY_MMS | `design_views.sync_delivery_mirrors()` | `models.recalculate_dc_status()` (GRN path); `views.create_delivery_challan()` | **None.** `create_delivery_challan` is `@role_required(['SCM'])` + `_active_project()`, with no `project_type` reference (lines 13656–13985 searched). `recalculate_dc_status` has none either. Inside `sync_delivery_mirrors`, `!= 'OPEX'` only picks a log branch when no mirrors exist | 102 (log branch only) |
| COD | `design_views.sync_cod_mirror()` | `cod_views.record_cod()`, `cod_views.withdraw_cod()` | `cod_views.record_refusal`: `if project.project_type != 'OPEX' or project.is_deleted: raise CodRefused(COD_OPEX_ONLY)`; `_cod_site` and `cod_record` 404 on non-OPEX; `cod_card_context` hides the card | 223, 225, 226, 227 |
| AS_BUILT_DRAWINGS | **no writer** | — | — (`templatetags/mirror_tags.py` docstring: "AS_BUILT_DRAWINGS and HOTO have no writer and therefore no constant anywhere"; `MIRROR_SOURCE_TEXT`: "Will update from the As-Built record (not built yet).") | — |
| HOTO | **no writer** | — | — (same module: "Will update from Final Acceptance (not built yet).") | — |

```python
# projects/design_views.py, apply_design_status()   — the design hook, no type test
        sync_design_mirror(assignment.project, assignment.status, actor)
# projects/models.py, recalculate_dc_status()        — the GRN hook, no type test
        sync_delivery_mirrors(challan.project)
# projects/views.py, create_delivery_challan()       — the DC-create hook, no type test
        sync_delivery_mirrors(project)
# projects/cod_views.py
COD_OPEX_ONLY = 'COD is recorded on RESCO sites only.'
    if project.project_type != 'OPEX' or project.is_deleted:
        raise CodRefused(COD_OPEX_ONLY)
```

No completion gate besides the mirror rung and the two-step rung: a search of `_apply_task_status_change` (lines 6532–6887) for `checklist|punch` finds no code line. Checklists and punch points do not refuse Done there.

### C.4 Tasks that would never complete on CAPEX if only the TASK category were unlocked

"TASK unlocked" here means CAPEX gets the 23 template tasks and the four task actions (which B.2 shows are already open to CAPEX), with every other table B gate left as it is.

| Task | Why it never completes | Gate (table B row) |
|---|---|---|
| **DESIGN** (mirror) | No `DesignAssignment` can exist for a CAPEX site: every design view 404s. The hook and the activation reconcile then have nothing to read, and rung 0 refuses human writes. Local DB: 0 design assignments on CAPEX (F10). | 100 (`design_views._opex_site`) |
| **COD** (mirror) | `record_cod` / `withdraw_cod` refuse non-OPEX, so `sync_cod_mirror` is never called. | 223, 226, 227 (`cod_views`) |
| **AS_BUILT_DRAWINGS** (mirror) | No writer exists for any project type. Stuck on OPEX too. | none (no hook) |
| **HOTO** (mirror) | No writer exists for any project type. Stuck on OPEX too. | none (no hook) |

**Not stuck, per the code:**
- The four **delivery mirrors**. Their hooks have no type gate, so they derive from CAPEX challans and GRNs as on OPEX.
- The **15 non-mirror tasks**. They complete through the one-step path: the two-step Done rung is `project.project_type == 'OPEX'` (row 55), so on CAPEX a direct move to Done is not refused. They complete *without* submit/approve, and on contractor bills they count as complete on Done alone (row 218).

**Prerequisite, recorded separately:** "TASK unlocked" alone does not give a CAPEX project any tasks. Activation still raises (A.3, row 33/359) until a template resolves for `project_type='CAPEX'`.

---

## D. Code-keyed lookups

For each lookup: what it matches on, and what happens if a second template carries the same codes.

| Lookup | Matches on | Second template with the same codes |
|---|---|---|
| **`DC_CATEGORY_TO_MIRROR_CODE`** (`models.py`) via `design_views._delivery_mirror_tasks()`, `delivery_detail_for_task()`, `models.first_component_received()` (category only) | **code alone**, scoped to one project: `Task.objects.filter(phase__project=project, is_mirror=True, template_task__code__in=DELIVERY_MIRROR_CODES)`. No template and no type in the filter. | Tasks built from any template carrying those codes with `is_mirror=True` resolve the same way, on any project type. Codes are unique only per phase (A.4), but the filter is per project, so templates do not collide. A project built from one template sees only its own rows. |
| **Design mirror** `design_views._design_mirror_task()` / `sync_design_mirror()` | **code alone** (`template_task__code=DESIGN_MIRROR_CODE` = `'DESIGN'`) + `is_mirror=True`, scoped to one project. Its docstring explains why the template/type join is "deliberately NOT added". | Same as above. The Residential template already carries code `DESIGN` (SQL D1) and is excluded only by `is_mirror=True` (its row is `False`). The lookup works for any type; the *hook* is OPEX-only through `_opex_site` (row 100). |
| **COD mirror** `design_views.cod_mirror_task()` / `sync_cod_mirror()`; also `cod_views` `TESTING_COMMISSIONING_CODE` lookup | **code alone** (`'COD'`, `'TESTING_COMMISSIONING'`) + `is_mirror=True` for COD, scoped to one project | Lookup resolves on any type; the writers refuse by **project type** (`!= 'OPEX'`, rows 223/226/227). |
| **Checklist → task** `views._checklist_task_link_for()`, its batch twin `qaqc_views.resolve_checklists_for_tasks()`, and the duplicate check in `views.admin_checklist_link_add()` | **code + the template's `project_type` equal to the project's**: `filter(template_task__code=code, template_task__phase__template__project_type=project.project_type)`. Fallback: `filter(task_name=task.task_name, project_type=project.project_type)`. | (a) A second template with `project_type='CAPEX'` resolves **none** of the 7 OPEX links; CAPEX needs its own links. The constraints allow them: the partial unique on `template_task`, and `unique_together('task_name','project_type')` differs by type. (b) CAPEX projects built from the OPEX-typed template itself also resolve **nothing**: the join compares `'OPEX'` to `'CAPEX'`, and the name fallback filters `project_type='CAPEX'`. (c) Two templates of the *same* type sharing a code: `.first()` picks under `ChecklistTaskLink.Meta.ordering = ['project_type','task_name']`. Pinned by `tests_checklist_snapshot::ChecklistResolutionTests::test_a_different_project_type_does_not_match_the_link` (§G). |
| **`get_opex_mandatory_items()`** (`models.py`) | **catalogue type only**: `BOQItemMaster.objects.filter(is_active=True, project_type='OPEX', is_mandatory=True)`. It takes no project or template argument. | Template codes are irrelevant to it. Every caller is behind an OPEX gate: `views.opex_boq_entry` / `opex_boq_upload` (`!= 'OPEX'` → 404) and `design_views.design_boq_complete` (`_opex_site`). A CAPEX BOQ goes to the Residential BOQ screen (`boq_detail`, row 59). |
| *(also code-keyed)* **Phase rules** `phase_rules.RULES` / `SiteRuleState._load()` | **(template code, version_no)**: `RULES = {('OPEX', 1): (...)}`, read from `template_task__phase__template__code/version_no` | A second template with a different code gets **no rules**. If it reused code `'OPEX'` (impossible while v1 is active, A.4) it would. Independently, `phase_rule_warnings`, `start_warnings` and `dc_creation_warnings` return `[]` unless `project_type == 'OPEX'` (rows 299–302). |
| *(also code-keyed)* `tender_stages` delivery-code lookup, `mirror_tags.MIRROR_SOURCE_TEXT` | code alone | Presentation and CEO tender stages; the tender scope is `CEO_TENDER_TYPES = ['OPEX']` (§B rows, DASHBOARD/UI). |

**What the evidence implies for "same template" (consequences, not recommendations):**
- Path 1 is a new `TaskTemplate(project_type='CAPEX')`. It must use a code other than `'OPEX'` (`uniq_active_task_template_per_code`). As a result `phase_rules.RULES` does not apply and the 7 checklist links do not apply.
- Path 2 resolves CAPEX to the OPEX template row. It changes `resolve_active_task_template()` / `attach_opex_template()` (rows 359; `phase_rules` row 298 has the same lookup). The checklist lookups then return nothing for CAPEX (D, row "Checklist") unless their `project_type` join changes.
- Either path leaves every `== 'OPEX'` / `!= 'OPEX'` gate in table B (176 rows marked "excluded") in place.

---

## E. Program hierarchy and intake

### E.1 Can a CAPEX project attach to a Program?

**Model: yes, to a CAPEX Program only.**

```python
# projects/models.py, Project._validate_program_link()  (called from Project.save())
        if self.project_type == 'Residential':
            raise ValidationError('Residential projects cannot be linked to a Program.')
        program_type = self.program.program_type
        if self.project_type != program_type:
            raise ValidationError(
                f'Type mismatch: a {self.project_type} site cannot belong to a '
                f'{program_type} Program.'
            )
# projects/models.py, Program
    PROGRAM_TYPE_CHOICES = [('OPEX', 'RESCO'), ('CAPEX', 'CAPEX')]
```

**Screens: no path attaches a CAPEX project to a Program.**
- `views.create_opex_site()` forces `site.project_type = 'OPEX'` (table B, `create_opex_site` 5138).
- `views.opex_site_create`, `opex_site_bulk_upload` and `opex_site_bulk_template` load the Program with `get_object_or_404(Program, pk=pk, is_deleted=False, program_type='OPEX')`, so a CAPEX Program 404s.
- `program_detail.html` shows "Bulk Upload Sites" / "+ Add Site" only `{% if can_edit and program.program_type == 'OPEX' %}`.
- `forms.ProjectCreateForm.Meta.fields` has no `program`. Its `__init__` and `clean_project_type` drop only `'OPEX'`, so a **standalone** CAPEX project can be created from the generic form.
- `admin.ProjectAdmin` (class body to the next `@admin.register`) contains no `program` reference.

CAPEX **Programs** can be created: `ProgramForm.clean` has an `elif program_type == 'CAPEX':` branch that blanks `short_tender_code`. The local DB has one, `Mehtab Ahmed` (Draft, 0 sites; F15).

### E.2 Does the Zoho webhook create CAPEX projects?

**No.** It creates Residential only, hard-coded:

```python
# projects/views.py, zoho_deal_closed_webhook()
            project = Project.objects.create(
                customer_name=customer_name or 'Unknown',
                ...
                project_type='Residential',
```

There is no deal-type → project-type mapping in the function. The only `project_type` hit in its body is this literal (table B, views.py `zoho_deal_closed_webhook`).

---

## F. Existing CAPEX data (read-only SQL on `solarpms_local`)

All statements ran inside `BEGIN; SET TRANSACTION READ ONLY; SET default_transaction_read_only = on; … ROLLBACK`. Raw query text and output are below, verbatim from the run.

```
== F0 identity
SQL: SELECT current_database(), now(), current_setting('default_transaction_read_only')
current_database | now | current_setting
solarpms_local | 2026-11-01 05:48:25.388841+00:00 | on
(1 rows)
```

```
== F1 projects by type/status/deleted
SQL: SELECT project_type, status, is_deleted, count(*) FROM projects_project GROUP BY 1,2,3 ORDER BY 1,2,3
project_type | status | is_deleted | count
CAPEX | Active | False | 1
OPEX | Active | False | 8
OPEX | Active | True | 1
OPEX | Draft | False | 101
Residential | Active | False | 26
Residential | Active | True | 11
Residential | Draft | False | 7
(7 rows)
```

```
== F2 CAPEX projects detail
SQL: SELECT id, project_id, status, is_deleted, program_id, activated_at, created_at::date FROM projects_project WHERE project_type='CAPEX' ORDER BY id
id | project_id | status | is_deleted | program_id | activated_at | created_at
240 | HRP-CAP-2026-001 | Active | False |  | 2026-08-15 07:08:19.001293+00:00 | 2026-08-15
(1 rows)
```

```
== F3 phases+tasks on CAPEX projects
SQL: SELECT p.project_id, count(DISTINCT ph.id) AS phases, count(t.id) AS tasks, count(t.template_task_id) AS tasks_with_template FROM projects_project p LEFT JOIN projects_projectphase ph ON ph.project_id=p.id LEFT JOIN projects_task t ON t.phase_id=ph.id WHERE p.project_type='CAPEX' GROUP BY p.project_id ORDER BY 1
project_id | phases | tasks | tasks_with_template
HRP-CAP-2026-001 | 0 | 0 | 0
(1 rows)
```

```
== F4 programs by type
SQL: SELECT program_type, status, is_deleted, count(*) FROM projects_program GROUP BY 1,2,3 ORDER BY 1,2,3
program_type | status | is_deleted | count
CAPEX | Draft | False | 1
OPEX | Active | False | 3
OPEX | Draft | False | 2
(3 rows)
```

```
== F5 CAPEX projects with a Program
SQL: SELECT p.project_id, p.program_id, pg.program_type, pg.name FROM projects_project p JOIN projects_program pg ON pg.id=p.program_id WHERE p.project_type='CAPEX'
project_id | program_id | program_type | name
(0 rows)
```

```
== F6 payment requests on CAPEX projects
SQL: SELECT pr.status, count(*) FROM projects_paymentrequest pr JOIN projects_project p ON p.id=pr.project_id WHERE p.project_type='CAPEX' GROUP BY 1
status | count
(0 rows)
```

```
== F7 delivery challans on CAPEX projects
SQL: SELECT dc.status, count(*) FROM projects_deliverychallan dc JOIN projects_project p ON p.id=dc.project_id WHERE p.project_type='CAPEX' GROUP BY 1
status | count
(0 rows)
```

```
== F8 vendor orders by project_type
SQL: SELECT project_type, count(*) FROM projects_vendororder GROUP BY 1 ORDER BY 1
project_type | count
OPEX | 2
(1 rows)
```

```
== F9 payment milestones on CAPEX projects
SQL: SELECT count(*) FROM projects_paymentmilestone pm JOIN projects_project p ON p.id=pm.project_id WHERE p.project_type='CAPEX'
count
6
(1 rows)
```

```
== F10 design assignments / BOQs / COD on CAPEX
SQL: SELECT (SELECT count(*) FROM projects_designassignment d JOIN projects_project p ON p.id=d.project_id WHERE p.project_type='CAPEX') AS design_assignments, (SELECT count(*) FROM projects_boq b JOIN projects_project p ON p.id=b.project_id WHERE p.project_type='CAPEX') AS boqs, (SELECT count(*) FROM projects_codrecord c JOIN projects_project p ON p.id=c.project_id WHERE p.project_type='CAPEX') AS cod_records
design_assignments | boqs | cod_records
0 | 0 | 0
(1 rows)
```

```
== F11 all task templates
SQL: SELECT id, code, label, project_type, version_no, status, effective_from FROM projects_tasktemplate ORDER BY code, version_no
id | code | label | project_type | version_no | status | effective_from
6 | OPEX | OPEX Execution | OPEX | 1 | active | 2026-09-01
1 | RESIDENTIAL | Residential EPC | Residential | 1 | active | 2026-08-28
(2 rows)
```

```
== F13 status transitions for HRP-CAP-2026-001
SQL: SELECT st.from_status, st.to_status, st.reason_code, st.occurred_at FROM projects_statustransition st WHERE st.subject_type='project' AND st.project_id=(SELECT id FROM projects_project WHERE project_id='HRP-CAP-2026-001') ORDER BY st.occurred_at
from_status | to_status | reason_code | occurred_at
(0 rows)
```

```
== F14 activity log for HRP-CAP-2026-001 (first 15)
SQL: SELECT a.action, a.action_code, a.timestamp FROM projects_activitylog a JOIN projects_project p ON p.id=a.project_id WHERE p.project_id='HRP-CAP-2026-001' ORDER BY a.timestamp LIMIT 15
action | action_code | timestamp
Uploaded 1 file(s) to project |  | 2026-08-15 06:57:06.237255+00:00
Uploaded 1 file(s) to project |  | 2026-08-15 06:57:35.818226+00:00
Raised issue: Detailed Design Engineering (Medium) | issue_created | 2026-08-15 07:04:23.030899+00:00
Activated project: HRP-CAP-2026-001 |  | 2026-08-15 07:08:19.024885+00:00
Assigned Design lead Praveen Kethunia to 0 tasks | design_bulk_assigned | 2026-08-15 07:08:28.907677+00:00
Deleted file: JAKEDA RFS.pdf |  | 2026-08-15 07:08:47.439176+00:00
(6 rows)
```

```
== F15 CAPEX program detail
SQL: SELECT id, name, program_type, status, short_tender_code, (SELECT count(*) FROM projects_project p WHERE p.program_id=pg.id) AS sites FROM projects_program pg WHERE program_type='CAPEX'
id | name | program_type | status | short_tender_code | sites
3 | Mehtab Ahmed | CAPEX | Draft |  | 0
(1 rows)
```

```
== F16 vendor orders / PRs touching CAPEX via other links
SQL: SELECT (SELECT count(*) FROM projects_vendororder WHERE project_type='CAPEX') AS capex_orders
capex_orders
0
(1 rows)
```

```
== F12 payment milestones on HRP-CAP-2026-001
SQL: SELECT pm.id, pm.milestone_name, pm.milestone_description, pm.status FROM projects_paymentmilestone pm JOIN projects_project p ON p.id=pm.project_id WHERE p.project_id='HRP-CAP-2026-001' ORDER BY pm.id
id | milestone_name | milestone_description | status
112 | M1 | On Survey Completion | Pending
113 | M2 | On Material Supply | Pending
114 | M3 | On Commissioning | Pending
115 | M1 | On Survey Completion | Pending
116 | M2 | On Material Supply | Pending
117 | M3 | On Commissioning | Pending
(6 rows)
```

```
== F13b status transitions on project 240, any subject_type
SQL: SELECT subject_type, subject_id, from_status, to_status, reason_code, occurred_at FROM projects_statustransition WHERE project_id=240 ORDER BY occurred_at
subject_type | subject_id | from_status | to_status | reason_code | occurred_at
(0 rows)
```

```
== F13c distinct subject_type values
SQL: SELECT subject_type, count(*) FROM projects_statustransition GROUP BY 1 ORDER BY 1
subject_type | count
approval_request | 7
delivery_challan | 9
design_assignment | 10
issue | 1
payment_request | 7
project | 23
task | 24
(7 rows)
```

```
== F2b CAPEX project assigned_design / assigned_pm
SQL: SELECT project_id, assigned_design_id, assigned_pm_id, is_test FROM projects_project WHERE project_type='CAPEX'
project_id | assigned_design_id | assigned_pm_id | is_test
HRP-CAP-2026-001 | 17 | 16 | False
(1 rows)
```

### F summary (from the output above)

| Item | Count |
|---|---|
| CAPEX projects | **1**: `HRP-CAP-2026-001`, Active, not deleted, `program_id` NULL, activated 2026-08-15 |
| Phases / tasks on CAPEX projects | **0 / 0** |
| CAPEX projects linked to a Program | **0** |
| CAPEX Programs | **1** (`Mehtab Ahmed`, Draft, 0 sites) |
| Payment requests on CAPEX projects | **0** |
| Delivery challans on CAPEX projects | **0** |
| Vendor orders with `project_type='CAPEX'` | **0** (only 2 OPEX) |
| Payment milestones on the CAPEX project | **6**: M1/M2/M3 twice (ids 112–117) |
| DesignAssignment / BOQ / CodRecord on CAPEX | 0 / 0 / 0 |
| StatusTransition rows for the CAPEX project | 0 |
| ActivityLog for the CAPEX project | 6 rows, incl. "Activated project: HRP-CAP-2026-001" (2026-08-15 07:08:19) and "Assigned Design lead Praveen Kethunia to 0 tasks" (`design_bulk_assigned`) |

The project's state (Active, `assigned_design_id` set, M1–M3 rows, zero tasks) is the `project_activate` outcome described in A.3 item 2. Why there are **two** M1–M3 sets against one "Activated project" log row is not established by these queries; recorded in `SECONDARY_FINDINGS.md`.

---

## G. Tests

Scope: `projects/tests*.py`. Read-only; suite not run for this section.

### Evidence: commands

```
$ grep -lE "CAPEX" projects/tests*.py | wc -l
25
$ grep -cE "CAPEX" projects/tests*.py | grep -v ":0"
tests_admin_project_type_filter.py:3   tests_bill_payment_forward.py:1   tests_bill_payment_readers.py:14
tests_ceo_tender_sites.py:5            tests_ceo_tenders_cleanup.py:3    tests_ceo_tenders_scope.py:3
tests_ceo_tenders_strip.py:13          tests_checklist_snapshot.py:1     tests_cod_record.py:3
tests_contractor_bills.py:1            tests_design_part10.py:1          tests_design_throughput.py:3
tests_g95_held_tasks.py:1              tests_opex_design_selector.py:1   tests_opex_manual_dates.py:9
tests_payment_queue.py:8               tests_payment_readers_o6.py:12    tests_purchases_workspace.py:1
tests_qaqc_visit.py:3                  tests_scm_context_filter.py:4     tests_stuck_sites.py:2
tests_task_health.py:10                tests_task_rename.py:2            tests_task_reorder.py:4
tests_tender_cards.py:4
```

Each line was mapped to its enclosing `Class::method` with an AST walk
(scratchpad `audit/gcap.py`, read-only). Raw mapping: 107 lines. Docstring and
comment lines are left out of G.1 below unless they are the only reference.

```
$ grep -nE "REORDERABLE_PROJECT_TYPES|CONTEXT_PROJECT_TYPES|_TENDER_TYPES|CEO_TENDER_TYPES|RECORD_TYPES" projects/tests*.py
projects/tests_scm_context_filter.py:5:One mapping, CONTEXT_PROJECT_TYPES in views.py, read by `_context_filter` (counters,
```
That is a docstring mention only. **No test imports or asserts the contents of
`REORDERABLE_PROJECT_TYPES`, `CONTEXT_PROJECT_TYPES`, `_TENDER_TYPES`,
`CEO_TENDER_TYPES` or `RECORD_TYPES`.**

```
$ grep -nE "TaskTemplate\.objects\.(count|get|filter)\(|TaskTemplate\.DoesNotExist" projects/tests*.py
```
29 hits (opex_activation 2, opex_manual_dates 1, opex_template 12,
opex_template_correction 6, task_template 8). Every lookup is filtered by
`code=OPEX_CODE`, `code=RESIDENTIAL_CODE` / `RESIDENTIAL_TEMPLATE_CODE`, or
`project_type='OPEX'`. The one unfiltered `TaskTemplate.objects.count()`
(`tests_opex_template::SeedIdempotencyTests::test_running_the_seed_again_creates_nothing`,
lines 289–300) compares before and after counts, not a constant. **No test pins
the total number of templates, so adding a CAPEX template would not break any
of these by count.**

---

### G.1 Tests referencing CAPEX

In the table, CI means "already CAPEX-inclusive". The last column says whether
the test changes if CAPEX gets OPEX behaviour.

| # | Test | Asserting line (quoted) | Changes if CAPEX gets OPEX behaviour? |
|---|---|---|---|
| 1 | tests_admin_project_type_filter::AdminProjectTypeFilterTests::test_each_type_returns_only_its_rows | `('CAPEX', self.capex)):` (fixture `cls.capex = [_project('Capex One', 'CAPEX')]`) | No (admin filter, CI) |
| 2 | tests_admin_project_type_filter::AdminProjectTypeFilterTests::test_count_line_matches_filtered_rows | `({'type': 'OPEX'}, 3), ({'type': 'CAPEX'}, 1),` | No |
| 3 | tests_bill_payment_forward::PoPiActionPinTests::act (helper used by that class's tests) | `client.get(reverse('payment_queue'), {'tab': 'CAPEX'})` | No |
| 4 | tests_bill_payment_readers::TabTests::test_each_bill_payment_is_on_its_bill_sites_tab_only | `'CAPEX': {self.capex_pay.pk, self.capex_bill_pay.pk}}` | No (CI) |
| 5 | tests_bill_payment_readers::CapexTabTests::test_a_capex_bill_payment_draws_the_tab_and_is_listed_on_it | `self.assertEqual(values, ['Residential', 'OPEX', 'CAPEX'])` | No (CI) |
| 6 | tests_bill_payment_readers::SearchTests::test_the_queue_finds_a_bill_payment_by_bill_site_tender_and_contractor | `('CAPEX', 'Civil Works', {self.capex_bill_pay.pk}),` | No |
| 7 | tests_bill_payment_readers::PageTests::test_every_queue_tab_and_chip_renders_and_shows_the_bill | `('CAPEX', self.capex_bill)):` | No |
| 8 | tests_bill_payment_readers::TestSiteTests::test_the_ceo_strip_drops_a_bill_on_a_test_site_and_keeps_a_real_one | `self.assertEqual(ceo_payment_strip(['CAPEX'], today)['awaiting'].count, 2)` | No |
| 9 | tests_bill_payment_readers::PoPiQueryCountTests (parametrised) | `('payment_queue', 'finance', {'tab': 'CAPEX'}, 17),` | No (query budget; could move if CAPEX gains joins) |
| 10 | tests_ceo_tender_sites::ScopeTests::test_test_and_capex_sites_never_appear | `self.assertFalse(ids & {'TEST-S1', 'CAPEX-S1'}, stage)` | **Yes, if CEO Tenders includes CAPEX** (see G.2) |
| 11 | tests_ceo_tender_sites::ScopeTests::test_unknown_values_are_a_404 | builds `Program(program_type='CAPEX')` + `s7._site(capex, 'CAPEX-P1', project_type='CAPEX')`; asserts 404 for its program id among the bad queries | **Yes, if CEO Tenders includes CAPEX** |
| 12 | tests_ceo_tenders_cleanup::ApprovalsFooterScopeTests::test_cancelled_and_capex_links_count_as_outside | `self.assertEqual(self.card()['unlinked'], 3)` (one of the 3 is the CAPEX-linked request) | **Yes, if CEO Tenders includes CAPEX** |
| 13 | tests_ceo_tenders_scope::CeoTendersScopeTests::test_capex_excluded_from_tenders_cards_and_tender_sites | `self.assertNotIn('S1-CPX', _card_ids('tenders'))` / `self.assertNotIn('S1-CPX', set(tender_sites_qs()...))` | **Yes, if CEO Tenders includes CAPEX** |
| 14 | tests_ceo_tenders_strip::BucketTests::test_payment_counts_is_unchanged | `self.assertEqual(payment_counts(['CAPEX'])[PaymentRequest.PENDING_APPROVAL], ...` | No |
| 15 | tests_ceo_tenders_strip::BucketTests (fixture) | `cls._pay([], '5000', project_type='CAPEX')`: "a CAPEX request outside the scope" | **Yes, if CEO Tenders includes CAPEX** (bucket counts) |
| 16 | tests_ceo_tenders_strip::HeaderTests::test_capex_line_follows_live_capex_projects | `self.assertIn('Not shown: CAPEX · 1 project', self._tenders_text())` | **Yes, if CEO Tenders includes CAPEX** (the line is removed) |
| 17 | tests_ceo_tenders_strip::NoContextTests::test_no_context_hides_test_projects_and_keeps_capex | `_site('S2-NC-CPX', Decimal('5'), project_type='CAPEX')`; no-context keeps CAPEX | No (CI) |
| 18 | tests_checklist_snapshot::ChecklistResolutionTests::test_a_different_project_type_does_not_match_the_link | `self.project.project_type = 'CAPEX'` → `self.assertIsNone(_checklist_for_task(self.task, self.project))` | **Yes, if CAPEX tasks resolve OPEX checklist links** |
| 19 | tests_cod_record::CodNotOnOtherSiteTypesTests::test_no_card_and_every_url_404s | `for project_type in ('Residential', 'CAPEX'):` … `assertEqual(...status_code, 404, url)` | **Yes** |
| 20 | tests_contractor_bills::CompletionTests::test_residential_and_capex_are_complete_when_done | `self.assertTrue(task_complete_for_bill(Task(status='Done'), project_type))` for `('Residential', 'CAPEX')` | **Yes, if two-step completion covers CAPEX** |
| 21 | tests_design_part10::AccessTests::test_a_residential_program_is_not_reachable | `Program(program_type='CAPEX')` → `self.assertEqual(response.status_code, 404)` on `design_quality_analytics_tender` | **Yes, if tender design analytics includes CAPEX** (the test name says "residential" but the program is CAPEX) |
| 22 | tests_design_throughput::ScopeTests::test_nothing_counts | `(capex, 'CPX', {'project_type': 'CAPEX'})` in fixture; asserts all-zero tuple | **Yes, if CEO Tenders includes CAPEX** |
| 23 | tests_g95_held_tasks::QaqcEngineerProjectsQTests::test_non_opex_project | `.update(project_type='CAPEX')` then `self._agree(self.qaqc.pk, self.site_a, False)` | **Yes, if QA/QC covers CAPEX** |
| 24 | tests_opex_design_selector::DesignSelectorBase::test_capex_still_renders_the_selector | `self.assertTrue(response.context['show_design_selector'])` | **Yes, if CAPEX gets OPEX design allocation** |
| 25 | tests_opex_design_selector::…::test_capex_post_still_assigns | `self.assertEqual(self.capex.assigned_design_id, self.designer.pk)` | **Yes, same** |
| 26 | tests_opex_manual_dates::ManualDatesFixture::_activated_capex_site (fixture) | docstring: "`attach_opex_template()` raises `TaskTemplate.DoesNotExist` for CAPEX — no ACTIVE CAPEX template has been seeded"; builds CAPEX by activating as OPEX then `site.project_type = 'CAPEX'` | Fixture workaround becomes unnecessary; the test still passes |
| 27 | tests_opex_manual_dates::RecalculateRefusesOpexTests::test_a_capex_site_is_refused_too | `assertEqual([d for d in self._due_dates(capex) if d is not None], [], ...)` | No (CI: pins "not Residential") |
| 28 | tests_opex_manual_dates::CascadeSchedulingRefusesOpexTests::test_a_capex_site_is_refused_too | `self.assertFalse(capex.cascade_scheduling)` | No (CI) |
| 29 | tests_payment_queue::TabTests::test_each_tab_shows_only_its_project_type | `self.assertEqual(self.listed(self.queue(tab='CAPEX')), {self.capex_pay.pk})` | No |
| 30 | tests_payment_queue::TabTests::test_a_siteless_payment_appears_in_its_tab | `self.assertIn(self.capex_pay.pk, self.listed(self.queue(tab='CAPEX')))` | No |
| 31 | tests_payment_queue::TabTests::test_tab_labels_counts_match_the_rows | `self.assertIn('CAPEX · 1 to approve · 0 to pay', text)` | No |
| 32 | tests_payment_queue::QueryCountTests::_fill / _count | `_make_order(self, 'CAPEX', ...)`; `client.get(reverse('payment_queue'), {'tab': 'CAPEX'})` | No |
| 33 | tests_payment_readers_o6::PaymentCountsTests::test_every_status_is_present_and_zero_filled | `counts = payment_counts(['CAPEX'])` | No |
| 34 | tests_payment_readers_o6::PaymentCountsTests::test_none_is_every_type_and_a_list_narrows | `tenders = payment_counts(['OPEX', 'CAPEX'])[PaymentRequest.APPROVED]` | No |
| 35 | tests_payment_readers_o6::PaymentCountsTests::test_a_siteless_and_a_draft_site_payment_are_counted | `self.assertEqual(pending.count, 1)  # the site-less CAPEX one` | No |
| 36 | tests_payment_readers_o6::ThreeWayAgreementTests::_queue_to_pay | `'tenders': ['OPEX', 'CAPEX']}[context]` | No |
| 37 | tests_payment_readers_o6::AmountRuleTests::test_a_plain_request_is_one_figure | `text = _text(self.queue(tab='CAPEX'))` | No |
| 38 | tests_payment_readers_o6::CapexTabTests::test_the_capex_tab_is_drawn_while_a_capex_order_exists | `self.assertEqual(tabs, ['Residential', 'OPEX', 'CAPEX'])` | No |
| 39 | tests_payment_readers_o6::CapexTabTests::test_an_order_with_no_payment_is_enough | `self.assertIn('CAPEX', tabs)` | No |
| 40 | tests_payment_readers_o6::CapexTabTests::test_no_capex_order_no_capex_tab | `self.assertNotContains(response, 'CAPEX ·')` | No |
| 41 | tests_payment_readers_o6::CapexTabTests::test_asking_for_the_missing_tab_lands_on_the_first | `self.assertEqual(self.queue(tab='CAPEX').context['tab'], 'Residential')` | No |
| 42 | tests_purchases_workspace::AddRecordTests::test_nothing_recorded_and_no_project_type_is_refused | `for choice in (None, '', 'CAPEX', 'junk'):` → `assertContains(response, 'choose whether this PO / PI is Residential or RESCO', status_code=400)` | **Yes, if the Purchases workspace accepts CAPEX records** |
| 43 | tests_qaqc_visit::NonOpexTests::test_no_card_and_no_urls | `for project_type in ('Residential', 'CAPEX'):` → `assertNotContains(page, 'qaqc-visits-card')` + URLs 404 | **Yes** |
| 44 | tests_scm_context_filter::ScmContextFilterTests::test_opex_and_capex_projects_listed_only_under_tenders (+ the other 5 tests in the class share the CAPEX fixture) | fixture `cls.capex = cls._project('CTX-CPX', 'CAPEX', program=capex_program)` | No (CI) |
| 45 | tests_stuck_sites::ScopeTests::test_s0_test_and_capex_never_appear | `capex = _site(None, 'CAPEX-S1', project_type='CAPEX')` → `self.assertEqual(result['rows'], [])` | **Yes, if CEO Tenders includes CAPEX** |
| 46 | tests_task_health::PredicateAgreementTests::test_project_type_and_approval_make_no_difference | `self.assertEqual(per_type['CAPEX'], per_type['Residential'])` | No (asserts type-independence) |
| 47 | tests_task_health::CeoOverdueListTests::test_the_list_is_the_cards_number_in_every_context (and the class's other tests using `SITES`) | `SITES = {None: ('OL-CAPEX','OL-OPEX','OL-RES'), 'residential': ('OL-RES',), 'tenders': ('OL-OPEX',)}`, comment "Tenders is RESCO only (D2 hides CAPEX there)" | **Yes, if CEO Tenders includes CAPEX** |
| 48 | tests_task_health::CeoOverdueListTests::test_longest_overdue_first_then_site | `(9, 'OL-CAPEX'), (9, 'OL-OPEX'), (9, 'OL-RES'), ...` (no-context ordering) | No |
| 49 | tests_task_rename::AllowedTests::test_allowed_on_capex | `self.assertEqual(Task.objects.get(pk=task.pk).task_name, 'Hand Task Renamed')` | No (CI) |
| 50 | tests_task_rename::ChecklistTests::test_a_linked_name_of_another_project_type_does_not_refuse | `ChecklistTaskLink(..., project_type='CAPEX')` on an OPEX task → rename succeeds | **Possibly**: it changes if link matching stops keying on `project_type` |
| 51 | tests_task_reorder::EligibilityTests::test_a_capex_project_is_admitted | `self.assertTrue(can_reorder_phase(self.pm.user, phases[0]))` | No (CI). The fixture docstring says "no real CAPEX project has phases yet" |
| 52 | tests_task_reorder::HandleTests::test_handle_on_a_capex_project_with_phases | `self.assertEqual(html.count(HANDLE_MARK), self._site_task_count(project))` | No (CI) |
| 53 | tests_tender_cards::ScopeTests::test_test_and_capex_excluded | `self.assertEqual({c['name'] for c in cards}, {'Alpha', 'Bravo', 'Charlie'})` (CAPEX programme `Capex Co` excluded) | **Yes, if CEO Tenders includes CAPEX** |
| 54 | tests_tender_cards::ScopeTests::test_cards_sum_to_the_header | `self.assertEqual(ctx['tender_header']['sites'], 12)` (with CAPEX fixture present) | **Yes, if CEO Tenders includes CAPEX** |

**G.1 count: 54 entries**, covering 25 files. Rows 3, 9, 15, 26, 32 and 44 are
fixtures or helpers; the rest are test methods.

---

### G.2 Tests pinning OPEX-only behaviour that CAPEX inclusion would change

Each entry gives the production gate the test pins (by function or constant)
and what would change. All of these come from G.1. A test that pins
OPEX-only behaviour with only a Residential negative would not change, so it is
in G.3, not here.

**G.2-a: closeout, QA/QC, approval and design behaviour (template/behaviour parity)**

| # | Test | Gate pinned (production) | What changes |
|---|---|---|---|
| 1 | tests_cod_record::CodNotOnOtherSiteTypesTests::test_no_card_and_every_url_404s | `cod_views` OPEX-only check (`COD_OPEX_ONLY = 'COD is recorded on RESCO sites only.'`), plus the overview COD card condition | CAPEX would get the card and 200s; the CAPEX subTest fails |
| 2 | tests_qaqc_visit::NonOpexTests::test_no_card_and_no_urls | `qaqc_views` OPEX-only gate on visit URLs + overview `qaqc-visits-card` | CAPEX subTest fails |
| 3 | tests_g95_held_tasks::QaqcEngineerProjectsQTests::test_non_opex_project | `qaqc_engineer_projects_q` (and the writer it agrees with, which "refuses a non-OPEX site") | CAPEX row would be counted; `_agree(..., False)` fails |
| 4 | tests_contractor_bills::CompletionTests::test_residential_and_capex_are_complete_when_done | `task_complete_for_bill(task, project_type)`: Done alone for non-OPEX; Done + `approved_at` for OPEX | If two-step covers CAPEX, `Task(status='Done')` without `approved_at` is no longer complete for CAPEX |
| 5 | tests_checklist_snapshot::ChecklistResolutionTests::test_a_different_project_type_does_not_match_the_link | `_checklist_for_task(task, project)`: link matched on `project_type` | If CAPEX tasks resolve the OPEX template's links, this returns the checklist instead of None |
| 6 | tests_opex_design_selector::…::test_capex_still_renders_the_selector | `show_design_selector` / `can_assign_design` in the project overview (PM selector hidden for OPEX only) | If CAPEX adopts OPEX design allocation, the selector is hidden for CAPEX |
| 7 | tests_opex_design_selector::…::test_capex_post_still_assigns | `assign_design` POST branch (refused for OPEX only) | The CAPEX POST would be refused |
| 8 | tests_task_rename::ChecklistTests::test_a_linked_name_of_another_project_type_does_not_refuse | `can_rename_task` reserved-name check against `ChecklistTaskLink.project_type` | Only if link matching changes from `project_type` to template; the OPEX task would then be refused |

**G.2-b: tender / CEO / SCM scope (changes only if the dashboard scope also widens to CAPEX)**

| # | Test | Gate pinned (production) | What changes |
|---|---|---|---|
| 9 | tests_ceo_tenders_scope::CeoTendersScopeTests::test_capex_excluded_from_tenders_cards_and_tender_sites | `CEO_TENDER_TYPES = ['OPEX']` via `tender_sites_qs()` and the CEO tenders card filter | S1-CPX would appear |
| 10 | tests_tender_cards::ScopeTests::test_test_and_capex_excluded | `tender_cards(tender_sites_qs(), ...)` | `Capex Co` card would appear |
| 11 | tests_tender_cards::ScopeTests::test_cards_sum_to_the_header | same | header `sites` 12 → 13; tenders count +1 |
| 12 | tests_ceo_tender_sites::ScopeTests::test_test_and_capex_sites_never_appear | `tender_site_list` view scope (`tender_sites_qs`) | CAPEX-S1 would be listed |
| 13 | tests_ceo_tender_sites::ScopeTests::test_unknown_values_are_a_404 | `tender_site_list` program validation | The CAPEX program id would be a 200, not a 404 |
| 14 | tests_ceo_tenders_cleanup::ApprovalsFooterScopeTests::test_cancelled_and_capex_links_count_as_outside | approvals footer `unlinked` aggregate (tender scope) | `unlinked` 3 → 2 |
| 15 | tests_ceo_tenders_strip::HeaderTests::test_capex_line_follows_live_capex_projects | "Not shown: CAPEX · N project" header line (`_capex_hidden_count()`) | The line has no reason to exist; the assertion fails |
| 16 | tests_ceo_tenders_strip::BucketTests (fixture CAPEX request "outside the scope") | `ceo_payment_strip(CEO_TENDER_TYPES, ...)` | Bucket counts shift |
| 17 | tests_design_throughput::ScopeTests::test_nothing_counts | `design_throughput(sites_qs, today)` over `tender_sites_qs()` | The non-zero CAPEX rows would count |
| 18 | tests_stuck_sites::ScopeTests::test_s0_test_and_capex_never_appear | `stuck_sites(sites_qs, ...)` over `tender_sites_qs()` | CAPEX-S1 could appear as stuck |
| 19 | tests_task_health::CeoOverdueListTests (`SITES['tenders'] == ('OL-OPEX',)`) | CEO overdue list in the Tenders context (D2 hides CAPEX) | `tenders` would include OL-CAPEX |
| 20 | tests_design_part10::AccessTests::test_a_residential_program_is_not_reachable | `design_quality_analytics_tender` program-type check | The CAPEX program would be a 200 |
| 21 | tests_purchases_workspace::AddRecordTests::test_nothing_recorded_and_no_project_type_is_refused | `purchases_views.RECORD_TYPES = [('Residential', 'Residential'), ('OPEX', 'RESCO')]` / `_record_project_type` | 'CAPEX' would no longer be refused |

**G.2 count: 21 entries** (8 in G.2-a, 13 in G.2-b).

**Already CAPEX-inclusive, so no change** (listed so that their silence is not
misread): rename `test_allowed_on_capex`; reorder
`test_a_capex_project_is_admitted` and `test_handle_on_a_capex_project_with_phases`;
manual dates `test_a_capex_site_is_refused_too` ×2; payment queue and O6 CAPEX
tab tests; admin type filter; SCM context filter (`CONTEXT_PROJECT_TYPES`
tenders = OPEX + CAPEX); `PredicateAgreementTests`.

**No test asserts that activating a CAPEX project is blocked or yields zero
tasks.** The only statement of it is the docstring of the
`tests_opex_manual_dates::ManualDatesFixture::_activated_capex_site` fixture,
quoted in G.1 row 26.

---

### G.3 OPEX-only gates tested with Residential as the only negative case

Method: `grep -nE "def test_[a-z0-9_]*(residential|non_opex|not_opex)[a-z0-9_]*" projects/tests*.py`
gave 117 method names across 55 files. I read the names and kept those that
assert an OPEX-only gate refuses or hides on Residential, and dropped the
"Residential unchanged" regression tests. These tests would **not** break if
CAPEX were included. They also give **no coverage** of CAPEX for that gate.

Limitation: this is name-based. A test whose name omits "residential" but uses
a Residential negative is not counted here.

| File | Count | Gate (test names) |
|---|---|---|
| tests_two_step_completion.py | 2 | two-step: `test_a_residential_task_goes_straight_to_done`, `test_the_approval_endpoints_refuse_a_residential_task` (asserts message "opex sites only") |
| tests_duplicate_for_locations.py | 1 | `can_duplicate_task_for_locations`: `test_refused_for_residential` |
| tests_punch_point.py | 2 | punch points on reject: `test_rejecting_a_residential_task_is_refused_and_raises_nothing`, `test_a_residential_task_detail_renders_with_no_punch_point_block` |
| tests_punch_rectify.py | 1 | `test_loop_urls_404_on_a_residential_project` |
| tests_cod_commissioned.py | 2 | `test_residential_has_no_cod_screen_or_pill`, `test_residential_is_refused_by_the_first_rule` |
| tests_qaqc_assignment.py | 1 | `test_the_writer_refuses_a_residential_site` |
| tests_site_drawings.py | 1 | `test_residential_draws_no_card` |
| tests_phase_list_approval.py | 2 | SE row approval UI: `test_residential_row_still_offers_done`, `test_residential_row_renders_no_approval_markup_at_all` |
| tests_phase_rules.py | 2 | `test_residential_never_writes_a_row`, `test_r1_on_a_residential_site_writes_nothing` |
| tests_design_mirror_derivation.py | 1 | `test_04_a_residential_project_has_no_design_mirror` |
| tests_delivery_detail.py | 1 | `test_05_a_residential_task_is_untouched` |
| tests_design_part11.py | 2 | OPEX BOQ catalogue/picker: `test_06b_residential_boq_is_never_design_locked`, `test_06c_picker_404s_on_a_residential_project` |
| tests_boq_correction.py | 2 | `test_residential_is_false_structurally`, `test_a_residential_project_404s` |
| tests_boq_upload.py | 1 | `test_a_residential_project_404s` |
| tests_design_assign_qc.py | 1 | `test_a_residential_or_deleted_site_is_404` |
| tests_design_combined_submission.py | 1 | `test_06_nothing_here_reaches_a_residential_project` |
| tests_opex_activation.py | 1 | `opex_site_activate`: `test_a_residential_project_is_refused_by_this_view` |
| tests_residential_baseline.py | 1 | `test_opex_site_refuses_a_residential_project` |
| tests_oneoff_ukru001_link.py | 1 | `test_a_row_on_a_non_opex_project_aborts` |
| tests_stuck_sites.py | 1 | `test_not_on_residential` |
| tests_ceo_approvals_waiting.py | 1 | `test_residential_has_no_card` (context gate) |
| tests_design_throughput.py | 1 | `test_residential_and_no_context_have_no_card` (context gate) |
| tests_gantt.py | 1 | inverse gate (Gantt is Residential-only): `test_non_residential_hides_gantt`, negative is **OPEX**, not CAPEX |
| tests_task_reorder.py | 2 | `test_refused_on_a_residential_project`, `test_no_handle_on_a_residential_project` (CAPEX is a tested *positive*, G.1 rows 51–52) |

**G.3 count: 33 tests across 24 files.**

Also noted: `tests_not_applicable.py` builds its fixture on a **Residential**
project (line 83: `project_type='Residential',`). None of its tests mentions
OPEX or CAPEX, so the NA tests assert no project-type gate at all.

---

## Appendix 1 — comment-only and docstring-only hits (163)

Same regex and scope as B.0. These lines mention a project type inside a comment or docstring and contain no condition; they are listed so that no grep hit is dropped.

| file | function/block | line | kind | text |
|---|---|---|---|---|
| projects/admin.py | ChecklistTaskLinkInline | 469 | comment | `# 2.4: template_task IS THE FIELD TO FILL IN. task_name and project_type are shown` |
| projects/admin.py | ChecklistTaskLinkAdmin | 542 | comment | `# checklist on". task_name/project_type stay visible and stay read-only — derived` |
| projects/cod_views.py | _cod_site | 461 | docstring | `recorded on RESCO sites only, so for Residential and CAPEX the URLs do not exist."""` |
| projects/cod_views.py | cod_record | 526 | comment | `# COD is recorded on RESCO sites only; for Residential and CAPEX the URL does not exist.` |
| projects/design_views.py | <module> | 15 | docstring | `OPEX ONLY. Every entry point re-checks 'project_type == 'OPEX''; Residential design work` |
| projects/design_views.py | <module> | 446 | comment | `# utils.attach_opex_template(). The audit's §3.4 finding is why there are two: design` |
| projects/design_views.py | _design_mirror_task | 586 | docstring | `'if project.project_type != 'OPEX'' would. 'code' is unique within a TEMPLATE, not` |
| projects/design_views.py | _design_mirror_task | 604 | docstring | `A 'project_type' join on 'template_task__phase__template' — the third guard` |
| projects/design_views.py | sync_design_mirror | 745 | docstring | `utils.attach_opex_template()   the RECONCILE — a site just got its tasks, and` |
| projects/design_views.py | sync_design_mirror | 790 | comment | `# attach_opex_template() calls this function the moment the rows appear.` |
| projects/design_views.py | apply_design_status | 1389 | comment | `# transition. That is what lets attach_opex_template() call the same function` |
| projects/design_views.py | <module> | 8412 | comment | `# Residential item_id. That is why the object lookup filters on project_type as well as pk` |
| projects/design_views.py | _opex_catalogue_item | 8436 | docstring | `THE project_type TERM IS THE BOUNDARY, not a convenience filter. A Residential item_id` |
| projects/design_views.py | design_boq_catalogue | 8451 | docstring | `Ordering is (sort_order, code) with no project_type term. The Admin screen needs one` |
| projects/design_views.py | design_boq_catalogue_create | 8483 | docstring | `'project_type' IS DELETED FROM THE FORM AND SET ON THE INSTANCE. Not hidden, not` |
| projects/design_views.py | design_boq_catalogue_create | 8485 | docstring | `project_type=Residential is never read at all. The field is editable on the shared` |
| projects/design_views.py | design_boq_catalogue_edit | 8534 | docstring | `'project_type' is dropped because this screen is OPEX-only. Left bound, it would let` |
| projects/forms.py | ProjectCreateForm.__init__ | 389 | comment | `# break existing legacy program=null rows). See also clean_project_type.` |
| projects/forms.py | ProjectEditForm | 436 | docstring | `"""Same as ProjectCreateForm but project_type is excluded (shown as read-only text in template)."""` |
| projects/forms.py | BOQItemMasterForm.clean | 843 | docstring | `'project_type' IS NOT ALWAYS ON THIS FORM, and this rule must not assume it is. The` |
| projects/forms.py | BOQItemMasterForm.clean | 845 | docstring | `'OPEX' on the instance AFTER validation, so at this point neither cleaned_data nor` |
| projects/forms.py | BOQItemMasterForm.clean | 847 | docstring | `'Residential' on the create path, which would refuse exactly the item the flag` |
| projects/forms.py | BOQItemMasterForm.clean | 851 | docstring | `'project_type' is the one that guarantees OPEX, which makes the check redundant` |
| projects/forms.py | BOQItemMasterForm.clean | 862 | comment | `# ACTIVE-NESS IS TESTED FIRST, AND ABOVE THE 'project_type' EARLY RETURN BELOW.` |
| projects/forms.py | BOQItemMasterForm.clean | 863 | comment | `# The Design Head's catalogue screens delete 'project_type' from the form but keep` |
| projects/management/commands/_walkthrough_support.py | permitted_real_email | 78 | docstring | `'utils.attach_residential_template()' resolves the Finance owner of the send-invoice` |
| projects/management/commands/seed_opex_installation_checklists.py | <module> | 37 | docstring | `(task_name, project_type) and the partial unique index on template_task. So Civil Work,` |
| projects/management/commands/seed_opex_installation_checklists.py | Command.handle | 603 | comment | `# template_task is the key; save() derives task_name/project_type from it,` |
| projects/management/commands/seed_opex_test_data.py | <module> | 44 | docstring | `'ProgramForm', OPEX sites through 'views.create_opex_site()', phases and tasks through` |
| projects/management/commands/seed_opex_test_data.py | <module> | 45 | docstring | `'utils.attach_opex_template()' / 'attach_residential_template()', group membership` |
| projects/management/commands/seed_opex_test_data.py | Command._create_opex_sites | 358 | docstring | `"""Four sites through 'views.create_opex_site()' — a genuine` |
| projects/management/commands/seed_opex_test_data.py | Command._activate_opex_sites | 392 | docstring | `real: 'record_transition()' and 'attach_opex_template()', which is what gives` |
| projects/management/commands/seed_opex_test_data.py | Command._create_residential | 437 | docstring | `is the same branch 'views.create_opex_site()' uses in production.` |
| projects/management/commands/seed_opex_test_data.py | Command._seed_boqs | 641 | comment | `# The project_type term is required, not cosmetic: three descriptions exist in` |
| projects/management/commands/seed_order_demo.py | <module> | 74 | docstring | `sites, BOQs, groups and vendors. Those rows need an author — 'create_opex_site()'` |
| projects/management/commands/seed_order_demo.py | <module> | 97 | docstring | `'ProgramForm', sites through 'views.create_opex_site()', group membership through` |
| projects/management/commands/seed_order_demo.py | Command._create_sites | 588 | docstring | `"""Seven sites through 'views.create_opex_site()' — a genuine request-independent` |
| projects/management/commands/seed_scm_handoff_data.py | Command._sweep_logs | 500 | docstring | `narrower: 'create_opex_site()' writes one of each per site, and 'log_activity'` |
| projects/management/commands/seed_scm_pilot.py | Command._sites | 473 | docstring | `"""Six sites through 'views.create_opex_site()' — a genuine request-independent` |
| projects/management/commands/seed_scm_pilot.py | Command._assign_se_tasks | 626 | docstring | `'attach_opex_template' pre-assigns only the PM's three real tasks; all eleven` |
| projects/management/commands/wipe_demo_2026_10_14.py | <module> | 53 | docstring | `record_transition(), attach_opex_template() (phases, tasks, the PM pre-assignment of` |
| projects/models.py | Project | 43 | comment | `# meaning by project_type — see OpexSiteForm and opex_site_form.html.` |
| projects/models.py | Project | 123 | comment | `# it). Dual meaning by project_type, same pattern as customer_phone/customer_email.` |
| projects/models.py | Project._validate_program_link | 180 | docstring | `• A site's project_type must equal its Program's program_type` |
| projects/models.py | Project._validate_program_link | 183 | docstring | `IntegrityError. 'project_type' is validated, not 'program.program_type',` |
| projects/models.py | Program | 226 | comment | `# Reuses Project's project_type vocabulary, restricted to OPEX/CAPEX — same casing,` |
| projects/models.py | Program | 260 | comment | `# OPEX-specific (nullable; populate only when program_type == 'OPEX').` |
| projects/models.py | BOQItemMaster | 988 | docstring | `catalogue. ONE TABLE, scoped by 'project_type' — not two tables, because everything` |
| projects/models.py | BOQItemMaster.Meta | 1032 | comment | `# which is fine, because every functional query filters by project_type first and` |
| projects/models.py | BOQItemMaster.Meta | 1034 | comment | `# project_type here would silently reorder aggregate_group_boq()'s output.` |
| projects/models.py | get_standard_boq_items | 1052 | docstring | `PART 11 ADDED THE 'project_type' TERM AND NOTHING ELSE. Name, signature and return` |
| projects/models.py | get_standard_boq_items | 1054 | docstring | `produced are precisely the rows migration 0057 stamps 'Residential'. The filter is` |
| projects/models.py | recalculate_dc_status | 2073 | comment | `# here would be a cycle. utils.attach_opex_template() reaches sync_design_mirror()` |
| projects/models.py | PaymentRequest | 2556 | docstring | `use payments.payment_type_q() / payment_project_type(), which read whichever is set.` |
| projects/models.py | VendorOrder | 2983 | comment | `# an order is Residential or RESCO without joining through its sites.` |
| projects/models.py | VendorOrderSite | 3131 | comment | `# Residential, which has no groups, and for a RESCO site picked individually.` |
| projects/models.py | VendorOrderLine | 3205 | comment | `# Single-site Residential lines only. A consolidated RESCO line spans several sites'` |
| projects/models.py | TaskDurationTemplate | 3368 | docstring | `Note also that its unique_together is ('project_type', 'task_name') — phase_name is` |
| projects/models.py | TaskTemplate | 3471 | comment | `# Project.project_type vocabulary ('Residential'), NOT TaskDurationTemplate's` |
| projects/models.py | TaskTemplate | 3472 | comment | `# lowercase 'residential' — this value is compared directly to project.project_type.` |
| projects/models.py | TaskTemplate.Meta | 3486 | comment | `# Activation's hot path is filter(project_type=..., status='active').` |
| projects/models.py | Checklist | 3996 | docstring | `on a task by linking it to one or more (task_name, project_type) pairs via` |
| projects/models.py | ChecklistTaskLink | 4239 | docstring | `and project_type are kept beside it and stay populated, but they are no longer what a` |
| projects/models.py | ChecklistTaskLink | 4257 | docstring | `(task_name, project_type) is kept exactly as it was, because the string columns are` |
| projects/order_views.py | _parse_group_sites | 1201 | docstring | `"""Section 3. Returns (sites, programs, project_type, unfrozen).` |
| projects/order_views.py | _write_order_record | 1596 | docstring | `'cleaned' carries vendor, project_type, po_number, pi_number, order_total, unfrozen,` |
| projects/payment_views.py | <module> | 65 | comment | `#: The three tabs, in order: (stored project_type, label). The label is the product's` |
| projects/payment_views.py | <module> | 66 | comment | `#: word — "RESCO" for the stored 'OPEX' (EXECUTION_MODULE_DEFERRED.md §12).` |
| projects/payment_views.py | payment_mark_paid | 410 | comment | `# site's (5a); both sides are joined here so payment_project_type() costs no query.` |
| projects/payments.py | <module> | 15 | docstring | `* payment_type_q() / payment_project_type() — a payment's project type, from its order` |
| projects/payments.py | payment_type_q | 63 | docstring | `"""A Q over PaymentRequest: the payment's project type is one of 'project_types'.` |
| projects/payments.py | payment_counts | 88 | docstring | `'project_types' filters through payment_type_q() (None = every type); nothing here` |
| projects/permissions.py | project_boq_is_design_locked | 1367 | docstring | `OPEX site (design_views._opex_site 404s everything else), so a Residential project has` |
| projects/purchases_views.py | <module> | 27 | docstring | `own — what a record is (Residential or RESCO), derived from what it is recorded against,` |
| projects/purchases_views.py | <module> | 28 | docstring | `because it is the first page where both kinds can be recorded (_record_project_type).` |
| projects/purchases_views.py | <module> | 64 | comment | `#: The two kinds a record is recorded as here. VendorOrder.project_type stores the` |
| projects/purchases_views.py | <module> | 65 | comment | `#: Project vocabulary, in which RESCO is 'OPEX'. CAPEX has no purchase path yet.` |
| projects/purchases_views.py | purchases_workspace | 124 | docstring | `narrows to Residential or RESCO. 'program' and 'project' narrow to records recorded` |
| projects/purchases_views.py | _record_project_type | 214 | docstring | `"""What the record IS — 'Residential' or 'OPEX' (RESCO) — or None with an error.` |
| projects/purchases_views.py | _record_project_type | 217 | docstring | `tenders. Two kinds is refused — a PO / PI is Residential or RESCO, never both, for` |
| projects/purchases_views.py | _record_project_type | 220 | docstring | `'project_type' choice, required then and ignored otherwise.` |
| projects/purchases_views.py | _new_record_prefill | 310 | docstring | `?program ticks a live RESCO tender. ?project adds that site or Residential project;` |
| projects/purchases_views.py | _new_record_prefill | 311 | docstring | `a RESCO site also ticks its tender, as adding it by ticking its group would."""` |
| projects/purchases_views.py | purchases_new | 362 | docstring | `RESCO tenders and sites, Residential projects, or nothing, and optionally raises its` |
| projects/qaqc_views.py | _visit_site | 1259 | docstring | `not exist for you (the 0.2 shape), and visits exist on RESCO (OPEX) sites only, so` |
| projects/site_pm.py | <module> | 3 | docstring | `Six places put a PM on a site — project_create, create_opex_site (single and bulk),` |
| projects/site_pm.py | change_site_pm | 205 | docstring | `Callers inside their own transaction.atomic() (project_create, create_opex_site,` |
| projects/task_dependencies.py | materialise_task_dependencies | 64 | docstring | `created and inside the same 'transaction.atomic()' — see 'attach_residential_template()'` |
| projects/task_health.py | <module> | 19 | docstring | `- project_type is not part of the rule. tests_task_health holds that true.` |
| projects/templates/projects/admin/boq_items.html | block:content | 96 | comment | `Residential row, so nothing here needs to branch on project_type.` |
| projects/templates/projects/cod_record.html | block:content | 6 | comment | `on a RESCO (OPEX) site — cod_views.cod_record refuses everyone else. While the site` |
| projects/templates/projects/cod_withdraw.html | block:content | 5 | comment | `{% comment %}Closeout step 4. Reached only by the site's PM and coordinators on a RESCO` |
| projects/templates/projects/design/boq_catalogue_form.html | block:<top> | 12 | comment | `The view deletes two fields before this renders, so neither appears: 'project_type',` |
| projects/templates/projects/partials/_checklist.html | block:<top> | 5 | comment | `(task_name + project_type); each row carries its per-(item, task) completion. Item` |
| projects/templates/projects/program_detail.html | block:content | 17 | comment | `RESCO tender — hidden in phase 1. The short tender code is a kept field and` |
| projects/templates/projects/program_form.html | block:content | 67 | comment | `RESCO Tender Details — tender reference number, bid value, award date,` |
| projects/templates/projects/project_overview.html | block:content | 179 | comment | `{% comment %}Closeout step 3: the site's QA/QC engineer, RESCO (OPEX) sites only` |
| projects/templates/projects/project_overview.html | block:content | 204 | comment | `{% comment %}show_design_selector is False on OPEX (RESCO) sites: there the field` |
| projects/templates/projects/project_overview.html | block:content | 285 | comment | `there should be some. 'milestone_create' grew a matching project_type guard in the` |
| projects/templates/projects/project_overview.html | block:content | 289 | comment | `Gated on project_type rather than on 'milestones' being empty. An earlier reading of` |
| projects/templates/projects/project_overview.html | block:content | 296 | comment | `than 'or', so 'project_type == 'Residential' and role == 'PM' or role == 'Finance''` |
| projects/templates/projects/project_overview.html | block:content | 447 | comment | `{% endif %}{# /project_type == 'Residential' — payment milestones card #}` |
| projects/templates/projects/project_overview.html | block:content | 787 | comment | `{% comment %}Closeout step 4 — the COD card, RESCO (OPEX) sites only (show_cod_card).` |
| projects/templates/projects/project_overview.html | block:content | 841 | comment | `{% comment %}Closeout step 7 — the QA/QC visits card, RESCO (OPEX) sites only` |
| projects/templates/projects/purchases_new.html | block:<top> | 12 | comment | `Residential record, RESCO tenders and sites a RESCO one, and the two together are refused.` |
| projects/templates/projects/purchases_new.html | block:<top> | 13 | comment | `Only when nothing is recorded against does the "Residential / RESCO" choice appear, and` |
| projects/templates/projects/site_qaqc.html | block:content | 6 | comment | `on a RESCO (OPEX) site — qaqc_views.site_qaqc refuses everyone else. The eligible` |
| projects/templates/projects/task_detail.html | block:content | 255 | comment | `project_type) via ChecklistTaskLink. Item authoring/assignment is done in portal-admin;` |
| projects/urls.py | <module> | 239 | comment | `# site_qaqc — the site's PM and coordinators; RESCO (OPEX) sites only. Assign,` |
| projects/urls.py | <module> | 242 | comment | `# QA/QC visits (closeout step 7) — RESCO (OPEX) sites only; 404 elsewhere. Start,` |
| projects/urls.py | <module> | 253 | comment | `# Punch point rectify / re-verify loop (closeout 7b) — QA/QC-raised points on RESCO` |
| projects/urls.py | <module> | 261 | comment | `# COD record (closeout step 4) — RESCO (OPEX) sites only; 404 elsewhere. Record and` |
| projects/urls.py | <module> | 500 | comment | `# Reusable Checklists — author name + items, assign to (task_name, project_type). Admin only.` |
| projects/utils.py | <module> | 1164 | comment | `# back (see attach_residential_template) rather than creating tasks unassigned.` |
| projects/utils.py | build_residential_phases | 1188 | docstring | `TaskTemplate 'RESIDENTIAL' v1; attach_residential_template() now reads that instead` |
| projects/utils.py | _residential_duration_resolver | 1521 | comment | `# Project.project_type stores 'Residential'. Two vocabularies, never compared.` |
| projects/utils.py | _attach_task_template | 1622 | docstring | `Shared by attach_residential_template() and attach_opex_template(), which differ` |
| projects/utils.py | attach_opex_template | 1733 | docstring | `Resolves by 'project.project_type' rather than a hardcoded 'OPEX', so a CAPEX` |
| projects/utils.py | attach_residential_template | 1804 | docstring | `Reads TaskTemplate 'RESIDENTIAL' rather than build_residential_phases() since prompt` |
| projects/views.py | _context_types | 361 | docstring | `payments.payment_counts() takes. Reads CONTEXT_PROJECT_TYPES exactly as` |
| projects/views.py | _context_includes | 368 | docstring | `True when 'context' shows projects of 'project_type'. For a section that is not a` |
| projects/views.py | _context_includes | 371 | docstring | `Reads CONTEXT_PROJECT_TYPES like '_context_filter' does, so the two cannot disagree.` |
| projects/views.py | _apply_project_sections | 434 | docstring | `No DB access. 'project_type' is read off the in-memory Project instance; none` |
| projects/views.py | dashboard_pm | 1026 | comment | `# is filtered to Active/In Progress, but 'create_opex_site()' hardcodes status='Draft'` |
| projects/views.py | dashboard_design | 1445 | comment | `# 'create_opex_site()' hardcodes status='Draft' and nothing in the OPEX flow ever` |
| projects/views.py | dashboard_design | 1451 | comment | `# as before; the exemption is scoped to project_type='OPEX' and nothing else.` |
| projects/views.py | <module> | 2097 | comment | `# therefore carries its own project_type term so it cannot match a non-Residential row` |
| projects/views.py | <module> | 2144 | comment | `# project_type term and both rows hide for those types.` |
| projects/views.py | <module> | 2151 | comment | `# project_type equality test, not an inference from the absence of a template, so it` |
| projects/views.py | <module> | 2257 | comment | `# mapping above, and ONLY the CEO dashboard reads what follows. CONTEXT_PROJECT_TYPES,` |
| projects/views.py | <module> | 2258 | comment | `# _context_filter, _context_types, _context_includes and _TENDER_TYPES are left as they` |
| projects/views.py | <module> | 2264 | comment | `# Tenders on the CEO dashboard is RESCO only (decision D2: CAPEX is hidden there).` |
| projects/views.py | <module> | 2267 | comment | `# Every status in which a RESCO site is still part of a tender the CEO tracks: Draft` |
| projects/views.py | _ceo_context_filter | 2277 | docstring | `context, with CEO_TENDER_TYPES in place of the shared Tenders list and test data` |
| projects/views.py | tender_sites_qs | 2332 | docstring | `Every live tender site: RESCO (OPEX), not deleted, not test data, in any` |
| projects/views.py | _get_ceo_dashboard_context | 2801 | comment | `# BRANCH BY project_type. The two design sources are not interchangeable: the` |
| projects/views.py | _get_ceo_dashboard_context | 2967 | comment | `# S1: Tenders reads CEO_TENDER_TYPES (RESCO only), so CAPEX money is off this tile while` |
| projects/views.py | _active_project | 3570 | docstring | `way. '_opex_site()' in design_views.py is the precedent this copies: every design` |
| projects/views.py | opex_site_create | 5174 | docstring | `Admin, or a PM who can access it. The creation core lives in create_opex_site() so` |
| projects/views.py | <module> | 5197 | comment | `# ALL-OR-NOTHING batch, reusing create_opex_site() (and therefore OpexSiteForm)` |
| projects/views.py | _validate_site_row_dry_run | 5263 | docstring | `"""Validate ONE would-be site with the REAL create_opex_site path, then throw the` |
| projects/views.py | _validate_site_row_dry_run | 5265 | docstring | `form by hand (which would silently drift the moment create_opex_site changes).` |
| projects/views.py | _validate_site_row_dry_run | 5267 | docstring | `HOW IT WORKS: create_opex_site() only reaches the DB for a *valid* row — an invalid` |
| projects/views.py | _validate_site_row_dry_run | 5269 | docstring | `our own transaction.atomic(); for a valid row create_opex_site performs the real` |
| projects/views.py | opex_site_bulk_upload | 5484 | comment | `# Batch-level audit entry (in addition to create_opex_site's per-site logs).` |
| projects/views.py | _phase_list_two_step | 5623 | docstring | `tests 'project.project_type == 'OPEX'' inline is one more place the scope of` |
| projects/views.py | _task_approval_context | 6026 | docstring | `never asks it: a template that tests 'project.project_type == 'OPEX'' inline is` |
| projects/views.py | _checklist_task_link_for | 6250 | docstring | `share, so it is the thing matched. project_type is carried on the join so a` |
| projects/views.py | boq_detail | 8685 | comment | `# PART 11 — THE project_type TERM IS REQUIRED, NOT COSMETIC. This dict is keyed` |
| projects/views.py | _validate_boq_rows | 9732 | comment | `# project_type='OPEX' — so "is it a real code", "is it an OPEX code" and` |
| projects/views.py | project_overview | 11034 | comment | `# THE HEADER DESIGN SELECTOR IS NOT OFFERED ON OPEX (RESCO) SITES.` |
| projects/views.py | project_overview | 11049 | comment | `# Tested against the STORED constant, never the 'RESCO' display label.` |
| projects/views.py | project_overview | 11521 | comment | `# B18: the project_type term is the visible half of the refusal in` |
| projects/views.py | task_detail | 11999 | comment | `# Checklist — items come from the Checklist linked to this (task_name, project_type);` |
| projects/views.py | admin_project_list | 15487 | comment | `# ?type= carries the STORED project_type ('OPEX', not its 'RESCO' label).` |
| projects/views.py | _checklist_task_name_choices | 15885 | docstring | `Returns [(project_type, type_label, [{'pk','code','task_name','phase_name'}, ...])],` |
| projects/views.py | _resolve_checklist_link_target | 15928 | docstring | `task_name=<label>&project_type= — the pre-2.4 pair, resolved by looking the label` |
| projects/views.py | admin_checklists | 15985 | docstring | `task_name/project_type pairs). Access: Admin only."""` |
| projects/views.py | admin_checklist_edit | 16050 | comment | `# project_type select the template used to render is gone, and with it the` |
| projects/views.py | admin_checklist_link_add | 16310 | docstring | `SINCE 2.4 THE LINK IS CREATED AGAINST THE TEMPLATE TASK. task_name and project_type` |
| projects/views.py | admin_checklist_link_add | 16313 | docstring | `The POST may still carry the old (task_name, project_type) pair — see` |
| projects/views.py | admin_checklist_link_add | 16350 | comment | `# task_name/project_type are deliberately NOT passed: save() derives them from the FK.` |
| projects/views.py | admin_boq_items | 16400 | docstring | `PART 11 added the project_type filter and the explicit ordering. The catalogue holds` |

---

## Appendix 2 — raw SQL output for sections C and D

Same read-only transaction as §F.

```
== C1 active OPEX template phases
SQL: SELECT ph.sort_order, ph.code, ph.label, count(tt.id) FROM projects_tasktemplate t JOIN projects_tasktemplatephase ph ON ph.template_id=t.id LEFT JOIN projects_tasktemplatetask tt ON tt.phase_id=ph.id WHERE t.project_type='OPEX' AND t.status='active' GROUP BY 1,2,3 ORDER BY 1
sort_order | code | label | count
1 | DESIGN | Design | 1
2 | APPROVALS_PRE_INSTALLATION | Approvals (Pre-Installation) | 2
3 | PROCUREMENT_DELIVERY | Procurement & Delivery | 4
4 | INSTALLATION | Installation | 9
5 | TESTING_COMMISSIONING | Testing & Commissioning | 2
6 | APPROVALS_POST_INSTALLATION | Approvals (Post-Installation) | 1
7 | CLOSEOUT | Closeout | 4
(7 rows)
```

```
== C2 active OPEX template tasks + checklist links
SQL: SELECT ph.sort_order AS ph, tt.sort_order AS ord, tt.code, tt.label, ph.code AS phase_code, tt.assigned_role, tt.task_type, tt.is_payment_milestone, tt.is_mirror, tt.duration_days, (SELECT count(*) FROM projects_checklisttasklink l JOIN projects_tasktemplatetask t2 ON t2.id=l.template_task_id JOIN projects_tasktemplatephase p2 ON p2.id=t2.phase_id JOIN projects_tasktemplate tp2 ON tp2.id=p2.template_id WHERE t2.code=tt.code AND tp2.project_type='OPEX') AS links_by_code, (SELECT string_agg(c.code || ' v' || c.version_no || ' ' || c.status, '; ') FROM projects_checklisttasklink l JOIN projects_checklist c ON c.id=l.checklist_id JOIN projects_tasktemplatetask t2 ON t2.id=l.template_task_id JOIN projects_tasktemplatephase p2 ON p2.id=t2.phase_id JOIN projects_tasktemplate tp2 ON tp2.id=p2.template_id WHERE t2.code=tt.code AND tp2.project_type='OPEX') AS linked_checklist FROM projects_tasktemplate t JOIN projects_tasktemplatephase ph ON ph.template_id=t.id JOIN projects_tasktemplatetask tt ON tt.phase_id=ph.id WHERE t.project_type='OPEX' AND t.status='active' ORDER BY 1,2
ph | ord | code | label | phase_code | assigned_role | task_type | is_payment_milestone | is_mirror | duration_days | links_by_code | linked_checklist
1 | 1 | DESIGN | Design | DESIGN | Design | Internal | False | True | 1 | 0 | 
2 | 1 | NET_METERING_APPROVAL | Net Metering Approval | APPROVALS_PRE_INSTALLATION | PM | Internal | False | False | 1 | 0 | 
2 | 2 | CEIG_APPROVAL | CEIG Approval | APPROVALS_PRE_INSTALLATION | PM | Internal | False | False | 1 | 0 | 
3 | 1 | DELIVERY_SOLAR_PANELS | Delivery — Solar Panels | PROCUREMENT_DELIVERY | SCM | Internal | False | True | 1 | 0 | 
3 | 2 | DELIVERY_INVERTERS | Delivery — Inverters | PROCUREMENT_DELIVERY | SCM | Internal | False | True | 1 | 0 | 
3 | 3 | DELIVERY_BOS_KIT | Delivery — BOS Kit | PROCUREMENT_DELIVERY | SCM | Internal | False | True | 1 | 0 | 
3 | 4 | DELIVERY_MMS | Delivery — MMS | PROCUREMENT_DELIVERY | SCM | Internal | False | True | 1 | 0 | 
4 | 1 | CIVIL_WORK_AND_MMS_INSTALLATION | Civil Work and MMS Installation | INSTALLATION | Site Engineer | Internal | False | False | 1 | 1 | OPEX-INST-CIVIL-MMS v1 active
4 | 2 | MODULE_INSTALLATION | Module Installation | INSTALLATION | Site Engineer | Internal | False | False | 1 | 1 | OPEX-INST-MODULE v1 active
4 | 3 | LA_AND_EARTHING_INSTALLATION | LA and Earthing Installation | INSTALLATION | Site Engineer | Internal | False | False | 1 | 1 | OPEX-INST-LA-EARTHING v1 active
4 | 4 | DC_CABLE_LAYING_WITH_CONDUIT | DC Cable Laying with Conduit | INSTALLATION | Site Engineer | Internal | False | False | 1 | 1 | OPEX-INST-DC-CABLE v1 active
4 | 5 | DCDB_AND_ACDB_INSTALLATION | DCDB and ACDB Installation | INSTALLATION | Site Engineer | Internal | False | False | 1 | 1 | OPEX-INST-DCDB-ACDB v1 active
4 | 6 | INVERTER_INSTALLATION | Inverter Installation | INSTALLATION | Site Engineer | Internal | False | False | 1 | 1 | OPEX-INST-INVERTER v1 active
4 | 7 | AC_CABLE_LAYING | AC Cable Laying | INSTALLATION | Site Engineer | Internal | False | False | 1 | 1 | OPEX-INST-AC-CABLE v1 active
4 | 8 | RMS_INSTALLATION | RMS Installation | INSTALLATION | Site Engineer | Internal | False | False | 1 | 0 | 
4 | 9 | SOLAR_GENERATION_METER_INSTALLATION | Solar Generation Meter Installation | INSTALLATION | Site Engineer | Internal | False | False | 1 | 0 | 
5 | 1 | TESTING_COMMISSIONING | Testing & Commissioning | TESTING_COMMISSIONING | Site Engineer | Internal | False | False | 1 | 0 | 
5 | 2 | NET_METER_INSTALLATION | Net Meter Installation | TESTING_COMMISSIONING | Site Engineer | Internal | False | False | 1 | 0 | 
6 | 1 | POST_INSTALLATION_APPROVALS | Post-Installation Approvals | APPROVALS_POST_INSTALLATION | PM | Internal | False | False | 1 | 0 | 
7 | 1 | COD | COD | CLOSEOUT | PM | Internal | False | True | 1 | 0 | 
7 | 2 | COMPLETION_CERTIFICATES_PAPERWORK | Completion Certificates (Paperwork) | CLOSEOUT | Project Coordinator | Internal | False | False | 1 | 0 | 
7 | 3 | AS_BUILT_DRAWINGS | As-Built Drawings | CLOSEOUT | Design | Internal | False | True | 1 | 0 | 
7 | 4 | HOTO | HOTO | CLOSEOUT | PM | Internal | False | True | 1 | 0 | 
(23 rows)
```

```
== C3 checklist links by project_type and keyed/unkeyed
SQL: SELECT project_type, (template_task_id IS NOT NULL) AS keyed, count(*) FROM projects_checklisttasklink GROUP BY 1,2 ORDER BY 1,2
project_type | keyed | count
OPEX | True | 7
Residential | True | 1
(2 rows)
```

```
== D1 template task codes shared across templates
SQL: SELECT tt.code, string_agg(DISTINCT t.code || '/' || t.project_type, ', ') FROM projects_tasktemplatetask tt JOIN projects_tasktemplatephase ph ON ph.id=tt.phase_id JOIN projects_tasktemplate t ON t.id=ph.template_id GROUP BY tt.code HAVING count(DISTINCT t.code) > 1 ORDER BY 1
code | string_agg
DESIGN | OPEX/OPEX, RESIDENTIAL/Residential
INVERTER_INSTALLATION | OPEX/OPEX, RESIDENTIAL/Residential
MODULE_INSTALLATION | OPEX/OPEX, RESIDENTIAL/Residential
(3 rows)
```

---

## Appendix 3 — scripts used

All four are read-only and lived in the session scratchpad, not the repo: `hits.py` (hit enumeration and code/comment/docstring classification, regex in B.0), `split.py` (chunking), `assemble.py` (merge plus the 486 = 486 set check and 8-cell check), and `sqlf.py` / `sqlf2.py` / `sqlf3.py` (the SQL above, each wrapped in `BEGIN; SET TRANSACTION READ ONLY; SET default_transaction_read_only = on; … ROLLBACK`).
