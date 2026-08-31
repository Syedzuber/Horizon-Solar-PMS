# Demo data — a populated local environment you can throw away

**This must never be run against production.** The commands refuse a non-local database
outright. Read [The interlock](#the-interlock) before overriding anything.

*A separate file rather than a section of `execution-model.md`, deliberately: that
document is 1100 lines of model, conventions and decision log — what the product IS.
This is a runbook — what to type, and what will happen. Someone setting up a laptop to
hand-test should not have to read an architecture document to find it, and a decision
about `StatusTransition` coverage should not be interleaved with a password. §17 of
`execution-model.md` points here.*

---

## Why this exists

Nobody has ever opened an activated OPEX site in a browser. The mirror write-refusal has
been proved in tests and in a shell and never seen by a human.
`PHASE_0_BROWSER_TEST_PLAN.md` has never been run. Production holds 96 OPEX sites in
Draft and almost no execution data, because the team is deliberately waiting for
delivery.

So there is nothing to hand-test against. These commands build it, locally, and remove
it again exactly.

---

## The three commands

```bash
# 1. the environment — users, warehouses, tenders, activation, design state, BOQs
python manage.py seed_opex_test_data

# 2. optional: the Part 6 SCM handoff states, layered on top
python manage.py seed_scm_handoff_data --confirm

# 3. remove everything both of them created
python manage.py teardown_opex_test_data --confirm
```

**Type the names in full.** `se`+Tab does not disambiguate `seed_opex_test_data` from
`send_eod_digest`, which mails the whole company.

Both seeds report without writing when run without their write flag
(`--dry-run` on the first, no `--confirm` on the second). The teardown is a dry run
unless `--confirm` is passed.

---

## What gets created

`seed_opex_test_data` — **247 rows** on a clean database:

| | |
|---|---|
| **7 users** | `demo.pm`, `demo.coord`, `demo.se`, `demo.scm`, `demo.design`, `demo.finance`, `demo.ceo` — one per role, each with a `UserProfile`. `demo.se` carries `is_qaqc` and `is_hse`; `demo.scm` carries `is_warehouse_keeper`. |
| **3 warehouses** | `DEMO-WH-1` and `DEMO-WH-2` share a keeper (`demo.scm`); `DEMO-WH-3` has none — the two things B-14 settled that the schema does not say out loud. |
| **1 OPEX tender** | `DEMO Local Demo Tender` (`DEMOTEND`) with four sites. |
| **2 activated sites** | `DEMOOPEX01`, `DEMOOPEX02` — 7 phases, 22 tasks, 5 mirrors each. |
| **1 Draft site** | `DEMOOPEX03` — so activation can be exercised by hand in a browser. |
| **1 released site** | `DEMOOPEX04` — design released, sits in the procurement group, carries an OPEX BOQ. |
| **1 Residential project** | `DEMO-RES-01`, activated — 9 phases, 52 tasks, M1/M2/M3, a Residential BOQ — so the two templates can be compared on screen. |
| **2 design assignments** | one mid-workflow (`in_design`), one released with a passed QC and an approved Arka. |
| **2 site groups** | one `procurement`, one `execution` — so D-1 is visible. |
| **1 delivery challan** | three lines, status Expected. |
| **task statuses** | Done / In Progress / Blocked / Not Started across the two activated sites, plus the blocking `Issue` a Block always raises. **No mirror is ever moved.** |

`seed_scm_handoff_data` adds **274 more rows**: a second tender (`DEMO SCM Handoff
Pile-up`, `DEMOSCM`) with six sites released 2–31 days ago, one LOCKED group, one DRAFT
group, three sites left in the pool, two historical removals (one a PM change request,
which renders its own red chip) and one ad-hoc BOQ row with no `item_master`, which is
what makes the "could not be aggregated" warning visible rather than theoretical.

**Credentials** are printed at the end of the seed run. All seven accounts share one
password; emails are `@demo.invalid`, a domain RFC 2606 reserves so it can never
resolve.

**There is no demo Admin.** `UserCreateForm` permits only one Admin account and every
real database already has one. That is a product rule, and demo tooling is exactly where
a "just this once" bypass gets copied later. Log in as the existing Admin.

---

## The manifest

**The teardown deletes only what a seed recorded creating.** Not by name, not by prefix,
not by date.

The seed writes every created primary key, by model, in creation order, to:

```
~/.horizon-pms-demo/demo_manifest.json          # printed on every run
```

Outside the repository on purpose — a file inside it is one `git add -A` away from being
committed, and that directory also holds `railway_backup.dump`, a production database
dump. Override with `--manifest PATH`; both seeds and the teardown take the flag, and
`seed_scm_handoff_data` **appends** to the same file so one teardown removes both.

The teardown walks the list in reverse and deletes it. It runs no query that could
select a row a seed did not create. Rows already gone — cascaded, or deleted by hand —
are reported and are not an error. The manifest is consumed (deleted) on a successful
teardown.

### If the manifest is missing, the teardown refuses

It does not fall back to guessing. A fallback that pattern-matched live tables would
reintroduce, as the error path, exactly the mechanism the manifest replaced.

**Known regression.** Before this change the teardown found its targets with
`project_id__startswith='Test-'` and needed no manifest. **Rows created by that older
seed cannot be removed by this version.** To clear them, check out a commit from before
the manifest change and run that version, or delete them by hand. The refusal message
says so, so nobody concludes the tool is broken.

### The namespace is the second line of defence, not the first

Everything carries `DEMO`: project IDs, program names, warehouse codes, `@demo.invalid`
emails. Nothing reads it — it exists so that anything which ever escapes the manifest is
identifiable by eye in a list, an export or a dashboard.

Two spellings, and both are forced:

- **OPEX site IDs have no hyphen** (`DEMOOPEX01`). An OPEX site's `project_id` IS its
  `site_code`, and `OpexSiteForm.clean_site_code()` runs it through
  `normalize_program_code()`, which strips everything outside `[A-Z0-9]`. `DEMO-OPEX-01`
  would be stored as `DEMOOPEX01` anyway.
- **The Residential ID does** (`DEMO-RES-01`), because it is set explicitly, which
  bypasses `generate_project_id()`. See the next section.

---

## The interlock

Both commands, every run:

1. **Print the database host first**, always, before anything else is decided. Host and
   name only — never the password. Same shape as `send_eod_digest`.
2. **Refuse a non-local host.** Local means `localhost`, `127.0.0.1`, `::1`, or an empty
   / socket host. A non-local host requires `--i-know-this-is-not-local`, and the
   refusal names the host and lists what would be written. Passing the flag prints the
   same list as a warning and proceeds.

```
[db] host=localhost name=solarpms_local
```

Demo data reaching production would pollute the CEO dashboard, the EOD digest and every
execution counter prompt 1.3b corrected — and it teaches users that the system is a toy.

---

## What is NOT created through a real code path

Everything goes through the product's own code where a path exists: users through
`UserCreateForm`, the tender through `ProgramForm`, OPEX sites through
`views.create_opex_site()`, phases and tasks through `utils.attach_opex_template()` and
`attach_residential_template()`, group membership through `design_views._add_sites()`.

Six things have **no path in the product** and use `objects.create()`. Each is marked
`# NO PRODUCT PATH` at its call site. Their existence in demo data is not evidence that
the corresponding workflow works, because there is no workflow:

1. `StockLocation` — no view, form or admin registration.
2. `is_qaqc` / `is_hse` / `is_warehouse_keeper` — no writer anywhere, not even in admin.
3. A `group_type='execution'` `SiteGroup` — `site_group_create` hardcodes procurement.
4. `DeliveryChallan` + `DCLineItem` — creation is inline in the view.
5. The activation status writes — inline in `opex_site_activate` / `project_activate`.
6. Task status changes — `_apply_task_status_change()` requires a `request`.

Design workflow states (assignment, attempt, Arka) are likewise direct writes: every
design transition lives inside a view.

**One deliberate bypass.** `DEMO-RES-01`'s `project_id` is set explicitly, which skips
`generate_project_id()`. What that forgoes: the `HRP-RES-{YEAR}-{NNN}` format, the
`select_for_update()` lock, and the max-suffix scan that reserves a number against
soft-deleted rows. Nothing else differs — `Project.save()` takes the same explicit-id
branch `create_opex_site()` uses in production. It is bypassed because otherwise the
demo project would consume a real Residential number on a database that is usually a
production restore, would be indistinguishable by eye from a real project, and would
hand that number back for reuse the moment the teardown ran.

Full list, with the product findings behind each: `EXECUTION_MODULE_DEFERRED.md` §B23.

---

## StatusTransition rows are deleted, and that is a bounded exception to R-4

`StatusTransition` is append-only: `save()` refuses to touch an existing row and
`delete()` raises `AppendOnlyViolation`. `QuerySet.delete()` operates in SQL and bypasses
both, as the model's own docstring states, and that is the route the teardown takes.

It is narrow on purpose. `StatusTransition.project` is `SET_NULL` precisely so a
hard-deleted project cannot erase its own history — so without this, every teardown
would leave orphaned ledger rows behind, permanently, one set per cycle, in the table the
dwell-time reports read. It is only safe because the **manifest bounds it** to rows a
seed created, on a database the interlock has already proved is local.

**Do not generalise it.** There is no other caller and there must not be one.

---

## Tests

`projects/tests_demo_data.py`, 15 tests:

```bash
python manage.py test projects.tests_demo_data --settings=solarpms.test_settings -v 2
```

They pin the five properties that would make the tooling dangerous rather than merely
broken: a seed/teardown round trip restores every per-model row count; the teardown
refuses without a manifest and its message names the regression; both commands refuse a
non-local host; two full cycles run clean; and an activated demo OPEX site carries 22
tasks and exactly 5 mirrors, by name.
