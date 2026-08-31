"""
Shared machinery for the three demo-data commands: the local-database interlock and
the creation manifest.

NOT A MANAGEMENT COMMAND, AND THE LEADING UNDERSCORE IS LOAD-BEARING. Django's
`find_commands()` lists every module in a `commands/` package that does not start with
`_`, so without it this file would appear as `python manage.py _demo_support` and
crash on invocation. It lives here rather than in `projects/` because it is tooling
for local demo data and ships no product behaviour.

WHY A MANIFEST AND NOT A NAME PREFIX
------------------------------------
Until this change `teardown_opex_test_data` found its targets by pattern-matching
LIVE TABLES:

    Project.objects.filter(project_id__startswith='Test-')

That is a delete whose blast radius is decided by a string comparison against every
row in the database, including rows it did not create. It worked, and it was one
mistyped constant away from not working. The manifest inverts the relationship: the
seed writes down every primary key it creates, and the teardown deletes that list and
refuses to do anything else. A row the seed did not create cannot be selected by the
teardown, because the teardown never runs a query that could select one.

The `DEMO` namespace survives as a SECOND line of defence, not the first: anything
that ever escapes the manifest is identifiable by eye in a list, an export or a
dashboard. It is not what the teardown reads.

WHY THERE IS NO FALLBACK WHEN THE MANIFEST IS MISSING
-----------------------------------------------------
A fallback that pattern-matched live tables when the manifest was absent would
reintroduce, as the error path, exactly the failure mode this design exists to
prevent — and the error path is where it is least likely to be tested. The teardown
refuses instead. See `MANIFEST_MISSING_MESSAGE` below, which also names the
regression this change introduces, because the first person to hit it would otherwise
reasonably conclude the tool is broken.
"""
import json
import sys
from datetime import datetime, timezone as dt_timezone
from pathlib import Path

from django.conf import settings
from django.db.models import Max

# ---------------------------------------------------------------------------
# Namespace. Every identifier the demo commands write carries this, so anything
# that escapes the manifest is unmistakable in a list or an export.
#
# NO HYPHEN, AND THAT IS FORCED BY THE PRODUCT, NOT A PREFERENCE. An OPEX site's
# project_id IS its site_code, and OpexSiteForm.clean_site_code() runs the entered
# value through normalize_program_code(), which strips everything outside [A-Z0-9].
# 'DEMO-OPEX-01' would be stored as 'DEMOOPEX01' regardless. Choosing the stripped
# form up front means the seeded ID is the ID the real code path produces, rather
# than the seed setting project_id explicitly to defeat its own creation path.
# ---------------------------------------------------------------------------
DEMO_PREFIX = 'DEMO'

# The demo accounts' email domain. `.invalid` is reserved by RFC 2606 and can never
# resolve, so a misconfigured notification run cannot reach a real inbox.
DEMO_EMAIL_DOMAIN = 'demo.invalid'

#: Printed at the end of a seed run. Long enough for UserCreateForm's min_length=8.
DEMO_PASSWORD = 'DemoPass!2026'


# ---------------------------------------------------------------------------
# TASK 1 — the interlock
# ---------------------------------------------------------------------------
# Hosts that mean "this machine". The empty string is what dj_database_url leaves
# behind for a socket connection, and a leading '/' is an explicit socket path.
LOCAL_HOSTS = {'', 'localhost', '127.0.0.1', '::1'}

OVERRIDE_FLAG = '--i-know-this-is-not-local'


def database_host():
    """DATABASES['default']['HOST'], or '' for a local socket.

    HOST only. The same dict holds PASSWORD and it is never read here, which is the
    rule `send_eod_digest` set and this follows.
    """
    return (settings.DATABASES.get('default', {}).get('HOST') or '')


def database_name():
    return settings.DATABASES.get('default', {}).get('NAME') or '(unset)'


def database_is_local():
    host = database_host()
    return host in LOCAL_HOSTS or host.startswith('/')


def print_db_banner(command):
    """Always the FIRST line of output, on both commands, on every run.

    Same shape as `send_eod_digest`: host and name, never the password. A demo
    command that writes to the wrong database is the one failure this whole module
    exists to prevent, so the operator is told which database before anything else
    is printed, including a dry run's plan.
    """
    host = database_host() or '(none - local socket)'
    command.stdout.write(f"[db] host={host} name={database_name()}")
    # FLUSHED, AND THAT IS NOT DECORATION. "First line of output, always" is the whole
    # requirement, and it is only true in a terminal without this: redirected to a file
    # or a pipe, stdout is block-buffered while stderr is not, so the refusal below
    # would print BEFORE the banner naming the database it is refusing. A build log or
    # a CI capture is exactly where somebody reads this ordering and trusts it.
    try:
        command.stdout.flush()
    except (AttributeError, ValueError):
        pass    # a StringIO in a test, or an already-closed stream — never fatal


def require_local_database(command, override, what_will_be_written):
    """Refuse to run against a non-local database unless `override` was passed.

    `what_will_be_written` is a list of plain-English lines. It appears in the
    refusal AND in the warning printed when the override is used, because the point
    of naming the writes is that somebody about to make a mistake reads them — and
    the person using the override is the one making it.

    Exits the process on refusal rather than raising CommandError, matching
    send_eod_digest: a non-zero exit is what a wrapper script or a cron job reads.
    """
    if database_is_local():
        return

    host = database_host()
    writes = '\n'.join(f'                  {line}' for line in what_will_be_written)

    if not override:
        command.stderr.write(
            f'REFUSING TO RUN: this database is not local.\n'
            f'  database host : {host}\n'
            f'  database name : {database_name()}\n'
            f'  would write   :\n{writes}\n'
            f'\n'
            f'Demo data must never reach production. It pollutes the CEO dashboard,\n'
            f'the EOD digest and every execution counter, and it teaches users that\n'
            f'the system is a toy.\n'
            f'\n'
            f'If this really is a throwaway remote database, re-run with '
            f'{OVERRIDE_FLAG}.'
        )
        sys.exit(1)

    command.stdout.write(command.style.WARNING(
        f'\n'
        f'!! {OVERRIDE_FLAG} WAS PASSED.\n'
        f'!! This database is NOT local.\n'
        f'!!   database host : {host}\n'
        f'!!   database name : {database_name()}\n'
        f'!!   writing       :\n{writes}\n'
    ))


# ---------------------------------------------------------------------------
# TASK 2 — the manifest
# ---------------------------------------------------------------------------
#: Default manifest location. DELIBERATELY OUTSIDE THE REPOSITORY — a file inside it
#: would be one `git add -A` away from being committed, and this directory also holds
#: `railway_backup.dump`, a production database dump. Overridable with --manifest.
DEFAULT_MANIFEST_PATH = Path.home() / '.horizon-pms-demo' / 'demo_manifest.json'

MANIFEST_VERSION = 1

MANIFEST_MISSING_MESSAGE = """\
REFUSING TO DELETE: no readable manifest at
  {path}

This command deletes ONLY what a seed run recorded creating. It does not search for
rows to delete, and it will not fall back to matching names or ID prefixes against
live tables — a delete whose blast radius is decided by a string comparison is the
failure mode the manifest exists to prevent, and an error path is the worst place to
reintroduce it.

If you are looking for the old behaviour: BEFORE this change, this command found its
targets with `project_id__startswith='Test-'` and needed no manifest. Rows created by
that older version of the seed CANNOT be removed by this version. To clear them,
either check out a commit from before the manifest change and run that version, or
delete them by hand. This is a known, accepted regression on a local database.

If a seed did run: pass --manifest PATH pointing at the file the seed printed."""


def _label(instance):
    """A short human tag stored beside each pk, so a manifest can be read by a person.

    Best-effort and never fatal: a row whose __str__ raises must not make the seed
    fail, and the label is never used to FIND anything — only the pk is.
    """
    try:
        return str(instance)[:120]
    except Exception:
        return ''


class Manifest:
    """Every primary key a seed run created, by model, in creation order.

    Appended to, never rewritten: `seed_scm_handoff_data` layers rows on top of what
    `seed_opex_test_data` created and records them into the SAME file, so one teardown
    removes both. That is the whole reason the extend was chosen over a second command
    pair — 390 rows sitting outside the manifest would make the discipline decorative.
    """

    def __init__(self, path):
        self.path = Path(path)
        self.entries = []       # [{'model': 'projects.Project', 'pk': 9, 'label': '...'}]
        self._seen = set()      # (model_label, pk) — a row is recorded at most once
        self.meta = {}

    # ---- writing -------------------------------------------------------------
    @staticmethod
    def model_label(model):
        return f'{model._meta.app_label}.{model.__name__}'

    def add(self, instance):
        """Record one saved instance. Order of calls IS the deletion order, reversed."""
        key = (self.model_label(type(instance)), instance.pk)
        if instance.pk is None or key in self._seen:
            return instance
        self._seen.add(key)
        self.entries.append({
            'model': key[0], 'pk': key[1], 'label': _label(instance),
        })
        return instance

    def add_all(self, iterable):
        for obj in iterable:
            self.add(obj)
        return iterable

    def add_new_since(self, model, high_water):
        """Record every row of `model` with pk > `high_water`.

        For rows the seed does not construct itself — the phases and tasks
        `_attach_task_template()` bulk-creates, the ActivityLog and StatusTransition
        rows the real code paths write on the way past. Ordered by pk, which for a
        single-threaded local seed is creation order.
        """
        added = []
        for obj in model.objects.filter(pk__gt=high_water).order_by('pk'):
            before = len(self.entries)
            self.add(obj)
            if len(self.entries) > before:
                added.append(obj)
        return added

    def save(self, db_host, db_name):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            'version': MANIFEST_VERSION,
            'written_at': datetime.now(dt_timezone.utc).isoformat(),
            'database': {'host': db_host or '(none - local socket)', 'name': db_name},
            'namespace': DEMO_PREFIX,
            'entries': self.entries,
        }
        self.path.write_text(json.dumps(payload, indent=2), encoding='utf-8')
        return self.path

    # ---- reading -------------------------------------------------------------
    @classmethod
    def load(cls, path):
        """Return a Manifest, or None if the file is missing or unreadable.

        A malformed file is treated the same as a missing one — the teardown refuses
        either way, and guessing at half-parsed JSON is a worse answer than stopping.
        """
        p = Path(path)
        try:
            raw = json.loads(p.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return None
        if not isinstance(raw, dict) or not isinstance(raw.get('entries'), list):
            return None
        manifest = cls(p)
        manifest.meta = {k: v for k, v in raw.items() if k != 'entries'}
        for row in raw['entries']:
            if isinstance(row, dict) and 'model' in row and 'pk' in row:
                manifest.entries.append(row)
                manifest._seen.add((row['model'], row['pk']))
        return manifest

    def counts_by_model(self):
        counts = {}
        for row in self.entries:
            counts[row['model']] = counts.get(row['model'], 0) + 1
        return counts


def high_water(model):
    """The current maximum pk for `model`, or 0 on an empty table.

    Taken before a seed step so `Manifest.add_new_since()` can pick up rows created
    as a SIDE EFFECT of a real code path — which are the rows nobody thinks to record
    and therefore exactly the ones that leak.
    """
    return model.objects.aggregate(_m=Max('pk'))['_m'] or 0
