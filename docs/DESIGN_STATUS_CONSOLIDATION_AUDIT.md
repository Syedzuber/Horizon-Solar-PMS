# A-2.2 — Design status consolidation audit

**Audit-only.** Read-only session, run 5 Sep 2026 against working tree at `c88bf08`
(clean apart from untracked `.audit_*` scratch files). Nothing was built. The one file
written is this one.

**What this audit is for.** It is the step directly in front of the Design derivation
hook. Before a hook can write the OPEX Design mirror from `DesignAssignment.status`,
there has to be one place that writes `DesignAssignment.status` — today there are
nineteen on the product path, spread across sixteen functions, and three more off it.
This enumerates them by content, reconciles the result against A-2.1's "sixteen write
sites", and proposes (does not build) the shape of the chokepoint.

**Verification bar.** Every claim below carries the grep hit or the paste it came from.
Where this audit disagrees with A-2.1, it says so and shows both.

---

## 0 · Pre-flight

### 0.1 The design-hold reopen fix — **LANDED**

```
$ git log --oneline -30 | grep -i reopen
57ae66f Close design hold reopen route on released assignments
```

```
$ git show --stat 57ae66f
 projects/design_views.py                         | 35 +++++++++
 projects/templates/projects/design/my_sites.html | 20 ++++-
 projects/tests_design_part8.py                   | 97 +++++++++++++++++++++++-
 3 files changed, 148 insertions(+), 4 deletions(-)
```

Both halves shipped. The view guard, at
[design_views.py:1341](../projects/design_views.py#L1341):

```python
    if assignment.status == DESIGN_RELEASED:
        return _deny(request,
                     f'{project.project_id} has already been released and cannot be '
                     f'placed on Design Hold through this action.',
                     'design_my_sites')
```

and the template fix, `my_sites.html:117` and `:150`, both now
`{% if not row.is_blocked and not row.is_released %}`, fed by a new `is_released` key in
the `design_my_sites` row builder at
[design_views.py:401](../projects/design_views.py#L401).

**Consequence for this audit, which is the reason the prompt asked.** Write site **#6**
(`design_mark_blocked`) no longer admits a `released` assignment. A-2.1 §3.1 listed
`design_mark_blocked` as the **second** of two reopen routes out of `released` and called
it out in bold as unguarded. **That route is now closed.** `design_change_request_accept
→ _open_next_attempt()` is once again the only route out of `released`, exactly as
`docs/OPEX_TEMPLATE_AUDIT.md:97` and `:836` always claimed and A-2.1 corrected them for.
The consolidated function inherits a two-route reopen story, not three.

**One finding carried forward from that fix, unresolved and in this session's scope.**
The commit's own template comment says so:

> The dashboard partial asks one flag, `can_mark_blocked`, built in design_views.py and
> excluding four statuses. THIS SCREEN DOES NOT SHARE IT: its rows come from
> `design_my_sites`, which builds a different dict […] Left as two per-template tests
> deliberately — recorded as a finding for the consolidation session already scheduled
> ahead of the Design derivation hook.

That is this session. Verified independently — two builders, two different dicts:

```
$ grep -rn "can_mark_blocked" projects/
projects/design_views.py:3930:            'can_mark_blocked': is_designer and assignment.status not in (
projects/templates/projects/design/_dashboard_design_actions.html:34:    {% if p.design.can_mark_blocked %}
projects/templates/projects/design/_dashboard_design_actions.html:42:    {% if p.design.can_mark_blocked %}
projects/templates/projects/design/my_sites.html:110:      The dashboard partial asks one flag, `can_mark_blocked`, built in
```

`my_sites.html` reads `row.is_blocked` / `row.is_released` from the `design_my_sites`
builder ([design_views.py:392-401](../projects/design_views.py#L392-L401)); the dashboard
partial reads `can_mark_blocked` from a different builder at
[design_views.py:3930](../projects/design_views.py#L3930). Two screens, two answers to
one question. See §5.4 — this is *adjacent* to the status chokepoint, not part of it, and
is called out so the build prompt can decide whether to take it.

### 0.2 Session A2 (cleanup) and Session B's 0078 — **BOTH LANDED**

`0078` is on disk and committed:

```
$ ls projects/migrations/ | tail -4
0076_two_step_task_completion.py
0077_checklist_task_link_template_task.py
0078_boq_correction_record.py
__init__.py

$ git log --oneline -3 -- projects/migrations/
6df4ab1 Add boq correction record migration
83c8db2 A checklist belongs to a template task, not to a task's name
a15aea4 2.1: two-step task completion for OPEX tasks
```

The Session A/B commits that touched `design_views.py` since A-2.1's own commit
(`803f0c3`, which is where the audit doc entered the repo):

```
$ git log --oneline -5 -- projects/design_views.py
c88bf08 The screens stop describing a gate that is gone, and the BOQ gets the badge …
cacfe30 The reviewer who finds the wrong quantity can now fix it instead of failing …
420bd2a CAD travels with the Arka, and the BOQ was never derived from it at all
57ae66f Close design hold reopen route on released assignments
f2c1aa4 1.1b: the consumers learn to ask which kind of group they mean
```

`design_views.py` grew by 253 lines net across those.

**The load-bearing measurement.** Despite that rewrite, *not one status-write line
changed*:

```
$ git diff 803f0c3 HEAD -- projects/design_views.py | grep "^[+-].*assignment\.status\s*="
+            'is_released': assignment.status == DESIGN_RELEASED,
+    if assignment.status == DESIGN_RELEASED:
```

Both are **reads** (the `is_released` flag and the reopen guard). Every `+` and `-` in
that diff that touches `assignment.status` as an lvalue: none. Confirmed by count across
every commit since:

```
$ for c in 803f0c3 57ae66f 420bd2a cacfe30 c88bf08; do
    git show $c:projects/design_views.py | grep -c "^\s*assignment\.status\s*="; done
18
18
18
18
$ grep -c "^\s*assignment\.status\s*=" projects/design_views.py
18
```

**Eighteen, at every commit including A-2.1's own.** The rewrite moved line numbers
(`_open_next_attempt`'s write went 2576 → 2720) and changed nothing about what writes
this field. That matters for the reconciliation in §2: the gap between A-2.1's sixteen
and this audit's count is **not drift since Session A**. It was a miscount at the time.

---

## 1 · The enumeration

### 1.1 Method

Located by content, not by trusting A-2.1's citations. Four independent sweeps:

```
# attribute writes, any rvalue — literal, helper call or variable
$ grep -rn "^\s*assignment\.status\s*=\|\.status\s*=\s*DESIGN_\|\.status\s*=\s*opening_status\|\.status\s*=\s*_status_after_unblock" --include=*.py . | grep -v "/tests"

# queryset writes
$ grep -rn "DesignAssignment" --include=*.py . | grep -v "/tests" | grep -i "update(\|create("
$ grep -rn "update(.*status=" --include=*.py projects/ | grep -v "/tests"
$ grep -rn "bulk_update" --include=*.py projects/ | grep -v "/tests"

# every module that can reach the model at all
$ grep -rln "DesignAssignment" --include=*.py . | grep -v "/tests"
```

The last sweep returns fifteen files. Eleven of them only read: `design_analytics.py`,
`design_metrics.py`, `permissions.py`, `utils.py`, `views.py` (comparisons and one
subquery), `teardown_opex_test_data.py` (deletes), and migrations `0048` (schema), `0051`
(writes `Project.assigned_design`, verified — never `status`), `0075` (a comment about
mirror derivation), `0078` (a comment saying it touches nothing existing). Confirmed for
`views.py`, which is the one that would hurt to miss:

```
$ grep -n "DesignAssignment" projects/views.py
43:    DesignAssignment,
1714:    # DesignAssignment instead, exactly as Part 6's settled decision 9 requires, …
1762:# DesignAssignment statuses that mean the DESIGNER HAS HANDED THE ARTIFACT OVER — the
1980:    # Design, TENDER side. DesignAssignment is a OneToOne to Project, so there is at
1982:    tender_design_subq = DesignAssignment.objects.filter(
2107:        # DesignAssignment state machine. Reading the wrong one for a project type is
2973:        `DesignAssignment.assigned_to`, not `Project.assigned_design`; 87 DesignAssignment
4211:    # status of another object: the Design mirror follows its DesignAssignment, Material
```

One import, one subquery, six comments. **`views.py` never writes this field.** The
writes live in four places: `design_views.py`, two seed commands, and `admin.py`.

### 1.2 Product-path transition writes — eighteen, in sixteen functions

Raw hits, then the function each lands in (resolved by walking `^def` backwards from each
line — script output, not eyeballing):

```
  469  assignment.status = _status_after_unblock(assignment)   <- design_survey_upload() @ 413
  474  assignment.status = DESIGN_AWAITING_ALLOCATION          <- design_survey_upload() @ 413
  589  assignment.status = _status_after_unblock(assignment)   <- design_survey_link_set() @ 511
  594  assignment.status = DESIGN_AWAITING_ALLOCATION          <- design_survey_link_set() @ 511
  726  assignment.status = DESIGN_IN_DESIGN                    <- _allocate_one() @ 646
 1357  assignment.status = DESIGN_SURVEY_RETURNED              <- design_mark_blocked() @ 1302
 1628  assignment.status = DESIGN_ARTIFACTS_UPLOADED           <- _maybe_advance_to_artifacts_uploaded() @ 1600
 1963  assignment.status = DESIGN_ARKA_SUBMITTED               <- design_arka_submit() @ 1891
 2073  assignment.status = DESIGN_AWAITING_HEAD_ARKA           <- design_arka_approve() @ 2034
 2144  assignment.status = DESIGN_ARKA_REJECTED                <- design_arka_reject() @ 2092
 2201  assignment.status = DESIGN_ARKA_SUBMITTED               <- design_arka_head_approve() @ 2159
 2279  assignment.status = DESIGN_ARKA_REJECTED                <- design_arka_head_reject() @ 2223
 2720  assignment.status = opening_status                      <- _open_next_attempt() @ 2664
 2863  assignment.status = DESIGN_IN_QC                        <- design_qc_start() @ 2829
 2934  assignment.status = DESIGN_AWAITING_HEAD_QC             <- design_qc_pass() @ 2893
 3016  assignment.status = DESIGN_QC_FAILED                    <- design_qc_fail() @ 2949
 3083  assignment.status      = DESIGN_RELEASED                <- design_head_qc_pass() @ 3036
 3162  assignment.status = DESIGN_QC_FAILED                    <- design_head_qc_fail() @ 3096
```

Two functions write twice (`design_survey_upload`, `design_survey_link_set` — the
hold-clearing branch and the first-survey branch), so eighteen writes across sixteen
functions.

### 1.3 The creation write — one more, and it is a real transition

[design_views.py:233-239](../projects/design_views.py#L233-L239):

```python
def _get_or_create_assignment(project):
    """The DesignAssignment is created lazily by the first survey upload, so seeded or
    imported sites do not all carry an empty row from day one."""
    assignment = getattr(project, 'design_assignment', None)
    if assignment is None:
        assignment = DesignAssignment.objects.create(project=project)
    return assignment
```

No explicit `status=`, so every grep for the field misses it — but the model default
fires:

```python
    status = models.CharField(
        max_length=30, choices=DESIGN_ASSIGNMENT_STATUS_CHOICES,
        default=DESIGN_AWAITING_SURVEY,
    )
```
— [models.py:3418-3421](../projects/models.py#L3418-L3421)

This is `'' → awaiting_survey`, which is precisely the shape `StatusTransition` already
models (`from_status = models.CharField(…, blank=True, default='')  # blank = creation`,
[models.py:1800](../projects/models.py#L1800)) and precisely what §13 records for
`project_create` and `create_opex_site`. **Counting it, the product path has nineteen
writes.** Called from two places, both inside the transaction of a `@login_required`
view: [design_views.py:457](../projects/design_views.py#L457) and
[:578](../projects/design_views.py#L578).

### 1.4 Off the product path — three seed creates and one admin form

| # | Site | Value written | Kind |
|---|---|---|---|
| S1 | [seed_opex_test_data.py:516](../projects/management/commands/seed_opex_test_data.py#L516) | `status=DESIGN_IN_DESIGN` | management command, dev tooling |
| S2 | [seed_opex_test_data.py:532](../projects/management/commands/seed_opex_test_data.py#L532) | `status=DESIGN_RELEASED` | management command, dev tooling |
| S3 | [seed_scm_handoff_data.py:209](../projects/management/commands/seed_scm_handoff_data.py#L209) | `status=DESIGN_RELEASED` | management command, dev tooling |
| **A1** | **`DesignAssignmentAdmin`, [admin.py:410-419](../projects/admin.py#L410-L419)** | **any of the 14 statuses** | **Django admin change form** |

**A1 is new to this audit. A-2.1 does not mention it.** The registration in full:

```python
@admin.register(DesignAssignment)
class DesignAssignmentAdmin(admin.ModelAdmin):
    list_display  = ['project', 'status', 'assigned_to', 'current_attempt_number',
                     'released_at', 'updated_at']
    list_filter   = ['status']
    search_fields = ['project__project_id', 'project__customer_name',
                     'assigned_to__user__username']
    raw_id_fields = ['project']
    readonly_fields = ['current_attempt_number', 'survey_link_added_at',
                       'created_at', 'updated_at']
```

No `fields`, no `fieldsets`, no `exclude`, and `status` is not in `readonly_fields`. A
`ModelAdmin` with none of those renders **every editable field**, so `status` — and
`released_at`, and `released_by` — are editable on the change form. There is no
`has_change_permission` override on this class:

```
$ grep -n "has_change_permission" projects/admin.py
278:    def has_change_permission(self, request, obj=None):
300:    def has_change_permission(self, request, obj=None):
341:    def has_change_permission(self, request, obj=None):
499:    def has_change_permission(self, request, obj=None):
521:    def has_change_permission(self, request, obj=None):
```
— none of those five is inside `DesignAssignmentAdmin` (lines 410-419).

The section header eleven lines above it says otherwise
([admin.py:400-408](../projects/admin.py#L400-L408)):

> Audit-trail models (attempts, Arka versions, files, change requests) are append-only
> records of what happened, so their historical fields are read-only here — the admin
> must not be a side door that rewrites design history. **Nothing below performs a status
> transition.**

That last sentence is false of the very next class. The parenthetical names attempts,
Arka versions, files and change requests — `DesignAssignment` is not in the list, and it
is the one with the fourteen-value state machine on it. A superuser can move a site from
`awaiting_survey` to `released` in one form submit: no transition-table check, no
attempt opened or closed, no `released_at`, no `ActivityLog` row, nothing.

**This is B10 happening a second time, on a different model.** `docs/execution-model.md`
§13 records the precedent verbatim:

> **Corrected again 30 Aug 2026 by prompt B10: four was still short.** `Project.status`
> was written in **five** places. The fifth was `ProjectAdmin`'s change form, which 0.3
> never counted because it went looking for views and the admin is not one — so the
> "four" above was a true statement about `views.py` presented as a true statement about
> the product, which is the same mistake in miniature that this whole section exists to
> stop.

A-2.1 §3.3 makes the identical move: *"No signals on the model, no Celery, no scheduled
job, no management command on the product path"* — a true statement about
`design_views.py`, presented as a statement about the product. §13 already wrote down
what to do about it, and §5.3 below applies it.

### 1.5 Companion fields

Three, and they are a single triple written in one place:

```
$ grep -rn "survey_returned_at\|survey_returned_by\|survey_return_reason" --include=*.py . | grep -v "/tests" | grep "=" | grep -v "==\|is not None"
./projects/design_views.py:1354:        assignment.survey_returned_at    = timezone.now()
./projects/design_views.py:1355:        assignment.survey_returned_by    = profile
./projects/design_views.py:1356:        assignment.survey_return_reason  = reason
```

All three at write site **#6** only, immediately above the status write. **There is no
clearing path** — deliberate, and documented at both unblock sites in identical terms
([design_views.py:466-468](../projects/design_views.py#L466-L468) and
[:586-588](../projects/design_views.py#L586-L588)):

```python
            # Clearing the block. survey_returned_at / _by / _reason are deliberately
            # LEFT IN PLACE: together with survey_uploaded_at they are the record of how
            # long the clock was stopped, without adding a schema field this session.
```

Consumed downstream as an ever-marker, so the non-clearing is load-bearing:
`design_analytics.py:440` `'ever_held': a.survey_returned_at is not None`, with
`design_analytics.py:794` explaining *"`survey_returned_at` is a permanent marker"*, and
`design_analytics.py:65` / `:836` noting the row carries **one** such triple, overwritten
on each new hold — so hold *duration* is reconstructed from paired `ActivityLog` codes
(`design_blocked` / `design_survey_unblocked`), not from the row. §6 returns to this: it
is the sharpest argument for the ledger.

The release triple (`released_at`, `released_by`) is written at site **#17** only, and by
A1's form. `current_attempt_number` is written at site **#13** only. No other companion
field is coupled to a status write.

### 1.6 Full table — transition, side effects, `request` in scope

`request` re-verified directly per the prompt, not carried over from A-2.0 §B23 or
A-2.1 §3.2. Decorators read off the two lines above each `^def`. "Attempt" column: what
the site does to `DesignAttempt`. Every product site calls `log_activity`; **no product
site calls `record_transition`**, and `grep -n "send_notification\|notify"
projects/design_views.py` returns **nothing** — the design module sends no notifications
at all, so that column is uniformly empty and is omitted.

| # | Line | Function | From → To | Attempt | ActivityLog `action_code` | Save form | `request`? |
|---|---|---|---|---|---|---|---|
| 1 | 469 | `design_survey_upload` | `survey_returned` → *derived* (`awaiting_allocation` \| `in_design` \| `allocated`) | — | `design_survey_unblocked` | bare `.save()` | **Yes**, `@login_required` @412 |
| 2 | 474 | `design_survey_upload` | `awaiting_survey` → `awaiting_allocation` | — | `design_survey_uploaded` | bare `.save()` | **Yes**, same |
| 3 | 589 | `design_survey_link_set` | `survey_returned` → *derived* | — | `design_survey_unblocked` | bare `.save()` | **Yes**, `@login_required` @510 |
| 4 | 594 | `design_survey_link_set` | `awaiting_survey` → `awaiting_allocation` | — | `design_survey_link_added` | bare `.save()` | **Yes**, same |
| 5 | 726 | `_allocate_one` | *any of* `REALLOCATABLE_STATUSES` → `in_design` | — | `design_allocated` \| `design_reallocated` | bare `.save()` | **No** — helper, takes `actor`. Both callers are views (§1.7) |
| 6 | 1357 | `design_mark_blocked` | *any but* `survey_returned`, `released` → `survey_returned` | — | `design_blocked` | bare `.save()` | **Yes**, `@login_required` @1301 |
| 7 | 1628 | `_maybe_advance_to_artifacts_uploaded` | `arka_submitted` → `artifacts_uploaded` | reads `attempt` | `design_artifacts_uploaded` | `update_fields` | **No** — helper, takes `actor`. Four callers, all views (§1.7) |
| 8 | 1963 | `design_arka_submit` | *in* `ARKA_SUBMITTABLE_STATUSES` → `arka_submitted` | may `_open_first_attempt` | `design_arka_submitted` | `update_fields` | **Yes**, `@login_required` @1890 |
| 9 | 2073 | `design_arka_approve` | `arka_submitted` → `awaiting_head_arka` | — | `design_arka_qc_approved` | `update_fields` | **Yes**, `@login_required` @2033 |
| 10 | 2144 | `design_arka_reject` | `arka_submitted` → `arka_rejected` | — | `design_arka_qc_rejected` | `update_fields` | **Yes**, `@login_required` @2091 |
| 11 | 2201 | `design_arka_head_approve` | `awaiting_head_arka` → `arka_submitted` | — | `design_arka_head_approved`, **then calls #7** | `update_fields` | **Yes**, `@login_required` @2158 |
| 12 | 2279 | `design_arka_head_reject` | `awaiting_head_arka` → `arka_rejected` | — | `design_arka_head_rejected` | `update_fields` | **Yes**, `@login_required` @2222 |
| 13 | 2720 | `_open_next_attempt` | *any* → `in_design` \| `arka_submitted` (follows carried Arka) | **closes N, opens N+1**; may call #7 | `design_attempt_opened_{reason}` | `update_fields` (+ `current_attempt_number`) | **No** — helper, takes `actor`. Three callers, all views (§1.7) |
| 14 | 2863 | `design_qc_start` | `artifacts_uploaded` → `in_qc` | stamps `qc_started_at` | `design_qc_started` | `update_fields` | **Yes**, `@login_required` @2828 |
| 15 | 2934 | `design_qc_pass` | `in_qc` → `awaiting_head_qc` | verdict + `head_started_at`; **not** closed | `design_qc_passed` | `update_fields` | **Yes**, `@login_required` @2892 |
| 16 | 3016 | `design_qc_fail` | `in_qc` → `qc_failed` *(transient)* | verdict, `redo_required`, **then #13** | `design_qc_failed` | `update_fields` | **Yes**, `@login_required` @2948 |
| 17 | 3083 | `design_head_qc_pass` | `awaiting_head_qc` → `released` | verdict + **`closed_at`** | `design_head_qc_passed` | `update_fields` (+ `released_at`, `released_by`) | **Yes**, `@login_required` @3035 |
| 18 | 3162 | `design_head_qc_fail` | `awaiting_head_qc` → `qc_failed` *(transient)* | verdict, overturn, `redo`, **then #13** | `design_head_qc_failed` | `update_fields` | **Yes**, `@login_required` @3095 |
| 19 | 238 | `_get_or_create_assignment` | `''` → `awaiting_survey` (model default) | — | **none** | `.objects.create()` | **No** — helper; both callers are views #1-#4 |
| S1-S3 | — | two seed commands | `''` → `in_design` \| `released` | creates attempts | none | `.objects.create()` | **No** — management command |
| A1 | — | `DesignAssignmentAdmin` form | **any → any** | **none** | **none** | admin `.save()` | Admin `request` (§5.3) |

Three notes the table compresses:

* **#16 and #18 write `qc_failed` and then immediately overwrite it.** `qc_failed` is
  never a resting state — the very next statement inside the same `atomic()` is
  `_open_next_attempt()`, which writes again. The comment at
  [design_views.py:3013-3015](../projects/design_views.py#L3013-L3015) is explicit:
  *"Status passes THROUGH qc_failed on its way to the new attempt. Recorded as its own
  log line so the failure is visible in the trail even though the stored status moves
  straight on."* **Two status writes per QC failure, one of which no query can ever
  observe.** A chokepoint that fires per write records both, which is right — but it is a
  thing the build must decide about deliberately rather than discover.
* **#1 and #3 have a computed to-status.** `_status_after_unblock()`
  ([design_views.py:242-263](../projects/design_views.py#L242-L263)) returns one of three
  values from `assigned_to_id` and `_effective_commitment()`. Any signature that takes a
  literal to-status has to accept the computed value, which it does naturally — but a
  design that tried to enumerate transitions as a table would need three edges here.
* **#11's to-status is a *backwards* move by status-name and forwards by meaning.**
  `awaiting_head_arka → arka_submitted`, explained at
  [design_views.py:2167-2169](../projects/design_views.py#L2167-L2169): the status carries
  `head_verdict='approved'` and therefore reads as "Arka approved, artifacts incomplete".
  **A transition table keyed on status alone cannot express this** — it looks like a
  regression and is not. Called out because §5's proposal deliberately does *not* include
  a `VALID_TRANSITIONS` table, and this is the reason.

### 1.7 Classification, and the `request` question for the three helpers

| Class | Sites |
|---|---|
| **Head action** | #1, #2, #3, #4 (survey upload / link), #5 via `design_allocate` and `design_bulk_allocate`, #11, #12 (Head Arka gate), #17, #18 (Head QC gate), #13 via `design_change_request_accept` |
| **Designer action** | #6 (Design Hold), #8 (Arka submit), #7 via `design_artifact_upload` and `design_boq_complete`, #19 (indirect — a Head upload creates the row) |
| **QC action** | #9, #10 (gate-1 Arka), #14, #15, #16 (gate-1 package) |
| **System / migration** | S1, S2, S3 (seeds). **No migration writes this field** — verified, `0051` writes `Project.assigned_design` only |
| **Other** | **A1** — Django admin, unclassifiable as any role's action, which is the finding |

Three sites are helpers with no `request`. Every one of their callers is a
`@login_required` view — this is the direct re-verification the prompt asked for, not a
carry-over:

```
$ grep -n "_allocate_one" projects/design_views.py
791:            due = _allocate_one(assignment, designer, request.user.profile)   <- design_allocate() @ 769
844:                due = _allocate_one(assignment, designer, actor, …)           <- design_bulk_allocate() @ 803

$ grep -n "_maybe_advance_to_artifacts_uploaded" projects/design_views.py   # call sites only
2210:        _maybe_advance_to_artifacts_uploaded(assignment, …, profile)      <- design_arka_head_approve() @ 2159
2393:        advanced = _maybe_advance_to_artifacts_uploaded(…, profile)       <- design_artifact_upload() @ 2299
2540:        advanced = _maybe_advance_to_artifacts_uploaded(…, profile)       <- design_boq_complete() @ 2432
2734:        _maybe_advance_to_artifacts_uploaded(assignment, new_attempt, actor) <- _open_next_attempt() @ 2664

$ grep -n "_open_next_attempt" projects/design_views.py   # call sites only
3025:        new_attempt = _open_next_attempt(…)   <- design_qc_fail() @ 2949
3172:        new_attempt = _open_next_attempt(…)   <- design_head_qc_fail() @ 3096
3437:        new_attempt = _open_next_attempt(…)   <- design_change_request_accept() @ 3395
```

```
$ for n in design_allocate design_bulk_allocate design_artifact_upload design_boq_complete; do
    grep -n -B2 "^def $n(" projects/design_views.py; done
767-
768-@login_required
769:def design_allocate(request, project_id):
801-
802-@login_required
803:def design_bulk_allocate(request, pk):
2297-
2298-@login_required
2299:def design_artifact_upload(request, project_id):
2430-
2431-@login_required
2432:def design_boq_complete(request, project_id):
```

`design_change_request_accept` is `@login_required` at
[design_views.py:3394](../projects/design_views.py#L3394); the other two callers of #13
are #16 and #18, already in the table.

**Verdict on §B23 for the Design hook, re-derived rather than assumed: A-2.1's answer
still holds on the product path, and is now known to be incomplete off it.**

* All nineteen product writes execute inside a `@login_required` view or a helper called
  only from one, so a derivation hook placed at any of them holds a live `request`. That
  much survives the rewrite intact — verified above, decorator by decorator.
* **A1 breaks the "every writer is a view" phrasing, and does not break the hook.** An
  admin `save_model()` also has a `request`. So the *conclusion* — a mirror hook can
  always reach a `request` — is unaffected; the *premise as A-2.1 stated it* was wrong.
  Worth the distinction because §5.3's recommendation is to close A1, not instrument it,
  and that recommendation does not rest on the hook being blocked.
* A-2.1's supporting evidence is unchanged and re-checked: `_open_next_attempt()` takes
  `actor` (a `UserProfile`), not `request`, and calls `log_activity(assignment.project,
  actor, …)` — the shared reopen function is already HTTP-free. Still true at
  [design_views.py:2664](../projects/design_views.py#L2664).

---

## 2 · Reconciliation against A-2.1's "sixteen"

A-2.1's list, verbatim from `docs/PHASE_2_PREFLIGHT_AUDIT.md:674-706` — fifteen literal
hits plus `opening_status`, called sixteen at `:758`:

> **Every product write to `DesignAssignment.status` — all sixteen, in both directions,
> including all four paths into and out of `released` — happens inside an
> `@login_required` Django view with a live `request` in scope.**

Reconciled site by site:

| A-2.1 line | This audit | Status |
|---|---|---|
| 462 `AWAITING_ALLOCATION` in `design_survey_upload` | #2 @474 | **Unchanged**, moved +12 |
| 582 `AWAITING_ALLOCATION` in `design_survey_link_set` | #4 @594 | **Unchanged**, moved +12 |
| 714 `IN_DESIGN` in `_allocate_one` | #5 @726 | **Unchanged**, moved +12 |
| 1319 `SURVEY_RETURNED` in `design_mark_blocked` | #6 @1357 | **Changed shape** — the write is identical; the function now refuses `released` above it (§0.1) |
| 1572 `ARTIFACTS_UPLOADED` | #7 @1628 | **Unchanged**, moved +56 |
| 1859 `ARKA_SUBMITTED` | #8 @1963 | **Unchanged**, moved +104 |
| 1967 `AWAITING_HEAD_ARKA` | #9 @2073 | **Unchanged**, moved +106 |
| 2034 `ARKA_REJECTED` | #10 @2144 | **Unchanged**, moved +110 |
| 2087 `ARKA_SUBMITTED` | #11 @2201 | **Unchanged**, moved +114 |
| 2161 `ARKA_REJECTED` | #12 @2279 | **Unchanged**, moved +118 |
| 2576 `opening_status` in `_open_next_attempt` | #13 @2720 | **Unchanged**, moved +144 |
| 2719 `IN_QC` | #14 @2863 | **Unchanged**, moved +144 |
| 2790 `AWAITING_HEAD_QC` | #15 @2934 | **Unchanged**, moved +144 |
| 2872 `QC_FAILED` in `design_qc_fail` | #16 @3016 | **Unchanged**, moved +144 |
| 2939 `RELEASED` in `design_head_qc_pass` | #17 @3083 | **Unchanged**, moved +144 |
| 3018 `QC_FAILED` in `design_head_qc_fail` | #18 @3162 | **Unchanged**, moved +144 |
| — | **#1 @469** `_status_after_unblock` in `design_survey_upload` | **MISSED BY A-2.1** |
| — | **#3 @589** `_status_after_unblock` in `design_survey_link_set` | **MISSED BY A-2.1** |
| — | **#19 @238** creation default in `_get_or_create_assignment` | **MISSED BY A-2.1** |
| seeds (noted, uncounted) | S1, S2, S3 | **Unchanged** — A-2.1 lists these at `:637-638`, correctly excluded from "product" |
| — | **A1** `DesignAssignmentAdmin` | **MISSED BY A-2.1** |

**Nothing is new since Session A or B. Nothing has ceased to exist.** One site changed
shape (#6 gained the `released` guard). The four additions are all miscounts at the time,
proved by §0.2's per-commit count: eighteen attribute writes at `803f0c3`, A-2.1's own
commit.

**How A-2.1 missed #1 and #3, and why it is the interesting kind of miss.** Its own text
names the mechanism one paragraph later:

> plus one the pattern misses because the value is a variable — design_views.py:2576,
> inside `_open_next_attempt()`

The grep was for `DESIGN_` literals. It caught the variable case once, by hand, and
stopped. `= _status_after_unblock(assignment)` is a third rvalue form and slipped through
the same hole. **A-2.1 knew about the site**: §3.2 row 4 reads *"return from Design Hold |
`design_survey_upload` | […] write at [:457] via `_status_after_unblock()`"* — citing
the exact line it then left out of the count. The enumeration and the calling-context
table disagreed with each other and nothing reconciled them. Confirmed at the source
commit:

```
$ git show 803f0c3:projects/design_views.py | grep -n "^\s*assignment\.status\s*="
457:            assignment.status = _status_after_unblock(assignment)
462:            assignment.status = DESIGN_AWAITING_ALLOCATION
577:            assignment.status = _status_after_unblock(assignment)
582:            assignment.status = DESIGN_AWAITING_ALLOCATION
714:    assignment.status = DESIGN_IN_DESIGN
…
```

Line 457 is right there. This audit's sweep uses three rvalue patterns plus a
create/update sweep plus a whole-repo module sweep for exactly this reason.

**Count, stated plainly:**

| Reading | Count |
|---|---|
| A-2.1's "product writes" | 16 |
| Product **transition** writes (this audit) | **18** |
| Product writes incl. creation default | **19** |
| Every write anywhere (incl. 3 seeds + admin form) | **23** |

**Stop condition: NOT triggered.** Eighteen against sixteen, in sixteen functions, with
no new site since A-2.1 and no structural change to any of them. That is a corrected
count of the same surface, not a larger surface. §7 gives the build verdict.

---

## 3 · Bare writes versus helper-mediated writes

The prompt asks which sites write with no validation function at all versus which go
through some existing helper. The honest answer is that **the second category is empty on
the product path.**

There is no `record_transition()` call anywhere in `design_views.py`, no
`send_notification()`, and no design-specific status helper. What exists is:

* `log_activity()` at all eighteen product transition sites — but that is a *feed writer*,
  not a validator, and it **swallows every exception**
  ([models.py:1673-1686](../projects/models.py#L1673-L1686)):
  ```python
      try:
          from projects.models import ActivityLog
          ActivityLog.objects.create(…)
      except Exception as e:
          import logging
          logging.getLogger(__name__).error(f"ActivityLog failed: {e}")
  ```
* per-view guards — `_verdict_target()`, `_head_verdict_target()`, `_qc_guard()`,
  `_blocking_change_request()`, `_other_gate_actor_conflict()`, `REALLOCATABLE_STATUSES`,
  `ARKA_SUBMITTABLE_STATUSES`. These are real and good, and **every one is a caller's
  own** — none is reached from the write itself. Delete a guard and the write still
  happens.

So the useful split is by *save form*, which is where a second, independent defect lives:

**Bare `.save()` — writes the whole row:** #1, #2, #3, #4 (line 480 / 601), #5 (line
727), #6 (line 1358). Six of eighteen.

**`.save(update_fields=['status', 'updated_at'])`:** the other twelve.

That difference is not cosmetic. The module already documents why, at
[design_views.py:967-970](../projects/design_views.py#L967-L970), on the one queryset
write it has:

```python
        # filter().update() rather than save(): two Heads assigning the same site at once
        # would otherwise each write a whole row from their own stale copy, and the loser's
        # view of every other field would silently win.
```

The six bare saves are exactly the hazard that comment describes, on the same model.
#5 (`_allocate_one`) is the sharpest: it is reachable from `design_bulk_allocate`, writes
`assigned_to` / `assigned_by` / `assigned_at` / `status` from a possibly stale in-memory
row, and a concurrent Design Hold on the same site would be clobbered wholesale.

**Not a defect this audit asks anyone to fix here** — it is named because a consolidated
writer makes it disappear for free: one function, one save form, one decision, applied at
all nineteen sites at once. That is a genuine dividend of the consolidation beyond the
hook it exists to enable, and §5.1 puts it in the proposal.

**Fully unvalidated, no helper of any kind, no log:** **A1** (admin) and **S1-S3**
(seeds). The seeds are marked dev tooling and are fine. A1 is not.

---

## 4 · What the mirror hook actually needs from this

Recorded so §5 is judged against the real requirement rather than a guess. The task-side
rung 0 that the hook must not disturb, [views.py:4207-4235](../projects/views.py#L4207-L4235):

```python
    # A MIRROR IS READ-ONLY TO EVERY HUMAN. This is rung 0 of the refusal ladder and
    # belongs nowhere else (R-18, R-20; OPEX spec §2.2). Prompt B22.
    #
    # A mirror (Task.is_mirror) does not hold a status somebody types — it REPORTS the
    # status of another object: the Design mirror follows its DesignAssignment, …
    #
    # NOT THE WHOLE FEATURE. The derivation hooks that will WRITE these statuses are
    # unbuilt. They belong to the source objects (phases 3-5), go through
```

and the sentence A-2.1 quoted from the same block: *"they will not call this function,
which exists to say no to people."*

So the hook's contract is fixed and narrow: **write the mirror `Task` from the
`DesignAssignment` side, via `record_transition()`, carrying the source event's actor,
without entering `_apply_task_status_change()` at all.** Nineteen call sites each doing
that by hand is the thing consolidation exists to prevent. One writer that already knows
`(assignment, from_status, to_status, actor)` has every argument the hook needs and
nothing else has to change.

---

## 5 · Proposed shape — NOT BUILT

### 5.1 The function

```python
def apply_design_status(assignment, new_status, actor, detail, action_code,
                        extra_fields=None, entity_type='DesignAssignment',
                        entity_id=None):
    """THE ONE PLACE DesignAssignment.status IS WRITTEN. Caller owns the atomic block."""
```

Parameters, each with the site that forces it:

| Param | Why it must exist |
|---|---|
| `assignment` | the subject; also the `from_status` source, read *before* the write |
| `new_status` | a value, not a table key — #1/#3 pass a computed result and #11 moves "backwards" by name (§1.6) |
| `actor` | `UserProfile`, never `request`. #5, #7, #13 already have only this, and #13 already logs with it |
| `detail` | the `log_activity` sentence. Every site's is different and several interpolate attempt numbers and reasons |
| `action_code` | eighteen distinct codes today, and `design_analytics` pairs `design_blocked` with `design_survey_unblocked` to reconstruct hold duration — these are load-bearing and cannot be derived from `new_status` |
| `extra_fields` | the companion writes that must land in the *same* save: `released_at`/`released_by` (#17), `survey_returned_*` (#6), `current_attempt_number` (#13) |
| `entity_type` / `entity_id` | sites log against `DesignAssignment`, `DesignAttempt`, `ArkaSubmission` and `DesignChangeRequest` — the existing codes and entity refs must survive verbatim or `design_analytics` breaks |

Body, in order: read `from_status`; no-op return if unchanged; write `status` **plus**
`extra_fields` in a single `save(update_fields=…)` — which retires all six bare saves of
§3 at a stroke; `log_activity(...)`; `record_transition(...)` **if** §6 is decided yes;
return `from_status` so callers can message on it.

Deliberately **not** in it: a `VALID_TRANSITIONS` table (§1.6's #11 and the transient
`qc_failed` of #16/#18 make status-keyed edges the wrong abstraction — the guards are
already per-gate and correctly so); permission checks (they need `request` and belong to
callers, matching `_apply_task_status_change`'s own docstring convention); the atomic
block (matching `record_transition()`'s documented contract that callers own it, and
`_open_next_attempt()`'s existing one).

### 5.2 Where `DesignAttempt` open/close sits

**Stays the caller's concern. Do not fold `_open_next_attempt()` into it.**

Three reasons from the code, not from taste:

1. `_open_next_attempt()` is already a chokepoint of its own, and says so at
   [design_views.py:2665](../projects/design_views.py#L2665): *"Close the current attempt
   and open the next one. THE ONLY PLACE THIS HAPPENS."* It has three callers and one
   documented rule about what it must not touch. Folding two chokepoints into one gives
   the merged function two reasons to change.
2. **The coupling is not one-to-one in either direction.** #13 writes status *inside*
   `_open_next_attempt()`; #16 and #18 write status *and then* call it — two status
   writes per QC failure (§1.6). #17 closes an attempt without opening one. #15 writes
   status and deliberately closes nothing. #7 and #14 touch attempt fields with no
   attempt lifecycle event at all.
3. The natural composition already works: `_open_next_attempt()` becomes **caller #19**
   of `apply_design_status()`, its own `assignment.save(update_fields=[…])` at
   [:2721](../projects/design_views.py#L2721) replaced by a call passing
   `extra_fields={'current_attempt_number': next_number}`. Nesting, not merging.

### 5.3 Where the mirror-refusal rung sits, and what to do about A1

**The mirror hook goes at the end of `apply_design_status()`, after the log and the
ledger write, and it touches nothing on the `Task` side.** It has `assignment` (hence
`project`), `from_status`, `new_status` and `actor` — everything §4 requires. Rung 0 in
`_apply_task_status_change()` is untouched: the hook writes the mirror `Task` directly
through `record_transition()`, exactly as the B22 comment prescribes, and never enters
the human-refusal ladder. **`views.py` needs no edit for this.** That is the whole point
of doing the consolidation first.

**A1 should be closed, not instrumented.** Add `'status'`, `'released_at'`,
`'released_by'` to `DesignAssignmentAdmin.readonly_fields`, and fix the section comment
at [admin.py:407](../projects/admin.py#L407) which currently asserts something false.
This is B10's precedent applied unchanged (§13: *"The fifth site is now closed rather
than instrumented […] The admin is not an activation route"*), and the design-specific
version of its second argument is stronger: `design_head_qc_pass` is the only path that
stamps `released_at`/`released_by` **and** closes the attempt, so an admin form that set
`status='released'` correctly would still produce a released site with an open attempt
and a downstream `ProcurementBatch` query that disagrees with the screens. **The admin is
not a release route.** Cost: three strings and one comment. If it is closed, the mirror
hook needs no admin-side branch at all.

### 5.4 Scope boundary, stated so the build prompt can hold it

**In:** the nineteen product writes, the six bare saves, A1's three `readonly_fields`.

**Out, and named rather than silently dropped:** the `can_mark_blocked` /
`is_blocked`+`is_released` duplication from §0.1. It is a *read*-side duplication in two
row-builders, does not touch `status` writing, and folding it in trades a bounded
mechanical change for an unbounded one across two screens. It should be its own small
prompt, and this document is where it stays recorded until then.

---

## 6 · OPEN DECISION — register `DesignAssignment` in `StatusTransition`? **Zuber's call.**

Not decided here. Both costs stated, and the two facts that make the question live.

**Where it stands today.** `DesignAssignment` is in §13's *NOT instrumented* table, with
this reason:

> The richest workflow in the product. Instrumenting it means editing `design_views.py`,
> which has been correctly scoped and untouched all programme. **It is a session of its
> own.**

That reason is *now spent*: the consolidation session edits `design_views.py` anyway, and
the six-value registry is real and working ([utils.py:428-435](../projects/utils.py#L428-L435)).

**The fact that makes it more than tidiness.** Design history lives only in
`ActivityLog`, and `log_activity()` swallows every exception (§3). It also has **no
`from_status`, no `to_status`, no `actor_role_code` and no `reason_code`** — the model's
own comment says so at [models.py:1692-1700](../projects/models.py#L1692-L1700): *"It has
no from-status, no to-status, no reason and no actor role, so it cannot answer 'how long
did this sit in Blocked' or 'who moved it, and why'."* Design already needs exactly that
answer and gets it the hard way: because the row carries only **one** `survey_returned_*`
triple, overwritten on each hold, `design_analytics` reconstructs hold duration by
pairing `design_blocked` against `design_survey_unblocked` `ActivityLog` codes
([design_analytics.py:65](../projects/design_analytics.py#L65),
[:836](../projects/design_analytics.py#L836)). **A metric the business reads is built on
string-matched rows from a writer that is allowed to fail silently.** A ledger row would
make that a direct query and make the swallow harmless.

**Cost of YES.** §13 states the recipe: *"three coordinated edits and one migration"* —
`SUBJECT_DESIGN_ASSIGNMENT` + `SUBJECT_TYPE_CHOICES` in `models.py`; the class entry in
`_subject_type_registry()` and a `'DesignAssignment': lambda s: s.project` line in
`_SUBJECT_PROJECT_RESOLVERS`; the call site — **one**, inside `apply_design_status()`,
which is precisely why doing it during consolidation is cheaper than doing it after; and
moving the row between §13's two tables, which `record_transition()`'s own `ValueError`
message demands. The migration is a choices-only `AlterField` (no DB change). Field
widths already fit: `to_status` is `max_length=50`, design statuses top out at 19 chars.
One real design decision falls out: `reason_code` — the `REASON_*` vocabulary is
task-shaped and design would want its own values, or none. And `record_transition()`
**raises where `log_activity()` swallows**, by explicit design
([utils.py:452-459](../projects/utils.py#L452-L459)) — so a ledger failure would abort a
designer's Arka submission where today it loses a feed line. That is the intended
behaviour and it is still a behaviour change to accept knowingly.

**Cost of NO.** Zero work now. Design keeps no non-lossy transition history; hold-duration
analytics keeps its string-matched, silently-failing foundation; §13's *NOT instrumented*
row keeps a reason that is no longer true, and the next audit re-asks this question with
the consolidation already spent — at which point the "one call site" saving is gone and
it costs a second pass over the same file. `DesignAttempt` stays out either way (§13
notes it carries ad-hoc stamps and needs its own decision about whether those become
transitions), so a YES here does not commit to it.

**Not decided. Flagged.**

---

## 7 · Verdict — is a single-session consolidation build realistic?

**Yes, with A1 in scope and §5.4's boundary held. Do not split it.**

The measurements that support that, all from above:

* **Nineteen writes in sixteen functions in one file**, plus three
  `readonly_fields` strings in a second. Not twenty-three files.
* **Zero new sites since A-2.1**, proved by the per-commit count in §0.2 — Sessions A and
  B rewrote 253 net lines around these writes and changed none of them. The surface is
  stable.
* **Every site already does the same three things** — set status, save, `log_activity`
  with an `action_code` — so each conversion is mechanical: delete two lines, call one
  function, pass the code through.
* **The two genuinely non-mechanical sites are known in advance**: #13
  (`_open_next_attempt`, which nests rather than merges, §5.2) and #16/#18 (the transient
  `qc_failed` double-write, §1.6). Two, not a discovery process.
* **`request` is a non-issue** — re-verified decorator by decorator (§1.7), and the
  function takes `actor` anyway, which three sites already have and the rest can supply
  from `request.user.profile` as they do today.

The two things that would make it unrealistic, and neither holds: a count substantially
above sixteen (it is eighteen, or nineteen counting creation — the stop condition is not
met), and writes scattered across modules (they are not; `views.py` never writes this
field).

**Split only if §6 is decided YES *and* the ledger's raise-don't-swallow semantics are
judged to need their own test pass.** Then: session 1 lands `apply_design_status()` and
converts all nineteen; session 2 adds the subject type at the single call site session 1
created. That ordering keeps the cheap version of §6 available rather than spending it.
As a single session with §6 answered NO, the work is bounded and the shape is already
known.

**The derivation hook is not in this scope** and should not be. It goes in after, at the
one line §5.3 identifies, and needs no `Task`-side edit when it does.
