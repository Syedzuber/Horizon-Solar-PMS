# Design approval audit — release, change requests, and who reads the ladder

```
AUDIT ONLY · 10 Sep 2026 · read-only

SCOPE. Establishes facts about the OPEX design release and change-request paths ahead
of a PM approval gate that a LATER session will design and build. It proposes no design
and contains no build prompt.

WHAT WAS DONE. The repository was read. The local database, a restored PRODUCTION dump,
was queried with .count()/.values() only; nothing was written. No .py file, template,
migration or test was touched.

HOW CLAIMS ARE LOCATED. Every claim is tied to a file and a function, class, constant or
template name, with the source quoted. Line numbers are NOT used as locators; this repo's
own audit documents show why. Where something could not be found, this document says so.

HISTORY. Part 0 was first run alone. Its stop condition fired: P1 and P3 each contain a
false clause. The requester then supplied Parts 4–7, which had been truncated, and asked
for Parts 0–7 in full. This document is that report. The stop condition's verdicts are
kept below, unchanged.
```

---

## Contents

- Part 0 — Pre-flight: five premises
- Part 1 — The status ladder, and who reads it
- Part 2 — What release actually does
- Part 3 — When SCM first sees a design
- Part 4 — The change-request path, end to end
- Part 5 — What authority a PM has in the design module
- Part 6 — Ledger and notification
- Part 7 — Findings
- Appendix — local-dump counts

---

# PART 0 — PRE-FLIGHT

| # | Premise | Verdict |
|---|---|---|
| P1 | `DESIGN_RELEASED` is terminal, with one entry point | **FALSE.** The single entry point is correct. "Terminal" is not: there is exactly one exit. |
| P2 | Release fields exist and are not cleared on reopen | **TRUE** — `released_at`, `released_by` |
| P3 | The Head accepts a change request → `_open_next_attempt(redo=None)`, and NO attempt is counted against the designer | **Mechanism TRUE; the last clause is FALSE.** Two shipped metrics count the reopened attempt against the designer. |
| P4 | Raising a change request *requires* a DRAFT procurement-group membership; the gate is computed in two functions (§B1) | **FALSE as stated.** Membership only *widens* the window to include `released`. There are two functions, but a third, divergent gate also exists. §B1 lives in `docs/EXECUTION_MODULE_DEFERRED.md`. |
| P5 | Only a PM may raise; SCM cannot | **TRUE in effect, but no role gate exists.** Authority is by identity — assigned PM **or** Project Coordinator. |

## P1 — one entry, one exit

**The entry.** Every product write of `DesignAssignment.status` goes through
`design_views.apply_design_status()`:

```python
    """THE ONE PLACE `DesignAssignment.status` IS WRITTEN. Session C (audit A-2.2 §5.1).
    ...
    DesignAssignment.objects.filter(pk=assignment.pk).update(**fields)
```

Its only caller that passes `DESIGN_RELEASED` is `design_views.design_head_qc_pass()`:

```python
        apply_design_status(
            assignment, DESIGN_RELEASED, profile,
            f'Design Head passed attempt {attempt.attempt_number} — design released',
            'design_head_qc_passed',
            extra_fields={'released_at': now, 'released_by': profile},
            entity_type='DesignAttempt', entity_id=attempt.pk)
```

That view is gated by `_qc_guard(request, project, (DESIGN_AWAITING_HEAD_QC,), gate='head')`.
The gate resolves to `permissions.user_can_head_gate_design()` → `user_can_qc_design()` →
`user_has_design_head_authority()`:

```python
    return user_is_design_head(user) or user_is_design_head_deputy(user)
```

So **"Design Head" includes the named deputy.**

The admin cannot write release. `admin.DesignAssignmentAdmin` declares:

```python
    readonly_fields = ['status', 'released_at', 'released_by', ...]
```

with the comment *"THE ADMIN IS NOT A RELEASE ROUTE"*.

Writes outside product code: `DesignAssignment.objects.create(..., status=DESIGN_RELEASED, ...)`
in the management commands `seed_opex_test_data`, `seed_scm_handoff_data` and `seed_scm_pilot`.

**The exit.** `design_views.design_change_request()` widens its status gate for a released
site that sits in a draft procurement group:

```python
    in_draft_group = membership is not None
    allowed_statuses = (CHANGE_REQUEST_STATUSES + (DESIGN_RELEASED,)
                        if in_draft_group else CHANGE_REQUEST_STATUSES)

    if assignment.status == DESIGN_RELEASED and not in_draft_group:
        return _back(f'{project.project_id}: the design is already released — a change '
                     f'now is a new scope of work, not a change request.')
```

`design_change_request_accept()` then calls `_open_next_attempt()`, which writes `in_design`
(see Part 4.3).

**The second exit that `docs/PHASE_2_PREFLIGHT_AUDIT.md` §6.1 reported is now CLOSED.**
`design_views.design_mark_blocked()`:

```python
    if assignment.status == DESIGN_RELEASED:
        return _deny(request,
                     f'{project.project_id} has already been released and cannot be '
                     f'placed on Design Hold through this action.',
                     'design_my_sites')
```

**Every other writer refuses a released row.** Each was checked individually:

| Writer | Guard |
|---|---|
| `design_survey_upload`, `design_survey_link_set` | Refuses a replacement unless `status in (DESIGN_AWAITING_SURVEY, DESIGN_AWAITING_ALLOCATION)` or the site is on hold; otherwise passes `new_status=None` |
| `_allocate_one` | `if assignment.status not in REALLOCATABLE_STATUSES: raise ValueError` |
| `design_arka_submit` | `ARKA_SUBMITTABLE_STATUSES = (DESIGN_IN_DESIGN, DESIGN_ARKA_REJECTED)` |
| `_verdict_target` / `_head_verdict_target` | `!= DESIGN_ARKA_SUBMITTED` / `!= DESIGN_AWAITING_HEAD_ARKA` |
| `_maybe_advance_to_artifacts_uploaded` | `if assignment.status != DESIGN_ARKA_SUBMITTED: return False` |
| QC start / pass / fail / head fail | `_qc_guard` requiring `(ARTIFACTS_UPLOADED,)`, `(IN_QC,)`, `(IN_QC,)`, `(AWAITING_HEAD_QC,)` |
| `design_change_request_accept` | **THE exit.** It has **no status check of its own**. It relies on the raise-time gate, and checks only `change.verdict != CHANGE_REQUEST_PENDING`. |

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

Neither field is cleared on reopen. `_open_next_attempt()`'s only status write passes
`extra_fields={'current_attempt_number': next_number}`. The only product writer of either
field is `design_head_qc_pass()`. `tests_design_status_chokepoint._DIRECT_WRITE_RE`
forbids a direct `assignment.released_(at|by) =` assignment.

## P3 — the mechanism is true; "not counted against the designer" is false

The mechanism is quoted in full in Part 4.2–4.3. The false clause is shown in Part 4.4.

## P4 — the draft group widens the window; it is not a requirement

A site whose status is in `CHANGE_REQUEST_STATUSES` is change-requestable with **no group
at all** (Part 4.2). Membership matters in two ways only. A LOCKED procurement group
*refuses* the request (Part 4.5). A DRAFT group *adds* `released` to the allowed statuses.

**Two functions, two spellings: TRUE.** In `design_change_request()`:

```python
    in_draft_group = membership is not None
```

In `design_change_request_form()`:

```python
    group_locked = membership is not None and membership.group.status == SITE_GROUP_LOCKED
    in_draft_group = membership is not None and not group_locked
```

**A third gate, `design_views.pm_change_request_targets()`, has no widening at all:**

```python
        if assignment is None or assignment.status == DESIGN_RELEASED:
            continue
```

**Citation.** The root `EXECUTION_MODULE_DEFERRED.md` has no §B1; its sections are numbered.
The entry is in `docs/EXECUTION_MODULE_DEFERRED.md`:

> *"### B1 — `in_draft_group` is computed twice, with two different spellings"*

## P5 — authority by identity, not by role

Both change-request views carry `@login_required` only. The routes `design_change_request_form`
and `design_change_request` in `urls.py` carry no `role_required`. The gate is
`permissions.user_can_request_design_change()`:

```python
    if project is None:
        return False
    return user_can_manage_project(user, project)
```

and `permissions.user_can_manage_project()`:

```python
    if project.assigned_pm == profile:          # PM authority — always checked, never gated
        return True
    return project.coordinators.filter(pk=profile.pk).exists()  # additive coordinator authority
```

SCM is excluded only because SCM users cannot fill either slot:

- `Project.assigned_pm` carries `limit_choices_to={'role': 'PM'}`.
- `Project.coordinators` carries `limit_choices_to={'role': 'Project Coordinator'}`.

`limit_choices_to` restricts forms and the admin only; it is not a database constraint. The
docstring of `permissions.user_can_view_project()` notes one such gap: assigned PMs are
*"reachable via the Zoho webhook, which matches on email with no role filter"*. On the local
dump, no OPEX project has a non-PM `assigned_pm` or a non-coordinator coordinator.

---

# PART 1 — THE STATUS LADDER, AND WHO READS IT

## 1.1 The fourteen values

`models.DESIGN_ASSIGNMENT_STATUS_CHOICES`, in declared order:

| # | Constant | Stored | Label |
|---|---|---|---|
| 1 | `DESIGN_AWAITING_SURVEY` | `awaiting_survey` | Awaiting survey |
| 2 | `DESIGN_AWAITING_ALLOCATION` | `awaiting_allocation` | Awaiting allocation |
| 3 | `DESIGN_ALLOCATED` | `allocated` | Allocated |
| 4 | `DESIGN_DUE_DATE_PROPOSED` | `due_date_proposed` | Due date proposed |
| 5 | `DESIGN_IN_DESIGN` | `in_design` | In design |
| 6 | `DESIGN_ARKA_SUBMITTED` | `arka_submitted` | Arka submitted |
| 7 | `DESIGN_AWAITING_HEAD_ARKA` | `awaiting_head_arka` | Arka — awaiting Design Head |
| 8 | `DESIGN_ARKA_REJECTED` | `arka_rejected` | Arka rejected |
| 9 | `DESIGN_ARTIFACTS_UPLOADED` | `artifacts_uploaded` | Artifacts uploaded |
| 10 | `DESIGN_IN_QC` | `in_qc` | In QC |
| 11 | `DESIGN_AWAITING_HEAD_QC` | `awaiting_head_qc` | QC passed — awaiting Design Head |
| 12 | `DESIGN_QC_FAILED` | `qc_failed` | QC failed |
| 13 | `DESIGN_RELEASED` | `released` | Released |
| 14 | `DESIGN_SURVEY_RETURNED` | `survey_returned` | Design Hold — survey inadequate |

`DesignAssignment.status`:

```python
    status = models.CharField(
        max_length=30, choices=DESIGN_ASSIGNMENT_STATUS_CHOICES,
        default=DESIGN_AWAITING_SURVEY,
    )
```

**`max_length=30`.** The longest stored value today is `awaiting_allocation` (19 characters).

Declared order is not a strict ladder. The model's own comment says `survey_returned` is an
*"off-sequence DESIGN HOLD FLAG ... listed last for that reason"*. `arka_rejected` and
`qc_failed` are loops back to the designer.

## 1.2 Writers and readers, per value

**Writers.** Every product write goes through `apply_design_status()`. Two helpers compute
the value they pass in:

- `_status_after_unblock()` returns `DESIGN_AWAITING_ALLOCATION` if there is no designer,
  `DESIGN_IN_DESIGN` if there is an effective commitment, and otherwise `DESIGN_ALLOCATED`.
- `_open_next_attempt()` passes `opening_status`: `DESIGN_ARKA_SUBMITTED` if an Arka
  approved at both gates was carried forward, otherwise `DESIGN_IN_DESIGN`.

**Pass-through readers that apply to all fourteen values** are not repeated in each row:

- `design_views.DESIGN_MIRROR_STATE_MAP`, via `derive_design_mirror_state()`.
- `sync_design_mirror()`, called from `apply_design_status()`, and from
  `utils.attach_opex_template()` as `sync_design_mirror(project, assignment.status, None)`.
- `design_metrics._classify()`, via its fallback `if status in STAGE_LABELS: return status`.
- `designer_dashboard_context()`, via `_DESIGNER_ACTIONS.get(assignment.status, ('none', '', ''))`.
- Every `get_status_display` / `status_label` render. These are in `head_sites.html`,
  `my_sites.html`, `qc_dashboard.html`, `tender_dashboard.html`, `change_request.html`,
  `head_review.html`, `qc_review.html`, `_design_status_chips.html` and
  `_dashboard_design_chips.html`.

**Test counts** include some uses of the same string that are not status reads, such as the
`detail` argument of `apply_design_status()`. They are counts of files, not assertions.

| Value | Product writers | Product .py readers (function / constant) | Template readers | Commands | Tests (files) |
|---|---|---|---|---|---|
| `awaiting_survey` | Field default on `_get_or_create_assignment()`'s `create()`. Both callers move the row on in the same atomic block, so **no product path commits it**. | `design_head_sites` (fallback value); `design_survey_upload` and `design_survey_link_set` (lock tuple, and `elif ... == DESIGN_AWAITING_SURVEY`); `_DESIGNER_ACTIONS`; `designer_dashboard_context.can_mark_blocked`; `design_metrics.STAGE_ORDER` | — | — | mirror_derivation, status_chokepoint |
| `awaiting_allocation` | `design_survey_upload`; `design_survey_link_set`; `_status_after_unblock` | `REALLOCATABLE_STATUSES` (read by `design_head_sites` and `_allocate_one`); survey lock tuples; `can_mark_blocked`; `_DESIGNER_ACTIONS`; `design_head_dashboard_counts['awaiting_allocation']`; `STAGE_ORDER`; `HEAD_ACTION_STAGES` | `dashboard/design.html` (`head_counts.awaiting_allocation`) | — | mirror_derivation, part8, part9, status_chokepoint, transition_ledger |
| `allocated` | `_status_after_unblock` only — a legacy Part 2 fallback. Its own comment: *"nothing allocated under Part 8 can reach this line"* | `REALLOCATABLE_STATUSES`; `_DESIGNER_ACTIONS` (`'propose_due'`); `STAGE_ORDER` | — | — | mirror_derivation, status_chokepoint |
| `due_date_proposed` | **None anywhere** | `REALLOCATABLE_STATUSES`; `_DESIGNER_ACTIONS`; `STAGE_ORDER`; `HEAD_ACTION_STAGES` | — | — | mirror_derivation |
| `in_design` | `_allocate_one`; `_status_after_unblock`; `_open_next_attempt` (from `design_qc_fail`, `design_head_qc_fail`, `design_change_request_accept`) | `REALLOCATABLE_STATUSES`; `ARKA_SUBMITTABLE_STATUSES` (read by `design_site_workspace` and `design_arka_submit`); `CHANGE_REQUEST_STATUSES`; `_DESIGNER_ACTIONS`; `STAGE_ORDER`; `_classify` (also its fallback bucket) | — | `seed_opex_test_data` | 12 files |
| `arka_submitted` | `design_arka_submit`; `design_arka_head_approve` (from `awaiting_head_arka`); `_open_next_attempt` | `CHANGE_REQUEST_STATUSES`; `_maybe_advance_to_artifacts_uploaded`; `_verdict_target`; `design_head_review`; `design_qc_queue` (Arka `status__in`); `designer_dashboard_context`; `design_head_dashboard_counts['awaiting_arka']`; `design_qc_dashboard_counts` (`gate1_owed`, `awaiting_arka`); `_classify` | `_dashboard_design_chips.html`, `_design_status_chips.html` (raw status); `qc_dashboard.html`, `tender_dashboard.html` (stage key) | — | 8 files |
| `awaiting_head_arka` | `design_arka_approve` | `CHANGE_REQUEST_STATUSES`; `design_head_review`; `_head_verdict_target`; `design_qc_queue`; `_DESIGNER_ACTIONS`; `design_head_dashboard_counts`; `STAGE_ORDER`; `HEAD_ACTION_STAGES`; `review_queue_age`; `attention_list` | both chip partials; `qc_dashboard.html`, `tender_dashboard.html` (stage key); `dashboard/design.html` | — | combined_submission, mirror_derivation, part9 |
| `arka_rejected` | `design_arka_reject`; `design_arka_head_reject` | `ARKA_SUBMITTABLE_STATUSES`; `CHANGE_REQUEST_STATUSES`; `_DESIGNER_ACTIONS`; `_classify` (→ `'in_design'`) | `_dashboard_design_chips.html` (×3); `_design_status_chips.html` | — | combined_submission, mirror_derivation, part9 |
| `artifacts_uploaded` | `_maybe_advance_to_artifacts_uploaded` only | `CHANGE_REQUEST_STATUSES`; `views.TENDER_DESIGN_SUBMITTED_STATUSES`; `design_qc_start` (`_qc_guard`); `design_qc_queue`; `design_qc_review.can_start_qc`; `_DESIGNER_ACTIONS`; both dashboard-count functions; `STAGE_ORDER`; `QC_ACTION_STAGES` | both chip partials | — | 6 files |
| `in_qc` | `design_qc_start` | `CHANGE_REQUEST_STATUSES`; `TENDER_DESIGN_SUBMITTED_STATUSES`; `design_qc_pass` and `design_qc_fail` (`_qc_guard`); `design_change_request.was_in_qc`; `design_qc_queue`; `design_qc_review.can_verdict`; `_DESIGNER_ACTIONS`; both dashboard-count functions; `STAGE_ORDER`; `QC_ACTION_STAGES` | `change_request.html` (`assignment.status == 'in_qc'`); `_dashboard_design_chips.html`; `dashboard/design.html` counts | — | 6 files |
| `awaiting_head_qc` | `design_qc_pass` | `CHANGE_REQUEST_STATUSES`; `TENDER_DESIGN_SUBMITTED_STATUSES`; `design_head_qc_pass` and `design_head_qc_fail` (`_qc_guard`); `was_in_qc`; `design_qc_queue`; `design_qc_review.awaiting_head`; `_DESIGNER_ACTIONS`; `design_head_dashboard_counts`; `STAGE_ORDER`; `HEAD_ACTION_STAGES`; `review_queue_age` | both chip partials; `qc_queue.html`, `qc_review.html` (the `awaiting_head` flag) | — | 5 files |
| `qc_failed` | **None.** Session C removed the write (see the comment in `design_qc_fail`) | `CHANGE_REQUEST_STATUSES`; `TENDER_DESIGN_SUBMITTED_STATUSES`; `_classify` | — | — | mirror_derivation, status_chokepoint |
| `released` | `design_head_qc_pass` only | See 1.3 | See 1.3 | `seed_opex_test_data`, `seed_scm_handoff_data`, `seed_scm_pilot` | 10 files |
| `survey_returned` | `design_mark_blocked` | `design_head_sites.is_blocked`; `design_my_sites.is_blocked`; `was_blocked` in both survey views; `_allocate_one`; `design_mark_blocked`; `_DESIGNER_ACTIONS`; `designer_dashboard_context` (`is_blocked`, `can_mark_blocked`); `_classify` (→ `'blocked'`); `tender_metrics['blocked']`; `design_analytics.m_hold_rate` | Only via the `is_blocked` / `blocked` flags | — | part8, part10, mirror_derivation, status_chokepoint |

String literals that are **not** this field were excluded:

- `ATTEMPT_REASON_QC_FAILED = 'qc_failed'` (`DesignAttempt.opened_reason`, rendered in
  `_attempt_history.html`).
- `SiteGroup.status` `'draft'` / `'locked'`.
- Arka verdicts.
- BOQ status tuples.
- `design_analytics` `'allocated'`, which tests `assigned_at`.
- `m_cr_by_stage` buckets, which are derived from timestamps.
- `scm_opex_tender_rows['in_design']`, which is a count of **all** assignments under a
  misleading key.

## 1.3 Every read of `released` — and what each does with a new status S immediately before it

S means a hypothetical new stored value, placed in the choices before `released` and written
by the Head's pass instead of `released`. Each line states **today's code's** behaviour. It
proposes nothing.

### Python — `design_views.py`

| # | Location | Read | A site in S would… |
|---|---|---|---|
| 1 | `DESIGN_MIRROR_STATE_MAP` / `derive_design_mirror_state()` | `DESIGN_RELEASED: Task.DONE`; unknown values `raise ValueError(...)` | **Fail to enter S on an activated site.** `sync_design_mirror()` calls `derive_design_mirror_state()` only when a mirror Task exists. The raise is deliberately unguarded inside `apply_design_status()`, so the Head's pass would roll back and 500. On an unactivated site the lookup returns early and the move succeeds silently. `tests_design_mirror_derivation.test_01` fails the moment S is added without a map entry. |
| 2 | `design_my_sites` | `'can_request_extension': ... and assignment.status != DESIGN_RELEASED`; `'is_released': assignment.status == DESIGN_RELEASED` | Offer the designer **Request extension** and **Place on Design Hold** on a design the Head has passed. |
| 3 | `design_due_date_propose` | `if assignment.status == DESIGN_RELEASED: return _deny(...)` | **Accept** an extension request. |
| 4 | `design_due_date_change` | `if assignment.status == DESIGN_RELEASED: return _back(...)` | **Accept** a Head due-date change. |
| 5 | `design_mark_blocked` | `if assignment.status == DESIGN_RELEASED: return _deny(...)` | **Accept a Design Hold.** The hold then clears through `_status_after_unblock()`, which returns `DESIGN_IN_DESIGN` because an effective commitment exists. That reopens the §6.1 route for S: a Head-passed site returns to `in_design` with no attempt, no change request, and `released_at` still unset. |
| 6 | `design_change_request` | `allowed_statuses = (CHANGE_REQUEST_STATUSES + (DESIGN_RELEASED,) if in_draft_group else ...)`; `if assignment.status not in allowed_statuses:` | **Refuse** with *"a change request cannot be raised at this stage (status "…")"*. S is not in `CHANGE_REQUEST_STATUSES`, and an S site cannot be in a group (row 15), so there is no widening. |
| 7 | `CHANGE_REQUEST_STATUSES` | released is excluded by omission | As row 6. |
| 8 | `design_qc_review` | `'released': assignment.status == DESIGN_RELEASED` | Show neither the gate-2 form (`awaiting_head` is False) nor the released note. `qc_review.html` falls to its `{% else %}`: *"Nothing to review yet. Design QC starts once the package is complete"* — **false for a passed package.** |
| 9 | `_DESIGNER_ACTIONS` | `DESIGN_RELEASED: ('none', '', 'Design released. Nothing further to do.')` | `.get()` defaults to `('none', '', '')`, so the designer's card shows **no action and no waiting text.** |
| 10 | `designer_dashboard_context` | `'can_mark_blocked': is_designer and assignment.status not in (DESIGN_SURVEY_RETURNED, DESIGN_RELEASED, ...)` | Offer **Place on Design Hold** on the dashboard (see row 5). |
| 11 | `pm_change_request_targets` | `if assignment is None or assignment.status == DESIGN_RELEASED: continue` | **Offer the PM the "Request design change" button**, because `qc_started_at` is set. The form it opens then says the window is closed (row 12). |
| 12 | `design_change_request_form` | `allowed_statuses` as row 6; `'released': assignment.status == DESIGN_RELEASED and not in_draft_group` | Render `window_open=False`, `group_locked=False`, `released=False`. The template reaches `{% else %}`: *"QC has not started on the current package, so there is nothing settled to raise a change against"* — **false.** |
| 13 | `post_qc_pool` | `.filter(..., status=DESIGN_RELEASED)` | Be **absent from SCM's pool** on `site_group_list`, `site_group_detail` and the SCM dashboard. |
| 14 | `tender_release_completeness` | `status=DESIGN_RELEASED).count()` | Not count toward "X of Y released" on the three SCM surfaces. |
| 15 | `_add_sites` | `if assignment is None or assignment.status != DESIGN_RELEASED:` | Be **refused** from a procurement group: *"not released (<S label>) — only released sites can be grouped"*. |
| 16 | `_workspace_context` → `_design_status_chips.html` | `'status'` passed through; the template has no `released` branch | Show the grey `get_status_display` chip — the same as `released` today. |

### Python — `design_metrics.py` (the Head's tender dashboard, and the QC dashboard subset)

| # | Location | Read | A site in S would… |
|---|---|---|---|
| 17 | `is_overdue` | `if assignment.status == DESIGN_RELEASED: return False` | **Go overdue against the designer's agreed date** while it waits on someone else. It feeds the `overdue` tile, `designer_workload['overdue']` and the attention list. |
| 18 | `STAGE_ORDER` / `_classify` | `('released', 'Released')` is the last stage; `if status in STAGE_LABELS: return status`, else **`return 'in_design'`** | Be classified **"In design, awaiting Arka"** on both dashboards and in every `?stage=` drill-down. Nothing raises. It is not in `HEAD_ACTION_STAGES` or `QC_ACTION_STAGES`, so it never appears as "waiting on you" either. |
| 19 | `tender_metrics` `'released': a.status == DESIGN_RELEASED` → `designer_workload`, `no_due_date`, `attention_list` | the `s['released']` flag | Count as the designer's **current load** (sites and kW), be excluded from the `released` denominator of `rework` / `input_quality` / `pm_change_multiplier`, and stay eligible for the ≥3-revisions attention line. |

### Python — `design_analytics.py` (`design_quality_analytics` / `design_quality_analytics_tender`)

| # | Location | A site in S would… |
|---|---|---|
| 20 | `analytics_dataset` `'released': a.status == DESIGN_RELEASED`, consumed by `m_first_pass_rate`, `m_rework_multiplier`, `m_capacity_throughput`, `m_change_request_rate`, `m_on_time_delivery`, `m_cycle_time`, and `compute()`'s `'released_count'` | Drop out of every released denominator, and out of `m_on_time_delivery` and `m_cycle_time`, which read `released_at`. `m_stage_dwell`'s `'Head verdict → released'` interval reads `released_at`, so its meaning depends on which event stamps that field. |

### Python — `views.py`

| # | Location | A site in S would… |
|---|---|---|
| 21 | `TENDER_DESIGN_SUBMITTED_STATUSES` → `_get_ceo_dashboard_context` `tender_design_subq` → `design_done` | **Flip the CEO card's design pill from Done back to Not Done.** `awaiting_head_qc` is in the tuple and S is not. The tuple's own comment names that exact failure: *"Excluding it would make the pill flip Done -> Not Done"*. |

### Templates

| # | Template | Read | A site in S would… |
|---|---|---|---|
| 22 | `_dashboard_design_chips.html` | `{% if p.design.status == 'released' %}` (green) … `{% else %}` grey | Get the grey chip. |
| 23 | `head_sites.html` (×2) | `{% if row.current_due and row.assignment.status != 'released' %}` | Show the Head **Change date** (the view accepts it — row 4). |
| 24 | `my_sites.html` (×2) | `{% if not row.is_blocked and not row.is_released %}` | Show **Place on Design Hold** (the view accepts it — row 5). |
| 25 | `qc_review.html` | `{% if released %}` banner; `{% elif released %}` | No banner; see row 8. |
| 26 | `change_request.html` | `{% elif released %}` | See row 12. |
| 27 | `tender_dashboard.html` | **`{% with released=m.stages|last %}`** | **A positional read.** Correct only while `'released'` is the last entry of `STAGE_ORDER`. S inserted before it would be uncounted; S appended after it would make the release bar count S. |
| 28 | `quality_analytics.html` | `{{ released_count }}`, `{{ r.released }}` | Not counted. |
| 29 | `site_groups.html`, `site_group_detail.html` | `{{ released }} of {{ total_sites }}` | Not counted. |
| 30 | `_scm_opex_groups.html` | `{{ row.released }} of {{ row.total_sites }}` | Not counted. |
| 31 | `ceo.html` `card.design_done`; `pm.html` `can_request_design_change` | Indirect | Rows 21 and 11. |

**Management command read:** `seed_scm_pilot` echoes `existing.status` into its report. It
does not compare it.

**No reads of `released` in:** `permissions.py` (`project_boq_is_design_locked` reads
`boq_submitted_at`, not status), `utils.py` apart from the mirror pass-through, `forms.py`,
`signals.py`, `notifications.py`, `context_processors.py`, `reports.py`, `report_views.py`,
or any `.js`.

## 1.4 Every hardcoded status list — does a pre-release status belong in it?

No list was changed. "Belongs?" states whether S shares the **property the list encodes**,
as the list's own code or comment defines it.

| List | Where | Encodes | S belongs? |
|---|---|---|---|
| `REALLOCATABLE_STATUSES` | `design_views` | Work not yet started | No |
| `ARKA_SUBMITTABLE_STATUSES` | `design_views` | Designer may submit an Arka | No |
| `CHANGE_REQUEST_STATUSES` | `design_views` | The change window (QC started, not released) | **Undecided by code.** Its docstring says the window closes at "release". S sits inside that window if release means `released`, and outside it if release means the Head's pass. Rows 6, 11 and 12 disagree today. |
| `released`-equality guards in `design_due_date_propose`, `design_due_date_change`, `design_mark_blocked` | `design_views` | "The design is finished" | **Yes.** Each guard's refusal text says *finished*/*released*. Without S, rows 3–5 open. |
| `can_mark_blocked` tuple | `designer_dashboard_context` | Hold is meaningless here | **Yes** |
| `is_released` / `can_request_extension` | `design_my_sites` | As above | **Yes** |
| `_DESIGNER_ACTIONS` | `design_views` | One entry per status | **Needs an entry**, or it defaults silently to nothing |
| `DESIGN_MIRROR_STATE_MAP` | `design_views` | One entry per status; exhaustiveness is test-enforced | **Must have an entry** (Done or In Progress is a later decision) |
| Arka `status__in=(ARKA_SUBMITTED, AWAITING_HEAD_ARKA)` | `design_qc_queue` | Arkas awaiting a verdict | No |
| Package `status__in=(ARTIFACTS_UPLOADED, IN_QC, AWAITING_HEAD_QC)` | `design_qc_queue` | Packages awaiting a Design verdict | No. S is past both Design gates. |
| `_qc_guard` required tuples | 5 QC endpoints | Per-gate entry state | No |
| **The designer dashboard's six counters** — `design_head_dashboard_counts`: `awaiting_allocation`, `awaiting_arka`, `awaiting_head_arka`, `awaiting_head_qc`, `awaiting_qc`, `in_qc` | `design_views` | "Queue sizes a Design Head can see" | No. None of the six is work the Head owes. **S would appear in no counter anywhere.** |
| `design_qc_dashboard_counts` (`gate1_owed`, `awaiting_arka`, `awaiting_qc`, `in_qc`) | `design_views` | Gate-1 work owed | No |
| `STAGE_ORDER` | `design_metrics` | One tile per bottleneck, "never merged" | **Yes, as its own entry before `'released'`.** Otherwise `_classify` puts it in `'in_design'` (row 18), and the positional read (row 27) stays correct only if it goes *before*. |
| `HEAD_ACTION_STAGES` / `QC_ACTION_STAGES` | `design_metrics` | Ball in the Head's / QC's court | No |
| `_classify` branches | `design_metrics` | Stage mapping; fallback is `'in_design'` | **Needs an explicit branch or `STAGE_ORDER` key** |
| `is_overdue` | `design_metrics` | "not released" = still owed by the designer | **Undecided by code.** Today S would be overdue against the designer (row 17). |
| `TENDER_DESIGN_SUBMITTED_STATUSES` | `views` | "The designer has handed the artifact over" | **Yes.** The tuple's own comment argues for including every post-handover state. |
| `post_qc_pool`, `tender_release_completeness`, `_add_sites` (`status=DESIGN_RELEASED`) | `design_views` | Available to SCM for procurement | **Not if the gate is meant to precede SCM.** See 3.2. |
| `pm_change_request_targets` (`== DESIGN_RELEASED`) | `design_views` | Link offered | Must agree with `CHANGE_REQUEST_STATUSES` (row 11) |
| `design_quality_analytics_tender` → `analytics_dataset` | `design_analytics` | Only `released` and `survey_returned` are read; no other list | Unaffected apart from the `released` flag (row 20) |
| Tender dashboard | `design_tender_dashboard` → `tender_metrics` | `STAGE_ORDER`, `HEAD_ACTION_STAGES`, `m.stages|last` | As above |
| QC dashboard | `design_qc_dashboard` → `tender_metrics` subset | `stages`, `qc_queue`, `qc_attention` | Stage only |
| `_dashboard_design_chips.html` `if/elif` chain (`released`; `in_qc`/`artifacts_uploaded`/`awaiting_head_qc`/`awaiting_head_arka`; `arka_submitted`; `arka_rejected`) | template | Chip colour | A presentation decision. Unhandled, S is grey. |
| `_design_status_chips.html` chain | template | Same | Same |
| `change_request.html` `assignment.status == 'in_qc'` | template | "Raising suspends a review" | No. Separately, **it omits `awaiting_head_qc`**, which the view's `was_in_qc` includes (7.2). |
| `head_sites.html` `!= 'released'` | template | Change date allowed | **Yes** (as the view guard) |
| `qc_dashboard.html` / `tender_dashboard.html` `stage_key == 'arka_submitted' or 'awaiting_head_arka'` | template | Arka-row link target | No |

## 1.5 The Designer User Manual

**The manual is not in the repository, and I could not find it.** I searched
`D:\HRP\PMS` for `*anual*` / `*Manual*` / `*guide*`, and for the phrases "user manual",
"designer manual", "13 stages" and "thirteen". The only match is
`projects/tests_opex_manual_dates.py`, which is unrelated. **So I cannot say which value the
manual omits.**

What the code does say is which values current product code **never commits**:

- `due_date_proposed` — no writer anywhere.
- `qc_failed` — the write was removed in Session C.
- `awaiting_survey` — the default, moved on inside the same transaction.
- `allocated` — reachable only for rows allocated under Part 2.

`DESIGN_MIRROR_STATE_MAP`'s comment names three of these as statuses *"that nothing writes any
more (`allocated`, `due_date_proposed`, `qc_failed`)"*. The manual's omission is most likely
one of those, but that is inference and is not asserted here.

---

# PART 2 — WHAT RELEASE ACTUALLY DOES

## 2.1 `design_head_qc_pass()` in full

```python
def design_head_qc_pass(request, project_id):
    """GATE 2 — the DESIGN HEAD passes a package Design QC has already passed. RELEASE.

    Release sets `released_at` / `released_by` on the assignment and moves it to
    `released`. THAT IS ALL IT DOES (Part 4 settled decision 9). It does not lock, group
    or hand over the BOQ — those are Part 6, and reading anything into `released` beyond
    "design is finished" would pre-empt decisions that have not been made.

    This is where the attempt finally closes, and where Part 4's release behaviour now
    lives unchanged apart from which gate triggers it.
    """
    project = _opex_site(project_id)
    assignment, attempt, error = _qc_guard(request, project, (DESIGN_AWAITING_HEAD_QC,),
                                           gate='head')
    if error is None:
        return HttpResponseForbidden(_GATE_FORBIDDEN['head'])
    if request.method != 'POST':
        return redirect('design_qc_review', project_id=project.project_id)
    if error:
        messages.error(request, error)
        return redirect('design_qc_queue')

    blocked = _blocking_change_request(project, attempt)
    if blocked:
        messages.error(request, blocked)
        return redirect('design_qc_review', project_id=project.project_id)

    profile = request.user.profile
    if _other_gate_actor_conflict(profile, attempt.qc_reviewed_by_id):
        messages.error(request, f'{project.project_id}: you passed attempt '
                                f'{attempt.attempt_number} through Design QC yourself, so '
                                f'you cannot also release it as Design Head. It needs a '
                                f'second pair of eyes.')
        return redirect('design_qc_review', project_id=project.project_id)

    now = timezone.now()
    with transaction.atomic():
        attempt.head_verdict     = QC_PASSED
        attempt.head_reviewed_by = profile
        attempt.head_reviewed_at = now
        attempt.closed_at        = now
        # Both gates passed — they agree, so no overturn.
        attempt.head_overturned_qc = False
        attempt.save(update_fields=['head_verdict', 'head_reviewed_by', 'head_reviewed_at',
                                    'closed_at', 'head_overturned_qc'])
        # THE RELEASE STAMP AND THE STATUS ARE ONE WRITE. ...
        apply_design_status(
            assignment, DESIGN_RELEASED, profile,
            f'Design Head passed attempt {attempt.attempt_number} — design released',
            'design_head_qc_passed',
            extra_fields={'released_at': now, 'released_by': profile},
            entity_type='DesignAttempt', entity_id=attempt.pk)

    messages.success(request, f'{project.project_id}: both review gates passed — design '
                              f'released on attempt {attempt.attempt_number}.')
    return redirect('design_qc_review', project_id=project.project_id)
```

**Every write, inside one `transaction.atomic()`:**

| Target | What |
|---|---|
| `DesignAttempt` (current) | `head_verdict=QC_PASSED`, `head_reviewed_by`, `head_reviewed_at`, `closed_at`, `head_overturned_qc=False` |
| `DesignAssignment` | Via `apply_design_status()`, one `filter().update()`: `status='released'`, `released_at`, `released_by`, `updated_at` |
| `ActivityLog` | Via `log_activity()` inside `apply_design_status()`: `action_code='design_head_qc_passed'`, `entity_type='DesignAttempt'`, `entity_id=attempt.pk` |
| `StatusTransition` | `record_transition(assignment, 'released', from_status='awaiting_head_qc', actor=profile)`, subject type `design_assignment` |
| OPEX Design mirror `Task` | `sync_design_mirror()` → `apply_mirror_status(task, Task.DONE, ...)`: `status`, `completed_at`, plus a second `StatusTransition` row (`reason_code=REASON_MIRROR_DERIVED`). **Only on activated sites** — see 2.2. |
| Notifications | **None** (Part 6.2) |
| `Project`, `BOQ`, `SiteGroup`, `SiteGroupMembership` | **Nothing.** `Project.status` stays as it was; OPEX sites are created `Draft`. |

## 2.2 The Design mirror is now derived — the premise that it is unwritten is FALSE

`apply_design_status()`, under the did-it-move guard:

```python
    if new_status is not None and new_status != from_status:
        record_transition(
            assignment, new_status, from_status=from_status, actor=actor,
        )
        ...
        sync_design_mirror(assignment.project, assignment.status, actor)
```

`DESIGN_MIRROR_STATE_MAP` maps `DESIGN_RELEASED: Task.DONE` and every allocated-to-QC status to
`Task.IN_PROGRESS`. Its comment on reopen reads:

> *"NOT terminal ... One reopen route exists (a PM change request accepted by the Head,
> design_change_request_accept -> _open_next_attempt), which moves released -> in_design and
> therefore Done -> In Progress with no special case."*

The reconcile happens in `utils.attach_opex_template()`:
`sync_design_mirror(project, assignment.status, None)`.

**Scope on live data:** 93 `DesignAssignment` rows; **6** of those sites have tasks; **6**
Design mirror Tasks. The hook writes for those six. For the other 87 it returns early:
*"Design mirror sync skipped: %s has no tasks yet (not activated)."*

`docs/execution-model.md` §5, R-21, `OPEX_task_template_spec.md` §2 rules 3–4 and §6,
`EXECUTION_PROMPT_LOG.md`, and `docs/EXECUTION_MODULE_DEFERRED.md` B27 all still say no
derivation hook exists (Part 7.1).

## 2.3 The two BOQ lock predicates — release changes neither

`permissions.project_boq_is_design_locked()`:

```python
    if project is None:
        return False
    assignment = getattr(project, 'design_assignment', None)
    if assignment is None or not assignment.current_attempt_number:
        return False
    return assignment.attempts.filter(
        attempt_number=assignment.current_attempt_number,
        boq_submitted_at__isnull=False,
    ).exists()
```

`permissions.project_boq_is_group_locked()`:

```python
    if project is None:
        return False
    return project.group_memberships.filter(
        removed_at__isnull=True, group__status='locked',
        group_type='procurement',
    ).exists()
```

Neither predicate reads `status`. Release closes the attempt but does not change its
`boq_submitted_at`. The design lock is already True from the moment the BOQ was marked
complete, and release leaves it True. Its docstring table confirms: *"Design Head approves |
same attempt | -> DESIGN LOCK"*. Release creates no group membership, so the group lock is
unchanged.

## 2.4 What changes on screen at release

- **Designer.**
  - `_DESIGNER_ACTIONS[DESIGN_RELEASED]` → *"Design released. Nothing further to do."*
  - The dashboard chip turns green (`_dashboard_design_chips.html`,
    `{% if p.design.status == 'released' %}`).
  - `my_sites.html` hides **Place on Design Hold** (`is_released`).
  - `design_my_sites.can_request_extension` becomes False.
  - `can_mark_blocked` becomes False on the dashboard.
  - The workspace chip (`_design_status_chips.html`) has no `released` branch and stays grey.
- **Design QC.** `design_qc_review` → `qc_review.html`:
  - the *"Design released"* banner, with `released_at` and `released_by`;
  - the actions block reads *"This site is released — both gates are complete."*;
  - the site leaves `design_qc_queue`, whose `status__in` tuples exclude it, and leaves both
    count strips.
- **Design Head.**
  - the same QC-review banner;
  - the site leaves `awaiting_head_qc` in `design_head_dashboard_counts` and in
    `review_queue_age`;
  - on the tender dashboard it moves to the `'released'` stage tile and the release progress
    bar (`m.stages|last`);
  - `is_overdue` stops counting it;
  - `head_sites.html` hides **Change date**;
  - it becomes a `released` unit in `designer_workload` and the analytics denominators.
- **SCM** (Part 3). It appears in `post_qc_pool` and the "X of Y released" counts.
- **PM** (Part 5). `pm_change_request_targets` stops offering **Request design change**. The
  form, reached directly, shows the *"design for this site was released"* note.

---

# PART 3 — WHEN SCM FIRST SEES A DESIGN

## 3.1 The eligibility predicates

`design_views.post_qc_pool()`:

```python
    rows = list(
        DesignAssignment.objects
        .filter(project__program=program, project__is_deleted=False,
                status=DESIGN_RELEASED)
        .exclude(project__in=SiteGroupMembership.objects
                 .filter(removed_at__isnull=True,
                         group_type=GROUP_TYPE_PROCUREMENT).values('project_id'))
        .select_related('project', 'released_by__user')
        .order_by('released_at')
    )
```

`design_views._add_sites()`, per site:

```python
        if project.program_id != group.program_id:
            refused.append(f'{project.project_id}: belongs to a different tender.')
            continue

        assignment = getattr(project, 'design_assignment', None)
        if assignment is None or assignment.status != DESIGN_RELEASED:
            state = assignment.get_status_display() if assignment else 'design not started'
            refused.append(f'{project.project_id}: not released ({state}) — only released '
                           f'sites can be grouped for procurement.')
            continue
        ...
        existing = active_group_membership(project, GROUP_TYPE_PROCUREMENT)
        if existing is not None:
            refused.append(...)
            continue
```

A site is available for grouping when all of these hold:

- it is not deleted;
- it is in the group's tender;
- its **stored status equals `'released'`**;
- it has no live procurement membership.

`site_group_add_sites` additionally requires `group.status == SITE_GROUP_DRAFT`. The write
gate is `permissions.user_can_manage_site_groups()`, which returns `profile.role == 'SCM'`.

## 3.2 If release became PM-gated

Both predicates test **stored equality with `DESIGN_RELEASED`**. Neither reads `released_at`,
a verdict, or an attempt. So:

- If the PM gate sat **before** the write of `'released'` (a new status S), sites would
  enter SCM's pool **after** PM approval. S is excluded by `status=DESIGN_RELEASED`.
- If the Head's pass kept writing `'released'` and the PM step were recorded elsewhere, sites
  would enter the pool **before** PM approval. That is today's timing.

The pool orders and ages by `released_at`, and `released_at` is stamped by the Head's pass.
If S kept that stamp at the Head's pass, the pool would show ages that include the time spent
waiting on the PM.

## 3.3 Can SCM see or act on a design before release?

**See: yes, the BOQ. Act: no.**

- **BOQ read.** `permissions.BOQ_PORTFOLIO_READ_ROLES = frozenset({'SCM', 'Admin', 'CEO'})`
  admits SCM in `user_can_view_project_boq()`, on every project, released or not.
  `views.boq_detail` gates on `user_can_view_project_boq` and does not redirect SCM to the
  picker; only the author is redirected. `views.opex_boq_download` gates on the same helper,
  and its docstring says *"NO LOCK CHECK, DELIBERATELY"*. So SCM can open and download the
  in-progress quantities of an unreleased OPEX site.
- **Project read.** `permissions.PORTFOLIO_VIEW_ROLES = frozenset({'CEO', 'Finance', 'SCM', 'Admin'})`.
- **BOQ write.** The SCM branch of `boq_detail` requires
  `role == 'SCM' and boq.status in ('Submitted', 'Acknowledged')`. No OPEX path writes
  `BOQ.status`. The only writers of `'Submitted'` are `boq_detail`'s `submit_design` branch
  and `boq_submit`, and an OPEX author is redirected to `opex_boq_entry`, which writes no BOQ
  status. **Live:** every OPEX BOQ is `Draft` — 3 on released sites, 4 on unreleased ones. The
  SCM write branch is unreachable for OPEX today, released or not.
- **SCM dashboard.** `scm_opex_tender_rows()` shows only counts for unreleased sites.
  `'in_design': assignments` is the count of **all** assignments under a misleading key.
  Everything site-level goes through `post_qc_pool()`, which is released only.
- **Site-group screens.** `site_group_list` and `site_group_detail` show only `post_qc_pool`
  and members. Members are released by construction.
- **Design screens.** SCM passes no design gate. `user_can_view_site_groups` lets SCM into
  group screens only.

---

# PART 4 — THE CHANGE-REQUEST PATH, END TO END

## 4.1 `DesignChangeRequest`

```python
CHANGE_REQUEST_PENDING  = 'pending'
CHANGE_REQUEST_ACCEPTED = 'accepted'
CHANGE_REQUEST_REJECTED = 'rejected'

CHANGE_REQUEST_VERDICT_CHOICES = [
    (CHANGE_REQUEST_PENDING,  'Pending'),
    (CHANGE_REQUEST_ACCEPTED, 'Accepted'),
    (CHANGE_REQUEST_REJECTED, 'Rejected'),
]
```

| Field | Definition | Related name |
|---|---|---|
| `attempt` | `ForeignKey(DesignAttempt, on_delete=CASCADE)` | `change_requests` |
| `requested_by` | `ForeignKey('UserProfile', on_delete=PROTECT)` | `design_change_requests` |
| `reason` | `TextField()` | — |
| `requested_at` | `DateTimeField(auto_now_add=True)` | — |
| `resulting_attempt` | `ForeignKey(DesignAttempt, null=True, blank=True, on_delete=SET_NULL)` | `caused_by_change_request` |
| `verdict` | `CharField(max_length=10, choices=CHANGE_REQUEST_VERDICT_CHOICES, default=CHANGE_REQUEST_PENDING)` | — |
| `decided_by` | `ForeignKey('UserProfile', null=True, blank=True, on_delete=SET_NULL)` | `triaged_design_change_requests` |
| `decided_at` | `DateTimeField(null=True, blank=True)` | — |
| `rejection_reason` | `TextField(blank=True, default='')` | — |

`Meta`: `ordering = ['-requested_at']`. **There are no indexes** beyond the implicit
foreign-key indexes. Constraints:

```python
            models.CheckConstraint(
                condition=(~models.Q(verdict=CHANGE_REQUEST_REJECTED)
                           | ~models.Q(rejection_reason='')),
                name='cr_rejection_reason_required_when_rejected',
            ),
            models.UniqueConstraint(
                fields=['attempt'],
                condition=models.Q(verdict=CHANGE_REQUEST_PENDING),
                name='uniq_pending_change_request_per_attempt',
            ),
```

**The constraint that refuses a second pending request is
`uniq_pending_change_request_per_attempt`.** The standing test
`tests_design_part46.RaisingTests.test_02_a_second_pending_request_is_refused_by_the_database`
asserts it by name:

```python
        with self.assertRaises(IntegrityError) as caught:
            with transaction.atomic():
                DesignChangeRequest.objects.create(
                    attempt=self.attempt, requested_by=self.pm,
                    reason='and another thing', verdict=CHANGE_REQUEST_PENDING)
        self.assertIn('uniq_pending_change_request_per_attempt', str(caught.exception))
```

The request hangs off the **attempt**, not the assignment. A released site's change request
is raised against the attempt `design_head_qc_pass()` already closed.

## 4.2 Gates, preconditions and writes

**Form (GET) — `design_change_request_form()`**

- **Gate:** `@login_required` and `if not user_can_request_design_change(request.user, project): return HttpResponseForbidden(...)`.
- **Other refusals:** `_opex_site()` 404s a non-OPEX site. No assignment →
  `raise Http404('Design has not started on this site yet.')`.
- **Computes:**

```python
    membership   = active_group_membership(project, GROUP_TYPE_PROCUREMENT)
    group_locked = membership is not None and membership.group.status == SITE_GROUP_LOCKED
    in_draft_group = membership is not None and not group_locked
    allowed_statuses = (CHANGE_REQUEST_STATUSES + (DESIGN_RELEASED,)
                        if in_draft_group else CHANGE_REQUEST_STATUSES)

    window_open = bool(attempt and attempt.qc_started_at
                       and assignment.status in allowed_statuses
                       and not group_locked)
```

- **Writes:** none.

**Raise (POST) — `design_change_request()`.** Gates and preconditions, in order:

1. `user_can_request_design_change`, else 403.
2. A GET redirects to the form.
3. There is an assignment.
4. `reason` is non-empty.
5. There is a current attempt.
6. The locked-group refusal (4.5).
7. `if assignment.status == DESIGN_RELEASED and not in_draft_group:` refuse.
8. `if attempt.qc_started_at is None:` refuse.
9. `if assignment.status not in allowed_statuses:` refuse.
10. `_pending_change_requests(attempt).first()` must be None.

Writes, in one atomic block:

```python
            if in_draft_group:
                remove_from_group(membership, profile, CHANGE_REQUEST_REMOVAL_REASON)

            change = DesignChangeRequest.objects.create(
                attempt=attempt, requested_by=profile, reason=reason,
                verdict=CHANGE_REQUEST_PENDING)
            log_activity(project, profile, ..., entity_type='DesignChangeRequest',
                         entity_id=change.pk, action_code='design_change_requested')
```

`remove_from_group()` stamps `removed_at`, `removed_by`, `removal_reason` and logs
`site_group_site_removed`. An `IntegrityError` is caught and reported as *"refused by the
database"*. **No status moves, and no attempt opens.**

**While a request is pending,** `_blocking_change_request()` refuses every verdict at both
gates, `design_head_qc_pass` included. `site_group_lock` also refuses while any member has a
pending request (`pending_change_requests_for`).

**Accept — `design_change_request_accept()`**

- **Gate:** `_triage_guard` → `user_has_design_head_authority(request.user)` (Head or
  deputy), and POST only.
- **Precondition:** it re-reads the request under `select_for_update()` and requires
  `change.verdict == CHANGE_REQUEST_PENDING`. **There is no assignment-status check.**
- **Writes:**

```python
        change.verdict    = CHANGE_REQUEST_ACCEPTED
        change.decided_by = profile
        change.decided_at = timezone.now()
        change.save(update_fields=['verdict', 'decided_by', 'decided_at'])

        new_attempt = _open_next_attempt(
            assignment, ATTEMPT_REASON_PM_CHANGE_REQUEST, profile,
            f'change request accepted on attempt {change.attempt.attempt_number}')

        change.resulting_attempt = new_attempt
        change.save(update_fields=['resulting_attempt'])

        log_activity(project, profile, ..., entity_type='DesignChangeRequest',
                     entity_id=change.pk, action_code='design_change_request_accepted')
```

**Reject — `design_change_request_reject()`**

- **Gate:** the same `_triage_guard`.
- **Precondition:** `rejection_reason` is non-empty (in the view, and by the CHECK
  constraint); the request is re-read under `select_for_update()` and must be pending.
- **Writes:** `verdict=REJECTED`, `rejection_reason`, `decided_by`, `decided_at`; then
  `log_activity(..., action_code='design_change_request_rejected')`.
- **Nothing reopens.** A site that left a draft group on raise **stays out.** The comment in
  `design_change_request()`: *"If the Head rejects, SCM re-adds the site; it is still
  `released`, so `_add_sites()` accepts it back."*

## 4.3 `_open_next_attempt()` in full

```python
def _open_next_attempt(assignment, reason, actor, detail, redo=None):
    """Close the current attempt and open the next one. THE ONLY PLACE THIS HAPPENS.
    ... (docstring: callers own the transaction; qc_verdict on the outgoing attempt,
    assigned_to, the survey and the approved DueDateCommitment are NOT touched; a PM
    change request passes redo=None on purpose; the opening status follows the Arka)
    """
    now = timezone.now()
    current = _current_attempt(assignment)
    if current is not None and current.closed_at is None:
        current.closed_at = now
        current.save(update_fields=['closed_at'])

    next_number = (assignment.attempts.aggregate(m=Max('attempt_number'))['m'] or 0) + 1
    new_attempt = DesignAttempt.objects.create(
        assignment=assignment, attempt_number=next_number, opened_reason=reason,
    )

    carried = []
    if redo is not None and current is not None:
        carried = _carry_forward_artifacts(current, new_attempt, redo)

    carried_arka = _current_arka(new_attempt)
    opening_status = (DESIGN_ARKA_SUBMITTED
                      if (carried_arka is not None
                          and carried_arka.head_verdict == ARKA_APPROVED)
                      else DESIGN_IN_DESIGN)

    detail_suffix = f' — carried forward: {", ".join(carried)}' if carried else ''

    apply_design_status(
        assignment, opening_status, actor,
        f'Attempt {next_number} opened ({new_attempt.get_opened_reason_display()}): '
        f'{detail}{detail_suffix}',
        f'design_attempt_opened_{reason}',
        extra_fields={'current_attempt_number': next_number},
        entity_type='DesignAttempt', entity_id=new_attempt.pk)

    if carried:
        _maybe_advance_to_artifacts_uploaded(assignment, new_attempt, actor)

    return new_attempt
```

**What it resets**

- On the **outgoing attempt:** `closed_at` only, and only if it is still open. A released
  site's attempt was already closed by `design_head_qc_pass()`, so nothing is written to it.
- On the **assignment:** `status` becomes `opening_status`; `current_attempt_number` becomes
  N+1; `updated_at`.
- It **creates** a fresh `DesignAttempt` whose every verdict, stamp and `boq_submitted_at`
  starts at its model default. With `redo=None`, `_carry_forward_artifacts()` is never
  called, so `boq_submitted_at` is null. That flips `project_boq_is_design_locked()` to False
  and reopens the BOQ. It also moves the Design mirror Done → In Progress, and writes a
  `StatusTransition` row and an `ActivityLog` row (`design_attempt_opened_<reason>`).

**What it leaves standing**

- `released_at`, `released_by`;
- `assigned_to` / `assigned_by` / `assigned_at`;
- `qc_assigned_to` / `qc_assigned_by` / `qc_assigned_at`;
- the survey fields and the `survey_returned_*` triple;
- every `DueDateCommitment`;
- the outgoing attempt's verdicts, remarks, categories, `redo_required` and
  `boq_submitted_at`;
- `DesignChangeRequest` rows;
- any `SiteGroupMembership`.

**Reason passed by each caller**

| Caller | `reason` | `redo` |
|---|---|---|
| `design_qc_fail` | `ATTEMPT_REASON_QC_FAILED` (`'qc_failed'`) | the reviewer's set |
| `design_head_qc_fail` | `ATTEMPT_REASON_QC_FAILED` (`'qc_failed'`) | the reviewer's set |
| `design_change_request_accept` | `ATTEMPT_REASON_PM_CHANGE_REQUEST` (`'pm_change_request'`) | not passed → `None` |

`ATTEMPT_REASON_INITIAL` is never passed. Attempt 1 is opened by `_open_first_attempt()`.

## 4.4 The rework, input and PM-change figures — and whether a change request touches them

The three figures are computed in **`design_metrics.designer_workload()`**, which backs the
tender dashboard's workload table. All three share the denominator `released`, the per-designer
count of `s['released']`:

```python
        designer_attempts = row['attempts'] - row['input_problem_attempts']
        row['rework'] = (round(designer_attempts / released, 1) if released else None)
        row['input_quality'] = (round(row['input_problem_attempts'] / released, 1)
                                if released else None)
        row['pm_change_multiplier'] = (round(row['pm_change_request'] / released, 1)
                                       if released else None)
```

`row['attempts'] += len(s['attempts'])` counts **every** attempt. `input_problem_attempts` is
incremented only for causes `'B'` / `'C'`.

**A PM change request contributes to TWO of the three figures, not one:**

- `input_quality`: **no.** `classify_attempt_causes()` maps a `pm_change_request` attempt to
  `CAUSE_PM_CHANGE`, which is not `'B'`/`'C'`.
- `rework`: **YES.** The attempt is in `row['attempts']` and is not subtracted. The function's
  own docstring says *"rework — attempts caused by a GROUP A failure"*. The code disagrees.
- `pm_change_multiplier`: yes, by design.

A change request that reopens a **released** site also enlarges nothing in the denominator:
the site is no longer `released` until it is re-released.

Elsewhere, `design_analytics.m_rework_multiplier()` **does** exclude it
(`if cause in (ERROR_GROUP_B, ERROR_GROUP_C, CAUSE_PM_CHANGE): ... continue`), and its
docstring names the divergence: *"the tender dashboard's rework column, which keeps PM-change
attempts in."* `design_analytics.m_first_pass_rate()` counts a site only
`if s['assignment'].current_attempt_number == 1`, so a change-request reopen **removes the
site from the designer's first-pass numerator** once it is re-released.
`m_change_request_rate()` divides by released sites *per requesting PM*.

## 4.5 A LOCKED procurement group — what the PM sees

**The PM dashboard offers no button.** A locked-group member is released by construction, and
`pm_change_request_targets()` skips released sites.

**The form (GET), reached by URL,** takes its `group_locked` branch in `change_request.html`:

> *"The BOQ for this site is **locked** — it is in a locked procurement group and its
> quantities have been committed to a purchase. A change now needs a variance against the
> order, which this system does not handle yet. Raise it with SCM directly."*

**The POST** is refused in `design_change_request()` with a flash message and a redirect to
the form (HTTP 302, not 403):

```python
    if membership is not None and membership.group.status == SITE_GROUP_LOCKED:
        return _back(f'{project.project_id}: the BOQ is locked — this site is in the '
                     f'locked procurement group "{membership.group.name}" and its '
                     f'quantities have been committed. A change now needs a variance '
                     f'against the order, which this system does not handle yet. Raise it '
                     f'with SCM directly.')
```

---

# PART 5 — WHAT AUTHORITY A PM HAS IN THE DESIGN MODULE TODAY

## 5.1 Design screens a PM can open

**One.** This assumes a PM who is `assigned_pm` of an OPEX site and holds no design flag.
Every gate below was read in its view.

| Screen | Result for such a PM |
|---|---|
| `design_change_request_form` | **200.** The only real screen |
| `design_my_sites` | 200 but **always empty**. It has no role gate; its rows are `filter(assigned_to=profile)` |
| `design_survey_download`, `design_file_download` | Pass `user_can_view_design`; **302** to a signed file URL, not a screen |
| Every other design URL — head sites, allocation, due dates, workspace, head review, Arka and QC verdicts, both queues, QC review, tender dashboard, QC dashboard, analytics, BOQ catalogue, change-request triage, site groups | **403**. Each gate names Head authority, the assigned designer, the QC flag or reviewer, or the SCM role |

**A PM approval screen would have to be built from nothing.** There is no PM-facing design
review surface. The PM cannot open `design_qc_review` or `design_head_review`:

```python
    if not (user_can_view_design_qc_dashboard(request.user)
            or user_is_assigned_qc_reviewer(request.user, project)):
        return HttpResponseForbidden(...)
```

The PM *can* reach the CAD and survey files, through `user_can_view_design()` →
`user_can_view_project()`.

## 5.2 Per site, via `assigned_pm`

`permissions.user_can_manage_project()` (quoted in P5) is the only PM authority path. PM is
absent from `PORTFOLIO_VIEW_ROLES`. `user_can_view_project()` has no PM branch of its own; a
PM sees a project only through `can_manage`. The authority is **per site, and it includes the
site's Project Coordinators.**

## 5.3 Shell and navigation entry

- **Shell.** All 14 templates in `projects/templates/projects/design/`, and the PM dashboard
  `dashboard/pm.html`, begin `{% extends "base.html" %}`, the Bootstrap shell.
  `projects/subadmin/subadmin_base.html` and `projects/admin/admin_base.html` are Tailwind
  shells for System Admin and Admin, and no PM screen uses them. A PM-facing design screen
  would therefore extend **`projects/templates/base.html`**.
- **R-11**, `docs/execution-model.md`: *"A screen ships with its navigation entry. Any prompt
  that adds a user-facing screen adds the way to reach it, in the same prompt."*
- **Where the entry goes.** `base.html`'s navbar has **no design entries and no PM block**.
  Its only PM-conditioned link is Programs:
  `{% if user.profile.role == 'Admin' or user.profile.role == 'PM' or user.profile.role == 'CEO' %}`.
  The established pattern for a PM reaching a per-site design action is a per-card button on
  **`projects/templates/dashboard/pm.html`**, driven by a flag computed in
  `views.dashboard_pm`. The Request-design-change button already works this way:
  `{% if dp.can_request_design_change %}` on the draft strip and
  `{% if row.can_request_design_change %}` on the progress card. Both strips are needed:
  `dashboard_pm`'s comment says every OPEX site reaches the PM through `draft_projects`,
  because `create_opex_site()` hardcodes `status='Draft'`.
  `ROLE_DASHBOARD['PM'] = '/dashboard/pm/'`, and `'Project Coordinator'` maps to the same
  dashboard.

---

# PART 6 — LEDGER AND NOTIFICATION

## 6.1 `DesignAssignment` IS in the `StatusTransition` registry — premise FALSE

`models.py`:

```python
SUBJECT_DESIGN_ASSIGNMENT = 'design_assignment'
...
    (SUBJECT_DESIGN_ASSIGNMENT, 'Design Assignment'),
]
```

`utils._subject_type_registry()`:

```python
            PaymentMilestone: SUBJECT_PAYMENT_MILESTONE,
            DesignAssignment: SUBJECT_DESIGN_ASSIGNMENT,
```

and `utils._SUBJECT_PROJECT_RESOLVERS`: `'DesignAssignment': lambda s: s.project`.

It was added by migration **`0079_design_assignment_subject_type`**, Session D. It is written
from `apply_design_status()` (quoted in 2.2). **Live:** 5 `design_assignment` ledger rows —
`arka_submitted` ×2, `awaiting_allocation`, `awaiting_head_arka`, `in_design`.

**What adding a subject costs,** as recorded for the one that was added:

- a `SUBJECT_*` constant and a `SUBJECT_TYPE_CHOICES` entry in `models.py`;
- an entry in both registry structures in `utils.py`;
- an `AlterField` migration on `StatusTransition.subject_type`, which is
  `CharField(max_length=30, choices=SUBJECT_TYPE_CHOICES, db_index=True)`. Per 0079's own
  docstring it is choices-only, *"no CHECK constraint"*, and a no-op in SQL;
- a row in `execution-model.md` §13, which `record_transition()`'s own error message demands.

**Can it record two transitions without lying about the other twelve?** Yes, as far as the
schema is concerned:

- `to_status` is `CharField(max_length=50)` and `from_status` is
  `CharField(max_length=50, blank=True, default='')`, both without choices.
- `record_transition()` validates only: the subject is registered; `to_status` is non-empty;
  a remark for `REMARK_REQUIRED_SUBJECT_TYPES`, which is `frozenset()`.

**But the question no longer arises.** `apply_design_status()` already records **every**
moving write of all fourteen statuses. A new status written through that function is recorded
automatically, at no cost. A new *subject*, such as a PM approval row, would cost the steps
listed above.

## 6.2 Notifications from the design module: none

A search of `projects/design_*.py` for `send_notification`, `Notification.objects` and
`notify` finds one comment, in `apply_design_status()`, and **no call**. The design module
states it:

- `apply_mirror_status()`: *"NO notification. ... Notifying on a derived state is a separate
  product decision."*
- `permissions.user_can_view_qc_queue()`: *"since nothing in this module notifies anyone"*.

The chokepoint is `notifications.send_notification(recipient, message, channels=None, ...)`.
It has no design caller.

**Would a site's PM receive one from any design event today? No.** This covers allocation,
Arka and package verdicts at both gates, release, Design Hold, and change-request raise,
accept and reject. A PM is notified only through non-design paths:

- `views._notify_boq_acknowledged`: `recipients = list(project_managers(boq.project))`.
  Unreachable for OPEX, because no OPEX BOQ reaches `Submitted` (3.3).
- `views._apply_task_status_change`'s payment-milestone branch. It is keyed on
  `is_payment_milestone`, which no mirror carries.

---

# PART 7 — FINDINGS

## 7.1 Contradictions

**With this prompt**

| Prompt says | The code says |
|---|---|
| P1 "`DESIGN_RELEASED` is terminal" | One exit (Part 0 P1) |
| P3 "NO attempt counted against the designer" | `designer_workload().rework`; `m_first_pass_rate()` (4.4) |
| P4 "requires ... DRAFT status"; "§B1" | A widening, not a requirement; §B1 is in `docs/EXECUTION_MODULE_DEFERRED.md`; a third gate exists |
| P5 "Only a PM"; "name the role gate" | Assigned PM or Project Coordinator; no role gate exists |
| 1.5 "The Designer User Manual documents THIRTEEN stages" | The manual is not in the repo |
| 2.2 "or is the mirror still unwritten by anything" | The mirror is written by `apply_design_status()` → `sync_design_mirror()` |
| 6.1 "Confirm `DesignAssignment` is absent from the `StatusTransition` subject registry" | Present since migration 0079 |

**With `docs/execution-model.md`**

- §5, OPEX template, says: *"The derivation hooks that will write mirror statuses do not
  exist ... Until then a mirror stays at its seeded status forever"*. The Design mirror is
  derived (2.2).
- R-21 says: *"its only task is a mirror, and no derivation hook exists to ever write it
  Done"*. The same code contradicts it.
- §13 lists under **"NOT instrumented"**: *"`DesignAssignment` | 14 ... | It is a session of
  its own."* The table in the same section also lists only six instrumented subjects. Both
  contradict 6.1.
- §5, the Design workflow row — *"`DESIGN_RELEASED` has exactly one exit ✔; a PM approval
  gate does not exist ✔"* — **agrees** with the code.

**With `docs/EXECUTION_PROMPT_LOG.md`**

- It says: *"What remains owed is the other half — the derivation hooks that will write
  mirror statuses"*.
- Row B21 says: *"Phase 1 holds only the `Design` mirror, which no hook can ever complete"*.
- Both contradict 2.2.

**With `docs/PHASE_0_COMPLETION.md`**

- "The design module's transitions" says: *"**Not instrumented, and this is the largest
  deliberate gap in the ledger.** `DesignAssignment` has 14 statuses ... Neither writes a
  `StatusTransition` row."* This contradicts 6.1.

**Also stale** (outside the four named documents):

- `docs/OPEX_task_template_spec.md` §2 rule 3: *"**NOT BUILT for any of the 8**, Design
  included."*
- `docs/EXECUTION_MODULE_DEFERRED.md` B27: *"none of their six mirrors can move"*.
- `docs/PHASE_2_PREFLIGHT_AUDIT.md` §6.1: *"There is no `released` guard"* on
  `design_mark_blocked`.
- The root `EXECUTION_MODULE_DEFERRED.md` §11 and `docs/EXECUTION_MODULE_DEFERRED.md` B2 name
  only the PM as the raiser; coordinators can raise too.

## 7.2 What makes a pre-release status harder than the ladder suggests

1. **Unknown statuses fail in opposite directions.**
   - `derive_design_mirror_state()` **raises**. The raise is unguarded inside the Head's
     transaction on the six activated sites, and silent elsewhere.
   - `design_metrics._classify()` **silently files it as `'in_design'`**.
   - `_DESIGNER_ACTIONS.get()` **silently offers nothing**.
   - Only the first has a test: `tests_design_mirror_derivation.test_01`.
2. **Three "finished" guards are equality tests against `DESIGN_RELEASED`**, not "at or after
   release": `design_mark_blocked`, `design_due_date_propose`, `design_due_date_change`, plus
   their template mirrors in `my_sites.html` and `head_sites.html`. Unamended, an S site can
   be put on Design Hold, and `_status_after_unblock()` then returns it to `in_design`. That
   recreates the §6.1 reopen route without an attempt or a change request.
3. **`is_overdue()` would charge the designer** for time an S site spends waiting on someone
   else.
4. **`TENDER_DESIGN_SUBMITTED_STATUSES`** would flip the CEO card's design pill backwards.
5. **`tender_dashboard.html` reads the released tile by position** (`m.stages|last`).
6. **Two change-request surfaces would disagree about an S site.** `pm_change_request_targets()`
   offers the button, and `design_change_request_form()` then shows the *"QC has not started"*
   text, which is false.
7. **`released_at` does double duty.** It is the release stamp, the SCM pool's age clock
   (`post_qc_pool` `.order_by('released_at')`, `_age_days`), and the endpoint of
   `m_cycle_time`, `m_on_time_delivery` and `m_stage_dwell`'s `'Head verdict → released'`.
   Which event stamps it changes all of them.
8. **`released_at` / `released_by` are not cleared on reopen** (P2). Any predicate written as
   "`released_at` is set" would treat a reopened site as released.
9. **`design_change_request_accept()` re-checks no status.** A request raised on a released
   draft-group site is accepted from whatever status the site is in at triage time.
10. **The only PM design screen is the change-request form.** A PM approval screen has no
    design surface to extend (5.1), and the PM receives no design notification of any kind
    (6.2).
11. **The prompt's own counts rest on a 14-value vocabulary with three values nothing
    commits** (1.5). An exhaustiveness rule, like the mirror map's, must still answer for all
    of them.
12. **`change_request.html` warns "QC is in progress" only for `'in_qc'`,** while
    `design_change_request()`'s `was_in_qc` also covers `awaiting_head_qc`. The screen and
    the view already disagree about which statuses count as "in review".

## 7.3 New deferred items

Appended to the root `EXECUTION_MODULE_DEFERRED.md` as §16–§21. Nothing already in that file
was edited.

---

# APPENDIX — local-dump counts (read-only)

```
DesignAssignment by status: arka_submitted 2, artifacts_uploaded 2, awaiting_allocation 82,
                            in_design 1, in_qc 1, released 5            (93 total)
released_at set but status != released: 0
status == released but released_at null: 0
DesignAttempt.opened_reason: initial 10
DesignChangeRequest: 0 rows
SiteGroup (type, status): procurement/draft 1, procurement/locked 1; memberships 6
OPEX BOQ (BOQ.status, design status): Draft/released 3, Draft/artifacts_uploaded 2,
                                      Draft/in_design 1, Draft/in_qc 1
Sites with a DesignAssignment that have any Task: 6; Design mirror Tasks on them: 6
StatusTransition subject_type='design_assignment': 5
OPEX projects with a non-PM assigned_pm / non-coordinator coordinator: none
```
