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
