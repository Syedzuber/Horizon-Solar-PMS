"""Test-only settings: run tests on REAL Postgres, WITH migrations.

    python manage.py test projects.tests_sched1_due_date_source --settings=solarpms.test_settings_postgres

Django creates and destroys `test_<NAME>` (locally `test_solarpms_local`); the working
database is never touched. Add --noinput to replace a test database a previous run left
behind.

WHY THIS FILE EXISTS. solarpms/test_settings.py runs the suite on in-memory SQLite with
migrations disabled, which is fast and is still the default. But some behaviour only
shows on Postgres: an aborted transaction after a failed statement, constraint names in
error text, and the rows data migrations seed (the 37 Residential catalogue items, for
example). This module keeps the real database and the real migration chain and changes
nothing else about the DB.

WHY THE KEYS ARE BLANKED. These are the real settings, and the local .env has carried
LIVE ZeptoMail and Interakt keys. A test that turns a notification switch on would send
real email or WhatsApp to real staff, and a transaction rollback cannot recall it. The
overrides below are the same safety ones test_settings.py makes; keep them in step with
that file.

NOT EVERY MODULE RUNS HERE. Much of the suite is written against an empty schema and
collides with the rows migrations seed (see test_settings.py's docstring: ~308 errors
under a migrated database). Run named modules or classes, not the whole app.
"""
from .settings import *  # noqa: F401,F403

# Same as test_settings.py: speed only.
PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']

# Same as test_settings.py: never reach a real email or WhatsApp provider from a test.
ZEPTOMAIL_API_KEY = ''
INTERAKT_API_KEY = ''

# Same as test_settings.py: deferred sends run inline, so tests see them deterministically.
NOTIFY_DEFERRED_SYNC = True
