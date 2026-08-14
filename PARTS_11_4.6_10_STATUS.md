# Parts 11, 4.6 and 10 — Implementation Status Audit

Audit session, 13 August 2026. Findings only; no code was written, changed or run against
production. Every claim below is evidence from the local working tree at HEAD, the local
Postgres database, or the Railway deployment record.

---

## SESSION OPENING CHECK

### a. Local repo

`c:\SolarPMS` is **not** a git repository. The repository is the subdirectory
`c:\SolarPMS\Horizon-Solar-PMS`; all git commands below were run there.

```
$ git status
On branch main
Your branch is up to date with 'origin/main'.

nothing to commit, working tree clean
```

**The working tree was clean at session start.** Nothing uncommitted, nothing untracked.

```
$ git log --oneline -10
2792f2c Fix survey-link comment rendering onto the Actions cell
3ce77ef Survey folder link as a second route to the allocation gate
c7c583d Consolidate the survey gate behind two DesignAssignment properties
273ff94 Add SECONDARY_FINDINGS.md — incidental issues log
b43e401 OPEX sites: site_code IS the project ID; contact fields optional
ff29f28 Part 10: the Design Head picks the metrics, but not the ones that measure the process
6ac23b9 Part 4.6: a PM change request goes to the Design Head, not straight into the queue
cc387ee Part 11: OPEX designers pick from their own catalogue, and the sheet locks when review starts
4642e4d Part 9.1: a failure sends back only what actually failed
907273b Design QC can open the CAD and the BOQ it is asked to judge
```

All three parts are present as single commits on `main`:

| Part | Commit | Date | Files changed |
|---|---|---|---|
| 11 | `cc387ee` | 2 Aug 2026 | 15 files, +2449 / −28 |
| 4.6 | `6ac23b9` | 2 Aug 2026 | 15 files, +1513 / −101 |
| 10 | `ff29f28` | 2 Aug 2026 | 11 files, +3194 / −0 |

There is also a second local branch, `fix/project-id-collision` at `c6cc2ce`
("EOD digest: role exclusions, coordinator content, open-work gating"), tracking
`origin/fix/project-id-collision`. It is unrelated to Parts 11 / 4.6 / 10 and is pushed,
not stranded.

### b. Deployed SHA

Railway project `triumphant-forgiveness`, service `Horizon-Solar-PMS`, environment
`production`:

```
2b4cc0e6-607f-428e-bbaf-7832fd739a8c | SUCCESS  | 2026-08-06 02:09:50 UTC | 2792f2c8c96d5fa635de4b756797273d68a084c7
ec0759f3-8b5f-49b7-8395-997c6c7277e2 | REMOVED  | 2026-08-05 13:08:53 UTC | b43e4017...
b1441886-88a1-4d9d-9792-698a325bd51d | REMOVED  | 2026-08-02 06:31:32 UTC | ff29f284...   <- Part 10
1782a444-7992-4bdc-9e2e-ff1f119b97b8 | REMOVED  | 2026-08-02 01:51:17 UTC | 6ac23b94...   <- Part 4.6
84f46a46-5f32-4e69-af1a-02d3ce9aa73d | REMOVED  | 2026-08-01 19:58:03 UTC | cc387eec...   <- Part 11
```

- **Local HEAD:** `2792f2c`
- **Deployed SHA:** `2792f2c8c96d5fa635de4b756797273d68a084c7`

**They match.** The current live deployment is local HEAD. Each of the three parts also had
its own successful deployment at the time it was committed, so none of them reached
production only as part of a later bundle.

---

## PART 11 — OPEX BOQ Item Master and Picker Entry

# **DONE**

Every verifiable claim in items 1–12 checks out. One caveat that is not an implementation
gap: Part 11's own 32-test suite does not currently execute — all 32 tests error in `setUp`
on a pre-existing collision with migration 0047 (deferred finding P2; see item 31). The
findings below are therefore evidenced from the code and from live queries against the local
database, not from a passing suite.

### Schema

**1. `BOQItemMaster.project_type` — present.** `projects/models.py:688-693`:

```python
project_type = models.CharField(
    max_length=20,
    choices=Project.PROJECT_TYPE_CHOICES,
    default='Residential',
    db_index=True,
)
```

**2. Catalogue counts.** The prompt's query aggregates both project types together; run as
written it returns figures that do not match the prompt's own expectations. Split by
`project_type` it matches exactly. Raw output of the prompt's query:

```
total: 244
Counter({'OPEX': 207, 'Residential': 37})
Counter({'BOS': 50, 'AC Cable': 39, 'Conduit': 33, 'Ring Type Lug': 25, 'ACDB': 20,
         'Inverter': 18, 'MMS': 16, 'Earthing': 10, 'DCDB': 6, 'Solar Meter + CT': 6,
         'Cable Tray': 5, 'Pin Type Lug': 4, 'DC Cable': 3, 'Data Logger+ WMS': 3,
         'Solar Modules': 2, 'Structure': 2, 'Module': 1, 'Civil': 1})
units: Counter({'Nos': 141, 'Meter': 64, 'Pkt': 15, 'Set': 8, 'Mtr': 6, 'KWp': 5,
                'LOT': 2, 'LS': 1, 'Pair': 1, 'Kg': 1})
```

Totals: **37 Residential / 207 OPEX — exactly as expected.**

Split by type, the OPEX side is exact on all 16 categories:

```
--- OPEX 207
 cats:  {'AC Cable': 39, 'ACDB': 20, 'BOS': 20, 'Cable Tray': 5, 'Civil': 1,
         'Conduit': 33, 'DC Cable': 3, 'DCDB': 6, 'Data Logger+ WMS': 3,
         'Earthing': 10, 'Inverter': 15, 'MMS': 16, 'Module': 1,
         'Pin Type Lug': 4, 'Ring Type Lug': 25, 'Solar Meter + CT': 6}
 units: {'KWp': 5, 'Kg': 1, 'Meter': 64, 'Nos': 115, 'Pair': 1, 'Pkt': 13, 'Set': 8}

--- Residential 37
 cats:  {'BOS': 30, 'Inverter': 3, 'Solar Modules': 2, 'Structure': 2}
 units: {'LOT': 2, 'LS': 1, 'Mtr': 6, 'Nos': 26, 'Pkt': 2}
```

Every one of the prompt's 16 expected category counts is matched on the OPEX side, and they
sum to 207. OPEX units are limited to exactly the seven permitted spellings — `Nos`,
`Meter`, `Pkt`, `Set`, `KWp`, `Pair`, `Kg`.

The apparent discrepancies in the combined figures are entirely the 37 pre-existing
Residential rows: `BOS` 50 = 20 OPEX + 30 Residential, `Inverter` 18 = 15 + 3, plus
`Solar Modules` 2 and `Structure` 2 which are Residential-only categories (30+3+2+2 = 37).
The out-of-vocabulary units `Mtr` (6), `LOT` (2) and `LS` (1) are likewise Residential rows,
untouched by Part 11 by design. See **CONFLICTS**.

**3. The four duplicate descriptions are present as 8 distinct rows.** Confirmed:

```
OPX-101 | Pin Type Lug  | '4Sqmm*Cu'  | Nos | OPEX
OPX-102 | Pin Type Lug  | '6Sqmm*Cu'  | Nos | OPEX
OPX-103 | Pin Type Lug  | '10Sqmm*Cu' | Nos | OPEX
OPX-104 | Pin Type Lug  | '16Sqmm*Cu' | Nos | OPEX
OPX-105 | Ring Type Lug | '4Sqmm*Cu'  | Nos | OPEX
OPX-106 | Ring Type Lug | '6Sqmm*Cu'  | Nos | OPEX
OPX-107 | Ring Type Lug | '10Sqmm*Cu' | Nos | OPEX
OPX-108 | Ring Type Lug | '16Sqmm*Cu' | Nos | OPEX
```

A duplicate-description scan over the OPEX catalogue returns exactly these four strings and
no others: `['4Sqmm*Cu', '6Sqmm*Cu', '10Sqmm*Cu', '16Sqmm*Cu']`.

**4. `get_standard_boq_items()` filters to Residential only.** `projects/models.py:729-733`:

```python
rows = list(
    BOQItemMaster.objects
    .filter(is_active=True, project_type='Residential')
    .order_by('sort_order', 'code')
    .values('sort_order', 'category', 'description', 'unit')
)
```

The OPEX side reads through a separate function, `get_opex_boq_catalogue()`
(`projects/models.py:751-770`), which returns model instances rather than dicts because the
picker records `BOQItem.item_master` by pk.

### Entry screen

**5. The picker exists.**

- URL: `projects/urls.py:188` — `projects/<str:project_id>/boq/entry/` → `opex_boq_entry`
- View: `projects/views.py:4592` — `opex_boq_entry()`
- Template: `projects/templates/projects/opex_boq_entry.html` (446 lines)

It 404s on a Residential site (`views.py:4608-4610`). All four required behaviours are
present:

- **Search box** — `opex_boq_entry.html:93-94`, `id="boqSearch"`, placeholder
  "Search: inverter, 6sqmm, ACDB…". The filter matches description, category **and** code
  (`renderResults`, lines 269-275).
- **Category filter** — `elCat` select, applied at line 270.
- **Add / remove** — search results render a `+` affordance per row and carry no quantity
  input (`renderResults`, lines 277-284); added rows carry a `×` remove button
  (`renderSheet`, lines 341-344).
- **Quantities on added items only** — the `<input class="boq-qty">` is emitted only in
  `renderSheet` (lines 332-339), i.e. only for rows already on the sheet.

The POST is a full reconciliation of the posted sheet against the stored one, not an append
(`views.py:4684-4722`): removed rows are deleted, added rows created, and `serial_no` comes
from the catalogue's `sort_order` so a row's number is stable regardless of add order.

**6. Per-category "not yet added" indicator — present.**
`opex_boq_entry.html:104-118` renders a panel headed *"Categories with nothing added yet —
check before marking complete"*, into `<div id="boqEmptyCats">`. It is re-rendered
client-side by `renderEmptyCats()` (lines 353-366) as items are added, so it tracks the
unsaved draft as well as the saved sheet, and collapses to *"Every category has at least one
item"* when none are empty.

**7. Residential BOQ entry still pre-populates all 37 rows, unchanged.**
`projects/views.py:4310-4336`. The redirect that sends the OPEX author to the picker sits at
line 4301, *before* the seeding block:

```python
if project.project_type == 'OPEX' and user_can_edit_project_boq(request.user, project):
    return redirect('opex_boq_entry', project_id=project_id)
```

and the seeding itself is unchanged apart from a `project_type` term on the lookup dict:

```python
masters = {
    m.description: m
    for m in BOQItemMaster.objects.filter(is_active=True,
                                          project_type='Residential')
}
BOQItem.objects.bulk_create([
    BOQItem(boq=boq, item_master=masters.get(item_data['description']), **item_data)
    for item_data in get_standard_boq_items()
])
```

That term is load-bearing, not cosmetic: three descriptions exist in both catalogues
(`PVC Elbow 25MM` ITM-015/OPX-131, `PVC Tee 25MM` ITM-016/OPX-132, `Silver Spray Paint`
ITM-024/OPX-193). Unscoped, the OPEX row would win the description key and a Residential BOQ
line would silently carry an `item_master` pointing into the OPEX catalogue — which is the
join Part 6 aggregation runs on.

Note that only the OPEX **author** is redirected. SCM, PM, Admin and the two reviewers still
read an OPEX BOQ on `boq_detail`.

### Locking

**8. Marking BOQ complete freezes it for the designer.** The enforcement point is
`project_boq_is_design_locked()`, `projects/permissions.py:689-742`:

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

It is ANDed at every caller rather than folded into `user_can_edit_project_boq()`:

- `views.py:4620` (picker) — `can_edit = can_author and not boq_group_locked and not boq_design_locked`
- `views.py:4641-4645` — the picker POST refuses with a message when design-locked
- `views.py:4295` — computed once in `boq_detail` and applied to all three Design write
  branches, so a hand-crafted POST to the older endpoint is not a way around it

**9. A QC or Head rejection restores the FULL picker, not just quantity fields.** Confirmed.

The lock is a single predicate on the *current* attempt's `boq_submitted_at`. A rejection
opens attempt N+1 via `_open_next_attempt()`, and the new attempt's `boq_submitted_at` is
null unless explicitly carried forward. Nothing in the lock or the picker is per-row: when
the stamp clears, `can_edit` becomes True and `opex_boq_entry.html` renders its search panel
and add buttons in full, so the designer can add an item that was never on the sheet.

The one place the stamp survives a rejection is `_carry_forward_artifacts()`,
`design_views.py:2239-2243`:

```python
if REDO_BOQ not in redo and old_attempt.boq_submitted_at is not None:
    new_attempt.boq_submitted_at = old_attempt.boq_submitted_at
    new_attempt.boq_submitted_by = old_attempt.boq_submitted_by
    new_attempt.save(update_fields=['boq_submitted_at', 'boq_submitted_by'])
```

i.e. the BOQ stays frozen only when the reviewer explicitly did **not** ask for BOQ rework
(Part 9.1 scoping — only `drawing_incomplete` does that). The docstring at
`permissions.py:713-725` states the rule and the reason:

> REOPENING IS TOTAL, NOT QUANTITY-ONLY. Nothing here is per-row: when the stamp clears, the
> whole entry screen comes back with its picker, so the designer can add an item that was
> never on the sheet. […] Restoring only the quantity fields would leave the designer unable
> to fix the thing they were failed for.

**10. Design Head approval applies a design lock.** Head approval does not open a new
attempt, so `boq_submitted_at` on the current attempt stays set and the predicate keeps
returning True. The progression is documented at `permissions.py:705-711`:

```
designer saving drafts        stamp null      -> editable
marks BOQ complete            stamp set       -> frozen
Design QC rejects             new attempt     -> reopens
Design QC approves            same attempt    -> stays frozen
Design Head rejects           new attempt     -> reopens
Design Head approves          same attempt    -> DESIGN LOCK
PM change request             new attempt     -> reopens
```

**11. `user_can_edit_project_boq()` was NOT modified.** Current body,
`projects/permissions.py:317-325` + `:326`:

```python
if project is None:
    return False
profile = getattr(user, 'profile', None)
if profile is None:
    return False

if profile.role != 'Design':
    return False

return project.assigned_design_id == profile.pk   # W-narrow — no task-holding fallback
```

Git history over those lines shows the last two commits to touch them are `83739b6`
("Narrow BOQ write gate to assigned_design only (W-narrow)") and `6656300` — both well
before `cc387ee`. Neither Part 11, 4.6 nor 10 modified it. It returns False for Residential
design-lock purposes structurally, because a `DesignAssignment` only ever exists on an OPEX
site.

### Review

**12. The review screens show only the added items, read-only.** There is one shared review
screen for both gates — `design_qc_review()`, `design_views.py:3228`, rendering
`projects/design/qc_review.html` — so this holds for Design QC and the Design Head alike.

Context comes from `_boq_review_panel()`, `design_views.py:1384-1409`:

```python
category_order = opex_catalogue_category_order()
catalogue_ids  = {m.pk for m in get_opex_boq_catalogue()}
on_rows, off_rows = split_opex_boq_rows(boq, catalogue_ids)

return {
    'boq_by_category':    group_boq_rows_by_category(on_rows, category_order),
    'boq_off_catalogue':  group_boq_rows_by_category(off_rows, category_order),
    'boq_row_count':      len(on_rows) + len(off_rows),
    'boq_quantity_count': sum(1 for row in on_rows + off_rows
                              if row.boq_quantity and row.boq_quantity > 0),
}
```

It splits **this BOQ's** rows — it never iterates the 207-item catalogue. The template panel
(`qc_review.html:117-208`) contains no `<form>`, no `<input>` and no action; it is read-only
by construction rather than by a flag, and it groups by category in catalogue order so the
reviewer and the designer see the same document in the same sequence.

---

## PART 4.6 — Design Head Triage of PM Change Requests

# **DONE**

### Schema

**13. All four fields present.** `projects/models.py:2634-2650`:

```python
# ── Part 4.6 — the Design Head's triage ──────────────────────────────────
verdict = models.CharField(
    max_length=10, choices=CHANGE_REQUEST_VERDICT_CHOICES,
    default=CHANGE_REQUEST_PENDING,
)
decided_by = models.ForeignKey(
    'UserProfile', null=True, blank=True, on_delete=models.SET_NULL,
    related_name='triaged_design_change_requests',
)
decided_at = models.DateTimeField(null=True, blank=True)
rejection_reason = models.TextField(blank=True, default='')
```

**14. Check constraint requiring `rejection_reason` on rejection — present.**
`projects/models.py:2658-2662`:

```python
models.CheckConstraint(
    condition=(~models.Q(verdict=CHANGE_REQUEST_REJECTED)
               | ~models.Q(rejection_reason='')),
    name='cr_rejection_reason_required_when_rejected',
),
```

Added by migration `0058_part46_change_request_triage.py:96`.

**15. Partial unique constraint allowing at most one pending request per attempt —
present.** `projects/models.py:2668-2672`:

```python
models.UniqueConstraint(
    fields=['attempt'],
    condition=models.Q(verdict=CHANGE_REQUEST_PENDING),
    name='uniq_pending_change_request_per_attempt',
),
```

Added by migration `0058:105`. The view catches its `IntegrityError` and reports it
(`design_views.py:2905-2911`).

**16. Row counts.**

```
total: 1
Counter({'accepted': 1})
with resulting_attempt: 1
```

One row on the local database, the single pre-amendment request that migration 0058
classified as `accepted` on the strength of its `resulting_attempt`. `decided_by` and
`decided_at` are left null on it rather than manufacturing a triage nobody performed.

### Behaviour

**17. Raising a change request does NOT open a new attempt.** Part 4.6 *is* done on this
point. `design_change_request()`, `design_views.py:2884-2904` — the entire write is:

```python
if in_draft_group:
    remove_from_group(membership, profile, CHANGE_REQUEST_REMOVAL_REASON)

change = DesignChangeRequest.objects.create(
    attempt=attempt, requested_by=profile, reason=reason,
    verdict=CHANGE_REQUEST_PENDING)
log_activity(project, profile, …, action_code='design_change_requested')
```

No `_open_next_attempt()` call, no status change. The success message says so explicitly
(`:2913-2916`): *"No new attempt has been opened — he decides whether this becomes rework."*

**18. Head accept and reject actions exist.**

| | Accept | Reject |
|---|---|---|
| URL | `design/change-request/<int:pk>/accept/` (`urls.py:130`) | `design/change-request/<int:pk>/reject/` (`urls.py:131`) |
| View | `design_change_request_accept` (`design_views.py:2976`) | `design_change_request_reject` (`design_views.py:3036`) |
| Permission | `user_has_design_head_authority()` | same |

Both refuse identically through one shared front half, `_triage_guard()`
(`design_views.py:2949-2961`), which is deliberate — *"a reject endpoint that admits
somebody the accept endpoint refuses is the same hole either way round."* Design QC is not
admitted. Both re-read the row under `select_for_update()` inside the transaction and refuse
a second decision (`:3007-3009`, `:3069-3071`). `_triage_redirect()` honours `next` only
when it is a local path, closing an open redirect.

**19. Acceptance calls the shared attempt-opening function.**

- The function: `_open_next_attempt()`, `design_views.py:2248` — *"Close the current attempt
  and open the next one. THE ONLY PLACE THIS HAPPENS."*
- Call sites: `2609`, `2756` (the two QC-failure loops) and `3018` (acceptance). No
  duplicate.
- The call site, `design_views.py:3018-3020`:

```python
new_attempt = _open_next_attempt(
    assignment, ATTEMPT_REASON_PM_CHANGE_REQUEST, profile,
    f'change request accepted on attempt {change.attempt.attempt_number}')
```

`redo` is left at its default `None` on purpose — the brief moved, so nothing drawn against
the old brief carries forward.

**20. The closed attempt keeps `qc_verdict` and `head_verdict` at `pending`.** Confirmed.
Neither is written anywhere in `design_change_request_accept()`; the only fields the accept
view writes on the change request are `verdict`, `decided_by`, `decided_at` and then
`resulting_attempt` (`design_views.py:3011-3014`, `3022-3023`). The shared opener's contract
(`design_views.py:2260-2262`) is explicit:

> What it does NOT touch is as important as what it does:
>   * `qc_verdict` on the outgoing attempt — the caller owns that. QC-fail sets it to
>     'failed' before calling; a change request leaves it 'pending' forever.

and the accept view's own docstring (`:2986-2989`):

> THE CLOSED ATTEMPT KEEPS BOTH VERDICTS AT 'pending'. It was interrupted, not judged.
> Writing 'failed' at either gate would charge the designer with a rework loop the PM caused
> and inflate the QC failure rate with work nobody found fault in.

`_attempt_history.html:80` renders that state as "not judged" rather than as a verdict.

**21. All three suspension checks now test `verdict='pending'`.**

The definition moved off `resulting_attempt` in `_pending_change_requests()`,
`design_views.py:2337-2339`:

```python
if attempt is None:
    return DesignChangeRequest.objects.none()
return attempt.change_requests.filter(verdict=CHANGE_REQUEST_PENDING)
```

*(i) and (ii) — Part 9's two verdict gates.* Both go through one guard,
`_blocking_change_request()`, `design_views.py:2469`:

```python
if _pending_change_requests(attempt).exists():
    return (f'{project.project_id}: a PM change request on this attempt is awaiting '
            f'the Design Head\'s decision — the review is suspended until he accepts '
            f'or rejects it.')
return ''
```

Called at four points, covering both gates in both directions:
`design_qc_pass` (`:2498`), `design_qc_fail` (`:2560`), `design_head_qc_pass` (`:2642`),
`design_head_qc_fail` (`:2704`).

*(iii) — Part 6's group lock.* `pending_change_requests_for()`, `design_views.py:3928-3931`:

```python
return list(DesignChangeRequest.objects
            .filter(attempt__assignment__project_id__in=member_ids,
                    verdict=CHANGE_REQUEST_PENDING)
            .select_related('attempt__assignment__project', 'requested_by__user'))
```

Its docstring records that Part 4.6 is what made this guard reachable at all: under Part 4,
`resulting_attempt` was set in the same transaction as the row, so the guard fired only
against rows made in Django admin (deferred finding G6, now marked CLOSED at
`DESIGN_MODULE_DEFERRED.md:485`).

**22. Pending queue on the Head's dashboard, and on the Part 5 attention list — both
present.**

- *Queue:* `tender_dashboard.html:362-440`, panel 4, *"PM change requests awaiting your
  decision"*, with a count badge and inline accept / reject forms posting to the two triage
  URLs. Uncapped and oldest-first. Built by `change_request_queue()`,
  `design_metrics.py:649`. Empty state reads *"Nothing awaiting triage."*
- *Attention list:* `design_metrics.py:740-751`, its own severity band `_SEV_CHANGE`,
  reason string *"PM change request awaiting your decision — N day(s), attempt N"*.
- *Withheld from Design QC:* the band sits inside the `if not own_only:` block
  (`design_metrics.py:733`), and the QC dashboard passes `own_only=True`
  (`design_metrics.py:372`). The comment is explicit: *"Excluded from the `own_only` list on
  purpose: triage is the Head's, and Design QC cannot clear this row."*

The underlying read is one batched query on the same `__in` shape as the four beside it
(`design_metrics.py:267-273`), so the dashboard's fixed query budget is kept.

**23. Part 6 draft-group removal fires on RAISE.** Reported, not changed.

`design_change_request()`, `design_views.py:2892-2893`, inside the same transaction that
creates the pending row:

```python
if in_draft_group:
    remove_from_group(membership, profile, CHANGE_REQUEST_REMOVAL_REASON)
```

This was a deliberate decision, not an oversight — the in-code reason (`:2884-2891`) is that
SCM must not be left aggregating quantities for a site whose BOQ is under dispute while the
Head deliberates. The recorded consequence is that on **rejection** the site does not rejoin
the group by itself; SCM has to re-add it, which it can, because the site is still
`released`. Documented as deferred finding **P1** (`DESIGN_MODULE_DEFERRED.md:1305`).

---

## PART 10 — Quality Analytics

# **DONE**

### Existence

**24. The view exists.**

- URLs (`projects/urls.py:109-112`), two routes onto one view so the combined view has no
  second code path to drift down:
  - `design/analytics/` → `design_quality_analytics`
  - `programs/<int:pk>/design/analytics/` → `design_quality_analytics_tender`
  - `design/analytics/configure/` → `design_analytics_configure`
  - `design/analytics/reset/` → `design_analytics_reset`
- View: `design_views.py:4340` — `design_quality_analytics()`
- Template: `projects/templates/projects/design/quality_analytics.html` (546 lines), plus
  three partials, all of them included: `_qa_figure.html`, `_qa_person_table.html`,
  `_qa_spread.html`
- Computation module: `projects/design_analytics.py` (1261 lines), read-only
- Permission check: `user_is_design_head(request.user)` → 403 (`design_views.py:4346-4347`),
  repeated on the configure endpoint (`:4403-4404`) and the reset endpoint

**25. Metric selection is persisted per user.** Mechanism: a new one-row-per-profile table,
`DesignAnalyticsPreference` (`models.py:2821-2844`, migration 0059), rather than a column on
`UserProfile`:

```python
profile = models.OneToOneField(
    UserProfile, on_delete=models.CASCADE,
    related_name='design_analytics_preference',
)
#: Optional metric keys only. Core keys are never written here — see the note above.
metrics    = models.JSONField(default=list, blank=True)
updated_at = models.DateTimeField(auto_now=True)
```

Written only by `design_analytics_configure()` (`design_views.py:4406-4413`), which filters
the POSTed keys against `OPTIONAL_METRICS` before storing, so a core key cannot reach
storage:

```python
chosen = [k for k in request.POST.getlist('metrics') if k in OPTIONAL_METRICS]
chosen = [k for k in OPTIONAL_METRICS if k in set(chosen)]
DesignAnalyticsPreference.objects.update_or_create(
    profile=request.user.profile, defaults={'metrics': chosen},
)
```

Read by `_analytics_preference()` (`design_views.py:4326-4336`), which uses `.filter().first()`
rather than `get_or_create()` so a page load never writes. Core is unioned in at read time by
`selected_metric_keys()` (`design_analytics.py:311-322`), and unrecognised keys are dropped
rather than raising. Reset deletes the row rather than storing an empty list.

**26. Metrics implemented — all 20, and the core/optional split matches the prompt
exactly.** Read from `METRIC_CATALOGUE` at runtime:

**Core (5, always-on, non-disableable — `core=True`, never stored in a preference row):**

| Metric | Key | Present |
|---|---|---|
| First-pass rate | `first_pass_rate` | present |
| Error category distribution | `error_distribution` | present |
| Design Hold rate | `hold_rate` | present |
| Change request rate (per requesting PM) | `change_request_rate` | present |
| Overturn rate | `overturn_rate` | present |

`change_request_rate` is per requesting PM as specified — `design_analytics.py:873-901`,
*"THE PM IS THE UNIT, not the designer […] dividing every PM's accepted requests by the
tender's total released count would make a PM holding two sites look identical to one
holding forty."*

Three of the five cannot make a designer look bad by construction (Design Hold rate measures
the survey, change request rate measures the PM, overturn rate measures the two review gates
themselves) — which is the stated reason the core set is locked on.

**Optional (15):**

| Metric | Key | Present |
|---|---|---|
| Rework multiplier (Group A only) | `rework_multiplier` | present |
| Arka iterations | `arka_iterations` | present |
| QC failure rate | `qc_failure_rate` | present |
| Head failure rate | `head_failure_rate` | present |
| Capacity throughput | `capacity_throughput` | present |
| Group B failure count | `group_b_failures` | present |
| Design Hold duration | `hold_duration` | present |
| Change request rejection rate | `cr_rejection_rate` | present |
| Group C failure count | `group_c_failures` | present |
| Change requests by stage | `cr_by_stage` | present |
| On-time delivery | `on_time_delivery` | present |
| Due date extension rate | `extension_rate` | present |
| Cycle time | `cycle_time` | present |
| Stage dwell time | `stage_dwell` | present |
| Review queue latency | `queue_latency` | present |

**Nothing in the prompt's catalogue is absent, and nothing extra is present.** Five metrics
carry an on-screen `caveat` string (`design_analytics.py:219, 238, 253, 273, 295`) — the four
that are reduced against what the catalogue asked for (`capacity_throughput`,
`hold_duration`, `cr_by_stage`, `stage_dwell`) plus `rework_multiplier`, whose caveat records
that it is stricter than the identically-named Part 5 dashboard column.

### Correctness

**27. The minimum-denominator guard is implemented.** `design_analytics.py:104` and
`113-158`:

```python
MIN_DENOMINATOR = 5
LOW_CONFIDENCE_MAX = 14

def _state(denominator):
    if denominator < MIN_DENOMINATOR:
        return STATE_INSUFFICIENT
    if denominator <= LOW_CONFIDENCE_MAX:
        return STATE_LOW
    return STATE_OK
```

`rate()` and `ratio()` are the only two ways a number leaves the module, and both set
`'value': None` when the state is `insufficient`. The single rendering point,
`_qa_figure.html:18-25`:

```django
{% if f.state == 'insufficient' %}
  <span class="qa-none">Insufficient data (n={{ f.n }})</span>
{% else %}
  <span class="qa-figure">{{ f.value }}{% if f.is_pct %}%{% endif %}</span>
  <span class="qa-den ms-1">n={{ f.n }}</span>
  {% if f.state == 'low' %}<span class="qa-low ms-1">low confidence</span>{% endif %}
{% endif %}
```

So the rule cannot be skipped by a panel that forgets to check state — no panel formats a
number itself. `n=0` is treated as `insufficient` like any other sub-5 denominator.
`test_rate_refuses_below_minimum` (`tests_design_part10.py:337`) pins it.

**28. Group B and Group C causes stay out of every designer error figure, via the Part 9
helper.** The module imports `error_category_group` from models
(`design_analytics.py:86`) and states the rule at `:40`: *"Every A/B/C decision goes through
models.error_category_group(). There is no category tuple in the new module."*

Verified — there is no hardcoded category membership list in the file. The group is attached
at row-build time (`:553`, `:570`) by calling the helper, and the filters read the attached
value. Error distribution, `design_analytics.py:640-645`:

```python
rows = [r for r in _failure_rows(data) if r['group'] == ERROR_GROUP_A]
```

with the docstring: *"it is enforced by filtering on error_category_group() rather than by
listing which categories are designer errors."*

Rework multiplier, `design_analytics.py:638-642`:

```python
cause = causes.get(t.attempt_number)
if cause in (ERROR_GROUP_B, ERROR_GROUP_C, CAUSE_PM_CHANGE):
    row['excluded'] += 1
    continue
row['designer_attempts'] += 1
```

Attempts opened by a PM change request are excluded too, which makes this stricter than the
Part 5 dashboard column of the same name; the divergence is printed on the panel rather than
left to be discovered. Group B and C failures are counted in their own panels
(`_group_failures(data, ERROR_GROUP_B)` at `:830`, `ERROR_GROUP_C` at `:925`).

**29. The view is Head-only, and the deputy is excluded too.** The gate is
`user_is_design_head()` (`permissions.py:388-403`), which reads `UserProfile.is_design_head`
and nothing else — not `user_has_design_head_authority()`, which every other design view
calls:

```python
profile = getattr(user, 'profile', None)
if profile is None:
    return False
return bool(profile.is_design_head)
```

Design QC, designers, PMs and SCM therefore all fail the check and receive 403. This is
pinned by `test_every_other_role_is_refused_by_direct_url`
(`tests_design_part10.py:737-753`), which asserts 403 across **all four URLs** for seven
users: two designers, Design QC, PM, SCM, Site Engineer **and the Head's named deputy**.
That test passed in this session's run. `test_a_refused_post_writes_nothing` additionally
confirms a refused configure POST leaves no preference row behind.

**30. Exports are absent, as specified.** A search of `design_analytics.py` and
`quality_analytics.html` for `csv`, `export`, `xlsx`, `attachment;` and `download` returns
exactly two hits, both of them the on-screen note declaring the omission
(`quality_analytics.html:540-541`):

> **Deliberate gap:** there is no export on this page — no CSV, no PDF, no scheduled report.
> A downloadable file of per-person performance data leaves this screen's access rules
> behind…

There is no export URL, no view and no response with a `Content-Disposition` header.
Recorded as deferred finding **Q7**. `tests_design_part10` pins the absence.

---

## CROSS-CUTTING CHECKS

**31. Full test suite.** The test database was created successfully — all 60 migrations
applied, including 0057, 0058 and 0059 — so this is a real result, not a setup failure.

```
$ python manage.py test projects --noinput --verbosity=2
…
Ran 281 tests in 1969.625s

FAILED (errors=32)
```

**281 tests run. 32 errors, 0 failures, 249 passed.**

**Every one of the 32 errors is in `projects/tests_design_part11.py`, and every one is the
same error in `setUp` — not a single one is a test assertion failing.** Confirmed by
grouping the error blocks: all 32 `ERROR:` headers name `projects.tests_design_part11`, no
other module appears, and all 32 tracebacks terminate identically at
`tests_design_part11.py:98`:

```
psycopg2.errors.UniqueViolation: duplicate key value violates unique constraint
"projects_boqitemmaster_code_key"

django.db.utils.IntegrityError: duplicate key value violates unique constraint
"projects_boqitemmaster_code_key"
```

The cause is `Part11Base.setUp()` (`tests_design_part11.py:96-102`) bulk-creating
`ITM-001..ITM-037`, which migration 0047 has already seeded into every freshly-created test
database:

```python
def setUp(self):
    BOQItemMaster.objects.bulk_create([
        BOQItemMaster(code=f'ITM-{i:03d}', description=description, unit=unit,
                      category=category, project_type='Residential',
                      is_active=True, sort_order=i)
        for i, (category, description, unit) in enumerate(RESIDENTIAL_SEED, start=1)
    ])
```

This is deferred finding **P2** and is **pre-existing — not caused by Part 4.6 or Part 10.**
The Part 4.6 session verified it independently by running `CatalogueTests` (a class Part 4.6
never touched) from a clean `git worktree` at HEAD `cc387ee`, getting an identical failure.

The practical consequence for this audit: **Part 11 has 32 tests and none of them currently
execute**, so its behaviour is verified here from the code and from live-data queries, not
from a passing suite. The Part 11 commit message records that those checks were run and
passed at the time the part was written.

The other two parts' suites both pass in full:

| Suite | Tests | Result |
|---|---|---|
| `tests.py` | 8 | pass |
| `tests_design_groups.py` | 14 | pass |
| **`tests_design_part10.py`** | **42** | **all pass** |
| **`tests_design_part11.py`** | **32** | **all error in `setUp` (P2)** |
| **`tests_design_part46.py`** | **19** | **all pass** |
| `tests_design_part8.py` | 37 | pass |
| `tests_design_part9.py` | 65 | pass |
| `tests_gantt.py` | 23 | pass |
| `tests_permissions.py` | 41 | pass |
| **Total** | **281** | **249 pass, 32 error, 0 fail** |

**32. `makemigrations --check --dry-run`.**

```
$ python manage.py makemigrations --check --dry-run
No changes detected
```

Exit code 0. **No model change is unmigrated.**

**33. Migrations above 0049.** Eleven, of which three belong to the parts under audit.

| # | Name | Purpose |
|---|---|---|
| 0050 | `alter_designassignment_status` | `AlterField` on `DesignAssignment.status` — choices only. |
| 0051 | `backfill_opex_assigned_design` | Data-only (Part 4.5): brings `Project.assigned_design` into step with `DesignAssignment.assigned_to` on OPEX sites where the two had diverged. |
| 0052 | `sitegroup_sitegroupmembership` | Part 6: creates `SiteGroup` and `SiteGroupMembership`, with a partial unique constraint `uniq_active_site_group_membership`. |
| 0053 | `alter_userprofile_role` | Part 6.5b: removes `'Design Head'` from `UserProfile.ROLE_CHOICES`. Choices only, no data operation — zero users held the role on either database. |
| 0054 | `part8_cad_zip_and_design_hold` | Part 8: adds `DesignFile.archive_listing` and the Design Hold rename. No data operation. |
| 0055 | `part9_design_qc_gate` | Part 9: the second review gate, plus a backfill of existing attempts. |
| 0056 | `part9_1_scoped_rework` | Part 9.1: adds `ArkaSubmission.carried_forward_from`, `DesignAttempt.redo_required`, `DesignFile.carried_forward_from`. |
| **0057** | **`boqitemmaster_project_type_opex_catalogue`** | **Part 11:** adds `BOQItemMaster.project_type`, stamps the 37 existing rows Residential and imports the 207 OPX rows (`AddField` + `RunPython scope_and_import` / `drop_import`). |
| **0058** | **`part46_change_request_triage`** | **Part 4.6:** adds `verdict`, `decided_by`, `decided_at`, `rejection_reason`; `RunPython` classifies pre-amendment rows (anything with a `resulting_attempt` = accepted); adds both constraints. |
| **0059** | **`part10_design_analytics_preference`** | **Part 10:** creates `DesignAnalyticsPreference`. One new table, nothing else — no field added to any design model, no data operation. |
| 0060 | `designassignment_survey_folder_url_and_more` | Post-Part-10: adds `DesignAssignment.survey_folder_url`, `survey_link_added_at`, `survey_link_added_by`. |

All three part migrations are reversible: 0057 ships `drop_import`, 0058 reverses cleanly per
its own note, 0059 is a bare `CreateModel`.

**34. `DESIGN_MODULE_DEFERRED.md`.** 15 section headings, 99 entries.

| Section | Entries |
|---|---|
| A. Explicitly out of scope for Part 0.5 (carried forward) | 5 |
| B. Found during Part 0.5 | 5 |
| C. Found during Part 0.6 (BOQ permission gating) | 11 |
| D. Found during Part 1 (OPEX design data model) | 5 |
| E. Found during Part 2 (storage, survey, allocation, due-date handshake) | 7 |
| F. Found during Part 3 (Arka, CAD, BOQ, versioning) | 7 |
| G. Found during Part 4 (QC review, deputy, PM change requests, release) | 8 |
| H. Found during Part 4.5 (dashboard integration) | 6 |
| I. Found during Part 5 (Design Head tender dashboard) | 6 |
| J. Found during Part 6 (site groups, aggregated BOQ, BOQ lock) | 8 |
| K. Found during Part 6.5a/6.5b (Design Head role audit and closure) | 5 |
| Part 8 — deferred findings | 6 |
| Part 9 — Design QC role, dual approval gates, error categories | 6 |
| N. Found during Part 11 (OPEX BOQ item master and picker-based entry) | 7 |
| Q. Found during Part 10 (quality analytics) | 7 |
| **Total** | **99** |

Sections A and E use tables; the rest use `###` sub-headings. The counts above include both
forms.

Contents of the three sections relevant here:

- **N (Part 11)** — N1 `BOQItem.CATEGORY_CHOICES`/`UOM_CHOICES` do not cover the OPEX
  vocabulary · N2 OPEX BOQ submission to SCM (`BOQ.status` Draft → Submitted) has no UI on
  the picker · N3 14 pre-Part-11 OPEX BOQs carry Residential catalogue rows · N4
  `seed_scm_handoff_data` seeds a Residential template onto OPEX test sites · N5 "search
  inverter returns 15" is 18 on the real catalogue · **P1** the Part 6 draft-group removal
  fires on RAISE not on ACCEPTANCE (Part 4.6) · **P2** `tests_design_part11` collides with
  migration 0047's catalogue seed.
- **Part 4.6** has **no section heading of its own.** Its two entries, P1 and P2, are filed
  under section N's heading. Its main outcome is recorded elsewhere, as the CLOSED note on
  G6 (`:485`).
- **Q (Part 10)** — Q1 no per-status entry timestamp, so stage dwell time is not answerable ·
  Q2 `DesignChangeRequest` does not record the stage it was raised at · Q3 Design Hold
  duration is only recoverable from the activity log · Q4 no period control on the analytics
  screen · Q5 two different numbers are both called "rework multiplier" · Q6 `head_started_at`
  is null on most rows that reached the Head gate · Q7 no export, and that is a decision
  rather than an omission.

**35. Incomplete, unreachable or unwired code belonging to these three parts.**

Every model, view, URL and template added by the three parts is wired. Specifically checked
and found sound:

- `DesignAnalyticsPreference` — written by `design_analytics_configure`, read by
  `_analytics_preference`, deleted by `design_analytics_reset`. Not a `DesignSubmission`
  repeat.
- `DesignChangeRequest.decided_by` / `decided_at` / `rejection_reason` — all three are read
  and rendered, on `change_request.html:141-160` (the PM's own screen) and
  `_attempt_history.html:177-188` (the designer's only sight of a change request).
- `_qa_figure.html`, `_qa_person_table.html`, `_qa_spread.html` — all three are `{% include %}`d
  by `quality_analytics.html`.
- `_attempt_history.html` — included by `qc_review.html:398`, `change_request.html:172` and
  `site_workspace.html:273`.
- `opex_boq_entry.html` — rendered by `views.opex_boq_entry`, which has a URL and is reached
  by redirect from `boq_detail`, so it is reachable by clicking rather than only by typing.
- Both triage URLs are posted to from two places each: the tender dashboard queue and the QC
  review screen banner.

One genuine finding:

- **The Part 10 quality analytics screen has no navigation entry point.** A search of
  `projects/templates/` for `design_quality_analytics` and for the literal path
  `design/analytics` returns hits only inside `quality_analytics.html` itself — its own
  scope-switcher links. Nothing in `base.html`, `tender_dashboard.html` or any other
  template links to it, so the Head can reach it only by typing the URL. This is the same
  class of gap as deferred findings E1, F3 and J5, but it is **not recorded** anywhere in
  section Q (Q1–Q7 do not cover it) or elsewhere in `DESIGN_MODULE_DEFERRED.md`. The view,
  URL and template are all complete and functional — this is a discoverability gap, not a
  broken path.

Two pre-existing items in the same territory, both already recorded, neither introduced by
these parts:

- **N2** — the OPEX picker has no "submit to SCM" control, so `BOQ.status` never moves Draft
  → Submitted from that screen. `boq_submit` still exists and still works but is no longer
  reachable from the designer's screens (and per deferred finding C10 it crashes for every
  authorised caller — pre-existing, unreachable from the UI).
- **P2** — `tests_design_part11` is broken in `setUp` (see item 31).

---

## DEPLOYED VS LOCAL

| Part | Local | Deployed to Railway |
|---|---|---|
| **Part 11** | `cc387ee`, on `main` | **Yes.** Deployed 1 Aug 2026 as `84f46a46`, and carried in the current live deployment `2b4cc0e6` (`2792f2c`). |
| **Part 4.6** | `6ac23b9`, on `main` | **Yes.** Deployed 2 Aug 2026 as `1782a444`, and carried in the current live deployment. |
| **Part 10** | `ff29f28`, on `main` | **Yes.** Deployed 2 Aug 2026 as `b1441886`, and carried in the current live deployment. |

Local HEAD `2792f2c` equals the deployed SHA. **There is no local-only work in any of the
three parts.** The working tree was clean at session start and no part exists only locally.

The database state reported at item 2 and item 16 is the **local** Postgres database. This
audit did not query the Railway database, so the live row counts for `BOQItemMaster` and
`DesignChangeRequest` are not established here — see **UNCERTAIN**.

---

## UNCERTAIN

1. **Live database row counts.** Items 2, 3 and 16 were answered against the local Postgres
   database only. The Part 11 commit message claims the 37/207 split and all 16 category
   counts were verified on live data at the time, but this session did not re-run those
   queries against Railway, so the current production state of `BOQItemMaster` and
   `DesignChangeRequest` could not be determined.

2. **Whether migrations 0057–0059 have actually been applied on Railway.** The deployment
   record shows the commits deployed successfully, but this audit did not read
   `django_migrations` on the production database. A successful build is not by itself proof
   that the release command ran the migrations.

3. **Item 12, for the Design Head specifically at gate 2.** The read-only BOQ panel is on
   `qc_review.html`, which is the one screen both gates use, and both `can_qc` and
   `can_head_verdict` are computed on it. The panel itself is outside any gate conditional,
   so it renders for both. Confirmed from the template structure rather than from a live
   Head-gate page load.

4. **Whether the "not yet added" indicator is correct on first load for a saved sheet with
   off-catalogue rows.** The server renders the panel and the JS re-renders it from
   `boqAddedData`; `usedCats()` reads the client-side `chosen` map, which is built from
   catalogue rows. Whether an off-catalogue row's category counts towards "used" was not
   traced. Cosmetic either way.

---

## CONFLICTS

1. **Item 2's query does not produce item 2's expected answer, and the code is not at
   fault.** The prompt asks for `Counter(BOQItemMaster.objects.values_list('category'))`
   across the whole table, then gives expected category counts that sum to 207 — i.e. the
   OPEX rows alone. Run as written, the query returns the combined 244-row distribution and
   therefore disagrees with the expectation on four categories (`BOS` 50 vs 20, `Inverter` 18
   vs 15, plus `Solar Modules` 2 and `Structure` 2 which the expectation does not list).
   Filtered to `project_type='OPEX'` every one of the 16 expected counts matches exactly. The
   expectation is right about the code and wrong about the query.

2. **Same for the unit vocabulary.** The prompt says "Units limited to: Nos, Meter, Pkt, Set,
   KWp, Pair, Kg." That is true of the OPEX catalogue and only of the OPEX catalogue. The
   table also contains `Mtr` (6), `LOT` (2) and `LS` (1), all on Residential rows that
   pre-date Part 11 and that Part 11 deliberately did not normalise. If the intent was that
   the *whole table* be limited to seven units, that is not the case and was never the
   design — the Part 11 commit message describes normalising nine spellings to seven for the
   imported OPEX rows only.

3. **Item 26's optional list has 15 entries, not the 17 the section header implies.** The
   prompt's own catalogue lists 5 core + 15 optional = 20. The code has exactly 20 metrics
   with exactly that 5/15 split, and every named metric is present. No discrepancy in
   substance; noted only because the phrasing of the item could be read as expecting more.

4. **Item 17's framing.** The item says "Does raising a change request still open a new
   `DesignAttempt` immediately? **If yes, Part 4.6 is not done**." It does not; Part 4.6 is
   done on this point and on every other point checked.

5. **Item 34 assumes a Part 4.6 section exists in `DESIGN_MODULE_DEFERRED.md`.** There is no
   `## P` heading. The two Part 4.6 entries, P1 and P2, are filed under section N (Part 11)
   despite their `P` prefix. The document is internally inconsistent in that one respect —
   the entries exist and are complete, but they are not where their identifiers suggest.

6. **The repository root is not `c:\SolarPMS`.** The prompt's opening check assumes the
   working directory is the repo. `c:\SolarPMS` is a container holding three sibling
   directories (`Horizon-Solar-PMS`, `HRP-PMS-UI`, `HRP-Solar-UI`) and `git status` there
   fails with "not a git repository". All commands were run in
   `c:\SolarPMS\Horizon-Solar-PMS`, and this document is written to that repo's root.
