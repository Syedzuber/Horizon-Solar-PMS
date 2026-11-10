"""
READ-ONLY check of the Site Dashboard data layer against the per-site reference functions.

    python manage.py dashboard_data_check                   # as the first active Admin
    python manage.py dashboard_data_check --viewer praveen  # as a named user

Builds every card with site_dashboard.build_site_cards() and compares each visible site,
one at a time, against the function the card must agree with:

    plan slip        scheduling.site_plan_summary(project)
    schedule reason  scheduling.schedule_site(project, apply=False).reason   (dry run)
    progress %       REFERENCE COPY of dashboard_pm's internal_percent queries
    overdue, blocked REFERENCE COPY of dashboard_pm's urgency aggregate
    current phase    utils.current_phase(project)
    latest activity  the newest ActivityLog row passing is_human_activity() AND, for a money
                     code, permissions.user_can_see_project_money(viewer, site), read
                     row by row: the per-site predicate, against the card's queryset form

Prints one line per mismatch, then totals (sites, mismatches per field, the card build's
query count and wall time) and exits non-zero on any mismatch.

WRITES NOTHING. schedule_site() is called with apply=False, which plans and returns; it
opens no transaction and logs nothing. No login, no request, so no login-signal row.
"""
import sys
import time

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.db.models import Count, Prefetch, Q
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from projects.activity_codes import MONEY_ACTIVITY_CODES
from projects.models import ActivityLog, Project, ProjectPhase, Task
from projects.permissions import user_can_see_project_money
from projects.scheduling import schedule_site, site_plan_summary
from projects.site_dashboard import (REASON_SCHEDULED, build_site_cards,
                                     is_human_activity)
from projects.task_health import overdue_q
from projects.utils import (applicable_tasks_q, current_phase, human_owned_tasks_q,
                            site_progress_tasks_q)


def _reference_pm_card(project, today):
    """REFERENCE ONLY — a copy of views.dashboard_pm's per-project queries, kept verbatim
    so the card is checked against what the PM dashboard shows today (ruling 2). Do not
    reuse: the named rules live in site_dashboard (card_progress_q, card_overdue_q,
    card_blocked_q). Returns (internal_percent, overdue_count, blocked_count)."""
    internal_overdue = Q(task_type=Task.INTERNAL) & overdue_q(today)
    internal_total = Task.objects.filter(phase__project=project, task_type=Task.INTERNAL).filter(site_progress_tasks_q()).filter(applicable_tasks_q()).count()
    internal_done = Task.objects.filter(phase__project=project, task_type=Task.INTERNAL, status='Done').filter(site_progress_tasks_q()).filter(applicable_tasks_q()).count()
    internal_percent = int(internal_done / internal_total * 100) if internal_total else 0
    _urgency = (
        Task.objects.filter(phase__project=project)
        .filter(human_owned_tasks_q()).filter(applicable_tasks_q())
        .aggregate(
            overdue=Count('pk', filter=internal_overdue),
            blocked=Count('pk', filter=Q(status=Task.BLOCKED)),
        )
    )
    return internal_percent, _urgency['overdue'], _urgency['blocked']


def _reference_latest(project, viewer):
    """The newest ActivityLog row of the site that `viewer` would see as latest activity,
    read row by row, newest first: is_human_activity() accepts it, and a money row only
    when user_can_see_project_money(viewer, project) is True, the per-site predicate the
    project overview's Recent activity applies (ruling 3). The card applies the same rule
    as one queryset (site_dashboard._money_rows_q), so this checks the two agree."""
    sees_money = user_can_see_project_money(viewer, project)
    for row in ActivityLog.objects.filter(project=project).order_by('-timestamp', '-id'):
        if not is_human_activity(row.action_code):
            continue
        # A money row the viewer may not see is skipped, as the card's feed skips it.
        if row.action_code in MONEY_ACTIVITY_CODES and not sees_money:
            continue
        return (row.timestamp, row.action)
    return None


class Command(BaseCommand):
    help = 'Read-only: compare Site Dashboard cards with the per-site reference functions.'

    def add_arguments(self, parser):
        parser.add_argument('--viewer', help='username to build the cards as and to apply '
                            'the money filter for (default: first active Admin)')

    def handle(self, *args, **opts):
        if opts['viewer']:
            user = User.objects.filter(username=opts['viewer']).first()
            if user is None:
                raise CommandError(f"no user {opts['viewer']!r}")
        else:
            user = (User.objects.filter(profile__role='Admin', profile__is_active=True,
                                        is_active=True).order_by('pk').first())
            if user is None:
                raise CommandError('no active Admin user; pass --viewer')
        # Load the profile before timing, so the measured count is the build's own.
        _ = getattr(user, 'profile', None)
        today = timezone.localdate()

        with CaptureQueriesContext(connection) as captured:
            started = time.perf_counter()
            cards = build_site_cards(user, today=today)
            wall = time.perf_counter() - started

        # The reference reads: each site with its phases and tasks prefetched, because
        # utils.current_phase() walks project.phases.all() / phase.tasks.all().
        projects = {p.pk: p for p in Project.objects.filter(pk__in=[c.id for c in cards])
                    .prefetch_related(Prefetch(
                        'phases', queryset=ProjectPhase.objects.prefetch_related('tasks')
                        .order_by('phase_order')))}

        fields = ('plan', 'reason', 'progress', 'overdue', 'blocked', 'current_phase', 'latest')
        mismatches = {f: 0 for f in fields}

        def report(card, name, card_value, reference):
            mismatches[name] += 1
            self.stdout.write(f'MISMATCH {card.project_id:<18} {name:<14} '
                              f'card={card_value!r} reference={reference!r}')

        for card in cards:
            project = projects[card.id]
            summary = site_plan_summary(project)
            ref_plan = (None if summary is None else
                        (summary.baseline_finish, summary.forecast_finish, summary.days_behind))
            got_plan = ((card.schedule.baseline_finish, card.schedule.forecast_finish,
                         card.schedule.days_behind)
                        if card.schedule.baseline_finish is not None else None)
            if got_plan != ref_plan:
                report(card, 'plan', got_plan, ref_plan)

            ref_reason = schedule_site(project, apply=False).reason or REASON_SCHEDULED
            if card.schedule.reason != ref_reason:
                report(card, 'reason', card.schedule.reason, ref_reason)

            ref_pct, ref_overdue, ref_blocked = _reference_pm_card(project, today)
            if card.progress != ref_pct:
                report(card, 'progress', card.progress, ref_pct)
            if card.overdue != ref_overdue:
                report(card, 'overdue', card.overdue, ref_overdue)
            if card.blocked != ref_blocked:
                report(card, 'blocked', card.blocked, ref_blocked)

            ref_phase = None
            if project.status != 'Draft':
                phase = current_phase(project)
                ref_phase = phase.phase_name if phase else None
            if card.current_phase != ref_phase:
                report(card, 'current_phase', card.current_phase, ref_phase)

            ref_latest = _reference_latest(project, user)
            got_latest = (card.latest.at, card.latest.what) if card.latest else None
            if got_latest != ref_latest:
                report(card, 'latest', got_latest, ref_latest)

        total = sum(mismatches.values())
        self.stdout.write('')
        self.stdout.write(f'viewer: {user.username} ({getattr(user.profile, "role", None)})  today: {today}')
        self.stdout.write(f'sites checked: {len(cards)}')
        for name in fields:
            self.stdout.write(f'  mismatches {name:<14} {mismatches[name]}')
        self.stdout.write(f'build_site_cards: {len(captured.captured_queries)} queries, {wall:.2f}s wall')
        if total:
            self.stdout.write(self.style.ERROR(f'{total} mismatch(es)'))
            sys.exit(1)
        self.stdout.write(self.style.SUCCESS('no mismatches'))
