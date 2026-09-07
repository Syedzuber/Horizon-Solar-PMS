"""
Fixture BOQ rows for SCMPILOT03, 04 and 05 so the group lock has quantities to freeze.

WHY THIS COMMAND EXISTS, AND WHY IT IS NOT A SEED.

`seed_scm_pilot` set `DesignAssignment.status = 'released'` directly and stamped
`DesignAttempt.boq_submitted_at`, but never created a `BOQ` row. All six SCMPILOT sites
carry `BOQ: None` and zero `BOQItem` rows. The consequence is on screen: the design
module's QC page reads the DesignAttempt stamp and shows a green "BOQ: Complete" badge,
while the BOQ page reads the BOQ row and says "BOQ not yet created by the Design team".
Both read real data; nothing reconciles them. That disagreement is a product finding and
is recorded in EXECUTION_MODULE_DEFERRED.md §10, NOT fixed here.

THE REAL AUTHORING ROUTE IS CLOSED ON THESE THREE SITES. `project_boq_is_design_locked()`
is true — attempt 1 carries `boq_submitted_at` — so both write paths (the picker
`opex_boq_entry` and `opex_boq_upload`) refuse before they reach a write. There is one
reopen route, and it was measured rather than assumed: a released site sitting in a DRAFT
procurement group is change-requestable, and `_open_next_attempt(redo=None)` skips
artifact carry-forward, so the design lock lifts and the picker opens. It was rejected
deliberately, for three costs it charges and this command does not:

  * three fabricated CAD archives in the LIVE production Supabase bucket — the seed
    created no DesignFile rows at all, so `_package_is_complete()` can never be satisfied
    on these sites without inventing one
  * destruction of the `released_at` dates the post-QC pool ages off (30 / 22 / 15 days)
  * modification of DesignAssignment / DesignAttempt / ArkaSubmission rows

The route itself is recorded in EXECUTION_MODULE_DEFERRED.md §11 so the next person to
meet it does not have to rediscover it.

WHAT THIS COMMAND WRITES, AND NOTHING ELSE:

    BOQ        1 row per site  x 3 sites  =  3 rows
    BOQItem   28 rows per site x 3 sites  = 84 rows

No BOQRevision. The group lock was read before this was written and it snapshots nothing:
`site_group_lock()` stamps `SiteGroup.status / locked_by / locked_at`, writes one
ActivityLog line per member, and returns. The freeze is the live predicate
`project_boq_is_group_locked()`, evaluated per request. So what a lock needs in order to
freeze something real is not a snapshot — it is BOQItem rows with `boq_quantity > 0` and a
non-null `item_master`, because that is the only join `aggregate_group_boq()` runs on.
A header row with no items would reproduce the current problem one layer down, which is
why the item set below is the point of this command and the BOQ row is scaffolding.

THE SHAPE IS COPIED FROM REAL PRODUCTION DATA, not from the Residential workflow. MB0141,
MB0191 and MB0164 are genuine designer-authored OPEX sheets in the MPUVNL tender, and all
three carry `status='Draft', submitted_by=None, submitted_at=None, version=1, notes=None`
— because the OPEX picker sets `project` and nothing else. Two consequences are
load-bearing:

  * `submitted_by` STAYS None. `_apply_boq_acknowledgement()` guards its notification on
    `if boq.submitted_by:` — stamping a seeded user there would arm a real notification at
    a real address the first time SCM acknowledges one of these. None is also what the
    real path produces, so there is no tension between safe and faithful.
  * category and unit are copied FROM THE MASTER, never from `BOQItem.CATEGORY_CHOICES` /
    `UOM_CHOICES`. The OPEX picker writes past both lists — production contains
    'Module', 'MMS', 'Solar Meter + CT', 'Meter', 'Pair' — and choices are form-level
    only, never enforced by the database. Copying the choices lists would produce rows the
    product cannot produce.

ADDITIVE ONLY. Every write is a `get_or_create`. Nothing is UPDATEd, nothing is DELETEd,
and the untouchable digest below proves it for the three sites that must not move.
"""

import hashlib
import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Max
from django.utils import timezone


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------

#: The only three sites this command may touch.
#:
#: 01 and 02 are already in locked procurement group 40 and are spent — writing
#: quantities onto a site whose group is locked would put numbers underneath a lock that
#: was taken before they existed, which is worse than the empty aggregate it would fix.
#: 06 is at `arka_submitted` and is the site the pilot walks by hand; giving it a fixtured
#: BOQ would delete the one demonstration that the real path works.
FIXTURE_SITES = ('SCMPILOT03', 'SCMPILOT04', 'SCMPILOT05')

#: Proven untouched at the end of every run, by digest. Not a comment — an assertion.
UNTOUCHABLE_SITES = ('SCMPILOT01', 'SCMPILOT02', 'SCMPILOT06')

#: Provenance vocabulary, carried verbatim from seed_scm_pilot so the two manifests speak
#: one language. Only one of the three words applies to anything this command makes.
PROV_FIXTURED = 'FIXTURED'

DEFAULT_MANIFEST_PATH = Path.home() / '.horizon-pms-scmpilot' / 'scmpilot_manifest.json'

LOCAL_HOSTS = {'', 'localhost', '127.0.0.1', '::1'}


# ---------------------------------------------------------------------------
# The item set
# ---------------------------------------------------------------------------
#
# SCALED FROM A REAL SHEET, NOT INVENTED. MB0141 is a genuine 60.00 kW production BOQ
# carrying 105 modules (=> ~570 Wp), 52 lines across 15 categories. These three sites are
# 185.50 / 120.00 / 95.00 kW, and the quantities below are that sheet scaled by capacity
# and rounded to whole units, with the inverter and ACDB rows picked to match each site's
# actual size rather than scaled.
#
# QUANTITIES DIFFER PER SITE ON PURPOSE. Three sites carrying the same number would make
# the group aggregate a multiplication rather than a sum, and the per-line "which sites
# are behind this total" breakdown — the thing that makes the aggregate auditable instead
# of a number SCM has to trust — would demonstrate nothing.
#
# ALL FOUR DELIVERY-MIRROR CATEGORIES ARE COVERED: Module, Inverter, MMS and BOS. Those
# are the OPEX catalogue's names for the four `DC_CATEGORY_TO_MIRROR_CODE` maps onto
# ('Solar Modules' and 'Structure' are RESIDENTIAL vocabulary and appear nowhere in the
# 207-row tender catalogue). The coherence this buys is NARRATIVE, not mechanical: the
# mirror derivation reads `DCLineItem.boq_category`, which the DC create form types
# independently of any BOQ. Nothing checks a delivery against the bill it was raised
# from — see EXECUTION_MODULE_DEFERRED.md §2 (B-18).
#
# BOTH MANDATORY ITEMS ARE CARRIED WITH QUANTITIES. OPX-001 and OPX-027 are the two
# `is_mandatory` OPEX rows, and `design_boq_complete()` refuses a sheet that leaves either
# without one. A fixture that omitted them would produce a sheet no designer could have
# marked complete — which is precisely the kind of unreachable state this command exists
# to stop creating.
#
# `None` means the row is not on that site's sheet at all.
#
#   code       SCMPILOT03   SCMPILOT04   SCMPILOT05
#              185.50 kW    120.00 kW     95.00 kW
ITEM_SET = [
    ('OPX-001',       326,         211,         167),   # Module   (MANDATORY)
    ('OPX-004',         4,           2,           2),   # DCDB
    ('OPX-020',         1,        None,        None),   # Inverter 100 kW
    ('OPX-019',         1,           1,           1),   # Inverter  75 kW
    ('OPX-016',      None,           1,        None),   # Inverter  40 kW
    ('OPX-012',      None,        None,           1),   # Inverter  25 kW
    ('OPX-027',        18,          12,           9),   # MMS      (MANDATORY)
    ('OPX-028',        12,           8,           6),   # MMS
    ('OPX-030',         4,           3,           2),   # MMS
    ('OPX-046',         1,        None,        None),   # ACDB Type-1 100KW
    ('OPX-043',      None,           1,        None),   # ACDB Type-1 33/36/40KW
    ('OPX-042',      None,        None,           1),   # ACDB Type-1 25/30KW
    ('OPX-056',         1,        None,        None),   # ACDB Type-2 100KW
    ('OPX-053',      None,           1,        None),   # ACDB Type-2 33/36/40KW
    ('OPX-052',      None,        None,           1),   # ACDB Type-2 25/30KW
    ('OPX-059',       900,         580,         460),   # DC Cable red
    ('OPX-060',       915,         590,         470),   # DC Cable black
    ('OPX-061',        44,          28,          22),   # DC Cable MC4 pairs
    ('OPX-062',       140,          90,          72),   # AC Cable 4sq
    ('OPX-069',        68,          44,          35),   # AC Cable 70sq
    ('OPX-101',       652,         422,         334),   # Pin Type Lug
    ('OPX-112',        99,          64,          51),   # Ring Type Lug
    ('OPX-136',       372,         240,         190),   # Conduit 40mm
    ('OPX-148',         6,           4,           3),   # Conduit saddles
    ('OPX-164',       124,          80,          63),   # Cable Tray
    ('OPX-168',       480,         310,         245),   # Earthing strip
    ('OPX-171',        18,          12,           9),   # Earthing electrode
    ('OPX-179',         1,           1,           1),   # Solar Meter
    ('OPX-183',         1,        None,        None),   # CT 200/5A
    ('OPX-182',      None,           1,        None),   # CT 150/5A
    ('OPX-181',      None,        None,           1),   # CT 100/5A
    ('OPX-184',         1,           1,           1),   # Data Logger
    ('OPX-188',        15,          10,           8),   # BOS cable tie
    ('OPX-194',        48,          32,          25),   # BOS fasteners
    ('OPX-197',         2,           2,           1),   # BOS fire extinguisher
    ('OPX-198',         6,           4,           3),   # BOS danger boards
]

#: What a refusal names as "would write". Plain English, because the point of listing the
#: writes is that somebody about to make a mistake reads them.
WRITES = [
    f'1 BOQ row on each of {", ".join(FIXTURE_SITES)} '
    f'(status Draft, submitted_by NULL — the real OPEX shape)',
    f'BOQItem rows from a {len(ITEM_SET)}-code set scaled per site by capacity',
    'nothing else — no BOQRevision, no DesignAssignment, no DesignAttempt, no SiteGroup',
]


# ---------------------------------------------------------------------------
# Local-database interlock. Deliberately duplicated from seed_scm_pilot rather than
# imported, for the reason that command's docstring gives: the two namespaces stay
# independent so a teardown written against one can never reach the other's rows. There
# is NO override flag, for the same reason there is none there.
# ---------------------------------------------------------------------------

def _database_host():
    from django.conf import settings
    return settings.DATABASES['default'].get('HOST') or ''


def _database_name():
    from django.conf import settings
    return settings.DATABASES['default'].get('NAME') or ''


def _database_is_local():
    host = _database_host()
    return host in LOCAL_HOSTS or host.startswith('/')


def untouchable_digest():
    """A sha256 over every field of every row that must not move, for the three sites
    this command is forbidden to touch.

    THE POINT IS THE FIELD DUMP, NOT THE COUNT. A row count proves only that nothing was
    inserted or deleted; it says nothing about an UPDATE, which is the write this command
    is most at risk of making by accident and the one a count would never catch. Hashing
    every column of every row catches all three.

    Deterministic by construction: models in a fixed order, rows ordered by pk, fields in
    `_meta.fields` order, values coerced through `repr` so a Decimal and a float do not
    collide. Taken before the transaction opens and again after it commits; the two must
    be equal or the run has broken its own rules.
    """
    from projects.models import (BOQ, BOQItem, DesignAssignment, DesignAttempt,
                                 ArkaSubmission, SiteGroup, SiteGroupMembership, Task)

    parts = []
    scoped = [
        (DesignAssignment,   'project__project_id__in'),
        (DesignAttempt,      'assignment__project__project_id__in'),
        (ArkaSubmission,     'attempt__assignment__project__project_id__in'),
        (BOQ,                'project__project_id__in'),
        (BOQItem,            'boq__project__project_id__in'),
        (SiteGroupMembership, 'project__project_id__in'),
        (Task,               'phase__project__project_id__in'),
    ]
    for model, lookup in scoped:
        parts.append(f'--- {model.__name__} ---')
        for row in model.objects.filter(**{lookup: UNTOUCHABLE_SITES}).order_by('pk'):
            parts.append('|'.join(f'{f.name}={getattr(row, f.attname)!r}'
                                  for f in row._meta.fields))

    # Every SiteGroup, not just those touching the three sites: group 40 holds 01 and 02,
    # and a new group appearing anywhere would be a write this command has no business
    # making.
    parts.append('--- SiteGroup (all) ---')
    for row in SiteGroup.objects.order_by('pk'):
        parts.append('|'.join(f'{f.name}={getattr(row, f.attname)!r}'
                              for f in row._meta.fields))

    blob = '\n'.join(parts)
    return hashlib.sha256(blob.encode('utf-8')).hexdigest(), len(parts)


class Command(BaseCommand):
    help = ('Fixture BOQ and BOQItem rows for SCMPILOT03/04/05 on a LOCAL database so '
            'the procurement group lock has real quantities to freeze. Additive only; '
            'refuses a non-local database outright. Idempotent.')

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='Print what would be created and create nothing.')
        parser.add_argument('--manifest', type=str, default='',
                            help=f'SCMPILOT manifest to APPEND to. '
                                 f'Default: {DEFAULT_MANIFEST_PATH}')

    # ================================================================== handle
    def handle(self, *args, **options):
        from projects.models import (ActivityLog, BOQ, BOQItem, BOQItemMaster,
                                     NotificationLog, Project)
        from projects.permissions import project_boq_is_group_locked

        # Always the first line of output, before anything else is decided. Flushed,
        # because stdout is block-buffered when redirected while stderr is not.
        self.stdout.write(f'[db] host={_database_host() or "(none - local socket)"} '
                          f'name={_database_name()}')
        try:
            self.stdout.flush()
        except (AttributeError, ValueError):
            pass

        if not _database_is_local():
            writes = '\n'.join(f'                  {line}' for line in WRITES)
            self.stderr.write(
                f'REFUSING TO RUN: this database is not local.\n'
                f'  database host : {_database_host()}\n'
                f'  database name : {_database_name()}\n'
                f'  would write   :\n{writes}\n'
                f'\n'
                f'Pilot data must never reach production. There is no override flag on '
                f'this command, by decision.\n')
            raise SystemExit(1)

        self.manifest_path = Path(options['manifest'].strip() or DEFAULT_MANIFEST_PATH)

        # ---- resolve the catalogue up front, so a missing code fails before any write
        codes = [row[0] for row in ITEM_SET]
        masters = {m.code: m for m in BOQItemMaster.objects.filter(
            code__in=codes, project_type='OPEX', is_active=True)}
        missing = [c for c in codes if c not in masters]
        if missing:
            raise CommandError(
                f'These catalogue codes are missing, inactive, or not OPEX: '
                f'{", ".join(missing)}. The item set is written against the 207-row OPEX '
                f'catalogue; fix the catalogue or the set, do not skip the rows.')

        # ---- resolve the sites, and refuse any that must not be written
        sites = {}
        for code in FIXTURE_SITES:
            project = Project.objects.filter(project_id=code, is_deleted=False).first()
            if project is None:
                raise CommandError(f'{code} does not exist (or is deleted). Run '
                                   f'seed_scm_pilot first.')
            if project.project_type != 'OPEX':
                raise CommandError(f'{code} is {project.project_type}, not OPEX.')
            # THE ONE STATE THIS COMMAND MUST NOT WRITE INTO. A locked group means a
            # purchase order was raised against this site's quantities; inserting rows
            # underneath that lock would put numbers into an aggregate that was committed
            # before they existed. There is no unlock, so there would be no way back.
            if project_boq_is_group_locked(project):
                raise CommandError(
                    f'{code} is in a LOCKED procurement group — its BOQ is frozen and '
                    f'this command will not write underneath a committed purchase.')
            sites[code] = project

        if options['dry_run']:
            self.stdout.write(self.style.WARNING('DRY RUN — nothing will be created.'))
            for line in WRITES:
                self.stdout.write(f'  WOULD CREATE  {line}')
            self.stdout.write('')
            for idx, code in enumerate(FIXTURE_SITES):
                planned = [r[0] for r in ITEM_SET if r[idx + 1] is not None]
                self.stdout.write(f'  {code} ({sites[code].capacity_kw} kW): '
                                  f'{len(planned)} BOQItem rows')
            self.stdout.write('')
            self.stdout.write('  WOULD NOT TOUCH: ' + ', '.join(UNTOUCHABLE_SITES))
            self.stdout.write(f'  Manifest would be APPENDED at: {self.manifest_path}')
            return

        # ---- the three "prove it" baselines, taken before anything opens
        digest_before, digest_rows = untouchable_digest()
        notif_before = NotificationLog.objects.count()
        activity_before = ActivityLog.objects.count()

        self.created = {}
        self.reused = {}
        self.rows = {}          # model label -> [{pk, label, created}]
        self.site_rows = {}     # site code -> summary for the manifest

        with transaction.atomic():
            for idx, code in enumerate(FIXTURE_SITES):
                self._fixture_site(sites[code], idx, masters)

            # GUARDS, NOT DECORATION. All three abort inside the transaction, so a
            # violation rolls back rather than being reported after the fact.
            #
            # The local .env points at PRODUCTION Interakt and ZeptoMail (DEFERRED §6),
            # and only two booleans stand between a notification and a real phone.
            notif_after = NotificationLog.objects.count()
            if notif_after != notif_before:
                raise CommandError(
                    f'NotificationLog grew from {notif_before} to {notif_after} — '
                    f'aborting and rolling back. Creating a BOQ must notify nobody.')

            # A BOQ write reaching log_activity() would mean it went through a view path,
            # which would mean it also went through a status write. It must not.
            activity_after = ActivityLog.objects.count()
            if activity_after != activity_before:
                raise CommandError(
                    f'ActivityLog grew from {activity_before} to {activity_after} — '
                    f'aborting and rolling back. Direct model writes log nothing.')

            digest_mid, _ = untouchable_digest()
            if digest_mid != digest_before:
                raise CommandError(
                    f'The untouchable digest changed during the run — aborting and '
                    f'rolling back.\n  before: {digest_before}\n  after:  {digest_mid}\n'
                    f'  {", ".join(UNTOUCHABLE_SITES)} must not move.')

        digest_after, _ = untouchable_digest()

        self._append_manifest(digest_after)
        self._report(digest_before, digest_after, digest_rows,
                     notif_before, activity_before)

    # ------------------------------------------------------------------ writes
    def _fixture_site(self, project, idx, masters):
        """One site: its BOQ header, then its item rows. `idx` selects the quantity
        column — 0 for SCMPILOT03, 1 for 04, 2 for 05.

        EVERY WRITE IS get_or_create, AND THE DEFAULTS ARE ONLY DEFAULTS. A second run
        finds every row and updates none — which is what makes "idempotent" a claim the
        second run can prove rather than a thing this docstring asserts. In particular a
        quantity somebody has since corrected by hand is NOT rewritten back to the table
        value: the table is what the rows are born with, not what they must remain.
        """
        from projects.models import BOQ, BOQItem

        boq, created = BOQ.objects.get_or_create(project=project)
        self._record('BOQ', boq, project.project_id, created)

        item_count = created_items = 0
        for code, *quantities in ITEM_SET:
            quantity = quantities[idx]
            if quantity is None:
                continue        # this row is not on this site's sheet
            master = masters[code]
            item_count += 1
            # THE UNIQUENESS KEY IS (boq, item_master), which is the same key the picker
            # reconciles on — `by_master = {row.item_master_id: row for row in ...}`. Any
            # other key here would let a second run add a duplicate line the picker would
            # then be unable to address.
            #
            # serial_no / category / description / uom ALL COME FROM THE MASTER, exactly
            # as opex_boq_entry's create branch sets them. description is a point-in-time
            # snapshot by design; the other three are re-derivable and still copied,
            # because a row that disagrees with the picker about its own serial number
            # would sort into a different place on the two screens that render it.
            item, item_created = BOQItem.objects.get_or_create(
                boq=boq, item_master=master,
                defaults={
                    'serial_no':        master.sort_order,
                    'category':         master.category,
                    'description':      master.description,
                    'uom':              master.unit,
                    'boq_quantity':     quantity,
                    'is_standard_item': True,
                },
            )
            created_items += int(item_created)
            self._record('BOQItem', item, f'{project.project_id} {code}', item_created)

        self.site_rows[project.project_id] = {
            'boq_pk':          boq.pk,
            'boq_created':     created,
            'capacity_kw':     str(project.capacity_kw),
            'item_rows':       item_count,
            'item_rows_made':  created_items,
            'status':          boq.status,
            'submitted_by':    boq.submitted_by_id,
            'provenance':      PROV_FIXTURED,
            'note':            ('BOQ and every line written directly by '
                                'fixture_scmpilot_boq — no designer authored this'),
        }

    def _record(self, label, obj, name, was_created):
        """Tally one row and remember it for the manifest."""
        bucket = self.created if was_created else self.reused
        bucket[label] = bucket.get(label, 0) + 1
        self.rows.setdefault(label, []).append(
            {'pk': obj.pk, 'label': name, 'created': was_created})
        return obj

    # ---------------------------------------------------------------- manifest
    def _append_manifest(self, digest):
        """APPEND to the SCMPILOT manifest. Never overwrite it.

        `seed_scm_pilot` owns this file and its `runs` list exists precisely so a second
        writer does not erase the record of the first. This command adds one entry to
        `runs`, merges its rows into `rows` under its own keys, and adds a `boq_fixture`
        section. Every other key the seed wrote is carried through untouched — including
        `command`, which stays 'seed_scm_pilot' because that is what created the pilot
        data. A missing manifest is not an error: the rows exist either way, and refusing
        to record them because the seed's file is gone would lose the more useful half.
        """
        payload = {}
        if self.manifest_path.exists():
            try:
                payload = json.loads(self.manifest_path.read_text(encoding='utf-8'))
            except (ValueError, OSError):
                # A corrupt manifest must not abort a run that already committed. Say so
                # in the file rather than silently starting a fresh one.
                payload = {'recovered_from_unreadable_manifest': True}

        now = timezone.now().isoformat()
        payload.setdefault('runs', []).append({
            'at':      now,
            'command': 'fixture_scmpilot_boq',
            'created': self.created,
            'reused':  self.reused,
        })
        rows = payload.setdefault('rows', {})
        for label, entries in self.rows.items():
            rows.setdefault(label, []).extend(entries)

        payload['boq_fixture'] = {
            'written_at':  now,
            'command':     'fixture_scmpilot_boq',
            'provenance':  PROV_FIXTURED,
            # The sentence this whole section exists to carry.
            'statement':   ('These BOQs were NEVER AUTHORED BY A DESIGNER. Every BOQ and '
                            'BOQItem row on SCMPILOT03, 04 and 05 was written directly by '
                            'fixture_scmpilot_boq, because seed_scm_pilot released those '
                            'sites without creating a BOQ and no reopen path exists on a '
                            'released assignment that does not also require a fabricated '
                            'CAD archive in the live production storage bucket. The '
                            'quantities are scaled from MB0141, a real production sheet. '
                            'They are plausible; they are not real design output, and no '
                            'engineering decision should be read out of them.'),
            'sites':       self.site_rows,
            'untouched':   {'sites': list(UNTOUCHABLE_SITES), 'digest_sha256': digest},
            'not_created': ['BOQRevision', 'DesignAssignment', 'DesignAttempt',
                            'ArkaSubmission', 'SiteGroup', 'SiteGroupMembership',
                            'ActivityLog', 'NotificationLog', 'StatusTransition'],
        }

        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        self.manifest_path.write_text(json.dumps(payload, indent=2), encoding='utf-8')

    # ------------------------------------------------------------------ report
    def _report(self, digest_before, digest_after, digest_rows,
                notif_before, activity_before):
        from projects.models import ActivityLog, NotificationLog

        w = self.stdout.write
        w('')
        w('=' * 78)
        w('SCMPILOT BOQ FIXTURE — MANIFEST')
        w('=' * 78)
        w('')
        w('  EVERY ROW BELOW IS **FIXTURED**. No designer authored any of it.')
        w('  seed_scm_pilot released 03/04/05 without creating a BOQ; the real')
        w('  authoring route is closed on a released assignment. Quantities are')
        w('  scaled from MB0141, a genuine production sheet — plausible, not real.')
        w('')

        for code, facts in self.site_rows.items():
            verb = 'created' if facts['boq_created'] else 'already present'
            w(f'  {code}  ({facts["capacity_kw"]} kW)')
            w(f'      BOQ pk={facts["boq_pk"]}  status={facts["status"]}  '
              f'submitted_by={facts["submitted_by"]}   [{verb}]   FIXTURED')
            w(f'      BOQItem rows: {facts["item_rows"]} on the sheet, '
              f'{facts["item_rows_made"]} created this run       FIXTURED')
        w('')

        made = sum(self.created.values())
        kept = sum(self.reused.values())
        w(f'  created this run : {self.created or "{}"}   (total {made})')
        w(f'  already present  : {self.reused or "{}"}   (total {kept})')
        w('')
        w('  NOT CREATED, deliberately:')
        w('      BOQRevision            the group lock snapshots nothing — it stamps the')
        w('                             SiteGroup and enforces a live predicate')
        w('      DesignAssignment       untouched, all six sites')
        w('      DesignAttempt          untouched, all six sites')
        w('      ArkaSubmission         untouched, all six sites')
        w('      SiteGroup / membership untouched — SCM forms the group by hand')
        w('      ActivityLog            direct model writes log nothing')
        w('      NotificationLog        creating a BOQ notifies nobody')
        w('')
        w(f'  UNTOUCHED, PROVEN BY DIGEST ({digest_rows} row-lines hashed):')
        w(f'      sites  : {", ".join(UNTOUCHABLE_SITES)}')
        w(f'      before : {digest_before}')
        w(f'      after  : {digest_after}')
        w(f'      equal  : {digest_before == digest_after}')
        w('')
        w(f'  NotificationLog : {notif_before} -> {NotificationLog.objects.count()}')
        w(f'  ActivityLog     : {activity_before} -> {ActivityLog.objects.count()}')
        w('')
        w(f'  Manifest APPENDED at {self.manifest_path}')
        w('=' * 78)
