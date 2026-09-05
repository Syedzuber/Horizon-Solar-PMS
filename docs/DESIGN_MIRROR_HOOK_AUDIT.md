# A-2.3 — OPEX Design mirror derivation: pre-hook design audit

**Read-only session.** Nothing in `projects/` was edited. This document is the only
file written. Every fact below was read out of the working tree at commit `fc24728`
or measured against the local development database; every proposal is labelled as
one and none of them has been built.

Companion to [`DESIGN_STATUS_CONSOLIDATION_AUDIT.md`](DESIGN_STATUS_CONSOLIDATION_AUDIT.md)
(A-2.2), which asked *"what has to be true before a mirror hook can exist"* and whose
§5.1 proposal shipped as Sessions C and D. This one asks *"the chokepoint exists —
what exactly does the hook write, and to which row"*.

---

## 0 · Summary of what was confirmed

| | Question | Answer |
|---|---|---|
| **T1** | Is rung 0 an unconditional refusal, or is it caller-sensitive? | **Unconditional.** No parameter, flag or context of any kind can pass it. Adding a bypass would weaken a guarantee this codebase has made absolute on purpose. |
| **T2** | How is *the Design mirror* found, as distinct from the other seven? | **`template_task.code == 'DESIGN'`, scoped to the project.** The field exists and is populated on every template-built task. `task_name` matching also resolves uniquely today, but it is the pre-2.4 pattern this codebase has already migrated away from once. **No such lookup exists anywhere yet — read-only or otherwise. It is new.** |
| **T3** | Is a narrow dedicated writer the right shape? | **Yes — confirmed workable, and it is what A-2.2 §5.3 already proposed.** But it is **not sufficient on its own**: see §3.4, the sequencing gap. |
| **T4** | Is `apply_design_status()` structurally unreachable for Residential? | **Yes at runtime, by convention rather than by constraint.** No explicit guard is needed in the hook; the lookup is the guard, for a better reason than the prompt assumed. §4.3. |
| **T5** | The status mapping | **Proposed in §5. Needs sign-off.** Two statuses are genuinely ambiguous and are flagged rather than defaulted. |

**Verdict on scope:** see §7. Short answer — the *hook* is one session. The *feature*
is not, unless the sequencing gap in §3.4 is explicitly ruled out of scope, in which
case say so in the build prompt rather than discovering it afterwards.

---

## 1 · TASK 1 — rung 0, re-read at current state

### 1.1 The refusal, pasted in full

`projects/views.py:4207-4247`, the opening statements of `_apply_task_status_change()`,
verbatim as of `fc24728`:

```python
    # A MIRROR IS READ-ONLY TO EVERY HUMAN. This is rung 0 of the refusal ladder and
    # belongs nowhere else (R-18, R-20; OPEX spec §2.2). Prompt B22.
    #
    # A mirror (Task.is_mirror) does not hold a status somebody types — it REPORTS the
    # status of another object: the Design mirror follows its DesignAssignment, Material
    # Delivery follows accepted delivery quantities, COD and HOTO follow the commissioning
    # and handover records. A mirror a human can move can disagree with its source, and
    # then neither number means anything. Five OPEX tasks carry the flag today (Design,
    # Material Delivery, COD, As-Built Drawings, HOTO); the Residential template has none.
    #
    # WHY ABOVE THE TRANSITION TABLE, and not inside it. "May a human write this task at
    # all" is a question about the TASK and has the same answer for every new_status, so
    # checking it here makes "every transition the table would otherwise allow is refused"
    # true by construction rather than by enumeration. It is also above the inline due_date
    # update forty lines down, which writes a column BEFORE the guards below it refuse
    # anything — rung 0 is the only position from which "a refused mirror move writes
    # nothing" is actually true.
    #
    # WHY THIS IS THE RULE AND THE OLD BEHAVIOUR WAS A COINCIDENCE. Mirrors are seeded
    # with no assignee, and both callers refuse an unassigned task before reaching this
    # function — so until now a mirror was unwritable by ACCIDENT. Assign one through
    # task_assign, an entirely reasonable thing to do to get it onto a dashboard, and
    # that accident evaporates silently. This line is the rule.
    #
    # NOTHING IS WRITTEN HERE — no StatusTransition, no ActivityLog, no notification.
    # A refused move is not an event.
    #
    # NOT THE WHOLE FEATURE. The derivation hooks that will WRITE these statuses are
    # unbuilt. They belong to the source objects (phases 3-5), go through
    # record_transition() like every other status change, and carry the SOURCE EVENT's
    # actor (spec §2.4, §2.8) — they will not call this function, which exists to say no
    # to people. Until they are wired a mirror sits at its seeded status, which is known
    # and accepted.
    if task.is_mirror:
        messages.error(
            request,
            f"'{task.task_name}' is a mirror task — its status is derived from the "
            f"workspace that owns the work and cannot be set here. It will update "
            f"itself when that record changes."
        )
        return _TASK_STATUS_REFUSED
```

And the signature it sits inside, `projects/views.py:4179`:

```python
def _apply_task_status_change(task, new_status, profile, request, project):
```

### 1.2 Is it caller-sensitive? No.

**It is an unconditional refusal for any `is_mirror=True` task, regardless of caller,
and there is nothing in the signature a legitimate derivation write could use.**

Stated precisely, because "unconditional" is the load-bearing word:

- The predicate is `task.is_mirror` and nothing else. It reads no argument, no request
  attribute, no session flag, no setting.
- The five parameters carry no caller identity. `request` is present **only** so the
  helper can emit its own `messages.error()` — the docstring says the wording must not
  be allowed to drift between the two screens. `profile` is the acting human. Neither
  is a channel through which "this call is a derivation, not a person" could be
  smuggled without inventing it.
- It is the **first statement of the function body**, above the validity check, above
  the OPEX two-step rung, above the transition table, and above the inline `due_date`
  write at `views.py:4310-4318` — which is the only write in the function that lands
  before any guard can refuse. That ordering is what makes *"a refused mirror move
  writes nothing"* literally true rather than approximately true.

There is exactly one sibling refusal, and it is not a bypass either —
`_approval_preconditions()` at `projects/views.py:4660`, which refuses `submit` and
`reject` on a mirror because those two acts write approval columns without changing
status and would therefore never reach rung 0:

```python
    if task.is_mirror:
        messages.error(
            request,
            f"'{task.task_name}' is a mirror task - its status is derived from the "
            f"workspace that owns the work, so there is nothing to submit or approve."
        )
        return False
```

### 1.3 Would a bypass parameter be safe? **No — and it is the one change to refuse.**

The evidence that this is a deliberate absolute, not an accident of implementation:

1. **R-18** (`docs/execution-model.md:149`) — *"Task status changes have one decision
   path... A new task-status rule is added to the helper, never to a view."*
   `execution-model.md:411` describes the mirror refusal as *"One check in one place,
   per R-18, so it holds on the project-overview row and the task-detail block
   identically and cannot be routed around by using the other screen."*
2. **The comment block itself pre-answers this question** and answers it no:
   *"they will not call this function, which exists to say no to people."*
3. **OPEX spec §2 rule 2** (`docs/OPEX_task_template_spec.md:72`) calls it *"the single
   most important line in the feature."*
4. **`tests_mirror_readonly.py`** pins it with 24 tests through both entry points, on a
   really activated OPEX site, and — per `execution-model.md:441` — *"Every test assigns
   the mirror before posting"*, because a refusal test against an unassigned mirror
   passes without proving anything. The module was verified negatively at build time:
   with the `if` neutralised, 34 assertions fail.
5. **B22's own precedent for how this function grows**: it returns the *existing*
   `_TASK_STATUS_REFUSED` rather than a fourth outcome constant. The established shape
   for adding to this function is "another reason to say no", never "a way to say yes".

A `bypass_mirror_guard=True` parameter would invert all five. It would also be strictly
worse than it looks, because it changes the class of question a reader has to ask at
every call site: today *"can this path write a mirror?"* is answered by reading one
line; with a bypass flag it is answered by auditing every caller, forever, including
callers not yet written. **The refusal must stay unconditional.** Everything in §3 is
built on that.

### 1.4 Two stale comments found while confirming this (documentation only)

Not defects — the code is correct — but they will mislead whoever builds the hook:

| Location | Says | Actually |
|---|---|---|
| `views.py:4214-4215` | *"Five OPEX tasks carry the flag today (Design, Material Delivery, COD, As-Built Drawings, HOTO)"* | **Eight**, since prompt 1.5 split Material Delivery into four. Measured in §2.3. |
| `models.py:411` | *"an activated OPEX site really does carry five `is_mirror=True` rows"* | Eight, same reason. |
| `views.py:2960` (`opex_site_activate` docstring) | *"the OPEX task template (7 phases, 22 tasks, 5 mirrors)"* | 7 phases, **23** tasks, **8** mirrors. `execution-model.md:466` already carries the corrected figures. |

Worth one line of a future prompt's MODE. Out of scope here.

---

## 2 · TASK 2 — how a specific mirror is found

### 2.1 What identifies a mirror row: one boolean, and that is all

`projects/models.py:433`:

```python
    is_mirror            = models.BooleanField(default=False, db_index=True)
```

There is **no** `derivation_source` column, no mirror-type enum, no per-mirror subclass.
`models.py:417-422` states why, and states that the enum is future work:

```python
    # What it is NOT: it is not "has a source object". COD, HOTO and As-Built are
    # mirrors with no source object in existence today, and they must still be
    # unwritable. That is why this is a boolean and not a nullable derivation_source
    # enum — under `source IS NOT NULL ⇒ read-only`, those three would be NULL and
    # therefore writable by anyone. `derivation_source` is added BESIDE this in phases
    # 3–5, under a check constraint `derivation_source IS NULL OR is_mirror`.
```

Confirmed by search: `derivation_source` appears **nowhere** in the codebase except
those three comment lines. It does not exist as a field, a migration, or a constant.

**So `is_mirror` alone cannot tell the eight mirrors apart.** Something else must.

### 2.2 What *does* tell them apart: `template_task.code`

`projects/models.py:440-446` — the provenance FK copied onto every task at activation:

```python
    template_task        = models.ForeignKey(
        'TaskTemplateTask',
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='tasks',
    )
```

and its target's stable identifier, `projects/models.py:2281-2283`:

```python
    code                 = models.CharField(max_length=100)   # Stable across versions; label may be reworded
    label                = models.CharField(max_length=200)   # Copied verbatim into Task.task_name
```

`code` is derived, not authored — `utils.template_code_from_label()` at `utils.py:1233`:

```python
def template_code_from_label(label, max_length=100):
    """Stable uppercase identifier derived from a label.

    `code` is the cross-version identity of a phase or task: when version 2 rewords a
    label, `code` is what says it is still the same row. Derived rather than authored so
    version 1 needed no hand-written mapping. Verified collision-free across all 9 phase
    names and all 52 task names of the Residential template.
    """
    return re.sub(r'[^A-Za-z0-9]+', '_', label).strip('_').upper()[:max_length]
```

`Task.template_task` is set on every template-built row, `utils.py:1519-1521`:

```python
                is_mirror=t.is_mirror,
                template_task=t,   # provenance; nothing reads it back
```

Note the trailing comment — **"nothing reads it back"** is true of `Task.template_task`
today. It is *not* true of `TaskTemplateTask.code`, which
`views.py:_checklist_task_link_for()` already joins on. See §2.5.

### 2.3 Measured, not assumed: the eight mirrors on a real attached site

Probe run against the local dev database inside `transaction.atomic()` and rolled back
(`Project.objects.filter(project_id='ZZ-AUDIT-A23').count()` → `0` afterwards,
asserted). A Draft OPEX project was created, `utils.attach_opex_template()` called on
it, and the resulting rows read back:

```
mirror count: 8
  Design                 | Design                  | code=DESIGN                | role=Design | status=Not Started | assigned_to=None
  Procurement & Delivery | Delivery — Solar Panels | code=DELIVERY_SOLAR_PANELS | role=SCM    | status=Not Started | assigned_to=None
  Procurement & Delivery | Delivery — Inverters    | code=DELIVERY_INVERTERS    | role=SCM    | status=Not Started | assigned_to=None
  Procurement & Delivery | Delivery — BOS Kit      | code=DELIVERY_BOS_KIT      | role=SCM    | status=Not Started | assigned_to=None
  Procurement & Delivery | Delivery — MMS          | code=DELIVERY_MMS          | role=SCM    | status=Not Started | assigned_to=None
  Closeout               | COD                     | code=COD                   | role=PM     | status=Not Started | assigned_to=None
  Closeout               | As-Built Drawings       | code=AS_BUILT_DRAWINGS     | role=Design | status=Not Started | assigned_to=None
  Closeout               | HOTO                    | code=HOTO                  | role=PM     | status=Not Started | assigned_to=None

lookup by (project, is_mirror, code=DESIGN)   -> 1 row(s): [(2956, 'Design', 'Not Started')]
lookup by (project, is_mirror, role=Design)   -> 2 row(s): ['Design', 'As-Built Drawings']
lookup by task_name="Design"                  -> 1
design_assignment exists on fresh OPEX site: False
```

Four things this settles:

1. **`code` is unique per mirror and stable.** Eight mirrors, eight distinct codes.
2. **`assigned_role` is NOT a discriminator.** `role=Design` matches **two** mirrors —
   `Design` and `As-Built Drawings`. Anyone reaching for the role because it is one join
   shorter gets the wrong row half the time. (`EXECUTION_MODULE_DEFERRED.md:1289`
   already records this trap from the assignment side: `task_assign_design_head` covers
   *"both Design mirrors"*.)
3. **All eight seed unassigned and Not Started**, which is the state every mapping in §5
   has to be read against.
4. **`design_assignment` does not exist on a freshly attached OPEX site** — it is
   created lazily. This is the seed of §3.4.

### 2.4 The collision the lookup must scope around

`code` is unique *within a template*, not globally. Queried against the local database:

```
---- codes containing DESIGN ----
RESIDENTIAL  phase=DESIGN                     code=DESIGN                           label='Design'                            is_mirror=False
OPEX         phase=DESIGN                     code=DESIGN                           label='Design'                            is_mirror=True
RESIDENTIAL  phase=DETAIL_ENGINEERING_VISIT   code=DEV_DATA_TO_DESIGN               label='DEV Data to Design'                is_mirror=False
RESIDENTIAL  phase=DESIGN                     code=DESIGN_APPROVAL_BY_INTERNAL_TEAM label='Design Approval by Internal Team'  is_mirror=False
RESIDENTIAL  phase=DESIGN                     code=DESIGN_APPROVAL_BY_CUSTOMER      label='Design Approval by Customer'       is_mirror=False
```

**The Residential template has a task whose code is also `DESIGN`, and it is not a
mirror.** The uniqueness constraint is `(phase, code)` — `models.py:2306-2309` — so this
is legal and expected.

A lookup scoped to one project (`phase__project=project`) cannot cross templates and is
therefore already safe. But a lookup written *portfolio-wide* — a backfill command, a
reconciliation job, a metric — is not, and this is exactly the trap
`_checklist_task_link_for()` already carries a guard for:

```python
        link = (ChecklistTaskLink.objects
                .select_related('checklist')
                .filter(template_task__code=code,
                        template_task__phase__template__project_type=project.project_type)
                .first())
```

with the reason stated at `views.py:4009`: *"project_type is carried on the join so a
Residential code can never pick up an OPEX link that happens to share it."*

### 2.5 Does this lookup exist anywhere today? **No. It is entirely new.**

Searched: `is_mirror` appears in application code at nine sites and **not one of them
selects a particular mirror**:

| Site | What it does |
|---|---|
| `utils.py:280` `human_owned_tasks_q()` | `Q(is_mirror=False)` — excludes all mirrors from workload counters |
| `utils.py:288` `is_human_owned()` | the row-at-a-time form of the same |
| `utils.py:1519` | copies the flag at activation |
| `utils.py:1613` | `is_mirror=False` in PM pre-assignment |
| `views.py:3931` | `eligible = project.project_type == 'OPEX' and not task.is_mirror` |
| `views.py:4240`, `views.py:4660` | the two refusals |
| `admin.py:225` | `readonly_fields = ['status', 'is_mirror']` |
| `partials/_task_row.html:26,76` | renders the read-only badge instead of the status control |
| `seed_opex_test_data.py:729,830` | dev tooling: excludes mirrors from the tasks it drives, then counts them |

Every one is a **set** operation — all mirrors, or no mirrors. **Nothing anywhere,
read-only display included, resolves "which mirror is this".** The Design mirror has
never been addressed by name in this codebase.

**The one precedent for how to do it correctly is `_checklist_task_link_for()`**
(`views.py:~3990-4045`), which was migrated from name-keyed to code-keyed by prompt 2.4
for reasons that apply here word for word — `models.py:2795-2801`:

```python
    KEYED BY TEMPLATE TASK SINCE 2.4, NOT BY NAME. `template_task` is the key; task_name
    and project_type are kept beside it and stay populated, but they are no longer what a
    lookup matches on. The string key was wrong in two directions at once:

      - Rewording a template task's label silently detached its checklist. The label is
        content and moves with a template version (R-7); the checklist assignment is not
        content and must not move with it.
```

Both failure modes recur here. If a template v2 rewords `Design` to `Design Package`, a
name-keyed hook stops finding its row and **fails silently** — the mirror simply stops
updating, and nothing raises. `code` survives the rewording by construction.

**Fact for the record:** `task_name='Design'` also resolves to exactly one row today
(measured, §2.3). Name matching would work *right now*. The recommendation against it is
about the second template version, not about today.

---

## 3 · TASK 3 — the write path

> **PROPOSAL. Not built, not decided. §3.1–§3.3 confirm the shape the prompt put
> forward; §3.4 is a finding that changes what "done" means.**

### 3.1 The proposed shape is correct, and it is already on record

The prompt's proposal — *a new, narrow, dedicated function that writes only to
`is_mirror` Task rows, callable only from the Design status hook, never entering
`_apply_task_status_change()`* — is **confirmed workable** and is what A-2.2 §5.3
already argued for, before the chokepoint that makes it possible existed:

> **The mirror hook goes at the end of `apply_design_status()`, after the log and the
> ledger write, and it touches nothing on the `Task` side.** It has `assignment` (hence
> `project`), `from_status`, `new_status` and `actor` — everything §4 requires. Rung 0 in
> `_apply_task_status_change()` is untouched: the hook writes the mirror `Task` directly
> through `record_transition()`, exactly as the B22 comment prescribes, and never enters
> the human-refusal ladder. **`views.py` needs no edit for this.**

Session C then built the attachment point and left it marked and empty —
`design_views.py:340-350`:

```python
    # ── MIRROR HOOK ATTACHMENT POINT — STILL EMPTY. THE STATE LEDGER IS NOT IT. ──
    #
    # Session C left one marker here and Session D attached ONE OF THE TWO THINGS it
    # anticipated. Keeping them apart matters, because they are not the same derivation:
    #
    #   * THE STATE LEDGER (attached below, Session D). A `StatusTransition` row about
    #     the assignment ITSELF. It answers "who moved this site, from what, and when".
    #   * THE OPEX DESIGN MIRROR (still not built). A mirror `Task` whose state is
    #     DERIVED from this field. It writes a DIFFERENT subject through
    #     `record_transition()` and MUST NOT enter `_apply_task_status_change()`, which
    #     exists to refuse humans (rung 0, R-18/R-20).
    #
    # Everything either one needs is in scope right here:
    #
    #     assignment    the subject, and `assignment.project` through it
    #     from_status   the status the row was on before this write
    #     new_status    the status it is on now (None when no transition happened)
    #     actor         the UserProfile that caused it
```

**Nothing has been found that argues for a different shape.** Two alternatives were
considered and both are worse:

- *A bypass parameter on `_apply_task_status_change()`* — refused in §1.3.
- *A `post_save` signal on `DesignAssignment`* — refused by the design module's own
  standing rule, `models.py:2944-2948`: *"these models RECORD state... no `save()`
  override advances a status, no signal fires, no state machine is enforced here. A
  model that silently changes its own status is much harder to debug than one that does
  not."* A signal would also lose the actor, which spec §2.8 requires the mirror write
  to carry.

### 3.2 What the function must do, derived from what already exists

Sketch only — the argument list is the finding, not the body:

```python
def apply_mirror_status(task, new_status, actor, reason_code, detail=None):
    """THE ONE PLACE A MIRROR TASK'S STATUS IS WRITTEN.  Caller owns the atomic block."""
```

| Requirement | Where it comes from |
|---|---|
| **Refuses a non-mirror task outright** | The exact inverse of rung 0. This function's `if not task.is_mirror: raise` is what keeps the two paths from ever becoming one. Both functions then state a total rule, and every `Task` row is writable by exactly one of them. |
| **Writes through `record_transition()`** | Spec §2 rule 4: *"Every mirror write goes through `record_transition()`, like any other status change, so the ledger stays complete."* `Task` is already an instrumented subject — `utils.py:437`, `Task: SUBJECT_TASK`. No registry change needed. |
| **Carries the source event's actor** | Spec §2 rule 8: *"the Design Head who released, the SCM user who confirmed the GRN... the ledger reads truthfully rather than attributing the write to a system user."* `actor` is already in scope at the attachment point. |
| **Needs a new `REASON_*` constant** | No existing constant fits (`REASON_MILESTONE_SYNC` is the task↔milestone sync). `models.py:1729-1732` states this costs nothing: *"a seventh REASON must not cost an AlterField migration"* — `reason_code` carries no `choices=`. |
| **Writes `completed_at` on Done, `blocked_since` on Blocked** | Parity with `_apply_task_status_change()`'s `update_kwargs` (`views.py:4324-4332`). A mirror that reaches Done with a null `completed_at` is invisible to the CEO "Top People / COMPLETED" query and to any completion-rate metric. |
| **No `due_date`, ever** | Mirrors seed with `due_date=NULL`, `calculate_due_dates()` is deliberately not called for OPEX (B18), and R-20 keeps mirrors out of every overdue count. The human path's *"In Progress requires a due date"* guard (`views.py:4321`) is a rule about people, not about rows, and must not be carried across. |
| **No Issue creation on Blocked** | The human Blocked branch auto-creates an `Issue` (`views.py:4365-4375`). Spec `OPEX_task_template_spec.md:197` dropped the Punch Points mirror precisely because that would *"conflate task blockers with the commissioning punch list"*. A derived Blocked must write **only** the status. |
| **No notification** | The payment-milestone notification at `views.py:4453` is keyed on `is_payment_milestone`, which no mirror carries. Notifying on a derived state is a separate product decision and is out of scope. |
| **No-op when the derived state is unchanged** | Load-bearing — see §3.3. |
| **`filter(pk=...).update()`, not `save()`** | Same reasoning `apply_design_status()` gives for itself, and what `_apply_task_status_change()` already does. |

### 3.3 The idempotency requirement is not optional

Measured: `apply_design_status()` is called from **19 sites** writing **13 distinct
status values**. Many consecutive design transitions map to the *same* mirror state —
`in_design → arka_submitted → awaiting_head_arka → artifacts_uploaded → in_qc` is five
design events that (under any mapping in §5) are all "In Progress".

If the hook writes unconditionally it produces four `StatusTransition` rows reading
`In Progress → In Progress`. The codebase already rules that out twice, in the same
words:

`design_views.py:352-357`:

```python
    # A NO-TRANSITION CALL IS NOT A TRANSITION, and the guard below says so once for
    # both. `new_status=None` is the companion-field write documented above, and
    # `new_status == from_status` re-states the value the row already carries — neither
    # moved anything, and a row claiming `x -> x` is a history of something that did not
    # happen (R-3). It is a statement about what a transition IS, not error handling.
```

and Session C removed a live status write for exactly this reason —
`design_views.py:3140-3147`, on the dropped `qc_failed` write:

```python
        # THE FAILURE IS A LOG LINE, NOT A STATUS. It used to be both: `qc_failed` was
        # written here and overwritten by _open_next_attempt() two statements later,
        # inside this same atomic block, so no query anywhere could ever observe it. The
        # log line was always the thing that made the failure visible in the trail — the
        # old comment here said so — and it is untouched. Session C dropped the write
        # itself: a status nothing can read is not a state the site was in, and a mirror
        # hook firing on it would announce a transition that never happened.
```

**The hook is guarded twice, and the two guards are different:** the outer one at the
attachment point (`new_status is not None and new_status != from_status` — the design
row actually moved) and the inner one in the writer (the *derived* state actually
differs from the mirror's current status). Neither implies the other.

There is a second reason the writer needs its own guard: `_open_next_attempt()` calls
`apply_design_status()` and is *itself* called from three views that also call it
(`design_qc_fail`, `design_head_qc_fail`, `design_change_request_accept`), so two design
status writes inside one atomic block is a normal, existing shape.

### 3.4 THE FINDING — the hook alone leaves the mirror wrong on every site that exists

**Design work and execution activation are completely unsequenced, and design runs
first.**

- `design_views._opex_site()` (`design_views.py:222-228`) gates on `project_type` and
  `is_deleted`. It does **not** read `project.status`. Nothing in `design_views.py`
  references `project.status` at all — grep returns zero hits.
- `opex_site_activate` (`views.py:2957`) requires `status == 'Draft'` and attaches the
  template. It has no design precondition — the docstring says so explicitly: *"NO
  `assigned_design_id`. Design allocation for OPEX lives on
  `DesignAssignment.assigned_to`."*
- Therefore **the mirror `Task` row does not exist while design work is happening**, and
  when it is finally created by activation it is created at `Not Started` regardless of
  how far design has got.

Measured on the local development database (its counts match the production figures the
earlier audits quote — 87 assignments against 96 sites):

```
OPEX sites: 96
  with design_assignment: 87
  with any phase: 0
  status counts: {'Draft': 95, 'Active': 1}
design statuses: Counter({'awaiting_allocation': 82, 'artifacts_uploaded': 2,
                          'in_qc': 1, 'arka_submitted': 1, 'in_design': 1})
non-OPEX design assignments: 0
```

**Zero OPEX sites have tasks. Eighty-seven have design assignments, five of them past
allocation.** So on the day the hook ships:

- it fires for **no site**, because no site has a mirror row to write;
- and the moment anyone activates a site, its Design mirror is created at `Not Started`
  while its `DesignAssignment` may already be at `released` — **a mirror disagreeing
  with its source, which is the single failure the whole mirror design exists to
  prevent** (`EXECUTION_MODULE_DEFERRED.md:1416` states it in those terms).

**This is not an argument against the proposed shape.** It is a second, equally small
piece of work that the shape makes easy: `attach_opex_template()` should seed the Design
mirror from the site's current `DesignAssignment.status` (if a row exists) instead of
from the template default — one call to the same mapping function, at attach time,
inside the same atomic block. Same mapping, same writer, two callers:

```
  apply_design_status()   -> derive_design_mirror_state() -> apply_mirror_status()   # the hook
  attach_opex_template()  -> derive_design_mirror_state() -> (seed value)            # the reconcile
```

The mapping must therefore be a **pure function of the design status**, not a function
of the transition, or the second caller cannot use it. That is a real constraint on §5
and it is why §5 maps *states*, not *events*.

**Recommendation:** build both, or explicitly scope the reconcile out **in writing**.
Shipping the hook alone is defensible only if someone has decided that the first
activated site's Design mirror being wrong is acceptable — and that decision should be
made deliberately, not inherited.

### 3.5 Where the seven other mirrors sit

The proposed writer is generic (`apply_mirror_status(task, ...)`), so the four delivery
mirrors, COD, As-Built and HOTO reuse it unchanged when their sources exist. Their
blockers are unrelated to this work and already recorded:

- **Four deliveries** — blocked on B-18, the BOQ catalogue mapping.
  `migrations/0075:99-105`: *"Of the 207 OPEX catalogue rows, these four buckets match
  Module, Inverter, BOS and MMS — 52 rows. 155 map to nothing."*
- **COD, HOTO** — no source object exists (phase 5.3).
- **As-Built Drawings** — is a Design-role mirror but does **not** follow
  `DesignAssignment.status`. Nothing in the design workspace records as-built delivery
  today; it is post-commissioning. **It must not be swept into this hook** just because
  it shares the role. §2.3 shows exactly how easy that mistake is to make.

---

## 4 · TASK 4 — project-type scoping

### 4.1 Confirmed: `apply_design_status()` cannot be reached for a Residential project

Traced every one of the 19 call sites to its project resolution. All of the enclosing
functions resolve through one of three gates:

| Callers | Resolution | Gate |
|---|---|---|
| 16 views (`design_survey_upload`, `design_survey_link_set`, `design_mark_blocked`, `design_allocate`, `design_arka_submit`, `design_arka_approve`, `design_arka_reject`, `design_arka_head_approve`, `design_arka_head_reject`, `design_qc_start`, `design_qc_pass`, `design_head_qc_pass`, `design_artifact_upload`, `design_boq_complete`, `design_qc_fail`, `design_head_qc_fail`) | `_opex_site(project_id)` | 404s anything not OPEX |
| `design_bulk_allocate` → `_allocate_one` | `get_object_or_404(Project, ..., project_type='OPEX', program=program)` | explicit type filter |
| `design_change_request_accept` → `_open_next_attempt` | `change.attempt.assignment.project` | **transitive only** — no type check |

The gate itself, `design_views.py:222-228`:

```python
def _opex_site(project_id):
    """Fetch a non-deleted OPEX site or 404. Residential and CAPEX are unreachable
    through every view in this module."""
    project = get_object_or_404(Project, project_id=project_id, is_deleted=False)
    if project.project_type != 'OPEX':
        raise Http404('Design workflow applies to OPEX sites only.')
    return project
```

And `apply_design_status()` has **no callers outside `design_views.py`** — searched
across the whole package; the only other hits are `tests_design_status_chokepoint.py`
and comment references in `admin.py`.

### 4.2 But the guarantee is a runtime convention, not a constraint

The prompt states *"no `DesignAssignment` row exists to call it on"* for a Residential
project. **That is true today and it is true by construction of the write paths, but it
is not enforced anywhere.** Precisely:

- `DesignAssignment.project` is `OneToOneField(Project, ...)` — `models.py:3314-3316`.
  **No `limit_choices_to`, no `CheckConstraint`, no `clean()`.** The model docstring says
  *"One per OPEX site"*; the schema does not.
- The only runtime creator is `_get_or_create_assignment()` (`design_views.py:233-239`),
  reached from two views, both behind `_opex_site()`. Confirmed lazily created:

```python
def _get_or_create_assignment(project):
    """The DesignAssignment is created lazily by the first survey upload, so seeded or
    imported sites do not all carry an empty row from day one."""
```

- `DesignAssignmentAdmin` has `raw_id_fields = ['project']` and no queryset restriction,
  so a superuser **can** create a row against a Residential project. They cannot then
  move it: `status`, `released_at` and `released_by` are read-only there since Session C
  (`admin.py:450-452`), and the section comment is explicit — *"THE ADMIN IS NOT A
  RELEASE ROUTE"*. So an admin-created Residential assignment is inert: it exists and
  never transitions, and `apply_design_status()` is still never called on it.
- Measured: `non-OPEX design assignments: 0`.

**One read surface already contemplates non-OPEX assignments** — `views.py:1982-1986`:

```python
    tender_design_subq = DesignAssignment.objects.filter(
        project=OuterRef('pk'),
        project__project_type__in=['OPEX', 'CAPEX'],
        status__in=TENDER_DESIGN_SUBMITTED_STATUSES,
    )
```

`OPEX_TEMPLATE_AUDIT.md:834` records the other side of it: *"The Design mirror cannot
work on CAPEX at all — `design_views._opex_site()` 404s"*. So the read side anticipates
a CAPEX design assignment that the write side cannot create. That is a pre-existing
inconsistency, not something this hook introduces, and it is worth knowing about because
a CAPEX template is an explicitly planned future (`utils.attach_opex_template` docstring:
*"Resolves by `project.project_type` rather than a hardcoded 'OPEX', so a CAPEX template
becomes a seed and an activation route with no further change here"*).

### 4.3 Verdict: no project-type guard needed *in the hook* — the lookup is the guard

**An explicit `if project.project_type != 'OPEX': return` in the hook would be dead code
today**, and the codebase's own precedent (B10, A1) is to *close* a side door rather than
instrument every consumer against it.

The guard that **is** needed is different and stronger, and it falls out of §2 for free:
the lookup is `is_mirror=True` **and** `template_task__code='DESIGN'` **scoped to
`phase__project=assignment.project`**. On a Residential project that returns **no row** —
the Residential template has no mirrors at all — so the hook no-ops without ever asking
about the project type. **A "mirror not found" no-op is the correct behaviour and the
right project-type guard at the same time**, because it is also the correct behaviour for
the far more common real case: an OPEX site whose design is under way and which has not
been activated yet (§3.4, and today that is *every* site).

That no-op should be **logged, not silent** — `logger.warning`, following the precedent
`_checklist_task_link_for()` sets for its own fallback path (*"neither should be
invisible: the warning is how anyone can see which tasks are still on the old path and
why"*). A design status moving on a site whose mirror cannot be found is either a
not-yet-activated site (expected, common) or a template rewording that has silently
detached the hook (a real defect). Both need to be visible.

---

## 5 · TASK 5 — proposed status mapping

> ### ⚠ PROPOSAL — REQUIRES SIGN-OFF. This is a product decision, not a technical one.
> Two rows are genuinely ambiguous and are marked. **Do not build against this table
> until it is confirmed.**

### 5.1 A mapping already exists on disk, unsigned

Not a new question. It was written down twice and never ratified. Spec
`OPEX_task_template_spec.md:142`:

> `DesignAssignment.status`. Not Started until allocated · In Progress from allocation ·
> Done at `DESIGN_RELEASED` · returns to In Progress on reopen. `DESIGN_RELEASED` is not
> terminal — one reopen route exists, which is what makes rule 3 achievable here.

and copied into `migrations/0075_seed_opex_template_v1.py:74-77` as a comment on the seed
row itself. **§5.3 below is that sentence expanded to all fourteen statuses**, with the
two places it does not answer flagged. Treat the existing text as the starting proposal
it is, not as a decision already taken.

### 5.2 The full status vocabulary, and which values are actually reachable

`models.py:2969-3006`, all fourteen, with what was measured about each:

| # | Constant | Value | Written by | On the local DB |
|---|---|---|---|---|
| 1 | `DESIGN_AWAITING_SURVEY` | `awaiting_survey` | model default | 0 |
| 2 | `DESIGN_AWAITING_ALLOCATION` | `awaiting_allocation` | survey upload / link set | **82** |
| 3 | `DESIGN_ALLOCATED` | `allocated` | only via `_status_after_unblock()` for pre-Part-8 rows | 0 |
| 4 | `DESIGN_DUE_DATE_PROPOSED` | `due_date_proposed` | **no live call site** — Part 8 auto-approves at allocation | 0 |
| 5 | `DESIGN_IN_DESIGN` | `in_design` | `_allocate_one`, `_open_next_attempt` | 1 |
| 6 | `DESIGN_ARKA_SUBMITTED` | `arka_submitted` | `design_arka_submit`, `design_arka_head_approve` | 1 |
| 7 | `DESIGN_AWAITING_HEAD_ARKA` | `awaiting_head_arka` | `design_arka_approve` | 0 |
| 8 | `DESIGN_ARKA_REJECTED` | `arka_rejected` | both Arka reject views | 0 |
| 9 | `DESIGN_ARTIFACTS_UPLOADED` | `artifacts_uploaded` | `_maybe_advance_to_artifacts_uploaded` | **2** |
| 10 | `DESIGN_IN_QC` | `in_qc` | `design_qc_start` | 1 |
| 11 | `DESIGN_AWAITING_HEAD_QC` | `awaiting_head_qc` | `design_qc_pass` | 0 |
| 12 | `DESIGN_QC_FAILED` | `qc_failed` | **nothing — Session C removed the write.** Legal value; no committed row has ever carried it | 0 |
| 13 | `DESIGN_RELEASED` | `released` | `design_head_qc_pass` only | 0 |
| 14 | `DESIGN_SURVEY_RETURNED` | `survey_returned` — user-facing name **"Design Hold"** | `design_mark_blocked` | 0 |

Rows 3, 4 and 12 are effectively dead but remain legal `choices` values; the mapping must
still answer for them, because it is a pure function of a stored value (§3.4).

### 5.3 PROPOSED mapping

| Design status | → Mirror | Confidence |
|---|---|---|
| `awaiting_survey` | **Not Started** | settled |
| `awaiting_allocation` | **Not Started** | ⚠ **see A** |
| `allocated` | **In Progress** | settled |
| `due_date_proposed` | **In Progress** | settled (dead value; map for completeness) |
| `in_design` | **In Progress** | settled |
| `arka_submitted` | **In Progress** | settled |
| `awaiting_head_arka` | **In Progress** | settled |
| `arka_rejected` | **In Progress** | settled |
| `artifacts_uploaded` | **In Progress** | settled |
| `in_qc` | **In Progress** | settled |
| `awaiting_head_qc` | **In Progress** | settled |
| `qc_failed` | **In Progress** | settled (dead value; map for completeness) |
| `released` | **Done** | settled |
| `survey_returned` (Design Hold) | **Blocked** | ⚠ **see B** |

The shape is deliberately coarse — three of four mirror states carry nine design statuses
between them. That is correct and is spec §2 rule 7 working as intended: *"Portfolio
metrics read the source object, never the mirror."* The mirror answers "has the PM's site
got its design"; the fourteen-status detail belongs to the design dashboard, which already
has `design_metrics._classify()` for it.

**Reopen is handled by construction.** `released → in_design` (via the draft-group
change-request route, `design_views.py:3405-3412`, then `_open_next_attempt()`) maps
`Done → In Progress` with no special case, satisfying spec rule 3 — *"Mirrors follow their
source in both directions."* Note this is a `Done → In Progress` move that
`_apply_task_status_change()`'s `VALID_TRANSITIONS` table forbids for humans
(`Task.DONE: {Task.BLOCKED}`, *"prevents gaming completion"*). **That is a real divergence
between the two writers and it is correct** — the human rule exists to stop someone
un-completing their own work; a mirror has no such actor. It should be stated in the
writer's docstring rather than discovered by whoever notices the tables differ.

### 5.4 ⚠ A — `awaiting_allocation`: does the mirror ever show Not Started?

**The prompt asked this directly, and it is the one row where the two defensible answers
give visibly different portfolios.**

The facts:

- **82 of 87 sites sit here right now.** Whatever this maps to is what the Design mirror
  reads on essentially the entire tender.
- `OPEX_TEMPLATE_AUDIT.md:454` already flagged the consequence: *"The Design mirror reads
  **Not Started on 82 of 87 sites** on day one. Expected, but worth [saying]."*
- `awaiting_allocation` means the Head has the survey and has not yet handed the site to a
  designer. **Nobody is doing design work.**

**Proposed: Not Started.** Because it is true — no design work is under way — and because
"In Progress" on 82 sites where nothing is happening destroys the mirror's only job, which
is to tell a PM whether their site's design is moving.

**The counter-argument, stated fairly:** the OPEX site is activated, and Phase 1 is Design,
so a PM opening the site sees Phase 1 at Not Started with no visible reason and no action
available to them. Under R-21 the site's "current phase" skips Design entirely
(`current_phase()` excludes mirrors, so a fresh OPEX site reads *Approvals
(Pre-Installation)*), which softens this — but does not remove it.

**The real fix for that discomfort is spec §2 rule 6, not this mapping.** Rule 6 is mirror
ageing — `Design — In Progress, 41 days` — and it is **NOT BUILT**; the spec calls it *"now
urgent rather than pending"* and *"Premortem #3 is live, not hypothetical."* Nothing in any
template reads `is_mirror` today except the read-only badge in `_task_row.html`. **If the
answer to A feels wrong either way, that is rule 6 missing, and no mapping choice can
substitute for it.**

**Decision needed: Not Started (proposed) or In Progress.**

### 5.5 ⚠ B — Design Hold: Blocked, or stay In Progress?

`survey_returned` is a **hold flag, not a workflow stage** — `models.py:2996-3001`:

> `survey_returned` is NOT part of that line any more. It is repurposed as an off-sequence
> DESIGN HOLD FLAG: the assigned designer puts the site on hold over an inadequate survey,
> which stops their clock and surfaces to the Head.

The case **for Blocked**: it is what Blocked means; a held site should look held; and
`_status_after_unblock()` already restores the pre-hold status cleanly, so the mirror
returns to In Progress by itself with no extra machinery.

The case **against**: `Task.BLOCKED` carries obligations the human path enforces and a
derived write must not. Specifically —

1. **The human Blocked branch creates an `Issue`.** A derived Blocked writes only the
   status (§3.2). So a Blocked mirror will be the **first** blocked task in the system with
   no linked `Issue`. Any screen that assumes "blocked ⇒ there is an issue" is wrong on it.
   That assumption was not audited here and should be before B is settled.
2. **`blocked_since` feeds the CEO aged-block KPI.** Checked: mirrors are excluded from it
   — `views.py:2153` applies `human_owned_tasks_q()` on the *base* queryset, and the
   comment says every one of the ~40 conditional counts inherits it. So a Blocked mirror
   does **not** pollute `blocked_open` or `blocked_aged_7d`. This risk is real but already
   closed.
3. **A Design Hold is not a project blocker.** The designer is waiting on a better survey
   from the Head; installation, procurement and approvals are unaffected. "Blocked" on the
   PM's task list may read as more alarming than the situation is.

**Proposed: Blocked** — it is the honest state, and the one counter-risk that mattered (2)
is already handled. **But the `Issue`-less-Blocked question in (1) is a genuine unknown and
should be checked before this is signed.**

**Decision needed: Blocked (proposed), or In Progress with a hold badge (which needs rule 6
first).**

### 5.6 What is NOT proposed

- **No mapping to a fifth mirror state.** `Task.STATUS_CHOICES` has four values and adding
  one is a migration plus every counter in the app.
- **No `due_date` derivation from `DueDateCommitment`.** The design due date is a real and
  useful date, and putting it on the mirror would drag mirrors into overdue counting, which
  R-20 forbids. It belongs to rule 6's ageing display, reading the source object directly.
- **No `assigned_to` derivation from `DesignAssignment.assigned_to`.** Tempting — it would
  put the designer's name on the row — but assigning a mirror grants project visibility and
  BOQ read via `permissions.user_can_view_project()` (`OPEX_TEMPLATE_AUDIT.md:577`), so it
  is an access-control change wearing a display change's clothes. Separate decision,
  separate prompt.

---

## 6 · Where this audit differs from the prompt's assumptions

Stated explicitly, as the verification bar requires.

1. **"the four delivery mirrors, COD, As-Built, and HOTO"** — correct (8 mirrors total,
   spec v1.5). But **three comments in the code still say five**, including rung 0's own
   comment block, which is the first thing a builder will read. §1.4.
2. **"a stable field/type identifying which mirror is which"** — the prompt offers this as
   an either/or against name matching. **Both exist.** `template_task.code` is the stable
   identifier (and is already the codebase's chosen pattern for this exact problem,
   post-2.4); `task_name` also resolves uniquely today. The recommendation against names is
   about template v2, not about correctness now. §2.5.
3. **"DesignAssignment only exists for OPEX projects"** — true in fact and in every write
   path, **but not enforced by the schema**, and the admin can still create (though not
   transition) a Residential row. Also, one read surface at `views.py:1984` already filters
   `project_type__in=['OPEX', 'CAPEX']`. §4.2.
4. **"does OPEX activation put Design work into some allocatable state that should already
   read In Progress?"** — the premise is inverted by what was measured. **Activation does
   not touch design at all, and design runs entirely before activation.** 87 sites have
   design assignments; **0 have tasks.** The question is not what activation does to
   design, it is what design has already done by the time activation happens. §3.4.
5. **The prompt frames the mapping as undecided.** A mapping is already written down in two
   places — spec §3 line 142 and the seed migration's own comment. It has never been
   ratified and §5 treats it as a proposal, but it is not a blank sheet, and building
   something that contradicts it would silently contradict the seed's documentation too.
   §5.1.

---

## 7 · Verdict

**The hook itself — one session, comfortably.** The chokepoint exists, the attachment point
is marked with all four required values in scope, `Task` is already an instrumented ledger
subject, the lookup is a two-clause filter on an existing indexed field plus an existing FK,
and the new writer is perhaps forty lines. No migration (`reason_code` carries no `choices=`
— `models.py:1729`). No model change. No template change. No edit to `views.py`.

**The feature is not one session unless the sequencing gap is scoped out in writing.** §3.4
is the whole of it: on today's data the hook would fire for **zero** sites, and the first
site anyone activates gets a Design mirror that disagrees with its source from the moment it
is created. The fix is small and uses the same mapping function, but it touches
`attach_opex_template()` — a different module, a different test surface, and the function
that 1.3c's activation tests pin.

**Recommended split, if it is split:**

| | Scope | Size |
|---|---|---|
| **A** | `apply_mirror_status()` + `derive_design_mirror_state()` + the lookup + the hook at the attachment point + tests | one session |
| **B** | reconcile at `attach_opex_template()`, so an activated site's Design mirror starts from the site's actual design status | half a session, but its own test surface |

**Recommended as one session** if the mapping (§5) is signed off first and B is explicitly
included. **A alone should not ship silently** — if it does, the deploy note must say that
the Design mirror is correct only for sites activated *before* design begins, which
describes none of the 96 sites that exist.

**Blocking on sign-off before any build:** §5.4 (A) and §5.5 (B). Everything else in this
document is a confirmed fact or a proposal that follows from one.
