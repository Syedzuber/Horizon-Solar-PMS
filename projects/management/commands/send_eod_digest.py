"""
End-of-Day (EOD) digest email.

Sends each active user one daily summary of THEIR OWN activity today plus a snapshot
of their open workload. Intended to run once daily from a Railway Cron Job:

    python manage.py send_eod_digest

Metrics (all "today" figures are the user's own actions, keyed on ActivityLog.actor,
and depend on the action_code values shipped in the Audit Log Coverage work — never
string-matched free text):
  1. assigned         — open tasks where assigned_to = user, status != Done (snapshot)
  2. started          — distinct Tasks the user set In Progress today  (task_status_in_progress)
  3. closed           — distinct Tasks the user set Done today          (task_status_done)
  4. issues_raised    — issues the user created today                   (issue_created)
  5. issues_resolved  — issues the user resolved today                  (issue_resolved)

Efficiency: metrics are computed with grouped aggregate queries (one per metric across
ALL users), not a per-user loop hitting the DB 5x each — so batch size scales with the
number of active users, not users x metrics.

Sending reuses the notifications.py chokepoint (send_notification, channels=['email']),
so the global master switch (SystemSettings.email_enabled), each user's
email_notifications preference, and NotificationLog logging all apply automatically:
  - master switch off      -> logged 'skipped: Master switch off', nothing sent
  - email_notifications off -> logged 'skipped: User preference off'
  - no email address        -> logged 'failed: Recipient has no email address'
  - otherwise               -> logged 'sent' / 'failed'

Aggregate digest: after the per-user sends, ONE company-wide email (same 5 categories
summed across the whole team, no actor filter) goes to two fixed addresses —
settings.ADMIN_DIGEST_EMAIL and settings.HR_DIGEST_EMAIL. Gated only by the global master
switch (not a per-user preference; these aren't UserProfiles). If either address still
contains the "REPLACE_WITH" placeholder, the aggregate send is aborted with CommandError
AFTER the individual digests, so a misconfigured aggregate never blocks the per-user run.
Aggregate sends log to NotificationLog under template='eod_digest_aggregate' (vs
'eod_digest' for individual) when the address matches a real user account.

Options:
  --dry-run          compute + render everything but send nothing (prints a table + totals)
  --user <email>     restrict to a single user (pre-flight); also skips the aggregate send
  --date <YYYY-MM-DD> override "today" (IST) for back-testing against real activity
"""

import logging
from datetime import date as date_cls

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count
from django.template.loader import render_to_string
from django.utils import timezone

logger = logging.getLogger(__name__)

# action_code -> metric key. task_status_* only ever appear on Task events and issue_*
# only on Issue events, so filtering on action_code alone already implies entity_type.
CODE_TO_METRIC = {
    'task_status_in_progress': 'started',
    'task_status_done':        'closed',
    'issue_created':           'issues_raised',
    'issue_resolved':          'issues_resolved',
}


class Command(BaseCommand):
    help = 'Send each active user their End-of-Day activity digest email.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='Compute and render but do not send; print a summary table.')
        parser.add_argument('--user', type=str, default='',
                            help='Restrict to a single user by email address.')
        parser.add_argument('--date', type=str, default='',
                            help='Override the reporting date (IST), format YYYY-MM-DD.')

    def handle(self, *args, **options):
        from projects.models import Task, ActivityLog, UserProfile, Project, Issue
        from projects.notifications import send_notification, _log

        dry_run   = options['dry_run']
        only_email = options['user'].strip().lower()

        if options['date']:
            try:
                today = date_cls.fromisoformat(options['date'])
            except ValueError:
                raise CommandError(f"--date must be YYYY-MM-DD, got {options['date']!r}")
        else:
            today = timezone.localdate()  # IST calendar date (TIME_ZONE=Asia/Kolkata)

        app_url = getattr(settings, 'APP_BASE_URL',
                          'https://horizon-solar-pms-production.up.railway.app')

        # --- Recipients: active profiles of active users (deactivated users excluded) ---
        # Hard role exclusion (§1): CEO/Admin/System Admin never get an INDIVIDUAL digest —
        # they already receive the company-wide aggregate email below, so a personal one is
        # redundant. This is permanent, NOT activity-gated. BD is deliberately NOT here — it
        # goes through the open-work gating (§3) like every other role. The aggregate totals
        # are computed by a separate query (_company_totals) that ignores this exclusion, so
        # an excluded user's own actions still count company-wide.
        excluded_roles = getattr(settings, 'EOD_DIGEST_EXCLUDED_ROLES', [])
        recipients = list(
            UserProfile.objects
            .filter(is_active=True, user__is_active=True)
            .exclude(role__in=excluded_roles)
            .select_related('user')
            .order_by('user__first_name', 'user__username')
        )
        if only_email:
            recipients = [p for p in recipients if (p.user.email or '').lower() == only_email]

        recipient_pks = [p.pk for p in recipients]
        if not recipient_pks:
            self.stdout.write('No matching active recipients — nothing to do.')
            return

        # Coordinator recipients get a role-based template branch (§2) and their own gating
        # inputs (§3). Roles are mutually exclusive, so this is a clean branch, not an overlay.
        COORDINATOR_ROLE = 'Project Coordinator'
        coord_pks = {p.pk for p in recipients if p.role == COORDINATOR_ROLE}

        # --- Metric 1: open tasks assigned to each user (snapshot, grouped) ---
        assigned_map = dict(
            Task.objects
            .filter(assigned_to__in=recipient_pks)
            .exclude(status=Task.DONE)
            .values_list('assigned_to')
            .annotate(c=Count('id'))
            .values_list('assigned_to', 'c')
        )

        # --- Metrics 2-5: today's own-activity, grouped by actor + action_code ---
        # Count DISTINCT entity_id so a task toggled to the same status twice today
        # (or an issue resolved-reopened-resolved) counts once, not per event.
        activity_map = {}  # actor_pk -> {metric_key: count}
        rows = (
            ActivityLog.objects
            .filter(
                timestamp__date=today,
                actor__in=recipient_pks,
                action_code__in=CODE_TO_METRIC.keys(),
            )
            .values('actor', 'action_code')
            .annotate(c=Count('entity_id', distinct=True))
        )
        for row in rows:
            metric_key = CODE_TO_METRIC[row['action_code']]
            activity_map.setdefault(row['actor'], {})[metric_key] = row['c']

        # --- Non-coordinator gating input: does this user have any OPEN issue they are
        # assigned to OR raised (§3)? Only >0 matters, so we collect the set of pks with at
        # least one unresolved issue via two grouped lookups (assigned_to, then raised_by).
        # "Unresolved" = status not in (Resolved, Closed). Issue has raised_by, not created_by.
        UNRESOLVED = [Issue.RESOLVED, Issue.CLOSED]
        open_issue_users = set(
            Issue.objects.filter(assigned_to__in=recipient_pks)
            .exclude(status__in=UNRESOLVED)
            .values_list('assigned_to', flat=True)
        )
        open_issue_users.update(
            Issue.objects.filter(raised_by__in=recipient_pks)
            .exclude(status__in=UNRESOLVED)
            .values_list('raised_by', flat=True)
        )

        # --- Coordinator content + gating (§2/§3), grouped per coordinator. "Active" project
        # = not soft-deleted, activated (activated_at set, so Draft is excluded), and not
        # Cancelled — per the confirmed definition. All three maps key on the coordinator pk.
        coord_projects_map = {}   # coord_pk -> count of active coordinated projects
        coord_tasks_map    = {}   # coord_pk -> open tasks (status != Done) across those projects
        coord_issues_map   = {}   # coord_pk -> unresolved issues across those projects
        if coord_pks:
            active_proj = dict(
                project__is_deleted=False,
                project__activated_at__isnull=False,
            )
            # Projects you coordinate — distinct projects per coordinator.
            proj_rows = (
                Project.objects
                .filter(coordinators__in=coord_pks, is_deleted=False,
                        activated_at__isnull=False)
                .exclude(status='Cancelled')
                .values_list('coordinators')
                .annotate(c=Count('id', distinct=True))
            )
            for cpk, c in proj_rows:
                if cpk in coord_pks:
                    coord_projects_map[cpk] = c
            # Open tasks across coordinated active projects (status != Done).
            task_rows = (
                Task.objects
                .filter(phase__project__coordinators__in=coord_pks,
                        phase__project__is_deleted=False,
                        phase__project__activated_at__isnull=False)
                .exclude(phase__project__status='Cancelled')
                .exclude(status=Task.DONE)
                .values_list('phase__project__coordinators')
                .annotate(c=Count('id', distinct=True))
            )
            for cpk, c in task_rows:
                if cpk in coord_pks:
                    coord_tasks_map[cpk] = c
            # Open (unresolved) issues across coordinated active projects.
            issue_rows = (
                Issue.objects
                .filter(project__coordinators__in=coord_pks, **active_proj)
                .exclude(project__status='Cancelled')
                .exclude(status__in=UNRESOLVED)
                .values_list('project__coordinators')
                .annotate(c=Count('id', distinct=True))
            )
            for cpk, c in issue_rows:
                if cpk in coord_pks:
                    coord_issues_map[cpk] = c

        date_str = today.strftime('%d %b %Y')
        sent = errored = attempted = skipped = 0

        for profile in recipients:
            act = activity_map.get(profile.pk, {})
            is_coordinator = profile.pk in coord_pks
            metrics = {
                'assigned':        assigned_map.get(profile.pk, 0),
                'started':         act.get('started', 0),
                'closed':          act.get('closed', 0),
                'issues_raised':   act.get('issues_raised', 0),
                'issues_resolved': act.get('issues_resolved', 0),
                # Coordinator-only fields (§2). Zero/ignored for every other role.
                'coord_projects':    coord_projects_map.get(profile.pk, 0),
                'coord_open_tasks':  coord_tasks_map.get(profile.pk, 0),
                'coord_open_issues': coord_issues_map.get(profile.pk, 0),
            }

            # --- Open-work gating (§3) --------------------------------------------------
            # Primary trigger = open workload snapshot (independent of today's activity).
            # For a coordinator that's their coordinated projects' open work; for everyone
            # else it's their own open assigned tasks OR open issues (assigned/raised).
            if is_coordinator:
                has_open_work = (metrics['coord_open_tasks'] > 0
                                 or metrics['coord_open_issues'] > 0)
            else:
                has_open_work = (metrics['assigned'] > 0
                                 or profile.pk in open_issue_users)
            # "Today" metrics are ADDITIONAL OR-triggers, not a replacement — this keeps the
            # same-day-closure case sending (closed_today=1 even when open count is now 0).
            has_today_activity = (metrics['started'] or metrics['closed']
                                  or metrics['issues_raised'] or metrics['issues_resolved'])
            if not (has_open_work or has_today_activity):
                skipped += 1
                reason = 'skipped: no open tasks/issues, no activity today'
                if dry_run:
                    self.stdout.write(f"[dry-run][skip] {profile.user.get_full_name() or profile.user.username} — {reason}")
                else:
                    # Log the skip to NotificationLog, consistent with the send path's
                    # skip rows (channel='email', status='skipped', reason in error_detail).
                    _log(
                        dict(recipient=profile, related_project=None, actor=None,
                             message='', template_name='eod_digest'),
                        'email', 'skipped', reason,
                    )
                continue

            recipient_name = profile.user.get_full_name() or profile.user.username
            ctx = {
                'recipient_name': recipient_name,
                'date_str':       date_str,
                'metrics':        metrics,
                'app_url':        app_url,
                'is_coordinator': is_coordinator,
            }

            # Render both bodies now — surfaces template errors in dry-run too.
            try:
                html_body = render_to_string('projects/email/eod_digest.html', ctx)
                text_body = render_to_string('projects/email/eod_digest.txt', ctx)
            except Exception as exc:
                errored += 1
                logger.error('send_eod_digest: render failed for %s — %s', recipient_name, exc)
                self.stderr.write(f'RENDER FAIL {recipient_name}: {exc}')
                continue

            if dry_run:
                if is_coordinator:
                    self.stdout.write(
                        f"[dry-run] {recipient_name:<28} [coordinator] "
                        f"projects={metrics['coord_projects']} open_tasks={metrics['coord_open_tasks']} "
                        f"open_issues={metrics['coord_open_issues']} started={metrics['started']} "
                        f"closed={metrics['closed']} raised={metrics['issues_raised']} "
                        f"resolved={metrics['issues_resolved']} email={profile.user.email or '(none)'}"
                    )
                else:
                    self.stdout.write(
                        f"[dry-run] {recipient_name:<28} "
                        f"assigned={metrics['assigned']} started={metrics['started']} "
                        f"closed={metrics['closed']} raised={metrics['issues_raised']} "
                        f"resolved={metrics['issues_resolved']} "
                        f"email={profile.user.email or '(none)'}"
                    )
                continue

            attempted += 1
            # send_notification never raises — a failed send logs to NotificationLog and
            # returns. We still guard so one bad recipient can't abort the whole batch.
            try:
                send_notification(
                    recipient=profile,
                    message=text_body,
                    channels=['email'],
                    subject=f'Your Horizon Solar EOD Summary — {date_str}',
                    template='eod_digest',   # labels the NotificationLog row (email-only, no WhatsApp)
                    html_message=html_body,
                )
                sent += 1
            except Exception as exc:
                errored += 1
                logger.error('send_eod_digest: send failed for %s — %s', recipient_name, exc)
                self.stderr.write(f'SEND FAIL {recipient_name}: {exc}')

        if dry_run:
            self.stdout.write(
                f"Dry run complete for {len(recipient_pks)} recipient(s), date {date_str}. "
                f"{skipped} gated out (no open work / no activity). Nothing sent.")
        else:
            summary = (f"EOD digest ({date_str}): {attempted} attempted, {sent} handed to sender, "
                       f"{skipped} gated out, {errored} errored. "
                       f"Per-channel outcome (sent/skipped/failed) in NotificationLog.")
            self.stdout.write(summary)
            logger.info(summary)

        # --- Company-wide aggregate digest (Admin + HR) ---
        # One invocation does both individual and aggregate. Skipped when --user restricts
        # to a single pre-flight recipient (you don't want a --user test firing two real
        # aggregate emails). The placeholder guard aborts ONLY this section — individual
        # digests above have already been processed and are unaffected.
        if only_email:
            self.stdout.write('Skipping aggregate digest (--user restricts to a single recipient).')
        else:
            self._run_aggregate(today, date_str, app_url, dry_run)

    def _company_totals(self, today):
        """Company-wide totals — every user's activity counts, no actor filter."""
        from projects.models import Task, ActivityLog

        # Metric 1: open assigned tasks across active (not soft-deleted) projects.
        assigned = (
            Task.objects
            .filter(assigned_to__isnull=False, phase__project__is_deleted=False)
            .exclude(status=Task.DONE)
            .count()
        )
        totals = {'assigned': assigned, 'started': 0, 'closed': 0,
                  'issues_raised': 0, 'issues_resolved': 0}
        # Metrics 2-5: today's activity, any actor, distinct entity_id (distinct tasks/issues).
        rows = (
            ActivityLog.objects
            .filter(timestamp__date=today, action_code__in=CODE_TO_METRIC.keys())
            .values('action_code')
            .annotate(c=Count('entity_id', distinct=True))
        )
        for row in rows:
            totals[CODE_TO_METRIC[row['action_code']]] = row['c']
        return totals

    def _run_aggregate(self, today, date_str, app_url, dry_run):
        from django.contrib.auth.models import User
        from projects.notifications import send_aggregate_email

        admin_email = (getattr(settings, 'ADMIN_DIGEST_EMAIL', '') or '').strip()
        hr_email    = (getattr(settings, 'HR_DIGEST_EMAIL', '') or '').strip()
        recipients = [
    ('Admin', admin_email, admin_email),
    ('HR', hr_email, hr_email),
        ]
        

        totals = self._company_totals(today)
        self.stdout.write(
            f"[aggregate] company totals ({date_str}): assigned={totals['assigned']} "
            f"started={totals['started']} closed={totals['closed']} "
            f"raised={totals['issues_raised']} resolved={totals['issues_resolved']}"
        )

        ctx = {
            'recipient_name': 'Team',
            'heading':        'Company-wide EOD Summary',
            'intro':          "Here is the whole team's activity today (IST).",
            'assigned_label': 'assigned across the team',
            'footer_note':    'Company-wide totals across all users for today (IST).',
            'date_str':       date_str,
            'metrics':        totals,
            'app_url':        app_url,
        }
        html_body = render_to_string('projects/email/eod_digest.html', ctx)
        text_body = render_to_string('projects/email/eod_digest.txt', ctx)

        placeholder = [cname for _label, cname, val in recipients if 'REPLACE_WITH' in val]

        if dry_run:
            for label, _cname, val in recipients:
                self.stdout.write(f"[aggregate][dry-run] would send to {label}: {val or '(unset)'}")
            if placeholder:
                self.stdout.write(
                    f"[aggregate][dry-run] WARNING: {', '.join(placeholder)} still contain "
                    f"a placeholder — a real run would ABORT the aggregate send here."
                )
            return

        # Real run: refuse to send while a placeholder remains. Raised AFTER the individual
        # digests, so only the aggregate portion is aborted (per spec).
        if placeholder:
            raise CommandError(
                f"Aggregate digest NOT sent — {', '.join(placeholder)} still contain "
                f"'REPLACE_WITH'. Set the real address(es) in settings/env. Individual "
                f"digests were already processed and are unaffected."
            )

        subject = f'Company-wide Horizon Solar EOD Summary — {date_str}'
        for label, _cname, val in recipients:
            # Resolve the address to a UserProfile (by email) so the send lands in
            # NotificationLog; if there's no matching account, it still sends but is
            # recorded only in the application log (see send_aggregate_email).
            prof = None
            u = (User.objects.filter(email__iexact=val, is_active=True)
                 .select_related('profile').first())
            if u is not None:
                prof = getattr(u, 'profile', None)
            send_aggregate_email(
                to_email=val, subject=subject, text_body=text_body, html_body=html_body,
                log_recipient=prof, template_name='eod_digest_aggregate',
            )
            note = '' if prof else ' (no matching user account — logged to app log only)'
            self.stdout.write(f"[aggregate] sent to {label} <{val}>{note}")
