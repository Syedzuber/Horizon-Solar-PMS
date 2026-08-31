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

### B18 — the OPEX template's durations are all 1, and that makes every site a 22-day project

Found by prompt 1.3a, 30 Aug 2026. **Not a defect today. It becomes one the moment 1.3c
calls `calculate_due_dates()` for an OPEX site.**

Durations are unset in OPEX v1 by decision (spec §5 — *"the team decides per task later"*),
so all 22 rows carry `duration_days`'s field default of **1**. All 22 are also `Internal`,
and `calculate_due_dates()` chains Internal tasks strictly sequentially off `activated_at`:

```python
task.due_date = add_calendar_days(previous_internal_due, task.duration_days)
previous_internal_due = task.due_date
```

So the last task, **HOTO, falls due `activated_at + 22 calendar days`**, and the whole site
reads as a 22-day project. Across the 95 tender sites that would put the entire OPEX
portfolio overdue within a month of activation — and the likely reaction to that is to
distrust the overdue number, not to fix the durations.

**STILL OPEN after 1.3c, 31 Aug 2026 — and now open on a live path rather than a
hypothetical one.** 1.3c took the second option: `opex_site_activate` does **not** call
`calculate_due_dates()`, so all 22 tasks are created with `due_date = NULL` and an
activated OPEX site is unscheduled rather than wrongly scheduled. A null due date says
"not scheduled"; 22 sequential days says something specific and false.

**What changed is the exposure.** The PM-facing **Recalculate dates** control
(`project_recalculate_dates`) is on `project_overview`, is gated only on `status != 'Draft'`
and `activated_at`, and is now reachable on every activated OPEX site. One press produces
the table below. Nothing warns the PM.

**The concrete dates, from `activated_at.date()`.** All 22 tasks are `Internal` with
`duration_days = 1`, and `add_calendar_days()` does not skip weekends, so task *N* in
template order falls due `activated_at + N` days:

| Task | Phase | Due |
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

A site activated on **31 Aug 2026** and recalculated the same day would show HOTO due
**22 Sep 2026**, and would be entirely overdue by early October.

**What closes this: the Tenders team supplying real durations.** Not a code change, and
not something a session should invent — `tests_opex_activation.NoResidentialMilestonesTests
.test_due_dates_are_left_null` pins the current behaviour so nobody wires the recalc in by
accident before the numbers exist.

Fixing it later is a template **version bump** (v2 as a draft, then `activate()`), not an
`UPDATE` — R-7 forbids editing an active version in place, and in-flight projects keep the
durations they were built from regardless (B-10).

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

### B21 — `current_phase` is computed four times, one copy is in `models.py`, and a stuck mirror pins all four

Found by prompt 1.3b, 30 Aug 2026. **Deferred by decision, not by oversight** (30 Aug).

"First phase holding a not-Done task" exists in four independent copies:

| Site | Shape |
|---|---|
| `views.py` `dashboard_pm` | `project.phases.filter(tasks__status__in=[...])` |
| `views.py` `dashboard_site_engineer` | the same queryset |
| `views.py` `dashboard_bd` | an **inline Python loop** over prefetched phases |
| `models.py` `Project.get_current_phase()` | an inline Python loop |

A mirror that never completes — and COD, HOTO and As-Built have no source object in existence
to complete them — pins a site at its earliest mirror-bearing phase permanently, on all four.

1.3b left every one of them alone. `models.py` is outside its MODE, and fixing three of four
is worse than fixing none: the copies would then disagree with each other about what phase a
project is in, which is harder to diagnose than a consistently wrong answer. **The fix is the
consolidation, not the filter** — one helper, called by all four, with R-20's exclusion inside
it. Whoever does it needs `models.py` in scope.

---

### B22 — the human-write refusal on a mirror is still not built, and a mirror is protected only by having no assignee

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

### B23 — the PM dashboard's draft card still opens the designer modal, so an OPEX site cannot be activated from there

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
