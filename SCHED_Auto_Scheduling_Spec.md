# SCHED — Auto-scheduling by capacity (RESCO v1)

Status: decisions locked 8 Nov 2026. Builds in three sessions: SCHED-1, SCHED-2, SCHED-3. Manual browser verification is required between sessions.
Source facts: `SCHED_A_FINDINGS.md` (audit, 5 Nov DB restore).

---

## Layer 1 — Plain English

When a PM sets the due date of a RESCO site's Civil Work and MMS Installation task, the system fills in the due dates of its execution tasks automatically. The dates come from the site's DC capacity, using the PMs' duration table. The schedule runs from the task after Civil through Testing; the Civil date itself always belongs to the PM.

Every due date remembers whether a person or the system set it. The system only ever rewrites dates it set itself. Once a PM edits a date by hand, that date belongs to the PM permanently.

Already-activated sites get dates as soon as their Civil task has a due date and the site has a DC capacity. SCHED-3 gives PMs a bulk screen to set Civil due dates across a tender.

Admin can switch the schedule between calendar days (the default) and working days (Sundays skipped).

---

## Layer 2 — Data fields

### On `Task` (SCHED-1)
| Field | Type | Notes |
|---|---|---|
| `due_date_source` | char, choices `manual` / `system`, nullable | Null only where `due_date` is null. The migration sets every existing non-null due date to `manual` (all project types). |
| `schedule_start` | date, nullable | Written only by the scheduler (SCHED-2). Supplies planned starts to the future Gantt. Never shown as a commitment. |

### Single date-setting function (SCHED-1)
`set_task_due_date(task, new_date, source, actor, note=None)`. This becomes the only writer of `Task.due_date` on all 7 write sites found in audit T2:
- the edit screen
- the In Progress date entry
- adding a task
- duplicate-for-locations
- Django admin
- the two Residential calculation functions

Every call writes an ActivityLog row with an `action_code`:
- `due_date_changed` for a manual edit, with the old and new dates in the log
- `due_dates_scheduled` for a scheduler run, as one row per site per run (not per task)

The existing Residential-only blocks marked "do not remove" stay exactly as they are.

### Schedule definition (SCHED-2)
`ScheduleBlock`
- `template` (FK TaskTemplate)
- `sequence` (int)
- `name`
- `days_le_50`, `days_51_100`, `days_101_plus` (Decimal 4,1)

`ScheduleBlockTask`
- `block` (FK)
- `template_task` (FK TaskTemplateTask), unique per template

Seeded for the OPEX Execution v1 template:

| Seq | Block | Template tasks | ≤50 | 51–100 | 101+ |
|---|---|---|---|---|---|
| 1 | Civil & MMS | Civil Work and MMS Installation | 3.5 | 7 | 11 |
| 2 | Modules | Module Installation | 1.5 | 4 | 5 |
| 3 | Earthing & DC | LA and Earthing Installation; DC Cable Laying with Conduit | 1.5 | 2.5 | 5 |
| 4 | Inverter & DBs | DCDB and ACDB Installation; Inverter Installation | 1 | 1 | 1 |
| 5 | AC cable | AC Cable Laying | 0.5 | 0.5 | 2 |
| 6 | Testing | *Testing-phase tasks — mapping confirmed by Zuber in SCHED-2 pre-flight* | 0.5 | 1 | 1 |

Tasks in no block get no system date:
- RMS Installation and Solar Generation Meter Installation (until the PMs give numbers)
- post-installation approvals
- Closeout
- the mirror tasks (COD, As-Built, HOTO)

Adding numbers later means adding a seeded row; no code change is needed.

### On `Project` (site)
| Field | Type | Notes |
|---|---|---|
| `exclude_from_auto_schedule` | bool, default False | Django admin only; changes logged. Turning it on clears the site's system dates. |

(The `planned_mobilization_*` fields were designed and then dropped on 8 Nov; see "Anchor redesign".)

### Admin setting (SCHED-2)
`schedule_skip_sundays` (bool, default **False** = calendar days, matching the Day-3 lock). Stored in the existing admin settings model if one exists (SCHED-2 pre-flight checks), otherwise in a new singleton. Public holidays are out of scope for v1.

---

## Layer 3 — User journey

1. **Admin** (once) checks the working-day switch. Flipping it shows "This will recalculate N system-set dates on M sites", requires confirmation, then recalculates.
2. **PM** opens the tender's "Mobilization dates" screen (SCHED-3). It lists the tender's activated sites with:
   - site code
   - capacity
   - band
   - current anchor
   - status: "Scheduled", "Civil already started (anchored to <date>)", "Needs capacity", or "Needs date"

   The PM ticks sites (or uses select-all), enters one date, and adjusts single sites inline. Before saving, the screen shows "K tasks will be overdue immediately"; the PM confirms.
3. The system schedules each selected site. The task list shows due dates; system dates carry a small "auto" marker and manual ones don't.
4. **SE / PM** works normally. A PM editing a due date turns that task manual, with no prompt.
5. When a block finishes late, later system-set dates on that site shift forward automatically.

---

## Layer 4 — Edge cases and rules

**Anchor**
- **Anchor** = the due date of the site's unlabelled Civil Work and MMS Installation task. With no date (or Civil N/A), the site is not scheduled. Full rules under "Anchor redesign" below.
- **Band**:
  - DC kWp ≤ 50
  - > 50 and ≤ 100
  - > 100

  Null or zero capacity means the site is not scheduled and shows "Needs capacity".

**Day arithmetic**
- Keep a running total *C* in half-days. The anchor is day 1.
- Block *i* starts on day floor(C₍ᵢ₋₁₎)+1 and is due on day ceil(Cᵢ), counted in calendar days, or working days when the switch is on.
- Example (≤50): blocks are due on days 4, 5, 7, 8, 8. Two blocks may share a due date.
- All tasks in a block get the same `schedule_start` and `due_date`.

**Late finish**
- Walk the blocks in order. For a block whose tasks are all Done or N/A, the next block's cursor = max(planned end, actual last Done date). Never pull a schedule earlier.
- A block that is entirely N/A takes zero days.

**What the scheduler never writes**
- Tasks that are Done, N/A, or whose `due_date_source` is anything other than `system`: that is `manual`, **or null with a date** (for example rows written by the demo seed command). Manual dates do not move the chain in v1.

**SCHED-1 rulings (8 Nov 2026)**
- **A. Logging:** `set_task_due_date()` writes the only activity row for a manual edit. It keeps the existing opening words ("Updated/Cleared due date for task: X") and adds "(was A, now B)". Callers drop their own line. No row is written when the date is unchanged. The log write runs in its own savepoint, so it can never abort the caller's transaction.
- **B. Residential cascade:** in `recalculate_from_task`, the starting task's typed date is `manual` (logged once); the tasks calculated after it are `system`.
- **C. Residential regression test:** test the "recalculate dates" button and switching cascade scheduling on. Both must produce the same dates as before, now marked `system` (activation never calculated dates).
- **D. Demo seed:** `seed_opex_test_data.py` is left untouched and listed as the one known exception to the no-direct-write rule. Its rows end up with a date and a null source, which the scheduler treats as untouchable.
- **Create-then-date:** `task_add` and duplicate-for-locations create the row, then set the date through the function and save just those fields. Both steps must stay inside the caller's existing transaction.
- Tasks with a `location_label` (duplicated per location; UKRU001 only).
- Mirror tasks, and tasks with no `template_task`.

**Triggers**
- anchor set or changed
- DC capacity changed
- Civil moved to In Progress
- any scheduled task moved to Done
- working-day switch flipped

Each trigger reschedules only that site, except the switch, which reschedules all sites.

**Site loses capacity or anchor**: system-set dates (and `schedule_start`) on its not-Done tasks are cleared, logged once. Manual dates stay.

**Other rules**
- **Scope**: RESCO (OPEX Execution v1) only. CAPEX gets blocks later by seeding rows. Residential is untouched.
- **Dates**: always `timezone.localdate()`; never `date.today()`.
- **"Due date required for In Progress"**: system dates satisfy it, and no PM confirmation is required (decision 7).

---

## Layer 5 — Bug pre-emption

- A missed write site in SCHED-1 would let a manual edit stay marked `system` and then be overwritten. Verification must exercise all 7 paths.
- Duplicate-for-locations copies the source task's due date. The copy must be `manual`, never `system`.
- The working-day switch must not recalc Done tasks or manual dates.
- Concurrency: run the scheduler per site inside one transaction with `select_for_update` on the site.
- The scheduler must run after commit when triggered from a status change, so a failed scheduler never rolls back an SE's status update. Log failures; don't raise.
- Decimal half-days: use integer half-day units internally, not floats.
- Sunday skipping must also apply to the anchor itself. A Sunday anchor moves to Monday when the switch is on.
- The overdue preview on the screen must use the same overdue rule as the dashboards, not a re-implementation.

---

## Layer 6 — Pre-flight simulation (expected, to verify in builds)

- About 1,819 undated execution tasks become eligible. Only sites with capacity and an anchor get dates.
- After SCHED-1: no visible change. 46 RESCO tasks plus all other dated tasks are marked `manual`.
- After SCHED-2 with a dry run: the 4 sites with Civil started can be scheduled. A dry-run report shows dates and an overdue count before any write.
- After SCHED-3: the dates entered decide the overdue exposure, and the PM sees it before saving.

---

## SCHED-2 rulings (8 Nov 2026, after pre-flight)

- **Block 6** = `TESTING_COMMISSIONING` only. `NET_METER_INSTALLATION` stays unscheduled, because it depends on the DISCOM.
- **Working-day switch** lives on the existing `SystemSettings` screen (`admin_master_switches`), with the confirmation rendered in place. No new URL.
- **Triggers:** one after-commit call inside `_apply_task_status_change` (covers status updates, self-certified completion, approve, reject, reopen), plus `task_set_not_applicable` (mark and un-mark), `TaskAdmin.save_model` (N/A edits), `project_field_edit` and a new `ProjectAdmin.save_model` (capacity, exclusion flag), and any change to the Civil task's due date (edit screen, In Progress date entry, Django admin). The scheduler is idempotent, so extra triggers are harmless.
- **Per-site exclusion:** new `Project.exclude_from_auto_schedule` (bool, default False), editable in Django admin only, with changes logged. An excluded site returns `excluded`: nothing is written or cleared. It is set by hand for UKRU001 after deploy, because a 900 kWp site does not fit the 101+ band's 25 days.
- **Anchor on multi-location sites:** use the unlabelled Civil task only.
- **Done date** = `Task.completed_at` (latest Done). **First In Progress** = the earliest task StatusTransition to In Progress. Both are converted with `timezone.localtime(...).date()`.
- **Eligible sites:** OPEX, not deleted, not test, activated, status Active or In Progress, at least one task from the OPEX Execution v1 template, and not excluded.
- **Known gap, not fixed here:** N/A edits made in Django admin skip the reason and the log. This is logged to SECONDARY_FINDINGS.

## Anchor redesign (8 Nov 2026, supersedes the planned-mobilization-date design)

- **No `planned_mobilization_date` field.** It is removed from SCHED-2 before commit.
- **Anchor** = due date of the site's unlabelled **Civil Work and MMS Installation** task, whatever its source. No date, or the task is N/A → `needs_anchor`. The `civil_started` anchor source is gone.
- **Block 1 is never written.** The Civil date belongs to the PM. Block-1 durations only set Civil's `schedule_start` (due date minus the duration, counted backwards), and only when that start is empty or system-set.
- **Block 2 starts on the next working day after the Civil due date**, with C reset to 0. No half-day carry-over from Civil (ruled 8 Nov).
  - Worked example: Civil due Tue 10 Nov 2026, band ≤50, calendar days → Modules due 12 Nov, Earthing & DC 13 Nov, Inverter & DBs 14 Nov, AC 15 Nov, Testing 15 Nov.
- **Late Civil:** if Civil is Done after its due date, blocks 2+ re-anchor from the next working day after its `completed_at` date. An early finish never pulls dates earlier.
- **Trigger:** setting, changing or clearing the Civil due date reschedules the site after commit. Clearing it clears the site's system dates.
- **Exclusion:** turning `exclude_from_auto_schedule` on clears the site's system dates on unfinished tasks; turning it off reschedules the site.
- **SCHED-3** becomes a bulk screen for setting Civil due dates across a tender.
