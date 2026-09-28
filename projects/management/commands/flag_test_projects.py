"""
Management command: set or clear Project.is_test for every project whose project_id
starts with one of the given prefixes.

    python manage.py flag_test_projects --prefix SCMPILOT --prefix ORDDEMO            # dry run
    python manage.py flag_test_projects --prefix SCMPILOT --prefix ORDDEMO --apply    # flag
    python manage.py flag_test_projects --prefix SCMPILOT --apply --unflag            # unflag

NO PREFIX IS HARDCODED HERE. Decision D4: test data is identified by the flag, and the
flag is set by an operator who names the prefixes on the command line. A list baked into
code would be exactly the name rule D4 rules out — it would silently flag the next real
tender whose code happened to share a prefix.

Matches on project_id (case-sensitive startswith), not site_code: the TESTTENDER26 sites
have no site_code at all. Soft-deleted projects that match are included — flagging them
is harmless and keeps them flagged if ever restored.
"""
from django.core.management.base import BaseCommand
from django.db.models import Q

from projects.models import Project


class Command(BaseCommand):
    help = 'Flag (or --unflag) projects whose project_id starts with a --prefix as test data. Dry run unless --apply.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--prefix', action='append', required=True, dest='prefixes',
            help='project_id prefix, case-sensitive. Repeat for several.',
        )
        parser.add_argument('--apply', action='store_true', help='Write the change. Without it: dry run.')
        parser.add_argument('--unflag', action='store_true', help='Set is_test=False instead of True.')

    def handle(self, *args, prefixes, apply, unflag, **options):
        target = not unflag

        # OR of one startswith per prefix — every project any prefix names, each once.
        match = Q()
        for prefix in prefixes:
            match |= Q(project_id__startswith=prefix)
        matched = (
            Project.objects.filter(match)
            .select_related('program')   # the program name is printed on every line
            .order_by('project_id')
        )

        # Re-checked in Python because __startswith is case-sensitive on Postgres but not
        # on SQLite (LIKE ignores ASCII case there), and the promise is case-sensitive on
        # every backend. On Postgres this drops nothing.
        rows = [p for p in matched if p.project_id.startswith(tuple(prefixes))]
        mode = 'APPLY' if apply else 'DRY RUN'
        self.stdout.write(f'{mode}: {len(rows)} project(s) match {prefixes}; is_test -> {target}')
        for p in rows:
            program = p.program.name if p.program else '-'
            self.stdout.write(f'{p.pk} | {p.project_id} | {p.project_type} | {p.status} | {program}')

        if not apply:
            self.stdout.write('Dry run: nothing changed. Re-run with --apply to write.')
            return

        # filter().update() on the pks just listed, so the rows written are exactly the
        # rows printed. No race concern: is_test is written only here and in the Django
        # admin, and a concurrent admin edit to the same flag would be last-writer-wins
        # on a boolean either way. update() skips save(), which is intended — no signal
        # or StatusTransition belongs to a test-data flag.
        updated = Project.objects.filter(pk__in=[p.pk for p in rows]).update(is_test=target)
        self.stdout.write(f'Updated {updated}')
