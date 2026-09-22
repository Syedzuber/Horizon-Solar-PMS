# Execution module — deferred work

Things found while building, deliberately not acted on. Each entry says what is true, why
it was left, and what a session that picks it up has to do. Nothing here is a bug report
against work that shipped; it is the list of what the shipped work knowingly does not do.

---

## 1. `StockLocation` is the model; "Warehouse" is the label

**Deliberate, not an inconsistency.** The model keeps the name `StockLocation` (models.py,
Execution 1.2a, decision B-14). Every user-facing string — the DC create form's field
label, its placeholder, any future screen — says **"Warehouse"**.

The split is on purpose in both directions. The model is `StockLocation` because it is one
physical place that material is received into, held at, and issued from; naming it
`Warehouse` would invite a second model for stores and site containers, which are the same
thing. The label is "Warehouse" because that is the only word SCM uses for these three
buildings, and a form that asks for a "stock location" is a form people mis-answer.

A session that finds this jarring should change neither half without reading
`StockLocation`'s section note first.

---

## 2. DC line-item categories are still the hardcoded four (T4, deferred whole)

**State today.** `DCLineItem.CATEGORY_CHOICES` is four values — `Solar Modules`,
`Structure`, `Inverter`, `BOS` — hardcoded on the model and offered on the DC create form
regardless of project type.

**The gap.** The OPEX catalogue carries **16** categories (`Module`, `DCDB`, `Inverter`,
`MMS`, `ACDB`, `DC Cable`, `AC Cable`, `Pin Type Lug`, `Ring Type Lug`, `Conduit`,
`Cable Tray`, `Earthing`, `Solar Meter + CT`, `Data Logger+ WMS`, `Civil`, `BOS`). Only
`Inverter` and `BOS` overlap the hardcoded four, so **14 real OPEX categories have no
option at all** and SCM recording DC cable or earthing has nowhere to file it.

**Why it was deferred rather than fixed.** Sourcing the list by project type is a small
change at the form layer. Making the result *visible* is not, and shipping the first
without the second is worse than shipping neither: the new categories would be writable
and invisible. Three read paths are built on the four-string vocabulary —

- `models.get_material_status()` — iterates a **hardcoded local list** of the same four
  strings and filters `boq_category=` each one. A line filed as `DC Cable` is matched by
  none of the four passes, so it appears in no category row.
- `views._delivery_lookup_for_projects()` — `.values(..., 'boq_category')` into a dict
  consumed by `dashboard_pm` and `dashboard_scm`.
- the `project_overview` aggregation building `material_status_by_category`.

Wrong label but visible beats right label but invisible for a pilot.

**What a session that builds T4 must do, all in one change:**

1. Source the offered list from the project's `project_type`, following the precedent in
   `models.opex_catalogue_category_order()` — same queryset shape, `is_active=True`, order
   of first appearance by `sort_order`, derived not stored. That function hardcodes
   `project_type='OPEX'`; generalise it to take the type as an argument and leave the
   existing name delegating to it, so the BOQ picker's behaviour is provably untouched.
2. Fix all three read paths above in the same commit. `get_material_status()`'s local list
   is the one that must stop being a literal.
3. **Handle CAPEX.** `CAPEX` is a valid `Project.PROJECT_TYPE_CHOICES` value with **zero**
   `BOQItemMaster` rows. A type-sourced list hands a CAPEX project an *empty* dropdown
   where today it gets four options — a regression that a Residential-only check will not
   catch. Decide the fallback (the hardcoded four is the safe answer) before building.
4. Do **not** edit `DCLineItem.CATEGORY_CHOICES` or migrate it. Existing rows carry the
   four values and must keep them; the fix belongs at the form/view layer.

**Verified precondition, so the next session need not re-derive it:** the Residential
catalogue's derived categories are exactly `['Solar Modules', 'Structure', 'Inverter',
'BOS']`, in that order — byte-identical to the current hardcoded constant. Residential's
rendered options will not change.

---

## 3. There is no way to create a warehouse through the product

`StockLocation` has no view, no form, and no Django admin registration. The three rows
Horizon runs are entered by shell today, by explicit decision this session.

This is a **rollout blocker, not a defect**. The model's own section note is emphatic that
the count must not be structural — warehouses are rows, added through a screen, so a
fourth warehouse is a form submission and not a deploy. Right now a fourth warehouse is a
shell session, which is only marginally better than a code change.

**The precedent to follow is `Vendor`, not Django admin.** Master data in this system is
managed by custom portal screens: `vendor_list` / `vendor_add` / `vendor_edit` /
`vendor_toggle_status` (urls.py), and the two `BOQItemMaster` catalogue screens. Django
admin is registered only for verification and its own header says these "are not
user-facing screens"; neither `Vendor` nor `BOQItemMaster` is registered there at all.

Build the `vendor_list`-shaped screen — list, add, edit, toggle-active, **no delete** —
before rollout. Deactivation is the only retirement this model has, and
`tests_capability_flags.test_deactivation_is_the_only_retirement_there_is` enforces that.

---

## 4. `issued_from_warehouse` is optional everywhere, including OPEX

The FK is nullable and the form does not require it, deliberately: existing challans have
no honest value, and there is no warehouse entry screen to send someone to when the
dropdown is empty (see 3).

Making it **mandatory on OPEX** is a one-line change in `create_delivery_challan` — a
guard beside the existing `if not dc_number or not dc_date_s:` check, testing
`project.project_type == 'OPEX' and not warehouse`, re-rendering through `_form_context()`
like every other validation failure. The model FK stays nullable either way; the rule is a
form rule, not a schema rule, because the pre-existing rows must stay valid.

**Do it after 3, not before.** A required field with an empty dropdown is an unsubmittable
form.

---

## 5. `project_overview` passes `dc_category_choices` to nothing

`views.project_overview` puts `'dc_category_choices': DCLineItem.CATEGORY_CHOICES` in its
context. No template reads it — grep finds zero references. Dead context, harmless, left
in place because removing it is not this session's business. Worth deleting alongside the
T4 work, which is the only thing that will otherwise make someone wonder whether it feeds
a category list somewhere.

---

## 6. The local `.env` points at PRODUCTION Supabase, and storage has no master switch

Found while seeding the SCM pilot (`seed_scm_pilot`, 6 Sep 2026), on a local database that
is a restored production dump.

`SUPABASE_URL` in the local `.env` is `https://wqpxtjelsfgwhhyyafus.supabase.co` — the
same project that hosts all 18 stored `file_url` values in the dump. It is not a staging
bucket that resembles production; it **is** production storage, reachable from a developer
laptop with a live `SUPABASE_KEY`.

**Why this is not the same as the notification hazard, and is worse.** Notifications have a
master switch: `SystemSettings.whatsapp_enabled` and `email_enabled` are both `False` on
this restore, so the live Interakt and ZeptoMail keys sitting in the same file cannot
actually deliver anything. There is **no equivalent flag for storage**. `upload_design_file`
and `supabase_storage` consult nothing before writing; any local action that uploads —
`design_artifact_upload`, `design_survey_upload`, task file attachments — puts a real
object into the real bucket, silently and immediately.

The concrete consequence, already paid: the SCM pilot's design walk had to be cut short.
Reaching `released` requires a CAD upload, so the walkthrough site is walked only to the
Arka gate pair and the rest is fixtured — recorded as `PARTIAL` in the pilot manifest
rather than `WALKED`. That is a real loss of coverage caused by a configuration, not by
the product.

**What a session picking this up has to do**, in order of how much it buys:

1. Give local development its own Supabase project, or at minimum its own bucket, and put
   that in the developer `.env`. This is the actual fix and everything else is mitigation.
2. Failing that, add a settings-level storage kill switch that `design_storage` and
   `supabase_storage` both consult — one that raises rather than silently no-ops, so a
   developer learns immediately instead of wondering why a file vanished.
3. Either way, a check equivalent to `_demo_support.require_local_database()` but for
   storage: refuse a write when the database is local and the bucket is not.

Note that `DEBUG=True` is also set locally, so a stray production connection would be
serving tracebacks. `.env` is correctly gitignored and untracked; the exposure is the
credential's reach, not its disclosure.

---

## 7. `TESTTENDER26` is test data living in the production database

`Program` id 1 — `short_tender_code='TESTTENDER26'`, name `HRP-Test`, client
`Test-HRP-Client`, created 28 Jul 2026 by user id 10 — carries 11 OPEX sites
(`TESTTENDER26-MB001` .. `MB011`), one of them soft-deleted. All of it is present in
`railway_backup.dump`, which means it was created **on production, through the browser**,
not locally by an audit.

Left alone deliberately. It is out of scope for the pilot, and deleting a Program on
production is not a thing to do as a side effect of seeding a local rehearsal.

What a session picking it up needs to know: `Project.program` is `on_delete=PROTECT`, so
the sites must go first, and `short_tender_code` uniqueness is checked **soft-delete-aware**
against the unfiltered manager — a soft-deleted `TESTTENDER26` keeps reserving its code
and keeps its sites' `project_id` values reserved with it. Decide hard vs soft deletion
before starting, not halfway through.

Related but distinct: the 12 `MS0xx` sites under `MPUVL` looked like test data to a
pattern match and are **not** — they are ordinary MPUVNL tender sites alongside the `MB0xx`
ones. Do not sweep on the prefix.

---

## 8. One account has `UserProfile.is_active=True` and `auth.User.is_active=False`

`pradeep` (Site Engineer, email `pushkar@horizonrenewablepower.com`). The account **cannot
log in**, while every screen that reads the profile shows it as active. It is the only such
row in the database; the inverse case (profile inactive, user active) is zero rows.

Not touched — it is a dump-originated row and correcting it is a production data decision,
not a seeding one. Whoever owns it needs to decide which of the two columns is the truth
and set the other to match.

`seed_scm_pilot._assert_loginable()` exists because of this row: every pilot user has both
flags asserted `True` at creation and printed per user, so the pilot cannot inherit the
same failure and discover it mid-rehearsal.

---

## 9. The delivery mirrors have a hook but no RECONCILE

Found while wiring `sync_delivery_mirrors()`. Not a defect in that function — a gap
beside it, and the same gap the Design mirror already solved.

The Design mirror has **two** entry points and needs both:

    apply_design_status()          the HOOK      — a source status just moved
    utils.attach_opex_template()   the RECONCILE — the task rows just appeared, and the
                                                   source had a head start

The delivery mirrors have only hooks — `recalculate_dc_status()` and
`create_delivery_challan()`. Both fire on a delivery EVENT. Neither fires when the task
rows themselves appear.

**The consequence is visible on the pilot data right now.** `SCMPILOT01` carries two
challans with Solar Modules, Inverter and Structure lines all confirmed in full, and its
four mirror rows are all still `Not Started`. Running the derivation by hand moves three
of them to `Done` immediately:

    SCMPILOT01   DELIVERY_SOLAR_PANELS    Not Started -> Done
    SCMPILOT01   DELIVERY_INVERTERS       Not Started -> Done
    SCMPILOT01   DELIVERY_MMS             Not Started -> Done

(`DELIVERY_BOS_KIT` correctly stays `Not Started` — those challans carry no BOS line.)

Nothing will move them until somebody raises another challan on that site or re-saves a
GRN. Any OPEX site activated AFTER its material was delivered lands in the same state,
which is the exact shape of the problem audit A-2.3 §3.4 measured for design: the source
event happens before the rows exist, so a hook alone is correct for no site that already
has history.

**Left unfixed deliberately.** The approved call-site set for this session was the
minimum that covers every path *forward*, and it does. A reconcile is a third call site
in `utils.attach_opex_template()`, which was outside the session's MODE, and it is a
different decision from the hook: it needs someone to choose whether newly activated
sites should silently inherit `Done` buckets from material delivered months earlier, or
whether that should be a visible backfill somebody runs and reads.

**What a session picking it up needs to know.** The function is already safe to call
this way — it derives from the live line set rather than from an event, takes only a
project, is idempotent, and returns without writing or raising for a non-OPEX project or
one with no mirrors. So the fix is one call plus the decision above, and
`sync_delivery_mirrors()` needs no change to support it. Adding that call means adding
`attach_opex_template` to the closed caller list in
`tests_design_mirror_derivation.test_01b`, which is the intended way to make it a
deliberate act rather than a silent one.

A one-off backfill for the six seeded sites is the same call in a loop, and is worth
running before the pilot rehearsal so `SCMPILOT01` demonstrates the feature rather than
demonstrating this gap.

---

## 10. Nothing is the authority on whether a design package is complete

**Five signals, no reconciliation.** "Is this site's BOQ finished" and "is this design
package finished" are each answered by several rows that were written independently, and
no code compares them. The disagreement is not hypothetical — it is on screen right now on
the pilot data, and it is visible to two different roles at the same moment.

**Reproduce it, as the Design Head and as SCM:**

    /design/SCMPILOT03/qc/         green badge: "BOQ: Complete"
                                   "BOQ marked complete 20 Aug 2026 22:26"
    /projects/SCMPILOT03/boq/      "BOQ not yet created by the Design team."

Both screens read real data and neither is wrong about what it reads. The design module
reads `DesignAttempt.boq_submitted_at`; the BOQ page reads the `BOQ` row, which did not
exist. Since `fixture_scmpilot_boq` ran, the second screen renders a sheet — the fixture
changed the DATA, not the mechanism. The two signals are still unconnected, and the next
site to reach this state will disagree the same way.

**The five signals, and what each actually means:**

| # | signal | written by | means |
|---|---|---|---|
| 1 | `BOQ.status` | Residential submit/acknowledge workflow | where a *Residential* BOQ is in its approval loop. Permanently `Draft` on OPEX — the picker never touches it |
| 2 | `DesignAttempt.boq_submitted_at` | `design_boq_complete()` | the designer ticked "BOQ complete" on THIS attempt |
| 3 | group lock (`project_boq_is_group_locked()`) | `site_group_lock()` | SCM committed these quantities to a purchase |
| 4 | `DesignAssignment.status == released` | `design_head_qc_pass()` | both review gates passed |
| 5 | a `BOQ` row with items | the OPEX picker / upload | somebody actually entered quantities |

The first three were named as unreconciled by the 23 Aug audit. This session found the
fourth and fifth by walking the pilot data.

**The fourth signal deserves naming on its own.** That same QC screen reads
**"CAD: Pending"** on a site the Design Head has already RELEASED. Release is supposed to
be unreachable without a current `cad_zip` — `_package_is_complete()` requires one — yet
all six SCMPILOT sites carry zero `DesignFile` rows and five of them are `released`.
Either the release is not real or the requirement is not enforced on every route into
`released`; the seed reached it by writing the status directly, which is a sixth way to
produce the state and the one nothing guards. A screen that says a released package is
missing its drawings is telling the truth about a state the product should not be able to
hold.

**And the group lock itself freezes nothing when there is nothing to freeze.**
`site_group_lock()` refuses exactly two things — an empty group, and a member with a
pending change request. It does NOT check that any member has a BOQ. A group of sites with
no `BOQItem` rows locks successfully, reports success, and freezes nothing:

    group 40 "demo rehearse group"  status=locked  members=[SCMPILOT01, SCMPILOT02]
    aggregate: {lines: 0, unlinked: 0, item_count: 0, site_count: 2}

Note the last two keys. **`site_count` comes from `len(member_ids)`; `item_count` comes
from the data.** They are derived from different sources and nothing reconciles them
either, so the screen states "2 sites" over an empty table with equal confidence. That
lock is irreversible by design — there is no unlock — so group 40 is now permanently
locked over two sites whose quantities were never entered, and the only recorded trace is
an ActivityLog line per site saying "BOQ locked".

**Why this is one finding and not five.** Each row above is individually defensible; the
problem is that there is no single predicate anybody can call to ask "is this package
finished", so every screen answers it from whichever row is nearest to hand. Fixing any
one signal in isolation makes the set MORE inconsistent, not less. A session picking this
up should start by deciding which signal is authoritative and making the other four derive
from it — or state plainly that they measure something narrower — not by adding a sixth.

**Not fixed here, deliberately.** This session's MODE was a data fixture; changing what
any of these screens reads is a product decision with a blast radius across the design
module, the BOQ pages and the SCM handoff.

---

## 11. A released OPEX site CAN be reopened — through a draft procurement group

**Undocumented, reachable, and nobody wrote it down.** The design module reads as though
`released` is terminal: there is no revise button, no reopen control, and
`design_change_request()` refuses a released site outright. That refusal has an exception,
and the exception is easy to reach by accident.

**The route, measured end to end rather than reasoned about:**

    SCM    POST /programs/<tender>/site-groups/create/    (adds the released site)
    PM     POST /design/<site>/change-request/raise/      (accepted — see below)
    HEAD   POST /design/change-request/<pk>/accept/       (opens attempt N+1)
    DESIGN POST /projects/<site>/boq/entry/               (the BOQ is editable again)

**Why it works.** `design_change_request()` widens its own status gate for a site sitting
in a DRAFT procurement group:

    allowed_statuses = (CHANGE_REQUEST_STATUSES + (DESIGN_RELEASED,)
                        if in_draft_group else CHANGE_REQUEST_STATUSES)

This is Part 6 §4 as specified and the function's docstring explains it — a draft-group
member is `released` by construction, so admitting the branch at all means admitting a
released site. What the docstring does not say is the second half: acceptance calls
`_open_next_attempt(assignment, ..., redo=None)`, and `redo=None` means
`_carry_forward_artifacts()` is **never called**. So attempt N+1 starts with
`boq_submitted_at` unset, `project_boq_is_design_locked()` goes false, and the BOQ picker
opens. The reopen is a side effect of the carry-forward scoping rule, not a decision
anybody made about reopening.

**The state it leaves behind** is the part worth knowing before somebody triggers it on
real tender data:

* the site LEAVES the group on RAISE, before the Head has ruled (deliberate — SCM must not
  aggregate a site under dispute)
* `DesignAssignment.status` goes `released` -> `in_design`, and `released_at` /
  `released_by` are **not cleared** — so the row now says "in design" while still carrying
  the stamps of a release that has been undone. The post-QC pool ages sites off
  `released_at`, so a reopened site returns to the pool wearing its original age.
* attempt N is closed with both verdicts left `pending` forever (deliberate — the PM
  caused the rework, not the designer)
* the Arka does not carry forward, so the designer resubmits one and BOTH gates re-approve
  an Arka nobody disputed

**It is one-way on the pilot data, and that is an accident of the seed.** Getting back to
`released` needs `_package_is_complete()`, which needs a current `cad_zip` — and
`seed_scm_pilot` created no `DesignFile` rows at all (see §10). So on SCMPILOT03/04/05 this
route can UNDO a release and cannot redo one without a fabricated CAD archive in the live
production Supabase bucket (§6). That is the specific reason `fixture_scmpilot_boq` exists
instead of a walkthrough: the route is real, it was tested in a rolled-back transaction,
and it was rejected for what it costs rather than assumed to be closed.

**What a session picking this up has to decide.** Not whether to close the route — it is
specified behaviour and settled decision 6 depends on it. The open questions are narrower:
whether `released_at` / `released_by` should be cleared when a release is undone, whether
the reopen should be visible as such anywhere (today it is inferable only from
`DesignAttempt.opened_reason == pm_change_request`), and whether SCM adding a released site
to a draft group should say out loud that it has just made that site change-requestable.
The last one is the surprise: SCM's action, taken for procurement reasons, is what unlocks
a PM's ability to reopen someone else's finished design.

---

## 12. `OPEX` is the stored value; "RESCO" is the label

**Deliberate, not a half-finished rename.** `OPEX` remains the stored string in
`Project.project_type`, `Program.program_type` and 207 `BOQItemMaster` rows. Every
user-facing surface says **RESCO**. Django choices are `(value, label)` pairs, so the
change was one label per tuple in `Project.PROJECT_TYPE_CHOICES` and
`Program.PROGRAM_TYPE_CHOICES`, plus the prose that spelled the word out by hand.

**Why the value did not move.** `'OPEX'` is not decoration, it is a key:

* `TaskDurationTemplate` / `TaskTemplate` are keyed by `(project_type, phase_name,
  task_name)` — `attach_opex_template()` finds the 23-task, 8-mirror template by it
* `ChecklistTaskLink.unique_together` is `('task_name', 'project_type')`
* `get_opex_boq_catalogue()` and `get_opex_mandatory_items()` filter
  `BOQItemMaster.project_type='OPEX'` — 207 rows and codes `OPX-001` / `OPX-027`
* `sync_delivery_mirrors()` and every `design_views` entry point guard on
  `project.project_type != 'OPEX'`
* eleven migrations already contain the literal, and production carries real MPUVNL data
  typed `OPEX`

Renaming the value is a data migration across five fields plus a rewrite of every
comparison, filter and dictionary key. This change deliberately avoids that: the migration
it generates (`0083`) is five `AlterField` operations, state-only, and changes no row.

**Where the split is visible, and why each side is correct.**

Users see RESCO in: every `project_type` / `program_type` dropdown (`<option value="OPEX">
RESCO</option>`), the project detail, project list, program list and program detail badges,
the BOQ catalogue screens, the EOD digest and CEO report footnotes, form help text,
validation errors and `Http404` messages, and the Django admin's tender fieldset.

The word OPEX survives, correctly, in:

* every `== 'OPEX'` comparison, queryset filter and dictionary key, including the four in
  templates (`boq_items.html`, `program_detail.html`) and the one in JS
  (`program_form.html`, which reads the `<option value>`)
* every code comment and `{% comment %}` block — 48 of them describe the stored value
* every function, module, template filename, URL name and test — `attach_opex_template`,
  `opex_boq_entry.html`, `_opex_site`, `tests_opex_template.py`
* the `opex_sites_<code>.xlsx` export filename
* management command stdout and the seed scripts, which are developer-facing.
  `seed_scm_handoff_data.py:319` writes `Created OPEX Program:` into an `ActivityLog` row —
  demo data only, but it is the one place a seed script's wording reaches a user's screen
* `TaskDurationTemplate.PROJECT_TYPE_CHOICES`, a separate lowercase list
  (`[('residential', 'Residential')]`) with no OPEX member at all

**ActivityLog rows written before this change still read OPEX.** Nothing rewrites history,
so the audit log reads `Created OPEX site …` for everything up to now and `Created RESCO
site …` after. That is a consequence of leaving the data alone, not an oversight.

**What a session picking up the full rename has to do.** Not decide whether RESCO is the
right word — that is settled. The work is: a `RunPython` data migration over
`Project.project_type`, `Program.program_type`, `BOQItemMaster.project_type`,
`TaskTemplate.project_type` and `ChecklistTaskLink.project_type`; a sweep of every
comparison and filter listed above; the eleven historical migrations, which must keep
`OPEX` because they describe the state at the time they ran; and a decision about whether
existing `ActivityLog` prose is rewritten or left as the record of what the system called
it then. Until that happens, the rule is one line: **users see RESCO, the code says OPEX.**

---

## 13. A System Admin is granted the daily report but has no way to navigate to it

Found 9 Sep 2026 while verifying that `'System Admin'` had reached all three places the
CEO daily report gates on. It had — `USER_STATUS_REPORT_ROLES`
(`permissions.py:1039`), the `@role_required` list (`report_views.py:61`) and the
`base.html` nav condition (`base.html:64`) all read `'CEO', 'Admin', 'System Admin'`, and
the page returns 200 for that role. **Nothing was changed.** The grant is real.

**But the link is in a shell that role never sees.** `ROLE_DASHBOARD['System Admin']` is
`/sub-admin/projects/` (`decorators.py:24`), and every sub-admin screen extends
`projects/subadmin/subadmin_base.html` — a Tailwind shell whose nav offers exactly three
links: Projects, Task Durations, Departments. The Daily Report link lives in `base.html`,
the Bootstrap shell, which a System Admin only ever renders **by arriving at the report
itself**. Verified: `GET /sub-admin/projects/` as a System Admin contains neither
`/reports/user-status/` nor the string `Daily Report`.

So a System Admin can reach the report only by typing the URL, and once there sees the
link they could not previously find. R-11 says a screen ships with its navigation entry;
for two of the three admitted roles it did, for the third it did not.

**Not fixed here** because the prompt granting the role explicitly fenced off changes to
any other nav entry, and `subadmin_base.html` is a different design system (Tailwind +
Alpine + Lucide) from the one the link is currently written in. Doing it properly means a
Lucide-icon link in that shell's sidebar, not a copy-paste of the Bootstrap markup.

**Risk if left:** the role has access nobody told it about. The likely outcome is not a
security problem but a silent one — a System Admin never opens the report, and the grant
reads as done because all three code sites say so.

## 14. `role_required` denies with a completely empty 403 body

The 403 status is deliberate and decision-logged (28 Aug, prompt 0.2 — one authorisation
failure, one response, so a probe cannot tell "wrong role" from "not yours"). **That part
is settled and should not be reverted.** This entry is about the body, not the status.

`role_required()` returns bare `HttpResponseForbidden()` at `decorators.py:150` and `:152`
— no content, no template. There is no `handler403` anywhere in `solarpms/` or
`projects/urls.py` and no `403.html` template in the repo, and a `HttpResponseForbidden`
*returned* from a view does not pass through Django's `permission_denied` handler anyway
(only a raised `PermissionDenied` does). Measured 9 Sep: a Finance user and an SCM user
hitting `/reports/user-status/` each receive **HTTP 403 with a 0-byte body** — a blank
white page, no explanation, no way back.

This is repo-wide behaviour affecting **every** `@role_required` view, not a property of
the report. It replaced the previous redirect-with-flash-message, which was chatty but
told the user what had happened.

**Not fixed** (R-12 — a report-access verification is the wrong session to change the
denial surface of every gated view in the product). **The fix is small and belongs in its
own prompt:** a `403.html` extending `base.html` plus `handler403`, and switching the
decorator to `raise PermissionDenied` so the handler fires. Worth pairing with the same
treatment for 404.

**Risk if left:** a user denied a page sees nothing at all and reports it as "the site is
broken". Support cost, not a security one — the denial itself is correct.

## 15. Programme and site fields reshaped for phase 1 — hidden, not removed

Two decisions from the same session, recorded together because they share one rationale.

### 15a. Eleven programme fields and `contract_value` were HIDDEN, not dropped

**Hidden from the programme create and edit forms** (`ProgramForm.Meta.fields` and
`program_form.html` together — see below for why both):

| Field | Group it sat in |
|---|---|
| `expected_completion_date` | Program Details |
| `tender_reference_number` | RESCO Tender Details |
| `bid_value` | RESCO Tender Details |
| `award_date` | RESCO Tender Details |
| `ppa_reference` | RESCO Tender Details |
| `ppa_signed_date` | RESCO Tender Details |
| `ppa_per_unit_rate` | RESCO Tender Details |
| `ppa_escalation_percentage` | RESCO Tender Details |
| `ppa_escalation_frequency` | RESCO Tender Details |
| `financing_partner_name` | CAPEX Financing |
| `financing_assistance_type` | CAPEX Financing |

The last two were not on the original hide list by name; they were included as the
"anything below the fold" the brief allowed for, and confirmed. The seven that remain are
program type, status, short tender code, name, client name, total planned capacity
(`total_capacity`, in MW) and planned site count.

Two renders also went, because they displayed hidden fields: the **"Expected completion"
stat card** on `program_detail.html`, and that page's `reference_display` suffix, which
resolves to `tender_reference_number` for a RESCO tender — replaced with
`short_tender_code`, which is a kept field and identifies the programme just as well.

**Also hidden: `Project.contract_value`**, on the post-activation edit modal
(`PostActivationFieldEditForm`) *and* on the overview header pill
(`_project_editable_fields.html`). Commercial, out of phase 1. A pill on the header is
more visible than a field inside a modal, so hiding one without the other would have been
a louder version of the thing being hidden.

**EVERY COLUMN REMAINS.** Migration 0084 contains no `RemoveField` and no `RunPython`.
All eleven programme fields are empty on every row on both databases today, so nothing was
stranded; `contract_value` is NOT empty and is untouched — it still feeds the Finance and
CEO dashboard totals, still gates the M1/M2/M3 payment-milestone amounts, and is still
editable while a project is Draft through `ProjectEditForm` / `project_form.html`.

**Why hidden rather than removed.** Dropping a column means finding every read, owning a
rollback story, and writing a data migration for values that may arrive later. Hiding costs
one line per field in a `Meta.fields` list. Removal stays available at any time and is a
separate, deliberate decision — this entry is what makes it an informed one.

**THE MECHANISM MATTERS — DO NOT "TIDY" IT INTO A TEMPLATE-ONLY HIDE.** Each field came
off `Meta.fields` *and* out of the template. Removing only the markup would leave the field
ON the form, and a `ModelForm` field absent from the POST body cleans to `None`/`''` and is
written back by `construct_instance` — so a template-only hide would silently wipe these
columns on every save. Harmless today because they are empty; not harmless the day someone
backfills them.

**Two pieces of dormant code were left standing on purpose:**

- `ProgramForm.clean()` still holds the soft-delete-aware uniqueness check for
  `tender_reference_number`. `cleaned.get()` now returns `None`, so it short-circuits at a
  cost of one dict lookup. It is the ONLY uniqueness rule for that column and must return
  *with* the field, not after it.
- `PostActivationFieldEditForm.clean_contract_value` was the opposite call and was
  **removed**, because its body was a guard against a change the form can no longer make.
  Its rule — contract value may not change once payment-milestone amounts exist, because
  `set_milestone_amounts` validated M1+M2+M3 == contract_value and nothing reconciles them
  — is recorded in that form's docstring and in `projects/tests.py`. **If `contract_value`
  ever returns to that form, the check returns with it.**

**To restore any hidden field:** add its name back to the form's `Meta.fields`, its widget
to `Meta.widgets`, and a cell to the template. Nothing else is required — no migration, no
backfill.

**Risk if left:** none to data. The risk is institutional — in six months someone finds
eleven empty columns with no form behind them and cannot tell whether that is deliberate.
This entry is the answer: it is.

### 15b. `ac_capacity_kw` is blank on every site and needs backfilling

`Project.capacity_kw` was renamed to `dc_capacity_kw` by `RenameField` in migration 0084.
`RenameField` is an `ALTER TABLE ... RENAME COLUMN` — the column and its contents survive
intact — so **every stored capacity was preserved and is now READ AS DC**. Verified on the
local database: 139 non-null values before, the same 139 after, identical row by row.

**`ac_capacity_kw` is null on all 148 projects and no migration guessed at it.** There was
only ever one capacity figure, and inferring AC from DC needs an inverter-loading ratio
this system does not hold. **The team must backfill it.** Until they do:

- the overview header shows a DC pill and no AC pill (each is `{% if %}`-guarded);
- the bulk-upload template carries an `AC Capacity (kWp)` column for new sites;
- `OpexSiteForm` and the post-activation modal both accept it, so existing sites can be
  corrected one at a time without an upload.

The Zoho webhook writes its single capacity figure to `dc_capacity_kw` for the same reason
— Zoho has no AC/DC distinction — so webhook-created projects also land with AC null.

**One column ordering note for whoever does the backfill:** the upload parser matches on
header text, and `Capacity (kW)` was renamed to `DC Capacity (kWp)` in the template. The
old spelling is still mapped, via `_BULK_LEGACY_HEADERS` in `views.py`, so half-filled
sheets already in circulation still import their capacity into DC rather than having it
silently dropped into the ignored-extra-headers warning. **Retiring a header is additive
here — map the old spelling, never just replace it.**

### 15c. Two stale comments in the design module were left alone

`design_metrics.py:26` still reads `Project.capacity_kw is KILOWATTS`, and
`models.py:61` still says `site_code` is "Combined with `Program.short_tender_code` to
compose project_id" — a scheme dropped when `site_code` became the whole project ID.
Neither is executable and neither was touched, because the session's MODE fenced off the
design module. Both are worth a one-line fix in any session that legitimately opens those
files.

---

## 16. An unknown design status fails three different ways, and only one is tested

Found 10 Sep 2026 by the design approval audit (`docs/DESIGN_APPROVAL_AUDIT.md` §1.3, §7.2).

Three readers take `DesignAssignment.status` and have to cope with a value they do not know.
Each does something different:

- `design_views.derive_design_mirror_state()` **raises** `ValueError`. It runs inside the
  caller's atomic block and is unguarded by design, so on an activated site the whole write
  rolls back.
- `design_metrics._classify()` **silently** returns `'in_design'` — its last line is
  `return 'in_design'` — so the site is reported as "In design, awaiting Arka" on both
  tender dashboards.
- `designer_dashboard_context()` calls `_DESIGNER_ACTIONS.get(assignment.status, ('none', '', ''))`,
  which silently offers the designer nothing.

Only the first is guarded by a test: `tests_design_mirror_derivation.test_01` compares the
mirror map to `DESIGN_ASSIGNMENT_STATUS_CHOICES`. `STAGE_ORDER` and `_DESIGNER_ACTIONS`
have no such check.

A fourth reader depends on position rather than on the value. `tender_dashboard.html`
takes the released tile as `{% with released=m.stages|last %}`, which is right only while
`'released'` is the last entry of `STAGE_ORDER`.

**Not fixed** — audit session, `.py` and templates fenced off. **Risk if left:** the next
status added to the choices shows up correctly on the mirror and wrongly on the dashboards,
with nothing failing.

## 17. The tender dashboard's `rework` counts PM change requests against the designer

Found 10 Sep 2026 by the design approval audit (§4.4).

`design_metrics.designer_workload()` computes:

    designer_attempts = row['attempts'] - row['input_problem_attempts']
    row['rework'] = (round(designer_attempts / released, 1) if released else None)

`row['attempts']` counts every attempt, and only Group B/C attempts are subtracted. An
attempt opened by an accepted PM change request is therefore in the designer's `rework`
figure. The function's own docstring says the opposite: *"rework — attempts caused by a
GROUP A failure — the design was wrong. This is the designer's number"*.

`design_analytics.m_rework_multiplier()` excludes `CAUSE_PM_CHANGE` and says on screen that
the two figures differ. `design_analytics.m_first_pass_rate()` tests
`current_attempt_number == 1`, so a site reopened by a change request stops counting as
first-pass for its designer however clean the design was.

This is dormant on live data, which has zero `DesignChangeRequest` rows and ten `initial`
attempts. It starts to matter the moment any rework loop other than a QC failure goes
through `_open_next_attempt()`.

**Not fixed** — audit session. **Risk if left:** the Head coaches a designer over rework
the PM caused, which is exactly what the separate `opened_reason` values exist to prevent.

## 18. The PM dashboard never offers the change request a released draft-group site accepts

Found 10 Sep 2026 by the design approval audit (§0 P4, §1.3 row 11).

`design_change_request()` and `design_change_request_form()` both widen the change window to
`released` for a site in a draft procurement group (see `docs/EXECUTION_MODULE_DEFERRED.md`
B1). A third gate, `design_views.pm_change_request_targets()`, decides whether the PM
dashboard shows the button at all, and it has no widening:

    if assignment is None or assignment.status == DESIGN_RELEASED:
        continue

So §11's reopen route is reachable only by typing the form's URL. Nothing on any dashboard
leads to it.

**Not fixed** — audit session. **Risk if left:** there are now three spellings of one
window, and the only one a PM ever sees is the one that disagrees. B1's "one shared helper"
fix needs to cover this function too, or it will leave the dashboard behind.

## 19. The Design mirror hook and the design ledger are built; five documents say they are not

Found 10 Sep 2026 by the design approval audit (§2.2, §6.1, §7.1).

`apply_design_status()` calls both `record_transition()` and `sync_design_mirror()`.
`DesignAssignment` has been a registered ledger subject since migration
`0079_design_assignment_subject_type`. The local dump holds 5 `design_assignment` ledger
rows, and 6 activated sites carry a Design mirror that the hook writes.

These still describe the design mirror or the design ledger as unbuilt:

- `docs/execution-model.md` — §5 ("The derivation hooks that will *write* mirror statuses do
  not exist"), R-21, and §13's "NOT instrumented" row for `DesignAssignment`
- `docs/PHASE_0_COMPLETION.md` — "The design module's transitions"
- `docs/EXECUTION_PROMPT_LOG.md` — the "derivation hooks" paragraph and row B21
- `docs/OPEX_task_template_spec.md` — §2 rules 3–4, §4 and §6 ("NOT BUILT for any of the 8,
  Design included")
- `docs/EXECUTION_MODULE_DEFERRED.md` — B27 ("none of their six mirrors can move")

Separately, `docs/PHASE_2_PREFLIGHT_AUDIT.md` §6.1 says `design_mark_blocked` has no
`released` guard. It has one now.

**Not fixed** — the session could edit only its own audit file and this one (R-12). **Risk
if left:** the next prompt author reads §13, plans a session to instrument the design
ledger, and discovers mid-build that it already exists.

## 20. The change-request screen and its view disagree about "review in progress"

Found 10 Sep 2026 by the design approval audit (§1.4, §7.2).

`design_change_request()` sets
`was_in_qc = assignment.status in (DESIGN_IN_QC, DESIGN_AWAITING_HEAD_QC)` and warns the PM,
on raise, that either gate's review is suspended. The form template warns only for
`{% if assignment.status == 'in_qc' %}`. A PM raising against a package at
`awaiting_head_qc` is not told, before submitting, that they are about to stop the Design
Head's review.

`design_change_request_accept()` has a related gap. It checks only that the request is still
pending, never the site's status, and relies entirely on the status gate applied when the
request was raised.

**Not fixed** — audit session. **Risk if left:** low; the success message after the raise
does say it. It becomes relevant as soon as anything else can move a site while a request is
pending.

## 21. The Designer User Manual is not in the repository

Found 10 Sep 2026 by the design approval audit (§1.5).

The audit prompt said the manual documents thirteen stages against the model's fourteen and
asked which one is missing. The repo and its parent directory contain no manual: there is no
file named like one and no text matching "user manual", "designer manual" or "13 stages".
The question could not be answered.

The code does show that three values are never committed by any current writer —
`due_date_proposed` (no writer anywhere), `qc_failed` (write removed in Session C) and
`awaiting_survey` (the default, moved on inside the same transaction) — and that `allocated`
is reachable only for legacy rows.

**Not fixed** — nothing to fix in code. **Risk if left:** user-facing documentation lives
somewhere no code audit can check it against, and it will drift.

## 22. Not Applicable is excluded from metrics but not from the Gantt or the status path

Found 20 Sep 2026, building the Not Applicable flag.

Three surfaces were left alone deliberately, each because touching it was out of the
prompt's scope and none is wrong today:

**`compute_gantt_schedule()` has no applicability filter.** It reads every task on the
project and passes `task.status` through as a display string, so an N/A task still gets a
bar. `_gantt_grid.html` only special-cases `'Done'`, so the bar renders as ordinary open
work with its stored status in the tooltip. The metric readers all exclude N/A, so this is
the one screen where an N/A task looks live. **Risk if left:** low and cosmetic, but a PM
reading the Gantt sees a bar for work that no longer counts anywhere else.

**The status path still accepts an N/A task.** `_apply_task_status_change()` is untouched
by design, so the assignee of an N/A task can still move it Not Started -> In Progress ->
Done. Nothing breaks — the row is excluded from every metric either way, and the stored
status is exactly what un-marking restores — but "Done and not applicable" is a reachable
state that means nothing. Marking a *Done* task N/A is refused; completing an *N/A* task is
not. **Risk if left:** low. The two guards belong together and only one was in scope.

**`sync_delivery_mirrors()` / `sync_design_mirror()` are untouched**, correctly:
`user_can_mark_task_not_applicable()` refuses a mirror, so no mirror can carry the flag and
the derivations cannot encounter one. If a future prompt ever lets a mirror be marked N/A,
both sync paths must learn the flag first — they recompute status from the source and would
write straight past it.

**Not fixed** — out of scope for the build prompt, which named its readers exhaustively and
listed the status path and the design module as MUST NOT TOUCH.

## 23. O2 vendor orders — what the raise and order pages left for later

Found 22 Sep 2026, building O2 (Residential vendor order raise and order detail).

**An order with no payment cannot be found again.** The order page is reached from the
redirect after raising and from the "View order" link on each payment (overview card 4b,
payment detail). An order recorded without a first payment has no payment row, so once
the user leaves the page nothing links back to it. O2 was limited to one link per payment
in both templates. **Risk if left:** medium. SCM records an order, navigates away and cannot
reopen it until O2b adds a payment path or an order list.

**`raised_with_unfrozen_quantities` is always False on a Residential order.** Its
definition ("any site in no locked procurement group") would make EVERY Residential order
True, because Residential sites are never grouped. The raise view leaves the default, since
the flag is meant to mark a risk and a flag that is always True marks nothing. **Decide in O3**,
which is where it starts to mean something.

**Card 4b and the payment detail page still read the legacy fields.** The BOQ-item column,
Invoice # and the invoice-document link are blank on every O2-raised payment, which writes
the NOT NULL legacy columns as ''. Both screens also still show two statuses
(Pending/Confirmed) out of the five O1 introduced. O2 was allowed to add one link to each
screen and change nothing else. **O6 rewrites these readers** when it drops the fields.

**Pre-existing, seen in passing:** payment_request_detail's back button goes to My
Documents whatever the entry point, and its role allowlist leaves out CEO, although the new
order page admits CEO. confirm_payment_request writes no StatusTransition (O5's job).

**Not fixed**: out of scope (R-12).

## 24. O2b order payments, document append, order list — what was left

Found 22 Sep 2026, building O2b.

**#23's "order with no payment cannot be found again" is only partly closed.** The order
list (`vendor_order_list`) is linked from the SCM dashboard's Residential card ("Orders
(n)") and from each order page ("Back to orders"). Nothing links to it for a PM or for
Finance. Their natural entry point is project_overview card 4b, and O2b was barred from
changing 4b. **Risk if left:** low. They can read the list, but only by URL or through a
payment's "View order". **Fix in O6** when 4b is rewritten.

**Appending documents has no idempotency key.** VendorOrderDocument has no `client_uuid`,
and O2b could not add fields. A double-submit, or a retry after a slow upload, appends the
same files twice. The page disables its button on submit, and the records are append-only
anyway, so a duplicate costs clutter, not money. **Risk if left:** low. Add a key if an
offline or queued client ever posts here.

**A further payment is filed under the order's FIRST site** (`_order_project`). That is
exact for Residential, where an order has one site. A group-scoped order (O3) has several,
and PaymentRequest.project must name one. **O3 must decide** before
user_can_request_order_payment admits group orders. Today the predicate checks only role and
live sites, not project_type.

**The order list 404s on a deleted site.** An order placed for a site that was later
soft-deleted can still be read on its own page, but it no longer appears in any list.
**Risk if left:** low. Deleted sites are rare, and the order is still readable through its
payments.

**The client-side checks are not browser-verified.** The raise, add-payment and
add-documents pages block and explain problems before submit (missing PO/PI, an
incomplete invoice slot, a payment above the total or the available balance). The JS was
compiled into the templates, but no browser run exercised it. The server rules are
unchanged, so the worst case is a refusal after a reload, as in O2.

**Not fixed**: out of scope (R-12).
