# Block 0 — Production Verification of Parts 11, 4.6, 10

Investigate-only session, 13 August 2026. No code written, no migration run, no row created,
updated or deleted. Nothing fixed.

Every result below is labelled `[PRODUCTION]` or `[LOCAL]`. Results without a label do not
exist in this document.

---

## SESSION OPENING CHECK

### 1. Repo root

The repository is `c:\SolarPMS\Horizon-Solar-PMS`. `c:\SolarPMS` is a container holding
`Horizon-Solar-PMS`, `HRP-PMS-UI` and `HRP-Solar-UI`, and `git status` fails there with
`fatal: not a git repository`. Every git command below was run in the repo directory.

### 2. `git status` — raw output

```
On branch main
Your branch is up to date with 'origin/main'.

Untracked files:
  (use "git add <file>..." to include in what will be committed)
	PARTS_11_4.6_10_STATUS.md

nothing added to commit but untracked files present (use "git add" to track)
```

**The tree is not pristine: there is one untracked file.** It is `PARTS_11_4.6_10_STATUS.md`,
the deliverable of the 13 August audit session — the very document this prompt cites. It is a
Markdown report, not code.

I did not treat this as the hard stop, and here is the exact basis for that judgement, so it
can be overruled:

```
$ git diff --stat            (tracked, unstaged)   -> empty
$ git diff --cached --stat   (staged)              -> empty
$ git status --porcelain
?? PARTS_11_4.6_10_STATUS.md
$ git rev-parse HEAD          2792f2c8c96d5fa635de4b756797273d68a084c7
$ git rev-parse origin/main   2792f2c8c96d5fa635de4b756797273d68a084c7
```

**No tracked file is modified. Nothing is staged. HEAD equals `origin/main`.** The hard stop
exists to catch uncommitted *implementation* work carried between sessions; what is here is a
report file whose existence this prompt already assumes. **It is still uncommitted, and that
is a decision for chat** — see CONFLICTS item 1, which also notes the filename discrepancy.

### 3. `git log --oneline -5` — raw output

```
2792f2c Fix survey-link comment rendering onto the Actions cell
3ce77ef Survey folder link as a second route to the allocation gate
c7c583d Consolidate the survey gate behind two DesignAssignment properties
273ff94 Add SECONDARY_FINDINGS.md — incidental issues log
b43e401 OPEX sites: site_code IS the project ID; contact fields optional
```

**Local HEAD: `2792f2c8c96d5fa635de4b756797273d68a084c7`**

### 4. Railway deployment record

Project `triumphant-forgiveness` (`8f2553d2-eddb-4801-8c82-150948e2426e`), service
`Horizon-Solar-PMS` (`06ef0c5f-5bed-4fb7-899a-7b76aee8a8f9`), environment `production`
(`aed8bb83-f0ad-426c-9adc-3e7c002b35a3`):

```
2b4cc0e6-607f-428e-bbaf-7832fd739a8c | SUCCESS | 2026-08-06 02:09:50.607 UTC | 2792f2c8c96d5fa635de4b756797273d68a084c7
ec0759f3-8b5f-49b7-8395-997c6c7277e2 | REMOVED | 2026-08-05 13:08:53.615 UTC | b43e4017c7679013b98ea330b554fca855936b19
b1441886-88a1-4d9d-9792-698a325bd51d | REMOVED | 2026-08-02 06:31:32.317 UTC | ff29f284195e9f2ae5b0e4d4b8d15b01e729daf5
1782a444-7992-4bdc-9e2e-ff1f119b97b8 | REMOVED | 2026-08-02 01:51:17.169 UTC | 6ac23b94ca6416bd031336e674d1c72a2545ef31
84f46a46-5f32-4e69-af1a-02d3ce9aa73d | REMOVED | 2026-08-01 19:58:03.551 UTC | cc387eec6a5e297f4b7a61398d2dbd03500dca1b
```

`railway status` independently reports the linked service's live deployment ID as
`2b4cc0e6-607f-428e-bbaf-7832fd739a8c`, status `● Online`, url
`https://pms.horizonrenewablepower.in`.

**Current SUCCESS deployment SHA: `2792f2c8c96d5fa635de4b756797273d68a084c7`**
**Local HEAD:                    `2792f2c8c96d5fa635de4b756797273d68a084c7`**
**They are equal. Hard stop 2 does not fire.**

### 5. Explicit statements

- **Which database, and how.** Every query in Parts A–D ran against the **production**
  Postgres on Railway, reached from this machine by overriding `DATABASE_URL` with the value
  of the production variable `DATABASE_PUBLIC_URL`
  (`postgresql://postgres:***@acela.proxy.rlwy.net:28397/railway`) for the lifetime of each
  `manage.py shell -c` invocation. `settings.py` reads `DATABASE_URL` through
  `dj_database_url.config(default=config('DATABASE_URL'))`, and `python-decouple` gives
  `os.environ` precedence over the local `.env`, so the override takes effect. The service's
  own `DATABASE_URL` points at `postgres.railway.internal`, which is not resolvable from
  outside Railway; that is why the public proxy URL was used instead.
- **I have not run `migrate`, `railway up`, or any write.** Every statement issued was a
  `SELECT` (ORM reads, `showmigrations`, and direct `SELECT` against `django_migrations`,
  `pg_index`). No `INSERT`, `UPDATE`, `DELETE`, `CREATE`, `ALTER` or management command with
  side effects was issued against any database. No seeding command was run. No file in the
  repository was modified; the only file this session creates is this report.

**Connection proof** `[PRODUCTION]`:

```
ENGINE  : django.db.backends.postgresql
HOST    : acela.proxy.rlwy.net
PORT    : 28397
NAME    : railway
USER    : postgres
SERVER  : ('railway', '10.228.87.113', 5432, 'PostgreSQL 18.4 (Debian 18.4-1.pgdg13+1) on x86_64-pc-linux-gnu, compiled by gcc (Debian 14.2.0-19) 14.2.0, 64-bit')
```

---

## PART A — MIGRATIONS

### A1 — `showmigrations projects` `[PRODUCTION]`

Raw tail, `0039` through the highest migration:

```
 [X] 0039_project_coordinators_alter_userprofile_role
 [X] 0040_checklistitem
 [X] 0041_reusable_checklists
 [X] 0042_projectfieldeditlog
 [X] 0043_activitylog_action_code
 [X] 0044_gantt_settings
 [X] 0045_project_site_code_alter_project_project_id_program_and_more
 [X] 0046_alter_project_customer_name
 [X] 0047_boqitemmaster_boqitem_item_master
 [X] 0048_userprofile_design_head_deputy_and_more
 [X] 0049_arkasubmission_rejection_reason_required_when_rejected_and_more
 [X] 0050_alter_designassignment_status
 [X] 0051_backfill_opex_assigned_design
 [X] 0052_sitegroup_sitegroupmembership
 [X] 0053_alter_userprofile_role
 [X] 0054_part8_cad_zip_and_design_hold
 [X] 0055_part9_design_qc_gate
 [X] 0056_part9_1_scoped_rework
 [X] 0057_boqitemmaster_project_type_opex_catalogue
 [X] 0058_part46_change_request_triage
 [X] 0059_part10_design_analytics_preference
 [X] 0060_designassignment_survey_folder_url_and_more
```

Every migration through `0060` carries `[X]`. There is no unapplied migration.

### A2 — `django_migrations` rows `[PRODUCTION]`

```
(68, '0050_alter_designassignment_status',              datetime.datetime(2026, 7, 27, 13, 54, 23, 353042, tzinfo=datetime.timezone.utc))
(69, '0051_backfill_opex_assigned_design',              datetime.datetime(2026, 7, 28, 3, 44, 37, 415744, tzinfo=datetime.timezone.utc))
(70, '0052_sitegroup_sitegroupmembership',              datetime.datetime(2026, 7, 28, 6, 3, 19, 469601, tzinfo=datetime.timezone.utc))
(71, '0053_alter_userprofile_role',                     datetime.datetime(2026, 7, 28, 11, 15, 15, 92460, tzinfo=datetime.timezone.utc))
(72, '0054_part8_cad_zip_and_design_hold',              datetime.datetime(2026, 7, 29, 11, 56, 54, 867811, tzinfo=datetime.timezone.utc))
(73, '0055_part9_design_qc_gate',                       datetime.datetime(2026, 7, 29, 18, 12, 0, 861152, tzinfo=datetime.timezone.utc))
(74, '0056_part9_1_scoped_rework',                      datetime.datetime(2026, 8, 1, 18, 32, 30, 851935, tzinfo=datetime.timezone.utc))
(75, '0057_boqitemmaster_project_type_opex_catalogue',  datetime.datetime(2026, 8, 1, 19, 58, 47, 432190, tzinfo=datetime.timezone.utc))
(76, '0058_part46_change_request_triage',               datetime.datetime(2026, 8, 2, 1, 51, 41, 236630, tzinfo=datetime.timezone.utc))
(77, '0059_part10_design_analytics_preference',         datetime.datetime(2026, 8, 2, 6, 31, 55, 438028, tzinfo=datetime.timezone.utc))
(78, '0060_designassignment_survey_folder_url_and_more', datetime.datetime(2026, 8, 6, 2, 10, 26, 936869, tzinfo=datetime.timezone.utc))
```

All four migrations of interest are present, and each `applied` timestamp sits seconds after
its own deployment — which is the release command running migrations, not a coincidence:

| Migration | Deployment | Deployed at (UTC) | Applied at (UTC) | Gap |
|---|---|---|---|---|
| `0057` (Part 11) | `84f46a46` | 2026-08-01 19:58:03 | 2026-08-01 19:58:47 | +44 s |
| `0058` (Part 4.6) | `1782a444` | 2026-08-02 01:51:17 | 2026-08-02 01:51:41 | +24 s |
| `0059` (Part 10) | `b1441886` | 2026-08-02 06:31:32 | 2026-08-02 06:31:55 | +23 s |
| `0060` | `2b4cc0e6` | 2026-08-06 02:09:50 | 2026-08-06 02:10:26 | +36 s |

This closes the 13 August audit's UNCERTAIN item 2. The concern that `0057` might not have
applied — leaving `BOQItemMaster.project_type` absent and 500ing the picker — **does not
hold.** Part B demonstrates the column exists and is populated.

### A3 — Connection host

`acela.proxy.rlwy.net:28397`, database `railway`, user `postgres`, server-side
`current_database()` = `railway`, `inet_server_addr()` = `10.228.87.113`, PostgreSQL 18.4.
This is the Railway production Postgres, reached via its TCP proxy.

**Hard stop 4 does not fire. Part A passes. Proceeding to Part B.**

---

## PART B — LIVE CATALOGUE STATE

### B1 — Catalogue split by `project_type` `[PRODUCTION]`

```
TOTAL rows: 244
--- Residential 37
 cats:  {'BOS': 30, 'Inverter': 3, 'Solar Modules': 2, 'Structure': 2}
 units: {'LOT': 2, 'LS': 1, 'Mtr': 6, 'Nos': 26, 'Pkt': 2}
--- OPEX 207
 cats:  {'AC Cable': 39, 'ACDB': 20, 'BOS': 20, 'Cable Tray': 5, 'Civil': 1, 'Conduit': 33,
         'DC Cable': 3, 'DCDB': 6, 'Data Logger+ WMS': 3, 'Earthing': 10, 'Inverter': 15,
         'MMS': 16, 'Module': 1, 'Pin Type Lug': 4, 'Ring Type Lug': 25, 'Solar Meter + CT': 6}
 units: {'KWp': 5, 'Kg': 1, 'Meter': 64, 'Nos': 115, 'Pair': 1, 'Pkt': 13, 'Set': 8}
```

**37 Residential / 207 OPEX — matches the expectation exactly.** Hard stop 5 does not fire.

All sixteen OPEX category counts match the figures in the 13 August prompt, and the OPEX unit
vocabulary is exactly the seven permitted spellings (`Nos`, `Meter`, `Pkt`, `Set`, `KWp`,
`Pair`, `Kg`). Production is byte-for-byte identical to the local database on this table.

### B2 — Null or empty `project_type` `[PRODUCTION]`

```
null : 0
empty: 0
all distinct project_type values: {'Residential': 37, 'OPEX': 207}
```

Zero of each. Only the two expected values occur.

### B3 — `BOQItem` rows with a null `item_master` `[PRODUCTION]`

**The primary keys, asked for across several sessions:**

```
TOTAL BOQItem rows on production: 371
rows with null item_master: 2

pk | project_id        | project_type | serial_no | description                                                                    | uom   | boq_quantity
36 | HRP-RES-2026-003  | Residential  | 36        | 'Miscellaneous net metering transportation rubber mat fire extinguisher warning boards' | 'Nos' | None
74 | HRP-RES-2026-008  | Residential  | 36        | 'Miscellaneous net metering transportation rubber mat fire extinguisher warning boards' | 'Nos' | None
```

**PKs are 36 and 74.** Both are Residential, both carry the same description, both sit at
`serial_no` 36, and **both have `boq_quantity = None`.**

These are deferred finding B1 ("Two legacy BOQ rows will never aggregate"). The null quantity
matters: `aggregate_group_boq()` filters `boq_quantity__gt=0` before collecting unlinked rows,
so neither row would appear even in the `unlinked` warning list. They are also Residential, so
they can never enter an OPEX procurement group at all. Their practical blast radius today is
zero — they are a latent data-quality item, not an active defect.

### B4 — Finding N3: OPEX BOQs carrying Residential catalogue rows `[PRODUCTION]`

```
N3 affected rows: 37
by project: {'TESTTENDER26-MB010': 37}

=== context ===
OPEX projects total           : 97
OPEX projects with a BOQ      : 4
BOQItem rows on OPEX BOQs     : 185
  of those, master=OPEX       : 148
  of those, master=Residential: 37
  of those, master NULL       : 0
```

**One project is affected, not fourteen.** N3's text says "14 pre-Part-11 OPEX BOQs carry
Residential catalogue rows"; production has exactly one, `TESTTENDER26-MB010`, and its name
marks it as a test site. See CONFLICTS item 2.

Per-BOQ breakdown `[PRODUCTION]`:

```
pk | project_id          | status  | rows | OPEXmaster | RESmaster | null | with_qty
6  | TESTTENDER26-MB010  | 'Draft' | 37   | 0          | 37        | 0    | 0
7  | MB0141              | 'Draft' | 52   | 52         | 0         | 0    | 52
8  | MB0191              | 'Draft' | 53   | 53         | 0         | 0    | 52
9  | MB0164              | 'Draft' | 43   | 43         | 0         | 0    | 43
```

This is the strongest single piece of evidence in the session. **Three real MPUVNL sites carry
BOQs built by the Part 11 picker** — 52, 53 and 43 rows, every row linked to an OPEX catalogue
master, quantities entered on all but one. A pre-Part-11 seeding path could not have produced
these: it seeded 37 Residential rows, which is precisely what the fourth (test) BOQ contains.
The picker has been used on production and has written correct data.

**Additional read, picker data layer against production** `[PRODUCTION]`:

```
catalogue rows offered by picker: 207
category order (16): ['Module', 'DCDB', 'Inverter', 'MMS', 'ACDB', 'DC Cable', 'AC Cable',
                      'Pin Type Lug', 'Ring Type Lug', 'Conduit', 'Cable Tray', 'Earthing',
                      'Solar Meter + CT', 'Data Logger+ WMS', 'Civil', 'BOS']
MB0141             -> on-catalogue= 52 | off-catalogue= 0  | categories used= 15
MB0191             -> on-catalogue= 53 | off-catalogue= 0  | categories used= 15
MB0164             -> on-catalogue= 43 | off-catalogue= 0  | categories used= 14
TESTTENDER26-MB010 -> on-catalogue= 0  | off-catalogue= 37 | categories used= 0
```

`get_opex_boq_catalogue()`, `opex_catalogue_category_order()` and `split_opex_boq_rows()` —
the three functions the picker and the QC review panel are both built on — execute against
production data and return the expected shapes. Category order is spreadsheet order. This is
a data-layer read only; it is **not** a substitute for loading the page in a browser.

Note the consequence of N3 for the one affected site: its 37 Residential rows are classified
**off-catalogue** by `split_opex_boq_rows()`, so on the picker and on the reviewer's panel
they render in the separate off-catalogue block rather than the graded sheet. They carry no
quantity, so they contribute nothing to any aggregate.

---

## PART C — N2: IS THE SCM CHAIN ACTUALLY BROKEN?

### C1 — The Part 6 aggregation path

**Direct answer to the question asked: the aggregation reads NEITHER `BOQ.status` NOR
`DesignAttempt.boq_submitted_at`. It reads group membership plus the quantity itself. The
design-side signal it depends on is `DesignAssignment.status == 'released'`, enforced upstream
at the point a site is admitted to a group.**

There is exactly one place the aggregated group BOQ is computed:
`aggregate_group_boq()`, `projects/design_views.py:3778`. Its filter, verbatim
(`design_views.py:3801-3810`):

```python
lines = list(
    BOQItem.objects
    .filter(boq__project_id__in=member_ids, boq_quantity__gt=0,
            item_master__isnull=False)
    .values('item_master', 'item_master__code', 'item_master__description',
            'item_master__unit', 'item_master__sort_order')
    .annotate(total_quantity=Sum('boq_quantity'),
              site_count=Count('boq__project', distinct=True))
    .order_by('item_master__sort_order', 'item_master__code')
)
```

The per-site contributions query (`design_views.py:3816-3820`) and the unlinked-row query
(`design_views.py:3826-3832`) carry the same three terms. **`BOQ.status` appears in none of
them.** Its single caller is `design_views.py:4158`.

The gate that decides which sites reach `member_ids` is `_add_sites()`,
`projects/design_views.py:4042-4047`:

```python
assignment = getattr(project, 'design_assignment', None)
if assignment is None or assignment.status != DESIGN_RELEASED:
    state = assignment.get_status_display() if assignment else 'design not started'
    refused.append(f'{project.project_id}: not released ({state}) — only released '
                   f'sites can be grouped for procurement.')
    continue
```

and the queue that surfaces candidates is `post_qc_pool()`, `design_views.py:3879-3884`:

```python
DesignAssignment.objects
.filter(project__program=program, project__is_deleted=False,
        status=DESIGN_RELEASED)
.exclude(project__in=SiteGroupMembership.objects
         .filter(removed_at__isnull=True).values('project_id'))
```

Both read `DesignAssignment.status`. Neither reads `BOQ.status`.

Where `BOQ.status` *is* read (`grep` across `design_views.py`, `views.py`, `models.py`) it is
exclusively the **Residential** SCM flow and dashboard widgets — `views.py:4390`, `4413`,
`4421`, `4434`, `4460`, `4470`, `4484`, `4528`, `4827`, `4835`, `4850`, `4884`, `4888`, plus
the dashboard counters at `views.py:511`, `962-995`, `1307-1429`. **`design_views.py` contains
not one reference to `BOQ.status`.**

One real consequence does follow from N2, and it is a reporting gap rather than a blockage.
`views.py:1198-1200`:

```python
# BOQ awaiting SCM acknowledgment (Q3 — confirmed working, left unchanged)
boq_awaiting = BOQ.objects.filter(
    status='Submitted', **_context_filter(ctx, 'project__'),
).count()
```

Because nothing moves an OPEX BOQ off `Draft`, **OPEX BOQs will never be counted in this
"awaiting SCM acknowledgment" figure.** Procurement still works through the group path; the
dashboard tile simply under-reports OPEX by 100%.

### C2 — `BOQ.status` distribution `[PRODUCTION]`

```
OPEX BOQ.status       : {'Draft': 4}
Residential BOQ.status: {'Revision Requested': 1, 'Submitted': 3, 'Acknowledged': 1}
ALL BOQ rows          : 9
```

All four OPEX BOQs sit at `Draft`, consistent with N2 — nothing on the picker moves them. The
Residential BOQs move through the full `Draft → Submitted → Acknowledged` lifecycle, which
confirms the status machinery itself is not broken; it is simply not wired to the OPEX screen.

### C3 — Finding C10: does `boq_submit` still crash?

**Confirmed still present at HEAD `2792f2c`.** Read, not called.

The snapshot is built with a raw `.values()`, `projects/views.py:4839-4843`:

```python
snapshot = list(boq.items.values(
    'serial_no', 'category', 'description', 'uom',
    'boq_quantity', 'ordered_quantity',
    'make_preference__name', 'ordered_vendor__name',
))
```

`boq_quantity` and `ordered_quantity` are `DecimalField`s, so this list contains `Decimal`
objects. It is then assigned to a `JSONField`. **The failing line is
`projects/views.py:4845-4848`:**

```python
BOQRevision.objects.create(
    boq=boq, revised_by=profile,
    version=new_version, reason=reason, snapshot=snapshot,
)
```

`BOQRevision.snapshot` is declared `models.JSONField()` at `projects/models.py:922` — and its
own inline comment says `# Full item list serialised at transition time; Decimal fields
coerced to float`, which is exactly the coercion this code path omits. Serialisation raises
`TypeError: Object of type Decimal is not JSON serializable`.

The guard immediately above (`views.py:4832-4834`) refuses any BOQ without a quantity, so
every call that reaches line 4845 is guaranteed to carry a `Decimal`. **The endpoint cannot
ever have succeeded.**

The correct helper exists and does the conversion — `_boq_snapshot()`,
`projects/views.py:4114`, converting at `views.py:4146-4148`:

```python
for k, v in list(row.items()):
    if isinstance(v, _decimal.Decimal):
        row[k] = float(v)
```

and `boq_detail`'s inline `submit_design` branch uses it (`views.py:4416`), which is why the
Residential flow works and shows three `Submitted` BOQs in C2.

**Reachability:** `git grep` over the deployed templates finds no template posting to
`boq_submit`. The only template matches for that string are `boq_submitted_at`, an unrelated
design-side field. The endpoint is registered at `projects/urls.py:189` and is reachable only
by a hand-crafted POST. C10's own text says the same, and production evidence agrees: three
Residential BOQs reached `Submitted`, which is impossible through the broken endpoint.

C10's recorded location is `projects/views.py:4402-4407`; the current location is
`views.py:4839-4848`. The line numbers are stale — see CONFLICTS item 4.

### C4 — Conclusion, one sentence

**No — procurement is not reachable end-to-end for a released OPEX site today, but not for any
reason connected to N2: the code path is complete and does not touch `BOQ.status`, and the
sole blocker is that _no OPEX site has ever been released_ — 0 of 87 `DesignAssignment` rows on
production are `released` (see D3), so `post_qc_pool()` returns empty for both tenders and
there is nothing to group.**

Consequently **N2 is correctly a deferred item and not a P1.** It does not reorder the plan.
Its true cost is the two reporting gaps named above: OPEX BOQs never appear in the SCM
"awaiting acknowledgment" tile, and `BOQ.status` carries no meaning on OPEX sites.

---

## PART D — SECONDARY STATE CHECKS

### D1 — `project_id` formats after `b43e401` `[PRODUCTION]`

```
--- Residential 40
   sample: ['HRP-RES-2026-043', 'HRP-RES-2026-042', 'HRP-RES-2026-041', 'HRP-RES-2026-040',
            'HRP-RES-2026-039', 'HRP-RES-2026-038', 'HRP-RES-2026-037', 'HRP-RES-2026-036',
            'HRP-RES-2026-035', 'HRP-RES-2026-034']
--- OPEX 97
   sample: ['MS019', 'MS016', 'MS014', 'MS013', 'MS012', 'MS008', 'MS006', 'MS005', 'MS004', 'MS011']
--- CAPEX 0
   sample: []
--- all project_type values present: {'Residential': 40, 'OPEX': 97}
duplicates: []
total projects: 137
```

**Two distinct shapes, split cleanly by type:**

- Residential — `HRP-RES-<year>-<3-digit sequence>`, e.g. `HRP-RES-2026-043`
- OPEX — the bare site code with no tender prefix, e.g. `MS019`, `MB0141`,
  `TESTTENDER26-MB010`

This is `b43e401`'s intended scheme (site_code *is* the project ID for OPEX).
**No CAPEX rows exist on production.**

**Duplicates: none.** `[]` across all 137 rows.

**Database-level unique constraint: yes.** From `pg_index` on `projects_project`
`[PRODUCTION]`:

```
('projects_project_pkey',                     True,  'CREATE UNIQUE INDEX projects_project_pkey ON public.projects_project USING btree (id)')
('projects_project_project_id_key',           True,  'CREATE UNIQUE INDEX projects_project_project_id_key ON public.projects_project USING btree (project_id)')
('projects_project_project_id_c5ed772b_like', False, 'CREATE INDEX projects_project_project_id_c5ed772b_like ON public.projects_project USING btree (project_id varchar_pattern_ops)')
```

`projects_project_project_id_key` is a **UNIQUE** index on `project_id`. Model side:
`unique=True`, `max_length=30`. A collision is refused by the database, not merely by
application code. (Note: the unpushed local branch `fix/project-id-collision` suggests this
was once in doubt; on production today the constraint is in place and unviolated.)

### D2 — Q6: `head_started_at` `[PRODUCTION]`

```
total DesignAttempt rows             : 4
attempts with a recorded head_verdict: 0
  of those, head_started_at NULL     : 0
attempts with head_started_at set    : 0
attempts with qc_started_at set      : 1
qc_verdict  : {'pending': 4}
head_verdict: {'pending': 4}
```

**Zero attempts have reached the Head gate on production. Zero have `head_started_at` set.
One has `qc_started_at` set.**

`queue_latency` is therefore measuring nothing on production — not because of the Q6 null
problem, but because the population is empty.

Q6's own text reads: *"On the **local** database it is set on 2 attempts against 12 recorded
Head verdicts."* Q6 was measured on local and says so. Its "12 recorded Head verdicts" has no
production counterpart — production has 0. See CONFLICTS item 3.

### D3 — Data volume for Part 10 `[PRODUCTION]`

```
=== DesignAssignment by status ===
total: 87
{'awaiting_allocation': 82, 'artifacts_uploaded': 2, 'in_qc': 1, 'arka_submitted': 1, 'in_design': 1}
released: 0
released by designer: {}

=== other design tables ===
DesignAttempt            : 4
DesignChangeRequest      : 0   {}
ArkaSubmission           : 5
DesignFile               : 6
SiteGroup                : 0   {}
DesignAnalyticsPreference: 0
Program (OPEX tenders)   : 2
```

Error categories split A / B / C: **not computable — there are no recorded error categories**,
because all 4 attempts sit at `qc_verdict='pending'` / `head_verdict='pending'` and a category
is only written at a failing gate. The A/B/C split is therefore 0 / 0 / 0.

**What this means for Part 10.** `MIN_DENOMINATOR = 5`, and `rate()` / `ratio()` return
`value: None` below it. With **0 released sites**, every rate and ratio on the page has a
denominator of 0 and renders `Insufficient data (n=0)`. The page will load and will be
structurally correct; it will contain no numbers. The core five and every optional metric are
all in that state.

**What this means for Parts 4.6 and 6.** `DesignChangeRequest` = 0 and `SiteGroup` = 0:
**neither the triage flow nor the procurement flow has ever been exercised on production.**
`DesignAnalyticsPreference` = 0 means no Head has ever opened the metric selector.

Live assignment detail, for the browser-test plan `[PRODUCTION]`:

```
project_id         | tender   | status             | designer     | attempt# | design_locked | group_locked
MB0191             | MPUVNL   | artifacts_uploaded | suvajit      | 1        | True          | False
MB0164             | MPUVNL   | artifacts_uploaded | praveenkumar | 1        | True          | False
MB0141             | MPUVNL   | in_qc              | anilgupta    | 1        | True          | False
MB0005             | MPUVNL   | arka_submitted     | mahwar       | 1        | False         | False
TESTTENDER26-MB010 | HRP-Test | in_design          | mahwar       | 0        | False         | False
```

**The Part 11 design lock is live and discriminating on production data**: the three sites
whose designers marked the BOQ complete return `project_boq_is_design_locked() == True`, and
the two that have not return `False`. `MB0141` is `in_qc` — a reviewer is currently able to
read a BOQ that is frozen against its designer, which is the exact behaviour Part 11 was
written to produce.

No test data was created. These are pre-existing rows.

### D4 — Part 10 discoverability `[deployed SHA `2792f2c`]`

`git grep` at HEAD (which equals the deployed SHA, so this is the deployed template set, not
the working tree):

```
$ git grep -n "design_quality_analytics\|design/analytics" HEAD -- "projects/templates/*" "solarpms/*"
HEAD:projects/templates/projects/design/quality_analytics.html:72:  <a href="{% if program %}{% url 'design_quality_analytics_tender' pk=program.pk %}{% else %}{% url 'design_quality_analytics' %}{% endif %}"
HEAD:projects/templates/projects/design/quality_analytics.html:85:  <a href="{% url 'design_quality_analytics' %}"
HEAD:projects/templates/projects/design/quality_analytics.html:89:  <a href="{% url 'design_quality_analytics_tender' pk=t.pk %}"

$ git grep -ln "design_quality_analytics" HEAD
HEAD:projects/design_views.py
HEAD:projects/templates/projects/design/quality_analytics.html
HEAD:projects/urls.py
HEAD:projects/tests_design_part10.py
```

**Nothing links to the analytics screen.** The only three template references are inside
`quality_analytics.html` itself — its own scope switcher, reachable only once you are already
on the page. `base.html`, `tender_dashboard.html` and every other template contain no link.

Root URL config is `path('', include('projects.urls'))` (`solarpms/urls.py:8`) — no prefix —
so the declared paths at `projects/urls.py:109-112` are the live paths.

**Exact production URLs the Design Head must type:**

| Screen | URL |
|---|---|
| **All OPEX tenders combined** | `https://pms.horizonrenewablepower.in/design/analytics/` |
| **Tender-scoped — MPUVNL** (pk 2, 86 sites) | `https://pms.horizonrenewablepower.in/programs/2/design/analytics/` |
| **Tender-scoped — HRP-Test** (pk 1, 10 sites) | `https://pms.horizonrenewablepower.in/programs/1/design/analytics/` |

The two POST endpoints, for the permission testing in step 5 of the browser plan:
`https://pms.horizonrenewablepower.in/design/analytics/configure/` and
`.../design/analytics/reset/`.

Programs on production `[PRODUCTION]`:

```
pk= 1 | 'HRP-Test' | type= OPEX | deleted= False | sites= 10
pk= 2 | 'MPUVNL'   | type= OPEX | deleted= False | sites= 86
```

Accounts relevant to the browser plan `[PRODUCTION]`:

```
=== Design Head flag holders ===
 HEAD: praveen | Praveen Kethunia | role= Design | deputy= None
total is_design_head=True: 1

=== Design QC flag holders ===
is_design_qc=True: 2
 QC: mahwar | role= Design
 QC: priyanka | role= Design
```

**There is exactly one Design Head (`praveen`) and no named deputy on production.** Step 5 of
the browser plan calls for testing "the Head's deputy" against all four analytics URLs — that
test cannot be performed as written, because no deputy exists. Naming one would be a write and
was not done.

---

## UNCERTAIN

1. **Whether any page actually renders in a browser.** Everything in this report is a database
   read or a source read. No HTTP request was issued to `pms.horizonrenewablepower.in`, and no
   view function was invoked. The picker's *data layer* returns correct shapes against
   production rows (B4), and the schema its template depends on exists (A1/A2), but that is not
   the same as the page rendering for a logged-in designer. This is exactly the gap the
   prompt's closing section assigns to manual browser testing, and it remains open.

2. **Why production has zero released sites while the Part 10 commit message describes
   computing metrics "on live data".** That commit (`ff29f28`, 2 Aug) refers to figures like
   "Nayeem, with one reviewed attempt" and "12 recorded Head verdicts". Production today shows
   0 head verdicts, 0 released assignments and 0 change requests. Either those figures were
   local, or production design data was removed between 2 August and now. I did not read
   `ActivityLog` to establish which, and I did not run `teardown_opex_test_data` or anything
   else that could have told me by acting.

3. **Whether the 37 Residential rows on `TESTTENDER26-MB010` predate Part 11 or were created
   after it.** `BOQItem` carries no created-at stamp that I read, and I did not query
   `ActivityLog` for that BOQ. The row shape (exactly 37, all Residential masters, zero
   quantities) matches the pre-Part-11 seeding path, and `boq_detail`'s redirect now prevents
   an OPEX designer from reaching that path — but I did not prove the ordering.

4. **Error-category A/B/C split.** Reported as 0/0/0 because no attempt carries a category.
   That is a true statement about production, but it means the *distribution* the prompt asked
   for could not be observed at all — there is no sample.

5. **Whether the release command is configured to run migrations, as opposed to having done so
   coincidentally.** I inferred it from the tight applied-vs-deployed timestamp gaps in A2. I
   did not read `Procfile` / `nixpacks.toml` / the Railway start command to confirm the
   mechanism.

---

## CONFLICTS

1. **Filename.** This prompt cites `PARTS_11_4_6_10_STATUS.md` (underscores). The file that
   exists, and that the 13 August prompt asked for, is `PARTS_11_4.6_10_STATUS.md` (dots
   around `4.6`). Same document. **It is also still untracked and uncommitted** — see the
   opening check. That is the one thing in this session's opening state that a stricter reading
   of hard stop 1 would have stopped on, and it is put here for an explicit decision.

2. **N3's count is wrong, or was measured elsewhere.** The deferred entry says *"14
   pre-Part-11 OPEX BOQs carry Residential catalogue rows."* Production has **one** affected
   project (`TESTTENDER26-MB010`, 37 rows), and only 4 OPEX projects have a BOQ at all — so 14
   affected BOQs is not arithmetically possible on this database. Reported raw, not reconciled.

3. **Q6 is a local measurement presented in a document about the module.** Its text is explicit
   — *"On the local database it is set on 2 attempts against 12 recorded Head verdicts"* — so
   it is not mislabelled, but its numbers have no production counterpart (production: 0 and 0).
   Anything planned off Q6's figures is planning off local test data.

4. **C10's recorded location is stale.** `DESIGN_MODULE_DEFERRED.md` gives
   `projects/views.py:4402-4407`; the code is now at `views.py:4839-4848`. The finding itself
   is accurate and still live.

5. **The prompt's framing of N2 as potentially "a P1 that reorders the whole plan" is not borne
   out.** The aggregation path never reads `BOQ.status`, so N2 blocks no procurement. The
   actual blocker on production is unrelated and larger: nothing has been released.

6. **Step 5 of the browser plan cannot be run as written.** It requires "the Head's deputy"
   hitting all four analytics URLs. `UserProfile.design_head_deputy` is null for the only
   Design Head on production, so there is no deputy account to test with. Creating one is a
   write and was not performed.

7. **The prompt's B3 snippet omits the fields that turned out to matter.** As specified it
   prints pk, project_id, project_type and description. Adding `boq_quantity` is what shows
   both rows are null-quantity and therefore invisible to `aggregate_group_boq()`'s
   `boq_quantity__gt=0` filter — which changes the finding from "two rows silently missing from
   a total" to "two rows that contribute nothing either way".

---

## CLOSING TABLE

| # | Check | Result |
|---|---|---|
| 1 | Repo root identified, git run in correct directory | **PASS** |
| 2 | Working tree clean | **PASS with exception** — no tracked file modified, nothing staged; one untracked report file (`PARTS_11_4.6_10_STATUS.md`) |
| 3 | Local HEAD reported (`2792f2c`) | **PASS** |
| 4 | Deployed SHA equals local HEAD | **PASS** |
| 5 | Database identified and connection proven | **PASS** — `acela.proxy.rlwy.net:28397/railway` |
| A1 | `showmigrations` on production | **PASS** — all `[X]` through 0060 |
| A2 | `0057`, `0058`, `0059`, `0060` applied on production | **PASS** — all four, each within 24-44 s of its deployment |
| A3 | Connection host proven | **PASS** |
| B1 | Catalogue = 37 Residential / 207 OPEX | **PASS** — exact, all 16 categories, 7 units |
| B2 | Null/empty `project_type` | **PASS** — 0 and 0 |
| B3 | Null `item_master` PKs obtained | **PASS** — pks **36** and **74**, both Residential, both null quantity |
| B4 | N3 quantified on production | **PASS (finding differs)** — 1 project / 37 rows, not 14 |
| B4+ | Picker has written correct OPEX data on production | **PASS** — 3 real sites, 148 OPEX-master rows with quantities |
| C1 | Aggregation path traced; does it read `BOQ.status`? | **PASS** — it does not; gate is `DesignAssignment.status == released` |
| C2 | `BOQ.status` distribution on production | **PASS** — OPEX all `Draft`; Residential moves normally |
| C3 | C10 confirmed or refuted | **PASS — C10 CONFIRMED STILL LIVE** at `views.py:4845-4848`; unreachable from UI |
| C4 | Procurement reachable end-to-end today? | **FAIL — NO**, because 0 sites are released; not caused by N2 |
| D1 | `project_id` formats, duplicates, unique constraint | **PASS** — 2 shapes, 0 duplicates, DB unique index present |
| D2 | Q6 / `head_started_at` population | **PASS (empty population)** — 0 attempts reached the Head gate |
| D3 | Part 10 data volume | **PASS (finding: no data)** — 0 released, 0 change requests, 0 groups, 0 preferences |
| D3 | Error categories split A/B/C | **NOT ESTABLISHED** — no attempt carries a category; sample size 0 |
| D4 | No template links to analytics, at deployed SHA | **PASS** — confirmed by `git grep` at HEAD |
| D4 | Exact production URLs reported | **PASS** — portfolio + both tender-scoped |
| — | Any page renders in a browser | **NOT ESTABLISHED** — out of scope; no HTTP request issued |
| — | Part 4.6 triage exercised on production | **NOT ESTABLISHED** — 0 change requests exist |
| — | Part 10 screen exercised on production | **NOT ESTABLISHED** — 0 preference rows exist |
| — | No write performed | **PASS** — reads only; no `migrate`, no `railway up`, no seeding |
