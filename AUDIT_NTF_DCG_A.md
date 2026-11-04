# AUDIT NTF-DCG-A — DC WhatsApp to SE + GRN confirmation email

Read-only audit, 2026-11-04. Code at `f4487ac` (main; `git log origin/main..main` = 0, so everything is pushed). Data from local Postgres `solarpms_local` (the 31-Oct production restore), read through a throwaway management command (`projects/management/commands/tmp_ntf_dcg_audit.py`). The command was **deleted after the run**, along with its `.pyc` files, and `git status --short` was clean afterwards apart from `?? .env1`. Raw output is quoted in full under each task.

Context correction applied (Zuber, "proceed" message): `notify()` (`projects/notify.py`) + `notify_registry.py` is the entry point for new events, and `send_notification()` is the final sender.

---

## 1. Pre-flight output

```
main
f4487ac Snapshot designer on BOQ corrections; threshold and unassigned note on panel
--- status (excl staticfiles) ---
?? .env1
```

Assumptions already seen to be wrong at pre-flight (all confirmed below):
1. `send_notification()` is the final sender, not the entry point. `notify()` sits above it.
2. The WhatsApp side of Admin template management is not built (T3b).
3. Santosh is hardcoded **by email** for Residential invoice / finance-confirmation **task assignment**, not as a notification recipient. Shankey appears nowhere in code.
4. `daily_task_report` is an Interakt template name. The command that sends it is `send_morning_task_report`.

No stop condition was triggered:
- The tree is clean.
- The DC creation path (`views.py:13765`) and the GRN paths (`views.py:14542`, `:14667`) exist.
- `send_notification()` exists at `notifications.py:24`.
- DC status changes only through `models.recalculate_dc_status()`, a plain module function the views call. No DC/GRN `save()` override or signal exists. The one `post_save` receiver on `StatusTransition` returns early for every subject except payment requests (`signals.py:36-37`: `if not created or instance.subject_type != SUBJECT_PAYMENT_REQUEST: return`).

---

## 2. Findings T1–T7

### T1 — DC model and creation path

**`DeliveryChallan`, `projects/models.py:1600-1667` (fields verbatim):**
```python
project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='delivery_challans')
vendor = models.ForeignKey(Vendor, on_delete=models.SET_NULL, null=True, blank=True, related_name='delivery_challans')
po_number = models.CharField(max_length=100, blank=True, default='')
# po_number is a free-text reference to external PO (Excel/Zoho Inventory)
# PO creation inside SolarPMS is Phase 2 scope — do NOT add PO model today
issued_from_warehouse = models.ForeignKey('StockLocation', null=True, blank=True, on_delete=models.PROTECT, related_name='delivery_challans')
dc_number = models.CharField(max_length=100)
dc_date                = models.DateField()
expected_delivery_date = models.DateField(null=True, blank=True)
status = models.CharField(max_length=30, choices=STATUS_CHOICES, default=EXPECTED)   # Expected / Partially Received / Received / Rejected
notes      = models.TextField(blank=True, default='')
created_by = models.ForeignKey(UserProfile, on_delete=models.SET_NULL, null=True, blank=True, related_name='created_challans')
created_at = models.DateTimeField(auto_now_add=True)
client_uuid = models.UUIDField(null=True, unique=True, editable=False)
```

**`DCLineItem`, `models.py:1670-1763`:**
```python
challan          = models.ForeignKey(DeliveryChallan, on_delete=models.CASCADE, related_name='line_items')
boq_category     = models.CharField(max_length=50, choices=CATEGORY_CHOICES)   # Solar Modules / Structure / Inverter / BOS
item_description = models.CharField(max_length=255)
ordered_quantity = models.DecimalField(max_digits=10, decimal_places=2)
unit             = models.CharField(max_length=20, default='Nos')
received_quantity = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
condition = models.CharField(max_length=20, choices=CONDITION_CHOICES, null=True, blank=True)
damaged_quantity = models.PositiveIntegerField(default=0)  # How many of received_quantity arrived damaged — precise complement to received_quantity
grn_date         = models.DateField(null=True, blank=True)
grn_confirmed_by = models.ForeignKey(UserProfile, on_delete=models.SET_NULL, null=True, blank=True, related_name='confirmed_line_items')
grn_notes = models.TextField(blank=True, default='')
grn_on_behalf        = models.BooleanField(default=False)
grn_on_behalf_reason = models.TextField(blank=True, default='')
```

- **Delivery date:** `expected_delivery_date` exists and is **nullable**. The create form treats it as optional: `views.py:13884` `expected_delivery_date, expected_delivery_error = check_typed_date(expected_delivery_s)`, and a blank value passes as None. `dc_date` (the dispatch/challan date) is required (`views.py:13874`). Locally all 19 DCs have it set (`DCs with expected_delivery_date set=19`), but nothing enforces that.
- **Rate / price / amount:** **none** on `DeliveryChallan` or `DCLineItem`. The full field lists are quoted above. The only price exposure is a vendor document attached at create (`DeliveryChallanAttachment`). That is held by a no-prices tick plus a CHECK (`models.py:1811-1813`), which is the uploader's word for it, not a content check.

**Every code path that creates a DC:**

| Path | file:line | In `transaction.atomic`? | Final successful save point |
|---|---|---|---|
| `create_delivery_challan` (SCM, `@role_required(['SCM'])` `views.py:13763-13764`) → `_create_delivery_challan_rows` | `views.py:14041` (challan), `:14065` (lines), `:14071` (attachments) | Yes, `views.py:14040` `with transaction.atomic():` | The block exits at `views.py:14102`. The commit point is the end of that `with`. After it, the view only logs activity and redirects (`:14022-14028`). |
| `seed_opex_test_data` (seed, marked `# NO PRODUCT PATH`) | `management/commands/seed_opex_test_data.py:673`, lines `:685` | Not verified | n/a (seed) |
| `seed_walkthrough` | `seed_walkthrough.py:1322` POSTs to the real `create_delivery_challan` view | Through the view | Same as the view |
| `dc_edit.save_challan_edit` creates **new lines** on an existing DC | `dc_edit.py:729` `DCLineItem.objects.create(challan=locked, **values)` | Yes (under `lock_challan`) | Inside `save_challan_edit` (`dc_edit.py:648`) |
| Django admin | `projects/admin.py` has **no** `DeliveryChallan` / `DCLineItem` registration (grep returned nothing) | — | — |

`seed_scm_pilot.py:188` lists `'DeliveryChallan'` in `NOT_CREATED`.

**DC edit after creation (DC-B3, `delivery_challan_edit` `views.py:14220` → `dc_edit.py`):**
- Header, editable in any status: `dc_edit.py:194-196` `('vendor', 'Vendor'), ('dc_number', 'DC number'), ('dc_date', 'DC date'), ('expected_delivery_date', 'Expected delivery'), ('issued_from_warehouse', 'Warehouse'), ('po_number', 'PO reference'), ('notes', 'Notes')`.
- Lines without a receipt: `dc_edit.py:81` `LINE_EDIT_FIELDS = ('boq_category', 'item_description', 'ordered_quantity', 'unit')`. Lines can also be removed, and new lines added (`:729`).
- Lines with a receipt: description and unit only (memory/DC-B3 commit message `77a12b0`: "received lines description/unit only").
- **So yes: the material lines, quantities and expected delivery date can all change after an SE has been notified.** `dc_edit.py` sends no notification (grep for `send_notification|notify` in `dc_edit.py` returned nothing).

**One DC, more than one site?** No. `project` is a single non-null FK (`models.py:1615`), and the create URL takes one `project_id` (`views.py:13765`).

**DC for a site with no SE?** Yes. `create_delivery_challan` has no SE or task check of any kind; its only guards are the role decorator, the key guard and field validation (`views.py:13786-13990`). Locally every OPEX site with a DC has ≥1 SE (T2), so a no-SE case is reachable but not present in the data.

### T2 — "SE assigned to the site"

Mechanisms found:
1. **Holding any task on the project.** `permissions.py:322-325`:
   ```python
   if role == 'Site Engineer':
       # Mirrors dashboard_site_engineer's scoping: any task on this project assigned
       # to this user. Reverse relations only — keeps this module import-free.
       if project.phases.filter(tasks__assigned_to=profile).exists():
   ```
   This is the only relationship that gives an SE visibility or work rights (`user_can_view_project` / `user_works_on_site`, `permissions.py:143`, `:200`). It is **any task, any status, any role on the task**. The SE dashboard uses the same rule (`views.py:1234` `phases__tasks__assigned_to=se_profile`).
2. **`Task.assigned_role = 'Site Engineer'`** (`models.py:420`), a task's role. Seeds pick SE tasks this way (`seed_walkthrough.py:1296`), but it is not a relationship to a person on its own.
3. **The site's QA/QC engineer**, `SiteQaqcAssignment` (`models.py:6658`), roles `QAQC_ENGINEER_ROLES = frozenset({'Site Engineer', 'PM', 'Project Coordinator'})` (`permissions.py:1715`). This grants **view only**. `confirm_grn` refuses it (`views.py:14582` `if not user_works_on_site(request.user, project): return HttpResponseForbidden()`).
4. **No project-level SE field.** The `Project` FKs/M2Ms are `program`, `assigned_pm`, `assigned_design`, `coordinators`, `created_by` (awk over `class Project`); none is an SE.
5. `bill_rules._site_engineer_ids_on` (`bill_rules.py:151-154`) returns "every profile holding a task on `project`", **any role** (not only SEs), for bill warnings.

**Existing helper that returns a site's SEs?** **None.** The nearest are `bill_rules._site_engineer_ids_on` (any task-holder, not role-filtered, private to bills) and `bill_rules.site_engineer_choices` (every active SE in the company, with on-site ones first).

**Local data, OPEX sites with ≥1 DC (not soft-deleted). Raw output:**
```
== T2: SEs per RESCO/OPEX site with >=1 DC (not soft-deleted) ==
  OPEX sites with >=1 DC: 10
  M1 role=SE profile holding ANY task (user_can_view_project SE branch): 0:0, 1:9, 2:0, 3+:1
  M1a same, active profile+login only: 0:0, 1:9, 2:0, 3+:1
  M2 anyone holding a task with assigned_role=Site Engineer: 0:0, 1:9, 2:0, 3+:1

  Residential/CAPEX sites with >=1 DC, M1a:
  Residential sites=1: 0:0, 1:1, 2:0, 3+:0
  CAPEX sites=0: 0:0, 1:0, 2:0, 3+:0

== extra: per OPEX site SE detail ==
  UKRU001 dcs=4 abhijeet(tasks=29,not_done=16); arfan(tasks=1,not_done=0); awdhesh(tasks=1,not_done=1); goutam(tasks=1,not_done=1); naresh(tasks=1,not_done=1); rohit(tasks=1,not_done=0); tarundeep(tasks=1,not_done=1)
  2512 dcs=1 abhijeet(tasks=1,not_done=1)
  2503 dcs=1 preetam(tasks=1,not_done=1)
  2422 dcs=1 avishek(tasks=1,not_done=1)
  2402 dcs=1 tarundeep(tasks=1,not_done=1)
  2209 dcs=1 vijay(tasks=1,not_done=1)
  1918 dcs=1 preetam(tasks=1,not_done=1)
  1908 dcs=1 rohit(tasks=1,not_done=1)
  1905 dcs=1 naresh(tasks=1,not_done=1)
  1904 dcs=1 awdhesh(tasks=1,not_done=1)
```
UKRU001 has 7 SEs under the "holds any task" rule. Two of them (arfan, rohit) hold only Done tasks there. The mechanism a build picks decides whether those two get the WhatsApp.

### T3 — Phone numbers and WhatsApp plumbing

**Storage:** `UserProfile.phone_number = models.CharField(max_length=10, blank=True)` (`models.py:787`, within `class UserProfile` at `:749`). It is a 10-digit Indian mobile, with no country code. The forms enforce it at `forms.py:72-78`, `:132-138`, `:241-247`:
```python
if not re.fullmatch(r'[0-9]{10}', value):
    raise forms.ValidationError('Phone number must be exactly 10 digits.')
if value[0] not in '6789':
    raise forms.ValidationError('Phone number must start with 6, 7, 8, or 9.')
```
`send_notification` adds `+91` (`notifications.py:136-157`).

**Local data. Raw output:**
```
== T3: active SE phone numbers ==
  active SEs=23 blank=0 malformed=0 whatsapp_notifications_off=0
  phone values not 10 chars across ALL profiles: 0
  SystemSettings whatsapp_enabled=True email_enabled=True
  NotificationEventSetting rows=0; whatsapp-channel rows=0
  NotificationLog whatsapp by status: [{'status': 'failed', 'n': 601}, {'status': 'sent', 'n': 959}, {'status': 'skipped', 'n': 753}]
```
"Malformed" here means not matching `^[6-9][0-9]{9}$`.

**`send_notification()` signature, `notifications.py:24-37`:**
```python
def send_notification(
    recipient, message, channels=None, link='', subject='', template=None,
    template_params=None, related_project=None, actor=None, html_message=None,
    event_code=None, template_version=None,
):
```

**How a WhatsApp send maps to Interakt (`notifications.py:126-189`):** the template name is the `template` argument. The language is hardcoded `'languageCode': 'en'` (`:162`). Params `'headerValues': params[:1]` and `'bodyValues': params[1:]` (`:163-164`). Success is `if 200 <= resp.status_code < 300:` (`:179`). The gates are the master switch `SystemSettings.whatsapp_enabled` (`:94-96`) and `recipient.whatsapp_notifications` (`:97-99`). Every outcome writes a `NotificationLog` row (`_log`, `:345-366`). **No registry or event involvement:** WhatsApp is mapped at each call site, by passing a literal template name and an ordered list.

**A working call site end to end: `send_morning_task_report`.**
- `send_morning_task_report.py:56` `TEMPLATE = 'daily_task_report'`
- `:65-76` `whatsapp_params()` returns `[date_str, first_name, scope, delayed, due_today, no_due_date, link]`, the header first.
- `:111` `channels_wanted = [EMAIL] if options['no_whatsapp'] else [EMAIL, WHATSAPP]`
- `:189-195`:
  ```python
  send_notification(
      report['profile'], text_body, channels=channels,
      subject=email_subject(report, date_str), html_message=html_body,
      template=TEMPLATE,
      template_params=whatsapp_params(report, date_str, report['link']),
  )
  ```
- It bypasses `notify()` entirely (own HTML template `projects/email/morning_task_report.html`, own idempotency read of `NotificationLog`, `:153-158`).

**Synchronous? Can a failure roll back or error the triggering request?**
- HTTP is synchronous in the calling thread: `requests.post(..., timeout=10)` (`notifications.py:169-177` Interakt, `:224-232` ZeptoMail).
- Every exception is caught and logged: `except Exception as e: _log(base, 'whatsapp', 'failed', str(e))` (`:188-189`) and `except Exception as e: return False, str(e)` (`:236-237`).
- `notify()` defers to `transaction.on_commit(..., robust=True)` (`notify.py:366-370`), so a send runs **after** the commit and cannot roll the action back. Robust means an exception in the callback is logged, not raised. The direct WhatsApp legs use the same pattern, for example `grn_damage.py:446` `transaction.on_commit(_send_issue_whatsapp, robust=True)`.
- **But it is still inside the request.** On-commit callbacks run in the request thread before the response returns. So a slow Interakt/ZeptoMail adds up to 10 s **per recipient per channel** to the GRN or DC request. The worst case for a GRN email to ~6 people is about 60 s. The Procfile runs `gunicorn solarpms.wsgi --log-file -` with **no `--timeout`**. Gunicorn's default (30 s) is UNVERIFIED for the Railway runtime, but if it applies, a slow mail provider could kill the worker **after** the commit. The data would be saved, the user would get an error page, and later recipients would never be sent to.

**Admin template-management layer: what a new event needs (from the code):**
1. Add a `NotificationEvent(...)` to `NOTIFICATION_EVENTS` in `notify_registry.py`: `code`, `label`, `module`, `tier`, `channels`, `variables`, `in_app_text`, `default_subject`, `default_body`, `button_label`, `recipients_rule` (documentation only, `notify_registry.py:10-11`), `default_state` (`'active'|'paused'`), and `whatsapp_sent_separately`. `_build_registry` checks the placeholders at import time (`:90-122`).
2. The call site resolves its own recipients through `permissions.py` helpers and calls `notify(code, recipients=..., values=..., actor=..., related_project=..., link=..., log_label=...)` (`notify.py:330`).
3. Admin's per-channel pause is a `NotificationEventSetting` row (`models.py:8118`, unique on `(event_code, channel)`). With no row, the registry `default_state` applies (`notify.py:174-180`). `set_state()` (`notify_admin.py:366`) writes the row plus an append-only `NotificationEventSettingChange`, and refuses to switch email on until the live content has been test-sent (`:392-395`).
4. Versioning is `NotificationTemplate` (`models.py:7946`): draft → test-send → `activate()` (`notify_admin.py:298`, refused unless `version_tested`). With no row, the code default renders.
5. `NotificationLog` columns written: `recipient, channel, status, message, template_name (=log_label), related_project, actor, error_detail, interakt_message_id, created_at, event_code, template_version, delivery_status` (`models.py:2469-2531`).
6. Two tests pin the registry contents (`tests_notify.py`, `tests_notify_migration_c.py`, matched on `NOTIFICATION_EVENTS)`). They will need updating.

### T3b — WhatsApp control today

**Every WhatsApp send, how it is sent, and what can stop it:**

| Call site | file:line | Interakt template | Via registry? | Pause / kill switch other than the global master + user pref |
|---|---|---|---|---|
| Task status change, payment milestone | `views.py:6933` in `_apply_task_status_change` (`:6584`), on_commit `:6940` | `payment_notification` | Direct `send_notification` | **None.** The event `payment_milestone_reached` is `channels=('email',)`, `whatsapp_sent_separately=True` (`notify_registry.py:297-325`) |
| BOQ acknowledged | `views.py:8570` in `_notify_boq_acknowledged` (`:8525`), on_commit `:8580` | `boq_acknowledged` | Direct | None (`boq_acknowledged`, `whatsapp_sent_separately=True`, `:271-295`) |
| Zoho webhook PM assigned | `views.py:11993` in `zoho_deal_closed_webhook` (`:11796`), on_commit `:12003` | `assign_project` | Direct | None (`project_pm_assigned`, `:212-238`) |
| Admin assigns PM | `views.py:15734` in `admin_assign_pm` (`:15638`), on_commit `:15744` | `assign_project` | Direct | None |
| Project issue raised | `views.py:12863` in `create_project_issue` (`:12777`) | `issue_created` | Direct | None (`issue_raised`, `:158-185`) |
| Task issue raised | `views.py:12977` in `create_task_issue` (`:12890`) | `issue_created` | Direct | None |
| Delivery / damage issue raised | `grn_damage.py:432-446` (`notify_delivery_issue_raised`; called from `create_delivery_issue` `views.py:13078`, `confirm_grn` `:14648`, `override_grn` `:14811`) | `issue_created` | Direct | None |
| Issue resolved | `views.py:13275` in `resolve_issue` (`:13196`), on_commit `:13282` | `issue_resolved` | Direct | None (`issue_resolved`, `:187-210`) |
| Task assignment | `utils.py:156-165` inside `decide_and_send` (`:116`), on_commit `:167` | `label` (assign_task / bulk) | Direct, beside `notify_now` | None (`task_assigned`, `tasks_assigned_bulk`, `whatsapp_sent_separately=True`) |
| Attendance not logged | `send_attendance_alerts.py:280-285` | `WHATSAPP_TEMPLATE` (`contractor_attendance_missing`) | Direct, `event_code=EVENT_CODE` passed for the log | **Env var** `settings.ATTENDANCE_ALERT_WHATSAPP_ENABLED` (`settings.py:196-199`, default False), read at `send_attendance_alerts.py:184`. The settings comment: "the event's Admin switch cannot pause it; this does." |
| Morning task report | `send_morning_task_report.py:189-195` | `daily_task_report` | Direct | CLI flag `--no-whatsapp` only (`:111`) |
| `test_whatsapp` command | `management/commands/test_whatsapp.py:117` | (manual) | Direct | n/a |

The only switches that apply to all of them are `SystemSettings.whatsapp_enabled` (`notifications.py:94`, a DB singleton, currently **True** locally) and `UserProfile.whatsapp_notifications` (`:97`). **No registry flag, no `NotificationEventSetting` row and no per-event setting pauses any WhatsApp send.** The one per-event control is the attendance env var.

Why the Admin layer cannot reach WhatsApp today:
- `notify()` only does in-app and email: `_send` → `_send_to_one`, which calls `send_notification(channels=['in_app'])` then `['email']` (`notify.py:245-284`). There is no WhatsApp branch.
- No event declares `'whatsapp'` (all 38 have `channels=('email',)`; grep for `channels=(.*whatsapp` returned nothing). `notify_admin._event_for` refuses a channel the event does not declare (`notify_admin.py:54-57`).
- `send_test` refuses WhatsApp (`notify_admin.py:217-220`): `# Only email can be tested: no event declares WhatsApp yet, and a WhatsApp test needs an approved Interakt template (ruling Q8).` and `if channel != NOTIFY_CHANNEL_EMAIL: raise NotificationAdminRefused('Test sends are available for email only.')`.
- `NotificationTemplate` already has `wa_template_name`, `wa_language`, `wa_approval_status` (Pending/Submitted/Approved/Rejected), `wa_slots` (`models.py:8016-8019`, choices `:7989-7994`). **Nothing reads them** except `create_draft` copying them (`notify_admin.py:97-98`). No send path, screen or gate uses them.

**Registry entry, verbatim. Email-only event `boq_acknowledged`, which is also `whatsapp_sent_separately` (`notify_registry.py:270-295`):**
```python
NotificationEvent(
    code='boq_acknowledged',
    label='BOQ acknowledged by SCM',
    module='BOQ',
    tier=TIER_KNOW,
    channels=('email',),
    variables=(
        Variable('customer_name', 'Customer name of the project', 'Sharma Residence'),
        Variable('project_id', 'Project ID', 'HRP-RES-2026-014'),
        Variable('acknowledged_by_name', 'The SCM person who acknowledged the BOQ',
                 'Anil Gupta'),
    ),
    in_app_text=('The Bill of Quantities for project {{ customer_name }} has been '
                 'acknowledged by {{ acknowledged_by_name }}. Material procurement can '
                 'now proceed.'),
    default_subject='BOQ Acknowledged — {{ customer_name }}',
    default_body=(
        'The Bill of Quantities for project {{ customer_name }} has been acknowledged '
        'by {{ acknowledged_by_name }}.\n\n'
        'Material procurement can now proceed.'
    ),
    button_label='View in Horizon Solar PMS',
    recipients_rule='The project managers (PM and coordinators).',
    default_state='active',
    whatsapp_sent_separately=True,
),
```
**A plain email event with no WhatsApp leg, `admin_test_message` (`notify_registry.py:127-147`):**
```python
NotificationEvent(
    code='admin_test_message',
    label='Admin test message',
    module='Notifications',
    tier=TIER_KNOW,
    channels=('email',),
    variables=(
        Variable('sender_name', 'The Admin who sent the test', 'Asha Rao'),
    ),
    in_app_text='Test message from {{ sender_name }}.',
    default_subject='Test message from Horizon Solar PMS',
    default_body=(
        'Hi {{ recipient_name }},\n\n'
        'This is a test message sent by **{{ sender_name }}** from the notification '
        'settings.\n\n'
        'If you are reading it, email from Horizon Solar PMS reaches you.'
    ),
    button_label='Open Horizon Solar PMS',
    recipients_rule='The person the Admin picks on the test-send screen.',
    default_state='active',
),
```
The one existing `default_state='paused'` event is `contractor_attendance_missing` (`notify_registry.py:1251`), which pauses its email only.

**The smallest Admin-pausable WhatsApp control for ONE new event that fits the existing per-channel design (described only, no code):**
1. Declare the event with `channels=('whatsapp',)` (or `('email', 'whatsapp')`) and `default_state='paused'`. The registry already accepts `'whatsapp'` (`CHANNELS = ('email', 'whatsapp')`, `notify_registry.py:33`). The Admin screens list a switch for every declared channel (`notify_views.py:149-153`, `:198-205`), and `set_state()` would then accept `(code, 'whatsapp')`. The pause therefore costs no new model, migration or screen.
2. The send has to honour that switch. Either `notify()` gains a WhatsApp leg that calls `channel_is_active(event, 'whatsapp')` (`notify.py:174`) before `send_notification(channels=['whatsapp'], template=..., template_params=...)`, or the call site checks `channel_is_active` itself, the same way `send_attendance_alerts` checks the email side (`:180`). If declared without that, the registry's own warning comes true (`notify_registry.py:152-154`: "no Admin switch here can appear to pause a WhatsApp it does not control").
3. A paused send should write the same `'skipped' / 'event paused'` row the email side writes (`notify.py:264-266`).
4. **The gap against the locked rule:** to activate only when "Approved in Interakt, mapped and test-sent", `set_state` would need a WhatsApp counterpart of its email-only test gate (`notify_admin.py:392-395`). It would require an active `NotificationTemplate` for `(code, 'whatsapp')` with `wa_approval_status='Approved'`, a `wa_template_name`, `wa_slots` that match the event's variables, and a WhatsApp test-send row. `send_test` refuses WhatsApp today (`:219-220`). Without that gate, steps 1–3 are a pause switch only, with no activation discipline. If Zuber accepts that for the first event, the env-flag precedent (`ATTENDANCE_ALERT_WHATSAPP_ENABLED`) is the even smaller alternative, but it is **not** Admin-pausable.

### T4 — GRN model and confirmation path

**There is no GRN model.** A GRN is the set of receipt columns on `DCLineItem` (quoted in T1): `received_quantity`, `damaged_quantity`, `condition` (derived), `grn_date`, `grn_confirmed_by`, `grn_notes`, `grn_on_behalf`, `grn_on_behalf_reason`. There are no accepted, short or rejected-quantity columns. Short has to be computed as `ordered_quantity - received_quantity` (T7).

**GRN attachments: only damage photos.** `DCLineDamagePhoto` (`models.py:1848-1934`):
```python
line = models.ForeignKey(DCLineItem, on_delete=models.PROTECT, related_name='damage_photos')
issue = models.ForeignKey(Issue, on_delete=models.PROTECT, null=True, blank=True, related_name='damage_photos')
uploaded_by = models.ForeignKey('UserProfile', on_delete=models.PROTECT, related_name='dc_damage_photos_uploaded')
uploaded_at = models.DateTimeField(default=timezone.now)
original_name = models.CharField(max_length=255)
bucket = models.CharField(max_length=100)
path = models.CharField(max_length=500, unique=True)
size = models.PositiveIntegerField()
mime = models.CharField(max_length=100)
withdrawn_at / withdrawn_by / withdrawn_reason
```
- CHECK `mime__in=['image/jpeg', 'image/png']` (`:1900-1902`).
- A photo is allowed only on a damaged line: `grn_damage.py:221-224` refuses "photos were attached, but no units are marked damaged". **An SE cannot attach any photo or document to a GRN with no damage.** No GRN document model exists.
- `DeliveryChallanAttachment` (`models.py:1769`) is SCM's vendor document at **create**, not an SE GRN attachment.
- **"Plus any photos/documents the SE attached at GRN" therefore means at most the damage photos.** On a clean receipt there is nothing.

**What "confirmed" means:**
- It is a single SE POST to `confirm_grn` (`views.py:14542`, `@role_required(['Site Engineer'])` plus `user_works_on_site` `:14582`). That writes the lines (`:14625-14632`), then calls `recalculate_dc_status(...)` (`:14639`), which sets `DeliveryChallan.status`. There is no draft/submit/approve step.
- **Partial:** a blank received box skips the line (`views.py:14499-14500` `if received_qty is None: continue  # Skip items where no received quantity was entered`). One DC can therefore be "confirmed" several times, one line per submission.
- **Re-confirm:** an SE may POST again unless the DC is already Received (`views.py:14591-14592` `if challan.status == DeliveryChallan.RECEIVED: return HttpResponseForbidden()`). A Partially Received or Rejected DC can be re-confirmed, and the earlier receipt values and `grn_confirmed_by` are overwritten (`:14626-14632`). Only the ledger and activity log keep any history.
- **Empty submission:** a POST with every box blank passes. `grn_lines=[]`, no line is saved, the status is recalculated and the activity log still says `"SE confirmed GRN for DC …"` (`:14643-14646`).
- **Override:** SCM `override_grn` (`views.py:14667`) has "No status restriction: an override may correct a Received or Rejected DC."
- **Void:** there is no void or reverse. DEL-A already recorded "GRN reversal … needs its own in-app flow" (SECONDARY_FINDINGS, DEL-A section).
- **One GRN per DC?** In storage, yes: one set of receipt columns per line. There is no GRN number and no per-receipt row. Locally: `DCs whose lines carry >1 distinct grn_date=0`, `DCs with some but not all lines received=0`.

```
== T4: GRN state on lines ==
  lines with received_quantity set=21 with damaged>0=0 on_behalf=0 grn_notes non-blank=0
  DCs whose lines carry >1 distinct grn_date=0
  DCs with some but not all lines received=0
```

**The damage flow's notifications (so SCM is not emailed twice):**
- They are sent only when a damage Issue is **newly created** (`views.py:14647-14649` `if damage is not None and damage.created: notify_delivery_issue_raised(damage.issue, profile, also_tell=damage_issue_scm_recipients())`). Adding damage to an open damage Issue writes only a Comment and sends nothing (`grn_damage.py:361-363`).
- Recipients and channels, from `grn_damage.py:394-467`:
  - The Issue's assignee, i.e. the site's **assigned PM only** if active (`damage_issue_assignee`, `:134-146`), gets `notify('issue_raised')` (in-app + **email**) and a separate **WhatsApp** `issue_created` (`:420-446`).
  - **Every active SCM user** (`damage_issue_scm_recipients` → `active_scm_users()`) gets `notify('issue_raised')`, which is in-app + **email** (`:462-467`).
  - Every other project manager (coordinators) gets an in-app note only (`:452-461`).
- **So a damaged GRN that raises a new Issue already emails every active SCM user and the PM.** A new GRN email to SCM + PM would be a second email to the same people about the same submission.

**GRN attachment storage and signed URLs:**
- Path: `build_design_path(project.project_id, DC_DAMAGE_PHOTO_PATH_KIND, file_obj.name)` (`delivery_storage.py:219`) → `{project_id}/grn-damage/{uuid4-hex}.{ext}` (`design_storage.py:133`, `:145`).
- Bucket: `SUPABASE_DELIVERY_BUCKET` (`settings.py:42`, default `''`), private.
- Signed URL: `damage_photo_url(photo, expires_in=DC_DOCUMENT_LINK_SECONDS)` (`delivery_storage.py:255`), where `DC_DOCUMENT_LINK_SECONDS = 300` (`:37`). It is minted **on click** by `dc_damage_photo_open` (`views.py:14386`, URL `projects/<project_id>/delivery-challans/<dc_id>/damage-photos/<photo_id>/`, `urls.py:623`), which redirects to the signed URL. A signed URL placed in an email would be dead in 5 minutes. The link has to be the app route.

### T5 — Recipients for the GRN email

**SCM:** identified by role string through `permissions.active_scm_users()` (`permissions.py:3638-3640`: `_active_profiles().filter(role='SCM')`). The damage flow already uses it.
```
  active SCM=3: subhash, mahesh, ishmeet
```

**PM:**
- `project.assigned_pm` is the PM of record, written only by `site_pm.change_site_pm()` (`grn_damage.py:137-138` docstring).
- Helpers: `permissions.site_assigned_pm(project)` (`permissions.py:1679-1692`) is the PM alone, None if deactivated, "coordinators are left out on purpose". `permissions.project_managers(project)` (`:1657-1676`) is the PM plus every active coordinator, deduplicated. `grn_damage.damage_issue_assignee` is a third copy of the PM-alone rule.
- **Coordinators elsewhere:** `issue_raised`, `issue_resolved` and `boq_acknowledged` all use "project managers (PM and coordinators)" (registry `recipients_rule` texts above). The attendance alert uses the PM alone.
```
  OPEX sites with DC: assigned_pm null=0 with coordinators=9
```

**Finance "Santosh" and "Shankey". Raw output:**
```
  active Finance=5
    Finance: username=santosh name='Santosh Thakur' profile_active=True login_active=True email_present=True email_domain=horizonrenewablepower.com email_notifications=True
    Finance: username=rekha name='Rekha Garg' profile_active=True login_active=True email_present=True email_domain=horizonrenewablepower.com email_notifications=False
    Finance: username=payal name='Payal Payal' profile_active=True login_active=True email_present=True email_domain=horizonrenewablepower.com email_notifications=False
    Finance: username=neetu name='Neetu Bakshi' profile_active=True login_active=True email_present=True email_domain=horizonrenewablepower.com email_notifications=False
    Finance: username=shankey name='Shankey Garg' profile_active=True login_active=True email_present=True email_domain=horizonrenewablepower.com email_notifications=False
  match 'santosh': username=santosh name='Santosh Thakur' role='Finance' profile_active=True login_active=True email='santosh@horizonrenewablepower.com'
  match 'shank': username=shankey name='Shankey Garg' role='Finance' profile_active=True login_active=True email='shankey@horizonrenewablepower.com'
```
**Shankey's `email_notifications` is False.** `send_notification` would log `'skipped', 'User preference off'` (`notifications.py:106-108`), and Shankey would never receive the GRN email unless that preference is turned on. Rekha, Payal and Neetu are off as well.

**How Santosh is hardcoded today:**
- `projects/utils.py:1165` `RESIDENTIAL_FINANCE_ASSIGNEE_EMAIL = 'santosh@horizonrenewablepower.com'`.
- Read at `utils.py:1849-1853`:
  ```python
  finance_assignee = (
      UserProfile.objects
      .filter(user__email=RESIDENTIAL_FINANCE_ASSIGNEE_EMAIL)
      .first()
  )
  ```
  then `assign_tasks_to(... INVOICE_TASK_NAMES + RESIDENTIAL_FINANCE_CONFIRMATION_TASK_NAMES ...)` (`:1861-1866`). Activation raises if the account is missing (`:1854-1860`).
- This is **task assignment** at Residential activation, not a notification recipient list. Santosh hears about invoices only through the ordinary assignment notices. Also read by `_walkthrough_support.py:89`.

**`solarpms/settings.py:175`** is a comment: "Mirrors the Santosh-for-Finance hardcoding pattern." The values it introduces:
```python
ADMIN_DIGEST_EMAIL = config('ADMIN_DIGEST_EMAIL', default='REPLACE_WITH_ACTUAL_ADMIN_EMAIL')   # :180
HR_DIGEST_EMAIL    = config('HR_DIGEST_EMAIL',    default='REPLACE_WITH_ACTUAL_HR_EMAIL')      # :181
```
- These are fixed addresses, not UserProfiles, read only by `send_eod_digest` (`send_eod_digest.py:509-510`, `:527-528`) for the company-wide aggregate digest, sent with `send_aggregate_email`.
- **They are overridden at the bottom of the file:** `settings.py:240-241`:
  ```python
  ADMIN_DIGEST_EMAIL = 'smzk07@gmail.com'
  HR_DIGEST_EMAIL = 'shweta@horizonrenewablepower.com'
  ```
  These lines were added in `c6cc2ce`. They re-assign after `config()`, so the env vars have no effect (secondary finding).

**A configurable recipient list anywhere?** None:
- `notify_registry.py` has no recipient field. `recipients_rule` is "documentation shown to Admin. It is not executed: each call site resolves its recipients" (`notify_registry.py:10-11`), and the module docstring says Admin "may NOT … change who receives it" (`:6-8`).
- `NotificationEventSetting` holds only `is_active` per channel.
- The only named-recipient precedents are the hardcoded email constant (`utils.py:1165`) and settings addresses (`settings.py:180-181`, `:240-241`).
- **No capability flag on `UserProfile`** names a Finance sub-group. The flags seen are `is_design_head`, `is_design_qc`, `is_warehouse_keeper`, `whatsapp_notifications`, `email_notifications`.

**Can Finance open the DC / GRN pages?**
- **DC detail page: NO.** `views.py:14116-14118`:
  ```python
  # Only SCM, PM, SE, Admin can view DC pages
  if profile.role not in ('SCM', 'PM', 'Project Coordinator', 'Site Engineer', 'Admin'):
      return HttpResponseForbidden()
  ```
  This runs before the scope check, so Finance gets 403 even though `user_can_view_project` is True for Finance (`PORTFOLIO_VIEW_ROLES = frozenset({'CEO', 'Finance', 'SCM', 'Admin'})`, `permissions.py:36`).
- **Challan documents: NO.** `user_can_open_dc_document` requires `DC_DETAIL_ROLES` (`permissions.py:2735`, `:2754-2756`). Its docstring: "CEO, System Admin, Finance, Design and BD cannot open the detail page today".
- **Damage photos: YES.** `user_can_open_damage_photo` admits any project viewer for a live photo (`permissions.py:2798-2816`; "NOT user_can_open_dc_document(): that one is the DC page's role list, which would refuse Finance or the CEO a link the issue page shows").
- So a GRN email "View DC" button would 403 for Santosh and Shankey. Damage-photo links would work.

### T6 — Email content constraints

- **Attachments: not supported.** The ZeptoMail payload is `from`, `to`, `subject`, `textbody`, optional `reply_to` and `htmlbody` (`notifications.py:205-221`), with no `attachments` key. `send_notification` has no attachment parameter (signature in T3). There is no size handling because nothing is attached.
- **Tables and link lists in the brand layout:** `base_email.html` (55 lines) uses presentation tables for the frame only (`:22`, `:25`) and draws **one** button (`:36-42`). The body arrives as `{{ body_html }}` (`:33`), produced by `notify.render_body_html`. That emits only `<p>`, `<br>` and `<strong>`, and **escapes every value** (`notify.py:116-141`: "Every character in the result is therefore either escaped input or a tag written here"). **A material-lines table or a list of `<a>` links cannot be passed through `notify()` today.** A code-composed variable holding HTML would be escaped and shown as text. A plain URL per line would render as text, and some mail clients auto-link it.
  - The precedent for richer email is code-owned templates sent directly through `send_notification(html_message=...)`, outside `notify()`: `projects/email/morning_task_report.html` (`send_morning_task_report.py:204-207`). Those have no Admin wording or pause control.
- **Typical GRN attachment sizes, local. Raw output:**
  ```
  == T6: attachment sizes ==
    DeliveryChallanAttachment: {'n': 0, 'mx': None, 'av': None, 'mn': None, 'tot': None} live=0
    DCLineDamagePhoto: {'n': 0, 'mx': None, 'av': None, 'mn': None, 'tot': None} live=0
  ```
  There are no rows locally. The only bounds come from code: ≤20 MB per file (`delivery_storage.py:40`), ≤5 live photos per line (`:190`), ≤10 challan documents (`:43`). The browser re-encodes photos to JPEG before upload (`delivery_storage.py:183` comment). **Typical sizes are UNVERIFIED** (no data).

### T7 — Vendor / PO-PI link, GRN reference, per-line quantities

- **DC → vendor:** `DeliveryChallan.vendor` → `Vendor` (FK, SET_NULL, nullable, `models.py:1618-1621`). `Vendor` holds `name, contact_person, phone, email, gst_number, …` (`models.py:905-924`).
- **DC → vendor order / PO-PI record: none.** The only reference is the free-text `po_number` (`models.py:1622-1624`, "do NOT add PO model today"). `VendorOrder` has its own `po_number` / `pi_number` text (`models.py:2983+`, fields at +25/+26) and `vendor` FK, and covers sites via `VendorOrderSite(order, project)` (`models.py:3123+`). **There is no FK in either direction between `DeliveryChallan`/`DCLineItem` and `VendorOrder`/`VendorOrderLine`.** The grep of `DeliveryChallan|DCLineItem` across `models.py` shows references only from `Issue.delivery_challan` (`:1514`), `PhaseRuleOverride.delivery_challan` (`:7851`) and the DC's own children. A link could only be inferred by text match: same `po_number` + same vendor + an order covering the site.
- **GRN number / reference: none.** **Receipt date:** `DCLineItem.grn_date` (DateField, per line), set to `date.today()` at `views.py:14594` / `:14725`. That is distinct from `created_at` (which is the DC's). `date.today()` is the server OS date, not `timezone.localdate()`. If the Railway container runs on UTC, a GRN between 00:00 and 05:30 IST would be dated the previous day. **UNVERIFIED** (container TZ not checked; `settings.py:135` `TIME_ZONE = 'Asia/Kolkata'` does not affect `date.today()`).
- **Per line, from stored fields:**
  - DC qty = `ordered_quantity`
  - received = `received_quantity` (NULL = not yet received)
  - damaged = `damaged_quantity`, a part of received, not in addition: "`damaged_quantity` is a part of `received_quantity`, never in addition to it" (`models.py:2040`)
  - **short** = `ordered_quantity - received_quantity`, computed. `_dc_item_severity` uses exactly that comparison (`models.py:2064-2067`).
  - good = `received_quantity - damaged_quantity`, computed.
  - One caveat: `damaged_quantity` is a whole number and is clamped to `int(received_qty)` (`views.py:14514`).
- **Local data. Raw output:**
  ```
  == T7: vendor / PO-PI resolvability ==
    DCs=19 vendor set=19 po_number non-blank=0 both=0
    VendorOrders=0 distinct PO/PI numbers=0
    DCs whose po_number text equals a VendorOrder PO/PI number=0
    ... and same vendor + order covers the DC's site=0
    sample po_number values: []
  ```
  All 19 DCs have a vendor, **none** has a PO reference, and there are no VendorOrders locally.

---

## 3. Open questions

| Open question | What the code currently allows | Risk if left as is |
|---|---|---|
| Which SEs get the DC WhatsApp? | No helper exists. The only working relationship is "holds any task on the site" (`permissions.py:325`), including Done tasks. | UKRU001 would message 7 SEs, 2 of them with only Done tasks there. A narrower rule ("open SE task") would disagree with who can actually confirm the GRN (`user_works_on_site`). |
| DC created on a site with no SE | Allowed (`views.py:13765-14102`, no SE check). | A silent no-send. SCM is not told nobody was messaged unless the build says so. |
| Delivery date | `expected_delivery_date` is optional (nullable, `models.py:1647`). | A WhatsApp template with a date slot needs a fallback value, or the send fails or reads oddly. |
| DC edited after the SE was told | Header, lines, quantities and dates are editable (`dc_edit.py:81`, `:194-196`), with no notification. | The SE acts on a stale WhatsApp (wrong quantity or date). |
| What is "the GRN confirmed" event? | Repeated partial confirms, re-confirms until Received, empty confirms, and SCM overrides at any time (T4). | Several emails per DC, including one for an empty submission. An override sends nothing unless it is wired in too. |
| "Photos/documents the SE attached at GRN" | Only damage photos, only on damaged lines (`grn_damage.py:221-224`). | The email will have nothing attached on a clean receipt, which the spec may not expect. |
| Damage GRN → SCM emailed twice | The damage flow already emails every active SCM user plus the PM via `issue_raised` when it raises a new Issue (`grn_damage.py:448-467`). | Duplicate emails to SCM and the PM on the same submission. |
| Finance can open the links? | DC detail is 403 for Finance (`views.py:14117`). Challan documents are refused. Damage photos are allowed. | The email's main button fails for Santosh and Shankey. |
| Shankey actually receives it | `shankey.email_notifications=False` locally. `send_notification` skips it (`notifications.py:106-108`). | The named recipient silently never gets the email (logged as skipped). |
| Named Finance recipients | No configurable list. The precedent is a hardcoded email constant (`utils.py:1165`) or a settings address. | A hardcode needs a code change and deploy when staff change. A role-wide list would include 5 Finance users, not 2. |
| PM: PM alone or PM + coordinators? | Both helpers exist (`site_assigned_pm`, `project_managers`). 9 of 10 OPEX DC sites have coordinators. | Coordinators are either missed or included, against the spec's "PM". |
| Material table and links in the email | `notify()` escapes values and allows only `<p>/<br>/<strong>` (`notify.py:116-141`). There are no attachments. | Either the table renders as text, or the event leaves `notify()` and loses the Admin pause and wording control. |
| Signed photo links in the email | The signer gives 300 s links (`delivery_storage.py:37`). | Use the app route `dc_damage_photo_open`, or the links are dead within minutes. |
| WhatsApp pause for the new event | Nothing in the registry pauses WhatsApp (T3b). | The DC WhatsApp is either unpausable or needs the T3b control built first. Without it the locked activation rule cannot be met. |
| Request latency | Sends run in-request after commit, 10 s timeout each (T3). No gunicorn `--timeout` is set. | A slow provider delays or kills the GRN/DC response after the data is saved, and remaining recipients are skipped. |
| Concurrent DC edit and GRN | GRN saves stale challan/line copies after `lock_challan` (`views.py:14624-14639`, SECONDARY_FINDINGS DC-B3 entry, still present and now pushed). | A GRN email could describe a line set that a just-committed edit had changed; the edit itself is reverted. |
| GRN date | `date.today()` (`views.py:14594`). | Possibly the previous day for receipts after midnight IST (UNVERIFIED). |
| Vendor / PO in the email | Vendor FK is always set locally. PO is free text, blank on all 19, with no order link. | A "PO reference" field in the email would always be blank today. |

---

## 4. Files a build would need to touch (no code)

- `projects/notify_registry.py`: two new events (DC → SE WhatsApp; GRN → email), `default_state='paused'`.
- `projects/notify.py`: only if the WhatsApp leg or a table-capable body goes through `notify()` (T3b step 2; T6).
- `projects/notify_admin.py`: only if WhatsApp activation follows the locked rule (a WhatsApp test-send and Approved/mapped gate in `send_test` / `set_state`).
- `projects/notify_views.py` and `projects/templates/projects/notify/…`: only if the WhatsApp row needs the `wa_*` mapping fields shown or edited (UNVERIFIED which templates; not opened).
- `projects/views.py`: `_create_delivery_challan_rows` / `create_delivery_challan` (DC send after commit); `confirm_grn` (and `override_grn` if overrides notify); possibly `delivery_challan_detail` if Finance gains access.
- `projects/permissions.py`: a site-SE recipient helper; a named-Finance recipient helper; possibly a DC-detail access helper replacing the inline tuple at `views.py:14117`.
- A new sender module (pattern: `projects/approval_notices.py`, `projects/grn_damage.py` notification section), e.g. `projects/delivery_notices.py`.
- `projects/grn_damage.py`: only if de-duplication against the damage `issue_raised` email is done there.
- `projects/templates/projects/email/base_email.html` / `.txt`, or new code-owned email templates for the GRN table.
- `solarpms/settings.py` or `projects/utils.py`: wherever the named Finance recipients live, if not a DB setting.
- Tests: new test files for both events; `projects/tests_notify.py` and `projects/tests_notify_migration_c.py` (registry pins).
- Possibly `projects/models.py` + a migration, only if a recipient list or GRN reference is stored. Not needed for the minimum.
- Interakt console: new WhatsApp template (external, not a repo file).

---

## 5. Contradictions with the locked rules in Context

1. **"All sends go through `send_notification()`"** holds at the bottom layer: every WhatsApp and email send above ends in it. **But WhatsApp never goes through `notify()`/the registry** (`notify.py:245-284` has no WhatsApp leg). The ten WhatsApp call sites in T3b each map their own template. Per the corrected Context (`notify()` is the entry point), WhatsApp has no entry point today.
2. **"Authority checks only via `permissions.py` helpers, never role-string comparisons":**
   - `delivery_challan_detail` gates on an inline role tuple, `views.py:14117` `if profile.role not in ('SCM', 'PM', 'Project Coordinator', 'Site Engineer', 'Admin'):`. That duplicates `permissions.DC_DETAIL_ROLES` (`permissions.py:2735`); the DC-B2 SECONDARY_FINDINGS already records the copy.
   - `views.py:14197` `'can_confirm_grn': (profile.role == 'Site Engineer' and user_works_on_site(...))`.
   - The DC/GRN endpoints gate on `@role_required(['SCM'])` (`views.py:13764`, before `override_grn` `:14666`) and `@role_required(['Site Engineer'])` (before `confirm_grn` `:14541`).
3. **"New events start paused; WhatsApp templates activate only when Approved in Interakt, mapped and test-sent":**
   - Pausing at start is supported for email (`default_state='paused'`, precedent `notify_registry.py:1251`).
   - For WhatsApp none of this exists. No event can declare WhatsApp without a send path that honours the switch. `send_test` refuses WhatsApp (`notify_admin.py:219-220`). The `wa_approval_status` / `wa_template_name` / `wa_slots` fields exist (`models.py:8016-8019`) but nothing reads them.
   - Most existing events default to `'active'`.
4. **"Interakt success = any 2xx"** matches (`notifications.py:179`). ZeptoMail is stricter, `if resp.status_code in (200, 201):` (`:233`). The rule only names Interakt, so this is noted, not a contradiction.
5. **"`template_params[0]` = header, `[1:]` = body"** matches (`notifications.py:163-164`).
6. **"Status transitions in the view layer only"** holds. `recalculate_dc_status` is a module function in `models.py:2071`, called only from views and `dc_edit` (inside `save_challan_edit`). No signal or `save()` override changes DC status.
7. **"DC attachments must never show prices to SEs"** holds by attestation only: the uploader's tick plus `CHECK no_prices_confirmed` (`models.py:1811-1813`), with withdrawal as the remedy. Nothing inspects content. DC lines carry no price fields, so a WhatsApp built from lines cannot leak a price.

---

## 6. Secondary findings

**6** new entries appended to `SECONDARY_FINDINGS.md` under "From NTF-DCG-A". Nothing was acted on.

---

## 7. Commit message

None. Read-only audit.
