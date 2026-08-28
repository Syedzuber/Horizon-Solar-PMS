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

_No entries yet._

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

### G1 — `dashboard_ceo` has no role gate at all

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
