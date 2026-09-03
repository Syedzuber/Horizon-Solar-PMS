"""
Management command: build the released/grouped/locked states the Part 6 SCM handoff
screens need, on top of the demo environment `seed_opex_test_data` creates.

    python manage.py seed_opex_test_data              # first — users and the base tender
    python manage.py seed_scm_handoff_data --confirm  # then this
    python manage.py teardown_opex_test_data --confirm

TYPE THE COMMAND NAMES IN FULL. `se`+Tab does not disambiguate any of the three seed
commands from `send_eod_digest`, which mails the whole company.

WHY THIS IS SEPARATE FROM seed_opex_test_data
---------------------------------------------
The base seed puts sites at the START of the design flow, deliberately, because
Parts 2-5 are exercised by driving the UI forward from there. The SCM handoff is at
the OTHER end: it only has anything to show once sites are RELEASED, grouped and
locked. Driving six sites through survey → allocation → due date → Arka → CAD → BOQ →
QC by hand just to see a group screen is hours of clicking, so this command writes
that end state directly.

WHAT CHANGED
------------
Three things, all consequences of the manifest:

  1. IT WRITES TO THE SHARED MANIFEST. Every row is recorded into the same file the
     base seed wrote, so `teardown_opex_test_data` removes this command's ~390 rows
     along with everything else. Leaving them outside it would have made the manifest
     discipline decorative, which is the whole reason the existing commands were
     extended rather than a second pair added.
  2. `--reset` IS GONE. It was a second teardown with its own idea of what this
     command had created — the duplication the manifest exists to end. There is now
     one teardown, and it reads the manifest.
  3. IT OWNS ITS SITES. It used to release and group sites belonging to the base
     tender. The base seed now releases and groups one of those itself, and a site may
     hold only ONE active membership per group_type (`uniq_active_site_group_membership
     _per_type`), so the two commands would have collided. This one creates its own
     tender and its own six sites, and every screen state it existed to show survives
     unchanged.

WHAT IT BUILDS — everything the group screens can display, at once
-----------------------------------------------------------------
  * six released sites, staggered 2 to 31 days ago, so the pool ageing column spans
    fresh to badly overdue (the screen reddens anything >= 14 days)
  * one LOCKED group (two sites) and one DRAFT group (one site)
  * three sites in neither, so the pool is not empty
  * two historical removals, one of them a PM change request, which renders its own
    red chip
  * one ad-hoc BOQ row with no `item_master`, so the "could not be aggregated" warning
    on the group BOQ screen is visible rather than theoretical. That state is real —
    `boq_detail`'s add_item branch creates exactly it (deferred finding B4/J7) — and it
    is the one aggregation edge case with no other way to see.

SAFETY
------
Local-only, through the same interlock as the other two commands. Users are the demo
accounts, not real ones, so no production user's name appears on a demo group. It
NEVER touches a real tender, a Residential project, any user's role or flag, or
SystemSettings. Like its siblings it captures the NotificationLog count before and
after and aborts the whole transaction if anything sent a notification.
"""
from datetime import timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from projects.management.commands._demo_support import (
    DEMO_EMAIL_DOMAIN, DEMO_PREFIX, DEFAULT_MANIFEST_PATH, Manifest,
    MANIFEST_MISSING_MESSAGE, OVERRIDE_FLAG, database_host, database_name,
    high_water, print_db_banner, require_local_database,
)
from projects.management.commands.seed_opex_test_data import (
    PROGRAM_NAME as BASE_PROGRAM_NAME,
)

# ---------------------------------------------------------------------------
# Identity of this command's own tender. DEMO-namespaced like everything else, and
# with no hyphen in the site codes because OpexSiteForm strips them.
# ---------------------------------------------------------------------------
P2_PROGRAM_NAME = f'{DEMO_PREFIX} SCM Handoff Pile-up'
P2_CLIENT       = f'{DEMO_PREFIX} Client (Local Only)'
P2_TENDER_CODE  = 'DEMOSCM'
P2_SITE_CODE    = 'DEMOSCM%02d'
P2_SITE_COUNT   = 6

# Site n -> days since release. Chosen to straddle the 14-day threshold the pool screen
# reddens at, so the ageing column shows fresh, borderline and badly overdue together.
P2_RELEASE_AGE_DAYS = {1: 2, 2: 6, 3: 11, 4: 17, 5: 24, 6: 31}

GROUP_LOCKED_NAME = f'{DEMO_PREFIX} Batch A — Modules & Structure'
GROUP_DRAFT_NAME  = f'{DEMO_PREFIX} Batch B — BOS & Cabling'

LOCKED_MEMBERS = ['DEMOSCM01', 'DEMOSCM02']
DRAFT_MEMBERS  = ['DEMOSCM03']
#: Left in neither group, so the pool screen has rows and an ageing column worth reading.
POOL_MEMBERS   = ['DEMOSCM04', 'DEMOSCM05', 'DEMOSCM06']

# BOQ quantity profile, by catalogue code. Deliberately overlapping-but-different per
# site so the aggregate is worth reading and can be checked by hand against the
# per-line contributing-sites column.
QTY_PROFILE = {
    'DEMOSCM01': {'ITM-001': 9,  'ITM-002': 18, 'ITM-003': 20, 'ITM-005': 2, 'ITM-008': 150},
    'DEMOSCM02': {'ITM-001': 6,  'ITM-002': 12, 'ITM-005': 1, 'ITM-008': 100},
    'DEMOSCM03': {'ITM-001': 11, 'ITM-002': 22, 'ITM-003': 25, 'ITM-008': 180},
    'DEMOSCM04': {'ITM-001': 3,  'ITM-002': 6,  'ITM-005': 1, 'ITM-008': 60},
    'DEMOSCM05': {'ITM-001': 8,  'ITM-002': 16, 'ITM-003': 18, 'ITM-005': 1},
    'DEMOSCM06': {'ITM-001': 5,  'ITM-002': 10, 'ITM-008': 85},
}

# The unlinked row — a quantity with no catalogue entry to sum it against.
ADHOC_SITE        = 'DEMOSCM03'
ADHOC_DESCRIPTION = 'Site-specific cable tray fabrication (ad-hoc, no catalogue entry)'
ADHOC_QTY         = Decimal('35.00')

WRITES = [
    f'1 OPEX Program ({P2_PROGRAM_NAME!r}) with {P2_SITE_COUNT} sites',
    f'{P2_SITE_COUNT} released DesignAssignments, attempts and Arka submissions',
    f'{P2_SITE_COUNT} BOQs with quantities, plus 1 ad-hoc row with no item_master',
    '2 SiteGroups (one locked, one draft) and 5 memberships, 2 of them removed',
]


class Command(BaseCommand):
    help = ('Build the released/grouped/locked states the Part 6 SCM handoff screens '
            'need. Requires seed_opex_test_data to have run. Dry run unless --confirm.')

    def add_arguments(self, parser):
        parser.add_argument('--confirm', action='store_true',
                            help='Actually write. Without this the command only reports.')
        parser.add_argument('--manifest', type=str, default='',
                            help=f'Manifest to append to. Default: {DEFAULT_MANIFEST_PATH}')
        parser.add_argument(OVERRIDE_FLAG, action='store_true',
                            help='Required to run against a non-local database.')

    # ------------------------------------------------------------------ helpers
    def _demo_user(self, username, expect_role):
        from projects.models import UserProfile
        try:
            profile = UserProfile.objects.select_related('user').get(
                user__username=username)
        except UserProfile.DoesNotExist:
            raise CommandError(
                f"Demo user {username!r} not found. Run "
                f"`python manage.py seed_opex_test_data` first — this command uses the "
                f"demo accounts and will not invent them or borrow a real one.")
        if profile.role != expect_role:
            raise CommandError(
                f"Demo user {username!r} has role {profile.role!r}, expected "
                f"{expect_role!r}. Refusing to proceed.")
        return profile

    def _ensure_boq(self, manifest, site, codes_to_qty):
        """Create the site's BOQ from the catalogue, then set quantities.

        Mirrors what `boq_detail` does on first GET for an authorised designer: seed
        every active BOQItemMaster row, carrying `item_master` so the quantities can
        aggregate.

        PART 11 — the masters lookup is scoped to Residential for the same reason
        boq_detail's is: three descriptions now exist in both catalogues, and unscoped
        the OPEX row would win the key. This command seeds ITM- codes, so scoping keeps
        it producing what it always produced. That it seeds a RESIDENTIAL template onto
        OPEX sites is a pre-existing mismatch with Part 11's picker, recorded in
        DESIGN_MODULE_DEFERRED.md rather than changed here.
        """
        from projects.models import BOQ, BOQItem, BOQItemMaster, get_standard_boq_items

        boq = BOQ.objects.create(project=site)
        manifest.add(boq)
        masters = {m.description: m
                   for m in BOQItemMaster.objects.filter(is_active=True,
                                                         project_type='Residential')}
        BOQItem.objects.bulk_create([
            BOQItem(boq=boq, item_master=masters.get(d['description']), **d)
            for d in get_standard_boq_items()
        ])
        manifest.add_all(boq.items.order_by('pk'))

        by_code = {i.item_master.code: i
                   for i in boq.items.select_related('item_master')
                   if i.item_master_id}
        for code, qty in codes_to_qty.items():
            item = by_code.get(code)
            if item is None:
                raise CommandError(
                    f'{site.project_id}: catalogue code {code!r} not found on the BOQ. '
                    f'Has BOQItemMaster been seeded (migration 0047)?')
            item.boq_quantity = Decimal(qty)
            item.save(update_fields=['boq_quantity'])
        return boq

    def _release(self, manifest, site, designer, head, released_at, capacity_kw):
        """Put one site into the exact state a QC pass leaves it in.

        NO PRODUCT PATH — every design transition lives inside a view. Writes the
        assignment, one closed attempt with a passed QC verdict, and the approved Arka
        that attempt derives from, so a site that shows as released on the SCM screens
        also reads correctly if the user clicks through to the design screens.

        `survey_file_path` is left EMPTY on purpose: a fake path would make the teardown
        attempt a Supabase delete for an object that never existed. The SCM handoff
        screens never read it.
        """
        from projects.models import (ARKA_APPROVED, ATTEMPT_REASON_INITIAL,
                                     ArkaSubmission, DESIGN_RELEASED, DesignAssignment,
                                     DesignAttempt, QC_PASSED)

        assignment = DesignAssignment.objects.create(
            project=site, assigned_to=designer, assigned_by=head,
            assigned_at=released_at - timedelta(days=20), status=DESIGN_RELEASED,
            released_at=released_at, released_by=head, current_attempt_number=1,
        )
        manifest.add(assignment)

        attempt = DesignAttempt.objects.create(
            assignment=assignment, attempt_number=1,
            opened_reason=ATTEMPT_REASON_INITIAL,
            qc_started_at=released_at - timedelta(days=1), qc_verdict=QC_PASSED,
            qc_reviewed_by=head, qc_reviewed_at=released_at,
            boq_submitted_at=released_at - timedelta(days=2), boq_submitted_by=designer,
            closed_at=released_at,
        )
        manifest.add(attempt)
        # opened_at is auto_now_add — backdate it separately so the attempt does not
        # claim to have opened after the QC that closed it.
        DesignAttempt.objects.filter(pk=attempt.pk).update(
            opened_at=released_at - timedelta(days=18))

        manifest.add(ArkaSubmission.objects.create(
            attempt=attempt, version=1, capacity_kw=Decimal(capacity_kw),
            arka_link='https://example.invalid/arka/demo-seed', submitted_by=designer,
            verdict=ARKA_APPROVED, reviewed_by=head,
            reviewed_at=released_at - timedelta(days=5), is_current=True,
        ))
        return assignment

    # ------------------------------------------------------------------ main
    def handle(self, *args, **options):
        from projects.design_views import CHANGE_REQUEST_REMOVAL_REASON
        from projects.forms import ProgramForm
        from projects.models import (BOQItem, GROUP_TYPE_PROCUREMENT, NotificationLog,
                                     Program, SITE_GROUP_DRAFT, SITE_GROUP_LOCKED,
                                     SiteGroup, SiteGroupMembership, log_activity)
        from projects.views import create_opex_site

        print_db_banner(self)
        require_local_database(
            self, options.get('i_know_this_is_not_local', False), WRITES)

        confirm       = options['confirm']
        manifest_path = options['manifest'].strip() or DEFAULT_MANIFEST_PATH
        now           = timezone.now()

        designer_a = self._demo_user('demo.design', 'Design')
        designer_b = designer_a          # one demo Design account; both slots use it
        head       = designer_a
        scm        = self._demo_user('demo.scm', 'SCM')
        pm         = self._demo_user('demo.pm',  'PM')

        base = Program.objects.filter(name=BASE_PROGRAM_NAME).first()
        if base is None:
            raise CommandError(
                f"Program {BASE_PROGRAM_NAME!r} not found. Run "
                f"`python manage.py seed_opex_test_data` first.")

        self.stdout.write('Resolved demo users:')
        for label, p in (('Design', designer_a), ('SCM', scm), ('PM', pm)):
            self.stdout.write(f'  {label:<8} {p.user.username} (pk={p.pk})')
        self.stdout.write(f'  Base tender  {base.name!r} pk={base.pk}')
        self.stdout.write('')

        if not confirm:
            self.stdout.write(self.style.WARNING('DRY RUN — nothing will be written.'))
            for line in WRITES:
                self.stdout.write(f'  WOULD CREATE  {line}')
            self.stdout.write('')
            self.stdout.write(f'  Would append to manifest: {manifest_path}')
            self.stdout.write('  Re-run with --confirm to write.')
            return

        # The manifest MUST already exist: this command layers onto the base seed's
        # tender and its rows have to land in the same file, or the teardown would
        # delete half a demo environment and leave the rest.
        manifest = Manifest.load(manifest_path)
        if manifest is None:
            raise CommandError(MANIFEST_MISSING_MESSAGE.format(path=manifest_path))

        if Program.objects.filter(name=P2_PROGRAM_NAME).exists():
            raise CommandError(
                f'Program {P2_PROGRAM_NAME!r} already exists — this command has already '
                f'run. Tear the demo environment down and re-seed for a clean rebuild.')

        notif_before = NotificationLog.objects.count()

        # High-water marks for the two log tables, taken BEFORE a single write. The
        # sweep at the end records rows above these marks and nothing else — passing a
        # mark of 0 would sweep every ActivityLog and StatusTransition row in the
        # database into the manifest, and the teardown would then delete the lot.
        from projects.models import ActivityLog, StatusTransition
        log_marks = {ActivityLog: high_water(ActivityLog),
                     StatusTransition: high_water(StatusTransition)}

        with transaction.atomic():
            # ---- 1. the tender, through ProgramForm ----
            form = ProgramForm({
                'program_type': 'OPEX', 'name': P2_PROGRAM_NAME,
                'client_name': P2_CLIENT, 'status': 'Active',
                'short_tender_code': P2_TENDER_CODE,
                'planned_site_count': P2_SITE_COUNT,
            })
            if not form.is_valid():
                raise CommandError(f'ProgramForm refused the handoff tender: '
                                   f'{form.errors.as_json()}')
            p2 = form.save(commit=False)
            p2.created_by = pm.user
            p2.save()
            manifest.add(p2)
            log_activity(None, pm, f'Created OPEX Program: {p2.name}',
                         entity_type='Program', entity_id=p2.pk,
                         action_code='program_created')

            # ---- 2. six sites, released at staggered ages ----
            self.stdout.write(f'{P2_PROGRAM_NAME} — the post-QC pile-up:')
            sites = {}
            for n, age in sorted(P2_RELEASE_AGE_DAYS.items()):
                site_code = P2_SITE_CODE % n
                designer  = designer_a if n % 2 else designer_b
                site, site_form = create_opex_site(
                    p2,
                    {
                        'site_code': site_code,
                        'customer_contact_person': f'Handoff Site In-Charge {n:02d}',
                        'customer_phone': '98888000%02d' % n,
                        'customer_email': f'handoff{n:02d}@{DEMO_EMAIL_DOMAIN}',
                        'site_address': f'{site_code}, demo address (local only)',
                        'city': 'Gurugram', 'state': 'Demo State',
                        'capacity_kw': str(200 + n),
                    },
                    creator=pm.user, profile=pm,
                )
                if site is None:
                    raise CommandError(f'create_opex_site() refused {site_code!r}: '
                                       f'{site_form.errors.as_json()}')
                manifest.add(site)
                self._release(manifest, site, designer, head,
                              now - timedelta(days=age), 200 + n)
                self._ensure_boq(manifest, site, QTY_PROFILE[site_code])
                sites[site_code] = site
                self.stdout.write(self.style.SUCCESS(
                    f'  {site_code}: released {age:2d} days ago, designer '
                    f'{designer.user.username}, '
                    f'{len(QTY_PROFILE[site_code])} BOQ quantities set'))

            # ---- 3. the ad-hoc, unlinked BOQ row ----
            adhoc_site  = sites[ADHOC_SITE]
            last_serial = max(i.serial_no for i in adhoc_site.boq.items.all())
            manifest.add(BOQItem.objects.create(
                boq=adhoc_site.boq, serial_no=last_serial + 1, category='Other',
                description=ADHOC_DESCRIPTION, uom='LOT',
                boq_quantity=ADHOC_QTY, is_standard_item=False,   # item_master stays NULL
            ))
            self.stdout.write(self.style.SUCCESS(
                f'  {ADHOC_SITE}: 1 ad-hoc BOQ row with no item_master '
                f'(qty {ADHOC_QTY}) -> drives the aggregation warning'))

            # ---- 4. the locked group ----
            # PROCUREMENT is stated rather than inherited from the model default, the
            # same way site_group_create states it: a lock freezes member BOQs because a
            # purchase order is about to be raised, which is a procurement act. An
            # execution group cannot be locked at all — CHECK constraint
            # `execution_groups_are_never_locked`.
            self.stdout.write('')
            self.stdout.write('Groups:')
            member_mark = high_water(SiteGroupMembership)

            locked = SiteGroup.objects.create(
                program=p2, name=GROUP_LOCKED_NAME, status=SITE_GROUP_LOCKED,
                group_type=GROUP_TYPE_PROCUREMENT, created_by=scm, locked_by=scm,
                locked_at=now - timedelta(days=4),
                notes='First procurement batch — modules, transport and structure.',
            )
            manifest.add(locked)
            SiteGroup.objects.filter(pk=locked.pk).update(
                created_at=now - timedelta(days=11))
            for site_code in LOCKED_MEMBERS:
                m = SiteGroupMembership.objects.create(
                    group=locked, project=sites[site_code], added_by=scm)
                SiteGroupMembership.objects.filter(pk=m.pk).update(
                    added_at=now - timedelta(days=11))
            self.stdout.write(self.style.SUCCESS(
                f'  LOCKED {GROUP_LOCKED_NAME!r}: {", ".join(LOCKED_MEMBERS)} '
                f'(locked 4 days ago by {scm.user.username})'))

            # ---- 5. the draft group ----
            draft = SiteGroup.objects.create(
                program=p2, name=GROUP_DRAFT_NAME, status=SITE_GROUP_DRAFT,
                group_type=GROUP_TYPE_PROCUREMENT, created_by=scm,
                notes='Second batch — cables, BOS and the items still being quoted.',
            )
            manifest.add(draft)
            SiteGroup.objects.filter(pk=draft.pk).update(
                created_at=now - timedelta(days=3))
            for site_code in DRAFT_MEMBERS:
                m = SiteGroupMembership.objects.create(
                    group=draft, project=sites[site_code], added_by=scm)
                SiteGroupMembership.objects.filter(pk=m.pk).update(
                    added_at=now - timedelta(days=3))
            self.stdout.write(self.style.SUCCESS(
                f'  DRAFT  {GROUP_DRAFT_NAME!r}: {", ".join(DRAFT_MEMBERS)}'))

            # ---- 6. two historical removals ----
            # `removed_at` is set IN THE INSERT, not by a follow-up update. The partial
            # unique constraint counts any row with removed_at IS NULL as active, so
            # creating these blank-then-updating would momentarily give the site two live
            # memberships of the same group_type and trip
            # uniq_active_site_group_membership_per_type. Only `added_at` is backdated
            # afterwards, because it is auto_now_add and cannot be set here.
            #
            # (a) SCM moved a site to a later batch — the ordinary case.
            r1 = SiteGroupMembership.objects.create(
                group=locked, project=sites[DRAFT_MEMBERS[0]], added_by=scm,
                removed_by=scm, removed_at=now - timedelta(days=6),
                removal_reason='Lead time on cable drums — moved to the next batch',
            )
            SiteGroupMembership.objects.filter(pk=r1.pk).update(
                added_at=now - timedelta(days=11))

            # (b) a PM change request pulled a site out (Part 6 §4). The reason string
            #     must match CHANGE_REQUEST_REMOVAL_REASON exactly or the group screen
            #     renders it as ordinary text instead of the red chip.
            r2 = SiteGroupMembership.objects.create(
                group=draft, project=sites[POOL_MEMBERS[0]], added_by=scm,
                removed_by=pm, removed_at=now - timedelta(days=1),
                removal_reason=CHANGE_REQUEST_REMOVAL_REASON,
            )
            SiteGroupMembership.objects.filter(pk=r2.pk).update(
                added_at=now - timedelta(days=3))
            self.stdout.write(self.style.SUCCESS(
                f'  2 removed memberships recorded ({DRAFT_MEMBERS[0]} from Batch A; '
                f'{POOL_MEMBERS[0]} from Batch B as {CHANGE_REQUEST_REMOVAL_REASON!r})'))

            manifest.add_new_since(SiteGroupMembership, member_mark)

            # ---- 7. everything the real paths wrote on the way past ----
            # Same leak sweep the base seed runs, over the two log tables this command
            # can reach. Appended last, so the teardown deletes them first.
            self._sweep_logs(manifest, log_marks)

            # ---- guard: nothing here may notify a real user ----
            notif_after = NotificationLog.objects.count()
            if notif_after != notif_before:
                raise CommandError(
                    f'NotificationLog grew from {notif_before} to {notif_after} during '
                    f'seeding — aborting and rolling back.')

        written = manifest.save(database_host(), database_name())

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS('=' * 72))
        self.stdout.write(self.style.SUCCESS(
            f'Done. NotificationLog unchanged at {notif_before}.'))
        self.stdout.write(self.style.SUCCESS('=' * 72))
        self.stdout.write('')
        self.stdout.write(f'Manifest now holds {len(manifest.entries)} rows: {written}')
        self.stdout.write('')
        self.stdout.write('Log in as  demo.scm  to see every screen below.')
        self.stdout.write('')
        self.stdout.write('  /dashboard/scm/')
        self.stdout.write('      OPEX section: the base tender and this one, side by side.')
        self.stdout.write(f'  /programs/{p2.pk}/site-groups/')
        self.stdout.write('      Both groups listed, and 3 sites in the pool oldest-first')
        self.stdout.write('      with the >=14 day ages in red.')
        self.stdout.write(f'  /site-groups/{locked.pk}/')
        self.stdout.write('      LOCKED group — aggregated BOQ, lock banner, no lock button,')
        self.stdout.write('      one site that left with a reason.')
        self.stdout.write(f'  /site-groups/{draft.pk}/')
        self.stdout.write('      DRAFT group — aggregate, the unaggregated-rows WARNING,')
        self.stdout.write('      add/remove controls, the lock button, and the red')
        self.stdout.write('      "PM change request" departure chip.')
        self.stdout.write(f'  /projects/{LOCKED_MEMBERS[0]}/boq/')
        self.stdout.write('      A LOCKED member site: banner shown, quantities read-only.')
        self.stdout.write(f'  /projects/{DRAFT_MEMBERS[0]}/boq/')
        self.stdout.write('      A draft-group site: still fully editable by its designer.')
        self.stdout.write('')
        self.stdout.write('Remove everything with:')
        self.stdout.write('  python manage.py teardown_opex_test_data --confirm')

    # ------------------------------------------------------------------ sweep
    @staticmethod
    def _sweep_logs(manifest, log_marks):
        """Record the ActivityLog and StatusTransition rows the real paths wrote.

        `log_marks` are the pk high-water marks taken before any write. THEY ARE THE
        SAFETY PROPERTY, not an optimisation: a mark of 0 would record every row in
        both tables — thousands of them, belonging to real work — and the teardown,
        which asks no questions about what is in the manifest, would delete them.

        Narrower than the base seed's whole-app sweep because this command's reach is
        narrower: `create_opex_site()` writes one of each per site, and `log_activity`
        writes one for the Program. Both tables are leaves, so recording them last —
        and therefore deleting them first — is always safe.
        """
        for model, mark in log_marks.items():
            manifest.add_new_since(model, mark)
