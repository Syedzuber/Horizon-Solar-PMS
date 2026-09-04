# Browser Test Plan — before the phase 1 merge

Horizon Solar PMS · 31 Aug 2026 · run against local demo data, not production

**Why this exists.** Twelve sessions and 999 tests have never been opened in a browser. Every claim
below is currently supported by tests and shell output only. This plan is the human verification.

**Setup.** Local database, then:

```
python manage.py seed_opex_test_data
python manage.py seed_scm_handoff_data --confirm
python manage.py runserver
```

Demo logins, password `DemoPass!2026`: `demo.pm` · `demo.coord` · `demo.se` · `demo.scm` ·
`demo.design` · `demo.finance` · `demo.ceo`. No Admin demo user — use your existing Admin account.

Sites: `DEMOOPEX01` and `DEMOOPEX02` active · `DEMOOPEX03` **Draft, activate this one by hand** ·
`DEMOOPEX04` Draft with design released · `DEMO-RES-01` active Residential.

**When done:** `python manage.py teardown_opex_test_data --confirm`

**How to use this.** Work top to bottom. **[B]** items are blockers — a failure stops the merge.
**[C]** items are checks — record and continue. Write what you actually saw, not "OK".

---

## 0. Read this first — things that look broken and are not

Do not raise these as failures.

- **Five tasks on every OPEX site sit at Not Started forever** — Design, Material Delivery, COD,
  As-Built Drawings, HOTO. They are mirrors; no derivation hook exists yet. Only Design has a live
  source and even that is unwired.
- **An OPEX site's current phase is never "Design."** Phase 1 holds no human-owned task, by design.
- **There is no way to create an execution-type site group through any screen.** The schema
  supports it; no creator was built.
- **Warehouses and the three capability flags exist in the database with no screen at all.**
- **Automatic date scheduling is off for OPEX.** Dates are set by hand, one task at a time.

---

## 1. Access isolation — the highest-risk area [B]

Phase 0's lockdown changed what every non-PM role can reach. It has only ever been tested against
fixtures. **These are the failures most likely to appear in the first week of real use.**

For each: log in, paste the URL directly, record what happens.

| # | As | URL | Expect |
|---|---|---|---|
| 1.1 | `demo.se` | a project they hold no task on | 403, not a redirect loop, not a 500 |
| 1.2 | `demo.design` | same | 403 |
| 1.3 | `demo.coord` | a project they do not coordinate | 403 |
| 1.4 | `demo.se` | `/dashboard/ceo/` | 403 |
| 1.5 | `demo.finance` | `/dashboard/ceo/` | 403 |
| 1.6 | `demo.finance` | `/reports/user-status/` | refused, not a 500, not blank |
| 1.7 | `demo.scm` | any project | **allowed** — SCM is portfolio-wide by decision D-4 |
| 1.8 | `demo.finance` | any project | **allowed** — same decision |
| 1.9 | `demo.ceo` | anything | allowed |

**1.10 [B]** As each of the seven roles in turn, open their own dashboard from the nav. Record any
that 500, render blank, or show a section with no data where data should exist.

**1.11 [C]** As `demo.se`, work out whether you can reach anything you would not expect to. Click
around rather than following this list. This is the test the list cannot write.

---

## 2. Soft delete [B]

**2.1** As `demo.pm`, delete a demo project. Then paste its URL directly. Expect refusal, not a
rendered page.

**2.2** With that project deleted, confirm it disappears from the PM dashboard, the project list,
and the CEO dashboard totals.

**2.3** Try to change a task status on the deleted project by URL. Expect refusal.

---

## 3. The admin surface [B]

Log in with your real Admin account.

**3.1** Open `/admin/projects/project/` and `/admin/projects/project/add/`. Both must load — they
were 500ing until B11.

**3.2** Open a Task in the admin. **`status` must not be editable.** Neither must `is_mirror`.

**3.3** Open a Project in the admin. **`status` must not be editable.**

**3.4 [C]** Click through every model listed in the admin index. Record any that 500.

---

## 4. OPEX activation — the headline feature [B]

**4.1** As `demo.pm`, open `DEMOOPEX03` (Draft). Find the activation control. **Record where it is
and whether you would have found it without being told.**

**4.2** Activate it. Expect: status Active, and a task list appearing.

**4.3** Count what you see: **7 phases, 22 tasks.** Name the phases in order and compare against
`docs/OPEX_task_template_spec.md` v1.3.

**4.4** Confirm **no payment milestones** were created. Residential mints M1/M2/M3; OPEX must not.

**4.5** Confirm all 22 due dates are empty.

**4.6 [C]** Does the page tell you which five tasks are mirrors, in any way at all? Record the
answer honestly — if it does not, a PM cannot tell why five rows never change.

**4.7** Activate `DEMOOPEX03` a second time by re-posting or re-clicking. Expect a refusal or no
change, not a duplicate set of tasks.

---

## 5. Mirrors [B]

On an activated OPEX site.

**5.1** As `demo.pm`, assign the **COD** task to yourself.

**5.2** Try to change its status. **Expect the refusal message**, naming the rule rather than a
permission. Write down the exact wording you see.

**5.3** Do the same on the **task detail screen**. The message must be identical — B8 consolidated
these paths so the wording cannot drift.

**5.4** Confirm the task's status did not change.

**5.5** Change a non-mirror task on the same site as the same user. It must work normally.

**5.6 [C]** Try setting a **due date** on a mirror. It will succeed — this is known and recorded.
Note whether it looks confusing on screen.

---

## 6. Current phase — four screens, one answer [B]

**6.1** On a freshly activated OPEX site, record the current phase shown on: the PM dashboard, the
Site Engineer dashboard, the BD dashboard, and the Admin project list. **All four must match**, and
must **not** say "Design".

**6.2** As `demo.se` or `demo.pm`, complete every task in the first phase that has human-owned
tasks. Re-check all four screens. The phase must have advanced, on all four.

**6.3** On `DEMO-RES-01`, record the current phase on all four. Compare to what you would expect
from the Residential template.

---

## 7. Counters and dashboards [B]

**7.1** On the PM dashboard, record the pending-approvals and blocked counts for an OPEX site.
**The five mirrors must not be in them.**

**7.2** Open the CEO dashboard as `demo.ceo`. Record the department totals. Look specifically at
the SCM row — Material Delivery is a mirror and must not appear.

**7.3** Look at a project's progress percentage. **Mirrors are out of both numerator and
denominator** — so progress is out of 17, not 22. Confirm the number is consistent with what you
can see.

**7.4 [C]** Does any number on any dashboard look wrong to you? This is the test that matters most
and the one no test file can write. Record anything that does not match what you know to be true.

**7.5 [C]** Open `/reports/user-status/` as your Admin account. Confirm the four status columns sum
to Tasks Assigned on every row.

---

## 8. Manual dates [B]

**8.1** As `demo.pm`, set a due date on an OPEX task assigned to **Site Engineer**. It must work —
a PM who owns the project skips the role check.

**8.2** Do the same on an SCM task and a Project Coordinator task.

**8.3** Confirm the OPEX site shows **no** "Recalculate dates" or "Enable cascade scheduling"
control.

**8.4** On `DEMO-RES-01`, confirm those controls **do** appear and still work. Recalculate and check
that 52 dates appear.

---

## 9. The status path [C]

**9.1** Move the same task through both the project overview row control and the task detail
screen. The rules, the messages and the outcome must be identical.

**9.2** Try an illegal transition — Done back to In Progress — on both. Same refusal, same wording.

**9.3** Set a task to Blocked without a reason. Expect the block-reason prompt, on both screens.

---

## 10. Two operational questions [C]

**10.1 — GRN practice.** As `demo.se`, find the delivery challan on the demo data and try to
confirm the GRN. It is scoped to engineers holding a task on that project. **Confirm with Sudhir or
your SCM lead that this matches how it is actually done** — if whoever is at the warehouse records
receipt in practice, the rule needs widening, not reverting.

**10.2 — Residential regression sweep.** Open `DEMO-RES-01` and walk a normal Residential flow end
to end: view phases, change a task status, tick a checklist item, look at the BOQ. **Anything that
feels different from before is a phase 0 or phase 1 regression and is a blocker.**

---

## 11. What you could not do

Not a test — a record. After working through the above, write down:

- anything you tried to do and could not find
- any screen you reached that you did not expect to
- anything a real PM or engineer would ask for in the first hour that is not there

This is the most useful output of the exercise, and the reason it is worth two hours of your time
rather than another session.

---

## Recording results

For each failure: what you did, what you expected, what happened, and the URL. A failed **[B]**
stops the merge until it has a session. A failed **[C]** goes into
`EXECUTION_MODULE_DEFERRED.md` §B and is scheduled.

**Teardown when finished** — `python manage.py teardown_opex_test_data --confirm` — and confirm the
per-model census returns to where it started.
