# Design approval audit — Part 0 pre-flight

Audit-only session, 10 Sep 2026. No `.py`, template, migration or test was touched. The
local database (a restored production dump) was queried read-only, `.count()` / `.values()`
only.

## Outcome: STOP CONDITION FIRED — Parts 1–4 were not started

| # | Premise | Verdict |
|---|---|---|
| P1 | `DESIGN_RELEASED` is terminal, one entry point | **FALSE.** The single entry point is correct. "Terminal" is not: there is exactly one exit. |
| P2 | release fields exist and are not cleared on reopen | **TRUE** (`released_at`, `released_by`) |
| P3 | Head accepts CR → `_open_next_attempt(redo=None)`, NO attempt counted against the designer | **Mechanism TRUE, last clause FALSE.** Two shipped metrics do count the reopened attempt against the designer. |
| P4 | raising a CR *requires* a DRAFT procurement-group membership; computed in two functions (§B1) | **FALSE as stated.** The requirement applies only to a site already `released`. "Two functions" is right, but a third, divergent gate exists. §B1 is in `docs/EXECUTION_MODULE_DEFERRED.md`, not the root file. |
| P5 | only a PM may raise; SCM cannot | **TRUE in effect, but no role gate exists.** The gate is identity-based: assigned PM **or** a Project Coordinator. |

The prompt's stop rule says to halt if P1 or P3 is false. Both contain a false clause. The
P1 falsity is the reopen path that P3 itself describes, so P1 and P3 cannot both be true as
written. That contradiction is part of what needs fixing before Part 1. The P3 falsity
matters directly to the feature: any PM-rejection loop routed through `_open_next_attempt()`
would feed the same two metrics described below.

Separately, **the prompt was truncated.** It ends mid-sentence at §4.1 ("A standing test").
Nothing after that point was available to this session.

---

## P1 — `DESIGN_RELEASED`: one entry, and one exit (so not terminal)

### The entry point — correct

Every product write to `DesignAssignment.status` goes through one function,
`design_views.apply_design_status()`:

```python
def apply_design_status(assignment, new_status, actor, detail, action_code,
                        extra_fields=None, entity_type='DesignAssignment',
                        entity_id=None):
    """THE ONE PLACE `DesignAssignment.status` IS WRITTEN. Session C (audit A-2.2 §5.1).
    ...
    DesignAssignment.objects.filter(pk=assignment.pk).update(**fields)
```

The only caller that passes `DESIGN_RELEASED` is `design_views.design_head_qc_pass()`:

```python
        apply_design_status(
            assignment, DESIGN_RELEASED, profile,
            f'Design Head passed attempt {attempt.attempt_number} — design released',
            'design_head_qc_passed',
            extra_fields={'released_at': now, 'released_by': profile},
            entity_type='DesignAttempt', entity_id=attempt.pk)
```

It is gated after the head QC gate by
`_qc_guard(request, project, (DESIGN_AWAITING_HEAD_QC,), gate='head')`.
**"Design Head" here includes the named deputy.**
`permissions.user_can_head_gate_design()` returns `user_can_qc_design(user, assignment)`,
which requires `user_has_design_head_authority(user)`:

```python
    return user_is_design_head(user) or user_is_design_head_deputy(user)
```

The Django admin cannot set it. `admin.DesignAssignmentAdmin` has
`readonly_fields = ['status', 'released_at', 'released_by', ...]`, with the comment
*"THE ADMIN IS NOT A RELEASE ROUTE"*.

Non-product writes, all `DesignAssignment.objects.create(..., status=DESIGN_RELEASED, ...)`
in management commands: `seed_opex_test_data`, `seed_scm_handoff_data`, `seed_scm_pilot`.
Tests write it too (e.g. `tests_design_part9`, `tests_design_part46`) and are not counted.

### The exit — why "terminal" is false

`design_views.design_change_request()` widens its status gate for a released site in a
draft procurement group:

```python
    in_draft_group = membership is not None
    allowed_statuses = (CHANGE_REQUEST_STATUSES + (DESIGN_RELEASED,)
                        if in_draft_group else CHANGE_REQUEST_STATUSES)

    if assignment.status == DESIGN_RELEASED and not in_draft_group:
        return _back(f'{project.project_id}: the design is already released — a change '
                     f'now is a new scope of work, not a change request.')
```

Acceptance (`design_change_request_accept()`) then calls `_open_next_attempt()`. That function
writes `in_design`, or `arka_submitted` if an approved Arka carried forward. With `redo=None`
nothing carries forward, so the result is always `in_design`:

```python
    opening_status = (DESIGN_ARKA_SUBMITTED
                      if (carried_arka is not None
                          and carried_arka.head_verdict == ARKA_APPROVED)
                      else DESIGN_IN_DESIGN)
```

The root `EXECUTION_MODULE_DEFERRED.md` §11 already records this:
*"A released OPEX site CAN be reopened — through a draft procurement group"*.

### The second exit that an earlier audit found is now CLOSED

`docs/PHASE_2_PREFLIGHT_AUDIT.md` §6.1 reported a second route out: `design_mark_blocked`
had no released guard. It has one now. From `design_views.design_mark_blocked()`:

```python
    if assignment.status == DESIGN_RELEASED:
        return _deny(request,
                     f'{project.project_id} has already been released and cannot be '
                     f'placed on Design Hold through this action.',
                     'design_my_sites')
```

**§6.1 of that document is now stale.**

### Every other status writer refuses a released site

This was checked caller by caller, not assumed:

| Writer | Why it cannot move a `released` row |
|---|---|
| `design_survey_upload`, `design_survey_link_set` | Replacing an existing survey/link is refused unless the status is in `(DESIGN_AWAITING_SURVEY, DESIGN_AWAITING_ALLOCATION)` or the site is on hold. The only branch a released row could reach passes `new_status=None` ("no transition"). |
| `_allocate_one` | `if assignment.status not in REALLOCATABLE_STATUSES: raise ValueError`. The tuple is `(AWAITING_ALLOCATION, ALLOCATED, DUE_DATE_PROPOSED, IN_DESIGN)`. |
| `design_arka_submit` | `ARKA_SUBMITTABLE_STATUSES = (DESIGN_IN_DESIGN, DESIGN_ARKA_REJECTED)` |
| `design_arka_approve` / `_reject` | `_verdict_target`: `if assignment.status != DESIGN_ARKA_SUBMITTED` refuses |
| `design_arka_head_approve` / `_reject` | `_head_verdict_target`: `if assignment.status != DESIGN_AWAITING_HEAD_ARKA` refuses |
| `_maybe_advance_to_artifacts_uploaded` | `if assignment.status != DESIGN_ARKA_SUBMITTED: return False` |
| `design_qc_start` / `qc_pass` / `qc_fail` / `head_qc_fail` | `_qc_guard(..., required_statuses)` with `(ARTIFACTS_UPLOADED,)`, `(IN_QC,)`, `(IN_QC,)`, `(AWAITING_HEAD_QC,)` |
| `design_mark_blocked` | Explicit released guard (quoted above) |
| `design_change_request_accept` → `_open_next_attempt` | **THE exit.** Note that accept itself has **no status check**. It relies on the gate at raise time, and only checks `change.verdict != CHANGE_REQUEST_PENDING`. |

### Corrected P1

`released` has exactly one entry, `design_head_qc_pass()` (Head or deputy), and exactly one
exit. That exit needs all three of:
1. SCM puts the released site in a **draft** procurement group.
2. A PM or coordinator raises a change request. Raising it soft-removes the site from the group.
3. The Head or deputy accepts it, and `_open_next_attempt()` moves the site to `in_design`.

---

## P2 — release fields: TRUE

`models.DesignAssignment`:

```python
    # ── Release ─────────────────────────────────────────────────────────────
    released_at = models.DateTimeField(null=True, blank=True)
    released_by = models.ForeignKey(
        'UserProfile', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='released_design_assignments',
    )
```

Neither is cleared on reopen. `_open_next_attempt()`'s only status write passes
`extra_fields={'current_attempt_number': next_number}`. The only non-test writer of
`released_at` / `released_by` outside management commands is the `extra_fields` of
`design_head_qc_pass()`. The chokepoint test regex in
`tests_design_status_chokepoint` (`_DIRECT_WRITE_RE`) forbids direct
`.released_(at|by) =` assignment in product code.

Consequence, already noted in root `EXECUTION_MODULE_DEFERRED.md` §11: a reopened row reads
`in_design` while still carrying the stamps of the undone release. `post_qc_pool()` orders by
and ages off `released_at`.

**Live data (local dump):** `released_at set but status != released: 0`. The reopen route has
never been taken. `DesignChangeRequest` has **0 rows**. `DesignAttempt.opened_reason` is
`initial` on all 10 rows.

---

## P3 — the reopen path: mechanism TRUE, "not counted against the designer" FALSE

### What is true

`design_change_request_accept()`, gated by `_triage_guard()` →
`user_has_design_head_authority()` (Head **or deputy**):

```python
        new_attempt = _open_next_attempt(
            assignment, ATTEMPT_REASON_PM_CHANGE_REQUEST, profile,
            f'change request accepted on attempt {change.attempt.attempt_number}')
```

`redo` is not passed, so it defaults to `None` (`def _open_next_attempt(assignment, reason,
actor, detail, redo=None)`). The outgoing attempt's verdicts are left `pending`. The
docstring says: *"Writing 'failed' at either gate would charge the designer with a rework
loop the PM caused"*. So the designer is not charged with a QC failure.

### What is false — the new attempt IS counted against the designer in two places

**1. `design_metrics.designer_workload()`, the tender dashboard's rework column.** The code
subtracts only Group B/C attempts:

```python
        designer_attempts = row['attempts'] - row['input_problem_attempts']
        row['rework'] = (round(designer_attempts / released, 1) if released else None)
```

`row['attempts']` is `len(s['attempts'])`, i.e. every attempt including `pm_change_request`
ones. So a PM change request raises the designer's `rework` multiplier. The same function's
docstring says `rework` is *"attempts caused by a GROUP A failure — the design was wrong"*.
**The code and the docstring disagree.** `design_analytics.m_rework_multiplier()` confirms
the divergence is known:

> *"That last exclusion is where this diverges from the tender dashboard's rework column,
> which keeps PM-change attempts in."*

**2. `design_analytics.m_first_pass_rate()`, the Group A "designer execution" metric:**

```python
        if s['assignment'].current_attempt_number == 1:
            row['first'] += 1
```

A site reopened by an accepted change request and re-released on attempt 2 has
`current_attempt_number == 2`. It is therefore **not first-pass**, and the designer's
first-pass rate falls. `opened_reason` is not consulted.

(`design_analytics.m_rework_multiplier()` does exclude `CAUSE_PM_CHANGE` correctly, so
three metrics give three different answers.)

**Why this matters to the feature:** if a PM rejection of a released-by-Head design were
built on `_open_next_attempt()`, every rejection would drop the designer's first-pass rate
and raise their tender-dashboard rework figure. That would happen even with a new
`opened_reason`, because neither metric filters on it.

---

## P4 — the `in_draft_group` gate: FALSE as stated

**Membership is not a requirement for raising a change request.** It is a *widening* that
applies only to released sites. A site in any of `CHANGE_REQUEST_STATUSES` is
change-requestable with no group at all:

```python
CHANGE_REQUEST_STATUSES = (DESIGN_IN_QC, DESIGN_QC_FAILED, DESIGN_IN_DESIGN,
                           DESIGN_ARKA_SUBMITTED, DESIGN_ARKA_REJECTED,
                           DESIGN_ARTIFACTS_UPLOADED,
                           DESIGN_AWAITING_HEAD_ARKA, DESIGN_AWAITING_HEAD_QC)
```

(The other requirements are `attempt.qc_started_at` set, and no pending request on the
attempt.) A **locked** group membership *refuses* the request outright:
`if membership is not None and membership.group.status == SITE_GROUP_LOCKED: return _back(...)`.
Group statuses are `SITE_GROUP_DRAFT = 'draft'` and `SITE_GROUP_LOCKED = 'locked'`. The
membership is looked up by `active_group_membership(project, GROUP_TYPE_PROCUREMENT)`.

**Computed independently in two functions: TRUE**, and with different spellings.

`design_change_request()`, where the locked case has already returned above:
```python
    in_draft_group = membership is not None
```

`design_change_request_form()`:
```python
    group_locked = membership is not None and membership.group.status == SITE_GROUP_LOCKED
    in_draft_group = membership is not None and not group_locked
```

**There is a third, divergent gate the premise does not name:
`design_views.pm_change_request_targets()`.** It decides whether the PM dashboard offers the
link (`views.py`, the `_cr_targets = pm_change_request_targets(...)` block). It has no
draft-group widening at all:

```python
        if assignment is None or assignment.status == DESIGN_RELEASED:
            continue
```

So a released site in a draft group accepts a change request on POST and shows an open
window on the form, **but the PM dashboard never links to it**. The route is reachable only
by URL, or via the form page reached some other way.

**Citation correction:** the root `EXECUTION_MODULE_DEFERRED.md` has no §B1 (its sections
are numbered 1–15). The entry *"B1 — `in_draft_group` is computed twice, with two different
spellings"* is in **`docs/EXECUTION_MODULE_DEFERRED.md`**, a separate file with the same
name. Its line references (`~3119`, `~4055`) are stale. It does not mention
`pm_change_request_targets()`.

---

## P5 — who may raise: TRUE in effect, but there is no role gate

Both `design_change_request()` and `design_change_request_form()` are decorated with
`@login_required` only. Their URLs in `urls.py` (`design_change_request_form`,
`design_change_request`) carry no `role_required`. The gate is
`permissions.user_can_request_design_change()`:

```python
    if project is None:
        return False
    return user_can_manage_project(user, project)
```

and `permissions.user_can_manage_project()` is **identity-based, not role-based**:

```python
    if project.assigned_pm == profile:          # PM authority — always checked, never gated
        return True
    return project.coordinators.filter(pk=profile.pk).exists()  # additive coordinator authority
```

So the raisers are **the site's assigned PM or any of its Project Coordinators**, not "only a
PM". SCM is excluded only because SCM users cannot be either. `Project.assigned_pm` has
`limit_choices_to={'role': 'PM'}` and `Project.coordinators` has
`limit_choices_to={'role': 'Project Coordinator'}`. **`limit_choices_to` is form/admin-level,
not a database constraint.** A user whose role changed after assignment would keep the
authority.

**Live data:** no OPEX project has an `assigned_pm` whose role is not `PM`, and no OPEX
coordinator has a role other than `Project Coordinator`. So in practice SCM cannot raise.

---

## Local-dump counts (read-only)

```
DesignAssignment by status: arka_submitted 2, artifacts_uploaded 2, awaiting_allocation 82,
                            in_design 1, in_qc 1, released 5
released_at set but status != released: 0
status == released but released_at null: 0
DesignAttempt by opened_reason: initial 10
DesignChangeRequest: 0 rows
SiteGroup (type, status): procurement/draft 1, procurement/locked 1
SiteGroupMembership: 6
```

## What a re-issued prompt should change

1. **P1:** replace "terminal" with *"one entry (`design_head_qc_pass`, Head or deputy) and one
   exit (draft-group CR → accept → `_open_next_attempt`)"*.
2. **P3:** drop "no attempt counted against the designer". Add a Part 4 question on
   `designer_workload().rework` and `m_first_pass_rate()`.
3. **P4:** restate it as "a draft-group membership *widens* the window to include
   `released`". Cite `docs/EXECUTION_MODULE_DEFERRED.md` B1, and add
   `pm_change_request_targets()` as the third gate.
4. **P5:** restate it as "assigned PM or Project Coordinator, by identity, via
   `user_can_manage_project`".
5. Supply the rest of the prompt from §4.1 onward.
6. Say which `EXECUTION_MODULE_DEFERRED.md` the append permission means. The root file
   currently has someone else's **uncommitted** §13/§14 edits, which a by-name commit would
   sweep in.
