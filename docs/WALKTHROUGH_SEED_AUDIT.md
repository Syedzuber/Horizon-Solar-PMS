# Walkthrough seed — Part A audit and proposed design

> **Superseded in part by the Part B sign-off (26 Sep 2026).** Built as
> `seed_walkthrough` / `teardown_walkthrough`; the map is `docs/WALKTHROUGH_DATA.md`.
> Changed from the proposal below:
> - Check 4 was added: no email/WhatsApp NotificationLog row with status `sent`.
> - The Residential finance address is **not** swapped. A user with that exact
>   address is seeded, and check 3 admits it and no other.
> - The teardown **never** bypasses the append-only ledger. It refuses instead, and
>   the reset is DROP DATABASE and re-seed.
> - Backdating is done by a simulated clock during the drive, not by `.update()`.
> - `is_hse` is not set on anyone.
> - There are 16 users. `walk.finassignee` holds the real address.

Audit only. No product code, command or test was changed to produce this. Everything is
located by function, field or constant name. Each claim was checked by grep or a
read-only query in the session that wrote this (25 Sep 2026).

---

## A1. Existing seed and teardown commands

All of them live in `projects/management/commands/`.

| Command | What it creates | Namespace / marker | Idempotent? | Teardown |
|---|---|---|---|---|
| `seed_opex_test_data` | Users, warehouses and the tender:<br>• 7 `demo.*` users (no Admin)<br>• 3 `StockLocation`<br>• 1 OPEX tender `DEMOTEND` with 4 sites (2 activated by replicating the view inline)<br>Design and BOQs:<br>• 1 Residential `DEMO-RES-01`, activated by replicating the view inline<br>• 1 in-design and 1 released `DesignAssignment` (direct writes)<br>• 1 procurement and 1 execution `SiteGroup`<br>• 2 BOQs<br>Delivery and tasks:<br>• 1 `DeliveryChallan`<br>• task statuses written directly | `DEMO` prefix, plus a pk manifest at `~/.horizon-pms-demo/demo_manifest.json` | **No.** A second run prints "already present" and refuses | `teardown_opex_test_data` |
| `seed_scm_handoff_data` | Layered on the above:<br>• tender `DEMOSCM` with 6 released sites (direct writes)<br>• 1 locked and 1 draft group (direct writes)<br>• 2 historical removals<br>• 1 ad-hoc BOQ row | `DEMO`; appends to the same manifest | **No.** Refuses if its Program exists | `teardown_opex_test_data` |
| `teardown_opex_test_data` | Deletes the manifest's pks in reverse order. Also deletes `StatusTransition` rows with `QuerySet.delete()`, which bypasses the append-only guard. Removes design-bucket objects through `delete_design_objects()` | reads the manifest only; no name matching | n/a. Dry run is the default; `--confirm` deletes | — |
| `seed_order_demo` | • 3 `orddemo.*` users and 3 vendors<br>• 2 tenders with 7 sites left in Draft<br>• 7 released sites (direct writes, **no Arka, no DesignFile**)<br>• OPEX BOQs and 3 groups, one locked by direct write<br>• deliberately **no** VendorOrder and no PaymentRequest | `ORDDEMO`; manifest at `~/.horizon-pms-orderdemo/` | **No.** Refuses if present | `teardown_order_demo`, which also refuses if an order now references the seed |
| `seed_scm_pilot` | • 6 `scmpilot.*` users, 3 warehouses<br>• tender `SCMPILOT` with 6 sites, activated **by POSTing to the real `opex_site_activate`**<br>• SE task assignment through `assign_tasks_to()`<br>• 5 sites released by direct write; 1 left with no assignment, for a hand walk | `SCMPILOT`; a record manifest only, never read back | **Yes.** Uses get-or-create per row and reports created vs reused | **None, by decision** ("a re-dump is the reset path") |
| `fixture_scmpilot_boq` | BOQ plus 28 `BOQItem` rows on SCMPILOT03/04/05 | `SCMPILOT` | no | none |
| `seed_opex_installation_checklists` | 7 active OPEX installation checklists (reference content). No users, projects or namespace | idempotent by checklist `code` | **Yes** | none. Nothing to tear down; this is product content |

**Shared machinery.** `_demo_support.py` provides `print_db_banner`, `require_local_database` (host-only, with a `--i-know-this-is-not-local` override), `Manifest` and `high_water`.
- `seed_scm_pilot` duplicates the interlock instead of importing it, and has no override flag.
- `Manifest.save()` hardcodes `'namespace': DEMO_PREFIX`.

**What none of them covers:**
- a design state reached through the views (except the one site `seed_scm_pilot` leaves for a hand walk)
- a `DesignFile` / CAD
- the PM approval gate (`awaiting_pm_approval`, `pm_rejected`, `pm_approved_*` stamps)
- any change request, in any verdict
- any `VendorOrder`, `PaymentRequest` or `PaymentRequestHold`
- GRN confirmation or override
- two-step completion, punch points, Not Applicable
- the issue lifecycle past Open
- checklist answers
- BOQ Submitted / Acknowledged / Revision Requested
- payment milestones past Pending
- an Admin, System Admin, BD, Design QC or Design Head in the DEMO set
- per-user notification preferences: **no seed turns off `email_notifications` or `whatsapp_notifications`**, and none sets `SystemSettings`

**Where they break the standing rule** (never fixture a terminal status without its artifacts; see A4):
- **`released` with no CAD `DesignFile`** in all four seeds that release sites. Also no `pm_approved_*` stamps and no design `StatusTransition` rows.
- **`seed_order_demo` released sites also have no Arka.**
- **`seed_opex_test_data._vary_task_statuses`** writes `Done` onto OPEX tasks with no `submitted_*` / `approved_*`. `_apply_task_status_change` refuses exactly that on OPEX (`task.approved_at is None`).
- **`seed_opex_test_data._seed_delivery_challan`** copies `create_delivery_challan` but omits the `sync_delivery_mirrors(project)` call the view now makes. The copy has drifted.

**Two constraints any new seed inherits:**
1. **`tests_design_pm_gate_live`** parses every non-test `.py` under `projects/`, management commands included.
   - It fails if a function outside `INTENDED_WRITERS` writes `awaiting_pm_approval`, `pm_rejected` or `released`.
   - The four seeds are admitted **by name** in `SEED_RELEASE_WRITERS`. A fifth writer fails `test_a1`.
   - A computed `status=` write fails `test_a3`.
   - **So a new seed may not write design statuses directly. It has to drive the views.**
2. **`seed_opex_test_data` and `seed_scm_handoff_data` have test coverage** in `tests_demo_data.py`.

**Recommendation:**

| Command | Verdict | Why |
|---|---|---|
| `seed_opex_installation_checklists` | **Reuse**: call it from the new seed | Reference content, idempotent |
| `_demo_support.Manifest`, `high_water`, `print_db_banner` | **Reuse** | Shared machinery |
| `require_local_database` | **Do not reuse** | Host-only, and it has an override flag (see A5) |
| `seed_opex_test_data`, `seed_scm_handoff_data`, `teardown_opex_test_data` | **Superseded** | Their fixtures break the artifact rule |
| `seed_order_demo`, `teardown_order_demo` | **Superseded** | The walkthrough covers the pool through to paid orders |
| `seed_scm_pilot`, `fixture_scmpilot_boq` | **Not superseded** | Different job: an additive rehearsal on the production restore |

For the superseded commands:
- Leave them in place this session. Retiring them means editing `SEED_RELEASE_WRITERS` and `tests_demo_data.py`, which is out of scope here.
- The new doc says they are superseded.

---

## A2. Roles and capability flags

**`UserProfile.ROLE_CHOICES` (10):** Admin, System Admin, PM, Project Coordinator, Site Engineer, Design, Finance, SCM, CEO, BD.

**`Task.ROLE_CHOICES`** uses `'BD / Sales'` where the profile uses `'BD'`.

**Capability flags on `UserProfile`:**

| Flag | Read by | Status |
|---|---|---|
| `is_design_head` | `permissions.user_is_design_head` and gate-2 views | live |
| `is_design_qc` | `user_is_design_qc`, the gate-1 pool | live |
| `design_head_deputy` (self-FK) | `user_is_design_head_deputy` | live |
| `is_qaqc` | `user_can_approve_task` arm, punch points | live |
| `is_warehouse_keeper` | `StockLocationForm` keeper validation | live; grants nothing on its own |
| `is_payment_approver` | `user_is_payment_approver`; `AdminUserEditForm.clean` limits it to `PAYMENT_APPROVER_ROLES = {'Finance','CEO'}` | live |
| **`is_hse`** | **nothing**; its only non-test mentions are `models.py` and `seed_opex_test_data` | storage-only |

**Preferences:** `email_notifications` and `whatsapp_notifications` both default True.

The `UserProfile` comment "NOTHING READS THESE YET" is stale for `is_qaqc`, `is_warehouse_keeper` and `is_payment_approver`. It is still true for `is_hse`.

### Users the walkthrough needs

Each user earns a place because some rule needs a *distinct person*, or a role or flag has its own screen.

| Username | Role + flags | Why it earns a user |
|---|---|---|
| `walk.admin` | Admin | Admin Panel, where flags are set, catalogue and settings. `UserCreateForm` allows exactly one Admin, and an empty DB has none |
| `walk.sysadmin` | System Admin | Sub-admin shell and warehouse CRUD; lands on `subadmin_projects` |
| `walk.pm` | PM | Assigned PM on every site: PM approval gate, PM-origin change requests, Not Applicable (assigned PM only), punch waiver (managers only), SCM-CR triage |
| `walk.coord` | Project Coordinator | Linked through `Project.coordinators`. Second triager and the coordinator arm of `can_approve_design_release`. Owns "Completion Certificates" |
| `walk.se` | Site Engineer | Holds the SE tasks. `confirm_grn` needs `user_can_view_project`, i.e. a held task. Submits OPEX tasks for approval |
| `walk.qaqc` | Site Engineer + `is_qaqc` (+ `is_hse`) | `task_approve` refuses the submitter, so the approver must be someone else. Shows the QA/QC arm; `is_hse` is set only so the flag appears somewhere |
| `walk.design` | Design | Assigned designer. Gate views refuse the designer at both gates |
| `walk.designqc` | Design + `is_design_qc` | Gate 1. `_other_gate_actor_conflict` refuses one person both verdicts |
| `walk.designhead` | Design + `is_design_head` | Gate 2, CR accept/reject/correct, send-back / return-to-PM |
| `walk.designdeputy` | Design, named in the Head's `design_head_deputy` | Shows deputy authority, a separate predicate |
| `walk.scm` | SCM + `is_warehouse_keeper` | Groups, lock, orders, SCM-origin CRs, challans, GRN override. Keeper of a warehouse |
| `walk.finance` | Finance + `is_payment_approver` | Approve / hold / reject. The approver cannot be the requester (SCM) |
| `walk.finpay` | Finance | Marks paid. `mark_payment_paid` refuses the approver, so this must be a second Finance user. Also the Residential finance assignee (see A3 blocker 1) and milestone invoicing |
| `walk.ceo` | CEO | CEO dashboard and daily report. `LANDING_ROLES` route CEO / Finance / SCM through `/landing/` |
| `walk.bd` | BD | BD dashboard |

**15 users.** Password rule: `UserCreateForm` needs at least 8 characters. Every address is `@walk.invalid`, and the phone numbers are valid-format but fake.

---

## A3. States per module, and which need the real views

**Key:**
- **V** — reachable only by POSTing to a view (request-bound, no extracted core)
- **F** — a request-free function exists
- **∅** — no writer anywhere in the product

### Blockers on an empty migrated database

What an empty migrated DB already contains:
- **From migrations:** 6 `VendorCategory` (0009), 50 `TaskDurationTemplate` (0034), 37 Residential + 207 OPEX `BOQItemMaster` (0047, 0057), the RESIDENTIAL template v1 (0067: 9 phases / 52 tasks), and the OPEX template v1 (0075: 7 phases / 23 tasks / 8 mirrors).
- **No migration creates:** users, `SystemSettings` (created lazily by `SystemSettings.get()`), checklists, stock locations, or a CAPEX template.

The blockers:
1. **Residential activation needs a real address.** `attach_residential_template` looks up `UserProfile` by `user__email == RESIDENTIAL_FINANCE_ASSIGNEE_EMAIL = 'santosh@horizonrenewablepower.com'` (`utils.py`) and raises `DoesNotExist` if it is missing. That rolls back `project_activate`. This collides with the `.invalid` rule.
2. **Order raises need storage.** Every order-raise path calls `supabase_storage.get_supabase_client()` inside `order_views._upload_and_record_documents`, even with zero documents. The raise validators then require at least one PO or PI file, uploaded to `settings.SUPABASE_BUCKET` (the **public** bucket).
3. **CAD upload needs storage.** `design_views.design_artifact_upload` calls `upload_design_file`, which writes to the private design bucket.
   - Both buckets are **production Supabase**: `.env` carries the production `SUPABASE_URL` and `SUPABASE_KEY`.
   - `artifacts_uploaded` and `in_qc` require a current `cad_zip` `DesignFile` (`_package_is_complete`).
4. **CAPEX cannot be activated:** no CAPEX `TaskTemplate` exists, and `attach_opex_template` raises.

### Design (`DesignAssignment.status`)

**Single writer:** `apply_design_status` (F).

| State | Reached by | Notes |
|---|---|---|
| no row ("Survey") | — | the starting point |
| `awaiting_allocation` | V `design_survey_link_set` | no storage; host must be in `SURVEY_LINK_ALLOWED_HOSTS`. The alternative, `design_survey_upload`, needs storage |
| `in_design` | V `design_allocate`, or F `_allocate_one` | |
| `arka_submitted` | V `design_arka_submit` | |
| `awaiting_head_arka` | V `design_arka_approve` | gate 1 |
| `arka_rejected` | V `design_arka_reject` / `design_arka_head_reject` | reason and category required |
| `artifacts_uploaded` | V `design_artifact_upload` (CAD) + V `design_boq_complete` | BOQ needs ≥1 qty>0 and every mandatory item quantified |
| `in_qc` | V `design_qc_start` | |
| `awaiting_head_qc` | V `design_qc_pass` | |
| QC-fail loop | V `design_qc_fail` / `design_head_qc_fail` → `_open_next_attempt` | opens attempt N+1 |
| `awaiting_pm_approval` | V `design_head_qc_pass` or `design_head_return_to_pm` | |
| `pm_rejected` | V `design_pm_reject` | |
| send-back | V `design_head_send_back` | opens attempt N+1 with a redo scope |
| `released` | V `design_pm_approve` | the only product writer |
| `survey_returned` (Hold) | V `design_mark_blocked` | |
| restore from Hold | V `design_survey_link_set` | |

**∅ (no writer):** `due_date_proposed`, `qc_failed` (removed in Session C), and `allocated` (reached only through the legacy branch of `_status_after_unblock`).

**All of the gate and PM states are V-only**, and the parse test forbids writing them directly.

### PM approval gate

- **Views (V):** `design_pm_approval_queue`, `design_pm_approve`, `design_pm_reject`.
- **Authority:** `can_approve_design_release`, meaning the assigned PM or a coordinator.

### Change requests (`DesignChangeRequest.verdict`)

There are seven verdicts, all V:

| Verdict | Written by | Notes |
|---|---|---|
| `pending` | `design_change_request` | PM-origin, or SCM-origin with no triager |
| `with_pm` | `design_change_request` | SCM-origin with a triager |
| `pending` from `with_pm` | `design_change_request_forward` | |
| `accepted` | `design_change_request_accept` | opens a `pm_change_request` attempt, clears the release stamps, removes the site from a draft group |
| `rejected` | `design_change_request_reject` | |
| `pm_rejected` | `design_change_request_pm_reject` | |
| `withdrawn` | `design_change_request_withdraw` | |
| `corrected` | `design_change_request_correct` | needs `BOQCorrection` rows from V `boq_correct` after the raise |

- **Window:** `change_request_window_open` — the site is released and **not** in a locked group, or the pre-release QC window for PM/coordinator.
- `_change_request_email` calls `request.build_absolute_uri`, so every CR path needs a request.

### Procurement groups

| State | Reached by | Notes |
|---|---|---|
| pool | read by `post_qc_pool` | released sites with no live procurement membership |
| draft group | V `site_group_create` | hardcodes procurement |
| add a site | F `_add_sites` | |
| remove a site | F `remove_from_group` | |
| **locked** | **V `site_group_lock` only** | refuses an empty group, or a member with an open CR. No unlock exists anywhere |
| execution group | **∅** | |

### BOQ

`BOQ.status` values: Draft, Submitted, Acknowledged, Revision Requested.

| State / action | Reached by | Notes |
|---|---|---|
| Residential Draft | V `boq_detail` GET | seeds rows from the template |
| OPEX Draft | V `opex_boq_entry` | F `_boq_upload_apply` also exists |
| Submitted | V `boq_detail` `submit_design` / `boq_submit` | |
| Acknowledged | V `boq_detail` `acknowledge_scm` / `boq_acknowledge` | notifies (see A5) |
| Revision Requested | V `boq_request_revision` | |
| OPEX design lock | V `design_boq_complete` | |
| reviewer correction | V `boq_correct` | |
| group lock | V `site_group_lock` | the lock predicate is `project_boq_is_group_locked` |

### Delivery challans

`DeliveryChallan` statuses: Expected, Partially Received, Received, Rejected.

| State / action | Reached by | Notes |
|---|---|---|
| Expected | V `create_delivery_challan` | inline; also calls `sync_delivery_mirrors` |
| GRN | V `confirm_grn` | needs an SE who holds a task on the site |
| override / on-behalf | V `override_grn` | SCM; `grn_on_behalf_reason` required |
| status rollup | F `recalculate_dc_status` | always syncs the delivery mirrors |
| delivery issue | V `create_delivery_issue` | |
| warehouses | V `stock_location_create` / edit / toggle | |

### Payment requests (`PaymentRequest.status`)

Statuses: `pending_approval`, `approved`, `on_hold`, `rejected`, `confirmed` (label "Paid"). Partial approval is `approved` with `approved_amount < amount`.

| Transition | Reached by | Notes |
|---|---|---|
| raise → `pending_approval` | F `_create_order_payment` / V `vendor_order_create*`, `purchases_*` | |
| → `approved` | V `payment_approve` | |
| → `on_hold` | V `payment_hold` | creates a `PaymentRequestHold` |
| `on_hold` → `rejected` | V `payment_reject` | only from on_hold |
| `on_hold` → `pending_approval` | V `payment_hold_respond` | |
| → `confirmed` | F `payments.mark_payment_paid` | not the approver, not the requester, reference required, date not in the future and not before the raise |

- `signals.payment_transition_notices` sends in-app notices on commit for every payment transition unless `record_transition(notify=False)` was used. **No product payment writer passes it.**

### Vendor orders

- **No status field.** Paid, balance, committed and available are computed.
- **Raise (V, all need storage):** `vendor_order_create` (Residential; PO/PI required), `vendor_order_create_group` (PO/PI and a payment required), `purchases_new`.
- **Other:** `vendor_order_add_documents`, append-only.

### Residential

| State / action | Reached by | Notes |
|---|---|---|
| Draft | V `project_create` | real ID through `generate_project_id` |
| Active | V `project_activate` | needs `assigned_design_id`; creates milestones M1–M3; blocked by blocker 1 |
| milestone Pending → Invoiced | V `milestone_invoice` | |
| milestone → Received | V `milestone_receive` | also V `_apply_task_status_change` sync |
| **Project `In Progress` / `Commissioned` / `On Hold` / `Cancelled`** | **∅** | no writer anywhere |

### OPEX / RESCO sites

- **Create:** F `views.create_opex_site`.
- **Activate:** V `opex_site_activate`, which gives 7 phases / 23 tasks / 8 mirrors.

### Tasks

| State / action | Reached by | Notes |
|---|---|---|
| status change | V `task_status_update` / `task_detail_status_update` → `_apply_task_status_change` | the helper is request-bound; an unassigned task cannot move; In Progress needs a due date |
| **OPEX Done** | V `task_submit_for_approval` then V `task_approve` (a different person) | `_apply_task_status_change` refuses Done while `approved_at is None` |
| reject + punch point | V `task_reject` | |
| waiver | V `punch_point_waive` | |
| Not Applicable | V `task_set_not_applicable` | |
| assign | V `task_assign` / F `assign_tasks_to` | `assign_tasks_to` is silent |
| rename / reorder / duplicate | V `task_rename_save` / `phase_tasks_reorder` / `task_duplicate_locations_create` | |
| Blocked | V, through the same status helper | creates an Open `Issue` |
| COD / As-Built / HOTO mirrors | **∅** | no writer |

### Issues

Open → In Progress → Resolved → Closed (and Reopen), through V `update_issue_status`, `resolve_issue`, `close_issue` and `reopen_issue`. Resolving an issue does not unblock its task.

### Checklists

- **Content:** `seed_opex_installation_checklists` (7 checklists / 272 items, `requires_photo=False`).
- **Answers:** V `checklist_item_complete`, open while `checklist_answers_open` holds. A photo is needed only when `requires_photo` is set, and the photo upload needs storage.
- **Residential:** has no checklists.

**Summary.** Almost everything past creation is V-only. The request-free cores are:
- `create_opex_site`, `_allocate_one`, `_add_sites`, `remove_from_group`
- `recalculate_dc_status`, `sync_delivery_mirrors`
- `_create_order_payment`, `mark_payment_paid`
- `assign_tasks_to`, `_boq_upload_apply`

The seed should therefore drive `django.test.Client`, as `seed_scm_pilot` already does for activation, with `SERVER_NAME='localhost'`.

---

## A4. Artifacts each terminal state implies

A state is only seeded if these exist. The views write them all, which is the reason to drive them.

| State | Artifacts it implies |
|---|---|
| **design `released`** | Assignment:<br>• `released_at/by` and `pm_approved_at/by`<br>• ≥1 attempt with `qc_verdict=passed` by the QC reviewer and `head_verdict=passed` by the Head, two different people, with `closed_at`<br>Artifacts:<br>• the current `ArkaSubmission` approved at both gates<br>• a current `cad_zip` `DesignFile`<br>• a BOQ with every mandatory item quantified, plus `boq_submitted_at`<br>Ledger and notices:<br>• a `design_assignment` `StatusTransition` for every hop, including `REASON_DESIGN_HEAD_PASSED` and `REASON_DESIGN_PM_APPROVED`<br>• ActivityLog codes per hop<br>• the in-app notice to the PM from the head pass<br>• on an activated site, the Design mirror task synced |
| `awaiting_pm_approval` / `pm_rejected` | Everything above up to the head pass, plus the PM's remark on the rejection transition |
| QC-fail loop | Attempt 1 closed with a failed verdict and remarks (a CHECK requires them), and attempt 2 opened as `qc_failed` |
| hold (`survey_returned`) | `survey_returned_at/by/reason` and ActivityLog `design_blocked` |
| CR `accepted` | A new attempt (`pm_change_request`), cleared `released_at/by`, `resulting_attempt`, and a draft-group removal carrying `CHANGE_REQUEST_REMOVAL_REASON` |
| CR `corrected` | `BOQCorrection` rows after the raise, attached as evidence |
| CR `withdrawn` / `pm_rejected` | SCM origin; the three required fields per the CHECKs; `pm_note` |
| locked group | Every member released, no open CR, `locked_by/at`, and ActivityLog `site_group_locked` per member |
| challan Received / Partial / Rejected | Lines with received / damaged quantities, `grn_confirmed_by` (an SE holding a task on the site) or the on-behalf reason, the Expected→X transition, and the delivery mirror synced |
| payment `confirmed` | A `VendorOrder` with a PO/PI document; `approved_amount`; `approved_by` ≠ requester; `confirmed_by` (Finance) ≠ approver; reference and date; transitions pending→approved→confirmed; in-app notices |
| payment `on_hold` / `rejected` | A `PaymentRequestHold` with its reason; a rejection needs `decision_reason` |
| OPEX task Done | A due date; `submitted_*` by one person and `approved_*` by another; `completed_at`; transitions |
| task Blocked | `blocked_since`, and an Open Issue linked to the task with its transition |
| punch point waived | A `task_reject` (submitted, then rejected) and a waiver reason from a manager |
| issue Closed | Assignee, In Progress, `resolution_note`, and each transition |
| Residential BOQ Acknowledged | Submitted first (`BOQRevision`, `submitted_by`), then the SCM acknowledgement revision and transition |
| milestone Received | Invoiced first; the Finance task synced to Done |

---

## A5. Proving the database is not production

### Signals available

- **Host.** `DATABASES['default']['HOST']`. Production is `postgres.railway.internal` inside Railway and `acela.proxy.rlwy.net` from a laptop. Local is `localhost`. A host check alone passes **the production restore**: `solarpms_local` is on `localhost` and holds **76 users, 67 of them with non-`.invalid` addresses**, plus 155 projects (read-only query, 25 Sep).
- **Name.** Production is `railway`; the restore is `solarpms_local`.
- **`DEBUG`.** True in the local `.env`. The Django test runner forces it False.
- **A marker row.** There is no table for one without a migration.
- **The data itself.** Every user in a synthetic DB has a `.invalid` address. An empty migrated DB has **zero** users: no migration creates one, and 0003 only upgrades existing superusers.

### Recommended rule

All checks must pass. There is **no override flag**, and the refusal lists every failing check by name.

1. The host is local (`LOCAL_HOSTS` or a socket path).
2. The database name starts with `solarpms_walk`. This refuses `railway` and `solarpms_local` by name.
3. **No `auth.User` has an email outside `.invalid`.** This refuses production and any restore of it, whatever it is called or hosted on.
4. (`DEBUG` is not used as a check. It adds nothing once 1–3 pass, and it would make the command untestable under the test runner.)

### Safety measures for every run (not checks)

- Force `SystemSettings` `email_enabled=False` and `whatsapp_enabled=False`.
- Blank `INTERAKT_API_KEY` and `ZEPTOMAIL_API_KEY` for the process, as `test_settings.py` does.
- Stub storage (see the design below).
- Compare `NotificationLog` rows with channel email/whatsapp and a status other than `skipped`, before and after. Abort on any increase.
- In-app `Notification` rows **are** expected, because they are artifacts of the states. They are recorded in the manifest.

---

## Proposed design (for sign-off)

### Target database

A dedicated database, e.g. `solarpms_walk`:
1. Create it empty.
2. Run `migrate`.
3. Run `seed_walkthrough`.

A `.env` for the walk points `DATABASE_URL` at it.

### Commands and documentation

- `seed_walkthrough` and `teardown_walkthrough`, in `projects/management/commands/`.
- A small `_walkthrough_support.py` holding the A5 rule and the stubs.
- `docs/WALKTHROUGH_DATA.md`.
- No product file is touched.

### Marker

- **Exact teardown:** the pk `Manifest` from `_demo_support`, at `~/.horizon-pms-walkthrough/<db name>.json`, outside the repo.
- **Visible namespace:** `WALK` on usernames, tender codes, site codes, vendor names and group names. Ledger and log rows have no name field; the manifest alone marks them, and they are captured by the `high_water` sweep.

### How states are reached

- `django.test.Client(SERVER_NAME='localhost')` with `force_login` per actor, POSTing to the real views in the order a person would.
- Request-free cores are called directly where A3 lists one.
- Nothing writes a design status, so `tests_design_pm_gate_live` needs no edit.

### Four in-process patches, active only during a seed run (no product edit)

1. `projects.design_views.upload_design_file` returns `('walkthrough-stub', path)`. This is the seam the tests already use. `validate_cad_zip` runs for real on a generated minimal zip.
2. `projects.supabase_storage.get_supabase_client` returns a no-op client, and `settings.SUPABASE_BUCKET` is overridden to `'walkthrough-stub'`. Order documents are recorded but never uploaded.
3. `projects.utils.RESIDENTIAL_FINANCE_ASSIGNEE_EMAIL` becomes `walk.finpay@walk.invalid`, the only way through blocker 1 without a real address.
4. The notification keys are blanked (A5).

Consequence: download links for seeded files fail during the walk. This is listed in DEFERRED.

### Areas (`--only <area>`)

Each area owns its own tender and sites, so they rebuild independently. Every area depends only on `users`.

| Area | Content |
|---|---|
| `users` | the 15 users, flags, deputy link, preferences off, `SystemSettings` off, warehouses, vendors, and the checklists (through `seed_opex_installation_checklists`) |
| `design` | tender `WALKDSN`, one site per design state (A3 list, including the QC-fail loop, send-back, hold and restore) and one site with no assignment |
| `changes` | tender `WALKCR`, released sites, one per verdict (7), plus a site in a locked group that shows the refusal |
| `procurement` | tender `WALKPRC`: pool, draft group, locked group; a group order, a Residential order and a purchases order; payments in pending, approved, partial, on hold, rejected and paid |
| `delivery` | activated site(s): challans Expected / Partial / Received / Rejected, GRN by SE, override on-behalf, a delivery issue |
| `execution` | activated OPEX site(s): In Progress, awaiting approval, Done (QA/QC-approved), rejected with an open punch point, a waived punch point, Blocked with an issue, Not Applicable, renamed, reordered, duplicated for locations; checklist answers; the issue lifecycle |
| `residential` | one Draft and one Active project (real `HRP-RES-…` IDs): BOQ Submitted / Acknowledged / Revision Requested, milestones Pending / Invoiced / Received |

### Idempotency

- Each area is recorded as its own manifest section.
- If a section exists and every pk is present, a run **skips that area and says so**. Two runs therefore produce the same row counts.
- `--rebuild` tears down the named area(s) by manifest and seeds them again.
- `--dry-run` plans without writing.

### Teardown

- Walks the manifest in reverse, per area or all, with `--dry-run` first.
- It runs no storage deletes, because the stub bucket never existed.

---

## Decisions needed before Part B

1. **Target DB and rule.** A dedicated `solarpms_walk` database, and the three-check rule in A5 with no override flag.
2. **The four in-process patches.** Especially #3, swapping the Residential finance assignee address for the seed run only. Without it, Residential activation is impossible on a DB with only `.invalid` users.
3. **`StatusTransition` deletion in the teardown.** This would be a **second** caller of the `QuerySet.delete()` bypass. `teardown_opex_test_data`'s docstring says "There is no other caller and there must not be one."
4. **Backdating timestamps.** Released-at, group created-at and similar ages can only come from direct `.update()` of timestamps. Views stamp "now", so without it every pool age reads 0 days. Proposal: allow timestamp backdating only (never a status), listed in the doc.
5. **The superseded commands.** Leave the DEMO / ORDDEMO commands in place and only mark them superseded in the doc. No test edits this session.

## Findings recorded, not fixed

- The `UserProfile` flag comment ("nothing reads these yet") is stale for 3 of 4 flags.
- `seed_opex_test_data` has drifted from `create_delivery_challan` (no mirror sync), and its OPEX Done tasks lack approval stamps.
- `is_hse` has no reader.
- `Project` statuses In Progress / Commissioned / On Hold / Cancelled have no writer.
- `DESIGN_DUE_DATE_PROPOSED` and `DESIGN_QC_FAILED` have no writer.
- There is no CAPEX template, so CAPEX sites cannot be activated.
- `in_app_notifications_enabled` is read by no send path.
- Residential activation hardcodes a real employee's address.
