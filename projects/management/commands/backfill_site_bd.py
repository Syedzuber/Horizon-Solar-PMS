"""FIN-M-B5-1 (ruling FR7), written 2026-11-19. Fill each Residential site's BD
(Project.bd) from its "OCR, Documentation & Verification" task.

    python manage.py backfill_site_bd                               # dry run
    python manage.py backfill_site_bd --apply --actor <username>

WHAT IT DOES
------------
Project.bd is new; nothing stored a site's BD before it. The only signal is the
assignee of the Residential template's first task, OCR_DOCUMENTATION_VERIFICATION, which
BD has always held. For every in-scope site (site_bd.bd_scope_projects(): non-deleted,
non-test Residential, every status except Cancelled) whose BD is empty, the command sets
the OCR task's assignee as BD — only when that person is an ACTIVE BD (role BD, profile
and login both active). Otherwise the seat stays empty and the line says why:
OCR task held by a PM / by a non-BD role / by an inactive BD / unassigned / no OCR task
(a Draft has no tasks until activation) / several OCR tasks.

SAFE TO RE-RUN. A site whose BD is set — by an earlier run or by hand — is never
touched (ALREADY SET); a second --apply writes nothing. Each write goes through
site_bd.set_site_bd() with expected_old_id=None, so a BD set by an Admin between the
plan and the write is refused, not overwritten.

ACTOR. --actor must be someone who may set a BD from the overview
(permissions.user_can_set_site_bd: CEO, Admin or System Admin, profile and login both
active), so the command writes nothing the same person could not write by hand.

LOGGING. One ActivityLog line per site set, code `bd_backfilled` ("BD: blank → Name
(from the OCR task's assignee)"), naming --actor. That code is in NON_ACTIVITY_CODES,
so the fill never shows as a site's "latest activity". The run ends by counting the
rows it wrote against the sites it set.

AT THE END it prints the "No BD set" list (site_bd.sites_without_bd()) with a reason
per site — the sites to set by hand on the overview's BD line.
"""
from django.conf import settings
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError

from projects.models import ActivityLog, UserProfile
from projects.permissions import user_can_set_site_bd
from projects.site_bd import (
    BD_BACKFILLED, OUTCOME_ALREADY_SET, OUTCOME_SET, OUTCOME_SKIPPED,
    REASON_CHANGED_DURING_RUN, SiteBdChangeRefused, display_name, plan_bd_backfill,
    set_site_bd, sites_without_bd, stale_bd_reason, summarise,
)

NOTE = "from the OCR task's assignee"


class Command(BaseCommand):
    help = ("FIN-M-B5-1: set each Residential site's BD from its OCR task's assignee "
            "where that person is an active BD. Dry run unless --apply.")

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Write the changes. Without it nothing is written.')
        parser.add_argument('--actor', metavar='USERNAME',
                            help='The person the ActivityLog rows name. Required with --apply.')

    def handle(self, *args, **options):
        apply = options['apply']
        db = settings.DATABASES['default']
        self.stdout.write(f"Database: {db.get('HOST') or '(local)'} / {db.get('NAME')}")
        self.stdout.write('MODE: APPLY' if apply else 'MODE: DRY RUN (nothing will be written)')
        if apply and not options['actor']:
            raise CommandError('--apply requires --actor <username>. Nothing was written.')
        actor = self._actor(options['actor']) if options['actor'] else None

        # The highest ActivityLog pk before the run: rows above it with the backfill's
        # code are this run's, whatever else is logging at the same time.
        baseline = ActivityLog.objects.order_by('-pk').values_list('pk', flat=True).first() or 0

        rows = plan_bd_backfill()
        written = 0
        for row in rows:
            if row.outcome == OUTCOME_SET and apply:
                try:
                    set_site_bd(row.project, row.bd, actor, expected_old_id=None,
                                action_code=BD_BACKFILLED, note=NOTE)
                    written += 1
                except SiteBdChangeRefused:
                    # Someone set this site's BD after the plan was read; theirs stands.
                    row.outcome, row.reason = OUTCOME_SKIPPED, REASON_CHANGED_DURING_RUN
            self._print_row(row, apply)

        outcomes, reasons = summarise(rows)
        self.stdout.write('')
        reason_text = ', '.join(f'{name} {n}' for name, n in sorted(reasons.items())) or 'none'
        self.stdout.write(
            f"{'SET' if apply else 'WOULD SET'} {outcomes[OUTCOME_SET]}, "
            f'ALREADY SET {outcomes[OUTCOME_ALREADY_SET]}, '
            f'SKIPPED {outcomes[OUTCOME_SKIPPED]} ({reason_text}), '
            f'CHECKED {len(rows)}')

        logged = ActivityLog.objects.filter(pk__gt=baseline, action_code=BD_BACKFILLED).count()
        self.stdout.write(f'ActivityLog rows written: {logged} (expected {written})')
        if logged != written:
            self.stdout.write(self.style.WARNING(
                f'WARNING: {written} BDs set but {logged} ActivityLog rows found. The BDs '
                f'stand; the audit trail is incomplete. Check the application log for '
                f'"ActivityLog failed".'))

        self._print_no_bd_list(rows, apply)

    # -- output ---------------------------------------------------------------

    def _print_row(self, row, apply):
        site = row.project
        head = f'{site.project_id} | {site.status} | '
        if row.outcome == OUTCOME_SET:
            verb = 'set to' if apply else 'would set to'
            self.stdout.write(f'{head}{verb} {display_name(row.bd)} ({row.bd.user.username})')
        elif row.outcome == OUTCOME_ALREADY_SET:
            self.stdout.write(f'{head}already set: {display_name(row.bd)}')
        else:
            self.stdout.write(f'{head}skipped: {row.reason}')

    def _print_no_bd_list(self, rows, apply):
        """The "No BD set" list as it stands now — after the writes on --apply. On a dry
        run the sites the run WOULD set are left off, so the list is the same one an
        --apply would end with."""
        would_set = ({r.project.pk for r in rows if r.outcome == OUTCOME_SET}
                     if not apply else set())
        reasons = {r.project.pk: r.reason for r in rows if r.outcome == OUTCOME_SKIPPED}
        sites = [s for s in sites_without_bd() if s.pk not in would_set]
        self.stdout.write('')
        label = 'after this run' if apply else 'if applied'
        self.stdout.write(f'No BD set ({label}): {len(sites)}. Set these by hand on the '
                          f"site's overview")
        for site in sites:
            reason = (stale_bd_reason(site) if site.bd_id is not None
                      else reasons.get(site.pk, 'no BD set'))
            self.stdout.write(f'  {site.project_id} | {site.status} | {reason}')

    # -- actor ----------------------------------------------------------------

    def _actor(self, username):
        """The UserProfile the log lines name. Must exist, be active on both models (the
        audit trail never names someone who has left) and pass user_can_set_site_bd —
        CEO, Admin or System Admin — the people who may set a BD by hand (follow-up
        ruling 2). Checked before anything is planned or written."""
        user = User.objects.filter(username=username).first()
        if user is None:
            raise CommandError(f'--actor {username!r}: no such user. Nothing was written.')
        if not user.is_active:
            raise CommandError(f'--actor {username!r} is inactive. Nothing was written.')
        try:
            profile = user.profile
        except UserProfile.DoesNotExist:
            raise CommandError(f'--actor {username!r} has no UserProfile. Nothing was written.')
        if not profile.is_active:
            raise CommandError(f'--actor {username!r} has an inactive profile. '
                               f'Nothing was written.')
        # CEO / Admin / System Admin only, the same rule as the overview's BD pencil: a
        # PM, BD or any other role may not set BDs by hand, so not by command either.
        if not user_can_set_site_bd(user):
            raise CommandError(f'--actor {username!r} ({profile.role or "no role"}) may not '
                               f'set a BD: CEO, Admin or System Admin only. '
                               f'Nothing was written.')
        return profile
