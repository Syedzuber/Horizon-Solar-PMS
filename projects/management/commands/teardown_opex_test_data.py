"""
Management command: remove exactly what `seed_opex_test_data` and
`seed_scm_handoff_data` recorded creating, and nothing else.

    python manage.py teardown_opex_test_data              # dry run — reports only
    python manage.py teardown_opex_test_data --confirm    # delete

TYPE BOTH COMMAND NAMES IN FULL. `se`+Tab does not disambiguate `seed_opex_test_data`
from `send_eod_digest`, which mails the whole company. The runbook already records
that collision; it is repeated here because this is where somebody will be typing
fast.

THE MANIFEST IS THE ONLY THING THIS COMMAND READS
-------------------------------------------------
It deletes a list of primary keys. It does not search for rows to delete, and it runs
no query that could select a row the seed did not create.

Until this change it identified its targets by pattern-matching LIVE TABLES —
`Project.objects.filter(project_id__startswith='Test-')` and the relation traversals
hanging off it. That worked, and it was one mistyped constant away from not working.
The `DEMO` namespace still exists, so anything that ever escapes the manifest is
identifiable by eye, but nothing here reads it.

WHY THERE IS NO FALLBACK, AND WHAT IT COSTS
-------------------------------------------
When the manifest is missing this command REFUSES. It does not fall back to matching
names or prefixes: a fallback would reintroduce, as the error path, exactly the
mechanism the manifest replaced — and the error path is where it would be least
tested.

The cost is a real regression, and the refusal message names it: `Test-` prefixed rows
created by the OLD seed cannot be removed by this version. Whoever hits that needs to
know it is a known consequence and not a broken tool, or they will spend an hour
looking for the bug.

DELETION ORDER
--------------
The manifest is written in creation order and walked in REVERSE, which puts children
before parents and leaves before both. That matters: `DesignFile.derived_from_arka`
and `Project.program` are PROTECT, and Django does not resolve a PROTECT inside a
single cascade even when the protecting row is part of the same delete.

A row already gone — cascaded away by an earlier step, or deleted by hand — is
reported and is NOT an error. Absence is the desired end state.

STATUS TRANSITIONS ARE DELETED, AND THAT IS A DELIBERATE EXCEPTION TO R-4
-------------------------------------------------------------------------
`StatusTransition` is append-only. `save()` refuses to touch an existing row and
`delete()` raises `AppendOnlyViolation` outright — the ledger's central guarantee.
`QuerySet.delete()` operates in SQL and bypasses both overrides, as the model's own
docstring says out loud, and that is the route taken here.

This is a decision, not an accident, and it is narrow on purpose:

  * A demo site's ledger rows would otherwise SURVIVE the site. `StatusTransition.project`
    is SET_NULL precisely so a hard-deleted project cannot erase its own history — so
    every teardown would leave orphaned rows behind, permanently, growing by one set
    per cycle, in the table the dwell-time reports read.
  * It is only ever safe because the MANIFEST BOUNDS IT. These are rows this seed
    created, on a database the interlock has already proved is local. Nothing here can
    reach a transition somebody's real work wrote.

Do not generalise it. There is no other caller and there must not be one.

STORAGE OBJECTS
---------------
`DesignFile` rows and `DesignAssignment.survey_file_path` values point at objects in
the private Supabase design bucket, which no row deletion touches. Bucket and path are
collected from the manifest's own rows BEFORE anything is deleted and removed through
`design_storage.delete_design_objects()`, which refuses any bucket that is not the
private design bucket.

STORAGE FAILURES ARE REPORTED, NOT FATAL. An unreachable bucket or a revoked key must
never make demo rows undeletable — the whole point of this command is that it always
works. The row deletion proceeds regardless.
"""
from django.apps import apps
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from projects.design_storage import delete_design_objects
from projects.management.commands._demo_support import (
    DEFAULT_MANIFEST_PATH, MANIFEST_MISSING_MESSAGE, Manifest, OVERRIDE_FLAG,
    print_db_banner, require_local_database,
)

WRITES = [
    'DELETE every row listed in the manifest, in reverse creation order',
    'including its StatusTransition rows, bypassing the append-only guard',
    'and the Supabase objects its DesignFile rows point at',
]


class Command(BaseCommand):
    help = ('Delete exactly the rows a demo seed recorded creating. Refuses without a '
            'manifest and refuses a non-local database. Dry run unless --confirm.')

    def add_arguments(self, parser):
        parser.add_argument('--confirm', action='store_true',
                            help='Actually delete. Without this the command only reports.')
        parser.add_argument('--dry-run', action='store_true',
                            help='Report only. This is also the default.')
        parser.add_argument('--manifest', type=str, default='',
                            help=f'Manifest to read. Default: {DEFAULT_MANIFEST_PATH}')
        parser.add_argument(OVERRIDE_FLAG, action='store_true',
                            help='Required to run against a non-local database.')

    def handle(self, *args, **options):
        # TASK 1 — always the first line of output, before anything else is decided.
        print_db_banner(self)
        require_local_database(
            self, options.get('i_know_this_is_not_local', False), WRITES)

        confirm       = options['confirm']
        manifest_path = options['manifest'].strip() or DEFAULT_MANIFEST_PATH

        manifest = Manifest.load(manifest_path)
        if manifest is None:
            raise CommandError(MANIFEST_MISSING_MESSAGE.format(path=manifest_path))

        self.stdout.write(f'Manifest      : {manifest_path}')
        written_at = manifest.meta.get('written_at', '(unknown)')
        seeded_db  = manifest.meta.get('database', {})
        self.stdout.write(f'Written at    : {written_at}')
        self.stdout.write(f'Seeded against: host={seeded_db.get("host", "?")} '
                          f'name={seeded_db.get("name", "?")}')
        self.stdout.write(f'Rows recorded : {len(manifest.entries)}')
        self.stdout.write('')

        if not manifest.entries:
            self.stdout.write('Manifest is empty — nothing to delete.')
            return

        # ---- resolve each entry to a model, in reverse creation order ----
        # An entry naming a model this build does not have is reported, never guessed
        # at: a manifest written before a model was renamed must not be silently
        # half-applied.
        plan, unknown = [], []
        for row in reversed(manifest.entries):
            model = self._resolve(row['model'])
            if model is None:
                unknown.append(row)
                continue
            plan.append((model, row))

        present, missing = [], []
        for model, row in plan:
            (present if model.objects.filter(pk=row['pk']).exists()
             else missing).append((model, row))

        self.stdout.write('To delete, by model (deletion order is reverse of creation):')
        for label, count in self._tally(present):
            self.stdout.write(f'  {label:<34} {count}')
        self.stdout.write(f'  {"TOTAL":<34} {len(present)}')
        if missing:
            self.stdout.write('')
            self.stdout.write('Already gone (cascaded, or removed by hand) — not an error:')
            for label, count in self._tally(missing):
                self.stdout.write(f'  {label:<34} {count}')
        if unknown:
            self.stdout.write('')
            for row in unknown:
                self.stderr.write(self.style.WARNING(
                    f'  UNKNOWN MODEL  {row["model"]} pk={row["pk"]} — this build has '
                    f'no such model; left alone.'))
        self.stdout.write('')

        # ---- collect storage objects BEFORE anything is deleted ----
        storage = self._storage_targets(present)
        self.stdout.write(f'Storage objects in the private design bucket ({len(storage)}):')
        for label, bucket, path in storage:
            self.stdout.write(f'  {label:<34} {bucket}/{path}')
        if not storage:
            self.stdout.write('  (none)')
        self.stdout.write('')

        if not confirm:
            self.stdout.write(self.style.WARNING(
                'DRY RUN — nothing deleted, no storage object removed, manifest left '
                'in place. Re-run with --confirm to delete.'))
            return

        # ---- storage first: collect, delete objects, then rows ----
        # Deliberately OUTSIDE the transaction. Supabase cannot participate in a
        # Postgres transaction, so wrapping it would buy nothing and a rollback could
        # not put an object back.
        if storage:
            self.stdout.write('Removing storage objects:')
            labels  = {(b, p): lbl for lbl, b, p in storage}
            results = delete_design_objects([(b, p) for _l, b, p in storage])
            failures = 0
            for bucket, path, ok, error in results:
                label = labels.get((bucket, path), '')
                if ok:
                    self.stdout.write(self.style.SUCCESS(
                        f'  removed  {label:<34} {bucket}/{path}'))
                else:
                    failures += 1
                    self.stderr.write(self.style.WARNING(
                        f'  FAILED   {label:<34} {bucket}/{path} — {error}'))
            if failures:
                self.stdout.write(self.style.WARNING(
                    f'{failures} storage object(s) could not be removed. Row deletion '
                    f'continues — an unreachable bucket must not make demo data '
                    f'undeletable.'))
            self.stdout.write('')

        # ---- delete rows, one manifest entry at a time, in reverse order ----
        # Per-entry rather than one queryset per model, because ORDER is the whole
        # mechanism: a bulk delete per model would re-sort the work by model and lose
        # the dependency ordering the manifest encodes.
        #
        # Model.objects.filter(pk=...).delete() is a QuerySet delete: it does not call
        # the instance's delete(), which is what lets StatusTransition rows go at all.
        # See the module docstring — that is a bounded, deliberate exception to R-4.
        deleted, vanished = {}, 0
        with transaction.atomic():
            for model, row in plan:
                label = row['model']
                count, _detail = model.objects.filter(pk=row['pk']).delete()
                if count:
                    deleted[label] = deleted.get(label, 0) + 1
                else:
                    vanished += 1

        self.stdout.write('Deleted:')
        for label in sorted(deleted):
            self.stdout.write(self.style.SUCCESS(f'  {label:<34} {deleted[label]}'))
        self.stdout.write(f'  {"TOTAL":<34} {sum(deleted.values())}')
        if vanished:
            self.stdout.write(f'{vanished} manifest row(s) were already gone — '
                              f'cascaded or removed by hand. Not an error.')
        self.stdout.write('')

        # The manifest describes rows that no longer exist. Leaving it in place would
        # make the next teardown report a database-wide "already gone", which reads as
        # a failure; and the next seed writes a fresh one anyway.
        try:
            manifest.path.unlink()
            self.stdout.write(f'Manifest consumed and removed: {manifest.path}')
        except OSError as exc:
            self.stdout.write(self.style.WARNING(
                f'Rows deleted, but the manifest could not be removed '
                f'({manifest.path}): {exc}. Delete it by hand.'))

        self.stdout.write(self.style.SUCCESS(
            'Teardown complete — hard delete, no is_deleted flag was set.'))

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _resolve(label):
        try:
            app_label, model_name = label.split('.', 1)
            return apps.get_model(app_label, model_name)
        except (ValueError, LookupError):
            return None

    @staticmethod
    def _tally(pairs):
        counts = {}
        for _model, row in pairs:
            counts[row['model']] = counts.get(row['model'], 0) + 1
        return sorted(counts.items())

    @staticmethod
    def _storage_targets(present):
        """(label, bucket, path) for every storage object the manifest's own rows own.

        Read off manifest rows only — never by listing the bucket — so the safety
        property the row deletion has is the property the storage deletion has too.
        """
        from projects.models import DesignAssignment, DesignFile

        file_pks   = [r['pk'] for m, r in present if m is DesignFile]
        assign_pks = [r['pk'] for m, r in present if m is DesignAssignment]

        targets = []
        for f in (DesignFile.objects.filter(pk__in=file_pks)
                  .select_related('attempt__assignment__project')
                  .order_by('attempt__assignment__project__project_id', 'kind',
                            'version')):
            targets.append((
                f'{f.attempt.assignment.project.project_id} {f.kind} v{f.version}',
                f.bucket, f.path))
        for a in (DesignAssignment.objects.filter(pk__in=assign_pks)
                  .select_related('project').order_by('project__project_id')):
            if a.survey_file_path:
                targets.append((f'{a.project.project_id} survey',
                                a.survey_file_bucket, a.survey_file_path))
        return targets
