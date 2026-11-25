"""FIN-M-B5-5a (ruling FR37 option b), written 2026-11-25. Mark every milestone that was
Received before client receipts existed, so the receipt recompute never un-receives it.

    python manage.py mark_legacy_received_milestones                         # dry run
    python manage.py mark_legacy_received_milestones --apply --actor santosh

WHAT IS MARKED. Live Residential milestones (site not soft-deleted, R3) whose status is
Received and that have NO receipt row of any status. For each: amount_received is copied
to legacy_amount_received, received_date to legacy_received_date, and
received_before_receipts and legacy_marked_at are set. Status, amount received, received
date and tasks are NEVER changed; the milestone stays Received (FR37).

WHEN. On production, by Zuber, BEFORE CLIENT_RECEIPTS_LIVE is turned on. It may also run
with the switch on (ruling Q1, 25 Nov): it then warns and marks the milestones an old path
turned Received after the first run. Until marked, recompute leaves such a milestone
alone.

ACTOR (ruling 25 Nov). --actor must pass permissions.user_can_import_milestone_amounts —
Finance, CEO, Admin or System Admin, login and profile active — the same people who may
load agreed amounts with import_milestone_amounts. Checked before anything is read.

SAFE TO RE-RUN. An already marked milestone is counted ALREADY and left alone; a second
--apply marks 0. Each marked milestone writes one ActivityLog line (code
milestone_legacy_marked: a money code, not site activity). The summary ends with
"rows written: n (expected n)", the log rows actually written against those planned.

REPORT ONLY (ruling Q5). "HAS CONFIRMED RECEIPTS, NOT RECOMPUTED" lists live Residential
milestones that already have a confirmed receipt — confirmed while the switch was off,
when recompute did nothing. Expected 0 before go-live. This command never recomputes them.
"""
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Count
from django.utils import timezone

from projects.models import ActivityLog, PaymentMilestone, PaymentReceipt, UserProfile, log_activity
from projects.permissions import user_can_import_milestone_amounts
from projects.receipts import receipts_live
# The milestone log's own formatters, so this line reads like every other milestone line
# ("₹1,50,000", "5 Nov 2026", "blank"). Module level is safe: views does not import this.
from projects.views import _milestone_day, _milestone_rupees

CODE = 'milestone_legacy_marked'
MILESTONE_ENTITY = 'Milestone'

# Outcomes, in the order a milestone is tested.
DELETED = 'deleted site'
NOT_RESIDENTIAL = 'not Residential'
ALREADY = 'already marked'
NOT_RECEIVED = 'not Received'   # broken down by status in the summary
HAS_RECEIPTS = 'has receipts'
MARK = 'mark'


def classify(milestone, receipt_rows):
    """The outcome for one milestone (project selected with it), given how many receipt
    rows of any status it has."""
    if milestone.project.is_deleted:
        return DELETED
    if milestone.project.project_type != 'Residential':
        return NOT_RESIDENTIAL
    if milestone.received_before_receipts:
        return ALREADY
    if milestone.status != PaymentMilestone.RECEIVED:
        return NOT_RECEIVED
    if receipt_rows:
        return HAS_RECEIPTS
    return MARK


def plain_amount(value):
    """ASCII for the console (a piped Windows console mangles the rupee sign)."""
    return 'blank' if value is None else f'{value}'


class Command(BaseCommand):
    help = ('FIN-M-B5-5a: mark milestones Received before receipts existed. Dry run unless '
            '--apply --actor <username>.')

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Mark the milestones. Without it nothing is written.')
        parser.add_argument('--actor', metavar='USERNAME',
                            help='The person the log lines name. Required with --apply.')

    def handle(self, *args, **options):
        apply = options['apply']
        actor = None
        if options['actor']:
            actor = self._actor(options['actor'])
        elif apply:
            raise CommandError('--apply needs --actor <username>. Nothing was written.')
        if receipts_live():
            # Ruling Q1: allowed, so late milestones can be marked after go-live.
            self.stdout.write(self.style.WARNING('switch is on: marking late milestones'))

        plan = self._plan()
        marked = 0
        log_rows = 0
        if apply and plan[MARK]:
            marked, log_rows = self._apply(plan[MARK], actor)
        self._print(plan, apply, marked, log_rows)

    # -- plan -----------------------------------------------------------------

    def _plan(self):
        """{outcome: [milestone, ...]} over every milestone, plus the not-Received split by
        status and the report-only list. Three queries, whatever the number of rows."""
        rows = {m['milestone_id']: m['n'] for m in
                PaymentReceipt.objects.values('milestone_id')
                .annotate(n=Count('pk'))
                .order_by()}
        plan = {key: [] for key in (DELETED, NOT_RESIDENTIAL, ALREADY, NOT_RECEIVED,
                                    HAS_RECEIPTS, MARK)}
        by_status = {}
        checked = 0
        for m in PaymentMilestone.objects.select_related('project').order_by('pk'):
            checked += 1
            outcome = classify(m, rows.get(m.pk, 0))
            plan[outcome].append(m)
            if outcome == NOT_RECEIVED:
                by_status[m.status] = by_status.get(m.status, 0) + 1
        plan['checked'] = checked
        plan['by_status'] = by_status
        # Ruling Q5: report only, never recomputed here.
        plan['confirmed'] = list(
            PaymentMilestone.objects.filter(
                project__is_deleted=False, project__project_type='Residential',
                receipts__status=PaymentReceipt.CONFIRMED)
            .distinct().order_by('pk').values_list('pk', flat=True))
        return plan

    # -- the write ------------------------------------------------------------

    def _apply(self, to_mark, actor):
        """Mark every planned milestone in ONE transaction; return (marked, log rows).

        Each row is re-read under select_for_update, in pk order so two runs cannot
        deadlock, and marked only if it still qualifies: a receipt recorded or a status
        changed between the plan and this write leaves that milestone unmarked (it is
        then reported as not marked). The receipt check runs under the milestone lock,
        which every receipt writer takes before writing a receipt, so none can land
        between the check and the mark."""
        now = timezone.now()
        marked = 0
        before_logs = ActivityLog.objects.filter(action_code=CODE).count()
        with transaction.atomic():
            locked = list(PaymentMilestone.objects.select_for_update()
                          .filter(pk__in=[m.pk for m in to_mark]).order_by('pk'))
            for milestone in locked:
                has_rows = PaymentReceipt.objects.filter(milestone=milestone).exists()
                if (milestone.received_before_receipts
                        or milestone.status != PaymentMilestone.RECEIVED or has_rows):
                    self.stdout.write(f'pk {milestone.pk} | changed since the plan | not marked')
                    continue
                milestone.received_before_receipts = True
                milestone.legacy_amount_received = milestone.amount_received
                milestone.legacy_received_date = milestone.received_date
                milestone.legacy_marked_at = now
                milestone.save(update_fields=['received_before_receipts',
                                              'legacy_amount_received',
                                              'legacy_received_date', 'legacy_marked_at'])
                # A money line (the old amount) and not site activity: both sets list
                # the code (activity_codes, ruling Q6).
                log_activity(
                    milestone.project, actor,
                    f'{milestone.milestone_name}: marked as received before receipts '
                    f'(old amount {_milestone_rupees(milestone.amount_received)}, '
                    f'received {_milestone_day(milestone.received_date)})',
                    entity_type=MILESTONE_ENTITY, entity_id=milestone.pk,
                    action_code='milestone_legacy_marked')
                marked += 1
        log_rows = ActivityLog.objects.filter(action_code=CODE).count() - before_logs
        return marked, log_rows

    # -- output ---------------------------------------------------------------

    def _print(self, plan, applied, marked, log_rows):
        verb = 'marked' if applied else 'would mark'
        for m in plan[MARK]:
            self.stdout.write(
                f'{m.project.project_id} | {m.milestone_name} | pk {m.pk} | {verb} | '
                f'old amount {plain_amount(m.amount_received)} | received '
                f'{_milestone_day(m.received_date)}')
        for m in plan[HAS_RECEIPTS]:
            self.stdout.write(f'{m.project.project_id} | {m.milestone_name} | pk {m.pk} | '
                              f'skipped: Received but has receipts')
        with_amount = [m for m in plan[MARK] if m.amount_received is not None]
        total = sum(m.amount_received for m in with_amount)
        by_name = {}
        for m in plan[MARK]:
            by_name[m.milestone_name] = by_name.get(m.milestone_name, 0) + 1
        self.stdout.write('')
        self.stdout.write(f'{"MARKED" if applied else "WOULD MARK"} '
                          f'{marked if applied else len(plan[MARK])} '
                          f'(planned: ' + (', '.join(f'{k} {v}' for k, v in sorted(by_name.items()))
                                           or 'none')
                          + f'; with old amount {len(with_amount)}, sum {total}; '
                          f'without {len(plan[MARK]) - len(with_amount)})')
        self.stdout.write(f'ALREADY {len(plan[ALREADY])}')
        skipped = (len(plan[DELETED]) + len(plan[NOT_RESIDENTIAL]) + len(plan[NOT_RECEIVED])
                   + len(plan[HAS_RECEIPTS]))
        status_words = ', '.join(f'{k} {v}' for k, v in sorted(plan['by_status'].items()))
        self.stdout.write(
            f'SKIPPED {skipped} by reason: {DELETED} {len(plan[DELETED])}, '
            f'{NOT_RESIDENTIAL} {len(plan[NOT_RESIDENTIAL])}, '
            f'{NOT_RECEIVED} {len(plan[NOT_RECEIVED])}'
            + (f' ({status_words})' if status_words else '')
            + f', {HAS_RECEIPTS} {len(plan[HAS_RECEIPTS])}')
        self.stdout.write(f'CHECKED {plan["checked"]}')
        confirmed = plan['confirmed']
        self.stdout.write(f'HAS CONFIRMED RECEIPTS, NOT RECOMPUTED: {len(confirmed)}'
                          + (f' (pks {", ".join(str(pk) for pk in confirmed)})'
                             if confirmed else ''))
        self.stdout.write(f'rows written: {log_rows} (expected {marked if applied else 0})')
        if not applied:
            self.stdout.write('Dry run: nothing was written. Add --apply --actor <username>.')

    # -- actor ----------------------------------------------------------------

    def _actor(self, username):
        """The UserProfile the log lines name. Must exist and pass
        user_can_import_milestone_amounts (ruling 25 Nov, as the import command)."""
        user = User.objects.filter(username=username).first()
        if user is None:
            raise CommandError(f'--actor {username!r}: no such user. Nothing was written.')
        try:
            profile = user.profile
        except UserProfile.DoesNotExist:
            raise CommandError(f'--actor {username!r} has no UserProfile. Nothing was written.')
        # Finance, CEO, Admin or System Admin, login and profile both active: marking old
        # money is a Finance data step, so the import's people (and only they) may run it.
        # Every other role, and an inactive user, is refused before anything is read.
        if not user_can_import_milestone_amounts(user):
            raise CommandError(f'--actor {username!r} ({profile.role or "no role"}, login '
                               f'{"active" if user.is_active else "inactive"}, profile '
                               f'{"active" if profile.is_active else "inactive"}) may not '
                               f'mark milestones: Finance, CEO, Admin or System Admin, '
                               f'active, only. Nothing was written.')
        return profile
