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