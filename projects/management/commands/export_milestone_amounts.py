"""FIN-M-AB, written 2026-11-20. Write the workbook Finance fills with the agreed M1-M3
amounts of existing Residential sites. Read-only: it writes a file, never the database.

    python manage.py export_milestone_amounts --out milestone_amounts.xlsx
    python manage.py export_milestone_amounts --out bd_check.xlsx --by-bd

One row per site in milestone_amount_import.import_scope_projects() (non-deleted,
non-test Residential, not Cancelled, not Draft), ordered by code — or with --by-bd by BD
name then code, sites with no BD last, so each BD can be sent their rows to check.

Columns: see milestone_amount_import.COLUMNS. The current and received columns are for
reference only; the import reads project_code, M1_new, M2_new, M3_new and finance_note.
"""
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from projects.milestone_amount_import import export_rows, write_workbook


class Command(BaseCommand):
    help = ('FIN-M-AB: write the milestone amounts workbook (.xlsx) for Finance to fill. '
            'Read-only.')

    def add_arguments(self, parser):
        parser.add_argument('--out', required=True, metavar='FILE.xlsx',
                            help='Where to write the workbook.')
        parser.add_argument('--by-bd', action='store_true',
                            help='Sort by BD then code (the BD check list).')

    def handle(self, *args, **options):
        out = Path(options['out'])
        if out.suffix.lower() != '.xlsx':
            raise CommandError(f'--out must end in .xlsx (got {out.name}). Nothing was written.')
        db = settings.DATABASES['default']
        self.stdout.write(f"Database: {db.get('HOST') or '(local)'} / {db.get('NAME')}")
        rows = export_rows(by_bd=options['by_bd'])
        write_workbook(rows, out)
        self.stdout.write(f'Rows: {len(rows)}')
        self.stdout.write(f'Written: {out.resolve()}')
