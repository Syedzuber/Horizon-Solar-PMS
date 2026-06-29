DATE: 08-June-2026
DAY: 1 of 24

COMPLETED TODAY:
- Login system with role-based redirect (all 7 roles)
- UserProfile model with role choices
- User management UI (create, edit, deactivate)
- role_required and login_required decorators
- Placeholder dashboards for all 7 roles
- Data migration for superuser UserProfile
- Deployed and tested on Railway

CURRENT STATE:
- All 7 role logins working on production URL
- User creation, edit, deactivation working
- All 7 dashboard placeholders loading correctly
- Migrations applied to Railway PostgreSQL

KNOWN ISSUES / INCOMPLETE:
- User edit was not working , but corrected in one attempt

NEXT SESSION GOAL:
Project Creation (manual form)

TASK TEMPLATE — LOCKED DECISIONS:
- 51 tasks, 9 phases, Residential only
- Project ID format: HRP-RES-2026-001
- Capacity field name: capacity_kw
- Design is a valid assigned_role on Task model
- O&M deferred to Phase 2
- OPEX/CAPEX: tasks added manually post-activation
- Payment milestones embedded at tasks 2, 28, 51
- Board demo trigger: Task 'Plant Commissioning'
  Phase 8, assigned to Site Engineer

PHASE 2 BACKLOG:
- Task dependencies (TaskDependency model,
  predecessor validation on status update,
  blocked state auto-assignment)
  Trigger: after board demo, informed by
  pilot project data from champion PM


DATE: 09-June-2026
DAY: 2 of 24

COMPLETED TODAY:
- Project model with capacity_kw, project_type,
  all fields deployed to Railway PostgreSQL
- ProjectPhase model (9 phases)
- Task model with 6 role choices including Design
- Project creation form (PM only)
- Project detail view with phase/task grouping
- Project list view (role-filtered)
- Activate Project with 51-task residential template
- Manual task-add for OPEX/CAPEX
- Task status update with role-based permission
- Project ID format: HRP-RES-2026-001
- All 7 views deployed and tested on production

VERIFIED ON PRODUCTION:
- Role distribution: BD/Sales=1, Design=6,
  Finance=3, PM=14, SCM=12, Site Engineer=15
- Task count assertion passing at 51
- HRP prefix confirmed in project IDs
- PM isolation working (404 on other PM projects)
- OPEX/CAPEX manual task add working

KNOWN ISSUES / INCOMPLETE:
everything working fine, Procfile and old model for project created issue, but resolved and both updated

DAY 3 READY TO BUILD:
- Prompt saved at: day3_claude_code_prompt.txt
- Task model needs: duration_days, task_type
- 39 internal tasks, 12 external tasks
- PM, SE, Design, SCM dashboards
- Full prompt ready, start fresh thread

DATE: 10-June-2026
DAY: 3 of 24
COMPLETED TODAY:
- duration_days, task_type fields on Task model
- assigned_to ForeignKey (PM/SE pre-assigned on activation)
- add_workdays() — calendar days, no weekend skip
- calculate_due_dates() — chain from activation date
- Recalculate Due Dates button (backfills existing projects)
- PM dashboard — 4 summary cards, internal/external split
- Site Engineer dashboard — assigned tasks view
- Design dashboard — role-filtered tasks
- SCM dashboard — role-filtered tasks
- Assign modal — inline, dropdown filtered by role
VERIFIED:
- Due dates calculating correctly from activation date
- Assign modal populates by role
- All 4 dashboards loading
NEXT SESSION GOAL:
- Day 4: Issues & Comments module + Finance dashboard


DATE: 11-June-2026
DAY: 4 of 24

COMPLETED DAYS 1-3:
- Login, roles, user management (Day 1)
- Project creation, 51-task residential
  template, task tracking, project IDs (Day 2)
- duration_days, task_type, assigned_to
  on Task model (Day 3)
- Calendar-day due date calculation,
  no weekend skip (Day 3)
- Due date calendar picker — PM sets
  start date, chain calculates from that (Day 3)
- Due date editing — PM edits any task,
  following tasks recalculate using
  Option B (absolute from new date) (Day 3)
- Recalculate Due Dates backfill button (Day 3)
- PM, SE, Design, SCM dashboards live (Day 3)
- Assign modal — inline, filtered by role (Day 3)

VERIFIED ON PRODUCTION:
- Assign modal populates correctly by role
- Due dates calculating from activation date
- All 4 dashboards loading
- Calendar days, no weekend skip confirmed

DAY 4 GOAL:
Build in this exact order:
1. Dashboard fixes (do these first)
2. Vendor Master
3. BOQ Submission

DASHBOARD FIXES REQUIRED:
- All cards: col-md-6, side by side,
  max-height 350px with scroll
- Remove Task Type column from all
  4 dashboard task tables
- Replace due date inline alerts with
  static bell icon in navbar (placeholder)
  Notification model deferred to Day 5
- SCM PO list: group by project,
  chronological within each group
- SCM BOQ card: overall % progress bar
  + category pills (Done/Pending)
  for Solar Modules, Structure,
  Inverter, BOS

NEW MODELS TO BUILD DAY 4:
- VendorCategory (pre-populated:
  Solar Modules, Structure, Inverter,
  BOS, Services, Other)
- Vendor (name, contact_person, phone,
  email, gst_number, msme_status,
  msme_number, address, categories M2M,
  is_active, created_by)
- BOQ (OneToOne to Project, status,
  version, submitted_by, submitted_at,
  notes)
- BOQItem (boq FK, serial_no, category,
  description, make_preference vendor FK,
  uom, boq_quantity, ordered_quantity,
  ordered_vendor FK, is_standard_item)
- BOQRevision (boq FK, version, reason,
  snapshot JSONField, revised_by,
  revised_at)

KEY DECISIONS LOCKED:
- Vendor categories: Solar Modules,
  Structure, Inverter, BOS, Services,
  Other
- One vendor can have multiple categories
- Vendors have Active/Inactive toggle
- No vendor delete — only deactivate
- BOQ pre-populated with 37 standard
  items from real company BOQ file
- Standard items cannot be deleted
  from BOQ — only quantity left blank
- make_preference = vendor FK
  (not free text)
- Vendor dropdown filtering by category
  uses vanilla JS with json_script tag
- Category change re-filters dropdowns
  (Option B — applies to all rows)
- BOQ revision history via JSONField
  snapshot on every submission
- SCM fills ordered_quantity and
  ordered_vendor progressively
- Variance highlighted amber if
  ordered_qty differs from boq_qty
- PM can request revision with reason

WHATSAPP:
- Interakt account already active
  (replaces Twilio in all future prompts)
- Create commissioning notification
  template in Interakt before Week 3

INTEGRATIONS STATUS:
- Railway PostgreSQL: active
- SendGrid: pending (Week 3)
- Interakt WhatsApp: account active,
  template creation pending
- Zoho CRM: Day 5
- Supabase file storage: Days 6-8

PROMPT FILES SAVED LOCALLY:
- day4_claude_code_prompt.txt ← USE THIS

NEXT SESSION GOAL:
Day 5 — Strengthning of Onformation low among the roles 
		Better info display and actionable items on 
		PM, Design and SCM dashboards


DATE: 11-June-2026
DAY: 5 of 24

COMPLETED TODAY:
- assigned_design ForeignKey on Project model
  (nullable, limit_choices_to role=Design,
  SET_NULL on delete, related_name=
  design_projects)
- Migration 0012: assigned_design field
- PM can assign Design member from project
  detail page (dropdown, active Design users
  only, editable when project Active/In Progress)
- On assigned_design save: bulk update
  assigned_to on all incomplete Design-role
  tasks for that project in one .update() call
- On assigned_design cleared: assigned_to
  set to None on same tasks
- Design dashboard now filters by
  phase__project__assigned_design=current_user
  (was: all active projects with Design tasks)
- Design dashboard empty state: "No projects
  assigned to you yet." when no projects assigned
- PM dashboard: BOQ status column added to
  project list table — coloured badge per project
  with direct link to /projects/<id>/boq/
  Badge colours: Draft=gray, Submitted=blue,
  Acknowledged=green, Revision Requested=amber,
  No BOQ=plain dash
- pm_dashboard view annotates boq_status and
  boq_url on each project via try/except
  (never accesses project.boq in template)
- SCM dashboard: "POs Pending" card replaced
  with "BOQ awaiting action" (count of
  Submitted BOQs)
- SCM dashboard: BOQ status list section —
  queries BOQ objects directly with
  select_related('project'), shows Project ID,
  Customer, status badge, "View BOQ →" link
- Design dashboard: "Revision Requested"
  summary card (filtered to current user's
  assigned projects only)
- Design dashboard: BOQ status list section —
  shows all assigned projects with BOQ status
  badge and "View BOQ →" link, including
  projects with no BOQ (not started)

MIGRATIONS APPLIED:
- 0012: assigned_design FK on Project

LOCKED DECISIONS:
- Design assignment is project-level FK,
  set by PM post-activation, optional
- SCM remains a team entity — no project-level
  assignment, all SCM users see all active
  projects (correct behaviour)
- Design Head (Phase 2): will take over
  assigned_design from PM. New 8th role.
  Dashboard: all projects, assigns by
  availability and complexity
- BOQ URL always uses project.project_id
  (string e.g. HRP-RES-2026-001), never
  project.pk

KNOWN GOTCHAS (new, carry forward):
- project.boq raises RelatedObjectDoesNotExist —
  always annotate in view, never in template
- For SCM BOQ list: query BOQ objects directly
  (not projects) to avoid RelatedObjectDoesNotExist
- For Design BOQ list: query projects then
  annotate (Design must see projects with no BOQ)
- assigned_design form: use explicit
  ModelChoiceField queryset, do not rely on
  limit_choices_to alone for form rendering
- Bulk task update: use .update() not loop
  of .save() calls

BACKLOG NOTES (do not build yet):
- PM dashboard: append delivery status
  (materials arrived/pending) to project list
  alongside BOQ badge when SCM delivery
  tracker is built
- CEO dashboard action items: spec in Week 3
  when Finance and Issues modules exist.
  Candidate actions: contract value approval,
  milestone payment release, On Hold resumption,
  high-value procurement sign-off

INTEGRATIONS STATUS:
- Railway PostgreSQL: active
- SendGrid: pending (Week 3)
- Interakt WhatsApp: account active,
  template creation pending — DO THIS NOW,
  2-5 day verification window
- Zoho CRM: deferred (not demo-blocking)
- Supabase file storage: Days 6-8

PROMPT FILES SAVED LOCALLY:
- day5_claude_code_prompt.md ← USE THIS

NEXT SESSION GOAL:
Day 5 — Finance dashboard (M1/M2/M3 milestones)
		Individual Project Dashboard
		
		
# PROJECT_CONTEXT.md — Day 6 Updates
# Date: 12 June 2026
# Paste this into your existing PROJECT_CONTEXT.md, replacing the relevant sections

---

## Current migration state
**Latest migration: 0014**
- 0013 — PaymentMilestone model + commissioned_at on Project
- 0014 — customer_contact_person + zoho_deal_id on Project
  (Note: 0014 had a deployment issue — migration file was missing from git commit.
  Fixed manually. Always verify migration files are committed before pushing.)

---

## Completed build days
- Day 1: Auth + role-based redirect (7 roles)
- Day 2: Project model, auto-IDs, Draft→Activate, 51-task residential template
- Day 3: Role dashboards (PM, SE, Design, SCM), duration fields, due date cascade (Option B)
- Day 4: Vendor Master, BOQ submission workflow, BOQRevision history
- Day 5: (completed in two sessions, counted as Day 5 + Day 6 in sprint)
  Session 1: Finance dashboard, BD dashboard (basic), Individual project overview
  Session 2: Zoho CRM webhook integration

---

## Models — additions from Day 6

### PaymentMilestone (new — migration 0013)
```python
class PaymentMilestone(models.Model):
    STATUS_CHOICES = [('Pending','Pending'), ('Invoiced','Invoiced'), ('Received','Received')]
    NAME_CHOICES = [('M1','M1'), ('M2','M2'), ('M3','M3')]

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='milestones')
    milestone_name = models.CharField(max_length=10, choices=NAME_CHOICES)
    milestone_description = models.CharField(max_length=100, default='')
    amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    amount_received = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    variance_reason = models.CharField(max_length=255, blank=True, default='')
    due_date = models.DateField(null=True, blank=True)
    invoice_date = models.DateField(null=True, blank=True)
    received_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='Pending')
    created_by = models.ForeignKey(UserProfile, null=True, blank=True, on_delete=models.SET_NULL)

    class Meta:
        ordering = ['milestone_name']
```

M1/M2/M3 correspond conceptually to tasks 2, 28, 51 (survey, material supply, commissioning).
NO task FK — execution tracking and financial tracking are intentionally separate.
Notification engine (Week 3) will bridge these two systems.

### Project model — new fields (migrations 0013 + 0014)
```python
commissioned_at = models.DateField(null=True, blank=True)        # migration 0013
customer_contact_person = models.CharField(max_length=255, blank=True, default='')  # migration 0014
zoho_deal_id = models.CharField(max_length=100, blank=True, default='')             # migration 0014
```

commissioned_at: set when project status changes to 'Commissioned'.
zoho_deal_id: stores Zoho Record Id for duplicate webhook guard.
customer_contact_person: individual contact name when customer is a company (Account Name used as customer_name).

---

## New views and URLs

### Finance dashboard — /dashboard/finance/
- @role_required(['Finance'])
- Summary cards: Total Pending (₹), Total Invoiced (₹), Total Received (₹), Overdue count
- Project list table with M1/M2/M3 status badges
- Pending badge → one-click Mark as Invoiced (POST)
- Invoiced badge → Bootstrap modal → Mark as Received (captures amount_received + variance_reason)
- Received badges are static (no action)
- Variance flag (amber icon) when amount_received != amount

### Milestone actions
- POST /projects/<project_id>/milestone/<milestone_pk>/invoice/ → @role_required(['Finance'])
- POST /projects/<project_id>/milestone/<milestone_pk>/receive/ → @role_required(['Finance'])
- POST /projects/<project_id>/milestones/create/ → @role_required(['PM']) — creates 3 default milestones for pre-Day-6 projects

### BD dashboard — /dashboard/bd/
- @role_required(['BD'])
- Entire dashboard is READ-ONLY — no POST actions
- Summary cards: Total Active Projects, Commissioned This Month, Total Contracted Value (₹), Pending Payments
- Project list table: Project ID, Customer, Capacity (kWp), Current Phase, M1/M2/M3 badges (read-only), Target commissioning date, Assigned PM
- Each row links to /projects/<project_id>/overview/
- Document upload: placeholder card only ("coming soon — Phase 2")
- commissioned_at field used for "Commissioned This Month" filter — null-safe

### Individual project overview — /projects/<project_id>/overview/
- @login_required + PM isolation in view (PM sees own projects only, Http404 otherwise)
- All other roles see all projects
- Panel 1 — Project header (all roles): status, type, capacity, customer, address, assigned PM/SE/Design, contract value, dates
- Panel 2 — Task progress (PM, SE, Admin only): phase-wise progress bars, internal completion %, external pending count
- Panel 3 — BOQ status (PM, SCM, Design, Admin): badge, last revision event, link to BOQ
  CRITICAL: always try/except on project.boq — never direct access in template
- Panel 4 — Finance milestones (PM, Finance, CEO, Admin): full milestone table with variance column
  Finance role sees action buttons; all other roles read-only
- Panel 5 — Recent activity (all roles): last 5 events combining DueDateChangeLog + BOQRevision, sorted by date descending

Entry points:
- PM dashboard project list → "Overview" link per row
- BD dashboard → entire row clickable
- Project detail page → "Project Overview" button in header

### Zoho CRM webhook — /webhooks/zoho/deal-closed/
- @csrf_exempt (no @login_required — machine-to-machine, token auth only)
- Secret token passed as query parameter: ?secret=ZOHO_WEBHOOK_SECRET
- Fires when Zoho deal Stage = Closed Won
- Creates project in Draft status, project_type = Residential, hardcoded

**Field mapping — Zoho → Django:**
| Zoho Field | Django Field |
|---|---|
| Account Name (else Contact Name) | customer_name |
| Contact Name (when Account Name present) | customer_contact_person |
| Capacity(kW) | capacity_kw |
| Amount | contract_value |
| City | city |
| State | state |
| Mobile | customer_phone |
| Customer Email | customer_email |
| Assign PM (new Zoho field, email) | assigned_pm (UserProfile iexact lookup) |
| Closing Date | target_commissioning_date |
| Record Id | zoho_deal_id |

**Webhook logic:**
1. Token validation → 403 on failure
2. Stage check → 200 + return if not Closed Won
3. Duplicate guard → filter(zoho_deal_id=record_id).exists() → 200 + return if exists
4. Defensive payload parsing with .get() at every level
5. safe_decimal() applied to contract_value and capacity_kw
6. PM lookup via UserProfile.objects.filter(user__email__iexact=pm_email).first()
7. If PM not found → assigned_pm=None + Admin Notification created
8. Project.objects.create() in try/except → always return 200 on valid token
9. Notification.objects.create() in separate try/except — non-critical

**Settings:**
```python
ZOHO_WEBHOOK_SECRET = env('ZOHO_WEBHOOK_SECRET', default='')
```
Must be added to Railway environment variables manually.

---

## Activation change (Day 6)
After attach_residential_template(), create 3 PaymentMilestone records:
```python
from projects.models import PaymentMilestone  # import inside function, not module level
milestone_defaults = [
    ('M1', 'On Survey Completion'),
    ('M2', 'On Material Supply'),
    ('M3', 'On Commissioning'),
]
for name, desc in milestone_defaults:
    PaymentMilestone.objects.create(
        project=project,
        milestone_name=name,
        milestone_description=desc,
        created_by=request.user.userprofile
    )
```
Pre-Day-6 projects (no milestones): show empty state card with "Create milestones" button on project detail page.

---

## PM milestone edit (project detail page)
PM-only section below task list on project detail page.
Fields per row: milestone_description, amount (₹), due_date.
POST with action='update_milestone' and milestone_pk hidden field.
PM CANNOT touch: status, invoice_date, received_date, amount_received.

---

## Known bugs fixed (Day 6)
1. Migration 0014 missing from git commit — fields existed in models.py but migration file
   was never generated or committed. Fixed by running makemigrations explicitly and committing
   the migration file separately. Always verify migration files appear in git status before pushing.
2. Webhook null assigned_pm condition — unassigned project creation path had a bug when
   PM email was absent or unmatched. Fixed in Claude Code session. Ensure Admin Notification
   is always created in a separate try/except when assigned_pm is None.

---

## Known gotchas — carry forward
- DecimalField sum with nulls: always use aggregate(s=Sum('amount'))['s'] or 0
- project.boq access: always try/except in view, never direct in template
- Milestone update views must be POST with CSRF — never GET links that write to DB
- Circular import: import PaymentMilestone inside activation function body, not module level
- Mark as Received modal: form action must POST to /projects/<id>/milestone/<pk>/receive/
  not to current page — common mistake when multiple modals on one page
- Variance calculation: annotate in view Python, not in template arithmetic
- commissioned_at is null on all pre-Day-6 projects — BD dashboard "Commissioned This Month"
  must filter commissioned_at__isnull=False before month/year filter
- Zoho payload nesting: parse with .get() at every level, never direct key access
  data = payload.get('data', [{}])[0]; deal = data.get('Deal', data)
- Zoho numeric fields may arrive as Indian-formatted strings ("4,50,000") — safe_decimal()
  helper strips non-numeric characters before Decimal conversion
- Always return HTTP 200 on valid token webhook calls — even on application errors.
  Non-200 causes Zoho to retry → duplicate projects

---

## Zoho setup — manual steps completed / pending
- [x] ZOHO_WEBHOOK_SECRET added to Railway environment variables
- [x] Webhook endpoint configured in Zoho CRM
- [x] Workflow rule: Stage = Closed Won → fire webhook
- [ ] Assign PM field added to Zoho Deal layout (email type, mandatory on Closed Won)
- [ ] Test payload fired and payload structure confirmed in Railway Deploy Logs
- [ ] logger.info(request.body) line removed from views.py after payload confirmed

---

## Next up — Day 7
- File upload for every role
- 

---

## Sprint status
- Days completed: 6 (two sessions on final day)
- Current migration: 0014
- Hard deadline: 4 July 2026 (board demo)
- Demo centerpiece: commissioning update → email + WhatsApp → CEO dashboard refresh
- Week 3: SendGrid email + Interakt WhatsApp notifications
- Week 3: CEO action items spec (discuss once Finance + Issues built)
- Week 4: Live pilot on 2 real residential projects

# PROJECT_CONTEXT.md — Day 7 Updates
# Date: 13 June 2026
# Paste this into your existing PROJECT_CONTEXT.md, replacing the relevant sections

---

## Current migration state
**Latest migration: 0015**
- 0013 — PaymentMilestone model + commissioned_at on Project
- 0014 — customer_contact_person + zoho_deal_id on Project
- 0015 — ProjectDocument + TaskAttachment models

---

## Completed build days
- Day 1: Auth + role-based redirect (7 roles)
- Day 2: Project model, auto-IDs, Draft→Activate, 51-task residential template
- Day 3: Role dashboards (PM, SE, Design, SCM), duration fields, due date cascade (Option B)
- Day 4: Vendor Master, BOQ submission workflow, BOQRevision history
- Day 5/6 Session 1: Finance dashboard, BD dashboard (basic), Individual project overview
- Day 5/6 Session 2: Zoho CRM webhook integration
- Day 7: File uploads and document management (project-level + task-level)

---

## Models — additions from Day 7

### ProjectDocument (new — migration 0015)
```python
class ProjectDocument(models.Model):
    FILE_TYPE_CHOICES = [('Document', 'Document'), ('Photo', 'Photo')]

    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name='documents'
    )
    uploaded_by = models.ForeignKey(
        UserProfile, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='project_documents'
    )
    file_name = models.CharField(max_length=255)
    file_url = models.URLField(max_length=1000)
    supabase_path = models.CharField(max_length=500)
    file_type = models.CharField(max_length=20, choices=FILE_TYPE_CHOICES)
    file_size_kb = models.PositiveIntegerField(default=0)
    uploaded_at = models.DateTimeField(auto_now_add=True)
    is_deleted = models.BooleanField(default=False)
    deleted_at = models.DateTimeField(null=True, blank=True)
    deleted_by = models.ForeignKey(
        UserProfile, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='deleted_project_documents'
    )

    class Meta:
        ordering = ['-uploaded_at']
```

### TaskAttachment (new — migration 0015)
```python
class TaskAttachment(models.Model):
    FILE_TYPE_CHOICES = [('Document', 'Document'), ('Photo', 'Photo')]

    task = models.ForeignKey(
        Task, on_delete=models.CASCADE, related_name='attachments'
    )
    uploaded_by = models.ForeignKey(
        UserProfile, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='task_attachments'
    )
    file_name = models.CharField(max_length=255)
    file_url = models.URLField(max_length=1000)
    supabase_path = models.CharField(max_length=500)
    file_type = models.CharField(max_length=20, choices=FILE_TYPE_CHOICES)
    file_size_kb = models.PositiveIntegerField(default=0)
    uploaded_at = models.DateTimeField(auto_now_add=True)
    is_deleted = models.BooleanField(default=False)
    deleted_at = models.DateTimeField(null=True, blank=True)
    deleted_by = models.ForeignKey(
        UserProfile, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='deleted_task_attachments'
    )

    class Meta:
        ordering = ['-uploaded_at']
```

---

## File upload configuration

### Allowed file types
- Documents: pdf, doc, docx, xls, xlsx
- Photos: jpg, jpeg, png
- Max size: 20MB per file
- Multiple files per upload: yes

### Supabase path convention
```
project-documents/{project_id}/{uuid}_{original_filename}
task-attachments/{project_id}/{task_id}/{uuid}_{original_filename}
```
UUID prefix always used — prevents filename collisions.

### Supabase bucket
- Bucket name: solarpms-files
- Bucket type: PUBLIC (permanent URLs, no signed URLs)
- Public URL format:
  {SUPABASE_URL}/storage/v1/object/public/{SUPABASE_BUCKET}/{supabase_path}
- supabase pinned to version 2.31.0 in requirements.txt

### Settings added
```python
FILE_RETENTION_DAYS = env.int('FILE_RETENTION_DAYS', default=90)
DATA_UPLOAD_MAX_MEMORY_SIZE = 104857600   # 100MB — handles multi 20MB uploads
FILE_UPLOAD_MAX_MEMORY_SIZE = 104857600
SUPABASE_URL = env('SUPABASE_URL', default='')
SUPABASE_KEY = env('SUPABASE_KEY', default='')
SUPABASE_BUCKET = env('SUPABASE_BUCKET', default='solarpms-files')
```

---

## New views and URLs

```python
# Project-level
path('projects/<str:project_id>/documents/upload/',
     views.upload_project_document, name='upload_project_document'),
path('projects/<str:project_id>/documents/<int:doc_pk>/delete/',
     views.delete_project_document, name='delete_project_document'),

# Task-level
path('projects/<str:project_id>/tasks/<int:task_id>/attachments/upload/',
     views.upload_task_attachment, name='upload_task_attachment'),
path('projects/<str:project_id>/tasks/<int:task_id>/attachments/<int:attach_pk>/delete/',
     views.delete_task_attachment, name='delete_task_attachment'),
```

### View behaviours
- upload_project_document: multi-file, server-side extension + MIME + size
  validation, Supabase upload, ProjectDocument record per file,
  partial failure returns warning not error
- delete_project_document: ownership check (uploader or Admin), soft delete
- upload_task_attachment: cross-project guard
  (task.phase.project.project_id == project_id), same upload flow
- delete_task_attachment: ownership check, soft delete
- All error paths use direct redirect('view_name', kwarg=...) —
  no _safe_redirect helper (removed — caused NoReverseMatch)

---

## UI additions

### Project detail page + project overview page
- "Documents" section added below existing content
- Card: header + "Upload Files" button + file list table
- Columns: File Name (download link), Type badge, Size, Uploaded By,
  Uploaded At, Delete icon (uploader/Admin only)
- Photo files: thumbnail preview (img tag, max-height 80px)
- Empty state: "No files uploaded yet"

### Task detail page
- "Attachments" section added
- Same card pattern with attachment count badge
- Photo files show thumbnail; documents show file icon + download link

### Task close warning (warn but allow)
- When user marks task as Done with zero active attachments:
  Bootstrap modal fires before POST
- Modal options: "Upload File" (opens file input) / "Close Without Attachment"
  (proceeds with POST) / "Cancel" (dismisses)
- If attachments exist: no modal, task closes normally
- Implementation: JS data attribute `data-attachment-count` on task row,
  checked before POST fires
- No server-side blocking — Phase 1 is warn but allow
- Mandatory proof enforcement deferred to Phase 2
  (will be a flag on Task model, not a new model)

---

## Soft delete + purge

- Soft delete: is_deleted=True, deleted_at=now(), deleted_by=profile
- Files hidden from all queries via .filter(is_deleted=False)
- Hard delete: management command purge_deleted_files
  Location: projects/management/commands/purge_deleted_files.py
  Logic: deletes from Supabase first, then DB record
  Failure handling: logs error per file, continues — never stops full run
- Railway cron: 0 2 * * * python manage.py purge_deleted_files (2AM daily)
- Retention period: FILE_RETENTION_DAYS env var, default 90 days

---

## Known bugs fixed (Day 7)

### Bug 1 — 500 on every file upload
Symptom: 500 error on every upload attempt
Root cause: _safe_redirect helper called with missing project_id kwarg →
NoReverseMatch; task variant passed HttpResponseRedirect object as fallback
Fix: Removed _safe_redirect helper entirely. All error paths use
direct redirect('view_name', kwarg=value) — no helper abstraction.
Carry forward rule: never use redirect helper wrappers in this codebase.
Always use direct redirect() with explicit kwargs.

### Bug 2 — ModuleNotFoundError: No module named 'supabase'
Symptom: App crashed on Railway after deploy
Root cause: bare 'supabase' in requirements.txt silently not installed
by Railway's pip resolver
Fix: Pinned to supabase==2.31.0 in requirements.txt.
Broadened except clause to catch both ValueError and ImportError
in get_supabase_client().
Carry forward rule: always pin third-party packages to exact versions
in requirements.txt. Never leave packages unpinned — Railway pip
behaviour differs from local venv.

### Bug 3 — Task-close modal did nothing on "Done"
Symptom: Clicking "Done" on task closed it without showing modal
Root cause: new bootstrap.Modal() in inline script ran before Bootstrap
JS loaded → IIFE crashed → window.handleTaskStatusChange never defined
Fix: Moved Bootstrap modal init into DOMContentLoaded event listener.
Defined handleTaskStatusChange immediately after DOMContentLoaded.
Carry forward rule: never initialise Bootstrap components in inline
scripts outside DOMContentLoaded. Always wrap Bootstrap JS in
DOMContentLoaded or place at bottom of body after Bootstrap CDN script.

---

## Known gotchas — carry forward (updated)

- DecimalField sum with nulls: always use aggregate(s=Sum('amount'))['s'] or 0
- project.boq access: always try/except in view, never direct in template
- Milestone update views must be POST with CSRF — never GET links that write to DB
- Circular import: import PaymentMilestone inside activation function body
- Mark as Received modal: form action must POST to correct URL —
  common mistake when multiple modals on one page
- Variance calculation: annotate in view Python, not template arithmetic
- commissioned_at is null on all pre-Day-6 projects — always filter
  commissioned_at__isnull=False before month/year filter in BD dashboard
- Zoho payload: parse with .get() at every level, never direct key access
- Zoho numeric fields: safe_decimal() strips Indian comma formatting
- Always return HTTP 200 on valid token webhook — non-200 causes Zoho retry
- Supabase client: never initialise at module level — lazy init only
- Always pin packages to exact versions in requirements.txt
- Never use redirect helper wrappers — always direct redirect() with kwargs
- Bootstrap JS components: always init inside DOMContentLoaded
- File extension extraction: always rsplit('.', 1)[-1].lower() —
  never split('.')[1], filenames can have multiple dots

---

## Sprint status
- Days completed: 7
- Current migration: 0015
- Hard deadline: 4 July 2026 (board demo)
- Demo centerpiece: commissioning update → email + WhatsApp → CEO dashboard refresh

## Next up — Day 8
- Issues module
- Comments module

# PROJECT_CONTEXT.md — Day 8 Updates
# Date: 14 June 2026
# Paste this into your existing PROJECT_CONTEXT.md, replacing the relevant sections

---

## Current migration state
**Latest migration: 0017**
- 0013 — PaymentMilestone model + commissioned_at on Project
- 0014 — customer_contact_person + zoho_deal_id on Project
- 0015 — ProjectDocument + TaskAttachment models
- 0016 — Issue model + ActivityLog stub model
- 0017 — Comment model

---

## Completed build days
- Day 1: Auth + role-based redirect (7 roles)
- Day 2: Project model, auto-IDs, Draft→Activate, 51-task residential template
- Day 3: Role dashboards (PM, SE, Design, SCM), duration fields, due date cascade (Option B)
- Day 4: Vendor Master, BOQ submission workflow, BOQRevision history
- Day 5/6 Session 1: Finance dashboard, BD dashboard (basic), Individual project overview
- Day 5/6 Session 2: Zoho CRM webhook integration
- Day 7: File uploads and document management (project-level + task-level)
- Day 8 Session 1: Issues module — full lifecycle, accountability, severity, ActivityLog stub
- Day 8 Session 2: Comments module, ActivityLog wired, Project Timeline, Portal Activity Log
- Day 8 Session 3: Retrospective code commenting pass — all existing files documented

---

## Data Models

### Issue (migration 0016)
```python
class Issue(models.Model):
    SEVERITY_CHOICES = [('Low','Low'), ('Medium','Medium'),
                        ('High','High'), ('Critical','Critical')]
    STATUS_CHOICES = [('Open','Open'), ('In Progress','In Progress'),
                      ('Resolved','Resolved'), ('Closed','Closed')]

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='issues')
    task = models.ForeignKey(Task, on_delete=models.SET_NULL,
                             null=True, blank=True, related_name='issues')
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True, default='')
    severity = models.CharField(max_length=20, choices=SEVERITY_CHOICES, default='Medium')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='Open')
    raised_by = models.ForeignKey(UserProfile, on_delete=models.SET_NULL,
                                  null=True, blank=True, related_name='raised_issues')
    assigned_to = models.ForeignKey(UserProfile, on_delete=models.SET_NULL,
                                    null=True, blank=True, related_name='assigned_issues')
    raised_at = models.DateTimeField(auto_now_add=True)
    due_date = models.DateField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    resolution_note = models.TextField(blank=True, default='')

    class Meta:
        ordering = ['-raised_at']
```

task FK is nullable — issues can be task-level (task set) or project-level (task=None).
Raise Project Issue button on project detail/overview creates project-level issues.

### ActivityLog (migration 0016 — stub, fully wired in Day 8 Session 2)
```python
class ActivityLog(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE,
                                null=True, blank=True, related_name='activity_logs')
    actor = models.ForeignKey(UserProfile, on_delete=models.SET_NULL,
                              null=True, blank=True, related_name='activity_logs')
    action = models.CharField(max_length=255)
    entity_type = models.CharField(max_length=50, blank=True, default='')
    entity_id = models.PositiveIntegerField(null=True, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-timestamp']
```

entity_type values in use: 'Issue', 'Comment', 'Task', 'BOQ', 'Milestone', 'File'

### Comment (migration 0017)
```python
class Comment(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='comments')
    task = models.ForeignKey(Task, on_delete=models.CASCADE,
                             null=True, blank=True, related_name='comments')
    issue = models.ForeignKey(Issue, on_delete=models.CASCADE,
                              null=True, blank=True, related_name='comments')
    parent = models.ForeignKey('self', on_delete=models.CASCADE,
                               null=True, blank=True, related_name='replies')
    author = models.ForeignKey(UserProfile, on_delete=models.SET_NULL,
                               null=True, blank=True, related_name='comments')
    body = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    is_deleted = models.BooleanField(default=False)
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['created_at']
```

Comment constraints (enforced in views, not model):
- Exactly one of task or issue must be set — never both, never neither
- parent must belong to same task/issue as the reply
- parent must have parent=None — replies to replies are not allowed (400)
- Soft-deleted comments show "This comment was deleted" placeholder — never removed from queryset

---

## Features built — Day 8

### Issues module (Session 1)
- Any role can raise a task-level issue (from task detail) or project-level issue
  (from project detail / project overview via dedicated "Raise Project Issue" button)
- Severity: Low / Medium / High / Critical
  Badge colours: secondary / warning / danger / danger fw-bold with ⚠ CRITICAL prefix
- Lifecycle: Open → In Progress → Resolved → Closed
  - Open → In Progress: anyone (assigned_to must exist first)
  - In Progress → Resolved: assigned_to only, or PM if unassigned
    Modal required: resolution_note (mandatory)
  - Resolved → Closed: PM of project only (one-click)
  - Resolved → Open: PM of project only (reopen, clears resolved_at + resolution_note)
- Accountability: any role can assign/reassign at any time (not Closed)
- Closed issues: fully read-only, all action buttons hidden
- Status transitions use filter().update() not .save() — race condition guard
- log_activity() called for all issue events

### Comments module (Session 2)
- Comments on tasks and issues — same Comment model, different FK set
- Two-level threading: comment → reply (no replies to replies)
- Reply textarea appears inline on "Reply" click (JS toggle, no page reload)
- Soft delete: is_deleted=True, shows "This comment was deleted" placeholder
  Replies remain visible under deleted parent
- Delete icon visible to author and Admin only
- log_activity() called for all comment events

### ActivityLog — wired events (Session 2)
Wired to all new Day 8 events plus backfilled into existing views:
- Issue raised, assigned, In Progress, Resolved, Closed, Reopened
- Comment posted (task + issue), reply posted, comment deleted
- Task status changed, due date changed
- BOQ submitted, approved, rejected
- Milestone invoiced, received
- File uploaded (project + task), file deleted
- Project activated

### Project Timeline (/projects/<id>/timeline/)
- All roles, PM isolation applies
- ActivityLog filtered by project, newest first, paginated 20/page
- Entry: timestamp | role badge | actor name | action | entity badge
- Entity badge colours: Issue=danger, Comment=info, Task=primary,
  BOQ=warning, Milestone=success, File=secondary
- Link in project detail header alongside "Project Overview" button

### Portal Activity Log (/portal-admin/activity-log/)
- Admin only (@role_required(['Admin']))
- All ActivityLog records, all projects, newest first, paginated 50/page
- Filters: Project, Actor, Entity Type, Date From, Date To (all optional, chainable)
- Link from Admin dashboard

---

## log_activity() helper — usage rules

Location: projects/views.py (or projects/utils.py — check your codebase)

```python
def log_activity(project, actor, action, entity_type='', entity_id=None):
    try:
        from projects.models import ActivityLog
        ActivityLog.objects.create(
            project=project,
            actor=actor,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
        )
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"ActivityLog failed: {e}")
```

Rules:
- ALWAYS use log_activity() helper — never call ActivityLog.objects.create() directly in views
- ALWAYS call log_activity() AFTER the primary save — never before
- A failed log write must NEVER block the primary action (already handled by try/except)
- ActivityLog import is inside the function — do NOT add top-level ActivityLog import
  in views.py, it may cause circular import

---

## New URLs (Day 8)

```python
# Issues
path('projects/<str:project_id>/issues/create/', views.create_project_issue),
path('projects/<str:project_id>/tasks/<int:task_id>/issues/create/', views.create_task_issue),
path('issues/<int:issue_id>/', views.issue_detail),
path('issues/<int:issue_id>/update-status/', views.update_issue_status),
path('issues/<int:issue_id>/resolve/', views.resolve_issue),
path('issues/<int:issue_id>/close/', views.close_issue),
path('issues/<int:issue_id>/reopen/', views.reopen_issue),
path('issues/<int:issue_id>/assign/', views.assign_issue),

# Comments
path('projects/<str:project_id>/tasks/<int:task_id>/comments/create/', views.create_task_comment),
path('issues/<int:issue_id>/comments/create/', views.create_issue_comment),
path('comments/<int:comment_id>/delete/', views.delete_comment),

# Activity
path('projects/<str:project_id>/timeline/', views.project_timeline),
path('portal-admin/activity-log/', views.portal_activity_log),
```

---

## Code commenting standard (active from Day 8 onwards)

Every Claude Code session from Day 8 onwards applies this standard to all new code:

- Every view function: one-line docstring — what it does + who can access it
- Every permission check: comment explaining the rule enforced
- Every complex queryset: comment explaining what it fetches and why
- Every non-obvious logic pattern: comment explaining the decision
  (filter().update() race guard, lazy imports, always-200 webhook, etc.)
- Every URL entry: brief comment naming view and access level
- TODO flags for known issues — flag but do not fix in same session

Retrospective commenting pass completed on Day 8 Session 3 —
all files from Days 1–7 are now documented to this standard.

---

## Known gotchas — carry forward (updated Day 8)

- DecimalField sum with nulls: always aggregate(s=Sum('amount'))['s'] or 0
- project.boq access: always try/except in view, never direct in template
- Milestone update views: POST with CSRF only — never GET links that write to DB
- Circular import: import PaymentMilestone inside activation function body
- Mark as Received modal: form action must POST to correct URL —
  common mistake when multiple modals on one page
- Variance calculation: annotate in Python view, not template arithmetic
- commissioned_at null on pre-Day-6 projects: filter commissioned_at__isnull=False
  before month/year filter in BD dashboard
- Zoho payload: parse with .get() at every level, never direct key access
- Zoho numeric fields: safe_decimal() strips Indian comma formatting
- Always return HTTP 200 on valid token webhook — non-200 causes Zoho retry
- Supabase client: never initialise at module level — lazy init only
- Always pin packages to exact versions in requirements.txt
- Never use redirect helper wrappers — always direct redirect() with explicit kwargs
- Bootstrap JS components: always init inside DOMContentLoaded
- File extension extraction: always rsplit('.', 1)[-1].lower()
- Never use _safe_redirect helper — causes NoReverseMatch on missing kwargs

### New gotchas from Day 8

**Admin URL prefix conflict:**
Never use /admin/ as a URL prefix for custom views.
Django reserves /admin/ for its built-in admin interface.
Custom admin-like views (Portal Activity Log, Admin dashboard) must use
/portal-admin/ or similar prefix.
Full documentation: see feedback_django_admin_url_prefix.md in project root.

**log_activity() usage rule:**
Never call ActivityLog.objects.create() directly in views.
Always use the log_activity() helper — it handles try/except internally.
A failed ActivityLog write must never block the primary action.
Call log_activity() after the primary save, never before.

**Comment depth enforcement:**
Replies to replies are not supported (one level of threading only).
Always validate in view before saving:
if parent_comment.parent is not None → return HttpResponse(status=400)
Also verify parent belongs to same task/issue before accepting reply.
Never rely on frontend to enforce this — always server-side.

**task_set_due_date missing redirect:**
When adding log_activity() calls to existing views during backfill,
verify every code path has a redirect at the end.
The task due date update view was missing a redirect on one branch —
caught during Day 8 Session 2 backfill. Always check all branches
(success + error + validation failure) have explicit redirects.

---

## Supplementary file created (Day 8)
feedback_django_admin_url_prefix.md — in project root
Documents: /admin/ URL prefix conflict with Django built-in admin,
why custom views fail silently or raise NoReverseMatch,
and the rule to always use /portal-admin/ or /portal/ prefix
for custom admin-like views in this codebase.

---

## Sprint status
- Days completed: 8 (three sessions on Day 8)
- Current migration: 0017
- Hard deadline: 4 July 2026 (board demo)
- Demo centerpiece: commissioning update → email + WhatsApp → CEO dashboard refresh

## Next up — Day 9
- SCM Delivery Tracker
- PM dashboard update: delivery status appended to project list
  (backlog item decided 2026-06-11)
  
  ## Session Update — 2026-06-17

### UX-First Strategic Pivot

Paused new backend feature development after recognizing pieces weren't
connecting smoothly, especially on mobile. New approach: validate UX in
standalone prototypes first, then retrofit into Django.

**Tech stack change for all new frontend work:**
- FROM: Bootstrap 5
- TO: Tailwind CSS + Alpine.js (local UI state only — no fetch/backend calls
  from Alpine) + Lucide Icons — all via CDN, no build pipeline

**Brand colors** (extracted directly from the Horizon Renewable Power logo,
not approximated):
- Primary green: `#1a7a4a`
- Secondary green: `#7cb847`
- Primary gold: `#f0a829`
- Secondary yellow: `#efc938`
- Accent navy: `#243a6b`

**8th role confirmed: Sales & BD**
- Already built, real role — not equal to PM in project creation
- Scope: ORC (Order Confirmation Receipt — a business document, NOT Optical
  Character Recognition) upload, payment status updates, document management
  on already-activated projects
- Does NOT create projects
- Project lifecycle: Zoho CRM creates project as Draft → PM activates →
  Sales/BD uploads ORC and updates payment status

---

### Login Page — COMPLETE, tested locally, working end to end

- Used Django's existing function-based view as-is
- Hardcoded `name="username"` / `name="password"` fields (matches existing
  view, no changes to views.py)
- `{% for message in messages %}` for error display (not `form.errors`)
- Decision made: adapt template to existing view (Option A) rather than
  adding an AuthenticationForm to view context (Option B) — zero backend
  changes required
- Tailwind + Lucide, brand colors, light background with subtle green/gold
  gradient accent

---

### PM Dashboard Redesign — IN PROGRESS (template built, integration pending)

**Problem identified:** existing `dashboard_pm` view (live on Railway
production at `/dashboard/pm/`) is information-dense with no visual
hierarchy — 5 equal-weight stat cards (Active Projects, Due Today, Blocked,
Pending Approvals: 54, Awaiting Authorities: 16) plus a dense project table.
PM can't tell what's actually urgent at a glance.

**New design — one card per project:**
- Sorted most-urgent-first: highest (blocked + overdue) count first, then
  delayed projects, then on-time projects last
- Collapsed card shows ONLY: red circle with combined blocked+overdue count
  (green checkmark if zero), project name, Delayed/On-time status —
  deliberately minimal, no other badges
- Expanded card shows: phase, site engineer, progress, BOQ status, materials
  summary, a "needs attention" list of THAT project's blocked/overdue tasks,
  two quick actions (Review BOQ, Raise issue), link to `project_overview`
  for full detail and commenting
- Reassign task / Update due date are explicitly NOT in the card — they stay
  on the detail page only (due date triggers cascade recalculation, deserves
  fuller context; reassign is per-task, not per-project)
- No project-level comment quick-action — backend only supports task-level
  and issue-level comments, so this was deliberately dropped rather than
  building a feature with no real data target

**Backend changes required in `dashboard_pm` view (NO migrations):**
- Per-project `blocked_count` (currently only computed globally)
- Per-project `blocked_tasks_for_project` and `overdue_tasks_for_project`
  querysets (small lists for the "needs attention" section)
- Sort `projects_with_progress` by `(blocked_count + overdue_count)` descending
- `is_delayed` — must be a computed model `@property` or computed in the
  view; explicitly NOT a new stored field

---

### Project Detail + Overview Page Merge — IN PROGRESS (template built, integration pending)

**Decision:** combine `project_detail` (task management) and
`project_overview` (status summary) into ONE page/URL — not tabs, not a
full unstructured merge. Both pages are genuinely used; the problem was
navigation friction between them, not redundancy.

**New page structure (top to bottom):**
1. Main project info card (unchanged)
2. Full-width "Task progress by phase" block — always visible, no expand
3. Grid of compact, click-to-expand status blocks:
   - BOQ status (quarter width)
   - Payment milestones (quarter width)
   - Material status (half width)
   - Delivery challans (half width)
   - Recent activity (half width)
4. Project documents (full width, no expand — already minimal)
5. Unchanged phase-wise task list (existing `project_detail` content)

**Interaction model:** each block expands independently on click (NOT
hover-flip — rejected because no hover state exists on mobile/touch
devices). No "expand all" option — kept simple per product owner decision.
Lucide icons on every block header are part of the approved design, not
decoration — must be preserved during integration.

**Open data-sourcing questions flagged for Claude Code to investigate:**
1. Does BOQ revision history exist beyond the latest single revision note
   (possibly JSONField snapshots per earlier BOQ workflow notes)?
2. Can `DCLineItem` model data support per-line-item detail in the Material
   Status / Delivery Challans expanded views?
- Recent Activity is CONFIRMED backed by an existing ActivityLog-style model
  — no investigation needed there.

**Open decision before integration:** which URL survives —
`project_detail` or `project_overview`? Depends on what else in the app
currently links to each; must be checked before merging.

---

### Documented Gotcha — Django auto-escaping breaks JSON in inline `<script>` blocks

**Context:** cost 6 hours during login/PM dashboard integration. Claude Code
could not detect this itself; found via Claude Chat + manual browser
inspector.

**Root cause:** a Python list of dicts (e.g. phase progress data with
pk/pct/internal_done fields) was serialized server-side via `json.dumps()`
in the view, then inserted into a `<script>` block in the template via a
template variable. Django's auto-escaping was still active for that
variable, converting every `"` into `&quot;`. `&quot;` is not valid
JavaScript, so the JS parser died at the first `&` — silently breaking the
entire `<script>` block, including unrelated functionality in the same
block (e.g. a file upload listener), which made the symptom look unrelated
to its actual cause.

**Fix pattern — apply this any time server-rendered JSON is injected into
an inline `<script>` tag:** mark the `json.dumps()` output as safe from
auto-escaping, using either Django's `|safe` template filter or
`mark_safe()` / `json_script()` in the view — applied narrowly to that
specific JSON-producing variable. Do NOT disable autoescape broadly on the
template.

**Debugging shortcut for next time:** if JS "works in isolation but breaks
on this page," check FIRST whether any inline `<script>` block contains a
Django template variable carrying JSON. This single check would have found
this bug in minutes instead of 6 hours.

DATE: 19-June-2026
DAY: ~12 of 24 (verify against your local day-counter — calendar gaps may not map 1:1 to numbered build days)

COMPLETED TODAY:
- SE dashboard designed collaboratively in Claude Chat — one-card-per-project,
  same pattern as PM/SCM
- se_dashboard_prototype.html built (Tailwind + Alpine + inline SVG, no build pipeline)
- Full six-layer integration spec produced for SE dashboard
- Complete Claude Code prompt written, including stop-and-report triggers,
  no-migration constraint, and the full code commenting standard
- Git push procedure discussed for PM dashboard, SCM dashboard, and
  project_detail/project_overview merge — NOT yet confirmed executed

CURRENT STATE:
- Four redesigns from first UX-pivot wave verified working as of 18-June session:
  login page, PM dashboard, SCM dashboard, project_detail+overview merge
- ⚠ PUSH STATUS UNCONFIRMED: these four redesigns may still be uncommitted/unpushed
  to GitHub main as of this session. CONFIRM PUSH BEFORE NEXT CLAUDE CODE SESSION —
  if unpushed, Railway production does not yet reflect these changes, and the
  board demo environment is stale.
- Migration sequence at 0019+ (Issue.delivery_challan FK, DCLineItem.damaged_quantity).
  Verify migration 0019 is included in whatever push happens — a push without it
  will break SCM/PM dashboards in production (code expects the field, DB won't have it).
- SE dashboard: prototype + spec + Claude Code prompt all ready. NOT yet built in Django.

SE DASHBOARD — LOCKED DECISIONS (19-June session):
- One card per project, urgency circle = overdue_count + blocked_count +
  pending_grn_count + issue_count (same combined-count pattern as PM)
- Card-level action buttons: Raise Issue + View Project ONLY (two buttons, same as PM)
- Task status updates and GRN confirmation do NOT happen on the dashboard card —
  both happen on project_overview, which SE shares with PM (no separate SE detail page)
- "View Project" routes to existing project_overview page, same URL PM uses
- Confirmed: GRN confirm button does not currently exist for SE role on
  project_overview (shows to no one as of last check) — separate session required
- Confirmed: SE updates assigned tasks via project_overview's phase-wise task list
  (not yet verified whether buttons are role-gated correctly for SE — pending check)

KNOWN ISSUES / INCOMPLETE (carried forward + new):
- GRN confirm button for SE role on project_overview — does not exist yet,
  needs its own spec session before SE can actually confirm receipt from the dashboard flow
- Task status update buttons on project_overview — role-gate for SE not yet verified
- Push status of last three dashboard redesigns to git/Railway — unconfirmed,
  resolve before starting next Claude Code build session

NEXT SESSION GOAL:
Continue UX-first prototype design in Claude Chat for the next wave of dashboards —
Finance, Design, and Sales & BD. Same process as PM/SCM/project_overview/SE:
standalone HTML prototype (Tailwind + Alpine + Lucide) → confirm direction with
Zuber → six-layer Django integration spec → Claude Code prompt. No Claude Code
session planned next — this is a Chat-only design session.

SE dashboard Claude Code integration (prototype + spec + prompt all ready from the
18/19-June sessions) remains in the queue but is NOT scheduled for the next session.
Hand it to Claude Code whenever a build session is slotted in — five stop-and-report
checks must be answered first (SE-project link field, DC status values, is_delayed
location, blocked-task field name, task_type values).

REMAINING DASHBOARDS NOT YET REDESIGNED UNDER TAILWIND/ALPINE/LUCIDE:
- Finance dashboard
- Design dashboard
- Sales & BD dashboard
- CEO dashboard
- Admin dashboard

# PROJECT_CONTEXT.md — Update

**Update date:** 22 June 2026
**Previous update:** 19 June 2026
**Covers:** Notification channels build + WhatsApp verification; CEO dashboard & admin specs; ZeptoMail decision

---

## HEADLINE SINCE 19 JUNE

The external notification system went from spec to **built and WhatsApp-verified end-to-end**. Real WhatsApp messages now arrive on a real phone with correct header/body variables, through the `send_notification()` chokepoint. This was the demo-critical path and it is now substantially de-risked. Remaining work is wiring the REAL views.py trigger call sites (test harness proved templates; production call sites still need param-count correction) and the behavioural pre-flight scenarios.

---

## NOTIFICATION SYSTEM — BUILT (Phase 1)

**Migrations 0025–0028 applied (Railway confirmed clean before build):**
- `UserProfile.email_notifications`, `UserProfile.whatsapp_notifications` (bool, default True)
- `NotificationLog` model — logs every attempt (sent/failed/skipped) with channel, status, message, template_name, error_detail, related_project, actor
- `SystemSettings` singleton (`get_or_create(pk=1)`) — `whatsapp_enabled` / `email_enabled`, both default **False** (master kill switch)
- `Task.is_payment_milestone` (bool, default False)

**`projects/notifications.py` — the chokepoint:**
- Order: master switch → user preference → send → log. Every path logged.
- WhatsApp via Interakt (`INTERAKT_API_KEY`). Phone normalised: strips any existing `+91`, sends `countryCode: "+91"` + 10-digit number separately.
- Email via ZeptoMail (`ZEPTOMAIL_API_KEY`, `ZEPTOMAIL_FROM_EMAIL`). **SendGrid retained as commented fallback (do not delete).**
- Failed send never propagates — all external calls try/except wrapped. (Highest-severity rule, honoured.)
- **Header/body split (critical fix):** Interakt requires header variable values and body variable values as SEPARATE fields. Convention: `template_params[0]` = header value, `template_params[1:]` = body values. Every approved template has exactly 1 header variable.
- **201 success fix:** Interakt returns HTTP **201** (`{"result":true,...}`) on successful queue, not 200. Success check is now 2xx, not `== 200`. (Before this fix, every successful send was mislogged as failed.)

**ZeptoMail confirmed working** (test email sent and received). Email is the should-have; WhatsApp is the must-have for the demo. ZeptoMail sending-domain verification (SPF/DKIM/domain-ownership DNS) is the email gate — confirm done.

---

## SIX APPROVED INTERAKT TEMPLATES + CONFIRMED VARIABLE STRUCTURE

All approved in Interakt. **Confirmed against console** (header value first, then body, in registered order). Header variable is always the project name. This table is hard-won — preserve it; it's the single thing that broke and fixed the whole WhatsApp path.

| Template | Header (1) | Body | Total |
|---|---|---|---|
| `issue_created` | project | recipient_name, project, raiser_name | 1+3 |
| `issue_resolved` | project | (CONFIRM final body count in console) | 1+N |
| `assign_task` | project | recipient_name, task_name, project, line_item | 1+4 |
| `assign_project` | project | (CONFIRM final body count in console) | 1+N |
| `boq_acknowledged` | project | project, scm_name | 1+2 |
| `payment_notification` | project | milestone_name, project | 1+2 |
| `invoice_paid` | project | item_name, company_name | 1+2 |

**Note:** project name legitimately appears in BOTH header and body for most templates — pass the same value twice, not a bug.

**Test harness:** `projects/management/commands/test_whatsapp.py` — sends all templates to a test phone, prints a status board (sent/failed/skipped + error_detail). Run with `python manage.py test_whatsapp` (NOT shell piping — Windows PowerShell has no `<` redirect; management command avoids the issue entirely). Current state: **all 7 sending correctly** after the header/body and 201 fixes.

---

## VERIFIED WORKING (as of 22 June)
- All 7 WhatsApp templates send and arrive correctly formed on a real phone (header + body variables in right slots).
- ZeptoMail email sends and arrives.
- The chokepoint's master-switch check, preference check, logging, and exception-swallowing all function.

## NOT YET VERIFIED — IMMEDIATE NEXT WORK
- **Real views.py trigger call sites still pass WRONG variable counts** (the same bug the test harness had pre-fix). Test harness proves templates; production call sites do NOT yet fire correctly. A real task-assignment in the live app will currently 400. **An investigation-only Claude Code prompt is prepared** to report (not edit) each call site's current vs corrected params, flag missing data (e.g. `line_item` for assign_task, resolution note for issue_resolved), and flag any template with no call site at all. Review plan → resolve missing-data items → approve edits → test by performing real actions.
- **Behavioural pre-flight scenarios not yet run by Zuber:** preference-suppression → skipped logged; master-off → skipped, in_app still fires; forced-failure → task still commits, failed logged; no-phone recipient → graceful.
- **Payment-milestone flag (`is_payment_milestone`) only set on the RESIDENTIAL template** (M1 "DEV Conduct" Ph2, M2 "Delivery of Module" Ph6, M3 "Plant Commissioning" Ph8 = demo moment). OPEX and CAPEX milestone tasks unflagged — payment notifications won't fire for those project types. Coverage gap, acceptable for demo (runs on residential), note for later.

---

## SPECS WRITTEN (build not started)

**CEO Dashboard — six-layer spec complete.** Built against verified data audit: 28 of 30 numbers are pure aggregation (no schema work), target is ONE aggregation service in 3 queries (Project/Task/Issue), department rollup as 18 conditional Counts inside the Task aggregate (NOT a separate GROUP BY). Decisions locked: At Risk = query-derived (overdue internal tasks AND target date still future); blocked-aged-7d+ needs `Task.blocked_since` migration; Issue-overdue tile accepted to read low (due_date optional). **Finance added as 6th department** (PM/SCM/Design/BD/Execution/Finance). Manual refresh control top-left (Lucide refresh icon + last-loaded timestamp) until WebSockets. Guardrails: Execution overdue filters `task_type='Internal'`; BD rollup uses `assigned_role='BD / Sales'`; three distinct "blocked" numbers labelled non-reconcilable.

**Admin panel — scope decided, spec pending.** Phase 1 (light): per-user email/WhatsApp toggles, system-wide master switch, department-wise employee list, user activate/deactivate, role reassignment (roles stay fixed choices). Medium: send-records/counts screen reading NotificationLog. **Heavy structural DEFERRED to Phase 2:** Create Department, Create Role (would require converting hardcoded role choices to dynamic lookup tables + refactoring every hardcoded role reference — touches the CEO dashboard rollup). The admin is downstream of the notification scaffolding already built — toggles/log/master-switch all read what the chokepoint writes.

---

## KEY LEARNINGS THIS CYCLE
- **Interakt header vs body params are separate API fields**, not one combined array. Param COUNT and ORDER must match the console registration exactly, header first. This single issue caused every WhatsApp 400.
- **Interakt returns HTTP 201 on success, not 200.** Check 2xx.
- **Windows PowerShell has no `<` input redirect** ("reserved for future use" error). Use a management command, or `Get-Content file | python manage.py shell`, or `cmd /c "... < file"`. Management command is cleanest.
- **Django interactive shell breaks pasted multi-line loops** on blank lines (IndentationError) and needs imports re-run per session (NameError). Don't paste scripts into `>>>`; run as a file/command.
- **"Sent" ≠ correct.** HTTP 201/sent only means Interakt accepted it; variables can still be in wrong slots. Always read the actual message on the phone.
- **Green test board ≠ working production.** Test harness and real views.py call sites are separate code passing separate values. Production call sites still need the same fix.
- **`payment_notification` reused for the commissioning→Finance moment** — it's the M3 milestone task hitting Done via the general `is_payment_milestone` rule, NOT a bespoke commissioning trigger. No task-name string-matching.

---

## ENV VARS (Railway)
- `INTERAKT_API_KEY`, `INTERAKT_BASE_URL` — WhatsApp
- `ZEPTOMAIL_API_KEY`, `ZEPTOMAIL_FROM_EMAIL` — email (primary)
- `SENDGRID_API_KEY` — only if commented fallback is reactivated

## GO-LIVE CHECKLIST (notifications)
1. Run the investigation prompt → review plan → fix real call sites → test by real actions.
2. Run the 4 behavioural pre-flight scenarios personally.
3. Confirm ZeptoMail domain verified.
4. Confirm `payment_notification` wording fits a commissioning trigger.
5. Master switch stays OFF until a clean live run; flip ON via `/admin/projects/systemsettings/`.

---

# PROJECT_CONTEXT.md — Notification Call Site Fixes
# Date: 24 June 2026
# Covers: Batch 1 + Batch 2 views.py param corrections, recipient expansions, test harness overhaul

---

## HEADLINE

All 7 real `send_notification()` call sites in `views.py` have been audited and corrected.
The test harness is now in raw HTTP mode and confirms all 7 templates return HTTP 201 from Interakt.
`invoice_paid` template param count corrected from 3 to 5 (document above was out of date).

---

## CONFIRMED TEMPLATE PARAM STRUCTURES (ground truth — verified 201 from Interakt)

| Template | Params (header | body...) | Total |
|---|---|---|---|
| `issue_created` | customer_name \| recipient_name, customer_name, raiser_name | 4 |
| `issue_resolved` | customer_name \| issue_title, customer_name, resolver_name, issue_link | 5 |
| `assign_task` | customer_name \| recipient_name, task_name, customer_name, task_url | 5 |
| `assign_project` | customer_name \| customer_name, city, pm_display_name | 4 |
| `boq_acknowledged` | customer_name \| design_user_name, boq_link | 3 |
| `payment_notification` | customer_name \| task_name, customer_name | 3 |
| `invoice_paid` | customer_name \| boq_item_description, invoice_no, amount, vendor_name | 5 |

**Note:** `invoice_paid` is 5 params (1 header + 4 body), NOT 3 as recorded in the previous entry.
The earlier entry (item_name, company_name) was a draft that was never confirmed against Interakt.
The 5-param structure was confirmed with HTTP 201 from Interakt on 24 June.

**Note:** customer_name appears in both header AND body for most templates — pass the same value twice, not a bug.

---

## BATCH 1 — PARAM FIXES (views.py call sites)

Four call sites corrected for wrong param count / wrong order:

### `_notify_boq_acknowledged()` helper
- **Recipients changed:** was Design submitter only → now PM (`project.assigned_pm`) + acknowledging SCM user, deduplicated.
- **Params fixed:** was `[project_id, customer_name, scm_name]` → now `[customer_name, design_user_name, boq_link_abs]`
- design_user_name = `boq.submitted_by.user.get_full_name() or boq.submitted_by.user.username`
- Helper signature updated to accept `request` (needed for `build_absolute_uri`)

### `payment_notification` (task_status_update view)
- **Params reordered:** was `[project_id, customer_name, task_name]` → now `[customer_name, task_name, customer_name]`

### `issue_resolved` (resolve_issue view)
- **Params fixed:** was `[resolver_name, issue_title, project_id]` (3 params) → now `[customer_name, issue_title, customer_name, resolver_name, issue_link_abs]` (5 params)

### `issue_created` (create_project_issue, create_task_issue, create_delivery_issue — 3 sites)
- **Params fixed:** was `[title, project_id, customer_name, raiser_name]` → now `[customer_name, recipient_name, customer_name, raiser_name]`
- Added `recipient_name = assigned_to.user.get_full_name() or assigned_to.user.username` before each call

---

## BATCH 2 — PARAM FIXES + RECIPIENT EXPANSIONS + NEW CALL SITE (views.py)

### `assign_task` (task_assign view)
- **Params fixed:** was `[task_name, project_id, customer_name]` (3 params) → now `[customer_name, recipient_name, task_name, customer_name, task_url_abs]` (5 params)
- Added `recipient_name = assignee.user.get_full_name() or assignee.user.username`
- Added `task_url = f'/projects/{project.project_id}/tasks/{task.pk}/'`
- Added `task_url_abs = request.build_absolute_uri(task_url)` — absolute URL for WhatsApp clickable link
- `link` param uses relative `task_url` (for in-app); template_params[4] uses absolute `task_url_abs`

### `assign_project` (zoho_deal_closed_webhook view)
- **Params fixed:** was `[project_id, customer_name, city]` (3 params) → now `[customer_name, customer_name, city, pm_display_name]` (4 params)
- Added `pm_display_name = project.assigned_pm.user.get_full_name() or project.assigned_pm.user.username`

### `issue_created` — PM in-app expansion (all 3 sites)
- PM now receives an in-app-only notification when an issue is raised on their project
- Guard: `project.assigned_pm and project.assigned_pm != profile and project.assigned_pm != assigned_to`
- PM does NOT receive WhatsApp for issue_created — the template says "assigned to you", wrong context for PM

### `issue_resolved` — recipient expansion
- Was: PM only (if PM != resolver)
- Now: PM + issue.assigned_to + issue.raised_by — deduplicated via `notified_pks` set
- Resolver (profile.pk) is pre-excluded from the set so they never notify themselves
- `resolver_name` moved outside the conditional (was only computed inside the PM block)
- All three recipients get WhatsApp + in_app with the same `issue_resolved` template

### `payment_notification` — recipient expansion
- Was: Finance role only
- Now: Finance + PM (project.assigned_pm) + BD role + CEO role — deduplicated via `seen_pks` set
- All recipients get WhatsApp + email + in_app

### `invoice_paid` — new call site in `confirm_payment_request` view
- Added `select_related('vendor', 'boq_item')` to the `get_object_or_404` queryset
- Fires after `pr.save()` on Finance payment confirmation
- Recipients: SCM (all active) + PM (project.assigned_pm) + CEO (all active) — deduplicated
- Params: `[customer_name, boq_desc, pr.invoice_number, str(pr.amount), pr.vendor.name]`
- Null guard on `pr.boq_item`: `boq_desc = pr.boq_item.description if pr.boq_item else '(item)'`

---

## ABSOLUTE URLS FOR WHATSAPP LINKS

WhatsApp only renders clickable links for fully qualified URLs (https://...).
Relative paths like `/projects/HRP-001/tasks/5/` appear as plain unclickable text.

**Rule:** any URL passed as a `template_params` value must use `request.build_absolute_uri(relative_path)`.
The `link` param in `send_notification()` should stay relative (used for in-app notifications only).

Templates currently passing absolute URLs in params:
- `assign_task` → params[4] = task_url_abs
- `issue_resolved` → params[4] = issue_link_abs
- `boq_acknowledged` → params[2] = boq_link_abs

---

## TEST HARNESS OVERHAUL (`projects/management/commands/test_whatsapp.py`)

The harness was rewritten to bypass `send_notification()` and make **direct HTTP calls to Interakt**,
printing the exact payload sent and raw HTTP status + response body for each template.

This was done to diagnose why some templates were logged as "sent" but not received on the phone.
Raw output confirmed: all 7 templates return HTTP 201 from Interakt (queued for delivery).
Delivery is async — Interakt queues and sends; "sent" ≠ immediately on phone.

`invoice_paid` params updated in test harness from 3 to 5:
```python
# Before (wrong — caused HTTP 400):
"invoice_paid": ["Miracle Hospital", "Inverter", "HRP"]

# After (correct — HTTP 201):
"invoice_paid": ["Miracle Hospital", "Inverter", "INV-001", "50000", "HRP Suppliers"]
```

**Run command:** `python manage.py test_whatsapp`

---

## MIGRATION STATE (unchanged this session)

No new migrations were added in this session. All changes were to `views.py` and the test harness only.
Current latest migration: 0029 (or wherever the session that built the notification models left off — verify with `python manage.py showmigrations`).

---

## KNOWN ISSUES / GOTCHAS (new from this session)

- **All test users share one phone number (9873340425):** In dev, every UserProfile has the same test phone. Sending multiple templates in rapid succession to the same number can cause Interakt to throttle delivery. Stagger test sends by 30–60 seconds when diagnosing delivery.
- **"sent" log ≠ delivered to phone:** Interakt returns 201 and queues the message. Delivery is async. If a message doesn't arrive within a few minutes, check the Interakt dashboard for delivery status — not the NotificationLog.
- **invoice_paid boq_item nullable:** `pr.boq_item` FK can be null. Always guard: `boq_desc = pr.boq_item.description if pr.boq_item else '(item)'` before passing to template_params.
- **`reverse()` not imported in views.py:** All URLs in views.py are built as f-strings (e.g. `f'/projects/{project.project_id}/tasks/{task.pk}/'`). Do not add `reverse()` calls — keep the f-string convention consistent with the rest of the file.
- **`_notify_boq_acknowledged` now requires `request`:** Call site updated. If this helper is ever called from a non-view context (e.g. a management command or signal), `request` won't be available — will need a fallback (settings-based base URL or skip abs URL).

---

## GO-LIVE CHECKLIST (notifications) — UPDATED

1. ~~Run investigation prompt → fix real call sites~~ **DONE — all 7 call sites corrected.**
2. Run the 4 behavioural pre-flight scenarios personally (preference-off → skipped; master-off → in_app still fires; forced-failure → task commits; no-phone recipient → graceful).
3. Confirm ZeptoMail domain verified (SPF/DKIM/domain-ownership DNS).
4. Confirm `payment_notification` wording fits a commissioning trigger (M3 = Plant Commissioning).
5. Flip master switches ON via Django admin once a clean live test passes.
6. Verify `is_payment_milestone` flag is set on the 3 residential template tasks (M1/M2/M3). OPEX/CAPEX tasks intentionally unflagged.

---

# PROJECT_CONTEXT.md — 24 June 2026, Session 1
# Covers: Systemic bug fixes (SYS-1, SYS-2, SYS-3, SYS-5)

---

## HEADLINE

Four systemic bugs fixed across shared code paths. No new features, no migrations, no notification changes. All fixes are browser-verified by Zuber.

---

## BUGS FIXED

### SYS-1 — Mark task "Done" broken (project_overview.html)

**Root cause:** Bootstrap's `hide.bs.modal` event fires **synchronously** when `.hide()` is called. The no-attachment modal had a `hide.bs.modal` listener that nullified `pendingStatusForm = null`. Both button click handlers called `bsNoAttachModal.hide()` first, then read `pendingStatusForm` — which was already null by then. Neither button worked.

**Symptoms fixed:**
- "Close Without Attachment" now submits the form → task marked Done, `completed_at` set to today
- "Upload File" now redirects correctly to the task detail page
- Completion date column now populates (was a consequence of form never submitting)

**Fix:** Capture all needed references (form, row, task ID) BEFORE calling `.hide()` in each click handler.

**File changed:** `projects/templates/projects/project_overview.html` (JS inside DOMContentLoaded)

**Carry-forward rule:** In any Bootstrap modal click handler that reads shared state AFTER calling `.hide()` — always capture the value into a local variable first. `hide.bs.modal` fires synchronously; there is no async gap to rely on.

---

### SYS-2 — Quantity fields increment in decimals (BOQ + GRN templates)

**Root cause:** All quantity number inputs had `step="0.01"`, allowing decimal increments via the browser up/down arrows.

**Fix:** Changed `step="0.01"` → `step="1"` on all quantity fields. For DC creation inputs, also changed `min="0.01"` → `min="1"` (a line item with zero quantity is invalid).

**Fields fixed:**
- `boq_qty_*` — Design BOQ quantity entry (`boq_detail.html`)
- `ord_qty_*` — SCM ordered quantity entry (`boq_detail.html`)
- `received_qty_*` — GRN received quantity, both SE and SCM forms (`delivery_challan_detail.html`, 2 occurrences)
- `line_item_qty_*` — DC creation, static first row and JS-generated rows (`delivery_challan_create.html`)

**Not touched:** `damaged_qty_*` (already had `step="1"`), all amount/price/capacity fields.

**Files changed:** `boq_detail.html`, `delivery_challan_detail.html`, `delivery_challan_create.html`

---

### SYS-3 — Deleted projects appearing in role dashboards

**Root cause:** `project_delete` sets `is_deleted=True` only — it does NOT change the project's `status`. Deleted projects remain `Active` or `In Progress` in the DB. Every dashboard queryset that filtered only on `status__in=['Active', 'In Progress']` was silently including deleted projects.

**Fix:** Added `is_deleted=False` to the `Project.objects.filter(...)` call in five dashboard views.

**Dashboards fixed:** SCM, Design, Site Engineer, Finance, Sales & BD

**File changed:** `projects/views.py`

**Note:** The PM dashboard loop filters by `assigned_pm=pm_profile`, which coincidentally excluded deleted projects during testing (the deleted test project was not assigned to the testing PM). It still technically lacks `is_deleted=False` — flag for a future cleanup pass, but not urgent.

**Carry-forward rule:** Every `Project.objects.filter(...)` on any dashboard view must include `is_deleted=False`. The soft-delete never changes status. Reference pattern: `project_list` view, which already has it.

---

### SYS-5 — Role column visible in phase-wise task list

**Root cause:** The `<th>Role</th>` header and corresponding `<td>{{ task.assigned_role }}</td>` data cell were present in the shared task list table in `project_overview.html`.

**Fix:** Removed the Role `<th>`, removed the Role `<td>`, and decremented the empty-state `colspan` from 7 to 6.

**File changed:** `projects/templates/projects/project_overview.html` (phase-wise task table)

**Note:** Single shared template — fix applies to all roles simultaneously. The `assigned_role` value is still available on the Task model; it's just not displayed in this table. It remains visible on the task detail page and in the assign modal.

---

## MIGRATION STATE (unchanged this session)

No migrations added. Latest migration remains 0029.

---

## FILES CHANGED THIS SESSION

| File | What changed |
|---|---|
| `projects/templates/projects/project_overview.html` | SYS-1: modal JS handlers; SYS-5: Role column removed |
| `projects/templates/projects/boq_detail.html` | SYS-2: step="1" on boq_qty and ord_qty inputs |
| `projects/templates/projects/delivery_challan_detail.html` | SYS-2: step="1" on received_qty inputs (2 occurrences) |
| `projects/templates/projects/delivery_challan_create.html` | SYS-2: step="1" min="1" on line_item_qty inputs (static + JS) |
| `projects/views.py` | SYS-3: is_deleted=False added to 5 dashboard querysets |

---

## KNOWN GOTCHAS — new from this session

- **Bootstrap `hide.bs.modal` is synchronous:** Fires the instant `.hide()` is called, before any subsequent line in the same handler. If a `hide.bs.modal` listener clears shared state, always capture that state into a local variable before calling `.hide()`.
- **Soft-delete does not change status:** `project_delete` only sets `is_deleted=True`. Every queryset filtering by status alone will include deleted projects. Always add `is_deleted=False` to project dashboard queries.
- **PM dashboard missing is_deleted filter:** The PM dashboard project loop (`Project.objects.filter(assigned_pm=pm_profile, ...)`) does not include `is_deleted=False`. Not currently causing a visible bug (PM's deleted projects would show if they were assigned to the same PM and then deleted), but should be fixed in a future cleanup pass.

---

## SPRINT STATUS

- Hard deadline: 4 July 2026 (board demo)
- Current migration: 0029
- Session 1 (24 June): systemic bug fixes complete
- Session 2 (24 June): SCM + Design dashboard polish complete (see below)
- Next: board demo prep — push to Railway, smoke-test all role dashboards on production

---

# PROJECT_CONTEXT.md — 24 June 2026, Session 2
# Covers: SCM + Design dashboard bug fixes and UI polish

---

## HEADLINE

Eight targeted fixes and improvements across SCM dashboard, Design dashboard, project_overview, and notifications. No migrations. All browser-verified.

---

## BUGS FIXED + FEATURES ADDED

### BUG-SCM-5 — All SCM project cards showed red left border regardless of severity

**Root cause:** Only one CSS class existed (`.scm-card-stalled`, always red). Template applied it whenever `row.is_stalled` was true, regardless of `row.stall_level`.

**Fix:** Added `.scm-card-amber` CSS class (amber border + tint). Template now checks `stall_level == 'red'` before applying red vs amber class.

**File:** `projects/templates/dashboard/scm.html`

---

### BUG-SCM-6 — BOQ badge always showed "No BOQ" even when BOQ was acknowledged

**Root cause:** `boq_map` was keyed by `b.project_id` (Django FK accessor → integer auto-PK), not the string `project.project_id` field like `HRP-RES-2026-001`. Key never matched.

**Fix:** Changed to `b.project.project_id` with `.select_related('project')`.

**File:** `projects/views.py` (`dashboard_scm` view, `boq_map` dict comprehension)

**Carry-forward rule:** `b.project_id` in Django always returns the integer FK value (auto-PK). To get a custom string field on the related model, always use `b.project.project_id` with `select_related`.

---

### BUG-SCM-1 — Raise Issue modal missing Assign To and Due Date fields

**Root cause:** `scmRaiseIssueModal` in `scm.html` only had Title, Description, Severity. The `all_profiles` context variable was already passed; both backend views already accepted the fields.

**Fix:** Added Assign To (`<select name="assigned_to">`) and Due Date (`<input type="date" name="due_date">`) to the modal. Added JS reset for both on modal open.

**File:** `projects/templates/dashboard/scm.html`

---

### BUG-DES-2 — Material Status appeared before Delivery Challan in project_overview

**Fix:** Removed Material Status from Row 3 (was beside BOQ). Inserted it into Row 4 after Delivery Challan. Row 4 is now three equal columns: DC | Material Status | Recent Activity. Role gates preserved.

**File:** `projects/templates/projects/project_overview.html`

---

### Layout — project_overview Row 2 + Row 3 restructure

**Changes:**

1. **Recent Activity moved to Row 2** beside Task Progress by Phase (fills empty space for non-PM/Finance/CEO/Admin roles). Column widths: `col-md-4` when Payment Milestones visible (3-column row), `col-md-6` when not (2-column row).

2. **BOQ + Delivery Challans + Material Status merged into one Row 3** (`mb-4`). BOQ column narrowed from `col-md-6` to `col-md-4`. All three equal `col-md-4` columns.

3. **BOQ, DC, and Material Status now open by default** — all three use `collapse show` + `aria-expanded="true"`.

**File:** `projects/templates/projects/project_overview.html`

---

### BUG-DES-1 / BUG-SCM-2 — Stat block row leaves blank space at mid-widths

**Root cause:** SCM used `col-4` (always 3-column, too narrow on mobile). Design used `col-6 col-md-4` for first two and `col-12 col-md-4` for third (third takes full width on mobile, others don't).

**Fix:** All six stat blocks (3 SCM + 3 Design) changed to `col-12 col-sm-6 col-lg-4`.
- < 576px: 1 per row (stacked)
- 576–991px: 2 per row (wraps cleanly)
- ≥ 992px: 3 per row (unchanged desktop)

This layout also accommodates 6 blocks (Thread 4 addition) without any further changes — will produce 2 rows of 3 at desktop.

**Files:** `projects/templates/dashboard/scm.html`, `projects/templates/dashboard/design.html`

---

### NEW-4a — "Overview" button on each SCM project card

**Approach:** The card header was a full-width `<button>` (collapse toggle). Adding a link inside a button is invalid HTML. Fix: added `d-flex align-items-center` to `card-header-inner`, changed button from `w-100` to `flex-grow-1 min-w-0`, added `<a class="btn btn-outline-secondary btn-sm" href="{% url 'project_overview' %}">Overview</a>` as sibling after the button.

**File:** `projects/templates/dashboard/scm.html`

---

### NEW-7 — "Back to Dashboard" button on notification page

**Fix:** `notifications_view` now passes `user_dashboard_url: get_user_dashboard(request.user)` to context (function already imported from `.decorators`). Template header now shows `← Dashboard` button (`btn-outline-secondary btn-sm`) linking to `user_dashboard_url` — routes each role to their own dashboard.

**Files:** `projects/views.py`, `projects/templates/projects/notifications.html`

---

## FILES CHANGED THIS SESSION

| File | What changed |
|---|---|
| `projects/templates/dashboard/scm.html` | BUG-SCM-5 amber border; BUG-SCM-1 modal fields; BUG-DES-1/SCM-2 stat grid; NEW-4a Overview button |
| `projects/templates/dashboard/design.html` | BUG-DES-1/SCM-2 stat grid |
| `projects/templates/projects/project_overview.html` | BUG-DES-2 section reorder; Row 2 Recent Activity; Row 3 merge; collapses open by default |
| `projects/templates/projects/notifications.html` | NEW-7 Back to Dashboard button |
| `projects/views.py` | BUG-SCM-6 boq_map key fix; NEW-7 user_dashboard_url in notifications context |

---

## MIGRATION STATE (unchanged)

No migrations added. Latest migration remains 0029.

---

# PROJECT_CONTEXT.md — 24 June 2026, Session 4
# Covers: Thread 4 (task drill-down + stat blocks) + Thread 5 (Raise Issue validation, Finance contract block, project overview UX, DC color coding)

---

## HEADLINE

Two full feature threads shipped and browser-verified. No migrations. Board demo deadline 4 July 2026.

---

## THREAD 4 — Task Drill-Down + Due-Date Stat Blocks

### New URLs

```python
path('tasks/due-today/', views.tasks_drill_down, {'filter_type': 'due-today'}, name='tasks_due_today'),
path('tasks/due-soon/',  views.tasks_drill_down, {'filter_type': 'due-soon'},  name='tasks_due_soon'),
path('tasks/overdue/',   views.tasks_drill_down, {'filter_type': 'overdue'},   name='tasks_overdue'),
```

### New view — `tasks_drill_down` (views.py)

Single view handles all 3 filter types via URL kwarg. Role-based scoping:
- PM → `phase__project__assigned_pm=profile`
- Design → `phase__project__assigned_design=profile`
- Site Engineer → `phase__project__assigned_site_engineer=profile`
- SCM / others → all active non-deleted projects

Filter definitions:
- **Due today:** `due_date=today, status in [Not Started, In Progress, Blocked]`
- **Due soon:** `due_date > today AND <= today+7, status in active`
- **Overdue:** `due_date < today, status != Done`

All filters guard `due_date__isnull=False` and `is_deleted=False` on project.

Groups tasks by project (dict keyed on `project_id`), passes as list of dicts to template. Back button URL resolved per role via `_ROLE_DASHBOARD` dict + `reverse()`.

### New template — `projects/templates/tasks/task_drill_down.html`

Lucide arrow-left back button (role-aware), task count summary, project-grouped list. Task names link to `task_detail` URL. Due dates shown formatted.

### Stat blocks added to 4 dashboards

3 new count queries added to each view (`tasks_due_today`, `tasks_due_soon`, `tasks_overdue`), using the same filter definitions as the drill-down view.

| Dashboard | Context key pattern | Grid |
|---|---|---|
| PM | `summary.tasks_due_today` etc. | `col-12 col-sm-6 col-lg-4` (new row, first stat row on this dashboard) |
| Design | Top-level keys | Extended from 3→6 blocks, same grid |
| SCM | `summary.tasks_*` | Extended from 3→6 blocks, same grid |
| Site Engineer | Top-level keys | Extended from 4→7 blocks, `col-6 col-md-3` |

### PM "Due Today" badge on project cards

Gold (`#f0a829`) pill badge in collapsed card header showing per-project due-today count. Hidden when 0. Computed via `due_today_for_project` query inside the existing per-project loop.

---

## THREAD 5 — Raise Issue Validation, Finance Block, Overview UX, DC Color

### NEW-6 — Raise Issue modal: Assign To + Due Date mandatory

7 modal instances updated. PM and SE modals were missing both fields — added to match SCM pattern. DC detail modal keeps Due Date absent (delivery issues — Due Date optional by design).

| Template | Modal ID | Fields added | Validation |
|---|---|---|---|
| `dashboard/pm.html` | `raiseIssueModal` | Assign To + Due Date (new) | Both mandatory |
| `dashboard/site-engineer.html` | `seIssueModal` | Assign To + Due Date (new) | Both mandatory |
| `dashboard/scm.html` | `scmRaiseIssueModal` | — (already had both) | Both mandatory |
| `projects/project_overview.html` | `raiseProjectIssueModal` | — (already had both) | Both mandatory |
| `projects/task_detail.html` | `raiseIssueModal` | — (already had both) | Both mandatory |
| `projects/project_detail.html` | raise issue modal | — (already had both) | Both mandatory |
| `projects/delivery_challan_detail.html` | `raiseDcIssueModal` | — (Assign To already present) | Assign To only |

Validation: JS intercepts submit, inline `<div class="text-danger small mt-1">` error below failing field, `e.preventDefault()`. Errors clear on modal hide. Backend unchanged — `create_project_issue` already accepted both fields as optional.

### Finance — Total Client Contract Value stat block

New aggregation in `dashboard_finance`: `Sum('contract_value')` across active non-deleted projects. Stat row restructured from 3×`col-md-4` to 4×`col-md-3`. New block shows `₹{{ total_client_contract_value|floatformat:0 }}`. Finance + BD existing blocks left as plain text — none map to task drill-down URLs.

### Project overview — Activity block open by default

`#activityCollapse` default changed from closed to open (`collapse` → `collapse show`, `aria-expanded="false"` → `"true"`). Only affects projects with more than 3 activity entries. BOQ, DC, Materials were already open by default.

### NEW-8 — DC number color-coded by status in project overview

DC number link in Delivery Challan table colored by `dc.status` (inline style). Received = `#1a7a4a`, Partially Received = `#f0a829`, Expected/Rejected = `#dc3545`. Template-only change — severity is already encoded in `dc.status` by `recalculate_dc_status()`.

---

## FILES CHANGED — SESSION 4

| File | What changed |
|---|---|
| `projects/urls.py` | 3 new task drill-down URL patterns |
| `projects/views.py` | `tasks_drill_down` view; stat block counts on PM/Design/SCM/SE views; `total_client_contract_value` aggregation in Finance view |
| `projects/templates/tasks/task_drill_down.html` | New template (new `tasks/` subdirectory) |
| `projects/templates/dashboard/pm.html` | Stat blocks row; PM card "Today: N" badge; Raise Issue modal fields + validation |
| `projects/templates/dashboard/design.html` | 3 new stat blocks (extended 3→6) |
| `projects/templates/dashboard/scm.html` | 3 new stat blocks (extended 3→6); Raise Issue validation |
| `projects/templates/dashboard/site-engineer.html` | 3 new stat blocks (extended 4→7); Raise Issue modal fields + validation |
| `projects/templates/dashboard/finance.html` | Stat row restructured 4×col-md-3; Total Client Contract Value block |
| `projects/templates/projects/project_overview.html` | Raise Issue validation; Activity collapse open by default; DC number color coding |
| `projects/templates/projects/task_detail.html` | Raise Issue modal validation |
| `projects/templates/projects/project_detail.html` | Raise Issue modal validation |
| `projects/templates/projects/delivery_challan_detail.html` | Raise Issue Assign To validation |

---

## MIGRATION STATE (unchanged)

No migrations added. Latest migration remains 0029.

---

## SPRINT STATUS

- Hard deadline: 4 July 2026 (board demo)
- Current migration: 0029
- Sessions 1–4 complete (24 June): bug fixes + Thread 4 + Thread 5 all done
- Next: push to Railway, smoke-test all role dashboards on production, board demo prep

---

# PROJECT_CONTEXT.md — 24 June 2026, Session 3
# Covers: Finance role fixes — task dropdowns, milestone flow, bidirectional sync, payment capture, In Progress due date, Raise Issue

---

## HEADLINE

All Finance role bugs from the audit list resolved. No migrations. All changes browser-verified.

---

## BUGS FIXED + FEATURES ADDED

### BUG-FIN-1 — Finance task list greyed out (read-only) on project overview

**Root cause:** `project_overview.html` had an explicit Finance exclusion on the task status dropdown condition:
```django
{% if role != 'Finance' and is_assigned_pm or role != 'Finance' and user_task_role == task.assigned_role %}
```

**Fix:** Removed the Finance exclusion entirely. Condition simplified to:
```django
{% if is_assigned_pm or user_task_role == task.assigned_role %}
```

Backend (`task_status_update` view) had no Finance exclusion — only the template did.

**File:** `projects/templates/projects/project_overview.html`

---

### NEW-9 — Invoice button should not appear for Finance on project overview

**Root cause:** Finance milestone card showed both an Invoice button (Pending→Invoiced) and a Receive button (Invoiced→Received) on the project overview.

**Fix:** Removed the Invoice form/button entirely. Receive button kept. Decided subsequently (see Invoiced step elimination below) to remove Receive button too and replace with pencil icon flow.

**File:** `projects/templates/projects/project_overview.html`

---

### Invoiced step eliminated from payment milestone workflow

**Decision:** Finance goes directly Pending → Received. The Invoiced intermediate state is no longer used from project_overview.

**Changes:**
- `milestone_receive` view: guard changed from `if milestone.status != 'Invoiced':` → `if milestone.status == 'Received':` — allows Pending milestones through
- `project_overview.html`: Receive button condition was `m.status == 'Invoiced'` → removed entirely (pencil icon flow replaces it)

**File:** `projects/views.py`, `projects/templates/projects/project_overview.html`

---

### Finance edit pencil (milestone metadata + payment capture)

**Finance now sees the pencil edit icon on milestone rows** (previously PM-only).

**Pencil form for Finance shows:** description (editable) + amount_received (entering an amount marks milestone as Received).
**Pencil form for PM shows:** description + expected amount + due_date (unchanged).

**POST gate fixed:** `project_overview` view POST block was gated on `is_assigned_pm` only. Finance POST to `update_milestone` was silently ignored. Fixed:
```python
# Before:
if request.method == 'POST' and is_assigned_pm:
# After:
if request.method == 'POST' and (is_assigned_pm or (role == 'Finance' and request.POST.get('action') == 'update_milestone')):
```

**`update_milestone` handler** now branches on role: Finance path saves `amount_received`, sets `status='Received'`, `received_date=today()`, triggers task sync. PM path unchanged.

**Carry-forward rule:** When adding Finance write access to any POST block gated on `is_assigned_pm`, expand the gate condition to include the specific Finance action. Never silently ignore Finance POSTs.

**Files:** `projects/views.py`, `projects/templates/projects/project_overview.html`

---

### Bidirectional sync — Finance confirmation tasks ↔ PaymentMilestone

Three Finance tasks in the residential template map to milestones:
- "Advance Payment Confirmation" (Phase 1, task 2) ↔ M1
- "Finance Confirmation" (Phase 5, task 6) ↔ M2
- "100% Payment Confirmation" (Phase 9, task 1) ↔ M3

**Task → Milestone (implemented in `task_status_update`):** When Finance (or PM) marks one of these tasks Done, the corresponding milestone auto-sets to `status='Received'`, `received_date=today()`. Amount_received and variance_reason are also saved if supplied in the POST.

**Milestone → Task (implemented in `milestone_receive` and `update_milestone`):** When a milestone is marked Received (via either path), the corresponding Finance confirmation task auto-sets to `status=Done`, `completed_at=now()`.

Both directions wrapped in `try/except` — sync failures never block the primary operation. Sync skips if target is already in the desired state (filtered by `status__in`).

**Mapping is by task name string** — names are fixed in the residential template (`attach_residential_template()`). No migration needed.

**Files:** `projects/views.py`

---

### Payment capture modal — Finance marks confirmation task Done from task list

When Finance (or PM) selects "Done" on one of the 3 Finance confirmation tasks from the task list dropdown, a `#paymentCaptureModal` intercepts before submission.

**Modal fields:** Amount Received (₹, optional) + Note/variance_reason (optional) + "Save & Mark Done" button + "Skip" button (marks Done without payment info).

**JS flow:**
1. `handleTaskStatusChange` fires — step 2.5 checks `_FINANCE_CONF_TASKS.indexOf(select.dataset.taskName)` and `bsPaymentModal`
2. If match: resets dropdown to original status, sets modal form action to task's `task_status_update` URL, shows modal
3. Modal submit: POSTs `status=Done` + `amount_received` + `variance_reason` to `task_status_update`
4. Skip: dismisses modal via `hidden.bs.modal` event, then submits task form with `status=Done`

**`data-task-name` attribute** added to status `<select>` elements.

**State machine fix:** `NOT_STARTED → DONE` added to `VALID_TRANSITIONS`. Finance confirmation tasks can be marked Done directly from Not Started (they are acknowledgment tasks, not work tasks).

**Files:** `projects/templates/projects/project_overview.html`, `projects/views.py`

---

### Finance In Progress — inline due date picker

Finance could mark tasks Done (after state machine fix) but not In Progress, because the due_date input was PM-only. This was a logical inconsistency.

**Fix:**
- A hidden `<input type="date" name="due_date">` is now inside the Finance status form, rendered when `not is_assigned_pm and not task.due_date`.
- When Finance selects "In Progress", existing JS step 2 finds this input, calls `dateInput.style.display = ''` to reveal it, highlights it red, and focuses it.
- When Finance picks a date, `onchange` sets `select.value = 'In Progress'` and submits the form with both `status=In Progress` and `due_date`.
- `task_status_update` view reads `due_date` from POST; if present and task has no due_date, saves it before the In Progress guard check — guard then passes.

**Files:** `projects/templates/projects/project_overview.html`, `projects/views.py`

---

### Thread 5 — Raise Issue available to Finance on project overview

**Root cause:** `project_overview.html` had `{% if role != 'Finance' %}` gating the "Raise Issue" button in the Project Issues card header.

**Fix:** Removed the gate entirely. `create_project_issue` view already accepts all roles.

**File:** `projects/templates/projects/project_overview.html`

---

## FILES CHANGED THIS SESSION

| File | What changed |
|---|---|
| `projects/templates/projects/project_overview.html` | BUG-FIN-1 task dropdown gate; NEW-9 Invoice button removed; Receive button removed; Finance pencil icon + form; payment capture modal HTML + JS; In Progress hidden due_date input; JS step 2 reveal + step 2.5 intercept; DOMContentLoaded init; Thread 5 Raise Issue gate removed |
| `projects/views.py` | `milestone_receive` guard (Pending allowed); POST gate Finance update_milestone; `update_milestone` Finance/PM branch; task→milestone sync (amount_received + variance_reason); milestone→task sync in `update_milestone`; state machine NOT_STARTED→DONE; due_date save before In Progress guard |

---

## KNOWN GOTCHAS — new from this session

- **`project_overview` POST gate is `is_assigned_pm`-only:** Finance POSTs to `update_milestone` were silently ignored. Any future Finance write action via `project_overview` POST must expand the gate condition explicitly.
- **`VALID_TRANSITIONS[NOT_STARTED]` now includes DONE:** Finance confirmation tasks (acknowledgment tasks) can skip In Progress. All other task types can also now skip In Progress — acceptable because the attachment check still warns on Done with no files.
- **Finance due_date input is inside the status form:** It POSTs `due_date` to `task_status_update`, not to `task_set_due_date`. The view saves it before the guard. PM's due_date input is a separate form in the due_date column posting to `task_set_due_date` — these are two different mechanisms for two different roles.
- **Payment capture modal is inside `{% if role == 'Finance' %}` block:** PM marking a Finance confirmation task Done from the task list does NOT see the payment capture modal (modal not rendered for PM). PM can still mark tasks Done normally; the sync fires without payment info.
- **Bidirectional sync is name-based:** Task names 'Advance Payment Confirmation', 'Finance Confirmation', '100% Payment Confirmation' are hardcoded. If task names in the residential template ever change, the sync will silently stop firing. Never rename these tasks without updating both `_FINANCE_TASK_TO_MILESTONE` dicts.

---

## MIGRATION STATE (unchanged)

No migrations added. Latest migration remains 0029.

---

# PROJECT_CONTEXT.md — 20-22 June 2026
# Covers: Dashboard redesigns (SE / Design / BD / CEO / SCM), VendorBrand model, Task.blocked_since, Project soft-delete

---

## HEADLINE

Major sprint: all remaining role dashboards redesigned to the one-card-per-project pattern.
VendorBrand multi-brand system built. Project soft-delete implemented. Latest migration: 0029.

---

## DASHBOARD REDESIGNS (no migrations)

### SE Dashboard — one card per project
- Sorted by urgency (overdue + blocked + pending GRN + open issues — descending)
- Urgency circle: combined count; green checkmark if zero
- Collapsed card: project name, Delayed/On-time badge, urgency circle
- Expanded card: phase progress, SE-assigned task list with status, Raise Issue + View Project buttons
- View Project → `/projects/<id>/overview/` (same URL as PM)
- Task status updates and GRN confirmation happen on project_overview, NOT dashboard card

### Design Dashboard — one card per project
- Sorted by BOQ urgency (Revision Requested first, then others)
- Card shows BOQ status badge, task progress, urgency badge
- Summary stat row: Revision Requested count, Pending Approval count
- BOQ Revision Requested badge links directly to `/projects/<id>/boq/`

### BD Dashboard — one card per project
- Read-only; no POST actions
- One card per project: ORC status + M1/M2/M3 payment milestone badges
- ORC = Order Confirmation Receipt (business document — NOT OCR)
- BD does NOT create projects; projects arrive via Zoho webhook → PM activates → BD uploads ORC

### CEO Dashboard
- Portfolio health cards: Total Active, Commissioned This Month, Total Contracted Value (₹), At Risk count
- Project status badges table: all projects, status/type/phase/PM/capacity
- Department KPI summary table: PM/SCM/Design/BD/SE/Finance row-wise counts
- Read-only view across all projects; no role isolation

### SCM Dashboard
- Vendor quick-link button per project card → `/vendors/` filtered or plain list
- Per-project 4-stage pipeline display (BOQ → Order → Delivery → Issues)
- Stall indicator on pipeline stages

### PM Dashboard — delivery issues panel
- Delivery Issues panel added inside each expanded project card
- Pulls open DC-scoped issues per project
- Links to `delivery_challan_detail` for each issue

---

## VENDORBRAND MODEL (migrations 0022/0023)

New model: `VendorBrand`
- FK Vendor CASCADE (related_name='brands')
- FK VendorCategory nullable SET_NULL (related_name='brands')
- `make_brand` CharField(max_length=200)
- `is_active` BooleanField(default=True)

Rules:
- `category` nullable — null means brand appears across ALL categories that vendor supplies
- Non-null scopes brand to one category only
- Vendors with no brands fall back to company name in BOQ dropdowns
- Multi-brand form on vendor edit page — inline formset pattern
- BOQ `make_preference` dropdown filters VendorBrand.make_brand by BOQItem.category
- BOQ `ordered_vendor` column same brand-aware pattern for SCM

Horizon Solar logo asset added to `static/images/honor-logo-1.png`.

---

## TASK.BLOCKED_SINCE (migration 0024)

New field: `Task.blocked_since = DateTimeField(null=True, blank=True)`
- Set when status transitions TO 'Blocked'
- Cleared on un-block (re-blocks age from zero — not cumulative)
- Used by dashboards to show "blocked N days" badge
- No trigger in model — set/cleared explicitly in `task_status_update` view

---

## PROJECT SOFT-DELETE (migration 0029)

New fields on Project:
- `is_deleted = BooleanField(default=False)`
- `deleted_at = DateTimeField(null=True, blank=True)`

### View — `project_delete`
- POST only, Admin role only
- Sets `is_deleted=True`, `deleted_at=now()` — does NOT change `status`
- Redirects to project list with success message

### Admin overrides (`projects/admin.py`)
- `delete_model`: soft-delete instead of hard (preserves history)
- `get_actions`: removes built-in `delete_selected` (avoids misleading cascade-warning page)
- Custom actions: `soft_delete_selected`, `restore_selected`
- `get_queryset`: hides `is_deleted=True` by default; filter `is_deleted__exact=1` in admin URL to reveal

### Frontend (Admin role only)
- Project list: trash icon column per row; Bootstrap confirmation modal
- Project overview header: "Delete" button; same modal pattern
- Both use CSRF-protected POST form inside Bootstrap modal

### CRITICAL GOTCHA
`project_delete` sets `is_deleted=True` only — does NOT change `status`. Deleted projects remain
`Active` or `In Progress` in the DB. Every dashboard queryset that filters `status__in=[...]` will
silently include deleted projects unless `is_deleted=False` is also added. Reference pattern:
`project_list` view — already has it.

---

## MIGRATION STATE (20-22 June)

| Migration | Name | Contents |
|---|---|---|
| 0022 | vendor_make_brand | VendorBrand initial model |
| 0023 | vendor_brand | VendorBrand refinements (is_active etc.) |
| 0024 | task_blocked_since | Task.blocked_since DateTimeField |
| 0029 | project_soft_delete | Project.is_deleted + Project.deleted_at |

(0025–0028 are the notification system — documented in the 22 June entry above.)

---

# PROJECT_CONTEXT.md — 25-26 June 2026
# Covers: WhatsApp delivery tracking (0030), bug fixes on notification call sites + residential template

---

## HEADLINE

WhatsApp delivery tracking added end-to-end. Six bug fixes shipped. Migration 0030 applied.

---

## WHATSAPP DELIVERY TRACKING (migration 0030)

Two new fields on `NotificationLog`:
- `interakt_message_id = CharField(max_length=100, blank=True, default='')` — Interakt's message ID, stored from API response
- `delivery_status = CharField(max_length=30, choices=DELIVERY_STATUS_CHOICES, default='')` — updated by Interakt webhook

Delivery status values (priority order, lower → higher):
`sent` → `delivered` → `read` (higher-priority events always win — never overwrite with a lower-priority status)

### Interakt delivery webhook — `/webhooks/interakt/delivery/`
- `@csrf_exempt`, no login required (machine-to-machine)
- HMAC-SHA256 signature validation: `sha256=<hex>` — note the `sha256=` prefix (must prepend to expected digest before `compare_digest`)
- Updates `NotificationLog.delivery_status` for matching `interakt_message_id` using priority check
- Always returns HTTP 200

### Admin WhatsApp Log — `/portal/whatsapp-log/`
- Admin role only
- Table: channel, status, delivery_status, template_name, recipient, project, created_at
- Link from Admin dashboard

### show_notification_log management command
- `python manage.py show_notification_log` — prints last 20 NotificationLog rows
- For local diagnostics only

---

## BUG FIXES (25-26 June)

### Fix: Interakt webhook signature — must prepend 'sha256='
**Root cause:** signature comparison was against raw hex digest; Interakt sends `sha256=<hex>`.
**Fix:** Expected digest built as `'sha256=' + hmac.new(...).hexdigest()` before `compare_digest`.

### Fix: WhatsApp log 500 — Project has no 'name' field
**Root cause:** notification context used `project.name`; Project model uses `project.customer_name`.
**Fix:** All notification helper functions updated to use `project.customer_name`.

### Fix: Email channel missing from 7 notification triggers
**Root cause:** Original implementation only wired WhatsApp channel; email calls were missing.
**Fix:** All 7 `send_notification()` call sites now pass both 'whatsapp' and 'email' channels.

### Fix: 3 WhatsApp param bugs
Three templates had wrong variable counts or order in production call sites (post-batch-1 review).
All corrected to match confirmed Interakt-approved param structures.

### Fix: Residential template milestone flags
`attach_residential_template()` was not setting `is_payment_milestone=True` on the 3 milestone tasks.
**Fix:** M1 (Phase 2), M2 (Phase 6 task 4), M3 (Phase 8 task 6 — Plant Commissioning) now flagged.
This was causing the `payment_notification` to never fire from real task completions.

### Fix: Block status change on unassigned tasks
`task_status_update` was allowing Blocked transition on tasks with no `assigned_to`.
**Fix:** Guard added — returns 400 JSON error if `task.assigned_to is None` on Blocked attempt.

---

## MIGRATION STATE (25-26 June)

| Migration | Name | Contents |
|---|---|---|
| 0030 | whatsapp_delivery_tracking | NotificationLog.interakt_message_id + delivery_status |

---

# PROJECT_CONTEXT.md — 27 June 2026
# Covers: My Documents page, DesignSubmission model (0031), navbar username dropdown

---

## HEADLINE

Personal document archive page built for all roles. New DesignSubmission model. Navbar updated.
Migration 0031 applied.

---

## DESIGNSUBMISSION MODEL (migration 0031)

New model in `projects/models.py`:

```python
class DesignSubmission(models.Model):
    STATUS_CHOICES = [('Pending','Pending'), ('Approved','Approved'), ('Rejected','Rejected')]

    project      = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='design_submissions')
    submitted_by = models.ForeignKey('UserProfile', on_delete=models.SET_NULL,
                                     null=True, blank=True, related_name='design_submissions')
    title        = models.CharField(max_length=200)
    description  = models.TextField(blank=True, default='')

    # Supabase storage — same three-field pattern as ProjectDocument / TaskAttachment
    file_name    = models.CharField(max_length=255, blank=True, default='')
    file_url     = models.URLField(max_length=1000, blank=True, default='')
    supabase_path = models.CharField(max_length=500, blank=True, default='')

    status       = models.CharField(max_length=20, choices=STATUS_CHOICES, default='Pending')
    submitted_at = models.DateTimeField(auto_now_add=True)

    reviewed_by  = models.ForeignKey('UserProfile', on_delete=models.SET_NULL,
                                      null=True, blank=True, related_name='reviewed_design_submissions')
    reviewed_at  = models.DateTimeField(null=True, blank=True)
    review_notes = models.TextField(blank=True, default='')

    class Meta:
        ordering = ['-submitted_at']
```

**NOTE: No create flow yet.** Model + detail view exist; Design Submissions section shows empty until
a submit form is built. File fields are blank=True to allow migration without data.

---

## MY DOCUMENTS PAGE — `/profile/documents/`

### View — `my_documents`
Role-aware personal document archive. All querysets capped at `[:50]` (no pagination yet).

| Section | Roles | Query | Link target |
|---|---|---|---|
| A — Uploaded Files | All | `TaskAttachment + ProjectDocument` filtered by `uploaded_by=profile, is_deleted=False` | Download (Supabase URL) |
| B — BOQ Submissions | Design | `BOQ.filter(submitted_by=profile)` | `boq_detail` |
| C — Design Submissions | Design | `DesignSubmission.filter(submitted_by=profile)` | `design_submission_detail` |
| D — Delivery Challans | SCM | `DeliveryChallan.filter(created_by=profile)` | `delivery_challan_detail` |
| E — Payment Requests | SCM | `PaymentRequest.filter(requested_by=request.user)` | `payment_request_detail` |

**CRITICAL — Section E:** `PaymentRequest.requested_by` is FK to `auth.User` (NOT UserProfile).
Filter uses `requested_by=request.user` (the Django User object), never `requested_by=profile`.

**CRITICAL — Task path:** `TaskAttachment` has no direct `project` FK. Path is `task → phase → project`.
`select_related` must be `'task__phase__project'`; template uses `att.task.phase.project.project_id`.

**CRITICAL — BOQ fields:** `BOQ.submitted_by` (not `created_by`), `BOQ.submitted_at`
(not `created_at`). `submitted_at` is nullable — use `|default:"—"` in templates.

### New views added
- `my_documents` — main listing page
- `design_submission_detail` — read-only; submitter + PM + Admin access
- `payment_request_detail` — read-only; SCM + Finance + PM + Admin access

### New URLs added
```python
path('profile/documents/',                                            views.my_documents,             name='my_documents'),
path('design-submissions/<int:pk>/',                                  views.design_submission_detail, name='design_submission_detail'),
path('projects/<str:project_id>/payment-requests/<int:request_id>/', views.payment_request_detail,   name='payment_request_detail'),
```

### New templates
- `projects/templates/projects/my_documents.html`
- `projects/templates/projects/design_submission_detail.html`
- `projects/templates/projects/payment_request_detail.html`

---

## NAVBAR USERNAME DROPDOWN

Replaced standalone "My Documents" link with a click-based dropdown under the username.

**HTML structure** (`base.html`):
```html
<div class="position-relative" id="user-hover-menu">
  <span class="text-white-50 small">{{ user.get_full_name|default:user.username }}</span>
  <div id="user-hover-dropdown">
    <a href="{% url 'my_documents' %}" class="dropdown-item small rounded">
      <i class="bi bi-folder2-open me-2"></i>My Documents
    </a>
  </div>
</div>
```

**Behaviour:** hidden by default (CSS `display:none`), `.open` class sets `display:block`.
Click on `#user-hover-menu` toggles `.open` on `#user-hover-dropdown`.
Outside-click listener on `document` removes `.open`.
Pure CSS + JS — no Bootstrap dropdown JS dependency.

---

## PHASE 2 BACKLOG (as of 28 June)

- DesignSubmission create/submit form for Design users (file upload + title + description)
- DesignSubmission approval flow for PM (Approved/Rejected + review notes)
- PaymentRequest listing page for Finance role
- ORC upload flow for BD role
- Task dependencies (TaskDependency model, predecessor validation)
- GRN confirmation mandatory-proof enforcement (currently warn-but-allow)
- Pagination on My Documents sections (currently `[:50]` hard cap)

---

## DAY 16 — 28 June 2026

### Deployment fixes (Railway crash + logo 404)

**ImportError on startup:** `views.py` imported `AdminUserEditForm` but `forms.py` defining it had not been committed. Fix: staged and pushed `forms.py` along with 7 new admin templates.

**Logo 404 (two causes):**
1. File named `honor-logo-1.png` but templates referenced `horizon-logo.png` — renamed file, updated `base.html`.
2. `Procfile` ran `migrate` but not `collectstatic`; `nixpacks.toml` ran `collectstatic` but not `migrate`. Whichever Railway used, static files were never collected. Fix: both files now run `migrate && collectstatic && gunicorn`.

**Procfile (current):**
```
web: python manage.py migrate --run-syncdb && python manage.py collectstatic --noinput && gunicorn solarpms.wsgi --log-file -
```

**nixpacks.toml (current):**
```toml
[start]
cmd = "python manage.py migrate --run-syncdb && python manage.py collectstatic --noinput && gunicorn solarpms.wsgi --log-file -"
```

---

### Admin Panel — Project List

Moved the project list out of the generic `/projects/` URL and into the Admin Panel.

**New URL:** `GET /portal-admin/projects/` → `admin_project_list` (Admin only)
**Sidebar link:** "All Projects" section added above "Settings" in `admin_base.html`
**Template:** `projects/templates/projects/admin/projects_list.html` — Tailwind table, 11 columns:
Project ID, Customer, Type, Status, Current Phase, Assigned PM, Capacity (kW), Contract Value (₹), Target Date, Created, (actions)

**`/projects/` URL** (`project_list` view) is now a smart redirect — no longer renders a list:
- Admin → `/portal-admin/projects/`
- PM → `/dashboard/pm/`
- CEO → `/dashboard/ceo/`
Named URL kept so `{% url 'project_list' %}` references in `project_overview.html` ("← Projects" button) continue to work.

---

### Activation date on all dashboards

`Project.activated_at` (DateTimeField, nullable, stamped on `project_activate`) is now displayed on every role's project card and in the project overview header.

| Template | Location |
|---|---|
| `dashboard/pm.html` | After project ID in collapsed card header |
| `dashboard/bd.html` | Project-ID subtitle line |
| `dashboard/finance.html` | Project-ID subtitle line |
| `dashboard/design.html` | Project-ID subtitle line |
| `dashboard/ceo.html` | Below target date in expanded card body |
| `projects/project_overview.html` | Already present as info-pill — no change needed |

---

### Zoho webhook — PM assignment redesign

**Removed:** fallback that assigned all unresolved projects to `chetan@horizonrenewablepower.com`.

**New behaviour when Zoho `Assign_PM` email doesn't match any user:**
- `assigned_pm = None` — project is created as Draft with no PM
- In-app notification sent to first Admin profile ("PM could not be assigned. Please assign a PM.")
- Direct email sent to `smzk07@gmail.com` via new `send_raw_email()` helper (bypasses master switch and UserProfile lookup — platform-level alert)
- Email body includes Project ID, Customer, City, Zoho Deal ID, the PM email from Zoho (or blank), and a direct URL to the project overview

**`send_raw_email(to_email, subject, body)`** — new function in `projects/notifications.py`. Sends ZeptoMail directly to any email address without requiring a UserProfile. Does NOT check `SystemSettings.email_enabled` — reserved for system-level alerts only.

---

### Admin: Assign PM to unassigned projects

Unassigned Draft projects (e.g. from Zoho webhook with no matching PM) can now be assigned directly from the Admin Panel project list.

**New URL:** `POST /portal-admin/projects/<project_id>/assign-pm/` → `admin_assign_pm` (Admin only)

**Table cell behaviour:** When `project.assigned_pm` is None, the Assigned PM cell shows an amber "Assign PM" button (user-plus icon). The cell has `onclick="event.stopPropagation()"` so clicking it doesn't navigate to the project.

**Assign PM modal:** Opens on button click; populated with project ID + customer name from data attributes. Dropdown lists all active PM-role UserProfiles alphabetically. Helper text: "The project stays as Draft. The PM will review and activate it."

**`admin_assign_pm` view:**
- POST only (GET redirects to list)
- Validates `pm_user_id` → must be active PM role
- Sets `project.assigned_pm`, saves with `update_fields=['assigned_pm']`
- Calls `log_activity()` — logs old PM name if this was a reassignment
- Fires `assign_project` notification to PM (in_app + whatsapp + email, same payload as webhook)
- `messages.success` toast, redirects to `admin_project_list`

**`admin_project_list` view** now also passes `pm_users` (all active PMs) to the template for the dropdown.

---

### PM dashboard — Draft Projects section

PMs now see Draft projects assigned to them (Zoho-created or manually created but not yet activated) in a dedicated section above "My Projects".

**Query:** `Project.objects.filter(assigned_pm=pm_profile, status='Draft', is_deleted=False).order_by('-created_at')`

**Card design:** Amber left-border (3px `#f0a829`), shows customer name, project ID, city, capacity, created date, amber "Draft" badge. Two action buttons: **View** (→ project overview) and **Activate** (→ `project_activate`, with `onclick=confirm()` dialog).

Section is hidden entirely when no drafts exist. Context key: `draft_projects`.

Also added `is_deleted=False` guard to the existing active-projects loop in `dashboard_pm` (was missing).

---

### Locked decisions updated

- **Removed:** default PM fallback `chetan@horizonrenewablepower.com` for unresolved Zoho `Assign_PM`. Unresolved Zoho projects now land with `assigned_pm=None`.
- **Added:** Admin can assign PM from the Admin Panel project list. Admin gets email alert at `smzk07@gmail.com` for unassigned projects.

---

## MIGRATION STATE — COMPLETE SEQUENCE (28 June 2026)

| Migration | Name | Contents |
|---|---|---|
| 0001–0013 | scaffold | All core models through PaymentMilestone + commissioned_at |
| 0014 | webhook_fields | zoho_deal_id + customer_contact_person on Project |
| 0015 | file_uploads | ProjectDocument + TaskAttachment |
| 0016 | issue_activitylog | Issue + ActivityLog |
| 0017 | comment | Comment |
| 0018 | delivery_challan | DeliveryChallan + DCLineItem |
| 0019 | issue_delivery_challan_fk | Issue.delivery_challan FK |
| 0020 | dclineitem_damaged_quantity | DCLineItem.damaged_quantity |
| 0021 | add_payment_request | PaymentRequest |
| 0022 | vendor_make_brand | VendorBrand initial |
| 0023 | vendor_brand | VendorBrand refinements |
| 0024 | task_blocked_since | Task.blocked_since |
| 0025 | userprofile_notification_prefs | UserProfile.email_notifications + whatsapp_notifications |
| 0026 | notification_log | NotificationLog |
| 0027 | system_settings | SystemSettings |
| 0028 | task_is_payment_milestone | Task.is_payment_milestone |
| 0029 | project_soft_delete | Project.is_deleted + Project.deleted_at |
| 0030 | whatsapp_delivery_tracking | NotificationLog.interakt_message_id + delivery_status |
| 0031 | design_submission | DesignSubmission |

No new migrations in Day 16.

---

## SPRINT STATUS (28 June 2026)

- Hard deadline: 4 July 2026 (board demo) — 6 days remaining
- Current migration: 0031 (no change)
- SystemSettings.whatsapp_enabled / email_enabled = False by default — flip ON before demo via Django admin
- All 8 role dashboards live and redesigned
- Notification system built and WhatsApp-verified (all 7 templates return HTTP 201 from Interakt)
- My Documents page live for all roles
- Zoho webhook: unassigned projects now alert Admin via in-app + email to smzk07@gmail.com
- Admin can assign PM to any project via Admin Panel modal
- PM sees Draft projects in dedicated dashboard section
- Board demo centerpiece: Task "Plant Commissioning" (M3) marked Done by SE → WhatsApp + email fires to Finance + PM + CEO → CEO dashboard updates

---

## DAY 17 — 28 June 2026 (Session 2)

### CEO Dashboard: Finance Summary Strip

Added three finance stat cards at the top of the CEO dashboard (above "Portfolio health"), mirroring the values already shown on the Finance dashboard:

1. **Payment Requests Pending** — count of `PaymentRequest` rows with `status=PENDING` on active projects
2. **Vendor Payments Outstanding** — sum of pending `PaymentRequest.amount` on active projects  
3. **Total Client Contract Value** — sum of `Project.contract_value` on active/in-progress projects (amber `#d97706` colour)

**View change:** `_get_ceo_dashboard_context()` in `views.py` — added Query 4 block computing `fin_payment_requests_pending`, `fin_vendor_payments_outstanding`, `fin_client_contract_value`. These are passed into the context dict alongside existing keys.

**Template change:** `projects/templates/dashboard/ceo.html` — new 3-column card row inserted at the top of `{% block content %}`, before the "Portfolio health" section label.

---

### CEO Dashboard: is_deleted Bug Fix

**Bug:** After soft-deleting a project, CEO dashboard numbers (project count, tasks, issues, contract value, payment requests) did not update even after page refresh. Other role dashboards were correct.

**Root cause:** `_get_ceo_dashboard_context()` was missing `is_deleted=False` on all four queries:
- Query 1 (projects): filtered only on `status__in=active_statuses` — missing `is_deleted=False`
- Query 2 (tasks): filtered `phase__project__status__in` only — missing `phase__project__is_deleted=False`
- Query 3 (issues): filtered `project__status__in` only — missing `project__is_deleted=False`
- Query 4 (finance, newly added): `active_filter` dict was missing `project__is_deleted: False`

**Fix:** Added `is_deleted=False` / `is_deleted__isnull=False` traversal guards to all four queries inside `_get_ceo_dashboard_context()`. No migrations needed.

**Commit:** `73dff0e` — "Add finance summary strip to CEO dashboard and fix is_deleted filter"

---

## SPRINT STATUS (28 June 2026 — End of Day 17)

- Hard deadline: 4 July 2026 (board demo) — 6 days remaining
- Current migration: 0031 (no change)
- SystemSettings.whatsapp_enabled / email_enabled = False by default — flip ON before demo via Django admin
- All 8 role dashboards live and redesigned
- CEO dashboard now shows finance summary strip (payment requests + vendor outstanding + contract value)
- CEO dashboard soft-delete filter fixed — numbers now reflect deleted projects correctly
- Notification system built and WhatsApp-verified (all 7 templates return HTTP 201 from Interakt)
- My Documents page live for all roles
- Zoho webhook: unassigned projects now alert Admin via in-app + email to smzk07@gmail.com
- Admin can assign PM to any project via Admin Panel modal
- PM sees Draft projects in dedicated dashboard section
- Board demo centerpiece: Task "Plant Commissioning" (M3) marked Done by SE → WhatsApp + email fires to Finance + PM + CEO → CEO dashboard updates