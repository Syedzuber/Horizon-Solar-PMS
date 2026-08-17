# SESSION T — Unblock `tests_design_part11` and report the result

## Headline

**No code was changed. No fix was needed. All 32 tests execute and all 32 pass.**

The suite was not broken. It was being invoked without its settings module. The repo already
ships `solarpms/test_settings.py`, whose docstring states the required invocation, and
`tests_design_part11.py:52` says in a comment that the suite depends on it.

```
python manage.py test projects.tests_design_part11 --settings=solarpms.test_settings
→ Ran 32 tests in 2.565s
→ OK
```

`git diff --stat` is empty. Nothing in `projects/tests_design_part11.py` was touched.

---

## Opening check

| Check | Result |
|---|---|
| Repo | `c:\SolarPMS\Horizon-Solar-PMS` |
| `git status` | clean tracked tree; untracked = `SESSION_B_AUDIT.md`, `SESSION_C_AUDIT.md`, `SESSION_D_AUDIT.md` (all `.md`) — **pass** |
| `git log --oneline -4` | `92e1a50 [Session C.1]`, `ee8c752 [Session C]`, `9fb3c59`, `338ebde` |
| Local HEAD | `92e1a50d3787e8953452557df4351d5e336259ae` — as expected |
| Deployed SHA | deployment `4f99560f-3ec1-4179-84aa-6b7f9cb870a7`, ● Online, verified in Session D as `92e1a50` — **equal to HEAD** |
| Migration head | `0064_boqitemmaster_is_mandatory.py` |

Note on the deploy check: the Railway **MCP** token returned `Unauthorized` this session. The
Railway **CLI** is still authenticated and reported the linked service's live deployment ID as
`4f99560f-3ec1-4179-84aa-6b7f9cb870a7`, which is the same deployment ID confirmed against SHA
`92e1a50` in Session D. The check passes; the MCP token needs re-authing at some point, which is
unrelated to this session.

**Commit prefix `[Session T]`: not used — there is no commit, because there is no code change.**

---

## PRE-FLIGHT REPORT

### PF1 — The suite as invoked without the settings module

Command as run (the form used in every prior session):

```
python manage.py test projects.tests_design_part11 -v 2
```

Raw output, head:

```
Creating test database for alias 'default' ('test_solarpms_local')...
Found 32 test(s).
Operations to perform:
  Synchronize unmigrated apps: humanize, messages, runserver_nostatic, staticfiles
  Apply all migrations: admin, auth, contenttypes, projects, sessions
Synchronizing apps without migrations:
  Creating tables...
    Running deferred SQL...
Running migrations:
  Applying contenttypes.0001_initial... OK
  Applying auth.0001_initial... OK
  Applying admin.0001_initial... OK
```

Raw output, tail:

```
----------------------------------------------------------------------
Ran 32 tests in 0.408s

FAILED (errors=32)
Destroying test database for alias 'default' ('test_solarpms_local')...
 OK
System check identified no issues (0 silenced).
```

**Totals: 32 tests, 32 errors, 0 failures, 0 passes.** Note `Apply all migrations: ... projects`
in the header — that line is the entire cause, and it is absent under the correct invocation.

### PF2 — Representative traceback, and confirmation of a single root cause

```
ERROR: test_06c_picker_404s_on_a_residential_project (projects.tests_design_part11.ResidentialUnaffectedTests.test_06c_picker_404s_on_a_residential_project)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "...\django\db\backends\utils.py", line 105, in _execute
    return self.cursor.execute(sql, params)
           ~~~~~~~~~~~~~~~~~~~^^^^^^^^^^^^^
psycopg2.errors.UniqueViolation: duplicate key value violates unique constraint "projects_boqitemmaster_code_key"
DETAIL:  Key (code)=(ITM-001) already exists.


The above exception was the direct cause of the following exception:

Traceback (most recent call last):
  File "C:\SolarPMS\Horizon-Solar-PMS\projects\tests_design_part11.py", line 98, in setUp
    BOQItemMaster.objects.bulk_create([
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~^^
        BOQItemMaster(code=f'ITM-{i:03d}', description=description, unit=unit,
        ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
    ...<2 lines>...
        for i, (category, description, unit) in enumerate(RESIDENTIAL_SEED, start=1)
        ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
    ])
    ^^
  ...
  File "...\django\db\models\query.py", line 833, in bulk_create
  File "...\django\db\models\query.py", line 1956, in _batched_insert
  File "...\django\db\models\query.py", line 1918, in _insert
  File "...\django\db\models\sql\compiler.py", line 1925, in execute_sql
    cursor.execute(sql, params)
  ...
django.db.utils.IntegrityError: duplicate key value violates unique constraint "projects_boqitemmaster_code_key"
DETAIL:  Key (code)=(ITM-001) already exists.
```

**Every error has the same root cause.** Verified mechanically across the full run:

| Probe | Result |
|---|---|
| `grep -c "^ERROR: "` | `32` |
| Distinct terminal exceptions | `django.db.utils.IntegrityError` / `psycopg2.errors.UniqueViolation`, both the same constraint |
| Distinct `DETAIL: Key (code)=(…)` values | **one** — `(ITM-001)` |
| Distinct `in setUp` frames | three — lines **98**, **484**, **782** |

The three `setUp` frames are not three causes. Lines 484 (`DesignLockTests`) and 782
(`OffCatalogueRowTests`) are subclass `setUp`s that call `super().setUp()`, so their frames sit
above line 98 in the same stack. One insert, one constraint, one duplicate key.

**Hard stop 4 does not trigger** — there is exactly one root cause.

### PF3 — `setUp` and the class structure

`Part11Base.setUp`, [tests_design_part11.py:93-122](projects/tests_design_part11.py#L93-L122):

```python
class Part11Base(TestCase):
    """The real catalogue — 207 OPEX rows from the migration literal, plus a Residential
    template — and one OPEX site with a designer, a QC reviewer and a Head."""

    def setUp(self):
        BOQItemMaster.objects.bulk_create([
            BOQItemMaster(code=f'ITM-{i:03d}', description=description, unit=unit,
                          category=category, project_type='Residential',
                          is_active=True, sort_order=i)
            for i, (category, description, unit) in enumerate(RESIDENTIAL_SEED, start=1)
        ])
        BOQItemMaster.objects.bulk_create([
            BOQItemMaster(code=f'OPX-{i:03d}', description=description, unit=unit,
                          category=category, project_type='OPEX',
                          is_active=True, sort_order=i)
            for i, (category, description, unit) in enumerate(OPEX_BOQ_ITEMS, start=1)
        ])

        self.designer = _profile('des11',  'Design')
        self.other    = _profile('des11b', 'Design')
        self.qc       = _profile('qc11',   'Design', is_design_qc=True)
        self.head     = _profile('head11', 'Design', is_design_head=True)
        self.pm       = _profile('pm11',   'PM')
        self.scm      = _profile('scm11',  'SCM')

        self.program = Program.objects.create(
            name='Test-Part11', program_type='OPEX', client_name='P11Client',
            status='Active', short_tender_code='P11')

        self.site, self.assignment = self._opex_site('P11-A')
```

There is **no `setUpTestData`** anywhere in the file. Two subclasses extend `setUp`
(`DesignLockTests` at line 483, `OffCatalogueRowTests` at line 781), both via `super().setUp()`.

**The load-bearing comment is at [tests_design_part11.py:50-54](projects/tests_design_part11.py#L50-L54)**,
and it states the operating assumption in as many words:

```python
# The migration's frozen literal IS the catalogue. Reading it here rather than
# re-parsing the spreadsheet means these tests assert the thing that actually ships, and
# the suite runs with MIGRATION_MODULES disabled so nothing else would have created it.
_MIGRATION = importlib.import_module(
    'projects.migrations.0057_boqitemmaster_project_type_opex_catalogue')
```

*"the suite runs with `MIGRATION_MODULES` disabled so nothing else would have created it."*
The author knew. The `setUp` is correct **for the invocation it documents.**

Class structure:

| Class | Line | Tests | What it tests |
|---|---|---|---|
| `Part11Base` | 93 | — | Base fixture: catalogue + one OPEX site with designer/QC/Head |
| `CatalogueTests` | 202 | 5 | The 207-row OPEX catalogue: counts, per-category counts, unit normalisation, duplicate descriptions, whitespace |
| `ResidentialUnaffectedTests` | 265 | 4 | Part 11 did not disturb Residential: scoped template, BOQ creation, lock never applies, picker 404s |
| `PickerTests` | 324 | 8 | The OPEX picker GET and POST: payload/search, no pre-population, redirect, reconciliation save, forged pks, empty-category indicator, authority |
| `DesignLockTests` | 481 | 6 | The lock progression: mark complete freezes, QC sees only added rows, rejection reopens, scoped rejection stays frozen, Head approval locks, PM change request reopens |
| `AggregationAndBlastRadiusTests` | 695 | 3 | Part 6 aggregation joins on `item_master`; group lock beats design lock; nothing outside the OPEX BOQ changed |
| `OffCatalogueRowTests` | 776 | 5 | Rows whose master is not in the active OPEX catalogue: render, survive, editable quantity, removal, review panel |

**Total 31 tests across six classes** — the runner reports 32; the discrepancy is one test I did
not attribute by reading alone, and the parsed run output below accounts for all 32 by name.

### PF4 — Migration 0047

[0047_boqitemmaster_boqitem_item_master.py](projects/migrations/0047_boqitemmaster_boqitem_item_master.py):

```python
CODE_TEMPLATE = 'ITM-{:03d}'  # Deterministic: ITM-001 … ITM-037, by position in the list above

def seed_catalogue(apps, schema_editor):
    ...
    BOQItemMaster.objects.bulk_create(rows)
```

Its header states the source, [0047:5-8](projects/migrations/0047_boqitemmaster_boqitem_item_master.py#L5-L8):

```python
# Verbatim snapshot of the literal list that get_standard_boq_items() returned before
# this migration. The function now reads BOQItemMaster instead; this copy exists solely
# to seed the catalogue and must never be edited — later catalogue changes belong in the
# admin screen, not here.
```

**Rows created: 37, `project_type='Residential'`, codes `ITM-001` … `ITM-037`.** Confirmed by the
migration's own runtime log, visible in the failing run:

```
[0047] BOQItemMaster seeded: 37 rows (ITM-001..ITM-037)
```

Migration **0057** additionally seeds 207 rows `OPX-001` … `OPX-207` with `project_type='OPEX'`:

```
[0057] Created 207 OPEX catalogue row(s), by category:
[0057] Final catalogue count by project type:
         Residential          37
         OPEX                207
```

### PF5 — The collision, precisely

`setUp` creates:
- `ITM-001` … `ITM-008` — 8 rows, `project_type='Residential'`, from `RESIDENTIAL_SEED`
- `OPX-001` … `OPX-207` — 207 rows, `project_type='OPEX'`, from the 0057 literal

When migrations run (the wrong invocation), the test database **already contains**
`ITM-001`…`ITM-037` from 0047 and `OPX-001`…`OPX-207` from 0057. The first `bulk_create` collides
on its very first row, `ITM-001`.

The constraint, [models.py:680](projects/models.py#L680):

```python
    code        = models.CharField(max_length=32, unique=True)   # Short stable identifier, e.g. ITM-001 / OPX-001
```

`unique=True` on `code` produces the Postgres unique index `projects_boqitemmaster_code_key`
named in the error.

**Note the eight-versus-thirty-seven detail.** `RESIDENTIAL_SEED` has only 8 entries
([tests_design_part11.py:69-78](projects/tests_design_part11.py#L69-L78)) and is explicitly a
*stand-in*, not a copy of the real 37:

```python
#: A stand-in for the 37 Part 0.5 rows. The count does not matter to anything Part 11
#: changed — what matters is that get_standard_boq_items() returns THESE and only these.
```

`test_05` asserts `len(items) == len(RESIDENTIAL_SEED)`. Under migrations that assertion would be
`8 == 37` and fail even if the `IntegrityError` were suppressed with `ignore_conflicts=True`. The
suite is not merely blocked by migrations — it is **incompatible with them by design**, which is
why the file says it runs with them disabled.

### PF6 — The narrowest possible fix

**Zero lines. The fix is the invocation, and it is already documented in the repo.**

[solarpms/test_settings.py](solarpms/test_settings.py), in full:

```python
"""Test-only settings: run the suite on in-memory SQLite with migrations disabled.

Why this exists: the local Postgres role can't CREATE DATABASE (needed for the normal
test DB), and one historical migration uses Postgres-only raw SQL that SQLite rejects.
Disabling migrations makes Django build the schema directly from current model state,
so tests run anywhere without a Postgres server or createdb privilege.

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

Line 8 is the answer: `Usage:  python manage.py test projects --settings=solarpms.test_settings`.

The three fixes the prompt anticipated — stop creating rows the migration seeds, `get_or_create`,
or scope to unused codes — would all have been **wrong**. Each would have edited a correct test
file to accommodate an invocation the file explicitly says it does not support, and the first two
would then have failed `test_05` on the 8-vs-37 count.

### PF7 — Files touched

**None.** Not `projects/tests_design_part11.py`, not migration 0047, not application code, not
another test module. **Hard stop 3 does not trigger** — no application code, migration or other
test module is required, because no change is required at all.

---

## AFTER — the suite as documented

```
python manage.py test projects.tests_design_part11 -v 2 --settings=solarpms.test_settings
```

```
Creating test database for alias 'default' ('file:memorydb_default?mode=memory&cache=shared')...
test_15_group_aggregation_sums_across_sites_using_opex_items (projects.tests_design_part11.AggregationAndBlastRadiusTests.test_15_group_aggregation_sums_across_sites_using_opex_items)
VERIFICATION 15 — the Part 6 join is item_master, and OPEX rows carry it. ... ok
test_15b_group_lock_still_beats_the_design_lock (projects.tests_design_part11.AggregationAndBlastRadiusTests.test_15b_group_lock_still_beats_the_design_lock)
The procurement lock is not reversible and is not weakened by Part 11. ... ok
test_16_nothing_outside_the_opex_boq_changed (projects.tests_design_part11.AggregationAndBlastRadiusTests.test_16_nothing_outside_the_opex_boq_changed)
VERIFICATION 16 — no Task, no NotificationLog, no Residential row touched. ... ok
test_01_counts_by_project_type (projects.tests_design_part11.CatalogueTests.test_01_counts_by_project_type)
VERIFICATION 1 — 207 OPEX, and the Residential rows untouched beside them. ... ok
test_02_per_category_counts (projects.tests_design_part11.CatalogueTests.test_02_per_category_counts)
VERIFICATION 2 — every category count matches the brief exactly. ... ok
test_03_units_are_the_seven_normalised_values (projects.tests_design_part11.CatalogueTests.test_03_units_are_the_seven_normalised_values)
VERIFICATION 3 — nine source spellings collapsed to seven units. ... ok
test_04_duplicate_descriptions_are_eight_distinct_rows (projects.tests_design_part11.CatalogueTests.test_04_duplicate_descriptions_are_eight_distinct_rows)
VERIFICATION 4 — the four lug descriptions exist twice, not deduplicated. ... ok
test_04b_descriptions_are_whitespace_collapsed_and_otherwise_verbatim (projects.tests_design_part11.CatalogueTests.test_04b_descriptions_are_whitespace_collapsed_and_otherwise_verbatim)
Settled decision 8 — collapsed, and nothing else changed. ... ok
test_10_mark_complete_freezes_the_boq (projects.tests_design_part11.DesignLockTests.test_10_mark_complete_freezes_the_boq)
VERIFICATION 10 — confirmed by direct POST, not by a missing button. ... Forbidden: /projects/P11-A/boq/submit/
ok
test_11_design_qc_sees_only_the_added_items (projects.tests_design_part11.DesignLockTests.test_11_design_qc_sees_only_the_added_items)
VERIFICATION 11 — six rows on the review screen, never 207. ... Forbidden: /projects/P11-A/boq/entry/
ok
test_12_qc_rejection_reopens_the_full_picker (projects.tests_design_part11.DesignLockTests.test_12_qc_rejection_reopens_the_full_picker)
VERIFICATION 12 — the designer adds an item that was NEVER on the sheet. ... ok
test_12b_a_rejection_that_was_not_about_the_boq_leaves_it_frozen (projects.tests_design_part11.DesignLockTests.test_12b_a_rejection_that_was_not_about_the_boq_leaves_it_frozen)
Part 9.1 scoping is respected rather than overruled — see the docstring on ... ok
test_13_head_approval_locks_the_boq (projects.tests_design_part11.DesignLockTests.test_13_head_approval_locks_the_boq)
VERIFICATION 13 — confirmed by direct POST. ... ok
test_14_pm_change_request_reopens_with_the_full_picker (projects.tests_design_part11.DesignLockTests.test_14_pm_change_request_reopens_with_the_full_picker)
VERIFICATION 14 — a new attempt clears the stamp, so the picker returns. ... ok
test_off_catalogue_quantity_is_editable_when_the_field_is_posted (projects.tests_design_part11.OffCatalogueRowTests.test_off_catalogue_quantity_is_editable_when_the_field_is_posted)
A posted field is applied; an empty one clears — the browser always posts it. ... ok
test_off_catalogue_rows_are_removed_when_dropped (projects.tests_design_part11.OffCatalogueRowTests.test_off_catalogue_rows_are_removed_when_dropped) ... ok
test_off_catalogue_rows_render_and_are_not_offered_by_the_picker (projects.tests_design_part11.OffCatalogueRowTests.test_off_catalogue_rows_render_and_are_not_offered_by_the_picker) ... ok
test_off_catalogue_rows_survive_a_save_that_does_not_drop_them (projects.tests_design_part11.OffCatalogueRowTests.test_off_catalogue_rows_survive_a_save_that_does_not_drop_them)
Kept rows stay, and an ABSENT quantity field leaves the number alone. ... ok
test_review_panel_shows_off_catalogue_rows_separately (projects.tests_design_part11.OffCatalogueRowTests.test_review_panel_shows_off_catalogue_rows_separately) ... ok
test_07_catalogue_payload_supports_the_documented_searches (projects.tests_design_part11.PickerTests.test_07_catalogue_payload_supports_the_documented_searches)
VERIFICATION 7 — "inverter" and "6sqmm", against the real catalogue. ... ok
test_07b_boq_starts_empty_and_a_get_writes_nothing (projects.tests_design_part11.PickerTests.test_07b_boq_starts_empty_and_a_get_writes_nothing)
The whole point of the picker: no pre-population, and no row created on GET. ... ok
test_07c_opex_designer_is_redirected_from_boq_detail (projects.tests_design_part11.PickerTests.test_07c_opex_designer_is_redirected_from_boq_detail)
boq_detail must not seed an OPEX site with the Residential template. ... ok
test_07d_non_author_roles_still_read_boq_detail (projects.tests_design_part11.PickerTests.test_07d_non_author_roles_still_read_boq_detail)
SCM and PM have no picker; the redirect must not catch them. ... ok
test_08_add_six_items_save_draft_and_reload (projects.tests_design_part11.PickerTests.test_08_add_six_items_save_draft_and_reload)
VERIFICATION 8 — six persist with their quantities intact. ... ok
test_08b_save_is_a_reconciliation_so_removal_deletes (projects.tests_design_part11.PickerTests.test_08b_save_is_a_reconciliation_so_removal_deletes) ... ok
test_08c_a_forged_item_id_is_dropped_not_trusted (projects.tests_design_part11.PickerTests.test_08c_a_forged_item_id_is_dropped_not_trusted)
Residential master pks and junk must not become OPEX BOQ rows. ... ok
test_09_empty_category_indicator (projects.tests_design_part11.PickerTests.test_09_empty_category_indicator)
VERIFICATION 9 — untouched categories are reported, touched ones are not. ... ok
test_09b_only_the_assigned_designer_may_write (projects.tests_design_part11.PickerTests.test_09b_only_the_assigned_designer_may_write)
user_can_edit_project_boq is unchanged; the picker just applies it. ... Forbidden: /projects/P11-A/boq/entry/
ok
test_05_get_standard_boq_items_returns_residential_only (projects.tests_design_part11.ResidentialUnaffectedTests.test_05_get_standard_boq_items_returns_residential_only)
VERIFICATION 5 — same shape, Residential rows only, OPEX absent. ... ok
test_06_residential_boq_creation_is_unchanged (projects.tests_design_part11.ResidentialUnaffectedTests.test_06_residential_boq_creation_is_unchanged)
VERIFICATION 6 — create one end to end and compare it to the template. ... ok
test_06b_residential_boq_is_never_design_locked (projects.tests_design_part11.ResidentialUnaffectedTests.test_06b_residential_boq_is_never_design_locked)
The lock is structurally False without a DesignAssignment, which is what makes ... ok
test_06c_picker_404s_on_a_residential_project (projects.tests_design_part11.ResidentialUnaffectedTests.test_06c_picker_404s_on_a_residential_project) ... Not Found: /projects/HRP-RES-P11C/boq/entry/
ok

----------------------------------------------------------------------
Ran 32 tests in 2.565s

OK
Destroying test database for alias 'default' ('file:memorydb_default?mode=memory&cache=shared')...
```

*(The `Creating table …` lines emitted between the header and the first test — 49 of them, since
migrations are disabled and the schema is built from model state — are elided above for
readability. The `Forbidden:` and `Not Found:` lines are Django request-logger output from tests
that assert 403 and 404 respectively; they are expected, and each is followed by `ok`.)*

**Repeatability confirmed** — a second independent run reported `OK` / `Found 32 test(s)`.

---

## Results table

| # | Test | Class | Result | Diagnosis |
|---|---|---|---|---|
| 1 | `test_01_counts_by_project_type` | `CatalogueTests` | PASS | — |
| 2 | `test_02_per_category_counts` | `CatalogueTests` | PASS | — |
| 3 | `test_03_units_are_the_seven_normalised_values` | `CatalogueTests` | PASS | — |
| 4 | `test_04_duplicate_descriptions_are_eight_distinct_rows` | `CatalogueTests` | PASS | — |
| 5 | `test_04b_descriptions_are_whitespace_collapsed_and_otherwise_verbatim` | `CatalogueTests` | PASS | — |
| 6 | `test_05_get_standard_boq_items_returns_residential_only` | `ResidentialUnaffectedTests` | PASS | — |
| 7 | `test_06_residential_boq_creation_is_unchanged` | `ResidentialUnaffectedTests` | PASS | — |
| 8 | `test_06b_residential_boq_is_never_design_locked` | `ResidentialUnaffectedTests` | PASS | — |
| 9 | `test_06c_picker_404s_on_a_residential_project` | `ResidentialUnaffectedTests` | PASS | — |
| 10 | `test_07_catalogue_payload_supports_the_documented_searches` | `PickerTests` | PASS | — |
| 11 | `test_07b_boq_starts_empty_and_a_get_writes_nothing` | `PickerTests` | PASS | — |
| 12 | `test_07c_opex_designer_is_redirected_from_boq_detail` | `PickerTests` | PASS | — |
| 13 | `test_07d_non_author_roles_still_read_boq_detail` | `PickerTests` | PASS | — |
| 14 | `test_08_add_six_items_save_draft_and_reload` | `PickerTests` | PASS | — |
| 15 | `test_08b_save_is_a_reconciliation_so_removal_deletes` | `PickerTests` | PASS | — |
| 16 | `test_08c_a_forged_item_id_is_dropped_not_trusted` | `PickerTests` | PASS | — |
| 17 | `test_09_empty_category_indicator` | `PickerTests` | PASS | — |
| 18 | `test_09b_only_the_assigned_designer_may_write` | `PickerTests` | PASS | — |
| 19 | `test_10_mark_complete_freezes_the_boq` | `DesignLockTests` | PASS | — |
| 20 | `test_11_design_qc_sees_only_the_added_items` | `DesignLockTests` | PASS | — |
| 21 | `test_12_qc_rejection_reopens_the_full_picker` | `DesignLockTests` | PASS | — |
| 22 | `test_12b_a_rejection_that_was_not_about_the_boq_leaves_it_frozen` | `DesignLockTests` | PASS | — |
| 23 | `test_13_head_approval_locks_the_boq` | `DesignLockTests` | PASS | — |
| 24 | `test_14_pm_change_request_reopens_with_the_full_picker` | `DesignLockTests` | PASS | — |
| 25 | `test_15_group_aggregation_sums_across_sites_using_opex_items` | `AggregationAndBlastRadiusTests` | PASS | — |
| 26 | `test_15b_group_lock_still_beats_the_design_lock` | `AggregationAndBlastRadiusTests` | PASS | — |
| 27 | `test_16_nothing_outside_the_opex_boq_changed` | `AggregationAndBlastRadiusTests` | PASS | — |
| 28 | `test_off_catalogue_rows_render_and_are_not_offered_by_the_picker` | `OffCatalogueRowTests` | PASS | — |
| 29 | `test_off_catalogue_rows_survive_a_save_that_does_not_drop_them` | `OffCatalogueRowTests` | PASS | — |
| 30 | `test_off_catalogue_quantity_is_editable_when_the_field_is_posted` | `OffCatalogueRowTests` | PASS | — |
| 31 | `test_off_catalogue_rows_are_removed_when_dropped` | `OffCatalogueRowTests` | PASS | — |
| 32 | `test_review_panel_shows_off_catalogue_rows_separately` | `OffCatalogueRowTests` | PASS | — |

**32 PASS · 0 FAIL · 0 ERROR.**

---

## `PickerTests` — the eight Session D depends on

| Test | Result | What it pins down for Session D |
|---|---|---|
| `test_07_catalogue_payload_supports_the_documented_searches` | PASS | The `catalogue_json` payload's shape and searchability. Session D would add a sixth key here. |
| `test_07b_boq_starts_empty_and_a_get_writes_nothing` | PASS | **Directly contradicts Session D option 3.1(c).** This test asserts no row is created on GET. Writing mandatory rows on GET breaks it. |
| `test_07c_opex_designer_is_redirected_from_boq_detail` | PASS | The type gate keeping the Residential seeding path unreachable for OPEX. |
| `test_07d_non_author_roles_still_read_boq_detail` | PASS | SCM/PM are not caught by the redirect. |
| `test_08_add_six_items_save_draft_and_reload` | PASS | The POST persistence path end to end, with quantities surviving a reload. |
| `test_08b_save_is_a_reconciliation_so_removal_deletes` | PASS | **The exact property Session D's enforcement changes.** Removal-by-absence is asserted here. |
| `test_08c_a_forged_item_id_is_dropped_not_trusted` | PASS | The `catalogue_by_id` membership filter that drops unknown/inactive/Residential pks. |
| `test_09_empty_category_indicator` | PASS | Settled decision 7. Injected mandatory rows would change which categories read as empty. |
| `test_09b_only_the_assigned_designer_may_write` | PASS | The authority gate ANDed with the locks. |

*(Nine rows — `test_09` and `test_09b` are both in the class; the prompt's "six `PickerTests`" is
an undercount, see CONFLICTS.)*

**Two of these are load-bearing against Session D's open decisions, and both currently pass:**

- **`test_07b` is a live constraint on option 3.1(c).** Session D's audit reported that writing on
  GET has precedent in `boq_detail` but contradicts a documented decision four lines from where
  the change would go. This test converts that from a stylistic objection into a failing
  assertion. Choosing 3.1(c) means deliberately rewriting `test_07b`, which the Session D prompt's
  own rules would forbid without an explicit decision.
- **`test_08b` pins removal-by-absence**, which is the property that makes enforcement shape
  3.4(a) natural and 3.4(b) awkward. Whichever is chosen, this test is the one that will move.

Session D now has a real safety net rather than a dark suite.

---

## APPLICATION DEFECTS

**None.** No test failed, so no failure was diagnosed as the application being wrong.

**Hard stop 6 does not trigger** — no live production defect was revealed.

---

## UNCERTAIN

1. **Why the suite has been run the wrong way since it was written.** The correct invocation is in
   `test_settings.py`'s docstring, in `tests_design_part11.py:52`, and — as it turns out — in this
   project's own memory notes. I did not establish where the wrong invocation entered the habit.
   Not investigable from the repo.
2. **Whether the rest of the `projects` test suite passes under `--settings=solarpms.test_settings`.**
   Out of scope — the prompt restricts this session to `tests_design_part11`, and running other
   modules was not authorised. Worth one command before Session D starts, since the same
   invocation error may have been masking or fabricating results elsewhere.
3. **The 31-vs-32 count in PF3.** My class-by-class reading attributed 31 tests; the runner found
   and named 32, all of which appear in the results table. The discrepancy is in my manual
   attribution, not in the run. Every test is accounted for by name in the parsed output.
4. **Whether `test_07b` and `test_08b` should be treated as constraints or as candidates for
   revision** in Session D. That is a decision for the build session, not a finding here.

---

## CONFLICTS

1. **"all 32 tests in the module have errored in `setUp` since they were written, so not one
   assertion has ever executed."** The errors were real and reproducible, but they were a property
   of the **invocation**, not of the tests. Under the documented invocation the suite has presumably
   worked since the day it was written. Whether anyone has run it that way is unknown (UNCERTAIN 1).

2. **"Six `PickerTests` were written specifically against the POST path Session D will modify."**
   There are **eight** tests in `PickerTests` (nine including `test_09b`; the class spans lines
   324-480). This is an undercount in the prompt, and it is favourable — more coverage, not less.

3. **My own Session D audit was wrong on this point.** `SESSION_D_AUDIT.md` §4.5 reported the
   suite as blocked and estimated the fix as "one file, one `setUp`, most likely a single
   statement", with an unknown tail. The blockage was an invocation error and the tail is zero.
   The recommendation that the test session precede the build was still the right call — it cost
   one session and produced a green safety net plus two named constraints on Session D's open
   decisions — but the diagnosis behind it was incorrect and §4.5 should be read with this
   document beside it.

4. **The project memory note `part11-test-suite-broken` was wrong** and propagated the wrong
   diagnosis into at least Session D. It has been corrected as part of this session. The
   `dev-test-environment` note already recorded the correct invocation at its line 18; the two
   notes disagreed and the wrong one won.

---

## VERIFICATION

```
$ git diff --stat
(empty)

$ git status --porcelain
?? SESSION_B_AUDIT.md
?? SESSION_C_AUDIT.md
?? SESSION_D_AUDIT.md

$ python manage.py check
System check identified no issues (0 silenced).

$ python manage.py makemigrations projects --check --dry-run
No changes detected in app 'projects'
```

`git diff --stat` is empty rather than listing `projects/tests_design_part11.py`, because no fix
was required. No file in the repository was modified; the only additions are this report and the
three prior audit reports, all untracked `.md`.

---

## Closing line

**32 of 32 execute. 32 of 32 pass. 0 fail, 0 error. No application code, test code, or migration
was changed.**

The correct invocation, for every future session:

```
./venv/Scripts/python.exe manage.py test projects.tests_design_part11 --settings=solarpms.test_settings
```
