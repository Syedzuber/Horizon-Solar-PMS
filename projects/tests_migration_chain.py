"""The migration chain applies from empty, on Postgres. R-22's guard.

WHY THIS FILE EXISTS. On 03 Sep 2026 the phase 1 merge could not deploy: migration 0067
raised `TypeError: TaskTemplateTask() got unexpected keyword arguments: 'is_mirror'`, so
`manage.py migrate` exited non-zero, gunicorn never started, and production had to be
rolled back. Every one of the 1,060 tests passed before that merge and not one of them
could have caught it, because `solarpms/test_settings.py` disables migrations: the suite
builds its schema straight from today's `models.py` and has therefore NEVER RUN A
MIGRATION. A developer's local `migrate` was equally blind — 0067 had applied there months
earlier, so it was never re-run against the model state it actually gets on a fresh
database.

WHAT THIS GUARDS, precisely: that every migration, in order, applies to an EMPTY Postgres
database. That is a different question from "does the schema match the models", and it is
the question that was never being asked.

HOW, and why it is a subprocess rather than a `MigrationExecutor` call. The chain has to be
exercised under the REAL settings module, on a REAL Postgres database, whichever settings
the suite itself happens to be running under — that is the entire point, and a guard that
inherited the suite's settings could inherit the very shim that hid the bug. So it shells
out to `manage.py migrate` with `DATABASE_URL` pointed at a scratch database and
`DJANGO_SETTINGS_MODULE=solarpms.settings`, and reads the exit code. It costs one process
and one schema build; see `docs/execution-model.md` R-22 for the timing.

THIS TEST MUST NOT LEARN TO SKIP QUIETLY. If Postgres cannot be reached it FAILS rather
than skips. A skip is how this class of bug hides, and a green suite that silently declined
to check the chain is exactly the state the repository was in on 02 Sep.
"""
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

import dj_database_url
import psycopg2
from decouple import config as env_config
from django.test import SimpleTestCase

REPO_ROOT = Path(__file__).resolve().parent.parent

# Django prints "  Applying projects.0067_seed_residential_template_v1..." BEFORE running
# the migration and " OK" only after it succeeds, so the last name it printed is the one
# that broke. Matching the announcement rather than the traceback means this still names
# the migration when the failure is a database error with no Python frame in our code.
_APPLYING = re.compile(r'Applying ([\w.]+)\.\.\.')


def _real_database_config():
    """The Postgres the project actually uses — read the same way settings.py reads it.

    Deliberately NOT `django.db.connection.settings_dict`: under
    --settings=solarpms.test_settings that is in-memory SQLite, and this guard exists
    precisely to be immune to which settings module the suite was launched with.
    """
    return dj_database_url.config(default=env_config('DATABASE_URL'))


class MigrationChainAppliesFromEmptyTests(SimpleTestCase):
    """R-22. Runs the whole chain against a throwaway database and asserts it lands."""

    # SimpleTestCase touches no database of its own; everything happens in the subprocess
    # against a database this class creates and drops by hand.
    databases = []

    def setUp(self):
        self.cfg = _real_database_config()
        if 'postgresql' not in self.cfg.get('ENGINE', ''):
            self.fail(
                'DATABASE_URL does not point at Postgres, so the migration chain cannot '
                'be verified against the engine production runs on. This guard does not '
                'skip — see the module docstring.'
            )
        # A name unique per run: two sessions, or a suite and a developer, must not
        # collide on one scratch database.
        self.scratch = f'test_chain_{uuid.uuid4().hex[:12]}'
        self._admin_sql(f'CREATE DATABASE "{self.scratch}"')

    def tearDown(self):
        # Best effort by design: a leaked scratch database is untidy, but a tearDown that
        # raises would mask the failure the test is reporting.
        try:
            self._admin_sql(f'DROP DATABASE IF EXISTS "{self.scratch}"')
        except Exception:  # pragma: no cover - cleanup only
            pass

    # -- plumbing ----------------------------------------------------------------

    def _admin_sql(self, sql):
        """CREATE/DROP DATABASE, which cannot run inside a transaction."""
        conn = psycopg2.connect(
            dbname='postgres',
            user=self.cfg.get('USER') or None,
            password=self.cfg.get('PASSWORD') or None,
            host=self.cfg.get('HOST') or None,
            port=self.cfg.get('PORT') or None,
        )
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(sql)
        finally:
            conn.close()

    def _scratch_url(self):
        user = self.cfg.get('USER') or ''
        pwd  = self.cfg.get('PASSWORD') or ''
        host = self.cfg.get('HOST') or 'localhost'
        port = self.cfg.get('PORT') or 5432
        # The password is passed through unquoted on purpose: it is whatever DATABASE_URL
        # already carried, and re-encoding it here would change it.
        return f'postgresql://{user}:{pwd}@{host}:{port}/{self.scratch}'

    def _manage(self, *args, database_url):
        env = dict(os.environ)
        env['DATABASE_URL'] = database_url
        # The real settings, never the caller's. python-decouple reads os.environ before
        # .env, so the line above wins over the developer's local database.
        env['DJANGO_SETTINGS_MODULE'] = 'solarpms.settings'
        return subprocess.run(
            [sys.executable, 'manage.py', *args],
            cwd=str(REPO_ROOT), env=env,
            capture_output=True, text=True, encoding='utf-8', errors='replace',
        )

    # -- the guard ---------------------------------------------------------------

    def test_every_migration_applies_to_an_empty_database(self):
        # `--run-syncdb` because that is Railway's start command verbatim
        # (`migrate --run-syncdb && collectstatic && gunicorn`). It is a no-op while every
        # installed app has migrations, and running the deploy's exact command costs
        # nothing while guessing at it costs the next incident.
        proc = self._manage('migrate', '--run-syncdb', '--noinput', '-v', '1',
                            database_url=self._scratch_url())
        if proc.returncode == 0:
            return

        applied = _APPLYING.findall(proc.stdout)
        culprit = applied[-1] if applied else '(none announced — the failure is before '\
                                              'the first migration)'
        self.fail(
            '\n'
            '================ THE MIGRATION CHAIN DOES NOT APPLY ================\n'
            f'FAILING MIGRATION: {culprit}\n'
            f'{len(applied) - 1} migration(s) applied before it.\n'
            '\n'
            'A fresh database — which is what production gets for any migration that '
            'has never run there — cannot be built. `manage.py migrate` exits non-zero, '
            'so the deploy stops before the web server starts.\n'
            '\n'
            'The usual cause is R-22: a migration calling live application code whose '
            'signature has moved on. See docs/execution-model.md R-22.\n'
            '\n'
            '---------------- migrate stdout ----------------\n'
            f'{proc.stdout[-4000:]}\n'
            '---------------- migrate stderr ----------------\n'
            f'{proc.stderr[-6000:]}\n'
            '===================================================================='
        )

    def test_models_and_migrations_agree(self):
        """No un-migrated model change is sitting in models.py.

        The companion half of the same question. The chain applying proves the migrations
        are runnable; this proves they are COMPLETE — a field added to models.py without a
        migration passes the whole suite under the no-migrations shim and then serves 500s
        in production against a column that does not exist.

        Runs against the scratch database setUp already built so it needs no live one.
        """
        proc = self._manage('makemigrations', '--check', '--dry-run', '-v', '1',
                            database_url=self._scratch_url())
        self.assertEqual(
            proc.returncode, 0,
            'models.py has changes with no migration written for them.\n'
            f'--- stdout ---\n{proc.stdout[-3000:]}\n'
            f'--- stderr ---\n{proc.stderr[-3000:]}'
        )
