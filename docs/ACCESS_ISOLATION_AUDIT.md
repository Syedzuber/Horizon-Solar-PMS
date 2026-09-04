# Access Isolation Audit — prompt A-0.2

**Read-and-report session. No `.py`, `.html`, `.css` or `.js` file was modified. No migration
was created. `manage.py` was not run.** Everything below is read from source at commit
`bb0f905`, with the working tree as delivered (`PROJECT_CONTEXT.md` modified;
`EXECUTION_MODULE_DEFERRED.md` and `docs/execution-model.md` untracked).

Prepared for prompt 0.2 (`docs/execution-model.md` §2 D-4). Nothing here is a plan of record.

---

## 0. Stop conditions

Four of the five are clear: no source file needed to change, no migration was needed, and
every file named in the prompt exists.

**The fifth is ambiguous and is reported rather than acted on.** `docs/execution-model.md`
exists and its content is unambiguously the document the prompt describes — §2 D-4 states the
access-isolation prerequisite, §1 the vocabulary, §3 the standing rules. But the file
**carries no version number anywhere**. Its only provenance line is:

> Last updated: 23 Aug 2026 · covers prompts 0.1 through 5.4 of the execution build plan.

There is no `1.1`, no `version:` key, no version-history section. The stop condition
*"is not version 1.1"* therefore cannot be evaluated as written. The audit proceeded, because
the document's content is the right document and halting a whole session over a missing string
would have delivered nothing. **If the intent is that a version stamp must exist before 0.2
runs, that stamp needs to be added and this audit re-read against it.**

---

## Task A — Endpoint inventory

### A.0 How "permission check applied" is classified

Three classes, and the distinction is the whole point of the exercise:

| Class | Meaning |
|---|---|
| **SCOPE** | A check that ties the actor to **this** object — `user_can_view_project()`, `user_can_manage_project()`, `user_can_view_project_boq()`, `user_is_assigned_designer()`, `_pm_owns_project()`, `task.assigned_to == profile`, `uploaded_by == profile`. |
| **ROLE** | A role gate and nothing more — `@role_required([...])` or an inline `profile.role != 'X'` test. Passes every holder of that role on **every** project. |
| **none** | `@login_required` only, or a check that fires for one role and lets the other nine through. |

The PM-isolation idiom that appears eighteen times —

```python
if profile.role == 'PM' and not user_can_manage_project(request.user, project):
    raise Http404
```

— is classified **none**, not SCOPE. It is a scope check *for PMs* and a no-op for the other
nine values in `UserProfile.ROLE_CHOICES`. Prompt 0.1 established that PM, Project Coordinator,
Site Engineer and Design are all scoped **on their dashboards**; this idiom is why that scoping
does not survive a typed URL.

### A.1 Counts

**117 endpoints in scope — 19 list/dashboard, 98 detail.**

Detail endpoints (98):

| Class | Count | of which writes |
|---|---:|---:|
| **SCOPE** — tied to this object | 46 | 32 |
| **ROLE** — role gate, no object scope | 34 | 22 |
| **none** — login only, or PM-only isolation | **18** | **13** |

List / dashboard endpoints (19):

| Class | Count |
|---|---:|
| Carries a per-user term in its queryset | 6 |
| `@role_required` but portfolio-wide queryset | 9 |
| **No role gate at all** | **4** |

**The headline: 18 detail endpoints have no effective object-level check, and 13 of those are
writes.** The remaining 5 are reads.

Separately, **34 detail endpoints are role-gated with no object scope**. Most are deliberate
today (SCM and Finance are portfolio-wide by remit, Admin is unrestricted, Design Head
authority is portfolio-wide by design). Three are not: `confirm_grn`, `set_milestone_amounts`
and `payment_request_detail` — see A.4.

### A.2 List / dashboard endpoints (19)

| URL name | View | R/W | Objects listed | Permission check | Roles that pass today | `is_deleted` filtered? |
|---|---|---|---|---|---|---|
| `landing` | [views.py:261](projects/views.py#L261) | R | Project (counts) | ROLE `@role_required(LANDING_ROLES)` | CEO, Finance, SCM | Yes [:271](projects/views.py#L271) |
| `dashboard_pm` | [views.py:454](projects/views.py#L454) | R | Project, Task | ROLE + **per-user term** `Q(assigned_pm=me) \| Q(coordinators=me)` [:469](projects/views.py#L469) | PM, Project Coordinator | Card query yes [:519](projects/views.py#L519); **count queries no** [:481-508](projects/views.py#L481-L508) |
| `dashboard_site_engineer` | [views.py:738](projects/views.py#L738) | R | Project, Task, DeliveryChallan, Issue | ROLE + **per-user term** `phases__tasks__assigned_to=me` [:759](projects/views.py#L759) | Site Engineer | Yes [:757](projects/views.py#L757) |
| `dashboard_design` | [views.py:906](projects/views.py#L906) | R | Project, BOQ | ROLE + **per-user term** `assigned_design=me OR phases__tasks__assigned_to=me` [:926](projects/views.py#L926) | Design | Yes [:929](projects/views.py#L929) |
| `dashboard_finance` | [views.py:1066](projects/views.py#L1066) | R | Project, PaymentMilestone, PaymentRequest | ROLE only — **portfolio-wide, no per-user term** [:1082](projects/views.py#L1082) | Finance | Projects yes; **milestone/PR aggregates no** [:1096-1112](projects/views.py#L1096-L1112) |
| `dashboard_scm` | [views.py:1199](projects/views.py#L1199) | R | Project, BOQ, DeliveryChallan, Issue | ROLE only — **portfolio-wide, no per-user term** [:1237](projects/views.py#L1237) | SCM | Projects yes; **BOQ/DC/Issue queries no** |
| `dashboard_ceo` | [views.py:2211](projects/views.py#L2211) | R | Project, Task, Issue | **none — `@login_required` only** | **every authenticated user** | Yes, inside `_get_ceo_dashboard_context` |
| `dashboard_bd` | [views.py:6473](projects/views.py#L6473) | R | Project, Task, Issue, PaymentMilestone | ROLE only — **portfolio-wide, no per-user term** [:6487](projects/views.py#L6487) | BD | Yes [:6487](projects/views.py#L6487) |
| `tasks_due_today` / `tasks_due_soon` / `tasks_overdue` (3 URLs) | `tasks_drill_down` [views.py:381](projects/views.py#L381) | R | Task, Project | **none** — in-body scoping for PM/PC/Design/SE only; **SCM, Finance, BD, CEO, Admin, System Admin fall through to every active project** [:397-415](projects/views.py#L397-L415) | every authenticated user | Yes [:392](projects/views.py#L392) |
| `project_list` | [views.py:2372](projects/views.py#L2372) | R | — (redirect only) | ROLE `['PM','Admin','CEO']` | PM, Admin, CEO | n/a |
| `program_list` | [views.py:2790](projects/views.py#L2790) | R | Program | ROLE + `_can_access_program` filter **for PM only**; Admin/CEO unfiltered [:2803](projects/views.py#L2803) | Admin, PM, CEO | Yes [:2799](projects/views.py#L2799) |
| `admin_project_list` | [views.py:9674](projects/views.py#L9674) | R | Project | ROLE `['Admin']` | Admin | Yes [:9678](projects/views.py#L9678) |
| `subadmin_projects` | [views.py:10399](projects/views.py#L10399) | R/W | Project | ROLE `@system_admin_required` | System Admin | Yes [:10409](projects/views.py#L10409) |
| `design_my_sites` | [design_views.py:360](projects/design_views.py#L360) | R | DesignAssignment | **per-user term** `assigned_to=me` [:366](projects/design_views.py#L366) | anyone with a profile — returns only their own rows | Yes [:366](projects/design_views.py#L366) |
| `design_qc_queue` | [design_views.py:3421](projects/design_views.py#L3421) | R | DesignAssignment | SCOPE `user_can_view_qc_queue()` + `_qc_scope()` per-user filter [:3450](projects/design_views.py#L3450) | Design QC, Design Head, deputy, assigned reviewer | Yes [:3466](projects/design_views.py#L3466) |
| `my_documents` | [views.py:9033](projects/views.py#L9033) | R | ProjectDocument, TaskAttachment, BOQ | **per-user term** `uploaded_by=me` / `submitted_by=me` [:9039-9048](projects/views.py#L9039-L9048) | any authenticated user, own rows only | Documents yes; **project `is_deleted` not filtered** |
| `design_quality_analytics` (no pk) | [design_views.py:4721](projects/design_views.py#L4721) | R | DesignAssignment aggregates | `user_is_design_head()` [:4728](projects/design_views.py#L4728) | Design Head flag only — **the deputy is deliberately refused** | Yes |

`vendor_list`, `portal_activity_log`, `admin_whatsapp_log`, `admin_audit_log` and
`notifications` list none of the ten named models and are out of scope.

### A.3 Detail endpoints — SCOPE (46)

Correct today; listed for completeness rather than tabulated.

**Project / BOQ (19):** `project_edit`, `project_field_edit`, `project_activate`,
`project_recalculate_dates`, `enable_cascade_scheduling`, `task_add`, `task_assign`,
`task_detail_status_update`, `assign_coordinators`, `boq_detail`, `opex_boq_entry`,
`opex_boq_download`, `opex_boq_upload`, `boq_submit`, `boq_request_revision`, `boq_history`,
`milestone_create`, `delete_project_document`, `delete_task_attachment`.

**Program (5):** `program_detail`, `program_edit`, `opex_site_create`,
`opex_site_bulk_upload`, `opex_site_bulk_template` — all `_can_access_program()`
[views.py:2773](projects/views.py#L2773).

**Issue (2):** `close_issue`, `reopen_issue` — both `_is_project_pm()`
[views.py:7775](projects/views.py#L7775).

**Design (20):** `design_survey_download`, `design_due_date_propose`, `design_mark_blocked`,
`design_site_workspace`, `design_head_review`, `design_arka_submit`, `design_arka_approve`,
`design_arka_reject`, `design_arka_head_approve`, `design_arka_head_reject`,
`design_artifact_upload`, `design_file_download`, `design_boq_complete`, `design_qc_start`,
`design_qc_pass`, `design_qc_fail`, `design_head_qc_pass`, `design_head_qc_fail`,
`design_qc_review`, `design_change_request_form` / `design_change_request`.

**The design module is the one part of this codebase where object-level scoping is
consistent.** Every endpoint resolves the site through `_opex_site()`
[design_views.py:218](projects/design_views.py#L218) — which filters `is_deleted=False` and
refuses non-OPEX — and then asks a named `permissions.py` helper. It is the model 0.2 should
copy, not a place 0.2 needs to touch.

### A.4 Detail endpoints — ROLE only (34): the three that are not deliberate

| URL name | View | R/W | Object | Check | Who passes | Problem |
|---|---|---|---|---|---|---|
| `confirm_grn` | [views.py:8815](projects/views.py#L8815) | **W** | DeliveryChallan | `@role_required(['Site Engineer'])` [:8814](projects/views.py#L8814) | **every Site Engineer** | **No project scope at all.** Any SE can record a GRN — received and damaged quantities, notes, `grn_confirmed_by` stamp, and the DC status recalculation — on any challan on any project. This is D-4's "a Site Engineer into another engineer's site", literally. |
| `set_milestone_amounts` | [views.py:6642](projects/views.py#L6642) | **W** | PaymentMilestone | `@role_required(['BD','PM'])` [:6641](projects/views.py#L6641) | **every BD and every PM** | No `_pm_owns_project` call anywhere in the view. A PM can rewrite M1/M2/M3 agreed amounts on another PM's project. |
| `payment_request_detail` | [views.py:9102](projects/views.py#L9102) | R | PaymentRequest | inline `role in ('SCM','Finance','PM','Admin')` [:9107](projects/views.py#L9107) | every SCM, Finance, PM, Admin | Any PM reads any project's vendor invoice metadata and document URL. |

The other 31 are role gates that are correct **under today's policy**, and become findings only
if 0.2 narrows Finance or SCM:

- **Portfolio-by-remit today:** `boq_acknowledge`, `raise_payment_request`, `override_grn`,
  `create_delivery_challan` (SCM); `milestone_invoice`, `milestone_receive`,
  `confirm_payment_request` (Finance); `delivery_challan_detail` (role allowlist + PM isolation).
- **Admin-unrestricted by design:** `project_delete`, `program_delete`.
- **Design Head authority, portfolio-wide by design (15):** `design_head_sites`,
  `design_bulk_allocate`, `design_tender_dashboard`, `design_qc_dashboard`,
  `design_quality_analytics_tender`, `design_survey_upload`, `design_survey_link_set`,
  `design_allocate`, `design_assign_qc`, `design_due_date_approve`, `design_due_date_reject`,
  `design_due_date_change`, `design_change_request_accept`, `design_change_request_reject`,
  `task_assign_design_head`.
- **SCM-only group formation (settled decision 3, Part 6):** `site_group_list`,
  `site_group_create`, `site_group_detail`, `site_group_add_sites`, `site_group_remove_site`,
  `site_group_lock`.

### A.5 Detail endpoints — none (18). This is the finding.

| URL name | View | R/W | Object resolved | Permission check applied | Roles that pass today | `is_deleted` filtered? |
|---|---|---|---|---|---|---|
| `project_detail` | [views.py:2419](projects/views.py#L2419) | R | Project | **none** (redirects to `project_overview`) | every authenticated user | n/a |
| `project_overview` | [views.py:6718](projects/views.py#L6718) | R (+ gated POST) | Project, Task, Issue, ProjectDocument, PaymentMilestone, PaymentRequest, DeliveryChallan | **none** — PM-only isolation [:6734](projects/views.py#L6734) | **every role except PM** | **No** [:6724](projects/views.py#L6724) |
| `task_detail` | [views.py:7335](projects/views.py#L7335) | R | Project, Task, Issue, TaskAttachment, Comment | **none** — PM-only isolation [:7345](projects/views.py#L7345) | every role except PM | **No** |
| `project_timeline` | [views.py:8489](projects/views.py#L8489) | R | Project, ActivityLog | **none** — PM-only isolation [:8497](projects/views.py#L8497) | every role except PM | **No** |
| `issue_detail` | [views.py:8084](projects/views.py#L8084) | R | Issue, Project, Comment | **none** — PM-only isolation [:8093](projects/views.py#L8093) | every role except PM | **No** |
| `task_status_update` | [views.py:3564](projects/views.py#L3564) | **W** | Task | **none for role-matchers** — `role != task.assigned_role and not is_pm` [:3602](projects/views.py#L3602) | **anyone whose role matches the task's `assigned_role`, on any project** | **No** |
| `task_set_due_date` | [views.py:4117](projects/views.py#L4117) | **W** | Task | **none for role-matchers** [:4137](projects/views.py#L4137) | same | **No** |
| `checklist_item_complete` | [views.py:7677](projects/views.py#L7677) | **W** | Task, ChecklistItemCompletion | **none for role-matchers** — `_user_can_complete_checklist_item` [:2349](projects/views.py#L2349) | same | **No** |
| `upload_project_document` | [views.py:7388](projects/views.py#L7388) | **W** | ProjectDocument | **none** — PM-only isolation [:7401](projects/views.py#L7401) | every role except PM | **No** |
| `upload_task_attachment` | [views.py:7522](projects/views.py#L7522) | **W** | TaskAttachment | **none** — PM-only isolation [:7535](projects/views.py#L7535) | every role except PM | **No** |
| `create_project_issue` | [views.py:7788](projects/views.py#L7788) | **W** | Issue | **none** — PM-only isolation [:7799](projects/views.py#L7799) | every role except PM | **No** |
| `create_task_issue` | [views.py:7882](projects/views.py#L7882) | **W** | Issue | **none** — PM-only isolation [:7893](projects/views.py#L7893) | every role except PM | **No** |
| `create_delivery_issue` | [views.py:7978](projects/views.py#L7978) | **W** | Issue, DeliveryChallan | **none** — PM-only isolation [:7991](projects/views.py#L7991) | every role except PM | **No** |
| `update_issue_status` | [views.py:8119](projects/views.py#L8119) | **W** | Issue | **none** — PM-only isolation [:8132](projects/views.py#L8132) | every role except PM | **No** |
| `resolve_issue` | [views.py:8158](projects/views.py#L8158) | **W** | Issue | **none** — PM-only isolation [:8172](projects/views.py#L8172) | every role except PM | **No** |
| `assign_issue` | [views.py:8295](projects/views.py#L8295) | **W** | Issue | **none** — PM-only isolation [:8308](projects/views.py#L8308) | every role except PM | **No** |
| `create_task_comment` | [views.py:8340](projects/views.py#L8340) | **W** | Comment | **none** — PM-only isolation [:8350](projects/views.py#L8350) | every role except PM | **No** |
| `create_issue_comment` | [views.py:8401](projects/views.py#L8401) | **W** | Comment | **none** — PM-only isolation [:8411](projects/views.py#L8411) | every role except PM | **No** |

Not one of the eighteen filters `is_deleted`. See Task G.

---

## Task B — the Issue-endpoint claim: **substantially TRUE, with one correction**

### B.1 Verdict

The earlier claim — *"Issue endpoints have effectively no role gating"* — is **correct for six
of the eight Issue endpoints and wrong for two.** Close and reopen ARE guarded. Everything
else is not.

The full gate on `create_project_issue`, `create_task_issue`, `create_delivery_issue`,
`issue_detail`, `update_issue_status`, `resolve_issue`, `assign_issue` and
`create_issue_comment` is `@login_required` plus one identical five-line stanza. Quoted in
full from `resolve_issue` [views.py:8168-8173](projects/views.py#L8168-L8173):

```python
issue   = get_object_or_404(Issue, pk=issue_id)
project = issue.project
profile = request.user.profile

if profile.role == 'PM' and not user_can_manage_project(request.user, project):
    raise Http404
```

`issue_id` is a bare integer PK. There is no project in the URL, no `is_deleted` term, no
membership test, and no role allowlist. **For any user whose `UserProfile.role` is not the
literal string `'PM'`, the stanza evaluates to `False and ...` and the view proceeds.**

`close_issue` [views.py:8241](projects/views.py#L8241) and `reopen_issue`
[views.py:8274](projects/views.py#L8274) are the exception, and they use a different helper:

```python
if not _is_project_pm(profile, project):
    return HttpResponseForbidden('Only the project PM can close issues.')
```

with [views.py:7785](projects/views.py#L7785):

```python
return profile.role in ('PM', 'Project Coordinator') and user_can_manage_project(profile.user, project)
```

That is a genuine object-level scope check on both role and ownership.

### B.2 Answering the specific question: can any authenticated user close an issue on any project?

**No — close is properly guarded.** But that is the only thing that is. Endpoint by endpoint,
for a logged-in user with `role='Site Engineer'` and no relationship of any kind to project X:

| Endpoint | Gate | Can our unrelated SE do it on project X? |
|---|---|---|
| `create_project_issue` | PM-only stanza [:7799](projects/views.py#L7799) | **Yes** — raise an issue on X, set severity, due date, and assign it to any `UserProfile` in the system |
| `create_task_issue` | PM-only stanza [:7893](projects/views.py#L7893) | **Yes** |
| `create_delivery_issue` | PM-only stanza [:7991](projects/views.py#L7991) | **Yes** — DC is cross-checked against the URL project [:7995-7997](projects/views.py#L7995-L7997), but the project itself is unguarded |
| `issue_detail` (GET) | PM-only stanza [:8093](projects/views.py#L8093) | **Yes** — reads title, description, severity, resolution note, every comment, and `all_profiles` (every active user in the system, [:8098](projects/views.py#L8098)) |
| `update_issue_status` | PM-only stanza [:8132](projects/views.py#L8132) | **Yes** — Open → In Progress |
| `resolve_issue` | PM-only stanza [:8172](projects/views.py#L8172) | **Yes** — In Progress → Resolved, writes `resolution_note` and `resolved_at`, and **fires WhatsApp + email to every project manager, the assignee and the raiser** [:8206-8218](projects/views.py#L8206-L8218) |
| `assign_issue` | PM-only stanza [:8308](projects/views.py#L8308) | **Yes** — reassign to anyone, or unassign |
| `create_issue_comment` | PM-only stanza [:8411](projects/views.py#L8411) | **Yes** |
| `close_issue` | `_is_project_pm()` [:8241](projects/views.py#L8241) | **No** |
| `reopen_issue` | `_is_project_pm()` [:8274](projects/views.py#L8274) | **No** |

### B.3 How bad

Worse than "can see data it should not", because six of the eight are writes and one of them
sends outbound notifications:

1. **`resolve_issue` is the sharpest.** Any authenticated user can mark any issue on any
   project Resolved, with a resolution note of their choosing, and the view then sends
   WhatsApp and email to the PM, every coordinator, the assignee and the raiser
   [:8206-8218](projects/views.py#L8206-L8218). The audit trail records their name via
   `log_activity`, so it is attributable — but the message has already gone out.
2. **`assign_issue` lets anyone push work onto anyone.** `UserProfile.objects.get(pk=...)`
   [:8306](projects/views.py#L8306) with no filter — any profile, active or not, any role.
3. **`issue_detail` leaks the full user directory.** `all_profiles` is every active
   `UserProfile` with its `user` joined [:8098](projects/views.py#L8098), rendered into the
   assignee dropdown on a page any authenticated user can open for any issue.
4. **Project Coordinator is not excluded by the stanza.** The literal comparison is
   `role == 'PM'`; a coordinator's role string is `'Project Coordinator'`, so a coordinator
   passes every one of the eight. Their dashboard is scoped
   [views.py:469](projects/views.py#L469); their Issue access is not.
5. **Soft-deleted projects are reachable.** `Issue.objects` and `_issue_base_qs()`
   [views.py:7772](projects/views.py#L7772) carry no `project__is_deleted=False` term, so
   every one of the ten Issue endpoints — including the two guarded ones — operates on issues
   belonging to deleted projects.

**This is the largest single block in 0.2's scope: eight endpoints, one shared five-line
stanza, one fix.**

---

## Task C — Role reachability matrix

Ten values in `UserProfile.ROLE_CHOICES` [models.py:508-539](projects/models.py#L508-L539).
`'Design Head'` was removed in migration 0053 and is **not** a role; it is the boolean
`UserProfile.is_design_head` [models.py:549](projects/models.py#L549). `is_design_qc`
[models.py:557](projects/models.py#L557) is a second, independent boolean.

### C.1 The two mechanisms, and why they disagree

Every cell below resolves to one of two things:

- **`permissions.user_can_view_project()`** [permissions.py:63](projects/permissions.py#L63) —
  the single home for visibility, and the only function allowed to branch on role strings.
- **The view-layer PM-only stanza** — which ignores `user_can_view_project()` entirely and
  admits everyone but a PM.

They do not agree, and the second one wins wherever it is used, because it is what the view
actually calls.

`PORTFOLIO_VIEW_ROLES = frozenset({'CEO', 'Finance', 'SCM', 'Admin'})`
[permissions.py:29](projects/permissions.py#L29). BD is a separate branch
[permissions.py:135](projects/permissions.py#L135). Design Head is admitted by flag *or* the
vestigial role string [permissions.py:118](projects/permissions.py#L118).

### C.2 Per role

#### CEO
- **Lands on:** `/dashboard/ceo/` [decorators.py:22](projects/decorators.py#L22) — but via
  `/landing/` first, since CEO is in `LANDING_ROLES`
  [decorators.py:32](projects/decorators.py#L32).
- **Dashboard queries:** `_get_ceo_dashboard_context()`
  [views.py:1696](projects/views.py#L1696) — portfolio-wide Project/Task/Issue aggregates in
  three queries, no per-user term. `is_deleted=False` applied.
- **Arbitrary project by URL:** Yes — `PORTFOLIO_VIEW_ROLES` and the PM-only stanza both admit.
- **BOQ / Issue / DC / PaymentRequest / design:** BOQ yes (`BOQ_PORTFOLIO_READ_ROLES`
  [permissions.py:157](projects/permissions.py#L157)). Issue yes. **DC no** — the allowlist at
  [views.py:8779](projects/views.py#L8779) omits CEO. **PaymentRequest no** — allowlist at
  [views.py:9107](projects/views.py#L9107) omits CEO. Design surfaces yes for reads that route
  through `user_can_view_design()`; every design *action* is refused.
- **Can write to an unassigned project:** raise / resolve / assign / comment on any Issue;
  upload project documents and task attachments; comment on any task. **Cannot** change a task
  status or due date — `'CEO'` is not in `Task.ROLE_CHOICES`, so the role-match branch never
  fires for them.
- **Note:** `dashboard_ceo` has **no** `@role_required` [views.py:2210](projects/views.py#L2210)
  despite its docstring saying "CEO role only". Every authenticated user reaches the CEO
  portfolio dashboard.

#### Admin
- **Lands on:** `/dashboard/admin/` — which renders a bare template
  [views.py:290](projects/views.py#L290) and is itself `@login_required` only.
- **Dashboard queries:** none. The real Admin surface is `admin_project_list`
  [views.py:9674](projects/views.py#L9674), `@role_required(['Admin'])`, portfolio-wide,
  `is_deleted=False`.
- **Arbitrary project / BOQ / Issue / DC by URL:** Yes to all. PaymentRequest yes.
  Design *read* surfaces yes (`user_can_view_project()` admits via `PORTFOLIO_VIEW_ROLES`);
  design *actions* no.
- **Can write to an unassigned project:** everything the "none"-class endpoints allow, plus
  the 33 `@role_required(['Admin'])` administration screens, plus `project_delete` and
  `program_delete`. Admin is intended unrestricted, so this is policy, not a finding.

#### System Admin
- **Lands on:** `/sub-admin/projects/` [decorators.py:12](projects/decorators.py#L12).
- **Dashboard queries:** `subadmin_projects` [views.py:10399](projects/views.py#L10399) —
  portfolio-wide, `is_deleted=False`; first-time PM assignment only.
- **`user_can_view_project()` gives System Admin nothing** — it has no branch and falls
  through to management authority alone [permissions.py:150](projects/permissions.py#L150).
- **But the view layer does not care.** Arbitrary project overview, task detail, timeline,
  every Issue endpoint, project documents: **all reachable**, because the PM-only stanza does
  not name System Admin. BOQ **is** correctly refused
  ([permissions.py:270](projects/permissions.py#L270) has no System Admin branch), as is DC
  detail and PaymentRequest detail (both allowlists omit it).
- **This is the clearest single demonstration that the two mechanisms disagree.** The one
  role the permissions module deliberately gives nothing to has near-complete read and write
  access through the URL bar.

#### PM
- **Lands on:** `/dashboard/pm/`. Queries: `Q(assigned_pm=me) | Q(coordinators=me)`
  [views.py:469](projects/views.py#L469).
- **Arbitrary project by URL:** **No.** The one role the stanza actually stops.
- **BOQ:** no (`user_can_view_project_boq` routes through `user_can_manage_project`).
  **Issue:** no. **DC:** no — explicit PM isolation [views.py:8783](projects/views.py#L8783).
  **PaymentRequest detail: YES** — role allowlist only [views.py:9107](projects/views.py#L9107).
- **Can write to an unassigned project:** `set_milestone_amounts`
  [views.py:6642](projects/views.py#L6642) — no ownership check, so a PM can set M1/M2/M3
  agreed amounts on another PM's project. Also `task_status_update` and `task_set_due_date` on
  any task whose `assigned_role == 'PM'`, on any project, because the role-match branch is
  evaluated when `is_pm` is False.

#### Project Coordinator
- **Lands on:** `/dashboard/pm/` — deliberate reuse [decorators.py:17](projects/decorators.py#L17).
  Scoped identically to a PM.
- **Arbitrary project by URL:** **Yes.** The stanza compares against `'PM'`; a coordinator's
  role string is `'Project Coordinator'`.
- **BOQ:** no. **DC detail: yes** — the allowlist at [views.py:8779](projects/views.py#L8779)
  names `'Project Coordinator'` and the PM-isolation line below it does not.
  **Issue: yes, all eight unguarded endpoints.** **PaymentRequest:** no.
- **Can write to an unassigned project:** every "none"-class write; plus `task_status_update`
  and `task_set_due_date` fail for them, since `'Project Coordinator'` is not in
  `Task.ROLE_CHOICES`.
- **Prompt 0.1's finding restated precisely:** a coordinator is scoped on the dashboard and
  everywhere `user_can_manage_project()` is called, and unscoped everywhere the stanza is used.

#### Site Engineer
- **Lands on:** `/dashboard/site-engineer/`. Queries: `phases__tasks__assigned_to=me`
  [views.py:759](projects/views.py#L759).
- **`user_can_view_project()`** gives SE a task-holding branch
  [permissions.py:138](projects/permissions.py#L138) — correct, and mirrors the dashboard.
- **Arbitrary project by URL:** **Yes**, because `project_overview` never calls it.
- **BOQ:** no. **PaymentRequest:** no. **DC detail: yes** — role allowlist admits every SE.
  **Issue: yes.** **Design:** survey and CAD downloads are refused unless they hold a task.
- **Can write to an unassigned project — the most serious row in this table:**
  - `confirm_grn` [views.py:8815](projects/views.py#L8815) — **any SE, any challan, any
    project.** Writes `received_quantity`, `damaged_quantity`, `condition`, `grn_notes`,
    `grn_confirmed_by`, `grn_date` and recalculates DC status.
  - `task_status_update` [views.py:3602](projects/views.py#L3602) — any task with
    `assigned_role='Site Engineer'` on any project, including marking Plant Commissioning
    **Done**, which is the payment-milestone (M3) notification trigger.
  - `task_set_due_date`, `checklist_item_complete`, all Issue writes, both upload endpoints.

#### Design
- **Lands on:** `/dashboard/design/`. Queries: `assigned_design=me OR
  phases__tasks__assigned_to=me`, with OPEX exempt from the status filter
  [views.py:926-931](projects/views.py#L926-L931).
- **`user_can_view_project()`** mirrors that union exactly
  [permissions.py:144](projects/permissions.py#L144).
- **Arbitrary project by URL:** **Yes** via `project_overview` / `task_detail` / timeline;
  **no** for BOQ — `user_can_view_project_boq()` is genuinely enforced, and BOQ *write* is
  narrower still (`assigned_design` on this project alone,
  [permissions.py:326](projects/permissions.py#L326)).
- **Design screens:** scoped correctly and per-site — allocated designer, or Design Head
  authority, or the named QC reviewer.
- **Can write to an unassigned project:** all Issue writes, both uploads, task comments; and
  `task_status_update` / `task_set_due_date` / `checklist_item_complete` on any task with
  `assigned_role='Design'` anywhere in the portfolio.
- **Flag-dependent:** `is_design_qc` widens BOQ read portfolio-wide
  [permissions.py:281](projects/permissions.py#L281), admits them to the QC dashboard, and
  gives them the open pool in `_qc_scope()` [design_views.py:3416](projects/design_views.py#L3416).
  A designer with **no** flag who has been named `qc_assigned_to` on a site gets per-site read
  on that site only [permissions.py:604](projects/permissions.py#L604).

#### Finance
- **Lands on:** `/landing/` then `/dashboard/finance/`.
- **Dashboard queries:** `Project.objects.filter(is_deleted=False, status__in=['Active','In
  Progress'])` [views.py:1082](projects/views.py#L1082) — **portfolio-wide, no per-user term
  of any kind.** All four header aggregates likewise.
- **Arbitrary project by URL:** **Yes** — `PORTFOLIO_VIEW_ROLES` and the stanza.
- **BOQ: no.** Finance is deliberately excluded from `BOQ_PORTFOLIO_READ_ROLES`
  [permissions.py:157](projects/permissions.py#L157) — "Finance has no BOQ surface anywhere in
  the product". **DC: no** — the allowlist omits Finance. **PaymentRequest: yes.**
  **Issue: yes.** **Design read surfaces: yes**, through `user_can_view_project()`.
- **Can write to an unassigned project:** `milestone_invoice`, `milestone_receive`,
  `confirm_payment_request`, the milestone-update POST branch of `project_overview`
  [views.py:6742](projects/views.py#L6742), all Issue writes, both uploads, and
  `task_status_update` / `task_set_due_date` on any `assigned_role='Finance'` task portfolio-wide.

#### SCM
- **Lands on:** `/landing/` then `/dashboard/scm/`.
- **Dashboard queries:** portfolio-wide throughout — BOQ awaiting acknowledgement, DCs today,
  overdue DCs, every active project, every DC, per-project issue aggregates
  [views.py:1213-1300](projects/views.py#L1213-L1300). No per-user term anywhere. Plus
  `scm_opex_tender_rows()` [design_views.py:4625](projects/design_views.py#L4625), which walks
  every OPEX Program.
- **Arbitrary project by URL:** **Yes.** **BOQ: yes** — `BOQ_PORTFOLIO_READ_ROLES`.
  **DC: yes.** **PaymentRequest: yes.** **Issue: yes.** **Design: read surfaces yes; site
  groups yes (`user_can_manage_site_groups()` is the SCM role alone,
  [permissions.py:744](projects/permissions.py#L744)).**
- **Can write to an unassigned project:** `boq_acknowledge`, `create_delivery_challan`,
  `override_grn`, `raise_payment_request`, every site-group write, plus all the "none"-class
  writes, plus `task_status_update` / `task_set_due_date` on any `assigned_role='SCM'` task.
- **SCM has the widest write surface of any non-Admin role in the product.**

#### BD
- **Lands on:** `/dashboard/bd/`. Not in `LANDING_ROLES`.
- **Dashboard queries:** `Project.objects.filter(is_deleted=False, status__in=['Active','In
  Progress'])` [views.py:6487](projects/views.py#L6487) — portfolio-wide, no per-user term.
- **Arbitrary project by URL:** **Yes**, via its own branch
  [permissions.py:135](projects/permissions.py#L135) and via the stanza.
- **BOQ: no** — deliberately absent from `BOQ_PORTFOLIO_READ_ROLES`. **DC: no.**
  **PaymentRequest: no.** **Issue: yes.** **Design: read surfaces yes.**
- **Can write to an unassigned project:** `set_milestone_amounts` on any project
  [views.py:6642](projects/views.py#L6642); all Issue writes; both uploads;
  `task_status_update` / `task_set_due_date` / `checklist_item_complete` on the single
  `assigned_role='BD / Sales'` task each Residential project carries — with the
  `{'BD': 'BD / Sales'}` normalisation applied at
  [views.py:3599](projects/views.py#L3599), [:4134](projects/views.py#L4134) and
  [:2364](projects/views.py#L2364).

#### Design Head — the `is_design_head` flag, not a role
- **Lands on:** wherever their `role` sends them. `ROLE_DASHBOARD` has no entry for a flag.
- **Visibility:** `user_can_view_project()` returns **True for every project**
  [permissions.py:118](projects/permissions.py#L118), independently of `role`, and
  `user_can_view_project_boq()` likewise [permissions.py:277](projects/permissions.py#L277).
- **Authority:** fifteen design endpoints via `user_has_design_head_authority()`
  [permissions.py:449](projects/permissions.py#L449), plus `task_assign_design_head`
  [views.py:4073](projects/views.py#L4073), plus site-group **read**
  [permissions.py:759](projects/permissions.py#L759), plus quality analytics
  ([design_views.py:4728](projects/design_views.py#L4728), which uses the narrower
  `user_is_design_head()` and refuses the deputy).
- **Deputy:** `user_is_design_head_deputy()` [permissions.py:412](projects/permissions.py#L412)
  — presence of `design_head_deputy` on a profile that is still a Head. The deputy gets
  **BOQ read portfolio-wide** [permissions.py:277](projects/permissions.py#L277) and every
  design action, but **not** project visibility: `user_can_view_project()` has no deputy
  branch, and `user_has_design_head_authority()` documents this refusal explicitly
  [permissions.py:440-447](projects/permissions.py#L440-L447).

### C.3 Summary table

| Role | Dashboard scoped? | Any project by URL? | BOQ | Issue | DC | PaymentRequest | Design read | Writes on an unassigned project |
|---|---|---|---|---|---|---|---|---|
| CEO | No (portfolio) | Yes | Yes | Yes | No | No | Yes | Issues, uploads, comments |
| Admin | No (portfolio) | Yes | Yes | Yes | Yes | Yes | Yes | Everything (intended) |
| System Admin | No (portfolio) | **Yes** | No | **Yes** | No | No | No | Issues, uploads, comments |
| PM | **Yes** | **No** | No | No | No | **Yes** | No | `set_milestone_amounts`; PM-role tasks |
| Project Coordinator | **Yes** | **Yes** | No | **Yes** | **Yes** | No | No | Issues, uploads, comments |
| Site Engineer | **Yes** | **Yes** | No | **Yes** | **Yes** | No | Task-holding only | **`confirm_grn`**; SE-role tasks; issues; uploads |
| Design | **Yes** | **Yes** | Scoped | **Yes** | No | No | Scoped | Design-role tasks; issues; uploads |
| Finance | No (portfolio) | Yes | **No** | Yes | No | Yes | Yes | Milestones, payment confirm, Finance tasks, issues |
| SCM | No (portfolio) | Yes | Yes | Yes | Yes | Yes | Yes | **Widest non-Admin surface** |
| BD | No (portfolio) | Yes | No | Yes | No | No | Yes | `set_milestone_amounts`; BD task; issues; uploads |
| *(flag)* Design Head | n/a | Yes | Yes | via role | via role | via role | Yes + all design actions | Design workflow |
| *(flag)* Design QC | n/a | via role | **Yes** | via role | via role | via role | Yes | Gate-1 verdicts |

---

## Task D — Finance and SCM impact analysis

### D.1 Does the existing task template give them anything?

`build_residential_phases()` [utils.py:697-813](projects/utils.py#L697-L813) — 9 phases, **52
tasks** (the assertion at [utils.py:912](projects/utils.py#L912) is `== 52`; the success
message at [views.py:2631](projects/views.py#L2631) says "53 tasks created" and is **wrong**).

**Finance: 6 of 52. SCM: 11 of 52. Seventeen in total.**

**Finance (6):**

| Phase | # | Task name |
|---|---:|---|
| 1 · Sales & Documentation | 2 | Send Invoice - Advance Payment |
| 1 · Sales & Documentation | 3 | Advance Payment Confirmation *(M1)* |
| 5 · Procurement | 6 | Send Invoice - Material Supply |
| 5 · Procurement | 7 | Pre Dispatch Payment Confirmation *(M2)* |
| 8 · Commissioning | 10 | Send Invoice - Final Payment |
| 9 · Finance Closure | 1 | 100% Payment Confirmation *(M3)* |

**SCM (11):**

| Phase | # | Task name |
|---|---:|---|
| 4 · Pre-Installation Approvals | 3 | Vendor Registration |
| 5 · Procurement | 1 | Procurement Schedule |
| 5 · Procurement | 2 | PO Placed MMS |
| 5 · Procurement | 3 | PO Placed Module |
| 5 · Procurement | 4 | PO Placed Inverter |
| 5 · Procurement | 5 | PO for B & C Class Items |
| 6 · Delivery | 1 | Delivery Schedule |
| 6 · Delivery | 2 | Delivery of MMS |
| 6 · Delivery | 3 | Delivery of B & C Class Items |
| 6 · Delivery | 4 | Delivery of Module |
| 6 · Delivery | 5 | Delivery of Inverter |

So the answer to "if the answer is zero, that is a blocking finding" is: **it is not zero, but
it is worse than zero for SCM and worse than useful for Finance.** See D.2.

**And it is zero for OPEX and CAPEX.** `attach_residential_template()` is called only inside
`if project.project_type == 'Residential'` [views.py:2610](projects/views.py#L2610). OPEX sites
created through `create_opex_site()` [views.py:2935](projects/views.py#L2935) get no phases and
no tasks at all; the activation message tells the PM to "Add tasks manually"
[views.py:2632](projects/views.py#L2632). **Task-based scoping gives Finance and SCM literally
nothing on any OPEX or CAPEX site, today and until prompt 2.4 lands an execution template.**

### D.2 Are those tasks actually assigned to a user?

**This is the blocking finding, and it splits the two roles.**

**SCM: all 11 tasks are created with `assigned_to = NULL` and nothing ever assigns them
automatically.** `attach_residential_template()` [utils.py:830-919](projects/utils.py#L830-L919)
`bulk_create`s every task with no `assigned_to`, then back-assigns exactly two groups:

```python
assign_tasks_to(Task.objects.filter(phase__project=project, assigned_role=Task.PM), pm_profile)
...
assign_tasks_to(
    Task.objects.filter(
        phase__project=project,
        task_name__in=INVOICE_TASK_NAMES + RESIDENTIAL_FINANCE_CONFIRMATION_TASK_NAMES,
    ),
    finance_assignee,
)
```

The docstring says it outright: *"SE-role tasks start unassigned — same as Design/SCM/Finance"*
[utils.py:875](projects/utils.py#L875). **`phases__tasks__assigned_to=<any SCM profile>` matches
zero rows on a freshly activated Residential project.** An SCM user acquires a task only if a
PM opens `task_assign` and hands them one by name, which nothing in the product prompts them to
do.

**Finance: all 6 tasks ARE assigned — to one hardcoded person.**
`RESIDENTIAL_FINANCE_ASSIGNEE_EMAIL = 'santosh@horizonrenewablepower.com'`
[utils.py:679](projects/utils.py#L679). The three `INVOICE_TASK_NAMES` and the three
`RESIDENTIAL_FINANCE_CONFIRMATION_TASK_NAMES` [utils.py:690-694](projects/utils.py#L690-L694)
together cover exactly the six Finance rows, and activation **fails loudly and rolls back** if
that account is missing [utils.py:895-901](projects/utils.py#L895-L901).

The consequence is precise and awkward:

- **One named Finance user would be assignment-scoped to every Residential project** — which
  is portfolio-wide by another name, and would look like nothing changed.
- **Every other Finance user would see nothing at all.**

So assignment-scoping Finance produces a scope that is simultaneously too wide (for one person)
and empty (for everyone else), and which silently depends on a hardcoded email address.

### D.3 What breaks

Assuming 0.2 replaces `PORTFOLIO_VIEW_ROLES` membership with `phases__tasks__assigned_to=me`
for Finance and SCM, and nothing else changes:

#### SCM-facing

| Surface | File | Effect under task-assignment scoping |
|---|---|---|
| `dashboard_scm` — `boq_awaiting` | [views.py:1213](projects/views.py#L1213) | **Zero.** Counts submitted BOQs portfolio-wide; no SCM user holds a task on any of those projects. |
| `dashboard_scm` — `deliveries_today` | [views.py:1219](projects/views.py#L1219) | **Zero.** Would return only challans on projects where this SCM user holds an assigned task — and they hold none. |
| `dashboard_scm` — `overdue` | [views.py:1228](projects/views.py#L1228) | **Zero.** |
| `dashboard_scm` — `active_projects` / `dc_by_project` / `delivery_challans` / material badges / issue aggregates / the whole 4-stage pipeline table | [views.py:1237-1420](projects/views.py#L1237-L1420) | **Empty dashboard.** Every one of these starts from `active_projects`. |
| `scm_opex_tender_rows()` | [design_views.py:4625](projects/design_views.py#L4625) | **Zero rows** — OPEX sites have no tasks at all, so no SCM user can ever hold one. |
| `boq_acknowledge` | [views.py:5993](projects/views.py#L5993) | **Breaks system-wide.** The docstring already warns: *"DELIBERATELY role-only… scoping this to a relationship would break acknowledgement system-wide. Do not 'harden' this to match the Design gates."* [views.py:6005-6007](projects/views.py#L6005-L6007) |
| `boq_detail` SCM branch (`ordered_quantity`, acknowledge) | [views.py:4581](projects/views.py#L4581) | **403 on every project** — `BOQ_PORTFOLIO_READ_ROLES` would have to lose `'SCM'`, and `user_can_edit_project_boq()` never admitted SCM anyway. |
| `create_delivery_challan` | [views.py:8658](projects/views.py#L8658) | **403 on every project.** SCM raises the DC that *creates* the delivery record — there is no prior relationship to derive scope from. Chicken-and-egg. |
| `override_grn` | [views.py:8894](projects/views.py#L8894) | 403 on every project. |
| `raise_payment_request` | [views.py:6292](projects/views.py#L6292) | 403 on every project. |
| `delivery_challan_detail` | [views.py:8769](projects/views.py#L8769) | 403 on every project. |
| Site-group formation (6 endpoints) | [design_views.py:4335-4600](projects/design_views.py#L4335) | **Unaffected** — `user_can_manage_site_groups()` is the SCM role string alone [permissions.py:744](projects/permissions.py#L744) and takes no project. But the sites *inside* a group become unreadable, and `site_group_detail`'s aggregated BOQ reads every member's BOQ [design_views.py:4159](projects/design_views.py#L4159). |
| `vendor_*` (4 endpoints) | [views.py:4279-4443](projects/views.py#L4279) | Unaffected — Vendor is not project-scoped. |

**Every SCM operational surface returns empty or 403.** SCM's job in this product *starts*
before any relationship exists: they acknowledge a BOQ they did not author, on a project they
were not assigned to, and then raise the first delivery challan. Assignment scoping inverts the
causality.

#### Finance-facing

| Surface | File | Effect under task-assignment scoping |
|---|---|---|
| `dashboard_finance` — project cards | [views.py:1082](projects/views.py#L1082) | **The one named user keeps everything; every other Finance user sees zero cards.** |
| `dashboard_finance` — four header aggregates | [views.py:1096-1122](projects/views.py#L1096-L1122) | Same split. Note these currently have **no `is_deleted` term** and would need one either way. |
| `landing` counts | [views.py:271](projects/views.py#L271) | Would over-report for every Finance user but the named one — the card count would not match the dashboard it opens, which is the exact defect the landing docstring says it exists to avoid [views.py:265-268](projects/views.py#L265-L268). |
| `milestone_invoice` / `milestone_receive` | [views.py:6166](projects/views.py#L6166), [:6192](projects/views.py#L6192) | Named user unaffected on Residential; every other Finance user 403s. **All Finance users 403 on OPEX/CAPEX**, which have no tasks. |
| `confirm_payment_request` | [views.py:6388](projects/views.py#L6388) | Same. |
| `payment_request_detail` | [views.py:9102](projects/views.py#L9102) | Same. |
| `project_overview` Finance milestone POST branch | [views.py:6742](projects/views.py#L6742) | Same. |
| `task_detail_status_update` on Finance tasks | [views.py:3798](projects/views.py#L3798) | **Already correctly scoped** — `task.assigned_to == profile`. Unaffected. |
| `tasks_drill_down` | [views.py:381](projects/views.py#L381) | Finance currently falls through to every active project [views.py:415](projects/views.py#L415); would need an explicit branch either way. |

### D.4 What is the smallest assignment mechanism that keeps those views useful?

**Program-level alone is not sufficient, because Residential has no Program.**
`docs/execution-model.md` §1: *"Residential never has one."* `Project.program` is nullable and
every Residential row has `program = NULL` [models.py:49-55](projects/models.py#L49-L55). A
`ProgramAssignment` would give Finance and SCM exactly zero visibility on the Residential
portfolio, which is the entire live business today.

**Project-level alone works but is unusable at OPEX scale.** A tender carries up to ~97 sites
(`tender_release_completeness` walks them all); asking Admin to create 97 rows per Finance user
per tender is a data-entry feature nobody will maintain, and stale rows become silent access.

**The smallest mechanism that keeps every view above useful is one effective-dated table
carrying BOTH a nullable `program` FK and a nullable `project` FK, with a database CHECK that
exactly one is set.** A Program row expands to every non-deleted site under it; a Project row
covers one site. One table, one helper, one branch in `user_can_view_project()`. Detail in
Task E.

**Three things it still will not fix, and they need decisions rather than code:**

1. **`boq_acknowledge`, `create_delivery_challan` and `raise_payment_request` are
   relationship-creating acts.** SCM acts first and the relationship follows. These three should
   stay role-gated regardless of what 0.2 does to visibility, exactly as their own docstrings
   already argue.
2. **The hardcoded Finance assignee.** As long as `RESIDENTIAL_FINANCE_ASSIGNEE_EMAIL` is the
   only mechanism putting a Finance user on a Residential project, Finance scoping is one
   person's inbox. Either it is replaced by an assignment row written at activation, or Finance
   scoping is deferred.
3. **OPEX/CAPEX have no tasks at all,** so any task-derived scope is empty there until 2.4.

---

## Task E — `ProgramAssignment` proposal

**Proposal only. No model was written and no migration was created.**

### E.1 Shape

```
PROGRAM_ASSIGNMENT_ROLE_CHOICES  (module-level constants, per R-10)
    MANAGER      = 'manager'       # the PM who owns the whole Program (§4)
    COORDINATOR  = 'coordinator'
    FINANCE      = 'finance'
    SCM          = 'scm'
```

| Field | Type | Notes |
|---|---|---|
| `program` | FK → `Program`, `related_name='assignments'`, `on_delete=CASCADE`, **null=True** | One of `program` / `project` is set, never both |
| `project` | FK → `Project`, `related_name='assignments'`, `on_delete=CASCADE`, **null=True** | The Residential arm — see D.4 |
| `profile` | FK → `UserProfile`, `related_name='portfolio_assignments'`, `on_delete=PROTECT` | PROTECT, matching `Project.assigned_pm`: deactivating a user must not erase who held what |
| `assignment_role` | `CharField(max_length=20, choices=PROGRAM_ASSIGNMENT_ROLE_CHOICES)` | **Not** named `role` — `UserProfile.role` and `Task.assigned_role` are already two vocabularies (§4); a third bare `role` would be unreadable in a join |
| `assigned_by` | FK → `UserProfile`, `SET_NULL`, null | Mirrors `SiteGroupMembership.added_by` |
| `assigned_at` | `DateTimeField(auto_now_add=True)` | |
| `removed_by` | FK → `UserProfile`, `SET_NULL`, null | |
| `removed_at` | `DateTimeField(null=True, blank=True)` | **The effective-dating.** Never hard-delete |
| `removal_reason` | `TextField(blank=True, default='')` | |

`client_uuid` (R-14) is **not** proposed: R-14 scopes it to "any table a site engineer writes
to in the field", and no site engineer writes here.

### E.2 Does it replace `Project.assigned_pm` / `coordinators`?

**No, and it must not.** `docs/execution-model.md` §4 is explicit: *"`assigned_pm` and
`coordinators` on `Project` keep their existing behaviour and are not replaced."*

They sit alongside, and the composition rule is the one `user_can_manage_project()` already
uses [permissions.py:32-60](projects/permissions.py#L32-L60) — an **unconditional OR**, never
an if/else. A Program assignment can only ever *add* a manager; it can never remove the site's
own PM. If 0.2 restructures this as "if a program assignment exists, use it, else fall back",
it will silently lock out the PM the same way the invariant note at
[permissions.py:43-48](projects/permissions.py#L43-L48) warns about.

Concretely: `user_can_manage_project()` gains a third term after the existing two, and
`user_can_view_project()` gains one branch. Nothing else in either function moves.

### E.3 Constraints — and yes, the D-1 lesson applies directly

The existing constraint is [models.py:2894-2902](projects/models.py#L2894-L2902):

```python
models.UniqueConstraint(
    fields=['project'],
    condition=models.Q(removed_at__isnull=True),
    name='uniq_active_site_group_membership',
)
```

**That constraint is keyed on `project` alone, and D-1 has already outgrown it.** §2 D-1 now
requires that *"A Project may hold one active membership of each type simultaneously"* —
procurement and execution. `SiteGroup` has no `group_type` field yet
[models.py:2815-2854](projects/models.py#L2815-L2854), so the moment prompt 1.1 adds it, this
constraint forbids the very thing D-1 says must be possible.

**And it will break a `.first()`.** `active_group_membership()`
[design_views.py:4118-4128](projects/design_views.py#L4118-L4128) says so in its own docstring:

> *"One" is guaranteed by the partial unique constraint on `SiteGroupMembership`, not by this
> query — `.first()` here is picking the only row that can exist, not the first of several.*

Once two types can coexist, that `.first()` returns whichever row the default ordering
(`['added_at']`) puts first, and its three callers —
`design_change_request` [design_views.py:3084](projects/design_views.py#L3084),
`design_change_request_form` [design_views.py:4052](projects/design_views.py#L4052) — would
silently read a procurement lock where they meant an execution grouping, or the reverse.

**The lesson, applied here:** put the discriminator in the constraint from day one, and never
write a lookup helper that omits it.

```python
constraints = [
    models.UniqueConstraint(
        fields=['program', 'profile', 'assignment_role'],
        condition=models.Q(removed_at__isnull=True, project__isnull=True),
        name='uniq_active_program_assignment',
    ),
    models.UniqueConstraint(
        fields=['project', 'profile', 'assignment_role'],
        condition=models.Q(removed_at__isnull=True, program__isnull=True),
        name='uniq_active_project_assignment',
    ),
    models.CheckConstraint(
        check=(models.Q(program__isnull=False, project__isnull=True)
               | models.Q(program__isnull=True,  project__isnull=False)),
        name='program_assignment_exactly_one_target',
    ),
]
```

Two partial uniques rather than one, because `NULL` never equals `NULL` in a unique index and a
single constraint spanning both FKs would not enforce either.

**Indexes:**

- `Index(fields=['profile', 'removed_at'])` — the hot path: "which programs/projects is this
  user on", evaluated on every `user_can_view_project()` call for a Finance or SCM user.
- `Index(fields=['program', 'removed_at'])` — "who is on this program", for the assignment
  management screen.
- `Index(fields=['project', 'removed_at'])` — the Residential arm of the same question.

### E.4 Interaction with `user_can_view_project()`

**Yes — holding a Program assignment grants visibility of every non-deleted Project under it.**
That is the point; anything narrower is just project assignment with extra steps.

One new branch, placed **after** the `can_manage` short-circuit and **before** the role
branches, so it applies regardless of role:

```python
# after: can_manage = user_can_manage_project(user, project); if can_manage: return True
if _has_active_assignment(profile, project):
    return True
```

where `_has_active_assignment()` uses reverse relations only, keeping `permissions.py`
model-import-free exactly as `user_is_design_head_deputy()` and
`user_is_assigned_qc_reviewer()` do:

```python
qs = profile.portfolio_assignments.filter(removed_at__isnull=True)
return qs.filter(project=project).exists() or (
    project.program_id is not None and qs.filter(program_id=project.program_id).exists()
)
```

Three consequences worth naming before 0.2 writes it:

1. **Two queries per call in the worst case.** `user_can_view_project()` is called per-object,
   and `project_overview` renders one page per project — acceptable. A *list* view must not
   loop it; lists need the equivalent `Q()` object, which should be a second helper
   (`visible_projects_q(user)`) written in the same commit or the two will drift, exactly the
   way `tasks_drill_down` has already drifted into a third copy of the PM rule
   [views.py:397-405](projects/views.py#L397-L405).
2. **A Program assignment must not grant `user_can_manage_project()` by itself.** Visibility
   and authority are two questions with two answers
   [permissions.py:4-7](projects/permissions.py#L4-L7); only `assignment_role='manager'` should
   feed the authority helper, and that is a separate decision.
3. **It grants nothing on a soft-deleted site.** The helper must carry
   `project__is_deleted=False` on the Program arm, since there is no custom manager (§6).

### E.5 Backfill

**Derivable, for `manager` and `coordinator` only:**

- For each non-deleted `Program`, one `manager` row per distinct `assigned_pm` across its
  non-deleted child sites, and one `coordinator` row per distinct coordinator. This exactly
  reproduces what `user_can_manage_program()`
  [permissions.py:338-357](projects/permissions.py#L338-L357) derives at runtime today, so no
  PM gains or loses anything on the day it ships.
- `assigned_by = NULL`, `assigned_at = Program.created_at`, `removed_at = NULL`.
- Plus one `manager` row for `Program.created_by`'s profile where that user is not already
  covered — this preserves the `created_by` fallback in `_can_access_program()`
  [views.py:2786](projects/views.py#L2786), which is what lets a brand-new empty Program stay
  reachable by the person who made it.

**Not derivable, and this is a blocking question:** there is **no** Finance or SCM relation
anywhere on `Program` or `Project` to backfill from. `Program` has `created_by` and nothing
else [models.py:234-236](projects/models.py#L234-L236). The only Finance signal in the codebase
is the hardcoded email at [utils.py:679](projects/utils.py#L679). For SCM there is not even
that.

So the backfill has exactly two honest options, and the product owner has to pick one:

- **(a)** Backfill every active Finance and SCM user onto every Program and every Residential
  project — reproduces today's behaviour exactly, ships zero risk, and defers the actual
  narrowing to a later prompt with real data behind it.
- **(b)** Backfill nothing for Finance and SCM, ship the assignment screen, and accept that
  both dashboards are empty until somebody fills it in.

Option (b) is only safe if the assignment screen ships in the *same* prompt, which R-11
requires anyway.

---

## Task F — evidence for the two unresolved roles

**No decision is taken here.**

### F.1 BD

**What `permissions.py` says.** BD is its own branch
[permissions.py:127-137](projects/permissions.py#L127-L137), deliberately not folded into
`PORTFOLIO_VIEW_ROLES`, and the comment records it as **settled current policy decided by the
product owner**:

> *BD's existing workflow (dashboard_bd renders a flat portfolio queryset with no per-user
> term) works and is not being changed… if BD's scope is ever revisited, the branch to change
> is already isolated and this comment says why it is separate.*

**Is there any per-user term on BD data anywhere?** Searched exhaustively:

| Candidate | Finding |
|---|---|
| `assigned_bd` on Project | **Does not exist.** `dashboard_bd`'s own comment confirms: *"no assigned_bd field on Project model confirmed"* [views.py:6477](projects/views.py#L6477) |
| `Project.created_by` | FK to `auth.User`, but a BD user can never be its value: `project_create` is `@role_required(['PM'])` [views.py:2389](projects/views.py#L2389), `opex_site_create` is `['Admin','PM']` [views.py:2991](projects/views.py#L2991), and the Zoho webhook writes `created_by=None` |
| A deal or lead relation | **None exists.** There is no `Deal`, `Lead` or `Opportunity` model in `models.py` |
| `zoho_deal_id` | A `CharField` holding the Zoho record id, written by the webhook as a duplicate guard [models.py:92](projects/models.py#L92). Not a foreign key, not per-user, and carries no owner |
| `Task.assigned_role = 'BD / Sales'` | Exists, and there is **exactly one such task per Residential project** — "OCR, Documentation & Verification", Phase 1 pos 1 [utils.py:710](projects/utils.py#L710) |
| Is that task assigned? | **No.** `attach_residential_template()` back-assigns only PM-role tasks and the six named Finance tasks [utils.py:876-907](projects/utils.py#L876-L907). The BD task starts `assigned_to = NULL` and stays that way unless a PM assigns it by hand |
| `PaymentMilestone` | BD's actual workflow surface — `set_milestone_amounts` [views.py:6642](projects/views.py#L6642). `PaymentMilestone.created_by` is the *PM* who activated the project, not a BD user |

**What BD would lose under assignment scoping:** everything. `dashboard_bd` is one flat
queryset [views.py:6487](projects/views.py#L6487), and the ORC-status batch query
[views.py:6509](projects/views.py#L6509) is likewise portfolio-wide. With no per-user term
available, an assignment-scoped BD sees an empty dashboard on day one — the same failure mode
as SCM in D.3, and for the same reason.

**Is a defensible narrower scope available at all?** Only two, both requiring new data:

- **(i)** Assign the one BD task per project to a real BD user at activation, the way Finance's
  six are assigned — then `phases__tasks__assigned_to` works. Cost: one line in
  `attach_residential_template()`, plus deciding *which* BD user, which reintroduces the
  hardcoded-email problem from D.2. Gives BD exactly one task's worth of relationship per
  project, which is thin but real.
- **(ii)** A `ProgramAssignment`/project-assignment row (Task E), same as Finance and SCM.

There is no third option that derives BD scope from data already in the database.

### F.2 Design Head

**Does `is_design_head` grant project visibility, or only design-workflow authority?**
**Both, and through two separate mechanisms.**

**Visibility — yes, portfolio-wide.** Two independent grants:

- `user_can_view_project()` [permissions.py:114-119](projects/permissions.py#L114-L119) returns
  `True` for **every** project for a flag-holder, checked *before* the role branches and
  explicitly independent of `role`.
- `user_can_view_project_boq()` [permissions.py:275-277](projects/permissions.py#L275-L277)
  does the same for every BOQ — read only; the flag is deliberately absent from
  `user_can_edit_project_boq()` [permissions.py:296-303](projects/permissions.py#L296-L303).

**Authority — yes, and it is the flag's real purpose.** Fifteen endpoints gate on
`user_has_design_head_authority()`, plus `task_assign_design_head`
[views.py:4073](projects/views.py#L4073) which reads `is_design_head` directly, plus site-group
**read** [permissions.py:759](projects/permissions.py#L759), plus quality analytics via the
narrower `user_is_design_head()` [design_views.py:4728](projects/design_views.py#L4728).

**Does the deputy mechanism change that? Yes — it separates the two, and the split is
deliberate and documented.**

`user_is_design_head_deputy()` [permissions.py:400-419](projects/permissions.py#L400-L419) —
the presence of `design_head_deputy` on a profile that is *still* a Head is the whole rule.

- The deputy **does** get: every design action (`user_has_design_head_authority()`
  [permissions.py:436](projects/permissions.py#L436)), portfolio-wide **BOQ read**
  [permissions.py:277-281](projects/permissions.py#L277-L281), design-surface read
  [permissions.py:707](projects/permissions.py#L707), and site-group read
  [permissions.py:759](projects/permissions.py#L759).
- The deputy does **not** get: portfolio-wide **project** visibility. `user_can_view_project()`
  has no deputy branch, and `user_has_design_head_authority()`'s docstring states the reason
  [permissions.py:440-447](projects/permissions.py#L440-L447):

  > *Widening a deputy's read access across the whole portfolio would be a much larger
  > decision than "somebody is covering QC this week."*

- The deputy is also refused quality analytics on purpose — per-designer error rates
  [design_views.py:4666-4680](projects/design_views.py#L4666-L4680).

**So the deputy already demonstrates that design-workflow authority and portfolio project
visibility are separable in this codebase, and that they have been separated once before, on
purpose.**

**Is scoping Design Head by assignment even coherent?** The evidence cuts both ways and is
presented, not resolved:

**Against — three structural reasons:**

1. `is_design_head` is a **flag, not a role**, and migration 0053 removed `'Design Head'` from
   `ROLE_CHOICES` for reasons documented at length
   [models.py:515-534](projects/models.py#L515-L534). An assignment table keyed on
   `UserProfile` can hold a flag-holder fine, but there is no role string to gate on and no
   `@role_required` decorator admits one.
2. The Head's job **is** the portfolio. `design_head_sites`
   [design_views.py:270](projects/design_views.py#L270) is per-tender, but `_qc_scope()`
   [design_views.py:3410-3413](projects/design_views.py#L3410-L3413) deliberately returns an
   unscoped `Q()` for Head authority, with the reason spelled out: *"hiding sites he has
   assigned to somebody else would remove from his screen exactly the work he assigned and is
   accountable for."*
3. `design_bulk_allocate` [design_views.py:788](projects/design_views.py#L788) and
   `design_assign_qc` [design_views.py:910](projects/design_views.py#L910) are the acts that
   *create* everyone else's assignments. Scoping the assigner by assignment is circular.

**For — two reasons:**

1. Per-**tender** scoping is coherent and already half-built: `design_head_sites`,
   `design_tender_dashboard`, `design_qc_dashboard` and `design_quality_analytics_tender` all
   take a `Program` pk. A `ProgramAssignment` row with `assignment_role='design_head'` would
   scope those four naturally, and multiple Heads across multiple tenders is a plausible growth
   path.
2. The Head's **portfolio-wide BOQ read** and **portfolio-wide project visibility** are wider
   than anything the eighteen design views actually need. Every design view resolves its site
   through `_opex_site()` and gates per-site; the two portfolio grants in `permissions.py` are
   there for convenience, not because a view requires them.

**A defensible middle position exists and is worth putting to the product owner:** keep
`is_design_head` portfolio-wide for design *authority*, and scope its portfolio *visibility*
grants (the two branches at [permissions.py:118](projects/permissions.py#L118) and
[:277](projects/permissions.py#L277)) to Programs the Head is assigned to. That is exactly the
split the deputy already embodies.

---

## Task G — `is_deleted` and object-resolution audit

There are no custom managers (§6), so every queryset must carry the filter itself.

**Only five models have an `is_deleted` field:** `Project` [models.py:111](projects/models.py#L111),
`Program` [:237](projects/models.py#L237), `TaskAttachment` [:436](projects/models.py#L436),
`ProjectDocument` [:1046](projects/models.py#L1046), `Comment` [:1165](projects/models.py#L1165).
**`BOQ`, `Issue`, `DeliveryChallan`, `Task` and `DesignAssignment` have none** — for those, the
question is whether the *parent* Project's `is_deleted` is filtered.

`project_delete` [views.py:2426-2434](projects/views.py#L2426-L2434) sets `is_deleted=True` and
`deleted_at`, and **does not change `status`**. A soft-deleted project keeps
`status='Active'`, so it still matches every `status__in=['Active','In Progress']` filter that
does not also say `is_deleted=False`.

### G.1 `Project` resolutions that DO filter (5)

| View | Line | R/W |
|---|---|---|
| `project_delete` | [views.py:2430](projects/views.py#L2430) | W |
| `task_assign_design_head` | [views.py:4074](projects/views.py#L4074) | W |
| `admin_assign_pm` | [views.py:9705](projects/views.py#L9705) | W |
| `subadmin_projects` | [views.py:10409](projects/views.py#L10409) | W |
| `_opex_site()` — every design endpoint | [design_views.py:221](projects/design_views.py#L221) | R/W |

### G.2 `Project` resolutions that do NOT filter (40)

**Writes (30) — the serious ones. Each can mutate a soft-deleted project.**

| View | Line | What it writes to a deleted project |
|---|---|---|
| `project_edit` | [views.py:2445](projects/views.py#L2445) | Full project form (Draft only) |
| `project_field_edit` | [views.py:2503](projects/views.py#L2503) | Capacity, contract value, target date + audit rows |
| `project_activate` | [views.py:2587](projects/views.py#L2587) | **Resurrects it operationally** — status→Active, 52 tasks, 3 milestones |
| `project_recalculate_dates` | [views.py:2648](projects/views.py#L2648) | Every task due date |
| `enable_cascade_scheduling` | [views.py:2676](projects/views.py#L2676) | **Irreversible flag** |
| `task_add` | [views.py:2717](projects/views.py#L2717) | New Task row |
| `task_status_update` | [views.py:3576](projects/views.py#L3576) | Task status, auto-created Issue, notifications |
| `task_detail_status_update` | [views.py:3809](projects/views.py#L3809) | Task status |
| `task_assign` | [views.py:4016](projects/views.py#L4016) | `Task.assigned_to` + notification |
| `task_set_due_date` | [views.py:4127](projects/views.py#L4127) | Due date + cascade |
| `assign_coordinators` | [views.py:4210](projects/views.py#L4210) | M2M membership |
| `boq_detail` (POST branches) | [views.py:4615](projects/views.py#L4615) | **Auto-creates and seeds a BOQ** on GET |
| `opex_boq_entry` | [views.py:4936](projects/views.py#L4936) | Full BOQ reconciliation |
| `opex_boq_upload` | [views.py:5676](projects/views.py#L5676) | BOQ rows from a spreadsheet |
| `boq_submit` | [views.py:5937](projects/views.py#L5937) | BOQ status + `BOQRevision` |
| `boq_acknowledge` | [views.py:6001](projects/views.py#L6001) | BOQ status |
| `boq_request_revision` | [views.py:6038](projects/views.py#L6038) | BOQ status + `BOQRevision` |
| `milestone_create` | [views.py:6264](projects/views.py#L6264) | Three `PaymentMilestone` rows |
| `raise_payment_request` | [views.py:6302](projects/views.py#L6302) | `PaymentRequest` + **Supabase upload** |
| `confirm_payment_request` | [views.py:6398](projects/views.py#L6398) | Payment confirmation |
| `set_milestone_amounts` | [views.py:6652](projects/views.py#L6652) | Milestone amounts |
| `project_overview` (POST) | [views.py:6724](projects/views.py#L6724) | Milestone update + Finance task sync |
| `upload_project_document` | [views.py:7398](projects/views.py#L7398) | **Supabase upload** + row |
| `delete_project_document` | [views.py:7492](projects/views.py#L7492) | Soft-delete |
| `upload_task_attachment` | [views.py:7532](projects/views.py#L7532) | **Supabase upload** + row |
| `delete_task_attachment` | [views.py:7625](projects/views.py#L7625) | Soft-delete |
| `checklist_item_complete` | [views.py:7686](projects/views.py#L7686) | Completion row + **photo upload** |
| `create_project_issue` / `create_task_issue` / `create_delivery_issue` | [:7796](projects/views.py#L7796), [:7890](projects/views.py#L7890), [:7987](projects/views.py#L7987) | Issue rows + **WhatsApp/email** |
| `create_task_comment` | [views.py:8345](projects/views.py#L8345) | Comment |
| `create_delivery_challan` | [views.py:8665](projects/views.py#L8665) | DC + line items |
| `confirm_grn` / `override_grn` | [:8827](projects/views.py#L8827), [:8906](projects/views.py#L8906) | GRN quantities + DC status |

**Reads (10):** `project_detail` [:2430 via redirect](projects/views.py#L2419),
`project_overview` GET [:6724](projects/views.py#L6724),
`opex_boq_download` [:5298](projects/views.py#L5298),
`boq_history` [:6092](projects/views.py#L6092),
`task_detail` [:7340](projects/views.py#L7340),
`project_timeline` [:8493](projects/views.py#L8493),
`delivery_challan_detail` [:8776](projects/views.py#L8776),
`payment_request_detail` [:9104](projects/views.py#L9104),
`my_documents` [:9039-9048](projects/views.py#L9039-L9048),
`design_submission_detail` [:9093](projects/views.py#L9093).

**Highest-severity write path: `project_activate`** [views.py:2587](projects/views.py#L2587).
It requires `status == 'Draft'` and `project_delete` never changes `status`, so a soft-deleted
Draft is still activatable — creating 52 tasks and 3 milestones on a record the Admin believes
is gone, and firing an assignment notification to the Finance assignee.

**Second: `enable_cascade_scheduling`** [views.py:2676](projects/views.py#L2676) — the flag is
documented as irreversible [models.py:108-111](projects/models.py#L108-L111).

**Third: the three Supabase upload paths** — a deleted project gains real stored objects that
`purge_deleted_files` will not have queued.

### G.3 `Program` resolutions

**All ten filter `is_deleted=False`.** `views.py` [:2830](projects/views.py#L2830),
[:2879](projects/views.py#L2879), [:2912](projects/views.py#L2912),
[:2999](projects/views.py#L2999), [:3191](projects/views.py#L3191),
[:3327](projects/views.py#L3327); `design_views.py` [:276](projects/design_views.py#L276),
[:797](projects/design_views.py#L797), [:3937](projects/design_views.py#L3937),
[:4002](projects/design_views.py#L4002), [:4322](projects/design_views.py#L4322),
[:4703](projects/design_views.py#L4703). **No finding.**

### G.4 `BOQ` (4 resolutions)

All four are `get_object_or_404(BOQ, project=project)` — [:5954](projects/views.py#L5954)
(`boq_submit`, W), [:6011](projects/views.py#L6011) (`boq_acknowledge`, W),
[:6045](projects/views.py#L6045) (`boq_request_revision`, W),
[:6098](projects/views.py#L6098) (`boq_history`, R). The `project=project` term is a correct
cross-project guard, but the `project` it scopes to was itself resolved without an `is_deleted`
filter, so all four inherit G.2.

### G.5 `Issue` (7 resolutions) — **none filter, and there is nothing to filter on**

`_issue_base_qs()` [views.py:7772](projects/views.py#L7772) plus six bare
`get_object_or_404(Issue, pk=issue_id)` calls at [:8128](projects/views.py#L8128),
[:8168](projects/views.py#L8168), [:8237](projects/views.py#L8237),
[:8270](projects/views.py#L8270), [:8304](projects/views.py#L8304),
[:8406](projects/views.py#L8406).

`Issue` has no `is_deleted` field and none of the seven adds `project__is_deleted=False`.
**Six of the seven are writes.** Combined with Task B, this means any authenticated user can
resolve, reassign or comment on an issue attached to a project that was deleted months ago —
and `resolve_issue` will send WhatsApp and email about it.

### G.6 `DeliveryChallan` (4 resolutions) — none filter

[:7995](projects/views.py#L7995) (`create_delivery_issue`, W),
[:8788](projects/views.py#L8788) (`delivery_challan_detail`, R),
[:8831](projects/views.py#L8831) (`confirm_grn`, **W**),
[:8910](projects/views.py#L8910) (`override_grn`, **W**).

All four are `get_object_or_404(DeliveryChallan, pk=dc_id)` followed by a manual
`challan.project.project_id != project_id` cross-project guard. The guard is correct; the
`is_deleted` term is absent from both sides.

### G.7 Counts

| Model | Resolutions | Filter `is_deleted` | Do not | of which **writes** |
|---|---:|---:|---:|---:|
| Project | 45 | 5 | **40** | **30** |
| Program | 12 | 12 | 0 | 0 |
| BOQ | 4 | 0 (inherit) | 4 | 3 |
| Issue | 7 | 0 | **7** | **6** |
| DeliveryChallan | 4 | 0 | **4** | **3** |
| **Total** | **72** | **17** | **55** | **42** |

---

## Task H — Decorator inventory

**64 `@role_required` / `@system_admin_required` uses** across `views.py` and
`design_views.py`. `design_views.py` uses **none** — every one of its 40 endpoints is
`@login_required` plus a named `permissions.py` helper.

### H.1 Distinct usages

| Decorator | Admits | Count |
|---|---|---:|
| `@role_required(['Admin'])` | Admin | 33 |
| `@role_required(['PM', 'Project Coordinator'])` | PM, Project Coordinator | 7 |
| `@role_required(['Admin', 'PM'])` | Admin, PM | 5 |
| `@system_admin_required` | System Admin | 3 |
| `@role_required(['SCM'])` | SCM | 3 |
| `@role_required(['Finance'])` | Finance | 3 |
| `@role_required(['Site Engineer'])` | Site Engineer | 2 |
| `@role_required(['Admin', 'PM', 'CEO'])` | Admin, PM, CEO | 2 |
| `@role_required(['PM'])` | PM | 1 |
| `@role_required(['PM', 'Admin', 'CEO'])` | PM, Admin, CEO | 1 |
| `@role_required(['Design'])` | Design | 1 |
| `@role_required(['BD'])` | BD | 1 |
| `@role_required(['BD', 'PM'])` | BD, PM | 1 |
| `@role_required(list(LANDING_ROLES))` | CEO, Finance, SCM | 1 |

Plus four inline role tuples that are role gates in all but name: `vendor_*` ×4
(`role not in ('SCM','Admin')`, [views.py:4287](projects/views.py#L4287)),
`payment_request_detail` [views.py:9107](projects/views.py#L9107),
`delivery_challan_detail` [views.py:8779](projects/views.py#L8779),
`raise_payment_request` [views.py:6299](projects/views.py#L6299),
`confirm_payment_request` [views.py:6395](projects/views.py#L6395),
`boq_acknowledge` [views.py:6008](projects/views.py#L6008).

### H.2 Views with a decorator, no object-level check, and a URL parameter

**Flagged — a role gate without a scope gate:**

| View | Decorator | Object from URL | R/W | Verdict |
|---|---|---|---|---|
| **`confirm_grn`** [views.py:8815](projects/views.py#L8815) | `['Site Engineer']` | `dc_id` | **W** | **Real leak.** Exactly D-4's failure: this decorator passes a Site Engineer into another engineer's site. |
| **`set_milestone_amounts`** [views.py:6642](projects/views.py#L6642) | `['BD', 'PM']` | `project_id` | **W** | **Real leak.** The only `@role_required(['PM', ...])` view in the file that never calls `_pm_owns_project()`. |
| `milestone_invoice` [views.py:6166](projects/views.py#L6166) | `['Finance']` | `milestone_pk` | W | Deliberate today; becomes a leak the moment Finance is scoped. |
| `milestone_receive` [views.py:6192](projects/views.py#L6192) | `['Finance']` | `milestone_pk` | W | Same. |
| `create_delivery_challan` [views.py:8658](projects/views.py#L8658) | `['SCM']` | `project_id` | W | Deliberate; relationship-creating (D.4). |
| `override_grn` [views.py:8894](projects/views.py#L8894) | `['SCM']` | `dc_id` | W | Deliberate today. |
| `project_delete` [views.py:2426](projects/views.py#L2426) | `['Admin']` | `project_id` | W | Admin unrestricted — correct. |
| `program_delete` [views.py:2907](projects/views.py#L2907) | `['Admin']` | `pk` | W | Correct — and additionally guarded by a live-site count [:2914](projects/views.py#L2914). |
| 12 `@role_required(['Admin'])` checklist / BOQ-master / user views | `['Admin']` | various | W | Admin-only master data — correct. |

**Not flagged — decorator plus a real object check (14):** `project_edit`,
`project_activate`, `project_recalculate_dates`, `task_add`, `task_assign`, `milestone_create`
(all `_pm_owns_project`); `program_detail`, `program_edit`, `opex_site_create`,
`opex_site_bulk_upload`, `opex_site_bulk_template` (all `_can_access_program`);
`admin_assign_pm`, `subadmin_projects`, `user_edit` (Admin/System Admin scope by design).

### H.3 Two structural notes on the decorator itself

1. **`role_required` fails open for a profile-less user.**
   [decorators.py:104-109](projects/decorators.py#L104-L109):

   ```python
   except Exception:
       logger.warning(...)
       role = 'Admin'
   ```

   A Django superuser created with `createsuperuser` has no `UserProfile`, so **every one of
   the 33 `@role_required(['Admin'])` screens admits them**. Meanwhile every `permissions.py`
   helper returns `False` for the same user, by explicit `getattr(user, 'profile', None)`
   guards. The decorator layer and the permissions layer have opposite failure modes.
   `get_user_dashboard()` has the same fallback [decorators.py:51-55](projects/decorators.py#L51-L55).

2. **`role_required` denies with a redirect, not a 403.**
   [decorators.py:110-112](projects/decorators.py#L110-L112) — `messages.error` plus
   `redirect(get_user_dashboard(...))`. Every `permissions.py`-gated view returns
   `HttpResponseForbidden`. Two denial semantics in one product; worth unifying in 0.2 so a
   probe cannot distinguish "wrong role" from "not yours".

---

## Findings, ranked

Severity ordering: **write** on data you have no relationship to outranks **read**; a write
that sends an outbound notification or creates an irreversible state outranks one that does not;
breadth of the admitted population breaks ties.

### 1 — Any Site Engineer can confirm a GRN on any project · **WRITE** · CRITICAL

`confirm_grn` [views.py:8814-8815](projects/views.py#L8814-L8815). Gate is
`@role_required(['Site Engineer'])` and nothing else — no project relationship test anywhere in
the view. Writes `received_quantity`, `damaged_quantity`, `condition`, `grn_notes`,
`grn_confirmed_by`, `grn_date`, then calls `recalculate_dc_status()`. The DC is cross-checked
against the URL project [:8831-8833](projects/views.py#L8831-L8833), but the project itself is
unguarded. **Exposed to:** every Site Engineer, on every delivery challan in the portfolio.
This is D-4's stated failure mode, verbatim, and it ships before any site engineer receives a
login only if 0.2 fixes it.

### 2 — Any authenticated user can resolve, assign or comment on any Issue on any project · **WRITE** · CRITICAL

`resolve_issue` [views.py:8172](projects/views.py#L8172), `assign_issue`
[:8308](projects/views.py#L8308), `update_issue_status` [:8132](projects/views.py#L8132),
`create_issue_comment` [:8411](projects/views.py#L8411), plus the three Issue-creation
endpoints. All eight carry only the PM-only stanza. `resolve_issue` additionally **sends
WhatsApp and email** to every project manager, the assignee and the raiser
[:8206-8218](projects/views.py#L8206-L8218). `assign_issue` accepts any `UserProfile` pk with
no filter [:8306](projects/views.py#L8306). **Exposed to:** every authenticated user of every
role except PM — including System Admin, whom `permissions.py` deliberately grants nothing.
Close and reopen are correctly guarded; nothing else is.

### 3 — Any role-matching user can change task status and due dates on any project · **WRITE** · CRITICAL

`task_status_update` [views.py:3602](projects/views.py#L3602),
`task_set_due_date` [:4137](projects/views.py#L4137),
`checklist_item_complete` via `_user_can_complete_checklist_item`
[:2349-2367](projects/views.py#L2349-L2367). All three read
`normalised_user_role == task.assigned_role OR is_pm` — the role branch carries no project
term. **Exposed to:** every Site Engineer for every SE task, every SCM user for every SCM task,
every Finance user for every Finance task, every Design user for every Design task, every BD
user for the one BD task, portfolio-wide. Includes marking **Plant Commissioning** Done, which
is the M3 payment-milestone notification trigger.

### 4 — 30 write paths can mutate or resurrect a soft-deleted project · **WRITE** · HIGH

Task G.2. `project_delete` [views.py:2426-2434](projects/views.py#L2426-L2434) sets
`is_deleted=True` and leaves `status` untouched. `project_activate`
[:2587](projects/views.py#L2587) requires only `status == 'Draft'`, so a deleted Draft can be
activated — 52 tasks, 3 milestones, and an assignment notification on a record the Admin
believes is gone. `enable_cascade_scheduling` [:2676](projects/views.py#L2676) sets a flag
documented as irreversible. Three Supabase upload paths ([:7398](projects/views.py#L7398),
[:7532](projects/views.py#L7532), [:7686](projects/views.py#L7686)) create stored objects the
purge command will never see.

### 5 — Any PM or BD can rewrite milestone amounts on any project · **WRITE** · HIGH

`set_milestone_amounts` [views.py:6640-6652](projects/views.py#L6640-L6652).
`@role_required(['BD','PM'])` with no `_pm_owns_project()` call anywhere in the body. Writes
M1/M2/M3 agreed amounts — the contractual figures the Finance dashboard, the BD dashboard and
the CEO finance cards all read. It is the only `@role_required(['PM', ...])` project-scoped
view in `views.py` that omits the ownership check.

### 6 — Every role except PM can open any project's full overview page · **READ** · HIGH

`project_overview` [views.py:6734-6735](projects/views.py#L6734-L6735). One page carrying the
info card, financials, every milestone, every task, every document, the Gantt, the DC list and
the issue list. `task_detail` [:7345](projects/views.py#L7345), `project_timeline`
[:8497](projects/views.py#L8497) and `issue_detail` [:8093](projects/views.py#L8093) are the
same stanza on the same terms. `payment_requests` is separately narrowed to
`('Finance','PM','SCM','Admin')` [:7027](projects/views.py#L7027) — the only field-level
narrowing on the page.

### 7 — Any authenticated user can upload files to any project · **WRITE** · HIGH

`upload_project_document` [views.py:7401](projects/views.py#L7401) and
`upload_task_attachment` [:7535](projects/views.py#L7535). PM-only stanza. Both write real
objects to Supabase and create rows attributed to the uploader. Deletion is correctly guarded
(uploader or Admin, [:7500](projects/views.py#L7500), [:7634](projects/views.py#L7634)) — which
means an unrelated user can upload a file that only they or an Admin can remove.

### 8 — `dashboard_ceo` has no role gate · **READ** · HIGH

[views.py:2210-2212](projects/views.py#L2210-L2212). `@login_required` only, despite the
docstring reading *"Access: CEO role only"*. Every authenticated user — Site Engineer included
— reaches the full portfolio: contract values, at-risk classification, department rollups,
resolution-time KPIs. It is the only dashboard in the product missing its decorator.

### 9 — `tasks_drill_down` falls through to the whole portfolio for six roles · **READ** · MEDIUM

[views.py:397-415](projects/views.py#L397-L415). No `@role_required`. Explicit branches for
PM/PC, Design and Site Engineer; the trailing comment says *"SCM and others: all active
non-deleted projects"*. SCM, Finance, BD, CEO, Admin and System Admin all fall through. This is
also a **third** copy of the PM-or-coordinator scoping rule, after `dashboard_pm`
[:469](projects/views.py#L469) and `permissions.user_can_manage_project()`.

### 10 — System Admin has near-complete access through a permissions module that grants it nothing · **READ + WRITE** · MEDIUM

`user_can_view_project()` gives System Admin no branch and falls through to management authority
alone [permissions.py:150](projects/permissions.py#L150). But it is not named in the PM-only
stanza, so all 18 "none"-class endpoints admit it. Compounded by
[decorators.py:104-109](projects/decorators.py#L104-L109), where a profile-less user is treated
as `'Admin'` and passes all 33 Admin screens.

### 11 — `payment_request_detail` admits any PM to any project's vendor invoice · **READ** · MEDIUM

[views.py:9102-9107](projects/views.py#L9102-L9107). Role allowlist
`('SCM','Finance','PM','Admin')` with no project term. Exposes invoice number, amount, vendor,
and the Supabase document URL.

### 12 — `delivery_challan_detail` admits every Site Engineer and every Coordinator to every DC · **READ** · MEDIUM

[views.py:8779-8786](projects/views.py#L8779-L8786). The role allowlist names
`'Site Engineer'` and `'Project Coordinator'`; the PM-isolation line immediately below it names
only `'PM'`. Also renders `all_profiles` — the full active-user directory
[:8811](projects/views.py#L8811).

### 13 — Project Coordinator is scoped on its dashboard and unscoped everywhere else · **READ + WRITE** · MEDIUM

The stanza compares `role == 'PM'`; a coordinator's role string is `'Project Coordinator'`.
`user_can_manage_project()` treats them as PM-equivalent
[permissions.py:60](projects/permissions.py#L60), so every helper-gated view is correct — and
every stanza-gated view is not. This is the sharpest instance of prompt 0.1's finding.

### 14 — Seven Issue endpoints and four DeliveryChallan endpoints operate on soft-deleted projects · **WRITE** · MEDIUM

Task G.5 and G.6. Neither model has an `is_deleted` field and neither adds
`project__is_deleted=False`. Six of the seven Issue resolutions and three of the four DC
resolutions are writes.

### 15 — Two denial semantics, and one fails open · **STRUCTURAL** · LOW-but-load-bearing

`role_required` redirects with a flash message [decorators.py:110-112](projects/decorators.py#L110-L112)
and treats a missing profile as Admin [:104-109](projects/decorators.py#L104-L109);
`permissions.py` helpers return `HttpResponseForbidden` and treat a missing profile as False.
Any 0.2 that mixes the two mechanisms inherits both failure modes.

---

## Blocking questions

**These four cannot be resolved from the code. Prompt 0.2 needs answers before it is written.**

### Q1 — Finance and SCM: what is the assignment source of truth on Residential? *(from D.1/D.2 — the blocking finding)*

Task-based scoping gives **SCM exactly zero** visibility (all 11 SCM template tasks are created
`assigned_to = NULL` and nothing ever assigns them, [utils.py:875](projects/utils.py#L875)) and
gives **Finance one hardcoded person everything** (all 6 Finance tasks are back-assigned to
`santosh@horizonrenewablepower.com`, [utils.py:679](projects/utils.py#L679)) and every other
Finance user nothing. There is no third source of truth in the database.

Pick one:

- **(a)** Ship the assignment table (Task E) and populate it for Finance and SCM before
  narrowing them. Correct, and the largest.
- **(b)** Narrow Finance and SCM to assignment scope and accept empty dashboards until the
  table is populated. Honest, and unusable on day one.
- **(c)** **Defer the Finance/SCM narrowing out of 0.2 entirely**, fix the 18 unscoped
  endpoints and the `is_deleted` gaps, and revisit Finance/SCM once the assignment table exists.
  *This is the recommendation — see the proposed scope below.*

### Q2 — Do `boq_acknowledge`, `create_delivery_challan` and `raise_payment_request` stay role-gated regardless?

All three are **relationship-creating** acts: SCM acknowledges a BOQ they did not author, then
raises the first delivery challan, and only then does any relationship exist. Scoping them
inverts the causality, and `boq_acknowledge`'s own docstring already forbids it
[views.py:6005-6007](projects/views.py#L6005-L6007): *"Do not 'harden' this to match the Design
gates."* Confirm that these three are permanently exempt, or name what relationship they should
require.

### Q3 — Backfill policy for Finance and SCM *(from E.5)*

`manager` and `coordinator` rows are derivable from `assigned_pm` / `coordinators` and reproduce
today's behaviour exactly. **Finance and SCM are not derivable — there is no field to derive
them from.** Backfill everyone onto everything (zero behavioural change, defers the real
decision) or backfill nothing (empty dashboards until somebody fills the screen in)?

### Q4 — Is `is_deleted` a 0.2 concern or its own prompt?

55 of 72 object resolutions omit the filter, 42 of them writes (G.7). It is a genuine
correctness defect and it is **not** access isolation. Fixing it inside 0.2 doubles that prompt's
diff and couples two unrelated rollbacks — R-12 territory. Recommend it becomes prompt 0.2b with
its own review, **except** for the resolutions 0.2 is already editing, where adding
`is_deleted=False` in the same line is free.

**Two smaller decisions that are not blocking but should be made in the same sitting:**

- **BD** (F.1) — no per-user term exists anywhere. Assignment scoping empties `dashboard_bd`
  completely. Options: assign the one BD task at activation, or an assignment row, or leave BD
  portfolio-wide as settled.
- **Design Head** (F.2) — the flag grants portfolio visibility *and* design authority through
  two separate mechanisms, and the deputy already demonstrates they are separable. A defensible
  middle: keep authority portfolio-wide, scope the two visibility grants per-Program.

---

## Proposed scope for prompt 0.2 — a proposal for approval

Ordered by dependency. Steps 1–3 are the ones that make D-4 true; 4–6 are the smaller leaks;
7 is deferred by recommendation.

### Step 1 — one helper, applied to the eighteen unscoped endpoints

**No new model. No migration. No policy change.** Replace every instance of

```python
if profile.role == 'PM' and not user_can_manage_project(request.user, project):
    raise Http404
```

with a single call to the existing `permissions.user_can_view_project()` (for reads) or a new
`user_can_act_on_project()` (for writes). Under today's policy — Finance/SCM/CEO/Admin/BD stay
portfolio-wide — this changes behaviour for exactly four populations, all of them intended:
Project Coordinator, Site Engineer, Design and System Admin stop reaching projects they hold no
relationship to.

18 endpoints, 13 of them writes. **This is the largest risk reduction in the smallest diff, and
it needs none of the blocking answers.**

### Step 2 — the eight Issue endpoints

Same fix, applied to the block Task B settled: `create_project_issue`, `create_task_issue`,
`create_delivery_issue`, `issue_detail`, `update_issue_status`, `resolve_issue`,
`assign_issue`, `create_issue_comment`. Called out separately from Step 1 because they resolve
the project **through** `issue.project` rather than from the URL, so the edit shape differs, and
because `resolve_issue` sends outbound notifications and deserves its own verification.

Also narrow `all_profiles` [views.py:8098](projects/views.py#L8098) — the full active-user
directory has no business being on an issue page.

### Step 3 — the three role-gates that need a scope gate

- `confirm_grn` [views.py:8815](projects/views.py#L8815) — add
  `user_can_view_project()` (or, better, a task-holding test) beside the SE role gate.
  **Finding 1; do not ship a site-engineer login without it.**
- `set_milestone_amounts` [views.py:6642](projects/views.py#L6642) — add
  `user_can_manage_project()` for the PM arm. The BD arm needs Q-decision-BD first, so gate the
  PM arm now and leave BD as-is with a comment.
- `payment_request_detail` [views.py:9102](projects/views.py#L9102) — add
  `user_can_view_project()` beside the role allowlist.

### Step 4 — the two missing dashboard gates

- `dashboard_ceo` [views.py:2210](projects/views.py#L2210) — add `@role_required(['CEO'])`.
  One line; the docstring already claims it.
- `tasks_drill_down` [views.py:381](projects/views.py#L381) — give the fall-through an explicit
  branch instead of a trailing comment, and **route its PM/PC term through
  `permissions.py`** rather than keeping a third copy of the rule.

### Step 5 — close the `role_required` fail-open

[decorators.py:104-109](projects/decorators.py#L104-L109) — a profile-less user is treated as
`'Admin'`, which contradicts every `permissions.py` guard. Change it to deny, and unify the
denial response with `HttpResponseForbidden`. Small, but everything above rests on it.

### Step 6 — R-13 compliance sweep

`permissions.py` says *"do not compare role strings AT A CALL SITE"*
[permissions.py:16-21](projects/permissions.py#L16-L21). After Steps 1–4, audit what remains:
`_is_project_pm` [views.py:7785](projects/views.py#L7785) (already routes correctly),
`_user_can_complete_checklist_item` [views.py:2349](projects/views.py#L2349), the
`{'BD': 'BD / Sales'}` normalisation duplicated at [:2364](projects/views.py#L2364),
[:3599](projects/views.py#L3599) and [:4134](projects/views.py#L4134) — §4 says
`_PROFILE_TO_TASK_ROLE` is consolidated in **prompt 1.2**, so leave the three copies and record
that they are known.

### Step 7 — deferred out of 0.2, by recommendation

- **Finance and SCM narrowing** — blocked on Q1. Every Finance and SCM view returns empty or
  403 today under assignment scoping (D.3), and there is no data to scope on. Recommend
  Q1 option (c): keep them portfolio-wide in 0.2, and revisit once `ProgramAssignment` exists.
- **`ProgramAssignment`** — R-1 requires it be its own prompt with its own reviewed migration.
  Task E is the input to that prompt, not part of this one.
- **`is_deleted`** — blocked on Q4. Recommend prompt 0.2b, with `is_deleted=False` added
  opportunistically to any resolution Steps 1–4 already touch.
- **BD and Design Head** — blocked on the two smaller decisions above. Neither is touched by
  Steps 1–6, so both stay exactly as they are today.

### What this proposal deliberately does NOT do

- No new model, no migration, no schema change (R-1).
- No change to `PORTFOLIO_VIEW_ROLES`, the BD branch, or either Design Head branch.
- No change to the design module — it is already correct (A.3) and nothing in Steps 1–6
  touches `design_views.py`.
- No entries added to `EXECUTION_MODULE_DEFERRED.md`, per the prompt's instruction. Everything
  a future session needs is above; which items become deferred is the product owner's call.
