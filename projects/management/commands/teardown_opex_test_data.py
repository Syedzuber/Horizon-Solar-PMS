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

STORAGE OBJECTS ARE DELETED TOO (E5)
------------------------------------
Deleting the rows alone used to leave every uploaded survey, CAD file and BOQ
attachment sitting in the private Supabase bucket with nothing referencing it —
`build_design_path()` mints a fresh uuid4 per upload, so replaced files never overwrite
their predecessors and orphans accumulate with every seed/teardown cycle.

The bucket and path are collected from `DesignFile` and from
`DesignAssignment.survey_file_path` BEFORE any row is deleted (afterwards there is
nothing left to read them from), and the objects are removed through
`design_storage.delete_design_objects()`.

STORAGE FAILURES ARE REPORTED, NOT FATAL. An unreachable bucket, a revoked key or an
object someone already deleted by hand must never make the test rows undeletable — the
whole point of this command is that it always works. Every object gets its own
success/failure line and the row deletion proceeds regardless.

THE PUBLIC BUCKET IS NEVER TOUCHED. `delete_design_objects()` refuses any pair whose
bucket is not the private design bucket, so a hand-edited `bucket` column cannot aim a
delete at `SUPABASE_BUCKET` or at the four existing public-bucket call sites.

SAFETY
------
Rows are identified ONLY by the literal `Test-` prefix, imported from the seed command
so the two cannot drift. Every Project caught in the sweep is re-checked against that
prefix immediately before deletion; a single non-prefixed row aborts the whole
transaction rather than being deleted. Storage objects inherit that safety: they are
read off rows that already passed the prefix guard, never located by listing the bucket.
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from projects.design_storage import delete_design_objects
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
            DesignFile, DesignChangeRequest, SiteGroup, SiteGroupMembership,
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
        #
        # SITE GROUPS (Part 6) are listed FIRST and deleted explicitly even though both
        # FKs are CASCADE and would go anyway — the point is the inventory. Without these
        # two rows the dry run silently under-reports what a --confirm is about to remove,
        # which is the one thing this command exists to be trusted about. Memberships
        # precede groups; a membership can also hang off a Test- site that belongs to a
        # group under a NON-Test program, so it is filtered by project, not by group.
        steps = [
            ('SiteGroupMembership', SiteGroupMembership.objects.filter(
                                        project__in=target_projects)),
            ('SiteGroup',           SiteGroup.objects.filter(program__in=programs)),
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

        # ---- collect storage objects BEFORE anything is deleted ----
        # Read from the same querysets the deletion steps use, so the objects listed are
        # exactly the objects belonging to the rows about to go. Ordered and labelled so
        # the dry run reads as an inventory rather than a list of opaque uuids.
        storage_targets = []   # (label, bucket, path)
        for f in (DesignFile.objects
                  .filter(attempt__in=attempts)
                  .select_related('attempt__assignment__project')
                  .order_by('attempt__assignment__project__project_id', 'kind', 'version')):
            storage_targets.append((
                f'{f.attempt.assignment.project.project_id} {f.kind} v{f.version}',
                f.bucket, f.path,
            ))
        for a in (assignments.select_related('project').order_by('project__project_id')):
            if a.survey_file_path:
                storage_targets.append((
                    f'{a.project.project_id} survey', a.survey_file_bucket, a.survey_file_path,
                ))

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

        self.stdout.write(f'Storage objects in the private design bucket ({len(storage_targets)}):')
        if not storage_targets:
            self.stdout.write('  (none)')
        for label, bucket, path in storage_targets:
            self.stdout.write(f'  {label:<34} {bucket}/{path}')
        self.stdout.write('')

        if not confirm:
            self.stdout.write(self.style.WARNING(
                'DRY RUN — nothing deleted, no storage object removed. '
                'Re-run with --confirm to delete.'))
            return

        # ---- storage first, per the E5 brief: collect, delete objects, then rows ----
        # Deliberately OUTSIDE the transaction. Supabase cannot participate in a Postgres
        # transaction, so wrapping it would buy nothing and a rollback could not put an
        # object back. Failures are printed and the row deletion below runs regardless.
        if storage_targets:
            self.stdout.write('Removing storage objects:')
            label_by_path = {(b, p): lbl for lbl, b, p in storage_targets}
            results = delete_design_objects([(b, p) for _lbl, b, p in storage_targets])
            failures = 0
            for bucket, path, ok, error in results:
                label = label_by_path.get((bucket, path), '')
                if ok:
                    self.stdout.write(self.style.SUCCESS(f'  removed  {label:<34} {bucket}/{path}'))
                else:
                    failures += 1
                    self.stderr.write(self.style.WARNING(
                        f'  FAILED   {label:<34} {bucket}/{path} — {error}'))
            if failures:
                self.stdout.write(self.style.WARNING(
                    f'{failures} storage object(s) could not be removed. Row deletion '
                    f'continues — an unreachable bucket must not make test data '
                    f'undeletable.'))
            self.stdout.write('')

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
