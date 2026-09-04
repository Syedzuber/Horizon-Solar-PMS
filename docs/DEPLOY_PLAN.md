# Deploy Plan — `execution-phase-1` to production

Horizon Solar PMS · 01 Sep 2026

**What is deploying.** Fourteen sessions on `execution-phase-1`, none of which has ever run against
production. `origin/main` is at migration **0064**; this branch is at **0075**. Eleven migrations,
one of them seeding the OPEX task template for the first time.

**What changes for users on day one: almost nothing.** No OPEX site is activated and none needs to
be. The template exists but attaches to nothing until a PM chooses. Residential is behaviourally
untouched — proved by 92 characterisation tests, a browser sweep, and every session that refused to
edit `project_activate`.

**What changes silently is access.** Phase 0's lockdown altered what six roles can reach. That is
the risk in this deploy, and it is the thing to watch.

---

## Before you push

### 1. Take a backup you have actually tested

Railway → Postgres → **Backups** → create one, and note the time. **Then confirm you know how to
restore it** — an untested backup is a hope. If Railway's restore path is unfamiliar, read it now,
not at 11pm.

### 2. Confirm the migration path

```powershell
$env:DATABASE_URL="<read-only url>"
python manage.py showmigrations projects | Select-String "\[ \]"
Remove-Item Env:\DATABASE_URL
```

Expect exactly the migrations from 0065 to 0075 unapplied, and nothing unexpected. If a migration
you do not recognise appears, stop.

### 3. Check the tree and the suite one last time

```powershell
git status                 # clean
git log --oneline -1       # the 1.6 commit
python manage.py test --settings=solarpms.test_settings
```

Expect **1060 tests, 1 failure, 1 error** — the SQLite constraint-name assertion and the
`test_whatsapp_templates` collection artefact. Anything else stops the deploy.

### 4. Tear down the demo data

```powershell
python manage.py teardown_opex_test_data --confirm
```

It is local-only and cannot reach production, but leaving it in place makes your local database an
unreliable comparison for the next week.

### 5. Tell the team it is happening

One message, before not after. Name the day, say that nothing they do changes, and say that if a
screen they used yesterday refuses them today they should tell you rather than working around it.
**That last sentence is the most valuable part of the deploy plan** — the access lockdown's failures
are invisible unless somebody reports them.

---

## The deploy

```powershell
git checkout main
git merge execution-phase-1 --no-ff
git push
```

`--no-ff` keeps the branch visible as one unit in the history, which matters if you ever need to
identify what shipped together.

Railway redeploys and runs `migrate --run-syncdb` itself. **Watch the deploy log until it goes
green.** Migration 0075 seeds the template; expect it to log `7 phases, 23 tasks, 8 mirrors`.

**Do not push at the end of a day.** Push in the morning, with hours of working time after it.

---

## Immediately after — twenty minutes, non-negotiable

Log in as yourself and check, in this order:

1. **The site loads.** `pms.horizonrenewablepower.in` — the login page renders.
2. **A Residential project opens** and looks exactly as it did yesterday. Phases, tasks, BOQ,
   milestones.
3. **The OPEX template exists**: an OPEX site in Draft shows an activation control, and the
   `TaskTemplate` table has an active OPEX row with 23 tasks.
4. **No OPEX site is activated** — `SELECT COUNT(*) FROM projects_project WHERE project_type='OPEX'
   AND activated_at IS NOT NULL;` should be 1, the same one as before.
5. **The admin loads** — `/admin/projects/project/` and a Task change form. Both were 500ing before
   B11.
6. **The CEO dashboard loads** as a CEO account, and its numbers are not obviously different from
   yesterday's.

If any of these fail, you are in the rollback section below.

---

## The first 48 hours — what to watch, in priority order

### Access refusals — the real risk

Six roles can reach less than they could. Everything they lost, they lost correctly — but "correct"
and "nobody depended on it" are different claims, and only the second matters to the person who
cannot do their job.

**Watch for:** anyone saying a screen now refuses them. Do not assume it is a bug and do not assume
it is correct. Open `ACCESS_ISOLATION_AUDIT.md`, find the endpoint, and see what the rule is.

**The two most likely:**

- **GRN confirmation.** Scoped to engineers holding a task on that project. You have confirmed this
  matches practice — but confirm again with Sudhir the first time someone actually records one.
- **A profile-less account.** You confirmed none exists. If one appears, give it a `UserProfile`;
  do not restore the fail-open.

**If a refusal turns out to be wrong, widen the rule. Never revert the lockdown.**

### The unexplained twelve

Twelve `PaymentMilestone` rows sit on non-Residential projects and **nobody knows what created
them** (§B28). The reasoning that says they cannot exist is sound; the rows exist anyway. That means
a path nobody has identified can mint them.

Watch whether the number moves. If it becomes thirteen, something wrote it after the deploy and you
have a live mechanism to find.

```sql
SELECT COUNT(*) FROM projects_paymentmilestone m
JOIN projects_project p ON p.id = m.project_id
WHERE p.project_type != 'Residential';
```

### Notifications

Phase 1 did not touch them, but three call-site fixes were never verified on Railway with a real
phone in hand — `issue_resolved`, `boq_acknowledged`, `assign_project`. If anyone reports a WhatsApp
message with the wrong project name, that is why. `NotificationLog` will show `sent` either way;
"sent" has never meant "correct" on this integration.

### The EOD digest

Confirm one arrives. `SystemSettings.email_enabled` defaults to `False` and fails **silently** —
absence of an error is not evidence of a send. The morning after, check `NotificationLog` for a row
with `status='sent'`.

### Nothing at all

The most likely outcome of the first 48 hours is that nobody notices anything, because nobody is
using the system yet. That is a successful deploy, not a wasted one — it means the access lockdown
and eleven migrations landed without breaking what little is in use.

---

## Rollback

**Code rollback is easy. Schema rollback is not.**

Reverting the merge and pushing returns the code. The eleven migrations stay applied — and that is
usually fine, because the new columns are additive and old code ignores them. The two exceptions:

- **`uniq_active_site_group_membership_per_type`** replaced the old constraint. Reverted code
  creates only procurement memberships, so `(project, group_type)` unique behaves identically to
  `(project)` unique. Safe.
- **The OPEX template rows.** Harmless to old code, which never looks for them.

So the practical rollback is: **revert the code, leave the schema.** Restoring the backup is the
last resort, and it costs whatever was written since.

**One thing genuinely cannot be undone:** if a PM activates an OPEX site, that site has 23 tasks
forever. There is no deactivation and project closure is phase 5. Which is the argument for the next
section.

---

## The first activation — deliberately, not casually

Nothing forces you to activate anything. When a real OPEX site genuinely starts:

1. **Activate one. Stop.** Check the site's task list, the PM dashboard, the CEO totals, and
   `NotificationLog`.
2. **Watch the PM's inbox.** Each activation assigns three PM tasks. One site is fine; twenty at
   once buries somebody. Tell Chetan before the first one, not after.
3. **Never write a bulk activation command** unless it re-implements the `status != 'Draft'` guard —
   which lives inside the view and does not travel — and is tested against a double run first
   (§B24). Without it, a rerun gives a site 46 tasks and nothing stops it.

---

## What is still not built, and should be said out loud

If anyone asks whether the system is ready, this is the honest answer:

- **15 of 23 OPEX tasks work fully.** Assignable, statusable, due-datable, ledgered.
- **8 are mirrors with no source.** Design has a live source and no hook; the four deliveries need
  B-18 **and** SCM's catalogue mapping (52 of 207 items map today); COD, As-Built and HOTO need
  phase 5.
- **SCM and Design own no actionable OPEX task** — both have mirrors only.
- **Execution site groups cannot be created** through any screen. The schema supports it; no
  creator was built.
- **Warehouses and the three capability flags** exist in the database with no screen at all.
- **No scheduling.** Dates are manual, one task at a time.

That is a real field-execution system for a site engineer and a PM, and a placeholder for SCM and
Design. Saying so now is cheaper than having it discovered.

---

## After the deploy — the shortlist

In the order I would take them:

1. **The Design derivation hook.** The only mirror with a live source, and it proves the mechanism
   before COD depends on it. Also forces a real answer to §B23's finding: the hooks cannot call
   `_apply_task_status_change()`, which needs a `request`, so the transition table gets restated
   somewhere or it diverges.
2. **B-18** — the `DCLineItem` → `BOQItem` FK. Nothing about deliveries derives without it.
3. **SCM's catalogue mapping.** Needs Sudhir, not a session. Start asking now.
4. **B-11's HSE list** and **task durations** — both need other people, both block later phases.
5. **B20** — the missing Coordinator department row on the CEO rollup. 95 tasks counted in totals
   and in no department.
6. **1.2b** — `ProjectAssignment`, staged, against a system you have watched for a fortnight.

---

## Premortem

**Most likely: nothing happens.** Nobody is using the system, the migrations apply cleanly, and the
first week is quiet. The risk then is complacency — a quiet deploy proves the code runs, not that
the access rules match how people work. That evidence only arrives when they start.

**Second, and the one to plan for:** somebody loses a screen they used and works around it rather
than telling you. WhatsApp is the workaround and it is already the default for 95% of the team. You
find out in October that a workflow has been dead since September. The mitigation is the message in
step 5 — asking explicitly for refusals to be reported — and asking Chetan and Sudhir directly at
the end of week one rather than waiting to be told.

**Third, moderate:** migration 0075 fails partway on production. It has run cleanly on a local
database dozens of times, and Railway's `migrate --run-syncdb` runs it inside a transaction, so a
failure should roll back rather than half-apply. Should. This is the backup's job.

**Fourth, low but expensive:** somebody activates several OPEX sites to "see how it looks", and
those sites carry 23 tasks each, permanently, in every count. There is no undo. One site, then stop.

**What success looks like:** the deploy log goes green, a Residential project looks identical, an
OPEX site in Draft offers an activation button that nobody presses, and in a week somebody tells you
they cannot open something they used to. That last one is not a failure — it is the first real
information this system has produced.
