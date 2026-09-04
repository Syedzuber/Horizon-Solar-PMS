# A-2.1 — Design Head requests: pre-flight audit

**Read-only session, 4 Sep 2026.** One file was written (this one). No application code,
template, migration, `models.py`, `views.py`, `design_views.py`, `permissions.py` or test
was changed. Every claim below is a paste from the working tree or a grep hit with file and
line. Where a question could only be answered by changing code, I say so and stop.

```
$ git rev-parse --abbrev-ref HEAD
main

$ git rev-parse HEAD
57ae66fadacca304ae57ba0bb00d63acfe38221e

$ git log -1 --oneline
57ae66f Close design hold reopen route on released assignments
```

`git status` at the start of this session carried 19 untracked `.audit_*` scratch files from
a prior session and nothing else. They are untouched here.

---

## Q1 — Arka / CAD linkage

### Q1.1 · The field that ties a CAD submission to a specific Arka version

**It is a real FK, `NOT NULL`, `on_delete=PROTECT`, pointing at a row that carries an
integer `version` unique within the attempt. It is not descriptive.**

`projects/models.py:3644-3650` — `DesignFile`, the model every CAD and BOQ artifact is a row
of:

```python
class DesignFile(models.Model):
    """A CAD or BOQ artifact uploaded against an attempt, versioned per kind.

    ARTIFACT PAIRING (settled decision 4): derived_from_arka is NOT NULL. Every CAD
    and BOQ artifact records which Arka version it was drawn against, so QC can never
    review a CAD produced from a superseded layout. PROTECT on that FK, so an Arka
    version that artifacts derive from cannot be deleted out from under them.
```

The field itself, `projects/models.py:3696-3698`:

```python
    derived_from_arka = models.ForeignKey(
        ArkaSubmission, on_delete=models.PROTECT, related_name='derived_files',
    )
```

The version number lives on the target row, `projects/models.py:3535-3541`:

```python
    attempt = models.ForeignKey(
        DesignAttempt, on_delete=models.CASCADE, related_name='arka_submissions',
    )
    version     = models.PositiveIntegerField()
    capacity_kw = models.DecimalField(max_digits=10, decimal_places=2)
    ...
    arka_link   = models.URLField(max_length=1000)
```

and is unique per attempt, with a partial unique constraint naming exactly one live version,
`projects/models.py:3616-3625`:

```python
    class Meta:
        ordering        = ['attempt', 'version']
        unique_together = ('attempt', 'version')
        constraints = [
            # Exactly one live Arka version per attempt; superseded rows coexist freely.
            models.UniqueConstraint(
                fields=['attempt'],
                condition=models.Q(is_current=True),
                name='uniq_current_arka_per_attempt',
            ),
```

So the linkage is `DesignFile.derived_from_arka_id -> ArkaSubmission.id`, and
`(attempt_id, version)` identifies the Arka version in business terms. Nothing about the
pairing is a string, a label, or a timestamp inference.

The value written into that FK is not chosen by the caller — it is whatever the gate helper
returns, `projects/design_views.py:2223-2225`:

```python
    PAIRING: derived_from_arka is set to the value _require_approved_arka() returns,
    which is the CURRENT approved version. It is never read off an older submission and
    never inferred from timestamps.
```

`projects/design_views.py:2288-2296`:

```python
        design_file = DesignFile.objects.create(
            attempt=attempt, kind=kind, version=next_version,
            bucket=bucket, path=stored_path,
            original_filename=(upload.name or '')[:255],
            size_bytes=getattr(upload, 'size', None),
            content_type=(getattr(upload, 'content_type', '') or '')[:100],
            archive_listing=listing,
            derived_from_arka=arka,
            uploaded_by=profile, remarks=remarks, is_current=True,
        )
```

**One consequence worth stating now, because it bounds Q1.4:** `derived_from_arka` is NOT
NULL but carries no constraint on the target's *verdict*. Nothing in the schema stops a
`DesignFile` from pointing at a `pending` or `rejected` Arka. The approval requirement lives
entirely in the view layer — see Q1.3.

---

### Q1.2 · Every reference to the Arka statuses

There are three stored status values and one derived stage key in play:

| what | stored value | where defined |
|---|---|---|
| Arka awaiting Design QC | `arka_submitted` | `models.py:2819` |
| Arka awaiting Design Head | `awaiting_head_arka` | `models.py:2831` |
| Arka approved, artifacts incomplete | **not a stored value** — `arka_submitted` + `head_verdict='approved'`, surfaced as the derived key `arka_approved` | `design_metrics.py:182` |

`projects/models.py:2819`, `2831`, and the choices list at `2862-2864`:

```python
DESIGN_ARKA_SUBMITTED      = 'arka_submitted'
DESIGN_ARKA_REJECTED       = 'arka_rejected'
...
DESIGN_AWAITING_HEAD_ARKA  = 'awaiting_head_arka'
...
    (DESIGN_ARKA_SUBMITTED,      'Arka submitted'),
    (DESIGN_AWAITING_HEAD_ARKA,  'Arka — awaiting Design Head'),
    (DESIGN_ARKA_REJECTED,       'Arka rejected'),
```

The reason "Arka approved, artifacts incomplete" has no stored value of its own is stated in
the code, `projects/design_views.py:209-216`:

```python
# STATUS AFTER A HEAD ARKA APPROVAL IS `arka_submitted`, NOT a new value. Part 3 settled
# that there is no status for "Arka approved, artifacts outstanding" — the verdict on the
# current Arka disambiguates, and design_metrics._classify() already splits the two. Part 9
# keeps that exactly: QC approval moves the site to `awaiting_head_arka`, and the Head's
# approval returns it to `arka_submitted`, now carrying head_verdict='approved'. The site
# is not going backwards; `awaiting_head_arka` means strictly "the Head has not ruled", so
# the site must leave it the moment he does.
```

#### `DESIGN_ARKA_SUBMITTED` — application code

```
$ grep -rn "DESIGN_ARKA_SUBMITTED" --include=*.py projects/ | grep -v "/tests"
projects/design_metrics.py:40:    DESIGN_DUE_DATE_PROPOSED, DESIGN_IN_DESIGN, DESIGN_ARKA_SUBMITTED,
projects/design_metrics.py:214:    if status == DESIGN_ARKA_SUBMITTED:
projects/design_views.py:62:    DESIGN_ARKA_SUBMITTED, DESIGN_ARKA_REJECTED, DESIGN_ARTIFACTS_UPLOADED,
projects/design_views.py:181:                           DESIGN_ARKA_SUBMITTED, DESIGN_ARKA_REJECTED,
projects/design_views.py:1597:    if assignment.status != DESIGN_ARKA_SUBMITTED:
projects/design_views.py:1790:        and assignment.status == DESIGN_ARKA_SUBMITTED
projects/design_views.py:1894:        assignment.status = DESIGN_ARKA_SUBMITTED
projects/design_views.py:1919:    if assignment.status != DESIGN_ARKA_SUBMITTED:
projects/design_views.py:2122:        assignment.status = DESIGN_ARKA_SUBMITTED
projects/design_views.py:2605:    opening_status = (DESIGN_ARKA_SUBMITTED
projects/design_views.py:3505:                                status__in=(DESIGN_ARKA_SUBMITTED,
projects/design_views.py:3736:        if assignment.status == DESIGN_ARKA_SUBMITTED:
projects/design_views.py:3819:        'awaiting_arka':       base.filter(status=DESIGN_ARKA_SUBMITTED,
projects/design_views.py:3888:        Q(status=DESIGN_ARKA_SUBMITTED,
projects/design_views.py:3894:        'awaiting_arka': base.filter(status=DESIGN_ARKA_SUBMITTED,
projects/models.py:2819:DESIGN_ARKA_SUBMITTED      = 'arka_submitted'
projects/models.py:2862:    (DESIGN_ARKA_SUBMITTED,      'Arka submitted'),
```

What each non-import hit is:

- `design_metrics.py:214` — `_classify()`, the branch that splits `arka_submitted` into two
  stage buckets by `head_verdict`.
- `design_views.py:181` — membership in `CHANGE_REQUEST_STATUSES`.
- `design_views.py:1597` — the `_maybe_advance_to_artifacts_uploaded()` idempotency guard;
  the progression only ever fires *from* this status.
- `design_views.py:1790` — `design_head_review()` context: `can_qc_verdict`.
- `design_views.py:1894` — `design_arka_submit()` writes it.
- `design_views.py:1919` — `_verdict_target()`, the gate-1 refusal test.
- `design_views.py:2122` — `design_arka_head_approve()` writes it *back* after Head approval.
- `design_views.py:2605` — `_open_next_attempt()` opening-status choice when a head-approved
  Arka was carried forward.
- `design_views.py:3505` — `design_qc_queue()` queryset.
- `design_views.py:3736` — `designer_dashboard_context()`, the next-action resolver.
- `design_views.py:3819` / `3888` / `3894` — dashboard counters (see below).

#### `DESIGN_AWAITING_HEAD_ARKA` — application code

```
$ grep -rn "DESIGN_AWAITING_HEAD_ARKA\|awaiting_head_arka" --include=*.py projects/ | grep -v "/tests" | grep -v migrations
projects/design_metrics.py:43:    DESIGN_AWAITING_HEAD_ARKA, DESIGN_AWAITING_HEAD_QC,
projects/design_metrics.py:181:    ('awaiting_head_arka',  'Arka awaiting Design Head'),
projects/design_metrics.py:202:                      'awaiting_head_arka', 'awaiting_head_qc')
projects/design_metrics.py:218:        # QC has passed but the Head has not sits at `awaiting_head_arka` and never
projects/design_metrics.py:637:    return _queue_age(sites, 'awaiting_head_arka', 'awaiting_head_qc', today)
projects/design_metrics.py:776:        # Both Arka stages date from the submission itself. `awaiting_head_arka` is the
projects/design_metrics.py:780:        if site['stage'] in ('arka_submitted', 'awaiting_head_arka') and site['arka'] is not None:
projects/design_views.py:64:    DESIGN_AWAITING_HEAD_ARKA, DESIGN_AWAITING_HEAD_QC,
projects/design_views.py:178:# `awaiting_head_arka` is included for completeness and is unreachable in practice —
projects/design_views.py:183:                           DESIGN_AWAITING_HEAD_ARKA, DESIGN_AWAITING_HEAD_QC)
projects/design_views.py:1799:        and assignment.status == DESIGN_AWAITING_HEAD_ARKA
projects/design_views.py:1808:        and assignment.status == DESIGN_AWAITING_HEAD_ARKA
projects/design_views.py:1945:    if assignment.status != DESIGN_AWAITING_HEAD_ARKA:
projects/design_views.py:2002:        assignment.status = DESIGN_AWAITING_HEAD_ARKA
projects/design_views.py:3506:                                            DESIGN_AWAITING_HEAD_ARKA),
projects/design_views.py:3522:        awaiting_head = assignment.status == DESIGN_AWAITING_HEAD_ARKA
projects/design_views.py:3692:    DESIGN_AWAITING_HEAD_ARKA:  ('none', '', 'Arka passed Design QC — waiting for the '
projects/design_views.py:3825:        'awaiting_head_arka':  base.filter(status=DESIGN_AWAITING_HEAD_ARKA).count(),
projects/models.py:2829:# the QC reviewer owes a verdict, `awaiting_head_arka` means the Head does, and a
projects/models.py:2831:DESIGN_AWAITING_HEAD_ARKA  = 'awaiting_head_arka'
projects/models.py:2863:    (DESIGN_AWAITING_HEAD_ARKA,  'Arka — awaiting Design Head'),
projects/views.py:1760:# The three Arka statuses (arka_submitted / awaiting_head_arka / arka_rejected) are
```

Migration hit, for completeness — the value appears once more inside the choices blob of an
`AlterField`, `projects/migrations/0055_part9_design_qc_gate.py:166`.

#### `arka_approved` — the derived "Arka approved, artifacts incomplete" key

```
$ grep -rn "arka_approved" --include=*.py --include=*.html projects/
projects/design_metrics.py:172:#   'arka_approved'  is status=arka_submitted whose current Arka is already approved,
projects/design_metrics.py:182:    ('arka_approved',       'Arka approved, artifacts incomplete'),
projects/design_metrics.py:221:            return 'arka_approved'
projects/design_views.py:1691:        'arka_approved':  _approved_arka(attempt) is not None,
projects/templates/projects/design/site_workspace.html:140:    {% elif arka_approved %}
projects/templates/projects/design/site_workspace.html:159:    {% if not arka_approved %}
projects/templates/projects/design/site_workspace.html:238:      {% elif is_designer and arka_approved %}
projects/templates/projects/design/site_workspace.html:276:    {% if not arka_approved %}
projects/templates/projects/design/_design_status_chips.html:24:  than the raw status: `arka_approved` when head_verdict is approved, `arka_submitted`
projects/templates/projects/design/_design_status_chips.html:33:  {% elif status == 'arka_submitted' and arka_approved %}
projects/templates/projects/design/_design_status_chips.html:49:  gate. `arka_approved` is _approved_arka() and now tests head_verdict, so the green pill
projects/templates/projects/design/_design_status_chips.html:54:  {% if arka_approved %}
```

The classifier that produces it, `projects/design_metrics.py:209-222`:

```python
def _classify(assignment, current_arka):
    """Which stage bucket one assignment falls in. Exactly one, always."""
    status = assignment.status
    if status == DESIGN_SURVEY_RETURNED:
        return 'blocked'
    if status == DESIGN_ARKA_SUBMITTED:
        # PART 9: the test is head_verdict, not verdict. `arka_submitted` carrying a
        # head-approved Arka means BOTH gates are done and the designer owes artifacts;
        # anything else at this status means Design QC still owes a verdict. (An Arka that
        # QC has passed but the Head has not sits at `awaiting_head_arka` and never
        # reaches this branch.) Same status, opposite bottleneck.
        if current_arka is not None and current_arka.head_verdict == ARKA_APPROVED:
            return 'arka_approved'
        return 'arka_submitted'
```

#### Templates

```
$ grep -rn "arka_submitted" projects/templates/
projects/templates/projects/design/qc_dashboard.html:182:                {% if row.stage_key == 'arka_submitted' or row.stage_key == 'awaiting_head_arka' %}
projects/templates/projects/design/qc_dashboard.html:239:            {% if s.stage == 'arka_submitted' or s.stage == 'awaiting_head_arka' %}
projects/templates/projects/design/tender_dashboard.html:476:            {% if row.stage_key == 'arka_submitted' or row.stage_key == 'awaiting_head_arka' %}
projects/templates/projects/design/tender_dashboard.html:538:            {% if s.stage == 'arka_submitted' or s.stage == 'awaiting_head_arka' %}
projects/templates/projects/design/_dashboard_design_chips.html:35:    `arka_submitted` covers BOTH "waiting on Design QC" and "approved at both gates, CAD and
projects/templates/projects/design/_dashboard_design_chips.html:39:    {% elif p.design.status == 'arka_submitted' and p.design.arka.head_verdict == 'approved' %}
projects/templates/projects/design/_dashboard_design_chips.html:42:    {% elif p.design.status == 'arka_submitted' %}
projects/templates/projects/design/_design_status_chips.html:15:  `arka_submitted` IS TWO DIFFERENT STATES AND MUST NOT RENDER AS ONE. Part 3 settled that
projects/templates/projects/design/_design_status_chips.html:17:  `arka_submitted` and the VERDICT disambiguates. Part 9 kept that, so a site whose Arka has
projects/templates/projects/design/_design_status_chips.html:18:  cleared both gates still stores `arka_submitted` — and a chip reading "Arka submitted"
projects/templates/projects/design/_design_status_chips.html:24:  than the raw status: `arka_approved` when head_verdict is approved, `arka_submitted`
projects/templates/projects/design/_design_status_chips.html:33:  {% elif status == 'arka_submitted' and arka_approved %}
projects/templates/projects/design/_design_status_chips.html:36:  {% elif status == 'arka_submitted' %}
```

Hits at `_dashboard_design_chips.html:35`, `_design_status_chips.html:15/17/18/24` and
`tender_dashboard.html:229` are inside `{% comment %}` blocks, not logic. Excluding those,
the logic count is **nine template branches in four files**, plus:

- the `arka_rejected` branches, `_dashboard_design_chips.html:45`, `:103`, `:108` and
  `_design_status_chips.html:30`;
- the counter render, `projects/templates/dashboard/design.html:188-189`:

  ```html
          <div class="design-stat-num {% if head_counts.awaiting_head_arka %}text-warning{% else %}text-muted{% endif %}">
            {{ head_counts.awaiting_head_arka }}</div>
  ```

- the shared colour branch that lumps `awaiting_head_arka` with the QC-side statuses,
  `_dashboard_design_chips.html:31` and `_design_status_chips.html:39`.

#### Notifications — **zero references**

```
$ grep -rni "arka" projects/notifications.py
(no output)

$ grep -rni "design" projects/notifications.py
(no output)
```

`projects/notifications.py` contains no reference to Arka, to any design status, or to
`DesignAssignment` at all. **No notification is sent at any Arka gate today.** Whatever is
built for these requests inherits no notification behaviour and breaks none.

#### `DueDateCommitment` — **zero references to any Arka status**

```
$ grep -rn "DueDateCommitment" --include=*.py projects/ | grep -v "/tests" | grep -v migrations
projects/admin.py:8:    DesignAssignment, DueDateCommitment, DesignAttempt, ArkaSubmission,
projects/admin.py:398:@admin.register(DueDateCommitment)
projects/design_analytics.py:81:    DesignChangeRequest, DueDateCommitment,
projects/design_analytics.py:378:        DueDateCommitment.objects.filter(assignment_id__in=assignment_ids)
projects/design_metrics.py:38:    DueDateCommitment, CHANGE_REQUEST_PENDING,
projects/design_metrics.py:262:        DueDateCommitment.objects.filter(assignment_id__in=assignment_ids)
projects/design_views.py:52:    Program, Project, UserProfile, BOQ, BOQItem, DesignAssignment, DueDateCommitment,
projects/design_views.py:730:    DueDateCommitment.objects.create(
projects/design_views.py:1098:        DueDateCommitment.objects.create(
projects/design_views.py:1199:            DueDateCommitment.objects.filter(pk=restored.pk).update(is_current=True)
projects/design_views.py:1281:        DueDateCommitment.objects.create(
```

The model itself, `projects/models.py:3290-3335`, has fields
`assignment / proposed_date / proposed_by / proposed_at / approved_by / approved_at /
change_reason / is_current` and **no status field and no reference to any Arka status**. Its
whole lifecycle sits before `in_design`. Nothing in Q1 touches it.

#### Dashboard counters

Design Head strip, `projects/design_views.py:3816-3828`:

```python
        'awaiting_allocation': base.filter(status=DESIGN_AWAITING_ALLOCATION).count(),
        # Gate 1's Arka queue — still keyed on the QC verdict, which is what `verdict`
        # now means. The status term alone would be enough; the verdict term is kept as
        # the same belt-and-braces re-check it has always been.
        'awaiting_arka':       base.filter(status=DESIGN_ARKA_SUBMITTED,
                                           attempts__arka_submissions__is_current=True,
                                           attempts__arka_submissions__verdict=ARKA_PENDING
                                           ).distinct().count(),
        # PART 9 — gate 2's two queues, counted separately from gate 1's. Merging them
        # would tell the Head how much work exists without telling him how much is his.
        'awaiting_head_arka':  base.filter(status=DESIGN_AWAITING_HEAD_ARKA).count(),
        'awaiting_head_qc':    base.filter(status=DESIGN_AWAITING_HEAD_QC).count(),
        'awaiting_qc':         base.filter(status=DESIGN_ARTIFACTS_UPLOADED).count(),
        'in_qc':               base.filter(status=DESIGN_IN_QC).count(),
```

Design QC strip, `projects/design_views.py:3886-3897`:

```python
    gate1_owed = base.filter(
        Q(status=DESIGN_ARKA_SUBMITTED,
          attempts__arka_submissions__is_current=True,
          attempts__arka_submissions__verdict=ARKA_PENDING)
        | Q(status__in=(DESIGN_ARTIFACTS_UPLOADED, DESIGN_IN_QC))
    ).distinct()
    counts = {
        'awaiting_arka': base.filter(status=DESIGN_ARKA_SUBMITTED,
                                     attempts__arka_submissions__is_current=True,
                                     attempts__arka_submissions__verdict=ARKA_PENDING
                                     ).distinct().count(),
```

Queue-age panels, `projects/design_metrics.py:637` and `:645`:

```python
    return _queue_age(sites, 'awaiting_head_arka', 'awaiting_head_qc', today)
...
    return _queue_age(sites, 'arka_submitted', 'artifacts_uploaded', today)
```

Attention-list clock, `projects/design_metrics.py:774-782`:

```python
    def _waiting_since(site):
        # Both Arka stages date from the submission itself. `awaiting_head_arka` is the
        # same physical Arka that `arka_submitted` was, handed on rather than resubmitted,
        # so its clock does not restart when Design QC passes it — the designer has been
        # waiting since they submitted, whoever currently holds it.
        if site['stage'] in ('arka_submitted', 'awaiting_head_arka') and site['arka'] is not None:
            return site['arka'].submitted_at
        return site['assignment'].updated_at
```

Queue-membership constants, `projects/design_metrics.py:200-206`:

```python
HEAD_ACTION_STAGES = ('awaiting_allocation', 'due_date_proposed',
                      'awaiting_head_arka', 'awaiting_head_qc')

# Stages where the ball is in DESIGN QC's court. Allocation and due dates are absent —
# those are the Head's alone and Part 9 does not move them.
QC_ACTION_STAGES = ('arka_submitted', 'artifacts_uploaded', 'in_qc')
```

Residential-side consumer, `projects/views.py:1754-1770` — the three Arka statuses are
deliberately **excluded** from the "design submitted" pill:

```python
# The three Arka statuses (arka_submitted / awaiting_head_arka / arka_rejected) are
# deliberately EXCLUDED. Arka is the earlier capacity-submission gate, not artifact
# submission; the two exist as separate statuses precisely so a dashboard can tell
# which one is holding a tender up (see the note at models.py:1781-1785).
TENDER_DESIGN_SUBMITTED_STATUSES = (
    DESIGN_ARTIFACTS_UPLOADED,
    DESIGN_IN_QC,
    DESIGN_AWAITING_HEAD_QC,
    DESIGN_QC_FAILED,
    DESIGN_RELEASED,
)
```

#### Tests (not asked for; listed so the blast radius is complete)

```
$ grep -rn "DESIGN_ARKA_SUBMITTED\|DESIGN_AWAITING_HEAD_ARKA" projects/tests_*.py
projects/tests_design_part11.py:39, :156
projects/tests_design_part46.py:38, :105
projects/tests_design_part8.py:44, :466, :504
projects/tests_design_part9.py:38, :40, :116, :145, :156, :166, :202, :231, :722, :854, :1123
```

`tests_design_part9.py:722` is the one that asserts the "artifacts outstanding" meaning of
`arka_submitted` directly:

```python
        self.assertEqual(self.a.status, DESIGN_ARKA_SUBMITTED)   # the "artifacts outstanding" state
```

---

### Q1.3 · Is CAD upload hard-gated on Arka approval, or only hidden by the UI?

**Hard-gated in code, at a single chokepoint. The UI hiding is a second, softer layer on
top.**

The chokepoint, `projects/design_views.py:1446-1477` — quoted in full:

```python
def _require_approved_arka(attempt):
    """Return the fully approved current Arka, or raise ValueError with the message the
    designer needs to see.

    Single chokepoint for settled decision 1. Every artifact write path calls this; none
    of them re-derives the rule, so the call sites cannot drift.

    The messages name WHICH gate is outstanding, because "waiting for approval" is not
    actionable when there are two approvers — the designer needs to know whether to chase
    Design QC or the Design Head.
    """
    if attempt is None:
        raise ValueError('no design attempt has been opened yet — submit an Arka first')
    arka = _current_arka(attempt)
    if arka is None:
        raise ValueError('no Arka has been submitted yet. CAD and BOQ can only be '
                         'uploaded against an approved Arka')
    if arka.verdict == ARKA_PENDING:
        raise ValueError(f'Arka v{arka.version} is still awaiting the Design QC review. '
                         f'CAD and BOQ cannot be uploaded until it is approved')
    if arka.verdict == ARKA_REJECTED:
        raise ValueError(f'Arka v{arka.version} was rejected at Design QC. Submit a new '
                         f'Arka version and have it approved before uploading CAD or BOQ')
    if arka.head_verdict == ARKA_PENDING:
        raise ValueError(f'Arka v{arka.version} passed Design QC but is still awaiting '
                         f'the Design Head\'s approval. CAD and BOQ cannot be uploaded '
                         f'until the Head has approved it')
    if arka.head_verdict == ARKA_REJECTED:
        raise ValueError(f'Arka v{arka.version} was rejected by the Design Head. Submit a '
                         f'new Arka version and have it approved before uploading CAD or BOQ')
    return arka
```

The predicate it wraps, `projects/design_views.py:1428-1444`:

```python
def _approved_arka(attempt):
    """The current Arka ONLY IF it has cleared BOTH gates. This is the object CAD and BOQ
    artifacts pair to — deliberately not "the most recently approved version", so a
    superseded approval can never become the parent of a new artifact.

    PART 9 MOVED THE TEST FROM `verdict` TO `head_verdict`, and that is the whole change
    to the Part 3 gate. `verdict='approved'` now means only that Design QC passed it; an
    Arka the Head has not yet approved is not a layout anybody may build against, so CAD
    and BOQ stay locked until the second gate clears. Because the serial rule guarantees
    head_verdict can only be 'approved' after verdict is, testing the Head field alone is
    sufficient and there is no need to test both.
    """
    arka = _current_arka(attempt)
    if arka is not None and arka.head_verdict == ARKA_APPROVED:
        return arka
    return None
```

**It has exactly two call sites, and both are write endpoints:**

```
$ grep -rn "_require_approved_arka" --include=*.py projects/ | grep -v "/tests"
projects/design_views.py:1376:# endpoint below calls _require_approved_arka() before it touches storage or the
projects/design_views.py:1446:def _require_approved_arka(attempt):
projects/design_views.py:2223:    PAIRING: derived_from_arka is set to the value _require_approved_arka() returns,
projects/design_views.py:2257:        arka = _require_approved_arka(attempt)
projects/design_views.py:2378:        _require_approved_arka(attempt)
projects/models.py:3529:    (design_views._require_approved_arka). An Arka that only Design QC has passed is not
```

CAD upload, `projects/design_views.py:2255-2259`:

```python
    attempt = _current_attempt(assignment)
    try:
        arka = _require_approved_arka(attempt)
    except ValueError as exc:
        return _back(f'{project.project_id}: {exc}.')
```

BOQ completion, `projects/design_views.py:2376-2380`:

```python
    attempt = _current_attempt(assignment)
    try:
        _require_approved_arka(attempt)
    except ValueError as exc:
        return _back(f'{project.project_id}: {exc}.')
```

The module states explicitly that this is view-enforced and not UI-enforced,
`projects/design_views.py:1371-1379`:

```python
# No CAD file and no BOQ submission may exist until the CURRENT Arka version has
# verdict='approved', and every artifact records WHICH Arka version it was drawn
# against (DesignFile.derived_from_arka, NOT NULL from Part 1).
#
# Both halves are enforced HERE, in the view, not by hiding a button: every write
# endpoint below calls _require_approved_arka() before it touches storage or the
# database, so a direct POST to a bare URL is refused exactly as a click would be.
# Verified by direct POST — see the session's verification run.
```

and again on the endpoint, `projects/design_views.py:2219-2221`:

```python
    REFUSED unless the current Arka is approved — settled decision 1, enforced here and
    not by the template. A direct POST to this URL with an unapproved Arka gets the same
    refusal a hidden button would have prevented.
```

and once more on the model, `projects/models.py:3527-3531`:

```python
    CAD AND BOQ UPLOAD NOW GATE ON head_verdict='approved', NOT verdict='approved'
    (design_views._require_approved_arka). An Arka that only Design QC has passed is not
    yet a layout anybody may build against.
```

Existing test coverage of the gate, `projects/tests_design_part9.py:234-235`:

```python
        from .design_views import _require_approved_arka, _current_attempt
        self.assertIsNotNone(_require_approved_arka(_current_attempt(self.a)))
```

**The UI layer, for completeness.** `site_workspace.html:159-167` hides the upload form and
says why:

```html
    {% if not arka_approved %}
      <div class="alert alert-warning py-2 small mb-0">
        CAD and BOQ files can only be uploaded once the current Arka is approved.
        Uploading earlier guarantees rework against a layout that may still change.
      </div>
    {% elif is_designer %}
      <form method="post" enctype="multipart/form-data"
            action="{% url 'design_artifact_upload' project_id=project.project_id %}"
            class="row g-2 align-items-end">
```

and `:130-138` names which of the two approvers is outstanding:

```html
    {% elif arka and arka.verdict == 'approved' and arka.head_verdict == 'pending' %}
      ...
      <div class="small text-muted">
        Arka v{{ arka.version }} ({{ arka.capacity_kw }} kW) passed Design QC and is now
        with the Design Head. CAD and BOQ unlock once they approve it.
      </div>
```

Removing the template guard alone would change nothing — the POST is still refused.

**There is no schema-level gate.** `DesignFile.derived_from_arka` is NOT NULL but carries no
condition on the target's verdict, and `DesignFile` has no `constraints` at all —
`projects/models.py:3722-3724` is:

```python
    class Meta:
        ordering        = ['attempt', 'kind', 'version']
        unique_together = ('attempt', 'kind', 'version')
```

The approval rule exists in exactly one Python function.

---

### Q1.4 · Plain statement: how big is this build?

**Combining Arka and CAD submission into one step is NOT a status change and NOT a model
change. The review sequence can stay identical. But it is not "only the upload form changes"
either: it is a deliberate reversal of one view-layer predicate plus its call sites.**

Taking each layer in turn.

**Model — no change needed.** `DesignFile.derived_from_arka` is NOT NULL, but the target may
legally be a `pending` Arka: nothing in the schema tests `verdict` or `head_verdict` (Q1.1,
Q1.3). A CAD uploaded in the same transaction as the Arka it derives from points at that
Arka's row, which is exactly what the pairing rule wants. No column, no constraint, no
migration.

**Status — no change needed.** No new `DesignAssignment.status` value is required. The
combined submit would write `DESIGN_ARKA_SUBMITTED` exactly as `design_arka_submit()` does at
`design_views.py:1894`, and the two gates run unchanged: QC approve moves to
`awaiting_head_arka` (`:2002`), Head approve moves back to `arka_submitted` (`:2122`). The
"Arka approved, artifacts incomplete" bucket keeps working because it is derived from
`head_verdict`, not stored (`design_metrics.py:214-222`).

**The progression rule already handles the reordering, and this is the part worth knowing.**
`_maybe_advance_to_artifacts_uploaded()` (`design_views.py:1596-1607`) requires three things —
an approved Arka, a current `cad_zip`, and `boq_submitted_at` — and does not care in what
order they arrived:

```python
    if assignment.status != DESIGN_ARKA_SUBMITTED:
        return False
    if attempt is None or _approved_arka(attempt) is None:
        return False
    if not attempt.design_files.filter(
            kind__in=PROGRESSION_CAD_KINDS, is_current=True).exists():
        return False
    if attempt.boq_submitted_at is None:
        return False

    assignment.status = DESIGN_ARTIFACTS_UPLOADED
```

and `design_arka_head_approve()` already calls it after stamping the approval,
`design_views.py:2130-2133`:

```python
        # A re-approval on an attempt that already carries CAD and a submitted BOQ would
        # otherwise leave the status behind; evaluating here costs one query.
        _maybe_advance_to_artifacts_uploaded(
            assignment, _current_attempt(assignment), profile)
```

So a site that arrives with CAD already attached advances to `artifacts_uploaded` the instant
the Head approves the Arka, with no new code. **The sequence "Arka reviewed and approved, CAD
validity assessed after" stays identical.**

**What actually has to change: `_require_approved_arka()`.** Today a combined form would call
`design_artifact_upload()` while `arka.verdict == ARKA_PENDING`, and be refused at
`design_views.py:2257` with *"Arka v1 is still awaiting the Design QC review"*. That refusal
is the feature as currently specified — settled decision 1 — so relaxing it for the combined
path is a deliberate reversal, not an incidental edit. It touches:

1. `_require_approved_arka()` itself (`design_views.py:1446`) — or a sibling predicate for the
   combined path, leaving the existing one intact for the piecemeal path.
2. `design_artifact_upload()` (`:2257`) — the CAD half.
3. `design_boq_complete()` (`:2378`) — **decide explicitly whether BOQ moves too.** The
   request as stated is about CAD; this is the other half of the same gate, and leaving it
   alone is a legitimate answer. Flagged rather than assumed.

**Two consequences that are not in the request and should be decided before building:**

- **A rejected Arka would leave a CAD attached to a dead layout.** Today a CAD can only exist
  against an Arka both gates approved, so "CAD drawn against a superseded layout" is
  structurally impossible — the stated reason for the pairing rule (`models.py:3646-3650`).
  If CAD arrives before the verdict, a gate-1 or gate-2 rejection leaves a `DesignFile`
  pointing at a rejected Arka. The rework scoping rule already anticipates this and does the
  right thing, `design_views.py:1570-1573`:

  ```python
      if REDO_ARKA in redo and REDO_CAD not in redo:
          return set(), ('a new Arka means new CAD too — every drawing records the Arka '
                         'version it was made from, and QC must never review a CAD drawn '
                         'against a layout that has been replaced')
  ```

  It holds, but it is currently unreachable at the Arka stage and would become reachable.

- **The designer-facing next-action resolver** (`design_views.py:3736-3748`) tells a designer
  at `arka_submitted` + head-approved to "Upload CAD". With a combined submit the CAD is
  already there, so the resolver falls through to "Enter BOQ" or "Package complete" on its
  own. It reads correctly as written; worth a look, not a rewrite.

**Size verdict:** one predicate, two call sites, one form, one template panel, and a decision
about rejection handling. No migration. No new status. No model field.

---

## Q2 — BOQ correction

### Q2.1 · Current permission / view logic for editing a `BOQItem` quantity

There are **two** authoring screens; they share one permission helper and two locks.

#### The permission helper — `projects/permissions.py:350-397`

```python
def user_can_edit_project_boq(user, project):
    """
    Return True if `user` may WRITE `project`'s BOQ — quantities, make preference, ad-hoc
    rows, submission, and the auto-create-and-seed that boq_detail performs on GET.

    BOQ authorship belongs to the Design role and to nobody else:

        Design AND (assigned_design on this project OR holds a task on it)

    Everyone else is excluded ON PURPOSE, and each for its own reason:
      ...
      * Design Head — portfolio-wide READ only. The flag confers no approval or authorship
        authority anywhere in the product today; granting write here would invent some.
      ...
    WRITE RULE: this is W-narrow — `assigned_design` on THIS project, and nothing else.
    """
    if project is None:
        return False
    profile = getattr(user, 'profile', None)
    if profile is None:
        return False

    if profile.role != 'Design':
        return False

    return project.assigned_design_id == profile.pk   # W-narrow — no task-holding fallback
```

Read is wider and explicitly write-free, `projects/permissions.py:243-249` and `:295-298`:

```python
        PM / coordinator     — user_can_manage_project(), the one canonical path
        SCM / Admin / CEO    — portfolio-wide by remit (BOQ_PORTFOLIO_READ_ROLES)
        Design Head          — portfolio-wide read, no write (see below)
        Design Head's deputy — portfolio-wide read, no write (Part 6.5b, closes G4)
        Design QC            — portfolio-wide read, no write (Part 9, closes G4 again)
        Design               — assigned_design on this project, OR holds a task on it
...
    READ ONLY, DELIBERATELY. user_can_edit_project_boq() is NOT modified and admits
    neither a deputy nor Design QC. A reviewer reads a BOQ; they do not author one.
    W-narrow stands.
```

#### Screen 1 — Residential authoring / everyone-else read screen, `views.boq_detail`

Gate assembled at `projects/views.py:5694-5696`:

```python
        _can_edit = (user_can_edit_project_boq(request.user, project)
                     and not boq_group_locked
                     and not boq_design_locked)
```

Quantity write branch, `projects/views.py:5720-5731`:

```python
        if action in ('save_design', 'submit_design') and _can_edit and boq.status in _DESIGN_EDITABLE:
            boq.notes = request.POST.get('notes', '').strip() or None
            boq.save(update_fields=['notes'])
            for item in boq.items.all():
                qty_str    = request.POST.get(f'boq_qty_{item.pk}', '').strip()
                vendor_str = request.POST.get(f'make_pref_{item.pk}', '').strip()
                try:
                    item.boq_quantity = Decimal(qty_str) if qty_str else None
                except InvalidOperation:
                    item.boq_quantity = None
                item.make_preference_id = int(vendor_str) if vendor_str.isdigit() else None
                item.save(update_fields=['boq_quantity', 'make_preference'])
```

with the status window at `projects/views.py:5677`:

```python
        _DESIGN_EDITABLE = ('Draft', 'Revision Requested', 'Acknowledged')
```

SCM writes a *different* column and is deliberately outside all of this,
`projects/views.py:5776-5788` — `ordered_quantity`, `make_preference`, `ordered_vendor`,
gated on `role == 'SCM'` alone.

#### Screen 2 — OPEX picker, `views.opex_boq_entry`

Gate at `projects/views.py:5953-5956`:

```python
    boq_group_locked  = project_boq_is_group_locked(project)
    boq_design_locked = project_boq_is_design_locked(project)
    can_author        = user_can_edit_project_boq(request.user, project)
    can_edit          = can_author and not boq_group_locked and not boq_design_locked
```

POST refusals at `projects/views.py:5969-5984`, then the quantity write at
`projects/views.py:6060-6076`.

#### "BOQ upload never deletes rows" — **enforced at the VIEW, not the model**

Model level: **nothing.** `BOQItem` (`projects/models.py:1061-1116`) has no `save()` and no
`delete()` override — the only method on it is `__str__`:

```
$ awk 'NR>=1061 && NR<=1117' projects/models.py | grep -n "def "
54:    def __str__(self):
```

There is no `CheckConstraint`, no `Meta.constraints`, and no signal. `BOQItem.Meta` is
`ordering = ['serial_no']` and nothing more (`projects/models.py:1111-1112`).

View level: the rule is a property of *one* function — `_boq_upload_apply()` simply contains
no delete branch, `projects/views.py:6860-6871`:

```python
def _boq_upload_apply(project, clean):
    """Apply the validated rows. Returns (boq, added, changed, readded).

    THE PICKER'S PERSISTENCE BLOCK MINUS ITS TWO DELETE LOOPS (see opex_boq_entry).
    The lazy BOQ create, split_opex_boq_rows(), the by_master index and the
    create/update loop's field mapping are all the picker's, so a row this writes
    is indistinguishable from a row the picker writes. What is absent is absent on
    purpose: there is no branch here that can delete a BOQItem.

    OFF-CATALOGUE ROWS ARE NOT EVEN READ. The file never carries them — they live
    on their own sheet, which the parser skips by name — so they cannot be changed
    and cannot be removed by any file.
    """
```

and the blank-cell rule inside it, `projects/views.py:6903-6909`:

```python
            elif entry['quantity'] is not None and entry['quantity'] != row.boq_quantity:
                # A BLANK CELL FALLS THROUGH THIS BRANCH UNTOUCHED. That is decision
                # 5, and it is what makes a mis-filtered or partly-filled file
                # incapable of destroying a quantity.
                row.boq_quantity = entry['quantity']
                row.save(update_fields=['boq_quantity'])
```

**The rule is scoped to the upload path only.** The other two authoring paths DO delete:

- Picker reconciliation, `projects/views.py:6047-6053`:

  ```python
            # Removed catalogue rows, and removed off-catalogue rows.
            for master_id, row in by_master.items():
                if master_id not in chosen_set:
                    row.delete()
            for row in existing_off:
                if row.pk not in keep_off:
                    row.delete()
  ```

  documented as intended at `projects/views.py:5937-5941`:

  ```python
    THE SAVE IS A FULL RECONCILIATION of the posted sheet against the stored one, not an
    append. Rows the designer removed are gone from the POST, so they are deleted here;
    rows they added arrive as catalogue pks and are created.
  ```

- `boq_detail` `delete_item`, `projects/views.py:5820-5829` — non-standard rows only:

  ```python
        elif action == 'delete_item' and _can_edit and boq.status in _DESIGN_EDITABLE:
            item_id = request.POST.get('item_id', '')
            if item_id.isdigit():
                # Object consistency: the item must belong to THIS project's BOQ. An id from
                # another project's BOQ is a 404, not a silent no-op reported as success.
                item = get_object_or_404(BOQItem, pk=int(item_id), boq=boq)
                if not item.is_standard_item:     # standard rows are undeletable, as before
                    item.delete()
  ```

**Would a new QC/Head edit path need to respect the same rule?** The "never deletes" rule is
a property of the *spreadsheet upload*, and an edit path that writes `boq_quantity` on
existing rows satisfies it trivially — there is no deletion to suppress. What a QC/Head edit
path **would** collide with is the design lock, and that is the finding:

`projects/permissions.py:905-947`, `project_boq_is_design_locked()`:

```python
    THE CONDITION IS ONE FIELD: the CURRENT attempt's `boq_submitted_at`. That single test
    produces every row of the Part 11 lock progression, because the Part 9 rework loop
    already maintains that stamp exactly as the progression describes:

        designer saving drafts        stamp null      -> editable
        marks BOQ complete            stamp set       -> frozen
        Design QC rejects             new attempt     -> reopens (see below)
        Design QC approves            same attempt    -> stays frozen
        Design Head rejects           new attempt     -> reopens
        Design Head approves          same attempt    -> DESIGN LOCK
        PM change request             new attempt     -> reopens
```

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

**The lock is True in exactly the window in which a QC or Head correction would happen.**
`boq_submitted_at` is stamped the moment the designer marks the BOQ complete, which is the
moment the reviewer receives it. So a QC/Head quantity-correction path is refused by
construction unless it is written to bypass `project_boq_is_design_locked` — and both
existing screens refuse before reaching any write, `projects/views.py:5712-5718` and
`projects/views.py:5979-5984`:

```python
        if boq_design_locked and action in ('save_design', 'submit_design',
                                            'add_item', 'delete_item'):
            messages.error(request, 'This BOQ has been marked complete and is with design '
                                    'review — it cannot be changed until a reviewer sends '
                                    'it back or a change request opens a new attempt.')
            return redirect('boq_detail', project_id=project_id)
```

**And separately from the lock, the reviewer fails the permission helper anyway.** A Design
QC reviewer is by construction not the site's `assigned_design` — `projects/permissions.py:288-291`:

```python
    It has to go HERE for the same reason the deputy branch did: this WIDENS, and no
    caller can widen a gate that has already returned False. A Design QC reviewer is a
    plain `role='Design'` user who is by construction NOT the site's `assigned_design` —
    the assigned designer is the one person forbidden from reviewing it — so the Design
    branch below refuses them every time.
```

So request #2 needs **two** deliberate widenings, not one: `user_can_edit_project_boq()` (or a
sibling), and an exemption from `project_boq_is_design_locked`. Neither is a bug; both are
stated decisions with written rationale, so both are reversals to be made on purpose.

Today's sanctioned route for a correction is the reopen, not an edit —
`projects/permissions.py:923-930`:

```python
    REOPENING IS TOTAL, NOT QUANTITY-ONLY. Nothing here is per-row: when the stamp clears,
    the whole entry screen comes back with its picker, so the designer can add an item that
    was never on the sheet. That is the point — 14 of the 16 error categories map to a redo
    set containing REDO_BOQ, and the two BOQ-specific ones (`boq_quantity`,
    `boq_specification`) are precisely the failures that may need a NEW line rather than a
    corrected number. Restoring only the quantity fields would leave the designer unable to
    fix the thing they were failed for.
```

---

### Q2.2 · Does `BOQItem` or `BOQ` carry an `updated_by` / `updated_at` pair?

**No. Neither model has one, and there is no `auto_now` field on either.**

`projects/models.py:1033-1056` — the whole of `BOQ`:

```python
class BOQ(models.Model):
    """Bill of Quantities for a project. One BOQ per project (OneToOne)."""

    # Workflow: Draft → Submitted (by Design) → Acknowledged (by SCM) or Revision Requested (by PM)
    STATUS_CHOICES = [
        ('Draft',              'Draft'),
        ('Submitted',          'Submitted'),
        ('Acknowledged',       'Acknowledged'),
        ('Revision Requested', 'Revision Requested'),
    ]

    project      = models.OneToOneField(Project, on_delete=models.CASCADE, related_name='boq')
    submitted_by = models.ForeignKey(
        'UserProfile',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='submitted_boqs',
    )
    submitted_at = models.DateTimeField(null=True, blank=True)
    status       = models.CharField(max_length=20, choices=STATUS_CHOICES, default='Draft')
    version      = models.IntegerField(default=1)  # Increments on each resubmission after revision
    notes        = models.TextField(null=True, blank=True)
```

`submitted_by` / `submitted_at` are a **submission** pair, not an edit pair: they are written
only in the submit branch (`views.py:5758-5760`) and say who last handed the sheet to SCM. A
plain quantity correction that did not resubmit would leave them untouched, and one that did
would overwrite the submission record with an edit — neither is what an edit trail means.

`projects/models.py:1061-1116` — the whole of `BOQItem` carries **no** actor field and **no**
timestamp at all. Its fields are `boq`, `serial_no`, `category`, `description`, `item_master`,
`make_preference`, `uom`, `boq_quantity`, `ordered_quantity`, `ordered_vendor`,
`is_standard_item`.

**What DOES exist and is usable without a new column:**

1. **`BOQRevision`** — `projects/models.py:1118-1132`, the closest thing to an edit record:

   ```python
   class BOQRevision(models.Model):
       """Immutable snapshot of a BOQ at each workflow transition (submit, acknowledge, revision)."""

       boq        = models.ForeignKey(BOQ, on_delete=models.CASCADE, related_name='revisions')
       revised_by = models.ForeignKey('UserProfile', on_delete=models.SET_NULL, null=True)
       revised_at = models.DateTimeField(auto_now_add=True)
       version    = models.IntegerField()
       reason     = models.TextField()
       snapshot   = models.JSONField()  # Full item list serialised at transition time; Decimal fields coerced to float
   ```

   It has `revised_by` + `revised_at` + a free-text `reason` + a full item snapshot. A
   correction could write one row here with `reason='QC correction'` and the sheet's state
   after the edit; the before-state is recoverable by diffing against the previous revision.
   **This needs no migration.** Its limitation is that it is whole-sheet, not per-row: it
   will not say "item 14 went from 8 to 6" without a diff. Written today at
   `views.py:5752-5755` (submit) and `views.py:5793-5798` (acknowledge).

2. **`StatusTransition`** — `projects/models.py:1682-1773`. BOQ is already an instrumented
   subject type (`SUBJECT_BOQ` in `SUBJECT_TYPE_CHOICES`, `models.py:1631`) and `boq_detail`
   already calls `record_transition(boq, ...)` at `views.py:5765-5771`. It is a **status**
   ledger, though — `to_status` is required and `record_transition()` raises without it
   (`utils.py:495-497`) — so a quantity correction that changes no status has nothing to put
   in it. It is also append-only by construction (`models.py:1766-1772`).

3. **`ActivityLog`** — `projects/models.py:1275-1298`, with `action_code`, free-text detail,
   and `entity_type` / `entity_id`. The human-readable feed the design module writes to.

4. **`ProjectFieldEditLog`** — `projects/models.py:595-632`. **Not usable**: its
   `FIELD_CHOICES` is a closed set of three `Project` fields (`capacity_kw`,
   `contract_value`, `target_commissioning_date`) and its FK is to `Project`, not to a BOQ
   row. Extending it to BOQ is a schema change.

**Answer in one line:** no `updated_by`/`updated_at` pair exists; `BOQRevision` is the audit
field set that can carry a plain edit record with no new column, at whole-sheet granularity.

---

### Q2.3 · Is `BOQ.status` a separate field from `DesignAssignment.status`?

**Separate. Different models, different columns, different enums, no shared constant, and no
code path that reads one as the other.**

`BOQ.status` — `projects/models.py:1036-1050`:

```python
    STATUS_CHOICES = [
        ('Draft',              'Draft'),
        ('Submitted',          'Submitted'),
        ('Acknowledged',       'Acknowledged'),
        ('Revision Requested', 'Revision Requested'),
    ]
    ...
    status       = models.CharField(max_length=20, choices=STATUS_CHOICES, default='Draft')
```

`DesignAssignment.status` — the fourteen-value enum at `projects/models.py:2857-2874`
(`awaiting_survey`, `awaiting_allocation`, `allocated`, `due_date_proposed`, `in_design`,
`arka_submitted`, `awaiting_head_arka`, `arka_rejected`, `artifacts_uploaded`, `in_qc`,
`awaiting_head_qc`, `qc_failed`, `released`, `survey_returned`).

No overlap in values, no shared choices list. The design module says so explicitly,
`projects/models.py:3450-3453`:

```python
    # The design workflow RECORDS that a BOQ was submitted; it does not duplicate BOQ
    # data (settled decision 7). The BOQ / BOQItem / BOQItemMaster models are used
    # as-is and are not touched by this module.
```

**What this means for the dependency question — and it is not a clean "no".**

Through the *status field*: independent. Nothing in the consolidation/hook work changes
`BOQ.status`, and nothing reads `DesignAssignment.status` to decide whether a BOQ is
editable.

Through the *design review loop*: coupled, by exactly one field. BOQ editability is decided
by `project_boq_is_design_locked()`, which reads `DesignAttempt.boq_submitted_at` — **not**
`DesignAssignment.status` and **not** `BOQ.status` (paste in Q2.1). And `boq_submitted_at` is
written by `design_boq_complete()` (`design_views.py:2347`) and carried forward or dropped by
`_carry_forward_artifacts()` when a new attempt opens (`design_views.py:2604`).

So: **request #2 does not depend on the Arka/CAD consolidation through any status enum, but
it does share one field with the design review loop — `DesignAttempt.boq_submitted_at` — and
that is the field the lock in Q2.1 reads.** If the consolidation changes when
`boq_submitted_at` is stamped (see the Q1.4 note about `design_boq_complete()` being the
second `_require_approved_arka` call site), it changes when the BOQ locks, and therefore when
a correction path is refused. That is the whole of the coupling, and it is one field wide.

---

### Q2.4 · Does "correction" mean quantities only, or adding/removing lines?

**The request as given is ambiguous, and I am flagging it rather than choosing.** Nothing in
the repository records the Design Head's own wording, so there is no source to disambiguate
against:

```
$ grep -rn -i "Design Head request" docs/*.md *.md
(no output)
```

The three readings are materially different builds:

| reading | what it touches | new surface |
|---|---|---|
| **A. Quantity only** | `BOQItem.boq_quantity` on existing rows | reuse either existing screen, read-only plus one editable column |
| **B. Quantity + add lines** | plus `BOQItem.objects.create(...)` from the catalogue | needs the OPEX picker, or the Residential `add_item` branch |
| **C. Quantity + add + remove lines** | plus `row.delete()` | collides with the delete rules in Q2.1 and destroys history |

Three things make this worth settling before any build, not during:

1. **The codebase already treats "correction" as reading on a spectrum.** The lock docstring
   at `permissions.py:923-930` argues that a BOQ reopen must be *total* rather than
   quantity-only, precisely because two of the sixteen error categories (`boq_quantity`,
   `boq_specification`) "are precisely the failures that may need a NEW line rather than a
   corrected number". A quantity-only edit path would be, by that document's own reasoning,
   insufficient for the failures it exists to fix.

2. **Reading C has no undo.** There is no soft-delete on `BOQItem` and no per-row history —
   `BOQRevision` snapshots the sheet at *workflow transitions only*, so a row deleted and
   re-added between two transitions leaves no trace anywhere. Removal by a reviewer is the
   one variant that can lose data silently.

3. **The existing route already covers B and C** — a reviewer failing the package reopens the
   whole entry screen for the designer (Q2.1). If the request is really "let the Head fix a
   number without bouncing the package", that is reading A and it is small. If it is "let the
   Head restructure the bill", the reopen already does it, and the request may be about
   *speed* rather than *capability*.

**Question for the Design Head, in his terms:** *when you say correction, do you mean changing
a number the designer already entered, or also adding an item they left out and removing one
that does not belong?* The answer decides between a one-column edit and a second authoring
screen.

---

## Summary

| # | question | answer |
|---|---|---|
| 1.1 | Arka/CAD linkage | Real FK. `DesignFile.derived_from_arka -> ArkaSubmission`, NOT NULL, `PROTECT`; the version is `ArkaSubmission.version`, unique per attempt. Not descriptive. No constraint on the target's verdict. |
| 1.2 | Status references | `arka_submitted`: 17 `.py` hits (15 outside `models.py`). `awaiting_head_arka`: 21 `.py` hits. Derived `arka_approved`: 12 hits. Nine template logic branches in four files, plus one counter render. **0 in `notifications.py`. 0 on `DueDateCommitment`.** Six dashboard counters, two queue-age helpers, two stage-membership constants. |
| 1.3 | CAD gate | Hard-gated in code at `design_views._require_approved_arka` (`:1446`), called from `:2257` and `:2378`. Template hiding is a second layer. No schema-level gate. |
| 1.4 | Size of the build | **Not a status or model change.** One predicate, two call sites, the form, one template panel. No migration. The review sequence stays identical — but it is not "only the form changes": settled decision 1 has to be deliberately reversed. Decide rejection-handling for a CAD attached to a not-yet-approved Arka, and whether BOQ moves with CAD. |
| 2.1 | BOQ edit logic | `user_can_edit_project_boq` (Design role + `assigned_design` on this project) AND not group-locked AND not design-locked. "Never deletes" is **view-level only**, and scoped to the upload path — the picker and `delete_item` both delete. A QC/Head path needs **two** deliberate widenings: the permission helper, and an exemption from `project_boq_is_design_locked`, which is True in exactly the review window. |
| 2.2 | Audit fields | No `updated_by`/`updated_at` on `BOQ` or `BOQItem`; `BOQItem` has no timestamp at all. `BOQRevision` (`revised_by`/`revised_at`/`reason`/`snapshot`) can carry an edit record with no new column, at whole-sheet granularity. |
| 2.3 | Status coupling | Separate fields, separate enums, no shared constant. Independent of the consolidation through status — **coupled through one field, `DesignAttempt.boq_submitted_at`**, which is what the BOQ design lock reads. |
| 2.4 | "Correction" scope | **Ambiguous — flagged, not assumed.** Quantity-only vs. add-lines vs. add-and-remove are three different builds; the third has no undo. |

## MODE compliance

Files written this session: `docs/DESIGN_HEAD_REQUESTS_AUDIT.md` (this file), new.
No other file was created, modified or deleted. No question required a code change to
answer; none was made.
