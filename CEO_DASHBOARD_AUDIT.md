# CEO Dashboard — Card Enrichment Audit

**Mode:** investigation only. No source file was modified, created, or deleted. No migration was
generated or applied. The only files written by this session are this report and one appended block
in `SECONDARY_FINDINGS.md`.

**Date:** 2026-08-15
**Repo:** `c:\SolarPMS\Horizon-Solar-PMS` (branch `main`)

## Data sources used

Two databases were queried, both read-only:

| Source | Shape | Used for |
|---|---|---|
| Local Postgres (`solarpms_local`, the active `DATABASE_URL`) | 8 active projects, 358 tasks, ₹42.5 L contract value | Query-count measurement (G19), template render check |
| Railway production (the commented-out `DATABASE_URL` in `.env`) | 28 active projects, 1310 tasks, ₹1.217 cr contract value | All data figures in sections C, D, E, F |

The production session was opened with `SET default_transaction_read_only = on` plus
`psycopg2.set_session(readonly=True)`, so no write was possible at the server level. Every statement
issued was a `SELECT`. The production figures reproduce the numbers in the brief exactly (1310 tasks,
26 active Residential projects, ₹1.21 cr contract value, ₹0 payment pending), confirming production is
the dataset the brief describes.

**Git status note.** At session start `git status` was not empty: five untracked files
(`SESSION_B_AUDIT.md`, `SESSION_C_AUDIT.md`, `SESSION_D_AUDIT.md`, `SESSION_E_AUDIT.md`,
`SESSION_T_TEST_UNBLOCK.md`) — all prior-session audit reports. **No tracked file was modified.** As
this session is read-only, work proceeded rather than hard-stopping; the condition is recorded here
so it is not mistaken for something this session caused.

---

# A. Design status source of truth

## A1. Which view and template render the CEO dashboard project cards

**View:** [projects/views.py:1826-1835](projects/views.py#L1826-L1835) — the entry point.

```python
@login_required
def dashboard_ceo(request):
    """CEO portfolio overview. Access: CEO role only. Renders in 3 DB queries via _get_ceo_dashboard_context."""
    # Display context (EPC Residential / Tenders). None => no filter => the exact
    # portfolio-wide behaviour this dashboard had before the context feature.
    selected_context = _read_context(request)
    ctx = _get_ceo_dashboard_context(selected_context)
    ctx['now'] = timezone.now()
    ctx['context_nav'] = _context_nav(request, selected_context)
    return render(request, 'dashboard/ceo.html', ctx)
```

**Context builder:** [projects/views.py:1537-1823](projects/views.py#L1537-L1823) —
`_get_ceo_dashboard_context()`.

**The card queryset itself:** [projects/views.py:1591-1623](projects/views.py#L1591-L1623).

```python
    projects_qs = (
        Project.objects
        .filter(is_deleted=False, status__in=active_statuses, **_context_filter(context))
        .annotate(
            has_blocked_task=Exists(blocked_subq),
            has_at_risk_task=Exists(at_risk_subq),
        )
        .order_by('target_commissioning_date', 'project_id')
    )

    # Classify each project in Python (no extra DB hits)
    # Precedence: Blocked > Delayed > At Risk > On Time (worst condition wins)
    proj_total = proj_on_time = proj_at_risk = proj_delayed = proj_blocked = 0
    project_cards = []
    for p in projects_qs:
        proj_total += 1
        is_delayed = bool(p.target_commissioning_date and p.target_commissioning_date < today)
        ...
        project_cards.append({'project': p, 'badge': badge, 'is_delayed': is_delayed})
```

**Template:** [projects/templates/dashboard/ceo.html](projects/templates/dashboard/ceo.html), card
loop at [lines 607-682](projects/templates/dashboard/ceo.html#L607-L682). URL route:
[projects/urls.py:26](projects/urls.py#L26).

### Do the two tabs share a template/queryset?

**Yes — one view, one template, one queryset.** `EPC Residential` and `Tenders` are not separate
pages. They are a query-string display filter (`?context=residential` / `?context=tenders`) that
injects one extra `filter()` kwarg. From
[projects/views.py:100-142](projects/views.py#L100-L142):

```python
CONTEXT_PROJECT_TYPES = {
    CONTEXT_RESIDENTIAL: ['Residential'],
    CONTEXT_TENDERS:     ['OPEX', 'CAPEX'],
}
...
def _context_filter(context, prefix=''):
    types = CONTEXT_PROJECT_TYPES.get(context)
    if not types:
        return {}
    return {f'{prefix}project_type__in': types}
```

**This is the single most important structural fact for the enrichment work.** Any per-card field
added must render sensibly for *both* a Residential project and an OPEX/CAPEX tender site, because
the same `{% for card in project_cards %}` loop at
[ceo.html:619](projects/templates/dashboard/ceo.html#L619) renders both. There is no per-type card
template today.

## A2. For a Residential project, what record indicates a designer has submitted the design?

**There is no such record.** No model field anywhere is set when a designer submits a Residential
design.

Three candidates were checked and all three are ruled out:

1. **`DesignAssignment`** ([models.py:2096-2242](projects/models.py#L2096-L2242)) — OPEX only.
   `project = models.OneToOneField(Project, ...)` is type-agnostic in the schema, but production
   holds **0 rows on any Residential project** (87 rows, all OPEX). The docstring states the scope
   explicitly at [models.py:2097](projects/models.py#L2097): *"One per OPEX site — the container for
   all design work on that site."*

2. **`DesignSubmission`** ([models.py:1567-1606](projects/models.py#L1567-L1606)) — a **dead model**.
   It has the right shape (`project`, `submitted_by`, `status` in Pending/Approved/Rejected,
   `submitted_at`, `reviewed_by`) but **no write path exists anywhere in the codebase**. The only
   references are two read sites — [views.py:8731](projects/views.py#L8731) (My Documents listing)
   and [views.py:8764](projects/views.py#L8764) (read-only detail view) — plus the migration. There
   is no `DesignSubmission.objects.create(...)` in the repo. Production row count: **0**.

3. **`BOQ.status`** ([models.py:874-899](projects/models.py#L874-L899)) — real and written, but it
   tracks the bill of quantities, not the design. See section B.

The only remaining signal is a Design-department **Task** marked `Done` — see A3.

## A3. How the "design task" is identified

**By title string match, on a name that is only defined in a Python list literal.** There is no FK,
no `milestone_key`, and no stable key of any kind.

`Task` has no identifier field. Complete field list, read from the model at
[models.py:316-375](projects/models.py#L316-L375):

```
id, phase, task_name, task_order, assigned_role, assigned_to, status, task_type,
duration_days, due_date, completed_at, blocked_since, is_payment_milestone, created_at
```

Tasks are created from a hardcoded list of dicts in `build_residential_phases()`,
[projects/utils.py:507-623](projects/utils.py#L507-L623). The Design phase is
[utils.py:535-547](projects/utils.py#L535-L547):

```python
            {
                'phase_name':  'Design',
                'phase_order': 3,
                'tasks': [
                    {'task_order': 1, 'task_name': 'Design',                            'assigned_role': Task.DESIGN, 'task_type': Task.INTERNAL},
                    {'task_order': 2, 'task_name': 'Array Layout',                      'assigned_role': Task.DESIGN, 'task_type': Task.INTERNAL},
                    {'task_order': 3, 'task_name': 'SLD',                               'assigned_role': Task.DESIGN, 'task_type': Task.INTERNAL},
                    {'task_order': 4, 'task_name': 'Installation Drawings',             'assigned_role': Task.DESIGN, 'task_type': Task.INTERNAL},
                    {'task_order': 5, 'task_name': 'BOQ Preparation',                  'assigned_role': Task.DESIGN, 'task_type': Task.INTERNAL},
                    {'task_order': 6, 'task_name': 'Design Approval by Internal Team', 'assigned_role': Task.PM,     'task_type': Task.INTERNAL},
                    {'task_order': 7, 'task_name': 'Design Approval by Customer',      'assigned_role': Task.PM,     'task_type': Task.EXTERNAL},
                ],
            },
```

That the codebase already relies on name matching — and that the practice is known to be fragile — is
visible at [views.py:3324-3330](projects/views.py#L3324-L3330), where the comment says so outright:

```python
        # Bidirectional sync: Finance confirmation tasks → PaymentMilestone Received.
        # Mapping by task name — names are fixed in the residential template.
        _FINANCE_TASK_TO_MILESTONE = {
            'Advance Payment Confirmation':      'M1',
            'Pre Dispatch Payment Confirmation': 'M2',
            '100% Payment Confirmation':         'M3',
        }
```

This exact pattern has **already broken once** in the reverse direction — see the Secondary Findings
note appended by this session.

Production shape (active projects, `assigned_role='Design'`), 26 Residential projects:

| task_name | rows | Done |
|---|---|---|
| `Design` | 26 | 12 |
| `Array Layout` | 26 | 12 |
| `SLD` | 26 | 12 |
| `Installation Drawings` | 26 | 12 |
| `BOQ Preparation` | 26 | 12 |
| `DEV Inputs Validation` | 26 | 12 |

Note that all six move together — 12 Done on each — so "which one is *the* design task" is currently
an unforced choice, not one the data settles.

## A4. Tender/Site equivalent

**Confirmed: `DesignAssignment.status`.** Field at
[models.py:2223-2226](projects/models.py#L2223-L2226); the vocabulary is module-level at
[models.py:1768-1826](projects/models.py#L1768-L1826):

```python
DESIGN_ASSIGNMENT_STATUS_CHOICES = [
    (DESIGN_AWAITING_SURVEY,     'Awaiting survey'),
    (DESIGN_AWAITING_ALLOCATION, 'Awaiting allocation'),
    (DESIGN_ALLOCATED,           'Allocated'),
    (DESIGN_DUE_DATE_PROPOSED,   'Due date proposed'),
    (DESIGN_IN_DESIGN,           'In design'),
    (DESIGN_ARKA_SUBMITTED,      'Arka submitted'),
    (DESIGN_AWAITING_HEAD_ARKA,  'Arka — awaiting Design Head'),
    (DESIGN_ARKA_REJECTED,       'Arka rejected'),
    (DESIGN_ARTIFACTS_UPLOADED,  'Artifacts uploaded'),
    (DESIGN_IN_QC,               'In QC'),
    (DESIGN_AWAITING_HEAD_QC,    'QC passed — awaiting Design Head'),
    (DESIGN_QC_FAILED,           'QC failed'),
    (DESIGN_RELEASED,            'Released'),
    (DESIGN_SURVEY_RETURNED,     'Design Hold — survey inadequate'),
]
```

### Which stages count as designer-submitted

The designer's hand-off is the **artifact upload**, so the designer-submitted set is everything at or
past that point:

- `artifacts_uploaded` — designer has uploaded; nobody has picked it up yet
- `in_qc` — Design QC is reviewing the submitted artifact
- `awaiting_head_qc` — QC passed, Design Head owes a verdict
- `qc_failed` — was submitted, came back; a new attempt is open
- `released` — terminal success

`arka_submitted`, `awaiting_head_arka` and `arka_rejected` are a **separate, earlier gate** (the Arka
capacity submission) and should not be conflated with design-artifact submission. The comment at
[models.py:1781-1785](projects/models.py#L1781-L1785) is explicit that these statuses exist precisely
so a dashboard can tell *who is holding the tender up*, which is the distinction that matters here.

Production distribution (87 `DesignAssignment` rows, all OPEX): `awaiting_allocation` 82,
`artifacts_uploaded` 2, `in_qc` 1, `in_design` 1, `arka_submitted` 1. Note only 1 OPEX project is
*active*, so at most one of these rows can ever appear on the CEO dashboard's Tenders tab today.

## A5. Is there a stable, rename-proof identifier for "the design task" on Residential?

**No.**

The only identifiers available are the `task_name` string, and the positional pair
(`phase.phase_order=3`, `task.task_order=1`). The string breaks on rename; the position breaks on
reorder or insertion. Neither is stable. `Task` carries no `milestone_key`, no template FK, and no
slug. `is_payment_milestone` ([models.py:374](projects/models.py#L374)) is the only semantic flag on
the model and it is Finance-specific.

---

# B. BOQ on Residential

## B6. Do Residential projects have BOQ records?

**Yes.** Model: `BOQ` ([models.py:874-899](projects/models.py#L874-L899)), one per project
(`OneToOneField`), with `BOQItem` children ([models.py:902-956](projects/models.py#L902-L956)).

```python
class BOQ(models.Model):
    """Bill of Quantities for a project. One BOQ per project (OneToOne)."""

    # Workflow: Draft → Submitted (by Design) → Acknowledged (by SCM) or Revision Requested (by PM)
    STATUS_CHOICES = [
        ('Draft',              'Draft'),
        ('Submitted',          'Submitted'),
        ('Acknowledged',       'Acknowledged'),
        ('Revision Requested', 'Revision Requested'),
    ]

    project      = models.OneToOneField(Project, on_delete=models.CASCADE, related_name='boq')
```

### Same model as the OPEX `OPX-nnn` catalogue?

**Same `BOQ`/`BOQItem` tables; one shared catalogue table scoped by a column.** The catalogue is
`BOQItemMaster` ([models.py:654-708](projects/models.py#L654-L708)), partitioned by `project_type`:

```python
    project_type = models.CharField(
        max_length=20,
        choices=Project.PROJECT_TYPE_CHOICES,
        default='Residential',
        db_index=True,
    )
```

Production counts: **207 OPEX rows (`OPX-nnn`) and 37 Residential rows (`ITM-nnn`)**, all active.

The two sides differ in *how a sheet is populated*, and this matters for a status card:

- **Residential** — auto-seeded. On first view by an authorised editor, the BOQ is created and
  **every** active Residential catalogue row is bulk-inserted:
  [views.py:4318-4344](projects/views.py#L4318-L4344).
- **OPEX** — starts empty and is picked from, via a dedicated picker screen; the author is redirected
  away from this view at [views.py:4309-4310](projects/views.py#L4309-L4310).

Consequence: on Residential, *"a BOQ exists"* is nearly meaningless (it appears the moment anyone with
edit rights loads the page, pre-filled with 37 blank-quantity rows). Only `BOQ.status` and the
presence of non-null `boq_quantity` values carry information.

Production BOQ rows: Residential 5 (3 `Submitted`, 1 `Revision Requested`, 1 `Acknowledged`), OPEX 4
(all `Draft`). **Only 2 BOQ rows exist across all 28 active projects** — so on the live dashboard a
BOQ-status card would read "—" for 26 of 28 cards.

## B7. Does Residential lack a BOQ concept?

No — it has one. This question does not apply.

---

# C. Payment reconciliation

## C8. Models holding per-project payment received and pending

Two, with different counterparties:

| Model | Direction | Received field | Pending derivation | Location |
|---|---|---|---|---|
| `PaymentMilestone` | **Client → Horizon** | `amount_received` | `amount - amount_received` | [models.py:996-1026](projects/models.py#L996-L1026) |
| `PaymentRequest` | **Horizon → Vendor** | (none; `status='confirmed'`) | `amount` where `status='pending'` | [models.py:1507-1564](projects/models.py#L1507-L1564) |

`PaymentMilestone` is the only per-project record of client money:

```python
class PaymentMilestone(models.Model):
    """M1/M2/M3 payment checkpoint for a project. Managed by Finance."""

    PENDING  = 'Pending'
    INVOICED = 'Invoiced'
    RECEIVED = 'Received'
    ...
    project              = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='milestones')
    milestone_name       = models.CharField(max_length=10, choices=NAME_CHOICES)
    milestone_description = models.CharField(max_length=100, default='')
    amount               = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)  # Expected invoice amount
    amount_received      = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)  # Actual amount received (may differ)
```

Note both money fields are `null=True` **with no default**.

## C9. The four header-card queries

Rendered at [ceo.html:79-118](projects/templates/dashboard/ceo.html#L79-L118). Computed at
[views.py:1744-1780](projects/views.py#L1744-L1780):

```python
    # -- QUERY 4: Finance summary (payment requests + contract value) --
    active_filter = {
        'project__is_deleted': False,
        'project__status__in': ['Active', 'In Progress'],
        **_context_filter(context, 'project__'),
    }
    fin_payment_requests_pending = PaymentRequest.objects.filter(
        status=PaymentRequest.PENDING, **active_filter
    ).count()
    fin_vendor_payments_outstanding = (
        PaymentRequest.objects.filter(
            status=PaymentRequest.PENDING, **active_filter
        ).aggregate(s=Sum('amount'))['s'] or 0
    )
    fin_client_contract_value = (
        Project.objects.filter(
            is_deleted=False, status__in=['Active', 'In Progress'],
            **_context_filter(context),
        ).aggregate(s=Sum('contract_value'))['s'] or 0
    )
    # Milestones Finance has invoiced (triggered by task Done) but not yet fully collected.
    # status='Invoiced' → all of amount is owed; status='Received' with partial → amount-amount_received.
    # Overpaid/exact rows excluded at DB level (amount_received__gte=amount).
    _DECIMAL_ZERO = Value(Decimal('0'), output_field=DecimalField())
    fin_client_payment_pending = (
        PaymentMilestone.objects.filter(
            project__is_deleted=False,
            project__status__in=['Active', 'In Progress'],
            status__in=['Invoiced', 'Received'],
            amount__isnull=False,
            **_context_filter(context, 'project__'),
        ).filter(
            Q(amount_received__isnull=True) | Q(amount_received__lt=F('amount'))
        ).aggregate(
            s=Sum(F('amount') - Coalesce(F('amount_received'), _DECIMAL_ZERO))
        )['s'] or 0
    )
```

**Card 4 has no "received" counterpart.** There is no `fin_client_payment_received` anywhere in the
context dict ([views.py:1807-1820](projects/views.py#L1807-L1820)). The proposed per-card
"Payment (received / pending)" field has no existing aggregate to reuse for the received half.

## C10. Read-only check on production data

Portfolio-wide (no context filter), 28 active projects:

| Card | Renders |
|---|---|
| Payment Requests Pending | **0** |
| Vendor Payments Outstanding | **₹0** |
| Total Client Contract Value | **₹1,21,70,785.54** |
| Payment Pending from Client | **₹0** — `rows_matched = 0` |

Milestone rows on those same 28 active projects — **87 rows total**:

| status | `amount` null | `amount_received` null | rows | Σ amount | Σ received |
|---|---|---|---|---|---|
| Pending | yes | yes | 63 | 0 | 0 |
| Received | yes | yes | 23 | 0 | 0 |
| Received | yes | **no** | 1 | 0 | ₹10,000 |

Across **all** projects (including the 98 Drafts), 117 milestone rows exist and only **8 have a
non-null `amount`** — none of which are on an active project. Total `amount_received` ever recorded
anywhere in production is ₹9,88,000, against ₹1.21 cr of active contract value.

Per-project detail, active projects (abridged; full run in the session probe):

```
HRP-RES-2026-012  cv=190000.00    Σamt=0  Σrecv=0      M1:Received,M2:Received,M3:Pending
HRP-RES-2026-024  cv=180000.00    Σamt=0  Σrecv=10000  M1:Received,M2:Pending,M3:Pending
HRP-RES-2026-037  cv=2606823.00   Σamt=0  Σrecv=0      M1:Pending,M2:Received,M3:Received
HRP-RES-2026-039  cv=245000.00    Σamt=0  Σrecv=0      M1:Received,M2:Received,M3:Received
HRP-CAP-2026-001  cv=NULL         Σamt=0  Σrecv=0      M1:Pending,M1:Pending,M2:Pending,M2:Pending,M3:Pending,M3:Pending
```

Note `HRP-RES-2026-039` has **all three milestones `Received` with zero money recorded**, and
`HRP-CAP-2026-001` carries **six** milestones (a duplicate set of M1/M2/M3).

## C11. Explanation of the discrepancy

**Cause: missing data, produced by a status-transition path that skips the step where the amount
would be entered. It is not a wrong filter and not a wrong model — and the money is definitively not
settled.**

The card query is correct for what it was written to measure. It fails on live data because of two
compounding facts:

**(1) `amount` is never populated at creation.** Milestones are created at project activation with
only a name and a description — [views.py:2229-2240](projects/views.py#L2229-L2240):

```python
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
                created_by=request.user.profile,
            )
```

No `amount`, no `due_date`. Both stay `NULL`. The **only** place `amount` can ever be set is a manual
Finance edit on the project-overview screen,
[views.py:6459-6471](projects/views.py#L6459-L6471):

```python
                    else:
                        milestone.milestone_description = request.POST.get('milestone_description', '').strip()
                        amount_str   = request.POST.get('amount', '').strip()
                        due_date_str = request.POST.get('due_date', '').strip()
                        try:
                            milestone.amount = Decimal(amount_str) if amount_str else None
                        except InvalidOperation:
                            milestone.amount = None
```

In production that edit has been performed 8 times in the system's entire history, never on an
active project.

**(2) The `Invoiced` state is bypassed entirely.** The card's filter is
`status__in=['Invoiced', 'Received']`. The path that *would* set `Invoiced` is
`milestone_invoice()`, [views.py:5842-5865](projects/views.py#L5842-L5865) — Finance-only, POST-only,
manual. But there is a second, automatic path that jumps **`Pending` → `Received` in one hop**, at
[views.py:3331-3348](projects/views.py#L3331-L3348):

```python
        if new_status == Task.DONE and task.task_name in _FINANCE_TASK_TO_MILESTONE:
            _ms_label  = _FINANCE_TASK_TO_MILESTONE[task.task_name]
            _ms_update = {'status': 'Received', 'received_date': date.today()}
            _ar_str    = request.POST.get('amount_received', '').strip()
            _vr_str    = request.POST.get('variance_reason', '').strip()
            if _ar_str:
                try:
                    _ms_update['amount_received'] = Decimal(_ar_str)
                except InvalidOperation:
                    pass
            if _vr_str:
                _ms_update['variance_reason'] = _vr_str
            try:
                _ms_updated = PaymentMilestone.objects.filter(
                    project=project,
                    milestone_name=_ms_label,
                    status__in=['Pending', 'Invoiced'],
                ).update(**_ms_update)
```

`amount_received` is taken from an **optional POST field**. When a PM or Coordinator marks
`Advance Payment Confirmation` as Done without filling that box, the milestone flips to `Received`
with `amount = NULL` and `amount_received = NULL`. That is exactly the 23-row bucket in the table
above.

**Putting the two together against the card's filter:**

- `status__in=['Invoiced','Received']` — 24 of 87 active rows qualify (0 `Invoiced`, 24 `Received`);
- `AND amount__isnull=False` — **eliminates all 24**, because no active milestone has an amount.

`rows_matched = 0` → `Sum` returns `None` → `or 0` → the card renders **₹0**.

**What ₹0 means today:** not "nothing is owed" but "no invoice amount has ever been entered." 24
milestones across the active portfolio are marked *Received* while carrying no money at all, and
₹1.21 cr of contract value has ₹10,000 of recorded receipts against it. The card is silently
reporting a data-entry gap as a settled balance.

*(Per instruction, this is reported and not fixed.)*

---

# D. Task count definitions

## D12. The exact querysets

### Header Tasks card

Rendered at [ceo.html:156-183](projects/templates/dashboard/ceo.html#L156-L183). Computed inside the
single `.aggregate()` at [views.py:1626-1640](projects/views.py#L1626-L1640):

```python
    task_agg = Task.objects.filter(
        phase__project__is_deleted=False,
        phase__project__status__in=active_statuses,
        **_context_filter(context, 'phase__project__'),
    ).aggregate(
        task_total     =Count('pk'),
        # Status summary (portfolio-wide)
        task_unassigned=Count('pk', filter=Q(assigned_to__isnull=True)),
        task_inprogress=Count('pk', filter=Q(status=Task.IN_PROGRESS)),
        task_completed =Count('pk', filter=Q(status=Task.DONE)),
        # Overdue = internal tasks only; external delays are not team overdue (SE has DISCOM tasks)
        task_overdue   =Count('pk', filter=Q(
            task_type=Task.INTERNAL, due_date__lt=today, due_date__isnull=False,
            status__in=[Task.NOT_STARTED, Task.IN_PROGRESS],
        )),
```

**These four are not a partition.** `task_unassigned` filters on an *assignment* predicate;
`task_inprogress` and `task_completed` filter on *status*; `task_overdue` filters on status **and**
`task_type` **and** `due_date`. A single task can be counted in `Unassigned` and `Overdue`
simultaneously, and an assigned Not-Started task is counted in none of them. The card presents four
rows under one `{{ task_total }}` badge, which invites exactly the arithmetic the brief attempted in
D13.

### Top Assignees card

Rendered at [ceo.html:426-450](projects/templates/dashboard/ceo.html#L426-L450). Computed at
[views.py:1782-1805](projects/views.py#L1782-L1805):

```python
    # -- QUERY 5: Top-5 assignee leaderboard (open tasks per user) --
    # Open = Not Started / In Progress / Blocked (Blocked counts as still on the plate).
    # select_related pulls the profile→user names in the same query — no per-row round-trip.
    top_assignees = list(
        Task.objects.filter(
            phase__project__is_deleted=False,
            phase__project__status__in=active_statuses,
            assigned_to__isnull=False,
            status__in=[Task.NOT_STARTED, Task.IN_PROGRESS, Task.BLOCKED],
            **_context_filter(context, 'phase__project__'),
        )
        .values(
            'assigned_to',
            'assigned_to__user__first_name',
            'assigned_to__user__last_name',
            'assigned_to__user__username',
        )
        .annotate(count=Count('pk'))
        .order_by('-count')[:5]
    )
```

Production result (top 15 shown; the card truncates to 5):

```
siddharth  222     chetan      20     girijesh    2
subhash    129     priyanka     6     pradeep     2
santosh     71     neelam       4     pradyumna   2
shyam       64     awdhesh      4     aman        2
dipesh      58     aabid        4     deepti      1
```

Two things matter for the proposed merged "Top People" card: the distribution is extremely
top-heavy (top 2 hold 351 of ~594 open assigned tasks), and this card measures **open workload**,
which is a different axis from both "Completed" and "Usage". They cannot share a queryset.

## D13. Where the missing ~593 tasks are

**They are `Not Started` **and assigned**.** Nothing is missing — the four card numbers overlap and
under-cover, so they were never meant to sum to the total.

Production, dashboard scope (28 active projects), **1310 tasks**:

| status | count |
|---|---|
| `Not Started` | **843** |
| `Done` | **467** |
| `In Progress` | 0 |
| `Blocked` | 0 |

Cross-tabulated by assignment:

| status | assigned? | count |
|---|---|---|
| `Not Started` | assigned | **593** |
| `Done` | assigned | 467 |
| `Not Started` | unassigned | 250 |

The arithmetic: `250 (Unassigned) + 0 (In Progress) + 467 (Completed) = 717`, and
`1310 − 717 = 593` = *assigned, Not Started*. The card has no row for that state, which is the
single largest bucket in the portfolio.

Status values in use across the **whole** database (any project state), for completeness:
`Not Started` 1361, `Done` 492, `In Progress` 5, `Blocked` 3. The `In Progress` and `Blocked` states
exist and are written, but every instance sits on a Draft project.

## D14. What "Next task" can honestly mean

### The blocking fact: no open task in production has a due date

Dashboard scope, 1310 tasks:

| | count |
|---|---|
| `due_date IS NULL` | **1193** |
| `due_date IS NOT NULL` | 117 |
| open (not Done) **and** past due | **0** |
| open (not Done) **and** future due | **0** |

All 117 due-dated tasks are `Done`. **Zero open tasks carry a due date.** This is why the header
`Overdue` card reads 0 portfolio-wide, and it means the "+ due date" half of the proposed field
cannot be read from `Task.due_date` for a single project on the live system.

### Proposed ordering rule

> **Next task** = the first task in `(phase.phase_order, task.task_order)` order whose status is not
> `Done`; its due date is the `end` value that `compute_gantt_schedule(project)` produces for that
> task.

Ordering by template position rather than by date is the only rule that returns a non-null answer for
all 28 active projects. It also matches how the sequence is actually defined —
`ProjectPhase.Meta.ordering = ['phase_order']` ([models.py:309-310](projects/models.py#L309-L310))
and `Task.Meta.ordering = ['task_order']` ([models.py:377-378](projects/models.py#L377-L378)).

For the date, the engine already exists:
[projects/utils.py:258-341](projects/utils.py#L258-L341), `compute_gantt_schedule()`, whose docstring
at [utils.py:262-268](projects/utils.py#L262-L268) describes exactly this hybrid and confirms the
null problem is already known:

```python
    HYBRID date source (stored-or-computed):
      - A task's END is its Task.due_date when that is set, otherwise the computed
        chain end (previous end + duration_days). So a PM's due-date edits (and the
        cascade in recalculate_from_task) move the bars, while the ~all-null live
        projects still render via the computed chain and are never blank.
```

### Failure modes

1. **Projects whose targets are already in the past** — the stated concern. `compute_gantt_schedule`
   anchors the chain at `project.activated_at` ([utils.py:299](projects/utils.py#L299)) and walks
   forward by `duration_days`. It has **no notion of "today"**: on a project activated months ago
   and since stalled, the computed date for the next open task will be in the past, often far in the
   past. The card will show a Next task with a due date that already elapsed, for a task nobody has
   started. Displayed without qualification, every stalled project reads as catastrophically
   overdue, and the number is an artifact of the template durations rather than a commitment anyone
   made. This must be labelled as a projection, not a due date — or suppressed when it falls before
   today.

2. **Not-activated projects** — `compute_gantt_schedule` returns `[]` when `activated_at is None`
   ([utils.py:288-289](projects/utils.py#L288-L289)). The card needs an explicit empty state.

3. **Position ≠ readiness** — the first non-Done task by position may be an `External` task blocked
   on DISCOM while internal work proceeds in a later phase. "Next" will name the stalled external
   task, not what the team is actually doing.

4. **Cost** — see G. `compute_gantt_schedule` issues one query per project; calling it inside the
   card loop turns a flat 12-query page into 12 + N.

---

# E. Activity / usage metric

## E15. The model written by `log_activity()`

`ActivityLog`, [models.py:1116-1138](projects/models.py#L1116-L1138). Writer at
[models.py:1399-1421](projects/models.py#L1399-L1421).

```python
class ActivityLog(models.Model):
    """Append-only audit log. Written by log_activity(); never edited after creation."""

    project     = models.ForeignKey(
        Project, on_delete=models.CASCADE, null=True, blank=True, related_name='activity_logs',
    )
    actor       = models.ForeignKey(
        'UserProfile', on_delete=models.SET_NULL, null=True, blank=True, related_name='activity_logs',
    )
    action      = models.CharField(max_length=255)   # Human-readable description of what happened
    # Stable machine-readable event key for querying (e.g. 'task_assigned', 'issue_resolved').
    # Blank on existing rows and on the ~80 call sites not yet retrofitted — populated only where
    # this feature logs, or where the EOD digest queries. Never string-match `action`; filter this.
    action_code = models.CharField(max_length=50, blank=True, db_index=True, default='')
    entity_type = models.CharField(max_length=50, blank=True, default='')   # e.g. 'Task', 'Issue', 'BOQ', 'File'
    entity_id   = models.PositiveIntegerField(null=True, blank=True)         # PK of the affected object
    timestamp   = models.DateTimeField(auto_now_add=True)
```

Requested fields:

| Asked | Field | Notes |
|---|---|---|
| user FK | `actor` → `UserProfile` | nullable, `SET_NULL`; **not** `auth.User` |
| timestamp | `timestamp` | `auto_now_add`, indexed via `Meta.ordering = ['-timestamp']` |
| action code | `action_code` | `db_index=True`, **blank on most rows** — see below |
| project FK | `project` | **nullable** — login/logout rows have `project=None` |

**Coverage caveat:** production has 1622 `ActivityLog` rows and **697 (43%) carry `action_code=''`**.
The comment at [models.py:1126-1128](projects/models.py#L1126-L1128) confirms this is by design — only
retrofitted call sites populate it. Any usage metric filtered on `action_code` silently drops 43% of
recorded activity; any metric that counts all rows is a mix of user actions and auth noise.

## E16. Distinct-user action counts, last 30 days, descending

Production, `timestamp >= now() - interval '30 days'`. **Full list, 18 users.**

**Including login/logout:**

| # | user | role | actions |
|---|---|---|---|
| 1 | sudhir | Project Coordinator | **734** |
| 2 | admin | Admin | 172 |
| 3 | praveen | Design | 137 |
| 4 | nirankar | PM | 91 |
| 5 | siddharth | PM | 33 |
| 6 | saurav | CEO | 28 |
| 7 | nitesh | PM | 23 |
| 8 | anilgupta | Design | 16 |
| 9 | mahwar | Design | 13 |
| 10 | mehtab | PM | 11 |
| 11 | santosh | Finance | 10 |
| 12 | praveenkumar | Design | 10 |
| 13 | suvajit | Design | 7 |
| 14 | chetan | PM | 5 |
| 15 | shyam | Design | 4 |
| 16 | subhash | SCM | 4 |
| 17 | neelam | BD | 2 |
| 18 | natasha | CEO | 1 |

**Excluding `user_login` / `user_logout` (15 users — 3 users did nothing but log in):**

| # | user | role | actions |
|---|---|---|---|
| 1 | sudhir | Project Coordinator | **725** |
| 2 | admin | Admin | 144 |
| 3 | praveen | Design | 118 |
| 4 | nirankar | PM | 88 |
| 5 | nitesh | PM | 19 |
| 6 | saurav | CEO | 16 |
| 7 | siddharth | PM | 14 |
| 8 | mahwar | Design | 9 |
| 9 | praveenkumar | Design | 7 |
| 10 | mehtab | PM | 7 |
| 11 | santosh | Finance | 6 |
| 12 | anilgupta | Design | 5 |
| 13 | suvajit | Design | 4 |
| 14 | shyam | Design | 4 |
| 15 | chetan | PM | 1 |

**Shape of the distribution — this is the point of asking for the full list.** One user accounts for
**60% of all logged activity** (725 of 1207 non-auth actions). The top 3 hold 82%. Eleven of the 15
active users are in single or low-double digits, and 46 of the 64 user accounts produced **zero**
activity in 30 days. A "Usage" leaderboard on this data will show one bar and fourteen slivers; the
ranking below position 4 is noise and will reorder on a handful of clicks.

The concentration is partly an artifact of event granularity, not effort — see the `action_code`
distribution:

| action_code | count | | action_code | count |
|---|---|---|---|---|
| `task_status_done` | 461 | | `design_allocated` | 5 |
| *(blank)* | 380 | | `design_arka_submitted` | 5 |
| `opex_site_created` | 97 | | `task_status_in_progress` | 4 |
| `design_survey_link_added` | 86 | | `pm_assigned` | 4 |
| `user_login` | 74 | | `program_created` | 3 |
| `user_logout` | 60 | | `design_arka_qc_approved` | 3 |
| `task_assigned` | 58 | | `design_arka_head_approved` | 3 |
| `design_bulk_assigned` | 27 | | `design_artifacts_uploaded` | 3 |
| `task_reassigned` | 12 | | `design_boq_submitted` | 3 |
| `design_artifact_uploaded` | 6 | | *(15 more, ≤2 each)* | |

`task_status_done` (461) and `opex_site_created` (97) are bulk-ish operations; a user who closes 50
tasks in one sitting outranks a user who ran a difficult design review. Counting rows measures
clicks, not contribution.

## E17. Are login events captured separately from actions?

**Yes, in two places — but not in a dedicated model.**

**(1) In `ActivityLog` itself,** written by signal receivers in
[projects/signals.py:28-58](projects/signals.py#L28-L58):

```python
@receiver(user_logged_in)
def log_user_login(sender, request, user, **kwargs):
    try:
        profile = user.profile  # related_name='profile' on UserProfile.user
        ip = _get_ip(request)
        ActivityLog.objects.create(
            project=None,
            actor=profile,
            action=f"User logged in from {ip}",
            action_code='user_login',   # machine-readable key so digests can exclude auth noise
            entity_type='User',
            entity_id=user.id,
        )
    except Exception:
        pass
```

A matching `log_user_logout` receiver writes `action_code='user_logout'`. These are **rows in the
same table**, separable only by `action_code`, and the client IP is embedded in the free-text
`action` string rather than stored in a column. Production: 74 logins, 60 logouts in 30 days.

**(2) `auth_user.last_login`** — Django's built-in, giving most-recent login only, no history.
Production: **23 of 64 users have ever logged in.** It is already surfaced in two admin templates
([projects/templates/projects/admin/departments.html:103](projects/templates/projects/admin/departments.html#L103),
[projects/templates/projects/subadmin/departments.html:118](projects/templates/projects/subadmin/departments.html#L118)).

There is **no dedicated login/session model.** Note also that `log_user_login` swallows all
exceptions silently (`except Exception: pass`), so login capture is best-effort, not guaranteed.

---

# F. Material delivery

## F18. Does a model exist for stock receipt, material issue, or delivery status?

**Yes, for stock receipt and delivery status. No, for material issue.**

Two models, [models.py:1175-1275](projects/models.py#L1175-L1275):

**`DeliveryChallan`** — inbound delivery from a vendor, with project FK and aggregate status:

```python
class DeliveryChallan(models.Model):
    """Delivery Challan raised by SCM for incoming materials."""

    # Status reflects aggregate receipt state across all line items
    EXPECTED           = 'Expected'
    PARTIALLY_RECEIVED = 'Partially Received'
    RECEIVED           = 'Received'
    REJECTED           = 'Rejected'
    ...
    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name='delivery_challans'
    )
    ...
    expected_delivery_date = models.DateField(null=True, blank=True)
```

**`DCLineItem`** — the GRN (goods receipt note) layer:
`ordered_quantity`, `received_quantity`, `condition` (Good/Damaged/Partial), `damaged_quantity`,
`grn_date`, `grn_confirmed_by`.

**No model exists for material issue / consumption from stock.** PO creation is also explicitly out
of scope — [models.py:1198-1199](projects/models.py#L1198-L1199):

```python
    # po_number is a free-text reference to external PO (Excel/Zoho Inventory)
    # PO creation inside SolarPMS is Phase 2 scope — do NOT add PO model today
```

**Production usage is near-zero:** 6 `DeliveryChallan` rows in the entire database (4 `Expected`,
1 `Partially Received`, 1 `Received`). Across all 28 active projects: **1 challan, 3 line items,
1 project**. A Material Delivery status card would render an empty state on 27 of 28 cards.

*(One search, as instructed. No model designed.)*

---

# G. Query cost

## G19. Query count for a single CEO dashboard page load

**Measured: 12 queries per page load, and the count does not vary with the number of projects.**

Measurement used `django.test.utils.CaptureQueriesContext` around a real authenticated `GET` through
the Django test `Client`, against the **local** database (8 active projects). The production database
was **not** used for this measurement: `Client.force_login()` writes a `django_session` row, and this
session does not write to production.

| URL | status | queries |
|---|---|---|
| `/dashboard/ceo/` | 200 | **12** |
| `/dashboard/ceo/?context=residential` | 200 | **12** |
| `/dashboard/ceo/?context=tenders` | 200 | **12** |

The captured SQL, in order:

| # | query | source |
|---|---|---|
| 1 | `django_session` lookup | middleware |
| 2 | `auth_user` lookup | auth middleware |
| 3 | `projects_project` — the card list, with the two `Exists` annotations | [views.py:1591](projects/views.py#L1591) |
| 4 | `projects_task` — the ~40-`FILTER` aggregate | [views.py:1626](projects/views.py#L1626) |
| 5 | `projects_issue` — the issue aggregate | [views.py:1712](projects/views.py#L1712) |
| 6 | `COUNT(*)` `projects_paymentrequest` | [views.py:1750](projects/views.py#L1750) |
| 7 | `SUM(amount)` `projects_paymentrequest` | [views.py:1753](projects/views.py#L1753) |
| 8 | `SUM(contract_value)` `projects_project` | [views.py:1758](projects/views.py#L1758) |
| 9 | `SUM(amount - COALESCE(amount_received,0))` `projects_paymentmilestone` | [views.py:1768](projects/views.py#L1768) |
| 10 | top-5 assignee `GROUP BY` | [views.py:1785](projects/views.py#L1785) |
| 11 | `projects_userprofile` | base template / context processor |
| 12 | unread `projects_notification` count | base template / context processor |

**8 of the 12 belong to the dashboard; 4 are page chrome.** No statement in the trace is
project-scoped, so the count is **O(1) in project count** — 26 active projects produce the same 12
queries as 8. This is confirmed structurally: the only per-project work is the Python `for` loop at
[views.py:1605-1623](projects/views.py#L1605-L1623), which touches nothing but fields already loaded
by query 3.

**Correction to the code's own documentation:** both the function docstring
([views.py:1539](projects/views.py#L1539), *"in exactly 3 DB queries"*) and the view docstring
([views.py:1828](projects/views.py#L1828), *"Renders in 3 DB queries"*) are out of date. The measured
figure is **8**. The numbered `QUERY 4` and `QUERY 5` comment blocks at
[views.py:1744](projects/views.py#L1744) and [views.py:1782](projects/views.py#L1782) were added after
the docstring and never propagated to it. Logged in Secondary Findings.

## G20. How the project card list is built

**Via `annotate()` only — no `select_related()`, no `prefetch_related()`, and no per-object attribute
access that triggers a query.**

Two `Exists` subqueries are folded into the list query at
[views.py:1579-1599](projects/views.py#L1579-L1599):

```python
    # Subquery: does this project have any task with status='Blocked'?
    blocked_subq = Task.objects.filter(
        phase__project=OuterRef('pk'),
        status=Task.BLOCKED,
    )
    # Subquery: does this project have any overdue internal task (still future target date handled in Python)?
    at_risk_subq = Task.objects.filter(
        phase__project=OuterRef('pk'),
        task_type=Task.INTERNAL,
        due_date__lt=today,
        due_date__isnull=False,
        status__in=[Task.NOT_STARTED, Task.IN_PROGRESS],
    )
    projects_qs = (
        Project.objects
        .filter(...)
        .annotate(
            has_blocked_task=Exists(blocked_subq),
            has_at_risk_task=Exists(at_risk_subq),
        )
```

The template reads **only local columns** of `Project` —
[ceo.html:628-674](projects/templates/dashboard/ceo.html#L628-L674) touches `p.project_id`,
`p.customer_name`, `p.city`, `p.project_type`, `p.capacity_kw`, `p.target_commissioning_date`,
`p.activated_at`. No FK is traversed in the card (not even `assigned_pm`), which is why there is no
N+1 today.

**Implication for the enrichment work.** Every proposed per-card field crosses a relation the card
does not currently touch — `boq`, `milestones`, `phases__tasks`, `design_assignment`,
`delivery_challans`. Added naively as `{{ p.boq.status }}` in the template, each field costs **one
query per card**: at 26 cards, seven new fields would take the page from 12 queries to roughly 12 +
(26 × 7) ≈ **194**. The existing code is written in a deliberate style that avoids this — conditional
`Count`/`Sum` inside one `.aggregate()`, `Exists` for booleans, `.values()` + `.annotate()` for
leaderboards — and any new field should follow it (subquery-annotation or a single grouped query
joined in Python), not per-object access.

---

# Blockers

Proposed card fields that **cannot be built today**, each with the reason:

1. **Design status (Residential)** — no record exists that a designer submitted anything. `DesignAssignment` is OPEX-only (0 Residential rows in production) and `DesignSubmission` has no write path anywhere in the codebase. *(A2)*

2. **Design status — stable identification of "the design task"** — even falling back to task completion, `Task` has no `milestone_key`, template FK, or slug; the only handle is the literal string `'Design'` from a Python list, which any template rename or reorder breaks silently. Six Design-role tasks currently move together, so which one counts is an unforced choice. *(A3, A5)*

3. **Payment received** — no aggregate exists for client money received, and the underlying data is absent: 24 of 87 active milestones are marked `Received` with `amount_received = NULL`, and only ₹10,000 is recorded across ₹1.21 cr of active contract value. *(C8, C10, C11)*

4. **Payment pending** — the existing card renders ₹0 not because nothing is owed but because `amount` is `NULL` on every active milestone and the `Pending → Received` auto-transition skips `Invoiced` entirely. Any per-card version reproduces the same ₹0 per project. Needs the data-entry gap closed first, not a query change. *(C11)*

5. **Next task — due date** — zero open tasks in production carry a `due_date` (1193 of 1310 are `NULL`; all 117 that are set are already `Done`). Any date shown must be computed by `compute_gantt_schedule()`, which is anchored to `activated_at` and has no notion of "today", so it will render past dates as due dates on every stalled project. *(D14)*

6. **Next task — at current query cost** — `compute_gantt_schedule()` issues one query per project; used in the card loop it makes the page O(N). Buildable only after the schedule is either batched or computed without a per-project query. *(D14 §4, G20)*

7. **Material Delivery status** — the models exist (`DeliveryChallan` / `DCLineItem`) but production holds **1 challan across all 28 active projects**. The field is technically buildable and practically empty; 27 of 28 cards would show no data. *(F18)*

8. **BOQ status (Residential)** — `BOQ` exists but only **2 rows exist across all 28 active projects**, and on Residential a BOQ row is auto-created with 37 blank rows the first time an authorised user opens the page, so "has a BOQ" carries no signal. Only `BOQ.status` is meaningful, and it is absent for 26 of 28 cards. *(B6)*

9. **"Top People — Usage" view** — countable, but not honestly rankable. One user holds 60% of 30-day activity and the top 3 hold 82%; 43% of all `ActivityLog` rows carry a blank `action_code`; and `task_status_done` (461 of 1207 events) makes bulk task-closing dominate the count. The metric measures clicks, not contribution. *(E15, E16)*

Fields with **no blocker** — buildable today against existing data:

- **Pending task count** and **Completed task count** per project — one `.annotate()` with conditional `Count` on the existing card queryset, no new query. *(D12, G20)*
- **Next task — name only** (without a date) — first non-`Done` task by `(phase_order, task_order)`; needs one grouped subquery, not a per-card lookup. *(D14)*
- **Design status (Tenders tab only)** — `DesignAssignment.status` is real and populated, though only 1 OPEX project is active. *(A4)*
- **"Top People — Open"** — already built; it is the existing Top Assignees card. *(D12)*
- **"Top People — Completed"** — same query shape as Open with `status='Done'`; 467 completed tasks in scope. *(D12, D13)*

---

## Verification

- **Files changed by this session:** `CEO_DASHBOARD_AUDIT.md` (this report, new) and `SECONDARY_FINDINGS.md` (one appended section). No source file, template, or migration was touched.
- **`git status` at completion:** clean apart from these two files and the five pre-existing untracked session reports listed at the top. Confirmed below.
- **No `makemigrations` or `migrate` was run.** No write was issued to either database.
