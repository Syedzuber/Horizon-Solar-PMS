# Execution Module — Deferred Findings

Items found during execution-module work but deliberately **not fixed**. Opened by
prompt 0.1 (context pack). Nothing outside this file was changed by that session.

This file exists because of rule **R-12** in [`docs/execution-model.md`](docs/execution-model.md):
*never fix an unrelated finding in the same session.* A session that trips over a bug,
an inconsistency or a gap outside its own scope records it here and continues. Fixing
it in place makes the session's diff unreviewable and couples an unrelated defect to a
feature's rollback.

**What belongs here**

- A defect found while implementing something else.
- A gap the current prompt deliberately leaves open, with the reason.
- A decision taken that a later session must know about but that is not itself a bug.
- A stop-condition breach, recorded honestly rather than quietly.

**What does not belong here**

- Anything the current prompt was asked to build. That is scope, not a deferred item.
- Speculative improvements with no observed trigger.
- Findings a verification report should carry instead, until the product owner has
  decided they are deferred rather than in scope.

**Entry format.** One `###` heading per finding, numbered within its lettered section
(`A1`, `A2`, …). State what was observed, where — with `file:line` — why it was not
fixed, and what it blocks or risks if left. Close an item by striking its heading and
appending **— CLOSED by <prompt>**; never delete a closed entry, so the history of what
was known and when stays readable.

---

## A. Phase 0 — foundations (prompts 0.1 – 0.6)

### A1 — B-8: the inline BOQ acknowledge writes a `BOQRevision` snapshot, the standalone endpoint does not

`projects/views.py:5068-5075` (inline `acknowledge_scm` branch of `boq_detail`) versus
`projects/views.py:6312` (`boq_acknowledge`).

0.2b moved the status write, the `ActivityLog` row and the notification into
`_apply_boq_acknowledgement()` (`views.py:4763`), so those three no longer depend on which
control the user pressed — that was defect **B-5**, and it is closed. 0.3 later put the
`StatusTransition` write inside the same helper for the same reason.

**One asymmetry was left standing.** Only the inline branch creates the revision row:

```python
snapshot = _boq_snapshot(boq)
BOQRevision.objects.create(
    boq=boq, revised_by=profile,
    version=boq.version,
    reason=f'SCM Acknowledged v{boq.version}',
    snapshot=snapshot,
)
_apply_boq_acknowledgement(boq, profile, request)
```

The standalone endpoint calls `_apply_boq_acknowledgement()` alone. The helper's own
docstring records the boundary: *"The revision snapshot also stays with the inline caller —
only that path has ever written one."*

**Why it was not fixed.** 0.2b's prompt named three duplications to extract — the snapshot
helper, the acknowledgement, and the M2 map — and this was not one of them (**R-12**). It is
also not a straight extraction: the two paths differ in *which* is right. Adding the snapshot
to the standalone endpoint changes what that endpoint records; removing it from the inline
one destroys a record that has been written for as long as the branch has existed. That is a
decision about what a BOQ acknowledgement *is*, not a deduplication.

**Risk if left.** The acknowledgement audit trail depends on which button was pressed —
exactly the class of defect 0.2b existed to remove, surviving in the one place 0.2b was told
not to look. It is narrower than B-5 was (nothing is notified or not notified; a history row
is written or not), but it is the same shape. Whoever owns BOQ revision history next should
settle which behaviour is correct and make both paths do it.

### A2 — `_DESIGN_EDITABLE` is spelled out twice in `boq_detail`, and the duplicate is unreachable from the constant

`projects/views.py:4952` defines `_DESIGN_EDITABLE = ('Draft', 'Revision Requested',
'Acknowledged')` **inside** the `if request.method == 'POST'` block. The GET path at
`views.py:5137` computes `design_form_open` from a repeated literal instead, because the
constant is not in scope there.

Both sites carry a comment pointing at the other, and they are in step today. **This has a
written deferral already** — `DESIGN_MODULE_DEFERRED.md` **J3**, which states the fix (hoist
the constant to module scope: one line, no behaviour change) and says it should be done by
whoever next has a reason to touch that view.

**Recorded here rather than re-deferred there** because phase 0 read it as a *fourth*
duplicated constant, alongside the three 0.2b consolidated, and because J3's own note matters
to this module: A2/C2 in that file — `'Acknowledged'` being editable at all, so Design can
still edit a BOQ SCM has acknowledged — is now duplicated too, and fixing it means editing
both places.

**Why it was not fixed.** 0.2b's scope was three named extractions (R-12). `boq_detail` was
also under an explicit instruction from Part 6 to leave that constant alone.

**Risk if left.** If the two drift, the symptom is a page that renders inputs the POST
handler then refuses, or the reverse — a form that silently does nothing.

### A3 — `_TASK_TO_PROFILE_ROLE` is still declared locally, twice

`projects/views.py:4252` and `projects/views.py:7372`, both
`{'BD / Sales': 'BD'}`.

0.2b consolidated the **forward** map — seven byte-identical local copies of
`_PROFILE_TO_TASK_ROLE` became one module-level constant at `views.py:413` — and did not
touch the inverse. `DESIGN_MODULE_DEFERRED.md` **K5** recorded six local dicts (four forward,
two inverse) expressing one mapping; **K5 is now half closed.**

**Why it was not fixed.** The 0.2b commit message states the forward consolidation was itself
pulled forward from prompt 1.2; extending it to the inverse was not in scope (R-12).

**Risk if left.** K5's original point stands for the remaining two: this mapping is what
decides whether a user may change a task's status, set a due date or tick a checklist item,
and a future role that needs to act on tasks must still be added in three places rather than
one. Lower than K5's original six, and no longer symmetrical — the forward direction is now
safe and the reverse is not, which is a worse thing to reason about than either uniform state.
**Prompt 1.2 should finish it and close K5.**

### A4 — `user_can_act_on_project()` was proposed by the audit and never built

`ACCESS_ISOLATION_AUDIT.md`'s "Proposed scope for prompt 0.2", Step 1, says to replace the
PM-only guard with *"a single call to the existing `permissions.user_can_view_project()` (for
reads) or a new `user_can_act_on_project()` (for writes)"*.

**The second helper does not exist.** `projects/permissions.py` has no such function, and 0.2
routed 19 endpoints — writes included — through `user_can_view_project()`.

**This is not a defect and is not being reported as one.** The audit's own Step 1 observes
that under today's policy the two questions have the same answer, so one helper is
behaviourally sufficient, and 38 isolation tests pin the behaviour that shipped.

**Recorded because the gap between a report and the code is worth one line.** A future session
reading the audit will look for `user_can_act_on_project()` and not find it, and should know
that is a decision rather than an omission. It is also the natural seam: the day view
authority and write authority need to differ — the most likely trigger being a QA/QC or
warehouse role that may *see* a project it may not *write* to — this is where the split goes.

### A5 — `Project` resolution is single-path in new code and four-exceptions in old

**R-16** says a Project is resolved through `_active_project()`. Four call sites in
`projects/views.py` — `project_delete` (`:2543`), `task_assign_design_head` (`:4302`),
`admin_assign_pm` (`:10331`), `subadmin_projects` (`:11117`) — plus `_opex_site()` in
`projects/design_views.py:221` still call `get_object_or_404(Project, …)` directly.

**Every one already passes `is_deleted=False`.** They pre-date 0.2c and were correct before
it, which is exactly why 0.2c left them: the prompt's job was closing the ~30 sites that had
**no** filter, not churning the handful that did. Two of them (`subadmin_projects`,
`_opex_site`) also key on something other than `project_id`, which `_active_project()` does
not accept in its current signature.

**Why it was not fixed.** Out of 0.2c's scope, and the safety property already holds. Changing
`_opex_site()` is additionally a design-module edit, which has been correctly scoped out all
programme.

**Risk if left.** Low for correctness, real for legibility: R-16 reads as absolute and the
code has five exceptions, so the next reader must decide whether each is a bug. That decision
is the cost the rule exists to remove. R-16 in `docs/execution-model.md` §3 names all five
explicitly so nobody has to re-derive the list — **keep that list current if any of them
moves.**

---

## B. Phase 1 — grouping, assignment and templates (prompts 1.1 – 1.4)

_No entries yet._

---

## C. Phase 2 — installation, HSE, QA/QC and punch points (prompts 2.1 – 2.4)

_No entries yet._

---

## D. Phase 3 — design handover and as-built (prompts 3.1 – 3.4)

_No entries yet._

---

## E. Phase 4 — material movement verification (prompts 4.1 – 4.4)

_No entries yet._

---

## F. Phase 5 — statutory approvals, vendor verification and handover (prompts 5.1 – 5.4)

_No entries yet._

---

## G. Cross-cutting — found outside any one phase

### ~~G1 — `dashboard_ceo` has no role gate at all~~ — **CLOSED by prompt 0.2**

**Closed 28 Aug 2026.** `dashboard_ceo` now carries
`@role_required(['CEO', 'Admin', 'System Admin'])` at `projects/views.py:2270`, and its
docstring names the reason for the three roles rather than one — Admin and System Admin are
unrestricted per `docs/execution-model.md` §2 D-4. The bounce behaviour this entry flagged as
the reason it needed its own session was handled in the same commit: `role_required()` now
returns **403** instead of redirecting to `get_user_dashboard()`, so the loop concern through
`ROLE_DASHBOARD` cannot arise. `tests_access_isolation.DashboardGateTests` pins it.

*Original entry, kept for the record:*

`projects/views.py:2232-2241`. The view carries only `@login_required`. Its own docstring
states *"Access: CEO role only"*, but there is no `@role_required(['CEO'])` decorator and
no in-body role check, so **any authenticated user** — Site Engineer, Design, BD, a user
with a blank role — can open `/dashboard/ceo/` and read the whole portfolio: every
project, financial totals, and the payment-milestone figures.

Compare `user_list` at `views.py:2248-2252`, which stacks `@login_required` +
`@role_required(['Admin'])` for far less sensitive data.

**Not fixed here** because the CEO daily report session was scoped to the report and was
explicitly instructed to leave this alone. It is not a side effect of that work — the gap
predates it.

**Risk if left:** portfolio-wide financial disclosure to every logged-in account. This is
the widest of the access gaps found so far and should be treated as more urgent than the
rest of this section, not folded into the general 0.2 lockdown queue. One decorator line
fixes it; the reason it needs its own session is that `dashboard_ceo` is reachable from
`ROLE_DASHBOARD` (`decorators.py:22`) and from `get_user_dashboard()`'s fallback, so the
bounce behaviour for a wrong-role user needs checking rather than assuming.

### G2 — `APP_BASE_URL` is not defined; every email link uses a hardcoded fallback

`projects/management/commands/send_eod_digest.py:98-99` reads
`getattr(settings, 'APP_BASE_URL', 'https://horizon-solar-pms-production.up.railway.app')`.
There is no `APP_BASE_URL` in `solarpms/settings.py`, so the fallback is *always* what is
used — the `getattr` is decorative.

The CEO daily report's "open the full report" link reuses that same expression rather than
introducing a second source of truth, so this session did not widen the problem, but it did
add a second consumer of it.

**Not fixed** (R-12, and the session was told to record only). **Risk if left:** the day the
Railway domain changes, every link in every digest email silently points at a dead host, and
nothing in settings names the value to update.

### G3 — `settings.py:203-204` overwrite the digest addresses, killing their env config

`ADMIN_DIGEST_EMAIL` and `HR_DIGEST_EMAIL` are defined via `config(...)` with placeholder
defaults at `solarpms/settings.py:151-152`, then **unconditionally reassigned to hardcoded
literals fifty lines later** at `:203-204`, after the `LOGGING` block:

```python
ADMIN_DIGEST_EMAIL = 'smzk07@gmail.com'
HR_DIGEST_EMAIL = 'shweta@horizonrenewablepower.com'
```

The `ADMIN_DIGEST_EMAIL` / `HR_DIGEST_EMAIL` environment variables therefore have no effect
on Railway or anywhere else, and the placeholder guard in `_run_aggregate()` (which aborts
the aggregate send while either address still contains `REPLACE_WITH`) can never fire.

**Not fixed** (session was told to record only). **Risk if left:** changing a digest
recipient requires a code change and a deploy, and looks from the top of the file as though
it should be an env var. Two people editing the "wrong" one is the predictable failure.

### G4 — `parse_date()` raises on an in-range-looking but invalid date

Not a codebase defect — a Django API sharp edge found while building the report, recorded so
the next person does not repeat it. `django.utils.dateparse.parse_date` returns `None` for a
string that does not look like a date, but **raises `ValueError`** for one that matches the
regex with out-of-range values (`'2026-13-45'` → `month must be in 1..12`).

Code that treats it as "returns None or a date" ships a 500 on a hand-edited query string.
`report_views._resolve_report_date()` catches both. **Any other view parsing a user-supplied
date should be checked** — this session did not audit for other call sites (R-12).

### G5 — the per-user status report is not covered by a committed test module

`projects/reports.py` and `projects/report_views.py` were verified during their build session
by a temporary scaffold (query-count flatness at 11x users, the row-sum invariant under
multi-project users, soft-delete/cancelled/un-activated exclusion, the deactivated-user drop,
the full role matrix including a profile-less superuser, and six malformed/future date inputs)
— **all 12 checks passed, and the scaffold was then deleted** because tests were outside that
prompt's fixed scope.

**Risk if left:** the row-sum invariant is the thing that catches a join fan-out, and a future
change adding a column or a relation to `build_user_status_rows()` has nothing standing guard
over it. The scaffold is reconstructable from this entry; committing it as
`projects/tests_user_status_report.py` is a small, self-contained follow-up.
