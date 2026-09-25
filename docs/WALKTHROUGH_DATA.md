# Walkthrough data — the synthetic database for walking every feature by hand

## Reset: drop the database and re-seed

This is the supported reset — the only one that always works. Run from the repository
root, with `DATABASE_URL` pointing at `solarpms_walk` for the shell (see *Pointing the
app at it* below):

```
dropdb --if-exists -h localhost -U solarpms_user solarpms_walk && createdb -h localhost -U solarpms_user solarpms_walk
python manage.py migrate && python manage.py seed_walkthrough
```

A full seed takes about 25 seconds on top of the ~45-second `migrate`. The old manifest
belonged to the dropped database; the seed notices that (it is bound to the database's
oid, not just its name), moves it aside as `*.stale.json`, and starts a new one.

---

## Read this first

* **What it is.** `seed_walkthrough` builds a complete, synthetic database by DRIVING THE
  REAL VIEWS — it signs in as each person and POSTs to the product exactly as they would,
  then checks after every step that the state actually moved. Every seeded state carries
  the artifacts it implies: a released site has its survey link, allocation, Arka approved
  at both gates by two different people, CAD archive, BOQ, QC and Head passes, the PM's
  approval, and a ledger row for every hop. Nothing is written to *look like* a state.
* **Where it runs.** Only on a database that passes four checks, all of them, with no
  override flag: (1) the host is local; (2) the name starts `solarpms_walk`; (3) no user
  has an email outside `.invalid` except the one address below; (4) no email/WhatsApp
  NotificationLog row is `sent`. The first line of every run prints the host and name.
  Checks 3 and 4 read the database, so they run only after 1 and 2 pass.
  `solarpms_local` (the production restore) is refused at check 2 — and would fail
  check 3 as well: it holds real people.
* **Idempotent by area.** A second run finds every area present and writes nothing.
  `--only <area>` seeds one area (plus `users` and `reference` if absent).
* **Audit behind it.** `docs/WALKTHROUGH_SEED_AUDIT.md`.

### Pointing the app at it

`python-decouple` reads the environment before `.env`, so for one shell:

```
set DATABASE_URL=postgresql://solarpms_user:<password>@localhost:5432/solarpms_walk   (cmd)
$env:DATABASE_URL = "postgresql://solarpms_user:<password>@localhost:5432/solarpms_walk"   (PowerShell)
python manage.py runserver
```

`<password>` is the local Postgres password from your `.env`; it is not written here.
Consider also blanking `SUPABASE_KEY` in that shell: the walk never needs real storage
(see *Files*), and the local `.env` carries the production key.

---

## Logins

Every account shares one password: **`WalkPass!2026`**. The seed never prints it; this
file and `seed_walkthrough.py` are the only places it appears.

| Username | Role | Flags | Lands on | What it is for |
|---|---|---|---|---|
| `walk.admin` | Admin | — | `/dashboard/admin/` | Admin Panel: users, flags, master switches, preferences, checklists, catalogue. The only Admin (the product allows one) |
| `walk.sysadmin` | System Admin | — | `/sub-admin/projects/` | Sub-admin shell, warehouse screens |
| `walk.pm` | PM | — | `/dashboard/pm/` | Assigned PM on every site and project: PM approval queue, PM change requests, SCM-request triage, Not Applicable, punch-point waiver, issue close/reopen |
| `walk.coord` | Project Coordinator | — | `/dashboard/pm/` | Coordinator on WALKE01; approved the Net Metering task; holds Completion Certificates |
| `walk.se` | Site Engineer | — | `/dashboard/site-engineer/` | Holds the SE tasks on WALKE01 and two on WALKL01; confirmed the GRNs; submits tasks for approval |
| `walk.qaqc` | Site Engineer | `is_qaqc` | `/dashboard/site-engineer/` | Approves / rejects submitted OPEX tasks (raises punch points). Holds Net Meter Installation so it can see WALKE01 |
| `walk.design` | Design | — | `/dashboard/design/` | The designer on every design site and the Residential BOQs |
| `walk.designqc` | Design | `is_design_qc` | `/dashboard/design/` | Gate 1: Arka review and package QC; recorded the BOQ correction on WALKC07; named QC reviewer on WALKD03 |
| `walk.designhead` | Design | `is_design_head` | `/dashboard/design/` | Gate 2, allocation, survey links, change-request verdicts, send-back / return-to-PM |
| `walk.designdeputy` | Design | named as the Head's deputy | `/dashboard/design/` | Has Head authority as deputy; no action seeded as deputy |
| `walk.scm` | SCM | `is_warehouse_keeper` | `/landing/` → `/dashboard/scm/` | Groups, locks, orders, payments raised, holds answered, challans, GRN on-behalf, SCM change requests; keeper of two warehouses |
| `walk.finance` | Finance | `is_payment_approver` | `/landing/` → `/dashboard/finance/` | Approved / partly approved / held / rejected the payments |
| `walk.finpay` | Finance | — | `/landing/` → `/dashboard/finance/` | Marked the payment paid (the approver may not); invoiced and received the Residential milestones |
| `walk.finassignee` | Finance | — | `/landing/` → `/dashboard/finance/` | **The one real address** — see below |
| `walk.ceo` | CEO | — | `/landing/` → `/dashboard/ceo/` | CEO dashboard, daily report, payment queue |
| `walk.bd` | BD | — | `/dashboard/bd/` | BD dashboard |

Every account has email **and** WhatsApp notifications OFF, set through the Admin's own
`admin_notification_prefs` screen, and the master switches (`email_enabled`,
`whatsapp_enabled`) are OFF, set through `admin_master_switches`. In-app notifications
are on and are real: the seed's own actions leave them in people's bells.

### The one address that is not `.invalid`

`walk.finassignee` has the email `santosh@horizonrenewablepower.com`. That is not a
choice: `utils.attach_residential_template()` looks up the Finance owner of the
send-invoice and finance-confirmation tasks by exactly that address
(`utils.RESIDENTIAL_FINANCE_ASSIGNEE_EMAIL`) and raises if no user has it, which rolls
back `project_activate` — so without it no Residential project can be activated at all.
The account has both preferences off, the master switches are off, and the seed runs
with the notification API keys blanked. Check 3 admits exactly that value, read from
utils at run time, and no other.

**DEFERRED (a defect, not fixed here):** the assignee is a hardcoded email in `utils`. It
should be configurable (a setting, or a flag on the Finance profile).

---

## What is where

Site codes ARE the project IDs. Every tender is OPEX (shown as RESCO). Dates are
relative to the day the seed ran.

### Tender WALKDSN — the design walk (area `design`)

One site per design state. Open as `walk.designhead` → the tender's design sites page,
or the site's design workspace.

| Site | State | Shows |
|---|---|---|
| WALKD01 | no DesignAssignment | where every real walk starts — record a survey link on it |
| WALKD02 | awaiting_allocation | survey link recorded |
| WALKD03 | in_design | allocated, due date committed; **named QC reviewer** (`walk.designqc`) instead of the open pool |
| WALKD04 | arka_submitted | Arka waiting for gate 1 |
| WALKD05 | awaiting_head_arka | gate 1 passed, waiting for the Head |
| WALKD06 | arka_rejected | rejected at gate 1 with an electrical-design category |
| WALKD07 | survey_returned | Design Hold taken from in_design |
| WALKD08 | arka_submitted | held at arka_submitted, then lifted by a new survey link — **restored** (D18) |
| WALKD09 | artifacts_uploaded | Arka both gates, CAD, BOQ complete — ready for QC |
| WALKD10 | in_qc | QC started |
| WALKD11 | awaiting_head_qc | QC passed |
| WALKD12 | awaiting_pm_approval | Head passed; in `walk.pm`'s PM approval queue |
| WALKD13 | pm_rejected | PM rejected with a remark; the Head's return / send-back choice |
| WALKD14 | in_design, attempt 2 | PM rejected, Head sent it back with a full redo scope |
| WALKD15 | released, attempt 2 | failed QC once (BOQ redo), fixed, passed, released |
| WALKD16 | released, **Active** | released cleanly then activated — its Design mirror task reads Done |

### Tender WALKCR — change requests (area `changes`)

Eight released sites; one change request per verdict. Open as `walk.designhead` (QC
review page), `walk.pm` (PM approval queue lists SCM requests awaiting the PM), or
`walk.scm` (change-request form).

| Site | Verdict | Path |
|---|---|---|
| WALKC01 | pending | raised by the PM |
| WALKC02 | accepted | raised by the PM while in a DRAFT group; the Head accepted — the site left the group with the red "PM change request" reason and is back in design on attempt 2 |
| WALKC03 | rejected | raised by the PM, rejected by the Head |
| WALKC04 | with_pm | raised by SCM, waiting for the PM |
| WALKC05 | pm_rejected | raised by SCM, rejected by the PM |
| WALKC06 | withdrawn | raised by SCM, withdrawn by SCM |
| WALKC07 | corrected | raised by SCM, forwarded by the PM, BOQ corrected by Design QC, marked corrected by the Head |
| WALKC08 | (none) | released and in a LOCKED group — a change request is refused here |

### Tender WALKPRC — procurement and payments (area `procurement`)

| What | Where |
|---|---|
| Released sites aged 30 / 26 / 21 / 17 / 11 / 6 / 3 days | WALKP01–P07 |
| LOCKED group "WALK Batch A - Modules & Structure" | P01, P02 |
| DRAFT group "WALK Batch B - BOS & Cabling" | P03 in it; P04 added then removed by SCM with a reason |
| The post-QC pool | P04–P07 (ageing column spans fresh to overdue) |
| Order 1 — group raise against Batch A, ₹10,00,000, PO + invoice documents | payment 1 **paid** (approved by `walk.finance`, paid by `walk.finpay`); payment 2 **approved in part** (₹1,00,000 of ₹1,50,000, with the remark); payment 3 **held then rejected** |
| Order 2 — purchases-workspace record, no site, ₹5,00,000, PI document | payment 1 **pending approval**; payment 2 **on hold**; payment 3 **approved**; payment 4 **held, answered by SCM, back to pending** |

Open: `walk.finance` → payment queue (OPEX tab); `walk.scm` → purchases workspace, the
tender's site groups, the tender's order list; `walk.ceo` → CEO dashboard.

### Tender WALKDLV — deliveries (area `delivery`)

WALKL01, activated; two SE tasks held by `walk.se`.

| Challan | State |
|---|---|
| WALK-DC-0001 | Expected (issued from warehouse WALK-WH-DEL) |
| WALK-DC-0002 | Partially Received (GRN by `walk.se`); carries a delivery issue |
| WALK-DC-0003 | Received (GRN by `walk.se`) |
| WALK-DC-0004 | Rejected (nothing arrived) |
| WALK-DC-0005 | Received **on behalf** by `walk.scm`, with the reason |

The four Delivery mirror tasks on WALKL01 are derived from these (Inverters Done; the
others In Progress).

### Tender WALKEXE — site execution (area `execution`)

WALKE01, activated; coordinator `walk.coord`; SE tasks held by `walk.se`.

| Task | State |
|---|---|
| Net Metering Approval | Done — submitted by the PM, approved by the coordinator |
| Civil Work and MMS Installation | Done — checklist answered (yes / na / no with remarks), submitted by SE, approved by QA/QC |
| Module Installation | **awaiting approval** (submitted; checklist answers now closed) |
| LA and Earthing Installation | rejected by QA/QC — **open punch point**, back In Progress |
| DC Cable Laying with Conduit | rejected — **punch point waived** by the PM |
| DCDB and ACDB Installation | In Progress, checklist partly answered |
| Inverter Installation | **Blocked**, with its blocking issue |
| RMS Installation | **Not Applicable** (PM) |
| Solar Generation Meter Installation (CT-operated) | **renamed** by the PM |
| AC Cable Laying — Block A / Block B | **duplicated for locations** |
| Testing & Commissioning | due date set, Not Started |
| WALK Site cleaning and debris removal | **added** by the PM (not in the template) |
| Installation phase | **reordered** (the last task moved to the top) |

Issues on WALKE01: one Open, one In Progress, one Resolved, one Closed, one Reopened.

### Residential (area `residential`)

IDs come from the real generator (`HRP-RES-<year>-NNN`); on a fresh database 001–004.
Find them by customer name.

| Customer | State |
|---|---|
| WALK Residential Draft Sharma | Draft (not activated) |
| WALK Residential Active Iyer | Active; BOQ **Acknowledged**; milestones M1 Received, M2 Invoiced, M3 Pending; a Residential vendor order with a pending payment; two PM tasks Done, one In Progress |
| WALK Residential Revision Khan | Active; BOQ **Revision Requested** |
| WALK Residential Submitted Das | Active; BOQ **Submitted** |

### Reference (area `reference`)

Warehouses `WALK-WH-DEL` and `WALK-WH-MUM` (keeper `walk.scm`), `WALK-WH-SITE` (no
keeper); three `WALK …` vendors; the seven OPEX installation checklists
(`seed_opex_installation_checklists`, reused as is).

---

## Files: why a download does nothing

The local `.env` carries the PRODUCTION Supabase key, so the seed never touches storage.
While it runs, uploads are stubbed and `SUPABASE_URL` / `SUPABASE_KEY` are blanked, so
any call it did not stub fails instead of reaching production. Every file it "uploads"
is named `SEEDED-NO-FILE-<kind>` — `SEEDED-NO-FILE-cad_zip.zip`, `SEEDED-NO-FILE-po.pdf`,
`SEEDED-NO-FILE-pi.pdf`, `SEEDED-NO-FILE-invoice.pdf` — and recorded in the bucket
`walkthrough-stub`, which exists nowhere. The rows are real; the objects are not. A
download link on any of them does nothing, and the name on screen says why. (The CAD
archive was a real, valid zip when it was validated; only the upload was skipped.)

## Time: what is backdated, and how

Histories are driven under a simulated clock (`SimClock`): while a step runs,
`django.utils.timezone.now()` returns a past instant, so **the product itself writes every
timestamp it stamps with `timezone.now()`** — `StatusTransition.occurred_at`,
`ActivityLog` times, `created_at`/`requested_at`/`submitted_at`/`approved_at`/
`released_at`/`pm_approved_at`/`locked_at`/`added_at`/`removed_at`/`completed_at`/
`blocked_since`/`raised_at`/`resolved_at`/`closed_at`, `PaymentRequest.requested_date`,
Notification times and the rest. Nothing is updated afterwards; only rows this seed's
own requests create are affected; and within any one history the clock only moves
forward, so no ledger contradicts its own sequence (checked in the tests).

Two things are stamped with `date.today()` and cannot be moved: `DCLineItem.grn_date`
and `PaymentMilestone.invoice_date` / `received_date`. Those steps are driven at today's
date, minutes apart, so they agree with their ledger rows.

## Tearing down

`teardown_walkthrough --dry-run` prints a verdict per area; `teardown_walkthrough
[--only <area>]` removes. It runs Django's deletion Collector first and refuses — deleting
nothing — if removal would need a StatusTransition row force-deleted (the append-only
ledger; there is one sanctioned bypass in the codebase and this is not a second), would
delete or null a row the seed did not record (anything a walker made), or is blocked by
a PROTECT.

| Area | Can be torn down this way? |
|---|---|
| `users` | Yes — but only together with `reference`, and only when no other area exists (every other area's rows point at these users) |
| `reference` | Yes — with `users`, when no other area exists (orders and challans point at the vendors and warehouses) |
| `design`, `changes`, `procurement`, `delivery`, `execution`, `residential` | **No** — every one writes StatusTransition rows. Drop the database. |

---

## What this database cannot show you

States from the audit that NO product code reaches. They are absent here because a
person cannot reach them either; writing them directly would fake a workflow that does
not exist.

* **Project In Progress / Commissioned / On Hold / Cancelled** — only Draft and Active
  have writers (`project_create`/`create_opex_site`, `project_activate`/`opex_site_activate`); closure is phase 5 (D-4).
* **Design `due_date_proposed`** — no writer anywhere.
* **Design `qc_failed`** — its writer was removed (Session C); a QC fail now opens the next attempt directly (see WALKD15).
* **Execution site groups** — `site_group_create` hardcodes procurement; no path creates an execution group.
* **The COD, HOTO and As-Built mirror tasks moving** — no code derives them; they stay Not Started.
* **CAPEX activation** — no CAPEX task template exists, so `attach_opex_template` raises.

## DEFERRED — reachable, but not seeded (and why)

* **File downloads** — every file is a stub (see *Files*).
* **Survey FILE upload and checklist photos** — both need storage; the survey uses the
  link path, and the seeded checklists have `requires_photo=False`.
* **Design `allocated`** — reachable only through the legacy branch of
  `_status_after_unblock()` (no approved due-date commitment); allocation always
  commits one now.
* **The design due-date proposal / approval / change screens** and the Head's
  due-date extension request — reachable, not yet seeded.
* **The deputy acting as Head** — `walk.designdeputy` holds the authority; no verdict
  was recorded as deputy.
* **`is_hse`** — the flag has no reader and no writer in the product; nothing to show.
* **The deputy link itself** has no portal writer (Django admin only) and is set
  directly — `# NO PRODUCT PATH` in the seed. So is the first Admin account, because on
  an empty database there is no Admin to create one through `user_create`.
* **External notifications** — by design, never. `email`/`whatsapp` rows are all `skipped`.
* **Zoho-webhook-created projects, the EOD digest** — they send real mail; not driven.
* **The hardcoded Residential finance assignee email** — a defect; see *Logins*.

## Superseded demo seeds

`seed_opex_test_data` + `seed_scm_handoff_data` + `teardown_opex_test_data` (the DEMO
namespace) and `seed_order_demo` + `teardown_order_demo` (ORDDEMO) are **superseded by
this seed** and left in place. Their released sites have no CAD file, no PM approval and
no design ledger, their OPEX "Done" tasks carry no approval, and their challan skips the
mirror sync — states the product cannot produce. Prefer this database for any walk.
`seed_scm_pilot` / `fixture_scmpilot_boq` are NOT superseded: they are a rehearsal
layered onto the production restore, a different job.
