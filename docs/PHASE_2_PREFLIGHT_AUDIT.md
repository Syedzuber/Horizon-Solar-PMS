# A-2.0 — Phase 2 pre-flight audit

**Read-only session, 3 Sep 2026.** One file written (this one); no application code, template,
migration, `test_settings.py`, `utils.py`, `views.py`, `design_views.py`, `models.py` or
`admin.py` was changed. Every claim below is a paste, a grep hit with file and line, or a
command's actual stdout. Where I could not run something I say so rather than describing what
it would have shown.

**Three documented claims turned out to be false, and one I filed as false is not — I
retracted it in §6.3 after re-measuring. They are collected in §6.** The most consequential
is §6.1: **`DESIGN_RELEASED` has a second route out, and it is not `_open_next_attempt()`.**
§5 gives the verdict on §B23; §1.6 is an addendum comparing what migration 0067 seeded
before and after 0074, which came out identical.

---

## 0 · Pre-flight

### Branch, status, HEAD

```
$ git rev-parse --abbrev-ref HEAD
main

$ git log -1 --format='%H%n%s%n%an%n%ad'
7b555bc334f75e234657a3e5527d64e9c7c518cf
Merge branch 'execution-phase-1'
Syed Zuber
Thu Sep 3 16:26:52 2026 +0530
```

`git status` at the start of this session was **not clean**: an uncommitted documentation
reorganisation (12 root `*.md` deleted, 15 files untracked under `docs/`, two of them new —
`docs/DEPLOY_PLAN.md` and `docs/PHASE_1_OUTCOME.md`). That move is untouched by this session
and is the reason `git status` cannot be clean after committing only this document. See §8.

### HOTFIX-1

**No commit in this repository has "hotfix" in its subject:**

```
$ git log --all --oneline --grep='HOTFIX' -i
(no output)
```

HOTFIX-1 is present on this branch as commit `45f11f3`, the second parent of HEAD:

```
$ git show --stat 45f11f3
45f11f3 A helper stops writing fields a migration's model has not got yet, and the chain becomes a test
Thu Sep 3 16:25:02 2026 +0530

 EXECUTION_MODULE_DEFERRED.md                       |  61 +++++++
 EXECUTION_PROMPT_LOG.md                            | 111 +++++++++++++
 docs/execution-model.md                            | 113 +++++++++++++
 .../0067_seed_residential_template_v1.py           |  19 +++
 projects/tests_migration_chain.py                  | 179 +++++++++++++++++++++
 projects/utils.py                                  | 116 +++++++++----
 solarpms/test_settings.py                          |  41 ++++-
 7 files changed, 607 insertions(+), 33 deletions(-)
```

What it changed, by file:

**1. `projects/utils.py` — the fix.** New function `kwargs_for_model_state()`, and all three
`create`/`bulk_create` calls in `seed_task_template_version()` routed through it:

```python
    names = {f.name for f in model._meta.get_fields()}
    missing = sorted(k for k in required if k not in names)
    if missing:
        raise TypeError(
            f'{model.__name__} (model state as of this migration) has no field(s) '
            f'{missing}. ...'
        )
    out = dict(required)
    out.update({k: v for k, v in optional.items() if k in names})
    return out
```

`is_mirror` and `is_payment_milestone` both moved into `optional=`.

**2. `projects/migrations/0067_seed_residential_template_v1.py` — comment only, +19 lines.**
The commit says so itself: *"NOTHING IN THIS FILE CHANGED except this comment — the
incompatibility was never in the migration."* It also records the licence: *"0067 has NOT
applied on production as of 03 Sep 2026 (production sits at 0066)."*

**3. `solarpms/test_settings.py` — docstring only (+41/-4). Migrations are still disabled.**
The only non-docstring change in the whole diff is a trailing newline on the
`PASSWORD_HASHERS` line:

```diff
-PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']
\ No newline at end of file
+PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']
```

**Answering the open question in the prompt directly: HOTFIX-1 did not re-enable migrations
under the test settings.** It rewrote the docstring to explain why not, and moved the
retirement of the file to §B31.

**4. `projects/tests_migration_chain.py` — new, 179 lines.** The claimed plug: shells out to
`manage.py migrate --run-syncdb` against a throwaway Postgres database under
`DJANGO_SETTINGS_MODULE=solarpms.settings`, so it is immune to the settings the suite was
launched with. It fails rather than skips when Postgres is unreachable.

---

## 1 · Q1 — test settings and the migration chain

### 1.1 `solarpms/test_settings.py` in full

```python
"""Test-only settings: run the suite on in-memory SQLite with migrations disabled.

⚠ THIS FILE IS WHY MIGRATION 0067 REACHED PRODUCTION. Read that before trusting a green
run from it. Disabling migrations means the schema is built directly from today's
`models.py`, so **no test run under these settings has ever executed a migration** — and on
03 Sep 2026 a migration that could not apply to an empty database took the site down with
all 1,060 tests passing. See rule **R-22** and **§18** in `docs/execution-model.md`.

    THE HOLE IS PLUGGED, BUT NOT BY THIS FILE. `projects/tests_migration_chain.py` runs
    the real chain against a throwaway Postgres database, in a subprocess under the REAL
    settings, so it holds even when the suite is launched with these ones. It lives in
    `projects/` on purpose, so an ordinary `manage.py test projects` picks it up and no
    session can miss it — about 11 seconds of the 75. Run it alone, in ~15 s, after
    touching a migration:  python manage.py test projects.tests_migration_chain

WHY THIS FILE STILL EXISTS. Both reasons the original docstring gave are gone: the
`CREATEDB` grant has been made (`ALTER ROLE solarpms_user CREATEDB`), and the Postgres-only
raw SQL it referred to is `0005_project_redesign`'s `DROP TABLE … CASCADE`, which is not a
problem on Postgres. On that reasoning this file should have been deleted on 03 Sep. It was
kept on two measured numbers instead:

  * the suite takes **~75 s** here and **~1,350 s** — 22 minutes — under real settings;
  * and under real settings it reports **3 failures and 308 errors**, none of them product
    defects. The suite is written against a schema with no rows in it, which is what this
    file produces. Run the real chain and the data migrations have run too, so **306 of the
    308 are one collision** — a shared fixture creating `BOQItemMaster` rows whose codes
    migrations 0047 and 0057 already seeded (`Key (code)=(OPX-001) already exists`), across
    seven test modules. The other two, and all three failures, are `TaskDurationTemplate`
    the same way: 0034 seeds 50 rows and the tests assert a count of 0.

It cuts both ways: `tests_design_part46`'s standing failure here — a constraint name SQLite
does not report — PASSES under Postgres. Neither run is a superset of the other.

Making the suite pass under real settings is a programme across every test module, not a
session, and it is recorded as **§B31** in `EXECUTION_MODULE_DEFERRED.md` with the trigger
that retires this file: **the two runs disagreeing on a failure that is not already on the
baseline list.**

Usage:  python manage.py test projects --settings=solarpms.test_settings
This module is additive — it imports the real settings and overrides only the DB and
migration machinery; production/dev config is untouched.
"""
from .settings import *  # noqa: F401,F403

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': ':memory:',
    }
}


class _DisableMigrations:
    def __contains__(self, item):
        return True

    def __getitem__(self, item):
        return None


MIGRATION_MODULES = _DisableMigrations()

# Faster hashing for the test users.
PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']
```

### 1.2 Are migrations disabled? Yes.

The mechanism is a `MIGRATION_MODULES` override backed by a sentinel class,
[test_settings.py:51-60](solarpms/test_settings.py#L51-L60):

```python
class _DisableMigrations:
    def __contains__(self, item):
        return True

    def __getitem__(self, item):
        return None


MIGRATION_MODULES = _DisableMigrations()
```

`__contains__` returning `True` for every app makes Django believe every app has a
`MIGRATION_MODULES` entry; `__getitem__` returning `None` makes that entry "no migration
module". Django then builds the schema from current model state with `create_all`.

**`_DisableMigrations` has no docstring.** The prompt asked me to quote "its docstring
verbatim"; the class carries none, and the docstring quoted in §1.1 is the module's. That is
the only docstring in the file.

### 1.3 The full chain, forward, from an empty database

`.env` carries two `DATABASE_URL` lines; line 3 (Railway production) is commented out, so
python-decouple resolves the local one. Checked before running anything:

```
$ grep -n 'DATABASE_URL' .env
3:#DATABASE_URL=postgresql://postgres:***@acela.proxy.rlwy.net:28397/railway
8:DATABASE_URL=postgresql://solarpms_user:***@123@localhost:5432/solarpms_local

$ python -c "from decouple import config; print(config('DATABASE_URL'))"
postgresql://solarpms_user:***@123@localhost:5432/solarpms_local
```

A scratch database `audit_chain_8d082c71` was created empty, then:

```
$ DATABASE_URL=<scratch> DJANGO_SETTINGS_MODULE=solarpms.settings \
    python manage.py migrate projects --noinput -v 1
EXIT=0
```

Raw tail, including 0067 and 0075 (stderr was empty):

```
  Applying projects.0064_boqitemmaster_is_mandatory... OK
  Applying projects.0065_statustransition... OK
  Applying projects.0066_task_template... OK
  Applying projects.0067_seed_residential_template_v1...  [0067] Seeded RESIDENTIAL v1 (active): 9 phases, 52 tasks.
  [0067] Backfill: 0 matched, 0 unmatched, 0 project(s) with >=1 unmatched task.
 OK
  Applying projects.0068_checklist_versioning... OK
  Applying projects.0069_backfill_checklist_versions...  [0069] Checklists migrated to v1: 0
  [0069] Completions backfilled with item_text_snapshot: 0
  [0069] Completions with a null item (already lost, left null): 0
 OK
  Applying projects.0070_checklist_drop_is_active... OK
  Applying projects.0071_site_group_type... OK
  Applying projects.0072_execution_capability_flags_and_stock_location... OK
  Applying projects.0073_task_dependencies... OK
  Applying projects.0074_is_mirror_and_coordinator_role... OK
  Applying projects.0075_seed_opex_template_v1...  [0075] Seeded OPEX v1 (active): 7 phases, 23 tasks, 8 mirrors.
 OK
```

**The chain applies from empty. No stop condition triggered.** The scratch database was
dropped afterwards.

### 1.5 Every migration that imports live application code

```
$ grep -rn -E '^\s*(from|import)\s+' projects/migrations/*.py \
    | grep -vE 'from django|import django|^\S+:[0-9]+:import (os|re|sys|json|uuid|datetime|decimal)\b|from decimal|from datetime|import uuid|from collections' \
    | grep -E 'projects|utils|models|permissions|notifications'

projects/migrations/0067_seed_residential_template_v1.py:31:from projects.utils import (
projects/migrations/0069_backfill_checklist_versions.py:8:from projects.models import derive_checklist_code
projects/migrations/0075_seed_opex_template_v1.py:46:from projects.utils import seed_task_template_version
```

Cross-checked with a second grep for any other reference to a `projects.` symbol in a
migration; every remaining hit is a schema string (`to='projects.userprofile'` and the
like). **Three migrations, and that is the whole population.** This matches the table in
`EXECUTION_MODULE_DEFERRED.md` §B30, which HOTFIX-1 wrote. Full symbol list:

| # | Migration | Line | Symbol | Kind | Compatible with the model state it runs against? |
|---|---|---|---|---|---|
| 1 | `0067_seed_residential_template_v1` | 31 | `utils.RESIDENTIAL_TEMPLATE_CODE` | `str` constant, [utils.py:1222](projects/utils.py#L1222) | Yes — no model touched. Exposure is semantic: it is the key of 0067's idempotency check (`TaskTemplate.objects.filter(code=...)`), so changing its VALUE silently makes 0067 re-seed a database that already has the template |
| 2 | `0067` | 31 | `utils.RESIDENTIAL_TEMPLATE_LABEL` | `str` constant, [utils.py:1223](projects/utils.py#L1223) | Yes — written into `TaskTemplate.label`, which exists at 0066 |
| 3 | `0067` | 31 | `utils._get_duration` | pure function, [utils.py:1021](projects/utils.py#L1021) | Yes — `(task_name, overrides)`, dict lookups only, touches no model |
| 4 | `0067` | 31 | `utils.build_residential_phases` | [utils.py:1057](projects/utils.py#L1057) | **Yes today, and this is the one §B30 does not name — see below** |
| 5 | `0067` | 31 | `utils.seed_task_template_version` | [utils.py:1293](projects/utils.py#L1293) | Yes — this was the failure, and every write now goes through `kwargs_for_model_state()`, which drops optional fields the handed-over model state lacks |
| 6 | `0069_backfill_checklist_versions` | 8 | `models.derive_checklist_code` | [models.py:2444](projects/models.py#L2444) | Yes — signature `(name, checklist_model, exclude_pk=None)`, takes its model class as an argument, and reads exactly one field: `Checklist.code`, added by 0068, one migration earlier. Breaks if `code` is renamed or dropped, or if the function grows a second field read |
| 7 | `0075_seed_opex_template_v1` | 46 | `utils.seed_task_template_version` | [utils.py:1293](projects/utils.py#L1293) | Yes — same helper, and the caller `is_mirror` was added for. Safe today because 0075 is the chain head; that stops being true the moment a 0076 adds a field to `TaskTemplateTask`, which is what the helper's `optional=` tolerance now absorbs |

**No second incompatible signature was found. The stop condition did not trigger.**

#### The gap in §B30's table: entry 4

§B30 records 0067's exposure as *"**WAS the failure.** Fixed at the helper by
`utils.kwargs_for_model_state()`; the migration body is unchanged."* That is not the whole
exposure. `build_residential_phases()` opens with a runtime import of the **concrete** model
and reads eight class constants off it:

```
$ sed -n '1057,1070p' projects/utils.py
def build_residential_phases():
    """
    Return the Residential EPC template as a list of phase dicts (9 phases / 52 tasks).
    ...
    Task is imported inside to avoid a module-level circular import.
    """
    from .models import Task

$ awk 'NR>=1057 && NR<=1222' projects/utils.py | grep -o 'Task\.[A-Z_]*' | sort | uniq -c
      1 Task.BD
      6 Task.DESIGN
      8 Task.EXTERNAL
      6 Task.FINANCE
     44 Task.INTERNAL
     14 Task.PM
     11 Task.SCM
     14 Task.SITE_ENGINEER
```

Those values are written into `TaskTemplateTask.assigned_role` and `.task_type`. The import
is inside the function body, so it is evaluated **when the migration runs**, against
today's `models.py`, not against the historical state.

`kwargs_for_model_state()` does not cover this: it protects against a missing *field*, not
against a changed or removed *constant*. Rename or delete any of those eight and 0067 raises
`AttributeError` on a fresh database — the exact failure mode of 03 Sep, in a different
costume. Change one's **value** and 0067 seeds a `assigned_role` that no longer appears in
`ROLE_CHOICES`; `choices` is not a database constraint, so that write succeeds silently.

This is not hypothetical drift. The constants block 0067 reads is under active edit by this
very programme — migration **0074** is named `is_mirror_and_coordinator_role`, and
[models.py:352](projects/models.py#L352) shows what it added:

```python
    PROJECT_COORDINATOR = 'Project Coordinator'   # 19 chars — fits max_length=20

    ROLE_CHOICES = [
        (PM,                  'PM'),
        (SITE_ENGINEER,       'Site Engineer'),
        ...
        (PROJECT_COORDINATOR, 'Project Coordinator'),
    ]
```

0074 only added a member, so nothing broke. A rename would not have been so kind.

For contrast, **0075 does not have this exposure**: it writes role and type as literal
strings and says so at [line 66](projects/migrations/0075_seed_opex_template_v1.py#L66) —
*"Roles use Task.ROLE_CHOICES values as literal strings"*. 0067 is the outlier.

**Recommended addition to §B30, not made here (no code or doc edits outside this file):**
the 0067 row should read "fixed for field writes; still reads eight live `Task` class
constants at migrate time", and the trigger should be **any rename or value change to a
`Task.ROLE_CHOICES` or `Task.TYPE_CHOICES` constant**, not just `Checklist.code`.

---

## 2 · Q2 — the status chokepoint

### 2.1 `_apply_task_status_change()` — [views.py:4062-4333](projects/views.py#L4062-L4333)

Signature and complete docstring, verbatim:

```python
def _apply_task_status_change(task, new_status, profile, request, project):
    """
    Apply a user-initiated change to a Task's status: validation, the field writes,
    the StatusTransition row, the ActivityLog row, and the downstream milestone sync
    and notification.

    THE ONE DECISION PATH FOR TASK STATUS (R-18). Single implementation shared by
    task_status_update (the project-overview row control) and task_detail_status_update
    (the task-detail status block) — two screens the same person uses interchangeably.
    Previously each was a ~180-line copy of the other, so a rule added to one was not
    enforced, merely avoidable. A NEW TASK-STATUS RULE IS ADDED HERE, NEVER TO A VIEW.

    Gates and response shaping stay with the callers, as in _apply_boq_acknowledgement:
    each keeps its own answer to "may you move this task" (role-or-PM on the overview,
    assignee-only on the detail page) and its own redirect or HTMX partial. This helper
    never returns an HttpResponse and does not know which screen called it.

    `task` is NOT refreshed here — `task.status` stays the pre-change value throughout,
    because it is the from-status every record_transition() call needs. Callers refresh
    before rendering.

    `project` is passed rather than read from `task.phase.project`: both callers already
    hold it, and dereferencing would cost a query on the hottest write path in the app.

    Emits its own user-facing messages so the wording of a refusal cannot drift between
    the two screens. Returns one of _TASK_STATUS_APPLIED, _TASK_STATUS_REFUSED or
    _TASK_STATUS_NEEDS_BLOCK_REASON — see those constants for why three.
    """
```

The body is 271 lines and is transcribed rung-by-rung in §4.1 rather than pasted twice; the
paste of every line that touches `request` follows below, which is what the question turns on.

### 2.2 Every use of `request` inside it

```
$ grep -n 'request' projects/views.py | awk -F: '$1>=4062 && $1<=4335'
4062: def _apply_task_status_change(task, new_status, profile, request, project):
4125:     request,                                     # -> messages.error(...)
4134:     messages.error(request, 'Invalid status value.')
4149:     request,                                     # -> messages.error(...)
4155:     _due_date_str = request.POST.get('due_date', '').strip()
4166:     messages.warning(request, 'Please set a due date before marking this task as In Progress.')
4181:     block_issue_title = request.POST.get('block_issue_title', '').strip()
4183:     messages.error(request, 'Please state the blocking issue before marking this task as Blocked.')
4200:     block_severity = request.POST.get('block_issue_severity', Issue.HIGH)
4204:     block_assignee_id = request.POST.get('block_issue_assigned_to', '').strip()
4215:     description=request.POST.get('block_issue_description', '').strip(),
4231:     messages.success(request, f'Task blocked. Issue "{block_issue_title}" created.')
4255:     _ar_str    = request.POST.get('amount_received', '').strip()
4256:     _vr_str    = request.POST.get('variance_reason', '').strip()
```

Thirteen uses, in exactly two categories:

| Line | Use | Category | Non-HTTP equivalent already present? |
|---|---|---|---|
| 4125 | mirror refusal message | messages | Yes — the outcome is the return code `_TASK_STATUS_REFUSED`, which both callers already read; the message is presentation |
| 4134 | invalid-status message | messages | Yes, same |
| 4149 | illegal-transition message | messages | Yes, same |
| 4155 | `POST['due_date']` | form input | Yes — an ordinary value; parsed by `date.fromisoformat` and written by `Task.objects.filter(...).update()` |
| 4166 | no-due-date warning | messages | Yes, same as above |
| 4181 | `POST['block_issue_title']` | form input | Yes — a value |
| 4183 | missing-block-reason message | messages | Yes — `_TASK_STATUS_NEEDS_BLOCK_REASON` already carries this outcome |
| 4200 | `POST['block_issue_severity']` | form input | Yes — already defaulted to `Issue.HIGH` |
| 4204 | `POST['block_issue_assigned_to']` | form input | Yes — already optional |
| 4215 | `POST['block_issue_description']` | form input | Yes — already optional |
| 4231 | task-blocked success message | messages | Yes — `_TASK_STATUS_APPLIED` |
| 4255 | `POST['amount_received']` | form input | Yes — already optional |
| 4256 | `POST['variance_reason']` | form input | Yes — already optional |

**There is no third category.** In particular:

* **No `request.user` anywhere in the function.** Actor identity arrives as the separate
  `profile` argument, and that is what `record_transition(actor=profile)` and
  `log_activity(project, profile, ...)` receive. Both callers resolve `request.user.profile`
  themselves, at [views.py:4398](projects/views.py#L4398) and
  [views.py:4449](projects/views.py#L4449) — outside the helper.
* **No permission check.** The docstring says so: *"Gates and response shaping stay with the
  callers … each keeps its own answer to 'may you move this task'."* Verified: the role-or-PM
  gate is at [views.py:4390](projects/views.py#L4390), the assignee-only gate at
  [views.py:4444](projects/views.py#L4444). Neither is inside the helper.
* **No session, no cookies, no `request.META`, no `_is_hx(request)`.** `_is_hx()` is called
  only by the callers, after the helper returns.

### 2.3 `record_transition()` — [utils.py:439-551](projects/utils.py#L439-L551)

```python
def record_transition(subject, to_status, from_status='', actor=None,
                      reason_code='', remark='', project=None,
                      client_uuid=None, occurred_at=None):
    """
    Write one StatusTransition row. MUST be called inside the same
    transaction.atomic() block as the status change it records.

    A transition row without its status change, or a status change without its
    row, is worse than neither ...

    THIS FUNCTION RAISES. IT NEVER SWALLOWS. That is the whole difference from
    log_activity(), which catches bare `Exception` and logs it ...

    Args:
        subject:     the model INSTANCE whose status changed. Its class decides
                     subject_type via _subject_type_registry() — never a string.
        to_status:   required, the new status.
        from_status: the previous status; '' for a creation transition.
        actor:       UserProfile, or None where no human acted (the Zoho
                     webhook). actor.role is COPIED into actor_role_code, never
                     joined, so a later role change cannot rewrite history.
        reason_code: one of the REASON_* constants in models.
        remark:      free text. Mandatory for subject types listed in
                     REMARK_REQUIRED_SUBJECT_TYPES (R-9) — empty today.
        project:     override for the derived project. Pass it only where the
                     resolver cannot work (e.g. a subject not yet saved).
        client_uuid: R-14 idempotency key. A repeat is IGNORED — same row
                     returned, nothing duplicated, no exception.
        occurred_at: override for "now", for a replayed offline submission.

    Returns the StatusTransition row (existing one on an idempotent repeat).
    """
    from django.db import IntegrityError
    from .models import (
        StatusTransition, ACTOR_ROLE_SYSTEM, REMARK_REQUIRED_SUBJECT_TYPES,
    )

    subject_type = _subject_type_registry().get(type(subject))
    if subject_type is None:
        raise ValueError(...)                      # not an instrumented subject type

    if not to_status:
        raise ValueError('record_transition(): to_status is required.')

    if subject_type in REMARK_REQUIRED_SUBJECT_TYPES and not (remark or '').strip():
        raise ValueError(...)                      # R-9

    if client_uuid:
        existing = StatusTransition.objects.filter(client_uuid=client_uuid).first()
        if existing is not None:
            return existing

    if project is None:
        resolver = _SUBJECT_PROJECT_RESOLVERS.get(type(subject).__name__)
        if resolver is not None:
            project = resolver(subject)

    # Copied, not joined. A departed or re-roled user must not be able to change
    # what this row says they were at the time.
    if actor is not None:
        actor_role_code = actor.role or ACTOR_ROLE_SYSTEM
    else:
        actor_role_code = ACTOR_ROLE_SYSTEM

    row = StatusTransition(
        subject_type=subject_type, subject_id=subject.pk, project=project,
        from_status=from_status or '', to_status=to_status,
        actor=actor, actor_role_code=actor_role_code,
        reason_code=reason_code or '', remark=remark or '',
        client_uuid=client_uuid or None,
        occurred_at=occurred_at or timezone.now(),
    )

    if not client_uuid:
        row.save()
        return row

    try:
        with transaction.atomic():
            row.save()
        return row
    except IntegrityError:
        existing = StatusTransition.objects.filter(client_uuid=client_uuid).first()
        if existing is not None:
            return existing
        raise  # some other integrity problem — never swallow it
```

**What it needs to write a row:** a model instance registered in `_subject_type_registry()`,
a non-empty `to_status`, and an open `transaction.atomic()` owned by the caller. Everything
else is optional and defaulted.

**Is any of it HTTP-bound? No — none of it.** `actor` is a `UserProfile` or `None`, and
`None` is explicitly supported: it stamps `ACTOR_ROLE_SYSTEM`, and the docstring names the
Zoho webhook as the existing non-human caller. `occurred_at` exists precisely so a replayed
offline submission can supply its own timestamp. This function was written to be callable
from outside a request.

### 2.4 Every caller of `_apply_task_status_change()`

```
$ grep -rn '_apply_task_status_change(' --include=*.py . | grep -v '/venv/'
projects/views.py:4062:            def _apply_task_status_change(task, new_status, profile, request, project):
projects/views.py:4397:            outcome = _apply_task_status_change(
projects/views.py:4448:            _apply_task_status_change(
projects/tests_role_mapping.py:253:      _apply_task_status_change(
```

| Caller | File and line | Context |
|---|---|---|
| `task_status_update` | [views.py:4397](projects/views.py#L4397); view defined at [4337](projects/views.py#L4337) | `@login_required` view, real request |
| `task_detail_status_update` | [views.py:4448](projects/views.py#L4448); view defined at [4420](projects/views.py#L4420) | `@login_required` view, real request |
| `test_helper_applies_a_coordinator_status_change` | [tests_role_mapping.py:253](projects/tests_role_mapping.py#L253) | **no view, no HTTP** |

Two product callers, and **one existing non-HTTP caller that already works and is in the
passing suite**:

```python
    # The helper itself, called directly — no view, no gate, no HTTP. Proves
    # _apply_task_status_change() accepts a coordinator profile and that the write
    # lands, independent of either caller's permission model.
    def test_helper_applies_a_coordinator_status_change(self):
        from django.test import RequestFactory
        from django.contrib.messages.storage.fallback import FallbackStorage

        request = RequestFactory().post('/')
        request.user = self.member_coord_user
        request.session = self.client.session
        request._messages = FallbackStorage(request)

        _apply_task_status_change(
            self.coord_task, Task.IN_PROGRESS, self.member_coord, request, self.project,
        )
        self.coord_task.refresh_from_db()
        self.assertEqual(self.coord_task.status, Task.IN_PROGRESS)
```

Note what it did **not** need. It sets `request.user` only because `RequestFactory` leaves it
unset and the author was being careful — nothing in the helper reads it (§2.2). What it
actually needed was `request.POST` (supplied empty by `RequestFactory().post('/')`) and
`request._messages`.

### 2.5 What §B23 reads as forbidding non-HTTP callers, and whether code enforces it

The claim, quoted verbatim from `docs/EXECUTION_MODULE_DEFERRED.md` §B23 item 6:

> **6. Task status changes require a `request`.** `_apply_task_status_change()` is the one
> decision path for task status (R-18) and correctly so, but it reads `request.POST` for the
> block reason and the inline due date and writes `messages`, so no command, job or
> derivation hook can call it. That matters beyond seeding: the mirror **derivation** hooks
> of phases 3–5 will write mirror statuses from source events, and the docstring already
> says they "will not call this function". So the rule set that lives in
> `_apply_task_status_change` — the transition table, `completed_at`, `blocked_since` — has
> no non-HTTP home, and the derivation hooks will have to restate it or diverge from it.

The "docstring" §B23 quotes is not the function docstring; it is the rung-0 comment at
[views.py:4118-4121](projects/views.py#L4118-L4121):

> NOT THE WHOLE FEATURE. The derivation hooks that will WRITE these statuses are unbuilt.
> They belong to the source objects (phases 3-5), go through `record_transition()` like every
> other status change, and carry the SOURCE EVENT's actor (spec §2.4, §2.8) — **they will not
> call this function, which exists to say no to people.**

**Is it enforced anywhere in code? No. It is prose only.** Specifically:

* **no assert** — there is no `assert` statement between lines 4062 and 4333;
* **no type hint** — the signature is bare:
  `def _apply_task_status_change(task, new_status, profile, request, project):`;
* **no early access to `request.user`** — `request.user` does not appear in the function at
  all (§2.2);
* **no `isinstance(request, HttpRequest)` check**, no `hasattr` probe, nothing.

`request` is a duck-typed bag from which the function pulls `.POST` (anything with `.get`)
and which it hands to `django.contrib.messages`. `tests_role_mapping.py:253` demonstrates in
the existing, passing suite that a `RequestFactory` object satisfies both.

**A correction to the §B23 text itself, which cuts in favour of the hooks:** "reads
`request.POST` for the block reason and the inline due date" undercounts — there are **seven**
`request.POST` reads, not two (`due_date`, `block_issue_title`, `block_issue_severity`,
`block_issue_assigned_to`, `block_issue_description`, `amount_received`, `variance_reason`).
Five of the seven are already optional with defaults, so the true barrier is smaller than
§B23 describes, not larger.

---

## 3 · Q3 — the Design hook's actual calling context

### 3.1 Every path that sets `DESIGN_RELEASED`, and every path that leaves it

Of the 30 `DESIGN_RELEASED` hits outside tests, **exactly one is a product write**:

| Path | File and line | Kind |
|---|---|---|
| `assignment.status = DESIGN_RELEASED` | [design_views.py:2939](projects/design_views.py#L2939) | **the only product write** |
| `status=DESIGN_RELEASED` in `DesignAssignment.objects.create(...)` | [seed_opex_test_data.py:535](projects/management/commands/seed_opex_test_data.py#L535) | management command (dev tooling) |
| `status=DESIGN_RELEASED` in `DesignAssignment.objects.create(...)` | [seed_scm_handoff_data.py:211](projects/management/commands/seed_scm_handoff_data.py#L211) | management command (dev tooling) |

Every remaining hit is a comparison, a queryset filter, or a template flag. The single write,
in context:

```python
@login_required
def design_head_qc_pass(request, project_id):
    """GATE 2 — the DESIGN HEAD passes a package Design QC has already passed. RELEASE.
    ...
    now = timezone.now()
    with transaction.atomic():
        attempt.head_verdict     = QC_PASSED
        ...
        assignment.released_at = now
        assignment.released_by = profile
        assignment.status      = DESIGN_RELEASED
        assignment.save(update_fields=['released_at', 'released_by', 'status', 'updated_at'])
```

There are no signals on `DesignAssignment` — `projects/signals.py` registers only `post_save`
on `auth.User`, `user_logged_in` and `user_logged_out` — no Celery, and no `apps.ready()`
hook beyond importing that module. Nothing writes this status asynchronously.

#### Leaving `released` — the handover's claim, and what the code has

The handover names `design_change_request_accept → _open_next_attempt()` as **the** reopen
chokepoint. `docs/OPEX_TEMPLATE_AUDIT.md` states it twice:

> *"But there is one route out of `released`, and it matters to the spec"*
> — [OPEX_TEMPLATE_AUDIT.md:97](docs/OPEX_TEMPLATE_AUDIT.md#L97)
>
> *"**`DESIGN_RELEASED` is not terminal** — one reopen route exists
> (`design_change_request_accept` → `_open_next_attempt()`)."*
> — [OPEX_TEMPLATE_AUDIT.md:836](docs/OPEX_TEMPLATE_AUDIT.md#L836)

I enumerated every write to `DesignAssignment.status` rather than assume it:

```
design_views.py:462   DESIGN_AWAITING_ALLOCATION   in design_survey_upload      (view, :401)
design_views.py:582   DESIGN_AWAITING_ALLOCATION   in design_survey_link_set    (view, :499)
design_views.py:714   DESIGN_IN_DESIGN             in _allocate_one             (helper, :634)
design_views.py:1319  DESIGN_SURVEY_RETURNED       in design_mark_blocked       (view, :1290)
design_views.py:1572  DESIGN_ARTIFACTS_UPLOADED    (view)
design_views.py:1859  DESIGN_ARKA_SUBMITTED        (view)
design_views.py:1967  DESIGN_AWAITING_HEAD_ARKA    (view)
design_views.py:2034  DESIGN_ARKA_REJECTED         (view)
design_views.py:2087  DESIGN_ARKA_SUBMITTED        (view)
design_views.py:2161  DESIGN_ARKA_REJECTED         (view)
design_views.py:2719  DESIGN_IN_QC                 (view)
design_views.py:2790  DESIGN_AWAITING_HEAD_QC      (view)
design_views.py:2872  DESIGN_QC_FAILED             in design_qc_fail            (view, :2805)
design_views.py:2939  DESIGN_RELEASED              in design_head_qc_pass       (view, :2892)
design_views.py:3018  DESIGN_QC_FAILED             in design_head_qc_fail       (view, :2952)
```

plus one the pattern misses because the value is a variable —
[design_views.py:2576](projects/design_views.py#L2576), inside `_open_next_attempt()`:

```python
    opening_status = (DESIGN_ARKA_SUBMITTED
                      if (carried_arka is not None
                          and carried_arka.head_verdict == ARKA_APPROVED)
                      else DESIGN_IN_DESIGN)

    assignment.current_attempt_number = next_number
    assignment.status = opening_status
    assignment.save(update_fields=['current_attempt_number', 'status', 'updated_at'])
```

Which of these can fire while `status == released`:

* `_allocate_one` **cannot** — it refuses first at
  [design_views.py:700](projects/design_views.py#L700), because
  `REALLOCATABLE_STATUSES = (awaiting_allocation, allocated, due_date_proposed, in_design)`
  ([design_views.py:110](projects/design_views.py#L110)) excludes `released`.
* `design_due_date_change` **cannot** — explicit guard at
  [design_views.py:1249](projects/design_views.py#L1249).
* `design_change_request` admits a released assignment only through the draft-group carve-out
  ([design_views.py:3123-3130](projects/design_views.py#L3123-L3130)); the Head accepting it
  at [:3251](projects/design_views.py#L3251) then calls `_open_next_attempt()`. **This is the
  route the handover names, and it is real.**
* **`design_mark_blocked` ([design_views.py:1290](projects/design_views.py#L1290)) has no
  guard against `released` at all.** A second route — see §6.1.

`_open_next_attempt()` itself has **three** callers, not one:

```
projects/design_views.py:2520:def _open_next_attempt(assignment, reason, actor, detail, redo=None):
projects/design_views.py:2881:  _open_next_attempt(   # in design_qc_fail                (:2805)
projects/design_views.py:3028:  _open_next_attempt(   # in design_head_qc_fail           (:2952)
projects/design_views.py:3293:  _open_next_attempt(   # in design_change_request_accept  (:3251)
```

Only the third is reachable from `released`; the two QC-fail callers act on `in_qc` /
`awaiting_head_qc` packages, and a pending change request blocks a verdict at both gates in
any case (`_pending_change_requests` docstring, [design_views.py:2595](projects/design_views.py#L2595)).
So the handover is right about `_open_next_attempt()` and **incomplete about the state
machine** — §6.1.

### 3.2 Does each path execute inside a Django view with `request` in scope?

| # | Path | View | Line | `request` in scope? |
|---|---|---|---|---|
| 1 | set `released` | `design_head_qc_pass` | [design_views.py:2892](projects/design_views.py#L2892), `@login_required`; write at [:2939](projects/design_views.py#L2939) | **Yes** |
| 2 | reopen via change request | `design_change_request_accept` | [design_views.py:3251](projects/design_views.py#L3251), `@login_required`; `_open_next_attempt()` at [:3293](projects/design_views.py#L3293) | **Yes** |
| 3 | leave `released` via Design Hold | `design_mark_blocked` | [design_views.py:1290](projects/design_views.py#L1290), `@login_required`; write at [:1319](projects/design_views.py#L1319) | **Yes** |
| 4 | return from Design Hold | `design_survey_upload` | [design_views.py:401](projects/design_views.py#L401), `@login_required`; write at [:457](projects/design_views.py#L457) via `_status_after_unblock()` | **Yes** |
| — | seed data | `seed_opex_test_data`, `seed_scm_handoff_data` | management commands | No — dev tooling, marked `# NO PRODUCT PATH`, writes no mirror |

Worth stating separately: **`_open_next_attempt()` already takes `actor` (a `UserProfile`),
not `request`** — `def _open_next_attempt(assignment, reason, actor, detail, redo=None)` at
[design_views.py:2520](projects/design_views.py#L2520) — and calls
`log_activity(assignment.project, actor, ...)`. The one shared reopen function is already
free of HTTP.

### 3.3 Does §B23's problem exist for the Design hook at all?

**No.**

**Every product write to `DesignAssignment.status` — all sixteen, in both directions,
including all four paths into and out of `released` — happens inside an `@login_required`
Django view with a live `request` in scope.** No signals on the model, no Celery, no
scheduled job, no management command on the product path. A Design-mirror derivation hook
called from any of those four views holds the same `request` the view already has, and can
pass it straight to `_apply_task_status_change()`.

Two things make that stronger rather than weaker:

1. The hook does not need a *real* `HttpRequest`. §2.2 shows the helper never touches
   `request.user`, and `tests_role_mapping.py:253` already calls it with a `RequestFactory`
   object in the passing suite.
2. The hook does not need `request.POST` to carry anything. A mirror moving to `Done` or back
   to `In Progress` supplies none of the seven POST keys: `due_date` is read only when moving
   to `In Progress` **and** `task.due_date` is empty, and the five block keys only on a fresh
   transition to `Blocked`, which a Design mirror never makes.

**Where §B23 still applies, and why.** The eight mirrors seeded by 0075
([lines 77, 114-117, 166, 174, 176](projects/migrations/0075_seed_opex_template_v1.py#L77)):

| Mirror | Source | Does §B23 apply? |
|---|---|---|
| Design | `DesignAssignment.status` | **No** — every writer is a view (§3.2) |
| Delivery — Solar Panels | `DCLineItem` accepted qty | **No, on the same evidence.** `DCLineItem` rows are created in `create_delivery_challan` ([views.py:9562](projects/views.py#L9562); create at [:9668](projects/views.py#L9668)) and received/damaged quantities written in `confirm_grn` ([views.py:9730](projects/views.py#L9730); fields set at [:9803-9805](projects/views.py#L9803-L9805)). Both are views |
| Delivery — Inverters | " | **No** — same two views |
| Delivery — BOS Kit | " | **No** — same two views |
| Delivery — MMS | " | **No** — same two views |
| COD | commissioning record, phase 5.3 | **Unknown — the source does not exist yet.** §B23 here is a forecast about unwritten code |
| As-Built Drawings | design workspace, post-commissioning | **Unknown**, same |
| HOTO | handover record, phase 5.3 | **Unknown**, same |

So §B23 item 6 applies to **none of the five mirrors whose sources exist today**, and to three
whose sources are unbuilt, where it is a prediction rather than a finding. Its one
demonstrated instance is the demo seed (`seed_opex_test_data.py`) — dev tooling, and the
context §B23 was written in.

---

## 4 · Q4 — where two-step completion lands

### 4.1 The rungs of `_apply_task_status_change()`, in order

| # | Line | What it does or refuses |
|---|---|---|
| 0 | [4123](projects/views.py#L4123) | **Mirror refusal (B22, R-18, R-20).** `if task.is_mirror:` → message + `_TASK_STATUS_REFUSED`. Deliberately above everything, including the inline `due_date` write, so a refused mirror move writes nothing |
| 1 | [4132-4135](projects/views.py#L4132-L4135) | `new_status` must be in `Task.STATUS_CHOICES`, else `'Invalid status value.'` + `REFUSED` |
| 2 | [4139-4152](projects/views.py#L4139-L4152) | **The transition table.** `VALID_TRANSITIONS = {NOT_STARTED: {IN_PROGRESS, BLOCKED, DONE}, IN_PROGRESS: {DONE, BLOCKED}, BLOCKED: {IN_PROGRESS, BLOCKED}, DONE: {BLOCKED}}`; anything else → `REFUSED` |
| 2b | [4155-4162](projects/views.py#L4155-L4162) | **The inline `due_date` write.** Not a refusal — a `Task.objects.filter(pk=...).update(due_date=...)` that lands *before* rung 3 can refuse. Only when the POST carries `due_date`, the target is `In Progress`, and `task.due_date` is empty |
| 3 | [4165-4167](projects/views.py#L4165-L4167) | `In Progress` requires a due date, else warning + `REFUSED` |
| 3b | [4169-4177](projects/views.py#L4169-L4177) | Builds `update_kwargs`: `status`; `completed_at=now()` on `Done`; `blocked_since=now()` on a fresh block; `blocked_since=None` on unblock |
| 4 | [4180-4184](projects/views.py#L4180-L4184) | Fresh `Blocked` requires a stated issue, else `_TASK_STATUS_NEEDS_BLOCK_REASON` |
| 4b | [4190-4196](projects/views.py#L4190-L4196) | **Blocked branch, R-2 tight atomic:** `Task…update(**update_kwargs)` + `record_transition(reason_code=REASON_BLOCKED, remark=block_issue_title)` |
| 4c | [4200-4232](projects/views.py#L4200-L4232) | Creates the `Issue` (own atomic + its own creation `record_transition`), `log_activity(action_code='issue_created')`, returns `APPLIED` |
| 5 | [4235-4241](projects/views.py#L4235-L4241) | **Main branch, R-2 tight atomic:** `Task…update(**update_kwargs)` + `record_transition(reason_code=REASON_UNBLOCKED if leaving Blocked else '')` |
| 6 | [4246-4247](projects/views.py#L4246-L4247) | `log_activity(action_code=f'task_status_{new_status…}')` |
| 7 | [4252-4298](projects/views.py#L4252-L4298) | Finance-task → `PaymentMilestone` sync (`Received`), its `record_transition` rows and an attribution `log_activity` — the whole block wrapped in `try / except Exception: pass` |
| 8 | [4302-4331](projects/views.py#L4302-L4331) | Payment-milestone notification fan-out to Finance, PMs, BD and CEO |
| 9 | [4333](projects/views.py#L4333) | `return _TASK_STATUS_APPLIED` |

### 4.2 Where a two-step completion gate would insert, and what `Task` lacks

**Insertion point: rung 2, [views.py:4139](projects/views.py#L4139).** The
`VALID_TRANSITIONS` dict is the state machine, and a submit-then-approve flow is a new node
in it plus new edges — not a new refusal bolted on elsewhere. R-18 is explicit that a new
task-status rule is added here and never in a view.

Three consequences that follow from the rung order above, stated because they are structural
and not design choices:

* **`completed_at` moves.** It is stamped at rung 3b on any move to `Done`. With two steps,
  `Done` is reached by the approver, so `completed_at` follows the approval; the submission
  needs its own timestamp.
* **Rungs 7 and 8 must not fire on submission.** Both are gated on
  `new_status == Task.DONE` — the Finance→milestone sync and the payment-milestone
  notification. If the submitted state is not `Done`, they stay put with no edit; if it is,
  an engineer's submission would flip a payment milestone to `Received`.
* **"May this person approve" is a task-status rule, so by R-18 it belongs in this helper —
  but the docstring puts every "may you move this task" gate in the callers.** That tension
  is real and unresolved; it is a decision for the build prompt, not this audit.

**What `Task` does not have today.** The full field list
([models.py:385-446](projects/models.py#L385-L446)) is: `phase`, `task_name`, `task_order`,
`assigned_role`, `assigned_to`, `status`, `task_type`, `duration_days`, `due_date`,
`completed_at`, `blocked_since`, `is_payment_milestone`, `is_mirror`, `template_task`,
`created_at`. Missing, and required:

1. **A fifth status value.** `STATUS_CHOICES` is `Not Started / In Progress / Done / Blocked`
   ([models.py:364-374](projects/models.py#L364-L374)); there is no "submitted" or "awaiting
   approval" node for rung 2's table to point at. `status` is `max_length=20`, which
   constrains the wording.
2. **Who submitted, and when.** `completed_at` is the approval timestamp under any two-step
   reading; the submission has no field. `assigned_to` is the doer, not the submitter of
   record.
3. **Who approved, and when.** No approver FK exists. `Task` has exactly one `UserProfile` FK
   (`assigned_to`); `record_transition(actor=…)` records the mover in the ledger, but nothing
   on `Task` carries the approving authority, so no queryset can filter "approved by me".
4. **Which tasks require the gate.** `is_payment_milestone` and `is_mirror` are the only
   per-task booleans. Nothing marks a task as needing approval, and nothing on
   `TaskTemplateTask` ([models.py:2121](projects/models.py#L2121)) would seed such a flag —
   so the gate is currently all-tasks-or-none.

Not designed here, per the prompt. Named only.

---

## 5 · Verdict on §B23

**§B23 item 6 FALLS for every mirror whose source exists today (Design and the four
Delivery mirrors), and is UNPROVEN — not standing — for the three whose sources are unbuilt
(COD, As-Built Drawings, HOTO).**

The two premises it rests on are both false as stated:

* *"no command, job or derivation hook can call it"* — `tests_role_mapping.py:253` calls it
  today, from no view, with a `RequestFactory` object, and the write lands. The suite passes.
* *"derivation hooks fire outside a request"* — every product writer of every existing mirror
  source is an `@login_required` Django view (§3.2, §3.3).

What survives of §B23 is narrower and still worth keeping: `_apply_task_status_change()`
takes an argument it does not need in the shape it names, so its **signature** advertises an
HTTP coupling it does not have. That is a naming and ergonomics problem, not a blocker, and
the three unbuilt sources are the place to decide it — before they are written, not after.

---

## 6 · Contradictions with the documents

### 6.1 `DESIGN_RELEASED` has a SECOND route out, and it is not `_open_next_attempt()`

**`docs/OPEX_TEMPLATE_AUDIT.md` says "one route out of `released`" ([:97](docs/OPEX_TEMPLATE_AUDIT.md#L97)) and "one reopen route exists (`design_change_request_accept` → `_open_next_attempt()`)" ([:836](docs/OPEX_TEMPLATE_AUDIT.md#L836)). That is false. There are two, and the second bypasses `_open_next_attempt()` entirely.**

`design_mark_blocked` in full — [design_views.py:1289-1330](projects/design_views.py#L1289-L1330):

```python
@login_required
def design_mark_blocked(request, project_id):
    """The allocated DESIGNER marks the site blocked on an inadequate survey. ..."""
    project = _opex_site(project_id)
    assignment = getattr(project, 'design_assignment', None)
    if assignment is None or not user_is_assigned_designer(request.user, assignment):
        return HttpResponseForbidden('Only the designer allocated to this site may flag it.')
    if request.method != 'POST':
        return redirect('design_my_sites')

    if assignment.status == DESIGN_SURVEY_RETURNED:
        return _deny(request, f'{project.project_id} is already on Design Hold.',
                     'design_my_sites')

    reason = (request.POST.get('reason') or '').strip()
    if not reason:
        return _deny(request, 'Please say what is inadequate about the survey.',
                     'design_my_sites')

    profile = request.user.profile
    with transaction.atomic():
        assignment.survey_returned_at    = timezone.now()
        assignment.survey_returned_by    = profile
        assignment.survey_return_reason  = reason
        assignment.status = DESIGN_SURVEY_RETURNED
        assignment.save()
        log_activity(project, profile, f'Site placed on Design Hold — survey inadequate: {reason}',
                     entity_type='DesignAssignment', entity_id=assignment.pk,
                     action_code='design_blocked')
```

```
$ awk 'NR>=1286 && NR<=1332 && /RELEASED/' projects/design_views.py
(no output)
```

**There is no `released` guard.** The four gates are: allocated-designer identity, POST,
already-on-hold, and a non-empty reason. `released` fails none of them, and release does not
clear `assigned_to` — [design_views.py:2939-2940](projects/design_views.py#L2939-L2940) saves
only `released_at`, `released_by`, `status`, `updated_at`. So the designer of a released site
can put it back on Design Hold. Compare `design_due_date_change`, which *does* guard
([design_views.py:1249](projects/design_views.py#L1249)): the guard was written once and not
generalised.

It then comes back the other way. The Head clears the hold by uploading a replacement survey
(`design_survey_upload`, [design_views.py:401](projects/design_views.py#L401)), which calls
`_status_after_unblock()` — [design_views.py:239-260](projects/design_views.py#L239-L260):

```python
def _status_after_unblock(assignment):
    if assignment.assigned_to_id is None:
        return DESIGN_AWAITING_ALLOCATION
    if _effective_commitment(assignment) is not None:
        return DESIGN_IN_DESIGN
    return DESIGN_ALLOCATED
```

A released site has both a designer and an approved commitment, so it returns to
**`in_design`** — the same status `_open_next_attempt()` would have written, reached without
opening an attempt, without a change request, and without
`action_code='design_attempt_opened_*'`. `released_at` and `released_by` stay stamped. And
because `in_design` **is** in `REALLOCATABLE_STATUSES`
([design_views.py:110](projects/design_views.py#L110)), the site is now reallocatable to a
different designer.

**Why this matters to phase 2, concretely.** A Design-mirror derivation hook hung on
`_open_next_attempt()` — which is what the audit document recommends, calling it *"a usable
hook point"* ([OPEX_TEMPLATE_AUDIT.md:108](docs/OPEX_TEMPLATE_AUDIT.md#L108)) — **would not
fire on this route.** The mirror task would sit at `Done` while the design it mirrors is back
in `in_design`: exactly the "a mirror can disagree with its source, and then neither number
means anything" failure that rung 0 of `_apply_task_status_change()` exists to prevent.

Two hook points are needed, or one placed on `DesignAssignment.status` itself rather than on
the attempt lifecycle. **Not designed here, and no code was changed.** I also did not build a
fixture to execute the sequence end-to-end — this is a code-reading finding, and the guard
set quoted above is the whole of the evidence.

### 6.2 §B30's exposure table understates migration 0067

Detailed in §1.5. §B30 records 0067 as *"Fixed at the helper by
`utils.kwargs_for_model_state()`"*, but `build_residential_phases()` still reads eight
constants off the **concrete** `Task` class at migrate time (`Task.PM`, `Task.SITE_ENGINEER`,
`Task.FINANCE`, `Task.SCM`, `Task.BD`, `Task.DESIGN`, `Task.INTERNAL`, `Task.EXTERNAL`).
`kwargs_for_model_state()` covers missing fields, not renamed constants. Migration **0074**
edited that very constants block.

### 6.3 The `test_settings.py` docstring's timing figures — NOT a contradiction

I initially recorded these as wrong and they are not. My first measurements (245 s for the
shim suite, 41 s for the chain guard) were taken while the real-settings run was occupying
the same machine. Re-measured with nothing else running, the docstring's figures hold: 96 s
against a claimed *"~75 s"*, and 18 s for the chain guard against a claimed *"~15 s"*. See
§1.4 for both, and §1.4's note on the one figure that does not hold — the real-settings
total.

### 6.4 `EXECUTION_MODULE_DEFERRED.md` has two sections numbered B23

`### B23 (original) — the PM dashboard's draft card still opens the designer modal` and
`### B23 — six things the demo seed could not build through any product code path` are
different findings under one number. This prompt's §B23 is the second. Cosmetic, but a
document that is cited by number should not have a collision in it.

---

## 7 · Noticed, not acted on — for `EXECUTION_MODULE_DEFERRED.md` §B

**1. The Finance→milestone sync swallows a `record_transition()` failure.**
[views.py:4264-4298](projects/views.py#L4264-L4298) wraps the whole milestone block —
including the `atomic()` that pairs the `PaymentMilestone` update with its ledger rows — in
`try: … except Exception: pass  # Non-critical — never block the task update`. The pairing
itself is safe (the atomic rolls both back), but `record_transition()`'s contract says *"THIS
FUNCTION RAISES. IT NEVER SWALLOWS … a swallowed failure would leave a status change with no
record of who made it, silently"*, and here the caller does the swallowing. The visible
symptom would be a Finance task marked Done with its payment milestone silently still
Pending, and nothing in the feed or the ledger saying why. Not fixed: it is a live path, the
`except` predates the ledger, and narrowing it is a behaviour change that needs its own test.

**2. `_apply_task_status_change()` takes `request` but never reads `request.user`.**
Documented in §2.2. Whatever phase 2 decides about derivation hooks, the parameter could be
narrowed to what it is actually used for (a POST-like mapping and a messages sink) without
touching a single call site's behaviour. That would also delete the premise of §B23 item 6
rather than merely disproving it.

**3. `Task.STATUS_CHOICES` is `max_length=20`.** [models.py:369-374](projects/models.py#L369-L374).
Any fifth status for a two-step gate must fit: "Awaiting Approval" is 17 characters and fits,
"Submitted for Approval" is 22 and does not. Worth knowing before the wording is chosen,
because widening the column is a migration on the hottest table in the app.

**4. `design_due_date_change` guards `released` and `design_mark_blocked` does not.**
The narrower half of §6.1. Even setting the mirror aside, a released site going back on
Design Hold is a state the design workflow's own documentation does not describe.

**5. The suite's own timing numbers are only meaningful uncontended.** §1.4 and §6.3. The
shim suite measures 96 s alone and 245 s while another suite is running on the same machine;
the chain guard, 18 s and 41 s. Anyone re-measuring to decide §B31 should run one suite at a
time, or they will produce the same 2.5x artefact I did.

**6. The uncommitted docs move.** Twelve root `*.md` deleted and fifteen untracked under
`docs/`, sitting in the working tree since before this session. Two of the untracked files
are new, not moves. It is not this session's to commit, but it means `git status` is not
clean and any session that runs `git add -A` will sweep it in by accident.

---

## 8 · What this session changed

This file, and nothing else. `git diff --stat` over tracked application code is empty; the
only tracked-file changes in the working tree are the pre-existing documentation move
described in §0 and §7.6, which this session did not touch and does not commit.

Commands that ran against a database created and dropped a throwaway Postgres database
(`audit_chain_8d082c71`); the local `solarpms_local` database was read for configuration only
and never migrated.

---

## 1.6 · Addendum — does 0067's output depend on when it ran?

**Verdict: IDENTICAL.** The working local database (0067 applied 28 Aug 2026, before 0074)
and a database built by running the full chain from empty today (0067 applied under the
current constants) produce **byte-identical** template rows — same 9 phases, same 52 tasks,
same ids, same every field — differing only in the two fields that stamp when the migration
ran (`effective_from`, `created_at`). Neither database is a "wrong" baseline for template
content; on the two run-stamped fields the fresh chain is the one that matches production,
since production also first applied 0067 on 03 Sep.

### 1.6.1 What 0067 writes

Three models, via `apps.get_model('projects', …)` at
[0067:41-45](projects/migrations/0067_seed_residential_template_v1.py#L41-L45), plus a
best-effort provenance backfill on a fourth:

| Model | Table | Written by |
|---|---|---|
| `TaskTemplate` | `projects_tasktemplate` | one row, via `seed_task_template_version()` |
| `TaskTemplatePhase` | `projects_tasktemplatephase` | 9 rows |
| `TaskTemplateTask` | `projects_tasktemplatetask` | 52 rows |
| `Task` | `projects_task` | `template_task_id` only, backfilled by name match ([0067:91-118](projects/migrations/0067_seed_residential_template_v1.py#L91-L118)) |

The migration body:

```python
def seed_v1(apps, schema_editor):
    TaskTemplate         = apps.get_model('projects', 'TaskTemplate')
    TaskTemplatePhase    = apps.get_model('projects', 'TaskTemplatePhase')
    TaskTemplateTask     = apps.get_model('projects', 'TaskTemplateTask')
    TaskDurationTemplate = apps.get_model('projects', 'TaskDurationTemplate')
    Task                 = apps.get_model('projects', 'Task')

    # Idempotent: a database that already carries a RESIDENTIAL template ...
    if TaskTemplate.objects.filter(code=RESIDENTIAL_TEMPLATE_CODE).exists():
        print(f'  [0067] {RESIDENTIAL_TEMPLATE_CODE} template already present — skipping seed.')
        return

    overrides = {
        row.task_name: row.duration_days
        for row in TaskDurationTemplate.objects.filter(project_type='residential')
    }

    template = seed_task_template_version(
        template_model=TaskTemplate,
        phase_model=TaskTemplatePhase,
        task_model=TaskTemplateTask,
        code=RESIDENTIAL_TEMPLATE_CODE,
        label=RESIDENTIAL_TEMPLATE_LABEL,
        project_type='Residential',   # Project.project_type vocabulary (capitalised)
        version_no=1,
        phases=build_residential_phases(),
        duration_resolver=lambda name: _get_duration(name, overrides),
        created_by=None,
    )
```

The fields each row gets are fixed by `seed_task_template_version()`
([utils.py:1293](projects/utils.py#L1293)), not by `build_residential_phases()` directly:

* **`TaskTemplate`** — required `code`, `label`, `project_type`, `version_no`,
  `status='draft'` (flipped to `active` at the end), `effective_from=timezone.now().date()`;
  optional `created_by`.
* **`TaskTemplatePhase`** — required `template`, `code=template_code_from_label(phase_name, 50)`,
  `label=phase_name`, `sort_order=phase_order`.
* **`TaskTemplateTask`** — required `phase`, `code=template_code_from_label(task_name, 100)`,
  `label=task_name`, `sort_order=task_order`, `assigned_role`, `task_type`,
  `duration_days=duration_resolver(task_name)`; optional `is_payment_milestone`, `is_mirror`.

So what `build_residential_phases()` supplies per phase is `phase_name`, `phase_order`,
`tasks`; and per task `task_order`, `task_name`, `assigned_role`, `task_type`, and
optionally `is_payment_milestone`. It supplies **no** duration (that comes from
`_get_duration`) and **no** `is_mirror` (the Residential dicts carry no such key, so it
seeds `False`).

`build_residential_phases()` in full — [utils.py:1057-1179](projects/utils.py#L1057-L1179):

```python
def build_residential_phases():
    """
    Return the Residential EPC template as a list of phase dicts (9 phases / 52 tasks).

    NO LONGER EXECUTED AT RUNTIME. Prompt 0.4 moved the template into the database as
    TaskTemplate 'RESIDENTIAL' v1; attach_residential_template() now reads that instead
    of calling this. This function stays because it IS the seed migration 0067 read, and
    because the runtime bootstrap re-seeds a virgin database from it — deleting it would
    make both unreproducible. Change the template by shipping version+1, never by
    editing these literals.

    Task is imported inside to avoid a module-level circular import.
    """
    from .models import Task
    PHASES = [
            {
                'phase_name':  'Sales & Documentation',
                'phase_order': 1,
                'tasks': [
                    {'task_order': 1, 'task_name': 'OCR, Documentation & Verification', 'assigned_role': Task.BD,      'task_type': Task.INTERNAL},
                    {'task_order': 2, 'task_name': INVOICE_TASK_ADVANCE,                'assigned_role': Task.FINANCE, 'task_type': Task.INTERNAL},  # Send invoice — advance
                    {'task_order': 3, 'task_name': 'Advance Payment Confirmation',       'assigned_role': Task.FINANCE, 'task_type': Task.INTERNAL, 'is_payment_milestone': True},  # M1: Advance Payment
                ],
            },
            {
                'phase_name':  'Detail Engineering Visit',
                'phase_order': 2,
                'tasks': [
                    {'task_order': 1, 'task_name': 'DEV Schedule',          'assigned_role': Task.PM,           'task_type': Task.INTERNAL},
                    {'task_order': 2, 'task_name': 'DEV Conduct',           'assigned_role': Task.SITE_ENGINEER, 'task_type': Task.INTERNAL},
                    {'task_order': 3, 'task_name': 'DEV Data to Design',    'assigned_role': Task.SITE_ENGINEER, 'task_type': Task.INTERNAL},
                    {'task_order': 4, 'task_name': 'DEV Inputs Validation', 'assigned_role': Task.DESIGN,        'task_type': Task.INTERNAL},
                ],
            },
            {
                'phase_name':  'Design',
                'phase_order': 3,
                'tasks': [
                    {'task_order': 1, 'task_name': 'Design',                            'assigned_role': Task.DESIGN, 'task_type': Task.INTERNAL},
                    {'task_order': 2, 'task_name': 'Array Layout',                      'assigned_role': Task.DESIGN, 'task_type': Task.INTERNAL},
                    {'task_order': 3, 'task_name': 'SLD',                               'assigned_role': Task.DESIGN, 'task_type': Task.INTERNAL},
                    {'task_order': 4, 'task_name': 'Installation Drawings',             'assigned_role': Task.DESIGN, 'task_type': Task.INTERNAL},
                    {'task_order': 5, 'task_name': 'BOQ Preparation',                  'assigned_role': Task.DESIGN, 'task_type': Task.INTERNAL},
                    {'task_order': 6, 'task_name': 'Design Approval by Internal Team', 'assigned_role': Task.PM,     'task_type': Task.INTERNAL},
                    {'task_order': 7, 'task_name': 'Design Approval by Customer',      'assigned_role': Task.PM,     'task_type': Task.EXTERNAL},
                ],
            },
            {
                'phase_name':  'Pre-Installation Approvals',
                'phase_order': 4,
                'tasks': [
                    {'task_order': 1, 'task_name': 'Pre Installation Approvals',          'assigned_role': Task.PM,  'task_type': Task.INTERNAL},
                    {'task_order': 2, 'task_name': 'LC / PC / NC Required',               'assigned_role': Task.PM,  'task_type': Task.EXTERNAL},
                    {'task_order': 3, 'task_name': 'Vendor Registration',                 'assigned_role': Task.SCM, 'task_type': Task.EXTERNAL},
                    {'task_order': 4, 'task_name': 'Document Preparation',                'assigned_role': Task.PM,  'task_type': Task.INTERNAL},
                    {'task_order': 5, 'task_name': 'Signing Document by Customer',        'assigned_role': Task.PM,  'task_type': Task.EXTERNAL},
                    {'task_order': 6, 'task_name': 'Net Metering Application Submission', 'assigned_role': Task.PM,  'task_type': Task.INTERNAL},
                    {'task_order': 7, 'task_name': 'TFR Received',                        'assigned_role': Task.PM,  'task_type': Task.EXTERNAL},
                ],
            },
            {
                'phase_name':  'Procurement',
                'phase_order': 5,
                'tasks': [
                    {'task_order': 1, 'task_name': 'Procurement Schedule',              'assigned_role': Task.SCM,     'task_type': Task.INTERNAL},
                    {'task_order': 2, 'task_name': 'PO Placed MMS',                     'assigned_role': Task.SCM,     'task_type': Task.INTERNAL},
                    {'task_order': 3, 'task_name': 'PO Placed Module',                  'assigned_role': Task.SCM,     'task_type': Task.INTERNAL},
                    {'task_order': 4, 'task_name': 'PO Placed Inverter',                'assigned_role': Task.SCM,     'task_type': Task.INTERNAL},
                    {'task_order': 5, 'task_name': 'PO for B & C Class Items',          'assigned_role': Task.SCM,     'task_type': Task.INTERNAL},
                    {'task_order': 6, 'task_name': INVOICE_TASK_MATERIAL,              'assigned_role': Task.FINANCE, 'task_type': Task.INTERNAL},  # Send invoice — material supply
                    {'task_order': 7, 'task_name': 'Pre Dispatch Payment Confirmation', 'assigned_role': Task.FINANCE, 'task_type': Task.INTERNAL, 'is_payment_milestone': True},  # M2: Pre Dispatch (replaces deleted Finance Confirmation)
                ],
            },
            {
                'phase_name':  'Delivery',
                'phase_order': 6,
                'tasks': [
                    {'task_order': 1, 'task_name': 'Delivery Schedule',             'assigned_role': Task.SCM, 'task_type': Task.INTERNAL},
                    {'task_order': 2, 'task_name': 'Delivery of MMS',               'assigned_role': Task.SCM, 'task_type': Task.INTERNAL},
                    {'task_order': 3, 'task_name': 'Delivery of B & C Class Items', 'assigned_role': Task.SCM, 'task_type': Task.INTERNAL},
                    {'task_order': 4, 'task_name': 'Delivery of Module',            'assigned_role': Task.SCM, 'task_type': Task.INTERNAL},
                    {'task_order': 5, 'task_name': 'Delivery of Inverter',          'assigned_role': Task.SCM, 'task_type': Task.INTERNAL},
                ],
            },
            {
                'phase_name':  'Installation',
                'phase_order': 7,
                'tasks': [
                    {'task_order': 1, 'task_name': 'MMS Installation',             'assigned_role': Task.SITE_ENGINEER, 'task_type': Task.INTERNAL},
                    {'task_order': 2, 'task_name': 'Earthing Work',                'assigned_role': Task.SITE_ENGINEER, 'task_type': Task.INTERNAL},
                    {'task_order': 3, 'task_name': 'Module Installation',          'assigned_role': Task.SITE_ENGINEER, 'task_type': Task.INTERNAL},
                    {'task_order': 4, 'task_name': 'Inverter Installation',        'assigned_role': Task.SITE_ENGINEER, 'task_type': Task.INTERNAL},
                    {'task_order': 5, 'task_name': 'DC Wire Work',                 'assigned_role': Task.SITE_ENGINEER, 'task_type': Task.INTERNAL},
                    {'task_order': 6, 'task_name': 'AC Cable Work',                'assigned_role': Task.SITE_ENGINEER, 'task_type': Task.INTERNAL},
                    {'task_order': 7, 'task_name': 'Connections and Voc Testing',  'assigned_role': Task.SITE_ENGINEER, 'task_type': Task.INTERNAL},
                    {'task_order': 8, 'task_name': 'Pre Commissioning Check List', 'assigned_role': Task.SITE_ENGINEER, 'task_type': Task.INTERNAL},
                ],
            },
            {
                'phase_name':  'Commissioning',
                'phase_order': 8,
                'tasks': [
                    {'task_order': 1, 'task_name': 'Pre Commissioning Visit by DISCOM', 'assigned_role': Task.PM,            'task_type': Task.EXTERNAL},
                    {'task_order': 2, 'task_name': 'Meter Testing',                     'assigned_role': Task.SITE_ENGINEER,  'task_type': Task.INTERNAL},
                    {'task_order': 3, 'task_name': 'SCO Release',                       'assigned_role': Task.PM,            'task_type': Task.EXTERNAL},
                    {'task_order': 4, 'task_name': 'Meter Installation by DISCOM',      'assigned_role': Task.PM,            'task_type': Task.EXTERNAL},
                    {'task_order': 5, 'task_name': 'RMS Configuration',                 'assigned_role': Task.SITE_ENGINEER,  'task_type': Task.INTERNAL},
                    {'task_order': 6, 'task_name': 'Plant Commissioning',               'assigned_role': Task.SITE_ENGINEER,  'task_type': Task.INTERNAL},
                    {'task_order': 7, 'task_name': 'Commissioning Report Prepared',     'assigned_role': Task.SITE_ENGINEER,  'task_type': Task.INTERNAL},
                    {'task_order': 8, 'task_name': 'Commissioning Report Approved',     'assigned_role': Task.PM,            'task_type': Task.INTERNAL},
                    {'task_order': 9, 'task_name': 'Customer Handover',                 'assigned_role': Task.PM,            'task_type': Task.INTERNAL},
                    {'task_order': 10, 'task_name': INVOICE_TASK_FINAL,                 'assigned_role': Task.FINANCE,       'task_type': Task.INTERNAL},  # Send invoice — final payment
                ],
            },
            {
                'phase_name':  'Finance Closure',
                'phase_order': 9,
                'tasks': [
                    {'task_order': 1, 'task_name': '100% Payment Confirmation', 'assigned_role': Task.FINANCE, 'task_type': Task.INTERNAL, 'is_payment_milestone': True},  # M3: 100% Payment
                ],
            },
    ]
    return PHASES
```

### 1.6.2 The eight constants, and what 0074 did to them

The eight read off the concrete `Task` class inside that function:

```
$ awk 'NR>=1057 && NR<=1179' projects/utils.py | grep -o 'Task\.[A-Z_]*' | sort | uniq -c
      1 Task.BD
      6 Task.DESIGN
      8 Task.EXTERNAL
      6 Task.FINANCE
     44 Task.INTERNAL
     14 Task.PM
     11 Task.SCM
     14 Task.SITE_ENGINEER
```

0074 was added by commit `2051f89` ("1.3a: the OPEX template becomes data, and is_mirror
becomes a column nothing reads", 30 Aug 2026). Its edit to the constants block, verbatim:

```diff
     # Role constants — mirror UserProfile.ROLE_CHOICES where relevant
-    PM            = 'PM'
-    SITE_ENGINEER = 'Site Engineer'
-    FINANCE       = 'Finance'
-    SCM           = 'SCM'
-    BD            = 'BD / Sales'
-    DESIGN        = 'Design'
+    PM                  = 'PM'
+    SITE_ENGINEER       = 'Site Engineer'
+    FINANCE             = 'Finance'
+    SCM                 = 'SCM'
+    BD                  = 'BD / Sales'
+    DESIGN              = 'Design'
+    # Added by 1.3a for the OPEX template's "Completion Certificates (Paperwork)" ...
+    PROJECT_COORDINATOR = 'Project Coordinator'   # 19 chars — fits max_length=20

     ROLE_CHOICES = [
-        (PM,            'PM'),
-        (SITE_ENGINEER, 'Site Engineer'),
-        (FINANCE,       'Finance'),
-        (SCM,           'SCM'),
-        (BD,            'BD / Sales'),
-        (DESIGN,        'Design'),
+        (PM,                  'PM'),
+        (SITE_ENGINEER,       'Site Engineer'),
+        (FINANCE,             'Finance'),
+        (SCM,                 'SCM'),
+        (BD,                  'BD / Sales'),
+        (DESIGN,              'Design'),
+        (PROJECT_COORDINATOR, 'Project Coordinator'),
     ]
```

Side by side:

| Constant | Value before 0074 | Value after 0074 | Changed? |
|---|---|---|---|
| `Task.PM` | `'PM'` | `'PM'` | No |
| `Task.SITE_ENGINEER` | `'Site Engineer'` | `'Site Engineer'` | No |
| `Task.FINANCE` | `'Finance'` | `'Finance'` | No |
| `Task.SCM` | `'SCM'` | `'SCM'` | No |
| `Task.BD` | `'BD / Sales'` | `'BD / Sales'` | No |
| `Task.DESIGN` | `'Design'` | `'Design'` | No |
| `Task.INTERNAL` | `'Internal'` | `'Internal'` | No — not in the diff at all |
| `Task.EXTERNAL` | `'External'` | `'External'` | No — not in the diff at all |
| `Task.PROJECT_COORDINATOR` | *(did not exist)* | `'Project Coordinator'` | **Added** |

**The edit was purely additive.** Every `-` line has a matching `+` line whose string value
is character-for-character the same; the only textual change to the six existing constants is
whitespace realignment to accommodate the longer new name. No Python attribute was renamed
and no string value changed. `TYPE_CHOICES` (`INTERNAL` / `EXTERNAL`) was not touched by the
commit at all.

That is *why* the two databases agree — and it is worth being precise about what it does and
does not prove. **It does not weaken §1.5 or §6.2.** The hazard there is that
`build_residential_phases()` reads live constants at migrate time; this run shows that on
this occasion the constants had not moved. Purely additive was luck of the draft, not a rule
the code enforces.

One second-order effect also cancelled out, and is worth naming because it could have gone
the other way: 0074 added `is_mirror` to `TaskTemplateTask`. On the fresh chain,
`kwargs_for_model_state()` drops `is_mirror` at 0067 (the field does not exist yet) and 0074's
`AddField(default=False)` supplies it seven migrations later. On the working database, 0067
ran when the field did not exist at all and 0074's `AddField(default=False)` filled it in
afterwards. Both routes land on `is_mirror=False` for all 52 rows, which the dumps confirm.

### 1.6.3 The two dumps

Both dumps are pure `SELECT`s issued through `psycopg2`, ordered
`ORDER BY phase.sort_order, task.sort_order, task.label` (and `sort_order, label` for
phases), one row per line, every column of all three tables including surrogate ids.
**Nothing was written to the working database and `migrate` was never pointed at it** — the
only statements it received were the reads below and one `information_schema` query.

* **(a) working local database** — `solarpms_local`, the default settings `DATABASE_URL`.
* **(b) fresh chain** — the Q1 scratch database had been dropped, so **a new one was created
  and the full chain run into it**: `audit_q16_fce8814d`, built by
  `manage.py migrate --run-syncdb --noinput` under `solarpms.settings`, exit 0, with
  `[0067] Seeded RESIDENTIAL v1 (active): 9 phases, 52 tasks.` and
  `[0075] Seeded OPEX v1 (active): 7 phases, 23 tasks, 8 mirrors.`

Working database, first 15 of 63 lines:

```
TEMPLATE id=1 code=RESIDENTIAL label=Residential EPC project_type=Residential version_no=1 status=active effective_from=2026-08-28 created_at=2026-08-28 23:22:24.927652+05:30 created_by_id=None
PHASE    id=1 sort_order=1 code=SALES_DOCUMENTATION label=Sales & Documentation
PHASE    id=2 sort_order=2 code=DETAIL_ENGINEERING_VISIT label=Detail Engineering Visit
PHASE    id=3 sort_order=3 code=DESIGN label=Design
PHASE    id=4 sort_order=4 code=PRE_INSTALLATION_APPROVALS label=Pre-Installation Approvals
PHASE    id=5 sort_order=5 code=PROCUREMENT label=Procurement
PHASE    id=6 sort_order=6 code=DELIVERY label=Delivery
PHASE    id=7 sort_order=7 code=INSTALLATION label=Installation
PHASE    id=8 sort_order=8 code=COMMISSIONING label=Commissioning
PHASE    id=9 sort_order=9 code=FINANCE_CLOSURE label=Finance Closure
TASK     id=1 phase=1 pos=1 code=OCR_DOCUMENTATION_VERIFICATION label=OCR, Documentation & Verification role=BD / Sales type=Internal duration_days=2 is_payment_milestone=False is_mirror=False
TASK     id=2 phase=1 pos=2 code=SEND_INVOICE_ADVANCE_PAYMENT label=Send Invoice - Advance Payment role=Finance type=Internal duration_days=1 is_payment_milestone=False is_mirror=False
TASK     id=3 phase=1 pos=3 code=ADVANCE_PAYMENT_CONFIRMATION label=Advance Payment Confirmation role=Finance type=Internal duration_days=1 is_payment_milestone=True is_mirror=False
TASK     id=4 phase=2 pos=1 code=DEV_SCHEDULE label=DEV Schedule role=PM type=Internal duration_days=1 is_payment_milestone=False is_mirror=False
TASK     id=5 phase=2 pos=2 code=DEV_CONDUCT label=DEV Conduct role=Site Engineer type=Internal duration_days=2 is_payment_milestone=False is_mirror=False
```

The fresh-chain dump is the same 63 lines with one line differing; rather than print it twice,
§1.6.4 gives the whole diff, which is complete.

### 1.6.4 The diff, and the hashes

```
$ diff -u .audit_dump_working_full.txt .audit_dump_fresh_full.txt
--- .audit_dump_working_full.txt	2026-09-03 22:26:24.783073800 +0530
+++ .audit_dump_fresh_full.txt	2026-09-03 22:27:19.570818700 +0530
@@ -1,4 +1,4 @@
-TEMPLATE id=1 code=RESIDENTIAL label=Residential EPC project_type=Residential version_no=1 status=active effective_from=2026-08-28 created_at=2026-08-28 23:22:24.927652+05:30 created_by_id=None
+TEMPLATE id=1 code=RESIDENTIAL label=Residential EPC project_type=Residential version_no=1 status=active effective_from=2026-09-03 created_at=2026-09-03 22:27:01.718623+05:30 created_by_id=None
 PHASE    id=1 sort_order=1 code=SALES_DOCUMENTATION label=Sales & Documentation
 PHASE    id=2 sort_order=2 code=DETAIL_ENGINEERING_VISIT label=Detail Engineering Visit
 PHASE    id=3 sort_order=3 code=DESIGN label=Design
DIFF_EXIT=1
```

**That is the entire diff.** One line, two fields. 62 of 63 lines — all 9 phases, all 52
tasks, every field including the surrogate `id` of every row — are byte-identical.

Hashes of the raw dumps (they differ, because of that one line):

```
$ sha256sum .audit_dump_working_full.txt .audit_dump_fresh_full.txt
eb3c251aae15a947e48b342b10c062bb0874f52f644fdf90db5ba4ad051abccb  .audit_dump_working_full.txt
f11e3c36f7666c21fb7de94d6506bdfb9c393cba78852497ddb0b329df46dd4e  .audit_dump_fresh_full.txt
```

The same dumps with the two run-stamped fields replaced by `<RUN>`, and the phases+tasks
block hashed on its own:

```
$ diff -u .audit_w_norm.txt .audit_f_norm.txt
DIFF_EXIT=0

$ sha256sum .audit_w_norm.txt .audit_f_norm.txt
c6d05c2067aaf15711f486931aba2673f396800a3fe462d34e1a601294f90c9e  .audit_w_norm.txt
c6d05c2067aaf15711f486931aba2673f396800a3fe462d34e1a601294f90c9e  .audit_f_norm.txt

$ tail -n +2 .audit_dump_working_full.txt | sha256sum
4a775d28b2351fee4abf22fdcb549f1deecca5ce73b23151ef4e51ffa84da336  -
$ tail -n +2 .audit_dump_fresh_full.txt | sha256sum
4a775d28b2351fee4abf22fdcb549f1deecca5ce73b23151ef4e51ffa84da336  -
```

The one input that could have made the two disagree without any constant moving is the
duration resolver, which 0067 reads from the live `TaskDurationTemplate` table rather than
from migration 0034's list — an admin edit on the working database would have propagated into
`duration_days`. It has not been edited:

```
WORKING TaskDurationTemplate(residential) rows = 50 sha256 = 538cbfefde589bb05e8c87ecea904baa0436ee51f1d8a34dc4101c0d8232f885
FRESH   TaskDurationTemplate(residential) rows = 50 sha256 = 538cbfefde589bb05e8c87ecea904baa0436ee51f1d8a34dc4101c0d8232f885
```

### 1.6.5 The two differences, assessed

| Field | Working DB | Fresh chain | Behaviour change for a project activated under it? | Asserted by any of the 92 phase-0 characterisation tests? |
|---|---|---|---|---|
| `TaskTemplate.effective_from` | `2026-08-28` | `2026-09-03` | **No.** Grepping the codebase, `effective_from` is written by `seed_task_template_version()` ([utils.py:1325](projects/utils.py#L1325)) and by `TaskTemplate.activate()` ([models.py:2081-2083](projects/models.py#L2081-L2083)), and read only for display — `admin.py:323`/`:489` list columns and two `task_durations.html` templates. No code branches on its value. `resolve_active_task_template()` selects on `status='active'` + `project_type`, not on a date, so activation picks the same row either way | **No.** `tests_residential_baseline.py` never mentions the field (`grep -c 'effective_from\|created_at'` → `0`). The only two assertions anywhere are `assertIsNotNone`, at [tests_task_template.py:424](projects/tests_task_template.py#L424) and [:658](projects/tests_task_template.py#L658) — value-agnostic by construction |
| `TaskTemplate.created_at` | `2026-08-28 23:22:24…` | `2026-09-03 22:27:01…` | **No.** `auto_now_add` provenance; nothing branches on it | **No**, same evidence |

Everything that *does* drive activation behaviour — the 52 `(phase, sort_order, label,
assigned_role, task_type, duration_days, is_payment_milestone, is_mirror)` tuples, and the 9
phase rows they hang off — is identical, so a Residential project activated against either
database gets the same 52 tasks with the same owners, types, durations and payment-milestone
flags.

**Which is the correct baseline.** For template *content*, the question does not arise: they
agree. For the two run-stamped fields, the fresh chain is the one that matches production,
because production also applied 0067 for the first time on 03 Sep. Your working database is
the odd one out on those two values only, and neither is load-bearing.

**One caveat on how far this generalises, stated because it is the point of §1.5.** This
comparison shows that 0067 produced the same output *on these two occasions*, and §1.6.2
shows why: 0074's edit happened to be purely additive. It is not evidence that 0067 is
insulated from `models.py`. The next edit to `Task.ROLE_CHOICES` that renames or re-values a
constant, rather than appending one, would separate a working database from a fresh chain
exactly here — and nothing in the code, and no test, would report it. §1.5 and §6.2 stand
unchanged.
