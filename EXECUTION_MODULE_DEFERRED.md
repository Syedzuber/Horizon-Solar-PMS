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

### B1 — `in_draft_group` is computed twice, with two different spellings

Found by the A-1.1 audit (read-only), 29 Aug 2026. `design_views.py:design_change_request`
(~3119) and `design_views.py:design_change_request_form` (~4055) each compute the same gate, and
they do not spell it the same way:

```python
# design_change_request (the POST) — the locked case has already returned above
in_draft_group = membership is not None

# design_change_request_form (the GET)
group_locked   = membership is not None and membership.group.status == SITE_GROUP_LOCKED
in_draft_group = membership is not None and not group_locked
```

The two agree **only** because the POST returns early on the locked case, several lines above.
The form's own comment says the two must agree — *"the window the FORM offers must agree with the
one design_change_request() enforces, or the PM gets a button that 403s or a missing button that
would have worked"* — and keeps them in step by hand rather than by construction.

**Not fixed** — A-1.1 is an audit session and may not modify `.py` files, and `design_views.py`
is behind a standing scope boundary (see B2). **Risk if left:** any future edit to the locked-case
early return in the POST silently changes what `in_draft_group` means on that side only, and the
form and the POST diverge with no test between them. The natural fix is one shared helper
returning `(membership, group_locked, in_draft_group)`, called by both — which prompt 1.1 is
already touching both functions to do.

### B2 — fixing the change-request gate requires editing `design_views.py`, which is behind a standing scope boundary

Found by the A-1.1 audit, 29 Aug 2026. Recorded here as a **decision a later session must know
about**, not as a defect.

`docs/execution-model.md` §2 D-1 requires `SiteGroup.group_type`, and adding it makes
`design_views.py:active_group_membership`'s `.first()` order-dependent — its own docstring says
that `.first()` is *"picking the only row that can exist, not the first of several"*, and that
stops being true the moment two live memberships are possible. The gate it feeds, `in_draft_group`,
decides whether a **released** design may be reopened by a PM. A wrong answer does not error; it
answers a different question.

**Every piece of the fix is inside `design_views.py`** — the helper, both gates, `_add_sites()`'s
duplicate-add check, `remove_from_group()`'s hardcoded *"Removed from procurement group"* log
string, `_group_or_404()`, and `site_group_lock`'s write path. §13 of the model doc states the
boundary plainly for `DesignAssignment`: *"editing design_views.py … has been correctly scoped and
untouched all programme. It is a session of its own."*

**Not fixed, and deliberately not worked around.** **Risk if left:** prompt 1.1 cannot deliver D-1
without crossing this boundary, so it must be granted permission explicitly and in writing before
it starts — otherwise it will either stop halfway or cross the boundary on its own judgement. The
smallest diff (three lines, one function, no caller changes) is quoted in `SITE_GROUP_AUDIT.md`
Task C.

Two related notes for whoever takes that session: `post_qc_pool()` silently loses sites to
execution groups unless narrowed (Task B), and `project_boq_is_group_locked()` is correct today
only because `'locked'` happens to exist on no other group type (Task D).

### B3 — the checklist fixture is written out three times across two regression-net files

Found by prompt 1.0 (test-baseline repair), 29 Aug 2026. The *create draft → add items →
`activate()`* fixture is not one shared helper. It exists as:

- `tests_soft_delete.py:200` — `SoftDeleteBase._make_checklist()`, a real helper, used by
  `DeletedProjectWriteRefusalTests` and `LiveProjectStillWorksTests`
- `tests_residential_baseline.py:1058` — the same lines inline in
  `test_the_assigned_user_completes_a_checklist_item_with_a_photo`
- `tests_residential_baseline.py:1088` — the same lines inline again in
  `test_a_checklist_item_cannot_be_checked_without_a_photo`

All three were repaired identically and independently. **Not consolidated** — R-12: prompt 1.0's
mandate was to reorder fixtures, and lifting a shared helper across two test modules is a refactor
of the regression net immediately before phase 1 leans on it. **Risk if left:** the next change to
how a checklist is published (a third status, a required `code`, a `published_by`) has to be made
in three places, and a fixture that is only fixed in two of them fails at the fixture line again
rather than at an assertion — which is exactly the failure mode this session existed to clear.
`tests_checklist_snapshot.py:118` `_publish_checklist()` is a fourth spelling of the same idea and
is the one to consolidate *onto* if a later session is permitted to.

### B4 — the `is_active` setter shim writes `status` directly, bypassing `activate()`

Found by prompt 1.0, 29 Aug 2026, as the proximate cause of the four errors it repaired.
`Checklist.is_active` (`models.py:2176`) is a deprecated property shim kept so old readers still
see one truth. Its **setter** does:

```python
@is_active.setter
def is_active(self, value):
    self.status = self.ACTIVE if value else self.ARCHIVED
```

`Checklist.objects.create(name=..., is_active=True)` therefore produces an *active* checklist
without ever passing through `activate()` — so no draft check, and none of the sibling versions
with the same name/code get archived. The three broken fixtures all did exactly that, and R-7 then
correctly refused the item they added next. The read half of the shim is sound; it is only the
write half that offers a second, weaker door into the active state.

**Not changed** — R-12, and it is a non-test file. **Risk if left:** any caller that sets
`is_active = True` mints an active version that skipped activation, which can leave two active rows
for one `code` if the partial unique index does not happen to catch them, and no
`TemplateVersionLocked` where one is due. The observation stands that the setter would be better
raising `TemplateVersionLocked` (pointing the caller at `activate()`) than silently writing
`status`; that is a product change and needs its own session with a survey of every assignment to
`is_active` first.

### B5 — `save()` guards on `group_type` are bypassed by `update()` and `bulk_create()`

Found and **deliberately not fixed** by prompt 1.1a, 29 Aug 2026. Recorded because it is a
known hole, not a discovered one.

`SiteGroupMembership.save()` stamps `group_type` from the group on insert and refuses to change
it afterwards; `SiteGroup.save()` refuses to change it at all. Neither runs under
`QuerySet.update()` or `bulk_create()`, so either one can write a `group_type` the guards would
have refused — including a membership whose type disagrees with its own group's, which would put
the row under the wrong half of the exclusivity constraint.

**Why it was not fixed:** the alternatives are worse at this stage. A `CheckConstraint` cannot
reach across the FK to compare the two columns; a database trigger is not a thing this codebase
has anywhere; and refusing `update()` outright would break soft removal, which is an
`update()` in `seed_scm_handoff_data` and a `save()` in the view. **This is the same half-measure
R-17 and §13 already document for `StatusTransition`** — the ledger is complete only for writes
that go through `record_transition()`, and this column is honest only for writes that go through
`save()`.

**What makes it acceptable today:** the only production writer is `design_views._add_sites()`,
which uses `objects.create()` per row for its own documented reasons (per-site savepoints, so
one refused site does not fail the batch). Verified at 1.1a's pre-flight. `grep bulk_create`
returns no hit on either model.

**What would make it unacceptable:** anything that reaches for `bulk_create()` on
`SiteGroupMembership` — most likely a future "add all released sites to this group" bulk action.
Such a caller must set `group_type` itself. Stated in a comment on both `save()` methods so the
person writing that caller is told at the point they would get it wrong.

### B6 — four documents still name the pre-1.1a constraint, and were out of scope to fix

Found by prompt 1.1a, 29 Aug 2026. The constraint was renamed
`uniq_active_site_group_membership` → `uniq_active_site_group_membership_per_type`. Three
documents and one migration still carry the old name:

| File | Status |
|---|---|
| `projects/migrations/0052_sitegroup_sitegroupmembership.py` | **Correct as-is, leave it.** A migration is history; it created the constraint under its original name and must keep saying so. |
| `ACCESS_ISOLATION_AUDIT.md` (~719) | Stale. Out of 1.1a's write scope. |
| `PARTS_11_4.6_10_STATUS.md` (~837) | Stale. Out of 1.1a's write scope. |
| `PHASE_0_COMPLETION.md` (~323) | Stale, **and now wrong in substance** — it states the migration "must alter `uniq_active_site_group_membership` from `(project)` to ...", which is the shape F-1 showed does not compile. Out of 1.1a's write scope. |

Two in-scope references **were** updated: `docs/execution-model.md` §2 D-1, and the comment in
`TaskTemplate.Meta` that cites the site-group constraint as the precedent for its own partial
unique index.

Not urgent — all four are records rather than instructions, and none is loaded by code. Worth a
sweep whenever one of those documents is next opened for another reason.

**Prompt 1.1b adds two more, both missed by the table above because it swept `.md` files and
these are `.py` comments.** Neither is behaviour-bearing; no code matches on the constraint
name string anywhere (1.1b pre-flight verified this, and `_add_sites()` catches the exception
*type*, not the message).

| File | Status |
|---|---|
| `projects/migrations/0070_checklist_drop_is_active.py` (~31) | Comment citing the old name as precedent. A migration is history — arguably correct as-is, like `0052`. Leave it. |
| `projects/management/commands/seed_scm_handoff_data.py` (~429) | Comment explaining why the fixture stamps `removed_at` via `update()`, citing the old name. Stale wording only. Fix when that command is next opened. |

---

### B7 — `boq_detail`'s `locked_group` banner lookup is the one membership read 1.1b did not narrow

Found and left by prompt 1.1b, 29 Aug 2026. **`projects/views.py` was on that prompt's
forbidden list**, so this was reported rather than fixed. [views.py:5145](projects/views.py#L5145):

```python
locked_membership = (project.group_memberships
                     .filter(removed_at__isnull=True, group__status='locked')
                     .select_related('group', 'group__locked_by__user').first())
```

It is a hand-written second copy of `project_boq_is_group_locked()`'s query — same two filter
terms — fetched only to name the locking group in the BOQ banner. 1.1b narrowed the predicate
and could not narrow this.

**Why it is not urgent.** It runs only inside `if boq_group_locked:`, and that flag now comes
from the narrowed predicate, so it fires only when a locked *procurement* group exists. Its own
`group__status='locked'` term restricts it to a status only procurement groups use. It is
therefore correct today — by the same coincidence the predicate was correct before 1.1b, and
with the same expiry date.

**Why it is real.** If an execution lifecycle ever reuses the `status` column or the string
`locked`, a site in both a locked procurement group and a locked execution group would have two
matching rows and an unordered `.first()` between them: the banner would name the PM's execution
batch as the reason the BOQ is frozen. Cosmetic, but it is a screen SCM and the designer read to
find out who to talk to.

**The fix is one filter term** — `group_type='procurement'`, spelled on the membership, matching
what `permissions.project_boq_is_group_locked()` now does. **Do it in the next session allowed to
touch `views.py`.** Better still, have that session delete the duplicate query and have the
predicate return the group, so there is one spelling rather than two.

---

#### UPDATED 30 Aug 2026 BY PROMPT 1.2a — STILL OPEN, BUT NO LONGER RESTING ON A COINCIDENCE

**B7 IS NOT CLOSED. The unnarrowed read is still there, unchanged**, and 1.2a was forbidden
`views.py` exactly as 1.1b was. What changed is the ground underneath it.

Everything above says this query is correct **by coincidence** — that no locked execution group
happens to exist, upheld by nothing but six filter terms spread across the views, "with the same
expiry date" as the pre-1.1b predicate. **That is now a database guarantee.** 1.2a added
`execution_groups_are_never_locked` to `SiteGroup.Meta.constraints`:

```sql
CHECK (NOT ("group_type" = 'execution' AND "status" = 'locked'))
```

**So the failure scenario two paragraphs up cannot occur.** It required "a site in both a locked
procurement group and a locked execution group"; the second of those is unwritable — by INSERT,
by UPDATE, and by `QuerySet.update()`, which a CHECK catches where the model's `save()` guards
by their own admission do not. There is no longer a state in which `.first()` has two rows to
choose between, and no expiry date on that.

**What is left is cosmetic, and it is worth saying which two things that means.** *(1)* The
duplicate query — two spellings of one question, which drift independently; the fix is still to
delete it and have `project_boq_is_group_locked()` return the group. *(2)* The missing
`group_type='procurement'` term, which is now **documentation of intent** rather than a
correctness fix: it makes the reader's guarantee local to the line instead of requiring them to
know a constraint exists three thousand lines away in `models.py`.

**Why it stays open rather than being downgraded and forgotten.** The constraint is what makes
this inert, and a future session that wants an execution lifecycle with a `locked` state will
have to drop that constraint to get it. If B7 has been closed by then, this read silently
becomes a live bug again the moment the constraint goes. Keep it open; whoever removes
`execution_groups_are_never_locked` must fix this line in the same edit. **The next session
allowed to touch `views.py` should still do it.**

---

### B8 — `task_status_update` and `task_detail_status_update` are two copies of one function

Found by prompt 1.4a's pre-flight, 30 Aug 2026. **`projects/views.py` was on that prompt's
forbidden list**, so this was enumerated rather than fixed. [views.py:3718](projects/views.py#L3718)
and [views.py:3998](projects/views.py#L3998).

The two are near-identical for roughly 180 lines each. Both parse `request.POST['status']`
against the same `valid_statuses`, apply the same allowed-transition table, build the same
`update_kwargs`, set and clear `blocked_since` on the same condition, write the status and its
`record_transition()` row inside the same deliberately tight `transaction.atomic()`, run the same
`_FINANCE_TASK_TO_MILESTONE` sync and the same `is_payment_milestone` branch. **They differ in
two places only**: how the actor is resolved (`request.user.profile` versus a `profile` already
bound by the caller), and what they render (an HTMX task row versus a task-detail redirect).

**Why this matters more than an ordinary duplication.** These are two of the four write paths
that change a `Task`'s status, and they are reached from two different screens that the same
person uses interchangeably. **Any rule added to one and not the other is not enforced — it is
merely avoidable**, by clicking through the other screen. 1.4b adds exactly such a rule: the
B-08 early-start warning and its mandatory reason. If it lands in one copy, the feature is
decorative.

**The fix** is the 0.2b shape — extract the shared body into one helper that takes the actor and
returns the outcome, leaving the two views owning only their gate and their rendering, the way
`_apply_boq_acknowledgement()` left `boq_acknowledge` and the inline branch owning theirs.
**That is a consolidation session, not 1.4b's job (R-12).** 1.4b must edit both copies and say
in its report that it did.

**Related, and counted honestly:** ⑤ `milestone_receive` and ⑥ `project_overview`'s Finance
`update_milestone` branch contain a **second** duplicated pair — the milestone→task sync block,
written out verbatim twice, both wrapped in `except Exception: pass`. Neither is a user starting
a task, so 1.4b's warning does not belong in them, and they are noted here only so the count of
task status-write paths is not later reported as four when it is six.

---

### B9 — the Django admin can change a task's status with no transition row

Found by prompt 1.4a's pre-flight, 30 Aug 2026. **`projects/admin.py` was outside that prompt's
MODE list**, so this was reported rather than fixed. [admin.py:142](projects/admin.py#L142):

```python
@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = ['task_name', 'phase', 'assigned_role', 'status', 'due_date', 'completed_at']
    list_filter  = ['assigned_role', 'status']
    search_fields = ['task_name']
```

There is no `readonly_fields`, no `fields` and no `exclude`, so **every field including `status`
is editable on the admin change form**, and `save_model()` intercepts only `assigned_to` — it
routes that one field through the assignment chokepoint and passes everything else to
`super().save_model()` untouched.

**What that costs.** An admin status change writes **no `StatusTransition` row**. It does not set
`completed_at` on a move to Done, does not set or clear `blocked_since`, does not run the
task↔milestone sync, and — once 1.4b ships — will not show the early-start warning or collect
the mandatory reason. `docs/execution-model.md` §13 currently lists `task` under **"Instrumented —
every status write goes through `record_transition()`"** and names four view functions. **That
claim is true of the views and not true of the product**, and §13's whole purpose is that a
missing row must mean something definite.

**Why it is not urgent.** The Django admin is reachable only by staff superusers, this is a
five-person operation, and nothing in the product's own screens routes through it. The rows it
writes are correct data; what is missing is the record of who changed them and why.

**The fix, and it is small.** Either add `'status'` to `TaskAdmin.readonly_fields` — the admin
stops being a status-write path at all, which is the honest answer given that `task_status_update`
exists and is the supported one — or extend `save_model()` to call `record_transition()` when
`'status' in form.changed_data`, alongside the `assigned_to` interception already there. **The
first is preferable**: an admin form that writes a status correctly is a second implementation of
the ladder, and D-3 and B8 above are both about not having two.

**Do it in the next session allowed to touch `admin.py`, and correct §13 in the same edit** — a
coverage table that overstates its coverage is worse than one that admits a gap.

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
