"""
Management command: demo data for the O3 GROUP ORDER raise, on a LOCAL database.

    python manage.py seed_order_demo --dry-run        # plan only, writes nothing
    python manage.py seed_order_demo                  # create
    python manage.py teardown_order_demo --confirm    # remove exactly what was created

TYPE BOTH COMMAND NAMES IN FULL. `se`+Tab does not disambiguate this command from
`send_eod_digest`, which mails the whole company, nor from the three other seeds. The
runbook already records that collision; it is repeated here because this is where
somebody will be typing fast.

WHAT THIS IS FOR
----------------
`vendor_order_create_group` (O3) asks SCM to size ONE order against sites drawn from
several procurement groups and several tenders. Nothing on this database lets that
page be exercised: the aggregate needs released sites with linked BOQ rows, in
procurement groups, under more than one tender, and there is one OPEX tender here
with a hand-walked pilot on it. This builds that stage.

It stops where the demonstration starts. NO VendorOrder AND NO PaymentRequest ARE
CREATED — those are what the operator raises by hand through the UI, and seeding one
would leave nothing to show. `teardown_order_demo` refuses to run if an order or a
payment has since come to reference any of this, so an order the operator raised can
never be deleted by the tool that cleans up around it.

THIS IS LOCAL-ONLY BY DECISION, NOT BY CONVENTION
-------------------------------------------------
Demo data must never reach production. It would pollute the CEO dashboard, the EOD
digest and every execution counter, and it teaches users that the system is a toy.
The interlock is `_demo_support.require_local_database()`, IMPORTED rather than
reimplemented, and it is the same one `seed_opex_test_data` uses:

    LOCAL_HOSTS = {'', 'localhost', '127.0.0.1', '::1'}
    def database_is_local():
        host = database_host()
        return host in LOCAL_HOSTS or host.startswith('/')

— the empty string being what dj_database_url leaves behind for a socket connection,
and a leading '/' an explicit socket path. `seed_scm_pilot` duplicates those twelve
lines rather than importing them, deliberately, because it keeps NO ties at all to
the demo tooling. This command has no such reason: it is demo tooling, it wants the
same refusal text and the same `--i-know-this-is-not-local` door, and a second copy
of an interlock is a second thing to get wrong.

The `ORDDEMO` namespace is the SECOND line of defence, not the first — everything
written here carries it somewhere a human will see, so anything that ever escapes is
identifiable by eye in a list, an export or a dashboard.

THE MANIFEST IS SHARED MACHINERY; THE FILE IS NOT
--------------------------------------------------
`Manifest` comes from `_demo_support` — the seed writes down every primary key it
creates and `teardown_order_demo` deletes that list and nothing else, so a row this
command did not create cannot be selected by the teardown.

THE PATH IS ITS OWN (`~/.horizon-pms-orderdemo/`), AND THAT MATTERS. Writing these
rows into the DEMO manifest at `~/.horizon-pms-demo/demo_manifest.json` would mean
`teardown_opex_test_data`, run months from now for an unrelated reason, silently
takes the order demo down with it — and nobody would connect the two. That is the
coupling `seed_scm_pilot`'s docstring warns about, arriving from the other direction.
The machinery is shared; the lifetimes are not.

IDEMPOTENCY: IT REFUSES TO DOUBLE-SEED, WHICH IS `seed_opex_test_data`'s ANSWER
-------------------------------------------------------------------------------
A second run would collide on username, site_code and short_tender_code anyway.
Refusing up front says so in words instead of as an IntegrityError two hundred rows
in. `seed_scm_pilot` is get_or_create-idempotent instead, and that is right for a
command with NO teardown — here a merge would have to reconcile two manifests, and a
half-recorded row is exactly what makes a teardown unsafe.

THREE USERS, WHICH THE BRIEF DOES NOT NAME
-------------------------------------------
Stated plainly because they are an addition: the inventory asked for is tenders,
sites, BOQs, groups and vendors. Those rows need an author — `create_opex_site()`
takes a creator, a released DesignAssignment records who released it, and a locked
SiteGroup records who locked it — and this command will NOT borrow a real account
from the dump for that, for the same reason `seed_scm_pilot` refuses to: it would
write a real employee's name onto demo data, and their password is unknown anyway, so
it would not even let the operator log in. Three is the minimum that keeps the
attribution honest: a PM who owns the tenders and their sites, a Designer who
released them, and an SCM who made the groups AND IS WHO THE OPERATOR LOGS IN AS —
SCM is the only role `user_can_raise_group_order()` accepts, and SCM is
portfolio-wide under `user_can_view_project()`, which the raise view checks per
posted site.

THE SITES ARE LEFT IN Draft, AND ARE STILL RELEASED
----------------------------------------------------
Design release is a DesignAssignment state; it does not require the site to have
started execution, and `_add_sites()` asks only for `status == DESIGN_RELEASED`.
Activating all seven would attach 7 phases / 23 tasks / 8 mirrors each — some 1,600
rows through the manifest and out again — for a screen that never reads one of them.
`seed_opex_test_data` does the same thing with its own released site.

EVERYTHING GOES THROUGH A REAL CODE PATH WHERE ONE EXISTS
---------------------------------------------------------
Users through `UserCreateForm`, vendors through `VendorForm`, programs through
`ProgramForm`, sites through `views.create_opex_site()`, group membership through
`design_views._add_sites()`.

Four things have no callable path and are created with `objects.create()`, each
marked `# NO PRODUCT PATH` at its call site:

  1. The released DesignAssignment / DesignAttempt — every design transition lives
     inside a view. The field-set is `seed_opex_test_data`'s, unchanged, so the two
     commands cannot describe "released" differently.
  2. The BOQ and its rows — `opex_boq_entry` is request-bound. The field mapping
     below (serial_no from the catalogue's sort_order, category/description/uom from
     the MASTER) is that view's persistence block, copied.
  3. The two item_master=NULL rows — `boq_detail`'s `add_item` branch, copied:
     max(serial_no)+1, no master, `is_standard_item=False`.
  4. The group lock — `site_group_lock` is a view. Its three field writes and its
     one-log-line-per-member-site are replicated exactly.

NOTIFICATIONS
-------------
Nothing here should send one — no view runs and no assignment chokepoint is called.
As a guard rather than an assumption, the NotificationLog row count is captured
before and compared after; any change aborts the whole transaction.
"""
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from django.apps import apps
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from projects.management.commands._demo_support import (
    Manifest, OVERRIDE_FLAG, database_host, database_name, high_water,
    print_db_banner, require_local_database,
)

# ---------------------------------------------------------------------------
# Identity of the order-demo data.
#
# NO HYPHEN IN THE PREFIX, AND THAT IS FORCED BY THE PRODUCT. An OPEX site's
# project_id IS its site_code, and OpexSiteForm.clean_site_code() runs the entered
# value through normalize_program_code(), which strips everything outside [A-Z0-9].
# 'ORDDEMO-A-01' would be STORED as 'ORDDEMOA01' regardless, so the stripped form is
# chosen up front and the seeded ID is the ID the real path produces.
#
# 'ORDDEMO' DOES NOT START WITH 'DEMO', which is load-bearing rather than incidental:
# it is what keeps this data out of `seed_opex_test_data`'s own double-seed guard
# (`project_id__startswith=DEMO_PREFIX`) and out of any eye-scan for DEMO rows.
# ---------------------------------------------------------------------------
ORDER_DEMO_PREFIX = 'ORDDEMO'

#: RFC 6761 reserves `.invalid` as permanently undeliverable, so a misconfigured
#: notification run cannot reach a real inbox.
EMAIL_DOMAIN = 'orderdemo.invalid'

#: Long enough for UserCreateForm's min_length=8. Printed at the end of a seed run.
PASSWORD = 'OrderDemo!2026'

#: DELIBERATELY OUTSIDE THE REPOSITORY — a file inside it would be one `git add -A`
#: away from being committed. And deliberately NOT ~/.horizon-pms-demo/: see the
#: module docstring on why the machinery is shared and the lifetimes are not.
DEFAULT_MANIFEST_PATH = (
    Path.home() / '.horizon-pms-orderdemo' / 'order_demo_manifest.json')

#: username, first, last, role, phone. Three, and the docstring says why each exists.
#: No Admin: UserCreateForm.clean() refuses a second one and this database has one.
DEMO_USERS = [
    ('orddemo.pm',     'Order', 'Pm',     'PM',     '9700000001'),
    ('orddemo.scm',    'Order', 'Scm',    'SCM',    '9700000002'),
    ('orddemo.design', 'Order', 'Design', 'Design', '9700000003'),
]
PM_USERNAME     = 'orddemo.pm'
SCM_USERNAME    = 'orddemo.scm'
DESIGN_USERNAME = 'orddemo.design'

#: key, short_tender_code, name, client_name. TWO TENDERS IS THE POINT OF THE FIXTURE
#: — the raise page offers every procurement group in the system regardless of the
#: tender it was entered from, and one tender cannot show that.
TENDERS = [
    ('A', 'ORDDEMOA', f'{ORDER_DEMO_PREFIX} Tender A',
     f'{ORDER_DEMO_PREFIX} Client A (Local Only)'),
    ('B', 'ORDDEMOB', f'{ORDER_DEMO_PREFIX} Tender B',
     f'{ORDER_DEMO_PREFIX} Client B (Local Only)'),
]

#: Catalogue rows every site carries. Six, and the two OPEX mandatory items are among
#: them — asserted in _check_catalogue() rather than assumed, because "mandatory" is a
#: FLAG on BOQItemMaster and the flagged set can change under this file.
CORE_ITEM_CODES = ['OPX-001', 'OPX-027', 'OPX-059', 'OPX-168', 'OPX-178', 'OPX-188']

#: site_code -> (tender key, city, state, dc_capacity_kw, quantity multiplier,
#:               extra catalogue codes on top of CORE_ITEM_CODES).
#:
#: THE MULTIPLIER IS WHAT MAKES THE AGGREGATE READABLE. Every site's quantity for an
#: item is BASE_QUANTITY[code] * multiplier, and the multipliers are 1..7, all
#: different — so a consolidated total is visibly a sum of unequal parts rather than a
#: number that could equally be one site's figure copied.
#:
#: THE EXTRAS OVERLAP PARTLY, ON PURPOSE. Sites in one group share some non-core items
#: and not others, so `item_count` differs between the three groups and differs again
#: when two groups are combined — which is the only way to tell a real union from a
#: template being echoed back.
SITES = {
    'ORDDEMOA01': ('A', 'Gurugram',   'Haryana',     Decimal('120.00'), 1,
                   ['OPX-002', 'OPX-008', 'OPX-039']),
    'ORDDEMOA02': ('A', 'Faridabad',  'Haryana',     Decimal('180.00'), 2,
                   ['OPX-002', 'OPX-009', 'OPX-040', 'OPX-130', 'OPX-163']),
    'ORDDEMOA03': ('A', 'Karnal',     'Haryana',     Decimal('240.00'), 3,
                   ['OPX-003', 'OPX-008', 'OPX-039', 'OPX-130']),
    'ORDDEMOA04': ('A', 'Hisar',      'Haryana',     Decimal('300.00'), 4,
                   ['OPX-003', 'OPX-011', 'OPX-041', 'OPX-130', 'OPX-163', 'OPX-169']),
    'ORDDEMOB01': ('B', 'Nashik',     'Maharashtra', Decimal('150.00'), 5,
                   ['OPX-002', 'OPX-012', 'OPX-042']),
    'ORDDEMOB02': ('B', 'Aurangabad', 'Maharashtra', Decimal('210.00'), 6,
                   ['OPX-004', 'OPX-012', 'OPX-042', 'OPX-164']),
    'ORDDEMOB03': ('B', 'Solapur',    'Maharashtra', Decimal('165.00'), 7,
                   ['OPX-004', 'OPX-013', 'OPX-043', 'OPX-164', 'OPX-131', 'OPX-169']),
}

#: One site's quantity for an item before the multiplier. Shaped like a real sheet —
#: modules and cable in the hundreds, one inverter and one ACDB per site.
BASE_QUANTITY = {
    'OPX-001': Decimal('60'),   'OPX-027': Decimal('12'),  'OPX-059': Decimal('300'),
    'OPX-168': Decimal('4'),    'OPX-178': Decimal('1'),   'OPX-188': Decimal('2'),
    'OPX-002': Decimal('1'),    'OPX-003': Decimal('1'),   'OPX-004': Decimal('1'),
    'OPX-008': Decimal('1'),    'OPX-009': Decimal('1'),   'OPX-011': Decimal('1'),
    'OPX-012': Decimal('1'),    'OPX-013': Decimal('1'),
    'OPX-039': Decimal('1'),    'OPX-040': Decimal('1'),   'OPX-041': Decimal('1'),
    'OPX-042': Decimal('1'),    'OPX-043': Decimal('1'),
    'OPX-130': Decimal('120'),  'OPX-131': Decimal('90'),
    'OPX-163': Decimal('40'),   'OPX-164': Decimal('30'),
    'OPX-169': Decimal('6'),
}

#: The one site that carries BOQ rows with NO `item_master`.
#:
#: IT IS IN A LOCKED GROUP DELIBERATELY. aggregate_group_boq() cannot sum a row with a
#: null master, so it returns those rows in `unlinked` for the raise page to shout
#: about in its "Not orderable" block — and a block that is empty on every group is a
#: block nobody has ever seen render. Putting them on an ungrouped site would leave it
#: empty everywhere that matters.
UNLINKED_SITE_CODE = 'ORDDEMOA02'

#: category, description, uom, quantity. `category` MUST come from
#: BOQItem.CATEGORY_CHOICES ('Other'), not the OPEX catalogue's own vocabulary — these
#: rows have no master to take a category from, which is the whole point of them.
#:
#: EVERY QUANTITY IS > 0, AND THAT IS LOAD-BEARING: aggregate_group_boq() selects
#: `unlinked` with `boq_quantity__gt=0`, the same guard it sums with. A null or zero
#: quantity here would produce two rows that are invisible on the one screen they
#: exist to populate.
UNLINKED_ROWS = [
    ('Other', f'{ORDER_DEMO_PREFIX} ad-hoc: cable trenching across the access road',
     'LS', Decimal('1')),
    ('Other', f'{ORDER_DEMO_PREFIX} ad-hoc: crane hire for rooftop module lift',
     'Nos', Decimal('2')),
]

#: key, tender key, name, member site codes, locked?
#:
#: SIX OF THE SEVEN SITES ARE GROUPED. ORDDEMOB03 is left out on purpose: it is
#: released and in no procurement group, which is the definition of post_qc_pool(), so
#: the raise page's pool block has a site in it too.
SITE_GROUPS = [
    ('a_locked', 'A', f'{ORDER_DEMO_PREFIX} A — Batch 1 (locked)',
     ['ORDDEMOA01', 'ORDDEMOA02'], True),
    ('a_draft',  'A', f'{ORDER_DEMO_PREFIX} A — Batch 2 (draft)',
     ['ORDDEMOA03', 'ORDDEMOA04'], False),
    ('b_locked', 'B', f'{ORDER_DEMO_PREFIX} B — Batch 1 (locked)',
     ['ORDDEMOB01', 'ORDDEMOB02'], True),
]

#: name, contact person, phone, category names. VendorForm requires at least one
#: category, so the names are resolved against VendorCategory and a missing one is a
#: refusal that names what the database does have.
VENDORS = [
    (f'{ORDER_DEMO_PREFIX} Cable & BOS Supplies', 'Order Demo Sales Three',
     '9700000013', ['BOS', 'Other']),
    (f'{ORDER_DEMO_PREFIX} Inverter & Switchgear Supplies', 'Order Demo Sales Two',
     '9700000012', ['Inverter', 'BOS']),
    (f'{ORDER_DEMO_PREFIX} Modules & Structure Supplies', 'Order Demo Sales One',
     '9700000011', ['Solar Modules', 'Structure']),
]

#: What the interlock names as "would write" on a non-local host. Plain English,
#: because the point of listing the writes is that somebody about to make a mistake
#: reads them.
WRITES = [
    f'{len(DEMO_USERS)} users (@{EMAIL_DOMAIN}) with a shared known password',
    f'{len(VENDORS)} active Vendors',
    f'{len(TENDERS)} OPEX Programs carrying {len(SITES)} sites, all left in Draft',
    f'{len(SITES)} released DesignAssignments (+ one DesignAttempt each)',
    f'{len(SITES)} OPEX BOQs, 8-12 catalogue rows each, plus '
    f'{len(UNLINKED_ROWS)} unlinked rows on {UNLINKED_SITE_CODE}',
    f'{len(SITE_GROUPS)} procurement SiteGroups and their memberships',
    'the ActivityLog and StatusTransition rows those paths write on the way past',
]

#: Said out loud in the plan and in the report, because it is the whole shape of the
#: fixture: the demonstration is what the operator does next.
NOT_CREATED = ['VendorOrder', 'VendorOrderSite', 'VendorOrderLine', 'PaymentRequest']


class Command(BaseCommand):
    help = ('Build the LOCAL demo data for the O3 group vendor-order raise: two OPEX '
            'tenders, seven released sites with BOQs, three procurement groups and '
            'three vendors. Creates no order and no payment. Refuses a non-local '
            'database. Writes a manifest that teardown_order_demo reads.')

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='Print what would be created and create nothing.')
        parser.add_argument('--manifest', type=str, default='',
                            help=f'Where to write the creation manifest. '
                                 f'Default: {DEFAULT_MANIFEST_PATH}')
        parser.add_argument(OVERRIDE_FLAG, action='store_true',
                            help='Required to run against a non-local database.')

    # ------------------------------------------------------------------ handle
    def handle(self, *args, **options):
        from projects.models import NotificationLog, Program, Project

        # Always the first line of output, before anything else is decided.
        print_db_banner(self)
        require_local_database(
            self, options.get('i_know_this_is_not_local', False), WRITES)

        manifest_path = options['manifest'].strip() or DEFAULT_MANIFEST_PATH

        # Catalogue sanity BEFORE the plan is printed, so a dry run reports it too. The
        # fixture names two dozen catalogue codes and assumes the OPEX mandatory items
        # are among the six every site carries; both are facts about BOQItemMaster rows
        # this command does not create and cannot repair.
        self._check_catalogue()

        if options['dry_run']:
            self._print_plan(manifest_path)
            return

        # Refuse to double-seed — see the module docstring. Checked in the three places
        # a second run would collide, so the message names what is actually there.
        clashes = []
        if User.objects.filter(username__startswith='orddemo.').exists():
            clashes.append('orddemo.* user accounts')
        if Program.objects.filter(
                short_tender_code__in=[code for _k, code, _n, _c in TENDERS]).exists():
            clashes.append('ORDDEMO tender codes')
        if Project.objects.filter(project_id__startswith=ORDER_DEMO_PREFIX).exists():
            clashes.append(f'{ORDER_DEMO_PREFIX}-prefixed sites')
        if clashes:
            self.stdout.write(self.style.WARNING(
                'Order-demo data already present — nothing created:'))
            for line in clashes:
                self.stdout.write(f'  {line}')
            self.stdout.write(
                'Run  python manage.py teardown_order_demo --confirm  first.')
            return

        manifest = Manifest(manifest_path)

        # Every model's pk high-water mark, taken before a single write. The leak sweep
        # at the end uses these to catch rows created as a SIDE EFFECT of a real code
        # path — the ActivityLog and StatusTransition rows nobody thinks to record, and
        # therefore exactly the rows that leak.
        watched = self._watched_models()
        marks   = {m: high_water(m) for m in watched}

        notif_before = NotificationLog.objects.count()

        with transaction.atomic():
            users    = self._create_users(manifest)
            vendors  = self._create_vendors(manifest, users)
            programs = self._create_programs(manifest, users)
            sites    = self._create_sites(manifest, programs, users)
            self._release_designs(manifest, sites, users)
            boqs     = self._create_boqs(manifest, sites)
            self._create_unlinked_rows(manifest, boqs)
            groups   = self._create_groups(manifest, programs, sites, users)

            # ---- leak sweep -------------------------------------------------
            # Anything new that was not explicitly recorded above gets recorded now, and
            # lands at the END of the manifest — so the teardown, which walks it in
            # reverse, deletes these FIRST. That is the safe order: side-effect rows are
            # leaves, and deleting a leaf before its parent never trips a PROTECT.
            swept = self._sweep(manifest, marks)

            # Guard, not decoration: if anything unexpectedly emitted a notification,
            # abort so no real user is left having been messaged about demo data.
            notif_after = NotificationLog.objects.count()
            if notif_after != notif_before:
                raise CommandError(
                    f'NotificationLog grew from {notif_before} to {notif_after} during '
                    f'seeding — aborting and rolling back.')

        written = manifest.save(database_host(), database_name())
        self._report(manifest, users, vendors, programs, sites, groups, swept,
                     written, notif_before)

    # -------------------------------------------------------------- preflight
    def _check_catalogue(self):
        """Refuse early if the OPEX catalogue cannot back the fixture.

        Three separate facts, all about rows this command does NOT create and cannot
        repair:

          * every code it names still exists and is ACTIVE — a deactivated master is
            not offered by the picker, so a BOQ row pointing at one is a row the
            product could not have made;
          * the mandatory set is a SUBSET of CORE_ITEM_CODES. `is_mandatory` is a flag
            on BOQItemMaster and somebody can raise a third one tomorrow. Silently
            seeding sites that miss it would make every demo BOQ one the real
            completion guard (`design_boq_complete`) would have refused;
          * the per-site row count is 8-12, which this file claims in its own docstring
            and which an edit to SITES is exactly what would quietly break.
        """
        from projects.models import get_opex_boq_catalogue, get_opex_mandatory_items

        wanted = set(CORE_ITEM_CODES)
        for _t, _c, _s, _k, _m, extras in SITES.values():
            wanted.update(extras)

        active  = {m.code for m in get_opex_boq_catalogue()}
        missing = sorted(wanted - active)
        if missing:
            raise CommandError(
                f'These OPEX catalogue codes are missing or inactive, so the fixture '
                f'cannot be built: {", ".join(missing)}. Seed the Part 11 catalogue '
                f'first.')

        mandatory = {m.code for m in get_opex_mandatory_items()}
        unseeded  = sorted(mandatory - set(CORE_ITEM_CODES))
        if unseeded:
            raise CommandError(
                f'The OPEX catalogue now marks {", ".join(unseeded)} mandatory, and '
                f'CORE_ITEM_CODES does not carry it — every seeded BOQ would be one '
                f'design_boq_complete() would refuse. Add it to CORE_ITEM_CODES (and '
                f'to BASE_QUANTITY) in this file.')

        for site_code, (_t, _c, _s, _k, _m, extras) in sorted(SITES.items()):
            codes = set(CORE_ITEM_CODES) | set(extras)
            if not 8 <= len(codes) <= 12:
                raise CommandError(
                    f'{site_code} would get {len(codes)} catalogue rows; the fixture is '
                    f'specified as 8-12 per site. Fix SITES in this file.')
            for code in sorted(codes):
                if code not in BASE_QUANTITY:
                    raise CommandError(
                        f'{site_code} names {code}, which has no BASE_QUANTITY entry '
                        f'in this file.')

    # ------------------------------------------------------------------- plan
    def _print_plan(self, manifest_path):
        self.stdout.write(self.style.WARNING('DRY RUN — nothing will be created.'))
        for line in WRITES:
            self.stdout.write(f'  WOULD CREATE  {line}')
        self.stdout.write('')
        self.stdout.write('  WOULD NOT CREATE (deliberately — these are the '
                          'demonstration):')
        for name in NOT_CREATED:
            self.stdout.write(f'      {name}')
        self.stdout.write('')
        for site_code, (key, _c, _s, _k, multiplier, extras) in sorted(SITES.items()):
            rows  = len(set(CORE_ITEM_CODES) | set(extras))
            extra = (f' + {len(UNLINKED_ROWS)} unlinked'
                     if site_code == UNLINKED_SITE_CODE else '')
            self.stdout.write(f'      Tender {key}  {site_code:<12} '
                              f'{rows} catalogue rows{extra}, quantities x{multiplier}')
        self.stdout.write('')
        self.stdout.write(f'  Manifest would be written to: {manifest_path}')
        self.stdout.write(f'  Password would be:            {PASSWORD}')

    # ------------------------------------------------------------------ users
    def _create_users(self, manifest):
        """Three users, through `UserCreateForm` plus the exact steps `views.user_create`
        performs after it validates.

        THE FORM IS RUN FOR ITS VALIDATION, NOT FOR CONVENIENCE. It is what enforces the
        username charset, the 10-digit phone starting 6-9, the 8-character password
        minimum and the single-Admin rule. A demo account the real form would have
        refused is not a demo account.
        """
        from projects.forms import UserCreateForm

        created = {}
        for username, first, last, role, phone in DEMO_USERS:
            form = UserCreateForm({
                'first_name': first, 'last_name': last, 'username': username,
                'email': f'{username}@{EMAIL_DOMAIN}', 'password': PASSWORD,
                'role': role, 'phone_number': phone, 'is_active': True,
            })
            if not form.is_valid():
                raise CommandError(
                    f'UserCreateForm refused the demo {role} account {username!r}: '
                    f'{form.errors.as_json()}')
            cd = form.cleaned_data

            user = User.objects.create_user(
                username=cd['username'], password=cd['password'],
                first_name=cd['first_name'], last_name=cd['last_name'],
                email=cd['email'], is_active=cd['is_active'],
                is_staff=False,     # three roles, no Admin — see the module docstring
            )
            manifest.add(user)

            profile = user.profile          # auto-created by the post_save signal
            profile.role         = cd['role']
            profile.phone_number = cd['phone_number']
            profile.is_active    = cd['is_active']
            profile.save()
            manifest.add(profile)
            created[username] = profile
        return created

    # ---------------------------------------------------------------- vendors
    def _create_vendors(self, manifest, users):
        """Three active vendors, through `VendorForm` plus `views.vendor_create`'s own
        three lines (`created_by`, `save()`, `save_m2m()`).

        NO VendorBrand ROWS. `_save_vendor_brands()` reads a dynamic POST body this
        command does not have, and nothing on the order raise reads a brand — its
        vendor select is `Vendor.objects.filter(is_active=True)`. An empty brand list
        is the documented fallback (the vendor's own name is displayed), not a gap.

        `is_active` is not a VendorForm field; the model default is True, which is what
        puts these three in that select.
        """
        from projects.forms import VendorForm
        from projects.models import VendorCategory

        by_name = {c.name: c for c in VendorCategory.objects.all()}
        rows = []
        for name, contact, phone, category_names in VENDORS:
            unknown = [n for n in category_names if n not in by_name]
            if unknown:
                raise CommandError(
                    f'VendorCategory {", ".join(unknown)} does not exist, so {name!r} '
                    f'cannot be created — VendorForm requires at least one category. '
                    f'This database has: {", ".join(sorted(by_name)) or "(none)"}.')
            form = VendorForm({
                'name': name, 'contact_person': contact, 'phone': phone,
                'email': f'{phone}@{EMAIL_DOMAIN}',
                'address': f'{name}, demo address (local only)',
                'gst_number': '', 'msme_status': False, 'msme_number': '',
                'categories': [by_name[n].pk for n in category_names],
            })
            if not form.is_valid():
                raise CommandError(f'VendorForm refused {name!r}: '
                                   f'{form.errors.as_json()}')
            vendor = form.save(commit=False)
            vendor.created_by = users[SCM_USERNAME]
            vendor.save()
            form.save_m2m()
            rows.append(manifest.add(vendor))
        return rows

    # --------------------------------------------------------------- programs
    def _create_programs(self, manifest, users):
        """The two OPEX tenders, through `ProgramForm` plus `views.program_create`'s own
        three lines. The form is what enforces the reserved-code guard and the
        soft-delete-aware uniqueness of `short_tender_code`."""
        from projects.forms import ProgramForm
        from projects.models import log_activity

        pm = users[PM_USERNAME]
        programs = {}
        for key, code, name, client in TENDERS:
            planned = sum(1 for spec in SITES.values() if spec[0] == key)
            form = ProgramForm({
                'program_type': 'OPEX', 'name': name, 'client_name': client,
                'status': 'Active', 'short_tender_code': code,
                'planned_site_count': planned,
            })
            if not form.is_valid():
                raise CommandError(f'ProgramForm refused {name!r}: '
                                   f'{form.errors.as_json()}')
            program = form.save(commit=False)
            program.created_by = pm.user
            program.save()
            manifest.add(program)
            log_activity(
                None, pm, f'Created {program.program_type} Program: {program.name}',
                entity_type='Program', entity_id=program.pk,
                action_code='program_created',
            )
            programs[key] = program
        return programs

    # ------------------------------------------------------------------ sites
    def _create_sites(self, manifest, programs, users):
        """Seven sites through `views.create_opex_site()` — a genuine request-independent
        service, so this is the real creation path end to end, including the
        OpexSiteForm validation and the R-2 ledger row.

        Left in Draft. See the module docstring: release is a DesignAssignment state and
        does not wait on execution starting.
        """
        from projects.views import create_opex_site

        pm = users[PM_USERNAME]
        sites = {}
        for index, (site_code, spec) in enumerate(sorted(SITES.items()), start=1):
            key, city, state, capacity, _multiplier, _extras = spec
            site, form = create_opex_site(
                programs[key],
                {
                    'site_code': site_code,
                    'customer_contact_person': f'Order Demo In-Charge {index:02d}',
                    'customer_phone': '97000001%02d' % index,
                    'customer_email': f'{site_code.lower()}@{EMAIL_DOMAIN}',
                    'site_address': f'{site_code}, demo address (local only)',
                    'city': city, 'state': state,
                    'dc_capacity_kw': str(capacity),
                },
                creator=pm.user, profile=pm,
            )
            if site is None:
                raise CommandError(f'create_opex_site() refused {site_code!r}: '
                                   f'{form.errors.as_json()}')
            manifest.add(site)
            sites[site_code] = site
        return sites

    # ---------------------------------------------------------------- designs
    def _release_designs(self, manifest, sites, users):
        """Every site fixtured straight to `released`, with one closed attempt.

        NO PRODUCT PATH — every design transition lives inside a view. The field-set is
        `seed_opex_test_data._seed_design_state()`'s released branch, unchanged, so the
        two commands cannot end up describing "released" differently.

        `boq_submitted_at` IS SET, and it is not decoration: it is what
        `project_boq_is_design_locked()` reads, so these BOQs are final the way a real
        released site's BOQ is final. A released site whose BOQ was still open for
        editing is a state the product cannot reach.

        NO ArkaSubmission AND NO DesignFile. Both point at objects in the private
        Supabase bucket, which on this machine is reached with the PRODUCTION
        credentials; nothing on the order raise reads either, and a row with a
        fabricated path would make the teardown attempt a storage delete for an object
        that never existed.
        """
        from projects.models import (ATTEMPT_REASON_INITIAL, DESIGN_RELEASED,
                                     DesignAssignment, DesignAttempt, QC_PASSED)

        designer = users[DESIGN_USERNAME]
        now      = timezone.now()

        for offset, (_site_code, site) in enumerate(sorted(sites.items())):
            # Staggered, so post_qc_pool()'s "oldest release first" ordering and its age
            # column have something to order and something to show.
            released_at = now - timedelta(days=3 + offset)

            assignment = DesignAssignment.objects.create(   # NO PRODUCT PATH
                project=site, assigned_to=designer, assigned_by=designer,
                assigned_at=released_at - timedelta(days=20),
                status=DESIGN_RELEASED, released_at=released_at,
                released_by=designer, current_attempt_number=1,
            )
            manifest.add(assignment)

            attempt = DesignAttempt.objects.create(   # NO PRODUCT PATH
                assignment=assignment, attempt_number=1,
                opened_reason=ATTEMPT_REASON_INITIAL,
                qc_started_at=released_at - timedelta(days=1), qc_verdict=QC_PASSED,
                qc_reviewed_by=designer, qc_reviewed_at=released_at,
                boq_submitted_at=released_at - timedelta(days=2),
                boq_submitted_by=designer, closed_at=released_at,
            )
            manifest.add(attempt)
            # opened_at is auto_now_add, so it cannot be set on create.
            DesignAttempt.objects.filter(pk=attempt.pk).update(
                opened_at=released_at - timedelta(days=18))

    # ------------------------------------------------------------------- BOQs
    def _create_boqs(self, manifest, sites):
        """One OPEX BOQ per site, every row linked to a BOQItemMaster.

        NO PRODUCT PATH — `opex_boq_entry` is request-bound. What is copied from it is
        the part that matters: the lazy `BOQ.objects.create(project=...)`, and the
        create loop's field mapping — `serial_no` from the catalogue's `sort_order`,
        and `category` / `description` / `uom` from the MASTER, never invented here. A
        row this writes is indistinguishable from a row the picker writes.

        QUANTITY IS base x multiplier, and the multipliers are all different, so a
        consolidated total is a sum of unequal parts. See SITES.
        """
        from projects.models import BOQ, BOQItem, get_opex_boq_catalogue

        by_code = {m.code: m for m in get_opex_boq_catalogue()}
        boqs = {}
        for site_code, site in sorted(sites.items()):
            _key, _city, _state, _kw, multiplier, extras = SITES[site_code]
            boq = BOQ.objects.create(project=site)          # NO PRODUCT PATH
            manifest.add(boq)
            boqs[site_code] = boq

            for code in sorted(set(CORE_ITEM_CODES) | set(extras)):
                master = by_code[code]
                manifest.add(BOQItem.objects.create(
                    boq=boq, item_master=master, serial_no=master.sort_order,
                    category=master.category, description=master.description,
                    uom=master.unit,
                    boq_quantity=BASE_QUANTITY[code] * multiplier,
                    is_standard_item=True,
                ))
        return boqs

    def _create_unlinked_rows(self, manifest, boqs):
        """The two rows with no `item_master`, on one site.

        NO PRODUCT PATH — this is `boq_detail`'s `add_item` branch, whose field-set is
        copied exactly: `serial_no` is max(serial_no)+1 rather than a catalogue
        `sort_order` (there is no catalogue row to take one from), `item_master` is left
        null, and `is_standard_item=False` — which is what makes an ad-hoc row
        deletable, the only way it differs from a standard one.

        These are the "Not orderable" block on the group raise page. See
        UNLINKED_SITE_CODE for why they live on a site inside a LOCKED group.
        """
        from django.db.models import Max

        from projects.models import BOQItem

        boq    = boqs[UNLINKED_SITE_CODE]
        serial = boq.items.aggregate(Max('serial_no'))['serial_no__max'] or 0
        for category, description, uom, quantity in UNLINKED_ROWS:
            serial += 1
            manifest.add(BOQItem.objects.create(   # NO PRODUCT PATH
                boq=boq, serial_no=serial, category=category,
                description=description, uom=uom, boq_quantity=quantity,
                is_standard_item=False,
            ))

    # ------------------------------------------------------------ site groups
    def _create_groups(self, manifest, programs, sites, users):
        """Three procurement groups — two on Tender A (one locked, one draft) and one
        locked on Tender B.

        The group itself is created the way `site_group_create` creates one, and its
        members go in through `design_views._add_sites()` — the real path, which carries
        the per-site savepoint, the exclusivity IntegrityError catch and the refusal of
        a site that is not released. Driving it here means the fixture cannot contain a
        membership the product would not have allowed.

        THE LOCK HAS NO PRODUCT PATH — `site_group_lock` is a view. Its three field
        writes are replicated exactly, as is its one-log-line-per-member-site: the group
        is not a project, so ActivityLog cannot hang the event off it, and the site is
        where a PM or designer looks to find out why the BOQ stopped accepting edits.
        """
        from projects.design_views import _add_sites
        from projects.models import (GROUP_TYPE_PROCUREMENT, SITE_GROUP_DRAFT,
                                     SITE_GROUP_LOCKED, SiteGroup,
                                     SiteGroupMembership, log_activity)

        scm = users[SCM_USERNAME]
        now = timezone.now()
        groups = {}

        for key, tender_key, name, member_codes, locked in SITE_GROUPS:
            group = SiteGroup.objects.create(
                program=programs[tender_key], name=name, status=SITE_GROUP_DRAFT,
                group_type=GROUP_TYPE_PROCUREMENT, created_by=scm,
                notes=f'{ORDER_DEMO_PREFIX} procurement batch — local only.',
            )
            manifest.add(group)

            member_mark = high_water(SiteGroupMembership)
            _added, refused = _add_sites(
                group, [str(sites[code].pk) for code in member_codes], scm)
            if refused:
                raise CommandError(
                    f'_add_sites() refused a member of {name!r}: {refused}')
            manifest.add_new_since(SiteGroupMembership, member_mark)

            if locked:
                # NO PRODUCT PATH — site_group_lock's own three writes, in its order.
                group.status    = SITE_GROUP_LOCKED
                group.locked_by = scm
                group.locked_at = now
                group.save(update_fields=['status', 'locked_by', 'locked_at'])
                for code in member_codes:
                    log_activity(
                        sites[code], scm,
                        f'BOQ locked — site group "{group.name}" locked for procurement',
                        entity_type='SiteGroup', entity_id=group.pk,
                        action_code='site_group_locked')

            groups[key] = group
        return groups

    # ------------------------------------------------------------------ sweep
    @staticmethod
    def _watched_models():
        """Every model the seed could touch, for the leak sweep."""
        return list(apps.get_app_config('projects').get_models()) + [User]

    def _sweep(self, manifest, marks):
        """Record every row created since the marks that is not already in the manifest.

        This is what makes "the teardown deletes everything the seed made" true rather
        than aspirational: the explicit `manifest.add()` calls above cover what this
        command constructs, and the sweep covers what the real code paths wrote on the
        way past — ActivityLog, StatusTransition, and anything a future change adds
        without remembering to record it.
        """
        swept = {}
        for model, mark in marks.items():
            added = manifest.add_new_since(model, mark)
            if added:
                swept[Manifest.model_label(model)] = len(added)
        return swept

    # ----------------------------------------------------------------- report
    def _report(self, manifest, users, vendors, programs, sites, groups, swept,
                written, notif_before):
        from projects.design_views import _group_member_ids, aggregate_group_boq

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS('=' * 72))
        self.stdout.write(self.style.SUCCESS('Order-demo data created.'))
        self.stdout.write(self.style.SUCCESS('=' * 72))

        self.stdout.write('')
        self.stdout.write('Manifest written to:')
        self.stdout.write(self.style.SUCCESS(f'  {written}'))
        self.stdout.write(f'  {len(manifest.entries)} rows recorded, in creation order.')

        self.stdout.write('')
        self.stdout.write('Rows recorded, by model:')
        for label, count in sorted(manifest.counts_by_model().items()):
            marker = '  (leak sweep)' if label in swept else ''
            self.stdout.write(f'  {label:<34} {count}{marker}')

        self.stdout.write('')
        for key, code, name, _client in TENDERS:
            program = programs[key]
            self.stdout.write(f'Tender {key}: {name!r} ({code}) — pk={program.pk}')
            for site_code, spec in sorted(SITES.items()):
                if spec[0] != key:
                    continue
                site   = sites[site_code]
                total  = site.boq.items.count()
                nulls  = site.boq.items.filter(item_master__isnull=True).count()
                suffix = f', {nulls} unlinked' if nulls else ''
                self.stdout.write(f'    {site_code:<12} {site.status:<6} '
                                  f'{total} BOQ rows{suffix}, quantities x{spec[4]}')

        self.stdout.write('')
        self.stdout.write('Procurement groups — as aggregate_group_boq() sees them:')
        for key, _tender, name, _member_codes, _locked in SITE_GROUPS:
            group = groups[key]
            agg   = aggregate_group_boq(_group_member_ids(group))
            self.stdout.write(f'  {name}')
            self.stdout.write(
                f'    pk={group.pk} {group.status:<7} sites={agg["site_count"]} '
                f'items={agg["item_count"]} unlinked={len(agg["unlinked"])}')
        grouped   = {code for _k, _t, _n, codes, _l in SITE_GROUPS for code in codes}
        ungrouped = sorted(set(SITES) - grouped)
        self.stdout.write(f'  In no group, so in the post-QC pool: '
                          f'{", ".join(ungrouped) or "(none)"}')

        self.stdout.write('')
        self.stdout.write('Vendors: ' + ', '.join(v.name for v in vendors))
        self.stdout.write(f'NotificationLog unchanged at {notif_before}.')

        self.stdout.write('')
        self.stdout.write(self.style.WARNING(
            'NOT created, deliberately — these are the demonstration:'))
        self.stdout.write('  ' + ', '.join(NOT_CREATED))

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            'Sign in as the SCM account — it is the only role '
            'user_can_raise_group_order() accepts:'))
        self.stdout.write(f'  password: {PASSWORD}')
        for username, _f, _l, role, _p in DEMO_USERS:
            self.stdout.write(f'  {username:<16} {role:<8} {username}@{EMAIL_DOMAIN}')

        self.stdout.write('')
        self.stdout.write('Then open the group raise, entering from Tender A:')
        self.stdout.write(self.style.SUCCESS(
            f'  /orders/new-group/?program={programs["A"].pk}'))
        self.stdout.write('  Every procurement group in the system is offered there, '
                          'across both tenders;')
        self.stdout.write('  the entering tender only decides what is pre-ticked.')

        self.stdout.write('')
        self.stdout.write('Remove all of it with:')
        self.stdout.write(f'  python manage.py teardown_order_demo --confirm'
                          + ('' if str(written) == str(DEFAULT_MANIFEST_PATH)
                             else f' --manifest "{written}"'))
