"""
Management command: completely remove the OPEX test tender created by
`seed_opex_test_data`, including anything Part 2+ testing hung off it.

HARD DELETE, NOT SOFT DELETE
----------------------------
This calls `.delete()` and never sets `is_deleted=True`. A soft-deleted row keeps
occupying its unique values: `Project.project_id` is UNIQUE at the DB level, and
`Program.short_tender_code` is checked for uniqueness against the UNFILTERED manager
in ProgramForm — so a soft-deleted tender would permanently reserve its code and block
a re-seed. This codebase has already been bitten by that pattern with `zoho_deal_id`.
Only a hard delete releases both cleanly, which is what makes seed -> teardown -> seed
work.

DELETION ORDER IS LOAD-BEARING
------------------------------
`DesignFile.derived_from_arka` is PROTECT, and Django does NOT resolve a PROTECT inside
a single cascade even when the protecting row is part of the same delete. Verified
empirically: `project.delete()` on a site that has one DesignFile raises

    ProtectedError: Cannot delete some instances of model 'Project' because they are
    referenced through protected foreign keys: 'DesignAssignment.project'

So the design rows are deleted explicitly bottom-up before the Project, and the
Projects before the Program (`Project.program` is also PROTECT). Deleting in this order
is what makes the command work at all once Part 2 has created design artifacts —
it is not merely for tidy per-model counts.

SAFETY
------
Rows are identified ONLY by the literal `Test-` prefix, imported from the seed command
so the two cannot drift. Every Project caught in the sweep is re-checked against that
prefix immediately before deletion; a single non-prefixed row aborts the whole
transaction rather than being deleted.
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from projects.management.commands.seed_opex_test_data import (
    TEST_PREFIX, PROGRAM_NAME,
)


class Command(BaseCommand):
    help = ('Hard-delete the Test- prefixed OPEX tender and everything under it. '
            'Dry run unless --confirm is passed.')

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Print what would be deleted, per model. This is also the default.',
        )
        parser.add_argument(
            '--confirm', action='store_true',
            help='Actually delete. Without this the command only reports.',
        )

    def handle(self, *args, **options):
        from projects.models import (
            Program, Project, ProjectPhase, Task, BOQ, BOQItem, BOQRevision,
            DesignAssignment, DueDateCommitment, DesignAttempt, ArkaSubmission,
            DesignFile, DesignChangeRequest,
        )

        confirm = options['confirm']

        # ---- identify targets by prefix only ----
        programs = Program.objects.filter(name__startswith=TEST_PREFIX)
        # Both the tender's children AND any stray Test- prefixed project, so an
        # orphaned site is not left behind holding its unique project_id.
        projects = Project.objects.filter(project_id__startswith=TEST_PREFIX)
        project_ids = list(projects.values_list('pk', flat=True))
        for program in programs:
            for pk in program.sites.values_list('pk', flat=True):
                if pk not in project_ids:
                    project_ids.append(pk)

        target_projects = Project.objects.filter(pk__in=project_ids)

        if not programs.exists() and not target_projects.exists():
            self.stdout.write('Nothing to delete — no Test- prefixed Program or Project found.')
            return

        # ---- SAFETY GUARD: refuse anything not prefixed ----
        # A non-prefixed site can only get here by being a child of a Test- Program,
        # i.e. something real was attached to the test tender. Abort rather than delete.
        unsafe_projects = [p for p in target_projects if not p.project_id.startswith(TEST_PREFIX)]
        unsafe_programs = [p for p in programs if not p.name.startswith(TEST_PREFIX)]
        if unsafe_projects or unsafe_programs:
            for p in unsafe_projects:
                self.stderr.write(self.style.ERROR(
                    f'REFUSING: Project pk={p.pk} project_id={p.project_id!r} '
                    f'does not start with {TEST_PREFIX!r}'))
            for p in unsafe_programs:
                self.stderr.write(self.style.ERROR(
                    f'REFUSING: Program pk={p.pk} name={p.name!r} '
                    f'does not start with {TEST_PREFIX!r}'))
            raise CommandError(
                'Aborted: a row without the Test- prefix was caught in the sweep. '
                'Nothing was deleted.'
            )

        # ---- collect, bottom-up, in deletion order ----
        assignments = DesignAssignment.objects.filter(project__in=target_projects)
        attempts    = DesignAttempt.objects.filter(assignment__in=assignments)
        boqs        = BOQ.objects.filter(project__in=target_projects)
        phases      = ProjectPhase.objects.filter(project__in=target_projects)

        # Ordered so that every entry's PROTECT/CASCADE dependencies are already gone
        # by the time it is deleted. DesignFile MUST precede ArkaSubmission.
        steps = [
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
            ('Project',             target_projects),
            ('Program',             programs),
        ]

        counts = [(label, qs.count()) for label, qs in steps]

        self.stdout.write('Targets (identified by the %r prefix):' % TEST_PREFIX)
        for p in programs.order_by('pk'):
            self.stdout.write(f'  Program pk={p.pk} name={p.name!r} code={p.short_tender_code!r}')
        for p in target_projects.order_by('project_id'):
            self.stdout.write(f'  Project pk={p.pk} project_id={p.project_id!r} '
                              f'is_deleted={p.is_deleted}')
        self.stdout.write('')
        self.stdout.write('Row counts, in deletion order:')
        for label, n in counts:
            self.stdout.write(f'  {label:<20} {n}')
        total = sum(n for _, n in counts)
        self.stdout.write(f'  {"TOTAL":<20} {total}')
        self.stdout.write('')

        if not confirm:
            self.stdout.write(self.style.WARNING(
                'DRY RUN — nothing deleted. Re-run with --confirm to delete.'))
            return

        deleted = []
        with transaction.atomic():
            for label, qs in steps:
                n, _detail = qs.delete()
                deleted.append((label, n))

        self.stdout.write('Deleted (rows reported by Django per .delete() call):')
        for label, n in deleted:
            self.stdout.write(self.style.SUCCESS(f'  {label:<20} {n}'))
        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS('Hard delete complete — no is_deleted flag was set.'))
