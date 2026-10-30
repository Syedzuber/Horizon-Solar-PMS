# ATT-A — Contractor Team Attendance: Read-Only Audit

Date: 30 Oct 2026. Branch `main` at `77a12b0` (up to date with `origin/main`), migrations
applied through `0125_deliverychallanrevision`. No code, template, migration, model,
setting or data was changed. Database facts come from the **local** database
(`solarpms_local` on `localhost`, printed below). **Production was not queried: every
count here is NOT CHECKED against production.**

Tags: **VERIFIED** = seen in code or data; **INFERRED** = reasoned, not seen.

---

## A. Existing attendance or manpower features

**A.1 — search.** Whole repo, `.py` / `.html` / `.js`, venv and staticfiles excluded:

```
$ grep -rniE "attendance|manpower|headcount|head_count|man_day|manday|man-day|daily_log|site_diary|diary" --include=*.py --include=*.html --include=*.js -l .
./projects/models.py
./projects/qaqc_views.py
./projects/templates/dashboard/design.html
./projects/tests_morning_task_report.py
./projects/views.py
```

Every hit is a false positive (VERIFIED, lines shown):

```
projects/models.py:238:    client_name = models.CharField(max_length=200)  # Govt subsidiary (OPEX) ...   <- "diary" in "subsidiary"
projects/qaqc_views.py:157:    the exclusion was lifted for manpower, and what keeps the check independent is   <- docstring, CL-L ruling
projects/templates/dashboard/design.html:142..253   head_counts.*                                    <- Design Head queue counts
projects/tests_morning_task_report.py:156:    def test_two_queries_whatever_the_head_count(self):
projects/views.py:1580:        'head_counts': design_head_dashboard_counts(request.user),
```

```
$ grep -rniE "labou?r" --include=*.py --include=*.html -n projects solarpms | grep -v /migrations/
(no output)
$ grep -rliE "labou?r" projects/migrations
(no output)
```

`worker`: test fixture names (`self.worker` in tests_cod_record, tests_g85_…, tests_notify_migration_c), plus
`checklist_pdf.py:114` ("gunicorn worker"). `crew`: `seed_opex_test_data.py:134`
`GROUP_EXECUTION_NAME = f'{DEMO_PREFIX} Execution Crew A'`, test group names, and comments in `views.py:11354-11358`.

The "Execution Crew A" hit is a **SiteGroup of `group_type='execution'`** (`models.py:6036-6043`,
`6083-6165`). It is the PM's batch of sites. It has `program`, `name`, `status`, `group_type`, `created_by`,
`locked_by`, `notes` and no people, headcount or vendor field. **Not an attendance record.**

The documentation search found one mention, `docs/CLOSEOUT_SPEC.md:44` ("Nirankar Mishra / product owner, manpower"), which is a
person's department, not a feature.

**A.2 — conclusion (VERIFIED):** no model, field or screen records people on site. No stop
condition. The audit continued.

---

## B. Contractor / service-vendor entity

**B.1 — outside-party models (VERIFIED, `projects/models.py`):**

| Model | Lines | Fields | Kind flag |
|---|---|---|---|
| `VendorCategory` | 876-886 | `name` (unique) | none. It is a supply category (Solar Modules, Inverter, …, plus a `Services` row in data) |
| `Vendor` | 905-940 | `name, contact_person, phone, email, gst_number, msme_status, msme_number, address, categories (M2M VendorCategory), is_active, kind, created_by, created_at, updated_at` | **`kind`**, choices at 893-899: `supplier` / `contractor` / `both`. CHECK `vendor_kind_known` (934-936) |
| `VendorBrand` | 943-970 | `vendor, make_brand, category` | none (material make labels) |

```
projects/models.py:893  VENDOR_KIND_SUPPLIER   = 'supplier'
projects/models.py:894  VENDOR_KIND_CONTRACTOR = 'contractor'
projects/models.py:895  VENDOR_KIND_BOTH       = 'both'
projects/models.py:902  VENDOR_BILLABLE_KINDS = frozenset({VENDOR_KIND_CONTRACTOR, VENDOR_KIND_BOTH})
projects/models.py:916  kind = models.CharField(max_length=20, choices=VENDOR_KIND_CHOICES,
projects/models.py:917      default=VENDOR_KIND_SUPPLIER)  # Supplier / contractor / both — decides only whether a contractor bill may name this vendor (D-A38)
```

There is no separate `Contractor` model. The material/service split is exactly the `kind`
column. `VendorForm` exposes it (`forms.py:740-771`). Only SCM and Admin may add or edit a vendor:
`views.py:8319`, `8363`, `8407` `if profile.role not in ('SCM', 'Admin'): return HttpResponseForbidden()`.

**B.2 — contractor-billing models (VERIFIED):**

- `ContractorBillDetail` (`models.py:7726-7776`) is 1:1 with an `ApprovalRequest` of kind `contractor_bill`.
  - **Contractor identity:** not on the detail. It is `ApprovalRequest.vendor`, a **FK to `Vendor`, PROTECT** (`models.py:7255-7258`).
    It is required for the bill kind by CHECK `approval_request_vendor_required_for_kind` (`7296-7300`). The chokepoint
    refuses a vendor whose `kind` is not in `VENDOR_BILLABLE_KINDS` or that is inactive (`approvals.py:737-747`).
    The raise page offers `contractor_choices()`, which returns active contractor/both vendors (`approval_forms.py:657-661`).
  - **Site link:** `ContractorBillDetail.project` FK PROTECT, one project per bill (`7747-7748`).
  - **Man-days / labour / quantity:** **none.** The fields are `amount`, `bill_number`, `bill_date`, the `pdf_*` fields and
    `tasks` (M2M through `ContractorBillTask`, `7779-7802`). `amount` is "as billed … not checked against any rate (B-16)" (`7750`).
- **Work-completion proof:** the SE's step on the bill's `ApprovalStep` must carry at least one site photo
  (`approvals.py:68-71`, `176-179` `_SITE_PHOTO_SUFFIXES = ('.jpg', '.jpeg', '.png')`). The photos are stored as
  `ApprovalAttachment` rows (`models.py:7456-7503`): request, round, file fields, `uploaded_by`, `uploaded_at`, optional `step`.
  They are append-only.

**B.3 — counts (local DB only):**

```
$ python manage.py shell -c "exec(open('.../q_b.py').read())"
Vendor total: 6
Vendor by kind: [{'kind': 'supplier', 'n': 6}]
Vendor by kind+is_active: [{'kind': 'supplier', 'is_active': True, 'n': 6}]
VendorCategory: [('Other', 2), ('Solar Modules', 3), ('Inverter', 3), ('Services', 1), ('Structure', 1), ('BOS', 2)]
Contractor-kind vendors: []
ContractorBillDetail total: 0
Bill vendors: []
Bills by project_type: []

$ python manage.py shell -c "...for v in Vendor.objects...; print DB NAME"
1 | horizon renewable power | supplier | True | ['Inverter', 'Other', 'Services']
2 | horizon | supplier | True | ['Inverter', 'Solar Modules']
8 | ORDDEMO Cable & BOS Supplies | supplier | True | ['BOS', 'Other']
9 | ORDDEMO Inverter & Switchgear Supplies | supplier | True | ['BOS', 'Inverter']
10 | ORDDEMO Modules & Structure Supplies | supplier | True | ['Solar Modules', 'Structure']
16 | DR2 Walk Vendor | supplier | True | ['Solar Modules']
DB NAME: solarpms_local localhost
```

Locally there are **0 service contractors and 6 material suppliers**. Production counts: **NOT CHECKED.**

**B.4 — conclusion.** The entity is **not ambiguous**. Attendance can point to `Vendor` by FK today and
restrict it to `kind in VENDOR_BILLABLE_KINDS` and `is_active`. That is the same rule and the same
helper shape the contractor bill already uses (VERIFIED). What is missing is **data** (no contractor rows
locally; production unknown) and **any site ↔ contractor link**. Nothing records which contractors work on which site,
so a picker can only be "every active contractor" (as the bill raise page does) unless a ruling adds one.
See Blocker 2. No stop condition is triggered.

---

## C. SE ↔ site assignment and permissions

**C.1 — how an SE is linked to a project (VERIFIED).** Only by **holding a task** (`Task.assigned_to`). There is no
field on `Project` and no through table for SEs.

```
projects/permissions.py:322    if role == 'Site Engineer':
projects/permissions.py:325        if project.phases.filter(tasks__assigned_to=profile).exists():
projects/permissions.py:334            if qaqc_grant or site_qaqc_engineer_id(project) != profile.pk:
```

The dashboard selects the same way (`views.py:1224-1228`):
`Project.objects.filter(is_deleted=False, status__in=['Active', 'In Progress'], phases__tasks__assigned_to=se_profile)`.
The task's status does not matter. An SE whose tasks on a site are all Done still "holds a task" there (VERIFIED: no status term).

**C.2 — "can this user see / act on this project" helpers (`projects/permissions.py`, VERIFIED):**

| Helper | Line | Answers |
|---|---|---|
| `user_can_manage_project` | 39 | PM-level authority: assigned PM OR coordinator |
| `manageable_projects_q` | 69 | queryset form of the above |
| `managed_project_ids_by_profile` | 97 | bulk form, active profiles only |
| `user_can_view_project` | 143 | visibility (role-shaped, includes the QA/QC engineer's view-only grant) |
| `user_works_on_site` | 200 | view **minus** the QA/QC grant, i.e. a working relationship |
| `user_works_on_task` | 233 | `user_works_on_site` OR assignee of that task |
| `project_managers` | 1534 | list of PM + **active** coordinators (for notifications) |

An SE holding a task passes **`user_can_view_project`** and **`user_works_on_site`**. The second is False for
the site's QA/QC engineer even if that engineer holds tasks there (`permissions.py:200-231`, branch at 334).

**C.3 — PM vs coordinator in `user_can_manage_project()` (VERIFIED):**

```
projects/permissions.py:64    if project.assigned_pm == profile:          # PM authority — always checked, never gated
projects/permissions.py:65        return True
projects/permissions.py:66    return project.coordinators.filter(pk=profile.pk).exists()  # additive coordinator authority
```

PM and coordinator are equal here, OR'd unconditionally. **It checks no `is_active` flag.** `project_managers()`
does (`permissions.py:1549` `for coord in project.coordinators.filter(is_active=True):`).
"Active" at request time relies on login. The Admin user-edit view sets `User.is_active` and `UserProfile.is_active`
together (`views.py:3519`, `3526`). Settings name no `AUTHENTICATION_BACKENDS`, so Django's default `ModelBackend`
applies, and it refuses an inactive `User` (INFERRED from Django defaults). The result: a coordinator deactivated through the
product cannot reach the edit at all. **Conclusion: `user_can_manage_project()` is the right gate for "PM or
coordinator edits after the day".** The gap where `UserProfile.is_active=False` while `User.is_active=True` is
reachable only through the Django admin or shell (INFERRED) and is logged in SECONDARY_FINDINGS.

**C.4 — narrow helper patterns to copy (VERIFIED):**

- `user_can_view_site_drawings` (`1087-1104`): `return user_can_view_project(user, project) and project.project_type == 'OPEX'`.
  It is the canonical "existing helper AND project-type" shape from the Drawings & BOQ work.
- `user_can_conduct_qaqc_visit` (`2267-2288`): an OPEX/is_deleted guard, then delegation to one authority helper.
  The per-day rule stays in the writer, re-checked under a lock (docstring 2281-2282).
- `user_can_edit_dc` (`2623-2640`): role-only; its docstring states the caller must ask view first.

INFERRED shape for attendance: a create helper = `user_works_on_site()` plus the Site Engineer role term, written
inside `permissions.py`, so the QA/QC engineer is excluded by construction. A correct-after-the-day helper = `user_can_manage_project()`.
Not designed further here.

---

## D. SE mobile path and placement

**D.1 — landing (VERIFIED).** `login_view` (`views.py:457-481`) → `get_post_login_url` (`decorators.py:76-94`). SE is not in
`LANDING_ROLES` (`decorators.py:44`), so `get_user_dashboard` → `ROLE_DASHBOARD['Site Engineer'] = '/dashboard/site-engineer/'`
(`decorators.py:30`) → `urls.py:31` → `dashboard_site_engineer` (`views.py:1200-1202`, `@role_required(['Site Engineer'])`)
→ `projects/templates/dashboard/site-engineer.html`.

The dashboard has 7 stat cards (lines 45-111), the "My punch points" card (132), `_qaqc_engineer_sections.html` (160), and then
**one card per site, collapsed by default** (`x-data="{ expanded: false }"`, line 175). Inside the expanded body (219-296) are the current
phase, a progress bar, urgency chips, the next task, and two buttons, **Raise Issue** (281-286, a shared modal) and **View Project**
(288-293 → `project_overview`).

**D.2 — project overview as an SE sees it** (`projects/templates/projects/project_overview.html`, 2367 lines, VERIFIED), in order:

| # | Block | Lines | SE sees? | Default |
|---|---|---|---|---|
| 1 | Project info card | 84-240 | yes | always open |
| 1b | Site drawings card | 242 (include) | OPEX only (`state != 'not_opex'`) | **closed** (DR-2c-1) |
| 2a | Phase Progress | 248-274 | yes if phases | open |
| 2b | Payment Milestones | 299-446 | no (Residential + PM/Finance/CEO/Admin/BD) | — |
| 2c | Recent Activity | 448-489 | yes | open (`collapse show`, 471) |
| 3a | BOQ | 495-554 | no (manages_as_pm / SCM / Design / Admin) | — |
| 3b | Delivery Challans | 558-628 | yes (role list includes SE) | open (`dcCollapse show`) |
| 3c | Material Status | 630-704 | yes | open (`materialsCollapse show`) |
| 4b | Vendor orders | 717-784 | no (`show_vendor_orders`, PM on managed site) | — |
| 4c | COD card | 791-838 | OPEX (`show_cod_card`) | open |
| 4d | QA/QC visits card | 846-902 | OPEX (`show_qaqc_visit_card`) | open |
| 4e | Started-early card | 906-908 | only if overrides exist | — |
| 5 | Project Documents | 910-976 | yes | open |
| 5b | Cascade scheduling | 991-1028 | PM only | — |
| 5.5 | Gantt | 1031-1104 | Residential (`gantt_available`) | **closed** |
| 6 | Phase-wise task list | 1106-1147 | yes | open |
| 7 | Project Issues | 1149-1209 | yes | open |

**D.3 — candidate entry points (proposal only, not built):**

1. **SE dashboard site card, beside "Raise Issue" / "View Project"** (`site-engineer.html:279-293`). It takes **two taps** from
   landing: expand the card, then tap Add attendance. This is where the SE already lands, the card is already per-site, and
   "Raise Issue" is the precedent for a site-scoped action launched from this row. A "logged today: yes/no" chip could sit
   among the urgency chips (237-262) at no extra tap. Cost: the dashboard view's query budget (counts are annotated, 1230-1278).
2. **A new overview card near the top** (after block 1/1b, before block 2), open by default, holding today's entries and
   an Add button. It is the natural home for the day's list and the PM/coordinator corrections. But from landing it is **three
   taps** (expand → View Project → Add), so it should not be the only entry point.

Recommendation: entry from (1), list and corrections on (2).

---

## E. Photo capture and storage

**E.1 — existing pattern (VERIFIED):**

- **Field and capture:** `<input type="file" name="photo" accept=".jpg,.jpeg,.png" capture="environment" required>` on the SE
  checklist (`partials/_checklist.html:164`). The comment at 151-162 explains why `capture` is used only on photo-only inputs.
  The same attribute appears on the QA/QC point (`partials/_qaqc_point.html:38`) and punch-point rounds (`partials/_punch_points.html:153`, `168`).
  The GRN damage-photo input (`delivery_challan_detail.html:426-430`) is `multiple` and has **no** `capture`.
- **Client-side prep:** `partials/_qaqc_photo_script.html` (included by `qaqc_visit.html:103`, `task_detail.html:77`,
  `delivery_challan_detail.html:756`). It reads EXIF DateTimeOriginal (tag `0x9003`) into a hidden `photo_taken_at`, and
  downscales to a 1600 px long edge at JPEG quality 0.8 (`MAX_EDGE = 1600, QUALITY = 0.8`, line 26). On any failure it uploads the original.
- **Storage helper:** `projects/delivery_storage.py`.
  - `validate_dc_document` (74-104) checks the extension, rejects an empty file, enforces `DC_DOCUMENT_MAX_BYTES = 20 * 1024 * 1024` (40), checks the claimed MIME
    against an allowlist (92-95), and checks the **magic bytes** (96-103).
  - `validate_damage_photo` (199-206) narrows that to jpg/jpeg/png.
  - `upload_damage_photo` (209-235) writes to the private delivery bucket at `build_design_path(project_id, 'grn-damage', name)`
    and returns `{original_name, bucket, path, size, mime}`. It never returns a URL.
  - `discard_unrecorded_damage_photo` (238-252) cleans up after a failed write.
  - `damage_photo_url` (255-269) signs a link at the click, valid for `DC_DOCUMENT_LINK_SECONDS = 300` (37).
  - QA/QC visit photos use the private **bills** bucket instead (`models.py:7050` docstring "bills bucket, no URL stored";
    `qaqc_views.start_qaqc_visit` refuses when `bills_bucket()` is empty, `qaqc_views.py:1038-1039`). `bill_storage.BILL_LINK_SECONDS = 900` (35).
- **Server-side 20 MB, extension, MIME and magic-byte checks apply whatever the browser did** (comment, `delivery_storage.py:180-184`).

**E.2 — EXIF and server time (VERIFIED):**

- **EXIF: yes, browser-side only.** `qaqc_views.parse_photo_taken_at` (976-995) parses the posted string as IST. It drops anything
  before 2020 or more than a day ahead (546-548) and never refuses. It is stored as `photo_taken_at` on `QaqcVisitAnswer`
  (`models.py:7076-7078`) and `PunchPointEvent` (`6590`). **The server never reads EXIF itself.** `requirements.txt` has no Pillow, and there is no `PIL`
  import (`grep -inE "pillow|exif" requirements.txt` → no output; `grep -rnE "from PIL" projects` → no output).
  The canvas re-encode likely strips EXIF from the stored file (INFERRED from browser canvas behaviour), so the stored object cannot be
  re-checked later.
- **Separate server time: yes.** `QaqcVisitAnswer.answered_at = models.DateTimeField(default=timezone.now)  # server time of the latest write`
  (`7079`) beside `photo_taken_at`. Its comment says `answered_at` "is the authority". `DCLineDamagePhoto.uploaded_at` (`models.py:1859`) and
  `ApprovalAttachment.uploaded_at` (`7478`) are server time only.

Note for ruling 5 ("taken at the time"): `capture="environment"` asks for the camera but **does not prevent** picking a gallery
file on every phone (INFERRED, since browser behaviour varies). The only "at the time" evidence the codebase has is the EXIF time next to the
server time. It is shown, never enforced.

---

## F. Same-day rule, time zone and day boundaries

**F.1 (VERIFIED):**

```
solarpms/settings.py:135  TIME_ZONE = 'Asia/Kolkata'  # IST — all datetimes stored as UTC, displayed as IST
solarpms/settings.py:137  USE_TZ = True
```

**F.2 — one-per-site-per-day (VERIFIED): `QaqcVisit`.**

- `visit_date = models.DateField()  # IST day the visit was started (V-1)` (`models.py:6977`).
- Partial unique `uniq_qaqc_visit_site_day` on `(project, visit_date)` excluding lapsed (`6989-6994`).
- Writer `start_qaqc_visit` (`qaqc_views.py:1030-1074`): `today = timezone.localdate()` (1041), `select_for_update()` on the project
  row (1046), re-check `visit_date=today` (1054-1056), and `IntegrityError` → refusal as a backstop (1067-1068).

This is IST-correct. `timezone.localdate()` converts `now()` to `TIME_ZONE`.

There is no existing "editable only on the same day" rule. `checklist_answers_open` (`permissions.py:2342`) closes on task
status, not on a date.

How "today" is computed across the code (VERIFIED counts):

```
$ grep -rnE "timezone\.localdate\(\)" projects --include=*.py | grep -v /tests | wc -l
57
$ grep -rnE "date\.today\(\)" projects --include=*.py | grep -v /tests | wc -l
15
```

`report_views.py:38-39` states the rule: "timezone.localdate() — NOT date.today()". The overview itself still passes
`'today': date.today()` (`views.py:11637`) (see SECONDARY_FINDINGS). Attendance must use `timezone.localdate()`.

**F.3 — cron entry points:**

- 8 PM IST: `projects/management/commands/send_eod_digest.py`. `today = timezone.localdate()` (132), and activity is filtered
  `timestamp__date=today` (218, 444). Its schedule is **not in the repo**: `Procfile` has only `web:`, and there is no `railway.json`,
  `railway.toml` or `railpack.json` in the root. The only record is `docs/DESIGN_MODULE_NOTIFICATIONS_SPEC.md:279`: "run from the Railway cron service
  `laudable-cat` (`30 14 * * *` UTC = 20:00 IST)", and item 8 at line 623 says its start command "was not read". **NOT VERIFIED.**
- Morning report: `projects/management/commands/send_morning_task_report.py` with `report_date = timezone.localdate()` (116). The 10 AM
  schedule is **NOT VERIFIED**: no file in the repo records it.
- **"Open entry, no time-out" flag.** It can be a **derived predicate, not a stored flag**:
  `time_out IS NULL AND attendance_date < timezone.localdate()` (or `= today` once the 8 PM run fires). That is the same way
  overdue is derived live by `task_health.overdue_q(today)`, which the SE dashboard and morning report both call
  (`views.py:1245`, `task_report.py:192`). Computing it at report or render time needs no signal, no `save()` override and no
  cron write (INFERRED design note, not built). A cron job that *writes* a flag would be a second writer of row state. Nothing
  here needs one.

---

## G. Edit audit pattern

The existing per-edit records (VERIFIED, `projects/models.py`) are below.

| Model | Lines | Shape |
|---|---|---|
| **`DeliveryChallanRevision`** (DC-B3, newest) | 1918-1972 | `challan` FK PROTECT, `revision_no` (unique with challan, 1-based, optimistic count posted by the form), `revised_by` FK PROTECT, `revised_at` default now, `reason` TextField + CHECK non-empty, `before` JSON, `after` JSON (snapshot schema 1). Append-only: `save()` on existing / `delete()` raise `AppendOnlyViolation` |
| `BOQCorrection` | 1306-1380 | per-row action, `quantity_before` / `_after`, `corrected_by` SET_NULL, `corrected_at` |
| `ProjectFieldEditLog` | 701-746 | one row per changed field, `old_value` / `new_value` text, `edited_by` SET_NULL, `reason` (blank allowed) |
| `DueDateChangeLog` | 685-698 | `old_date` / `new_date`, `changed_by`, `changed_at` |
| `PunchPointEvent` | 6544 | event rows with action, actor, remark and photo |
| `ActivityLog` | 1522-1544 | feed line with `action_code` (machine key), `entity_type` / `entity_id` |

**Recommendation (INFERRED):** copy `DeliveryChallanRevision`, which is the current house shape. It has a whole-record before/after
JSON snapshot, a mandatory reason as a CHECK, PROTECT FKs, append-only, a unique `(entry, revision_no)` guard against two concurrent edits,
and it is written in the same transaction as the edit under a row lock (`dc_edit.py`). Add one `ActivityLog` line with an
`action_code`, as DC-B3 does (`dc_edited`).

---

## H. Residential effort assessment

**H.1 — SE assignment: same mechanism (VERIFIED).** `_project_visibility`'s SE branch (`permissions.py:322-334`) and
`dashboard_site_engineer`'s queryset (`views.py:1224-1228`) have **no project-type term**:

```
$ sed -n 1200,1420p projects/views.py | grep -nE "project_type"
(no output)
$ grep -nE "project_type" projects/templates/dashboard/site-engineer.html
(no output)
```

Local data shows SEs hold tasks on both types:

```
Projects by type/status: [CAPEX Active 1, OPEX Active 8, OPEX Draft 101, Residential Active 26, Residential Draft 7]
SE-role tasks by project_type (assigned / unassigned): [OPEX 74/10, Residential 207/158]
Distinct SE users holding a task, by project_type: [OPEX: 4 people / 7 projects, Residential: 9 people / 18 projects]
```

**H.2 — same page (VERIFIED).** Both types render `projects/project_overview.html` from one view (`views.py:11006`, `11597`). Blocks
differ only by `project_type` conditions (template lines 116, 299, 1283; plus `gantt_available`, `site_drawings.state`,
`show_cod_card`, `show_qaqc_visit_card` computed in the view).

**H.3 — same contractor entity (VERIFIED for the code, NOT CHECKED for the business).** `Vendor` has no project-type
field. Contractor bills are not type-gated: `bill_sites()` = `Project.objects.filter(is_deleted=False, is_test=False).exclude(status='Draft')`
(`approval_forms.py:632-639`), and `_clean_bill_site` refuses only Draft (`approvals.py:755-760`). **Whether Residential work is
actually done by third-party contractors was not checked.** There are 0 contractor vendors and 0 bills locally, and it is a business fact the code cannot
answer.

**H.4 — OPEX-only gates an SE passes (VERIFIED).** None on the dashboard (above). On the overview the SE-relevant
gates are additive cards keyed on `project_type`. A new attendance card or button would need at most one project-type condition
(the `user_can_view_site_drawings` shape) if scope is limited, and none if it is not.

**Verdict: Residential: include.** Items 1-3 use the same mechanism and item 4 needs nothing beyond a project-type condition.
The one caveat: item 3 is verified for the data model only. If Residential sites are worked by in-house crews, the
contractor picker would be empty there. That is a usage question, not effort.

---

## I. Trade categories

```
$ grep -rniE "\btrade\b|discipline|work_type|WORK_TYPE|TRADE_" projects --include=*.py | grep -v /tests | grep -v /migrations/
projects/design_metrics.py:578:        discipline respectively.          <- prose
projects/management/commands/seed_opex_test_data.py:38:...discipline decorative.   <- prose
projects/management/commands/seed_scm_handoff_data.py:28:...discipline decorative  <- prose
projects/management/commands/_demo_support.py:206:...discipline decorative.        <- prose
projects/views.py:2135:# This is a considered trade of precision for coverage.     <- prose
$ grep -rnE "'(Civil|Mechanical|Electrical)'" projects --include=*.py | grep -v /tests | grep -v /migrations/
(no output)
```

`models.py:4865` `ERR_ELECTRICAL_DESIGN` is a design-error code, not a trade. Template data contains exactly one trade word:

```
Template phases: []
Template tasks: [('OPEX', 1, 'CIVIL_WORK_AND_MMS_INSTALLATION', 'Civil Work and MMS Installation')]
```

The OPEX v1 INSTALLATION phase tasks are task names, not a trade list: Civil Work and MMS Installation, Module
Installation, LA and Earthing Installation, DC Cable Laying with Conduit, DCDB and ACDB Installation, Inverter
Installation, AC Cable Laying, RMS Installation, Solar Generation Meter Installation, plus Testing & Commissioning and
Net Meter Installation. All have `assigned_role` = Site Engineer.

**Conclusion (VERIFIED): no trade / discipline choices list exists.** None was invented (Blocker 1).

---

## J. Notifications and reporting hooks (look only)

Morning report assembly (VERIFIED):

- `task_report.build_task_reports(report_date)` (`task_report.py:155-259`) is pure and read-only with a fixed 5 queries (docstring 42-44):
  1. recipients: active profile + active user, excluding `MORNING_REPORT_EXCLUDED_ROLES` (172-179)
  2. `managed_project_ids_by_profile()` (182)
  3. grouped counts per (project, assignee) (189-199)
  4. the delayed/due-today task list (205-211)

  Each person's scope comes from `ALL_PROJECTS_ROLES` / managed projects / own tasks (215-222), and the counts are summed in Python.
  **A person whose three counts are all zero is dropped** (`229: if not (delayed or due_today or no_due_date): continue`).
  It returns one dict per person (246-258).
- `send_morning_task_report` renders `projects/email/morning_task_report.txt` / `.html` (`_render`, 204-207) and sends through
  `send_notification`. **WhatsApp uses a fixed Interakt template with exactly seven parameters** (`whatsapp_params`, 65-77; docstring 14-17),
  so a new line could reach email without a new Interakt template, but not WhatsApp.
- EOD digest: per-user metrics grouped by `ActivityLog.action_code` with `timestamp__date=today` (`send_eod_digest.py:211-218`).
- Notification layer: `notify.py` header (1-27) says `notify()` / `notify_now()` sit **above** `send_notification()`, and every send still
  goes through it. Since T1 every new event is registered in `notify_registry` (see Blocker 6).

---

## Blockers

These need a ruling from Zuber before a build prompt can be written.

1. **Trade list (ruling 3).** No list exists (Section I). Name the values (e.g. Civil / Mechanical / Electrical / …?), and say
   whether "Other" is allowed. If it is, say whether it carries text, which would re-open free text.
2. **Contractor master data and picker (ruling 2).** Locally there are 0 contractor-kind vendors. Production is NOT CHECKED. Only SCM/Admin
   can create or re-kind a vendor (`views.py:8319`). Rule on two things: (a) when a crew's contractor is not in the list, does the SE get stopped
   or does something else happen? (b) is the picker "every active contractor" (as on the bill raise page) or narrowed to the site? There is
   no site ↔ contractor record today, so narrowing needs a new link.
3. **Photo bucket.** The bills bucket (QA/QC photos, 900 s links) or the delivery bucket (GRN damage photos, 300 s links)? Also: who may
   open an attendance photo (every site viewer, or managers + SE only), and may a photo be replaced on edit?
4. **Who creates, and who corrects (ruling 6).** (a) May the PM or a coordinator also *create* an entry on the day, or only the SE?
   (b) Is Admin excluded from after-the-day correction? `user_can_manage_project` excludes Admin. (c) Does the SE's
   same-day edit also need a note, or only the after-the-day correction? (d) Can an entry be voided or withdrawn, or only edited?
5. **How attendance feeds billing (ruling 1).** `ContractorBillDetail` has no man-days field (Section B.2). Is v1 just the
   record, with billing reading it later, or must the bill raise or approval page show the contractor's attendance for that site in v1?
6. **Notification chokepoint wording.** The locked rule says "only via `send_notification()`". The codebase now registers events in
   `notify_registry` and sends via `notify()` / `notify_now()`, which call `send_notification()`. Confirm the build should use `notify()`.
   This only matters if attendance sends anything in v1.

Minor (a default is proposed; flag if wrong): attendance is allowed on sites in Active / In Progress only. That matches the SE
dashboard (`views.py:1226`).

---

## Proposed data shape (field list only, no code)

**AttendanceEntry** (one row per site × contractor × trade × day):

| Field | Ruling | Note |
|---|---|---|
| `project` FK Project, PROTECT | 3, 8 | the site |
| `contractor` FK Vendor, PROTECT | 2 | writer refuses `kind` ∉ `VENDOR_BILLABLE_KINDS` or inactive, as the bill does |
| `trade` choice | 3 | **cannot be placed until Blocker 1** |
| `attendance_date` Date | 3, 6 | `timezone.localdate()` at create, never typed |
| `headcount` positive integer, CHECK > 0 | 3 | |
| `time_in` Time, required | 4 | |
| `time_out` Time, nullable | 4 | open entry = derived predicate (F.3), no stored flag |
| `photo_file_name`, `photo_bucket`, `photo_path`, `photo_size_kb` | 5 | bucket per Blocker 3; no URL stored |
| `photo_taken_at` DateTime, nullable | 5 | browser EXIF, evidence only (E.2) |
| `recorded_by` FK UserProfile, PROTECT | 6 | |
| `recorded_at` DateTime, default now | 5, 6 | server time, the authority |
| `client_uuid` UUID, unique, nullable | — | house duplicate-submit key (R-14), not tied to a ruling |
| Unique (`project`, `contractor`, `trade`, `attendance_date`) | 3 | |
| *(no task FK)* | 7 | |

**AttendanceRevision** (copy of `DeliveryChallanRevision`): `entry` FK PROTECT, `revision_no` (unique with entry),
`revised_by` FK PROTECT, `revised_at`, `reason` (CHECK non-empty at least for after-the-day, per Blocker 4c), `before` JSON,
`after` JSON. Rulings 6.

**Could not place:**

- `trade` values (Blocker 1).
- Any void/withdrawn state (Blocker 4d).
- Any billing link field (Blocker 5).
- Whether `time_out` may be earlier than `time_in` (a night shift crossing midnight). No ruling covers it, so a CHECK `time_out > time_in` cannot be assumed.

---

## Residential verdict

**Include.** Same assignment mechanism, same page, same contractor entity, and nothing beyond a project-type condition is
needed. Caveat: Residential use of third-party contractors was not checked.

---

## Risks

1. **An empty contractor list on day one.** With 0 contractor vendors locally and no SE path to add one, the first SE to open the form
   may find nothing to pick, and attendance stalls behind SCM (Blocker 2).
2. **"Taken at the time" cannot be enforced.** `capture` can be bypassed with a gallery pick on many phones, and the server has no EXIF reader.
   The record is the server time plus an optional browser EXIF time. That is evidence for review, not proof.
3. **The day boundary.** Any code that uses `date.today()` instead of `timezone.localdate()` can put an entry on the wrong day or close
   the SE's edit window at the wrong time. 15 such calls already exist.
4. **Billing expectations.** Ruling 1 says attendance feeds billing, but bills carry no man-days and no rate (B-16). If billing
   users expect a computed amount, v1 cannot provide one.
5. **SE scope via held tasks.** An SE sees a site only by holding a task on it. If an SE's tasks are reassigned mid-day, they lose
   access to their own same-day entry and cannot fix it. Only the PM or a coordinator can then.

---

## Secondary findings

Appended to `SECONDARY_FINDINGS.md` under "From ATT-A — contractor attendance audit (2026-10-30)":

- `user_can_manage_project` ignores `is_active`; `project_managers` does not. Profile-only deactivation leaves coordinator authority intact.
- `project_overview` (`views.py:11637`) and 14 other non-test sites use `date.today()` against the `timezone.localdate()` rule.
  The comment's "server clock is UTC" premise conflicts with Django setting `TZ` on Linux.
- Railway cron schedules (EOD 8 PM, morning 10 AM) and their start commands are recorded nowhere in the repo.
- The morning report drops a person whose three task counts are zero, so a report-borne reminder would never reach an SE with no open tasks.
