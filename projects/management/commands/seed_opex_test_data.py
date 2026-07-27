"""
Management command: create one small OPEX tender with five sites, as test data for
building and verifying the OPEX design module.

There are no OPEX projects in production, so every part of the design module from
Part 2 onward is built against data that does not otherwise exist. This command
creates that data; `teardown_opex_test_data` removes it completely.

WHY THE `Test-` PREFIX IS LITERAL
---------------------------------
Every identifier this command writes starts with the literal string `Test-`, so the
rows are unmistakable in any list, dashboard or export — and so the teardown can
identify them by prefix and refuse to touch anything else.

That means this command sets `Project.project_id` EXPLICITLY rather than composing it
through OpexSiteForm. The form path runs both halves of the ID through
`normalize_program_code()`, which strips everything outside [A-Z0-9]: `Test-Site-01`
would become `TESTSITE01` and the composed ID `TESTOPEX-TESTSITE01`, which does not
start with `Test-` and would leave the teardown's safety guard matching nothing.
Setting `project_id` before save() is the same technique `views.create_opex_site()`
already uses, and it bypasses `generate_project_id()` entirely.

WHAT THIS COMMAND DELIBERATELY DOES NOT DO
------------------------------------------
No design workflow state is seeded — no DesignAssignment, attempt, Arka submission or
file. Sites are created at the very start of the flow because Part 2 onward creates
those rows through the UI, and the UI is what needs testing. No BOQ rows either, and
`is_deleted` is never set.

NOTIFICATIONS
-------------
Nothing here can send one: `projects/signals.py` registers no receiver on Project, and
`send_notification` is never called from models.py or signals.py — notifications are
entirely view-layer, and this command does not go through a view. As a guard rather
than an assumption, the NotificationLog row count is captured before and compared
after; any change aborts the whole transaction.
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

# ---------------------------------------------------------------------------
# Shared identity of the seeded data. teardown_opex_test_data imports these, so the
# prefix its safety guard enforces is the exact same string this command writes —
# they cannot drift apart.
# ---------------------------------------------------------------------------
TEST_PREFIX       = 'Test-'
PROGRAM_NAME      = 'Test-Opex'
PROGRAM_CLIENT    = 'Test-Opex Client'
TENDER_CODE       = 'TESTOPEX'      # normalized form; not an identifier the teardown matches on
SITE_COUNT        = 5
SITE_ID_TEMPLATE  = 'Test-Site-%02d'

# Usernames, not first names and never primary keys. Username is unique, stable, and
# present in both the local and production databases; `first_name` is not reliably
# populated in production, which is exactly what made the first lookup attempt fail.
DESIGN_HEAD_USERNAME = 'praveen'
DESIGNER_USERNAMES   = ['priyanka', 'shyam']
# Sites 1-3 to the first designer, 4-5 to the second.
DESIGNER_SPLIT = {1: 0, 2: 0, 3: 0, 4: 1, 5: 1}


class Command(BaseCommand):
    help = 'Create one Test- prefixed OPEX tender with five sites (idempotent).'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Print exactly what would be created and create nothing.',
        )

    def handle(self, *args, **options):
        from projects.models import Program, Project, UserProfile, NotificationLog

        dry_run = options['dry_run']

        # ---- resolve users by database lookup (no hardcoded PKs) ----
        def resolve(username, expect_design_head=None):
            try:
                profile = UserProfile.objects.select_related('user').get(user__username=username)
            except UserProfile.DoesNotExist:
                raise CommandError(
                    f"User '{username}' not found. This command resolves real users at "
                    f"runtime and will not invent test accounts."
                )
            except UserProfile.MultipleObjectsReturned:
                raise CommandError(f"More than one UserProfile for username '{username}'.")
            if profile.role != 'Design':
                raise CommandError(
                    f"User '{username}' has role {profile.role!r}, expected 'Design'. "
                    f"Refusing to proceed — this command never changes a user's role."
                )
            if expect_design_head and not profile.is_design_head:
                raise CommandError(
                    f"User '{username}' is expected to be the Design Head but "
                    f"is_design_head is False. Refusing to proceed."
                )
            return profile

        design_head = resolve(DESIGN_HEAD_USERNAME, expect_design_head=True)
        designers   = [resolve(u) for u in DESIGNER_USERNAMES]

        # Deterministic PM choice so repeated runs and both environments agree: the
        # lowest-username active PM. Reported below rather than assumed.
        pm = (UserProfile.objects
              .select_related('user')
              .filter(role='PM', is_active=True)
              .order_by('user__username')
              .first())
        if pm is None:
            raise CommandError('No active PM found to assign these sites to.')

        self.stdout.write('Resolved users:')
        self.stdout.write(f'  Design Head : {design_head.user.username} '
                          f'({design_head.user.get_full_name()}) pk={design_head.pk} '
                          f'is_design_head={design_head.is_design_head}')
        for d in designers:
            self.stdout.write(f'  Designer    : {d.user.username} '
                              f'({d.user.get_full_name()}) pk={d.pk}')
        self.stdout.write(f'  PM          : {pm.user.username} '
                          f'({pm.user.get_full_name()}) pk={pm.pk}   [chosen: '
                          f'lowest-username active PM]')
        self.stdout.write('')

        # ---- idempotency: refuse to double-seed ----
        existing_program = Program.objects.filter(name=PROGRAM_NAME).first()
        existing_sites   = Project.objects.filter(project_id__startswith=TEST_PREFIX)
        if existing_program or existing_sites.exists():
            self.stdout.write(self.style.WARNING(
                'Seed data already present — nothing created.'))
            if existing_program:
                self.stdout.write(f"  Program pk={existing_program.pk} name={existing_program.name!r}")
            for p in existing_sites.order_by('project_id'):
                self.stdout.write(f"  Project pk={p.pk} project_id={p.project_id!r}")
            self.stdout.write('Run teardown_opex_test_data --confirm first if you want a clean re-seed.')
            return

        # ---- build the plan ----
        plan = []
        for n in range(1, SITE_COUNT + 1):
            designer = designers[DESIGNER_SPLIT[n]]
            plan.append({
                'project_id':   SITE_ID_TEMPLATE % n,
                'site_code':    'SITE%02d' % n,
                'designer':     designer,
                'contact':      f'Test Site In-Charge {n:02d}',
                'phone':        '99999%05d' % n,
                'city':         'Delhi',
                'capacity_kw':  100 + n,
            })

        if dry_run:
            self.stdout.write(self.style.WARNING('DRY RUN — nothing will be created.'))
            self.stdout.write(f"WOULD CREATE Program  name={PROGRAM_NAME!r} "
                              f"type=OPEX client={PROGRAM_CLIENT!r} "
                              f"short_tender_code={TENDER_CODE!r} planned_site_count={SITE_COUNT}")
            for row in plan:
                self.stdout.write(
                    f"WOULD CREATE Project  project_id={row['project_id']!r} "
                    f"site_code={row['site_code']!r} type=OPEX status='Draft' "
                    f"assigned_design={row['designer'].user.username} "
                    f"assigned_pm={pm.user.username}")
            self.stdout.write('')
            self.stdout.write(f'Totals: 1 Program, {len(plan)} Projects. '
                              f'No design workflow rows, no BOQ rows, no notifications.')
            return

        notif_before = NotificationLog.objects.count()

        with transaction.atomic():
            program = Program.objects.create(
                program_type='OPEX',
                name=PROGRAM_NAME,
                client_name=PROGRAM_CLIENT,
                status='Active',
                short_tender_code=TENDER_CODE,
                planned_site_count=SITE_COUNT,
            )
            self.stdout.write(self.style.SUCCESS(
                f'CREATED Program  pk={program.pk} name={program.name!r} '
                f'code={program.short_tender_code!r}'))

            created = []
            for row in plan:
                site = Project(
                    project_id   = row['project_id'],     # explicit -> skips generate_project_id()
                    customer_name= PROGRAM_CLIENT,        # OPEX freezes the parent client name
                    customer_phone= row['phone'],
                    customer_contact_person = row['contact'],   # reused as Site In-Charge for OPEX
                    site_address = f"{row['project_id']}, test address",
                    city         = row['city'],
                    state        = 'Delhi',
                    project_type = 'OPEX',
                    program      = program,
                    site_code    = row['site_code'],
                    capacity_kw  = row['capacity_kw'],
                    status       = 'Draft',
                    assigned_pm  = pm,
                    assigned_design = row['designer'],
                )
                site.save()
                created.append(site)
                self.stdout.write(self.style.SUCCESS(
                    f"CREATED Project  pk={site.pk} project_id={site.project_id!r} "
                    f"site_code={site.site_code!r} design={row['designer'].user.username} "
                    f"pm={pm.user.username}"))

            # Guard, not decoration: if anything unexpectedly emitted a notification,
            # abort so no real user is left having been messaged about test sites.
            notif_after = NotificationLog.objects.count()
            if notif_after != notif_before:
                raise CommandError(
                    f'NotificationLog grew from {notif_before} to {notif_after} during '
                    f'seeding — aborting and rolling back.'
                )

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            f'Done: 1 Program + {len(created)} Projects created. '
            f'NotificationLog unchanged at {notif_before}.'))
