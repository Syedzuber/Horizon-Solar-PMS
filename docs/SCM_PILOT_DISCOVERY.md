# SCM pilot discovery — what is real, on a screen, today

**Audit-only session, 5 Sep 2026.** Read-only investigation. No application code was
changed by this session; the only file written is this one.

**What this is for.** The four OPEX delivery tasks are mirrors with no derivation hook —
that is confirmed again below and is *out of scope for this pilot*. This document asks a
different question: **separately from that broken task list, what can SCM genuinely do
today on one real site?** Tasks 1–6 are confirmed facts. Task 7 is a proposal and
**needs sign-off before the pilot plan is updated.**

## Evidence base, and its limits

Everything below was checked against **the local development Postgres**
(`solarpms_local`), which carries the full 0080 migration chain, plus a direct read of
the code on the working tree.

```
DB ENGINE: django.db.backends.postgresql NAME: solarpms_local HOST: localhost
```

Two caveats that matter when reading the counts:

* **Production is `origin/main`, which is 22 commits behind this working tree.** Migration
  `0075` (the OPEX template) *is* on `origin/main` — execution phase 1 was merged and
  deployed. `0076`–`0080` (two-step completion, punch points) are **not**. Nothing in this
  document depends on those five migrations.
* **Row counts are dev-database counts.** They are cited as evidence of *what state the
  system reaches*, not as a census of production.

Three walkthroughs were run as real HTTP requests through `django.test.Client`, logged in
as the actual SCM user (`subhash`), **inside a transaction that was rolled back**. Nothing
was persisted; each run ends by re-asserting the pre-run counts.

---

## TASK 1 — the four delivery mirrors: confirmed unchanged

**Nothing has changed.** All four are still `is_mirror=True`, still have no derivation
hook, and still sit permanently at `Not Started`.

The template rows, read live from `TaskTemplateTask`:

```
=== TaskTemplate rows ===
  code=OPEX version=1 status='active' tasks=23 mirrors=8
  code=RESIDENTIAL version=1 status='active' tasks=52 mirrors=0

=== TaskTemplateTask is_mirror=True ===
  OPEX v1 | phase 1 'Design' | order 1 | 'Design' | role='Design' | is_mirror=True
  OPEX v1 | phase 3 'Procurement & Delivery' | order 1 | 'Delivery — Solar Panels' | role='SCM' | is_mirror=True
  OPEX v1 | phase 3 'Procurement & Delivery' | order 2 | 'Delivery — Inverters' | role='SCM' | is_mirror=True
  OPEX v1 | phase 3 'Procurement & Delivery' | order 3 | 'Delivery — BOS Kit' | role='SCM' | is_mirror=True
  OPEX v1 | phase 3 'Procurement & Delivery' | order 4 | 'Delivery — MMS' | role='SCM' | is_mirror=True
  OPEX v1 | phase 7 'Closeout' | order 1 | 'COD' | role='PM' | is_mirror=True
  OPEX v1 | phase 7 'Closeout' | order 3 | 'As-Built Drawings' | role='Design' | is_mirror=True
  OPEX v1 | phase 7 'Closeout' | order 4 | 'HOTO' | role='PM' | is_mirror=True
```

One correction to the record, since it changes what a reader should assume: **prompt 1.5
*was* built.** `docs/OPEX_task_template_spec.md` on disk may still read v1.3, but migration
`0075` was corrected in place to v1.5 (23 tasks, 8 mirrors, Material Delivery split into
four) and its own header says so:

```
# CORRECTED IN PLACE BY PROMPT 1.5 to docs/OPEX_task_template_spec.md v1.5 §3:
# 7 phases, 23 tasks, 8 mirrors. The two inspections left Phase 3 and Material Delivery
# split into four. THE SEED LITERAL WAS EDITED RATHER THAN SUPERSEDED BY AN OPEX v2
```

Note also that `projects/views.py:4244` still says *"Five OPEX tasks carry the flag today
(Design, Material Delivery, COD, As-Built Drawings, HOTO)"*. That comment is stale — it is
eight, and Material Delivery is four rows. Cosmetic; not fixed by this audit.

### What activation actually produces

Rather than trust the template, an OPEX site was activated for real (`attach_opex_template`)
inside a rolled-back transaction:

```
SITE: TESTTENDER26-MB001 MB001 status= Draft
tasks created: 23

  ph1 Design                   #1 MIRROR status=Not Started  role=Design   assigned_to=None | Design
  ph2 Approvals (Pre-Installation) #1     status=Not Started  role=PM       assigned_to=10 | Net Metering Approval
  ph2 Approvals (Pre-Installation) #2     status=Not Started  role=PM       assigned_to=10 | CEIG Approval
  ph3 Procurement & Delivery   #1 MIRROR status=Not Started  role=SCM      assigned_to=None | Delivery — Solar Panels
  ph3 Procurement & Delivery   #2 MIRROR status=Not Started  role=SCM      assigned_to=None | Delivery — Inverters
  ph3 Procurement & Delivery   #3 MIRROR status=Not Started  role=SCM      assigned_to=None | Delivery — BOS Kit
  ph3 Procurement & Delivery   #4 MIRROR status=Not Started  role=SCM      assigned_to=None | Delivery — MMS
  ...
  ph7 Closeout                 #1 MIRROR status=Not Started  role=PM       assigned_to=None | COD
  ph7 Closeout                 #3 MIRROR status=Not Started  role=Design   assigned_to=None | As-Built Drawings
  ph7 Closeout                 #4 MIRROR status=Not Started  role=PM       assigned_to=None | HOTO

MIRROR ROWS ONLY:
  'Delivery — Solar Panels' status='Not Started' assigned_to=None due_date=None completed_at=None submitted_at=None approved_at=None
  'Delivery — Inverters' status='Not Started' assigned_to=None due_date=None completed_at=None submitted_at=None approved_at=None
  'Delivery — BOS Kit' status='Not Started' assigned_to=None due_date=None completed_at=None submitted_at=None approved_at=None
  'Delivery — MMS' status='Not Started' assigned_to=None due_date=None completed_at=None submitted_at=None approved_at=None

ROLLED BACK — nothing persisted.
OPEX tasks in DB after rollback: 0
```

### There is exactly one derivation door, and only Design goes through it

`apply_mirror_status()` is the only function that may write a mirror's status. Every
reference to it in non-test source:

```
projects/design_views.py:53:    # apply_mirror_status() below, never by a view in this module: the design workspace
projects/design_views.py:282:#   apply_mirror_status()         HOW a mirror's status is written at all
projects/design_views.py:455:def apply_mirror_status(task, new_status, actor, reason_code):
projects/design_views.py:514:            f"apply_mirror_status(): task {task.pk} ({task.task_name!r}) is not a "
projects/design_views.py:633:    return apply_mirror_status(
```

The single call site is inside `sync_design_mirror()` (design_views.py:563), which resolves
its task by `template_task__code='DESIGN'`. **No function anywhere resolves, reads or
writes the four delivery mirrors.** They have no source object and no writer.

### Proven by walking the delivery, not by reading the code

The full material path was run end to end on an activated OPEX site (details in Task 3) and
the mirrors were re-read after each step:

```
  POST create DC -> 302 /projects/TESTTENDER26-MB001/delivery-challans/14/
  DC created: DC-PILOT-1 | status: Expected | line items: 1

  after DC create — 'Delivery — Solar Panels' status='Not Started'
  after DC create — 'Delivery — Inverters' status='Not Started'
  after DC create — 'Delivery — BOS Kit' status='Not Started'
  after DC create — 'Delivery — MMS' status='Not Started'

  POST GRN -> 302 /projects/TESTTENDER26-MB001/delivery-challans/14/
  DC status after GRN: Received | line received_quantity: 40.00
  after GRN — 'Delivery — Solar Panels' status='Not Started'
  after GRN — 'Delivery — Inverters' status='Not Started'
  after GRN — 'Delivery — BOS Kit' status='Not Started'
  after GRN — 'Delivery — MMS' status='Not Started'
```

A challan was created, materials were fully received, the challan reached `Received` —
and all four mirrors stayed `Not Started`. **Confirmed, empirically, not inferred.**

---

## TASK 2 — BOQ acknowledgement

### There is no `action_code='boq_acknowledged'`

The string `boq_acknowledged` exists in this codebase as a **notification template name**,
not as an `ActivityLog.action_code`:

```
projects/views.py:5662:            template='boq_acknowledged',
projects/management/commands/test_whatsapp.py:53:    "boq_acknowledged": [...]
```

The acknowledgement's own log call passes no `action_code` at all
(`projects/views.py:5699-5703`):

```python
    log_activity(
        boq.project, profile,
        f"BOQ Acknowledged for project: {boq.project.project_id}",
        entity_type='BOQ', entity_id=boq.pk,
    )
```

Confirmed against every `action_code` value in the database — there is no BOQ
acknowledgement code, and 805 rows carry the empty default:

```
=== ActivityLog action_code distribution (top 25) ===
   '' 805
   'task_status_done' 509
   'user_login' 179
   ...
   'design_boq_submitted' 3
   'design_boq_item_updated' 2
```

If the pilot needs to *query* acknowledgements, that instrumentation does not exist yet.

### What the action is, and what it changes

The real action is `_apply_boq_acknowledgement()` (`projects/views.py:5672`), shared by two
callers. It writes:

* `BOQ.status = 'Acknowledged'`, in an atomic block with a `StatusTransition` ledger row;
* an `ActivityLog` row (no action_code, as above);
* a notification to the Design submitter, if `boq.submitted_by` is set.

The **inline** caller additionally saves, per BOQ line, the SCM-owned columns
`ordered_quantity`, `make_preference_id` and `ordered_vendor_id`, and writes a
`BOQRevision` snapshot. That is the substantive change: **acknowledgement is how SCM
records what it intends to order against each BOQ line.**

### Who can perform it — SCM, and only SCM

Both callers gate on the role independently. The standalone endpoint:

```python
    # DELIBERATELY role-only, with no project relationship requirement. SCM is portfolio-wide
    # by remit and acknowledges BOQs across every project; scoping this to a relationship
    # would break acknowledgement system-wide. Do not "harden" this to match the Design gates.
    if profile.role != 'SCM':
        return HttpResponseForbidden()
```

The inline branch: `elif action in ('save_scm', 'acknowledge_scm') and role == 'SCM' and boq.status in ('Submitted', 'Acknowledged')`.

### Reachable through a screen? Yes on Residential — **no on OPEX**

The only rendered control is on `boq_detail.html:317`, gated on `role == 'SCM'` **and**
`boq.status == 'Submitted'`:

```django
    {% if boq.status == 'Submitted' %}
    <button type="submit" form="saveSCMForm" name="action" value="acknowledge_scm"
            ...>Save &amp; Acknowledge</button>
    {% endif %}
```

`boq_acknowledge` (the standalone URL) appears in **no template** — it is raw-URL only.

`boq_detail`'s own docstring asserts *"SCM still acknowledges an OPEX BOQ here"*, and SCM
does reach the screen. **But an OPEX BOQ cannot reach `Submitted` through any screen**, so
the button never renders. Walked as SCM on a real OPEX site:

```
TASK 2 — SCM on OPEX BOQ screen: 200
  'Save & Acknowledge' button rendered: False
  BOQ status shown: Draft
  POST /boq/acknowledge/ -> 302 /projects/TESTTENDER26-MB010/boq/
  BOQ status after: Draft
```

The reason is a genuine gap, not a data accident. Only two code paths write
`BOQ.status = 'Submitted'` (views.py:5939 inline, views.py:7435 standalone), and both are
Design-authored. The one rendered control for either is `boq_detail.html:303`'s
**"Submit to SCM"**, in the `{% if role == 'Design' %}` branch — and Part 11 redirects the
OPEX author away from that screen before it renders:

```python
    # PART 11: send the OPEX author to the OPEX screen. Only the author — SCM, PM, Admin
    # and the two QC reviewers have no picker to use and read the sheet right here.
    if project.project_type == 'OPEX' and user_can_edit_project_boq(request.user, project):
        return redirect('opex_boq_entry', project_id=project_id)
```

Walked as the allocated designer on the same site:

```
  allocated designer: mahwar
  user_can_edit_project_boq(designer): True
  designer GET /boq/ -> 302 /projects/TESTTENDER26-MB010/boq/entry/
  designer GET /boq/entry/ -> 200 | 'Submit to SCM' on picker: False | 'submit_design' on picker: False
  design_locked: False | group_locked: False
```

The picker's completion action is `design_boq_complete`, which by its own docstring
*"records boq_submitted_at / boq_submitted_by — the design workflow's own note that this
step is done"* and does not touch `BOQ.status`. Worse, that stamp engages
`project_boq_is_design_locked()`, and `boq_submit` refuses a design-locked BOQ outright —
so even a hand-crafted POST is refused **after** the designer marks the BOQ complete.

Every OPEX BOQ in the database bears this out:

```
=== OPEX BOQs and their category mix ===
  TESTTENDER26-MB010 status=Draft items=37 qty>0=0  cats=['BOS','Inverter','Solar Modules','Structure']
  MB0141             status=Draft items=52 qty>0=52 cats=['AC Cable','ACDB','BOS','Cable Tray','Conduit','DC Cable','DCDB','Data Logger+ WMS','Earthing','Inverter','MMS','Module','Pin Type Lug','Ring Type Lug','Solar Meter + CT']
  MB0191             status=Draft items=53 qty>0=51 cats=[... same 15 ...]
  MB0164             status=Draft items=43 qty>0=43 cats=[... same 14 ...]
```

Four OPEX BOQs, three of them fully quantified, **all four stuck in `Draft`**.

**Conclusion.** BOQ acknowledgement is a real, screen-reachable SCM action **on
Residential**. On OPEX it is unreachable — and, as Task 7 argues, that may be correct
design rather than a defect, because the OPEX commit point is the site-group lock.

---

## TASK 3 — delivery challan creation

**Yes. This is a real SCM action, on a real screen, and it works on an OPEX site today.**

`create_delivery_challan` (`projects/views.py:10516`) is decorated `@role_required(['SCM'])`
— SCM alone creates a DC. It is reachable from two rendered controls on
`project_overview.html`, both inside `{% if role == 'SCM' %}`:

```django
  <a href="{% url 'create_delivery_challan' project.project_id %}" class="btn btn-brand btn-sm py-0">+ Add DC</a>
  ...
  <a href="{% url 'create_delivery_challan' project.project_id %}" class="btn btn-brand btn-sm">+ Add First DC</a>
```

and from a third on the SCM dashboard's per-project card (`dashboard/scm.html:363`,
`row.schedule_delivery_url` = `/projects/<pid>/delivery-challans/create/`), labelled
**"Schedule Delivery"**.

### The OPEX site reaches those screens

`dashboard_scm`'s per-project card list has **no project-type filter** — only a status one:

```python
    # Active projects SCM tracks: all Active/In Progress non-deleted projects
    active_projects = list(
        Project.objects.filter(is_deleted=False, status__in=['Active', 'In Progress'],
                               **_context_filter(ctx))
        ...
```

So a **Draft** OPEX site is invisible there, and an **activated** one appears with the full
button set. Confirmed by walking it as SCM on a freshly-activated OPEX site:

```
  SCM dashboard      /dashboard/scm/                                         -> 200
  BOQ detail         /projects/TESTTENDER26-MB001/boq/                       -> 200
  Project overview   /projects/TESTTENDER26-MB001/overview/                  -> 200
  DC create (GET)    /projects/TESTTENDER26-MB001/delivery-challans/create/  -> 200

  OPEX site id appears on SCM dashboard body: True
  'Schedule Delivery' button present: True
  'Raise Payment Request' button present: True
  'Tenders — grouped procurement' section present: True
```

### The flow, confirmed end to end

**material arrives → a DeliveryChallan exists for the SE to confirm GRN against:**

1. **PM or Coordinator activates the site.** `opex_site_activate` is gated on
   `_pm_owns_project()` and `status == 'Draft'`. **SCM cannot do this step** — the site
   must be Active before it appears on SCM's dashboard at all.
2. **SCM creates the DC, before or as material ships.** Header fields (vendor, PO number,
   DC number, DC date, expected delivery date, notes) plus at least one line item
   (category, description, quantity, unit). Created at `status=DeliveryChallan.EXPECTED`
   with a `StatusTransition` row (`REASON_CREATED`) and an `ActivityLog` entry.
3. **The DC is now visible to SE, PM and Admin** on `delivery_challan_detail` and counted
   as `pending_grn_count` on the SE dashboard.
4. **The SE confirms GRN** — `confirm_grn`, `@role_required(['Site Engineer'])` plus a
   project-scope gate (`user_can_view_project`, whose SE branch is "holds a task on this
   project"). Per line, they enter received quantity, damaged quantity and notes.
   `recalculate_dc_status()` then moves the DC to Received / Partially Received / Rejected.

Run for real:

```
  POST create DC -> 302 /projects/TESTTENDER26-MB001/delivery-challans/14/
  DC created: DC-PILOT-1 | status: Expected | line items: 1
  SE user: abhishek
  SE given task: Civil Work and MMS Installation
  POST GRN -> 302 /projects/TESTTENDER26-MB001/delivery-challans/14/
  DC status after GRN: Received | line received_quantity: 40.00
```

**One gate to know before the pilot:** the SE's first GRN attempt returned **404**, because
`confirm_grn` requires the SE to *hold a task on that project*. Activation leaves all nine
Site Engineer tasks unassigned. **The PM must assign the SE at least one task on the site
before the SE can confirm any GRN.** This is correct behaviour (it is the 0.2 lockdown that
made SE logins safe), but it is a step the pilot script has to contain or the walkthrough
will dead-end on a 404 with no explanation on screen.

---

## TASK 4 — payment requests

**Yes. SCM can raise a PaymentRequest against a real OPEX project through a real screen.**

`raise_payment_request` (`projects/views.py:7834`) — *"SCM raises a payment request against
a vendor invoice. SCM role only. POST."* — refuses any other role with a 403. The rendered
control is the **"Raise Payment Request"** button on every SCM dashboard project card
(`dashboard/scm.html:368`), opening the `#scmRaisePaymentModal` form.

The form requires vendor, **BOQ item**, invoice number, amount and a mandatory invoice
document. The BOQ-item dropdown is server-populated per project and scoped to that project
(`boq_item = get_object_or_404(BOQItem, pk=boq_item_id, boq__project=project)`).

**It populates correctly for an OPEX site**, with the real OPEX catalogue categories:

```
TASK 4 — payment-request BOQ dropdown for MB0141
  present: True
  categories: ['Module', 'DCDB', 'Inverter', 'MMS', 'ACDB', 'DC Cable', 'AC Cable',
               'Pin Type Lug', 'Ring Type Lug', 'Conduit', 'Cable Tray', 'Earthing',
               'Solar Meter + CT', 'Data Logger+ WMS', 'BOS']
  sample items: ['Solar PV Module', '10 In/ 10 Out DCBDB with DC SPD type-2, ',
                 '60 kW Grid -Tie Inverter @3P', '2P*4*1000mm height']
```

Both validation branches were exercised for real:

```
  (a) no invoice file -> 302 /dashboard/scm/
      messages: ['Invoice document is required.']
  (b) with invoice file -> 302 /dashboard/scm/
      messages: ['Invoice document is required.',
                 'Upload service unavailable. (SUPABASE_URL and SUPABASE_KEY must be configured)']
      PaymentRequest rows on site: 0
```

Reading (b) precisely: the request **passed every field validation**, resolved the vendor
and the OPEX BOQ item, and reached the Supabase upload — which is unconfigured in this
local environment. That last step is the **only** part not verified here, and it is proven
elsewhere: two `PaymentRequest` rows already exist on Residential projects, one confirmed:

```
=== PaymentRequest ===
rows: 2 [{'project__project_type': 'Residential', 'status': 'confirmed', 'n': 1},
         {'project__project_type': 'Residential', 'status': 'pending', 'n': 1}]
```

**Caveat for the pilot: the request is uneditable and uncancellable by design.** The model
docstring: *"A vendor payment request raised by SCM, confirmed by Finance, visible to PM.
No edit/cancel by design."* A mistyped amount on a pilot walkthrough is permanent.

---

## TASK 5 — SiteGroup membership and locking

**Usable through a real screen, fully. It is dormant only because no site has been
released.**

Screens and endpoints exist and are wired:

```
161:    path('programs/<int:pk>/site-groups/',        design_views.site_group_list,   name='site_group_list'),
162:    path('programs/<int:pk>/site-groups/create/', design_views.site_group_create, name='site_group_create'),
163:    path('site-groups/<int:pk>/',                 design_views.site_group_detail,      name='site_group_detail'),
164:    path('site-groups/<int:pk>/add-sites/',       design_views.site_group_add_sites,   name='site_group_add_sites'),
165:    path('site-groups/<int:pk>/remove-site/',     design_views.site_group_remove_site, name='site_group_remove_site'),
166:    path('site-groups/<int:pk>/lock/',            design_views.site_group_lock,        name='site_group_lock'),
```

Navigation is real: `dashboard/scm.html:100` includes `_scm_opex_groups.html`, whose tender
row carries a **"Groups"** button to `site_group_list`. Write authority is
`user_can_manage_site_groups` (SCM alone); read adds Admin and the Design Head.

The whole cycle was run as SCM, for real, rolled back:

```
PROGRAM: 2 MPUVNL
current SiteGroups: 0 | released assignments: 0

GET /programs/2/site-groups/ -> 200
  'Create group' form present: False
  pool rows rendered: 0

released: ['MS016', 'MB0238']
  site-groups screen -> 200 | both sites listed in pool: True
  POST create -> 302 /site-groups/39/
    messages: ['Group "Pilot batch 1" created. 2 site(s) added.']
  group: 39 Pilot batch 1 | status: draft | type: procurement | members: 2
  POST lock -> 302 /site-groups/39/
    messages: ['"Pilot batch 1" locked. The BOQ of 2 site(s) is now read-only.
                There is no unlock — a later change needs a variance against the order.']
  group status: locked | locked_by: subhash (SCM) | locked_at: 2026-09-05 13:04:43+00:00
  project_boq_is_group_locked(MS016): True

ROLLED BACK. SiteGroups now: 0
```

Create, add, lock, and the downstream BOQ freeze all work. Note the first two lines: with
nothing released, the screen renders **200 with no create form** — `site_groups.html:122`
puts the form inside `{% if pool %}`. That is why the feature *looks* unbuilt: it is
correctly hiding a form that would have nothing to act on.

### The single prerequisite

`_add_sites()` refuses any site that is not released:

```python
        assignment = getattr(project, 'design_assignment', None)
        if assignment is None or assignment.status != DESIGN_RELEASED:
            state = assignment.get_status_display() if assignment else 'design not started'
            refused.append(f'{project.project_id}: not released ({state}) — only released '
                           f'sites can be grouped for procurement.')
            continue
```

And nothing is released today:

```
=== DesignAssignment status distribution ===
[{'status': 'awaiting_allocation', 'n': 82}, {'status': 'in_design', 'n': 1},
 {'status': 'in_qc', 'n': 1}, {'status': 'arka_submitted', 'n': 1},
 {'status': 'artifacts_uploaded', 'n': 2}]

=== SiteGroups ===
  total: 0
```

Release happens at exactly one place — the **Design Head** passing a package that Design QC
has already passed (`design_views.py:3610`, `apply_design_status(assignment, DESIGN_RELEASED, ...)`),
which stamps `released_at` / `released_by` in the same write. **So SCM's group screen cannot
be walked in a pilot until one site clears both design gates.** That is a Design dependency,
not an SCM defect.

Two further constraints worth knowing before the walkthrough: **there is no unlock**
(deliberate — a post-order change is a variance, and variance handling does not exist), and
a lock is refused if the group is empty or any member has a change request awaiting the
Design Head.

---

## TASK 6 — B-18's actual blast radius

**B-18 is a later-phase blocker, not a pilot blocker.** It blocks **none** of tasks 2–5.

### What B-18 is

`DCLineItem` has no FK to `BOQItem`. Its full field list:

```
=== DCLineItem fields ===
['id', 'challan', 'boq_category', 'item_description', 'ordered_quantity', 'unit',
 'received_quantity', 'condition', 'damaged_quantity', 'grn_date', 'grn_confirmed_by', 'grn_notes']
```

A challan line names its material with a free-text `item_description` and a `boq_category`
CharField. Nothing joins it to the BOQ line it was ordered against.

### What it does *not* block

* **Task 2 (BOQ acknowledgement)** — reads and writes `BOQ`/`BOQItem` only. No DC involved.
* **Task 3 (DC creation and GRN)** — proven end to end above. A challan is created, received
  and closed with no BOQ join anywhere in the path.
* **Task 4 (payment requests)** — `PaymentRequest.boq_item` is a **real FK to `BOQItem`**.
  The payment path already has the join B-18 is about; it simply does not go via the DC.
  (`PaymentRequest`'s own comment records that a DC FK was explicitly declined: *"Zuber
  explicitly decided against a DC FK (19-June session) to keep the model simple; payment
  requests may not always map 1:1 to a DC."*)
* **Task 5 (site groups and locking)** — `SiteGroup` → `SiteGroupMembership` → `Project`.
  No DC in the model at all.

### What it does block

Exactly what `docs/delivery-state-authority.md` §4 and `docs/execution-model.md` §8 already
scope it to: **reconciliation against the site BOQ** — "40 of 48 modules received against
BOQ line 7". That is explicitly future SCM-workspace work (StockCheck, cross-tender
attribution, tender margin), and it is also the prerequisite for ever deriving the four
delivery mirrors. `DEPLOY_PLAN.md` states the dependency: *"the four deliveries need B-18
**and** SCM's catalogue mapping"*.

### One thing the existing record understates, and it is worse on OPEX

`delivery-state-authority.md` describes the mismatch as *"four category values against
`BOQItem`'s five, so anything filed under `Other` is unreconcilable"*. That is the
**Residential** picture. `DCLineItem.CATEGORY_CHOICES` is hardcoded to four values —
`Solar Modules`, `Structure`, `Inverter`, `BOS` — and was confirmed live on the DC create
form:

```
  DC create form category options: [('Solar Modules','Solar Modules'), ('Structure','Structure'),
                                    ('Inverter','Inverter'), ('BOS','BOS')]
```

But the **OPEX** catalogue uses sixteen categories, and `BOQItem.category` is an unenforced
CharField, so real OPEX rows carry them:

```
=== BOQItemMaster by project_type x category ===
  OPEX  AC Cable 39      OPEX  ACDB 20        OPEX  BOS 20             OPEX  Cable Tray 5
  OPEX  Civil 1          OPEX  Conduit 33     OPEX  Data Logger+ WMS 3 OPEX  DC Cable 3
  OPEX  DCDB 6           OPEX  Earthing 10    OPEX  Inverter 15        OPEX  MMS 16
  OPEX  Module 1         OPEX  Pin Type Lug 4 OPEX  Ring Type Lug 25   OPEX  Solar Meter + CT 6
  Residential  BOS 30    Residential  Inverter 3
  Residential  Solar Modules 2                Residential  Structure 2
```

Of the four categories a DC offers, only **`BOS`** and **`Inverter`** exist in the OPEX
catalogue at all. `Solar Modules` and `Structure` do not — OPEX calls them **`Module`** and
**`MMS`**. So on an OPEX site an SCM user recording a delivery of DC cable, earthing,
conduit or a data logger has **no category that fits**, and will file it under BOS.

**This does not block anything in the pilot** — the DC is still created, still confirmed,
still closed, and `item_description` still carries the truth in free text. But it makes the
DC's category field meaningless on OPEX, and it will make B-18 harder later, because the
category is the coarse key any string-matching reconciliation would start from.

**Verdict: B-18 is a later-phase blocker.** It should not gate the pilot, and the pilot
should not attempt anything that reads "received against BOQ".

---

## TASK 7 — proposed SCM pilot scope

> **PROPOSAL — NOT YET AGREED. Needs sign-off before the pilot plan is updated.**

### The shape of it

SCM's honest pilot story on one real site is **the material trail**, not the task list. It
has three verifiable acts, and each leaves a durable record someone else can see:

1. **SCM records what is coming** — creates a Delivery Challan against the site.
2. **The site engineer confirms what arrived** — GRN, per line, with damage.
3. **SCM raises the money against it** — a PaymentRequest, which Finance then confirms.

That is a real closed loop, it works today on an OPEX site, and it is worth walking.

### Proposed inclusions

| # | Step | Actor | Screen | Status |
|---|---|---|---|---|
| 1 | Activate the pilot OPEX site | PM / Coordinator | `project_overview` → Activate | Works. **Prerequisite, not SCM's step.** |
| 2 | Assign the SE at least one task on the site | PM | task assign | Works. **Required or step 5 404s.** |
| 3 | SCM opens the site from their dashboard | SCM | `dashboard/scm/` card | Works |
| 4 | SCM creates a Delivery Challan | SCM | "Schedule Delivery" / "+ Add DC" | Works |
| 5 | SE confirms GRN with a partial + a damaged line | SE | `delivery_challan_detail` | Works |
| 6 | SCM overrides / corrects a GRN line | SCM | `override_grn` | Present; **not exercised by this audit** |
| 7 | SCM raises a delivery issue against the DC | SCM | "Raise Issue" | Present; **not exercised by this audit** |
| 8 | SCM raises a PaymentRequest against a BOQ line | SCM | dashboard modal | Works (upload step verified only on Residential) |
| 9 | Finance confirms the payment | Finance | `dashboard_finance` | Present; **not exercised by this audit** |

### Proposed exclusions, with reasons

* **The four delivery mirrors.** Confirmed inert. They will sit at `Not Started` for the
  whole walkthrough while material demonstrably arrives. **Say this to the pilot users
  before they see it**, or the first thing they will report is that the task list is broken —
  and they will be right.
* **BOQ acknowledgement.** Unreachable on OPEX (Task 2). Do not script it.
* **Site groups and locking.** Real and working, but needs one released site (Task 5).
  **Include it only if Design can clear one site through both gates before the pilot;**
  otherwise defer, and do not present the empty screen as evidence of anything.
* **Anything reading "received against BOQ".** B-18. Out of scope by decision, not by
  accident.
* **StockCheck, cross-tender attribution, tender margin.** Explicitly the larger SCM
  workspace. Not in this pilot.

### Found broken or missing — the b1-b3-shaped shortlist

Ranked. The first two are the ones I would actually fix before the walkthrough.

**S-1 — DC categories are wrong for OPEX. (Small fix, real user-facing confusion.)**
Four hardcoded values, of which two exist in the OPEX catalogue; 14 OPEX categories have no
home. An SCM user recording DC cable will hunt for a category and give up. The narrow fix
is to source the DC category list from the site's `project_type` the way the BOQ picker
already does, rather than from a constant. **This is a genuine fix, not a workaround for
B-18** — it does not create a join, it just stops offering the wrong list. If it is not
fixed, the pilot script must tell SCM which category to pick and why it is wrong.

**S-2 — vendor master is empty. (Data, not code. Blocks a meaningful walkthrough.)**

```
active vendors: 2 / total 2
   horizon
   horizon renewable power
```

Both are test rows. A DC and a PaymentRequest both require a vendor, and the pilot record
will read "horizon supplied modules to horizon". **Load the real vendor list before the
pilot**, or the artefacts the walkthrough produces are unusable as a record.

**S-3 — the SE task-assignment step is invisible.** `confirm_grn` 404s for an SE with no
task on the site, with no on-screen explanation. Cheapest fix is the pilot script (step 2
above). A better one later is an explanatory refusal instead of a bare 404.

**S-4 — no `action_code` on BOQ acknowledgement, and none on the DC/GRN path either.**
Not a pilot blocker; flagged because "show me what SCM did last week" is a question the
pilot will provoke, and today it can only be answered by string-matching `action`, which
`ActivityLog.action_code`'s own comment forbids.

**S-5 — PaymentRequest is uneditable and uncancellable.** By design, but it means a
fat-fingered pilot amount is permanent. Either brief SCM, or accept a junk row in the
ledger.

**S-6 — stale comment, cosmetic.** `views.py:4244` says five mirrors; there are eight.

### The one thing I would say out loud in the pilot briefing

The site engineer's half of this loop is genuinely complete: they see pending GRNs, they
confirm quantities and damage, and the challan closes. **SCM's half is complete for material
and money, and empty for tasks.** The four Procurement & Delivery rows on the task list are
placeholders with no source, and they will not move no matter what SCM does. Naming that
before the walkthrough costs one sentence; discovering it mid-demo costs the pilot's
credibility.

---

## Appendix — what this audit did not verify

Stated so nobody reads more confidence into the above than it earned.

* **Production.** Everything here is the local dev database plus the working tree.
  Production runs `origin/main`, 22 commits behind.
* **The Supabase upload** in `raise_payment_request` (unconfigured locally). Inferred
  working from two existing Residential `PaymentRequest` rows.
* **`override_grn`, `create_delivery_issue`, and Finance's `confirm_payment_request`.**
  Present and routed; not exercised.
* **Notifications.** `_notify_boq_acknowledged` and the delivery templates were read, not
  fired.
* **Browser rendering.** All walkthroughs were server-side HTTP; no JavaScript ran. The
  payment-request modal's client-side vendor/BOQ population was verified by reading the
  server-rendered `boqByProject` payload, not by driving the modal.
