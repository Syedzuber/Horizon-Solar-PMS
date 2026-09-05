# 2.3 discovery — QA/QC gate and punch points

**Read-only audit. 5 Sep 2026.** No application code was written. Tasks 1–3 are confirmed
facts with raw pastes; task 4 is a set of sized options that need a decision, not a
recommendation dressed as one.

---

## Task 1 — how Blocked → Issue creation actually works today

### Where it lives

One place: the Blocked branch of `_apply_task_status_change()`, the single decision path for
task status (R-18). `projects/views.py:4359-4412`.

Both status screens — `task_status_update` (`views.py:4517`, the project-overview row control)
and `task_detail_status_update` (`views.py:4600`, the task-detail block) — reach it through
that one function, so there is exactly **one** auto-creation site, not two.

### The code, verbatim

```python
4359:    # Blocked requires a stated blocking issue (fresh transition only)
4360:    if new_status == Task.BLOCKED and task.status != Task.BLOCKED:
4361:        block_issue_title = request.POST.get('block_issue_title', '').strip()
4362:        if not block_issue_title:
4363:            messages.error(request, 'Please state the blocking issue before marking this task as Blocked.')
4364:            return _TASK_STATUS_NEEDS_BLOCK_REASON
4365:
4366:        # R-2: the status write and its ledger row commit together or not at all.
4367:        # The atomic block is deliberately TIGHT — it holds the pair and nothing
4368:        # else, so the Issue creation and notifications below keep exactly the
4369:        # failure behaviour they had before this was instrumented.
4370:        with transaction.atomic():
4371:            Task.objects.filter(pk=task.pk).update(**update_kwargs)
4372:            record_transition(
4373:                task, to_status=new_status, from_status=task.status,
4374:                actor=profile, reason_code=REASON_BLOCKED,
4375:                # The hold reason, captured where one exists — this branch is the
4376:                # only task path that collects any free text at all.
4377:                remark=block_issue_title,
4378:            )
4379:
4380:        block_severity = request.POST.get('block_issue_severity', Issue.HIGH)
4381:        if block_severity not in dict(Issue.SEVERITY_CHOICES):
4382:            block_severity = Issue.HIGH
4383:        block_assignee = None
4384:        block_assignee_id = request.POST.get('block_issue_assigned_to', '').strip()
4385:        if block_assignee_id:
4386:            try:
4387:                block_assignee = UserProfile.objects.get(pk=block_assignee_id)
4388:            except UserProfile.DoesNotExist:
4389:                pass
4390:        with transaction.atomic():
4391:            issue = Issue.objects.create(
4392:                project=project,
4393:                task=task,
4394:                title=block_issue_title,
4395:                description=request.POST.get('block_issue_description', '').strip(),
4396:                severity=block_severity,
4397:                status=Issue.OPEN,
4398:                raised_by=profile,
4399:                assigned_to=block_assignee,
4400:            )
4401:            # Creation transition: from_status is blank because there is no from.
4402:            record_transition(
4403:                issue, to_status=Issue.OPEN, actor=profile,
4404:                reason_code=REASON_BLOCKED, remark=block_issue_title,
4405:            )
4406:        log_activity(
4407:            project, profile,
4408:            f"Blocked task '{task.task_name}' — issue: {block_issue_title}",
4409:            entity_type='Issue', entity_id=issue.pk, action_code='issue_created',
4410:        )
4411:        messages.success(request, f'Task blocked. Issue "{block_issue_title}" created.')
4412:        return _TASK_STATUS_APPLIED
```

### Every field set on that Issue at creation

| Field | Value on the auto-created Issue | Discriminating? |
|---|---|---|
| `project` | the task's project | No |
| `task` | the blocked task — **always non-null on this path** | Partly — see below |
| `delivery_challan` | not passed → `None` | No |
| `title` | `block_issue_title`, free text, required | No |
| `description` | `block_issue_description`, free text, may be `''` | No |
| `severity` | POST `block_issue_severity`, **defaulting to `Issue.HIGH`**, validated against `SEVERITY_CHOICES` | No — user-chosen |
| `status` | `Issue.OPEN` | No |
| `raised_by` | the actor's `UserProfile` | No |
| `assigned_to` | POST `block_issue_assigned_to` or `None` | No |
| `due_date` | not passed → `None` | No |
| `resolved_at` / `closed_at` / `resolution_note` | not passed → `None` / `None` / `''` | No |

### The Issue model in full — there is no type, category or flag field

`projects/models.py:1306-1357`, verbatim:

```python
class Issue(models.Model):
    """A blocker or problem raised against a project or a specific task."""

    LOW      = 'Low'
    MEDIUM   = 'Medium'
    HIGH     = 'High'
    CRITICAL = 'Critical'
    SEVERITY_CHOICES = [
        (LOW,      'Low'),
        (MEDIUM,   'Medium'),
        (HIGH,     'High'),
        (CRITICAL, 'Critical'),
    ]

    # Workflow: Open → In Progress → Resolved (by anyone) → Closed (by PM only)
    # PM can also reopen a Resolved issue back to Open
    OPEN        = 'Open'
    IN_PROGRESS = 'In Progress'
    RESOLVED    = 'Resolved'
    CLOSED      = 'Closed'
    STATUS_CHOICES = [
        (OPEN,        'Open'),
        (IN_PROGRESS, 'In Progress'),
        (RESOLVED,    'Resolved'),
        (CLOSED,      'Closed'),
    ]

    project         = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='issues')
    task            = models.ForeignKey(Task, on_delete=models.SET_NULL, null=True, blank=True, related_name='issues')  # Null for project-level issues not tied to a task
    delivery_challan = models.ForeignKey(  # Null unless the issue was raised directly against a specific delivery
        'DeliveryChallan', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='issues',
    )
    title           = models.CharField(max_length=200)
    description     = models.TextField(blank=True, default='')
    severity        = models.CharField(max_length=20, choices=SEVERITY_CHOICES, default=MEDIUM)
    status          = models.CharField(max_length=20, choices=STATUS_CHOICES, default=OPEN)
    raised_by       = models.ForeignKey(
        'UserProfile', on_delete=models.SET_NULL, null=True, blank=True, related_name='raised_issues',
    )
    assigned_to     = models.ForeignKey(
        'UserProfile', on_delete=models.SET_NULL, null=True, blank=True, related_name='assigned_issues',
    )
    raised_at       = models.DateTimeField(auto_now_add=True)
    due_date        = models.DateField(null=True, blank=True)
    resolved_at     = models.DateTimeField(null=True, blank=True)  # Set when status transitions to Resolved
    closed_at       = models.DateTimeField(null=True, blank=True)  # Set when PM closes the issue
    resolution_note = models.TextField(blank=True, default='')    # Required text when resolving

    class Meta:
        ordering = ['-raised_at']

    def __str__(self):
        return f"{self.project.project_id} — {self.title}"
```

**Fifteen columns. None of them is a type, category, kind, class, source or origin.**

### Is there an existing flag that could distinguish a punch point without a new field?

**No — but the question deserves the four near-misses spelled out, because each looks usable
until you check what else writes it.**

1. **`severity`** — the auto-created blocker defaults to `HIGH`, other creation sites default
   to `MEDIUM`. That is a *default*, not an invariant: all four creation paths read severity
   from the POST and let the user pick any of the four values. `severity=Critical` is
   already reachable from the ordinary "raise issue" modal. Cannot discriminate.

2. **`task IS NOT NULL`** — true of every auto-created blocker, but *also* true of every
   issue raised through `create_task_issue` (`views.py:9593`), which is a person choosing to
   file an issue against a task by hand. And a punch point raised against a specific
   installation step would *want* a task FK. Discriminates the wrong axis.

3. **`delivery_challan IS NOT NULL`** — identifies delivery issues only
   (`views.py:9703`). Orthogonal.

4. **`StatusTransition.reason_code`** — this is the closest thing that exists. The Issue's
   creation transition carries `reason_code=REASON_BLOCKED` on the auto-created path
   (`views.py:4404`) and `REASON_CREATED` on all three manual paths (`views.py:9501`,
   `9606`, `9717`). The vocabulary is module-level constants with **no `choices=` on the
   field**, so a new reason costs no migration (`models.py:1730-1735`):

   ```python
   # Reason vocabulary — module-level constants per R-10, NOT a lookup table and
   # deliberately NOT a `choices=` list on the field. subject_type gets choices
   # because it is structural (a seventh subject type means new instrumentation and
   # a code change anyway); a seventh REASON must not cost an AlterField migration.
   REASON_CREATED            = 'created'             # first state; there is no from-status
   REASON_BLOCKED            = 'blocked'             # task blocked, blocking Issue raised alongside
   ```

   **This is a real discriminator and it needs no migration.** It is also the wrong place to
   put a load-bearing gate, for a reason the codebase already states about itself:
   `StatusTransition` is an append-only *ledger*, its rows are history, and R-3/R-4 keep it
   as the record of what happened rather than as current state. Reading "is this a punch
   point" out of the ledger means a subquery on every COD gate check and every punch-point
   count, and it makes the answer un-editable — a mis-filed punch point could never be
   re-classified without writing a *second* creation row for an object created once. Sized
   in Task 4 as option C anyway, because "no migration" is a real advantage and the decision
   is not mine.

**Confirmed: no field on `Issue` today distinguishes a QA/QC punch point from an ordinary
blocker. The nearest usable signal is on a different table and is append-only.**

### Why this matters — the template already paid for it

The OPEX template dropped its Punch Points mirror for exactly this reason. Migration
`0075_seed_opex_template_v1.py:146-150`, in the Phase 5 block:

```python
            # Punch Points was a mirror in spec v1.0 and is DROPPED: `Issue` exists per
            # site, but "punch point" and "blocking" do not, and the Blocked branch of
            # _apply_task_status_change() auto-creates Issue rows — so the mirror would
            # conflate task blockers with the commissioning punch list. Returns at
            # phase 2.3. Do not re-add it here.
```

And `docs/DESIGN_MIRROR_HOOK_AUDIT.md:424` records the same constraint from the derivation
side: *"A derived Blocked must write **only** the status."*

---

## Task 2 — does COD exist as a gateable object yet?

### The older spec doc's claim

`docs/PHASE_1_OUTCOME.md:97`:

```
- **COD, HOTO, As-Built** need phase 5 and phase 3 objects that do not exist.
```

### Verdict: **still true.** Nothing built since has created a COD object.

Here is the whole of what exists and what does not.

**What exists: a COD `TaskTemplateTask` row, flagged `is_mirror=True`.**
`projects/migrations/0075_seed_opex_template_v1.py:161-177`:

```python
        {
            'phase_name': 'Closeout',
            'phase_order': 7,
            'tasks': [
                # Milestone / COD record (phase 5.3). No source object exists today.
                {'task_order': 1, 'task_name': 'COD', 'assigned_role': 'PM', 'task_type': 'Internal', 'is_mirror': True},
                # Jointly owned by PM and Coordinator per the spec. assigned_role holds
                # ONE value, so 1.3a added 'Project Coordinator' to Task.ROLE_CHOICES —
                # a value UserProfile already had — rather than storing an
                # unrepresentable composite or silently defaulting to PM.
                {'task_order': 2, 'task_name': 'Completion Certificates (Paperwork)', 'assigned_role': 'Project Coordinator', 'task_type': 'Internal'},
                # Design workspace. Post-commissioning, so Closeout rather than Phase 1
                # despite appearing under Design in the source list. Blocks HOTO.
                {'task_order': 3, 'task_name': 'As-Built Drawings', 'assigned_role': 'Design', 'task_type': 'Internal', 'is_mirror': True},
                # Phase 5.3. No source object exists today.
                {'task_order': 4, 'task_name': 'HOTO', 'assigned_role': 'PM', 'task_type': 'Internal', 'is_mirror': True},
            ],
        },
```

So on an activated OPEX site there **is** a row called COD, with a `status` column, in
phase 7. That is the whole of the good news, and it is not enough, for three reasons that
compound.

**Reason 1 — a mirror's status is refused to every human, so COD cannot move at all today.**
Rung 0 of the refusal ladder, `projects/views.py:4267-4274`:

```python
    if task.is_mirror:
        messages.error(
            request,
            f"'{task.task_name}' is a mirror task — its status is derived from the "
            f"workspace that owns the work and cannot be set here. It will update "
            f"itself when that record changes."
        )
        return _TASK_STATUS_REFUSED
```

**Reason 2 — no derivation hook exists to write it either.** The comment on
`Task.is_mirror` (`models.py:417-421`) names COD as the canonical example of a mirror whose
source does not exist:

```python
    # What it is NOT: it is not "has a source object". COD, HOTO and As-Built are
    # mirrors with no source object in existence today, and they must still be
    # unwritable. That is why this is a boolean and not a nullable derivation_source
    # enum — under `source IS NOT NULL ⇒ read-only`, those three would be NULL and
    # therefore writable by anyone.
```

`docs/PHASE_1_OUTCOME.md:88` states the general position: **"No derivation hook exists. Not
one."** All eight mirrors sit at Not Started permanently.

**Reason 3 — the thing COD is supposed to mirror is a milestone/COD record, and OPEX sites
are minted no milestones at all.** `PaymentMilestone` (`models.py:1242`) is the only
milestone-shaped model in the codebase, its vocabulary is `M1 | M2 | M3`, and
`milestone_create` refuses OPEX outright (`views.py:7689-7697`):

```python
    # M1/M2/M3 ARE RESIDENTIAL. Prompt 1.5.
    #
    # `opex_site_activate` deliberately mints no milestones (spec v1.5 §2a) — M1 On
    # Survey Completion / M2 On Material Supply / M3 On Commissioning describe a
    # three-milestone residential contract, not a tender. This view is that decision's
    # blind spot: it is the manual fallback for projects activated before milestones
    # existed, and it checked POST and PM ownership and nothing else, so a PM could put
    # the exact three rows onto a tender site that activation refuses to create.
```

### Model inventory — searched, not assumed

Every model class in `projects/models.py` (54 of them):

```
Project Program ProjectPhase Task Milestone ProjectDocument DueDateChangeLog
ProjectFieldEditLog UserProfile VendorCategory Vendor VendorBrand BOQItemMaster BOQ
BOQItem BOQRevision BOQCorrection Notification PaymentMilestone TaskAttachment Issue
ActivityLog Comment DeliveryChallan DCLineItem AppendOnlyViolation StatusTransition
NotificationLog SystemSettings PaymentRequest DesignSubmission TaskDurationTemplate
TemplateVersionLocked TaskTemplate TaskTemplatePhase TaskTemplateTask DependencyCycle
TaskTemplateTaskDependency TaskDependency Checklist ChecklistItem ChecklistTaskLink
ChecklistItemCompletion DesignAssignment DueDateCommitment DesignAttempt ArkaSubmission
DesignFile DesignChangeRequest SiteGroupTypeImmutable SiteGroup SiteGroupMembership
DesignAnalyticsPreference StockLocation
```

**There is no `Commissioning`, `COD`, `CommercialOperation`, `Handover`, `HOTO` or `AsBuilt`
model.** `Milestone` (`models.py:521`) is the Residential project milestone, unrelated.
The only other occurrences of the word are `Project.target_commissioning_date` (a date field
on the project, `models.py:92`), the Gantt label map (`gantt_constants.py:26,101-111`, which
maps Residential task *names* to display strings), and the Residential template's
"Plant Commissioning" task.

### What *does* have a movable status near commissioning

Worth naming, because it is the honest alternative if the gate has to bite on something:
the **Phase 5 "Testing & Commissioning"** task, `migration 0075:151-155`:

```python
            'tasks': [
                {'task_order': 1, 'task_name': 'Testing & Commissioning', 'assigned_role': 'Site Engineer', 'task_type': 'Internal'},
                # The physical install. Distinct from the Phase 2 approval — locked.
                {'task_order': 2, 'task_name': 'Net Meter Installation',  'assigned_role': 'Site Engineer', 'task_type': 'Internal'},
            ],
```

This one is **not** a mirror. It is an ordinary Site Engineer task, it moves through
`_apply_task_status_change()`, and since 2.1 it already carries a submit → approve gate
(below). It is a real, gateable object. It is *not* COD — it is the site engineer's
commissioning work, not the commercial operation date — and treating it as COD would be a
substitution the spec has not been asked to make.

### Plain statement

**Enforcing "an open punch point blocks COD" requires an object that does not exist yet.**
The COD row that exists is a mirror with no source and no hook: it is permanently Not
Started, no human may write it, and gating a status nothing can change is a no-op that would
pass its own tests. The COD source object is scheduled for phase 5.3 and is unbuilt.

**A separate, buildable thing does exist:** a punch-point gate on the *Phase 5 Testing &
Commissioning task's approval*, which is a live status with a live approver. Whether that
counts as "the COD gate" is a product decision, not a technical one.

### One caveat on the counts in this document

The local database has **97 OPEX projects (95 Draft, 2 Active) and zero activated with the
template** — `ProjectPhase` rows on OPEX projects: 0; `Task` rows on OPEX projects: 0;
`is_mirror=True` tasks: 0; `Task` rows named 'COD': 0; `Issue` rows on OPEX projects: 0.
So every COD statement above is read off the *template* and the code, which is where the
facts are, and no local row contradicts or confirms them. Production counts were not read.
Total `Issue` rows locally: **29** (5 task-linked, 1 DC-linked, 24 Open/In Progress).

---

## Task 3 — the QA/QC failure path itself

### Design has two QA/QC failure gates. Both are Design-module-only.

**Gate 1 — `design_qc_fail`**, `projects/design_views.py:3466`:

```python
def design_qc_fail(request, project_id):
    """GATE 1 — DESIGN QC fails the package: the attempt closes and N+1 opens for rework.

    `qc_remarks` AND `qc_failure_category` are both mandatory (settled decision 7). The
    remarks are checked here so the reviewer gets a usable message, and enforced
    underneath by the Part 1 CHECK constraint `qc_remarks_required_when_qc_failed` for any
    writer that bypasses this view. The category is view-enforced only.

    A QC FAILURE ENDS THE ATTEMPT — the Head never sees it, and head_verdict stays
    'pending' forever on this row, meaning "not judged" exactly as it does on an attempt
    closed by a PM change request.

    THE CATEGORY'S GROUP DECIDES WHOSE REWORK THIS IS. A Group A failure counts toward the
    designer's multiplier; Group B and C do not (settled decision 8). Nothing is computed
    here — the category is stored, and design_metrics reads it back through
    error_category_group().
    """
```

Its write block (`design_views.py:3524-3549`):

```python
    now = timezone.now()
    with transaction.atomic():
        attempt.qc_verdict          = QC_FAILED
        attempt.qc_remarks          = remarks
        attempt.qc_failure_category = category
        attempt.qc_reviewed_by      = profile
        attempt.qc_reviewed_at      = now
        attempt.redo_required       = sorted(redo)
        attempt.save(update_fields=['qc_verdict', 'qc_remarks', 'qc_failure_category',
                                    'qc_reviewed_by', 'qc_reviewed_at', 'redo_required'])
        # THE FAILURE IS A LOG LINE, NOT A STATUS. It used to be both: `qc_failed` was
        # written here and overwritten by _open_next_attempt() two statements later,
        # inside this same atomic block, so no query anywhere could ever observe it. The
        # log line was always the thing that made the failure visible in the trail — the
        # old comment here said so — and it is untouched. Session C dropped the write
        # itself: a status nothing can read is not a state the site was in, and a mirror
        # hook firing on it would announce a transition that never happened.
        ...
        log_activity(project, profile,
                     f'Design QC failed attempt {attempt.attempt_number} '
                     f'[{DESIGN_ERROR_CATEGORY_LABELS.get(category, category)}] '
                     f'— redo: {", ".join(sorted(redo))}: {remarks}',
                     entity_type='DesignAttempt', entity_id=attempt.pk,
                     action_code='design_qc_failed')

        new_attempt = _open_next_attempt(
            assignment, ATTEMPT_REASON_QC_FAILED, profile,
            f'Design QC failure on attempt {attempt.attempt_number}', redo=redo)
```

**Gate 2 — `design_head_qc_fail`**, `design_views.py:3622`, same shape,
`action_code='design_head_qc_failed'`.

**Neither creates an `Issue` row.** A design QC failure is an `ActivityLog` line plus a new
`DesignAttempt`; it produces no punch point, no blocker, and nothing on the project's issue
list. It is also scoped to `DesignAttempt` / `ArkaSubmission` — objects that exist only in
the design workspace and have no bearing on an installation task.

Note what Design *has* that Issue does not: **a failure category with a group taxonomy**
(`qc_failure_category`, `models.py:3563`, backed by a CHECK constraint
`qc_remarks_required_when_qc_failed` at `models.py:3657`, read back by
`design_analytics.py:225,548,565,710`). That is the shape a punch-point category would take
if option B in Task 4 is chosen — precedent exists in this codebase and it works.

### Is there an equivalent QA/QC failure path for OPEX execution tasks generally?

**There is a QA/QC *rejection* path, and it creates nothing.** Prompt 2.1's two-step
completion is the first and only consumer of `is_qaqc`. `projects/views.py:4935`:

```python
def task_reject(request, project_id, task_id):
    """
    Reject a submitted OPEX task. Clears the submission so the engineer can act on
    the remarks and submit again; the task returns to (in practice, remains) In
    Progress (2.1 5).
    ...
    The rejection reason is written to `approval_remarks` and the approval columns
    are left null - a rejected task has no approver, which is what keeps
    `approved_at` usable as the Done gate.

    Access: PM-level authority on the project, or QA/QC with sight of it - the same
    people who may approve. POST only.
    """
```

Its entire write (`views.py:4999-5020`):

```python
    Task.objects.filter(pk=task.pk).update(
        submitted_by=None,
        submitted_at=None,
        # The engineer's own account of the work goes with the submission it
        # belonged to; leaving it would attach last round's description to next
        # round's submission.
        submission_remarks='',
        approval_remarks=remarks,
    )
    task.refresh_from_db()

    # Almost always already true - see the docstring. Routed through the chokepoint
    # rather than written directly, so that if it ever does fire it is a status
    # change with a ledger row like every other (R-18).
    if task.status != Task.IN_PROGRESS:
        _apply_task_status_change(task, Task.IN_PROGRESS, profile, request, project)

    log_activity(
        project, profile,
        f"Rejected submission: {task.task_name} - {remarks}",
        entity_type='Task', entity_id=task.pk, action_code='task_rejected',
    )
    messages.warning(request, f"'{task.task_name}' sent back to the assignee.")
    return _approval_response(request, project, task)
```

**No `Issue` is created. No punch point is raised. `approval_remarks` is a single text
column that the next rejection overwrites**, and the model comment says so explicitly
(`models.py:485-489`):

```python
    # Carries BOTH verdicts' text: an approval note when approved_at is set, and the
    # rejection reason when it is not. One column because a task holds one open
    # verdict at a time, and the ActivityLog line beside it names which it was.
    approval_remarks     = models.TextField(blank=True, default='')
```

### Who holds QA/QC authority, and how thin it is

`projects/permissions.py:1155-1181`, verbatim:

```python
def user_can_approve_task(user, project):
    """
    Return True if `user` may approve or reject a submitted task on `project`.

    PM-level authority on the project, OR the QA/QC capability flag plus visibility
    of the project.

    WHY `is_qaqc` IS PAIRED WITH VISIBILITY AND NOT USED ALONE. UserProfile.is_qaqc
    is a PORTFOLIO-WIDE boolean — there is no per-project QA/QC assignment anywhere
    in the schema today. Read on its own it would let one QA/QC holder sign off work
    on every site in the company, including sites they cannot open. Anding it with
    user_can_view_project() scopes the capability to the projects the person already
    reaches by their role, which is the closest thing to "QA/QC on that project" the
    data model can currently express. When a per-project QA/QC assignment arrives,
    it replaces the visibility term HERE and nowhere else.

    No `task` argument: approval authority is a property of the project and the
    person, identical for every task on it. The per-task questions — is this thing
    actually submitted, and is the person asking the one who submitted it — are
    state, not permission, and are asked by `task_approve`.
    """
    profile = getattr(user, 'profile', None)
    if profile is None:
        return False
    if user_can_manage_project(user, project):
        return True
    return bool(profile.is_qaqc) and user_can_view_project(user, project)
```

And the flag itself, `models.py:723` — with the comment that predicted this prompt:

```python
    is_qaqc                 = models.BooleanField(default=False)  # May record a QA/QC verdict on a site's work, and raise a punch point against it
```

`models.py:717-718`: *"Consumers arrive with 2.2 (`is_hse`), **2.3 (`is_qaqc`)** and 4.1
(`is_warehouse_keeper`)"*. `user_can_approve_task` is the first half of that; **"and raise a
punch point against it" is the half 2.3 owes.**

There is still **no UI writer for `is_qaqc`** — `seed_opex_test_data.py:54` records it as
*"no writer anywhere, not even in admin"*, so the flag is set from the shell or Django admin
only. Any 2.3 feature gated on `is_qaqc` is dark until somebody sets the flag.

### Task 3, stated plainly

**Outside the Design module there is no QA/QC *failure* path at all — only a QA/QC
*rejection* path, and it raises nothing.** Rejecting an OPEX task writes one overwritable
text column and an `ActivityLog` line, then hands the task back to the engineer. There is no
record that survives the next submission, nothing appears on any issue list, nothing is
assignable to a third party, nothing has a due date, and nothing can be counted. The only
way a QA/QC failure becomes a tracked object today is if the rejector *separately* uses the
ordinary "raise issue" modal — an unlinked, manual second act.

---

## Task 4 — sized options for distinguishing a punch point from an ordinary blocker

**These are options, not a recommendation. The decision is the product owner's.**

### The read sites that would have to change, counted once for all options

Any option that makes punch points a subset of `Issue` forces every existing consumer to
declare which subset it wants. There are **23 `Issue.objects` call sites** outside tests and
migrations:

| Location | What it does | Would need a decision |
|---|---|---|
| `views.py:4391` | Blocked auto-create | **write site** |
| `views.py:9487` | `create_project_issue` | **write site** |
| `views.py:9593` | `create_task_issue` | **write site** |
| `views.py:9703` | `create_delivery_issue` | **write site** |
| `seed_opex_test_data.py:757` | demo data | **write site** |
| `views.py:9377` | `_active_issue()` — the single resolution path | pass-through, no change |
| `views.py:786` | PM dashboard delivery-issue block | read — likely exclude punch points |
| `views.py:1491`, `1661` | dashboard issue counts | read — likely exclude |
| `views.py:2239` | CEO issue aggregate | read — likely split |
| `views.py:8453` | `project_overview` issue card | read — likely split |
| `views.py:8909` | `task_detail` issue list | read — likely split |
| `views.py:10557` | DC detail issue list | read — unaffected |
| `views.py:9844`–`10067` | 6 lifecycle updates (status/resolve/close/reopen/assign ×2) | pass-through, no change |
| `send_eod_digest.py:234`, `239`, `286` | 3 digest metrics | read — likely exclude or split |

**5 write sites, 8 read sites that would need a decision, 10 pass-throughs.** Plus **4
templates** rendering issue lists (`project_overview.html:1001-1052`,
`project_detail.html:535-609`, `task_detail.html:168-236`,
`delivery_challan_detail.html:251-270`) and **13 URL routes** (`urls.py:297-322,425-427`).
`Issue` is **not registered in Django admin**, so there is no admin form to widen. There is
**no `issue_list` view** — issues are only ever rendered embedded in a parent object's page,
which means a punch-point list screen would be new, not a filter on an existing one.

---

### Option A — new boolean `Issue.is_punch_point`

```python
is_punch_point = models.BooleanField(default=False, db_index=True)
```

- **Migration:** yes — one `AddField`. Trivial and non-blocking: `default=False` is correct
  for all 29 existing rows (all pre-date the concept), no backfill, no `RunPython`. Follows
  the `is_mirror` precedent exactly, index included for the same reason (`models.py:433`).
- **Write sites to change:** 1 of 5 — only the new punch-point raise path sets `True`. The
  four existing creates keep the column default and are **untouched**; the Blocked branch in
  particular needs no edit at all, which is the strongest thing about this option.
- **Read sites to change:** the 8 above, each adding `is_punch_point=False` or `=True`.
- **What it buys:** the exact predicate the COD gate needs —
  `Issue.objects.filter(project=…, is_punch_point=True, status__in=[OPEN, IN_PROGRESS])` —
  in one indexed column.
- **What it does not buy:** any classification *within* punch points. Every punch point is
  the same kind of punch point. If the QA/QC verdict later needs categories the way Design's
  does (`qc_failure_category`), that is a second migration.
- **Precedent argument in its favour:** `docs/OPEX_TEMPLATE_AUDIT.md:335-347` is the record
  of this codebase choosing a boolean over a nullable enum for the *same* shape of question,
  and stating why — *"one status field answers one question"* (R-5).

---

### Option B — new category/type field on `Issue`

```python
ISSUE_KIND_BLOCKER     = 'blocker'
ISSUE_KIND_PUNCH_POINT = 'punch_point'
KIND_CHOICES = [...]
kind = models.CharField(max_length=20, choices=KIND_CHOICES, default=ISSUE_KIND_BLOCKER, db_index=True)
```

- **Migration:** yes — one `AddField`. Also non-blocking, but **the default is a claim about
  history**: defaulting all 29 existing rows to `'blocker'` asserts that none of them was a
  punch point, which is true only because the concept did not exist. That is defensible and
  should be written into the migration's comment rather than left implied.
- **Write sites to change:** potentially all 5, if each is made to name its kind explicitly
  rather than lean on the default. Leaning on the default costs 1 site (same as option A);
  naming them costs 5. **A `choices=` list means every future value is an `AlterField`
  migration** — this is precisely the cost `StatusTransition.reason_code` was designed to
  avoid, and `models.py:1727-1731` says so in as many words.
- **Read sites to change:** the same 8, with `kind=` filters.
- **What it buys over A:** room for a third and fourth kind without another schema decision
  — `'hse_finding'` (2.2), `'delivery_defect'` (4.x), `'snag'`. If the roadmap has more than
  two kinds of Issue coming, this is the cheaper *total*.
- **What it costs over A:** every read site must now enumerate rather than negate. `NOT
  is_punch_point` is total; `kind='blocker'` silently drops a future third kind from every
  count that was written before it existed. That failure is silent and is the single
  strongest argument against B.

---

### Option C — no new field: read `StatusTransition.reason_code`

Add `REASON_PUNCH_POINT = 'punch_point'` beside the eleven existing reason constants and
have the punch-point raise path pass it to `record_transition()`.

- **Migration:** **none.** `reason_code` carries no `choices=` specifically so a new reason
  costs nothing (`models.py:1727-1731`, quoted in Task 1).
- **Write sites to change:** 1 — the new raise path. Nothing else moves.
- **Read sites to change:** all 8, and each becomes a **join or subquery** against
  `StatusTransition` filtered to `subject_type='issue'`, `reason_code='punch_point'`. The
  COD gate becomes an `EXISTS` subquery rather than a column read.
- **What it buys:** zero schema change, zero migration risk, and the discriminator lands in
  the ledger where the *act* of raising a punch point is already recorded anyway.
- **What it costs, and this is the part that needs deciding, not glossing:**
  1. **The ledger is append-only by design** (R-4; `models.py:1786-1802`, `save()` refuses to
     touch an existing row, `delete()` refuses outright). A mis-filed punch point can never
     be reclassified — you would have to write a second *creation* row for an object created
     once, which is a lie in the ledger.
  2. **Current state read from history.** Every other "what kind of thing is this" question
     in this codebase is a column. R-3 keeps `ActivityLog` as the trail rather than as state;
     the same argument applies to `StatusTransition`.
  3. **`update()` bypasses the append-only overrides** — the model docstring admits it
     (*"QuerySet.update() and QuerySet.delete() operate in SQL and bypass both overrides
     entirely"*), so the immutability protecting the discriminator is application-level only.
  4. **Performance.** The COD gate would run on a screen render, not once.

---

### Option D — a separate `PunchPoint` model

Not a variant of the above; a different answer to the question.

- **Migration:** yes — `CreateModel`, plus whatever FKs (project, task, raised_by,
  assigned_to, verified_by, waived_by, waiver_reason).
- **Write sites to change:** 0 existing. `Issue` is untouched entirely.
- **Read sites to change:** 0 existing. Every punch-point read is new.
- **What it buys:** the punch point gets fields `Issue` should not have — a *verification*
  step (`docs/execution-model.md:297`: QA/QC *"raises punch points, verifies resolution"*)
  and a **waiver** (B-12, `execution-model.md:581`: *"the PM alone waives, at any severity,
  with no second signature"*). Neither maps onto `Issue`'s
  Open → In Progress → Resolved → Closed workflow: resolution-verified-by-QA/QC and
  waived-by-PM are two different terminal states and `Issue.status` has one.
- **What it costs:** a second issue-shaped table with its own list, detail, comment,
  notification and assignment surfaces — most of which `Issue` already has and none of which
  would be shared. `docs/execution-model.md:322` explicitly maps *"Punch points → `Issue`"*
  in the model inventory, so choosing D reverses a recorded architectural decision. It
  should be chosen deliberately or not at all.
- **The thing that argues for D despite the cost:** B-12's waiver. A waived punch point is
  neither Resolved nor Closed — the problem is still there and someone accepted it. Under
  A/B/C that is a fifth `Issue.status` value affecting all 29 existing rows' state machine
  and all 6 lifecycle views. Under D it is a column on a new table nothing else reads.

---

### Sizing summary

| | Migration | Write sites touched | Read sites needing a decision | New surface | Supports B-12 waiver |
|---|---|---|---|---|---|
| **A** boolean | 1 AddField, no backfill | 1 of 5 | 8 | list screen | Needs a 5th `Issue.status` |
| **B** kind enum | 1 AddField, no backfill; future values cost AlterField | 1 (or 5 if explicit) | 8 | list screen | Needs a 5th `Issue.status` |
| **C** reason_code | **none** | 1 | 8, each becoming a subquery | list screen | No — ledger is append-only |
| **D** new model | 1 CreateModel | 0 | 0 | full CRUD + list + detail | Natively |

**The one thing all four share:** none of them makes "punch points block COD" enforceable,
because of Task 2.

---

## Verdict

**The full "QA/QC gate + punch points blocking COD" feature is NOT buildable today. It
splits, and the split falls exactly where task 2's finding puts it.**

### Buildable now — "build the distinction"

Everything except the COD gate itself:

1. **The discriminator** — one of options A–D. All four are small; the decision is about
   which future they make cheap, not about this week's work.
2. **Raising a punch point** — `is_qaqc` already grants approve/reject authority through
   `user_can_approve_task()` (`permissions.py:1155`), and the model comment on the flag
   already promises *"and raise a punch point against it"*. The authority half exists; only
   the raise path is missing.
3. **Wiring it to `task_reject`** — today a QA/QC rejection writes one overwritable text
   column and vanishes on resubmission. Giving the rejector the option to raise a punch
   point alongside is the natural home for it, and it is the gap task 3 found.
4. **Every read that consumes punch points** — counts, lists, dashboards, the EOD digest.
5. **Punch-point resolution and verification** — QA/QC verifies, per `execution-model.md:297`.
6. **The waiver** — B-12 is *answered* (PM alone, any severity, no counter-signature) and
   recorded as still open until 2.3 builds it. Buildable under any option; cheapest under D.

### Deferred — "wire the actual COD gate later"

The gate itself, because there is nothing to gate:

- The COD `Task` row exists but is `is_mirror=True` with **no source object and no derivation
  hook**. It is permanently Not Started, refused to every human at rung 0. **A gate on it
  would be a no-op that passes its own tests** — the worst kind of shipped feature.
- The COD source object is a *"Milestone / COD record (phase 5.3)"* per the template comment.
  It does not exist, and `PaymentMilestone` is not it — OPEX sites are minted no milestones
  at all, by decision (`views.py:7689`).
- **The gate is a two-line predicate once the object exists.** The expensive part is the
  discriminator and the punch-point lifecycle, which is what 2.3 can build now. Nothing about
  building the distinction first makes the gate harder later.

### One decision 2.3 should take on purpose rather than by accident

If the product owner wants a punch-point gate *biting on something* in this phase, the only
live candidate is the **Phase 5 "Testing & Commissioning"** task's approval — a non-mirror
task with a real status, a real approver and an existing 2.1 approval rung that a punch-point
check would slot into as **rung 2**. That is a defensible feature. **It is not COD**, and it
should not be labelled COD in the code, the UI or the tests, or phase 5.3 inherits a name
already taken by something else.

### Also noted, not in this prompt's remit

- **B-6 — the checklist gate on task completion — is assigned to 2.3**
  (`docs/execution-model.md:1096`: *"No checklist gate on task completion. That is **B-6**
  and it belongs to 2.3."*). It is not mentioned in this prompt's four tasks. Whoever scopes
  the 2.3 build prompt should confirm whether it is in or out.
- **`is_qaqc` has no UI writer anywhere, not even Django admin.** Any 2.3 surface gated on it
  is invisible until somebody sets the flag from a shell. That is a one-line addition to
  `UserProfileAdmin` and it is not currently anybody's prompt.
