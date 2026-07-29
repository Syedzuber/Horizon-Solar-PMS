"""
Management command: build the data needed to verify the Part 6 SCM handoff UI.

WHY THIS EXISTS SEPARATELY FROM seed_opex_test_data
---------------------------------------------------
`seed_opex_test_data` creates a tender and five sites at the very START of the design
flow — deliberately, because Parts 2-5 are exercised by driving the UI forward from
there. The SCM handoff (Part 6) is at the OTHER end: it only has anything to show once
sites are RELEASED, grouped, and locked. Driving five sites through survey → allocation
→ due date → Arka → CAD → BOQ → QC by hand just to see a group screen is hours of
clicking, so this command writes that end state directly.

WHAT IT BUILDS — three tender rows on the SCM dashboard, each a different state
------------------------------------------------------------------------------
  Test-Opex          procurement under way — one LOCKED group, one DRAFT group, an
                     empty pool, and two historical removals (one of them a PM change
                     request, which renders its own red chip)
  Test-Opex-Phase2   the pile-up — six released sites, NO groups, release dates
                     staggered from 2 to 31 days ago so the pool ageing column spans
                     fresh to badly overdue (the screen reddens anything >= 14 days)
  Finolex            untouched — design still in flight, so it shows the
                     nothing-released-yet row for comparison

It also leaves ONE ad-hoc BOQ row (no `item_master`) carrying a quantity on a draft-group
site, so the "could not be aggregated" warning on the group BOQ screen is visible rather
than theoretical. That state is real — `boq_detail`'s add_item branch creates exactly it
(deferred finding B4/J7) — and it is the one aggregation edge case with no other way to see.

SAFETY
------
Every row this command writes is under the literal `Test-` prefix, so
`teardown_opex_test_data` removes all of it. It NEVER touches the Finolex tender, any
Residential project, any user's role, flag or phone, or SystemSettings. Like the sibling
seed command it captures the NotificationLog count before and after and aborts the whole
transaction if anything sent a notification.

USAGE
-----
    python manage.py seed_scm_handoff_data                    # dry run — reports, writes nothing
    python manage.py seed_scm_handoff_data --confirm          # create
    python manage.py seed_scm_handoff_data --reset --confirm  # remove what it made, then re-create

`--reset` removes ONLY this command's own output: every SiteGroup / SiteGroupMembership
under a Test- program, the Phase2 tender and its rows, and the ad-hoc BOQ row. It does
not un-release or delete anything `seed_opex_test_data` created.
"""
from datetime import timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from projects.management.commands.seed_opex_test_data import (
    TEST_PREFIX, PROGRAM_NAME as BASE_PROGRAM_NAME,
)

# ---------------------------------------------------------------------------
# Identity of the second tender. Test- prefixed so the existing teardown owns it.
# ---------------------------------------------------------------------------
P2_PROGRAM_NAME = 'Test-Opex-Phase2'
P2_CLIENT       = 'Test-Opex Client'
P2_TENDER_CODE  = 'TESTOPEX2'
P2_SITE_ID      = 'Test-P2-Site-%02d'

# Site n -> days since release. Chosen to straddle the 14-day threshold the pool screen
# reddens at, so the ageing column shows fresh, borderline and badly overdue together.
P2_RELEASE_AGE_DAYS = {1: 2, 2: 6, 3: 11, 4: 17, 5: 24, 6: 31}

GROUP_LOCKED_NAME = 'Batch 1 — Modules & Structure'
GROUP_DRAFT_NAME  = 'Batch 2 — BOS & Cabling'

# BOQ quantity profile, by catalogue code. Deliberately overlapping-but-different per
# site so the aggregate is worth reading and can be checked by hand against the
# per-line contributing-sites column.
QTY_PROFILE = {
    'Test-Site-01':    {'ITM-001': 7,  'ITM-002': 14, 'ITM-005': 1, 'ITM-008': 120},
    'Test-Site-04':    {'ITM-001': 5,  'ITM-002': 10, 'ITM-003': 15, 'ITM-005': 1, 'ITM-008': 90},
    'Test-Site-05':    {'ITM-001': 4,  'ITM-002': 8,  'ITM-003': 12, 'ITM-008': 75},
    'Test-P2-Site-01': {'ITM-001': 9,  'ITM-002': 18, 'ITM-003': 20, 'ITM-005': 2, 'ITM-008': 150},
    'Test-P2-Site-02': {'ITM-001': 6,  'ITM-002': 12, 'ITM-005': 1, 'ITM-008': 100},
    'Test-P2-Site-03': {'ITM-001': 11, 'ITM-002': 22, 'ITM-003': 25, 'ITM-008': 180},
    'Test-P2-Site-04': {'ITM-001': 3,  'ITM-002': 6,  'ITM-005': 1, 'ITM-008': 60},
    'Test-P2-Site-05': {'ITM-001': 8,  'ITM-002': 16, 'ITM-003': 18, 'ITM-005': 1},
    'Test-P2-Site-06': {'ITM-001': 5,  'ITM-002': 10, 'ITM-008': 85},
}

# The unlinked row — a quantity with no catalogue entry to sum it against.
ADHOC_SITE        = 'Test-Site-05'
ADHOC_DESCRIPTION = 'Site-specific cable tray fabrication (ad-hoc, no catalogue entry)'
ADHOC_QTY         = Decimal('35.00')


class Command(BaseCommand):
    help = ('Build releasing/grouping/locking state so the Part 6 SCM handoff screens '
            'have something to show. Dry run unless --confirm is passed.')

    def add_arguments(self, parser):
        parser.add_argument('--confirm', action='store_true',
                            help='Actually write. Without this the command only reports.')
        parser.add_argument('--reset', action='store_true',
                            help='Remove this command\'s own output first, then re-create.')

    # ------------------------------------------------------------------ helpers
    def _resolve(self, username, expect_role=None):
        from projects.models import UserProfile
        try:
            p = UserProfile.objects.select_related('user').get(user__username=username)
        except UserProfile.DoesNotExist:
            raise CommandError(
                f"User '{username}' not found. This command resolves real users at "
                f"runtime and will not invent accounts.")
        if expect_role and p.role != expect_role:
            raise CommandError(
                f"User '{username}' has role {p.role!r}, expected {expect_role!r}. "
                f"Refusing to proceed — this command never changes a user's role.")
        return p

    def _ensure_boq(self, site, codes_to_qty):
        """Create the site's BOQ from the catalogue if absent, then set quantities.

        Mirrors what boq_detail does on first GET for an authorised designer: seed every
        active BOQItemMaster row, carrying `item_master` so the quantities can aggregate.
        Quantities are SET, not added, so a re-run is idempotent.
        """
        from projects.models import BOQ, BOQItem, BOQItemMaster, get_standard_boq_items

        boq, created = BOQ.objects.get_or_create(project=site)
        if created or not boq.items.exists():
            masters = {m.description: m for m in BOQItemMaster.objects.filter(is_active=True)}
            BOQItem.objects.bulk_create([
                BOQItem(boq=boq, item_master=masters.get(d['description']), **d)
                for d in get_standard_boq_items()
            ])

        # Clear first so a re-run with a changed profile does not leave stale quantities.
        boq.items.filter(is_standard_item=True).update(boq_quantity=None)
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

    def _release(self, site, designer, head, released_at, capacity_kw):
        """Put one site into the exact state QC-pass leaves it in.

        Writes the assignment, one closed attempt with a passed QC verdict, and the
        approved Arka that attempt derives from — so a site that shows as released on the
        SCM screens also reads correctly if the user clicks through to the design screens.

        `survey_file_path` is left EMPTY on purpose: a fake path would make
        teardown_opex_test_data attempt a Supabase delete for an object that never
        existed. The SCM handoff screens never read it.
        """
        from projects.models import (DesignAssignment, DesignAttempt, ArkaSubmission,
                                     DESIGN_RELEASED, ATTEMPT_REASON_INITIAL,
                                     QC_PASSED, ARKA_APPROVED)

        a, _ = DesignAssignment.objects.get_or_create(project=site)
        a.assigned_to  = designer
        a.assigned_by  = head
        a.assigned_at  = released_at - timedelta(days=20)
        a.status       = DESIGN_RELEASED
        a.released_at  = released_at
        a.released_by  = head
        a.current_attempt_number = 1
        a.save()

        att, _ = DesignAttempt.objects.get_or_create(
            assignment=a, attempt_number=1,
            defaults={'opened_reason': ATTEMPT_REASON_INITIAL},
        )
        att.qc_started_at    = released_at - timedelta(days=1)
        att.qc_verdict       = QC_PASSED
        att.qc_reviewed_by   = head
        att.qc_reviewed_at   = released_at
        att.qc_remarks       = ''
        att.boq_submitted_at = released_at - timedelta(days=2)
        att.boq_submitted_by = designer
        att.closed_at        = released_at
        att.save()
        # opened_at is auto_now_add — backdate it separately so the attempt does not
        # claim to have opened after the QC that closed it.
        DesignAttempt.objects.filter(pk=att.pk).update(
            opened_at=released_at - timedelta(days=18))

        arka, _ = ArkaSubmission.objects.get_or_create(
            attempt=att, version=1,
            defaults={
                'capacity_kw': Decimal(capacity_kw),
                'arka_link':   'https://example.com/arka/test-seed',
                'submitted_by': designer,
            },
        )
        arka.verdict     = ARKA_APPROVED
        arka.reviewed_by = head
        arka.reviewed_at = released_at - timedelta(days=5)
        arka.is_current  = True
        arka.save()
        return a

    # ------------------------------------------------------------------ reset
    def _reset(self):
        from projects.models import (Program, Project, ProjectPhase, Task, BOQ, BOQItem,
                                     BOQRevision, DesignAssignment, DueDateCommitment,
                                     DesignAttempt, ArkaSubmission, DesignFile,
                                     DesignChangeRequest, SiteGroup, SiteGroupMembership)
        removed = []

        # 1. every group under a Test- program (memberships cascade)
        groups = SiteGroup.objects.filter(program__name__startswith=TEST_PREFIX)
        n_mem = SiteGroupMembership.objects.filter(group__in=groups).count()
        n_grp = groups.count()
        SiteGroupMembership.objects.filter(group__in=groups).delete()
        groups.delete()
        removed.append(('SiteGroupMembership', n_mem))
        removed.append(('SiteGroup', n_grp))

        # 2. the ad-hoc BOQ row
        n_adhoc = BOQItem.objects.filter(
            boq__project__project_id=ADHOC_SITE, is_standard_item=False,
            description=ADHOC_DESCRIPTION).count()
        BOQItem.objects.filter(
            boq__project__project_id=ADHOC_SITE, is_standard_item=False,
            description=ADHOC_DESCRIPTION).delete()
        removed.append(('BOQItem (ad-hoc)', n_adhoc))

        # 3. the Phase2 tender, bottom-up (same order teardown_opex_test_data uses,
        #    because DesignFile.derived_from_arka is PROTECT and Project.program is PROTECT)
        p2 = Program.objects.filter(name=P2_PROGRAM_NAME)
        sites = Project.objects.filter(program__name=P2_PROGRAM_NAME)
        assignments = DesignAssignment.objects.filter(project__in=sites)
        attempts    = DesignAttempt.objects.filter(assignment__in=assignments)
        boqs        = BOQ.objects.filter(project__in=sites)
        phases      = ProjectPhase.objects.filter(project__in=sites)
        for label, qs in [
            ('DesignFile',          DesignFile.objects.filter(attempt__in=attempts)),
            ('ArkaSubmission',      ArkaSubmission.objects.filter(attempt__in=attempts)),
            ('DesignChangeRequest', DesignChangeRequest.objects.filter(attempt__in=attempts)),
            ('DesignAttempt',       attempts),
            ('DueDateCommitment',   DueDateCommitment.objects.filter(assignment__in=assignments)),
            ('DesignAssignment',    assignments),
            ('BOQItem',             BOQItem.objects.filter(boq__in=boqs)),
            ('BOQRevision',         BOQRevision.objects.filter(boq__in=boqs)),
            ('BOQ',                 boqs),
            ('Task',                Task.objects.filter(phase__in=phases)),
            ('ProjectPhase',        phases),
            ('Project (Phase2)',    sites),
            ('Program (Phase2)',    p2),
        ]:
            n = qs.count()
            if n:
                qs.delete()
            removed.append((label, n))
        return removed

    # ------------------------------------------------------------------ main
    def handle(self, *args, **options):
        from projects.models import (Program, Project, NotificationLog, SiteGroup,
                                     SiteGroupMembership, BOQItem, DesignAssignment,
                                     SITE_GROUP_DRAFT, SITE_GROUP_LOCKED)

        confirm = options['confirm']
        reset   = options['reset']
        now     = timezone.now()

        head     = self._resolve('praveen', expect_role='Design')
        designer_a = self._resolve('priyanka', expect_role='Design')
        designer_b = self._resolve('shyam',    expect_role='Design')
        scm      = self._resolve('subhash', expect_role='SCM')
        pm       = self._resolve('chetan',  expect_role='PM')

        base = Program.objects.filter(name=BASE_PROGRAM_NAME).first()
        if base is None:
            raise CommandError(
                f"Program {BASE_PROGRAM_NAME!r} not found. Run "
                f"`python manage.py seed_opex_test_data` first.")

        self.stdout.write('Resolved users:')
        for label, p in (('Design Head', head), ('Designer A', designer_a),
                         ('Designer B', designer_b), ('SCM', scm), ('PM', pm)):
            self.stdout.write(f'  {label:<12} {p.user.username} (pk={p.pk})')
        self.stdout.write(f'  Base tender  {base.name!r} pk={base.pk}\n')

        # ---------------- dry run ----------------
        if not confirm:
            self.stdout.write(self.style.WARNING('DRY RUN — nothing will be written.\n'))
            if reset:
                self.stdout.write('WOULD RESET: every SiteGroup/Membership under a Test- '
                                  'program, the ad-hoc BOQ row, and the whole '
                                  f'{P2_PROGRAM_NAME!r} tender.\n')
            self.stdout.write(f'WOULD ENSURE released + BOQ quantities on: '
                              f'Test-Site-01, Test-Site-04, Test-Site-05')
            self.stdout.write(f'WOULD CREATE Program {P2_PROGRAM_NAME!r} '
                              f'(code {P2_TENDER_CODE!r}) with 6 released sites, '
                              f'released {min(P2_RELEASE_AGE_DAYS.values())}-'
                              f'{max(P2_RELEASE_AGE_DAYS.values())} days ago')
            self.stdout.write(f'WOULD CREATE SiteGroup {GROUP_LOCKED_NAME!r} LOCKED '
                              f'(Test-Site-01, Test-Site-04)')
            self.stdout.write(f'WOULD CREATE SiteGroup {GROUP_DRAFT_NAME!r} DRAFT '
                              f'(Test-Site-05)')
            self.stdout.write('WOULD CREATE 2 removed memberships '
                              '(one reason="PM change request")')
            self.stdout.write(f'WOULD CREATE 1 ad-hoc BOQItem on {ADHOC_SITE} '
                              f'(qty {ADHOC_QTY}, no item_master)')
            self.stdout.write('\nRe-run with --confirm to write.')
            return

        notif_before = NotificationLog.objects.count()

        with transaction.atomic():
            if reset:
                self.stdout.write('RESET:')
                for label, n in self._reset():
                    self.stdout.write(f'  removed {label:<22} {n}')
                self.stdout.write('')

            if SiteGroup.objects.filter(program__name__startswith=TEST_PREFIX).exists():
                raise CommandError(
                    'Site groups already exist under a Test- program. Re-run with '
                    '--reset --confirm to rebuild them cleanly.')

            # ---- 1. base tender: three released sites with a richer BOQ ----
            self.stdout.write('Base tender — releasing sites and setting BOQ quantities:')
            base_sites = {}
            for pid, designer, cap in (('Test-Site-01', designer_a, 101),
                                       ('Test-Site-04', designer_b, 104),
                                       ('Test-Site-05', designer_b, 105)):
                site = Project.objects.get(project_id=pid)
                self._release(site, designer, head,
                              now - timedelta(days={'Test-Site-01': 12,
                                                    'Test-Site-04': 12,
                                                    'Test-Site-05': 5}[pid]),
                              cap)
                self._ensure_boq(site, QTY_PROFILE[pid])
                base_sites[pid] = site
                self.stdout.write(self.style.SUCCESS(
                    f'  {pid}: released, {len(QTY_PROFILE[pid])} BOQ quantities set'))

            # ---- 2. the ad-hoc, unlinked BOQ row ----
            adhoc_site = base_sites[ADHOC_SITE]
            last_serial = max(i.serial_no for i in adhoc_site.boq.items.all())
            BOQItem.objects.create(
                boq=adhoc_site.boq, serial_no=last_serial + 1, category='Other',
                description=ADHOC_DESCRIPTION, uom='LOT',
                boq_quantity=ADHOC_QTY, is_standard_item=False,   # item_master stays NULL
            )
            self.stdout.write(self.style.SUCCESS(
                f'  {ADHOC_SITE}: 1 ad-hoc BOQ row with no item_master '
                f'(qty {ADHOC_QTY}) -> drives the aggregation warning'))

            # ---- 3. Phase2 tender: six released sites, staggered ages, no groups ----
            self.stdout.write(f'\n{P2_PROGRAM_NAME} — the post-QC pile-up:')
            p2 = Program.objects.create(
                program_type='OPEX', name=P2_PROGRAM_NAME, client_name=P2_CLIENT,
                status='Active', short_tender_code=P2_TENDER_CODE, planned_site_count=6,
            )
            for n, age in sorted(P2_RELEASE_AGE_DAYS.items()):
                pid = P2_SITE_ID % n
                designer = designer_a if n % 2 else designer_b
                site = Project(
                    project_id=pid, customer_name=P2_CLIENT,
                    customer_phone='98888%05d' % n,
                    customer_contact_person=f'Phase2 Site In-Charge {n:02d}',
                    site_address=f'{pid}, test address', city='Gurugram', state='Haryana',
                    project_type='OPEX', program=p2, site_code='P2SITE%02d' % n,
                    capacity_kw=200 + n, status='Draft',
                    assigned_pm=pm, assigned_design=designer,
                )
                site.save()
                self._release(site, designer, head, now - timedelta(days=age), 200 + n)
                self._ensure_boq(site, QTY_PROFILE[pid])
                self.stdout.write(self.style.SUCCESS(
                    f'  {pid}: released {age:2d} days ago, designer '
                    f'{designer.user.username}'))

            # ---- 4. the locked group ----
            self.stdout.write('\nGroups:')
            locked = SiteGroup.objects.create(
                program=base, name=GROUP_LOCKED_NAME, status=SITE_GROUP_LOCKED,
                created_by=scm, locked_by=scm, locked_at=now - timedelta(days=4),
                notes='First procurement batch — modules, transport and structure.',
            )
            SiteGroup.objects.filter(pk=locked.pk).update(
                created_at=now - timedelta(days=11))
            for pid in ('Test-Site-01', 'Test-Site-04'):
                m = SiteGroupMembership.objects.create(
                    group=locked, project=base_sites[pid], added_by=scm)
                SiteGroupMembership.objects.filter(pk=m.pk).update(
                    added_at=now - timedelta(days=11))
            self.stdout.write(self.style.SUCCESS(
                f'  LOCKED {GROUP_LOCKED_NAME!r}: Test-Site-01, Test-Site-04 '
                f'(locked 4 days ago by {scm.user.username})'))

            # ---- 5. the draft group ----
            draft = SiteGroup.objects.create(
                program=base, name=GROUP_DRAFT_NAME, status=SITE_GROUP_DRAFT,
                created_by=scm,
                notes='Second batch — cables, BOS and the items still being quoted.',
            )
            SiteGroup.objects.filter(pk=draft.pk).update(created_at=now - timedelta(days=3))
            m = SiteGroupMembership.objects.create(
                group=draft, project=base_sites['Test-Site-05'], added_by=scm)
            SiteGroupMembership.objects.filter(pk=m.pk).update(
                added_at=now - timedelta(days=3))
            self.stdout.write(self.style.SUCCESS(
                f'  DRAFT  {GROUP_DRAFT_NAME!r}: Test-Site-05'))

            # ---- 6. two historical removals ----
            # `removed_at` is set IN THE INSERT, not by a follow-up update. The partial
            # unique constraint counts any row with removed_at IS NULL as active, so
            # creating these blank-then-updating would momentarily give Test-Site-05 two
            # live memberships and trip uniq_active_site_group_membership. Only `added_at`
            # is backdated afterwards, because it is auto_now_add and cannot be set here.
            #
            # (a) SCM moved a site to a later batch — the ordinary case.
            r1 = SiteGroupMembership.objects.create(
                group=locked, project=base_sites['Test-Site-05'], added_by=scm,
                removed_by=scm, removed_at=now - timedelta(days=6),
                removal_reason='Lead time on cable drums — moved to the next batch',
            )
            SiteGroupMembership.objects.filter(pk=r1.pk).update(
                added_at=now - timedelta(days=11))

            # (b) a PM change request pulled a site out (Part 6 §4). Test-Site-02 is
            #     `in_design` with two attempts — exactly the state that leaves behind.
            #     The reason string must match CHANGE_REQUEST_REMOVAL_REASON exactly or
            #     the group screen renders it as ordinary text instead of the red chip.
            from projects.design_views import CHANGE_REQUEST_REMOVAL_REASON
            site02 = Project.objects.get(project_id='Test-Site-02')
            r2 = SiteGroupMembership.objects.create(
                group=draft, project=site02, added_by=scm, removed_by=pm,
                removed_at=now - timedelta(days=1),
                removal_reason=CHANGE_REQUEST_REMOVAL_REASON,
            )
            SiteGroupMembership.objects.filter(pk=r2.pk).update(
                added_at=now - timedelta(days=3))
            self.stdout.write(self.style.SUCCESS(
                '  2 removed memberships recorded '
                f'(Test-Site-05 from Batch 1; Test-Site-02 from Batch 2 as '
                f'{CHANGE_REQUEST_REMOVAL_REASON!r})'))

            # ---- guard: nothing here may notify a real user ----
            notif_after = NotificationLog.objects.count()
            if notif_after != notif_before:
                raise CommandError(
                    f'NotificationLog grew from {notif_before} to {notif_after} during '
                    f'seeding — aborting and rolling back.')

        # ---------------- what to look at ----------------
        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS('=' * 72))
        self.stdout.write(self.style.SUCCESS('Done. NotificationLog unchanged at %d.'
                                             % notif_before))
        self.stdout.write(self.style.SUCCESS('=' * 72))
        self.stdout.write('')
        self.stdout.write('Log in as  subhash  (SCM) to see every screen below.')
        self.stdout.write('The Design Head (praveen) and Admin can VIEW the group screens')
        self.stdout.write('read-only; every write button is SCM-only.')
        self.stdout.write('')
        self.stdout.write('  /dashboard/scm/')
        self.stdout.write('      OPEX section, three tender rows: Test-Opex (2 groups, 1')
        self.stdout.write('      locked, empty pool), Test-Opex-Phase2 (no groups, 6 in the')
        self.stdout.write('      pool, oldest 31d), Finolex (nothing released yet).')
        self.stdout.write(f'  /programs/{base.pk}/site-groups/')
        self.stdout.write('      Test-Opex: both groups listed, pool empty.')
        self.stdout.write(f'  /programs/{p2.pk}/site-groups/')
        self.stdout.write('      Phase2: no groups, 6 sites in the pool oldest-first with')
        self.stdout.write('      the >=14 day ages in red, and the create-a-group form.')
        self.stdout.write(f'  /site-groups/{locked.pk}/')
        self.stdout.write('      LOCKED group — aggregated BOQ, lock banner, no lock button,')
        self.stdout.write('      one site that left with a reason.')
        self.stdout.write(f'  /site-groups/{draft.pk}/')
        self.stdout.write('      DRAFT group — aggregate, the unaggregated-rows WARNING,')
        self.stdout.write('      add/remove controls, the lock button, and the red')
        self.stdout.write('      "PM change request" departure chip.')
        self.stdout.write('  /projects/Test-Site-01/boq/')
        self.stdout.write('      A LOCKED member site: banner shown, quantities read-only.')
        self.stdout.write('  /projects/Test-Site-05/boq/')
        self.stdout.write('      A draft-group site: still fully editable by its designer.')
        self.stdout.write('')
        self.stdout.write('Undo with:  python manage.py seed_scm_handoff_data --reset --confirm')
        self.stdout.write('Or remove everything Test- prefixed with:')
        self.stdout.write('            python manage.py teardown_opex_test_data --confirm')
