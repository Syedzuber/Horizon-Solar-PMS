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
| 6 | RMS & Gen meter | RMS Installation; Solar Generation Meter Installation | 0.5 | 0.5 | 0.5 |
| 7 | Testing | Testing & Commissioning | 0.5 | 1 | 1 |
| 8 | Net meter | Net Meter Installation | 0.5 | 1 | 1 |

Blocks 1–5 and Testing are seeded by migration 0138 (Testing at 6). Migration 0139 (SCHED-2c, see "Meter blocks") moves Testing to 7 and adds blocks 6 and 8.

Tasks in no block get no system date:
- pre-installation approvals (Net Metering Approval, CEIG Approval)
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

- ~~**Block 6** = `TESTING_COMMISSIONING` only. `NET_METER_INSTALLATION` stays unscheduled, because it depends on the DISCOM.~~ **Superseded 9 Nov:** see "Meter blocks". Testing moves to block 7, RMS & Gen meter is block 6, and Net Meter is block 8.
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

## Meter blocks (9 Nov 2026)

- **RMS Installation + Solar Generation Meter Installation** form one block: **0.5 day in every band**, placed after AC cable and before Testing.
- **Net Meter Installation** gets its own block **after Testing & Commissioning**: 0.5 / 1 / 1 days (≤50 / 51–100 / 101+).
- New block order:
  1. Civil
  2. Modules
  3. Earthing & DC
  4. Inverter & DBs
  5. AC cable
  6. RMS & Gen meter
  7. Testing
  8. Net meter
- Worked example, Civil due Tue 10 Nov 2026, calendar days. Each date is a due date; a range shows the planned start → due date.

| Block | ≤50 | 51–100 | 101+ |
|---|---|---|---|
| Modules | 11 → 12 Nov | 11 → 14 Nov | 11 → 15 Nov |
| Earthing & DC | 12 → 13 Nov | 15 → 17 Nov | 16 → 20 Nov |
| Inverter & DBs | 14 Nov | 17 → 18 Nov | 21 Nov |
| AC cable | 15 Nov | 18 Nov | 22 → 23 Nov |
| RMS & Gen meter | 15 Nov | 19 Nov | 24 Nov |
| Testing | 16 Nov | 19 → 20 Nov | 24 → 25 Nov |
| Net meter | 16 Nov | 20 → 21 Nov | 25 → 26 Nov |

- **Net Meter depends on the DISCOM.** Expect it to go red more often than the other blocks; this was accepted by Zuber.

## Live refresh (9 Nov 2026)

- When an action reschedules a site, the task list updates without a manual page reload. A short notice tells the user how many dates were updated automatically.

## Forecast shift (SCHED-2b, 9 Nov 2026)

- When a block is open (not all Done or N/A) and its due date has passed, the later **system** dates re-anchor so the next block starts on the next working day after today. A daily run at 06:30 IST, before the morning report, keeps this current.
- The late block's own dates never move, and manual dates never move. Dates are never pulled earlier.
- Each change is logged as `due_dates_forecast_shift`, one row per site.
- Scheduler log rows must not count as user activity for digests, "latest activity" or "gone quiet". This rule binds the future common dashboard.

**SCHED-2b rulings (9 Nov 2026)**
- **Same definitions as the late-finish rule.** A block's due date is its *planned* due date from the chain walk, never a task's stored date. For Civil it is the Civil task's own due date. "Complete" means at least one Done and every task Done or N/A; for Civil, the Civil task is Done. A complete block keeps the late-finish rule, and an open block past due is forecast. The two never apply to the same block, and at most one block per site is forecast, because every due date after it falls after today.
- **A system date is never written in the past for the first time** (amendment, 9 Nov). If an open block's planned due date is before today and none of its tasks has a system date, the block itself starts on the next working day after today with C = 0, and the blocks after it chain from it. The forecast row then reads "Forecast: <block> not yet dated and already past plan; started <date>".
- **A block whose tasks already carry system dates keeps them.** The chain re-anchors at the block's stored start, and never earlier than the plan. If those dates have passed, the block stays red and the forecast moves the blocks after it. So a first-dated block keeps its dates on the following days and turns red only after they pass. A block that has only manual dates counts as never system-dated.
- **At most one forecast event per site:** either a late block or a first-dated block. Every date after either one falls after today.
- **Logging.** `due_dates_forecast_shift` is always written with **no actor**, trigger runs included: "Forecast: <block> open past due <date>; N later dates moved to start <date>". N counts the due dates written in the blocks after the late block. When every change in a run is a forecast move, only this row is written. Otherwise `due_dates_scheduled` (with the triggering actor, unchanged) is written as well.
- **Readers.** The project overview's 8-line "Recent activity" hides `due_dates_forecast_shift`; the site timeline shows it. The Admin audit log's 7-day "Most active" ranks only rows with an actor; its "today" count still includes system rows.
- **Daily run.** A Railway cron service runs `python manage.py schedule_sites --all --apply` at `0 1 * * *` UTC (06:30 IST). Zuber configures it; the schedule is not in the repo.
- **`schedule_sites -v 2`** lists every block of each site: sequence, name, planned start -> due, each task's action, and `forecast` on the late block.

## First-dating rule (9 Nov 2026, SCHED-2b amendment)

- **A system date is never written in the past for the first time.** If a block is open, its planned due date has passed, and none of its tasks has a system date yet, the block starts on the next working day after today.
- **Once a block is first-dated, its stored dates are kept.** On later runs, the chain re-anchors at the block's stored start, never earlier than the plan. A first-dated block therefore turns red only when its own new dates pass, and the daily run never undoes a first dating.
- **A block with only manual dates counts as never system-dated.** Its manual tasks stay red; the block's other tasks are first-dated from tomorrow.

## Baseline (9 Nov 2026, SCHED-2e)

Zuber chose option B: keep the original plan (baseline) and the moving forecast side by side.

Fields

Task.baseline_start and Task.baseline_due hold the baseline.
Only three things may write them: the scheduler, the backfill migration, and rebaseline_site.

When the baseline changes

Until Civil starts: every scheduling run that changes the site's dates re-snapshots the baseline. PMs can still correct their plan freely.
Once Civil goes In Progress or Done: the baseline is frozen. Forecast shifts, late finishes and date edits never change it.
Exception to the freeze: a task dated for the first time after the freeze takes that date as its baseline.
Deliberate re-plan: rebaseline_site re-snapshots a site. It requires a note, runs as a dry run unless told to apply, and logs baseline_reset. A PM button for this comes in SCHED-3.

Site summary

Shown on the project overview: "Planned finish X · Forecast Y · N days behind plan".
Days behind = latest forecast date minus latest baseline date. Being ahead of plan shows as 0.

Backfill

The data migration copies today's dates as the baseline for the 20 IPGCL sites.

## Baseline amendment (9 Nov 2026, after the SCHED-2e pre-flight)

The baseline is the pure plan, not a copy of the current dates.

Civil task: the baseline is the Civil task's own due date.
Every other in-block task: the baseline is the date the chain arithmetic gives from the Civil due date, the capacity band and the working-day mode. It ignores three things:
the forecast shift;
the late-finish re-anchor;
the first-dating shift.
Manual dates: a manual date on a task does not change that task's baseline.
Not Applicable tasks: an N/A task has no baseline, and the site summary ignores N/A tasks (ruled 9 Nov, after the SCHED-2e build).

Why: the pure plan moves only when the Civil date, the capacity or the working-day mode changes. As a result:

No daily drift. The daily forecast run can never move the baseline, so a site that has not started cannot drift to "On plan".
Backfill shows real slip. Sites already pushed forward by the forecast (1901, 1908) get their original 6 Nov plan as their baseline, so they show real slip.

Unchanged:

The baseline still re-snapshots until Civil starts, then freezes.
Tasks first dated after the freeze still take their own baseline: their pure-plan dates, not the forecast date they were first given (ruled 9 Nov, after the SCHED-2e build).
rebaseline_site still re-snapshots a site on demand.

Known gap (v1): before Civil starts, a PM who pushes a late Civil date forward re-plans the baseline. The change is still logged as due_date_changed (was/now).

## SCHED-2e build notes (9 Nov 2026)

- **Pure plan.** `scheduling._plan(pure=True)` is the scheduler's own walk with the forecast, late-finish and first-dating shifts switched off. It is not a second copy of the arithmetic. For a site where nothing is late, the pure plan and the real plan are identical; a test pins this.
- **Civil has started** when its status is In Progress or Done, or it has a `completed_at`, or it has a StatusTransition row into In Progress or Done. The last test catches a Civil that started and was then Blocked.
- **When the baseline is written.** Every apply run re-snapshots it from the pure plan, writing only tasks whose baseline differs, until Civil starts. It writes no ActivityLog row of its own.
- **A task first dated after the freeze takes its pure-plan dates**, not the forecast date it was first given. Otherwise a task dated while the site is already late would carry no slip.
- **No baseline:** Not Applicable tasks, and any non-Civil task placed in block 1.
- **Unscheduled sites.** Before Civil starts, a site that becomes unscheduled (excluded, no capacity, no Civil date) has its baseline cleared, the same way its system dates are.
- **Summary scope.** The summary line shows only on eligible, non-excluded sites with a baseline. It ignores Not Applicable tasks.
- **`rebaseline_site` refuses an unscheduled site.** Re-planning one would only blank its baseline.
- **Backfill (0141).** Every in-block task on the sites that have system dates gets its pure plan. On local (production restore of 9 Nov 18:37) that is 220 tasks on 20 sites. 1901, 1908, 1902 and 2420 are 3 days behind; the other 16 are on plan.
