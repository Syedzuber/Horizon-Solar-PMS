# Prompt 1 (final) — Residential Gantt (compute-live), Internal/Client views

> Audit-grounded implementation spec for the Phase 1.5 Gantt feature.
> Derived from a read-only audit of the live Railway DB (2026-07-22).

## 0. Audit grounding — build to these, do not re-litigate
- **`Task` has no `start_date` field.** Only `Task.due_date` (`DateField`), `duration_days`
  (`PositiveIntegerField`, default 1), `task_order`, `task_type`, `phase` (FK→`ProjectPhase`→`Project`).
  (`projects/models.py:125`)
- **`Task.due_date` is ~99.6% null in production** (1097 of 1101 activated-Residential tasks). It is
  **NOT read** in v1. **`duration_days` is 100% populated; `Project.activated_at` is set on activated projects.**
- **Therefore the schedule is computed in-memory** from `activated_at` + `duration_days`, reusing the chain
  algorithm in `utils.calculate_due_dates` (`projects/utils.py:128`). No writes to task/schedule data.
- Dates are `DateField` (no timezone) — the UTC-cron concern does not apply.

## 1. Scope
**In:** Residential only; collapsible "Gantt" section in `project_overview`; Internal/Client toggle;
weekly-column grid; compute-live engine + pure buffer-cascade; **admin-configurable buffer + external-bar
floor**; **role-gated Client view**; unit tests.
**Out (explicit TODOs, do not build):** OPEX/CAPEX; PDF/image export; persisting due dates; per-phase
(non-uniform) buffers; frozen/re-baseline mechanism; client login.

## 2. Terminology
"**Client view**" = external / client-facing chart (buffered dates + friendly names). Used interchangeably
with "external view". "**Internal view**" = raw computed chain (actual internal task names, unbuffered).

## 3. Admin-configurable settings — requirement #1
> DEVIATION FROM ORIGINAL "no migration" LOCK: an admin-editable value must persist. This is a low-risk
> ADDITIVE migration on the single-row `SystemSettings` table — NOT on Task/schedule data, so the
> schedule-data no-migration guarantee still holds.

Add to `SystemSettings` (`projects/models.py:1061`), with a new migration:
```python
gantt_client_buffer_days        = models.PositiveIntegerField(default=3,
    help_text="Calendar days added to each phase end in the Client Gantt view, cascading downstream.")
gantt_external_min_display_days = models.PositiveIntegerField(default=3,
    help_text="Minimum visual bar width (days) for external/third-party tasks so they don't render too thin.")
```
Wire both into `admin_master_switches` (`projects/views.py:6677`, `@role_required(['Admin'])`): these are
**integer inputs, not checkboxes** — parse with a guard (`int(...)`, reject negatives, keep current value on
bad input), and emit a `log_activity` line on change matching the existing per-field pattern. Add two number
inputs to the master-switches template. `GANTT_PHASE_BUFFER_DAYS` (see §4) remains ONLY the migration default /
hard fallback; runtime always reads `SystemSettings.get().gantt_client_buffer_days`.

## 4. Constants — `projects/gantt_constants.py` (new file)
`GANTT_PHASE_BUFFER_DAYS = 3` (fallback only — runtime reads `SystemSettings`, see §3), plus the two
display maps below. Both are **temporary hardcodes — replace when Project Templates ships** (task/phase
names become editable there). Keyed on EXACT internal strings from `utils.build_residential_phases()`;
unmapped keys fall back to the internal name, never blank. Tone: third person.

```python
GANTT_PHASE_BUFFER_DAYS = 3  # fallback only; runtime reads SystemSettings.gantt_client_buffer_days

# Internal phase_name -> client-facing band label
GANTT_PHASE_DISPLAY_NAME_MAP = {
    'Sales & Documentation':       'Order & Documentation',
    'Detail Engineering Visit':    'Site Survey',
    'Design':                      'System Design',
    'Pre-Installation Approvals':  'Approvals & Permits',
    'Procurement':                 'Procurement',
    'Delivery':                    'Material Delivery',
    'Installation':                'Installation',
    'Commissioning':               'Commissioning & Handover',
    'Finance Closure':             'Project Closure',
}

# Internal task_name -> client-facing label.  # EXT = external/third-party (floored bar)  # ◇ = 0-duration milestone
GANTT_TASK_DISPLAY_NAME_MAP = {
    # Phase 1 — Sales & Documentation
    'OCR, Documentation & Verification':    'Order Confirmation & Documentation',
    'Send Invoice - Advance Payment':       'Advance Payment Invoice',
    'Advance Payment Confirmation':         'Advance Payment Received',

    # Phase 2 — Detail Engineering Visit
    'DEV Schedule':                         'Site Survey Scheduled',
    'DEV Conduct':                          'Site Survey Visit',
    'DEV Data to Design':                   'Site Data Handover to Design',
    'DEV Inputs Validation':                'Site Data Validation',

    # Phase 3 — Design
    'Design':                               'System Design',
    'Array Layout':                         'Solar Panel Layout',
    'SLD':                                  'Electrical Design (Single-Line Diagram)',
    'Installation Drawings':                'Installation Drawings',
    'BOQ Preparation':                      'Material & Equipment List',
    'Design Approval by Internal Team':     'Design Quality Review',
    'Design Approval by Customer':          'Design Sign-off (Customer)',            # EXT

    # Phase 4 — Pre-Installation Approvals
    'Pre Installation Approvals':           'Pre-Installation Approvals',
    'LC / PC / NC Required':                'Electricity Board Clearances',          # EXT  (verify acronym)
    'Vendor Registration':                  'Utility Vendor Registration',           # EXT
    'Document Preparation':                 'Application Document Preparation',
    'Signing Document by Customer':         'Document Signing (Customer)',           # EXT
    'Net Metering Application Submission':  'Net Metering Application',
    'TFR Received':                         'Technical Feasibility Approval (TFR)',  # EXT  (verify acronym)

    # Phase 5 — Procurement
    'Procurement Schedule':                 'Procurement Planning',
    'PO Placed MMS':                        'Mounting Structure Ordered',
    'PO Placed Module':                     'Solar Panels Ordered',
    'PO Placed Inverter':                   'Inverter Ordered',
    'PO for B & C Class Items':             'Balance of System Ordered',             # (verify: B & C class)
    'Send Invoice - Material Supply':       'Material Supply Invoice',
    'Pre Dispatch Payment Confirmation':    'Pre-Dispatch Payment Received',

    # Phase 6 — Delivery
    'Delivery Schedule':                    'Delivery Planning',
    'Delivery of MMS':                      'Mounting Structure Delivered',
    'Delivery of B & C Class Items':        'Balance of System Delivered',
    'Delivery of Module':                   'Solar Panels Delivered',
    'Delivery of Inverter':                 'Inverter Delivered',

    # Phase 7 — Installation
    'MMS Installation':                     'Mounting Structure Installation',
    'Earthing Work':                        'Earthing & Safety Work',
    'Module Installation':                  'Solar Panel Installation',
    'Inverter Installation':                'Inverter Installation',
    'DC Wire Work':                         'DC Wiring',
    'AC Cable Work':                        'AC Cabling',
    'Connections and Voc Testing':          'Electrical Connections & Testing',
    'Pre Commissioning Check List':         'Pre-Commissioning Inspection',          # ◇

    # Phase 8 — Commissioning
    'Pre Commissioning Visit by DISCOM':    'Utility Inspection Visit',              # EXT
    'Meter Testing':                        'Meter Testing',
    'SCO Release':                          'Utility Sanction (SCO)',                # EXT  (verify acronym)
    'Meter Installation by DISCOM':         'Net Meter Installation (Utility)',      # EXT
    'RMS Configuration':                    'Remote Monitoring Setup',
    'Plant Commissioning':                  'System Commissioning',
    'Commissioning Report Prepared':        'Commissioning Report',
    'Commissioning Report Approved':        'Commissioning Sign-off',                # ◇
    'Customer Handover':                    'Handover to Customer',                  # ◇
    'Send Invoice - Final Payment':         'Final Payment Invoice',

    # Phase 9 — Finance Closure
    '100% Payment Confirmation':            'Final Payment Received',
}
```
> ACRONYM ASSUMPTIONS TO VERIFY (inferred, may be wrong for this DISCOM/process): LC/PC/NC = electricity-board
> clearances; TFR = Technical Feasibility Report/approval; SCO = utility Sanction/Supply Connection Order;
> "B & C Class Items" = Balance of System (cables/connectors/minor items).

## 5. Date engine — `compute_gantt_schedule(project, buffer_days, external_min_days)` in `utils.py`
Pure, no writes; mirrors `calculate_due_dates`. Returns rows in `(phase__phase_order, task_order)` order:
`{task, phase_name, phase_order, task_order, start, end, is_external, is_marker, status, label_internal}`.
- Anchor `cursor = project.activated_at.date()`; if `activated_at is None` → return `[]`.
- **Internal, duration > 0:** `start=cursor`, `end=add_workdays(cursor, duration_days)`, `cursor=end`, `is_marker=False`.
- **Internal, duration == 0:** `start=end=cursor`, `is_marker=True` (genuine instantaneous milestone → diamond).
  Cursor unchanged. **[decision: milestone diamond, not floored bar]**
- **External:** `start=cursor`, `end=cursor + max(duration_days, external_min_days)`, `is_external=True`,
  `is_marker=False`, **cursor NOT advanced** (parallel / non-blocking). Gives a readable, real-data-based width
  floored at the admin value; bar may legitimately overlap the following internal bar (parallel dependency).
  **[decision: own duration floored at admin min]**
- **Buffer cascade:** after the last task of each phase, `cursor = add_workdays(cursor, buffer_days)`. With
  `buffer_days=0` the output is identical to the raw chain (the unit-test invariant).

## 6. View wiring — `project_overview` (`projects/views.py:4306`) — requirement #2
- Non-Residential (`project.project_type != 'Residential'`) → `gantt_available=False`; do NOT compute.
- Compute **internal** rows for **every** role that can already see the project:
  `compute_gantt_schedule(project, 0, ext_min)`.
- Compute **client** rows **only if** `request.user.profile.role in {'PM', 'Project Coordinator', 'CEO'}`:
  `compute_gantt_schedule(project, sys.gantt_client_buffer_days, ext_min)`, then map task labels via
  `GANTT_TASK_DISPLAY_NAME_MAP` and phase-band labels via `GANTT_PHASE_DISPLAY_NAME_MAP` (both fall back to the
  internal name on a miss). **Security:** client rows are placed in context ONLY for these roles — do not
  render-and-CSS-hide (a non-authorized user must not be able to view-source the buffered schedule). Pass
  `can_view_client=<bool>`.
- `ext_min = sys.gantt_external_min_display_days`. Weekly columns from `min(start) → max(end)` per grid
  (external ends can extend the range). `activated_at` null → `gantt_not_activated=True`.
- Existing project isolation (PM ownership, SE isolation) still applies — the chart only ever covers a project
  the user can already open.

## 7. Template — `projects/templates/projects/project_overview.html`
- New collapse block `ganttCollapse`, consistent with the existing `activityCollapse` / `boqCollapse` stack
  (`data-bs-toggle="collapse"`, `.block-toggle` chevron).
- **Internal/Client toggle renders only when `can_view_client`**; otherwise the internal grid shows alone with
  no toggle. Toggle is client-side show/hide of the two pre-rendered (server-role-filtered) grids — no HTMX.
- Bar styles: internal solid, colored by phase; **external = distinct hatched/outlined "third-party
  dependency" bar**; internal 0-duration = milestone diamond; null-date = "date TBD" row (never a broken bar).
- Phase band labels: Internal view uses raw `phase_name`; Client view uses the `GANTT_PHASE_DISPLAY_NAME_MAP`
  label (applied server-side in §6, not in the template).
- Client grid caption: **"Client view — buffered schedule, reflects current plan as of {{ today }}."**
- `{# TODO: task names/order + display-name map are hardcoded; re-check when Project Templates ships #}`.

## 8. Tests — `projects/tests/test_gantt.py` (new; use `SERVER_NAME='localhost'`)
1. **Divergence (sequential):** set `SystemSettings.gantt_client_buffer_days`; assert Client end at phase *k*
   exceeds Internal by `k × buffer` (cumulative cascade); Internal == raw chain.
2. **Overlap / external width:** external task → `is_external`, width `== max(duration_days, ext_min)`, cursor
   unaffected (next internal start unchanged), bar overlaps the following internal bar.
3. **Anchor:** first internal task `start == activated_at.date()`.
4. **Null anchor:** `activated_at=None` → engine returns `[]`; view renders the not-activated message, no crash.
5. **Non-Residential:** OPEX project → fallback; engine not called.
6. **Role gating:** PM / Project Coordinator / CEO → `can_view_client=True` and client rows present; Site
   Engineer / Finance / SCM / Design / BD → `can_view_client=False` and **no client rows in context** (assert
   absence, not just hidden).
7. **Buffer admin round-trip:** POST a new buffer to `admin_master_switches` → persisted + `log_activity`
   written; negative/garbage input rejected (value unchanged).
8. **Display-name fallback + desync guard:** unmapped `task_name` → returns internal name (never blank);
   assert every `GANTT_TASK_DISPLAY_NAME_MAP` key ∈ `get_residential_template_task_names()`, and every
   `GANTT_PHASE_DISPLAY_NAME_MAP` key ∈ the phase names from `build_residential_phases()`.

## 9. Pre-flight (read-only, post-build, against Railway)
Reuse the audit harness (env-var `DATABASE_URL` override, `.env` untouched): pick 2–3 activated Residential
projects — confirm internal bars are non-empty; as a non-PM role only the internal grid renders; as PM the
Client view diverges per phase and external bars show a readable floored width; a non-activated project shows
the message.

## Locked judgment calls
- **External bar width:** own `duration_days` floored at the admin minimum (default 3d), overlapping the next
  internal task. (Rejected: stretch-to-next-milestone — width would be a layout artifact.)
- **Internal 0-duration tasks:** milestone diamonds. (Rejected: floored bars — would imply a fake duration.)
