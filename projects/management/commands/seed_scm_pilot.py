"""
Management command: seed the walkthrough data for the SCM pilot rehearsal.

    python manage.py seed_scm_pilot --dry-run   # plan only, writes nothing
    python manage.py seed_scm_pilot             # create

TYPE THE COMMAND NAME IN FULL. `se`+Tab does not disambiguate this from
`send_eod_digest`, which mails the whole company, nor from the three demo-data
commands. The runbook already records that collision; it is repeated here because this
is where somebody will be typing fast.

THE LOCAL DATABASE IS A RESTORED PRODUCTION DUMP
------------------------------------------------
Real people, real phone numbers, real email addresses, and production's own
`SystemSettings` row. Everything below is shaped by that:

  * ADDITIVE ONLY. This command INSERTs. It never UPDATEs or DELETEs a row that came
    from the dump, for any reason, including to correct something that looks wrong. The
    only rows it updates are `UserProfile` rows the post_save signal created
    milliseconds earlier, on users this command itself made.
  * IT CREATES ITS OWN USERS AND NEVER BORROWS ONE. The dump's password hashes are real
    and unknown; the only way to sign in as a dump account is to overwrite a real
    employee's password. That is refused here and is not offered as an option.
  * IT NEVER TOUCHES SystemSettings. `whatsapp_enabled` and `email_enabled` are both
    False on this restore, and that is the only thing standing between this machine and
    live Interakt / ZeptoMail delivery to real numbers — both API keys are present and
    non-blank in the local environment. As a guard rather than an assumption, the
    NotificationLog row count is captured before and compared after; any change aborts
    the whole transaction.
  * SEEDED EMAIL ADDRESSES USE `.invalid`, which RFC 6761 reserves as permanently
    undeliverable. If a notification ever does escape, it cannot reach a person.

WHY `SCMPILOT` AND NOT `DEMO`
------------------------------
`seed_opex_test_data` / `seed_scm_handoff_data` / `teardown_opex_test_data` are a
working, current trio (last touched 1 Sep 2026) that owns the `DEMO` namespace and a
shared manifest at ~/.horizon-pms-demo/. This command deliberately shares NEITHER:

  * `teardown_opex_test_data` deletes exactly the primary keys in the DEMO manifest.
    Writing pilot rows into that manifest would mean a teardown months from now takes
    the pilot down with the demo, and nobody would connect the two.
  * The demo seeds create SiteGroups and a DeliveryChallan. This command must not — see
    below. Reusing them would seed away the thing the pilot exists to show.

So the two namespaces are independent by construction. The small helpers below
(`_database_is_local`, `_high_water`) duplicate `_demo_support` equivalents ON PURPOSE:
duplicating twelve lines is the price of not coupling the pilot's lifetime to the demo
tooling's, and it is the cheaper of the two mistakes.

THERE IS NO TEARDOWN, BY DECISION. Local is disposable and a re-dump is the reset path.
The manifest is written anyway, because it is the record of what a rehearsal was
standing on — not because something is going to read it back and delete things.

WHAT IT DELIBERATELY DOES NOT CREATE
-------------------------------------
The SiteGroup, the DeliveryChallan, the GRN, the payment request and the delivery
issue. Those five ARE the demonstration. Seeding them leaves nothing to show.

ACTIVATION GOES THROUGH THE REAL VIEW
--------------------------------------
`opex_site_activate` is a request-bound view with no extracted core, so the only two
ways to activate are to replicate its atomic block inline (what `seed_opex_test_data`
does, and marks NO PRODUCT PATH) or to drive the view itself. This drives the view,
through `django.test.Client`, signed in as the pilot PM with the pilot password.

That was decided against the inline copy for a specific failure it prevents: a copy
attaches the template and stamps `activated_at` but silently drifts from the view on
the StatusTransition row, or the mirrors, or the activity log — and then the pilot
sites have no activation ledger entry, and the first screen that reads the ledger
during the rehearsal shows nothing, attributed to the wrong cause.

The post-conditions are asserted and printed per site REGARDLESS of route, so a
divergence is caught rather than assumed away: `activated_at` set, status Active, 23
tasks, 8 mirrors, 4 delivery rows, and a StatusTransition row for the activation.

`SERVER_NAME='localhost'` is passed because ALLOWED_HOSTS on this machine is
`localhost,127.0.0.1` and the Client's default `testserver` is not in it.

THE DESIGN WALK
----------------
Five sites are FIXTURED directly to `released`. The sixth carries NO DesignAssignment
row at all, because that is where a real walk starts: `_get_or_create_assignment()`
makes the row lazily on the first survey action, so a hand-created row at
`awaiting_survey` would already be a fixture of the step it is meant to prove.

The walk is PARTIAL by decision, and the manifest says so in that word. Reaching
`released` requires a CAD upload through `design_artifact_upload`, and this machine's
Supabase credentials are the PRODUCTION ones — there is no master switch for storage
the way there is for notifications. So the walk proves the reachable half (survey ->
allocate -> Arka submit -> Arka gate 1 -> Arka gate 2) and stops before storage.

SIX SEEDED USERS, NOT FIVE. The walkthrough names five roles, but the gate-1 Arka
verdict needs a Design QC reviewer who is NEITHER the site's designer NOR the Design
Head — `_other_gate_actor_conflict()` (settled decision 2) refuses one person both
verdicts on the same artifact, and `user_can_qc_gate_design()` refuses the designer
outright. Without a sixth account the walk cannot pass gate 1 at all except by
borrowing a real employee's login, which is the thing this command refuses to make
possible. The sixth user is the walk's precondition, not a convenience.
"""
import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Max
from django.test import Client
from django.urls import reverse
from django.utils import timezone

# ---------------------------------------------------------------------------
# Identity of the pilot data. Everything this command writes carries SCMPILOT
# somewhere a human will see it, so a reader three weeks from now can tell seeded rows
# from dump rows without consulting anything.
# ---------------------------------------------------------------------------
PILOT_PREFIX   = 'SCMPILOT'
TENDER_CODE    = 'SCMPILOT'          # <=20 chars, [A-Z0-9], and not the reserved 'HRP'
PROGRAM_NAME   = 'SCMPILOT SCM Pilot Walkthrough'
PROGRAM_CLIENT = 'SCMPILOT Client (Local Only)'

PILOT_EMAIL_DOMAIN = 'scmpilot.invalid'   # RFC 6761 — permanently undeliverable
PILOT_PASSWORD     = 'ScmPilot!2026'      # >=8 chars, satisfies UserCreateForm

#: (site_code, city, state, dc_capacity_kw). The site_code IS the project_id — no tender
#: prefix (settled 5 Aug 2026). No hyphens: OpexSiteForm strips everything outside
#: [A-Z0-9], so a hyphen here would silently produce a different project_id.
PILOT_SITES = [
    ('SCMPILOT01', 'New Delhi', 'Delhi',           Decimal('150.00')),
    ('SCMPILOT02', 'Bhopal',    'Madhya Pradesh',  Decimal('210.00')),
    ('SCMPILOT03', 'Indore',    'Madhya Pradesh',  Decimal('185.50')),
    ('SCMPILOT04', 'Jammu',     'Jammu & Kashmir', Decimal('120.00')),
    ('SCMPILOT05', 'Gwalior',   'Madhya Pradesh',  Decimal('95.00')),
    ('SCMPILOT06', 'Srinagar',  'Jammu & Kashmir', Decimal('160.00')),
]

#: The one site left for a real design walk. Everything else is stage dressing.
WALK_SITE_CODE      = 'SCMPILOT06'
FIXTURED_SITE_CODES = [code for code, _, _, _ in PILOT_SITES if code != WALK_SITE_CODE]

#: username, first, last, role, phone, profile flags to raise.
#: No Admin: UserCreateForm.clean() refuses a second one and this database has one.
PILOT_USERS = [
    ('scmpilot.pm',         'Pilot', 'Pm',         'PM',            '9500000001', ()),
    ('scmpilot.scm',        'Pilot', 'Scm',        'SCM',           '9500000002', ()),
    ('scmpilot.se',         'Pilot', 'Se',         'Site Engineer', '9500000003', ()),
    ('scmpilot.design',     'Pilot', 'Design',     'Design',        '9500000004', ()),
    # Gate 1 — the Arka / package QC reviewer. See the docstring: the walk cannot pass
    # gate 1 without an account that is neither the designer nor the Head.
    ('scmpilot.designqc',   'Pilot', 'Designqc',   'Design',        '9500000005',
     ('is_design_qc',)),
    # Gate 2 — Design Head. `is_design_head` is a FLAG on a Design-role profile, not a
    # role of its own; production's own Head is shaped the same way. No uniqueness
    # constraint exists on it, so raising it here takes nothing from the real Head.
    ('scmpilot.designhead', 'Pilot', 'Designhead', 'Design',        '9500000006',
     ('is_design_head',)),
]

PM_USERNAME            = 'scmpilot.pm'
SCM_USERNAME           = 'scmpilot.scm'
SE_USERNAME            = 'scmpilot.se'
WALK_DESIGNER_USERNAME = 'scmpilot.design'
WALK_QC_USERNAME       = 'scmpilot.designqc'
WALK_HEAD_USERNAME     = 'scmpilot.designhead'

#: code, name, city, state, is_central. Delhi is the central warehouse.
PILOT_WAREHOUSES = [
    ('SCMPILOT-WH-DEL', 'SCMPILOT Delhi Central Warehouse', 'New Delhi', 'Delhi',           True),
    ('SCMPILOT-WH-MP',  'SCMPILOT Madhya Pradesh Store',    'Bhopal',    'Madhya Pradesh',  False),
    ('SCMPILOT-WH-JK',  'SCMPILOT Jammu Store',             'Jammu',     'Jammu & Kashmir', False),
]

#: Provenance vocabulary for the manifest. A fixtured status proves a screen renders and
#: proves nothing about whether anyone can reach it — which is the whole reason these are
#: three words and not one.
PROV_WALKED   = 'WALKED'     # reached this status through the real path
PROV_PARTIAL  = 'PARTIAL'    # walked to Arka approval, fixtured onward to released
PROV_FIXTURED = 'FIXTURED'   # set directly

PROVENANCE_VOCABULARY = {
    PROV_WALKED:   'reached this status through the real path',
    PROV_PARTIAL:  'walked to Arka approval, fixtured onward to released',
    PROV_FIXTURED: 'set directly',
}

#: The five rows this command must not create. They are the demonstration.
NOT_CREATED = ['SiteGroup', 'DeliveryChallan', 'GRN confirmation', 'PaymentRequest',
               'delivery Issue']

DEFAULT_MANIFEST_PATH = Path.home() / '.horizon-pms-scmpilot' / 'scmpilot_manifest.json'

LOCAL_HOSTS = {'', 'localhost', '127.0.0.1', '::1'}

#: What a refusal names as "would write". Plain English, because the point of listing
#: the writes is that somebody about to make a mistake reads them.
WRITES = [
    f'{len(PILOT_USERS)} pilot users (@{PILOT_EMAIL_DOMAIN}) with a shared known password',
    f'{len(PILOT_WAREHOUSES)} StockLocation rows',
    f'1 OPEX Program ({PROGRAM_NAME!r}) with {len(PILOT_SITES)} sites, all activated '
    f'through opex_site_activate (7 phases / 23 tasks / 8 mirrors each)',
    f'Site Engineer task assignments on all {len(PILOT_SITES)} sites',
    f'{len(FIXTURED_SITE_CODES)} DesignAssignments fixtured to released (+ attempts and '
    f'Arka submissions); {WALK_SITE_CODE} left with none',
    'the ActivityLog and StatusTransition rows the real activation path writes',
]


# ---------------------------------------------------------------------------
# Local-database interlock. Deliberately duplicated from _demo_support rather than
# imported — see the module docstring on why the two namespaces stay independent.
# There is NO override flag: unlike the demo seeds, nothing about a pilot rehearsal
# needs a throwaway remote database, and an override is a door that only ever gets used
# the once it should not have been.
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


def _high_water(model):
    """Current max pk for `model`, 0 on an empty table. Taken before a write so rows
    created as a SIDE EFFECT of a real code path can be identified afterwards — which
    are the rows nobody thinks to record, and therefore exactly the ones that go
    unrecorded."""
    return model.objects.aggregate(_m=Max('pk'))['_m'] or 0


class Command(BaseCommand):
    help = ('Seed the SCM pilot walkthrough data on a LOCAL database. Additive only; '
            'refuses a non-local database outright. Idempotent.')

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='Print what would be created and create nothing.')
        parser.add_argument('--manifest', type=str, default='',
                            help=f'Where to write the manifest. '
                                 f'Default: {DEFAULT_MANIFEST_PATH}')

    # ================================================================== handle
    def handle(self, *args, **options):
        from projects.models import NotificationLog

        # Always the first line of output, before anything else is decided. Flushed,
        # because stdout is block-buffered when redirected while stderr is not, and a
        # refusal printed before the banner naming the database is worse than useless.
        self.stdout.write(f'[db] host={_database_host() or "(none - local socket)"} '
                          f'name={_database_name()}')
        try:
            self.stdout.flush()
        except (AttributeError, ValueError):
            pass    # a StringIO in a test, or an already-closed stream — never fatal

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

        if options['dry_run']:
            self.stdout.write(self.style.WARNING('DRY RUN — nothing will be created.'))
            for line in WRITES:
                self.stdout.write(f'  WOULD CREATE  {line}')
            self.stdout.write('')
            self.stdout.write('  WOULD NOT CREATE (deliberately — these are the '
                              'demonstration):')
            for name in NOT_CREATED:
                self.stdout.write(f'      {name}')
            self.stdout.write('')
            self.stdout.write(f'  Manifest would be written to: {self.manifest_path}')
            self.stdout.write(f'  Pilot password would be:      {PILOT_PASSWORD}')
            return

        # Running tallies. `created` and `reused` are kept apart all the way to the
        # report, because "idempotent" is a claim the second run has to be able to prove.
        self.created     = {}
        self.reused      = {}
        self.rows        = {}      # model label -> [{pk, label, created}]
        self.activation  = {}      # site_code -> post-condition facts
        self.design_rows = []      # the provenance table

        notif_before = NotificationLog.objects.count()

        with transaction.atomic():
            users      = self._users()
            warehouses = self._warehouses(users)
            program    = self._program(users)
            sites      = self._sites(program, users)
            self._activate(sites, users)
            se_tasks   = self._assign_se_tasks(sites, users)
            self._design_state(sites, users)

            # Guard, not decoration: if anything unexpectedly emitted a notification,
            # abort so no real person is messaged about pilot data. The local Interakt
            # and ZeptoMail keys are live; only two booleans stand in the way.
            notif_after = NotificationLog.objects.count()
            if notif_after != notif_before:
                raise CommandError(
                    f'NotificationLog grew from {notif_before} to {notif_after} during '
                    f'seeding — aborting and rolling back.')

        self._write_manifest()
        self._report(users, warehouses, program, se_tasks, notif_before)

    # -------------------------------------------------------------- bookkeeping
    def _record(self, label, obj, name, was_created):
        """Tally one row and remember it for the manifest."""
        bucket = self.created if was_created else self.reused
        bucket[label] = bucket.get(label, 0) + 1
        self.rows.setdefault(label, []).append(
            {'pk': obj.pk, 'label': name, 'created': was_created})
        return obj

    # ------------------------------------------------------------------- users
    def _users(self):
        """Six pilot users, through `UserCreateForm` plus the exact steps
        `views.user_create` performs after it validates.

        THE FORM IS RUN FOR ITS VALIDATION, NOT FOR CONVENIENCE. It is what enforces the
        username charset, the 10-digit phone starting 6-9, the 8-character password
        minimum and the single-Admin rule. A pilot account the real form would have
        refused is not a pilot account.

        Idempotency is checked BEFORE the form rather than by catching its error:
        `clean_username` refuses an existing username, so on a second run the form would
        fail for a reason that is not a failure.

        BOTH ACTIVE FLAGS ARE ASSERTED, not assumed. `UserProfile.is_active` and
        `auth.User.is_active` are separate columns, and this database already contains
        one account where they disagree (`pradeep`) — an account that cannot log in
        while looking active on every screen that reads the profile. A pilot user in
        that state would be discovered mid-rehearsal.
        """
        from projects.forms import UserCreateForm

        profiles = {}
        for username, first, last, role, phone, flags in PILOT_USERS:
            existing = User.objects.filter(username=username).select_related('profile').first()
            if existing is not None:
                profile = existing.profile
                self._assert_loginable(profile)
                profiles[username] = self._record('UserProfile', profile, username, False)
                continue

            form = UserCreateForm({
                'first_name': first, 'last_name': last, 'username': username,
                'email': f'{username}@{PILOT_EMAIL_DOMAIN}', 'password': PILOT_PASSWORD,
                'role': role, 'phone_number': phone, 'is_active': True,
            })
            if not form.is_valid():
                raise CommandError(
                    f'UserCreateForm refused the pilot {role} account {username!r}: '
                    f'{form.errors.as_json()}')
            cd = form.cleaned_data

            user = User.objects.create_user(
                username=cd['username'], password=cd['password'],
                first_name=cd['first_name'], last_name=cd['last_name'],
                email=cd['email'], is_active=cd['is_active'],
                is_staff=False,          # no pilot account gets the Django admin
            )
            profile = user.profile       # auto-created by the post_save signal
            profile.role         = cd['role']
            profile.phone_number = cd['phone_number']
            profile.is_active    = cd['is_active']
            for flag in flags:
                setattr(profile, flag, True)
            profile.save()

            self._assert_loginable(profile)
            profiles[username] = self._record('UserProfile', profile, username, True)

        # Sanity on the thing the whole walk depends on: three distinct people at the
        # designer, gate-1 and gate-2 positions. If a future edit collapses two of them,
        # `_other_gate_actor_conflict()` refuses the second verdict in the browser and
        # the walk looks broken rather than misconfigured.
        trio = {profiles[WALK_DESIGNER_USERNAME].pk, profiles[WALK_QC_USERNAME].pk,
                profiles[WALK_HEAD_USERNAME].pk}
        if len(trio) != 3:
            raise CommandError(
                'The designer, the gate-1 QC reviewer and the Design Head must be three '
                'different people — settled decision 2 refuses one person both verdicts '
                'on the same artifact.')
        return profiles

    def _assert_loginable(self, profile):
        """Both active flags True, or refuse."""
        if not profile.user.is_active or not profile.is_active:
            raise CommandError(
                f'Pilot user {profile.user.username!r} is not loginable: '
                f'auth.User.is_active={profile.user.is_active}, '
                f'UserProfile.is_active={profile.is_active}. Both must be True.')

    # -------------------------------------------------------------- warehouses
    def _warehouses(self, users):
        """Three StockLocation rows, Delhi central.

        NO PRODUCT PATH — StockLocation has no view, no form and no admin registration
        anywhere in the codebase, so `get_or_create` is not a shortcut past a real path;
        it is the only path that exists. Recorded as such rather than left to be
        inferred — `tests_capability_flags.py` already calls this table out as "one
        table with no writer".
        """
        from projects.models import StockLocation

        keeper = users[SCM_USERNAME]
        rows = []
        for code, name, city, state, is_central in PILOT_WAREHOUSES:
            location, was_created = StockLocation.objects.get_or_create(
                code=code,
                defaults={
                    'name': name, 'city': city, 'state': state,
                    'address': f'{name}, pilot address (local only)',
                    'is_central': is_central, 'keeper': keeper, 'is_active': True,
                },
            )
            rows.append(self._record('StockLocation', location, code, was_created))
        return rows

    # ----------------------------------------------------------------- program
    def _program(self, users):
        """The OPEX tender, through `ProgramForm` plus `views.program_create`'s own three
        lines. The form is what enforces the reserved-code guard and the
        soft-delete-aware GLOBAL uniqueness of `short_tender_code` — the latter matters
        because the code is a building block of every site's project_id.
        """
        from projects.forms import ProgramForm
        from projects.models import Program, log_activity

        existing = Program.objects.filter(short_tender_code=TENDER_CODE).first()
        if existing is not None:
            return self._record('Program', existing, existing.name, False)

        pm = users[PM_USERNAME]
        form = ProgramForm({
            'program_type': 'OPEX', 'name': PROGRAM_NAME,
            'client_name': PROGRAM_CLIENT, 'status': 'Active',
            'short_tender_code': TENDER_CODE,
            'planned_site_count': len(PILOT_SITES),
        })
        if not form.is_valid():
            raise CommandError(f'ProgramForm refused the pilot tender: '
                               f'{form.errors.as_json()}')
        program = form.save(commit=False)
        program.created_by = pm.user
        program.save()
        log_activity(
            None, pm, f'Created {program.program_type} Program: {program.name}',
            entity_type='Program', entity_id=program.pk, action_code='program_created',
        )
        return self._record('Program', program, program.name, True)

    # ------------------------------------------------------------------- sites
    def _sites(self, program, users):
        """Six sites through `views.create_opex_site()` — a genuine request-independent
        service, so this is the real creation path end to end: OpexSiteForm validation,
        the frozen client_name copy, the explicit project_id, and the R-2 ledger row.

        `profile=pm` is what sets `assigned_pm`, and that is load-bearing rather than
        cosmetic: `opex_site_activate` gates on `_pm_owns_project()`, so a site created
        without a PM profile could not be activated by the step that follows. Asserted
        rather than trusted, because the failure would otherwise arrive as a bare 404.
        """
        from projects.models import Project
        from projects.views import create_opex_site

        pm = users[PM_USERNAME]
        sites = {}
        for index, (site_code, city, state, capacity) in enumerate(PILOT_SITES, start=1):
            existing = Project.objects.filter(project_id=site_code).first()
            if existing is not None:
                sites[site_code] = self._record('Project', existing, site_code, False)
                continue

            site, form = create_opex_site(
                program,
                {
                    'site_code': site_code,
                    'customer_contact_person': f'Pilot Site In-Charge {index:02d}',
                    'customer_phone': '95000001%02d' % index,
                    'customer_email': f'site{index:02d}@{PILOT_EMAIL_DOMAIN}',
                    'site_address': f'{site_code}, pilot address (local only)',
                    'city': city, 'state': state,
                    'dc_capacity_kw': str(capacity),
                },
                creator=pm.user, profile=pm,
            )
            if site is None:
                raise CommandError(
                    f'create_opex_site() refused {site_code!r}: {form.errors.as_json()}')
            if site.assigned_pm_id != pm.pk:
                raise CommandError(
                    f'{site_code}: assigned_pm was not set to the pilot PM, so '
                    f'opex_site_activate would 404 at _pm_owns_project().')
            sites[site_code] = self._record('Project', site, site_code, True)
        return sites

    # -------------------------------------------------------------- activation
    def _activate(self, sites, users):
        """Activate all six sites BY POSTING TO THE REAL VIEW.

        Not a replication of `opex_site_activate`'s atomic block — the view itself,
        through `django.test.Client`, signed in as the pilot PM with the pilot password.
        Nothing here knows what activation is supposed to write, which is the point: an
        inline copy that drifts from the view produces sites that RESEMBLE activated
        sites, and the drift surfaces during a rehearsal attributed to the wrong cause.

        `client.login()` rather than `force_login()` deliberately — it exercises the real
        authentication backend and so proves, as a side effect, that the seeded PM
        account can actually sign in. That is one of the two things a seeded user is for.

        THE LOGIN IS LAZY, AND THAT IS AN IDEMPOTENCY FIX RATHER THAN AN OPTIMISATION.
        `signals` writes an ActivityLog row on `user_logged_in` ('User logged in from
        unknown', action_code `user_login`). Logging in unconditionally therefore made a
        re-run that created nothing still write one row, while the report said NOTHING
        WAS CREATED — a small lie, in the command whose entire job is to be honest about
        what it made. So the client is built only once a site actually needs activating.

        POST-CONDITIONS ARE ASSERTED HERE AND NOT ASSUMED, and they are the same six
        whichever route is ever taken: this is the check that would catch the drift the
        inline copy risks.
        """
        from projects.models import Task

        pm = users[PM_USERNAME]
        client = None
        self.login_side_effect = False

        for site_code, _, _, _ in PILOT_SITES:
            site = sites[site_code]
            if site.status != 'Draft':
                # The view REFUSES a second activation rather than being idempotent
                # (there is no uniqueness on (project, phase_order), so a second attach
                # would silently produce 14 phases and 46 tasks). Skipping here is what
                # makes this command idempotent without asking the view to be.
                site.refresh_from_db()
                self.activation[site_code] = self._activation_facts(site, reused=True)
                self.reused['Activation'] = self.reused.get('Activation', 0) + 1
                continue

            if client is None:
                client = Client(SERVER_NAME='localhost')  # ALLOWED_HOSTS has no 'testserver'
                if not client.login(username=pm.user.username, password=PILOT_PASSWORD):
                    raise CommandError(
                        f'The pilot PM {pm.user.username!r} could not log in with the '
                        f'pilot password. Activation goes through the real view and '
                        f'cannot proceed.')
                self.login_side_effect = True

            task_mark = _high_water(Task)
            response = client.post(reverse('opex_site_activate', args=[site.project_id]))
            if response.status_code != 302:
                raise CommandError(
                    f'{site_code}: opex_site_activate returned HTTP '
                    f'{response.status_code}, expected a 302 redirect.')

            site.refresh_from_db()
            facts = self._activation_facts(site, reused=False)
            facts['tasks_created_by_this_post'] = Task.objects.filter(
                phase__project=site, pk__gt=task_mark).count()
            self.activation[site_code] = facts
            self.created['Activation'] = self.created.get('Activation', 0) + 1

        # ---- the post-conditions, asserted per site -------------------------
        for site_code, facts in self.activation.items():
            problems = []
            if facts['activated_at'] is None:
                problems.append('activated_at is null')
            if facts['status'] != 'Active':
                problems.append(f'status is {facts["status"]!r}, expected \'Active\'')
            if facts['tasks'] != 23:
                problems.append(f'{facts["tasks"]} tasks, expected 23')
            if facts['mirrors'] != 8:
                problems.append(f'{facts["mirrors"]} mirrors, expected 8')
            if facts['delivery_tasks'] != 4:
                problems.append(f'{facts["delivery_tasks"]} delivery rows, expected 4')
            if not facts['has_activation_transition']:
                problems.append('no StatusTransition row for the activation — the '
                                'ledger would be silent on this site')
            if problems:
                raise CommandError(f'{site_code}: activation post-conditions failed — '
                                   + '; '.join(problems))

    def _activation_facts(self, site, reused):
        from projects.models import (REASON_EXECUTION_STARTED, SUBJECT_PROJECT,
                                     StatusTransition, Task)

        tasks = Task.objects.filter(phase__project=site)
        return {
            'reused':         reused,
            'status':         site.status,
            'activated_at':   site.activated_at,
            'phases':         site.phases.count(),
            'tasks':          tasks.count(),
            'mirrors':        tasks.filter(is_mirror=True).count(),
            'delivery_tasks': tasks.filter(task_name__startswith='Delivery').count(),
            'has_activation_transition': StatusTransition.objects.filter(
                subject_type=SUBJECT_PROJECT, subject_id=site.pk,
                to_status='Active', reason_code=REASON_EXECUTION_STARTED,
            ).exists(),
        }

    # ------------------------------------------------------- SE task assignment
    def _assign_se_tasks(self, sites, users):
        """Give the pilot Site Engineer the Site-Engineer-role tasks on every site.

        THIS IS THE GATE THAT 404s `confirm_grn`, and it must not be left to the demo.
        `attach_opex_template` pre-assigns only the PM's three real tasks; all eleven
        Site Engineer rows arrive with `assigned_to` NULL, so a freshly activated site
        has no SE holding anything on it.

        `confirm_grn` is `@role_required(['Site Engineer'])` AND `user_can_view_project()`,
        whose Site Engineer branch is exactly
        `project.phases.filter(tasks__assigned_to=profile).exists()`. One task is
        technically enough; all eleven is what a PM would actually have done, and it is
        what makes the SE dashboard readable during the rehearsal instead of empty.

        ALL SIX SITES, not just the walkthrough one. The design walk site and the site
        the SCM demonstration ends up running on need not be the same, and a 404
        discovered mid-rehearsal costs more than sixty-six rows.

        Written through `assign_tasks_to()`, the set-based chokepoint. It is structurally
        silent — no `notify` parameter exists on it by design — so this cannot page
        anybody. Only tasks with `assigned_to` NULL are touched, so a re-run assigns
        nothing and a hand-made assignment is never overwritten.
        """
        from projects.models import Task
        from projects.utils import assign_tasks_to

        se = users[SE_USERNAME]
        per_site = {}
        for site_code, _, _, _ in PILOT_SITES:
            site = sites[site_code]
            unassigned = Task.objects.filter(
                phase__project=site, assigned_role='Site Engineer',
                assigned_to__isnull=True,
            )
            newly = assign_tasks_to(unassigned, se)
            held  = Task.objects.filter(phase__project=site, assigned_to=se)
            per_site[site_code] = {
                'newly_assigned': newly,
                'total_held':     held.count(),
                'task_names':     list(held.order_by('pk')
                                           .values_list('task_name', flat=True)),
            }
            if per_site[site_code]['total_held'] == 0:
                raise CommandError(
                    f'{site_code}: the pilot Site Engineer holds no task, so confirm_grn '
                    f'would 404 at user_can_view_project().')
            if newly:
                self.created['Task assignment'] = (
                    self.created.get('Task assignment', 0) + newly)
            else:
                self.reused['Task assignment'] = self.reused.get('Task assignment', 0) + 1
        return per_site

    # ------------------------------------------------------------ design state
    def _design_state(self, sites, users):
        """Five assignments fixtured to `released`; the sixth site left with none.

        NO PRODUCT PATH — every design transition lives inside a view, so the five
        fixtures are direct writes, and they are labelled FIXTURED in the manifest for
        exactly that reason. What they buy is the group screens having something to
        render; what they do NOT buy is any evidence that a site can be got to
        `released` by a person, which is what the sixth site is for.

        THE FIXTURES OBEY THE TWO-PERSON RULE even though nothing forces them to. Gate 1
        is stamped with the QC reviewer and gate 2 with the Head, never one person twice.
        A fixture recording both verdicts under one profile would be a row the product
        cannot produce, and the first person to read it would learn something false about
        the rule.

        THE SIXTH SITE GETS NO ROW AT ALL. `_get_or_create_assignment()` creates the
        DesignAssignment lazily on the first survey action, so "no row" IS the state a
        real walk starts from; a hand-made row at `awaiting_survey` would already have
        fixtured the step it is meant to prove.
        """
        from projects.models import (ARKA_APPROVED, ATTEMPT_REASON_INITIAL,
                                     ArkaSubmission, DESIGN_RELEASED, DesignAssignment,
                                     DesignAttempt, QC_PASSED)

        designer = users[WALK_DESIGNER_USERNAME]
        qc       = users[WALK_QC_USERNAME]
        head     = users[WALK_HEAD_USERNAME]
        now      = timezone.now()

        # Staggered so the pool-ageing column on the group screens spans fresh to overdue
        # (it reddens anything >= 14 days) rather than showing five identical rows.
        age_days = {'SCMPILOT01': 3, 'SCMPILOT02': 8, 'SCMPILOT03': 15,
                    'SCMPILOT04': 22, 'SCMPILOT05': 30}

        for site_code in FIXTURED_SITE_CODES:
            site = sites[site_code]
            if getattr(site, 'design_assignment', None) is not None:
                existing = site.design_assignment
                self._record('DesignAssignment', existing, site_code, False)
                # The attempt and the Arka are recorded on the reused path too, not just
                # the created one. The manifest is an inventory of what the pilot data
                # IS, and an inventory that goes thinner every time somebody re-runs the
                # command is worse than no inventory — it reads as though rows vanished.
                for attempt in existing.attempts.order_by('attempt_number'):
                    self._record('DesignAttempt', attempt,
                                 f'{site_code} attempt {attempt.attempt_number}', False)
                    for arka in attempt.arka_submissions.order_by('version'):
                        self._record('ArkaSubmission', arka,
                                     f'{site_code} arka v{arka.version}', False)
                self.design_rows.append({
                    'site': site_code, 'status': existing.status,
                    'provenance': PROV_FIXTURED,
                    'note': 'already present — this run left it untouched',
                })
                continue

            released_at = now - timedelta(days=age_days[site_code])
            assignment = DesignAssignment.objects.create(
                project=site, assigned_to=designer, assigned_by=head,
                assigned_at=released_at - timedelta(days=20),
                status=DESIGN_RELEASED, released_at=released_at, released_by=head,
                current_attempt_number=1,
            )
            self._record('DesignAssignment', assignment, site_code, True)

            attempt = DesignAttempt.objects.create(
                assignment=assignment, attempt_number=1,
                opened_reason=ATTEMPT_REASON_INITIAL,
                boq_submitted_at=released_at - timedelta(days=2),
                boq_submitted_by=designer,
                qc_started_at=released_at - timedelta(days=1),
                qc_verdict=QC_PASSED, qc_reviewed_by=qc,
                qc_reviewed_at=released_at - timedelta(hours=6),
                head_verdict=QC_PASSED, head_reviewed_by=head,
                head_reviewed_at=released_at,
                closed_at=released_at,
            )
            # opened_at is auto_now_add, so it can only be backdated after the insert.
            DesignAttempt.objects.filter(pk=attempt.pk).update(
                opened_at=released_at - timedelta(days=18))
            self._record('DesignAttempt', attempt, f'{site_code} attempt 1', True)

            # survey_file_path on the assignment is left EMPTY on purpose: a fabricated
            # path would point at a production Supabase object that does not exist, and
            # the first download click would fail in a way that looks like a bug.
            arka = ArkaSubmission.objects.create(
                attempt=attempt, version=1, capacity_kw=site.dc_capacity_kw,
                arka_link='https://example.invalid/arka/scmpilot-seed',
                submitted_by=designer,
                verdict=ARKA_APPROVED, reviewed_by=qc,
                reviewed_at=released_at - timedelta(days=5),
                head_verdict=ARKA_APPROVED, head_reviewed_by=head,
                head_reviewed_at=released_at - timedelta(days=4),
                is_current=True,
            )
            self._record('ArkaSubmission', arka, f'{site_code} arka v1', True)

            self.design_rows.append({
                'site': site_code, 'status': DESIGN_RELEASED,
                'provenance': PROV_FIXTURED,
                'note': f'set directly; gate 1 stamped to {qc.user.username}, '
                        f'gate 2 to {head.user.username}',
            })

        # ---- the sixth site --------------------------------------------------
        walk_site = sites[WALK_SITE_CODE]
        if getattr(walk_site, 'design_assignment', None) is not None:
            raise CommandError(
                f'{WALK_SITE_CODE} already carries a DesignAssignment. The walkthrough '
                f'site must start with none — that is the state a real walk begins from, '
                f'and a row here means the walk has already been fixtured.')
        self.design_rows.append({
            'site': WALK_SITE_CODE, 'status': '(no DesignAssignment row)',
            'provenance': PROV_PARTIAL,
            'note': ('to be walked by hand: survey link -> allocate -> Arka submit -> '
                     'Arka gate 1 -> Arka gate 2. Reaching `released` needs a CAD upload, '
                     'which writes to PRODUCTION Supabase, so the walk stops at the Arka '
                     'gate pair and onward to released must be fixtured.'),
        })

    # ---------------------------------------------------------------- manifest
    def _write_manifest(self):
        """The manifest is the record of what a rehearsal was standing on.

        Nothing reads it back — there is no teardown this session, by decision. It exists
        so the WALKED / PARTIAL / FIXTURED distinction survives past the conversation
        that created it. A fixtured status proves a screen renders and proves nothing
        about whether anyone can reach it, and that difference is exactly what gets lost
        in three weeks.

        THE RUN HISTORY IS APPENDED, NOT OVERWRITTEN, and that is the whole reason `runs`
        exists. A plain overwrite meant the second, idempotent run replaced the record of
        the first with `created: {}` — so the file that exists to say what was seeded
        would end up saying nothing ever was, and the person reading it in three weeks
        would draw exactly the wrong conclusion. `rows` is the inventory of what IS;
        `runs` is the history of how it got there. Both are needed, for different
        questions.
        """
        prior_runs = []
        if self.manifest_path.exists():
            try:
                prior_runs = json.loads(
                    self.manifest_path.read_text(encoding='utf-8')).get('runs', [])
            except (ValueError, OSError):
                # A corrupt or unreadable manifest must not abort a seed that already
                # committed. The history is lost; what exists in the database is not.
                prior_runs = []

        payload = {
            'version': 1,
            'command': 'seed_scm_pilot',
            'prefix': PILOT_PREFIX,
            'written_at': timezone.now().isoformat(),
            'database': {'host': _database_host(), 'name': _database_name()},
            'password': PILOT_PASSWORD,
            'created': self.created,
            'reused': self.reused,
            # Every run this manifest has seen, oldest first. The run that actually
            # created the pilot data is the first entry and stays there.
            'runs': prior_runs + [{
                'at': timezone.now().isoformat(),
                'created': self.created,
                'reused': self.reused,
            }],
            'rows': self.rows,
            'activation': {
                site: {k: (v.isoformat() if hasattr(v, 'isoformat') else v)
                       for k, v in facts.items()}
                for site, facts in self.activation.items()
            },
            'design_assignments': self.design_rows,
            'provenance_vocabulary': PROVENANCE_VOCABULARY,
            'deliberately_not_created': NOT_CREATED,
            # Recorded rather than left to be discovered. Driving the real activation
            # view means signing in, and signing in writes an ActivityLog row — a row
            # this command did not set out to make, on a table it otherwise only adds to
            # through the paths it drives.
            'side_effects': {
                'activity_log_login_row': self.login_side_effect,
                'note': ('signals writes one ActivityLog row on user_logged_in '
                         "('User logged in from unknown', action_code=user_login) when "
                         'the PM signs in to drive opex_site_activate. Written only on a '
                         'run that actually activates something.'),
            },
        }
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        self.manifest_path.write_text(json.dumps(payload, indent=2), encoding='utf-8')

    # ------------------------------------------------------------------ report
    def _report(self, users, warehouses, program, se_tasks, notif_before):
        from projects.models import NotificationLog

        w = self.stdout.write

        w('')
        if sum(self.created.values()) == 0:
            w(self.style.WARNING(
                'NOTHING WAS CREATED — every row this command makes was already present. '
                'This is the idempotent path.'))
        else:
            w(self.style.SUCCESS('SCM pilot data seeded.'))

        w('')
        w('ROWS')
        w('  %-18s %-10s %s' % ('model', 'created', 'already present'))
        for label in sorted(set(self.created) | set(self.reused)):
            w('  %-18s %-10s %s'
              % (label, self.created.get(label, 0), self.reused.get(label, 0)))

        w('')
        w('USERS — password %s' % PILOT_PASSWORD)
        w('  %-22s %-16s %-16s %-22s %s'
          % ('username', 'role', 'flags', 'UserProfile.is_active', 'auth.User.is_active'))
        for username, _, _, _, _, flags in PILOT_USERS:
            p = users[username]
            w('  %-22s %-16s %-16s %-22s %s'
              % (username, p.role, ','.join(flags) or '-', p.is_active, p.user.is_active))

        w('')
        w('WAREHOUSES')
        for loc in warehouses:
            w('  %-18s %-36s is_central=%-6s keeper=%s'
              % (loc.code, loc.name, loc.is_central,
                 loc.keeper.user.username if loc.keeper else '-'))

        w('')
        w('PROGRAM')
        w('  %s  (%s / %s)  status=%s  pk=%s'
          % (program.name, program.program_type, program.short_tender_code,
             program.status, program.pk))

        w('')
        w('SITES — activation post-conditions')
        w('  %-12s %-8s %-7s %-8s %-9s %-8s %s'
          % ('site', 'status', 'tasks', 'mirrors', 'delivery', 'ledger', 'activated_at'))
        for site_code, _, _, _ in PILOT_SITES:
            f = self.activation[site_code]
            w('  %-12s %-8s %-7s %-8s %-9s %-8s %s'
              % (site_code, f['status'], f['tasks'], f['mirrors'], f['delivery_tasks'],
                 'OK' if f['has_activation_transition'] else 'MISSING', f['activated_at']))

        w('')
        w('SITE ENGINEER — %s' % users[SE_USERNAME].user.username)
        for site_code, _, _, _ in PILOT_SITES:
            t = se_tasks[site_code]
            w('  %-12s holds %2d task(s)   (%d assigned by this run)'
              % (site_code, t['total_held'], t['newly_assigned']))
        w('  Tasks held on the walkthrough site %s:' % WALK_SITE_CODE)
        for name in se_tasks[WALK_SITE_CODE]['task_names']:
            w('      - %s' % name)

        w('')
        w('DESIGN ASSIGNMENTS — provenance')
        for key, meaning in PROVENANCE_VOCABULARY.items():
            w('  %-10s %s' % (key, meaning))
        w('')
        w('  %-12s %-28s %s' % ('site', 'status', 'provenance'))
        for row in self.design_rows:
            w('  %-12s %-28s %s' % (row['site'], row['status'], row['provenance']))
        w('')
        for row in self.design_rows:
            w('  %s [%s]' % (row['site'], row['provenance']))
            w('      %s' % row['note'])

        w('')
        w('NOT CREATED, DELIBERATELY — these five are the demonstration:')
        for name in NOT_CREATED:
            w('  - %s' % name)

        w('')
        w('NOTIFICATIONS')
        w('  NotificationLog before=%d after=%d (unchanged)'
          % (notif_before, NotificationLog.objects.count()))

        w('')
        w('SIDE EFFECT OF DRIVING THE REAL VIEW')
        if self.login_side_effect:
            w('  1 ActivityLog row written by the user_logged_in signal when the pilot PM')
            w('  signed in to POST to opex_site_activate. Not a row this command set out')
            w('  to make; recorded here and in the manifest rather than left to be found.')
        else:
            w('  None — no site needed activating, so no login happened and no')
            w('  ActivityLog row was written.')

        w('')
        w('NEXT STEP — the walked site is %s' % WALK_SITE_CODE)
        w('  It carries NO DesignAssignment row. Sign in as %s (Design Head) and record'
          % WALK_HEAD_USERNAME)
        w('  a survey FOLDER LINK on it — design_survey_link_set, NOT the file upload,')
        w('  which would write an object into PRODUCTION Supabase. That first action')
        w('  creates the row and moves it to awaiting_allocation.')
        w('')
        w('  Then, still as %s: allocate the site to %s.'
          % (WALK_HEAD_USERNAME, WALK_DESIGNER_USERNAME))
        w('  As %s: submit an Arka (a link, no file).' % WALK_DESIGNER_USERNAME)
        w('  As %s: pass Arka gate 1.' % WALK_QC_USERNAME)
        w('  As %s: pass Arka gate 2.' % WALK_HEAD_USERNAME)
        w('  STOP THERE. The CAD upload that follows writes to production storage.')

        w('')
        w('MANIFEST written to %s' % self.manifest_path)
