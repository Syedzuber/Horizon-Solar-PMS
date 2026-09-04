# `SiteGroup` membership audit — before `group_type` exists

**Prompt A-1.1. Read-only session. Branch `execution-phase-1`, branched from `execution-phase-0` @ `3797eb5`.**

Nothing outside this file and `EXECUTION_MODULE_DEFERRED.md` §B was written. No `.py`, `.html`,
`.css` or `.js` file was created or modified. No migration was created.

Everything below is read from source at commit `3797eb5`. Line numbers are as-of-this-commit
hints only; every reference is anchored on a function or class name, because
`ACCESS_ISOLATION_AUDIT.md`'s line references went stale inside two sessions.

---

## 0. The two findings that change what 1.1 does

Read these before the rest of the file.

### F-1 · The constraint D-1 prescribes cannot be written as specified

`docs/execution-model.md` §2 D-1 says two simultaneous memberships *"require altering it to
`(project, group_type)`"*. **That is not implementable as written**, and the reason is
structural rather than a matter of taste.

`group_type` is a field on **`SiteGroup`**. The constraint lives on **`SiteGroupMembership`**.
A `UniqueConstraint`'s `fields` must be *local* columns: Django validates them through
`Model._check_local_fields()` (`django/db/models/base.py`), which looks each name up in
`_meta._get_fields(reverse=False)` and raises `models.E012 — refers to the nonexistent field`
for anything containing `__`. That is not a Django quirk to route around either — a PostgreSQL
unique index is over columns of one table, so `group__group_type` has nowhere to be indexed.

**So prompt 1.1 has a design decision to take before it has a migration to write**, and this
audit cannot take it for the product owner. The two shapes are set out in Task E §1. The one
this audit recommends is **denormalising `group_type` onto `SiteGroupMembership` as well**, set
from the group at insert and never updated, because it is the only shape in which the database
— not a view — enforces D-1.

D-1's ⚠ box should be corrected to say so. It currently reads as though the migration is
mechanical.

### F-2 · The test baseline is red beyond the one known failure — but every failure is documented

Task G's stop condition fires on the letter, and does not fire on the substance. Full suite:
**645 tests, 1 failure + 4 errors.** The prompt expected exactly one failure. The other four
are *already written down* — `PHASE_0_COMPLETION.md` §"One outstanding chore in the net" names
all four by test method, states the cause and states the fix. They are a phase-0 chore that was
handed forward, not a new regression, and they are unrelated to `SiteGroup`.

Details and the exact list in Task G. **This is reported, not worked around; no test file was
touched.** The product owner should decide whether 1.1 clears the chore first — it is a fixture
reorder in two files with no assertion changes.

---

## Task Z — the eight claims, verified against source

**Corrections first.**

| # | Claim | Verdict |
|---|---|---|
| **4** | `active_group_membership()` lives in `projects/design_views.py` and carries the quoted docstring | **CONFIRMED with a correction to the quote's source.** The function is `design_views.py:active_group_membership` (~4118) and the docstring is: *"'One' is guaranteed by the partial unique constraint on SiteGroupMembership, not by this query — `.first()` here is picking the only row that can exist, not the first of several."* The prompt's paraphrase is accurate. What is **not** accurate is `ACCESS_ISOLATION_AUDIT.md` E.3, which says *"its three callers"* and then lists **two**. There are three callers; only two compute the gate. See claim 5. |
| **8** | `SiteGroup` has `SITE_GROUP_STATUS_CHOICES` and is not instrumented by `StatusTransition` | **CONFIRMED, and stronger than stated.** `SiteGroup` is also **not registered in `projects/admin.py`** (nor is `SiteGroupMembership`), so there is no admin write path to instrument or to defend against. Relevant to Task F: the status write surface is exactly one line of one view. |
| 1 | Partial `UniqueConstraint` on `['project']`, `condition=Q(removed_at__isnull=True)`, named `uniq_active_site_group_membership` | **CONFIRMED**, verbatim. `models.py:SiteGroupMembership.Meta.constraints` (~3496). Present in migration `0052_sitegroup_sitegroupmembership` and never altered since. |
| 2 | `SiteGroup.program` is a non-nullable FK, so a Residential project can never join one | **CONFIRMED, with the mechanism stated precisely.** `program = ForeignKey(Program, on_delete=CASCADE, related_name='site_groups')` — no `null=True`. The Residential exclusion is *two* facts, not one: (a) `Project._validate_program_link()` refuses `program` on a Residential project at every save path, and (b) `design_views._add_sites()` refuses `project.program_id != group.program_id`. Note (b) is a **view** check — see Task D for what that means. |
| 3 | `SiteGroup` has no `group_type` field today | **CONFIRMED.** Fields are exactly: `program`, `name`, `status`, `created_by`, `created_at`, `locked_by`, `locked_at`, `notes`. |
| 5 | Callers are `design_change_request` and `design_change_request_form`, and they use the result to compute `in_draft_group` | **CONFIRMED as stated, and incomplete as a list of callers.** Those two compute `in_draft_group` and no others do. But there is a **third** caller of `active_group_membership()`: `design_views._add_sites()`, which uses the result for a duplicate-membership refusal message. Three callers, two gates. The third one still breaks — see Task B. |
| 6 | `SiteGroupMembership`'s default ordering is `['added_at']` | **CONFIRMED.** `Meta.ordering = ['added_at']`, set explicitly. So `.first()` today returns the **oldest live** membership. Once two types coexist, the type that happens to have been added first wins — which for an existing site is always procurement, and for a new site is whichever team acted first. Deterministic in the query plan, non-deterministic in practice: the worst combination, because it will look correct in testing. |
| 7 | `project_boq_is_group_locked()` requires membership of a **locked** SiteGroup, and is structurally `False` on every Residential project | **CONFIRMED.** `permissions.py:project_boq_is_group_locked` filters `group_memberships` on `removed_at__isnull=True, group__status='locked'`. Structurally False on Residential via claim 2's chain. One caveat about *where* that is enforced — Task D. |

**Claims not found:** none. All eight were locatable in source.

---

## Task A — the two models as they actually are

### `SiteGroup` — `projects/models.py:SiteGroup` (~3420)

| Field | Type | Null | Default |
|---|---|---|---|
| `program` | FK → `Program`, `on_delete=CASCADE`, `related_name='site_groups'` | **no** | — |
| `name` | `CharField(max_length=120)` | no | — |
| `status` | `CharField(max_length=10, choices=SITE_GROUP_STATUS_CHOICES)` | no | `'draft'` |
| `created_by` | FK → `UserProfile`, `SET_NULL`, `related_name='created_site_groups'` | yes | — |
| `created_at` | `DateTimeField(auto_now_add=True)` | no | now |
| `locked_by` | FK → `UserProfile`, `SET_NULL`, `related_name='locked_site_groups'` | yes | — |
| `locked_at` | `DateTimeField` | yes | — |
| `notes` | `TextField(blank=True)` | no | `''` |

`Meta.ordering = ['-created_at']`. **No constraints and no `Meta.indexes`** — the only indexes
are the ones Django creates for the three FKs.

**`SITE_GROUP_STATUS_CHOICES` in full, and which code writes each value:**

| Value | Label | Written by |
|---|---|---|
| `'draft'` | Draft | The field default, and one explicit `status=SITE_GROUP_DRAFT` in `design_views.site_group_create`. Also `seed_scm_handoff_data` (dev fixture) and four test modules at `objects.create()` time. |
| `'locked'` | Locked | **`design_views.site_group_lock`, and nowhere else in application code.** `seed_scm_handoff_data` writes it directly for its fixture; `tests_boq_upload.UploadBase._group_lock`, `tests_design_part11` test 15b and `tests_design_groups` construct it at creation time. |

**What marks a group locked:** the `status` string `'locked'`, plus two provenance stamps
`locked_by` (FK → `UserProfile`) and `locked_at` (`DateTimeField`), all three written in one
`transaction.atomic()` block by `site_group_lock`.

**There is genuinely no unlock path.** Grepped the whole repo: no code anywhere writes
`SITE_GROUP_DRAFT` onto an already-locked group; `SiteGroup` is not in `admin.py`; there is no
unlock URL, view or management command. Both the model docstring and the view docstring say the
absence is deliberate — *"UNLOCKING IS NOT BUILT AND THAT IS DELIBERATE… the correction is a
variance against the order, not an edit to the BOQ it was raised from"*. The only ways back are
a shell, a `QuerySet.update()`, or dropping the row.

### `SiteGroupMembership` — `projects/models.py:SiteGroupMembership` (~3462)

| Field | Type | Null | Default |
|---|---|---|---|
| `group` | FK → `SiteGroup`, `CASCADE`, `related_name='memberships'` | no | — |
| `project` | FK → `Project`, `CASCADE`, `related_name='group_memberships'` | no | — |
| `added_by` | FK → `UserProfile`, `SET_NULL`, `related_name='added_group_memberships'` | yes | — |
| `added_at` | `DateTimeField(auto_now_add=True)` | no | now |
| `removed_by` | FK → `UserProfile`, `SET_NULL`, `related_name='removed_group_memberships'` | yes | — |
| `removed_at` | `DateTimeField` | yes | — |
| `removal_reason` | `TextField(blank=True)` | no | `''` |

`Meta.ordering = ['added_at']` — **set explicitly**, not inherited or absent.

Constraint, quoted verbatim:

```python
models.UniqueConstraint(
    fields=['project'],
    condition=models.Q(removed_at__isnull=True),
    name='uniq_active_site_group_membership',
)
```

**No `Meta.indexes`.** The partial unique index doubles as the index on `project`; there is
**no index on `(group, removed_at)`**, which is the shape both `_group_member_ids()` and
`site_group_detail` query. Not a defect at present volume; noted for 1.1's judgement, since
adding `group_type` is the natural moment to add one.

### `removed_at` semantics — append-and-stamp, not delete-and-recreate

**Stated plainly, because prompt 1.1's migration depends on the answer and neither document
gives it: membership is APPEND-AND-STAMP.**

- The only application-code writer of `removed_at` is `design_views.remove_from_group()`, which
  stamps `removed_at` / `removed_by` / `removal_reason` in one `save(update_fields=…)` and logs
  it. Its docstring calls itself *"THE ONLY PLACE A SITE LEAVES A GROUP"*, and both callers —
  `site_group_remove_site` (SCM removing by hand) and `design_change_request` (a PM change
  request pulling the site out) — route through it.
- **No application code path deletes a membership row.** The two `.delete()` call sites are both
  dev/test-data management commands: `seed_scm_handoff_data --reset` and
  `teardown_opex_test_data`, each scoped to `Test-`-prefixed programs.
- Two *implicit* delete paths exist and should be named rather than assumed away:
  `SiteGroupMembership.group` and `.project` are both `on_delete=CASCADE`. `SiteGroup` is not in
  the admin and has no delete view, and `project_delete` is a **soft** delete (`is_deleted=True`,
  `status` untouched — R-16), so neither is reachable from the UI today. A hard
  `Project.delete()` from a shell would take the membership history with it.

**Consequence for the migration:** every row is either live or tombstoned, and no row is ever
re-used or resurrected. A `group_type` backfill can therefore write every existing row
unconditionally — tombstones included — without racing a deletion, and without needing to
reason about which rows are "current".

---

## Task B — every consumer of membership, classified

Search covered `projects/*.py`, `projects/templates/**`, `projects/management/commands/**`,
`projects/migrations/**` and every test module, on `SiteGroup`, `SiteGroupMembership`,
`group_memberships` (the `related_name` from `Project`), `memberships` (from `SiteGroup`),
`site_groups` (from `Program`), `project_boq_is_group_locked`, `boq_group_locked`,
`in_draft_group` and `draft_group`.

`SiteGroup` / `SiteGroupMembership` appear in **no** other module: not `admin.py`, `forms.py`,
`utils.py`, `signals.py`, `decorators.py`, `context_processors.py`, `notifications.py`,
`reports.py`, `report_views.py`, `design_metrics.py`, `design_analytics.py` or
`design_storage.py`.

### Application code

| Consumer | File : function | What it reads | Assumes one row? | Which type does it mean? | What breaks with two |
|---|---|---|---|---|---|
| `active_group_membership()` | `design_views.py : active_group_membership` | `project.group_memberships` live, `.first()` | **YES — explicitly, in its own docstring** | **PROCUREMENT** | Returns the oldest live row of *either* type. Every downstream fact — lock state, group name, `in_draft_group` — becomes order-dependent. This is the root, not a symptom. |
| Change-request gate (POST) | `design_views.py : design_change_request` | the helper → locked check, `in_draft_group` | YES, via the helper | **PROCUREMENT** | Two silent wrong answers. See Task C. |
| Change-request gate (GET) | `design_views.py : design_change_request_form` | the helper → `group_locked`, `in_draft_group`, `window_open`, `draft_group` | YES, via the helper | **PROCUREMENT** | Form and POST are required to agree by construction. If both read the same wrong row they stay consistent *and* both wrong. |
| Duplicate-add pre-check | `design_views.py : _add_sites` | the helper → refusal message | YES, via the helper | **PROCUREMENT** | An execution membership makes SCM's add refuse with *"already in group X"* naming an execution group. Looks cosmetic; is actually blocking, because it stops a legitimate procurement add. |
| The soft-removal writer | `design_views.py : remove_from_group` | writes `removed_at`/`removed_by`/`removal_reason`; log text hardcodes *"Removed from procurement group"* | no (takes a row) | **PROCUREMENT** — *by its hardcoded log string* | Nothing errors. It will stamp an execution membership and write "procurement group" into the activity feed. Needs the wording generalised, or the caller narrowed. |
| Member ids of a group | `design_views.py : _group_member_ids` | `group.memberships` live | no | **EITHER** — confident: scoped to one `group`, so its answer is already type-scoped by whichever group it was handed. | Nothing. |
| Group member count | `design_views.py : _group_rows` | `program.site_groups` + `Count('memberships', filter=Q(memberships__removed_at__isnull=True))` | no | **PROCUREMENT** — feeds the SCM group screen and the SCM dashboard, both procurement UI. Needs `group_type='procurement'` on `program.site_groups`. | SCM sees the PM's execution batches listed as procurement groups, with a Lock button beside them. |
| Post-QC pool | `design_views.py : post_qc_pool` | `.exclude(project__in=SiteGroupMembership.objects.filter(removed_at__isnull=True).values('project_id'))` | **YES, implicitly** — treats *any* live membership as "spoken for" | **PROCUREMENT** | A site in an execution group vanishes from SCM's post-QC pool — the queue whose whole purpose is that released sites do not pile up unseen. **Silent, and exactly the failure the function's own docstring says it exists to prevent.** |
| Group detail screen | `design_views.py : site_group_detail` | `group.memberships` live and removed | no | **EITHER** — scoped to one group. | Nothing, *provided* `_group_or_404` narrows by type. |
| Group list screen | `design_views.py : site_group_list` | `_group_rows`, `post_qc_pool` | no | **PROCUREMENT** | Inherits both rows above. |
| Group creation | `design_views.py : site_group_create` | writes `SiteGroup(status='draft')` | n/a | **PROCUREMENT** | Must set `group_type` explicitly, not lean on the migration default that a later execution screen would also inherit. |
| Add sites | `design_views.py : site_group_add_sites` | `_add_sites` | via helper | **PROCUREMENT** | Inherits `_add_sites`. |
| Remove site | `design_views.py : site_group_remove_site` | `SiteGroupMembership` by pk, scoped `group=group` | no | **EITHER** — scoped to a group by pk. | Nothing, given the resolver narrows. |
| Lock | `design_views.py : site_group_lock` | `_group_member_ids`; writes `status='locked'` | no | **PROCUREMENT** — the lock is procurement-only by D-1. | An execution group could be locked through this endpoint if `_group_or_404` is not narrowed. **A write path — narrow it in the resolver, never in the template.** |
| The group resolver | `design_views.py : _group_or_404` | `get_object_or_404(SiteGroup, pk=…, program__is_deleted=False, program__program_type='OPEX')` | n/a | **PROCUREMENT** | **The single highest-leverage line in the file.** Adding `group_type='procurement'` here type-scopes six views at once, including both write paths. |
| SCM dashboard OPEX section | `design_views.py : scm_opex_tender_rows` | `_group_rows`, `post_qc_pool`, `locked_count` | no | **PROCUREMENT** | Inherits both. |
| The BOQ lock predicate | `permissions.py : project_boq_is_group_locked` | `project.group_memberships` live + `group__status='locked'` | **no — `.exists()`, not `.first()`** | **PROCUREMENT** | **Nothing breaks *today*, and that is the trap.** `.exists()` across both types stays correct *while* `'locked'` exists only on procurement groups. It becomes wrong the moment an execution lifecycle uses that word or shares the `status` column. Narrow it anyway — Task D. |
| `user_can_manage_site_groups`, `user_can_view_site_groups` | `permissions.py` | role strings only; reads no membership and no group row | no | **EITHER** — nothing is read. | Nothing. But both are named for *procurement* authority; an execution group needs its own helper, not a widening of these (R-13, R-15). |
| BOQ detail screen | `views.py : boq_detail` | `project_boq_is_group_locked(project)`; separately `project.group_memberships.filter(removed_at__isnull=True, group__status='locked').first()` for the banner | banner uses `.first()`, but narrowed by `group__status='locked'`, so it is one row **by construction**, not by the constraint | **PROCUREMENT** | As the predicate: safe today, unsafe if `locked` ever becomes an execution status. |
| OPEX BOQ picker | `views.py : opex_boq_entry` | `project_boq_is_group_locked` | no | **PROCUREMENT** | As above. |
| OPEX BOQ upload | `views.py : opex_boq_upload` | `project_boq_is_group_locked` | no | **PROCUREMENT** | As above. |
| BOQ submit | `views.py : boq_submit` | `project_boq_is_group_locked` | no | **PROCUREMENT** | As above. |
| Design workspace | `design_views.py : design_site_workspace` | `project_boq_is_group_locked` | no | **PROCUREMENT** | As above. |
| SCM dashboard | `views.py : dashboard_scm` | `scm_opex_tender_rows()` | no | **PROCUREMENT** | Inherits. |

### Templates

| Consumer | File | What it reads | Assumes one row? | Type | What breaks with two |
|---|---|---|---|---|---|
| BOQ lock banner | `projects/boq_detail.html` | `boq_group_locked`, `locked_group` | no | **PROCUREMENT** | Text says "locked procurement group"; inherits the view's answer. |
| Workspace lock banner | `projects/design/site_workspace.html` | `boq_group_locked` | no | **PROCUREMENT** | Inherits. |
| Change-request page | `projects/design/change_request.html` | `draft_group`, `group_locked`, `window_open`, `released` | via the view | **PROCUREMENT** | Would name an execution group to the PM as *"procurement group X"* and tell them raising a change removes it from that group. |
| Group list | `projects/design/site_groups.html` | `groups`, `pool` | no | **PROCUREMENT** | Inherits `_group_rows` / `post_qc_pool`. |
| Group detail | `projects/design/site_group_detail.html` | `memberships`, `removed`, `blockers`, `pool` | no | **EITHER** — scoped to one group. | Nothing, given the resolver narrows. |
| SCM dashboard partial | `projects/design/_scm_opex_groups.html` | `opex_tender_rows` | no | **PROCUREMENT** | Inherits. |

### Management commands

| Consumer | File | Type | Note |
|---|---|---|---|
| `seed_scm_handoff_data` | creates 2 groups + 4 memberships (2 live, 2 removed); `--reset` **hard-deletes** them | **PROCUREMENT** | Dev fixture. Must set `group_type` explicitly once the field exists, or the fixture stops representing what SCM sees. |
| `teardown_opex_test_data` | deletes `SiteGroupMembership` / `SiteGroup` under `Test-` programs | **EITHER** — a teardown deletes whatever is there, by design. | Widen it to both types when execution groups exist, or test data leaks. |

### Tests

| Consumer | File : class | Type | Note |
|---|---|---|---|
| `PostQCPoolTests` (11 tests) | `tests_design_groups.py` | **PROCUREMENT** | Contains the one test that changes meaning silently — Task G. |
| `CompletenessAndMembershipTests` (3 tests) | `tests_design_groups.py` | **PROCUREMENT** | `test_group_member_ids_counts_active_memberships_only`. |
| `UploadBase._group_lock` | `tests_boq_upload.py` | **PROCUREMENT** | Fixture helper: locked group + membership. |
| `test_15b_group_lock_still_beats_the_design_lock` | `tests_design_part11.py` | **PROCUREMENT** | |
| `GroupLockTests` | `tests_design_part46.py` | **PROCUREMENT** | Lock refused while a change request is pending. |

### Migrations

`0052_sitegroup_sitegroupmembership` creates both models and the constraint.
`0053_alter_userprofile_role` depends on `0052` and touches nothing here. **No later migration
alters either model.** Migrations run to `0070_checklist_drop_is_active`; the next free number
is `0071`.

### Counts

| Class | Count |
|---|---|
| **PROCUREMENT** | **32** — 21 application-code paths (including `remove_from_group`, `_group_rows`, `_group_or_404`), 5 templates, 1 management command, 5 test classes/fixtures |
| **EXECUTION** | **0** — nothing in the codebase reads a membership on behalf of execution today, because execution grouping does not exist yet |
| **EITHER** | **7** — `_group_member_ids`, `site_group_detail`, `site_group_remove_site`, the two permission helpers, `site_group_detail.html`, `teardown_opex_test_data` |
| **UNCLASSIFIABLE** | **0** |

**No consumer was unclassifiable**, and the reason is worth recording rather than being taken as
reassurance: every reader in the codebase today was written by Part 6, for procurement, and most
say so in a docstring or in user-visible text.

The risk in 1.1 is therefore not ambiguity — it is the seven **EITHER** rows, which are
type-agnostic *only because they are scoped to a group someone else already resolved*. Every one
of them becomes type-specific the instant `_group_or_404` stops narrowing. That resolver is
load-bearing for six views and one template, and it is one line.

---

## Task C — the design change-request gate, traced end to end

### What `in_draft_group` decides

It is computed twice, and the two are **not spelled the same**:

```python
# design_change_request (the POST) — the locked case has already returned above
in_draft_group = membership is not None

# design_change_request_form (the GET)
group_locked   = membership is not None and membership.group.status == SITE_GROUP_LOCKED
in_draft_group = membership is not None and not group_locked
```

They agree today only because the POST returns early on the locked case. Two spellings of one
rule, kept in step by a comment. Recorded as **B1** in `EXECUTION_MODULE_DEFERRED.md` §B; not
fixed (R-12).

What it decides, per state, for the PM (or a coordinator) on an OPEX site:

| Membership state | `in_draft_group` | What the PM can and cannot do |
|---|---|---|
| No live membership | `False` | May raise a change request **only** while `assignment.status` ∈ `CHANGE_REQUEST_STATUSES`. A `released` design is refused: *"the design is already released — a change now is a new scope of work, not a change request."* |
| Live membership, group `draft` | `True` | `DESIGN_RELEASED` is **added** to the allowed statuses — the PM may reopen a released design. Raising it also **soft-removes the site from the group** in the same transaction, and the success message says so. |
| Live membership, group `locked` | n/a — returns first | Refused outright: *"the BOQ is locked… A change now needs a variance against the order."* The form renders the lock notice instead of the form. |

So `in_draft_group=True` is a **widening**: it is the one thing in the product that lets a
released design be reopened by a PM. The code says so itself — *"admitting it here means
stepping past BOTH of Part 4's release guards"*.

### The full path from the membership row to the decision

```
Project
  └─ .group_memberships                      (related_name on SiteGroupMembership.project)
       └─ .filter(removed_at__isnull=True)
            └─ .select_related('group', 'group__program')
                 └─ .first()                 ← design_views.active_group_membership
                      ├─ membership.group.status == 'locked'  → hard refusal, both views
                      └─ in_draft_group
                           ├─ allowed_statuses += (DESIGN_RELEASED,)   ← the widening
                           ├─ remove_from_group(...)                   ← a WRITE
                           ├─ window_open / draft_group   (form + template)
                           └─ 'released' context flag      (template branch)
```

### The two wrong answers, once two types coexist

Both are silent. Neither raises.

1. **False widening.** The site is in an *execution* group (active) and in no procurement group.
   `.first()` returns the execution row. `in_draft_group` becomes `True`, so a PM may reopen a
   `released` design that Part 4 was guarding — **and `remove_from_group()` soft-removes the site
   from the PM's own execution batch**, writing "Removed from procurement group" into the feed.
   One `.first()`, one lifted gate and two wrong writes.

2. **False refusal / missed lock.** The site is in a *locked procurement* group **and** an
   execution group, and `added_at` puts the execution row first. `membership.group.status` is
   then the execution group's status, so the hard refusal **does not fire** and the PM is
   admitted to raise a change request against a BOQ whose quantities have already gone to a
   vendor. The BOQ lock itself still holds — `project_boq_is_group_locked()` uses `.exists()`,
   not `.first()` (Task D) — so the *quantities* are safe. What is lost is the refusal message
   and the guarantee that a locked group's members are not under dispute; `site_group_lock`'s
   pending-request blocker is defeated after the fact.

### The correct behaviour when a site is in both

**Both gates must ask for `procurement` and ignore execution entirely.**

An execution group is the PM's own delivery batch. It carries no commitment to a vendor, has its
own re-plannable lifecycle (D-1: planning / active / closed), and there is no reason for it to
grant or withhold the right to raise a design change request.

Concretely, for a site in a **draft procurement** group and an **active execution** group
simultaneously: `in_draft_group` is `True` *because of the procurement membership*, the widening
applies, and `remove_from_group()` removes the site from the **procurement** group only. The
execution membership is untouched — the PM's batch does not lose a site because the design is
being revised; the *order* does.

### Does fixing this require editing `projects/design_views.py`?

**YES. Loudly, and there is no way around it.**

`active_group_membership()` is *defined in* `design_views.py`, and both gates are *in*
`design_views.py`. The design module has been correctly scoped out of every session in this
programme — §13 of the model doc says instrumenting `DesignAssignment` *"means editing
design_views.py, which has been correctly scoped and untouched all programme. **It is a session
of its own.**"*

**This is a standing scope boundary the product owner has to lift deliberately. It is not a
session's to cross on its own judgement.** Prompt 1.1 must be given that permission explicitly,
in writing, before it opens this file.

The compensation is that the smallest diff is genuinely small — three lines, one function, no
caller changes:

```diff
-def active_group_membership(project):
+def active_group_membership(project, group_type=GROUP_TYPE_PROCUREMENT):
     """The site's one live membership, or None.

-    "One" is guaranteed by the partial unique constraint on
-    SiteGroupMembership, not by this query — `.first()` here is picking the only row
-    that can exist, not the first of several.
+    "One" is guaranteed by the partial unique constraint on SiteGroupMembership —
+    which is keyed on (project, group_type) since prompt 1.1, NOT on project alone.
+    `.first()` here is picking the only row that can exist OF THIS TYPE. The default
+    is procurement because every caller in this module is a procurement caller: the
+    change-request gate and SCM's duplicate-add check. An execution caller must pass
+    the type explicitly rather than rely on the default.
     """
     return (project.group_memberships
-            .filter(removed_at__isnull=True)
+            .filter(removed_at__isnull=True, group_type=group_type)
             .select_related('group', 'group__program').first())
```

`group_type=group_type` filters the membership's own denormalised column (F-1). If the product
owner instead keeps `group_type` only on `SiteGroup`, the filter becomes
`group__group_type=group_type` — which works for a *query* — but then the constraint cannot be
written at all and D-1 is enforced by convention alone.

A **defaulted** keyword argument rather than a required one is deliberate: it keeps the diff at
three lines instead of five call-site edits, and it makes the dangerous mistake impossible to
write by accident — an execution caller that forgets the argument gets **zero rows**, not the
wrong row.

`remove_from_group()`'s hardcoded `"Removed from procurement group"` log string should be looked
at in the same pass. It is not a correctness bug once the gate is narrowed, but it becomes one
the day an execution caller reuses the helper.

---

## Task D — the BOQ lock chain

### Every consumer of `project_boq_is_group_locked()`

Imported in exactly two modules — `views.py` and `design_views.py` — and called in six places.
**All six are read-then-refuse. None of them writes.**

| Call site | What it gates |
|---|---|
| `views.boq_detail` | Computed **once** at the top and reused by five gates in the one view: the BOQ auto-create branch, the `action in ('save_design','submit_design','add_item','delete_item')` refusal, `design_form_open`, the template's input gates, and the banner's `locked_group` lookup. Its comment says the single computation exists *"so the locks can never be applied to one branch and forgotten on another"* — a property worth preserving through 1.1. |
| `views.opex_boq_entry` | `can_edit = can_author and not boq_group_locked and not boq_design_locked` |
| `views.opex_boq_upload` | `stage='locked'` with the procurement wording, checked **before** the design lock ("most specific first") |
| `views.boq_submit` | `HttpResponseForbidden` |
| `design_views.design_site_workspace` | The `can_edit_boq` context flag and the workspace banner |
| Templates | `boq_detail.html`, `site_workspace.html`, `opex_boq_upload.html` — display only |

What is deliberately **not** gated: the SCM branch of `boq_detail` (`save_scm`,
`acknowledge_scm`). It writes `ordered_quantity`, and locking the group is precisely the signal
for SCM to start ordering. Correct, and it must stay outside the lock.

### Structurally `False` on Residential — confirmed, with one honest caveat

The chain: a lock needs a live `SiteGroupMembership` whose `group.status='locked'`; a `SiteGroup`
needs a non-nullable `program`; `Project._validate_program_link()` refuses a `program` on a
Residential project at **every** save path. So no Residential project can be in any group, locked
or otherwise. **Confirmed.**

The caveat, stated because the audit found it rather than assumed it away: the *membership row
itself* carries no type check. `SiteGroupMembership.project` is a plain FK to `Project`. The rule
"a site must belong to this group's tender" is enforced in `design_views._add_sites()` — a
**view**, not a constraint. A row inserted from a shell, a data import, or a future code path
that does not go through `_add_sites()` could put a Residential project into an OPEX group, and
`project_boq_is_group_locked()` would then correctly return `True` for it. Not a live
vulnerability — there is no such path today — and it is exactly why §3.3's "both locks are still
evaluated on the Residential path" is the right design. Counting query 6 in Task E checks it on
production.

### Does the property survive `group_type`?

**Yes, and it is strengthened, provided one line changes.**

`RESIDENTIAL_BASELINE.md` §3.3 is precise about *why* both locks stay evaluated on Residential:
*"they exist so a hand-crafted POST to a Residential endpoint cannot become an OPEX bypass."*
That property is about the predicate being **called**, not about what it returns. Nothing in
`group_type` removes a call site, so all six survive unchanged and the property holds.

The one line: `project_boq_is_group_locked()` should gain a `group_type='procurement'` term
alongside `group__status='locked'`. Today it is correct without it, because `'locked'` exists
only on procurement groups. **Do not rely on that.** D-1 gives execution groups their own
lifecycle — *planning / active / closed* — and if a later session ever reuses the `status` column
or the string `locked`, an execution group would begin freezing member sites' BOQs with no
unlock, and nothing in the test suite would catch it. Narrowing costs one filter term and makes
the predicate say what it means.

The same applies to `boq_detail`'s `locked_group` banner lookup, which is a separate hand-written
query carrying the same two filter terms.

---

## Task E — the proposed migration, as text

**R-1: proposed for approval, not written. No file was created and `makemigrations` was not
run.** The next free number is `0071` (migrations run to `0070_checklist_drop_is_active`).

### 1. The `group_type` field — and the decision F-1 forces

**On `SiteGroup`:**

```python
GROUP_TYPE_PROCUREMENT = 'procurement'
GROUP_TYPE_EXECUTION   = 'execution'

GROUP_TYPE_CHOICES = [
    (GROUP_TYPE_PROCUREMENT, 'Procurement'),
    (GROUP_TYPE_EXECUTION,   'Execution'),
]

group_type = models.CharField(
    max_length=12, choices=GROUP_TYPE_CHOICES, default=GROUP_TYPE_PROCUREMENT,
    db_index=True,
)
```

**Non-null with a default, not nullable-then-backfill.** Reasons, in order of weight:

1. Every existing row genuinely *is* procurement — verified from the creation code in §2 below,
   not assumed. There is no row whose type is unknown, so `NULL` would mean nothing.
2. A nullable discriminator cannot go into a unique constraint usefully: `NULL != NULL` in a
   PostgreSQL unique index, so any row with `group_type IS NULL` would be exempt from the very
   rule the field exists to express. That is the same trap `ACCESS_ISOLATION_AUDIT.md` E.3 flags
   for `ProgramAssignment`'s two-target design.
3. The table is tiny (see the counting SQL). On PostgreSQL 11+ adding a column with a
   non-volatile default does not rewrite the table at all.

`default=GROUP_TYPE_PROCUREMENT` is right for the *migration* and **wrong to lean on in code**:
`site_group_create` should pass `group_type=GROUP_TYPE_PROCUREMENT` explicitly, so an execution
creation path added later cannot inherit procurement by forgetting.

**And now the decision F-1 forces.**

**Shape A — denormalise onto the membership (RECOMMENDED).** Add the same field to
`SiteGroupMembership`, populated from `group.group_type` at insert and never updated:

```python
# on SiteGroupMembership
group_type = models.CharField(
    max_length=12, choices=GROUP_TYPE_CHOICES, default=GROUP_TYPE_PROCUREMENT,
)
```

- **For:** the constraint becomes writable, and D-1 becomes true *in the database* — the standard
  this codebase holds itself to everywhere else (`uniq_active_task_template_per_code`,
  `uniq_active_checklist_per_code`, `uniq_pending_change_request_per_attempt`) and the standard
  this constraint's own docstring sets: *"the view checks it too, for a decent error message, but
  the database is what makes it true — a view check alone loses to a concurrent add."*
- **Against:** denormalisation can drift. Mitigated completely by making `SiteGroup.group_type`
  immutable after creation — a group does not change what kind of thing it is — and by setting
  the membership's copy from `group.group_type` rather than from a caller-supplied argument. The
  codebase already has and defends this exact pattern: R-8, and
  `StatusTransition.actor_role_code` (*"COPIED AT WRITE TIME, NEVER JOINED"*).

**Shape B — keep it only on `SiteGroup`.** Then there is **no constraint**, and D-1 is enforced
by `_add_sites()`'s pre-check alone — precisely the guarantee the model docstring says is
insufficient. This audit does not recommend it. If the product owner chooses it anyway, that
choice belongs in §12's decision log with its reason, not left implicit in a migration.

Everything below assumes **Shape A**.

### 2. The backfill — verified from the creation code, not from the assumption

**Every path that creates a `SiteGroup`:**

| Path | Who calls it | Context |
|---|---|---|
| `design_views.site_group_create` | SCM, via `POST /programs/<pk>/site-groups/create/`, gated by `permissions.user_can_manage_site_groups` (`role == 'SCM'`; Admin deliberately excluded) | **The only production path.** Writes `status=SITE_GROUP_DRAFT`. |
| `seed_scm_handoff_data` | A developer, by hand | Dev fixture, under `Test-`-prefixed programs |
| Four test modules | The test runner | Ephemeral |

**Every path that creates a `SiteGroupMembership`:**

| Path | Who | Context |
|---|---|---|
| `design_views._add_sites` | SCM, reached from `site_group_create` and `site_group_add_sites` | **The only production path** |
| `seed_scm_handoff_data`, four test modules | dev / test | — |

**No admin path** (neither model is registered), **no signal, no `save()` override, no
management command besides the two named, no import, no webhook.**

**Verdict: every existing `SiteGroup` row is an SCM procurement batch, and this is verified from
the code rather than assumed.** No path could have created something that is not procurement. A
blanket default is therefore correct, and the group table needs no `RunPython` at all.

The membership table needs one only under Shape A, and even then it is a single unconditional
`UPDATE`, because all rows are procurement:

```python
def _backfill_procurement(apps, schema_editor):
    apps.get_model('projects', 'SiteGroupMembership').objects.update(
        group_type='procurement')
```

Strictly redundant against the column default, and worth including anyway: it makes the intent
explicit in the migration history rather than relying on a future reader to notice a default. It
must run **after** `AddField` and **before** `AddConstraint`.

### 3. The operation sequence, in order

```python
class Migration(migrations.Migration):

    dependencies = [('projects', '0070_checklist_drop_is_active')]

    operations = [
        # 1. The discriminator on the group.
        migrations.AddField(
            model_name='sitegroup',
            name='group_type',
            field=models.CharField(
                choices=[('procurement', 'Procurement'), ('execution', 'Execution')],
                db_index=True, default='procurement', max_length=12),
        ),

        # 2. The denormalised copy on the membership (Shape A). Without this the
        #    constraint in step 5 CANNOT BE WRITTEN — see F-1.
        migrations.AddField(
            model_name='sitegroupmembership',
            name='group_type',
            field=models.CharField(
                choices=[('procurement', 'Procurement'), ('execution', 'Execution')],
                default='procurement', max_length=12),
        ),

        # 3. Explicit backfill. Redundant against the default; kept so the history says
        #    what was intended rather than relying on a default having been read.
        migrations.RunPython(_backfill_procurement, migrations.RunPython.noop),

        # 4. Drop the old rule. From here to step 5 there is no uniqueness — see §4.
        migrations.RemoveConstraint(
            model_name='sitegroupmembership',
            name='uniq_active_site_group_membership',
        ),

        # 5. The new rule: one live membership per project PER TYPE.
        migrations.AddConstraint(
            model_name='sitegroupmembership',
            constraint=models.UniqueConstraint(
                fields=['project', 'group_type'],
                condition=models.Q(removed_at__isnull=True),
                name='uniq_active_site_group_membership_per_type',
            ),
        ),
    ]
```

**The new constraint, quoted in full:**

```python
models.UniqueConstraint(
    fields=['project', 'group_type'],
    condition=models.Q(removed_at__isnull=True),
    name='uniq_active_site_group_membership_per_type',
)
```

**Rename it; do not reuse the old name.** Two reasons. The name is what appears in the
`IntegrityError` that `_add_sites()` catches and reports to SCM, and it should say what the rule
now is. And `tests_design_part46` already demonstrates that a test can assert on a constraint
name appearing in an exception string — a silently redefined name is exactly the sort of thing
that keeps passing a test while meaning something else.

### 4. The gap between `RemoveConstraint` and `AddConstraint`

**Answered from Django's installed source in this repo's venv, not from memory of another
project.**

`django/db/backends/base/schema.py`, `BaseDatabaseSchemaEditor.__init__`:

```python
self.atomic_migration = self.connection.features.can_rollback_ddl and atomic
```

and `__enter__` opens `atomic(self.connection.alias)` when that is true.
`django/db/backends/postgresql/features.py` sets `can_rollback_ddl = True`; the base class
defaults it to `False`. `Migration.atomic` defaults to `True`
(`django/db/migrations/migration.py`).

**So on PostgreSQL the whole migration — steps 1 through 5 — runs inside one transaction.** The
window is not visible to any other session: either every operation commits or none does. **It is
not a real window**, and no `SeparateDatabaseAndState` dance or two-migration split is needed.

Two facts that are true and must not be confused with the above:

- `RemoveConstraint` / `AddConstraint` take an `ACCESS EXCLUSIVE` lock on
  `projects_sitegroupmembership` for the duration. Concurrent writers **block**; they do not slip
  through the gap. On a table this size the lock is held for milliseconds.
- If this is ever run against **SQLite**, it also reports `can_rollback_ddl = True`, so the same
  guarantee holds for the test suite — which in any case runs with `MIGRATION_MODULES` disabled
  and builds the schema directly from model state.

### 5. Reversibility — the answer is no

**Every operation is individually reversible, and the migration as a whole is not reversible once
any project holds two live memberships.**

Reversing runs steps 5→1: drop the per-type constraint, then re-create
`uniq_active_site_group_membership` on `project` alone. If a single project by then holds one
live procurement membership *and* one live execution membership, creating that unique index
**fails** — `could not create unique index … Key (project_id)=(N) is duplicated` — and the whole
reverse migration rolls back. `RunPython`'s reverse is a `noop`, which is correct: the data
problem is not the backfill's, it is the constraint's.

**How this changes the deployment:**

- It becomes a **one-way door the moment the first execution group gains its second member** —
  which is the moment the feature is used at all.
- Take a database backup immediately before applying it. Not as good practice — as the only
  rollback that exists.
- Deploy the migration **before** the code that can create execution groups, and keep a window in
  which no execution group has been formed. During that window `migrate projects 0070` still
  works, and it is the only period in which it does.
- Do not read "it reverses cleanly on staging" as evidence. It reverses cleanly on any database
  with no execution memberships in it — which is every staging database until someone uses the
  feature.

Write this into the decision log alongside the migration, because the next person to reach for
`migrate projects 0070` will not have read this file.

### 6. The read-only counting query, for production

Runnable through the `report_ro` role. `SELECT` only. **Not run by this session.**

```sql
-- 1. Groups, by status. Expect every row to be a procurement batch (§2 above).
SELECT status, COUNT(*) AS groups
FROM   projects_sitegroup
GROUP  BY status
ORDER  BY status;

-- 2. Memberships: live vs tombstoned.
SELECT CASE WHEN removed_at IS NULL THEN 'live' ELSE 'removed' END AS state,
       COUNT(*) AS memberships
FROM   projects_sitegroupmembership
GROUP  BY 1
ORDER  BY 1;

-- 3. Locked groups and how many live members each freezes. These are the sites whose
--    BOQ is currently read-only: the blast radius of anything touching
--    project_boq_is_group_locked().
SELECT g.id, g.name, p.name AS program, g.locked_at,
       COUNT(m.id) FILTER (WHERE m.removed_at IS NULL) AS live_members
FROM   projects_sitegroup g
JOIN   projects_program   p ON p.id = g.program_id
LEFT   JOIN projects_sitegroupmembership m ON m.group_id = g.id
WHERE  g.status = 'locked'
GROUP  BY g.id, g.name, p.name, g.locked_at
ORDER  BY g.locked_at;

-- 4. THE ONE THAT MATTERS. Any project already holding more than one live membership.
--    The existing partial unique constraint makes this impossible, so a NON-EMPTY
--    result means the constraint is missing or was never applied on this database —
--    which must be resolved BEFORE the migration, not by it.
SELECT m.project_id, pr.project_id AS project_code, COUNT(*) AS live_memberships
FROM   projects_sitegroupmembership m
JOIN   projects_project pr ON pr.id = m.project_id
WHERE  m.removed_at IS NULL
GROUP  BY m.project_id, pr.project_id
HAVING COUNT(*) > 1;

-- 5. Sanity: confirm the constraint is actually present on this database.
SELECT conname, pg_get_constraintdef(oid) AS definition
FROM   pg_constraint
WHERE  conrelid = 'projects_sitegroupmembership'::regclass;

-- A partial unique constraint is created as an INDEX, so if query 5 does not show it,
-- this is where it lives:
SELECT indexname, indexdef
FROM   pg_indexes
WHERE  tablename = 'projects_sitegroupmembership';

-- 6. Residential contamination check. MUST return zero rows. A non-zero result refutes
--    RESIDENTIAL_BASELINE.md §3.3 on the live database and is a finding in its own right.
SELECT pr.project_id, pr.project_type, g.id AS group_id, g.status
FROM   projects_sitegroupmembership m
JOIN   projects_project   pr ON pr.id = m.project_id
JOIN   projects_sitegroup g  ON g.id  = m.group_id
WHERE  pr.project_type = 'Residential';
```

Query 4 is the go/no-go. Query 6 is worth running even if 1.1 is deferred.

---

## Task F — `SiteGroup` and the transition ledger

### What instrumenting it would actually cost

`docs/execution-model.md` §13 itemises the shape: *"Adding a subject type is three coordinated
edits and one migration."* Verified against source, it is:

1. **`models.py`** — a `SUBJECT_SITE_GROUP = 'site_group'` constant and an entry in
   `SUBJECT_TYPE_CHOICES`.
2. **A migration. Yes, `SUBJECT_TYPE_CHOICES` is a database choices field.**
   `StatusTransition.subject_type` is
   `CharField(max_length=30, choices=SUBJECT_TYPE_CHOICES, db_index=True)`, and `choices` is a
   field argument, so adding a value emits an `AlterField`. It is metadata-only on PostgreSQL —
   Django does not render `choices` into DDL, so there is no table rewrite and no check
   constraint — but it is a migration under R-1 all the same.
3. **`utils.py`** — an entry in `_subject_type_registry()` mapping `SiteGroup →
   SUBJECT_SITE_GROUP`, **and** a decision about `_SUBJECT_PROJECT_RESOLVERS`. That second one is
   the awkward part: every existing resolver returns *one* `Project`, and **a `SiteGroup` has no
   single project.** The options are to register no resolver (`record_transition()` already
   handles that — it only calls one if registered) or `lambda s: None`. Either way the group's
   transitions never appear in any project's timeline, which is the `sttrans_project_idx` query.
   That is a real loss of legibility and should be a conscious choice, not a side effect.
4. **The call sites.** Exactly **one** status write exists: `design_views.site_group_lock`, which
   already runs inside `transaction.atomic()` and would take
   `record_transition(group, to_status=SITE_GROUP_LOCKED, from_status=SITE_GROUP_DRAFT,
   actor=profile, …)` beside its existing `group.save(update_fields=…)`. Optionally a creation
   transition (`from_status=''`, `reason_code=REASON_CREATED`) in `site_group_create`, matching
   how `Project` creation is instrumented.
5. **`docs/execution-model.md` §13** — move the `SiteGroup` row from the second table to the
   first.

**What `record_transition()` expects:** a model **instance** (never a string — it derives
`subject_type` from `type(subject)` via the registry and raises `ValueError` for an unregistered
class), a non-empty `to_status`, an optional `UserProfile` actor whose `.role` is *copied* into
`actor_role_code`, and **a caller-owned `transaction.atomic()` block** — it deliberately does not
open its own. It raises rather than swallows, by design (R-2/R-3).

One cost §13 does not mention: **`site_group_lock` is in `design_views.py`.** Instrumenting the
group means touching the scoped-out module — the same boundary Task C hits. If 1.1 is being given
permission to edit `design_views.py` for the change-request gate anyway, this is nearly free; if
it is not, this alone is not worth lifting the boundary for.

### Recommendation: **its own session, not 1.1**

**For putting it in 1.1:** the file is already open, the write surface is one line, and §13
nominates 1.1 as the owner.

**Against — and this is the side this audit comes down on:**

- **A migration that does two unrelated things is harder to review and harder to roll back**, and
  1.1's migration is *already* the risky one. It alters a load-bearing uniqueness guarantee and,
  per §5 above, **is not reversible once the feature is used**. Bundling an `AlterField` on a
  different table into that same one-way door means an operator who wants to undo the
  instrumentation cannot, because undoing it means reversing the constraint change too.
- The two changes have **no shared failure mode and no shared review question**. The constraint
  change needs a reviewer thinking about concurrency, ordering and `.first()`. The instrumentation
  needs a reviewer thinking about subject vocabulary and the project resolver. Asking one person
  to hold both at once is how `lambda s: None` gets waved through.
- The `_SUBJECT_PROJECT_RESOLVERS` question is **genuinely open** and deserves an answer rather
  than a default. §13 exists precisely so that a missing row is unambiguous; a subject type whose
  rows are all `project=NULL` is a new kind of ambiguity, and one to add deliberately if at all.
- 1.1's real content is D-1 and the gate audit. A group's state changes are already legible
  without the ledger: two states, one transition, one direction, no unlock, and
  `locked_by`/`locked_at` already stamped on the row. The ledger adds the actor's *role at the
  time* and a reason code — worth having, not urgent.

**Recommendation: 1.1 alters the constraint and fixes the callers, and nothing else. §13's
`SiteGroup` row is amended to state the reason it is still not instrumented — the same "a new
subject type is a schema change" reason already recorded for `PaymentRequest`, `TaskTemplate` and
`Checklist`.** That makes four models waiting on one small session that adds several subject
types together, which is a better-shaped session than four half-sessions.

---

## Task G — the regression net

### Which phase-0 files touch this area

**None of the six.** Grepped all six for `SiteGroup`, `group_memberships`, `boq_group_locked`,
`project_boq_is_group_locked` and `change_request`: **zero hits** in
`tests_residential_baseline.py`, `tests_access_isolation.py`, `tests_soft_delete.py`,
`tests_status_transition.py`, `tests_task_template.py` and `tests_checklist_snapshot.py`.

The coverage lives in four **older**, design-module files:

| File : class | Tests | Covers |
|---|---|---|
| `tests_design_groups.py : PostQCPoolTests` | 11 | `post_qc_pool()` — the exclusion subquery, live vs removed membership, locked groups, ordering, ageing |
| `tests_design_groups.py : CompletenessAndMembershipTests` | 3 | `tender_release_completeness()`, `_group_member_ids()` |
| `tests_design_part46.py : GroupLockTests` | 1 | The lock refused while a change request is pending, permitted once rejected |
| `tests_design_part11.py` | 1 | `test_15b_group_lock_still_beats_the_design_lock` |
| `tests_boq_upload.py : UploadBase._group_lock` | fixture | Feeds the upload screen's lock branch |

**So the regression net phase 1 inherits does not cover the thing phase 1 is about to change.**
1.1's blast radius is guarded by four design-module test files that predate the execution
programme, not by the net phase 0 built.

### Suite result

```
python manage.py test projects --settings=solarpms.test_settings
Ran 645 tests in 150.974s
FAILED (failures=1, errors=4)
```

`python manage.py check` — *System check identified no issues (0 silenced).*

**The known failure — confirmed present and unchanged:**

- `tests_design_part46.RaisingTests.test_02_a_second_pending_request_is_refused_by_the_database`
  — asserts `'uniq_pending_change_request_per_attempt'` appears in the `IntegrityError` text;
  SQLite says `UNIQUE constraint failed: projects_designchangerequest.attempt_id` instead. A
  message difference between backends, not a behaviour difference. The constraint fires.

**Four further errors — the stop condition, reported and not worked around:**

- `tests_residential_baseline.TaskProgressionTests.test_the_assigned_user_completes_a_checklist_item_with_a_photo`
- `tests_residential_baseline.TaskProgressionTests.test_a_checklist_item_cannot_be_checked_without_a_photo`
- `tests_soft_delete.DeletedProjectWriteRefusalTests.test_checklist_item_complete_refuses_a_deleted_project`
- `tests_soft_delete.LiveProjectStillWorksTests.test_a_live_project_still_takes_a_checklist_completion`

All four fail identically, at the **fixture line, not at an assertion**:

```
projects.models.TemplateVersionLocked: Cannot modify checklist item
'Earth resistance < 5 ohm': Pre-commissioning v1 (active) is 'active', not a draft.
```

Cause: the shared helper does `Checklist.objects.create(name=…, is_active=True)` — and since 0.5
`is_active` is a property shim whose setter writes `status='active'` — then adds a
`ChecklistItem` to it, which 0.5's R-7 guard correctly refuses.

**These are known and already written down.** `PHASE_0_COMPLETION.md`, §"One outstanding chore in
the net", names all four by test method, states the cause, and states the fix: *"All four are
fixed by reordering the fixture to create draft, add items, activate(). No assertion changes."*
The behaviour each asserts is meanwhile covered by `tests_checklist_snapshot.py`.

**So the prompt's premise — "confirm the only failure is the known one" — is itself out of
date.** The baseline is 1 known failure + 4 known, documented, unrelated fixture errors. This
session changed nothing and fixed nothing (R-12). Whether 1.1 clears the chore first is the
product owner's call; it is a two-file fixture reorder touching neither `SiteGroup` nor anything
this audit examined.

### Tests 1.1 will need to add

1. **The constraint itself, both directions.** One project may hold a live `procurement` **and** a
   live `execution` membership simultaneously; a second live membership *of the same type* is
   refused **by the database**. Assert on the `IntegrityError` *type*, not on the constraint name
   in its message — see the SQLite caveat above, which is a standing trap in this repo.
2. **`active_group_membership()` returns the procurement row when both exist** — and, the test
   that actually matters, **returns it regardless of insertion order**. Write it both ways:
   execution added first, then procurement added first. `Meta.ordering = ['added_at']` means a
   naive implementation passes one and fails the other.
3. **The change-request gate ignores execution membership.** A site in an *active execution group
   only*, with a `released` design, is **refused** a change request — the widening must not apply.
   And a site in a *locked procurement* group plus an execution group is refused with the **lock**
   message, whichever was added first.
4. **`remove_from_group()` from a change request removes only the procurement membership**, and
   leaves the execution one live.
5. **`post_qc_pool()` still lists a site that is in an execution group** but no procurement group.
   This is the silent-failure test.
6. **`project_boq_is_group_locked()` is `False` for a site in an execution group**, whatever that
   group's status string is.
7. **`_group_or_404` and the six views refuse an execution group.** In particular
   `site_group_lock` must 404 rather than lock one — it is a write path.
8. **Residential is still structurally excluded.** Re-assert `project_boq_is_group_locked()` is
   `False` on a Residential project, and that both locks are still *evaluated* on the Residential
   BOQ path (§3.3's property).

### Does any existing test pass only because two memberships are currently impossible?

The prompt flags this as the question that bites. The honest answer is **yes — one, and it is not
obvious.**

**`tests_design_groups.PostQCPoolTests.test_site_with_a_live_membership_is_excluded`.**

It asserts that a site with *a* live membership is out of the post-QC pool. That is correct today
because a live membership can only be procurement. It will **keep passing** after `group_type`
exists — and will then be asserting something the product does not want: that a site in a *PM
execution batch* is invisible to SCM's procurement queue. **The test does not break; it silently
changes meaning**, which is exactly the shape the prompt warns about (the two `tests_gantt.py`
tests that turned out to pin an access finding).

The fix is not to change what it asserts. It should be **narrowed to say `procurement`** —
construct the membership with an explicit procurement group — and a **sibling test added**: *a
site in an execution group is still in the pool*. Same rule as 0.2a: assert the relationship,
never the incidental gate.

Two near-misses, examined and cleared:

- `test_pool_is_not_empty_when_no_membership_rows_exist_at_all` and
  `test_a_locked_group_also_removes_a_site_from_the_pool` both stay correct **and** stay
  meaningful — one is about an empty table, the other about the locked-procurement case
  specifically.
- `tests_design_part46.GroupLockTests` and `tests_design_part11` 15b each construct a specific
  group and a specific membership. They become *incomplete* (they will not exercise the execution
  case) but never *wrong*.

---

## Task H — Q-E1 and B-05

Neither is answered here. What each **changes in 1.1**:

### Q-E1 — do Residential projects need execution grouping at all?

**It changes one field's nullability, in 1.1, and that field is `SiteGroup.program`.**

- **Answer "no"** — `program` stays non-nullable, 1.1's migration is exactly the sequence in
  Task E, and execution grouping is an OPEX/CAPEX-under-a-Program feature. Residential keeps the
  structural exclusion `RESIDENTIAL_BASELINE.md` §3.3 describes, unchanged.
- **Answer "yes"** — `program` must become **nullable**, which is a *second* `AlterField` in the
  same migration, plus a constraint question nobody has asked yet: *may a procurement group have
  a null program?* (Almost certainly not — procurement is per-tender by definition.) That means a
  conditional constraint of roughly the shape
  `Q(group_type='procurement', program__isnull=False) | Q(group_type='execution')` — a materially
  larger and riskier migration than the one proposed above. It also reopens `_add_sites()`'s
  `project.program_id != group.program_id` check, which is meaningless when both sides are null,
  and it puts Residential projects inside a code path that has never seen one.

**So Q-E1 genuinely blocks 1.1**, and it blocks it on the migration — the one artefact R-1 says
must be approved before anything else is written. It cannot be deferred to a later prompt.

### B-05 — do execution groups carry their own schedule and milestones?

**It changes nothing in 1.1, and the product owner should be told so.**

1.1's deliverable is `group_type`, the constraint, and the caller narrowing. None of that moves on
the answer:

- **"Only a grouping"** — `SiteGroup` gains `group_type` and nothing else.
- **"Its own schedule and milestones"** — that is `start_date`, `target_date`, a milestone
  relation, possibly its own `ExecutionGroupMilestone` table, and its own status vocabulary. Every
  bit of it is **additive**: new fields and new tables, none of which alters `group_type`, the
  membership constraint, or a single consumer in the Task B table.

One caveat worth naming, and it is about sequencing rather than schema. If the answer is "its own
lifecycle", D-1's *planning / active / closed* needs somewhere to live — and whether that is
`SiteGroup.status` (a union of values whose meaning depends on `group_type`, which breaches
**R-5**) or a separate field is a decision better taken **before** `group_type` ships than after,
because it decides whether `project_boq_is_group_locked()`'s `group__status='locked'` filter can
ever collide with an execution value. Task D recommends narrowing that predicate by type
regardless, which neutralises the collision either way.

**Recommendation to the product owner: B-05 does not block 1.1. It blocks 1.3 / 2.x, wherever
execution scheduling is actually built.** §8 currently lists it as blocking 1.1; on the evidence
of the code, it does not, and the table should be amended. **Q-E1 does block 1.1, and it is the
single answer 1.1 cannot start without.**

---

## Recorded in `EXECUTION_MODULE_DEFERRED.md`

Section **B** (phase 1), entries **B1** and **B2**. Neither was fixed (R-12).

---

## Session close

- No `.py`, `.html`, `.css` or `.js` file created or modified.
- No migration created; `makemigrations` was not run.
- `python manage.py check` — *System check identified no issues (0 silenced).*
- Full test suite run; result reported in Task G in full.
- Files written: `SITE_GROUP_AUDIT.md` (this file) and section B of
  `EXECUTION_MODULE_DEFERRED.md`.

**The audit is reviewed before the build is written. Prompt 1.1 was not begun.**
