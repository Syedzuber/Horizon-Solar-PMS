# OPEX Design Module — Notification Specification (Part 7a)

**Investigation only. No code was changed by this session.**

| | |
|---|---|
| Local HEAD | `0ecf1ff` — *Part 6.5b: remove the 'Design Head' role choice, close the deputy BOQ gap* |
| Deployed SHA | `0ecf1ff7ba181696c166a111faf1925a79a4ab0e` — deployment `1b71e219`, SUCCESS |
| Match | Yes |
| Measurements from | **Local Postgres** (`solarpms_local`) + Railway environment variables. Production database NOT read. |

---

## 1. PREREQUISITE — the three known-broken call sites

**Short answer: the fix was applied and committed, and it made all three worse. All three are still broken, and each is now short by exactly one body variable.**

The fix landed in commit **`3ceffd9`** ("Add email channel to all 7 notification triggers; fix 3 WhatsApp param bugs", 27 Jun 2026 16:09 IST). Its stated change:

```
- Fix duplicate template_params: assign_project (4→3), boq_acknowledged (3→2),
  issue_resolved (5→4)
```

The premise — that the repeated `project.customer_name` was a duplicate — is **wrong**. Interakt's own error responses, stored in `NotificationLog.error_detail`, state the registered variable counts, and they contradict it.

### The evidence

`NotificationLog` on the local database holds 220 rows including every raw Interakt response. Three distinct failure modes are visible, in sequence:

**(a) Header/body not split** — 28 rows, 21-22 Jun, every template:
```
HTTP 400: "Missing variable values for template's header, expected number of values are 1"
```
Fixed by the `headerValues` / `bodyValues` split now at [notifications.py:151-152](projects/notifications.py#L151-L152).

**(b) Success check was `== 200`** — 6 rows, all at 22 Jun 06:31, logged `status='failed'` while carrying:
```
HTTP 201: {"result":true,"message":"Message queued for sending via Interakt...","id":"..."}
```
Fixed — [notifications.py:167](projects/notifications.py#L167) now reads `if 200 <= resp.status_code < 300:`.

**That 06:31 batch is the load-bearing evidence.** With the header split correct and the *original* parameter lists, **six of seven templates returned HTTP 201 — they were accepted by Interakt.** They were only recorded as failures because of bug (b).

**(c) Body count wrong** — the current state. Interakt states the expected body count explicitly:

```
issue_resolved   @ 2026-06-22 06:31  "Missing variable values for template's body, expected number of values are 4"
assign_project   @ 2026-06-28 03:14  "Missing variable values for template's body, expected number of values are 3"
```

The `assign_project` failure is dated **2026-06-28 03:14 — after the fix commit of 2026-06-27 16:09.** It is the fix's own output failing.

### Current state of each

| Template | Registered (per Interakt's error) | Code sends now | Verdict |
|---|---|---|---|
| `assign_project` | header 1 + body **3** = 4 | header 1 + body **2** = 3 | **STILL BROKEN**, short 1 body value. Proven by a post-fix 400. |
| `issue_resolved` | header 1 + body **4** = 5 | header 1 + body **3** = 4 | **STILL BROKEN**, short 1 body value. |
| `boq_acknowledged` | header 1 + body **2** = 3 *(inferred, see below)* | header 1 + body **1** = 2 | **ALMOST CERTAINLY BROKEN**, short 1 body value. Not directly proven — no send attempted since the fix. |

`boq_acknowledged` has no post-fix attempt in the log, so Interakt has never told us its count directly. Two independent sources say 2 body values: the pre-fix 3-param shape **returned HTTP 201** on 22 Jun 06:31, and `test_whatsapp.py:53` documents it as `# header (1) + body (2) = 3 total`.

### The three call sites, quoted

**`issue_resolved`** — [views.py:6752](projects/views.py#L6752), used at [views.py:6771-6772](projects/views.py#L6771):
```python
resolved_params = [project.customer_name, issue.title, resolver_name, issue_link_abs]
#                  ^header                 ^body[0]     ^body[1]       ^body[2]   -> only 3 body values, 4 required
```

**`boq_acknowledged`** — [views.py:4171-4175](projects/views.py#L4171):
```python
template='boq_acknowledged',
template_params=[
    boq.project.customer_name,   # [0] header
    scm_name,                    # [1] body[0] — scm_name
],                               # -> only 1 body value, 2 required
```

**`assign_project`** — two call sites, both the same shape.
[views.py:5839-5844](projects/views.py#L5839) (Zoho webhook):
```python
template='assign_project',
template_params=[
    project.customer_name,    # [0] header
    pm_display_name,          # [1] body[0] — user_name
    project_url_abs,          # [2] body[1] — project_url
],                            # -> only 2 body values, 3 required
```
[views.py:8303-8308](projects/views.py#L8303) (`admin_assign_pm`) is identical without the comments.

### Why this diagnosis holds

The three templates left untouched by `3ceffd9` all retain a repeated project name, and all three work:

| Template | Params in code | Repeats project name? | Evidence |
|---|---|---|---|
| `assign_task` | `[customer, recipient, task_name, customer, url]` = 1+4 | **yes** | **8 × `sent` on 1-2 Jul, after the fix** |
| `issue_created` | `[customer, recipient, customer, raiser]` = 1+3 | **yes** | HTTP 201 at 22 Jun 06:31 |
| `payment_notification` | `[customer, task_name, customer]` = 1+2 | **yes** | HTTP 201 at 22 Jun 06:31; `sent` 22 Jun 06:52 |
| `invoice_paid` | `[customer, boq_desc, inv_no, amount, vendor]` = 1+4 | no | HTTP 201; `sent` 22 Jun 06:52 |

The approved templates genuinely repeat the project name in the body — once in the header (as the WhatsApp header line) and again inside the message text. Removing it broke exactly the three it was removed from.

**Nothing in this session fixes any of it.** The user decides whether to correct these before 7b.

---

## 2. Infrastructure findings

### 2.1 Existing notification infrastructure

**WhatsApp send function** — `_send_whatsapp(recipient, template, template_params, base)`, [notifications.py:114](projects/notifications.py#L114). Private; never called directly.

**Email send function** — `_send_email(recipient, subject, message, base, html_message=None)`, [notifications.py:221](projects/notifications.py#L221). Delegates the HTTP call to `_zeptomail_post(to_email, to_name, subject, text_body, html_body=None)`, [notifications.py:180](projects/notifications.py#L180).

**The single public entry point** — [notifications.py:24-54](projects/notifications.py#L24-L54):
```python
def send_notification(
    recipient, message, channels=None, link='', subject='',
    template=None, template_params=None,
    related_project=None, actor=None, html_message=None,
)
```
Two other public functions exist: `send_raw_email(to_email, subject, body)` ([:251](projects/notifications.py#L251), **bypasses the master switch** — platform alerts only) and `send_aggregate_email(...)` ([:280](projects/notifications.py#L280), master switch only, no per-user preference).

**Template registration: there is none.** No map, no registry, no enum, no validation. `template` is a free-text string passed straight into the Interakt payload as `template.name`, and `template_params` is an unvalidated flat list:
```python
params = [str(p) for p in (template_params or [])]
payload = {..., 'template': {'name': template, 'languageCode': 'en',
                             'headerValues': params[:1], 'bodyValues': params[1:]}}
```
A wrong template name or a wrong parameter count is discoverable **only** from Interakt's HTTP 400 after the fact. This is the direct cause of §1 going unnoticed for five weeks.

**`NotificationLog` fields** — [models.py:1255-1302](projects/models.py#L1255-L1302): `recipient` (FK UserProfile, required), `channel`, `status`, `message`, `template_name`, `related_project`, `actor`, `error_detail`, `delivery_status`, `interakt_message_id`, `created_at`.

`status` values: **`sent` / `failed` / `skipped`** only. `delivery_status` is a separate, optional field for Interakt webhook callbacks: `message_api_sent` / `message_api_delivered` / `message_api_read` / `message_api_failed`.

**`SystemSettings.email_enabled`** is read in exactly two places: [notifications.py:63](projects/notifications.py#L63) (`send_notification`) and [notifications.py:314](projects/notifications.py#L314) (`send_aggregate_email`). `whatsapp_enabled` only at [notifications.py:62](projects/notifications.py#L62).

**`UserProfile.email_notifications`** is read at [notifications.py:94](projects/notifications.py#L94) only. `whatsapp_notifications` at [notifications.py:85](projects/notifications.py#L85) only. Both are also editable through the admin notification-preferences screen.

Gate order per channel, and it matters — a skip is logged, not silent:
```
master switch off  -> _log(..., 'skipped', 'Master switch off')      -> return
user preference off -> _log(..., 'skipped', 'User preference off')   -> return
no phone / no email -> _log(..., 'failed',  'Recipient has no ...')  -> return
```

### 2.2 Interakt integration specifics — all three claims confirmed

**Claim 1: header and body are separate API fields; `template_params[0]` is the header, `[1:]` are body.** TRUE — [notifications.py:138-153](projects/notifications.py#L138-L153):
```python
    # Interakt template API: header and body variables are separate fields.
    # Convention: pass template_params as one flat list — first element is the single
    # header variable value, remaining elements are body variable values (in registered order).
    # This matches all our approved templates which each have exactly 1 header variable.
    params = [str(p) for p in (template_params or [])]
    payload = {
        ...
        'template': {
            'name': template,
            'languageCode': 'en',
            'headerValues': params[:1],   # first element = the single header variable
            'bodyValues':   params[1:],   # remaining elements = body variables
        },
    }
```

**Claim 2: Interakt returns 201, so the check must be 2xx.** TRUE, and correctly implemented — [notifications.py:167](projects/notifications.py#L167):
```python
        if 200 <= resp.status_code < 300:
```
Confirmed against live responses: six `HTTP 201: {"result":true,"message":"Message queued for sending via Interakt..."}` rows exist in `NotificationLog`, all recorded `status='failed'` on 22 Jun 06:31 under the old `== 200` check. The same shape now logs `sent`.

**Claim 3: every approved template has exactly one header variable, always the project name.** TRUE as far as the evidence reaches. `headerValues=params[:1]` hard-codes exactly one, all 28 header errors said `expected number of values are 1`, and every call site passes `project.customer_name` (or `boq.project.customer_name`) first.

Note it is `customer_name`, **not** `project_id` and not `Program.name`. For an OPEX site, `customer_name` is documented as carrying a different meaning by `project_type` ([models.py:95](projects/models.py#L95)) — **see DECISION POINTS #3.**

**Finding not in the brief:** there is no validation that `template_params` matches the template. `_send_whatsapp` will happily post any length. A one-line length assertion per template would have caught all of §1 at the call site.

### 2.3 Every currently approved WhatsApp template

Seven, plus one test artifact. Body-variable meanings are inferred from the call sites and from `test_whatsapp.py`; **the authoritative registered text lives only in the Interakt console, which this session cannot read.**

| Template | Header | Body vars (registered count) | Call sites | State |
|---|---|---|---|---|
| `assign_task` | project | 4: recipient_name, task_name, project_name, task_url | [views.py:3657](projects/views.py#L3657), [3738](projects/views.py#L3738) | **WORKING** — 8 × `sent` post-fix |
| `issue_created` | project | 3: recipient_name, project_name, raiser_name | [views.py:6413](projects/views.py#L6413), [6509](projects/views.py#L6509), [6615](projects/views.py#L6615) | **WORKING** — 201 confirmed |
| `payment_notification` | project | 2: task_name, project_name | [views.py:3366](projects/views.py#L3366), [3560](projects/views.py#L3560) | **WORKING** — `sent` |
| `invoice_paid` | project | 4: boq_desc, invoice_no, amount, vendor_name | [views.py:5016](projects/views.py#L5016) | **WORKING** — `sent` |
| `issue_resolved` | project | **4** — code sends 3 | [views.py:6771](projects/views.py#L6771) | **BROKEN** |
| `assign_project` | project | **3** — code sends 2 | [views.py:5839](projects/views.py#L5839), [8303](projects/views.py#L8303) | **BROKEN** |
| `boq_acknowledged` | project | **2** — code sends 1 | [views.py:4171](projects/views.py#L4171) | **BROKEN (inferred)** |
| `test_template` | — | — | none (manual) | 1 × `sent`, 26 Jun |

`eod_digest` and `eod_digest_aggregate` are **NotificationLog labels only** — email-only, never sent to Interakt.

**No approved template is reusable for any of the six design events.** Every one is bound to a Residential concept (task, issue, payment, invoice, BOQ acknowledgement) with fixed body text. All six design events need new templates.

### 2.4 Email path

**`_send_email()` has NO per-template content map.** It sends whatever the caller passes:
```python
def _send_email(recipient, subject, message, base, html_message=None):
    email = (recipient.user.email or '').strip()
    if not email: ... return
    display_name = recipient.user.get_full_name() or recipient.user.username
    ok, detail = _zeptomail_post(email, display_name, subject, message, html_message)
```
Subject and body are composed at each call site. Every existing WhatsApp-and-email call site builds a plain-text body inline with a hard-coded `https://horizon-solar-pms-production.up.railway.app` link appended. Only the EOD digest uses Django templates (`projects/email/eod_digest.html` / `.txt`) with `html_message`.

**Consequence for 7b: email needs no Meta approval and no template registration. Any body text can ship the day it is written.**

**Is `email_enabled` True on production? I cannot check — the production database was not read this session, and `SystemSettings` is a database row, not an environment variable. The user must check.** On the **local** database both switches are OFF:
```
whatsapp_enabled: False
email_enabled: False
in_app_notifications_enabled: True
```
Local `NotificationLog` shows 38 WhatsApp and 21 email rows skipped with `Master switch off`, the most recent 23 Jul — so locally it has been off for a month.

**ZeptoMail environment variables on Railway** — read from the live service, values redacted:
```
ZEPTOMAIL_API_KEY      = <set, len 160>
ZEPTOMAIL_FROM_EMAIL   = noreply@horizonrenewablepower.com
INTERAKT_API_KEY       = <set, len 60>
INTERAKT_WEBHOOK_SECRET= <set, len 0>     <- EMPTY
```
Present and non-empty except the webhook secret. **Three further findings from the full Railway variable list:**

* **`ADMIN_DIGEST_EMAIL` and `HR_DIGEST_EMAIL` are NOT set on Railway.** They fall back to the `settings.py` defaults `REPLACE_WITH_ACTUAL_ADMIN_EMAIL` / `REPLACE_WITH_ACTUAL_HR_EMAIL`, which trip the guard at [send_eod_digest.py:411-416](projects/management/commands/send_eod_digest.py#L411-L416) and raise `CommandError` on **every** real cron run. The company-wide aggregate digest has therefore never sent on production. Individual digests are unaffected. (They *are* set in the local `.env` — `smzk07@gmail.com` / `shweta@horizonrenewablepower.com` — which is why this is invisible locally.)
* **`APP_BASE_URL` is NOT set on Railway**, so digest links fall back to `https://horizon-solar-pms-production.up.railway.app` while the live domain is `pms.horizonrenewablepower.in`.
* **`INTERAKT_WEBHOOK_SECRET` is empty**, so `NotificationLog.delivery_status` cannot be populated from verified callbacks. Every WhatsApp row will stay at `sent` with no delivery confirmation.

### 2.5 Recipient resolution

All resolutions below start from a `DesignAssignment` named `a`.

| Recipient | Resolution | Phone reliable? | Email reliable? |
|---|---|---|---|
| Assigned designer | `a.assigned_to` (FK `UserProfile`, **nullable** until allocation) | yes | yes |
| Design Head | `UserProfile.objects.filter(is_design_head=True, is_active=True)` — a **queryset**, not a single row; there is no unique constraint | yes | yes |
| Named deputy | `head.design_head_deputy` for each Head above. Authority helper is `permissions.user_is_design_head_deputy(user)`, which takes a `User`, not a profile — for *sending* you want the FK directly | n/a (none set) | n/a |
| Site's assigned PM | `a.project.assigned_pm`, **nullable**. Use `permissions.project_managers(a.project)` to include active coordinators — it returns `[]` when there is neither | yes | yes |
| SCM users | `UserProfile.objects.filter(role='SCM', is_active=True)` | yes | yes |

Measured on local data — `DesignAssignment` for `IPGCL26-MB002`:
```
a.assigned_to             -> shyam (Design)
a.project.assigned_pm     -> chetan (PM)
a.project.coordinators    -> []
a.project.assigned_design -> shyam (Design)
Design Head(s)            -> ['praveen']
deputies                  -> []            <- none set anywhere
SCM users                 -> ['subhash']
```

**Contact coverage for the named users — no gaps among the six that matter:**

| user | role | flag | phone | email | wa_pref | em_pref |
|---|---|---|---|---|---|---|
| praveen | Design | **True** | 9873340425 | praveen@horizonrenewablepower.com | True | True |
| priyanka | Design | False | 9873340425 | horizonrenewablepower@gmail.com | True | True |
| shyam | Design | False | 9873340425 | smzk07@gmail.com | True | True |
| nayeem | Design | False | 9873340425 | horizonrenewablepower@gmail.com | True | True |
| subhash | SCM | False | 9873340425 | horizonrenewablepower@gmail.com | True | True |
| chetan | PM | False | 9873340425 | horizonrenewablepower@gmail.com | True | True |

**Three findings that matter more than the absence of gaps:**

1. **`admin` has NO phone number** (`-- NONE --`). Any WhatsApp to that profile logs `failed: Recipient has no phone number`. Not a design-module recipient today, but it is the only profile with a missing contact field.
2. **Ten of fifteen users share the identical phone number `9873340425`.** This is test data. On production, every WhatsApp in a batch would land on one handset, and a "delivered" result proves nothing about whether the intended person received it. **The user must confirm production numbers are real and distinct.**
3. **No deputy is set anywhere**, so every "Design Head + deputy" recipient list currently resolves to Praveen alone. The deputy path will be exercised for the first time in production whenever a deputy is named.

### 2.6 The EOD digest

`projects/management/commands/send_eod_digest.py`, run from the Railway cron service `laudable-cat` (`30 14 * * *` UTC = 20:00 IST).

**How it selects content — five metrics, all keyed on `ActivityLog.action_code`, never free text** ([send_eod_digest.py:56-63](projects/management/commands/send_eod_digest.py#L56-L63)):
```python
CODE_TO_METRIC = {
    'task_status_in_progress': 'started',
    'task_status_done':        'closed',
    'issue_created':           'issues_raised',
    'issue_resolved':          'issues_resolved',
}
```
plus `assigned` — a snapshot count of open `Task` rows where `assigned_to = user`.

Recipients: active profiles, minus `EOD_DIGEST_EXCLUDED_ROLES` (`['CEO','Admin','System Admin']`), then **open-work gated** — a user with no open tasks/issues and no activity today is skipped and the skip is logged. Coordinators get a separate content branch. Bodies render from `projects/email/eod_digest.html` / `.txt`.

**Could design events be added without changing its structure?** **Partly — and the honest answer is no, not the useful half.**

* **Counting design *actions* is a config change only.** The design module already writes 21 distinct `action_code` values (`design_allocated`, `design_arka_approved`, `design_qc_passed`, `design_change_requested`, `site_group_locked`, …). Adding entries to `CODE_TO_METRIC` would count them with no structural change — the aggregate query already groups by `action_code`.
* **But the digest's own template would need new rows to display them**, and both `eod_digest.html` and `.txt` render a fixed five-metric layout.
* **And the numbers a Design Head actually needs are snapshots, not action counts** — "3 packages waiting for QC", "2 sites blocked". Those come from `DesignAssignment.status`, which the digest has no query for and no slot to render. `design_head_dashboard_counts()` ([design_views.py:1981](projects/design_views.py#L1981)) already computes exactly these four numbers and could be reused, but wiring it in is a new content branch, not a config edit.
* The digest is also **email-only and open-work gated**, so a Design Head with no open *Task* rows — likely, since design work lives on `DesignAssignment`, not `Task` — would be skipped entirely by `has_open_work` before any design content was considered.

**Not changed this session, as instructed.**

---

## 3. The six events, fully specified

Common to all six:

* **Channels: WhatsApp + email for all six.** In-app is added free by including `'in_app'` and costs nothing.
* **Header variable is always the project identifier** — see DECISION POINTS #3 on `customer_name` vs `project_id`.
* **Every template stays at or below 4 body variables**, matching the existing approved set.
* Sends belong **after** the `transaction.atomic()` block that performs the transition, matching every existing call site — `send_notification` never raises, but a send inside the transaction would fire on a subsequent rollback.
* **No existing approved template can be reused for any of the six.** Every existing template's body text is bound to a task, issue, payment, invoice or BOQ acknowledgement. All six require new submissions.

---

### Event 1 — Site allocated to a designer

| | |
|---|---|
| **Trigger** | `_allocate_one()` [design_views.py:365-387](projects/design_views.py#L365-L387), at `assignment.status = DESIGN_ALLOCATED`. Reached from `design_allocate` (single) and `design_bulk_allocate`. |
| **Transition** | `awaiting_allocation` / `allocated` / `due_date_proposed` → `allocated`; `action_code='design_allocated'` (or `design_reallocated`) |
| **Recipient** | The newly assigned designer — `designer` (the function argument), identical to `assignment.assigned_to` after the write |
| **Channels** | WhatsApp + email + in-app |
| **Reuse?** | **No — new template `design_site_allocated`** |

**Careful:** `design_bulk_allocate` loops `_allocate_one` over many sites. Notifying inside the loop sends one WhatsApp per site — a 20-site tender means 20 messages to one designer in seconds. **See DECISION POINTS #5.**

**WhatsApp** — header: project. Body (3):
```
Hi {{1}}, you have been allocated the design for {{2}}.
Design Head: {{3}}. Open the site in Horizon Solar PMS to propose your due date.
```
`{{1}}` designer_name · `{{2}}` project_name · `{{3}}` allocated_by_name

**Email** — subject `Design allocated: {project} — Horizon Solar PMS`
Body: greeting; who allocated it and when; the tender name; that the next action is to propose a due date for Design Head approval; link to `/design/<project_id>/work/`.

---

### Event 2 — Arka verdict (approved or rejected)

| | |
|---|---|
| **Trigger** | `design_arka_approve()` [design_views.py:1061](projects/design_views.py#L1061) and `design_arka_reject()` [:1101](projects/design_views.py#L1101) |
| **Transition** | `arka_submitted` → `in_design` (approved, `design_arka_approved`) or → `arka_rejected` (`design_arka_rejected`) |
| **Recipient** | `assignment.assigned_to` |
| **Channels** | WhatsApp + email + in-app |
| **Reuse?** | **No — ONE new template `design_arka_verdict` serves both outcomes** |

One template, not two: the verdict is a body variable. This halves the approval dependency and mirrors how `issue_created` serves three call sites.

**WhatsApp** — header: project. Body (4):
```
Hi {{1}}, your Arka for {{2}} was {{3}} by the Design Head.
Note: {{4}}
Open Horizon Solar PMS for the full package.
```
`{{1}}` designer_name · `{{2}}` project_name · `{{3}}` `approved` | `rejected` · `{{4}}` rejection reason, or `No remarks` when approved

`{{4}}` must never be empty — Interakt rejects blank body values outright (`"Body's variable value can not be null or empty"`, observed 22 Jun 05:27). Pass a literal fallback string.

**Email** — subject `Arka {verdict}: {project} — Horizon Solar PMS`
Body: verdict, reviewer name, timestamp, the full `rejection_reason` verbatim when rejected, the Arka version number, and the next action (approved → upload CAD and enter BOQ; rejected → submit a corrected Arka as the next version). Link to `/design/<project_id>/work/`.

---

### Event 3 — Package ready for QC

| | |
|---|---|
| **Trigger** | `_maybe_advance_to_artifacts_uploaded()` [design_views.py:809-839](projects/design_views.py#L809-L839), **only when it actually advances** — it is called from both `design_artifact_upload` and `design_boq_complete` and returns `False` when the package is still incomplete |
| **Transition** | → `artifacts_uploaded`; `action_code='design_artifacts_uploaded'` |
| **Recipients** | Every `UserProfile.objects.filter(is_design_head=True, is_active=True)`, **plus** each of their `design_head_deputy` values, deduplicated by pk |
| **Channels** | WhatsApp + email + in-app |
| **Reuse?** | **No — new template `design_qc_pending`** |

This is the one the brief calls the bottleneck, and it is also the one finding **F7/G7** flagged: today the Head learns a package is ready by being told out of band.

**WhatsApp** — header: project. Body (3):
```
{{1}} is ready for QC review.
Designer: {{2}}. Attempt {{3}}.
Open the QC queue in Horizon Solar PMS.
```
`{{1}}` project_name · `{{2}}` designer_name · `{{3}}` attempt_number

**Email** — subject `Ready for QC: {project} — Horizon Solar PMS`
Body: designer, attempt number, approved Arka version and capacity, the CAD files present, confirmation the BOQ is marked complete, and a link to `/design/<project_id>/qc/`. Mention the QC queue at `/design/qc/` for batch review.

---

### Event 4 — QC verdict (passed or failed)

| | |
|---|---|
| **Trigger** | `design_qc_pass()` [design_views.py:1525](projects/design_views.py#L1525) and `design_qc_fail()` [:1578](projects/design_views.py#L1578) |
| **Transition** | `in_qc` → `released` (`design_qc_passed`, also stamps `released_at`/`released_by`) or → new attempt via `_open_next_attempt` (`design_qc_failed`) |
| **Recipient** | `assignment.assigned_to` |
| **Channels** | WhatsApp + email + in-app |
| **Reuse?** | **No — ONE new template `design_qc_verdict` serves both outcomes** |

**WhatsApp** — header: project. Body (4):
```
Hi {{1}}, QC on {{2}} was {{3}}.
Remarks: {{4}}
Open Horizon Solar PMS for details.
```
`{{1}}` designer_name · `{{2}}` project_name · `{{3}}` `passed — site released` | `failed — rework required` · `{{4}}` `qc_remarks`, or `No remarks` on a pass

`qc_remarks` is required by DB constraint when the verdict is `failed`, so `{{4}}` is only ever empty on a pass — still pass a fallback.

**Email** — subject `QC {verdict}: {project} — Horizon Solar PMS`
Body: verdict, reviewer, timestamp, full `qc_remarks` verbatim, and the next action (passed → site released, no further design action; failed → a new attempt is open, address the remarks and resubmit). Link to `/design/<project_id>/work/`.

**Note:** a pass also means SCM can now group the site for procurement. That is a *separate* audience and is deliberately **not** notified — SCM's post-QC pool on the SCM dashboard already surfaces it with an age, which is exactly the "visible on a dashboard they are already opening" exclusion the brief sets out.

---

### Event 5 — PM change request raised

| | |
|---|---|
| **Trigger** | `design_change_request()` [design_views.py:1750-1758](projects/design_views.py#L1750-L1758), after `DesignChangeRequest.objects.create(...)` |
| **Transition** | current attempt closed, new attempt opened with `opened_reason='pm_change_request'`, status → `in_design`; `action_code='design_change_requested'` |
| **Recipients** | `assignment.assigned_to` **and** every Design Head + deputy |
| **Channels** | WhatsApp + email + in-app |
| **Reuse?** | **No — new template `design_change_requested`** |

The brief's strongest case: this **suspends an in-flight QC review without a verdict** and sends the site back to the designer immediately. Nobody can discover it any other way.

**WhatsApp** — header: project. Body (4):
```
{{1}} raised a change request on {{2}}.
Reason: {{3}}
Attempt {{4}} is now open and the site is back with the designer.
```
`{{1}}` pm_name · `{{2}}` project_name · `{{3}}` reason (truncated — see below) · `{{4}}` new attempt_number

`reason` is a free-text `TextField` with no length limit. WhatsApp body variables cannot contain newlines and are length-capped. **Truncate to ~200 characters with an ellipsis for WhatsApp; send the full text in the email.**

**Email** — subject `Change request raised: {project} — Horizon Solar PMS`
Body: requester, timestamp, the **full untruncated reason**, the new attempt number, and — when the request also pulled the site out of a draft procurement group (Part 6 §4) — the group name and that SCM's aggregate has changed. Link to `/design/<project_id>/work/`.

---

### Event 6 — Site blocked on an inadequate survey

| | |
|---|---|
| **Trigger** | `design_mark_blocked()` [design_views.py:674-711](projects/design_views.py#L674-L711) |
| **Transition** | → `survey_returned`; `action_code='design_blocked'` |
| **Recipients** | Every Design Head + deputy |
| **Channels** | WhatsApp + email + in-app |
| **Reuse?** | **No — new template `design_site_blocked`** |

Only the Head can clear it, by uploading a replacement survey. The designer's clock is stopped meanwhile.

**WhatsApp** — header: project. Body (3):
```
{{1}} has blocked {{2}} — the survey is inadequate.
Reason: {{3}}
Upload a replacement survey in Horizon Solar PMS to unblock.
```
`{{1}}` designer_name · `{{2}}` project_name · `{{3}}` `survey_return_reason` (truncate to ~200 chars)

**Email** — subject `Site blocked — survey inadequate: {project} — Horizon Solar PMS`
Body: designer, timestamp, full reason, that the designer's clock is stopped until a replacement survey is uploaded, and that blocked time does not extend the agreed due date (Part 5 settled decision 3). Link to `/programs/<program_pk>/design/`.

---

## 4. WHATSAPP TEMPLATES TO SUBMIT

**Six templates. Paste each block into the Interakt template submission form.**

Conventions matching the seven already approved: language **English**, category **Utility**, **one header variable** (the project), body variables numbered from `{{1}}` **within the body only** — the header variable is numbered separately by Interakt and is not part of the body sequence.

> **Before submitting, resolve DECISION POINTS #3** — whether the header carries `customer_name` (matching all seven existing templates) or `project_id` (which is what identifies an OPEX *site*). The header text below says "Project" and works either way, but the code must then match.

---

### Template 1 of 6

* **Template name:** `design_site_allocated`
* **Category:** Utility
* **Language:** English
* **Header (text, 1 variable):** `Design allocated — {{1}}`
* **Body:**
```
Hi {{1}}, you have been allocated the design for {{2}}.

Design Head: {{3}}

Please open Horizon Solar PMS to propose your due date for approval.
```
* **Body variables:** `{{1}}` = designer name · `{{2}}` = project name · `{{3}}` = name of the person who allocated it
* **Sample values:** `Shyam` · `Miracle Hospital` · `Praveen Kethuniya`
* **Serves:** Event 1 — site allocated

---

### Template 2 of 6

* **Template name:** `design_arka_verdict`
* **Category:** Utility
* **Language:** English
* **Header (text, 1 variable):** `Arka review — {{1}}`
* **Body:**
```
Hi {{1}}, your Arka for {{2}} was {{3}} by the Design Head.

Note: {{4}}

Open Horizon Solar PMS for the full package.
```
* **Body variables:** `{{1}}` = designer name · `{{2}}` = project name · `{{3}}` = `approved` or `rejected` · `{{4}}` = rejection reason, or `No remarks`
* **Sample values:** `Shyam` · `Miracle Hospital` · `rejected` · `Module layout exceeds the available roof area on the north block`
* **Serves:** Event 2 — Arka approved AND Arka rejected (one template, both outcomes)

---

### Template 3 of 6

* **Template name:** `design_qc_pending`
* **Category:** Utility
* **Language:** English
* **Header (text, 1 variable):** `Ready for QC — {{1}}`
* **Body:**
```
{{1}} is ready for QC review.

Designer: {{2}}
Attempt: {{3}}

Open the QC queue in Horizon Solar PMS to review the package.
```
* **Body variables:** `{{1}}` = project name · `{{2}}` = designer name · `{{3}}` = attempt number
* **Sample values:** `Miracle Hospital` · `Shyam` · `2`
* **Serves:** Event 3 — package ready for QC

---

### Template 4 of 6

* **Template name:** `design_qc_verdict`
* **Category:** Utility
* **Language:** English
* **Header (text, 1 variable):** `QC result — {{1}}`
* **Body:**
```
Hi {{1}}, QC on {{2}} was {{3}}.

Remarks: {{4}}

Open Horizon Solar PMS for details.
```
* **Body variables:** `{{1}}` = designer name · `{{2}}` = project name · `{{3}}` = `passed — site released` or `failed — rework required` · `{{4}}` = QC remarks, or `No remarks`
* **Sample values:** `Shyam` · `Miracle Hospital` · `failed — rework required` · `String sizing does not match the approved inverter model`
* **Serves:** Event 4 — QC passed AND QC failed (one template, both outcomes)

---

### Template 5 of 6

* **Template name:** `design_change_requested`
* **Category:** Utility
* **Language:** English
* **Header (text, 1 variable):** `Change request — {{1}}`
* **Body:**
```
{{1}} raised a change request on {{2}}.

Reason: {{3}}

Attempt {{4}} is now open and the site is back with the designer.
```
* **Body variables:** `{{1}}` = PM name · `{{2}}` = project name · `{{3}}` = reason (truncated to 200 characters) · `{{4}}` = new attempt number
* **Sample values:** `Chetan` · `Miracle Hospital` · `Client has moved the inverter room to the basement` · `3`
* **Serves:** Event 5 — PM change request raised

---

### Template 6 of 6

* **Template name:** `design_site_blocked`
* **Category:** Utility
* **Language:** English
* **Header (text, 1 variable):** `Site blocked — {{1}}`
* **Body:**
```
{{1}} has blocked {{2}} — the survey is inadequate.

Reason: {{3}}

Upload a replacement survey in Horizon Solar PMS to unblock the site.
```
* **Body variables:** `{{1}}` = designer name · `{{2}}` = project name · `{{3}}` = reason the survey was returned (truncated to 200 characters)
* **Sample values:** `Shyam` · `Miracle Hospital` · `Roof dimensions missing and no shadow analysis included` 
* **Serves:** Event 6 — site blocked on survey

---

**Submission checklist**

1. Submit all six together — approval time is per-template but the clock runs in parallel.
2. After approval, **read the registered variable order out of the Interakt console, not the preview.** §1 of this document is what happens when that is assumed. Record the exact header count and body count for each.
3. No body variable may ever be empty — Interakt rejects the whole message. Every optional value above has a documented fallback string.
4. Body variables cannot contain newlines. `reason` and `qc_remarks` are free-text `TextField`s and must be truncated and newline-stripped before sending.

---

## 5. UNCERTAIN

Everything here is stated as unknown rather than guessed.

1. **The authoritative registered variable order and body text of all seven existing templates.** Readable only in the Interakt console. §2.3's body-variable meanings are inferred from call sites and from `test_whatsapp.py`'s hand-written comments — which the author themselves annotated `CONFIRM body is 4, not 2` and `CONFIRM — your label says 3, your list has 4`. **Counts** for `assign_project` (body 3) and `issue_resolved` (body 4) are certain, because Interakt stated them in an error. Their **meanings and order** are not.
2. **`boq_acknowledged`'s registered body count is inferred, not proven.** Two independent sources say 2; no HTTP 400 has ever named the number.
3. **`SystemSettings.email_enabled` and `whatsapp_enabled` on production.** Database rows, not environment variables; the production DB was not read. Local shows both OFF. **If `email_enabled` is False on production, everything 7b builds will log `skipped: Master switch off` and send nothing.**
4. **Whether production phone numbers are real and distinct.** Ten of fifteen local profiles share `9873340425`. If production mirrors this, WhatsApp is untestable per-recipient and a delivery confirmation proves nothing.
5. **Whether production `UserProfile` rows have complete email addresses.** Locally only `admin` lacks a phone; nobody lacks an email. Not verified on production.
6. **Whether any WhatsApp message has ever actually reached a phone.** `NotificationLog.delivery_status` is empty on every row, and `INTERAKT_WEBHOOK_SECRET` is empty on Railway, so the delivery callback cannot be verified. `sent` means "Interakt accepted it", nothing more.
7. **Whether the Interakt account has a template quota or per-message cost** that a six-template addition would affect.
8. **What the Railway cron service `laudable-cat` actually runs.** It is scheduled `30 14 * * *` and is assumed to be `send_eod_digest`, but its start command was not read.
9. **Whether `Program.name` or `Project.customer_name` is the more useful header for an OPEX site** in the recipients' own reading — a product judgement, not a code fact.
10. **The real-world volume of Event 1 in bulk allocation.** No production tender has been bulk-allocated yet, so the burst size is theoretical.

---

## 6. DECISION POINTS

Stated neutrally. Each must be answered before 7b.

1. **Are the three broken call sites fixed before 7b, or after?**
   §1 shows `assign_project` and `issue_resolved` are provably still broken and `boq_acknowledged` almost certainly is. Each is short by exactly one body value, and the shape that worked is recoverable from the pre-`3ceffd9` code. Fixing first means new design notifications land on a layer whose failures are all attributable to new code. Fixing after means old and new failures are indistinguishable in `NotificationLog`, which is the situation this session was asked to prevent.

2. **Is `SystemSettings.email_enabled` turned on for production, and when?**
   7b delivers nothing observable while it is off. Turning it on also releases every *existing* email trigger — task assignment, issue created, issue resolved, BOQ acknowledged, payment, invoice — to every active user at once, plus the individual EOD digest. Whether that is wanted on the same day as the design emails is a separate decision.

3. **Does the WhatsApp header carry `customer_name` or `project_id`?**
   All seven existing templates pass `project.customer_name`. For an OPEX site that field is documented as carrying a different meaning by `project_type` ([models.py:95](projects/models.py#L95)), while `project_id` is the `{short_tender_code}-{site_code}` identifier a designer would recognise. The six new templates must pick one, and the header text submitted to Meta should match.

4. **Do all six events go to both WhatsApp and email, or is WhatsApp reserved for a subset?**
   The specification above proposes both for all six. An alternative is WhatsApp only for the three that stop work (3, 5, 6) and email for the rest — fewer templates to submit, and less risk of the channel being muted.

5. **What happens on bulk allocation?**
   `design_bulk_allocate` calls `_allocate_one` in a loop. One notification per site means a 20-site tender sends 20 WhatsApp messages to one designer within seconds. The options are: notify per site regardless; suppress inside the bulk path and notify only from the single-site path; or send one summary message per designer per bulk operation — which would need a **seventh** template and changes the body shape.

6. **Does the Design Head get their own notification when they are the acting designer?**
   `user_can_qc_design()` already refuses self-QC, but nothing stops the Head being allocated a site. Events 2 and 4 would then notify them as designer while events 3, 5 and 6 notify them as Head — potentially two messages for one transition. Deduplicating by pk across a combined recipient list is the mechanical fix; whether the Head should be excluded from designer-role notifications is the product question.

7. **Are the missing Railway environment variables set as part of 7b, or separately?**
   `ADMIN_DIGEST_EMAIL` and `HR_DIGEST_EMAIL` are absent, so the company-wide EOD aggregate raises `CommandError` on every cron run and has never sent. `APP_BASE_URL` is absent, so every emailed link points at the `railway.app` host rather than `pms.horizonrenewablepower.in`. Neither is caused by the design module, and both affect emails 7b will send.

8. **Should `_send_whatsapp` validate parameter counts before posting?**
   There is no template registry and no length check, which is why §1 went unnoticed for five weeks. A per-template expected-count map would turn a silent HTTP 400 into a startup-visible error. It is new infrastructure and it is not required by any of the six events, so it is a scope decision rather than a bug fix.

9. **Are design events added to the EOD digest, and in which form?**
   §2.6: counting design *actions* is a `CODE_TO_METRIC` edit; showing the numbers a Design Head needs (packages awaiting QC, sites blocked) requires a new content branch and template rows, and the open-work gate would skip a Head who holds no `Task` rows. This may belong in its own part rather than 7b.
