"""
Management command: build a populated LOCAL demo environment for hand-testing the
execution module.

    python manage.py seed_opex_test_data --dry-run     # plan only, writes nothing
    python manage.py seed_opex_test_data               # create
    python manage.py teardown_opex_test_data --confirm # remove exactly what was created

TYPE BOTH COMMAND NAMES IN FULL. `se`+Tab does not disambiguate this command from
`send_eod_digest`, which mails the whole company. The runbook already records that
collision; it is repeated here because this is where somebody will be typing fast.

THIS IS LOCAL-ONLY BY DECISION, NOT BY CONVENTION
-------------------------------------------------
Demo data must never reach production. It would pollute the CEO dashboard, the EOD
digest and every execution counter prompt 1.3b just corrected, and it teaches users
that the system is a toy. `_demo_support.require_local_database()` refuses a non-local
host outright; the `DEMO` namespace is a second line of defence so anything that ever
escapes is identifiable by eye.

WHAT CHANGED, AND WHAT THIS COMMAND USED TO BE
----------------------------------------------
This command previously created one `Test-` prefixed OPEX tender with five Draft
sites, and `teardown_opex_test_data` found them again by matching
`project_id__startswith='Test-'` against live tables. Both halves are replaced:

  * The payload is now a whole environment — users, warehouses, an activated tender,
    an activated Residential project, design state, groups, BOQs and a challan —
    because nobody has ever opened an activated OPEX site in a browser, and
    PHASE_0_BROWSER_TEST_PLAN.md has never been run.
  * The teardown reads a MANIFEST of primary keys this command writes, and refuses to
    delete anything else. See `_demo_support` for why a name-prefix sweep was the
    wrong mechanism and why there is deliberately no fallback to it.

`seed_scm_handoff_data` layers Part-6 SCM states on top of what this creates and
records them into the SAME manifest, so one teardown removes both. That is why the
existing pair was extended rather than a second pair added: 390 rows sitting outside
the manifest would have made the discipline decorative.

EVERYTHING GOES THROUGH A REAL CODE PATH WHERE ONE EXISTS
---------------------------------------------------------
Demo data created by a shortcut does not resemble what the product makes, and reading
it then proves nothing. Users go through `UserCreateForm`, the Program through
`ProgramForm`, OPEX sites through `views.create_opex_site()`, phases and tasks through
`utils.attach_opex_template()` / `attach_residential_template()`, group membership
through `design_views._add_sites()`.

Six things have NO code path in the product and are created with `objects.create()`.
Each is marked `# NO PRODUCT PATH` at its call site and listed in
EXECUTION_MODULE_DEFERRED.md §B. Nobody should read their existence here as evidence
that the corresponding workflow works, because there is no workflow:

  1. StockLocation                      — no view, form or admin registration
  2. is_qaqc / is_hse / is_warehouse_keeper — no writer anywhere, not even in admin
  3. A group_type='execution' SiteGroup — site_group_create hardcodes procurement
  4. DeliveryChallan + DCLineItem       — creation is inline in the view
  5. The activation status writes       — inline in opex_site_activate / project_activate
  6. Task status changes                — _apply_task_status_change() needs a request

SEVEN DEMO ROLES, NOT EIGHT. There is no demo Admin. `UserCreateForm.clean()` refuses
a second Admin account ("Only one Admin account is permitted") and this database
already has one. That is a real product rule, and demo tooling is precisely where a
"just this once" bypass gets copied later. Log in as the existing Admin.

NOTIFICATIONS
-------------
Nothing here should send one — `assign_tasks_to()` is structurally silent and no view
runs. As a guard rather than an assumption, the NotificationLog row count is captured
before and compared after; any change aborts the whole transaction.
"""
from datetime import timedelta
from decimal import Decimal

from django.apps import apps
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from projects.management.commands._demo_support import (
    DEMO_EMAIL_DOMAIN, DEMO_PASSWORD, DEMO_PREFIX, DEFAULT_MANIFEST_PATH,
    Manifest, OVERRIDE_FLAG, database_host, database_name, high_water,
    print_db_banner, require_local_database,
)

# ---------------------------------------------------------------------------
# Identity of the demo data. teardown_opex_test_data does NOT import these — it reads
# the manifest — but seed_scm_handoff_data does, so the two seeds cannot drift apart
# about which tender they are layering onto.
# ---------------------------------------------------------------------------
PROGRAM_NAME   = f'{DEMO_PREFIX} Local Demo Tender'
PROGRAM_CLIENT = f'{DEMO_PREFIX} Client (Local Only)'
TENDER_CODE    = 'DEMOTEND'

RESIDENTIAL_PROJECT_ID = f'{DEMO_PREFIX}-RES-01'

#: (site_code, city, capacity_kw). site_code IS the project_id — no tender prefix.
#: No hyphens: OpexSiteForm strips everything outside [A-Z0-9]. See _demo_support.
OPEX_SITES = [
    ('DEMOOPEX01', 'Delhi',     Decimal('120.00')),   # activated
    ('DEMOOPEX02', 'Jaipur',    Decimal('180.50')),   # activated
    ('DEMOOPEX03', 'Lucknow',   Decimal('95.00')),    # Draft — activate by hand
    ('DEMOOPEX04', 'Bhopal',    Decimal('240.00')),   # Draft — design released, grouped
]
ACTIVATED_SITE_CODES = ['DEMOOPEX01', 'DEMOOPEX02']
DRAFT_SITE_CODE      = 'DEMOOPEX03'   # left in Draft so activation can be exercised
RELEASED_SITE_CODE   = 'DEMOOPEX04'   # design released, sits in both site groups

#: username, first, last, role, phone, capability flags to raise.
#: Roles: seven. No Admin — see the module docstring.
DEMO_USERS = [
    ('demo.pm',      'Demo', 'Pm',      'PM',                  '9000000001', ()),
    ('demo.coord',   'Demo', 'Coord',   'Project Coordinator', '9000000002', ()),
    ('demo.se',      'Demo', 'Se',      'Site Engineer',       '9000000003',
     ('is_qaqc', 'is_hse')),
    ('demo.scm',     'Demo', 'Scm',     'SCM',                 '9000000004',
     ('is_warehouse_keeper',)),
    ('demo.design',  'Demo', 'Design',  'Design',              '9000000005', ()),
    ('demo.finance', 'Demo', 'Finance', 'Finance',             '9000000006', ()),
    ('demo.ceo',     'Demo', 'Ceo',     'CEO',                 '9000000007', ()),
]

#: code, name, keeper username or None. Two share a keeper and one has none — the two
#: things B-14 settled that the schema alone does not say out loud.
DEMO_WAREHOUSES = [
    ('DEMO-WH-1', f'{DEMO_PREFIX} Central Warehouse', 'demo.scm'),
    ('DEMO-WH-2', f'{DEMO_PREFIX} North Store',       'demo.scm'),
    ('DEMO-WH-3', f'{DEMO_PREFIX} Site Container',    None),
]

GROUP_PROCUREMENT_NAME = f'{DEMO_PREFIX} Batch 1 — Modules & Structure'
GROUP_EXECUTION_NAME   = f'{DEMO_PREFIX} Execution Crew A'

#: Lines on the demo Delivery Challan. Categories must match DCLineItem.CATEGORY_CHOICES.
DC_LINES = [
    ('Solar Modules', 'DEMO 540Wp Monocrystalline Module', Decimal('220'), 'Nos'),
    ('Structure',     'DEMO Galvanised Module Mounting Structure', Decimal('12'), 'MT'),
    ('BOS',           'DEMO DC Cable 4sqmm', Decimal('1800'), 'Mtr'),
]

#: What the interlock names as "would write" on a non-local host.
WRITES = [
    f'{len(DEMO_USERS)} demo users (@{DEMO_EMAIL_DOMAIN}) with a shared known password',
    f'{len(DEMO_WAREHOUSES)} StockLocation rows',
    f'1 OPEX Program ({PROGRAM_NAME!r}) with {len(OPEX_SITES)} sites, '
    f'{len(ACTIVATED_SITE_CODES)} of them activated (7 phases / 22 tasks / 5 mirrors each)',
    f'1 Residential project ({RESIDENTIAL_PROJECT_ID}) activated with its full template',
    '2 DesignAssignments, 2 SiteGroups, 2 BOQs, 1 DeliveryChallan, 1 Issue',
    'the ActivityLog and StatusTransition rows those paths write on the way past',
]


class Command(BaseCommand):
    help = ('Build a populated LOCAL demo environment for hand-testing the execution '
            'module. Refuses a non-local database. Writes a manifest that '
            'teardown_opex_test_data reads.')

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

        # TASK 1 — always the first line of output, before anything else is decided.
        print_db_banner(self)
        require_local_database(
            self, options.get('i_know_this_is_not_local', False), WRITES)

        dry_run       = options['dry_run']
        manifest_path = options['manifest'].strip() or DEFAULT_MANIFEST_PATH

        if dry_run:
            self._print_plan(manifest_path)
            return

        # Refuse to double-seed. A second run would collide on username, site_code and
        # short_tender_code anyway; refusing here says so in words instead of as an
        # IntegrityError three hundred rows in.
        clashes = []
        if User.objects.filter(username__startswith='demo.').exists():
            clashes.append('demo.* user accounts')
        if Program.objects.filter(name=PROGRAM_NAME).exists():
            clashes.append(f'Program {PROGRAM_NAME!r}')
        if Project.objects.filter(project_id__startswith=DEMO_PREFIX).exists():
            clashes.append(f'{DEMO_PREFIX}-prefixed projects')
        if clashes:
            self.stdout.write(self.style.WARNING(
                'Demo data already present — nothing created:'))
            for line in clashes:
                self.stdout.write(f'  {line}')
            self.stdout.write(
                'Run  python manage.py teardown_opex_test_data --confirm  first.')
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
            users      = self._create_users(manifest)
            warehouses = self._create_warehouses(manifest, users)
            program    = self._create_program(manifest, users)
            sites      = self._create_opex_sites(manifest, program, users)
            self._activate_opex_sites(manifest, sites, users)
            residential = self._create_residential(manifest, users)
            self._seed_design_state(manifest, sites, users)
            groups = self._seed_site_groups(manifest, program, sites, users)
            self._seed_boqs(manifest, sites, residential)
            self._seed_delivery_challan(manifest, sites, users)
            self._vary_task_statuses(manifest, sites, users)

            # ---- leak sweep -------------------------------------------------
            # Anything new that was not explicitly recorded above gets recorded now,
            # and lands at the END of the manifest — so the teardown, which walks it
            # in reverse, deletes these FIRST. That is the safe order: side-effect
            # rows are leaves, and deleting a leaf before its parent never trips a
            # PROTECT or orphans anything.
            swept = self._sweep(manifest, marks)

            # Guard, not decoration: if anything unexpectedly emitted a notification,
            # abort so no real user is left having been messaged about demo data.
            notif_after = NotificationLog.objects.count()
            if notif_after != notif_before:
                raise CommandError(
                    f'NotificationLog grew from {notif_before} to {notif_after} during '
                    f'seeding — aborting and rolling back.')

        written = manifest.save(database_host(), database_name())

        self._report(manifest, users, warehouses, program, sites, residential,
                     groups, swept, written, notif_before)

    # ------------------------------------------------------------------ plan
    def _print_plan(self, manifest_path):
        self.stdout.write(self.style.WARNING('DRY RUN — nothing will be created.'))
        for line in WRITES:
            self.stdout.write(f'  WOULD CREATE  {line}')
        self.stdout.write('')
        self.stdout.write(f'  Manifest would be written to: {manifest_path}')
        self.stdout.write(f'  Demo password would be:       {DEMO_PASSWORD}')

    # ------------------------------------------------------------------ users
    def _create_users(self, manifest):
        """Seven users, one per role, through UserCreateForm plus the exact steps
        `views.user_create` performs after validation.

        THE FORM IS RUN FOR ITS VALIDATION, NOT FOR CONVENIENCE. It is what enforces
        the username charset, the 10-digit phone starting 6-9, the 8-character password
        minimum and the single-Admin rule. A demo account the real form would have
        refused is not a demo account.
        """
        from projects.forms import UserCreateForm

        created = {}
        for username, first, last, role, phone, flags in DEMO_USERS:
            form = UserCreateForm({
                'first_name': first, 'last_name': last, 'username': username,
                'email': f'{username}@{DEMO_EMAIL_DOMAIN}', 'password': DEMO_PASSWORD,
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
                is_staff=(cd['role'] == 'Admin'),   # never true here; seven roles, no Admin
            )
            manifest.add(user)

            profile = user.profile        # auto-created by the post_save signal
            profile.role         = cd['role']
            profile.phone_number = cd['phone_number']
            profile.is_active    = cd['is_active']

            # NO PRODUCT PATH — the three execution capability flags have no writer
            # anywhere: not on UserCreateForm, not on UserEditForm, and not in
            # UserProfileAdmin's list_display or list_filter. Their consumers arrive
            # with 2.2 (is_hse), 2.3 (is_qaqc) and 4.1 (is_warehouse_keeper), each
            # bringing its own permission helper (R-12). Setting one here changes the
            # user's authority nowhere, which is R-15's whole point.
            for flag in flags:
                setattr(profile, flag, True)

            profile.save()
            manifest.add(profile)
            created[username] = profile
        return created

    # ------------------------------------------------------------- warehouses
    def _create_warehouses(self, manifest, users):
        from projects.models import StockLocation

        rows = []
        for code, name, keeper_username in DEMO_WAREHOUSES:
            # NO PRODUCT PATH — StockLocation has no view, no form and no admin
            # registration. tests_capability_flags.py records this as deliberate:
            # "one table with no writer", whose consumer arrives in 4.1.
            location = StockLocation.objects.create(
                code=code, name=name,
                address=f'{name}, demo address (local only)',
                keeper=users[keeper_username] if keeper_username else None,
            )
            rows.append(manifest.add(location))
        return rows

    # ---------------------------------------------------------------- program
    def _create_program(self, manifest, users):
        """The OPEX tender, through ProgramForm plus `views.program_create`'s own
        three lines. The form is what enforces the reserved-code guard and the
        soft-delete-aware uniqueness of `short_tender_code`."""
        from projects.forms import ProgramForm
        from projects.models import log_activity

        form = ProgramForm({
            'program_type': 'OPEX', 'name': PROGRAM_NAME,
            'client_name': PROGRAM_CLIENT, 'status': 'Active',
            'short_tender_code': TENDER_CODE,
            'planned_site_count': len(OPEX_SITES),
        })
        if not form.is_valid():
            raise CommandError(f'ProgramForm refused the demo tender: '
                               f'{form.errors.as_json()}')
        program = form.save(commit=False)
        program.created_by = users['demo.pm'].user
        program.save()
        manifest.add(program)
        log_activity(
            None, users['demo.pm'],
            f'Created {program.program_type} Program: {program.name}',
            entity_type='Program', entity_id=program.pk, action_code='program_created',
        )
        return program

    # ------------------------------------------------------------ opex sites
    def _create_opex_sites(self, manifest, program, users):
        """Four sites through `views.create_opex_site()` — a genuine
        request-independent service, so this is the real creation path end to end,
        including the OpexSiteForm validation and the R-2 ledger row."""
        from projects.views import create_opex_site

        pm = users['demo.pm']
        sites = {}
        for index, (site_code, city, capacity) in enumerate(OPEX_SITES, start=1):
            site, form = create_opex_site(
                program,
                {
                    'site_code': site_code,
                    'customer_contact_person': f'Demo Site In-Charge {index:02d}',
                    'customer_phone': '90000000%02d' % (10 + index),
                    'customer_email': f'site{index:02d}@{DEMO_EMAIL_DOMAIN}',
                    'site_address': f'{site_code}, demo address (local only)',
                    'city': city, 'state': 'Demo State',
                    'capacity_kw': str(capacity),
                },
                creator=pm.user, profile=pm,
            )
            if site is None:
                raise CommandError(
                    f'create_opex_site() refused {site_code!r}: {form.errors.as_json()}')
            manifest.add(site)
            sites[site_code] = site
        return sites

    def _activate_opex_sites(self, manifest, sites, users):
        """Start execution on two sites.

        NO PRODUCT PATH for the transition itself — `opex_site_activate` is a view with
        no extracted core, so the three field writes are replicated here EXACTLY as it
        performs them. The two things that actually produce the rows worth testing are
        real: `record_transition()` and `attach_opex_template()`, which is what gives
        each site its 7 phases, 22 tasks and 5 mirrors.

        Deliberately not replicated, because the view does not do them either: no
        PaymentMilestone rows and no calculate_due_dates(). OPEX due dates are set by
        hand, per task, by the PM (B18).
        """
        from projects.models import (ProjectPhase, REASON_EXECUTION_STARTED, Task,
                                     log_activity)
        from projects.utils import attach_opex_template, record_transition

        pm = users['demo.pm']
        for site_code in ACTIVATED_SITE_CODES:
            site = sites[site_code]
            phase_mark = high_water(ProjectPhase)
            task_mark  = high_water(Task)

            previous     = site.status          # 'Draft'
            site.status  = 'Active'
            site.activated_at = timezone.now()
            site.save()
            record_transition(
                site, to_status='Active', from_status=previous, actor=pm,
                reason_code=REASON_EXECUTION_STARTED,
            )
            attach_opex_template(site)

            # Recorded explicitly and in dependency order rather than left to the leak
            # sweep, so the manifest reads as an inventory of what activation produced.
            manifest.add_new_since(ProjectPhase, phase_mark)
            manifest.add_new_since(Task, task_mark)

            log_activity(site, pm, f'Activated project: {site.project_id}',
                         entity_type='Project', entity_id=site.pk)

    # ----------------------------------------------------------- residential
    def _create_residential(self, manifest, users):
        """One activated Residential project, so the two templates can be compared on
        screen side by side.

        `project_id` IS SET EXPLICITLY, which BYPASSES `generate_project_id()`. What
        that forgoes, stated plainly so nobody reads "created through the real path" as
        including it: the HRP-RES-{YEAR}-{NNN} format, the `select_for_update()` lock,
        and the max-suffix scan that reserves a number against soft-deleted rows. No
        other behaviour differs — `Project.save()` takes the explicit-id branch, which
        is the same branch `views.create_opex_site()` uses in production.

        It is bypassed on purpose. Through the generator this project would take the
        next REAL Residential number on a database that is a production restore, would
        be indistinguishable by eye from a real project, and would hand that number
        back for reuse the moment the teardown ran.
        """
        from projects.forms import ProjectCreateForm
        from projects.models import (PaymentMilestone, ProjectPhase, REASON_CREATED,
                                     Task, log_activity)
        from projects.utils import attach_residential_template, record_transition

        pm = users['demo.pm']
        form = ProjectCreateForm({
            'customer_name': f'{DEMO_PREFIX} Residential Customer',
            'customer_phone': '9000000021',
            'customer_email': f'residential@{DEMO_EMAIL_DOMAIN}',
            'site_address': 'Demo residential address (local only)',
            'city': 'Pune', 'state': 'Demo State',
            'project_type': 'Residential',
            'capacity_kw': '8.50', 'contract_value': '450000.00',
            'survey_date': (timezone.localdate() - timedelta(days=30)).isoformat(),
            'target_commissioning_date': (
                timezone.localdate() + timedelta(days=60)).isoformat(),
            'zoho_crm_id': '',
        })
        if not form.is_valid():
            raise CommandError(f'ProjectCreateForm refused the demo Residential '
                               f'project: {form.errors.as_json()}')

        project = form.save(commit=False)
        project.assigned_pm = pm
        project.status      = 'Draft'
        project.created_by  = pm.user
        project.project_id  = RESIDENTIAL_PROJECT_ID   # see the docstring — a bypass
        project.save()
        manifest.add(project)
        record_transition(project, to_status='Draft', actor=pm,
                          reason_code=REASON_CREATED)

        # ---- activation, replicating project_activate's own atomic block ----
        phase_mark = high_water(ProjectPhase)
        task_mark  = high_water(Task)

        previous = project.status
        project.assigned_design = users['demo.design']
        project.status          = 'Active'
        project.activated_at    = timezone.now()
        project.save()
        record_transition(project, to_status='Active', from_status=previous, actor=pm)
        attach_residential_template(project)

        for name, description in (('M1', 'On Survey Completion'),
                                  ('M2', 'On Material Supply'),
                                  ('M3', 'On Commissioning')):
            manifest.add(PaymentMilestone.objects.create(
                project=project, milestone_name=name,
                milestone_description=description, created_by=pm,
            ))

        manifest.add_new_since(ProjectPhase, phase_mark)
        manifest.add_new_since(Task, task_mark)
        log_activity(project, pm, f'Activated project: {project.project_id}',
                     entity_type='Project', entity_id=project.pk)
        return project

    # ---------------------------------------------------------- design state
    def _seed_design_state(self, manifest, sites, users):
        """One assignment mid-workflow and one released.

        NO PRODUCT PATH — every design transition lives inside a view. These are the
        same direct writes `seed_scm_handoff_data` has always used, kept identical so
        the two commands cannot describe "released" differently.
        """
        from projects.models import (ARKA_APPROVED, ATTEMPT_REASON_INITIAL,
                                     ArkaSubmission, DESIGN_IN_DESIGN, DESIGN_RELEASED,
                                     DesignAssignment, DesignAttempt, QC_PASSED)

        designer = users['demo.design']
        now      = timezone.now()

        # --- mid-workflow: allocated, due date agreed, designer at work ---
        mid = DesignAssignment.objects.create(
            project=sites[DRAFT_SITE_CODE], assigned_to=designer,
            assigned_by=designer, assigned_at=now - timedelta(days=6),
            status=DESIGN_IN_DESIGN, current_attempt_number=1,
        )
        manifest.add(mid)
        attempt = DesignAttempt.objects.create(
            assignment=mid, attempt_number=1,
            opened_reason=ATTEMPT_REASON_INITIAL,
        )
        manifest.add(attempt)
        DesignAttempt.objects.filter(pk=attempt.pk).update(
            opened_at=now - timedelta(days=6))   # opened_at is auto_now_add

        # --- released: QC passed, Arka approved, ready for SCM ---
        released_at = now - timedelta(days=3)
        rel = DesignAssignment.objects.create(
            project=sites[RELEASED_SITE_CODE], assigned_to=designer,
            assigned_by=designer, assigned_at=released_at - timedelta(days=20),
            status=DESIGN_RELEASED, released_at=released_at, released_by=designer,
            current_attempt_number=1,
        )
        manifest.add(rel)
        rel_attempt = DesignAttempt.objects.create(
            assignment=rel, attempt_number=1, opened_reason=ATTEMPT_REASON_INITIAL,
            qc_started_at=released_at - timedelta(days=1), qc_verdict=QC_PASSED,
            qc_reviewed_by=designer, qc_reviewed_at=released_at,
            boq_submitted_at=released_at - timedelta(days=2),
            boq_submitted_by=designer, closed_at=released_at,
        )
        manifest.add(rel_attempt)
        DesignAttempt.objects.filter(pk=rel_attempt.pk).update(
            opened_at=released_at - timedelta(days=18))

        # survey_file_path is left EMPTY on purpose: a fake path would make the
        # teardown attempt a Supabase delete for an object that never existed.
        manifest.add(ArkaSubmission.objects.create(
            attempt=rel_attempt, version=1,
            capacity_kw=sites[RELEASED_SITE_CODE].capacity_kw,
            arka_link='https://example.invalid/arka/demo-seed',
            submitted_by=designer, verdict=ARKA_APPROVED, reviewed_by=designer,
            reviewed_at=released_at - timedelta(days=5), is_current=True,
        ))

    # ----------------------------------------------------------- site groups
    def _seed_site_groups(self, manifest, program, sites, users):
        """One group of each `group_type`, so D-1 is visible on screen.

        The PROCUREMENT group is created the way `site_group_create` creates one, and
        its members go in through `design_views._add_sites()` — the real path, which
        carries the per-site savepoint and the exclusivity IntegrityError catch.

        The EXECUTION group has NO PRODUCT PATH AT ALL. `site_group_create` hardcodes
        `GROUP_TYPE_PROCUREMENT` and its own comment says the execution creator "will
        sit beside this one" when it is written. Until then an execution group cannot
        be made through the product by anybody, which is the finding, not the fix.
        Note it stays `draft`: `execution_groups_are_never_locked` is a CHECK
        constraint, so a locked one is not merely discouraged, it is impossible.
        """
        from projects.design_views import _add_sites
        from projects.models import (GROUP_TYPE_EXECUTION, GROUP_TYPE_PROCUREMENT,
                                     SITE_GROUP_DRAFT, SiteGroup, SiteGroupMembership)

        scm = users['demo.scm']
        groups = {}

        procurement = SiteGroup.objects.create(
            program=program, name=GROUP_PROCUREMENT_NAME, status=SITE_GROUP_DRAFT,
            group_type=GROUP_TYPE_PROCUREMENT, created_by=scm,
            notes='Demo procurement batch — local only.',
        )
        manifest.add(procurement)
        groups['procurement'] = procurement

        execution = SiteGroup.objects.create(   # NO PRODUCT PATH — see the docstring
            program=program, name=GROUP_EXECUTION_NAME, status=SITE_GROUP_DRAFT,
            group_type=GROUP_TYPE_EXECUTION, created_by=scm,
            notes='Demo execution crew — no product path creates one of these.',
        )
        manifest.add(execution)
        groups['execution'] = execution

        member_mark = high_water(SiteGroupMembership)
        _added, refused = _add_sites(
            procurement, [str(sites[RELEASED_SITE_CODE].pk)], scm)
        if refused:
            raise CommandError(f'_add_sites() refused the demo procurement member: '
                               f'{refused}')

        # The execution membership cannot go through _add_sites() either — it is
        # reached only from the procurement endpoint. Same shape, stated explicitly.
        SiteGroupMembership.objects.create(   # NO PRODUCT PATH
            group=execution, project=sites[ACTIVATED_SITE_CODES[0]], added_by=scm,
        )
        manifest.add_new_since(SiteGroupMembership, member_mark)
        return groups

    # ------------------------------------------------------------------ BOQs
    def _seed_boqs(self, manifest, sites, residential):
        """One OPEX BOQ from the Part 11 catalogue, one Residential BOQ from the
        standard template — so both catalogues are visible on screen."""
        from projects.models import (BOQ, BOQItem, BOQItemMaster,
                                     get_opex_boq_catalogue, get_opex_mandatory_items,
                                     get_standard_boq_items)

        # ---- OPEX: the picker's own field-set, item by item ----
        opex_boq = BOQ.objects.create(project=sites[RELEASED_SITE_CODE])
        manifest.add(opex_boq)
        catalogue = get_opex_boq_catalogue()[:12]
        mandatory = get_opex_mandatory_items()
        chosen    = {m.pk: m for m in catalogue}
        chosen.update({m.pk: m for m in mandatory})   # mandatory rows survive any save
        for position, master in enumerate(
                sorted(chosen.values(), key=lambda m: (m.sort_order, m.code)), start=1):
            manifest.add(BOQItem.objects.create(
                boq=opex_boq, item_master=master, serial_no=master.sort_order,
                category=master.category, description=master.description,
                uom=master.unit, boq_quantity=Decimal(position * 5),
                is_standard_item=True,
            ))

        # ---- Residential: exactly what boq_detail does on a designer's first GET ----
        # The project_type term is required, not cosmetic: three descriptions exist in
        # both catalogues and unscoped the OPEX row wins the key.
        res_boq = BOQ.objects.create(project=residential)
        manifest.add(res_boq)
        masters = {m.description: m for m in BOQItemMaster.objects.filter(
            is_active=True, project_type='Residential')}
        rows = BOQItem.objects.bulk_create([
            BOQItem(boq=res_boq, item_master=masters.get(d['description']), **d)
            for d in get_standard_boq_items()
        ])
        # bulk_create() returns rows with pks on PostgreSQL; re-read rather than trust
        # it, so the manifest is right on any backend.
        manifest.add_all(res_boq.items.order_by('pk'))
        return len(rows)

    # -------------------------------------------------------- delivery challan
    def _seed_delivery_challan(self, manifest, sites, users):
        """One challan with three lines on an activated site.

        NO PRODUCT PATH — challan creation is inline in `delivery_challan_create`,
        with no extracted service. The field-set below, including the R-2 ledger row
        and the deliberate absence of a `recalculate_dc_status()` call, is copied from
        that view so the demo challan is shaped exactly like a real one.
        """
        from projects.models import (DCLineItem, DeliveryChallan, REASON_CREATED,
                                     Vendor, log_activity)
        from projects.utils import record_transition

        scm     = users['demo.scm']
        project = sites[ACTIVATED_SITE_CODES[0]]
        vendor  = Vendor.objects.filter(is_active=True).order_by('pk').first()

        challan = DeliveryChallan.objects.create(   # NO PRODUCT PATH
            project=project, vendor=vendor, po_number=f'{DEMO_PREFIX}-PO-0001',
            dc_number=f'{DEMO_PREFIX}-DC-0001',
            dc_date=timezone.localdate() - timedelta(days=2),
            expected_delivery_date=timezone.localdate() + timedelta(days=5),
            status=DeliveryChallan.EXPECTED,
            notes='Demo challan — local only.', created_by=scm,
        )
        manifest.add(challan)
        record_transition(challan, to_status=DeliveryChallan.EXPECTED, actor=scm,
                          reason_code=REASON_CREATED)
        for category, description, quantity, unit in DC_LINES:
            manifest.add(DCLineItem.objects.create(
                challan=challan, boq_category=category, item_description=description,
                ordered_quantity=quantity, unit=unit,
            ))
        log_activity(
            project, scm,
            f'SCM created Delivery Challan {challan.dc_number} for '
            f'{vendor.name if vendor else "Unknown Vendor"}',
            entity_type='DeliveryChallan', entity_id=challan.pk,
        )
        return challan

    # ------------------------------------------------------------ task states
    def _vary_task_statuses(self, manifest, sites, users):
        """Move some tasks off Not Started so dashboards, counters and progress bars
        have something to show.

        NO PRODUCT PATH — `_apply_task_status_change()` takes a `request` and writes
        `messages`, so it cannot be called here. Its field writes are replicated
        exactly: `completed_at` on Done, `blocked_since` on a fresh Block, the
        StatusTransition on every move, and the blocking Issue that a Block always
        raises alongside.

        MIRRORS ARE NEVER TOUCHED. `is_mirror=False` is in the queryset, not a comment.
        A mirror reports the status of another object and no human may write it (R-18,
        R-20); a seed that moved one would be manufacturing the exact state the refusal
        exists to prevent, and would then be cited as proof the workflow works.

        A due date is set before anything goes In Progress, because the real path
        refuses that move without one — the same order a PM does it in.
        """
        from projects.models import (Issue, REASON_BLOCKED, REASON_CREATED, Task,
                                     log_activity)
        from projects.utils import record_transition

        pm    = users['demo.pm']
        today = timezone.localdate()

        # site_code -> [(status, how many)], applied to non-mirror tasks in task order.
        plan = {
            ACTIVATED_SITE_CODES[0]: [(Task.DONE, 3), (Task.IN_PROGRESS, 2),
                                      (Task.BLOCKED, 1)],
            ACTIVATED_SITE_CODES[1]: [(Task.DONE, 1), (Task.IN_PROGRESS, 1)],
        }

        for site_code, steps in plan.items():
            site  = sites[site_code]
            tasks = list(Task.objects.filter(phase__project=site, is_mirror=False)
                         .order_by('phase__phase_order', 'task_order', 'pk'))
            cursor = 0
            for new_status, count in steps:
                for _ in range(count):
                    task = tasks[cursor]
                    cursor += 1
                    previous = task.status

                    updates = {'status': new_status}
                    if new_status == Task.DONE:
                        updates['completed_at'] = timezone.now()
                    if new_status == Task.BLOCKED:
                        updates['blocked_since'] = timezone.now()
                    if new_status in (Task.IN_PROGRESS, Task.DONE) and not task.due_date:
                        # What task_set_due_date writes. In Progress is refused without
                        # one by the real path, so the demo data must carry it.
                        updates['due_date'] = today + timedelta(days=7)

                    Task.objects.filter(pk=task.pk).update(**updates)
                    record_transition(
                        task, to_status=new_status, from_status=previous, actor=pm,
                        reason_code=REASON_BLOCKED if new_status == Task.BLOCKED else '',
                        remark=(f'Demo blocker on {task.task_name}'
                                if new_status == Task.BLOCKED else ''),
                    )

                    if new_status == Task.BLOCKED:
                        issue = Issue.objects.create(
                            project=site, task=task,
                            title=f'{DEMO_PREFIX} blocker: {task.task_name}',
                            description='Demo blocking issue — local only.',
                            severity=Issue.HIGH, status=Issue.OPEN,
                            raised_by=pm, assigned_to=users['demo.se'],
                        )
                        manifest.add(issue)
                        record_transition(issue, to_status=Issue.OPEN, actor=pm,
                                          reason_code=REASON_CREATED,
                                          remark=issue.title)
                        log_activity(
                            site, pm,
                            f"Blocked task '{task.task_name}' — issue: {issue.title}",
                            entity_type='Issue', entity_id=issue.pk,
                            action_code='issue_created')
                    else:
                        log_activity(
                            site, pm,
                            f'Changed task status to {new_status}: {task.task_name}',
                            entity_type='Task', entity_id=task.pk,
                            action_code=f"task_status_"
                                        f"{new_status.lower().replace(' ', '_')}")

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
    def _report(self, manifest, users, warehouses, program, sites, residential,
                groups, swept, written, notif_before):
        from projects.models import Task

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS('=' * 72))
        self.stdout.write(self.style.SUCCESS('Demo environment created.'))
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
        self.stdout.write(f'OPEX tender  : {program.name!r} '
                          f'({program.short_tender_code}) — {len(sites)} sites')
        for site_code in [code for code, _c, _k in OPEX_SITES]:
            site = sites[site_code]
            total   = Task.objects.filter(phase__project=site).count()
            mirrors = Task.objects.filter(phase__project=site, is_mirror=True).count()
            phases  = site.phases.count()
            detail  = (f'{phases} phases / {total} tasks / {mirrors} mirrors'
                       if total else 'no phases or tasks yet')
            self.stdout.write(f'  {site.project_id:<12} {site.status:<8} {detail}')
        self.stdout.write(
            f'Residential  : {residential.project_id} {residential.status} — '
            f'{residential.phases.count()} phases / '
            f'{Task.objects.filter(phase__project=residential).count()} tasks')
        self.stdout.write(f'Warehouses   : ' + ', '.join(w.code for w in warehouses))
        self.stdout.write(f'Site groups  : '
                          f'{groups["procurement"].name!r} (procurement), '
                          f'{groups["execution"].name!r} (execution)')
        self.stdout.write(f'NotificationLog unchanged at {notif_before}.')

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS('Demo credentials — all seven accounts '
                                             f'share the password below.'))
        self.stdout.write(f'  password: {DEMO_PASSWORD}')
        for username, _f, _l, role, _p, flags in DEMO_USERS:
            extra = f'   flags: {", ".join(flags)}' if flags else ''
            self.stdout.write(f'  {username:<14} {role:<20} '
                              f'{username}@{DEMO_EMAIL_DOMAIN}{extra}')
        self.stdout.write('')
        self.stdout.write('  There is NO demo Admin — UserCreateForm permits only one '
                          'Admin account and')
        self.stdout.write('  this database already has one. Log in as that account for '
                          'Admin screens.')

        self.stdout.write('')
        self.stdout.write('Remove all of it with:')
        self.stdout.write(f'  python manage.py teardown_opex_test_data --confirm'
                          + ('' if str(written) == str(DEFAULT_MANIFEST_PATH)
                             else f' --manifest "{written}"'))
