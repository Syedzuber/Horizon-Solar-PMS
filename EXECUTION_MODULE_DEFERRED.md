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
