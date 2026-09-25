"""
Shared machinery for `seed_walkthrough` and `teardown_walkthrough`: the four-check
database refusal, the run sandbox, the simulated clock and the per-area manifest.

NOT A MANAGEMENT COMMAND. The leading underscore keeps it out of Django's
`find_commands()`, exactly as `_demo_support` does.

WHY THIS IS NOT `_demo_support.require_local_database()`
--------------------------------------------------------
That interlock asks one question — is the host local? — and has an override flag. The
local database on this machine is a RESTORED PRODUCTION DUMP on `localhost`: 76 users,
67 of them real people (audited 25 Sep 2026, docs/WALKTHROUGH_SEED_AUDIT.md §A5). A
host check passes it. The walkthrough seed may only ever run on a database that is
synthetic end to end, so it asks four questions, all of which must pass, and there is
NO override flag:

  1. the host is local (the same LOCAL_HOSTS set `_demo_support` uses);
  2. the database NAME starts with `solarpms_walk` — refuses `railway` and
     `solarpms_local` by name;
  3. no auth.User carries an email outside `.invalid` — refuses production and every
     restore of it, whatever it is called or hosted on — with EXACTLY ONE permitted
     exception, `utils.RESIDENTIAL_FINANCE_ASSIGNEE_EMAIL`, read from utils at call time
     (see `permitted_real_email()` for why that one address must exist here);
  4. no NotificationLog row has status 'sent' on an EXTERNAL channel (email, whatsapp).
     A database that has sent something to a person is not a walkthrough database.
     In-app 'sent' rows are allowed: `_send_in_app` logs every in-app notice as 'sent',
     and the states this seed drives write them as artifacts (decided at sign-off).

The refusal names every check that failed, not just the first — with one deliberate
exception: checks 3 and 4 query the database, so they run only once checks 1 and 2 have
passed. A database that is remote or wrongly named is refused without being read.
"""
import json
from contextlib import contextmanager, ExitStack
from datetime import datetime, timedelta, timezone as dt_timezone
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.core.management.base import CommandError
from django.db import connection
from django.test.utils import override_settings

from projects.management.commands._demo_support import (
    LOCAL_HOSTS, _label, database_host, database_name, high_water,
)

# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------
WALK_PREFIX       = 'WALK'
WALK_EMAIL_DOMAIN = 'walk.invalid'
WALK_DB_PREFIX    = 'solarpms_walk'

#: Channels that reach a person outside the product. A 'sent' row on one of these
#: fails check 4.
EXTERNAL_CHANNELS = ('email', 'whatsapp')

#: The bucket name every stubbed file records. It exists nowhere, so no teardown and no
#: download can ever resolve it to a real object.
STUB_BUCKET = 'walkthrough-stub'

#: Every stubbed file is uploaded under a name that says what it is, so a walker who
#: clicks a download that does nothing can read why from the file name on screen.
STUB_FILE_PREFIX = 'SEEDED-NO-FILE'

MANIFEST_DIR     = Path.home() / '.horizon-pms-walkthrough'
MANIFEST_VERSION = 1


def stub_file_name(kind, extension):
    return f'{STUB_FILE_PREFIX}-{kind}.{extension}'


def permitted_real_email():
    """The ONE non-.invalid address check 3 admits.

    `utils.attach_residential_template()` resolves the Finance owner of the send-invoice
    and finance-confirmation tasks by `user__email == RESIDENTIAL_FINANCE_ASSIGNEE_EMAIL`
    and raises if it is missing, rolling back `project_activate`. So a database with no
    user at that address cannot activate a Residential project at all. The seed creates
    that user with both notification preferences OFF (sign-off item 2). The hardcoded
    address is itself a defect, recorded in docs/WALKTHROUGH_DATA.md and not fixed here.

    Read from utils at call time, never copied, so this check can never admit a
    different address from the one the product demands.
    """
    from projects import utils
    return utils.RESIDENTIAL_FINANCE_ASSIGNEE_EMAIL


# ---------------------------------------------------------------------------
# The four checks
# ---------------------------------------------------------------------------
def database_is_local():
    host = database_host()
    return host in LOCAL_HOSTS or host.startswith('/')


def refusal_reasons():
    """Every failed check, as a list of plain-English lines. Empty means proceed."""
    from django.contrib.auth.models import User
    from projects.models import NotificationLog

    reasons = []
    host, name = database_host(), database_name()

    if not database_is_local():
        reasons.append(f'check 1 (local host): host {host!r} is not local')
    if not str(name).startswith(WALK_DB_PREFIX):
        reasons.append(f'check 2 (database name): {name!r} does not start with '
                       f'{WALK_DB_PREFIX!r}')

    # Checks 3 and 4 read the database, so they only run once 1 and 2 have passed. A
    # query against a production host is itself something this module exists to avoid.
    if reasons:
        return reasons

    real = (User.objects.exclude(email__iendswith='.invalid')
            .exclude(email__iexact=permitted_real_email()))
    real_count = real.count()
    if real_count:
        reasons.append(f'check 3 (synthetic users): {real_count} user(s) have an email '
                       f'outside .invalid other than the one permitted address — this '
                       f'database holds real people')

    sent = NotificationLog.objects.filter(channel__in=EXTERNAL_CHANNELS, status='sent')
    sent_count = sent.count()
    if sent_count:
        reasons.append(f'check 4 (nothing sent): {sent_count} email/WhatsApp '
                       f'NotificationLog row(s) have status sent — this database has '
                       f'sent something to a person')
    return reasons


def print_db_banner(command):
    """Always the first line of output. Host and name — never a URL, never a password."""
    host = database_host() or '(none - local socket)'
    command.stdout.write(f'[db] host={host} name={database_name()}')
    try:
        command.stdout.flush()
    except (AttributeError, ValueError):
        pass


def require_walkthrough_database(command):
    reasons = refusal_reasons()
    if not reasons:
        return
    lines = '\n'.join(f'  - {r}' for r in reasons)
    raise CommandError(
        f'REFUSING TO RUN: this is not a walkthrough database.\n{lines}\n'
        f'The walkthrough seed runs only on a synthetic database named '
        f'{WALK_DB_PREFIX}*, on this machine. There is no override flag.')


# ---------------------------------------------------------------------------
# The sandbox — active only while a seed run drives the product
# ---------------------------------------------------------------------------
class _StubBucket:
    def upload(self, *args, **kwargs):
        return {'Key': kwargs.get('path', '')}

    def remove(self, paths):
        return []

    def create_signed_url(self, *args, **kwargs):
        return {}

    def get_public_url(self, path):
        return ''


class _StubStorage:
    def from_(self, bucket):
        return _StubBucket()


class StubSupabaseClient:
    """What `order_views._upload_and_record_documents` needs from a Supabase client:
    `.storage.from_(bucket).upload(...)` and `.remove([...])`. Both do nothing."""
    storage = _StubStorage()


def _stub_design_upload(file_obj, path):
    # Same validation the real function runs first, so a file the product would refuse
    # is still refused; only the network write is skipped.
    from projects.design_storage import validate_design_file
    validate_design_file(file_obj)
    return STUB_BUCKET, path


@contextmanager
def seed_sandbox():
    """Everything a seed run needs to drive the real views without reaching anything
    real. In-process only; nothing here survives the run.

      * Storage: `design_views.upload_design_file` and
        `supabase_storage.get_supabase_client` are stubbed, the public bucket name is
        overridden to STUB_BUCKET, and SUPABASE_URL / SUPABASE_KEY are BLANKED — so any
        storage call this module did not stub raises instead of reaching the production
        project whose key is in the local .env.
      * Notifications: INTERAKT_API_KEY and ZEPTOMAIL_API_KEY blanked, as
        solarpms/test_settings.py does. With the master switches off (the users area
        sets them) no external send is attempted; with the keys blank none could succeed.
      * Sessions: the cache backend, so driving the views as fifteen people writes no
        django_session rows that the manifest would then have to track.
      * ALLOWED_HOSTS gains 'localhost' so `Client(SERVER_NAME='localhost')` is served.
    """
    hosts = list(getattr(settings, 'ALLOWED_HOSTS', []) or [])
    if 'localhost' not in hosts:
        hosts.append('localhost')
    with ExitStack() as stack:
        stack.enter_context(override_settings(
            SUPABASE_URL='', SUPABASE_KEY='', SUPABASE_BUCKET=STUB_BUCKET,
            INTERAKT_API_KEY='', ZEPTOMAIL_API_KEY='',
            SESSION_ENGINE='django.contrib.sessions.backends.cache',
            ALLOWED_HOSTS=hosts,
        ))
        stack.enter_context(mock.patch(
            'projects.design_views.upload_design_file', side_effect=_stub_design_upload))
        stack.enter_context(mock.patch(
            'projects.supabase_storage.get_supabase_client',
            return_value=StubSupabaseClient()))
        yield


# ---------------------------------------------------------------------------
# The simulated clock
# ---------------------------------------------------------------------------
class SimClock:
    """Drives the product at a past time, so ages, pools and "N days ago" read like a
    real portfolio.

    BACKDATING IS DONE BY TIME TRAVEL, NOT BY REWRITING ROWS. While a step runs,
    `django.utils.timezone.now` returns the simulated instant, so every timestamp the
    VIEWS write on the way — created_at, occurred_at, released_at, requested_date,
    completed_at and the rest — is written once, by the product, at that instant. No row
    is updated afterwards. Only rows this seed's own requests create are affected.

    THE CLOCK NEVER RUNS BACKWARDS WITHIN A HISTORY. Each call to now() returns an
    instant one millisecond after the last, and `at()` refuses to move a history to a
    point before where it already stands, so a subject's StatusTransition rows can never
    contradict their own sequence (sign-off item 4).
    """

    def __init__(self, real_now=None):
        from django.utils import timezone
        self._real_now = real_now or timezone.now()
        self._cursor = None
        self._history_high = {}

    def instant(self, days_ago, hours=0):
        return self._real_now - timedelta(days=days_ago, hours=-hours)

    @contextmanager
    def at(self, days_ago, history, hours=0):
        from django.utils import timezone
        target = self.instant(days_ago, hours)
        high = self._history_high.get(history)
        if high is not None and target <= high:
            target = high + timedelta(milliseconds=1)
        if target > self._real_now:
            raise CommandError(f'SimClock: {history} would be written in the future.')
        self._cursor = target

        def _now():
            self._cursor = self._cursor + timedelta(milliseconds=1)
            return self._cursor

        with mock.patch.object(timezone, 'now', side_effect=_now):
            yield
        self._history_high[history] = self._cursor


# ---------------------------------------------------------------------------
# The manifest — per area, bound to ONE database
# ---------------------------------------------------------------------------
def database_fingerprint():
    """Identity of THIS database, not merely its name.

    A dropped-and-recreated `solarpms_walk` has the same name and, after a re-seed,
    much the same primary keys — so a manifest left over from the old one could point
    a teardown at the new one's rows. PostgreSQL gives every database a new oid when it
    is created, so (name, oid) cannot survive a DROP DATABASE. Other backends (the test
    suite's SQLite) have no such id; there the name alone is used and tests pass their
    own manifest path.
    """
    name = database_name()
    if connection.vendor == 'postgresql':
        with connection.cursor() as cursor:
            cursor.execute('SELECT oid FROM pg_database WHERE datname = current_database()')
            oid = cursor.fetchone()[0]
        return f'postgresql:{name}:{oid}'
    return f'{connection.vendor}:{name}'


def default_manifest_path():
    safe = ''.join(c if c.isalnum() or c in '-_' else '_' for c in str(database_name()))
    return MANIFEST_DIR / f'{safe}.json'


class AreaManifest:
    """{area: [{'model', 'pk', 'label'}]}, plus the fingerprint of the database it was
    written against. Stored OUTSIDE the repository — this directory also holds a
    production dump, and a file inside it is one `git add -A` from being committed."""

    def __init__(self, path, fingerprint):
        self.path = Path(path)
        self.fingerprint = fingerprint
        self.areas = {}

    @classmethod
    def load(cls, path):
        p = Path(path)
        try:
            raw = json.loads(p.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return None
        if not isinstance(raw, dict) or not isinstance(raw.get('areas'), dict):
            return None
        manifest = cls(p, raw.get('fingerprint'))
        manifest.areas = raw['areas']
        return manifest

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            'version': MANIFEST_VERSION,
            'written_at': datetime.now(dt_timezone.utc).isoformat(),
            'database': {'host': database_host() or '(none - local socket)',
                         'name': database_name()},
            'fingerprint': self.fingerprint,
            'namespace': WALK_PREFIX,
            'areas': self.areas,
        }
        self.path.write_text(json.dumps(payload, indent=2), encoding='utf-8')
        return self.path

    def record_area(self, area, rows):
        self.areas[area] = [
            {'model': f'{type(r)._meta.app_label}.{type(r).__name__}', 'pk': r.pk,
             'label': _label(r)}
            for r in rows
        ]

    def entries(self, area):
        return self.areas.get(area, [])

    def missing_rows(self, area):
        """Entries of `area` whose row no longer exists."""
        from django.apps import apps
        missing = []
        for row in self.entries(area):
            try:
                model = apps.get_model(row['model'])
            except (LookupError, ValueError):
                missing.append(row)
                continue
            if not model.objects.filter(pk=row['pk']).exists():
                missing.append(row)
        return missing

    def counts(self, area):
        counts = {}
        for row in self.entries(area):
            counts[row['model']] = counts.get(row['model'], 0) + 1
        return counts


def watched_models():
    """Every model an area's requests can write a row in: the whole `projects` app —
    INCLUDING the auto-created many-to-many through tables (a vendor's categories, a
    site's coordinators), which `get_models()` leaves out by default and which a teardown
    would otherwise find as unrecorded rows — and auth.User. Sessions are excluded by
    construction: the sandbox keeps them in cache."""
    from django.apps import apps
    from django.contrib.auth.models import User
    return (list(apps.get_app_config('projects').get_models(include_auto_created=True))
            + [User])


def take_marks():
    return {m: high_water(m) for m in watched_models()}


def rows_since(marks):
    """Every row created since `marks`, model by model, in pk order."""
    rows = []
    for model, mark in marks.items():
        rows.extend(model.objects.filter(pk__gt=mark).order_by('pk'))
    return rows
