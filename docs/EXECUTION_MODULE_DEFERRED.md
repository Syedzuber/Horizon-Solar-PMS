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

**Standing note on counts (added by the D18 session, 17 Sep 2026).** `solarpms_local` is a
restored production dump PLUS seeded data: the SCMPILOT tender and the `demo.*` users are
local, and the dump is older than production. A pre-flight count taken there must exclude
SCMPILOT sites and `demo.*` users and say "local, stale", or be checked in production. A site
named in an entry from the local dump is evidence of an exposure, not of a production case.

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

### B28 — twelve `PaymentMilestone` rows exist on non-Residential projects, and spec §2a says they cannot

Found and recorded by prompt 1.6, 1 Sep 2026. **Not urgent and not a deploy blocker — it is
twelve rows. The unexplained mechanism matters more than the rows do.**

**The count, and how it moved twice.** The A-1.3 audit projected that activating 95 OPEX
sites through `project_activate` *would* mint **285** `PaymentMilestone` rows — M1 / M2 / M3
per site, a three-milestone residential contract on a tender. That projection was then
repeated as if it were a **count** of rows already in production, into spec v1.4 §5 (which
decided to "leave them alone") and into `opex_site_activate`'s own docstring. Spec **v1.5
§2a** corrected the number to **zero** and reasoned it out: both creation paths require an
activated project plus a PM action, and production has no active projects, so no such row
can exist.

**The query, run against production on 01 Sep 2026:**

```sql
SELECT COUNT(*) FROM projects_paymentmilestone m
  JOIN projects_project p ON p.id = m.project_id
 WHERE p.project_type != 'Residential';
-- 12
```

**So §2a's reasoning is sound and its conclusion is empirically false**, which means one of
its premises is wrong. Twelve rows exist and something made them. Nobody currently knows
what. The candidates, none verified:

- a project that *was* activated and later moved back to `Draft` or was soft-deleted —
  `is_deleted` and `status` are independent, and `project_delete` only sets the flag
  (see the soft-delete note in the memory index);
- a `project_type` changed **after** a Residential activation, which would move existing
  M1/M2/M3 rows onto a non-Residential project without any milestone path running;
- direct creation through the Django admin or a shell, which no product-code audit reaches;
- a data import or restore predating the current constraints.

**Why it matters more than twelve rows.** §2a is currently used as evidence that a whole
class of bad data does not exist. If the premise it rests on is wrong, the same reasoning
is wrong wherever else it appears — and "both creation paths require an activated project"
is exactly the kind of claim later sessions build guards on top of.

**What 1.6 did about it.** Corrected the *figure* everywhere it could reach: `views.py`'s
`opex_site_activate` docstring and `tests_opex_activation.py`'s one comment line both now
say **12**, with the provenance. It did **not** touch `docs/OPEX_task_template_spec.md`
(outside that session's MODE) — **§2a still says "No such rows exist", and is wrong.** Nor
`OPEX_TEMPLATE_AUDIT.md`, deliberately: that document should keep its own record of what
it projected.

**What closing this looks like:** identify which of the twelve projects hold the rows and
what their `status` / `is_deleted` / `activated_at` say, decide whether the rows are
harmful (they are M1/M2/M3 on a tender, so almost certainly meaningless rather than
dangerous), and correct spec §2a to state the count and the mechanism rather than a
negative.

---

### ~~B29 — one 1.3b-era test pins the rule 1.6 replaced, and 1.6's MODE could not edit it~~ — **CLOSED by prompt 1.6**

Found by prompt 1.6, 1 Sep 2026, reported as that session's one stop condition, and
**closed in the same session** once the product owner lifted MODE for the single method.
Recorded rather than deleted because the reasoning is what a later reader needs: this is
what it looks like when a correct test pins a rule that has since been replaced.

`tests_opex_activation.CountersOnARealSiteTests.test_the_project_card_counts_exclude_all_eight_mirrors`
asserts `card['total_tasks'] == 15` on `dashboard_pm`'s OPEX project card. That number is
**23** under R-20's PROGRESS half — the card asks "how much of this site is done", and 1.6
moved it accordingly. The test is not wrong about the code; it is a correct pin of the rule
that was replaced, and it now reads `AssertionError: 23 != 15`.

`tests_opex_activation.py` is outside 1.6's MODE (every existing test module is), and the
only two resolutions are to edit that test or to revert the decision.

**The class does not lose its proof if the test is retargeted.** Its docstring calls this
"counter 4 … a genuine third proof, substituted for the report above", because the card
counts are assignment-blind and so were the strongest available evidence that the exclusion
fired. Counters 1 and 2 in the same class — the CEO `dept_rows` for Design and SCM — are
**WORKLOAD** numbers, still excluded, and still prove exactly that. What the card now proves
is the other half of the rule, which is worth asserting under a name that says so.

**What was done.** The method was renamed to
`test_the_project_card_counts_every_task_including_mirrors` and now asserts **23**, with a
docstring recording that it asserted 15 from 1.3b until 1.6 and why that stopped being
right. Nothing else in `tests_opex_activation.py` was touched beyond the one 285 comment
line. The suite returned to its baseline of 1 failure + 1 error, both pre-existing.

---

### B30 — migration 0069 still calls live application code, and has simply not broken yet

**Found by prompt HOTFIX-1, 3 Sep 2026, while fixing the 0067 deploy failure (R-22). Not
fixed: it is not broken, and fixing an unbroken migration is a change with no test to prove
it right.**

Three migrations import from live modules. That is the general shape of the bug that took
production down, and `is_mirror` was one instance of it, not the class.

| Migration | Imports | Exposure |
|---|---|---|
| `0067_seed_residential_template_v1` | `utils.RESIDENTIAL_TEMPLATE_CODE`, `RESIDENTIAL_TEMPLATE_LABEL`, `_get_duration`, `build_residential_phases`, `seed_task_template_version` | **WAS the failure.** Fixed at the helper by `utils.kwargs_for_model_state()`; the migration body is unchanged |
| `0075_seed_opex_template_v1` | `utils.seed_task_template_version` | Same helper, and it is the caller `is_mirror` was added FOR. Safe today because 0075 is the chain head, so its model state is current — **that stops being true the moment a 0076 adds a field to `TaskTemplateTask`**, and the tolerance now in the helper is what will absorb it |
| `0069_backfill_checklist_versions` | `models.derive_checklist_code` | **STILL EXPOSED, narrowly.** The function is pure, takes its model class as an argument, and touches exactly one field — `Checklist.code`, added by 0068, one migration earlier. It breaks if `code` is renamed or dropped, or if the function grows a second field read. Neither is imminent; both are silent |

**Also carried by 0069 and worth naming, because it is the same shape one level down:**
the migration calls `checklist.save(update_fields=[...])` on a **historical** model class,
whose `save()` is the plain Django one. Any rule the concrete `Checklist.save()` enforces —
the R-7 versioning guards — is therefore not applied here. That is correct for a backfill
and deliberate, but it is the second thing about 0069 that depends on today's `models.py`
not moving.

**What would close this.** Not much: `derive_checklist_code` could take the field name, or
0069 could inline the ~15 lines it needs. Neither is worth a migration edit while the chain
is green and `tests_migration_chain.py` now fails loudly if it stops being. **The trigger is
a rename or removal of `Checklist.code`** — do it in the same session as that change, not
before.

**Do not add a fourth.** Three is the whole population and R-22 says prefer zero.

---

### B31 — the fast suite and the migrating suite are two runs, and nothing reconciles them

**Recorded by prompt HOTFIX-1, 3 Sep 2026. Accepted, not fixed — see R-22 and §18.**

`solarpms/test_settings.py` survives for speed, so there remain two ways to run the suite
and they do not check the same thing. `tests_migration_chain.py` closes the gap that
mattered — the chain now gets exercised under either — but it does not make the two runs
equivalent, and **a fast suite nobody has to reconcile against a slow one is how the 0067
failure happened.**

**Measured on this commit: 1 failure / ~75 s under the shim, against 3 failures + 308 errors
/ ~1,350 s under real settings.** None of the 308 are product defects — **306 are one
collision**, a shared fixture creating `BOQItemMaster` rows whose codes migrations 0047 and
0057 already seeded (`Key (code)=(OPX-001) already exists`), across seven modules:
`tests_residential_baseline` 92, `tests_task_status_path` 49, `tests_boq_upload` 47,
`tests_status_transition` 39, `tests_soft_delete` 32, `tests_design_part11` 32,
`tests_demo_data` 15. The other two errors and all three failures are `TaskDurationTemplate`
the same way — 0034 seeds 50 rows, the tests assert 0.

**The disagreement runs both ways**, which is the part that makes this a real gap rather
than a slow duplicate: `tests_design_part46`'s standing SQLite constraint-name failure
**passes** under Postgres. Neither run is a superset of the other. Also invisible under the
shim: `DROP TABLE … CASCADE` in `0005`, and the ordering of unordered querysets.

**The trigger to revisit:** if the two runs ever disagree on a failure that is not already
on the baseline list, the shim's cost has exceeded its benefit and it should go.

---

## C. Phase 2 — installation, HSE, QA/QC and punch points (prompts 2.1 – 2.4)

_No entries yet._

---

## D. Phase 3 — design handover and as-built (prompts 3.1 – 3.4)

> ### ✅ DEPLOY GATE — SATISFIED 15 Sep 2026 BY PROMPT 3.1b-3. 3.1b-2c (`1f2a137`) MAY REACH RAILWAY, BUT ONLY TOGETHER WITH OR AFTER THE 3.1b-3 COMMIT
>
> **What 3.1b-3 added.** The four people the gate blocks are told it is their turn, **in-app
> only**:
>
> | Transition | Who is told | Where the notice links |
> |---|---|---|
> | Head's pass | the site's PM and active Coordinators | the PM approval queue |
> | PM's rejection | the actor on the latest ledger transition into `awaiting_pm_approval` (the Head or deputy who passed it, or whoever returned it); else `head_reviewed_by`; else every active Design Head (that fan-out is logged) | `design_qc_review` |
> | Head's return | the site's PM and active Coordinators | the PM approval queue |
> | Head's send-back | the allocated designer | the design workspace |
>
> - **Every call site names `channels=['in_app']`.** Each send runs after the view's atomic block,
>   inside a `try`, so a failed send cannot unwind the transition.
> - **`tests_design_gate_notifications`** proves nothing else is transmitted, with the switches
>   ON.
> - **Still true after 3.1b-3:** nothing is emailed or sent on WhatsApp, there is no off switch
>   (§D37), and SCM is told nothing on release (§D39). The original entry follows.
>
> #### ⛔ ORIGINAL — PROMPT 3.1b-2c MUST NOT REACH RAILWAY BEFORE PROMPT 3.1b-3 (NOTIFICATIONS) EXISTS
>
> Recorded 13 Sep 2026 by prompt 3.1b-2c, the commit that makes the PM gate live.
>
> - **What 3.1b-2c changed.** The Design Head's QC pass no longer releases a site. It parks
>   the package at `awaiting_pm_approval` until the site's PM approves it. A PM rejection
>   parks it at `pm_rejected` until the Design Head answers.
> - **What is missing.** Nobody is TOLD. No email, no WhatsApp and no in-app notice goes to
>   the PM when a package lands in their queue, or to the Head when the PM rejects one. The
>   queue is reachable only from the nav link (`design_pm_approval_queue`).
> - **What happens if it ships alone.** Every design the Head passes stops at the PM and
>   nobody knows. SCM's pool stops receiving sites. The first person to notice is the CEO,
>   asking why nothing reached SCM. On today's data that PM is `nirankar`, who holds 86 of
>   the 93 design sites with no coordinator and no deputy (§D8).
> - **The rule.** Push 3.1b-2c to Railway ONLY together with, or after, 3.1b-3. Until then
>   it stays local and unpushed. Anyone deploying `main` must check this line first.

### ~~D1 — a site awaiting PM approval will count as its designer's current load and sit in the rework denominator, so 3.1-ACC must land BEFORE 3.1b~~ — **CLOSED by prompt 3.1-ACC**

#### CLOSED 11 Sep 2026 BY PROMPT 3.1-ACC (B-06 attempt accounting)

Each site now carries a `finished` flag, `status in DESIGN_WORK_FINISHED_STATUSES`, beside
the strict `released` one. It is built in `tender_metrics()` and in `analytics_dataset()`.
A site in `awaiting_pm_approval`:

- is **not** current load (sites and kW) in `designer_workload`;
- is **not** counted as released: the "N released" line, `released_count`, capacity
  throughput, on-time delivery and cycle time all still read `released`;
- **stays in** the finished-site denominator of rework, input, PM change, first-pass, the
  rework multiplier and the change-request rate, so no ratio inflates;
- is **not** in `no_due_date`.

Every one of those is exercised only by a fixture (`tests_design_attempt_accounting`
section d, through `PmGateBase._park_in_pm_gate`). Nothing can produce the status until
3.1b. The original entry follows.

Recorded by prompt 3.1a, 10 Sep 2026, which dropped this item (B7) from its own scope
on instruction.

`awaiting_pm_approval` exists and is unreachable. Two families of figures read
`released`-or-not and will mis-file it the moment prompt 3.1b makes it reachable:

- **`design_metrics.designer_workload()`.** `if s['released']: row['released'] += 1` /
  `else: row['sites'] += 1` and `capacity_kw`. A site with the PM is counted as the
  designer's **current load** (sites and kW), although the designer has nothing left to
  do. It is also missing from the `released` denominator of `rework`, `input_quality` and
  `pm_change_multiplier` until the PM approves.
- **`design_analytics.analytics_dataset()`'s `released` flag.** Every released-only figure
  (`m_first_pass_rate`, `m_rework_multiplier`, `m_capacity_throughput`,
  `m_change_request_rate`, `m_on_time_delivery`, `m_cycle_time`, `released_count`) leaves
  it out until the PM approves.

3.1a touched none of these. `tender_metrics`, `designer_workload`, `analytics_dataset` and
every rework and first-pass figure are prompt 3.1-ACC's.

**Ordering constraint:** 3.1-ACC must land **before 3.1b**, not merely before go-live.
From the first commit that writes the status, every Head-passed site shows up as live
designer load and drops out of the denominators.

### ~~D2 — `qc_review.html` would tell a reviewer "Nothing to review yet" about a design with the PM~~ — **CLOSED by prompt 3.1b-2b**

#### CLOSED 13 Sep 2026 BY PROMPT 3.1b-2b

The actions card's chain gained two branches ahead of its fall-through, read off the
`status` the shared workspace context already carries. At `awaiting_pm_approval` it says
both gates passed and the package is with the site's PM. At `pm_rejected` it says the PM
rejected it and it is back with the Design Head. The Head's own card at `pm_rejected` is
the two actions (§D14). Pinned by `tests_design_head_rejection_inert` `ScreenTests`. The
original entry follows.

Recorded by prompt 3.1a, where `qc_review.html` was out of scope.

The actions block's `if/elif` chain in `qc_review.html` reads `blocked_by_own_qc_verdict`,
`open_crs`, `awaiting_head` (`status == awaiting_head_qc`) and `released`
(`status == released`), then falls to `{% else %}`: *"Nothing to review yet. Design QC starts
once the package is complete"*. A site at `awaiting_pm_approval` matches none of the
branches, so a Head-passed package would be described as incomplete. The `released` banner
at the top of the page doesn't render for it either. `design_qc_review` builds the flags.

**Not reachable today.** It must be fixed with, or before, prompt 3.1b.

### D3 — PARTLY CLOSED (one of two, 13 Sep 2026): two stale sentences that 3.1a's MODE did not reach

**The first bullet was fixed by the comment-correction prompt.** The docstring now says the
map lists every choice, with no count to go stale. The second bullet stands on purpose: it is
false on its own, but the paragraph directly below it says the screen no longer reads the
flag (§D34).

- **`design_views.derive_design_mirror_state()`'s docstring** still says
  *"DESIGN_MIRROR_STATE_MAP lists all fourteen, so the only way to reach this line is to
  have added a fifteenth"*. There are fifteen statuses now, and the map lists all of them.
  The function wasn't in 3.1a's MODE.
- **`design_views.design_my_sites`'s comment on `is_released`** still says it is *"Read ONLY
  by the Design Hold control in my_sites.html"*. 3.1a moved that control to the new
  `design_work_finished` flag, as instructed, and left `is_released` in place and truthful.
  It now has no reader on that screen. The MODE admitted only the new flag in that
  function, so the old comment was left as it was.

**Risk if left:** the next reader is misled about a count and about who reads a flag.
There is no behaviour at stake.

### ~~D4 — the two design chip partials show a site awaiting PM approval as a grey chip~~ — **CLOSED by prompt 3.1b-2c**

#### CLOSED 13 Sep 2026 BY PROMPT 3.1b-2c

`awaiting_pm_approval` is **amber** in both partials. Amber is the colour the PM's own queue
already gives this state, so the PM and the design screens show it the same way. It is
distinct from `released` (green on the dashboard, grey on the workspace) and from
`pm_rejected` (navy).

- **`_dashboard_design_chips.html`.** One `or` was added to the existing amber branch, with
  no new markup. Amber there also means `arka_rejected`. That was accepted: the label tells
  them apart, and both mean "waiting on someone else".
- **`_design_status_chips.html`.** It had no amber branch, so one two-line `elif` was added.
  Joining the navy branch instead would have made `awaiting_pm_approval` and `pm_rejected`
  identical, which is the defect this entry exists to close.

The original entry follows.

Recorded by prompt 3.1a. The MODE allowed flag substitution only in these two templates.

`_dashboard_design_chips.html` and `_design_status_chips.html` pick a chip colour with an
`if/elif` chain over literal status values. Neither reads a flag that 3.1a changed, so
nothing needed substituting. Colouring the new status (for example, the navy "in review"
chip that `awaiting_head_qc` gets) would mean adding a literal comparison, which is new
logic rather than a substitution. Until someone decides the colour, a site awaiting PM
approval gets the grey fall-through chip. It still shows the correct label ("Awaiting PM
approval"), through `get_status_display` / `status_label`.

It is a presentation decision for prompt 3.1b or 3.1c, and nothing is reachable today.

### ~~D5 — the tender dashboard renders a Rework or Input of 0.0 as "—", with a tooltip that says there are no released sites~~ — **CLOSED by the zero-display prompt**

#### CLOSED 11 Sep 2026 BY THE ZERO-DISPLAY PROMPT

- **Cells.** The Rework and Input cells now test `is not None`, in both the display and
  the `data-v` sort key. A zero renders as `0.0×`, and the dash is kept for None only
  (the designer has no finished site). The tooltip on that dash now reads *"No finished
  sites yet — nothing to divide by"*.
- **Footer.** It now describes designer-caused attempts (Group A, plus uncategorised) ÷
  finished sites.
- **Two other captions.** The workload header ("exclude finished work") and the attention
  footer ("unfinished site") were corrected in the same session.
- **Left as they were,** because they cannot legitimately be zero: the drill-down kW and
  capacity-panel kW truthiness guards (`design_arka_submit` refuses a capacity ≤ 0).
- **Tests.** Both directions are pinned by `tests_design_zero_display`.

The original entry follows.

Recorded by prompt 3.1-ACC, 11 Sep 2026. `tender_dashboard.html` was outside its MODE.

**Visible today.** The workload table tests `{% if w.rework %}` and `{% if w.input_quality %}`.
Python's 0.0 is falsy, so a genuine zero renders as the "no data" dash, and its `title` says
*"No released sites yet — nothing to divide by"* (or *"…or no input-caused rework"*). The Input
column has done this since Part 9. B-06 made 0.0 the correct Rework for a clean record, so on
the SCMPILOT tender demo.design's Rework moved from **"1.2×" to "—"**, with a false tooltip.
The fix is `{% if w.rework is not None %}` in both cells, plus the matching `data-v` sort
keys. The figure itself is None only when the designer has no finished site.

**Same template, stale text.** The workload card footer says *"Rework = attempts caused by a
designer error (Group A) ÷ released sites"*. Since B-06 the numerator also counts
uncategorised pre-Part-9 QC failures (product owner, B-06 Stop 1 Q2), and the denominator is
finished sites: released, or awaiting PM approval. The two denominators agree until 3.1b.

### ~~D6 — the change-request-rate panel still labels its divisor "released" after it became "finished"~~ — **CLOSED by the zero-display prompt**

#### CLOSED 11 Sep 2026 BY THE ZERO-DISPLAY PROMPT

- **Headers.** They now read *"Accepted per finished site"* and *"Finished sites"*.
- **Column.** It renders `r.finished`, the figure's own divisor, so it always agrees with
  the `n=` beside it. `r.released` stays in the row and is no longer displayed here.
- **Legend.** The sample-size legend no longer names a single denominator (*"Under 5 sites
  or reviews in the denominator"*).
- **Tests.** Pinned by `tests_design_zero_display`, with a site parked in the PM gate so
  that finished and released differ.

The original entry follows.

Recorded by prompt 3.1-ACC, 11 Sep 2026. Only one `quality_analytics.html` header was in its
MODE.

`m_change_request_rate` now divides by finished sites (B-06, B4). Each row carries both the
strict `released` count and a `finished` count, which is the divisor. The panel's headers
*"Accepted per released site"* and *"Released sites"* are true until prompt 3.1b makes
`awaiting_pm_approval` reachable. After that, the figure's `n=` and the "Released sites"
column can disagree. **Fix with, or before, 3.1b:** relabel the first header and show
`r.finished` beside or instead of `r.released`.

### D7 — `pm_change_multiplier` is computed and nothing reads it

Recorded by prompt 3.1-ACC, 11 Sep 2026.

`designer_workload()` returns `pm_change_multiplier` (PM-change attempts on finished sites ÷
finished sites). No template, view or report reads it. The tender dashboard shows only the raw
chip count, *"N PM change"*. B-06 kept it consistent with rework and input: same
implementation (`rework_contribution()`), same denominator, pinned by
`tests_design_attempt_accounting` section c. On the pilot it read 0.0 before and after.
**Either display it as the third figure the workload card's footer already describes, or
delete it.** Until then it is dead weight that has to be kept correct for no reader.

### D8 — one PM holds 86 of 93 design sites, with no coordinator and no deputy

Recorded by prompt 3.1b-1 (its D-a), 12 Sep 2026, from the local dump.

- **The numbers.** 86 of the 93 OPEX sites with a design assignment have `nirankar` as PM
  (all of MPUVNL). `nitesh` has 10 OPEX sites and `demo.pm` has the six SCMPILOT sites.
- **No coverage.** No OPEX site has a Project Coordinator. `can_approve_design_release()`
  deliberately allows no deputy.
- **The risk — LIVE since 3.1b-2c, 13 Sep 2026** (on local `main`; not on Railway, per the
  DEPLOY GATE). `awaiting_pm_approval` is reachable, so one person's absence strands a whole
  tender at the PM gate.
- **The remedy is data, not code.** Populate `Project.coordinators`. That is identity-based
  authority, and `user_can_manage_project()` already honours it.
- **Owner and timing.** A data task for the product owner, BEFORE 3.1b-2c reaches Railway.
  Re-run the counts on Railway first: these are local figures.

### D9 — ANSWERED, NOT CLOSED: a PM rejection may land on a closed attempt

Recorded by prompt 3.1b-1 (its D-b).

**ANSWERED by the pm_rejected schema prompt, 13 Sep 2026: the rejection got a status of its
own,** `pm_rejected`, added by migration 0086 (`0086_design_pm_rejected`). The Head could not
act from `awaiting_head_qc` without rewriting the closed attempt's verdict:
`design_head_qc_fail()` would overwrite `head_verdict='passed'` (the reason `models.py` gives
beside `DESIGN_PM_REJECTED`). Prompt 3.1b-2c then retargeted `design_pm_reject` onto the new
status, so the attempt the Head passed stays closed with its verdict intact.

**Left open on purpose.** This entry is the record of WHY `pm_rejected` exists. Closing it
would hide the reasoning behind a schema decision behind a strikethrough. The original entry
follows.

- **What happens.** `design_pm_reject` returns the site to `awaiting_head_qc`. But the Head's
  pass may already have closed that attempt, with `head_verdict=passed` and `closed_at` set.
  It does both today. From there, `design_head_qc_fail` would call `_open_next_attempt`
  from a closed attempt, and `design_head_qc_pass` would rewrite the verdict.
- **What 3.1b-2's pre-flight must establish.** Can the Head act on a closed attempt WITHOUT
  rewriting its verdict?
- **If not,** the rejection needs a status of its own. That is a migration and a fresh
  R-1 approval.
- **Nothing in 3.1b-1 depends on the answer.** Its reject view writes only the status and
  the ledger row.

### ~~D10 — BLOCKS 3.1b-2: the design workspace tells a PM they are the Design Head~~ — **CLOSED by prompt 3.1b-2b**

#### CLOSED 13 Sep 2026 BY PROMPT 3.1b-2b

`design_site_workspace` now passes the `pm_reviewing` flag it already computed. A status
test alone would have shown the PM's text to the Design Head at the same status. The PM
gets a banner naming them as the site's Project Manager, pointing to their Design approvals
queue, and a back link to that queue instead of the always-empty "My sites". The Head's
banner and link are byte-identical. The PM's verdict controls stay on the queue only, as
3.1b-1 decided. The original entry follows.

Recorded by prompt 3.1b-1 (its D-c). `site_workspace.html` was outside its MODE.

- **What it says.** 3.1b-1 opened `design_site_workspace` to the site's PM (or a
  Coordinator) while the site is at `awaiting_pm_approval`. The screen's
  `{% if not is_designer %}` banner then tells them *"You are viewing this as Design
  Head"*.
- **Where it links.** It points them at `design_head_review` for the Arka verdict. That
  view 403s for a PM.
- **Two smaller dead ends.** The "My sites" back link goes to `design_my_sites`, which is
  always empty for a PM. The workspace also carries none of the PM's verdict controls:
  those live on the approval queue only.
- **When it matters.** Unreachable today. It is wrong the moment 3.1b-2 makes the status
  reachable.

### D11 — `released_by` changes meaning at the PM gate

Recorded by prompt 3.1b-1 (its D-d).

- **The five existing released rows** (SCMPILOT01–05) hold the Design Head in `released_by`
  and the Head's pass time in `released_at`.
- **Every row released by `design_pm_approve`** holds the approving PM and the approval
  time. That is by decision: `released_at` is the SCM pool's age clock and the end of every
  cycle-time figure, so it means RELEASED TO SCM.
- **Recorded, not migrated.** The old rows were true when they were written.
- **Reading across the boundary.** Anything that reads `released_by` as "who signed off
  the design technically" must read `attempt.head_reviewed_by` instead.

### D12 — R-9 is enforced per view, not centrally, for design transitions

Recorded by prompt 3.1b-1 (its D-e).

- **The gap.** `REMARK_REQUIRED_SUBJECT_TYPES` is `frozenset()`, so `record_transition()`
  demands a remark for no subject.
- **How 3.1b-1 handles it.** `design_pm_reject` enforces its mandatory remark in the view,
  and refuses a blank or whitespace-only remark before writing anything.
- **Why not add `design_assignment` to the set now.** It would change the contract of
  every existing design transition. Most of those write through `apply_design_status()`
  with no remark, so they would start raising.
- **Open decision.** Whether it should join the set belongs to a later session, after
  every design writer collects a remark.

### D13 — PARTLY CLOSED (three of five doors, 13 Sep 2026): a designer can put a handed-in package on Design Hold, and clearing the hold rewinds it to `in_design` with no attempt and no change request

Recorded by the pm_rejected schema prompt's pre-flight (its A2), 13 Sep 2026. **Reported,
not fixed** — on instruction, because it is a live defect with its own blast radius and
does not belong in a schema commit.

- **The guard.** `design_mark_blocked` refuses only `survey_returned` and
  `DESIGN_WORK_FINISHED_STATUSES` (`awaiting_pm_approval`, `released`). Nothing else. The
  dashboard's `can_mark_blocked` and `my_sites.html`'s Hold control use the same test, so
  the **button renders** too. A crafted POST is not needed.
- **The clear.** The Head clears a hold by replacing the survey file
  (`design_survey_upload`) or the folder link (`design_survey_link_set`). Both call
  `_status_after_unblock()`, which returns `in_design` for any allocated site with an
  approved date. It never returns the pre-hold status.
- **What it does to a handed-in package.** A hold at `artifacts_uploaded`, `in_qc` or
  `awaiting_head_qc` comes back at `in_design`, on the **same** attempt:
  - no `_open_next_attempt`, no new attempt, no change request;
  - `qc_started_at`, `boq_submitted_at` and any gate verdict stay on the attempt, so the
    BOQ stays design-locked while the status says design is under way;
  - the package drops out of `design_qc_queue`;
  - `ARKA_SUBMITTABLE_STATUSES` includes `in_design`, so the designer may submit a new
    Arka version onto the reviewed attempt.

  That is the §6.1 reopen route (PHASE_2_PREFLIGHT_AUDIT), live on reachable statuses.
- **Blast radius on the local dump** (computed read-only from `_status_after_unblock` on
  the real rows; nothing was written):

  | Site | Status | Current attempt | Clear restores |
  |---|---|---|---|
  | MB0141 | `in_qc` | 1, QC started, BOQ complete | `in_design` |
  | MB0164 | `artifacts_uploaded` | 1, BOQ complete | `in_design` |
  | MB0191 | `artifacts_uploaded` | 1, BOQ complete | `in_design` |

  None sit at `awaiting_head_qc` today; that status is reachable from every Design QC pass.
- **Adjacent, same mechanism.** A hold at `arka_submitted` or `awaiting_head_arka` also
  clears to `in_design`. That drops a pending Arka out of the Arka queue and reopens Arka
  submission. MB0005 and SCMPILOT06 sit at `arka_submitted`.
  - **Correction, D18 session, 17 Sep 2026.** Those two were EXPOSED, not affected: neither
    had ever been held. On the local dump no site has ever been held (no `design_blocked`
    ActivityLog row, no `survey_returned_at`), and SCMPILOT06 is local seed data, not a
    production site. This is fixed forward as §D18.
- **The open question for its own prompt.** Either the hold refuses post-hand-in statuses,
  or the clear restores the pre-hold status instead of deriving `in_design`. This is not
  obviously the first: a survey defect can surface during review. Re-run the counts on
  Railway first — these are local figures.
- **What the schema prompt did about it.** Nothing, deliberately. Its new "designer does
  not hold this site" set does NOT include `awaiting_head_qc`, `in_qc` or
  `artifacts_uploaded`, so no guard's answer changes for a reachable status.
- **The fix, located.** Add those three statuses to `models.DESIGN_NOT_WITH_DESIGNER_STATUSES`,
  which the four guards and the three screen flags already read. The set's own comment
  says so, so nobody takes the omission for the answer. `is_overdue()` and
  `attention_list()` deliberately name `pm_rejected` rather than reading that set, so that
  fix cannot move the overdue rule.
- **What the D13 prompt did, 13 Sep 2026 — three of five doors closed, NOT all of D13.**
  - **The set was split first (R-5), because it answered two questions.** The pre-flight
    found that `DESIGN_NOT_WITH_DESIGNER_STATUSES` fed the date controls as well as the
    hold: the designer's extension request and the Head's Change date. `is_overdue()`
    keeps counting `in_qc` and `awaiting_head_qc`, so adding the review statuses there
    would have left a site going overdue against a date nobody could move until a
    reviewer acted.
    - `DESIGN_CLOCK_STOPPED_STATUSES` is new and keeps the old membership:
      `awaiting_pm_approval`, `released`, `pm_rejected`. Its readers are
      `design_due_date_propose`, `can_request_extension`, `design_due_date_change` and
      head_sites' `clock_stopped` flag.
    - `DESIGN_NOT_WITH_DESIGNER_STATUSES` keeps its name and now adds `artifacts_uploaded`,
      `in_qc` and `awaiting_head_qc`. Its readers are `design_mark_blocked`,
      `can_mark_blocked` and my_sites' `not_with_designer` flag (renamed from
      `design_work_finished`).
  - **The three review statuses now refuse the hold, at the endpoint AND on the screen.**
    The refusal names the honest route: the reviewer holding the package fails it with
    "Survey data inadequate or incorrect". That is Group B, so attempt N+1 opens with
    reason `qc_failed` and counts as an input problem, not as the designer's rework.
    `tests_design_hold_refusal.py` drives that route end to end from `in_qc`, and from
    `artifacts_uploaded` via QC start. It also pins the reopen sequence so it fails if
    the fix is reverted.
  - **Two doors stay open: `arka_submitted` and `awaiting_head_arka`.** A refusal is the
    wrong instrument there, because the designer is still working (the card offers
    Upload CAD and Enter BOQ). The residual is **§D18**.
  - **Not answered here:** §D17's "does current load mean the designer holds it" question,
    which named D13 as its moment. `designer_workload` was outside the prompt's MODE.

### ~~D14 — HARD REQUIREMENT OF 3.1b-2b: the Head's counts and the QC queue must gain `pm_rejected` rows TOGETHER WITH their actions~~ — **CLOSED by prompt 3.1b-2b**

#### CLOSED 13 Sep 2026 BY PROMPT 3.1b-2b — one commit, rows and actions together

- **The two actions.** `design_head_return_to_pm` and `design_head_send_back` both require
  `pm_rejected` and gate-2 authority, through `_qc_guard(..., gate='head')`. They sit on
  `design_qc_review` behind one view flag, `can_resolve_pm_rejection`.
- **The Head's sites screen.** A `pm_rejected` row shows the PM's latest remark and its
  time, read from the ledger by `latest_design_transition()` in the screen's one query. It
  also carries a *Review PM rejection* link to the package screen.
- **The count.** `design_head_dashboard_counts()['pm_rejected']` is its own tile, shown only
  when it is non-zero, so no existing strip moves. It is NOT folded into `awaiting_head_qc`,
  so no package is counted in two tiles and no tile sums them. The tender dashboard's
  stage count still files `pm_rejected` under `awaiting_head_qc` via `_classify()`; that
  was the schema prompt's decision and is unchanged.
- **The QC queue EXCLUDES it.** It is excluded by `design_qc_queue`'s own status tuple,
  which no other screen reads, so no shared predicate was edited. The trap this entry
  named (`can_qc` True for a QC reviewer) cannot be reached. The queue carries a comment
  saying so.
- **Still open, and visible from 2c:** §D16's `_attempt_history.html` has no branch for a
  `pm_rejected` attempt. Its readers were outside this MODE.

The original entry follows.

Recorded by the pm_rejected schema prompt (its B9, D-f), 13 Sep 2026. **This is not a
deferral.**

- **Today.** `design_head_dashboard_counts` and `design_qc_queue` are unchanged, so a
  `pm_rejected` site is in neither. The status is unreachable, so nothing is wrong yet.
- **The trap.** `design_qc_queue` scopes by `_qc_scope()`, and a `pm_rejected` row sits
  inside that scope. If 3.1b-2b widens the queue's status tuple without also teaching its
  row flags, `can_qc = can_qc_gate and not awaiting_head` is True for a QC reviewer. That
  reviewer is shown gate-1 buttons on a package that is not theirs. The Head would get a
  Review link to a screen with no action for this status.
- **The requirement.** The Head's count, the queue rows, the row flags and the Head's two
  actions (return to PM, send back to designer) ship in ONE commit, or none of them do.

### ~~D15 — BLOCKS 3.1b-2b's send-back: attempt N+1 reopens against the OLD due date and is overdue on arrival~~ — **CLOSED by prompt 3.1b-2b, decision (c)**

#### CLOSED 13 Sep 2026 BY PROMPT 3.1b-2b

**The decision (product owner): (c).** The send-back moves the agreed date out by the
whole days since attempt N's `head_reviewed_at`, as one new approved commitment. This is
`_extend_due_date_for_pm_review()`, the same write `design_due_date_change` makes. It is
exactly the span the overdue rule's clock was stopped (`awaiting_pm_approval`, then
`pm_rejected`), so the designer gets back the margin they had at the Head's pass.

**The candidates that lost:**
- **(a) clear the date.** No field means "cleared", and `design_due_date_change` refuses a
  site with no approved date, so nothing could ever date the site again.
- **(b) keep it.** The designer is charged whenever the Head forgets to move it.

**An open extension request REFUSES the send-back** (product decision, 13 Sep 2026). Such a
request can be raised at `in_qc` and survives into the PM gate, because nothing stands one
down. The Head rules on it first from the sites screen's Extension button. Superseding it
would drop it with no verdict; rejecting it would record a verdict the Head never gave.

**Costs:** see §D23. The original entry follows.

Recorded by the pm_rejected schema prompt (its B9, D-g), 13 Sep 2026.

- **While the site is `pm_rejected`,** `is_overdue()` returns False: the Head passed the
  attempt, so the designer delivered.
- **The moment the Head sends it back,** `_open_next_attempt` moves the site to `in_design`
  or `arka_submitted`. It keeps every `DueDateCommitment`, by design (settled decision 2).
  So the old approved date is live again, and usually already past: the site is overdue on
  arrival for a delay the designer did not cause.
- **Decide BEFORE the transition is built.** Options: the send-back sets a new date, or
  requires the Head to set one, or rework runs without a date. `design_due_date_change`
  refuses `pm_rejected` today, so the date can only move after the send-back.

### D16 — LIVE: readers of `opened_reason` outside the schema prompt's MODE assume three values

Recorded by the pm_rejected schema prompt (its B9, D-h), 13 Sep 2026. **LIVE since 3.1b-2c,
13 Sep 2026:** every send-back opens an attempt with `opened_reason='pm_rejected'`, so each
bullet below describes what the code on `main` does now, not what it will do.

- **`design_metrics.designer_workload`'s raw counters.** They count `qc_failed` and
  `pm_change_request` and nothing else, so a PM-rejection attempt is in `attempts` and in
  neither counter. The Split chip (from `attempt_cause_split`) is correct:
  `classify_attempt_causes` gained its branch.
- **`design_analytics.m_first_pass_rate`.** It counts `qc_failed` only, so a
  PM-rejection send-back does not cost first-pass. **Open PRODUCT decision:** does a PM
  rejection the Head sends back to the designer cost first-pass?
- **`_attempt_history.html`.** Its reason border and label have branches for `qc_failed`
  and `pm_change_request` only, so every send-back's attempt renders with neither.
- **`METRIC_CATALOGUE` text** — moved to its own entry, **§D32**, because it is user-visible
  text describing a metric, not a reader of the value, and it is now incomplete on live data.
- **The first-pass decision, stated as the problem (D32 caption prompt, 15 Sep 2026).** A
  Head's send-back opens an attempt with reason `pm_rejected`. `m_first_pass_rate` counts
  only `qc_failed`, so that loop does not cost first-pass. For the same event,
  `classify_attempt_causes` charges the designer rework when the Head classes it Group A. So
  one event is a designer error on the rework figure and not a failed first pass on the
  first-pass figure. **Still OPEN.** The first-pass caption matches its code and was left
  byte-identical; `tests_design_metric_captions.test_first_pass_does_not_count_it_which_is_section_d16`
  pins the disagreement as it stands, and is the test the deciding session changes.

### D17 — LIVE: `designer_workload` puts a `pm_rejected` site back into the designer's current load

Recorded by the pm_rejected schema prompt (its B9, D-i), 13 Sep 2026. **Stated, not
changed**, because `designer_workload` is outside that MODE.

- `pm_rejected` is not finished (product decision D2), so the site counts as the
  designer's current load in sites and kW, although the Head holds it. That matches how
  `awaiting_head_qc` is treated today.
- So a site moves from load (review), to not load (with the PM), and back to load
  (rejected). Whether current load should mean "the designer holds it" is the same
  question as D13's, and should be answered with it.
- **Still open after the D13 prompt (13 Sep 2026).** D13's fix answered the question for
  the hold guards only; `designer_workload` was outside that MODE.
- **LIVE since 3.1b-2c, 13 Sep 2026.** `design_pm_reject` writes `pm_rejected`, so this now
  happens on every PM rejection.

### D18 — FIXED FORWARD (17 Sep 2026), NO ORPHAN IN PRODUCTION (18 Sep 2026): a Design Hold cleared from `arka_submitted` or `awaiting_head_arka` orphans a pending Arka verdict

#### FIXED FORWARD 17 Sep 2026 BY THE D18 SESSION — new lifts restore; existing orphans are §D46

- **What a lift does now.** `_status_after_unblock()` is still the one function that decides,
  and `design_survey_upload` and `design_survey_link_set` are still its only two callers
  (parsed before and after: the same set). It asks `_arka_status_before_hold()`, which reads
  the hold's own `StatusTransition` row through `latest_design_transition()`:
  - **R1.** The from-status is restored only when it is `arka_submitted` or
    `awaiting_head_arka`. Every other from-status is derived exactly as before.
  - **R2.** ...and only when the current Arka matches it (`_arka_matches_status()`, strict):
    `arka_submitted` needs both verdicts pending or both approved; `awaiting_head_arka` needs
    QC approved and the Head pending. Anything else is derived, with a `logger.warning`
    naming the site, the from-status and the Arka's state. Through the product no verdict can
    land during a hold (both verdict targets test the status); the Arka admin can make one.
  - **R3.** If the site's latest ledger row is not the hold itself, the hold has no row of its
    own. That is a hold placed before the ledger (`fc24728`, 5 Sep 2026, migration `0079`), or
    one written outside the product. In production the ledger's first design row is
    16 Sep 2026 08:46:33 UTC (A8, 18 Sep 2026), so there "before the ledger" means before that. It is derived, with a warning, and never refused. The
    latest-row test is what stops an OLDER ledgered hold being read as this one.
  - **Pre-§D13 holds.** A ledgered hold from a member of `DESIGN_NOT_WITH_DESIGNER_STATUSES`
    is derived, with a warning containing "pre-§D13 hold; needs repair". Only `in_qc`,
    `artifacts_uploaded` and `awaiting_head_qc` can carry one: the ledger began after the
    refusal at `released` (`57ae66f`) and before §D13's (`969eed4`), and
    `awaiting_pm_approval` and `pm_rejected` were refused by the commits that added them
    (`c212043`, `9104ee7`). Their derived result is the §D13 defect, so no test pins it.
  - **R5 (the progression rule).** After restoring `arka_submitted`, both lift views call
    `_maybe_advance_to_artifacts_uploaded()` in the same transaction. CAD upload and BOQ
    completion have no status guard (§D47), so a package can become complete during a hold.
- **R4 — no repair here.** Existing orphans are §D46.
- **Tests.** `tests_design_hold_restore.py`: every site driven through the real endpoints;
  §D13's three doors refused exactly as on the commit before (the same test class passes
  there); three mutations — dropping R2, dropping R3, ignoring the last-row guard — each turn
  named tests red.
- **Metrics.** Nothing reads the ledger for a figure. `m_stage_dwell` reads timestamps on
  `DesignAssignment`, `DesignAttempt` and `ArkaSubmission`, not `StatusTransition` (the D18
  prompt called it ledger-based; it is not). `design_figure_snapshot` on `solarpms_local`
  was byte-identical before and after, because no site there has ever been held.
- **Production (A8) — run 18 Sep 2026.** The two read-only console blocks were written and
  dry-run through a line-by-line console on 17 Sep, and run in the Railway shell on
  18 Sep 2026 (database `railway`, host `postgres.railway.internal`):
  - **Block 1:** 12 design ledger rows; first ledger row 2026-09-16 08:46:33 UTC; 0 hold or
    lift rows; 0 pre-ledger `design_blocked` ActivityLog rows; 0 holds still open in the
    ledger; no rows printed, so no ORPHAN.
  - **Block 2:** 0 sites on Design Hold.

  **No orphan exists in production. The fix is preventive:** no production site has been
  held since the ledger began, and none was held before it on the ActivityLog's evidence.
  §D46 is therefore NOT NEEDED.
- **The ledger began on 16 Sep in production, not 5 Sep.** `fc24728` (migration `0079`) is
  correctly dated 5 Sep 2026; that is when the ledger was written. The first production row
  is simply later, 16 Sep 2026 08:46:33 UTC. No doc stated a 5 Sep production start; every
  "5 Sep" beside `0079` or `fc24728` dates the commit, and stands.
- **The one residual blind spot.** A hold placed before the production ledger whose
  `design_blocked` ActivityLog row was swallowed by `log_activity()` (which does not raise)
  would be invisible to both blocks: no ledger row, no ActivityLog row, and — if since lifted
  — no current hold. Judged not worth chasing: it needs a hold in an 11-day window and a
  logging failure on the same request, and Block 2's zero rules out any such hold still open.
- **Corrected below, not deleted.** MB0005 and SCMPILOT06 were EXPOSED, not affected: neither
  was ever held, and SCMPILOT06 is local seed data.

The original entry follows.

Recorded by the D13 prompt (its A4), 13 Sep 2026. **Reported, not fixed.** These are the
two of D13's five doors that a refusal cannot close.

- **The route.** The designer takes a hold at `arka_submitted` or `awaiting_head_arka`.
  Both are allowed, and correctly so: the designer is still working, and the card offers
  Upload CAD and Enter BOQ. The Head then clears the hold, and `_status_after_unblock()`
  returns `in_design` on the SAME attempt.
- **What it orphans.** The current Arka keeps `is_current=True` and a `pending` verdict,
  but nobody can rule on it any more:
  - `_verdict_target()` refuses Design QC (`status != DESIGN_ARKA_SUBMITTED`), and
    `_head_verdict_target()` refuses the Head the same way;
  - the site drops out of `design_qc_queue`'s Arka rows, which filter
    `status__in=(arka_submitted, awaiting_head_arka)`;
  - `_maybe_advance_to_artifacts_uploaded()` returns early off `arka_submitted`, so the
    package cannot move forward.
- **The only way out makes it worse.** The designer must resubmit, and `design_arka_submit()`
  stands the old version down (`is_current=False`). Its verdict then stays `pending`
  forever. If the old Arka was already approved at both gates (the "artifacts outstanding"
  state), the resubmission also orphans the CAD paired to it through `derived_from_arka`.
  `ARKA_SUBMITTABLE_STATUSES`' own comment excludes `arka_submitted` to prevent exactly that.
- **Live rows on the local dump.** Re-count on Railway first.
  - `MB0005` is at `arka_submitted` with v1 `qc=pending head=pending`. A hold and clear
    orphans a pending verdict.
  - `SCMPILOT06` is at `arka_submitted` with v1 approved at both gates. A hold and clear
    reopens resubmission over an approved Arka.
  - **Correction, D18 session, 17 Sep 2026.** These rows show EXPOSURE, not damage. Neither
    site had been held: `solarpms_local` has no `design_blocked` ActivityLog row and no
    non-null `survey_returned_at` on any assignment. SCMPILOT06 is seeded pilot data that does
    not exist in production.
- **Proposed shape, for the session that takes it.** For these two statuses,
  `_status_after_unblock()` should return the status the hold was taken FROM, not
  `in_design` unconditionally.
  - That status is recoverable without a schema change: the hold's own `StatusTransition`
    row (`to_status='survey_returned'`) carries `from_status`. That keeps the function
    "derived, not stored", as its docstring requires.
  - The pending Arka then returns to its queue with its verdicts intact.
  - This changes `_status_after_unblock()` and its two callers, which the D13 prompt's
    MODE forbade. Decide whether the three review statuses keep the refusal or also move
    to restoration: restoring is safe for them too, but the refusal is what names the
    honest attempt-opening route.

### ~~D19 — two date-guard comments name the wrong set after the D13 split~~ — **CLOSED by the comment-correction prompt**

#### CLOSED 13 Sep 2026 BY THE COMMENT-CORRECTION PROMPT

Both comments, in `design_due_date_propose` and `design_due_date_change`, now name
`DESIGN_CLOCK_STOPPED_STATUSES` and the clock-stopped question, and keep the history of both
moves. A third statement of the same kind was fixed with them. `DESIGN_WORK_FINISHED_STATUSES`'
own comment told readers the five guards "test membership here". It now says they read the two
sets below it, both derived from it, and that the set read directly is the metrics' question.
The original entry follows.

Recorded by the D13 prompt, 13 Sep 2026. Comment-only; no behaviour.

- `design_due_date_propose` and `design_due_date_change` now test
  `DESIGN_CLOCK_STOPPED_STATUSES`. Each still carries a comment saying the pm_rejected
  prompt moved the test to `DESIGN_NOT_WITH_DESIGNER_STATUSES`. The propose one adds
  "the question is whether the designer holds the site". Both statements were true when
  written and are now superseded.
- They were left untouched because the D13 prompt required both views to stay
  byte-identical apart from the constant. The next session that touches either view
  should update the comment to name the clock-stopped question.
- **Still true after prompt 3.1b-2b,** whose MODE forbade both views as well.

### D20 — LIVE: the Arka review screen says "waiting on the designer's CAD and BOQ" about a package that is finished

Recorded by prompt 3.1b-2b (its D-j), 13 Sep 2026. `head_review.html`'s verdict card was
outside its MODE for this.

- **What it says.** For any current Arka with `head_verdict='approved'`, the card's
  fall-through branch renders *"Nothing to review — waiting on the designer's CAD and
  BOQ."*
- **Where it is false.** It is true only at `arka_submitted`, the "artifacts outstanding"
  state. On a `released` site it is false today, and that status is reachable. From 2c it
  is also false at `awaiting_pm_approval` and `pm_rejected`.
- **The fix.** Test the status before the Arka's verdict: at a package-stage status, say
  where the package is and link to `design_qc_review`.

### D21 — `design_head_sites` makes three queries per row: 26 at 6 rows, 266 at 86

Recorded by prompt 3.1b-2b (its D-k), 13 Sep 2026. Measured on the local dump as praveen:
SCMPILOT (6 sites) 26 queries; MPUVNL (86 sites) **266**. MPUVNL is the screen nirankar's
tender renders.

- **The cause.** Three reads per row, each its own query:
  `_effective_commitment(assignment)`, `_pending_extension(assignment)` and
  `assignment.due_date_commitments.count()`.
- **The fix pattern already exists.** It is the one this prompt's ledger read uses: work
  on the query the screen already runs. Prefetch `due_date_commitments` once and use
  `design_metrics.effective_commitment(rows)`, `pending_extension(rows)` and `len(rows)`,
  which are pure functions built for exactly this. Or annotate them as subqueries, the way
  `latest_design_transition()` does.
- **Not fixed here (R-12).** `tests_design_head_rejection_inert` (g) pins only that the
  ledger read adds zero queries, at 5 and at 86 rows. It deliberately does not pin the
  per-row growth, so the fix will not break it.

### ~~D22 — HARD REQUIREMENT OF 3.1b-2c: the Design Head's return-to-PM remark is stored and read by nothing~~ — **CLOSED by prompt 3.1b-2c**

#### CLOSED 13 Sep 2026 BY PROMPT 3.1b-2c

`design_pm_approval_queue` annotates its existing query with the **latest transition INTO
`awaiting_pm_approval`**: its reason code, remark and time. The remark shows on the row only
when that arrival was the Head's return (`REASON_DESIGN_HEAD_RETURNED_TO_PM`).

- **Why not filter on the reason, as this entry proposed.** A package can be returned by the
  Head, rejected again, sent back to the designer and brought to the PM by a second Head
  pass. The old return row is still on the ledger, so a reason-keyed read would show that
  stale remark on a fresh package. `tests_design_pm_gate_live` e3 drives exactly that cycle
  and asserts the remark is gone.
- **Cost:** zero added queries. Three correlated subqueries ride on the one SELECT the queue
  already ran: 7 queries at 1 row, 11 at 3 rows, before and after. The queue's own per-row
  cost predates this and is §D29.

The original entry follows.

Recorded by prompt 3.1b-2b (its D-l), 13 Sep 2026. **This is not a deferral.** It is D14's
kind of requirement, owed by the session that makes the path reachable.

- **What exists.** `design_head_return_to_pm` requires a remark and writes it, and only it,
  on the transition's ledger row (`reason_code=REASON_DESIGN_HEAD_RETURNED_TO_PM`). It is
  the Head's reason for overruling the PM, and it has no other home: the action writes
  nothing to the attempt.
- **What reads it.** Nothing. The PM's queue was MAY-NOT in 3.1b-2b.
- **The requirement.** 2c is already opening `design_pm_approval_queue`. It must annotate
  the queue with `latest_design_transition('remark', reason_code=
  REASON_DESIGN_HEAD_RETURNED_TO_PM)` and show it on a row that came back from the Head.
  The helper is one query for the whole queue. A mandatory remark nobody reads is a form
  field, not a record.

### D23 — LIVE: a PM-rejected send-back counts as a due-date revision, and a missing Head-pass time moves nothing

Recorded by prompt 3.1b-2b (its D-m), 13 Sep 2026. These are the two costs of §D15's
decision (c), stated at the code in `_extend_due_date_for_pm_review()`. **Known, and
deliberately not special-cased.** **LIVE since 3.1b-2c, 13 Sep 2026:** the send-back is
reachable, so both costs are current behaviour.

- **The revision.** The moved date is a new commitment row. `revisions` (rows − 1) goes up
  by one, both on the Head's sites screen and in `attention_list()`'s "Due date revised ≥3
  times" band. So a site the PM has rejected more than once reads as a designer who keeps
  needing more time, when the PM caused the returns. A fix would tell the automatic row
  apart from a requested one, for example by reading its `change_reason` or its
  `proposed_by`, and that is a decision about what a revision means.
- **The null branch.** If attempt N has no `head_reviewed_at`, there is no span to measure,
  so nothing is added and a past date stays past: the site is overdue on arrival. A package
  reaches the PM only through the Head's pass, which stamps that field, so only a row
  written another way (a fixture, a backfill) can hit this. Pinned by
  `tests_design_head_rejection_inert` c3.
- **A second figure distorted by the same row (D32 caption prompt, 15 Sep 2026).**
  `design_analytics.m_extension_rate` counts assignments carrying more than one commitment,
  so the send-back's row also raises the quality page's "Due date extension rate" — a PM
  rejection reads as a designer asking for more time. Its caption ("more than one due-date
  commitment") is literally true and was left byte-identical: the mismatch is between the
  metric's NAME and what a send-back does to it, and the fix is the same decision as the
  revision bullet above.

### ~~D24 — both PM-gate inertness modules RETIRE after 3.1b-2c; do not amend them a fourth time~~ — **CLOSED by prompt 3.1b-2c**

#### CLOSED 13 Sep 2026 BY PROMPT 3.1b-2c — split, not deleted

The prompt first said to delete the three modules. They held 76 tests: 15 absence proofs and
61 behaviour tests. Deleting them would have discarded the 61 this entry said to keep, and
broken three importing modules. So they were **split**.

- **The 15 absence proofs were dropped:** each module's `InertnessTests` (5 + 5 + 5),
  together with `tests_design_head_rejection_inert` a1 and a2, which pinned the same chain.
- **The 61 behaviour tests moved verbatim** to `tests_design_pm_gate_fixtured.py`, with the
  two fixture bases. An AST comparison shows 61 in and 61 out, and exactly five changed
  units, each approved in advance:
  - three class renames (the modules shared `DeclarationTests`, `MirrorDerivationTests` and
    `LadderAnswerTests`; merged as they were, 9 tests would have silently vanished);
  - `RejectTests` test_01 and test_02, whose status moved to `pm_rejected` (3 asserts);
  - three fixture docstrings that cited the dropped allow-lists.
- **Repointed.** `tests_design_head_rejection_inert`, `tests_design_attempt_accounting` and
  `tests_design_zero_display` changed their import lines only.
- **The successor, as specified.** `tests_design_pm_gate_live` (a) parses every non-test
  `.py` under `projects/`, rather than grepping it, and asserts per-status set equality:
  - `awaiting_pm_approval` = {`design_head_qc_pass`, `design_head_return_to_pm`};
  - `pm_rejected` = {`design_pm_reject`};
  - `released` = {`design_pm_approve`}, plus the three seed commands, which write fixture
    rows and are named one by one.

  The three status writes computed at run time (`design_survey_upload` and
  `design_survey_link_set`'s `restored`, and `_open_next_attempt`'s `opening_status`) are
  enumerated and resolved, and none of them can produce a gate status. The parse found no
  fifth writer.

The original entry follows.

Recorded by prompt 3.1b-2b (its D-n), 13 Sep 2026.

- **How far they have been bent.** `tests_design_pm_gate_inert` and
  `tests_design_pm_rejected_inert` were written to prove "nothing writes this". Each has
  now been amended three times to admit a writer that is real but unreachable.
- **What they prove now.** A chain. `pm_rejected` has no product writer. The one product
  writer of `awaiting_pm_approval` (`design_head_return_to_pm`) refuses every other status.
  Both halves are asserted in `tests_design_head_rejection_inert` (a): the first by grep,
  the second by a POST per status in the choices.
- **A third module pins the same property:** `tests_design_pm_approval` `InertnessTests`
  (3.1b-1). It needed the same amendment in 3.1b-2b, for the same write.
- **The three modules are not duplicates, and they earned their keep.** Each amendment was
  a real new writer, appearing where the previous session had said none could, and each
  was caught by a failing test rather than by a reader.
- **After 2c,** the first link breaks by design, and the chain proves nothing. Retire all
  three modules' absence proofs in 2c in favour of 2c's live-path module. Keep only what
  still pins a behaviour, not what pins an absence: the declaration tables, the
  CHECK-constraint tests and the guard-parity tests.
- **THE SUCCESSOR IS SPECIFIED, NOT PROMISED.** 2c's live-path module inherits the job the
  retiring ones were doing. It MUST assert that the set of product writers of each gate
  status is EXACTLY the set of views intended to write it, as a set equality over enclosing
  function names, parsed (as `_apply_design_status_callers_passing` does), not grepped:
  - `awaiting_pm_approval` — `design_head_qc_pass` (after 2c's flip) and
    `design_head_return_to_pm`;
  - `pm_rejected` — `design_pm_reject` (after 2c's retarget);
  - `released` — `design_pm_approve` alone.

  A writer added later then fails a test in the session that adds it, which is exactly what
  the three retiring modules did. Without that assertion, retiring them removes the only
  thing that has caught these writes.

### ~~D25 — the Head decides on a PM rejection without the PM's words on the screen where he decides~~ — **CLOSED by prompt 3.1b-2c**

#### CLOSED 13 Sep 2026 BY PROMPT 3.1b-2c

`design_qc_review` now fetches its `DesignAssignment` with one annotated query. The query
carries the PM's latest rejection remark and time, and it **replaces** the
`project.design_assignment` read the view used to make. The row is then seated back in the
reverse-relation cache, so the BOQ predicates do not re-query.

- **Why the reason filter is exact here.** At `pm_rejected` it cannot be stale:
  `design_pm_reject` is the only writer of that status.
- **What the Head sees.** The remark is on the Head's two-action card, beside the actions
  that answer it. The link-out to the sites screen is gone.
- **Cost, measured:** 28 queries at `pm_rejected` before and after, 28 at `in_qc`, and 26
  on a released site.

The original entry follows.

Recorded by prompt 3.1b-2b, 13 Sep 2026, from its own build.

- **Where the remark is.** The PM's rejection remark is on `design_head_sites`' row, read
  by `latest_design_transition()`. The two actions are on `design_qc_review`, whose view
  was admitted for ONE flag and nothing else.
- **What the Head sees there.** The action card says the remark is on the tender's sites
  screen and links to it. So the Head reads the PM's reason on one screen and answers it
  on another.
- **The fix.** One more key on `design_qc_review`: the same helper, annotated on a
  one-row `DesignAssignment` query.

### D26 — the send-back's own ledger row carries no reason code and no remark

Recorded by prompt 3.1b-2b, 13 Sep 2026.

- **Where the row comes from.** The `pm_rejected` → `in_design` / `arka_submitted` row is
  written by `_open_next_attempt()` through `apply_design_status()`. That function takes
  neither a reason code nor a remark, and it is shared with three live paths, so it was
  called and not edited.
- **Why it will bite.** The machine record of the most consequential transition in this
  workflow says only `pm_rejected` → `in_design` (or `arka_submitted`). Nothing on the row
  says why.
- **WHERE THE REASON ACTUALLY LIVES — THE REASON WAS CAPTURED, just not on this row.** It
  is on the DesignAttempt the Head sent back: the one with `assignment` = this row's
  `subject_id` and `attempt_number` = (the new `current_attempt_number` − 1). Read
  `pm_rejection_category` (whose rework it is, by group), `pm_rejection_remarks` (the
  Head's words to the designer) and `redo_required` there. The PM's own words are the
  EARLIER ledger row on the same subject with `reason_code=REASON_DESIGN_PM_REJECTED`
  (`latest_design_transition()` reads it). A session reading only the ledger must look
  there before concluding the reason was never captured.
- **How to identify the row.** `from_status='pm_rejected'`: it is the only exit from that
  status to a working status. The ActivityLog carries `design_attempt_opened_pm_rejected`
  and `design_head_sent_back_to_designer`.
- **The same gap on the QC-failure loop.** The same function writes the same empty
  reason code there, and has since Session D. If ledger readers come to need reasons on
  attempt-opening rows, give `_open_next_attempt()` optional reason and remark
  parameters, for all its callers at once.

### ~~D27 — five comments still cite the three grep proofs 3.1b-2c retired~~ — **CLOSED by the comment-correction prompt**

#### CLOSED 13 Sep 2026 BY THE COMMENT-CORRECTION PROMPT

None of the five says "by grep" any more.
- The two `models.py` comments name each status's product writers and cite
  `tests_design_pm_gate_live` (a).
- The admin comment names `design_pm_approve()` as the two fields' one product writer.
- The two test docstrings name the fixture where it now lives:
  `tests_design_pm_gate_fixtured.PmGateBase._park_in_pm_gate`.

A sixth was found and fixed with them: `tests_design_head_rejection_inert`'s module docstring
cited the "two inert modules" and their "walks". The original entry follows.

Recorded by prompt 3.1b-2c, 13 Sep 2026. **Recorded, not fixed.** Each sits in a file or
at a name outside that prompt's MODE.

- **`models.py:3264` and `:3273`.** They say `tests_design_pm_gate_inert.py` and
  `tests_design_pm_rejected_inert.py` prove "by grep" that nothing writes
  `awaiting_pm_approval` or `pm_rejected`. Both modules are gone, and both statuses have
  product writers.
- **`admin.py:457`.** It gives the same grep proof as the reason the admin fields are
  read-only. The reason still holds: `tests_design_pm_gate_live` (a) would see an admin
  writer only if it wrote through the product. The citation is stale.
- **`tests_design_attempt_accounting.py:20` and `tests_design_zero_display.py:24`.** Both
  docstrings name `tests_design_pm_gate_inert.PmGateBase._park_in_pm_gate` as the only
  writer of the status. The fixture now lives in `tests_design_pm_gate_fixtured`, and the
  status has product writers. 3.1b-2c's MODE allowed import lines only in those modules.

**Fix:** repoint each comment at `tests_design_pm_gate_live` (a), and drop "by grep".

### ~~D28 — `design_views` still describes the gate before it went live, in eight places~~ — **CLOSED by the comment-correction prompt**

#### CLOSED 13 Sep 2026 BY THE COMMENT-CORRECTION PROMPT

All eight were corrected:
- The two section headers read "LIVE SINCE PROMPT 3.1b-2c, 13 Sep 2026".
- `design_qc_pass` names `design_pm_approve()` as where release happens.
- `latest_design_transition()` lists three live consumers, with §D18 as the one planned.

Most were one pattern: a claim, dated against a prompt number, that a gate status or field is
unreachable. The same sweep corrected 36 FALSE statements in all, in `design_views`, `models`,
`admin`, `design_metrics`, `design_analytics` and three test modules. It also
corrected four more in `urls.py` (three) and `views.py` (one), whose MODE was widened for
exactly those lines. Every edit was proven comment-only by an AST comparison against 1f2a137.
The original entry follows.

Recorded by prompt 3.1b-2c, 13 Sep 2026. Comment-only; no behaviour. None of these names
was in that prompt's MODE, and every statement below has been false since that commit: six
say the PM gate is unreachable, and two say where release happens or who reads the ledger.

- **`:4079-4080`, the §12b section header:** "INERT UNTIL PROMPT 3.1b-2 … no code path
  writes that status yet: design_head_qc_pass() still releases directly".
- **`:4194`, `design_pm_approve`'s docstring:** "It reaches nothing today".
- **`:4297`, the §12c section header:** "INERT UNTIL PROMPT 3.1b-2c … nothing writes that
  status".
- **`:4394`, `design_head_return_to_pm`'s docstring:** "Nothing shows it to the PM yet".
  Since 3.1b-2c the PM's queue shows it.
- **`:2661`, `design_site_workspace`:** "inert until prompt 3.1b-2 makes that reachable".
- **`:5210`, the `_DESIGNER_ACTIONS` comment on `DESIGN_PM_REJECTED`:** "Unreachable until
  prompt 3.1b-2b".

Also stale: **`design_qc_pass`'s docstring (`:3772`)** says "release happens at
design_head_qc_pass() or not at all". Release is now `design_pm_approve`'s, and
`design_qc_pass` was MAY-NOT in 3.1b-2c. The same claim, shown to users, was FIXED in
`qc_review.html` in that commit under its B6: the Head's button read "Pass & release" and now
reads "Pass & send to the PM", and the gate-1 help text and two template comments were
corrected with it.

Also stale: **`latest_design_transition()`'s docstring (`:1004`)** says "TWO CONSUMERS".
There are now four. `design_head_sites` and `design_qc_review` read the PM's rejection,
`design_pm_approval_queue` reads the latest arrival at the PM, and §D18 remains unbuilt.

### D29 — the PM approval queue costs two queries per row

Recorded by prompt 3.1b-2c, 13 Sep 2026. **Predates that prompt.** It came with the queue
in 3.1b-1, and 3.1b-2c's remark read added nothing to it.

- **Measured** on the local dump as nirankar, with rows parked in a rolled-back
  transaction: 5 queries empty, 7 at 1 row, 11 at 3 rows. The cause is
  `_current_attempt(assignment)` and `_current_arka(attempt)` per row.
- **Why it matters now.** 3.1b-2c makes the queue live, and nirankar holds 86 sites. A
  tender passed in bulk would put about 177 queries on one screen, the same shape as §D21.
- **The fix pattern.** Prefetch `attempts` and `attempts__arka_submissions`, and pick the
  current ones in Python, as `design_head_sites` does for its gate-1 flag.

### ~~D30 — three recorded costs went live with 3.1b-2c~~ — **CLOSED by the comment-correction prompt**

#### CLOSED 13 Sep 2026 BY THE COMMENT-CORRECTION PROMPT

§D16, §D17 and §D23 now carry LIVE in their headings and say so in their bodies. §D8's risk
and timing were reworded the same way. D30's reminder has nothing left to remind. The
original entry follows.

Recorded by prompt 3.1b-2c, 13 Sep 2026. Nothing new here: these are reminders that
entries written as "inert until 3.1b-2c" now describe production behaviour.

- **§D16.** `_attempt_history.html` still has no branch for an attempt opened with reason
  `pm_rejected`. From 3.1b-2c every send-back produces one, so it renders with no reason
  border or label. `m_first_pass_rate` still does not count a PM-rejection send-back
  against first-pass; that product decision is still open.
- **§D17.** A `pm_rejected` site is the designer's current load again while the Head holds
  it.
- **§D23.** Each send-back that moves the date adds a due-date revision.

### ~~D31 — `tests_design_part9.test_head_pass_releases` is named for what the pass no longer does~~ — **CLOSED by the comment-correction prompt**

#### CLOSED 13 Sep 2026 BY THE COMMENT-CORRECTION PROMPT

Renamed to `PackageGateTests.test_head_pass_hands_to_the_pm`. The only other reference to the
old name anywhere in the repository was this entry's heading. The method's one-line docstring
was already true and is unchanged. The original entry follows.

Recorded by prompt 3.1b-2c, 13 Sep 2026.

- **What changed.** Its expectation was rewritten to `awaiting_pm_approval` with both
  release stamps null. The prompt admitted expectation-only changes to existing tests, so
  the name was kept, and a one-line docstring says what it now pins.
- **The fix.** Rename it (for example `test_head_pass_hands_to_the_pm`) the next time the
  module is open.

### ~~D32 — `METRIC_CATALOGUE`: user-visible text describing a metric omits a source that went live with 3.1b-2c~~ — **CLOSED by the D32 caption prompt**

#### CLOSED 15 Sep 2026 BY THE D32 CAPTION PROMPT

A full sweep of every catalogue entry, group blurb, caption, header, legend and tooltip on
the quality analytics page and the tender dashboard found 4 FALSE and 6 INCOMPLETE; the rest
are ACCURATE. Nine were corrected; the tenth (`extension_rate`) was dropped by decision and
moved to §D23.

- **Corrected.** `rework_multiplier` and the tender dashboard's Rework footer now name the
  Group A PM rejection. `error_distribution` names three sources, and its legend says the
  package count includes PM rejections. `group_b_failures` and the Group B blurb say "Never
  counted as designer rework" instead of "Counted here and nowhere else" / "Never folded into
  any designer figure", both false since Part 10: a Group B loop costs first-pass and counts
  in the QC and Head failure rates. The `m_change_request_rate` docstring no longer names a
  "Released sites" column that does not exist.
- **Deleted, not rewritten.** The Group A blurb's "Group A causes only — a bad survey or a
  moved brief never appears here" (first-pass and both failure rates count every loop
  whatever its category; the proposed replacement was itself false, because rework also
  counts uncategorised pre-Part-9 failures). The Group C blurb's "counted against the PM who
  moved it": the proposed "Change requests count against the PM who raised them" failed a
  literal check — see §D41.
- **The Rework footer kept its old sentence.** `tests_design_zero_display` pins it verbatim,
  and no test expectation could change, so the PM rejection is a new sentence after it
  rather than an insertion into it.
- **Proof.** `ast_proof` over `design_analytics.py` against `683a0ea`: identical outside
  caption constants, with exactly the seven intended ones changed; the rule is in the
  session report. `manage.py design_figure_snapshot` (committed for this purpose) is
  byte-identical before and after on the local database. `tests_design_metric_captions`
  asserts the corrected text in the rendered HTML of both screens.

The original entry follows.

Recorded by the comment-correction prompt, 13 Sep 2026. It was §D16's fourth bullet, and it
now has its own entry because of its class.

- **THIS IS USER-VISIBLE TEXT DESCRIBING A METRIC, NOT A COMMENT.** The descriptions in
  `design_analytics.METRIC_CATALOGUE` render on the quality analytics page. praveen reads them
  and checks figures against them. That kept them out of a session whose safety property was
  "nothing visible changed".
- **Where it belongs.** Prompt 3.1b-3, or a session of its own. **Fix it BEFORE the deploy:**
  before 3.1b-2c reaches Railway.
- **What changed underneath it.** Since 3.1b-2c, a PM rejection that the Design Head classifies
  Group A IS charged as designer rework. `classify_attempt_causes()`' `ATTEMPT_REASON_PM_REJECTED`
  branch reads `pm_rejection_category`. `_failure_rows()` also emits a third source, labelled
  `'PM'`. That is correct behaviour; the descriptions predate it, so the on-screen text now
  omits a live source of the number.
- **The sentences, verbatim:**
  - `rework_multiplier`: *"Designer-caused attempts per finished site (released, or awaiting PM
    approval): attempts a Group A failure opened, plus uncategorised pre-Part-9 QC failures. The
    initial attempt, and Group B, Group C and PM-change attempts, are not counted, so a clean
    record reads 0."* It omits the Group A PM rejection, which is counted. A reader takes
    "failure" to mean a gate failure.
  - `error_distribution`: *"Count by Group A category, team-wide and per designer, across both
    package failures and Arka rejections."* There are three sources now. "Both" is the nearest
    the catalogue comes to being false.
  - `first_pass_rate`: *"Finished sites (released, or awaiting PM approval) that no QC failure
    sent round again, per designer and team-wide. A reopen for a PM change request does not
    cost first-pass."* This matches the code, which counts `qc_failed` only. It is silent on
    PM rejections, which is §D16's open product decision. Settle that first.

### ~~D33 — the authority document says `DesignAssignment` is not instrumented, and it has been since the transition-ledger session~~ — **CLOSED by the execution-model v1.4 session**

#### CLOSED 15 Sep 2026 BY THE EXECUTION-MODEL v1.4 SESSION

`docs/execution-model.md` is now v1.4. §13 lists `design_assignment` in its Instrumented table
(16 statuses, migration `0079`), and the "NOT instrumented" row is gone. §5's design-workflow
row no longer says a PM approval gate does not exist: it states the one exit from `released`
and its writers per branch, and a new block at the end of §5's OPEX-only subsection describes
the gate. That block is scoped in its heading to local `main` from `1f2a137`, and says the gate
is unreachable on deployed `6cdb61e`. The same pass corrected 35 other claims and re-verified
every ✔ by grep: 47 held and 5 were false. The false ones were corrected and re-ticked. The
`tests_status_transition` docstring half of this entry is a `.py` file and was outside that
MODE; it is recorded in §D36. The original entry follows.

Recorded by the comment-correction prompt, 13 Sep 2026. **Older than the PM-gate run.**

- **What it says.** `docs/execution-model.md` §13's "NOT instrumented" table lists
  `DesignAssignment` with "14 (`DESIGN_ASSIGNMENT_STATUS_CHOICES`)" and "It is a session of its
  own." `tests_status_transition`'s module docstring says the same.
- **Why that is false.** Every design status write goes through `apply_design_status()`, which
  calls `record_transition()` in the same transaction. `tests_design_transition_ledger` pins
  that, and `latest_design_transition()` reads the ledger back on three screens. There are 16
  statuses, not 14.
- **What was changed.** The comment-correction prompt deleted only the count, "(fourteen
  statuses)", from the test docstring, because that was the part the PM-gate run had made
  false. The rest of that sentence, and §13 itself, need `docs/execution-model.md` open, which
  was outside its MODE.
- **The same document, the same kind.** §5's design-workflow table row says "a PM approval gate
  does not exist ✔". The gate has existed since 3.1b-1 and has been live since 3.1b-2c.
- **Why it matters.** This is the authority document on the ledger. A reader deciding whether
  design transitions can be queried for dwell time is told they cannot.

### D34 — true-but-stale statements left standing on purpose, which a reader may still trip on

Recorded by the comment-correction prompt, 13 Sep 2026. Each was classified STALE-BUT-TRUE and
left byte-identical. They are recorded so the next reader knows they were seen.

- **`permissions.py`.** "his released sites" (the site-groups section header) and "the sites he
  released" (`user_can_view_site_groups`). Since 3.1b-2c the Head passes and the PM releases.
  The rationale for the Head's read access is unchanged.
- **`tests_design_head_rejection_inert`.** The module name and the class `InertnessChainTests`
  still say "inert"; the class now holds only a3, a behaviour test. Two fixture docstrings are
  in the future tense ("as 3.1b-2c will leave one", "as the PM's reject will from 3.1b-2c").
  Renaming a module or a class was outside that MODE.
- **`design_views.design_my_sites`.** The `is_released` comment, "Read ONLY by the Design Hold
  control", is §D3's second bullet. It is false on its own and corrected by the paragraph
  directly below it.
- **`design_metrics.classify_attempt_causes`.** "The send-back-to-designer path in prompt
  3.1b-2b MUST require the category" is a requirement that is now met:
  `design_head_send_back` refuses a blank category.
- **`design_views`, the comment above `DESIGN_MIRROR_STATE_MAP`'s In Progress block.** "Which
  of the nine it is": the block has twelve entries. It was already false before the PM-gate run
  began (ten entries before prompt 3.1a), so it was outside that sweep.
- **Migrations 0085 and 0086.** Their "NOTHING WRITES…" docstrings describe each migration as
  it was written, which is the correct tense for a migration.

### D35 — `staticfiles/` is tracked in git, so any local `collectstatic` dirties the tree

Recorded by the execution-model v1.4 session, 15 Sep 2026. **Recorded, not fixed.**

- **What happens.** `staticfiles/` is committed. Any local `collectstatic`, including a
  deployment rehearsal, rewrites it. At the start of this session that showed as 130 `M`
  entries under `staticfiles/admin/`. `git diff --shortstat -- staticfiles` was empty, with
  and without `--ignore-cr-at-eol`, so they are stat-only phantom modifications, not content.
- **Why it bites.** A careless `git add -A` or `git commit -a` would commit generated files.
  It is also why this session's pre-flight had to stop at its "git status clean" check. The
  session committed its two `.md` files by explicit path.
- **The open decision, for its own session.** Untrack `staticfiles/` (with a `.gitignore`
  entry) if Railway runs `collectstatic` at start, which R-22 says its start command does. Or
  keep it tracked and document the phantom modifications. Check the deploy first: this
  session did not.

### D36 — PARTLY CLOSED (three of seven, 16 Sep 2026): false claims the v1.4 sweep found and its MODE could not fix

#### PARTLY CLOSED 16 Sep 2026 BY THE v1.5 SESSION — the three in `execution-model.md`

The last three bullets are fixed and `docs/execution-model.md` is now **v1.5**. The **first four
are unchanged and still live**, each outside that session's MODE, which admitted only
`execution-model.md` and section D: three name a `.py` file or another document, and the
fourth is §B27's premise, in **section B of this file**.

- **§5's mirrors/progress claim** → replaced. R-20's PROGRESS half counts mirrors, so the three
  sourceless mirrors sit in every OPEX progress denominator and can never reach its numerator.
- **§12's "Nine of the 22"** → **corrected to "Eleven of the 22", and the date kept.** This
  session established what v1.4 could not: **nine was wrong when it was written.** The seed live
  on 31 Aug (`0075` at `2051f89`) gave the Site Engineer **11 of 22**; today's gives 11 of 23.
  A transcription error in a dated entry is not stale history, so it was corrected rather than
  annotated — the 1 Sep seed change carries a separate dated note beneath the row. Rewriting a
  *decision* to match today's code is what §12 guards against; correcting a *figure* preserves
  the entry's moment.
- **§13's "this table is the whole answer"** → replaced with a counted statement. Sixteen models
  carry a field named `status`; thirteen are in §13's two tables; three — `Program`,
  `DesignSubmission`, `NotificationLog` — are in neither.

The original entry follows.

Recorded by the execution-model v1.4 session, 15 Sep 2026. Each was confirmed by grep in that
session. The first four are outside a MODE that admitted only `docs/execution-model.md` and
this section. The last three are in `docs/execution-model.md` but outside the 37 corrections
that were approved one by one, so they were left byte-identical.

- **`projects/tests_status_transition.py`'s module docstring** still lists `DesignAssignment`
  beside `DesignAttempt` and `PaymentRequest` as not instrumented. It is §D33's second half;
  a `.py` file.
- **`docs/PHASE_0_COMPLETION.md`**, "The design module's transitions": *"Not instrumented, and
  this is the largest deliberate gap in the ledger. `DesignAssignment` has 14 statuses"*.
  Since migration `0079` it is instrumented, and it has 16.
- **`docs/EXECUTION_PROMPT_LOG.md`, row B21**, and **`docs/OPEX_task_template_spec.md` §2 rule
  3** (*"NOT BUILT for any of the 8, Design included. No derivation hook exists yet"*). The
  Design mirror has been derived since `70a6684` (5 Sep 2026), and the four delivery mirrors
  since `0a106bb` (7 Sep 2026).
- **§B27's premise** (section B, outside this session's mandate). Its heading says the four
  delivery mirrors need SCM's catalogue mapping **and** B-18. `sync_delivery_mirrors()`
  derives them from delivery challan lines through `DC_CATEGORY_TO_MIRROR_CODE`, with neither.
- ~~**`execution-model.md` §5, OPEX template:** *"it is why mirrors leave both halves of a
  progress fraction (R-20)"*. Since prompt 1.6, R-20's progress half counts mirrors.~~
  **FIXED in v1.5.**
- ~~**`execution-model.md` §12, the 31 Aug `enable_cascade_scheduling` entry:** *"Nine of the 22
  OPEX tasks are the Site Engineer's."* The 0075 seed gives the Site Engineer 11 of 23 today.
  It is a dated decision-log row, and whether "nine" held on 31 Aug was not checked.~~
  **FIXED in v1.5 — and the unchecked half was the important one: it did not hold. 11 of 22.**
- ~~**`execution-model.md` §13:** *"so this table is the whole answer"*. v1.4 added the approved
  `PunchPoint` row, but `Program`, `DesignSubmission` and `NotificationLog` also carry a
  `status` field and appear in neither of §13's tables.~~ **FIXED in v1.5, and counted: 16
  models carry a `status` field, 13 are in the tables, those 3 are not.**

### D37 — THE IN-APP SWITCH DOES NOTHING, so the PM gate's notifications have no off switch

Recorded by prompt 3.1b-3 (its D-o), 15 Sep 2026. **Recorded, not fixed.**

- **What exists.** `SystemSettings.in_app_notifications_enabled` is on the admin master-switch
  screen (`admin_master_switches`, labelled "In-app notifications"). The screen saves it and
  logs the change.
- **What reads it.** Nothing. `send_notification()` reads `whatsapp_enabled` and
  `email_enabled`, and its `in_app` branch calls `_send_in_app()` with no check. There is no
  per-user in-app preference either. An administrator can toggle the control, see it save,
  and change nothing. A control that lies is worse than a missing one.
- **The consequence for 3.1b-3.** The PM gate's four notifications have no off switch. If they
  misfire on production, the only remedy is a deploy.
- **Why not fixed here.** The fix is one check in the chokepoint's `in_app` branch. It changes
  the behaviour of all sixteen existing call sites, and of the assignment cooldown
  (`utils._assign_notification_state`), which counts in-app `NotificationLog` rows *because*
  that channel is never skipped. It belongs in its own session.

### D38 — THE BELL ALREADY LINKS TO THE WRONG HOST

Recorded by prompt 3.1b-3 (its D-p), 15 Sep 2026. **LIVE, user-facing, pre-existing. Recorded,
not fixed.**

- **What happens.** The existing call sites build one message for all three channels, with
  `https://horizon-solar-pms-production.up.railway.app` appended to the text. For example, the
  payment milestone in `_apply_task_status_change`, `_notify_boq_acknowledged`, and both
  assignment messages in `utils._notify_assignment` (`SITE_BASE_URL`).
- **Why it reaches the bell.** That text is stored as `Notification.message`. On production,
  a user reading the bell at `pms.horizonrenewablepower.in` sees a Railway URL in the body, and
  clicking it goes to `railway.app`. `Notification.link` is relative and correct; the message
  text is not.
- **How 3.1b-3 avoided it.** Its links are relative, from `reverse()`, and no message carries a
  URL. `tests_design_gate_notifications` a3 pins that.
- **The fix.** Separate the in-app text from the email text at each call site, or strip the
  URL for in-app. A single `APP_BASE_URL` (§G2) would fix the host for email. It is its own
  session.

### D39 — SCM is told nothing when a design is released

Recorded by prompt 3.1b-3 (its D-q), 15 Sep 2026. **Deliberately out of scope.**

- **What happens.** `design_pm_approve` releases the site into SCM's pool, and nobody in SCM
  hears about it. SCM finds released sites by opening `post_qc_pool`.
- **Why it was left out.** 3.1b-3 had four firsts: the module's first notifications, first
  recipient resolution, first channel restriction and first logged fan-out. SCM would be a new
  audience on top of them.
- **Open questions for its own session.** Who in SCM: every active `role='SCM'` profile (the
  notifications spec §2.5), or an assignee? How to avoid a burst when a tender releases many
  sites in a day? And in-app only again, or email, now that `email_enabled` is ON in
  production?

### D40 — `project_managers()` drops inactive coordinators; the permission check does not

Recorded by prompt 3.1b-3 (its D-r), 15 Sep 2026. **Recorded, not fixed.**

- **The difference.** `permissions.project_managers()` returns the assigned PM plus
  `coordinators.filter(is_active=True)`. `can_approve_design_release()` is
  `user_can_manage_project()`, which checks `coordinators.filter(pk=...)` with no `is_active`.
  So an inactive coordinator can still approve or reject a design and is not told when one
  arrives. The assigned PM is not filtered on `is_active` by either.
- **Why it is immaterial today.** No OPEX site has a coordinator (0 of 102 on the local dump).
  It will matter once MPUVNL sites get coordinators.
- **Pinned.** `tests_design_gate_notifications` b1 asserts that a lapsed coordinator is not told.

---

### D41 — what the D32 caption sweep found outside its MODE

Recorded by the D32 caption prompt, 15 Sep 2026. **Recorded, not fixed.**

- **The change-request rate is keyed to two different people.** `m_change_request_rate`
  puts each ACCEPTED request in the row of `cr.requested_by`, and divides by the finished
  sites whose `project.assigned_pm` is that same person. `user_can_request_design_change()`
  routes through `user_can_manage_project()`, which also admits every active Project
  Coordinator. So a coordinator's accepted request lands in a row with no finished sites
  ("Insufficient data (n=0)"), and the site's PM's row never shows it. The column header
  "Requesting PM" and the catalogue's "per REQUESTING PM" read as PMs only. This is why the
  Group C blurb's attribution clause was deleted rather than rewritten. **Decide** whether a
  coordinator's request is charged to the site's PM.
- **`error_distribution.by_source` has two keys, not three.** The PM-source rows from
  `_failure_rows()` carry `source='Package'`, so they are inside the package count. The
  legend now says so; splitting them into their own count is a code change.
- **`m_change_request_rate` rows still carry `released`, which nothing renders.** Left in
  place: the return shape was frozen for the caption session.
- **`tests_design_part10` line 427 docstring,** "the Group B failure appears in B and nowhere
  else", echoes the catalogue claim that session deleted. What the test asserts is narrower
  and still true; the docstring was outside the MODE.

### D42 — what the v1.5 correction session found outside its MODE

Recorded by the execution-model v1.5 session, 16 Sep 2026. Its MODE admitted
`docs/execution-model.md` and this section only — no `.py` file, template, migration or test.
Everything below was confirmed by grep in that session.

- **A caption is pinned word for word by a test, so any future caption change is also a test
  change.** `tests_design_zero_display.test_the_captions_describe_what_the_figures_now_measure`
  asserts whole caption sentences with `assertIn` on the rendered HTML — *"Sites and kW are
  current load: they exclude finished work (released, or awaiting PM approval)."*, the
  designer-caused-attempts divisor, *"a clean record reads 0.0&times;"*, *"A dash means the
  designer has no finished site yet."* — and `test_the_false_tooltips_are_gone` asserts the
  absence of others. §D32's close-out records this for the Rework footer alone ("`tests_design_zero_display`
  pins it verbatim"), which reads as one local obstacle; it is general. **A session told to
  change a caption and forbidden to touch tests cannot do both.** Scope the caption work and the
  test edit into one prompt, or the session stops. Belongs beside §D41's bullets, which the
  caption session recorded on 15 Sep; this is the same class of finding, one session later.
- **`execution-model.md` §18 says the suite is 1566 tests; it is 1598.** `683a0ea` and `5e51080`
  added two test modules after that line's "(15 Sep 2026)" stamp. **Left alone by decision**, not
  oversight: a test count goes stale every session — the same class as the line-number anchors
  v1.4 removed rather than refreshed — and the number already carries its own date adjacent to
  it. Checked against the condition for changing it: §18's baseline that a future session is
  meant to compare against is the **failure set**, not the count (§B31's trigger is "a failure
  that is not already on the baseline list"). The count is colour beside a runtime. The same
  applies to the derived "1,564" further down the section.
- **The deployment-state sweep, so a future session need not re-grep it.** Nine claims in
  `execution-model.md` about what is deployed, held or pending were checked on 16 Sep 2026. Two
  were false and are fixed in v1.5 (§5's deploy-gate sentence; §12's 13 Sep row, which was true
  when written and now carries a dated note). One is the §18 count above. **Six were verified
  true and deliberately not touched:** §5's "on deployed `6cdb61e` the gate is present and
  unreachable"; §5's Design-workflow row and §8's B-06 row, both of which split local `main`
  from deployed `6cdb61e`; §5's "`origin/main` carries migrations `0075` through `0086`"
  (`git ls-tree` confirms 0086; its production half is dated, attributed to the product owner
  and already flagged in-text as not grep-verifiable); §5's "no template version authors any
  edge today" (`TaskTemplateTaskDependency` appears in migration `0073` and in no view, form,
  admin or url); and §5's "`materialise_task_dependencies()` — **Not yet called from anywhere**".
- **The method note this session owes its own successor.** Its instruction was to treat §12's
  "Nine of the 22" as a dated entry that must not be corrected, on the stated assumption that
  nine held on 31 Aug. **Grep showed the assumption was false** — the 31 Aug seed gave 11 of 22
  — which reversed the handling from annotate to correct. An approved instruction is not a
  verified fact, and the rule that every claim be re-grepped in the session that writes it
  earned itself again here.

### D43 — what session 3.1c-i (one design-change window) left open

Recorded by session 3.1c-i, 16 Sep 2026. **Recorded, not fixed.** Every claim below was
checked by grep in that session.

- **Approve-then-change-request.** D-a opens released sites to a change request, so a PM can
  approve at the gate (`design_pm_approve`) and then raise one instead of rejecting. The two
  are charged differently: `classify_attempt_causes()` files an attempt opened by an accepted
  request as `CAUSE_PM_CHANGE`, which is never the designer's, while a rejection the Head sends
  back reads `pm_rejection_category` — Group A or a blank category charges the designer. **Watch
  for accepted change requests clustering soon after the same PM's approval.** For the record,
  `CHANGE_REQUEST_STATUSES` is `in_qc, qc_failed, in_design, arka_submitted, arka_rejected,
  artifacts_uploaded, awaiting_head_arka, awaiting_head_qc`: neither `awaiting_pm_approval` nor
  `pm_rejected` is in it, so the PM cannot raise while the package is with them. Approve first
  is the only route.
- **A request refused at acceptance is NOT stranded** — the audit prompt assumed no reject path;
  `design_change_request_reject` exists. But it stays pending until the Head rejects it with a
  reason; nothing else closes it, and `uniq_pending_change_request_per_attempt` refuses another
  raise on that attempt meanwhile.
- **The lock race is narrowed, not closed.** `site_group_lock()` takes no row lock: it resolves
  the group with `_group_or_404()`, runs its pending-request check outside any transaction, and
  saves with a plain `group.save()`. `design_change_request_accept()` now locks the SiteGroup row
  and then the DesignAssignment row and re-checks `project_boq_is_group_locked()`, so a group
  locked between a raise and its acceptance is caught. What remains: the lock view's pending check
  is not serialised against a raise, so a group can be locked over a request raised at the same
  moment (acceptance then refuses, and the Head must reject); and a lock whose check ran before an
  acceptance commits waits on the row lock, then locks and writes its "BOQ locked" activity line
  for a member list read before the site left. Closing it needs `site_group_lock()` to lock the
  group and member rows before checking — outside "precondition only", declined by decision (Q4).
- **Django admin can create a change request with no window check.** `DesignChangeRequestAdmin`
  has no `has_add_permission` override.
- **`_add_sites()` admits a site with a pending change request.** It checks `released` and the
  absence of a live procurement membership only. Since Q2 such a site then blocks its new group's
  lock, which the blockers panel on `site_group_detail` shows.
- **`site_group_lock()`'s refusal says "has"/"have" by `len(blockers)`**, a count of requests, not
  of the sites it names. Equal today because only one request per attempt can be pending.
- **`tests_design_pm_gate_fixtured.RejectTests.test_02`'s docstring** — *"A reopened site keeps
  its old released_at (audit P2)"* — is no longer true of a reopen by an accepted change request,
  which clears both stamps. It stays true of the QC-fail and send-back loops, which call
  `_open_next_attempt()` without `extra_fields`. Outside this session's MODE.
- **`post_qc_pool()`'s docstring** says a change request "returns the site to the queue rather
  than losing it". Since Q2 the site leaves its group only at acceptance, which moves it to
  `in_design`, so it returns to the pool only when it is released again. Outside the MODE.
- **The stored `removal_reason` still says "PM change request"** for an SCM-raised request
  (`CHANGE_REQUEST_REMOVAL_REASON`; rows already carry the value, so it was not reworded). The
  departures chip on `site_group_detail` now reads "Change request". The acceptance's activity
  line also still says "PM change request accepted"; only the raise's text was made neutral.
- **The SCM dashboard's OPEX pool (`_scm_opex_groups.html`) has no "Request design change" link.**
  Its rows come from `scm_opex_tender_rows()` via `dashboard_scm`, not from the two group views
  this session was admitted to.
- **§D41's first bullet is closed** by D-c: attribution to the site's `assigned_pm`, the column
  header "Site PM", and the catalogue description. Its `released`-key bullet is unchanged.
- **§D40 is unchanged and now reaches further.** An inactive coordinator passes
  `user_can_manage_project()`, so it has both the authority and the PM's window, before and after
  release. Not fixed and not pinned, by instruction.
- **`DESIGN_APPROVAL_AUDIT.md` rows 11, 12 and 26** describe the form saying "QC has not started"
  at `awaiting_pm_approval` and the "new scope of work" branch. Both are gone: the form now renders
  a `stage` explanation from the POST's own refusal code. The audit document is a dated record and
  was not edited.

### D44 — what session 3.1c-ii (change-request notifications) left open

Recorded by session 3.1c-ii, 17 Sep 2026. **Recorded, not fixed.** Every claim below was
checked by grep, AST or a read-only query in that session. Figures from the local dump are
from `solarpms_local`, a restored production dump, read on 16 Sep 2026.

- **Change-request email has no off switch of its own.** The three sends name
  `channels=['in_app', 'email']`. The only thing that stops the email is
  `SystemSettings.email_enabled`, which also stops the EOD digest and every other email, or a
  recipient's own `email_notifications`. The in-app half cannot be stopped at all (§D37).
- **A deliberate asymmetry, to review.** The four PM-gate notifications (3.1b-3) stay in-app
  only while change requests also email. A package waiting at the PM gate is told to the bell;
  a change request is told to the bell and the inbox.
- **The email link carries the ACTOR's host** (§D38 family). The link is
  `request.build_absolute_uri()` on the raise, accept or reject request, so an actor on the
  Railway-generated domain sends that domain to every recipient. On 16 Sep 2026 both the custom
  domain and the Railway-generated domain answered `/login/` with 200, so the link works but
  needs its own login. Whether `CSRF_TRUSTED_ORIGINS` lists the Railway-generated domain was
  not verified (no Railway CLI in the session); if it does not, a POST after following such a
  link is refused. `APP_BASE_URL` is §D38's fix and was not added.
- **A raise does not reach a Design Head's deputy** (D4). Deputies may triage
  (`user_has_design_head_authority()`), but `GATE_CHANGE_RAISED` reads active
  `is_design_head` holders only. No deputy is named on the dump today; one appointed later
  will not hear about requests they can decide.
- **The gate branches still notify an inactive assigned PM** (D5). `project_managers()` does
  not check the assigned PM's `is_active`, and neither the four gate branches nor that function
  were changed. Only the three change-request branches drop inactive profiles and inactive
  `auth.User` rows (`_change_request_audience()`).
- **`demo.designhead` is an active, global Design Head on production data** (profile 127 on the
  dump). It receives every change-request raise and may triage any real tender. An owner
  decision about the demo accounts, not code.
  - **Correction, D18 session, 17 Sep 2026. FALSE as written.** `demo.designhead` does NOT
    exist in production (the product owner's production check). It exists only in
    `solarpms_local`, whose `auth.User` row was created on 6 Sep 2026. No seed command in the
    repository creates that username today (`seed_opex_test_data`'s list has no Design Head,
    and `seed_scm_pilot` creates `scmpilot.designhead`). The demo Head is not a production
    recipient; the "only the demo Head has `email_notifications` on" count below is local and
    includes seeded users.
- **`NotificationLog`'s reason field is `error_detail`** (added by the D18 session, 17 Sep
  2026). There is no `reason` field. A query for why a notification was skipped or failed
  reads `error_detail`.
- **Email will rarely deliver without a people change.** On the dump, of the 2 active Design
  Heads only the demo Head has `email_notifications` on; of the 3 PMs with OPEX sites, only
  the demo PM; of the 4 active SCM profiles, 1. On MPUVNL (86 sites, one PM, no
  coordinators) a raise per site would produce 172 in-app notes and 86 emails, all to the demo
  Head.
- **The EOD digest's recipients are hardcoded** — already §G3 (`ADMIN_DIGEST_EMAIL` and
  `HR_DIGEST_EMAIL` reassigned to literals at the bottom of `solarpms/settings.py`). Still open.
- **The Zoho webhook's alert recipient is hardcoded.** `zoho_deal_closed_webhook` calls
  `send_raw_email(to_email=...)` with a literal personal address, and its body appends the
  Railway-generated domain. No setting names either value.
- **The accept path's group-owner lookup reads two FKs after the transaction.**
  `_change_request_group_owner()` follows `membership.added_by` and `membership.group.created_by`
  on the membership carried out of the atomic block. It does not re-read the membership (D6), but
  the people it names are read after commit, so an SCM deactivated in that instant is still told.
- **`message` is both the bell text and the email's plain-text part** (`send_notification()`).
  The absolute link is therefore in the HTML part only; a mail client that shows plain text
  alone shows no link. Separating the two needs a `send_notification()` change, which was out
  of this session's MODE.
- **`tests_design_part46.test_14`'s name still says "no_notification".** Its NotificationLog
  assertion now allows the two change-request labels and nothing else; the name was left so the
  test id did not change.

### D45 — a Design Hold is accepted over a pending change request

Recorded by the D18 session, 17 Sep 2026. **Recorded, not fixed.** Belongs to a change-request
session.

- **The gap.** A Design Hold is accepted over a pending change request: `design_mark_blocked`
  reads no request. Driven through the real endpoints (a PM raises the request, the designer
  places the hold), the hold was accepted at `in_design` and at `arka_submitted`.
- **Unreachable today.** A pre-release raise needs `qc_started_at` on the current attempt
  (`_pre_release_window_open()`). Its only product writer is `design_qc_start`, which moves the
  site to `in_qc`; `_open_next_attempt()` creates attempts without it (three seed commands also
  write it, on released fixtures). All four QC verdict views refuse while a request is pending
  (`_blocking_change_request()` in `design_qc_pass`, `design_qc_fail`, `design_head_qc_pass`,
  `design_head_qc_fail`), so a package cannot leave `in_qc` or `awaiting_head_qc` with one
  pending, except by the request's own triage.
- **Reachable only before §D13** (`969eed4`): a hold at `in_qc` or `awaiting_head_qc` was
  allowed then, and a pending request can sit at both. **Production had zero
  `DesignChangeRequest` rows on 17 Sep 2026** (product owner's Railway shell), so no historical
  instance exists.
- **The fix.** Refuse the hold while a change request is pending, and remove the
  `@unittest.expectedFailure` on
  `tests_design_hold_restore.HoldOverPendingChangeRequestTests.test_02_the_hold_is_refused_while_a_change_request_is_pending`.
  That test asserts the correct behaviour today and fails on an assertion, not an error;
  `test_01` in the same class pins its fixture.
- **Related:** `design_change_request_accept` has no current-status guard (§D47). An accept
  during such a hold would move the site out of `survey_returned` with no lift event, and
  `m_hold_duration` would count that hold as open for good.

### D46 — repair of sites already orphaned by §D18

Recorded by the D18 session, 17 Sep 2026. **A separate session.** The §D18 fix changes future
lifts only (R4).

#### NOT NEEDED as of 18 Sep 2026 — production has no orphan

The A8 blocks were run in production on 18 Sep 2026 (§D18): 12 design ledger rows from
2026-09-16 08:46:33 UTC, 0 hold or lift rows, 0 pre-ledger `design_blocked` ActivityLog rows,
0 open holds, no ORPHAN printed, and 0 sites on Design Hold. There is nothing to repair. **The
entry is kept, not deleted:** it is the method if an orphan ever appears (a hold lifted by
some path that bypasses §D18's fix, or the blind spot recorded in §D18).

The original entry follows.

- **Find them first, in production.** Run the two A8 console blocks from the D18 session (every
  ledgered lift, flagged ORPHAN where a hold from an Arka status was lifted to anything else;
  and every site currently on hold, with its from-status). Their output had not been reported
  when this entry was written (reported 18 Sep 2026: none; see above). A hold lifted before the ledger (`fc24728`) cannot be diagnosed
  from the ledger at all.
- **The shape: a management command, `--dry-run` by default,** writing through
  `apply_design_status()` so the repair leaves a `StatusTransition` (with an explicit repair
  `reason_code` and a remark naming §D18 and the hold row), an ActivityLog line and a mirror
  sync. It restores a site only if its current Arka still matches the status the hold was taken
  from, the same test as `_arka_matches_status()`.
- **List resubmitted sites; never revive them.** Where the designer has already resubmitted,
  the old version is `is_current=False` with its verdict pending for good, and the new version
  is live. Repair must not revive the old version.
- **Not the repair:** asking for a re-hold and re-lift (the Head cannot place a hold, §D47, and
  a designer's re-hold records a false "survey inadequate" hold in `m_hold_duration`), or
  Django admin (`DesignAssignmentAdmin` has `status` read-only, and an admin edit leaves no
  ledger row).

### D47 — what the D18 session found outside its MODE

Recorded by the D18 session, 17 Sep 2026. **Recorded, not fixed.**

- **The overdue clock runs during a Design Hold, and `design_mark_blocked`'s docstring says it
  stops** ("This stops their clock"). `design_metrics.is_overdue()` does not exclude
  `survey_returned`, and its docstring says it "does not extend the due date for time spent
  blocked"; `DESIGN_CLOCK_STOPPED_STATUSES` does not contain it either, so the date controls stay
  open. **A product question for Sudhir:** should a hold caused by the survey charge the designer
  lateness? It conflicts with the Group B/C principle behind the rework figures, which do not
  charge the designer for input problems. The docstring was deliberately not changed.
- **CAD upload and BOQ completion have no status guard.** `design_artifact_upload` requires only
  a current Arka (`_require_current_arka()`); `design_boq_complete` requires quantities. Both
  land while a site is `survey_returned`. §D18's R5 advances such a package on the lift.
- **The Head cannot place a hold.** `design_mark_blocked` accepts only
  `user_is_assigned_designer()`, which the Design Head does not satisfy.
- **The Arka admin edits verdicts with no ledger row.** `ArkaSubmissionAdmin` has
  `readonly_fields = ['submitted_at']` only, so `verdict`, `head_verdict` and `is_current` are
  editable, and an edit writes neither a `StatusTransition` nor an ActivityLog line.
  `DesignAssignmentAdmin` closed its own status field for the same reason.
- **`design_change_request_accept` has no current-status guard.** It checks the request's own
  verdict and the procurement lock, then calls `_open_next_attempt()` whatever the assignment's
  status. See §D45 for the one status where that matters today.
- **`CHANGE_REQUEST_STATUSES` includes `in_design` and `arka_submitted`, but a pre-release raise
  needs `qc_started_at`, which new attempts never carry.** Those two members appear unreachable
  for a raise, and so, by the same reasoning, may `arka_rejected` and `awaiting_head_arka` (the
  tuple's own comment already says so of the latter). Record only; confirm in the change-request
  session.

### D48 — `Project.assigned_design` readers still see OPEX, after the header selector stopped offering it

Recorded by the OPEX design-selector session, 20 Sep 2026. **Recorded, not fixed — by
instruction.** That session hid the header Design selector on OPEX (RESCO) sites in
`project_overview` and refused its `assign_design` POST. It changed **no permission helper,
no dashboard and not the CEO report**, and it did **not** null any existing
`assigned_design` value. This entry is the standing exposure that leaves behind.

**ALL COUNTS BELOW CAME FROM THE LOCAL DEVELOPMENT DATABASE on 20 Sep 2026, by read-only
query — NOT from Railway production.** They are the basis on which the change was judged safe
and they must be re-measured against production before any follow-up acts on them.

```
Residential  total= 44   assigned_design set= 32
OPEX         total=103   assigned_design set= 12   (11 not soft-deleted)
CAPEX        total=  1   assigned_design set=  1

OPEX DesignAssignments with a designer allocated: 11
  ...where project.assigned_design matches       : 11   (100%)
  ...where it is NULL: 0      ...diverged: 0
OPEX not soft-deleted, by status: Draft 95, Active 7
```

Two facts carried the decision. **The field is already design-module-owned in practice on
OPEX** — all 11 populated rows agree exactly with their allocation, so there was no
PM-entered data to preserve. And **only 7 OPEX sites could see the selector at all**, since
`can_assign_design` also required `status in ('Active','In Progress')` and 95 of 102 are
Draft; those 7 are precisely the sites where allocation is in flight and a PM save would
have done damage.

**The readers that still take OPEX rows, none of them changed:**

- `permissions.user_can_view_project()` — Design branch, the FK **or** task-holding. This is
  how an allocated OPEX designer sees the site at all.
- `permissions.user_can_view_project_boq()` — the same union.
- `permissions.user_can_edit_project_boq()` — **W-narrow: the FK alone carries the BOQ write
  gate, with no task-holding fallback.** This is why allocation must keep stamping the field,
  and why a second writer was worth removing.
- `views.dashboard_design()` and its revision counter — and OPEX is *explicitly exempted*
  from their status filter, so Draft OPEX sites are keyed off this FK too.
- `views.py` task drill-down Design scoping; the two BOQ lock-reason strings; the
  issue-assignee candidate helper.
- `reports.build_user_status_rows()` (the CEO report) — sweeps `assigned_design__isnull=False`
  with **no `project_type` filter**, so OPEX rows are in it. Read-only; unaffected by the
  hiding, and deliberately untouched.

`permissions.user_can_manage_project()` does **not** read this field (verified: `assigned_pm`
+ `coordinators` only).

**THE REPAIR PATH FOR OPEX IS NOW SINGULAR, and that is the real cost of this change.** With
the selector gone there is exactly one way to correct a wrong or stale `assigned_design` on an
OPEX site: **the Design Head reallocating through `_allocate_one()`** (design_views), which
re-stamps the field as part of moving `DesignAssignment.assigned_to`. The only thing that
*surfaces* a divergence to a human is **the `site_workspace.html` banner**, which tells a
designer their site's `assigned_design` names someone else. Banner to notice it, Head
reallocation to fix it — there is no other route, and in particular:

- `_allocate_one()` refuses once `assignment.status` is outside `REALLOCATABLE_STATUSES`, so a
  site whose design work has started **cannot be reallocated at all** today ("reallocation at
  this stage is not supported yet"). A divergence discovered after that point has **no repair
  path in the product** — only Django admin, which leaves no `StatusTransition` and no
  ActivityLog row.
- Nothing reconciles the two fields in the background. Migration 0051 was a one-off backfill,
  not a recurring job.

**If this bites, the shape of the fix** is a reconcile — either a management command
(`--dry-run` by default) that reports and optionally re-stamps `assigned_design` from
`DesignAssignment.assigned_to` on OPEX, or widening `REALLOCATABLE_STATUSES` so a Head can
reallocate a started site. Do **not** reinstate the PM selector: it is the second writer this
session removed.

**Not covered anywhere, and out of this session's MODE:** `project_detail.html:81` carries a
second `assigned_design` selector. It was left alone because §G8 records that template as
dead. If it is ever revived, it needs this same gate.

### D49 — what session B1 (SCM change-request routing, schema only) left open

Recorded by session B1, 25 Sep 2026. **Recorded, not fixed.** Every claim below was checked
by grep, AST or a local query in that session. Migration `0102` added four verdict values
(`with_pm`, `pm_rejected`, `withdrawn`, `corrected`), `origin`, the PM-stage, withdrawal and
correction fields, the `boq_corrections` M2M and five CHECKs; it widened
`uniq_pending_change_request_per_attempt` to `verdict IN ('with_pm', 'pending')` under the
same name.

*The hard dependency B1 recorded here — both templates' catch-all `{% else %}` rendering
every new verdict as "Awaiting the Design Head" — was closed by session B2a, which gave all
seven verdicts their own branch in both. See §D50.*

- **The new values are unreachable until B2.** No product code writes a new verdict, a
  `pm_decided_*`, `withdrawn_*` or `corrected_*` field, or a `boq_corrections` link. The one
  new column B1 writes is `origin`, in `design_change_request`'s create call. **Django admin
  can still set them**: `verdict` stays editable there (unchanged by decision), so an admin
  user can choose `with_pm` today. The CHECKs hold the row's shape; nothing holds its meaning.
- **B3 owns the wording and the metrics.** The four catch-all readers, found by B1's A3:
  1. `design_analytics.m_cr_rejection_rate`: its denominator is `len(crs)` over an unfiltered
     list, so every new-value row enters it.
  2. `design_analytics.m_cr_by_stage` reads no verdict at all and counts every row.
  3. `change_request.html`: the `{% else %}` after `accepted` / `rejected` (see the hard
     dependency).
  4. `_attempt_history.html`: the same, plus its `alert-secondary` / `alert-warning` class
     test, which is `== 'rejected'` / else.
- **Three metric defects the audit found, none caused by B1:**
  - **The rejection-rate denominator** counts every request, pending included, by design
    (its docstring says so). After B2 it will also count requests that never reached the
    Head: with the PM, rejected by the PM, withdrawn.
  - **`cr_by_stage` puts every post-release request in "Raised while with the Head".** A
    released attempt has `qc_reviewed_at` set, so `head_at` is not null and any later
    `requested_at` passes `>= head_at`. There is no "after release" bucket, and every
    SCM-raised request is post-release (SCM's window is `design_change_window_open()` only).
  - **`quality_analytics.html:382` contradicts the code.** It says "Counted against the PM
    who raised the request", but `m_change_request_rate` buckets by the SITE's
    `project.assigned_pm`, never by `requested_by`. Now that SCM raises, it is wrong twice.
- **`corrected` needs at least one linked correction, and the database cannot say so.** A
  CHECK cannot count M2M rows. B2's view must refuse a `corrected` verdict with no
  `boq_corrections` link inside the same transaction that sets it. `related_name='+'`, so
  BOQCorrection has no reverse accessor: "is this correction already evidence?" is a query
  on `DesignChangeRequest.boq_corrections.through`.
- **`with_pm` is outside every "pending" reader**, which all name `pending` explicitly:
  `_pending_change_requests()` (QC suspension, the raise pre-check, `open_crs`),
  `pending_change_requests_for()` (the group-lock blocker), the `pending_change_request_rows`
  prefetch behind `design_pending_at()`, and `design_metrics`' `pending_crs` (the Head's
  queue). So in B2 a request with the PM suspends no review and blocks no lock. The raise
  pre-check does not see it either; the DB constraint does, and the view's `except
  IntegrityError` then says "only one change request at a time may await the Design Head",
  which is wrong for a request that is with the PM. Each of these is a B2 decision.
- **Accept and reject refuse the new values with odd wording.** Both re-read under the lock
  and refuse `verdict != 'pending'` with "that change request has already been
  {get_verdict_display|lower}", which would read "already been with the pm". The refusal is
  right; the sentence is B3's.
- **`origin` is set from `user_can_manage_project()`, the predicate `raised_as` uses**, so
  an Admin or coordinator who also holds SCM authority is `pm`, matching the log line.
  The create call evaluates the predicate a second time instead of sharing `raised_as`'s
  evaluation, because B1 might touch the create call only. The result is the same, since
  nothing between the two calls changes the user or the site. B2 may fold them into one.
- **PROTECT on `pm_decided_by`, `withdrawn_by` and `corrected_by`** (decision 3). No
  deletion is blocked today, because nothing writes them. From B2, a profile named in any
  of them cannot be deleted. The paths that delete a profile are Django admin's User delete
  (`UserProfile.user` is CASCADE) and UserProfile delete, and `teardown_order_demo` for its
  manifest's profiles. No product view deletes a user. `requested_by` was already PROTECT,
  so a raiser was already undeletable. `teardown_order_demo`'s relation guard lists
  **no** `DesignChangeRequest` relation, neither the old one nor the three new ones, so a
  demo profile named on a request surfaces as a ProtectedError, not the guard's message.
- **The admin add form must choose `origin`.** It has no model default and
  `cr_origin_valid` refuses `''`. On the change form it is read-only, along with every new
  field.

### D50 — what session B2a (SCM change-request routing behaviour) left open

Recorded by session B2a, 25 Sep 2026. **Recorded, not fixed.** Every claim was checked by
grep, AST or a test in that session. B2a wrote the new verdicts: the raise routes SCM to
`with_pm` (or to `pending` on a site with nobody to triage it, Q5), and four endpoints —
forward, PM reject, withdraw, corrected — move them. No migration. **NOT DEPLOYED: B1, B2a
and B2b deploy together.**

> **HARD DEPENDENCY — B2b MUST ADD THE "CORRECTED" BUTTON TO THE QC REVIEW BANNER.**
> `design_change_request_correct` exists and is tested, but no screen posts to it: the Head
> triages on `qc_review.html`'s `open_crs` banner, which B2a was not allowed to change, and
> `change_request.html` is 403 to him. Until B2b it is reachable by POST only. The browser
> walk of the whole route happens after B2b, not between B2a and B2b.

- **Closed from §D49 by B2a.** The two catch-all templates (every verdict now has a branch);
  "`with_pm` is outside every pending reader" (decided, A1 — see below); the
  at-least-one-correction rule (enforced in the view, inside the transaction); the
  "already been with the pm" refusal (a with-PM sentence); and `origin`'s double evaluation
  (folded into one).
- **The notification audiences are now wrong. B2a changed no send; B2b owes all of this.**
  1. **An SCM raise that lands `with_pm`** still sends `GATE_CHANGE_RAISED`: the site's
     managers AND every active Design Head, saying "a design change request is waiting for
     the Design Head". The Heads should not hear yet, the sentence is false, and the Head's
     link is `design_qc_review`, whose banner lists `pending` only, so he finds nothing
     there. The PM is told, but not that the request is theirs to forward.
  2. **Forward sends nothing.** The Head is never told that a request has arrived. His only
     notice was the false one at raise.
  3. **PM reject, withdraw and corrected send nothing.** SCM is not told the PM rejected
     its request, the PM is not told SCM withdrew one, and nobody is told a request was
     recorded as corrected. SCM, who raised it, most needs to know.
  4. Correct as they stand: a Q5 fallback raise (it is with the Head), accept, and reject.
- **B2b's screens.** The PM queue section and ageing counters for `with_pm`. On
  `design_head_sites`, a released site whose request is with the PM reads "SCM" in Pending
  at, because the `pending_change_request_rows` prefetch is pending-only by decision (A1).
  It needs a "PM (change request)" label rather than a wider prefetch. `qc_queue` and the
  `qc_review` banner still say "PM change request" of an SCM-origin request.
- **B3's wording and metrics, unchanged from §D49:** `m_cr_rejection_rate` now really does
  count `with_pm`, `pm_rejected`, `withdrawn` and `corrected` rows in its denominator;
  `cr_by_stage`; `quality_analytics.html:382`. Plus: accept or reject of a `corrected`
  request refuses with "has already been corrected in the boq" (`.lower()` on the display
  label); B2a's own new refusals lower-case the first letter only.
- **THE CORRECTED EVIDENCE IS MATCHED BY TIME, AND ONLY BY TIME.** The view links every
  BOQCorrection on the site's BOQ with `corrected_at >= requested_at` that no other request
  cites (through `DesignChangeRequest.boq_corrections.through`), by any reviewer. BOQCorrection
  has no link to a request, an attempt or a reason, so a correction made on that BOQ in
  that window for an unrelated reason is swept in and shown to SCM as part of the answer.
  `change_request.html` says so under the list. The Head's note is the only statement of
  which change answered the request. Precision would need a request FK on the correction
  written by `boq_correct`, which B2a was forbidden to touch.
- **A request with the PM is outstanding (A1).** `_open_change_requests()` feeds
  `_blocking_change_request`, the raise pre-check and `pending_change_requests_for()`. The
  QC-suspension half is unreachable through the product: SCM raises only on a released site,
  and grep finds no writer that moves a site out of `released` other than accept, which
  needs a `pending` request. `apply_design_status` has no transition table, so that is
  a grep result, not a proof. The test fixtures the state directly.
- **Q5 is decided at raise time only.** A PM assigned later does not pull a fallback
  request back to `with_pm` (it is already with the Head; correct). The reverse is a gap:
  if a site loses its PM and its last active coordinator while a request is `with_pm`,
  nobody can forward or reject it. Only an SCM withdrawal closes it, and the page offers
  that. Found, not fixed.
- **Q5's row text reads the raise line's action_code, `design_change_requested_no_pm`**,
  not the row: an SCM request that migration 0102 backfilled (`origin='scm'`, `pending`, no
  `pm_decided_at`) looks identical to a fallback, and `ActivityLog.action` is never
  string-matched. A reader counting raises by `action_code='design_change_requested'` now
  misses fallback raises; use `action_code__startswith='design_change_requested'`.
- **D-2's "active" narrows the triage predicate only.** `user_can_triage_scm_change_request`
  reads `project_managers()`, which drops inactive coordinators, but does NOT check the
  ASSIGNED PM's `is_active` (the list includes the assigned PM unconditionally). So an
  inactive assigned PM still counts as a triager and Q5 does not fire for their site. The
  raise, `origin` and the form's 403 still use `user_can_manage_project()`, which admits an
  inactive coordinator: such a person may raise (as `pm`) but not forward.
- **The forward's "no longer released" refusal is unreachable today**, for the grep reason
  above. It is kept so a future writer out of `released` cannot forward a stale request.
- **PROTECT now bites.** `pm_decided_by`, `withdrawn_by` and `corrected_by` are written from
  B2a, so a profile named in one cannot be deleted (§D49's list of delete paths applies).
  `teardown_order_demo`'s relation guard still lists no `DesignChangeRequest` relation.
- **`projects/urls.py` was changed, outside the prompt's MODE**, for the four routes the
  new endpoints need.
- **`change_request.html` costs two more queries when the site has requests**: the Q5
  ActivityLog lookup and the `boq_corrections` prefetch. `_attempt_history()`'s three new
  prefetches cost nothing while those FKs are null.

> **B2b status (25 Sep 2026):** the hard dependency above is CLOSED — the button is on the
> QC review banner. The notification items 1–3, "B2b's screens" and "D-2's active" are
> closed too. See §D51.

### D51 — what session B2b (SCM change-request screens and notifications) left open

Recorded by session B2b, 25 Sep 2026. **Recorded, not fixed.** Every claim was checked by
grep, AST or a test in that session. No migration. **NOT DEPLOYED: B1, B2a and B2b deploy
together; the browser walk follows this session.**

- **Closed from §D50 by B2b.**
  - The "corrected" button is on `qc_review.html`'s banner. It is gated on
    `has_head_authority`, which is `_triage_guard()`'s predicate, and has its own
    mandatory `correction_note`. It also shows the number of uncited corrections it
    would cite.
  - Every send is rerouted per D-12. Each new send is `['in_app', 'email']`, runs
    after the atomic block, and sits inside a try.
  - The PM queue has its with-PM section, with the nav entry renamed "Design & BOQ
    approvals" in `base.html`, the only shell with that link.
  - The Head's ageing is time with the Head (D-11).
  - `head_sites` reads "PM (change request)" for a with-PM request.
  - "An inactive assigned PM still counts as a triager" is closed:
    `scm_change_request_triagers()` requires profile AND user active on the PM and on
    every coordinator. The raise fallback, the forward and PM-reject predicate, the
    queue section and the with-PM raise audience all read it.
- **THE HEAD'S PULL OF A STRANDED REQUEST IS DEFERRED (A7, part 2).** A request can still
  be stranded at `with_pm`. If the site loses its PM and its last active coordinator after
  the raise, nobody can forward or reject it. The Head cannot move it either, and without
  a migration there is no honest record for that move:
  - **(a)** Writing the Head into `pm_decided_by` / `pm_decided_at` / `pm_note` makes every
    reader say the PM forwarded it. That covers `change_request.html`'s forwarded line,
    `_pm_stage_refusal`'s "already forwarded" and B3's metrics.
  - **(b)** Writing only an activity line keeps the record honest but loses the Head's
    clock start: D-11 would charge him the stranded days. The history would also live in
    `ActivityLog` alone.
  - A real pull needs `pulled_by` / `pulled_at` columns (a migration). It also needs a
    Head surface that lists stranded requests; none exists. `head_sites` shows "PM (change
    request)" but not that nobody can act on it.
  - **The escape that exists today:** any SCM user withdraws the request, and raising it
    again falls through Q5 straight to the Head. The helper now treats an inactive PM as
    nobody, so the new raise takes the Q5 path.
- **The "corrected" button is NOT on the tender dashboard's queue** (`tender_dashboard.html`
  still offers Accept and Reject only). Signed off: one triage surface at a time, and B3
  adds the second after the walk proves the first.
- **B3 still owes (unchanged from §D50):**
  - **The wording sweep.** `qc_review.html`'s banner heading and `qc_queue` say "PM change
    request" of an SCM-origin request. So do `tender_dashboard.html`'s panel header ("PM
    change requests awaiting your decision"), the attention band's reason text ("PM change
    request awaiting your decision") and the accept and reject log lines ("PM change
    request accepted/rejected").
  - `site_workspace.html` still calls the queue the "Design approvals" screen, twice.
  - A corrected request refuses a later accept or reject with "has already been corrected
    in the boq".
  - **The metrics.** `m_cr_rejection_rate`'s denominator, `m_cr_by_stage`'s buckets, and
    `quality_analytics.html`.
- **Found, not fixed.**
  - **Two scopes on the PM queue page.** Its approvals section is `manageable_projects_q()`
    (`user_can_manage_project()`, which admits an inactive coordinator); the new change-
    request section is the triager list. By design (A2), but a single page answers "whose
    sites" two ways.
  - **An inactive coordinator may still RAISE, as `origin='pm'`**, through
    `user_can_manage_project()` (unchanged from §D50). The helper narrows triage only.
  - **A PM assigned after a Q5 fallback raise hears about the Head's decision.** The
    request is `origin='scm'`, and a Head reject or corrected reads the triagers at send
    time, so a PM who never saw the request is told how it ended.
  - **The corrected message lists the time-matched evidence** (§D50). An unrelated
    correction made in the window is shown to SCM as part of what changed.
  - **Forward does not reach the Heads' deputies**, only `is_design_head` holders. That is
    the same choice as the raise (3.1c-ii D4); a deputy covering for the Head is not told a
    request has arrived.
  - **No query budget is pinned for the new sends.** Each runs after its block: the forward
    reads the Heads (one query), and corrected, withdraw and SCM reject read the triagers
    (one or two queries).

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

### ~~G4 — `parse_date()` raises on an in-range-looking but invalid date~~ — **CLOSED by the typed-date session, 18 Sep 2026**

Not a codebase defect — a Django API sharp edge found while building the report, recorded so
the next person does not repeat it. `django.utils.dateparse.parse_date` returns `None` for a
string that does not look like a date, but **raises `ValueError`** for one that matches the
regex with out-of-range values (`'2026-13-45'` → `month must be in 1..12`).

Code that treats it as "returns None or a date" ships a 500 on a hand-edited query string.
`report_views._resolve_report_date()` catches both. **Any other view parsing a user-supplied
date should be checked** — this session did not audit for other call sites (R-12).

**Closed 18 Sep 2026.** The typed-date session audited every date parser in the views. The
other call sites were `design_due_date_propose` and `design_due_date_change`, which called
`parse_date()` bare and answered `'2026-02-30'` with a 500. Both now go through
`forms.check_typed_date()`, which catches the `ValueError`, and
`tests_date_validation.py` posts that string to both. The remaining parsers are the admin
log filters (`date.fromisoformat` inside `try/except ValueError`, see G9) and the Zoho
webhook (`strptime` inside `try/except ValueError`, see G6).

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

### G6 — typed dates that bypass every view: Zoho, Django admin, the seed command

Found by the typed-date session (18 Sep 2026), not fixed: the prompt scoped the range rule
(`forms.check_typed_date()`: 2020-01-01 to today + 5 years) to views, and said to propose
nothing for these.

- **`zoho_deal_closed_webhook`** writes `Project.target_commissioning_date` from the deal's
  `Closing_Date` with `datetime.strptime(raw_date, '%Y-%m-%d')`. A malformed value is
  dropped silently (`except ValueError: pass`); an out-of-range one is stored. No range check.
- **Django admin** forms expose typed dates with no range check. Checked by building each
  registered `ModelAdmin`'s form: `Program` (`expected_completion_date`, `award_date`,
  `ppa_signed_date`), `Project` (`survey_date`, `target_commissioning_date`), `Task`
  (`due_date`), `Milestone` (`due_date`, `completed_date`, also through `MilestoneInline` on
  `Project`), `DueDateCommitment` (`proposed_date`), `Checklist` and `TaskTemplate`
  (`effective_from`). The Program and Milestone dates can be typed nowhere else.
- **`seed_opex_test_data`** sets `DeliveryChallan.dc_date` and `expected_delivery_date`
  relative to `timezone.localdate()`, so it stays in range; listed for completeness.
- **Computed dates are not checked either.** `recalculate_from_task()` and
  `calculate_due_dates()` add durations to an anchor, so an anchor typed at the ceiling
  cascades later tasks past it. Those dates are computed, not typed, and the rule does not
  apply to them.

**Risk if left:** the admin and the webhook can still store a year-26 date. `manage.py
list_implausible_dates` covers every `DateField`, so it will find what they write.

### G7 — the ceiling moves, and the three production rows are not corrected

The upper bound is today + 5 years, recomputed on every request (`forms.typed_date_ceiling()`,
and the `typed_date_max` context key). A date accepted today can fall outside the range
later, and nothing re-checks stored rows. That is by design: the rule is enforced when a date
is typed, never as a model constraint, because a constraint would refuse the bad rows on the
very save that corrects them. `list_implausible_dates` prints the range it used on every run.

The three production rows that started this (Task 2077 and 2072 on UKRU001, Task 405 on
HRP-RES-2026-012) were **not** corrected in code or by hand. Their owners fix them on screen;
every edit path accepts a valid date over a stored bad one (`tests_date_validation.py`,
`test_a_row_already_holding_a_bad_date_can_be_corrected`). Local, stale: `solarpms_local`
holds Task 405 at `0026-04-25`.

### G8 — `project_detail.html` is a dead template with three date inputs and no bounds

`project_detail` only redirects to `project_overview`, and no view renders
`projects/project_detail.html`. Its three `<input type="date">` (a task due date, the
milestone due date, the issue due date) did not get `min`/`max`, because nobody can reach
them. If the template is ever rendered again it needs the same two attributes as every other
date input, or it should be deleted.

### G9 — query-parameter dates do not use the typed-date helper, deliberately

`ceo_daily_report` resolves `?date=` in `report_views._resolve_report_date()`: an absent value
means today, and an unparseable or future one means today with a warning. The four admin
filters (`portal_activity_log`, `admin_whatsapp_log`, `admin_send_records`,
`admin_audit_log`) ignore an unparseable `date_from`/`date_to`. None of them stores anything,
and a filter date before 2020 is a legitimate question, so they keep their own handling and
get no `min`/`max`. Revisit only if one of them starts storing a date.

### G10 — `create_delivery_challan` is role-gated with no project scope

It is `@role_required(['SCM'])` and resolves the project with `_active_project()` only, so any
SCM user can raise a challan on any project. **This is known and currently deliberate:**
`ACCESS_ISOLATION_AUDIT.md` classes it as relationship-creating (D.4, and blocking question
Q2), and the 25 Aug row of `execution-model.md` §12 keeps SCM portfolio-wide. It is re-raised
here, and added as finding 16 in `ACCESS_ISOLATION_AUDIT.md`, so the access work revisits it
once an assignment table exists. The typed-date session did not change it.

### G11 — the human status path never clears `completed_at` when a task leaves Done

Found by the Active Today session (18 Sep 2026), not fixed: the prompt forbade touching
`_apply_task_status_change()` or any code that writes `completed_at`.

`_apply_task_status_change()` stamps `completed_at` on entering Done and does nothing to it on
the way out. `VALID_TRANSITIONS` lets a human move Done → Blocked, so a reopened task keeps a
completion date for work that is no longer complete. `apply_mirror_status()` already clears it
on the way out of Done, so the two writers disagree.

The CEO daily report now guards itself: Done Today in `build_user_status_rows()` requires
`status=Done` as well as `completed_at` on the date. Every other reader of `completed_at` is
still exposed — `dashboard_ceo`'s weekly and monthly done windows filter `status=Done` too and
are safe, but anything that reads `completed_at` without a status condition is not.

**Why not fixed here:** clearing `completed_at` on reopen changes what the column means, not
just one report. Decide it in the status path, with `apply_mirror_status()` as the model.

### G12 — Active Today cannot see page views or unrecorded actions

`build_user_status_rows()` marks a user active if they logged in that day, or are the actor on
at least one `ActivityLog` or `StatusTransition` row that day. Two gaps, both stated in the
report's footnote rather than fixed:

- **Page views are not recorded anywhere.** A user who only reads the portal on a session
  started the day before shows as not active. Closing this needs a request-level record
  (middleware or a last-seen field), which the session forbade.
- **Swallowed log writes.** `log_activity()` and the login signal both swallow exceptions, so a
  failed write removes a row. A user drops out only if every row they had that day failed, so
  the effect is small; `ActivityLog failed:` lines in the Railway logs measure it exactly.

`G5` is partly closed by `projects/tests_user_status_report.py`: the row-sum invariant and the
constant query count (eight) are now pinned. The deactivated-user drop, soft-delete/cancelled
exclusion, role matrix and malformed-date inputs from the original scaffold are still uncovered.

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

### B24 — nine findings carried out of sessions T2, T3, T4 and G1, none of them in that session's MODE

Recorded here because each was found while building something else, verified at the time,
and left untouched because the session's MODE did not reach it. They have been restated in
session reports since T2 without ever landing in this file; appended by G2 so they stop
being re-discovered.

**1. Nothing in the product UI edits `Task.assigned_role` (T2 / 1.3).** The column is
written once, at task creation — by the template attach, or by the add-task form, which is
where `roles_for_phase()` narrows the choice. After that no screen offers it. Reassigning a
task changes `assigned_to` and leaves `assigned_role` saying whatever it said when the row
was made, so a task moved to a person of another role carries a stale role label. Whether
that label should follow the assignee, or stay as the phase's statement about the work, is
a product question nobody has been asked.

**2. `_TASK_TO_PROFILE_ROLE` lives in `views.py` and belongs in a shared module (T2).** It is
a mapping between two role vocabularies, consulted by assignment code, and it sits in the
view layer with no importable home. `gantt_constants.py` and `models.py`'s
`DELIVERY_MIRROR_CODE_TO_CATEGORIES` are both precedents for where a mapping like this
should live. Moving it is a rename across its call sites and nothing else, but it is not
free and no session has had it in MODE.

**3. The standalone add-task page cannot narrow its role list without a POST (T2).** The
narrowing added by T2 works on the modal, which knows its phase. The standalone page is
reached before a phase is chosen, so `roles_for_phase()` has nothing to answer about and the
full role list is offered. A user can still create a task whose role the phase does not use
and only find out on submit. Fixing it means either a phase-first step or an HTMX round trip
on the phase select.

**4. The assignee list is not scoped to the project — neither in `task_assign` nor in the
add form (T2).** Both render `UserProfile.objects.filter(is_active=True)`, the whole
company, so a PM can assign a task on their site to an engineer who has never worked on it.
Not a security hole (the assignment is visible and logged, and holding a task is itself what
grants visibility), but it makes the dropdown long and the wrong choice easy. Any fix has to
decide what "on this project" means for a person who has not been given work there yet,
which is the chicken-and-egg that stopped T2 doing it.

**5. The HTMX row swap drops `delivery_consignments`, so a delivery row loses its count
until reload (T4).** The phase-list row partial is re-rendered by the task-action endpoints
with a context that does not include the delivery lookup T4 added, so a delivery mirror row
that is swapped in place shows its status without the consignment count beside it. A full
page load restores it. The fix is to thread the lookup through the swap context, which
touches every task-action endpoint — the reason T4 left it.

**6. `material_status_by_category` and `get_material_status` sum across units, and can read
Received where the delivery panel reads partial (T4).** They aggregate `DCLineItem`
quantities without grouping by `unit`, so 400 Nos and 1 Lot become 401 of nothing. The
delivery panel T4 built deliberately refuses to aggregate a category holding two units, so
the two surfaces can disagree about the same material on the same site. The panel is the
correct one; the older helpers are the ones to change.

**7. `BOQItem.category`'s declared choices do not match live OPEX values; it is not a
BOQ-to-delivery join (T4, B-18).** The field's `choices` were written for the Residential
vocabulary, and the OPEX catalogue stores values (`Module`, `MMS`, `Conduit`, …) that are not
in that list and do not share spellings with `DCLineItem.CATEGORY_CHOICES` either. So
nothing joins a BOQ line to a delivery line, and any figure implying "received against what
the site needs" would be invented. T4's panel reports against what was DISPATCHED for exactly
this reason. Closing B-18 means agreeing one vocabulary first.

**8. GRN `ActivityLog` rows are written with a blank `action_code`, so they are invisible to
consumers filtering on it (G1).** Both `confirm_grn` and `override_grn` call `log_activity()`
without the `action_code` argument, so their rows carry `''`. The per-user EOD digest metrics
group on `action_code`, so a site engineer who spent the day receiving material has that work
counted nowhere — the sentence is in the feed, but no consumer can find it by event type.
Two codes and two call-site arguments; out of MODE for G1, which was an audit, and for G2,
whose MODE forbids touching `confirm_grn`.

**9. `Task.template_task`'s model comment claims nothing reads it back; seven things do
(T3).** The comment on the field reads "PROVENANCE ONLY — which TaskTemplateTask this task
was created from. Nothing reads it back to decide behaviour." Verified readers, all
branching on it:

  * `design_views.sync_design_mirror()` — `template_task__code=DESIGN_MIRROR_CODE`
  * `design_views._delivery_mirror_tasks()` — `template_task__code__in=DELIVERY_MIRROR_CODES`
  * `design_views.delivery_detail_for_task()` — returns `None` on `template_task_id is None`
  * `views._checklist_for_task()` — resolves the checklist by `template_task__code`
  * `task_dependencies` — keys the template-edge copy on `template_task_id`
  * the phase list in `views.py` — `select_related('template_task')` for the consignment attach
  * the OPEX template correction path in `views.py` — matches on `template_task__code`

The column is fine and every one of those reads is correct. The COMMENT is false, and it is
the kind of false that gets a column deleted: it tells the next reader that dropping the
write is safe. One comment edit, out of MODE for every session that has noticed it.

### B32 — three things found while removing the approval deadlock (prompt 2.1 self-certification), none of them in that session's MODE

Recorded by the self-certification session, whose MODE was "remove the approval deadlock"
and explicitly forbade notifications, the design module, the CEO report and the two live
rows being repaired by hand. Each was verified at the time and left untouched. Numbered B32
because B25 was already taken when this was first appended; the follow-up session that
narrowed who may self-certify renumbered it and closed one of the four.

**1. A site with no active approver waits correctly, but nothing says so (departed-PM
reassignment).** `user_may_self_certify()` now requires the submitter to hold the approval
signature themselves, so a site engineer on a project whose PM has been deactivated takes
the ordinary path: the submission is recorded and the task sits at "Awaiting approval". That
is the correct outcome and the deadlock is not back — the task is neither lost nor silently
completed, and reassigning `assigned_pm` lets the new PM approve it with no code change
(`tests_approval_pool.DeactivatedPmTests` asserts exactly that, end to end).

What is missing is the SIGNAL. On every screen that task is indistinguishable from one
waiting on a PM who will be along shortly: the row badge reads "Awaiting approval", its
tooltip names "the project manager or QA/QC", and no surface anywhere says the project has
nobody active who can give that approval. `task_has_independent_approver(project, None)`
already answers the question — it was deliberately left taking a nullable submitter so this
work would not need a new helper — but nothing calls it that way. The shape is a warning on
the project overview, or a filter on an admin screen listing sites whose approver pool is
empty, plus most likely a nudge at the point `assigned_pm` is deactivated. Out of MODE for
both sessions, which were fixing the completion rule and not building notifications.

**2. `due_date` has no validation below the view layer, and one live row proves it
(task 2072).** `forms.check_typed_date()` guards all four task entry points — `TaskAddForm`,
the Finance inline set on In Progress, and both arms of the due-date update — but
`Task.due_date` is a bare `DateField` with no validators and no `CheckConstraint`, and
`TaskAdmin` neither makes it read-only nor cleans it. A Django-admin edit writes any year at
all. The local database holds one such row (`HRP-RES-2026-012`, "DEV Conduct",
`0026-04-25` — a dropped "20"). The model-level absence is deliberate and documented in
`forms.py`: a constraint would refuse the existing bad rows on the very save that corrects
them. What is NOT deliberate is the admin form bypassing the helper. `manage.py
list_implausible_dates` already reports the rows.

**3. GRN is not the only writer of blank `action_code` rows.** B24 item 8 recorded
`confirm_grn` and `override_grn`; the same is true of the due-date update path, which calls
`log_activity()` with no `action_code` for both "Updated due date for task" and "Cleared due
date for task". The EOD digest groups on `action_code`, so due-date maintenance is counted
nowhere. Same one-line fix as B24 item 8, same reason it was not made here.

*Closed from the original four:* **the phase-list confirm modal wording.** The modal in
`project_overview.html` kept the old "Submit for approval" title, body and confirm button
while the row button beside it said "Complete (self-certified)". Closed by the follow-up
session — all three strings now branch on `can_self_certify`, and
`tests_approval_pool.ButtonLabelTests` asserts the dialog and the row agree in both
directions.

### G13 — reopening a task does not clear its approval

Found by the checklist answer-window session (22 Sep 2026), not fixed: the prompt forbade
any change to status or approval behaviour.

Done → Blocked → In Progress does not clear approved_at/approved_by. A reopened task keeps a
stale approval, and its behaviour on resubmission is unverified.

`VALID_TRANSITIONS` allows the reopen, and nothing on that path touches the six 2.1 columns.
The task ends up In Progress with `submitted_at` and `approved_at` both set, so
`is_awaiting_approval` is False and `task_submit_for_approval()` refuses on its
`approved_at is not None` guard before it reaches the `submitted_at` one. Whether a reopened
OPEX task can be resubmitted at all has not been tested.

`checklist_answers_open()` deliberately does not read `approved_at`: a task reopened after
approval is OPEN for checklist answers, because reopening is a deliberate act and the redone
work should be checkable. Fixing this entry must not change that.

Sibling of G11 (`completed_at` not cleared on the same transition). Decide both in the status
path together.

### G14 — no single "N tasks assigned" message with the true N

Found by the Duplicate for locations session (22 Sep 2026), not fixed: `utils.py` was
outside that session's MODE.

No path sends a single 'N tasks assigned' message with the true N. assign_tasks_bulk
exists only as the cooldown's second message and undercounts.

`_notify_assignment()` sends `assign_task` for the first assignment to a person on a
project inside the hour, `assign_tasks_bulk` for the second (its count is `sends + 1`,
i.e. always 2), and nothing after that. `assign_tasks_to()` is silent by design. So
creating 9 location tasks for one engineer with `notify=True` on each would say "2 tasks".
`task_duplicate_locations_create` therefore notifies on the FIRST new task only and
creates the rest silently: one accurate message about one task, rather than an
inaccurate one about two.


### G15 — Client Gantt friendly labels are keyed by task name

Found by the Rename task session (22 Sep 2026), not fixed.

Client Gantt friendly labels (`GANTT_TASK_DISPLAY_NAME_MAP`) are keyed by task_name; a
renamed Residential task shows its raw name to the client.

### G16 — the finance-milestone sync matches by name on every project type

Found by the Rename task session (22 Sep 2026), not fixed: the sync was outside that
session's MODE.

`_apply_task_status_change` triggers the finance-milestone sync by task_name without
checking project_type. Rename refuses these names; other paths that set task_name (admin)
are not guarded.

### G17 — no site-wide location rename

Found by the Rename task session (22 Sep 2026), not built.

Location rename is per task. A site-wide location rename (all tasks sharing a label) is not
built.

### G18 — task reordering has no keyboard alternative

Found by the Reorder tasks session (22 Sep 2026), not built.

Task reordering is mouse-only; there is no keyboard alternative for moving a task.

### G19 — the milestone gate selects by position on every project type

Found by the Reorder tasks session (22 Sep 2026), not fixed: left unchanged by decision.

The milestone gate (_gate_task_pk / forloop.first) selects by position on every project
type. Harmless on OPEX today only because no OPEX task can be BD. If a BD task is ever
added to a non-Residential template, reordering could move the payment gate. Scope it to
Residential before that happens.

### G20 — `BOQItem.ordered_quantity` / `ordered_vendor` are legacy; `VendorOrder` is the authority for "ordered"

Found by O1 (22 Sep 2026), not changed: out of that session's MODE.

O1 made `VendorOrderLine` the record of what was ordered, from whom, for which sites. The
two per-row fields SCM types on the BOQ screen predate it and now answer the same question
a second way, with nothing keeping the two in step.

Every reader and writer, as of O1:

* **Writer** — `views.boq_detail`, the SCM `save_scm` / `acknowledge_scm` branch (sets both
  from the `ord_qty_<pk>` / `ord_vendor_<pk>` inputs). Not group-lock gated, deliberately —
  see `permissions.project_boq_is_group_locked`.
* **Readers** — `views._boq_snapshot` (into `BOQRevision.snapshot`, read back by
  `boq_history.html`), `views._notify_boq_acknowledged` (ordered ≠ BOQ quantity), the
  `boq_detail` render (`select_related('ordered_vendor')`) and `boq_detail.html`.
* Not these: `DCLineItem.ordered_quantity` is a different field on a different model, read by
  `_build_delivery_lookup`, `project_overview`, `create_delivery_challan`,
  `sync_delivery_mirrors`, `delivery_detail_for_task`, `recalculate_dc_status` and
  `get_material_status`. It is the challan's own quantity and is not affected.

**Decision to take after O3** (once orders are being raised): either derive the two BOQ
fields from `VendorOrderLine` (sum over lines whose `boq_item` is this row, or over
`item_master` for a single-site order) and make the inputs read-only, or retire them and
the SCM branch that writes them. Until then a BOQ screen may show an "ordered" figure that
no order supports.

**O3 has now landed and the decision is still open.** O3's MODE excluded both fields
explicitly — the group raise reads neither and writes neither — so nothing has changed here,
except that a group order now makes the second answer reachable: a consolidated line names
`item_master` and no `boq_item` at all, so any derivation must go through the catalogue
join, not through `boq_item`.

### G21 — a site-less order writes no feed line, because `ActivityLog` has no tender column

Found by O3 (23 Sep 2026), not fixed: it needs a model field, which O3's MODE excludes.

`vendor_order_create_group` logs `vendor_order_raised` once per site, on the site, exactly
as `site_group_lock` logs its lock. An order sized against ZERO sites (O2d's central
purchase) has nowhere to put that line: `ActivityLog.project` is nullable but there is no
`program` FK, so the event would be written with `project=NULL`, where no feed reads it.
Such an order is therefore **not logged at all** — it is visible on the order detail page,
in the tender's order list and in `StatusTransition` (if it carries a payment), and
nowhere in any activity feed.

Two ways out, both a migration: add `ActivityLog.program` and teach the tender dashboard
to read it, or give `Program` a feed of its own. The first is smaller and matches how
`PaymentRequest.project` was made nullable in O2d.

### G22 — the group raise page ships every candidate site's BOQ contributions

Found by O3 (23 Sep 2026), accepted by design, recorded because it will not scale forever.

O3's spec forbids a server round trip between sections, so the browser must be able to
re-aggregate as sites are ticked. The page therefore renders one row per catalogue item
across EVERY candidate site, each carrying a `data-contrib` map of `{project_pk: quantity}`.
Its QUERY count is flat in the number of sites (measured: 14 at 3 sites and at 30), but its
BYTE count is not — the map grows with sites × catalogue rows. Measured at build time on
the local database: 167 KB for 5 candidate sites and 36 line rows.

The fix, when a tender is large enough to need one, is an HTMX fetch of the aggregate on
each selection change — which is exactly the round trip the spec ruled out, so it is a
product decision, not a refactor.

### G23 — the raise page costs one query per OPEX tender

Found by O3 (23 Sep 2026), accepted deliberately.

`order_views._raise_sources()` calls `design_views.post_qc_pool(program)` once per shown
tender. The alternative is a second hand-written copy of that function's exclusion — the
`NOT EXISTS`/LEFT-JOIN trap its docstring spends twenty lines on, and which
`tests_design_groups` exists because of. A query per tender is the cheaper mistake. It is
the number of TENDERS, never the number of sites; revisit if the deployment ever carries
tenders in the hundreds.

### G24 — `aggregate_group_boq()`'s `contributions` map is keyed by site CODE, not pk

Found by O3 (23 Sep 2026), not changed: four screens read that function's return shape.

`design_views.aggregate_group_boq()` returns `contributions` keyed by
`boq__project__project_id` — the human site code, which is what `site_group_detail.html`
prints. The raise page has to match a contribution against a checkbox whose value is the
Project pk, so `order_views._candidate_contributions()` builds a second, pk-keyed map with
its own query over the same rows and the same three filter terms. Two queries where one
would do, and two places that must agree about which BOQ rows count.

Worth collapsing when something else forces a change to that function: return both keys,
or key by pk and have the group template look the code up.

### G25 — a hand-added line may name a DEACTIVATED catalogue row

Found by O3 (23 Sep 2026), not fixed: low severity, and the fix has a trade-off.

The "Add a line" picker offers `BOQItemMaster.objects.filter(is_active=True)`, but
`_parse_group_lines()` resolves posted pks without that term, so a crafted POST (or a page
left open while the Design Head deactivates a row) can put a deactivated item on an order.
The line's identity is snapshotted, so the record survives either way and nothing downstream
breaks. Adding `is_active=True` to the parser is one word — but it would also refuse an
AGGREGATE line whose catalogue row was deactivated after the BOQ was written, which is a
legitimate thing to order. Decide which rule is wanted before changing it.

### G26 — the per-site visibility check on the group raise is unreachable for its only caller

Found by O3 (23 Sep 2026), by design, recorded so nobody deletes it as dead code.

`_parse_group_sites()` refuses any posted site that fails `user_can_view_project()`. The
only role `user_can_raise_group_order()` admits is SCM, and SCM is portfolio-wide in
`user_can_view_project()`, so no site can fail that term today. It is there for the
assignment-based narrowing D-4 will bring, and it is pinned by a unit test that hands the
parser a PM with no claim on the site
(`tests_vendor_order_group.test_a_site_the_user_cannot_see_is_refused`) rather than by an
end-to-end one, which cannot exist yet. The deleted-site half of the same guard IS reachable
and is tested through the view.
