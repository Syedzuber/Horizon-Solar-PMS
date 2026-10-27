"""Extract the PDFs inside each current CAD zip into their own private objects (DR-1).

    python manage.py extract_cad_pdfs            # dry run: lists, writes nothing
    python manage.py extract_cad_pdfs --apply    # downloads, extracts, records

The backfill for design_artifact_upload, which extracts at upload time: zips uploaded
before DR-1, and any upload whose extraction failed (DR-9), have no DesignFilePdf rows
until this runs. Safe to re-run: an entry that already has a row is never attempted
again, and a storage failure records nothing, so the next run retries it.

DRY RUN BY DEFAULT, AND IT IS READ-ONLY BY CONSTRUCTION: SELECTs only, no storage call,
no activity log. What it would extract is read from each zip's archive_listing; whether
an entry really is a PDF is only known once --apply reads its bytes.

ONE PASS PER STORED OBJECT. Current cad_zip rows are resolved through cad_pdf_source() and
de-duplicated, so a carried copy and its original are one zip, downloaded and extracted
once, and the PDFs attach to the original.

NEVER DELETES ANYTHING, rows or objects. Prints the database host and the storage host
first, never a password or key.

A FAILURE GOES TO STDERR AND NOWHERE ELSE — no ActivityLog row. The upload path records
one (DR-9) because a designer acted and the site's feed should say the extraction did
not happen. A command run is an operator retrying a backfill: run against unreachable
storage it would add a row per zip per run to every site's feed, saying the same thing.
"""
import io
from urllib.parse import urlparse

from django.conf import settings
from django.core.management.base import BaseCommand

from projects.design_storage import (
    DesignStorageError, cad_pdf_source, cad_zip_pdf_entries, download_design_file,
    extract_cad_pdfs, unrecorded_cad_pdf_entries,
)
from projects.models import DESIGN_FILE_CAD_ZIP, DesignFile


class Command(BaseCommand):
    help = ('Extract the PDFs inside current CAD zips into their own private objects. '
            'Dry run unless --apply.')

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Download each zip, extract its PDFs and record them.')

    def handle(self, *args, **options):
        apply = options['apply']
        db = settings.DATABASES.get('default', {})
        # HOST and NAME only: the same dict holds PASSWORD, and it is never read here.
        self.stdout.write(f"Database host: {db.get('HOST') or '(local socket)'}  "
                          f"name: {db.get('NAME') or '(unset)'}")
        self.stdout.write(f'Storage host: {urlparse(settings.SUPABASE_URL or "").hostname}')
        self.stdout.write('Mode: APPLY' if apply else 'Mode: DRY RUN (nothing is written)')

        # Every CURRENT cad_zip on a live site. Superseded versions are not extracted: no
        # screen will offer them. The site's soft delete is filtered through the relation.
        current = (DesignFile.objects
                   .filter(kind=DESIGN_FILE_CAD_ZIP, is_current=True,
                           attempt__assignment__project__is_deleted=False)
                   .select_related('attempt__assignment__project')
                   .order_by('attempt__assignment__project__project_id', 'pk'))

        sources, seen = [], set()
        for design_file in current:
            try:
                source = cad_pdf_source(design_file)
            except DesignStorageError as exc:
                self.stderr.write(f'  DesignFile {design_file.pk}: skipped, {exc}')
                continue
            if source.pk not in seen:
                seen.add(source.pk)
                sources.append((design_file.attempt.assignment.project, source))

        todo = done = none_found = failed = written = 0
        for project, source in sources:
            label = f'{project.project_id} DesignFile {source.pk} ({source.path})'
            candidates = cad_zip_pdf_entries(source)
            pending = unrecorded_cad_pdf_entries(source)
            if not candidates:
                none_found += 1
                self.stdout.write(f'{label}: no .pdf entries')
                continue
            if not pending:
                done += 1
                self.stdout.write(f'{label}: done, {len(candidates)} recorded')
                continue

            todo += 1
            self.stdout.write(f'{label}: {len(pending)} of {len(candidates)} to extract')
            for name in pending:
                self.stdout.write(f'    {name!r}')
            if not apply:
                continue

            try:
                data = download_design_file(source.bucket, source.path)
                rows = extract_cad_pdfs(source, io.BytesIO(data))
            except Exception as exc:
                # Stderr only, no ActivityLog row: see the module docstring. Rows already
                # written for this zip stay, and the next run retries the rest.
                failed += 1
                self.stderr.write(f'    FAILED: {label}: {exc}')
                continue
            for row in rows:
                written += 1
                self.stdout.write(f'    -> {row.outcome:<10} {row.display_name!r} '
                                  f'{row.size_bytes} bytes {row.path or ""}')

        self.stdout.write(
            f'Zips: {len(sources)} | to extract: {todo} | done: {done} | '
            f'no PDFs: {none_found}'
            + (f' | rows written: {written} | failed: {failed}' if apply else ''))
