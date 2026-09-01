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

### ~~A3 — `_TASK_TO_PROFILE_ROLE` is still declared locally, twice~~ — **CLOSED 30 Aug 2026 by prompt K5**

**Closed.** Both local copies are gone. `_TASK_TO_PROFILE_ROLE` now sits at `views.py:435`,
immediately below the forward map and **derived from it by comprehension** rather than written
as a second literal — the same idiom `_MILESTONE_TO_FINANCE_TASK` uses thirteen lines above.
Two constants can drift; a derived one cannot. The rule is recorded as **R-19** in
`docs/execution-model.md` and enforced structurally by
`projects/tests_role_mapping.py::RoleMappingStructureTests`, which walks `Task.ROLE_CHOICES`
and asserts every value maps back to a real `UserProfile.ROLE_CHOICES` value.

**The line numbers below were stale by ~129 lines** — the actual sites were `views.py:4123`
and `views.py:7243`. Recorded because this is the third enumeration handed to a session in this
programme that did not survive contact with the file.

**Two findings K5 made while closing it**, neither of which A3 anticipated:

1. **Project Coordinator needed no map entry at all.** The map is differences-only, read through
   `.get(x, x)`; the profile and task strings for this role are byte-identical, so passthrough
   already resolved it. "Add the role to the mapping" was the obvious wrong fix.
2. **For a Project Coordinator the forward map is unreachable on the status path.**
   `user_can_view_project()` and `user_can_manage_project()` are the same predicate for this
   role, and the 0.2 view-scope lockdown runs before the role gate — so a coordinator off the
   project is refused at scope, and one on it already has `is_pm=True`. A3's stated risk
   ("decides whether a user may change a task's status…") is therefore true of BD and of any
   future role with a task-relation view branch, but **not** of Project Coordinator. See §4 of
   `docs/execution-model.md`.

The original entry follows.

---

### A3 (original) — `_TASK_TO_PROFILE_ROLE` is still declared locally, twice

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

### ~~B8 — `task_status_update` and `task_detail_status_update` are two copies of one function~~ — **CLOSED by prompt B8**

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

#### CLOSED 30 Aug 2026 BY PROMPT B8

Extracted as the entry recommended, in the `_apply_boq_acknowledgement()` shape:
`_apply_task_status_change(task, new_status, profile, request, project)` in
[projects/views.py](projects/views.py), called by both views. **R-18** records the rule in
`docs/execution-model.md` §3: *a new task-status rule is added to the helper, never to a view.*
`views.py` went from 11,417 lines to 11,288.

**This entry's "they differ in two places only" was wrong, and the correction is the substance
of what B8 found.** A mechanical diff of the two bodies turned up **eight** differences, not two.
Four are cosmetic or genuinely per-screen — the actor binding this entry named, the HTMX partial,
the redirect target, and a stray function-local `urlparse` import shadowing the module-level
`_urlparse`. **The other four are behavioural, and B8 preserved rather than resolved all four**,
because B8's remit was explicitly "no new rule, no behaviour change" and every non-preserve
answer changes behaviour on one screen. They are opened below as **B12–B15**, and each is pinned
by a test in `tests_task_status_path.DecidedDifferenceTests` so that "still differs" is a fact
the suite asserts rather than an omission nobody notices.

**⑤ and ⑥ were left exactly as they are, and the ledger gap this entry implied does not exist.**
Both write status through `filter().update()` and **both already call `record_transition()`** —
⑤ at [views.py:6569](projects/views.py#L6569), ⑥ at [views.py:7142](projects/views.py#L7142) —
each pre-reading the affected rows for their from-status and writing one ledger row per synced
task with `REASON_MILESTONE_SYNC`. Routing them through a helper that may raise, from inside
`except Exception: pass`, would convert a visible failure into a silent one. See **B16**.

Guarded by [projects/tests_task_status_path.py](projects/tests_task_status_path.py): 49 tests —
one contract mixin of 20 tests run through **both** entry points as `OverviewRowPathTests` and
`TaskDetailPathTests`, plus 9 pinning B12–B15. Verified adversarially: mutating the shared
transition table to permit Done → In Progress fails **two tests in each class**, symmetrically,
which is the property the module exists to guarantee.

---

### B12 — `task_detail_status_update` has no project-scope gate, and B8 preserved that

Found by prompt B8's pre-flight, 30 Aug 2026. Preserved deliberately (R-12) — closing it adds a
gate, which B8's MODE forbade. **Decided by the operator as "preserve and rectify later".**

[views.py:3718](projects/views.py#L3718) `task_status_update` opens with:

```python
if not user_can_view_project(request.user, project):
    raise Http404
```

added by the 0.2 lockdown **alongside** the role rule, because the role-matcher alone let any
Site Engineer move any Site Engineer task in the portfolio. `task_detail_status_update` has **no
equivalent**; its only gate is `task.assigned_to != profile → 403`.

**Mostly self-covering, and precisely where it is not.** `user_can_view_project`'s Site Engineer
and Design branches grant visibility to anyone *holding a task* on the project, so for those
roles `assigned_to == profile` already implies visibility. The exposure is the roles with **no
task-holding branch**: a PM or Project Coordinator personally assigned to a task on a project
they do not manage passes the detail path and is refused by the overview path. Narrow, but the
same class of hole 0.2 closed on the other screen.

**Before fixing**, confirm whether any legitimate workflow depends on a PM or Coordinator acting
on a task in a project they do not manage. If one does, the gate needs a task-holding branch
rather than a flat refusal. Pinned as-is by
`DecidedDifferenceTests.test_the_detail_path_has_no_project_scope_gate_and_lets_the_same_user_through`
— **that test should fail when this is closed**, and be replaced by its opposite.

---

### B13 — the two screens answer an unassigned task with different status codes

Found by prompt B8's pre-flight, 30 Aug 2026. Preserved (R-12).

Same condition, two answers. `task_status_update` has an explicit early branch: under HTMX a
flash message and a re-rendered row, otherwise
`JsonResponse({'success': False, 'error': …}, status=400)` — the only JSON response in either
view. `task_detail_status_update` folds `assigned_to is None` into its permission test and
returns a bare `HttpResponseForbidden`.

**403 is arguably the better answer, and B8 could not give it.** The 400 is pinned by
`tests_residential_baseline.TaskProgressionTests.test_an_unassigned_task_cannot_have_its_status_changed`,
one of the 92 characterisation tests, which asserts `status_code == 400`. Resolving this toward
403 means editing a characterisation test — out of scope for a consolidation session, and a
decision about what the API contract *is* rather than about tidiness.

---

### B14 — the two screens shape an HTMX permission refusal differently

Found by prompt B8's pre-flight, 30 Aug 2026. Preserved (R-12).

`task_status_update` under HTMX renders the task row carrying an error message (HTTP 200), so the
swap target receives valid HTML; only a non-HTMX request gets `HttpResponseForbidden`.
`task_detail_status_update` returns `HttpResponseForbidden` unconditionally — **an HTMX post from
the detail screen swaps a 403 body into the status block.** No test pinned either shape before
B8; both are pinned now.

The overview behaviour looks like the correct one. Changing the detail screen to match is a
visible behaviour change on a live screen and wants its own prompt.

---

### B15 — `?next=` is honoured on one screen and ignored on the other

Found by prompt B8's pre-flight, 30 Aug 2026. Preserved (R-12).

`task_status_update` honours a local `next` on success and on the missing-block-reason bail-out,
and ignores it on the other refusals — B8 kept that asymmetry exactly, and it is why
`_apply_task_status_change()` returns three outcomes rather than a bool.
`task_detail_status_update` never reads `next` and always returns to the task detail page.

Possibly correct as it stands — a detail screen has an obvious place to return to. Recorded
because it was preserved by default rather than decided.

---

### B16 — should ⑤ and ⑥ route their task sync through `_apply_task_status_change()`?

Raised by prompt B8, 30 Aug 2026. **Question, not a defect** — and explicitly not acted on.

⑤ `milestone_receive` and ⑥ `project_overview`'s `update_milestone` branch each flip a Finance
confirmation task to Done as an unattended sync. Both already write their `StatusTransition`
rows, so there is **no ledger gap**. The open question is whether they should share the helper.

**The argument against acting is stronger than it looks.** Both sit inside
`except Exception: pass`, chosen so a sync failure never blocks the milestone update. Routing
them through a helper that raises would convert a visible failure into a silent one — and worse,
the helper emits `messages.*` and skips the transition table, neither of which suits an
unattended sync where no user is watching and the milestone has already moved. A shared path
would also make the five features queued behind R-18 apply to machine syncs, which is not what
any of them means.

If this is ever revisited, the shape is a **second**, narrower helper for unattended syncs — not
a flag on this one.

---

### ~~B9 — the Django admin can change a task's status with no transition row~~ — **CLOSED by prompt B9**

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

#### CLOSED 30 Aug 2026 BY PROMPT B9

Fixed as the entry recommended, by the first option: `TaskAdmin.readonly_fields = ['status']`
([admin.py:159](projects/admin.py#L159)), with a comment above it naming R-10 and the reason,
because a field that is read-only for no visible reason is the kind of thing a maintainer
deletes. `status` stays in `list_display` and `list_filter` — those are read paths — and is
**not** in `list_editable`, which would have written past `readonly_fields` entirely.

`readonly_fields` alone was sufficient. `ModelAdmin.get_form()` adds the read-only names to the
form's `exclude`, so `status` is not a bound field on either the add or the change form: a POST
carrying `status` is ignored rather than merely un-rendered. Verified end-to-end through the real
admin URLs — `GET /admin/projects/task/add/` renders no `name="status"` input, and a POST to it
carrying `status=Done` creates the task at `Task.NOT_STARTED`, the model default.

Guarded by `AdminCannotWriteTaskStatusTests` in
[projects/tests_status_transition.py](projects/tests_status_transition.py) — four tests, all
reading the admin's *resolved* configuration off `admin.site._registry[Task]` and the form it
actually builds, so a restructure that moves `status` back into an editable position fails the
test rather than passing against a stale literal.

`docs/execution-model.md` §13 corrected in the same edit, and it keeps a note saying what the
old wording claimed. See **B10** below for the same question asked of the other five
instrumented subjects.

---

### ~~B10 — `ProjectAdmin` has the identical hole B9 just closed, and it was left open~~ — **CLOSED by prompt B10**

Found by prompt B9, 30 Aug 2026, while checking every subject in `StatusTransition`'s registry.
**B9's MODE allowed `admin.py` but scoped the fix to `Task` only**, so this is recorded rather
than fixed (R-12).

`utils._subject_type_registry()` ([utils.py:226](projects/utils.py#L226)) names six models. Their
admin exposure today:

| `subject_type` | Model | `ModelAdmin` | Status editable in the admin? |
|---|---|---|---|
| `project` | `Project` | `ProjectAdmin` — [admin.py:66](projects/admin.py#L66) | **YES** |
| `task` | `Task` | `TaskAdmin` — [admin.py:143](projects/admin.py#L143) | No — closed by B9 |
| `boq` | `BOQ` | *none* | No — not registered on `admin.site` |
| `delivery_challan` | `DeliveryChallan` | *none* | No — not registered on `admin.site` |
| `issue` | `Issue` | *none* | No — not registered on `admin.site` |
| `payment_milestone` | `PaymentMilestone` | *none* | No — not registered on `admin.site` |

**`ProjectAdmin` is exactly as exposed as `TaskAdmin` was, and this is not a flat filing.**
`status` sits in the `'Project Info'` fieldset ([admin.py:77](projects/admin.py#L77)) and
`readonly_fields` is `['project_id', 'created_at', 'activated_at', 'deleted_at']` — `status` is
not in it. There is no `save_model()` override at all on this class, so a status edit goes
straight to the column. It writes no `StatusTransition` row, and it is worse than the task case
was: moving a project to `Active` by hand skips `project_activate` entirely, so the phase and
task template is never attached, `activated_at` is never stamped, and the project sits Active and
empty. §13 lists **four** instrumented write sites for `project` — this is a fifth path that is
not one of them.

The other four models are safe only because nobody has registered them. That is an absence, not
a decision: `admin.register(BOQ)` added for shell verification in some later session silently
reopens the same hole, with no test anywhere that would notice.

**The same deploy-window argument that made B9 urgent applies here.** Once real users are on the
system, an admin project-status edit is an unreconstructable gap in the ledger — and, unlike the
task case, it also leaves the project itself in a state the product cannot produce.

**The fix is the same one line**: add `'status'` to `ProjectAdmin.readonly_fields`, with the same
R-10 comment, and extend `AdminCannotWriteTaskStatusTests` to loop over
`_subject_type_registry()` rather than naming `Task` — so a newly registered `BOQAdmin` fails the
test on the day it is written. **Do it in the next session allowed to touch `admin.py`, and
before the phase 1 deploy if one is available.**

#### CLOSED 30 Aug 2026 BY PROMPT B10

Fixed as the entry recommended and in the same shape as B9: `'status'` added to
`ProjectAdmin.readonly_fields` ([admin.py:94](projects/admin.py#L94)), under a
`DO NOT REMOVE — R-10` comment naming both reasons — the ledger gap, and the one this admin has
that `TaskAdmin` did not, that **activation is a view-layer action and the admin is not an
activation route**. `project_activate` is the only path that attaches the phase and task template
and stamps `activated_at`; an admin who could type 'Active' here produced an Active project with
zero phases and nothing raising. Losing that ability is the correct outcome, not a regression,
and the comment says so where a maintainer tempted to delete the line will read it.

`readonly_fields` alone was sufficient, verified against the resolved configuration rather than
by reading the class: `ProjectAdmin.list_editable` is `()`, there is no `fields`/`exclude`
override, and the two admin actions (`soft_delete_selected`, `restore_selected`) and
`delete_model()` touch only `is_deleted`/`deleted_at`. `Project.status` defaults to `'Draft'`,
and the admin add path is unaffected — a created project takes that default and still generates
its `project_id`.

`MilestoneInline` does expose an editable `Milestone.status` on this same admin. Left alone
deliberately: `Milestone` is the legacy model in §13's **NOT instrumented** table, superseded by
`PaymentMilestone` and never a subject type, so there is no ledger to write past.

Guarded by two test classes in
[projects/tests_status_transition.py](projects/tests_status_transition.py):

- `AdminCannotWriteProjectStatusTests` — the four B9-shaped tests, all reading
  `admin.site._registry[Project]` and the form it actually builds. The change-form test asserts
  the project stays `'Draft'` **and** that `activated_at` is null and no phases were attached,
  so the activation half of this finding is pinned too, not just the ledger half.
- `NoInstrumentedSubjectHasAnEditableAdminStatusTests` — the standing guard the entry asked for.
  It walks `utils._subject_type_registry()` itself and asserts, for every subject model that is
  registered on `admin.site`, that `status` is neither a bound field on the admin's form nor in
  `list_editable`. `BOQ`, `DeliveryChallan`, `Issue` and `PaymentMilestone` are unregistered and
  pass trivially; the day one is registered it fails and names the reason. A seventh subject type
  is covered the moment it enters the registry. Confirmed non-vacuous by registering `BOQ` at
  runtime in a scratch script and watching the assertion fire.

Verified end-to-end through the real admin URLs: `GET /admin/projects/project/add/` renders zero
form controls named `status` (and one named `city`, for contrast); a POST to it carrying
`status=Active` creates the project at `'Draft'` with `activated_at` null and no phases; and a
POST to the change form carrying `status=Commissioned` saves the row — `customer_name` changes —
with `status` still `'Draft'`. `docs/execution-model.md` §13 corrected in the same edit,
including the write-site count, with the previous wording struck rather than deleted.

**Found while verifying, not fixed here — see B11.**

---

### ~~B11 — the `ProjectAdmin` add and change pages have been raising `FieldError`, and `manage.py check` cannot see it~~ — **CLOSED by prompt B11**

Found by prompt B10, 30 Aug 2026, while driving the real admin URLs for its own verification.

[admin.py:21-24](projects/admin.py#L21):

```python
class DocumentInline(admin.TabularInline):
    model = ProjectDocument
    extra = 1
    fields = ['doc_type', 'title', 'file']
```

`ProjectDocument` has **none** of those three fields. Its actual columns are `file_name`,
`file_url`, `supabase_path`, `file_type`, `file_size_kb`, `uploaded_by`, `uploaded_at` and the
soft-delete pair ([models.py:434](projects/models.py#L434)). `DocumentInline` is on
`ProjectAdmin.inlines`, so **both** `/admin/projects/project/add/` and
`/admin/projects/project/<pk>/change/` raise

```
django.core.exceptions.FieldError: Unknown field(s) (doc_type, file, title) specified for ProjectDocument
```

— a 500, before any form is rendered. Reproduced on this branch and confirmed present at `HEAD`
(the line is untouched by B10). The changelist is fine; only the two form pages are dead.

**`manage.py check` reports no issues, and cannot.** Django does not validate names in a
`ModelAdmin`/`InlineModelAdmin` `fields` list against the model, because `fields` is allowed to
name fields contributed by a custom `ModelForm`. The mismatch is only discoverable by rendering
the page, which is why it has survived — nothing in the suite opens an admin project page.

**What it costs, and does not.** No data is at risk and no product screen is affected; the admin
project form is simply unreachable. It matters mostly because it is the kind of breakage that
makes an admin-side workaround look impossible during an incident, and because B10's own
end-to-end verification had to drop the inline at runtime to run at all.

**Not fixed here.** B10's task was `status`; this is an unrelated defect in a different class,
and inventing the right field list (`file_type`/`file_name`, plus whether an inline that cannot
upload to Supabase should exist on this admin at all) is a decision, not a rename (R-12).

**The fix is small but needs that decision.** Either correct `fields` to real columns — likely
`['file_type', 'file_name', 'file_url']` with `readonly_fields` for the Supabase-owned ones,
since the admin has no upload path — or drop `DocumentInline` from `ProjectAdmin.inlines`
entirely. **Whichever is chosen, add a test that GETs both admin project pages**, because that is
the only thing that would have caught this and the only thing that will catch the next one.

#### CLOSED 30 Aug 2026 BY PROMPT B11

Fixed as the entry's first option, and the entry's parenthetical was the right instinct: the
names do map one-to-one — `doc_type` → `file_type`, `title` → `file_name`, `file` → `file_url` —
but the **write path does not survive the correction**
([admin.py:21](projects/admin.py#L21)). `extra` is now `0`, all three fields are in
`readonly_fields`, `can_delete` is `False` and `has_add_permission()` returns `False`.

The reason is not tidiness. A `ProjectDocument` row is a *pointer* into a Supabase bucket, and the
two columns that make it resolvable are `file_url` and `supabase_path` — the latter being what
`purge_deleted_files` hands to `storage.remove()`
([purge_deleted_files.py:41](projects/management/commands/purge_deleted_files.py#L41)). A row
typed into an editable inline would name a file nobody uploaded and, with `supabase_path` left
empty, could never be purged either. The admin cannot put an object in the bucket, so it must not
create the row that claims one is there. That is the same rule `DesignFileAdmin` already states
for `bucket`/`path`, applied to the same kind of object. Upload and delete stay in the view
layer, which does both halves.

**No other admin or inline had the defect.** Established by measurement, not inference: the new
smoke test run against unfixed `HEAD` produced exactly one error — `(model='Project')`,
`FieldError: Unknown field(s) (title, doc_type, file) specified for ProjectDocument` — and passed
every other changelist and add form, `PhaseInline`, `MilestoneInline`, `ChecklistItemInline` and
`ChecklistTaskLinkInline` included.

**The durable half is the test the entry asked for**, generalised past the two pages that
prompted it: `EveryRegisteredAdminPageLoadsTests` in
[projects/tests_admin_smoke.py](projects/tests_admin_smoke.py) walks `admin.site._registry` —
the registry itself, never a tuple copied from it — and GETs every registered model's changelist
and add form, asserting 200 and naming the model and admin class on failure. No fixtures: a
changelist renders with zero rows and an add form with no object, and setup is the part of a
smoke test that rots. `NotificationLogAdmin` denies add outright, so its 403 proves nothing about
its field spec; for any such admin the test resolves `get_form()` and each inline's
`get_formset()` directly, so the one admin that forbids adding is not the one admin nothing
validates.

Two things the test needed that are worth recording. `solarpms.middleware.AdminAccessMiddleware`
gates `/admin/` on `UserProfile.role == 'Admin'`, **not** on `is_staff` — a superuser with any
other role is redirected, and the first run of this test read 302 on every page for that reason
alone. And it covers the **add** form, not the change form: a `fields` entry that resolved on add
and failed on change would slip past. Nothing in `projects/admin.py` builds fields conditionally
on `obj` today, and one valid instance per registered model — most behind required foreign keys —
costs more than that gap is worth.

**Confirmed non-vacuous**, in the shape B10 used. With `MilestoneAdmin.fields` broken to
`['due_dat']` at runtime in a scratch script, `manage.py check` still reported *"System check
identified no issues (0 silenced)"* — the mechanism, demonstrated rather than asserted — while
the smoke test errored at `(model='Milestone')` with
`FieldError: Unknown field(s) (due_dat) specified for Milestone. Check fields/fieldsets/exclude
attributes of class MilestoneAdmin.` Restored; nothing on disk was touched.

Verified end to end through the real admin URLs with **all three inlines in place** and a real row
in each: `GET /admin/projects/project/` → 200, `.../1/change/` → 200, `.../add/` → 200, with the
document's `file_name` rendering in the change form's read panel. `docs/execution-model.md` §13
records that `manage.py check` is not cover for admin field specs, and why it structurally cannot
be.

---

### B17 — the OPEX spec still assigned an unstorable role, and v1.2 did not catch it

Found by prompt 1.3a's pre-flight, 30 Aug 2026. **Reported before building, resolved by the
product owner, recorded because the same class of error survived a revision.**

`docs/OPEX_task_template_spec.md` v1.0 assigned two roles `Task.assigned_role` cannot store:
`—` on Punch Points and `PM / Coordinator` on Completion Certificates (Paperwork). The A-1.3
audit flagged **both**. v1.1/v1.2 fixed the first by **dropping Punch Points entirely**, and
the second **survived untouched into v1.2** — the revision resolved the finding by deleting
the row it happened to sit on rather than by acting on the finding.

Neither would have raised. `assigned_role` is `CharField(choices=…, default='PM')`, **not
null and with no blank choice**, and Django does not validate `choices` on `bulk_create` or
on a plain `create()` — an unstorable value becomes a **silent `PM`**. On Punch Points that
would have added a third PM mirror to `dashboard_pm`'s `pending_approvals`.

Resolved by adding `'Project Coordinator'` to `Task.ROLE_CHOICES` (see `docs/execution-model.md`
§12, 30 Aug). **The general lesson is the one worth keeping:** a spec's role column is
schema, and the only thing that catches a bad value is a test that asserts membership of an
allow-list — `tests_opex_template.test_every_assigned_role_is_a_storable_choice` is that
test, and it asserts against the five roles this template may use rather than against
`ROLE_CHOICES`, which the silent default would satisfy.

---

### B18 — the OPEX template's durations are all placeholders, and `compute_gantt_schedule` will read them

**REWRITTEN 31 Aug 2026 by prompt B18.** Found by 1.3a, 30 Aug. **The hazard this entry
originally described is closed. The wrong data it described is not, and that is what the
entry is now about.**

**What is closed.** Nothing writes the 22-day chain onto an OPEX site any more:

1. `opex_site_activate` does not call `calculate_due_dates()` (1.3c). An activated tender
   site starts with 22 NULL due dates.
2. `project_recalculate_dates` refuses `project_type != 'Residential'` (B18, this session).
3. `enable_cascade_scheduling` refuses it too, view and template both (B18, this session).

**The decision behind those three is recorded at `docs/execution-model.md` §16:
auto-scheduling is not in OPEX v1; dates are set manually, per task, by the PM.**

**Two corrections to what this entry used to say**, kept visible because the entry was
acted on twice and both readings mattered:

- **The *Recalculate dates* control is not on `project_overview`, and never was.** No
  template in the tree renders `project_recalculate_dates` — `grep` returns zero hits for
  any project type. Its exposure was the **view** accepting a direct POST, gated only on PM
  ownership, `status != 'Draft'` and `activated_at`. So the fix was view-side only, and
  `RecalculateControlIsNotRenderedTests` pins the absence so the claim cannot silently
  reverse.
- **The entry did not name the door that mattered.** `enable_cascade_scheduling` calls the
  same `calculate_due_dates()`, **does** render on `project_overview`, is **irreversible by
  design**, and once on makes `task_set_due_date` refuse every non-PM role owner outright —
  nine of the 22 OPEX tasks are the Site Engineer's. It would have deleted the only
  scheduling a tender site has, permanently. Latent only because
  `SystemSettings.cascade_scheduling_enabled` defaults `False`; a switch an Admin can flip
  is not a guarantee.

---

**WHAT IS STILL OPEN: the durations themselves.**

Durations are unset in OPEX v1 by decision (spec §5 — *"the team decides per task later"*),
so all 22 rows carry `duration_days`'s field default of **1**. That is a **placeholder
wearing the costume of a measurement**: nothing distinguishes "one day, decided" from "not
decided", and the column reads as authoritative to anything that consults it.

It is **inert while nothing reads it** — and today, for OPEX, nothing does.

**But `compute_gantt_schedule()` exists, and it will.** `projects/utils.py` computes
`(start, end)` per task in memory from a hybrid source: a task's end is its stored
`due_date` when set, **otherwise the computed chain end — previous end plus
`duration_days`**. It is precisely designed to render projects whose due dates are null,
which is every OPEX site. The Gantt is Residential-only today (`gantt_available`); the day
a tender Gantt is wired, all 95 sites render as 22-day bar charts anchored on
`activated_at`, with **no view to refuse the POST** because nothing is being written. The
three guards above stop dates being *written*. They do not stop the placeholder being
*read*.

**TRIGGER TO FIX: before anything reads `duration_days` on an OPEX task.** Not before the
next release, not on a schedule — before the first read. The candidates in order of
likelihood are a tender Gantt, any OPEX critical-path or float computation, and any
"expected completion" figure on a Program rollup.

**HOW IT IS FIXED: a template version bump, not an `UPDATE`.** Real durations from the
Tenders team, seeded as **OPEX v2** as a draft and then `activate()`d. R-7 forbids editing
an active version in place, and in-flight projects keep the durations they were built from
regardless (B-10). This is a data task with a code-shaped prerequisite (someone must ask
the Tenders team), which is why it has sat.

**What pins it.** `ActivatedOpexSiteStartsUnscheduledTests
.test_every_task_still_carries_the_placeholder_duration` asserts the whole duration set is
`{1}`. The day v2 lands with real numbers, that test fails and names §16 as the thing to
reconsider — so the arrival of good data is the event that reopens the decision, rather
than something anyone has to remember.

**The concrete dates the placeholder produces**, from `activated_at.date()`, kept from the
original entry because they are what a Gantt would draw. `add_calendar_days()` does not
skip weekends, so task *N* in template order falls at `activated_at + N` days:

| Task | Phase | Offset |
|---|---|---|
| Design | Design | **+1** |
| Net Metering Approval | Approvals (Pre-Installation) | +2 |
| CEIG Approval | Approvals (Pre-Installation) | +3 |
| Inspection — Factory / Vendor | Procurement & Delivery | +4 |
| Inspection — Post-Delivery / Unloading | Procurement & Delivery | +5 |
| Material Delivery | Procurement & Delivery | +6 |
| Civil Work and MMS Installation | Installation | +7 |
| Module Installation | Installation | +8 |
| LA and Earthing Installation | Installation | +9 |
| DC Cable Laying with Conduit | Installation | +10 |
| DCDB and ACDB Installation | Installation | +11 |
| Inverter Installation | Installation | +12 |
| AC Cable Laying | Installation | +13 |
| RMS Installation | Installation | +14 |
| Solar Generation Meter Installation | Installation | +15 |
| Testing & Commissioning | Testing & Commissioning | +16 |
| Net Meter Installation | Testing & Commissioning | +17 |
| Post-Installation Approvals | Approvals (Post-Installation) | +18 |
| COD | Closeout | +19 |
| Completion Certificates (Paperwork) | Closeout | +20 |
| As-Built Drawings | Closeout | +21 |
| **HOTO** | Closeout | **+22** |

---

**ALSO RECORDED HERE, found by B18's pre-flight: a due date can be set on a mirror.**

`task_set_due_date` has **no `is_mirror` gate** — B22's refusal lives on
`_apply_task_status_change()` and nowhere else, and `_task_row.html`'s PM branch renders an
editable date input for every row including mirrors. So a PM can date all five OPEX mirrors
(Design, Material Delivery, COD, As-Built Drawings, HOTO).

**Not built, deliberately, and not a stop condition.** A date on a mirror is **meaningless
rather than harmful**: a mirror is nobody's work and is already excluded from the overdue
counters (1.3b), so the date is never read as an obligation. It is also **not a one-line
addition to an existing gate** — there is no gate on that view to extend, only the two
`is_pm` branches, so a refusal means a new guard in both arms plus HTMX row-render handling
plus template suppression. That belongs with B22's family in one deliberate pass, not
bolted onto a scheduling session.

`ManualDueDatesOnMirrorsTests` pins the **current** behaviour, so a later session that
closes this gets a failure pointing at this paragraph rather than a mystery.

---

### ~~B19 — `attach_residential_template()` does not copy `is_mirror`, so every 1.3b exclusion is inert until 1.3c fixes it~~

**CLOSED 31 Aug 2026 by prompt 1.3c.** The seventh snapshot is copied.

`is_mirror=t.is_mirror` now sits between `is_payment_milestone` and `template_task` in the
one `bulk_create()`, which moved into `_attach_task_template()` — the single attach both
project types go through. There is no second copy of that loop for it to be missing from,
and that was the point of extracting rather than adding a sibling: a sibling would have
been a second place to forget it.

**Verified on a real attached site, not by a fixture.** A shell activation of an OPEX
project produces exactly five `Task` rows with `is_mirror=True` — **Design**, **Material
Delivery**, **COD**, **As-Built Drawings**, **HOTO** — and 17 without. The Residential
template flags nothing, so Residential activation produces zero mirrors and the 26 live
projects are unaffected.

**What this unlocks, which is the whole reason it mattered:** 1.3b's twelve counter
exclusions are no longer inert. `tests_opex_activation.CountersOnARealSiteTests` asserts
them on a site activated through the real view — `pending_approvals` 5 → 3, the project
card 22 → 17, the CEO `dept_pm_pending` / `dept_design_pending` / `dept_scm_pending` rows —
where every test in `tests_mirror_metrics.py` sets the flag by hand and could not.

**What is still NOT built, and is the reason B19's closure is not the end of the mirror
story:** the human-write refusal in `_apply_task_status_change()`. Recorded as **B22**
below.

---

### B20 — the CEO department block has no Project Coordinator row, so one OPEX task per site is counted by no department

Re-recorded by prompt 1.3b, 30 Aug 2026. Previously assigned to 1.3b by
`docs/execution-model.md` §4; **examined and deliberately not fixed.**

`_get_ceo_dashboard_context()` QUERY 2 builds six `dept_*` groups — PM, SCM, Design, BD,
Execution (SE), Finance. `Task.ROLE_CHOICES` has seven values since 1.3a. The OPEX template's
**Completion Certificates (Paperwork)** carries `assigned_role='Project Coordinator'`, so on
every OPEX site it lands in `task_total`, `task_unassigned` and the status counts, and in
**no department row**. The rollup under-covers the portfolio by one task per site — 95 rows
at full tender scale.

**Why 1.3b did not fix it, and why that is not evasion.** Completion Certificates is **not a
mirror**. 1.3b's exclusion has no bearing on it whatsoever; the two findings merely arrived in
the same audit. Closing the gap means *adding* a seventh department: three new conditional
counts in the aggregate, a seventh `dept_rows` entry, and a template row — a behaviour change
beyond that session's stated remit of "no behaviour change other than the exclusion itself".

**When it is fixed**, note that the six existing groups are hardcoded three times over
(aggregate keys, `dept_rows`, template). The honest fix derives all three from
`Task.ROLE_CHOICES`, in the shape R-19 established for the role map — otherwise the seventh
role is added in three places and the eighth is forgotten in one of them.

---

### ~~B21 — `current_phase` is computed four times, one copy is in `models.py`, and a stuck mirror pins all four~~ — **CLOSED by prompt B21**

**Closed 31 Aug 2026.** The four copies are one: **`utils.current_phase(project)`** (R-21), called by
`Project.get_current_phase()` — now a delegate returning `.phase_name` — and by all three dashboards.
The inline loop in `dashboard_bd` was **removed entirely** rather than kept as a fast path; it was
already reading the same prefetched data the helper reads, so there was no speed in it to keep, only
a second answer to the question. **Mirrors are excluded through `is_human_owned()`**, the existing
R-20 helper, not a second predicate. No migration, no template, no existing test module touched.

**The effect this was urgent for.** A freshly activated OPEX site now reads
**`Approvals (Pre-Installation)`**, not `Design`, on all four screens. Phase 1 holds exactly one task
— the `Design` mirror — so once mirrors are out, **Design can never be an OPEX site's current phase
at any point in its life**, which `OpexIsNotStuckOnDesignTests::test_03` walks the whole site forward
to prove.

---

#### FINDING — the four copies ALREADY DISAGREED, and it was a visible defect, not a latent one

This entry described four copies of one rule. They were not four copies of one rule. On a project
with every task Done:

| Copy | Answer, before B21 |
|---|---|
| `models.py` `Project.get_current_phase()` | `None` — the Admin project list printed **“—”** |
| `views.py` `dashboard_bd` | `None` |
| `views.py` `dashboard_pm` | the **last phase** (`order_by('-phase_order').first()`) |
| `views.py` `dashboard_site_engineer` | the **last phase**'s name |

**A completed Residential project therefore read “Finance Closure” on the PM dashboard and “—” on the
Admin project list, at the same moment, from the same data.** Nothing in production would have made
that legible — the two screens have different audiences — and it predates mirrors entirely. Reported
at pre-flight rather than absorbed into the refactor; **settled by decision on 31 Aug**: the current
phase is the **last phase HOLDING a human-owned task**. That equals the old PM/SE answer for both
templates shipping today, so the two most-used screens did not move.

Everything else about the four agreed and was verified rather than assumed: ordering (all four
`phase_order`, since `ProjectPhase.Meta.ordering` supplies it to the two that do not say so),
the not-Done predicate (`!= 'Done'` and `status__in=['Not Started','In Progress','Blocked']` are
extensionally identical **only because `STATUS_CHOICES` has exactly four values** — a fifth status
would have split them, and `AgreementTests::test_06` now pins the Blocked half), the empty-phase case,
and the absence of any task-type, role or soft-delete filter.

#### FINDING — the return type split is a template constraint in BOTH directions

`dashboard/pm.html` renders `{{ row.current_phase.phase_name }}`, so the PM context must hold a
**ProjectPhase object**; `admin/projects_list.html` renders `{{ project.get_current_phase }}` through
`|default:"—"`, so the model method must return a **string or None** (a `ProjectPhase` renders as
`"HRP-RES-2026-001 — Design"`). Templates were outside B21's MODE and both constraints hold in the
shipped shape: the helper returns the object, the model method takes `.phase_name` off it.

#### FINDING — `projects/templates/projects/project_list.html` is DEAD, and holds one of the two callers

Nothing renders it. `project_list` in `views.py` is a three-line redirect to
`admin_project_list` / `dashboard_ceo` / `dashboard_pm`, and no other view, include or `{% extends %}`
names the template. It contains the second `{% with cp=project.get_current_phase %}` block, which is
why the file looks live to a grep. **Not deleted by B21** — templates were outside its MODE, and a
dead template is not a defect, only a trap for the next person auditing callers of a method.
Whoever next has templates in scope should delete it.

#### FINDING — the three dashboards do not agree on how a context row identifies its project

`dashboard_pm` and `dashboard_bd` rows carry the fetched `Project` under `'project'` — the shape
`_apply_project_sections()` requires. `dashboard_site_engineer` rows carry a bare `'pk'` and
`'project_id'` and no object, so that dashboard cannot use `_apply_project_sections()` and does not.
Not a defect and not B21's to change; recorded because the new test module needed a two-branch
helper (`_row_pk`) to read the three of them, and the next person writing a cross-dashboard test
will hit the same thing.

#### NOT A DEFECT, but it is why the queryset form was rejected

`dashboard_pm` / `dashboard_site_engineer` joined `phases.filter(tasks__status__in=[...])` without
`.distinct()`, so a phase with three open tasks produced three duplicate rows. Correct as written —
`.first()` on the ordered result is still the right phase — and it disappears with the loop form.

---

### ~~B22 — the human-write refusal on a mirror is still not built, and a mirror is protected only by having no assignee~~ — **CLOSED by prompt B22**

**Closed 31 Aug 2026.** `_apply_task_status_change()` now refuses a mirror as its **first
statement**, above the transition table and above the inline `due_date` write, returning
the existing `_TASK_STATUS_REFUSED` — no fourth outcome, no per-screen wording, and
nothing written on a refusal: no `StatusTransition`, no `ActivityLog`, no notification.
One check, in the one place, per R-18; **neither caller was edited**, and no template,
model, migration or existing test module was touched.

The message names the rule rather than a permission, deliberately:

> `'COD' is a mirror task — its status is derived from the workspace that owns the work and cannot be set here. It will update itself when that record changes.`

**The trap this entry warned about was real, and the new module is built around it.**
`projects/tests_mirror_readonly.py` — 24 tests, contract half run through **both** entry
points, on a **really activated** OPEX site rather than hand-made rows. Every test assigns
the mirror to the acting PM first, and `_assign_mirror()` asserts the assignment landed,
so the module cannot quietly regress into testing the unassigned gate. `TheTrapTests` pins
the distinction from the other side. Verified negatively as well: with the new `if`
neutralised, 34 assertions across both entry points fail; with it in place, 921 tests pass
against the same one pre-existing failure and one collection error as baseline.

**Note the module name.** This entry, quoting the A-1.3 audit, placed the test in
`tests_task_status_path.py`; that file is an existing module and prompt B22's MODE
forbade editing one, so the tests are a new module instead. Nothing is lost — the two
modules use the same contract-mixin shape for the same reason.

**Two findings came out of the pre-flight and are open below as B25 and B26.** Neither is
a hole in this refusal. **Neither `milestone_receive` nor `project_overview`'s Finance
sync can reach a mirror** — checked, because if either could, "read-only" would have meant
something weaker than it now does: both select by task **name** from a three-entry map
(`Advance Payment Confirmation`, `Pre Dispatch Payment Confirmation`, `100% Payment
Confirmation`), none of the five mirror names is in it, the Residential template contains
no mirror at all, and both are keyed off a `PaymentMilestone` row that an OPEX site never
gets. B26 is the one way that could stop being true.

---

### B22 (original) — the human-write refusal on a mirror is still not built, and a mirror is protected only by having no assignee

Found by prompt 1.3c's pre-flight, 31 Aug 2026. **Promised to 1.3c by three earlier
documents and NOT delivered by it**, because 1.3c's remit was the opening transition, not
the status path. Recorded here rather than left in the earlier promises so it is not
assumed done.

`models.py`'s `Task.is_mirror` comment, migration `0075`'s header and
`tests_opex_template.py`'s docstring all say the refusal lives in
`_apply_task_status_change()` (R-18) and is 1.3c's. It is not there. Grepped: nothing in
`views.py` reads `is_mirror` on a write path — all 30-odd references are counter
querysets from 1.3b.

**What actually stops a human writing a mirror today, and why it is not good enough.**
Both status views refuse an unassigned task *before* `_apply_task_status_change()` runs:

```python
    if task.assigned_to is None:                              # task_status_update
    if task.assigned_to is None or task.assigned_to != profile:  # task_detail_status_update
```

and 1.3c seeds every mirror with `assigned_to = NULL` (see §12, 31 Aug). So a mirror
cannot be moved — **by accident, not by rule.** The day anybody assigns COD to the PM
through `task_assign`, which nothing prevents, the protection disappears silently and
that mirror becomes writable like any other task.

**The trap for whoever builds it**, already flagged by the A-1.3 audit and worth
restating: a refusal test written against a mirror as seeded will pass without proving
anything, because the unassigned check fires first. The test must assign the mirror to
the acting user and *then* assert the refusal, or it is testing the wrong branch.

**Where it goes:** `_apply_task_status_change()`, not either view — a rule added to one
view is not enforced, merely avoidable. `tests_task_status_path.py` is where the audit
placed the test.

---

### ~~B23 — the PM dashboard's draft card still opens the designer modal, so an OPEX site cannot be activated from there~~ — **CLOSED by prompt B18/B23/B26**

**Closed 31 Aug 2026.** The four lines this entry specified, applied verbatim:
`{% if dp.project_type == 'Residential' %}` around the existing button, a plain POST form
to `opex_site_activate` in the else branch. **No JS was copied** — the else branch needs
none, as the entry said. Nothing else on that page changed.

The two surfaces are now pinned **against each other** rather than against a literal:
`PmDashboardDraftCardActivatesOpexTests.test_the_dashboard_and_the_overview_agree_on_both
_project_types` renders both `dashboard/pm.html` and `project_overview.html` for the same
two drafts and asserts that whichever activation URL one offers, the other offers too —
so a future divergence fails on either side, not only on the one a test happened to name.
Verified negatively: with the branch collapsed to `{% if True %}`, three tests fail.

**One correction to this entry's line references.** The file is
`projects/templates/dashboard/pm.html`, not `projects/templates/projects/dashboard/pm.html`;
the button was at line 84, not 88, and the modal at 459, not 470. The finding itself was
exactly right.

---

### B23 (original) — the PM dashboard's draft card still opens the designer modal, so an OPEX site cannot be activated from there

Found by prompt 1.3c, 31 Aug 2026. **Deliberately not fixed** — 1.3c's stop conditions
allowed one control in one existing template, and this is a second.

1.3c branched the Activate control on `project_overview.html`: Residential keeps the
`#activateDesignerModal`, everything else posts to `opex_site_activate`. That is the
screen `program_detail.html` links every OPEX site to, so the path a tender PM actually
walks works.

`dashboard/pm.html:88` carries **the same button with the same `data-activate-url`** and
the same modal at line 470, unbranched. A PM whose draft OPEX site appears on their
dashboard's draft card gets the designer picker and the same dead end 1.3c removed
elsewhere — the `<select required>` will not submit, and there is no designer to pick.

**Not a correctness bug** — nothing wrong is written, the activation simply cannot be
started from that one card. **The fix is the same four lines** already applied to
`project_overview.html` (`{% if project.project_type == 'Residential' %}` around the
button, a plain POST form in the else branch), and it belongs in whatever session next
opens the PM dashboard. Do not copy the JS; the else branch needs none.

---

### B24 — `opex_site_activate` is one view, and 91 sites is not one POST

Found by prompt 1.3c, 31 Aug 2026. **Not a defect — a stated limit, recorded so the gap
is not discovered by someone clicking 91 times.**

1.3c ships the per-site transition a PM triggers from a screen. The production reality
behind it is **95 Draft tender sites**, and the A-1.3 audit costed moving them at
**≈ 3,040 queries, 2,755 `Task` rows and 760 `ProjectPhase` rows**. That is a management
command or a data migration, not 95 POSTs through a browser.

**What such a route must carry, and this is the load-bearing part:** its own idempotency
guard. `opex_site_activate`'s double-run protection is `if project.status != 'Draft'`
*inside the view*, and it **does not travel**. There is no uniqueness constraint on
`(project, phase_order)`, so a bulk route without its own check silently double-attaches —
14 phases, 44 tasks — and `_phase_progress_subqueries()` hides it by taking the lowest-pk
phase.

**Also note what activation does to two portfolio-wide surfaces**, per §12's 30 Aug
ordering decision: `activated_at` *is* the definition of "active" for the CEO per-user
report and the EOD digest, so a bulk run adds 95 sites to both on the day it runs. 1.3b's
exclusions are now live, so the mirrors will not inflate the counts — but the sites
themselves will appear, and that should be expected rather than investigated.

---

### B25 — four paths can assign a mirror to a person who cannot act on it, and one of them does it in bulk with no intent

Found by prompt B22's pre-flight, 31 Aug 2026. **Reported, not fixed** — B22's MODE was
`_apply_task_status_change()` and nothing else, and every path below is a different
function.

`Task.assigned_to` is written in exactly two functions, `assign_task_to()` and
`assign_tasks_to()` in `utils.py` (the chokepoint, `utils.py:173`). Neither looks at
`is_mirror`. Callers that can therefore land a mirror on a person:

| Path | Shape |
|---|---|
| `views.py:task_assign` | PM or Coordinator, one task. Candidates filtered by **role only**. This is the exact scenario B22 described — assign COD to the PM to get it onto a dashboard. |
| `views.py:task_assign_design_head` | Design Head, any Design-role task — which includes **both** Design mirrors, `Design` and `As-Built Drawings`. |
| `views.py:project_overview`, `assign_design` bulk (and its clear branch) | `filter(assigned_role=Task.DESIGN, status__in=['Not Started','In Progress'])` with **no `is_mirror=False`**. On an OPEX site one click assigns both Design mirrors to the design lead and logs *"Assigned Design lead X to N tasks"* with an inflated N. |
| `admin.py:TaskAdmin.save_model` | Any task, any assignee. |

**Since B22 this is an inconsistency, not a hole** — the status write is refused either
way. What makes it worth an entry is the third row: it needs **no intent at all**, and it
is the exact counterpart of the `is_mirror=False` filter 1.3c deliberately added to the
OPEX attach's PM pre-assignment (`utils.py:1412`) for precisely this reason. The two
disagree today.

**The shape of the fix, if one is wanted: one refusal in the chokepoint**, not four in
four views — same argument as R-18. Whether assigning a mirror should *refuse* or merely
be *filtered out of the candidate set* is a product question, and is why this is recorded
rather than guessed. `tests_mirror_readonly.TheTrapTests::`
`test_assigning_a_mirror_is_still_permitted_and_still_pointless` pins today's behaviour
and says in its own docstring that it should be replaced by its opposite when this closes.

---

### ~~B26 — `TaskAdmin` leaves `is_mirror` editable, which is the one way a Finance sync could reach a mirror~~ — **CLOSED by prompt B18/B23/B26**

**Closed 31 Aug 2026.** `is_mirror` joins `status` in `TaskAdmin.readonly_fields`, with
B9's `DO NOT REMOVE — R-10` comment **extended rather than duplicated**: the line now
carries two rules and says so, because a maintainer meeting one field's justification and
not the other's is how half a guard gets deleted.

**What it protects, stated plainly.** B22 proved that neither Finance sync can reach a
mirror, for three independent reasons, and concluded that *read-only* means what it says.
**That proof held by the coincidence that nobody had ticked the box.** It now holds by
configuration. The reasoning is recorded on the field and in `docs/execution-model.md` §13:
a `Task`'s mirror flag is a **snapshot** `_attach_task_template()` copies from
`TaskTemplateTask.is_mirror` at activation, and setting it by hand creates a row **the
derivation hooks will never write and the sync paths do not exclude** — the M3 sync reaching
it through `filter().update()`, outside R-18 by B16's decision.

**Confirmed from the resolved configuration, not the class body**, the pattern B10 and B11
both used. Before: `readonly_fields=['status']`, `fields=None`, `exclude=None`,
`list_editable=()`, `fieldsets=None`, and `is_mirror` **a bound field on both the change and
add forms**. After: `readonly_fields=['status', 'is_mirror']` and `is_mirror` bound on
neither. `AdminCannotWriteTaskMirrorFlagTests` asserts more than the absence — a POST
carrying `is_mirror=on` through the form the admin actually builds is **ignored, not merely
unrendered**, and an existing mirror **keeps** its flag through an admin edit rather than
being reset to the default.

**The tests live beside B9's in `projects/tests_status_transition.py` §8b**, not in a new
module and not in `tests_admin_smoke.py`. Same `readonly_fields` line, same class, same
mechanism, same failure mode — a maintainer removing that line should meet both guards at
once. `tests_admin_smoke.py` was the other candidate and is the wrong home: it is a
fixture-free sweep over `admin.site._registry` asserting only that every page returns 200,
and a field-specific rule would break that character.

**`is_mirror` IS still editable on one other `ModelAdmin`, and that is deliberate.** Read
off the registry at runtime, exactly two registered models carry the field:

| `ModelAdmin` | `is_mirror` editable? | State |
|---|---|---|
| `TaskAdmin` | No | Closed here. |
| `TaskTemplateTaskAdmin` | **Yes** (`readonly_fields = ()`) | **Left alone, by decision.** Setting the flag on a *template* row is template authoring, which is what that admin is for, and R-7's draft/active gate already governs when it may happen. Editing a `Task`'s flag is not the same question. |

**This entry's one deferred suggestion is NOT done.** It proposed `template_task` deserved
the same treatment — it is provenance (B-10) and nothing should retype it. That was outside
this session's remit (the prompt scoped `is_mirror` only) and remains **open**: a superuser
can still repoint a `Task` at a different template row. It is inert today because nothing
resolves behaviour through that FK, which is itself a B-10 decision that could change.

---

### B26 (original) — `TaskAdmin` leaves `is_mirror` editable, which is the one way a Finance sync could reach a mirror

Found by prompt B22's pre-flight, 31 Aug 2026. **Reported, not fixed** — `admin.py` was
outside that prompt's MODE. **Theoretical and superuser-only**, recorded because the
argument that currently closes it is not visible from either file.

B22 established that neither ⑤ `milestone_receive` nor ⑥ `project_overview`'s Finance
sync can write a mirror: both select tasks by **name** from `_MILESTONE_TO_FINANCE_TASK`,
whose three values are Residential Finance-confirmation task names, and the Residential
template contains no mirror. That argument holds **only while no Residential task carries
the flag.**

`TaskAdmin` sets `readonly_fields = ['status']` (B9) but declares no `fields` and no
`exclude`, so **every other column is editable on the change form, including
`is_mirror`**. A superuser can tick it on `100% Payment Confirmation` for a Residential
project. From that moment the M3 sync writes a task the helper calls read-only — through
`filter().update()`, which is outside R-18 by decision (**B16**) and cannot be made to
consult the refusal without the second, narrower helper B16 describes.

**Why the flag is a worse admin field than `status` was.** `status` is one row's current
value; `is_mirror` is a claim about **who owns that row's truth**, and the template is
supposed to be its only source (R-7: content is versioned template data, and instances
take copies). An admin tick manufactures a mirror with no source object and no template
row saying so — a state `attach_opex_template()` cannot produce.

**Fix, when `admin.py` is next open:** `is_mirror` joins `status` in `readonly_fields`,
with the same DO-NOT-REMOVE comment. `template_task` deserves the same look for the same
reason — it is provenance (B-10) and nothing should be able to retype it.

---

### B27 — the four delivery mirrors need SCM's catalogue mapping **and** B-18, and neither alone is sufficient

Found and recorded by prompt 1.5, 1 Sep 2026. **Not a defect — a dependency**, written as
one entry because splitting it into two would let somebody close half of it and believe
the mirrors could then derive.

Spec v1.4 split `Material Delivery` into four mirrors — `Delivery — Solar Panels`,
`Delivery — Inverters`, `Delivery — BOS Kit`, `Delivery — MMS` — because material arrival
is what a PM looks at first and one undifferentiated row does not say whether panels have
landed or only cable. All four read **Not Started permanently** today, and will until BOTH
of the following land:

1. **B-18 — the join key does not exist.** `DCLineItem` carries `boq_category` as a plain
   string and has no FK to `BOQItem`, so there is nothing to join accepted quantity to
   site BOQ quantity on. Without it the derivation rule (Not Started = none accepted ·
   In Progress = some, below BOQ · Done = accepted ≥ BOQ, damaged excluded) has no
   left-hand side. Tracked separately as **B18** for the durations question; this is the
   other half of the same gap.

2. **SCM's catalogue mapping does not exist, and the split made it BIGGER.** Of the 207
   OPEX catalogue rows (migration 0057, a frozen literal that asserts its own length),
   these four buckets match `Module`, `Inverter`, `BOS` and `MMS` — **52 rows. 155 map to
   nothing.** The figure of 120 that circulated until v1.5 was v1.2's, for a different set
   of six buckets, under which 84 map and 123 do not. No grouping has a category named RMS.

**Neither is sufficient alone.** B-18 without the mapping gives a join that resolves 52 of
207 items and silently reports three of the four mirrors Done on a site whose panels have
not arrived. The mapping without B-18 gives a correct bucket list and no way to read a
delivery against it. **Do not ship a derivation hook until both are in place** — a mirror
that can disagree with its source is the failure the whole mirror design exists to prevent.

**A consequence worth stating separately.** Removing the two inspections (spec v1.4 — an
inspection at a vendor's works covers a consignment, not a site) took away SCM's only
**entered** tasks. SCM now owns four mirrors and nothing else on an OPEX site, the position
Design was already in with `Design` and `As-Built Drawings`. So **no SCM or Design person
has a single actionable OPEX task today**, and none of their six mirrors can move until
the two dependencies above land. That is the specced behaviour, not a bug — but it means
an SCM user opening an OPEX site sees six rows they cannot touch and nothing they can, and
whoever demos this should say so before someone else notices.

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

---

### B23 — six things the demo seed could not build through any product code path

Found by prompt DEMO-1, whose remit was tooling and which shipped **no product change**.
Building a populated local environment is an unusually direct audit of "can this state be
reached through the product at all", because a seed that must reach for `objects.create()`
has found a state the product cannot produce. Six did.

Each is marked `# NO PRODUCT PATH` at its call site in
`projects/management/commands/seed_opex_test_data.py`, and listed in `docs/demo-data.md` so
nobody reads demo data as evidence a workflow works.

**1. `StockLocation` has no writer at all.** No view, no form, no `admin.site.register`. A
warehouse can only be created from a shell or a migration. Deliberate as of 1.2a —
`tests_capability_flags.py` calls it "one table with no writer" and its consumer arrives with
4.1 — but it is worth stating that the table shipped and remains unreachable.

**2. The three execution capability flags have no writer either.** `is_qaqc`, `is_hse` and
`is_warehouse_keeper` are absent from `UserCreateForm`, from `UserEditForm`, and — the part
that surprises — from `UserProfileAdmin.list_display` and `list_filter`, which do carry
`is_design_head` and `is_design_qc`. So unlike the other two profile flags, these cannot be
set even by an Admin through Django admin. Consumers arrive with 2.2 / 2.3 / 4.1 (R-12), but
whichever lands first needs a writer, and adding the three to `UserProfileAdmin` is a
one-line change that would make them settable today.

**3. A `group_type='execution'` `SiteGroup` cannot be created by anybody.**
`site_group_create` hardcodes `GROUP_TYPE_PROCUREMENT` and its own comment says the execution
creator "will sit beside this one" when written. Until it is, D-1 is unreachable through the
product: the column exists, the CHECK constraint
`execution_groups_are_never_locked` guards it, `SiteGroupMembership.group_type` denormalises
it, and no user can produce a single row with that value. The demo seed makes one directly so
the state is at least visible on screen.

**4. `DeliveryChallan` and `DCLineItem` have no extracted creation service.** Creation is
inline in `delivery_challan_create` (`views.py` ~9507), including its `record_transition` and
the deliberate absence of a `recalculate_dc_status()` call. Compare `create_opex_site()`,
which was extracted precisely so a non-request caller could use it. Not a defect; a gap that
makes the challan path untestable and unseedable without copying twenty lines.

**5. Neither activation has an extracted core.** `opex_site_activate` and `project_activate`
are views: the status write, `activated_at`, the ledger row and the template attach are
inline. `attach_opex_template()` / `attach_residential_template()` and `record_transition()`
are importable, so what the seed replicates is three field writes — but "activate this site"
is not callable from anywhere that is not an HTTP request. Any future BULK activation route
(and 91 tender sites are waiting for one) will either extract this or copy it.

**6. Task status changes require a `request`.** `_apply_task_status_change()` is the one
decision path for task status (R-18) and correctly so, but it reads `request.POST` for the
block reason and the inline due date and writes `messages`, so no command, job or derivation
hook can call it. That matters beyond seeding: the mirror **derivation** hooks of phases 3–5
will write mirror statuses from source events, and the docstring already says they "will not
call this function". So the rule set that lives in `_apply_task_status_change` — the
transition table, `completed_at`, `blocked_since` — has no non-HTTP home, and the derivation
hooks will have to restate it or diverge from it.

**Also recorded, not a finding:** `UserCreateForm.clean()` refuses a second Admin account, so
the demo set is **seven roles, not eight**. That is a real product rule working as intended;
the seed does not bypass it, and `docs/demo-data.md` tells the operator to log in as the
existing Admin. Noted here only so a later reader does not file the missing demo Admin as an
omission.
