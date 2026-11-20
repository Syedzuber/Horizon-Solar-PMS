"""FIN-M-AB (ruling FR23), written 2026-11-20. Load the agreed M1-M3 amounts Finance
filled into the workbook from export_milestone_amounts.

    python manage.py import_milestone_amounts milestone_amounts.xlsx          # dry run
    python manage.py import_milestone_amounts milestone_amounts.xlsx --apply --actor santosh

The rules (blank = no change, never overwrite, errors / conflicts / warnings, the contract
check) live in projects/milestone_amount_import.py and are described there.

ALL OR NOTHING. If the file has any error, --apply writes nothing and says so; conflicts
and warnings do not block. Every write is one transaction.

ACTOR. --actor is the person the log lines name — Finance, who filled the sheet. They
must pass permissions.user_can_import_milestone_amounts (Finance, CEO, Admin or System
Admin, profile and login active), checked before the file is even read.

SAFE TO RE-RUN. Only blank amounts are written, so a second --apply with the same file
reports SET 0.
"""
from django.conf import settings
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError

from projects.milestone_amount_import import (
    ALREADY_SET, CONFLICT, IMPORTED_CODE, NOT_FILLED, SET, ImportFileError, apply_plan,
    plain, plan_import, read_workbook, sites_still_without_amount, summarise,
)
from projects.models import ActivityLog, UserProfile
from projects.permissions import user_can_import_milestone_amounts


class Command(BaseCommand):
    help = ('FIN-M-AB: load M1-M3 agreed amounts from Finance\'s workbook. Dry run unless '
            '--apply --actor <username>.')

    def add_arguments(self, parser):
        parser.add_argument('path', metavar='FILE.xlsx', help='The filled workbook.')
        parser.add_argument('--apply', action='store_true',
                            help='Write the amounts. Without it nothing is written.')
        parser.add_argument('--actor', metavar='USERNAME',
                            help='The Finance user the log lines name. Required with --apply.')

    def handle(self, *args, **options):
        apply = options['apply']
        db = settings.DATABASES['default']
        self.stdout.write(f"Database: {db.get('HOST') or '(local)'} / {db.get('NAME')}")
        self.stdout.write('MODE: APPLY' if apply else 'MODE: DRY RUN (nothing will be written)')
        if apply and not options['actor']:
            raise CommandError('--apply requires --actor <username>. Nothing was written.')
        actor = self._actor(options['actor']) if options['actor'] else None

        try:
            sheet_rows = read_workbook(options['path'])
        except ImportFileError as exc:
            raise CommandError(str(exc))

        plans = plan_import(sheet_rows)
        has_errors = any(plan.errors for plan in plans)

        # The highest ActivityLog pk before the write: rows above it with the import's
        # code are this run's, whatever else is logging at the same time.
        baseline = ActivityLog.objects.order_by('-pk').values_list('pk', flat=True).first() or 0
        written = 0
        if apply and not has_errors:
            written = apply_plan(plans, actor)

        for plan in plans:
            self._print_plan(plan, apply and not has_errors)

        totals = summarise(plans)
        self.stdout.write('')
        self.stdout.write(
            # A refused --apply wrote nothing, so it reports SET 0 whatever was planned.
            f"{'SET' if apply else 'WOULD SET'} {0 if apply and has_errors else totals['set']}, "
            f"ALREADY SET {totals['already_set']}, NOT FILLED {totals['not_filled']}, "
            f"CONFLICTS {totals['conflicts']}, ERRORS {totals['errors']}, "
            f"WARNINGS {totals['warnings']}, ROWS {totals['rows']}")

        if apply and not has_errors:
            logged = ActivityLog.objects.filter(pk__gt=baseline,
                                                action_code=IMPORTED_CODE).count()
            self.stdout.write(f'ActivityLog rows written: {logged} (expected {written})')
            if logged != written:
                self.stdout.write(self.style.WARNING(
                    f'WARNING: {written} amounts set but {logged} ActivityLog rows found. '
                    f'The amounts stand; the audit trail is incomplete. Check the '
                    f'application log for "ActivityLog failed".'))
            self.stdout.write(f'Still without amount: {sites_still_without_amount()}')
        elif has_errors:
            self.stdout.write(f'Still without amount: {sites_still_without_amount()} '
                              f'(nothing can be applied until the errors are fixed)')
        else:
            self.stdout.write(f'Still without amount (if applied): '
                              f'{sites_still_without_amount() - totals["set"]}')

        if has_errors:
            message = (f"{totals['errors']} error(s) in the file. "
                       f"{'Nothing was written. ' if apply else ''}"
                       f"--apply writes nothing until every error is fixed.")
            if apply:
                raise CommandError(message)
            self.stdout.write(self.style.WARNING(message))

    # -- output ---------------------------------------------------------------

    def _print_plan(self, plan, applied):
        code = plan.sheet.code or f'(row {plan.sheet.line})'
        # '-' where a message concerns the whole row: plain ASCII, because a piped
        # Windows console prints an em dash as a replacement character.
        for name, message in plan.errors:
            self.stdout.write(f'{code} | {name or "-"} | error: {message}')
        if plan.errors:
            return
        for item in plan.items:
            if item.outcome == SET:
                text = f'{"set" if applied else "would set"} {plain(item.new)}'
            elif item.outcome == ALREADY_SET:
                text = 'already set'
            elif item.outcome == NOT_FILLED:
                text = 'not filled'
            elif item.outcome == CONFLICT:
                text = f'conflict: has {plain(item.current)}, sheet {plain(item.new)}'
            self.stdout.write(f'{code} | {item.name} | {text}')
        for name, message in plan.warnings:
            self.stdout.write(f'{code} | {name or "-"} | warning: {message}')

    # -- actor ----------------------------------------------------------------

    def _actor(self, username):
        """The UserProfile the log lines name. Must exist, be active on both models and
        pass user_can_import_milestone_amounts. Checked before the file is read."""
        user = User.objects.filter(username=username).first()
        if user is None:
            raise CommandError(f'--actor {username!r}: no such user. Nothing was written.')
        try:
            profile = user.profile
        except UserProfile.DoesNotExist:
            raise CommandError(f'--actor {username!r} has no UserProfile. Nothing was written.')
        # Finance, CEO, Admin or System Admin, login and profile both active: the people
        # who may load agreed amounts. BD is refused — BD checks the figures afterwards.
        if not user_can_import_milestone_amounts(user):
            raise CommandError(f'--actor {username!r} ({profile.role or "no role"}, login '
                               f'{"active" if user.is_active else "inactive"}, profile '
                               f'{"active" if profile.is_active else "inactive"}) may not '
                               f'import milestone amounts: Finance, CEO, Admin or System '
                               f'Admin, active, only. Nothing was written.')
        return profile
